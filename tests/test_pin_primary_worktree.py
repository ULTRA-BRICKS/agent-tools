"""Tests for the pin-primary-worktree agent-hook (pre-bash).

Covers the doctrine: in an ENROLLED repo's PRIMARY worktree, `git checkout`/`git switch` to
anything but the default branch is DENIED; the same command inside a LINKED worktree (a real
`git worktree add` tree) is ALLOWED; checking back OUT to the default branch is always allowed;
an un-enrolled repo is never blocked; the old self-service escape hatch is DEAD (deny-by-default;
a rig.yaml-configured approval_cmd exit-0 allows, unconfigured/nonzero/timeout deny); a path-restore
form (`checkout -- file`, `checkout .`) is not mistaken for a branch switch; a chain command finds
the offending segment; a `-C <dir>` cross-repo checkout is judged against THAT repo, not cwd.

Exercised against REAL temp git repos and REAL `git worktree add` trees, not mocks — the
git-subprocess primary/linked-worktree distinction is the whole point of this hook.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_pin_primary_worktree.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Isolate throwaway test repos from the developer's GLOBAL git config (SYNC with
# test_worktree_only_writes.py — same rationale: a rig-provisioned core.hooksPath must not
# fire pre-commit gates inside a bare temp repo's setup commands).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "pin-primary-worktree"
    / "pin_primary_worktree.py"
)
_spec = importlib.util.spec_from_file_location("pin_primary_worktree", _HOOK)
assert _spec and _spec.loader
ppw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ppw)

_ENV_KEYS = (
    "RIG_WORKTREE_ONLY",
    "RIG_ALLOW_MAIN_EDIT",
    "RIG_ALLOW_MAIN_EDIT_REASON",
    "RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE",
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    )
    return proc.stdout.strip()


def _make_repo(
    tmp_path: Path,
    *,
    branch: str = "main",
    enroll: bool = True,
    approval_cmd: str | None = None,
    approval_timeout: str | None = None,
) -> Path:
    """A real git repo on ``branch`` with one commit and (by default) an enrolling rig.yaml."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    if enroll:
        lines = ["agent_hooks:", "  worktree_only: true"]
        if approval_cmd is not None:
            lines.append(f"  approval_cmd: {approval_cmd}")
        if approval_timeout is not None:
            lines.append(f"  approval_cmd_timeout_s: {approval_timeout}")
        (repo / "rig.yaml").write_text("\n".join(lines) + "\n")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "feat/x")  # a feature branch to switch to, not yet checked out
    return repo


def _add_linked_worktree(repo: Path, path: Path, branch: str) -> None:
    _git(repo, "worktree", "add", str(path), branch)


def _run(cwd: Path, command: str, monkeypatch, env: dict | None = None) -> tuple[str, int]:
    event = {"cwd": str(cwd), "args": {"command": command}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = ppw.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: primary worktree, enrolled, checkout to a feature branch ──────────────────────────

def test_primary_checkout_to_feature_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "primary worktree" in json.loads(out)["message"].lower()


def test_primary_switch_to_feature_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git switch feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_primary_checkout_dash_b_new_branch_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout -b feat/new", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_primary_switch_dash_c_new_branch_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git switch -c feat/new", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_env_enrolled_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    out, code = _run(repo, "git checkout feat/x", monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW: switching (back) to the default branch is always fine ────────────────────────────

def test_checkout_back_to_default_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "feat/x")
    out, code = _run(repo, "git checkout main", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: a LINKED worktree may freely checkout/switch between feature branches ─────────────

def test_linked_worktree_checkout_to_feature_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    linked = tmp_path / "linked"
    _add_linked_worktree(repo, linked, "feat/x")
    _git(linked, "branch", "feat/y")
    out, code = _run(linked, "git checkout feat/y", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: not enrolled (no env, no rig.yaml knob) ───────────────────────────────────────────

def test_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_rigyaml_explicit_false_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: not a branch switch at all ────────────────────────────────────────────────────────

def test_path_restore_with_dashdash_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout feat/x -- seed.txt", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_checkout_dot_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout .", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_read_only_git_commands_allow(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    for cmd in ("git status", "git log --oneline -5", "git worktree list", "git branch"):
        out, code = _run(repo, cmd, monkeypatch)
        assert code == 0 and _decision(out) == "allow", cmd


def test_worktree_add_allows(tmp_path, monkeypatch):
    """`git worktree add` is the RECOMMENDED alternative — must never itself be blocked."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, f"git worktree add {tmp_path / 'wt2'} feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── the offending segment can be anywhere in a chain ─────────────────────────────────────────

def test_chain_finds_offending_segment(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git status && git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── chained multi-segment commands: EVERY segment must be evaluated, not just the first ────
# Regression for the bug found in review: `main()` used to `return 0` right after approving the
# FIRST primary-worktree checkout it found in a chained command, so a SECOND checkout/switch
# later in the same command — targeting a DIFFERENT enrolled primary repo with no approval
# configured — ran completely unchecked once the first was approved. `_make_repo` always makes
# `<tmp_path>/repo`, so each repo below gets its OWN parent subdir created first.

def test_chain_second_repo_unapproved_denies_whole_command(tmp_path, monkeypatch):
    """`git -C approved checkout feat/x ; git -C other checkout feat/x` — the first repo's
    approval_cmd would approve, but the second enrolled primary repo has none configured. The
    whole chained command must DENY: approving segment 1 must never let segment 2 through."""
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    approved_repo = _make_repo(approved_dir, approval_cmd='"exit 0"')
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_repo = _make_repo(other_dir)  # enrolled (default), no approval_cmd

    out, code = _run(
        tmp_path,
        f"git -C {approved_repo} checkout feat/x ; git -C {other_repo} checkout feat/x",
        monkeypatch,
    )
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_chain_first_repo_unapproved_denies_whole_command(tmp_path, monkeypatch):
    """The mirror order: the unapproved repo's checkout comes FIRST, the approved one SECOND —
    pinned down so the refactor that scans every segment doesn't regress the
    order-doesn't-matter property (this order already blocked via the pre-existing
    first-segment path)."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_repo = _make_repo(other_dir)
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    approved_repo = _make_repo(approved_dir, approval_cmd='"exit 0"')

    out, code = _run(
        tmp_path,
        f"git -C {other_repo} checkout feat/x ; git -C {approved_repo} checkout feat/x",
        monkeypatch,
    )
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_chain_both_repos_approved_allows_whole_command(tmp_path, monkeypatch):
    """The positive mirror: BOTH repos have `approval_cmd: exit 0` — the whole chained command
    is ALLOWED."""
    a_dir = tmp_path / "repo-a"
    a_dir.mkdir()
    repo_a = _make_repo(a_dir, approval_cmd='"exit 0"')
    b_dir = tmp_path / "repo-b"
    b_dir.mkdir()
    repo_b = _make_repo(b_dir, approval_cmd='"exit 0"')

    out, code = _run(
        tmp_path,
        f"git -C {repo_a} checkout feat/x ; git -C {repo_b} checkout feat/x",
        monkeypatch,
    )
    assert code == 0 and _decision(out) == "allow"


# ── aggregate approval budget: too many chained segments must DENY, not let an external ────
# manifest-timeout kill silently allow (this hook is on_error=open — a kill fails OPEN). See
# the review finding this addresses: gating EVERY segment (the fix above) means a single
# invocation can now call `approval_cmd` more than once, so the AGGREGATE time must stay well
# under the descriptor's `timeout_ms`, or an unapproved segment could slip through via the
# external kill instead of this hook's own logic.

def test_chain_budget_exhausted_denies_rather_than_risk_external_timeout(tmp_path, monkeypatch):
    """An exhausted `_MAIN_LOOP_BUDGET_S` must deny a segment that would OTHERWISE be approved
    — proving the budget is actually enforced (checked BEFORE spawning `approval_cmd`), not
    just documented."""
    monkeypatch.setattr(ppw, "_MAIN_LOOP_BUDGET_S", -1.0)
    repo = _make_repo(tmp_path, approval_cmd='"exit 0"')
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "budget" in json.loads(out)["message"].lower()


# ── `-C <dir>` is judged against THAT repo, not the shell cwd ────────────────────────────────

def test_dash_c_cross_repo_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out, code = _run(elsewhere, f"git -C {repo} checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dash_c_cross_repo_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, enroll=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")  # cwd IS enrolled…
    out, code = _run(elsewhere, f"git -C {repo} checkout feat/x", monkeypatch)  # …but repo is not
    assert code == 0 and _decision(out) == "allow"


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline) ──────────────────

def test_env_escape_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """RIG_ALLOW_MAIN_EDIT=1 as a real process env var must NO LONGER allow the checkout —
    the self-service bypass was removed (Alex tg#6554)."""
    repo = _make_repo(tmp_path)
    out, code = _run(
        repo, "git checkout feat/x", monkeypatch,
        {"RIG_ALLOW_MAIN_EDIT": "1", "RIG_ALLOW_MAIN_EDIT_REASON": "deliberate"},
    )
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_inline_escape_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """`RIG_ALLOW_MAIN_EDIT=1 git checkout feat/x` (inline VAR=val prefix) must still be
    classified as a git checkout (the leading-assignment skip stays) AND still BLOCK — the
    inline bypass is gone (Alex tg#6554)."""
    repo = _make_repo(tmp_path)
    out, code = _run(
        repo,
        'RIG_ALLOW_MAIN_EDIT=1 RIG_ALLOW_MAIN_EDIT_REASON="deliberate" git checkout feat/x',
        monkeypatch,
    )
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── external approval_cmd: unconfigured denies, exit-0 allows, nonzero/timeout deny ─────────

def test_approval_unconfigured_denies_no_subprocess(tmp_path, monkeypatch):
    """No approval_cmd in rig.yaml → deny, and no approval subprocess is spawned. The approval
    path spawns via subprocess.Popen(shell=True); git plumbing uses subprocess.run([...]) with a
    list argv, so counting shell=True Popen calls isolates the approval invocation."""
    import subprocess as _sub
    calls = {"n": 0}
    real_popen = _sub.Popen

    def _counting_popen(*a, **k):
        if k.get("shell"):
            calls["n"] += 1
        return real_popen(*a, **k)

    monkeypatch.setattr(ppw.subprocess, "Popen", _counting_popen)
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert calls["n"] == 0
    assert "no automatic bypass" in json.loads(out)["message"].lower()


def test_approval_configured_exit0_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, approval_cmd='"printf approved-by-owner"')
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert "approved-by-owner" in json.loads(out)["message"]


def test_approval_configured_nonzero_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, approval_cmd='"exit 3"')
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_approval_configured_timeout_denies(tmp_path, monkeypatch):
    """A hanging approval_cmd past approval_cmd_timeout_s → deny (never falls through to
    on_error=open)."""
    repo = _make_repo(tmp_path, approval_cmd='"sleep 5"', approval_timeout="0.2")
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_backgrounded_approval_cmd_times_out_and_denies(tmp_path, monkeypatch):
    """pin is on_error=open — the class this change removes. An approval_cmd that exits 0 but
    backgrounds a child holding the stdout pipe (`sleep 5 &`) must be SIGKILLed (process group)
    on the internal timeout and DENY, not hang until the dispatcher's manifest budget kills the
    hook into a silent ALLOW. Also asserts it returns fast."""
    import time
    repo = _make_repo(tmp_path, approval_cmd='"sleep 5 &"', approval_timeout="0.3")
    start = time.monotonic()
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    elapsed = time.monotonic() - start
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert elapsed < 3.0, f"hook hung {elapsed:.1f}s — backgrounded child held the pipe past timeout"


def test_approval_cmd_receives_context_env(tmp_path, monkeypatch):
    """The approval_cmd sees RIG_APPROVAL_TARGET / RIG_APPROVAL_KIND / RIG_APPROVAL_HOOK
    (dynamic data via env, not string-interpolated) — proving the trust boundary."""
    marker = tmp_path / "ctx.txt"
    script = tmp_path / "approve.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s" "$RIG_APPROVAL_KIND" "$RIG_APPROVAL_TARGET" "$RIG_APPROVAL_HOOK" > "{marker}"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    repo = _make_repo(tmp_path, approval_cmd=f'"{script}"')
    out, code = _run(repo, "git checkout feat/x", monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert marker.read_text() == "checkout|feat/x|pin-primary-worktree"


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


def test_hatch_escalation_exit0_allows_and_logs_source(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n" + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(ppw.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path)

    out, code = _run(
        repo,
        "git checkout feat/x",
        monkeypatch,
        {"RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE": "Need to inspect the primary checkout."},
    )

    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_escalation_denial_wins_over_approval_cmd(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(ppw.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, approval_cmd='"exit 0"')

    out, code = _run(
        repo,
        "git checkout feat/x",
        monkeypatch,
        {"RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE": "Need to inspect the primary checkout."},
    )

    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── `git checkout -` (previous branch) is resolved via @{-1}, not string-blind ───────────────

def test_checkout_dash_previous_branch_denies_when_feature(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "feat/x")
    _git(repo, "checkout", "main")  # @{-1} now resolves to feat/x
    out, code = _run(repo, "git checkout -", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_checkout_dash_previous_branch_allows_when_default(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "feat/x")  # main -> feat/x; @{-1} is now "main" (the default)
    out, code = _run(repo, "git checkout -", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── fail-OPEN: not a git repo, bad event, empty command ─────────────────────────────────────

def test_non_git_dir_allows(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    out, code = _run(plain, "git checkout feat/x", monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_bad_event_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    assert ppw.main() == 0 and _decision(out.getvalue()) == "allow"


def test_empty_command_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "", monkeypatch)
    assert code == 0 and _decision(out) == "allow"


# ── the bridge execs `cmd` DIRECTLY (subprocess.run([cmd, ...])) — a missing shebang is an
# exec-format error resolved via on_error=open, silently ALLOWING every checkout this hook
# exists to block. `importlib`-loading (every test above) can't catch this — only a real
# subprocess exec of the file exercises the actual bridge invocation shape ──────────────────────

def test_hook_is_directly_executable(tmp_path):
    """Regression for the missing-shebang bug: run the hook file itself as an executable
    (exactly how ``cc_hook_bridge/dispatch.py`` invokes it), not via ``import``. Before the fix
    this file started with a bare docstring, no ``#!``, so the OS fell back to running it as a
    shell script — the docstring/code was executed as shell commands (observed: it literally
    ran a stray `git checkout <branch>` parsed out of the docstring text)."""
    repo = _make_repo(tmp_path)
    event = {"cwd": str(repo), "args": {"command": "git checkout feat/x"}}
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    proc = subprocess.run(
        [str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert proc.returncode == ppw.BLOCK_EXIT_CODE, (proc.returncode, proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["decision"] == "block"


# ── a leading shell VAR=val assignment must not defeat classification ───────────────────────

def test_leading_unrelated_env_assignment_still_denies(tmp_path, monkeypatch):
    """`GIT_TRACE=1 git checkout feat/x` — an unrelated leading assignment — must still be
    recognized and BLOCKED, not silently allowed because `toks[0] != "git"`."""
    repo = _make_repo(tmp_path)
    out, code = _run(repo, "GIT_TRACE=1 git checkout feat/x", monkeypatch)
    assert code == ppw.BLOCK_EXIT_CODE and _decision(out) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
