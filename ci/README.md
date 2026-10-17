# ci/ — drop-in CI tools

Reusable, vendor-neutral CI building blocks. A CI-building agent (or human) looks **here**
first for "is there already a standard way to do X in CI?" Each subdirectory is one
concern, with a GitHub Actions workflow **and** a generic shell script (for GitLab /
Jenkins / Buildkite / cron), plus a README that states the standard tool and how to extend
it.

| Slot | Concern — when to use | Standard engine | Has |
| ---- | --------------------- | --------------- | --- |
| [`secret-scan/`](secret-scan/) | Stop credentials reaching git history — any repo | **gitleaks** | pinned GH Action + shell script + tiered configs |
| [`codeql/`](codeql/) | Deep semantic SAST (taint/data-flow) — any repo with source; **self-gate** variant works on a private repo with no GHAS | **github/codeql-action** | dashboard workflow + private-repo self-gate workflow |
| [`sast/`](sast/) | Fast pattern-based SAST, broad/custom rules — pair with CodeQL | **semgrep** | pinned GH Action + shell script |
| [`dependency-review/`](dependency-review/) | Block a PR adding a vulnerable/bad-license dep; audit existing deps | **actions/dependency-review-action** + native audit | PR workflow + multi-ecosystem audit script |
| [`license-policy/`](license-policy/) | Fail on a dep carrying a deny-listed (GPL/AGPL/…) license — the license half of the GHAS gap | native license reporters (license-checker / pip-licenses / cargo-deny / go-licenses) | workflow + multi-ecosystem license-audit script |
| [`trivy/`](trivy/) | Filesystem scan for dependency CVEs + secrets + IaC/Dockerfile misconfig in one pass — any repo | **Trivy** (aquasecurity) | SHA-pinned GH Action + shell runner |
| [`ai-review/`](ai-review/) | Post an AI reviewer's findings on each PR (advisory) — any repo with an AI review CLI + key | **configurable** (codex / review-cli / …) | PR workflow + generic diff-review script |
| [`copilot-findings/`](copilot-findings/) | Surface (optionally gate) Copilot review comments + Autofix alerts — repos using Copilot | GitHub APIs (review comments + code-scanning) | workflow + aggregation script + honest manual flow |
| [`review-threads/`](review-threads/) | Block merge while review threads are unresolved — when you lack branch-protection admin or want it in-repo | `gh api` GraphQL `reviewThreads` (GitHub) / Forgejo REST API (Forgejo, Gitea) | workflow + shell script |
| [`pr-checklist/`](pr-checklist/) | Block merge while PR-body `- [ ]` boxes are unchecked — repos with an acceptance-criteria template | github-script + tested parser | tamper-resistant workflow + parser + test + PR template |
| [`screenshots/`](screenshots/) | Require an embedded image on UI-touching PRs (at open AND merge) — front-end repos | `gh` PR metadata + path glob | workflow + shell script |
| [`pr-title-lint/`](pr-title-lint/) | Enforce Conventional Commits on the PR title — repos that squash-merge | dependency-free inline check (amannn/action-semantic-pull-request's rules, no API call) | workflow (runs on GitHub and Forgejo) |
| [`leftover-grep/`](leftover-grep/) | Fail on `.only`/`debugger`/`console.log`/untracked-TODO/conflict-markers in added code — any repo | dependency-free grep on the diff | workflow + shell script |
| [`python-only/`](python-only/) | Fail on new/changed non-Python tool code (TS/JS/Go/Rust, node/bun scripts, shell over 40 lines) — rig-ecosystem tool repos | dependency-free Python checker on the diff (also the global pre-commit fragment) | workflow + Python checker |
| [`ship/`](ship/) | Green-CI-gated PR merge + cleanup, with the above preflights — a client-side merge command | `gh` + git | portable ship script (all project coupling optional) |

## Conventions for slots in here

- **Name the standard engine** in the README's first lines — don't make the reader guess
  (secret scanning = gitleaks, not a hand-rolled regex).
- Ship **both** a GitHub Actions workflow (`*.yml`) and a **generic script** entry so
  non-GitHub CI is a first-class path, not an afterthought. A new script is **Python**
  (`*.py`, stdlib only): agent-tools is a rig-ecosystem tool repo, and the
  [`python-only/`](python-only/) gate blocks new shell over 40 lines here. The existing
  `*.sh` entries are being ported ([agent-tools#951](https://git.hyperide.ai/ultrabricks/agent-tools/issues/951)).
- **Gate scripts are never test-shaped.** Don't name a slot script `test_*.py`, `*_test.py`
  or `conftest.py`: rig vendors `ci/**/*.py` companions but skips test-shaped files by that
  naming contract, so a test-shaped gate would silently never reach consuming repos.
- **Pin** third-party actions to a commit SHA (supply-chain hygiene), with the version in
  a trailing comment.
- Document **how to extend** (rules/config) and the **escape hatch** for false positives.
- State the **tiers** (block vs warn) explicitly — CI defaults to block; warn is opt-in.
- A workflow that calls a sibling script does so as `python3 ci/<gate>/<script>.py` (older
  slots: `bash ci/<gate>/<script>.sh`) — so that
  path must exist in the consuming repo. **Vendor the `ci/` dir**, or copy the script into
  `.github/scripts/` and adjust the workflow's `run:` path. Each slot README's quick-start
  spells this out.

## Relationship to the git-hooks

Many CI checks have a local-hook twin under [`../git-hooks/`](../git-hooks/): the hook gives
the committer fast feedback and the CI check is the backstop for anyone whose hook is
missing or bypassed. Secret scanning is the canonical example — same gitleaks engine, two
carriers. See [`../docs/carrier-decision-guide.md`](../docs/carrier-decision-guide.md).

## How the gates fit together (a recommended stack)

A solid PR gate for a typical front-end-plus-backend repo:

1. **Build / lint / typecheck / unit tests** — your own CI (not templated here; project-
   specific). Gate merges on it.
2. **Security:** [`secret-scan/`](secret-scan/) + [`codeql/`](codeql/) (or its self-gate) +
   [`sast/`](sast/) + [`dependency-review/`](dependency-review/) (dep vulns) +
   [`license-policy/`](license-policy/) (dep licenses). Different bug classes; run them all.
   Add [`trivy/`](trivy/) for a one-pass dep-CVE + secret + IaC-misconfig net (especially if
   the repo builds container images or carries IaC).
3. **Review hygiene:** [`review-threads/`](review-threads/) (resolve comments) +
   [`pr-checklist/`](pr-checklist/) (tick the boxes) + [`leftover-grep/`](leftover-grep/)
   (no debug leftovers).
4. **UI proof:** [`screenshots/`](screenshots/) on front-end PRs.
5. **History hygiene:** [`pr-title-lint/`](pr-title-lint/) if you squash-merge.
6. **Reviewers (advisory):** [`ai-review/`](ai-review/) and/or
   [`copilot-findings/`](copilot-findings/) — they inform, they don't auto-block.
7. **Merge:** [`ship/`](ship/) re-runs the green-CI, review-thread, and screenshot
   preflights client-side, then merges + cleans up.

## Backlog — net-new gate ideas (documented, not yet built)

High-value candidates to add as slots later. Each is a self-contained gate that fits the
conventions above. PRs welcome.

- **coverage-delta** — fail if a PR drops line/branch coverage below a baseline (or by more
  than N%). Engine: your test runner's coverage output + a comparator; or an action like
  `codecov`/`coverallsapp`. Knob: absolute floor vs delta tolerance. Tricky bit: a trusted
  baseline artifact (store per-`main`-commit, like [`screenshots/`] stores nothing but
  [bundle-size] would store a report). Build when a repo has real coverage worth defending.
- **bundle-size / perf budget** — build the client, diff total/initial JS against a stored
  `main` baseline, comment the delta, fail past a threshold. Engine: a size reporter
  (`size-limit`, or a hand-rolled `du` on the build output) + `peter-evans/find-comment` for
  the sticky PR comment + an artifact baseline. The most generally useful next slot for any
  shipped front-end.
- **doc-link-check** — fail on broken links in Markdown/docs. Engine: `lycheeverse/lychee`
  (fast, Rust) or `markdown-link-check`. Knobs: include/exclude globs, allow-list for
  flaky/auth'd hosts, internal-only vs also-external. Low effort, catches rot.
- **CODEOWNERS / required-reviewers template** — ship a `.github/CODEOWNERS` scaffold + a
  note on wiring branch-protection "require review from code owners". Mostly a template +
  docs, not a workflow; pairs with [`review-threads/`].
- **stale-PR nudge** — a scheduled workflow that labels/comments PRs idle for N days (and
  optionally closes after M). Engine: `actions/stale`. Knob: day thresholds, exempt labels.
- **PR size-label** — auto-label `size/XS…XL` by lines changed (nudge toward small PRs).
  Engine: a tiny `gh api` script or `pascalgn/size-label-action`. Pairs with the
  `smallest-change` skill.
- **conventional-commit (commit messages, not just title)** — the [`pr-title-lint/`] slot
  covers the squash title; a full commit-message lint across the branch belongs in a
  `commit-msg` git-hook ([`../git-hooks/`](../git-hooks/)) for non-squash repos.
