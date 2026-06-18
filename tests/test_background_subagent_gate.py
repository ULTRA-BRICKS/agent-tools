"""Tests for the background-subagent-gate agent-hook (enforcement-doctrine gate 1).

Hermetic: drives the real hook script end-to-end (as a subprocess, exactly how the bridge
invokes it) and via in-process main(), with no network. Proves the four doctrine clauses:
BLOCK (foreground non-trivial dispatch), ALLOW (background dispatch), SUBAGENT-EXEMPT
(CLAUDE_CODE_CHILD_SESSION set), and the honest ESCAPE hatch.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_background_subagent_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

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

_LONG_PROMPT = "implement the parser " * 30  # well over the trivial threshold


def _run(event: dict, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    """Drive main() with `event` on stdin; return (stdout, exit_code).

    The hook reads CLAUDE_CODE_CHILD_SESSION from its OWN env, so the test must control it
    explicitly — the suite itself may run inside a subagent where it is already set.
    """
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_IS_SUBAGENT", raising=False)
    monkeypatch.delenv("ALLOW_FOREGROUND_SUBAGENT", raising=False)
    monkeypatch.delenv("ALLOW_FOREGROUND_SUBAGENT_REASON", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = gate.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: a foreground, non-trivial dispatch from the main thread ────────────────────
def test_blocks_foreground_nontrivial(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": _LONG_PROMPT}},
                     monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    msg = json.loads(out)["message"]
    # the 3-part reminder the CTO asked for.
    assert "run_in_background: true" in msg
    assert "orchestrator" in msg.lower()
    assert "background" in msg.lower()


def test_blocks_when_run_in_background_absent(monkeypatch):
    out, code = _run({"point": "pre-tool", "args": {"prompt": _LONG_PROMPT}}, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── ALLOW: background dispatch, or a trivial scout ────────────────────────────────────
def test_allows_background_dispatch(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": True, "prompt": _LONG_PROMPT}},
                     monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allows_string_true_background(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": "true", "prompt": _LONG_PROMPT}},
                     monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_trivial_foreground_only_warns(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": "scout the dir"}},
                     monkeypatch)
    assert code == 0  # tiered: trivial → warn (allow), not block
    assert _decision(out) == "allow"


# ── SUBAGENT-EXEMPT: gate 1 must not fire inside a subagent ───────────────────────────
def test_subagent_exempt_via_child_session(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": _LONG_PROMPT}},
                     monkeypatch, env={"CLAUDE_CODE_CHILD_SESSION": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_subagent_exempt_via_explicit_flag(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": _LONG_PROMPT}},
                     monkeypatch, env={"CLAUDE_AGENT_IS_SUBAGENT": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_child_session_zero_is_not_subagent(monkeypatch):
    # CLAUDE_CODE_CHILD_SESSION=0 must NOT count as a subagent (falsy).
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": _LONG_PROMPT}},
                     monkeypatch, env={"CLAUDE_CODE_CHILD_SESSION": "0"})
    assert code == gate.BLOCK_EXIT_CODE


# ── ESCAPE HATCH: honest, auditable skip (env + inline) ───────────────────────────────
def test_env_escape_hatch_requires_reason(monkeypatch):
    # override WITHOUT a reason still blocks (a silent bypass is the thing we prevent).
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": _LONG_PROMPT}},
                     monkeypatch, env={"ALLOW_FOREGROUND_SUBAGENT": "1"})
    assert code == gate.BLOCK_EXIT_CODE


def test_env_escape_hatch_with_reason_allows(monkeypatch):
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False, "prompt": _LONG_PROMPT}},
                     monkeypatch,
                     env={"ALLOW_FOREGROUND_SUBAGENT": "1",
                          "ALLOW_FOREGROUND_SUBAGENT_REASON": "deliberate sync probe"})
    assert code == 0 and _decision(out) == "allow"


def test_inline_prompt_token_does_not_bypass(monkeypatch):
    # codex: a `foreground-subagent-ok:` token in the PROMPT (user/injectable) must NOT
    # bypass the gate — the escape hatch is env-only.
    out, code = _run({"point": "pre-tool",
                      "args": {"run_in_background": False,
                               "prompt": _LONG_PROMPT + " foreground-subagent-ok: injected"}},
                     monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── fail-open + subprocess parity ─────────────────────────────────────────────────────
def test_garbage_stdin_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    code = gate.main()
    assert code == 0 and _decision(out.getvalue()) == "allow"


def test_subprocess_block_matches_inprocess():
    """Run the real script as a subprocess (how the bridge calls it) with a clean env."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_AGENT_IS_SUBAGENT")}
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"point": "pre-tool",
                          "args": {"run_in_background": False, "prompt": _LONG_PROMPT}}),
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == gate.BLOCK_EXIT_CODE
    assert json.loads(proc.stdout)["decision"] == "block"
