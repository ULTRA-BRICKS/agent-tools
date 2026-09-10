"""Tests for the enterworktree-canonical-path-grant agent-hook (pre-worktree-enter).

Covers the doctrine (rig-cli#418): an EnterWorktree(path=...) call whose target resolves
under the repo's rig-sanctioned <repo-root>/.worktrees/<name> is GRANTED (pre-approved); any
other target -- outside .worktrees/, the bare .worktrees/ directory itself, a .worktrees
symlink escaping the repo, or the `name`-based create path (no `path` at all) -- gets a plain
allow (no opinion), never a grant and never a block (this hook never blocks anything).

Exercised against a REAL temp git repo, not a mock -- the canonical-path resolution is the
whole point of this hook (same discipline as test_pin_primary_worktree.py /
test_block_raw_worktree_add.py).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_enterworktree_canonical_path_grant.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

# Isolate throwaway test repos from the developer's GLOBAL git config (SYNC with
# test_pin_primary_worktree.py / test_worktree_only_writes.py).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "enterworktree-canonical-path-grant"
    / "enterworktree_canonical_path_grant.py"
)
_spec = importlib.util.spec_from_file_location("enterworktree_canonical_path_grant", _HOOK)
assert _spec and _spec.loader
grant_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grant_hook)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, no .worktrees/ directory yet (the hook never
    requires the target to already exist -- EnterWorktree may be about to create it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _run(event: dict, monkeypatch) -> tuple[str, int]:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = grant_hook.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def test_canonical_worktrees_subdir_is_granted(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    target = repo / ".worktrees" / "my-worktree"
    event = {"cwd": str(repo), "args": {"path": str(target)}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "grant"
    assert str(target) in payload["message"]


def test_relative_path_resolved_against_cwd_is_granted(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    event = {"cwd": str(repo), "args": {"path": ".worktrees/rel-worktree"}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "grant"


def test_path_outside_worktrees_dir_is_plain_allow(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "elsewhere"
    event = {"cwd": str(repo), "args": {"path": str(outside)}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_bare_worktrees_dir_itself_is_not_sanctioned(tmp_path, monkeypatch):
    # no <name> subdirectory at all -- not the `rig worktree create` shape
    repo = _make_repo(tmp_path)
    event = {"cwd": str(repo), "args": {"path": str(repo / ".worktrees")}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_worktrees_symlink_escaping_repo_is_not_sanctioned(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    escape_target = tmp_path / "outside-target"
    escape_target.mkdir()
    (repo / ".worktrees").symlink_to(escape_target)
    event = {"cwd": str(repo), "args": {"path": str(repo / ".worktrees" / "name")}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_no_path_arg_creating_via_name_is_plain_allow(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    event = {"cwd": str(repo), "args": {"name": "my-new-worktree"}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_missing_args_is_plain_allow(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    event = {"cwd": str(repo)}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_repo_root_undetermined_fails_open_to_allow(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    event = {"cwd": str(not_a_repo), "args": {"path": str(not_a_repo / ".worktrees" / "x")}}
    out, code = _run(event, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_malformed_event_json_fails_open_to_allow(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{ not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = grant_hook.main()
    assert code == 0
    assert _decision(out.getvalue()) == "allow"


def test_non_dict_event_json_fails_open_to_allow(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(["not", "a", "dict"])))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = grant_hook.main()
    assert code == 0
    assert _decision(out.getvalue()) == "allow"


def test_hook_never_exits_with_the_block_code(tmp_path, monkeypatch):
    # this hook has no block path at all -- pin it so a future edit can't accidentally add one
    # without a deliberate, reviewed decision (see the module docstring's "No hatch escalation"
    # note: any block/deny path here is explicitly out of scope for this fix).
    repo = _make_repo(tmp_path)
    for target in (
        repo / ".worktrees" / "x",
        tmp_path / "elsewhere",
        repo / ".worktrees",
    ):
        event = {"cwd": str(repo), "args": {"path": str(target)}}
        _, code = _run(event, monkeypatch)
        assert code != grant_hook.BLOCK_EXIT_CODE
