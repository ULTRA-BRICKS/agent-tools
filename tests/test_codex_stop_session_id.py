"""Codex Stop must map to a stable v1 session id so the self-check cooldown can fire once.

Codex Stop often omits session_id and sends a unique per-invocation event_id/turn_id.
stop-completion-selfcheck keys cooldown on args.session_id then event.event_id; a unique
id makes every Stop look new and re-blocks forever (agent-tools#655). Mirror of
tests/test_omp_yield_stop.py's stable-session-id cases for the Codex bridge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import codex_hook_bridge.dispatch as dispatch  # noqa: E402


@pytest.fixture(autouse=True)
def _no_host_agent_markers(monkeypatch):
    """A rig-dispatched pytest process may carry RIG_AGENT_ID / RIG_DETACHED_AGENT."""
    monkeypatch.delenv("RIG_AGENT_ID", raising=False)
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)


def test_stop_session_id_is_stable_across_unique_event_ids():
    a = dispatch.to_v1_event(
        {
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "event_id": "ev-1",
            "turn_id": "turn-1",
        },
        point="stop",
    )
    b = dispatch.to_v1_event(
        {
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "event_id": "ev-2",
            "turn_id": "turn-2",
        },
        point="stop",
    )
    assert a["args"]["session_id"] == b["args"]["session_id"]
    assert a["event_id"] == b["event_id"]
    assert a["event_id"] not in ("ev-1", "turn-1")
    assert b["event_id"] not in ("ev-2", "turn-2")
    assert a["args"]["session_id"] == "/repo"
    assert a["args"]["session_id"]


def test_stop_session_id_prefers_real_session_id_over_unique_event_id():
    ev = dispatch.to_v1_event(
        {
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "event_id": "unique-xyz",
            "session_id": "sess-real",
        },
        point="stop",
    )
    assert ev["args"]["session_id"] == "sess-real"
    assert ev["event_id"] == "sess-real"


def test_stop_session_id_uses_conversation_id_when_session_id_absent():
    ev = dispatch.to_v1_event(
        {
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "conversation_id": "conv-abc",
            "turn_id": "turn-9",
        },
        point="stop",
    )
    assert ev["args"]["session_id"] == "conv-abc"
    assert ev["event_id"] == "conv-abc"


def test_stop_session_id_prefers_detached_agent_id_across_cwds(monkeypatch):
    monkeypatch.setenv("RIG_AGENT_ID", "Helper")
    a = dispatch.to_v1_event(
        {
            "hook_event_name": "Stop",
            "cwd": "/repo-a",
            "event_id": "ev-1",
        },
        point="stop",
    )
    b = dispatch.to_v1_event(
        {
            "hook_event_name": "Stop",
            "cwd": "/repo-b",
            "event_id": "ev-2",
        },
        point="stop",
    )
    assert a["args"]["session_id"] == b["args"]["session_id"] == "Helper"
    assert a["event_id"] == b["event_id"] == "Helper"
