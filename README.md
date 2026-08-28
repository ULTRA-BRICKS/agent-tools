# agent-tools

`agent-tools` is the portable **rule/guard catalog** for AI-assisted development — skills,
agent-hooks, git-hooks, CI gates, and MCP slots, distilled from practice and generalized so
they apply to **any** project, language, or team. It is a *library*, not an installer: you
don't cherry-pick 36 skills, 8 hooks, and 12 CI gates by hand. You install the whole catalog
through one front door — [`rig`](https://github.com/alex-mextner/rig-cli) — which reads a
committed `rig.yaml` and wires it in. agent-tools is the **what**; rig is the **how**.

## Quick start — the single front door (`rig init`)

You enter agent-tools through `rig`. One command, run once, scaffolds a `rig.yaml`, wires in
the catalog, and recommends auto-mode by default:

```bash
rig init          # first-run onboarding: scaffold rig.yaml + wire the catalog in
rig apply         # steady state: re-apply on every machine, identically (idempotent, backs up on conflict)
rig status        # later: has the repo drifted from rig.yaml?
```

`rig init` and `rig apply` are two distinct commands: `init` is first-run onboarding (no
config yet → scaffold one and walk you through the catalog); `apply` is the steady-state
reconcile (converge the disk to an existing `rig.yaml`). Interactivity (full TUI / semi /
non-interactive `--yes`) is orthogonal — both run in any mode. (`rig setup` is a back-compat
alias of `rig init`.) rig scans this catalog live and installs what `rig.yaml` enables
— skills to your harness's skills dir, agent-hooks to its hook dir, the global git-hook
dispatcher, CI workflows, MCP registrations. Update agent-tools, re-run `rig apply`, and the
new items flow in with no code change in rig. See
[rig-cli](https://github.com/alex-mextner/rig-cli) for install and the full `rig.yaml`
schema.

## Why this exists: autonomous, observable, controllable — and safe

The point of wiring the whole catalog in one shot is to let an agent **work autonomously
with minimum babysitting** after a single front-door command. That is only sane because of
the guards. The spine:

> **The guards make auto-mode safe; the logging makes it observable; the config makes it
> controllable.**

| Pillar | Mechanism in agent-tools |
| --- | --- |
| **Autonomous** | **Auto-mode** — the harness auto-accepts tool calls, so the agent runs without a permission prompt on every step. rig provisions this (`harness.auto_mode`) and **recommends it on by default** — *because the guards below are installed.* |
| **Safe** | **Agent-hooks** intercept the dangerous call *before* the side effect: block a secret write, block a `--no-verify` gate bypass, enforce shell timeouts, block a raw `process.env` read, and **block a raw `gh pr merge` that skips the green-CI + screenshot ship gates** (`block-raw-pr-merge`). The guards are what *buy* the autonomy — auto-mode is only safe because the irreversible actions are caught mid-session. |
| **Observable** | **Structured JSONL logging** (`lib/agenttools_log`) — one log shape across the ecosystem CLIs, plus tg reporting — so you can see what the agent actually did. |
| **Controllable** | **Config** (`rig.yaml`) is the steering wheel: enable/disable any item, pin targets, set escape hatches. Paired with decisions-as-buttons / `tg ask`, you steer the agent without sitting on it. |

Every guard ships an **escape hatch** (an env var or an inline sentinel), so safety is a
controllable gate, not a hard wall — you can override a guard *with an explicit, logged
reason* when you genuinely need to.

---

# Reference

Everything below is the catalog reference — the carriers, the per-directory map, install
instructions, and the inventory.

Nothing here is project-specific. Every skill, hook, and template is written to be useful to
a reader with no prior context: the rationale travels, the examples are generic, and the
mechanisms are stack-agnostic (bun/node, python-uv, go).

## What's in here

| Directory      | What it holds                                                                 |
| -------------- | ----------------------------------------------------------------------------- |
| `skills/`      | Advisory rules as markdown skills — `universal/` (any project), `by-type/{bot,backend,frontend,cli,library,infra,monorepo}/` (by project shape), and `by-stack/<l1>/<lang>[/<framework>]/` (by declared tech stack; a repo inherits every skill whose stack path is a prefix of its declared stack). Each is one `SKILL.md` with `name` + `description` frontmatter, the portable rule, a rationale, and a generic example. |
| `agent-hooks/` | Programmatic guards that fire on an agent's tool use (the `agents-hooks/v1` contract). Each is a descriptor + executable + README. They enforce, mid-session, what a skill can only advise. |
| `linters/` | Canonical linter/formatter carriers consumed by Rig (Oxc baseline: Oxlint + Oxfmt). |
| `git-hooks/`   | Copyable `pre-commit` / `commit-msg` / `pre-push` / `no-secrets-scan` hooks plus a `lefthook.yml`, generalized across three toolchains — and a **global dispatcher** (`global-dispatcher/`) that runs every hook in `~/.config/git/global-hooks.d/` in **every** repo, even ones whose lefthook/husky override `core.hooksPath`. |
| `ci/`          | Drop-in CI / PR-gate building blocks a CI-building agent looks for first — one slot per concern (workflow + optional shell script + README): secret-scan (gitleaks), CodeQL (incl. a no-GHAS self-gate), semgrep SAST, dependency-review + license, AI-review, Copilot-findings, unresolved-review-thread block, unchecked-checkbox block, mandatory screenshots, conventional-commit PR-title lint, leftover-marker grep, and a green-CI-gated `ship` merge command. See [`ci/README.md`](ci/README.md). |
| `mcp/`         | The MCP-vs-CLI+skill policy (why `review` is a CLI+skill, not an MCP) and a code-search MCP slot. |
| `lib/`         | Reusable, importable library code + shared contracts the ecosystem CLIs depend on. `agenttools_log` — shared **structured JSONL logging** (stdlib-only) so `review-cli`, `rig-cli`, and future Python CLIs log in one shape. `agenttools_providers` — the tool-agnostic **CORE of the multi-model provider abstraction** (stdlib-only at import): a capability-tagged model registry, role→model resolution that honors tags (role `vision` resolves only to vision-capable), a priority-ordered **failover board** (top-N reachable + reserve), and a **key cascade** (env-name precedence, then `.env` files). Decides *which* model/seat/key; transports (`oc:` routing, live calls) stay in the consuming tool. `contracts/models.yaml` — the **model board**: the current-best concrete model per provider, each tagged with capabilities (esp. `vision`, for the image-review filter), plus a symbolic roles/aliases map (validated by `contracts/models.schema.json`); loaded by `agenttools_providers.load_registry`. `checker/model_freshness.py` — a **daily currency checker** that polls provider model-list endpoints and PROPOSES version bumps (a PR, or a dated report) when a newer model appears (semi-automatic; a human confirms). rig provisions the checker as a daily noon cron. See [`lib/README.md`](lib/README.md), [`lib/agenttools_providers/README.md`](lib/agenttools_providers/README.md), [`lib/checker/README.md`](lib/checker/README.md). |
| `docs/`        | `carrier-decision-guide.md` — when a rule belongs in a skill vs an agent-hook vs a git-hook. |

## The three carriers (and why there are three)

A rule is enforced at a different moment depending on its carrier:

- **Skill** — advice the agent/human reads. The only honest carrier for *judgment* rules
  (naming, design, "investigate before deleting") that no regex captures.
- **Agent-hook** — intercepts a tool call *mid-session* and can block it *before* the side
  effect (block a `--no-verify` bypass; stop a secret being written; block a raw `gh pr
  merge` that skips the ship gates; prompt the completion self-check at end of turn). A
  git-hook can't do these — it fires too late.
- **Git-hook** — aborts a commit/push for *mechanical* checks (format, types, tests, no
  secrets) — for every committer, human or agent, no harness required.

Many rules want **both** an agent-hook (prevent early) and a git-hook (backstop). See
[`docs/carrier-decision-guide.md`](docs/carrier-decision-guide.md).

## Using the skills

Skills are plain markdown — drop them where your agent harness discovers skills (commonly a
skills directory it scans), or just read them. The frontmatter `description` is a
when-to-use trigger; the body is the rule. They have no runtime and no dependencies.

> **Two of these are always-apply — not opt-in, every task, every agent:**
> [`delegate-work-to-subagents`](skills/universal/delegate-work-to-subagents/SKILL.md) (the
> main thread is an orchestrator — plan, decompose, dispatch to subagents or a dynamic
> workflow, and verify; it does **not** do non-trivial work inline) and
> [`visual-proof-cycle`](skills/universal/visual-proof-cycle/SKILL.md) (capture and **look
> at** any user-visible change before claiming it works). Both are universal skills, so rig
> installs them by default (`skills.universal.all`), and their strong frontmatter triggers
> surface them on any matching task. The mandate lives in the universal layer itself —
> [`skills/universal/`](skills/universal/) is the source of truth (see the **universal skills
> layer** section of [`AGENTS.md`](AGENTS.md), which points back to it rather than restating it).

```
skills/universal/shell-timeouts/SKILL.md
skills/universal/web-page-reading-agent-browser/SKILL.md
skills/by-type/backend/atomic-db-transactions/SKILL.md
skills/by-stack/frontend/ts/react/vercel-react-patterns/SKILL.md
```

> **Reading a web page, API doc, or reference?** Reach for the
> [`web-page-reading-agent-browser`](skills/universal/web-page-reading-agent-browser/SKILL.md)
> skill: drive the [`agent-browser`](https://github.com/vercel-labs/agent-browser) CLI
> (`open` + `get text body` / `eval`) instead of a fetch-and-summarize tool that
> **truncates long pages and silently drops content**. It loads the full JS-rendered page
> and lets you extract just the section you need — for very long pages, extract a bounded
> slice (scoped selector / `eval`) or save to a file, so your own tool's output limit
> doesn't re-truncate it. `agent-browser` is a standalone
> third-party CLI (`npm i -g agent-browser && agent-browser install`, or `cargo install
> agent-browser && agent-browser install`) — not bundled here; the skill is the advisory
> "reach for it, and how."

### Universal skills vs. a project's `AGENTS.md`

There are two different homes for agent guidance, and they must not be confused:

- **A project's `AGENTS.md` (or a repo-level `CLAUDE.md`) is for project-specific guidance
  only** — how *this* repo builds, its layout, its local conventions. Nothing in it should
  apply to every other repo.
- **Cross-project, always-apply MANDATORY skills** (for example `visual-proof-cycle` or
  `task-completion-selfcheck`) live here in `skills/universal/`. They are provisioned by
  **rig's universal skill layer** and meant to reach **every project and every user** through
  the SessionStart blurb, the rig-installed skills, and each skill's own trigger
  `description`. That layer is their single source of truth.

**Never copy a universal mandatory skill into an individual `AGENTS.md`.** Duplicating it
there pins a stale copy to one repo, hides the real source, and goes stale the moment the
skill changes. The universal layer is the one place that carries these mandates across all
projects and users — let it. `AGENTS.md` stays project-specific; universal mandates stay
universal.

## Installing the agent-hooks

Each hook directory has a JSON **descriptor**, an executable **script**, and a **README**.
The descriptor speaks the `agents-hooks/v1` protocol (JSON on stdin, exit `0` = allow,
exit `10` = block, other = error → `on_error` policy). To install one:

1. `chmod +x` the script.
2. Set the descriptor's `cmd` to the script's **absolute path** (the runner rejects
   relative/bare commands).
3. Drop the descriptor into your harness's hook directory for the matching point
   (`pre-bash`, `pre-write`, `pre-agent`, `pre-skill`, `post-write`, `stop` — map to your
   harness's real event names).

`rig apply` does all three for you (it rewrites the `cmd` placeholder to the script's
absolute path in your agent-tools checkout). See [`agent-hooks/README.md`](agent-hooks/README.md)
for the full contract and each hook's README for test commands.

## Installing the git-hooks

Either copy them into `.git/hooks/` (per clone), point `core.hooksPath` at a tracked
directory, or use [`lefthook`](git-hooks/lefthook.yml) (`lefthook install`) for a committed,
team-wide setup. They auto-detect bun/node vs python-uv vs go. To make a hook run in **every**
repo on the machine — even ones with lefthook/husky that shadow `core.hooksPath` — use the
**global dispatcher** in [`git-hooks/global-dispatcher/`](git-hooks/global-dispatcher/README.md)
(`install-local-hooks.sh` retrofits an existing repo; `hooks-sweep` does the whole machine).
See [`git-hooks/README.md`](git-hooks/README.md) and the `global-git-hooks` skill.

## Using the CI tools

`ci/` holds drop-in CI / PR-gate building blocks — the first place a CI-building agent
should look for "is there a standard way to do this in CI?" It covers secret scanning
(gitleaks), SAST (CodeQL incl. a no-GHAS self-gate, and semgrep), dependency + license
review, an AI-review step, surfacing Copilot findings, and the "block merge until X" gates
(unresolved review comments, unchecked PR checkboxes, missing screenshots, non-conventional
PR titles, debug leftovers in the diff), plus a green-CI-gated `ship` merge command. Each
slot is a workflow (+ usually a generic shell script for non-GitHub CI) and a README with
the standard engine, knobs, and escape hatch. Copy the workflow into `.github/workflows/`,
or call the shell script from a pipeline step. Start at [`ci/README.md`](ci/README.md) and
the [`ci-gate-suite`](skills/universal/ci-gate-suite/SKILL.md) skill.

## Client-side vs. server-side enforcement (the #543 gap)

**A gate that only runs on the client is bypassable; durable enforcement is server-side.**
This is the single most misunderstood thing about the catalog, and the reason a real PR
(hyper-saas **#543**) squash-merged through **red CI, an unresolved review thread, and no
screenshot**. Know which half you're relying on:

| Layer | What it is | What bypasses it |
| --- | --- | --- |
| **Client-side** | The [`ship`](ci/ship/) gate (`gh ship`) and the `block-raw-pr-merge` agent-hook — they re-check green-CI / threads / screenshot / version **before** merging, in the session. | A merge from the **GitHub web UI**, a raw `gh pr merge` from an **uninstrumented shell** (no agent-hooks), or any tool that isn't `gh ship`. The client gate never runs, so nothing is checked. |
| **Server-side** | GitHub **branch protection**: **required status checks** (a `tier: block` gate's context listed as *required*) + **require-conversation-resolution** + required reviews + `enforce_admins`. Enforced by GitHub itself, on the merge button. | Nothing short of an admin turning protection off. This is the durable boundary. |

**A `tier: block` CI workflow is NOT enforcement by itself.** It only makes a check go
**red**; the merge button stays clickable until that check is promoted to a **required
status check** under branch protection. Promoting it is a **server-side** admin action — and
historically a *manual* one the catalog could not perform, which is precisely the #543 gap:
the gates were all present and red, but advisory, so the merge went through anyway.

**rig-cli#5** closes the gap: it makes branch protection a *reconciled, config-driven*
artifact (declared in `rig.yaml`, applied by `rig apply`) the same way skills/hooks/CI are —
so the required-checks set is provisioned, not hand-toggled. agent-tools is the **what**
(the gates + this policy); the reconciler that flips the server-side switches lives in
[rig-cli](https://github.com/alex-mextner/rig-cli) (#5).

### The `github:` block in `rig.yaml` (provisioned by rig-cli#5)

`rig.yaml` declares the repo's GitHub settings the same way it declares CI; `rig apply`
reconciles them via `gh api` (branch-protection / rulesets) — secure defaults ON, so a fresh
`rig init` lands the guardrails with zero hand-toggling. The illustrative shape below is the
**classic branch-protection** model (`required_status_checks.contexts`, `enforce_admins`,
`required_pull_request_reviews`); the newer *rulesets* API expresses the same intent with a
different field layout. Either way the authoritative schema and the chosen backend live in
[rig-cli](https://github.com/alex-mextner/rig-cli) (#5) — this is the concept, not the wire
format:

```yaml
github:
  branch_protection:
    branch: main
    # Promote each tier:block CI gate to a REQUIRED status check. rig derives this set from
    # the enabled tier:block ci.items; list extra contexts explicitly if you vendor a gate.
    # A context is the check-RUN name (the job's `name:`), NOT the workflow filename — a
    # required context that never reports (typo'd/nonexistent) stays forever `pending` and
    # blocks ALL merges (independent of `strict`, which only governs up-to-date-with-base),
    # so keep these in sync with each workflow's actual check name.
    required_status_checks:
      strict: true            # PR must be up to date with base before merge
      contexts:
        - tests
        - review-threads
        - leftover-grep
        - dependency-review
        - PR Checklist
        - screenshots
    required_conversation_resolution: true   # native "resolve threads before merge"
    required_pull_request_reviews:
      required_approving_review_count: 1
      dismiss_stale_reviews: true
    enforce_admins: true       # the rules apply to admins too — no quiet override
    allow_force_pushes: false
    required_linear_history: true
```

Without the `github:` block (or its equivalent set by hand), every gate above is a red light
that nobody is required to stop at — see #543.

## Inventory

- **Universal skills:** 47 — shell-timeouts, exit-codes-through-pipes, dead-code-
  investigation, TDD red-first, test discipline, atomic commits, pre-commit gate, secret
  scanning (gitleaks, hook + CI), CI gate suite, global git-hooks dispatcher, git-workflow
  safety (reset/fixup/partial-staging/worktree-removal), comment &
  naming hygiene, no type escape hatches, systematic debugging, smallest change, shared-util
  single-source, file-header comments, promise = durable action, worktree-base trap,
  **delegate-work-to-subagents** (the main thread orchestrates — plan, dispatch to
  subagents, verify — never does non-trivial work inline) and **visual proof cycle** (look
  at any user-visible change before claiming it works) — the two **always-apply** skills
  (see [`AGENTS.md`](AGENTS.md)) — GAN critic loop, **adversarial verification** (when
  verifying a fix, try to break it and prove the result with an artifact, don't just
  confirm the happy path), completion self-check, semantic code
  search, **web-page reading via agent-browser** (read full pages/docs with the
  `agent-browser` CLI instead of a truncating fetch tool), **message-scope verification**
  (before dropping unfinished work for a new inbound message, check it's actually on-topic
  and ask the user to confirm if it looks misdirected), **agent-browser concurrency**
  (give every `agent-browser` task its own session and close it, instead of racing
  concurrent tasks on the shared default session), and more.
- **By-type skills:** 45 — bot (12), backend (13), frontend (4), cli (7), library (4),
  infra (1), monorepo (4).
- **By-stack skills:** 5 — `frontend/ts` (ts-strictness) + `frontend/ts/react`
  (vercel-react-patterns); `mobile/swift` (swift-concurrency) + `mobile/swift/swiftui`
  (swiftui-mvvm, tca-swiftui). Selected by declared stack prefix, not project shape.
- **Agent-hooks:** see `agent-hooks/` for the current directory listing and
  `agent-hooks/README.md`'s point table for the full, authoritative set and count — a
  representative sample: block-no-verify, block-raw-pr-merge, block-secrets-write,
  require-review-before-commit, enforce-timeout-on-bash, block-raw-process-env,
  stop-completion-selfcheck, pkill-guard (blocks a pattern-based kill of a shared process
  name), **format-on-write** (runs the project's configured formatter
  on each file the agent writes — oxfmt/prettier/biome/ruff/black/gofmt/rustfmt; never
  blocks).
- **Git-hooks:** 4 templates — pre-commit, commit-msg, pre-push, no-secrets-scan (+ a
  lefthook.yml) — plus a global dispatcher (`global-dispatcher/`: `run-global-hooks`,
  `install-local-hooks.sh`, `hooks-sweep`, and a `global-hooks.d/` drop-in tree) that runs
  every global hook in every repo regardless of lefthook/husky `core.hooksPath` overrides.
- **CI:** 12 slots — secret-scan (gitleaks), codeql (incl. private-repo self-gate), sast
  (semgrep), dependency-review (+ license + multi-ecosystem audit), ai-review (configurable
  reviewer), copilot-findings (surface + optional gate), review-threads (unresolved-comment
  block), pr-checklist (unchecked-checkbox block), screenshots (mandatory image on UI PRs),
  pr-title-lint (Conventional Commits), leftover-grep (no `.only`/`debugger`/`console`/
  untracked-TODO), ship (green-CI-gated merge + cleanup). Plus a backlog of net-new ideas in
  `ci/README.md` (coverage-delta, bundle-size, doc-link-check, CODEOWNERS, stale-PR,
  size-label).
- **MCP:** a code-search slot (`review` is a CLI+skill, not an MCP — see [`mcp/README.md`](mcp/README.md)).

## How agent-tools compares

The closest neighbours each cover *one* carrier. **Git-hook managers** (pre-commit,
lefthook, husky) install commit/push-time checks — and nothing fires earlier or later than
that. **Curated skill lists** (awesome-claude-code and the many `everything-claude-code` /
`.claude-templates` collections) are link directories of prompts and configs, almost always
Claude-Code-specific, with no enforcement at all.

`agent-tools` spans all three carriers — **skills** (judgment rules an agent reads),
**agent-hooks** (block a tool call *mid-session*, before the side effect), and **git-hooks**
(mechanical backstop at commit/push) — plus **CI gates** and **MCP slots**, and a shared
`agenttools_log` lib. Everything is **harness-agnostic** (not tied to one agent) and
**generic** (no project assumptions; the rationale travels).

| Project | Git hooks | Agent-hooks (mid-session block) | Advisory skills | CI gates | MCP slots | Harness-agnostic | Generic / portable |
|---|---|---|---|---|---|---|---|
| **agent-tools** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| pre-commit | ✓ | — | — | ~ (CI action) | — | n/a | ✓ |
| lefthook | ✓ | — | — | ~ | — | n/a | ✓ |
| husky | ✓ | — | — | — | — | n/a | ~ (Node-centric) |
| awesome-claude-code (+ similar lists) | ~ (links) | ~ (links) | ✓ (links) | — | ~ (links) | — (Claude-only) | ~ (curated, not generalized) |

`~` = partial, `n/a` = not an agent tool. Hook managers are excellent at the commit gate
but cannot stop a `--no-verify` bypass or a secret being written *mid-session* — that needs
an agent-hook. Skill lists supply ideas but no mechanism. `agent-tools` is the only one here
that carries advice, mid-session enforcement, and the commit/CI backstop together, and is
consumed declaratively by [`rig`](https://github.com/alex-mextner/rig-cli).

## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — simple Telegram CLI to send messages, photos & files, and a two-way agent bridge (reports, Q→buttons, voice/rich)
- **[review-cli](https://github.com/alex-mextner/review-cli)** — multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.
- **[research-cli](https://github.com/alex-mextner/research-cli)** — multi-provider research/panel CLI on the shared providers engine: ask a question, get a synthesized answer from a panel of models. A distinct tool from review-cli — research, not code review. Spun out of this umbrella ([#125](https://github.com/alex-mextner/agent-tools/issues/125)); it vendors `agenttools_providers` + `agenttools_errors` + `models.yaml` with a drift-guard sync-check (strategy B), kept single-source against this repo's `lib/` canonical.
- **[rig-cli](https://github.com/alex-mextner/rig-cli)** — umbrella dev-env driver: sets up a repo from config — skills, hooks, CI, dep-bootstrap; reconciles drift
- **[task-cli](https://github.com/alex-mextner/task-cli)** — the enforced ticket interface: every request becomes a well-formed ticket, with acceptance criteria, motivation, user-impact & screenshots enforced by the tool. Backends: GitHub Issues (default) and Linear.
- **[pm-cli](https://github.com/alex-mextner/pm-cli)** — autonomous project-manager coordinator over the task/tg/rig ecosystem: an event-sourced work queue projected from an append-only JSONL log. Release 1 observes and reconciles on unforgeable evidence; it does not dispatch or edit code.
- **`dev`** (`lib/agenttools_dev/`) — project-scoped dev/e2e process runner shipped in this repo: runs a repo's `rig.yaml` scripts and manages background dev servers / e2e jobs, with a safe-by-default `dev stop` that only kills runners scoped to the current repo. See [`lib/agenttools_dev/README.md`](lib/agenttools_dev/README.md).
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.

Each CLI registers a skill into your agent harnesses (`<tool> install-skill`) so agents know it exists — see Install.

## License

MIT — see [LICENSE](LICENSE).
