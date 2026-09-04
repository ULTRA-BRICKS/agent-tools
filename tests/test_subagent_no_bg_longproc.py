"""Tests for the subagent-no-bg-longproc agent-hook (pre-bash, hard block).

The wedge this kills (agent-tools#52): a dispatched SUBAGENT backgrounds a long process
(`review` / test-suite / `--watch` / long `sleep`) — via `run_in_background: true`, a shell
`&`, or `setsid` — then ends its turn awaiting a completion notification it will never
receive (only the main loop is re-invoked by background completion), wedging forever.

Covers: BLOCK (subagent backgrounding a long process, every background form), ALLOW (subagent
runs the long process FOREGROUND; the orchestrator — no agent_id — backgrounds it; a subagent
backgrounds a SHORT command; a keyword inside a quoted arg; redirections that contain `&`),
and the deny-by-default Telegram hatch escalation (the old ALLOW_SUBAGENT_BACKGROUND env +
`# subagent-bg-ok:` sentinel are DEAD; RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC with a written
justification asks tg-ctl and allows only on exit 0, a bare `1` denies).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_subagent_no_bg_longproc.py -q
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
    / "subagent-no-bg-longproc"
    / "subagent_no_bg_longproc.py"
)
_spec = importlib.util.spec_from_file_location("subagent_no_bg_longproc", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _run(
    command,
    monkeypatch,
    *,
    agent_id="sub-1",
    run_in_background=None,
    env: dict | None = None,
) -> tuple[str, str, int]:
    args = {"command": command}
    if agent_id is not None:
        args["agent_id"] = agent_id
    if run_in_background is not None:
        args["run_in_background"] = run_in_background
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_SUBAGENT_BACKGROUND", "ALLOW_SUBAGENT_BACKGROUND_REASON",
              "RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: a subagent backgrounding a long process (the wedge) ───────────────────────────

@pytest.mark.parametrize("command", [
    "review",
    "review -C /repo",
    "review diff -C /repo -m claude:claude-opus-4-8",
    "gh pr checks 42 --watch",
    "vitest --watch",
    "npm test",
    "pnpm build",
    "pytest tests/",
    "cargo test",
    "make all",
    "sleep 30",
    "sleep 5m",
])
def test_block_subagent_run_in_background_flag(command, monkeypatch):
    """`run_in_background: true` on a long process from a subagent → BLOCK."""
    out, _e, code = _run(command, monkeypatch, run_in_background=True)
    assert code == hook.BLOCK_EXIT_CODE, command
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "SUBAGENT" in payload["message"]
    assert "FOREGROUND" in payload["message"]


@pytest.mark.parametrize("flag", ["true", "True", "1", "yes", 1])
def test_block_run_in_background_truthy_forms(flag, monkeypatch):
    """The flag may arrive stringified/integer from a wrapping harness → still BLOCK."""
    out, _e, code = _run("review diff -C /repo", monkeypatch, run_in_background=flag)
    assert code == hook.BLOCK_EXIT_CODE, flag
    assert _decision(out) == "block"


@pytest.mark.parametrize("flag", [False, "false", "0", 0])
def test_allow_run_in_background_falsey_forms(flag, monkeypatch):
    """A false/0/absent flag is foreground → ALLOW (the flag can't false-positive)."""
    out, _e, code = _run("review diff -C /repo", monkeypatch, run_in_background=flag)
    assert code == 0, flag
    assert _decision(out) == "allow"


def test_run_in_background_detaches_a_non_leading_long_job(monkeypatch):
    """`run_in_background: true` backgrounds the WHOLE command line, so a long process that is
    FOREGROUND relative to a `&` is still detached → BLOCK. `echo x & review -C /r` with the flag
    set blocks even though the `&` only backgrounds `echo` — the README's central claim, and a
    guard against `_wedge_label` losing `run_in_background` for a non-leading job (review #1)."""
    out, _e, code = _run("echo x & review -C /r", monkeypatch, run_in_background=True)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_agent_id_top_level_event_fallback(monkeypatch):
    """`agent_id` may be surfaced at the top level of the event (not under args) → still treated
    as a subagent and BLOCK. Symmetric with `test_run_in_background_top_level_event_fallback`
    (`_is_subagent` reads `args` then falls back to the top-level event)."""
    event = {"args": {"command": "review diff -C /repo", "run_in_background": True},
             "agent_id": "sub-top"}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_SUBAGENT_BACKGROUND", "ALLOW_SUBAGENT_BACKGROUND_REASON",
              "RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC"):
        monkeypatch.delenv(k, raising=False)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


def test_run_in_background_top_level_event_fallback(monkeypatch):
    """The flag may be surfaced at the top level of the event (not under args) → still BLOCK.

    Symmetry with `command`/`agent_id`, which both fall back to the top-level event (review #3).
    """
    event = {"args": {"agent_id": "sub-1", "command": "review diff -C /repo"},
             "run_in_background": True}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_SUBAGENT_BACKGROUND", "ALLOW_SUBAGENT_BACKGROUND_REASON",
              "RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC"):
        monkeypatch.delenv(k, raising=False)
    code = hook.main()
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out.getvalue()) == "block"


@pytest.mark.parametrize("command", [
    "review diff -C /repo &",            # trailing background operator
    "review -C /r & echo started",       # `&` backgrounds the review (segment to its left)
    "npm test &",
    "pytest tests/ &",
    "sleep 30 &",
    "nohup review -C /r &",              # nohup+& → the `&` makes it a background
    "setsid review diff -C /r",          # setsid detaches without a `&`
    "timeout 20m setsid review -C /r",   # setsid behind a no-op wrapper still detaches
    "review -C /r | tee review.log &",   # `&` backgrounds the WHOLE pipeline incl. review
    "review -C /r && git commit -am x &",  # `&` backgrounds the WHOLE AND-OR list incl. review
    "lint && pytest tests/ &",           # `&` backgrounds the AND-list incl. the suite
    "review -C /r |& tee log &",         # `|&` pipe-both is within-job; the `&` backgrounds it
])
def test_block_subagent_shell_backgrounding(command, monkeypatch):
    """A subagent backgrounding a long process via shell `&` / `setsid` → BLOCK.

    `&` backgrounds the WHOLE preceding pipeline / AND-OR list (correct bash job semantics), so
    `review | tee log &` and `review && commit &` are caught, not just a bare `review &`.
    """
    out, _e, code = _run(command, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


# ── ALLOW: backgrounding binds to the SPECIFIC segment, not the whole line (review #1) ─────

@pytest.mark.parametrize("command", [
    "echo started & review -C /r",       # `echo` job backgrounded; review runs FOREGROUND
    "npm run dev & review -C /r",        # a dev server backgrounded; review FOREGROUND
    "review -C /r ; echo bye &",         # `;` ends review's job FOREGROUND; only `echo` is bg
    "make build setsid",                 # `setsid` is an ARG, not a leading detacher (review #2)
])
def test_allow_long_process_foreground_after_other_bg(command, monkeypatch):
    """A `&` (or `setsid`) bound to a DIFFERENT, non-long job must NOT block a FOREGROUND long
    process elsewhere on the line — the wedge is the long process being detached, not any `&`
    anywhere (regression for review findings #1/#2)."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "(review -C /r)&",            # subshell — the `&` binds to the subshell close
    "review <(git diff) &",       # process substitution — the `(` ends the inner job
    "foo $(review) &",            # command substitution — `review` is inside `$( )`
])
def test_nested_shell_background_is_documented_underblock(command, monkeypatch):
    """A long process inside a backgrounded NESTED shell construct — subshell `(review)&`,
    process substitution `review <(…) &`, command substitution `foo $(review) &` — is a
    documented under-block: the construct's paren/opener is a job boundary that ends the inner
    job before the `&`. Pinned (all three forms the README/docstring name) so a tokenizer change
    can't silently flip them and a future precise fix flips them deliberately."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


def test_background_then_wait_is_documented_overblock(monkeypatch):
    """`review … & wait` backgrounds review then blocks on `wait`, so it would NOT actually wedge
    — but the gate flags it (documented, acceptable over-block: the remedy "run it foreground" is
    the simpler equivalent, and the escape hatch covers the rare case). Pinned so the over-block
    stays a deliberate, documented choice rather than drifting silently."""
    out, _e, code = _run("review -C /r & wait", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_sleep_at_exactly_ten_seconds_boundary(monkeypatch):
    """`sleep 10` is the inclusive boundary (>= 10s is "long"); backgrounded → BLOCK.

    Guards the `>= 10` comparison against an off-by-one drift to `> 10`."""
    out, _e, code = _run("sleep 10 &", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── ALLOW: a subagent running the long process FOREGROUND (the correct shape) ─────────────

@pytest.mark.parametrize("command", [
    "review",
    "review diff -C /repo -m claude:claude-opus-4-8",
    "npm test",
    "pytest tests/",
    "gh pr checks 42 --watch",
    "sleep 30",
    "nohup review -C /r",                # nohup WITHOUT `&` runs foreground and blocks
    "lint && npm test",                  # `&&` is logical AND, not a background
])
def test_allow_subagent_foreground_longprocess(command, monkeypatch):
    """A subagent running a long process FOREGROUND (no background flag/operator) → ALLOW."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── ALLOW: the orchestrator (no agent_id) is governed by no-long-inline-process ───────────

def test_allow_orchestrator_backgrounds_long_process(monkeypatch):
    """The orchestrator backgrounding a long process is NOT this gate's concern → ALLOW.

    (The orchestrator's discipline is `no-long-inline-process` / `background-subagent-gate`.)
    """
    out, _e, code = _run("review diff -C /repo", monkeypatch, agent_id=None, run_in_background=True)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_orchestrator_shell_background(monkeypatch):
    out, _e, code = _run("npm test &", monkeypatch, agent_id=None)
    assert code == 0
    assert _decision(out) == "allow"


# ── ALLOW: backgrounded but NOT a long process, or a quoted-keyword false-positive guard ──

def test_allow_subagent_backgrounds_short_command(monkeypatch):
    """Backgrounding a SHORT command is no wedge risk → ALLOW."""
    out, _e, code = _run("cp big big2 &", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_keyword_in_quoted_arg_to_other_command(monkeypatch):
    """A long-process keyword inside a quoted argument to a DIFFERENT command never trips it."""
    out, _e, code = _run('tg --tag report "ran review today" &', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "npm test 2>&1 | tee log",           # `2>&1` is a redirect (`>&`), NOT a background
    "review -C /r >& out.log",           # `>&` redirect, foreground
    "npm test &>out.log",                # `&>` redirect-both, foreground
])
def test_allow_redirections_are_not_backgrounding(command, monkeypatch):
    """A `&` fused into a redirection (`2>&1`, `>&`, `&>`) is foreground, not a background."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


def test_allow_short_sleep_backgrounded(monkeypatch):
    """A short `sleep` (< 10s) backgrounded is not a long process → ALLOW."""
    out, _e, code = _run("sleep 5 &", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline) ──────────────────

def test_old_env_escape_hatch_no_longer_bypasses(monkeypatch):
    """ALLOW_SUBAGENT_BACKGROUND=1 + _REASON as a real env pair must NO LONGER allow a
    backgrounded long process — the self-service bypass was removed (replaced by the Telegram
    hatch)."""
    out, _e, code = _run(
        "review diff -C /repo &",
        monkeypatch,
        env={"ALLOW_SUBAGENT_BACKGROUND": "1", "ALLOW_SUBAGENT_BACKGROUND_REASON": "watchdog"},
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_old_inline_sentinel_no_longer_bypasses(monkeypatch):
    """A genuine trailing `# subagent-bg-ok: …` comment (which previously ALLOWED) must now still
    BLOCK — the inline sentinel is gone."""
    out, _e, code = _run(
        "echo prep\nreview -C /r &  # subagent-bg-ok: handoff to orchestrator",
        monkeypatch,
    )
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Telegram hatch escalation (deny-by-default) ────────────────────────────────────────────

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


def test_hatch_unset_blocks_and_names_env_var(monkeypatch):
    out, _e, code = _run("review diff -C /repo &", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run("review diff -C /repo &", monkeypatch,
                         env={"RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC": "1"})
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        "review diff -C /repo &", monkeypatch,
        env={"RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC": "Self-managed watchdog, polls inline."},
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        "review diff -C /repo &", monkeypatch,
        env={"RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC": "Self-managed watchdog, polls inline."},
    )
    assert code == hook.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── fail-open & robustness ───────────────────────────────────────────────────────────────

def test_unparseable_command_fails_open(monkeypatch):
    """An unbalanced-quote command can't be tokenized → ALLOW (on_error=open)."""
    out, _e, code = _run('review -C "/repo &', monkeypatch, run_in_background=True)
    assert code == 0
    assert _decision(out) == "allow"


def test_bad_json_fails_open(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert _decision(out.getvalue()) == "allow"


def test_forged_tool_input_agent_id_is_trusted_signal(monkeypatch):
    """The hook trusts whatever ``args.agent_id`` the bridge supplies — the bridge is the
    trust boundary that drops a forged ``tool_input.agent_id`` (see cc_hook_bridge). When NO
    agent_id is present the orchestrator path is taken → ALLOW even when backgrounding."""
    out, _e, code = _run("review diff -C /repo &", monkeypatch, agent_id=None)
    assert code == 0
    assert _decision(out) == "allow"


# ── multiline / heredoc (this hook carries its OWN copy of the tokenizer) ─────────────────

def test_block_backgrounded_long_process_on_later_line(monkeypatch):
    """A NEWLINE is a job separator (`;`); a `review … &` on its own line is still backgrounded."""
    out, _e, code = _run("echo hi\nreview -C /r &", monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_foreground_long_process_on_later_line(monkeypatch):
    """A multiline command whose long process runs FOREGROUND (earlier line backgrounded) → ALLOW."""
    out, _e, code = _run("echo hi &\nreview -C /r", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_heredoc_body_that_looks_like_a_backgrounded_runner(monkeypatch):
    """A here-document BODY line that looks like a backgrounded runner is data, not a command."""
    out, _e, code = _run("cat <<EOF\nreview -C /r &\nEOF", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── drift guard: the long-process detection is copied VERBATIM from the sibling hook ──────

def _load_sibling():
    sib = (
        Path(__file__).resolve().parents[1]
        / "agent-hooks"
        / "no-long-inline-process"
        / "no_long_inline_process.py"
    )
    spec = importlib.util.spec_from_file_location("no_long_inline_process", sib)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_long_process_detection_constants_match_sibling():
    """The long-process detection surface is duplicated from no-long-inline-process by design
    (each hook is a standalone script, no shared import path). If the canonical sibling adds a
    runner or tweaks a regex, this guard FAILS so the copy is kept in sync (review finding #1)."""
    sib = _load_sibling()
    assert hook._DIRECT_RUNNERS == sib._DIRECT_RUNNERS
    assert hook._SLEEP_UNIT_S == sib._SLEEP_UNIT_S
    assert hook._SLEEP_OPERAND.pattern == sib._SLEEP_OPERAND.pattern
    assert hook._WRAPPERS == sib._WRAPPERS
    assert hook._WRAPPER_VALUE_FLAGS == sib._WRAPPER_VALUE_FLAGS
    assert hook._SEGMENT_BREAKS == sib._SEGMENT_BREAKS
    assert hook._SHELL_SEP == sib._SHELL_SEP
    assert hook._SUBSTITUTION_BREAKS == sib._SUBSTITUTION_BREAKS
    assert hook._PUNCT_CHARS == sib._PUNCT_CHARS
    assert hook._LEADING_SHELL_NOISE == sib._LEADING_SHELL_NOISE
    assert hook._INLINE_ENV.pattern == sib._INLINE_ENV.pattern
    assert hook._MAX_WRAPPER_NESTING == sib._MAX_WRAPPER_NESTING
    assert hook._MAX_LEADING_NOISE == sib._MAX_LEADING_NOISE
    assert hook._COMMENT_BOUNDARY_METACHARS == sib._COMMENT_BOUNDARY_METACHARS
    # _SUITE_RUNNERS maps a runner → a compiled regex; compare keys + pattern strings.
    assert hook._SUITE_RUNNERS.keys() == sib._SUITE_RUNNERS.keys()
    for key, pat in hook._SUITE_RUNNERS.items():
        assert pat.pattern == sib._SUITE_RUNNERS[key].pattern, key


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl via the new `command=` contract. Regression for the documented inline form (Codex #232)."""
    marker = tmp_path / "asked"
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f'touch {marker}\ncat > "{question}"\n' + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(hook.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        'RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC="self-managed watchdog, polls inline" review diff -C /repo &',
        monkeypatch)  # env deliberately NOT set — only the inline prefix
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "self-managed watchdog, polls inline" in question.read_text()
