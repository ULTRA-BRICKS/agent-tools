"""End-to-end proof that ``agenttools-stall-watchdog watch`` fires under a REAL stall.

Ticket: https://git.hyperide.ai/ultrabricks/agent-tools/issues/209

Why this file exists (separate from ``test_agenttools_stall_watchdog.py``): every test in
that suite drives either ``Watchdog`` directly with a fake clock, or ``cli.run_watch`` with
``time.time``/``time.sleep`` monkeypatched to a scripted sequence. That proves the
CLASSIFICATION logic is correct, but it never proves the thing actually fires when run for
real — a real subprocess, a real wall clock, a real ``time.sleep``, a real tmux pane. A bug
in argparse wiring, in how ``main()`` invokes ``run_watch``, or in how the tmux nudge reaches
an actual pane (as opposed to a monkeypatched ``inject``) would still pass every existing
test and still leave the watchdog silently non-functional in production.

This test launches the real ``python -m agenttools_stall_watchdog watch`` CLI as a
subprocess (no monkeypatching of time, no monkeypatching of ``Watchdog``) against a real
file whose mtime has been pushed into the past, with short (2s/4s) thresholds and a real
(private-socket) tmux pane as the ``--tmux-target``. It asserts:

* the WARN tier reaches the real pane as an ENGLISH nudge line (Tier-1 is agent-facing-only
  — Alex's tg#6967 correction, see ``actions.py`` module docstring), and
* the ABORT tier makes the real subprocess exit with code 2.

How a real stall is actually simulated — the non-obvious part
-------------------------------------------------------------
Backdating the watched file's mtime proves the CLI handles the exact "old transcript" input,
but it must NOT make the first poll instantly stale. ``core.Watchdog._observe`` clamps a
PRE-EXISTING old mtime up to ``started_at`` (see core.py's ``_baseline`` clamp and the
``test_preexisting_stale_file_counts_from_watchdog_start_not_old_mtime`` unit test): a
leftover log from a previous run must not make a freshly-started watch cross ``abort_after``
on poll #1 and SIGKILL a process that just started. So staleness is measured from
watchdog-START, not from the file's last write. A genuine stall is therefore simulated by
launching the real watch process against that backdated file, then letting real wall-clock
time pass with NO further writes for longer than the scaled-down thresholds. This test does
exactly that — it really sleeps inside the subprocess's poll loop.

OPT-IN / hermeticity: like the real-tmux tests in ``test_agenttools_tmux_inject.py``, these
run only when ``ASW_REAL_TMUX_TESTS`` is set (locally or a dedicated integration job), never
in the default hermetic ``pytest tests/`` gate — a real-tmux assertion is version-coupled
and would be a flaky CI signal otherwise. Also skipped when ``tmux`` isn't on PATH. The tmux
server is bound to a PRIVATE socket (``-L <socket>``) and the watchdog subprocess is pointed
at the same socket via an ``AGENTTOOLS_TMUX_BIN`` shim, so nothing ever touches the user's
real tmux server.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "lib"
_REPO_ROOT = Path(__file__).resolve().parent.parent

_REAL_TMUX_ENV = "ASW_REAL_TMUX_TESTS"
_TMUX_CMD_TIMEOUT = 10  # seconds for a single helper tmux invocation


def _real_tmux_enabled() -> bool:
    return bool(os.environ.get(_REAL_TMUX_ENV)) and shutil.which("tmux") is not None


real_tmux = pytest.mark.skipif(
    not _real_tmux_enabled(),
    reason=f"set {_REAL_TMUX_ENV}=1 (and have tmux on PATH) to run real-tmux integration",
)


def _wait_for_pane(capture, needle: str, timeout: float = 3.0) -> str:
    """Poll ``capture()`` until ``needle`` appears or ``timeout`` elapses; return last seen."""
    deadline = time.monotonic() + timeout
    captured = ""
    while time.monotonic() < deadline:
        captured = capture()
        if needle in captured:
            return captured
        time.sleep(0.1)
    return captured


@pytest.fixture
def live_pane(tmp_path):
    """A real, throwaway tmux session on a PRIVATE socket; yields ``(session, _tmux, shim)``.

    Mirrors the ``live_pane`` fixture in ``test_agenttools_tmux_inject.py``: the pane runs a
    ``while read`` / ``printf`` echo loop (NOT a login shell), so each injected line is echoed
    back verbatim as ``GOT[<line>]`` and delivery is verifiable via ``capture-pane`` — and,
    critically, the receiving process does NOT parse the nudge as a shell command line (a raw
    zsh/bash pane would choke on the nudge's ``[stall-watchdog]`` brackets; see ticket #210).
    The server is bound to a private ``-L <socket>`` and killed on exit, so the user's real
    tmux is never touched. ``shim`` is a tiny ``tmux`` wrapper that scopes any
    ``tmux send-keys`` to this same private socket; the watchdog subprocess is pointed at it
    via ``AGENTTOOLS_TMUX_BIN`` so ``inject()`` inside the subprocess reaches THIS pane.
    """
    tmux = shutil.which("tmux")
    socket = f"asw-e2e-{uuid.uuid4().hex[:8]}"
    session = "live"

    def _tmux(*args, check=True):
        return subprocess.run(
            [tmux, "-L", socket, *args],
            capture_output=True,
            text=True,
            timeout=_TMUX_CMD_TIMEOUT,
            check=check,
        )

    shim = tmp_path / "tmux"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(tmux)} -L {shlex.quote(socket)} "$@"\n')
    shim.chmod(0o755)

    _tmux(
        "new-session", "-d", "-s", session, "-x", "200", "-y", "50",
        "sh", "-c", 'while IFS= read -r x; do printf "GOT[%s]\\n" "$x"; done',
    )
    try:
        yield session, _tmux, shim
    finally:
        _tmux("kill-server", check=False)


def _subprocess_env(shim: Path) -> dict:
    """The watchdog subprocess's env: lib on PYTHONPATH + the tmux shim as AGENTTOOLS_TMUX_BIN.

    Setting ``AGENTTOOLS_TMUX_BIN`` explicitly (rather than inheriting whatever the outer env
    happens to have) closes the review finding that the subprocess could otherwise send to a
    DIFFERENT tmux binary/socket than the fixture captures from — here both are the private
    socket via ``shim``.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_LIB) + (os.pathsep + existing if existing else "")
    env["AGENTTOOLS_TMUX_BIN"] = str(shim)
    return env


@real_tmux
def test_real_cli_subprocess_fires_warn_nudge_and_aborts_on_a_real_stall(tmp_path, live_pane):
    """The load-bearing proof: real subprocess, real clock, real tmux pane, real stall."""
    session, _tmux, shim = live_pane

    transcript = tmp_path / "agent-e2e.jsonl"
    transcript.write_text("hello\n")
    # Old enough to be unquestionably backdated, while still expecting Watchdog to clamp the
    # first observation to process start rather than instantly crossing the abort threshold.
    old_mtime = time.time() - 3600
    os.utime(transcript, (old_mtime, old_mtime))
    assert transcript.stat().st_mtime < time.time() - 300

    warn_after = 2.0
    abort_after = 4.0

    argv = [
        sys.executable,
        "-m",
        "agenttools_stall_watchdog",
        "watch",
        "--watch-file",
        str(transcript),
        "--warn-after",
        str(warn_after),
        "--abort-after",
        str(abort_after),
        "--poll-interval",
        "1",
        "--tmux-target",
        session,
        "--no-tg",  # no real Telegram send in a test
        "--test",  # mandatory marker per actions.py's own contract for any live-delivery test
    ]

    # No `--pid`: this run doesn't need a real process to kill to prove tier delivery; the
    # subprocess's OWN exit code (2 on abort) is the abort-side proof instead. The watch loop
    # really sleeps through two poll intervals (past 2s -> WARN, past 4s -> ABORT).
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        cwd=_REPO_ROOT,
        env=_subprocess_env(shim),
        capture_output=True,
        text=True,
        timeout=30,
    )
    duration = time.monotonic() - started

    # --- Tier-2/ABORT: the real process must actually exit 2 -----------------------------
    assert proc.returncode == 2, (
        f"watchdog did not abort on a real stall past abort_after={abort_after}s; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert duration >= abort_after, (
        "backdated pre-existing mtime caused an immediate abort instead of being clamped to "
        f"watch start; duration={duration:.2f}s stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "ABORT" in proc.stdout, proc.stdout
    assert "WARN" in proc.stdout, "the WARN tier must fire before the ABORT tier does"

    # --- Tier-1/WARN: the real tmux pane must have received the ENGLISH *warn* nudge ------
    # Assert the WARN nudge SPECIFICALLY (not "warn OR abort"): a regression that wrapped
    # tmux_nudge in abort-only delivery — dropping the tier-1 nudge this whole tier exists
    # for — must fail here, so matching only "ABORTED" would hide exactly that bug (review
    # finding). The echo loop prefixes each delivered line with GOT[...].
    pane_text = _wait_for_pane(
        lambda: _tmux("capture-pane", "-t", session, "-p").stdout, "stall warning"
    )
    assert "GOT[" in pane_text, f"no nudge reached the real tmux pane; pane:\n{pane_text}"
    assert "stall warning" in pane_text, (
        f"the tier-1 WARN nudge never reached the pane; pane:\n{pane_text}"
    )
    # Alex's tg#6967 correction: the tmux nudge is agent-facing and MUST stay English
    # regardless of session language — only the (here disabled) tg alert is Russian.
    for russian in ("убит", "остановлен", "ПРЕДУПРЕЖДЕНИЕ", "ОСТАНОВКА"):
        assert russian not in pane_text, f"unexpected Russian in agent-facing nudge: {russian}"


@real_tmux
def test_real_cli_subprocess_does_not_fire_while_fresh(tmp_path, live_pane):
    """Negative control: a transcript touched just now must NOT trigger a WARN within
    ``--once``'s single poll — proves the positive test's failure mode isn't "always fires".
    """
    session, _tmux, shim = live_pane

    transcript = tmp_path / "agent-fresh.jsonl"
    transcript.write_text("hello\n")  # mtime = now

    argv = [
        sys.executable,
        "-m",
        "agenttools_stall_watchdog",
        "watch",
        "--watch-file",
        str(transcript),
        "--warn-after",
        "300",
        "--abort-after",
        "1800",
        "--once",
        "--tmux-target",
        session,
        "--no-tg",
    ]
    proc = subprocess.run(
        argv,
        cwd=_REPO_ROOT,
        env=_subprocess_env(shim),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == ""
