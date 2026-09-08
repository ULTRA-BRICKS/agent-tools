"""Tests for ops/zombie-parent-reaper/reap_zombie_parents.py.

Unix zombies (STAT=Z) cannot be killed by name. Only the parent can wait(), or
init can reap them after the parent dies. A named killer (`pkill -f Raycast`)
is the wrong tool: pkill-guard blocks it, and it would hit the user's GUI.
This guard signals only an allowlisted leaky parent (Raycast Helper
(Extensions)), PID-targeted, after re-reading argv.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_reap_zombie_parents.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "zombie-parent-reaper"
    / "reap_zombie_parents.py"
)
_spec = importlib.util.spec_from_file_location("reap_zombie_parents", _SCRIPT)
assert _spec and _spec.loader
reaper = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reaper
_spec.loader.exec_module(reaper)

HELPER_ARGS = "Raycast Helper (Extensions)"
HELPER_PATH_ARGS = (
    "/Applications/Raycast.app/Contents/MacOS/Raycast Helper (Extensions)"
)
RAYCAST_APP_ARGS = "/Applications/Raycast.app/Contents/MacOS/Raycast"
CHROME_HELPER_ARGS = "Google Chrome Helper (Extensions)"


def _proc(pid, ppid, *, stat="S", age_s=None, args=HELPER_ARGS):
    if age_s is None:
        age_s = reaper.MIN_ZOMBIE_AGE_S
    return reaper.ProcessInfo(pid=pid, ppid=ppid, stat=stat, age_s=age_s, args=args)


def _zombie(pid, ppid, *, age_s=None):
    return _proc(pid, ppid, stat="Z", age_s=age_s, args="<defunct>")


def test_helper_comm_name_is_leaky():
    assert reaper.is_leaky_helper(HELPER_ARGS) is True


def test_helper_full_path_is_leaky():
    assert reaper.is_leaky_helper(HELPER_PATH_ARGS) is True


def test_raycast_gui_app_is_not_leaky():
    assert reaper.is_leaky_helper(RAYCAST_APP_ARGS) is False


def test_bare_raycast_name_is_not_leaky():
    """Named killer would match this. The guard must not."""
    assert reaper.is_leaky_helper("Raycast") is False


def test_chrome_helper_is_not_leaky():
    assert reaper.is_leaky_helper(CHROME_HELPER_ARGS) is False


def test_phrase_embedded_in_another_value_is_not_leaky():
    args = "--note=Raycast Helper (Extensions)"
    assert reaper.is_leaky_helper(args) is False


def test_empty_args_is_not_leaky():
    assert reaper.is_leaky_helper("") is False


def test_helper_with_old_zombie_is_reaped():
    parent = _proc(49954, 49498, args=HELPER_ARGS)
    kids = [_zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    assert reaper.should_reap_parent(parent, kids) is True


def test_helper_with_young_zombie_gets_grace():
    parent = _proc(49954, 49498, args=HELPER_ARGS)
    kids = [_zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S - 1)]
    assert reaper.should_reap_parent(parent, kids) is False


def test_helper_with_no_zombies_is_not_reaped():
    parent = _proc(49954, 49498, args=HELPER_ARGS)
    kids = [_proc(50084, 49954, stat="S", args="some extension")]
    assert reaper.should_reap_parent(parent, kids) is False


def test_gui_app_with_old_zombies_is_never_reaped():
    parent = _proc(49498, 1, args=RAYCAST_APP_ARGS)
    kids = [_zombie(4104, 49498, age_s=reaper.MIN_ZOMBIE_AGE_S * 10)]
    assert reaper.should_reap_parent(parent, kids) is False


def test_pid_1_is_never_reaped():
    parent = _proc(1, 0, args=HELPER_ARGS)
    kids = [_zombie(2, 1, age_s=reaper.MIN_ZOMBIE_AGE_S * 10)]
    assert reaper.should_reap_parent(parent, kids) is False


def test_one_old_zombie_among_young_is_enough():
    parent = _proc(49954, 49498, args=HELPER_ARGS)
    kids = [
        _zombie(1, 49954, age_s=1),
        _zombie(2, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S),
    ]
    assert reaper.should_reap_parent(parent, kids) is True


def test_sweep_signals_only_the_helper_pid(monkeypatch):
    helper = _proc(49954, 49498, args=HELPER_ARGS)
    gui = _proc(49498, 1, args=RAYCAST_APP_ARGS)
    procs = [
        gui,
        helper,
        _zombie(4104, 49498, age_s=reaper.MIN_ZOMBIE_AGE_S * 10),
        _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S * 10),
    ]
    killed = []

    monkeypatch.setattr(reaper, "_reread_args", lambda pid, run=None: HELPER_ARGS)
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid, sig: killed.append((pid, sig)),
        sleeper=lambda _s: None,
        pid_alive=lambda _pid: False,
    )
    assert [p.pid for p in report.reaped] == [49954]
    assert all(pid == 49954 for pid, _sig in killed)
    assert 49498 not in [pid for pid, _ in killed]
    assert 4104 not in [pid for pid, _ in killed]
    assert 50084 not in [pid for pid, _ in killed]


def test_sweep_dry_run_never_signals():
    helper = _proc(49954, 49498, args=HELPER_ARGS)
    procs = [helper, _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    killed = []
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid, sig: killed.append((pid, sig)),
        dry_run=True,
    )
    assert [p.pid for p in report.reaped] == [49954]
    assert killed == []
    assert report.killed_pids == []


def test_sweep_refuses_pid_reused_since_listing(monkeypatch):
    helper = _proc(49954, 49498, args=HELPER_ARGS)
    procs = [helper, _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_reread_args", lambda pid, run=None: RAYCAST_APP_ARGS)
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid, sig: killed.append((pid, sig)),
    )
    assert report.reaped == []
    assert killed == []


def test_sweep_skips_pid_that_exited_before_reread(monkeypatch):
    helper = _proc(49954, 49498, args=HELPER_ARGS)
    procs = [helper, _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_reread_args", lambda pid, run=None: None)
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid, sig: killed.append((pid, sig)),
    )
    assert report.reaped == []
    assert killed == []


def test_sweep_escalates_to_sigkill_if_helper_survives_sigterm(monkeypatch):
    import signal

    helper = _proc(49954, 49498, args=HELPER_ARGS)
    procs = [helper, _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_reread_args", lambda pid, run=None: HELPER_ARGS)
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid, sig: killed.append((pid, sig)),
        sleeper=lambda _s: None,
        pid_alive=lambda _pid: True,
    )
    assert killed == [(49954, signal.SIGTERM), (49954, signal.SIGKILL)]
    assert report.killed_pids == [49954]


def test_sweep_no_sigkill_when_helper_dies_on_sigterm(monkeypatch):
    import signal

    helper = _proc(49954, 49498, args=HELPER_ARGS)
    procs = [helper, _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_reread_args", lambda pid, run=None: HELPER_ARGS)
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid, sig: killed.append((pid, sig)),
        sleeper=lambda _s: None,
        pid_alive=lambda _pid: False,
    )
    assert killed == [(49954, signal.SIGTERM)]
    assert report.killed_pids == [49954]


def test_sweep_empty_process_list():
    report = reaper.sweep(list_processes=lambda: [])
    assert report.reaped == []
    assert report.killed_pids == []


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_parse_bsd_etime_days_hours_the_live_shape():
    assert reaper._parse_bsd_elapsed_time("04-03:12:20") == 4 * 86400 + 3 * 3600 + 12 * 60 + 20


def test_parse_ps_rows_reads_zombie_stat():
    stdout = (
        f"49954 49498 S  04-03:12:23 {HELPER_ARGS}\n"
        "50084 49954 Z  04-03:12:20 <defunct>\n"
    )
    rows = reaper._parse_ps_rows(stdout)
    by_pid = {p.pid: p for p in rows}
    assert by_pid[49954].stat == "S"
    assert by_pid[50084].stat == "Z"
    assert by_pid[50084].ppid == 49954
    assert by_pid[50084].args == "<defunct>"


def test_parse_ps_rows_warns_when_nothing_parses(capsys):
    rows = reaper._parse_ps_rows("garbage that is not a ps row\n")
    assert rows == []
    assert "WARNING" in capsys.readouterr().err


def test_run_ps_requests_unlimited_width():
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return _FakeCompletedProcess("")

    reaper._run_ps(run=fake_run)
    assert "-ww" in captured[0]


def test_main_dry_run_reports_without_killing(monkeypatch, capsys):
    helper = _proc(49954, 49498, args=HELPER_ARGS)
    procs = [helper, _zombie(50084, 49954, age_s=reaper.MIN_ZOMBIE_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_list_processes", lambda run=None: procs)
    monkeypatch.setattr(
        reaper, "_default_kill", lambda pid, sig: killed.append((pid, sig))
    )
    rc = reaper.main(["--dry-run"])
    assert rc == 0
    assert killed == []
    out = capsys.readouterr().out
    assert "49954" in out
