# package-store-guard

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 20

A `pre-bash` agent-hook that keeps the machine's shared package stores, and the installs that
link into them, intact — keeps banned, disk-filling container VM runtimes (colima & co.) off
the machine, and stops hook managers from overwriting rig's global git hooks.

## Why

rig's `bun.enabled` writes `~/.bunfig.toml` with `linker = "isolated"` and `globalStore = true`.
Every bun worktree's `node_modules/.bun/<pkg>` is then a symlink into
`~/.bun/install/cache/links/`, so a worktree's `node_modules` costs a few MB. uv venvs are cloned
from `~/.cache/uv`. Four agent habits kept undoing that:

1. **Symlinking a worktree's `node_modules` to the main checkout** (`ln -s <main>/node_modules
   node_modules`). With bun 1.4.2, `bun install` inside such a worktree writes *through* the link
   and corrupts the main checkout (hypercalendarbot, 2026-09-09).
2. **Wiping the store to free disk.** `bun pm cache rm` (2026-09-15) deleted `links/` under every
   worktree at once: earlier installs became real copies and later ones got broken links.
3. **`npm install` / `npm ci` in a bun repo.** npm ignores `bun.lock`, uses no shared store and
   no clonefile, so every install is a full real copy. Third-party skills (e.g. a "using git
   worktrees" skill) tell agents to `npm install` in each new worktree.
4. Improvising installs at all, because nothing provisioned the new worktree
   (`rig worktree create` now runs `bun install --frozen-lockfile` in bun repos).

## What it blocks

| Shape | Examples | Instead |
| --- | --- | --- |
| bun store wipe | `bun pm cache rm`; `rm -rf ~/.bun/install/cache` (also `~/.bun`, `~/.bun/install/cache/links`, `$BUN_INSTALL_CACHE_DIR`, `rm -rf "$(bun pm cache)"`, `X=$(bun pm cache); rm -rf "$X"`, `mv`/`trash`/`rmdir`/`find [-L] … -delete`/`-exec rm` of it, `ls <store> \| xargs rm`) | `rig worktree gc --yes` / `rig worktree remove <name>`, or delete one worktree's `node_modules` and re-run `bun install --frozen-lockfile` |
| uv cache wipe | `uv cache clean`; `uv cache prune --all` / `--ci`; `rm -rf ~/.cache/uv` (also `$UV_CACHE_DIR`, `${UV_CACHE_DIR:-…}`, `$XDG_CACHE_HOME/uv`) | same — plain `uv cache prune` (unused entries only) stays allowed |
| `node_modules` symlink | `ln -s <main>/node_modules node_modules`, `ln -sfn …`, `ln -s <main>/node_modules .`, `ln -s <main>/node_modules <existing-dir>`, `ln -s <main>/node_modules` | `bun install --frozen-lockfile` in the worktree |
| foreign PM in a bun project | npm `install`/`i`/`ci`/`add`/`update`/`uninstall`/`rm`/`prune`/`dedupe`, pnpm `install`/`add`/`update`/`remove`/`prune`, bare `yarn`/`yarn install`/`add`/`remove`, where the **nearest** lockfile (cwd, `cd`, `--prefix`, `--dir`, `--cwd`, `pnpm -C`, walking up) is `bun.lock`/`bun.lockb` | `bun install --frozen-lockfile`, `bun add <pkg>`, `bun remove <pkg>` |
| banned container VM runtime | `colima start`/`restart`/`install`, `limactl start`/`create`, `brew install`/`reinstall`/`upgrade` of `colima`/`lima`/`podman`/`podman-desktop`/`orbstack`/`docker-desktop` (and `--cask docker`), `nix run`/`shell`/`profile install nixpkgs#colima` (conloca's `act-sim.sh` suggests it), `nix-env -iA nixpkgs.colima`, `nix-shell -p colima`, and launching the app (`open -a Docker`, `open -a OrbStack`) | docker compose through Apple `container` / `mocker`; the sme-archiving-gc tests through the native fake-gcs-server backend |
| hook manager into a global hooksPath | `lefthook install`/`add`, `husky`/`husky install`/`init`, `simple-git-hooks` (also via `npx`/`bunx`/`pnpm exec`) when `core.hooksPath` resolves OUTSIDE the repo — rig's global dispatcher dir, where the shim replaces the pre-commit composer (secret scan + review gate) for every repo | install into the repo's own `.git/hooks`, which the global composer runs first: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$(git rev-parse --absolute-git-dir)/hooks" lefthook install`; `rig status` flags a replaced composer, `rig apply` restores it |

Colima and similar VMs are banned on this machine: their disk images kept filling the Mac's disk,
and agents kept reinstalling them. `colima status`/`stop`/`delete`, `limactl list`/`stop`, and
`brew uninstall colima` stay allowed (they only inspect or free space).

Allowed on purpose: `npm i -g <tool>` (global), npm in a subproject whose nearest lockfile is
`package-lock.json`, `npm run …`/`yarn test`/`npx …`, `rm -rf node_modules` in a worktree,
symlinks created *inside* `node_modules`, a symlink merely pointing at a `node_modules` under
another name, read-only `find … -exec test`/`-print` in the store, and `bun pm cache` (prints the
path).

Commands are parsed, not raw-matched: `&&`/`;`/`|` segments, `cd`/`pushd`/`popd`/`cd -` (for
relative paths and lockfile lookup), shell assignments (`NAME=…`, `export NAME=$(bun pm cache)`)
and `${NAME:-default}`, every `$( … )`/backtick body wherever it appears (`echo $(bun pm cache rm)`),
wrappers (`sudo [-D dir]`, `env [-C dir] [-S '…'] [-a name]`, `exec -a name`, `timeout`, `nohup`, `xargs`, `busybox`, `xcrun`),
`bash -c`/`sh -lc`/`fish -c`/`eval` payloads with their `$1`/`$@` operands bound,
`ssh host '<cmd>'` remote commands, here-docs and here-strings fed to a shell (`bash <<EOF`,
`cat <<EOF | sh`, `ssh host <<EOF`, `bash <<< '…'`), `python -m uv`, package runners (`npx`,
`bunx`, `corepack`, `npm exec`, `pnpm dlx`: `npx npm install` is `npm install`), brace expansion
(`rm -rf ~/.bun/install/{cache,links}`), `mkdir` earlier in the same command, redirections, `cp -s` (a symlink by another name), and renaming or copying a
symlink onto `node_modules` (`ln -s … .nm && mv .nm node_modules`, `cp -P .nm node_modules`).
Other here-doc bodies, quoted multi-line arguments and comments are data and are skipped.
`node_modules` and store paths compare case-insensitively on macOS/Windows. A command shlex
cannot parse (an unbalanced quote) is re-read leniently instead of allowed — the real shell would
reject it anyway. `uv cache clean <pkg>` is blocked too (the user's rule is "never clean the
caches"); ask the human for a one-package clean.

A deleting `find` (`-delete`, `-exec rm …`) is blocked when its walk starts at, inside, or ABOVE a
store — filters such as `-type d`, `-name cache` or `-prune` still match store entries. Start the
walk at the directory you mean (`find ./build -name '*.o' -delete`).

Known gaps (best effort, not a sandbox): the RESULT of a substitution used as an argument or as the
command word (`rm -rf $(cat list)`, `$(echo rm) -rf …`), a package's own install script running a
hook manager (a `postinstall: lefthook install` — fix the package), deletes from other languages (`python -c 'shutil.rmtree(…)'`), a
variable set in an earlier, separate tool call, and a deliberately obfuscated command.

## Fail policy

`on_error: closed` in the descriptor. The script itself allows on an internal inspection error
(a parser bug must not wedge every shell call); the descriptor policy covers the host-side
failures (timeout, missing interpreter). No self-service bypass and no hatch: a genuine
exception (a truly corrupt store) is the human's call, and the human runs it themselves.

## Install

Installed by `rig apply` with the rest of the catalog when `agent_hooks.all: true`, into every
configured harness's hook dir (Claude Code `~/.claude/hooks`, Codex `~/.codex/hooks`, opencode
`~/.config/opencode/hooks`, omp `~/.omp/agent/hooks`); the per-harness bridges route each shell
tool call to `pre-bash`. `blurb.md` adds one SessionStart line so agents learn the rule before
they hit the block.
