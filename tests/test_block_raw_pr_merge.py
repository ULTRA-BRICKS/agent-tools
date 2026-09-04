"""Tests for agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py.

Covers:
  - True positives: `gh pr merge <N>` is blocked
  - The argv-parse FP fix: commands whose body/args contain "gh pr merge" as TEXT
    (e.g. `gh pr create --body "...gh pr merge..."`) are ALLOWED
  - Sanctioned paths: `gh ship`, `pr-ship.sh`, `ship.sh` are allowed
  - Shell chains: `gh pr merge` behind `&&`/`;`/`||` is caught
  - Inline env assignments before `gh` are stripped
  - The DEAD self-service escape hatch (`ALLOW_RAW_PR_MERGE` / `# no-ship-guard:` no longer
    allow) and the external Telegram hatch (`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE`)
  - Fail-closed: unbalanced quotes and malformed event both block

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_block_raw_pr_merge.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-raw-pr-merge"
    / "block_raw_pr_merge.py"
)
_spec = importlib.util.spec_from_file_location("block_raw_pr_merge", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run(command: str, monkeypatch, env: dict | None = None, cwd=None) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`.  Returns (stdout, stderr, exit)."""
    event = {"args": {"command": command}}
    if cwd is not None:
        event["cwd"] = str(cwd)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear escape-hatch env so ambient values don't leak into tests.
    for k in ("ALLOW_RAW_PR_MERGE", "ALLOW_RAW_PR_MERGE_REASON",
              "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


# ── True positives — should BLOCK ──────────────────────────────────────────────────────────

def test_block_basic_gh_pr_merge(monkeypatch):
    out, _err, code = _run("gh pr merge 123", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_with_squash(monkeypatch):
    out, _err, code = _run("gh pr merge 5 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_admin(monkeypatch):
    out, _err, code = _run("gh pr merge 42 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_with_leading_env(monkeypatch):
    """A VAR=value prefix before `gh` must not prevent detection."""
    out, _err, code = _run("GH_TOKEN=secret gh pr merge 7 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_and(monkeypatch):
    """A `gh pr merge` after `&&` in a chain must still be blocked."""
    out, _err, code = _run("echo done && gh pr merge 8 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_semicolon(monkeypatch):
    out, _err, code = _run("echo done; gh pr merge 9", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_gh_pr_merge_in_shell_chain_or(monkeypatch):
    out, _err, code = _run("false || gh pr merge 10", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── The FP fix — commands with "gh pr merge" as text must be ALLOWED ───────────────────────

def test_allow_gh_pr_create_with_merge_in_body(monkeypatch):
    """THE KEY BUG FIX: gh pr create whose --body contains 'gh pr merge' must NOT be blocked."""
    cmd = 'gh pr create --title "My PR" --body "After review, run gh pr merge 5 to land it"'
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_comment_with_merge_in_body(monkeypatch):
    """gh pr comment whose body text includes 'gh pr merge' must be allowed."""
    cmd = "gh pr comment 5 --body 'use gh pr merge to land this'"
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_echo_with_merge_text(monkeypatch):
    """An echo command printing 'gh pr merge' must not trigger the block."""
    out, _err, code = _run('echo "remember to gh pr merge 123"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_for_merge_string(monkeypatch):
    """grep searching for 'gh pr merge' in a file must not be blocked."""
    out, _err, code = _run("grep -r 'gh pr merge' .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Sanctioned paths must be ALLOWED ───────────────────────────────────────────────────────

def test_allow_gh_ship(monkeypatch):
    out, _err, code = _run("gh ship 123", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_view(monkeypatch):
    out, _err, code = _run("gh pr view 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_create(monkeypatch):
    out, _err, code = _run("gh pr create --title 'feat: add X'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_list(monkeypatch):
    out, _err, code = _run("gh pr list", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_gh_pr_checkout(monkeypatch):
    out, _err, code = _run("gh pr checkout 7", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_ship_sh(monkeypatch):
    out, _err, code = _run("./ship.sh 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_pr_ship_sh(monkeypatch):
    out, _err, code = _run("./pr-ship.sh 5", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── REGRESSION: the OLD self-service escape hatch is DEAD ────────────────────────────────────

def test_env_override_no_longer_allows(monkeypatch):
    """REGRESSION: `ALLOW_RAW_PR_MERGE=1` (+ `_REASON`) used to allow a raw merge — it must NO
    LONGER; the merge still BLOCKs."""
    out, _err, code = _run(
        "gh pr merge 5 --squash",
        monkeypatch,
        {"ALLOW_RAW_PR_MERGE": "1", "ALLOW_RAW_PR_MERGE_REASON": "CI provider outage"},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_inline_sentinel_no_longer_allows(monkeypatch):
    """REGRESSION: an inline `# no-ship-guard: <reason>` sentinel no longer allows — still BLOCK."""
    out, _err, code = _run(
        "gh pr merge 5 --admin  # no-ship-guard: hotfix during provider outage",
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── external Telegram hatch escalation (replaces the OLD self-service escape hatch) ──────────
_HATCH_ENV = "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"


def test_hatch_unset_blocks_with_howto(monkeypatch):
    out, _err, code = _run("gh pr merge 5", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert _HATCH_ENV in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` is an invalid request: deny WITHOUT contacting Telegram (the fake would touch a
    marker — it must not exist)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("gh pr merge 5", monkeypatch, {_HATCH_ENV: "1"}, cwd=tmp_path)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n" + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 5", monkeypatch,
        {_HATCH_ENV: "Ship gate is down; manual verify done, hotfix must land."}, cwd=tmp_path,
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()


def test_hatch_justification_exit1_blocks_citing_denied(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "gh pr merge 5", monkeypatch,
        {_HATCH_ENV: "Ship gate is down; manual verify done, hotfix must land."}, cwd=tmp_path,
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── Path-qualified and subshell forms — should BLOCK ───────────────────────────────────────

def test_block_path_qualified_gh(monkeypatch):
    """/opt/homebrew/bin/gh pr merge 5 must still be blocked (basename check)."""
    out, _err, code = _run("/opt/homebrew/bin/gh pr merge 5 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_subshell_gh_pr_merge(monkeypatch):
    """( gh pr merge 5 ) — subshell grouping must not bypass detection."""
    out, _err, code = _run("( gh pr merge 5 )", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_brace_group_gh_pr_merge(monkeypatch):
    """{ gh pr merge 5; } — brace grouping must not bypass detection."""
    out, _err, code = _run("{ gh pr merge 5; }", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Fail-closed paths ──────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_merge_blocks(monkeypatch):
    """Unbalanced quote on a command containing a merge pattern → fail closed (block)."""
    command = "gh pr merge 5 --body 'unclosed"
    with pytest.raises(ValueError):
        hook._split_segments(command)
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_merge_after_inline_env_blocks(monkeypatch):
    """The unparseable fallback resolves `gh` after a leading inline environment assignment."""
    command = "FOO=1 gh pr merge 5 --body 'unclosed"
    with pytest.raises(ValueError):
        hook._split_segments(command)
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_allows(monkeypatch):
    """Unbalanced quote on an unrelated command → allow (not a merge attempt)."""
    out, _err, code = _run("grep won't file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unbalanced_non_gh_report_mentioning_merge_tools_allows(monkeypatch):
    """An unparseable non-gh report that merely mentions merge tooling must still allow."""
    command = 'tg "text mentioning gh ship and block-raw-pr-merge and commit abc123'
    with pytest.raises(ValueError):
        hook._split_segments(command)
    assert hook._MERGE_HINT.search(command)
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_malformed_event_blocks(monkeypatch):
    """A JSON parse error on the event → fail closed."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_empty_command_allows(monkeypatch):
    """An empty command string has no segments → nothing to block."""
    out, _err, code = _run("", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── `\`-newline line continuation must not hide the merge (Codex review on #231) ────────────


def test_line_continuation_does_not_hide_merge():
    """shlex (whitespace_split=False) emits a standalone `\\n` token for a `\\`-newline line
    continuation. After the leading `VAR=` assignment is stripped that token would become
    argv[0], so the merge detector saw `argv[0] == '\\n'` and reported NO merge → the raw merge was
    ALLOWED with no approval. Detection must survive the continuation."""
    command = 'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="x" \\\n  gh pr merge 123 --admin'
    assert hook._command_contains_gh_pr_merge(command) is True


def test_hatch_inline_line_continuation_reaches_tg_ctl(tmp_path, monkeypatch):
    """End-to-end for the exact README inline-hatch shape (`VAR="why" \\`+newline+`gh pr merge`):
    it must be DETECTED as a raw merge AND route the inline justification to tg-ctl (env var NOT
    exported). If the `\\`-newline still hid the merge, the hook would `emit("allow")` WITHOUT
    ever calling the mocked tg-ctl, so the question file would not be written."""
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl", f'cat > "{question}"\n' + _ALLOW_REPLY_SH
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="ship gate down, manual verify done" \\\n'
        '  gh pr merge 123 --admin'
    )
    out, _err, code = _run(command, monkeypatch, cwd=tmp_path)  # env NOT set — inline only
    assert code == 0 and _decision(out) == "allow"
    assert "ship gate down, manual verify done" in question.read_text()


# ── bare-newline command separator: a merge on the 2nd line must not evade detection ────────


def test_bare_newline_second_line_merge_is_detected(monkeypatch):
    """A BARE newline separates commands in shell, so `echo ok`+newline+`gh pr merge 1` is a real
    raw merge on the second line and must BLOCK. shlex consumes a bare newline as whitespace, so
    without normalizing it to a `;` separator the two lines collapse into one `echo`-headed
    segment and the merge evades the gate (Codex review on #231)."""
    out, _err, code = _run("echo ok\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_bare_newline_after_env_assignment_merge_is_detected(monkeypatch):
    """`cd repo`+newline+`gh pr merge` — the merge on the second line is still detected."""
    out, _err, code = _run("cd repo\ngh pr merge 7 --squash", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_newline_inside_quoted_message_is_not_a_merge(monkeypatch):
    """A newline INSIDE a quoted argument must NOT be treated as a command separator — a two-line
    commit message merely mentioning a merge is not a `gh pr merge` invocation and must ALLOW."""
    out, _err, code = _run('git commit -m "line one\ngh pr merge in prose"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_comment_on_first_line_does_not_swallow_second_line_merge(monkeypatch):
    """A `#` comment runs only to the END OF ITS LINE. `echo ok # comment`+newline+`gh pr merge`
    must still BLOCK the merge on the second line. A naive `newline → ;` normalization that ignores
    comments regresses here: shlex then treats `#` as a comment to end-of-INPUT and swallows the
    merge, ALLOWING it (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run("echo ok # comment\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_comment_containing_a_merge_on_one_line_is_not_a_merge(monkeypatch):
    """A `#` comment that merely MENTIONS a merge on a single line is not a real invocation and must
    ALLOW — the comment (incl. any `;`/`gh pr merge` text inside it) is stripped to end of line."""
    out, _err, code = _run("echo done # then gh pr merge 5 --admin", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "g\\\nh pr merge 1 --admin",  # continuation INSIDE `gh`
        "gh p\\\nr merge 1 --admin",  # continuation INSIDE `pr`
        "gh pr m\\\nerge 1 --admin",  # continuation INSIDE `merge`
    ],
)
def test_line_continuation_inside_the_command_word_is_detected(command, monkeypatch):
    """A `\\`-newline is REMOVED by the shell (the parts join with NO space), so `g\\`+newline+`h pr
    merge` executes as `gh pr merge`. Normalizing the continuation to a SPACE instead would split it
    into `g h pr merge` (argv[0] == 'g') and ALLOW the raw merge — the continuation must be removed,
    not spaced (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_quote_outside_quotes_does_not_spoof_quote_state(monkeypatch):
    r"""A backslash-escaped `\"` is a LITERAL `"`, not an opening quote. `echo \"`+newline+`gh pr
    merge 1\"` runs a real merge on the second line; a scanner that ignores the `\` would treat the
    `"` as opening a quote, swallow the newline as quoted content, and ALLOW the merge (Codex review
    on the bare-newline follow-up)."""
    out, _err, code = _run('echo \\"\ngh pr merge 1 \\"', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_quote_inside_double_quotes_does_not_close_the_string(monkeypatch):
    r"""Inside a double-quoted string a `\"` is an escaped literal `"` that does NOT close the
    string. `echo "a \" b"`+newline+`gh pr merge 1`: the string closes at the SECOND real `"`, the
    newline is then unquoted and separates the real merge on line 2 — must BLOCK."""
    out, _err, code = _run('echo "a \\" b"\ngh pr merge 1 --admin', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo foo#bar && gh pr merge 1 --admin",  # mid-word `#`, same-line `&&` merge
        "echo foo#bar\ngh pr merge 1 --admin",  # mid-word `#`, second-line merge
        "echo http://x/#frag\ngh pr merge 1 --admin",  # `#` fragment in a URL, second-line merge
    ],
)
def test_midword_hash_is_not_a_comment_and_does_not_hide_merge(command, monkeypatch):
    """A `#` that is NOT at a word boundary (`foo#bar`, a URL `#frag`) is LITERAL in a real shell,
    not a comment. shlex's default commenter would truncate the whole line at that `#` and drop a
    following `gh pr merge`, allowing it. Must BLOCK (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_escaped_space_before_hash_does_not_start_a_comment(monkeypatch):
    r"""An ESCAPED space (`foo\ `) is a literal space still INSIDE the word, so a following `#` is
    NOT a comment boundary. `echo foo\ # x ; gh pr merge 1` runs the merge in a real shell; a
    scanner keying comment-start off the last emitted char (a space) would wrongly treat `#` as a
    comment, drop the rest, and ALLOW the merge (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run("echo foo\\ # x ; gh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\ngh pr merge 1 --admin\nEOF",  # executable-position merge in body
        "cat <<'EOF'\ngh pr merge 1\nEOF",  # quoted delimiter
        "git commit -F - <<-EOF\n\tgh pr merge 1 --admin\n\tEOF",  # `<<-` tab-stripped
    ],
)
def test_heredoc_body_with_executable_merge_is_over_blocked(command, monkeypatch):
    """DEFENSE-IN-DEPTH: a heredoc BODY line that is itself a `gh pr merge` invocation at executable
    position is over-blocked. A crafted heredoc can plant a matching terminator AFTER a real merge to
    skip past it, so skipped body lines are still scanned for an executable merge and re-injected.
    Over-blocking a merge at a body line's command position is the SAFE direction (coordinator 3×3
    review found the `(( 0 << merge ))` crafted-delimiter bypass)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\nsee the gh pr merge docs for details\nEOF",  # prose mention (not argv[0])
        "git commit -F - <<EOF\nfix: note about gh pr merge behaviour\nEOF",  # commit-message heredoc
    ],
)
def test_heredoc_body_prose_mention_of_merge_is_allowed(command, monkeypatch):
    """A PROSE mention of `gh pr merge` in a heredoc body — where `gh` is NOT at the command position
    (argv[0]) — is document text and must ALLOW. The defense-in-depth salvage counts only executable
    positions, so a commit-message heredoc that merely talks about a merge is not over-blocked."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_real_merge_before_a_heredoc_is_still_detected(monkeypatch):
    """The heredoc skip must not blind the gate to a REAL merge on the SAME line before the body."""
    out, _err, code = _run("gh pr merge 1 --admin <<EOF\nunrelated body\nEOF", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_real_merge_after_a_heredoc_is_still_detected(monkeypatch):
    """A real merge on a line AFTER the heredoc terminator must still be detected."""
    out, _err, code = _run("cat <<EOF\nbody\nEOF\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_punctuated_heredoc_delimiter_does_not_hide_a_later_merge(monkeypatch):
    """A punctuated delimiter (`EOF-MSG`) must be captured WHOLE. Capturing only `EOF` would never
    match the real `EOF-MSG` terminator, skip to end of input, and hide the real merge on the line
    AFTER the heredoc — the exact bypass this gate prevents (Codex review on the follow-up)."""
    out, _err, code = _run(
        "cat <<EOF-MSG\nbody mentions gh pr merge\nEOF-MSG\ngh pr merge 1 --admin", monkeypatch
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_punctuated_heredoc_delimiter_body_prose_is_skipped(monkeypatch):
    """The whole-word delimiter capture still skips the body of a punctuated-delimiter heredoc: a
    PROSE mention of a merge in the body (not at the command position) must ALLOW."""
    out, _err, code = _run("cat <<EOF-MSG\nplease do a gh pr merge later\nEOF-MSG", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "opener",
    [
        "<<\\EOF",  # escaped delimiter — terminates on `EOF`
        '<<E"OF"',  # mixed quoting within the word — terminates on `EOF`
        "<<'EOF'",  # fully single-quoted
    ],
)
def test_quote_removed_heredoc_delimiter_does_not_hide_a_later_merge(opener, monkeypatch):
    r"""The delimiter word is quote-removed the way the shell does (`\EOF`, `E"OF"`, `'EOF'` all
    terminate on `EOF`). Capturing the raw form would never match the `EOF` terminator, skip the
    body to end of input, and hide the real merge after the heredoc (Codex review on the follow-up)."""
    out, _err, code = _run(f"cat {opener}\nbody\nEOF\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "(( 1 << 2 ))\ngh pr merge 1 --admin",  # arithmetic left-shift, NOT a heredoc opener
        "echo $((1 << 2))\ngh pr merge 1 --admin",  # arithmetic expansion
    ],
)
def test_arithmetic_left_shift_is_not_a_heredoc(command, monkeypatch):
    """`<<` inside `(( … ))` / `$(( … ))` is a left-shift operator, not a heredoc. Mis-reading it as
    a heredoc opener and skipping to a never-found terminator would swallow the real merge on the
    next line and ALLOW it. Must BLOCK (Codex review on the bare-newline follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_crafted_arith_delimiter_planting_matching_terminator_is_blocked(monkeypatch):
    """The coordinator's 3×3-review bypass: `(( 0 << merge ))` makes `_read_heredoc` (if it treated
    the arith `<<` as a heredoc) capture delimiter `merge`, and a planted `merge` terminator line
    AFTER the real merge would let `_skip_heredoc_bodies` swallow it — rc=0 ALLOW. BOTH backstops
    must kill it: arithmetic-depth tracking (the `<<` is not a heredoc) AND the defense-in-depth
    salvage of an executable merge on a skipped body line. Must BLOCK."""
    out, _err, code = _run("(( 0 << merge ))\ngh pr merge 1 --admin\nmerge", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_arithmetic_left_shift_with_no_merge_is_not_blocked(monkeypatch):
    """Arithmetic `$(( x << 2 ))` with no merge anywhere must NOT be blocked — the `<<` is a
    left-shift, and treating it as a heredoc must not manufacture a false positive."""
    out, _err, code = _run("echo $((1 << 2))\necho done", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_real_heredoc_planting_own_delimiter_after_merge_is_salvaged(monkeypatch):
    """Pure defense-in-depth case (arithmetic tracking does NOT apply): a heredoc whose delimiter IS
    `merge` — `cat <<merge` — with a `merge` terminator planted AFTER an executable-position
    `gh pr merge` line. `_skip_heredoc_bodies` finds the terminator and would swallow that line; only
    the salvage of the executable-position merge blocks it (a deliberate, documented over-block of a
    body line that opens with `gh pr merge`, not a claim the body is executed). Must BLOCK."""
    out, _err, code = _run("cat <<merge\ngh pr merge 1 --admin\nmerge", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "git commit -F - <<EOF\nfix: don't auto-merge anything\nEOF",  # apostrophe, no merge
        "git commit -F - <<EOF\ndon't forget to gh pr merge later\nEOF",  # apostrophe + prose mention
    ],
)
def test_commit_message_heredoc_with_apostrophe_is_not_over_blocked(command, monkeypatch):
    """A commit-message heredoc body line with an apostrophe fails shlex parse (`don't`). The salvage
    must count ONLY lines that parse to a genuine executable merge (strict True), never a parse
    failure (None) — else a normal commit message that merely mentions a merge would be falsely
    blocked. Must ALLOW (claude/gemini 3×3 review)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unbalanced_arith_paren_does_not_blind_a_later_real_merge(monkeypatch):
    """An unbalanced / whitespace-split `((` must not leave `arith_depth > 0` polluting the rest of
    the input. `arith_depth` resets at each command boundary (newline), so a later real heredoc is
    still skipped AND a real `gh pr merge` after it is still detected. Must BLOCK."""
    out, _err, code = _run("(( x = 1 ) )\ncat <<EOF\nbody\nEOF\ngh pr merge 1 --admin", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo $(true)\ngh pr merge 1 --admin",  # prior line ends in `)` from command substitution
        "(echo x)\ngh pr merge 1 --admin",  # prior line ends in `)` from a subshell
    ],
)
def test_prior_line_ending_in_paren_does_not_glue_the_separator(command, monkeypatch):
    """The inserted `;` must be a standalone split point. If a prior line ends in `)`, gluing it to
    `;` yields a `);` punctuation run that `_split_segments` does not treat as a separator, keeping
    the merge in the previous segment and ALLOWING it. Must BLOCK (Codex review on the follow-up)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── command substitution: a merge hidden in $(…) / `…` must not evade detection (#248) ───────
#
# A command substitution runs its body as a shell command, EVEN inside double quotes
# (`echo "$(gh pr merge 1)"`). The argv scanner keeps such a body inside a single (quoted or
# `$`-prefixed) token, so argv[0] is never `gh` and the merge sailed past the gate. #248 tracked
# the backtick form; the double-quoted `$(…)` and bare `$(…)` forms were also missed. Single quotes
# suppress substitution, so a single-quoted `$(…)` is inert and must stay ALLOWED.


@pytest.mark.parametrize(
    "command",
    [
        "`gh pr merge 1`",  # backtick command substitution (the #248 headline)
        "`gh pr merge 1 --admin`",
        'echo "$(gh pr merge 1)"',  # $(…) inside double quotes — shell DOES execute it
        'echo "$(gh pr merge 1 --admin)"',
        "$(gh pr merge 1)",  # bare $(…) — also missed by the argv scanner
        "echo `gh pr merge 1`",  # backtick inside another command's args
        'result="$(gh pr merge 1 --squash)"',  # assigned from a double-quoted substitution
        "echo $(echo $(gh pr merge 1))",  # nested substitution
        "diff <(gh pr merge 1) other.txt",  # process substitution also executes its body
        "cat >(gh pr merge 1)",
    ],
)
def test_command_substitution_merge_is_blocked(command, monkeypatch):
    """A `gh pr merge` inside an EXECUTED command substitution (backtick or `$(…)`, incl. inside
    double quotes and nested) is a real raw merge and must BLOCK (#248)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "`date`",  # benign backtick substitution
        'echo "$(git rev-parse HEAD)"',  # benign $(…) inside double quotes
        "files=$(ls -1)",  # benign assignment from a substitution
        "echo '$(gh pr merge 1)'",  # SINGLE-quoted → substitution suppressed, inert
        'tg "mentions gh pr merge in a note"',  # plain double-quoted string arg, no substitution
        "grep -r 'gh pr merge' .",  # single-quoted search pattern
    ],
)
def test_benign_or_inert_substitution_is_allowed(command, monkeypatch):
    """A substitution with no merge, and a SINGLE-quoted `$(…)` (which the shell does NOT expand —
    inert), must be ALLOWED. Over-blocking the inert single-quoted form is avoided; a plain string
    that merely mentions a merge (no substitution) also passes (#248 false-positive guard)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── gh api merge routes: REST + graphql mutation must be caught too (#248) ───────────────────


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/1/merge -X PUT",  # REST merge, PUT
        "gh api repos/o/r/pulls/1/merge --method PUT",
        "gh api -X PUT repos/o/r/pulls/1/merge",  # method before endpoint
        "gh api graphql -f query='mutation { mergePullRequest(input:{}) { clientMutationId } }'",
        "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input:{}) { number } }'",
        "gh -R o/r pr merge 5",  # gh GLOBAL flag before `pr merge` (was evaded)
        "`gh api repos/o/r/pulls/1/merge -X PUT`",  # gh-api merge inside a substitution
        'echo "$(gh api graphql -f query=\'mutation { mergePullRequest }\')"',
    ],
)
def test_gh_api_merge_routes_are_blocked(command, monkeypatch):
    """`gh api …/pulls/<n>/merge` with a write method, and a `gh api graphql` merge mutation
    (mergePullRequest / enablePullRequestAutoMerge), skip the ship gate exactly like `gh pr merge`
    and must BLOCK — including a `gh` global flag before `pr merge`, and inside a substitution
    (#248 acceptance: cover the gh api merge routes)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/1/merge",  # GET on /merge — a merge-STATUS read, no write method
        "gh api repos/o/r/pulls",  # list PRs
        "gh api graphql -f query='query { repository { name } }'",  # a plain graphql read
        'tg "docs mention gh api pulls/1/merge and mergePullRequest"',  # prose in another program
        "echo mergePullRequest",  # a bare token, not a gh invocation
        "git merge main",  # git merge, not gh
    ],
)
def test_gh_api_non_merge_and_prose_is_allowed(command, monkeypatch):
    """A GET on `/merge` (status read), a graphql READ query, and any prose mention of the merge
    routes inside another program's args must be ALLOWED — the block anchors on `gh` at the command
    position (#248 false-positive guard)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_gh_api_merge_with_unbalanced_quote_fails_closed(monkeypatch):
    """A `gh api …/pulls/<n>/merge` write with an unbalanced quote can't be parsed; the merge-hint
    heuristic must recognise the gh-api merge shape (not just `gh pr merge`) and fail CLOSED."""
    out, _err, code = _run("gh api repos/o/r/pulls/1/merge -X PUT --jq 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_gh_api_graphql_merge_with_unbalanced_quote_fails_closed(monkeypatch):
    """An unparseable real GraphQL merge must still block via the merge-hint fallback."""
    command = "gh api graphql -f query='mutation { mergePullRequest(input:{}) }"
    with pytest.raises(ValueError):
        hook._split_segments(command)
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_file_backed_query_is_over_blocked(monkeypatch):
    """A `gh api graphql` whose query is fed from a FILE / stdin cannot be inspected at pre-exec
    time and MAY carry a merge mutation, so it is over-blocked (fail closed). Reading the file to
    refine this is a tracked follow-up; the SAFE direction is to block (#248)."""
    out, _err, code = _run("gh api graphql -F query=@mutation.graphql", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Review round 1 findings (Opus): deeper edge cases ────────────────────────────────────────


def test_escaped_nested_backtick_merge_is_blocked(monkeypatch):
    r"""POSIX: inside `` `…` `` an escaped `` \` `` opens a NESTED command substitution, so
    ``echo `echo \`gh pr merge 1\``` `` really executes the merge. The backtick body must be
    POSIX-unescaped before re-scan or the nested merge is MISSED — the #248 bypass one level deeper
    (Opus review round 1)."""
    out, _err, code = _run("echo `echo \\`gh pr merge 1\\``", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        # A `$`/backtick the shell would EXPAND in the query value can splice in a merge field name
        # the literal scan never sees. All of these are shell-expandable (double-quoted / unquoted /
        # concatenated / substitution) and must fail closed (#268; opus/codex reviews).
        'gh api graphql -f query="$Q"',  # whole query from a double-quoted shell var
        'gh api graphql -f query="${Q}"',  # brace param expansion
        'gh api graphql -f query="$(cat merge.graphql)"',  # command substitution
        'gh api graphql -f query="mutation { $OP(input:{}) }"',  # $ in selection position, dbl-quoted
        'gh api graphql -f query="mutation { a: $OP(input:{}) }"',  # after an ALIAS, dbl-quoted
        'gh api graphql -f query="mutation { $1(input:{}) }"',  # shell positional param
        # The concatenation breakout (codex round 4): the MIDDLE `$OP` is UNQUOTED — the shell expands
        # it to a field name while the single-quoted parts keep the rest literal.
        "gh api graphql -f query='mutation($OP:ID!){ '$OP'(input:{pullRequestId:$OP})"
        "{pullRequest{merged}}}' -F OP=PR_abc",
        # Glued flag spellings (opus/codex round 5): gh accepts `-fkey=val`, `--field=key=val`,
        # `--raw-field=key=val` — the expandable `$Q` must be caught in every form.
        'gh api graphql -fquery="$Q"',
        'gh api graphql --field=query="$Q"',
        'gh api graphql --raw-field=query="$Q"',
        'gh api graphql -f \'query=\'"$Q"',  # `query=` assembled from a quoted prefix + expandable
        # An expandable BACKTICK command substitution in the query value (opus round 6 test gap).
        'gh api graphql -f query="`cat q`"',
        # The expandable-query check must apply at EVERY recursion depth (codex round 6): inside a
        # `$( … )` / backtick substitution and a `bash -c '…'` rescan, not only at the top level.
        'echo $(gh api graphql -f query="$Q")',
        'echo `gh api graphql -f query="$Q"`',
        'bash -c \'gh api graphql -f query="$Q"\'',
    ],
)
def test_graphql_query_with_expandable_dollar_is_blocked(command, monkeypatch):
    """A shell-EXPANDABLE `$`/backtick in a `gh api graphql` `query=` value is fail-closed: the shell
    rewrites it at runtime, so the hook cannot prove it is not a merge. Quoting is read from the raw
    command (shlex has stripped it from the parsed tokens), which is why the concatenation form —
    identical to a literal query once shlex de-quotes it — is still caught (#268)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_detached_field_with_expandable_key_is_blocked(monkeypatch):
    """A detached whole field supplied by expansion may become `query=<merge>` at runtime."""
    out, _err, code = _run("gh api graphql -f $FIELD", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_nested_graphql_detached_field_with_expandable_key_is_blocked(monkeypatch):
    """The detached-field role check also runs inside a shell command string."""
    out, _err, code = _run(r'bash -c "gh api graphql -f \$FIELD"', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_glued_field_with_expandable_key_is_blocked(monkeypatch):
    """A glued whole field supplied by expansion may become `query=<merge>` at runtime."""
    out, _err, code = _run("gh api graphql --raw-field=$FIELD", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_bare_expandable_argument_is_blocked(monkeypatch):
    """A bare expansion may inject arbitrary `gh api graphql` flags at runtime."""
    out, _err, code = _run("gh api graphql $ARGS", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_literal_non_query_field_with_expandable_value_is_allowed(monkeypatch):
    """A literal non-query key remains a harmless GraphQL variable even with an expanded value."""
    out, _err, code = _run(
        "gh api graphql -F id=$NODE_ID -f query='query { node(id: $id) { id } }'", monkeypatch
    )
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "gh api graphql -f query='query { viewer { login } }' "
        '-H "Authorization: Bearer $TOKEN"',
        "gh api graphql -f query='query { viewer { login } }' --jq '$x'",
    ],
)
def test_graphql_recognized_value_flag_with_expandable_value_is_allowed(command, monkeypatch):
    """Recognized value-taking flags consume their expandable value instead of treating it as bare."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "query",
    [
        "mutation { $OP(input:{}) { x } }",  # $OP in selection position — but SINGLE-quoted
        "mutation X{ f(id:$Q) }",  # $Q in an argument-value position — SINGLE-quoted
        "mutation { $1(input:{}) }",  # a literal `$1` — SINGLE-quoted, not a shell positional here
    ],
)
def test_graphql_query_single_quoted_dollar_is_allowed(query, monkeypatch):
    """A SINGLE-quoted `$` in the query value is a LITERAL, not a shell expansion — the shell passes
    the exact text to GitHub. Such a query with an undeclared/mis-placed `$name` is INVALID GraphQL
    that GitHub rejects (no merge happens), and it carries no literal `mergePullRequest`, so the hook
    correctly ALLOWS it. The dangerous counterpart is the double-quoted/unquoted form above (#268)."""
    out, _err, code = _run(f"gh api graphql -f query='{query}'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_expandable_query_in_non_gh_command_is_not_blocked(monkeypatch):
    """The expandable-query check is argv-ANCHORED: a `query=$Q` in a NON-gh command (or a prose
    mention) must NOT be blocked — only a real `gh api graphql` segment triggers it (codex round 5)."""
    out, _err, code = _run('echo graphql query=$Q', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_glued_single_quoted_query_with_variable_is_allowed(monkeypatch):
    """The glued-flag counterpart of the allow-case: `-fquery='…$t…'` is single-quoted (literal), so
    it must ALLOW even though the value carries a GraphQL `$t` (round 5 — glued forms must not
    over-block the legitimate single-quoted mutation)."""
    out, _err, code = _run(
        "gh api graphql -fquery='mutation($t:ID!){resolveReviewThread(input:{threadId:$t})"
        "{thread{isResolved}}}' -F t=x",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_empty_graphql_query_is_blocked(monkeypatch):
    """An empty `query=` value is unreadable/degenerate and stays fail-closed (pins the behaviour so a
    refactor does not silently start allowing it — opus round 5 low)."""
    out, _err, code = _run("gh api graphql -f query=", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_backslash_dollar_in_double_quotes_is_over_blocked(monkeypatch):
    """A `\\$` inside double quotes is a shell LITERAL `$` (not an expansion), but the hook over-blocks
    it (single-quote is the only 'literal' form it recognises). This is the SAFE direction — a
    legitimate `-f query="…\\$t…"` is denied, never a merge let through. Pinned so a future 'support
    `\\$`' change is a conscious one (opus round 8)."""
    out, _err, code = _run(r'gh api graphql -f query="mutation{ f(id:\$t) }"', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_heredoc_stray_quote_does_not_mask_later_expandable_query(monkeypatch):
    """A heredoc body containing an unmatched `'` must NOT leave the single-quote blanker stuck and
    mask a real expandable `query="$Q"` on a LATER line — the expandable scan normalizes (removing
    heredoc bodies) before blanking, so the later merge is still caught (codex round 7)."""
    out, _err, code = _run(
        'cat <<EOF\n\'\nEOF\ngh api graphql -f query="$Q"',
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        # #1 — a wrapper `-c`/`eval` string is a NESTED context: the OUTER double quotes already
        # expanded `$Q` before bash saw the (now-literal-looking) single quotes. The old hook blocked
        # any `$` in a nested query; the quote-aware allowance must NOT reopen this (opus/codex).
        'bash -c "gh api graphql -f query=\'$Q\'"',
        'eval "gh api graphql -f query=\'$Q\'"',
        'sh -c "gh api graphql -f query=\'$Q\'"',
        # #3 — an UNQUOTED heredoc body EXPANDS `$`, so a `query="$Q"` / `query="$(…)"` there is a
        # real expandable-query merge even though the body is skipped by normalization.
        'bash <<EOF\ngh api graphql -f query="$Q"\nEOF',
        'bash <<EOF\ngh api graphql -f query="$(cat merge.graphql)"\nEOF',
        # #3b — a QUOTED-delimiter heredoc fed to an INTERPRETER: the outer shell keeps the body
        # literal, but the inner `bash`/`sh` expands `$Q` when it executes the line (chatgpt-codex-
        # connector P1 on the PR). An argv-position gh-api query with `$` in ANY heredoc body blocks.
        "bash <<'EOF'\ngh api graphql -f query=\"$Q\"\nEOF",
        "sh <<'EOF'\ngh api graphql -f query=\"$Q\"\nEOF",
    ],
)
def test_nested_context_expandable_query_is_blocked(command, monkeypatch):
    """A shell-expandable graphql `query=` reached through a NESTED context — a wrapper `-c`/`eval`
    string (whose inner quoting the outer shell already processed) or an UNQUOTED heredoc body (which
    expands `$`) — must BLOCK. These were blocked by the pre-#268 'block every `$`' rule; the
    quote-aware single-quote allowance is sound only at the TOP LEVEL, so nested scans stay strict
    (regression caught by the quorum re-review; opus/codex)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        # Alternate GraphQL landing routes must block even single-quoted / parameterized (codex): the
        # merge queue and a direct branch merge are just as much a bypass as mergePullRequest.
        "gh api graphql -f query='mutation($id:ID!){enqueuePullRequest(input:{pullRequestId:$id}){x}}' -F id=x",
        "gh api graphql -f query='mutation{enqueuePullRequest(input:{}){x}}'",
        "gh api graphql -f query='mutation($b:ID!){mergeBranch(input:{repositoryId:$b}){x}}' -F b=x",
        "gh api graphql -f query='mutation{mergeBranch(input:{}){x}}'",
    ],
)
def test_alternate_landing_mutations_are_blocked(command, monkeypatch):
    """`enqueuePullRequest` (merge queue) and `mergeBranch` (direct branch merge) are landing routes
    that skip ship, so the literal scan blocks them regardless of quoting — the quote-aware
    single-quote allowance must not open them (codex review)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_single_quoted_query_inside_quoted_heredoc_message_is_allowed(monkeypatch):
    """A QUOTED-delimiter heredoc body is literal (the shell never expands `$`), so a commit-message
    heredoc that merely MENTIONS a `resolveReviewThread($t)` query must still ALLOW — the strict
    nested rule applies only to EXPANDING (unquoted) heredoc bodies, not literal ones."""
    out, _err, code = _run(
        "git commit -F - <<'MSG'\nfix: resolveReviewThread($t) example in the body\nMSG",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_single_quoted_backtick_in_query_is_allowed(monkeypatch):
    """A backtick INSIDE the single-quoted query value is literal data the shell never runs, so a
    non-merge query that contains one must ALLOW — the mirror of the expandable-backtick block that
    exercises the single-quote-backtick blanking branch (opus round 6)."""
    out, _err, code = _run(
        "gh api graphql -f query='mutation{ addComment(input:{body:\"see `code`\"}){id} }'",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_gh_api_rest_merge_substring_in_field_value_is_not_blocked(monkeypatch):
    """A `pulls/<n>/merge` substring inside a FIELD VALUE (not the endpoint positional) must NOT
    false-block an unrelated `gh api` write — the REST path is matched against the endpoint only
    (Opus review round 1)."""
    out, _err, code = _run("gh api repos/o/r/issues -X POST -f body='see pulls/1/merge'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/1/merge -XPUT",  # glued short form
        "gh api repos/o/r/pulls/1/merge --method=POST",  # glued long form
        "gh api repos/o/r/pulls/1/merge -X=PUT",  # -X=PUT form
        "gh api repos/o/r/pulls/1/merge -X put",  # lowercase, separate
        "gh api repos/o/r/pulls/1/merge --method=post",  # lowercase, glued
    ],
)
def test_gh_api_glued_write_method_forms_are_blocked(command, monkeypatch):
    """The PUT/POST method flag in glued/lowercase forms (`-XPUT`, `--method=POST`, `-X=PUT`,
    `-X put`) must still be recognised as a write on the REST merge endpoint (Opus review — the
    glued/case-insensitive code paths need explicit coverage so a refactor can't silently drop
    them)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "echo ok # $(gh pr merge 1)",  # substitution inside a `#` comment — never executed
        "echo ok # `gh pr merge 1`",  # backtick substitution inside a comment
        "cat <<'EOF'\n$(gh pr merge 1)\nEOF",  # quoted heredoc: body literal, not expanded
        "git commit -F - <<'MSG'\nfix: `gh pr merge` example\nMSG",  # quoted heredoc, backtick prose
    ],
)
def test_substitution_in_comment_or_quoted_heredoc_is_not_executed_and_allowed(command, monkeypatch):
    """A `$(…)` / backtick inside a `#` comment (dropped) or a QUOTED-delimiter heredoc body
    (literal, not expanded) is NOT executed by the shell, so scanning the raw text would over-block
    it. The substitution scan runs on the normalized command (comments dropped, heredoc bodies
    skipped) so these are ALLOWED (codex review round 3)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\n$(gh pr merge 1)\nEOF",  # UNQUOTED heredoc: body IS expanded → merge executes
        "cat <<EOF\n`gh pr merge 1`\nEOF",  # unquoted, backtick substitution
        "cat <<-EOF\n\t$(gh pr merge 1)\nEOF",  # `<<-` tab-stripped, unquoted
    ],
)
def test_substitution_in_unquoted_heredoc_is_executed_and_blocked(command, monkeypatch):
    """An UNQUOTED heredoc (`<<EOF`) EXPANDS its body, so a `$(…)`/backtick there really runs the
    merge and must BLOCK — distinct from a quoted `<<'EOF'` (literal) body (codex review round 4)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/1/merge -X $METHOD",  # method from a shell variable
        "gh api repos/o/r/pulls/1/merge -X ${M}",
        "gh api repos/o/r/pulls/1/merge -X $(echo PUT)",  # method from a substitution
    ],
)
def test_gh_api_rest_merge_with_unprovable_method_fails_closed(command, monkeypatch):
    """On a literal `…/pulls/<n>/merge` endpoint a shell-expanded method (`-X $METHOD`) MAY be PUT —
    the hook can't resolve it at pre-exec time, so it fails closed and BLOCKs, consistent with the
    unprovable-graphql-query posture (codex review round 4)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "env gh pr merge 1",  # wrapper prefix
        "command gh pr merge 1",
        "nohup gh pr merge 1 --admin",
        "sudo -u ci gh pr merge 1",
        "timeout 60 gh pr merge 1",
        "env FOO=bar gh api repos/o/r/pulls/1/merge -X PUT",  # wrapper + gh api merge
    ],
)
def test_wrapper_prefixed_merge_is_blocked(command, monkeypatch):
    """A merge behind a wrapper command (`env`/`command`/`nohup`/`sudo`/`timeout` …) is an
    ACTUALLY-invoked merge and must BLOCK — the detector sees through the wrapper to the wrapped
    `gh` (Opus review round 4)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/env gh pr merge 1",  # path-qualified wrapper
        "/usr/bin/timeout 60 gh pr merge 1",
        "/usr/bin/env FOO=b gh api repos/o/r/pulls/1/merge -X PUT",
    ],
)
def test_path_qualified_wrapper_merge_is_blocked(command, monkeypatch):
    """A PATH-qualified wrapper (`/usr/bin/env gh pr merge`) must be seen through too — the wrapper
    match uses basename, not the literal argv[0] (codex review round 6)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'gh pr merge 1'",  # merge in a shell -c string
        "sh -c \"gh pr merge 1 --admin\"",
        "/bin/bash -c 'gh api repos/o/r/pulls/1/merge -X PUT'",  # gh api merge in -c
        "eval gh pr merge 1",  # eval joins its args and re-parses
        "eval \"gh pr merge 1\"",  # eval of a quoted string
    ],
)
def test_interpreter_string_arg_merge_is_blocked(command, monkeypatch):
    """A merge hidden in a shell interpreter's `-c` string or an `eval` argument is an
    actually-invoked merge and must BLOCK — the string is re-scanned as a command (Opus review
    round 6)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "env bash -c 'gh pr merge 1'",  # wrapper + interpreter composition
        "timeout 60 bash -c 'gh pr merge 1 --admin'",
        "sudo sh -c 'gh api repos/o/r/pulls/1/merge -X PUT'",
        "/usr/bin/env bash -c 'gh pr merge 1'",  # path-qualified wrapper + interpreter
        "env timeout 60 gh pr merge 1",  # two wrappers then gh
        "bash -cx 'gh pr merge 1'",  # combined short option -cx
        "sh -xc 'gh pr merge 1'",  # combined short option -xc
    ],
)
def test_wrapper_interpreter_composition_merge_is_blocked(command, monkeypatch):
    """A merge behind a wrapper + interpreter (`env bash -c '…'`) or a combined `-c` short option
    (`bash -cx`) is still an actually-invoked merge and must BLOCK — the resolver sees through the
    wrapper to the interpreter and re-scans its `-c` string (codex review round 7)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_interpreter_c_string_without_merge_is_allowed(monkeypatch):
    """A shell `-c` string with no merge must still ALLOW — the re-scan anchors on an invoked `gh`."""
    out, _err, code = _run("bash -c 'echo gh pr merge is just text'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_graphql_query_with_variables_is_allowed(monkeypatch):
    """A SINGLE-quoted GraphQL variable (`$owner`) in an inline non-merge `query` value must ALLOW.
    This is the conscious flip of the old 'block any `$`' behaviour (#268): a single-quoted `$name`
    is a literal GraphQL variable the shell never expands, so it cannot change WHICH mutation runs.
    The literal `_MERGE_MUTATION` scan still catches a real merge; a shell-EXPANDABLE `$` (double
    quotes / unquoted / substitution) stays fail-closed (see the expandable-dollar tests above)."""
    out, _err, code = _run(
        "gh api graphql -f query='query($owner:String!){ repository(owner:$owner){ name } }'",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_resolve_review_thread_mutation_with_variables_is_allowed(monkeypatch):
    """#268 core case: the `resolveReviewThread` mutation an agent must run to satisfy `gh ship`'s
    unresolved-threads gate is NOT a merge and must ALLOW, even with a GraphQL `$threadId` variable."""
    out, _err, code = _run(
        "gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t})"
        "{thread{isResolved}}}' -F t=PRRT_abc",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_add_review_thread_reply_mutation_is_allowed(monkeypatch):
    """`addPullRequestReviewThreadReply` (an agent replying to a bot nit before resolving) is not a
    merge and must ALLOW (#268)."""
    out, _err, code = _run(
        "gh api graphql -f query='mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply"
        "(input:{pullRequestReviewThreadId:$t,body:$b}){comment{id}}}' -F t=PRRT_abc -F b=fixed",
        monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_merge_mutation_with_variables_is_still_blocked(monkeypatch):
    """The protection must not be neutered: a real `mergePullRequest` mutation is still BLOCKED even
    when it carries GraphQL variables — the literal token is present, so `_MERGE_MUTATION` fires."""
    out, _err, code = _run(
        "gh api graphql -f query='mutation($id:ID!){mergePullRequest(input:{pullRequestId:$id})"
        "{pullRequest{merged}}}' -F id=PR_abc",
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_wrapper_prefixed_prose_is_not_blocked(monkeypatch):
    """A wrapper whose argument merely MENTIONS a merge (not an invoked `gh`) must still ALLOW —
    the wrapped-`gh` scan matches a token whose basename is `gh`, not a quoted string."""
    out, _err, code = _run('nohup echo "gh pr merge 1"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", ["strace gh pr merge 1", "taskset -c 0 gh pr merge 1"])
def test_additional_exec_wrappers_are_covered(command, monkeypatch):
    """The best-effort wrapper set covers common exec-wrappers (`strace`, `taskset`, …) so
    `strace gh pr merge 1` is still an invoked merge and BLOCKs (codex review round 8)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_command_dash_v_gh_is_not_a_merge(monkeypatch):
    """`command -v gh` (a lookup, `command` is a wrapper + a bare `gh` tail with no subcommand) must
    ALLOW — the resolved `gh` has no `pr merge`/`api` subargs (codex review round 8, FP guard)."""
    out, _err, code = _run("command -v gh", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_prose_mention_under_wrapper_scan_still_allowed(monkeypatch):
    """SENTINEL for the wrapper-scan boundary: a NON-wrapper leading token (`see`) must NOT be
    treated as a wrapper, so `see the gh pr merge docs` (prose) still ALLOWs. Guards against a
    future 'treat any leading token as a wrapper' change that would re-break prose anchoring."""
    out, _err, code = _run("cat <<EOF\nsee the gh pr merge docs for details\nEOF", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_graphql_inline_read_query_with_merge_token_string_literal_is_allowed(monkeypatch):
    """A `gh api graphql` read whose merge-mutation token is only a STRING LITERAL inside the query
    (`repository(name:"mergePullRequest")`) executes nothing and is now ALLOWED.

    This REPLACES the former deliberate whole-call over-block: per the CTO 2026-07-18 order to
    block merges more precisely, `_graphql_carries_merge_mutation` strips GraphQL string literals
    from the readable `query` value before scanning, so a read-only query that merely NAMES the
    mutation is no longer false-blocked. The safe-direction guarantees are preserved by the sibling
    tests: a real `mergePullRequest(` CALL, a token OUTSIDE a readable query value, and an
    unprovable/expandable query all still BLOCK."""
    out, _err, code = _run(
        "gh api graphql -f query='query { repository(name:\"mergePullRequest\") { id } }'", monkeypatch
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_unparseable_non_gh_prose_mentioning_gh_api_merge_token_is_allowed(monkeypatch):
    """An unparseable non-gh command mentioning a merge token remains argv-anchored (#289)."""
    out, _err, code = _run("echo gh docs on mergePullRequest don't forget", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unparseable_prose_without_merge_token_is_allowed(monkeypatch):
    """The mirror of the above: an unparseable command with NO merge token must still ALLOW — the
    fail-closed hint only fires on a merge-like shape (regression guard for the widened hint)."""
    out, _err, code = _run("grep won't file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_comment_with_paren_inside_substitution_does_not_hide_merge(monkeypatch):
    """A `)` inside a `#` comment WITHIN `$( … )` must not close the substitution early: the real
    `)` is after the merge, which the shell executes. `echo "$(echo ok # )`+newline+`gh pr merge
    1`+newline+`)"` must BLOCK — the paren reader is comment-aware (codex review round 5)."""
    out, _err, code = _run('echo "$(echo ok # )\ngh pr merge 1\n)"', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api -F query=@merge.graphql graphql",  # file-backed query, flag BEFORE endpoint
        "gh api graphql -F query=@merge.graphql",  # flag after endpoint
        "gh api --input @body.json graphql",  # input body, flag before endpoint
    ],
)
def test_gh_api_graphql_filebacked_query_with_flags_before_endpoint_is_blocked(command, monkeypatch):
    """A `gh api graphql` file-backed query must be detected regardless of flag/endpoint order.
    `-F query=@merge.graphql` must NOT fragment on `@` (which would make `_gh_api_endpoint` read
    `@` as the endpoint and miss the graphql merge) — fail closed (codex review round 5)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/1/merge --method=$METHOD",  # glued long form, shell var
        "gh api repos/o/r/pulls/1/merge -X=$METHOD",  # glued -X= form
        "gh api repos/o/r/pulls/1/merge --method=$(echo PUT)",  # glued, substitution
    ],
)
def test_gh_api_rest_merge_with_glued_unprovable_method_fails_closed(command, monkeypatch):
    """A GLUED shell-expanded method on a literal merge endpoint (`--method=$METHOD`, `-X=$METHOD`)
    must fail closed and BLOCK — the `$` must not fragment the token into an empty method value that
    slips through (coordinator ship review P1)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/1/merge --method=`echo PUT`",  # glued long backtick method
        "gh api repos/o/r/pulls/1/merge -X=`echo PUT`",  # glued -X= backtick method
    ],
)
def test_gh_api_rest_merge_with_glued_backtick_method_fails_closed(command, monkeypatch):
    """A GLUED backtick method value (`--method=\\`echo PUT\\``) fragments to an EMPTY `--method=`
    token; on a merge endpoint that empty/unprovable method must fail closed and BLOCK, not slip
    through (codex ship review round 2)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unquoted_heredoc_unparseable_substitution_fails_closed(monkeypatch):
    """An unquoted heredoc body whose `$( … )` is merge-like but UNPARSEABLE (an unbalanced quote
    inside) must fail CLOSED and BLOCK — matching the top-level contract, not fail open (Opus ship
    review round 2)."""
    out, _err, code = _run('cat <<EOF\n$(gh pr merge 1 "x)\nEOF', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\n# $(gh pr merge 1)\nEOF",  # `#` is LITERAL data in an unquoted heredoc body
        "cat <<EOF\n'$(gh pr merge 1)'\nEOF",  # `'` is LITERAL data; $() still expands
    ],
)
def test_unquoted_heredoc_literal_hash_or_quote_still_expands_substitution(command, monkeypatch):
    """In an UNQUOTED heredoc body a `#` and `'` are literal DATA while `$( … )` STILL EXPANDS and
    executes — so `# $(gh pr merge 1)` / `'$(gh pr merge 1)'` run the merge and must BLOCK. The body
    scan must not comment-strip or single-quote-inert an unquoted heredoc body (coordinator ship
    review P1; mirrors the confirmed asymmetry where bare `$(gh pr merge 1)` already blocks)."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unparseable_substitution_body_with_parseable_top_level_fails_closed(monkeypatch):
    """When the TOP-LEVEL command parses but a command-substitution BODY is merge-like yet
    unparseable (an unbalanced quote), the fail-closed `return sub` (None) branch must BLOCK — the
    only new control-flow path without direct coverage (Opus review round 2)."""
    out, _err, code = _run("echo \"$(gh pr merge 1 --jq 'unclosed)\"", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Precise merge-mutation detection: a merge TOKEN inside a read-only GraphQL query's STRING
#    LITERAL is data, not a mutation. The blunt `mergePullRequest`-anywhere over-block wrongly
#    denied read-only queries that merely NAME the mutation (e.g. a code/PR search). Block only an
#    actual merge mutation FIELD; keep every real merge and every unprovable form blocked. ──


def test_readonly_graphql_search_for_merge_token_string_literal_allowed(monkeypatch):
    """A read-only `search(query: "mergePullRequest")` names the mutation as a STRING LITERAL — it
    executes nothing. The guard must ALLOW it (the exact class the blunt token scan false-blocked)."""
    command = (
        "gh api graphql -f query='query{ search(query:\"mergePullRequest\" type:ISSUE "
        "first:5){ nodes{ ... on PullRequest{ title } } } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_readonly_graphql_block_string_mentioning_merge_token_allowed(monkeypatch):
    """A merge token inside a GraphQL block string (`\"\"\"…\"\"\"`) is documentation, not a call."""
    command = (
        'gh api graphql -f query=\'query{ """runs enablePullRequestAutoMerge later""" '
        "viewer{ login } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_readonly_graphql_field_named_like_merge_allowed(monkeypatch):
    """Reading merge-adjacent SCHEMA fields (`mergeable`, `autoMergeAllowed`) is read-only."""
    command = (
        "gh api graphql -f query='query{ repository(owner:\"o\" name:\"r\"){ "
        "autoMergeAllowed pullRequest(number:1){ mergeable mergeStateStatus } } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_graphql_real_merge_mutation_still_blocked(monkeypatch):
    """A real `mergePullRequest(` CALL in the query value must still BLOCK (regression guard)."""
    command = (
        "gh api graphql -f query='mutation{ mergePullRequest(input:{pullRequestId:\"x\"})"
        "{ clientMutationId } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "mutation_field",
    [
        "enablePullRequestAutoMerge(input:{pullRequestId:\"x\"}){ clientMutationId }",
        "enqueuePullRequest(input:{pullRequestId:\"x\"}){ clientMutationId }",
        "mergeBranch(input:{repositoryId:\"x\" base:\"main\" head:\"f\"}){ clientMutationId }",
    ],
)
def test_graphql_non_merge_pull_request_landing_mutations_still_blocked(mutation_field, monkeypatch):
    """The precise scan must still block EVERY landing route, not just `mergePullRequest`: an
    auto-merge enable, a merge-queue enqueue, and a direct branch merge are all real merges. This
    locks in that the swap from the blunt whole-call scan to `_graphql_carries_merge_mutation` kept
    the full `_MERGE_MUTATION` token set (findings: review round for this change)."""
    command = f"gh api graphql -f query='mutation{{ {mutation_field} }}'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_escaped_block_string_delimiter_honored_readonly_allowed(monkeypatch):
    """A merge token inside a well-formed block string that uses GraphQL's `\\\"\"\"` escape is data
    in a read-only query — the stripper honours the escape (the `\\\"\"\"` does not terminate the
    span), so the whole literal is removed and the call is ALLOWED."""
    command = 'gh api graphql -f query=\'query{ note(x:"""a \\""" mergePullRequest \\""" b""") viewer{ login } }\''
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "landing_field",
    ["mergePullRequest", "enablePullRequestAutoMerge", "enqueuePullRequest", "mergeBranch"],
)
def test_graphql_two_escaped_block_strings_cannot_straddle_a_merge_call(landing_field, monkeypatch):
    """Two `\\\"\"\"` escaped block-string delimiters must NOT re-pair across a real merge call and
    strip it. Correctly honouring the escape keeps each block string self-contained, so the
    executing `<merge>(input:…)` between them stays visible and BLOCKS (this was a real bypass: naive
    `find` re-pairing swallowed the call)."""
    command = (
        f"gh api graphql -f query='mutation{{ setX(a:\"\"\"p\\\"\"\"q\"\"\") "
        f"{landing_field}(input:{{pullRequestId:\"1\"}}){{ id }} setY(b:\"\"\"r\\\"\"\"s\"\"\") }}'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_merge_call_hidden_after_string_literal_still_blocked(monkeypatch):
    """De-stringing must not let a REAL call hide behind a string literal: a query that both quotes
    the token AND performs the mutation still BLOCKS."""
    command = (
        "gh api graphql -f query='mutation{ x: search(query:\"mergePullRequest\"){ n } "
        "mergePullRequest(input:{pullRequestId:\"x\"}){ id } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "comment_delim",
    ['"', '"""'],
)
def test_graphql_comment_quote_cannot_hide_a_merge_mutation(comment_delim, monkeypatch):
    """A GraphQL `#` line comment containing a quote must NOT open a phantom string span that
    swallows a real `mergePullRequest(` call on a LATER line. The stripper skips comments to
    end-of-line, so the executing call between two comment quotes stays visible and BLOCKS (this
    was a real bypass: a cross-line phantom string deleted the call → allow)."""
    command = (
        "gh api graphql -f query='mutation {\n"
        f"  # {comment_delim}\n"
        "  mergePullRequest(input:{pullRequestId:\"x\"}){ id }\n"
        f"  # {comment_delim}\n"
        "}'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_readonly_comment_naming_merge_token_is_allowed(monkeypatch):
    """A read-only query whose `#` comment merely mentions a merge token is allowed — the comment is
    skipped and no executing call remains."""
    command = (
        "gh api graphql -f query='query {\n"
        "  # remember to check mergePullRequest availability\n"
        "  viewer { login }\n"
        "}'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize("terminator", ["\r", "\r\n"])
def test_graphql_cr_terminated_comment_does_not_hide_a_merge_mutation(terminator, monkeypatch):
    """GraphQL's LineTerminator is `\\n`, `\\r`, or `\\r\\n` — a bare `\\r` also ends a `#` comment.
    The stripper previously only scanned for `\\n`, so a comment ending in a lone `\\r` (with a real
    call on the same "line" by `\\n`-only counting) swallowed the executing `mergePullRequest(` call
    as commented-out text — a fail-open bypass, since GitHub's real GraphQL parser treats the `\\r` as
    ending the comment and executes the call. Must BLOCK."""
    command = (
        "gh api graphql -f query='mutation{ #dummy"
        + terminator
        + "mergePullRequest(input:{pullRequestId:\"x\"}){ id } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize("terminator", ["\r", "\r\n"])
def test_graphql_readonly_cr_terminated_comment_naming_merge_token_is_allowed(terminator, monkeypatch):
    """The allow-side mirror of the `\\r`-comment fix: a `#` comment merely NAMING a merge token,
    terminated by `\\r`/`\\r\\n` instead of `\\n`, must still be stripped (not over-strip past it and
    swallow a real field) — the read-only query it precedes is allowed."""
    command = (
        "gh api graphql -f query='query{"
        + terminator
        + "  # remember to check mergePullRequest availability"
        + terminator
        + "  viewer { login }"
        + terminator
        + "}'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_graphql_comment_terminated_by_trailing_cr_at_end_of_value_fails_closed(monkeypatch):
    """A `#` comment where a lone `\\r` is the FINAL character of the value (nothing after it) must
    not crash the scanner and must still leave the preceding, already-visible merge call blocked —
    exercises the `j + 1 == n` bound in the new `\\r`-terminator loop."""
    command = "gh api graphql -f query='mutation{ mergePullRequest(input:{pullRequestId:\"x\"}){ id } } #c\r'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_unterminated_string_in_query_fails_closed(monkeypatch):
    """An UNTERMINATED GraphQL string literal is unprovable — de-stringing must fail closed so a
    merge token after the dangling quote cannot be smuggled past the scan."""
    command = "gh api graphql -f query='query{ x(q:\"open mergePullRequest(input:{}){id} }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_unterminated_block_string_in_query_fails_closed(monkeypatch):
    """An UNTERMINATED block string is a distinct code path from a regular one — it too must fail
    closed so a merge token after the dangling `\"\"\"` stays visible and blocks."""
    command = "gh api graphql -f query='query{ x(q:\"\"\"open enablePullRequestAutoMerge(input:{}){id} }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_merge_token_in_non_query_field_still_blocked(monkeypatch):
    """A merge token outside a readable `query` value keeps the conservative over-block (fail
    closed): the guard cannot prove a stray `mergePullRequest` token is inert data."""
    command = "gh api graphql -F note=mergePullRequest -f query='query{ viewer{ login } }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_glued_query_field_real_mutation_blocked(monkeypatch):
    """The GLUED field spelling (`-fquery=…`, no space) must be scanned like the detached form: a
    real mutation in it blocks."""
    command = "gh api graphql -fquery='mutation{ mergePullRequest(input:{}){ id } }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_graphql_glued_query_field_string_literal_allowed(monkeypatch):
    """The glued field spelling with a read-only query that only NAMES the token is allowed."""
    command = "gh api graphql --field=query='query{ search(query:\"mergePullRequest\"){ nodes{ __typename } } }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_graphql_glued_non_query_field_with_merge_token_over_blocked(monkeypatch):
    """A merge token in a GLUED non-`query` field keeps the conservative over-block (fail closed)."""
    command = "gh api graphql -Fnote=mergePullRequest -f query='query{ viewer{ login } }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Coupling guard: the provisioned `gh api graphql` helper's read-only recipe is NOT blocked. ──


def test_gh_graphql_helper_readonly_recipe_is_allowed(monkeypatch):
    """The `gh-graphql` skill's canonical read-only invocation (single-quoted inline query, `-F`
    variables) must pass the guard — the alias is read-only by design and must never be blocked."""
    command = (
        "gh api graphql -F owner=cli -F name=cli -f query='query($owner:String! $name:String!)"
        "{ repository(owner:$owner name:$name){ pullRequests(first:20 states:OPEN){ "
        "nodes{ number title } } } }'"
    )
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── `ghgql` wrapper coverage: `ghgql` execs `gh api graphql`, invisible to a command-string hook, so
#    the guard maps a `ghgql …` call to that request and blocks a merge through it — no bypass. ──


@pytest.mark.parametrize(
    "command",
    [
        "ghgql --allow-mutation 'mutation{ mergePullRequest(input:{pullRequestId:\"x\"}){ id } }'",
        "ghgql 'mutation{ mergePullRequest(input:{}){ id } }'",
        "ghgql -q @merge.graphql",            # file-backed: unreadable → unprovable → block
        "ghgql -F query=@merge.graphql",      # file-backed query FIELD: unprovable → block
        "ghgql -q -",                          # stdin: unprovable → block
        "ghgql -",                             # bare-dash stdin: unprovable → block
        "ghgql 'query{ viewer{ login } }' -f query='mutation{ mergePullRequest(input:{}){ id } }'",
        "env ghgql --allow-mutation 'mutation{ enqueuePullRequest(input:{}){ id } }'",
        "ghgql -F query='mutation{ enablePullRequestAutoMerge(input:{}){ id } }' 'query{ x }'",
        # Glued `-q`/`--query=` spellings: real ghgql doesn't recognize them as a query source (only
        # detached `-q <v>`/`--query <v>`), so the mapper must NOT read a merge token out of them and
        # allow through — it must treat the glued token as unprovable and fail closed (#303 review).
        "ghgql --query=@merge.graphql",
        "ghgql -qmutation{mergePullRequest(input:{}){id}}",
        # Sticky-ordering regression: a glued query flag must stay unprovable even when a LATER
        # provable-looking query source is present (the naive first draft cleared the "unprovable"
        # marker whenever ANY later query source was seen — a real fail-open bypass).
        "ghgql --query=@merge.graphql 'query{ viewer{ login } }'",
        "ghgql -q 'query{ viewer{ login } }' --query=@merge.graphql",
        # A `query=` value smuggled behind a NON-`-f`/`-F` value flag: real ghgql's `case` statement
        # captures `query=…` on EVERY one of -F/--field/-f/--raw-field/-H/--header/--jq/--hostname/
        # -p/--preview/-t/--template/--cache, not only -f/-F. The mapper previously forwarded these
        # as opaque header/jq/template/cache fields, hiding the actual query from the scan entirely —
        # a real bypass, since `--allow-mutation` disables ghgql's OWN guard and this is the only
        # remaining gate (#303 review, Codex).
        "ghgql --allow-mutation -H query=mutation{mergePullRequest(input:{}){id}}",
        "ghgql --allow-mutation --jq query=@merge.graphql",
        "ghgql --allow-mutation --cache query=@merge.graphql",
        # Isolated detached `-q <mutation>` — direct coverage of the ONE query source real ghgql's
        # `case` statement (and the mapper's first branch) recognizes, without a second/glued query
        # source in the same command masking whether this branch alone actually works.
        "ghgql --allow-mutation -q 'mutation{ mergePullRequest(input:{}){ id } }'",
        # Stdin marker (`-`) smuggled as a `query=` value behind a non-`-f`/`-F` flag: real ghgql
        # resolves ANY captured `query="-"` to stdin uniformly (its check runs after parsing, on
        # the one shared `query` variable), so this reads a piped mutation exactly like `-q -`
        # does — the mapper must canonicalize it to the unprovable `@-` marker here too, not scan
        # it as a harmless one-character literal (Fable review).
        "ghgql --allow-mutation -H query=-",
        # `--input` supplies gh's ENTIRE request body from a file/stdin, uninspected by any
        # `query=` scan — `_graphql_query_is_unprovable`'s existing `--input` check already covers
        # this once the flag reaches `rest` (Codex review: confirms it does for the ghgql route).
        "ghgql --allow-mutation 'query{ viewer{ login } }' --input body.json",
        # Glued native-gh field spelling (`-fquery=…`, no space) — already recognized by
        # `_graphql_query_field_values`'s regex independent of the ghgql-specific mapper branches;
        # locks that this stays true as the mapper evolves (Fable review).
        "ghgql --allow-mutation -fquery=mutation{mergePullRequest(input:{}){id}}",
    ],
)
def test_ghgql_merge_route_is_blocked(command, monkeypatch):
    """Every merge path THROUGH `ghgql` — a forced mutation, a positional mutation, an unreadable
    `@file`/stdin query, a smuggled second `query=` field, behind an `env` wrapper, a glued
    `-q`/`--query=` spelling (even ordered around a provable-looking query) — must block."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "ghgql 'query{ viewer{ login } }'",
        "ghgql -F owner=cli 'query($owner:String!){ repositoryOwner(login:$owner){ id } }'",
        "ghgql 'query{ search(query:\"mergePullRequest\" type:ISSUE){ nodes{ __typename } } }'",
        "ghgql --allow-mutation 'mutation{ addComment(input:{}){ clientMutationId } }'",  # non-merge write
    ],
)
def test_ghgql_readonly_and_non_merge_writes_allowed(command, monkeypatch):
    """A read-only `ghgql` query (incl. one naming a merge token only as a string literal) and a
    NON-merge mutation must pass the guard — `ghgql` is not blanket-blocked, only merges are."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        # #303 P1: `ghgql --allow-mutation "$Q"` where `$Q` expands to a merge mutation at RUNTIME.
        # ghgql's own read-only guard is skipped under `--allow-mutation`, and the query is not a
        # literal at the argv level, so only `_graphql_query_field_is_expandable`-equivalent coverage
        # of the `ghgql` wrapper (not just a direct `gh api graphql` invocation) catches this.
        'Q=\'mutation{ mergePullRequest(input:{pullRequestId:"x"}){ id } }\'; ghgql --allow-mutation "$Q"',
        'Q="mutation{ enablePullRequestAutoMerge(input:{}){ id } }"; ghgql --allow-mutation "$Q"',
        # `-q "$Q"` / `-f query="$Q"` spellings of the same expandable-query gap.
        'Q=\'mutation{ mergePullRequest(input:{}){ id } }\'; ghgql --allow-mutation -q "$Q"',
        'Q=\'mutation{ mergePullRequest(input:{}){ id } }\'; ghgql --allow-mutation -f query="$Q"',
    ],
)
def test_ghgql_expandable_query_is_blocked(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_ghgql_expandable_query_without_allow_mutation_still_blocked(monkeypatch):
    """Even without `--allow-mutation`, a `ghgql "$Q"` is fail-closed the same as a direct `gh api
    graphql -f query="$Q"` — an expandable query can't be proven read-only at pre-exec time."""
    command = 'Q="query{ viewer{ login } }"; ghgql "$Q"'
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_ghgql_single_quoted_query_stays_allowed(monkeypatch):
    """The quote-aware allowance must still hold for `ghgql`: a single-quoted query is the REAL
    quoting (not expandable), so it is scanned literally and allowed like any other read."""
    command = "ghgql --allow-mutation 'mutation{ addComment(input:{}){ id } }'"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Clustered short-flag bypass (a real `gh` short-flag group, e.g. `-iF`/`-iX`) ────────────

@pytest.mark.parametrize(
    "command",
    [
        # A `query` fed from a FILE (`@f`) can't be read at pre-exec time and must fail closed
        # exactly like the detached/glued `-F`/`-f` spelling already does — but only if the
        # clustered flag is recognised as carrying a `query` field at all. Glued value:
        "gh api -iFquery=@merge_payload.graphql graphql",
        "gh api -ifquery=@merge_payload.graphql graphql",
        # Clustered ahead of a DETACHED value flag (`-iF query=...` == `-i -F query=...`).
        "gh api -iF query=@merge_payload.graphql graphql",
        # A `query` fed from a shell VARIABLE is equally unreadable at pre-exec time.
        "gh api -iFquery=$Q graphql",
    ],
)
def test_gh_api_graphql_clustered_short_flag_unprovable_query_is_blocked(command, monkeypatch):
    """An unreadable (file-backed / shell-variable) `query` value must fail closed exactly like
    the detached/glued `-F`/`-f` spelling already does — but a clustered spelling (`-iFquery=...`)
    previously hid the field from `_graphql_query_field_values` entirely (it only recognised a
    token starting with `-f`/`-F`), so the fail-closed check never fired and the call was
    ALLOWED. A merge mutation is a valid body for such a query, so an unrecognised clustered field
    is a real, exploitable bypass of the `gh ship`-only gate — not merely a theoretical gap."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        # An INLINE merge mutation (not file/variable-backed) carried by a clustered `query`
        # field — the primary `_graphql_carries_merge_mutation` path over expanded tokens, and
        # the most direct real exploit shape.
        "gh api -iFquery='mutation{ mergePullRequest(input:{pullRequestId:\"x\"}){ clientMutationId } }' graphql",
        "gh api -iF query='mutation{ mergePullRequest(input:{pullRequestId:\"x\"}){ clientMutationId } }' graphql",
    ],
)
def test_gh_api_graphql_clustered_short_flag_inline_mutation_is_blocked(command, monkeypatch):
    """A real, INLINE (not file/variable-backed) merge mutation carried by a clustered `-iFquery=`
    spelling must be blocked by `_graphql_carries_merge_mutation` over the expanded tokens — the
    most direct shape of this bypass, distinct from the unprovable-query cases above."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api -iXPUT repos/o/r/pulls/1/merge",  # `-i` clustered ahead of a glued write method
        "gh api -iX PUT repos/o/r/pulls/1/merge",  # `-i` clustered ahead of a detached write method
        "gh api -iXPOST repos/o/r/pulls/1/merge",  # glued POST (not just PUT)
        "gh api -iX POST repos/o/r/pulls/1/merge",  # detached POST
    ],
)
def test_gh_api_rest_merge_clustered_write_method_is_blocked(command, monkeypatch):
    """The same clustering must not hide a write method (`-X PUT`) on the REST merge endpoint."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        # A legitimate READ-ONLY clustered query (no merge mutation) must still be allowed.
        "gh api -iFquery='query{ viewer{ login } }' graphql",
        "gh api -iF query='query{ viewer{ login } }' graphql",
        # `-i` clustered with a non-merge REST GET stays allowed too.
        "gh api -iX GET repos/o/r/issues",
    ],
)
def test_gh_api_clustered_short_flag_readonly_is_allowed(command, monkeypatch):
    """A clustered short-flag spelling must not become an over-block for ordinary read-only
    `gh api` usage — only an actual merge-carrying value blocks."""
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize("leading_flag", ["-t", "-q", "-H", "-p", "-X"])
def test_gh_api_value_flag_operand_shaped_like_a_cluster_still_catches_real_merge(
    leading_flag, monkeypatch
):
    """A detached value-taking flag's OPERAND that merely happens to LOOK like a short-flag
    cluster (`-t -iX ...` — `-iX` is `-t`'s template-string value, not `-i`+`-X`) must not be
    expanded as if it were a flag itself: doing so shifts every later token by one position and
    can hide the real endpoint/query from the scanner entirely (a review caught this as a
    fail-open regression in an earlier version of the fix). Parametrized across every detached
    short value flag `gh api` defines (`_API_VALUE_SHORT_CHARS`), since the operand-protection
    guarantee depends on ALL of them being recognized, not just the one case first tested."""
    command = f"gh api {leading_flag} -iX graphql -f query=@merge.graphql"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize("leading_flag", ["-t", "-q", "-H", "-p", "-X"])
def test_gh_api_value_flag_operand_shaped_like_a_cluster_stays_allowed_when_benign(
    leading_flag, monkeypatch
):
    """The symmetric allow-case: a benign detached operand that merely looks like a cluster must
    not become an over-block once it is correctly left untouched as the preceding flag's value."""
    command = f"gh api {leading_flag} -iX .foo repos/o/r/issues"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize("leading_flag", ["--jq", "--template", "--header", "--method"])
def test_gh_api_long_form_value_flag_operand_shaped_like_a_cluster_still_catches_real_merge(
    leading_flag, monkeypatch
):
    """Same guarantee as the short-flag operand-protection tests above, but for the LONG spelling
    of a detached value flag (`--jq`/`--template`/`--header`/`--method` are all in
    `_API_VALUE_FLAGS` alongside their short forms). A review raised this as a possible fail-open
    regression if a long form were ever missing from that set; it is not missing, but the
    guarantee was previously unproven for long spellings — this closes that coverage gap."""
    command = f"gh api {leading_flag} -iX graphql -f query=@merge.graphql"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize("leading_flag", ["--jq", "--template", "--header", "--method"])
def test_gh_api_long_form_value_flag_operand_shaped_like_a_cluster_stays_allowed_when_benign(
    leading_flag, monkeypatch
):
    """The symmetric allow-case for the long-flag operand-protection guarantee."""
    command = f"gh api {leading_flag} -iX .foo repos/o/r/issues"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_gh_api_graphql_multi_boolean_cluster_unprovable_query_is_blocked(monkeypatch):
    """A cluster may carry more than one leading boolean short flag (`gh api` only defines `-i`
    today, but the expansion loop supports repeats of it, e.g. a doubled/typo'd `-ii`); the loop
    that strips leading booleans must still find the trailing value flag regardless of how many
    booleans precede it."""
    command = "gh api -iiFquery=@merge_payload.graphql graphql"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "gh api -iX=PUT repos/o/r/pulls/1/merge",
        "gh api -iX=POST repos/o/r/pulls/1/merge",
    ],
)
def test_gh_api_rest_merge_clustered_write_method_equals_glued_is_blocked(command, monkeypatch):
    """A write method glued to the cluster with `=` (`-iX=PUT`, mirroring the long-flag
    `--method=PUT` spelling `_WRITE_METHOD_EQ` already recognizes) must expand to `-i`, `-X=PUT`
    and still be caught by `_WRITE_METHOD_EQ` over the expanded token."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        # Equals-glued fieldish spelling (`-F=query=<v>` sets the raw field `query` to `<v>`,
        # same as the bare-`=` `_graphql_query_field_values` regex already recognizes for an
        # UNCLUSTERED `-F`/`--field`) behind a leading `-i`. A review raised this as a possible
        # bypass distinct from the plain-glued (`-Fquery=<v>`) spelling already covered above.
        "gh api -iF=query=@merge.graphql graphql",
        "gh api -if=query=@merge.graphql graphql",
    ],
)
def test_gh_api_graphql_clustered_equals_glued_field_unprovable_query_is_blocked(
    command, monkeypatch
):
    """An unreadable (file-backed) `query` value carried by a clustered, equals-glued field
    spelling (`-iF=query=@x`) must fail closed exactly like every other glued spelling."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_gh_api_graphql_clustered_equals_glued_field_readonly_is_allowed(monkeypatch):
    """The symmetric allow-case: a clustered, equals-glued, read-only query must not be
    over-blocked."""
    command = "gh api -iF=query='query{ viewer{ login } }' graphql"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# Every detached value flag `_expand_clustered_gh_api_flags` must protect, DERIVED from the same
# source set the implementation reads (`hook._API_VALUE_FLAGS`, minus the fieldish flags — those
# have their own dedicated `-f`/`-F` coverage above and don't take a bare operand the same way) —
# so the completeness of this guarantee is proven against the actual set the code uses, not a
# hand-copied list that could silently drift from it as `gh api` grows more flags (review).
_NON_FIELDISH_VALUE_FLAGS = sorted(hook._API_VALUE_FLAGS - hook._FIELDISH)


@pytest.mark.parametrize("leading_flag", _NON_FIELDISH_VALUE_FLAGS)
def test_gh_api_every_value_flag_operand_shaped_like_a_cluster_still_catches_real_merge(
    leading_flag, monkeypatch
):
    """Exhaustive version of the short/long-form operand-protection tests above: EVERY detached
    value flag `gh api` defines (per `_API_VALUE_FLAGS`) must treat its own operand as a value, not
    a flag to expand — including `--input`/`--hostname`/`--cache`, which have no short form and
    were not covered by the earlier hand-picked lists. `--input` is the sharp case: a real
    `gh api --input -iF graphql -f query=@merge.graphql` must still be blocked."""
    command = f"gh api {leading_flag} -iX graphql -f query=@merge.graphql"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize("leading_flag", _NON_FIELDISH_VALUE_FLAGS)
def test_gh_api_every_value_flag_operand_shaped_like_a_cluster_stays_allowed_when_benign(
    leading_flag, monkeypatch
):
    """The symmetric allow-case for the exhaustive operand-protection guarantee above."""
    command = f"gh api {leading_flag} -iX .foo repos/o/r/issues"
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Unrecognized short-flag char in a cluster — fails closed (#333 review finding) ──────────
#
# `-z` is not a real `gh api` flag today (neither in `_API_BOOL_SHORT_CHARS` nor
# `_API_VALUE_SHORT_CHARS`), so it stands in for a hypothetical FUTURE boolean short flag this
# hardcoded allowlist has not been updated for yet — exactly the drift scenario the review raised.


@pytest.mark.parametrize(
    "command",
    [
        # Glued value behind the unrecognized leading char.
        "gh api -zFquery=@merge_payload.graphql graphql",
        # Detached value flag behind the unrecognized leading char.
        "gh api -zF query=@merge_payload.graphql graphql",
        # A shell-variable-fed query is equally unreadable.
        "gh api -zFquery=$Q graphql",
        # A real, INLINE merge mutation hidden behind the unrecognized leading char — before the
        # fix this was not merely "unprovable", it was a real, exploitable, silent bypass: the
        # whole token was left unexpanded and treated as an inert boolean flag.
        "gh api -zFquery='mutation{ mergePullRequest(input:{pullRequestId:\"x\"}){ clientMutationId } }' graphql",
        # An unrecognized char clustered ahead of a write method on the REST merge endpoint.
        "gh api -zXPUT repos/o/r/pulls/1/merge",
        "gh api -zX PUT repos/o/r/pulls/1/merge",
    ],
)
def test_gh_api_unrecognized_clustered_short_flag_char_is_blocked(command, monkeypatch):
    """A leading short-flag char that is in NEITHER `_API_BOOL_SHORT_CHARS` NOR
    `_API_VALUE_SHORT_CHARS` must block the call outright, not fall through to "not a merge".

    Before this fix, `_expand_clustered_short_flag` returned None for such a token (it is neither a
    recognized boolean-then-value cluster nor a plain value flag), and `_expand_clustered_gh_api_flags`
    then left it completely UNTOUCHED in the output. Every downstream detector
    (`_gh_api_endpoint`, `_graphql_carries_merge_mutation`, `_graphql_query_field_values`,
    `_rest_has_write_method`) only recognizes a value flag at the START of a token, so an
    unrecognized-prefixed token like `-zFquery=...` reads as an opaque, harmless boolean flag to
    ALL of them — the query field, the write method, everything after the unrecognized char is
    completely invisible. That is precisely the bypass class this whole clustered-flag fix (#333)
    exists to close, reopened the moment `gh` adds a flag this hardcoded set hasn't caught up to.
    The fix (`_gh_api_flags_contain_unprovable_cluster`) makes an unrecognized leading char
    UNPROVABLE rather than silently inert, so the call blocks."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_short_flag_cluster_unprovable_detector_direct():
    """Direct unit coverage of `_short_flag_cluster_is_unprovable`, independent of the full
    CLI-simulation harness — pins the exact boundary the detector draws, since there is no real
    `gh` flag today that exercises the "unrecognized leading char" branch through an actual `gh`
    invocation other than a made-up one."""
    # An unrecognized leading char followed by a recognized value char: genuinely unprovable.
    assert hook._short_flag_cluster_is_unprovable("-zFquery=x") is True
    # An unrecognized leading char with nothing recognizable after it either: still unprovable —
    # we cannot rule out it hides a value flag/query behind more unrecognized structure.
    assert hook._short_flag_cluster_is_unprovable("-zq") is True
    # A recognized boolean run followed by an unrecognized char: also unprovable (case not caught
    # by the pre-fix code, which only checked the FIRST unrecognized position after the leading
    # booleans and likewise fell through to None/pass-through).
    assert hook._short_flag_cluster_is_unprovable("-izquery=x") is True
    # A plain, already-handled direct value flag (no leading boolean at all) is NOT unprovable —
    # it's scanned correctly elsewhere by its own `-[fF]`/`-X`/... prefix check.
    assert hook._short_flag_cluster_is_unprovable("-Ffoo") is False
    assert hook._short_flag_cluster_is_unprovable("-Xvalue") is False
    # A pure recognized-boolean cluster (nothing to hide behind it) is not unprovable.
    assert hook._short_flag_cluster_is_unprovable("-ii") is False
    assert hook._short_flag_cluster_is_unprovable("-i") is False
    # A legitimate recognized boolean-then-value cluster is not unprovable (it's a normal,
    # expandable cluster `_expand_clustered_short_flag` handles directly).
    assert hook._short_flag_cluster_is_unprovable("-iFquery=x") is False
    # Not cluster-shaped at all: long flags, `--`, and a bare non-flag token are never flagged.
    assert hook._short_flag_cluster_is_unprovable("--field") is False
    assert hook._short_flag_cluster_is_unprovable("graphql") is False


def test_gh_api_flags_contain_unprovable_cluster_respects_value_position():
    """`_gh_api_flags_contain_unprovable_cluster` must not misjudge a preceding detached value
    flag's OPERAND as a flag to classify, mirroring the same operand-protection guarantee proven
    for `_expand_clustered_gh_api_flags` elsewhere in this file."""
    # `-zX` here is `-t`'s template-string OPERAND, not a flag — must not be flagged.
    assert hook._gh_api_flags_contain_unprovable_cluster(["-t", "-zX", "graphql"]) is False
    # But a genuine unrecognized cluster in flag position is caught.
    assert hook._gh_api_flags_contain_unprovable_cluster(["-zFquery=x", "graphql"]) is True
    # The SAME operand-protection must hold when the preceding detached value flag was itself
    # produced by expanding a legitimate cluster (`-iF` == `-i -F`) — `-zX` here is that expanded
    # `-F`'s OPERAND, not a flag to classify, even though it is itself shaped like an unprovable
    # cluster.
    assert hook._gh_api_flags_contain_unprovable_cluster(["-iF", "-zX", "graphql"]) is False


def test_guard_and_expander_agree_on_flag_value_position():
    """`_gh_api_flags_contain_unprovable_cluster` (the guard) and `_expand_clustered_gh_api_flags`
    (the expander) are two independently-maintained walks over the same clustered-short-flag
    grammar; they must agree on exactly where a flag ends and its operand begins, or an
    unrecognized-but-actually-an-operand token could be misjudged by one and not the other.
    Self-contained (both walks asserted on the SAME argv, in the SAME test) so the invariant
    cannot be silently broken by editing/removing/skipping only one half elsewhere: the guard must
    treat `-zX` (after an expanded `-iF`) as `-F`'s operand, not a flag to classify, AND the
    expander must leave that same `-zX` completely untouched as `-F`'s operand."""
    argv = ["-iF", "-zX", "graphql"]
    assert hook._gh_api_flags_contain_unprovable_cluster(argv) is False
    assert hook._expand_clustered_gh_api_flags(argv) == ["-i", "-F", "-zX", "graphql"]


def test_gh_api_is_merge_blocks_on_unrecognized_cluster_before_expansion():
    """Direct coverage of the `_gh_api_is_merge` wiring: an unprovable cluster forces `True`
    (block) even though, absent this check, the rest of the function would resolve the endpoint as
    a harmless-looking bare `graphql` positional and find no merge mutation (the query field is
    invisible to it, exactly as pre-fix)."""
    assert hook._gh_api_is_merge(["graphql", "-zFquery=@merge.graphql"]) is True


def test_api_bool_and_value_short_chars_stay_disjoint_and_in_sync():
    """`_API_BOOL_SHORT_CHARS` and `_API_VALUE_SHORT_CHARS` each mirror `gh api`'s flag grammar by
    hand (review flagged this as a drift risk: if a future `gh` adds a second boolean short flag
    without updating `_API_BOOL_SHORT_CHARS`, or a value short char is dropped from
    `_API_VALUE_SHORT_CHARS`, the cluster-expansion safety guarantee silently regresses). This pins
    the two invariants that must hold for `_expand_clustered_short_flag` to stay correct: the two
    sets never overlap (a char can't be both a boolean and a value flag), and every non-fieldish
    short char backing `_API_VALUE_FLAGS` (`-H/-q/-X/-p/-t`, i.e. the short spelling of a long-form
    entry) is present in `_API_VALUE_SHORT_CHARS`. A future `gh` change that breaks either
    invariant fails this test loudly instead of silently reopening the bypass."""
    assert hook._API_BOOL_SHORT_CHARS.isdisjoint(hook._API_VALUE_SHORT_CHARS)
    short_value_flags = {
        f.lstrip("-") for f in hook._API_VALUE_FLAGS if f.startswith("-") and not f.startswith("--")
    }
    # Both directions matter: a real short flag missing from either set now fails CLOSED for real
    # (`_gh_api_flags_contain_unprovable_cluster`, #333 review finding — an unrecognized char at
    # the classification boundary blocks the call outright, it is no longer silently left
    # unexpanded and invisible to every detector); an EXTRA, non-real char in
    # `_API_VALUE_SHORT_CHARS` is the fail-OPEN direction a review flagged — it would make
    # `_expand_clustered_short_flag` treat a bogus char as a real trailing value flag and swallow
    # the next token as its operand, hiding the real endpoint/query.
    assert short_value_flags <= hook._API_VALUE_SHORT_CHARS
    assert hook._API_VALUE_SHORT_CHARS - {"f", "F"} <= short_value_flags


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
