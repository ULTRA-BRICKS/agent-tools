"""Tests for git-hooks/global-dispatcher/hooks/review-gate."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_REVIEW_GATE = _ROOT / "git-hooks" / "global-dispatcher" / "hooks" / "review-gate"

# Enforcement tests must NOT create repos inside the OS temp dirs: the gate
# deliberately exempts temp/scratch repos, and pytest's tmp_path lives inside
# one (e.g. /private/var/folders on macOS). Keep test repos outside the
# exemption patterns so the gate's blocking behavior stays observable.
_SCRATCH_ROOT = _ROOT / ".test-repos"


def _scratch_base(tmp_path: Path) -> Path:
    _SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{tmp_path.name}-", dir=_SCRATCH_ROOT))


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _repo_with_staged_change(tmp_path: Path, relpath: str = "x.txt") -> Path:
    repo = _scratch_base(tmp_path) / "repo"
    repo.mkdir()
    assert _run("git", "init", cwd=repo).returncode == 0
    assert _run("git", "config", "user.email", "a@example.com", cwd=repo).returncode == 0
    assert _run("git", "config", "user.name", "A", cwd=repo).returncode == 0
    staged = repo / relpath
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("x\n", encoding="utf-8")
    assert _run("git", "add", relpath, cwd=repo).returncode == 0
    return repo


def _stage_file(repo: Path, relpath: str) -> None:
    staged = repo / relpath
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("x\n", encoding="utf-8")
    assert _run("git", "add", relpath, cwd=repo).returncode == 0


def test_review_gate_scratch_repos_are_distinct_for_repeated_test_paths(tmp_path):
    first = _repo_with_staged_change(tmp_path)
    second = _repo_with_staged_change(tmp_path)
    assert first != second
    assert first.is_dir() and second.is_dir()
    for repo in (first, second):
        assert _run(str(_REVIEW_GATE), cwd=repo).returncode == 1


def _staged_diff_hash(repo: Path) -> str:
    diff = _run("git", "diff", "--no-ext-diff", "--cached", cwd=repo).stdout
    return hashlib.sha256(diff.encode()).hexdigest()


def test_review_skip_env_does_not_bypass_global_review_gate(tmp_path):
    repo = _repo_with_staged_change(tmp_path)

    env = dict(os.environ)
    env["REVIEW_SKIP"] = "1"
    proc = _run(str(_REVIEW_GATE), cwd=repo, env=env)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr
    assert "review diff --staged" in proc.stderr
    assert "no-verify bypasses this and all other hooks" in proc.stderr


def test_review_gate_allows_docs_extension_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "README.md")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 0


def test_review_gate_allows_non_code_file_under_docs_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "docs/notes.txt")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 0


def test_review_gate_allows_nested_file_under_root_docs_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "docs/guides/notes.txt")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 0


def test_review_gate_allows_recognized_media_file_under_root_docs(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "docs/diagram.png")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 0


def test_review_gate_blocks_code_file_under_docs_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "docs/conf.py")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_html_file_under_docs_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "docs/index.html")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_dockerfile_extension_under_docs_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "docs/service.dockerfile")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_code_config_txt_basenames_under_docs(tmp_path):
    for relpath in ("docs/CMakeLists.txt", "docs/requirements.txt"):
        case_dir = tmp_path / relpath.replace("/", "_").replace(".", "_")
        case_dir.mkdir()
        repo = _repo_with_staged_change(case_dir, relpath)

        proc = _run(str(_REVIEW_GATE), cwd=repo)

        assert proc.returncode == 1, relpath
        assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_nested_docs_txt_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "src/docs/notes.txt")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_when_staged_file_listing_fails(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "src/tool.py")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_git = bindir / "git"
    fake_git.write_text(
        """#!/bin/sh
if [ "$1" = "-c" ] && [ "$2" = "core.quotepath=false" ] && [ "$3" = "diff" ]; then
  exit 42
fi
exec /usr/bin/git "$@"
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"

    proc = _run(str(_REVIEW_GATE), cwd=repo, env=env)

    assert proc.returncode == 1
    assert "could not list staged files" in proc.stderr


def test_review_gate_blocks_agent_instruction_markdown_without_review_stamp(tmp_path):
    for relpath in (
        "AGENTS.md",
        "CLAUDE.md",
        "skills/universal/demo/SKILL.md",
        "packages/skills/demo/helper.md",
        "packages/agent-hooks/demo/README.md",
    ):
        case_dir = tmp_path / relpath.replace("/", "_").replace(".", "_")
        case_dir.mkdir()
        repo = _repo_with_staged_change(case_dir, relpath)

        proc = _run(str(_REVIEW_GATE), cwd=repo)

        assert proc.returncode == 1, relpath
        assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_mixed_docs_and_code_without_review_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "README.md")
    _stage_file(repo, "src/tool.py")

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr


def test_review_gate_blocks_rename_from_code_to_docs_with_renames_enabled(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "src/tool.py")
    assert _run("git", "-c", "core.hooksPath=", "commit", "-qm", "init", cwd=repo).returncode == 0
    assert _run("git", "config", "diff.renames", "true", cwd=repo).returncode == 0
    (repo / "docs").mkdir()
    assert _run("git", "mv", "src/tool.py", "docs/notes.txt", cwd=repo).returncode == 0

    proc = _run(str(_REVIEW_GATE), cwd=repo)

    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr


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


def _fresh_temp_repo_with_staged_code(tmp_path: Path) -> Path:
    """A repo that lives INSIDE the OS temp dir (exemption territory)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run("git", "init", cwd=repo).returncode == 0
    assert _run("git", "config", "user.email", "a@example.com", cwd=repo).returncode == 0
    assert _run("git", "config", "user.name", "A", cwd=repo).returncode == 0
    (repo / "code.py").write_text("print('x')\n", encoding="utf-8")
    assert _run("git", "add", "code.py", cwd=repo).returncode == 0
    return repo


def test_review_gate_exempts_repos_inside_os_temp_dirs(tmp_path):
    # pytest's tmp_path IS inside the OS temp dir (e.g. /private/var/folders on
    # macOS, /tmp on Linux) — exactly what the exemption covers. No review stamp.
    repo = _fresh_temp_repo_with_staged_code(tmp_path)
    proc = _run(str(_REVIEW_GATE), cwd=repo)
    assert proc.returncode == 0


def test_review_gate_still_blocks_non_temp_repo_without_stamp(tmp_path):
    repo = _repo_with_staged_change(tmp_path, "src/tool.py")
    proc = _run(str(_REVIEW_GATE), cwd=repo)
    assert proc.returncode == 1
    assert "staged changes have not been reviewed" in proc.stderr
