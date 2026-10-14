"""Tests for the shared agent-hook Telegram hatch escalation helper."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import agenttools_hatch_escalation
from agenttools_hatch_escalation import hatch_env_var, request_hatch_approval

# The real `resolve_home` impl, captured before the autouse `_hermetic_home` fixture patches the
# module attribute — so the tests below can exercise the genuine OS-identity resolution + its
# fail-closed branch (not the monkeypatched stub every other test uses).
_REAL_RESOLVE_HOME = agenttools_hatch_escalation.resolve_home


def _write_tg_ctl(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol (it is the internal hook
# client documented as "reads a ButtonRequest JSON from stdin", not a generic ask-a-question CLI —
# see agenttools_hatch_escalation's own `_request_present_hatch_approval` for the full writeup of
# the real contract). A fake tg-ctl standing in for an "approved" answer must reply with the real
# hookSpecificOutput shape the helper actually parses (`decision.behavior == "allow"`) — printing
# arbitrary text and exiting 0 previously "worked" only because the helper wrongly treated ANY
# clean exit as approval, which is the exact bug this suite now guards against.
_ALLOW_REPLY_SH = 'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}\'\nexit 0\n'


def _allow_body(marker: Path | None = None) -> str:
    prefix = f"touch {marker}\n" if marker is not None else ""
    return prefix + _ALLOW_REPLY_SH


def _repo_with_tg_ctl(tmp_path: Path, tg_ctl: Path | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = ["agent_hooks:"]
    if tg_ctl is not None:
        lines.append(f'  tg_ctl_path: "{tg_ctl}"')
    (repo / "rig.yaml").write_text("\n".join(lines) + "\n")
    return repo


def _home_with_tg_ctl(
    tmp_path: Path, tg_ctl: Path | None = None, *, name: str = "home"
) -> Path:
    """A fake account home dir whose machine-global rig config (`.config/rig/config.yaml`) sets
    `tg_ctl.tg_ctl_path` to the fake tg-ctl. Tests monkeypatch `resolve_home` to return this — the
    only legitimate config source for the approval binary (the agent-controlled repo `cwd` must
    NOT be honored)."""
    home = tmp_path / name
    _write_rig_config(home, tg_ctl)
    return home


def _write_rig_config(home: Path, tg_ctl: Path | str | None) -> Path:
    config = home / ".config" / "rig" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    lines = ["version: 1", "tg_ctl:"]
    if tg_ctl is not None:
        lines.append(f'  tg_ctl_path: "{tg_ctl}"')
    config.write_text("\n".join(lines) + "\n")
    return config


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Point `resolve_home` at a clean home (no rig.yaml) for every test, so tg-ctl resolves via
    the test-provided candidates / trusted list and NEVER reads the real account home. Tests that
    exercise a home rig.yaml override re-monkeypatch `resolve_home` themselves (that wins)."""
    clean_home = tmp_path / "_clean_home"
    clean_home.mkdir()
    monkeypatch.setattr(
        agenttools_hatch_escalation, "resolve_home", lambda: str(clean_home)
    )


def test_env_unset_is_not_requested_and_does_not_contact_tg_ctl(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={},
        tg_ctl_candidates=[],
    )

    assert result.requested is False
    assert result.approved is False
    assert result.env_present is False
    assert result.should_stop is False
    assert "not set" in result.reason
    assert not marker.exists()


def test_whitespace_env_is_not_requested_and_does_not_contact_tg_ctl(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(repo),
        env={env_var: " \t\n "},
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.approved is False
    assert result.env_present is True
    assert result.should_stop is True
    assert "blank" in result.reason
    assert not marker.exists()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_bare_flag_env_is_rejected_without_tg_ctl_contact(tmp_path, value):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: value},
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is True
    assert result.approved is False
    assert result.env_present is True
    assert result.should_stop is True
    assert "written justification" in result.reason
    assert not marker.exists()



@pytest.mark.parametrize("value", ["pytest", "review"])
def test_single_token_is_not_a_written_justification(tmp_path, value):
    """A single token (the screenshot used `pytest`) is not a justification —
    deny without contacting Telegram."""
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("no-long-inline-process")

    result = request_hatch_approval(
        "no-long-inline-process",
        {"command": "pytest"},
        cwd=str(repo),
        env={env_var: value},
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is True
    assert result.approved is False
    assert result.env_present is True
    assert result.should_stop is True
    assert "written justification" in result.reason
    assert value in result.reason
    assert not marker.exists()


def test_real_justification_exit0_allows_and_includes_context(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"repo": str(repo), "target": "feat/x", "command": "git checkout feat/x"},
        cwd=str(repo),
        env={env_var: "Need to inspect the primary checkout before repairing it."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.requested is True
    assert result.approved is True
    assert "approved by tg-ctl ask" in result.reason
    question = question_file.read_text()
    assert "pin-primary-worktree" in question
    assert "Need to inspect the primary checkout before repairing it." in question
    assert "git checkout feat/x" in question
    assert result.tg_ctl_path == str(tg_ctl.resolve())


# --- hookId/operation grant fields (tg-cli#398/PR#400 late-approval grant protocol) ----------
#
# `tg-ctl`'s `isGrantableRequest` refuses to mint a late-approval grant unless the original
# ButtonRequest carried `hookId` plus a non-empty `operation.fingerprint`/`operation.cwd`. Every
# hatch ask sent by this library must include them, or a Telegram Approve tap arriving after the
# request's own window has already expired can never be bound back to it.


def test_button_request_carries_hook_id_and_operation_fields(tmp_path):
    capture_file = tmp_path / "captured.json"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'cat > "{capture_file}"\n' + _ALLOW_REPLY_SH,
    )
    repo = _repo_with_tg_ctl(tmp_path)
    hook_id = "pin-primary-worktree"
    env_var = hatch_env_var(hook_id)

    result = request_hatch_approval(
        hook_id,
        {"repo": str(repo), "command": "git checkout feat/x"},
        cwd=str(repo),
        env={env_var: "Need to inspect the primary checkout before repairing it."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    payload = json.loads(capture_file.read_text())
    assert payload["hookId"] == hook_id
    assert payload["operation"]["kind"] == hook_id
    assert payload["operation"]["cwd"] == str(repo)
    assert isinstance(payload["operation"]["fingerprint"], str)
    assert payload["operation"]["fingerprint"] != ""


def test_operation_fingerprint_is_deterministic_across_calls(tmp_path):
    """The SAME hook_id + context must fingerprint identically on two separate calls -- each
    hook invocation is a fresh Python process, so nothing but the inputs themselves can carry
    this identity between the original ask and a later retry of the same operation."""
    context = {"repo": "/tmp/repo", "command": "git checkout feat/x"}

    first = agenttools_hatch_escalation._operation_fingerprint("pin-primary-worktree", context)
    second = agenttools_hatch_escalation._operation_fingerprint(
        "pin-primary-worktree", {"command": "git checkout feat/x", "repo": "/tmp/repo"}
    )

    assert first == second


def test_operation_fingerprint_differs_for_different_context(tmp_path):
    """Two asks for the SAME hook at the SAME cwd but a DIFFERENT operation (a different `command`
    in context) must fingerprint differently -- this is what stops a late approval from ever
    being bound to the wrong operation once a pull/redemption flow consumes the fingerprint."""
    fp_a = agenttools_hatch_escalation._operation_fingerprint(
        "pin-primary-worktree", {"repo": "/tmp/repo", "command": "git checkout feat/x"}
    )
    fp_b = agenttools_hatch_escalation._operation_fingerprint(
        "pin-primary-worktree", {"repo": "/tmp/repo", "command": "git checkout feat/y"}
    )

    assert fp_a != fp_b


def test_question_renders_title_agent_branch_path_up_top_and_collapses_rest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature/redesign", str(repo)], check=True)
    # `git rev-parse --abbrev-ref HEAD` (what `_git_branch` runs) needs a resolvable HEAD commit
    # to succeed on this git version -- an empty repo with only a symbolic ref set by `init -b`
    # fails with "ambiguous argument 'HEAD'" rather than reporting the branch name.
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    branch = agenttools_hatch_escalation._git_branch(str(repo))
    question = agenttools_hatch_escalation._question(
        "block-reset-hard",
        "need to recover from a bad rebase",
        {"command": "git reset --hard HEAD~3", "kind": "hard"},
        str(repo),
        branch=branch,
        session_id="pid:1234",
    )
    assert question.startswith("<b>\U0001f513 Hatch approval request</b>")
    assert "<blockquote expandable>" in question
    head = question[: question.index("<blockquote expandable>")]
    assert "<b>Branch:</b> <code>feature/redesign</code>" in head
    assert "<b>Path:</b>" in head
    assert "<b>Hook:</b> <code>block-reset-hard</code>" in head
    assert "<b>Justification:</b> need to recover from a bad rebase" in head
    # The raw command is NOT up top -- it's inside the collapsed block only.
    assert "git reset --hard HEAD~3" not in head
    assert "git reset --hard HEAD~3" in question


def test_git_branch_falls_back_to_unknown_outside_a_repo(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert agenttools_hatch_escalation._git_branch(str(outside)) == "unknown"


def test_git_branch_falls_back_to_unknown_on_detached_head(tmp_path):
    """`rev-parse --abbrev-ref HEAD` prints the literal string "HEAD" for a detached HEAD
    rather than erroring -- pin that `_git_branch` maps that case to "unknown" too, per its
    own docstring, rather than leaking the literal string "HEAD" onto the approval card."""
    repo = tmp_path / "detached-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach", "HEAD"], check=True)
    assert agenttools_hatch_escalation._git_branch(str(repo)) == "unknown"


def test_question_escapes_html_metacharacters_in_dynamic_values(tmp_path):
    question = agenttools_hatch_escalation._question(
        "no-shell-file-edit",
        "the tag is <script> & needs review",
        {},
        str(tmp_path),
        branch="unknown",
        session_id="pid:1",
    )
    assert "<script>" not in question
    assert "&lt;script&gt;" in question
    assert "the tag is &lt;script&gt; &amp; needs review" in question


def test_question_with_no_extra_context_has_no_collapsed_block(tmp_path):
    question = agenttools_hatch_escalation._question(
        "subagent-no-monitor", "one-off check", {}, str(tmp_path), branch="main", session_id="pid:1",
    )
    assert "<blockquote expandable>" not in question


def test_real_justification_exit1_denies(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "exit 1\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "denied" in result.reason


def test_real_justification_unavailable_tg_ctl_denies(tmp_path):
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tmp_path / "missing" / "tg-ctl"],
        timeout_s=1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "not available" in result.reason


def test_real_justification_unexpected_exit_denies(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "exit 2\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "denied" in result.reason


def test_tg_ctl_timeout_closes_its_pipe_streams(monkeypatch):
    launched = []
    popen = subprocess.Popen

    def record_process(*args, **kwargs):
        proc = popen(*args, **kwargs)
        launched.append(proc)
        return proc

    monkeypatch.setattr(agenttools_hatch_escalation.subprocess, "Popen", record_process)
    outcome = agenttools_hatch_escalation._run_tg_ctl_ask(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        "{}",
        0.05,
        requested_timeout=0.05,
    )
    assert isinstance(outcome, str) and "timed out" in outcome
    assert len(launched) == 1
    proc = launched[0]
    assert proc.poll() is not None
    try:
        assert all(stream is not None and stream.closed for stream in (proc.stdin, proc.stdout, proc.stderr))
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def test_real_justification_timeout_denies(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "sleep 5\nexit 0\n")
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=0.2,
        process_margin_s=0.2,
    )

    assert result.requested is True
    assert result.approved is False
    assert "timed out" in result.reason


def test_critical_mode_ignores_max_tg_ctl_timeout_cap(tmp_path, monkeypatch):
    """agent-tools#754 AC#5: a `critical=True` call must not be capped by MAX_TG_CTL_TIMEOUT_S at
    all — not merely bounded to a bigger number. Patching the cap down to 0.2s and having the fake
    tg-ctl sleep past it (0.6s) before replying `allow` proves the cap is never consulted on the
    critical path: if `_bounded_timeout` were still in the loop anywhere, this call would time out
    exactly like the non-critical sibling test below does against the identical fake subprocess."""
    monkeypatch.setattr(agenttools_hatch_escalation, "MAX_TG_CTL_TIMEOUT_S", 0.2)
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "sleep 0.6\n" + _ALLOW_REPLY_SH)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        critical=True,
    )

    assert result.requested is True
    assert result.approved is True
    assert result.critical is True


def test_non_critical_mode_still_respects_max_tg_ctl_timeout_cap(tmp_path, monkeypatch):
    """Sibling to the test above, same fake subprocess and same patched cap: a NON-critical call
    (the default, every existing call site) must still deny at the (now tiny) MAX_TG_CTL_TIMEOUT_S
    bound — proving `critical=True` is what changed, not some accidental global relaxation of the
    cap for everyone."""
    monkeypatch.setattr(agenttools_hatch_escalation, "MAX_TG_CTL_TIMEOUT_S", 0.2)
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "sleep 0.6\n" + _ALLOW_REPLY_SH)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
        process_margin_s=0.1,
    )

    assert result.requested is True
    assert result.approved is False
    assert "timed out" in result.reason
    assert result.critical is False


def _ask(tmp_path: Path, tg_ctl_body: str):
    """Run one hatch request against a fake tg-ctl with the given shell body."""
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", tg_ctl_body)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    return request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )


# The deny branches below are THE point of the stdin-JSON contract: real `tg-ctl ask` exits 0
# no matter what happened (declined, timed out, daemon unreachable), so every "clean exit but no
# explicit allow" shape must deny. Each branch gets its own test so a refactor that re-adds an
# `exit 0 -> approve` shortcut cannot pass the suite.
def test_exit0_with_empty_stdout_denies(tmp_path):
    result = _ask(tmp_path, "exit 0\n")
    assert result.requested is True
    assert result.approved is False
    assert "no reply" in result.reason


def test_exit0_with_empty_stdout_and_stderr_detail_denies_with_detail(tmp_path):
    result = _ask(tmp_path, "echo 'daemon unreachable' >&2\nexit 0\n")
    assert result.approved is False
    assert "no reply" in result.reason and "daemon unreachable" in result.reason


# tg-cli#317: the client's `ask-forward …` outcome line is the deny reason, verbatim. Before,
# every empty-stdout outcome collapsed into "returned no reply (declined, timed out, or daemon
# unreachable)" — and a card the daemon had POSTED (messageId in its log) reached the agent as
# "not-posted".
def test_posted_then_expired_card_is_reported_as_expired_not_no_reply(tmp_path):
    line = ("[2026-09-05T19:48:25.229Z] ask-forward expired: req=hatch-x agent=claude pane=- "
            "messageId=14880 — posted to Telegram; no decision within 110000ms (daemon expiry, nobody tapped)")
    result = _ask(tmp_path, f"echo '{line}' >&2\nexit 0\n")
    assert result.approved is False
    assert result.reason.startswith("tg-ctl ask: ask-forward expired:")
    assert "messageId=14880" in result.reason and "posted to Telegram" in result.reason
    assert "no reply" not in result.reason and "not-posted" not in result.reason


def test_daemon_decline_reason_is_forwarded(tmp_path):
    line = "[2026-09-05T17:47:31.999Z] ask-forward not-posted: req=hatch-y agent=claude pane=- — daemon declined: reason=not-registered"
    result = _ask(tmp_path, f"echo '{line}' >&2\nexit 0\n")
    assert result.approved is False
    assert "daemon declined: reason=not-registered" in result.reason
    assert "no reply" not in result.reason


@pytest.mark.parametrize("kind", [
    "timed-out", "declined-after-post", "dropped", "unreachable", "multi-question not-completed",
    "some-future-kind",
])
def test_every_ask_forward_outcome_kind_is_forwarded(tmp_path, kind):
    result = _ask(tmp_path, f"echo '[t] ask-forward {kind}: req=hatch-k agent=claude pane=- — detail' >&2\nexit 0\n")
    assert result.reason == f"tg-ctl ask: ask-forward {kind}: req=hatch-k agent=claude pane=- — detail"


def test_last_ask_forward_line_wins_over_earlier_noise(tmp_path):
    body = ("echo '[t] ask reconnect: daemon socket dropped — resending' >&2\n"
            "echo '[t] ask-forward unreachable: req=hatch-z agent=claude pane=- — daemon socket closed or errored without a reply' >&2\n"
            "exit 0\n")
    result = _ask(tmp_path, body)
    assert result.reason == ("tg-ctl ask: ask-forward unreachable: req=hatch-z agent=claude pane=- — "
                             "daemon socket closed or errored without a reply")


def test_exit0_with_unparseable_stdout_denies(tmp_path):
    # The pre-fix fakes printed exactly this and were treated as approval.
    result = _ask(tmp_path, 'printf "approved\\n"\nexit 0\n')
    assert result.approved is False
    assert "unparseable" in result.reason and "approved" in result.reason


@pytest.mark.parametrize("behavior", ["deny", "ask", "Allow", ""])
def test_permission_request_reply_without_literal_allow_denies(tmp_path, behavior):
    reply = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": behavior}}}
    )
    result = _ask(tmp_path, f"printf '%s' '{reply}'\nexit 0\n")
    assert result.approved is False
    assert f"decision was {behavior!r}" in result.reason


def test_pretooluse_permission_decision_allow_approves(tmp_path):
    reply = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    )
    result = _ask(tmp_path, f"printf '%s' '{reply}'\nexit 0\n")
    assert result.approved is True
    assert "approved by tg-ctl ask" in result.reason


@pytest.mark.parametrize("decision", ["deny", "ask"])
def test_pretooluse_permission_decision_other_than_allow_denies(tmp_path, decision):
    reply = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}
    )
    result = _ask(tmp_path, f"printf '%s' '{reply}'\nexit 0\n")
    assert result.approved is False
    assert f"decision was {decision!r}" in result.reason


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # a top-level decision is NOT the hook reply shape — it must not be trusted
        ({"decision": "allow"}, None),
        ({"decision": {"behavior": "allow"}}, None),
        ({"hookSpecificOutput": "allow"}, None),
        ({"hookSpecificOutput": {}}, None),
        ({"hookSpecificOutput": {"decision": "allow"}}, None),
        ({"hookSpecificOutput": {"decision": {"behavior": 1}}}, None),
        ("allow", None),
        ([], None),
        ({"hookSpecificOutput": {"decision": {"behavior": "allow"}}}, "allow"),
        ({"hookSpecificOutput": {"permissionDecision": "deny"}}, "deny"),
        # the PreToolUse key wins when both are present — one reply never carries both, but the
        # precedence is pinned so a future dual-shape reply cannot flip between readings
        (
            {"hookSpecificOutput": {"permissionDecision": "deny", "decision": {"behavior": "allow"}}},
            "deny",
        ),
    ],
)
def test_parse_ask_decision_reads_only_the_two_hook_reply_shapes(reply, expected):
    assert agenttools_hatch_escalation._parse_ask_decision(reply) == expected


def test_path_shadowing_does_not_choose_fake_tg_ctl(tmp_path, monkeypatch):
    real_marker = tmp_path / "real-called"
    shadow_marker = tmp_path / "shadow-called"
    real = _write_tg_ctl(tmp_path / "real" / "tg-ctl", _allow_body(real_marker))
    trusted_link = tmp_path / "trusted" / "tg-ctl"
    trusted_link.parent.mkdir()
    trusted_link.symlink_to(real)
    shadow = _write_tg_ctl(
        tmp_path / "shadow" / "tg-ctl",
        f'touch {shadow_marker}\nprintf "shadow approved\\n"\nexit 0\n',
    )
    monkeypatch.setenv(
        "PATH", f"{shadow.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[trusted_link],
        timeout_s=1,
    )

    assert result.approved is True
    assert real_marker.exists()
    assert not shadow_marker.exists()
    assert shadow.exists()
    assert result.tg_ctl_path == str(real.resolve())


def test_home_rig_config_tg_ctl_path_override_is_used(tmp_path, monkeypatch):
    """`tg_ctl.tg_ctl_path` in the ACCOUNT HOME's machine-global rig config is the legitimate
    approval-binary config path and IS honored (home is not agent-controlled; resolve_home keys
    off the OS identity)."""
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "reviewed-config" / "tg-ctl", _allow_body(marker))
    home = _home_with_tg_ctl(tmp_path, tg_ctl)
    monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", lambda: str(home))
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(
            _repo_with_tg_ctl(tmp_path)
        ),  # the repo cwd carries NO override — it's ignored
        env={env_var: "Need to inspect the primary checkout before repairing it."},
        tg_ctl_candidates=[],
        timeout_s=1,
    )

    assert result.approved is True
    assert marker.exists()
    assert result.tg_ctl_path == str(tg_ctl.resolve())


def test_repo_local_rig_yaml_tg_ctl_path_is_ignored(tmp_path):
    """SECURITY (P1): a repo-local (agent-controlled `cwd`) rig.yaml pointing `tg_ctl_path` at an
    attacker's always-exit-0 binary must NOT yield approval — the approval binary is resolved ONLY
    from the account home (here the autouse clean home, which has no override). Without this a
    guarded agent could commit `rig.yaml` with `tg_ctl_path: /tmp/always-exit-0`, set the hatch
    env var, and self-approve — reopening the exact self-service bypass this gate closes."""
    marker = tmp_path / "evil-called"
    evil = _write_tg_ctl(
        tmp_path / "evil" / "tg-ctl", _allow_body(marker)
    )  # would approve
    repo = _repo_with_tg_ctl(
        tmp_path, evil
    )  # the repo cwd's rig.yaml points at the evil binary
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(repo),
        env={env_var: "attacker-supplied justification"},
        tg_ctl_candidates=[],  # no trusted binary reachable → must deny, not fall to the repo one
        timeout_s=1,
    )

    assert result.approved is False
    assert not marker.exists()  # the repo-local (attacker) binary was NEVER executed
    assert "not available" in result.reason


def test_default_tg_ctl_candidates_cover_every_supported_install_layout(tmp_path):
    """Defaults span macOS (both Homebrew prefixes), Linux (~/.local/bin, Linuxbrew, distro) and
    the per-user dirs; home-relative ones hang off the REAL home passed in, never `$HOME`."""
    home = tmp_path / "account"
    defaults = [Path(p) for p in agenttools_hatch_escalation._candidate_paths(None, str(home), [])]
    for expected in (
        home / ".local" / "bin" / "tg-ctl",
        home / "bin" / "tg-ctl",
        home / ".files" / "bin" / "tg-ctl",
        Path("/opt/homebrew/bin/tg-ctl"),
        Path("/usr/local/bin/tg-ctl"),
        Path("/home/linuxbrew/.linuxbrew/bin/tg-ctl"),
    ):
        assert expected in defaults
    assert all(path.is_absolute() for path in defaults)


_AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1] / "agent-hooks"


@pytest.mark.parametrize(
    "descriptor",
    [
        _AGENT_HOOKS_DIR
        / "block-devserver-primary"
        / "block-devserver-primary.pre-bash.json",
        _AGENT_HOOKS_DIR / "block-reset-hard" / "block-reset-hard.pre-bash.json",
        _AGENT_HOOKS_DIR
        / "pin-primary-worktree"
        / "pin-primary-worktree.pre-bash.json",
        _AGENT_HOOKS_DIR / "block-raw-pr-merge" / "block-raw-pr-merge.pre-bash.json",
        _AGENT_HOOKS_DIR / "pkill-guard" / "pkill-guard.pre-bash.json",
        _AGENT_HOOKS_DIR
        / "require-review-before-commit"
        / "require-review-before-commit.pre-bash.json",
        _AGENT_HOOKS_DIR
        / "decision-request-format"
        / "decision-request-format.pre-bash.json",
        _AGENT_HOOKS_DIR
        / "worktree-only-writes"
        / "worktree-only-writes.pre-write.json",
        _AGENT_HOOKS_DIR
        / "subagent-no-monitor"
        / "subagent-no-monitor.pre-monitor.json",
        _AGENT_HOOKS_DIR
        / "subagent-no-bg-longproc"
        / "subagent-no-bg-longproc.pre-bash.json",
    ],
)
def test_descriptor_timeout_strictly_exceeds_helper_worst_case(descriptor):
    """The descriptor's `timeout_ms` must strictly exceed the helper's worst-case wall time.

    The helper waits up to `MAX_TG_CTL_TIMEOUT_S` for tg-ctl plus `DEFAULT_PROCESS_MARGIN_S`
    before it kills the subprocess and returns its own deny. If the descriptor budget merely
    EQUALLED that worst case, a hung/unanswered tg-ctl could race the bridge's descriptor-timeout
    kill (which resolves via `on_error`, fail-OPEN for pin-primary-worktree) into a silent allow
    instead of the helper's intended deny (Codex PR #200 P2, tg#6554). A strict margin guarantees
    the helper resolves first. Regression guard: keep timeout_ms > (cap + margin) * 1000.
    """
    worst_case_ms = (
        agenttools_hatch_escalation.MAX_TG_CTL_TIMEOUT_S
        + agenttools_hatch_escalation.DEFAULT_PROCESS_MARGIN_S
    ) * 1000.0
    timeout_ms = json.loads(descriptor.read_text())["timeout_ms"]
    assert timeout_ms > worst_case_ms, (
        f"{descriptor.name}: timeout_ms={timeout_ms} must strictly exceed the helper's "
        f"worst case {worst_case_ms:.0f}ms so the helper's deny wins the race"
    )


# --- Inline `RIG_HATCH_REQUEST_<HOOK_ID>=value` on the Bash command string ------------------
#
# A pre-bash hook runs in its OWN process BEFORE the shell evaluates a `VAR=x cmd` prefix, so the
# documented inline hatch form (`RIG_HATCH_REQUEST_X="why" <gated-command>`) never reaches the
# hook's os.environ. request_hatch_approval(command=...) must instead parse the assignment out of
# the command string the event carries. These tests pin that behavior (env is forced empty so the
# ONLY possible source of the justification is the inline command parse).

_DESTRUCTIVE_RESET = "git reset " + "--hard"


def test_inline_command_assignment_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = (
        f'{env_var}="need to discard a disposable experiment" {_DESTRUCTIVE_RESET}'
    )

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.requested is True
    assert result.approved is True
    assert result.env_present is True
    assert result.should_stop is True
    question = question_file.read_text()
    assert "need to discard a disposable experiment" in question
    assert "block-reset-hard" in question


def test_inline_quoted_value_with_spaces_preserved(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-raw-pr-merge")
    command = f'{env_var}="ship gate down, manual verify done" gh pr merge 123 --admin'

    result = request_hatch_approval(
        "block-raw-pr-merge",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "ship gate down, manual verify done" in question_file.read_text()


def test_process_env_takes_precedence_over_inline(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f'{env_var}="INLINE justification" {_DESTRUCTIVE_RESET}'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={env_var: "EXPORTED justification"},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    question = question_file.read_text()
    # The chosen justification is the EXPORTED one, not the inline one. Assert on the specific
    # `Justification:` line (the raw command is echoed verbatim elsewhere in the question and
    # legitimately contains the inline text, so a bare `not in` would be a false negative).
    assert "<b>Justification:</b> EXPORTED justification" in question
    assert "<b>Justification:</b> INLINE justification" not in question


def test_inline_bare_flag_is_rejected_without_tg_ctl_contact(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f"{env_var}=1 {_DESTRUCTIVE_RESET}"

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is True
    assert result.approved is False
    assert result.should_stop is True
    assert "written justification" in result.reason
    assert not marker.exists()


def test_inline_only_matches_the_hooks_own_var(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    # A DIFFERENT hook's var (and an arbitrary env) must never be consumed by this hook.
    command = f'RIG_HATCH_REQUEST_SOME_OTHER_HOOK="why" FOO=bar {_DESTRUCTIVE_RESET}'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[],
    )

    assert result.requested is False
    assert result.env_present is False
    assert result.should_stop is False
    assert not marker.exists()


def test_inline_assignment_after_executable_is_not_consumed(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    # The assignment appears as an ARGUMENT after the executable, not a leading env prefix — a
    # real shell would pass it to `git` as an argument, not set it in the environment.
    command = f'git commit -m "{env_var}=sneaky"'

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[],
    )

    assert result.requested is False
    assert result.should_stop is False
    assert not marker.exists()


def test_no_command_and_empty_env_is_not_requested(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": _DESTRUCTIVE_RESET},
        cwd=str(repo),
        env={},
        command=None,
        tg_ctl_candidates=[],
    )

    assert result.requested is False
    assert result.env_present is False
    assert not marker.exists()


def test_inline_malformed_quoting_is_not_requested(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    command = f'{env_var}="unterminated {_DESTRUCTIVE_RESET}'  # unmatched quote

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[],
    )

    assert result.requested is False
    assert not marker.exists()


# --- Segment-aware inline parsing (`&&` / `||` / `;` / `|` / newline) ------------------------
#
# The gated command is often not the first simple command on the line. A pre-bash hook must
# still find the leading `RIG_HATCH_REQUEST_<HOOK_ID>=…` assignment on a later segment, exactly
# as the shell would apply the `VAR=x cmd` prefix to that segment's command. The hook gates and
# (on approval) allows the WHOLE command as a unit, and the full command is shown to the human
# approver, so a leading assignment on ANY segment requests approval of the whole command.


def _tg_ctl_recording(tmp_path, question_file):
    return _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )


def test_inline_multi_segment_after_and_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    command = f'cd {repo} && {env_var}="no reviewer available, verified by hand" git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert result.should_stop is True
    assert "no reviewer available, verified by hand" in question_file.read_text()


def test_inline_newline_separated_segment_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    command = f'cd {repo}\n{env_var}="hand-verified" git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "hand-verified" in question_file.read_text()


def test_inline_semicolon_gated_segment_carries_assignment(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    # Assignment sits on the SECOND (gated) segment, after a harmless first one.
    command = f'echo starting ; {env_var}="hand-verified" git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "hand-verified" in question_file.read_text()


def test_inline_whole_command_approval_from_earlier_segment(tmp_path):
    # Documented, intentional: the hook gates/allows the WHOLE command and the human approver
    # sees the full command in the tg-ctl question, so a leading assignment on an earlier segment
    # is a request to approve the entire command (never a silent bypass — approval is human).
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("require-review-before-commit")
    command = f'{env_var}="hand-verified" echo ok ; git commit -m x'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.requested is True
    assert result.should_stop is True
    question = question_file.read_text()
    assert "<b>Justification:</b> hand-verified" in question
    # The full command (including the later gated segment) is shown to the approver.
    assert "git commit -m x" in question


def test_inline_quoted_separator_is_not_a_false_positive(tmp_path):
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))
    repo = _repo_with_tg_ctl(tmp_path)
    # A `;` inside a quoted argument must not be mistaken for a real leading assignment on a new
    # segment — there is no hatch var here at all.
    command = 'git commit -m "step one; step two"'

    result = request_hatch_approval(
        "require-review-before-commit",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[],
    )

    assert result.requested is False
    assert not marker.exists()


# --- Quote-aware tokenization: line continuations, single `&`, quoted separators -------------
#
# The documented README examples use a `VAR="…" \`<newline>`<command>` line-continuation shape,
# and real gated commands can use a single `&` (background) separator. Quoting must be respected
# so a `;`/`&`/`|` inside the justification value is NOT treated as a command boundary.


def test_inline_line_continuation_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-raw-pr-merge")
    # The exact README shape: `VAR="…" \` at end of line, command on the next line.
    command = (
        f'{env_var}="ship gate down, manual verify done" \\\n  gh pr merge 123 --admin'
    )

    result = request_hatch_approval(
        "block-raw-pr-merge",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "ship gate down, manual verify done" in question_file.read_text()


def test_inline_single_ampersand_separator_triggers_ask(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("subagent-no-bg-longproc")
    command = f'sleep 1 & {env_var}="self-managed watchdog" review diff -C /repo'

    result = request_hatch_approval(
        "subagent-no-bg-longproc",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert "self-managed watchdog" in question_file.read_text()


def test_inline_quoted_separator_in_value_preserved(tmp_path):
    question_file = tmp_path / "question.txt"
    tg_ctl = _tg_ctl_recording(tmp_path, question_file)
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-raw-pr-merge")
    # A `&`/`;` inside the quoted justification must survive as part of the value, not split it.
    command = f'{env_var}="ci & security both green; verified" gh pr merge 7 --admin'

    result = request_hatch_approval(
        "block-raw-pr-merge",
        {"command": command},
        cwd=str(repo),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
    )

    assert result.approved is True
    assert (
        "<b>Justification:</b> ci &amp; security both green; verified" in question_file.read_text()
    )


# --- resolve_home(): the real OS-identity impl + its fail-closed branch ----------------------
#
# The autouse `_hermetic_home` fixture stubs `resolve_home` for the rest of the suite, so these
# exercise the GENUINE implementation (`_REAL_RESOLVE_HOME`, captured at import). Codex review
# flagged that an `expanduser("~")` fallback would reintroduce an $HOME/cwd-controlled trust
# anchor; these pin that it does NOT.


@pytest.mark.real_os_home
def test_resolve_home_is_os_account_home_not_env(monkeypatch):
    """The real resolve_home() keys off the OS identity (pwd.getpwuid), never $HOME."""
    import os as _os
    import pwd as _pwd

    monkeypatch.setattr(
        agenttools_hatch_escalation,
        "_find_tg_ctl",
        lambda *_args, **_kwargs: pytest.fail(
            "resolve_home test must not resolve tg-ctl"
        ),
    )
    monkeypatch.setenv("HOME", "/tmp/attacker-controlled-home")  # must be ignored
    assert _REAL_RESOLVE_HOME() == _pwd.getpwuid(_os.getuid()).pw_dir


def test_resolve_home_returns_none_when_no_passwd_entry(monkeypatch):
    """When the OS account home can't be resolved, resolve_home returns None — it does NOT fall
    back to $HOME or a cwd-relative `~` (both agent-controllable)."""

    def _boom(*_args, **_kwargs):
        raise KeyError("no passwd entry")

    monkeypatch.setattr(agenttools_hatch_escalation.pwd, "getpwuid", _boom)
    monkeypatch.setenv("HOME", "/tmp/attacker-controlled-home")
    assert _REAL_RESOLVE_HOME() is None


def test_unresolvable_home_does_not_fall_back_to_env_or_cwd(tmp_path, monkeypatch):
    """SECURITY (P1 follow-up): if pwd.getpwuid fails, the rig.yaml `tg_ctl_path` override is
    SKIPPED — it must NOT be read from $HOME (or a cwd-relative `~`). An approving rig.yaml placed
    in $HOME must therefore NOT self-approve; with no trusted binary reachable the request denies
    and the attacker binary is never executed."""
    marker = tmp_path / "evil-called"
    evil = _write_tg_ctl(
        tmp_path / "evil" / "tg-ctl", _allow_body(marker)
    )  # would approve
    attacker_home = _home_with_tg_ctl(tmp_path, evil, name="attacker-home")
    monkeypatch.setenv(
        "HOME", str(attacker_home)
    )  # $HOME points at the approving override
    # Run the REAL resolve_home (undo the autouse stub) with getpwuid forced to fail.
    monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", _REAL_RESOLVE_HOME)

    def _boom(*_args, **_kwargs):
        raise KeyError("no passwd entry")

    monkeypatch.setattr(agenttools_hatch_escalation.pwd, "getpwuid", _boom)
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(attacker_home),  # cwd also points at the override — must be ignored too
        env={env_var: "attacker-supplied justification"},
        tg_ctl_candidates=[],
        timeout_s=1,
    )

    assert result.approved is False
    assert not marker.exists()  # the $HOME/cwd override was NEVER honored
    assert "not available" in result.reason


def test_home_rig_tilde_tg_ctl_path_is_not_expanded_via_env_home(tmp_path, monkeypatch):
    """SECURITY (P1 follow-up): a `~`-prefixed `tg_ctl_path` in the (trusted) home rig config must
    NOT be expanded against `$HOME` — that is agent-exportable, so `expanduser("~/bin/tg-ctl")`
    would resolve to an attacker binary under a doctored `$HOME`. `~/` is expanded against the
    REAL home only, which here holds no binary: an approving binary reachable only via `$HOME`
    must never be executed."""
    evil_marker = tmp_path / "evil-called"
    attacker = tmp_path / "attacker"
    _write_tg_ctl(
        attacker / "bin" / "tg-ctl", f"touch {evil_marker}\nexit 0\n"
    )  # would approve
    monkeypatch.setenv("HOME", str(attacker))  # $HOME points at the attacker tree
    # The REAL home's rig config carries a ~-relative override (would expand via $HOME if allowed).
    home = tmp_path / "real-home"
    _write_rig_config(home, "~/bin/tg-ctl")
    monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", lambda: str(home))
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(tmp_path),
        env={env_var: "attacker-supplied justification"},
        tg_ctl_candidates=[],
        timeout_s=1,
    )

    assert result.approved is False
    assert not evil_marker.exists()  # the ~-via-$HOME binary was NEVER executed
    assert "not available" in result.reason


def test_home_rig_lookup_does_not_walk_up_to_parent(tmp_path, monkeypatch):
    """SECURITY (P1 follow-up): the home `tg_ctl_path` override is read EXACTLY from
    resolve_home()/.config/rig/config.yaml and must NOT walk up into a parent directory. If the
    account home is nested under a workspace (or any agent-controlled dir) whose parent carries an
    attacker rig config, that parent override must be ignored."""
    evil_marker = tmp_path / "evil-called"
    evil = _write_tg_ctl(
        tmp_path / "evil" / "tg-ctl", f"touch {evil_marker}\nexit 0\n"
    )  # would approve
    workspace = tmp_path / "workspace"
    _write_rig_config(workspace, evil)  # PARENT attacker
    home = workspace / "home"
    home.mkdir()  # the real home itself carries NO rig config
    monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", lambda: str(home))
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(tmp_path),
        env={env_var: "attacker-supplied justification"},
        tg_ctl_candidates=[],
        timeout_s=1,
    )

    assert result.approved is False
    assert not evil_marker.exists()  # the parent-dir override was NEVER honored
    assert "not available" in result.reason


# ── Escape-hatch audit sink (overrides.log, G-8 / retrospective 5.2.3 item 3) ────────────────


def _read_overrides_log(log_path: Path) -> list[dict]:
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


def test_unset_env_never_touches_overrides_log(tmp_path):
    """An env var that was never set is not a hatch USE — nothing should be logged, and neither
    the tier-3 `resolve_home()` default NOR the ACTUAL sink this test would write to (tier 2, the
    `AGENT_TOOLS_OVERRIDES_LOG` path the repo-wide conftest fixture exports for every test) may be
    created. Asserting only the tier-3 path would miss a regression that moved the audit-log call
    ahead of the `raw is None` early-return — that stray write would land in the tier-2 path, which
    wins precedence, leaving the tier-3 assertion trivially (and misleadingly) green."""
    repo = _repo_with_tg_ctl(tmp_path)
    default_log = tmp_path / "_clean_home" / ".config" / "agent-tools" / "overrides.log"
    actual_sink = Path(
        os.environ["AGENT_TOOLS_OVERRIDES_LOG"]
    )  # the tier that would really fire

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={},
        tg_ctl_candidates=[tmp_path / "missing" / "tg-ctl"],
    )

    assert result.env_present is False
    assert not default_log.exists()
    assert not actual_sink.exists()


def test_approved_hatch_use_is_logged_with_expected_fields(tmp_path):
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _ALLOW_REPLY_SH)
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("visual-proof-gate")

    result = request_hatch_approval(
        "visual-proof-gate",
        {"session_id": "sess-42", "command": "git commit -m x"},
        cwd=str(repo),
        env={env_var: "Deleting a dead component, nothing to render."},
        command="git commit -m x",
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
        overrides_log_path=log_path,
    )

    assert result.approved is True
    entries = _read_overrides_log(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["hatch"] == "visual-proof-gate"
    assert entry["env_var"] == env_var
    assert entry["session"] == "sess-42"
    assert entry["command"] == "git commit -m x"
    assert entry["reason"] == "Deleting a dead component, nothing to render."
    assert entry["decision"] == "approved"
    assert entry["cwd"] == str(repo)
    assert entry["ts"].endswith("Z")


def test_logged_command_falls_back_to_context_when_kwarg_omitted(tmp_path):
    """A caller that threads the command only through `context` (not the `command=` kwarg)
    must not ship a blank audit `command` field — most real hook call sites pass both
    redundantly, but the fallback covers any that only pass one."""
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "exit 1\n")
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("block-reset-hard")

    request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Recovering from a bad merge."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=2,
        overrides_log_path=log_path,
    )

    entries = _read_overrides_log(log_path)
    assert len(entries) == 1
    assert entries[0]["command"] == "git reset --hard"


def test_denied_hatch_use_is_still_logged(tmp_path):
    """A denied bypass is exactly the kind of pressure an unaudited hatch would hide — it must be
    logged just like an approved one, with decision=denied and the tg-ctl verdict in `detail`."""
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", "exit 1\n")
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git reset --hard"},
        cwd=str(repo),
        env={env_var: "Need to discard a disposable failed experiment."},
        tg_ctl_candidates=[tg_ctl],
        timeout_s=1,
        overrides_log_path=log_path,
    )

    assert result.approved is False
    entries = _read_overrides_log(log_path)
    assert len(entries) == 1
    assert entries[0]["decision"] == "denied"
    assert "denied" in entries[0]["detail"]


def test_blank_and_bare_hatch_attempts_are_logged(tmp_path):
    """Blank/bare-flag values never contact tg-ctl but are still a hatch USE attempt worth
    auditing (they are exactly the self-service-without-a-reason shape G-8 exists to surface)."""
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("block-reset-hard")

    request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "   "},
        overrides_log_path=log_path,
    )
    request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "1"},
        overrides_log_path=log_path,
    )

    entries = _read_overrides_log(log_path)
    assert len(entries) == 2
    assert all(e["decision"] == "denied" for e in entries)


def test_session_id_resolution_order(tmp_path):
    """context session_id wins over env vars, which win over the pid fallback.

    All three calls pass a deliberately-missing `tg_ctl_candidates` (matching every other
    test in this file) rather than leaving it at its default `None`. A `None` candidates list
    falls through to `_TRUSTED_TG_CTL_PATHS` (real, hardcoded system paths like
    `/Users/ultra/.files/bin/tg-ctl`) — on a dev machine that actually has tg-ctl installed
    there (as opposed to a CI runner, where the path is absent), this would resolve to the
    REAL tg-ctl and attempt a real `ask` round trip against the real Telegram bot for each of
    the three "reason one/two/three" justifications, each blocking up to
    DEFAULT_TG_CTL_TIMEOUT_S. This is exactly the class of test-isolation leak this PR's own
    audit-log fix addresses elsewhere — don't reintroduce it here.
    """
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")
    missing_tg_ctl = [tmp_path / "missing" / "tg-ctl"]

    log_path = tmp_path / "ctx-wins.log"
    request_hatch_approval(
        "block-reset-hard",
        {"session_id": "from-context"},
        cwd=str(repo),
        env={env_var: "reason one"},
        overrides_log_path=log_path,
        tg_ctl_candidates=missing_tg_ctl,
    )
    assert _read_overrides_log(log_path)[0]["session"] == "from-context"

    log_path = tmp_path / "env-wins.log"
    request_hatch_approval(
        "block-reset-hard",
        {},
        cwd=str(repo),
        env={env_var: "reason two", "CLAUDE_SESSION_ID": "from-env"},
        overrides_log_path=log_path,
        tg_ctl_candidates=missing_tg_ctl,
    )
    assert _read_overrides_log(log_path)[0]["session"] == "from-env"

    log_path = tmp_path / "pid-fallback.log"
    request_hatch_approval(
        "block-reset-hard",
        {},
        cwd=str(repo),
        env={env_var: "reason three"},
        overrides_log_path=log_path,
        tg_ctl_candidates=missing_tg_ctl,
    )
    assert _read_overrides_log(log_path)[0]["session"] == f"pid:{os.getpid()}"


def test_multiple_uses_append_rather_than_overwrite(tmp_path):
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("block-reset-hard")

    for _ in range(3):
        request_hatch_approval(
            "block-reset-hard",
            {"command": "git clean -fd"},
            cwd=str(repo),
            env={env_var: "1"},
            overrides_log_path=log_path,
        )

    assert len(_read_overrides_log(log_path)) == 3


def test_default_overrides_log_path_is_rooted_at_resolve_home(tmp_path, monkeypatch):
    """No explicit `overrides_log_path` AND no `AGENT_TOOLS_OVERRIDES_LOG` override -> the sink
    lands under the (test-monkeypatched) `resolve_home()`, exactly where
    `default_overrides_log_path()` reports it — never under a doctored `$HOME` or the
    agent-controlled `cwd`/repo. The repo-wide `tests/conftest.py` fixture exports
    `AGENT_TOOLS_OVERRIDES_LOG` for every test (subprocess hermeticity) — that tier is deliberately
    cleared here so this test proves the tier-3 fallback specifically."""
    monkeypatch.delenv("AGENT_TOOLS_OVERRIDES_LOG", raising=False)
    attacker_home = tmp_path / "attacker-home"
    attacker_home.mkdir()
    monkeypatch.setenv("HOME", str(attacker_home))
    real_home = tmp_path / "_clean_home"  # set up by the autouse _hermetic_home fixture
    repo = _repo_with_tg_ctl(tmp_path)
    env_var = hatch_env_var("block-reset-hard")

    request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "1"},
    )

    assert not (attacker_home / ".config" / "agent-tools" / "overrides.log").exists()
    expected = real_home / ".config" / "agent-tools" / "overrides.log"
    assert expected.exists()
    assert agenttools_hatch_escalation.default_overrides_log_path() == expected


def test_overrides_log_env_override_takes_precedence_over_resolve_home(
    tmp_path, monkeypatch
):
    """`AGENT_TOOLS_OVERRIDES_LOG` (tier 2) wins over the `resolve_home()`-rooted default (tier 3)
    — this is the subprocess-reachable override `tests/conftest.py` relies on for hermeticity, and
    an explicit `overrides_log_path=` kwarg (tier 1, exercised by every other test in this section)
    would win over it in turn."""
    repo = _repo_with_tg_ctl(tmp_path)
    env_log = tmp_path / "env-override" / "overrides.log"
    monkeypatch.setenv("AGENT_TOOLS_OVERRIDES_LOG", str(env_log))
    env_var = hatch_env_var("block-reset-hard")

    request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "1"},
    )

    entries = _read_overrides_log(env_log)
    assert len(entries) == 1
    real_home_default = (
        tmp_path / "_clean_home" / ".config" / "agent-tools" / "overrides.log"
    )
    assert not real_home_default.exists()


def test_overrides_log_write_failure_never_raises_or_blocks_hatch(tmp_path):
    """Audit logging is best-effort: if the log's parent path cannot be created (here, a FILE
    already occupies the would-be parent directory's name), the hatch flow must proceed normally
    rather than raising out of `request_hatch_approval`."""
    repo = _repo_with_tg_ctl(tmp_path)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("occupied")
    log_path = (
        blocked / "overrides.log"
    )  # blocked's parent mkdir will fail: it's a file, not a dir
    env_var = hatch_env_var("block-reset-hard")

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "1"},
        overrides_log_path=log_path,
    )

    assert result.approved is False  # the hatch itself still resolved normally
    assert not log_path.exists()


def test_overrides_log_is_created_with_0600_permissions(tmp_path):
    """The audit line can carry a free-text justification and a full shell command — it must be
    owner-only readable from the moment the file is created, not just by convention."""
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("block-reset-hard")

    request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "1"},
        overrides_log_path=log_path,
    )

    mode = stat.S_IMODE(log_path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_overrides_log_permissions_are_tightened_on_a_preexisting_loose_file(tmp_path):
    """A pre-existing log file with looser permissions (e.g. created before this feature shipped,
    or by a misconfigured umask) must be tightened to 0600 on the very next append, not left as
    a lingering world/group-readable file forever."""
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    log_path.write_text("")
    log_path.chmod(0o644)
    env_var = hatch_env_var("block-reset-hard")

    request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "1"},
        overrides_log_path=log_path,
    )

    mode = stat.S_IMODE(log_path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_internal_exception_is_still_logged_as_denied(tmp_path, monkeypatch):
    """`_append_overrides_log`'s docstring promises EVERY env-present attempt is audited,
    including the `except Exception` branch in `request_hatch_approval` (a crash inside
    `_request_present_hatch_approval` itself, e.g. `_find_tg_ctl` raising unexpectedly) — not just
    the approved/denied/blank/bare outcomes reached via the normal tg-ctl round trip."""
    repo = _repo_with_tg_ctl(tmp_path)
    log_path = tmp_path / "overrides.log"
    env_var = hatch_env_var("block-reset-hard")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(agenttools_hatch_escalation, "_find_tg_ctl", _boom)

    result = request_hatch_approval(
        "block-reset-hard",
        {"command": "git clean -fd"},
        cwd=str(repo),
        env={env_var: "a real written justification"},
        overrides_log_path=log_path,
    )

    assert result.approved is False
    assert "errored" in result.reason
    entries = _read_overrides_log(log_path)
    assert len(entries) == 1
    assert entries[0]["decision"] == "denied"
    assert "simulated internal failure" in entries[0]["detail"]


# --- late-approval grant redemption (agent-tools#771 / tg-cli#398) ----------------------------

def _grant_allow_body(argv_file: Path, payload_file: Path, grant_id: str) -> str:
    reply = json.dumps(
        {
            "requestId": "original-hatch-request",
            "approvalId": grant_id,
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            },
        }
    )
    return (
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'cat > "{payload_file}"\n'
        f"printf '%s' '{reply}'\n"
        "exit 0\n"
    )


def test_late_grant_fingerprint_ignores_inline_hatch_value():
    hook_id = "require-ticket-before-commit"
    env_var = hatch_env_var(hook_id)
    grant_id = "hgr-85d3654be317"
    first = (
        'cd /tmp/mocker-src && '
        f'{env_var}="Scratch clone approved for one local commit" '
        'git commit -m "feat: docker api"'
    )
    retry = (
        'cd /tmp/mocker-src && '
        f'{env_var}="approved:{grant_id}" '
        'git commit -m "feat: docker api"'
    )

    first_fp = agenttools_hatch_escalation._operation_fingerprint(
        hook_id, {"hook": hook_id, "command": first}, env_var=env_var, command=first
    )
    retry_fp = agenttools_hatch_escalation._operation_fingerprint(
        hook_id, {"hook": hook_id, "command": retry}, env_var=env_var, command=retry
    )

    assert first_fp == retry_fp


def test_approved_grant_token_verifies_once_without_posting_new_ask(tmp_path):
    hook_id = "require-ticket-before-commit"
    env_var = hatch_env_var(hook_id)
    grant_id = "hgr-85d3654be317"
    argv_file = tmp_path / "argv.txt"
    payload_file = tmp_path / "payload.json"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        _grant_allow_body(argv_file, payload_file, grant_id),
    )
    command = (
        'cd /tmp/mocker-src && '
        f'{env_var}="approved:{grant_id}" '
        'git commit -m "feat: docker api"'
    )

    result = request_hatch_approval(
        hook_id,
        {"hook": hook_id, "command": command},
        cwd="/Users/ultra/tmp",
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.approved is True
    assert result.should_stop is True
    argv = argv_file.read_text().splitlines()
    assert argv == ["ask", "--verify-grant", grant_id, "--hook", hook_id]
    payload = json.loads(payload_file.read_text())
    assert payload["operation"]["fingerprint"]
    assert payload["operation"]["cwd"] == "/Users/ultra/tmp"


def test_malformed_approved_grant_token_never_posts_new_ask(tmp_path):
    hook_id = "require-ticket-before-commit"
    env_var = hatch_env_var(hook_id)
    marker = tmp_path / "called"
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", _allow_body(marker))

    result = request_hatch_approval(
        hook_id,
        {"hook": hook_id, "command": "git commit -m x"},
        cwd=str(tmp_path),
        env={env_var: "approved:not-a-real-grant"},
        command="git commit -m x",
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.approved is False
    assert result.should_stop is True
    assert "malformed" in result.reason
    assert not marker.exists()


def test_plain_retry_consumes_matching_grant_via_pull(tmp_path):
    hook_id = "require-ticket-before-commit"
    grant_id = "hgr-0123456789ab"
    argv_file = tmp_path / "argv.txt"
    payload_file = tmp_path / "payload.json"
    tg_ctl = _write_tg_ctl(
        tmp_path / "trusted" / "tg-ctl",
        _grant_allow_body(argv_file, payload_file, grant_id),
    )
    command = 'git commit -m "feat: docker api"'

    result = request_hatch_approval(
        hook_id,
        {"hook": hook_id, "command": command},
        cwd=str(tmp_path),
        env={},
        command=command,
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.approved is True
    assert result.should_stop is True
    argv = argv_file.read_text().splitlines()
    assert argv == ["ask", "--verify-grant", "--pull", "--hook", hook_id]


def test_pull_deny_falls_back_to_original_block_without_new_ask(tmp_path):
    hook_id = "require-ticket-before-commit"
    argv_file = tmp_path / "argv.txt"
    payload_file = tmp_path / "payload.json"
    deny = json.dumps(
        {
            "requestId": "retry",
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": "no outstanding grant"},
            },
        }
    )
    body = (
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'cat > "{payload_file}"\n'
        f"printf '%s' '{deny}'\n"
        "exit 0\n"
    )
    tg_ctl = _write_tg_ctl(tmp_path / "trusted" / "tg-ctl", body)

    result = request_hatch_approval(
        hook_id,
        {"hook": hook_id, "command": "git commit -m x"},
        cwd=str(tmp_path),
        env={},
        command="git commit -m x",
        tg_ctl_candidates=[tg_ctl],
    )

    assert result.requested is False
    assert result.approved is False
    assert result.should_stop is False
    assert argv_file.read_text().splitlines() == [
        "ask", "--verify-grant", "--pull", "--hook", hook_id
    ]


# ── trusted-path resolution: macOS / Linux / Cygwin install layouts ────────────────────────────


def _install(path: Path, root: Path, *, body: str = _ALLOW_REPLY_SH) -> Path:
    """A fake tg-ctl at `path` whose dirs up to `root` are 0755 regardless of the runner's umask."""
    _write_tg_ctl(path, body)
    for directory in (path.parent, *path.parent.parents):
        if directory == root or root not in directory.parents:
            break
        directory.chmod(0o755)
    return path


def _git_checkout(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _lookup(monkeypatch, home: Path, cwd: Path, *, system=(), candidates=None):
    monkeypatch.setattr(agenttools_hatch_escalation, "resolve_home", lambda: str(home))
    monkeypatch.setattr(agenttools_hatch_escalation, "_TRUSTED_TG_CTL_PATHS", tuple(system))
    return agenttools_hatch_escalation._find_tg_ctl(candidates, str(cwd))


def _stat(mode: int, *, uid: int | None = None, gid: int = 4242) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, os.geteuid() if uid is None else uid, gid, 0, 0, 0, 0))


def test_local_bin_symlink_into_tg_cli_checkout_is_trusted(tmp_path, monkeypatch):
    """This machine's layout: `~/.local/bin/tg-ctl` -> `~/.local/share/tg-cli/tg-ctl`, where the
    target dir is itself a tg-cli git checkout. The symlink is followed, and a checkout the agent
    is NOT working in is fine."""
    home = tmp_path / "home"
    target = _install(home / ".local" / "share" / "tg-cli" / "tg-ctl", tmp_path)
    _git_checkout(target.parent)
    link = home / ".local" / "bin" / "tg-ctl"
    link.parent.mkdir(parents=True)
    link.parent.chmod(0o755)
    link.symlink_to(Path("..") / "share" / "tg-cli" / "tg-ctl")
    project = _git_checkout(tmp_path / "project")

    lookup = _lookup(monkeypatch, home, project)

    assert lookup.path == target


def test_tg_ctl_inside_the_checkout_the_agent_works_in_is_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    checkout = home / ".local" / "share" / "tg-cli"
    _install(checkout / "tg-ctl", tmp_path)
    _git_checkout(checkout)
    (checkout / "features").mkdir()

    lookup = _lookup(monkeypatch, home, checkout / "features", candidates=[checkout / "tg-ctl"])

    assert lookup.path is None
    assert f"inside the git checkout {checkout}" in lookup.unavailable_reason("x")


def test_symlink_hop_through_the_agents_checkout_is_rejected(tmp_path, monkeypatch):
    """`~/.local/bin/tg-ctl` -> `<checkout>/redirect` -> a binary outside: both endpoints are
    outside the checkout, but the agent's PR could repoint `redirect` at any exit-0 script."""
    home = tmp_path / "home"
    outside = _install(tmp_path / "opt" / "tg-ctl", tmp_path)
    checkout = _git_checkout(tmp_path / "checkout")
    (checkout / "redirect").symlink_to(outside)
    link = home / ".local" / "bin" / "tg-ctl"
    link.parent.mkdir(parents=True)
    for directory in (home, home / ".local", link.parent):
        directory.chmod(0o755)
    link.symlink_to(checkout / "redirect")

    lookup = _lookup(monkeypatch, home, checkout)

    assert lookup.path is None
    assert f"{checkout / 'redirect'} is inside the git checkout {checkout}" in lookup.unavailable_reason("x")


def test_tg_ctl_in_the_primary_checkout_of_the_agents_linked_worktree_is_rejected(
    tmp_path, monkeypatch
):
    """An agent in `.worktrees/w` can still edit the primary checkout, so that one counts too."""
    main = _git_checkout(tmp_path / "main")
    binary = _install(main / "tg-ctl", tmp_path)
    gitdir = main / ".git" / "worktrees" / "w"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")

    lookup = _lookup(monkeypatch, tmp_path / "home", worktree, candidates=[binary])

    assert lookup.path is None
    assert f"inside the git checkout {main}" in lookup.unavailable_reason("x")


def test_dotfiles_repo_rooted_at_home_does_not_swallow_home_bins(tmp_path, monkeypatch):
    home = _git_checkout(tmp_path / "home")
    binary = _install(home / ".local" / "bin" / "tg-ctl", tmp_path)

    assert _lookup(monkeypatch, home, home).path == binary


def test_homebrew_cellar_symlink_is_trusted(tmp_path, monkeypatch):
    """Homebrew links `<prefix>/bin/tg-ctl` relatively into the Cellar (both /opt/homebrew and
    /usr/local prefixes, and Linuxbrew, use this shape)."""
    prefix = tmp_path / "opt" / "homebrew"
    cellar = _install(prefix / "Cellar" / "tg-cli" / "1.2.0" / "bin" / "tg-ctl", tmp_path)
    link = prefix / "bin" / "tg-ctl"
    link.parent.mkdir()
    link.parent.chmod(0o755)
    link.symlink_to(Path("..") / "Cellar" / "tg-cli" / "1.2.0" / "bin" / "tg-ctl")

    lookup = _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[link])

    assert lookup.path == cellar


def test_cygwin_exe_suffix_is_found_for_a_bare_candidate(tmp_path, monkeypatch):
    exe = _install(tmp_path / "usr" / "local" / "bin" / "tg-ctl.exe", tmp_path)
    monkeypatch.setattr(agenttools_hatch_escalation, "_IS_CYGWIN", True)

    lookup = _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[exe.with_name("tg-ctl")])

    assert lookup.path == exe


def test_bare_candidate_does_not_match_exe_outside_cygwin(tmp_path, monkeypatch):
    exe = _install(tmp_path / "bin" / "tg-ctl.exe", tmp_path)
    monkeypatch.setattr(agenttools_hatch_escalation, "_IS_CYGWIN", False)

    assert _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[exe.with_name("tg-ctl")]).path is None


def test_cygwin_windows_drive_path_in_rig_config_is_translated(tmp_path, monkeypatch):
    exe = _install(tmp_path / "cygdrive" / "c" / "Users" / "me" / "bin" / "tg-ctl.exe", tmp_path)
    home = tmp_path / "home"
    config = _write_rig_config(home, None)
    config.write_text("tg_ctl:\n  tg_ctl_path: 'C:\\Users\\me\\bin\\tg-ctl.exe'\n")
    monkeypatch.setattr(agenttools_hatch_escalation, "_IS_CYGWIN", True)
    monkeypatch.setattr(
        agenttools_hatch_escalation, "_cygdrive_prefix", lambda: str(tmp_path / "cygdrive")
    )

    assert _lookup(monkeypatch, home, tmp_path, candidates=[]).path == exe


def test_cygwin_administrators_ownership_is_trusted_only_on_cygwin(monkeypatch):
    info = _stat(stat.S_IFREG | 0o755, uid=544)
    monkeypatch.setattr(agenttools_hatch_escalation, "_IS_CYGWIN", False)
    assert "owned by uid 544" in agenttools_hatch_escalation.path_trust_problem("/x", info)
    monkeypatch.setattr(agenttools_hatch_escalation, "_IS_CYGWIN", True)
    assert agenttools_hatch_escalation.path_trust_problem("/x", info) is None


def test_world_writable_install_dir_is_rejected(tmp_path, monkeypatch):
    binary = _install(tmp_path / "shared" / "bin" / "tg-ctl", tmp_path)
    binary.parent.chmod(0o777)

    lookup = _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[binary])

    assert lookup.path is None
    assert f"{binary.parent} is world-writable" in lookup.unavailable_reason("x")


def test_world_writable_binary_is_rejected(tmp_path, monkeypatch):
    binary = _install(tmp_path / "bin" / "tg-ctl", tmp_path)
    binary.chmod(0o777)

    lookup = _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[binary])

    assert lookup.path is None
    assert f"{binary} is world-writable" in lookup.unavailable_reason("x")


def test_sticky_world_writable_dir_on_the_way_is_fine(tmp_path, monkeypatch):
    """`/tmp`-style sticky dirs: nobody else can rename or replace an entry we own in them."""
    binary = _install(tmp_path / "sticky" / "mine" / "tg-ctl", tmp_path)
    (tmp_path / "sticky").chmod(0o1777)

    assert _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[binary]).path == binary


def test_trusted_symlink_into_a_world_writable_dir_is_rejected(tmp_path, monkeypatch):
    target = _install(tmp_path / "drop" / "tg-ctl", tmp_path)
    target.parent.chmod(0o777)
    link = tmp_path / "bin" / "tg-ctl"
    link.parent.mkdir()
    link.parent.chmod(0o755)
    link.symlink_to(target)

    lookup = _lookup(monkeypatch, tmp_path / "home", tmp_path, system=[link])

    assert lookup.path is None
    assert f"{target.parent} is world-writable" in lookup.unavailable_reason("x")


def test_group_write_is_trusted_only_for_a_private_group(monkeypatch):
    info = _stat(stat.S_IFDIR | 0o775, gid=4242)
    monkeypatch.setattr(agenttools_hatch_escalation, "_is_private_group", lambda gid: False)
    assert "group-writable by gid 4242" in agenttools_hatch_escalation.path_trust_problem("/d", info)
    monkeypatch.setattr(agenttools_hatch_escalation, "_is_private_group", lambda gid: gid == 4242)
    assert agenttools_hatch_escalation.path_trust_problem("/d", info) is None


def test_path_owned_by_another_account_is_rejected():
    info = _stat(stat.S_IFREG | 0o755, uid=os.geteuid() + 4242)
    assert "not by you or root" in agenttools_hatch_escalation.path_trust_problem("/x", info)


def test_untrusted_rig_config_is_ignored_and_reported(tmp_path, monkeypatch):
    marker = tmp_path / "called"
    evil = _install(tmp_path / "evil" / "tg-ctl", tmp_path, body=_allow_body(marker))
    home = tmp_path / "home"
    config = _write_rig_config(home, evil)
    config.chmod(0o666)

    lookup = _lookup(monkeypatch, home, tmp_path, candidates=[])

    assert lookup.path is None
    assert f"{config}: ignored ({config} is world-writable)" in lookup.unavailable_reason("x")


def test_rig_config_tilde_path_expands_against_the_real_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = _install(home / "tools" / "tg-ctl", tmp_path)
    _write_rig_config(home, "~/tools/tg-ctl")
    monkeypatch.setenv("HOME", str(tmp_path / "doctored"))

    assert _lookup(monkeypatch, home, tmp_path, candidates=[]).path == binary


def test_denial_reason_lists_every_searched_path_and_why(tmp_path):
    missing = tmp_path / "nowhere" / "tg-ctl"
    env_var = hatch_env_var("pin-primary-worktree")

    result = request_hatch_approval(
        "pin-primary-worktree",
        {"target": "feat/x"},
        cwd=str(tmp_path),
        env={env_var: "Need to inspect the primary checkout before repairing it."},
        tg_ctl_candidates=[missing],
        timeout_s=1,
    )

    assert result.approved is False
    assert result.reason.startswith("tg-ctl is not available at a trusted executable path (searched: ")
    assert f"{missing}: {missing.parent} does not exist" in result.reason


def test_test_sandbox_refuses_a_tg_ctl_outside_pytest_basetemp(tmp_path, monkeypatch):
    """The conftest-exported sandbox var keeps hook SUBPROCESSES (which the resolve_home patch
    never reaches) off the machine's real tg-ctl."""
    binary = _install(tmp_path / "outside" / "tg-ctl", tmp_path)
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("AGENTTOOLS_TEST_HERMETIC_HOME", str(sandbox / "home"))

    lookup = _lookup(monkeypatch, sandbox / "home", tmp_path, system=[binary])

    assert lookup.path is None
    assert f"outside the test sandbox {sandbox}" in lookup.unavailable_reason("x")


# ── machine_config_mapping: PyYAML-free reader for the machine-global rig config ───────────────


_RIG_SHAPED_CONFIG = """\
version: 1
agent_tools_source: ~/dev/agent-tools  # trailing comment
agent_hooks:
  enabled: true
  items:
    skills-read-gate:
      enabled: false
notes: |
  ci: this block scalar is not a key
tools:
  items:
    - name: review
      repo: ~/work/review-cli
ci:
  items:
    ship:
      external_review_owners:
      - alex-mextner
      - 'second-owner'
      require_external_review: false
tg_ctl:
  tg_ctl_path: "~/.local/bin/tg-ctl"
"""


def test_machine_config_reads_rig_written_block_yaml():
    read = agenttools_hatch_escalation.machine_config_mapping
    assert read(_RIG_SHAPED_CONFIG, ("ci", "items", "ship")) == {
        "external_review_owners": ["alex-mextner", "second-owner"],
        "require_external_review": False,
    }
    assert read(_RIG_SHAPED_CONFIG, ("tg_ctl",)) == {"tg_ctl_path": "~/.local/bin/tg-ctl"}
    assert read(_RIG_SHAPED_CONFIG, ("missing", "block")) is None


def test_machine_config_keeps_hash_inside_an_escaped_single_quoted_value():
    mapping = agenttools_hatch_escalation.machine_config_mapping(
        "ship:\n  name: 'a b''c #d'  # real comment\n", ("ship",)
    )
    assert mapping == {"name": "a b'c #d"}


def test_machine_config_reads_one_line_flow_lists_and_json():
    read = agenttools_hatch_escalation.machine_config_mapping
    assert read("ship:\n  owners: [a, 'b', \"c\"]\n  none: []\n", ("ship",)) == {
        "owners": ["a", "b", "c"],
        "none": [],
    }
    assert read('{"ship": {"owners": ["a"], "on": false}}', ("ship",)) == {
        "owners": ["a"],
        "on": False,
    }


@pytest.mark.parametrize(
    "text",
    [
        "ci:\n  items: {ship: {require_external_review: false}}\n",  # flow mapping on the path
        "ci:\n  items:\n    ship: {}\n  items:\n    ship: {}\n",  # duplicate key on the path
        "ci: false\n",
        "ci:\n    items: {}\n  oops: 1\n",  # inconsistent indentation
        "a: 1\n---\nci: {}\n",  # second document
        "ci:\n\titems: 1\n",
        "&anchor ci:\n  items: {}\n",
        '{"ci": {"items": {"ship": {"x": 1, "x": 2}}}}',
        "[]",
    ],
)
def test_machine_config_refuses_ambiguous_structure_on_the_path(text):
    with pytest.raises(agenttools_hatch_escalation.MachineConfigError):
        agenttools_hatch_escalation.machine_config_mapping(text, ("ci", "items", "ship"))


@pytest.mark.parametrize(
    "value", ["*alias", "&a false", "!!bool false", "yes", "off", "0", "1.5", "|", '"esc\\"aped"', "a: b"]
)
def test_machine_config_leaves_values_it_will_not_interpret_unparsed(value):
    mapping = agenttools_hatch_escalation.machine_config_mapping(f"ship:\n  flag: {value}\n", ("ship",))
    assert mapping == {"flag": agenttools_hatch_escalation.UNPARSED}
