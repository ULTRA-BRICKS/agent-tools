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
import shlex
import stat
import sys
from pathlib import Path

import pytest

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


def _run(
    command: str,
    monkeypatch,
    *,
    cwd: str | None = None,
    path: str | None = None,
    env: dict[str, str] | None = None,
    raw_event: dict | None = None,
) -> tuple[str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = raw_event if raw_event is not None else {"args": {"command": command}, "cwd": cwd or os.getcwd()}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    if path is not None:
        monkeypatch.setenv("PATH", path)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = brt.main()
    return out.getvalue(), code


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


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


@pytest.mark.parametrize("flags", ["-e", "--eval", "-p", "--print"])
def test_repeated_node_eval_tracker_write_is_inspected(flags):
    write = "fetch('https://forge.example/api/v1/repos/o/r/issues', {method:'POST'})"
    assert brt.is_raw_tracker_write(f"node -e 'console.log(1)' {flags} {shlex.quote(write)}") is True
    if flags.startswith("--"):
        assert brt.is_raw_tracker_write(f"node --eval='console.log(1)' {flags}={shlex.quote(write)}") is True


@pytest.mark.parametrize("command", [
    "node -- -e \"fetch('https://forge.example/api/v1/repos/o/r/issues', {method:'POST'})\"",
    "node -e 'console.log(1)' -- --eval \"fetch('https://forge.example/api/v1/repos/o/r/issues', {method:'POST'})\"",
    "node -e 'console.log(1)' --print '2 + 2'",
])
def test_node_eval_boundaries_and_repeated_reads_are_allowed(command):
    assert brt.is_raw_tracker_write(command) is False


@pytest.mark.parametrize("cluster", ["-iS", "-viS", "-iSfj issue create x"])
def test_env_split_short_clusters_are_unclassifiable(cluster):
    assert brt.is_raw_tracker_write(f"env {shlex.quote(cluster)} 'fj issue create x'") is None


def test_env_glued_split_read_with_uppercase_s_in_value_is_allowed():
    assert brt.is_raw_tracker_write("env '-Sprintf SAFE_SENTINEL'") is False


@pytest.mark.parametrize("command", [
    "env -S 'fj issue create x --body x'",
    "env --split-string='fj issue create x --body x'",
    "env -Sfj\\ issue\\ create\\ x",
    "env -u UNUSED -S 'curl -XPOST https://forge.example/api/v1/repos/o/r/issues -d title=x'",
    "timeout 1 env --split-string 'gh issue create --title x'",
    "env -S 'MODE=fixture fj issue create x --body x'",
    "env -S 'env -u UNUSED fj issue create x --body x'",
])
def test_env_split_string_tracker_writes_blocked(tmp_path, monkeypatch, command):
    assert brt.is_raw_tracker_write(command) is True
    out, code = _run(command, monkeypatch, cwd=str(tmp_path), path=_task_on_path(tmp_path))
    assert code == brt.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("command", [
    "env -S 'fj issue list'",
    "env --split-string='curl -XGET https://forge.example/api/v1/repos/o/r/issues'",
    "env -S 'MODE=fixture fj issue list'",
])
def test_env_split_string_reads_allowed(tmp_path, monkeypatch, command):
    assert brt.is_raw_tracker_write(command) is False
    out, code = _run(command, monkeypatch, cwd=str(tmp_path), path=_task_on_path(tmp_path))
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", ["env -S '${TRACKER} issue create x'", "env --split-string"])
def test_unresolved_env_split_string_is_unhatchable(tmp_path, monkeypatch, command):
    assert brt.is_raw_tracker_write(command) is None
    out, code = _run(command, monkeypatch, cwd=str(tmp_path), path=_task_on_path(tmp_path))
    assert code == brt.BLOCK_EXIT_CODE and _decision(out) == "block"


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


# ── Forgejo / Gitea / raw-API coverage (UB-25) ───────────────────────────────────────────────
#
# Every case runs with a task-cli shim on PATH, so a BLOCK assertion also proves the arming gate
# fired; without it the hook short-circuits to allow and these would pass vacuously. Fixture
# hosts are `.example` names and no command is ever executed — only fed to the parser.

_H = "https://forge.example"
_ISSUES = f"{_H}/api/v1/repos/org/repo/issues"


def _assert_blocks(command: str, tmp_path, monkeypatch) -> None:
    out, code = _run(command, monkeypatch, cwd=str(tmp_path), path=_task_on_path(tmp_path))
    assert code == brt.BLOCK_EXIT_CODE, (command, out)
    assert _decision(out) == "block"


def _assert_allows(command: str, tmp_path, monkeypatch) -> None:
    out, code = _run(command, monkeypatch, cwd=str(tmp_path), path=_task_on_path(tmp_path))
    assert code == 0, (command, out)
    assert _decision(out) == "allow"


def _inline(interpreter: str, flag: str, script: str) -> str:
    return f"{interpreter} {flag} {shlex.quote(script)}"


# Each coordinator-review finding (guard-review.md), pinned by name.
_COORDINATOR_MISSED_WRITES = [
    f"curl -XPOST {_ISSUES}/1",
    f"curl -T payload.json {_ISSUES}/1",
    "gh api --hostname forge.example repos/org/repo/issues --input payload.json",
    f"gh api {_ISSUES}/1 -X POST",
]


@pytest.mark.parametrize("command", _COORDINATOR_MISSED_WRITES)
def test_coordinator_missed_write_now_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


def test_coordinator_false_block_explicit_httpie_get_allowed(tmp_path, monkeypatch):
    """An explicit GET wins over HTTPie's body-implies-POST default."""
    _assert_allows(f"http GET {_ISSUES}/1 title=x", tmp_path, monkeypatch)


_FJ_BLOCKED = [
    'fj issue create "Title here" --body "x"',
    'fj issue create --body "x" "Title here"',
    'fj issue create "t" --body-file notes.md -r owner/repo',
    'fj issue -R origin create "t" --body x',
    'fj issue create -R origin "t" --body x',
    'fj -H git.example.com issue create "t" --body x',
    'fj --host=git.example.com issue create "t" --body x',
    'fj -C /tmp/repo issue comment 5 "hello"',
    "fj --cwd /tmp/repo -H git.example.com issue -R upstream comment 5 hello",
    "fj issue comment 5 --body-file reply.md",
    "fj issue edit 5 title 'New title'",
    "fj issue edit 5 labels --add bug",
    "fj issue close 5",
    'fj issue close 5 -w "closing: done"',
    "fj issue assign 5 alice bob",
    "fj issue unassign 5 alice",
    "fj repo labels create bug",
    "fj repo labels org/repo delete bug",
    "fj -H git.example.com repo labels org/repo edit bug",
    "/opt/homebrew/bin/fj issue create t --body x",
    "timeout 30 fj issue create t --body x",
    "env FJ_TOKEN=redacted fj issue create t --body x",
    "FJ_TOKEN=redacted fj issue create t --body x",
    # A body value that looks like a help flag must not read as `--help`.
    'fj issue create t --body "-h"',
]


@pytest.mark.parametrize("command", _FJ_BLOCKED)
def test_fj_mutation_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_FJ_ALLOWED = [
    "fj issue view 5",
    "fj issue view 5 -R origin",
    'fj issue search "bug"',
    "fj -H git.example.com issue search crash",
    "fj issue browse 5",
    "fj issue templates",
    "fj issue --help",
    "fj issue create --help",
    "fj issue create -h",
    "fj repo labels view",
    "fj repo labels org/repo view",
    "fj pr create --title x --body y",
    "fj pr view 5",
    "fj repo view",
    "fj whoami",
    'echo "run fj issue create to file it"',
    'fj issue search "fj issue create regression"',
]


@pytest.mark.parametrize("command", _FJ_ALLOWED)
def test_fj_read_help_and_pr_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


def test_fj_issue_create_allowed_when_task_cli_absent(tmp_path, monkeypatch):
    """The arming gate still applies: no task-cli, no enforcement (parity with gh/linear)."""
    out, code = _run('fj issue create "t" --body x', monkeypatch, cwd=str(tmp_path), path=_empty_path())
    assert code == 0, out
    assert _decision(out) == "allow"


_TEA_BLOCKED = [
    'tea issues create --title "x" --description "y"',
    'tea issue create --title "x"',
    'tea i create --title "x"',
    "tea issues close 5",
    "tea issues reopen 5",
    "tea issues edit 5 --title y",
    "tea -r owner/repo issues create --title x",
    "tea --login work issues create --title x",
    'tea comment 5 "hello there"',
    "tea label create --name bug",
    "tea labels delete bug",
]


@pytest.mark.parametrize("command", _TEA_BLOCKED)
def test_tea_mutation_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_TEA_ALLOWED = [
    "tea issues",
    "tea issues list",
    "tea issue 5",
    "tea labels list",
    "tea pulls create --title x",
    "tea --help",
    "tea issues create --help",
]


@pytest.mark.parametrize("command", _TEA_ALLOWED)
def test_tea_read_help_and_pr_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


_CURL_BLOCKED = [
    f"curl -X POST {_ISSUES} -d '{{\"title\":\"x\"}}'",
    f"curl -X post {_ISSUES} -d title=x",
    f"curl {_ISSUES} -X POST --json '{{\"title\":\"x\"}}'",
    f"curl --request PATCH {_ISSUES}/5 -d state=closed",
    f"curl -X DELETE {_ISSUES}/comments/9",
    f"curl -sS -X POST -H 'Authorization: token REDACTED' {_ISSUES}/5/comments -d body=hi",  # gitleaks:allow
    f'curl -X POST "{_ISSUES}/5/assets" -F attachment=@shot.png',
    f"curl -XPOST {_ISSUES}",
    f"curl -sXPOST {_ISSUES}",
    f"curl -sSXPATCH {_ISSUES}/5",
    f"curl -dJSON {_ISSUES}",
    f"curl -sd title=x {_ISSUES}",
    f"curl -T payload.json {_ISSUES}/1",
    f"curl --upload-file payload.json {_ISSUES}/1",
    f"curl {_ISSUES} -d title=x",
    f"curl --data-raw '{{\"title\":\"x; rm -rf / && echo $(id)\"}}' {_ISSUES}",
    f"curl --json '{{\"body\":\"a|b&c;d\"}}' {_ISSUES}/5/comments",
    f"curl -F 'title=x' {_ISSUES}",
    f"curl --url {_ISSUES} -X POST",
    f"curl -X POST {_H}/api/v1/repos/org/repo%2Fissues -d title=x",
    f"curl -X POST {_H}/api/v1/repos/org/repo%252Fissues -d title=x",
    f"curl -X POST {_H}/repos/org/repo/issues -d title=x",
    f"curl -X POST {_H}/api/v1/repos/org/repo/labels -d name=bug",
    f"curl -X PATCH {_H}/api/v1/repos/org/repo/labels/3 -d name=bug",
    f"curl -X DELETE {_H}/api/v1/repos/org/repo/labels/3",
    "curl -X POST https://api.github.com/repos/org/repo/issues -d '{}'",
    f"timeout 10 curl -X POST {_ISSUES} -d title=x",
    f"env -u HTTP_PROXY curl -X POST {_ISSUES} -d title=x",
]


@pytest.mark.parametrize("command", _CURL_BLOCKED)
def test_curl_tracker_write_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_CURL_ALLOWED = [
    f"curl {_ISSUES}",
    f"curl -sS {_ISSUES}/5",
    f"curl -X GET {_ISSUES}",
    f"curl -XGET {_ISSUES}/5/comments",
    # Explicit read methods win over a body flag; `-G` moves data into the query string.
    f"curl -X GET -d state=open {_ISSUES}",
    f"curl -G -d state=open {_ISSUES}",
    f"curl -I {_ISSUES}",
    f"curl -X HEAD {_ISSUES}",
    # PR and other non-tracker endpoints.
    f"curl -X POST {_H}/api/v1/repos/org/repo/pulls -d title=x",
    f"curl -X POST {_H}/api/v1/repos/org/repo/pulls/5/reviews -d body=x",
    f"curl -X POST {_H}/api/v1/repos/org/repo/releases -d tag_name=v1",
    # The issues path only in a data VALUE, not in the URL, is not an issues write.
    f"curl -X POST {_H}/api/v1/repos/org/repo/pulls -d 'body=see repos/org/repo/issues/5'",
    "curl --help",
]


@pytest.mark.parametrize("command", _CURL_ALLOWED)
def test_curl_read_or_non_tracker_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


_WGET_HTTPIE_BLOCKED = [
    f"wget --method=POST --body-data='title=x' {_ISSUES}",
    f"wget --method POST --body-data 'title=x' {_ISSUES}",
    f"wget --post-data='title=x' {_ISSUES}",
    f"wget --post-file=issue.json {_ISSUES}",
    f"wget --body-data=x {_ISSUES}",
    f"wget -qO- --method=DELETE {_ISSUES}/comments/9",
    f"http POST {_ISSUES} title=x",
    f"http patch {_ISSUES}/5 state=closed",
    f"http {_ISSUES} title=x",
    f"http {_ISSUES}/5/labels labels:='[1]'",
    f"http --form {_ISSUES} title=x",
    f"http -f {_ISSUES}/5/assets attachment@shot.png",
    f"http --raw '{{\"title\":\"x\"}}' {_ISSUES}",
    f"https POST {_ISSUES} title=x",
    f"xh post {_ISSUES} title=x",
]


@pytest.mark.parametrize("command", _WGET_HTTPIE_BLOCKED)
def test_wget_httpie_tracker_write_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_WGET_HTTPIE_ALLOWED = [
    f"wget -qO- {_ISSUES}",
    f"wget --method=GET {_ISSUES}",
    f"wget --method=GET --body-data=x {_ISSUES}",
    f"http GET {_ISSUES}",
    f"http GET {_ISSUES}/1 title=x",
    f"http {_ISSUES} state==open",
    f"http {_ISSUES} Authorization:'token REDACTED'",  # gitleaks:allow
    f"http --ignore-stdin {_ISSUES}",
    f"http --download GET {_ISSUES}",
    f"http --form GET {_ISSUES} title=x",
    f"http POST {_H}/api/v1/repos/org/repo/pulls title=x",
]


@pytest.mark.parametrize("command", _WGET_HTTPIE_ALLOWED)
def test_wget_httpie_read_or_non_tracker_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


_GH_API_BLOCKED = [
    "gh api --hostname forge.example repos/org/repo/issues -f title=x",
    "gh api --hostname=forge.example repos/org/repo/issues -F title=x",
    "gh --hostname forge.example api repos/org/repo/issues -f title=x",
    "gh api --hostname forge.example -X PATCH repos/org/repo/issues/5 -f state=closed",
    "gh api --hostname forge.example --method DELETE repos/org/repo/issues/comments/9",
    "gh api repos/org/repo/issues/5/comments --hostname forge.example -f body=hi",
    "gh api --hostname forge.example repos/org/repo/issues --input payload.json",
    f"gh api {_ISSUES}/1 -X POST",
    f"gh api {_ISSUES} -f title=x",
    "gh api -XPOST repos/org/repo/issues",
    "gh api -ftitle=x repos/org/repo/issues",
    "gh api -iX POST repos/org/repo/issues",
    "GH_HOST=forge.example gh api repos/org/repo/issues -f title=x",
    # API parity: GitHub's own REST issue/label writes via gh api are blocked too.
    "gh api repos/org/repo/issues -f title=x",
    "gh api repos/org/repo/issues/5 -X PATCH -f state=closed",
    "gh api /repos/org/repo/issues/5/comments -f body=hi",
    "gh api repos/org/repo/issues/5/labels -f 'labels[]=bug'",
    "gh api repos/org/repo/labels -f name=bug",
    "gh api --method DELETE repos/org/repo/labels/bug",
    "gh -R org/repo api repos/org/repo/issues --input issue.json",
    "gh api graphql -f query='mutation { createIssue(input: {repositoryId: \"R\", title: \"x\"}) { issue { id } } }'",
    "gh api graphql -f query='mutation($id: ID!) { closeIssue(input: {issueId: $id}) { clientMutationId } }' -F id=I_1",
]


@pytest.mark.parametrize("command", _GH_API_BLOCKED)
def test_gh_api_tracker_write_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_GH_API_ALLOWED = [
    "gh api --hostname forge.example repos/org/repo/issues",
    "gh api --hostname forge.example -X GET repos/org/repo/issues/5",
    "gh api repos/org/repo/issues",
    "gh api repos/org/repo/issues --paginate --jq '.[].title'",
    "gh api repos/org/repo/pulls -f title=x",
    "gh api repos/org/repo/pulls/5/comments -f body=x",
    "gh api --hostname forge.example repos/org/repo/pulls -f title=x",
    "gh api graphql -f query='query { repository(owner: \"o\", name: \"r\") { issues(first: 5) { nodes { title } } } }'",
    "gh api --help",
]


@pytest.mark.parametrize("command", _GH_API_ALLOWED)
def test_gh_api_read_pr_and_help_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


_INLINE_BLOCKED = [
    _inline("python3", "-c", f"import requests; requests.post('{_ISSUES}', json={{'title': 'x'}})"),
    _inline("python", "-c", f"import requests; requests.patch('{_ISSUES}/5', json={{}})"),
    _inline("python3", "-c", f"import requests; requests.delete('{_ISSUES}/comments/9')"),
    _inline("python3", "-c", f"import requests; requests.Session().post('{_ISSUES}', json={{}})"),
    _inline("python3", "-c", f"import requests; requests.request('POST', '{_ISSUES}', json={{}})"),
    _inline("python3", "-Ic", f"import httpx; httpx.put('{_ISSUES}/5/labels', json={{}})"),
    _inline(
        "python3", "-c",
        f"import urllib.request as u; u.urlopen(u.Request('{_ISSUES}', data=b'{{}}', method='POST'))",
    ),
    _inline("python3", "-c", f"import urllib.request as u; u.urlopen(u.Request('{_ISSUES}', data=b'{{}}'))"),
    _inline("python3", "-c", f"import urllib.request as u; u.urlopen('{_ISSUES}', b'{{}}')"),
    _inline("node", "-e", f"fetch('{_ISSUES}', {{method: 'POST', body: '{{}}'}})"),
    _inline("node", "--eval", f"fetch('{_ISSUES}/5', {{\"method\": \"PATCH\"}})"),
    _inline("node", "-e", f"require('axios').post('{_ISSUES}', {{}})"),
    _inline("bun", "-e", f"await fetch('{_ISSUES}', {{method: 'DELETE'}})"),
    f"python3 <<< {shlex.quote(f'import requests; requests.post({_ISSUES!r})')}",
]


@pytest.mark.parametrize("command", _INLINE_BLOCKED)
def test_inline_program_tracker_write_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_INLINE_ALLOWED = [
    _inline("python3", "-c", f"import requests; print(requests.get('{_ISSUES}').json())"),
    _inline("node", "-e", f"fetch('{_ISSUES}').then(r => r.json())"),
    _inline("node", "-e", f"fetch('{_ISSUES}', {{method: 'GET'}})"),
    _inline("python3", "-c", f"import urllib.request as u; print(u.urlopen('{_ISSUES}').read())"),
    _inline("python3", "-c", f"import urllib.request as u; u.urlopen('{_ISSUES}', timeout=5)"),
    _inline("python3", "-c", f"import requests; requests.post('{_H}/api/v1/repos/org/repo/pulls', json={{}})"),
    _inline("python3", "-c", "print('hello')"),
    # Inert text: a write-looking call or method literal INSIDE a string is not a call.
    _inline("python3", "-c", f"print(\"requests.post('{_ISSUES}')\")"),
    _inline("node", "-e", f"console.log(\"fetch(u, {{method: 'POST'}}) {_ISSUES}\")"),
    _inline("python3", "-c", f"print('use requests.delete on {_ISSUES}/comments/9')"),
]


@pytest.mark.parametrize("command", _INLINE_ALLOWED)
def test_inline_program_read_or_inert_text_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


_HEREDOC_BLOCKED = [
    f"python3 - <<'EOF'\nimport requests\nrequests.post('{_ISSUES}', json={{}})\nEOF",
    f"python3 <<EOF\nimport requests\nrequests.patch('{_ISSUES}/5', json={{}})\nEOF",
    f"node <<'JS'\nfetch('{_ISSUES}', {{method: 'POST'}})\nJS",
    f"python3 - <<-EOF\n\timport requests\n\trequests.delete('{_ISSUES}/comments/9')\n\tEOF",
    "bash <<'EOF'\nfj issue create t --body x\nEOF",
    "sh -s <<'EOF'\necho start\ngh issue create --title x\nEOF",
    f"git status && python3 - <<'EOF'\nimport requests\nrequests.post('{_ISSUES}', json={{}})\nEOF\necho after",
]


@pytest.mark.parametrize("command", _HEREDOC_BLOCKED)
def test_interpreter_heredoc_tracker_write_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_HEREDOC_ALLOWED = [
    f"python3 - <<'EOF'\nimport requests\nprint(requests.get('{_ISSUES}').json())\nEOF",
    # An apostrophe in the program body must not make the whole command unparseable.
    f"python3 - <<'EOF'\nimport requests\nprint(\"it's\", requests.get('{_ISSUES}').status_code)\nEOF",
    "cat <<'EOF'\nsome release notes\nEOF",
    "python3 -c 'import sys; print(sys.stdin.read())' <<'EOF'\nrequests.post(repos/org/repo/issues)\nEOF",
]


@pytest.mark.parametrize("command", _HEREDOC_ALLOWED)
def test_heredoc_read_or_data_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


_WRAPPED_AND_CHAINED_BLOCKED = [
    "bash -c 'fj issue create t --body x'",
    'sh -c "fj issue comment 5 hi"',
    "bash -lc 'fj issue close 5'",
    "zsh -c 'tea issues create --title x'",
    f"bash -c 'curl -XPOST {_ISSUES} -d title=x'",
    "eval 'fj issue create t --body x'",
    "bash -c \"bash -c 'fj issue create t --body x'\"",
    "bash -c 'gh issue create --title x'",
    "git status && fj issue create t --body x && echo done",
    "true; fj issue close 5; echo ok",
    "fj issue view 5 | tee /dev/null; fj issue comment 5 x",
    "echo start || fj issue edit 5 title new",
    f"ls\ncurl -X POST {_ISSUES} -d title=x\necho after",
    "( fj issue create t --body x )",
    "{ fj issue create t --body x; }",
    "if true; then fj issue close 5; fi",
    "fj issue create 'x; y && z | w' --body 'a $(b) `c` d > e < f'",
    'fj issue comment 5 "line1; gh pr view 3 && echo | cat"',
    # A `<<` inside shell arithmetic is a shift, not a heredoc opener: the next lines stay shell.
    "python3 bench.py $((1<<10))\ngh issue create --title bypass --body x",
    "node --max-old-space-size=$((1<<10)) server.js\nfj issue create t --body x",
    "x=$((SIZE<<SHIFT)); python3 run.py\nfj issue close 5",
    "(( n = 1 << 4 )); python3 run.py\ngh issue create --title x",
]


@pytest.mark.parametrize("command", _WRAPPED_AND_CHAINED_BLOCKED)
def test_wrapped_and_chained_mutation_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


_WRAPPED_ALLOWED = [
    "bash -c 'fj issue view 5'",
    "sh -c 'git status && fj pr create --title x'",
    f'echo "fj issue create; curl -X POST {_ISSUES}"',
    "git commit -m 'block fj issue create and curl -X POST .../issues'",
    "task new --title x --why y --impact z --if-not-done w --acceptance a",
    "task --help",
    "bash script.sh",
]


@pytest.mark.parametrize("command", _WRAPPED_ALLOWED)
def test_wrapped_reads_and_prose_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


def test_unparseable_plausible_fj_write_fails_closed(tmp_path, monkeypatch):
    _assert_blocks("fj issue create 'unterminated --body x", tmp_path, monkeypatch)


def test_unparseable_unrelated_command_allowed(tmp_path, monkeypatch):
    _assert_allows("echo 'unterminated", tmp_path, monkeypatch)


# ── GitHub/Linear regression guard: the tokenizer change must not regress existing coverage ──

_GH_LINEAR_STILL_BLOCKED = [
    "gh issue create --title x --body 'see https://example.com/a:b@c'",
    "gh issue comment 5 --body 'a, b, {c} %d +e $f'",
    "gh --repo=o/r issue close 5",
    "gh issue edit 5 --add-label bug",
    "linear issue update ABC-1 --state done",
]


@pytest.mark.parametrize("command", _GH_LINEAR_STILL_BLOCKED)
def test_gh_linear_coverage_not_regressed(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


def test_block_message_does_not_echo_credentials(tmp_path, monkeypatch):
    out, code = _run(
        f"curl -X POST -H 'Authorization: token SYNTHETIC_TOKEN_123' {_ISSUES} -d title=x",  # gitleaks:allow
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_on_path(tmp_path),
    )
    assert code == brt.BLOCK_EXIT_CODE
    assert "SYNTHETIC_TOKEN_123" not in out


# ── event-shape trap ─────────────────────────────────────────────────────────────────────────
#
# The v1 bridge sends `args.command`. A hook reading `tool_input.command` would see an empty
# command and allow everything, while a test helper using the right shape would hide it. These
# pin both halves: the contract shape MUST block, the harness-native shape is not read.


def test_event_shape_args_command_must_block(tmp_path, monkeypatch):
    out, code = _run(
        "",
        monkeypatch,
        path=_task_on_path(tmp_path),
        raw_event={"args": {"command": "fj issue create t --body x"}, "cwd": str(tmp_path)},
    )
    assert code == brt.BLOCK_EXIT_CODE, out


def test_event_shape_tool_input_is_not_the_contract(tmp_path, monkeypatch):
    out, code = _run(
        "",
        monkeypatch,
        path=_task_on_path(tmp_path),
        raw_event={"tool_input": {"command": "fj issue create t --body x"}, "cwd": str(tmp_path)},
    )
    assert code == 0, out


# ── hatch escalation (fake tg-ctl pinned as the only trusted path — never a real Telegram ask) ─

_HATCH_WHY = "Tracker migration backfill approved by the CTO."


def _pin_fake_tg_ctl(monkeypatch, tg_ctl: Path) -> None:
    monkeypatch.setattr(brt.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))


def _run_hatch(command: str, tmp_path, monkeypatch, value: str | None = _HATCH_WHY):
    env = {} if value is None else {"RIG_HATCH_REQUEST_BLOCK_RAW_TICKET": value}
    return _run(command, monkeypatch, cwd=str(tmp_path), path=_task_on_path(tmp_path), env=env)


def test_hatch_approved_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch('fj issue create "t" --body x', tmp_path, monkeypatch)
    assert code == 0, out
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_inline_prefix_form_found(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch(
        f'RIG_HATCH_REQUEST_BLOCK_RAW_TICKET="{_HATCH_WHY}" fj issue create t --body x',
        tmp_path, monkeypatch, value=None,
    )
    assert code == 0, out
    assert marker.exists()


def test_hatch_denied_blocks(tmp_path, monkeypatch):
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n"))
    out, code = _run_hatch("fj issue close 5", tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, out
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


def test_hatch_clean_exit_without_allow_verdict_blocks(tmp_path, monkeypatch):
    """An expired / unanswered ask (tg-ctl exits 0 with no allow decision) must deny."""
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n"))
    out, code = _run_hatch("fj issue close 5", tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, out


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "on", "pytest"])
def test_hatch_blank_bare_or_short_value_never_asks(tmp_path, monkeypatch, value):
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch("fj issue close 5", tmp_path, monkeypatch, value=value)
    assert code == brt.BLOCK_EXIT_CODE, (value, out)
    assert not marker.exists(), value


def test_unparseable_plausible_write_is_never_offered_to_the_hatch(tmp_path, monkeypatch):
    """A command the hook cannot parse cannot be classified, so it is a hard block even with an
    approving hatch — the approval would otherwise cover text nobody could analyse."""
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch("fj issue create 'unterminated --body x", tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, out
    assert not marker.exists()


_NESTED_UNCLASSIFIABLE = [
    "bash -c 'fj issue create \"unterminated --body x'",
    "eval 'fj issue create \"unterminated --body x'",
    "fj issue close 5; bash -c 'fj issue create \"unterminated --body x'",
    "bash <<'EOF'\nfj issue create 'unterminated --body x\nEOF",
]


@pytest.mark.parametrize("command", _NESTED_UNCLASSIFIABLE)
def test_nested_unclassifiable_write_stays_unclassifiable(command):
    """A wrapper must not launder an inner `None` (unclassifiable) into `True` (hatchable)."""
    assert brt.is_raw_tracker_write(command) is None


@pytest.mark.parametrize("command", _NESTED_UNCLASSIFIABLE)
def test_nested_unclassifiable_write_is_never_offered_to_the_hatch(tmp_path, monkeypatch, command):
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch(command, tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, (command, out)
    assert not marker.exists(), command


def test_nesting_beyond_the_depth_cap_is_unclassifiable():
    command = "fj issue view 1"
    for _ in range(brt._MAX_DEPTH + 2):
        command = f"bash -c {shlex.quote(command)}"
    assert brt.is_raw_tracker_write(command) is None


def test_hatch_unavailable_helper_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(brt, "hatch_escalation", brt._UnavailableHatch())
    out, code = _run_hatch("fj issue close 5", tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, out


def test_hatch_asks_once_for_a_multi_mutation_chain(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"echo x >> {calls}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch(
        "fj issue create t --body x && fj issue close 5 && gh issue comment 3 --body y",
        tmp_path, monkeypatch,
    )
    assert code == 0, out
    assert calls.read_text().count("x") == 1


def test_hatch_not_consulted_for_allowed_command(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch("fj issue view 5", tmp_path, monkeypatch)
    assert code == 0, out
    assert not marker.exists()


def test_hatch_does_not_override_junk_title_block(tmp_path, monkeypatch):
    """The Review-uncommitted refusal has no hatch (unchanged behavior)."""
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch('gh issue create --title "Review uncommitted foo diff"', tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, out
    assert not marker.exists()


# ── junk-title parity for the new CLIs (coordinator follow-up, guard-final-review.md #1) ─────
#
# The Review-uncommitted refusal is categorical: it fires even with task-cli absent, and an
# APPROVED raw-write hatch must never let a junk create through, in any shape.

_JUNK_CREATES = [
    'fj issue create "Review uncommitted foo diff" --body x',
    'fj issue create --body x "Review uncommitted foo diff"',
    'fj -H forge.example issue -R origin create "Review uncommitted policy diff"',
    'tea issues create --title "Review uncommitted foo diff"',
    'tea issue create -t "Review uncommitted foo diff"',
    'tea i create --title="Review uncommitted foo diff"',
]


@pytest.mark.parametrize("command", _JUNK_CREATES)
def test_junk_fj_tea_create_detected_by_parser(command):
    assert brt.is_junk_review_ticket_create(command) is True


@pytest.mark.parametrize("command", _JUNK_CREATES)
def test_junk_fj_tea_create_blocked_when_task_cli_absent(tmp_path, monkeypatch, command):
    out, code = _run(command, monkeypatch, cwd=str(tmp_path), path=_empty_path())
    assert code == brt.BLOCK_EXIT_CODE, (command, out)
    assert "review uncommitted" in json.loads(out)["message"].lower()


_JUNK_ANY_SHAPE = _JUNK_CREATES + [
    "bash -c 'fj issue create \"Review uncommitted foo diff\" --body x'",
    "bash -c 'gh issue create --title \"Review uncommitted foo diff\"'",
    f"curl -XPOST {_ISSUES} -d '{{\"title\":\"Review uncommitted foo diff\"}}'",
    "gh api repos/org/repo/issues -f 'title=Review uncommitted foo diff'",
]


@pytest.mark.parametrize("command", _JUNK_ANY_SHAPE)
def test_approved_hatch_never_lets_a_junk_create_through(tmp_path, monkeypatch, command):
    marker = tmp_path / "asked"
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH))
    out, code = _run_hatch(command, tmp_path, monkeypatch)
    assert code == brt.BLOCK_EXIT_CODE, (command, out)
    assert not marker.exists(), command
    assert "review uncommitted" in json.loads(out)["message"].lower()


# ── review iteration 5 findings (GLM seat, sess-20260922T202421_455036) ─────────────────────

_WRAPPED_BY_RUNNERS_BLOCKED = [
    "echo 42 | xargs -I{} fj issue close {}",
    "printf '5\\n' | xargs -n1 fj issue close",
    "xargs -a ids.txt -I% fj issue comment % done",
    "sudo fj issue close 5",
    "sudo -u deploy fj issue create t --body x",
    "doas fj issue close 5",
    "doas -a pam fj issue create t --body x",
    "echo 5 | xargs --process-slot-var=SLOT fj issue close",
    "echo 5 | xargs --process-slot-var SLOT fj issue close",
    f"uv run python -c \"import requests; requests.post('{_ISSUES}', json={{}})\"",
    f"uv run --with requests python3 -c \"import requests; requests.post('{_ISSUES}', json={{}})\"",
    "poetry run fj issue close 5",
    f"python3.11 -c \"import requests; requests.post('{_ISSUES}', json={{}})\"",
    f"pypy3 -c \"import requests; requests.post('{_ISSUES}', json={{}})\"",
    f"wget --post-file issue.json {_ISSUES}",
]


@pytest.mark.parametrize("command", _WRAPPED_BY_RUNNERS_BLOCKED)
def test_runner_and_versioned_interpreter_wrappers_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "command",
    [
        "echo 42 | xargs -I{} fj issue view {}",
        "sudo fj issue view 5",
        "uv run pytest tests/",
        "uv run python -c 'print(1)'",
    ],
)
def test_runner_wrappers_around_reads_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "mutation",
    ["reopenIssue", "deleteIssue", "transferIssue", "pinIssue", "unpinIssue", "updateLabel", "deleteLabel"],
)
def test_unparseable_graphql_mutation_fails_closed(tmp_path, monkeypatch, mutation):
    command = f"echo it's; gh api graphql -f query='mutation{{{mutation}(input:{{id:\"I_1\"}}){{clientMutationId}}}}'"
    _assert_blocks(command, tmp_path, monkeypatch)


def test_crlf_heredoc_delimiter_is_recognized(tmp_path, monkeypatch):
    """A CRLF-terminated delimiter still closes the heredoc; the following line is shell again."""
    command = (
        "python3 - <<'EOF'\r\nimport requests\r\nprint(requests.get('"
        + _ISSUES
        + "').json())\r\nEOF\r\nfj issue close 5"
    )
    _assert_blocks(command, tmp_path, monkeypatch)
    assert brt.is_raw_tracker_write(command.rsplit("\n", 1)[0]) is False


# ── review iteration (job 20260922T211004-048e8053) findings ─────────────────────────────────

_UNRESOLVED_METHOD_OR_PATH_BLOCKED = [
    # An explicit method the hook cannot resolve to a known verb must not disable the write check.
    f"curl -X$'POST' {_ISSUES} -d title=x",
    f"curl -X \"$M\" {_ISSUES} -d title=x",
    f"wget --method=$'POST' --body-data=x {_ISSUES}",
    f"http $'POST' {_ISSUES} title=x",
    "gh api -X $'POST' repos/org/repo/issues",
    # Path normalization: curl/wget/HTTPie collapse `.`, `..` and `//` before sending.
    f"curl -XPOST {_H}/api/v1/repos/org/repo/./issues -d title=x",
    f"curl -XPOST {_H}/api/v1/repos/org/repo/x/../issues -d title=x",
    f"curl -XPOST {_H}/api/v1/repos/org/repo//issues -d title=x",
    f"http POST {_H}/api/v1/repos/org/repo/x/../issues title=x",
    "gh api -X POST repos/org/repo/./labels -f name=x",
]


@pytest.mark.parametrize("command", _UNRESOLVED_METHOD_OR_PATH_BLOCKED)
def test_unresolved_method_and_unnormalized_path_blocked(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "command",
    [
        # curl picks the LAST method-setting flag; a body flag next to `-I` must not read as HEAD.
        f"curl -I -d '{{\"title\":\"x\"}}' {_ISSUES}",
        f"curl -d title=x -I {_ISSUES}",
        f"curl -I -T payload.json {_ISSUES}/1",
        f"curl --head --data title=x {_ISSUES}",
    ],
)
def test_curl_head_flag_never_downgrades_a_body_write(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


@pytest.mark.parametrize("command", [f"curl -I {_ISSUES}", f"curl -I -G -d state=open {_ISSUES}"])
def test_curl_head_without_body_stays_a_read(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "command",
    [
        # `--next` / `-:` start an independent request; a `-X GET` on request 1 does not carry over.
        f"curl -X GET https://safe.example/ping --next {_ISSUES} -d title=x",
        f"curl -X GET https://safe.example/ping -: -X POST {_ISSUES}",
        f"curl {_ISSUES} --next -T payload.json {_ISSUES}/1",
        f"curl -X GET https://safe.example/ping -s: --json '{{}}' {_ISSUES}",
        f"curl -X GET https://safe.example/ping -:s --json '{{}}' {_ISSUES}",
    ],
)
def test_curl_next_request_group_is_judged_separately(tmp_path, monkeypatch, command):
    _assert_blocks(command, tmp_path, monkeypatch)


def test_curl_next_with_only_reads_allowed(tmp_path, monkeypatch):
    _assert_allows(f"curl -X GET https://safe.example/ping --next {_ISSUES}", tmp_path, monkeypatch)


@pytest.mark.parametrize("option", ["-o", "-so", "--output"])
@pytest.mark.parametrize("operand", ["--next", "-:"])
def test_curl_next_as_option_operand_does_not_split_a_post(tmp_path, monkeypatch, option, operand):
    _assert_blocks(f"curl --json '{{\"title\":\"x\"}}' {option} {operand} {_ISSUES}", tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "command",
    [
        f"curl -X OPTIONS {_ISSUES}",
        f"curl -X get {_H}/api/v1/repos/org/repo/./issues",
        f"curl -XPOST {_H}/api/v1/repos/org/repo/./pulls -d title=x",
    ],
)
def test_known_read_method_and_normalized_pr_path_allowed(tmp_path, monkeypatch, command):
    _assert_allows(command, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "command",
    [
        'fj issue create "Fix login redirect" --body "review uncommitted changes first"',
        'fj issue search "Review uncommitted"',
        'tea issues create --title "Fix login" --description "Review uncommitted diff later"',
    ],
)
def test_junk_phrase_outside_a_create_title_is_not_the_junk_class(command):
    assert brt.is_junk_review_ticket_create(command) is False


# ── agent-tools#994: role-aware escalation ladder ─────────────────────────────────────────────
# A block must say WHAT TO DO NEXT, and the ladder differs by WHO reads it: a subagent redoes the
# write with task-cli and messages its coordinator (never the human); the coordinator relays and is
# the only one holding the human hatch; an unidentified reader gets both branches.
_SESSION_DIR = "2026-10-24T10-11-12-345Z_0a1b2c3d-1111-2222-3333-444455556666"
_SUBAGENT_ENV = {
    "RIG_AGENT_ID": "Kid",
    "PI_SESSION_FILE": f"/home/u/.omp/sessions/proj/{_SESSION_DIR}/Kid.jsonl",
}
_COORDINATOR_ENV = {"PI_SESSION_FILE": f"/home/u/.omp/sessions/proj/{_SESSION_DIR}.jsonl"}
_ROLE_ENV_VARS = ("RIG_AGENT_ID", "RIG_PARENT_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE")
_HATCH_VAR = "RIG_HATCH_REQUEST_BLOCK_RAW_TICKET"


def _task_path_once(tmp_path) -> str:
    """`_task_on_path` mkdirs; the ladder tests call the hook several times per tmp_path."""
    if (tmp_path / "bin" / "task").exists():
        return f"{tmp_path / 'bin'}{os.pathsep}/usr/bin:/bin"
    return _task_on_path(tmp_path)


def _ladder_message(tmp_path, monkeypatch, role_env: dict[str, str], command: str = "fj issue create t --body x") -> str:
    for name in _ROLE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(_HATCH_VAR, raising=False)
    out, code = _run(
        command, monkeypatch, cwd=str(tmp_path), path=_task_path_once(tmp_path), env=role_env
    )
    assert code == brt.BLOCK_EXIT_CODE, out
    return json.loads(out)["message"]


def test_ladder_leads_with_marker_and_task_new_remedy(tmp_path, monkeypatch):
    for env in ({}, _SUBAGENT_ENV, _COORDINATOR_ENV):
        msg = _ladder_message(tmp_path, monkeypatch, env)
        assert msg.startswith("[block-raw-ticket] BLOCKED")
        step1 = msg[msg.index("1. "):msg.index("2. ")]
        assert "task new --title" in step1
        for flag in ("--acceptance", "--why", "--impact", "--if-not-done"):
            assert flag in step1
        assert "Closes" not in msg and "Fixes" not in msg
        assert "ASK the human" not in msg
        assert len(msg) <= 1300


def test_ladder_subagent_messages_parent_and_has_no_human_hatch(tmp_path, monkeypatch):
    msg = _ladder_message(tmp_path, monkeypatch, _SUBAGENT_ENV)
    assert "`write agent://Main`" in msg
    assert "`yield` status `awaiting-coordinator` (NOT blocked/done/failed)" in msg
    assert "`write agent://Kid`" in msg
    assert "never retry with RIG_HATCH_REQUEST_* (coordinator only)" in msg
    assert _HATCH_VAR not in msg


def test_ladder_coordinator_relays_and_hatch_is_last_line(tmp_path, monkeypatch):
    msg = _ladder_message(tmp_path, monkeypatch, _COORDINATOR_ENV)
    assert "write agent://<id>" in msg
    last = msg.rstrip().splitlines()[-1]
    assert _HATCH_VAR in last
    assert msg.count(_HATCH_VAR) == 1


def test_ladder_unknown_role_has_both_branches(tmp_path, monkeypatch):
    msg = _ladder_message(tmp_path, monkeypatch, {})
    assert "If you were dispatched by another agent" in msg
    assert "If you are the top-level agent" in msg
    assert _HATCH_VAR in msg.rstrip().splitlines()[-1]


def test_ladder_hatch_denied_path_keeps_the_ladder(tmp_path, monkeypatch):
    _pin_fake_tg_ctl(monkeypatch, _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n"))
    for name in _ROLE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    out, code = _run(
        "fj issue close 5",
        monkeypatch,
        cwd=str(tmp_path),
        path=_task_path_once(tmp_path),
        env={_HATCH_VAR: _HATCH_WHY, **_SUBAGENT_ENV},
    )
    msg = json.loads(out)["message"]
    assert code == brt.BLOCK_EXIT_CODE
    assert msg.startswith("hatch escalation denied")
    assert "task new --title" in msg and "write agent://Main" in msg


def test_ladder_junk_block_names_review_diff_and_never_offers_the_hatch(tmp_path, monkeypatch):
    for name in _ROLE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for env, needle in ((_SUBAGENT_ENV, "write agent://Main"), ({}, "If you were dispatched")):
        for name in _ROLE_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        out, code = _run(
            'gh issue create --title "Review uncommitted changes" --body x',
            monkeypatch,
            cwd=str(tmp_path),
            path=_task_path_once(tmp_path),
            env=env,
        )
        msg = json.loads(out)["message"]
        assert code == brt.BLOCK_EXIT_CODE
        assert "review diff" in msg and needle in msg
        assert _HATCH_VAR not in msg
        assert "Closes" not in msg and "Fixes" not in msg


def test_ladder_falls_back_to_neutral_text_when_lib_is_unavailable(monkeypatch):
    monkeypatch.setattr(brt, "_guard_message", None)
    msg = brt._render_block("[block-raw-ticket] BLOCKED: x.", "Use `task new --title t`.", "hatch line")
    assert msg.startswith("[block-raw-ticket] BLOCKED")
    assert "task new --title" in msg and "dispatched by another agent" in msg
