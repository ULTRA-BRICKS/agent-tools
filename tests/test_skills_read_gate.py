"""Tests for the skills-read-gate agent-hook (pre-bash).

Covers the doctrine's four cases. Hooks 4-5 have no subagent exemption; the third case is
instead the SATISFIED-MARKER path (every mandatory skill marker fresh => allow). So:
  BLOCK   — a work action (commit/build) with a missing mandatory skill, on a repeat.
  ALLOW   — a non-work command (nothing to gate), and the first-offense WARN.
  MARKER  — all mandatory markers fresh => allow even on a work action.
  ESCAPE  — env+reason and inline sentinel allow; reasonless still blocks.

Hermetic: both the invoked-markers dir and the warn/block tier dir are redirected into
tmp_path via env (and the module constants re-pointed), so nothing touches the real cache.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_skills_read_gate.py -q
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
    / "skills-read-gate"
    / "skills_read_gate.py"
)
_spec = importlib.util.spec_from_file_location("skills_read_gate", _HOOK)
assert _spec and _spec.loader
srg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srg)

_MANDATORY = "delegate-work-to-subagents,visual-proof-cycle"


def _run(command, monkeypatch, *, invoked: Path, tier: Path,
         env: dict | None = None, agent_id: str | None = None,
         session_id: str | None = None,
         mandatory: str = _MANDATORY) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    args: dict = {"command": command}
    if agent_id is not None:
        args["agent_id"] = agent_id  # forwarded by the bridge inside a dispatched subagent
    if session_id is not None:
        args["session_id"] = session_id  # forwarded by the bridge (T2 precedence)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo", "args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(srg, "INVOKED_DIR", invoked)
    monkeypatch.setattr(srg, "TIER_DIR", tier)
    monkeypatch.setenv("MANDATORY_SKILLS", mandatory)
    monkeypatch.delenv("RIG_HATCH_REQUEST_SKILLS_READ_GATE", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = srg.main()
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


def _touch_all_skills(invoked: Path, *, session_id: str | None = None) -> None:
    base = invoked / session_id if session_id else invoked
    base.mkdir(parents=True, exist_ok=True)
    for skill in _MANDATORY.split(","):
        (base / skill).write_text("x")


# ── BLOCK (missing skill, on repeat) ───────────────────────────────────────────────────

def test_block_commit_missing_skills_on_repeat(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    # first work action → WARN (allow + message)
    out1, _e1, c1 = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow"
    assert "not invoked" in json.loads(out1)["message"]
    # repeat in the same cwd → BLOCK
    out2, _e2, c2 = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_block_build_missing_skills_on_repeat(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("npm run build", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("npm run build", monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW (non-work command) ───────────────────────────────────────────────────────────

def test_allow_non_work_command(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git status", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


def test_allow_merge_continue_is_not_a_commit(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git commit --continue", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


# ── SATISFIED MARKER (the honest action) ───────────────────────────────────────────────

def test_allow_when_all_mandatory_skills_invoked(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked)
    # even on what would otherwise be a repeat, fresh markers satisfy the gate
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"
    out2, _e2, c2 = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c2 == 0 and _decision(out2) == "allow"


# ── SESSION SCOPING (no cross-session marker leak) ─────────────────────────────────────

def test_allow_when_markers_invoked_in_this_own_session(tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked, session_id="sess-a")
    out, _e, c = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-a",
    )
    assert c == 0 and _decision(out) == "allow"


def test_block_when_markers_invoked_only_in_a_different_session(tmp_path, monkeypatch):
    """The core regression this change fixes: session B's commit must NOT be satisfied by
    session A's marker just because both belong to the same user/machine."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked, session_id="sess-a")
    # session B, first offense → WARN (still allow, but the message names it missing)
    out1, _e1, c1 = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-b",
    )
    assert c1 == 0 and _decision(out1) == "allow"
    assert "not invoked" in json.loads(out1)["message"]
    # session B, repeat → BLOCK — session A's marker never counted for it
    out2, _e2, c2 = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-b",
    )
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_falls_back_to_global_marker_when_no_session_id_on_event(tmp_path, monkeypatch):
    """No session_id at all (e.g. a non-CC harness) → the pre-session-scoping global marker
    path is used, unchanged from before this feature existed."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked)  # global path, no session subdir
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


def test_invalid_session_id_falls_back_to_global_marker(tmp_path, monkeypatch):
    """A session_id containing `/` is rejected by `_sanitize_session_id` (never split/nested)
    and the gate falls back to the global marker path, same as if none was sent at all."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked)  # global path
    out, _e, c = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="a/b",
    )
    assert c == 0 and _decision(out) == "allow"


def test_global_marker_still_satisfies_a_session_scoped_check(tmp_path, monkeypatch):
    """A harness/workflow with no session-aware marker producer (Codex/opencode today have
    no pre-skill mapping — see agent-hooks/README.md; or a human's manual `touch` recipe
    from this hook's own README) can still only ever write the GLOBAL marker path. That
    must keep satisfying the gate even when the event carries a session id (Codex's own
    bridge forwards one on pre-bash too) — otherwise session-scoping silently breaks the
    only workaround those harnesses have."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked)  # global path only, no session subdir
    out, _e, c = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="codex-sess-1",
    )
    assert c == 0 and _decision(out) == "allow"


def test_global_fallback_is_intentional_even_alongside_a_different_sessions_marker(tmp_path, monkeypatch):
    """Pins the residual, DELIBERATE tradeoff explicitly (not a bug to silently 'fix' later):
    session B has invoked nothing itself, session A HAS invoked the mandatory skills (its own
    session-scoped markers exist), and additionally the GLOBAL marker is fresh (e.g. from a
    manual touch, or a pre-session-scoping producer). Session B's check still passes, because
    the global marker is a valid lower-precedence signal independent of what any OTHER
    session's own scoped markers say. This is the documented tradeoff (see `_missing_skills`'s
    docstring and this hook's own README, "The marker contract") that keeps a harness/manual
    workaround with no session-aware producer working — it is not the automatic, silent,
    per-invocation cross-session leak this whole change exists to close (that leak required NO
    manual action at all; this requires someone/something to have written the global marker)."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked, session_id="sess-a")
    _touch_all_skills(invoked)  # also fresh at the global path
    out, _e, c = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-b",
    )
    assert c == 0 and _decision(out) == "allow"


def test_non_string_session_id_on_read_side_is_ignored_not_crashed_on(tmp_path, monkeypatch):
    """Mirrors the writer-side non-string session_id test: a model/serialization glitch that
    puts a non-string value in args.session_id must fall back to the global path, not crash."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _touch_all_skills(invoked)  # global path
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"cwd": "/repo", "args": {"command": "git commit -m x", "session_id": 12345}})),
    )
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(srg, "INVOKED_DIR", invoked)
    monkeypatch.setattr(srg, "TIER_DIR", tier)
    monkeypatch.setenv("MANDATORY_SKILLS", _MANDATORY)
    monkeypatch.delenv("RIG_HATCH_REQUEST_SKILLS_READ_GATE", raising=False)
    code = srg.main()
    assert code == 0, err.getvalue()
    assert _decision(out.getvalue()) == "allow"


def test_tier_escalation_is_session_scoped_not_just_cwd(tmp_path, monkeypatch):
    """The core tiering-side regression this change closes: session A WARNing in a cwd must
    NOT push session B's first action in that SAME cwd straight to BLOCK — B gets its own
    WARN first, exactly like a brand-new cwd would."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    # session A: first offense WARNs, repeat BLOCKs (unaffected by this change)
    out_a1, _e, c_a1 = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-a",
    )
    assert c_a1 == 0 and _decision(out_a1) == "allow"
    out_a2, _e, c_a2 = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-a",
    )
    assert c_a2 == srg.BLOCK_EXIT_CODE and _decision(out_a2) == "block"

    # session B, same cwd: must still get its OWN first-offense WARN, not inherit A's BLOCK tier
    out_b1, _e, c_b1 = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-b",
    )
    assert c_b1 == 0 and _decision(out_b1) == "allow"
    out_b2, _e, c_b2 = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier, session_id="sess-b",
    )
    assert c_b2 == srg.BLOCK_EXIT_CODE and _decision(out_b2) == "block"


def test_marker_path_helper_nests_under_session_seg():
    assert srg._marker_path("delegate-work-to-subagents", None) == (
        srg.INVOKED_DIR / "delegate-work-to-subagents"
    )
    assert srg._marker_path("delegate-work-to-subagents", "sess-1") == (
        srg.INVOKED_DIR / "sess-1" / "delegate-work-to-subagents"
    )


@pytest.mark.parametrize(
    "bad", ["", "x" * 200, "a/b", "a\\b", ".", "..", "bad\x00id"],
)
def test_sanitize_session_id_rejects_unsafe_values(bad):
    assert srg._sanitize_session_id(bad) is None


def test_sanitize_session_id_accepts_a_plausible_session_id():
    assert srg._sanitize_session_id("  sess-1234-abcd  ") == "sess-1234-abcd"


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline sentinel) ───────

def test_old_env_escape_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """`ALLOW_SKIP_SKILLS=1` (+ _REASON) as a real process env must NO LONGER bypass the gate —
    on a repeat it still BLOCKs. The self-service override was removed for the Telegram hatch."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # prime warn marker
    out, _e, c = _run(
        "git commit -m x", monkeypatch, invoked=invoked, tier=tier,
        env={"ALLOW_SKIP_SKILLS": "1", "ALLOW_SKIP_SKILLS_REASON": "docs-only"},
    )
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_old_inline_sentinel_no_longer_bypasses(tmp_path, monkeypatch):
    """An inline `# skills-ok: <reason>` comment must NO LONGER bypass — the per-command
    sentinel is gone; on a repeat it still BLOCKs."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git commit -m x  # skills-ok: trivial bump",
                      monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── Telegram hatch escalation (RIG_HATCH_REQUEST_SKILLS_READ_GATE) ───────────────────────

def test_hatch_unset_blocks_and_names_env_var(tmp_path, monkeypatch):
    """No hatch requested → the normal WARN-then-BLOCK tier, and the block message names the
    hatch env var so an agent knows the only sanctioned escape."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_SKILLS_READ_GATE" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` (no written justification) is an invalid request → deny (block) on the FIRST
    action, regardless of tier, and NO tg-ctl is invoked (a never-callable would-ALLOW proves
    no Telegram round-trip)."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n")  # would ALLOW if ever called
    monkeypatch.setattr(srg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      env={"RIG_HATCH_REQUEST_SKILLS_READ_GATE": "1"})
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 0 (human approved) → allow, even on the first
    action and with no fresh markers."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    monkeypatch.setattr(srg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      env={"RIG_HATCH_REQUEST_SKILLS_READ_GATE": "docs-only, skills N/A"})
    assert c == 0 and _decision(out) == "allow"
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks_citing_denial(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 1 (human declined / timed out) → block leading
    with the denial reason."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(srg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      env={"RIG_HATCH_REQUEST_SKILLS_READ_GATE": "docs-only, skills N/A"})
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── B2: the GIT_COMMIT regex must NOT match `git`+`commit` in plain prose ────────────────

@pytest.mark.parametrize("command", [
    'echo "remember to git, then commit"',
    'echo "git status is fine; commit later"',
])
def test_git_commit_prose_is_not_a_work_action(command, tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    # even on a "repeat" it stays ALLOW because it is not a work action at all (no gating)
    _run(command, monkeypatch, invoked=invoked, tier=tier)
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


def test_git_with_global_flags_commit_is_still_gated(tmp_path, monkeypatch):
    """`git -C path commit` (global flag before subcommand) must still be recognised."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git -C /repo commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git -C /repo commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── B6: extra build runners (deno/mvn/gradle/rake/msbuild) are gated ─────────────────────

@pytest.mark.parametrize("command", [
    "deno test", "mvn verify", "gradle build", "rake test", "msbuild proj.sln /t:build",
])
def test_extra_build_runners_are_work_actions(command, tmp_path, monkeypatch):
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run(command, monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── #4: BUILD_OR_TEST must be anchored at a command head, not match inside a string ──────

def test_commit_message_mentioning_npm_test_is_a_commit_not_a_build(tmp_path, monkeypatch):
    """`git commit -m "fix: npm test was flaky"` must be gated via the COMMIT path (it IS a
    commit), not mis-classified as a build action by the `npm test` substring in the message."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    cmd = 'git commit -m "fix: npm test was flaky"'
    # the substring `npm test` lives inside the commit message → BUILD_OR_TEST must NOT fire on it
    assert not srg.BUILD_OR_TEST.search(cmd)
    # but it is a real commit, so the gate still fires (warn → block on repeat)
    out1, _e1, c1 = _run(cmd, monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(cmd, monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    'echo "see npm test output"',
    'echo "run yarn build later"',
    'grep -r "pytest" .',
])
def test_build_or_test_substring_in_a_string_is_not_a_work_action(command, tmp_path, monkeypatch):
    """A build/test word buried in a string argument (not at a command head) is not work."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run(command, monkeypatch, invoked=invoked, tier=tier)
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("command", ["npm test", "npm run build", "pytest -q"])
def test_real_build_at_command_head_still_blocks_on_repeat(command, tmp_path, monkeypatch):
    """A REAL build/test at the command head is still a work action (block-on-repeat path)."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run(command, monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── codex P2: a skip token in a COMMENT / MESSAGE must NOT exempt a real commit ──────────

@pytest.mark.parametrize("command", [
    "git commit -m x # --abort",                  # skip token in a trailing shell comment
    "git commit -m 'x' # leftover --skip note",   # comment after a quoted message
    "git commit -m 'support --skip in messages'",  # skip token inside the commit message
    "git commit -am 'fix --continue handling'",   # -am clusters; value carries --continue
    "git commit -am --skip",                      # -am clusters; the VALUE is literally `--skip`
    "git commit -aF --abort",                     # -aF clusters; the file-path value is `--abort`
    "git commit -- --skip",                       # `--skip` is a PATHSPEC after `--`, not a flag
    "git commit -m x -- --abort src/",            # pathspec named --abort after `--`
    "git rebase --abort && git commit -m x",      # skip flag on a SIBLING command, not the commit
    "git commit --continue && git commit --trailer --skip -m x",  # --trailer's VALUE leaks as --skip
])
def test_skip_token_in_comment_or_message_does_not_bypass(command, tmp_path, monkeypatch):
    """codex P2 bypass: ``SKIP_COMMIT`` used to match the RAW string, so a normal commit was
    mis-classified as a non-work skip action (and the mandatory-skills gate skipped) by putting
    ``--abort``/``--skip`` in a trailing comment or in the commit message. The skip exemption now
    derives from the PARSED argv, so these are real work actions → WARN then BLOCK on repeat."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out1, _e1, c1 = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_real_skip_flag_still_exempt_after_parsing(tmp_path, monkeypatch):
    """The fix must not over-block: a genuine ``git commit --abort`` (skip flag in the real
    argv, not a comment) is still a non-work action and allowed even on a repeat."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit --abort", monkeypatch, invoked=invoked, tier=tier)
    out, _e, c = _run("git commit --abort", monkeypatch, invoked=invoked, tier=tier)
    assert c == 0 and _decision(out) == "allow"


# ── agent-tools#174: a chain must not be exempted on its FIRST commit segment alone ───────

@pytest.mark.parametrize("command", [
    "git commit --continue && git commit -m x",
    "git commit --continue ; git commit -m x",
])
def test_skip_commit_followed_by_real_commit_is_not_exempt(command, tmp_path, monkeypatch):
    """Regression (agent-tools#174): a rebase-plumbing ``--continue`` chained with a SECOND,
    REAL commit used to exempt the WHOLE command from the gate — is_skip_commit returned on
    the FIRST commit segment found (the plumbing one) and never looked at the second,
    authoring commit. One real commit anywhere in the chain must force gating (WARN then
    BLOCK on repeat, same as any other real commit)."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out1, _e1, c1 = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(command, monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_two_skip_commits_chained_are_still_exempt(tmp_path, monkeypatch):
    """Inverse of the above: if EVERY commit segment in the chain carries a skip flag, the
    whole command really is just plumbing and stays exempt — not a work action at all, so it
    ALLOWs even on what would otherwise be a repeat."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit --continue && git commit --continue", monkeypatch, invoked=invoked, tier=tier)
    out, _e, c = _run(
        "git commit --continue && git commit --continue", monkeypatch, invoked=invoked, tier=tier,
    )
    assert c == 0 and _decision(out) == "allow"


# ── codex P2 (PR #197 review): --trailer's VALUE must not leak as a skip flag ─────────────

def test_is_skip_commit_direct_trailer_value_does_not_leak_as_skip_flag():
    """Direct, gate-plumbing-free pin: ``--trailer --skip`` puts the LITERAL string ``--skip``
    in argv as the trailer's VALUE (per ``git commit -h``, `--trailer <token>[(=|:)<value>]`
    consumes exactly one following token) — not a real skip flag. Before the fix,
    ``_takes_following_value`` didn't know ``--trailer`` takes a following value, so the value
    token leaked into ``_commit_flags``' output and ``any(tok in SKIP_FLAGS ...)`` wrongly
    matched it, exempting the whole chained command (including the second, real commit)."""
    assert srg.is_skip_commit(
        "git commit --continue && git commit --trailer --skip -m x"
    ) is False
    # the `=`-glued form was never actually exploitable (a single token never in SKIP_FLAGS),
    # but pin it stays correctly gated too, consistent with --message=/--file=.
    assert srg.is_skip_commit("git commit --trailer=foo -m x") is False


def test_trailer_value_leak_does_not_bypass_gate(tmp_path, monkeypatch):
    """Same bypass as above, through the full WARN→BLOCK gate plumbing: the second, REAL commit
    in the chain must not be exempted by the leaked ``--skip`` trailer value."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    cmd = "git commit --continue && git commit --trailer --skip -m x"
    out1, _e1, c1 = _run(cmd, monkeypatch, invoked=invoked, tier=tier)
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(cmd, monkeypatch, invoked=invoked, tier=tier)
    assert c2 == srg.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── SUBAGENT: exempt from the ORCHESTRATION-ONLY defaults, still gated on project skills ──

def test_subagent_exempt_from_orchestration_only_defaults(tmp_path, monkeypatch):
    """A dispatched subagent (agent_id present) IS the delegated work and has no UI to prove, so
    the two orchestration-only defaults (delegate-work-to-subagents, visual-proof-cycle) are N/A
    for it → dropped. With ONLY those two demanded, a subagent commit ALLOWS even on a repeat —
    no more forced ALLOW_SKIP_SKILLS overrides (issue #112). Mirrors orchestrator-stays-thin's
    agent_id detection."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    # first action (warn tier) then a repeat — for the orchestrator this would BLOCK on the repeat
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier, agent_id="sub-7")
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier, agent_id="sub-7")
    assert c == 0 and _decision(out) == "allow"


def test_subagent_exempt_on_build_command_too(tmp_path, monkeypatch):
    """The drop is shape-INDEPENDENT: it fires on the demanded-skill set, not the command kind. A
    subagent running a build/test command (not just a commit) is equally exempt from the two
    orchestration defaults — pin it so the exemption isn't silently commit-only."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("npm run build", monkeypatch, invoked=invoked, tier=tier, agent_id="sub-7")
    out, _e, c = _run("npm run build", monkeypatch, invoked=invoked, tier=tier, agent_id="sub-7")
    assert c == 0 and _decision(out) == "allow"


def test_orchestrator_still_gated_on_orchestration_defaults(tmp_path, monkeypatch):
    """The exemption is subagent-ONLY: the orchestrator (no agent_id) with no fresh markers still
    WARN→BLOCKs on the two defaults. The gate is not weakened for the main thread."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)  # warn
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_subagent_still_gated_on_project_specific_skill(tmp_path, monkeypatch):
    """The exemption drops ONLY the two orchestration/visual defaults — a PROJECT-specific
    mandatory skill (set via MANDATORY_SKILLS) still applies to a subagent, because a subagent
    doing work should also have read the project's real rules. So a subagent with an unmet
    project skill still WARN→BLOCKs."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    mandatory = "delegate-work-to-subagents,visual-proof-cycle,project-test-discipline"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
         agent_id="sub-7", mandatory=mandatory)  # warn
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      agent_id="sub-7", mandatory=mandatory)
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"
    # For a SUBAGENT the message names the project skill and — crucially — NEVER the two dropped
    # defaults ANYWHERE (the example tail is suppressed for subagents), so a whole-message check is
    # both correct and robust to any tail-wording change (no fragile substring slicing).
    msg = json.loads(out)["message"]
    assert "project-test-discipline" in msg
    assert "delegate-work-to-subagents" not in msg
    assert "visual-proof-cycle" not in msg


def test_explicit_listing_of_an_na_skill_is_still_dropped_for_subagent(tmp_path, monkeypatch):
    """README footgun, pinned: a project that EXPLICITLY sets MANDATORY_SKILLS to an orchestration/
    visual default (here just `visual-proof-cycle`) does NOT re-enable it for a subagent — it is
    dropped by NAME regardless of source. So a subagent with that as its only mandatory skill, no
    marker, ALLOWs even on a repeat (the demanded set is empty after the drop). Guards the
    documented behavior against a regression that started honoring an explicit listing."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
         agent_id="sub-7", mandatory="visual-proof-cycle")  # warn tier (would-be)
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      agent_id="sub-7", mandatory="visual-proof-cycle")
    assert c == 0 and _decision(out) == "allow"


def test_subagent_with_only_defaults_and_project_skill_met_allows(tmp_path, monkeypatch):
    """Completeness: a subagent whose ONLY unmet skills are the two orchestration defaults (its
    project skill marker IS fresh) ALLOWS — the defaults are dropped, nothing real is missing."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    mandatory = "delegate-work-to-subagents,visual-proof-cycle,project-test-discipline"
    invoked.mkdir(parents=True, exist_ok=True)
    (invoked / "project-test-discipline").write_text("x")  # the real skill is satisfied
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
         agent_id="sub-7", mandatory=mandatory)
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier,
                      agent_id="sub-7", mandatory=mandatory)
    assert c == 0 and _decision(out) == "allow"


def test_empty_agent_id_does_not_exempt(tmp_path, monkeypatch):
    """An EMPTY/whitespace agent_id is NOT a subagent — it must NOT exempt the orchestration
    defaults (a forged/blank signal can't win). With a blank agent_id the orchestrator gate
    applies and a repeat commit with no markers still BLOCKs."""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier, agent_id="   ")  # warn
    out, _e, c = _run("git commit -m x", monkeypatch, invoked=invoked, tier=tier, agent_id="   ")
    assert c == srg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_only_args_agent_id_is_read_not_other_surfaces(tmp_path, monkeypatch):
    """TRUST BOUNDARY regression guard. `_is_subagent` reads ONLY `args.agent_id` — the single
    surface the bridge sanitizes (T2 precedence: it drops any model/tool_input-supplied copy). This
    pins that a truthy agent_id sitting ANYWHERE ELSE does NOT exempt:
      - nested under `args.tool_input` (a model-controllable surface), and
      - at the event TOP LEVEL (the bridge never writes it there; orchestrator-stays-thin reads it,
        this gate deliberately does NOT — see _is_subagent).
    If a future edit widened the read to either surface, the orchestrator could self-exempt; this
    test would catch it. (It does NOT claim a forged `args.agent_id` is rejected — that value IS
    trusted; its non-forgeability is the bridge's job, verified in cc_hook_bridge's own tests.)"""
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    decoys = {"cwd": "/repo", "agent_id": "top-level-decoy",
              "args": {"command": "git commit -m x", "tool_input": {"agent_id": "nested-decoy"}}}
    out = None
    for _ in range(2):  # no real args.agent_id → orchestrator gate applies → repeat BLOCKs
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(decoys)))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        monkeypatch.setattr(srg, "INVOKED_DIR", invoked)
        monkeypatch.setattr(srg, "TIER_DIR", tier)
        monkeypatch.setenv("MANDATORY_SKILLS", _MANDATORY)
        # isolate from a developer/CI env that sets the hatch (else this would falsely allow).
        monkeypatch.delenv("RIG_HATCH_REQUEST_SKILLS_READ_GATE", raising=False)
        code = srg.main()
    assert code == srg.BLOCK_EXIT_CODE and _decision(out.getvalue()) == "block"


def test_every_default_is_dropped_for_subagents():
    """Invariant pinning the intent: EVERY default-mandatory skill is an orchestration/visual one
    that is N/A for a subagent, so all of them are in SUBAGENT_NA_SKILLS. If a future edit adds a
    third skill to DEFAULT_MANDATORY without adding it to SUBAGENT_NA_SKILLS, `defaults <= NA`
    breaks → this test fails, forcing the author to decide (drop it for subagents too, or
    deliberately keep it gated for subagents and update this invariant)."""
    defaults = {s.strip() for s in srg.DEFAULT_MANDATORY.split(",") if s.strip()}
    assert defaults <= srg.SUBAGENT_NA_SKILLS
    # and nothing extra is dropped that wasn't a default (the dropped set is exactly the defaults).
    assert srg.SUBAGENT_NA_SKILLS <= defaults


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl via the new `command=` contract. Regression for the documented inline form (Codex #233)."""
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl", f'cat > "{question}"\n' + _ALLOW_REPLY_SH
    )
    monkeypatch.setattr(srg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    invoked, tier = tmp_path / "inv", tmp_path / "tier"
    out, _e, c = _run(
        'RIG_HATCH_REQUEST_SKILLS_READ_GATE="docs-only commit, mandatory skills N/A" git commit -m x',
        monkeypatch, invoked=invoked, tier=tier)  # env NOT set — inline only
    assert c == 0 and _decision(out) == "allow"
    assert "docs-only commit, mandatory skills N/A" in question.read_text()
