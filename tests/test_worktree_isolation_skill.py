"""Execute the worktree-isolation skill's documented fallback against real git repos.

The test extracts the fenced shell block from the consumed SKILL.md instead of copying
it into a second fixture, then checks path resolution, exclude-file safety, fail-closed
setup, and signal-safe cleanup prerequisites.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills/by-type/monorepo/worktree-isolation/SKILL.md"
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
_FALLBACK = re.compile(
    r"^## Fallback pattern \(rig unavailable\)\n\n```bash\n(?P<script>.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def _fallback_script() -> str:
    match = _FALLBACK.search(SKILL.read_text(encoding="utf-8"))
    assert match is not None, "fallback shell block is missing from the skill"
    return match.group("script")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "skill-test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "seed")
    return repo


def _run_fallback(
    repo: Path,
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    script: str | None = None,
) -> subprocess.CompletedProcess[str]:
    capture = tmp_path / "fallback-capture.txt"
    probe = f"\nprintf '%s\\n%s\\n' \"$repo_root\" \"$worktree\" > {shlex.quote(str(capture))}\n"
    return subprocess.run(
        ["bash", "-c", (script or _fallback_script()) + probe],
        cwd=cwd or repo,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


@pytest.mark.parametrize("use_linked_cwd", [False, True])
def test_fallback_resolves_primary_root_from_primary_or_linked_cwd(
    tmp_path: Path, use_linked_cwd: bool
) -> None:
    repo = _make_repo(tmp_path)
    cwd = repo
    if use_linked_cwd:
        cwd = tmp_path / "linked"
        _git(repo, "worktree", "add", str(cwd), "-b", "linked")

    result = _run_fallback(repo, tmp_path, cwd=cwd)

    assert result.returncode == 0, result.stderr
    root, target = (tmp_path / "fallback-capture.txt").read_text().splitlines()
    target_path = Path(target)
    assert Path(root) == repo
    assert target_path.is_absolute()
    assert target_path.parent == repo / ".worktrees"
    assert not target_path.exists()


def test_fallback_preserves_missing_exclude_final_newline(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    exclude = repo / ".git/info/exclude"
    exclude.write_bytes(b"# existing\ncustom")

    result = _run_fallback(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert exclude.read_bytes() == b"# existing\ncustom\n/.worktrees/\n"


def test_fallback_rejects_symlinked_worktrees_parent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".worktrees").symlink_to(outside, target_is_directory=True)

    result = _run_fallback(repo, tmp_path)

    assert result.returncode != 0
    assert "refusing non-directory .worktrees path" in result.stderr
    assert not list(outside.iterdir())


def test_fallback_leaves_failed_add_target_and_branch_for_inspection(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _git(repo, "branch", "work/collision")
    script = _fallback_script().replace(
        'run_id="$(date +%s)-$$"', 'run_id="collision"', 1
    )

    result = _run_fallback(repo, tmp_path, script=script)

    assert result.returncode != 0
    assert "worktree creation failed" in result.stderr
    assert not (repo / ".worktrees/agent-collision").exists()
    assert _git(repo, "show-ref", "--verify", "refs/heads/work/collision")


def test_fallback_cleans_created_worktree_and_branch_on_term(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    capture = tmp_path / "signal-capture.txt"
    placeholder = "# ... do the work in $worktree — fully isolated from other runs ..."
    script = _fallback_script()
    assert placeholder in script
    script = script.replace(
        placeholder,
        'test -d "$worktree" || exit 1\n'
        'git -C "$repo_root" show-ref --verify "refs/heads/work/$run_id" || exit 1\n'
        f"printf '%s\\n%s\\n' \"$worktree\" \"work/$run_id\" > {shlex.quote(str(capture))}\n"
        "kill -TERM $$",
        1,
    )

    result = _run_fallback(repo, tmp_path, script=script)

    assert result.returncode == 143, result.stderr
    target, branch = capture.read_text().splitlines()
    assert not Path(target).exists()
    assert f"worktree {target}" not in _git(repo, "worktree", "list", "--porcelain").splitlines()
    assert not _git(repo, "branch", "--list", branch)
