"""Tests for the block-raw-worktree-add agent-hook (pre-bash).

Covers the doctrine (agent-tools#608): a raw `git worktree add`/`git worktree remove` whose
target does NOT resolve under `<repo-root>/.worktrees/` is DENIED; the same command targeting
`.worktrees/<name>` is ALLOWED; `-b`/`-B` is recognized in either flag order so its value is
never mistaken for the target path; a `git -C <other-repo>` cross-repo call is judged against
THAT repo; a chained command finds the offending segment; a repo with an undetermined root (not
a git repo at all) fails open; `git worktree prune`/`list` are never gated; the Telegram hatch
escalation allows/denies exactly like the sibling hooks.

Exercised against REAL temp git repos, not mocks — the `--git-common-dir` repo-root resolution
this hook shares with `pin-primary-worktree` is the whole point.

Run from the repo root::

    python -m pytest tests/test_block_raw_worktree_add.py -q
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

# Isolate throwaway test repos from the developer's GLOBAL git config (SYNC with
# test_pin_primary_worktree.py / test_worktree_only_writes.py).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-raw-worktree-add"
    / "block_raw_worktree_add.py"
)
_spec = importlib.util.spec_from_file_location("block_raw_worktree_add", _HOOK)
assert _spec and _spec.loader
brwa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brwa)

_ENV_KEYS = ("RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD",)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A real git repo on `main` with one commit and a `feat/x` branch (not checked out)."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "feat/x")
    return repo


def _run(cwd: Path, command: str, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    event = {"cwd": str(cwd), "args": {"command": command}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = brwa.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: raw `git worktree add` outside `.worktrees/` ─────────────────────────────────────

def test_raw_add_outside_worktrees_blocks(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add ../elsewhere feat/x", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "rig worktree create" in json.loads(out)["message"]


def test_raw_add_sibling_worktrees_dir_blocks(tmp_path, monkeypatch):
    """The exact GH-329 pattern: a `.worktrees/` that sits BESIDE the repo, not inside it."""
    repo = _make_repo(tmp_path)
    sibling = tmp_path / ".worktrees" / "name"
    out, code = _run(repo, f"git worktree add {sibling} feat/x", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_raw_add_absolute_path_outside_blocks(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add /tmp/some-agent-worktree feat/x", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW: target resolves under `<repo-root>/.worktrees/` ──────────────────────────────────

def test_add_inside_worktrees_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add .worktrees/feat-x feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_add_dash_b_before_path_recognizes_value_flag(tmp_path, monkeypatch):
    """`-b <branch>` must not be mistaken for the target path — the path is the OTHER token."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add -b feat/new .worktrees/feat-new", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_add_dash_b_after_path_reverse_order_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add .worktrees/feat-new -b feat/new", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_add_dash_b_before_path_outside_still_blocks(tmp_path, monkeypatch):
    """Same flag shape as above, but the path itself is outside `.worktrees/` — must still block,
    proving `-b`'s value isn't accidentally treated as the checked path either."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add -b feat/new ../outside", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_add_nested_under_worktrees_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree add .worktrees/sub/feat feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── repo-root resolution from INSIDE a real linked worktree (the whole point of reusing
# pin-primary-worktree's --git-common-dir primitive: it must resolve to the PRIMARY root
# regardless of which worktree the command actually runs from) ──────────────────────────────

def test_from_inside_linked_worktree_resolves_primary_root_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    linked = repo / ".worktrees" / "existing"
    _git(repo, "worktree", "add", str(linked), "feat/x")
    # cwd is the LINKED worktree; a relative `../new-one` from there still resolves under the
    # PRIMARY repo's `.worktrees/`, not a sibling of the linked worktree itself.
    out, code = _run(linked, "git worktree add ../new-one -b feat/new", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_from_inside_linked_worktree_sibling_of_repo_blocks(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    linked = repo / ".worktrees" / "existing"
    _git(repo, "worktree", "add", str(linked), "feat/x")
    # `../../sibling` from the linked worktree resolves OUTSIDE the primary repo root entirely.
    out, code = _run(linked, "git worktree add ../../sibling -b feat/new2", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── a symlinked `.worktrees` must never be trusted as sanctioned ────────────────────────────

def test_symlinked_worktrees_dir_never_sanctioned(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside-worktrees"
    outside.mkdir()
    (repo / ".worktrees").symlink_to(outside, target_is_directory=True)
    out, code = _run(repo, "git worktree add .worktrees/feat-x feat/x", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── `git worktree remove` mirrors `add` ──────────────────────────────────────────────────────

def test_remove_outside_worktrees_blocks(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree remove ../elsewhere", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "rig worktree remove" in json.loads(out)["message"]


def test_remove_inside_worktrees_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree remove .worktrees/feat-x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_remove_force_flag_does_not_confuse_target(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git worktree remove -f ../elsewhere", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── out of scope: prune/list/lock/... are never gated ───────────────────────────────────────

@pytest.mark.parametrize("sub", ["prune", "list", "lock .worktrees/x", "unlock .worktrees/x"])
def test_other_worktree_subcommands_unaffected(tmp_path, monkeypatch, sub):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, f"git worktree {sub}", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_unrelated_git_commands_unaffected(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git status", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_non_git_commands_unaffected(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "rig worktree create feat-new", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── `git -C <other-repo>` is judged against THAT repo, not the shell cwd ────────────────────

def test_cross_repo_dash_c_blocks_against_target_repo(tmp_path, monkeypatch):
    repo_a = _make_repo(tmp_path, "repo-a")
    repo_b = _make_repo(tmp_path, "repo-b")
    out, code = _run(repo_a, f"git -C {repo_b} worktree add ../outside-b feat/x", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert str(repo_b) in json.loads(out)["message"] or "repo-b" in json.loads(out)["message"]


def test_cross_repo_dash_c_allows_when_inside_target_repos_worktrees(tmp_path, monkeypatch):
    repo_a = _make_repo(tmp_path, "repo-a")
    repo_b = _make_repo(tmp_path, "repo-b")
    out, code = _run(
        repo_a,
        f"git -C {repo_b} worktree add {repo_b}/.worktrees/feat-x feat/x",
        monkeypatch,
    )
    assert code == 0 and _decision(out) == "allow"


# ── chained commands: every segment is scanned, one bad segment blocks the whole chain ──────

def test_chained_command_finds_offending_segment(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "echo hi && git worktree add ../elsewhere feat/x", monkeypatch)
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_chained_command_all_segments_ok_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(
        repo, "echo hi && git worktree add .worktrees/feat-x feat/x", monkeypatch
    )
    assert code == 0 and _decision(out) == "allow"


# ── undetermined repo root (not a git repo at all) fails OPEN ────────────────────────────────

def test_not_a_git_repo_fails_open(tmp_path, monkeypatch):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    out, code = _run(plain_dir, "git worktree add ../elsewhere feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_malformed_event_fails_open(monkeypatch):
    out_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = brwa.main()
    assert code == 0 and _decision(out_buf.getvalue()) == "allow"


# ── generic Telegram hatch escalation: mirrors pin-primary-worktree's own tests ─────────────

def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


def test_hatch_escalation_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(brwa.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path)

    out, code = _run(
        repo,
        "git worktree add ../elsewhere feat/x",
        monkeypatch,
        {"RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD": "Migrating a pre-existing worktree."},
    )

    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_escalation_denied_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(brwa.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path)

    out, code = _run(
        repo,
        "git worktree add ../elsewhere feat/x",
        monkeypatch,
        {"RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD": "Migrating a pre-existing worktree."},
    )

    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


def test_hatch_inline_form_on_a_different_chain_segment_is_found(tmp_path, monkeypatch):
    """The documented inline form (`RIG_HATCH_REQUEST_..."why" <cmd>`) works even when the
    assignment sits on a DIFFERENT segment of a chained command than the gate-worthy one — this
    only works because the hatch call is given the FULL command, not just the offending segment
    (`hatch_escalation._parse_inline_hatch_value` scans every segment for the leading
    assignment)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(brwa.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path)

    inline = 'RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="Migrating a worktree." echo prep'
    out, code = _run(repo, f"{inline} && git worktree add ../elsewhere feat/x", monkeypatch)

    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_bare_hatch_value_rejected_without_telegram(tmp_path, monkeypatch):
    # No tg-ctl faked at all — a bare "1" must be rejected without ever spawning tg-ctl.
    repo = _make_repo(tmp_path)
    out, code = _run(
        repo,
        "git worktree add ../elsewhere feat/x",
        monkeypatch,
        {"RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD": "1"},
    )
    assert code == brwa.BLOCK_EXIT_CODE and _decision(out) == "block"
