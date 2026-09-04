"""Tests for the background-subagent-gate agent-hook (pre-agent).

Covers the doctrine's four cases: BLOCK (non-trivial foreground dispatch), ALLOW
(run_in_background true / trivial one-liner), SUBAGENT-EXEMPT (agent_id present), and the
deny-by-default Telegram hatch escalation (the old ALLOW_FOREGROUND_SUBAGENT self-service env
is DEAD; RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE with a written justification asks tg-ctl and
allows only on exit 0, a bare `1` denies).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_background_subagent_gate.py -q
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
    / "background-subagent-gate"
    / "background_subagent_gate.py"
)
_spec = importlib.util.spec_from_file_location("background_subagent_gate", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

_LONG = "x" * 300  # a clearly non-trivial single-line prompt (> 200 chars)

# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


def _run(event, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear the (now dead) old self-service env AND the hatch env so a stray ambient value can't
    # leak into a test.
    for k in ("ALLOW_FOREGROUND_SUBAGENT", "ALLOW_FOREGROUND_SUBAGENT_REASON",
              "RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = gate.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_block_non_trivial_foreground_dispatch(monkeypatch):
    out, _err, code = _run({"args": {"prompt": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "BACKGROUND" in payload["message"]


def test_allow_background_dispatch(monkeypatch):
    out, _err, code = _run({"args": {"run_in_background": True, "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_background_dispatch_string_true(monkeypatch):
    out, _err, code = _run({"args": {"run_in_background": "true", "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── fork / isolation:remote are inherently background per CC's own Agent tool contract ────
# (CC's `Agent` tool schema has no `run_in_background` property at all — see the module
# docstring — so these are the two real allow paths a non-trivial dispatch actually has.)

def test_allow_fork_dispatch_without_run_in_background(monkeypatch):
    out, _err, code = _run({"args": {"subagent_type": "fork", "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_isolation_remote_dispatch_without_run_in_background(monkeypatch):
    out, _err, code = _run({"args": {"isolation": "remote", "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_isolation_remote_with_realistic_subagent_type(monkeypatch):
    """The real production shape always carries `subagent_type` (schema-required) alongside
    `isolation`. Prove `isolation: "remote"` allows even when `subagent_type` is a normal,
    otherwise-blocking value like `general-purpose`, not just when `subagent_type` is absent."""
    out, _err, code = _run(
        {"args": {"subagent_type": "general-purpose", "isolation": "remote", "prompt": _LONG}},
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_isolation_worktree_alone_still_blocks(monkeypatch):
    """`isolation: "worktree"` is workspace isolation, not background execution — unlike
    `isolation: "remote"`, it must NOT exempt a non-trivial dispatch on its own."""
    out, _err, code = _run({"args": {"isolation": "worktree", "prompt": _LONG}}, monkeypatch)
    assert code == 10
    assert _decision(out) == "block"


def test_isolation_worktree_combined_with_fork_still_allows(monkeypatch):
    """`isolation: "worktree"` must be IGNORED by the allow logic, not an active block signal —
    combined with a real background shape (`fork`) it must still allow, proving worktree isn't
    silently overriding an otherwise-valid background dispatch.

    This rests on an assumption not independently verified here: that CC actually backgrounds a
    fork dispatch even when `isolation: "worktree"` is also set. If a future CC version makes
    worktree isolation change fork's background behavior, this test's expectation — not just the
    hook — would need revisiting."""
    out, _err, code = _run(
        {"args": {"isolation": "worktree", "subagent_type": "fork", "prompt": _LONG}}, monkeypatch
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_plain_nontrivial_dispatch_without_fork_or_remote_still_blocks(monkeypatch):
    """A non-fork, non-remote, non-trivial dispatch with no run_in_background must still
    block — this gate still enforces backgrounding, it just recognizes the real allow paths."""
    out, _err, code = _run(
        {"args": {"subagent_type": "general-purpose", "prompt": _LONG}}, monkeypatch
    )
    assert code == gate.BLOCK_EXIT_CODE
    message = json.loads(out)["message"]
    assert _decision(out) == "block"
    assert "fork" in message
    assert "NOT a real field" in message


def test_allow_trivial_one_liner(monkeypatch):
    out, _err, code = _run({"args": {"prompt": "rename foo to bar in one file"}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_subagent_exempt_allows_even_foreground(monkeypatch):
    out, _err, code = _run({"args": {"agent_id": "sub-1", "prompt": _LONG}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── regression: the OLD self-service escape hatch is DEAD ──────────────────────────────────

def test_old_env_escape_hatch_no_longer_bypasses(monkeypatch):
    """ALLOW_FOREGROUND_SUBAGENT=1 + _REASON as a real env pair must NO LONGER allow the
    foreground dispatch — the self-service bypass was removed (replaced by the Telegram hatch)."""
    out, _err, code = _run(
        {"args": {"prompt": _LONG}},
        monkeypatch,
        {"ALLOW_FOREGROUND_SUBAGENT": "1", "ALLOW_FOREGROUND_SUBAGENT_REASON": "latency probe"},
    )
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Telegram hatch escalation (deny-by-default) ────────────────────────────────────────────

def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_hatch_unset_blocks_and_names_env_var(monkeypatch):
    """No hatch env → normal block; the reminder names RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE."""
    out, _err, code = _run({"args": {"prompt": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` is not a justification → block, no tg-ctl call (fail if the fake is invoked)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(gate.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        {"args": {"prompt": _LONG}}, monkeypatch,
        {"RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE": "1"},
    )
    assert code == gate.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(gate.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        {"args": {"prompt": _LONG}}, monkeypatch,
        {"RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE": "Latency probe, must run inline now."},
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(gate.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        {"args": {"prompt": _LONG}}, monkeypatch,
        {"RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE": "Latency probe, must run inline now."},
    )
    assert code == gate.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── #6: triviality is judged on the LONGEST of prompt/description, not the first non-empty ─

def test_short_prompt_long_description_is_not_trivial(monkeypatch):
    """A short `prompt` paired with a long `description` must NOT be judged trivial — the gate
    must block the foreground dispatch. Judging only the first non-empty value let this slip
    through (#6)."""
    out, _err, code = _run({"args": {"prompt": "x", "description": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_long_prompt_short_description_is_not_trivial(monkeypatch):
    """Symmetric: a long prompt with a short description is also non-trivial → block."""
    out, _err, code = _run({"args": {"prompt": _LONG, "description": "x"}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_short_prompt_and_short_description_is_trivial(monkeypatch):
    """When BOTH are short and single-line the dispatch is trivial → allow inline."""
    out, _err, code = _run({"args": {"prompt": "rename foo", "description": "small refactor"}},
                           monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_multiline_description_is_not_trivial(monkeypatch):
    """A multi-line description (even if short per line) is non-trivial → block (#6)."""
    out, _err, code = _run({"args": {"prompt": "x", "description": "step 1\nstep 2"}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── #11: description-only (no prompt) is judged on its own length ─────────────────────────

def test_description_only_long_is_not_trivial(monkeypatch):
    """A dispatch carrying only a long `description` (no prompt) must block (#11)."""
    out, _err, code = _run({"args": {"description": _LONG}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_description_only_short_is_trivial(monkeypatch):
    """A short description-only dispatch is trivial → allow (#11)."""
    out, _err, code = _run({"args": {"description": "tidy imports in one file"}}, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
