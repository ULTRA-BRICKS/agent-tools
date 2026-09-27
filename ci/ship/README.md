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
| **No CI checks at all** | "No CI" is a *failed* gate, not a pass — ship refuses and tells you to set CI up first (`rig apply` provisions the gates, or add workflows). Override only with `--skip-ci`, which is **deny-by-default** (needs a one-time live Telegram approval — see below). **Exception — CI outage:** if the empty rollup is a genuine outage (a `pull_request`-triggered workflow exists on the PR's **pushed base branch** — `origin/<baseRefName>` — but registered no checks, i.e. billing suspended / Actions down), ship runs the local fallback gate below instead of refusing, and — only if it is fully green — falls through to the **normal (non-admin) merge** (the same posture as the checks-failed structural-outage path; a branch-protection ruleset still gates it, and if required checks are genuinely absent GitHub blocks the merge so you `--skip-ci` deliberately — ship never silently `--admin`-bypasses here). A failed/unreadable rollup query is NOT an empty rollup — an unknown remote state hard-refuses (never merges). A repo whose workflows never trigger on PRs (push/schedule only) is NOT an outage and still hard-refuses. The signal is read from the pushed `origin/<base>` ref, not the local worktree, so an untracked/unpushed workflow cannot trigger the exception. **Trigger detection is a heuristic** over a YAML subset: it recognises an unquoted top-level `on:` naming `pull_request`/`pull_request_target` inline, as a block key, or as a list item; a `pull_request` trigger **narrowed by `branches:`/`paths:`/`types:` filters is still treated as an outage** (the local gate then runs, so the merge is verified — it is only more permissive about substituting local gates for absent remote checks), and quoted-key (`"on":`) / flow-mapping (`on: { … }`) forms conservatively hard-refuse (the safe direction). |
| Any CI check's latest run failing | The green-CI gate (gates on ALL checks, by each check's LATEST run — the rollup is deduped to the newest run per logical check, so a stale FAILURE from a re-run that has since gone green does not block, matching GitHub's `mergeStateStatus`). **Exception — a confirmed pre-existing flake:** below the ~80% "CI is structurally down" threshold (see `ci_appears_structurally_down` above), the far more common shape is "one specific check is a flaky, already-broken-on-main test and everything else is green" — that never looks like an outage, so without help the only way through used to be a human escalation for something a quick check of the base branch's own recent CI history already answers. Pass `--known-flake <check-name>` (repeatable, once per currently-failing check) to name the claim; ship does **not** trust it — it independently queries the last `SHIP_FLAKE_LOOKBACK_RUNS` (default 5) completed runs of the SAME workflow on the PR's own base branch and requires a job of that exact name to have also FAILED in at least one of them. Every currently-failing check must be BOTH asserted AND confirmed this way, or the gate still hard-refuses (no partial credit) — an assertion that doesn't check out is refused identically to not passing the flag at all. On success it runs the SAME local fallback gate as the CI-down path and merges only if that is green too — a confirmed flake is not a free pass on testing. Logged to `SHIP_AUDIT_FILE` (`gate:"known-flake"`, `decision: confirmed`/`confirmed-local-gate-failed`/`refused` — `confirmed` is written only after an actual merge, never for a run that stopped short). Scope, stated plainly: this confirms the check NAME has also failed recently on the base branch — job-name granularity, not proof the SAME sub-test failed both times (a monorepo-wide test job could in principle fail for two different reasons); the refusal message that leads you here always prints exactly what failed, so look before asserting. **Known gap:** if the flaky check is also a *required* status check under branch protection, GitHub can still refuse the final `gh pr merge` even after this gate and the local tests pass (a required check FAILING still blocks, unlike the empty-rollup case above where nothing failed, it was merely absent) — you'd land at the `--skip-ci` hatch anyway. Not unsafe, just an unresolved UX gap. |
| CI still running | Ship **watches** pending checks to completion (polls every `SHIP_CI_POLL`s up to `SHIP_CI_WAIT`s) instead of refusing — you don't babysit. |
| Unresolved review threads | Same check as [`../review-threads/`](../review-threads/). Pass `--resolve-addressed-threads` to let ship first auto-close the threads that are safe without a human — unresolved + outdated (addressed by a later commit) + authored entirely by bots; human or unaddressed threads still block (see Flags, #268). |
| **PR younger than the review-dwell window** | Closes the premature-merge gap: the unresolved-threads check above only fails when threads *already exist*, so "0 unresolved threads" is **vacuously true** before any review has posted — a PR opened and shipped within seconds passes it without a single question forming. The dwell gate refuses until at least `SHIP_REVIEW_DWELL` seconds (default **600 / 10 min**) have elapsed since the PR's last code push, giving async review (multi-model / CI-AI / human) time to form its comments (which then become threads the check above forces resolved). Runs **independently of `--skip-ci`**. Window starts at `max(createdAt, head-commit committedDate)` so a new push restarts it. Disable with `SHIP_REVIEW_DWELL=0`; override one ship with `--no-review-dwell-ok <reason>` (logged). Fail-closed: unreadable/unparseable timestamps refuse. |
| **PR has ZERO GitHub-side reviews** | Closes a DIFFERENT gap from the two rows above: the unresolved-threads check and the dwell window are both vacuously satisfiable when literally nobody ever reviewed — dwell only buys *time* for a review to land, it never checks whether one actually did. `gh pr view --json reviews` must return at least one entry (any state — `APPROVED`/`COMMENTED`/`CHANGES_REQUESTED`/`DISMISSED` all count, any author including the PR's own — existence is the signal, not the verdict; a self-review defeats this the same way a shipper controlling the process can defeat any other gate here). Real incident: PR #764 in hyperide/hyper-saas merged via `gh ship` with zero reviews, on Guard-B (review-cli's automated pass) alone — a **separate** signal from an actual GitHub-side review. Runs **independently of `--skip-ci`**. Disable with `SHIP_EXTERNAL_REVIEW=0` as a **`.ship-config`** line **merged to the base branch** (env `SHIP_EXTERNAL_REVIEW=0` / `SHIP_EXTERNAL_REVIEW_ENABLED=0` is ignored — agent-tools#624; durable, per-repo, reviewed — agent-tools#600; read from `origin/<baseRefName>`, never from the shipping PR's own HEAD, so a PR cannot disable the gate on its own merge by adding the line to its own branch). There is **NO self-service override flag for a single PR** — a one-time bypass is requested via `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW="<justification>"` (same shared `agenttools_hatch_escalation` lib as the review-quorum/`--skip-ci` hatches; `ci/ship/external_review_hatch.py`). Audited to `SHIP_AUDIT_FILE` (`gate:"external-review"`, decision `external-review:refused` / `external-review:bypass:approved` / `external-review:bypass:denied`). A confirmed fleet-wide review-cli capacity outage (`review capacity-check --json` reports every configured provider `exhausted`, none `available`) auto-proceeds instead of prompting the one-time hatch, and is itself audited as `external-review:auto-proceed:capacity-outage`. |
| UI-touching PR with no screenshot | Same check as [`../screenshots/`](../screenshots/); override with `--no-screenshot-ok`. |
| **Shippable source changed but the version is UNCHANGED** | A ship of source is a release; the declared version (`pyproject.toml` `version`/`package.json` `"version"`, or the module's `__version__` for a dynamic pyproject version) must be bumped so `--version` stays a real freshness signal (skill: `bump-version-on-release`). Docs-only / pure test/CI PRs are exempt. Override a genuine no-release ship with `--no-version-bump-ok <reason>` (or `SHIP_SKIP_VERSION_BUMP=1`). |
| **Review-quorum bar not met** (Guard-B, self-merge-authority) | The gate that makes self-merge *"strictly controlled"*. Before merging, ship derives the PR's task code (`$REVIEW_TASK_CODE`, else a `HYP-<n>`/uppercase ticket token, else a purely descriptive ALL-CAPS/hyphenated code — `SME-ROADMAP-WORKTREE-NOTE`-shaped, 3+ segments, no digits — else, as the last fallbacks, a keyword-anchored GitHub issue reference — `Fixes #105`/`Refs #105`, kept literal as `#105` for repos that track work as plain GitHub issues; a bare `#105`, a cross-repo `org/repo#105`, a URL fragment, or two DISTINCT anchored refs in one text never qualify — and a full issue URL of the PR's **own** repo — `https://github.com/<owner>/<repo>/issues/105`, bare or as a markdown link `[#105](…)`, the shape task-cli's `links` gate demands; also `#105`. Another repo's issue URL, a `/pull/<n>` URL, or two distinct same-repo issue URLs never qualify (agent-tools#564). A Linear URL (`https://linear.app/<team>/issue/HYP-1440/…`) carries its code in the path and is caught by the `HYP-<n>`/uppercase arm — all tried against the branch name, then the PR body) and asks review-cli whether that task has >= `SHIP_REVIEW_QUORUM_MIN_ITER` **PASSED** review iterations across >= `SHIP_REVIEW_QUORUM_MIN_ROLES` distinct **BOARD ROLES** among those passed iterations (`review task <code> --check --min-roles N`, retrying without `--min-roles` if the installed review-cli predates role support — review-cli's own history shows no build ever supported both `--quorum-check` and `--min-roles`, so a role-less retry (still reporting real iteration/model data, honestly 0 roles) comes before the legacy `--quorum-check` fallback for pre-rename builds — a failed/degraded review does not count toward the bar). Role-based coverage is the **PRIMARY/default mechanism now** (matching review-cli's own default, review-cli#246) — `--min-roles` is always sent. `SHIP_REVIEW_QUORUM_MIN_MODELS` is enforced **ADDITIONALLY, only when the operator explicitly sets it** — there is no default model floor any more; ship sends `--min-models` to review-cli only in that case, mirroring review-cli's own explicit-vs-default AND logic so an explicit model-floor request can never be silently outvoted by role coverage (or the reverse). All floors are **clamped to a hard minimum of 3** (raise-only; a `0`/negative/below-3 value resolves to 3). Bar met -> ship **re-derives the verdict from the counts** (never trusts the subprocess's `passed` boolean alone), prints `AUTHORITY CONFIRMED`, and proceeds. Bar not met, a quorum reading 0 iterations / 0 distinct roles, no task code, `review`/`jq` missing, or the store unreadable -> **fail-closed refuse**. There is **NO self-service override flag**: a one-time bypass is requested by setting `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"`, which asks Alex **live on Telegram** (via the shared `agenttools_hatch_escalation` lib) and proceeds ONLY on his real-time approval — a blank/bare value is denied. Runs **independently of `--skip-ci`**. Every non-dry-run gated ship appends an audit line (`authorized` / `bypass:approved` / `bypass:denied` / `refused`, now carrying `roles`/`min_roles`/`min_models` alongside `models` — `min_models` reads `0` when the model floor was not enforced) to `SHIP_AUDIT_FILE`; `--dry-run` prints the would-be audit instead. Disable the whole gate with `SHIP_REVIEW_QUORUM=0`. A confirmed fleet-wide review-cli capacity outage (`review capacity-check --json` reports every configured provider `exhausted`, none `available`) auto-proceeds instead of prompting the one-time Telegram hatch, and is itself audited as `auto-proceed:capacity-outage`. |
| **Magic-close keyword in the PR title/body/commits** (`Closes #115`, `Fixes HYP-1295`, `Fixes https://github.com/acme/widgets/issues/115`, …) | GitHub's `close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved` (+ Linear's `-ing` forms) followed by `#N`, `owner/repo#N`, a ticket code (`ABC-123`), or a full `https://…/issues/N` or `…/pull/N` URL (GitHub documents the URL form as an equally valid close target) makes GitHub / Linear's GitHub integration move the ticket to **Done the instant the PR merges** — behind task-cli's close gates, so the ticket ends up Done with empty checkboxes (127 of 435 HYP tickets since June 2026; HYP-1440, HYP-1347, HYP-1295). Scans the PR title, body, **and every commit message** (GitHub's default squash-message template includes each commit's message in the final commit body, so a clean title/body with a keyword buried in one commit can still close the ticket). Ship refuses and prints the exact phrase(s) (`in the body: "Closes #115"`, `in a commit: "Closes HYP-1295"`) with the fix: write **`Refs <ref>`** (links without closing). `--rewrite-magic-close` rewrites a title/body keyword to `Refs` via `gh pr edit` and continues (audited, `decision: rewritten`) — a keyword found in a COMMIT message has **no automatic fix** (this script never rewrites or rebases a branch) and **still refuses even when a title/body keyword coexists**: the title/body rewrite still lands (harmless, worth keeping), but rewriting the PR text does nothing to stop the un-rewritable commit message from closing the ticket, so the ship is refused either way, with the commit needing a manual amend. A `gh pr view` fetch failure (rate limit, network blip) **fails closed** — refused, never treated as "no keyword found". Detection is whitespace-normalized (a linebreak between keyword and reference — `"Fixes\n#115"` — still matches, since GitHub's own parser is whitespace-tolerant there); before ever calling `gh pr edit`, ship checks the REWRITE RESULT itself and refuses instead of editing if a keyword would still be present (the line-based rewrite sed can't reach a cross-line phrase) — a local check, not a post-edit re-fetch, so it behaves identically under `--dry-run` and has nothing to fail open on if GitHub is briefly unreachable. Case-insensitive; a colon between keyword and reference is NOT a close (`fix: HYP-1295 …` conventional titles pass). Runs under `--dry-run` (same refusal; the edit is only printed). Audited to `SHIP_AUDIT_FILE` (`gate:"magic-close"`, `refused` / `rewritten`, matched phrases in `detail`). Disable with `SHIP_MAGIC_CLOSE_GATE=0`. |
| **Ticket not accepted** (`task gate <code>` exit 1) | The pre-merge **acceptance gate** (task-cli#115 / #521). When task-cli is on PATH and a ticket code is derivable (branch → PR title → PR body — the same derivation the post-merge notify uses), ship runs `task gate <code> --json` BEFORE merging and refuses while the ticket has **unchecked** criteria or criteria **checked without a proof** — printing them (`[3] survives a restart (unchecked)`) with the fix (`task accept <code>` / `task check <code> <n> … --proof …`). A ticket whose acceptance is inherently post-merge (a release publish) records `task change <code> --post-merge-acceptance "<reason>"` ON the ticket: the gate then passes and the reason lands in the audit line (`decision: authorized:post-merge-opt-out`). task-cli absent / foreign `--repo` / `task gate` exit 2 (could not evaluate — unknown ticket, backend error) → logged **skip**, never a refusal. No derivable code is a **refusal** (see the row below), not a skip. `task` is an AMBIGUOUS binary name (Taskwarrior, go-task also install as `task`) — an exit 0 OR 1 whose output does NOT carry task-cli's own JSON shape (`id`/`ok`/`criteria`) is treated as "not task-cli", not a genuine acceptance or refusal, and skips instead of blocking every merge (or, worse, silently authorizing one). Runs under `--dry-run`. Disable with `SHIP_ACCEPTANCE_GATE=0` (env, or a committed `.ship-config` line). Contract below. |
| **No ticket / no acceptance** | When task-cli is on PATH, a PR with **no derivable ticket code** is refused — a PR with no ticket has no acceptance criteria, so nothing can be proven (agent-tools#639). Create a ticket (`task create`) and reference it (`Refs #N` / `Refs HYP-N`). Repos that never installed task-cli still skip. |
| Local branch has unpushed/diverged commits, or dirty worktree | Avoids merging stale/uncommitted local state. |

Then it squash-merges, deletes the remote branch, removes the local branch+worktree
(unless you're *inside* that worktree — then it's left so your session keeps a cwd), and
fast-forwards your main checkout.

When ship decides CI is structurally unavailable and runs its local fallback gates, the
test runner picks a command in this order (highest priority first):

1. **`SHIP_LOCAL_TEST_CMD` env var** — test-only escape hatch, never set in production.
2. **`.ship-config` file, committed at the repo root** — an audited, per-repo override for
   repos where auto-detection can't guess correctly (e.g. a monorepo-of-fixtures whose real
   suite lives in a subdirectory, like a project with no root-level manifest). Simple
   `KEY=value` lines. Two of its four whitelisted keys drive THIS local-test-command
   selection: `SHIP_LOCAL_TEST_DIR=<path>` (a repo-relative subdirectory to run the command
   from, or to scope auto-detection to) and `SHIP_LOCAL_TEST_CMD=<cmd>` (a command line,
   eval'd the same way as the env var of the same name — this is just a committed, per-repo
   source for it). The other two, `SHIP_ACCEPTANCE_GATE=0` and `SHIP_EXTERNAL_REVIEW=0`, are
   unrelated ops off-switches for two different gates — see the knobs table and the refusal
   table above. These local-test-command keys and `SHIP_ACCEPTANCE_GATE` are read from the
   last **committed** content at this checkout's own `HEAD` (`git show HEAD:.ship-config`),
   never the working tree, so an uncommitted/staged-only edit is not honored — the "audited"
   claim is an enforced property. `SHIP_EXTERNAL_REVIEW` is the one exception: it is read from
   the PR's **base branch** instead of HEAD (see the knobs table above) — a merge-safety
   off-switch must not be settable by the PR it is about to wave through. `SHIP_LOCAL_TEST_DIR`
   is rejected (and invalidates the whole file) if it's absolute, contains a `..` path
   component, or resolves to the repo root itself.
3. **`dev run --repo-only test`** if the repo has a committed `rig.yaml`, that file declares
   top-level `scripts.test`, and `dev` is on PATH. Probe/runtime errors from
   `dev has-script --repo-only test` block instead of silently guessing a fallback runner.
   If `dev` is installed, install it with its runtime dependencies (`agenttools-config`/
   PyYAML); repos with `rig.yaml` fail closed on dependency or config errors so a declared
   script is not bypassed by auto-detection. A PATH `dev` must answer the hidden
   `dev --agenttools-dev-probe`; unrelated tools named `dev` are ignored and ship falls back
   to stack detection.
4. **Root-level auto-detection** — the portable pyproject/package/Cargo detection in
   `ship.sh`, at the repo root.
5. **`e2e/` subdirectory auto-detection** — the same three manifests, one level down, in
   `e2e/` ONLY. `test/` and `tests/` are deliberately never auto-probed (those directory
   names are also where repos most often keep fixture manifests unrelated to the real
   suite) — use `.ship-config` for any other/ambiguous subdirectory layout.

If nothing above matches, ship fails closed with "no recognized test runner found" rather
than merging unverified.

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
| `SHIP_FLAKE_LOOKBACK_RUNS` | `5` | For the `--known-flake` gate: how many recent COMPLETED runs of the same workflow on the base branch to search for a matching failure. A match in any of them is enough. |
| `SHIP_MAIN_CHECKOUT` | first worktree | Where to fast-forward after merge. |
| `SHIP_UI_PATH_REGEX` | common FE paths | What makes a PR "UI-touching". **Set empty to disable the screenshot gate.** |
| `SHIP_IMAGE_UPLOAD_CMD` | (unset) | Optional uploader for `--screenshot`: a command that takes the image (`{FILE}` token or `$1`) and prints a public URL. Without it, `--screenshot` just embeds a local-path note. |
| `SHIP_SKIP_VERSION_BUMP` | (unset) | `=1` overrides the version-bump gate (env equivalent of `--no-version-bump-ok`). |
| `SHIP_VERSION_FILES` | auto-detect | Space-separated version files to check (relative to repo root). Default: `pyproject.toml` then `package.json` at the root. Set for a non-standard layout. A `pyproject.toml` with `dynamic = ["version"]` (setuptools `version = { attr = "pkg.__version__" }`, hatch `[tool.hatch.version] path`) resolves to the module that holds `__version__` (flat or `src/` layout). A dynamic source ship cannot read (a module elsewhere, an attribute other than `__version__`, a computed `__version__` with no literal `"X"`, a setuptools `file`, a custom hatch `pattern`, both a setuptools attr and a hatch path) is refused with the reason: pin the module here, or override. The gate matches lines, not Python: a `__version__ = "X"` line inside a docstring reads like the real one, so it catches a forgotten bump, not a staged decoy. |
| `SHIP_REVIEW_DWELL` | `600` | Minimum seconds since the PR's last code push before a merge is allowed, so async review has time to **form** its comments. `0` disables the gate. |
| `SHIP_EXTERNAL_REVIEW_ENABLED` / `SHIP_EXTERNAL_REVIEW` | ignored as env | Env `=0` no longer disables the external-review gate (agent-tools#624 — the ambient-export hole #528/#609 left open). Durable disable is a `SHIP_EXTERNAL_REVIEW=0` `.ship-config` line already merged to the PR's **base branch** (`origin/<baseRefName>`, fetched fresh), never the PR's own HEAD: reading it from HEAD would let a PR disable the gate on its own merge by committing the line to its own branch, recreating PR #764 inside the fix for #764. The line only takes effect once it has itself been merged to the base through a normal, reviewed PR. One-time bypass: `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW`. |
| `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW` | (unset) | One-time bypass request for the external-review gate, same shape/hardening as `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` — see `ci/ship/external_review_hatch.py`. |
| `SHIP_REVIEW_QUORUM` / `SHIP_REVIEW_QUORUM_ENABLED` | enabled | `0`/`false`/`no` is **forbidden** (agent-tools#644) and is rejected at parse — the gate stays enabled. There is no ops off-switch. One-time bypass: `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM`. |
| `SHIP_REVIEW_QUORUM_MIN_ITER` | `3` | Quorum floor: PASSED review-cli iterations for the task. **Clamped to a hard minimum of 3** — this knob can only RAISE the bar, never lower it (an unset / `0` / negative / below-3 value resolves to 3, fail-closed #242). |
| `SHIP_REVIEW_QUORUM_MIN_ROLES` | `3` | Quorum floor: distinct **BOARD ROLES** across those PASSED iterations. **Clamped to a hard minimum of 3**, same as `MIN_ITER`. This is the **PRIMARY/default gate mechanism** — always enforced, matching review-cli's own default (review-cli#246). |
| `SHIP_REVIEW_QUORUM_MIN_MODELS` | `3` if set, else unenforced | Quorum floor: distinct models across those PASSED iterations. **Only takes effect if you explicitly set this env var** — there is no default model floor any more (role-based coverage is the default); `3` is merely the clamp floor the value resolves to once you opt in, same as `MIN_ITER`. Setting it to ANY value — including `0` or blank — counts as opting in and is clamped up to 3 (never treated as "explicitly disabled"; only leaving the var **completely unset** skips the model floor). When set, ship also passes `--min-models` to review-cli, so BOTH floors are required together (mirrors review-cli's own explicit-vs-default AND logic, review-cli#246). |
| `REVIEW_TASK_CODE` | (auto) | The PR's task code for the quorum gate. Unset → derived from a `HYP-<n>`/uppercase ticket token, then a purely descriptive ALL-CAPS/hyphenated code (`SME-ROADMAP-WORKTREE-NOTE`-shaped, 3+ segments, no digits), then — last — a keyword-anchored GitHub issue reference (`Fixes #105`/`Refs #105` → literal `#105`; two distinct anchored refs are ambiguous and derive nothing) or a full issue URL of the PR's own repo (`https://github.com/<owner>/<repo>/issues/105` → literal `#105`; another repo's issue, a `/pull/<n>` URL, or two distinct same-repo issues derive nothing). A Linear URL (`https://linear.app/<team>/issue/HYP-1440/…`) is caught by the `HYP-<n>` arm. Tried against the branch name then the PR body; none found → fail-closed refuse. |
| `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` | (unset) | One-time bypass request for a not-met quorum bar: set to a written justification to ask Alex live on Telegram (shared `agenttools_hatch_escalation` lib); proceeds ONLY on his real-time approval. Under `--dry-run`, the helper reports the would-be request but does not contact `tg-ctl` or write a bypass audit line. Blank/bare values denied. **No self-service reason flag exists.** The lib is imported from a fixed path relative to `ship.sh`, and tg-ctl is resolved from a rig.yaml in the OS account's **real home** (`pwd.getpwuid` — **not** the `$HOME` env var, **not** the PR's repo) then a trusted-paths allowlist. So neither an env var (including a doctored `HOME`) nor a rig.yaml the PR commits can redirect approval to a stub. |
| `SHIP_ACCEPTANCE_GATE` | enabled | Set to `0` to disable the pre-merge acceptance gate (`task gate`) — ops off-switch, also accepted as a committed `.ship-config` line. The per-ticket opt-out is `task change <code> --post-merge-acceptance "<reason>"`, not this. |
| `SHIP_MAGIC_CLOSE_GATE` | enabled | Set to `0` to disable the magic-close keyword gate. |
| `SHIP_AUDIT_FILE` | `~/.config/agent-tools/ship-audit.jsonl` | JSONL audit log; one line per non-dry-run gated ship. Review-quorum decisions: `authorized` / `bypass:approved` / `bypass:denied` / `refused` (with a `task_code`, plus `roles`/`min_roles`/`models`/`min_models` — `min_models` is `0` when the model floor was NOT enforced for that decision, since a genuinely enforced floor is always clamped to >=3 and can never legitimately be `0`; note the `bypass:approved` line is written by the shared `agenttools_hatch_escalation` lib and does not yet carry the roles/min_models fields — see the code comment at the DENIED-case call site, tracked as agent-tools#414). `--skip-ci` decisions: `skipci:bypass:approved` / `skipci:bypass:denied` / `skipci:refused` (with `gate":"skip-ci"`). External-review decisions: `external-review:refused` / `external-review:bypass:approved` / `external-review:bypass:denied` (with `gate":"external-review"`). `--known-flake` decisions: `confirmed` / `confirmed-local-gate-failed` / `refused` (with `"gate":"known-flake"` and the asserted check names) — `confirmed` is written only after `gh pr merge` actually succeeds, never merely after the claim checks out. Acceptance-gate decisions: `authorized` / `authorized:post-merge-opt-out` / `refused` / `skipped` / `auto-closed` / `auto-close-failed` (with `"gate":"acceptance"`, a `task_code` when one was derived, and a `detail` — the opt-out reason, `criteria=3 unchecked=3 proofless=2`, or why it was skipped; `auto-closed`/`auto-close-failed` are the post-merge `task done <code>` this gate also runs when everything was already proven — see "Auto-close" below). Magic-close decisions: `refused` / `rewritten` (with `"gate":"magic-close"` and the matched phrases in `detail`, `;`-joined). |

### Migrating to the role-based default (review-cli#246)

The review-quorum gate's primary/default check switched from counting distinct **models** to
counting distinct **BOARD ROLES**. This has TWO distinct migration consequences, in opposite
directions — an unexpected refusal for some tasks, and a silently WEAKENED default guarantee for
every deployment that never touches `SHIP_REVIEW_QUORUM_MIN_MODELS`:

- **A deployment that relied on the old default is now weaker without any action, or any warning
  at merge time.** Before, the default required 3 distinct **models** — a real independence
  signal (no single reviewer, human or AI, could clear the bar alone). Now the default requires
  3 distinct **roles**, which ONE model reviewing under three role hats (backend/security/
  architecture) can satisfy on its own, with no model-diversity floor unless you explicitly set
  `SHIP_REVIEW_QUORUM_MIN_MODELS`. If your deployment's threat model depends on multi-model
  independence, set that env var explicitly — the new default does not provide it any more.
- **An installed `review` (review-cli) build that predates role support** can cause an
  unexpected refusal. ship detects this and refuses with an explicit "the installed review-cli
  does not support `--min-roles`... upgrade it" hint (never a hollow "could not query"). Remedy:
  upgrade review-cli.
- **A task whose PASSED review history was recorded before role tagging existed** (or came from a
  mode with no role concept, e.g. `quorum`/`just-ask`/`brainstorm`) can also cause an unexpected
  refusal. Even a fully modern review-cli reads 0 roles for that history and refuses. Remedy: run
  a fresh `review diff --task <code>` (the review-diff/visual board dispatch is what records
  per-seat roles) so the task accrues real role-tagged iterations, then re-run ship.

If you're mid-migration and genuinely need to merge now, the one-time
`RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` Telegram hatch (see below) is the intended relief valve —
do not set `SHIP_REVIEW_QUORUM=0`: that value is forbidden and is rejected at parse (the gate
stays enabled). Role coverage is the predicate; `distinct_models_passed` is telemetry.

## Flags

- `--skip-ci` — admin-merge bypassing the green-CI gate (+ branch protection); the other
  preflights still run. **Deny-by-default**: it proceeds ONLY on a one-time live Telegram approval
  requested via `RIG_HATCH_REQUEST_SHIP_SKIP_CI="<justification>"` (same shared hatch lib as the
  review-quorum gate; a blank/bare value is denied; no ops off-switch). This is **not** the way to
  handle billing-blocked/stuck CI — for that, run **without** `--skip-ci`: the normal path
  auto-detects the outage, runs the local fallback gate, and does a normal (non-admin) merge.
- `--dry-run` — print, change nothing.
- `--no-screenshot-ok <reason>` — override the UI screenshot requirement, logged.
- `--no-version-bump-ok <reason>` — override the version-bump requirement for a genuine
  no-release ship (docs-only, pure test/CI, a revert), logged.
- `--no-review-dwell-ok <reason>` — fast-track past the review-dwell window for a genuine
  trivial/urgent merge, logged (the reason is printed to the ship log).
- `--resolve-addressed-threads` (or `SHIP_RESOLVE_ADDRESSED_THREADS=1`) — before the
  unresolved-threads gate, auto-resolve the review threads that are **safe to close without a
  human**: unresolved **and** `isOutdated` (the code the thread anchored to has changed — a later
  commit addressed it) **and** authored **entirely** by automated reviewers (login ends in
  `[bot]`, or `chatgpt-codex-connector` / `codex-review-bot`). A thread with **any** human comment,
  or one that is still current (not addressed), is **never** touched — it falls through and still
  blocks the merge. This lets a shipping agent close its own bot nits through `gh ship` instead of
  hand-running the `resolveReviewThread` mutation (which the `block-raw-pr-merge` agent-hook used to
  false-block, #268). Off by default; honours `--dry-run` (reports what it would resolve, mutates
  nothing). A bot thread with **more than 100 comments** is fail-closed (never auto-resolved, since a
  human reply could hide on an unfetched page) and must be resolved by hand. **Caveat:** `isOutdated`
  only means the anchored code *changed* since the comment (a heuristic that the nit was addressed) —
  **not** a verified fix. A still-valid bot finding on rewritten/rebased code can therefore be
  auto-closed; use the flag only when you trust the bot threads are genuinely addressed. A
  severity-aware gate (never auto-close a P0/P1/security bot thread) is a tracked follow-up.
- `--rewrite-magic-close` — when the magic-close gate finds `Closes/Fixes/Resolves <ref>` in the
  PR title or body, rewrite the keyword(s) to `Refs` via `gh pr edit` (audited, `rewritten`) and
  continue instead of refusing. Only the field that matched is edited; the rest of the text is
  untouched. Under `--dry-run` the edit is printed, not made.
- `--screenshot <path> [desc]` — upload (via `SHIP_IMAGE_UPLOAD_CMD`) and post a screenshot
  as a PR comment; repeatable.
- `--task-config <path>` — an explicit task-cli config file for this ship's ticket, for a
  ticket whose tracker differs from the repository's own task routing (e.g. a Tasks-app ticket
  such as `UB-25` shipped from a Forgejo repository). ship passes it as task-cli's standard
  `--config <path>` to the acceptance gate (`task gate`) and to the post-merge `task
  mark-shipped` / auto-close `task done`; task-cli always runs in the repository root, never with
  `-C`. A missing or unreadable file refuses before any gate runs. With `--task-config`, every
  acceptance outcome that is a logged skip without it (task-cli missing, unparseable output,
  `task gate` exit ≥ 2 such as an invalid config) refuses instead. It never disables or waives the
  gate: the ticket must still be accepted (or carry a recorded post-merge-acceptance reason).
  The file must be **routing-only** — `backend` (`tasks` or `forgejo`) plus that backend's block
  (`tasks: {url, team}` / `forgejo: {repo, url}`); any other key (enforcement or policy settings
  such as switching the acceptance gate off), duplicate keys, or non-plain YAML refuses before any
  gate runs ([`task_config_guard.py`](task_config_guard.py)). And with `--task-config`, as on the
  Forgejo provider, a `task gate` exit 0 counts only when its JSON says `ok: true`,
  `gate_enabled: true` and the ticket is not cancelled — a disabled acceptance gate is not
  evidence.
- `--known-flake <check-name>` — assert that the FAILED check named `<check-name>` (exactly as
  printed by the green-CI gate's own `x <name> -> <conclusion>` refusal lines) is a pre-existing
  failure unrelated to this diff, not the "CI is structurally down" case above. Repeatable — one
  per currently-failing check. **Not a blind trust-me flag**: ship independently verifies the
  claim against the base branch's own recent CI history before doing anything with it (see the
  table row above); an unverifiable assertion is refused, same as not passing the flag. On
  success, runs the local fallback gate and merges only if it's green too. Logged to
  `SHIP_AUDIT_FILE`.

The **review-quorum gate (Guard-B) has no override flag** by design — a bypass goes through live
Telegram approval to Alex via `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"` (see the env
table above), never a self-service reason. The hatch imports the shared `agenttools_hatch_escalation`
lib from `lib/` **relative to `ship.sh`'s location in the checkout**, so run `ship` from within the
agent-tools checkout (or via `gh ship` pointed at it) to use the hatch. A bare `cp ci/ship/ship.sh
~/bin/ship` copy can still *enforce* the gate but can't reach the lib, so a bypass request from a
detached copy **fails closed** (refuses) — that's intentional, not a bug.

## The acceptance gate's contract with task-cli (`task gate <code> --json`)

Kept byte-for-byte identical on both ends — this section and task-cli's README "The pre-merge
gate" describe the same thing; a change is a two-repo change.

```
task gate <code>            # exit 0 = accepted; 1 = NOT accepted; 2 = could not evaluate
task gate <code> --json     # exit 0 and 1 print one JSON object; exit 2 prints `error: …` (not JSON)
```

```json
{
  "id": "HYP-1440", "ok": false, "state": "done", "gate_enabled": true,
  "post_merge_acceptance": null,          // or the recorded opt-out reason (string)
  "criteria": 3,                          // total count; 0 => refused (nothing to accept)
  "below_minimum": false,                 // true when criteria < this repo's acceptance_min, not skipped
  "unchecked": [{"index": 3, "text": "survives a restart"}],
  "proofless": [{"index": 2, "text": "handles the empty case"}]   // checked without a proof/force reason
}
```

- **The verdict is the exit code.** Ship never re-derives it from the JSON, so a jq-less
  machine still gates correctly; the JSON only shapes the refusal message and the audit `detail`.
- Exit 0 covers: every criterion checked with a proof (or a `--force` reason) AND at least the
  configured minimum count; a recorded `post_merge_acceptance` (ship prints the reason and writes
  `authorized:post-merge-opt-out`); the gate disabled in that repo's task config
  (`gate_enabled: false`); a cancelled ticket.
- Exit 1 covers: any `unchecked` or `proofless` criterion, `criteria: 0`, or `below_minimum:
  true`. **`below_minimum` can be true with BOTH `unchecked` and `proofless` EMPTY** — a ticket
  below its configured minimum (e.g. one fully-proven criterion, minimum two) refuses on count
  alone, not on any specific criterion's proof — ship's own refusal message checks this field,
  and any other consumer must too, or a refusal like this prints with nothing listed under it.
  `index` is the 1-based position in the full criteria list — the number `task check <code> <n>`
  takes. `unchecked`/`proofless` are always populated, even on a passing opt-out, so the shipper
  sees what is still owed after the merge.
- Exit 2 (unknown id, backend error) is a logged skip on ship's side — not evidence either way.
- Ship's own knobs: `SHIP_ACCEPTANCE_GATE=0` (env or committed `.ship-config`) disables the
  gate; there is no reason flag — the opt-out lives ON the ticket, where `task done` and an
  auditor can see it.

The code derivation is `_ship_derive_task_code_for_notify` (branch name → PR title → PR body,
each candidate validated to contain a digit) — the same function the post-merge
`task mark-shipped` notify uses, so the ticket ship gates on is the ticket it later marks shipped.

No derivable code is a **refusal**, not a skip (agent-tools#639): a PR with no ticket has no
acceptance criteria. The post-merge notify step still skips in that case — the merge already
happened, and there is no ticket to mark shipped.

### Auto-close: the merge itself finishes the ticket when nothing is left to prove

A gate that only REFUSES a bad merge does not by itself close the gap it exists for: tg-cli#301
shipped for real via PR #305 (every criterion independently verified against the merged code)
yet sat OPEN for days, because closing it needed a separate `task done` nobody remembered to run
after the merge. So when the pre-merge acceptance gate finds every criterion **already** checked
with a proof — a genuine pass, never the post-merge opt-out, where acceptance is deliberately not
yet true — the post-merge notify step also runs `task done <code>` right there, immediately after
`task mark-shipped`. There is nothing left to prove at that point; `task done` is a formality
task-cli should be asked to do, not a step left for a human to remember.

Best-effort, like the rest of the notify step: `task done` still enforces every OTHER close gate
(formatting, links, screenshots, msgref, …), so a genuine refusal there is expected sometimes and
only ever WARNS with the manual fallback (`task done <code>`) — it never fails the ship (the merge
already succeeded and is durable). Runs only when `SHIP_TASK_NOTIFY_ENABLED` reaches that far (the
same switch as the notify step) and the acceptance gate genuinely ran and passed for THIS PR's own
derived code — a gate that was skipped, disabled, refused (unreachable — ship would already have
exited), or passed only via the opt-out never triggers an auto-close. "Genuinely passed" is
narrower than a bare `ok: true`: the JSON's `post_merge_acceptance` must be `null`,
`gate_enabled` must not be `false`, and `state` must be neither of task-cli's two terminal
states, `"cancelled"` or `"done"` — an already-cancelled or already-done ticket (e.g. a
follow-up PR that references a ticket an EARLIER merge already closed), or one whose
`acceptance_checked` gate is disabled for this repo, also exits 0, but none of those have
"nothing left to prove" in the sense that makes an automatic `task done` sensible; without `jq`
(so that distinction can't be verified from the JSON at all) auto-close never fires either.
stdout and stderr from `task gate` are captured SEPARATELY — any diagnostic line task-cli
writes to stderr is logged on its own, never mixed into the text `jq` parses as JSON. Audited
to `SHIP_AUDIT_FILE` as a THIRD acceptance-gate decision alongside `authorized`/`refused`/`skipped`: `auto-closed` /
`auto-close-failed`.

## What was stripped vs an internal version

This is generalized from a real in-house ship script. Removed/parameterized: any issue-
tracker coupling (ticket-id extraction, attaching proof to a tracker — replaced by the
generic `SHIP_IMAGE_UPLOAD_CMD` hook), hard-coded front-end path layout (now
`SHIP_UI_PATH_REGEX`), and any org/branch assumptions (now `SHIP_DEFAULT_BRANCH`). If you
want tracker integration back, set `SHIP_IMAGE_UPLOAD_CMD` to your tracker's attach command.

## When to use

When you merge PRs from the CLI and want the merge to be impossible unless the PR is green,
its threads resolved, a review-dwell window elapsed (so review questions had time to form),
(for UI) its screenshot present, its ticket accepted with proofs, and no magic-close keyword
waiting to close that ticket behind the gates — without trusting yourself to remember. The CI
workflows in this directory are the server-side backstop; `ship` is the client-side gate.

## Forgejo provider

`gh ship <PR>` (and `ship.sh <PR>`) also ship a pull request from a checkout whose `origin` is a
**Forgejo** host. `fj ship <PR>` is the same command: rig's `fj:` block installs an `fj` wrapper
whose `ship` subcommand runs exactly the `gh ship` dispatch (rig-cli#601). One `ship.sh` serves
both and dispatches by origin:

| `origin` | Provider |
| --- | --- |
| `github.com` (any URL form), a local path, `file://`, or no origin | GitHub — the unchanged `gh` path |
| any other host | probed with the Forgejo-only `GET /api/forgejo/v1/version`; confirmed → Forgejo, otherwise **refuse** |

`SHIP_PROVIDER=github|forgejo|auto` (default `auto`) pins the choice, e.g. `github` for a GitHub
Enterprise origin. Before this, a Forgejo checkout's `gh` silently resolved a different remote
(an archived GitHub mirror), so every gate read the wrong repository.

**How it works.** For a Forgejo origin, ship binds a shell function named `gh` to
[`forgejo_provider.py`](forgejo_provider.py) (stdlib Python). Every gate in `ship.sh` runs
**unchanged** against Forgejo evidence; child processes (`task`, `review`, the hatch helpers)
never see the function. Only the `gh` invocations ship makes are mapped; anything else exits
non-zero with a reason, which every call site treats as refuse or as "no extra evidence".

| Gate | Forgejo evidence (API v1, Swagger 16.0.3) |
| --- | --- |
| open / not draft / mergeable | `PullRequest.state`, `draft`, `merged`, `mergeable` (`false`, including "not computed yet", is reported as CONFLICTING) |
| exact base (BEHIND) | `PullRequest.merge_base` vs `Branch.commit.id` of the base — **strict**: any base movement refuses |
| green CI | `CombinedStatus.statuses` for the **pinned** head sha; required contexts from `Branch.enable_status_check` / `status_check_contexts` (globs) that never reported are added as pending, so ship waits and then refuses |
| CI outage → local fallback gate | same rule as GitHub, reading the first non-empty of `.forgejo/workflows`, `.gitea/workflows`, `.github/workflows` on `origin/<base>` |
| unresolved review threads | every review's comments are read (never skipped on `comments_count`); a conversation is keyed by `pull_request_review_id` + `original_commit_id` + `path` + line (`position`/`original_position`), a line-less comment is its own conversation, and it is resolved only when its **first** comment has `resolver` (Forgejo resolves through the first comment) — an older resolved conversation on the same line never hides a newer one |
| review dwell | `created_at`, head commit `committer.date`, latest `pull_push` timeline event |
| external review (≥1 review) | `PullReview` entries with state APPROVED / REQUEST_CHANGES / COMMENT (PENDING drafts and REQUEST_REVIEW rows are not reviews) |
| acceptance (task-cli) | `task gate <code> --json` exactly as on GitHub — the ticket is resolved by task-cli, never through the Forgejo issues API. **Difference:** every "could not evaluate" case that is a logged skip on GitHub (task-cli missing, a foreign `task`, unparseable output, `task gate` exit ≥ 2 such as an unroutable ticket) **refuses** on Forgejo. A ticket that lives in another tracker is routed with `--task-config <path>` (see Flags) |
| magic-close, review-quorum, screenshot, version bump, clean pushed tree | unchanged; they read title / body / commits / files / diff through the adapter, or local git |
| merge | `POST /pulls/{index}/merge` with `Do` and `head_commit_id` = the head pinned at the first read (Forgejo answers 409 if the head moved); `--skip-ci` maps to `force_merge` after its hatch approval; audited to `SHIP_AUDIT_FILE` as `gate:"merge", provider:"forgejo"` |
| post-merge | `task mark-shipped` notify as on GitHub (never closes the ticket); remote branch delete via `git push origin --delete` |

**Evidence ledger.** Some `ship.sh` call sites tolerate a failed `gh` (`|| true`, a fallback
value) because on GitHub that failure only means "no extra evidence". On Forgejo ship creates a
per-run ledger at start (refusing if it cannot), and the `gh` wrapper logs `start` before and
`ok` after every adapter call and appends every failure — unsupported call, HTTP error, moved
head, crash, even a SIGKILL — to `fatal.log`. The final pre-merge check (after re-reading the PR
head) refuses the ship, dry-run included, unless the ledger is intact, every started call
completed, and `fatal.log` is empty; it prints each recorded reason. The wrapper records a call's
outcome before releasing its output, and the exit trap lets in-flight calls finish (bounded)
before it removes the ledger; on a refusal it first prints a one-line ledger summary (calls
started / completed / failed, plus each recorded reason) to stderr.

**Head pinning.** The first PR read pins the head sha in a per-run state directory. Every later
read refuses if the head moved, CI statuses are read for the pinned sha, and the merge sends that
sha as `head_commit_id`.

**Credentials** are bound to the origin host: fj's key store entry for exactly that host
(`fj auth login` / `fj auth add-token`), or `SHIP_FORGEJO_TOKEN` only together with
`SHIP_FORGEJO_TOKEN_HOST` naming the same host. `GH_TOKEN`/`GITHUB_TOKEN` are never read. HTTP
redirects are refused, so a token never reaches another host. Plain `http://` is accepted only
for a loopback origin (tests). Without a credential, reads are anonymous (public repositories
only); every write (merge, comment, edit) refuses. fj's key store is `keys.json` in
`~/Library/Application Support/forgejo-cli.forgejo-cli/` (macOS),
`%APPDATA%\forgejo-cli\forgejo-cli\data\` (Windows) or `$XDG_DATA_HOME/forgejo-cli/`
(Linux). An `fj auth login` (OAuth) token expires after about an hour and fj refreshes it only
when fj runs: an expired entry makes the provider run `fj -H <origin host> whoami` once (fj
refreshes and rewrites its store) and re-read it; if it is still expired, or its expiry is in a
format the provider does not recognise, the run refuses.

**Fails closed on Forgejo (no evidence source):**

- `--resolve-addressed-threads`: Forgejo has no `isOutdated` or GraphQL resolve mutation, so the
  auto-resolve query is unsupported and the run refuses (see the evidence ledger below). Resolve
  conversations in the web UI and ship without the flag.
- `--known-flake` and the "CI structurally down" re-classification of failed checks: both rely on
  GitHub Actions run history / githubstatus.com, so a failing Forgejo check always refuses.
- `--repo` / `GH_SHIP_REPO` / `GH_REPO`: GitHub-only; run ship from the Forgejo checkout.
- The Rig machine external-review policy (below) is read for the Forgejo origin's own
  `owner/repo`; the owner list carries no host, so an owner listed there is opted out on both
  GitHub and Forgejo. An owner that is not listed still needs a real review, the base-branch
  `.ship-config` opt-out, or the one-time hatch.
- Warning statuses count as failures; an unknown status value counts as an error.
- PRs migrated from GitHub lost their conversation resolution, so their migrated review comments
  read as unresolved.

## Rig machine policy for external PR review

An explicit machine-owner policy can disable **only** the extra GitHub-side
external-review gate across selected repository owners, without modifying every
repository or authorizing each PR separately. Configure the Rig machine file
`~/.config/rig/config.yaml` (or `$XDG_CONFIG_HOME/rig/config.yaml`):

```yaml
ci:
  items:
    ship:
      require_external_review: false
      external_review_owners:
        - alex-mextner
```

`rig_external_review_policy.py` reads only this trusted machine file. The gate
resolves the actual `gh repo view` target, including `--repo`/`GH_REPO`, and checks
its exact owner against this list. A foreign owner's PR is not opted out merely
because the current checkout is owned by the operator. Empty scope, wrong types,
duplicate YAML keys, missing dependencies/files, unsafe permissions, symlinks and
repository-local policy files never authorize the opt-out. The bounded parser
runs with `python3 -I`; arbitrary project modules and ambient `SHIP_*` variables
cannot replace this configuration. Rig registers these keys as Global-only.

A successful machine decision is audited as
`external-review:disabled:rig-policy` with the actual repository. It takes effect
immediately on the next canonical `gh ship`; no CI/review-quorum/test/secret or
permission gate is changed. Legacy `.ship-config` opt-outs from the trusted base
remain supported. The earlier documentation's "only .ship-config or one-time
hatch" statement applies when no explicit Rig machine policy is configured.

The reader uses Rig's supported Python 3.10+ runtime and PyYAML. Parent Rig and
XDG configuration directories must also be user-owned and not group/world writable.
When the trusted base already opts out, no extra repository query is performed.
