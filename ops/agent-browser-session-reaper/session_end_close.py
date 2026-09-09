#!/usr/bin/env python3
"""session_end_close.py — close the agent-browser sessions a finishing Claude Code
session claimed.

This is the precise half of the agent-browser cleanup pair. The other half is the
periodic sweep (`agent_browser_reaper.py`), which is age-based and owner-blind.

WIRING (why this is NOT an agents-hooks/v1 descriptor)
------------------------------------------------------
The v1 hook contract in `agent-hooks/` has no `session-end` point, and
`lib/cc_hook_bridge` only dispatches PreToolUse / PostToolUse / Stop. Claude Code's
own `SessionEnd` event is what fires here, so this script is wired directly into
`~/.claude/settings.json` under `hooks.SessionEnd` by `install.sh` in this directory.
It reads Claude Code's raw SessionEnd JSON on stdin, not a v1 event.

Claude Code's `Stop` point is deliberately NOT used: `Stop` fires at the end of every
turn, and an agent routinely opens a browser in one turn and reads it in the next.
Closing on `Stop` would tear down a session that is still being worked on.

HOW OWNERSHIP IS KNOWN
----------------------
`claim_agent_browser_session.py` (the `pre-bash` agent-hook shipped in
`agent-hooks/agent-browser-session-claim/`) records every `agent-browser` session name
this Claude Code session touches, as a marker file under

    ~/.cache/agent-tools/agent-browser-sessions/<claude-session-id>/<session-name>

That is the same marker pattern `skills-marker-writer` already uses. Without it there
is no attribution at all, and the only thing a session-end hook could do is
`agent-browser close --all` — which on this machine, where several agents run in
parallel tmux windows, would tear down every sibling agent's live browser on every
single session exit. Attribution is what makes this mechanism safe to run at all.

THE SHARED-NAME CASE
--------------------
A session name is a global handle, not a per-Claude-session one: two concurrent agents
that both use `--session default` (or both pick the same name) share ONE browser. So a
claimed name is closed only when no OTHER claim directory still claims it. A claim
directory belonging to a crashed Claude Code session lingers and keeps blocking that
one name; the periodic sweep prunes such directories after CLAIM_TTL_HOURS and will
close the session on age in the meantime. That is the intended division of labour.

SESSION-END REASONS
-------------------
Claude Code reports why the session ended (`clear`, `resume`, `logout`,
`prompt_input_exit`, `other`). By default this acts on `logout`, `prompt_input_exit`
and `other` — a real exit — and NOT on `clear` or `resume`, where the human is still
sitting at the same terminal and very likely still wants their browser. Override with
SESSION_END_REASONS in the config file.

NEVER FAILS LOUDLY: SessionEnd cannot block anything in Claude Code, and this script
exits 0 on every path. Its default timeout in Claude Code is 1.5s, so `install.sh`
wires it with an explicit `"timeout": 30` — closing a browser takes a second or two.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_browser_reaper import (  # noqa: E402  (path set up above, by design)
    CLAIM_DIR,
    Config,
    close_session,
    list_live_sessions,
    load_config,
)

DEFAULT_REASONS = ("logout", "prompt_input_exit", "other")

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _log(msg: str) -> None:
    print(f"[agent-browser-session-end] {msg}", file=sys.stderr, flush=True)


def _read_event() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}


def _allowed_reasons(cfg_path: Optional[Path] = None) -> frozenset[str]:
    """Which SessionEnd `reason` values this hook acts on."""
    from agent_browser_reaper import CONFIG_PATH, ENV_PREFIX, _read_config_file

    file_values = _read_config_file(cfg_path or CONFIG_PATH)
    raw = os.environ.get(ENV_PREFIX + "SESSION_END_REASONS") or file_values.get(
        "SESSION_END_REASONS")
    if not raw:
        return frozenset(DEFAULT_REASONS)
    return frozenset(r.strip().lower() for r in raw.split(",") if r.strip())


def read_claims(session_id: str, claim_dir: Path = CLAIM_DIR) -> set[str]:
    """Session names this Claude Code session claimed."""
    try:
        return {p.name for p in (claim_dir / session_id).iterdir() if p.is_file()}
    except OSError:
        return set()


def claims_held_by_others(session_id: str, claim_dir: Path = CLAIM_DIR) -> set[str]:
    """Session names claimed by any OTHER still-present claim directory."""
    held: set[str] = set()
    try:
        entries = list(claim_dir.iterdir())
    except OSError:
        return held
    for entry in entries:
        if entry.name == session_id or not entry.is_dir():
            continue
        try:
            held.update(p.name for p in entry.iterdir() if p.is_file())
        except OSError:
            continue
    return held


def _drop_claim_dir(session_id: str, claim_dir: Path = CLAIM_DIR) -> None:
    try:
        shutil.rmtree(claim_dir / session_id)
    except OSError:
        pass


def _closable(claimed: set[str], cfg: Config, session_id: str,
              claim_dir: Path) -> list[str]:
    """Claimed AND live AND not exempt AND not shared with another live claimant."""
    live = set(list_live_sessions())
    shared = claims_held_by_others(session_id, claim_dir)
    out = []
    for name in sorted(claimed):
        if name not in live:
            continue
        if name in cfg.keep:
            _log(f"keep: {name} (listed in KEEP)")
            continue
        if name in shared:
            _log(f"keep: {name} (also claimed by another session)")
            continue
        out.append(name)
    return out


def run(event: dict, claim_dir: Path = CLAIM_DIR) -> int:
    """Close what this session owns. Returns the number of sessions closed."""
    cfg = load_config()
    session_id = str(event.get("session_id") or "")
    if not _SAFE_SESSION_ID.match(session_id):
        return 0
    if not cfg.session_end_enabled:
        _log("disabled (SESSION_END_ENABLED=0)")
        _drop_claim_dir(session_id, claim_dir)
        return 0
    reason = str(event.get("reason") or "other").lower()
    if reason not in _allowed_reasons():
        _log(f"reason={reason} is not acted on — leaving sessions alone")
        return 0
    claimed = read_claims(session_id, claim_dir)
    if not claimed:
        _drop_claim_dir(session_id, claim_dir)
        return 0
    closed = 0
    for name in _closable(claimed, cfg, session_id, claim_dir):
        if close_session(name):
            _log(f"closed: {name}")
            closed += 1
        else:
            _log(f"FAILED to close: {name}")
    _drop_claim_dir(session_id, claim_dir)
    return closed


def main() -> int:
    try:
        run(_read_event())
    except Exception as exc:  # never break session teardown
        _log(f"error (ignored): {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
