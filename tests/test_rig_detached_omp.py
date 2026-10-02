"""Tests for the rig-detached-omp launcher — the canonical rig-owned detached-agent
launcher for omp (oh-my-pi), shipped as the `rig-detached-omp` universal SKILL
(skills/universal/rig-detached-omp/) so rig provisions it to
~/.agents/skills/rig-detached-omp/ on every managed machine, mirroring
rig-detached-opencode (#476/#497).

The launcher exports RIG_AGENT_ID=<name> and RIG_DETACHED_AGENT=1 for a nohup-detached
`omp -p` child, so the omp hook bridge classifies the child session as a dispatched
subagent on every tool call. These tests NEVER launch a real omp: the happy path
shadows `omp` with a stub executable that records its environment and arguments,
echoes into the launcher's log file, and sleeps long enough to prove the launcher
returned without waiting for it.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_rig_detached_omp.py -q
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "universal"
    / "rig-detached-omp"
    / "rig-detached-omp"
)
_ISOLATED_LOG_DIR = Path(tempfile.mkdtemp(prefix="rig-detached-omp-test-logs-"))
_LOG_DIR = _ISOLATED_LOG_DIR

# See rig-detached-opencode's sibling test for the same budget rationale: the launcher
# spawns bash -> nohup -> bash -c -> python3 (setsid shim) -> stub, which under a loaded
# machine can take >1.5s; a 4.5s budget separates "the launcher returned before the
# child finished" from scheduler noise.
_STUB_SLEEP_S = 5.0


@pytest.fixture(scope="module", autouse=True)
def _launcher_executable():
    if not os.access(_LAUNCHER, os.X_OK):
        _LAUNCHER.chmod(0o755)


def _stub_omp(bin_dir: Path, marker: Path) -> None:
    """A stub `omp` that records env/args/cwd, streams into stdout (which the launcher
    redirects into the agent log), and sleeps past the launcher's return.

    Args are recorded ONE PER LINE as `ARG<n>=<value>` (not a space-joined `$*`
    string): a joined string cannot distinguish an empty argv word, a reordered
    `--model`/`--` pair, or a space-containing value from several other argv shapes
    that would produce the same joined text (review finding on agent-tools#687's
    model-passthrough patch — MiMo/GLM/DeepSeek-cc round 2)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "omp"
    stub.write_text(
        "#!/bin/sh\n"
        "{\n"
        '  echo "RIG_AGENT_ID=$RIG_AGENT_ID"\n'
        '  echo "RIG_DETACHED_AGENT=$RIG_DETACHED_AGENT"\n'
        '  echo "PWD=$PWD"\n'
        '  echo "ARGC=$#"\n'
        '  i=0\n'
        '  for a in "$@"; do\n'
        '    echo "ARG$i=$a"\n'
        '    i=$((i + 1))\n'
        '  done\n'
        f'}} > "{marker}.tmp" && mv "{marker}.tmp" "{marker}"\n'
        'echo "stub-omp-started"\n'
        f"sleep {_STUB_SLEEP_S}\n"
        'echo "stub-omp-done"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _recorded_argv(recorded: str) -> list[str]:
    """Reconstruct the exact child argv from a stub marker's `ARGC=`/`ARG<n>=` lines,
    in argv order (not dict/set order, and not vulnerable to a value that itself
    starts with `ARG`)."""
    lines = recorded.splitlines()
    argc = int(next(l for l in lines if l.startswith("ARGC=")).split("=", 1)[1])
    argv = []
    for i in range(argc):
        prefix = f"ARG{i}="
        line = next(l for l in lines if l.startswith(prefix))
        argv.append(line[len(prefix) :])
    return argv


def _run_launcher(
    *args: str,
    path_override: Path | None = None,
    extra_env: dict[str, str] | None = None,
    bash: str | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Isolate the log dir by default: on a shared host /tmp/agent-logs may be owned by
    # another user and the launcher (correctly) refuses it. Tests that want the real
    # default pass RIG_AGENT_LOG_DIR explicitly.
    env.setdefault("RIG_AGENT_LOG_DIR", str(_ISOLATED_LOG_DIR))
    if path_override is not None:
        # Stub dir FIRST so the stub shadows any real omp on PATH.
        env["PATH"] = str(path_override) + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)
    argv = [bash, str(_LAUNCHER), *args] if bash else [str(_LAUNCHER), *args]
    return subprocess.run(
        argv,
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


# ── argument validation -> exit 2 ──────────────────────────────────────────────────────


def test_no_args_prints_usage_and_exits_2():
    proc = _run_launcher()
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


def test_too_many_args_exits_2():
    """5 positional args (name, brief, workdir, model, one extra) is a usage error —
    the widened bound (2-4, was 2-3) must still reject a stray 5th arg rather than
    silently launching a child with a bogus model (Astra/MiMo review finding)."""
    proc = _run_launcher("a", "b", "c", "d", "e")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


def test_missing_brief_file_exits_2(tmp_path):
    proc = _run_launcher("agent-x", str(tmp_path / "nope.md"))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_missing_workdir_exits_2(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("mission", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief), str(tmp_path / "nope"))
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_omp_not_on_path_exits_127(tmp_path, monkeypatch):
    """No `omp` resolvable at all -> 127 (mirrors the shell's not-found code). PATH keeps
    the OS utility dirs (bash/date/mkdir/nohup for the launcher's shebang and body) but
    drops every dir that can hold a real omp (homebrew, npm globals)."""
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    brief = tmp_path / "brief.md"
    brief.write_text("mission", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief), str(tmp_path))
    assert proc.returncode == 127
    assert "not found on PATH" in proc.stderr


# ── happy path with a stub omp ─────────────────────────────────────────────────────────


def test_happy_path_no_model_omits_model_flag(tmp_path):
    """The default (no 4th arg) invocation every existing caller uses must still work,
    and must NOT pass a `--model` flag at all."""
    name = "omp687-test-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: write ok to the handoff file and exit.", encoding="utf-8")

    started = time.monotonic()
    proc = _run_launcher(name, str(brief), str(workdir), path_override=stub_dir)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    assert elapsed < _STUB_SLEEP_S - 0.5, f"launcher blocked for {elapsed:.2f}s"
    assert f"detached agent '{name}' launched (pid" in proc.stdout

    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"RIG_AGENT_ID={name}" in recorded
    assert "RIG_DETACHED_AGENT=1" in recorded
    assert f"PWD={workdir}" in recorded
    argv = _recorded_argv(recorded)
    assert "--model" not in argv, argv
    assert "write ok to the handoff file" in argv[-1]
    # The no-model header must NOT carry a `(model ...)` suffix — a stale suffix here
    # would mean the header lies about what was requested (the header is the ONLY
    # persistent record of the dispatch; see the model-path test below).
    _poll_for(log)
    header = log.read_text(encoding="utf-8").splitlines()[0]
    assert f"launching detached agent '{name}'" in header
    assert "(model " not in header, header


def test_model_arg_is_forwarded_as_model_flag(tmp_path):
    """A caller-supplied [model] must reach the child as a single `--model <value>`
    flag, verbatim, before the child's brief."""
    name = "omp687-test-model"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: pick a model.", encoding="utf-8")

    proc = _run_launcher(
        name, str(brief), str(workdir), "opencode-go/mimo-v2.5", path_override=stub_dir
    )
    assert proc.returncode == 0, proc.stderr

    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    argv = _recorded_argv(recorded)
    # `--model <value>` must appear as two adjacent argv words, strictly before the
    # `--` that ends option parsing and shields the brief.
    model_idx = argv.index("--model")
    assert argv[model_idx + 1] == "opencode-go/mimo-v2.5", argv
    assert argv.index("--") > model_idx + 1, argv
    # The launch header is the only persistent record of the dispatch (--no-session
    # means there is no omp session log as a fallback): it must name the requested
    # model, not just name/workdir, so a post-hoc "what was actually requested" is
    # answerable from the log alone (review finding).
    _poll_for(log)
    header = log.read_text(encoding="utf-8").splitlines()[0]
    assert f"launching detached agent '{name}'" in header
    assert "model 'opencode-go/mimo-v2.5'" in header, header


def test_empty_string_model_omits_model_flag(tmp_path):
    """An explicit empty string for [model] must degrade to the no-model default, not
    inject a bare `--model` with no value (the `${4:-}` / `[ -n "$model" ]` contract)."""
    name = "omp687-test-empty-model"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: empty model.", encoding="utf-8")

    proc = _run_launcher(name, str(brief), str(workdir), "", path_override=stub_dir)
    assert proc.returncode == 0, proc.stderr

    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    argv = _recorded_argv(recorded)
    assert "--model" not in argv, argv


def test_dash_prefixed_model_forwarded_verbatim_as_models_value(tmp_path):
    """A model value that itself starts with `-` (e.g. a caller typo, or a fuzzy-match
    string that happens to look like a flag) is forwarded verbatim as `--model`'s
    argument, not re-parsed as a second flag -- the array-based `model_args=(--model
    "$model")` construction, unlike a naive string interpolation, always keeps it as
    the single argv word immediately after `--model`."""
    name = "omp687-test-dash-model"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: dash-prefixed model.", encoding="utf-8")

    proc = _run_launcher(
        name, str(brief), str(workdir), "--not-a-flag", path_override=stub_dir
    )
    assert proc.returncode == 0, proc.stderr

    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    argv = _recorded_argv(recorded)
    model_idx = argv.index("--model")
    assert argv[model_idx + 1] == "--not-a-flag", argv
    # It must be omp's --model VALUE, not re-parsed as its own flag: exactly one
    # "--model" in argv, and the value slot is not itself "--model".
    assert argv.count("--model") == 1, argv


def test_space_containing_model_forwarded_as_single_argv_word(tmp_path):
    """A model value containing spaces (e.g. 'claude opus 4') must arrive as ONE argv
    word after --model, not be split — the array-based `model_args=(--model "$model")`
    construction double-quotes the value, so word splitting never applies."""
    name = "omp687-test-space-model"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: space model.", encoding="utf-8")

    proc = _run_launcher(
        name, str(brief), str(workdir), "claude opus 4", path_override=stub_dir
    )
    assert proc.returncode == 0, proc.stderr

    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    argv = _recorded_argv(recorded)
    model_idx = argv.index("--model")
    # "claude opus 4" must be ONE argv word, not split into ["claude", "opus", "4"]:
    assert argv[model_idx + 1] == "claude opus 4", argv
    # And the brief (after --) must not have consumed part of the model value:
    assert "--" in argv[model_idx + 2 :], argv


def test_control_chars_in_model_stripped_from_log_header_only(tmp_path):
    """A model value containing a newline reaches omp VERBATIM as one argv word (the
    array + quoted expansion is unaffected), but the log header — the only persistent
    record of what was requested — must strip control chars so a \n cannot fork the
    single header line into forged extra log lines (review finding)."""
    name = "omp687-test-ctrl-model"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: control-char model.", encoding="utf-8")

    hostile = "opus\nFORGED LOG LINE"
    proc = _run_launcher(name, str(brief), str(workdir), hostile, path_override=stub_dir)
    assert proc.returncode == 0, proc.stderr

    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    # The raw value reached the child verbatim: the stub's per-line ARG<n>=<value>
    # format forks on the embedded \n, so the marker shows the value's two halves —
    # proof the child received the unsanitized string (argv path unaffected).
    assert "ARG" in recorded and "opus" in recorded, recorded
    assert "FORGED LOG LINE" in recorded, recorded
    # And it stayed ONE argv word despite the embedded \n: quoted array expansion is
    # not subject to IFS splitting, so ARGC is exactly the 9-word model invocation
    # (-p --auto-approve --no-session --cwd <workdir> --model <value> -- <brief>).
    argc = int(
        next(l for l in recorded.splitlines() if l.startswith("ARGC=")).split("=", 1)[1]
    )
    assert argc == 9, recorded

    # Log path sanitized: the header is one line and contains no forged second line.
    _poll_for(log)
    log_text = log.read_text(encoding="utf-8")
    header = log_text.splitlines()[0]
    assert "(model 'opusFORGED LOG LINE')" in header, header
    forged = [ln for ln in log_text.splitlines()[1:] if ln.startswith("FORGED")]
    assert forged == [], log_text


def test_no_model_default_path_survives_bash_3_2s_nounset(tmp_path):
    """Regression test (Astra/MiMo/DeepSeek-cc review findings on agent-tools#687's
    model-passthrough patch): an EMPTY `model_args=()` expanded as a bare
    `"${model_args[@]}"` under `set -u` is bash's nounset trap on bash < 4.4, incl.
    macOS's stock `/bin/bash` 3.2.57 — the exact interpreter this launcher's own
    setsid/python3 fallback comment says it targets. Before the `${arr[@]+"${arr[@]}"}`
    guard, this aborted word expansion inside the backgrounded `&` subshell: `set -e`
    never observed it, so the launcher still printed "launched" and exited 0 while the
    child never spawned. Run explicitly under /bin/bash so this is deterministic
    regardless of which bash resolves first on $PATH."""
    if not Path("/bin/bash").exists():
        pytest.skip("no /bin/bash on this machine")
    bash_version = subprocess.run(
        ["/bin/bash", "-c", "echo $BASH_VERSINFO"], capture_output=True, text=True
    ).stdout.strip()
    if not bash_version or int(bash_version.split()[0]) >= 5:
        pytest.skip(f"/bin/bash is not the pre-4.4 nounset case here ({bash_version!r})")

    name = "omp687-test-bash32"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_omp(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: survive bash 3.2.", encoding="utf-8")

    proc = _run_launcher(
        name, str(brief), str(workdir), path_override=stub_dir, bash="/bin/bash"
    )
    assert proc.returncode == 0, proc.stderr
    assert "unbound variable" not in proc.stderr, proc.stderr
    _poll_for(marker)


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
