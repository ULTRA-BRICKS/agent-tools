#!/usr/bin/env python3
"""Unit tests for require_ticket_before_commit (stdlib unittest, no deps).

Run::
    python3 test_require_ticket.py            # this directory
    python3 -m pytest test_require_ticket.py  # also works under pytest
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
_SCRIPT = _HERE / "require_ticket_before_commit.py"

# Import the script as a module so we can unit-test its helpers directly.
_spec = importlib.util.spec_from_file_location("require_ticket", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_HATCH_ENV = "RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT"
# Ambient agent-identity env (the test run may itself be inside an omp subagent) selects the
# block message's role variant; scrub it so every helper starts from the "unknown role" baseline.
_ROLE_ENV = ("RIG_AGENT_ID", "RIG_PARENT_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE")


def run_hook(
    command: str,
    *,
    strict: bool | None = None,
    env_extra: dict | None = None,
    cwd: str | None = None,
) -> tuple[int, str | None]:
    """Run the script end-to-end with a JSON event on stdin; return (exit, decision).

    The event's `cwd` defaults to an isolated empty temp dir (no git repo) so the
    hook's branch detection returns "" — otherwise the host checkout's branch name
    could leak a ticket pattern and make these tests non-deterministic.

    `strict`: None → leave REQUIRE_TICKET_STRICT UNSET (exercise the real default,
    which is now strict-block); True → `=1`; False → `=0` (explicit warn-only opt-out).
    """
    env = dict(os.environ)
    env.pop("REQUIRE_TICKET_STRICT", None)
    env.pop("REQUIRE_TICKET_SKIP", None)
    env.pop("REQUIRE_TICKET_EXEMPT_TYPES", None)
    env.pop(_HATCH_ENV, None)
    for name in _ROLE_ENV:
        env.pop(name, None)
    if strict is True:
        env["REQUIRE_TICKET_STRICT"] = "1"
    elif strict is False:
        env["REQUIRE_TICKET_STRICT"] = "0"
    if env_extra:
        env.update(env_extra)

    with tempfile.TemporaryDirectory() as isolated:
        event_cwd = cwd or isolated
        event = json.dumps({"args": {"command": command}, "cwd": event_cwd})
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,  # process cwd also outside any git repo
            timeout=10,
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise AssertionError(f"empty stdout; stderr={proc.stderr!r}")
    decision = json.loads(out)["decision"]
    return proc.returncode, decision


def _run_hook_raw(command: str, **kwargs) -> dict:
    """Like run_hook but also returns the protocol `message` (for marker assertions)."""
    env = dict(os.environ)
    env.pop("REQUIRE_TICKET_STRICT", None)
    env.pop("REQUIRE_TICKET_SKIP", None)
    env.pop("REQUIRE_TICKET_EXEMPT_TYPES", None)
    env.pop(_HATCH_ENV, None)
    for name in _ROLE_ENV:
        env.pop(name, None)
    if kwargs.get("env_extra"):
        env.update(kwargs["env_extra"])
    with tempfile.TemporaryDirectory() as isolated:
        event = json.dumps({"args": {"command": command}, "cwd": isolated})
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,
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


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the hookSpecificOutput shape the hatch helper parses
# (`decision.behavior == "allow"`). Printing arbitrary text and exiting 0 is a DENY now — the
# helper no longer treats a clean exit as approval.
_ALLOW_REPLY_SH = (
    "printf '{\"hookSpecificOutput\":{\"hookEventName\":\"PermissionRequest\","
    "\"decision\":{\"behavior\":\"allow\"}}}'\nexit 0\n"
)


def _run_hook_inproc(command: str, *, env_extra: dict | None = None, tg_ctl: Path) -> dict:
    """Run the hook in-process so the trusted tg-ctl path can be patched to a fake binary."""

    env_keys = {
        "REQUIRE_TICKET_STRICT",
        "REQUIRE_TICKET_SKIP",
        "REQUIRE_TICKET_EXEMPT_TYPES",
        _HATCH_ENV,
    } | set(env_extra or {}) | set(_ROLE_ENV)
    old_env = {key: os.environ.get(key) for key in env_keys}
    try:
        for key in env_keys:
            os.environ.pop(key, None)
        for key, value in (env_extra or {}).items():
            os.environ[key] = value

        with tempfile.TemporaryDirectory() as isolated:
            home = Path(isolated) / "home"
            home.mkdir()
            event = json.dumps({"args": {"command": command}, "cwd": isolated})
            out = io.StringIO()
            with (
                mock.patch.object(mod, "STRICT", True),
                mock.patch.object(mod.hatch_escalation, "resolve_home", lambda: str(home)),
                mock.patch.object(mod.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,)),
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
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"hook stdout is not protocol JSON: {raw!r}") from exc
        return {"code": code, "decision": payload["decision"], "message": payload.get("message", "")}
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TicketHeuristic(unittest.TestCase):
    def test_detects_github_issue(self):
        self.assertTrue(mod.has_ticket_reference("feat: export (Refs #123)"))

    def test_detects_qualified_github(self):
        self.assertTrue(mod.has_ticket_reference("feat: x alex-mextner/task-cli#4"))

    def test_detects_linear_key(self):
        self.assertTrue(mod.has_ticket_reference("fix: crash ENG-456"))

    def test_detects_jira_style_lowercase_subject(self):
        self.assertTrue(mod.has_ticket_reference("ABC-12 add export"))

    def test_detects_task_cli_forms(self):
        self.assertTrue(mod.has_ticket_reference("done task:ABC-12"))
        self.assertTrue(mod.has_ticket_reference("done task #7"))
        self.assertTrue(mod.has_ticket_reference("done T-9"))

    def test_detects_trailer_keywords(self):
        self.assertTrue(mod.has_ticket_reference("Closes #12"))
        self.assertTrue(mod.has_ticket_reference("Fixes ABC-3"))

    def test_detects_tracker_url(self):
        self.assertTrue(
            mod.has_ticket_reference("see https://github.com/o/r/issues/42")
        )
        self.assertTrue(
            mod.has_ticket_reference("see https://linear.app/acme/issue/ENG-7/title")
        )

    def test_no_false_positive_on_plain_subject(self):
        self.assertFalse(mod.has_ticket_reference("feat: add CSV export to reports"))

    def test_no_false_positive_on_version_or_date(self):
        # A bare version / date must not look like a KEY-NUM ticket.
        self.assertFalse(mod.has_ticket_reference("chore: bump to v2 on 2026-06-15"))

    def test_single_letter_key_is_not_a_ticket(self):
        # KEY-NUM needs >=2 uppercase letters; `A1-456` must NOT count.
        self.assertFalse(mod.has_ticket_reference("perf: shrink A1-456 buffer"))

    def test_bare_fix_keyword_is_not_a_reference(self):
        # A trailer keyword must point at a real ref — `fix: null deref` is not one.
        self.assertFalse(mod.has_ticket_reference("fix: null deref in handler"))
        self.assertFalse(mod.has_ticket_reference("Fixes the broken parser"))

    def test_fix_keyword_with_real_ref_is_a_reference(self):
        self.assertTrue(mod.has_ticket_reference("Fixes #42"))
        self.assertTrue(mod.has_ticket_reference("Fixes ENG-3"))


class Exemptions(unittest.TestCase):
    def test_chore_type_is_exempt(self):
        self.assertTrue(mod.is_exempt("chore: bump lockfile"))

    def test_docs_type_is_exempt(self):
        self.assertTrue(mod.is_exempt("docs(readme): fix typo"))

    def test_wip_is_exempt(self):
        self.assertTrue(mod.is_exempt("wip: spike"))

    def test_fixup_is_exempt(self):
        self.assertTrue(mod.is_exempt("fixup! feat: thing"))

    def test_merge_is_exempt(self):
        self.assertTrue(mod.is_exempt("Merge branch 'main' into x"))

    def test_feat_is_not_exempt(self):
        self.assertFalse(mod.is_exempt("feat: add export"))

    def test_fix_is_not_exempt(self):
        self.assertFalse(mod.is_exempt("fix: null deref"))

    def test_body_line_does_not_exempt(self):
        # An exempt-looking type word in the body must not exempt a real `feat:`.
        self.assertFalse(mod.is_exempt("feat: add export\n\ndocs: also touched a doc"))


def _commit_argv(command: str) -> list[str]:
    """Helper for the unit tests: parse `command` and return the FIRST commit segment's argv (the
    tokens after `commit`), or [] if there is no real commit segment."""
    segs = mod.commit_segments(command)
    return segs[0].argv if segs else []


class MessageExtraction(unittest.TestCase):
    def test_pulls_dash_m(self):
        msg = mod.commit_message_from_argv(_commit_argv('git commit -m "feat: x Refs #5"'))
        self.assertIn("Refs #5", msg)

    def test_pulls_attached_dash_m(self):
        msg = mod.commit_message_from_argv(_commit_argv('git commit -m"ENG-1 thing"'))
        self.assertIn("ENG-1", msg)

    def test_pulls_message_equals(self):
        msg = mod.commit_message_from_argv(_commit_argv('git commit --message="task:T-3 thing"'))
        self.assertIn("task:T-3", msg)

    def test_pulls_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: big change\n\nCloses #99\n")
            path = fh.name
        try:
            msg = mod.commit_message_from_argv(_commit_argv(f"git commit -F {path}"))
            self.assertIn("Closes #99", msg)
        finally:
            os.unlink(path)

    def test_pulls_glued_dash_m_ticket(self):
        # a KEY-NUM ticket glued onto -m / --message= must be detected.
        self.assertIn("ABC-123", mod.commit_message_from_argv(_commit_argv("git commit -mABC-123")))
        self.assertIn("ENG-7", mod.commit_message_from_argv(_commit_argv("git commit --message=ENG-7")))

    def test_pulls_glued_dash_F_file(self):
        # `-FPATH` glued (git accepts both `-F path` and `-Fpath`) — the message file must be read.
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: big change\n\nCloses #321\n")
            path = fh.name
        try:
            msg = mod.commit_message_from_argv(_commit_argv(f"git commit -F{path}"))
            self.assertIn("Closes #321", msg)
        finally:
            os.unlink(path)

    def test_unbalanced_quotes_are_not_a_commit_segment(self):
        # An unbalanced-quote command can't be tokenized → no commit segment is parsed (the safe
        # direction for require-ticket, which is on_error=open: an unparseable command is not gated).
        self.assertEqual(mod.commit_segments('git commit -m "unterminated #7'), [])

    def test_dash_F_dash_does_not_read_a_file_named_dash(self):
        # `-F -` is the STDIN sentinel, NOT a file named "-": the message extractor must not try to
        # open it (it would fail) and must contribute no text — the stdin-message detection handles it.
        self.assertEqual(
            mod.commit_message_from_argv(_commit_argv("git commit -F -")).strip(), ""
        )


class HeredocMessages(unittest.TestCase):
    """This repo's own CLAUDE.md tells every agent to author a multi-line commit message via
    `git commit -m "$(cat <<'EOF' … EOF)"`. A heredoc BODY is not ordinary shell text — its
    `"`/`'`/`#` are literal, never real shell syntax — but the tokenizer used to have zero
    heredoc awareness, so raw body characters corrupted its quote/comment tracking. That went
    wrong BOTH ways: a real ticket reference past a stray embedded quote in the body was
    mistaken for a shell comment and silently dropped (agent-tools#406 — a false BLOCK on a
    correctly-ticketed commit), and an ODD embedded-quote count left the whole command looking
    quote-unbalanced, so the on_error=open gate failed OPEN — silently ALLOWING a genuinely
    ticketless commit (agent-tools#503/#430's same root cause, the other face of it). See
    `_neutralize_heredocs` in the hook for the fix."""

    def test_heredoc_m_with_ticket_allows(self):
        # agent-tools#503's exact repro: a multi-line -m built via `$(cat <<'EOF' … EOF)`.
        command = (
            "git commit -m \"$(cat <<'EOF'\n"
            "fix: some message\n\nCloses #502\n\nCo-Authored-By: Someone <x@example.com>\n"
            "EOF\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_m_unquoted_delimiter_with_embedded_quote_and_ticket_allows(self):
        # agent-tools#406's repro shape: an UNQUOTED delimiter (`<<EOF`, an expanding body) whose
        # body has an embedded double quote.
        command = (
            "git commit -m \"$(cat <<EOF\n"
            'fix: handle "quoted" phrase correctly\n\nFixes #123\nEOF\n)"'
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_m_paired_embedded_quote_before_ticket_allows(self):
        # The concrete false-BLOCK reproduced live against the pre-fix hook: a PAIRED embedded
        # quote ("note one") before the ticket reference desynced the naive quote-tracker's
        # parity, so `#999` landed in a spot the (uncorrected) tracker thought was "outside
        # quotes" and the comment-stripping step chopped the message there, losing "Closes
        # #999" entirely.
        command = (
            "git commit -m \"$(cat <<'EOF'\n"
            'fix: "note one" more text\n\nCloses #999\nEOF\n)"'
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_m_odd_embedded_quote_no_ticket_still_blocks(self):
        # The flip side of the same bug, also reproduced live: an ODD embedded-quote count made
        # the WHOLE command look quote-unbalanced to the pre-fix tokenizer, so it failed to parse
        # at all and the on_error=open gate silently ALLOWED — even with no ticket reference
        # anywhere. That is a real gate bypass, not just a false block, and must not regress.
        command = (
            "git commit -m \"$(cat <<'EOF'\n"
            'fix: handle "quoted phrase with no ticket at all, odd embedded quote\nEOF\n)"'
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_heredoc_m_closes_182_allows(self):
        # agent-tools#430's exact repro.
        command = (
            "git commit -m \"$(cat <<'EOF'\n"
            "fix: landing tg-cli PR 286\n\nCloses #182\nEOF\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_m_without_ticket_blocks(self):
        # Positive-path regression: a heredoc-authored message with NO ticket must still block —
        # the fix must not make the hook permissive for the documented multi-line pattern.
        command = "git commit -m \"$(cat <<'EOF'\nfix: some message with no ticket at all\nEOF\n)\""
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_heredoc_m_chore_exemption_preserved(self):
        # An exempt conventional-commit type authored via heredoc is still recognized as exempt —
        # the heredoc unwrap must recover the REAL subject line, not a `$(cat …)`-prefixed one.
        command = "git commit -m \"$(cat <<'EOF'\nchore: bump deps\nEOF\n)\""
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_m_second_commit_in_chain_without_ticket_blocks(self):
        # A chain of two heredoc-authored commits: the first is exempt (chore:), the second is
        # not and carries no ticket — the gate must still catch the SECOND segment, not just the
        # first.
        command = (
            "git commit -m \"$(cat <<'EOF'\nchore: first one\nEOF\n)\" && "
            "git commit -m \"$(cat <<'EOF'\nfix: second one no ticket\nEOF\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_heredoc_written_file_dash_F_with_ticket_allows(self):
        # agent-tools#570's primary repro: the message FILE is written by a heredoc in the SAME
        # Bash command as the `git commit -F <file>` that reads it. A PreToolUse hook runs BEFORE
        # the shell executes anything, so the file does not exist yet at hook time — a plain disk
        # read always fails for this shape. The fix reads the heredoc body captured from the
        # COMMAND TEXT instead.
        tmp_dir = tempfile.mkdtemp()
        msg = os.path.join(tmp_dir, "commit-msg.txt")  # deliberately NOT created ahead of time
        command = (
            f"cat > {msg} <<'EOF'\n"
            "fix: reveal feature slides one at a time\n\nRefs #143\nEOF\n"
            f"git commit -q -F {msg}"
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_written_file_dash_F_without_ticket_blocks(self):
        # Positive-path regression for the #570 fix: the SAME shape but with no ticket in the
        # body must still block — the heredoc-body fallback must not become a free bypass.
        tmp_dir = tempfile.mkdtemp()
        msg = os.path.join(tmp_dir, "commit-msg.txt")
        command = (
            f"cat > {msg} <<'EOF'\n"
            "fix: reveal feature slides one at a time, no ticket here\nEOF\n"
            f"git commit -q -F {msg}"
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_heredoc_body_mentioning_commit_flag_is_not_gated(self):
        # agent-tools#570's "bonus repro": a heredoc body that merely MENTIONS `git commit -F …`
        # as prose (inside an unrelated `gh issue create --body "$(cat <<'EOF' … EOF)"`) must
        # never be mistaken for a real commit segment — the pre-fix tokenizer's line-by-line
        # retry loop could treat an independently-parseable body LINE as its own command segment.
        command = (
            "gh issue create --body \"$(cat <<'EOF'\n"
            "Repro: run `git commit -F msg.txt` and see it block.\nEOF\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")


class HeredocReviewRoundFindings(unittest.TestCase):
    """Four real gaps found reviewing the heredoc-neutralization fix itself, before any of them
    shipped:
      1. `_heredoc_body_for_target` matched by TARGET PATH with no ordering check — a heredoc that
         writes its target file AFTER a commit already read it could retroactively "satisfy" that
         commit's ticket gate, even though the commit actually ran against whatever the file held
         BEFORE that later write. A real gate bypass.
      2. Backtick command substitution wasn't tracked as its own parsing frame, even though the
         `_BACKTICK_CAT_SUBST_RE` unwrap regex implied support — a heredoc inside backticks with a
         corrupting embedded quote could still fail to tokenize and fail OPEN.
      3. A COMPACT heredoc opener with no space before it (`cat<<'EOF'`) glued the sentinel
         directly onto `cat`, breaking the `$(cat SENTINEL)` unwrap's word boundary and
         false-blocking an otherwise-exempt commit.
      4. Multiple heredocs chained on one line (`cmd <<A <<B`) were modeled as only the first — a
         ticket hidden in an EARLIER, UNUSED heredoc could satisfy the gate while the command
         actually read a different, ticketless body (the LAST one wins in real shell redirection).
    """

    def test_heredoc_target_ordering_rejects_a_later_write(self):
        tmp_dir = tempfile.mkdtemp()
        msg = os.path.join(tmp_dir, "msg")
        with open(msg, "w", encoding="utf-8") as fh:
            fh.write("fix: no ticket\n")
        command = f"git commit -F {msg}; cat > {msg} <<'EOF'\nRefs #123\nEOF\n"
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_heredoc_target_ordering_allows_an_earlier_write(self):
        tmp_dir = tempfile.mkdtemp()
        msg = os.path.join(tmp_dir, "msg")
        command = f"cat > {msg} <<'EOF'\nRefs #123\nEOF\ngit commit -F {msg}"
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_backtick_heredoc_with_embedded_quote_and_no_ticket_still_blocks(self):
        command = (
            "git commit -m \"`cat <<'EOF'\n"
            'fix: unmatched " quote, no ticket\nEOF\n`"'
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_backtick_heredoc_with_ticket_allows(self):
        command = (
            "git commit -m \"`cat <<'EOF'\n"
            'fix: unmatched " quote, WITH ticket Refs #77\nEOF\n`"'
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_compact_heredoc_opener_no_space_chore_exemption_preserved(self):
        command = "git commit -m \"$(cat<<'EOF'\nchore: compact redirection\nEOF\n)\""
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_chained_heredocs_use_the_last_ones_body_not_the_first(self):
        command = (
            "git commit -m \"$(cat <<A <<B\n"
            "ignored Refs #99\nA\nfix: actual message without a ticket\nB\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_chained_heredocs_ticket_in_winning_body_allows(self):
        command = (
            "git commit -m \"$(cat <<A <<B\n"
            "ignored no ticket here\nA\nfix: actual message Refs #55\nB\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")


class HeredocReviewRound2Findings(unittest.TestCase):
    """Three more real gaps found reviewing round 1's own fix:
      P1 (critical — a real ALLOW bypass): `<<` inside a shell `#` COMMENT was still read as a
         real heredoc opener, so the neutralizer swallowed the next physical lines — including a
         REAL, ticketless `git commit` — as if they were the heredoc's body. `commit_segments`
         then found no commit at all and the hook silently ALLOWED it.
      P2a (a false BLOCK the round-1 ordering fix itself introduced): the ordering cutoff searched
         independently for a bare word "commit" in the rewritten text, which could match unrelated
         prose (`echo commit`) BEFORE the real commit segment and mis-anchor the cutoff, wrongly
         rejecting a genuinely earlier heredoc write.
      P2b (a false BLOCK on a plausible shape): the redirect-target regex was whitespace-delimited,
         so a quoted path containing an internal space (`cat > '/tmp/commit message' <<EOF`) was
         truncated at the first space and never matched the later `-F '/tmp/commit message'`.
    """

    def test_comment_hiding_a_heredoc_opener_does_not_hide_the_real_commit(self):
        command = "# <<EOF\ngit commit -m 'fix: no ticket'\nEOF\n"
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_leading_comment_does_not_break_a_legitimate_heredoc_commit(self):
        command = (
            "# a leading comment\n"
            "git commit -m \"$(cat <<'EOF'\nfix: real change\n\nRefs #200\nEOF\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_ordering_not_confused_by_unrelated_commit_prose(self):
        tmp_dir = tempfile.mkdtemp()
        msg = os.path.join(tmp_dir, "msg")
        command = (
            "echo commit\n"
            f"cat > {msg} <<'EOF'\nRefs #123\nEOF\n"
            f"git commit -F {msg}"
        )
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_heredoc_target_quoted_path_with_space_allows(self):
        tmp_dir = tempfile.mkdtemp()
        quoted = os.path.join(tmp_dir, "commit message")
        command = f"cat > '{quoted}' <<'EOF'\nRefs #123\nEOF\ngit commit -F '{quoted}'"
        code, decision = run_hook(command)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")


class HeredocReviewRound3Findings(unittest.TestCase):
    """Codex review on the heredoc PR (#599): last-write ordering for `-F`, inline `#` comments
    hiding `<<EOF`, and unquoted-body expansion false-positives."""

    def test_inline_comment_hiding_heredoc_opener_does_not_hide_real_commit(self):
        command = 'echo harmless # <<EOF\n git commit -m "fix: ticketless"\nEOF\n'
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_multiple_heredoc_writes_use_last_body_before_commit(self):
        tmp_dir = tempfile.mkdtemp()
        msg = os.path.join(tmp_dir, "msg")
        command = (
            f"cat > {msg} <<'EOF'\nRefs #123\nEOF\n"
            f"cat > {msg} <<'EOF'\nfix: no ticket\nEOF\n"
            f"git commit -F {msg}"
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_unquoted_heredoc_expansion_false_positive_does_not_allow(self):
        command = (
            "git commit -m \"$(cat <<EOF\n"
            "Refs ${UNSET:+#999}\nEOF\n)\""
        )
        code, decision = run_hook(command)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class StdinMessageDetection(unittest.TestCase):
    """`git commit -F -` streams the message on GIT's stdin, which a PreToolUse hook cannot read.
    The detector must recognize every spelling of the stdin sentinel so the gate can fail-closed
    with a hint (agent-tools#104)."""

    def test_detects_dash_F_dash(self):
        self.assertTrue(mod.commit_reads_stdin_message(_commit_argv("git commit -F -")))

    def test_detects_long_file_dash(self):
        self.assertTrue(mod.commit_reads_stdin_message(_commit_argv("git commit --file -")))

    def test_detects_glued_dash_F_dash(self):
        self.assertTrue(mod.commit_reads_stdin_message(_commit_argv("git commit -F-")))

    def test_detects_file_equals_dash(self):
        self.assertTrue(mod.commit_reads_stdin_message(_commit_argv("git commit --file=-")))

    def test_real_file_is_not_stdin(self):
        self.assertFalse(mod.commit_reads_stdin_message(_commit_argv("git commit -F /tmp/msg.txt")))

    def test_dash_m_is_not_stdin(self):
        self.assertFalse(mod.commit_reads_stdin_message(_commit_argv('git commit -m "feat: x"')))

    def test_dash_after_ddash_is_a_pathspec_not_stdin(self):
        # after `--` everything is a literal pathspec — a `-` there is a file, not a -F value.
        self.assertFalse(mod.commit_reads_stdin_message(_commit_argv("git commit -m x -- -")))

    def test_reader_and_detector_agree_on_every_file_spelling(self):
        # PARITY: a readable `-F`/`--file` value (any of the four spellings) must be READ by
        # commit_message_from_argv AND not be mistaken for stdin; a `-` value must do the opposite.
        # Pins that the two parsers don't drift (the docstring claims they recognize the same forms).
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: x\n\nCloses #5\n")
            path = fh.name
        try:
            for spelling in (f"-F {path}", f"-F{path}", f"--file {path}", f"--file={path}"):
                argv = _commit_argv(f"git commit {spelling}")
                self.assertIn("Closes #5", mod.commit_message_from_argv(argv), spelling)
                self.assertFalse(mod.commit_reads_stdin_message(argv), spelling)
        finally:
            os.unlink(path)
        for stdin_spelling in ("-F -", "-F-", "--file -", "--file=-"):
            argv = _commit_argv(f"git commit {stdin_spelling}")
            self.assertEqual(mod.commit_message_from_argv(argv).strip(), "", stdin_spelling)
            self.assertTrue(mod.commit_reads_stdin_message(argv), stdin_spelling)


class EndToEnd(unittest.TestCase):
    def test_missing_ticket_blocks_by_default(self):
        # The new default (REQUIRE_TICKET_STRICT unset) is a hard BLOCK.
        code, decision = run_hook('git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_block_message_carries_stable_marker(self):
        # The Docker test asserts on this fixed marker — it must lead the block message.
        proc_out = _run_hook_raw('git commit -m "feat: add export"')
        self.assertEqual(proc_out["code"], mod.BLOCK_EXIT_CODE)
        self.assertEqual(proc_out["decision"], "block")
        self.assertIn(mod.BLOCK_MARKER, proc_out["message"])
        self.assertIn("[require-ticket] BLOCKED: no ticket reference", proc_out["message"])

    def test_missing_ticket_warns_but_allows_when_strict_disabled(self):
        # REQUIRE_TICKET_STRICT=0 is the explicit warn-only opt-out.
        code, decision = run_hook('git commit -m "feat: add export"', strict=False)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_warn_mode_message_omits_block_marker(self):
        # An ALLOW (warn-only) must NOT carry BLOCK_MARKER — else an external check that greps
        # stdout for the marker gets a false positive on an allowed commit.
        out = _run_hook_raw('git commit -m "feat: add export"', env_extra={"REQUIRE_TICKET_STRICT": "0"})
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")
        self.assertNotIn(mod.BLOCK_MARKER, out["message"])

    def test_present_ticket_allows_clean(self):
        code, decision = run_hook('git commit -m "feat: add export (Refs #123)"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_missing_ticket_blocks_in_explicit_strict_mode(self):
        code, decision = run_hook('git commit -m "feat: add export"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_ticket_present_allows_even_in_strict_mode(self):
        code, decision = run_hook(
            'git commit -m "feat: add export ENG-9"', strict=True
        )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_chore_exempt_in_strict_mode(self):
        code, decision = run_hook('git commit -m "chore: bump dep"', strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_amend_not_gated_in_strict_mode(self):
        code, decision = run_hook("git commit --amend --no-edit", strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_non_commit_command_allowed(self):
        code, decision = run_hook("git status", strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_custom_exempt_types_in_strict_mode(self):
        code, decision = run_hook(
            'git commit -m "perf: faster export"',
            strict=True,
            env_extra={"REQUIRE_TICKET_EXEMPT_TYPES": "perf"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_bare_fix_blocks_in_strict_mode(self):
        # `fix:` is a bugfix (not exempt) and `null deref` is not a ticket ref.
        code, decision = run_hook('git commit -m "fix: null deref"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_relative_message_file_resolves_against_cwd(self):
        # -F with a RELATIVE path must resolve against the event cwd, not the hook's.
        with tempfile.TemporaryDirectory() as repo:
            msg_path = Path(repo) / "COMMIT_MSG.txt"
            msg_path.write_text("feat: add export\n\nCloses #321\n", encoding="utf-8")
            code, decision = run_hook(
                "git commit -F COMMIT_MSG.txt", strict=True, cwd=repo
            )
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_dash_F_dash_fails_closed_with_hint(self):
        # agent-tools#104 (reversing #102): `git commit -F -` streams the message on git's stdin,
        # which the hook CANNOT read. Rather than let `-F -` silently dodge the ticket gate, it now
        # FAILS CLOSED (block, exit 10, the stable marker) with an ACTIONABLE hint.
        out = _run_hook_raw("git commit -F -")
        self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE, "-F - is unreadable — must fail-closed")
        self.assertEqual(out["decision"], "block")
        self.assertIn(mod.BLOCK_MARKER, out["message"])
        # the hint must name the readable ways to satisfy the gate and the sanctioned live hatch.
        msg = out["message"]
        self.assertIn("stdin", msg.lower())
        self.assertIn("-m", msg)
        self.assertIn("-F <file>", msg)
        self.assertIn("branch", msg.lower())  # branch-name path is a valid way to ticket a `-F -`
        self.assertIn(_HATCH_ENV, msg)
        self.assertNotIn("REQUIRE_TICKET_SKIP=", msg)
        # the hint must NOT promise the `[skip-ticket: …]` TRAILER — it was removed, and for `-F -`
        # it would also live in unreadable stdin.
        self.assertNotIn("skip-ticket", msg.lower())
        # and the `-` sentinel must NOT have been treated as a file to open (no spurious read warning).
        self.assertNotIn("could not read commit-message file", out["stderr"])

    def test_dash_F_dash_every_spelling_fails_closed(self):
        # all four stdin spellings (`-F -`, `-F-`, `--file -`, `--file=-`) must fail-closed alike.
        for spelling in ("-F -", "-F-", "--file -", "--file=-"):
            code, decision = run_hook(f"git commit {spelling}", strict=True)
            self.assertEqual(code, mod.BLOCK_EXIT_CODE, spelling)
            self.assertEqual(decision, "block", spelling)

    def test_dash_F_dash_inline_skip_env_no_longer_bypasses(self):
        # The old per-command inline escape was removed; a no-ticket `-F -` commit still blocks
        # unless the live Telegram hatch is approved.
        code, decision = run_hook(
            "REQUIRE_TICKET_SKIP=1 git commit -F -", strict=True
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_dash_F_dash_hatch_approved_allows(self):
        # The sanctioned hatch is consulted before the unreadable-stdin block.
        with tempfile.TemporaryDirectory() as tmp:
            tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", _ALLOW_REPLY_SH)
            out = _run_hook_inproc(
                "git commit -F -",
                env_extra={_HATCH_ENV: "one-off backfill, no ticket"},
                tg_ctl=tg_ctl,
            )
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")
        self.assertNotIn(mod.BLOCK_MARKER, out["message"])

    def test_dash_F_dash_hatch_denied_blocks(self):
        # A requested hatch must fail closed when tg-ctl exits nonzero.
        with tempfile.TemporaryDirectory() as tmp:
            tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", 'printf "approved?\\n"\nexit 1\n')
            out = _run_hook_inproc(
                "git commit -F -",
                env_extra={_HATCH_ENV: "one-off backfill, no ticket"},
                tg_ctl=tg_ctl,
            )
        self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE)
        self.assertEqual(out["decision"], "block")
        self.assertIn("hatch escalation denied", out["message"].lower())

    def test_dash_F_dash_inline_hatch_approved_allows(self):
        # Pre-bash hooks must parse the documented inline prefix from the command string because
        # the shell has not exported it to the hook process yet.
        with tempfile.TemporaryDirectory() as tmp:
            tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", _ALLOW_REPLY_SH)
            out = _run_hook_inproc(
                f'{_HATCH_ENV}="one-off backfill, no ticket" git commit -F -',
                tg_ctl=tg_ctl,
            )
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")

    def test_dash_F_dash_on_ticket_branch_allows(self):
        # A `-F -` (unreadable stdin message) commit is still ticketed by its BRANCH name — same as
        # the editor-commit path. The stdin block must run AFTER branch detection, so this passes
        # rather than false-blocking a legit workflow (agent-tools#104 review finding).
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", "-b", "feature/ENG-42-export", repo], check=True)
            code, decision = run_hook(f"git -C {repo} commit -F -", strict=True, cwd=repo)
        self.assertEqual(code, 0, "branch ENG-42 should satisfy the gate even for `-F -`")
        self.assertEqual(decision, "allow")

    def test_dash_F_dash_on_non_ticket_branch_still_blocks(self):
        # control: a `-F -` on a branch WITHOUT a ticket id still fails-closed (the branch can't
        # rescue it), so the block isn't silently defeated by any branch.
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
            code, decision = run_hook(f"git -C {repo} commit -F -", strict=True, cwd=repo)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_dash_F_dash_warn_only_downgrades_to_allow(self):
        # REQUIRE_TICKET_STRICT=0 downgrades the `-F -` block to a warn-only allow, like every other
        # no-ticket case — and must NOT leak the BLOCK_MARKER on an allow.
        out = _run_hook_raw("git commit -F -", env_extra={"REQUIRE_TICKET_STRICT": "0"})
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["decision"], "allow")
        self.assertNotIn(mod.BLOCK_MARKER, out["message"])
        self.assertIn("stdin", out["message"].lower())

    def test_dash_F_real_file_without_ticket_still_blocks(self):
        # the `-F -` stdin special-case (fail-closed-with-hint) is ONLY for the `-` sentinel: a
        # READABLE `-F <file>` with no ticket is read and blocks on the normal no-ticket path.
        with tempfile.TemporaryDirectory() as repo:
            msg_path = Path(repo) / "COMMIT_MSG.txt"
            msg_path.write_text("feat: add export\n", encoding="utf-8")
            code, decision = run_hook("git commit -F COMMIT_MSG.txt", strict=True, cwd=repo)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class ValidRefFormsPassByDefault(unittest.TestCase):
    """Every accepted ticket-reference form must PASS end-to-end under the strict default
    (no REQUIRE_TICKET_STRICT set) — so a legitimately-ticketed commit is never wedged."""

    def _assert_allows(self, subject: str):
        code, decision = run_hook(f'git commit -m "{subject}"')
        self.assertEqual(code, 0, f"{subject!r} should pass but exit={code}")
        self.assertEqual(decision, "allow", f"{subject!r} should pass")

    def test_closes_hash(self):
        self._assert_allows("feat: add export\\n\\nCloses #123")

    def test_fixes_hash(self):
        self._assert_allows("fix: crash\\n\\nFixes #4")

    def test_bare_hash(self):
        self._assert_allows("feat: add export #88")

    def test_task_cli_key_num_id(self):
        self._assert_allows("feat: add export ENG-456")

    def test_task_cli_task_form(self):
        self._assert_allows("feat: add export task:ABC-12")

    def test_ticket_trailer(self):
        self._assert_allows("feat: add export [ticket: PROJ-9]")

    def test_ticket_trailer_with_slug(self):
        # a non-numeric task-cli id inside a [ticket: …] trailer also counts
        self._assert_allows("feat: add export [ticket: backfill-orders]")


class RemovedSelfServiceEscapes(unittest.TestCase):
    """Old self-service per-commit escapes must not bypass the ticket gate."""

    def test_skip_ticket_trailer_blocks_even_with_reason(self):
        code, decision = run_hook(
            'git commit -m "feat: x [skip-ticket: one-off backfill]"'
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_skip_ticket_trailer_requires_reason(self):
        # a blank `[skip-ticket:]` also blocks.
        code, decision = run_hook('git commit -m "feat: x [skip-ticket: ]"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_inline_skip_env_no_longer_bypasses(self):
        code, decision = run_hook('REQUIRE_TICKET_SKIP=1 git commit -m "feat: x"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_inline_skip_falsey_does_not_allow(self):
        code, decision = run_hook('REQUIRE_TICKET_SKIP=0 git commit -m "feat: x"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_inline_skip_on_sibling_does_not_bypass(self):
        # the assignment is on a SIBLING command, not the `git commit` segment → still blocks.
        code, decision = run_hook(
            'REQUIRE_TICKET_SKIP=1 echo hi && git commit -m "feat: x"'
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_removed_skip_helpers_are_not_exported(self):
        self.assertFalse(hasattr(mod, "has_skip_trailer"))
        self.assertFalse(hasattr(mod, "has_inline_skip"))

    def test_inline_skip_with_shell_op_in_message_no_longer_bypasses(self):
        # The quote-aware tokenizer still keeps `;` inside the commit message; the old inline skip
        # env simply is no longer a grant.
        code, decision = run_hook(
            'REQUIRE_TICKET_SKIP=1 git commit -m "fix: handle a; b edge case"'
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class StrictToggle(unittest.TestCase):
    def test_strict_default_when_unset(self):
        # the module-level STRICT reflects the env at import; assert the falsey set directly.
        self.assertIn("0", mod._STRICT_FALSEY)
        self.assertIn("false", mod._STRICT_FALSEY)
        self.assertIn("off", mod._STRICT_FALSEY)

    def test_strict_zero_warns(self):
        code, decision = run_hook('git commit -m "feat: x"', strict=False)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_strict_unset_blocks(self):
        code, decision = run_hook('git commit -m "feat: x"', strict=None)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


def _commit_segment(command: str):
    """The FIRST parsed commit segment of `command`, or None."""
    segs = mod.commit_segments(command)
    return segs[0] if segs else None


class GitDashCAndSkipScope(unittest.TestCase):
    def test_effective_cwd_honors_dash_C(self):
        # `git -C <repo> commit` acts on <repo> — branch detection must read THAT repo.
        self.assertEqual(
            mod.effective_cwd(_commit_segment("git -C /srv/repo commit -m x"), "/event"),
            "/srv/repo",
        )
        self.assertEqual(
            mod.effective_cwd(_commit_segment("git -C/srv/repo commit -m x"), "/event"),
            "/srv/repo",
        )
        # a relative -C resolves against the event cwd
        self.assertEqual(
            mod.effective_cwd(_commit_segment("git -C sub commit -m x"), "/event"), "/event/sub"
        )
        # no -C → the event cwd is unchanged
        self.assertEqual(mod.effective_cwd(_commit_segment("git commit -m x"), "/event"), "/event")

    def test_is_skip_commit_ignores_skip_flag_in_message(self):
        # a flag named only in the MESSAGE is NOT a real skip flag; a real argv flag IS.
        self.assertFalse(mod.is_skip_commit(_commit_argv('git commit -m "uses --amend"')))
        self.assertTrue(mod.is_skip_commit(_commit_argv('git commit --amend -m "x"')))
        # a message-bearing short CLUSTER (`-am`) whose VALUE is literally `--amend` must not exempt.
        self.assertFalse(mod.is_skip_commit(_commit_argv('git commit -am "--amend"')))
        self.assertFalse(mod.is_skip_commit(_commit_argv('git commit -am "support --skip"')))
        self.assertTrue(mod.is_skip_commit(_commit_argv('git commit -am "x" --amend')))

    def test_amend_as_am_message_value_still_blocks(self):
        # `git commit -am "--amend"` authors a real, ticketless commit (message is "--amend") → BLOCK.
        code, decision = run_hook('git commit -am "--amend"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_skip_flag_in_message_does_not_exempt(self):
        # `--amend` inside the commit MESSAGE must NOT exempt a real, ticketless commit.
        code, decision = run_hook('git commit -m "support the --amend flag"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class OverMatchRegression(unittest.TestCase):
    """agent-tools#97 — the gate must fire ONLY on a real `git commit` invocation, never when
    the words "git"/"commit" merely appear as a substring/argument/message-body of SOME OTHER
    command. The old raw regex ``\\bgit\\b.*\\bcommit\\b`` over the RAW command string blocked
    every benign command that mentioned both words (a LIVE false positive: a subagent's `gh
    issue create` whose body said "git commit"). These reproduce that class FIRST — they fail
    against the raw-regex code (it BLOCKS them) and pass once detection is argv-scoped."""

    def test_gh_issue_create_mentioning_commit_is_allowed(self):
        # The LIVE repro: an issue body that contains the words "git commit" is NOT a commit.
        code, decision = run_hook(
            'gh issue create --title "fix the gate" --body "we should git commit only on a real commit"'
        )
        self.assertEqual(code, 0, "gh issue create is not a git commit — must allow")
        self.assertEqual(decision, "allow")

    def test_echo_mentioning_git_commit_is_allowed(self):
        code, decision = run_hook('echo "git commit"')
        self.assertEqual(code, 0, "echo is not a git commit — must allow")
        self.assertEqual(decision, "allow")

    def test_git_log_grep_commit_is_allowed(self):
        # `git log --grep=commit` is a `git log`, not a `git commit`.
        code, decision = run_hook("git log --grep=commit")
        self.assertEqual(code, 0, "git log is not a git commit — must allow")
        self.assertEqual(decision, "allow")

    def test_git_log_grep_separate_value_is_allowed(self):
        code, decision = run_hook("git log --grep commit -n 5")
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_grep_for_word_commit_is_allowed(self):
        # The exact command class that blocked ME mid-session: a grep whose PATTERN says "commit".
        code, decision = run_hook('grep -rn "git commit" src/')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_git_config_commit_gpgsign_is_allowed(self):
        # `git config commit.gpgsign` mentions commit but is not authoring one.
        code, decision = run_hook("git config commit.gpgsign true")
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_git_help_commit_is_allowed(self):
        code, decision = run_hook("git help commit")
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_git_commit_graph_is_allowed(self):
        # `git commit-graph write` is a DIFFERENT subcommand — not `git commit`.
        code, decision = run_hook("git commit-graph write")
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_commit_word_in_filename_is_allowed(self):
        code, decision = run_hook('cat commit_message.txt | grep "git commit"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_tg_report_mentioning_git_commit_is_allowed(self):
        # A status report whose body talks ABOUT a git commit must not be gated.
        code, decision = run_hook('tg "I will git commit the fix once review passes"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    # --- the gate must STILL fire on a real commit (over-match fix must not under-match) ----

    def test_real_no_ticket_commit_still_blocks(self):
        code, decision = run_hook('git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_real_commit_after_a_mentioning_sibling_still_blocks(self):
        # `echo "git commit" && git commit -m feat` — the SECOND segment is a real commit.
        code, decision = run_hook('echo "git commit" && git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


class WrapperClasses(unittest.TestCase):
    """The gate must see a real `git commit` even behind the wrapper classes block-no-verify
    handles (env-prefix, sudo, runuser, timeout), so a wrapped no-ticket commit is still gated,
    and the accepted ticket forms / escapes keep working through the wrapper."""

    def test_env_prefixed_no_ticket_commit_blocks(self):
        code, decision = run_hook('env FOO=bar git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_var_assignment_prefixed_no_ticket_commit_blocks(self):
        code, decision = run_hook('FOO=bar git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_sudo_no_ticket_commit_blocks(self):
        code, decision = run_hook('sudo git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_sudo_u_user_no_ticket_commit_blocks(self):
        # `sudo -u git git commit …` — the `-u` value "git" must not be misread as the executable.
        code, decision = run_hook('sudo -u git git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_runuser_no_ticket_commit_blocks(self):
        code, decision = run_hook('runuser -u git -- git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_timeout_no_ticket_commit_blocks(self):
        code, decision = run_hook('timeout 60 git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_wrapped_commit_with_ticket_allows(self):
        code, decision = run_hook('env FOO=bar git commit -m "feat: add export (Closes #5)"')
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_path_qualified_git_no_ticket_commit_blocks(self):
        code, decision = run_hook('/usr/bin/git commit -m "feat: add export"')
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_effective_cwd_reads_git_dash_C_through_a_wrapper(self):
        # `env A=1 git -C /repo commit`: env's own options must NOT shadow the real `git -C /repo`.
        seg = _commit_segment("env A=1 git -C /repo commit -m x")
        self.assertIsNotNone(seg)
        self.assertEqual(mod.effective_cwd(seg, "/event"), "/repo")

    def test_branch_ticket_satisfies_wrapped_commit(self):
        # A ticket id encoded in the branch name (read via `git -C`) satisfies the gate even through
        # a wrapper — exercised end-to-end against a real temp repo on a ticketed branch.
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", "-b", "feature/ENG-42-export", repo], check=True)
            code, decision = run_hook(
                f'env A=1 git -C {repo} commit -m "feat: add export"', cwd=repo
            )
        self.assertEqual(code, 0, "branch ENG-42 should satisfy the gate")
        self.assertEqual(decision, "allow")


class ClusteredShortFlagDecluster(unittest.TestCase):
    """agent-tools#109 — the message-extraction path must de-cluster combined short option groups the
    way git's commit parser does, so `git commit -am "…"` (the very common `-a` + `-m`) gets its
    message read. The old code matched only `-m`/`-mMSG`/`-F`/`-FPATH`, so `-am`/`-aF`/`-amMSG` fell
    through with an EMPTY message → a ticketed `-am "Closes #5"` false-BLOCKED, and a `-am "chore: …"`
    LOST its exemption. Same clustered-short-flag class block-no-verify fixed in #36–#40. These
    reproduce the bug FIRST (they fail against the pre-fix code) and pass once de-clustering lands.

    Git's rule (verified against real git): a short cluster is read LEFT-TO-RIGHT; the FIRST value
    letter (`m`/`F`) wins — it consumes the rest-of-cluster as a GLUED value if chars follow it
    (`-amMSG` → message "MSG"; `-amF` → message "F", NOT a separate `-F`; `-aFp` → file "p"), else it
    is the cluster's last char and consumes the NEXT token (`-am MSG`, `-aF file`)."""

    # --- the unit helper, exact decluster semantics ---------------------------------------------
    def test_decluster_next_token_forms(self):
        # value letter is the cluster's LAST char → value is the NEXT argv token.
        self.assertEqual(mod._decluster_short_message_flag("-m"), ("m", None))
        self.assertEqual(mod._decluster_short_message_flag("-am"), ("m", None))
        self.assertEqual(mod._decluster_short_message_flag("-aF"), ("F", None))
        self.assertEqual(mod._decluster_short_message_flag("-saF"), ("F", None))

    def test_decluster_glued_forms(self):
        # value letter has chars AFTER it → those chars are the GLUED value; the first letter wins.
        self.assertEqual(mod._decluster_short_message_flag("-mMSG"), ("m", "MSG"))
        self.assertEqual(mod._decluster_short_message_flag("-amMSG"), ("m", "MSG"))
        self.assertEqual(mod._decluster_short_message_flag("-amF"), ("m", "F"))  # NOT a separate -F
        self.assertEqual(mod._decluster_short_message_flag("-aFpath"), ("F", "path"))
        self.assertEqual(mod._decluster_short_message_flag("-Fam"), ("F", "am"))

    def test_decluster_non_message_returns_none(self):
        # a long flag, a positional, a bare `-`, or a boolean-only cluster carries no message value.
        for tok in ("--message", "--amend", "-a", "-sv", "-", "feat", "--file=x"):
            self.assertIsNone(mod._decluster_short_message_flag(tok), tok)

    def test_decluster_other_value_letters_are_not_a_message(self):
        # `-C`/`-c` (reuse/reedit), `-t` (template), `-u` (untracked mode), `-S` (gpg keyid) all
        # greedily consume the rest-of-cluster as THEIR value — git parses `-Cm` as `-C m`, `-um` as
        # untracked mode "m", `-Sm` as keyid "m": a trailing `m`/`F` is that value, NOT a `-m`/`-F`
        # message (agent-tools#109 review finding #2). So de-cluster must return None for them.
        for tok in ("-Cm", "-cm", "-tm", "-um", "-Sm", "-aCm", "-aSF"):
            self.assertIsNone(mod._decluster_short_message_flag(tok), tok)

    # --- the message extractor de-clusters --------------------------------------------------------
    def test_dash_am_next_token_message_is_extracted(self):
        # THE BUG: `-am "Closes #5 fix"` → the message must be read from the NEXT token.
        msg = mod.commit_message_from_argv(_commit_argv('git commit -am "Closes #5 fix"'))
        self.assertIn("Closes #5", msg)

    def test_dash_am_glued_message_is_extracted(self):
        self.assertIn("ENG-7", mod.commit_message_from_argv(_commit_argv("git commit -amENG-7")))

    def test_dash_amF_is_a_message_not_a_file(self):
        # `-amF` is message "F" (git glues F onto -m), NOT a `-F` file read → no file open attempt.
        self.assertEqual(mod.commit_message_from_argv(_commit_argv("git commit -amF")), "F")

    def test_dash_aF_next_token_reads_the_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: x\n\nCloses #77\n")
            path = fh.name
        try:
            self.assertIn(
                "Closes #77", mod.commit_message_from_argv(_commit_argv(f"git commit -aF {path}"))
            )
        finally:
            os.unlink(path)

    def test_dash_aF_glued_reads_the_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: x\n\nFixes ABC-12\n")
            path = fh.name
        try:
            self.assertIn(
                "Fixes ABC-12", mod.commit_message_from_argv(_commit_argv(f"git commit -aF{path}"))
            )
        finally:
            os.unlink(path)

    # --- stdin-sentinel detection through a cluster ----------------------------------------------
    def test_dash_aF_dash_is_stdin(self):
        # `-aF -` is the stdin sentinel reached via a cluster — detected, contributes no text.
        argv = _commit_argv("git commit -aF -")
        self.assertTrue(mod.commit_reads_stdin_message(argv))
        self.assertEqual(mod.commit_message_from_argv(argv).strip(), "")

    def test_dash_am_is_not_stdin(self):
        # `-am -` is a MESSAGE whose value happens to be "-", NOT a stdin file read.
        self.assertFalse(mod.commit_reads_stdin_message(_commit_argv("git commit -am -")))

    def test_dash_am_dash_F_dash_message_value_is_not_stdin(self):
        # PARITY (agent-tools#109 review finding #1): `git commit -am -F -` → git reads `-m "-F"`
        # (message is the literal "-F"), then `-` is a pathspec; stdin is NOT read. The message
        # reader and the file-value reader must AGREE — the consumed `-F` token must not be
        # re-parsed by _file_flag_values as a real `-F` flag (which would falsely flag stdin).
        for command in ("git commit -am -F -", "git commit -m -F -"):
            argv = _commit_argv(command)
            self.assertEqual(mod.commit_message_from_argv(argv), "-F", command)
            self.assertFalse(mod.commit_reads_stdin_message(argv), command)

    def test_long_message_dash_F_dash_value_is_not_stdin(self):
        # PARITY for the LONG form (agent-tools#109 re-review #1): `git commit --message -F -` → git
        # reads `--message "-F"`, then `-` is a pathspec; NOT stdin. _file_flag_values must skip the
        # separate `--message` value so the `-F` token is not re-parsed as a real file flag.
        argv = _commit_argv("git commit --message -F -")
        self.assertEqual(mod.commit_message_from_argv(argv), "-F")
        self.assertFalse(mod.commit_reads_stdin_message(argv))

    def test_reuse_template_flag_value_is_not_a_message_or_stdin(self):
        # agent-tools#109 re-review #2: a SEPARATE `-C`/`-c`/`-t` (and long forms) takes its next
        # token as a MANDATORY value — git parses `-C -m x` as `-C` reuse-ref "-m" (NO message), and
        # `-t -F -` as `-t` template "-F" (NOT a `-F -` stdin read). Both parsers must skip the value.
        for command in ("git commit -C -m x", "git commit -c -m x", "git commit --template -F -"):
            argv = _commit_argv(command)
            self.assertEqual(mod.commit_message_from_argv(argv), "", command)
        self.assertFalse(mod.commit_reads_stdin_message(_commit_argv("git commit -t -F -")))
        self.assertFalse(mod.commit_reads_stdin_message(_commit_argv("git commit -aC -F -")))

    def test_value_bearing_long_flag_value_is_not_a_message_or_stdin(self):
        # agent-tools#114: --author/--date/--cleanup/--trailer/--pathspec-from-file each take a
        # MANDATORY next-token value (git-parseopt consumes it even when it starts with `-`). THE
        # BYPASS: `git commit --author '-m' 'ABC-1: ok'` — real git reads author="-m" and "ABC-1: ok"
        # as a PATHSPEC (ticketless), but the old parser de-clustered the value "-m" into a message
        # flag and ate the pathspec → phantom ticket. Both parsers must skip the flag AND its value.
        for flag in ("--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file"):
            argv = _commit_argv(f"git commit {flag} '-m' 'ABC-1: ok'")
            self.assertEqual(mod.commit_message_from_argv(argv), "", flag)
            self.assertTrue(mod._nonmessage_flag_consumes_next(flag), flag)
            # the `-F`-looking value must NOT be re-read as a real `-F -` stdin read either.
            self.assertFalse(
                mod.commit_reads_stdin_message(_commit_argv(f"git commit {flag} '-F' -")), flag
            )
        # a GLUED `--author=…`/`--cleanup=…` carries its own value → does NOT consume the next token.
        self.assertFalse(mod._nonmessage_flag_consumes_next("--author=someone"))
        self.assertEqual(
            mod.commit_message_from_argv(_commit_argv("git commit --author=x -m 'Closes #1'")),
            "Closes #1",
        )

    def test_optional_arg_flag_does_not_swallow_a_following_message(self):
        # `-S` (gpg keyid) and `-u` (untracked mode) have an OPTIONAL, ONLY-glued value — a SEPARATE
        # `-S -m …` / `-u -m …` is the flag with no value then a REAL `-m`, so the message must still
        # be read (they must NOT consume the next token). The reason _OTHER_VALUE_LETTERS keeps u/S
        # out of the next-token-consuming set.
        for command in ("git commit -S -m 'Closes #5'", "git commit -u -m 'Fixes ABC-7'"):
            self.assertTrue(
                mod.has_ticket_reference(mod.commit_message_from_argv(_commit_argv(command))), command
            )

    def test_trailing_message_flag_with_no_value_does_not_warn_or_crash(self):
        # a degenerate trailing `-aF`/`-F`/`-am` with no following value (an agent typo): the reader
        # must contribute no text and NOT call _read_message_file("") (agent-tools#109 review #3).
        for command in ("git commit -aF", "git commit -F", "git commit -am"):
            argv = _commit_argv(command)
            self.assertEqual(mod.commit_message_from_argv(argv), "", command)
            self.assertEqual(mod._file_flag_values(argv), [], command)

    # --- skip-flag scan de-clusters (a value can't be misread as a real flag) --------------------
    def test_skip_scan_declusters_amF_value(self):
        # `-amF "--amend"` is message "F" then a positional "--amend" pathspec — the value letter is
        # glued (F), so the cluster does NOT consume the next token; but the `--amend` is a positional
        # arg of a real commit, not a SKIP flag, because the message is "F" (no `--` seen, it's argv).
        # The important contract: `-am "--amend"` (next-token value) must NOT count as a skip flag.
        self.assertFalse(mod.is_skip_commit(_commit_argv('git commit -am "--amend"')))
        self.assertFalse(mod.is_skip_commit(_commit_argv('git commit -aF "--skip"')))

    def test_skip_scan_skips_value_bearing_long_flag_value(self):
        # agent-tools#114 finding 2: is_skip_commit's `_takes_following_message_value` used to skip
        # ONLY message/file values, not the non-message value-consumers — an asymmetry vs the other
        # two parsers. So a value-bearing long flag whose VALUE looks like a skip flag
        # (`--author '--amend'`) could be misread as a real `--amend`. It must be skipped as a value.
        self.assertFalse(mod.is_skip_commit(_commit_argv("git commit --author '--amend' -m x")))
        self.assertFalse(mod.is_skip_commit(_commit_argv("git commit --trailer '--amend'")))
        self.assertFalse(mod.is_skip_commit(_commit_argv("git commit -C '--amend' -m x")))
        # control: a REAL --amend is still detected as a skip commit.
        self.assertTrue(mod.is_skip_commit(_commit_argv("git commit --amend -m x")))

    def test_glued_short_message_ending_in_value_letter_is_not_a_next_token_consumer(self):
        # review of #114: `_takes_following_message_value` must judge a message/file token ENTIRELY by
        # its de-cluster result and never fall through to `_nonmessage_flag_consumes_next` — else a
        # glued message ending in `c`/`C`/`t` (`-amFixIt`) could be misread as a next-token consumer,
        # making `is_skip_commit` eat the following `--amend` and miss a real amend.
        for tok in ("-amFixIt", "-amC", "-amc", "-amt", "-aFpath", "-mC"):
            self.assertFalse(mod._takes_following_message_value(tok), tok)
        # the exact reviewer repro: a real --amend after a glued -am message IS still a skip commit.
        self.assertTrue(mod.is_skip_commit(["-amFixIt", "--amend"]))
        self.assertTrue(mod.is_skip_commit(["-amC", "--amend"]))

    # --- end-to-end, the exact repro from issue #109 ---------------------------------------------
    def test_e2e_am_with_ticket_allows(self):
        code, decision = run_hook('git commit -am "Closes #5 fix"', strict=True)
        self.assertEqual(code, 0, "a ticketed -am commit must be allowed")
        self.assertEqual(decision, "allow")

    def test_e2e_am_without_ticket_blocks(self):
        code, decision = run_hook('git commit -am "no ticket"', strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_e2e_am_chore_exemption_is_preserved(self):
        # the second half of the bug: an empty message lost the `chore:` exemption too.
        code, decision = run_hook('git commit -am "chore: cleanup"', strict=True)
        self.assertEqual(code, 0, "a chore: -am commit is exempt and must be allowed")
        self.assertEqual(decision, "allow")

    def test_e2e_am_skip_trailer_no_longer_bypasses(self):
        code, decision = run_hook(
            'git commit -am "no ticket [skip-ticket: deliberate]"', strict=True
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_e2e_am_inline_skip_env_no_longer_bypasses(self):
        code, decision = run_hook(
            'REQUIRE_TICKET_SKIP=1 git commit -am "no ticket"', strict=True
        )
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")

    def test_e2e_benign_clustered_flag_non_commit_passes(self):
        # the #37/#40 over-block cautionary tale: a benign command with a clustered short flag that
        # is NOT a git commit must still pass (no false BLOCK from over-eager de-clustering).
        for cmd in ("tar -amF archive.tar", "ls -la", 'grep -rn "git commit -am" src/'):
            code, decision = run_hook(cmd, strict=True)
            self.assertEqual(code, 0, cmd)
            self.assertEqual(decision, "allow", cmd)


class AbbreviatedLongFlagPrefix(unittest.TestCase):
    """agent-tools#120 — git resolves any UNAMBIGUOUS PREFIX of a long option (`--au` → `--author`,
    `--clea` → `--cleanup`, `--reu` → `--reuse-message`), and REJECTS an ambiguous prefix that matches
    >1 option (`--t` could be --trailer/--template; `--d` --date/--dry-run; `--f` --file/--fixup). #114
    added the value-bearing long flags to the value-consuming set but by EXACT string, so an
    abbreviated value flag was NOT recognized → its value de-clustered into a `-m` message and ate the
    next token → the same value-misattribution / TICKET-GATE BYPASS came back via the short form.

    These reproduce that bypass FIRST (they fail against the exact-match parser, which lets
    `git commit --au '-m' 'ABC-1: ok'` be misparsed as a `-m` message carrying a ticket) and pass once
    the parser resolves the prefix the way git does. The exact-prefix resolutions and the ambiguity
    boundary are VERIFIED against real git (`git commit --au … `, `--t` ambiguous, etc.)."""

    # --- the resolver itself: exact, unambiguous-prefix, ambiguous, unknown ----------------------
    def test_resolve_exact_match(self):
        self.assertEqual(mod._resolve_long_option("--author"), "--author")
        self.assertEqual(mod._resolve_long_option("--message"), "--message")

    def test_resolve_unambiguous_prefix(self):
        # verified against real git: each abbreviation resolves to exactly one option.
        self.assertEqual(mod._resolve_long_option("--au"), "--author")
        self.assertEqual(mod._resolve_long_option("--clea"), "--cleanup")
        self.assertEqual(mod._resolve_long_option("--reu"), "--reuse-message")
        self.assertEqual(mod._resolve_long_option("--ree"), "--reedit-message")
        self.assertEqual(mod._resolve_long_option("--fix"), "--fixup")
        self.assertEqual(mod._resolve_long_option("--dat"), "--date")
        self.assertEqual(mod._resolve_long_option("--templ"), "--template")
        self.assertEqual(mod._resolve_long_option("--mess"), "--message")
        self.assertEqual(mod._resolve_long_option("--am"), "--amend")
        self.assertEqual(mod._resolve_long_option("--pathspec-fr"), "--pathspec-from-file")

    def test_resolve_ambiguous_prefix_is_none(self):
        # git ERRORS on these (matches >1 option) and never commits — so the parser must NOT guess one.
        for amb in ("--t", "--d", "--f", "--a", "--pathspec-f", "--s"):
            self.assertIsNone(mod._resolve_long_option(amb), amb)

    def test_resolve_unknown_and_degenerate_is_none(self):
        for bad in ("--zzz", "--", "-m", "feat", "-", "--xy"):
            self.assertIsNone(mod._resolve_long_option(bad), bad)

    def test_canonical_value_flag_resolves_value_consumers_only(self):
        # an abbreviation of a value-consuming option resolves; a non-value option (`--am`/`--ver`) does not.
        self.assertEqual(mod._canonical_long_value_flag("--au"), "--author")
        self.assertEqual(mod._canonical_long_value_flag("--mess"), "--message")
        self.assertEqual(mod._canonical_long_value_flag("--fil"), "--file")
        self.assertIsNone(mod._canonical_long_value_flag("--am"))     # --amend, not value-consuming
        self.assertIsNone(mod._canonical_long_value_flag("--ver"))    # --verbose, not value-consuming
        self.assertIsNone(mod._canonical_long_value_flag("--author=x"))  # glued → caller handles

    # --- the THREE parsers skip an abbreviated value flag + its value (bypass closed) ------------
    def test_nonmessage_consumes_next_for_abbreviations(self):
        for flag in ("--au", "--dat", "--clea", "--trail", "--reu", "--ree", "--squ", "--templ"):
            self.assertTrue(mod._nonmessage_flag_consumes_next(flag), flag)
        # ambiguous / non-value abbreviations must NOT consume.
        for flag in ("--t", "--d", "--am", "--ver", "--author=x"):
            self.assertFalse(mod._nonmessage_flag_consumes_next(flag), flag)

    def test_abbreviated_value_flag_value_is_not_a_message(self):
        # THE BYPASS: `--au '-m' 'ABC-1: ok'` — real git reads author="-m" and "ABC-1: ok" as a
        # PATHSPEC (ticketless); the abbreviation must be recognized so the `-m` value is skipped, not
        # de-clustered into a message that eats the (phantom-ticket) pathspec.
        for flag in ("--au", "--dat", "--clea", "--trail", "--reu", "--ree", "--squ", "--templ"):
            argv = _commit_argv(f"git commit {flag} '-m' 'ABC-1: ok'")
            self.assertEqual(mod.commit_message_from_argv(argv), "", flag)
            # the `-F`-looking value must NOT be re-read as a real `-F -` stdin read either.
            self.assertFalse(
                mod.commit_reads_stdin_message(_commit_argv(f"git commit {flag} '-F' -")), flag
            )

    def test_is_skip_flag_exact_and_abbreviation(self):
        # `_is_skip_flag` resolves only `--amend` by prefix (the one SKIP_FLAG that is a real `git
        # commit` option); the rebase/merge plumbing flags match by EXACT string only — their
        # abbreviations are NOT `git commit` options, so they must return False (not silently resolve).
        for exact in ("--amend", "--continue", "--abort", "--skip"):
            self.assertTrue(mod._is_skip_flag(exact), exact)
        self.assertTrue(mod._is_skip_flag("--am"))  # --am → --amend, a real git commit option
        for not_a_skip in ("--con", "--abo", "--ski", "--a", "--au", "--m", "--message"):
            self.assertFalse(mod._is_skip_flag(not_a_skip), not_a_skip)

    def test_is_skip_commit_skips_abbreviated_value_flag_value(self):
        # a value-bearing abbreviation whose VALUE looks like a skip flag (`--au '--amend'`) must be
        # skipped as a value, not misread as a real `--amend`.
        self.assertFalse(mod.is_skip_commit(_commit_argv("git commit --au '--amend' -m x")))
        self.assertFalse(mod.is_skip_commit(_commit_argv("git commit --trail '--amend'")))
        # control: a real --amend (and its own abbreviation `--am`) is still a skip commit.
        self.assertTrue(mod.is_skip_commit(_commit_argv("git commit --amend -m x")))
        self.assertTrue(mod.is_skip_commit(_commit_argv("git commit --am -m x")))

    # --- abbreviated MESSAGE/FILE flags still get their value READ (no over-block) ---------------
    def test_abbreviated_message_flag_value_is_read(self):
        # `--mess 'Closes #1'` / `--mess=ENG-7` must be read as a message (else a legit abbreviated
        # ticketed commit would FALSE-BLOCK — the over-block direction).
        self.assertIn("Closes #1", mod.commit_message_from_argv(_commit_argv("git commit --mess 'Closes #1'")))
        self.assertIn("ENG-7", mod.commit_message_from_argv(_commit_argv("git commit --mess=ENG-7")))

    def test_abbreviated_file_flag_value_is_read_and_in_parity(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("feat: x\n\nCloses #88\n")
            path = fh.name
        try:
            for spelling in (f"--fil {path}", f"--fil={path}"):
                argv = _commit_argv(f"git commit {spelling}")
                self.assertIn("Closes #88", mod.commit_message_from_argv(argv), spelling)
                self.assertFalse(mod.commit_reads_stdin_message(argv), spelling)
        finally:
            os.unlink(path)
        # `--fil -` (abbreviated stdin) must be detected as stdin, contribute no text.
        argv = _commit_argv("git commit --fil -")
        self.assertEqual(mod.commit_message_from_argv(argv).strip(), "")
        self.assertTrue(mod.commit_reads_stdin_message(argv))

    # --- end-to-end: the bypass is closed, the legit path allows, ambiguity is benign ------------
    def test_e2e_abbreviated_author_dash_m_pathspec_blocks(self):
        # THE BYPASS, end-to-end: `git commit --au '-m' 'ABC-1: ok'` is a ticketless commit (the
        # ABC-1 token is a pathspec to git, not a message) → must BLOCK, not be fooled into ALLOW.
        code, decision = run_hook("git commit --au '-m' 'ABC-1: ok'", strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE, "abbreviated --au bypass must be closed")
        self.assertEqual(decision, "block")

    def test_e2e_abbreviated_value_flags_no_ticket_block(self):
        for flag in ("--clea", "--reu", "--dat", "--trail", "--squ", "--templ"):
            code, decision = run_hook(f"git commit {flag} '-m' 'ABC-1: ok'", strict=True)
            self.assertEqual(code, mod.BLOCK_EXIT_CODE, flag)
            self.assertEqual(decision, "block", flag)

    def test_e2e_legit_abbreviated_author_with_real_ticket_allows(self):
        # `--au 'someone' -m 'Closes #1'` is a properly-ticketed commit (author is "someone", the real
        # message carries the ticket) → must ALLOW (no over-block).
        code, decision = run_hook("git commit --au 'someone' -m 'Closes #1'", strict=True)
        self.assertEqual(code, 0, "a legit ticketed abbreviated-flag commit must allow")
        self.assertEqual(decision, "allow")

    def test_e2e_abbreviated_message_flag_with_ticket_allows(self):
        code, decision = run_hook("git commit --mess 'feat: x Closes #2'", strict=True)
        self.assertEqual(code, 0)
        self.assertEqual(decision, "allow")

    def test_glued_abbreviated_value_flag_does_not_consume_next_or_leak(self):
        # a GLUED abbreviated non-message value-consumer (`--au=name`) carries its own value → it must
        # NOT consume the next token, and its value must NOT leak into the commit message.
        self.assertFalse(mod._nonmessage_flag_consumes_next("--au=someone"))
        self.assertIsNone(mod._canonical_long_value_flag("--au=someone"))
        # `--au=name -m 'Closes #1'`: author is glued, the real `-m` message carries the ticket.
        self.assertEqual(
            mod.commit_message_from_argv(_commit_argv("git commit --au=name -m 'Closes #1'")),
            "Closes #1",
        )

    def test_import_time_guard_rejects_unknown_value_long(self):
        # the import-time invariant (_KNOWN_VALUE_LONG <= _ALL_COMMIT_LONG_OPTIONS) must actually fire
        # on a typo'd value-consuming option — re-execute the module source with a poisoned set.
        import types

        src = _SCRIPT.read_text(encoding="utf-8").replace(
            '"--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file",',
            '"--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file", "--bogus-flag",',
            1,
        )
        self.assertIn("--bogus-flag", src, "test setup: the value-long set line must have been patched")
        ns = types.ModuleType("rt_poisoned")
        ns.__dict__["__file__"] = str(_SCRIPT)
        with self.assertRaises(RuntimeError):
            exec(compile(src, str(_SCRIPT), "exec"), ns.__dict__)

    def test_e2e_ambiguous_prefix_does_not_overconsume(self):
        # `--t 'Closes #9'` is an AMBIGUOUS option to git (--trailer/--template) — git would error and
        # never commit. Our parser must NOT silently resolve `--t` to a value-consumer and swallow the
        # 'Closes #9' as its value; the token is just an unrecognized flag, the next is a pathspec, no
        # message → no ticket → BLOCK (the SAFE direction: we don't manufacture a phantom ticket from
        # an over-consumed value, and we don't crash). This proves ambiguity is handled per git.
        code, decision = run_hook("git commit --t 'Closes #9'", strict=True)
        self.assertEqual(code, mod.BLOCK_EXIT_CODE)
        self.assertEqual(decision, "block")


_SESSION_DIR = "2026-10-24T10-11-12-345Z_0a1b2c3d-1111-2222-3333-444455556666"
_SUBAGENT_ENV = {
    "RIG_AGENT_ID": "Kid",
    "PI_SESSION_FILE": f"/home/u/.omp/sessions/proj/{_SESSION_DIR}/Kid.jsonl",
}
_COORDINATOR_ENV = {"PI_SESSION_FILE": f"/home/u/.omp/sessions/proj/{_SESSION_DIR}.jsonl"}
_BLOCKED_COMMANDS = {
    "plain": 'git commit -m "feat: add export"',
    "stdin": "git commit -F -",
}


class BlockMessageEscalationLadder(unittest.TestCase):
    """agent-tools#994: a block must say WHAT TO DO NEXT, and the ladder differs by WHO reads it.

    Incident 2026-10-24: a subagent hit this block, never ran `task new`, and yielded "blocked at
    commit" because the text said "ASK the human" and showed `Closes #123`.
    """

    def _block(self, kind: str, env: dict | None = None) -> str:
        out = _run_hook_raw(_BLOCKED_COMMANDS[kind], env_extra=env)
        self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE)
        self.assertEqual(out["decision"], "block")
        return out["message"]

    def test_marker_leads_and_self_service_remedy_is_first_step(self):
        for kind in _BLOCKED_COMMANDS:
            for env in (None, _SUBAGENT_ENV, _COORDINATOR_ENV):
                msg = self._block(kind, env)
                self.assertTrue(msg.startswith(mod.BLOCK_MARKER), (kind, env, msg))
                after_marker = msg[len(mod.BLOCK_MARKER):]
                step1 = after_marker[after_marker.index("1. "):after_marker.index("2. ")]
                self.assertIn("task new --title", step1)
                for flag in ("--acceptance", "--why", "--impact", "--if-not-done"):
                    self.assertIn(flag, step1)
                self.assertIn("Refs #<id>", step1)
                self.assertRegex(step1, r"(?i)do not stop")

    def test_never_suggests_closing_keywords_or_asking_the_human_first(self):
        for kind in _BLOCKED_COMMANDS:
            for env in (None, _SUBAGENT_ENV, _COORDINATOR_ENV):
                msg = self._block(kind, env)
                self.assertNotIn("Closes", msg)
                self.assertNotIn("Fixes", msg)
                self.assertNotIn("ASK the human", msg)
                self.assertNotIn("No self-service bypass", msg)

    def test_subagent_messages_parent_and_never_sees_the_human_hatch(self):
        for kind in _BLOCKED_COMMANDS:
            msg = self._block(kind, _SUBAGENT_ENV)
            self.assertIn("write agent://Main", msg)
            self.assertIn("`yield` status `awaiting-coordinator` (NOT blocked/done/failed)", msg)
            self.assertIn("`write agent://Kid`", msg)  # the coordinator revives the subagent by id
            self.assertIn("never retry with REQUIRE_TICKET_STRICT=0 or RIG_HATCH_REQUEST_*", msg)
            # the concrete hatch line is coordinator-only
            self.assertNotIn("RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT", msg)
            self.assertNotIn("`wait`", msg)

    def test_subagent_parent_override(self):
        msg = self._block("plain", {**_SUBAGENT_ENV, "RIG_PARENT_AGENT_ID": "Boss"})
        self.assertIn("write agent://Boss", msg)

    def test_coordinator_relays_and_hatch_is_the_last_line(self):
        for kind in _BLOCKED_COMMANDS:
            msg = self._block(kind, _COORDINATOR_ENV)
            self.assertIn("write agent://<id>", msg)
            self.assertIn("`wait` for its final report", msg)
            last = msg.rstrip().splitlines()[-1]
            self.assertIn("RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT", last)
            self.assertRegex(last, r"(?i)trivial")
            self.assertEqual(msg.count("RIG_HATCH_REQUEST"), 1)

    def test_unknown_role_has_both_branches_and_hatch_last(self):
        for kind in _BLOCKED_COMMANDS:
            msg = self._block(kind)
            self.assertIn("If you were dispatched by another agent", msg)
            self.assertIn("If you are the top-level agent", msg)
            self.assertIn("awaiting-coordinator", msg)
            self.assertIn("RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT", msg.rstrip().splitlines()[-1])

    def test_stdin_hint_still_explains_the_unreadable_message(self):
        msg = self._block("stdin")
        self.assertIn("-F -", msg)
        self.assertIn("stdin", msg)
        self.assertIn("-m", msg)

    def test_messages_stay_short(self):
        for kind in _BLOCKED_COMMANDS:
            for env in (None, _SUBAGENT_ENV, _COORDINATOR_ENV):
                self.assertLessEqual(len(self._block(kind, env)), 1300, (kind, env))

    def test_warn_only_advisory_has_remedy_but_no_marker_or_closing_keywords(self):
        for kind in _BLOCKED_COMMANDS:
            out = _run_hook_raw(_BLOCKED_COMMANDS[kind], env_extra={"REQUIRE_TICKET_STRICT": "0"})
            self.assertEqual(out["decision"], "allow")
            self.assertNotIn(mod.BLOCK_MARKER, out["message"])
            self.assertNotIn("Closes", out["message"])
            self.assertNotIn("Fixes", out["message"])
            self.assertNotIn("ASK the human", out["message"])
        plain = _run_hook_raw(_BLOCKED_COMMANDS["plain"], env_extra={"REQUIRE_TICKET_STRICT": "0"})
        self.assertIn("task new --title", plain["message"])

    def test_hatch_denied_path_carries_the_same_ladder(self):
        for kind in _BLOCKED_COMMANDS:
            with tempfile.TemporaryDirectory() as tmp:
                tg_ctl = _fake_tg_ctl(Path(tmp) / "tg-ctl", 'printf "no\n"\nexit 1\n')
                out = _run_hook_inproc(
                    _BLOCKED_COMMANDS[kind],
                    env_extra={_HATCH_ENV: "one-off", **_SUBAGENT_ENV},
                    tg_ctl=tg_ctl,
                )
            self.assertEqual(out["code"], mod.BLOCK_EXIT_CODE)
            self.assertIn("hatch escalation denied", out["message"].lower())
            self.assertIn(mod.BLOCK_MARKER, out["message"])
            self.assertIn("task new --title", out["message"])
            self.assertIn("write agent://Main", out["message"])
            self.assertNotIn("Closes", out["message"])

    def test_falls_back_to_neutral_message_when_lib_is_unavailable(self):
        with mock.patch.object(mod, "_guard_message", None):
            msg = mod._render_block(mod.BLOCK_MARKER + ".", "Run `task new --title x`.", "hatch line")
        self.assertTrue(msg.startswith(mod.BLOCK_MARKER))
        self.assertIn("task new --title", msg)
        self.assertIn("dispatched by another agent", msg)
        self.assertIn("awaiting-coordinator", msg)
        self.assertNotIn("Closes", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
