"""Tests for the rig-detached-claude-code launcher — the canonical rig-owned detached-agent
launcher for Claude Code (agent-tools#831), shipped as the `rig-detached-claude-code`
universal SKILL (skills/universal/rig-detached-claude-code/) so rig provisions it to
~/.agents/skills/rig-detached-claude-code/ on every managed machine, exactly like its
rig-detached-{omp,codex,opencode} siblings.

The launcher exports RIG_AGENT_ID=<name> and RIG_DETACHED_AGENT=1 for a nohup-detached
`claude -p` child, so cc_hook_bridge restores `args.agent_id` from them and every
subagent-exempt gate classifies the child session as a dispatched subagent. These tests
NEVER launch a real Claude Code (no model call): every test that gets past validation
shadows `claude` with a stub executable that records its environment and arguments,
echoes into the launcher's log file, and sleeps long enough to prove the launcher returned
without waiting for it.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_rig_detached_claude_code.py -q
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "universal"
    / "rig-detached-claude-code"
    / "rig-detached-claude-code"
)
# Resolved: the launcher prints `pwd -P` of the log dir (/private/var/... on macOS).
_LOG_DIR = Path(tempfile.mkdtemp(prefix="rig-detached-claude-code-test-logs-")).resolve()

# Same budget as test_rig_detached_opencode.py: bash -> nohup -> bash -c -> python3 (setsid
# shim) -> stub can take >1.5s on a loaded machine; 4.5s separates "returned before the child
# finished" from scheduler noise.
_STUB_SLEEP_S = 5.0


def _stub_claude(bin_dir: Path, marker: Path) -> None:
    """A stub `claude` that records env/args/cwd (one ARG= line per argv entry), streams into
    stdout (which the launcher redirects into the agent log), and sleeps past the launcher's
    return. The record is written to a temp file and renamed so a poller never sees half."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        "{\n"
        '  echo "RIG_AGENT_ID=$RIG_AGENT_ID"\n'
        '  echo "RIG_DETACHED_AGENT=$RIG_DETACHED_AGENT"\n'
        '  echo "PWD=$PWD"\n'
        "  printf 'ARG=%s\\n' \"$@\"\n"
        "  echo \"PID=$$ PGID=$(ps -o pgid= -p $$ | tr -d ' ')\"\n"
        "  echo \"STDIN_EOF=$(head -c1 </dev/stdin | wc -c | tr -d ' ')\"\n"
        f'}} > "{marker}.tmp" && mv "{marker}.tmp" "{marker}"\n'
        'echo "stub-claude-started"\n'
        f"sleep {_STUB_SLEEP_S}\n"
        'echo "stub-claude-done"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_launcher(
    *args: str,
    path_override: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # A rig-dispatched pytest run may itself carry the markers; the launcher must set them.
    env.pop("RIG_AGENT_ID", None)
    env.pop("RIG_DETACHED_AGENT", None)
    # Isolated log dir by default: on a shared host /tmp/agent-logs may be owned by another
    # user and the launcher (correctly) refuses it.
    env.setdefault("RIG_AGENT_LOG_DIR", str(_LOG_DIR))
    if path_override is not None:
        # Stub dir FIRST so the stub shadows the real claude on PATH.
        env["PATH"] = str(path_override) + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(_LAUNCHER), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


def _poll_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    pytest.fail(f"{path} never appeared within {timeout_s}s")


def _args(recorded: str) -> list[str]:
    return [line[len("ARG="):] for line in recorded.splitlines() if line.startswith("ARG=")]


def test_launcher_is_a_tracked_executable_skill_carrier():
    """rig copies the whole skill dir; a non-executable launcher would be provisioned as a
    command that cannot run, and a SKILL.md without its name would not be catalogued."""
    assert os.access(_LAUNCHER, os.X_OK), f"{_LAUNCHER} is not executable"
    skill_md = (_LAUNCHER.parent / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.lstrip().startswith("---")
    assert "\nname: rig-detached-claude-code\n" in skill_md
    assert "~/.agents/skills/rig-detached-claude-code/rig-detached-claude-code" in skill_md


# ── argument validation → exit 2 ──────────────────────────────────────────────────────────

def test_no_args_prints_usage_and_exits_2():
    proc = _run_launcher()
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


def test_too_many_args_exits_2():
    proc = _run_launcher("a", "b", "c", "d")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


@pytest.mark.parametrize("bad", ["bad name", "../evil", "-leading", "a/b", ""])
def test_invalid_agent_name_exits_2(tmp_path, bad):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher(bad, str(brief))
    assert proc.returncode == 2
    assert "invalid agent name" in proc.stderr


def test_missing_brief_file_exits_2(tmp_path):
    proc = _run_launcher("agent-x", str(tmp_path / "nope.md"))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_empty_brief_file_exits_2(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_missing_workdir_exits_2(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief), str(tmp_path / "no-dir"))
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_claude_not_on_path_exits_127(tmp_path):
    """No `claude` resolvable → 127 BEFORE anything is detached or logged. PATH keeps the OS
    utility dirs (bash/date/mkdir/nohup) but drops every dir that can hold a real claude."""
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    log_dir = tmp_path / "agent-logs"
    proc = _run_launcher(
        "agent-x", str(brief),
        extra_env={"PATH": os.pathsep.join(["/usr/bin", "/bin"]), "RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 127
    assert "'claude' not found on PATH" in proc.stderr
    assert not (log_dir / "agent-x.log").exists()


# ── happy path with a stub claude ─────────────────────────────────────────────────────────

def test_happy_path_exports_markers_runs_print_mode_and_detaches(tmp_path):
    name = "cc831-test-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_claude(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: write ok to the handoff file and exit.", encoding="utf-8")

    started = time.monotonic()
    proc = _run_launcher(name, str(brief), str(workdir), path_override=stub_dir)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    # Detachment: the launcher returns well before the stub's sleep finishes.
    assert elapsed < _STUB_SLEEP_S - 0.5, f"launcher blocked for {elapsed:.2f}s"
    assert re.fullmatch(
        rf"detached agent '{name}' launched \(pid \d+, log {re.escape(str(log))}, "
        rf"workdir {re.escape(str(workdir))}\)",
        proc.stdout.strip(),
    ), proc.stdout

    _poll_for(log)
    assert f"launching detached agent '{name}' in '{workdir}'" in log.read_text(encoding="utf-8").splitlines()[0]

    # The child is non-interactive print mode with auto-approved tools, the brief after `--`,
    # the identity markers exported, running in the requested workdir.
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert _args(recorded) == [
        "-p", "--permission-mode", "bypassPermissions", "--",
        "Mission: write ok to the handoff file and exit.",
    ]
    assert f"RIG_AGENT_ID={name}" in recorded
    assert "RIG_DETACHED_AGENT=1" in recorded
    assert f"PWD={workdir}" in recorded
    # A real detach: the child leads its OWN session/process group, so a harness that
    # group-kills the launcher's tool call cannot take it down; its stdin is /dev/null.
    pid_line = next(l for l in recorded.splitlines() if l.startswith("PID="))
    pid, pgid = (part.split("=")[1] for part in pid_line.split())
    assert pid == pgid, pid_line
    assert "STDIN_EOF=0" in recorded

    # The child's stdout streams into the same log file.
    deadline = time.monotonic() + 10
    while "stub-claude-started" not in log.read_text(encoding="utf-8") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert "stub-claude-started" in log.read_text(encoding="utf-8")


def test_relative_brief_with_different_workdir_reaches_the_child(tmp_path, monkeypatch):
    """A RELATIVE brief path + a DIFFERENT [workdir]: the brief is validated against the
    caller's cwd, so it must also be READ there, before the cd — a post-cd read would launch
    claude with an EMPTY prompt while exiting 0 (the GH-497 P1 class)."""
    name = "cc831-test-relative-brief"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "brief.md").write_text("relative-brief-sentinel must reach the child.", encoding="utf-8")
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()

    monkeypatch.chdir(caller)
    proc = _run_launcher(name, "brief.md", str(workdir), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"PWD={workdir}" in recorded
    assert _args(recorded)[-1] == "relative-brief-sentinel must reach the child."


def test_relative_workdir_is_canonicalized_in_the_launch_line_and_the_log(tmp_path, monkeypatch):
    """A relative [workdir] is resolved once, against the caller's cwd: the child runs there
    and the launch line + log record the ABSOLUTE path, never the relative spelling."""
    name = "cc831-test-relative-workdir"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    (tmp_path / "packages" / "app").mkdir(parents=True)
    brief = tmp_path / "brief.md"
    brief.write_text("relative workdir brief", encoding="utf-8")
    log_dir = tmp_path / "agent-logs"

    monkeypatch.chdir(tmp_path)
    proc = _run_launcher(
        name, str(brief), "./packages/app", path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )

    assert proc.returncode == 0, proc.stderr
    absolute = tmp_path / "packages" / "app"
    assert proc.stdout.rstrip().endswith(f"workdir {absolute})"), proc.stdout
    _poll_for(marker)
    assert f"PWD={absolute}" in marker.read_text(encoding="utf-8")
    assert f"in '{absolute}'" in (log_dir / f"{name}.log").read_text(encoding="utf-8")


def test_log_dir_is_private_and_symlink_log_is_refused(tmp_path):
    """The log holds the brief + the child's transcript: 0700 owned dir, 0600 log, and a
    pre-planted symlink at the log path is refused instead of followed."""
    name = "cc831-test-private-log"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("private brief", encoding="utf-8")
    log_dir = tmp_path / "agent-logs"

    proc = _run_launcher(
        name, str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    log = log_dir / f"{name}.log"
    _poll_for(log)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber", encoding="utf-8")
    (log_dir / "cc831-test-planted.log").symlink_to(victim)
    proc = _run_launcher(
        "cc831-test-planted", str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert victim.read_text(encoding="utf-8") == "do not clobber"


def test_brief_starting_with_dashes_reaches_the_child_after_end_of_options(tmp_path):
    """A markdown brief starting with `---` (frontmatter) or `- ` (a bullet) is an argv entry
    starting with `-`; without `--` claude's CLI parses it as an option and the child starts
    without the brief while the launcher still exits 0."""
    name = "cc831-test-dash-brief"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("---\nmission: dash-brief-sentinel\n---\n- do the thing", encoding="utf-8")

    proc = _run_launcher(name, str(brief), str(tmp_path), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    args = _args(marker.read_text(encoding="utf-8"))
    assert args[:5] == ["-p", "--permission-mode", "bypassPermissions", "--", "---"], args
    assert "dash-brief-sentinel" in marker.read_text(encoding="utf-8")


def test_relative_log_dir_is_resolved_before_cd(tmp_path, monkeypatch):
    """A relative RIG_AGENT_LOG_DIR is owner-checked in the caller's cwd, so it must be made
    absolute before `cd "$workdir"` or the log redirect opens somewhere else."""
    name = "cc831-test-relative-logdir"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    caller = tmp_path / "caller"
    (caller / "logs").mkdir(parents=True)
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("relative log dir brief", encoding="utf-8")

    monkeypatch.chdir(caller)
    proc = _run_launcher(
        name, str(brief), str(workdir), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": "logs"},
    )

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    log = caller / "logs" / f"{name}.log"
    _poll_for(log)
    assert f"launching detached agent '{name}'" in log.read_text(encoding="utf-8")


def test_inherited_cdpath_does_not_redirect_or_corrupt_the_log_dir(tmp_path, monkeypatch):
    """An exported CDPATH containing a dir with the same child name as a relative
    RIG_AGENT_LOG_DIR must not win over the caller's cwd: bash's `cd` would jump there AND
    print the path, corrupting the `$(cd … && pwd -P)` capture (review finding, #831)."""
    name = "cc831-test-cdpath-logdir"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    caller = tmp_path / "caller"
    (caller / "logs").mkdir(parents=True)
    decoy = tmp_path / "decoy"
    (decoy / "logs").mkdir(parents=True)
    brief = tmp_path / "brief.md"
    brief.write_text("cdpath brief", encoding="utf-8")

    monkeypatch.chdir(caller)
    proc = _run_launcher(
        name, str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": "logs", "CDPATH": str(decoy)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"log {caller / 'logs' / name}.log," in proc.stdout, proc.stdout
    assert not (decoy / "logs" / f"{name}.log").exists()


def test_workdir_defaults_to_the_callers_cwd(tmp_path, monkeypatch):
    name = "cc831-test-cwd-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_claude(stub_dir, marker)
    workdir = tmp_path / "here"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("brief body", encoding="utf-8")

    monkeypatch.chdir(workdir)
    proc = _run_launcher(name, str(brief), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    assert f"PWD={workdir}" in marker.read_text(encoding="utf-8")
