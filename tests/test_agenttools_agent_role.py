"""Contract tests for ``lib/agenttools_agent_role`` — the single subagent-role predicate.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_agent_role.py -q

Every test passes ``environ`` explicitly (or monkeypatches ``os.environ`` for the default-env
case) so a rig/omp-dispatched test run, which carries the launcher markers itself, never leaks
into the assertions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_agent_role import (  # noqa: E402
    DETACHED_LABEL,
    NESTED_SESSION_MARKER_PREFIX,
    has_omp_session_ancestor,
    is_subagent,
    launcher_agent_id,
    native_payload_agent,
    nested_id_from_session_file,
    omp_nested_agent_id,
    opencode_child_agent_id,
)

_SESSION_DIR = "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e"
_MAIN = f"/sessions/-xp-repo/{_SESSION_DIR}.jsonl"
_CHILD = f"/sessions/-xp-repo/{_SESSION_DIR}/Worker.jsonl"
_GRANDCHILD = f"/sessions/-xp-repo/{_SESSION_DIR}/Worker/Grand.jsonl"


# ── launcher_agent_id ────────────────────────────────────────────────────────────────────


def test_launcher_rig_agent_id_is_the_identity():
    assert launcher_agent_id({"RIG_AGENT_ID": "  Reviewer "}) == "Reviewer"


def test_launcher_rig_agent_id_wins_over_anonymous_marker():
    assert launcher_agent_id({"RIG_AGENT_ID": "Named", "RIG_DETACHED_AGENT": "1"}) == "Named"


def test_launcher_anonymous_marker_is_detached_label():
    assert launcher_agent_id({"RIG_DETACHED_AGENT": "1"}) == DETACHED_LABEL == "detached"


@pytest.mark.parametrize("env", [
    {},
    {"RIG_AGENT_ID": "   "},
    {"RIG_DETACHED_AGENT": "true"},   # only the exact value 1 is a marker
    {"RIG_DETACHED_AGENT": "0"},
    {"RIG_DETACHED_AGENT": ""},
])
def test_launcher_blank_or_inexact_markers_are_not_an_identity(env):
    assert launcher_agent_id(env) == ""


def test_launcher_defaults_to_process_environ(monkeypatch):
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)
    monkeypatch.setenv("RIG_AGENT_ID", "FromProcessEnv")
    assert launcher_agent_id() == "FromProcessEnv"
    # an explicit environ is authoritative: the process env must not bleed in
    assert launcher_agent_id({}) == ""


# ── native_payload_agent ─────────────────────────────────────────────────────────────────


def test_native_payload_reads_top_level_agent_fields():
    payload = {"agent_id": "sub-42", "agent_type": "general-purpose", "session_id": "s"}
    assert native_payload_agent(payload) == ("sub-42", "general-purpose")


def test_native_payload_never_reads_tool_input():
    payload = {"tool_input": {"agent_id": "forged", "agent_type": "forged-t"}}
    assert native_payload_agent(payload) == ("", "")


@pytest.mark.parametrize("payload", [None, {}, {"agent_id": None}, {"agent_id": "  "}, "not-a-dict"])
def test_native_payload_absent_or_blank_is_empty(payload):
    assert native_payload_agent(payload) == ("", "")


# ── omp nested session path ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    (_MAIN, ""),                      # main session: parent is the project slug
    (_CHILD, "Worker"),               # depth 1
    (_GRANDCHILD, "Grand"),           # depth 2: immediate parent is an AGENT id
    ("/repo/notes/archive/transcript.jsonl", ""),
    (f"/sessions/x/{_SESSION_DIR}/Worker.txt", ""),   # not a .jsonl
    (f"/sessions/x/{_SESSION_DIR}/.jsonl", ""),       # blank stem
    ("", ""),
    ("   ", ""),
])
def test_nested_id_from_session_file(path, expected):
    assert nested_id_from_session_file(path) == expected


def test_has_omp_session_ancestor_walks_every_ancestor():
    assert has_omp_session_ancestor(Path(_GRANDCHILD)) is True
    assert has_omp_session_ancestor(Path(_MAIN)) is False


def test_omp_nested_payload_session_file_beats_env():
    event = {"sessionFile": _CHILD}
    assert omp_nested_agent_id(event, {"PI_SESSION_FILE": _MAIN}) == "Worker"


def test_omp_flat_payload_session_file_short_circuits_env_and_marker():
    """A present-but-flat session file is real evidence of a MAIN session: neither a nested
    env file nor `isMainSession: false` may override it."""
    event = {"sessionFile": _MAIN, "isMainSession": False, "sessionId": "abc"}
    assert omp_nested_agent_id(event, {"PI_SESSION_FILE": _CHILD}) == ""


def test_omp_env_file_used_when_payload_has_none():
    assert omp_nested_agent_id({}, {"PI_SESSION_FILE": _CHILD}) == "Worker"
    assert omp_nested_agent_id(None, {"PI_SESSION_FILE": _MAIN}) == ""


def test_omp_is_main_session_false_marker_only_without_any_file_evidence():
    event = {"isMainSession": False, "sessionId": "sess-1"}
    assert omp_nested_agent_id(event, {}) == f"{NESTED_SESSION_MARKER_PREFIX}sess-1" == "omp-nested:sess-1"
    # marker needs a session id, and `True`/missing never synthesizes one
    assert omp_nested_agent_id({"isMainSession": False}, {}) == ""
    assert omp_nested_agent_id({"isMainSession": True, "sessionId": "s"}, {}) == ""
    assert omp_nested_agent_id({"sessionId": "s"}, {}) == ""


# ── opencode ─────────────────────────────────────────────────────────────────────────────


def test_opencode_child_agent_id_is_the_session_id_when_a_parent_exists():
    assert opencode_child_agent_id("ses_child", "ses_parent") == "ses_child"


@pytest.mark.parametrize("parent", [None, "", "   "])
def test_opencode_coordinator_session_has_no_child_id(parent):
    assert opencode_child_agent_id("ses_top", parent) == ""


def test_opencode_child_without_a_session_id_is_empty():
    assert opencode_child_agent_id("", "ses_parent") == ""


# ── is_subagent ──────────────────────────────────────────────────────────────────────────


def test_is_subagent_true_for_sanitized_args_agent_id():
    assert is_subagent({"agent_id": "sub-1"}, {}) is True


@pytest.mark.parametrize("args", [None, {}, {"agent_id": ""}, {"agent_id": "  "}, {"agent_id": None}])
def test_is_subagent_false_for_blank_args_agent_id_and_clean_env(args):
    assert is_subagent(args, {}) is False


@pytest.mark.parametrize("env", [
    {"RIG_AGENT_ID": "Child"},
    {"RIG_DETACHED_AGENT": "1"},
    {"PI_SESSION_FILE": _CHILD},
    {"PI_SESSION_FILE": _GRANDCHILD},
])
def test_is_subagent_true_for_each_trusted_env_source(env):
    assert is_subagent({}, env) is True


@pytest.mark.parametrize("env", [
    {"PI_SESSION_FILE": _MAIN},   # MAIN omp session file: not nested
    {"PI_SESSION_FILE": ""},
    {"RIG_DETACHED_AGENT": "yes"},
    {"CLAUDE_CODE_CHILD_SESSION": "1"},   # =1 on the main thread of `claude -p` too
    {"CLAUDECODE": "1", "AI_AGENT": "1", "RIG_OMP_SESSION": "omp-abc", "TMUX_PANE": "%3"},
])
def test_is_subagent_false_for_non_signals(env):
    assert is_subagent({}, env) is False


def test_is_subagent_ignores_model_controlled_tool_input():
    args = {"tool_input": {"agent_id": "forged"}, "agent": "forged", "agentId": "forged"}
    assert is_subagent(args, {}) is False


def test_is_subagent_defaults_to_process_environ_but_explicit_environ_wins(monkeypatch):
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)
    monkeypatch.delenv("PI_SESSION_FILE", raising=False)
    monkeypatch.setenv("RIG_AGENT_ID", "FromProcessEnv")
    assert is_subagent({}) is True
    assert is_subagent({}, {}) is False
    assert os.environ["RIG_AGENT_ID"] == "FromProcessEnv"  # never mutated
