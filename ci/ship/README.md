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
| **Shippable source changed but the version is UNCHANGED** | A ship of source is a release; the declared version (`pyproject.toml` `version`/`package.json` `"version"`) must be bumped so `--version` stays a real freshness signal (skill: `bump-version-on-release`). Docs-only / pure test/CI PRs are exempt. Override a genuine no-release ship with `--no-version-bump-ok <reason>` (or `SHIP_SKIP_VERSION_BUMP=1`). |
| Local branch has unpushed/diverged commits, or dirty worktree | Avoids merging stale/uncommitted local state. |

Then it squash-merges, deletes the remote branch, removes the local branch+worktree
(unless you're *inside* that worktree — then it's left so your session keeps a cwd), and
fast-forwards your main checkout.

When ship decides CI is structurally unavailable and runs its local fallback gates, the
test runner first prefers `dev run test` if `rig.yaml` declares top-level `scripts.test`
and `dev` is on PATH. If not, it falls back to the portable pyproject/package/Cargo
auto-detection in `ship.sh`.

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
| `SHIP_VERSION_FILES` | auto-detect | Space-separated version files to check (relative to repo root). Default: `pyproject.toml` then `package.json` at the root. Set for a non-standard layout. |
| `SHIP_REVIEW_DWELL` | `600` | Minimum seconds since the PR's last code push before a merge is allowed, so async review has time to **form** its comments. `0` disables the gate. |

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
