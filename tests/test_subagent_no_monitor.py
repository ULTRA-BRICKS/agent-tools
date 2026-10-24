"""Tests for the subagent-no-monitor agent-hook (pre-monitor, hard block).

The wedge this kills (HYP-1350's retrospective, hyperide/hyper-saas PR #754): a dispatched
SUBAGENT calls the Monitor tool — CC's fire-and-forget background event-stream watch, e.g. on
its own spawned Bash `run_in_background` child — then ends its turn awaiting a Monitor-event
notification it will never receive (only the main loop is re-invoked by such notifications),
wedging forever. Sibling of `subagent-no-bg-longproc` (agent-tools#52) for a different CC tool.

Covers: BLOCK (any subagent call to Monitor, unconditionally — there's no foreground/background
axis to classify since Monitor IS the background primitive), ALLOW (the orchestrator's own
Monitor use — no agent_id), the top-level agent_id fallback, and the deny-by-default Telegram
hatch escalation (RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR with a written justification asks
tg-ctl and allows only on an explicit allow reply; a bare `1` denies without contacting Telegram).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_subagent_no_monitor.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "subagent-no-monitor"
    / "subagent_no_monitor.py"
)
_spec = importlib.util.spec_from_file_location("subagent_no_monitor", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# The sibling hook, loaded so the two remedies BLOCK_SELF_SERVE recommends can be run through the
# REAL sibling gate (not merely asserted as strings) — see
# test_remedy_1_run_in_background_ordinary_command_passes_sibling_gate and
# test_remedy_2_heartbeat_loop_passes_sibling_gate below.
_SIBLING_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "subagent-no-bg-longproc"
    / "subagent_no_bg_longproc.py"
)
_sibling_spec = importlib.util.spec_from_file_location("subagent_no_bg_longproc", _SIBLING_HOOK)
assert _sibling_spec and _sibling_spec.loader
sibling_hook = importlib.util.module_from_spec(_sibling_spec)
_sibling_spec.loader.exec_module(sibling_hook)


def _run(
    monkeypatch,
    *,
    agent_id="sub-1",
    description="watch tests",
    env: dict | None = None,
    event: dict | None = None,
) -> tuple[str, str, int]:
    if event is None:
        args = {"description": description}
        if agent_id is not None:
            args["agent_id"] = agent_id
        event = {"args": args}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: a subagent calling Monitor at all (the wedge) ─────────────────────────────────

def test_block_subagent_monitor_call(monkeypatch):
    out, _e, code = _run(monkeypatch, agent_id="sub-1")
    assert code == hook.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "SUBAGENT" in payload["message"]
    assert "Monitor" in payload["message"]


def test_block_regardless_of_what_is_watched(monkeypatch):
    """No 'own child process' special-casing — every subagent Monitor call is the wedge."""
    out, _e, code = _run(monkeypatch, agent_id="sub-1", description="watch some other service")
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_agent_id_top_level_event_fallback(monkeypatch):
    """`agent_id` may be surfaced at the top level of the event (not under args) → still
    treated as a subagent and BLOCK. Symmetric with subagent-no-bg-longproc's own test."""
    event = {"args": {"description": "watch tests"}, "agent_id": "sub-top"}
    out, _e, code = _run(monkeypatch, event=event)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_message_names_the_synchronous_poll_alternative(monkeypatch):
    out, _e, _c = _run(monkeypatch, agent_id="sub-1")
    msg = json.loads(out)["message"]
    assert "foreground" in msg.lower()
    assert "timeout" in msg


def test_block_message_names_run_in_background_for_a_single_command(monkeypatch):
    """The first alternative: a single long-running command should use the Bash tool's
    own `run_in_background: true`, which the harness auto-resumes the subagent from —
    Monitor is never the right tool even for this shape."""
    out, _e, _c = _run(monkeypatch, agent_id="sub-1")
    msg = json.loads(out)["message"]
    assert "run_in_background" in msg
    assert "auto-resume" in msg.lower()


def test_block_message_names_the_bounded_heartbeat_loop(monkeypatch):
    """The second alternative (anything else): a foreground heartbeat loop that echoes at
    least every ~20s and stays under the Bash tool's own ~600s hard cap per call — NOT a
    single unbounded `timeout 900` call, which would exceed that cap and never fire."""
    out, _e, _c = _run(monkeypatch, agent_id="sub-1")
    msg = json.loads(out)["message"]
    assert "20" in msg  # echo cadence
    assert "540" in msg  # per-call bound, safely under the Bash tool's 600s cap
    assert "echo" in msg


def _run_sibling(command, monkeypatch, *, run_in_background=None) -> tuple[str, int]:
    """Drive the REAL `subagent-no-bg-longproc` sibling hook with a subagent event, exactly as
    a subagent following BLOCK_SELF_SERVE's printed remedy would trigger it — proves the remedy is
    a legal move, not just a string this test file asserts appears in the message."""
    args = {"command": command, "agent_id": "sub-1"}
    if run_in_background is not None:
        args["run_in_background"] = run_in_background
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in ("ALLOW_SUBAGENT_BACKGROUND", "ALLOW_SUBAGENT_BACKGROUND_REASON",
              "RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC"):
        monkeypatch.delenv(k, raising=False)
    code = sibling_hook.main()
    return out.getvalue(), code


def test_remedy_1_run_in_background_ordinary_command_passes_sibling_gate(monkeypatch):
    """Remedy (1) — an ORDINARY (non-labeled) command backgrounded with run_in_background:true —
    must actually be ALLOWED by subagent-no-bg-longproc, or the printed remedy pinballs a
    subagent between the two gates with no legal move (agent-tools#546 follow-up context)."""
    out, code = _run_sibling("curl -sS https://example.com/status", monkeypatch,
                             run_in_background=True)
    assert code == 0, out
    assert json.loads(out)["decision"] == "allow"


def test_remedy_2_heartbeat_loop_passes_sibling_gate(monkeypatch):
    """Remedy (2) — the literal foreground heartbeat-loop command from BLOCK_SELF_SERVE — must
    also be ALLOWED by subagent-no-bg-longproc (it is foreground, and its invoked head is
    `timeout`->`bash`, not a labeled long-process command at argv[0])."""
    remedy_2 = (
        'timeout 540 bash -c \'i=0; until <condition-check> || [ "$i" -ge 26 ]; do sleep 20; '
        'i=$((i+1)); echo "[wait] tick $i ($((i*20))s)"; done\''
    )
    out, code = _run_sibling(remedy_2, monkeypatch)
    assert code == 0, out
    assert json.loads(out)["decision"] == "allow"


# ── ALLOW: the orchestrator's own Monitor use (no agent_id) ──────────────────────────────

def test_allow_orchestrator_monitor_call(monkeypatch):
    out, _e, code = _run(monkeypatch, agent_id=None)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_orchestrator_empty_agent_id(monkeypatch):
    """An empty-string agent_id must not be treated as a subagent signal (mirrors the
    `_is_subagent` `.strip()` guard in the sibling hook)."""
    out, _e, code = _run(monkeypatch, agent_id="   ")
    assert code == 0
    assert _decision(out) == "allow"


# ── fail-open & robustness ────────────────────────────────────────────────────────────────

def test_unparseable_stdin_fails_open(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert _decision(out.getvalue()) == "allow"


# ── Telegram hatch escalation (deny-by-default) ───────────────────────────────────────────

def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves
# (agent-tools#513). SYNC: identical to subagent-no-bg-longproc's own `_ALLOW_REPLY_SH`.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


def test_block_message_hides_the_hatch_from_the_blocked_subagent(monkeypatch):
    """agent-tools#994: the hatch is the coordinator's last resort; a subagent is told to message
    its coordinator instead. The hatch itself still works (tests below)."""
    out, _e, code = _run(monkeypatch, agent_id="sub-1")
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR" not in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(monkeypatch, agent_id="sub-1",
                         env={"RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR": "1"})
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_inline_form_in_description_does_not_self_arm(tmp_path, monkeypatch):
    """A blocked subagent cannot re-arm the hatch by writing an inline
    `RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR="..."` assignment into the free-form, model-authored
    `description` Monitor field. That inline-parse path only ever fires against a real Bash
    `command=` string passed to `request_hatch_approval` (pre-bash hooks only, parsing the
    documented `VAR=value <gated-command>` shell prefix) -- Monitor has no shell command string,
    so this hook must never pass the watched description as `command=`. With the env var unset,
    the described self-arm attempt must still deny with no tg-ctl contact."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        monkeypatch, agent_id="sub-1",
        description='RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR="self-authored justification" watch tests',
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        monkeypatch, agent_id="sub-1",
        env={"RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR": "self-managed watchdog, polls inline"},
    )
    assert code == 0 and _decision(out) == "allow", (out, _e)
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        monkeypatch, agent_id="sub-1",
        env={"RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR": "self-managed watchdog, polls inline"},
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── role-aware escalation ladder (agent-tools#994) ───────────────────────────────────────

def _ladder(monkeypatch, *, harness="omp") -> str:
    for key in ("RIG_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE"):
        monkeypatch.delenv(key, raising=False)
    event: dict = {"args": {"description": "watch tests", "agent_id": "Sub"}}
    if harness:
        event["harness"] = harness
    out, _err, code = _run(monkeypatch, event=event)
    assert code == hook.BLOCK_EXIT_CODE
    return json.loads(out)["message"]


def test_ladder_subagent_self_serve_first_then_coordinator_never_human(monkeypatch):
    message = _ladder(monkeypatch)
    lines = message.splitlines()
    assert lines[0].startswith("You are a SUBAGENT")
    assert lines[1].startswith("1. ") and hook.HEARTBEAT_LOOP_EXAMPLE in lines[1]
    assert "write agent://Main" in message and "awaiting-coordinator" in message
    assert "ASK the human" not in message
    assert "RIG_HATCH_REQUEST" not in message


def test_ladder_subagent_detected_via_top_level_agent_id(monkeypatch):
    for key in ("RIG_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE"):
        monkeypatch.delenv(key, raising=False)
    out, _err, code = _run(
        monkeypatch, event={"args": {"description": "w"}, "agent_id": "Sub", "harness": "omp"}
    )
    assert code == hook.BLOCK_EXIT_CODE
    message = json.loads(out)["message"]
    assert "awaiting-coordinator" in message and "RIG_HATCH_REQUEST" not in message


def test_ladder_falls_back_when_the_lib_is_missing(monkeypatch):
    monkeypatch.setattr(hook, "_guard_message", None)
    message = _ladder(monkeypatch)
    lines = message.splitlines()
    assert lines[0].startswith("You are a SUBAGENT")
    assert lines[1].startswith("1. ") and hook.HEARTBEAT_LOOP_EXAMPLE in lines[1]
    assert "awaiting-coordinator" in message
    assert "ASK the human" not in message
    assert "RIG_HATCH_REQUEST" not in message
