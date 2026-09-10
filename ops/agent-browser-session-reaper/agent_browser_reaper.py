#!/usr/bin/env python3
"""agent_browser_reaper.py — close stale `agent-browser` sessions (periodic sweep).

WHY THIS EXISTS (2026-09-04 incident)
-------------------------------------
`agent-browser` sessions are daemon + headless-Chrome process trees. One idle
session measured on this machine is ~14 processes and ~1.2 GB RSS. An agent run
that opens a browser and never closes it leaves the whole tree resident. On
2026-09-04 the machine hit memory exhaustion twice (swap 13.6 GB used of 14.3 GB,
background tasks killed by the OS); two manual cleanups found 4 and then 5
abandoned sessions, together ~2.4 GB and 82 processes at peak — leftovers named
`default`, `scrollstack-verify-25863`, `hero-followup-verify`, `pricing-fix-verify`,
`content-editors-verify`, `scrollstack-fix2-verify`, `hero-rounding-check`.

This is the OS-level backstop: a periodic launchd job that closes sessions past an
age threshold no matter which agent (or which harness) abandoned them. The
complementary, more precise mechanism is the session-end hook (`session_end_close.py`
in this same directory), which closes only the sessions a finishing Claude Code
session actually claimed. Neither replaces the other: the hook cannot fire for a
crashed/killed session, and the sweep cannot know who owned what.

HOW SESSIONS ARE ENUMERATED — READ-ONLY, VERIFIED
-------------------------------------------------
`agent-browser session list --json` returns `{"data": {"sessions": [names...]}}` and
does NOT launch a browser. This was verified empirically on 2026-09-04 (0 matching
processes before the call, 0 after). That distinction matters: `agent-browser tab
list` DOES launch a browser when none is running, so a naive "is anything running?"
probe creates the very session it was meant to count. Never probe with `tab list`.

HOW AGE IS DETERMINED — AND THE HONEST LIMIT OF IT
--------------------------------------------------
agent-browser exposes NO per-command last-activity timestamp, on disk or in any
command output. This was established by measurement, not assumption:

  * `session list` returns names only — no timestamps.
  * `session info --json` returns {active, pid, session, socketDir, version} — no
    timestamps.
  * `<state_dir>/<name>.target` mtime moves on navigation / tab rebinding but NOT on
    a plain `get url`, `get title`, or `eval` (measured: 5 consecutive `get url`
    calls left the mtime unchanged).
  * The Chrome `--user-data-dir` mtime does not move on those commands either.
  * The daemon's own CPU time is too coarse to use (5 commands moved it by 0.00s).

The daemon DOES track its own last activity internally — that is what
`AGENT_BROWSER_IDLE_TIMEOUT_MS` / `--idle-timeout` act on (verified: a session
launched with `--idle-timeout 20s` self-terminated within 30s, and the env-var form
works identically) — but it does not expose that clock to any caller.

So what this script computes is deliberately named `last_seen`, not "last activity":

    last_seen = max(daemon process start time, mtime of every <name>.* sidecar)

Both inputs are real, verifiable timestamps. Taking the MAX is the conservative
direction: it can only ever make a session look YOUNGER than it is, i.e. err toward
NOT closing. What it cannot see is a session that has been driven hard for hours
without ever navigating — such a session looks its full lifetime old. The mitigations
for that residual case are (a) a deliberately generous default threshold (3h — longer
than any agent verification run observed here) and (b) the `KEEP` list, which exempts
named sessions outright. If you need a hard guarantee for a long-running session, put
its name in `KEEP`.

SAFETY — WHY THIS CANNOT SHOOT SOMETHING ELSE
---------------------------------------------
This script NEVER sends a signal to any process. It never calls `kill`, and it never
matches processes by a `pkill -f` pattern (a loose pattern has twice killed unrelated
background jobs on this machine). The only destructive action it can take is
`agent-browser --session <name> close`, i.e. agent-browser's own supported teardown
for a session name that agent-browser itself just reported as live. A PID read from a
sidecar file is used for ONE thing — reading an elapsed time out of `ps` — so even a
recycled/mismatched PID can only skew an age estimate, never target a kill.

Stdlib only — no third-party deps, so it runs standalone under launchd's bare
/usr/bin/python3 with no virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# --- Defaults (documented in this directory's README and in --help) ---------------

DEFAULT_MAX_AGE_MIN = 180          # 3 hours
DEFAULT_CLAIM_TTL_HOURS = 24       # prune session-end claim dirs older than this
DEFAULT_ENABLED = True

CONFIG_PATH = Path(os.path.expanduser(
    os.environ.get("AGENT_BROWSER_REAPER_CONFIG",
                   "~/.config/agent-tools/agent-browser-reaper.conf")))

STATE_DIR = Path(os.path.expanduser(
    os.environ.get("AGENT_BROWSER_REAPER_STATE_DIR", "~/.agent-browser")))

CLAIM_DIR = Path(os.path.expanduser(
    os.environ.get("AGENT_BROWSER_REAPER_CLAIM_DIR",
                   "~/.cache/agent-tools/agent-browser-sessions")))

ENV_PREFIX = "AGENT_BROWSER_REAPER_"

# Sidecar suffixes agent-browser writes next to a session in its state dir. Only
# these are consulted for mtimes; anything else in the dir is ignored.
SIDECAR_SUFFIXES = (".pid", ".sock", ".version", ".engine", ".stream",
                    ".config", ".target")

# A session name agent-browser will accept back on the command line. Anything else is
# skipped rather than passed through to a subprocess.
_SAFE_SESSION_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")

_SUBPROCESS_TIMEOUT_S = 60

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _log(msg: str) -> None:
    print(f"[agent-browser-reaper] {msg}", flush=True)


# --- Configuration ---------------------------------------------------------------


@dataclass
class Config:
    """Resolved knobs. Precedence: environment variable > config file > default."""

    enabled: bool = DEFAULT_ENABLED
    max_age_min: int = DEFAULT_MAX_AGE_MIN
    claim_ttl_hours: int = DEFAULT_CLAIM_TTL_HOURS
    session_end_enabled: bool = True
    keep: frozenset[str] = field(default_factory=frozenset)


def _read_config_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE config file. Missing/unreadable file means "no overrides"."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = value.strip().strip('"').strip("'")
    return values


def _lookup(key: str, file_values: dict[str, str]) -> Optional[str]:
    """One knob, environment first (launchd has no shell env, so the file matters)."""
    from_env = os.environ.get(ENV_PREFIX + key)
    if from_env is not None and from_env != "":
        return from_env
    return file_values.get(key)


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _as_int(value: Optional[str], default: int, minimum: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Resolve every knob from the environment, then the config file, then defaults."""
    file_values = _read_config_file(path)
    keep_raw = _lookup("KEEP", file_values) or ""
    keep = frozenset(n.strip() for n in keep_raw.split(",") if n.strip())
    return Config(
        enabled=_as_bool(_lookup("ENABLED", file_values), DEFAULT_ENABLED),
        max_age_min=_as_int(_lookup("MAX_AGE_MIN", file_values),
                            DEFAULT_MAX_AGE_MIN, minimum=1),
        claim_ttl_hours=_as_int(_lookup("CLAIM_TTL_HOURS", file_values),
                                DEFAULT_CLAIM_TTL_HOURS, minimum=1),
        session_end_enabled=_as_bool(
            _lookup("SESSION_END_ENABLED", file_values), True),
        keep=keep,
    )


# --- agent-browser interaction ---------------------------------------------------


def _agent_browser_bin() -> Optional[str]:
    """Absolute path to the agent-browser CLI, or None when it is not installed."""
    override = os.environ.get("AGENT_BROWSER_REAPER_BIN")
    if override:
        return override if os.path.isfile(override) else None
    return shutil.which("agent-browser")


def list_live_sessions(run: Runner = subprocess.run) -> list[str]:
    """Live session names via `agent-browser session list --json`.

    Read-only: verified not to launch a browser (unlike `tab list`, which does).
    Returns [] on any failure — a broken probe must never be read as "everything is
    stale", because the caller would then try to close names it never confirmed.
    """
    binary = _agent_browser_bin()
    if not binary:
        return []
    try:
        proc = run([binary, "session", "list", "--json"], capture_output=True,
                   text=True, timeout=_SUBPROCESS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return []
    sessions = (payload.get("data") or {}).get("sessions")
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, str) and s]


def close_session(name: str, run: Runner = subprocess.run) -> bool:
    """Close ONE session through agent-browser's own supported teardown.

    This is the only destructive operation in this module. No signals, no pkill.
    """
    binary = _agent_browser_bin()
    if not binary or not _SAFE_SESSION_NAME.match(name):
        return False
    try:
        proc = run([binary, "--session", name, "close", "--json"],
                   capture_output=True, text=True,
                   timeout=_SUBPROCESS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# --- Age estimation --------------------------------------------------------------


def _parse_bsd_elapsed(etime: str) -> Optional[float]:
    """`ps -o etime=` ([[DD-]HH:]MM:SS) to seconds. None when unparseable."""
    text = etime.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    while len(numbers) < 3:
        numbers.insert(0, 0)
    hours, minutes, seconds = numbers
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _daemon_start_time(name: str, state_dir: Path,
                       run: Runner = subprocess.run) -> Optional[float]:
    """Epoch seconds when this session's daemon started, from its `.pid` sidecar.

    The PID is used ONLY to read an elapsed time out of `ps`. Nothing is ever
    signalled, so a stale or recycled PID can at worst skew an age estimate.
    """
    try:
        pid = int((state_dir / f"{name}.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        proc = run(["ps", "-p", str(pid), "-o", "etime="], capture_output=True,
                   text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    elapsed = _parse_bsd_elapsed(proc.stdout)
    return None if elapsed is None else time.time() - elapsed


def _sidecar_mtimes(name: str, state_dir: Path) -> list[float]:
    mtimes = []
    for suffix in SIDECAR_SUFFIXES:
        try:
            mtimes.append((state_dir / f"{name}{suffix}").stat().st_mtime)
        except OSError:
            continue
    return mtimes


def session_last_seen(name: str, state_dir: Path = STATE_DIR,
                      run: Runner = subprocess.run) -> Optional[float]:
    """Newest verifiable timestamp for a session: daemon start or any sidecar mtime.

    MAX, not min: every unknown pushes the answer toward "recent", i.e. toward not
    closing. Returns None when nothing at all is known, which the sweep treats as
    "leave it alone".
    """
    candidates = _sidecar_mtimes(name, state_dir)
    started = _daemon_start_time(name, state_dir, run=run)
    if started is not None:
        candidates.append(started)
    return max(candidates) if candidates else None


# --- Sweep -----------------------------------------------------------------------


def _stale_sessions(names: Iterable[str], cfg: Config, now: float,
                    state_dir: Path, run: Runner) -> list[tuple[str, float]]:
    """(name, age_minutes) for every session past the threshold and not in KEEP."""
    threshold_s = cfg.max_age_min * 60
    stale = []
    for name in names:
        if name in cfg.keep:
            _log(f"keep: {name} (listed in KEEP)")
            continue
        last_seen = session_last_seen(name, state_dir, run=run)
        if last_seen is None:
            _log(f"skip: {name} (no verifiable timestamp)")
            continue
        age_s = now - last_seen
        if age_s >= threshold_s:
            stale.append((name, age_s / 60.0))
        else:
            _log(f"keep: {name} (last seen {age_s / 60.0:.0f} min ago)")
    return stale


def prune_claim_dirs(cfg: Config, now: float, claim_dir: Path = CLAIM_DIR) -> int:
    """Delete session-end claim directories older than CLAIM_TTL_HOURS.

    A claim dir is normally removed by the session-end hook itself. One left behind
    means that Claude Code session crashed or was killed. Nothing but disk clutter
    depends on it after the TTL, and leaving it forever would keep blocking the
    session-end hook from closing a shared session name (see session_end_close.py).
    """
    cutoff = now - cfg.claim_ttl_hours * 3600
    removed = 0
    try:
        entries = list(claim_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
        except OSError:
            continue
        removed += 1
    if removed:
        _log(f"pruned {removed} claim dir(s) older than {cfg.claim_ttl_hours}h")
    return removed


def sweep(cfg: Config, *, dry_run: bool = False, state_dir: Path = STATE_DIR,
          claim_dir: Path = CLAIM_DIR, run: Runner = subprocess.run) -> int:
    """Close every live session past the age threshold. Returns the number closed."""
    if not cfg.enabled:
        _log("disabled (ENABLED=0) — nothing to do")
        return 0
    now = time.time()
    live = list_live_sessions(run=run)
    if not live:
        _log("no live sessions")
        prune_claim_dirs(cfg, now, claim_dir)
        return 0
    _log(f"{len(live)} live session(s); threshold {cfg.max_age_min} min")
    closed = 0
    for name, age_min in _stale_sessions(live, cfg, now, state_dir, run):
        if dry_run:
            _log(f"DRY RUN would close: {name} (last seen {age_min:.0f} min ago)")
            continue
        if close_session(name, run=run):
            _log(f"closed: {name} (last seen {age_min:.0f} min ago)")
            closed += 1
        else:
            _log(f"FAILED to close: {name} (last seen {age_min:.0f} min ago)")
    prune_claim_dirs(cfg, now, claim_dir)
    return closed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close stale agent-browser sessions.",
        epilog=(f"Config file: {CONFIG_PATH} (KEY=VALUE). Every key is also readable "
                f"as {ENV_PREFIX}<KEY>, which wins. Keys and defaults: "
                f"ENABLED=1, MAX_AGE_MIN={DEFAULT_MAX_AGE_MIN}, "
                f"CLAIM_TTL_HOURS={DEFAULT_CLAIM_TTL_HOURS}, "
                f"SESSION_END_ENABLED=1, KEEP= (comma-separated session names never "
                f"closed)."))
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be closed, close nothing")
    parser.add_argument("--max-age-min", type=int, default=None,
                        help="override the age threshold for this run")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.max_age_min is not None and args.max_age_min >= 1:
        cfg = Config(cfg.enabled, args.max_age_min, cfg.claim_ttl_hours,
                     cfg.session_end_enabled, cfg.keep)
    sweep(cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
