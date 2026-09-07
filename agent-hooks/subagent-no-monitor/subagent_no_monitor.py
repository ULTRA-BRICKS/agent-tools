#!/usr/bin/env python3
"""agents-hooks/v1 pre-monitor hook — stop a SUBAGENT from calling Monitor at all.

Sibling wedge to ``subagent-no-bg-longproc`` (agent-tools#52), hit in a different shape in
HYP-1350's retrospective (hyperide/hyper-saas PR #754): a dispatched subagent, instead of
backgrounding a Bash command, called the **Monitor** tool on its own spawned child process
and then ENDED ITS TURN expecting a completion notification. Monitor is CC's dedicated
fire-and-forget watch tool — start it, keep working, get notified per output line/event later
— which is precisely the shape that never reaches a subagent: a subagent is NOT re-invoked by
a Monitor-event notification, only the main loop is (Monitor has no "child" the harness
tracks against the calling agent). This is DIFFERENT from a Bash `run_in_background: true`
child, which the harness DOES track against its calling agent and DOES use to re-invoke that
agent once the child exits (verified empirically: a subagent that backgrounds a Bash job this
way and ends its turn is resumed with the job's output once it completes) — that is exactly
why remedy (1) below recommends it as Monitor's replacement for this one shape. So a subagent
that reaches for Monitor instead idles forever with uncommitted work and no PR, identical to
the Bash-backgrounding wedge `subagent-no-bg-longproc` blocks for a LABELED long process, just
via a tool that has no foreground mode and no harness-tracked child to fall back to at all.

Unlike ``subagent-no-bg-longproc``, there is no "foreground vs backgrounded" axis to classify
here: Monitor *is* the background primitive, unconditionally, by construction — there is no
such thing as a Monitor call that blocks the caller's turn until it finishes. So the rule is
simpler and unconditional: **any** subagent call to Monitor is the wedge. The orchestrator's
own Monitor use (e.g. watching a backgrounded subagent's own progress) is unaffected — this
gate governs only tool calls made INSIDE a dispatched subagent (``agent_id`` present).

The correct replacement depends on what the subagent is waiting for:

1. Waiting on a SINGLE ORDINARY command it just started — one that is NOT itself a labeled long
   process (``review``, ``--watch``, a build-test suite, a long ``sleep``; see below) — set
   ``run_in_background: true`` on the Bash tool call. The harness auto-resumes the subagent when
   that command completes; no Monitor is needed for this shape at all.
2. Waiting on anything else — a LABELED long process, a condition to become true, a file to
   appear, several things at once — block on it SYNCHRONOUSLY in the foreground with a heartbeat
   loop: echo a line at
   least every ~20s and keep each Bash call comfortably under ~540s (well inside the Bash
   tool's own 600s hard cap), repeating the same bounded call until the wait is over, e.g.::

    timeout 540 bash -c 'i=0; until <condition-check> || [ "$i" -ge 26 ]; do sleep 20; i=$((i+1)); echo "[wait] tick $i ($((i*20))s)"; done'

Both shapes pass ``subagent-no-bg-longproc``'s own long-process/backgrounding rules (a
foreground heartbeat loop is not backgrounded; ``run_in_background: true`` is only blocked by
that sibling gate when the *backgrounded* command is itself a long-process label like
``review``/``--watch``/a build-test suite/a long ``sleep`` — an ordinary single command is
unaffected).

Contract (agents-hooks/v1):
  stdin  : JSON event; the subagent signal is in args.agent_id (or the top-level event)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": this is responsiveness/anti-wedge discipline, not a security boundary — a
crash here must never wedge a subagent's ability to call a tool.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    module = importlib.util.module_from_spec(spec)
    previous_modules = {
        name: sys.modules[name]
        for name in tuple(sys.modules)
        if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}.")
    }
    for name in previous_modules:
        if name != _HATCH_MODULE:
            sys.modules.pop(name, None)
    sys.modules[_HATCH_MODULE] = module
    # Leave the repo-local module installed on success so later imports in this
    # hook process cannot regain a preloaded user/site package or submodule.
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        for name in tuple(sys.modules):
            if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        # A helper that calls sys.exit() at import must not make the hook exit 0 (allow);
        # convert it to an import failure after cleanup. Ctrl-C still propagates.
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ImportError(f"cannot execute hatch escalation helper from {hatch_init}: {exc}") from exc
    return module


hatch_escalation = _load_hatch_escalation()

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"subagent-no-monitor: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present).

    The signal is authoritative ONLY because `cc_hook_bridge` takes agent_id from CC's
    top-level event and DROPS any copy forged in `tool_input` — a worker can't be spoofed in,
    and (the trust direction that matters here) the orchestrator can't be spoofed OUT of the
    gate by a forged `args.agent_id`. SYNC: identical to subagent-no-bg-longproc's helper.
    """
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


# The bounded foreground heartbeat loop both subagent anti-wedge gates point at as the universal
# alternative to backgrounding. SYNC: identical constant in subagent-no-bg-longproc
# (tests/test_subagent_no_bg_longproc.py pins the two equal), so the two hooks never drift on the
# one remedy they share.
HEARTBEAT_LOOP_EXAMPLE = (
    "`timeout 540 bash -c 'i=0; until <condition-check> || [ \"$i\" -ge 26 ]; do sleep "
    "20; i=$((i+1)); echo \"[wait] tick $i ($((i*20))s)\"; done'`"
)

BLOCK_MESSAGE = (
    "You are a SUBAGENT — do not call Monitor. Monitor is a fire-and-forget background "
    "watch: you start it, then get notified per event LATER. A subagent is NOT re-invoked by "
    "that notification (only the main loop is), so calling Monitor and ending your turn "
    "wedges you FOREVER with uncommitted work and no PR — the identical failure "
    "subagent-no-bg-longproc blocks for a backgrounded Bash command, just via a tool with no "
    "foreground mode to fall back to. Use one of these instead: "
    "(1) waiting on a SINGLE ORDINARY command you just started (NOT review/--watch/a "
    "build-test suite/a long sleep — those are blocked from backgrounding by "
    "subagent-no-bg-longproc, run those in the FOREGROUND instead) — set "
    "`run_in_background: true` on the Bash tool call; the harness auto-resumes you when that "
    "command completes, no Monitor needed. "
    "(2) waiting on anything else (a labeled long process, a condition, a file, several things "
    "at once) — block on it "
    "yourself in the FOREGROUND with a heartbeat loop: echo a line at least every ~20s and "
    "keep each Bash call under ~540s, repeating the same bounded call until the wait is over, "
    "e.g.: " + HEARTBEAT_LOOP_EXAMPLE + ". There is NO self-service "
    "bypass. For a genuine exception, ASK the human, or request a one-time Telegram approval "
    "by setting RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR=\"<written justification>\" "
    "(deny-by-default; a bare 1 is rejected)."
)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # This gate governs WORKERS only. The orchestrator's own Monitor use (watching a
    # backgrounded subagent) is legitimate and unaffected — a non-subagent (no agent_id) is
    # always allowed.
    if not _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    cwd = str(event.get("cwd") or args.get("cwd") or os.getcwd())

    # SECURITY: the watched target is recorded into the audit trail via ctx["command"], NEVER
    # via request_hatch_approval's own `command=` kwarg. That kwarg is also this library's INLINE
    # hatch source (`RIG_HATCH_REQUEST_<ID>=<value> <rest>` parsed out of a real shell command
    # string) — and `description` is a free-form, fully MODEL-authored Monitor tool_input field.
    # Passing it as `command=` would let a blocked subagent re-arm the hatch by writing
    # `description: 'RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR="..." watch tests'` on its retry,
    # firing a real Telegram ask with a self-authored justification — exactly the self-service
    # bypass this hook's own README says is impossible for Monitor (it has no shell command
    # string). `_append_overrides_log` falls back to `context.get("command")` for the audit line
    # (see `lib/agenttools_hatch_escalation/__init__.py`), so ctx alone is enough to fix the
    # empty-"command"-forever audit gap without reopening the inline-parse path.
    watched = str(args.get("description") or "monitor-watch")
    ctx = {"hook": "subagent-no-monitor", "description": watched, "command": watched}
    hatch = hatch_escalation.request_hatch_approval(
        "subagent-no-monitor", ctx, cwd=cwd
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"subagent-no-monitor allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        emit("block", f"hatch escalation denied: {hatch.reason}\n{BLOCK_MESSAGE}")
        return BLOCK_EXIT_CODE

    emit("block", BLOCK_MESSAGE)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
