#!/usr/bin/env python3
"""reap_tgctl_omp_acp.py — periodic sweep for orphaned tg-ctl ACP test helpers.

WHY THIS EXISTS (2026-09-09 OOM incident, agent-tools#697)
----------------------------------------------------------
tg-cli's ctl-omp-acp integration tests spawn throwaway `omp acp` children via
fake binaries under `mkdtemp(..., 'tgctl-omp-acp-')` / `tgctl-omp-reply-`
config dirs. When a test run is interrupted or teardown is skipped, those
children are reparented to launchd (ppid == 1) and sit idle at 0% CPU,
consuming hundreds of MB of RSS until someone manually hunts them down.

This is the same failure class ops/zombie-parent-reaper and
ops/vscode-orphan-reaper already catch — a different leaking source with a
distinct positive-match predicate.

SAFETY
------
Reap ONLY when ALL of the following hold (positive match, never absence):
  - ppid == 1 (owning tg-ctl daemon / test harness is provably gone)
  - elapsed >= REPARENTED_MIN_AGE_S (15 min grace)
  - %cpu == 0.0 (idle — a live wedge-simulation child still being driven
    shows non-zero CPU occasionally; leaked fixtures are fully stuck)
  - argv carries a structurally valid `/tgctl-omp-(acp|reply)-<suffix>`
    temp-dir path token (tg-cli's mkdtemp prefix convention)
  - argv is an `omp acp` invocation (token-boundary `omp` + `acp` subcommand)

A live `omp acp` child still owned by the running tg-ctl daemon has
ppid != 1 and is never a candidate.

USAGE
    reap_tgctl_omp_acp.py              # reap + report
    reap_tgctl_omp_acp.py --dry-run    # report only, kill nothing
    reap_tgctl_omp_acp.py --json       # machine-readable report on stdout

Stdlib only — no third-party deps, so it can run standalone under launchd's bare
`/usr/bin/python3` without a virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

REPARENTED_MIN_AGE_S = 15 * 60  # 15 min, only when ppid == 1

# tg-cli tests: mkdtempSync(join(tmpdir(), 'tgctl-omp-acp-')) / 'tgctl-omp-reply-'
_TGCTL_TEMP_DIR_RE = re.compile(r"/tgctl-omp-(?:acp|reply)-[A-Za-z0-9]+")

# Token-boundary `omp` executable + `acp` subcommand (not `omp --print`, not bare `omp`).
_OMP_EXE_RE = re.compile(r"(?:^|\s)(?:/[^\s]+/)?omp(?:\s|$)")
_ACP_SUBCMD_RE = re.compile(r"(?:^|\s)acp(?:\s|$)")

# pid ppid etime %cpu args — etime is NO-SPACE (`[[dd-]hh:]mm:ss`).
_PS_ROW = re.compile(r"^(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)$")


def _warn(msg: str) -> None:
    print(f"reap-tgctl-omp-acp: WARNING: {msg}", file=sys.stderr)


def _parse_bsd_elapsed_time(etime: str) -> Optional[float]:
    """Parse BSD `ps -o etime=` (`[[dd-]hh:]mm:ss`) into seconds.

    SYNC: same parser as ops/vscode-orphan-reaper/reap_vscode_orphans.py and
    ops/zombie-parent-reaper/reap_zombie_parents.py — duplicated because each
    launchd script is a standalone stdlib file with no shared import path.
    """
    etime = etime.strip()
    if not etime:
        return None
    day_part = 0.0
    if "-" in etime:
        day_s, etime = etime.split("-", 1)
        try:
            day_part = float(day_s) * 86400.0
        except ValueError:
            return None
    parts = etime.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        total = nums[0] * 3600 + nums[1] * 60 + nums[2]
    elif len(parts) == 2:
        total = nums[0] * 60 + nums[1]
    else:
        return None
    return day_part + float(total)


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    age_s: float
    cpu_pct: float
    args: str


@dataclass
class ReapReport:
    reaped: list[ProcessInfo] = field(default_factory=list)
    skipped: list[ProcessInfo] = field(default_factory=list)
    killed_pids: list[int] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "reaped": [asdict(p) for p in self.reaped],
            "reaped_count": len(self.reaped),
            "killed_pid_count": len(self.killed_pids),
            "skipped_within_grace_count": len(self.skipped),
        }


def has_tgctl_temp_dir_marker(args: str) -> bool:
    """True when argv carries a tg-cli mkdtemp `tgctl-omp-(acp|reply)-*` path."""
    return _TGCTL_TEMP_DIR_RE.search(args) is not None


def is_omp_acp_argv(args: str) -> bool:
    """True when argv is an `omp acp` invocation."""
    return _OMP_EXE_RE.search(args) is not None and _ACP_SUBCMD_RE.search(args) is not None


def should_reap_orphan(proc: ProcessInfo) -> bool:
    """Pure predicate — see module docstring SAFETY section."""
    if proc.pid <= 1 or proc.pid == os.getpid():
        return False
    if proc.ppid != 1:
        return False
    if proc.age_s < REPARENTED_MIN_AGE_S:
        return False
    if proc.cpu_pct > 0.0:
        return False
    if not has_tgctl_temp_dir_marker(proc.args):
        return False
    if not is_omp_acp_argv(proc.args):
        return False
    return True


def _run_ps(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> Optional[str]:
    try:
        proc = run(
            ["ps", "-eo", "pid=,ppid=,etime=,%cpu=,args=", "-ww"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        _warn(f"ps invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        _warn(f"ps exited {proc.returncode}: {(proc.stderr or '').strip()}")
        return None
    return proc.stdout


def _parse_ps_rows(stdout: str) -> list[ProcessInfo]:
    out: list[ProcessInfo] = []
    nonempty_line_count = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        nonempty_line_count += 1
        m = _PS_ROW.match(stripped)
        if not m:
            continue
        pid_s, ppid_s, etime_s, cpu_s, args = m.groups()
        age_s = _parse_bsd_elapsed_time(etime_s)
        if age_s is None:
            continue
        try:
            cpu_pct = float(cpu_s)
        except ValueError:
            continue
        try:
            out.append(
                ProcessInfo(
                    pid=int(pid_s),
                    ppid=int(ppid_s),
                    age_s=age_s,
                    cpu_pct=cpu_pct,
                    args=args,
                )
            )
        except ValueError:
            continue
    if nonempty_line_count and not out:
        _warn(
            f"ps produced {nonempty_line_count} non-empty line(s) but NONE matched "
            "the expected row shape (pid ppid etime %cpu args) — every candidate "
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
            ["ps", "-p", str(pid), "-o", "args=", "-ww"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    args = proc.stdout.strip()
    return args or None


def _default_kill(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def sweep(
    list_processes: Optional[Callable[[], list[ProcessInfo]]] = None,
    reread_args: Optional[Callable[[int], Optional[str]]] = None,
    kill: Optional[Callable[[int], None]] = None,
    dry_run: bool = False,
) -> ReapReport:
    list_processes = list_processes or _list_processes
    reread_args = reread_args or _reread_args
    kill = kill or _default_kill

    report = ReapReport(dry_run=dry_run)
    for info in list_processes():
        if not should_reap_orphan(info):
            if (
                info.ppid == 1
                and has_tgctl_temp_dir_marker(info.args)
                and is_omp_acp_argv(info.args)
            ):
                report.skipped.append(info)
            continue
        if dry_run:
            report.reaped.append(info)
            continue
        current_args = reread_args(info.pid)
        if current_args is None:
            continue
        candidate = ProcessInfo(
            pid=info.pid,
            ppid=info.ppid,
            age_s=info.age_s,
            cpu_pct=info.cpu_pct,
            args=current_args,
        )
        if not should_reap_orphan(candidate):
            continue
        try:
            kill(info.pid)
            report.reaped.append(info)
            report.killed_pids.append(info.pid)
        except (ProcessLookupError, PermissionError):
            continue
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, kill nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    args = parser.parse_args(argv)

    report = sweep(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        verb = "would reap" if args.dry_run else "reaped"
        if report.reaped:
            for p in report.reaped:
                print(
                    f"reap-tgctl-omp-acp: {verb} pid={p.pid} ppid={p.ppid} "
                    f"age={round(p.age_s / 60)}min cpu={p.cpu_pct}%"
                )
        print(
            f"reap-tgctl-omp-acp: {verb} {len(report.reaped)} orphan(s), "
            f"{len(report.skipped)} tgctl helper(s) still within grace"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
