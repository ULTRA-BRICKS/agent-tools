"""Tests for the visual-proof-gate agent-hook (enforcement-doctrine gate 5).

Hermetic: builds a throwaway git repo per test, isolates the marker dir. Proves: BLOCK
(commit/stop on a user-visible change with no proof), ALLOW (proof present — a real fresh
screenshot OR a recorded visual-proof-cycle invocation; a backend-only change), the
record→clear loop, and the honest ESCAPE hatch.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_visual_proof_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "visual-proof-gate"
    / "visual_proof_gate.py"
)
_spec = importlib.util.spec_from_file_location("visual_proof_gate", _HOOK)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

SID = "vp-sess"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    # neutralize any machine-global git hooks (e.g. a review-before-commit pre-commit) so the
    # throwaway repo's own setup commits don't get blocked by the host's git config.
    _git(r, "config", "core.hooksPath", "/dev/null")
    _git(r, "config", "commit.gpgsign", "false")
    return r


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    d = tmp_path / "vp-markers"
    monkeypatch.setattr(gate, "MARKER_DIR", d)
    monkeypatch.delenv("SKIP_VISUAL_PROOF", raising=False)
    monkeypatch.delenv("SKIP_VISUAL_PROOF_REASON", raising=False)
    return d


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


def _commit_event(repo: Path):
    return {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
            "args": {"command": "git commit -m feat", "session_id": SID}}


def _stop_event(repo: Path):
    return {"point": "stop", "event_id": SID, "cwd": str(repo),
            "args": {"session_id": SID}}


def _stage_visible(repo: Path):
    (repo / "Button.tsx").write_text("export const Button = () => <button/>;\n")
    _git(repo, "add", "Button.tsx")


def _stage_backend(repo: Path):
    (repo / "server.py").write_text("def handler(): return 1\n")
    _git(repo, "add", "server.py")


# ── detection unit tests ──────────────────────────────────────────────────────────────
def test_touches_visible():
    assert gate.touches_visible(["src/Button.tsx"])
    assert gate.touches_visible(["assets/logo.png"])
    assert not gate.touches_visible(["server.py", "README.md"])


# ── BLOCK: visible change, no proof (commit) ──────────────────────────────────────────
def test_commit_visible_no_proof_warns_then_blocks(repo, isolated, monkeypatch):
    _stage_visible(repo)
    out1, code1 = _run(_commit_event(repo), monkeypatch)
    assert code1 == 0 and _decision(out1) == "allow"  # warn first
    out2, code2 = _run(_commit_event(repo), monkeypatch)
    assert code2 == gate.BLOCK_EXIT_CODE
    assert "screenshot" in json.loads(out2)["message"].lower()


# ── ALLOW: backend-only change is never gated ─────────────────────────────────────────
def test_backend_change_not_gated(repo, isolated, monkeypatch):
    _stage_backend(repo)
    out, code = _run(_commit_event(repo), monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    # even a repeat stays allowed (no warn marker was set).
    out2, code2 = _run(_commit_event(repo), monkeypatch)
    assert code2 == 0 and _decision(out2) == "allow"


# ── commit detection (codex P1): git -C must not bypass ───────────────────────────────
def test_is_git_commit_handles_global_options():
    assert gate.is_git_commit("git commit -m x")
    assert gate.is_git_commit("git -C /repo commit -m x")
    assert not gate.is_git_commit("git commit --abort")
    assert not gate.is_git_commit("git status")


def test_git_dash_c_commit_is_gated(repo, isolated, monkeypatch):
    _stage_visible(repo)
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": f"git -C {repo} commit -m feat", "session_id": SID}}
    _run(ev, monkeypatch)  # warn
    out, code = _run(ev, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── ALLOW: a verification screenshot (NOT in the commit) counts as proof ──────────────
def test_verification_shot_outside_commit_is_proof(repo, isolated, monkeypatch):
    _stage_visible(repo)
    # an UNTRACKED fresh screenshot (the agent's verification capture) — not in the commit.
    (repo / "verify.png").write_bytes(b"\x89PNG\r\n")
    out, code = _run(_commit_event(repo), monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_explicit_screenshot_token_is_proof(repo, isolated, monkeypatch):
    _stage_visible(repo)
    (repo / "shot.png").write_bytes(b"\x89PNG\r\n")
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git commit -m x  # screenshot: shot.png", "session_id": SID}}
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_explicit_screenshot_in_commit_is_not_self_proof(repo, isolated, monkeypatch):
    # codex P2: naming a screenshot that is ITSELF part of the commit is not proof.
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n")
    _git(repo, "add", "logo.png")
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git commit logo.png -m x  # screenshot: logo.png",
                   "session_id": SID}}
    _run(ev, monkeypatch)  # warn
    out, code = _run(ev, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_chained_commit_is_gated(repo, isolated, monkeypatch):
    _stage_visible(repo)
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git add -A && git commit -m x", "session_id": SID}}
    _run(ev, monkeypatch)  # warn
    out, code = _run(ev, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── codex P2: git -C scopes to the RIGHT repo, pathspec to the right files ─────────────
def test_git_dash_C_scopes_to_target_repo(repo, isolated, monkeypatch, tmp_path):
    # a backend-only commit in `repo` via `git -C repo` issued from an UNRELATED cwd that
    # itself has an unstaged visible file must not be gated (scope follows -C, not the cwd).
    _stage_backend(repo)
    other = tmp_path / "other"
    other.mkdir()
    (other / "Stray.tsx").write_text("export const S = () => <i/>;\n")
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(other),
          "args": {"command": f"git -C {repo} commit -m backend", "session_id": SID}}
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_pathspec_commit_scopes_to_named_paths(repo, isolated, monkeypatch):
    # `git commit server.py -m x` commits ONLY server.py → backend-only even with a staged .tsx.
    _stage_visible(repo)   # stages Button.tsx
    _stage_backend(repo)   # stages server.py
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git commit server.py -m backend", "session_id": SID}}
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── codex P2: a committed image asset cannot be its OWN proof ──────────────────────────
def test_committed_image_is_not_self_proof(repo, isolated, monkeypatch):
    # staging logo.png (a visible change) must NOT auto-satisfy the gate for itself.
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n")
    _git(repo, "add", "logo.png")
    _run(_commit_event(repo), monkeypatch)  # warn
    out, code = _run(_commit_event(repo), monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── codex P2: scope to the COMMIT set, not the whole working tree ──────────────────────
def test_unstaged_visible_does_not_block_backend_commit(repo, isolated, monkeypatch):
    # a staged backend change + an UNSTAGED visible file → the commit is backend-only,
    # so the unstaged .tsx must not block it.
    _stage_backend(repo)
    (repo / "Widget.tsx").write_text("export const W = () => <div/>;\n")  # unstaged
    out, code = _run(_commit_event(repo), monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    out2, code2 = _run(_commit_event(repo), monkeypatch)
    assert code2 == 0 and _decision(out2) == "allow"


def test_commit_dash_a_includes_tracked_modified(repo, isolated, monkeypatch):
    # `git commit -a` lands tracked-modified files → a modified tracked .tsx IS in the set.
    (repo / "Card.tsx").write_text("v1\n")
    _git(repo, "add", "Card.tsx")
    _git(repo, "commit", "-m", "init", "-q")
    (repo / "Card.tsx").write_text("v2 changed\n")  # modified, not staged
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git commit -a -m update", "session_id": SID}}
    _run(ev, monkeypatch)  # warn
    out, code = _run(ev, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


def test_stale_screenshot_is_not_proof(repo, isolated, monkeypatch):
    _stage_visible(repo)
    shot = repo / "old.png"  # an untracked verification shot, but stale
    shot.write_bytes(b"\x89PNG\r\n")
    # backdate the file well past the freshness window.
    old = time.time() - 999999
    os.utime(shot, (old, old))
    _run(_commit_event(repo), monkeypatch)  # warn
    out, code = _run(_commit_event(repo), monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── intent is not evidence: a bare skill invocation does NOT clear the gate (codex) ───
def test_bare_skill_invocation_does_not_clear_commit(repo, isolated, monkeypatch):
    # invoking visual-proof-cycle with NO resulting artifact must still block — only a real
    # screenshot counts.
    _stage_visible(repo)
    rec = {"point": "pre-skill", "event_id": SID,
           "args": {"skill": "visual-proof-cycle", "session_id": SID}}
    _run(rec, monkeypatch)  # no artifact produced
    _run(_commit_event(repo), monkeypatch)  # warn
    out, code = _run(_commit_event(repo), monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── codex: a chained `git add -A && git commit` can't bypass via the stale index ──────
def test_chained_add_and_commit_uses_working_tree(repo, isolated, monkeypatch):
    # an UNSTAGED visible file + `git add -A && git commit` → the gate must see the .tsx the
    # `add` is about to stage (the index is empty when the hook fires).
    (repo / "Widget.tsx").write_text("export const W = () => <div/>;\n")  # unstaged
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git add -A && git commit -m x", "session_id": SID}}
    _run(ev, monkeypatch)  # warn
    out, code = _run(ev, monkeypatch)
    assert code == gate.BLOCK_EXIT_CODE


# ── stop ("done" claim) gate ──────────────────────────────────────────────────────────
def test_stop_blocks_once_on_visible_no_proof(repo, isolated, monkeypatch):
    _stage_visible(repo)
    out1, code1 = _run(_stop_event(repo), monkeypatch)
    assert code1 == gate.BLOCK_EXIT_CODE  # block the stop once
    # a second stop in the same session is allowed (so it can actually finish).
    out2, code2 = _run(_stop_event(repo), monkeypatch)
    assert code2 == 0 and _decision(out2) == "allow"


def test_stop_allows_backend_change(repo, isolated, monkeypatch):
    _stage_backend(repo)
    out, code = _run(_stop_event(repo), monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ESCAPE HATCH ──────────────────────────────────────────────────────────────────────
def test_env_escape_requires_reason(repo, isolated, monkeypatch):
    _stage_visible(repo)
    _run(_commit_event(repo), monkeypatch)  # warn
    out, code = _run(_commit_event(repo), monkeypatch, env={"SKIP_VISUAL_PROOF": "1"})
    assert code == gate.BLOCK_EXIT_CODE  # no reason → blocks


def test_env_escape_with_reason_allows(repo, isolated, monkeypatch):
    _stage_visible(repo)
    _run(_commit_event(repo), monkeypatch)
    out, code = _run(_commit_event(repo), monkeypatch,
                     env={"SKIP_VISUAL_PROOF": "1", "SKIP_VISUAL_PROOF_REASON": "headless CI"})
    assert code == 0 and _decision(out) == "allow"


def test_inline_escape_allows(repo, isolated, monkeypatch):
    _stage_visible(repo)
    ev = {"point": "pre-bash", "event_id": SID, "cwd": str(repo),
          "args": {"command": "git commit -m x  # visual-proof-ok: text-only tweak",
                   "session_id": SID}}
    _run(_commit_event(repo), monkeypatch)
    out, code = _run(ev, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── fail-open ─────────────────────────────────────────────────────────────────────────
def test_garbage_stdin_fails_open(isolated, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("nope"))
    monkeypatch.setattr(sys, "stdout", out)
    assert gate.main() == 0 and _decision(out.getvalue()) == "allow"
