# Merge gate: unresolved review threads

**Standard mechanism = GitHub branch protection "Require conversation resolution before
merging".** Prefer that native toggle if you can set it. This slot exists for two cases the
toggle doesn't cover:

1. You can't reach branch-protection admin (or want the gate to live *in the repo*, code-
   reviewed and versioned).
2. You want the same check inside a **ship script** preflight (see [`../ship/`](../ship/),
   which runs this exact GraphQL count before merging).

## What it does

Counts the PR's **unresolved** review threads via the GraphQL `reviewThreads` API and fails
if any remain. A "thread" is a review comment conversation; "resolved" is the green
*Resolved* state a reviewer/author toggles.

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `review-threads.sh` + `review_threads.py` | **Any CI** + ship preflight | `gh api graphql` paginates threads (GitHub) or reads the Forgejo REST API (Forgejo / Gitea Actions), counts unresolved, exits non-zero if > 0. |
| `workflow.yml` | **GitHub Actions** | Runs the script as a PR check named `review-threads`. |

## Quick start

```bash
cp ci/review-threads/workflow.yml .github/workflows/review-threads.yml
# The workflow runs `bash ci/review-threads/review-threads.sh`, so the script must be
# present at that path in your repo — vendor the ci/ dir, or copy the launcher and `review_threads.py` together and adjust
# the `run:` path (e.g. to .github/scripts/):
cp ci/review-threads/review-threads.sh ci/review-threads/review_threads.py .github/scripts/   # then edit the run: path
# REQUIRED to actually block (not optional — see "Enforcement" below):
#   Settings -> Branches -> required checks -> add "review-threads"
#   AND enable "Require conversation resolution before merging".

# Standalone / ship preflight:
sh ci/review-threads/review-threads.sh 123
```

## Enforcement — a REQUIRED check, or it does not block the merge button

A `tier: block` workflow **only goes red** — by itself it does **not** block the merge
button. To actually ENFORCE this gate its `review-threads` context must be a **REQUIRED
status check** under **server-side branch protection**, and you should *also* turn on the
native **"Require conversation resolution before merging"** toggle (the two are
complementary: the toggle blocks on unresolved threads server-side; the required check keeps
the in-repo count honest and ship-preflight-reusable). rig-cli#5 provisions both from the
`github:` block in `rig.yaml` (`required_status_checks` + `required_conversation_resolution`).
Without server-side enforcement a GitHub-UI merge or a raw `gh pr merge` lands the PR over an
unresolved thread — exactly how hyper-saas #543 merged through an open thread (and red CI, and
no screenshot). See **[Client-side vs. server-side enforcement](../../README.md#client-side-vs-server-side-enforcement-the-543-gap)**
in the repo README.

## Knobs

- `PR_NUMBER` (or `$1`) — the PR to check.
- `IGNORE_OUTDATED=1` — don't count threads GitHub marked *outdated* (default counts them;
  an unresolved thread is unresolved even if the line moved). GitHub only.
- `REVIEW_THREADS_PROVIDER` — `github` | `forgejo` | `auto` (default; see above).

## Forgejo / Gitea runners

In a CI job whose `$GITHUB_SERVER_URL` is not `https://github.com` (Forgejo / Gitea Actions),
the script uses the **Forgejo REST API v1** instead of `gh` + GraphQL (neither exists there).
It needs only stdlib `python3` (the `.sh` is a thin POSIX launcher for `review_threads.py`); the API base is `$GITHUB_API_URL` (else
`$GITHUB_SERVER_URL/api/v1`) and the token is the job token the workflow already passes as
`GH_TOKEN`. The resolution rule is the one [`../ship/`](../ship/)'s Forgejo provider uses:
every review's comments are read; a conversation is what Forgejo's UI shows as one — every
comment on the same path + line (`position` / `original_position`), across reviews and
commits; a line-less comment is its own conversation; and a conversation is resolved only
when its **first** comment has `resolver` ("Resolve conversation" marks only that one).
Forgejo has no *outdated* flag, so `IGNORE_OUTDATED=1` is ignored there (every unresolved
conversation counts). `REVIEW_THREADS_PROVIDER=github|forgejo|auto` overrides the choice —
set `github` on GitHub Enterprise Server, whose server URL is not github.com.

## Re-evaluation — closing the stale-green gap (agent-tools#65)

`pull_request_target` only fires on PR lifecycle (open/synchronize/...), so a review thread
**added after an earlier green check** used to leave the required `review-threads` status
**dishonestly green** — a PR could be merged via the GitHub UI over a fresh unresolved thread
(the [ship gate](../ship/) re-checks at merge time, but the required *status* lied). The
workflow now **also re-runs when a review is submitted or a review comment is
created/edited/deleted** (`pull_request_review`, `pull_request_review_comment`) — exactly when
an unresolved thread appears — so the gate re-counts and reds the check. Both events run on
the PR **merge ref** (`GITHUB_REF = refs/pull/N/merge`, per the GitHub Actions event docs),
exactly like `pull_request` — so GitHub attaches their check-run to the PR and branch
protection's required `review-threads` context is refreshed on the PR head. (That merge-ref
behavior is also why the checkout below must pin to the trusted base — see the security note.)

Deliberately **not** triggered on (each would be a false-coverage no-op):

- `issue_comment` — runs on the **default-branch** SHA, so its check-run never attaches to the
  PR head where branch protection looks; it would re-run but never update the required status.
  (A plain PR comment also doesn't change thread state.)
- `pull_request_review_thread` (resolved/unresolved) — **not a supported Actions trigger** (it
  exists only as a webhook), so listing it would silently never fire.

**Residual gap (the safe direction):** *resolving* the last thread fires no supported Actions
trigger, so the check won't auto-flip back to green on its own. Push a commit, re-run via
`workflow_dispatch`, or rely on the ship-time preflight ([`../ship/`](../ship/)) which always
re-counts at merge time. Staying red until then is conservative — it never lets an unresolved
thread merge, it only delays the green.

**The vacuous-pass gap (questions that never had time to form):** this check counts *unresolved*
threads, so "0 unresolved" is also true when **no review has posted yet**. On its own it can't
stop a PR opened and merged within seconds, before any async (multi-model / CI-AI / human) review
forms its questions. The [ship preflight](../ship/) closes that with a **review-dwell window**
(`SHIP_REVIEW_DWELL`, default 600s): it refuses to merge until enough time has elapsed since the
last push for review to land — at which point whatever it posts becomes a thread this gate then
forces resolved. The two compose: dwell gives comments **time to form**, this gate forces them
**resolved**.

## Security note — tamper-resistant

A merge-BLOCKING gate must not run a script the PR can edit (a PR could weaken its own
gate). So the workflow uses `pull_request_target` and pins the checkout to the PR **base
branch (trusted)**, running that copy of the script; it reads only the **PR number** from the
event payload (data), never checks out or executes PR-head code, and keeps a read-only token.
The pinned base checkout matters for the review/comment triggers: their *default* checkout ref
is the PR **merge** ref (which contains the PR's edits to the gate script), so pinning to
`base.ref` keeps the trusted copy running. The standalone `review-threads.sh` is for local /
ship-preflight use. `workflow_dispatch` takes a `pr_number` input (a manual run has no PR in
the event payload).

## When to use

Any repo where review comments must be addressed, not silently merged over — and you either
lack branch-protection admin or want the rule versioned in-repo. Pairs with the
[`../pr-checklist/`](../pr-checklist/) gate (unchecked task-list boxes).
