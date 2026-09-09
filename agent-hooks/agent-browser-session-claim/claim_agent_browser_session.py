#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — record which agent-browser sessions THIS Claude Code
session opened.

WHY THIS EXISTS
---------------
`agent-browser` sessions are daemon + headless-Chrome trees (~14 processes, ~1.2 GB RSS
each, measured 2026-09-04). Agent runs that open a browser and never close it are what
exhausted this machine's memory twice on 2026-09-04. The fix has two halves; this hook
is the bookkeeping that makes the precise half possible.

The precise half is a Claude Code `SessionEnd` hook
(`ops/agent-browser-session-reaper/session_end_close.py`) that closes the browser
sessions a finishing agent session left behind. To do that safely it must know WHICH
sessions belong to the finishing agent. Nothing in agent-browser records that: a
session is a bare global name, and `agent-browser session list` returns names only.
Without attribution the only available action at session end is `agent-browser close
--all`, which on a machine running several agents in parallel tmux windows would tear
down every sibling agent's live browser on every session exit.

So this hook watches Bash commands for `agent-browser` invocations, extracts the
session name each one targets, and touches

    ~/.cache/agent-tools/agent-browser-sessions/<claude-session-id>/<session-name>

exactly the marker pattern `skills-marker-writer` already uses for skills-read-gate.

NEVER BLOCKS. This is a recording tap, not a gate: every path emits allow / exit 0, and
the descriptor is `on_error: open`. A missed marker costs one uncleaned browser session
(which the periodic sweep still catches on age); a blocked Bash command would cost the
agent its ability to work.

MATCHING IS DELIBERATELY LOOSE, AND THAT IS SAFE
------------------------------------------------
Over-claiming a name is harmless here: the session-end hook only closes a claimed name
that agent-browser independently reports as LIVE, that is not in the KEEP list, and that
no other claim directory also claims. A name claimed but never actually opened simply
never matches anything live. Under-claiming is the costly direction (a leaked browser),
so the parser errs toward claiming: any `agent-browser` token in the command line makes
the command a candidate, and an invocation with no explicit session flag claims
`default`, which is the name agent-browser itself uses in that case.

Contract (agents-hooks/v1):
  stdin  : JSON event; the Bash command is in args.command (or top-level command)
  stdout : protocol JSON only        exit 0 : allow (this hook never blocks)

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HOOK_API = "agents-hooks/v1"

CLAIM_DIR = Path(os.path.expanduser(os.environ.get(
    "AGENT_BROWSER_REAPER_CLAIM_DIR",
    "~/.cache/agent-tools/agent-browser-sessions")))

# The name agent-browser uses when no session is named.
IMPLICIT_SESSION = "default"

_MAX_SESSION_NAME_LEN = 200
_MAX_SESSION_ID_LEN = 128
_MAX_CLAIMS_PER_COMMAND = 16

# `agent-browser` as a whole word — a command mentioning `agent-browser-reaper` or a
# path ending in /agent-browser both count, but an unrelated word containing it as a
# substring (e.g. `my-agent-browserish`) does not.
_MENTIONS_AGENT_BROWSER = re.compile(r"(?:^|[\s/'\"=;&|(])agent-browser(?:[\s'\";&|)]|$)")

# --session NAME | --session=NAME | AGENT_BROWSER_SESSION=NAME
_EXPLICIT_SESSION = re.compile(
    r"--session[=\s]+['\"]?([A-Za-z0-9._-]{1,200})['\"]?"
    r"|AGENT_BROWSER_SESSION=['\"]?([A-Za-z0-9._-]{1,200})['\"]?")

# An invocation that only READS state never creates a session worth claiming.
_READ_ONLY_SUBCOMMAND = re.compile(
    r"agent-browser\s+(?:--\S+\s+)*"
    r"(?:session\s+(?:list|info|id)|skills|doctor|profiles|plugin|--help|--version)\b")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _allow() -> int:
    print(json.dumps({"hook_api": HOOK_API, "decision": "allow"}))
    return 0


def _command_from(event: dict) -> str:
    args = event.get("args")
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return args["command"]
    return event.get("command") if isinstance(event.get("command"), str) else ""


def _session_id_from(event: dict) -> str:
    """CC's own session id. The bridge gives it T2 precedence, so it is not spoofable.

    Rejected on any `/` or oversized value: it is used as ONE flat path component.
    """
    args = event.get("args")
    raw = args.get("session_id") if isinstance(args, dict) else None
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_SESSION_ID_LEN:
        return ""
    return raw if _SAFE_NAME.match(raw) else ""


def session_names(command: str) -> list[str]:
    """Every agent-browser session name this command line targets.

    Returns [] when the command does not invoke agent-browser at all, or only invokes
    a read-only subcommand that cannot create a session.
    """
    if not command or not _MENTIONS_AGENT_BROWSER.search(command):
        return []
    explicit = []
    for match in _EXPLICIT_SESSION.finditer(command):
        name = match.group(1) or match.group(2)
        if name and len(name) <= _MAX_SESSION_NAME_LEN and name not in explicit:
            explicit.append(name)
        if len(explicit) >= _MAX_CLAIMS_PER_COMMAND:
            break
    if explicit:
        return explicit
    if _READ_ONLY_SUBCOMMAND.search(command):
        return []
    return [IMPLICIT_SESSION]


def write_claims(session_id: str, names: list[str],
                 claim_dir: Path = CLAIM_DIR) -> int:
    """Touch one marker file per claimed name. Returns how many were written."""
    if not session_id or not names:
        return 0
    target = claim_dir / session_id
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    written = 0
    for name in names:
        if not _SAFE_NAME.match(name) or len(name) > _MAX_SESSION_NAME_LEN:
            continue
        try:
            (target / name).touch()
        except OSError:
            continue
        written += 1
    return written


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return _allow()
    if not isinstance(event, dict):
        return _allow()
    try:
        write_claims(_session_id_from(event), session_names(_command_from(event)))
    except Exception:  # a bookkeeping hiccup must never stop a Bash command
        pass
    return _allow()


if __name__ == "__main__":
    sys.exit(main())
