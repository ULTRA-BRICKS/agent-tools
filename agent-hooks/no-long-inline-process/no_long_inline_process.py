#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — no long-running process in the orchestrator.

Enforcement-doctrine gate 3 (ROADMAP "DOCTRINE: enforce correct agent behavior with
PreToolUse hooks"). A long-running command in the MAIN thread (orchestrator) holds it
hostage — the agent can't dispatch or collect anything else until it returns. The fix is
to run it in a BACKGROUND subagent. This gate detects the long-running shapes the CTO
named and BLOCKS them on the main thread: `review` (multi-model, minutes), anything
`--watch` (`gh pr checks --watch`, `tsc --watch`, dev servers), a build/test SUITE run,
and `sleep`.

This is narrower and stricter than gate 2 (orchestrator-stays-thin): gate 2 is about
*implementing* inline; this is specifically about a process that BLOCKS for a long time.
Because the cost is a wedged main thread (not just wrong altitude), the default tier here
is BLOCK, not warn — but the block is satisfiable (run it in a background subagent) and
carries an honest escape hatch.

SCOPED — subagents are EXEMPT (doctrine clause c): a subagent legitimately runs `review`
before it commits, watches its own CI, etc. The signal is CC's ``CLAUDE_CODE_CHILD_SESSION``.

SATISFIABLE (clause a): the honest action — dispatch the long command to a background
subagent (`run_in_background: true`) — always clears it. TIERED (clause b): a command that
already runs in the background (`... &`, `nohup`, `setsid`) or self-bounds with `timeout`
to a SHORT bound only WARNS. HONEST escape hatch (clause d): ``ALLOW_LONG_INLINE=1`` +
``ALLOW_LONG_INLINE_REASON='why'``, or an inline ``# long-inline-ok: why`` token.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only   exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a behavioral nudge must never wedge the orchestrator.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# `review` (the multi-model CLI — minutes), anything `--watch`, a `sleep`, and full build/
# test SUITE invocations are the long-running shapes the doctrine names.
_LONG_PATTERNS = (
    (re.compile(r"(?:^|[\s;&|])review\b(?!\s+(?:--help|-h|--version|install-skill)\b)"),
     "`review` is multi-model and runs for MINUTES"),
    (re.compile(r"--watch\b"),
     "a `--watch` process never returns"),
    # `-w` is the short watch alias for these tools (`tsc -w`, `vite -w`). Scope it to
    # watch-capable commands so a generic `-w` elsewhere isn't swept up (codex).
    (re.compile(r"\b(?:tsc|jest|vitest|webpack|vite|rollup|esbuild|next|nodemon|nuxt|astro)\b[^;&|]*\s-w\b"),
     "a `-w` watch process never returns"),
    (re.compile(r"\bgh\s+run\s+watch\b"),
     "`gh run watch` blocks until the run finishes"),
    (re.compile(r"\bgh\s+(?:pr\s+checks|run\s+view)\b.*\s--watch\b"),
     "a `gh … --watch` blocks until CI finishes"),
    (re.compile(r"(?:^|[\s;&|])sleep\s+\d"),
     "`sleep` blocks the main thread for no reason"),
    (re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|e2e|build|dev|start)\b"),
     "a build/test/dev suite run is long-running"),
    (re.compile(r"\b(?:pytest|jest|vitest|playwright|cypress|cargo\s+test|go\s+test|mvn\s+test|gradle\s+test)\b"),
     "a test suite run is long-running"),
)

# A trailing comment must be stripped before we judge backgrounding — otherwise a stray word
# in a `# …` comment (e.g. `# run_in_background`) would falsely look like a directive (codex P1).
_TRAILING_COMMENT = re.compile(r"(?<!\S)#.*$")
# `timeout N` with a small N self-bounds a watch/sleep so it can't wedge the session.
_SHORT_TIMEOUT = re.compile(r"\b(?:timeout|gtimeout)\s+(\d+)\b")


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"no-long-inline-process: {msg}\n")


# SYNC: keep _is_truthy / is_subagent identical across the five enforcement-doctrine hooks
# (see background-subagent-gate for why they can't share an import).
def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() not in ("", "0", "false", "no", "off")


def is_subagent() -> bool:
    return _is_truthy(os.environ.get("CLAUDE_CODE_CHILD_SESSION")) or _is_truthy(
        os.environ.get("CLAUDE_AGENT_IS_SUBAGENT")
    )


def _override_reason(command: str) -> str | None:
    if _is_truthy(os.environ.get("ALLOW_LONG_INLINE")):
        reason = (os.environ.get("ALLOW_LONG_INLINE_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = re.search(r"#\s*long-inline-ok:\s*(\S.*)", command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def long_running_reason(command: str) -> str | None:
    """The human reason this command is long-running, or None if it isn't one."""
    for pat, reason in _LONG_PATTERNS:
        if pat.search(command):
            return reason
    return None


def is_short_bounded(command: str) -> bool:
    """A `timeout N ...` with a small N self-bounds, so it can't wedge the session."""
    m = _SHORT_TIMEOUT.search(command)
    if not m:
        return False
    try:
        return int(m.group(1)) <= int(os.environ.get("LONG_INLINE_TIMEOUT_OK_S", "120"))
    except ValueError:
        return False


def is_backgrounded(command: str) -> bool:
    """True only when the command is ACTUALLY sent to the background with a real `&`.

    Strips a trailing `# comment` first (so a word inside a comment can't fake a directive —
    codex P1), then requires the command to END in a single ``&`` (job control), not the
    ``&&`` operator. ``nohup foo`` / ``setsid foo`` / a separate ``; disown`` do NOT return
    control to the shell, so they are NOT treated as backgrounded.
    """
    stripped = _TRAILING_COMMENT.sub("", command).rstrip()
    return bool(re.search(r"(?<![&\d>])&$", stripped))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # SCOPED: subagents legitimately run review / watch their own CI.
    if is_subagent():
        emit("allow")
        return 0

    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    reason = long_running_reason(command)
    if reason is None:
        emit("allow")
        return 0

    # already backgrounded with a real `&` → the right way; nothing to enforce.
    if is_backgrounded(command):
        emit("allow")
        return 0

    override = _override_reason(command)
    if override:
        warn(f"long inline command allowed via escape hatch ({override})")
        emit("allow", f"allowed via escape hatch ({override})")
        return 0

    msg = (
        f"Refusing a long-running command in the orchestrator: {reason}. Run it in a "
        "BACKGROUND subagent (Agent/Task with run_in_background: true) so the main thread "
        "stays free to dispatch and collect. Honest skip (auditable): set ALLOW_LONG_INLINE=1 "
        "and ALLOW_LONG_INLINE_REASON='why', or append `# long-inline-ok: why`."
    )

    # TIERED: a short, self-bounded `timeout N ...` only WARNS (it can't wedge for long).
    if is_short_bounded(command):
        warn(f"long-ish but short-bounded — {reason} (advisory)")
        emit("allow", msg)
        return 0

    emit("block", msg)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
