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
| `pre-bash` | `block-no-verify` (fail-closed), `block-raw-pr-merge`, `require-review-before-commit`, `require-ticket-before-commit`, `enforce-timeout-on-bash`, `orchestrator-stays-thin` (gate 2, bash half), `no-long-inline-process` (gate 3), `skills-read-gate` (gate 4, commit), `visual-proof-gate` (gate 5, commit) |
| `pre-write` | `block-secrets-write`, `block-raw-process-env`, `orchestrator-stays-thin` (gate 2, write half), `skills-read-gate` (gate 4, code-write) |
| `pre-tool` | `background-subagent-gate` (gate 1 — fires on the `Task`/`Agent` dispatch tool) |
| `pre-skill` | `skills-read-gate` (records a skill as read) — fires on the `Skill` tool |
| `post-write` | `format-on-write` (reacts to the completed write; never blocks — see the bridge note: not carried to CC yet) |
| `stop` | `stop-completion-selfcheck`, `visual-proof-gate` (gate 5 — blocks a "done" on an un-proven visible change, once) |

The **enforcement-doctrine gates** (ROADMAP "DOCTRINE: enforce correct agent behavior with
PreToolUse hooks") — `background-subagent-gate`, `orchestrator-stays-thin`,
`no-long-inline-process`, `skills-read-gate`, `visual-proof-gate` — make the
orchestrate-only / background-subagents / read-your-skills / visual-proof rules STRUCTURAL
instead of prose that regresses. Each is **satisfiable** by an honest action, **tiered**
(warn before block), carries an **honest escape hatch** (an env override with a logged
reason, or an inline sentinel — never a forge), and gates 1-3 **exempt subagents** via CC's
`CLAUDE_CODE_CHILD_SESSION` env signal (set in every child/subagent session). See each hook's
README for its triggers, env knobs, and tests.

**The carrier trap (`lib/cc_hook_bridge`).** Claude Code does **not** run these descriptors
directly — it only runs hooks declared in `settings.json`. `lib/cc_hook_bridge` is the
dispatcher that makes them fire: `rig` wires it into `settings.json` and it translates the v1
exit-10 BLOCK into CC's `permissionDecision: "deny"` / `decision: "block"`. **Without that
bridge an agent-hook is inert in CC** (agent-tools#18). The dispatcher itself is fail-**open**
at the top level (a broken bridge must never wedge every tool call), while an individual
fail-closed hook still blocks.

**Non-obvious:** the bridge only maps the points CC has a matching event for —
`PreToolUse` → `pre-bash`/`pre-write`/`pre-tool` (`Task|Agent`)/`pre-skill` (`Skill`) and
`Stop` → `stop` (see `point_for_event` in `dispatch.py`). It does **not** yet map
`PostToolUse` → `post-write`, so `format-on-write` — the one `post-write` hook — is not
carried to CC through this bridge today; it works on harnesses whose own
`post-write`/`PostToolUse` event you map it to directly. Don't assume every shipped hook is
live in CC just because the bridge exists. Each new PreToolUse point also needs its matcher
in rig-cli's `hook_bridge_entries` (`Task|Agent`, `Skill`) — without that, CC never invokes
the bridge for that tool and the gate is inert (the same carrier trap).

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
- the local branch has **no unpushed/diverged commits** and a **clean worktree**.

Then it squash-merges, deletes the remote branch, removes the local worktree + branch (unless
you're sitting inside that worktree), and fast-forwards the main checkout. It is
repo-agnostic: nothing about an org/tracker/path layout is hard-coded, and knobs are env-driven
— most notably **`SHIP_MAIN_CHECKOUT`** (the primary checkout to refresh post-merge; defaults
to the first `git worktree list` entry), plus `SHIP_DEFAULT_BRANCH`, `SHIP_MERGE_METHOD`,
`SHIP_UI_PATH_REGEX`, `SHIP_IMAGE_UPLOAD_CMD`. `--skip-ci` admin-merges (CI billing-blocked
only) and still runs the other preflights.

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
