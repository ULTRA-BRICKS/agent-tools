---
name: secret-scanning
description: 'Use when setting up secret-leak protection for a repo — a pre-commit guard, a CI check, or both — or deciding how to stop credentials reaching git history. Standard engine: gitleaks. Covers wiring it as a git-hook (local) and CI (backstop), block-vs-warn tiers, and extending rules/allowlisting false positives.'
---

# Secret scanning: gitleaks, as a git-hook AND in CI

A credential in git history is the most expensive mistake in this list — once pushed it is
effectively permanent (rotate it, you can't un-leak it). Guard against it in **two places**,
with **one engine**.

## The engine is gitleaks — don't reinvent it

[gitleaks](https://github.com/gitleaks/gitleaks) is the de-facto standard secret scanner:
a maintained ruleset for ~hundreds of providers (AWS, GCP, Azure, GitHub, Slack, Stripe,
private keys, JWTs…), entropy heuristics, an allowlist + inline-comment escape hatch, and a
`.gitleaks.toml` extension model. Do not hand-roll regexes — wrap gitleaks. (`brew install
gitleaks`, or `go install github.com/gitleaks/gitleaks/v8@latest`, or the release tarball.)

## Two carriers, same engine

- **Local — pre-commit git-hook.** Scans the **staged** diff before the commit lands. Fast
  feedback; stops the secret on the committer's machine. See `git-hooks/no-secrets-scan`.
- **CI — backstop.** Scans on every push/PR. Catches anyone whose local hook is missing,
  disabled, or `--no-verify`-bypassed. See `ci/secret-scan/` (pinned GitHub Action +
  generic shell script).

You want **both**: the hook for speed, CI because hooks are not enforceable (a committer can
always skip them — CI can't be skipped without touching the pipeline).

## Block vs warn — two tiers

- **BLOCK** — high-confidence findings (a real provider credential format, a PEM private
  key). These **abort** the commit / **fail** the CI job. No judgment call; it's a secret.
- **WARN** — low-confidence heuristics (a high-entropy blob, a `secret`/`token`/`password`
  variable assigned an opaque literal that matches no provider rule). These are **printed
  but allowed** — most are false positives, and hard-blocking them trains people to
  `--no-verify`, which defeats the whole guard.

Implement the warn tier as a **separate gitleaks pass** with a lighter config whose exit
code is ignored (the scan still finds; the wrapper just doesn't fail on it). Locally, run
both passes every commit. In CI, default to **block-only** — a non-failing CI check is
ignored and provides no protection; make warn an explicit opt-in (a `continue-on-error`
job, or `SECRET_SCAN_WARN_CONFIG` for the shell runner).

## Extending — rules & allowlist

gitleaks auto-reads `.gitleaks.toml` at the repo root. Inherit a shared baseline and add to
it:

```toml
title = "my-repo"
[extend]
useDefault = true               # gitleaks' built-in provider rules
# path = "~/.config/gitleaks/gitleaks.toml"   # or extend a global/org config

[[rules]]
id = "acme-internal-token"
description = "ACME internal service token"
regex = '''acme_(live|test)_[0-9a-zA-Z]{32}'''
keywords = ["acme_"]
```

Keep custom **block** rules high-confidence (fixed prefixes / lengths). Anything fuzzy
belongs in the **warn** config, not the block one — or it generates noise people ignore.

## The escape hatch (must always exist)

A guard with no recourse gets ripped out. Always provide, and name in the block message:

1. **Inline** — a `gitleaks:allow` comment on the offending line.
2. **Allowlist** — a `regexes` / `paths` / `stopwords` entry under `[allowlist]` in
   `.gitleaks.toml` (or a global config).
3. (Documented last resort, discouraged) — `--no-verify` / a skip env var bypasses the
   local hook; CI still catches it.

Use the escape hatch for **proven non-secrets** (documented example keys like
`AKIAIOSFODNN7EXAMPLE`, test fixtures) — never to wave through a real credential "for now".

## Where the pieces live in this repo

- `git-hooks/no-secrets-scan` — the local pre-commit gitleaks wrapper.
- `ci/secret-scan/secret-scan.yml` — pinned GitHub Actions workflow (block tier).
- `ci/secret-scan/secret-scan.sh` — generic runner for any CI (block + opt-in warn): a short POSIX-sh launcher for
  `ci/secret-scan/secret_scan.py`, so it needs `python3` and both files side by side.
- `ci/secret-scan/gitleaks.toml` / `gitleaks-warn.toml` — the block / warn tier configs.
