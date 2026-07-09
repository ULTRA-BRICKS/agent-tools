"""Tests for git-hooks/global-dispatcher/hooks/review-gate."""

from __future__ import annotations

import os
import subprocess
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_REVIEW_GATE = _ROOT / "git-hooks" / "global-dispatcher" / "hooks" / "review-gate"


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _repo_with_staged_change(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run("git", "init", cwd=repo).returncode == 0
    assert _run("git", "config", "user.email", "a@example.com", cwd=repo).returncode == 0
    assert _run("git", "config", "user.name", "A", cwd=repo).returncode == 0
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    assert _run("git", "add", "x.txt", cwd=repo).returncode == 0
    return repo


def _staged_diff_hash(repo: Path) -> str:
    diff = _run("git", "diff", "--no-ext-diff", "--cached", cwd=repo).stdout
    proc = subprocess.run(
        ["shasum", "-a", "256"],
        input=diff,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.split()[0]


def test_review_skip_env_does_not_bypass_global_review_gate(tmp_path):
    repo = _repo_with_staged_change(tmp_path)

    env = dict(os.environ)
    env["REVIEW_SKIP"] = "1"
    proc = _run(str(_REVIEW_GATE), cwd=repo, env=env)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr
    assert "review diff --staged" in proc.stderr
    assert "no-verify bypasses this and all other hooks" in proc.stderr


def test_review_gate_allows_matching_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path)
    stamp = _run("git", "rev-parse", "--git-path", "review-stamp", cwd=repo).stdout.strip()
    (repo / stamp).write_text(_staged_diff_hash(repo) + "\n", encoding="utf-8")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 0


def test_review_gate_blocks_stale_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path)
    stamp = _run("git", "rev-parse", "--git-path", "review-stamp", cwd=repo).stdout.strip()
    (repo / stamp).write_text("not-the-current-diff\n", encoding="utf-8")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "review diff --staged" in proc.stderr


def test_review_gate_allows_empty_staged_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run("git", "init", cwd=repo).returncode == 0

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 0


def test_no_current_docs_advertise_review_skip_bypass():
    current_docs_roots = [
        _ROOT / "README.md",
        _ROOT / "ROADMAP.md",
        _ROOT / "agent-hooks",
        _ROOT / "git-hooks",
        _ROOT / "skills",
    ]
    stale_pattern = re.compile(
        r"REVIEW_SKIP=1\s+git commit|sanctioned\s+`?REVIEW_SKIP=1`?|"
        r"REVIEW_SKIP=1\s*/\s*git commit",
        re.IGNORECASE,
    )
    stale = []
    for root in current_docs_roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if stale_pattern.search(text):
                stale.append(path.relative_to(_ROOT).as_posix())

    assert stale == []
