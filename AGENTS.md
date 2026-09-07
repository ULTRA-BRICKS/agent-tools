# AGENTS.md — agent-tools

This repo is the portable **rule/guard catalog** for AI-assisted development — a library of
skills, agent-hooks, git-hooks, CI gates, and MCP slots, distilled from practice and
generalized so they apply to *any* project, language, or harness. It is **not** an installer
and has **no runtime of its own**: the directories here are *content*, consumed through one
front door — [`rig`](https://github.com/alex-mextner/rig-cli) (the separate `rig-cli` repo) —
which reads a committed `rig.yaml` and wires the catalog into two distinct targets at once:

- **a repo** — via committed artifacts: git-hooks (or the global dispatcher), CI workflows in
  `.github/workflows/`, MCP registrations, and the `rig.yaml` itself.
- **a dev machine / agent harness** — via the machine layer: skills dropped into the harness's
  skills dir, agent-hooks installed into its hook dir, the global git-hook dispatcher, the
  model-freshness cron.

The same catalog feeds both. A `skills/universal/*` entry lands in your harness; a `ci/<slot>/`
workflow lands in a repo's `.github/`. agent-tools is the **what**; `rig` is the **how** — and
`rig` lives in another repo, so nothing in *this* tree imports or runs `rig`.

The single most important thing for an agent landing here: **you are editing a content
catalog, not an application.** There is no `main()`, no entry point, no `bin/rig` in this
repo. The "behavior" is what `rig` does *with* these files elsewhere. Keep that frame and the
rest of this doc makes sense.

---

## Orchestration doctrine: the main agent only orchestrates

The main/orchestrator agent **only orchestrates**: it **PLANS**, **DISPATCHES** work to
subagents/workflows, and **VERIFIES** results. It does **not** implement inline. **All**
implementation work — fixes, features, builds, investigations, migrations — runs in
**subagent swarms** (the `Agent` tool or a Workflow), never in the main thread. Fan out
broadly and in **parallel**, grouped **by repo** (one shipping agent per repo at a time) to
avoid worktree/ship collisions, and parallel **across** repos. Each dispatched agent carries
the full discipline: **ticket → fresh worktree → tests → review-gate (`[ok]`) → `gh ship`**.
The orchestrator **verifies every "done"** — PR merged **and** CI green, never trusting a
subagent's "all green" on its own word — and marks a roadmap item done only after it is
verified **and** the CTO has confirmed it's OK. This is enforced mechanically by the
`orchestrator-stays-thin` agent-hook, which blocks the main agent's implementation-shaped
Bash/Edit/Write while exempting subagents (see [Agent-hooks](#agent-hooks-the-agents-own-mid-session-gates)
below). The hook is the mechanism; this section is the intent.

---

## How the catalog is discovered: directory convention, zero registration

There is no central registry, manifest, or index file you edit to "add" a catalog item.
`rig` scans this tree live and an item *is* a directory at a known path. **Drop a directory
in the right place and it becomes a catalog item** — that is the whole registration story.

| Carrier | Convention | What a new directory must contain |
| --- | --- | --- |
| Skill | `skills/universal/<name>/SKILL.md`, `skills/by-type/<group>/<name>/SKILL.md`, or `skills/by-stack/<l1>/<lang>[/<framework>]/<name>/SKILL.md` | one `SKILL.md` with `name` + `description` frontmatter (the trigger), the portable rule, rationale, a generic example |
| Agent-hook | `agent-hooks/<name>/` | a `<id>.<point>.json` descriptor + the executable it points at + `README.md` |
| CI gate | `ci/<slot>/` | a `README.md` + either a `workflow.yml` (most slots) or a shell script (a client-side gate like `ci/ship/ship.sh`, which is a merge command, not a GitHub workflow) |
| Git-hook | `git-hooks/<hook>` (`pre-commit`, `commit-msg`, `pre-push`, `no-secrets-scan`, `lefthook.yml`, `global-dispatcher/`) | the copyable hook script |
| MCP slot | `mcp/<name>/` | the slot's config + `README.md` (see the MCP-vs-CLI policy first — the default is *don't add one*) |
| Lib module | `lib/<module>/` | an importable Python package (often its own `pyproject.toml` — see below) |

The `<group>` axis for by-type skills is fixed:
`bot`, `backend`, `frontend`, `cli`, `library`, `infra`, `monorepo`. A skill that applies to
*every* project goes in `universal/`; one scoped to a project shape goes under its `by-type/`
group.

The **third axis is `by-stack/`** — the stack-preset curation axis. Its path IS the stack
path: `by-stack/<l1>/<lang>[/<framework>]/<name>/SKILL.md` (minimum depth `<l1>/<lang>`; a
skill placed directly under `<l1>` with no `<lang>` is ignored). A repo declares a stack (e.g.
`mobile/swift/swiftui`) and inherits every by-stack skill whose stack path is a **prefix** of
it — so a lang-level skill at `frontend/ts/` is inherited by every `frontend/ts/*` stack, and a
framework-level skill at `frontend/ts/react/` only by React repos. Place a skill at the *shallowest*
prefix that honestly applies, and keep its `description` scope no broader than that prefix (a skill
under `frontend/ts` must not claim to cover backend TS — a `backend/ts` stack never inherits it).
`by-type/` and `by-stack/` are complementary: `by-type` selects by project *shape*, `by-stack`
by declared tech *stack*.

**Non-obvious:** because discovery is by location, the catalog grows with **no code change in
`rig`**. You add a directory here, the consumer re-runs `rig apply` in their repo, and the new
item flows in. Conversely, *moving or renaming* a directory is a breaking change to its
identity — a skill's path, a hook's `<id>.<point>.json` name, and a CI slot's directory name
are the stable handles `rig.yaml` and the harness key on.

---

## The declarative / reconcile model (it lives in `rig`, acts on this catalog)

`rig` treats your repo + machine like Terraform treats infra: `rig status` computes the
**two-way drift** between what `rig.yaml` declares and what is actually on disk; `rig apply`
**reconciles** — it builds the set of intended actions, diffs against current state, and
applies only the delta (idempotent; backs up on conflict). The plan/drift/reconcile *engine*
(the `_build_*` action emitters and `_do_<kind>` handlers) is **in `rig-cli`, not in this
repo** — so do not go looking for `plan.py` / `drift.py` / `runner.py` here. What you control
from *this* repo is the catalog the engine reconciles toward.

Practical consequence for an agent: you never "install" a skill or a hook by hand in a
consumer repo. You add/edit content here, and the declarative pass in `rig` converges the
target. If a change isn't showing up, the question is "did `rig apply` run against an enabled
item in `rig.yaml`?" — not "did I copy the file to the right place?".

---

## Agent-hooks: the agent's OWN mid-session gates

Agent-hooks are the non-obvious bit of this repo. They are **not** git hooks and **not** CI.
They are out-of-process guards that intercept *the agent's own tool calls* — a Bash command, a
file write, the end of a turn — **before the side effect happens**, and can deny it. A git hook
fires too late (at commit); a CI check fires far too late (after push). Only an agent-hook can
stop a `--no-verify` commit (the very thing being skipped) or stop a secret from being written
to a file in the first place.

The contract is `agents-hooks/v1`: a descriptor (`{id, point, cmd (ABSOLUTE), priority,
timeout_ms, on_error}`) names an executable that reads a JSON event on stdin and signals via
exit code — **`0` allow · `10` BLOCK (canonical even if stdout is malformed) · any other →
the descriptor's `on_error` policy** (`open` = warn-and-proceed, `closed` = deny on failure).
`cmd` must be absolute; installers (i.e. `rig`) substitute the real path for the shipped
`/ABSOLUTE/PATH/TO/...` placeholder.

The shipped hooks and their points:

| Point | Hooks |
| --- | --- |
| `pre-agent` **(live in CC and opencode once rig registers their matchers; NOT mapped in Codex yet)** | `background-subagent-gate` (orchestration doctrine: block a non-trivial FOREGROUND subagent dispatch; subagent-exempt) |
| `pre-bash` | `block-no-verify` (fail-closed), `block-raw-pr-merge`, `block-reset-hard` (fail-closed: blocks `git reset --hard` and `git clean -f...`/`-fd`/`-fdx` — irreversible working-tree wipes with no undo; deny-by-default, no self-service bypass — an optional repo-owner `agent_hooks.approval_cmd` in rig.yaml is the only override, Alex tg#6554), `pkill-guard` (fail-closed: blocks a PATTERN-based kill — `pkill -f`/`killall`/`kill $(pgrep ...)`/`pgrep | xargs kill` — of a shared/ambiguous process name like `node`/`codex`/`review diff`; allows `kill <pid>` and any session-scoped pattern; deny-by-default, `RIG_HATCH_REQUEST_PKILL_GUARD` Telegram hatch only; retrospective gap G-5), `pin-primary-worktree` (per-repo `worktree_only` opt-in: blocks a `git checkout`/`switch` that would move the repo's PRIMARY worktree off its default branch — the pre-bash complement to `worktree-only-writes` below; deny-by-default, `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE` Telegram hatch only), `block-devserver-primary` (same `worktree_only` opt-in: blocks launching a dev server / dev-watch process — `npm run dev`, `vite`, `next dev`, ... — while the effective cwd sits on an enrolled repo's default branch; a dev server's own writes bypass Edit/Write entirely, so this is the Bash-launch counterpart neither `worktree-only-writes` nor `pin-primary-worktree` can see; deny-by-default, `RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY` Telegram hatch only), `require-review-before-commit`, `require-ticket-before-commit`, `enforce-timeout-on-bash`, `orchestrator-stays-thin` (impl-bash, warn→block, subagent-exempt), `no-long-inline-process` (review/--watch/build-test/long-sleep, subagent-exempt), `subagent-no-bg-longproc` (the INVERSE: block a SUBAGENT from BACKGROUNDING a *labeled* long process — `run_in_background:true`/`&`/`setsid` on review/--watch/build-test/long-sleep. The precise mechanism (agent-tools#546): a Bash `run_in_background:true` child IS harness-tracked and DOES resume its calling subagent, so an ORDINARY non-labeled command backgrounded that way is allowed; a Monitor watch and a shell-DETACHED job (`&`, `nohup … &`, a forking `setsid`) never resume a subagent and a `--watch` loop never exits — those wedge it forever. Whether a bounded labeled process under `run_in_background:true` should keep being blocked is a scope follow-up (agent-tools#559), not folded into the wording fix; subagent-ONLY), `no-shell-file-edit` (block `sed -i`/`perl -i`/`gawk -i inplace` or a `> file` redirect editing a tracked source file; parsed not raw-matched; NOT subagent-exempt), `skills-read-gate` (mandatory skills before work, warn→block), `visual-proof-gate` (block a UI commit with no looked-at screenshot), `decision-request-format` (graduated: on a `tg --tag decision`/`--tag problem` send, self-check the body for Context/Options/Recommendation per the `decision-request-discipline` skill — a complete body passes silently, a partial one gets an advisory, a genuinely BARE body is blocked with a re-send skeleton, `RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT` Telegram hatch only; the removed `question` tag is blocked outright with a redirect to `--tag decision`, no hatch (#524); parsed not raw-matched; NOT subagent-exempt) |
| `pre-write` | `block-secrets-write`, `block-raw-process-env`, `orchestrator-stays-thin` (non-docs code Edit/Write, warn→block, subagent-exempt), `worktree-only-writes` (per-repo `worktree_only` opt-in: blocks an Edit/Write while the checkout sits on the repo's default branch, redirecting to a separate worktree; deny-by-default, `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES` Telegram hatch only) |
| `pre-skill` **(live in CC once rig registers the `Skill` matcher; NOT mapped in Codex/opencode yet)** | `skills-marker-writer` (touches the freshness marker `skills-read-gate` reads — ADVISORY, never blocks) |
| `pre-monitor` **(live in CC once rig registers the `Monitor` matcher; Codex/opencode have no Monitor-equivalent tool, so this point is CC-only)** | `subagent-no-monitor` (block a SUBAGENT from calling Monitor AT ALL — Monitor has no harness-tracked child and no foreground mode, unlike an ordinary backgrounded Bash command (the same mechanism the `subagent-no-bg-longproc` entry above states, agent-tools#546), so there's no foreground/background axis to classify; subagent-ONLY) |
| `post-write` | `format-on-write`, `lint-on-write` (react to the completed write; exit-10 is feedback because the write already landed) |
| `stop` | Runs in ascending priority order, first BLOCK short-circuits the rest: `chat-dictionary-gate` (priority 10 — blocks a reply containing a banned word from `~/.claude/DICT.json`, the same dictionary the `tg` Telegram-send CLI enforces for outgoing messages, now also covering the Claude Code chat surface itself; must sort ahead of `stop-completion-selfcheck` below or that hook's own block could consume the Stop event first — see its README), `model-error-fallback` (priority 20 — purely advisory, never blocks, so its relative order doesn't matter for the short-circuit above; missing from this table before now, a pre-existing doc gap unrelated to this entry), `stop-completion-selfcheck` (priority 50) |

**The carrier trap (`lib/cc_hook_bridge`, `lib/codex_hook_bridge`).** Harnesses do **not**
run these descriptors directly; they only run hooks declared in their own config. The bridge
dispatchers are the carriers that make installed descriptors fire. The dispatcher itself is
fail-**open** at the top level (a broken bridge must never wedge every tool call), while an
individual fail-closed hook still blocks through the shared `agents-hooks/v1` runner.

**Claude Code:** `lib/cc_hook_bridge` is wired by `rig` into `settings.json`. It maps
`PreToolUse` → `pre-bash`/`pre-write`/`pre-agent`/`pre-skill`/`pre-monitor` (the third for the
`Agent`/`Task` subagent tools, the fourth for the `Skill` tool, the fifth for the `Monitor`
tool), `PostToolUse` file-edit tools → `post-write`, and `Stop` → `stop`; it translates exit-10
BLOCK into CC's `permissionDecision: "deny"` / `decision: "block"`. The bridge also forwards
CC's `agent_id`/`agent_type` (present only inside a dispatched subagent) into the v1 event, so
a subagent-exempt (or, for `subagent-no-monitor`, subagent-ONLY) gate can tell a subagent's own
tool use apart from the orchestrator's.
**Two-repo split, both halves required:** the point mapping (which CC `(event, tool)` maps
to which logical point) lives here; the matcher that makes CC actually *fire* that event —
an `Agent|Task`, `Skill`, or `Monitor` `PreToolUse` matcher in `settings.json`'s
`hook_bridge_entries` — is registered by the separate rig-cli repo. Either half alone is
inert; `pre-agent`, `pre-skill`, and `pre-monitor` are each live only once BOTH sides have
shipped and `rig apply` has run.

**Codex:** `lib/codex_hook_bridge` is the first bridge for the confirmed Codex hooks
contract. Codex TOML hooks call it for `PreToolUse` `Bash` (`pre-bash`), `PreToolUse`
`apply_patch` (`pre-write`), `PostToolUse` `apply_patch` (`post-write` feedback), and `Stop`
(`stop`), reading descriptors from `~/.codex/hooks` and emitting Codex's plain
`{"decision":"block","reason":"..."}` shape. It deliberately does **not** map Codex
`SubagentStart`/`SubagentStop` to `pre-agent` yet; that needs a trustworthy captured payload
fixture before this catalog can safely enforce subagent dispatch semantics.

**The "runs before the command" trap.** `pre-bash` hooks run *before* the command they gate,
so the precondition they check must already be satisfied. The clearest example is
`require-review-before-commit`: it allows `git commit` only if a **review marker file is
fresh** — it stats `REVIEW_MARKER` (default `~/.cache/agent-tools/last-review`) and passes
only when its mtime is within `REVIEW_FRESH_WINDOW_S` (3600s) of now. So the workflow is two
steps: **run the review and refresh the marker first, *then* `git commit`.** Committing first
and reviewing after does not work — the gate has already fired. (This hook is `on_error: open`
— a discipline reminder, not a security boundary; contrast `block-no-verify`, fail-closed.)

---

## The ship gate: the only sanctioned merge

A raw `gh pr merge` is **not** how work lands here — `block-raw-pr-merge` exists to stop it
mid-session. The sanctioned path is **`gh ship <PR>`** (which calls
[`ci/ship/ship.sh`](ci/ship/ship.sh)). Before it merges, `ship` refuses unless:

- the PR is OPEN, not CONFLICTING, not BEHIND its base;
- **every reported CI check's LATEST run is green** — it polls the PR's `statusCheckRollup`,
  collapses it to the latest run per logical check (keyed by check type + workflow + provider +
  name/context, matching how GitHub computes `mergeStateStatus`, so a stale FAILURE from a
  re-run that has since gone green does not block), and treats any check whose latest run isn't
  SUCCESS/SKIPPED/NEUTRAL as a failure. *All* reported checks gate the merge, not only the
  branch-ruleset's required ones (an optional/advisory check whose latest run is red still blocks
  `gh ship`). And *no CI at all is itself a failed gate*, not a free pass — it watches pending
  checks to completion, then gates on the result. **Exception — a genuine CI outage:** if the
  rollup is empty because a `pull_request`-triggered workflow exists on the PR's pushed base
  branch (`origin/<baseRefName>`) but registered no check (billing suspended / Actions down),
  ship runs the local fallback gate (tests + leftover scan + PR checklist + review threads) and,
  only if it is fully green, falls through to the normal non-admin merge (branch protection still
  gates it — no silent `--admin`-bypass); a repo whose workflows never trigger on PRs stays a
  hard refuse (see `ci/ship/README.md` for the trigger-detection heuristic and its residuals);
- there are **no unresolved review threads** — pass `--resolve-addressed-threads` (or
  `SHIP_RESOLVE_ADDRESSED_THREADS=1`) to let ship first auto-close the threads that are safe without
  a human (unresolved + outdated = addressed by a later commit + authored entirely by bots); a human
  or still-unaddressed thread is never touched and still blocks (#268);
- the PR has **at least one GitHub-side review** — `gh pr view --json reviews` must be
  non-empty (any state, any author; existence is the signal, not the verdict). Closes a gap
  the unresolved-threads check above cannot: zero reviews means zero threads, which is
  vacuously "clean". Real incident: hyperide/hyper-saas PR #764 merged with zero reviews on
  Guard-B alone. Disable with `SHIP_EXTERNAL_REVIEW_ENABLED=0` (or the shorter
  `SHIP_EXTERNAL_REVIEW=0`); no self-service override — a one-time bypass is
  `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW="<justification>"` (same shared hatch lib as
  `--skip-ci`/review-quorum; see `ci/ship/external_review_hatch.py`);
- a UI-touching PR carries an embedded **screenshot** (override with `--no-screenshot-ok
  <reason>`);
- a PR that changes **shippable source** (not docs/test/CI) has **bumped the declared
  version** (`pyproject.toml` `version` / `package.json` `"version"`) vs the PR base — a ship
  of source is a release, so `--version` stays a real freshness signal instead of a stale
  literal (skill: `bump-version-on-release`); override a genuine no-release ship with
  `--no-version-bump-ok <reason>` or `SHIP_SKIP_VERSION_BUMP=1`;
- the **review-quorum bar is met** (Guard-B of the self-merge-authority program) — the PR's task
  code (`$REVIEW_TASK_CODE`, else a `HYP-<n>`/uppercase ticket token, else a purely descriptive
  ALL-CAPS/hyphenated code (`SME-ROADMAP-WORKTREE-NOTE`-shaped, 3+ segments, no digits) — all
  tried against the branch name or PR
  body) has ≥ `SHIP_REVIEW_QUORUM_MIN_ITER` PASSED review-cli iterations across ≥
  `SHIP_REVIEW_QUORUM_MIN_ROLES` distinct BOARD ROLES (`review task <code> --check`) — this is
  the DEFAULT check and is always enforced, matching review-cli's own role-based coverage
  (review-cli#246). `SHIP_REVIEW_QUORUM_MIN_MODELS` is an OPT-IN extra: unset by default (no
  model floor), and if an operator sets it, both the role floor AND the model floor apply
  together. Both floors, when in force, are **clamped to a hard minimum of 3** — the env knobs
  can only RAISE the bar, never lower it (a `0`/negative/below-3 value resolves to 3). This is
  the gate that makes self-merge *strictly controlled*; it **fails closed** (a missing task
  code / `review` CLI / unreadable store, OR a quorum reading 0 iterations / 0 distinct roles,
  all refuse — ship re-derives the verdict from the counts, never trusts the subprocess's
  `passed` boolean alone, #242). An installed `review-cli` too old to report roles gets an
  explicit "upgrade review-cli" refusal instead of a generic error. There is **NO self-service override flag** — a one-time bypass
  is requested via `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"`, which asks Alex live
  on Telegram (shared `agenttools_hatch_escalation` lib) and proceeds ONLY on his real-time
  approval. Disable the whole gate with `SHIP_REVIEW_QUORUM=0`. Every non-dry-run gated ship is
  audited to `SHIP_AUDIT_FILE`; dry-runs print the would-be audit without writing it. **Every
  `RIG_HATCH_REQUEST_*` hatch across every agent-hook** (not just ship's own two) is separately
  audited by the shared lib itself: any attempt that REACHES `request_hatch_approval` with the env
  var carrying a value — blank, bare-flag, denied, or approved — appends one JSON line to
  `overrides.log` (default `<real-home>/.config/agent-tools/overrides.log`,
  `default_overrides_log_path()`), closing gap G-8 from the 2026-07-01 agent-ecosystem retrospective
  (`docs/specs/…` in hyperide, section 5.2.3 item 3: "escape hatches have no audit sink"). Two
  pre-existing exceptions, same shape: `ci/ship/skip_ci_hatch.py` (for
  `RIG_HATCH_REQUEST_SHIP_SKIP_CI`) and `ci/ship/external_review_hatch.py` (for
  `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW`) each deny a blank/bare value LOCALLY (a deliberate,
  lib-version-independent guard, unrelated to this feature) before ever calling the shared lib, so
  that specific blank/bare case is recorded only in `SHIP_AUDIT_FILE` on the non-dry-run path — and
  in NEITHER file on `--dry-run` (the local guard denies without calling `_audit`, and `ship.sh`'s
  own dry-run audit helper prints the would-be line without writing it). Every other outcome
  (denied/approved, and the review-quorum hatch's own blank/bare case) routes through the lib as
  described above. A `rig
  status` "overrides this week" section and a weekly tg digest reading this file are tracked as
  follow-up work, not yet built;
- the local branch has **no unpushed/diverged commits** and a **clean worktree**.

Then it squash-merges, deletes the remote branch, removes the local worktree + branch (unless
you're sitting inside that worktree), and fast-forwards the main checkout. It is
repo-agnostic: nothing about an org/tracker/path layout is hard-coded, and knobs are env-driven
— most notably **`SHIP_MAIN_CHECKOUT`** (the primary checkout to refresh post-merge; defaults
to the first `git worktree list` entry), plus `SHIP_DEFAULT_BRANCH`, `SHIP_MERGE_METHOD`,
`SHIP_UI_PATH_REGEX`, `SHIP_IMAGE_UPLOAD_CMD`. `--skip-ci` admin-merges (still runs the other
preflights) and is **deny-by-default** — it proceeds only on a one-time live Telegram approval via
`RIG_HATCH_REQUEST_SHIP_SKIP_CI="<justification>"`. It is NOT the billing-blocked-CI path: for that,
run without `--skip-ci` (the normal path auto-detects the outage and does a normal non-admin merge).

---

## Client-side vs. server-side enforcement (and why `ship` is not enough)

`ship.sh` and the `block-raw-pr-merge` agent-hook are **client-side** gates: they re-check
green-CI / unresolved-threads / screenshot / version-bump *in the session*, before merging.
They are **bypassable** — a merge from the **GitHub web UI**, a raw `gh pr merge` from an
**uninstrumented shell** (no agent-hooks loaded), or any tool that isn't `gh ship` never runs
them, so nothing is checked. That is not a hypothetical: hyper-saas **#543** squash-merged
through **red CI, an open review thread, and no screenshot** precisely because the client gate
was bypassed and there was **no server-side** enforcement behind it.

**Durable enforcement is server-side**, on the merge button itself: GitHub **branch
protection** with the `tier: block` gate contexts listed as **required status checks**, plus
**require-conversation-resolution**, required reviews, and `enforce_admins`. A `tier: block`
CI workflow alone only goes **red** — it does **not** block the merge until its check is
promoted to *required*. That promotion is a server-side admin action this catalog documents
but cannot itself perform; **rig-cli#5** provisions it from a `github:` block in `rig.yaml`
(required_status_checks derived from the enabled block-tier gates,
`required_conversation_resolution`, `enforce_admins`, required reviews — see the
[Client-side vs. server-side enforcement](README.md#client-side-vs-server-side-enforcement-the-543-gap)
section in the README for the schema/example). The catalog is the **what** (gates + this
policy); the reconciler that flips the server-side switches lives in rig-cli (#5).

The block-tier merge-gate READMEs in [`ci/`](ci/) — `tests`, `review-threads`,
`leftover-grep`, `pr-checklist`, `dependency-review`, `screenshots` — now carry this note: a
`tier: block` workflow is a red light, not a merge block, until it is a **required status
check under server-side branch protection** (the same applies to the security gates —
secret-scan, sast, codeql, trivy, license-policy — when you run them as required).

---

## The universal skills layer: where mandatory behavior lives (don't restate it here)

`skills/universal/*` are the cross-project skills, and the global `rig` config selects them by
default for every machine via **`skills.universal.all: true`** (default-on: a universal skill
is included unless explicitly disabled). **This is the single source of truth for universal,
always-apply mandatory behavior** — e.g. delegating non-trivial work to subagents and the
visual-proof cycle for any user-visible change. The harness surfaces them through each skill's
own trigger `description` plus the SessionStart blurb; `rig` installs them.

**Deliberately, this AGENTS.md does not list or restate those universal mandates.** Duplicating
a universal mandatory skill into a per-repo `AGENTS.md` pins a stale copy to one repo, hides the
real source, and goes out of date the moment the skill changes. `AGENTS.md` is for
**project-specific** guidance only; universal mandates stay in the universal layer that carries
them to *every* project and user. If you want the catalog of always-apply behavior, read
`skills/universal/` — not this file.

---

## Other things that surprise an agent here

- **`lib/` modules are separate Python distributions, not one package.** `lib/pyproject.toml`,
  `lib/agenttools_config/`, and `lib/agenttools_retry/` each build/publish independently;
  `agenttools_log` "builds as the `agenttools-log` distribution" and "ships standalone."
  Several are **extracted *from* `rig-cli`** to be shared (e.g. `agenttools_config` is
  generalized from `riglib.config`) — so the dependency direction is `rig-cli` → `lib/`, never
  the reverse. They are **stdlib-only at import time** (heavy deps like PyYAML are lazy) so
  importing one never pulls a toolchain.
- **`rig.yaml` here is REPO-level only.** It declares this repo's own CI gates (currently just
  `ci.items.tests`, tier `block`). The machine-wide installs (skills, agent-hooks, the
  git-hook dispatcher, MCP, harness auto-mode) live in the **global** layer
  (`~/.config/rig/config.yaml`) and are intentionally *not* repeated in the committed file. The
  cascade is by location: global is global, this committed file is the repo and overrides it.
- **The repo dogfoods its own gates on itself.** `.github/workflows/tests.yml` *is* the `ci/tests`
  slot, running `uv run --with pytest pytest tests/` on every PR — and it's the green/red signal
  `gh ship` and the branch ruleset wait on here. The agent-hooks and git-hooks this catalog ships
  are the same ones an agent working in this repo is expected to operate under.
- **The MCP default is "don't add one."** `mcp/` is mostly a *policy*: tools advertise to agents
  as a **CLI + skill**, not an MCP server (an MCP pays a permanent context tax). Add an MCP slot
  only when the agent genuinely can't reach the system from a shell. `review` is a CLI+skill for
  exactly this reason — don't wrap it in MCP.
- **An unused tool / skill / MCP / command is a SIGNAL, not a deletion candidate.** When something
  installed is never invoked, the reflex "prune it" is *wrong* — same root as the dead-code rule.
  "Unused" almost always means **broken, unwired, forgotten, or superseded-but-not-migrated**: a
  capability someone built and never finished routing to its callsites (a registry that points at a
  path the harness doesn't read; a skill the model never gets advertised; a command wired to no
  invoker). Investigate WHY it's unused (`git log -S` the symbol, read the commit that added it,
  check for a missing wiring/registration step), then **FIX or wire it** — or leave it. Deleting
  destroys both the signal and a probably-useful-but-broken capability. This applies to the
  catalog's own skills/hooks AND to a user's installed toolset: never propose pruning a user's
  tools because they "look unused" — find out why they aren't used and fix that.

---

## Working in this repo

- **Docs are English-only.** Every agent-facing doc — this `AGENTS.md`, any repo-level
  `CLAUDE.md`, every `SKILL.md`, every `README.md` — is English. No Cyrillic, not even in
  examples. These files are read by all agents/subagents.
- **Content over code.** Most changes are markdown (a skill, a README) or a small, self-contained
  hook/CI script. A skill has no runtime; a CI slot is a workflow + README; an agent-hook is a
  descriptor + a stdlib-only script. Keep examples generic and portable — nothing in the catalog
  may assume a specific project, since it ships to all of them.
- **Fresh worktrees, off the origin default branch.** Don't work in the primary checkout (another
  agent may hold it). A fresh worktree bases on `origin/<default branch>`, not your current HEAD —
  verify the base before you start if your work is stacked on unmerged branches.
- **Create worktrees with `rig worktree create <name> --from origin/main`** (rig-cli — the
  explicit `--from` matters: the command defaults to branching off your current `HEAD`, which
  contradicts the "off the origin default branch" rule above if you run it from inside an
  existing feature worktree). It lands the tree at the standardized `<repo>/.worktrees/<name>`
  and, *before* creating it, registers `/.worktrees/` in this **clone's** `.git/info/exclude`
  (shared by every linked worktree of this clone, never committed) — that's why `.worktrees/`
  is intentionally NOT in the committed `.gitignore`: it's a local scratch convention, not
  something every clone should carry. Remove a worktree with **`rig worktree remove <name>`**,
  never by deleting its source files by hand (leaves stale `git worktree` metadata) and never
  with plain `git worktree remove` either — that leaves the branch behind, and a later `rig
  worktree create <same-name>` then fails because the branch already exists; `rig worktree
  remove` deletes both the tree and its branch together.
  - **First time in a clone** (including right after pulling this change, or on a brand new
    clone/CI checkout): the exclude entry doesn't exist yet, so `git status` will show any
    `.worktrees/*` content — pre-existing or hand-made — as untracked. Fix it by running `rig
    worktree create <a-new-name> --from origin/main` once (same `--from` reasoning as above —
    this bootstrap worktree is throwaway, remove it with `rig worktree remove` right after) —
    the registration covers the whole `.worktrees/` directory, not just that one name, so it
    retroactively ignores any existing entries too. (It must be a name that doesn't already
    exist under `.worktrees/`: the command checks `target.exists()` and errors "already exists"
    *before* it reaches the exclude reconcile, so re-running it for an already-created
    worktree's own name does nothing.) The other fix is to append `/.worktrees/` to
    `.git/info/exclude` by hand, no `rig worktree create` involved — run this from the
    **primary checkout**, or resolve the shared path first with `git rev-parse
    --git-common-dir` (inside a *linked* worktree `.git` is a file, not a directory, so a
    literal `.git/info/exclude` path doesn't exist there). Do this before running `git add
    -A`/`git add .` from the primary checkout — otherwise a stray `.worktrees/*` entry can get
    staged as a broken gitlink (or, if a worktree's own `.git` file is missing, as plain files).
- **The gates apply to you too.** Atomic, conventional commits (one logical change each); review
  before each commit (refresh the marker, *then* commit); never `--no-verify`; land only via the
  ship gate. These are the same guards this catalog installs for any repo — here they're
  dogfooded.

See [`README.md`](README.md) for the full catalog reference, inventory, and the carrier-decision
guide ([`docs/carrier-decision-guide.md`](docs/carrier-decision-guide.md): skill vs. agent-hook
vs. git-hook).

---

## Review-thread resolution authority

A shipping agent **may** autonomously resolve a review thread on a PR it created when ALL
four conditions hold:

1. **Quorum review passed**: `review diff` (or `review quorum`) produced an `[ok]` marker for
   the current diff — multi-model consensus cleared.
2. **Finding is P2 / advisory / suggestion / nit** — explicitly NOT P0, P1, critical, or
   security-tagged.
3. **Commenter is an automated bot and no human replied** — the original commenter's
   `author.login` ends in `[bot]` OR is a known automated reviewer (`chatgpt-codex-connector`,
   `codex-review-bot`), AND every other comment in the thread also passes this check. If any
   human commented anywhere in the thread, CTO instruction is required.
4. **The PR was created by the agent in this session** — not resolving threads on someone
   else's pre-existing PR.

The mutation is `resolveReviewThread(input: {threadId: "PRRT_..."})` via `gh api graphql`.

**The `block-raw-pr-merge` false-positive is fixed (#268).** The hook used to fail-closed on
**any** `gh api graphql` call whose query carried a `$variable` — it could not prove the mutation
wasn't a merge, so it blocked, wedging `resolveReviewThread`/`addPullRequestReviewThreadReply`. It
now blocks only an **actual** merge: `gh pr merge` and a literal `mergePullRequest` /
`enablePullRequestAutoMerge` mutation token. A **single-quoted** inline mutation that carries literal
GraphQL variables (`-f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t})…}'`) is
**allowed** — the shell never expands a single-quoted `$t`, so the query GitHub receives is exactly
the text the hook read and the literal scan is authoritative. A query stays fail-closed when it is
unreadable at pre-exec time: a `@file`/stdin body, or a **shell-expandable** `$`/backtick — one that
is unquoted, double-quoted, or concatenated (`-f query="$Q"`, `-f query="mutation{$OP}"`,
`-f query='…'$OP'…'`) — because the shell rewrites it at runtime and could splice a merge past the
literal scan. So an agent that meets the four conditions can run the resolve mutation directly (keep
the query single-quoted).

**Two ways to resolve, both live:**

- **Direct — the autonomous agent path.** Run `gh api graphql -f
  query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -F
  t=PRRT_...` — no longer blocked by the hook (#268). This is the path the **four conditions above
  govern**: the agent is responsible for verifying quorum-passed, advisory-severity, all-bot, and
  own-PR before issuing the mutation.
- **Through ship — an explicit operator flag, narrower and self-enforcing.** `gh ship <PR>
  --resolve-addressed-threads` is **not** the autonomous path and does **not** rely on the agent's
  judgement of the four conditions; it enforces its own conservative rule against live PR data: it
  resolves a thread only when it is unresolved **and** `isOutdated` (the anchored code changed — a
  later commit addressed it) **and** authored **entirely** by bots (with comment-truncation and
  null-author both failing closed). A human or still-current thread is never touched and still blocks.
  It runs during ship's preflight, so if a **later** gate (review-dwell, version-bump, quorum,
  clean-worktree) then refuses, some eligible bot nits may already be resolved on a PR that does not
  merge *this* run — accepted, because those nits are genuinely addressed and would need resolving on
  the next run anyway; the flag never resolves a human or unaddressed thread regardless of outcome.
