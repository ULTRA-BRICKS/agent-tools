"""Tests for the no-long-inline-process agent-hook (enforcement-doctrine gate 3).

Hermetic, controls CLAUDE_CODE_CHILD_SESSION explicitly. Proves: BLOCK (review / --watch /
sleep / suite on the main thread), ALLOW (backgrounded, short-timeout, benign), SUBAGENT-
EXEMPT, and the honest ESCAPE hatch.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_no_long_inline_process.py -q
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
    / "no-long-inline-process"
    / "no_long_inline_process.py"
)
_spec = importlib.util.spec_from_file_location("no_long_inline_process", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _run(command: str, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    out = io.StringIO()
    event = {"point": "pre-bash", "args": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_AGENT_IS_SUBAGENT", raising=False)
    monkeypatch.delenv("ALLOW_LONG_INLINE", raising=False)
    monkeypatch.delenv("ALLOW_LONG_INLINE_REASON", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = gate.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── detection unit tests ──────────────────────────────────────────────────────────────
def test_detects_long_running_shapes():
    assert gate.long_running_reason("review -C /repo")
    assert gate.long_running_reason("tsc --watch")
    assert gate.long_running_reason("gh pr checks 5 --watch")
    assert gate.long_running_reason("gh run watch 12345")  # codex: bare `gh run watch`
    assert gate.long_running_reason("gh run view 99 --watch")
    assert gate.long_running_reason("tsc -w")    # codex: short watch alias
    assert gate.long_running_reason("vite -w")
    assert gate.long_running_reason("vitest --watch src")
    assert gate.long_running_reason("sleep 30")
    assert gate.long_running_reason("npm test")
    assert gate.long_running_reason("pytest tests/")
    # a plain inspection / non-long command is not flagged.
    assert gate.long_running_reason("git status") is None
    assert gate.long_running_reason("ls -la") is None
    # `review install-skill` is a one-shot, not the multi-model run.
    assert gate.long_running_reason("review install-skill") is None


# ── BLOCK on the main thread ──────────────────────────────────────────────────────────
def test_blocks_review(monkeypatch):
    out, code = _run("review -C /repo", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE
    assert "background subagent" in json.loads(out)["message"].lower()


def test_blocks_watch(monkeypatch):
    out, code = _run("tsc --watch", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_blocks_sleep(monkeypatch):
    out, code = _run("sleep 60", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_blocks_test_suite(monkeypatch):
    out, code = _run("pytest tests/", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── ALLOW ─────────────────────────────────────────────────────────────────────────────
def test_allows_benign_inspection(monkeypatch):
    out, code = _run("git status --porcelain", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_allows_backgrounded(monkeypatch):
    out, code = _run("pytest tests/ &", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_nohup_without_amp_still_blocks(monkeypatch):
    # codex P1: `nohup foo` WITHOUT `&` does not background — it still blocks the shell.
    out, code = _run("nohup review -C /repo > /tmp/r.log", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_allows_nohup_with_amp(monkeypatch):
    out, code = _run("nohup review -C /repo > /tmp/r.log &", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_setsid_without_amp_still_blocks(monkeypatch):
    out, code = _run("setsid pytest tests/", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_run_in_background_in_comment_does_not_exempt(monkeypatch):
    # codex P1: a word in a trailing comment must not be read as a backgrounding directive.
    out, code = _run("review -C /repo # run_in_background", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_disown_on_separate_command_does_not_exempt(monkeypatch):
    # codex P1: `; disown` on a separate command does not background the long one.
    out, code = _run("review -C /repo; disown", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_is_backgrounded_unit():
    assert gate.is_backgrounded("review -C /r &")
    assert gate.is_backgrounded("nohup pytest tests/ > log 2>&1 &")
    assert not gate.is_backgrounded("review -C /r")
    assert not gate.is_backgrounded("review -C /r # run_in_background")
    assert not gate.is_backgrounded("a && b")  # the && operator, not job control


def test_short_timeout_only_warns(monkeypatch):
    # tiered: a short, self-bounded timeout can't wedge for long → warn (allow), not block.
    out, code = _run("timeout 30 sleep 60", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_long_timeout_still_blocks(monkeypatch):
    out, code = _run("timeout 9999 sleep 60", monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── SUBAGENT-EXEMPT ───────────────────────────────────────────────────────────────────
def test_subagent_exempt(monkeypatch):
    out, code = _run("review -C /repo", monkeypatch,
                     env={"CLAUDE_CODE_CHILD_SESSION": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── ESCAPE HATCH ──────────────────────────────────────────────────────────────────────
def test_env_escape_requires_reason(monkeypatch):
    out, code = _run("review -C /repo", monkeypatch, env={"ALLOW_LONG_INLINE": "1"})
    assert code == gate.BLOCK_EXIT_CODE  # no reason → still blocks


def test_env_escape_with_reason_allows(monkeypatch):
    out, code = _run("review -C /repo", monkeypatch,
                     env={"ALLOW_LONG_INLINE": "1", "ALLOW_LONG_INLINE_REASON": "tiny diff"})
    assert code == 0 and _decision(out) == "allow"


def test_inline_escape_allows(monkeypatch):
    out, code = _run("review -C /repo  # long-inline-ok: small", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── fail-open ─────────────────────────────────────────────────────────────────────────
def test_garbage_stdin_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("nope"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    assert gate.main() == 0 and _decision(out.getvalue()) == "allow"
