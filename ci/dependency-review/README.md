# Dependency audit (free, no GHAS)

Block a PR/push whose dependency tree has a known **high+ vulnerability** — using only OSS
tooling, no GitHub Advanced Security and no paid action.

- **`workflow.yml`** — GitHub Actions gate. Runs `dep-audit.sh` (below). Drop into
  `.github/workflows/`.
- **`dep-audit.sh`** — generic multi-ecosystem audit (bun / npm / pnpm / yarn audit,
  pip-audit, cargo-audit, govulncheck). Audits the **whole tree**. Works in any CI or locally.

## Why not `actions/dependency-review-action`?

GitHub's `dependency-review-action` **requires GitHub Advanced Security (GHAS, paid) on
private repos** — it hard-fails `"Dependency review is not supported on this repository …
requires GitHub Advanced Security"` *even when the (free) Dependency Graph is enabled*. (The
graph alone is not enough; the action gates on GHAS for private repos.) So we dropped it.

`dep-audit.sh` covers the same ground for free, and arguably better:

| | `dependency-review-action` (GHAS) | `dep-audit.sh` (OSS) |
|---|---|---|
| Cost | GHAS (paid) on private repos | free |
| Scope | the PR **diff** (new deps only) | the **whole tree** (incl. pre-existing) |
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
- `uv.lock` / `poetry.lock` are parsed as **text** (never `uv export` / `poetry export`).
  Registry/PyPI `name==version` pairs are written to a temp file *outside* the audited tree
  and audited with `pip-audit --no-deps -r <temp>`. Editable/path/git/url/virtual/directory
  packages are skipped (`source = { editable = "." }` is the local project). A package
  record that cannot be emitted as a registry pin (unknown source, unparseable
  name/version, leftover quoting) **fails closed** — a sibling valid pin must not mask
  it. The lockfile itself is never passed to pip-audit — that would **build** it (RCE
  under the privileged trigger).
- A `pyproject.toml` with **no** pinned `requirements*.txt` **and** no auditable lockfile
  pins can only be audited by building it, so it **fails closed** here (commit `uv.lock` /
  `poetry.lock`, pin a `requirements*.txt`, or `DEP_AUDIT_ALLOW_MISSING=1`). An **empty/stub
  `requirements.txt` does not mask this** unless a lockfile supplies registry pins.

A bare no-arg `pip-audit` would instead audit the *runner's* installed packages and silently
miss the PR's deps — never reintroduce it (agent-tools#131).

## Quick start

```bash
cp ci/dependency-review/workflow.yml .github/workflows/dependency-review.yml
# `setup-bun` ships ENABLED (bot repos use bun.lock; without it a bun.lock repo fail-CLOSES
# because dep-audit.sh can't find `bun`). Python is covered too: when the base tree has a
# pyproject.toml / requirements*.txt / uv.lock / poetry.lock, the workflow sets up Python and a
# pinned pip-audit in a job-local venv on PATH (no pipx, no root — works on Forgejo / GARM
# runners too). Add the toolchain for your other ecosystems (see the comments in
# workflow.yml); ubuntu-latest already ships node+npm so npm-only repos need nothing extra —
# trim setup-bun if you have no bun.lock.

# Run the same audit locally / in any other CI (optional dir arg, default '.'):
sh ci/dependency-review/dep-audit.sh           # audit the current tree
sh ci/dependency-review/dep-audit.sh path/to/checkout   # audit another checkout's lockfiles
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
