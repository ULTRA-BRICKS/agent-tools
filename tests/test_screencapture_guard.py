"""Tests for agent-hooks/screencapture-guard/screencapture_guard.py.

Covers the G-6 PreToolUse contract from the 2026-07-01 agent-ecosystem
retrospective (section 5.2.3 item 2): block a real screencapture invocation
and point at Playwright/CDP; do not false-block a command that only mentions
the word.

Run from the repo root::

    uv run --with "pytest>=8,<9" python -m pytest tests/test_screencapture_guard.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "screencapture-guard"
    / "screencapture_guard.py"
)
_spec = importlib.util.spec_from_file_location("screencapture_guard", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

_HERMETIC_CWD = tempfile.mkdtemp(prefix="scg-hermetic-")


def _run(command: str, monkeypatch) -> tuple[str, str, int]:
    event: dict = {"args": {"command": command}, "cwd": _HERMETIC_CWD}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_hook_is_directly_executable(tmp_path):
    event = {"cwd": str(tmp_path), "args": {"command": "screencapture /tmp/x.png"}}
    proc = subprocess.run(
        [str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=5,
        check=False,
    )
    assert proc.returncode == hook.BLOCK_EXIT_CODE, (proc.returncode, proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["decision"] == "block"


@pytest.mark.parametrize(
    "command",
    [
        "screencapture /tmp/x.png",
        "screencapture -x /tmp/x.png",
        "/usr/sbin/screencapture -x /tmp/x.png",
        "/usr/bin/screencapture out.png",
    ],
)
def test_block_direct_screencapture(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_message_names_playwright_or_cdp(monkeypatch):
    out, _err, _code = _run("screencapture /tmp/x.png", monkeypatch)
    msg = json.loads(out)["message"].lower()
    assert "playwright" in msg or "cdp" in msg


@pytest.mark.parametrize(
    "command",
    [
        "timeout 5 screencapture /tmp/x.png",
        "sudo screencapture /tmp/x.png",
        "env screencapture /tmp/x.png",
        "env FOO=1 screencapture /tmp/x.png",
        "nice screencapture /tmp/x.png",
    ],
)
def test_block_wrapper_prefixed_screencapture(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_behind_and_and(monkeypatch):
    out, _err, code = _run("echo hi && screencapture /tmp/x.png", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_behind_semicolon(monkeypatch):
    out, _err, code = _run("cd /tmp; screencapture /tmp/x.png", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo screencapture",
        'git commit -m "never screencapture"',
        "agent-browser screenshot /tmp/x.png",
        "git status",
        "rg screencapture",
    ],
)
def test_allow_mention_or_unrelated(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_malformed_event_blocks(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_missing_command_field_allows(monkeypatch):
    event = {"args": {}, "cwd": _HERMETIC_CWD}
    buf, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert _decision(buf.getvalue()) == "allow"

@pytest.mark.parametrize(
    "command",
    [
        "2>/dev/null screencapture /tmp/x.png",
        ">/tmp/log /usr/sbin/screencapture out.png",
        "screencapture /tmp/x.png >/dev/null",
    ],
)
def test_block_leading_redirection(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_screencapture_inside_heredoc_body(monkeypatch):
    command = "cat <<'EOF'\nscreencapture is forbidden\nEOF"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"
