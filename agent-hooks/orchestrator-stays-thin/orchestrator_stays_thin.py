#!/usr/bin/env python3
"""agents-hooks/v1 pre-write + pre-bash hook — the orchestrator stays thin.

Enforcement-doctrine gate 2 (ROADMAP "DOCTRINE: enforce correct agent behavior with
PreToolUse hooks"). The main thread (orchestrator) plans, dispatches, and verifies — it
does NOT implement inline. When it tries to do non-trivial work itself — a code Edit/Write
to a non-docs file, or a multi-step/long Bash command — this gate WARNS, then BLOCKS, with
"delegate to a subagent" (the `delegate-work-to-subagents` skill, enforced).

ONE script, TWO points (a descriptor per point points here):
  - pre-write : a code Edit/Write (not docs/config/test scaffolding) by the main thread.
  - pre-bash  : a multi-step or long-running Bash command by the main thread.

SCOPED — subagents are EXEMPT (doctrine clause c): a subagent legitimately does the work.
The signal is CC's ``CLAUDE_CODE_CHILD_SESSION`` (set in child/subagent sessions). On the
main thread the gate is active; inside a subagent it is a clean no-op.

TIERED (clause b) — the doctrine asks for "warn THEN block". This gate is a true two-stage
escalation keyed per target: the FIRST non-trivial attempt at a given file/command shape
WARNS (and is allowed through), a REPEAT within the warn window BLOCKS. A brittle rule that
hard-blocks the first keystroke wedges the loop; a warn-first rule gives the orchestrator a
chance to self-correct before the wall. SATISFIABLE (clause a): the honest action — hand
the edit to a subagent — always clears it. HONEST escape hatch (clause d):
``ALLOW_ORCHESTRATOR_WORK=1`` + ``ALLOW_ORCHESTRATOR_WORK_REASON='why'``, or an inline
``# orchestrator-work-ok: why`` token in a bash command — a real, auditable skip.

Contract (agents-hooks/v1):
  stdin  : JSON event; point in event.point, payload in args
  stdout : protocol JSON only   exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a behavioral nudge must never break the session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# A per-target warn marker lives here; a repeat within the window escalates warn → block.
MARKER_DIR = Path(
    os.path.expanduser(os.environ.get("ORCH_THIN_MARKER_DIR", "~/.cache/agent-tools/orch-thin"))
)
WARN_WINDOW_S = int(os.environ.get("ORCH_THIN_WARN_WINDOW_S", "900"))

# Docs / config / fixtures the orchestrator may legitimately touch inline (a ROADMAP note,
# a small config tweak) — editing these is NOT "implementing a feature", so never gate them.
_DOC_LIKE = re.compile(
    r"\.(md|mdx|markdown|rst|txt|json|ya?ml|toml|ini|cfg|env|lock|csv|svg)$"
    r"|(^|/)(readme|license|changelog|roadmap|agents|claude|\.gitignore)\b",
    re.IGNORECASE,
)
# Source extensions where an inline edit IS implementing — the thing to delegate.
_CODE_LIKE = re.compile(
    r"\.(py|ts|tsx|js|jsx|mjs|cjs|go|rs|rb|java|kt|c|h|cc|cpp|hpp|cs|php|swift|scala|sh|bash|zsh)$",
    re.IGNORECASE,
)

# A multi-step / long bash command: a chain (&&, ||, ;, |), a heredoc, a loop, or a build/
# test/install runner (the kind of work a subagent should own).
_MULTISTEP_BASH = re.compile(r"&&|\|\||;|\||<<|\bfor\b|\bwhile\b|\bmake\b")
_HEAVY_BASH = re.compile(
    r"\b(npm|pnpm|yarn|bun|pip|uv|cargo|go|mvn|gradle|pytest|jest|vitest|playwright|docker|"
    r"webpack|vite|tsc|rollup|esbuild)\b"
)
# Read-only / inspection bash the orchestrator legitimately runs to VERIFY — never gate it.
# Matched PER SEGMENT (see is_heavy_bash): a chain is exempt only if EVERY segment is
# read-only, so `git status; rm -rf x` does NOT slip past by starting with an inspection
# command (codex P2).
_INSPECTION_SEGMENT = re.compile(
    r"^\s*(git\s+(status|log|diff|show|branch|remote|rev-parse|worktree|config\s+--get)\b"
    r"|gh\s+(pr|run|issue|ship|api)\b"
    r"|ls|cat|head|tail|grep|rg|find|wc|stat|echo|pwd|env|printenv|which|jq|sort|uniq|awk|sed|true|column|tr|cut)\b"
)

REMINDER_WRITE = (
    "The orchestrator is editing source code inline. Delegate to a subagent: the main "
    "thread plans, dispatches, and verifies — it doesn't implement. Hand this code change "
    "to a subagent (Agent/Task with run_in_background: true), then verify its result. "
    "Honest skip (auditable): set ALLOW_ORCHESTRATOR_WORK=1 and "
    "ALLOW_ORCHESTRATOR_WORK_REASON='why'."
)
REMINDER_BASH = (
    "The orchestrator is running a multi-step / heavy Bash command inline. Delegate the "
    "implementation work to a subagent — the main thread plans, dispatches, and verifies. "
    "(Read-only inspection like git status / gh pr view is fine; building/testing/"
    "installing is a subagent's job.) Honest skip (auditable): set ALLOW_ORCHESTRATOR_WORK=1 "
    "and ALLOW_ORCHESTRATOR_WORK_REASON='why', or append `# orchestrator-work-ok: why`."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"orchestrator-stays-thin: {msg}\n")


# SYNC: keep _is_truthy / is_subagent identical across the five enforcement-doctrine hooks
# (see background-subagent-gate for the rationale: each hook is an independent install dir
# run by absolute path, so they can't share an import).
def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() not in ("", "0", "false", "no", "off")


def is_subagent() -> bool:
    return _is_truthy(os.environ.get("CLAUDE_CODE_CHILD_SESSION")) or _is_truthy(
        os.environ.get("CLAUDE_AGENT_IS_SUBAGENT")
    )


def _override_reason(command: str) -> str | None:
    if _is_truthy(os.environ.get("ALLOW_ORCHESTRATOR_WORK")):
        reason = (os.environ.get("ALLOW_ORCHESTRATOR_WORK_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = re.search(r"#\s*orchestrator-work-ok:\s*(\S.*)", command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def is_code_write(file_path: str) -> bool:
    """A non-docs source edit is "implementing"; docs/config/fixtures are not."""
    if not file_path:
        return False
    if _DOC_LIKE.search(file_path):
        return False
    return bool(_CODE_LIKE.search(file_path))


def _all_segments_read_only(command: str) -> bool:
    """True only when EVERY segment of a pipeline/chain is a read-only inspection command.

    Splits on the shell operators that separate commands (``;`` ``&&`` ``||`` ``|`` ``&``)
    and requires each non-empty segment to match an inspection command. A heredoc/loop
    keyword anywhere disqualifies it (those are not simple inspection). This is what stops
    ``git status; rm file`` or ``git status | python evil.py`` from being exempted because
    they merely START with an inspection command (codex P2).
    """
    if re.search(r"<<|\bfor\b|\bwhile\b|\bmake\b|\$\(|`", command):
        return False
    segments = re.split(r"&&|\|\||[;|&]", command)
    seen = False
    for seg in segments:
        if not seg.strip():
            continue
        seen = True
        if not _INSPECTION_SEGMENT.search(seg):
            return False
    return seen


def is_heavy_bash(command: str) -> bool:
    """A multi-step or heavy build/test/install command — NOT read-only inspection."""
    if not command.strip():
        return False
    # an all-read-only pipeline/chain is the orchestrator's legitimate verify job → exempt.
    if _all_segments_read_only(command) and not _HEAVY_BASH.search(command):
        return False
    return bool(_MULTISTEP_BASH.search(command) or _HEAVY_BASH.search(command))


def _marker(target: str) -> Path:
    sid = hashlib.sha256(target.encode()).hexdigest()[:16]
    return MARKER_DIR / f"{sid}.warn"


def _warned_recently(target: str) -> bool:
    p = _marker(target)
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) <= WARN_WINDOW_S
    except OSError:
        return False


def _record_warn(target: str) -> None:
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        _marker(target).write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        warn(f"could not write warn marker: {exc}")


def _tiered(target: str, reminder: str) -> int:
    """warn THEN block: first attempt at this target warns; a repeat blocks."""
    if _warned_recently(target):
        emit("block", reminder)
        return BLOCK_EXIT_CODE
    _record_warn(target)
    warn("orchestrator doing inline work — warning first; a repeat will block")
    emit("allow", reminder)
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # SCOPED: subagents do the work — never gate them.
    if is_subagent():
        emit("allow")
        return 0

    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    point = event.get("point") or ""
    # Key the warn marker by CWD + SESSION too — otherwise a warn for `src/app.py` (or
    # `npm test`) in one repo/session would make the FIRST attempt in another repo block
    # within the window (codex). ctx isolates the warn state per repo+session.
    ctx = f"{event.get('cwd') or args.get('cwd') or ''}|{args.get('session_id') or event.get('event_id') or ''}"

    if point == "pre-write":
        file_path = args.get("file_path") or args.get("path") or ""
        if not isinstance(file_path, str):
            file_path = str(file_path)
        if not is_code_write(file_path):
            emit("allow")
            return 0
        # an escape hatch via env works for writes too (no command string to annotate).
        if _is_truthy(os.environ.get("ALLOW_ORCHESTRATOR_WORK")) and (
            os.environ.get("ALLOW_ORCHESTRATOR_WORK_REASON") or ""
        ).strip():
            warn("orchestrator code-write allowed via env escape hatch")
            emit("allow")
            return 0
        return _tiered(f"{ctx}|write:{file_path}", REMINDER_WRITE)

    if point == "pre-bash":
        command = args.get("command") or args.get("cmd") or event.get("command") or ""
        if not isinstance(command, str):
            command = str(command)
        if not is_heavy_bash(command):
            emit("allow")
            return 0
        reason = _override_reason(command)
        if reason:
            warn(f"orchestrator heavy bash allowed via escape hatch ({reason})")
            emit("allow", f"allowed via escape hatch ({reason})")
            return 0
        return _tiered(f"{ctx}|bash:{command.strip()[:120]}", REMINDER_BASH)

    # any other point → not ours.
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
