"""Tests for the rig-detached-<harness> launchers — the canonical rig-owned detached-agent
launchers for opencode (#476), codex and omp (#573), shipped as the `rig-detached-opencode` /
`rig-detached-codex` / `rig-detached-omp` universal SKILLS (thin wrappers) over the shared
`rig-detached-agent` core skill, so rig provisions them to ~/.agents/skills/<skill>/ on every
managed machine (PR #497: `bin/` is not a rig-discovered carrier).

Every launcher exports RIG_AGENT_ID=<name> and RIG_DETACHED_AGENT=1 for a nohup-detached
child (`opencode run` / `codex exec` / `omp -p`), so that harness's hook bridge classifies the
child session as a dispatched subagent on every tool call. These tests NEVER launch a real
harness: the happy path shadows the harness binary with a stub executable that records its
environment and arguments, echoes into the launcher's log file, and sleeps long enough to
prove the launcher returned without waiting for it. Every test runs once per harness.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_rig_detached_launchers.py -q
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills" / "universal"
_HARNESSES = ("opencode", "codex", "omp")
# The per-harness child argv the core must produce, up to (and including) the `--` that
# precedes the brief (see the core's header for why each flag is there).
_EXPECTED_CHILD_ARGV = {
    "opencode": "run --title {name} --",
    "codex": "exec --dangerously-bypass-hook-trust --skip-git-repo-check --",
    "omp": "-p --auto-approve --",
}


def _launcher(harness: str) -> Path:
    return _SKILLS / f"rig-detached-{harness}" / f"rig-detached-{harness}"


@pytest.fixture(params=_HARNESSES)
def harness(request) -> str:
    return request.param


_LAUNCHER = _launcher("opencode")  # the module-level fixture below chmods every launcher
_ISOLATED_LOG_DIR = Path(tempfile.mkdtemp(prefix="rig-detached-opencode-test-logs-"))
_LOG_DIR = _ISOLATED_LOG_DIR

# The detach assertion is `launcher elapsed < _STUB_SLEEP_S - 0.5`. The launcher now
# spawns bash -> nohup -> bash -c -> python3 (setsid shim) -> stub, which under a loaded
# machine (a full suite running next to it) can take >1.5s; a 4.5s budget separates "the
# launcher returned before the child finished" from scheduler noise (flaked once at 2.0).
_STUB_SLEEP_S = 5.0


@pytest.fixture(scope="module", autouse=True)
def _launcher_executable():
    for path in [_launcher(h) for h in _HARNESSES] + [_SKILLS / "rig-detached-agent" / "rig-detached-agent"]:
        if not os.access(path, os.X_OK):
            path.chmod(0o755)


def _stub_harness(harness: str, bin_dir: Path, marker: Path) -> None:
    """A stub harness binary that records env/args/cwd, streams into stdout (which the
    launcher redirects into the agent log), and sleeps past the launcher's return."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / harness
    stub.write_text(
        "#!/bin/sh\n"
        "{\n"
        f'  echo "RIG_AGENT_ID=$RIG_AGENT_ID"\n'
        f'  echo "RIG_DETACHED_AGENT=$RIG_DETACHED_AGENT"\n'
        f'  echo "PWD=$PWD"\n'
        f'  echo "ARGS=$*"\n'
        f'  echo "PID=$$ PGID=$(ps -o pgid= -p $$ | tr -d \' \')"\n'
        f'  echo "STDIN_EOF=$(head -c1 </dev/stdin | wc -c | tr -d \' \')"\n'
        # Write the record to a temp file and rename it into place: `_poll_for(marker)`
        # returns the moment the path EXISTS, and the `$(ps ...)`/`$(head ...)` lines
        # above take real time under load — a reader saw a half-written record once.
        f'}} > "{marker}.tmp" && mv "{marker}.tmp" "{marker}"\n'
        'echo "stub-harness-started"\n'
        f"sleep {_STUB_SLEEP_S}\n"
        'echo "stub-harness-done"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_launcher(
    harness: str,
    *args: str,
    path_override: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Isolate the log dir by default: on a shared host /tmp/agent-logs may be owned by
    # another user and the launcher (correctly) refuses it. Tests that want the real
    # default pass RIG_AGENT_LOG_DIR explicitly.
    env.setdefault("RIG_AGENT_LOG_DIR", str(_ISOLATED_LOG_DIR))
    if path_override is not None:
        # Stub dir FIRST so the stub shadows any real harness binary on PATH.
        env["PATH"] = str(path_override) + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(_launcher(harness)), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _poll_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    pytest.fail(f"{path} never appeared within {timeout_s}s")


# ── argument validation → exit 2 ──────────────────────────────────────────────────────────

def test_no_args_prints_usage_and_exits_2(harness):
    proc = _run_launcher(harness)
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


def test_too_many_args_exits_2(harness):
    proc = _run_launcher(harness, "a", "b", "c", "d")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


@pytest.mark.parametrize("bad", ["bad name", "../evil", "-leading", "a/b", ""])
def test_invalid_agent_name_exits_2(harness, tmp_path, bad):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher(harness, bad, str(brief))
    assert proc.returncode == 2
    assert "invalid agent name" in proc.stderr


def test_missing_brief_file_exits_2(harness, tmp_path):
    proc = _run_launcher(harness, "agent-x", str(tmp_path / "nope.md"))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_empty_brief_file_exits_2(harness, tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("", encoding="utf-8")
    proc = _run_launcher(harness, "agent-x", str(brief))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_missing_workdir_exits_2(harness, tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher(harness, "agent-x", str(brief), str(tmp_path / "no-dir"))
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_oversized_brief_exits_2_before_detaching(harness, tmp_path):
    """The brief is one argv element; past the per-argument limit the backgrounded exec would
    fail with E2BIG after "launched" was already printed (codex P1, #573 round 2). The launcher
    refuses up front: exit 2, no launch line, no child."""
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("x" * 100_001, encoding="utf-8")
    proc = _run_launcher(harness, "agent-x", str(brief), str(tmp_path), path_override=stub_dir)
    assert proc.returncode == 2
    assert "100001 bytes" in proc.stderr and "limit" in proc.stderr
    assert "launched" not in proc.stdout
    time.sleep(0.3)
    assert not marker.exists() and not (tmp_path / "stub-env.txt.tmp").exists()


def test_brief_plus_huge_inherited_env_exits_2_before_detaching(harness, tmp_path):
    """ARG_MAX bounds argv AND environment together (codex P1, #573 round 3): a caller whose
    environment nearly fills it can start the launcher yet lose the detached child to E2BIG.
    Pad the environment (in 64 KiB slices — Linux caps one string at 128 KiB) to within 30 KB
    of ARG_MAX, then a 60 KB brief must be refused up front."""
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("x" * 60_000, encoding="utf-8")
    arg_max = os.sysconf("SC_ARG_MAX")
    env_now = sum(len(k) + len(v) + 2 for k, v in os.environ.items())
    pad_total = arg_max - env_now - 30_000
    assert pad_total > 0
    slice_len = 64 * 1024
    pad = {}
    remaining = pad_total
    while remaining > 0:
        pad[f"RIG_TEST_ENV_PAD_{len(pad)}"] = "p" * min(slice_len, remaining)
        remaining -= slice_len + 20
    proc = _run_launcher(harness, "agent-x", str(brief), str(tmp_path),
                         path_override=stub_dir, extra_env=pad)
    assert proc.returncode == 2, proc.stderr
    assert "ARG_MAX" in proc.stderr
    assert "launched" not in proc.stdout
    time.sleep(0.3)
    assert not marker.exists()


def test_brief_at_the_limit_still_launches(harness, tmp_path):
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("x" * 100_000, encoding="utf-8")
    proc = _run_launcher(harness, "agent-x", str(brief), str(tmp_path), path_override=stub_dir)
    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)


def test_harness_not_on_path_exits_127(harness, tmp_path, monkeypatch):
    """No harness binary resolvable at all → 127 (mirrors the shell's not-found code).
    PATH keeps the OS utility dirs (bash/date/mkdir/nohup for the launcher's shebang and
    body) but drops every dir that can hold a real harness (homebrew, npm globals)."""
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher(harness, "agent-x", str(brief))
    assert proc.returncode == 127
    assert "not found on PATH" in proc.stderr


# ── happy path with a stub harness binary ───────────────────────────────────────────────────────

def test_happy_path_exports_markers_and_detaches(harness, tmp_path):
    name = "oc476-test-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_harness(harness, stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: write ok to the handoff file and exit.", encoding="utf-8")

    started = time.monotonic()
    proc = _run_launcher(harness, name, str(brief), str(workdir), path_override=stub_dir)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    # Detachment: the launcher must return well before the stub's sleep finishes.
    assert elapsed < _STUB_SLEEP_S - 0.5, f"launcher blocked for {elapsed:.2f}s"
    assert f"detached agent '{name}' launched (pid" in proc.stdout
    assert str(log) in proc.stdout

    # The launcher appended its own launch line to the log immediately.
    _poll_for(log)
    first_line = log.read_text(encoding="utf-8").splitlines()[0]
    assert f"launching detached agent '{name}'" in first_line

    # The child ran with the identity markers exported, in the requested workdir,
    # with the brief's content as the child's message.
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"RIG_AGENT_ID={name}" in recorded
    assert "RIG_DETACHED_AGENT=1" in recorded
    assert f"PWD={workdir}" in recorded
    args_line = next(l for l in recorded.splitlines() if l.startswith("ARGS="))
    assert _EXPECTED_CHILD_ARGV[harness].format(name=name) in args_line, args_line
    assert "write ok to the handoff file" in recorded
    # A real detach: the child leads its OWN session/process group (pgid == its pid), so a
    # harness that group-kills the launcher's tool call cannot take it down; and its stdin
    # is /dev/null (a read returns EOF immediately instead of blocking on the caller's pipe).
    pid_line = next(l for l in recorded.splitlines() if l.startswith("PID="))
    pid, pgid = (part.split("=")[1] for part in pid_line.split())
    assert pid == pgid, pid_line
    assert "STDIN_EOF=0" in recorded

    # The child's stdout streams into the same log file.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "stub-harness-started" in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)
    assert "stub-harness-started" in log.read_text(encoding="utf-8")


def test_relative_brief_with_different_workdir_reaches_the_child(harness, tmp_path, monkeypatch):
    """A RELATIVE brief path + a DIFFERENT [workdir] must still deliver the brief's
    content to the child (codex P1, PR #497).

    Validation (`[ -s "$brief" ]`) ran against the caller's cwd, but the launcher then
    `cd`s to workdir; a `$(cat "$brief")` evaluated AFTER the cd resolved the relative
    path in the wrong directory, failed silently inside the backgrounded command
    substitution, and launched the child with an EMPTY prompt while exiting 0. The
    launcher now reads the brief before changing directory."""
    name = "oc476-test-relative-brief"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)

    caller_cwd = tmp_path / "caller"
    caller_cwd.mkdir()
    (caller_cwd / "brief.md").write_text(
        "Mission: relative-brief-sentinel must reach the child.", encoding="utf-8"
    )
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    # Not in workdir: a post-cd `cat brief.md` would fail there.
    assert not (workdir / "brief.md").exists()

    monkeypatch.chdir(caller_cwd)
    proc = _run_launcher(harness, name, "brief.md", str(workdir), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"PWD={workdir}" in recorded
    assert "relative-brief-sentinel must reach the child" in recorded


def test_log_dir_is_private_and_symlink_log_is_refused(harness, tmp_path):
    """Review finding (GH-497 round 1): the log holds the brief + the child's transcript.
    The launcher must create a 0700 log dir it owns, write a 0600 log, and refuse a
    pre-planted symlink at the log path instead of following it."""
    name = "oc476-test-private-log"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("private brief", encoding="utf-8")
    log_dir = tmp_path / "agent-logs"

    proc = _run_launcher(harness,
        name, str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    log = log_dir / f"{name}.log"
    _poll_for(log)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    # a symlink planted at the log path of ANOTHER agent name must be refused, not followed
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber", encoding="utf-8")
    planted = log_dir / "oc476-test-planted.log"
    planted.symlink_to(victim)
    proc = _run_launcher(harness,
        "oc476-test-planted", str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert victim.read_text(encoding="utf-8") == "do not clobber"


def test_brief_starting_with_dashes_reaches_the_child_verbatim(harness, tmp_path):
    """A markdown brief that starts with `---` (frontmatter) or `- ` (a bullet) is an argv
    entry starting with `-`; without `--` the harness CLI (opencode's yargs, codex's clap, omp's oclif) eats it as an option and the
    child starts with an EMPTY message (review finding, GH-497 round 2)."""
    name = "oc476-test-dash-brief"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("---\nmission: dash-brief-sentinel\n---\n- do the thing", encoding="utf-8")

    proc = _run_launcher(harness, name, str(brief), str(tmp_path), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    args_line = next(l for l in recorded.splitlines() if l.startswith("ARGS="))
    assert " -- ---" in args_line, args_line
    # the brief is multi-line, so the sentinel lands on a later line of the same record
    assert "dash-brief-sentinel" in recorded


def test_relative_log_dir_is_resolved_before_cd(harness, tmp_path, monkeypatch):
    """A relative RIG_AGENT_LOG_DIR was owner-checked in the caller's cwd but opened after
    `cd "$workdir"`, so the log redirect failed silently and no child launched while the
    launcher exited 0 (review finding, GH-497 round 2)."""
    name = "oc476-test-relative-logdir"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "logs").mkdir()
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("relative log dir brief", encoding="utf-8")

    monkeypatch.chdir(caller)
    proc = _run_launcher(harness,
        name, str(brief), str(workdir), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": "logs"},
    )

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    log = caller / "logs" / f"{name}.log"
    _poll_for(log)
    assert f"launching detached agent '{name}'" in log.read_text(encoding="utf-8")


def test_happy_path_defaults_workdir_to_cwd(harness, tmp_path, monkeypatch):
    name = "oc476-test-cwd-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_harness(harness, stub_dir, marker)
    workdir = tmp_path / "here"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("brief body", encoding="utf-8")

    monkeypatch.chdir(workdir)
    proc = _run_launcher(harness, name, str(brief), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"PWD={workdir}" in recorded


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))


# ── wrapper ↔ core contract (#573) ────────────────────────────────────────────────────────

def test_wrapper_refuses_when_the_core_skill_is_missing(harness, tmp_path):
    """The provisioned wrapper resolves the core relative to its OWN real path; a machine
    where only the wrapper skill was provisioned must fail loudly (127 + a message naming
    the missing core), never launch an unidentified child."""
    wrapper_dir = tmp_path / "skills" / f"rig-detached-{harness}"
    wrapper_dir.mkdir(parents=True)
    wrapper = wrapper_dir / f"rig-detached-{harness}"
    wrapper.write_bytes(_launcher(harness).read_bytes())
    wrapper.chmod(0o755)
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")

    proc = subprocess.run([str(wrapper), "agent-x", str(brief)], capture_output=True, text=True, timeout=30)

    assert proc.returncode == 127
    assert "launcher core" in proc.stderr and "missing" in proc.stderr


def test_wrapper_resolves_the_core_through_a_harness_skill_dir_symlink(harness, tmp_path):
    """codex reaches skills through `~/.codex/skills/<skill>` symlinks into `~/.agents/skills/`;
    the wrapper must resolve the core from the symlink TARGET's directory, not the link's."""
    provisioned = tmp_path / "agents-skills"
    for skill in (f"rig-detached-{harness}", "rig-detached-agent"):
        (provisioned / skill).mkdir(parents=True)
        src = _SKILLS / skill / skill
        dst = provisioned / skill / skill
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o755)
    link_dir = tmp_path / "codex-skills"
    link_dir.mkdir()
    link = link_dir / f"rig-detached-{harness}"
    link.symlink_to(provisioned / f"rig-detached-{harness}")
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")

    proc = subprocess.run(
        [str(link / f"rig-detached-{harness}"), "agent-x", str(brief)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": os.pathsep.join(["/usr/bin", "/bin"]), "RIG_AGENT_LOG_DIR": str(_ISOLATED_LOG_DIR)},
    )

    # The core ran (its own not-on-PATH refusal), so the symlinked wrapper found it.
    assert proc.returncode == 127, proc.stderr
    assert "not found on PATH" in proc.stderr
    assert "launcher core" not in proc.stderr


def test_core_rejects_an_unknown_harness():
    core = _SKILLS / "rig-detached-agent" / "rig-detached-agent"
    proc = subprocess.run([str(core), "gemini", "a", "b"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()
