#!/usr/bin/env python3
"""agents-hooks/v1 post-write hook — format the file the agent just wrote.

Runs the project's configured formatter on each file the agent writes/edits, in place,
so formatting is a harness concern instead of a per-project manual step. Fires on
`post-write` (after the file lands on disk — a formatter rewrites a real file, so it must
exist first). NEVER blocks: every failure mode (no formatter, tool missing, formatter
non-zero, timeout, bad event) resolves to `allow`; the pre-commit git-hook stays the gate.
Escape hatch: NO_FORMAT_HOOK=1.

See README.md (next to this file) for the full rationale, the detection table, and the
"why post-write, not pre-write" discussion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

HOOK_API = "agents-hooks/v1"
ESCAPE_HATCH_ENV = "NO_FORMAT_HOOK"
# Per-file formatter run budget. The host's own timeout_ms is the hard ceiling; this is a
# softer internal bound so a wedged formatter can't eat the whole host budget.
RUN_TIMEOUT_S = 8


def emit_allow() -> None:
    # Trailing newline: line-oriented hosts read the protocol line with readline().
    sys.stdout.write(json.dumps({"hook_api": HOOK_API, "decision": "allow"}) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    sys.stderr.write(f"format-on-write: {msg}\n")


def find_up(start: Path, *names: str) -> Path | None:
    """Walk up from `start`'s directory to the filesystem root looking for any of `names`.

    Returns the directory containing the first match (so callers can resolve a sibling,
    e.g. node_modules next to the package.json that mentioned the tool)."""
    here = start if start.is_dir() else start.parent
    for d in [here, *here.parents]:
        for name in names:
            if (d / name).exists():
                return d
    return None


def repo_root(start: Path) -> Path | None:
    # The repo root anchors both the package.json lookup and the node_modules/.bin lookup,
    # so repo-LOCAL formatter detection only fires inside a git worktree. Outside one
    # (root is None) only globally-installed tools (gofmt/rustfmt/a global oxfmt) apply.
    return find_up(start, ".git")


def _package_json(root: Path | None) -> dict:
    """The repo-root package.json as a dict; {} when absent, unreadable, or not an object."""
    if root is None:
        return {}
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _declares_dependency(pkg: dict, name: str) -> bool:
    """True when package.json lists `name` as a DIRECT (dev)dependency — the repo chose the
    tool, as opposed to a transitive install that merely drops a bin in node_modules/.bin."""
    return any(
        isinstance(pkg.get(field), dict) and name in pkg[field]
        for field in ("dependencies", "devDependencies")
    )


def _has_any(root: Path | None, names: tuple[str, ...]) -> bool:
    return root is not None and any((root / n).is_file() for n in names)


def local_bin(root: Path | None, tool: str) -> str | None:
    """Prefer a repo-local node_modules/.bin/<tool> if present and executable."""
    if root is None:
        return None
    cand = root / "node_modules" / ".bin" / tool
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand)
    return None


def has_global(tool: str) -> bool:
    return shutil.which(tool) is not None


# ── Detection table ────────────────────────────────────────────────────────────────────
# Maps a file extension to an ordered list of formatters. The FIRST formatter the repo
# CONFIGURES for that extension is the one used; if it is not installed the hook is a
# no-op. Availability alone never selects a tool, and a configured-but-missing tool never
# falls through to the next one: in a biome repo that also uses prettier for Markdown,
# a transitive node_modules/.bin/prettier or a missing biome must not let prettier
# rewrite .ts files in a style the repo's own lint then rejects (agent-tools#901).
# The table is fixed (not user-configurable) — a deliberately small, common set; an
# unsupported tool simply falls through to a no-op (the escape hatch / pre-commit gate
# remain available).

# What biome can format, keyed to the biome.json language section whose `formatter.enabled`
# switches it. HTML formatting is opt-in in biome (off unless `html.formatter.enabled: true`);
# Markdown/YAML stay with prettier/oxfmt.
BIOME_LANGUAGE = {
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"), "javascript"),
    ".json": "json", ".jsonc": "json", ".css": "css", ".html": "html",
}
BIOME_OPT_IN_LANGUAGES = {"html"}
JS_TS_EXT = set(BIOME_LANGUAGE) | {".md", ".mdx", ".yaml", ".yml"}

# biome's own lookup order for a config file in a directory
BIOME_CONFIG_FILES = ("biome.json", "biome.jsonc", ".biome.json", ".biome.jsonc")
OXFMT_CONFIG_FILES = (".oxfmtrc.json", ".oxfmtrc.jsonc", "oxfmt.config.ts", "oxfmt.config.mts")
PRETTIER_CONFIG_FILES = (
    ".prettierrc", ".prettierrc.json", ".prettierrc.json5", ".prettierrc.yaml",
    ".prettierrc.yml", ".prettierrc.toml", ".prettierrc.js", ".prettierrc.cjs",
    ".prettierrc.mjs", ".prettierrc.ts", ".prettierrc.cts", ".prettierrc.mts",
    "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs",
    "prettier.config.ts", "prettier.config.cts", "prettier.config.mts",
)


# JSONC → JSON in two passes, each matching strings first and keeping them verbatim (so a
# "//" inside a URL survives): drop comments, then drop commas before a closing bracket.
_JSON_STRING = r'("(?:\\.|[^"\\])*")'
_JSONC_COMMENT = re.compile(_JSON_STRING + r"|//[^\n]*|/\*.*?\*/", re.S)
_JSONC_TRAILING_COMMA = re.compile(_JSON_STRING + r"|,(?=\s*[}\]])")


def _jsonc_loads(text: str) -> object:
    for pattern in (_JSONC_COMMENT, _JSONC_TRAILING_COMMA):
        text = pattern.sub(lambda m: m.group(1) or "", text)
    return json.loads(text)


def _biome_config(root: Path) -> dict:
    """The root biome.json(c) (JSONC: comments and trailing commas allowed); {} when it
    cannot be read or parsed — biome stays the repo's tool and reports the error itself."""
    for name in BIOME_CONFIG_FILES:
        try:
            data = _jsonc_loads((root / name).read_text(encoding="utf-8"))
        except OSError:
            continue
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _formatter_enabled(section: object) -> bool | None:
    """A config section's explicit `formatter.enabled`, or None when it does not set one."""
    fmt = section.get("formatter") if isinstance(section, dict) else None
    enabled = fmt.get("enabled") if isinstance(fmt, dict) else None
    return enabled if isinstance(enabled, bool) else None


def _biome_formats(root: Path | None, file: Path) -> bool:
    """True when the repo configures biome, unless the root biome config plainly switches
    the formatter off for `file`'s language (a lint-only biome) — then the file goes to the
    next configured formatter. The language section's `formatter.enabled` wins over the
    global one, as in biome. An opt-in language (HTML) is biome's only when the config
    enables it.

    Wrong formatting is the harm this hook must avoid; no formatting is harmless (the
    pre-commit gate is the backstop). So whenever the effective setting needs biome's own
    resolution — a nested biome config between the file and the root, an `extends` chain,
    a `"root": false` config that inherits from outside the repo, or `overrides` that
    touch the language's formatting — biome keeps the file: at worst biome formats
    nothing, but a second formatter never takes over a file biome may own."""
    if not _has_any(root, BIOME_CONFIG_FILES):
        return False
    cfg = _biome_config(root)
    lang = BIOME_LANGUAGE[file.suffix.lower()]
    raw_overrides = cfg.get("overrides")
    overrides = [o for o in raw_overrides if isinstance(o, dict)] if isinstance(raw_overrides, list) else []
    opt_in = lang in BIOME_OPT_IN_LANGUAGES
    # An opt-in language ignores a global `formatter` override: only its own section enables it.
    if (
        any(_has_any(d, BIOME_CONFIG_FILES) for d in file.parents if d != root and root in d.parents)
        or "extends" in cfg
        or cfg.get("root") is False
        or any(lang in o or ("formatter" in o and not opt_in) for o in overrides)
    ):
        return True
    enabled = _formatter_enabled(cfg.get(lang))
    if opt_in:
        return enabled is True
    return enabled if enabled is not None else _formatter_enabled(cfg) is not False


def _oxfmt_configured(root: Path | None) -> bool:
    return _has_any(root, OXFMT_CONFIG_FILES) or _declares_dependency(_package_json(root), "oxfmt")


def _prettier_configured(root: Path | None) -> bool:
    if _has_any(root, PRETTIER_CONFIG_FILES):
        return True
    pkg = _package_json(root)
    return "prettier" in pkg or _declares_dependency(pkg, "prettier")


def _ruff_configured(root: Path | None) -> bool:
    """True when the repo CONFIGURES ruff: a ruff.toml/.ruff.toml, or a [tool.ruff] (or
    [tool.ruff.*]) section in pyproject.toml."""
    if not root:
        return False
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return True
    pp = root / "pyproject.toml"
    if pp.is_file():
        try:
            text = pp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return re.search(r"(?m)^\s*\[tool\.ruff(\.[^\]]+)?\]", text) is not None
    return False


def _ruff_opted_in(root: Path | None) -> bool:
    # A repo-local ruff is an explicit opt-in. A GLOBAL ruff is the project's formatter ONLY
    # if the repo CONFIGURES ruff; else a repo set up for Black (with ruff merely installed
    # globally) would be silently reformatted by ruff.
    return local_bin(root, "ruff") is not None or _ruff_configured(root)


def _always(_root: Path | None) -> bool:
    # The language's canonical formatter (black/gofmt/rustfmt): used whenever installed.
    return True


def resolve_cmd(root: Path | None, tool: str) -> str | None:
    """The runnable command: a repo-local bin (absolute) over a bare global name on PATH."""
    return local_bin(root, tool) or (tool if has_global(tool) else None)


# argv builders
def _argv_oxfmt(cmd: str, f: str) -> list[str]:
    return [cmd, "--write", "--no-error-on-unmatched-pattern", f]


def _argv_prettier(cmd: str, f: str) -> list[str]:
    return [cmd, "--write", f]


def _argv_biome(cmd: str, f: str) -> list[str]:
    return [cmd, "format", "--write", f]


def _argv_black(cmd: str, f: str) -> list[str]:
    return [cmd, "-q", f]


def _argv_ruff(cmd: str, f: str) -> list[str]:
    return [cmd, "format", f]


def _argv_gofmt(cmd: str, f: str) -> list[str]:
    return [cmd, "-w", f]


def _argv_rustfmt(cmd: str, f: str) -> list[str]:
    return [cmd, f]


# A formatter's `configured(root, file)` says whether the repo chose it for `file`.
Configured = Callable[[Path | None, Path], bool]


class Formatter(NamedTuple):
    tool: str
    configured: Configured
    argv: Callable[[str, str], list[str]]


def _per_repo(pred: Callable[[Path | None], bool]) -> Configured:
    return lambda root, _file: pred(root)


# extension → ordered formatters; the first one the repo configures wins
TABLE: dict[str, list[Formatter]] = {}
for _ext in JS_TS_EXT:
    TABLE[_ext] = [
        *([Formatter("biome", _biome_formats, _argv_biome)] if _ext in BIOME_LANGUAGE else []),
        Formatter("oxfmt", _per_repo(_oxfmt_configured), _argv_oxfmt),
        Formatter("prettier", _per_repo(_prettier_configured), _argv_prettier),
    ]
for _ext in (".py", ".pyi"):
    TABLE[_ext] = [
        Formatter("ruff", _per_repo(_ruff_opted_in), _argv_ruff),
        Formatter("black", _per_repo(_always), _argv_black),
    ]
TABLE[".go"] = [Formatter("gofmt", _per_repo(_always), _argv_gofmt)]
TABLE[".rs"] = [Formatter("rustfmt", _per_repo(_always), _argv_rustfmt)]


def select_formatter(root: Path | None, file: Path) -> tuple[Formatter, str | None] | None:
    """The repo's configured formatter for `file` plus its runnable command — None for the
    command when that formatter is configured but not installed; None overall when the
    repo configures no formatter for `file`."""
    for fmt in TABLE.get(file.suffix.lower(), []):
        if fmt.configured(root, file):
            return fmt, resolve_cmd(root, fmt.tool)
    return None


def resolve_path(event: dict) -> Path | None:
    args = event.get("args") or {}
    raw = (
        args.get("path")
        or args.get("file_path")
        or args.get("filePath")
        or event.get("path")
        or ""
    )
    if not isinstance(raw, str) or not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        cwd = event.get("cwd")
        if isinstance(cwd, str) and cwd:
            p = Path(cwd) / p
    return p


def main() -> int:
    # Escape hatch: bail before doing anything.
    if os.environ.get(ESCAPE_HATCH_ENV) == "1":
        log(f"{ESCAPE_HATCH_ENV}=1 — skipping (allow)")
        emit_allow()
        return 0

    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        log(f"could not parse event: {exc} — allow (formatting is advisory)")
        emit_allow()
        return 0
    if not isinstance(event, dict):
        log("event was not a JSON object — allow (formatting is advisory)")
        emit_allow()
        return 0

    path = resolve_path(event)
    if path is None or not path.is_file():
        log(f"no written file to format (path={path!r}) — allow")
        emit_allow()
        return 0

    ext = path.suffix.lower()
    if ext not in TABLE:
        log(f"no formatter mapping for '{ext}' — allow (no-op)")
        emit_allow()
        return 0

    root = repo_root(path)
    selected = select_formatter(root, path)
    if selected is None:
        log(f"no configured/available formatter for '{ext}' in this repo — allow (no-op)")
        emit_allow()
        return 0
    fmt, cmd = selected
    if cmd is None:
        log(f"'{fmt.tool}' formats '{ext}' in this repo but is not installed — allow (no-op)")
        emit_allow()
        return 0
    try:
        # noqa: S603,S607 -- the executable is a formatter from a fixed allowlist
        # (resolved to an absolute repo-local bin, or a bare global name like "gofmt"
        # that we deliberately let PATH resolve); the only data input is the written
        # file path, passed as its own argv element (never shell-interpolated).
        proc = subprocess.run(  # noqa: S603
            fmt.argv(cmd, str(path)),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            cwd=str(root) if root is not None else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"formatter '{fmt.tool}' failed to run ({exc}) — allow (no-op)")
        emit_allow()
        return 0
    if proc.returncode == 0:
        log(f"formatted {path.name} with {fmt.tool}")
    else:
        # Non-zero (e.g. syntax error in a half-written file) — don't block, just note.
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = err[-1] if err else "non-zero exit"
        log(f"formatter '{fmt.tool}' exited {proc.returncode}: {tail} — allow")
    emit_allow()
    return 0


if __name__ == "__main__":
    sys.exit(main())
