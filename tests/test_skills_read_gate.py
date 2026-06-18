"""Tests for the skills-read-gate agent-hook (enforcement-doctrine gate 4).

Hermetic: isolates the marker dir. Proves the satisfiable record→check loop: a commit /
code write with mandatory skills UNREAD warns then BLOCKS; a real Skill-tool invocation
RECORDS the skill (the honest action) and clears the gate; the honest ESCAPE hatch; and that
the gate applies to subagents too (NOT exempt).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_skills_read_gate.py -q
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
    / "skills-read-gate"
    / "skills_read_gate.py"
)
_spec = importlib.util.spec_from_file_location("skills_read_gate", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

SID = "sess-xyz"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    monkeypatch.setattr(gate, "MARKER_DIR", d)
    # only require ONE mandatory skill, deterministically.
    monkeypatch.setenv("SKILLS_READ_MANDATORY", "visual-proof-cycle")
    monkeypatch.delenv("SKIP_SKILLS_READ_GATE", raising=False)
    monkeypatch.delenv("SKIP_SKILLS_READ_GATE_REASON", raising=False)
    return d


def test_default_mandatory_is_just_delegate(monkeypatch):
    # codex: visual-proof-cycle must NOT be in the always-mandatory default (it would
    # over-block backend-only work); only the universal delegate skill is.
    monkeypatch.delenv("SKILLS_READ_MANDATORY", raising=False)
    assert gate.mandatory_skills() == ["delegate-work-to-subagents"]
    assert "visual-proof-cycle" not in gate.mandatory_skills()


def _run(event: dict, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = gate.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _commit_event():
    return {"point": "pre-bash", "event_id": SID,
            "args": {"command": "git commit -m feat", "session_id": SID}}


def _skill_event(name="visual-proof-cycle"):
    return {"point": "pre-skill", "event_id": SID,
            "args": {"skill": name, "session_id": SID}}


# ── commit detection (codex P1): git -C / git -c must not bypass the gate ──────────────
def test_is_git_commit_handles_global_options():
    assert gate.is_git_commit("git commit -m x")
    assert gate.is_git_commit("git -C /repo commit -m x")
    assert gate.is_git_commit("git -c user.name=x commit -m y")
    assert gate.is_git_commit("/usr/bin/git -C /r commit")
    # not authoring commits / not commits at all.
    assert not gate.is_git_commit("git commit --continue")
    assert not gate.is_git_commit("git status")
    assert not gate.is_git_commit("echo git commit")


def test_is_git_commit_handles_chained_commands():
    # codex P1: shell separators must split segments even without surrounding spaces.
    assert gate.is_git_commit("git status; git commit -m x")
    assert gate.is_git_commit("git status;git commit -m x")
    assert gate.is_git_commit("true|git commit -m x")
    assert gate.is_git_commit("foo && git commit")
    assert gate.is_git_commit("git add -A && git commit -m x")
    # a quoted separator is NOT a real one.
    assert not gate.is_git_commit("echo 'git commit' && ls")


def test_is_git_commit_message_value_is_not_a_continuation_flag():
    # codex P1: `-m --skip` is a MESSAGE of "--skip", a real authoring commit, not a rebase step.
    assert gate.is_git_commit("git commit -m --skip")
    assert gate.is_git_commit("git commit -m --abort")
    assert gate.is_git_commit("git commit --message --continue")
    # the genuine continuation steps still don't count.
    assert not gate.is_git_commit("git commit --skip")
    assert not gate.is_git_commit("git commit --continue")


def test_git_dash_c_commit_is_gated(isolated, monkeypatch):
    ev = {"point": "pre-bash", "event_id": SID,
          "args": {"command": "git -C /repo commit -m feat", "session_id": SID}}
    out1, code1 = _run(ev, monkeypatch)
    assert code1 == 0  # warn
    out2, code2 = _run(ev, monkeypatch)
    assert code2 == gate.BLOCK_EXIT_CODE  # the bypass is closed


# ── the satisfiable record → check loop ───────────────────────────────────────────────
def test_commit_warns_then_blocks_when_unread(isolated, monkeypatch):
    out1, code1 = _run(_commit_event(), monkeypatch)
    assert code1 == 0 and _decision(out1) == "allow"  # first → warn
    out2, code2 = _run(_commit_event(), monkeypatch)
    assert code2 == gate.BLOCK_EXIT_CODE  # repeat → block
    assert "visual-proof-cycle" in json.loads(out2)["message"]


def test_recording_the_skill_clears_the_gate(isolated, monkeypatch):
    # warn once so the NEXT commit would block...
    _run(_commit_event(), monkeypatch)
    # ...invoke the real skill (the honest, satisfiable action)...
    out_rec, code_rec = _run(_skill_event(), monkeypatch)
    assert code_rec == 0 and _decision(out_rec) == "allow"
    # ...now the commit is allowed.
    out, code = _run(_commit_event(), monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_only_a_real_skill_invocation_records(isolated, monkeypatch):
    # recording a DIFFERENT skill does not satisfy the mandatory one.
    _run(_skill_event("some-other-skill"), monkeypatch)
    _run(_commit_event(), monkeypatch)  # warn
    out, code = _run(_commit_event(), monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_plugin_qualified_skill_name_matches(isolated, monkeypatch):
    # a `plugin:visual-proof-cycle` invocation should satisfy `visual-proof-cycle`.
    _run(_skill_event("superpowers:visual-proof-cycle"), monkeypatch)
    out, code = _run(_commit_event(), monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── code-write is also a substantive act ──────────────────────────────────────────────
def test_code_write_gated_docs_write_not(isolated, monkeypatch):
    code_ev = {"point": "pre-write", "event_id": SID,
               "args": {"file_path": "src/app.py", "session_id": SID}}
    docs_ev = {"point": "pre-write", "event_id": SID,
               "args": {"file_path": "README.md", "session_id": SID}}
    # docs write → never gated.
    out_d, code_d = _run(docs_ev, monkeypatch)
    assert code_d == 0 and _decision(out_d) == "allow"
    # code write warns then blocks.
    _run(code_ev, monkeypatch)
    out, code = _run(code_ev, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── applies to SUBAGENTS too (NOT exempt) ─────────────────────────────────────────────
def test_gate_applies_inside_subagent(isolated, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    _run(_commit_event(), monkeypatch)  # warn
    out, code = _run(_commit_event(), monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE  # still gated despite being a subagent


# ── ESCAPE HATCH ──────────────────────────────────────────────────────────────────────
def test_env_escape_requires_reason(isolated, monkeypatch):
    _run(_commit_event(), monkeypatch)
    out, code = _run(_commit_event(), monkeypatch, env={"SKIP_SKILLS_READ_GATE": "1"})
    assert code == gate.BLOCK_EXIT_CODE  # no reason → still blocks


def test_env_escape_with_reason_allows(isolated, monkeypatch):
    _run(_commit_event(), monkeypatch)
    out, code = _run(_commit_event(), monkeypatch,
                     env={"SKIP_SKILLS_READ_GATE": "1",
                          "SKIP_SKILLS_READ_GATE_REASON": "hotfix, skills already known"})
    assert code == 0 and _decision(out) == "allow"


def test_inline_escape_allows(isolated, monkeypatch):
    ev = {"point": "pre-bash", "event_id": SID,
          "args": {"command": "git commit -m x  # skills-read-ok: trivial", "session_id": SID}}
    _run(_commit_event(), monkeypatch)
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── non-substantive bash is never gated ───────────────────────────────────────────────
def test_non_commit_bash_not_gated(isolated, monkeypatch):
    ev = {"point": "pre-bash", "event_id": SID,
          "args": {"command": "ls -la", "session_id": SID}}
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── fail-open ─────────────────────────────────────────────────────────────────────────
def test_garbage_stdin_fails_open(isolated, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("xx"))
    monkeypatch.setattr(sys, "stdout", out)
    assert gate.main() == 0 and _decision(out.getvalue()) == "allow"
