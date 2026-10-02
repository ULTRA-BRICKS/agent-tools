"""Late-Approval Grant Protocol tests for the shared hatch lib (agent-tools#742, contract v1.2).

Covers the `approved:<grant-id>` token path (allow only on an explicit daemon allow; every
other outcome denies; malformed ids deny without contacting tg-ctl), the enriched ask payload
(hookId / timeoutMs / origin / operation), the sha256 pre-write content digest (a grant for one
edit must NOT verify a different edit to the same file — deny WITHOUT consuming), the bash
fingerprint canonicalization (the inline justification OR `approved:<id>` prefix is invisible),
the automatic PULL path (route-equality match, fail-closed on every infra failure, token path
unaffected by route), the requestId-in-deny-reason tolerant parsing, the two authorized
hook-side context enrichments (worktree-only-writes, orchestrator-stays-thin pre-write), and
the P1 amendment's third enrichment (background-subagent-gate dispatch binding — a changed
prompt or subagent_type denies WITHOUT consuming), plus the ruling-2 fail-closed layers
(2026-09-12): the GATE denies before any lib call when the op-fields helper is missing or
yields no usable identity, and the LIB rejects a dispatch-bound ask/verify/pull whose context
carries no dispatch identity — no path may mint or consume a grant at hook-only granularity.

The tg-ctl fake is a PYTHON script (sh is too weak for grant-store state): it records every
invocation's argv, persists the "daemon grant store" as one JSON state file, and touches a
consumed marker ONLY on a full (id, hook, operation[, route]) match — a mismatch denies WITHOUT
consuming, which is exactly the property the digest tests assert end to end.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_hatch_late_approval.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess  # noqa: S404 - test-only `git init` on a temp dir
import sys
from pathlib import Path

import pytest

# Load the three authorized enriched hooks first (their loaders install their own lib copy in
# sys.modules), then bind THIS module's `agenttools_hatch_escalation` to the last-installed
# copy so direct-lib tests and the bsg hook share one object (the wow and ost hooks keep
# their own).
_HOOKS_ROOT = Path(__file__).resolve().parents[1] / "agent-hooks"


def _load_hook(name: str, script: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS_ROOT / name / script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wow = _load_hook("worktree-only-writes", "worktree_only_writes.py")
ost = _load_hook("orchestrator-stays-thin", "orchestrator_stays_thin.py")
bsg = _load_hook("background-subagent-gate", "background_subagent_gate.py")

import agenttools_hatch_escalation  # noqa: E402 - after the hook loaders above
from agenttools_hatch_escalation import (  # noqa: E402
    _canonical_bash_fingerprint,
    _canonical_digest,
    hatch_env_var,
    hatch_write_op_fields,
    request_hatch_approval,
)

_HOOK = "block-reset-hard"
_ENV_VAR = hatch_env_var(_HOOK)
_GRANT_ID = "hgr-test123abc"
# The genuine `_git_repo_root` bound at import time — the autouse determinism fixture below
# patches the module attribute per test, so the one repoRoot test re-binds from THIS constant.
_REAL_GIT_REPO_ROOT = agenttools_hatch_escalation._git_repo_root

# Isolate throwaway git repos from the developer's global git config (rig-provisioned
# core.hooksPath would fail `git commit` in a bare temp repo). Mirror of the hook test files.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


@pytest.fixture(autouse=True)
def _deterministic_repo_root(monkeypatch):
    """Pin `_git_repo_root` so recorded/verified operations match regardless of the machine
    (the one repoRoot test below re-pins it against a real git repo)."""

    monkeypatch.setattr(
        agenttools_hatch_escalation, "_git_repo_root", lambda cwd: "/repo/root"
    )


def _write_grant_tg_ctl(
    path: Path, state_json: Path, consumed_marker: Path, argv_log: Path
) -> Path:
    """A fake tg-ctl implementing the daemon side of the v1.2 contract well enough to test the
    lib: `ask` records the payload as a grant; `ask --verify-grant <id> --hook <h>` consumes it
    only on a full (id, hookId, operation) match; `ask --verify-grant --pull --hook <h>` matches
    on (hookId, operation) PLUS route equality, with the no-stable-route deny carrying the
    token-path guidance marker the lib surfaces verbatim."""

    script = '''#!{py}
import json, sys, pathlib
state_p = pathlib.Path({state!r})
consumed_p = pathlib.Path({consumed!r})
argv_p = pathlib.Path({argv!r})
argv = sys.argv[1:]
payload = json.load(sys.stdin)
argv_p.parent.mkdir(parents=True, exist_ok=True)
with argv_p.open("a") as fh:
    fh.write(json.dumps(argv) + "\\n")
ALLOW = {{"hookSpecificOutput": {{"hookEventName": "PermissionRequest", "decision": {{"behavior": "allow"}}}}}}
def deny(reason, request_id=None, shape="top"):
    out = {{"hookSpecificOutput": {{"hookEventName": "PermissionRequest",
                                   "decision": {{"behavior": "deny"}}, "reason": reason}}}}
    if request_id:
        if shape == "top":
            out["requestId"] = request_id
        else:
            out["hookSpecificOutput"]["requestId"] = request_id
    print(json.dumps(out)); sys.exit(0)
def allow_extra(approval_id, request_id):
    out = json.loads(json.dumps(ALLOW))
    out["approvalId"] = approval_id
    out["requestId"] = request_id
    out["hookSpecificOutput"]["approvalId"] = approval_id
    print(json.dumps(out)); sys.exit(0)
if "--verify-grant" in argv:
    hook = argv[argv.index("--hook") + 1]
    rec = json.loads(state_p.read_text()) if state_p.exists() else None
    if "--pull" in argv:
        if rec is None or consumed_p.exists():
            deny("no matching grant")
        if payload.get("hookId") != rec["hookId"] or payload.get("operation") != rec["operation"]:
            deny("no matching grant")
        want = (payload.get("routeEvidence") or {{}}).get("tmuxPane", "")
        have = rec.get("routePane", "")
        if not want or not have:
            deny("no stable route identity for auto-pull; retry explicitly with RIG_HATCH_REQUEST_"
                 + hook.replace("-", "_").upper() + '="approved:' + rec["approvalId"] + '"')
        if want != have:
            deny("no matching grant")
        consumed_p.touch()
        allow_extra(rec["approvalId"], rec["requestIds"][0])
    vid = argv[argv.index("--verify-grant") + 1]
    if rec is None or consumed_p.exists() or vid != rec["approvalId"]:
        shape = rec.get("denyShape", "top") if rec else "top"
        deny("unknown or consumed grant " + vid,
             request_id="hatch-block-reset-hard-orig123", shape=shape)
    if payload.get("hookId") != rec["hookId"] or payload.get("operation") != rec["operation"]:
        deny("operation mismatch; grant not consumed")
    consumed_p.touch()
    allow_extra(rec["approvalId"], rec["requestIds"][0])
rec = dict(payload)
rec["approvalId"] = "{gid}"
rec["requestIds"] = [payload.get("requestId") or "hatch-orig-1"]
rec["routePane"] = (payload.get("routeEvidence") or {{}}).get("tmuxPane", "")
state_p.parent.mkdir(parents=True, exist_ok=True)
state_p.write_text(json.dumps(rec))
print(json.dumps(ALLOW))
'''.format(
        py=sys.executable, state=str(state_json), consumed=str(consumed_marker),
        argv=str(argv_log), gid=_GRANT_ID,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    path.chmod(0o755)
    return path


def _write_raw_tg_ctl(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


class _Fake:
    """The fake tg-ctl plus its state paths, created per test under tmp_path."""

    def __init__(self, tmp_path: Path):
        self.state = tmp_path / "grant-state.json"
        self.consumed = tmp_path / "consumed"
        self.argv_log = tmp_path / "argv.log"
        self.path = _write_grant_tg_ctl(tmp_path / "bin" / "tg-ctl", self.state, self.consumed, self.argv_log)

    def argvs(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text().splitlines() if line]

    def recorded(self) -> dict:
        return json.loads(self.state.read_text())


def _ask(tmp_path: Path, *, command=None, context=None, cwd=None, env=None, fake=None,
         overrides_log_path=None):
    fake = fake or _Fake(tmp_path)
    result = request_hatch_approval(
        _HOOK,
        context if context is not None else {"command": command},
        cwd=cwd or str(tmp_path / "w"),
        env=env if env is not None else {_ENV_VAR: "genuine one-off justification for the test"},
        command=command,
        tg_ctl_candidates=[fake.path],
        timeout_s=2,
        process_margin_s=1,
        overrides_log_path=overrides_log_path,
    )
    return fake, result


def _verify(tmp_path: Path, *, command=None, context=None, cwd=None, env=None, fake=None):
    fake = fake or _Fake(tmp_path)
    result = request_hatch_approval(
        _HOOK,
        context if context is not None else {"command": command},
        cwd=cwd or str(tmp_path / "w"),
        env=env if env is not None else {_ENV_VAR: f"approved:{_GRANT_ID}"},
        command=command,
        tg_ctl_candidates=[fake.path],
        timeout_s=2,
        process_margin_s=1,
    )
    return fake, result


def _pull(tmp_path: Path, *, command=None, context=None, cwd=None, env=None, fake=None):
    fake = fake or _Fake(tmp_path)
    result = request_hatch_approval(
        _HOOK,
        context if context is not None else {"command": command},
        cwd=cwd or str(tmp_path / "w"),
        env=env if env is not None else {},
        command=command,
        tg_ctl_candidates=[fake.path],
        timeout_s=2,
        process_margin_s=1,
    )
    return fake, result


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)
    (repo / "seed.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)
    return repo


# ── unit: the helpers the fingerprint contract rests on ────────────────────────────────────

class TestHatchWriteOpFields:
    def test_edit_includes_all_fields_with_replace_all_default_false(self):
        fields = hatch_write_op_fields("Edit", {
            "file_path": "a.py", "old_string": "x", "new_string": "y",
        })
        assert fields == {
            "tool_name": "Edit", "file_path": "a.py",
            "old_string": "x", "new_string": "y", "replace_all": False,
        }

    def test_write_and_multiedit_shapes(self):
        assert hatch_write_op_fields("Write", {"file_path": "b.py", "content": "z"}) == {
            "tool_name": "Write", "file_path": "b.py", "content": "z",
        }
        edits = [{"old_string": "x", "new_string": "y"}]
        assert hatch_write_op_fields("MultiEdit", {"file_path": "c.py", "edits": edits}) == {
            "tool_name": "MultiEdit", "file_path": "c.py", "edits": edits,
        }

    def test_tool_name_case_insensitive(self):
        assert hatch_write_op_fields("edit", {"file_path": "a.py"})["tool_name"] == "edit"

    def test_unrecognized_write_tool_binds_at_path_granularity(self):
        assert hatch_write_op_fields("ApplyPatch", {"file_path": "d.py", "patch": "..."}) == {
            "tool_name": "ApplyPatch", "file_path": "d.py",
        }

    def test_unrecognized_write_tool_target_triple_includes_notebook_path(self):
        # A notebook-only event must NOT degrade to a branch-level hook-event digest — the
        # fallback scans the SAME file_path/path/notebook_path triple the descriptors use.
        assert hatch_write_op_fields("ApplyPatch", {"notebook_path": "n.ipynb"}) == {
            "tool_name": "ApplyPatch", "file_path": "n.ipynb",
        }

    def test_notebookedit_full_shape(self):
        fields = hatch_write_op_fields("NotebookEdit", {
            "notebook_path": "n.ipynb", "cell_id": "c1",
            "new_source": "print('x')", "edit_mode": "replace",
        })
        assert fields == {
            "tool_name": "NotebookEdit", "notebook_path": "n.ipynb", "cell_id": "c1",
            "new_source": "print('x')", "edit_mode": "replace",
        }

    def test_no_tool_and_no_file_path_gives_empty_fields(self):
        assert hatch_write_op_fields(None, {}) == {}


class TestCanonicalBashFingerprint:
    def test_inline_prefix_justification_or_token_or_bare_all_collapse(self):
        var = _ENV_VAR
        assert _canonical_bash_fingerprint(var, f'{var}="why here" git reset --hard') == "git reset --hard"
        assert _canonical_bash_fingerprint(var, f"{var}=approved:{_GRANT_ID} git reset --hard") == "git reset --hard"
        assert _canonical_bash_fingerprint(var, "git reset --hard") == "git reset --hard"

    def test_separator_tokens_are_kept_and_distinct(self):
        var = _ENV_VAR
        assert _canonical_bash_fingerprint(var, f'{var}="w" a && b') == "a && b"
        assert _canonical_bash_fingerprint(var, f'{var}="w" a; b') == "a ; b"
        assert _canonical_bash_fingerprint(var, f'{var}="w" a && b') != _canonical_bash_fingerprint(var, f'{var}="w" a; b')

    def test_other_leading_assignments_survive(self):
        var = _ENV_VAR
        assert _canonical_bash_fingerprint(var, f"A=1 {var}=x git commit") == "A=1 git commit"
        assert _canonical_bash_fingerprint(var, "A=1 git commit") == "A=1 git commit"

    def test_mid_segment_assignment_is_not_stripped(self):
        assert _canonical_bash_fingerprint(_ENV_VAR, "git {0}=x reset".format(_ENV_VAR)) != "git reset"

    def test_unbalanced_quotes_fall_back_to_raw_string(self):
        raw = 'echo "unterminated'
        assert _canonical_bash_fingerprint(_ENV_VAR, raw) == raw

    def test_quoted_vs_bare_argument_with_space_stay_distinct(self):
        var = _ENV_VAR
        assert _canonical_bash_fingerprint(var, 'git commit -m "fix bug"') == 'git commit -m "fix bug"'
        assert _canonical_bash_fingerprint(var, "git commit -m fix_bug") == "git commit -m fix_bug"
        assert _canonical_bash_fingerprint(var, 'git commit -m "fix bug"') != _canonical_bash_fingerprint(var, "git commit -m fix_bug")

    def test_single_vs_double_quoted_command_substitution_stay_distinct(self):
        """Quote style is SHELL SEMANTICS: '$(x)' passes a literal, "$(x)" EXECUTES it — a
        grant for one must never verify the other (review round 2, High)."""
        var = _ENV_VAR
        single = _canonical_bash_fingerprint(var, f"{var}=w git reset --hard -- '$(touch /tmp/pwn)'")
        double = _canonical_bash_fingerprint(var, f'{var}=w git reset --hard -- "$(touch /tmp/pwn)"')
        assert single == "git reset --hard -- '$(touch /tmp/pwn)'"
        assert double == 'git reset --hard -- "$(touch /tmp/pwn)"'
        assert single != double

    def test_quoted_literal_newline_never_collides_with_semicolon(self):
        """A newline INSIDE quotes is a literal character, an unquoted one a separator —
        `--exclude='keep\nalso'` and `--exclude='keep;also'` must fingerprint differently
        (review round 3, P1: the pre-tokenizer fold erased the distinction)."""
        var = _ENV_VAR
        with_newline = _canonical_bash_fingerprint(
            var, f"{var}=w git clean -fd --exclude='keep\nalso-keep'"
        )
        with_semicolon = _canonical_bash_fingerprint(
            var, f"{var}=w git clean -fd --exclude='keep;also-keep'"
        )
        assert with_newline == "git clean -fd --exclude='keep\nalso-keep'"
        assert with_semicolon == "git clean -fd --exclude='keep;also-keep'"
        assert with_newline != with_semicolon

    def test_unquoted_newline_and_continuation_still_normalize(self):
        var = _ENV_VAR
        assert _canonical_bash_fingerprint(var, f"{var}=w a\nb") == "a ; b"
        assert _canonical_bash_fingerprint(var, f"{var}=w a \\\nb") == "a b"


# ── token path: `approved:<id>` verify ─────────────────────────────────────────────────────

def test_approved_token_allows_and_reason_names_id(tmp_path):
    fake, ask = _ask(tmp_path, command="git reset --hard")
    assert ask.approved is True
    fake, result = _verify(tmp_path, command=f"{_ENV_VAR}=approved:{_GRANT_ID} git reset --hard", fake=fake)
    assert result.requested is True
    assert result.approved is True
    assert _GRANT_ID in result.reason
    assert fake.consumed.exists()


def test_approved_token_unknown_id_denies(tmp_path):
    fake, _res = _ask(tmp_path, command="git reset --hard")
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: "approved:hgr-nope888"}, command="git reset --hard",
        tg_ctl_candidates=[fake.path], timeout_s=2,
    )
    assert result.approved is False
    assert "hgr-nope888" in result.reason
    assert result.env_present is True


@pytest.mark.parametrize("value", ["approved:", "approved:bad!chars"])
def test_malformed_grant_reference_denies_without_contacting_tg_ctl(tmp_path, value):
    fake = _Fake(tmp_path)
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: value}, command="git reset --hard",
        tg_ctl_candidates=[fake.path], timeout_s=2,
    )
    assert result.approved is False
    assert "malformed" in result.reason
    assert fake.argvs() == []  # denied LOCALLY — tg-ctl never invoked


def test_whitespace_bearing_approved_sentence_is_a_justification_not_a_token(tmp_path):
    """`approved: per Alex tg#1234, safe to re-run` is a real sentence that merely starts with
    the word — it must NOT be treated as a fumbled grant id (review round 3); it posts a normal
    ask like any written justification."""
    fake = _Fake(tmp_path)
    _, result = _ask(
        tmp_path, command="git reset --hard", fake=fake,
        env={_ENV_VAR: "approved: per Alex tg#1234, safe to re-run"},
    )
    assert result.approved is True
    argvs = fake.argvs()
    assert len(argvs) == 1 and "--verify-grant" not in argvs[0]


def test_missing_tg_ctl_denies_naming_the_grant(tmp_path):
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: f"approved:{_GRANT_ID}"}, command="git reset --hard",
        tg_ctl_candidates=[tmp_path / "nowhere" / "tg-ctl"], timeout_s=2,
    )
    assert result.approved is False
    assert _GRANT_ID in result.reason
    assert result.env_present is True


def test_grant_is_single_use_second_consume_denies(tmp_path):
    fake, _res = _ask(tmp_path, command="git reset --hard")
    _, first = _verify(tmp_path, command="git reset --hard", fake=fake)
    assert first.approved is True
    _, second = _verify(tmp_path, command="git reset --hard", fake=fake)
    assert second.approved is False


def test_verify_unparseable_stdout_denies_forwarding_detail(tmp_path):
    fake = _write_raw_tg_ctl(tmp_path / "bin" / "tg-ctl", "printf 'not-json '\nexit 0\n")
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: f"approved:{_GRANT_ID}"}, command="git reset --hard",
        tg_ctl_candidates=[fake], timeout_s=2,
    )
    assert result.approved is False
    assert "unparseable" in result.reason
    assert "not-json" in result.reason


def test_verify_nonzero_exit_denies(tmp_path):
    fake = _write_raw_tg_ctl(tmp_path / "bin" / "tg-ctl", "exit 3\n")
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: f"approved:{_GRANT_ID}"}, command="git reset --hard",
        tg_ctl_candidates=[fake], timeout_s=2,
    )
    assert result.approved is False
    assert "exit 3" in result.reason
    assert _GRANT_ID in result.reason


@pytest.mark.parametrize("shape", ["top", "nested"])
def test_verify_deny_reason_names_approval_id_and_original_requestId(tmp_path, shape):
    """Contract v1.2 item 3: the parsed-deny branch must surface BOTH the approval_id and the
    daemon reply's original requestId, tolerantly (top-level OR nested under
    hookSpecificOutput)."""

    fake = _Fake(tmp_path)
    _, ask = _ask(tmp_path, command="git reset --hard", fake=fake)
    assert ask.approved is True
    # Re-seed the knob AFTER the priming ask (the fake's ask branch re-records state wholesale,
    # which is exactly what would otherwise drop it).
    rec = fake.recorded()
    rec["denyShape"] = shape
    fake.state.write_text(json.dumps(rec))
    # Verify with a DIFFERENT id → the fake's unknown-grant deny carries the original requestId.
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: "approved:hgr-does-not-match"}, command="git reset --hard",
        tg_ctl_candidates=[fake.path], timeout_s=2,
    )
    assert result.approved is False
    assert "hgr-does-not-match" in result.reason
    assert "hatch-block-reset-hard-orig123" in result.reason


# ── enriched ask payload (v1 §2) ───────────────────────────────────────────────────────────

def test_ask_payload_carries_hook_timeout_origin_and_operation(tmp_path):
    fake = _Fake(tmp_path)
    _, result = _ask(
        tmp_path, command="git reset --hard", fake=fake,
        env={_ENV_VAR: "genuine justification", "RIG_AGENT_ID": "at-late-approval-d",
             "TG_AGENT": "codex", "CLAUDE_SESSION_ID": "sess-42"},
    )
    assert result.approved is True
    payload = fake.recorded()
    assert payload["hookId"] == _HOOK
    assert payload["timeoutMs"] == 600_000
    assert payload["origin"]["agent"] == "codex"
    assert payload["origin"]["sessionId"] == "sess-42"
    assert payload["origin"]["subagent"] == "at-late-approval-d"
    op = payload["operation"]
    assert op["kind"] == "bash-command"
    assert op["fingerprint"] == "git reset --hard"
    assert op["cwd"] == os.path.abspath(str(tmp_path / "w"))
    assert op["repoRoot"] == "/repo/root"
    # The verify argv form is exactly the contract's.
    assert fake.argvs()[0][:2] == ["ask", "--agent"]


def test_repo_root_resolved_against_a_real_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(agenttools_hatch_escalation, "_git_repo_root", _REAL_GIT_REPO_ROOT)
    repo = _make_git_repo(tmp_path)
    fake, _ = _ask(tmp_path, cwd=str(repo), command="git status", fake=_Fake(tmp_path))
    assert fake.recorded()["operation"]["repoRoot"] == str(repo.resolve())


def test_expired_ask_denies_with_the_late_approval_retry_hint(tmp_path):
    fake = _write_raw_tg_ctl(
        tmp_path / "bin" / "tg-ctl",
        "printf 'ask-forward expired: posted messageId=7 - no decision within 115s' >&2\nexit 0\n",
    )
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: "genuine justification"}, command="git reset --hard",
        tg_ctl_candidates=[fake], timeout_s=2,
    )
    assert result.approved is False
    assert "ask-forward expired" in result.reason
    assert "approved:<id>" in result.reason
    assert "RIG_HATCH_REQUEST_<HOOK>" in result.reason


# ── bash fingerprint binding end to end ────────────────────────────────────────────────────

def test_inline_justification_ask_then_inline_approved_retry_match(tmp_path):
    fake, _ = _ask(tmp_path, command=f'{_ENV_VAR}="why here" git reset --hard')
    _, result = _verify(tmp_path, command=f"{_ENV_VAR}=approved:{_GRANT_ID} git reset --hard", fake=fake)
    assert result.approved is True


def test_bare_command_ask_then_inline_approved_retry_match(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard", env={_ENV_VAR: "why here"})
    _, result = _verify(tmp_path, command=f"{_ENV_VAR}=approved:{_GRANT_ID} git reset --hard", fake=fake)
    assert result.approved is True


def test_different_command_denies_without_consuming_then_original_still_allows(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard")
    _, changed = _verify(tmp_path, command=f"{_ENV_VAR}=approved:{_GRANT_ID} git clean -fdx", fake=fake)
    assert changed.approved is False
    assert not fake.consumed.exists()  # mismatch denied WITHOUT consuming
    _, original = _verify(tmp_path, command="git reset --hard", fake=fake)
    assert original.approved is True


def test_different_cwd_denies_without_consuming(tmp_path):
    fake, _ = _ask(tmp_path, cwd=str(tmp_path / "w"), command="git reset --hard")
    _, moved = _verify(tmp_path, cwd=str(tmp_path / "other"), command="git reset --hard", fake=fake)
    assert moved.approved is False
    assert not fake.consumed.exists()


def test_quote_style_change_denies_without_consuming(tmp_path):
    """'$(...)' is a literal argument, "$(...)" EXECUTES at expansion — the retry changing the
    quoting is a materially different operation and must not consume the grant."""
    fake, _ = _ask(tmp_path, command=f"{_ENV_VAR}='why' git reset --hard -- '$(touch /tmp/pwn)'")
    _, retried = _verify(
        tmp_path,
        command=f'{_ENV_VAR}=approved:{_GRANT_ID} git reset --hard -- "$(touch /tmp/pwn)"',
        fake=fake,
    )
    assert retried.approved is False
    assert not fake.consumed.exists()


def test_process_env_justification_beats_inline_approved_token(tmp_path):
    fake = _Fake(tmp_path)
    _, result = _ask(
        tmp_path, command=f"{_ENV_VAR}=approved:{_GRANT_ID} git reset --hard", fake=fake,
        env={_ENV_VAR: "genuine justification"},
    )
    assert result.approved is True
    argvs = fake.argvs()
    assert len(argvs) == 1
    assert "--verify-grant" not in argvs[0]  # a NORMAL ask ran; the inline token was ignored


# ── pre-write sha256 digest binding (v1.2 item 2a) ────────────────────────────────────────

_EDIT_CTX = {
    "hook": _HOOK, "tool_name": "Edit", "file_path": "src/x.py",
    "old_string": "OLD-7f3a-sync", "new_string": "NEW-7f3a-sync", "replace_all": False,
}


def _expected_edit_digest() -> str:
    return _canonical_digest({
        "tool_name": "Edit", "file_path": "src/x.py",
        "old_string": "OLD-7f3a-sync", "new_string": "NEW-7f3a-sync", "replace_all": False,
    })


def test_file_write_ask_records_sha256_digest_and_question_hides_content(tmp_path):
    fake, result = _ask(tmp_path, context=dict(_EDIT_CTX))
    assert result.approved is True
    payload = fake.recorded()
    op = payload["operation"]
    assert op["kind"] == "file-write"
    assert op["fingerprint"] == _expected_edit_digest()
    assert op["fingerprint"].startswith("sha256:")
    # Raw edit content never leaves the hook process: not in the payload JSON at all except as
    # the digest, and not rendered into the Telegram question.
    assert "OLD-7f3a-sync" not in payload["question"]
    assert "NEW-7f3a-sync" not in payload["question"]


def test_byte_identical_edit_retry_consumes_once(tmp_path):
    fake, _ = _ask(tmp_path, context=dict(_EDIT_CTX))
    _, retry = _verify(tmp_path, context=dict(_EDIT_CTX), fake=fake)
    assert retry.approved is True


def test_changed_new_string_same_path_denies_without_consuming(tmp_path):
    fake, _ = _ask(tmp_path, context=dict(_EDIT_CTX))
    changed = dict(_EDIT_CTX, new_string="NEW-different")
    _, denied = _verify(tmp_path, context=changed, fake=fake)
    assert denied.approved is False
    assert not fake.consumed.exists()
    _, still = _verify(tmp_path, context=dict(_EDIT_CTX), fake=fake)
    assert still.approved is True  # the untouched grant still covers the ORIGINAL edit


def test_changed_old_string_same_new_string_denies_without_consuming(tmp_path):
    fake, _ = _ask(tmp_path, context=dict(_EDIT_CTX))
    changed = dict(_EDIT_CTX, old_string="OLD-other")
    _, denied = _verify(tmp_path, context=changed, fake=fake)
    assert denied.approved is False
    assert not fake.consumed.exists()


def test_fallback_fingerprint_no_file_target_is_canonical_context_digest(tmp_path):
    ctx = {"hook": "background-subagent-gate", "agent_id": "sub-7", "description": "run e2e"}
    fake, result = _ask(tmp_path, context=ctx)
    assert result.approved is True
    op = fake.recorded()["operation"]
    assert op["kind"] == "hook-event"
    assert op["fingerprint"] == _canonical_digest({"hook": _HOOK, "context": ctx})


# ── PULL path (v1.2 item 4) ────────────────────────────────────────────────────────────────

def test_pull_with_matching_route_consumes_once(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard", env={_ENV_VAR: "legit reason", "TMUX_PANE": "%3"})
    _, pulled = _pull(tmp_path, command="git reset --hard", env={"TMUX_PANE": "%3"}, fake=fake)
    assert pulled.approved is True
    assert pulled.grant_pull is True
    assert _GRANT_ID in pulled.reason
    assert "original request hatch-block-reset-hard-" in pulled.reason  # the ORIGINAL ask's id
    assert fake.consumed.exists()
    # Single-use: the second pull finds nothing and the plain not-requested deny returns.
    _, again = _pull(tmp_path, command="git reset --hard", env={"TMUX_PANE": "%3"}, fake=fake)
    assert again.approved is False
    assert again.requested is False
    assert again.env_present is False


def test_verify_generic_ask_allow_shape_denies_old_daemon_cannot_vouch(tmp_path):
    """An OLD/fake tg-ctl that ignores `--verify-grant` degrades the call to a plain ask and
    replies with the GENERIC ask-allow shape (no original requestId, no echoed approvalId) —
    a human tapping Allow on the spurious card must NOT authorize the operation."""

    fake = _write_raw_tg_ctl(tmp_path / "bin" / "tg-ctl", _ALLOW_REPLY_SH)
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: f"approved:{_GRANT_ID}"}, command="git reset --hard",
        tg_ctl_candidates=[fake], timeout_s=2,
    )
    assert result.approved is False
    assert _GRANT_ID in result.reason
    assert "grant-consume" in result.reason


def test_verify_allow_with_requestId_but_no_approvalId_echo_denies(tmp_path):
    """Stricter half of the grant-shape guard (review round 3): a reply echoing a requestId but
    NOT the approval id is still not proof the NAMED grant was consumed — deny."""

    reply = (
        'printf \'{"requestId":"hatch-block-reset-hard-orig123",'
        '"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
        '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
    )
    fake = _write_raw_tg_ctl(tmp_path / "bin" / "tg-ctl", reply)
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: f"approved:{_GRANT_ID}"}, command="git reset --hard",
        tg_ctl_candidates=[fake], timeout_s=2,
    )
    assert result.approved is False
    assert "grant-consume" in result.reason


def test_pull_ordinary_no_match_leaves_deny_unchanged(tmp_path):
    fake = _Fake(tmp_path)  # NO ask → empty grant store
    _, result = _pull(tmp_path, command="git reset --hard", env={"TMUX_PANE": "%3"}, fake=fake)
    assert result.requested is False
    assert result.env_present is False
    assert result.should_stop is False
    assert "not set" in result.reason
    assert not fake.consumed.exists()


def test_pull_runs_once_per_process_for_same_hook_and_cwd(tmp_path):
    """The per-process negative memo: a multi-segment gated command (block-reset-hard consults
    the hatch per dangerous segment) must pull ONCE per (hook, cwd), not once per segment."""

    fake = _Fake(tmp_path)  # empty grant store → every pull comes back empty
    for _ in range(3):
        _, result = _pull(tmp_path, command="git reset --hard", env={"TMUX_PANE": "%3"}, fake=fake)
        assert result.requested is False
    pulls = [argv for argv in fake.argvs() if "--pull" in argv]
    assert len(pulls) == 1


def test_pull_false_kwarg_opts_advisory_consults_out(tmp_path):
    """Advisory-tier consults (a hook checking the hatch BEFORE deciding warn vs block) pass
    pull=False so an early-session advisory check does not pay a daemon lookup per event."""

    fake = _Fake(tmp_path)
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={}, command="git reset --hard",
        tg_ctl_candidates=[fake.path], timeout_s=2, pull=False,
    )
    assert result.requested is False
    assert fake.argvs() == []


def test_blank_hatch_value_suppresses_the_pull(tmp_path):
    """A present-but-blank value is an INVALID request, not an absent one — it must deny
    locally without adding a pull daemon query (pins `if raw is None` against `if not raw`)."""

    fake = _Fake(tmp_path)
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={_ENV_VAR: "  "}, command="git reset --hard",
        tg_ctl_candidates=[fake.path], timeout_s=2,
    )
    assert result.requested is False
    assert result.env_present is True
    assert "blank" in result.reason
    assert fake.argvs() == []


def test_pull_route_mismatch_denies_without_consuming_or_surfaces_nothing(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard", env={_ENV_VAR: "legit reason", "TMUX_PANE": "%3"})
    _, other = _pull(tmp_path, command="git reset --hard", env={"TMUX_PANE": "%9"}, fake=fake)
    assert other.requested is False  # ordinary mismatch deny → unchanged not-requested result
    assert not fake.consumed.exists()


def test_pull_no_stable_route_surfaces_daemon_guidance_verbatim(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard", env={_ENV_VAR: "legit reason"})  # no TMUX_PANE
    _, result = _pull(tmp_path, command="git reset --hard", env={}, fake=fake)
    assert result.requested is True
    assert result.approved is False
    assert result.env_present is True  # should_stop: the surfaced deny suppresses fallbacks
    assert "RIG_HATCH_REQUEST_" in result.reason
    assert "approved:" in result.reason
    assert not fake.consumed.exists()


def test_pull_skipped_when_env_token_present(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard", env={_ENV_VAR: "legit reason", "TMUX_PANE": "%3"})
    _, result = _verify(tmp_path, command="git reset --hard",
                        env={_ENV_VAR: f"approved:{_GRANT_ID}", "TMUX_PANE": "%3"}, fake=fake)
    assert result.approved is True
    argvs = fake.argvs()
    assert len(argvs) == 2  # ask + verify — NO --pull invocation ever ran
    assert all("--pull" not in argv for argv in argvs)


def test_pull_fails_closed_when_tg_ctl_missing(tmp_path):
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={}, command="git reset --hard",
        tg_ctl_candidates=[tmp_path / "nowhere" / "tg-ctl"], timeout_s=2,
    )
    assert result.requested is False
    assert result.env_present is False
    assert "not set" in result.reason


def test_token_path_is_route_agnostic_cross_route_consume_allowed(tmp_path):
    fake, _ = _ask(tmp_path, command="git reset --hard", env={_ENV_VAR: "legit reason", "TMUX_PANE": "%5"})
    _, result = _verify(tmp_path, command="git reset --hard",
                        env={_ENV_VAR: f"approved:{_GRANT_ID}", "TMUX_PANE": "%9"}, fake=fake)
    assert result.approved is True


def test_pull_timeout_fails_closed_to_unchanged_deny(tmp_path, monkeypatch):
    # Bound the pull short for the test (a real `sleep 30` fake would otherwise burn the full
    # 15s grant-lookup budget in wall-clock — the bound itself is what's under test).
    monkeypatch.setattr(agenttools_hatch_escalation, "_GRANT_LOOKUP_TIMEOUT_S", 1.0)
    fake = _write_raw_tg_ctl(tmp_path / "bin" / "tg-ctl", "sleep 30\n")
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={}, command="git reset --hard",
        tg_ctl_candidates=[fake], timeout_s=2,
    )
    assert result.requested is False
    assert result.env_present is False


# ── audit line shape ───────────────────────────────────────────────────────────────────────

def _read_log(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


def test_audit_token_allow_and_deny_carry_approval_id(tmp_path):
    log = tmp_path / "overrides.log"
    fake, _ = _ask(tmp_path, command="git reset --hard", fake=_Fake(tmp_path),
                   overrides_log_path=log)
    call = lambda **kw: request_hatch_approval(  # noqa: E731 - test-local shorthand
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        command="git reset --hard", tg_ctl_candidates=[fake.path], timeout_s=2,
        overrides_log_path=log, **kw,
    )
    call(env={_ENV_VAR: f"approved:{_GRANT_ID}"})
    call(env={_ENV_VAR: "approved:hgr-nope888"})
    entries = _read_log(log)
    assert [e["decision"] for e in entries] == ["approved", "approved-late", "denied"]
    assert entries[1]["approval_id"] == _GRANT_ID
    assert entries[2]["approval_id"] == "hgr-nope888"
    assert all(e["hatch"] == _HOOK for e in entries)


def test_audit_pull_allow_uses_approved_late_pull_decision(tmp_path):
    log = tmp_path / "overrides.log"
    fake, _ = _ask(tmp_path, command="git reset --hard",
                   env={_ENV_VAR: "legit reason", "TMUX_PANE": "%3"},
                   overrides_log_path=log)
    result = request_hatch_approval(
        _HOOK, {"command": "git reset --hard"}, cwd=str(tmp_path / "w"),
        env={"TMUX_PANE": "%3"}, command="git reset --hard",
        tg_ctl_candidates=[fake.path], timeout_s=2, overrides_log_path=log,
    )
    assert result.approved is True
    entries = _read_log(log)
    assert entries[-1]["decision"] == "approved-late-pull"
    assert entries[-1]["approval_id"] == _GRANT_ID


# ── the two authorized hook-side context enrichments ───────────────────────────────────────

_WOW_ENV_KEYS = (
    "RIG_WORKTREE_ONLY", "RIG_ALLOW_MAIN_EDIT", "RIG_ALLOW_MAIN_EDIT_REASON",
    "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES",
)


def test_worktree_only_writes_threads_edit_fields_into_hatch_context(tmp_path, monkeypatch):
    """The hook passes the event's tool + exact tool_input fields through `hatch_write_op_fields`
    so the lib's file-write fingerprint is the sha256 of THIS byte-exact edit (agent-tools#742),
    and the raw content stays out of the Telegram question."""

    repo = _make_git_repo(tmp_path)
    target = repo / "src" / "a.ts"
    payload_file = tmp_path / "wow-payload.json"
    tg_ctl = _write_raw_tg_ctl(
        tmp_path / "bin" / "tg-ctl",
        f'cat > "{payload_file}"\n' + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    event = {
        "cwd": str(repo), "tool": "Edit",
        "args": {"file_path": str(target), "old_string": "OLD-7f3a-sync",
                 "new_string": "NEW-7f3a-sync"},
    }
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for key in _WOW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    monkeypatch.setenv(
        "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES",
        "hotfix, worktree overkill for this one edit",
    )
    code = wow.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"
    payload = json.loads(payload_file.read_text())
    expected = wow.hatch_escalation._canonical_digest({
        "tool_name": "Edit", "file_path": str(target),
        "old_string": "OLD-7f3a-sync", "new_string": "NEW-7f3a-sync", "replace_all": False,
    })
    assert payload["operation"]["kind"] == "file-write"
    assert payload["operation"]["fingerprint"] == expected
    assert "OLD-7f3a-sync" not in payload["question"]
    assert "NEW-7f3a-sync" not in payload["question"]


def test_orchestrator_stays_thin_pre_write_threads_edit_fields(tmp_path, monkeypatch):
    payload_file = tmp_path / "ost-payload.json"
    tg_ctl = _write_raw_tg_ctl(
        tmp_path / "bin" / "tg-ctl",
        f'cat > "{payload_file}"\n' + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(ost.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))

    marker_dir = tmp_path / "markers"
    event = {
        "point": "pre-write", "cwd": "/repo", "tool": "Edit",
        "args": {"file_path": "/repo/src/a.ts", "old_string": "OLD-7f3a-sync",
                 "new_string": "NEW-7f3a-sync"},
    }

    def _run(env: dict | None = None) -> tuple[str, int]:
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        monkeypatch.setenv("ORCH_THIN_MARKER_DIR", str(marker_dir))
        monkeypatch.setattr(ost, "MARKER_DIR", marker_dir)
        for key in ("ALLOW_ORCHESTRATOR_WORK", "ALLOW_ORCHESTRATOR_WORK_REASON",
                    "RIG_ORCHESTRATOR_ONLY", "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN",
                    "RIG_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE"):
            monkeypatch.delenv(key, raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        code = ost.main()
        return out.getvalue(), code

    _run()  # prime the warn marker
    out, code = _run({"RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN":
                      "One-char fix in a generated file."})
    assert code == 0
    assert json.loads(out)["decision"] == "allow"
    payload = json.loads(payload_file.read_text())
    expected = ost.hatch_escalation._canonical_digest({
        "tool_name": "Edit", "file_path": "/repo/src/a.ts",
        "old_string": "OLD-7f3a-sync", "new_string": "NEW-7f3a-sync", "replace_all": False,
    })
    assert payload["operation"]["kind"] == "file-write"
    assert payload["operation"]["fingerprint"] == expected
    assert "OLD-7f3a-sync" not in payload["question"]
    assert "NEW-7f3a-sync" not in payload["question"]


def test_worktree_only_writes_consumes_a_grant_via_real_pull_path(tmp_path, monkeypatch):
    """End-to-end pre-write PULL through the real hook `main()`: the ask records the enriched
    operation (ask path, exported var + TMUX_PANE), then — hatch var cleared — an identical
    retry is consumed automatically by the pull. Un-sets the conftest seam var for this one
    test so the hook's candidate-less pull actually reaches the fake trusted tg-ctl."""

    repo = _make_git_repo(tmp_path)
    target = repo / "src" / "a.ts"
    fake = _Fake(tmp_path)
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (fake.path,))
    monkeypatch.delenv("AGENTTOOLS_TEST_HERMETIC_HOME", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%3")
    event = {
        "cwd": str(repo), "tool": "Edit",
        "args": {"file_path": str(target), "old_string": "OLD-7f3a-sync",
                 "new_string": "NEW-7f3a-sync"},
    }

    def _run(hatch_env: str | None) -> tuple[str, int]:
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        for key in _WOW_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
        if hatch_env is not None:
            monkeypatch.setenv("RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES", hatch_env)
        code = wow.main()
        return out.getvalue(), code

    # 1. The original denied write asks via the exported var (allow reply records the grant).
    out, code = _run("hotfix, worktree overkill for this one edit")
    assert code == 0 and json.loads(out)["decision"] == "allow"
    # 2. The retry: hatch var UNSET — the lib's pull matches route + operation and consumes.
    out, code = _run(None)
    assert code == 0 and json.loads(out)["decision"] == "allow"
    assert fake.consumed.exists()
    pulls = [argv for argv in fake.argvs() if "--pull" in argv]
    assert len(pulls) == 1


def test_hooks_without_a_file_target_fall_back_to_context_digest(tmp_path, monkeypatch):
    """background-subagent-gate / subagent-no-monitor pass no file target: the enrichment adds
    nothing, and the lib's fallback (canonical context JSON digest) keeps the binding
    deterministic across an ask/retry of the same event."""

    ctx = {"hook": "subagent-no-monitor", "description": "watch build", "command": "npm run w"}
    fake, result = _ask(tmp_path, context=ctx)
    assert result.approved is True
    op = fake.recorded()["operation"]
    assert op["kind"] == "hook-event"
    assert op["fingerprint"] == _canonical_digest({"hook": _HOOK, "context": ctx})


# ── P1 (2026-09-10 amendment): background-subagent-gate dispatch binding ────────────────────
# A grant minted for one blocked dispatch must NOT be consumable by a LATER, UNRELATED blocked
# dispatch behind the same hook_id. The gate threads its normalized dispatch identity through
# `hatch_dispatch_op_fields`; the lib's hook-event branch binds the fingerprint to it.


_BG_HOOK = "background-subagent-gate"
_BG_ENV_VAR = hatch_env_var(_BG_HOOK)
_BG_PROMPT = (
    "PROMPT-9d3f-raw read the shard allocation table, move every cold shard to region B, "
    "rebalance the hot replicas so no node exceeds seventy percent utilization, then report "
    "per-region counts, the total bytes moved, and any shards that could not be relocated."
)
_BSG_ENV_KEYS = (
    "RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE", "RIG_AGENT_ID", "RIG_DETACHED_AGENT",
    "PI_SESSION_FILE", "RIG_SESSION_ID",
)


def _dispatch_ctx(**overrides) -> dict:
    """The ctx background-subagent-gate builds after the P1 enrichment: hook id plus the
    normalized dispatch fields from `hatch_dispatch_op_fields`."""
    args = {
        "subagent_type": "general",
        "description": "Rebalance the shard allocator across regions",
        "prompt": _BG_PROMPT,
    }
    args.update(overrides)
    ctx: dict = {"hook": _BG_HOOK}
    ctx.update(agenttools_hatch_escalation.hatch_dispatch_op_fields("Task", args))
    return ctx


def _bg_call(tmp_path: Path, ctx: dict, *, env: dict, fake: _Fake | None = None):
    fake = fake or _Fake(tmp_path)
    result = request_hatch_approval(
        _BG_HOOK, ctx, cwd=str(tmp_path / "w"), env=env,
        tg_ctl_candidates=[fake.path], timeout_s=2, process_margin_s=1,
    )
    return fake, result


def test_bsg_changed_prompt_denies_without_consuming(tmp_path):
    fake, _ = _bg_call(tmp_path, _dispatch_ctx(),
                       env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat"})
    changed = _dispatch_ctx(prompt=_BG_PROMPT + " Also rotate the keys.")
    _, denied = _bg_call(tmp_path, changed, env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert denied.approved is False
    assert not fake.consumed.exists()
    # The untouched grant still covers the ORIGINAL dispatch.
    _, still = _bg_call(tmp_path, _dispatch_ctx(),
                        env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert still.approved is True


def test_bsg_changed_subagent_type_same_prompt_denies_without_consuming(tmp_path):
    fake, _ = _bg_call(tmp_path, _dispatch_ctx(),
                       env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat"})
    changed = _dispatch_ctx(subagent_type="explore")
    _, denied = _bg_call(tmp_path, changed, env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert denied.approved is False
    assert not fake.consumed.exists()


def test_bsg_byte_identical_dispatch_retry_consumes_once(tmp_path):
    fake, _ = _bg_call(tmp_path, _dispatch_ctx(),
                       env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat"})
    _, retry = _bg_call(tmp_path, _dispatch_ctx(),
                        env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert retry.approved is True
    assert fake.consumed.exists()
    _, again = _bg_call(tmp_path, _dispatch_ctx(),
                        env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert again.approved is False  # single-use


def test_bsg_ask_digest_binds_dispatch_and_question_hides_prompt(tmp_path):
    ctx = _dispatch_ctx()
    fake, result = _bg_call(tmp_path, ctx,
                            env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat"})
    assert result.approved is True
    payload = fake.recorded()
    op = payload["operation"]
    assert op["kind"] == "hook-event"
    assert op["fingerprint"] == _canonical_digest({"hook": _BG_HOOK, "context": ctx})
    # Raw dispatch text never renders into the Telegram card — digest strings only.
    assert "PROMPT-9d3f-raw" not in payload["question"]
    assert "Rebalance the shard allocator" not in payload["question"]


def test_hatch_dispatch_op_fields_normalization():
    op_fields = agenttools_hatch_escalation.hatch_dispatch_op_fields
    fields = op_fields(
        "  Task  ",
        {"subagent_type": " general ", "agent": "", "description": None, "prompt": "do it",
         "task": "  ", "isolation": "local", "run_in_background": None},
    )
    assert fields == {
        "tool_name": "Task", "subagent_type": "general", "prompt": "do it",
        "isolation": "local", "run_in_background": False,
    }
    assert op_fields(None, {"run_in_background": True}) == {"run_in_background": True}
    assert op_fields("", {"run_in_background": "TRUE"}) == {"run_in_background": True}
    assert op_fields("Task", {"run_in_background": "false"})["run_in_background"] is False
    assert op_fields("Task", {"run_in_background": "yes please"})["run_in_background"] is False


def test_background_subagent_gate_pull_binds_dispatch_identity_e2e(tmp_path, monkeypatch):
    """End-to-end through the REAL gate main(): a grant minted for one blocked dispatch is
    NOT pulled by a CHANGED prompt behind the same hook (the P1), while the byte-identical
    retry consumes it. Each _run clears the lib's per-process pull memo — a real hook process
    is one-shot, so every event is a fresh process."""

    fake = _Fake(tmp_path)
    monkeypatch.setattr(bsg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (fake.path,))
    monkeypatch.delenv("AGENTTOOLS_TEST_HERMETIC_HOME", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%3")
    event = {
        "harness": "claude-code", "cwd": str(tmp_path / "w"), "tool": "Task",
        "args": {
            "subagent_type": "general",
            "description": "Rebalance the shard allocator across regions",
            "prompt": _BG_PROMPT,
        },
    }

    def _run(hatch_env: str | None) -> tuple[str, int]:
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        for key in _BSG_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        bsg.hatch_escalation._PULL_NEGATIVE.clear()  # fresh one-shot hook process
        if hatch_env is not None:
            monkeypatch.setenv(_BG_ENV_VAR, hatch_env)
        code = bsg.main()
        return out.getvalue(), code

    # 1. The original blocked dispatch asks via the exported var (allow reply records the grant).
    out, code = _run("one-off foreground dispatch, human pre-approved in chat")
    assert code == 0 and json.loads(out)["decision"] == "allow"
    # 2. CHANGED prompt, hatch var unset: the pull consults the daemon, must NOT match →
    #    block, grant unconsumed.
    event["args"]["prompt"] = _BG_PROMPT + " Also rotate the keys."
    out, code = _run(None)
    assert code == bsg.BLOCK_EXIT_CODE and json.loads(out)["decision"] == "block"
    assert not fake.consumed.exists()
    pulls = [argv for argv in fake.argvs() if "--pull" in argv]
    assert len(pulls) == 1  # the changed dispatch DID consult — and was denied
    assert "PROMPT-9d3f-raw" not in fake.recorded()["question"]
    # 3. Byte-identical retry: the pull matches route + dispatch identity and consumes.
    event["args"]["prompt"] = _BG_PROMPT
    out, code = _run(None)
    assert code == 0 and json.loads(out)["decision"] == "allow"
    assert fake.consumed.exists()


# ── Ruling 2 (2026-09-12): grant paths fail closed on weak dispatch binding ────────────────
# Belt-and-suspenders, BOTH layers required: (a) the GATE denies before any lib call when the
# op-fields helper is missing (older lib / _UnavailableHatch) or yields no usable identity;
# (b) the LIB rejects a dispatch-bound payload carrying no identity on ask/verify/pull. No
# weak-binding path may exist: no Telegram ask under hook-only granularity, no consume.


class _OldLibNoDispatchHelper:
    """An OLDER lib copy (pre-P1): real-shaped `request_hatch_approval` but NO
    `hatch_dispatch_op_fields`. It records every call and would ALLOW — so if the gate ever
    degraded to weak binding and consulted it, the allow (exit 0) would surface the bug
    loudly instead of a silent weak ask."""

    def __init__(self) -> None:
        self.calls = 0

    def request_hatch_approval(self, *args, **kwargs):
        self.calls += 1
        return agenttools_hatch_escalation.HatchApprovalResult(
            requested=True,
            approved=True,
            reason="old-lib stub allowed (a weak-binding path BUG if this surfaces)",
            env_var=_BG_ENV_VAR,
            env_present=True,
        )


def _run_bsg(monkeypatch, event: dict, *, hatch_env: str | None) -> tuple[str, int]:
    """Run the REAL gate main() on `event` with the hatch env scrubbed/seeded per call (a
    real hook process is one-shot; monkeypatch restores everything between calls)."""

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for key in _BSG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    if hatch_env is not None:
        monkeypatch.setenv(_BG_ENV_VAR, hatch_env)
    code = bsg.main()
    return out.getvalue(), code


@pytest.mark.parametrize(
    "hatch_value",
    [
        "one-off foreground dispatch, human asked in chat",
        f"approved:{_GRANT_ID}",
    ],
    ids=["ask-justification", "verify-token"],
)
def test_bsg_old_lib_without_helper_env_set_denies_closed_no_tg_ctl(
    tmp_path, monkeypatch, hatch_value
):
    """Ruling 2 test (g), ask AND verify flavors: a lib without `hatch_dispatch_op_fields`
    plus a hatch env value → the gate denies naming the missing binding; the lib is never
    consulted and tg-ctl never invoked, so nothing is asked or consumed."""

    stub = _OldLibNoDispatchHelper()
    monkeypatch.setattr(bsg, "hatch_escalation", stub)
    fake = _Fake(tmp_path)
    out, code = _run_bsg(
        monkeypatch,
        {
            "harness": "claude-code", "cwd": str(tmp_path / "w"), "tool": "Task",
            "args": {"subagent_type": "general", "description": "Rebalance shards",
                     "prompt": _BG_PROMPT},
        },
        hatch_env=hatch_value,
    )
    assert code == bsg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    # The deny reason NAMES the missing binding (ruling 2 refinement item 3) — not a generic
    # failure, so a refactor that re-widens the fallback breaks here loudly.
    assert "too old" in payload["message"]
    assert "hatch_dispatch_op_fields" in payload["message"]
    assert "no Telegram ask was posted" in payload["message"]
    assert stub.calls == 0  # the gate never consulted the lib — no weak ask/verify/pull
    assert fake.argvs() == []  # tg-ctl never invoked
    assert not fake.consumed.exists()  # nothing consumed


def test_bsg_old_lib_without_helper_env_absent_blocks_without_weak_pull(tmp_path, monkeypatch):
    """Ruling 2 test (g), pull flavor: with NO hatch env value the old-lib gate skips the lib
    entirely (no weak pull can fire) and today's default block proceeds — #547 fail-open
    semantics survive for the gate's NON-hatch behavior only."""

    stub = _OldLibNoDispatchHelper()
    monkeypatch.setattr(bsg, "hatch_escalation", stub)
    fake = _Fake(tmp_path)
    out, code = _run_bsg(
        monkeypatch,
        {
            "harness": "claude-code", "cwd": str(tmp_path / "w"), "tool": "Task",
            "args": {"subagent_type": "general", "description": "Rebalance shards",
                     "prompt": _BG_PROMPT},
        },
        hatch_env=None,
    )
    assert code == bsg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "too old" not in payload["message"]  # plain reminder — today's semantics
    assert stub.calls == 0  # the lib was skipped: no weak pull query was even attempted
    assert fake.argvs() == []
    assert not fake.consumed.exists()


def test_bsg_unavailable_hatch_stub_env_set_denies_closed(tmp_path, monkeypatch):
    """Ruling 2 test (h): the import-failure `_UnavailableHatch` stub plus a hatch env value
    → the gate denies closed naming the lib as too old (the stub's should_stop=False
    fail-open must apply to NON-hatch behavior only)."""

    monkeypatch.setattr(bsg, "hatch_escalation", bsg._UnavailableHatch())
    fake = _Fake(tmp_path)
    out, code = _run_bsg(
        monkeypatch,
        {
            "harness": "claude-code", "cwd": str(tmp_path / "w"), "tool": "Task",
            "args": {"subagent_type": "general", "description": "Rebalance shards",
                     "prompt": _BG_PROMPT},
        },
        hatch_env="one-off foreground dispatch, human asked in chat",
    )
    assert code == bsg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "too old" in payload["message"]
    assert "hatch_dispatch_op_fields" in payload["message"]
    assert fake.argvs() == []
    assert not fake.consumed.exists()


def test_bsg_unavailable_hatch_stub_env_absent_keeps_default_block(tmp_path, monkeypatch):
    """The `_UnavailableHatch` stub with NO hatch value keeps today's plain block (the #547
    fail-open contract for the gate's non-hatch classification), with no weak pull."""

    monkeypatch.setattr(bsg, "hatch_escalation", bsg._UnavailableHatch())
    fake = _Fake(tmp_path)
    out, code = _run_bsg(
        monkeypatch,
        {
            "harness": "claude-code", "cwd": str(tmp_path / "w"), "tool": "Task",
            "args": {"subagent_type": "general", "description": "Rebalance shards",
                     "prompt": _BG_PROMPT},
        },
        hatch_env=None,
    )
    assert code == bsg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "too old" not in payload["message"]
    assert fake.argvs() == []


def test_bsg_dispatch_event_without_identity_env_set_denies_closed(tmp_path, monkeypatch):
    """Helper PRESENT but the event yields no usable dispatch identity (no tool, no
    subagent_type/description/prompt): the gate denies naming the unavailable identity —
    never a hook-only ask. Reaches the hatch consult because a dispatch with no
    prompt/description is not claimable as trivial."""

    fake = _Fake(tmp_path)
    monkeypatch.setattr(bsg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (fake.path,))
    out, code = _run_bsg(
        monkeypatch,
        {"harness": "claude-code", "cwd": str(tmp_path / "w"),
         "args": {"command": "npm run build"}},
        hatch_env="one-off foreground dispatch, human asked in chat",
    )
    assert code == bsg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "no usable dispatch identity" in payload["message"]
    assert "no Telegram ask was posted" in payload["message"]
    assert fake.argvs() == []  # the real lib was never consulted either
    assert not fake.consumed.exists()


# Lib-side layer (b): the shared lib itself rejects a dispatch-bound payload with no
# identity — an older or partial caller cannot mint or consume weakly even if the gate-side
# check is missing or bypassed.


def test_lib_rejects_dispatch_bound_ask_without_identity_and_audits_it(tmp_path):
    """Ruling 2 refinement item 1(b): request (ask) for a dispatch-bound hook with hook-only
    ctx → denied naming the missing binding, no tg-ctl contact, and the attempt is still
    audited (a weak-binding ask is a hatch use, gap G-8)."""

    fake = _Fake(tmp_path)
    log = tmp_path / "overrides.log"
    result = request_hatch_approval(
        _BG_HOOK, {"hook": _BG_HOOK}, cwd=str(tmp_path / "w"),
        env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat"},
        tg_ctl_candidates=[fake.path], timeout_s=2,
        overrides_log_path=log,
    )
    assert result.approved is False
    assert result.env_present is True  # a present hatch value → deny closed, stop fallbacks
    assert result.should_stop is True
    assert "dispatch-bound" in result.reason
    assert _BG_HOOK in result.reason
    assert "hatch_dispatch_op_fields" in result.reason
    assert "denied without contacting tg-ctl" in result.reason
    assert fake.argvs() == []
    entries = _read_log(log)
    assert len(entries) == 1 and entries[0]["decision"] == "denied"


def test_lib_rejects_dispatch_bound_verify_without_identity_grant_unconsumed(tmp_path):
    """Verify (approved:<id>) for a dispatch-bound hook with hook-only ctx → denied WITHOUT
    consuming; the grant minted for the properly-bound dispatch survives for its bound
    retry."""

    fake, _ = _bg_call(tmp_path, _dispatch_ctx(),
                       env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat"})
    argvs_after_ask = len(fake.argvs())
    _, denied = _bg_call(tmp_path, {"hook": _BG_HOOK},
                         env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert denied.approved is False
    assert "dispatch-bound" in denied.reason
    assert len(fake.argvs()) == argvs_after_ask  # no verify query was even sent
    assert not fake.consumed.exists()
    _, still = _bg_call(tmp_path, _dispatch_ctx(),
                        env={_BG_ENV_VAR: f"approved:{_GRANT_ID}"}, fake=fake)
    assert still.approved is True  # the untouched grant still covers the BOUND dispatch


def test_lib_rejects_dispatch_bound_pull_without_identity_no_query(tmp_path):
    """Pull for a dispatch-bound hook with hook-only ctx → NO query at all (fail closed to
    the unchanged deny); the grant minted for the properly-bound dispatch survives for its
    bound pull."""

    fake, _ = _bg_call(
        tmp_path, _dispatch_ctx(),
        env={_BG_ENV_VAR: "one-off foreground dispatch, human asked in chat", "TMUX_PANE": "%3"},
    )
    argvs_after_ask = len(fake.argvs())
    _, result = _bg_call(tmp_path, {"hook": _BG_HOOK}, env={"TMUX_PANE": "%3"}, fake=fake)
    assert result.requested is False  # unchanged not-requested deny (fail closed)
    assert result.env_present is False
    assert len(fake.argvs()) == argvs_after_ask  # no --pull invocation was even attempted
    assert not fake.consumed.exists()
    _, pulled = _bg_call(tmp_path, _dispatch_ctx(), env={"TMUX_PANE": "%3"}, fake=fake)
    assert pulled.approved is True  # the BOUND dispatch still consumes via the pull
    assert fake.consumed.exists()
