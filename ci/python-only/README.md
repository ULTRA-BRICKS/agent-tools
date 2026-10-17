# python-only — rig-ecosystem tools are written in Python

**Engine:** `python_only.py` here — one stdlib Python file, no dependencies. It is both the
CI check (this slot) and the global pre-commit fragment
(`git-hooks/global-dispatcher/global-hooks.d/pre-commit/20-python-only`, a byte-identical
copy; `tests/test_python_only.py` keeps the two equal, so edit one and copy it over).

**Why:** the owner decided on 2026-06-14 ("Okay, we're moving the tools to Python") and restated
on 2026-10-17 ("all rig-ecosystem tools must be in Python") that every rig-ecosystem tool is
Python. Written down only as advice, the rule drifted: tg-cli gained hundreds of TypeScript
files after the decision, and this repo gained a 1,383-line bash script. See AGENTS.md
"Python-only tools" and [agent-tools#945](https://git.hyperide.ai/ultrabricks/agent-tools/issues/945).

## What it checks

Only in a **rig-ecosystem tool repo**: the `origin` is in an owner namespace
(`git.hyperide.ai/ultrabricks`, `github.com/alex-mextner`, `github.com/ultrabricks`) and the
repo name ends in `-cli` or is listed in `REPOS` (agent-tools, text-lint, claude-accounts).
Anything else (apps, sites, bots, third-party clones) passes without a check.

In those repos, every added, changed or copied file fails when it is:

- a non-Python language by extension: TypeScript/JavaScript (incl. `.vue .svelte .astro`),
  Go, Rust, Ruby, Swift, C/C++, Java and the rest of `NON_PYTHON_EXT` in the checker;
- any other file whose shebang runs a non-Python interpreter (node, bun, deno, tsx, npx,
  ruby, perl, pwsh, ... — `OTHER_INTERPRETERS`), whatever its name (`tg`, `run.command`,
  `x.txt`, even `tool.py`);
- a shell script (`.sh .bash .zsh .ksh .fish`, or a shell shebang) longer than **40 lines**.
  Short glue is fine.

Deletions, symlinks and submodules pass. An unchanged move of a file that was already
non-compliant where it was passes too; a move that turns a passing file into a failing one
(a rename to `.ts`, out of an exempt directory) fails. While resolving a merge, only content
that is new relative to both parents counts.

## Tiers

- **block** (default): the commit or the PR fails.
- **warn**: printed, never fails. Granted only in `REPOS`, only with the port ticket that ends
  it. Today: tg-cli, until [tg-cli#768](https://git.hyperide.ai/ultrabricks/tg-cli/issues/768)
  lands.

## Exemptions (the escape hatch)

Only code whose host forces another language: omp/opencode/pi extensions, browser assets,
the oxlint plugin API, VS Code extension-host code, a macOS-native Swift helper. Add an
`Allow(glob, reason)` to that repo's `RepoPolicy` in `REPOS` (a reason is mandatory; the
checker refuses to run with an empty one), land it as an agent-tools PR, then `rig apply`.
The registry is central on purpose: the repo being checked cannot exempt itself, and the CI
job runs the base-branch copy.

GNOME Shell extensions laid out as installed (`**/gnome-shell/extensions/<uuid>/**`) and the
oxlint files rig generates in a repo that enables `linters.rules` (root `oxlint.config.ts`,
`tools/oxlint/anti-slop/**`) are exempt in every repo (`HOST_FORCED_ALLOW`). Host files rig
generates outside any repo, like `~/.omp/agent/extensions/rig-permissions-guard.ts`, are never
checked.

Downstream, the vendored checker, its workflow and the vendored gate scripts are trusted by
path: the checker does not compare them with agent-tools. A PR that edits them shows in its
diff, and `rig status` reports an edited copy as drift (rig-cli#725); `rig apply` restores it.

The exact CI scripts that `rig apply` vendors from agent-tools `ci/` (`VENDORED_FILES`, e.g.
`ci/leftover-grep/leftover-grep.sh`, `.github/scripts/checklist-gate.mjs`) are exempt
downstream; any other shell under `ci/` is not, and at their source in agent-tools they are not.

## Install

`rig.yaml`:

```yaml
ci:
  items:
    python-only: { enabled: true, tier: block }
```

`rig apply` writes `.github/workflows/python-only.yml` and vendors `ci/python-only/python_only.py`
(rig-cli with [rig-cli#725](https://git.hyperide.ai/ultrabricks/rig-cli/issues/725); older rig
versions copy only `.sh`/`.mjs` companions, so copy the checker by hand there).
The pre-commit fragment comes with the global git-hook dispatcher (`rig apply` on the machine).

## Run by hand

```sh
python3 ci/python-only/python_only.py                                  # staged diff
python3 ci/python-only/python_only.py --base origin/main --head HEAD   # a branch
python3 ci/python-only/python_only.py --base origin/main --origin https://git.hyperide.ai/ultrabricks/x-cli
```

Exit codes: `0` pass, warn, or out of scope; `1` blocked; `2` git error or invalid registry.
