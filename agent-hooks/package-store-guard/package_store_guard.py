#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — keep the shared package stores and store-backed installs intact.

Accessed via: harness PreToolUse bash -> a hook bridge -> this script (rig apply installs the
descriptor from the agent-tools catalog when agent_hooks.all is true, for every harness kind:
Claude Code, Codex, opencode and omp all route their shell tool through ``pre-bash``).

Assumptions: rig's ``bun.enabled`` writes ``~/.bunfig.toml`` with ``linker = "isolated"`` and
``globalStore = true``. Every bun worktree's ``node_modules/.bun/<pkg>`` is then a symlink into
``~/.bun/install/cache/links``, so the bun cache IS the store: wiping it breaks every install on the
machine. uv venvs are cloned from ``~/.cache/uv``; wiping it turns every later sync into a cold,
full-copy download. The hook reads the command string only (plus the lockfiles that decide whether a
directory is a bun project, and whether an ``ln`` destination is a directory); it never runs it.

Blocks six shapes, each with the command to run instead:

1. ``bun pm cache rm`` (the whole global store);
2. deleting or moving ``~/.bun/install/cache`` (or an ancestor such as ``~/.bun``, or anything
   inside it) with ``rm``/``rmdir``/``unlink``/``trash``/``mv``/``find -delete``/``… | xargs rm``;
3. ``uv cache clean``, ``uv cache prune --all``/``--ci``, and deleting ``~/.cache/uv``;
4. creating a symlink named ``node_modules`` (``ln -s``, ``ln -sfn``, ``ln -s <src> .``, ...);
5. npm/pnpm/yarn install-class verbs (install/ci/add/update/uninstall/remove/prune, bare ``yarn``)
   where the nearest lockfile is ``bun.lock``/``bun.lockb``. A directory whose nearest lockfile is
   only ``package-lock.json`` (an npm-only subproject) stays allowed, as do global installs;
6. installing or starting a banned container VM runtime (``colima start``, ``limactl start``,
   ``brew install colima|lima|podman|orbstack|docker-desktop``, ``nix run nixpkgs#colima``): their
   disk images filled the Mac's disk and agents kept reinstalling them. Compose goes through Apple
   ``container`` / ``mocker``, the sme-archiving-gc tests through the native fake-gcs-server backend.

Parsing is best-effort but deliberately wide: ``&&``/``;``/``|`` segments, ``cd``/``pushd``,
``NAME=value`` / ``NAME=$(bun pm cache)`` assignments (and the substitutions inside them),
``${NAME:-default}``, wrappers (``sudo``, ``env -S``/``env -C``, ``timeout``, ``xargs``,
``busybox``), ``bash -c``/``eval``/``ssh host '…'`` payloads with ``$1``/``$@`` bound, here-docs
fed to a shell, redirections, and ``cp -s``. Other here-doc bodies are data, not commands. A
command shlex cannot parse is re-read leniently rather than allowed. Known gaps: command
substitutions used as arguments (``rm -rf $(cat list)``) and code in other languages
(``python -c 'shutil.rmtree(…)'``).

Past bugs this exists for (2026-09): agents symlinked a worktree's node_modules to the main
checkout; bun 1.4.2's ``bun install`` inside such a worktree wrote THROUGH the link and corrupted
the main checkout (hypercalendarbot, 2026-09-09). Agents then deleted the links and ran full npm
installs (npm never clonefiles, so every install is a real copy). A disk-cleanup agent ran
``bun pm cache rm`` on 2026-09-15 and destroyed the global store under every worktree.
No self-service bypass: a genuine exception is the human's call.
"""

from __future__ import annotations

import fnmatch
import functools
import importlib.util
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
import time

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# The role-aware "what to do next" builder (agent-tools#994): loaded by repo-local path and
# OPTIONAL — a load failure falls back to the neutral text in `_render_block` (a crash would fail
# open via on_error and silently drop this block).
_GUARD_MESSAGE_MODULE = "agenttools_guard_message"


def _load_guard_message():
    init = Path(__file__).resolve().parents[2] / "lib" / _GUARD_MESSAGE_MODULE / "__init__.py"
    if not init.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_GUARD_MESSAGE_MODULE, init)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_GUARD_MESSAGE_MODULE)
    # dataclasses resolves string annotations through sys.modules[cls.__module__], so the module
    # must be registered while it executes.
    sys.modules[_GUARD_MESSAGE_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        if previous is None:
            sys.modules.pop(_GUARD_MESSAGE_MODULE, None)
        else:
            sys.modules[_GUARD_MESSAGE_MODULE] = previous
        if isinstance(exc, KeyboardInterrupt):
            raise
        return None
    return module


_guard_message = _load_guard_message()
# The event main() is deciding, so the block emitters can name the reader's role without threading
# it through every signature. One event per hook process.
_CURRENT_EVENT: dict = {}
HOOK_ID = "package-store-guard"

# ── shell grammar ─────────────────────────────────────────────────────────────
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_PIPES = frozenset({"|", "|&"})
_PUNCT = frozenset("();<>|&")
_ESCAPED_SEMI = "__package_store_guard_escaped_semicolon__"
_SUBST_MARK = "__psg_subst_{}_{}__"
# Longest first, so `&&` wins over `&` and `;;&` over `;;`.
_OPERATORS = ("<<<", ";;&", "&>>", "&&", "||", ";;", ";&", "|&", "&>", ">>", ">&", "<&", ">|", "<>", "<<",
              "(", ")", ";", "|", "&", "<", ">")
_LEADING_SHELL_NOISE = frozenset({"(", ")", "{", "}", "!", "then", "do", "else", "elif"})
_ASSIGNMENT = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$", re.DOTALL)
_DECLARERS = frozenset({"export", "local", "declare", "typeset", "readonly"})
_REDIRECT_OPS = frozenset({">", ">>", "<", ">&", "&>", "&>>", "<&", ">|", "<>", "<<", "<<<"})
_HEREDOC_RE = re.compile(
    r"<<(?!<)(?P<dash>-)?[ \t]*(?:'(?P<sq>[^'\n]*)'|\"(?P<dq>[^\"\n]*)\"|(?P<bare>[^\s'\"<>|&;()]+))"
)
_VAR_RE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<default>[^}]*))?\}"
    r"|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)
_GLOB_CHARS = re.compile(r"[*?\[]")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer", "command",
    "sudo", "doas", "exec", "caffeinate", "busybox", "xargs", "xcrun",
})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "env": frozenset({"-u", "--unset", "-a", "--argv0"}),
    "exec": frozenset({"-a"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from", "-h", "--host", "-p", "--prompt"}),
    "doas": frozenset({"-u", "-C"}),
    "xargs": frozenset({"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a", "-J", "-R", "-S",
                        "--max-args", "--max-procs", "--delimiter", "--arg-file"}),
}
# Wrapper flags whose value is a directory the wrapped command runs in.
_WRAPPER_CHDIR_FLAGS: dict[str, frozenset[str]] = {
    "env": frozenset({"-C", "--chdir"}),
    "sudo": frozenset({"-D", "--chdir"}),
}
_ENV_SPLIT_FLAGS = frozenset({"-S", "--split-string"})
_SSH_VALUE_FLAGS = frozenset({
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p",
    "-Q", "-R", "-S", "-W", "-w", "-B",
})
_MAX_DEPTH = 4  # nested bash -c / eval / ssh / env -S / -exec payloads
_MAX_VAR_DEPTH = 4  # `${A:-${B:-…}}` defaults
_MAX_WRAPPERS = 16  # sudo env timeout … peeled per command
_MAX_LOCKFILE_WALK = 64  # parent dirs searched for the nearest lockfile

# ── package managers and stores ───────────────────────────────────────────────
_BUN_LOCKFILES = ("bun.lock", "bun.lockb")
_OTHER_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")
_NPM_INSTALL_VERBS = frozenset({
    "install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall",
    "add", "ci", "clean-install", "ic", "install-clean", "isntall-clean", "install-test", "it",
    "install-ci-test", "cit", "update", "up", "upgrade", "udpate",
    "uninstall", "unlink", "remove", "rm", "r", "un", "prune", "dedupe", "ddp",
})
_PNPM_INSTALL_VERBS = frozenset({
    "install", "i", "add", "update", "up", "upgrade", "remove", "rm", "uninstall", "un", "prune",
})
_YARN_INSTALL_VERBS = frozenset({"install", "add", "upgrade", "up", "remove"})
# Value-taking flags shared by npm/pnpm/yarn. npm's `-w <workspace>` takes a value; pnpm's and
# yarn's `-w` (workspace root) do not — treating it as one there would swallow the verb.
_PM_VALUE_FLAGS = frozenset({
    "--prefix", "-C", "--dir", "--cwd", "-w", "--workspace", "--filter", "-F", "--registry",
    "--cache", "--userconfig", "--loglevel", "--reporter", "--mutex", "--modules-folder",
})
_PNPM_YARN_VALUE_FLAGS = _PM_VALUE_FLAGS - {"-w"}
_PM_DIR_FLAGS = frozenset({"--prefix", "--dir", "--cwd"})
_PM_GLOBAL_FLAGS = frozenset({"-g", "--global", "--location=global", "--location=user"})
_INFO_FLAGS = frozenset({"-v", "--version", "-h", "--help"})
_BUN_CACHE_VERBS = frozenset({"rm", "clean", "clear", "remove", "delete"})
_UV_VALUE_FLAGS = frozenset({
    "--cache-dir", "--directory", "--project", "--config-file", "--color", "--python", "-p",
    "--allow-insecure-host", "--python-preference",
})
_UV_PRUNE_WIPE_FLAGS = frozenset({"--all", "--ci"})
_DELETERS = frozenset({"rm", "rmdir", "unlink", "trash", "srm", "shred", "gio"})
_MOVERS = frozenset({"mv"})
_REMOVERS = _DELETERS | _MOVERS
_FIND_EXEC_OPS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_VALUELESS_OPTS = frozenset({"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"})
_FIND_EXPR_START = frozenset({"(", "!", ")", "\\(", "\\!"})
# `$(bun pm cache)` / `$(uv cache dir)` — the substitution must END there: `$(bun pm cache rm)`
# is a wipe, not a path, and must reach the bun check instead.
_STORE_SUBST_RE = re.compile(r"\b(?P<bun>bun\s+pm\s+cache)\s*[)`]|\b(?P<uv>uv\s+cache\s+dir)\s*[)`]")
_CASE_INSENSITIVE_FS = sys.platform in {"darwin", "win32"}
_STORE_SPAN_RE = re.compile(
    r"\$\(\s*(?:(?P<bun>bun\s+pm\s+cache)|(?P<uv>uv\s+cache\s+dir))\s*\)"
    r"|`\s*(?:(?P<bun2>bun\s+pm\s+cache)|(?P<uv2>uv\s+cache\s+dir))\s*`"
)

# ── banned VM runtimes (colima & co. filled the Mac's disk; agents kept reinstalling them) ──
_VM_RUNTIMES = frozenset({"colima", "lima", "podman", "podman-desktop", "orbstack", "docker-desktop"})
_VM_START_VERBS = {"colima": frozenset({"start", "restart", "install"}), "limactl": frozenset({"start", "create"})}
_VM_SUBCOMMANDS = {
    "colima": frozenset({
        "start", "stop", "restart", "delete", "status", "list", "ls", "ssh", "ssh-config", "kubernetes",
        "nerdctl", "prune", "template", "update", "version", "completion", "help", "install",
    }),
    "limactl": frozenset({
        "start", "stop", "create", "delete", "rm", "list", "ls", "shell", "edit", "copy", "cp", "info",
        "prune", "factory-reset", "sudoers", "disk", "snapshot", "validate", "show-ssh", "protect",
        "unprotect", "start-at-login", "help", "completion", "tunnel",
    }),
}
# every _VM_START_VERBS head needs its _VM_SUBCOMMANDS list (pinned by the tests, not an import-time
# assert: a failing import would block every shell call through on_error: closed)
# flags that take NO value; any other flag before the subcommand consumes the next word
_VM_BOOL_FLAGS = {
    "colima": frozenset({"-v", "--verbose", "--very-verbose", "-h", "--help", "--version", "-f", "--force"}),
    "limactl": frozenset({"--debug", "-h", "--help", "-v", "--version", "--tty", "-y", "--yes"}),
}
_VM_APPS = frozenset({
    "docker", "docker desktop", "orbstack", "podman desktop", "colima",
    "com.docker.docker", "dev.kdrag0n.macvirt", "com.orbstack.orbstack", "io.podman_desktop.podmandesktop",
})
_BREW_INSTALL_VERBS = frozenset({"install", "reinstall", "upgrade"})
# hook managers → the verbs that (re)write hook shims into core.hooksPath ("" = bare command)
_HOOK_MANAGER_INSTALLS = {
    "lefthook": frozenset({"install", "add"}),
    "husky": frozenset({"", "install", "init"}),
    "simple-git-hooks": frozenset({""}),
}
_GIT_TIMEOUT_S = 1.5  # one query; answers are memoized per inspected command
_GIT_BUDGET_S = 3.0  # all git queries of one hook run: well inside the 5 s descriptor budget
_NIX_RUNNERS = frozenset({"run", "shell", "develop", "build"})
# `uvx` / `uv [tool] run` options that take a value before the launched command
_UV_RUN_VALUE_FLAGS = frozenset({
    "--from", "--with", "-w", "--with-editable", "--with-requirements", "--python", "-p", "--package",
    "--project", "--directory", "--env-file", "--group", "--extra", "--index", "--default-index",
    "--index-url", "--extra-index-url", "-i", "--constraints", "-c", "--overrides", "--cache-dir",
})

_STORE_WHAT = (
    "[package-store-guard] BLOCKED: this would wipe a shared package store ({what}). "
    "The bun global store (~/.bun/install/cache, including links/) is what every worktree's "
    "node_modules symlinks into, and uv venvs are cloned from ~/.cache/uv — wiping either breaks "
    "or re-bloats every install on the machine."
)
_STORE_SELF_SERVE = (
    "Never clean package-manager caches or stores to free disk. To reclaim space instead: remove "
    "stale worktrees (`rig worktree gc --yes`, `rig worktree remove <name>`), or delete ONE "
    "worktree's node_modules and re-run `bun install --frozen-lockfile` there."
)
_STORE_RELAY = (
    "If a subagent reported this block, hand it the reclaim steps above via `write agent://<id>`; "
    "only a genuine store wipe needs the human."
)
_STORE_HUMAN = "a real store wipe is the human's decision alone — ask the human; there is no self-service bypass."
_SYMLINK_MSG = (
    "[package-store-guard] BLOCKED: do not symlink node_modules to another checkout. `bun install` "
    "in a worktree whose node_modules is a symlink writes THROUGH it and corrupts the other "
    "checkout (bun 1.4.2 did exactly this on 2026-09-09). Run `bun install --frozen-lockfile` in "
    "this worktree instead: with rig's ~/.bunfig.toml (isolated linker + globalStore) it only "
    "creates symlinks into the shared store — a few MB, seconds — unless the repo's own "
    "bunfig.toml asks for a local layout. `rig worktree create` already does this for bun repos."
)
_VM_WHAT = (
    "[package-store-guard] BLOCKED: {what} — colima/lima-style container VMs are banned on this "
    "machine (their disk images kept filling the Mac's disk, and agents kept reinstalling them)."
)
_VM_SELF_SERVE = (
    "For docker compose use Apple `container` / `mocker`; for the sme-archiving-gc tests use the "
    "native fake-gcs-server backend. Do not install or start another VM runtime (colima, lima, "
    "podman, OrbStack, Docker Desktop)."
)
_VM_RELAY = (
    "If a subagent reported this block, point it at the alternatives above via "
    "`write agent://<id>`; if none fit, that is a question for the human."
)
_VM_HUMAN = "if nothing else works, ask the human — there is no self-service bypass."
_NO_BYPASS = (
    "As a subagent never wipe a store or start a VM runtime yourself and never go to the human — "
    "your coordinator decides."
)


def _render_block(what: str, self_serve: str, human: str = "", relay: str = "") -> str:
    """Role-aware ladder via the shared builder; neutral two-branch text if the lib is unusable."""
    if _guard_message is not None:
        try:
            role = _guard_message.detect_role(_CURRENT_EVENT.get("event") or {})
            return _guard_message.render(
                _guard_message.Remedy(
                    what=what, self_serve=self_serve, relay=relay, human=human, no_bypass=_NO_BYPASS
                ),
                role,
            )
        except Exception:  # noqa: BLE001 - a wording helper must never turn a block into a crash
            pass
    top = "3. If you are the top-level agent: " + (relay or "supply the missing step to the blocked subagent.")
    if human:
        top += f" Last resort, genuine exceptions only: {human}"
    return "\n".join([
        what,
        f"1. {self_serve}",
        "2. If you were dispatched by another agent: `write agent://<parent-id>` with what is "
        "missing + what you tried, then `yield` status `awaiting-coordinator` (NOT "
        "blocked/done/failed); the coordinator's `write agent://<you>` revives you. "
        + _NO_BYPASS,
        top,
    ])


def _store_msg(what: str) -> str:
    return _render_block(_STORE_WHAT.format(what=what), _STORE_SELF_SERVE, _STORE_HUMAN, _STORE_RELAY)


def _vm_msg(what: str) -> str:
    return _render_block(_VM_WHAT.format(what=what), _VM_SELF_SERVE, _VM_HUMAN, _VM_RELAY)


_HOOK_MANAGER_MSG = (
    "[package-store-guard] BLOCKED: `{tool}` would write its hook shims into core.hooksPath = {path}, "
    "which is OUTSIDE this repo — rig's global hooks dir — and replace the global pre-commit "
    "composer (secret scan + review gate) for EVERY repo (a postinstall `lefthook install` did "
    "exactly that on 2026-09-26). The global composer already runs this repo's own "
    ".git/hooks/<hook> first, so install the manager THERE: "
    "`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath "
    "GIT_CONFIG_VALUE_0=\"$(git rev-parse --absolute-git-dir)/hooks\" {tool}`. If the global "
    "pre-commit is already a shim, `rig status` flags it and `rig apply` restores it."
)
_NPM_MSG = (
    "[package-store-guard] BLOCKED: `{tool}` in a bun project ({lockdir} has {lockfile}). "
    "npm/pnpm/yarn ignore bun.lock and write a full real copy of node_modules (no shared store, "
    "no clonefile). Use `bun install --frozen-lockfile` to install, `bun add <pkg>` / "
    "`bun remove <pkg>` to change dependencies. A skill or doc telling you to `npm install` in a "
    "new worktree means `bun install --frozen-lockfile` here. Global tool installs (`npm i -g`) "
    "and npm-only subprojects (nearest lockfile is package-lock.json) are not affected."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


# ── tokenizing ────────────────────────────────────────────────────────────────


def _basename(tok: str) -> str:
    return tok.replace("\\", "/").rsplit("/", 1)[-1]


def _cmd(tok: str) -> str:
    """A command word's name: its basename, casefolded where command lookup is case-insensitive
    (macOS/Windows run `Bun pm cache rm` and `RM -rf` as bun and rm)."""
    name = _basename(tok)
    return name.casefold() if _CASE_INSENSITIVE_FS else name


def _unquoted_heredocs(line: str) -> list[tuple[str, bool, str, str]]:
    """(delimiter, `<<-`, text before `<<`, text after the opener) for each here-doc a line opens
    OUTSIDE quotes and comments."""
    found: list[tuple[str, bool, str, str]] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;&|("):
            break  # the rest of the line is a comment
        elif line.startswith("<<", i) and not line.startswith("<<<", i):
            m = _HEREDOC_RE.match(line, i)
            if m:
                delim = m.group("sq") if m.group("sq") is not None else m.group("dq")
                if delim is None:
                    delim = m.group("bare").replace("\\", "")  # `<<\EOF` quotes it too
                found.append((delim, bool(m.group("dash")), line[:i], line[m.end():]))
                i = m.end()
                continue
        i += 1
    return found


def _stdin_script_dir(stage: str) -> str | None:
    """If this pipeline stage reads a shell script from stdin (`bash`, `sudo -u root sh -s`,
    `env -C dir bash`, `ssh host`), the directory that script runs in (``"."``: unchanged);
    else None. Wrappers are peeled by the same :func:`_unwrap` as commands."""
    try:
        toks = shlex.split(stage)
    except ValueError:
        toks = stage.split()
    toks, cwd, _nested, _xargs = _unwrap(toks, _Ctx("."))
    if not toks:
        return None
    head, args = _cmd(toks[0]), toks[1:]
    flags = [a for a in args if a.startswith("-")]
    operands = [a for a in args if not a.startswith("-")]
    if head in _SHELLS:
        # `bash -c '…'` / `bash script.sh` read stdin as data; `bash -s x` does not
        if any("c" in f[1:] for f in flags if not f.startswith("--")):
            return None
        return cwd if (not operands or "-s" in flags) else None
    if head == "ssh" and _ssh_remote_command(toks) is None:
        return _home()  # only a host: stdin is the remote login shell, in the remote home
    return None


def _heredoc_script_dir(before: str, after: str) -> str | None:
    """`bash <<EOF`, `ssh host <<EOF` or `cat <<EOF | bash`: the here-doc body is a script — the
    directory it runs in; None when the body is data."""
    found = _stdin_script_dir(re.split(r"&&|\|\||[;|&(]", before)[-1])
    if found is not None:
        return found
    for stage in re.split(r"&&|\|\||;", after)[0].split("|")[1:]:  # the rest of this pipeline
        found = _stdin_script_dir(stage)
        if found is not None:
            return found
    return None


def strip_heredoc_bodies(command: str) -> str:
    """Drop here-document bodies — `cat > f <<'EOF' … EOF` text is data, not commands — except
    a body fed to a shell (`bash <<EOF`, `cat <<EOF | sh`, `ssh host <<EOF`), which is a script."""
    out: list[str] = []
    pending: list[tuple[str, bool, str | None]] = []
    for line in command.split("\n"):
        if pending:
            delim, dash, script_dir = pending[0]
            bare = line.rstrip("\r")
            if (bare.lstrip("\t") if dash else bare) == delim:
                pending.pop(0)
                if script_dir not in (None, "."):
                    out.append("popd")
            elif script_dir is not None:
                out.append(line)
            continue
        out.append(line)
        for delim, dash, before, after in _unquoted_heredocs(line):
            script_dir = _heredoc_script_dir(before, after)
            if script_dir not in (None, ".") and not pending:
                out.append(f"pushd {shlex.quote(script_dir)}")  # the script runs there
            pending.append((delim, dash, script_dir))
    return "\n".join(out)


def substitution_bodies(text: str) -> list[str]:
    """The commands inside `$( … )`, backticks and process substitutions `<( … )`/`>( … )` (not in
    single quotes, not `$(( … ))` arithmetic): the shell runs them wherever they appear, so each is
    inspected on its own."""
    bodies: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "'" and quote is None:
            quote = "'"
        elif ch == "#" and quote is None and (i == 0 or text[i - 1] in " \t\r\n;&|()"):
            end = text.find("\n", i)  # a comment: nothing in it runs
            i = len(text) if end < 0 else end
            continue
        elif ch == '"':
            quote = None if quote == '"' else '"'
        elif (text.startswith("$(", i) and not text.startswith("$((", i)) or (
            quote is None and text.startswith(("<(", ">("), i)  # process substitution
        ):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            bodies.append(text[i + 2 : j - 1] if depth == 0 else text[i + 2 :])
            i = j
            continue
        elif ch == "`":
            end = text.find("`", i + 1)
            end = len(text) if end < 0 else end
            bodies.append(text[i + 1 : end])
            i = end + 1
            continue
        i += 1
    return [b for b in bodies if b.strip()]


def _newlines_to_separators(text: str) -> str:
    """Unquoted newlines separate commands; quoted ones stay inside their token. Comments are
    dropped here, shell-style: `#` starts one only at the start of a word, so `nixpkgs#colima`
    stays one word (shlex's own comment handling would cut it)."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            if text[i + 1] == "\n" and quote is None:
                out.append(" ")  # line continuation
            elif text[i + 1] == ";" and quote is None:
                out.append(_ESCAPED_SEMI)  # find's `-exec … \;` terminator, not a separator
            else:
                out.append(text[i : i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\r\n;&|()"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        elif ch == "\n":
            out.append("\n ; ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_operators(tok: str) -> list[str]:
    """shlex glues adjacent punctuation (`);`, `)&&`) into one token; split it into operators."""
    if not tok or any(ch not in _PUNCT for ch in tok):
        return [tok]
    out: list[str] = []
    i = 0
    while i < len(tok):
        op = next((o for o in _OPERATORS if tok.startswith(o, i)), tok[i])
        out.append(op)
        i += len(op)
    return out


def _protect_substitutions(text: str) -> tuple[str, list[str]]:
    """Swap each `$( … )` / backtick span (outside single quotes) for a placeholder word, so its
    parens and inner `;`/quotes neither split the surrounding word nor end the command."""
    spans: list[str] = [secrets.token_hex(6)]  # [0] is this call's nonce
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'":
            out.append(text[i : i + 2])
            i += 2
            continue
        if quote == "'":
            quote = None if ch == "'" else quote
            out.append(ch)
            i += 1
            continue
        end = -1
        if (text.startswith("$(", i) and not text.startswith("$((", i)) or (
            quote is None and text.startswith(("<(", ">("), i)  # `cat <(…)` is not a `<` redirect
        ):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            end = j
        elif ch == "`":
            close = text.find("`", i + 1)
            end = len(text) if close < 0 else close + 1
        if end > 0:
            spans.append(text[i:end])
            out.append(_SUBST_MARK.format(spans[0], len(spans) - 1))
            i = end
            continue
        if ch == "'" and quote is None:
            quote = "'"
        elif ch == '"':
            quote = None if quote == '"' else '"'
        out.append(ch)
        i += 1
    return "".join(out), spans


def _restore_substitutions(tok: str, spans: list[str]) -> str:
    """Put this call's `$( … )` spans back; a marker-like word typed in the command stays as is."""
    prefix = f"__psg_subst_{spans[0]}_"
    if prefix not in tok:
        return tok

    def back(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return spans[idx] if 0 < idx < len(spans) else m.group(0)

    return re.sub(re.escape(prefix) + r"(\d+)__", back, tok)


def _tokenize(command: str) -> list[str]:
    """Shell-ish tokens. A command shlex rejects (unbalanced quotes) is re-read leniently —
    the real shell would reject it too, so over-reading it costs nothing."""
    try:
        protected, spans = _protect_substitutions(command)
        lex = shlex.shlex(_newlines_to_separators(protected), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""  # comments were already dropped, word-start `#` only
        return [
            _restore_substitutions(piece.replace(_ESCAPED_SEMI, "\\;"), spans)
            for tok in lex
            for piece in _split_operators(tok)
        ]
    except ValueError:
        spaced = re.sub(r"(&&|\|\||;|\||&)", r" \1 ", command.replace("\n", " ; "))
        return [t.strip("'\"") for t in spaced.split() if t.strip("'\"")]


def _segments(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Simple commands, each with whether it is piped from the previous one."""
    segs: list[tuple[list[str], bool]] = [([], False)]
    for tok in tokens:
        if tok in _SHELL_SEPS:
            segs.append(([], tok in _PIPES))
        else:
            segs[-1][0].append(tok)
    return [s for s in segs if s[0]]


def _strip_redirects(argv: list[str]) -> list[str]:
    """Drop `>file`, `2>&1`, `<in`, `<<EOF` so their targets never read as operands."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _REDIRECT_OPS:
            if out and out[-1].isdigit():
                out.pop()  # the fd number of `2>` / `1>&`
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def _strip_leading(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and (argv[i] in _LEADING_SHELL_NOISE or _ASSIGNMENT.match(argv[i])):
        i += 1
    return argv[i:]


class _Ctx:
    """Per-command state: effective cwd (+ pushd stack / OLDPWD), shell variables, store paths the
    current pipeline named, and symlinks an earlier `ln -s` in the same command created."""

    def __init__(self, cwd: str, variables: dict[str, str] | None = None) -> None:
        self.cwd = cwd
        self.oldpwd = cwd
        self.dirstack: list[str] = []
        self.vars: dict[str, str] = dict(variables or {})
        self.store_seen: str | None = None  # label of a store path an earlier piped segment named
        self.links_made: set[str] = set()
        self.dirs_made: set[str] = set()  # `mkdir` earlier in the same command
        self.inline_env: dict[str, str] = {}  # `NAME=value cmd …` of the command being checked

    def is_dir(self, path: str) -> bool:
        """A directory on disk, or one an earlier `mkdir` in this command creates."""
        return os.path.isdir(path) or path in self.dirs_made

    def chdir(self, head: str, args: list[str]) -> None:
        """Track `cd X`, `cd`, `cd -`, `pushd X`, `popd` so relative paths resolve correctly."""
        dests = _positional(args)
        if head == "popd":
            if self.dirstack:
                self.oldpwd, self.cwd = self.cwd, self.dirstack.pop()
            return
        if dests and dests[0] == "-":
            self.cwd, self.oldpwd = self.oldpwd, self.cwd
            return
        target = _expand(dests[0], self.cwd, self.vars) if dests else _home()
        if head == "pushd":
            self.dirstack.append(self.cwd)
        self.oldpwd, self.cwd = self.cwd, target


def _unwrap(argv: list[str], ctx: _Ctx) -> tuple[list[str], str, list[str], bool]:
    """Peel wrappers. Returns (argv, cwd for it, nested command strings, came through xargs)."""
    cwd = ctx.cwd
    nested: list[str] = []
    via_xargs = False
    argv = _strip_leading(argv)
    for _ in range(_MAX_WRAPPERS):
        if not argv or _cmd(argv[0]) not in _WRAPPERS:
            break
        wrapper = _cmd(argv[0])
        via_xargs = via_xargs or wrapper == "xargs"
        value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
        chdir_flags = _WRAPPER_CHDIR_FLAGS.get(wrapper, frozenset())
        rest = argv[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            m = _ASSIGNMENT.match(tok)
            if m:
                if wrapper == "env":
                    ctx.inline_env[m.group("name")] = m.group("value")  # `env NAME=v cmd`
                i += 1
                continue
            flag, eq, attached = tok.partition("=")
            if wrapper == "env" and flag in _ENV_SPLIT_FLAGS:
                payload = attached if eq else (rest[i + 1] if i + 1 < len(rest) else "")
                nested.append(" ".join([payload, *rest[i + (1 if eq else 2):]]))
                rest = []
                break
            if flag in chdir_flags:
                target = attached if eq else (rest[i + 1] if i + 1 < len(rest) else "")
                cwd = _expand(target, cwd, ctx.vars)
                i += 1 if eq else 2
                continue
            if tok in value_flags:
                i += 2
                continue
            if eq and flag in value_flags:
                i += 1
                continue
            if tok.startswith("-"):
                i += 1
                continue
            break
        rest = rest[i:]
        if wrapper == "timeout" and rest and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", rest[0]):
            rest = rest[1:]
        argv = _strip_leading(rest)
    return argv, cwd, nested, via_xargs


# ── paths ─────────────────────────────────────────────────────────────────────


def _expand_vars(text: str, local: dict[str, str], _depth: int = 0) -> str:
    """`$NAME`, `${NAME}`, `${NAME:-default}` from command-local assignments, then the env."""

    def repl(m: re.Match[str]) -> str:
        name = m.group("braced") or m.group("bare")
        value = local.get(name, os.environ.get(name))
        op = m.group("op")
        if op and (value is None or (op == ":-" and value == "")):
            return _expand_vars(m.group("default") or "", local, _depth + 1) if _depth < _MAX_VAR_DEPTH else ""
        return value if value is not None else m.group(0)

    return _VAR_RE.sub(repl, text)


def _expand(path: str, cwd: str, local: dict[str, str] | None = None) -> str:
    """Expand ~ / $VARS and anchor a relative path at the effective cwd (no symlink resolution)."""
    expanded = os.path.expanduser(_expand_vars(path, local or {}))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def _components(path: str) -> list[str]:
    norm = os.path.normpath(path).replace("\\", "/")
    parts = [c for c in norm.split("/") if c not in ("", ".")]
    return [c.casefold() for c in parts] if _CASE_INSENSITIVE_FS else parts


def _home() -> str:
    return os.path.expanduser("~")


def bun_store_roots() -> list[str]:
    """Every bun global-store location this machine may use."""
    roots = [os.path.join(_home(), ".bun", "install", "cache")]
    bun_install = os.environ.get("BUN_INSTALL", "").strip()
    if bun_install:
        roots.append(os.path.join(os.path.expanduser(bun_install), "install", "cache"))
    explicit = os.environ.get("BUN_INSTALL_CACHE_DIR", "").strip()
    if explicit:
        roots.append(os.path.expanduser(explicit))
    return [os.path.normpath(r) for r in dict.fromkeys(roots)]


def uv_cache_roots() -> list[str]:
    """Every uv cache location this machine may use."""
    roots = [os.path.join(_home(), ".cache", "uv")]
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        roots.append(os.path.join(os.path.expanduser(xdg), "uv"))
    explicit = os.environ.get("UV_CACHE_DIR", "").strip()
    if explicit:
        roots.append(os.path.expanduser(explicit))
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        roots.append(os.path.join(local_app, "uv", "cache"))
    return [os.path.normpath(r) for r in dict.fromkeys(roots)]


_ROOT_ENV = ("HOME", "USERPROFILE", "BUN_INSTALL", "BUN_INSTALL_CACHE_DIR", "UV_CACHE_DIR", "XDG_CACHE_HOME", "LOCALAPPDATA")


# The argument is only the cache key: the roots depend on these env vars, so a HOME/BUN_INSTALL
# change must produce a new entry instead of stale roots.
@functools.lru_cache(maxsize=8)
def _roots_for(_env_key: tuple[str, ...]) -> tuple[tuple[str, list[str]], ...]:
    return tuple(
        [("the bun global store", _components(r)) for r in bun_store_roots()]
        + [("the uv cache", _components(r)) for r in uv_cache_roots()]
    )


def _protected_roots() -> tuple[tuple[str, list[str]], ...]:
    return _roots_for(tuple(os.environ.get(v, "") for v in _ROOT_ENV))


def _target_hits_root(target: list[str], root: list[str], *, allow_ancestor: bool) -> bool:
    """True when ``target`` components (may hold globs) equal, contain, or lie inside ``root``.

    Component-wise fnmatch, so `~/.bun/install/cache/*` and `~/.cache/*` match while
    `~/.cache/pip*` does not reach `~/.cache/uv`.
    """
    if not target:
        return allow_ancestor  # `/` itself contains everything
    for pattern, part in zip(target, root):
        if _GLOB_CHARS.search(pattern):
            if not fnmatch.fnmatchcase(part, pattern):
                return False
        elif pattern != part:
            return False
    if len(target) >= len(root):
        return True  # equal to the root, or inside it
    return allow_ancestor  # an ancestor of the root: deleting it deletes the root


def expand_braces(word: str, limit: int = 64) -> list[str]:
    """Bash brace expansion of a word (`a{b,c}d` → `abd`, `acd`; nested; `{x}` without a comma is
    literal), capped at ``limit`` results."""
    depth = 0
    start = -1
    commas: list[int] = []
    for i, ch in enumerate(word):
        if ch == "{":
            if depth == 0:
                start, commas = i, []
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and commas:
                pre, post = word[:start], word[i + 1 :]
                cuts = [start, *commas, i]
                out: list[str] = []
                for a, b in zip(cuts, cuts[1:]):
                    for mid in expand_braces(word[a + 1 : b], limit):
                        out.extend(expand_braces(pre + mid + post, limit))
                        if len(out) >= limit:
                            return out[:limit]
                return out
        elif ch == "," and depth == 1:
            commas.append(i)
    return [word]


def _protected_hit(raw_target: str, ctx_cwd: str, local: dict[str, str], *, allow_ancestor: bool = True) -> str | None:
    for word in expand_braces(_store_value(raw_target)):
        target = _components(_expand(word, ctx_cwd, local))
        for label, root in _protected_roots():
            if _target_hits_root(target, root, allow_ancestor=allow_ancestor):
                return label
    return None


def _store_value(value: str) -> str:
    """`$(bun pm cache)/links` → `<bun store>/links`; other substitutions are left in place."""
    value = _STORE_SPAN_RE.sub(lambda m: bun_store_roots()[0] if (m.group("bun") or m.group("bun2")) else uv_cache_roots()[0], value)
    return value


def _store_substitution(text: str) -> str | None:
    """`$(bun pm cache)` / `$(uv cache dir)` inside ``text`` → that store's path."""
    m = _STORE_SUBST_RE.search(text)
    if not m:
        return None
    return bun_store_roots()[0] if m.group("bun") else uv_cache_roots()[0]


# ── per-command checks ────────────────────────────────────────────────────────


def _positional(argv: list[str]) -> list[str]:
    out: list[str] = []
    end_of_opts = False
    for tok in argv:
        if not end_of_opts and tok == "--":
            end_of_opts = True
            continue
        if not end_of_opts and tok.startswith("-") and tok != "-":
            continue
        out.append(tok)
    return out


def _check_bun(argv: list[str]) -> str | None:
    words = [t for t in argv[1:] if not t.startswith("-")]  # a valued flag (`--cwd /x`) may sit in front
    for i in range(len(words) - 2):
        if words[i] == "pm" and words[i + 1] == "cache" and words[i + 2] in _BUN_CACHE_VERBS:
            return _store_msg(f"`bun pm cache {words[i + 2]}`")
    return None


def _check_uv(argv: list[str]) -> str | None:
    words: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _UV_VALUE_FLAGS:
            i += 2
            continue
        if not tok.startswith("-"):
            words.append(tok)
        i += 1
    if len(words) >= 2 and words[0] == "cache":
        if words[1] == "clean":
            return _store_msg("`uv cache clean`")
        if words[1] == "prune" and _UV_PRUNE_WIPE_FLAGS.intersection(argv):
            return _store_msg("`uv cache prune --all/--ci`")
    return None


def _check_remove(head: str, operands: list[str], rest: list[str], ctx: _Ctx, cwd: str) -> str | None:
    if _store_substitution(" ".join(rest)):
        return _store_msg(f"`{head}` of the store path")
    for target in operands:
        label = _protected_hit(target, cwd, ctx.vars)
        if label:
            return _store_msg(f"`{head} {target}` removes {label}")
    return None


def _check_delete(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    head = _cmd(argv[0])
    rest = argv[1:]
    if head == "gio":
        if not rest or rest[0] not in {"trash", "remove"}:
            return None
        rest = rest[1:]
    return _check_remove(head, _positional(rest), rest, ctx, cwd)


def _mv_cp_operands(rest: list[str]) -> tuple[list[str], str | None, bool]:
    """(sources, destination, destination is a directory by syntax) for `mv`/`cp` argv tails."""
    target_dir: str | None = None
    operands: list[str] = []
    end_of_opts = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not end_of_opts and tok == "--":
            end_of_opts = True
        elif not end_of_opts and tok in {"-t", "--target-directory"} and i + 1 < len(rest):
            target_dir = rest[i + 1]
            i += 1
        elif not end_of_opts and tok.startswith("--target-directory="):
            target_dir = tok.split("=", 1)[1]
        elif end_of_opts or not tok.startswith("-") or tok == "-":
            operands.append(tok)
        i += 1
    if target_dir is not None:
        return operands, target_dir, True
    if len(operands) < 2:
        return [], None, False
    return operands[:-1], operands[-1], operands[-1].endswith("/") or len(operands) > 2


def _renamed_to_node_modules(sources: list[str], dest: str | None, into_dir: bool, ctx: _Ctx, cwd: str) -> bool:
    """`mv .nm node_modules` / `cp -P .nm node_modules` where `.nm` is a symlink: the same
    write-through shape as `ln -s … node_modules`, one step later."""
    if dest is None:
        return False
    dest_abs = _expand(dest, cwd, ctx.vars)
    into_dir = into_dir or ctx.is_dir(dest_abs)
    for src in sources:
        src_abs = _expand(src, cwd, ctx.vars)
        if not (os.path.islink(src_abs) or src_abs in ctx.links_made):
            continue
        name = os.path.join(dest_abs, _basename(src_abs)) if into_dir else dest_abs
        if _is_node_modules(name):
            return True
    return False


def _check_mv(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    rest = argv[1:]
    sources, dest, into_dir = _mv_cp_operands(rest)
    if _renamed_to_node_modules(sources, dest, into_dir, ctx, cwd):
        return _SYMLINK_MSG
    return _check_remove("mv", sources, rest, ctx, cwd)


def _find_paths(rest: list[str]) -> list[str]:
    """Starting points of a `find`: after its leading options (-H/-L/-P, -O3, -D x, BSD -f p)."""
    paths: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _FIND_VALUELESS_OPTS or re.fullmatch(r"-O\d*", tok):
            i += 1
        elif tok in {"-f", "-D"} and i + 1 < len(rest):
            if tok == "-f":
                paths.append(rest[i + 1])
            i += 2
        else:
            break
    while i < len(rest) and not (rest[i].startswith("-") or rest[i] in _FIND_EXPR_START):
        paths.append(rest[i])
        i += 1
    return paths


def _find_deletes(rest: list[str]) -> bool:
    """`-delete`, or an `-exec`/`-execdir`/`-ok` whose command removes or moves files."""
    for i, tok in enumerate(rest):
        if tok == "-delete":
            return True
        if tok in _FIND_EXEC_OPS and i + 1 < len(rest) and _cmd(rest[i + 1]) in _REMOVERS:
            return True
    return False


def _find_exec_tails(rest: list[str]) -> list[list[str]]:
    """The commands after each `-exec`/`-execdir`/`-ok`, up to their `;`/`+` terminator."""
    tails: list[list[str]] = []
    i = 0
    while i < len(rest):
        if rest[i] in _FIND_EXEC_OPS:
            j = i + 1
            while j < len(rest) and rest[j] not in {"\\;", ";", "+"}:
                j += 1
            tails.append(rest[i + 1 : j])
            i = j
        i += 1
    return tails


def _check_find(argv: list[str], ctx: _Ctx, cwd: str, depth: int = 0) -> str | None:
    rest = argv[1:]
    paths = _find_paths(rest) or ["."]  # no start path: find walks `.`
    # an -exec tail is a command of its own (`-exec rm -rf <store> {} +`, `-exec bash -c '…'`)
    for tail in _find_exec_tails(rest):
        if tail and depth < _MAX_DEPTH:
            msg = inspect_command(" ".join(shlex.quote(t.replace("{}", paths[0])) for t in tail), cwd, depth + 1, ctx.vars)
            if msg:
                return msg
    if not _find_deletes(rest):
        return None
    # Any deleting walk rooted at, inside, or ABOVE a store can reach it: filters such as
    # `-type d`, `-name cache` or `-prune` still match store entries. Root the walk at the
    # directory you mean instead (`find ./build -name '*.o' -delete`).
    for path in paths:
        label = _protected_hit(path, cwd, ctx.vars, allow_ancestor=True)
        if label:
            return _store_msg(f"`find {path} ... -delete/-exec rm` reaches {label}")
    return None


def _ln_link_names(argv: list[str], cwd: str, local: dict[str, str] | None = None,
                   dirs_made: frozenset[str] | set[str] = frozenset()) -> list[str] | None:
    """Link paths an `ln -s` would create; None if this `ln` does not make a symlink."""
    symbolic = False
    no_target_dir = False
    no_deref = False
    target_dir: str | None = None
    operands: list[str] = []
    end_of_opts = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not end_of_opts and tok == "--":
            end_of_opts = True
        elif not end_of_opts and tok.startswith("--"):
            if tok == "--symbolic":
                symbolic = True
            elif tok == "--no-target-directory":
                no_target_dir = True
            elif tok == "--no-dereference":
                no_deref = True
            elif tok.startswith("--target-directory="):
                target_dir = tok.split("=", 1)[1]
            elif tok == "--target-directory" and i + 1 < len(argv):
                target_dir = argv[i + 1]
                i += 1
        elif not end_of_opts and tok.startswith("-") and tok != "-":
            flags = tok[1:]
            symbolic = symbolic or "s" in flags
            no_target_dir = no_target_dir or "T" in flags
            no_deref = no_deref or "n" in flags or "h" in flags
            if "t" in flags:
                after = flags.split("t", 1)[1]
                if after:
                    target_dir = after
                elif i + 1 < len(argv):
                    target_dir = argv[i + 1]
                    i += 1
        else:
            operands.append(tok)
        i += 1
    if not symbolic or not operands:
        return None
    if target_dir is not None:
        return [os.path.join(target_dir, _basename(src.rstrip("/"))) for src in operands]
    if len(operands) == 1:
        return [_basename(operands[0].rstrip("/"))]
    dest = operands[-1]
    into_dir = [os.path.join(dest, _basename(src.rstrip("/"))) for src in operands[:-1]]
    if no_target_dir:
        return [dest]
    if dest.endswith("/") or len(operands) > 2:
        return into_dir  # `ln -s SRC DIR/` and `ln -s A B DIR` always link INSIDE the dir
    # `ln -s SRC DEST` makes DEST, or DEST/basename(SRC) when DEST is an existing directory
    # (-n/-h: a symlink to a directory counts as a plain file).
    dest_abs = _expand(dest, cwd, local)
    if (os.path.isdir(dest_abs) or dest_abs in dirs_made) and not (no_deref and os.path.islink(dest_abs)):
        return into_dir
    return [dest]


def _is_node_modules(name: str) -> bool:
    base = _basename(name.rstrip("/"))
    return (base.casefold() if _CASE_INSENSITIVE_FS else base) == "node_modules"


def _check_ln(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    names = [_expand_vars(n, ctx.vars) for n in _ln_link_names(argv, cwd, ctx.vars, ctx.dirs_made) or []]
    if any(_is_node_modules(name) for name in names):
        return _SYMLINK_MSG
    ctx.links_made.update(_expand(n, cwd, ctx.vars) for n in names)
    return None


def _check_cp(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    """GNU `cp -s`/`--symbolic-link` (`-rs` builds a tree of links) is `ln -s` by another name, and
    copying a symlink itself (`cp -P`/`-a`, BSD `cp -R`) onto node_modules is a rename of it."""
    rest = argv[1:]
    sources, dest, into_dir = _mv_cp_operands(rest)
    if dest is None:
        return None
    flags = [t for t in rest if t.startswith("-") and t != "-"]
    symbolic = any(f == "--symbolic-link" or (not f.startswith("--") and "s" in f[1:]) for f in flags)
    if symbolic:
        dest_abs = _expand(dest, cwd, ctx.vars)
        into_dir = into_dir or ctx.is_dir(dest_abs)
        names = [os.path.join(dest, _basename(src.rstrip("/"))) for src in sources] if into_dir else [dest]
        names = [_expand_vars(n, ctx.vars) for n in names]
        if any(_is_node_modules(n) for n in names):
            return _SYMLINK_MSG
        ctx.links_made.update(_expand(n, cwd, ctx.vars) for n in names)  # like `ln -s`
        return None
    if _renamed_to_node_modules(sources, dest, into_dir, ctx, cwd):
        return _SYMLINK_MSG
    return None


def _vm_package(ref: str) -> str | None:
    """`colima`, `homebrew/cask/orbstack`, `nixpkgs#colima`, `nixpkgs.lima` → the banned name."""
    name = ref.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    name = name.split(".", 1)[1] if name.startswith(("nixpkgs.", "legacyPackages.")) else name
    name = name.split("@", 1)[0].lower()
    return name if name in _VM_RUNTIMES else None


def _vm_verb(head: str, rest: list[str]) -> str | None:
    """The subcommand of `colima …` / `limactl …`, found the way their CLI parser (cobra) finds it:
    a `--flag` without `=` consumes the next word as its value unless it is a known boolean, so
    `colima --profile stop status` runs `status` and `colima --profile ci --arch x start` runs
    `start`."""
    booleans = _VM_BOOL_FLAGS.get(head, frozenset())
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            i += 1 if ("=" in tok or tok in booleans or tok == "--") else 2
            continue
        return tok if tok in _VM_SUBCOMMANDS.get(head, frozenset()) else None
    return None


def _check_vm_runtime(argv: list[str]) -> str | None:
    """`colima start`, `limactl start`, `brew install colima`, `nix run nixpkgs#colima`, …"""
    head = _cmd(argv[0])
    words = [t for t in argv[1:] if not t.startswith("-")]
    flags = [t for t in argv[1:] if t.startswith("-")]
    if head in _VM_START_VERBS:
        verb = _vm_verb(head, argv[1:])
        return _vm_msg(f"`{head} {verb}`") if verb in _VM_START_VERBS[head] else None
    if head == "podman" and "machine" in words:
        # `podman [global flags] machine init|start` creates/boots podman's VM; stop/rm/list stay allowed
        after = words[words.index("machine") + 1 :]
        if after and after[0] in {"init", "start"}:
            return _vm_msg(f"`podman machine {after[0]}`")
        return None
    if head == "brew" and words and words[0] in _BREW_INSTALL_VERBS:
        for pkg in words[1:]:
            name = _vm_package(pkg)
            if name or ("--cask" in flags and pkg.rsplit("/", 1)[-1] == "docker"):
                return _vm_msg(f"`brew {words[0]} {pkg}`")
        return None
    if head == "nix" and words and (words[0] in _NIX_RUNNERS or words[:2] == ["profile", "install"]):
        for ref in words[1:]:
            if _vm_package(ref):
                return _vm_msg(f"`nix {words[0]} {ref}`")
        return None
    if head == "open":
        # `open -a Docker` / `open -a OrbStack` launch the VM app itself
        for i, tok in enumerate(argv[1:-1], start=1):
            if tok in {"-a", "-b"} and _basename(argv[i + 1]).lower().removesuffix(".app") in _VM_APPS:
                return _vm_msg(f"`open {tok} {argv[i + 1]}`")
        return None
    if head in {"nix-env", "nix-shell"} and any(f.startswith(("-i", "-p", "--install", "--packages")) for f in flags):
        for ref in words:
            if _vm_package(ref):
                return _vm_msg(f"`{head} … {ref}`")
    return None


_git_memo: dict[tuple[str, ...], str | None] = {}
_git_deadline = [0.0]  # monotonic deadline for this hook run's git queries (reset per command)


def _reset_git_budget() -> None:
    _git_memo.clear()
    _git_deadline[0] = time.monotonic() + _GIT_BUDGET_S


def _git(cwd: str, *args: str, git_dir: str | None = None) -> str | None:
    """A read-only git query in ``cwd`` (stdout, stripped), or None when it fails or this run's
    git budget is spent (a stalled git must not outlast the descriptor's timeout, which would
    block the command with a protocol error). ``git_dir`` mirrors a `GIT_DIR=…` the gated command
    sets, so the query sees the repo the command will. Answers are memoized per command, so
    `lefthook install && lefthook add …` asks git once."""
    cmd = ["git", "-C", cwd, *([f"--git-dir={git_dir}"] if git_dir else []), *args]
    key = tuple(cmd)
    if key in _git_memo:
        return _git_memo[key]
    remaining = _git_deadline[0] - time.monotonic()
    if remaining <= 0:
        return None
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=min(_GIT_TIMEOUT_S, remaining))
        out = res.stdout.strip() if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        out = None
    _git_memo[key] = out
    return out


def _check_hook_manager(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    """`lefthook install`, `husky`/`husky install`, `simple-git-hooks` write their shims into
    whatever core.hooksPath resolves to. When that is a path OUTSIDE the repo (rig's global
    dispatcher dir), they overwrite the global composer for every repo (2026-09-26: a postinstall
    `lefthook install` replaced ~/.config/git/hooks/pre-commit — secret scan and review gate)."""
    head = _cmd(argv[0])
    words = [t for t in argv[1:] if not t.startswith("-")]
    if not words and any(t in _INFO_FLAGS for t in argv[1:]):
        return None  # `husky --help`, `lefthook --version`; `lefthook install -v` still installs
    verb = words[0] if words else ""
    if verb not in _HOOK_MANAGER_INSTALLS[head]:
        return None
    env = {**ctx.vars, **ctx.inline_env}  # `export GIT_CONFIG_…; lefthook install` counts too
    git_dir_env = env.get("GIT_DIR") and _expand(env["GIT_DIR"].strip("\"'"), cwd, ctx.vars)
    paths = _git(cwd, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir", git_dir=git_dir_env)
    if not paths or len(paths.splitlines()) < 3:
        # outside any repo the manager has nothing to install into — unless GIT_DIR points
        # somewhere this check could not read: then fail closed
        return _HOOK_MANAGER_MSG.format(tool=_cmd(argv[0]), path="<unknown: GIT_DIR>") if git_dir_env else None
    top, git_dir, common = paths.splitlines()[:3]
    explicit = _explicit_hooks_path(env)
    if explicit is not None:
        # the documented per-command override — honoured only if it really lands in this repo
        for probe in ("$(git rev-parse --absolute-git-dir)", "$(git rev-parse --git-dir)", "`git rev-parse --absolute-git-dir`"):
            explicit = explicit.replace(probe, git_dir)
        hooks_path = explicit if explicit and "$(" not in explicit and "`" not in explicit else "<unresolvable>"
    else:
        hooks_path = _git(cwd, "config", "--get", "core.hooksPath", git_dir=git_dir_env) or ""
        if not hooks_path:
            return None  # the default: the repo's own .git/hooks
    resolved = _expand(_expand_vars(hooks_path, ctx.vars), top)
    real = os.path.realpath(resolved)  # a symlink out of the repo is not repo-local
    inside = [os.path.realpath(d) for d in (top, git_dir, _expand(common, top))]
    linked_now = any(resolved == made or resolved.startswith(made + os.sep) for made in ctx.links_made)
    # the user's global git config dir is never repo-local, even inside a work tree at $HOME
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    global_git = _components(os.path.realpath(os.path.join(xdg, "git")))
    in_global = _components(real)[: len(global_git)] == global_git
    if hooks_path != "<unresolvable>" and not linked_now and not in_global and any(
        _components(real)[: len(_components(d))] == _components(d) for d in inside
    ):
        return None  # a repo-local hooks dir (`.husky/_`, `.git/hooks`)
    shown = f"{head} {verb}".strip()
    return _HOOK_MANAGER_MSG.format(tool=shown, path=resolved)


def _explicit_hooks_path(env: dict[str, str]) -> str | None:
    """core.hooksPath set for this one command via GIT_CONFIG_COUNT/KEY_n/VALUE_n (git reads the
    KEY/VALUE pairs only below a valid COUNT), or None."""
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0").strip("\"'"))
    except ValueError:
        return None
    for n in range(max(0, min(count, 64))):
        if env.get(f"GIT_CONFIG_KEY_{n}", "").strip("\"'").lower() == "core.hookspath":
            return env.get(f"GIT_CONFIG_VALUE_{n}", "").strip("\"'")
    return None


def _nearest_lockfile(start: str) -> tuple[str, str] | None:
    """(dir, lockfile name) of the nearest ancestor-or-self holding any JS lockfile."""
    cur = os.path.abspath(start)
    for _ in range(_MAX_LOCKFILE_WALK):
        for name in (*_BUN_LOCKFILES, *_OTHER_LOCKFILES):
            if os.path.isfile(os.path.join(cur, name)):
                return cur, name
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def _pm_install_target(tool: str, argv: list[str], cwd: str, local: dict[str, str]) -> tuple[str, str] | None:
    """(project dir, verb) of an install-class npm/pnpm/yarn command; None when not one."""
    rest = argv[1:]
    words: list[str] = []
    project_dir = cwd
    value_flags = _PM_VALUE_FLAGS if tool == "npm" else _PNPM_YARN_VALUE_FLAGS
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _PM_GLOBAL_FLAGS or tok in _INFO_FLAGS:
            return None
        if tok == "--location" and i + 1 < len(rest) and rest[i + 1] in {"global", "user"}:
            return None
        flag, eq, attached = tok.partition("=")
        if flag in _PM_DIR_FLAGS or (flag == "-C" and tool in {"pnpm", "npm"}):
            target = attached if eq else (rest[i + 1] if i + 1 < len(rest) else "")
            project_dir = _expand(target, cwd, local)
            i += 1 if eq else 2
            continue
        if tok in value_flags:
            i += 2
            continue
        if not tok.startswith("-"):
            words.append(tok)
        i += 1
    verbs = {"npm": _NPM_INSTALL_VERBS, "pnpm": _PNPM_INSTALL_VERBS, "yarn": _YARN_INSTALL_VERBS}[tool]
    if words and words[0] in verbs:
        return project_dir, words[0]
    if tool == "yarn" and not words:
        return project_dir, "install"
    return None


def _check_pm(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    tool = _cmd(argv[0])
    target = _pm_install_target(tool, argv, cwd, ctx.vars)
    if target is None:
        return None
    project_dir, verb = target
    found = _nearest_lockfile(project_dir)
    if found is None or found[1] not in _BUN_LOCKFILES:
        return None
    return _NPM_MSG.format(tool=f"{tool} {verb}", lockdir=found[0], lockfile=found[1])


# ── driver ────────────────────────────────────────────────────────────────────


def _bind_positional(script: str, params: list[str]) -> str:
    """`bash -c 'rm -rf "$1"' _ <path>`: substitute `$0`…`$9`, `${N}`, `$@`, `$*` from the
    operands (tilde-expanded, as the outer shell passed them). Values are spliced in unquoted:
    a path with spaces may split, which only matters for display, not for the verdict."""
    values = [os.path.expanduser(p) for p in params]

    def repl(m: re.Match[str]) -> str:
        token = m.group("n") or m.group("bn") or m.group("all")
        if token in {"@", "*"}:
            return " ".join(values[1:])
        idx = int(token)
        return values[idx] if idx < len(values) else ""

    # `"$@"` expands to one word per operand, so its quotes must go with it
    script = re.sub(r'"\$[@*]"', lambda _m: " ".join(values[1:]), script)
    return re.sub(r"\$\{(?P<bn>\d)\}|\$(?P<n>\d)|\$(?P<all>[@*])", repl, script)


def _shell_c_payload(argv: list[str]) -> str | None:
    """`bash -c '...' [$0 $1…]` / `sh -lc "..."` / `eval '...'` → the inner command string."""
    head = _cmd(argv[0])
    if head == "eval":
        return " ".join(argv[1:]) or None
    if head not in _SHELLS:
        return None
    for i, tok in enumerate(argv[1:], start=1):
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            if i + 1 >= len(argv):
                return None
            return _bind_positional(argv[i + 1], argv[i + 2 :])
    return None


def _ssh_remote_command(argv: list[str]) -> str | None:
    """`ssh [opts] host 'cmd ...'` → the remote command string (run from the remote home)."""
    i = 1
    host_seen = False
    while i < len(argv):
        tok = argv[i]
        if host_seen:
            return " ".join(argv[i:]) or None
        if tok in _SSH_VALUE_FLAGS:
            i += 2
            continue
        host_seen = not tok.startswith("-")
        i += 1
    return None


def _assignment_prefix(words: list[str]) -> tuple[list[list[str]], list[str]]:
    """Split leading `NAME=value` words (a `NAME=$( … )` spans shlex's split tokens) from the rest."""
    groups: list[list[str]] = []
    i = 0
    while i < len(words) and _ASSIGNMENT.match(words[i]):
        j = i + 1
        if words[i].endswith("$") and j < len(words) and words[j] == "(":
            depth = 0
            while j < len(words):
                depth += {"(": 1, ")": -1}.get(words[j], 0)
                j += 1
                if depth == 0:
                    break
        groups.append(words[i:j])
        i = j
    return groups, words[i:]


def _record_assignments(seg: list[str], ctx: _Ctx) -> list[str]:
    """Strip leading `NAME=…` words and return the command left to inspect (``[]`` for a pure
    assignment statement such as `export NAME=$(bun pm cache)`).

    Only a pure statement records variables: in `NAME=x cmd …` the shell expands cmd's arguments
    BEFORE applying the inline env, so NAME means nothing to cmd — and cmd is still inspected.
    Command substitutions anywhere in the command are inspected separately (see
    :func:`substitution_bodies`).
    """
    words = seg[1:] if seg and seg[0] in _DECLARERS else seg
    groups, rest = _assignment_prefix(words)
    ctx.inline_env = {}
    if not groups:
        return seg
    for group in groups:
        m = _ASSIGNMENT.match(group[0])
        if not m:
            continue
        value = " ".join([m.group("value"), *group[1:]]).strip() if len(group) > 1 else m.group("value")
        if rest:
            ctx.inline_env[m.group("name")] = value  # inline env for the command in ``rest``
            continue
        resolved = _store_value(value)
        if "$(" not in resolved and "`" not in resolved:
            ctx.vars[m.group("name")] = _expand_vars(resolved.strip("\"'"), ctx.vars)
        elif m.group("name").startswith(("GIT_CONFIG_", "GIT_DIR")):
            ctx.vars[m.group("name")] = resolved  # kept raw: the hook-manager check resolves it
    return rest


def _note_store_paths(argv: list[str], ctx: _Ctx, cwd: str) -> None:
    """Remember a store path a piped segment names, for a later `| xargs rm`. A `find` rooted
    above a store lists store entries too, so there an ancestor counts."""
    if ctx.store_seen:
        return
    is_find = _cmd(argv[0]) == "find"
    candidates = (_find_paths(argv[1:]) or ["."]) if is_find else argv[1:]
    allow_ancestor = is_find
    for tok in candidates:
        if tok.startswith("-"):
            continue
        label = _protected_hit(tok, cwd, ctx.vars, allow_ancestor=allow_ancestor)
        if label:
            ctx.store_seen = label
            return


def _python_module_argv(argv: list[str]) -> list[str] | None:
    """`python3 -m uv cache clean` → `uv cache clean` (the module runs as that CLI)."""
    if not re.fullmatch(r"python[0-9.]*", _cmd(argv[0])):
        return None
    for i, tok in enumerate(argv[1:-1], start=1):
        if tok == "-m":
            return argv[i + 1 :]
        if not tok.startswith("-"):
            return None  # a script path: not a module run
    return None


def _words_at(rest: list[str], value_flags: frozenset[str]) -> list[tuple[int, str]]:
    """The non-flag words of ``rest`` with their positions, skipping the value of each flag in
    ``value_flags`` (`pnpm -C dir lefthook install` → lefthook); after `--` every word counts."""
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            out.extend((j, rest[j]) for j in range(i + 1, len(rest)))
            break
        if tok.startswith("-"):
            i += 2 if tok in value_flags else 1
            continue
        out.append((i, tok))
        i += 1
    return out


def _launched_argv(argv: list[str]) -> list[str] | None:
    """The command a package runner launches: `npx X …`, `bunx X …`, `corepack X …`,
    `npm exec [--] X …`, `pnpm dlx|exec X …`, `yarn dlx|exec X …`, `bun|npm|pnpm|yarn run X …`,
    `uvx X …`, `uv [tool] run X …`, and a hook manager run bare through yarn/pnpm/bun
    (`yarn lefthook install`) → `X …`. A `run` of a package.json script yields the script's name,
    which no rule matches."""
    head = _cmd(argv[0])
    rest = argv[1:]
    value_flags: frozenset[str] = frozenset({"-p", "--package"})
    if head in {"uvx", "uv"}:
        if head == "uv":
            words = _words_at(rest, _UV_VALUE_FLAGS | _UV_RUN_VALUE_FLAGS)
            names = [w for _, w in words]
            if names[:2] == ["tool", "run"]:
                rest = rest[words[1][0] + 1 :]
            elif names[:1] == ["run"]:
                rest = rest[words[0][0] + 1 :]
            else:
                return None
        value_flags = _UV_RUN_VALUE_FLAGS
    elif head in {"npm", "pnpm", "yarn", "bun"}:
        words = _words_at(rest, _PM_VALUE_FLAGS if head == "npm" else _PNPM_YARN_VALUE_FLAGS)
        runner_verbs = {"x", "run"} if head == "bun" else {"exec", "x", "dlx", "run"}
        if not words:
            return None
        at, first = words[0]
        if head != "npm" and first in _HOOK_MANAGER_INSTALLS:
            rest = rest[at:]  # `yarn lefthook install` runs the bin
        elif first in runner_verbs:
            rest = rest[at + 1 :]
        else:
            return None
    elif head not in {"npx", "bunx", "corepack", "pnpx"}:
        return None
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        flag, eq, attached = rest[i].partition("=")
        if rest[i] == "--":
            i += 1
            break
        if flag in {"-c", "--call"} and head not in {"uvx", "uv"}:  # `npx -c '…'` is `sh -c '…'`
            return ["sh", "-c", attached if eq else (rest[i + 1] if i + 1 < len(rest) else "")]
        i += 2 if (rest[i] in value_flags and not eq) else 1
    launched = rest[i:]
    if not launched:
        return None
    spec = launched[0].split("==", 1)[0]  # uvx `pkg==1.2`
    at = spec.rfind("@")
    if at > 0:  # `npm@10`, `@scope/pkg@1.2` → the package's own command name
        spec = spec[:at]
    return [spec, *launched[1:]]


def _check_argv(argv: list[str], ctx: _Ctx, cwd: str, via_xargs: bool, depth: int = 0) -> str | None:
    module_argv = _python_module_argv(argv)
    if module_argv:
        argv = module_argv
    for _ in range(_MAX_WRAPPERS):  # `npx npm install`, `corepack npm ci`, `npx husky`
        launched = _launched_argv(argv)
        if not launched:
            break
        argv = launched
    script = _shell_c_payload(argv)
    if script is not None:  # `npx -c '…'` became `sh -c '…'`
        return inspect_command(script, cwd, depth + 1, ctx.vars) if depth < _MAX_DEPTH else None
    head = _cmd(argv[0])
    if via_xargs and ctx.store_seen and head in _REMOVERS:
        return _store_msg(f"`… | xargs {head}` over paths in {ctx.store_seen}")
    if head in {"bun", "bunx"}:
        return _check_bun(argv)
    if head == "uv":
        return _check_uv(argv)
    if head in _DELETERS:
        return _check_delete(argv, ctx, cwd)
    if head in _MOVERS:
        return _check_mv(argv, ctx, cwd)
    if head == "find":
        return _check_find(argv, ctx, cwd, depth)
    if head == "ln":
        return _check_ln(argv, ctx, cwd)
    if head == "cp":
        return _check_cp(argv, ctx, cwd)
    if head in {"npm", "pnpm", "yarn"}:
        return _check_pm(argv, ctx, cwd)
    if head in {"colima", "limactl", "podman", "brew", "nix", "nix-env", "nix-shell", "open"}:
        return _check_vm_runtime(argv)
    if head in _HOOK_MANAGER_INSTALLS:
        return _check_hook_manager(argv, ctx, cwd)
    return None


def inspect_command(command: str, cwd: str, _depth: int = 0, _vars: dict[str, str] | None = None) -> str | None:
    """The block message for ``command`` run from ``cwd``, or None to allow."""
    if _depth == 0:
        _reset_git_budget()
    command = strip_heredoc_bodies(command)
    ctx = _Ctx(cwd, _vars)
    if _depth < _MAX_DEPTH:
        for body in substitution_bodies(command):
            msg = inspect_command(body, cwd, _depth + 1, ctx.vars)
            if msg:
                return msg
    for seg, piped in _segments(_tokenize(command)):
        if not piped:
            ctx.store_seen = None  # `| xargs rm` only inherits paths from its own pipeline
        here_string = next((seg[k + 1] for k, t in enumerate(seg[:-1]) if t == "<<<"), None)
        plain = _strip_redirects(seg)
        argv = _record_assignments(plain, ctx)
        if not argv:
            continue
        if argv and "$" in argv[0]:
            word = _expand_vars(argv[0], {**ctx.vars, **ctx.inline_env})
            if word != argv[0]:
                argv = [*word.split(), *argv[1:]]  # unquoted expansion word-splits
        argv, seg_cwd, nested, via_xargs = _unwrap(argv, ctx)
        if here_string is not None and argv and _stdin_script_dir(" ".join(shlex.quote(a) for a in argv)) is not None:
            nested.append(here_string)  # `bash <<< '…'`: the string is the script
        for inner in nested:
            if _depth < _MAX_DEPTH:
                msg = inspect_command(inner, seg_cwd, _depth + 1, ctx.vars)
                if msg:
                    return msg
        if not argv:
            continue
        head = _cmd(argv[0])
        if head in {"cd", "pushd", "popd"}:
            ctx.chdir(head, argv[1:])
            continue
        inner_cmd, inner_cwd = _shell_c_payload(argv), seg_cwd
        if inner_cmd is None and head == "ssh":
            inner_cmd, inner_cwd = _ssh_remote_command(argv), _home()
        if inner_cmd is not None:
            if _depth < _MAX_DEPTH:
                msg = inspect_command(inner_cmd, inner_cwd, _depth + 1, ctx.vars)
                if msg:
                    return msg
            continue
        msg = _check_argv(argv, ctx, seg_cwd, via_xargs, _depth)
        if msg:
            return msg
        if head == "mkdir":
            ctx.dirs_made.update(_expand(d, seg_cwd, ctx.vars) for d in _positional(argv[1:]))
        _note_store_paths(argv, ctx, seg_cwd)
    return None


# Cheap pre-filter: a command naming none of these cannot hit any rule.
_HINT = re.compile(
    r"\b(?:bun|bunx|uv|rm|rmdir|unlink|trash|srm|shred|gio|mv|find|ln|cp|npm|pnpm|yarn"
    r"|colima|limactl|podman|brew|nix|nix-env|nix-shell|open|lefthook|husky|simple-git-hooks)\b",
    re.IGNORECASE,
)


def main() -> int:
    try:
        event = json.load(sys.stdin)
        args = event.get("args") if isinstance(event, dict) else None
        _CURRENT_EVENT["event"] = event
        args = args if isinstance(args, dict) else {}
        command = args.get("command") or args.get("cmd") or (event.get("command") if isinstance(event, dict) else "") or ""
        cwd = str((event.get("cwd") if isinstance(event, dict) else None) or args.get("cwd") or os.getcwd())
    except (ValueError, AttributeError, TypeError) as exc:
        warn(f"could not read the event: {exc} — allowing")
        emit("allow")
        return 0
    if not isinstance(command, str):
        command = str(command)

    if not _HINT.search(command):
        emit("allow")
        return 0
    try:
        message = inspect_command(command, cwd)
    except Exception as exc:  # noqa: BLE001 — a parser bug must not wedge every shell call
        warn(f"inspection failed ({exc!r}) — allowing")
        emit("allow")
        return 0
    if message:
        emit("block", message)
        return BLOCK_EXIT_CODE
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
