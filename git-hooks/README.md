# Git hooks

Copyable git hooks plus a `lefthook.yml` example. When a repo declares
`scripts.test` in `rig.yaml` and the `dev` CLI is on PATH, the hooks run
`dev run test` as the repo's test command. Otherwise they fall back to auto-detecting
**bun/node**, **python (uv)**, and **go** from files in the repo root, so the same file
drops into any of the three kinds of project.

> **Want a hook to run in EVERY repo on the machine — even ones with lefthook/husky that
> override `core.hooksPath`?** Use the **global dispatcher** in
> [`global-dispatcher/`](global-dispatcher/README.md): one entry point + a drop-in
> `~/.config/git/global-hooks.d/` directory, plus `install-local-hooks.sh` and a manual
> `hooks-sweep` to retrofit existing repos. The standalone hooks below are the per-repo
> building blocks; the dispatcher is how you make them universal. See the
> `global-git-hooks` skill.

## What's here

| File              | Hook point   | Does                                                              |
| ----------------- | ------------ | ---------------------------------------------------------------- |
| `pre-commit`      | `pre-commit` | lint + typecheck + tests, zero-warnings — the quality gate       |
| `commit-msg`      | `commit-msg` | conventional-commit format validator                             |
| `pre-push`        | `pre-push`   | full test suite before the push leaves your machine              |
| `no-secrets-scan` | (any)        | gitleaks wrapper over the staged diff — blocks committing secrets|
| `lefthook.yml`    | —            | the same gates expressed for the `lefthook` runner               |

## Install — plain git hooks

```bash
# Copy the hooks into the repo's .git/hooks and make them executable.
cp git-hooks/pre-commit git-hooks/commit-msg git-hooks/pre-push .git/hooks/
chmod +x .git/hooks/pre-commit .git/hooks/commit-msg .git/hooks/pre-push
# no-secrets-scan is invoked by pre-commit; keep it on PATH or alongside the hooks.
cp git-hooks/no-secrets-scan .git/hooks/ && chmod +x .git/hooks/no-secrets-scan
```

> `.git/hooks` is not version-controlled, so each clone must install them. To version the
> hooks, set `git config core.hooksPath .githooks` and keep them in a tracked `.githooks/`
> dir — or use lefthook (below), which manages installation for you.

## Install — lefthook (recommended for teams)

```bash
# lefthook installs the git hooks from lefthook.yml and is itself a tracked dependency.
lefthook install
```

`lefthook.yml` is the source of truth; `lefthook install` wires up the actual git hooks.
This is the team-friendly path — the config is committed, so every clone gets the same
gates after one `lefthook install`.

## Stack detection

The standalone hooks first prefer the repo-owned test script:

- `rig.yaml` top-level `scripts.test` + `dev` on PATH -> `dev run test`

When that is absent, they detect the toolchain by what's in the repo root:

- `package.json` (+ `bun.lockb`/`bunfig.toml` or not) → **bun/node**
- `pyproject.toml` / `uv.lock` → **python (uv)**
- `go.mod` → **go**

and run the corresponding lint/typecheck/test commands. Prefer moving project-specific test
details into `rig.yaml` `scripts.test`; adjust the fallback command lists only when a repo
cannot use `dev`.

## Carrier choice

Git hooks enforce rules at commit/push time — mechanically checkable gates (format, types,
tests pass, no secrets). Rules that need to fire *mid-session* (e.g. blocking a
`--no-verify` bypass, or a secret *before* it's written) belong in `../agent-hooks/`
instead — a git hook literally can't block its own bypass. See
`../docs/carrier-decision-guide.md`.
