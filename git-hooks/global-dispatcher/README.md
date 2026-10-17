# Global git-hook dispatcher

A **single, extensible** mechanism that runs a growing set of global git hooks in
**every** repo on this machine — including repos whose local hook manager
(lefthook / husky) or raw `.git/hooks` overrides `core.hooksPath` and would otherwise
shadow the global hooks entirely.

> **This supersedes `install-local-secret-scan.sh`** (the narrow, secret-scan-only
> injector left on the `ci-gate-suite` branch). That script wired ONE hook into one
> repo. This wires the **dispatcher** instead, so secret-scan *and every future global
> hook* run with no further per-repo edits. Reconcile the two at merge — keep this one.

## The model in one paragraph

There is ONE entry point, `run-global-hooks <event>`, and one directory of hooks,
`~/.config/git/global-hooks.d/<event>/`. A "global hook" is just an executable file
dropped into that directory. The dispatcher enumerates and runs them all, in sort order,
for the given event; any non-zero exit blocks. Every repo is wired to call the dispatcher
**once per event** — so adding a new global hook later is a one-file drop that
automatically takes effect everywhere, with zero per-repo changes.

```
~/.config/git/
├── run-global-hooks                 # THE DISPATCHER (one entry point)
├── global-hooks.d/                  # the single source of truth — drop hooks here
│   ├── pre-commit/
│   │   ├── 10-secret-scan           # gitleaks tiered staged-diff gate (block + warn)
│   │   └── 20-python-only           # rig-ecosystem tools are Python (twin of ci/python-only/python_only.py)
│   ├── commit-msg/
│   └── pre-push/
│       └── 10-protect-main          # block direct pushes to main/master (audited PUSH_MAIN_OK escape)
├── hooks/                           # the GLOBAL composers (core.hooksPath target)
│   ├── pre-commit                   # repo-local hook → dispatcher → review-gate
│   ├── commit-msg                   # repo-local hook → dispatcher
│   ├── pre-push                     # repo-local hook → dispatcher
│   └── review-gate                  # review-before-commit gate
├── template/hooks/                  # init.templateDir → new repos get dispatcher hooks
├── install-local-hooks.sh           # per-repo injection (+ --commit, + auto-dedup)
└── hooks-sweep                      # MANUAL retrofit scan (no cron, run anytime)
```

A wired repo may also carry a tracked **`.githooks-skip`** file at its root — the
per-repo dedup list (capability ids the dispatcher must NOT run there because the repo
runs them locally). See **Dedup** below. `selfcheck.sh` (next to the scripts) proves the
wiring and the dedup with throwaway repos.

The files here in `agent-tools/git-hooks/global-dispatcher/` are the **shipped twins** of
the live `~/.config/git/` copies. Keep them in sync.

## Adding a new global hook (the whole point)

```sh
# 1. write the hook (gets event args; pre-push refs on stdin). Non-zero = block.
cat > ~/.config/git/global-hooks.d/pre-commit/20-no-todo-in-prod <<'EOF'
#!/bin/sh
git diff --cached -U0 | grep -nE '^\+.*TODO\(prod\)' && {
  echo "no-todo-in-prod: remove TODO(prod) before committing" >&2; exit 1; }
exit 0
EOF
chmod +x ~/.config/git/global-hooks.d/pre-commit/20-no-todo-in-prod
```

That's it. It now runs in EVERY wired repo. No installer, no sweep, no per-repo edit. The
number prefix controls order (`10-` before `20-`). Mirror the file into this dir in
agent-tools and commit it to keep the shipped twin current.

## Per-repo injection — `install-local-hooks.sh`

Wires ONE repo by detecting its hook manager and idempotently injecting a single
dispatcher call per event (guarded by a `global-git-hooks-dispatcher` marker — re-running
is a no-op):

| Manager  | What it injects                                                              |
| -------- | --------------------------------------------------------------------------- |
| lefthook | a `global-git-hooks-dispatcher` command under each event (MERGED, not clobbered) |
| husky    | a guarded dispatcher line appended to `.husky/<event>`                       |
| raw      | the dispatcher call inserted after the shebang in `.git/hooks/<event>` (body kept) |

```sh
install-local-hooks.sh [--commit] [--no-dedup] [REPO_DIR]   # default: repo of $PWD
```

- `--commit` — after wiring, commit the **tracked** wiring changes (`lefthook.yml`,
  `.husky/<event>`, `.githooks-skip`) in the repo with a fixed conventional message
  (`chore(hooks): wire global git-hook dispatcher ...`). It stages **only the exact files
  this run wrote**, so unrelated changes are never bundled. Makes the wiring a
  reproducible, tracked step rather than a working-tree edit you must remember to commit.
  It is a no-op if nothing changed, and it never tries to commit raw `.git/hooks` files
  (those aren't tracked). The commit runs with `--no-verify` (committing the wiring
  through the very hooks being wired would be circular). **Safety:** if a file it would
  touch was **already dirty** before the run (a pre-existing unrelated edit in the same
  file), `--commit` refuses and asks you to commit by hand — it cannot split your hunk
  from the wiring hunk. A real `git add`/`git commit` error makes the script exit
  non-zero; "nothing to commit" and "refused (dirty)" are deliberate no-ops.
- `--no-dedup` — skip the automatic secret-scan detection (see **Dedup** below).

Run it from a `~/work`/`~/xp` repo, or pass `REPO_DIR`. For lefthook in a pull-only
checkout, run on a branch (or with `--commit`) — it edits the tracked file in place.

Repos with **no** local `core.hooksPath` override need nothing — the global composer in
`~/.config/git/hooks/` already covers them.

## Dedup — run each check EXACTLY once (local vs. global)

A repo with a local hook manager **and** its own copy of a check (e.g. its own lefthook
`gitleaks` command) would run that check twice once the dispatcher is also wired in: once
locally, once via the global fragment. The dispatcher prevents this with a deterministic
**capability-id** skip list.

- **Capability id** — every global fragment has a stable id: the value of a
  `# global-hook-id: <id>` header line, else its basename minus the numeric prefix
  (`10-secret-scan` → `secret-scan`). Ids are matched literally and case-sensitively.
- **Opt out** of an **unprotected** capability in a repo via any of (any match skips that
  ONE fragment in that repo only):
  1. `git config --get-all hooks.skipGlobal` — multi-valued; repo-local or global.
  2. env `GLOBAL_HOOKS_SKIP="id1:id2"` — ad-hoc / CI override.
  3. **tracked** `<repo>/.githooks-skip` — one id per line, `#` comments; the
     committable, reproducible source that travels with the repo.
- **Trust (security):** a fragment can mark itself **protected** with a
  `# global-hook-protected: true` header (the secret scan is). For a protected id, only
  sources a **cloned repo cannot forge** may skip it:
  - `git config hooks.skipGlobal` — **always** (you set it; a repo can't set your git
    config), and
  - the **tracked** `.githooks-skip` — **only** if you opt in via **git config**
    `git config --global hooks.trustSkipFile true`.

  Both trusted sources are **git config**, read from your local/global config — a cloned
  tree can't ship `.git/config`. **Env vars are not trusted** for protected ids (neither
  `GLOBAL_HOOKS_SKIP` nor any trust flag): a repo's lefthook `env:` block can inject env,
  so honoring it would defeat the protection. Otherwise the protected fragment
  **still runs** (fail-safe), so a repo you cloned cannot silently switch your secret scan
  off. The skip file must be a **regular file** at the repo root (a symlink is rejected) so
  a cloned repo can't point it outside the worktree. The retrofit writes **only** the
  tracked file (reproducible) — it never auto-writes a trusted git config, because silently
  disabling a security gate from a heuristic match (or leaving it disabled after the local
  hook is removed) is exactly the foot-gun the protection exists to prevent. To dedup a
  protected scan, opt in **once, explicitly**: `git config --global hooks.trustSkipFile
  true` (honor every tracked file) **or** `git -C <repo> config --add hooks.skipGlobal
  secret-scan` (this repo only). Until then a retrofitted repo with a local secret scan
  runs it locally *and* globally (one extra scan) rather than risk skipping it unsafely.

`install-local-hooks.sh` does this for you: it detects an existing local secret scan and
writes `secret-scan` into `.githooks-skip` (committed too with `--commit`). Detection is
**conservative, pre-commit-scoped, and active-manager-scoped** — the global `secret-scan`
fragment is a *pre-commit* hook, so the installer only looks at the **active** manager's
**pre-commit** gate (the lefthook `pre-commit:` block, or `.husky/pre-commit`, or the raw
`pre-commit` in the resolved hooks dir honouring a non-default `core.hooksPath` — whichever
manager actually owns the hooks). It only counts an **active, blocking** command — full-line
and inline `#` comments are stripped, and a **neutralized** invocation (`gitleaks … ||
true` / `|| :` / `|| exit 0`) is dropped — that runs `gitleaks` at a command position or
invokes a `secret-scan` / `no-secrets` helper. So a commented-out `# no-secrets:` block, a
`# gitleaks handled in CI` note, a `fail_text: gitleaks missing`, a `git-hooks/no-secrets-scan`
helper that no hook invokes, a scan wired **only to pre-push**, a **stale inactive**
`.husky/pre-commit` in a lefthook repo, or a neutralized `|| true` scan do **not** count —
the global pre-commit scan is never disabled by accident. A repo with **no** active local
pre-commit secret scan gets no skip entry, so the global fragment runs there as usual.
Verify with the self-check below.

```sh
sh git-hooks/global-dispatcher/selfcheck.sh   # proves wiring + dedup (no double-runs)
```

## Never-forget — three passive layers, NO schedule

1. **`init.templateDir`** → `git config --global init.templateDir ~/.config/git/template`.
   Every `git init` / `git clone` copies `template/hooks/<event>` (which call the
   dispatcher) into the new repo's `.git/hooks`. New raw repos are wired automatically.
2. **Manager templates** (`templates/lefthook.yml`, `templates/husky/*`) — preseeded
   with the dispatcher call. Copy one when starting a new lefthook/husky repo and it's
   wired from day one.
3. **`hooks-sweep`** — a MANUAL, run-anytime scan of `~/work` + `~/xp` that finds
   local-manager repos not yet wired and injects (idempotent) + reports. It mops up
   EXISTING repos created before wiring. **It is NOT scheduled** — no cron, no launchd.
   Run it when you remember; the passive layers above catch the rest.

```sh
hooks-sweep --dry-run                 # report what WOULD be wired; change nothing
hooks-sweep                           # wire unwired managed repos under ~/work + ~/xp
HOOKS_SWEEP_SKIP="/path/a:/path/b" hooks-sweep   # skip active-agent repos
hooks-sweep DIR1 DIR2                 # scan other roots
```

The sweep prunes worktrees, caches, `node_modules`, `cloned-projects`, and vendored deps;
it skips **linked worktrees** (their `.git` is a file) because they share the main repo's
tracked hook config — wire the main repo once and worktrees inherit it.

## Bypass / escape hatches

- Docs-only commits pass without a review stamp when every staged path is a documentation
  extension (`*.md`, `*.mdx`, `*.markdown`, `*.rst`, `*.adoc`, `*.rdoc`, `*.pod`) or a
  recognized prose/media file under the root `docs/` tree (`*.txt`, `*.text`, common raster
  images, or `*.pdf`). Agent instruction Markdown (`AGENTS.md`, `CLAUDE.md`, `SKILL.md`,
  `skills/**`, `agent-hooks/**`) and code/config-like files under `docs/` still require review.
- `git commit --no-verify` — Git's blanket bypass for ALL pre-commit gates (last resort;
  there is no `REVIEW_SKIP` self-service bypass in the review gate).
- Secret-scan false positive: inline `gitleaks:allow` comment, or an `[allowlist]` in
  `~/.config/gitleaks/gitleaks.toml` (global) / the repo's `.gitleaks.toml`.

## Why a git hook (and where it can't reach)

Git hooks enforce mechanically checkable gates at commit/push time. A git hook literally
cannot block its own `--no-verify` bypass — rules that must fire mid-session or resist
bypass belong in `../../agent-hooks/`. See `../README.md` and `../../docs/carrier-decision-guide.md`.
