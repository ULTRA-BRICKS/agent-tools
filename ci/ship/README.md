# ship — green-CI-gated PR merge + cleanup

> **Agents: the sanctioned interface is `gh ship <PR>`** (a gh alias → the repo's provisioned
> `pr-ship.sh`). Never invoke this `ship.sh`/`pr-ship.sh` by path and never hand-roll a merge
> script — `gh ship` is the single front door. This file documents the underlying behavior;
> `ship.sh` is the source/template that `rig apply` provisions as `pr-ship.sh`.

`ship.sh <PR>` is a portable "merge this PR safely, then clean up" helper. It is **not** a
CI workflow — it's a local/CI command you run to merge a ready PR. Before it merges, it
runs the same preflights the CI gates in this directory enforce, so you can't merge a PR
that isn't actually ready even with a one-liner.

## What it refuses to merge

| Refusal | Why |
| ------- | --- |
| PR not OPEN / CONFLICTING / BEHIND base | Not mergeable / ruleset wants up-to-date branch. |
| **No CI checks at all** | "No CI" is a *failed* gate, not a pass — ship refuses and tells you to set CI up first (`rig apply` provisions the gates, or add workflows). Override only with `--skip-ci`. |
| Any CI check failing | The green-CI gate (gates on ALL checks). |
| CI still running | Ship **watches** pending checks to completion (polls every `SHIP_CI_POLL`s up to `SHIP_CI_WAIT`s) instead of refusing — you don't babysit. |
| Unresolved review threads | Same check as [`../review-threads/`](../review-threads/). |
| **PR younger than the review-dwell window** | Closes the premature-merge gap: the unresolved-threads check above only fails when threads *already exist*, so "0 unresolved threads" is **vacuously true** before any review has posted — a PR opened and shipped within seconds passes it without a single question forming. The dwell gate refuses until at least `SHIP_REVIEW_DWELL` seconds (default **600 / 10 min**) have elapsed since the PR's last code push, giving async review (multi-model / CI-AI / human) time to form its comments (which then become threads the check above forces resolved). Runs **independently of `--skip-ci`**. Window starts at `max(createdAt, head-commit committedDate)` so a new push restarts it. Disable with `SHIP_REVIEW_DWELL=0`; override one ship with `--no-review-dwell-ok <reason>` (logged). Fail-closed: unreadable/unparseable timestamps refuse. |
| UI-touching PR with no screenshot | Same check as [`../screenshots/`](../screenshots/); override with `--no-screenshot-ok`. |
| **Shippable source changed but the version is UNCHANGED** | A ship of source is a release; the declared version (`pyproject.toml` `[project] version`, dynamic pyproject sources such as `VERSION` or `__init__.py`, Poetry `[tool.poetry] version`, or `package.json` `"version"`) must be bumped so `--version` stays a real freshness signal (skill: `bump-version-on-release`). Docs-only / pure test/CI PRs are exempt. Override a genuine no-release ship with `--no-version-bump-ok <reason>` (or `SHIP_SKIP_VERSION_BUMP=1`). |
| **Review-quorum bar not met** (Guard-B, self-merge-authority) | The gate that makes self-merge *"strictly controlled"*. Before merging, ship derives the PR's task code (`$REVIEW_TASK_CODE`, else a `HYP-<n>`/uppercase ticket token from the branch name, else the PR body) and asks review-cli whether that task has >= `SHIP_REVIEW_QUORUM_MIN_ITER` recorded review iterations across >= `SHIP_REVIEW_QUORUM_MIN_MODELS` distinct models (`review task <code> --check`, falling back to `--quorum-check` on an older review-cli). Bar met -> prints `AUTHORITY CONFIRMED` and proceeds. Bar not met, no task code, `review`/`jq` missing, or the store unreadable -> **fail-closed refuse**. There is **NO self-service override flag**: a one-time bypass is requested by setting `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"`, which asks Alex **live on Telegram** (via the shared `agenttools_hatch_escalation` lib) and proceeds ONLY on his real-time approval — a blank/bare value is denied. Runs **independently of `--skip-ci`**. Every non-dry-run gated ship appends an audit line (`authorized` / `bypass:approved` / `bypass:denied` / `refused`) to `SHIP_AUDIT_FILE`; `--dry-run` prints the would-be audit instead. Disable the whole gate with `SHIP_REVIEW_QUORUM=0`. |
| Local branch has unpushed/diverged commits, or dirty worktree | Avoids merging stale/uncommitted local state. |

Then it squash-merges, deletes the remote branch, removes the local branch+worktree
(unless you're *inside* that worktree — then it's left so your session keeps a cwd), and
fast-forwards your main checkout.

When ship decides CI is structurally unavailable and runs its local fallback gates, the
test runner first prefers `dev run --repo-only test` if the repo has a committed `rig.yaml`,
that file declares top-level `scripts.test`, and `dev` is on PATH. Probe/runtime errors from
`dev has-script --repo-only test` block instead of silently guessing a fallback runner. If
the repo script is absent, ship falls back to the portable pyproject/package/Cargo
auto-detection in `ship.sh`. If `dev` is installed, install it with its runtime dependencies
(`agenttools-config`/PyYAML); repos with `rig.yaml` fail closed on dependency or config errors
so a declared script is not bypassed by auto-detection. A PATH `dev` must answer the hidden
`dev --agenttools-dev-probe`; unrelated tools named `dev` are ignored and ship falls back to
stack detection.

## Quick start

```bash
cp ci/ship/ship.sh ~/bin/ship && chmod +x ~/bin/ship    # or wherever your PATH points
ship 123                                                # merge PR #123 when green
ship 123 --dry-run                                      # show what it would do
ship 123 --screenshot ./after.png "new dialog"          # attach + post visual proof
```

Wire it as a `gh` alias if you like:

```bash
gh alias set ship '!bash ~/bin/ship'    # then: gh ship 123
```

> **Hatch bypass needs the sibling `lib/`.** The review-quorum gate's one-time Telegram bypass
> imports `agenttools_hatch_escalation` from `lib/` two levels above `ship.sh` in the checkout.
> The sanctioned `gh ship` → `pr-ship.sh` delegator runs the **canonical catalog** `ci/ship/ship.sh`
> (resolved from the repo checkout or `AGENT_TOOLS_ROOT`), so `lib/` is present and the bypass works.
> A bare `cp ci/ship/ship.sh ~/bin/ship` copy has no sibling `lib/`, so a bypass request from it
> **fails closed** (the gate still enforces; you just can't hatch-bypass) — run `ship` from the
> checkout, or point `AGENT_TOOLS_ROOT` at it, to use the bypass.

> **Trust model — run `gh ship` from a protected checkout.** Every gate here (CI, threads,
> version-bump, review-quorum) trusts the ship PROCESS's execution environment: the tools it
> resolves on `PATH` (`gh`, `git`, `jq`, `python3`, `review`, `tg-ctl`) and the `ci/ship/ship.sh`
> + `lib/` code it runs. A party who controls that environment — a hostile `PATH`, or a PR
> worktree whose own `ci/ship/ship.sh` is executed — can weaken any gate, not just the hatch.
> So `gh ship` is meant to run from the **orchestrator's canonical/main checkout**, never from an
> untrusted PR worktree. Within that trust model the review-quorum bypass is closed: nothing the
> PR contents or the `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` value carries can forge approval — the
> lib loads by fixed file path under `python3 -I`, tg-ctl resolves off the OS-identity home, and
> approval requires the helper's explicit stdout sentinel (a benign/broken `python3` fails closed).

## All project coupling is OPTIONAL (env knobs)

Nothing org-/tracker-/layout-specific is hard-coded. Configure via env:

| Env | Default | Purpose |
| --- | ------- | ------- |
| `SHIP_DEFAULT_BRANCH` | `main` | Base branch. |
| `SHIP_MERGE_METHOD` | `squash` | `squash` / `merge` / `rebase`. |
| `SHIP_CI_WAIT` | `900` | Max seconds to WATCH pending CI to completion before giving up. |
| `SHIP_CI_POLL` | `20` | Poll interval (seconds) while watching pending CI. |
| `SHIP_CI_GRACE` | `45` | Grace window for checks to *register* on a fresh PR before concluding "no CI" (an empty rollup is briefly normal right after opening a PR). |
| `SHIP_MAIN_CHECKOUT` | first worktree | Where to fast-forward after merge. |
| `SHIP_UI_PATH_REGEX` | common FE paths | What makes a PR "UI-touching". **Set empty to disable the screenshot gate.** |
| `SHIP_IMAGE_UPLOAD_CMD` | (unset) | Optional uploader for `--screenshot`: a command that takes the image (`{FILE}` token or `$1`) and prints a public URL. Without it, `--screenshot` just embeds a local-path note. |
| `SHIP_SKIP_VERSION_BUMP` | (unset) | `=1` overrides the version-bump gate (env equivalent of `--no-version-bump-ok`). |
| `SHIP_VERSION_FILES` | auto-detect | Space-separated version sources to check (relative to repo root). Use `path::ATTR` for a Python module attribute. Default: static `pyproject.toml`, dynamic pyproject sources (`VERSION`, package `__init__.py`, and setuptools `[tool.setuptools.dynamic]` / `[tool.setuptools.dynamic.version]` `attr`/`file` config), Poetry `[tool.poetry] version`, then `package.json` at the root. Set for a non-standard layout. The lightweight TOML reader supports single-line setuptools inline tables (`version = {attr = "pkg.VERSION"}` / `{file = "VERSION"}`) or the `[tool.setuptools.dynamic.version]` subtable; multi-line inline tables should use `SHIP_VERSION_FILES` or the subtable form. |
| `SHIP_REVIEW_DWELL` | `600` | Minimum seconds since the PR's last code push before a merge is allowed, so async review has time to **form** its comments. `0` disables the gate. |
| `SHIP_REVIEW_QUORUM` / `SHIP_REVIEW_QUORUM_ENABLED` | enabled | Set either to `0` to disable the review-quorum gate (Guard-B) entirely (ops off-switch). |
| `SHIP_REVIEW_QUORUM_MIN_ITER` | `3` | Quorum floor: recorded review-cli iterations for the task. |
| `SHIP_REVIEW_QUORUM_MIN_MODELS` | `3` | Quorum floor: distinct models across those iterations. |
| `REVIEW_TASK_CODE` | (auto) | The PR's task code for the quorum gate. Unset → derived from a `HYP-<n>`/uppercase ticket token in the branch name, then the PR body; none found → fail-closed refuse. |
| `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` | (unset) | One-time bypass request for a not-met quorum bar: set to a written justification to ask Alex live on Telegram (shared `agenttools_hatch_escalation` lib); proceeds ONLY on his real-time approval. Under `--dry-run`, the helper reports the would-be request but does not contact `tg-ctl` or write a bypass audit line. Blank/bare values denied. **No self-service reason flag exists.** The lib is imported from a fixed path relative to `ship.sh`, and tg-ctl is resolved from a rig.yaml in the OS account's **real home** (`pwd.getpwuid` — **not** the `$HOME` env var, **not** the PR's repo) then a trusted-paths allowlist. So neither an env var (including a doctored `HOME`) nor a rig.yaml the PR commits can redirect approval to a stub. |
| `SHIP_AUDIT_FILE` | `~/.config/agent-tools/ship-audit.jsonl` | JSONL audit log; one line per non-dry-run gated ship (`authorized` / `bypass:approved` / `bypass:denied` / `refused`). |

## Flags

- `--skip-ci` — admin-merge bypassing the green-CI gate (use only when CI is billing-blocked
  or stuck; the other preflights still run).
- `--dry-run` — print, change nothing.
- `--no-screenshot-ok <reason>` — override the UI screenshot requirement, logged.
- `--no-version-bump-ok <reason>` — override the version-bump requirement for a genuine
  no-release ship (docs-only, pure test/CI, a revert), logged.
- `--no-review-dwell-ok <reason>` — fast-track past the review-dwell window for a genuine
  trivial/urgent merge, logged (the reason is printed to the ship log).
- `--screenshot <path> [desc]` — upload (via `SHIP_IMAGE_UPLOAD_CMD`) and post a screenshot
  as a PR comment; repeatable.

The **review-quorum gate (Guard-B) has no override flag** by design — a bypass goes through live
Telegram approval to Alex via `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"` (see the env
table above), never a self-service reason. The hatch imports the shared `agenttools_hatch_escalation`
lib from `lib/` **relative to `ship.sh`'s location in the checkout**, so run `ship` from within the
agent-tools checkout (or via `gh ship` pointed at it) to use the hatch. A bare `cp ci/ship/ship.sh
~/bin/ship` copy can still *enforce* the gate but can't reach the lib, so a bypass request from a
detached copy **fails closed** (refuses) — that's intentional, not a bug.

## What was stripped vs an internal version

This is generalized from a real in-house ship script. Removed/parameterized: any issue-
tracker coupling (ticket-id extraction, attaching proof to a tracker — replaced by the
generic `SHIP_IMAGE_UPLOAD_CMD` hook), hard-coded front-end path layout (now
`SHIP_UI_PATH_REGEX`), and any org/branch assumptions (now `SHIP_DEFAULT_BRANCH`). If you
want tracker integration back, set `SHIP_IMAGE_UPLOAD_CMD` to your tracker's attach command.

## When to use

When you merge PRs from the CLI and want the merge to be impossible unless the PR is green,
its threads resolved, a review-dwell window elapsed (so review questions had time to form),
and (for UI) its screenshot present — without trusting yourself to remember. The CI workflows
in this directory are the server-side backstop; `ship` is the client-side gate.
