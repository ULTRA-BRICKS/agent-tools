# Guardian adaptive backoff for the single-tab patrol (agenttools-chatgpt 0.3.15)

Scope: `lib/agenttools_chatgpt` only (the existing native single-tab patrol; no new daemon, no
new browser path, no classifier). Source and offline tests; nothing here is live-proven.

## Problem (verified in 0.3.14)

- `patrol.cooldown_for` returned the literal Retry-After (clamped to 60..86400 s) or a flat 900 s.
  Repeated 429s never learned anything, and a larger server delay was clamped down.
- `Rotation.set_cooldown` redrew `next_due` on every extension, so a late burst moved the due time
  at random.
- The check visit under a machine hold picked the normal queue, and `_read_and_act` did not
  navigate when the page already showed the chat, so an old current-endpoint 429 could never get a
  new read or 2xx while the controller only re-read the stale DOM.
- A failed visit advanced the cursor with no durable per-chat retry deadline and no pressure
  statistics. Hold verification had no paced way to revisit its failed paths, and a legacy hold
  (no evidence) stayed silent forever without a visible state.

## Formula and constants (all validated, persisted in the rotation file)

`BackoffConfig` defaults: `base = 600`, `cap = 3600`, `jitter = 0.25`, `clean_visits = 3`,
`episode_gap = 120`, `transient_base = 300`, `transient_cap = 1800` (seconds unless a count).
Validated ranges: `300 <= base <= 3600`, `base <= cap <= 86400`, `0 <= jitter <= 0.5`,
`1 <= clean_visits <= 10`, `30 <= episode_gap <= 900`, `60 <= transient_base <= transient_cap <= 3600`.

Pressure level `L` is an integer in `0..L_max`, where `L_max` is the smallest level with
`base * 2^(L-1) >= cap` (4 with the defaults).

- Ladder: `ladder(L) = 0` for `L = 0`, else `min(cap, base * 2^(L-1))` -> 600, 1200, 2400, 3600.
- Delay window `delay_range(L) = (low, high)`: `(0, 0)` for `L <= 0`; with `top = ladder(L) * (1 + jitter)`
  it is `(ladder(L), top)` while `top <= cap`, else `(min(ladder(L), cap / (1 + jitter)), cap)`. With
  the defaults: L1 `[600, 750]`, L2 `[1200, 1500]`, L3 `[2400, 3000]`, L4 and above `[2880, 3600]`.
  Jitter is positive and survives at the cap (the window is shifted down, not clamped to one point,
  so long-running retries do not synchronise); only `jitter = 0` is deterministic.
- Adaptive delay after a 429 episode: `adaptive(L, u) = low + (high - low) * u` with `u` in `[0, 1)`
  from the injected generator. `effective_interval` uses the same window as floor and ceiling.
- 429 cooldown deadline: `until = now + max(adaptive(L, u), retry_after)` where `retry_after` is the
  parsed Retry-After (delta-seconds or HTTP date, never clamped, never shortened; a past date or
  an absent header contributes 0). A burst of 429s inside one episode only ever extends `until`
  to `max(until, now + retry_after)`; it draws no new adaptive delay and never moves the level.
- Strike (level `+1`, capped at `L_max`): the first 429 after a new issued visit/probe
  (`pressure.issued != pressure.strike_issue`), or a 429 outside an active cooldown that arrives at
  least `episode_gap` after the previous 429. Repeated responses inside one visit, or while the
  account is cooling, are one episode. 100 subresource 429s therefore cost exactly one level.
- Recovery: every clean visit adds one to `clean_streak`. CLEAN means a fresh 2xx of THIS chat's own
  bare read endpoint (`/backend-api/conversation[s]/<uuid>`) seen in the current visit
  (`confirmed_at >= visit start`), with no 429 and no 5xx in it. The DOM outcome alone is never
  evidence: `complete`, `draft`, `unknown`, `failed:*`, `working` and `approval:*` are `unproven`
  (`backoff.outcome_kind`), and an unproven visit WITHOUT that 2xx is neutral (no level change, no
  success-rate sample, the chat stays visible and retained, its retry is not cleared, `last_ok_at` is
  not set). With the 2xx a draft/unknown/failed task outcome is still transport-ok: transport health
  and task completion are separate. After `clean_visits` clean visits the level drops by ONE and
  the streak resets.
  A 429 resets the streak. A 200 of any endpoint (avatar, list, an unrelated chat) never touches
  the level: only visit outcomes do.
- Visit spacing under pressure: `draw = max(base_draw, adaptive(L, u))` with the same single `u`
  that produced `base_draw = low + (high - low) * u` (default `240..360`). At `L = 0` it is exactly the
  old draw, so the base interval is unchanged.
- 429 retry deadline: ONE deadline, no second interval after it. `next_due` is the max of the
  Retry-After deadline, the jittered adaptive deadline and the minimum spacing deadline; the first two are already in
  `cooldown.until` and the third is the spacing draw taken at the visit start, so
  `next_due = cooldown.until` whenever the cooldown outlasts it (the cooldown record has no `draw`).
  A later or longer Retry-After still extends both; a burst never moves them again. With zero
  jitter repeated 429s are due exactly 600, 1200, 2400 and then 2880 s (window floor at the cap)
  after the visit start. A restart redraws an overdue due time once from now (no catch-up burst) and
  keeps a still-future one exactly; a visit that overruns its own draw is likewise redrawn once.
- Transient failures (timeouts, navigation errors, 5xx on the account API seen during a visit):
  per-chat retry `retry.at = now + min(transient_cap, transient_base * 2^(failures-1) * (1 + jitter*u))`
  (300, 600, 1200, 1800 s cap). Two consecutive transient visits (any chats) also push the
  account-wide `next_due` by the same formula on the account streak, bounded by `transient_cap`.
  Never a hold, never a `stop:*` control. A clean visit resets the account streak.
- Inconclusive probe (read-only check visit saw neither 429 nor a matching 2xx): quiet period
  `adaptive(max(L, 1), u)`, no strike; a legacy hold that can never be verified waits `cap`.
- Retries persist indefinitely at the cap; no maximum retry count and no drop.

## Fair durable queue

- Rotation state schema 2 (migration from 1 is lossless: queue order, cursor, visit counts, Go
  record, owned URLs, controls, evidence are copied; new fields are `pressure`, `backoff`,
  per-entry `retry`, `last_ok_at`, `confirmed_at`; the cooldown keeps its four schema 1 keys). `Rotation.read` migrates in
  memory only and never writes; `Rotation.open` persists the migration.
- A failed visit keeps the chat enrolled with `retry = {at, reason, failures, since}`. It never
  sets `last_ok_at`, so it cannot count as fresh, successful or done.
- `pick`: the cursor still advances on every in-order visit, entries whose `retry.at` is in the
  future are skipped (kept, not erased), and due retries alternate with cursor order
  (`pressure.retry_turn`), so one failing chat can use at most every second slot and never starves
  the rest. Owner-paused, stopped and unwatched chats stay in the queue as excluded rows.
- No visit, navigation or Go happens for an account while its 429 cooldown is active; another
  account has its own rotation file, lock and pressure and is independent.
- Under a machine 429 hold the due visit is a paced controlled probe: the target is the known
  chat whose read endpoint (`/backend-api/conversation[s]/<uuid>...`) is unverified (oldest visit
  first); without a mappable endpoint (list, legacy) the ordinary cursor chat is read, whose page
  load fetches the list. The probe ALTERNATES with the ordinary cursor check: after a slot that
  went to a failing chat (`pressure.retry_turn`) the next slot is the cursor/retry choice, so with
  A answering 429 the slots are A, B, A, C, A, ... never A, A, A. B and C stay enrolled and get
  paced read-only visits; a neighbour's success never clears the hold or sends anything (only a later
  same-path 2xx of A does). A single-chat queue still probes that chat. A page already on the target is navigated again (one `goto`) only when it
  is idle (no draft, challenge or running response); otherwise the visit is deferred and the hold
  stays. Nothing is clicked or sent in the probe path.
- The hold clears only with a 2xx of the same exact path later than its latest 429, no newer
  401/403/quota, cooldown elapsed. A legacy or unknown hold gets an explicit `legacy-hold`
  cooldown (cap) plus a `FOREIGN_HOLD` unverified endpoint, is probed read-only at the cap cadence,
  and is never cleared by the clock.

## Telemetry (existing `status` / `monitor --json` rotation block, read-only)

`pressure`: level, max level, strikes, effective interval, transient streak, earliest retry
(`retry_at`, `retry_reason`), persisted `next_due`, last confirmed target 2xx, controlled probes,
observed 429 responses, bounded recent-visit outcomes with the success rate over those
observations only. `coverage`: never-clean count, oldest-unvisited age, retry-pending count,
next retry, remaining in this pass and explicit excluded chats. Nothing claims a universally safe
rate, and `registered`, `status=sent` or test fakes are never counted as proven.

## Tests (injected clock, generator and fake browser)

New: `tests/test_chatgpt_guardian_backoff.py` (pure ladder, parsing, migration) and
`tests/test_chatgpt_guardian_patrol_backoff.py` (scenarios). Updated deliberately because the
documented behaviour changed: the old clamp/default parametrization and the redraw-per-extension
test. Regression: rotation, rotation_view, patrol, patrol_hold, patrol_safety, patrol_serve,
monitor, single-tab coverage test files, one at a time.

## Not live-proven

Everything is offline. A real ChatGPT rate limit may behave differently; the first live
observation is the measurement.
