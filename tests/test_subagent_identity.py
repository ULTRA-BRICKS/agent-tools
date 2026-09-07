"""Tests for the shared subagent-identity helper the non-CC bridges use (agent-tools#573).

Two identity sources, both outside the reach of a model-authored ``tool_input``:

- the LAUNCHER ENV markers (``RIG_AGENT_ID`` / ``RIG_DETACHED_AGENT``) set by the
  ``rig-detached-<harness>`` launcher skills for a child process;
- PROCESS ANCESTRY: the bridge's own process tree shows a second, older process of the same
  harness above the one that dispatched this hook — a ``codex exec`` started from a codex
  session's shell tool, an ``omp -p`` started from an omp session, and so on.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_subagent_identity.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agent_hooks_v1 import subagent_identity as ident  # noqa: E402


# ── env markers ──────────────────────────────────────────────────────────────────────────

def test_env_marker_named_agent_wins():
    assert ident.env_marker_agent_id({"RIG_AGENT_ID": "worker-7", "RIG_DETACHED_AGENT": "1"}) == "worker-7"


def test_env_marker_anonymous_detached_yields_detached():
    assert ident.env_marker_agent_id({"RIG_DETACHED_AGENT": "1"}) == "detached"


@pytest.mark.parametrize("environ", [{}, {"RIG_AGENT_ID": "   "}, {"RIG_DETACHED_AGENT": "0"},
                                     {"RIG_DETACHED_AGENT": "true"}, {"RIG_AGENT_ID": "", "RIG_DETACHED_AGENT": ""}])
def test_env_marker_blank_or_wrong_value_is_no_marker(environ):
    assert ident.env_marker_agent_id(environ) == ""


# ── process ancestry ─────────────────────────────────────────────────────────────────────

def _table(rows):
    """rows: list of (pid, ppid, args)."""
    return {pid: (ppid, args) for pid, ppid, args in rows}


# The real shape captured on this machine (codex 0.153.4, macOS): a hook runs as
# `bash capture.sh` under the vendor binary, which sits under the `node` wrapper script.
_CODEX_SELF_RUN = [
    (580, 91739, "bash /x/capture.sh PreToolUse"),
    (91739, 91716, "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex exec -s danger-full-access hi"),
    (91716, 91712, "node /opt/homebrew/bin/codex exec -s danger-full-access hi"),
    (91712, 91710, "timeout 300 codex exec -s danger-full-access hi"),
]


def test_ancestry_top_level_codex_session_is_not_a_subagent():
    rows = _CODEX_SELF_RUN + [
        (91710, 91537, "/bin/zsh -c source snapshot.sh && eval 'codex exec ...'"),
        (91537, 74821, "/bin/zsh -c ..."),
        (74821, 74810, "claude --permission-mode bypassPermissions --name rig-fable"),
        (74810, 70483, "bash /Users/ultra/.files/bin/claude-rotate --name rig-fable"),
        (70483, 1, "tmux"),
    ]
    assert ident.ancestor_agent_id("codex", pid=580, table=_table(rows)) == ""


def test_ancestry_codex_exec_under_a_codex_session_is_a_subagent():
    """A child `codex exec` launched from a parent codex session's shell tool: the hook's own
    contiguous codex run (vendor binary + node wrapper + the `timeout` wrapper the parent's
    shell used) is skipped, the parent's `/bin/zsh -lc` breaks the run, and the parent's own
    codex binary above it is the ancestor that makes this a subagent."""
    rows = _CODEX_SELF_RUN + [
        (91710, 50000, "/bin/zsh -lc codex exec -- run the tests"),
        (50000, 49990, "/opt/homebrew/lib/node_modules/@openai/codex/.../bin/codex"),
        (49990, 49980, "node /opt/homebrew/bin/codex"),
        (49980, 1, "/bin/zsh -l"),
    ]
    assert ident.ancestor_agent_id("codex", pid=580, table=_table(rows)) == "ancestor:codex:50000"


def test_ancestry_wrapper_chain_of_one_session_does_not_count_as_two():
    """The node wrapper + vendor binary of ONE codex invocation are contiguous and must never be
    mistaken for a parent/child pair — only a same-harness process past a non-harness break counts."""
    rows = _CODEX_SELF_RUN + [(91710, 1, "/bin/zsh -l")]
    assert ident.ancestor_agent_id("codex", pid=580, table=_table(rows)) == ""


def test_ancestry_different_harness_ancestor_does_not_count():
    """Same-harness only (the brief's contract): a codex spawned by a Claude Code session is not
    classified by ancestry — its identity, if any, has to come from the env markers."""
    rows = _CODEX_SELF_RUN + [
        (91710, 74821, "/bin/zsh -c ..."),
        (74821, 1, "claude --name rig-fable"),
    ]
    assert ident.ancestor_agent_id("codex", pid=580, table=_table(rows)) == ""


def test_ancestry_omp_under_omp_is_a_subagent():
    rows = [
        (700, 650, "python3 -m omp_hook_bridge tool_call"),
        (650, 640, "/opt/homebrew/Cellar/omp/18.0.11/bin/omp -p --no-session -- brief"),
        (640, 600, "/bin/zsh -c omp -p --no-session -- brief"),
        (600, 1, "/opt/homebrew/Cellar/omp/18.0.11/bin/omp"),
    ]
    assert ident.ancestor_agent_id("omp", pid=700, table=_table(rows)) == "ancestor:omp:600"


def test_ancestry_argv1_counts_only_behind_a_js_interpreter_wrapper():
    """`node /opt/homebrew/bin/codex` is codex; `python3 codex` (a same-named local script) or
    `sh omp` is not — argv[1] is promoted only behind a known interpreter wrapper."""
    assert ident._is_harness_process("node /opt/homebrew/bin/codex exec", "codex")
    assert ident._is_harness_process("bun /usr/local/lib/opencode/index.js", "opencode") is False
    assert ident._is_harness_process("bun /usr/local/bin/opencode", "opencode")
    assert ident._is_harness_process("python3 codex --serve", "codex") is False
    assert ident._is_harness_process("sh omp", "omp") is False
    assert ident._is_harness_process("", "omp") is False


def test_ancestry_harness_name_must_be_a_whole_basename():
    """`omp-helper` / `codex-wrapper` / a path that merely CONTAINS the name is not the harness."""
    rows = [
        (700, 650, "python3 -m omp_hook_bridge tool_call"),
        (650, 640, "/opt/homebrew/bin/omp"),
        (640, 600, "/bin/zsh -c ..."),
        (600, 590, "/usr/local/bin/omp-helper --serve"),
        (590, 1, "/home/omp/bin/something"),
    ]
    assert ident.ancestor_agent_id("omp", pid=700, table=_table(rows)) == ""


def test_ancestry_without_any_harness_process_is_empty():
    rows = [(700, 650, "python3 -m pytest"), (650, 1, "/bin/zsh")]
    assert ident.ancestor_agent_id("codex", pid=700, table=_table(rows)) == ""


def test_ancestry_cycle_or_missing_parent_terminates():
    rows = [(700, 650, "python3"), (650, 700, "/bin/zsh")]  # bogus cycle
    assert ident.ancestor_agent_id("codex", pid=700, table=_table(rows)) == ""
    assert ident.ancestor_agent_id("codex", pid=999, table=_table(rows)) == ""


def test_ancestry_ps_failure_fails_closed(monkeypatch):
    """No process table (ps missing/broken) → no identity → the call stays GOVERNED."""
    monkeypatch.setattr(ident, "_process_table", lambda: {})
    assert ident.ancestor_agent_id("codex") == ""


def test_real_process_table_parses_this_process():
    """`ps` really runs and the current process is in the table with a numeric parent."""
    import os

    table = ident._process_table()
    assert os.getpid() in table
    ppid, args = table[os.getpid()]
    assert ppid == os.getppid()
    assert "python" in args or "pytest" in args


def test_real_process_table_keeps_a_long_argv_intact():
    """procps clips `args` to 80 columns when stdout is not a terminal unless `-ww` is passed —
    CI's uv-installed python (`/home/runner/work/_temp/setup-uv-cache/builds-v0/.tmp…/bin/`)
    fell off the end and the sibling test above failed there. A harness under a long install
    path would be invisible to the ancestry walk the same way."""
    import subprocess
    import sys

    marker = "subagent-identity-long-argv-" + "x" * 160
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; time.sleep(30)", marker],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        table = ident._process_table()
    finally:
        child.kill()
        child.wait()
    assert child.pid in table
    assert marker in table[child.pid][1]


def test_real_ancestry_walk_end_to_end(tmp_path):
    """The walk against a REAL process tree (review finding, #573 round 2): a stub harness
    binary — `/bin/sh` reachable under a unique harness name, so argv[0]'s basename IS the
    harness and nothing else on the machine can match — runs the probe once directly (top
    level → no identity) and once under a second copy of itself with a plain `sh` in between
    (the parent session's shell tool → the outer one is the ancestor)."""
    import os
    import shlex
    import subprocess
    import sys

    harness = f"fakeharness{os.getpid()}"
    stub = tmp_path / "bin" / harness
    stub.parent.mkdir()
    stub.symlink_to("/bin/sh")
    probe = tmp_path / "probe.py"
    probe.write_text(
        f"import sys; sys.path.insert(0, {str(_LIB)!r})\n"
        "from agent_hooks_v1 import subagent_identity\n"
        f"print(subagent_identity.ancestor_agent_id({harness!r}))\n",
        encoding="utf-8",
    )
    # `; true` keeps every shell level from exec-replacing itself with its only command.
    leaf = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}; true"
    top = subprocess.run([str(stub), "-c", leaf], capture_output=True, text=True, timeout=30)
    assert top.returncode == 0, top.stderr
    assert top.stdout.strip() == ""

    inner = f"{shlex.quote(str(stub))} -c {shlex.quote(leaf)}; true"
    nested_cmd = f"/bin/sh -c {shlex.quote(inner)}; true"
    nested = subprocess.run([str(stub), "-c", nested_cmd], capture_output=True, text=True, timeout=30)
    assert nested.returncode == 0, nested.stderr
    assert nested.stdout.strip().startswith(f"ancestor:{harness}:")


# ── combined precedence ──────────────────────────────────────────────────────────────────

def test_detect_prefers_env_marker_over_ancestry(monkeypatch):
    monkeypatch.setattr(ident, "ancestor_agent_id", lambda harness: "ancestor:codex:1")
    assert ident.detect_subagent("codex", environ={"RIG_AGENT_ID": "w1"}) == ("w1", "detached")


def test_detect_falls_back_to_ancestry(monkeypatch):
    monkeypatch.setattr(ident, "ancestor_agent_id", lambda harness: f"ancestor:{harness}:42")
    assert ident.detect_subagent("omp", environ={}) == ("ancestor:omp:42", "ancestor")


def test_detect_nothing_is_empty(monkeypatch):
    monkeypatch.setattr(ident, "ancestor_agent_id", lambda harness: "")
    assert ident.detect_subagent("opencode", environ={}) == ("", "")
