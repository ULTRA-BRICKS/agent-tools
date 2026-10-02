"""Tests for agent-fan-out-memory-gate (pre-agent, agent-tools#519)."""

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
    / "agent-fan-out-memory-gate"
    / "agent_fan_out_memory_gate.py"
)
_spec = importlib.util.spec_from_file_location("agent_fan_out_memory_gate", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run_event(event, monkeypatch, *, assessment):
    monkeypatch.setattr(hook.mp, "read_snapshot", lambda **k: assessment[0])
    monkeypatch.setattr(hook.mp, "load_config", lambda cwd=None: assessment[1])
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out.getvalue(), err.getvalue(), hook.main()


def test_blocks_task_dispatch_under_pressure(monkeypatch):
    snap = hook.mp.MemorySnapshot(pressure_level=4, free_bytes=50 * 1024 ** 2, swap_used_bytes=9000, swap_total_bytes=10000)
    cfg = hook.mp.MemoryGateConfig()
    out, _err, code = _run_event({"tool": "Task", "args": {"subagent_type": "generalPurpose"}}, monkeypatch, assessment=(snap, cfg))
    assert code == hook.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"


def test_allows_dispatch_when_healthy(monkeypatch):
    snap = hook.mp.MemorySnapshot(pressure_level=1, free_bytes=8 * 1024 ** 3, swap_used_bytes=100, swap_total_bytes=10000)
    cfg = hook.mp.MemoryGateConfig()
    out, _err, code = _run_event({"tool": "Agent", "args": {"prompt": "hi"}}, monkeypatch, assessment=(snap, cfg))
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_fails_open_on_bad_json(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert hook.main() == 0
    assert json.loads(out.getvalue())["decision"] == "allow"
