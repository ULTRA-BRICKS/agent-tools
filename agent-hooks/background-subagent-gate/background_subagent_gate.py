#!/usr/bin/env python3
"""agents-hooks/v1 pre-tool hook — orchestrator must dispatch subagents in the BACKGROUND.

Enforcement-doctrine gate 1 (ROADMAP "DOCTRINE: enforce correct agent behavior with
PreToolUse hooks"). The orchestrator (the MAIN thread) plans, dispatches, and verifies —
it must NOT foreground a subagent, because a foreground Agent/Task call blocks the main
thread until the subagent finishes, serializing what should be parallel and stalling the
session. The right way is `run_in_background: true` (or a dynamic Workflow).

Fires on the subagent-dispatch tool (CC `Task`, some builds `Agent`), which the bridge
maps to the `pre-tool` point. Detects: the dispatched call has `run_in_background` not
truthy → BLOCK with the 3-part reminder the CTO asked for.

SCOPED — subagents are EXEMPT (doctrine clause c): a subagent legitimately fans out its
own children synchronously, and CC nesting is one level anyway. The main-vs-subagent
signal is CC's `CLAUDE_CODE_CHILD_SESSION` env var (set in every child/subagent session,
absent in the main thread) — see `is_subagent`.

SATISFIABLE (clause a): the honest action that clears the gate is real and one keystroke —
set `run_in_background: true` on the dispatch. TIERED (clause b): a *trivial* dispatch
(a one-line scout prompt) only WARNS; a non-trivial one BLOCKS. HONEST escape hatch
(clause d): `ALLOW_FOREGROUND_SUBAGENT=1` + `ALLOW_FOREGROUND_SUBAGENT_REASON='why'`, or
an inline `foreground-subagent-ok: <reason>` token in the prompt — a real, auditable skip,
never a silent forge.

Contract (agents-hooks/v1):
  stdin  : JSON event; the dispatch payload is in args (the tool_input passthrough)
  stdout : protocol JSON only   exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash in a behavioral nudge must never wedge the orchestrator's
ability to dispatch work.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# A dispatch is "trivial" (warn-only, not block) when its prompt is short — a quick scout
# that returns fast, where foregrounding barely costs anything. Non-trivial work BLOCKS.
TRIVIAL_PROMPT_CHARS = int(os.environ.get("BG_SUBAGENT_TRIVIAL_CHARS", "240"))

INLINE_SENTINEL = re.compile(r"foreground-subagent-ok:\s*(\S.*)")

REMINDER = (
    "Refusing a FOREGROUND subagent dispatch. The orchestrator must launch subagents in "
    "the BACKGROUND:\n"
    "  1. WHY — a foreground Agent/Task call blocks the main thread until the subagent "
    "finishes, serializing work that should run in parallel and stalling the session.\n"
    "  2. THE RIGHT WAY — set `run_in_background: true` on this dispatch, or use a dynamic "
    "Workflow that fans the units out concurrently.\n"
    "  3. THE ROLE — the orchestrator plans, dispatches, and verifies; the subagent does "
    "the work. Keep the main thread free to dispatch the next unit and collect results.\n"
    "Honest skip (auditable, not a forge): set ALLOW_FOREGROUND_SUBAGENT=1 and "
    "ALLOW_FOREGROUND_SUBAGENT_REASON='why', or add `foreground-subagent-ok: why` to the "
    "prompt."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"background-subagent-gate: {msg}\n")


# SYNC: the subagent signal + truthy-env helpers are intentionally duplicated across the
# five enforcement-doctrine hooks (background-subagent-gate, orchestrator-stays-thin,
# no-long-inline-process, skills-read-gate, visual-proof-gate). Each hook is installed by
# rig as an INDEPENDENT directory copied to ~/.claude/hooks and run by absolute path, so a
# shared import would need its own install path and break the one-dir-per-hook model. Keep
# these three helpers identical in all five — change one, change all.
def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() not in ("", "0", "false", "no", "off")


def is_subagent() -> bool:
    """True when running inside a CC subagent/child session (gates 1-3 EXEMPT it).

    CC sets ``CLAUDE_CODE_CHILD_SESSION`` in every spawned child/subagent session and
    leaves it unset in the top-level (orchestrator) session — the reliable harness signal
    the doctrine asks for. ``CLAUDE_AGENT_IS_SUBAGENT`` is honored as an explicit override
    for harnesses/tests that set their own flag.
    """
    return _is_truthy(os.environ.get("CLAUDE_CODE_CHILD_SESSION")) or _is_truthy(
        os.environ.get("CLAUDE_AGENT_IS_SUBAGENT")
    )


def _override_reason(prompt: str) -> str | None:
    if _is_truthy(os.environ.get("ALLOW_FOREGROUND_SUBAGENT")):
        reason = (os.environ.get("ALLOW_FOREGROUND_SUBAGENT_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(prompt)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def runs_in_background(args: dict) -> bool:
    """True when the dispatch already requests background execution.

    Accepts the boolean ``run_in_background: true`` and its common string spellings, so a
    JSON ``true`` and a stringly-typed ``"true"`` both satisfy the gate.
    """
    val = args.get("run_in_background")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return _is_truthy(val)
    return bool(val)


def _prompt_text(args: dict) -> str:
    parts = [args.get("prompt"), args.get("description"), args.get("task")]
    return "\n".join(p for p in parts if isinstance(p, str))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # SCOPED: a subagent fanning out its own children is legitimate → never gate it.
    if is_subagent():
        emit("allow")
        return 0

    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    # already background → nothing to enforce.
    if runs_in_background(args):
        emit("allow")
        return 0

    prompt = _prompt_text(args)
    reason = _override_reason(prompt)
    if reason:
        warn(f"foreground subagent allowed via escape hatch ({reason})")
        emit("allow", f"foreground subagent allowed via escape hatch ({reason})")
        return 0

    # TIERED: a trivial (short-prompt) scout only WARNS so a quick lookup isn't wedged;
    # a non-trivial dispatch BLOCKS.
    if len(prompt.strip()) <= TRIVIAL_PROMPT_CHARS:
        warn("trivial foreground subagent — prefer run_in_background: true (advisory)")
        emit("allow", REMINDER)
        return 0

    emit("block", REMINDER)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
