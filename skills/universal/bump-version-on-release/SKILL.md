---
name: bump-version-on-release
description: Use when releasing/shipping a code change in a versioned tool — a CLI, package, or service that declares a version. Bump the declared version (pyproject.toml, dynamic pyproject source, Poetry, or package.json) every release, and make `--version` read that declared version dynamically instead of a hardcoded literal that silently goes stale.
---

# Bump the version on every release — and read it dynamically

A tool's version is a freshness signal: it lets a user (and you) tell which build they're
running and whether a fix actually landed. That signal only works if two things hold on
every release:

1. the **declared** version is bumped, and
2. `--version` reads that **same declared** value at runtime.

Break either and the version lies. The canonical failure: `rig --version` printed a
hardcoded `0.1.0` for the entire life of the tool — the literal was baked into the code and
the version was never bumped across many releases. So `--version` told every user `0.1.0`
no matter which build they had: a useless, permanently-stale string, not a freshness signal.
Worse, a hardcoded literal can drift from the declared `pyproject.toml`, dynamic version
source, Poetry metadata, or `package.json` version, so the two disagree and neither is
trustworthy.

## Rule

- **Bump the declared version on every release** of a code change. The declared version is
  the single source of truth:
  - Python: `[project] version = "X.Y.Z"` in `pyproject.toml`.
  - Python dynamic metadata: `[project] dynamic = ["version"]` with a declared source such
    as `[tool.setuptools.dynamic] version = {attr = "pkg.VERSION"}`, `{file = "VERSION"}`,
    or `[tool.setuptools.dynamic.version] attr = "pkg.VERSION"`. For simple release gates,
    prefer single-line inline tables or the subtable form; multi-line inline tables may need
    an explicit `SHIP_VERSION_FILES` source.
  - Poetry: `[tool.poetry] version = "X.Y.Z"` in `pyproject.toml`.
  - Node: `"version": "X.Y.Z"` in `package.json`.
- **Semver the bump to the change:**
  - **patch** (`0.4.1` → `0.4.2`) — a bugfix, no API change.
  - **minor** (`0.4.1` → `0.5.0`) — a backward-compatible feature.
  - **major** (`0.4.1` → `1.0.0`) — a breaking change.
- **`--version` MUST read the declared version dynamically — never a hardcoded literal.**
  Resolve it from package metadata at runtime so it can't drift:
  - Python: `importlib.metadata.version("your-dist")` (installed dist), or read the
    configured pyproject/static/dynamic source for a source checkout.
  - Node: read `version` from `package.json` (e.g. `require('./package.json').version`).
- **Docs-only / pure-test / pure-CI / a revert** is not a release — no bump required. The
  rule fires only when **shippable source** actually changed.

## Why

The version is the only thing a user can read to answer "is the fix in the build I'm
running?". If the literal is hardcoded it answers that question wrong forever; if it isn't
bumped on release it answers "same as last time" forever. Both turn the one cheap freshness
signal into noise. Reading the declared version dynamically makes drift structurally
impossible — there's one number, and bumping it is the entire release ritual.

## Generic example

```toml
# pyproject.toml — the single source of truth
[project]
name = "mytool"
version = "0.5.0"        # bump THIS every release (was 0.4.1; minor: added a subcommand)
```

Dynamic Python packages still have one source of truth; it just lives outside the project
table:

```toml
[project]
name = "mytool"
dynamic = ["version"]

[tool.setuptools.dynamic]
version = {attr = "mytool.VERSION"}  # bump mytool/__init__.py's VERSION every release
```

```python
# mytool/cli.py — --version reads the declared version, never a literal
from importlib.metadata import version, PackageNotFoundError

def get_version() -> str:
    try:
        return version("mytool")          # dynamic: tracks pyproject's [project] version
    except PackageNotFoundError:
        return "0.0.0+unknown"            # source checkout without install metadata
    # NEVER:  return "0.1.0"   <- a literal that goes stale the moment you ship and forget
```

A release is then: edit the one `version` line, ship. `--version` updates itself — there is
no second place to remember, so it can't drift and can't go stale.
