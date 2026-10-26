"""Tests for the local-path-in-gh agent-hook (pre-bash).

Doctrine (agent-tools#1002): text written to GitHub (PR / issue / comment / review / release
notes, and the task-cli fields that write to a tracker) must never name a local filesystem
path. A local path is unreadable by everyone on GitHub and gone after a reboot, so as a proof
or a reference it is fake. The hook DENIES such a write and tells the agent to attach the
content itself; the only bypass is the one-time Telegram hatch
`RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH`.

Every case drives the real hook entrypoint (`main()`) with a real event on stdin; body files
are real files in a pytest tmp dir. No usernames are hardcoded: the "home" under test is
derived from pytest's tmp dir and the process's own identity.

Run from the repo root::

    python -m pytest tests/test_local_path_in_gh.py -q
"""

from __future__ import annotations

import getpass
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

_HOOK_DIR = Path(__file__).resolve().parents[1] / "agent-hooks" / "local-path-in-gh"
_HOOK = _HOOK_DIR / "local_path_in_gh.py"
_spec = importlib.util.spec_from_file_location("local_path_in_gh", _HOOK)
assert _spec and _spec.loader
lpg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lpg)

HATCH_VAR = "RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH"
# A stand-in account name that is not anyone's real one; derived, never a literal login.
FAKE_USER = f"acct{os.getpid()}"


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    """Point HOME at a throwaway dir so 'the current home' is a value the tests control."""
    home = tmp_path / "homedir" / FAKE_USER
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(HATCH_VAR, raising=False)
    return home


def _run(command: str, monkeypatch, cwd: Path, env: dict | None = None) -> tuple[dict, int]:
    event = {"cwd": str(cwd), "args": {"command": command}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    code = lpg.main()
    return json.loads(out.getvalue()), code


def _blocked(command: str, monkeypatch, cwd: Path) -> str:
    result, code = _run(command, monkeypatch, cwd)
    assert code == lpg.BLOCK_EXIT_CODE, f"expected BLOCK for: {command}\n{result}"
    assert result["decision"] == "block"
    return result["message"]


def _allowed(command: str, monkeypatch, cwd: Path) -> None:
    result, code = _run(command, monkeypatch, cwd)
    assert code == 0 and result["decision"] == "allow", f"expected ALLOW for: {command}\n{result}"


# ── BLOCK: gh pr / issue inline text ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title t --body 'proof: /tmp/proofs/check-gl.txt'",
        "gh pr create --title t --body '![shot](/tmp/proofs/check-gl.txt)'",
        "gh pr create --title t --body='see /var/tmp/run.log'",
        "gh pr create -t t -b 'see /dev/shm/x'",
        "gh pr create -t 'fix /tmp/x' -b ok",
        "gh pr create --title t --body 'log in /var/folders/zz/T/out.txt'",
        "gh pr create --title t --body 'socket /run/user/1000/bus'",
        "gh issue create --title t --body 'looked in /root/.cache'",
        "gh issue comment 4 --body 'artifact at /private/tmp/a.png'",
        "gh issue edit 4 --body 'see ~/Downloads/shot.png'",
        "gh pr comment 7 --body 'copy to $HOME/out.txt'",
        "gh pr comment 7 --body 'copy to ${HOME}/out.txt'",
        "gh pr review 7 --approve --body 'evidence: file:///tmp/e.png'",
        "gh pr review 7 --comment -b 'C:\\Users\\dev\\shot.png'",
        "gh pr review 7 --comment -b 'D:/work/shot.png'",
        "gh pr merge 7 --squash --body 'see /tmp/x'",
        "gh issue close 4 --comment 'done, see /tmp/x'",
        "gh -R o/r pr create --title t --body '/tmp/x'",
        "GH_TOKEN=x gh pr create --title t --body '/tmp/x'",
        "/usr/bin/gh pr create --title t --body '/tmp/x'",
        "env FOO=1 gh pr create --title t --body '/tmp/x'",
        "gh release create v1 --notes 'binary at /tmp/build/app'",
        "gh release edit v1 -n 'binary at /tmp/build/app'",
        "gh pr create --title t --body 'multi\nline\n/tmp/x'",
        "gh pr create --title t --body \"it's at /tmp/x\"",
    ],
)
def test_gh_inline_text_with_local_path_blocks(command, tmp_path, monkeypatch):
    message = _blocked(command, monkeypatch, tmp_path)
    assert "BLOCKED" in message


def test_home_placeholders_and_generic_user_dirs_block(tmp_path, monkeypatch):
    for text in (f"/home/{FAKE_USER}/shot.png", f"/Users/{FAKE_USER}/shot.png"):
        message = _blocked(f"gh pr comment 1 --body 'see {text}'", monkeypatch, tmp_path)
        assert FAKE_USER not in message


def test_current_home_value_is_matched_and_masked(tmp_path, monkeypatch, _fake_home):
    shot = f"{_fake_home}/Pictures/shot.png"
    message = _blocked(f"gh pr comment 1 --body 'see {shot}'", monkeypatch, tmp_path)
    assert str(_fake_home) not in message
    assert FAKE_USER not in message
    assert "<home>" in message


def test_runtime_home_outside_the_usual_roots_is_matched(tmp_path, monkeypatch):
    odd_home = tmp_path / "srv-data" / f"u{FAKE_USER}"
    odd_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(odd_home))
    message = _blocked(f"gh pr comment 1 --body 'see {odd_home}/x.png'", monkeypatch, tmp_path)
    assert str(odd_home) not in message and "<home>" in message


# ── BLOCK: body files, stdin, same-command files ─────────────────────────────────────────────

def test_body_file_contents_are_scanned(tmp_path, monkeypatch):
    body = tmp_path / "body.md"
    body.write_text("## Proof\n\n![x](/tmp/proofs/check-gl.txt)\n")
    message = _blocked(f"gh pr create --title t --body-file {body}", monkeypatch, tmp_path)
    assert "--body-file" in message


def test_short_F_body_file_contents_are_scanned(tmp_path, monkeypatch):
    body = tmp_path / "body.md"
    body.write_text("see /var/tmp/x\n")
    _blocked(f"gh pr comment 1 -F {body}", monkeypatch, tmp_path)


def test_relative_body_file_resolves_against_event_cwd(tmp_path, monkeypatch):
    (tmp_path / "body.md").write_text("see /var/tmp/x\n")
    _blocked("gh pr comment 1 --body-file body.md", monkeypatch, tmp_path)


def test_release_notes_file_contents_are_scanned(tmp_path, monkeypatch):
    notes = tmp_path / "notes.md"
    notes.write_text("artifacts: /tmp/dist\n")
    _blocked(f"gh release create v1 --notes-file {notes}", monkeypatch, tmp_path)


def test_heredoc_on_stdin_body_file_is_scanned(tmp_path, monkeypatch):
    command = "gh pr comment 1 --body-file - <<'EOF'\n## Evidence\nlog: /tmp/run.log\nEOF\n"
    message = _blocked(command, monkeypatch, tmp_path)
    assert "heredoc" in message.lower() or "stdin" in message.lower()


def test_heredoc_inside_command_substitution_is_scanned(tmp_path, monkeypatch):
    command = (
        "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\nshot: /dev/shm/a.png\nEOF\n)\""
    )
    _blocked(command, monkeypatch, tmp_path)


def test_stdin_redirect_file_contents_are_scanned(tmp_path, monkeypatch):
    body = tmp_path / "b.md"
    body.write_text("see ~/x\n")
    _blocked(f"gh pr comment 1 --body-file - < {body}", monkeypatch, tmp_path)


def test_piped_echo_and_cat_feed_stdin_body(tmp_path, monkeypatch):
    _blocked("echo 'see /tmp/x' | gh pr comment 1 --body-file -", monkeypatch, tmp_path)
    body = tmp_path / "b.md"
    body.write_text("see /tmp/x\n")
    _blocked(f"cat {body} | gh pr comment 1 --body-file -", monkeypatch, tmp_path)


def test_file_written_earlier_in_the_same_command_is_judged_by_what_it_will_hold(
    tmp_path, monkeypatch
):
    """A stale clean file on disk must not hide the dirty text the same command writes into it."""
    (tmp_path / "b.md").write_text("clean\n")
    command = "cat > b.md <<'EOF'\nshot at /tmp/x.png\nEOF\ngh pr comment 1 --body-file b.md"
    _blocked(command, monkeypatch, tmp_path)
    command = "echo 'shot at /tmp/x.png' > b.md && gh pr comment 1 --body-file b.md"
    _blocked(command, monkeypatch, tmp_path)


# ── BLOCK: gh api writes ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/issues/1/comments -f body='see /tmp/x'",
        "gh api repos/o/r/issues/1/comments -F body='see /tmp/x'",
        "gh api repos/o/r/issues/1/comments --raw-field body='see /tmp/x'",
        "gh api repos/o/r/issues/1/comments --field body='see /tmp/x'",
        "gh api repos/o/r/issues/1/comments -fbody='see /tmp/x'",
        "gh api repos/o/r/issues/1/comments -f body=/tmp/x",
        "gh api -X PATCH repos/o/r/issues/comments/9 -f body='~/x.png'",
        "gh api --method POST repos/o/r/pulls/3/reviews -f body='/tmp/x' -f event=COMMENT",
        "gh api repos/o/r/pulls/3/comments -f body='/tmp/x' -f path=a.c -F line=3",
        "gh api repos/o/r/pulls/3 -X PATCH -f title='/tmp/x'",
        "gh api repos/o/r/releases -f tag_name=v1 -f body='/tmp/x'",
        "gh api graphql -f query='mutation { addComment(input:{body:\"/tmp/x\"}) { clientMutationId } }'",
        "gh api repos/o/r/issues -f title=t -f body='/tmp/x'",
    ],
)
def test_gh_api_text_field_with_local_path_blocks(command, tmp_path, monkeypatch):
    message = _blocked(command, monkeypatch, tmp_path)
    assert "BLOCKED" in message


def test_gh_api_field_from_file_reads_the_file(tmp_path, monkeypatch):
    body = tmp_path / "b.md"
    body.write_text("see /tmp/x\n")
    _blocked(f"gh api repos/o/r/issues/1/comments -F body=@{body}", monkeypatch, tmp_path)


def test_gh_api_raw_field_with_at_sign_is_literal_text_not_a_file(tmp_path, monkeypatch):
    """`-f key=@file` sends the literal string (only `-F` reads the file)."""
    body = tmp_path / "b.md"
    body.write_text("see /tmp/x\n")
    _allowed(f"gh api repos/o/r/issues/1/comments -f body=@{body}", monkeypatch, tmp_path)


def test_gh_api_input_file_is_scanned(tmp_path, monkeypatch):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"body": "see /tmp/x"}))
    _blocked(f"gh api repos/o/r/issues/1/comments --input {payload}", monkeypatch, tmp_path)


def test_gh_api_endpoint_itself_is_never_scanned(tmp_path, monkeypatch):
    _allowed("gh api repos/o/r/issues/1/comments -f body='clean'", monkeypatch, tmp_path)
    _allowed("gh api repos/tmp/home/issues/1/comments -f body='clean'", monkeypatch, tmp_path)


def test_gh_api_reads_and_unrelated_endpoints_are_ignored(tmp_path, monkeypatch):
    _allowed("gh api repos/o/r/issues/1/comments", monkeypatch, tmp_path)
    _allowed("gh api -X GET repos/o/r/issues -f state=open -f q=/tmp/x", monkeypatch, tmp_path)
    _allowed("gh api repos/o/r/contents/a.txt -X PUT -f content=/tmp/x -f message=m", monkeypatch, tmp_path)
    _allowed("gh api user", monkeypatch, tmp_path)


# ── BLOCK: task-cli fields ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "task check 4 1 --proof /tmp/proofs/check-gl.txt",
        "task check 4 --all --proof /tmp/a.png",
        "task check 4 1 2 --proof a.png --proof /tmp/b.png",
        "task check 4 1 --screenshot /tmp/a.png",
        "task check 4 1 --proof ~/shots/a.png",
        "task check 4 1 --force 'proven by /tmp/run.log'",
        "task new --title t --what 'see /tmp/x' --why w --impact i --if-not-done n",
        "task create --title t --why 'found in ~/notes.md'",
        "task new --title t --acceptance 'proof in /var/tmp/p.png'",
        "task change 4 --screenshot /tmp/a.png",
        "task change 4 --what 'now see /tmp/x'",
        "task change 4 --skip-screenshots 'captured in /tmp/x'",
        "task attach 4 /tmp/a.png",
        "task attach 4 ok.png /dev/shm/a.png",
        "task comment 4 'see /tmp/x'",
        "task done 4 --screenshot /tmp/a.png",
    ],
)
def test_task_cli_fields_with_local_path_block(command, tmp_path, monkeypatch):
    message = _blocked(command, monkeypatch, tmp_path)
    assert "BLOCKED" in message


def test_task_comment_body_file_contents_are_scanned(tmp_path, monkeypatch):
    body = tmp_path / "c.md"
    body.write_text("see /tmp/x\n")
    _blocked(f"task comment 4 --body-file {body}", monkeypatch, tmp_path)


def test_task_cwd_and_config_flags_are_not_text(tmp_path, monkeypatch):
    _allowed(f"task -C {tmp_path} check 4 1 --proof https://example.com/p.png", monkeypatch, tmp_path)
    _allowed(f"task change 4 --config {tmp_path}/task.yaml --what ok", monkeypatch, tmp_path)
    _allowed(f"task new -C {tmp_path} --title t --why w", monkeypatch, tmp_path)


# ── ALLOW ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title t --body 'plain text, no paths at all'",
        "gh pr create --title t --body 'see https://example.com/tmp/y and https://x.io/home/z.png'",
        "gh pr create --title t --body 'see [log](https://example.com/run.log)'",
        "gh pr create --title t --body '![shot](https://user-images.example.com/a.png)'",
        "gh pr create --title t --body 'edit src/foo.cpp and scripts/x.sh, ./relative/file.c, ../up/x'",
        "gh pr create --title t --body 'in tests/tmp/data and docs/home/x.md and a/root/b'",
        "gh pr create --title t --body 'ratio 3/4 and and/or and w/o'",
        "gh pr create --title t --body 'mail me@host.io, version 1.2.3-tmp'",
        "gh pr create --title t --body 'clone git@example.com:org/repo.git'",
        "gh pr create --title t --body 'shot: ![x](shots/a.png)'",
        "gh pr create --title t --body 'see /home/<user>/shot and /Users/<user>/x and /home/${USER}/y'",
        "gh pr create --title t --body 'cost ~5 minutes, ~10%'",
        "gh pr create --title t --body 'price $HOMEBREW and ${HOMEPAGE}'",
        "gh pr create --title t --body '```text\nbuild ok\n```\n<details><summary>log</summary>\n\nok\n\n</details>'",
        "gh pr view 7",
        "gh pr list --search 'in:title /tmp'",
        "gh pr diff 7",
        "gh pr checkout 7",
        "gh issue list",
        "gh issue view 4 --comments",
        "gh pr create --fill",
        "gh pr comment 1 --body ok",
        "gh gist create /tmp/shot-log.txt",
        "gh release upload v1 /tmp/build/app.tar.gz",
        "gh release create v1 /tmp/build/app.tar.gz --notes 'release'",
        "gh api repos/o/r/issues/1/comments",
        "task new --title t --why w --impact i",
        "task check 4 1 --proof https://example.com/p.png",
        "task check 4 1 --proof shot.png",
        "task check 4 1 --proof ./shots/a.png",
        "task check 4 1 --force 'covered by the named test test_x'",
        "task attach 4 shot.png",
        "task read 4",
        "task list",
        "task gate 4",
        "ls /tmp && cat ~/notes.txt | wc -l",
        "echo /tmp/x",
        "cp /tmp/a.png ./shots/a.png",
        "git commit -m 'note /tmp/x'",
        "tg report --title t --ticket 4 --impact i --verify-cmd c --verify-result r --photo /tmp/a.png --file /tmp/b.log",
        "tg --photo /tmp/shot.png 'caption without paths'",
        "curl -s https://example.com/tmp/x",
    ],
)
def test_clean_or_ungated_commands_are_allowed(command, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


def test_clean_body_file_is_allowed_even_though_its_own_path_is_a_tmp_path(tmp_path, monkeypatch):
    """The remediation: the NAME of the body file is a local path by construction, its CONTENT is
    what lands on GitHub. Only content is judged."""
    body = tmp_path / "body.md"
    body.write_text("## Proof\n\n```text\nPASS 12 tests\n```\n")
    assert str(tmp_path).startswith("/")
    _allowed(f"gh pr create --title t --body-file {body}", monkeypatch, tmp_path)
    _allowed(f"cat {body} | gh pr comment 1 --body-file -", monkeypatch, tmp_path)
    _allowed(f"gh pr comment 1 --body-file - < {body}", monkeypatch, tmp_path)
    _allowed(f"gh api repos/o/r/issues/1/comments -F body=@{body}", monkeypatch, tmp_path)


def test_missing_body_file_is_not_this_hooks_business(tmp_path, monkeypatch):
    _allowed(f"gh pr create --title t --body-file {tmp_path}/nope.md", monkeypatch, tmp_path)


def test_path_in_another_chain_segment_does_not_taint_a_clean_gh_call(tmp_path, monkeypatch):
    _allowed("echo /tmp/x && gh pr comment 1 --body ok", monkeypatch, tmp_path)
    _allowed("ls /tmp; gh pr comment 1 --body ok | cat", monkeypatch, tmp_path)


def test_markdown_target_rules(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    _blocked("gh pr comment 1 --body '[doc](/docs/a.md)'", monkeypatch, tmp_path)  # site-root link
    _blocked("gh pr comment 1 --body '![x](/srv/shots/a.png)'", monkeypatch, tmp_path)
    _blocked("gh pr comment 1 --body '[x](/nowhere/a.png)'", monkeypatch, tmp_path)
    _blocked("gh pr comment 1 --body '<img src=\"/srv/shots/a.png\">'", monkeypatch, tmp_path)
    _allowed("gh pr comment 1 --body '![x](https://h.io/a.png) [y](#anchor) [z](mailto:a@b.io)'", monkeypatch, tmp_path)


# ── shell shapes ─────────────────────────────────────────────────────────────────────────────

def test_chained_commands_and_wrappers_are_judged(tmp_path, monkeypatch):
    _blocked("git push && gh pr create --title t --body '/tmp/x'", monkeypatch, tmp_path)
    _blocked("bash -c \"gh pr comment 1 --body '/tmp/x'\"", monkeypatch, tmp_path)
    _blocked("eval \"gh pr comment 1 --body '/tmp/x'\"", monkeypatch, tmp_path)
    _blocked("gh pr create \\\n  --title t \\\n  --body '/tmp/x'", monkeypatch, tmp_path)


def test_unparseable_gh_segment_falls_back_to_a_raw_scan(tmp_path, monkeypatch):
    _blocked("gh pr comment 1 --body 'it is /tmp/x", monkeypatch, tmp_path)
    _allowed("gh pr comment 1 --body 'it is fine", monkeypatch, tmp_path)


# ── the block message ────────────────────────────────────────────────────────────────────────

def test_message_names_the_flag_the_token_and_the_remediation(tmp_path, monkeypatch):
    message = _blocked(
        "gh pr create --title t --body 'proof: /tmp/proofs/check-gl.txt'", monkeypatch, tmp_path
    )
    lines = message.splitlines()
    assert lines[0].startswith("local-path-in-gh BLOCKED")
    assert lines[1].startswith("TRIGGER:")
    assert lines[2].startswith("WHY:")
    assert lines[3].startswith("DO THIS:")
    assert any(line.startswith("EXCEPTION:") for line in lines)
    assert "gh pr create" in lines[1] and "--body" in lines[1]
    assert "/tmp/proofs/check-gl.txt" in lines[1]
    do_this = "\n".join(lines[3:])
    for needle in ("fenced", "```text", "<details>", "https", "repo-relative", "<path>"):
        assert needle in do_this, needle
    assert HATCH_VAR in message
    assert "bare" in message


def test_message_for_a_task_proof_says_how_a_proof_is_attached(tmp_path, monkeypatch):
    message = _blocked("task check 4 1 --proof /tmp/proofs/check-gl.txt", monkeypatch, tmp_path)
    assert "task check" in message and "--proof" in message
    assert "https" in message and "--force" in message


def test_message_for_body_file_names_the_file_flag(tmp_path, monkeypatch):
    body = tmp_path / "body.md"
    body.write_text("see /tmp/x\n")
    message = _blocked(f"gh pr create --title t --body-file {body}", monkeypatch, tmp_path)
    assert "--body-file" in message and "contents" in message


def test_message_lists_several_findings_without_flooding(tmp_path, monkeypatch):
    body = " ".join(f"/tmp/f{i}.txt" for i in range(12))
    message = _blocked(f"gh pr comment 1 --body '{body}'", monkeypatch, tmp_path)
    assert "/tmp/f0.txt" in message and "more" in message
    assert "/tmp/f11.txt" not in message


def test_message_and_sources_never_contain_a_real_username():
    login = getpass.getuser()
    assert len(login) >= 2
    shipped = [_HOOK, _HOOK_DIR / "README.md", _HOOK_DIR / "local-path-in-gh.pre-bash.json", Path(__file__)]
    for path in shipped:
        text = path.read_text(encoding="utf-8")
        assert not re.search(rf"(?<![A-Za-z0-9_]){re.escape(login)}(?![A-Za-z0-9_])", text), path.name


def test_descriptor_is_a_valid_pre_bash_hook():
    descriptor = json.loads((_HOOK_DIR / "local-path-in-gh.pre-bash.json").read_text())
    assert descriptor["id"] == "local-path-in-gh"
    assert descriptor["point"] == "pre-bash"
    assert descriptor["cmd"] == "/ABSOLUTE/PATH/TO/agent-hooks/local-path-in-gh/local_path_in_gh.py"
    assert descriptor["on_error"] in {"open", "closed"}
    assert isinstance(descriptor["timeout_ms"], int) and descriptor["priority"] >= 0
    assert os.access(_HOOK, os.X_OK)


# ── hatch: deny-by-default one-time Telegram approval ────────────────────────────────────────

def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)
_JUSTIFICATION = "Posting a verbatim path in a bug report about path handling."


def test_hatch_env_var_is_the_documented_name():
    assert lpg.hatch_escalation.hatch_env_var("local-path-in-gh") == HATCH_VAR


def test_hatch_approved_lets_the_command_through(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(lpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    result, code = _run(
        "gh pr comment 1 --body 'path /tmp/x'", monkeypatch, tmp_path, {HATCH_VAR: _JUSTIFICATION}
    )
    assert code == 0 and result["decision"] == "allow"
    assert marker.exists()
    assert "hatch escalation" in result["message"].lower()


def test_hatch_inline_prefix_form_is_found(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(lpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = f'{HATCH_VAR}="{_JUSTIFICATION}" gh pr comment 1 --body "path /tmp/x"'
    result, code = _run(command, monkeypatch, tmp_path)
    assert code == 0 and result["decision"] == "allow" and marker.exists()


def test_hatch_denied_blocks_and_says_so(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(lpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    result, code = _run(
        "gh pr comment 1 --body 'path /tmp/x'", monkeypatch, tmp_path, {HATCH_VAR: _JUSTIFICATION}
    )
    assert code == lpg.BLOCK_EXIT_CODE and result["decision"] == "block"
    assert "hatch escalation denied" in result["message"].lower()
    assert "TRIGGER:" in result["message"]


@pytest.mark.parametrize("value", ["1", "true", "yes", ""])
def test_bare_hatch_value_is_rejected_without_telegram(value, tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"echo x >> {calls}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(lpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    result, code = _run(
        "gh pr comment 1 --body 'path /tmp/x'", monkeypatch, tmp_path, {HATCH_VAR: value}
    )
    assert code == lpg.BLOCK_EXIT_CODE and result["decision"] == "block"
    assert not calls.exists()


def test_no_other_env_var_or_flag_bypasses(tmp_path, monkeypatch):
    env = {
        "LOCAL_PATH_IN_GH": "0",
        "RIG_LOCAL_PATH_IN_GH": "off",
        "SKIP_LOCAL_PATH_IN_GH": "1",
        "RIG_HATCH_ALL": "1",
    }
    _, code = _run("gh pr comment 1 --body 'path /tmp/x'", monkeypatch, tmp_path, env)
    assert code == lpg.BLOCK_EXIT_CODE


def test_one_hatch_round_trip_covers_a_chain_with_several_offences(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"echo x >> {calls}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(lpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = "gh pr comment 1 --body 'a /tmp/a' && task check 4 1 --proof /tmp/b.png"
    result, code = _run(command, monkeypatch, tmp_path, {HATCH_VAR: _JUSTIFICATION})
    assert code == 0 and result["decision"] == "allow"
    assert calls.read_text().count("x") == 1


def test_clean_command_never_touches_the_hatch(tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"echo x >> {calls}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(lpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    result, code = _run(
        "gh pr comment 1 --body ok", monkeypatch, tmp_path, {HATCH_VAR: _JUSTIFICATION}
    )
    assert code == 0 and result["decision"] == "allow"
    assert not calls.exists()


# ── event shapes ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", ["not json", "null", "5", "[]", '"oops"', "{}", '{"args": 3}'])
def test_malformed_events_fail_open(payload, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert lpg.main() == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_command_is_read_from_the_fallback_fields(tmp_path, monkeypatch):
    for event in (
        {"cwd": str(tmp_path), "args": {"cmd": "gh pr comment 1 --body '/tmp/x'"}},
        {"cwd": str(tmp_path), "command": "gh pr comment 1 --body '/tmp/x'"},
    ):
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        assert lpg.main() == lpg.BLOCK_EXIT_CODE


def test_cat_command_substitution_is_judged_by_the_file_content_not_its_name(tmp_path, monkeypatch):
    clean = tmp_path / "clean.md"
    clean.write_text("## Proof\n\n```text\nPASS\n```\n")
    dirty = tmp_path / "dirty.md"
    dirty.write_text("shot at /tmp/x.png\n")
    _allowed(f'gh pr create --title t --body "$(cat {clean})"', monkeypatch, tmp_path)
    _allowed(f"gh pr comment 1 --body \"$(cat '{clean}')\"", monkeypatch, tmp_path)
    _blocked(f'gh pr create --title t --body "$(cat {dirty})"', monkeypatch, tmp_path)
    _blocked(f"gh pr comment 1 --body \"`cat {dirty}`\"", monkeypatch, tmp_path)


def test_stdin_shapes_are_all_read(tmp_path, monkeypatch):
    dirty = tmp_path / "dirty.md"
    dirty.write_text("see /tmp/x\n")
    # a heredoc on the same command never hides an explicit `< file`
    _blocked(f"gh pr comment 1 --body-file - < {dirty} <<EOF\nclean\nEOF", monkeypatch, tmp_path)
    # a heredoc piped in through cat
    _blocked("cat <<'EOF' | gh pr comment 1 --body-file -\nsee /tmp/proof.png\nEOF", monkeypatch, tmp_path)
    _allowed("cat <<'EOF' | gh pr comment 1 --body-file -\n```text\nPASS\n```\nEOF", monkeypatch, tmp_path)
    # a here-string
    _blocked("gh pr comment 1 --body-file - <<< 'see /tmp/x'", monkeypatch, tmp_path)


def test_gh_api_method_flag_order_does_not_hide_the_endpoint(tmp_path, monkeypatch):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"body": "see /tmp/x"}))
    _blocked(f"gh api -X POST repos/o/r/issues/1/comments --input {payload}", monkeypatch, tmp_path)
    _blocked(f"gh api repos/o/r/issues/1/comments --input {payload} -X POST", monkeypatch, tmp_path)


def test_task_positionals_and_equals_form_global_flags(tmp_path, monkeypatch):
    _blocked("task new 'Ticket title: evidence at /tmp/proof.png'", monkeypatch, tmp_path)
    _allowed("task new 'Plain title'", monkeypatch, tmp_path)
    # the ticket id positional of `task comment` is not comment text
    _allowed("task comment /tmp/123 'clean comment'", monkeypatch, tmp_path)
    _blocked("task --config=cfg.yaml check 4 --proof /tmp/evidence.png", monkeypatch, tmp_path)
    _blocked("task --repo=o/r check 4 1 --proof=/tmp/evidence.png", monkeypatch, tmp_path)


def test_equals_form_short_flags_and_unquoted_html_targets(tmp_path, monkeypatch):
    _blocked("gh pr comment 1 -b=/tmp/x", monkeypatch, tmp_path)
    _blocked("gh pr comment 1 -b/tmp/x", monkeypatch, tmp_path)
    _blocked("gh pr comment 1 --body '<img src=/srv/proofs/check.png>'", monkeypatch, tmp_path)
    _allowed("gh pr comment 1 --body '<img src=https://h.io/a.png>'", monkeypatch, tmp_path)


def test_cd_in_a_chain_moves_where_relative_body_files_are_read(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "body.md").write_text("shot at /tmp/proof.png\n")
    (tmp_path / "body.md").write_text("clean\n")
    _blocked("cd sub && gh pr comment 1 --body-file body.md", monkeypatch, tmp_path)
    _allowed("cd sub && cd .. && gh pr comment 1 --body-file body.md", monkeypatch, tmp_path)


def test_append_redirects_keep_what_the_file_already_holds(tmp_path, monkeypatch):
    (tmp_path / "b.md").write_text("shot at /tmp/proof.png\n")
    _blocked("echo clean >> b.md && gh pr comment 1 --body-file b.md", monkeypatch, tmp_path)
    _allowed("echo clean > b.md && gh pr comment 1 --body-file b.md", monkeypatch, tmp_path)
    _blocked("echo clean | tee -a b.md && gh pr comment 1 --body-file b.md", monkeypatch, tmp_path)


def test_heredoc_fed_to_api_input_or_field_is_scanned(tmp_path, monkeypatch):
    _blocked(
        "gh api repos/o/r/issues/1/comments --input - <<'EOF'\n{\"body\": \"/tmp/proof.png\"}\nEOF",
        monkeypatch, tmp_path,
    )
    _blocked(
        "gh api repos/o/r/issues/1/comments -F body=@- <<'EOF'\nsee /tmp/proof.png\nEOF",
        monkeypatch, tmp_path,
    )
    _allowed(
        "gh api repos/o/r/issues/1/comments -F body=@- <<'EOF'\n```text\nPASS\n```\nEOF",
        monkeypatch, tmp_path,
    )


def test_redirect_inputs_are_not_posted_text(tmp_path, monkeypatch):
    """`cat /tmp/clean.md > body.md`: the operand NAME is an input, the CONTENT is what is posted."""
    clean = tmp_path / "clean.md"
    clean.write_text("```text\nPASS\n```\n")
    dirty = tmp_path / "dirty.md"
    dirty.write_text("shot at /tmp/proof.png\n")
    _allowed(f"cat {clean} > body.md && gh pr comment 1 --body-file body.md", monkeypatch, tmp_path)
    _blocked(f"cat {dirty} > body.md && gh pr comment 1 --body-file body.md", monkeypatch, tmp_path)


def test_process_substitution_body_file_is_judged_by_its_producer(tmp_path, monkeypatch):
    clean = tmp_path / "clean.md"
    clean.write_text("```text\nPASS\n```\n")
    _blocked("gh pr comment 1 --body-file <(printf 'see /tmp/proof.png')", monkeypatch, tmp_path)
    _allowed(f"gh pr comment 1 --body-file <(cat {clean})", monkeypatch, tmp_path)
    _allowed("gh pr comment 1 --body-file <(printf 'all good')", monkeypatch, tmp_path)


def test_substituted_and_grouped_commands_are_judged(tmp_path, monkeypatch):
    _blocked("echo $(gh pr comment 1 --body '/tmp/proof.png')", monkeypatch, tmp_path)
    _blocked("echo `gh pr comment 1 --body '/tmp/proof.png'`", monkeypatch, tmp_path)
    _blocked("( gh pr comment 1 --body '/tmp/proof.png' )", monkeypatch, tmp_path)
    _blocked("{ gh pr comment 1 --body '/tmp/proof.png'; }", monkeypatch, tmp_path)
    _allowed("echo $(gh pr comment 1 --body ok) && ls /tmp", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "tg 'see /tmp/x'",
        "tg --tag report 'see /tmp/x'",
        "tg report --title t --ticket 4 --impact 'log in ~/out.log' --verify-cmd c --verify-result r",
        "tg report --title t --ticket 4 --impact i --verify-cmd c --verify-result r --evidence 'file:///x'",
        "echo 'see /var/tmp/x' | tg -",
    ],
)
def test_tg_text_with_local_path_blocks(command, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_task_body_file_resolves_against_task_cwd_flag(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / "body.md").write_text("proof at /tmp/proof.png")
    work = tmp_path / "work"
    work.mkdir()
    (work / "body.md").write_text("clean text")
    _blocked(f"task -C {other} comment 4 --body-file body.md", monkeypatch, work)
    _blocked(f"task --cwd={other} comment 4 --body-file body.md", monkeypatch, work)


@pytest.mark.parametrize(
    "command",
    [
        "timeout 30 gh pr comment 1 --body 'see /tmp/proof.png'",
        "gtimeout --signal TERM 30 gh pr comment 1 --body 'see /tmp/proof.png'",
        "timeout --signal=TERM 30 gh pr comment 1 --body 'see /tmp/proof.png'",
        "timeout --kill-after=2 30 gh pr comment 1 --body 'see /tmp/proof.png'",
        "timeout -sTERM 30 gh pr comment 1 --body 'see /tmp/proof.png'",
        "timeout --preserve-status 30s task comment 4 'see ~/o.log'",
    ],
)
def test_timeout_wrapped_writes_are_still_scanned(command, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_timeout_wrapped_clean_write_is_allowed(tmp_path, monkeypatch):
    _allowed("timeout 30 gh pr comment 1 --body 'clean'", monkeypatch, tmp_path)
