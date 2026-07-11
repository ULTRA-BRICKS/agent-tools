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
| Skill | `skills/universal/<name>/SKILL.md` or `skills/by-type/<group>/<name>/SKILL.md` | one `SKILL.md` with `name` + `description` frontmatter (the trigger), the portable rule, rationale, a generic example |
| Agent-hook | `agent-hooks/<name>/` | a `<id>.<point>.json` descriptor + the executable it points at + `README.md` |
| CI gate | `ci/<slot>/` | a `README.md` + either a `workflow.yml` (most slots) or a shell script (a client-side gate like `ci/ship/ship.sh`, which is a merge command, not a GitHub workflow) |
| Git-hook | `git-hooks/<hook>` (`pre-commit`, `commit-msg`, `pre-push`, `no-secrets-scan`, `lefthook.yml`, `global-dispatcher/`) | the copyable hook script |
| MCP slot | `mcp/<name>/` | the slot's config + `README.md` (see the MCP-vs-CLI policy first — the default is *don't add one*) |
| Lib module | `lib/<module>/` | an importable Python package (often its own `pyproject.toml` — see below) |

The `<group>` axis for by-type skills is fixed:
`bot`, `backend`, `frontend`, `cli`, `library`, `infra`, `monorepo`. A skill that applies to
*every* project goes in `universal/`; one scoped to a project shape goes under its `by-type/`
group.

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
| `pre-agent` **(bridge-ready; NOT yet live in CC — needs the rig-cli `Agent\|Task` matcher; NOT mapped in Codex yet)** | `background-subagent-gate` (orchestration doctrine: block a non-trivial FOREGROUND subagent dispatch; subagent-exempt) |
| `pre-bash` | `block-no-verify` (fail-closed), `block-raw-pr-merge`, `block-reset-hard` (fail-closed: blocks `git reset --hard` and `git clean -f...`/`-fd`/`-fdx` — irreversible working-tree wipes with no undo; deny-by-default, no self-service bypass — an optional repo-owner `agent_hooks.approval_cmd` in rig.yaml is the only override, Alex tg#6554), `require-review-before-commit`, `require-ticket-before-commit`, `enforce-timeout-on-bash`, `orchestrator-stays-thin` (impl-bash, warn→block, subagent-exempt), `no-long-inline-process` (review/--watch/build-test/long-sleep, subagent-exempt), `subagent-no-bg-longproc` (the INVERSE: block a SUBAGENT from BACKGROUNDING a long process — `run_in_background:true`/`&`/`setsid` on review/--watch/build-test/long-sleep — since a subagent is never re-invoked by a background-completion notification and would wedge forever; subagent-ONLY), `no-shell-file-edit` (block `sed -i`/`perl -i`/`gawk -i inplace` or a `> file` redirect editing a tracked source file; parsed not raw-matched; NOT subagent-exempt), `skills-read-gate` (mandatory skills before work, warn→block), `visual-proof-gate` (block a UI commit with no looked-at screenshot), `decision-request-format` (ADVISORY, never blocks: on a `tg --tag decision` send, self-check the body for Context/Options/Recommendation per the `decision-request-discipline` skill; parsed not raw-matched; NOT subagent-exempt) |
| `pre-write` | `block-secrets-write`, `block-raw-process-env`, `orchestrator-stays-thin` (non-docs code Edit/Write, warn→block, subagent-exempt) |
| `post-write` | `format-on-write`, `lint-on-write` (react to the completed write; exit-10 is feedback because the write already landed) |
| `stop` | `stop-completion-selfcheck` |

**The carrier trap (`lib/cc_hook_bridge`, `lib/codex_hook_bridge`).** Harnesses do **not**
run these descriptors directly; they only run hooks declared in their own config. The bridge
dispatchers are the carriers that make installed descriptors fire. The dispatcher itself is
fail-**open** at the top level (a broken bridge must never wedge every tool call), while an
individual fail-closed hook still blocks through the shared `agents-hooks/v1` runner.

**Claude Code:** `lib/cc_hook_bridge` is wired by `rig` into `settings.json`. It maps
`PreToolUse` → `pre-bash`/`pre-write`/`pre-agent` (the last for the `Agent`/`Task` subagent
tools), `PostToolUse` file-edit tools → `post-write`, and `Stop` → `stop`; it translates
exit-10 BLOCK into CC's `permissionDecision: "deny"` / `decision: "block"`. The bridge also
forwards CC's `agent_id`/`agent_type` (present only inside a dispatched subagent) into the v1
event, so a subagent-exempt gate can tell a subagent's own tool use apart from the
orchestrator's. **rig-cli follow-up:** for CC to actually *fire* `pre-agent`, rig-cli must
add an `Agent|Task` PreToolUse matcher to `settings.json` (`hook_bridge_entries`) — that
wiring lives in the separate rig-cli repo; the bridge half (point mapping + signal forwarding)
lives here.

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
- **every reported CI check is green** — it polls the PR's `statusCheckRollup` and treats any
  check that isn't SUCCESS/SKIPPED/NEUTRAL as a failure, so *all* reported checks gate the merge,
  not only the branch-ruleset's required ones (an optional/advisory check that goes red still
  blocks `gh ship`). And *no CI at all is itself a failed gate*, not a free pass — it watches
  pending checks to completion, then gates on the result;
- there are **no unresolved review threads**;
- a UI-touching PR carries an embedded **screenshot** (override with `--no-screenshot-ok
  <reason>`);
- a PR that changes **shippable source** (not docs/test/CI) has **bumped the declared
  version** (`pyproject.toml` `[project] version`, Poetry `[tool.poetry] version`, supported
  dynamic pyproject sources such as `VERSION`/`__init__.py`, or `package.json` `"version"`) vs
  the PR base — a ship of source is a release, so `--version` stays a real freshness signal
  instead of a stale literal (skill: `bump-version-on-release`); override a genuine no-release ship with
  `--no-version-bump-ok <reason>` or `SHIP_SKIP_VERSION_BUMP=1`;
- the **review-quorum bar is met** (Guard-B of the self-merge-authority program) — the PR's task
  code (`$REVIEW_TASK_CODE`, else a `HYP-<n>`/uppercase ticket token from the branch name or PR
  body) has ≥ `SHIP_REVIEW_QUORUM_MIN_ITER` recorded review-cli iterations across ≥
  `SHIP_REVIEW_QUORUM_MIN_MODELS` distinct models (`review task <code> --check`). This is the gate
  that makes self-merge *strictly controlled*; it **fails closed** (a missing task code / `review`
  CLI / readable store all refuse). There is **NO self-service override flag** — a one-time bypass
  is requested via `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"`, which asks Alex live
  on Telegram (shared `agenttools_hatch_escalation` lib) and proceeds ONLY on his real-time
  approval. Disable the whole gate with `SHIP_REVIEW_QUORUM=0`. Every non-dry-run gated ship is
  audited to `SHIP_AUDIT_FILE`; dry-runs print the would-be audit without writing it;
- the local branch has **no unpushed/diverged commits** and a **clean worktree**.

Then it squash-merges, deletes the remote branch, removes the local worktree + branch (unless
you're sitting inside that worktree), and fast-forwards the main checkout. It is
repo-agnostic: nothing about an org/tracker/path layout is hard-coded, and knobs are env-driven
— most notably **`SHIP_MAIN_CHECKOUT`** (the primary checkout to refresh post-merge; defaults
to the first `git worktree list` entry), plus `SHIP_DEFAULT_BRANCH`, `SHIP_MERGE_METHOD`,
`SHIP_UI_PATH_REGEX`, `SHIP_IMAGE_UPLOAD_CMD`. `--skip-ci` admin-merges (CI billing-blocked
only) and still runs the other preflights.

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

**Current enforcement gap**: the harness auto-mode classifier treats this mutation as
"permission laundering" when conditions are passed only in prompt text. To make autonomous
resolution work, one of the following must be in place:

- A Bash permission rule in `~/.claude/settings.json` allowing `gh api graphql` mutations
  on PR review threads matching the above conditions; OR
- An `agent-hooks/resolve-review-thread/` gate (a `pre-bash` hook) that validates the four
  conditions before allowing the `gh api graphql resolveReviewThread` call — verified against
  the actual PR data, not prompt-text claims.

Until one of those mechanisms exists, an agent that meets all four conditions should still
attempt the resolution; if the harness blocks it, escalate to the CTO with the thread ID
and evidence that all four conditions are met.
