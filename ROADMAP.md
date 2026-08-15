# Agent CLI Ecosystem — Roadmap

Status snapshot: **2026-07-23** (newest session block first, right below the north-star). This is
the handoff/roadmap for the agent-native CLI ecosystem (`tg-cli`, `review-cli`, `rig-cli`,
`draw-cli`, `3d-cli`, `task-cli`) + the `agent-tools` umbrella.

> **Status legend:** ✅ = done AND CTO-reviewed · 🔬 = shipped/merged, AWAITING CTO review
> (the CTO reserves ✅ for after he reviews) · 🔮 = building now (in-flight this turn, not yet
> merged) · ⏭️ = skipped (with reason) · ⛔ = blocked · ⏳ = pending / still open / unclear ·
> 📋 = informational (narrative, context, rationale, catalog/reference entry — not a discrete
> work item; excluded from any completion-percentage count). A merged PR is 🔬, never ✅, until
> the CTO signs off. **Every bullet in this file carries one of these markers** (backfilled
> 2026-07-25, agent-tools#338: all 431 bullets classified against real repo state where
> checkable — no bullet is left unmarked). ⚠️ Caveat: this is a chronological log, so the SAME
> work item can appear in more than one dated block, each carrying the marker that was true
> *at the time that block was written* — a raw count across the whole file double-counts
> repeated mentions. The **§Open-ticket ledger** and the **newest dated session block** are the
> two sections meant to hold one live status per item; treat older dated blocks as history, not
> an additional vote. **Completion-percentage formula (raw whole-file bullet count, the
> methodology used for the number in agent-tools#338):** done = ✅ + 🔬 · remaining = ⛔ + ⏳ + 🔮 ·
> excluded from both = ⏭️ + 📋 · percentage = done / (done + remaining). This is a **bullet-count**
> approximation, not a **work-item-count** — per the caveat above it over-counts repeated
> mentions of the same item, so treat the resulting percentage as directional, not exact; for a
> precise per-item count, tally the §Open-ticket ledger and newest dated block only.

> **Reading order:** the AUTHORITATIVE current state is the **newest dated session block at the very
> top** (currently **2026-07-18 → 07-23**), then **§Remaining work (prioritized)** and the **§Open-ticket
> ledger** below. ⚠️ The dated session blocks lead; the lower §Remaining-work / §Open-ticket ledger lag
> behind the newest block and may still list items the top block has since shipped — when they disagree,
> the newest dated block wins. The two collapsed `📜 …` blocks are older dated transcript (2026-06-15 →
> 06-17), kept for history; a folded section is not an assertion that it shipped.

> **North-star architecture (DECIDED — do not re-litigate):** the whole CLI ecosystem is
> **Python-only**. `tg-cli` (currently Bun/TS) migrates to Python **LAST**. The shared
> `agent-tools/lib` is a **plain Python library** (NO `lib/ts`, NO cross-language contracts).
> New tools are built in Python on that lib so a new tool spins up fast on one architecture.
> `rig` is the front door (`rig init` onboarding, `rig apply` reconcile — two distinct commands,
> interactivity orthogonal). `agent-tools` = WHAT (the catalog), `rig` = HOW (installs it).

---

## 2026-08-15 — two new provisioning targets scoped (⏳ design done via `review brainstorm`, nothing shipped)

*Answers two direct CTO questions: does the roadmap cover performance checks (no — the only prior
mention is the task-cli classifier's own p50/p95 latency benchmark, see the "task-cli classifier"
entry further down; nothing at the ecosystem/CI-gate level), and can rig configure task-cli's
ticket template/work logic (not yet — the read side already exists, the write side doesn't, see
below). A `review brainstorm` (5 rounds, claude:claude-opus-4-8 + codex:gpt-5.6-terra +
zai:glm-5.2, moderated) ran against rig-cli's actual current code
(`riglib/schema.py`/`config.py`/`config_schema.py`, task-cli's `tasklib/config.py`); the diff
recording it then went through two rounds of this repo's own `review diff` gate, which caught and
corrected one factual error in the brainstorm's own assumption plus several internal
contradictions the first write-up introduced while fixing that error. Both tracked under
rig-cli#270 (attach the full transcript there instead of the local log path this entry no longer
carries). **This entry is a directional design snapshot, not a spec — the "Known open design
questions" subsections below are real, review-caught gaps that the implementing PR must resolve,
not oversights to re-litigate here.***

### (1) Performance-check provisioning — not started, design converged on stateless A/B

- ⏳ **No perf/benchmark CI gate slot exists in `agent-tools/ci/` today.** `rig apply` provisions
  secret-scan / dependency-review / license-policy / tests as reconciled targets via
  `riglib/catalog.py`, but none of them carry state across runs (pass/fail per run). Perf
  regression is different — the panel's first instinct (a checked-in `bench-baseline.json`,
  hyperfine/criterion-style) was **rejected in round 4** in favor of something cheaper.
- ⏳ **Adopted design: same-job interleaved A/B, not a stored baseline.** Check out the PR's
  merge-base into a worktree in the *same* CI job, interleave `base, head, base, head, …`, compare
  the median of per-pair ratios. This deletes the entire stored-baseline trust surface in one move
  — no baseline storage, no staleness/rebasing, no cross-runner keying, no "a fork PR could
  overwrite the trusted baseline" risk. Cost: ~1.2–1.5× a normal run *when setup is shared between
  base and head* — which only holds if their dependency sets match. A PR that touches a lockfile
  breaks that assumption (merge-base code would run against head's deps). The design must diff the
  merge-base/head lockfiles first and declare the run **inconclusive** (not a false regression) on
  any mismatch, or pay full dual setup and drop the 1.2–1.5× claim for that run.
- ⏳ **This is a new catalog-provisioned target type, not a bespoke one-off workflow file** — a
  reconciled `ci.performance` slot in `agent-tools/ci/` consumed via `riglib/catalog.py`, same
  shape as the existing gates. "Don't fan this out via the catalog to other repos" (below) is
  about the *rollout* being deliberately gated to one pilot repo first, not about the provisioning
  mechanism being different from the other CI gates.
- ⏳ **Advisory-first, ship the failure UX as the actual product.** Report-only on first rollout —
  a hosted CI runner is noisy enough that a hard latency gate false-fails, gets muted, and trains
  everyone to ignore it. The verdict goes in the check's **title/summary text**, not its name —
  GitHub branch protection matches required checks by *exact name*, so a name that encodes the
  verdict could never become a required check, which forecloses the enforce-mode path this same
  section commits to below. Map every non-regression outcome (inconclusive / bypassed / disabled /
  dependency-skew) to a terminal GitHub `neutral` conclusion under one **stable** check name — **a
  kill switch that just stops the job freezes every required check instead of disabling the
  gate**, which is the opposite of what a kill switch is for.
- ⏳ **Fork-PR safety boundary:** advisory-only on ephemeral hosted runners for fork PRs (running
  fork code on a self-hosted runner is a known compromise vector); blocking/"enforce" mode is
  same-repo branches only, and only after real false-positive-rate + cost-per-signal data exists.
- ⏳ **Pilot workload, singular:** rig-cli's own large-catalog plan construction (`plan.build`) —
  every-`apply`-invocation frequency, deterministic, setup-dominated, and it's the one repo with
  an obvious hot path users actually feel. Do not fan the *rollout* out to other repos until the
  pilot has proven the variance is manageable.
- ⏳ **Explicitly NOT building (for now):** a benchmark-storage service, stored-baseline
  infrastructure, canary tiering (only bites once a gate can block merges — quarters away at
  advisory-only), a general monotonic perf-policy engine.
- ⏳ **Known open design questions (review-caught, not resolved by this entry):**
  - The `neutral`-conclusion design as stated needs a Checks-API check run, which needs
    `checks: write` — but `pull_request` events from a fork PR get a read-only `GITHUB_TOKEN`.
    The flagship advisory path (fork PRs on hosted runners) can't publish the verdict-in-summary
    check this design depends on as written. The fix is a `workflow_run`-style trusted-reporter
    split, **not** `pull_request_target` — this gate executes fork head code (the benchmark
    itself), and running untrusted code in a `pull_request_target` context (which carries a write
    token) is the canonical Actions vulnerability, not a security-review-later alternative. A
    materially different, separately-designed provisioning shape from the existing gate workflows
    either way — not "same catalog shape as the other gates."
  - **As specified (required check + `neutral`-satisfies-it + advisory-only for forks), the
    enforce-mode gate is trivially bypassable by opening from a fork** — a fork PR always lands on
    the advisory `neutral` path, satisfying the required check with no real measurement. Possibly
    an acceptable trade-off for a perf (not security) gate, but it undercuts the "required check"
    framing and needs to be a named, explicit trade-off before enforce mode ships, not an emergent
    surprise discovered after.
  - Promoting the gate to a required check isn't automatic just because it's catalog-provisioned:
    rig-cli's ruleset promotion only recognizes slots listed in a fixed registry
    (`github_ruleset.CI_GATE_CHECK_CONTEXTS`, currently just `pr-checklist`/`review-threads`).
    The stable job/check name and its registry entry need to be specified as part of the
    enforce-mode work, not assumed.
  - Where the per-repo benchmark spec (command, warmup, threshold) actually lives — `rig.yaml`
    (landing on the same schema surface section 2 is extending), a repo-local file, or the
    catalog — is unspecified. Design once, not twice, if it ends up in `rig.yaml`.

### (2) task-cli template/logic configurability via `rig.yaml`'s `task:` block — read side exists, write side doesn't

- ⏳ **Confirmed, live, currently one-sided.** `task-cli`'s `tasklib/config.py::rig_task_overlay()`
  (called from `load()`, not dead code) already reads a `rig.yaml` `task:` block and applies
  `enforce` (the ticket template: `acceptance_criteria`/`motivation`/`user_impact`/
  `cost_of_inaction`/`formatting`/`screenshots`) and `classify` (the work logic: the classifier's
  model fallback chain + its `change`-vs-`justAsk` bias) as an overlay on task-cli's own
  `task.yaml` defaults. `riglib/schema.py` and `riglib/config_schema.py` in rig-cli have **zero**
  knowledge of a `task:` key at all.
- ⏳ **Correction to the brainstorm's own working assumption (caught by `review diff`, not the
  panel):** rig-cli's config validator is already **strict**, not lenient — `riglib/config.py`'s
  `validate()` calls `_reject_unknown_keys(data, "")` unconditionally (`config.py:539`), fail-closed
  before anything is written. So a hand-added `task:` block does **not** get silently clobbered on
  the next `rig apply` — it makes every `rig apply`/`rig config`/wizard call on that repo hard-fail
  with an unknown-key error until `task:` is added to the schema. There is no data-loss bug and no
  strict-vs-lenient question left to resolve with a preservation test; the fix is simply **add
  `task:` to `riglib/config_schema.py`'s accepted key set** — that one change is what unblocks
  everything else in this section.
- ⏳ **`task.code_prefix` (rig-cli#217, still open, unmerged — `.ship-config`'s
  `SHIP_TASK_CODE_PREFIX` does not exist on `main` today, only on #217's own branch) is proposed
  to NOT route through `rig_task_overlay()`, and the roadmap must not describe it as if it already
  did.** The overlay's passthrough set is `{backend, github, linear, projects, enforce, classify,
  session}` (`tasklib/config.py:71`) — `code_prefix` is not in it, so once #217 lands and a repo
  sets `task.code_prefix`, task-cli's overlay hits the forward-compat branch and logs a `WARN` +
  skips on *every* invocation in *every* repo using it, forever — a **noisy** no-op, not a silent
  one. `code_prefix`'s design is a **separate consumption path**: rig-cli writes it into
  `.ship-config`'s `SHIP_TASK_CODE_PREFIX`, read by `ship.sh` directly, not through task-cli's
  runtime config cascade. Both keys can live as siblings under the same `rig.yaml` `task:` block,
  and #217 needs no task-cli code change to work as designed — but silencing the chronic warning
  does need one (teach task-cli's known-key set that `code_prefix` is intentionally foreign);
  that's a real, separate follow-up, not automatic.
- ⏳ **Precedence gap for the planned overlay editor:** task-cli's real layer order (later wins) is
  `defaults → global → rig.yaml task: block → task.yaml → --config P` (`tasklib/config.py:319`).
  A repo with its own hand-tuned `task.yaml` — or one invoked with `--config` — silently shadows
  whatever the `rig.yaml` overlay sets. The editor must surface this: `rig status`/`rig config get`
  should warn when a `task.*` value it thinks it's controlling is actually overridden by a native
  `task.yaml`, or a CTO will see a successful `rig config set` that task-cli never actually honors.
- ⏳ **Recommended sequencing** (not started):
  1. **Now, rig-cli-only:** add `task:` to `riglib/config_schema.py`'s accepted top-level keys so
     a `rig.yaml` carrying one stops hard-failing. Whether the *nested* shape is strict or lenient
     is an open ownership-boundary question (see below) — resolve that as part of this step, not
     by default-matching this repo's existing schema style. A round-trip preservation test is
     still needed for the *write* path once the key is admitted (also below) — the earlier
     strict-vs-clobber question this entry originally reached for a preservation test to answer is
     separately resolved: rejection, not clobbering, is what happens today.
  2. **Merge #217** (`task.code_prefix`) under that named `task:` block — not a speculative
     generic external-tool-config registry, and not routed through `rig_task_overlay()` (see
     above; it goes to `.ship-config` instead).
  3. **Separate follow-up PR — the overlay editor**, scoped to `task.enforce.*` **only**
     (`rig setup` / `rig config set task.enforce.acceptance_criteria …`): tristate writes
     (`inherit` / `required` / `forbidden`, with a real `rig config unset`, never materializing a
     default "for completeness" — an absent `task:` block must stay absent), a vendored flat
     key-list in rig-cli + a grep-based contract test against a pinned task-cli ref (rejected: a
     shared-schema-import abstraction or a foreign-namespace config registry — both premature at
     N=1 consumer; "extract from the *second* real consumer, not the first"), provenance surfaced
     in `rig status`/`rig config get` output including the `task.yaml`-shadow warning above.
     **Framing matters:** a branch-local `rig.yaml` edit is not an authority boundary — `rig
     setup` editing `task.enforce.*` is "repo convention, task-cli enforces," never presented as
     CTO-mandated org policy. True enforcement needs a higher-trust managed policy floor that does
     not exist yet; don't ship a toggle that *looks* like a mandate before building the enforcement
     it implies.
  4. **`task.classify.*` gets no `rig config set` / wizard write path** — not because rig can
     stop it from taking effect (once step 1 admits `task:` to the schema, a *hand-edited*
     `task.classify` block passes validation and `rig_task_overlay()` honors it exactly like
     `enforce`; the read-capability is already live and step 1 doesn't and can't change that), but
     because rig-cli itself should not offer ergonomics for locally overriding a global
     classification policy. This is a narrower, more honest claim than "no exceptions" — it
     withholds the *convenience*, not the *capability*. If that capability gap turns out to matter
     in practice, closing it is a deliberate, separate decision (e.g. rejecting `classify` in the
     schema entirely), not a side effect of this sequencing.
- ⏳ **Explicitly NOT building:** the foreign-namespace config registry, a task-cli-published
  versioned schema contract (blocks on another repo's release cadence), and any `rig config
  set`/wizard write path for `task.classify.*` (see step 4 above).
- ⏳ **Known open design questions (review-caught, not resolved by this entry):**
  - **Strict-vs-lenient is an ownership-boundary question, not a style-consistency one.**
    Matching "this repo's existing schema style" (closed/strict) for the `task:` block's *nested*
    shape means every new key task-cli's own overlay learns later breaks `rig apply`/`rig config`
    on repos using it, until rig-cli ships a matching schema update — making rig-cli a
    release-gating validator for a namespace it doesn't own. That's the exact cross-repo coupling
    the "explicitly not building" bullet already rejects for a shared schema contract, just
    reached from the other direction. Needs an explicit decision: lenient over the *nested*
    `task:` shape (rig validates presence/type of the block, not its contents) with strictness
    reserved for keys rig itself writes (`code_prefix`), vs. accepting the coupling deliberately.
  - **"No preservation test needed" only holds for the read path, not the write path.** The
    fail-closed `validate()` proves a `task:` block can't be silently dropped *while the key stays
    rejected* — rejection is what's protecting it. Once step 1 admits the key, the risk moves to
    rig's own serializers: the wizard, or `rig config set` on an unrelated key, deserializing and
    re-emitting `rig.yaml` could drop hand-set `task:` sub-keys rig doesn't model (e.g.
    `task.session`, `task.backend`, if the step-3 editor only models `task.enforce.*`). A
    round-trip preservation test for the *post-step-1 write path* is still needed — just not for
    the strict/lenient question this entry originally raised it for.
  - **`task.enforce.*` is not uniformly tristate-shaped.** `screenshots` is a nested
    label-policy object, not a boolean-like toggle, and the enforcement policy may grow numeric
    fields (e.g. a minimum-count style setting) alongside the scalar ones. A flat
    `inherit`/`required`/`forbidden` editor fits the scalar boolean-like keys
    (`acceptance_criteria`, `motivation`, `user_impact`, `cost_of_inaction`, `formatting`); the
    step-3 editor needs to either scope itself explicitly to those, or define a separate typed
    editor + contract test for the nested/numeric ones. Don't build one flat editor and assume it
    covers the whole `enforce` shape.
  - **"Add `task:` to the accepted key set" is schema admission, not full validation.**
    `riglib/config.py::validate()` has no `task`-shaped runtime check today (no type/mapping
    validator for its contents) — step 1 needs its own validator (mapping type, nested-key
    policy per the strict/lenient decision above), not just a schema-registry entry, or a
    malformed `task:` value passes rig's validation and is silently dropped by task-cli's own
    foreign-`rig.yaml` fallback instead.
  - **The `task.yaml`-shadow warning (step 3) requires rig to parse task-cli's native config
    shape** — which is the exact cross-tool coupling the rest of this section avoids — and even
    then can't see the `--config P` layer (invisible to rig entirely), so the warning can never be
    complete. A CTO could still see a "successful" `rig config set` that task-cli silently ignores.
    The boundary-preserving alternative is task-cli exposing its own effective-config provenance
    (e.g. a `task config --explain`) rather than rig reverse-engineering `task.yaml`'s shape — but
    that's a cross-repo dependency of its own kind, so treat this as unresolved, not a
    straightforward step-3 deliverable.
  - **Mixed ownership inside one `task:` namespace has a cost that "both keys can live as
    siblings" glosses over:** task-cli must learn to ignore rig's key (`code_prefix`, the chronic
    warning) while rig must be lenient about task-cli's keys but strict about its own — two tools
    each special-casing the other's keys in the same block. A `task.rig.*` sub-namespace for
    rig-owned keys (or moving `code_prefix` out of `task:` entirely, since it doesn't flow through
    task-cli's cascade anyway) would avoid this; the sequencing below merges #217 under `task:`
    before this ownership question is actually decided.

---

## 2026-07-18 → 07-23 — multi-day autonomous backlog marathon (🔬 under CTO review)

*A long multi-day autonomous shipping run across the whole ecosystem, tracked live via `gh` throughout.
Headline: **38 PRs merged 2026-07-18 → 07-22** (each verified MERGED on its repo's `origin/main`, not a
self-report) across five repos — agent-tools 7, review-cli 4, rig-cli 13, tg-cli 12, task-cli 2 — plus
several notable items that landed just after that tally (below, also all verified MERGED). Separately, one
real unpatched security finding is held back deliberately (see below, no exploit details in this public
file), and a systemic CI-gate problem still needs a CTO decision. Within the "Held / deferred" section
below, each bullet's own marker says its actual state (⛔ = genuinely blocked/held, not merged; ⏳ =
deliberately not merged, pending review; 📋 = informational, not a merge claim; 🔬 = a real correction or
completed piece of work that itself IS merged/verified — e.g. the task-cli #56 correction, the `dev-cli`
extraction, the cc-restore investigation). As with every block above, 🔬 is "shipped, awaiting CTO sign-off,"
never ✅.*

### Additional notable items (shipped after the 38-PR tally, each with a real finding behind it)

- 🔬 **rig-cli #179** — tmux **pane-titles** feature, merged. Also surfaced a real machine-side bug: a
  stale tmux-boot LaunchAgent carrying a wrong `PATH`. Root-caused, but the fix needs a one-time **manual
  restart of the tmux boot service on the real machine** — not something an agent should do to a live
  session unattended. **Alex: this restart is still owed on your side.**
- 🔬 **agent-tools #303** — `gh-graphql` alias (`ghgql`) + precision fix to `block-raw-pr-merge`. Several
  real security-adjacent bugs were found and fixed during review, not just the headline feature.
- 🔬 **tg-cli #232** — fixes the hook-disconnected UX: a message to a pane whose hook bridge is down now
  gets proper pane identification and **queued delivery**, instead of a silent drop.
- 🔬 **tg-cli #234** — a direct follow-up to #232: removes a **10-minute hard expiry** on queued messages
  that had crept in, which violated Alex's standing rule ("queue until resolved, never a timer"). Found and
  fixed a real per-invocation identity bug along the way (queued messages could be mis-attributed across
  invocations); fixed with nonce-based scoping.

### Held / deferred — deliberately, with real reasons (do not "just do it" without re-reading these)

- ⛔ **A real, unpatched security bypass exists on `block-raw-pr-merge`.** **Held privately, on purpose —
  NOT filed as a public GitHub issue**, because this repo is public and an issue would be premature exploit
  disclosure. The mechanics are intentionally NOT described here or in any public tracker. **Talk to the
  CTO directly** for the dedicated private fix. This is the top security item outstanding — tracked in
  "Remaining as of 2026-07-23" below.
- 🔬 **agent-tools #294 is now MERGED** (was listed here as held, not merged). The salvaged WIP pi
  permission-guard extension's 5 unresolved P1/P2 security findings were fixed by a dedicated security
  review pass, which also surfaced and fixed additional, worse findings before merge — tracked for an
  independent re-verify in "Remaining as of 2026-07-23" below.
- 🔬 **The review-threads CI gate is no longer blocking review-cli #157 or agent-tools #290 / #295** —
  all three have since merged (verified via `gh pr view --json state,mergedAt`; which of the two sanctioned
  resolution paths — `AGENTS.md` §gh-graphql's direct `resolveReviewThread` mutation, or `gh ship
  --resolve-addressed-threads` — applied per PR was not re-checked). review-cli #157 (provider-failover
  hardening) itself carried 3 P2 codex findings fixed on the PR plus 5 follow-up issues filed for other
  findings surfaced during the work (review-cli #169–#173, none blocking). Tracked in "Remaining as of
  2026-07-23" below.
- 🔬 **Correction to a prior assumption:** **task-cli #56**'s earlier finding ("the classifier denies
  merging non-authored PRs") turned out to be about one specific bad-faith retry attempt, **not** a
  permanent structural block. It was later legitimately merged by a different agent, who found and fixed a
  real credential-leak bug in it along the way. If any earlier part of this roadmap reads as "task-cli PRs
  can never be agent-merged," that was wrong — correct it on sight.
- 📋 **~35+ follow-up tickets** were filed across the ecosystem during the marathon for findings noticed
  but not fixed inline (real bugs, design gaps, doc inaccuracies). Not enumerated here — the exact list
  drifts as they get triaged; run `gh issue list` per repo for the current state rather than trusting a
  number written down at one point in time.
- 🔬 **`dev-cli` extraction complete** — successfully split out of `agent-tools` into its own standalone
  public repo, [`alex-mextner/dev-cli`](https://github.com/alex-mextner/dev-cli), earlier in this effort.
  Cutover verified.
- 🔬 **cc-restore mechanism (rig-cli, `riglib/tmux.py`) — investigated, CONFIRMED WORKING, no bug found.**
  It resumes the actual prior Claude Code conversation across a tmux server restart (`claude --resume
  <session-id>`), not just a fresh relaunch reusing the same window. Verified via isolated e2e tests plus
  live config inspection. No follow-up needed here.

### Remaining as of 2026-07-23 (a snapshot, not a permanent section — supersede on the next dated block)

- ⛔ **`block-raw-pr-merge` security bypass** — see "A real, unpatched security bypass exists on
  `block-raw-pr-merge`" above. Still the single biggest open item; talk to the CTO directly, no further
  status detail belongs in this public file.
- ⏳ **task-cli #72** — a real design disagreement, not a bug: the codex bot flagged
  `_daemon_bootstrapped`'s inconclusive-timeout-treated-as-healthy behavior as a P2 finding; Alex
  responded on the PR thread that it is a deliberate, already-reviewed trade-off (an inconclusive
  slow-but-fine bootstrap must never be misreported as a failure). PR is open, tests green, blocked only on
  whether the repo owner wants to resolve/merge it as-is or push back further — needs Alex's own call, not
  an agent's.
- ⏳ **Unverified: did the review-cli #157 / agent-tools #290 / #295 merges actually go through one of
  the two sanctioned resolution paths** (`AGENTS.md` §gh-graphql's `resolveReviewThread` mutation, or
  `gh ship --resolve-addressed-threads`), **or around the gate some other way?** That was not re-checked
  before this entry was written. Answer this before deciding whether "is the review-threads gate too
  strict" is even the right question to escalate to the CTO.
- ⏳ **agent-tools #294 (pi permission-guard) merged but not yet independently re-verified** — worth a
  second pass before treating the fixed findings as fully closed.
- 📋 **~16 untriaged bare branches across 5 repos**, found in a recent worktree audit — not yet triaged
  into "finish" vs "abandon" per branch. Needs a dedicated sweep.
- 📋 **Carried forward:** harden rig's provisioning of `ghgql`-style and `dev-cli`-style tools as
  first-class capabilities (rather than one-off installs bolted on after the fact) — both shipped as real
  tools this marathon but rig doesn't yet treat their provisioning as a reconciled, drift-checked target
  the way skills/agent-hooks/CI are.

---

## 2026-06-27 → 06-28 — shipped this session (🔬 under CTO review)

*Every **PR** below is **MERGED on each repo's `origin/main`** and is marked 🔬 (awaiting CTO review),
NOT ✅ — the CTO signs off to ✅ (the "Not a PR" subsection is non-PR machine/config changes, not merges).
The 🔮 / REMAINING blocks at the end are work still in flight or gated. Grouped by area; ~20 PRs across
the ecosystem. Headline of the session: the **q&a forwarding
bridge is fixed and LIVE end-to-end**, the **tg HTML→PDF render path is fixed + hardened**, and the
**agent-tools CI gates + a NEW anti-wedge agent-hook** landed and are live on the machine.*

### tg-cli — 🔬 under review (all MERGED) — q&a bridge LIVE + HTML/PDF render

- 🔬 **#98** — q&a bridge: stop the **duplicate `AskUserQuestion` card** (a forwarded question rendered
  twice). Part of getting the q&a forwarding bridge correct end-to-end.
- 🔬 **#101** — q&a bridge: fix the **120s tap-loss** — a button tap arriving after the idle window is no
  longer dropped; **pane-inject + lossless daemon restart** so an in-flight answer survives a restart.
  With #98 this makes the q&a forwarding bridge **fixed and LIVE**.
- 🔬 **#96** — `tg --format html` + the `--file *.md` → **PDF render path** emits RENDERED Telegram-allowed
  tags / a formatted PDF instead of raw tag-soup text. Closes the prior 🔮 "tg HTML→PDF raw-tags" item.
- 🔬 **#105** — md→PDF **security hardening**: **DNS-blackhole** the renderer's network + drop
  `--embed-resources` so a malicious `.md` can't exfiltrate or pull remote assets during PDF render.

### agent-tools — 🔬 under review (all MERGED) — CI gates + NEW anti-wedge hook

- 🔬 **#131** *(resolves #129)* — CI-gate fixes wave.
- 🔬 **#133** — leftover-marker grep: **two-dot fallback** so the leftover-grep gate works where the
  three-dot diff range is unavailable.
- 🔬 **#134** — **re-run the review-threads check on `review`-type events** so resolving a thread refreshes
  the status (the stale review-threads-status footgun).
- 🔬 **#132** — `no-long-inline-process` made **argv-aware** (parse the command, don't raw-match) so it
  scopes the long-process block correctly.
- 🔬 **#135** — **NEW `subagent-no-bg-longproc` anti-wedge agent-hook**: blocks a subagent backgrounding
  `review` / a long process (the exact wedge that strands a subagent mid-turn). **Live on the machine.**

### rig-cli — 🔬 under review (all MERGED) — rig-init redesign + install UX

- 🔬 **#80** — `uv` install hints + a **condensed plan** output (clearer apply UX).
- 🔬 **#82** *(v0.3.0)* — **`rig init` redesign**: bare `rig init` = **PREVIEW, non-destructive**;
  `--yes` scaffolds; `--yes --apply` scaffolds **and** applies. The non-destructive-by-default front door.

### review-cli — 🔬 under review (all MERGED) — OpenRouter + review-qa Tier-1 (catches the q&a bug class)

- 🔬 **#89** — **OpenRouter backend** (a new board transport / model source).
- 🔬 **#85 / #87** — **review-qa Tier-1**: the qa suite + the **agent-side driver**, RED→GREEN — it
  reproduces and catches the **q&a bug class** that tg-cli #98/#101 fixed (the harness now guards the
  forwarding bridge against regression).

### bots — 🔬 under review (all MERGED) — real-fixes wave (un-provisioned bots SKIPPED per CTO)

- 🔬 **ExpenseSyncBot #104**, **garage-band #3**, **HyperSummaryBot #8**, **HyperCalendarBot #100** — real
  bug-fixes on the live bot repos. ⏭️ **4 un-provisioned bots SKIPPED** per the CTO (no rollout without a
  per-repo OK).

### research-cli — 🔬 under review (MERGED) — spin-out populated

- 🔬 **#5** — the **standalone `research-cli` spin-out is populated** (the strategy-B standalone CLI now has
  its content). Unblocks the research-cli env-desync follow-ups (#2 / #6). Closes the prior 🔮 spin-out item.

### Not a PR — 🔬 under review (machine/config changes this session)

- 🔬 **Activation done** — the personal CLIs + the machine agent-hooks are **live** (the new
  `subagent-no-bg-longproc` hook fires), **rig is at 0.3.0**, and the **egg-info version-shadow** that masked
  the live `--version` is fixed.

### 🔮 / REMAINING — still in flight or gated (NOT shipped this session)

**In flight:**
- 🔮 **rig-cli #68** — `rig` **TUI auto-install** (in flight).

**(C) Creds / infra-gated — the one CTO-blocker:**
- ⛔ **review-qa Tier-2 LIVE tier is BLOCKED on CTO MTProto creds** — needs `TG_TEST_API_ID`,
  `TG_TEST_API_HASH`, `TG_TEST_SESSION`, `TG_TEST_CHAT_ID` (a dedicated test Telegram account). Tier-1 is
  green; Tier-2 cannot run without these. **The key CTO-gated action for live q&a regression testing** (the
  06-22→06-26 "(C)" block also tracks the rest of the Tier-2 surface — review-cli #82 visual diffing,
  screenshot infra, curated SUT repos).

**(D) Small / tracked — bounded follow-ups, just need a turn:**
- 🔬 **ship.sh `-R` flag** — let `gh ship` target a repo explicitly. Implemented (`ci/ship/ship.sh` has a
  `--repo`/`-R` cross-repo path, verified against the current script source).
- 🔬 **research-cli #2 / #6** — env-desync follow-ups (unblocked now that #5 populated the spin-out).
  Both CLOSED (`gh issue view 2/6 --repo alex-mextner/research-cli`).
- ⏳ **worktree / daemon-stray hygiene** — sweep orphan worktrees + stray daemons.
- ⏳ **rig egg-info-shadows-version** — the editable-install egg-info that can shadow `--version` (fixed on
  this machine; needs to be guarded so it can't recur).
- 🔬 **tg-ctl forum-topics** — the remaining forum-topics wiring. tg-cli #84/#56/#52/#86/#27/#67 all
  CLOSED (`gh issue list --repo alex-mextner/tg-cli`); one narrower residual bug (#185) stays open.

---

## 2026-06-22 → 06-26 — shipped this session (🔬 under CTO review)

*Everything below is **MERGED on each repo's `origin/main`** (verified via `gh pr list --state
merged --search "merged:>=…"`) but is marked 🔬 (awaiting CTO review), NOT ✅ — the CTO signs off to
✅. The 🔮 block at the end is work still building / deferred (not merged). Grouped by repo; each
line maps to the §Remaining-work / §Open-ticket item it advances, which is also annotated
`🔬 review (PR #…)` inline below. **Tally: 102 merged PRs across 7 repos** — tg-cli 27, rig-cli 15,
agent-tools 21, review-cli 15, task-cli 16, 3d-cli 5, draw-cli 3. The 06-24/06-25/06-26 post-wave is
called out per-repo below.*

### tg-cli — 🔬 under review (all MERGED) — multi-agent / forum-topic wave

- 🔬 **#47** — tg-ctl locale fix: force a UTF-8 locale for discovery (the REAL "Claude Code not in
  tmux" root cause — launchd has no locale → tmux mangles the `\t` pane separator → 0 panes).
- 🔬 **#48** — `tg` `parse_mode` follows `--format` only; plain text containing a URL / `--` / `:`
  no longer 400s (the "Only those HTML tags are supported" misfire). Maps to §9 parse_mode bug.
- 🔬 **#49** — file-excerpt: strip wrapping punctuation so a `path:line` spec in prose attaches its
  excerpt. Maps to §9 / tg-cli#29.
- 🔬 **#50** — the OUTBOUND `tg` sender honors `TG_API_BASE` (was hardcoded). Prereq for the
  review-qa bot-harness Tier-1 mock (review-qa §"bot harness").
- 🔬 **#51** — defer inbound for an UNSCOPED question too (tg#30 fail-OPEN fix — pairs with #60).
- 🔬 **#54** — forum-topics ROUTING half: topic→pane map + inbound route + outbound
  `message_thread_id`. Advances the forum-topics mode / tg-cli#27 wiring.
- 🔬 **#55** — reply-misroute fix: a recognized reply route never loses to the global registration
  (#53). Pairs with the per-pane registration set (#69).
- 🔬 **#60** — a SCOPED forwarded question waits INDEFINITELY for its Telegram answer (no idle-exit
  on a pending question). Maps to the decisions-as-buttons / hanging-question danger (§9, tg-cli#30).
- 🔬 **#61** — `tg --topic` / `TG_TOPIC` outbound threading (forum-topics increment 2).
- 🔬 **#62** — `/agent` inline-keyboard picker with distinct cwd labels (closes #59).
- 🔬 **#64** — `/agent` picker buttons-only (drop the redundant window header + text list).
- 🔬 **#66** — log every ask-forward attempt + outcome (no silent non-forward).
- 🔬 **#69** — per-pane registration SET: every live session's questions forward (multi-session
  forwarding, #67). Pairs with #55.
- 🔬 **#71** — self-provision the bot command menu via `setMyCommands` on startup.
- 🔬 **#73** — `--tag answer` requires `--reply-to` (hidden terminal-only bypass). Maps to the
  tg-otvet-requires-reply-to rule.
- 🔬 **#76** — ambiguous/gone target → an `/agent` button picker, not a text dump.
- 🔬 **#77** — no-reply inbound auto-binds to the last-active agent + `/agent` picker uses tmux
  window names. (Superseded in part by #79's simplification.)
- 🔬 **#79** — no-reply inbound binds to the last-message agent (simplify the MRU machinery).
- 🔬 **#81** — single-source `tg` version from `package.json` (#80). Maps to the version-bump rule.
- 🔬 **#83** — ctl-integration test: guarantee + scope daemon teardown to stop process leaks.
- 🔬 **#85** — spawn an agent on `forum_topic_created` (the `/new` flow trigger; forum-topics incr 3).
- 🔬 **#87** *(06-24)* — forum-topics **increment-4** lifecycle polish (the `/new` flow remainder past #85).
- 🔬 **#89** *(06-24)* — `tg-ctl status` detects an EXTERNAL launchd autostart supervision (Closes #88) — no
  more false "not supervised" when the daemon is managed by launchd, not the in-process autostart.
- 🔬 **#90** *(06-25)* — docs(agents): correct the stale test count + scope `bun test` (the tg-cli #32 drift
  fix — a hardcoded count went stale; replaced/scoped so it can't drift again).
- 🔬 **#91** *(06-25)* — dead-letter the backlog stranded by an unscoped-question expiry (Closes #58). Pairs
  with the #51/#60 unscoped-question fail-OPEN saga — a backlog isn't silently lost on expiry.
- 🔬 **#92** *(06-26)* — `/new` flat-chat command: spawn an agent via inline buttons (Closes #27) — the
  interactive new-agent flow past #85's spawn-trigger + #87's lifecycle polish. Closes the largest single
  open tg-cli feature (the prior ⏳ #27 / 🔮 `/new` item).
- 🔬 **#94** *(06-26)* — `cleanExit` removes the pidfile only when it OWNS it (Closes #93) — a flock-loser no
  longer deletes the winner's pid-file on exit (the daemon-singleton ownership fix).
- 🔬 **issue #31** *(06-26, CLOSED)* — **CodeQL JS/TS: all 8 pre-existing TS findings fixed-or-justified**
  (surfaced by the #25 rig rollout). Clears the prior §"tg-cli #31 CodeQL" residual item.

### review-cli — 🔬 under review (all MERGED) — advances §"review qa — agent-as-tester mode"

- 🔬 **#53** — brainstorm fails loud on a MID-RUN dead-panel collapse, not just round 1. Maps to §9
  "review brainstorm must FAIL LOUD when backends return empty".
- 🔬 **#55** — review-qa **Phase 1**: mode skeleton + no-suites gate (the registry-wiring slice).
- 🔬 **#59** — board: GLM 5.2 via commandcode as a read-only seat (priority 3, under Opus).
- 🔬 **#62** — review-qa **Phase 2**: write/exec agentic executor + judge + 2-fixture DoD.
- 🔬 **#64** — review-qa **Phase 3**: SUT env bring-up (detect / reuse / health-gate / guaranteed
  teardown). Advances review-qa §"Backend SUT…".
- 🔬 **#66** — board resilience under concurrent load: memory cap + slow-seat deprioritize.
- 🔬 **#68** — review-qa **bot Tier-1** hermetic harness (fake Telegram + inject/capture). Consumes
  tg-cli #50's `TG_API_BASE` prereq. Advances review-qa §"bot harness".
- 🔬 **#70** — review-qa **web Tier-1** deterministic harness (Playwright bring-up + DOM-driving
  driver). Advances review-qa §"web Playwright harness".
- 🔬 **#72** — `review diff --staged -C <repo>` honors the `-C` repo + strips leaked git env (#71).
  The git-env-leak cwd-footgun fix.
- 🔬 **#74** *(06-24)* — review-qa **ext (VS Code) Tier-1** deterministic harness (isolated VS Code +
  command-driving driver). Closes the ext-harness item the prior 🔮 block listed as deferred.
- 🔬 **#77** *(06-24)* — the **opus review seat runs via `claude --print`**, not the claude-p TUI-scraper —
  the default board's premium seat no longer silently degrades when the TUI path breaks. Maps to §"review
  resilience" + the board-seat stability lessons.
- 🔬 **#79** *(06-26)* — board: resolve seats by **capability/role from the shared `models.yaml` manifest**
  (the rig-cli #8 consumer half) — the board no longer hardcodes model ids; it reads the capability flags.
  Advances rig-cli #8 models.yaml consumer capability-wiring.
- 🔬 **#80** *(06-26)* — qa: detect Python Telegram bots in `--kind auto` + document `qa` in help/config
  (the §"D" `--kind auto` bot-detection follow-up, ex-#61).
- 🔬 **#81** *(06-26)* — qa: forward the `-m` model suffix to the tester + reap the ephemeral-worktree trust
  (the §"D" `-m` forward follow-up, ex-#60).
- 🔬 **#83** *(06-26)* — qa: harden the ext harness — queue-backed stdout reader + isolation warning
  (the §"D" ext-harness live-runner robustness follow-up, ex-#75).

### rig-cli — 🔬 under review (all MERGED)

- 🔬 **#53** — provision sub-agents (a `.claude/agents/*.md` category, global + repo-local). REVERTED
  by #55 — CTO decided to SKIP custom agents (agents-skip decision).
- 🔬 **#54** — docs(specs): rig provisioning designs (github-settings / subagents / cross-harness).
- 🔬 **#55** — revert the subagents-provisioning category (#53) — CTO decided to SKIP custom agents.
- 🔬 **#56** — codex skills → `~/.codex/skills`; opencode auto-loads natively. Maps to §9 multi-harness
  skill provisioning (rig-cli#9).
- 🔬 **#58** — skill conflict-backups (`rig-bak`) land OUTSIDE the natively-scanned skills dir.
- 🔬 **#60** — drop the dead `mcp.items.review` slot from the shipped template (the `review --mcp`
  removal residue). Maps to §9 "`review --mcp` broken ecosystem-wide".
- 🔬 **#62** — test: fake catalog mirrors reality (drop the fabricated removed mcp/review slot).
- 🔬 **#64** — `rig status` missing-target scan follows the configured settings path, not a
  hardcoded `~/.claude` (#63). Maps to the error-system v2 *missing-target* heuristic.
- 🔬 **#67** — `rig apply` installs the personal CLI ecosystem (tg/review/task/draw). The provisioning
  half of "rig is the front door for the ecosystem".
- 🔬 **#69** — register the #67 `tools` category in the area/layer registries (so `rig status` reports
  the CLI ecosystem). Maps to §"rig status must cover ALL reconciled areas".
- 🔬 **#71** — `rig --version` reads `pyproject` dynamically (no stale literal; 0.2.0). Version-bump rule.
- 🔬 **#72** — regression-guard test: rig provisions `require-ticket-before-commit` (#65). Pairs with
  agent-tools #95.
- 🔬 **#74** — CI runs the real-catalog pytest guards so they actually gate.
- 🔬 **#76** — docs(spec) fix: correct the false "CC ships no built-in agents" claim (the agents study).
- 🔬 **#78** *(06-26)* — docs(spec): mark `rig-subagents-provisioning` as **SKIPPED** (not planned work) —
  the canonical spec now carries the skip-marker, pairing with agent-tools #124's orphan-spec removal.

### agent-tools — 🔬 under review (all MERGED)

- 🔬 **#78** — require-review block message no longer claims "docs-only allowed" unconditionally.
- 🔬 **#79** — AGENTS doctrine: an unused tool/skill/MCP is a signal to FIX, not delete.
- 🔬 **#82** — AGENTS doctrine: orchestrator-only — all work runs in subagent swarms.
- 🔬 **#83** — orchestrator-stays-thin: never block a fully read-only shell chain (orch-hook pipes).
- 🔬 **#85** — research-cli adopts the shared `agenttools_errors` layer (what/why/how-to-fix +
  structured exit codes). Maps to §6 research-cli + the error-system v2 generalization (§3 `errors`).
- 🔬 **#90** — model-freshness CLI adopts the shared `agenttools_errors` layer (#89). Maps to §4
  model-currency + the error-system v2 generalization.
- 🔬 **#93** — ROADMAP-mark: record the 2026-06-22 shipped wave as 🔬 under CTO review (predecessor of
  THIS refresh).
- 🔬 **#95** — `require-ticket-before-commit` STRICT by default + rig-provisioned (Closes #92). The
  re-enable+strict opener of the require-ticket saga (pairs with rig-cli #72).
- 🔬 **#96** — research-cli test: skip the yaml-less NETWORK-class test (hidden PyYAML dep). The
  research-cli bugfix companion to #85.
- 🔬 **#99** — `bump-version-on-release` skill + a version-bump `ship.sh` gate. The single-source-version
  rule the per-tool `--version` PRs implement (tg #81, rig #71, task #23, 3d #10, draw #6).
- 🔬 **#100** — require-ticket saga: argv-scope detection so a "commit"-mention no longer over-blocks.
- 🔬 **#102** — require-ticket saga: `-F`/`--file` read parity (`-F -` stdin handled).
- 🔬 **#103** — research-cli: machine-readable `--json` output for `research ask`.
- 🔬 **#105** — require-ticket saga: flip `-F -` (stdin) from fail-open to fail-closed-with-hint.
- 🔬 **#108** *(06-25)* — **OSS license-policy CI gate** (default-deny-copyleft, NO GHAS). Closes the
  long-standing license-policy gap (the one parity hole from the GHAS-de-GHAS work, ex-#21).
- 🔬 **#110** *(06-25)* — **Trivy filesystem CI gate** (vuln + secret + misconfig). Closes the Trivy gap
  agent-tools #24 flagged when removing hyper's bespoke `security-scan.yml` — now a shipped catalog gate.
- 🔬 **#111** *(06-25)* — `require-ticket`: de-cluster combined short flags so `-am <msg>` extracts the
  message (the `-am` bypass). Require-ticket saga; companion to #114.
- 🔬 **#116** *(06-25)* — `skills-read-gate` exempts DISPATCHED SUBAGENTS from the orchestration/visual
  defaults (a subagent's own tool use is no longer over-blocked by the mandatory-skills gate).
- 🔬 **#117** *(06-25)* — `block-no-verify` emits an ACTIONABLE fail-closed message (not the opaque "could
  not inspect") — the fail-closed boundary now tells you why + how to proceed.
- 🔬 **#118** *(06-25)* — `orchestrator-stays-thin` reads ONLY the sanitized `args.agent_id` (#115) —
  closes a forged-exemption hole where a faked agent id could dodge the orchestrator-thin gate.
- 🔬 **#119** *(06-25)* — `require-ticket` skips value-bearing git-commit flags (#114) so a value like
  `--author`/`--date` is no longer mistaken for the commit message — closes the value-flag bypass.
- 🔬 **#124** *(06-25)* — docs(roadmap): resolve #106 — **drop the orphan `subagents-provisioning` spec**
  (an unmarked "not yet implemented" spec masquerading as planned work; the design is preserved in rig-cli's
  canonical spec via #78's skip-marker). Pairs with rig-cli #78.

### task-cli — 🔬 under review (all MERGED) — advances §"1. task-cli"

- 🔬 **#11** — v1: `task new` / `task done` verb aliases (+ clean ruff), usable against GitHub Issues.
- 🔬 **#14** — install writes the SessionStart `.blurbs/task.md` entry too (the rollout/blurb half).
- 🔬 **#17** — Docker real-agent integration harness: task-cli advertised + usable on a fresh machine.
- 🔬 **#18** — validate transition legality on all three close paths (#10, the close-path validation).
- 🔬 **#21** — harden the Docker real-agent leg vs the Task-tool collision + assert require-ticket
  enforcement.
- 🔬 **#23** — resolve `--version` dynamically from `pyproject` (0.2.0; version-bump rule).
- 🔬 **#26** *(06-24)* — **due-date reminder daemon** + `--due` field. Advances task-cli#2 (daemon) / #3
  (due-notify) — the Phase-2 daemon foundation.
- 🔬 **#28** *(06-24)* — **read-only due-date timeline (Gantt) view**. Advances task-cli#1 (deps + Gantt).
- 🔬 **#30** *(06-24)* — align list columns by terminal DISPLAY width, not code-point count (#29) — wide /
  emoji-bearing rows no longer mis-align.
- 🔬 **#31** *(06-25)* — bring `task install-skill` to 3-layer parity with the sibling CLIs (skill +
  SessionStart blurb + harness wiring). Maps to the install-* parity rule.
- 🔬 **#33** *(06-25)* — daemon: **PID-identity guard on stop + flock singleton** (#25) — a long-argv or
  stale daemon can't be wrongly stopped/duplicated.
- 🔬 **#34** *(06-25)* — Docker test: guard the **vendored require-ticket hook against drift** (#20) — the
  vendored copy can't silently diverge from agent-tools canonical.
- 🔬 **#35** *(06-25)* — daemon: read full cmdline via `ps -ww` so a long-argv daemon isn't orphaned (the
  ps-truncation fix).
- 🔬 **#37** *(06-25)* — re-sync the vendored require-ticket fixture to the canonical `-am` de-cluster fix
  (agent-tools #111) — the drift-guard's companion re-sync.
- 🔬 **#39** *(06-26)* — classify: **resolve the classifier model from the shared `models.yaml` by capability**
  (the rig-cli #8 consumer half) — the auto-classifier no longer hardcodes a model; it reads capability flags.
  Advances rig-cli #8 models.yaml consumer capability-wiring.
- 🔬 **#40** *(06-26)* — daemon: make `status` / `start` **PID-identity-aware**, consistent with `stop` (#32) —
  closes the §"D" task-cli #32 status-identity follow-up to the #33 stop-path guard.

### 3d-cli — 🔬 under review (all MERGED)

- 🔬 **#10** — read the `3d` version dynamically from `pyproject` (#9; version-bump rule).
- 🔬 **#12** — gitignore stray `*.log` scratch files (proves the #11 pre-commit hook fix).
- 🔬 **#14** — bump `starlette` 1.2.1 → 1.3.1 to clear the Dependabot alert.
- 🔬 **#16** — track + auto-install the repo-dev pre-commit gate (Closes #15).
- 🔬 **#18** *(06-25)* — cover `scripts/install-dev-hooks.sh` with tests (Closes #17) — the hook-installer
  is now regression-guarded.

### draw-cli — 🔬 under review (all MERGED)

- 🔬 **#6** — add `draw --version` reading the version dynamically from `pyproject` (version-bump rule).
- 🔬 **#8** — resolve the symlink before deriving the repo root for `draw --version` (the live-symlink fix).
- 🔬 **#9** *(06-25)* — package as `draw_cli` for pipx + a pipx-first installer. Resolves the prior ⏳
  "draw-cli pipx" CTO-decision item — pipx is now the shipped install path.

### Not a PR — 🔬 under review (machine/config changes this session)

- 🔬 HA dead-MCP + its token removed from `~/.claude.json` (configured-but-inert MCP cleanup).
- 🔬 `rig apply` re-run (github repo settings reconciled).
- 🔬 agents-skip decision recorded (SKIP custom sub-agents — cc/oc-only; covered by Task tool +
  swarms). Codifies rig-cli #55 above.
- 🔬 agents study (read-only, reported): confirmed CC ships built-in agents (corrected the false spec
  claim → rig-cli #76); the git-hook near-miss was verified-restored, no loss.
- 🔬 **Stale-done issues CLOSED this session** (the ROADMAP had carried them as open — now resolved):
  **tg-cli #28** (`tg#<id>` ref + autolink), **#29** (file-excerpt attachment), **#30**
  (decisions-as-buttons + inject-collision — the unscoped-question fail-OPEN saga); **agent-tools #86**
  (model-freshness → shared `agenttools_errors`); **rig-cli #5** (repo security settings at init/apply),
  **#6** (auto-mode is user-level `auto`, not a committed project bypass — see the line-513 correction).

### 🔮 IN FLIGHT — building NOW this turn (as of 06-26; first two now SHIPPED — see the 06-28 block at top)

> **SUPERSEDED (2026-06-28):** the first two 🔮 items below **SHIPPED** in the 06-27→06-28 wave — do NOT
> resume them. research-cli spin-out B → research-cli **#5** (spin-out populated); tg HTML→PDF raw-tags →
> tg-cli **#96** (render path) + **#105** (md-pdf security). Both are recorded as 🔬 in the top 06-28 block.

- 🔬 **research-cli spin-out — strategy B** (standalone CLI) — **SHIPPED (research-cli #5)**: the
  standalone `alex-mextner/research-cli` is populated (vendored shared lib + drift-guard, the task-cli
  pattern, no CTO PyPI account). Advances §6 research-cli + the (B) "research-cli spin-out" call below.
- 🔬 **tg-cli HTML→PDF raw-tags fix** — **SHIPPED (tg-cli #96, hardened by #105)**: `tg --format html`
  and the `--file *.md`→PDF path now emit RENDERED Telegram-allowed tags / a formatted PDF instead of raw
  tag soup; #105 adds md-pdf security (DNS-blackhole + drop `--embed-resources`).
- 🔮 task-cli **daemon service + webhooks** (task-cli#2) — Phase 2, daemon FOUNDATION shipped
  (#26/#33/#35/#40), the webhook/adapter half still open (queued, not building this turn).

### ⏳ Decisions awaiting the CTO (not work — sign-off needed)

- ✅ **agent-tools #106** (CLOSED) — the orphan `docs/specs/rig-agents-provisioning.md` (the
  SKIPPED subagents-provisioning design) was DROPPED, not landed: the feature is dead (rig-cli #53
  reverted by #55, zero code), the design is preserved in the committed rig-cli spec
  `docs/specs/rig-subagents-provisioning.md` (#76, marked SKIPPED by rig-cli #78) + the
  `agents-skip-decision` memory. Removed the untracked file (#124).
- ✅ **tg-cli #27** (`/new` interactive flow) — **SHIPPED** as the `/new` flat-chat command (tg-cli #92):
  spawn an agent via inline buttons. The largest single open tg-cli feature is now closed (awaiting CTO ✅).

### 🧭 Genuinely-remaining work (re-derived 2026-06-26 — honest, post-wave)

*What is ACTUALLY left after the 06-22→06-26 wave, grouped by what unblocks it. Nothing here is ✅
(the CTO marks ✅). The big shipped items above already cleared most of the prior backlog; this is the
real residual. The one remaining IN-FLIGHT item (tg HTML→PDF fix) is still building; research-cli
spin-out is DONE (PR #128 merged) — see the 🔮 block above.*

**(B) CTO-decision-gated — needs a product/architecture call, not just execution:**
- ⏳ **rollout wave-2** (rig-cli #7) — `rig init --yes` + AGENTS/CLAUDE slim **applied to the bot repos**
  (and the tool repos themselves). A staged rollout that needs the CTO's go-ahead per wave + which repos
  to touch (no cross-repo work without a per-repo OK). **The top CTO-gated item.**
- 🔬 ~~**research-cli spin-out**~~ — **DONE** (PR #128): `alex-mextner/research-cli` created, `tools/research-cli/` removed from agent-tools. Awaiting CTO ✅.
- ⏳ **draw-cli pipx posture** — pipx packaging SHIPPED (draw-cli #9); whether pipx becomes the SOLE
  sanctioned install path (drop the symlink/dev mode) is the remaining call.

**(C) Creds / infra-gated — blocked on credentials or a real environment, not a decision:**
- ⛔ **review-qa Tier-2** — review-qa LIVE (real-Telegram MTProto + agent-browser web) + **review-cli #82**
  (window-screenshot visual diffing + more assertion types). All three Tier-1 harnesses (bot #68, web #70,
  ext #74) shipped; Tier-2 waits on live creds / a dedicated test account + screenshot infra + curated SUT
  repos. **The infra-gated item.**

**(D) Small / tracked — bounded follow-ups, just need a turn:**
- 🔬 **agent-tools #120** — require-ticket parser: value-consuming long flags matched by exact name (the
  flag-abbrev follow-up after #119/#111 closed the `-am` / value-flag bypasses). CLOSED
  (`gh issue view 120 --repo alex-mextner/agent-tools`).
- ⏳ **rig-cli #8 follow-through** — manifest + cron real; the board (review-cli #79), the classifier
  (task-cli #39) now READ the capability flags. Any remaining non-wired consumer is the residual.
- 📋 *Closed by the 06-26 batch (no longer remaining):* tg-cli #31 (CodeQL — all 8 fixed); task-cli #32
  (status-identity — review-cli… task-cli #40); review-cli #60/#61/#75 (qa `-m` forward / `--kind auto`
  bot-detection / ext-harness robustness — #81/#80/#83).

---

<details>
<summary>📜 <b>Session transcript — 2026-06-15 / 06-16 handoff state</b> (landed-work logs + superseded next-actions/in-flight/open-PR snapshots; click to expand)</summary>

## ✅ Done (this session, 2026-06-15)

### review-cli

- ✅ **Modes → first-class SUBCOMMANDS** (`review review` default, `brainstorm`, `just-ask`,
  `quorum`; mode-registry plugin-dirs; `--brainstorm/--quorum/--just-ask` flags removed; bare
  `review -C <repo>` still defaults to diff-review; `--visual` stays a composable flag) — PR #23 **merged**.
- ✅ README reframed: visual review surfaced, spec-web/dashboard de-headlined → "spec-review
  tooling", removed agentic-marketing phrasing, ralphex comparison kept with two SVG diagrams.
- ✅ Dashboard `--host` Tailscale exposure + **live SSE** activity stream — PR #21 merged.
- ✅ Board: agentic codex seat (GPT-5.5 = codex); Kimi-K2.7-Code on board; pool=4 default.
- ✅ Ecosystem doc migration: `~/.claude/CLAUDE.md` + `review install-skill` regenerated to subcommand syntax.

### tg-cli

- ✅ `tg replies [user|agent|all] [list|find]` — recall session messages (history.jsonl) — PR #24 merged.
- ✅ Inbound media download retry-with-backoff — PR #22 merged.
- ✅ README orthogonality: branding-follows-model (not opencode-specific), detection precedence,
  3-axis comparison — PR #23 merged.

### rig-cli

- ✅ `rig init` onboarding front door + `rig apply` reconcile + auto-mode harness provisioning
  (writes `permissions.defaultMode`) — PR #3 merged. Installed + verified (doctor green).
- ✅ Global layer applied machine-wide (47 skills → ~/.agents/skills, 7 agent-hooks, git-dispatcher, ship, mcp).

### agent-tools

- ✅ README reframed around rig front door + 4 pillars (autonomous/observable/controllable/safe).
- ✅ New agent-hook `block-raw-pr-merge` (forces `gh ship`, escape-hatch) — PR #8 merged.
- ✅ Shared-lib architecture **design doc** at `docs/specs/2026-06-15-shared-lib-architecture.md`
  (NOTE: its `lib/ts`/contracts-twinning part is SUPERSEDED — Python-only).
- ✅ **Auto-mode provisioned + made AUTOMATIC (self-dogfood)** — committed `rig.yaml`
  (`harness.auto_mode: true`) AND committed `.claude/settings.json` (`bypassPermissions`). Reversed an
  earlier wrong call: `.claude/settings.json` is Claude Code's SHARED/committed slot, so committing it is
  what makes auto-mode turn on by itself on a fresh checkout (gitignoring it was the reason it "didn't
  turn on" — CTO caught this). Only `.claude/settings.local.json` (the personal slot) stays gitignored.
  Matches the rig-cli reference. ⚠️ Safety caveat: the "safe because agent-hooks intercept" rationale is
  bridge-pending — CC-level guards are INERT until the #18 hook-bridge lands + is live-verified.
- ✅ **CI gates de-GHAS'd (PR #23 merged)** — the rig-shipped gates depended on PAID GitHub features and
  hard-failed on the hyper org/private repo. Dropped them for free OSS engines (CTO call, tg#3774):
  `secret-scan.yml` → gitleaks **OSS binary** (`gitleaks dir`; same engine+ruleset, no org license, vs the
  `gitleaks-action` that demands a paid `GITLEAKS_LICENSE`); `dependency-review.yml` → scripted
  `dep-audit.sh` (whole-tree bun/npm/pip/cargo/go audit, no GHAS, vs `dependency-review-action` that needs
  GHAS on private repos). READMEs corrected (the old "GHAS only for the dashboard" claim was wrong). Verified
  parity (tg#3777): secrets 1:1, vulns equal-or-better; **one gap = license-policy → #21**. CodeQL
  actions-suppressions in the leftover-grep/review-threads templates were already correct (untouched).

### hyper #463 (rig-rollout PR) — how it goes green

- 📋 The 3 red checks on #463 were all from the GHAS/licensed gates above; **PR #23 fixes them at the source**.
  #463 goes green when the **rig agent re-applies `rig apply`** (pulls the #23 templates + commits auto-mode in
  one pass) — owned by the parallel rig agent, NOT hand-edited on the #463 branch (two agents, one branch =
  collision). The CodeQL-actions red on #463 is a stale rollout copy → the re-apply replaces it.
- ⏳ ⚠️ **Separate REAL finding (pre-existing, not the rollout):** hyper's OWN `security-scan.yml` "Dependency
  Audit" flags a genuine high vuln — **esbuild `^0.25` (GHSA-gv7w-rqvm-qjhr**, RCE via Deno install path; not
  exploitable on Node/bun). Bump or waive with justification — tracked as **HYP-743** (Linear, not
  checkable via `gh`; kept ⏳ rather than guessed resolved).

### ➡️ DELEGATED to the rig agent — finish the hyper rollout (CTO tg#3783/#3784)

The parallel rig agent owns completing hyper's rollout; this session hands it off:

1. **`rig apply` re-run on hyper** → pulls the #23 templates (de-GHAS'd gates) + commits auto-mode → greens #463
   (replaces the stale CodeQL-actions copy too).
2. **Remove hyper's bespoke `security-scan.yml`** (CTO: common gates belong in agent-tools, not per-repo).
   Coverage parity BEFORE deleting — its three jobs: **Semgrep** ✅ covered by agent-tools `ci/sast/`;
   **bun audit** ✅ covered by `ci/dependency-review/dep-audit.sh` (#23); **Trivy** ❌ **NO agent-tools
   equivalent yet** → either add a `ci/trivy/` (or fs-scan) gate to agent-tools and provision via rig, or
   keep hyper's Trivy step. Don't silently drop Trivy. Tracked: **agent-tools #24**.
3. **esbuild HYP-743** must be bumped/waived for any whole-tree dep-audit (rig's or hyper's) to pass green.

### ecosystem hygiene

- ✅ ralphex/quorex removed everywhere + `alex-mextner/quorex` **archived** with a "superseded by
  review-cli" banner; 3d-cli docs purged; hyper-saas AGENTS.md ralphex workflow removed (PR #461 merged).
- ✅ hyperide.ai homepage URL cleared on all 6 tool repos.

---

## ✅ Done (2026-06-16) — auto-mode SAFETY BLOCKER CLOSED + 17 PRs landed

*(The detail below lists the first 9; the §3 lib stack #30/#33/#17, this roadmap #40, rig-cli #18, and the
3 cross-repo doc PRs (review #27, draw #4, 3d #7) landed later the same session — see "Next actions".)*

### 🎯 Auto-mode guard — RESOLVED (the central "bridge-pending safety" blocker is CLOSED)

- 🔬 **#20 hook-bridge MERGED** (`d4ff40f`) + hardened (timeout_ms null/bool/0/neg; non-UTF-8 decode
  pinned `errors=replace`/`encoding=utf-8`; NotebookEdit path normalized so pre-write guards scope it).
- 🔬 **`rig apply` wired `cc_hook_bridge` into `~/.claude/settings.json`** (3 dispatcher hooks:
  PreToolUse×2 + Stop). **Live-proven THIS machine:** a blockable cmd → `permissionDecision: deny`,
  benign → allow. So `defaultMode: auto` is now GUARDED — the installed agent-hooks (block-secrets-write,
  enforce-timeout-on-bash, require-review-before-commit, block-no-verify, block-raw-pr-merge, …) actually
  FIRE under auto-mode. The roadmap's "safe pillar is bridge-pending" caveat is now satisfied.
- 📋 Provisioned via rig (dogfood), not hand-edits.

### Merged this session (gh ship, gated — green CI + resolved codex threads)

- 🔬 **agent-tools #38** — NEW `ci/tests` slot (pytest via uv, secretless) + rig provisions it. agent-tools had
  12 governance CI slots but none that RAN the repo's tests → `gh ship` refused no-CI PRs. This unblocked the
  whole backlog. Keystone.
- 🔬 **agent-tools #29** — `lib/agenttools_retry` (§3 "retry"). **agent-tools #32** — mcp-policy (review is a
  CLI+skill, NOT an MCP; removed the dead `mcp/review` slot + repointed 6 dangling refs). **agent-tools #34**
  — third-party tool triage (#19). **agent-tools #35** — strict-ticket-discipline skill + require-ticket
  hook (task-cli integration piece; fixed 2 real hook bugs: honor `git -C`, don't exempt on flags in the msg).
- 🔬 **rig-cli #21** — NEW `rig stats show`: tool-adoption analytics across agent harnesses (sources/aggregate/
  render pipeline, json/tui/web). Fixed 2 P2 (parse_iso non-string; inverted --since/--until).
- 🔬 **review-cli #28 + #29** — spec-web phase-1 (mobile UX) + phase-2 (submit→agent handoff, debounced disk
  drafts, `review spec-web reply` → UI+tg). §7 review-cli follow-ups. (Caught a CI regression the building
  agent's subset-test run missed — `--spec` in `_VALUE_TAKING_OPTS` broke a guard test.)

### Durable / infra

- 📋 Global `~/.gitignore` now ignores `.serena/` + `.claude/worktrees/` (machine artifacts were tripping
  `gh ship`'s clean-worktree check — not a ship bug; ship works fine from clean worktrees, verified).
- 📋 `gh ship` (ci/ship/ship.sh) confirmed repo-agnostic: ran it against rig-cli/review-cli via `bash
  <agent-tools>/ci/ship/ship.sh <PR>` from each repo's worktree.

### Still-open in §3 (Shared lib extraction) — handed off with documented threads

- 🔬 **#30 lib-config** (§3 "config") — rebased onto main, conflict resolved, 30 tests pass (with pyyaml). **2
  open P2:** (1) `core.py` "global-only" mode still loads the repo layer (no suppress) — needs an API knob +
  test; (2) ⚠️ **SYSTEMIC:** umbrella `lib/pyproject.toml` builds only `agenttools_log`
  (`include=["agenttools_log*"]`), so NO new lib module installs — fix ONCE for all (`#29 lib-retry` already
  merged with this latent gap).
- 🔬 **#33 lib-advertise** (§3 "advertise") + **#17 format-hook** — CONFLICTING; need rebase (lib/README.md row
  conflict, same as #30) + thread resolution.
- ⏳ **Uncommitted lib modules** in `.claude/worktrees/wf_9d942e4d-*`: `tmux_inject` (§3 "tmux-inject"),
  `daemon` (§3 "daemon-supervisor"), `registry` (§3 "registry"); `gantt`/`providers` committed → need push+PR.

### New tickets opened (add to ledger)

- 🔬 **agent-tools #39** — surface advisory (exit-0) v1 hook messages as CC `additionalContext` (bridge follow-up).
- 🔬 **review-cli #30** — spec-web draft autosave race → server-side ordering token/tombstone.

## 🎯 Next actions — audit-reconciled 2026-06-16 (ordered; drives the loop)

*Live cross-repo audit of all 7 tool repos (roadmap-state-audit workflow). Re-audit (or `gh pr view`)
before acting — items 1–4 + 6's #18 LANDED later in this same 2026-06-16 session.*

1. ✅ **DONE — Easy wins:** review-cli **#27**, draw-cli **#4**, 3d-cli **#7** MERGED; review-cli **#26**
   closed. PENDING: tg-cli **#33** (no local checkout — clone tg-cli first), 3d-cli **#1** (blocked on a
   required check — re-check).
2. ✅ **DONE — Cleanup:** agent-tools **#18** closed (bridge done, pointed at `d4ff40f`); rig-cli **#12**
   was already merged.
3. ✅ **INVESTIGATED — not a hard blocker:** the lib modules DO install via their NESTED pyproject
   (`uv --with .../lib/agenttools_config` — verified). The umbrella `lib/pyproject.toml` is just
   agenttools_log's own dist; the real fix is per-module README install paths (done in #30). No packaging
   refactor needed. (Optional polish: clarify the misleadingly-named root pyproject.)
4. ✅ **DONE (PRs) — §3 lib stack:** **#30** lib-config, **#33** lib-advertise, **#17** format-hook ALL
   MERGED (each had REAL codex P2s — global-only, symlink-write-through, ruff-in-Black-repo — fixed+tested).
   ⏳ STILL TODO: **proper commit+push+PR** the 3 uncommitted modules (tmux_inject/daemon/registry) — they are
   **backed up** at `~/xp/agent-tools-lib-modules-backup-2026-06-16/` (loss risk gone) but NOT yet in git;
   plus PR the committed gantt/providers. Do BEFORE any `git worktree prune` of `wf_9d942e4d-*`.
5. **tg-cli (S):** ship #34 (autolink → tg#28) and #35 (defer-while-waiting → tg#30) after clearing 1 thread each. (No local tg-cli checkout — clone first.)
6. **rig-cli:** ✅ **#18** (readme) MERGED. ⏳ **#20** (`rig config get|set`, §5 piece) — has a real P2 (validate
   the GLOBAL layer in isolation so a repo overlay can't mask a bad `--global` write; fix verified-viable: an
   empty-cwd `_load_plan` sees only the global layer) + a `docs/gen_svgs.py` rebase conflict to resolve.
7. **rig-cli §5 (L):** repo-settings provisioning via `gh api` + `agent-browser` (current branch
   `roadmap-rig-repo-settings`). Guard with capability detection (org/private repos can hard-fail on
   ruleset/GHAS endpoints — same class that forced the GHAS→OSS retreat); default-on only where the API permits.

## 🚧 In-flight at handoff (background agents — verify state, resume if incomplete)

> ⚠️ **SUPERSEDED (2026-06-16):** every "hook-bridge / auto-mode-guard is bridge-pending / live-verify
> pending / land #20 / #18 is a hard blocker" item in THIS section and below is **DONE** — see
> "✅ Done (2026-06-16)" above. #20 is merged (`d4ff40f`), `cc_hook_bridge` is wired into
> `~/.claude/settings.json` and **live-proven** (deny/allow). Do NOT re-verify or re-merge the bridge.
> The `.claude/scripts/pr-ship.sh` shim is present and CI exists (#36/#37/#38). Follow the "🎯 Next
> actions" block. The ONLY still-open auto-mode item is the **hyper product repo** rollout (below) —
> a different repo, not the bridge.

### ➡️ rig-tooling session (2026-06-15) — RESUME HERE

- 🔬 **hook-bridge #18 LIVE-CC VERIFIED** ✅ — fresh `claude -p` under `bypassPermissions`: a raw
  `gh pr merge` → CC `permissionDecision: deny` (bridge → block-raw-pr-merge), a benign cmd → pass.
  The "live-CC round-trip" the bridge section flags as pending is **done**. #20's conflict with main is
  resolved + pushed (branch `cc-hooks-bridge`). **Land #20 then #12.**
- 📋 **Auto-mode finding: `auto` > `bypassPermissions`, and `auto` is USER-level ONLY** — CC silently ignores
  `defaultMode:auto` in a repo's project/local `.claude/settings.json` (since v2.1.142; confirmed empirically
  on 2.1.177), honoring it only from `~/.claude/settings.json`. `auto` (research preview) auto-approves WITH a
  safety classifier; bypass skips everything. So the committed-per-repo bypass rollout is the inferior model
  AND project-bypass overrides user-auto. Redesign: `docs/specs/2026-06-15-harness-layer-redesign.md` (PR #25)
  — CC → user-level `auto`, migrate the 6 tool repos off committed project bypass, per-harness branch-on-kind.
- 🔬 **Open PRs:** **#25** (harness-redesign spec), **#26** (this rig-config-UX capture: config get/set, schema,
  symlinks, repo-settings), **#20** + **rig-cli #12** (the verified bridge). agent-tools `main` is now
  branch-protected → PRs only (the `gh ship` script `.claude/scripts/pr-ship.sh` was missing here — fix it).
  #25/#26 both CONFIRMED MERGED via `gh pr view`.
- ⏳ **Pending impl:** delete `rig setup` alias (CTO); implement the #26-captured rig features; verify tg
  `PermissionRequest` passthrough fires in `auto` mode (tg-ctl already installs the hook). No PR/issue
  reference to verify against; `rig setup` is a real interactive wizard elsewhere in this file's later
  sections, but whether that traces back to closing THIS specific pending-impl note is unclear.
- ⏳ **⚠️ `/Users/ultra/work/hyper-canvas-draft` auto-mode STILL DOES NOT WORK (CTO confirmed 2026-06-15).**
  Diagnosed: its committed `.claude/settings.json` + `.claude/settings.local.json` set **no `defaultMode` at
  all** (the huge `permissions.allow` list is auto-accumulated "always allow", not auto-mode). And `auto`
  can't be project-committed (user-level only), so the real fix is **user-level `auto`** (or the rig harness
  migration), provisioned by **rig** — NOT a hand-edit (a manual settings.local.json edit was made then
  reverted per "fix tooling, not manual"). Close it via the rig harness redesign + the hyper rollout
  (delegated to the rig agent). It's a TEAM product repo → user-level auto for Alex, don't commit bypass to
  the org repo.
- 📋 task-cli foundation (agent a9cef4ca) — building the Python tool in the new `alex-mextner/task-cli`
  repo per `task-cli/docs/2026-06-15-task-cli-spec.md`. Check the repo for the pushed branch/PR.
- 📋 **model-currency** (agent a48e44ab) — building `lib/contracts/models.yaml` (→ relocate to a
  Python `lib/` path, no contracts dir needed) + `lib/checker/model_freshness.py` + rig daily-noon
  cron provisioning. Check agent-tools + rig-cli for its PRs.
- 📋 **CI-gate resilience** — DONE: agent-tools **PR #9** (gate templates degrade gracefully); wave-1
  rollout PRs patched green except tg-cli #25 CodeQL.

## 📋 Open PRs ready to land

- 📋 agent-tools **#9** — CI gate resilience — **MERGED**.
- 📋 rollout: draw-cli **#3** — **MERGED**; 3d-cli **#6** — **MERGED**; rig-cli **#4** (model-cron
  schedule) — **MERGED** (gitleaks GITHUB_TOKEN fix + 2 codex review findings resolved).
- 📋 **tg-cli #25** (rig rollout) — **MERGED** with a documented justification: it is infra-only
  (CI workflows + `ci/` companions + AGENTS.md + rig.yaml), touches no `features/**` source, and the
  CodeQL JS/TS self-gate's only red was **8 pre-existing findings** in untouched `features/**` code
  (the gate firing on legacy code, not a regression). Those are tracked for a real fix-or-justify at
  alex-mextner/tg-cli#31 — NOT mass-suppressed; the self-gate goes green on main once #31 lands.

---

</details>

## 🗺️ Remaining work (prioritized)

### review qa — agent-as-tester mode for review-cli

Spec SHIPPED (review-cli `docs/specs/review-qa.md`, PR #52, 6-agent workflow design + adversarial critique). Implementation NOT started — the spec's §11 / its own "open issues — resolve before building" list are blocking for an implementer. Phased build:

- 🔬 review (review-cli PR #55) — **Ship the mode skeleton + suites gate first** because it is the smallest verifiable slice and proves the registry wiring: new `reviewlib/modes/qa.py` (`MODE = ModeSpec(name="qa", diff_policy="none", aliases=("test",))`), one import + one entry in `reviewlib/modes/registry.py:34`, no `cli.py` dispatch surgery. Add `EXIT_QA_NO_SUITES=5` (next free after the verified 3/4) and the no-suites 3-part WHAT/WHY/HOW gate (mirror `_fail_not_a_repo`, `cli.py:104`); a green run with no authored cases is a lie.
- 🔬 review (review-cli PR #62) — **Add the write/exec executor next** because qa is the FIRST mode needing a non-read-only agent — it must NOT ride `run_panel`/the board (codex `-s read-only` at `backends.py:74`, opencode deny-all at `backends.py:233`). New `reviewlib/qa/executor.py` spawns ONE backend (claude default; codex `-s workspace-write --full-auto` alternate; opencode out of v1) in an isolated `git worktree add` of the SUT (`--in-place` escape hatch), single-seat. Comment the deliberate exception next to `_READONLY_AGENT_DENIED_PERMISSIONS`; carve out the long timeout (qa is `panel_mode` at `cli.py:1579` → would get the short `PANEL_TIMEOUT_DEFAULT`).
- 🔮 building (Phase 3, this turn) — **Backend SUT against an existing stage before any harness** because it needs zero new repos: deterministic `reviewlib/qa/env.py` (stage-detect → reuse-if-reachable → never-tear-down-what-you-didn't-bring-up; else `EXIT_QA_NO_ENV=6`), then compose bring-up + health gate (`EXIT_QA_ENV_UNHEALTHY=7`) reusing the `ext-test-projects/e2e/docker-compose.e2e.yml` `-p`-namespaced pattern, guaranteed teardown via `reviewlib/backstop.py`.
- 🔬 **web/ext Playwright harness is a SEPARATE prerequisite** because rig cannot clone external repos (`riglib/catalog.py:48` scans only skills/agent_hooks/git_hooks/ci/mcp): create `alex-mextner/vscode-playwright` by selective copy from private `hyperide/hyper-ext-e2e` `e2e/` (generalize `launchVSCode`, drop HyperIDE specifics — needs human curation), ship it as an agent-tools skill via its own `install.sh`. Drive ext via `launchVSCode()`+CDP `window.screenshot` (never `screencapture`), web via the `agent-browser` skill.
- 📋 **bot harness last, Tier-1 hermetic only for v1** because real-Telegram driving needs unprovisioned credentials: a mock Bot-API server the SUT polls via `TG_API_BASE` (verified honored by `tg-ctl:2169`). PREREQUISITE filed against tg-cli: make the `tg` OUTBOUND sender honor `TG_API_BASE` (currently hardcoded at `tg:610`) so captured `sendMessage` works — **🔬 review (tg-cli PR #50): this prereq is now MERGED** (confirmed via `gh`). Tier 2 (Telethon MTProto + agent-browser Telegram Web, dedicated test account, fail-closed on real `TG_CHAT_ID`) deferred until credentials exist.
- 📋 See docs/specs/review-qa.md §11 for the open CTO decisions (tester backend, isolation default, single-seat, seeding subset, rig Option A vs B, suite format, bot credentials, k3s).

### 1. task-cli (after foundation lands)

- 🔬 review (task-cli PR #11): v1 verbs (`task new` / `task done`) MERGED, usable against GitHub
  Issues. 🔮 building this turn: rollout, `require-ticket-before-commit` enforcement via rig, a
  Docker real-agent CI test, and #10 (close-path source-state-transition validation, OPEN issue).
- 🔬 **Phase 1** (foundation, in-flight): Python CLI — backends (GitHub Issues default, Linear per-repo)
  call the provider **API directly** with creds harvested from `gh`/`linear` configs; classification
  `change|justAsk` via `review just-ask` per-provider fallback chain (haiku head); enforcement gates
  (acceptance criteria / motivation / user-impact / cost-of-inaction / screenshots / formatting, each
  with `--skip-<gate> "<reason>"` escape hatch); `list` defaults to this-session tickets.
- 🔮 **Phase 2** (#3695): task **dependency system** + **Gantt** rendering (alex-mextner/task-cli#1); a
  **daemon service** (like tg-ctl: first call brings it up, survives restarts/kills, subscribes to
  gh-issues/Linear **webhooks**, **adapter-based** for future trackers) (alex-mextner/task-cli#2); on
  completion → inject "done, X unblocked" into the agent's tmux pane (via shared-lib tmux-inject);
  **due-date** reminders to the agent (alex-mextner/task-cli#3); task-cli self-hook.
- 🔮 **Integrations** (alex-mextner/task-cli#4): tg-cli classify inbound hook (#3645); agent-tools
  `strict-ticket-discipline` skill + `require-ticket-before-commit` guard (+ optional
  `ci/ticket-required`) — 🔮 the `require-ticket` guard + rig wiring is building THIS turn (a
  separate agent owns it); rig provisioning (installs linear-cli when backend=linear, for auth only).
  Needs a NEW `on-inbound` hook point in `agents-hooks/v1`.

### 2. tg-cli `/tasks` (after task-cli)

- ⏳ `/tasks` → table + Gantt + agent summary for the session's tasks; show external-to-session deps;
  diagram sent **HD inline** (not as a file, uncompressed-quality). (alex-mextner/tg-cli#26)

### 3. Shared lib extraction (Python-only) — `agent-tools/lib/<module>`

Tracking issue: alex-mextner/agent-tools#12. Per the design doc; phased, lowest-churn first. Each
tool migrates to import from the lib.

1. **`advertise`** (install-skill — 4 copies today) — first extraction, stands up the skeleton.
2. **`hooks`** (`agents-hooks/v1` dispatcher; tg vendored it — de-dupe).
3. **`providers`** (the biggest asset: board, failover, transports, `oc:` routing, key cascade,
   **capability tags incl. vision-filter for `--visual`**, **model-currency manifest**). Consumed by
   review, task-cli classifier, research-cli.
4. **`config`** (cascade + env precedence + `~/.config` paths).
5. **`registry` + trust-kernel** (the hook-trust + visual-module-registry trust algorithm, one place).
6. **`retry`** (net-new), **`tmux-inject`** (extract from tg-ctl), **daemon-supervisor**
   (survives kills — shared by task-cli + tg-ctl), **Gantt-render**, `agenttools_log` (exists).

### 4. model-currency (in-flight → finish)

- 🔮 `models.yaml` manifest: per-provider current model + **capability tags** (vision/code/reasoning) +
  role aliases (role `vision` resolves only to vision-capable). Daily-noon **cron checker** that polls
  provider `/models` and opens a bump PR (semi-auto). rig provisions the schedule (launchd/cron),
  check-and-install-if-missing at init/apply. (alex-mextner/rig-cli#8 — rig cron provisioning landed
  in PR #4, merged.) — 🔬 review (agent-tools PR #90): the model-freshness CLI now adopts the shared
  `agenttools_errors` layer (#89).

### 5. rig

- 🔬 **rig provisions REPOSITORY SETTINGS at init/apply** (config-driven, sensible defaults ON) (#3696/#85,
  CTO 2026-06-15): rig.yaml declares the repo's GitHub settings and `rig init`/`rig apply` reconciles them,
  the same way it does skills/hooks/CI. **Two backends, auto-selected per setting:**
  - 🔬 **`gh api`** for everything the GitHub API exposes: **branch protection / rulesets** (require-PR,
    required status checks + reviews, linear history, block force-push — the `…/settings/rules/` the CTO
    just enabled by hand), **GHAS** (Dependency Graph + vuln-alerts + secret-scanning + code-scanning),
    merge-button policy (squash-only, auto-delete head branch, auto-merge), Actions permissions, etc.
  - ⏳ **`agent-browser`** for settings the API does NOT expose — drive the GitHub settings UI headlessly to
    flip the switches `gh api` can't reach. A first-class rig backend invoked INSIDE init/apply, not a
    manual step.
  Everything maps onto rig.yaml (a `repo:`/`github:` block) with the **secure/sensible set enabled by
  default**, so a fresh `rig init` lands the guardrails (branch protection + GHAS + squash-merge + ship gate)
  with zero hand-toggling. `rig status` reports drift on these like any other managed artifact. (Supersedes
  the narrow "gh api enable Dependency Graph" scope.) (alex-mextner/rig-cli#5)
- 🔬 **`rig config get|set [--global] <path> [val]` — the recommended way to change config** (CTO 2026-06-15):
  read/write a single nested key in `rig.yaml` (or `~/.config/rig/config.yaml` with `--global`) WITHOUT
  hand-editing YAML, and **reconcile immediately** — `set` runs `rig apply` so the change takes effect on the
  spot. `<path>` is dot-notation into the YAML tree (`harness.mode`, `ci.items.secret-scan.tier`,
  `github.branch_protection.required_reviews`). The tooling-driven config-change UX: agents and the CTO change
  rig config through `rig config set`, never by hand-patching the file. (Pairs with the repo-settings
  provisioning above + the harness-layer redesign + the "fix tooling, not manual" rule.)
- 🔬 **rig.yaml has a real, ENFORCED JSON schema + config docs in the rig repo** (CTO 2026-06-15): ship a JSON
  Schema for `rig.yaml` + the global config and actually VALIDATE against it (not just prose) — `rig apply` /
  `rig config set` reject an unknown key or bad value with a clear error + the schema path, and editors get
  completion/validation. The human-readable config reference (`docs/config-schema.md`, already cited from the
  rig.yaml header) must EXIST in rig-cli and stay in sync with the schema (one source of truth, every key
  documented). "It should work" = a malformed config fails loudly, not silently.
- 🔬 **rig always provisions `AGENTS.md` + `CLAUDE.md` as symlinks** (CTO 2026-06-15): a repo should carry BOTH
  files so every harness finds its expected name (codex/opencode/gemini read `AGENTS.md`, Claude Code reads
  `CLAUDE.md`), pointing at ONE source of truth via a symlink (e.g. `CLAUDE.md` → `AGENTS.md`) so the two can
  never drift. `rig init`/`rig apply` create/repair the symlink (never clobber a real file without backup);
  `rig status` flags a missing or broken link.
- 🔬 **Auto-mode is COMMITTED** (decision REVERSED 2026-06-15, CTO): `.claude/settings.json` is Claude
  Code's SHARED/committed slot — committing it (`defaultMode: bypassPermissions`) is what makes auto-mode
  turn on by itself on every checkout. The personal per-machine slot is `.claude/settings.local.json`
  (that one stays gitignored). rig-cli#6 ("gitignore settings.json") is REVERSED/wontfix — the opposite is
  correct. ⚠️ The footgun every reviewer flagged: committed `bypassPermissions` auto-accepts every tool
  call on any LOCAL CLI checkout (web sessions ignore it), and the agent-hooks that would gate it are
  INERT in CC until the #18 bridge lands + is live-verified. So the "safe" pillar is bridge-pending —
  #18 is now a HARD blocker for blessing auto-mode, not a nicety. (alex-mextner/rig-cli#6 — reverse it.)
- 🔮 **Rollout (#3686)**: wave-1 tool repos = PRs open (draw/3d green, tg pending TS fix). **Wave-2 =
  bots** (ExpenseSyncBot, garage-band, summary-bot, esphome-ir, claude-p, diploma, sme-archiving-gc,
  talks, upwork) + review-cli/rig-cli/agent-tools themselves. Each: `rig init --yes` + conservative
  AGENTS/CLAUDE slim (drop now-self-advertised generic rules, keep project specifics) + harvest report.
  (alex-mextner/rig-cli#7)
  - 🔬 **⚠️ CORRECTED (2026-06-25) — auto-mode is USER-LEVEL `auto`, NOT a committed `.claude/settings.json`.**
    The earlier morning-of-2026-06-15 note here ("all six Python tool repos now COMMIT
    `.claude/settings.json` / `bypassPermissions`") was **REVERSED the same evening** by **rig-cli #13**
    ("Claude Code auto-mode as user-level `auto`, not project bypass", merged 2026-06-15) — see also
    rig-cli #6, closed this session. The mechanism is: `rig` writes `permissions.defaultMode: auto` into the
    **user-level** `~/.claude/settings.json`, and tool repos do **NOT** commit a project `.claude/settings.json`
    (a committed project `bypassPermissions` was the wrong call). Verified: `git ls-files .claude/settings.json`
    in this repo returns NOTHING — agent-tools does not commit it. Treat the old per-repo ✅ table above as
    history, not current state. (Separate, still-relevant: the rig.yaml `mcp.review.command: "review --mcp"`
    was STALE — `--mcp` was dropped in the review subcommand refactor — and was removed via rig-cli #60.)

### 5b. rig manages tmux configuration (CTO 2026-06-16)

`.tmux.conf` + plugin setup (tpm/resurrect/continuum) become rig-managed artifacts, reconciled like
skills/hooks/CI — not hand-edited dotfiles that drift. Requirements surfaced while fixing a stale-session
reboot (continuum's last save was 3 weeks old):

- 🔬 **Apply mechanism = import-preferred, managed-block fallback (CTO 2026-06-16).** rig must MIGRATE an
  existing hand-written `~/.tmux.conf`, not clobber it. Preferred: rig owns a generated file (e.g.
  `~/.config/rig/tmux/rig.tmux.conf`) built declaratively from rig.yaml, and `~/.tmux.conf` carries a
  single `source-file <that path>` import; rig rewrites its own file wholesale on every `rig apply`
  (idempotent) and never touches the user's hand-written lines. Fallback (when an import is undesirable):
  splice a managed block between sentinel markers (`# === rig-managed (tmux) BEGIN ===` / `… END ===`)
  and replace ONLY between the markers (conda-init style). First apply: detect rig-owned settings already
  inline (resurrect/continuum/Moshi/etc.), lift them into the import/block, back up the original
  (`~/.tmux.conf.rig-bak`), leave user-specific lines intact. `rig status` reports drift on the managed
  region only. Because rig generates the managed region, it can GUARANTEE ordering (continuum's
  status-right hook last — the root cause of the stale-session bug) instead of relying on hand-ordering.
- 🔬 **Moshi-specific tmux tweaks must be SEPARATELY toggled (opt-in), not unconditional.** Current bug:
  `set -g status-right ''` under `$MOSHI_CLIENT` wipes tmux-continuum's autosave timer (it lives in
  `status-right`) → continuum silently stops saving → a reboot restores a weeks-stale session. Moshi
  tweaks must gate behind an explicit enable and must never break continuum/resurrect (use a Moshi-safe
  status layout that keeps continuum's hook, or move continuum off status-right).
- 🔬 **resurrect must restart Claude Code per-window with the right session id.** `@resurrect-processes`
  has no `claude` today, so cc is not restored at all after a reboot. Need `claude` in the list PLUS a
  save/restore hook that records each window's cwd→cc session id and relaunches `claude --continue`
  (most-recent in cwd) or `claude --resume <id>` (exact) after restore.
- 🔬 **tmux must auto-come-up after a machine reboot** — verify continuum-boot / a launchd agent actually
  fires (currently `@continuum-boot on` + `boot-options iterm`, unverified), don't assume.
- 🔬 **reconnect must attach to the ONE session, not spawn a new one (anti-sprawl).** Observed: a Moshi/iTerm
  reconnect created a duplicate session ("3" alongside the working "2", same `ext` window) instead of
  re-attaching — the user reads this as "tmux crashed again". The login/reconnect entry must do
  attach-or-create (`tmux attach -t main || tmux new -s main`), single canonical session. Note there are
  TWO tmux wrappers in play (`tmux` with `.tmux.conf` + `ln` with `.ln.conf`) — reconcile/clarify which is
  the canonical one so they don't fight.

### 6. research-cli (after lib `providers`)

- 🔬 Separate Python CLI on the shared lib's panel engine (NOT a review mode). Reuses providers verbatim.
  (alex-mextner/agent-tools#13) — 🔬 review (agent-tools PR #85): research-cli adopts the shared
  `agenttools_errors` layer (what/why/how-to-fix + structured exit codes). 🔮 the rest of research-cli
  (real transport / spin-out) is queued, building.
- ✅ **MVP + SPUN OUT** — a distinct tool (NOT a review mode) that reuses the merged
  `agenttools_providers` CORE (#49) verbatim: registry + `resolve_role`, the failover `Board` /
  pool-reserve split, the key cascade, capability tags, the shared `models.yaml` manifest. Adds the half
  the CORE defers — a `Transport` (reachability via the key cascade + a live call, behind a protocol so the
  engine is network-free in tests) + the single-round panel pass + deterministic synthesis.
  Self-registering commands (`ask`, `board`, `install-skill`), structured exit codes, deterministic tests,
  README. **Now lives in its own repo: [`alex-mextner/research-cli`](https://github.com/alex-mextner/research-cli)**
  — spun out via **strategy B (vendor + drift-guard)**: it vendors `agenttools_providers` +
  `agenttools_errors` + `models.yaml` under `vendor/` with a pinned-SHA drift-guard
  (`scripts/resync_vendored_libs.py --check` + a scheduled CI job against this repo's canonical), the proven
  task-cli pattern. The `tools/research-cli/` scaffold was removed from this umbrella (#125). **Strategy A**
  (publish `agenttools-providers`/`agenttools-errors` to PyPI and depend on them) is deferred — needs the
  CTO's PyPI account; when provisioned, the vendored copies swap for declared deps. **Phased rest** (tracked
  in the new repo): a real transport (`oc:` router / api|cli mode / parsing), multi-round follow-ups,
  adversarial cross-examination, citation verification, model-driven synthesis.

### 7. review-cli follow-ups

- 🔬 #75: make ALL board models agentic via opencode (re-investigate commandcode + GLM custom provider).
  (alex-mextner/review-cli#24 — CLOSED, verified via `gh`; corrects the ⏳ this got in the first backfill
  pass, reconciled against the ledger's 🔬 for the same issue).
- 🔬 Fix flat `DEFAULT_MODELS` stale `kimi-k2p6-turbo` via Fireworks (dead `glide` account) → manifest.
  (alex-mextner/review-cli#25 — CLOSED, verified via `gh`; same reconciliation as #24 above.)
- 🔬 Propagate canonical ecosystem one-liner to other repos' ecosystem tables (after their rollout PRs):
  *"multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual
  review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic."*
  (alex-mextner/review-cli#26)

### 8. agent-tools harvest (one centralized pass, from rollout reports)

Tracking issue: alex-mextner/agent-tools#14.

- 📋 Candidate skills (Sources: 3d-cli AGENTS):
  - ✅ **worktree-via-project-cli** (dep provisioning, distinct from worktree-base-trap) — authored
    (`skills/universal/`), GREEN-verified per writing-skills.
  - ✅ **queued-report-durability** (channel-unavailable → don't fake delivery) — authored, GREEN-verified.
  - ⏭️ **subagent-delegation contract** — SKIPPED: already covered by `subagent-handoff-contract` (dupe).
  - ⏭️ **"diagnostic image ≠ proof" acceptance bar** — SKIPPED: overlaps `visual-proof-cycle`; fold a
    proof-claim acceptance-bar note into that skill rather than ship a near-duplicate.
- ⏳ `strict-ticket-discipline` skill + `require-ticket-before-commit` guard (with task-cli).

### 9. Misc / cleanup

- ⏳ **PREREQUISITE for ALL AGENTS.md/CLAUDE.md slimming — skills must actually LOAD** (tg#3736/3740,
  rig-cli#9): the harness discovers Skill-tool skills from `~/.claude/skills/` (symlinks → ~/.agents/skills),
  NOT from ~/.agents/skills directly. rig installed to ~/.agents/skills but never symlinked into
  ~/.claude/skills → rig-installed universal skills were NOT loaded → "self-advertised via skill" was
  FALSE. **✅ DURABLE FIX LANDED (2026-06-15): rig-cli PR #11 (`link_skill_harness`) MERGED** — `rig apply`
  idempotently symlinks each enabled skill into the harness discovery dir (claude-code → ~/.claude/skills),
  harness-keyed via `harness.kind`, never clobbers real non-symlink dirs, `rig status` reports link drift.
  **Verified post-merge: 56 entries in ~/.claude/skills, 53 symlinks all resolve (0 broken SKILL.md), 3
  real hand-authored dirs untouched.** So the **cc** harness is DONE + reproducible for any machine.
  **STILL OPEN — must work for ALL supported harnesses** (tg#3747), each with its own discovery mechanism:
  cc → `~/.claude/skills` (dir-skills); codex → `~/.codex/AGENTS.md`; opencode (oc) → `~/.config/opencode/AGENTS.md`;
  gemini → `~/.gemini/GEMINI.md`; commandcode (cmd) → has its own agent CLI (CONFIRMED by CTO) — provision its global instruction dir;
  pi → **earendil-works/pi** (github.com/earendil-works/pi, the minimal extensible coding-agent harness; loads AGENTS.md, multi-provider) → AGENTS.md-style provisioning like codex/oc (confirm its global dir at impl). The **supported-harness list must be in the README** (agent-tools + rig).
- 🔬 **⚠️ agent-hooks don't FIRE in Claude Code — need an agents-hooks/v1 → CC bridge** (agent-tools#18): CC runs settings.json hooks (PreToolUse/PostToolUse), NOT the ~/.claude/hooks/*.json `agents-hooks/v1` descriptors rig installs → block-raw-pr-merge / block-secrets / format-on-write / etc. are ALL INERT in CC (files nothing invokes). The 'safe because guards intercept' pillar is FALSE until a bridge runner is wired into settings.json (rig-installed, harness-keyed) + verified by the clean-room e2e. Same class as the skill-loading gap. DO NOT claim any guard 'works' in CC until this lands.
  **✅ BUILT (2026-06-15) — NOT merged:** **agent-tools PR #20** (dispatcher `lib/cc_hook_bridge`: reads CC's
  tool-call JSON, maps `(event,tool)`→v1 point, runs the `~/.claude/hooks/*.json` descriptors, translates
  exit-10 BLOCK → CC's `permissionDecision: deny` on exit 0 — contract confirmed against installed CC 2.1.177)
  - 🔬 **rig-cli PR #12** (`register_hook_bridge` wires it into settings.json PreToolUse/PostToolUse on `rig apply`).
  Clean-room test PROVES it: raw `gh pr merge` → DENY, `gh ship` → pass.
  **➡️ NEXT ACTION (DO THIS before merging): live-CC round-trip verification.** The clean-room proof drives the
  dispatcher exactly as CC invokes it, but it was NOT run inside a live CC session. To close the loop: in a
  real CC session run `rig apply` (so `register_hook_bridge` writes the settings.json hooks), then trigger a
  guarded action (a raw `gh pr merge`) and confirm **CC itself refuses the tool call**; confirm a benign
  action passes. THEN merge **#20 first, then #12** (interdependent). Until that live round-trip passes, the
  "safe because guards intercept" pillar stays UNPROVEN — do not claim any guard "works" in CC, do not merge.
- ⏳ **Clean-room / Docker e2e for rig** (tg#3745, rig-cli#10): the manual symlink fixed THIS machine; only a
  fresh-environment e2e (Docker container or throwaway `$HOME`) running `rig init` as a brand-new user
  proves it works for ANYONE on ANY machine — assert skills discoverable by the harness (~/.claude/skills
  resolve + in the Skill-tool list), hooks/dispatcher/CI/auto-mode installed, idempotent re-apply,
  `rig status` clean. (Current rig tests use a tmp-$HOME unit sandbox; this is the full clean-room integration.)
- ⏳ **Slim `~/.claude/CLAUDE.md` to PERSONAL-ONLY** (#3704): it should hold only user-specific content
  (identity / "address me as Alex" / preferences / dictionary / Telegram report style). ALL universal
  tool + process docs (how to use `review`/`tg`/`draw`, the no-short-timeout rule, etc.) must
  self-advertise via SKILLS, not be hand-written there — many already have skills + the `<!-- skill:X -->`
  blurbs are the self-advertising mechanism. The hand-written "## Review and sanity checks" section
  duplicates the `review` skill → remove it; audit the rest the same way. (My in-place "migrate CLAUDE.md
  review docs to subcommands" was wrong — those docs shouldn't live in CLAUDE.md at all.) Careful pass,
  it's the global config. (alex-mextner/agent-tools#15)
- 🔮 **Decisions-as-buttons + the hanging-question DANGER** (#3706): verify the agent
  question-with-options flow (tg inline tappable buttons) actually works end-to-end. **Critical
  bug risk:** `tg-ctl` injects inbound messages into the agent's tmux pane via `send-keys`; if an
  interactive prompt/question is open in that pane, the injected text is typed INTO the prompt and
  LOST/corrupts it. Fix: answers must come through a SEPARATE channel (tg inline buttons → routed
  reply), and while a question is pending tg-ctl must DEFER/queue inbound text injection (detect the
  pending-question state) rather than blast it into the pane. Test + fix. (alex-mextner/tg-cli#30)
  — 🔬 review (tg-cli PR #60): a SCOPED forwarded question now waits INDEFINITELY for its answer
  (no idle-exit while pending). Related picker/routing work also under review: PR #62/#64 (`/agent`
  inline-button picker), PR #69 (per-pane registration set — every live session forwards), PR #55
  (recognized reply route never loses to the global registration), PR #66 (ask-forward logging).
  🔮 building: the ambiguous-target outbound button picker + the `--tag answer` reply-to policy.
- 🔬 **tg-cli `tg#<id>` message-ref convention** (tg#3715): reference inbound TG messages as `tg#3715`
  (NOT bare `#3715` — collides with PR/issue refs). The inbound inject wrap should render the id as
  `tg#<id>`; the autolink layer must recognize `tg#\d+` as a message reference and link it, **before**
  the PR/issue `#\d+` detection runs. ("— reply via tg" suffix already removed, tg-cli main 83ac5db.)
  (alex-mextner/tg-cli#28)
- 🔬 **tg-cli file-excerpt attachment** (tg#3715): when a message gives a file **path + line / line-range**
  (in any of the common formats — `path:42`, `path:10-20`, `path#L10-L20`, etc.), attach the actual
  excerpt (the referenced lines) as a quote below. Currently broken — fix. (alex-mextner/tg-cli#29)
  — 🔬 review (tg-cli PR #49): strip wrapping punctuation so a `path:line` spec in prose attaches its
  excerpt.
- 🔬 **tg-cli `/new` command** (tg#3717): `/new [<model>] [<dir>] name [<task description>]` — spawn a new
  agent session. **Omitted options are chosen interactively via inline buttons.** Directory options =
  the dirs already in use + their `..` parents, all normalized + uniq + ranked LRU/MRU. `name` validated
  with a **uniqueness warning**. (Pairs with the decisions-as-buttons work.) (alex-mextner/tg-cli#27)
- ⏳ **tg-ctl must self-register for system autostart on first run** (CTO 2026-06-16): on first launch the
  tg-ctl daemon installs a boot/login autostart entry so it survives reboots — per-OS, for every OS
  agent-tools supports: macOS → launchd LaunchAgent (same mechanism as continuum's `Tmux.Start.plist`);
  Ubuntu/Debian/other Linux → systemd **user** service (`systemctl --user enable`), with a fallback for
  no-systemd. Idempotent (re-run safe), removable, and the supported-OS matrix lives in agent-tools.
  Pairs with the task-cli/tg-ctl daemon-supervisor work (§3 daemon-supervisor).
- ⏳ **review brainstorm must FAIL LOUD when backends return empty (not silently "converge")** (CTO
  2026-06-16): observed a brainstorm that ran 5 rounds with every round empty ("(no output)") and the
  moderator stamping `DECISION: STOP` each time → it printed an empty synthesis and exited as if it
  worked, wasting ~20 min of "it's still thinking". When all/most panel seats return empty for a round,
  the run must surface a clear error (dead/credential-less backends) and non-zero exit, not a hollow
  STOP. Pairs with the dead-provider cleanup (Fireworks suspended) — and with the new `review sessions`
  resume work (a dead run should be resumable AND diagnosable).
- 🔬 **tg-cli rejects messages containing URLs / `--` / colons with a bogus "Only those HTML tags are
  supported" error** (CTO 2026-06-16): a plain-text message (no `<`/`>`/`&`) that merely contains a
  `https://…` link or `--flag` or `1: foo` fails to send; stripping the URL/double-dash made the SAME
  text send fine (`OK`). So tg is defaulting to an HTML/markdown parse_mode and mis-parsing ordinary
  punctuation as entities. Fix: auto-escape (or send `parse_mode=None` by default and only opt into
  HTML via `--format html`), so links and CLI snippets in reports just work. Cost us 4 failed sends.
  PINPOINTED: the trigger is the `://` scheme — `https://github.com/...` fails, bare `github.com/...`
  sends `OK` (telegram autolinks the domain). Even a correct `<a href="https://…">` tag is rejected by
  tg's OWN pre-send validation (before telegram sees it), confirming it's tg-cli mis-parsing, not the
  Bot API. Workaround in use: send links as bare domains (no scheme).
  — 🔬 review (tg-cli PR #48): `parse_mode` now follows `--format` only, so plain text with a URL /
  `--` / `:` sends without 400ing. Workaround no longer needed.
- 🔬 tg-cli #25: fix-or-justify the 8 CodeQL TS findings (length-counting helpers + install TOCTOU).
  (alex-mextner/tg-cli#31)
- 🔬 tg-cli AGENTS doc carried a HARDCODED test count that drifted — replace any literal count with
  "all tests pass / see CI", never a number that goes stale. **SHIPPED via tg-cli #90** (06-25). (alex-mextner/tg-cli#32)
- ⏳ hyper-saas AGENTS.md "further-trim candidates" (Systematic Debugging / Dead-code / On-Task-Completion
  / Naming / Codex-Specific) — CTO to confirm before trimming (generic blocks now covered by skills).
  (No tracking issue — hyper-saas is a product repo, not a tool repo, and this is CTO-gated.)
- 📋 Billing: Fireworks/Fire Pass (`glide` account) suspended — only that provider is down; rest work.
  (No issue — account-status note; the dead-provider dependency is removed by alex-mextner/review-cli#25.)
- ⏳ **`review --mcp` is broken ecosystem-wide** (no tracking issue yet — surfaced by the auto-mode rollout
  reviewers): the review subcommand refactor dropped the `--mcp` flag, but `~/.claude/mcp/mcp.json` AND
  every rig.yaml `mcp.review.command` still invoke `review --mcp` → `rig apply` registers a dead MCP
  server (and the global review MCP is currently inert). Fix-or-remove: implement a real review MCP
  entrypoint, or drop `mcp.review` from the rig.yaml template + the global registration. Affects all
  six provisioned tool repos' rig.yaml. — 🔬 review (rig-cli PR #60 + #62): the dead `mcp.items.review`
  slot is dropped from the shipped rig template (+ the fake-catalog test now mirrors reality). The
  global `~/.claude/mcp/mcp.json` registration still needs the fix-or-remove pass.

### 10. Third-party skill/tool ecosystem: enable · harmonize · delineate (RESEARCH) (tg#3754)

**Problem (CTO observation):** parse a month of hyper sessions and **most installed third-party skills/tools
were never invoked.** Same root cause as the skill-loading + hook-firing gaps — *installed ≠ discovered ≠
applied.* We've accreted overlapping tool systems with no routing doctrine → agents default to grep/Read and
ignore the specialized tools, or burn tokens choosing between redundant ones. Need a data-driven triage:
which to enable, which to harmonize, which to delineate, which to prune.

**Full inventory of third-party / external systems to triage (NOT our own ecosystem):**

- 📋 **MCP servers (tool providers):**
  - 📋 **Haft** (`h-reason`) — structured engineering reasoning, decision artifacts, FPF patterns. Heavy ceremony.
    Overlaps superpowers brainstorming + the `h-reason` skill dir.
  - 📋 **superpowers** — skill-discovery framework + workflow skills (using-superpowers, brainstorming, debugging,
    TDD…). Self-asserts at SessionStart.
  - 📋 **sverklo** — multi-repo code intelligence: semantic search, concepts/clusters, memories, impact/deps,
    review-diff. Overlaps serena + grep + the `semantic-code-search` skill.
  - 📋 **serena** — LSP-backed semantic code: symbol find/edit, references, onboarding, project memories.
    Overlaps sverklo + grep.
  - 📋 **context7** — live library/framework docs. Overlaps WebFetch + agent-browser.
  - 📋 **computer-use** — desktop control (native apps, cross-app).
  - 📋 **claude-in-chrome** — browser automation (DOM-aware). Overlaps computer-use browser tier + agent-browser.
- 📋 **CLI-backed external skills:** **agent-browser** — read web pages as markdown (replaces WebFetch for long
  pages). Overlaps context7 + WebFetch + claude-in-chrome.
- 📋 **Hand-authored skill dirs (non-rule):** **h-reason**, **debate-swarm**, **moshi-best-practices**.
- 📋 *(Boundary, NOT the subject: our own — tg, review, draw, 3d, rig, task-cli, rtk, linear, + 50 agent-tools
  rule-skills. The research delineates the third-party tools AGAINST these.)*

**Three axes:**

1. **Enable (заэнейблить)** — audit each: actually installed + discoverable in EVERY harness we support?
   (Likely many are configured-but-inert or cc-only — same failure class as skill-loading/hook-firing.)
2. **Harmonize (подружить)** — make overlapping systems compose, not collide: Haft-vs-superpowers reasoning;
   serena-vs-sverklo memories (two stores + agent-tools MEMORY.md = THREE memory systems); two
   "think-before-build" frameworks; agent-browser-vs-context7-vs-WebFetch doc reading.
3. **Delineate (разграничить)** — a written routing doctrine / decision table so an agent picks right without
   burning tokens: code search → serena (symbol-precise) | sverklo (cross-repo concept) | grep (literal);
   docs → context7 (libraries) | agent-browser (arbitrary web); reasoning → Haft (irreversible/decision-record)
   | superpowers brainstorming (divergence).

**Evidence step (the killer):** parse ~1 month of hyper session transcripts (`~/.claude/projects/**/**.jsonl`),
count tool/skill invocations by name, surface the never-fired and rarely-fired. Output = a routing-doctrine doc
with concrete enable/prune/route actions (likely a rig provisioning concern + a routing skill).
Tracking issue: **alex-mextner/agent-tools#19**.

**✅ Evidence DONE (2026-06-15) — hypothesis confirmed:** only **9/204 hyper sessions (4.4%)** invoked any
third-party tool; serena + claude-in-chrome = literal zero. Root cause: those tools are **deferred** (need a
ToolSearch first) so the agent stays on the zero-friction Bash+grep path. Full table + script in **#19**.

---

## 📇 Open-ticket ledger — every open issue/PR (keep in sync; nothing dropped)

*Snapshot 2026-06-15. The prose sections above carry the context/detail; THIS list is the completeness index —
if a ticket exists it must appear here. Details live in the tickets, not here.*

**2026-06-22 → 06-25 delta (all 🔬 under CTO review — MERGED, awaiting review; see the dated section
at top; 93 PRs across 7 repos):**
tg-cli #47/#48/#49/#50/#51/#54/#55/#60/#61/#62/#64/#66/#69/#71/#73/#76/#77/#79/#81/#83/#85/**#87**/**#89**/**#90**/**#91** ·
review-cli #53/#55/#59/#62/#64/#66/#68/#70/#72/**#74**/**#77** ·
rig-cli #53/#54/#55/#56/#58/#60/#62/#64/#67/#69/#71/#72/#74/#76 ·
agent-tools #78/#79/#82/#83/#85/#90/#93/#95/#96/#99/#100/#102/#103/#105/**#108**/**#110**/**#111**/**#116**/**#117**/**#118**/**#119** ·
task-cli #11/#14/#17/#18/#21/#23/**#26**/**#28**/**#30**/**#31**/**#33**/**#34**/**#35**/**#37** ·
3d-cli #10/#12/#14/#16/**#18** · draw-cli #6/#8/**#9** *(bold = the 06-24/06-25 post-wave)*.
**Stale-done CLOSED this session (were carried as open):** tg-cli #28/#29/#30 · agent-tools #86 · rig-cli #5/#6.
**OPEN (🔮 building / deferred or ⏳ CTO-gated):** review-qa **Tier-2** only (ext Tier-1 SHIPPED, review-cli
#74); task-cli **#2 daemon webhooks** (daemon foundation shipped #26/#33/#35) + **#32 status-identity** +
**#4 integrations**; **tg-cli #27 `/new`** (bigger daily-critical interactive flow) + **#31 CodeQL**;
research-cli spin-out; rig-cli **#7 rollout wave-2** + **#8 models.yaml** + **#10 clean-room e2e**;
agent-tools **#120 flag-abbrev** (require-ticket follow-up);
review-cli **#60/#61/#75** (qa hardening). The §Remaining-work and §9 lines above are annotated
`🔬 review (PR #…)` where a shipped PR advanced them.*

**2026-06-16 delta:** MERGED → agent-tools #20 (bridge, +wired+live-proven), #29 (lib-retry), #32 (mcp-policy),

# 34 (=#19 triage), #35 (strict-ticket), **#38** (ci/tests gate, NEW); rig-cli **#21** (rig stats, NEW)

review-cli **#28**+**#29** (spec-web). OPENED → agent-tools **#39** (bridge advisory-msg), review-cli **#30**
(spec-web autosave race). STILL OPEN → agent-tools #17 (format-hook), #30 (lib-config, 2 P2 incl. umbrella-
packaging systemic), #33 (lib-advertise). SYSTEMIC TODO → `lib/pyproject.toml` packages only agenttools_log.

- ⏳ **agent-tools** — #12 shared-lib (Python) · #13 research-cli · #14 harvest skills · #15 slim ~/.claude/CLAUDE.md ·
  #18 agent-hooks→CC bridge *(✅ built: PR #20 + rig-cli #12, clean-room-proven; live-CC round-trip pending)* ·
  #19 third-party tool triage · #21 OSS license-policy gate · #22 self-hosted security dashboard ·
  #24 Trivy CI gate · **PR #17** format-on-write hook · **PR #20** hook-bridge dispatcher
  *(merge AFTER live-CC verify; before rig-cli #12)*.
- ⏳ **rig-cli** — #5 enable repo security settings · #6 auto-mode = gitignored local · #7 rollout wave-2 (bots +
  self) · #8 model-currency manifest+cron · #9 multi-harness skill provisioning (codex/oc/gemini/cmd/pi) ·
  #10 clean-room/Docker e2e · **PR #12** hook-bridge provisioning *(depends agent-tools #20)*.
  *(#11 skill-harness-link ✅ MERGED 2026-06-15.)*
- 🔬 **review-cli** — #24 all board models agentic via opencode · #25 stale `DEFAULT_MODELS` → manifest ·
  #26 propagate canonical ecosystem one-liner to other repos.
- ⏳ **tg-cli** — STILL OPEN (re-verified 2026-07-25 via `gh issue view --repo alex-mextner/tg-cli`):
  only #26 `/tasks` remains open. **#27 `/new` and #31 CodeQL are both CLOSED** (stale in the original
  ledger text — corrected here, matches the 🔬/📋 markers already recorded for #27/#31 elsewhere in
  this file). CLOSED this session: #28 (`tg#<id>` ref+autolink) · #29 (file-excerpt attachment) · #30
  (decisions-as-buttons + inject-collision) · #32 (stale test count — fixed via #90; the doc now says
  "all tests pass / see CI", no literal count to drift).
- 🔬 **3d-cli** — **PR #1** slim AGENTS.md (rollout).
- ⏳ **task-cli** — #1 Phase-2 deps+Gantt · #2 daemon+webhooks · #3 completion/due notify · #4 integrations.
  *(foundation in-flight, agent a9cef4ca — check for its branch/PR.)*
- 📋 **draw-cli** — none open.

> Bridge subagent (agent-tools#18) will likely open NEW PRs (agent-tools + maybe rig-cli) — add them here when it reports.

---

## Key constraints / lessons (read before continuing)

- 📋 **Python-only ecosystem; tg migrates last; lib is plain Python.** (Decided twice — don't re-ask.)
- 📋 **Every provider/harness write-up should be GENERAL, not special-cased** (no "opencode does X" when
  every harness does X). Models marked by **capabilities**, not just version.
- 📋 **auto-mode `bypassPermissions` = COMMITTED `.claude/settings.json`** (CC's shared slot → turns on by
  itself; `.claude/settings.local.json` is the gitignored personal slot). [Reversed 2026-06-15.] Its
  "safe because guards fire" rationale is **RESOLVED 2026-06-16** (#18/#20 bridge merged + wired into
  `~/.claude/settings.json` + live-proven; guards fire under auto-mode). No longer bridge-pending.
- 📋 **Use `gh ship`, not raw `gh pr merge`** (the `block-raw-pr-merge` guard enforces it — now LIVE via the bridge).
- 📋 Tool repos work directly on `main` (push often); hyper-saas via PR + `gh ship`.
- 📋 This roadmap lives in `agent-tools` because ecosystem work should run from a tool repo / agent-tools,
  not from an unrelated product repo.

---

<details>
<summary>📜 <b>Session transcript — 2026-06-15 → 06-17 CTO feature sections</b> (~50 dated items: GHAS-parity, rig tmux/error-system/status, review-cli resilience/help/dashboard, task-cli, hooks-doctrine wave. Most shipped across the 06-16/06-17 merge waves; the authoritative open subset is the §Open-ticket ledger above. Click to expand.)</summary>

## 🆕 From the GHAS-parity analysis (2026-06-15, tg#3777/#3779/#3780)

We dropped every GHAS / licensed-action gate for free OSS equivalents (gitleaks OSS **binary** instead of the
licensed `gitleaks-action`; scripted `dep-audit.sh` instead of `dependency-review-action`). Verified parity:
secret-scan is **identical** (same engine + `useDefault` ruleset), dep-audit is **equal-or-better** for vulns
(whole-tree vs PR-diff). Two follow-ups the parity check surfaced:

- 🔬 **OSS license-policy gate — agent-tools #21.** The one real gap: `dependency-review-action` also enforced a
  license allow/deny policy, which `dep-audit.sh` does not. Fill with an OSS license checker
  (`license-checker` / `cargo-deny` / `pip-licenses`), rig-provisioned as a CI gate, default-deny copyleft
  (AGPL/GPL) to match the old template. Lives in `ci/license-policy/`. #21 CONFIRMED CLOSED via `gh`,
  matching the later 🔬 agent-tools #108 entry elsewhere in this file ("OSS license-policy CI gate …
  Closes the long-standing license-policy gap … ex-#21").
- ⏳ **Self-hosted security findings dashboard — agent-tools #22.** The one thing GHAS uniquely offered that we
  don't replicate is its hosted code-scanning dashboard. Aggregate the OSS scanners' SARIF/JSON (CodeQL-CLI,
  Semgrep, gitleaks, dep-audit) into one Tailscale-served dashboard — likely **extending review-cli's existing
  dashboard** (SARIF is the common format). CTO: copy GitHub's code-scanning UI and improve it (cross-repo
  view, our own triage state, link to the suppressing `// codeql[...]` line).

## ⚠️ rig tmux v2 — REAL reboot cycle broke (CTO 2026-06-16, post-reboot)

The #24/#26 tmux provisioning passed unit (456) + tmux parse-check but I never ran a REAL
e2e (apply→save→REBOOT→restore) on a live machine. A reboot exposed multiple defects — fix
so a CLEAN-machine `rig init` does EVERYTHING with NO manual steps:

1. **boot**: launchd ran `tmux start-server` = an EMPTY server (config/plugins load only on the
   first session, so continuum-restore never fired). Use a boot script that `tmux new-session -d`
   (loads conf) THEN restores; and `rig` must `launchctl load` the agent itself (it didn't).
2. **cc-save detect**: filtered on `pane_current_command == claude`, but cc shows as its VERSION
   (`2.1.178`); the real process is a CHILD (`claude --resume`, pid in the pane's tree). Detect via
   the pane's process tree, not the command string → map was empty → cc never resumed.
3. **default-command `''`** → resurrect restores a NON-login shell → `.zprofile` not sourced.
   Set a login shell so restored panes get the full env.
4. **resurrect dir** `~/.tmux/resurrect` was absent → no snapshot written at all. Ensure it exists.
5. **old continuum boot not cleaned**: `osx_iterm/terminal_start_tmux.sh` still register in macOS
   Login Items (competing boot). rig must disable/clean them (continuum `osx_disable.sh` + unload).
6. **plugins/tpm install + first save**: on a clean machine rig must install tpm + resurrect/
   continuum (clone) and take a first resurrect save, else there is nothing to restore.
**Acceptance = a REAL e2e**, not unit-only: fresh HOME → `rig init` → boot brings tmux up with the
restored session, cc resumes by session-id, panes are login shells. Verify on THIS machine too.

## ⚠️ rig MUST gate smoke.sh on pre-commit + always run smoke, not just pytest (CTO 2026-06-16)

Two failures the same day traced to the SAME root: I shipped rig changes after green PYTEST
(456/460) but NEVER ran `bash tests/smoke.sh` — the real `rig init/apply/status` flow.

- 📋 `rig status` errored `unknown mcp item(s): review (known: none)` — a STALE `mcp.items.review`
  (the `review --mcp` slot was removed in #32) lingered in `~/.config/rig/config.yaml`. smoke
  (which runs `rig status` against a sample config) would have caught it. (FIXED on this machine
  by removing the block; the rig-cli template is already clean — verify `rig init` never re-adds it.)
- 📋 the tmux reboot-cycle defects (see "rig tmux v2") — unit-only, no real e2e.
REQUIREMENTS:

1. **rig provisions a pre-commit hook (via its own git-hooks dispatcher / lefthook) that runs
   `bash tests/smoke.sh`** for the rig-cli repo (and offers it to any repo that has a smoke target),
   so a commit that breaks the real CLI flow is blocked locally — not just in CI.
2. **Every rig PR runs smoke (real flow), not just pytest.** smoke must exercise `rig status` with a
   config that includes EVERY catalog area (skills/hooks/CI/mcp/tmux) so an unknown-item / dead-slot
   regression fails loudly. Add an assertion that `rig status` exits 0 on the sample config.
3. CI already runs smoke (`tests/smoke.sh`) — keep it; the gap was LOCAL pre-commit + my discipline.

## ⚠️ rig (+ ecosystem) error system v2 — every error says what/why/how-to-fix, with heuristics (CTO 2026-06-16)

Today's failures were hard to diagnose because the errors were thin: `unknown mcp item(s): review
(known: none)` (no hint it was a removed slot or how to remove it); a dead rtk hook surfaced only as
a generic CC "PreToolUse error"; tmux defects were SILENT (no error at all). Build a real error layer:

- 📋 **Every rig error = 3 parts**: WHAT happened, WHY (root cause / context), HOW to fix (a concrete
  command or edit). Pattern from the `structured-exit-codes` skill. Stable, per-class EXIT CODES.
- 📋 **Heuristics**:
  - ⏳ *did-you-mean*: for an unknown item (mcp/skill/hook/ci/tmux key) Levenshtein-match against the
    catalog and suggest the nearest valid name, OR say "no such slot" + list the known ones.
  - ⏳ *deprecated/removed slots*: a registry of removed slots (e.g. `mcp.items.review` removed in #32,
    `review --ln`/`--mcp` dropped) → the error names the PR and says "remove it from <config path>"
    (and, when `rig config unset` exists, prints that exact command).
  - 📋 *missing-target*: a config points at a file/binary that doesn't exist (the rtk-hook case:
    settings.json → a hook path that's gone) → say which file is missing and how to regenerate it.
  - ⏳ *silent-success guard*: an apply/provision that did nothing meaningful (empty cc-map, no snapshot,
    hook installed but inert) must WARN, not look clean — tie into `rig status`/`doctor`.
- ⏳ **`rig doctor` / `rig status` surface these proactively** (drift + dead-config + missing-target), so
  a problem is visible BEFORE it bites at runtime/reboot.
- 📋 Generalize the pattern so review/tg/draw/3d/task CLIs reuse it (shared lib `errors` module candidate,
  §3). Tests assert the message contains the fix hint + the right exit code per failure class.
Build as a background subagent; do NOT merge without the CTO's verify (real `rig status`/smoke proof).

## rig status — separate GLOBAL vs REPO layers + clearer drift (CTO 2026-06-16, part of error-system v2)

`rig status` in a repo (e.g. hyperide) mixes machine-wide GLOBAL drift (skills in ~/.agents, harness
links, agent-hooks — owned by `~/.config/rig/config.yaml`) with REPO drift (this repo's `.github/`
CI, AGENTS/CLAUDE symlinks, rig.yaml). Group the output by LAYER:

- ⏳ **GLOBAL** (from ~/.config/rig/config.yaml): skills/hooks/harness/mcp on this machine.
- ⏳ **REPO** (from ./rig.yaml): CI workflows, repo symlinks, repo settings.
Each item should show WHICH layer/config FILE declares it (or "not declared in any layer").
Also make the "extras are left for you to decide" guarantee LOUD (apply NEVER deletes disk-not-declared
items) so a user isn't scared `rig apply` will nuke their skills — print that reassurance in `status`
and in `rig apply --help`. And: a repo with NO committed rig.yaml should say so prominently with the
fix (`run rig init to create one`), not just a one-line warning buried above a 49-line drift dump.
Ties into the did-you-mean / 3-part-error work.

## rig status: non-git dir must NOT claim "no rig.yaml, should be committed" (CTO 2026-06-16)

`rig status` run in `~` (no `.git/`) prints "warning: no rig.yaml in this repo (it should be committed)"
— but `~` is NOT a repo at all. Detect non-git dirs: show ONLY the global layer + "(not a git
repository — repo layer/rig.yaml N/A here)". The "commit a rig.yaml" advice only applies inside an
actual git repo. Part of the error-system/status-clarity work.

## review-cli must not crash outside a git repo (CTO 2026-06-16)

Running bare `review` outside a `.git` repo throws a raw RuntimeError + Python traceback
(`_git_diff` → `git diff` failed) — a user just trying the tool gets a stack trace. Fix:

- ⏳ git is needed ONLY by the diff modes (`review`/`review review`, `--diff`, `--staged`).
  `just-ask`, `quorum`, `brainstorm` do NOT need git and must work anywhere.
- ⏳ Outside a git repo, the diff path must fail GRACEFULLY (no traceback): a clear 3-part message —
  "not in a git repository; the diff review needs one. Run `review just-ask`/`quorum`/`brainstorm`
  (no git needed), or cd into a repo" — and a stable non-zero exit code (align with the rig
  error-system / `structured-exit-codes` work).
- ⏳ Bare `review --list-defaults` / `--show-board` / `--help` (meta) must work outside a repo too.
- ⏳ Add a smoke/test: run `review` and `review just-ask "x"` in a non-git tmpdir; assert no traceback,
  helpful message + correct exit on the diff path, and just-ask/meta paths work.
Fix as a background subagent (review-cli), no merge without CTO verify.

## review-cli: rename `review review` → `review diff`; bare `review` = HELP (CTO 2026-06-16)

`review review …` is bad UX (stutter). Two changes:

- 📋 The diff-review SUBCOMMAND is renamed `review` → **`review diff`**. (The "review review" stutter and
  the "bare review defaults to a diff review" behavior were a mistake — never do that.)
- 📋 **Bare `review` (no subcommand/args) prints the HELP/usage**, it does NOT silently run a diff review
  (today it dumps "No diff to review" or, outside git, crashes). Meta flags still work
  (`review --list-defaults` etc.).
- 📋 Other subcommands unchanged: `diff`, `brainstorm`, `just-ask`, `quorum`, `dashboard`, `spec-web`, …
- ⏳ Update README/AGENTS, the `~/.claude/CLAUDE.md` review-skill blurb, and any docs that say
  `review review`. Decide migration: a removed-`review`-subcommand should print a one-line
  "use `review diff`" pointer (like the old removed mode-flags), not error opaquely.
SEQUENCING: do this in review-cli AFTER the "non-git graceful" PR merges (same cli.py/dispatch area) —
one review-cli agent at a time to avoid collisions. CTO verifies, no blind merge.

## install-* commands must show INSTALLED state (✓ + "already configured") (CTO 2026-06-16)

The `install-skill` / `install-commit-hook` / `register-module` (review-cli) and rig's `install-*`
surfaces should INDICATE current state, not just offer the action. When the thing is already set up,
show a green ✓ + "already configured — nothing to do" (idempotent, like `rig doctor`'s dependency
checks). So a user listing/running install-* sees what's done vs pending at a glance. Applies to
review-cli install subcommands and any rig install/provision listing (status/doctor style). Part of
the ecosystem error-system / clarity work — implement alongside it (subagents), CTO verifies.

## Topic-based help across the ecosystem (CTO 2026-06-16)

Tools need DEEP help topics, advertised from the main help:

- ⏳ **review**: `review help config` (or `review --help config`) — a real config reference: the reviewer
  board, model/backend selection, config file paths + cascade (`~/.config/review-cli/…` + repo), env
  vars, how to add/override a seat. The MAIN `review --help` must POINT at it ("see `review help config`
  for configuration", plus any other topics).
- ⏳ **Generalize**: a `<tool> help <topic>` convention for non-obvious features (config, board/models,
  auth, output formats, etc.), and the top-level `--help` LISTS the available topics. Apply across the
  ecosystem — review, tg, draw, 3d, rig, task — each ships the topic helps relevant to it.
- ⏳ Keep topic help in sync with behavior (the `help-docs-sync` skill): a flag/behavior change updates its
  topic help in the same commit. Tests assert main `--help` lists the topics and each topic prints.
Implement via subagents (per tool), CTO verifies. Start with `review help config` since that's the ask.

## Help must show ACTUAL defaults — esp. --model (CTO 2026-06-16)

In `review --help` (and across tools) every configurable option must show its EFFECTIVE default value,
not a vague description:

- ⏳ **`--model`**: today it says only "model/backend to run; repeat or comma-separate" — it must show the
  ACTUAL default (the reviewer board / models that run when you DON'T pass --model). E.g. "(default: the
  active board — see `review help config` / `--show-board`)" or the concrete default model id(s).
- ⏳ Other configurable defaults (`--pool` 4, `--timeout` 1200/240, `--moderator`, `--vision-timeout`, …)
  already show some defaults — make ALL of them show their CURRENT effective value, respecting the config
  cascade (if the user set a default in config, the help reflects it, or at least points at where it's set).
- ⏳ Generalize to the ecosystem: a flag with a configurable default prints that default in --help. Ties into
  topic-help (`review help config`) and the help-docs-sync skill (tests assert --help shows defaults).
Implement with the review-cli UX subagent (queued after non-git), CTO verifies.

## Subcommand-only options belong in the subcommand help, not the global list (CTO 2026-06-16)

`review --help` dumps options that apply only to specific modes/features into the GLOBAL option list,
cluttering it. Scope them:

- ⏳ **Visual-only opts** (`--visual` and its companions `--before`, `--intent`, `--expect`, `--check`,
  `--json`, `--strict`, `--no-ai`, `--no-local-model`, `--vision-timeout`, `--project`) belong in the
  visual section / `review --visual --help`, NOT the global list.
- ⏳ Mode-only opts belong on that subcommand's parser (`review brainstorm --help` already owns
  `--rounds`/`--max-rounds` — do the same for every mode/feature-specific flag).
- ⏳ The GLOBAL `review --help` shows only TRULY global opts (`-m/--model`, `-C`, `-o`, `--timeout`,
  `--list-defaults`, `--show-board`, `--pool`) + the subcommands list + the topic-help pointer.
- ⏳ argparse: give each subcommand (and the composable `--visual` feature) its own argument group/parser so
  `--help` is scoped automatically. Tests assert a visual-only flag does NOT appear in the global help and
  DOES appear in the visual/subcommand help.
Part of the review-cli UX subagent (queued after non-git). Generalize the principle to other tools' CLIs.

## review dashboard as a managed service (run/start/status/stop/enable/disable) (CTO 2026-06-16)

`review dashboard` gets service-style subcommands:

- ⏳ **run** — run in the foreground (this shell), blocking; for `disable`d / ad-hoc use.
- ⏳ **start** — start in the BACKGROUND (detached daemon), return immediately.
- ⏳ **status** — is it running? pid/port/url.
- ⏳ **stop** — stop the background instance.
- ⏳ **enable** — install OS autostart (launchd LaunchAgent on macOS / systemd user unit on Linux — same
  mechanism as the tg-ctl autostart item) AND start it in the background now.
- ⏳ **disable** — remove autostart (and stop).
- ⏳ **bare `review dashboard`** (no subcommand) → print the dashboard HELP (like bare `review` → help),
  not launch anything.
- ⏳ On `start`/`run`, HINT how to enable autostart ("run `review dashboard enable` to start at login").
  Idempotent, removable; supported-OS matrix in agent-tools. SHARE the run/start/status/stop/enable/disable
  OS-autostart machinery with tg-ctl autostart and the daemon-supervisor (§3) — one service-management
  helper, not per-tool copies. review-cli UX subagent (queued), CTO verifies.

## review-cli dashboard is under-tested — fix data + tabs, real visual QA (CTO 2026-06-16)

A live look at `review dashboard` shows it was never properly tested:

- ⏳ **literal "topic"** is rendered for every brainstorm session instead of the REAL brainstorm topic
  (a placeholder/parse bug — pull the actual topic from the discussion-log header `# Brainstorm: <topic>`).
- ⏳ **missing data everywhere**: panel sessions show no prompt/title; many fields blank.
- ⏳ **tabs barely work**: Overview/Chat logs/Stats/Models&roles/Metrics/Overseer feedback/Modes/Errors/
  Tasks/Prompts/PRs&tickets — several likely empty/broken ("mostly doesn't work").
Fix: populate REAL data per session (brainstorm topic, panel prompt, models, durations, error details);
every tab either renders real data or is removed; wire the parser to the actual log/store format.
ACCEPTANCE = real visual QA (the `visual-proof-cycle` skill): start the dashboard against sample logs,
SCREENSHOT each tab, READ IT BACK, assert no "topic" placeholder and real data shows — not unit-only.
This is the same failure mode as the tmux/smoke gaps today: looked done, wasn't exercised. review-cli
subagent, CTO verifies with a screenshot.

## ONE shared help-formatter in agent-tools/lib — reuse across ALL tools (CTO 2026-06-16)

tg-cli's `--help` is NOT colorized like review/rig/draw — inconsistent. ROOT PRINCIPLE (CTO, repeated):
extract EVERYTHING reusable into `agent-tools/lib` and share it; do not re-implement per tool. Help is a
prime candidate — build ONE shared help layer (Python lib module, §3) and have every CLI use it:

- ⏳ colors/styling, section layout, usage line, subcommands list;
- ⏳ the help-clarity rules from today all live HERE so a fix lands everywhere: show ACTUAL defaults
  (esp. --model), SCOPE subcommand-only options to their subcommand, topic-help (`<tool> help <topic>`)
  advertised in main help, install-* state (✓ already-configured), service subcommands help.
- ⏳ the 3-part error/exit-code layer (error-system v2) is the same story — one shared `errors` + `help`
  module, consumed by review/rig/tg/draw/3d/task.
tg-cli is Bun/TS (migrates to Python LAST) — until then, at minimum match the color scheme; after
migration it imports the shared lib like the rest. Tracking: agent-tools#12 (shared lib). Build the
shared help/errors modules, then refit each CLI (subagents), CTO verifies parity (same look across tools).

## tg --tag: lowercase-english only (CTO 2026-06-16)

`tg --tag` currently accepts Russian (ОТВЕТ/РЕШЕНИЕ/...) and uppercase. Restrict to LOWERCASE ENGLISH
words only: `answer` / `decision` / `problem` / `report` (and any other defined tags) — lowercase. Reject
anything else with a clear 3-part error ("tag must be a lowercase english word, e.g. answer/decision/
problem/report"). Update tg docs + the ~/.claude/CLAUDE.md tg blurb (it currently shows ОТВЕТ/РЕШЕНИЕ).
tg-cli is Bun/TS (no local checkout — clone first); pairs with tg help-coloring (match other tools) and the
shared help/errors lib (after the Python migration). Subagent, CTO verifies.

## tg help specifics (CTO 2026-06-16) — apply with the tg-cli help work

- ⏳ **Don't repeat `[--format text|html]` many times** in the help. It's a global option — show it ONCE
  (in the global options), not duplicated per command/usage line.
- ⏳ **Replace the `--format-help` flag with standard topic-help**: `tg help format` (and `tg help <topic>`
  for other such sections). The MAIN `tg --help` briefly LISTS the available topics ("see `tg help format`
  …"). Same convention as the ecosystem topic-help item — not a bespoke `--format-help` flag.
- ⏳ **`tg voice setup` (and similar setup/install surfaces) show actual STATUS**: green ✓ when configured,
  yellow ○ when not — the install-* state principle, applied to voice setup. So the user sees what's done
  vs pending without running it blindly.
These extend the already-queued tg-cli help work (--tag + coloring + shared help lib). CTO verifies.

## rig status must cover ALL reconciled areas, not mostly skills (CTO 2026-06-16)

`rig status` is skill-heavy, but rig reconciles MANY areas — all must show, grouped by area (and by the
GLOBAL vs REPO layer split): skills, agent-hooks (v1 descriptors), git-hooks dispatcher, CI gates,
MCP servers, AGENTS.md/CLAUDE.md symlinks, repo settings (branch protection / GHAS / merge policy),
harness auto-mode settings, tmux config (the new provisioning), model-freshness cron, ship/`gh ship`.
Each AREA shows in-sync vs drift (count) under its heading, so the user sees the FULL picture of what rig
manages and where it's out of sync — not a wall of skill lines with everything else buried. Ties into the
error-system/status-clarity agent (global/repo separation + which config file declares each).

## Commands should work OUTSIDE a repo; task list groups by repo/project (CTO 2026-06-16)

General principle (same spirit as review-non-git): a tool's read/list/global operations should NOT require
being inside a git repo. Specifically:

- ⏳ **`task list` outside any repo** → show ALL tasks across repos/projects, GROUPED by repo/project (a
  heading per project, tasks under it). Inside a repo → scope to that repo (current) but offer `--all` for
  the cross-repo grouped view.
- ⏳ Other task-cli read commands (status/show) work outside a repo too; only repo-bound actions (create-in-
  this-repo) need a repo, and then with a clear message if absent.
- ⏳ Generalize: every ecosystem CLI's non-repo-bound commands run anywhere; only diff/repo-scoped ops need a
  repo, failing gracefully (3-part error) when missing. Pairs with review-non-git + rig-status-non-git.
task-cli is in-flight (foundation agent a9cef4ca) — fold this into it. CTO verifies.

## task list: fallback to all-tasks + pagination + session-vs-all messaging (CTO 2026-06-16)

`task list` defaults to THIS-agent-SESSION's tasks. But:

- ⏳ In a repo, run OUTSIDE an agent session → FALL BACK to showing ALL tasks (grouped by repo/project).
- ⏳ In a session but NO session tasks → same fallback (show all tasks).
- ⏳ In BOTH fallback cases, the output must SAY SO: "showing all project tasks (`task list` defaults to
  tasks created in the agent session)" — so the user understands why they see everything.
- ⏳ **Pagination**: default through a pager (less, like git), with `NO_PAGER`/`--no-pager` support and the
  git convention (no pager when output fits / not a tty). Respect `$PAGER`.
Fold into task-cli (foundation agent a9cef4ca). Pairs with "commands work outside repo / group by project".

## task list pager: interactive-only + higher limit there (CTO 2026-06-16)

Refinement of the task-list pagination: the PAGER (less) is used ONLY in interactive mode (stdout is a
tty). In non-interactive (piped/scripted) output → NO pager, plain text (scriptable). In interactive
mode the display LIMIT is HIGHER (e.g. 100 tasks) since the pager handles scrolling; non-interactive
keeps a small default (or `--all`/all, machine-readable). Respect `NO_PAGER`/`--no-pager`/`$PAGER` and the
git convention. Fold into the task-cli list work.

## Universal zsh tab-completion: shared generator + auto-installer in agent-tools/lib (CTO 2026-06-16)

ONE shared module in `agent-tools/lib` that GENERATES and AUTO-INSTALLS zsh tab-completion for every tool
(review, rig, tg, draw, 3d, task) — not per-tool copies (the "extract everything reusable" principle).

- ⏳ **Generator**: derive completions from each tool's CLI structure — subcommands, options, topic-help
  topics, choice values (argparse introspection for the Python tools). Keeps completion in sync with the
  CLI automatically (no hand-written, drift-prone _tool files).
- ⏳ **Auto-installer**: on first run (idempotent) and/or `<tool> completion install` — write the completion
  into an fpath dir (e.g. ~/.zsh/completions) + ensure it's on `fpath` + `compinit`, with clear status
  (✓ installed / how to enable). Removable (`completion uninstall`). No clobber, idempotent.
- ⏳ Bash later if needed, but zsh first. Ties into the shared help/errors lib (one lib stack, §3).
Build as a subagent on agent-tools/lib + wire each tool, CTO verifies (tab-complete actually works in zsh).

## task-cli classifier (change vs justAsk) — make it accurate, fast, reliable (CTO 2026-06-16)

`task classify 'когда началась сессия?'` → `change` — WRONG (it's a justAsk question). The change/justAsk
classifier is inaccurate. Improve it properly:

- ⏳ **Brainstorm a fast + reliable design** (done as a real review brainstorm — see launch). Likely: cheap
  deterministic heuristics first (interrogative words/`?` → justAsk; imperative verbs/file refs → change)
  to short-circuit obvious cases with ZERO LLM latency, then a small local-model head (haiku) with a
  provider fallback chain only for the ambiguous middle; cache by normalized text.
- ⏳ **Metrics + benchmarks + tests**: a LABELED dataset (change vs justAsk, RU+EN, incl. tricky cases like
  "когда началась сессия?"). Report accuracy / precision / recall per class + p50/p95 latency. CI test
  asserts accuracy ≥ threshold and latency budget. Track regressions.
- ⏳ **Deliberate small BIAS toward `change`**: a false-`change` is cheaper than a false-`justAsk` (better to
  over-propose a task than to silently drop a real change). Tune the threshold accordingly + document it.
- ⏳ **The task-FORMING agent must be told the classification MAY BE A FALSE POSITIVE**: when it turns a
  `change` into a ticket, instruct it to sanity-check ("this was auto-classified as a change and may be
  wrong — if it's actually a question, don't create a ticket") so the bias doesn't create noise.
task-cli (foundation agent a9cef4ca). CTO verifies with the benchmark numbers.

## task list --all shows nothing on hyperide (CTO 2026-06-16)

`task list --all` returns NOTHING in the hyperide repo — it should show ALL tasks across all known
projects/repos (grouped by project, per the outside-repo/grouping item). Bug in the `--all` cross-repo
aggregation (backend query scope, or it only looks at the current repo). Fix + test: `--all` returns tasks
from every configured/known project (GitHub Issues + Linear backends), grouped, with the session-vs-all
messaging. Pairs with the task-list grouping/fallback items. task-cli.

## First-run mapping for external per-repo config → persist to rig.yaml; informative/proactive/friendly (CTO 2026-06-16)

PILLARS of our tooling: informative, proactive, friendly. The `linear` CLI fails cryptically when it can't
infer the team key from the dir name ("Could not determine team key from directory name or team flag") —
the user must manually `linear team list` then `--team HYP`. Our tooling (task-cli with the Linear backend,
and any tool wrapping an external command that needs per-repo config) must instead:

- ⏳ **First-run mapping flow**: when a needed per-repo value (Linear team key, project, etc.) is missing,
  DETECT it (e.g. fetch `linear team list`; if exactly one team → auto-pick) or ASK, then PERSIST it to the
  LOCAL `rig.yaml` (repo config) so it's remembered — no re-asking, no cryptic failures next time.
- ⏳ **Interactive mode**: show the options (the team list) and let the user pick; confirm what was written.
- ⏳ **Non-interactive mode**: auto-infer when unambiguous (single team), else take a flag/env, else fail with
  a clear 3-part error (WHAT/WHY/HOW: "run `<tool> link --team HYP`" or "set X in rig.yaml").
- ⏳ **Informative/proactive/friendly**: tell the user what was detected, what got written to rig.yaml, and
  how to change it — never a bare "could not determine X".
Generalize across the ecosystem; tie into rig.yaml repo config + the error-system. task-cli/rig subagents.

## task list: interactive TUI + non-interactive hints (CTO 2026-06-16)

- ⏳ **Interactive mode (tty)**: `task list` is a TUI — a SCROLLABLE list, tasks are SELECTABLE (arrow keys
  enter to open/act), and tasks CREATED IN THIS SESSION are visually MARKED/highlighted. Take inspiration
  from the linear CLI's TUI (key/state columns, navigation). Pairs with the interactive pager + higher limit.
- ⏳ **Non-interactive mode (piped/script)**: no TUI — plain list, and print a DIMMED/grey HINT showing how to
  open a task or do basic ops (e.g. "↳ `task show <id>` to view · `task done <id>` to close"). Informative,
  proactive, friendly (the pillars) without breaking machine-readability (hint on stderr or clearly dimmed).
Folds into the task-cli list work (grouping, --all, session-vs-all messaging, pager). task-cli.

## task current: map a CURRENT task to the session/dir (CTO 2026-06-16)

A CURRENT task can be mapped to the agent session / working directory:

- ⏳ **`task current get|set|unlink|change|link`** — manage the mapping. `get` shows the current task;
  `link`/`set` binds a task as current; `change` swaps it; `unlink` clears it. (Use consistent verbs;
  `link`+`change`+`unlink`, with `get` to read.)
- ⏳ The current task is HIGHLIGHTED in `task list` (distinct from the session-created marking) and SURFACED
  prominently (e.g. a header line "current: HYP-xxx …" / shown on a bare `task current`).
- ⏳ Persisted per session/dir (alongside the team-key / rig.yaml mapping or session state). Informative when
  set/changed (what it points at now). Pairs with the TUI selection + session-task marking + grouping.
task-cli.

## review CLI must be RESILIENT to backend 500 / rate-limit (CTO 2026-06-16)

Today review brainstorm/panel returned SILENT-EMPTY (0 bytes) when Anthropic backends threw 500 /
"Server temporarily limiting requests" / model-unavailable. review must HANDLE this, not give up:

- ⏳ **Retry with exponential backoff + jitter** per backend on transient errors (HTTP 500/502/503/429,
  "rate limited", "temporarily unavailable") — a few attempts before declaring a seat failed. Later
  sections in this file ("review resilience — concrete retry vs reserve-replace policy", "reserve-replace
  failover IS firing") suggest partial follow-through, but not verifiable as fully closing this specific
  requirement — kept ⏳ rather than guessed done, consistent with its 3 sibling bullets below.
- ⏳ **Fall back to other available board seats** (the failover pool already exists for unavailability —
  extend it to transient errors: a 500 on one seat promotes a reserve, not a dead round).
- ⏳ **NEVER silent-empty**: if a round/all seats fail after retries, FAIL LOUD with a clear error naming
  which backends failed + why + a retry hint + non-zero exit (ties into brainstorm-fail-loud + error-system).
- ⏳ Make timeouts/retry budgets configurable; surface "retrying seat X (attempt n)" so it's not a silent hang.
review-cli subagent. CTO verifies by simulating backend 500s (mock) — retries fire, fallback works, no
silent-empty.

## review resilience — concrete retry vs reserve-replace policy (CTO 2026-06-16)

Refinement of "review must be resilient": classify backend errors and act per class:

- 📋 **Retryable (transient)** — HTTP 500/502/503/429, "rate limited", "temporarily limiting", timeouts:
  RETRY the SAME seat up to ~3 times with exponential backoff + jitter before giving up on it. (3 is the
  default where retrying makes sense; tunable.)
- 📋 **Seat-fatal (not worth retrying)** — model unavailable, auth/credential failure, persistent refusal,
  unknown-model: immediately REPLACE that seat with the next RESERVE from the board (failover), don't waste
  retries on a dead seat.
- 📋 After retries+replacement exhaust the pool+reserves, FAIL LOUD (which seats failed, why, retry hint,
  non-zero exit) — never silent-empty.
Implement in panel.py/backends.py (the run_panel/failover path) — surface "retry seat X (n/3)" / "seat X
fatal → promoting reserve Y". Tests: mock 500 → 3 retries then success; mock auth-fail → immediate reserve
swap; mock all-fail → loud error + exit code. CTO verifies with mocked backend errors.

## On a killed/crashed review → RESUME via `review sessions -s`, not from scratch (CTO 2026-06-16)

review-cli already ships resumable sessions (PR #31): `review sessions -a` lists sessions incl. interrupted
ones, `review sessions -s <id>` CONTINUES from where it stopped. USE IT. When a long review/brainstorm is
killed or crashes (Anthropic storm, timeout), the continuation must `review sessions -s <id>` instead of
restarting from round 1 — for the orchestrator AND for any subagent that runs review/brainstorm.

- 📋 Process rule: before launching a fresh brainstorm/panel, check `review sessions -a` for an INTERRUPTED
  session of the same topic and resume it.
- 📋 Pairs with review-resilience (retry/reserve-replace handles transient errors WITHIN a run; sessions-resume
  recovers a run that was killed entirely). Together: a review never has to start over.
- 📋 Caveat: a run that died BEFORE writing its discussion log (the classifier brainstorm hit 0 bytes — backends
  never produced a round) has nothing to resume; that's the resilience layer's job (retry/fallback so a round
  is actually produced + logged), after which resume works.

## review retry count must be CONFIGURABLE (CTO 2026-06-16)

The ~3 retries (and backoff/timeout budgets) must be CONFIGURABLE — via review-cli config file, env var
(e.g. REVIEW_RETRIES), and/or a flag — not hardcoded. Sensible default (3), overridable per env/run.

## (reserve-replace failover already exists — verify it actually fires in the logs; see check below)

## reserve-replace failover IS firing, but the promotion event is NOT durably logged (CTO 2026-06-16, VERIFIED)

Verified via `~/.config/review-cli/run-stats.jsonl`: the failover fires on nearly EVERY review run.

- 📋 Steady state: `pool_size:4, ok:4, fail:1`, recorded `models=[fable5,opus,codex,glm]`. The planned
  top-4 by board priority (config.py:181-196) is `[fable5,opus,codex,KIMI]`. Kimi (commandcode) dies at
  runtime (timeouts `EXIT 124` / empty `output_tokens=0` in commandcode-r0 logs) and GLM (first reserve)
  backfills it — that's the `fail:1`. So replacement is the steady state, not the exception.
- 📋 Heavy cascade 2026-06-16T20:25:40: `models=[fable5,opus,glm,gemini]`, fail=4 — kimi AND codex failed,
  reserve cascaded down to gemini (priority 8).
- 📋 Live (21:00-21:06): claude-r0 logs show `ClaudeFable5 is currently unavailable` with EXIT 0 — a sentinel
  body that `result_is_usable` catches → another real-time promotion.
GAP TO FIX: the promotion line `[review-cli] board: X failed — promoting reserve Y` (panel.py:339) is
written to **stderr only**, never to the per-backend `~/Library/Logs/review-cli/*.log` files. So the
failover is invisible "by the logs" — only inferrable from run-stats + EXIT codes. Fix: also append the
FailoverOutcome (each promotion: failed seat, promoted reserve, round) to a durable run log so a kill/
post-mortem can SEE the replacements, not just the final usable set.

## rig must ignore .claude/worktrees globally via core.excludesfile (CTO 2026-06-17, DECIDED)
The harness (Claude Code / Workflow tool) creates throwaway worktrees under each repo's
`.claude/worktrees/`; they pollute `git status` in every repo and even got accidentally
committed into agent-tools as 160000 gitlinks (commit 6d9b5ac, cleaned up forward by
untracking them). CTO DECISION (chosen over PR #23's per-repo `.gitignore` block): rig
provisions a single rig-managed marker block in the GLOBAL git excludes file
(`core.excludesfile`), so it covers EVERY repo on the machine with zero per-repo commits.
- ⏳ Target resolution: respect an already-set `core.excludesfile` (this machine: `~/.gitignore`);
  else set it to `~/.config/git/ignore` and write the block there. Clean machine: `rig init`
  does both (set the git config + write block).
- ⏳ Block: `# >>> rig-managed (do not edit) >>>` … `# <<< … <<<`, default entry `**/.claude/worktrees/`.
- ⏳ STRICT idempotency required: evidence of a prior non-idempotent appender — `~/.config/git/ignore`
  had ~280 duplicated `**/.claude/settings.local.json` lines (and that file is currently DEAD because
  core.excludesfile points elsewhere). rig's reconcile must never duplicate; collapse the managed
  region only, never touch user lines.
- 📋 Immediate relief already applied by hand on this machine: rig-marker block with `**/.claude/worktrees/`
  appended to `~/.gitignore`; all repos now ignore it. Reworked rig feature (branch
  `rig-global-excludesfile`, supersedes #23) makes it durable / clean-machine.
- ⏳ FOLLOW-UP (tracked): clean the DEAD `~/.config/git/ignore` — it has ~280 duplicated
  `**/.claude/settings.local.json` lines and is not even read by git (core.excludesfile points
  to `~/.gitignore`). Harmless now but a latent landmine if rig ever repoints excludesfile there.
  Awaiting CTO go (offered in chat); collapse the dups, leave one canonical line, touch nothing else.

## CORRECTION: --tag ANSWER/ОТВЕТ is NOT a bug — it requires --reply-to (by design)
My earlier entry here claimed `--tag ANSWER` emits an "invalid HTML pill" and marked it VERIFIED.
That was WRONG — I saw the error text and GUESSED the cause instead of reading the source. The CTO
corrected it: `--tag ANSWER`/`ОТВЕТ` REQUIRES `--reply-to <message_id>` — answering means answering
a SPECIFIC message (tg source line ~122: "ANSWER (ОТВЕТ) tag REQUIRES this"). `tg --tag ANSWER "x"`
without `--reply-to` is correctly rejected; `tg --reply-to <id> --tag ОТВЕТ "x"` works (verified by
threading a real reply to msg 3970). The ONLY arguably-rough edge: the rejection prints the generic
"Only those HTML tags are supported…" message instead of a clear "ANSWER requires --reply-to" — a
minor error-message UX nit, NOT a broken-HTML bug. Lesson: do not assert a root cause you have not
verified in the source.

## rig-cli worktree hygiene incidents (2026-06-17, found while reconciling agent PRs)
Two issues surfaced while the parallel rig-cli agents (PRs #27/#28/#29 + tg-ctl-boot) ran:
1. **Orphaned WIP stash — RECLAIM:** an unrelated `agents_md` symlink-"repair" feature (touches
   `riglib/actions/runner.py` + `riglib/drift.py`) was found uncommitted in a worktree and stashed
   by the excludesfile agent as `stash@{0}: pre-existing-agents-md-repair-and-untracked-WIP`
   (repo-global stash, visible from every rig-cli worktree, NOT lost). This is a real forgotten
   feature — investigate (git log -S) and either finish or land it; do NOT drop it.
2. **Main checkout left on a feature branch:** the live `rig` (`~/.local/bin/rig` →
   `~/xp/rig-cli/bin/rig`, the MAIN checkout) was found on branch `rig-global-excludesfile`, not
   `main` — a parallel agent's isolation worktree contaminated the shared main checkout. Restored to
   `main` (tree was clean, branch safe on origin/PR #29). Watch: agents running with isolation:worktree
   against rig-cli are stepping on the shared checkout / each other (the tmux agent also reported a
   two-agent same-worktree collision). Must verify ~/xp/rig-cli is on `main` BEFORE running ship.sh
   (its post-merge pull targets that checkout).

## tmux session-restore for codex / opencode / commandcode (not just claude) (CTO 2026-06-17, #3972)
cc-save originally filtered `pane_current_command == claude`; tmux-v2 (#28) rewrote it to walk the
pane process TREE for `claude`. EXTEND it so session restore works the SAME for the other agent CLIs:
`codex`, `opencode`, `cmd` (commandcode). Per agent kind: detect its process in the pane tree, capture
the cwd + its resumable session id, and on restore relaunch with that agent's resume syntax (claude
`--resume <id>`; codex / opencode / commandcode each have their own — research each CLI's resume flag).
Generalize cc-save/cc-restore from claude-only to a per-agent-kind map. Builds on #28 (the tmux boot +
cc-save machine). Tests + smoke mandatory; verify live that each agent kind round-trips.

## (DONE via PR #33) dashboard problematic-models badge — built, not a pending ROADMAP item
[36] review dashboard per-model health + problematic-count badge on Models&roles tab — delivered as
DRAFT PR #33 (review-cli), 61 tests + smoke, screenshot badge=4. Tracked on GitHub, not pending here.

## OWED: gh ship must work in every repo via provisioning, not a runtime alias hack (CTO 2026-06-17, #3975)
`gh ship` is a gh alias → `<repo>/.claude/scripts/pr-ship.sh`, which only EXISTS in agent-tools (it
delegates to `ci/ship/ship.sh`). In rig-cli/review-cli/tg-cli the delegator was MISSING → `gh ship`
failed there. STOPGAP applied now: improved the gh alias to fall back to the canonical
`~/xp/agent-tools/ci/ship/ship.sh` when no repo-local `pr-ship.sh` exists (unblocks the merge wave).
DURABLE deliverable still OWED (CTO wants it PR'd + reviewed + merged): rig (or the agent-tools
installer) must PROVISION `.claude/scripts/pr-ship.sh` into every managed repo so `gh ship` works
everywhere on a clean machine — AND handle gitignore so the provisioned file doesn't dirty the tree
(ship refuses a dirty worktree). Then drop the alias fallback. Test + review iterations + merge.

## FOLLOW-UPS from the merge wave (2026-06-17)
- ⏳ **tg-cli #36 merged but NOT deployed:** the lowercase-english `--tag` + help-color landed on tg-cli
  `main`, but the LIVE `tg` is `~/.files/bin/tg` (1.11.0), a separate checkout. `answer`/`decision`/
  etc. won't work in the live tg until ~/.files is synced/rebuilt from tg-cli. Deploy step owed.
- ⏳ **review-cli stray-commit branch:** `salvage-readme-agenttools-fix` preserves local-main commit
  5d7c957 (docs readme fix) that an agent made directly on the main checkout; it appears to DUPLICATE
  already-merged #27-readme (0748b8a). Verify and delete the branch if redundant.
- ⏳ **multi-agent restore (#3972) recorded but NOT started** — codex/opencode/commandcode session
  restore in cc-save/cc-restore. Delegate after the merge wave settles.

## DONE 2026-06-17: merge wave — all 9 PRs landed (CTO "лей" #3974/#3975)
Shipped via `gh ship` (after fixing the alias to work outside agent-tools): review-cli #32/#34/#33,
rig-cli #28/#27/#29/#30, agent-tools #52, tg-cli #36 (--skip-ci for 8 pre-existing CodeQL findings).
#23 closed as superseded by #29. rig-cli #27/#29/#30 each needed a serial rebase onto the advancing
main (all touch the same provisioning files) + codex P2 thread fixes — done by subagents, verified
green (smoke exit 0 + CI) before each ship.
FINDING worth fixing: the CI `review-threads` gate reads GREEN while `gh ship` still refuses for
unresolved codex threads — the two gates use different criteria, so a PR looks merge-ready in CI but
ship blocks. Align them (do X: make the CI review-threads check enforce ship's all-resolved rule,
because Y: green CI currently misrepresents thread state and wasted merge cycles).
FOLLOW-UP: worktree hygiene — prune the merged-branch + /tmp/wt-* worktrees across rig-cli/review-cli
and the salvage branches (salvage-readme-agenttools-fix) now that the wave is done.

## Forward harness confirmation / permission prompts to TG as inline buttons (CTO 2026-06-17)
The agent harness's BLOCKING prompts must MIRROR to Telegram as tappable inline buttons, not only
render in the tmux pane. Trigger that surfaced it: the dynamic-**workflow launch** confirm dialog
("Run a dynamic workflow? 1. Yes, run it / 2. View raw script / 3. No") appeared ONLY in the pane —
a backgrounded/remote run stalls on a prompt the CTO never sees from the phone, or it just auto-runs
without the chance to say no. Scope: detect the harness prompt state (Claude Code workflow-confirm +
tool-permission + plan-approval prompts at minimum; generalize to codex/opencode/etc.), push the
prompt text + its option list to TG as inline buttons, and route the tap back as the selection /
keystroke into the pane (or via the harness prompt API). This EXTENDS decisions-as-buttons
(#3706, tg-cli#30): same inline-button + routed-reply infra, and it MUST honor the pending-question
DEFER (tg-ctl must not blast injected text into a pane that has an open prompt — see tg-cli#30).
Tracking: extend tg-cli#30 (or a new tg-cli issue).

## INCIDENT 2026-06-17 ~09:52: tmux died (spawn-storm, not reboot/OOM) + restore machinery built-but-not-deployed
Read-only forensics. The machine did NOT reboot (uptime ~12h45, boot prev-day 21:18) and did NOT OOM
(~17 GB free, no jetsam on tmux, no `.ips` crash report). The old server (PID 9214) continuum heartbeat
collapsed during a **`tmux`-command spawn-storm ~09:51-09:53** — hundreds of short-lived one-shot `tmux`
invocations, consistent with the **185 leftover `rigtest-*`/`ccdbg-*`/`tgctl-test-*` sockets** in
/private/tmp/tmux-501/. The actual SIGKILL/SIGTERM was not logged, so the exact terminator is unprovable;
strongest circumstantial read = a rig/test loop's tmux-socket churn starved/killed the server. ACTION:
find+stop whatever loops `tmux` (the `rigtest-*` socket factory) and clean the 185 stale test sockets; rig
tests must use throwaway sockets they tear down in a trap.
Why the manual restart restored NOTHING (all already in §5b / "v2 reboot", DOCUMENTED but NOT DEPLOYED):
- 📋 **cc not resumed**: deployed `~/.config/rig/tmux/cc-save.sh` matches `pane_current_command == claude`, but
  a live cc pane reports `2.1.179` (cc's versioned node binary name) -> never matches -> `cc-sessions.map`
  is 0 bytes -> cc-restore has nothing. Fix (in rig's TEMPLATE, not a hand-patch of the provisioned file):
  detect cc by walking the pane pid-tree for the claude/node process, not by command-string. Highest value.
- ⏳ **zsh profile missing in panes**: the heavy profile (zplug, spaceship prompt, gs/gpf aliases, full PATH)
  lives only in `~/.zprofile` (LOGIN shells); `.zshrc` is 4 lines. tmux `default-command ''` spawns
  NON-login panes -> only `.zshrc` runs. Fix: `set -g default-command "$SHELL -l"` in rig.tmux.conf.
- ⏳ **launchd boot = empty server**: `ai.hyperide.tmux-boot.plist` runs bare `tmux start-server` (no session
  -> no conf/plugins/continuum-restore). Point it at a script that does `new-session -d` (rig tmux-boot.sh).
- ⏳ **duplicate boot agent**: a legacy `~/Library/LaunchAgents/Tmux.Start.plist` (continuum
  `osx_iterm_start_tmux.sh`) competes with rig's agent. Unload+remove it. This — NOT an `ln`/`.ln.conf`
  wrapper — is the real "two wrappers"; CORRECT the §5b note: there is NO `ln`/`.ln.conf` on this machine.
- ⏳ **session sprawl**: the manual bare `tmux` bypassed `attach -t main || new -s main` -> 4 sessions (2/3/4/main).
GOOD NEWS: the Moshi `status-right ''` continuum-wipe did NOT bite this time — rig's ordering fix held
(status-right intact under MOSHI_CLIENT=1). All fixes belong in **rig tmux provisioning** (§5b), not hand-edits.
**ROOT CAUSE — RESOLVED 2026-06-17 (corrects the "spawn-storm/unprovable" read above):** a killer subagent
PROVED the mechanism — `tmux kill-server` ends the server process but does NOT unlink the socket file on
macOS, so test fixtures that `kill-server` without unlinking LEAK one socket inode per run. Two leakers:
rig-cli `tests/test_tmux_e2e.py` (fixture `tmux_env`, `rigtest-<uuid>` — 157 of the 166) and tg-cli
`tests/ctl-tmux-integration.test.ts` (`afterAll`, `tgctl-test-<pid>`). The "spawn-storm" I saw WAS these
fixtures churning sockets. FIX (PRs open, NOT merged): rig-cli **#31** + tg-cli **#37** — teardown now kills
the server AND unlinks the socket, + a regression test asserting no leak. Tests green (rig-cli 660 + 6 e2e,
tg-cli 1015 bun); reviews caught+fixed 2 real bugs (env-mismatch socket path, kill-timeout skips unlink).
166 stale sockets cleaned to 0; live session intact.
Follow-ups: (a) rig-cli leak regression is gated behind `RIG_TMUX_E2E=1` + GitHub-reachability -> decouple
into a tmux-only no-network check; (b) `ati-test-*` (14) + `ccdbg-*` (3) debris has NO source in any managed
repo -> trace which tool makes them so it gets the same kill+unlink fix; (c) prune worktrees after merge
(`~/xp/rig-cli-worktrees/fix-tmux-socket-leak`, `~/.files/repos/tg-cli-wt-socketleak`).

## require-review-before-commit hook is too broad + ignores the env bypass (found 2026-06-17)
The `require-review-before-commit` agent-hook blocks a commit when no fresh review marker
(`~/.cache/agent-tools/last-review`, mtime-windowed) exists — but: (1) it fires on a pure **docs-only**
change (e.g. a ROADMAP.md edit), where the project rule explicitly allows skipping review; (2) it also
catches `git stash` (any persisting git op), so you can't even park a docs edit; (3) it does NOT honor
`REVIEW_MARKER=...` passed as INLINE ENV on the git command — the hook reads the marker
FILE / its own process env, not the to-be-run command's env, so `REVIEW_MARKER=x git commit` is ignored
(the error message advertises `REVIEW_MARKER` as if inline would work). Net: a docs commit forces either a
full multi-model `review` run (heavy, and a crash-risk when other agents are already reviewing) or a touch
of the GLOBAL marker (which then lets concurrent code agents' commits skip the gate inside the 1h window).
Resolved direction: detect docs-only diffs and allow them; keep self-service review bypasses removed;
use explicit external hatch approval for real exceptions in the pre-bash agent hook; keep the global
git review-gate stricter (review stamp or docs-only fast path, no self-service bypass); and don't gate
`git stash`/`git worktree` (non-commit ops). Pairs with the "docs-only OK to skip review" rule.
Tracking: agent-hooks + global git-hooks (this repo).

## rig setup — interactive config wizard (CTO 2026-06-17)
`rig setup` is the INTERACTIVE wizard. It (1) SHOWS what is currently enabled/configured — the live
state across ALL reconciled areas (skills, agent-hooks, git-hooks, CI, MCP, harness/auto-mode, repo
settings, tmux, model-cron — the same areas `rig status` covers), and (2) lets you CHANGE anything in
BOTH the local (`rig.yaml`) and global (`~/.config/rig/config.yaml`) config from inside the wizard, then
APPLY (`rig apply`) so the change takes effect on the spot. Each option carries an inline HINT explaining
how it works and why it's needed (the "why" sits next to the toggle, not buried in docs).
NON-interactive `rig setup` (no TTY / piped / non-interactive run) just prints USAGE help for the core
commands: `init`, `apply`, `config get|set` — it degrades to a help/pointer, it does NOT run a
half-wizard. (Pairs with `rig config get|set` and the rig.yaml JSON-schema work in §5 — the wizard reads
the schema for the option list + hints. This REFINES/REVERSES the earlier "delete the `rig setup` alias"
item above: `rig setup` is no longer an alias, it IS the wizard; `init` (onboarding front door) and
`apply` (reconcile) stay distinct commands.)

## MISSING gate: "mandatory/relevant skills were READ before work" (CTO 2026-06-17)
There is NO hook that verifies an agent actually READ the mandatory + task-relevant skills before it
starts working. Today skill-loading is purely advisory — the frontmatter `description` triggers + the
SessionStart blurb SUGGEST skills, but NOTHING enforces that the universal-mandatory skills
(delegate-work-to-subagents, visual-proof-cycle) or the task-relevant ones were invoked before the agent
acts/commits. Need a GUARD (a new `agents-hooks/v1` point — e.g. a pre-work / first-substantive-tool gate,
carried into Claude Code via the `cc_hook_bridge`, same as the other gates) that checks the required
skills for the context were invoked this session/task and BLOCKS or warns otherwise. Design questions to
settle: what counts as "read" (Skill-tool invocation this session vs this task), which skills are
mandatory-always vs relevance-triggered (and how relevance is detected), and warn-vs-block tier. Pairs
with the skill-loading work (§9 — skills must actually LOAD) and the require-review-before-commit gate
(same hook-bridge carrier). Tracking: agent-hooks (this repo).

## DOCTRINE: enforce correct agent behavior with PreToolUse hooks (rig-provisioned), not prompts/promises (CTO 2026-06-17)
**Root lesson from this session:** behavioral rules that live only in prose (CLAUDE.md, AGENTS.md, skills,
"I'll do better going forward") REGRESS — the agent forgets or rationalizes them within a few turns. The
CTO repeated the SAME corrections many times in one session (orchestrate-only; subagents in the
BACKGROUND; no long inline processes) because nothing ENFORCED them. A behavioral rule with no mechanism
is a promise, and promises regress. The fix is structural: encode each rule as an `agents-hooks/v1`
**PreToolUse** hook (carried into every harness via `cc_hook_bridge`), provisioned UNIVERSALLY by rig
(rig.yaml agent-hooks layer, default-on), self-dogfooded here. Build these, each its own descriptor under
`agent-hooks/<name>/`:

1. **background-subagent gate** (PreToolUse on the Agent/Task dispatch tool). If the ORCHESTRATOR launches
   a subagent WITHOUT `run_in_background: true` (and it isn't a trivial one-liner), BLOCK (exit 10) with a
   3-part reminder: "the orchestrator must dispatch subagents in the BACKGROUND — set
   `run_in_background: true` or use a dynamic Workflow; a foreground subagent blocks the main thread." This
   is the CTO's specific ask: detect that the main agent is about to launch a subagent NOT in the
   background and remind/block with the right way. Detect: `tool in {Agent, Task}` and `run_in_background != true`.
2. **orchestrator-stays-thin gate** (PreToolUse on Edit/Write/Bash, MAIN thread only). When the main thread
   attempts non-trivial work itself — a code Edit/Write (non-docs), or a multi-step/long Bash — WARN then
   BLOCK with "delegate to a subagent; the orchestrator plans, dispatches, verifies — it doesn't implement
   inline." (delegate-work-to-subagents, enforced.)
3. **no-long-inline-process gate** (PreToolUse on Bash, MAIN thread only). Detect long-running commands in
   the orchestrator (`review`, `* --watch`, `gh pr checks --watch`, build/test suites, `sleep`) and BLOCK:
   "run this in a background subagent, not the orchestrator." Pattern-match the command.
4. **skills-read gate** — the separate "MISSING gate" item above (verify mandatory/relevant skills were
   invoked before work).
5. **visual-proof gate** — block a commit / "done" claim on a user-visible change with no attached
   screenshot the agent actually looked at (visual-proof-cycle, enforced).

**Fighting the RIGIDITY (equally important — a gate that can't be satisfied is as bad as no gate):** the
require-review-before-commit hook is this session's cautionary tale — its marker was UNSATISFIABLE
(nothing wrote it) and it gated even docs/`git stash`, so it forced agents to either STALL or "forge" the
marker. So every enforcement hook MUST be: (a) **satisfiable** by an honest action (and that action must
exist + be wired — e.g. `review` must actually write its marker); (b) **tiered** — warn before block where
feasible, so a brittle rule doesn't wedge the whole loop; (c) **scoped** — SUBAGENTS are EXEMPT from
gates 1-3 (a subagent legitimately does the work + runs review + ships); this needs a reliable
main-thread-vs-subagent signal (an env var the harness/bridge sets); (d) **honest escape hatch** — a
real, auditable skip (a commit trailer / documented flag the hook reads), never a silent forge. rig
provisions all of them via the agent-hooks layer + `cc_hook_bridge`; `rig status` reports drift; they are
default-ON so a fresh machine inherits the enforcement. Tracking: agent-hooks (this repo) + rig provisioning.

## MISSING universal-mandatory skill: extract lessons from process mistakes + user complaints (CTO 2026-06-17)
There is NO skill that mandates SELF-LEARNING — turning a process mistake or a user
complaint/correction into a durably-recorded lesson. Checked: the 38 universal skills include
`promise-durable-action` (turn a promise into a mechanism — that's the DOWNSTREAM step) and
`task-completion-selfcheck` (end-of-task reflection), but NOTHING covers the upstream act of NOTICING a
recurring failure / a user's repeated correction and EXTRACTING + recording the generalizable lesson. The
proof it's needed is this very session: the CTO had to repeat the same corrections many times
(orchestrate-only; subagents in the background; no long inline processes) because the lesson was never
captured — it stayed an apology, not a durable change.
**Author a new universal-MANDATORY skill** (e.g. `learn-from-feedback` / `capture-lessons-durably`,
skills/universal/, English-only, follow writing-skills, GREEN-verified). Trigger: ANY user
complaint/correction, OR a self-noticed process failure (a repeated mistake, a tool that didn't behave as
the user expected, a workflow that failed the same way twice). Mandate: do NOT just apologize and fix the
instance — (1) name the ROOT cause, (2) extract the GENERALIZABLE lesson, (3) durably record it in the
right place: a `MEMORY.md` memory file for cross-session/behavioral lessons, a ROADMAP/ticket entry for a
project process gap, and — if it's a behavioral rule — escalate it to ENFORCEMENT (a hook/skill/config
change) per the doctrine above. It is MANDATORY-ALWAYS (every agent, every session), in the same tier as
`delegate-work-to-subagents` / `visual-proof-cycle`. Pairs with `promise-durable-action` (the mechanism
step it feeds into), `task-completion-selfcheck`, and the enforcement DOCTRINE above. ENFORCEMENT hook
(ties into that doctrine): after a user message that reads as a correction/complaint, a PreToolUse/Stop
nudge "extract + durably record the lesson before moving on" — so self-learning itself isn't just another
prose promise. Tracking: skills/universal + agent-hooks (this repo).

## Model-fallback skill + cross-harness hooks: escalate on repeated main-model errors (CTO 2026-06-17)
When the MAIN model hits REPEATED errors (rate-limit / overload / API 5xx — e.g. today's transient
"temporarily limiting requests" throttle), the agent must AUTO-FALL-BACK down a priority chain instead of
stalling or dying:
> **`claude:fable` -> `claude:opus` -> `oc:GLM-5.2` -> `codex:gpt5.5`**

Notation `<harness/provider>:<model>`: fable then opus within Claude, then GLM-5.2 via opencode, then
GPT-5.5 via codex — the chain deliberately CROSSES HARNESSES so a whole-provider outage (like today's
Anthropic throttle) doesn't wedge the work. Build BOTH:
- 📋 **A skill** (universal-mandatory) defining the chain + the fallback discipline: on the Nth repeated
  model error, drop to the NEXT entry; surface which model is now active; return to the top when the
  preferred model recovers. Mandatory so every agent inherits resilient execution.
- ⏳ **Supporting hooks** that make it AUTOMATIC, not manual: an error-detection hook (counts repeated model
  errors per task) that, past a threshold, triggers the switch — within a harness it's a model swap
  (`claude:fable`->`claude:opus`); across the harness boundary it RE-DISPATCHES the unit of work to the
  next harness as an executor (shell out to `opencode` / `codex exec` — today's codex-as-executor pattern,
  generalized + made reliable, since codex exec exit-0 lies about completion).
- ⏳ **CROSS-HARNESS — must work in cc AND codex AND oc AND pi**, not only Claude Code. Each harness has its
  own model-selection + error-surfacing mechanism, so the error-hook + switch mechanism is provisioned PER
  harness (cc via settings hooks + `cc_hook_bridge`; codex via its hooks/config; opencode + pi likewise),
  all reconciled by rig from ONE chain definition (in `rig.yaml` / the providers manifest) so every harness
  reads the same priority order.
Builds on the shared `providers` lib (§3 — it already owns board/failover/`oc:`-routing/key-cascade; this
is the agent-facing trigger + the cross-harness wiring on top). Pairs with the transient-rate-limit lesson
(retry-then-fallback, NOT reduce-fan-out) and the enforcement DOCTRINE (hooks, rig-provisioned,
cross-harness). Tracking: skills/universal + agent-hooks + lib/providers + rig provisioning.

## CI/tooling follow-ups from the 2026-06-17 ship wave (found by the serial shippers)
- ⏳ **`dependency-review` preflight is flaky — one-line fix** (agent-tools `ci/dependency-review/workflow.yml`,
  the source-of-truth drop-in copied into every repo). The SBOM-probe step runs under GitHub's default
  `bash -e {0}`; `code="$(gh api .../dependency-graph/sbom -i | awk …)"` exits non-zero on ANY non-200 — the
  exact "GHAS off / transient" case it's meant to gracefully detect — and `-e` then HARD-FAILS the whole step
  instead of skipping. It flaked a real ship (tg #33, passed on rerun). Fix: append `|| true` to the probe
  pipeline (and/or explicit `shell: bash` without `-e`). One line, but it lands in EVERY repo using the
  drop-in → real CI-flakiness reduction. (do X because Y: harden the probe because a transient non-200 SBOM
  response currently red-fails CI and blocks ships.) Dedicated PR.
- ⏳ **`review` CLI shim can't import `reviewlib` in agent/subagent shells** — `/opt/homebrew/bin/review` fails
  to import `reviewlib` (it lives in `~/xp/review-cli`, not on the shim's sys.path); even an explicit
  PYTHONPATH run produced no output. A shipper had to fall back to the then-sanctioned manual bypass (tests /
  secrets / leftover gates still ran). This makes the review-before-commit gate effectively non-functional in
  those shells and is the deeper reason the gate fought us all session (the #36 marker-write fix only helps
  when `review` actually RUNS). Investigate the shim's path resolution / install model
  (`single-file-live-symlink-cli` + `no-npx-direct-binary`) so `review` runs from any shell. Pairs with the
  require-review-before-commit + model-fallback items. Dedicated investigation + fix.

---

</details>

## ✅ Done (2026-06-18) — enforcement-doctrine + review-resilience wave landed; CTO #4133 migration COMPLETE

*All PRs below verified MERGED on each repo's `origin/main` via `gh pr view <N> -R <repo> --json
state,mergeCommit` (three repos — the `-R` matters). Snapshot taken 2026-06-18.*

### agent-tools

- ✅ **#61** (`26136cb`) — `no-shell-file-edit` PreToolUse gate: blocks in-place stream-edits (`sed -i`,
  `perl -i`, in-place `awk`) and shell-redirect overwrites onto a tracked source, forcing real Edit/Write.
- ✅ **#62** (`8050a58`) — `decision-request-discipline` skill + advisory `tg`-decision self-check hook
  (the CTO-decision-request half of the #4133 migration).
- ✅ **#63** (`e6f7639`) — `require-review-before-commit` hardened: **docs-only skip**, per-commit skip
  trailer, parser-scoped skip-flag — closes the "too-broad gate" complaint + the #20 raw-string bypass.
- ✅ **#64** (`c01b5e1`) — `ship.sh` hardened against exit-128 on multi-worktree checkouts + made the
  post-merge cleanup non-masking (this is the exit-128 fix only; the broader `ship.sh` self-cleanup work
  is a SEPARATE, still-in-flight item — see Open follow-ups below).
- ✅ **#65** (`92b889b`) — evidence-first `ticket-documentation-standard` folded into
  `strict-ticket-discipline` (the ticket-doc-standard half of #4133).
- ✅ **#66** (`ede5bb9`) — migrated the `timeout-evil` + `schematic-via-codex` doctrine into the
  test-discipline + visual-proof-cycle skills (the development.md residual of #4133).

### review-cli

- ✅ **#46** (`132233a`) — in-seat `--retry` (retryable vs seat-fatal classification before reserve-replace),
  scoped to diff mode.
- ✅ **#47** (`44232c0`) — hardened `_ensure_opencode_readonly_agent`: validate/rewrite a permissive global
  opencode agent so a stray write-capable config can't leak into a read-only review (security).
- ✅ **#48** (`585b08b`) — dashboard JS unit tests (`resolveModel`/`filteredRuns`) + hardened the
  lib-absent smoke path (exit-4).

### rig-cli

- ✅ **#50** (`c0d3b15`) — tmux migration neutralizes pre-existing rig-owned plugin/continuum/resurrect
  init lines so a second `rig apply` can't double-initialize them.

### 🎯 CTO request #4133 — migrate hyperide docs/rules as ENFORCED mechanisms — COMPLETE

The brief: don't copy hyperide's `docs/rules` as prose, encode them as enforced mechanisms. Status:

- 📋 **cto-decision-requests → DONE via #62** (decision-request-discipline skill + advisory hook).
- 📋 **ticket-documentation-standard → DONE via #65** (evidence-first standard inside strict-ticket-discipline).
- 🔬 **development.md → DONE** — the bulk already landed on `main` (the tamper-proof `ci/pr-checklist` gate,
  the 42 universal skills, and the enforcement agent-hooks), with the `timeout-evil` + `schematic-via-codex`
  residuals closed by **#66**.
- ⏭️ **Intentionally NOT migrated** (project-specific to hyperide, not universal ecosystem doctrine): Linear
  ticket-naming conventions, hyperide `bun` command recipes, the hyperide release flow, and the
  `ext-test-projects` debug workflow. These stay in the product repo where they belong.

### Open follow-ups carried forward (genuinely open — lower-value, not this session's blockers)

These survive the wave and stay open:

- ⏳ **oc provider-auth probe** + **tg-cli `.gitignore`** hygiene (small).
- 🔮 **rig smoke — real-catalog CI** (in flight).
- ⏳ **review-gate / review-tool: pinned `git diff` vs `rtk` fragility** — the marker re-derives
  `git diff --cached`; an rtk-intercepted diff can make the stamp check misread. Pin to a real `git`.
- ⏳ **rig-cli `core.bare=true` forensics** — investigate the bare-repo edge that tripped worktree ops.
- ⏳ **review-cli CI: ruff + ~12 latent errors** to fix-or-justify.
- 📋 **continuum autosave startup-race** — watch (the status-right ordering fix held, keep an eye on it).
- ⏳ **opencode permission-schema floor** — watch (a schema-version regression could re-open the #47 hole).
- 🔮 **ship.sh self-cleanup** (in flight — a concurrent agent owns `ci/ship/ship.sh`; distinct from the
  #64 exit-128 hardening above, this is the broader post-merge self-cleanup pass).
- 🔮 **review-cli spec-web autosave race #30** + **stale `DEFAULT_MODELS` #25** (in flight).
- ⛔ **HOLD-for-CTO:** `tg` deploy / inline buttons / spawning new PRs — parked pending CTO direction
  (ties into the new forum-topics item below).

### ➡️ NEW (CTO 2026-06-18) — tg-cli / tg-ctl: Telegram forum-topics mode — one topic per agent

Replace the current flat `/agent` phonetic selector with **persistent per-agent Telegram forum topics**.
Each agent runs in its OWN forum topic (`message_thread_id`):

- ⏳ **Topic creation spins up an agent.** A `forum_topic_created` update triggers the existing `/new` flow —
  clarifying the working **PATH** + **MODEL** for that agent — and **binds topic → agent**.
- 🔮 **Routing is per-topic.** Inbound messages in a topic route to that topic's agent; the agent's replies
  post back into the **same** topic. No more phonetic disambiguation — the topic IS the address.
  — 🔬 review (tg-cli PR #54): the ROUTING half (topic→pane map + inbound route + outbound
  `message_thread_id`) is MERGED; 🔬 PR #61 adds `tg --topic` / `TG_TOPIC` outbound threading
  (increment 2). The topic-creation→agent-spawn binding (`forum_topic_created` → `/new`) is still open.
- 📋 **Wiring:** ties into the HOLD `tg /new` feature (interactive PATH/MODEL selection via inline buttons,
  tg-cli#27) and tg-ctl routing (`discover.ts` / `routes.ts` — the topic→agent map lives here).
- ⛔ ⚠️ **NEEDS CTO CONFIRMATION before building:** the request referenced an attached screenshot
  (a "mirror this existing UX" pointer) that **did not arrive**. The exact `/new` clarification UX to
  mirror must be confirmed with the CTO before implementation — do not guess the flow.
- 📋 **Tracking:** tg-cli (new issue, or extend tg-cli#27 `/new`) + tg-ctl routing.

## ✅ Done (2026-06-19) — security-bypass follow-ups + review-cli lint gate + worktree sweep

*All PRs verified MERGED on each repo's `origin/main`; the block-no-verify fixes re-probed against the
merged hook (feeding `{"args":{"command":…}}`, the shape the hook actually reads). Snapshot 2026-06-19.*

- ✅ **agent-tools #71** (`0f6da0e`) — block-no-verify: close the `sudo -u git git commit --no-verify`
  value-flag bypass (the `-u` value `git` was mis-read as the executable) **and** the symmetric
  over-block of benign commands carrying a `commit`/`mergeCommit` substring. Post-merge probe 8/8.
- ✅ **agent-tools #72** (`85d4603`) — block-no-verify: gate `runuser`'s `-u user [--] command`
  direct-exec form (the sudo sibling), value-flags from authoritative man7. The su-compatible
  positional form passes args to the shell (not a direct exec) so it is intentionally left ungated —
  caught by the gate-review after an over-gating first revision was reverted. 4 review rounds.
- ✅ **review-cli #51** (`72766eb`) — add a pinned-`ruff` CI `lint` job (none existed) + fix the 13
  latent errors + add `--retry` to `_VALUE_TAKING_OPTS` (with a behavioral pre-scan test). CI lint job
  green; post-merge `ruff check .` clean.
- ✅ **Resolved by investigation — no code change shipped (analysis recorded on the tasks):**
  - 🔬 *ship.sh per-worktree dirty-check (#38)* — already safe: removal happens only in `cleanup()`, which
    gates `--force` on `git status` rc==0 AND empty; the preflight conflation removes nothing. A blanket
    preflight abort was tried and **reverted** (it regressed the "broken worktree must not block merge" test).
  - 🔬 *review-gate ↔ review-tool `git diff` pin (rtk fragility)* — already pinned: both sides use the
    identical `git diff --no-ext-diff --cached`; the agent-tools hook is mtime-based (no diff hash);
    review-cli execs git via subprocess (rtk's shell rewrite never fires). Not a live risk.
- ✅ **Orphan-worktree sweep** — rig-cli 8→5, review-cli 2→1. Removed 3 confirmed-merged orphans (patches
  backed up); **kept 6 worktrees holding genuine UNSHIPPED work** (`feat/resumable-sessions`,
  `feat/gh-repo-settings-autonomous`, `feat/rig-github-provision-autonomous` [17 uncommitted files],
  `rig-gitignore`, `fix/tmux-socket-leak-regression-hermetic`) — these need CTO triage (finish vs abandon),
  NOT an orphan sweep.
- ✅ **agent-tools #73** (`0229a0e`) — ROADMAP archival: folded the 06-15→06-17 transcript into collapsed
  `<details>` (additions-only, content preserved) so the file reads as a roadmap, not a transcript.
- ✅ **tg-cli #44** (`790a562`) — **tg-ctl "Claude Code not in tmux" daemon bug, fixed + DEPLOYED.** The
  launchd daemon intermittently read an EMPTY `tmux list-panes` (exit 0, no panes — a momentary connect to
  the wrong/empty server), silently degraded EVERY inbound message to the misleading "not in tmux" reply
  and idle-exit-thrashed every 30 min, despite live agent panes (discovery code + env verified correct;
  unreproducible from a TTY). `panesWithRetry` retries ONLY that flake (exit-0+empty), never a non-zero
  exit (no-server steady state — would block the daemon ~240ms/msg). 8 unit tests, 4 review rounds.
  Deployed: installed checkout `~/.files/repos/tg-cli` refreshed, daemon restarted, live `discovery=ok`.
- ✅ **tg-cli #45** (`2157f09`) — **forum-topics mode, FOUNDATION (increment 1/4)** (CTO "делай", no
  screenshot needed). Spec + pure core (types, binding store, lifecycle state machine, model catalog,
  Cyrillic-aware slug) + leak-safe `stepUpdates` routing behind `topicsEnabled` (default OFF). 29 tests,
  6 review rounds. Increments 2-4 (the entrypoint executor: agent spawn + `/new` button UX + per-topic
  routing + outbound threading) remain — the spec is the contract. See task #31.

### Still open after 2026-06-19 (each assessed, deferred with cause)

- ⏳ **review-cli per-provider opencode auth probe** — `backend_available()` only checks the `opencode`
  binary, not commandcode/zai provider auth. Fix is delicate (a false-negative prunes good seats from the
  review pool); needs opencode's auth-storage semantics + a configured/unconfigured test host to verify.
- ⏭️ **runuser su-positional form** — ungated by design (args go to the shell, not exec); best-effort, like
  `unshare`/`nsenter`/`bash -c`.
- 📋 **Watches:** opencode permission-schema floor — observational. (The continuum/CC-outside-tmux flap that
  fed the autosave watch is addressed by the tg-ctl #44 discovery-resilience fix above.)
- ⛔ **forum-topics increments 2-4** — the executor (agent spawn + `/new` path/model button UX + per-topic
  routing + outbound threading + lifecycle polish). Foundation shipped (#45); the spec is the contract.
  Awaiting CTO go-ahead to build increment 2 now (it spawns agents + drives a UX — confirmed before building).
- ⏳ **block-no-verify fail-closed over-block (#40)** — a benign non-git command with unparseable quoting
  (a `tg` report with an HTML body) fail-closes; do a cheap "is there a git commit/push at all?" pre-check
  before failing closed. Caught live this session.
- ⛔ **6 kept worktree branches** (above) — CTO triage.
