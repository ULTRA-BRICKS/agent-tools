"""Tests for agent-hooks/pkill-guard/pkill_guard.py.

Covers:
  - True positives: `pkill -f <shared-name>`, `killall <shared-name>`,
    `kill $(pgrep -f <shared-name>)`, `` kill `pgrep -f <shared-name>` ``,
    `pgrep <shared-name> | xargs kill`, and the "narrow grep" pipeline shape
    (`ps aux | grep <shared-name> | ... | xargs kill`) are blocked.
  - The two real incidents this hook exists for: `pkill -f "review diff"` and a
    grep-into-kill pipeline targeting a shared name.
  - Session-scoped patterns are ALLOWED even when they also name a shared tool (a path, the
    harness's isolation-prefix token, a hex/uuid-looking run).
  - `kill <pid>` (bare, multiple PIDs, with -SIGNAL) is always allowed.
  - An unrecognized/unlisted pattern is allowed (fails open on unknowns).
  - `dev stop` and unrelated commands are allowed.
  - Wrapped forms (`sudo pkill -f node`, `timeout 5 killall node`) don't defeat detection.
  - Shell chains: the dangerous form behind `&&`/`;`/newline is still caught.
  - External Telegram hatch: unset denies (block), a real justification + tg-ctl exit 0
    allows, tg-ctl exit nonzero denies, a blank/bare-flag value denies without asking.
  - Fail-closed: unbalanced quotes with a plausible dangerous hint, and a malformed event.

Run from the repo root::

    uv run --with "pytest>=8,<9" python -m pytest tests/test_pkill_guard.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "pkill-guard"
    / "pkill_guard.py"
)
_spec = importlib.util.spec_from_file_location("pkill_guard", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

# A throwaway cwd — this hook has no rig.yaml walk-up (only the Telegram hatch), but keep the
# same hermetic-cwd discipline as the sibling hook tests.
_HERMETIC_CWD = tempfile.mkdtemp(prefix="pkg-hermetic-")


def _run(command: str, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    """Run the hook with a `pre-bash` event carrying `command`. Returns (stdout, stderr, exit)."""
    event: dict = {"args": {"command": command}, "cwd": _HERMETIC_CWD}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("RIG_HATCH_REQUEST_PKILL_GUARD", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── the bridge execs the descriptor's `cmd` DIRECTLY (subprocess.run([cmd, ...])), exactly the
# shape `cc_hook_bridge/dispatch.py` uses — importlib-loading (every test above) can't catch a
# missing executable bit or a missing shebang; only a real subprocess exec of the file itself
# exercises the actual invocation. Without the executable bit, `subprocess.run([cmd], ...)`
# raises `PermissionError` (EACCES), which `agent_hooks_v1.run_hook` resolves via this
# descriptor's `on_error: closed` — meaning a missing `chmod +x` fails CLOSED here (denies every
# Bash command at priority 10), the opposite direction from pin-primary-worktree's
# missing-shebang regression (which fails OPEN, `on_error: open`) but just as much a real outage.

_HATCH_ENV_KEY = "RIG_HATCH_REQUEST_PKILL_GUARD"


def test_hook_is_directly_executable(tmp_path):
    event = {"cwd": str(tmp_path), "args": {"command": "pkill -f node"}}
    env = {k: v for k, v in os.environ.items() if k != _HATCH_ENV_KEY}
    proc = subprocess.run(
        [str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=False,
    )
    assert proc.returncode == hook.BLOCK_EXIT_CODE, (proc.returncode, proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["decision"] == "block"


# ── True positives: direct pkill/killall of a shared name — BLOCK ──────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        'pkill -f "review diff"',  # the actual 2026-06-26 incident shape
        "pkill -f node",
        "pkill -9 -f codex",
        "killall node",
        "killall -9 claude",
    ],
)
def test_block_direct_pattern_kill_of_shared_name(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_message_names_pid_and_dev_stop_alternatives(monkeypatch):
    out, _err, _code = _run("pkill -f node", monkeypatch)
    msg = json.loads(out)["message"]
    assert "kill <pid>" in msg
    assert "dev stop" in msg


# ── True positives: kill fed a pgrep substitution — BLOCK ───────────────────────────────────

def test_block_kill_dollar_paren_pgrep(monkeypatch):
    out, _err, code = _run('kill $(pgrep -f codex)', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_kill_dollar_paren_pgrep_unlisted_name(monkeypatch):
    """The substitution-extraction branch must ALLOW too, not just block — a pgrep pattern
    that isn't on the denylist is safe to feed into kill."""
    out, _err, code = _run('kill $(pgrep -f "my-custom-project-server")', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_kill_substitution_with_unparseable_dangerous_hint(monkeypatch):
    """The SUBSTITUTION's own inner text failing to parse (an unclosed quote) must fail closed
    exactly like the same text WOULD outside a substitution — not silently skip to allow."""
    out, _err, code = _run("kill $(pgrep -f 'review diff)", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_kill_backtick_pgrep(monkeypatch):
    out, _err, code = _run("kill `pgrep -f playwright`", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_kill_dollar_paren_pgrep_with_signal(monkeypatch):
    out, _err, code = _run('kill -9 $(pgrep -f "review diff")', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── True positives: pipeline forms — BLOCK ──────────────────────────────────────────────────

def test_block_pgrep_pipe_xargs_kill(monkeypatch):
    out, _err, code = _run("pgrep -f playwright | xargs kill", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_pgrep_pipe_xargs_kill_with_flags(monkeypatch):
    out, _err, code = _run("pgrep -f node | xargs -I{} kill -9 {}", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_narrow_grep_pipeline(monkeypatch):
    """The 2026-06-27 incident shape: ps | grep <shared-name> | awk | xargs kill."""
    out, _err, code = _run(
        "ps aux | grep claude | awk '{print $2}' | xargs kill -9", monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_pgrep_pipe_bare_kill(monkeypatch):
    out, _err, code = _run("pgrep -f node | kill", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_xargs_with_unrecognized_value_flag(monkeypatch):
    """Regression: `-E` is not a flag this hook confidently knows takes/doesn't take a value —
    the fallback must still find `kill` later in argv rather than silently allow."""
    out, _err, code = _run("pgrep -f node | xargs -E '' kill", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "pgrep -f node | xargs env kill",
        "pgrep -f node | xargs sudo kill",
        "pgrep -f node | xargs timeout 5 kill",
        "pgrep -f node | xargs env sudo kill",  # a wrapper chain, not just one level
    ],
)
def test_block_xargs_wrapped_command_through_wrapper_table(command, monkeypatch):
    """Regression (Codex round-4 finding): xargs's wrapped-command resolution used to compare
    xargs's IMMEDIATE payload token against the literal string "kill" — a wrapper executable
    there (`env`, `sudo`, `timeout`) always failed that comparison even though the wrapper
    transparently passes `kill` through to the OS. Must resolve through `_strip_wrappers` the
    same way a top-level stage already does."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_xargs_wrapped_pkill(monkeypatch):
    """The xargs-wrapped-command check also accepts `pkill`/`killall`, not just bare `kill`."""
    out, _err, code = _run("pgrep -f node | xargs env pkill -f", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_xargs_wrapper_chain_overflow_fails_closed(monkeypatch):
    """A wrapper chain inside xargs's payload deeper than the nesting cap propagates
    `_WrapperOverflow` up to `_classify`'s existing fail-closed handler — same as every other
    `_strip_wrappers` call site in this file."""
    payload = "env " * (hook._MAX_WRAPPER_NESTING + 1) + "echo hi"
    out, _err, code = _run(f"pgrep -f node | xargs {payload}", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_xargs_wrapped_non_kill_command(monkeypatch):
    """A wrapper around a non-kill command must still be allowed — the wrapper-peel must not
    make this function classify EVERY wrapped xargs payload as kill-capable."""
    out, _err, code = _run("pgrep -f node | xargs env echo hi", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_pidof_pipe_xargs_kill(monkeypatch):
    out, _err, code = _run("pidof node | xargs kill", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_kill_dollar_paren_pidof(monkeypatch):
    out, _err, code = _run("kill $(pidof codex)", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_pidof_unlisted_name(monkeypatch):
    out, _err, code = _run("pidof my-custom-server | xargs kill", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_pipeline_pattern_before_a_filtering_stage(monkeypatch):
    """A filtering stage (grep -v) BEFORE the real pattern must not hide it (all earlier
    grep-family stages are checked, not just the first)."""
    out, _err, code = _run(
        "ps aux | grep -v ignored | grep node | xargs kill", monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_pgrep_before_a_non_kill_last_stage(monkeypatch):
    """A pipeline with no kill-performing stage AT ALL is not gated — a pgrep/grep stage alone
    doesn't do anything dangerous by itself."""
    out, _err, code = _run("pgrep -f node | wc -l", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_kill_in_non_final_pipeline_stage(monkeypatch):
    """Regression (Codex round-5 finding): a kill genuinely executing in a NON-FINAL pipeline
    stage, with a trailing consumer reading its output (`| tee log.txt`), must still be caught —
    the kill already ran; a trailing stage reading its stdout does not undo it. A prior fix that
    narrowed detection to "only the last stage" (to match an inaccurate doc description) was
    itself a real detection regression."""
    out, _err, code = _run("pgrep -f node | xargs kill | tee kill.log", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Regression: redirection must not defeat pattern extraction ─────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "pkill -f node >/dev/null 2>&1",
        "pkill -f node > /dev/null 2>&1",
        "killall node 2>/dev/null",
    ],
)
def test_block_pattern_kill_survives_trailing_redirection(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_pattern_kill_survives_leading_redirection(monkeypatch):
    """A redirect appearing BEFORE the real arguments must not swallow them too — the fix is to
    remove operator+target PAIRS wherever they occur, not truncate at the first one."""
    out, _err, code = _run("pkill 2>/dev/null -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_wrapped_command_survives_leading_fd_digit_redirect(monkeypatch):
    """Regression: `sudo 2>/dev/null pkill -f node` tokenizes to `['sudo', '2', '>',
    '/dev/null', 'pkill', '-f', 'node']` — `_skip_wrapper_args` stops peeling `sudo`'s flags at
    the first non-flag token (`'2'`), so without stripping the fd-digit prefix too, the orphan
    `'2'` survives as `argv[0]` and hides the real `pkill` behind it."""
    out, _err, code = _run("sudo 2>/dev/null pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "2>/dev/null sudo pkill -f node",
        "2>/dev/null FOO=1 pkill -f node",
    ],
)
def test_block_wrapped_command_survives_leading_redirect_before_wrapper(command, monkeypatch):
    """Regression (k3 round-5 finding): a redirect appearing BEFORE the wrapper/inline-env
    assignment, not just after it, used to defeat `_stage_argv` entirely — a single fixed-order
    pass (strip env/wrappers, then redirections) never revisits `argv[0]` once a leading
    redirect is removed, so `sudo`/`FOO=1` right after it was never peeled and survived as the
    stage's `argv[0]`, making the whole stage invisible to the pkill/killall check. `_stage_argv`
    now loops every strip to a fixpoint instead of a single fixed order."""
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Regression: comment handling must not create a quote-balance false merge ───────────────

def test_block_trailing_comment_after_dangerous_pattern(monkeypatch):
    out, _err, code = _run("pkill -f node # cleanup old dev server", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_two_line_command_with_normal_comment_then_safe(monkeypatch):
    """A comment on line 1 must not prevent line 2 from being tokenized as its own, independent
    command."""
    out, _err, code = _run("echo hi # just a note\necho bye", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_comment_on_first_line_then_dangerous_kill_on_second(monkeypatch):
    out, _err, code = _run("echo hi # just a note\npkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_quoted_value_containing_hash_is_not_treated_as_comment(monkeypatch):
    """A quoted argument that merely CONTAINS a `#` must survive whole — only a genuinely
    unquoted `#` is a comment marker."""
    out, _err, code = _run("echo 'value # not a comment'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_comment_with_stray_quote_does_not_swallow_next_line_kill(monkeypatch):
    """Regression (Codex round-4 finding): a stray apostrophe sitting INSIDE a real comment on
    line 1 (`# it's a comment`) used to be treated as a genuinely unclosed quote, merging the
    comment forward into line 2 and swallowing the real `pkill -f node` there inside what looked
    like one giant quoted argument — classifying the whole command as safe. The dual-pass
    raw/posix tokenizer must not silently drop the real command; it must at minimum fail closed
    (block) rather than allow."""
    out, _err, code = _run("echo ok # it's a comment\npkill -f node # '", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Regression: every positional is checked, not just the trailing one ─────────────────────

def test_block_pkill_pattern_before_a_trailing_value_flag(monkeypatch):
    """A flag's own value operand (`-u root`) must not be mistaken for THE pattern and hide
    the real one (`node`) sitting earlier in the argument list."""
    out, _err, code = _run("pkill -f node -u root", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_killall_multi_name_with_shared_name_first(monkeypatch):
    out, _err, code = _run("killall node my-project-worker", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_killall_multi_name_with_shared_name_last(monkeypatch):
    out, _err, code = _run("killall my-project-worker node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Allowed: session-scoped patterns, even naming a shared tool ────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        'pkill -9 -f "hvsc-3-a1b2c3d4"',  # the sanctioned e2e-harness recipe
        'pkill -f "/Users/ultra/work/hyperide-worktrees/agent-x/node_modules/.bin/vitest"',
        'killall -9 "node-worker-8f3a9c21"',
        'pkill -f "codex-session-4471"',
    ],
)
def test_allow_session_scoped_pattern(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Regression: a bare port number / a global system bin path is NOT session-scoping ───────

@pytest.mark.parametrize(
    "command",
    [
        # A 4-digit dev port is shared by every concurrent session on that default port — it
        # does not narrow a pattern-kill to the caller's own process.
        'pkill -f "chrome --remote-debugging-port=9222"',
        'pkill -f "node --inspect=9229"',
        # A bare path to a well-known GLOBAL interpreter binary matches every process on the
        # machine using the system interpreter — it is not session-scoping either.
        'pkill -f "/opt/homebrew/bin/node"',
        'pkill -f "/usr/bin/python3"',
        'pkill -f "/usr/local/bin/node"',
    ],
)
def test_block_pattern_with_non_scoping_signal(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_five_digit_run_still_scoped(monkeypatch):
    """A 5+ digit run (unlike a 4-digit port) still counts as a scoping signal."""
    out, _err, code = _run('pkill -f "node 84471"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Allowed: PID-targeted kill ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "command",
    [
        "kill 12345",
        "kill -9 12345",
        "kill -s TERM 12345",
        "kill 111 222 333",
        "kill %1",
    ],
)
def test_allow_pid_targeted_kill(command, monkeypatch):
    out, _err, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Allowed: unrecognized pattern, unrelated commands, dev stop ────────────────────────────

def test_allow_unlisted_process_name(monkeypatch):
    out, _err, code = _run('pkill -f "my-custom-project-server"', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_dev_stop(monkeypatch):
    out, _err, code = _run("dev stop", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_unrelated_command(monkeypatch):
    out, _err, code = _run("git status", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_grep_mentioning_pkill_as_text(monkeypatch):
    """Text that merely mentions pkill/node — no pkill/kill invocation — is allowed."""
    out, _err, code = _run('grep -r "pkill -f node" docs/', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_ps_grep_without_kill_stage(monkeypatch):
    """A pipeline that merely LOOKS at processes (no kill/xargs-kill stage) is allowed."""
    out, _err, code = _run("ps aux | grep node", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── Wrapped forms don't defeat detection ────────────────────────────────────────────────────

def test_block_sudo_wrapped(monkeypatch):
    out, _err, code = _run("sudo pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_timeout_wrapped(monkeypatch):
    out, _err, code = _run("timeout 5 killall node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_sudo_with_user_flag_wrapped(monkeypatch):
    out, _err, code = _run("sudo -u alice pkill -f codex", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_env_wrapped(monkeypatch):
    out, _err, code = _run("env pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_env_wrapped_with_inline_assignment(monkeypatch):
    out, _err, code = _run("env FOO=1 pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_ionice_with_value_flag_wrapped(monkeypatch):
    """Regression: `ionice` is a recognized wrapper but `-c` was previously missing from its
    value-flag table, so the flag's value (`3`) was misread as the wrapped command and
    `pkill -f node` after it was never inspected at all."""
    out, _err, code = _run("ionice -c 3 pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_stdbuf_with_value_flag_wrapped(monkeypatch):
    out, _err, code = _run("stdbuf -o L pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_sudo_with_prompt_value_flag_wrapped(monkeypatch):
    out, _err, code = _run('sudo -p "password: " pkill -f node', monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_leading_inline_env_assignment(monkeypatch):
    out, _err, code = _run("FOO=1 pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_backgrounded_pkill(monkeypatch):
    """`&` (a background operator) is a group separator, same as `;`/`&&` — the dangerous form
    before it must still be caught."""
    out, _err, code = _run("pkill -f node &", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Shell chains: the dangerous form behind &&/;/newline is still caught ───────────────────

def test_block_behind_and_and(monkeypatch):
    out, _err, code = _run("echo hi && pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_behind_semicolon(monkeypatch):
    out, _err, code = _run("cd /repo; pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_on_second_line(monkeypatch):
    out, _err, code = _run("cd /repo\npkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_quoted_value_spanning_a_real_newline_then_dangerous_kill(monkeypatch):
    """A double-quoted value that legitimately spans a real newline (an unbalanced quote on its
    OWN line) must merge forward into the next line rather than being treated as a premature
    command boundary — and the dangerous command on the following real line is still caught."""
    out, _err, code = _run(
        'echo "multi\nline value"\npkill -f node', monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Fail-closed paths ────────────────────────────────────────────────────────────────────────

def test_wrapper_chain_overflow_blocks_even_on_innocuous_payload(monkeypatch):
    """A wrapper chain deeper than the nesting cap can't be resolved to a real command — this
    must fail CLOSED (block) rather than silently treat the unresolved chain as safe, even when
    the actual wrapped command (`echo hi`) has nothing to do with killing anything."""
    chain = "sudo " * (hook._MAX_WRAPPER_NESTING + 1) + "echo hi"
    out, _err, code = _run(chain, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_with_dangerous_hint_blocks(monkeypatch):
    out, _err, code = _run("pkill -f 'node --unclosed", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unbalanced_quotes_unrelated_command_allowed(monkeypatch):
    out, _err, code = _run("echo won't fail", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_malformed_event_blocks(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    out_buf, err_buf = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.setattr(sys, "stderr", err_buf)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out_buf.getvalue()) == "block"


def test_missing_command_field_allows(monkeypatch):
    """No command at all (an event with an empty args) is trivially safe."""
    out, _err, code = _run("", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── External Telegram hatch escalation ──────────────────────────────────────────────────────

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


def test_hatch_unset_denies(monkeypatch):
    out, _err, code = _run("pkill -f node", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_hatch_blank_value_denies_without_asking(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "touch asked; exit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run("pkill -f node", monkeypatch, {"RIG_HATCH_REQUEST_PKILL_GUARD": "   "})
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert not (tmp_path / "asked").exists()


def test_hatch_bare_flag_value_denies_without_asking(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "touch asked; exit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    _out, _err, code = _run("pkill -f node", monkeypatch, {"RIG_HATCH_REQUEST_PKILL_GUARD": "1"})
    assert code == hook.BLOCK_EXIT_CODE
    assert not (tmp_path / "asked").exists()


def test_hatch_exit0_allows(monkeypatch, tmp_path):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n" + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "pkill -f node",
        monkeypatch,
        {"RIG_HATCH_REQUEST_PKILL_GUARD": "Cleaning up my own stray dev-server instances."},
    )
    assert code == 0
    assert _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_exit_nonzero_denies(monkeypatch, tmp_path):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _err, code = _run(
        "killall node",
        monkeypatch,
        {"RIG_HATCH_REQUEST_PKILL_GUARD": "Need a one-off exception."},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert "hatch escalation denied" in json.loads(out)["message"].lower()
