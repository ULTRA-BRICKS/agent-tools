"""Tests for agent-hooks/block-reset-hard/block_reset_hard.py.

Covers:
  - True positives: `git reset --hard` (bare / with ref) and `git clean -f...` (any
    short-flag clustering with -d/-x) are blocked.
  - Safe alternatives are ALLOWED: checkout/restore, bare/--mixed/--soft reset,
    `git clean -n`/no-force.
  - The argv-parse FP fix: text that merely MENTIONS "reset --hard"/"clean -fd" (a commit
    message, a comment, a grep) is ALLOWED.
  - Wrapped forms (`timeout N git ...`, `sudo git ...`) and git global options
    (`git -C <dir> ...`) don't defeat detection.
  - Shell chains: the dangerous form behind `&&`/`;` is still caught.
  - External approval (replaces the removed self-service hatch): unconfigured denies,
    approval_cmd exit-0 allows, nonzero/timeout denies — for BOTH forms.
  - Fail-closed: unbalanced quotes (hint-matched vs. unrelated) and a malformed event.

Run from the repo root::

    uv run --with "pytest>=8,<9" python -m pytest tests/test_block_reset_hard.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-reset-hard"
    / "block_reset_hard.py"
)
_spec = importlib.util.spec_from_file_location("block_reset_hard", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# A throwaway empty dir with NO rig.yaml anywhere up its tree — the DEFAULT event cwd so the
# hook's rig.yaml walk-up is hermetic. Before approval config existed the hook never touched
# cwd; now the dangerous path resolves approval_cmd by walking up from event.cwd, so a `_run`
# without an explicit cwd must NOT fall through to os.getcwd() (the repo root, which HAS a
# rig.yaml) — otherwise adding agent_hooks.approval_cmd to this repo's own rig.yaml would flip
# legacy block-expecting tests to allow and mask a regression.
_HERMETIC_CWD = tempfile.mkdtemp(prefix="brh-hermetic-")


def _run(
    command: str, monkeypatch, env: dict | None = None, cwd: str | None = None,
) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`.  Returns (stdout, stderr, exit)."""
    event: dict = {"args": {"command": command}, "cwd": cwd if cwd is not None else _HERMETIC_CWD}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Clear the removed escape-hatch env so ambient values don't leak into tests (they no
    # longer do anything, but a regression test asserts exactly that).
    for k in (
        "ALLOW_GIT_RESET_HARD",
        "ALLOW_GIT_RESET_HARD_REASON",
        "RIG_HATCH_REQUEST_BLOCK_RESET_HARD",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _rig_dir(tmp_path, approval_cmd: str | None = None, approval_timeout: str | None = None) -> str:
    """A tmp dir containing a rig.yaml (optionally wiring agent_hooks.approval_cmd). Returned as
    the event cwd so the hook's rig.yaml walk-up is hermetic (never the real repo's rig.yaml)."""
    d = tmp_path / "repo"
    d.mkdir()
    if approval_cmd is None and approval_timeout is None:
        (d / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    else:
        lines = ["agent_hooks:"]
        if approval_cmd is not None:
            lines.append(f"  approval_cmd: {approval_cmd}")
        if approval_timeout is not None:
            lines.append(f"  approval_cmd_timeout_s: {approval_timeout}")
        (d / "rig.yaml").write_text("\n".join(lines) + "\n")
    return str(d)


# ── True positives: git reset --hard — should BLOCK ────────────────────────────────────────

def test_block_bare_reset_hard(monkeypatch):
    out, _err, code = _run("git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_reset_hard_with_relative_ref(monkeypatch):
    out, _err, code = _run("git reset --hard HEAD~3", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_reset_hard_with_remote_ref(monkeypatch):
    out, _err, code = _run("git reset --hard origin/main", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── True positives: git clean -f... — should BLOCK ─────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "git clean -fd",
        "git clean -df",
        "git clean -fdx",
        "git clean -xdf",
        "git clean -f -d",
        "git clean --force --force",
        "git clean -f",
        "git clean --force",
        "git clean -nf",  # -n does not cancel a real -f present in the clustering
        "git clean -fn",
        "git clean -fe*.o",  # f before e: a real force flag, not part of -e's value
    ],
)
def test_block_clean_force_variants(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block", command


# ── Safe alternatives — should ALLOW ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "git checkout -- file.txt",
        "git restore file.txt",
        "git reset",
        "git reset --mixed",
        "git reset --soft HEAD~1",
        "git clean -n",
        "git clean --dry-run",
        "git clean",
        "git clean -n -e*.conf",  # dry-run with an exclude pattern, no force
        "git clean -ef*.o",  # -e consumes "f*.o" as its pattern VALUE, not a force flag
    ],
)
def test_allow_safe_alternatives(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow", command


# ── The FP fix — text merely mentioning the phrase must be ALLOWED ─────────────────────────

def test_allow_commit_message_mentioning_reset_hard(monkeypatch):
    cmd = 'git commit -m "remember: never run git reset --hard on a shared checkout"'
    out, _err, code = _run(cmd, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_echo_mentioning_reset_hard(monkeypatch):
    out, _err, code = _run('echo "the phrase reset --hard is dangerous"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_for_clean_fd_string(monkeypatch):
    out, _err, code = _run("grep -r 'clean -fd' .", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Wrapped forms — should still BLOCK ─────────────────────────────────────────────────────

def test_block_wrapped_timeout_reset_hard(monkeypatch):
    out, _err, code = _run("timeout 60 git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_wrapped_sudo_clean_fd(monkeypatch):
    out, _err, code = _run("sudo git clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_git_global_dash_c_reset_hard(monkeypatch):
    """`git -C <dir> reset --hard` must not evade detection via a global option."""
    out, _err, code = _run("git -C /some/path reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_git_global_no_pager_clean_fd(monkeypatch):
    out, _err, code = _run("git --no-pager clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_path_qualified_git(monkeypatch):
    """/opt/homebrew/bin/git reset --hard must still be blocked (basename check)."""
    out, _err, code = _run("/opt/homebrew/bin/git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Additional wrapper forms (ported from block-no-verify's wrapper table) — should BLOCK ──

@pytest.mark.parametrize(
    "command",
    [
        "command git reset --hard",
        "exec git reset --hard",
        "time git reset --hard",
        "setsid git clean -fd",
        "nohup git reset --hard",
        "sudo -u git git reset --hard",  # value-flag consumes "git" as the -u operand
        "nice -n 10 git reset --hard",  # value-flag consumes "10" as the -n operand
        "env -u FOO git reset --hard",  # value-flag consumes "FOO" as the -u operand
    ],
)
def test_block_additional_wrapper_forms(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block", command


def test_block_while_loop_control_flow(monkeypatch):
    """`while`/`do` control-flow tokens must not shield the dangerous command inside."""
    out, _err, code = _run("while git clean -fd; do echo x; done", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_if_then_control_flow_reset_hard(monkeypatch):
    """`if ... ; then` must not shield a `git reset --hard` inside."""
    out, _err, code = _run("if git reset --hard; then echo x; fi", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_if_then_control_flow_clean_force(monkeypatch):
    out, _err, code = _run("if git clean -fd; then echo x; fi", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_wrapper_chain_overflow_fails_closed(monkeypatch):
    """A wrapper chain deeper than the nesting cap must BLOCK (fail-closed), never silently
    allow just because the real command couldn't be resolved through the chain."""
    command = ("command " * (hook._MAX_WRAPPER_NESTING + 4)) + "git reset --hard"
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Multi-line commands and mid-word `#` — should still BLOCK ─────────────────────────────
# A single Bash tool call spanning two lines (`cd /repo` then `git reset --hard` on the next
# line) is a common, entirely ACCIDENTAL shape — the literal incident this hook guards
# against, replayed through a two-line command. A flat single-line shlex pass misses it
# entirely (the newline is just whitespace, so line one's `cd`/`echo` becomes argv[0]).

def test_block_reset_hard_on_second_line(monkeypatch):
    out, _err, code = _run("cd /repo\ngit reset --hard origin/main", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_clean_force_on_second_line(monkeypatch):
    out, _err, code = _run("echo starting cleanup\ngit clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_mid_word_hash_does_not_truncate_parsing(monkeypatch):
    """A `#` in the MIDDLE of a word (`foo#bar`) is literal text to a real shell, not a
    comment start — it must not truncate parsing and hide a later chained command."""
    out, _err, code = _run("echo foo#bar && git clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_hash_inside_commit_message(monkeypatch):
    """A `#` inside a quoted commit message (`fix #42`) is message text, not a comment —
    must not be misparsed either way, and the command overall is still safe (no reset/clean)."""
    out, _err, code = _run("git commit -m 'fix #42'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "echo '# heading' && git reset --hard",
        'echo "#hi" && git reset --hard',
    ],
)
def test_block_quoted_word_initial_hash_does_not_fake_a_comment(command, monkeypatch):
    """A QUOTED argument that starts with `#` (`'# heading'`) dequotes to the same string a
    real unquoted comment would produce. A naive `tok.startswith("#")` check on the dequoted
    token alone can't tell them apart and would wrongly treat the rest of the line — including
    a chained `&& git reset --hard` — as inert comment text, silently letting it through."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block", command


# ── Shell chains — should still BLOCK ──────────────────────────────────────────────────────

def test_block_reset_hard_in_shell_chain_and(monkeypatch):
    out, _err, code = _run("echo done && git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_clean_force_in_shell_chain_semicolon(monkeypatch):
    out, _err, code = _run("echo done; git clean -fd", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_reset_hard_with_leading_env(monkeypatch):
    out, _err, code = _run("GIT_TRACE=1 git reset --hard", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── chained multi-segment commands: EVERY dangerous segment must be approved ──────────────
# Regression for the bug found in review: `_classify` used to `return` on the FIRST dangerous
# segment it found, so `main()` only ever requested approval for (and gated) that one segment
# — a SECOND dangerous segment later in the same chained command, aimed at a different
# (unapproved) repo, ran completely unchecked once the first was approved.

def test_chain_second_segment_unapproved_blocks_whole_command(monkeypatch, tmp_path):
    """`git -C approved reset --hard ; git -C other clean -fd` — the FIRST segment's repo would
    approve, but the SECOND targets a repo with no approval_cmd configured at all. The whole
    chained command must be BLOCKED: an approved first segment must never wave through an
    unapproved second one."""
    approved_repo = tmp_path / "approved"
    approved_repo.mkdir()
    (approved_repo / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    (other_repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")

    out, _err, code = _run(
        f"git -C {approved_repo} reset --hard ; git -C {other_repo} clean -fd", monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_chain_first_segment_unapproved_blocks_whole_command(monkeypatch, tmp_path):
    """The mirror order: the unapproved, dangerous segment comes FIRST, the approved one
    SECOND. This already blocked via the pre-existing first-segment path — pinned down here so
    the refactor that scans every segment doesn't regress the order-doesn't-matter property."""
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    (other_repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    approved_repo = tmp_path / "approved"
    approved_repo.mkdir()
    (approved_repo / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')

    out, _err, code = _run(
        f"git -C {other_repo} clean -fd ; git -C {approved_repo} reset --hard", monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_chain_both_segments_approved_allows_whole_command(monkeypatch, tmp_path):
    """The positive mirror: BOTH segments target repos with `approval_cmd: exit 0` — the whole
    chained command is ALLOWED, and the allow message mentions both dangerous kinds found."""
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    (repo_a / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    (repo_b / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')

    out, _err, code = _run(
        f"git -C {repo_a} reset --hard ; git -C {repo_b} clean -fd", monkeypatch,
    )
    assert code == 0
    assert _decision(out) == "allow"
    message = json.loads(out)["message"].lower()
    assert "reset --hard" in message
    assert "clean -f" in message


# ── aggregate approval budget: too many chained dangerous segments must DENY cleanly ───────
# rather than let an external manifest-timeout kill decide (this hook is on_error=closed, so
# that kill already fails safe — but a legitimately-approved multi-segment command shouldn't
# spuriously get killed instead of denied by this hook's own clear message).

def test_chain_budget_exhausted_denies_with_clear_message(monkeypatch, tmp_path):
    """An exhausted `_MAIN_LOOP_BUDGET_S` must deny a segment that would OTHERWISE be approved
    — proving the budget is actually enforced (checked BEFORE spawning `approval_cmd`), not
    just documented."""
    monkeypatch.setattr(hook, "_MAIN_LOOP_BUDGET_S", -1.0)
    out, _err, code = _run(
        "git reset --hard", monkeypatch, cwd=_rig_dir(tmp_path, approval_cmd='"exit 0"'),
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "budget" in json.loads(out)["message"].lower()


# ── external approval (replaces the removed self-service escape hatch) ─────────────────────

def test_reset_hard_env_bypass_dead(monkeypatch, tmp_path):
    """The removed env hatch: ALLOW_GIT_RESET_HARD=1 (+ reason) must NO LONGER allow."""
    out, _err, code = _run(
        "git reset --hard", monkeypatch,
        {"ALLOW_GIT_RESET_HARD": "1", "ALLOW_GIT_RESET_HARD_REASON": "deliberate"},
        cwd=_rig_dir(tmp_path),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_reset_hard_inline_sentinel_dead(monkeypatch, tmp_path):
    """The removed inline hatch: `# no-reset-guard: reason` must NO LONGER allow."""
    out, _err, code = _run(
        "git reset --hard  # no-reset-guard: deliberate", monkeypatch, cwd=_rig_dir(tmp_path),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_clean_force_env_bypass_dead(monkeypatch, tmp_path):
    out, _err, code = _run(
        "git clean -fd", monkeypatch,
        {"ALLOW_GIT_RESET_HARD": "1", "ALLOW_GIT_RESET_HARD_REASON": "deliberate"},
        cwd=_rig_dir(tmp_path),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_approval_unconfigured_denies_no_subprocess(monkeypatch, tmp_path):
    """No approval_cmd → deny, and no approval subprocess spawned. The approval path uses
    subprocess.Popen(shell=True), so patch Popen (not run) to actually catch a stray spawn."""
    import subprocess as _sub
    calls = {"n": 0}
    real_popen = _sub.Popen

    def _counting_popen(*a, **k):
        if k.get("shell"):
            calls["n"] += 1
        return real_popen(*a, **k)

    monkeypatch.setattr(hook.subprocess, "Popen", _counting_popen)
    out, _err, code = _run("git reset --hard", monkeypatch, cwd=_rig_dir(tmp_path))
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert calls["n"] == 0
    assert "no automatic bypass" in json.loads(out)["message"].lower()


def test_approval_configured_exit0_allows_reset_hard(monkeypatch, tmp_path):
    out, _err, code = _run(
        "git reset --hard", monkeypatch,
        cwd=_rig_dir(tmp_path, approval_cmd='"printf owner-approved"'),
    )
    assert code == 0 and _decision(out) == "allow"
    assert "owner-approved" in json.loads(out)["message"]


def test_approval_configured_exit0_allows_clean_force(monkeypatch, tmp_path):
    out, _err, code = _run(
        "git clean -fd", monkeypatch, cwd=_rig_dir(tmp_path, approval_cmd='"exit 0"'),
    )
    assert code == 0 and _decision(out) == "allow"


def test_approval_configured_nonzero_denies(monkeypatch, tmp_path):
    out, _err, code = _run(
        "git reset --hard", monkeypatch, cwd=_rig_dir(tmp_path, approval_cmd='"exit 4"'),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_approval_configured_timeout_denies(monkeypatch, tmp_path):
    """Hanging approval_cmd past approval_cmd_timeout_s → deny (never on_error=closed's
    generic block reason — this is the approval verdict, resolved inside _request_approval)."""
    out, _err, code = _run(
        "git reset --hard", monkeypatch,
        cwd=_rig_dir(tmp_path, approval_cmd='"sleep 5"', approval_timeout="0.2"),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_approval_cross_dash_c_uses_target_repo_config(monkeypatch, tmp_path):
    """`git -C <target> reset --hard` resolves approval against <target>'s rig.yaml (the repo
    being wiped), NOT the shell cwd — so an approver in the cwd repo can't approve wiping
    another repo. cwd repo WOULD approve; target has none → deny."""
    cwd_repo = tmp_path / "cwd"
    cwd_repo.mkdir()
    (cwd_repo / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')
    target = tmp_path / "target"
    target.mkdir()
    (target / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    out, _err, code = _run(f"git -C {target} reset --hard", monkeypatch, cwd=str(cwd_repo))
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_approval_cross_dash_c_target_approves(monkeypatch, tmp_path):
    """The mirror: when the TARGET repo (`git -C`) has approval_cmd exit-0, it is honored even
    though the shell cwd repo has no approval configured."""
    cwd_repo = tmp_path / "cwd"
    cwd_repo.mkdir()
    (cwd_repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    target = tmp_path / "target"
    target.mkdir()
    (target / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')
    out, _err, code = _run(f"git -C {target} reset --hard", monkeypatch, cwd=str(cwd_repo))
    assert code == 0 and _decision(out) == "allow"


def test_approval_wrapped_dash_c_resolves_target_repo(monkeypatch, tmp_path):
    """A WRAPPED destructive command (`timeout 60 git -C <target> reset --hard`) must still
    resolve approval against <target>'s rig.yaml — proving `_git_dash_c_dir` peels the wrapper
    before reading `-C`. cwd repo would approve; target has none → deny (so a wrapper can't make
    the guard fall back to the cwd repo's approver for another repo)."""
    cwd_repo = tmp_path / "cwd"
    cwd_repo.mkdir()
    (cwd_repo / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')
    target = tmp_path / "target"
    target.mkdir()
    (target / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    out, _err, code = _run(
        f"timeout 60 git -C {target} reset --hard", monkeypatch, cwd=str(cwd_repo),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_approval_glued_dash_c_resolves_target_repo(monkeypatch, tmp_path):
    """Glued `-C<path>` form is honored too (target has approval exit-0 → allow)."""
    cwd_repo = tmp_path / "cwd"
    cwd_repo.mkdir()
    (cwd_repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    target = tmp_path / "target"
    target.mkdir()
    (target / "rig.yaml").write_text('agent_hooks:\n  approval_cmd: "exit 0"\n')
    out, _err, code = _run(f"git -C{target} reset --hard", monkeypatch, cwd=str(cwd_repo))
    assert code == 0 and _decision(out) == "allow"


def test_approval_cmd_receives_context_env(monkeypatch, tmp_path):
    marker = tmp_path / "ctx.txt"
    script = tmp_path / "approve.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s" "$RIG_APPROVAL_KIND" "$RIG_APPROVAL_HOOK" > "{marker}"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    out, _err, code = _run(
        "git reset --hard", monkeypatch, cwd=_rig_dir(tmp_path, approval_cmd=f'"{script}"'),
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.read_text() == "reset --hard|block-reset-hard"


def test_backgrounded_approval_cmd_times_out_and_denies(monkeypatch, tmp_path):
    """An approval_cmd whose shell exits 0 but leaves a BACKGROUNDED child holding the stdout
    pipe (`sleep 5 &`) must NOT hang the hook to the manifest budget: the process group is
    SIGKILLed on the internal timeout and the verdict is deny — keeping the ceiling effective so
    pin's on_error=open can never be reached this way. Also asserts it returns fast."""
    import time
    start = time.monotonic()
    out, _err, code = _run(
        "git reset --hard", monkeypatch,
        cwd=_rig_dir(tmp_path, approval_cmd='"sleep 5 &"', approval_timeout="0.3"),
    )
    elapsed = time.monotonic() - start
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert elapsed < 3.0, f"hook hung {elapsed:.1f}s — backgrounded child held the pipe past timeout"


# ── approval-timeout resolution: clamp to ceiling, floor to default, non-numeric ──────────

def test_approval_timeout_clamped_to_ceiling(monkeypatch):
    assert hook._approval_timeout_s("agent_hooks:\n  approval_cmd_timeout_s: 30\n") == hook._APPROVAL_TIMEOUT_CEILING_S


def test_approval_timeout_small_value_kept(monkeypatch):
    assert hook._approval_timeout_s("agent_hooks:\n  approval_cmd_timeout_s: 0.2\n") == 0.2


def test_approval_timeout_nonpositive_floors_to_default(monkeypatch):
    assert hook._approval_timeout_s("agent_hooks:\n  approval_cmd_timeout_s: 0\n") == hook._APPROVAL_TIMEOUT_DEFAULT_S
    assert hook._approval_timeout_s("agent_hooks:\n  approval_cmd_timeout_s: -3\n") == hook._APPROVAL_TIMEOUT_DEFAULT_S


def test_approval_timeout_nonnumeric_defaults(monkeypatch):
    assert hook._approval_timeout_s("agent_hooks:\n  approval_cmd_timeout_s: soon\n") == hook._APPROVAL_TIMEOUT_DEFAULT_S


def test_approval_timeout_absent_defaults(monkeypatch):
    assert hook._approval_timeout_s("agent_hooks:\n  worktree_only: false\n") == hook._APPROVAL_TIMEOUT_DEFAULT_S


def test_approval_detail_capped(monkeypatch, tmp_path):
    """A verbose approval_cmd stdout is trimmed to _APPROVAL_DETAIL_CAP in the logged detail."""
    script = tmp_path / "loud.sh"
    script.write_text("#!/bin/sh\nawk 'BEGIN{for(i=0;i<4000;i++)printf \"A\"}'\nexit 0\n")
    script.chmod(0o755)
    approved, detail = hook._request_approval(
        _rig_dir(tmp_path, approval_cmd=f'"{script}"'),
        {"hook": "block-reset-hard", "kind": "reset --hard", "target": "", "command": "x"},
    )
    assert approved is True
    assert detail is not None and len(detail) == hook._APPROVAL_DETAIL_CAP


# ── generic Telegram hatch escalation: env-var path wins before approval_cmd ───────────────

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


def test_hatch_escalation_exit0_allows_reset_hard_and_logs_source(monkeypatch, tmp_path):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n" + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "git reset --hard",
        monkeypatch,
        {"RIG_HATCH_REQUEST_BLOCK_RESET_HARD": "Need to discard a disposable failed experiment."},
        cwd=_rig_dir(tmp_path),
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_escalation_denial_wins_over_approval_cmd(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "git clean -fd",
        monkeypatch,
        {"RIG_HATCH_REQUEST_BLOCK_RESET_HARD": "Need to discard a disposable failed experiment."},
        cwd=_rig_dir(tmp_path, approval_cmd='"exit 0"'),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── Fail-closed paths ──────────────────────────────────────────────────────────────────────

def test_unbalanced_quotes_reset_hard_hint_blocks(monkeypatch):
    """Unbalanced quote on a command that plausibly is reset --hard → fail closed (block)."""
    out, _err, code = _run("git reset --hard --author 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_clean_force_hint_blocks(monkeypatch):
    out, _err, code = _run("git clean -fd --exclude 'unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_allows(monkeypatch):
    """Unbalanced quote on an unrelated command → allow (not a reset/clean attempt)."""
    out, _err, code = _run("grep won't file", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_unparseable_still_blocks_and_skips_approval(monkeypatch, tmp_path):
    """An unparseable command that looks like reset --hard still fails closed — even a
    configured approval_cmd is NOT reached on the unparseable path (classification yields
    'unparseable', not 'dangerous', so approval is never consulted)."""
    import subprocess as _sub
    calls = {"n": 0}
    real_popen = _sub.Popen

    def _counting_popen(*a, **k):
        if k.get("shell"):
            calls["n"] += 1
        return real_popen(*a, **k)

    monkeypatch.setattr(hook.subprocess, "Popen", _counting_popen)
    out, _err, code = _run(
        "git reset --hard --author 'unclosed", monkeypatch,
        cwd=_rig_dir(tmp_path, approval_cmd='"exit 0"'),
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert calls["n"] == 0


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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
