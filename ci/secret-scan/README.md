# Secret-scan (CI) — gitleaks

**Secret-scanning standard = [gitleaks](https://github.com/gitleaks/gitleaks).** Don't
reinvent it — it's the engine. This slot ships the CI-side pieces that pair with the
local git-hook (`../../git-hooks/no-secrets-scan`) so the same guard runs in two places:

- **Local** (committer's machine): a `pre-commit` git-hook scans the **staged** diff.
- **CI** (backstop): scans on every push/PR, catching anyone whose local hook is missing,
  disabled, or bypassed with `--no-verify`.

## What's here

| File                 | For                          | Does                                                          |
| -------------------- | ---------------------------- | ------------------------------------------------------------ |
| `secret-scan.yml`    | **GitHub Actions**           | Installs the **gitleaks OSS binary** (pinned version) and scans the tree — NO `gitleaks-action`, NO org license. Drop into `.github/workflows/`. |
| `secret-scan.sh`     | **Any other CI** (GitLab/Jenkins/Buildkite/cron) | POSIX-sh gitleaks runner; installs gitleaks if missing; block + optional warn tier. |
| `gitleaks.toml`      | **Block-tier config**        | High-confidence ruleset (extends gitleaks defaults) + allowlist. Copy as repo `.gitleaks.toml`, or point `GITLEAKS_CONFIG`/`SECRET_SCAN_CONFIG` at it. |
| `gitleaks-warn.toml` | **Warn-tier config**         | Fuzzy heuristics (entropy, suspicious var names). Warn-only — never blocks. |

## Quick start

**GitHub Actions:**
```bash
cp ci/secret-scan/secret-scan.yml  .github/workflows/secret-scan.yml
cp ci/secret-scan/gitleaks.toml    .gitleaks.toml   # optional: customize rules/allowlist
git add .github/workflows/secret-scan.yml .gitleaks.toml && git commit -m "ci: secret scan"
```

**Other CI** (a shell step in your pipeline):
```yaml
# GitLab CI example
secret-scan:
  script:
    - sh ci/secret-scan/secret-scan.sh
```
```bash
# Jenkins / Buildkite / cron — same one-liner:
sh ci/secret-scan/secret-scan.sh
```

## Tiers — block vs warn

- **BLOCK** (default, both YAML and shell): a **high-confidence** finding (a real provider
  credential format, a private key) **fails the job**. In CI this is the correct default —
  a warn-only required check is ignored and worthless.
- **WARN** (opt-in): low-confidence heuristics (high-entropy blobs, secret-looking variable
  names with no provider match). Surfaced for a human to eyeball; **never fails the build**.
  - shell: set `SECRET_SCAN_WARN_CONFIG=ci/secret-scan/gitleaks-warn.toml`.
  - GitHub Actions: add a second, non-required job with `continue-on-error: true` and
    `GITLEAKS_CONFIG` pointed at `gitleaks-warn.toml`.

The local git-hook (`../../git-hooks/`) runs **both tiers** every commit; CI defaults to
block-only because a non-failing CI check provides no protection.

## Extending — custom rules & allowlist

gitleaks auto-reads `.gitleaks.toml` at the repo root. To inherit the shipped block config
and add your own:

```toml
title = "my-repo"
[extend]
path = ".gitleaks.toml.base"   # or an absolute path to a shared org config
useDefault = true              # also gitleaks' built-in provider rules

[[rules]]
id = "my-internal-token"
description = "ACME internal service token"
regex = '''acme_(live|test)_[0-9a-zA-Z]{32}'''
keywords = ["acme_"]

[allowlist]
regexes = ['''acme_test_0{32}''']   # documented dummy used in examples
paths   = ['''(.*?)/fixtures/''']
```

## False positives — the escape hatch

1. **Inline** — add a `gitleaks:allow` comment on the offending line.
2. **Allowlist** — add a `regexes` / `paths` / `stopwords` entry to `[allowlist]` in
   `.gitleaks.toml`.
3. Never delete the CI step or `--no-verify` past the local hook to "fix it later" — that's
   how a credential reaches history (effectively permanent once pushed).

## Why the binary, not `gitleaks/gitleaks-action`

The gitleaks **Action** requires a paid `GITLEAKS_LICENSE` on GitHub **organizations** (it
refuses to run without one — `"[org] is an organization. License key is required."`). The
gitleaks **binary** is MIT/open and is the *same engine with the same ruleset* — the Action
is just a wrapper around it. So `secret-scan.yml` downloads the pinned binary and runs
`gitleaks dir` directly: identical detection, zero license, works on org repos. `secret-scan.sh`
does the same for non-GitHub CI.

## Pinning / supply-chain

`secret-scan.yml` pins the **gitleaks release version** (`GITLEAKS_VERSION`) and the sha256
of its linux_x64 tarball (`GITLEAKS_SHA256`, from the release's `gitleaks_<version>_checksums.txt`);
the download is verified with `sha256sum -c` before it is extracted, into a job-local directory
on `PATH` (so a non-root self-hosted runner user, e.g. a Forgejo/Gitea act runner, can install
it). `actions/checkout` is pinned to a commit SHA. Bump version and digest together, deliberately.

See the [`secret-scanning`](../../skills/universal/secret-scanning/SKILL.md) skill for the
full local-hook + CI + tiers + extension story.
