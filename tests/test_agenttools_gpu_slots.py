"""Tests for agenttools_gpu_slots — the machine-wide GPU-model slot pool (agent-tools#817).

Every holder here is a REAL separate process (the wrapper execs a tiny fake "model" script),
because the property under test is cross-process: flock lock files shared by every agent and
harness on the machine, released by the kernel when a holder dies. Nothing loads a real model or
touches the GPU — the fake model only records when it ran and sleeps. Each test points the pool at
its own ``tmp_path`` via ``RIG_GPU_SLOT_DIR`` so nothing touches the developer's real slots.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_gpu_slots.py -q
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_gpu_slots as slots  # noqa: E402

_PKG = _LIB / "agenttools_gpu_slots"

# A stand-in for whisper-cli: logs "<start|end> <pid> <clock>" around a sleep, or prints
# "ready" and sleeps long enough to be killed. The clock is CLOCK_MONOTONIC read directly:
# system-wide on macOS and Linux, so timestamps from separate processes compare.
# (`time.monotonic()` is not always: macOS Python 3.9 counts it from process start.)
_FAKE_MODEL = """\
import os, sys, time
log, hold = sys.argv[1], float(sys.argv[2])
with open(log, "a") as f:
    f.write(f"start {os.getpid()} {time.clock_gettime(time.CLOCK_MONOTONIC)}\\n")
print("ready", flush=True)
time.sleep(hold)
with open(log, "a") as f:
    f.write(f"end {os.getpid()} {time.clock_gettime(time.CLOCK_MONOTONIC)}\\n")
"""


@pytest.fixture
def pool(tmp_path, monkeypatch):
    slot_dir = tmp_path / "slots"
    monkeypatch.setenv("RIG_GPU_SLOT_DIR", str(slot_dir))
    monkeypatch.setenv("RIG_GPU_SLOTS", "2")
    monkeypatch.delenv("RIG_GPU_SLOT_FD", raising=False)
    fake = tmp_path / "fake_model.py"
    fake.write_text(_FAKE_MODEL)
    return slot_dir, fake, tmp_path / "events.log"


def _wrapper(*args: str) -> list[str]:
    """Invoke the wrapper exactly as documented: run the package directory as a script."""
    return [sys.executable, str(_PKG), *args]


def _max_overlap(log: Path) -> tuple[int, int]:
    """(max concurrently running, number of completed runs) from the start/end event log."""
    events = []
    for line in log.read_text().splitlines():
        kind, _pid, ts = line.split()
        # At an identical timestamp an end sorts before a start: the ender's slot was released
        # before the starter could have acquired it.
        events.append((float(ts), 0 if kind == "end" else 1))
    running = peak = 0
    for _ts, delta in sorted(events):
        running += 1 if delta else -1
        peak = max(peak, running)
    return peak, sum(1 for _ts, d in events if d == 0)


def test_five_concurrent_holders_never_exceed_two_running(pool):
    _slot_dir, fake, log = pool
    procs = [
        subprocess.Popen(
            _wrapper("run", "--", sys.executable, str(fake), str(log), "1.0"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for _ in range(5)
    ]
    for p in procs:
        _out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()

    peak, completed = _max_overlap(log)
    assert completed == 5
    assert peak <= 2
    # The pool is a limit, not a serializer: two slots really do run two models at once.
    assert peak == 2


def _start_holder(fake: Path, log: Path, hold: str = "60") -> subprocess.Popen:
    proc = subprocess.Popen(
        _wrapper("run", "--", sys.executable, str(fake), str(log), hold),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"ready"
    return proc


def test_sigkilled_holder_frees_its_slot_at_once(pool, monkeypatch):
    slot_dir, fake, log = pool
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    holder = _start_holder(fake, log)
    try:
        assert slots.free_slot_count(slot_dir, 1) == 0
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        # No retry loop, no reaper, no timeout: the kernel dropped the flock when the process died.
        assert slots.free_slot_count(slot_dir, 1) == 1
        slot = slots.try_acquire(slot_dir, 1)
        assert slot is not None
        slot.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_wrapper_waits_for_a_killed_holders_slot_instead_of_deadlocking(pool, monkeypatch):
    _slot_dir, fake, log = pool
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    holder = _start_holder(fake, log)
    waiter = subprocess.Popen(
        _wrapper("run", "--timeout", "30", "--", sys.executable, str(fake), str(log), "0"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        assert waiter.poll() is None, "the waiter must wait while the only slot is held"
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        out, err = waiter.communicate(timeout=10)
        assert waiter.returncode == 0, err.decode()
        assert b"ready" in out
    finally:
        for p in (holder, waiter):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_wrapper_gives_up_after_its_timeout_with_the_tempfail_code(pool, monkeypatch):
    _slot_dir, fake, log = pool
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    holder = _start_holder(fake, log)
    try:
        started = time.monotonic()
        res = subprocess.run(
            _wrapper("run", "--timeout", "0.5", "--", sys.executable, str(fake), str(log), "0"),
            capture_output=True,
            timeout=30,
        )
        elapsed = time.monotonic() - started
        assert res.returncode == slots.EXIT_SLOT_TIMEOUT
        assert b"ready" not in res.stdout, "the model command must never start without a slot"
        assert "slot" in res.stderr.decode().lower()
        assert elapsed < 10
    finally:
        holder.kill()
        holder.wait()


def test_nested_wrapper_reuses_the_inherited_slot_instead_of_deadlocking(pool, monkeypatch):
    """stt run under the wrapper takes a slot itself; with one slot, a second acquire from the
    same holder tree would wait on its own parent forever."""
    _slot_dir, fake, log = pool
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    res = subprocess.run(
        _wrapper(
            "run", "--timeout", "2", "--",
            *_wrapper("run", "--timeout", "2", "--", sys.executable, str(fake), str(log), "0"),
        ),
        capture_output=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr.decode()
    assert b"ready" in res.stdout


def test_stale_slot_fd_env_naming_an_unrelated_fd_does_not_bypass_the_pool(pool, monkeypatch, tmp_path):
    _slot_dir, fake, log = pool
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    holder = _start_holder(fake, log)
    unrelated = open(tmp_path / "unrelated", "w")  # noqa: SIM115 — fd must outlive the with-less call
    try:
        os.set_inheritable(unrelated.fileno(), True)
        env = dict(os.environ, RIG_GPU_SLOT_FD=str(unrelated.fileno()))
        res = subprocess.run(
            _wrapper("run", "--timeout", "0.3", "--", sys.executable, str(fake), str(log), "0"),
            capture_output=True,
            timeout=30,
            env=env,
            pass_fds=(unrelated.fileno(),),
        )
        assert res.returncode == slots.EXIT_SLOT_TIMEOUT
        assert b"ready" not in res.stdout
    finally:
        unrelated.close()
        holder.kill()
        holder.wait()


def test_probe_does_not_create_the_slot_dir_or_hold_a_slot(tmp_path):
    slot_dir = tmp_path / "never-created"
    assert slots.free_slot_count(slot_dir, 2) == 2
    assert not slot_dir.exists()


@pytest.mark.parametrize(("raw", "expected"), [(None, 2), ("3", 3), ("1", 1), ("0", 2), ("-4", 2), ("two", 2)])
def test_slot_count_default_and_env_override(raw, expected):
    env = {} if raw is None else {"RIG_GPU_SLOTS": raw}
    assert slots.slot_count(env) == expected


def test_slot_dir_default_is_under_the_users_state_dir(tmp_path):
    assert slots.slot_dir({"HOME": str(tmp_path)}) == tmp_path / ".local" / "state" / "agent-tools" / "gpu-slots"
    assert slots.slot_dir({"HOME": str(tmp_path), "RIG_GPU_SLOT_DIR": "/x/y"}) == Path("/x/y")


def test_wrapper_reports_a_missing_command_with_127(pool):
    res = subprocess.run(_wrapper("run", "--", "definitely-not-a-real-binary-817"), capture_output=True, timeout=30)
    assert res.returncode == 127
    assert "definitely-not-a-real-binary-817" in res.stderr.decode()
