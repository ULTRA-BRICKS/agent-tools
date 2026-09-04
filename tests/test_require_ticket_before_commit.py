"""Tests for the require-ticket-before-commit agent-hook (pre-bash, strict-by-default).

This is the CI-collected mirror of the hook's own `test_require_ticket.py` (which lives
beside the script under `agent-hooks/` and is not picked up by `pytest tests/`). It pins
the contract the task-cli Docker test and the orchestration swarm depend on:

  - STRICT BY DEFAULT: a no-ticket, non-chore `git commit` BLOCKS (exit 10, decision "block")
    with no env set — the inversion this hook now ships.
  - the block message LEADS with the stable marker `[require-ticket] BLOCKED: no ticket
    reference`, so an external check can assert on one fixed string.
  - EVERY accepted ticket-reference form PASSES (Closes #N / Fixes #N / #N / a KEY-NUM
    task-cli id / a `task:` form / a `[ticket: …]` trailer) — a legit commit is never wedged.
  - the OLD per-commit self-service escapes are DEAD: a `[skip-ticket: <reason>]` message
    trailer and an inline `REQUIRE_TICKET_SKIP=1 git commit …` no longer bypass (both removed).
  - the only sanctioned out for a genuinely ticketless commit is the Telegram hatch
    `RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT` (deny-by-default; approved via tg-ctl only).
  - `REQUIRE_TICKET_STRICT=0` opts back to warn-only (allow with advisory) — repo-level dial kept.

The hook reads only the command + cwd from the stdin event (the cwd is an isolated empty
temp dir so branch detection finds no incidental ticket pattern). Pure parse — no temp repo.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_require_ticket_before_commit.py -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "require-ticket-before-commit"
    / "require_ticket_before_commit.py"
)
_spec = importlib.util.spec_from_file_location("require_ticket", _HOOK)
assert _spec and _spec.loader
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


_HATCH_ENV = "RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT"


def _fake_tg_ctl(path: Path, body: str) -> Path:
    """A throwaway executable standing in for `tg-ctl ask` — its exit code is the human verdict."""
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


def _run(
    command: str,
    *,
    strict: bool | None = None,
    env_extra: dict | None = None,
    tg_ctl: Path | None = None,
) -> dict:
    """End-to-end: feed the stdin JSON event to the script; return {code, decision, message}.

    strict=None leaves REQUIRE_TICKET_STRICT UNSET (the real default → strict block);
    True sets `=1`; False sets `=0` (the warn-only opt-out). cwd is an isolated empty dir.

    The hook runs as a SUBPROCESS, so the shared hatch helper's trusted tg-ctl path can't be
    monkeypatched in-process — instead, when ``tg_ctl`` is given, a rig.yaml pointing
    ``agent_hooks.tg_ctl_path`` at it is written into the isolated cwd (the helper reads that
    committed-config path, walking up from cwd).
    """
    import os

    env = dict(os.environ)
    for k in ("REQUIRE_TICKET_STRICT", "REQUIRE_TICKET_SKIP", "REQUIRE_TICKET_EXEMPT_TYPES",
              _HATCH_ENV):
        env.pop(k, None)
    if strict is True:
        env["REQUIRE_TICKET_STRICT"] = "1"
    elif strict is False:
        env["REQUIRE_TICKET_STRICT"] = "0"
    if env_extra:
        env.update(env_extra)
    with tempfile.TemporaryDirectory() as isolated:
        if tg_ctl is not None:
            (Path(isolated) / "rig.yaml").write_text(
                f"agent_hooks:\n  tg_ctl_path: {tg_ctl}\n", encoding="utf-8",
            )
        event = json.dumps({"args": {"command": command}, "cwd": isolated})
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            cwd=isolated,
            timeout=10,
        )
    payload = json.loads((proc.stdout or "").strip())
    return {"code": proc.returncode, "decision": payload["decision"], "message": payload.get("message", "")}


def _run_inproc(command, monkeypatch, tmp_path, *, strict=None, env_extra=None, tg_ctl):
    """In-process variant for tests that must exercise a CONTROLLABLE tg-ctl. The shared lib
    resolves the approval binary from the account's real home (`resolve_home`), which a subprocess
    can't redirect — so these run the hook's `main()` in-process with `resolve_home` pointed at a
    clean fake home (no rig.yaml) and `_TRUSTED_TG_CTL_PATHS` monkeypatched to the fake tg-ctl.
    That is the ONLY safe way to reach a mocked tg-ctl; a subprocess would hit the real one."""
    import io

    home = tmp_path / "_home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(rt, "STRICT", strict is not False)
    monkeypatch.setattr(rt.hatch_escalation, "resolve_home", lambda: str(home))
    monkeypatch.setattr(rt.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    for k in ("REQUIRE_TICKET_STRICT", "REQUIRE_TICKET_SKIP", "REQUIRE_TICKET_EXEMPT_TYPES",
              _HATCH_ENV):
        monkeypatch.delenv(k, raising=False)
    if strict is True:
        monkeypatch.setenv("REQUIRE_TICKET_STRICT", "1")
    elif strict is False:
        monkeypatch.setenv("REQUIRE_TICKET_STRICT", "0")
    for k, v in (env_extra or {}).items():
        monkeypatch.setenv(k, v)
    isolated = tmp_path / "_iso"
    isolated.mkdir(exist_ok=True)
    event = json.dumps({"args": {"command": command}, "cwd": str(isolated)})
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    monkeypatch.setattr(sys, "stdout", out)
    code = rt.main()
    payload = json.loads(out.getvalue().strip())
    return {"code": code, "decision": payload["decision"], "message": payload.get("message", "")}


# ── STRICT BY DEFAULT ─────────────────────────────────────────────────────────────────────────


def test_no_ticket_blocks_by_default():
    r = _run('git commit -m "feat: add export"')
    assert r["code"] == rt.BLOCK_EXIT_CODE == 10
    assert r["decision"] == "block"


def test_block_message_leads_with_stable_marker():
    r = _run('git commit -m "feat: add export"')
    assert r["decision"] == "block"
    assert rt.BLOCK_MARKER == "[require-ticket] BLOCKED: no ticket reference"
    assert r["message"].startswith(rt.BLOCK_MARKER)


def test_bare_fix_blocks_by_default():
    # `fix:` is a bugfix (not exempt) and the subject has no ref.
    r = _run('git commit -m "fix: null deref"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_strict_disabled_warns_but_allows():
    r = _run('git commit -m "feat: add export"', strict=False)
    assert r["code"] == 0
    assert r["decision"] == "allow"


def test_warn_mode_message_omits_block_marker():
    # The stable marker must appear ONLY on a real block — never on a warn-mode allow, or an
    # external grep-for-marker check would false-positive on an allowed commit.
    r = _run('git commit -m "feat: add export"', strict=False)
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]


# ── EVERY VALID REFERENCE FORM PASSES ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("subject", [
    "feat: add export\n\nCloses #123",   # Closes #N
    "fix: crash\n\nFixes #4",            # Fixes #N
    "feat: add export #88",             # bare #N
    "feat: add export ENG-456",         # KEY-NUM task-cli/Linear id
    "feat: add export task:ABC-12",     # task: form
    "feat: add export [ticket: PROJ-9]",       # [ticket: id]
    "feat: add export [ticket: backfill-orders]",  # [ticket: slug] (non-numeric task-cli id)
])
def test_valid_ref_forms_pass(subject):
    r = _run(f'git commit -m {subject!r}')
    assert r["code"] == 0, f"{subject!r} should pass but exit={r['code']} ({r['message']})"
    assert r["decision"] == "allow"


# ── regression: the OLD per-commit self-service escapes are DEAD ─────────────────────────────────
# The `[skip-ticket: <reason>]` message trailer and the inline `REQUIRE_TICKET_SKIP=1` env were
# removed — an agent could set either on its own command, so they were self-grants, not controls.
# The only sanctioned out for a genuinely ticketless commit is now the Telegram hatch (below).


def test_old_skip_ticket_trailer_no_longer_bypasses():
    """A `[skip-ticket: <reason>]` trailer (even with a real reason) must NO LONGER bypass —
    it is now just message text, and the ticketless commit still BLOCKs under the strict default."""
    r = _run('git commit -m "feat: x [skip-ticket: one-off backfill]"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_old_inline_skip_env_no_longer_bypasses():
    """An inline `REQUIRE_TICKET_SKIP=1 git commit …` must NO LONGER bypass — the per-commit
    env is gone; the commit still BLOCKs."""
    r = _run('REQUIRE_TICKET_SKIP=1 git commit -m "feat: x"')
    assert r["code"] == 10
    assert r["decision"] == "block"


# ── Telegram hatch escalation (RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT) ───────────────────


def test_hatch_unset_blocks_and_names_env_var():
    """No hatch requested → the normal strict block, and the guidance names the hatch env var so
    an agent knows the only sanctioned escape."""
    r = _run('git commit -m "feat: add export"')
    assert r["code"] == 10 and r["decision"] == "block"
    assert _HATCH_ENV in r["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path):
    """A bare `1` (no written justification) is an invalid request → deny (block), and NO tg-ctl
    is invoked (a never-callable would-ALLOW proves no Telegram round-trip)."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n")  # would ALLOW if ever called
    r = _run('git commit -m "feat: x"', env_extra={_HATCH_ENV: "1"}, tg_ctl=tg_ctl)
    assert r["code"] == 10 and r["decision"] == "block"
    assert "hatch escalation denied" in r["message"].lower()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 0 (the human approved) → allow."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    r = _run_inproc('git commit -m "feat: x"', monkeypatch, tmp_path,
                    env_extra={_HATCH_ENV: "one-off backfill, no ticket warranted"}, tg_ctl=tg_ctl)
    assert r["code"] == 0 and r["decision"] == "allow"
    assert "hatch escalation" in r["message"].lower()
    assert rt.BLOCK_MARKER not in r["message"]


def test_hatch_justification_exit1_blocks_citing_denial(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 1 (the human declined / timed out) → block, leading
    with the denial reason."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    r = _run_inproc('git commit -m "feat: x"', monkeypatch, tmp_path,
                    env_extra={_HATCH_ENV: "one-off backfill, no ticket warranted"}, tg_ctl=tg_ctl)
    assert r["code"] == 10 and r["decision"] == "block"
    assert "hatch escalation denied" in r["message"].lower()


def test_hatch_ignored_under_warn_only():
    """The hatch is only relevant when STRICT would block: under REQUIRE_TICKET_STRICT=0 the gate
    never blocks, so a ticketless commit warn-allows regardless of the hatch env (no tg call)."""
    r = _run('git commit -m "feat: x"', strict=False, env_extra={_HATCH_ENV: "whatever"})
    assert r["code"] == 0 and r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]


# ── -F MESSAGE SOURCE: file readable, stdin (-F -) fail-CLOSED with a hint ───────────────────────
# #100 made `-F <file>` readable; #102 first handled the `-F -` (stdin) gap by failing OPEN, but
# agent-tools#104 reverses that: git streams a `-F -` message on its OWN stdin, which a PreToolUse
# hook cannot read, and silently allowing it made `-F -` a free bypass of the ticket gate. So for
# `-F -` ONLY the gate now FAILS CLOSED (block, exit 10, the marker) with an actionable hint —
# unless an approved Telegram hatch (consulted BEFORE this block) or warn-only mode applies.


# Every readable `-F`/`--file` SPELLING (separate, glued, long, `=`) must actually READ the file and
# gate on its ticket — the stdin fail-closed is ONLY for `-F -`, never for a real file in any spelling.
@pytest.mark.parametrize("spelling", ["-F {p}", "-F{p}", "--file {p}", "--file={p}"])
def test_file_spellings_with_ticket_allow(tmp_path, spelling):
    msg = tmp_path / "COMMIT_MSG.txt"
    msg.write_text("feat: add export\n\nCloses #321\n", encoding="utf-8")
    r = _run(f"git commit {spelling.format(p=msg)}")
    assert r["code"] == 0, f"{spelling!r} with Closes #321 must read the file and allow ({r['message']})"
    assert r["decision"] == "allow"


@pytest.mark.parametrize("spelling", ["-F {p}", "-F{p}", "--file {p}", "--file={p}"])
def test_file_spellings_without_ticket_block(tmp_path, spelling):
    msg = tmp_path / "COMMIT_MSG.txt"
    msg.write_text("feat: add export\n", encoding="utf-8")
    r = _run(f"git commit {spelling.format(p=msg)}")
    assert r["code"] == 10, f"{spelling!r} readable file with no ticket must still block ({r['message']})"
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    "git commit -F -",       # separate stdin sentinel
    "git commit -F-",        # glued
    "git commit --file -",   # long form
    "git commit --file=-",   # long form, glued
])
def test_dash_F_stdin_fails_closed_with_hint(command):
    # agent-tools#104: a `-F -` stdin message is unreadable, so rather than let it bypass the gate it
    # now BLOCKS (exit 10, the marker) with an ACTIONABLE hint naming the readable ways + escapes.
    r = _run(command)
    assert r["code"] == 10, f"{command!r} streams the message on stdin (unreadable) — must fail-closed"
    assert r["decision"] == "block"
    assert r["message"].startswith(rt.BLOCK_MARKER)
    msg = r["message"]
    assert "stdin" in msg.lower(), "the hint must name the unreadable-stdin cause"
    assert "-m" in msg and "-F <file>" in msg, "the hint must name the readable ways to satisfy it"
    assert "branch" in msg.lower(), "the hint must mention the branch-name path"
    assert _HATCH_ENV in msg, "the hint must name the Telegram hatch (the sanctioned out)"
    # the old `[skip-ticket: …]` trailer escape is gone — the hint must not offer it.
    assert "skip-ticket" not in msg.lower(), "the removed trailer escape must not be advertised"
    assert "REQUIRE_TICKET_SKIP" not in msg, "the removed inline-env escape must not be advertised"


def test_dash_F_stdin_hatch_approved_allows(tmp_path, monkeypatch):
    # THE spec requirement: the hatch is consulted BEFORE the `-F -` stdin block, so an approved
    # hatch lets a genuinely ticketless `-F -` commit through (read from the command env + tg-ctl,
    # never git's unreadable stdin).
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    r = _run_inproc("git commit -F -", monkeypatch, tmp_path,
                    env_extra={_HATCH_ENV: "one-off backfill, no ticket"}, tg_ctl=tg_ctl)
    assert r["code"] == 0, f"an approved hatch must allow a `-F -` commit ({r['message']})"
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]


def test_dash_F_stdin_hatch_denied_blocks(tmp_path, monkeypatch):
    # A requested hatch must fail closed when tg-ctl exits nonzero, even for the otherwise special
    # unreadable `-F -` path.
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", 'printf "approved?\\n"\nexit 1\n')
    r = _run_inproc("git commit -F -", monkeypatch, tmp_path,
                    env_extra={_HATCH_ENV: "one-off backfill, no ticket"}, tg_ctl=tg_ctl)
    assert r["code"] == 10 and r["decision"] == "block"
    assert "hatch escalation denied" in r["message"].lower()


def test_dash_F_stdin_warn_only_downgrades_to_allow():
    # REQUIRE_TICKET_STRICT=0 downgrades the `-F -` block to a warn-only allow (no marker leaked).
    r = _run("git commit -F -", strict=False)
    assert r["code"] == 0
    assert r["decision"] == "allow"
    assert rt.BLOCK_MARKER not in r["message"]
    assert "stdin" in r["message"].lower()


def test_dash_F_stdin_on_ticket_branch_allows(tmp_path):
    # A `-F -` (unreadable stdin message) commit is still ticketed by its BRANCH name, read via
    # `git -C <repo>` — same as the editor-commit path. The stdin block runs AFTER branch detection,
    # so this legit workflow passes rather than false-blocking (agent-tools#104 review finding).
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feature/ENG-42-export", str(repo)], check=True)
    r = _run(f"git -C {repo} commit -F -", strict=True)
    assert r["code"] == 0, f"branch ENG-42 should satisfy the gate even for `-F -` ({r['message']})"
    assert r["decision"] == "allow"


def test_dash_F_stdin_on_non_ticket_branch_still_blocks(tmp_path):
    # control: a `-F -` on a branch WITHOUT a ticket id still fails-closed — no branch silently
    # defeats the gate.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    r = _run(f"git -C {repo} commit -F -", strict=True)
    assert r["code"] == 10
    assert r["decision"] == "block"


# ── EXEMPTIONS still hold under the strict default ──────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    'git commit -m "chore: bump lockfile"',
    'git commit -m "docs(readme): fix typo"',
    'git commit -m "wip: spike"',
    "git commit --amend --no-edit",
    "git status",
])
def test_exempt_and_non_commit_allowed(command):
    r = _run(command)
    assert r["code"] == 0
    assert r["decision"] == "allow"


# ── OVER-MATCH REGRESSION (agent-tools#97) ──────────────────────────────────────────────────────
# The gate must fire ONLY on a real `git commit` invocation — never when the words "git"/"commit"
# merely appear as a substring/argument/message-body of some OTHER command. The old raw regex
# `\bgit\b.*\bcommit\b` over the RAW command string blocked every benign command that mentioned both
# words (a LIVE false positive: a subagent's `gh issue create` whose body said "git commit").


@pytest.mark.parametrize("command", [
    # the LIVE repro: an issue body containing the words "git commit"
    'gh issue create --title "fix gate" --body "we should git commit only on a real commit"',
    'echo "git commit"',                       # echo is not a commit
    "git log --grep=commit",                   # git log, not git commit (glued value)
    "git log --grep commit -n 5",              # git log, not git commit (separate value)
    'grep -rn "git commit" src/',              # a grep whose pattern says "commit"
    "git config commit.gpgsign true",          # mentions commit, not authoring one
    "git help commit",                         # help for the commit subcommand
    "git commit-graph write",                  # a DIFFERENT subcommand
    'tg "I will git commit the fix once review passes"',  # a status report ABOUT a commit
])
def test_mentioning_commit_is_not_gated(command):
    r = _run(command)
    assert r["code"] == 0, f"{command!r} is not a git commit — must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_real_commit_after_a_mentioning_sibling_still_blocks():
    # `echo "git commit" && git commit -m feat` — the SECOND segment is a real, ticketless commit.
    r = _run('echo "git commit" && git commit -m "feat: add export"')
    assert r["code"] == 10
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    'env FOO=bar git commit -m "feat: add export"',          # env-prefixed
    'FOO=bar git commit -m "feat: add export"',              # bare inline VAR=value
    'sudo git commit -m "feat: add export"',                 # sudo
    'sudo -u git git commit -m "feat: add export"',          # sudo -u <user named git>
    'runuser -u git -- git commit -m "feat: add export"',    # runuser
    'timeout 60 git commit -m "feat: add export"',           # timeout (operand-drop)
    '/usr/bin/git commit -m "feat: add export"',             # path-qualified git
])
def test_wrapped_no_ticket_commit_still_blocks(command):
    r = _run(command)
    assert r["code"] == 10, f"{command!r} is a real wrapped commit — must block, got exit={r['code']}"
    assert r["decision"] == "block"


# ── CLUSTERED SHORT FLAG `-am` (agent-tools#109) ─────────────────────────────────────────────────
# `git commit -am "…"` is `-a` + `-m "…"`; the message-extraction path must de-cluster combined short
# option groups the way git's commit parser does (the FIRST value letter `m`/`F` wins — glued tail or
# next token). The pre-fix code matched only `-m`/`-mMSG`/`-F`/`-FPATH`, so `-am`/`-aF`/`-amMSG` fell
# through with an EMPTY message → a ticketed `-am "Closes #5"` false-BLOCKED and a `-am "chore: …"`
# LOST its exemption. Same clustered-short-flag class block-no-verify fixed in #36–#40.


def test_am_with_ticket_allows():
    # THE issue #109 repro: a ticket in a `-am` message must be detected → ALLOW.
    r = _run('git commit -am "Closes #5 fix"')
    assert r["code"] == 0, f"a ticketed -am commit must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_am_without_ticket_blocks():
    r = _run('git commit -am "no ticket"')
    assert r["code"] == 10
    assert r["decision"] == "block"


def test_am_chore_exemption_is_preserved():
    # the second half of the bug: an empty message lost the `chore:` exemption too.
    r = _run('git commit -am "chore: cleanup"')
    assert r["code"] == 0, f"a chore: -am commit is exempt and must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_am_glued_ticket_allows():
    # `-amENG-7` — the message is glued onto the cluster.
    r = _run("git commit -amENG-7")
    assert r["code"] == 0
    assert r["decision"] == "allow"


@pytest.mark.parametrize("command", [
    'git commit -am "no ticket [skip-ticket: deliberate]"',  # removed `[skip-ticket: …]` trailer
    'REQUIRE_TICKET_SKIP=1 git commit -am "no ticket"',      # removed inline-env escape
])
def test_am_old_escapes_no_longer_bypass(command):
    """The removed per-commit escapes must NOT bypass even on a clustered `-am` commit — a
    ticketless `-am` still BLOCKs under the strict default."""
    r = _run(command)
    assert r["code"] == 10, f"{command!r} used a removed self-service escape — must still block"
    assert r["decision"] == "block"


@pytest.mark.parametrize("sep", [" ", ""])  # `-aF <file>` (next token) and `-aF<file>` (glued)
def test_am_aF_file_with_ticket_allows(tmp_path, sep):
    # `-aF` clustered — the message file must be read and its ticket detected, both spellings.
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("feat: big change\n\nCloses #99\n")
    r = _run(f"git commit -aF{sep}{msg_file}")
    assert r["code"] == 0, f"a -aF file with a ticket must allow, got exit={r['code']}"
    assert r["decision"] == "allow"


def test_amF_is_a_message_not_a_file_block(tmp_path):
    # `-amF` is message "F" (git glues F onto -m), NOT a `-F` file read → no ticket → BLOCK.
    r = _run("git commit -amF")
    assert r["code"] == 10
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    "tar -amF archive.tar",          # a clustered short flag on a NON-git command
    "ls -la",                        # benign cluster, not a commit
    'grep -rn "git commit -am" src/',  # a grep whose pattern contains `-am`
])
def test_benign_clustered_flag_non_commit_allowed(command):
    # the #37/#40 over-block cautionary tale: de-clustering must not over-match a non-commit command.
    r = _run(command)
    assert r["code"] == 0, f"{command!r} is not a git commit — must allow"
    assert r["decision"] == "allow"


def test_am_amend_value_still_blocks():
    # `git commit -am "--amend"` authors a real ticketless commit (message "--amend") → BLOCK; the
    # `--amend` is the -m VALUE, not a real skip flag.
    r = _run('git commit -am "--amend"')
    assert r["code"] == 10
    assert r["decision"] == "block"


@pytest.mark.parametrize("command", [
    'git commit -am "-F"',          # `-am "-F"`: message is "-F", the value is not a real -F flag
    "git commit -am -F -",          # `-am` takes "-F" as the message; `-` is a pathspec, NOT stdin
    "git commit -m -F -",           # short separate form
    "git commit --message -F -",    # LONG separate form (re-review #1)
])
def test_message_with_dash_F_looking_value_is_not_a_stdin_block(command):
    # agent-tools#109 review #1: a `-m`/`-am`/`--message` whose VALUE looks like `-F` must NOT be
    # re-parsed as a real `-F -` stdin read (which would false-BLOCK with the stdin hint). The
    # message "-F" carries no ticket so the commit still BLOCKS — but with the ordinary no-ticket
    # marker, NOT the stdin hint; the point is the gate did not misclassify it as a stdin commit.
    r = _run(command)
    assert r["code"] == 10, command
    assert r["decision"] == "block"
    assert "on git's stdin" not in r["message"], f"must not be the stdin hint: {command!r}"


@pytest.mark.parametrize("command", ["git commit -t -F -", "git commit -aC -F -"])
def test_reuse_template_flag_value_is_not_a_stdin_block(command):
    # agent-tools#109 re-review #2: a SEPARATE `-C`/`-t` takes its next token as a mandatory value —
    # `git commit -t -F -` is `-t` template "-F", NOT a `-F -` stdin read. Must not be the stdin hint.
    r = _run(command)
    assert r["code"] == 10
    assert "on git's stdin" not in r["message"], command


def test_reuse_message_cluster_is_not_a_dash_m_message():
    # agent-tools#109 review #2: `git commit -Cm` is `-C m` (reuse commit "m"), NOT a `-m` message.
    # With no readable message and no ticket, it blocks — and must NOT extract a phantom message.
    assert rt.commit_message_from_argv(["-Cm"]) == ""
    assert rt.commit_message_from_argv(["-um"]) == ""
    assert rt.commit_message_from_argv(["-Sm"]) == ""
    # SEPARATE `-C`/`-c`/`-t` swallow their next token too — no phantom message from the `-m`.
    assert rt.commit_message_from_argv(["-C", "-m", "x"]) == ""
    assert rt.commit_message_from_argv(["-t", "-m", "x"]) == ""


@pytest.mark.parametrize("command", [
    "git commit -S -m 'Closes #5'",   # -S optional keyid (no value) then a REAL -m
    "git commit -u -m 'Fixes ABC-7'",  # -u optional untracked mode then a REAL -m
])
def test_optional_arg_flag_then_separate_message_with_ticket_allows(command):
    # agent-tools#109 re-review #3: `-S`/`-u` have an OPTIONAL only-glued value, so a separate
    # `-S -m …` must still read the `-m` ticket → ALLOW (they must NOT consume the next token).
    r = _run(command)
    assert r["code"] == 0, f"{command!r} carries a ticket in a real -m — must allow"
    assert r["decision"] == "allow"


# ── #114: value-bearing long flags (--author/--date/--cleanup/--trailer/--pathspec-from-file) ──
# git-parseopt consumes the NEXT token for these (even when it starts with `-`), exactly like the
# already-handled --reuse-message/--template. If the parser does NOT skip the value, a value that
# looks like `-m`/`-F` is re-read as a real message flag and DIVERGES from git → ticket-gate bypass.

@pytest.mark.parametrize("flag", [
    "--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file",
])
def test_value_bearing_long_flag_does_not_eat_a_dash_m_looking_value(flag):
    # THE BYPASS (issue #114 finding 1): `git commit --author '-m' 'TICKET-1: ok'`.
    # Real git: author="-m", "TICKET-1: ok" is a PATHSPEC, no message in argv → editor opens, the
    # commit is TICKETLESS. Our old parser skipped --author (i+=1), then de-clustered the value "-m"
    # into a message flag, ate "TICKET-1: ok" → phantom ticket → gate falsely PASSED. The flag must
    # consume its value so the de-clustered "-m" is never re-read; the message is empty → BLOCK.
    r = _run(f"git commit {flag} '-m' 'TICKET-1: ok'")
    assert r["code"] == 10, f"{flag}: a -m-looking value must not satisfy the gate"
    assert r["decision"] == "block"
    assert "on git's stdin" not in r["message"], flag  # not misread as a -F - stdin commit either


@pytest.mark.parametrize("flag", [
    "--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file",
])
def test_value_bearing_long_flag_value_is_not_a_dash_F_stdin_block(flag):
    # `git commit --author '-F' -`: real git reads author="-F" and `-` as a pathspec — it does NOT
    # read stdin. The parser must skip the flag's value so the `-F` is not re-parsed as a real file
    # flag (which would falsely BLOCK with the stdin hint). It blocks for NO TICKET, not as stdin.
    r = _run(f"git commit {flag} '-F' -")
    assert r["code"] == 10, flag
    assert "on git's stdin" not in r["message"], f"{flag}: must not be misread as a -F - stdin read"


@pytest.mark.parametrize("flag", [
    "--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file",
])
def test_value_bearing_long_flag_then_real_message_with_ticket_allows(flag):
    # The legit path: the flag consumes ONLY its own value; a following real `-m` with a ticket is
    # still read → ALLOW. Proves the fix doesn't over-consume.
    r = _run(f"git commit {flag} 'someone' -m 'Closes #42'")
    assert r["code"] == 0, f"{flag}: a real -m ticket after the flag must still allow"
    assert r["decision"] == "allow"


def test_value_bearing_long_flag_parser_skips_the_value():
    # Parser-level pins (argv = tokens AFTER `commit`): the value of each value-bearing long flag
    # must NOT surface as a phantom message, mirroring the --reuse-message/--template assertions.
    for flag in ("--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file"):
        assert rt.commit_message_from_argv([flag, "-m", "x"]) == "", flag
        assert rt._nonmessage_flag_consumes_next(flag) is True, flag
    # A glued `--author=…` carries its own value and does NOT consume the next token.
    assert rt._nonmessage_flag_consumes_next("--author=someone") is False
    assert rt.commit_message_from_argv(["--author=x", "-m", "Closes #1"]) == "Closes #1"


def test_separate_gpg_sign_long_flag_does_not_swallow_a_following_message():
    # review of #114: `--gpg-sign` is deliberately OMITTED from `_OTHER_VALUE_LONG` — its value is
    # OPTIONAL and only ever GLUED (`--gpg-sign=keyid`), so a SEPARATE `--gpg-sign -m …` is the flag
    # with no value then a REAL `-m`. Adding it to the set (a plausible future "completeness" edit)
    # would be a BUG: it would eat the following `-m` ticket and falsely BLOCK. This pins that mirror
    # of the short `-S`/`-u` case for the long form, so the regression is caught.
    assert rt._nonmessage_flag_consumes_next("--gpg-sign") is False
    assert rt.commit_message_from_argv(["--gpg-sign", "-m", "Closes #1"]) == "Closes #1"
    r = _run("git commit --gpg-sign -m 'Closes #1'")
    assert r["code"] == 0, "a separate --gpg-sign must leave the following -m ticket intact"
    assert r["decision"] == "allow"


def test_value_bearing_long_flag_value_named_amend_is_not_a_skip_commit():
    # finding 2: is_skip_commit must skip a value-bearing long flag's value so a `--amend`-looking
    # value (`--author '--amend'`) is NOT misread as a real --amend skip flag. The token after the
    # flag is its VALUE; the commit is a fresh authoring commit, not an amend → not skipped.
    assert rt.is_skip_commit(["--author", "--amend", "-m", "x"]) is False
    assert rt.is_skip_commit(["--trailer", "--amend"]) is False
    assert rt.is_skip_commit(["-C", "--amend", "-m", "x"]) is False  # short next-token form
    # CLUSTERED short next-token form (`-aC`): the fall-through routes it through
    # `_nonmessage_flag_consumes_next`, so `is_skip_commit` must also skip its value. Real git reads
    # `-C`="--amend" (a reuse-message ref, NOT an amend) → not a skip commit. This closes the same
    # three-parser symmetry that `commit_reads_stdin_message` already pins via `-aC -F -`.
    assert rt.is_skip_commit(["-aC", "--amend"]) is False
    # GLUED short value (`-Cabc`) carries its value inside the token → does NOT consume the next, so
    # a following real `--amend` IS a skip commit (the new path must not over-consume).
    assert rt._nonmessage_flag_consumes_next("-Cabc") is False
    assert rt.is_skip_commit(["-Cabc", "--amend"]) is True
    # control: a REAL --amend is still a skip commit.
    assert rt.is_skip_commit(["--amend", "-m", "x"]) is True


def test_glued_short_message_ending_in_value_letter_is_not_a_next_token_consumer():
    # review of #114: the broadened `_takes_following_message_value` must judge a message/file token
    # ENTIRELY by its de-cluster result and NEVER fall through to `_nonmessage_flag_consumes_next`,
    # or a glued message ending in `c`/`C`/`t` (`-amFixIt`, `-amC`) could be misread as a next-token
    # consumer → `is_skip_commit` would eat the following `--amend` and miss a real amend (false
    # BLOCK of a legit ticketless amend). A glued message carries its value inside the token, so the
    # NEXT token is NOT consumed.
    for tok in ("-amFixIt", "-amC", "-amc", "-amt", "-aFpath", "-mC"):
        assert rt._takes_following_message_value(tok) is False, tok
    # the exact reviewer repro: a real --amend after a glued -am message IS still a skip commit.
    assert rt.is_skip_commit(["-amFixIt", "--amend"]) is True
    assert rt.is_skip_commit(["-amC", "--amend"]) is True


def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl — a pre-bash hook parses the leading RIG_HATCH_REQUEST_… assignment out of the command
    string the event carries. Regression guard for the documented inline form being unusable
    (Codex P1 on #233): before the fix the var was only read from os.environ and this blocked."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    command = (
        'RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="one-off backfill, no ticket warranted" '
        'git commit -m "feat: x"'
    )
    # env var deliberately NOT set — only the inline prefix
    r = _run_inproc(command, monkeypatch, tmp_path, tg_ctl=tg_ctl)
    assert r["code"] == 0 and r["decision"] == "allow"
    assert "hatch escalation" in r["message"].lower()


def test_hatch_inline_command_justification_allows_dash_F_stdin(tmp_path, monkeypatch):
    """The inline prefix also works for `-F -`, proving the hatch is consulted before stdin blocks."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    command = (
        'RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="one-off backfill, no ticket warranted" '
        "git commit -F -"
    )
    r = _run_inproc(command, monkeypatch, tmp_path, tg_ctl=tg_ctl)
    assert r["code"] == 0 and r["decision"] == "allow"
    assert "hatch escalation" in r["message"].lower()


def test_hatch_inline_command_after_separator_allows(tmp_path, monkeypatch):
    """The inline prefix is honored even when the commit is not the first command on the line."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    command = (
        'echo staging '
        '&& RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="one-off backfill" '
        'git commit -m "feat: x"'
    )
    r = _run_inproc(command, monkeypatch, tmp_path, tg_ctl=tg_ctl)
    assert r["code"] == 0 and r["decision"] == "allow"
