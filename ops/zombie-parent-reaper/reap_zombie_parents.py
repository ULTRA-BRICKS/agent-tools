#!/usr/bin/env python3
"""reap_zombie_parents.py — periodic sweep that reaps Unix zombies by
PID-targeted restart of an allowlisted leaky parent.

Accessed via: launchd StartInterval (install.sh) every 15 minutes, or
`python3 reap_zombie_parents.py --dry-run` from a checkout.

Assumptions: macOS BSD `ps` (`etime=` not Linux `etimes=`; `-ww` for full
argv). Stdlib only, so launchd can run `/usr/bin/python3` with no venv.

Past bugs this exists to close: a one-shot named kill (`pkill -f Raycast`,
the 2026-09-05 cleanup) does not prevent recurrence (175-asks 131/138),
cannot reap STAT=Z children (they are already dead), and is blocked by
pkill-guard for shared process names. Zombies measured 2026-09-08 sat
under `Raycast Helper (Extensions)` for 3–4 days.

GUARD, not named killer:
- Victim identity is STAT=Z, not a process name.
- The process we signal is the PARENT, identified by a POSITIVE allowlist
  phrase (`Raycast Helper (Extensions)` at a token/`/` boundary), never
  a generic `Raycast` substring.
- We NEVER signal Raycast.app itself (user-facing GUI), pid 1, our own
  pid, or a parent whose re-read argv, process start time, or inferred
  birth epoch no longer matches the process-list snapshot (PID reuse,
  including a same-argv respawn landing between the snapshot and the
  first signal — review follow-up #683).
- Age gate: at least one Z child older than MIN_ZOMBIE_AGE_S so a brief
  fork/exec race is not a kill.
- Kill is PID-targeted SIGTERM, then SIGKILL if still alive — the only
  way to let init reap someone else's zombies is to end the parent.
  Raycast respawns the Helper.

TREE / NAME KILLS ARE OUT OF SCOPE: this script never calls pkill/killall
and never signals the zombie pids themselves.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

MIN_ZOMBIE_AGE_S = 15 * 60
STOP_GRACE_S = 2.0

# Tolerance for the birth-token check in sweep(): the initial process-list
# snapshot and the pre-SIGTERM identity reread are two separate `ps` calls,
# so their timestamps can't line up exactly. This covers `etime`/`lstart`
# 1s rounding plus the (normally sub-second) gap between the two calls.
# Review follow-up #683.
BIRTH_TOKEN_TOLERANCE_S = 5.0

# `ps -o lstart=` format on macOS, e.g. "Wed Sep 16 14:39:05 2026".
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"

# The helper's comm contains spaces, so we cannot split args= on whitespace
# to get argv[0]. Match ONLY the entire args string: the bare comm, or a
# single absolute path whose basename is that comm (no extra argv, no
# `python worker.py …Helper`). Review P2 on #669.
_HELPER_COMM = "Raycast Helper (Extensions)"

# pid ppid stat etime args. etime is NO-SPACE (`[[dd-]hh:]mm:ss`).
_PS_ROW = re.compile(r"^(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)$")


def _warn(msg: str) -> None:
    print(f"reap-zombie-parents: WARNING: {msg}", file=sys.stderr)


def _parse_bsd_elapsed_time(etime: str) -> Optional[float]:
    """Parse BSD `ps -o etime=` (`[[dd-]hh:]mm:ss`) into seconds.

    SYNC: same parser as ops/vscode-orphan-reaper/reap_vscode_orphans.py
    (`_parse_bsd_elapsed_time`). Duplicated because each launchd script is
    a standalone stdlib file with no shared import path.
    """
    etime = etime.strip()
    if not etime:
        return None
    days = 0
    rest = etime
    if "-" in etime:
        day_part, rest = etime.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = rest.split(":")
    try:
        parts_i = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts_i) == 2:
        mm, ss = parts_i
        total = days * 86400 + mm * 60 + ss
    elif len(parts_i) == 3:
        hh, mm, ss = parts_i
        total = days * 86400 + hh * 3600 + mm * 60 + ss
    else:
        return None
    return float(total)


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    stat: str
    age_s: float
    args: str


@dataclass
class ReapReport:
    reaped: list[ProcessInfo] = field(default_factory=list)
    killed_pids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "reaped_pids": [p.pid for p in self.reaped],
            "killed_pids": list(self.killed_pids),
            "killed_pid_count": len(self.killed_pids),
        }


def is_leaky_helper(args: str) -> bool:
    """True iff argv is the Raycast Extensions helper, not the GUI app.

    The helper comm contains spaces, so ``args=`` cannot be split on
    whitespace to recover argv[0]. Accept only (a) the bare comm as the
    entire display string, or (b) a single absolute path whose *entire*
    remaining string is ``/<comm>`` and whose directory prefix has no
    spaces — so ``python worker.py …Helper`` cannot match.
    """
    args = args.strip()
    if not args:
        return False
    if args == _HELPER_COMM:
        return True
    suffix = "/" + _HELPER_COMM
    if not args.endswith(suffix):
        return False
    prefix = args[: -len(_HELPER_COMM)]
    return prefix.startswith("/") and prefix.endswith("/") and " " not in prefix


def should_reap_parent(parent: ProcessInfo, children: list[ProcessInfo]) -> bool:
    """True when parent is the leaky helper and has at least one old Z child."""
    if parent.pid <= 1:
        return False
    if parent.pid == os.getpid():
        return False
    if not is_leaky_helper(parent.args):
        return False
    if parent.stat[:1] == "Z":
        return False
    return any(c.stat[:1] == "Z" and c.age_s >= MIN_ZOMBIE_AGE_S for c in children)


def _run_ps(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> Optional[str]:
    try:
        proc = run(
            ["ps", "-ax", "-ww", "-o", "pid=,ppid=,stat=,etime=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        _warn(f"ps failed to run: {exc}")
        return None
    if proc.returncode != 0:
        _warn(f"ps exited {proc.returncode}: {(proc.stderr or '').strip()}")
        return None
    return proc.stdout


def _parse_ps_rows(stdout: str) -> list[ProcessInfo]:
    out: list[ProcessInfo] = []
    nonempty = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        nonempty += 1
        m = _PS_ROW.match(line.strip())
        if not m:
            continue
        pid_s, ppid_s, stat, etime, args = m.groups()
        age = _parse_bsd_elapsed_time(etime)
        if age is None:
            continue
        out.append(
            ProcessInfo(
                pid=int(pid_s),
                ppid=int(ppid_s),
                stat=stat,
                age_s=age,
                args=args,
            )
        )
    if nonempty and not out:
        _warn(
            f"ps produced {nonempty} non-empty line(s) but NONE matched the "
            "expected row shape (pid ppid stat etime args) -- every candidate "
            "was silently dropped this sweep."
        )
    return out


def _list_processes(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> list[ProcessInfo]:
    stdout = _run_ps(run=run)
    if stdout is None:
        return []
    return _parse_ps_rows(stdout)


def _reread_args(
    pid: int,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> Optional[str]:
    try:
        proc = run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    args = (proc.stdout or "").strip()
    return args or None


def _process_start_time(
    pid: int,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> Optional[str]:
    """Return `ps -o lstart=` for pid: a fixed wall-clock timestamp for the
    life of the process. Unlike `etime` (elapsed time, ticks every second),
    this is stable identity — it lets us tell "same process" apart from "a
    different process, even one with identical argv, that reused this pid"
    (review P1 #669: argv alone cannot catch a same-argv Helper respawn).
    """
    try:
        proc = run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    start_time = (proc.stdout or "").strip()
    return start_time or None


def _parse_lstart_epoch(lstart: str) -> Optional[float]:
    """Parse `ps -o lstart=` into a Unix epoch, or None if unparseable.

    Used by sweep()'s birth-token check (review follow-up #683) to confirm
    the pid we are about to signal is old enough to be the parent that had
    the aged zombie child, not a fresh same-argv respawn that reused the
    pid between the process-list snapshot and this reread.

    Residual risk, accepted rather than fixed here: `lstart` has 1-second
    resolution, so a respawn landing in the SAME second as the original
    process would produce an identical string this check (and the later
    argv+start-time `_identity_ok` recheck) cannot distinguish. Closing
    that would need a sub-second-precision source (e.g. proc_pidinfo via
    ctypes) for a guard that is already narrowed by an exact-argv
    allowlist match, a positive parent-age requirement, and a 15-minute
    zombie-age gate — the same guard-not-hardened-security-boundary
    trade-off already made for the deferred residence-time check on #669.
    """
    try:
        return time.mktime(time.strptime(lstart.strip(), _LSTART_FORMAT))
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_kill(pid: int, sig: int) -> None:
    os.kill(pid, sig)


def _stop_pid(
    pid: int,
    *,
    kill: Callable[[int, int], None],
    sleeper: Callable[[float], None],
    pid_alive: Callable[[int], bool],
    identity_ok: Callable[[int], bool],
) -> None:
    try:
        kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    sleeper(STOP_GRACE_S)
    if not pid_alive(pid):
        return
    # PID-reuse during the grace window: kill(pid, 0) succeeds on an
    # unrelated replacement — including a freshly respawned Helper with the
    # SAME argv. Re-validate argv AND start time before SIGKILL (review P1
    # #669): start time is the only thing a pid-reusing respawn can't share.
    if not identity_ok(pid):
        return
    try:
        kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def sweep(
    *,
    list_processes: Optional[Callable[[], list[ProcessInfo]]] = None,
    kill: Optional[Callable[[int, int], None]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
    dry_run: bool = False,
) -> ReapReport:
    # Resolve defaults inside the body so monkeypatching module-level names
    # (as main()'s tests do) actually takes effect.
    list_processes = list_processes or _list_processes
    kill = kill or _default_kill
    sleeper = sleeper or time.sleep
    pid_alive = pid_alive or _pid_alive

    procs = list_processes()
    now0 = time.time()
    by_pid = {p.pid: p for p in procs}
    by_ppid: dict[int, list[ProcessInfo]] = defaultdict(list)
    for p in procs:
        by_ppid[p.ppid].append(p)

    report = ReapReport()
    for ppid, kids in by_ppid.items():
        parent = by_pid.get(ppid)
        if parent is None:
            continue
        if not should_reap_parent(parent, kids):
            continue
        if dry_run:
            report.reaped.append(parent)
            continue
        current = _reread_args(parent.pid)
        if current is None or not is_leaky_helper(current):
            continue
        start_time = _process_start_time(parent.pid)
        if start_time is None:
            # Cannot fingerprint the process we're about to signal — do not
            # act (review P1 #669).
            continue
        # Birth-token check (review follow-up #683): the process-list
        # snapshot above and this reread are two separate `ps` calls. If
        # the parent pid exited and got reused by a brand-new same-argv
        # respawn in between, `current`/`start_time` describe the NEW
        # process, not the one should_reap_parent() actually decided on.
        # A reused pid is always young: its real lstart is ~now, while the
        # snapshot's inferred birth epoch (now0 - parent.age_s) is at
        # least MIN_ZOMBIE_AGE_S in the past — a zombie can never be older
        # than its own parent. Refuse whenever the two disagree by more
        # than measurement/timing slop.
        lstart_epoch = _parse_lstart_epoch(start_time)
        expected_birth_epoch = now0 - parent.age_s
        if (
            lstart_epoch is None
            or abs(expected_birth_epoch - lstart_epoch) > BIRTH_TOKEN_TOLERANCE_S
        ):
            continue

        def _identity_ok(pid: int, _start_time: str = start_time) -> bool:
            again = _reread_args(pid)
            if not again or not is_leaky_helper(again):
                return False
            return _process_start_time(pid) == _start_time

        _stop_pid(
            parent.pid,
            kill=kill,
            sleeper=sleeper,
            pid_alive=pid_alive,
            identity_ok=_identity_ok,
        )
        report.reaped.append(parent)
        report.killed_pids.append(parent.pid)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report leaky parents, signal nothing",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)
    report = sweep(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report.as_dict()))
        return 0
    verb = "would reap" if args.dry_run else "reaped"
    if not report.reaped:
        print(f"reap-zombie-parents: {verb} 0 leaky parent(s)")
        return 0
    for p in report.reaped:
        print(
            f"reap-zombie-parents: {verb} pid={p.pid} ppid={p.ppid} "
            f"args={p.args!r}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
