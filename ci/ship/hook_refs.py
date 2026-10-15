#!/usr/bin/env python3
"""Find installed agent-hook wiring that executes code from inside a worktree.

Why: `rig apply` run from a feature worktree (dogfooding a hook before it merges) writes that
worktree's absolute path into the machine-wide hook wiring — every harness's agent-hook
descriptors (`cmd`), the bridge commands in the harness settings, the omp/pi extension links,
and the `AGENT_TOOLS_ROOT` pointer the `gh ship` delegator sources. When ship's post-merge
cleanup then deletes the worktree, every one of those hooks fails, and an `on_error: open` hook
silently stops gating for every agent session on the machine (agent-tools#905:
runpod-offload-gate after PR #904).

How it is reached: ship.sh's cleanup runs `python3 -I hook_refs.py <worktree> <main-checkout>`
before removing each PR worktree and keeps the tree when this exits non-zero (and once more
after a removal, to report wiring that appeared while the tree was being removed). Any further
arguments are repos whose repo-local opencode plugin links are scanned too.

Exit codes (ship.sh's `_ship_keep_hook_backed_worktree` / `_ship_warn_hooks_left_dangling` /
`_ship_report_hook_refs` compare these literally — change them together):
    0  nothing installed references the worktree — safe to remove
    3  at least one reference found (one `<file>:<line>: <text>` per reference on stdout)
    4  no reference found, but some wiring file could not be read (listed on stdout) — the
       unread file might be the one that points into the worktree, so the caller keeps the tree

Scanned: the default AND env-relocated locations of each harness, mirroring the bridges'
`hooks_dir()` and rig's root resolvers (a location that does not exist is skipped, so
over-listing costs nothing; it is a union because ship's env need not match a harness's):
    roots     ~/.omp/agent, $OMP_CODING_AGENT_DIR, $PI_CODING_AGENT_DIR, $PI_CONFIG_DIR/agent,
              ~/.pi/agent, ~/.claude, $CLAUDE_CONFIG_DIR, ~/.claude-accounts/account-*,
              ~/.codex, $RIG_CODEX_HOME, $CODEX_HOME, $XDG_CONFIG_HOME/opencode,
              ~/.config/opencode
    hook dirs `<root>/hooks`, $OMP_HOOKS_DIR, $CC_HOOKS_DIR, $CODEX_HOOKS_DIR,
              $OPENCODE_HOOKS_DIR — their `*.json` descriptors
    link dirs omp/pi `<root>/extensions`, opencode `<root>/plugin(s)` — links checked by target,
              files by content, packaged plugin dirs walked (minus node_modules, dot-dirs).
              Claude's and Codex's own `plugins/` are their marketplace caches, not rig wiring.
    settings  in every root, the files rig writes bridge commands into
    repo-local `<repo>/.opencode/plugin(s)` (rig's default opencode bridge link) of every repo
              given as an argument and every repo in rig's registry
              (`<config>/rig/repositories.json`)
    plus `<config>/agent-tools/env`.
Matching: a wiring line (and, for JSON files, every decoded string — a `cmd` may be written
with `\\u002f`-escaped slashes) reaches the tree when it spells the tree's path, or when an
absolute path in it resolves into the tree through a symlink; a wiring entry reaches it when
its own real location is inside. Backups (`*.rig-bak-*`, `*.bak`) are skipped: no harness
loads them.

Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

EXIT_CLEAN = 0
EXIT_FOUND = 3
EXIT_INCOMPLETE = 4
SETTINGS_FILES = ("settings.json", "settings.local.json", "hooks.json", "config.toml",
                  "opencode.json", "opencode.jsonc")
AGENT_LINK_DIRS = ("extensions",)       # omp / pi extension modules
OPENCODE_LINK_DIRS = ("plugin", "plugins")
HOOKS_DIR_ENV = ("OMP_HOOKS_DIR", "CC_HOOKS_DIR", "CODEX_HOOKS_DIR", "OPENCODE_HOOKS_DIR")
SKIP_DIR_NAMES = {"node_modules"}
BACKUP_NAME = re.compile(r"\.rig-bak-\d[\d.-]*$|\.bak$")
# A path that is simply not there (or a link loop no harness could load either). Every other
# stat failure means the scan could not look, never that nothing is there.
ABSENT_ERRNOS = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}
PREVIEW_CHARS = 200  # of a matching line, in the report
# An absolute path inside a line of wiring (`cmd`, `PYTHONPATH=/a:/b`, an import specifier).
PATH_TOKEN = re.compile(r"(?<![\w.\-~])/[^\s\"'`:;,=()\[\]{}<>|\\]+")


def _expand(value: str, home: Path) -> Path:
    """An override as rig and the bridges resolve it: `$VAR`s expanded, then absolute,
    `~`-prefixed, or relative to home."""
    value = os.path.expandvars(value)
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    p = Path(value)
    return p if p.is_absolute() else home / p


def _dedupe(items: list[T], key: Callable[[T], Path] = lambda p: p) -> list[T]:
    """First occurrence of each item, compared by the realpath of `key(item)`."""
    seen: set[str] = set()
    out = []
    for item in items:
        real = os.path.realpath(key(item))
        if real not in seen:
            seen.add(real)
            out.append(item)
    return out


def _config_dirs(env: dict[str, str], home: Path) -> list[Path]:
    dirs = [home / ".config"]
    if env.get("XDG_CONFIG_HOME"):
        dirs.insert(0, _expand(env["XDG_CONFIG_HOME"], home))
    return _dedupe(dirs)


def harness_roots(env: dict[str, str], home: Path,
                  scan: Scan) -> list[tuple[Path, tuple[str, ...]]]:
    """Every harness root, each with the subdirs that harness loads extension code from."""
    omp_pi = [home / ".omp" / "agent", home / ".pi" / "agent"]
    omp_pi.extend(_expand(env[n], home) for n in ("OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR")
                  if env.get(n))
    if env.get("PI_CONFIG_DIR"):
        omp_pi.append(_expand(env["PI_CONFIG_DIR"], home) / "agent")
    claude_codex = [home / ".claude", home / ".codex"]
    claude_codex.extend(_expand(env[n], home) for n in ("CLAUDE_CONFIG_DIR", "RIG_CODEX_HOME",
                                                        "CODEX_HOME") if env.get(n))
    accounts = home / ".claude-accounts"
    if scan.kind(accounts) == "dir":
        claude_codex.extend(accounts / n for n in scan.listdir(accounts)
                            if n.startswith("account-") and scan.kind(accounts / n) == "dir")
    opencode = [c / "opencode" for c in _config_dirs(env, home)]
    # Claude's and Codex's own plugins/ are marketplace caches, not rig wiring: no link dirs.
    tagged = ([(r, AGENT_LINK_DIRS) for r in omp_pi] + [(r, ()) for r in claude_codex]
              + [(r, OPENCODE_LINK_DIRS) for r in opencode])
    return _dedupe(tagged, key=lambda pair: pair[0])


def _is_backup(name: str) -> bool:
    """rig's `<name>.rig-bak-<timestamp>` copies and plain `*.bak`. A live `x.rig-bak.json`
    is no backup: the descriptor loader reads every `*.json`."""
    return BACKUP_NAME.search(name) is not None


class Scan:
    """One scan's wiring entries and the places it could not read."""

    def __init__(self) -> None:
        self.entries: list[Path] = []
        self.unreadable: list[str] = []

    def mark_unreadable(self, path: Path, exc: Exception) -> None:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        self.unreadable.append(f"{path}: unreadable ({reason})")

    def kind(self, p: Path, *, follow: bool = True) -> str | None:
        """`dir`, `file`, `link` (only when not following) or `other`; None when `p` is
        absent. Any other failure — EACCES on `p` or an ancestor, say — is not "absent": it
        marks the scan incomplete (and returns None). Path.is_dir()/exists() cannot tell the
        two apart on every Python, so nothing here uses them."""
        try:
            mode = (os.stat(p) if follow else os.lstat(p)).st_mode
        except OSError as exc:
            if exc.errno not in ABSENT_ERRNOS:
                self.mark_unreadable(p, exc)
            return None
        if stat.S_ISLNK(mode):
            return "link"
        return "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"

    def listdir(self, d: Path) -> list[str]:
        try:
            return sorted(os.listdir(d))
        except OSError as exc:
            self.mark_unreadable(d, exc)
            return []

    def add_descriptor_dir(self, d: Path) -> None:
        """Every `*.json` — exactly what the descriptor loader reads (a backup never ends in
        `.json`)."""
        if self.kind(d) == "dir":
            self.entries.extend(d / n for n in self.listdir(d) if n.endswith(".json"))

    def add_link_dir(self, d: Path) -> None:
        """Every entry of an extension/plugin dir: a link is checked by its target, a file by
        its content, and a real subdirectory (a packaged plugin) is walked."""
        if self.kind(d) != "dir":
            return
        for name in self.listdir(d):
            entry = d / name
            if _is_backup(name):
                continue
            if self.kind(entry, follow=False) == "dir":
                if name not in SKIP_DIR_NAMES and not name.startswith("."):
                    self.add_link_dir(entry)
                continue
            self.entries.append(entry)


def _registry_repos(scan: Scan, env: dict[str, str], home: Path) -> list[Path]:
    """Repo paths from rig's machine-local registry; an unreadable registry is incomplete."""
    repos: list[Path] = []
    for config in _config_dirs(env, home):
        registry = config / "rig" / "repositories.json"
        if scan.kind(registry) != "file":
            continue
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            repos.extend(Path(os.path.expanduser(r["path"])) for r in data["repositories"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            scan.mark_unreadable(registry, exc)
    return repos


def scan_wiring(env: dict[str, str], home: Path, repos: list[Path]) -> Scan:
    scan = Scan()
    roots = harness_roots(env, home, scan)
    hook_dirs = [r / "hooks" for r, _ in roots]
    for name in HOOKS_DIR_ENV:
        if env.get(name):
            # The bridges take the override verbatim (`Path(value)`, so relative to their cwd
            # — ship's cwd is the best stand-in); rig writes it expanded.
            hook_dirs.extend((Path(os.path.abspath(env[name])), _expand(env[name], home)))
    for d in _dedupe(hook_dirs):
        scan.add_descriptor_dir(d)
    for root, link_dirs in roots:
        scan.entries.extend(root / name for name in SETTINGS_FILES)
        for sub in link_dirs:
            scan.add_link_dir(root / sub)
    for repo in _dedupe(repos + _registry_repos(scan, env, home)):
        for sub in OPENCODE_LINK_DIRS:
            scan.add_link_dir(repo / ".opencode" / sub)
    scan.entries.extend(c / "agent-tools" / "env" for c in _config_dirs(env, home))
    return scan


def _json_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _json_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _json_strings(v)


def _needle_pattern(worktree: str) -> re.Pattern[str]:
    """The worktree path as a whole path component chain: `<wt>` or `<wt>/...`, never a sibling
    sharing its prefix (`<wt>-other`, `<wt>2`) nor a longer name ending in it (`/x<wt>`). A
    preceding `/` still matches: `//<wt>` and `file://<wt>` are the same tree."""
    needles: set[str] = set()
    for p in {worktree.rstrip("/"), os.path.realpath(worktree)}:
        escaped = json.dumps(p)[1:-1]  # JSON may carry the path \u-escaped
        # A shell command may carry it backslash-escaped (`feat\ hook`), as printf %q does.
        shell = re.sub(r"([^\w/.\-+@%:,=])", r"\\\1", p)
        needles.update({p, escaped, escaped.replace("/", "\\/"), shell})
    alt = "|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True))
    return re.compile(rf"(?<![\w.\-])(?:{alt})(?![\w.\-])")


class TreeMatcher:
    """Does wiring reach into the tree — by spelling its path, or through a symlink?"""

    def __init__(self, worktree: str) -> None:
        self.real = os.path.realpath(worktree)
        self.pattern = _needle_pattern(worktree)

    def inside(self, path: str | Path) -> str | None:
        """The real location of `path` when it lies in the tree, else None."""
        target = os.path.realpath(path)
        return target if target == self.real or target.startswith(self.real + "/") else None

    def hit(self, text: str) -> str | None:
        """What in `text` reaches the tree: the text itself when it spells the tree's path or,
        whole, is an absolute path resolving into it (a descriptor `cmd` is run verbatim as
        argv[0], spaces and colons included); else an absolute path inside it that resolves
        into the tree (`/opt/hooks/gate` where `/opt/hooks` links into the tree)."""
        if self.pattern.search(text):
            return text.strip()[:PREVIEW_CHARS]
        candidates = ([text] if text.startswith("/") else []) + PATH_TOKEN.findall(text)
        for path in candidates:
            target = self.inside(path)
            if target:
                return f"{path[:PREVIEW_CHARS]} -> {target}"
        return None


def _content_refs(entry: Path, data: bytes, matcher: TreeMatcher) -> list[str]:
    """References in a file's raw lines; for a JSON file with none there, in its decoded
    strings (what a JSON reader such as the hook runner actually executes)."""
    text = data.decode("utf-8", errors="replace")
    refs = []
    for lineno, line in enumerate(text.splitlines(), 1):
        hit = matcher.hit(line)
        if hit:
            refs.append(f"{entry}:{lineno}: {hit}")
    if refs or entry.suffix != ".json":
        return refs
    try:
        decoded = json.loads(text)
    except ValueError:
        return refs  # the raw scan above is all a non-JSON reader could act on
    hits = (matcher.hit(s) for s in _json_strings(decoded))
    return [f"{entry}: (decoded) {hit}" for hit in hits if hit]


def find_refs(worktree: str, env: dict[str, str], home: Path,
              repos: list[Path]) -> tuple[list[str], list[str]]:
    matcher = TreeMatcher(worktree)
    scan = scan_wiring(env, home, repos)
    refs: list[str] = []
    for entry in scan.entries:
        own_kind = scan.kind(entry, follow=False)
        if own_kind is None:
            continue  # absent (or unlistable, which kind() already recorded)
        # By real location, not only for a link itself: a plugin file reached through a
        # symlinked plugins/ or hooks/ dir (or root) lives in the tree just the same.
        target = matcher.inside(entry)
        if target:
            refs.append(f"{entry}: -> {target}")
            continue
        if own_kind != "file" and not (own_kind == "link" and scan.kind(entry) == "file"):
            continue  # a directory, or a link to something outside the tree that is no file
        try:
            # The whole file: a reference past any size cap must not read as absent.
            data = entry.read_bytes()
        except OSError as exc:
            scan.mark_unreadable(entry, exc)
            continue
        refs.extend(_content_refs(entry, data, matcher))
    # The delegator prefers an exported AGENT_TOOLS_ROOT over the env file.
    root = env.get("AGENT_TOOLS_ROOT")
    hit = matcher.hit(root) if root else None
    if hit:
        refs.append(f"$AGENT_TOOLS_ROOT: {hit}")
    return refs, scan.unreadable


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: hook_refs.py <worktree> [repo ...]", file=sys.stderr)
        return 2
    repos = [Path(r) for r in argv[2:]]
    refs, unreadable = find_refs(argv[1], dict(os.environ), Path.home(), repos)
    for line in refs + unreadable:
        print(line)
    if refs:
        return EXIT_FOUND
    return EXIT_INCOMPLETE if unreadable else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main(sys.argv))
