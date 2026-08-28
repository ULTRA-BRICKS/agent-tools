"""Tests for the enterworktree-foreign-guard agent-hook (pre-worktree-enter, hard block).

The wedge this kills: an agent (orchestrator or a dispatched subagent) calls
`EnterWorktree(path=<a worktree a DIFFERENT agent created>)`. The tool's own validation only
confirms the path is a real, registered worktree of the repo — never that the caller owns it —
so the call reports SUCCESS and then permanently bricks the calling agent's Bash tool for the
rest of its session, with no recovery path (not even ExitWorktree). Confirmed at least four
times in one project's session history (HYP-1384's retrospective).

Covers: BLOCK (a foreign `agent-<id>` worktree, from a mismatched subagent or from the
orchestrator with no agent_id, including a nested path inside a foreign worktree), ALLOW
(re-entering your own worktree, a brand-new worktree via `name`, a path that doesn't match the
`agent-<id>` convention at all), and the deny-by-default Telegram hatch escalation. This file
tests the HOOK SCRIPT in isolation (it trusts `event["args"]["agent_id"]`/`event["agent_id"]`
as already-resolved input) — the T2 trust boundary that makes a forged `args.agent_id` unable
to fake ownership (`cc_hook_bridge` strips it whenever CC's own top-level `agent_id` is absent)
is enforced and tested one layer up, in `tests/test_cc_hook_bridge.py`.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_enterworktree_foreign_guard.py -q
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
    / "enterworktree-foreign-guard"
    / "enterworktree_foreign_guard.py"
)
_spec = importlib.util.spec_from_file_location("enterworktree_foreign_guard", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run(
    monkeypatch,
    *,
    path="/repo/.claude/worktrees/agent-deadbeef01",
    name=None,
    agent_id="sub-1",
    env: dict | None = None,
    event: dict | None = None,
) -> tuple[str, str, int]:
    if event is None:
        args: dict = {}
        if path is not None:
            args["path"] = path
        if name is not None:
            args["name"] = name
        if agent_id is not None:
            args["agent_id"] = agent_id
        event = {"args": args}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("RIG_HATCH_REQUEST_ENTERWORKTREE_FOREIGN_GUARD", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: entering a worktree a DIFFERENT agent owns ────────────────────────────────────

def test_block_subagent_entering_foreign_worktree(monkeypatch):
    out, _e, code = _run(monkeypatch, path="/repo/.claude/worktrees/agent-deadbeef01", agent_id="sub-1")
    assert code == hook.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "agent-deadbeef01" in payload["message"]
    assert "gh pr checkout" in payload["message"]


def test_block_orchestrator_entering_any_agent_worktree(monkeypatch):
    """The orchestrator (no agent_id) never owns a dispatched subagent's worktree either."""
    out, _e, code = _run(monkeypatch, path="/repo/.claude/worktrees/agent-deadbeef01", agent_id=None)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_nested_path_inside_foreign_worktree(monkeypatch):
    """A `path` pointing INSIDE a foreign worktree (not just its root) is still recognized."""
    out, _e, code = _run(
        monkeypatch,
        path="/repo/.claude/worktrees/agent-deadbeef01/vscode-extension/hypercanvas-preview",
        agent_id="sub-1",
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_message_names_the_safe_alternative(monkeypatch):
    out, _e, _c = _run(monkeypatch, agent_id="sub-1")
    msg = json.loads(out)["message"]
    assert "gh pr checkout" in msg
    assert "own" in msg.lower()


def test_agent_id_top_level_event_fallback(monkeypatch):
    """`agent_id` may be surfaced at the top level of the event (not under args)."""
    event = {"args": {"path": "/repo/.claude/worktrees/agent-deadbeef01"}, "agent_id": "sub-top"}
    out, _e, code = _run(monkeypatch, event=event)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── ALLOW: legitimate EnterWorktree uses ──────────────────────────────────────────────────

def test_allow_reentering_own_worktree(monkeypatch):
    out, _e, code = _run(
        monkeypatch, path="/repo/.claude/worktrees/agent-sub-1", agent_id="sub-1"
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_creating_brand_new_worktree(monkeypatch):
    """No `path` (creating fresh via `name`) is always the caller's own — untouched."""
    out, _e, code = _run(monkeypatch, path=None, name="my-new-worktree", agent_id=None)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_path_not_matching_agent_convention(monkeypatch):
    """A worktree that doesn't follow the `agent-<id>` naming convention at all is out of
    this guard's understood scope — fail open rather than guess."""
    out, _e, code = _run(
        monkeypatch, path="/repo/.claude/worktrees/my-custom-worktree", agent_id="sub-1"
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_empty_agent_id_in_path_segment_not_matched(monkeypatch):
    """A short/malformed `agent-` segment (below the 6-hex-char floor) does not match."""
    out, _e, code = _run(monkeypatch, path="/repo/.claude/worktrees/agent-a1", agent_id=None)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_blank_path(monkeypatch):
    out, _e, code = _run(monkeypatch, path="   ", agent_id="sub-1")
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


def test_hatch_unset_blocks_and_names_env_var(monkeypatch):
    out, _e, code = _run(monkeypatch, agent_id="sub-1")
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_ENTERWORKTREE_FOREIGN_GUARD" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        monkeypatch, agent_id="sub-1",
        env={"RIG_HATCH_REQUEST_ENTERWORKTREE_FOREIGN_GUARD": "1"},
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nprintf approved\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        monkeypatch, agent_id="sub-1",
        env={"RIG_HATCH_REQUEST_ENTERWORKTREE_FOREIGN_GUARD": "resuming my own worktree after a session restart"},
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        monkeypatch, agent_id="sub-1",
        env={"RIG_HATCH_REQUEST_ENTERWORKTREE_FOREIGN_GUARD": "resuming my own worktree after a session restart"},
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()
