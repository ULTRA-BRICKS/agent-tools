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

Blocks five shapes, each with the command to run instead:

1. ``bun pm cache rm`` (the whole global store);
2. deleting or moving ``~/.bun/install/cache`` (or an ancestor such as ``~/.bun``, or anything
   inside it) with ``rm``/``rmdir``/``unlink``/``trash``/``mv``/``find -delete``/``… | xargs rm``;
3. ``uv cache clean``, ``uv cache prune --all``/``--ci``, and deleting ``~/.cache/uv``;
4. creating a symlink named ``node_modules`` (``ln -s``, ``ln -sfn``, ``ln -s <src> .``, ...);
5. npm/pnpm/yarn install-class verbs (install/ci/add/update/uninstall/remove/prune, bare ``yarn``)
   where the nearest lockfile is ``bun.lock``/``bun.lockb``. A directory whose nearest lockfile is
   only ``package-lock.json`` (an npm-only subproject) stays allowed, as do global installs.

Parsing is best-effort but deliberately wide: ``&&``/``;``/``|`` segments, ``cd``/``pushd``,
``NAME=value`` / ``NAME=$(bun pm cache)`` assignments and ``${NAME:-default}``, wrappers
(``sudo``, ``env -S``/``env -C``, ``timeout``, ``xargs``, ``busybox``), ``bash -c``/``eval``/
``ssh host '…'`` payloads, redirections and here-doc bodies (data, not commands). A command shlex
cannot parse is re-read leniently rather than allowed. Known gaps: other command substitutions,
``find ~ -delete`` (a walk rooted above the store, with or without filters), and code in other
languages (``python -c 'shutil.rmtree(…)'``).

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
import json
import os
import re
import shlex
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "package-store-guard"

# ── shell grammar ─────────────────────────────────────────────────────────────
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_PIPES = frozenset({"|", "|&"})
_PUNCT = frozenset("();<>|&")
_ESCAPED_SEMI = "__package_store_guard_escaped_semicolon__"
# Longest first, so `&&` wins over `&` and `;;&` over `;;`.
_OPERATORS = ("<<<", ";;&", "&>>", "&&", "||", ";;", ";&", "|&", "&>", ">>", ">&", "<&", ">|", "<>", "<<",
              "(", ")", ";", "|", "&", "<", ">")
_LEADING_SHELL_NOISE = frozenset({"(", ")", "{", "}", "!", "then", "do", "else", "elif"})
_ASSIGNMENT = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$", re.DOTALL)
_DECLARERS = frozenset({"export", "local", "declare", "typeset", "readonly"})
_REDIRECT_OPS = frozenset({">", ">>", "<", ">&", "&>", "&>>", "<&", ">|", "<>", "<<", "<<<"})
_HEREDOC_RE = re.compile(r"<<(?!<)(?P<dash>-)?\s*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)")
_VAR_RE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<default>[^}]*))?\}"
    r"|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)
_GLOB_CHARS = re.compile(r"[*?\[]")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer", "command",
    "sudo", "doas", "exec", "caffeinate", "busybox", "xargs",
})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "env": frozenset({"-u", "--unset"}),
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
_MAX_DEPTH = 4

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
_FIND_EXEC_OPS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_VALUELESS_OPTS = frozenset({"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"})
_FIND_EXPR_START = frozenset({"(", "!", ")", "\\(", "\\!"})
_STORE_SUBST_RE = re.compile(r"\b(?P<bun>bun\s+pm\s+cache)\b|\b(?P<uv>uv\s+cache\s+dir)\b")
_CASE_INSENSITIVE_FS = sys.platform in {"darwin", "win32"}

_STORE_MSG = (
    "[package-store-guard] BLOCKED: this would wipe a shared package store ({what}). "
    "The bun global store (~/.bun/install/cache, including links/) is what every worktree's "
    "node_modules symlinks into, and uv venvs are cloned from ~/.cache/uv — wiping either breaks "
    "or re-bloats every install on the machine. Never clean package-manager caches or stores to "
    "free disk. To reclaim space instead: remove stale worktrees (`rig worktree gc --yes`, "
    "`rig worktree remove <name>`), or delete ONE worktree's node_modules and re-run "
    "`bun install --frozen-lockfile` there. No self-service bypass — ask the human."
)
_SYMLINK_MSG = (
    "[package-store-guard] BLOCKED: do not symlink node_modules to another checkout. `bun install` "
    "in a worktree whose node_modules is a symlink writes THROUGH it and corrupts the other "
    "checkout (bun 1.4.2, hypercalendarbot 2026-09-09). Run `bun install --frozen-lockfile` in "
    "this worktree instead: with rig's ~/.bunfig.toml (isolated linker + globalStore) it only "
    "creates symlinks into the shared store — a few MB, seconds — unless the repo's own "
    "bunfig.toml asks for a local layout. `rig worktree create` already does this for bun repos."
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


def strip_heredoc_bodies(command: str) -> str:
    """Drop here-document bodies: `cat > f <<'EOF' ... EOF` text is data, not commands."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            delim, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend((m.group("delim"), bool(m.group("dash"))) for m in _HEREDOC_RE.finditer(line))
    return "\n".join(out)


def _newlines_to_separators(text: str) -> str:
    """Unquoted newlines separate commands; quoted ones stay inside their token."""
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
        elif ch == "\n":
            out.append("\n ; ")  # after the newline, so a `#` comment cannot swallow it
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


def _tokenize(command: str) -> list[str]:
    """Shell-ish tokens. A command shlex rejects (unbalanced quotes) is re-read leniently —
    the real shell would reject it too, so over-reading it costs nothing."""
    try:
        lex = shlex.shlex(_newlines_to_separators(command), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return [
            piece.replace(_ESCAPED_SEMI, "\\;") for tok in lex for piece in _split_operators(tok)
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
    """Per-command state: effective cwd, shell variables, store paths the current pipeline named."""

    def __init__(self, cwd: str, variables: dict[str, str] | None = None) -> None:
        self.cwd = cwd
        self.vars: dict[str, str] = dict(variables or {})
        self.store_seen: str | None = None  # label of a store path an earlier piped segment named


def _unwrap(argv: list[str], ctx: _Ctx) -> tuple[list[str], str, list[str], bool]:
    """Peel wrappers. Returns (argv, cwd for it, nested command strings, came through xargs)."""
    cwd = ctx.cwd
    nested: list[str] = []
    via_xargs = False
    argv = _strip_leading(argv)
    for _ in range(16):
        if not argv or _basename(argv[0]) not in _WRAPPERS:
            break
        wrapper = _basename(argv[0])
        via_xargs = via_xargs or wrapper == "xargs"
        value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
        chdir_flags = _WRAPPER_CHDIR_FLAGS.get(wrapper, frozenset())
        rest = argv[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if _ASSIGNMENT.match(tok):
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
        rest = rest[i:] if rest else rest
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
            return _expand_vars(m.group("default") or "", local, _depth + 1) if _depth < 4 else ""
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


def _protected_hit(raw_target: str, ctx_cwd: str, local: dict[str, str], *, allow_ancestor: bool = True) -> str | None:
    target = _components(_expand(raw_target, ctx_cwd, local))
    for label, root in _protected_roots():
        if _target_hits_root(target, root, allow_ancestor=allow_ancestor):
            return label
    return None


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
    words = [t for t in argv[1:] if not t.startswith("-")]
    if len(words) >= 3 and words[0] == "pm" and words[1] == "cache" and words[2] in _BUN_CACHE_VERBS:
        return _STORE_MSG.format(what="`bun pm cache " + words[2] + "`")
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
            return _STORE_MSG.format(what="`uv cache clean`")
        if words[1] == "prune" and _UV_PRUNE_WIPE_FLAGS.intersection(argv):
            return _STORE_MSG.format(what="`uv cache prune --all/--ci`")
    return None


def _check_remove(head: str, operands: list[str], rest: list[str], ctx: _Ctx, cwd: str) -> str | None:
    if _store_substitution(" ".join(rest)):
        return _STORE_MSG.format(what=f"`{head}` of the store path")
    for target in operands:
        label = _protected_hit(target, cwd, ctx.vars)
        if label:
            return _STORE_MSG.format(what=f"`{head} {target}` removes {label}")
    return None


def _check_delete(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    head = _basename(argv[0])
    rest = argv[1:]
    if head == "gio":
        if not rest or rest[0] not in {"trash", "remove"}:
            return None
        rest = rest[1:]
    return _check_remove(head, _positional(rest), rest, ctx, cwd)


def _check_mv(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    rest = argv[1:]
    operands = _positional(rest)
    has_target_dir = any(t == "-t" or t.startswith("--target-directory") for t in rest)
    return _check_remove("mv", operands if has_target_dir else operands[:-1], rest, ctx, cwd)


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
        if tok in _FIND_EXEC_OPS and i + 1 < len(rest) and _basename(rest[i + 1]) in _DELETERS | _MOVERS:
            return True
    return False


def _check_find(argv: list[str], ctx: _Ctx, cwd: str) -> str | None:
    rest = argv[1:]
    if not _find_deletes(rest):
        return None
    for path in _find_paths(rest):
        # find removes only what matches, so a walk rooted ABOVE the store is not flagged;
        # one rooted at or inside the store deletes store entries.
        label = _protected_hit(path, cwd, ctx.vars, allow_ancestor=False)
        if label:
            return _STORE_MSG.format(what=f"`find {path} ... -delete/-exec rm` inside {label}")
    return None


def _ln_link_names(argv: list[str], cwd: str) -> list[str] | None:
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
    dest_abs = _expand(dest, cwd)
    if os.path.isdir(dest_abs) and not (no_deref and os.path.islink(dest_abs)):
        return into_dir
    return [dest]


def _check_ln(argv: list[str], cwd: str) -> str | None:
    for name in _ln_link_names(argv, cwd) or []:
        if _basename(name.rstrip("/")) == "node_modules":
            return _SYMLINK_MSG
    return None


def _nearest_lockfile(start: str) -> tuple[str, str] | None:
    """(dir, lockfile name) of the nearest ancestor-or-self holding any JS lockfile."""
    cur = os.path.abspath(start)
    for _ in range(64):
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
        if flag in _PM_DIR_FLAGS or (flag == "-C" and tool == "pnpm"):
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
    tool = _basename(argv[0])
    target = _pm_install_target(tool, argv, cwd, ctx.vars)
    if target is None:
        return None
    project_dir, verb = target
    found = _nearest_lockfile(project_dir)
    if found is None or found[1] not in _BUN_LOCKFILES:
        return None
    return _NPM_MSG.format(tool=f"{tool} {verb}", lockdir=found[0], lockfile=found[1])


# ── driver ────────────────────────────────────────────────────────────────────


def _shell_c_payload(argv: list[str]) -> str | None:
    """`bash -c '...'` / `sh -lc "..."` / `eval '...'` → the inner command string."""
    head = _basename(argv[0])
    if head == "eval":
        return " ".join(argv[1:]) or None
    if head not in _SHELLS:
        return None
    for i, tok in enumerate(argv[1:], start=1):
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            return argv[i + 1] if i + 1 < len(argv) else None
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


def _record_assignments(seg: list[str], ctx: _Ctx) -> bool:
    """`NAME=value` / `export NAME=$(bun pm cache)` segments update ``ctx.vars``. True if consumed."""
    words = seg[1:] if seg and seg[0] in _DECLARERS else seg
    first = _ASSIGNMENT.match(words[0]) if words else None
    if first is None:
        return False
    if not all(_ASSIGNMENT.match(w) for w in words) and not _store_substitution(" ".join(words)):
        return False  # `NAME=x cmd …` is an inline env for cmd, not a shell assignment
    text = " ".join(words)
    store = _store_substitution(text)
    if store is not None:
        ctx.vars[first.group("name")] = store
        return True
    for word in words:
        m = _ASSIGNMENT.match(word)
        if m:
            value = m.group("value")
            if "$(" not in value and "`" not in value:
                ctx.vars[m.group("name")] = _expand_vars(value, ctx.vars)
    return True


def _note_store_paths(argv: list[str], ctx: _Ctx, cwd: str) -> None:
    """Remember a store path an earlier segment names, for a later `| xargs rm`."""
    if ctx.store_seen:
        return
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        label = _protected_hit(tok, cwd, ctx.vars, allow_ancestor=False)
        if label:
            ctx.store_seen = label
            return


def _check_argv(argv: list[str], ctx: _Ctx, cwd: str, via_xargs: bool) -> str | None:
    head = _basename(argv[0])
    if via_xargs and ctx.store_seen and head in _DELETERS | _MOVERS:
        return _STORE_MSG.format(what=f"`… | xargs {head}` over paths in {ctx.store_seen}")
    if head in {"bun", "bunx"}:
        return _check_bun(argv)
    if head == "uv":
        return _check_uv(argv)
    if head in _DELETERS:
        return _check_delete(argv, ctx, cwd)
    if head in _MOVERS:
        return _check_mv(argv, ctx, cwd)
    if head == "find":
        return _check_find(argv, ctx, cwd)
    if head == "ln":
        return _check_ln(argv, cwd)
    if head in {"npm", "pnpm", "yarn"}:
        return _check_pm(argv, ctx, cwd)
    return None


def inspect(command: str, cwd: str, _depth: int = 0, _vars: dict[str, str] | None = None) -> str | None:
    """The block message for ``command`` run from ``cwd``, or None to allow."""
    ctx = _Ctx(cwd, _vars)
    for seg, piped in _segments(_tokenize(strip_heredoc_bodies(command))):
        if not piped:
            ctx.store_seen = None  # `| xargs rm` only inherits paths from its own pipeline
        seg = _strip_redirects(seg)
        if _record_assignments(seg, ctx):
            continue
        argv, seg_cwd, nested, via_xargs = _unwrap(seg, ctx)
        for inner in nested:
            if _depth < _MAX_DEPTH:
                msg = inspect(inner, seg_cwd, _depth + 1, ctx.vars)
                if msg:
                    return msg
        if not argv:
            continue
        head = _basename(argv[0])
        if head in {"cd", "pushd"}:
            dests = _positional(argv[1:])
            if dests and dests[0] != "-":
                ctx.cwd = _expand(dests[0], ctx.cwd, ctx.vars)
            elif not dests:
                ctx.cwd = _home()
            continue
        inner_cmd, inner_cwd = _shell_c_payload(argv), seg_cwd
        if inner_cmd is None and head == "ssh":
            inner_cmd, inner_cwd = _ssh_remote_command(argv), _home()
        if inner_cmd is not None:
            if _depth < _MAX_DEPTH:
                msg = inspect(inner_cmd, inner_cwd, _depth + 1, ctx.vars)
                if msg:
                    return msg
            continue
        msg = _check_argv(argv, ctx, seg_cwd, via_xargs)
        if msg:
            return msg
        _note_store_paths(argv, ctx, seg_cwd)
    return None


# Cheap pre-filter: a command naming none of these cannot hit any rule.
_HINT = re.compile(
    r"\b(?:bun|bunx|uv|rm|rmdir|unlink|trash|srm|shred|gio|mv|find|ln|npm|pnpm|yarn)\b"
)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing")
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    cwd = str(event.get("cwd") or args.get("cwd") or os.getcwd())

    if not _HINT.search(command):
        emit("allow")
        return 0
    try:
        message = inspect(command, cwd)
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
