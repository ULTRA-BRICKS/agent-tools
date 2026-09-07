# Dependency audit (free, no GHAS)

Block a PR/push whose dependency tree has a known **high+ vulnerability** — using only OSS
tooling, no GitHub Advanced Security and no paid action.

- **`workflow.yml`** — GitHub Actions gate. Runs `dep-audit.sh` (below). Drop into
  `.github/workflows/`.
- **`dep-audit.sh`** — generic multi-ecosystem audit (bun / npm / pnpm / yarn audit,
  pip-audit, cargo-audit, govulncheck). Works in any CI or locally. For a PR, only FAILS on
  advisories NEW relative to the base branch (see "Scoping to the PR" below); with no base
  branch to compare against, audits the whole tree as before.

## Scoping to the PR (task-cli#140)

Auditing the whole tree unconditionally meant every PR in a repo with ANY pre-existing
high+ transitive advisory failed this gate forever, regardless of what the PR itself changed
— a real incident (`hyperide/hyper-saas#780`, a docs-only PR) failed on 6 pre-existing
`browserslist`/`fast-uri` advisories it never touched.

`dep-audit.sh` now takes an optional second argument, `BASE_DIR` — the base branch's checked
out tree. `workflow.yml` passes it as `$GITHUB_WORKSPACE` under `pull_request_target` (which
is already the base branch's checkout, see "Tamper-resistance" below). When given, the
bun/npm/pnpm/yarn branches also audit `BASE_DIR` and diff by the GitHub Advisory Database's
numeric id: an advisory present in **both** the PR head and the base branch is printed as a
visible informational warning but does **not** fail the check; an advisory present **only**
in the PR head is genuinely new and **does** fail it. Python/Rust/Go audits are not scoped
this way yet — they keep auditing the whole tree unconditionally.

Every path that can't establish a trustworthy baseline (`jq` not installed, `BASE_DIR` unset
or unresolvable, no matching lockfile there, or an unparseable audit run) fails OPEN toward
the **original** whole-tree behavior for that ecosystem — it never crashes and never silently
passes something it can't verify.

## Why not `actions/dependency-review-action`?

GitHub's `dependency-review-action` **requires GitHub Advanced Security (GHAS, paid) on
private repos** — it hard-fails `"Dependency review is not supported on this repository …
requires GitHub Advanced Security"` *even when the (free) Dependency Graph is enabled*. (The
graph alone is not enough; the action gates on GHAS for private repos.) So we dropped it.

`dep-audit.sh` covers the same ground for free, and arguably better:

| | `dependency-review-action` (GHAS) | `dep-audit.sh` (OSS) |
|---|---|---|
| Cost | GHAS (paid) on private repos | free |
| Scope | the PR **diff** (new deps only) | the **whole tree**, scoped to advisories new-vs-base for bun/npm/pnpm/yarn (task-cli#140); still whole-tree for pip/cargo/go |
| Vuln source | GitHub Advisory DB | each ecosystem's auditor (npm/bun → GitHub Advisory DB; pip-audit → OSV; cargo-audit → RustSec; govulncheck → Go DB) |
| **License policy** | **allow/deny licenses ✅** | **not covered ⚠️** |

The **one** thing we lose is **license-policy enforcement** (deny AGPL/GPL etc.). Fill it
with an OSS license gate (`license-checker` / `cargo-deny` / `pip-licenses`) — see
`ci/license-policy/` and agent-tools#21.

## Tamper-resistance (agent-tools#129)

This is a merge-**blocking** gate, so it must not run a copy of the script the PR can edit
(a PR could otherwise weaken the very gate it has to pass). Like `ci/leftover-grep` and
`ci/review-threads`, `workflow.yml` runs under **`pull_request_target`**: the **base-branch
(trusted) copy** of both the workflow and `dep-audit.sh` run; the PR's version is ignored.
The PR's **manifests/lockfiles** are fetched as **DATA** into a side worktree and audited
there — the native auditors (`npm`/`pnpm`/`yarn`/`bun audit`, `pip-audit`, `cargo-audit`)
only **read** the lockfile and query an advisory DB; they do **not** run the package's
install/lifecycle scripts, so no PR-controlled code executes. **Hard rule:** never add an
`npm install` / `bun install` / build step or check the PR head onto the workspace — that
would execute PR code under the privileged trigger. Like any base-run gate, it does not run
on the PR that first introduces it.

**Python (pip-audit) is the one auditor that can execute its input** — a *resolving* run
(`-r` without `--no-deps`, `-e .`, a project path) downloads and **builds** the PR's sdists
(runs `setup.py`), i.e. RCE under the privileged trigger. So `dep-audit.sh` audits Python
deps **as data, never building**:

- Each `requirements*.txt` (and the `requirements/<env>.txt` layout) is audited with
  `pip-audit --no-deps -r <file>` — **but only after a scan confirms every line is a pinned
  `name==version` spec** (plus benign `#` comments / blanks / `--hash` / `--require-hashes` /
  `; markers`). `--no-deps` suppresses *transitive* resolution; the scan rejects any *direct*
  reference (editable `-e`, a URL/VCS `git+…`/`://`, a PEP 508 `name @ url`, a local
  path/archive, an `-r`/`-c` include, a foreign-index option, or an unpinned/prefix spec) —
  because pip-audit must **build** such an entry to read its metadata, which is the RCE. A file
  with any such line **fails closed** (pin every line, or set `DEP_AUDIT_ALLOW_MISSING=1`). The
  result audits the PR's *declared, pinned* deps against the advisory DB with no build.
- A `pyproject.toml`/`poetry.lock` with **no** pinned `requirements*.txt` can only be audited
  by building it, so it **fails closed** here (pin a `requirements*.txt`, or
  `DEP_AUDIT_ALLOW_MISSING=1`). An **empty/stub `requirements.txt` does not mask this** — a
  Python source tree must have a real pinned spec to be audited as data.

A bare no-arg `pip-audit` would instead audit the *runner's* installed packages and silently
miss the PR's deps — never reintroduce it (agent-tools#131).

## Quick start

```bash
cp ci/dependency-review/workflow.yml .github/workflows/dependency-review.yml
# `setup-bun` ships ENABLED (bot repos use bun.lock; without it a bun.lock repo fail-CLOSES
# because dep-audit.sh can't find `bun`). Add the toolchain for your other ecosystems (see
# the comments in workflow.yml); ubuntu-latest already ships node+npm so npm-only repos need
# nothing extra — trim setup-bun if you have no bun.lock.

# Run the same audit locally / in any other CI (optional AUDIT_DIR, default '.'; optional
# BASE_DIR to scope failures to advisories new relative to it — requires `jq` on PATH):
sh ci/dependency-review/dep-audit.sh                                    # audit the current tree
sh ci/dependency-review/dep-audit.sh path/to/checkout                   # audit another checkout's lockfiles
sh ci/dependency-review/dep-audit.sh path/to/pr-head path/to/base-branch # only fail on advisories new vs. base
```

> If your repo already has its own dependency-audit job, you don't need this one — don't
> double up.

## Enforcement — a REQUIRED check, or it does not block the merge button

A `tier: block` workflow **only goes red** — by itself it does **not** block the merge
button. To actually ENFORCE this gate its `dependency-review` context must be a **REQUIRED
status check** under **server-side branch protection** (Settings -> Branches -> required
checks -> add `dependency-review`). rig-cli#5 provisions exactly that from the `github:`
block in `rig.yaml` — it lifts every `tier: block` gate into `required_status_checks`.
Without it, a GitHub-UI merge or a raw `gh pr merge` lands the PR over a red check — the same
client-side bypass that let hyper-saas #543 merge over a red check. See **[Client-side vs. server-side enforcement](../../README.md#client-side-vs-server-side-enforcement-the-543-gap)**
in the repo README.

## Knobs (`dep-audit.sh`)

- `DEP_AUDIT_LEVEL` — `low|moderate|high|critical` (default `high`).
- `DEP_AUDIT_ALLOW_MISSING` — `1` to fail-OPEN when a manifest is found but its scanner
  isn't installed. **Default `0` (fail-CLOSED):** a detected ecosystem with no usable scanner
  is a gate failure, not a silent skip — otherwise "no audit ran" masquerades as "no vulns".
  Install the named tool in your CI image, or set this to `1` to intentionally skip.
- `BASE_DIR` (2nd positional arg, not an env var) — the base branch's checked-out tree, for
  scoping bun/npm/pnpm/yarn failures to advisories new relative to it (task-cli#140). Requires
  `jq` on PATH; without it, or without this argument, falls back to the original whole-tree
  behavior. See "Scoping to the PR" above.

## Relationship to other slots

- [`../secret-scan/`](../secret-scan/) — credentials, not dependencies.
- [`../sast/`](../sast/) & [`../codeql/`](../codeql/) — your *own* code, not third-party deps.
- [`../license-policy/`](../license-policy/) — dependency **licenses** (the gap above).
- This slot — third-party **dependency vulnerabilities**.

## Note on lockfiles audit tools miss

GitHub's Dependabot does not parse every lockfile format (e.g. `bun.lock`). `dep-audit.sh`
runs the ecosystem's own tool (`bun audit` reads `bun.lock` directly), so vulnerabilities
invisible to Dependabot still get caught.

## When to use

Any repo that pulls third-party dependencies — i.e. nearly all of them.
