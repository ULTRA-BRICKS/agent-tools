#!/usr/bin/env python3
"""Unit tests for require_task_cli_for_tickets (stdlib unittest, no deps).

Run::
    python3 test_require_task_cli_for_tickets.py
    python3 -m pytest test_require_task_cli_for_tickets.py  # also works under pytest
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE / "require_task_cli_for_tickets.py"

_spec = importlib.util.spec_from_file_location("require_task_cli", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_HATCH_ENV = "RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS"


def _fake_task_bin(bin_dir: Path) -> None:
    """Drop a fake `task` executable into `bin_dir` that behaves like the REAL task-cli enough
    to pass `_task_binary_is_task_cli`'s identity check: it must print task-cli's own
    description string on `--help`."""
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


def _fake_collision_task_bin(bin_dir: Path) -> None:
    """Drop a fake `task` executable that resolves on PATH but does NOT identify as task-cli —
    stands in for a Taskwarrior/go-task binary shadowing the real one (the collision
    `_task_binary_is_task_cli` guards against, agent-tools#597)."""
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


def _git_repo(tmp_root: Path) -> Path:
    repo = tmp_root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo.as_posix()], check=True)
    return repo


def run_hook(
    command: str,
    *,
    cwd: str,
    strict: bool | None = None,
    task_on_path: bool = True,
    env_extra: dict | None = None,
    real_task_bin: Path | None = None,
) -> tuple[int, str | None]:
    """Run the script end-to-end with a JSON event on stdin; return (exit, decision)."""
    env = dict(os.environ)
    env.pop("REQUIRE_TASK_CLI_STRICT", None)
    env.pop(_HATCH_ENV, None)
    if strict is True:
        env["REQUIRE_TASK_CLI_STRICT"] = "1"
    elif strict is False:
        env["REQUIRE_TASK_CLI_STRICT"] = "0"
    if env_extra:
        env.update(env_extra)

    with tempfile.TemporaryDirectory() as scratch:
        bin_dir = Path(scratch) / "bin"
        bin_dir.mkdir()
        if task_on_path:
            if real_task_bin is not None:
                (bin_dir / "task").symlink_to(real_task_bin)
            else:
                _fake_task_bin(bin_dir)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        else:
            # A PATH with no `task` anywhere on it.
            env["PATH"] = str(bin_dir)

        event = json.dumps({"args": {"command": command}, "cwd": cwd})
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=scratch,
            timeout=10,
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise AssertionError(f"empty stdout; stderr={proc.stderr!r}")
    decision = json.loads(out)["decision"]
    return proc.returncode, decision


def _run_hook_raw(command: str, *, cwd: str, **kwargs) -> dict:
    env = dict(os.environ)
    env.pop("REQUIRE_TASK_CLI_STRICT", None)
    env.pop(_HATCH_ENV, None)
    if kwargs.get("env_extra"):
        env.update(kwargs["env_extra"])
    with tempfile.TemporaryDirectory() as scratch:
        bin_dir = Path(scratch) / "bin"
        bin_dir.mkdir()
        if kwargs.get("task_on_path", True):
            _fake_task_bin(bin_dir)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        else:
            env["PATH"] = str(bin_dir)
        event = json.dumps({"args": {"command": command}, "cwd": cwd})
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=scratch,
            timeout=10,
        )
    payload = json.loads((proc.stdout or "").strip())
    return {
        "code": proc.returncode,
        "decision": payload["decision"],
        "message": payload.get("message", ""),
        "stderr": proc.stderr or "",
    }


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


_ALLOW_REPLY_SH = (
    "printf '{\"hookSpecificOutput\":{\"hookEventName\":\"PermissionRequest\","
    "\"decision\":{\"behavior\":\"allow\"}}}'\nexit 0\n"
)


def _run_hook_inproc(command: str, *, cwd: str, env_extra: dict | None = None, tg_ctl: Path) -> dict:
    env_keys = {"REQUIRE_TASK_CLI_STRICT", _HATCH_ENV} | set(env_extra or {})
    old_env = {key: os.environ.get(key) for key in env_keys}
    try:
        for key in env_keys:
            os.environ.pop(key, None)
        for key, value in (env_extra or {}).items():
            os.environ[key] = value

        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / "home"
            home.mkdir()
            event = json.dumps({"args": {"command": command}, "cwd": cwd})
            out = io.StringIO()
            with (
                mock.patch.object(mod, "STRICT", True),
                mock.patch.object(mod.hatch_escalation, "resolve_home", lambda: str(home)),
                mock.patch.object(mod.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,)),
                mock.patch("shutil.which", return_value="/fake/bin/task"),
                # The real identity check (`_task_binary_is_task_cli`) execs "task --help" on
                # PATH — irrelevant to what this test class exercises (hatch escalation
                # plumbing, already covered by TaskCliActive/GhIssueClassification below), so
                # it's stubbed True here rather than standing up a fake binary on PATH too.
                mock.patch.object(mod, "_task_binary_is_task_cli", return_value=True),
                mock.patch.object(sys, "stdin", io.StringIO(event)),
                mock.patch.object(sys, "stdout", out),
            ):
                try:
                    code = mod.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        raw = out.getvalue().strip()
        if not raw:
            raise AssertionError(f"empty hook stdout from in-process run; code={code}")
        payload = json.loads(raw)
        return {"code": code, "decision": payload["decision"], "message": payload.get("message", "")}
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class GhIssueClassification(unittest.TestCase):
    def test_create_is_mutating(self):
        self.assertEqual(mod.gh_issue_mutation(["gh", "issue", "create"]), "create")

    def test_edit_is_mutating(self):
        self.assertEqual(mod.gh_issue_mutation(["gh", "issue", "edit", "42"]), "edit")

    def test_close_is_mutating(self):
        self.assertEqual(mod.gh_issue_mutation(["gh", "issue", "close", "42"]), "close")

    def test_comment_is_mutating(self):
        self.assertEqual(mod.gh_issue_mutation(["gh", "issue", "comment", "42"]), "comment")

    def test_reopen_delete_transfer_lock_are_mutating(self):
        for sub in ("reopen", "delete", "transfer", "lock", "unlock", "pin", "unpin"):
            self.assertEqual(mod.gh_issue_mutation(["gh", "issue", sub, "42"]), sub, sub)

    def test_view_is_not_mutating(self):
        self.assertIsNone(mod.gh_issue_mutation(["gh", "issue", "view", "42"]))

    def test_list_is_not_mutating(self):
        self.assertIsNone(mod.gh_issue_mutation(["gh", "issue", "list"]))

    def test_status_is_not_mutating(self):
        self.assertIsNone(mod.gh_issue_mutation(["gh", "issue", "status"]))

    def test_non_issue_gh_command_is_none(self):
        self.assertIsNone(mod.gh_issue_mutation(["gh", "pr", "create"]))

    def test_bare_gh_is_none(self):
        self.assertIsNone(mod.gh_issue_mutation(["gh"]))

    def test_global_repo_flag_is_skipped(self):
        self.assertEqual(
            mod.gh_issue_mutation(["gh", "--repo", "o/r", "issue", "create"]), "create"
        )
        self.assertEqual(
            mod.gh_issue_mutation(["gh", "-R", "o/r", "issue", "edit", "1"]), "edit"
        )

    def test_global_repo_flag_after_issue_is_consumed(self):
        """`gh issue -R o/r create` / `gh issue --repo o/r create` — the global flag placed
        AFTER `issue` but before the real subcommand — review finding, agent-tools#597."""
        self.assertEqual(
            mod.gh_issue_mutation(["gh", "issue", "-R", "o/r", "create"]), "create"
        )
        self.assertEqual(
            mod.gh_issue_mutation(["gh", "issue", "--repo", "o/r", "create", "--title", "x"]),
            "create",
        )

    def test_new_is_a_mutating_alias_of_create(self):
        """`gh issue new` is gh's own documented alias for `gh issue create` (verified live
        against `gh issue create --help`'s ALIASES section) — review finding, agent-tools#597."""
        self.assertEqual(mod.gh_issue_mutation(["gh", "issue", "new"]), "new")
        self.assertEqual(
            mod.gh_issue_mutation(["gh", "issue", "new", "--title", "x"]), "new"
        )


class GhApiClassification(unittest.TestCase):
    def test_post_to_issues_path_is_mutation(self):
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-X", "POST"])
        )

    def test_field_flag_to_issues_path_is_mutation(self):
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-f", "title=x"])
        )

    def test_comments_subresource_post_is_mutation(self):
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues/42/comments", "-f", "body=hi"]
            )
        )

    def test_plain_get_to_issues_path_is_not_mutation(self):
        self.assertFalse(mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues"]))

    def test_get_method_explicit_is_not_mutation(self):
        self.assertFalse(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-X", "GET"])
        )

    def test_unrelated_path_is_not_mutation(self):
        self.assertFalse(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/pulls", "-X", "POST"])
        )

    def test_non_api_gh_command_is_not_mutation(self):
        self.assertFalse(mod.gh_api_issue_mutation(["gh", "issue", "create"]))

    def test_method_equals_form_is_recognized(self):
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "--method=POST"])
        )

    def test_attached_field_flags_are_recognized(self):
        """`--field=`/`--raw-field=`/`--input=` (long, `=`-attached) and `-ftitle=x`/`-Ftitle=x`
        (short, concatenated) — gh's own pflag grammar accepts both alongside the separate-token
        form. Review finding, agent-tools#597."""
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "--field=title=x"])
        )
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "--raw-field=title=x"])
        )
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-ftitle=x"])
        )
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-Ftitle=x"])
        )

    def test_explicit_get_overrides_field_inference(self):
        """`-X GET -f state=open` sends the field as a query parameter, not a body — gh's own
        docs say so explicitly. An explicit read-only method must override the field-presence
        heuristic. Review finding, agent-tools#597."""
        self.assertFalse(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues", "-X", "GET", "-f", "state=open"]
            )
        )

    def test_explicit_post_is_mutation_even_with_no_fields(self):
        self.assertTrue(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-X", "POST"])
        )

    def test_compact_method_flag_forms_are_recognized(self):
        """`-XPOST` (concatenated) and `-X=POST` (pflag `=` form) — both verified live against
        the installed gh (a 404 JSON response, not an "unknown flag" error). Found during the
        review-cli quorum pass on agent-tools#597, not one of the original 6 threads."""
        self.assertTrue(mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-XPOST"]))
        self.assertTrue(mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-X=POST"]))
        self.assertFalse(mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues", "-XGET"]))
        self.assertFalse(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues", "-XGET", "-f", "state=open"]
            )
        )

    def test_hostname_flag_value_is_consumed_after_api(self):
        """`gh api --hostname ghe.example repos/o/r/issues -X POST` — `--hostname` takes a
        SEPARATE value (verified live: `gh api --hostname <host> <path>` fails on a connection
        to `<host>`, proving the value was consumed as the hostname, not left as noise). Without
        consuming it, the generic single-token flag skip mistook the hostname for the endpoint
        path and never saw the real `repos/.../issues` path. Found during the review-cli quorum
        pass, agent-tools#597."""
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "--hostname", "ghe.example.com", "repos/o/r/issues", "-X", "POST"]
            )
        )
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "-R", "o/r", "repos/o/r/issues", "-X", "POST"]
            )
        )

    def test_sub_resource_beyond_the_old_enumeration_is_mutation(self):
        """The path pattern is shape-based (issue number + one sub-resource segment), not an
        enumerated allowlist — `sub_issues`/`reactions` are real GitHub issue-mutating
        sub-resources the old `comments|labels|assignees|lock` enumeration missed entirely.
        Found during the review-cli quorum pass, agent-tools#597."""
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues/42/sub_issues", "-f", "sub_issue_id=99"]
            )
        )
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues/42/reactions", "-f", "content=+1"]
            )
        )

    def test_unlisted_sub_resource_get_still_not_gated(self):
        """The broadened path shape must not, by itself, gate a plain read — only a mutating
        signal (method/field) on top of a matching path fires the gate."""
        self.assertFalse(
            mod.gh_api_issue_mutation(["gh", "api", "repos/o/r/issues/42/timeline"])
        )

    def test_concrete_comment_route_is_mutation(self):
        """`repos/<owner>/<repo>/issues/comments/<comment_id>` — GitHub's route for mutating ONE
        issue comment directly (it does not nest under an issue number). Review finding,
        agent-tools#597."""
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues/comments/123", "-X", "PATCH", "-f", "body=x"]
            )
        )

    def test_concrete_label_route_is_mutation(self):
        self.assertTrue(
            mod.gh_api_issue_mutation(
                ["gh", "api", "repos/o/r/issues/42/labels/bug", "-X", "DELETE"]
            )
        )


class TaskCliActive(unittest.TestCase):
    def test_active_when_binary_present_and_inside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _fake_task_bin(bin_dir)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{old_path}"
            try:
                self.assertTrue(mod.task_cli_active(str(repo)))
            finally:
                os.environ["PATH"] = old_path

    def test_inactive_when_binary_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(Path(tmp) / "empty-bin")
            try:
                self.assertFalse(mod.task_cli_active(str(repo)))
            finally:
                os.environ["PATH"] = old_path

    def test_inactive_when_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_repo = Path(tmp) / "not-a-repo"
            not_repo.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _fake_task_bin(bin_dir)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{old_path}"
            try:
                self.assertFalse(mod.task_cli_active(str(not_repo)))
            finally:
                os.environ["PATH"] = old_path

    def test_inactive_when_task_binary_is_a_same_named_collision(self):
        """A `task` on PATH that resolves but is NOT task-cli (Taskwarrior/go-task shadowing
        it) must not be treated as an active alternative — review finding, agent-tools#597."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _fake_collision_task_bin(bin_dir)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{old_path}"
            try:
                self.assertFalse(mod.task_cli_active(str(repo)))
            finally:
                os.environ["PATH"] = old_path

    def test_active_when_real_task_cli_signature_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            _fake_task_bin(bin_dir)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{old_path}"
            try:
                self.assertTrue(mod._task_binary_is_task_cli(str(repo)))
            finally:
                os.environ["PATH"] = old_path


class EndToEnd(unittest.TestCase):
    def test_gh_issue_create_blocks_by_default_when_task_cli_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook(
                'gh issue create --title x --body y', cwd=str(repo)
            )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_block_message_carries_stable_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            out = _run_hook_raw('gh issue create --title x', cwd=str(repo))
        self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE)
        self.assertIn(mod.BLOCK_MARKER, out["message"])
        self.assertIn("task new", out["message"])
        self.assertIn(_HATCH_ENV, out["message"])

    def test_gh_issue_view_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('gh issue view 42', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_gh_issue_list_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('gh issue list', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_no_task_binary_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook(
                'gh issue create --title x', cwd=str(repo), task_on_path=False
            )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_outside_git_repo_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_repo = Path(tmp) / "not-a-repo"
            not_repo.mkdir()
            code, decision = run_hook('gh issue create --title x', cwd=str(not_repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_warn_only_opt_out_allows_with_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            out = _run_hook_raw(
                'gh issue create --title x', cwd=str(repo), env_extra={"REQUIRE_TASK_CLI_STRICT": "0"}
            )
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")
        self.assertNotIn(mod.BLOCK_MARKER, out["message"])

    def test_gh_pr_create_is_not_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('gh pr create --title x --body y', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_task_new_command_itself_is_never_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook(
                'task new --title "x" --acceptance "y" --why "z" --impact "w"', cwd=str(repo)
            )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_task_classify_command_itself_is_never_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('task classify "some request" --create', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_non_gh_command_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('git status', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_gh_api_issues_post_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook(
                'gh api repos/o/r/issues -f title=x -f body=y', cwd=str(repo)
            )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_gh_api_issues_get_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('gh api repos/o/r/issues', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_gh_issue_new_blocks_by_default_when_task_cli_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('gh issue new --title x --body y', cwd=str(repo))
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_task_binary_collision_allows(self):
        """A same-named `task` that is NOT task-cli must not be treated as an active
        alternative — the gate should allow (fail open), not block toward an unusable
        `task new`/`task classify`. Review finding, agent-tools#597."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            env = dict(os.environ)
            env.pop("REQUIRE_TASK_CLI_STRICT", None)
            env.pop(_HATCH_ENV, None)
            with tempfile.TemporaryDirectory() as scratch:
                bin_dir = Path(scratch) / "bin"
                bin_dir.mkdir()
                _fake_collision_task_bin(bin_dir)
                env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
                event = json.dumps(
                    {"args": {"command": "gh issue create --title x"}, "cwd": str(repo)}
                )
                proc = subprocess.run(
                    [sys.executable, str(_SCRIPT)],
                    input=event,
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=scratch,
                    timeout=10,
                )
            payload = json.loads((proc.stdout or "").strip())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(payload["decision"], "allow")


class OverMatchRegression(unittest.TestCase):
    """The gate must fire only on a real `gh issue`/`gh api` mutation, never when the words
    merely appear inside some other command's argument/message body (the same over-match class
    require-ticket-before-commit's agent-tools#97 fix guards against)."""

    def test_echo_mentioning_gh_issue_create_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('echo "gh issue create is blocked"', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_grep_for_phrase_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('grep -rn "gh issue create" docs/', cwd=str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_real_mutation_after_mentioning_sibling_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook(
                'echo "gh issue create" && gh issue create --title x', cwd=str(repo)
            )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class WrapperClasses(unittest.TestCase):
    def test_env_prefixed_mutation_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('env FOO=bar gh issue create --title x', cwd=str(repo))
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_sudo_mutation_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('sudo gh issue create --title x', cwd=str(repo))
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_timeout_mutation_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            code, decision = run_hook('timeout 30 gh issue create --title x', cwd=str(repo))
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class HatchEscalation(unittest.TestCase):
    def test_hatch_approved_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", _ALLOW_REPLY_SH)
            out = _run_hook_inproc(
                'gh issue create --title x',
                cwd=str(repo),
                env_extra={_HATCH_ENV: "one-off backfill"},
                tg_ctl=tg_ctl,
            )
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")
        self.assertNotIn(mod.BLOCK_MARKER, out["message"])

    def test_hatch_denied_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", 'printf "no\\n"\nexit 1\n')
            out = _run_hook_inproc(
                'gh issue create --title x',
                cwd=str(repo),
                env_extra={_HATCH_ENV: "one-off backfill"},
                tg_ctl=tg_ctl,
            )
        self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE)
        self.assertEqual(out["decision"], "block")
        self.assertIn("hatch escalation denied", out["message"].lower())

    def test_bare_hatch_value_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", _ALLOW_REPLY_SH)
            out = _run_hook_inproc(
                'gh issue create --title x', cwd=str(repo), env_extra={_HATCH_ENV: "1"}, tg_ctl=tg_ctl
            )
        self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE)
        self.assertEqual(out["decision"], "block")


if __name__ == "__main__":
    unittest.main()
