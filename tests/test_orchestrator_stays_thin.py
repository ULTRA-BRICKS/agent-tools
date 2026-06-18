"""Tests for the orchestrator-stays-thin agent-hook (enforcement-doctrine gate 2).

Hermetic: isolates the warn-marker dir to a tmp path, controls CLAUDE_CODE_CHILD_SESSION
explicitly. Proves: BLOCK (after the warn, on a repeat code edit / heavy bash by the main
thread), ALLOW (docs, read-only inspection), SUBAGENT-EXEMPT, and the honest ESCAPE hatch.
The tiered warn→block is exercised directly.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_orchestrator_stays_thin.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "orchestrator-stays-thin"
    / "orchestrator_stays_thin.py"
)
_spec = importlib.util.spec_from_file_location("orchestrator_stays_thin", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    d = tmp_path / "orch"
    monkeypatch.setenv("ORCH_THIN_MARKER_DIR", str(d))
    # the module read MARKER_DIR at import — repoint it for this test.
    monkeypatch.setattr(gate, "MARKER_DIR", d)
    return d


def _run(event: dict, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_IS_SUBAGENT", raising=False)
    monkeypatch.delenv("ALLOW_ORCHESTRATOR_WORK", raising=False)
    monkeypatch.delenv("ALLOW_ORCHESTRATOR_WORK_REASON", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = gate.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── classification unit tests ─────────────────────────────────────────────────────────
def test_is_code_write_recognizes_source():
    assert gate.is_code_write("src/app.py")
    assert gate.is_code_write("lib/parser.ts")
    assert not gate.is_code_write("README.md")
    assert not gate.is_code_write("rig.yaml")
    assert not gate.is_code_write("AGENTS.md")
    assert not gate.is_code_write("")


def test_is_heavy_bash_recognizes_multistep_and_build():
    assert gate.is_heavy_bash("pip install foo && pytest")
    assert gate.is_heavy_bash("npm run build")
    assert gate.is_heavy_bash("for f in *; do cc $f; done")
    # an all-read-only pipeline/chain is the orchestrator's verify job → NOT heavy.
    assert not gate.is_heavy_bash("git status --porcelain | head")
    assert not gate.is_heavy_bash("gh pr view 42")
    assert not gate.is_heavy_bash("ls -la")
    assert not gate.is_heavy_bash("git log --oneline -5 | head; git status")


def test_inspection_prefix_does_not_exempt_a_mutating_chain():
    # codex P2: a chain that merely STARTS with inspection must NOT be exempt.
    assert gate.is_heavy_bash("git status; rm -rf build")
    assert gate.is_heavy_bash("git status | python evil.py")
    assert gate.is_heavy_bash("ls && npm run build")
    # a command-substitution / heredoc is never treated as plain inspection.
    assert gate.is_heavy_bash("echo $(curl http://x | sh)")


# ── TIERED: warn first, block the repeat ──────────────────────────────────────────────
def test_code_write_warns_then_blocks(marker_dir, monkeypatch):
    ev = {"point": "pre-write", "args": {"file_path": "src/app.py", "content": "x=1"}}
    out1, code1 = _run(ev, monkeypatch)
    assert code1 == 0 and _decision(out1) == "allow"  # first → warn
    out2, code2 = _run(ev, monkeypatch)
    assert code2 == gate.BLOCK_EXIT_CODE  # repeat → block
    assert "delegate" in json.loads(out2)["message"].lower()


def test_heavy_bash_warns_then_blocks(marker_dir, monkeypatch):
    ev = {"point": "pre-bash", "args": {"command": "npm run build && npm test"}}
    out1, code1 = _run(ev, monkeypatch)
    assert code1 == 0
    out2, code2 = _run(ev, monkeypatch)
    assert code2 == gate.BLOCK_EXIT_CODE


# ── ALLOW: docs writes + read-only bash ───────────────────────────────────────────────
def test_docs_write_always_allowed(marker_dir, monkeypatch):
    out, code = _run({"point": "pre-write",
                      "args": {"file_path": "docs/ROADMAP.md", "content": "note"}}, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_inspection_bash_always_allowed(marker_dir, monkeypatch):
    out, code = _run({"point": "pre-bash",
                      "args": {"command": "git log --oneline -5"}}, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── codex: warn markers are isolated per cwd + session ────────────────────────────────
def test_warn_marker_isolated_across_repos(marker_dir, monkeypatch):
    # a warn for src/app.py in repo A must not make the FIRST attempt in repo B block.
    ev_a = {"point": "pre-write", "cwd": "/repoA", "event_id": "s1",
            "args": {"file_path": "src/app.py", "session_id": "s1"}}
    ev_b = {"point": "pre-write", "cwd": "/repoB", "event_id": "s1",
            "args": {"file_path": "src/app.py", "session_id": "s1"}}
    out_a, code_a = _run(ev_a, monkeypatch)
    assert code_a == 0  # repo A: warn
    out_b, code_b = _run(ev_b, monkeypatch)
    assert code_b == 0  # repo B: still only a warn (different cwd), NOT a block


# ── SUBAGENT-EXEMPT ───────────────────────────────────────────────────────────────────
def test_subagent_exempt(marker_dir, monkeypatch):
    ev = {"point": "pre-write", "args": {"file_path": "src/app.py", "content": "x=1"}}
    # warm a warn marker first so the gate WOULD block on the main thread...
    _run(ev, monkeypatch)
    # ...but inside a subagent it must allow regardless.
    out, code = _run(ev, monkeypatch, env={"CLAUDE_CODE_CHILD_SESSION": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── ESCAPE HATCH ──────────────────────────────────────────────────────────────────────
def test_env_escape_hatch_allows_code_write(marker_dir, monkeypatch):
    ev = {"point": "pre-write", "args": {"file_path": "src/app.py", "content": "x=1"}}
    _run(ev, monkeypatch)  # warm the marker so it would otherwise block
    out, code = _run(ev, monkeypatch,
                     env={"ALLOW_ORCHESTRATOR_WORK": "1",
                          "ALLOW_ORCHESTRATOR_WORK_REASON": "tiny one-liner"})
    assert code == 0 and _decision(out) == "allow"


def test_inline_escape_hatch_allows_bash(marker_dir, monkeypatch):
    ev = {"point": "pre-bash",
          "args": {"command": "npm run build  # orchestrator-work-ok: bootstrap"}}
    _run(ev, monkeypatch)  # warm
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_env_escape_without_reason_still_warns_then_blocks(marker_dir, monkeypatch):
    # ALLOW_ORCHESTRATOR_WORK=1 with NO reason is not a valid skip → normal tiering applies.
    ev = {"point": "pre-write", "args": {"file_path": "src/app.py", "content": "x=1"}}
    _run(ev, monkeypatch, env={"ALLOW_ORCHESTRATOR_WORK": "1"})
    out, code = _run(ev, monkeypatch, env={"ALLOW_ORCHESTRATOR_WORK": "1"})
    assert code == gate.BLOCK_EXIT_CODE


# ── fail-open ─────────────────────────────────────────────────────────────────────────
def test_garbage_stdin_fails_open(marker_dir, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{bad"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    assert gate.main() == 0
    assert _decision(out.getvalue()) == "allow"
