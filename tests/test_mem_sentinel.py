"""Tests for ops/mem-sentinel/mem_sentinel.py (agent-tools#884).

The sentinel once read memory.current (which includes reclaimable page cache), steered sessions
while the box had 20-37 GB available, and told an agent to kill its own root omp. These tests
drive the real tick() against a fake /proc and cgroup tree built from a fixture memory.stat
taken from such a cache-heavy session (anon 0.5 GB, file 10.7 GB).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_mem_sentinel.py -q
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[1] / "ops" / "mem-sentinel"
_spec = importlib.util.spec_from_file_location("mem_sentinel", _DIR / "mem_sentinel.py")
assert _spec and _spec.loader
ms = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ms
_spec.loader.exec_module(ms)

GB = ms.GB
KB = 1024
PAGE = 4096
FIXTURE = _DIR / "fixtures" / "cache-heavy-scope"
TOTAL = 46 * GB
ROOT_OMP = 100
LAUNCHER = 90


class FakeSessions:
    def __init__(self, by_pid):
        self.by_pid = by_pid

    def refresh(self, now):
        pass


class Collect(list):
    path = "test"

    def __call__(self, msg, prio=None):
        self.append(msg)

    def steers(self):
        return [m for m in self if m.startswith(("steer", "would-steer"))]

    def broadcasts(self):
        return [m for m in self if m.startswith(("broadcast", "would-broadcast"))]


class Box:
    """A fake /proc + /sys/fs/cgroup with one scoped omp session."""

    def __init__(self, tmp: Path):
        self.proc = tmp / "proc"
        self.cg = tmp / "cg"
        self.proc.mkdir()
        self.scope = (self.cg / "user.slice" / f"user-{ms.CONFIG['UID']}.slice"
                      / f"user@{ms.CONFIG['UID']}.service" / "omp.slice" / "omp-100-1.scope")
        shutil.copytree(FIXTURE, self.scope)
        (self.proc / "pressure").mkdir()
        self.set_psi(0.0)
        self.set_mem(avail=21.6 * GB, swap_free_pct=0.0)
        # launcher -> root omp -> (bun test, small helper)
        self.add_proc(LAUNCHER, 1, "bash", anon=0.01 * GB)
        self.add_proc(ROOT_OMP, LAUNCHER, "omp", anon=1.4 * GB, cache=0.2 * GB)
        self.add_proc(201, ROOT_OMP, "bun", anon=0.3 * GB)
        self.add_proc(202, ROOT_OMP, "rg", anon=0.01 * GB)
        self.write_procs([LAUNCHER, ROOT_OMP, 201, 202])

    def add_proc(self, pid, ppid, comm, anon, cache=0.0):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 0\n")
        shared = int(cache // PAGE)
        resident = int(anon // PAGE) + shared
        (d / "statm").write_text(f"{resident * 2} {resident} {shared} 1 0 {resident} 0\n")

    def write_procs(self, pids):
        (self.scope / "cgroup.procs").write_text("\n".join(map(str, pids)) + "\n")

    def set_mem(self, avail, swap_free_pct=100.0):
        swap_total = 16 * GB
        (self.proc / "meminfo").write_text(
            f"MemTotal: {TOTAL // KB} kB\nMemFree: {int(avail) // KB} kB\nMemAvailable: {int(avail) // KB} kB\n"
            f"SwapTotal: {swap_total // KB} kB\nSwapFree: {int(swap_total * swap_free_pct / 100) // KB} kB\n")

    def set_anon(self, anon):
        stat = (self.scope / "memory.stat").read_text().splitlines()
        stat = [f"anon {int(anon)}" if line.startswith("anon ") else line for line in stat]
        (self.scope / "memory.stat").write_text("\n".join(stat) + "\n")

    def set_psi(self, full):
        (self.proc / "pressure" / "memory").write_text(
            f"some avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\nfull avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\n")

    def sentinel(self):
        log = Collect()
        s = ms.Sentinel(log=log, sessions=FakeSessions({ROOT_OMP: {"pid": ROOT_OMP, "cwd": "/w", "sessionId": "sid-1"}}),
                        proc_root=str(self.proc), cg_root=str(self.cg))
        s.delivered = []
        s.run_bg = lambda argv, what, **kw: s.delivered.append(argv)
        return s, log


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setitem(ms.CONFIG, "DRY_RUN", False)
    monkeypatch.setitem(ms.CONFIG, "HEARTBEAT_S", 0.0)
    monkeypatch.setattr(ms.os, "sysconf", lambda name: PAGE)
    return Box(tmp_path)


def test_footprint_from_fixture_excludes_page_cache():
    m = ms.cgroup_mem(str(FIXTURE))
    assert m["anon"] == 536870912
    assert m["shmem"] == 2097152
    assert m["swap"] == 0
    assert m["footprint"] == 536870912 + 2097152
    assert m["cache"] == 11489037107 - 2097152
    # memory.current (what the old sentinel read) is 12.1 GB; the footprint is ~0.5 GB
    assert int((FIXTURE / "memory.current").read_text()) > 20 * m["footprint"]


def test_swap_counts_toward_footprint(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "s")
    (tmp_path / "s" / "memory.swap.current").write_text(str(3 * GB))
    assert ms.cgroup_mem(str(tmp_path / "s"))["footprint"] == 536870912 + 2097152 + 3 * GB


def test_cache_heavy_session_with_high_avail_never_steers(box):
    """The #884 incident: 10.7 GB cache, MemAvailable 21.6 GB of 46, swap full, flat - zero steers."""
    s, log = box.sentinel()
    for i in range(0, 1800, 2):          # 30 min of 2 s ticks
        s.tick(now=1000.0 + i)
    assert log.steers() == []
    assert log.broadcasts() == []
    assert s.delivered == []


def test_no_steer_when_avail_high_and_flat_even_for_large_anon(box):
    """Gate: MemAvailable >= LOW_AVAIL_PCT and fall <= 0 -> nothing, whatever the session holds."""
    box.set_anon(20 * GB)
    box.set_mem(avail=0.30 * TOTAL, swap_free_pct=0.0)
    s, log = box.sentinel()
    for i in range(0, 120, 2):
        s.tick(now=1000.0 + i)
    assert log.steers() == []


def test_session_reasons_gate():
    sessions = {"k": dict(footprint=20 * GB, swap=0)}
    growths = {"k": 5 * GB}
    high = dict(avail=0.30 * TOTAL, total=TOTAL, swap_free=0, swap_total=16 * GB)
    assert ms.session_reasons(sessions, growths, fall=0.0, **high) == {}
    assert ms.session_reasons(sessions, growths, fall=-3 * GB, **high) == {}
    # a fast fall with headroom never blames a session that is not itself growing (swap full,
    # 20 GB footprint: the level reasons (a)/(d)/(e) need low MemAvailable)
    assert ms.session_reasons(sessions, {"k": 0.0}, fall=8 * GB, **high) == {}
    # falling fast opens the gate for the session that grows
    fast = ms.session_reasons(sessions, growths, fall=8 * GB, **high)
    assert any(r.startswith("(b)") for r in fast["k"])
    # low headroom opens it too
    low = dict(high, avail=0.20 * TOTAL)
    assert any(r.startswith("(a)") for r in ms.session_reasons(sessions, growths, fall=0.0, **low)["k"])


def test_low_avail_steer_names_heavy_child_not_root_and_splits_anon_cache(box):
    box.set_anon(9 * GB)
    box.add_proc(201, ROOT_OMP, "bun", anon=6 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, log = box.sentinel()
    s.tick(now=1000.0)
    [steer] = log.steers()
    argv = s.delivered[0]
    text = argv[-1]
    # exact-pid delivery: omp-steer would also match the pid as a substring of other sessions' cwd
    assert argv[:4] == ["tg-ctl", "omp-live", "send", str(ROOT_OMP)]
    assert argv[6:9] == ["--session", "sid-1", "--"]
    assert "kill 201" in text and "bun" in text
    assert f"kill {ROOT_OMP}" not in text and f"kill {LAUNCHER}" not in text
    assert f"omp pid {ROOT_OMP} " not in text.split("heavy children:")[1].split(")")[0]
    assert "anon 9.0" in text
    assert "page cache 10.7 GB not counted" in text
    assert "(a)" in steer
    assert "Why: (a) avail 15% < 25%" in text


def test_kill_advice_names_no_pid_when_only_root_is_heavy(box):
    box.set_anon(9 * GB)
    box.add_proc(ROOT_OMP, LAUNCHER, "omp", anon=8 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, log = box.sentinel()
    s.tick(now=1000.0)
    text = s.delivered[0][-1]
    assert "kill " not in text
    assert "No single child process is heavy" in text


def test_swap_reason_blames_the_session_holding_swap_not_the_largest():
    big = dict(footprint=9 * GB, swap=0)
    swapper = dict(footprint=2 * GB, swap=1.5 * GB)
    low = dict(avail=0.20 * TOTAL, total=TOTAL, swap_free=0, swap_total=16 * GB, fall=0.0)
    reasons = ms.session_reasons({"big": big, "swapper": swapper}, {"big": 0.0, "swapper": 0.0}, **low)
    assert [r for r in reasons.get("big", []) if r.startswith("(d)")] == []
    assert any(r.startswith("(d)") for r in reasons["swapper"])
    # nobody holds meaningful swap (swap filled by something outside the omp scopes): no (d) at all
    tiny = dict(footprint=2 * GB, swap=0.1 * GB)
    reasons = ms.session_reasons({"big": big, "tiny": tiny}, {"big": 0.0, "tiny": 0.0}, **low)
    assert not any(r.startswith("(d)") for rs in reasons.values() for r in rs)


def test_counter_resets_after_a_quiet_period(box):
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, log = box.sentinel()
    s.tick(now=1000.0)
    box.set_mem(avail=0.40 * TOTAL, swap_free_pct=50.0)       # pressure gone
    t = 1000.0
    while t < 1000.0 + ms.CONFIG["REPEAT_COOLDOWN_S"] + 10:
        t += 2
        s.tick(now=t)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)       # back, much later: a fresh #1
    s.tick(now=t + 2)
    assert len(s.delivered) == 2
    assert "WARNING #" not in s.delivered[1][-1]


def test_heavy_children_excludes_root_and_its_ancestors():
    procs = {1: (0, "systemd", 0, 0), 90: (1, "bash", 3 * GB, 0), 100: (90, "omp", 5 * GB, 0),
             201: (100, "bun", 2 * GB, 0), 202: (100, "rg", GB // 10, 0)}
    s = dict(pids=[90, 100, 201, 202], roots=[100])
    assert [pid for _, _, pid in ms.heavy_children(s, procs)] == [201, 202]


def test_identical_repeats_are_rate_limited(box, monkeypatch):
    box.set_anon(9 * GB)
    box.add_proc(201, ROOT_OMP, "bun", anon=6 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, log = box.sentinel()
    for i in range(0, 890, 2):           # same reasons, same footprint: one steer per REPEAT_COOLDOWN_S
        s.tick(now=1000.0 + i)
    assert len(log.steers()) == 1
    s.tick(now=1000.0 + ms.CONFIG["REPEAT_COOLDOWN_S"] + 1)
    assert len(log.steers()) == 2
    # a materially different footprint is news after the base cooldown
    box.set_anon(11 * GB)
    s.tick(now=1000.0 + ms.CONFIG["REPEAT_COOLDOWN_S"] + 1 + ms.CONFIG["STEER_COOLDOWN_S"] + 1)
    assert len(log.steers()) == 3


def test_flat_repeat_does_not_claim_still_growing(box):
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, _ = box.sentinel()
    s.tick(now=1000.0)
    s.tick(now=1000.0 + ms.CONFIG["REPEAT_COOLDOWN_S"] + 1)
    assert "WARNING #2 -" in s.delivered[1][-1]
    assert "still growing" not in s.delivered[1][-1]


def test_critical_broadcast_still_fires_near_earlyoom(box):
    box.set_mem(avail=0.08 * TOTAL, swap_free_pct=0.0)
    s, log = box.sentinel()
    s.tick(now=1000.0)
    assert len(log.broadcasts()) == 1
    assert any(d[:2] == ["omp-steer", "all"] for d in s.delivered)


def test_psi_thrash_broadcasts_even_with_headroom(box):
    box.set_psi(35.0)
    s, log = box.sentinel()
    s.tick(now=1000.0)
    assert len(log.broadcasts()) == 1
    assert log.steers() == []


def test_failed_session_discovery_keeps_the_cached_map(monkeypatch):
    """tg-ctl fails with rc 1 and empty stdout; that is not "no live sessions"."""
    log = Collect()
    sessions = ms.Sessions(log)
    sessions.by_pid = {ROOT_OMP: {"pid": ROOT_OMP, "cwd": "/w"}}
    monkeypatch.setattr(ms.subprocess, "run",
                        lambda *a, **kw: ms.subprocess.CompletedProcess(a, 1, "", "backend unavailable"))
    sessions.fetch()
    assert sessions.by_pid == {ROOT_OMP: {"pid": ROOT_OMP, "cwd": "/w"}}
    assert sessions.failed_once
    assert any("omp-live status failed" in m for m in log)


def test_nested_legacy_session_is_not_counted_or_named_by_its_parent(tmp_path, monkeypatch):
    """omp 100 launched omp 200 (both live, no scope): 200's tree belongs to 200 only."""
    monkeypatch.setattr(ms.os, "sysconf", lambda name: PAGE)
    box = Box(tmp_path)
    shutil.rmtree(box.scope)
    box.add_proc(200, ROOT_OMP, "omp", anon=2 * GB)
    box.add_proc(301, 200, "pytest", anon=1 * GB)
    s = ms.Sentinel(log=Collect(), sessions=FakeSessions({ROOT_OMP: {"pid": ROOT_OMP}, 200: {"pid": 200}}),
                    proc_root=str(box.proc), cg_root=str(box.cg))
    procs, sessions = s.sample(1000.0)
    parent, child = sessions[f"pid-{ROOT_OMP}"], sessions["pid-200"]
    assert set(parent["pids"]) == {ROOT_OMP, 201, 202}
    assert set(child["pids"]) == {200, 301}
    assert 200 not in [pid for _, _, pid in ms.heavy_children(parent, procs)]
    assert [pid for _, _, pid in ms.heavy_children(child, procs)] == [301]


def test_hung_discovery_never_blocks_a_tick(box, monkeypatch):
    """A wedged tg-ctl (20 s timeout) must not stop sampling or the critical broadcast."""
    release = ms.threading.Event()
    calls = []

    def hung_run(*a, **kw):
        calls.append(1)
        release.wait(10)
        raise ms.subprocess.TimeoutExpired(a[0], 20)

    monkeypatch.setattr(ms.subprocess, "run", hung_run)
    box.set_mem(avail=0.08 * TOTAL)
    sessions = ms.Sessions(Collect())
    log = Collect()
    s = ms.Sentinel(log=log, sessions=sessions, proc_root=str(box.proc), cg_root=str(box.cg))
    s.run_bg = lambda argv, what, **kw: None
    t0 = ms.time.monotonic()
    for i in range(0, 40, 2):                 # 40 s of ticks while discovery hangs
        s.tick(now=1000.0 + i)
    try:
        assert ms.time.monotonic() - t0 < 5
        assert len(log.broadcasts()) == 1
        assert len(calls) == 1                # no second fetch while one is in flight
    finally:
        release.set()
        sessions.running.join(5)


def test_scoped_only_tick_skips_the_proc_scan_until_a_steer_needs_it(box, monkeypatch):
    scans = []
    real = ms.proc_snapshot
    monkeypatch.setattr(ms, "proc_snapshot", lambda *a, **kw: scans.append(1) or real(*a, **kw))
    s, _ = box.sentinel()
    for i in range(0, 20, 2):
        s.tick(now=1000.0 + i)
    assert scans == []
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s.tick(now=1030.0)
    assert scans == [1]
    assert "heavy children: bun pid 201" in s.delivered[0][-1]   # the late scan still names the child


def test_first_delivery_is_not_held_back_by_a_tick_before_discovery(box):
    """Async discovery: the first ticks may see the scope before its omp is known (no recipient).
    That undeliverable warning must not start the cooldown for the real recipient."""
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, log = box.sentinel()
    known = s.sessions.by_pid
    s.sessions.by_pid = {}
    s.tick(now=1000.0)
    assert s.delivered == [] and len(log.steers()) == 1        # logged, nobody to deliver to
    s.sessions.by_pid = known
    s.tick(now=1002.0)
    assert len(s.delivered) == 1
    assert "WARNING #" not in s.delivered[0][-1]               # the session's first warning


def test_new_session_on_the_same_pid_gets_its_own_first_warning(box):
    """omp -r can reuse a pid with a new sessionId; the old session's cooldown is not the new one's."""
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, _ = box.sentinel()
    s.tick(now=1000.0)
    s.sessions.by_pid = {ROOT_OMP: {"pid": ROOT_OMP, "cwd": "/w", "sessionId": "sid-2"}}
    s.tick(now=1200.0)
    assert len(s.delivered) == 2
    assert s.delivered[1][6:8] == ["--session", "sid-2"]
    assert "WARNING #" not in s.delivered[1][-1]


def test_no_delivery_without_a_discovered_session_id(box):
    """Without --session, tg-ctl would hand the text to whatever session owns the pid now."""
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    s, log = box.sentinel()
    s.sessions.by_pid = {ROOT_OMP: {"pid": ROOT_OMP, "cwd": "/w"}}
    s.tick(now=1000.0)
    assert s.delivered == []
    assert "no live omp session" in log.steers()[0]


def test_scope_warning_goes_to_the_outermost_omp_even_with_a_smaller_nested_pid(box):
    """A nested omp stays in its parent's scope; after pid wrap its pid can be the smaller one."""
    box.set_anon(9 * GB)
    box.set_mem(avail=0.15 * TOTAL, swap_free_pct=50.0)
    box.add_proc(50, ROOT_OMP, "omp", anon=0.2 * GB)           # nested omp, smaller pid
    box.write_procs([LAUNCHER, 50, ROOT_OMP, 201, 202])
    s, _ = box.sentinel()
    s.sessions.by_pid = {ROOT_OMP: {"pid": ROOT_OMP, "sessionId": "outer"},
                         50: {"pid": 50, "sessionId": "nested"}}
    s.tick(now=1000.0)
    assert s.delivered[0][3] == str(ROOT_OMP)
    assert s.delivered[0][6:8] == ["--session", "outer"]


def test_outermost_never_falls_back_to_a_proven_nested_pid(box):
    """Launcher exits mid-walk: 100's chain breaks, 50 is still proven nested under 100."""
    box.add_proc(50, ROOT_OMP, "omp", anon=0.2 * GB)
    shutil.rmtree(box.proc / str(LAUNCHER))
    s, _ = box.sentinel()
    assert s.outermost([50, ROOT_OMP]) == ROOT_OMP
