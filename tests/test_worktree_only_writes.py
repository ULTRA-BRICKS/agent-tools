"""Tests for the worktree-only-writes agent-hook (pre-write).

Covers the doctrine: on the DEFAULT branch of an ENROLLED repo an Edit/Write is DENIED; on a
feature branch it is ALLOWED; an un-enrolled repo (no env, no rig.yaml knob) is never blocked;
the escape hatch allows a deliberate main edit; detached HEAD / non-git fail OPEN. Default-branch
detection is exercised against REAL temp git repos (origin/HEAD, init.defaultBranch, and the
`master` case), not mocks — the git-subprocess path is the whole point of the hook.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_worktree_only_writes.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Isolate the throwaway test repos from the developer's GLOBAL git config — in particular the
# rig-provisioned `core.hooksPath`, whose pre-commit gates would otherwise fail `git commit` in a
# bare temp repo. The HOOK's own read-only git queries (rev-parse/symbolic-ref) are unaffected by
# hooks and deliberately use the ambient env, so this only sanitizes the test's setup commands.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "worktree-only-writes"
    / "worktree_only_writes.py"
)
_spec = importlib.util.spec_from_file_location("worktree_only_writes", _HOOK)
assert _spec and _spec.loader
wow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wow)

_ENV_KEYS = (
    "RIG_WORKTREE_ONLY",
    "RIG_ALLOW_MAIN_EDIT",
    "RIG_ALLOW_MAIN_EDIT_REASON",
    "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES",
)


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    )


def _make_repo(tmp_path: Path, *, branch: str, default_via: str | None = None) -> Path:
    """A real git repo checked out on ``branch`` with one commit.

    default_via: None → rely on repo-local branch-existence detection; "origin" → point
    refs/remotes/origin/HEAD at origin/<branch> (the authoritative path).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    if default_via == "origin":
        _git(repo, "remote", "add", "origin", str(repo))
        _git(repo, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    return repo


def _run(cwd: Path, monkeypatch, env: dict | None = None, *, target: Path | None = None) -> tuple[str, int]:
    fp = str(target) if target is not None else str(cwd / "src" / "a.ts")
    event = {"cwd": str(cwd), "args": {"file_path": fp}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = wow.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── DENY on the default branch when enrolled ──────────────────────────────────────────────

def test_default_branch_enrolled_via_env_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == wow.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "worktree" in json.loads(out)["message"].lower()


def test_default_branch_enrolled_via_rigyaml_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    out, code = _run(repo, monkeypatch)  # no env → enrolled purely by the committed rig.yaml
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_master_default_repo_denies_on_master(tmp_path, monkeypatch):
    """Default branch is DETECTED repo-locally, not hardcoded 'main': a master-only repo (no
    origin/HEAD, no global config consulted) resolves to master via branch existence and blocks."""
    repo = _make_repo(tmp_path, branch="master")
    assert wow.default_branch(str(repo)) == "master"
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_origin_head_detection_denies(tmp_path, monkeypatch):
    """The primary detection path — refs/remotes/origin/HEAD → origin/main → main."""
    repo = _make_repo(tmp_path, branch="main", default_via="origin")
    assert wow.default_branch(str(repo)) == "main"
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW on a feature branch ─────────────────────────────────────────────────────────────

def test_feature_branch_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "-b", "feat/x")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW when the repo is not enrolled (the 3d-cli / works-on-main case) ──────────────────

def test_default_branch_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")  # no env, no rig.yaml → default OFF
    out, code = _run(repo, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_rigyaml_explicit_false_allows(tmp_path, monkeypatch):
    """A repo that works on main opts OUT explicitly (worktree_only: false) → never blocked."""
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    out, code = _run(repo, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_env_zero_overrides_rigyaml_true(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "0"})
    assert code == 0 and _decision(out) == "allow"


# ── the verdict follows the WRITE TARGET's checkout, not the shell cwd (codex) ────────────

def test_target_path_in_main_repo_denies_even_when_cwd_is_feature(tmp_path, monkeypatch):
    """cwd is a feature worktree, but the Write TARGETS an absolute path inside a main-branch
    checkout → BLOCK. If the guard used cwd's branch (feature) it would wrongly allow."""
    main_repo = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")  # cwd is a DIFFERENT checkout, on a feature branch
    out, code = _run(feat, monkeypatch, {"RIG_WORKTREE_ONLY": "1"},
                     target=main_repo / "src" / "a.ts")
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_target_path_in_feature_repo_allows_even_when_cwd_is_main(tmp_path, monkeypatch):
    """The inverse: cwd on main, but the Write TARGETS a feature worktree → ALLOW (authoring is
    happening where it should). If the guard used cwd's branch (main) it would falsely block."""
    main_repo = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    _git(feat, "config", "user.email", "t@t.t")
    _git(feat, "config", "user.name", "t")
    (feat / "seed.txt").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "seed")
    out, code = _run(main_repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"},
                     target=feat / "src" / "a.ts")
    assert code == 0 and _decision(out) == "allow"


# ── the OLD self-service escape hatch is GONE (agent-tools#213) ────────────────────────────

def test_old_self_service_env_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """`RIG_ALLOW_MAIN_EDIT=1` was the self-graded bypass an agent could set on itself. It is
    removed: setting it on the default branch of an enrolled repo no longer allows the write."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1", "RIG_ALLOW_MAIN_EDIT": "1"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── the RIG_HATCH_REQUEST_* Telegram escalation replaces it ────────────────────────────────

def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` (no written justification) is an invalid request → deny (block), and NO tg-ctl
    is ever invoked. A never-callable path proves no Telegram round-trip happens."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n")  # would ALLOW if ever called
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {
        "RIG_WORKTREE_ONLY": "1", "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES": "1",
    })
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 0 (the human approved) → allow."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES": "Hotfix on main, worktree unavailable.",
    })
    assert code == 0 and _decision(out) == "allow"
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks_citing_denial(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 1 (the human declined / timed out) → block, and the
    message leads with the denial reason."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES": "Hotfix on main, worktree unavailable.",
    })
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── fail-OPEN: detached HEAD / not a git repo ─────────────────────────────────────────────

def test_detached_head_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "--detach", "HEAD")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_non_git_dir_allows(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    out, code = _run(plain, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"  # current_branch None → fail-open


def test_bad_event_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    assert wow.main() == 0 and _decision(out.getvalue()) == "allow"


# ── unit: the minimal rig.yaml boolean parse ──────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("agent_hooks:\n  worktree_only: true\n", True),
    ("agent_hooks:\n  worktree_only: false\n", False),
    ("agent_hooks:\n  worktree_only: yes  # inline comment\n", True),
    ("agent_hooks:\n  all: true\n", False),                       # key absent → default
    ("skills:\n  worktree_only: true\n", False),                  # wrong block → not read
    ("agent_hooks:\n  items:\n    x:\n      on_error: open\n", False),  # nested other key
    # P2b: a DEEPER-nested worktree_only (under items.<hook>) must NOT flip the guard — only a
    # DIRECT child of agent_hooks counts.
    ("agent_hooks:\n  items:\n    x:\n      worktree_only: true\n", False),
    ("agent_hooks:\n  all: true\n  worktree_only: true\n", True),  # direct child (not first) → read
    ("agent_hooks:\n  worktree_only:\n", False),                   # blank value → default (False here)
    ("", False),
])
def test_agent_hooks_bool_parse(text, expected):
    assert wow._agent_hooks_bool(text, "worktree_only", default=False) is expected


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
