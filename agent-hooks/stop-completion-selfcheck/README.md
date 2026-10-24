# stop-completion-selfcheck

**Point:** `stop` · **Fail policy:** `open` · **Priority:** 50

Fires when the agent is about to **end its turn**, and injects a self-check as the block
message. It does this by **blocking the stop exactly once per cooldown window** (exit 10
with the prompt), so the agent has to run the check before finishing. A per-session marker
(TTL/cooldown default 30min) prevents both an infinite stop→block→stop loop and — since
agent-tools#529 — re-blocking on every single stop in a long-lived session: the marker
stays in place for the full cooldown window, so at most one block fires per window, not
one per Stop event.

## Cooldown, not consume-on-allow (agent-tools#529)

Earlier, the marker was **deleted** the moment a stop was allowed, on the theory that a
later genuinely-new task in the same session should re-prompt sooner than the full TTL.
In practice, scanning real session transcripts showed the opposite effect dominates: a
long-lived autonomous/watch-loop session (waiting on background agents, polling status,
`ScheduleWakeup`-driven wake-ups) ends its own turn with a Stop event constantly —
sometimes tens of seconds apart. Deleting the marker on every allowed stop meant almost
every one of those wake-ups was treated as a brand-new session and re-blocked with the
identical static prompt, training a ritual "Self-check: nothing new, still waiting" reply
instead of any real re-verification. Measured real gaps between firings in busy sessions
were as low as 5-70 seconds — nowhere near the intended 30-minute cadence. The marker is
now left in place until it goes stale, so re-blocking is capped at once per
`SELFCHECK_TTL_S`. The trade-off: a genuinely new task started inside the cooldown window
doesn't get an immediate fresh prompt — tune `SELFCHECK_TTL_S` down for a repo/workflow
where that matters more than avoiding spam. Since a marker is no longer deleted on
allow, something has to garbage-collect old ones: each new block opportunistically sweeps
`SELFCHECK_MARKER_DIR` for markers older than the TTL and removes them.

## Three prompt variants, picked from the transcript

A single static checklist fired on every turn regardless of content trains shallow "yep
all done" pattern-completion — a trivial Q&A reply doesn't need "did you push/deploy?",
and a generic checklist has no clause for an agent that just *offered* to check something
instead of checking it. So this hook reads the turn's own transcript (`transcript_path`,
forwarded by `cc_hook_bridge` at the TOP level of the v1 event — this hook also accepts it
under `args.transcript_path` for forward-compat with a future bridge/caller shape) and
classifies what happened since the last real user message — best-effort; any read/parse
failure falls back to FULL, the original behavior:

- **HEDGE** — the agent's reply matched a hedge-and-defer pattern ("I can check...", or its
  Russian equivalents such as "могу поискать" / "I can search") and made **no tool calls**
  this turn. Quotes the offer back and says: if it's cheap/reversible/read-only, do it now
  instead of asking.
- **LIGHT** — no tool calls at all, no hedge detected (a plain, complete text reply). One
  question — did you fully answer, or leave a gap — instead of the full engineering
  checklist.
- **FULL** — tool calls happened this turn (real work). The original checklist, unchanged:
  1. Did I finish *everything* in the request — code, commits, push, deploy, docs, cleanup,
     artifacts? What did I miss?
  2. Concrete follow-ups — a bug noticed, an improvement worth a ticket, dead code to remove?

## Why an agent-hook

This is the one rule that *can't* be anything but a Stop hook — it has to fire at the
moment the agent decides it's done, which is a turn-lifecycle event, not a tool call or a
git action. It converts the `task-completion-selfcheck` skill from "the model might
remember to do this" into "the model is reliably prompted".

## Configuration

- `SELFCHECK_MARKER_DIR` — where per-session markers live (default
  `~/.cache/agent-tools/selfcheck`)
- `SELFCHECK_TTL_S` — marker freshness / cooldown window in seconds (default `1800`).
  Clamped to a minimum of `1`: `0` or negative would otherwise mean "a marker is never
  fresh," which re-blocks every single stop forever with no way out short of the kill
  switch — a real foot-gun, since `0` reads like "no cooldown" but means the opposite.
  The clamp turns that mistake into "almost no cooldown" (effectively `1`) instead.
- `SELFCHECK_TRANSCRIPT_TAIL_LINES` — how many trailing transcript lines to scan when
  classifying the turn (default `500`); `0` (or negative) disables classification
  entirely, same as a missing transcript — always FULL_PROMPT
- `SELFCHECK_TRANSCRIPT_TAIL_BYTES` — byte budget for the bounded tail read (default
  `2000000`, ~2MB) — bounds I/O/decode cost to this window regardless of total transcript
  file size, independent of `TAIL_LINES`. A turn whose real start falls outside either
  bound is treated as unreliable and falls back to FULL_PROMPT (see "Three prompt
  variants" above) rather than risking a false LIGHT/HEDGE on a heavy turn it only
  partially saw.
- `SELFCHECK_FIRINGS_LOG` — where firing decisions are logged, one JSON line per
  invocation (default `<SELFCHECK_MARKER_DIR>/firings.jsonl`) — see "Usefulness logging"
- `SELFCHECK_DISABLE` — set to `1`/`true` to unconditionally allow every stop (kill
  switch); equivalent to dropping a `DISABLED` file in `SELFCHECK_MARKER_DIR`

## Usefulness logging (agent-tools#529)

Every invocation that reaches a block/allow decision appends one line to `firings.jsonl`:
`{ts, session, decision, harness, prompt_variant, hook_ms}` — `harness` is the bridge-stamped
source (`omp`, `claude-code`, `codex`, null for a bare event; added with agent-tools#1003 so a
harness whose Stop never fires shows up as a missing value instead of silence) — the two early-exit paths (a malformed
stdin event, and a marker-write failure) skip logging along with the decision itself, since
neither represents a real block/allow outcome worth counting. This hook only ever *writes*
that log — it never reads it back, so a logging failure can't change a decision (same
fail-open contract as everything else here). It exists so a follow-up tool (tracked
separately, agent-tools#529) can report, over time, how often each prompt variant fires and
how that correlates with what the agent did next — the empirical question this hook could
not previously answer at all.

**Growth is currently unbounded** — one row per Stop invocation across every session,
including allows, so a busy watch-loop session (a Stop every 30-70s) can add on the order
of a megabyte a day with no rotation. Point `SELFCHECK_FIRINGS_LOG` at a location you're
willing to prune manually, or watch it, until a rotation/size-cap policy lands alongside
the measurement tool this log is feeding.

## Kill switch

If this hook is ever misbehaving in a live session, `SELFCHECK_DISABLE=1` (env) or an empty
`DISABLED` file in `SELFCHECK_MARKER_DIR` makes every stop succeed immediately, with no
marker or log writes — no redeploy needed.

## Fail-open

`on_error: "open"`. If anything goes wrong (can't parse the event, can't write the
marker), it **allows** the stop — the worst failure mode for a stop-blocker is trapping
the agent unable to finish, so it errs the other way.

## Test

```bash
chmod +x stop_selfcheck.py
rm -rf ~/.cache/agent-tools/selfcheck
echo '{"event_id":"sess-1"}' | ./stop_selfcheck.py; echo "exit=$?"
# → decision":"block" with the self-check prompt, exit=10  (first stop)
echo '{"event_id":"sess-1"}' | ./stop_selfcheck.py; echo "exit=$?"
# → decision":"allow", exit=0  (second stop, same session — no loop)
echo '{"event_id":"sess-1"}' | ./stop_selfcheck.py; echo "exit=$?"
# → decision":"allow", exit=0  (third+ stop within the cooldown — still allowed, not
#   re-blocked; the marker is only stale, and re-blocks, once SELFCHECK_TTL_S has passed)
cat ~/.cache/agent-tools/selfcheck/firings.jsonl
# → one JSON line per invocation above (block, allow, allow)
SELFCHECK_DISABLE=1 sh -c '
  rm -rf ~/.cache/agent-tools/selfcheck
  echo "{\"event_id\":\"sess-2\"}" | ./stop_selfcheck.py; echo "exit=$?"'
# → decision":"allow", exit=0 (kill switch — checked before any marker/log write, so
#   nothing is written regardless of TTL/cooldown state)
```
