"""Tests for the shared guard-message builder (agent-tools#994).

A blocking guard must tell its reader WHAT TO DO NEXT, and the answer depends on WHO reads it:
a subagent self-serves then messages its coordinator (never the human); the coordinator relays
or supplies the missing thing and is the only one who may escalate to the human.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agenttools_guard_message import Remedy, Role, detect_role, render

_SESSION_DIR = "2026-10-24T10-11-12-345Z_0a1b2c3d-1111-2222-3333-444455556666"
_CHILD_FILE = f"/home/u/.omp/sessions/proj/{_SESSION_DIR}/FixPlugTip.jsonl"
_MAIN_FILE = f"/home/u/.omp/sessions/proj/{_SESSION_DIR}.jsonl"
_NESTED_FILE = f"/home/u/.omp/sessions/proj/{_SESSION_DIR}/Parent/Parent.Child.jsonl"


def _event(**args):
    return {"harness": "omp", "args": args}


def test_omp_depth1_child_parent_is_main():
    env = {"RIG_AGENT_ID": "FixPlugTip", "PI_SESSION_FILE": _CHILD_FILE}
    role = detect_role(_event(), env)
    assert role == Role(kind="subagent", agent_id="FixPlugTip", parent_id="Main", harness="omp")


def test_nested_child_parent_is_dotted_prefix():
    env = {"RIG_AGENT_ID": "Parent.Child", "PI_SESSION_FILE": _NESTED_FILE}
    role = detect_role(_event(), env)
    assert role.kind == "subagent"
    assert role.agent_id == "Parent.Child"
    assert role.parent_id == "Parent"


def test_deeply_nested_child_parent_drops_only_last_segment():
    role = detect_role(_event(agent_id="A.B.C"), {})
    assert (role.kind, role.parent_id) == ("subagent", "A.B")


def test_args_agent_id_only():
    role = detect_role(_event(agent_id="Worker"), {})
    assert role.kind == "subagent"
    assert role.agent_id == "Worker"
    assert role.parent_id == "Main"  # omp harness, depth-1


def test_session_file_alone_identifies_child():
    role = detect_role(_event(), {"PI_SESSION_FILE": _CHILD_FILE})
    assert role.kind == "subagent"
    assert role.agent_id == "FixPlugTip"
    assert role.parent_id == "Main"


def test_parent_env_override_wins():
    env = {"RIG_AGENT_ID": "Child", "RIG_PARENT_AGENT_ID": "Boss", "PI_SESSION_FILE": _CHILD_FILE}
    assert detect_role(_event(), env).parent_id == "Boss"


def test_flat_session_file_is_coordinator():
    role = detect_role(_event(), {"PI_SESSION_FILE": _MAIN_FILE})
    assert role.kind == "coordinator"
    assert role.parent_id == ""


def test_agent_id_main_is_coordinator():
    assert detect_role(_event(agent_id="Main"), {}).kind == "coordinator"
    assert detect_role(_event(), {"RIG_AGENT_ID": "Main"}).kind == "coordinator"


def test_no_signals_is_unknown():
    role = detect_role({"harness": "omp", "args": {"command": "git commit"}}, {})
    assert role.kind == "unknown"
    assert role.harness == "omp"


def test_non_omp_harness_subagent_has_no_parent():
    role = detect_role({"harness": "claude-code", "args": {"agent_id": "abc123"}}, {})
    assert role.kind == "subagent"
    assert role.parent_id == ""


def test_detached_flag_is_subagent_with_unknown_parent():
    role = detect_role({"harness": "claude-code", "args": {}}, {"RIG_DETACHED_AGENT": "1"})
    assert role.kind == "subagent"
    assert role.parent_id == ""


def test_forged_tool_input_is_ignored():
    event = {
        "harness": "omp",
        "agent_id": "Forged",
        "role": "coordinator",
        "args": {
            "role": "coordinator",
            "parent_id": "Evil",
            "tool_input": {"agent_id": "Forged", "role": "subagent"},
            "command": "git commit",
        },
    }
    assert detect_role(event, {}).kind == "unknown"


def test_forged_args_role_cannot_downgrade_a_real_subagent():
    event = _event(agent_id="Real", role="coordinator", parent_id="Evil")
    role = detect_role(event, {})
    assert role.kind == "subagent"
    assert role.parent_id == "Main"


def test_garbage_inputs_never_raise():
    assert detect_role({}, {}).kind == "unknown"
    assert detect_role({"args": "nope", "harness": 3}, {}).kind == "unknown"  # type: ignore[arg-type]
    assert detect_role({"args": {"agent_id": 7}}, {}).kind in {"subagent", "unknown"}


def test_default_env_is_process_env(monkeypatch):
    monkeypatch.setenv("RIG_AGENT_ID", "FromEnv")
    monkeypatch.delenv("PI_SESSION_FILE", raising=False)
    monkeypatch.delenv("RIG_PARENT_AGENT_ID", raising=False)
    assert detect_role(_event()).agent_id == "FromEnv"


_REMEDY = Remedy(
    what="[x] BLOCKED: no ticket.",
    self_serve="Run `task new --title T --acceptance A --why W --impact I --if-not-done N`, then recommit with `Refs #<id>`.",
    human='Genuine trivial ticketless commit only: RIG_HATCH_REQUEST_X="<why>".',
    no_bypass="As a subagent never retry with REQUIRE_TICKET_STRICT=0 or RIG_HATCH_REQUEST_* (coordinator only) and never go to the human.",
)
_SUB = Role(kind="subagent", agent_id="Kid", parent_id="Boss", harness="omp")
_COORD = Role(kind="coordinator", agent_id="Main", harness="omp")
_UNKNOWN = Role(kind="unknown")


def test_render_leads_with_what_then_self_serve():
    for role in (_SUB, _COORD, _UNKNOWN):
        text = render(_REMEDY, role)
        assert text.startswith(_REMEDY.what)
        assert text.index("1. ") < text.index("2. ")
        assert _REMEDY.self_serve in text.split("2. ")[0]


def test_render_omp_subagent_pauses_with_awaiting_coordinator_not_wait():
    # A subagent cannot block-wait (omp `wait` errors "Nothing to wait for"): the protocol is
    # message the parent, `yield` awaiting-coordinator, get revived by `write agent://<you>`.
    text = render(_REMEDY, _SUB)
    assert "`write agent://Boss` with what is missing + what you tried" in text
    assert "`yield` status `awaiting-coordinator` (NOT blocked/done/failed)" in text
    assert "not terminal" in text
    assert "`write agent://Kid`" in text  # the coordinator's revive address is the subagent's own id
    assert "WAIT" not in text and "`wait`" not in text
    assert "never retry with REQUIRE_TICKET_STRICT=0 or RIG_HATCH_REQUEST_*" in text
    assert "never go to the human" in text
    assert "ask the human" not in text.lower()
    assert "RIG_HATCH_REQUEST_X" not in text  # the coordinator's concrete hatch line


def test_render_omp_subagent_without_known_id_uses_placeholder():
    text = render(_REMEDY, Role(kind="subagent", parent_id="Main", harness="omp"))
    assert "write agent://<you>" in text


def test_render_subagent_unknown_parent_non_omp_is_generic():
    text = render(_REMEDY, Role(kind="subagent", agent_id="Kid", harness="claude-code"))
    assert "dispatcher" in text
    assert "awaiting-coordinator" in text
    assert "agent://" not in text  # no invented omp address
    assert "RIG_HATCH_REQUEST_X" not in text


def test_render_coordinator_relays_and_human_is_last():
    text = render(_REMEDY, _COORD)
    assert "write agent://<id>" in text
    assert "`wait` for its final report" in text
    assert text.rstrip().endswith(_REMEDY.human)
    assert text.index("write agent://") < text.index("RIG_HATCH_REQUEST")


def test_render_coordinator_custom_relay_replaces_generic():
    remedy = Remedy(what="W.", self_serve="S.", relay="RELAY-LINE", human="H.")
    text = render(remedy, _COORD)
    assert "RELAY-LINE" in text
    assert text.rstrip().endswith("H.")


def test_render_coordinator_without_human_has_no_escalation_line():
    text = render(Remedy(what="W.", self_serve="S."), _COORD)
    assert "human" not in text.lower()


def test_render_unknown_has_both_branches_and_human_last():
    text = render(_REMEDY, _UNKNOWN)
    assert "If you were dispatched by another agent" in text
    assert "If you are the top-level agent" in text
    assert "awaiting-coordinator" in text.split("If you are the top-level agent")[0]
    assert text.index("dispatched by another agent") < text.index("top-level agent")
    assert text.rstrip().endswith(_REMEDY.human)


@pytest.mark.parametrize("role", [_SUB, _COORD, _UNKNOWN])
def test_render_is_short_and_has_no_closing_keywords(role):
    text = render(_REMEDY, role)
    assert len(text) <= 1000
    assert "Closes" not in text and "Fixes" not in text
    assert "ASK the human" not in text


def test_defaults_are_generic_not_ticket_specific():
    remedy = Remedy(what="W.", self_serve="S.", human="H.")
    for role in (_SUB, _COORD, _UNKNOWN):
        text = render(remedy, role)
        assert "REQUIRE_TICKET" not in text and "RIG_HATCH_REQUEST" not in text
        assert "ticketless" not in text
    assert "Last resort, genuine exceptions only: H." in render(remedy, _COORD)
    assert "never use a hatch/env bypass and never go to the human" in render(remedy, _SUB)
    assert "never use a hatch/env bypass and never go to the human" in render(remedy, _UNKNOWN)


def test_last_resort_and_no_bypass_overrides():
    remedy = Remedy(what="W.", self_serve="S.", human="H.", last_resort="LR: ", no_bypass="NB.")
    assert render(remedy, _COORD).rstrip().endswith("LR: H.")
    assert "LR: H." in render(remedy, _UNKNOWN)
    assert "NB." in render(remedy, _SUB) and "NB." in render(remedy, _UNKNOWN)
    assert "NB." not in render(remedy, _COORD)


def test_coordinator_ask_is_named_in_subagent_and_unknown_step_only():
    remedy = Remedy(what="W.", self_serve="S.", human="H.", coordinator_ask="DO-THIS-THING.")
    for role in (_SUB, _UNKNOWN):
        assert "Tell the coordinator exactly what you need: DO-THIS-THING." in render(remedy, role)
    assert "DO-THIS-THING" not in render(remedy, _COORD)
    assert "Tell the coordinator" not in render(Remedy(what="W.", self_serve="S."), _SUB)
