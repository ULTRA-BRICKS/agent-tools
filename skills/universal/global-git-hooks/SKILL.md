---
name: global-git-hooks
description: 'Use to make a git hook run in EVERY repo, or when a local hook manager (lefthook/husky) or raw .git/hooks bypasses global core.hooksPath. Covers the dispatcher model (one entry point, a global-hooks.d/ drop-in dir), wiring a single repo, and the passive layers that keep new repos covered.'
---

# Global git hooks: one dispatcher, runs everywhere

A global `core.hooksPath` covers repos that don't override it. But a repo with a local
hook manager (lefthook → `.git/hooks`, husky → `.husky/_`) or its own raw `.git/hooks`
**overrides `core.hooksPath` and silently shadows every global hook**. The result: your
secret-scan (and anything else you put in the global path) does NOT run in exactly the
repos most likely to need it. The fix is a dispatcher, not N copies of N hooks.

## The model

- **One entry point**: `~/.config/git/run-global-hooks <event>`.
- **One drop-in directory**: `~/.config/git/global-hooks.d/<event>/`. A global hook is
  just an executable file there. The dispatcher runs them all in sort order; any non-zero
  blocks; an empty dir passes.
- **Every repo calls the dispatcher once per event.** Adding a new global hook later is a
  single-file drop — it then runs in every wired repo with ZERO per-repo edits.

Do NOT inject individual hooks into repos. Inject the **dispatcher** (one line per event).
That's what makes "add a hook once, it runs everywhere" true.

## Add a hook (the entire workflow)

```sh
printf '#!/bin/sh\n<your check; non-zero to block>\nexit 0\n' \
  > ~/.config/git/global-hooks.d/pre-commit/20-my-check
chmod +x ~/.config/git/global-hooks.d/pre-commit/20-my-check
```

Number prefixes order them (`10-` before `20-`). pre-push hooks get the pushed refs on
stdin; commit-msg hooks get the message-file path as `$1`.

## Wire a repo

```sh
install-local-hooks.sh [--commit] [--no-dedup] [REPO_DIR]
                                     # detects lefthook | husky | raw, injects ONE
                                     # dispatcher call per event, idempotent (marker)
```

- **lefthook**: a `global-git-hooks-dispatcher` command is MERGED under each event (never
  clobbers your lint/test commands). The `run:` value MUST be a single-quoted YAML scalar.
- **husky**: a guarded dispatcher line is appended to `.husky/<event>`.
- **raw**: the dispatcher call is inserted after the shebang in `.git/hooks/<event>`, so it
  runs even if the existing body later `exit`s; the body is preserved.
- **no override**: nothing to do — the global composer already covers the repo.
- `--commit`: commit the TRACKED wiring (`lefthook.yml` / `.husky/*` / `.githooks-skip`)
  so it's a reproducible step, not a working-tree edit to remember. No-op if unchanged.
- `--no-dedup`: skip the automatic secret-scan detection (see Dedup below).

For a pull-only checkout (e.g. a shared main), inject into the manager's tracked config on
a **branch**, not the working tree, and commit it there (or use `--commit`).

## Dedup — each check runs EXACTLY once (local vs. global)

A repo that already runs a check locally (its own lefthook `gitleaks` command, a raw
`.git/hooks` secret scan) and is ALSO wired to the dispatcher would run that check twice.
Fix: every global fragment has a stable **capability id** (a `# global-hook-id: <id>`
header, else basename minus the `NN-` prefix — `10-secret-scan` → `secret-scan`), and a
repo opts out of a capability so the dispatcher skips that ONE fragment there only.
Opt-out sources (any match skips; precedence top-down):

1. `git config --get-all hooks.skipGlobal` (multi-valued; genuinely user-controlled),
2. env `GLOBAL_HOOKS_SKIP="id1:id2"` (ad-hoc / CI),
3. tracked `<repo>/.githooks-skip` (one id per line, `#` comments) — committable,
   reproducible, travels with the repo (must be a regular file, not a symlink).

**Trust:** a fragment can mark itself `# global-hook-protected: true` (the secret scan
does). For a protected id, only **git config** can skip it: `git config hooks.skipGlobal`
(always) and the **tracked** file (only if `git config hooks.trustSkipFile true`). **Env
vars are not trusted** for protected ids (a repo's lefthook `env:` block can inject them);
otherwise the fragment **still runs** — a cloned repo can't silently disable your secret
scan, and the skip file must be a regular file (symlinks rejected).

`install-local-hooks.sh` auto-detects a local secret scan in the **active** manager's
pre-commit (ignoring comments, neutralized `|| true` scans, stale inactive hooks, and
pre-push-only scans) and writes `.githooks-skip` (reproducible). It does **not** auto-write
a trusted git config — to dedup the *protected* secret scan you opt in once, explicitly:
`git config --global hooks.trustSkipFile true` or `git -C <repo> config --add
hooks.skipGlobal secret-scan`. Prove it: `sh git-hooks/global-dispatcher/selfcheck.sh`.

## Never forget — three passive layers, NEVER a schedule

1. `git config --global init.templateDir ~/.config/git/template` — new `git init`/`clone`
   repos get dispatcher hooks automatically.
2. Manager templates (`templates/lefthook.yml`, `templates/husky/*`) preseeded with the
   dispatcher call — copy one when starting a new managed repo.
3. `hooks-sweep` — a MANUAL, run-anytime scan of `~/work`+`~/xp` that retrofits existing
   unwired managed repos (idempotent). **Do not** install a cron/launchd job for it; the
   passive layers catch new repos and the sweep is there for the occasional mop-up.

```sh
hooks-sweep --dry-run        # see what would be wired
hooks-sweep                  # wire them (idempotent)
```

## Don't

- Don't copy each hook into each repo — that's the N×M trap this replaces.
- Don't unquote the lefthook `run:` value — `run: "..." pre-commit` is invalid YAML.
- Don't schedule the sweep. It's a tool, not a daemon.
- Don't put bypass-resistant or mid-session rules here — a git hook can't block its own
  `--no-verify`. Those belong in an agent hook (`agent-tools/agent-hooks/`).

Reference implementation + the shipped script twins:
`agent-tools/git-hooks/global-dispatcher/` (README there has the full file map).
