"""omp yield tool_call must map to v1 stop so session-end hooks can block a premature yield.

CC maps Stop→stop; Codex maps Stop→stop. omp has no Stop event, but the hidden yield
tool is a tool_call, and tool_call can return {block:true}. Refs agent-tools#640.
"""

from __future__ import annotations

import json

import omp_hook_bridge.dispatch as dispatch

from test_omp_hook_bridge import _install_descriptor, _run_dispatch


def test_point_for_event_maps_yield_to_stop():
    assert dispatch.point_for_event("tool_call", "yield") == "stop"
    assert dispatch.point_for_event("tool_call", "YIELD") == "stop"
    assert dispatch.point_for_event("tool_result", "yield") is None


def test_omp_bridge_blocks_yield_via_stop_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    blocker = tmp_path / "block_stop.py"
    blocker.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(10)\n", encoding="utf-8")
    blocker.chmod(0o755)
    _install_descriptor(hooks_dir, hook_id="stop-block", point="stop", cmd=blocker)
    event = {
        "toolName": "yield",
        "input": {"data": {"pr": "https://example.com/pull/1"}},
        "cwd": str(tmp_path),
        "toolCallId": "yield-1",
    }
    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_stop_session_id_is_stable_across_yield_retries():
    a = dispatch.to_v1_event(
        {"toolName": "yield", "toolCallId": "call-1", "cwd": "/repo", "input": {}},
        point="stop",
    )
    b = dispatch.to_v1_event(
        {"toolName": "yield", "toolCallId": "call-2", "cwd": "/repo", "input": {}},
        point="stop",
    )
    assert a["args"]["session_id"] == b["args"]["session_id"]
    assert a["event_id"] == b["event_id"]
    assert a["event_id"] != "call-1"
    assert a["args"]["session_id"]


def test_to_v1_event_forwards_ctx_agent_id():
    ev = dispatch.to_v1_event(
        {"toolName": "bash", "toolCallId": "c1", "cwd": "/repo", "input": {"command": "ls"}, "agentId": "Helper"},
        point="pre-bash",
    )
    assert ev["args"]["agent_id"] == "Helper"


def test_stop_session_id_prefers_agent_id_over_cwd():
    a = dispatch.to_v1_event(
        {"toolName": "yield", "toolCallId": "c1", "cwd": "/repo", "input": {}, "agentId": "Helper"},
        point="stop",
    )
    b = dispatch.to_v1_event(
        {"toolName": "yield", "toolCallId": "c2", "cwd": "/repo", "input": {}, "agentId": "Helper"},
        point="stop",
    )
    assert a["args"]["session_id"] == b["args"]["session_id"] == "Helper"
