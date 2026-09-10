"""Tests for ops/tgctl-omp-acp-reaper/reap_tgctl_omp_acp.py.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_reap_tgctl_omp_acp.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "tgctl-omp-acp-reaper"
    / "reap_tgctl_omp_acp.py"
)
_spec = importlib.util.spec_from_file_location("reap_tgctl_omp_acp", _SCRIPT)
assert _spec and _spec.loader
reaper = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reaper
_spec.loader.exec_module(reaper)

LEAKED_ARGS = (
    "/var/folders/xx/tgctl-omp-acp-AbCdEf/bin/omp acp --allow-home "
    "--cwd=/Users/ultra/xp/tg-cli"
)
LIVE_DAEMON_CHILD_ARGS = (
    "/opt/homebrew/bin/omp acp --allow-home --cwd=/Users/ultra/xp/tg-cli"
)
REPLY_LEAKED_ARGS = (
    "/tmp/tgctl-omp-reply-Zz99Yy/bin/omp acp --no-session --allow-home "
    "--cwd=/Users/ultra/xp/tg-cli"
)


def _proc(pid, ppid, *, age_s=None, cpu_pct=0.0, args=LEAKED_ARGS):
    if age_s is None:
        age_s = reaper.REPARENTED_MIN_AGE_S
    return reaper.ProcessInfo(pid=pid, ppid=ppid, age_s=age_s, cpu_pct=cpu_pct, args=args)


def test_has_tgctl_temp_dir_marker_acp():
    assert reaper.has_tgctl_temp_dir_marker(LEAKED_ARGS) is True


def test_has_tgctl_temp_dir_marker_reply():
    assert reaper.has_tgctl_temp_dir_marker(REPLY_LEAKED_ARGS) is True


def test_has_tgctl_temp_dir_marker_rejects_unrelated():
    assert reaper.has_tgctl_temp_dir_marker(LIVE_DAEMON_CHILD_ARGS) is False
    assert reaper.has_tgctl_temp_dir_marker("tgctl-omp-acp-without-slash") is False
    assert reaper.has_tgctl_temp_dir_marker("/tgctl-omp-anchor-foo/bin/omp") is False


def test_is_omp_acp_argv():
    assert reaper.is_omp_acp_argv(LEAKED_ARGS) is True
    assert reaper.is_omp_acp_argv("/opt/homebrew/bin/omp --print hi") is False
    assert reaper.is_omp_acp_argv("/opt/homebrew/bin/omp") is False


def test_orphaned_leaked_helper_is_reaped():
    proc = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S)
    assert reaper.should_reap_orphan(proc) is True


def test_live_daemon_child_not_reaped():
    """ppid is the running tg-ctl daemon — must NOT match."""
    proc = _proc(825, 7103, age_s=reaper.REPARENTED_MIN_AGE_S, args=LIVE_DAEMON_CHILD_ARGS)
    assert reaper.should_reap_orphan(proc) is False


def test_young_orphan_within_grace():
    proc = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S - 1)
    assert reaper.should_reap_orphan(proc) is False


def test_nonzero_cpu_within_grace():
    proc = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S, cpu_pct=0.1)
    assert reaper.should_reap_orphan(proc) is False


def test_missing_temp_dir_marker_not_reaped():
    proc = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S, args=LIVE_DAEMON_CHILD_ARGS)
    assert reaper.should_reap_orphan(proc) is False


def test_sweep_dry_run_lists_candidate():
    leaked = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S)
    live = _proc(825, 7103, age_s=reaper.REPARENTED_MIN_AGE_S, args=LIVE_DAEMON_CHILD_ARGS)
    report = reaper.sweep(list_processes=lambda: [leaked, live], dry_run=True)
    assert len(report.reaped) == 1
    assert report.reaped[0].pid == 99123


def test_sweep_kills_after_reread_verification():
    leaked = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S)
    killed: list[int] = []

    def fake_kill(pid: int) -> None:
        killed.append(pid)

    report = reaper.sweep(
        list_processes=lambda: [leaked],
        reread_args=lambda pid: LEAKED_ARGS,
        kill=fake_kill,
    )
    assert killed == [99123]
    assert report.killed_pids == [99123]


def test_sweep_skips_when_reread_args_changed():
    leaked = _proc(99123, 1, age_s=reaper.REPARENTED_MIN_AGE_S)
    killed: list[int] = []

    report = reaper.sweep(
        list_processes=lambda: [leaked],
        reread_args=lambda pid: LIVE_DAEMON_CHILD_ARGS,
        kill=lambda pid: killed.append(pid),
    )
    assert killed == []
    assert report.reaped == []


def test_parse_bsd_elapsed_time_mm_ss():
    assert reaper._parse_bsd_elapsed_time("20:15") == 20 * 60 + 15
