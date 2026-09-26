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
directory is a bun project); it never runs the command.

Blocks five shapes, each with the command to run instead:

1. ``bun pm cache rm`` (the whole global store);
2. deleting or moving ``~/.bun/install/cache`` (or an ancestor such as ``~/.bun``, or anything
   inside it) with ``rm``/``rmdir``/``unlink``/``trash``/``mv``/``find -delete``;
3. ``uv cache clean``, ``uv cache prune --all``/``--ci``, and deleting ``~/.cache/uv``;
4. creating a symlink named ``node_modules`` (``ln -s``, ``ln -sfn``, ``ln -s <src> .``, ...);
5. ``npm install``/``npm ci``/``pnpm install``/``yarn`` (and their add/update verbs) where the nearest
   lockfile is ``bun.lock``/``bun.lockb``. A directory whose nearest lockfile is only
   ``package-lock.json`` (an npm-only subproject) stays allowed, as do global installs.

Past bugs this exists for (2026-09): agents symlinked a worktree's node_modules to the main
checkout; bun 1.4.2's ``bun install`` inside such a worktree wrote THROUGH the link and corrupted
the main checkout (hypercalendarbot, 2026-09-09). Agents then deleted the links and ran full npm
installs (npm never clonefiles, so every install is a real copy). A disk-cleanup agent ran
``bun pm cache rm`` on 2026-09-15 and destroyed the global store under every worktree.
No self-service bypass: a genuine exception is the human's call.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "package-store-guard"

_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_LEADING_SHELL_NOISE = frozenset({"(", ")", "{", "}", "!", "then", "do", "else", "elif", "time"})
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer", "command",
    "sudo", "doas", "exec", "caffeinate",
})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir"}),
    "doas": frozenset({"-u", "-C"}),
}
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_GLOB_CHARS = re.compile(r"[*?\[]")

_BUN_LOCKFILES = ("bun.lock", "bun.lockb")
_OTHER_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")

_NPM_INSTALL_VERBS = frozenset({
    "install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall",
    "add", "ci", "clean-install", "ic", "install-clean", "isntall-clean", "install-test", "it",
    "install-ci-test", "cit", "update", "up", "upgrade", "udpate",
})
_PNPM_INSTALL_VERBS = frozenset({"install", "i", "add", "update", "up", "upgrade"})
_YARN_INSTALL_VERBS = frozenset({"install", "add", "upgrade", "up"})
_NPM_VALUE_FLAGS = frozenset({
    "--prefix", "-C", "--dir", "--cwd", "-w", "--workspace", "--filter", "-F", "--registry",
    "--cache", "--userconfig", "--loglevel", "--reporter", "--mutex", "--modules-folder",
})
_INFO_FLAGS = frozenset({"-v", "--version", "-h", "--help"})
_BUN_CACHE_VERBS = frozenset({"rm", "clean", "clear", "remove", "delete"})
_SSH_VALUE_FLAGS = frozenset({
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p",
    "-Q", "-R", "-S", "-W", "-w", "-B",
})
# Store wipes spelled literally inside a command shlex cannot parse (unbalanced quotes).
_UNPARSEABLE_STORE_WIPE = re.compile(
    r"\bbun\s+pm\s+cache\s+(?:rm|clean|clear|remove|delete)\b|\buv\s+cache\s+clean\b"
)
_UV_VALUE_FLAGS = frozenset({
    "--cache-dir", "--directory", "--project", "--config-file", "--color", "--python", "-p",
    "--allow-insecure-host", "--python-preference",
})
_DELETERS = frozenset({"rm", "rmdir", "unlink", "trash", "srm", "shred", "gio"})

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
    "creates symlinks into the shared store — a few MB, seconds. `rig worktree create` already "
    "does this for bun repos."
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


def _tokenize(command: str) -> list[str] | None:
    """punctuation_chars so `true;rm` / `a&&b` split; newlines are command separators."""
    try:
        out: list[str] = []
        for i, line in enumerate(command.splitlines()):
            if not line.strip():
                continue
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
            if i and out and toks:
                out.append(";")
            out.extend(toks)
        return out
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SHELL_SEPS:
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def _strip_leading(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and (argv[i] in _LEADING_SHELL_NOISE or _INLINE_ENV.match(argv[i])):
        i += 1
    return argv[i:]


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    i = 0
    while i < len(argv):
        tok = argv[i]
        if _INLINE_ENV.match(tok):
            i += 1
            continue
        if tok in value_flags:
            i += 2
            continue
        if any(tok.startswith(f"{flag}=") for flag in value_flags):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    rest = argv[i:]
    if wrapper == "timeout" and rest and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", rest[0]):
        rest = rest[1:]
    return rest


def _strip_wrappers(argv: list[str]) -> list[str]:
    guard = 0
    argv = _strip_leading(argv)
    while argv and _basename(argv[0]) in _WRAPPERS and guard < 16:
        guard += 1
        argv = _strip_leading(_skip_wrapper_args(_basename(argv[0]), argv[1:]))
    return argv


# ── paths ─────────────────────────────────────────────────────────────────────


def _expand(path: str, cwd: str) -> str:
    """Expand ~ / $VARS and anchor a relative path at the effective cwd (no symlink resolution)."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def _components(path: str) -> list[str]:
    norm = os.path.normpath(path).replace("\\", "/")
    return [c for c in norm.split("/") if c not in ("", ".")]


def _home() -> str:
    return os.path.expanduser("~")


def bun_store_roots() -> list[tuple[str, str]]:
    """(label, absolute path) of every bun global-store location this machine may use."""
    home = _home()
    roots = [os.path.join(home, ".bun", "install", "cache")]
    bun_install = os.environ.get("BUN_INSTALL", "").strip()
    if bun_install:
        roots.append(os.path.join(os.path.expanduser(bun_install), "install", "cache"))
    explicit = os.environ.get("BUN_INSTALL_CACHE_DIR", "").strip()
    if explicit:
        roots.append(os.path.expanduser(explicit))
    return [("the bun global store", os.path.normpath(r)) for r in dict.fromkeys(roots)]


def uv_cache_roots() -> list[tuple[str, str]]:
    """(label, absolute path) of every uv cache location this machine may use."""
    home = _home()
    roots = [os.path.join(home, ".cache", "uv")]
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        roots.append(os.path.join(os.path.expanduser(xdg), "uv"))
    explicit = os.environ.get("UV_CACHE_DIR", "").strip()
    if explicit:
        roots.append(os.path.expanduser(explicit))
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        roots.append(os.path.join(local_app, "uv", "cache"))
    return [("the uv cache", os.path.normpath(r)) for r in dict.fromkeys(roots)]


def _protected_roots() -> list[tuple[str, str]]:
    return bun_store_roots() + uv_cache_roots()


def _target_hits_root(target: str, root: str, *, allow_ancestor: bool) -> bool:
    """True when ``target`` (may hold glob chars) equals, contains, or lies inside ``root``.

    Compared component-wise with fnmatch so `~/.bun/install/cache/*` and `~/.cache/*` match
    while `~/.cache/pip*` does not reach `~/.cache/uv`.
    """
    t = _components(target)
    r = _components(root)
    if not t:
        return allow_ancestor  # `/` itself contains everything
    common = min(len(t), len(r))
    for i in range(common):
        pattern = t[i]
        if _GLOB_CHARS.search(pattern):
            if not fnmatch.fnmatchcase(r[i], pattern):
                return False
        elif pattern != r[i]:
            return False
    if len(t) >= len(r):
        return True  # equal to the root, or inside it
    return allow_ancestor  # an ancestor of the root: deleting it deletes the root


def _protected_hit(raw_target: str, cwd: str, *, allow_ancestor: bool = True) -> str | None:
    target = _expand(raw_target, cwd)
    for label, root in _protected_roots():
        if _target_hits_root(target, root, allow_ancestor=allow_ancestor):
            return label
    return None


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


def _uv_words(argv: list[str]) -> list[str]:
    words: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _UV_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        words.append(tok)
        i += 1
    return words


def _check_uv(argv: list[str]) -> str | None:
    words = _uv_words(argv)
    if len(words) >= 2 and words[0] == "cache":
        if words[1] == "clean":
            return _STORE_MSG.format(what="`uv cache clean`")
        if words[1] == "prune" and any(t in {"--all", "--ci"} for t in argv):
            return _STORE_MSG.format(what="`uv cache prune --all/--ci`")
    return None


def _substitutes_store_path(argv: list[str]) -> bool:
    """`rm -rf $(bun pm cache)` / `rm -rf "$(uv cache dir)"`: the target is the store itself."""
    text = " ".join(argv)
    return bool(re.search(r"\bbun\s+pm\s+cache\b|\buv\s+cache\s+dir\b", text))


def _check_delete(argv: list[str], cwd: str) -> str | None:
    head = _basename(argv[0])
    rest = argv[1:]
    if head == "gio":
        if not rest or rest[0] not in {"trash", "remove"}:
            return None
        rest = rest[1:]
    if _substitutes_store_path(rest):
        return _STORE_MSG.format(what=f"`{head}` of the store path")
    for target in _positional(rest):
        label = _protected_hit(target, cwd)
        if label:
            return _STORE_MSG.format(what=f"`{head} {target}` removes {label}")
    return None


def _check_mv(argv: list[str], cwd: str) -> str | None:
    rest = argv[1:]
    operands = _positional(rest)
    has_target_dir = any(t in {"-t"} or t.startswith("--target-directory") for t in rest)
    sources = operands if has_target_dir else operands[:-1]
    if _substitutes_store_path(rest):
        return _STORE_MSG.format(what="`mv` of the store path")
    for src in sources:
        label = _protected_hit(src, cwd)
        if label:
            return _STORE_MSG.format(what=f"`mv {src}` moves {label} away")
    return None


def _check_find(argv: list[str], cwd: str) -> str | None:
    rest = argv[1:]
    if not any(t in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for t in rest):
        return None
    for tok in rest:
        if tok.startswith("-") or tok in {"(", "!", ")"}:
            break
        # find prunes only what matches, so an ancestor (`find ~ -name x -delete`) is fine;
        # a walk rooted at or inside the store deletes store entries.
        label = _protected_hit(tok, cwd, allow_ancestor=False)
        if label:
            return _STORE_MSG.format(what=f"`find {tok} ... -delete/-exec` inside {label}")
    return None


def _ln_link_names(argv: list[str]) -> list[str] | None:
    """Link paths an `ln -s` would create; None if this `ln` does not make a symlink."""
    symbolic = False
    no_target_dir = False
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
            elif tok.startswith("--target-directory="):
                target_dir = tok.split("=", 1)[1]
            elif tok == "--target-directory" and i + 1 < len(argv):
                target_dir = argv[i + 1]
                i += 1
        elif not end_of_opts and tok.startswith("-") and tok != "-":
            flags = tok[1:]
            if "s" in flags:
                symbolic = True
            if "T" in flags:
                no_target_dir = True
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
    if dest.endswith("/"):
        return into_dir  # `ln -s SRC DIR/` always links INSIDE DIR
    # `ln -s SRC DEST` makes DEST, or DEST/basename(SRC) when DEST is an existing directory.
    return [dest, *into_dir]


def _check_ln(argv: list[str]) -> str | None:
    names = _ln_link_names(argv)
    if not names:
        return None
    for name in names:
        if _basename(name.rstrip("/")) == "node_modules":
            return _SYMLINK_MSG
    return None


def _nearest_lockfile(start: str) -> tuple[str, str] | None:
    """(dir, lockfile name) of the nearest ancestor-or-self holding any JS lockfile."""
    cur = os.path.abspath(start)
    for _ in range(64):
        for name in _BUN_LOCKFILES:
            if os.path.isfile(os.path.join(cur, name)):
                return cur, name
        for name in _OTHER_LOCKFILES:
            if os.path.isfile(os.path.join(cur, name)):
                return cur, name
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def _pm_install_dir(tool: str, argv: list[str], cwd: str) -> str | None:
    """The project dir an install-class npm/pnpm/yarn command targets; None when not an install."""
    rest = argv[1:]
    words: list[str] = []
    project_dir = cwd
    value_flags = _NPM_VALUE_FLAGS if tool == "npm" else _NPM_VALUE_FLAGS - {"-w"}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in {"-g", "--global"} or tok in {"--location=global", "--location=user"}:
            return None
        if tok in _INFO_FLAGS:
            return None
        if tok == "--location" and i + 1 < len(rest) and rest[i + 1] in {"global", "user"}:
            return None
        if tok in {"--prefix", "--dir", "--cwd"} or (tok == "-C" and tool == "pnpm"):
            if i + 1 < len(rest):
                project_dir = _expand(rest[i + 1], cwd)
            i += 2
            continue
        for flag in ("--prefix=", "--dir=", "--cwd="):
            if tok.startswith(flag):
                project_dir = _expand(tok[len(flag):], cwd)
        if tok in value_flags:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        words.append(tok)
        i += 1
    if tool == "npm":
        return project_dir if words and words[0] in _NPM_INSTALL_VERBS else None
    if tool == "pnpm":
        return project_dir if words and words[0] in _PNPM_INSTALL_VERBS else None
    if tool == "yarn":
        return project_dir if not words or words[0] in _YARN_INSTALL_VERBS else None
    return None


def _check_pm(argv: list[str], cwd: str) -> str | None:
    tool = _basename(argv[0])
    project_dir = _pm_install_dir(tool, argv, cwd)
    if project_dir is None:
        return None
    found = _nearest_lockfile(project_dir)
    if found is None:
        return None
    lockdir, lockfile = found
    if lockfile not in _BUN_LOCKFILES:
        return None
    verb = next((t for t in argv[1:] if not t.startswith("-")), "")
    shown = f"{tool} {verb}".strip()
    return _NPM_MSG.format(tool=shown, lockdir=lockdir, lockfile=lockfile)


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
        if not host_seen:
            if tok in _SSH_VALUE_FLAGS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            host_seen = True
            i += 1
            continue
        return " ".join(argv[i:]) or None
    return None


def inspect(command: str, cwd: str, _depth: int = 0) -> str | None:
    """The block message for ``command`` run from ``cwd``, or None to allow."""
    tokens = _tokenize(command)
    if tokens is None:
        hit = _UNPARSEABLE_STORE_WIPE.search(command)
        return _STORE_MSG.format(what=f"`{hit.group(0)}`") if hit else None
    eff_cwd = cwd
    for seg in _segments(tokens):
        argv = _strip_wrappers(seg)
        if not argv:
            continue
        head = _basename(argv[0])
        if head in {"cd", "pushd"}:
            dests = _positional(argv[1:])
            if dests and dests[0] != "-":
                eff_cwd = _expand(dests[0], eff_cwd)
            elif not dests:
                eff_cwd = _home()
            continue
        inner = _shell_c_payload(argv)
        inner_cwd = eff_cwd
        if inner is None and head == "ssh":
            inner, inner_cwd = _ssh_remote_command(argv), _home()
        if inner is not None and _depth < 4:
            inner_msg = inspect(inner, inner_cwd, _depth + 1)
            if inner_msg:
                return inner_msg
            continue
        msg: str | None = None
        if head in {"bun", "bunx"}:
            msg = _check_bun(argv)
        elif head == "uv":
            msg = _check_uv(argv)
        elif head in _DELETERS:
            msg = _check_delete(argv, eff_cwd)
        elif head == "mv":
            msg = _check_mv(argv, eff_cwd)
        elif head == "find":
            msg = _check_find(argv, eff_cwd)
        elif head == "ln":
            msg = _check_ln(argv)
        elif head in {"npm", "pnpm", "yarn"}:
            msg = _check_pm(argv, eff_cwd)
        if msg:
            return msg
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
