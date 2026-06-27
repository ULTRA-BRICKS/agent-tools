"""Tests for the no-long-inline-process agent-hook (pre-bash, hard block).

Covers the doctrine's four cases: BLOCK (review / --watch / build-test suite / long sleep),
ALLOW (short sleep, a path that merely contains "review", a benign read), SUBAGENT-EXEMPT
(agent_id present), and the ESCAPE hatch (env+reason and inline sentinel; reasonless still
blocks).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_no_long_inline_process.py -q
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
    / "no-long-inline-process"
    / "no_long_inline_process.py"
)
_spec = importlib.util.spec_from_file_location("no_long_inline_process", _HOOK)
assert _spec and _spec.loader
nlip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nlip)


def _run(command, monkeypatch, *, agent_id=None, env: dict | None = None) -> tuple[str, str, int]:
    args = {"command": command}
    if agent_id is not None:
        args["agent_id"] = agent_id
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_INLINE_PROCESS", "ALLOW_INLINE_PROCESS_REASON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = nlip.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "review",
    "review -C /repo",
    "review diff -C /repo -m claude:claude-opus-4-8",  # the real diff-review invocation
    "gh pr checks 42 --watch",
    "vitest --watch",
    "npm test",
    "pnpm build",
    "pytest tests/",
    "cargo test",
    "go build ./...",
    "make all",
    "sleep 30",
    "sleep 5m",                      # 5 minutes — a bare \\d+ would read this as 5 and pass
    "sleep 1h",                      # 1 hour
    "sleep 600",                     # 10 minutes
])
def test_block_long_running(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "BACKGROUND" in payload["message"]


# ── ALLOW ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "sleep 2",                       # short sleep is fine
    "sleep 9",                       # just under the N>=10 threshold
    "sleep 5s",                      # 5 seconds with an explicit unit is still short
    'echo "sleep 100"',              # the word inside a string is not a sleep command
    "cat docs/review-notes.md",      # "review" only as a path substring
    "ls src/review/",                # "review" only in a dir name
    "git status",                    # plain inspection
    "echo done",
])
def test_allow_benign(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── #60: a long-process KEYWORD inside a QUOTED ARGUMENT to a DIFFERENT command must ALLOW ──
# The old raw regex anchored on shell-separator chars (`(`/`|`/`;`/`&&`) and a bare `--watch`
# substring; those chars also appear INSIDE quoted argument strings, so a benign report/commit/
# echo whose TEXT mentioned review/--watch/npm test/sleep was mis-read as the invocation and
# BLOCKED. argv-awareness flags only the REAL invoked command (argv[0]), so these now ALLOW.

@pytest.mark.parametrize("command", [
    'tg --tag report --title x "text with review and review-qa in it"',
    'tg --tag report --title "x" "we ran review-qa (review) today"',  # `(review` inside quotes
    'echo "run review later"',
    'git commit -m "add review gate"',
    'git commit -m "fix; npm test now green"',          # `;` + suite inside the quoted message
    'git commit -m "wire --watch flag"',                # `--watch` substring inside the message
    'tg "status: (sleep 600 elapsed) done"',            # `(sleep 600` inside the quoted arg
    'echo "ci runs | pytest fast"',                     # `|` + runner inside the quoted arg
    'tg --photo p.png "review the screenshot in review-qa"',
])
def test_allow_keyword_in_quoted_argument(command, monkeypatch):
    """#60: review / --watch / npm test / sleep appearing inside a quoted argument to a DIFFERENT
    command (tg / echo / git commit -m) must NOT trip the gate — only a real invocation does."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── codex: WRAPPED long processes (timeout/env/nice/time/…) must still BLOCK ─────────────

@pytest.mark.parametrize("command", [
    "timeout 600 npm test",          # timeout + duration wraps the suite
    "timeout 5m review",             # duration with a unit suffix
    "timeout -k 5 600 pytest",       # timeout with its own -k flag + value, then duration
    "env CI=1 pytest",               # env + a KEY=VALUE assignment
    "env CI=1 NODE_ENV=test vitest",  # multiple env assignments
    "nice -n10 review -C /repo",     # nice + joined -n10
    "nice -n 10 npm run build",      # nice + separated -n 10
    "time make build",              # bare wrapper, no args of its own
    "stdbuf -oL pytest tests/",      # stdbuf + a flag, no positional
    "nohup cargo build",            # nohup wraps directly
    "git pull && timeout 600 npm test",  # wrapper on a non-head segment
])
def test_block_wrapped_long_process(command, monkeypatch):
    """codex: ``_matched_long_process`` anchored on the runner and missed common wrappers, so
    ``timeout 600 npm test`` / ``env CI=1 pytest`` / ``timeout 5m review`` slipped through. The
    wrapper is now peeled off each segment before matching → these BLOCK."""
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    assert json.loads(out)["decision"] == "block"


@pytest.mark.parametrize("command", [
    "timeout 5 ls",                  # a wrapped SHORT/benign command is not long-running
    "env EDITOR=vim git status",     # env wrapping a benign command
    "nice -n10 git log",             # nice wrapping a benign command
    "time true",                    # time wrapping a no-op
    "timeout 30 cat big.log",        # a wrapped non-suite command
])
def test_allow_wrapped_benign_command(command, monkeypatch):
    """Unwrapping must not over-block: a wrapper in front of a BENIGN command is still allowed —
    only the wrapped command's own long-running-ness decides."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── SUBAGENT-EXEMPT ────────────────────────────────────────────────────────────────────

def test_subagent_exempt_allows_long_process(monkeypatch):
    out, _e, code = _run("npm test", monkeypatch, agent_id="sub-3")
    assert code == 0
    assert _decision(out) == "allow"


def test_subagent_exempt_allows_review(monkeypatch):
    """The same real `review` that blocks for the orchestrator is allowed inside a subagent."""
    out, _e, code = _run("review diff -C /repo", monkeypatch, agent_id="sub-7")
    assert code == 0
    assert _decision(out) == "allow"


# ── ESCAPE ─────────────────────────────────────────────────────────────────────────────

def test_escape_env_reason_allows(monkeypatch):
    out, _e, code = _run(
        "review", monkeypatch,
        env={"ALLOW_INLINE_PROCESS": "1", "ALLOW_INLINE_PROCESS_REASON": "one-shot probe"},
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_escape_inline_sentinel_allows(monkeypatch):
    out, _e, code = _run("npm test  # inline-process-ok: single fast file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_reasonless_override_still_blocks(monkeypatch):
    out, _e, code = _run("review", monkeypatch, env={"ALLOW_INLINE_PROCESS": "1"})  # no reason
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
