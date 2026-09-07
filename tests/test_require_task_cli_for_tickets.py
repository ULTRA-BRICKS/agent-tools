"""Tests for the require-task-cli-for-tickets agent-hook (pre-bash, strict-by-default).

This is the CI-collected mirror of the hook's own `test_require_task_cli_for_tickets.py` (which
lives beside the script under `agent-hooks/` and is not picked up by `pytest tests/`, mirroring
`require-ticket-before-commit`'s own split). It pins the contract:

  - STRICT BY DEFAULT: `gh issue create/edit/close/comment/...` and a mutating `gh api
    repos/<o>/<r>/issues...` call BLOCK (exit 10, decision "block") when task-cli is active
    (the `task` binary is on PATH and cwd is inside a git repo) — with no env set.
  - the block message LEADS with the stable marker `[require-task-cli] BLOCKED: direct ticket
    mutation`, so an external check can assert on one fixed string.
  - `gh issue view/list/status` and a read-only `gh api` call are NEVER gated.
  - task-cli's OWN commands (`task new`, `task classify`) are never gated.
  - task-cli not on PATH, or cwd outside a git repo, → allow (nothing to point the agent at).
  - `REQUIRE_TASK_CLI_STRICT=0` opts back to warn-only (allow with advisory).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_require_task_cli_for_tickets.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "require-task-cli-for-tickets"
    / "require_task_cli_for_tickets.py"
)
_spec = importlib.util.spec_from_file_location("require_task_cli", _HOOK)
assert _spec and _spec.loader
rtc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtc)

_HATCH_ENV = "RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS"


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo.as_posix()], check=True)
    return repo


def _run(
    command: str,
    *,
    cwd: Path,
    strict: bool | None = None,
    task_on_path: bool = True,
    env_extra: dict | None = None,
    scratch: Path,
) -> dict:
    env = dict(os.environ)
    env.pop("REQUIRE_TASK_CLI_STRICT", None)
    env.pop(_HATCH_ENV, None)
    if strict is True:
        env["REQUIRE_TASK_CLI_STRICT"] = "1"
    elif strict is False:
        env["REQUIRE_TASK_CLI_STRICT"] = "0"
    if env_extra:
        env.update(env_extra)

    bin_dir = scratch / "bin"
    bin_dir.mkdir(exist_ok=True)
    if task_on_path:
        # `--help` must print task-cli's OWN description string — `_task_binary_is_task_cli`
        # positively identifies the resolved `task` binary this way (rather than trusting PATH
        # resolution alone) to guard against a same-named Taskwarrior/go-task collision.
        fake = bin_dir / "task"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--help" ]; then\n'
            '  echo "task \\xe2\\x80\\x94 the enforced ticket interface."\n'
            "  exit 0\n"
            "fi\n"
            "echo fake-task\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    else:
        env["PATH"] = str(bin_dir)

    event = json.dumps({"args": {"command": command}, "cwd": str(cwd)})
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=event,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch),
        timeout=10,
    )
    out = (proc.stdout or "").strip()
    assert out, f"empty stdout; stderr={proc.stderr!r}"
    payload = json.loads(out)
    return {"code": proc.returncode, "decision": payload["decision"], "message": payload.get("message", "")}


class TestBlocksDirectMutation:
    def test_gh_issue_create_blocks_by_default(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run("gh issue create --title x --body y", cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"
        assert rtc.BLOCK_MARKER in out["message"]
        assert "task new" in out["message"]

    def test_gh_issue_edit_close_comment_block(self, tmp_path):
        repo = _git_repo(tmp_path)
        for sub in ("edit 1 --title x", "close 1", "comment 1 --body hi"):
            out = _run(f"gh issue {sub}", cwd=repo, scratch=tmp_path)
            assert out["code"] == rtc.BLOCK_EXIT_CODE, sub
            assert out["decision"] == "block", sub

    def test_gh_api_mutating_issues_call_blocks(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run("gh api repos/o/r/issues -f title=x", cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_gh_issue_new_alias_blocks(self, tmp_path):
        """`gh issue new` is gh's own documented alias for `gh issue create` — review finding,
        agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run("gh issue new --title x --body y", cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_global_repo_flag_after_issue_still_blocks(self, tmp_path):
        """`gh issue -R o/r create` / `gh issue --repo o/r create` — the global flag placed
        AFTER `issue` but before the real subcommand — review finding, agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run("gh issue -R o/r create --title x", cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_gh_api_attached_field_flag_blocks(self, tmp_path):
        """`-ftitle=x` (short flag, value concatenated, no separate token) — review finding,
        agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run("gh api repos/o/r/issues -ftitle=x", cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_gh_api_compact_method_flag_blocks(self, tmp_path):
        """`-XPOST` (concatenated) — verified live against the installed gh (a 404 JSON
        response, not an "unknown flag" error). Found during the review-cli quorum pass on
        agent-tools#597, not one of the original 6 threads."""
        repo = _git_repo(tmp_path)
        out = _run("gh api repos/o/r/issues -XPOST", cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_gh_api_hostname_flag_value_consumed_still_blocks(self, tmp_path):
        """`--hostname` takes a SEPARATE value; without consuming it the generic flag skip
        mistook the hostname for the endpoint path and never saw the real issues path. Found
        during the review-cli quorum pass, agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run(
            "gh api --hostname ghe.example.com repos/o/r/issues -X POST",
            cwd=repo,
            scratch=tmp_path,
        )
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_gh_api_sub_issues_route_blocks(self, tmp_path):
        """The path pattern is shape-based, not an enumerated allowlist — `sub_issues` is a
        real GitHub issue-mutating sub-resource the old `comments|labels|assignees|lock`
        enumeration missed. Found during the review-cli quorum pass, agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run(
            "gh api repos/o/r/issues/42/sub_issues -f sub_issue_id=99",
            cwd=repo,
            scratch=tmp_path,
        )
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"

    def test_gh_api_concrete_comment_route_blocks(self, tmp_path):
        """`repos/<owner>/<repo>/issues/comments/<comment_id>` — GitHub's route for mutating ONE
        issue comment directly, not nested under an issue number. Review finding,
        agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run(
            "gh api repos/o/r/issues/comments/123 -X PATCH -f body=x", cwd=repo, scratch=tmp_path
        )
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"


class TestReadOnlyNeverBlocks:
    @pytest.mark.parametrize("sub", ["view 1", "list", "status"])
    def test_gh_issue_read_subcommands_allow(self, tmp_path, sub):
        repo = _git_repo(tmp_path)
        out = _run(f"gh issue {sub}", cwd=repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"

    def test_gh_api_get_issues_allows(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run("gh api repos/o/r/issues", cwd=repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"

    def test_gh_api_explicit_get_overrides_field_inference(self, tmp_path):
        """`-X GET -f state=open` sends the field as a query parameter, not a body — an
        explicit read-only method must override the field-presence heuristic. Review finding,
        agent-tools#597."""
        repo = _git_repo(tmp_path)
        out = _run(
            "gh api repos/o/r/issues -X GET -f state=open", cwd=repo, scratch=tmp_path
        )
        assert out["code"] == 0
        assert out["decision"] == "allow"

    def test_gh_pr_create_is_not_gated(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run("gh pr create --title x --body y", cwd=repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"


class TestTaskCliCommandsNeverGated:
    def test_task_new_allows(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run('task new --title "x" --acceptance "y" --why "z" --impact "w"', cwd=repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"

    def test_task_classify_allows(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run('task classify "some request" --create', cwd=repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"


class TestTaskCliMustBeActive:
    def test_no_task_binary_allows(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run("gh issue create --title x", cwd=repo, task_on_path=False, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"

    def test_outside_git_repo_allows(self, tmp_path):
        not_repo = tmp_path / "not-a-repo"
        not_repo.mkdir()
        out = _run("gh issue create --title x", cwd=not_repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"


class TestTaskCliIdentity:
    """`task` is a famously ambiguous binary name (Taskwarrior, go-task also install as `task`)
    — the gate must not treat a same-named collision as an active alternative. Review finding,
    agent-tools#597 (mirrors the exact collision guard ship.sh's own acceptance gate already has
    at `ci/ship/ship.sh`'s `_acceptance_gate_looks_like_task_cli_json`)."""

    def test_same_named_non_task_cli_binary_allows(self, tmp_path):
        repo = _git_repo(tmp_path)
        env = dict(os.environ)
        env.pop("REQUIRE_TASK_CLI_STRICT", None)
        env.pop(_HATCH_ENV, None)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "task"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--help" ]; then\n'
            '  echo "task 2.6.2 (Taskwarrior)"\n'
            "  exit 0\n"
            "fi\n"
            "echo not-task-cli\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        event = json.dumps({"args": {"command": "gh issue create --title x"}, "cwd": str(repo)})
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
            timeout=10,
        )
        payload = json.loads((proc.stdout or "").strip())
        assert proc.returncode == 0
        assert payload["decision"] == "allow"


class TestStrictToggle:
    def test_warn_only_opt_out_allows_with_advisory(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run("gh issue create --title x", cwd=repo, strict=False, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"
        assert rtc.BLOCK_MARKER not in out["message"]


class TestOverMatchRegression:
    """Argv-scoped detection must never fire on a command that merely MENTIONS the words."""

    def test_echo_mentioning_gh_issue_create_is_allowed(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run('echo "gh issue create is blocked"', cwd=repo, scratch=tmp_path)
        assert out["code"] == 0
        assert out["decision"] == "allow"

    def test_real_mutation_after_mentioning_sibling_still_blocks(self, tmp_path):
        repo = _git_repo(tmp_path)
        out = _run('echo "gh issue create" && gh issue create --title x', cwd=repo, scratch=tmp_path)
        assert out["code"] == rtc.BLOCK_EXIT_CODE
        assert out["decision"] == "block"
