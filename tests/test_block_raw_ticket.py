"""Tests for agent-hooks/block-raw-ticket — raw gh issue / Linear vs task-cli.

The 015 crux: ``gh issue new`` under a configured task-cli MUST block
(exit 10). If that command were allowed, this file is red.

The 041 crux: ``gh issue create --title "Review uncommitted …"`` MUST block
even when task-cli is absent — that title must not become a GitHub ticket.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-raw-ticket"
    / "block_raw_ticket.py"
)

_spec = importlib.util.spec_from_file_location("block_raw_ticket", _HOOK)
assert _spec and _spec.loader
brt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brt)


def _run(command: str, monkeypatch, *, cwd: str | None = None, path: str | None = None) -> tuple[str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"args": {"command": command}, "cwd": cwd or os.getcwd()}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    if path is not None:
        monkeypatch.setenv("PATH", path)
    code = brt.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _task_on_path(tmp_path: Path) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    task = bindir / "task"
    task.write_text("#!/bin/sh\n# tasklib shim for tests\nexit 0\n", encoding="utf-8")
    task.chmod(task.stat().st_mode | stat.S_IXUSR)
    return f"{bindir}{os.pathsep}/usr/bin:/bin"


def _empty_path() -> str:
    return "/usr/bin:/bin"


def test_gh_issue_new_blocked_when_task_cli_configured(tmp_path, monkeypatch):
    """CRUX: gh issue new under task-cli must not succeed — this test is red if it would."""
    out, code = _run(
        "gh issue new --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"
    msg = json.loads(out).get("message", "").lower()
    assert "task" in msg
    assert "gh issue" in msg or "raw" in msg


def test_gh_issue_create_blocked_when_task_cli_configured(tmp_path, monkeypatch):
    out, code = _run(
        "gh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_linear_issue_create_blocked_when_task_cli_configured(tmp_path, monkeypatch):
    out, code = _run(
        "linear issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_gh_issue_new_allowed_when_task_cli_absent(tmp_path, monkeypatch):
    out, code = _run(
        "gh issue new --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_linear_issue_create_allowed_when_task_cli_absent(tmp_path, monkeypatch):
    out, code = _run(
        "linear issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_task_new_allowed_when_task_cli_configured(tmp_path, monkeypatch):
    out, code = _run(
        'task new --title "x" --why y --impact z --if-not-done w --acceptance a',
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_gh_pr_create_allowed_when_task_cli_configured(tmp_path, monkeypatch):
    out, code = _run(
        "gh pr create --title x --body y",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_gh_issue_create_behind_repo_flag_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "gh -R owner/repo issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_echo_mentioning_gh_issue_allowed(tmp_path, monkeypatch):
    out, code = _run(
        'echo "do not gh issue create by hand"',
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_gh_issue_list_allowed_read(tmp_path, monkeypatch):
    out, code = _run(
        "gh issue list",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_configured_via_task_yaml_without_binary_does_not_block(tmp_path, monkeypatch):
    """Activated AND configured: a task.yaml alone is not enough without `task` on PATH."""
    (tmp_path / "task.yaml").write_text("version: 1\\nbackend: github-issues\\n", encoding="utf-8")
    out, code = _run(
        "gh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def _foreign_task_on_path(tmp_path: Path) -> str:
    """A `task` binary that is NOT task-cli (Taskwarrior/go-task shape)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    task = bindir / "task"
    task.write_text("#!/bin/sh\necho go-task\nexit 0\n", encoding="utf-8")
    task.chmod(task.stat().st_mode | stat.S_IXUSR)
    return f"{bindir}{os.pathsep}/usr/bin:/bin"


def test_gh_issue_new_allowed_when_foreign_task_binary(tmp_path, monkeypatch):
    out, code = _run(
        "gh issue new --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_foreign_task_on_path(tmp_path),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_newline_separated_gh_issue_create_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "cd /repo\ngh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_unspaced_semicolon_gh_issue_create_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "true;gh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_unspaced_andand_gh_issue_create_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "true&&gh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_gh_issue_repo_flag_after_issue_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "gh issue --repo owner/repo create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_env_unset_wrapper_operand_still_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "env -u GH_TOKEN gh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_timeout_signal_wrapper_operand_still_blocked(tmp_path, monkeypatch):
    out, code = _run(
        "timeout --signal KILL 10 gh issue create --title x",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_review_uncommitted_gh_issue_create_blocked_when_task_cli_absent(tmp_path, monkeypatch):
    """CRUX 041: a Review-uncommitted title must not become a GitHub ticket, even via raw gh."""
    out, code = _run(
        'gh issue create --title "Review uncommitted foo diff"',
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"
    msg = json.loads(out).get("message", "").lower()
    assert "review uncommitted" in msg or "junk" in msg


def test_review_uncommitted_gh_issue_new_blocked_when_task_cli_absent(tmp_path, monkeypatch):
    out, code = _run(
        'gh issue new --title "Review uncommitted policy diff"',
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_review_uncommitted_task_new_blocked_when_task_cli_configured(tmp_path, monkeypatch):
    out, code = _run(
        'task new --title "Review uncommitted foo diff" --why y --impact z --if-not-done w --acceptance a',
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"


def test_echo_mentioning_review_uncommitted_allowed(tmp_path, monkeypatch):
    out, code = _run(
        'echo "do not Review uncommitted foo diff"',
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_gh_pr_create_review_uncommitted_title_allowed(tmp_path, monkeypatch):
    """PRs are not tickets — junk-title refuse is for issue/ticket create only."""
    out, code = _run(
        'gh pr create --title "Review uncommitted foo diff" --body y',
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == 0, out
    assert _decision(out) == "allow"


def test_review_uncommitted_attached_short_title_flag_blocked(tmp_path, monkeypatch):
    """gh accepts -tVALUE / -t\"VALUE\"; that form must not slip past the extractor."""
    out, code = _run(
        'gh issue create -t"Review uncommitted foo diff"',
        monkeypatch,
        cwd=str(tmp_path),
        path=_empty_path(),
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    assert _decision(out) == "block"
