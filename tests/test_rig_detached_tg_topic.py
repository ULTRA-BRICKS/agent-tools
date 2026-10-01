"""The rig-detached-{omp,codex,opencode,claude-code} launchers hand the caller's forum topic to
the detached child (agent-tools#848).

A nohup-detached child is reparented to pid 1, so `tg` cannot tie its calls (nor those of tools
that shell out to it, such as the task-cli `[task created]` notifier) to any session and they
land in General. `tg --session-topic` (tg-cli#553) prints the caller's topic id, or nothing; each
launcher asks it BEFORE detaching and exports the answer as TG_TOPIC for the child, only when it
is a topic id. With no session, no `tg`, or an unusable answer the child gets no TG_TOPIC and the
launcher still succeeds.

Every case shadows both the harness binary and `tg` with stubs on a PATH that has no other `tg`;
no real model or Telegram call is made.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_rig_detached_tg_topic.py -q
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills" / "universal"
# launcher skill dir -> the harness binary it execs
_LAUNCHERS = {
    "rig-detached-omp": "omp",
    "rig-detached-codex": "codex",
    "rig-detached-opencode": "opencode",
    "rig-detached-claude-code": "claude",
}
_LOG_DIR = Path(tempfile.mkdtemp(prefix="rig-detached-tg-topic-test-logs-")).resolve()


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _stub_harness(bin_dir: Path, binary: str, marker: Path) -> None:
    """Records whether TG_TOPIC is exported to the child and its value; the record is written
    to a temp file and renamed so a poller never reads half of it."""
    _write_exec(
        bin_dir / binary,
        "#!/bin/sh\n"
        "{\n"
        '  echo "TG_TOPIC_SET=${TG_TOPIC+yes}"\n'
        '  echo "TG_TOPIC=$TG_TOPIC"\n'
        f'}} > "{marker}.tmp" && mv "{marker}.tmp" "{marker}"\n',
    )


def _stub_tg(bin_dir: Path, stdout: str, exit_code: int = 0) -> None:
    """A stub `tg` that answers `--session-topic` with `stdout` and exits `exit_code`; it fails
    the test (exit 99 recorded by the missing marker) if called any other way."""
    _write_exec(
        bin_dir / "tg",
        "#!/bin/sh\n"
        'if [ "$1" != "--session-topic" ] || [ "$#" -ne 1 ]; then exit 99; fi\n'
        f"printf '%s' '{stdout}'\n"
        'echo "tg: noise on stderr" >&2\n'
        f"exit {exit_code}\n",
    )


def _launch(
    launcher: str, tmp_path: Path, *, tg: tuple[str, int] | None, caller_topic: str | None = None
) -> dict[str, str]:
    """Run `launcher` against stubs; `tg=None` means no `tg` on PATH at all. Returns the
    child's recorded environment."""
    binary = _LAUNCHERS[launcher]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "child-env.txt"
    _stub_harness(bin_dir, binary, marker)
    if tg is not None:
        _stub_tg(bin_dir, *tg)
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: record the env and exit.", encoding="utf-8")

    env = dict(os.environ)
    for var in ("TG_TOPIC", "RIG_AGENT_ID", "RIG_DETACHED_AGENT"):
        env.pop(var, None)
    if caller_topic is not None:
        env["TG_TOPIC"] = caller_topic
    env["RIG_AGENT_LOG_DIR"] = str(_LOG_DIR)
    # Only the stubs and the OS utility dirs: a real `tg` on the developer's PATH must not leak in.
    env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
    proc = subprocess.run(
        [str(_SKILLS / launcher / launcher), f"tgtopic848-{binary}", str(brief), str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    assert "tg:" not in proc.stderr and "not found" not in proc.stderr, proc.stderr

    deadline = time.monotonic() + 10
    while not marker.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"the stub {binary} never ran")
        time.sleep(0.05)
    return dict(line.split("=", 1) for line in marker.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_child_gets_the_callers_session_topic(launcher, tmp_path):
    recorded = _launch(launcher, tmp_path, tg=("4242", 0))
    assert recorded["TG_TOPIC_SET"] == "yes"
    assert recorded["TG_TOPIC"] == "4242"


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_no_session_topic_leaves_the_child_without_tg_topic(launcher, tmp_path):
    """`tg --session-topic` printing nothing (no session, no topic yet) → TG_TOPIC stays unset,
    not exported empty."""
    recorded = _launch(launcher, tmp_path, tg=("", 0))
    assert recorded["TG_TOPIC_SET"] == ""


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_missing_tg_leaves_the_child_without_tg_topic_and_the_launch_succeeds(launcher, tmp_path):
    recorded = _launch(launcher, tmp_path, tg=None)
    assert recorded["TG_TOPIC_SET"] == ""


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_failing_tg_leaves_the_child_without_tg_topic_and_the_launch_succeeds(launcher, tmp_path):
    recorded = _launch(launcher, tmp_path, tg=("", 3))
    assert recorded["TG_TOPIC_SET"] == ""


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_non_topic_answer_is_not_exported(launcher, tmp_path):
    """An older `tg` that does not know the flag prints usage text, not an id; that must never
    become the child's TG_TOPIC."""
    recorded = _launch(launcher, tmp_path, tg=("usage: tg [options]", 0))
    assert recorded["TG_TOPIC_SET"] == ""


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_numeric_answer_from_a_failed_tg_is_not_exported(launcher, tmp_path):
    recorded = _launch(launcher, tmp_path, tg=("4242", 3))
    assert recorded["TG_TOPIC_SET"] == ""


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_callers_own_tg_topic_passes_through_when_there_is_no_session_topic(launcher, tmp_path):
    """A TG_TOPIC the caller already exports (a grandchild launched by a detached worker, whose
    own lookup finds no session) is not clobbered or cleared."""
    recorded = _launch(launcher, tmp_path, tg=("", 0), caller_topic="777")
    assert recorded["TG_TOPIC"] == "777"


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_session_topic_wins_over_an_inherited_tg_topic(launcher, tmp_path):
    recorded = _launch(launcher, tmp_path, tg=("4242", 0), caller_topic="777")
    assert recorded["TG_TOPIC"] == "4242"
