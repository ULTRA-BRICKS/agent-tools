#!/usr/bin/env python3
"""agents-hooks/v1 pre-skill + pre-bash + pre-write hook — mandatory skills read first.

Enforcement-doctrine gate 4 (ROADMAP "DOCTRINE" + the "MISSING gate" item): nothing today
verifies an agent actually INVOKED the mandatory (and task-relevant) skills before it acts.
Skill-loading is purely advisory — the frontmatter triggers SUGGEST, but nothing ENFORCES.
This gate makes "read your skills first" real.

How it works — ONE script, THREE points (a descriptor per point points here):
  - pre-skill : the Skill tool is firing → RECORD this skill as invoked this session
    (writes a per-session marker). This is the satisfiable, honest action: invoking the
    skill (the real thing) is what clears the gate. No marker is forged — only an actual
    Skill-tool call writes it.
  - pre-bash  : a `git commit` → the first substantive act → CHECK the mandatory skills
    were invoked this session; warn (first), then block.
  - pre-write : a code Edit/Write (non-docs) → also a substantive act → same CHECK.

MANDATORY-ALWAYS skills (doctrine design-question, settled): the universal-mandatory tier
the doctrine names — ``delegate-work-to-subagents`` and ``visual-proof-cycle`` — plus any
listed in ``SKILLS_READ_MANDATORY`` (comma-separated, overrides the default). "Read" =
a Skill-tool invocation THIS SESSION (keyed by the CC session id). Relevance-triggered
skills are out of scope for the hard gate (relevance detection is heuristic and would make
the gate brittle); the gate enforces only the always-mandatory set, the safe subset the
doctrine prioritizes.

TIERED (clause b): a missing mandatory skill WARNS on the first substantive act, then
BLOCKS the next within the warn window — never a first-keystroke wall. SATISFIABLE
(clause a): invoke the named skill (Skill tool) and the marker is written for real.
SCOPED: this gate applies to BOTH the orchestrator AND subagents (a subagent must also
read its skills) — it is NOT one of the subagent-exempt gates 1-3. HONEST escape hatch
(clause d): ``SKIP_SKILLS_READ_GATE=1`` + ``SKIP_SKILLS_READ_GATE_REASON='why'``, or an
inline ``# skills-read-ok: why`` in a commit command — a real, auditable skip.

Contract (agents-hooks/v1):
  stdin  : JSON event; point in event.point, Skill name in args.skill, session in event_id
  stdout : protocol JSON only   exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a discipline reminder must never wedge the session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

MARKER_DIR = Path(
    os.path.expanduser(os.environ.get("SKILLS_READ_MARKER_DIR", "~/.cache/agent-tools/skills-read"))
)
# How long a skill invocation "counts as read" — a session's worth.
READ_TTL_S = int(os.environ.get("SKILLS_READ_TTL_S", "7200"))
# How long after a first warn before the gate escalates to block.
WARN_WINDOW_S = int(os.environ.get("SKILLS_READ_WARN_WINDOW_S", "1800"))

# Only the truly universal-ALWAYS skill is a hard default here. `visual-proof-cycle` is
# relevance-triggered (it applies to user-VISIBLE work) — enforcing it for every backend
# commit would over-block (codex), and it is already enforced where it belongs by the
# dedicated visual-proof-gate. Add relevance-triggered skills via SKILLS_READ_MANDATORY.
_DEFAULT_MANDATORY = ("delegate-work-to-subagents",)

_CODE_LIKE = re.compile(
    r"\.(py|ts|tsx|js|jsx|mjs|cjs|go|rs|rb|java|kt|c|h|cc|cpp|hpp|cs|php|swift|scala|sh|bash|zsh)$",
    re.IGNORECASE,
)


# SYNC: is_git_commit is duplicated in visual-proof-gate (and mirrors the argv-aware
# parsing already in require-ticket-before-commit). Keep all three in step — a naive
# `\bgit commit\b` regex misses `git -C /repo commit` and `git -c k=v commit`, the exact
# bypass codex flagged. Each hook is an independent install dir (see background-subagent-gate)
# so they can't share an import.
# Shell control operators that SEPARATE commands. Using shlex with punctuation_chars makes
# these standalone tokens even when not space-padded (`git status;git commit` → `… ; …`),
# which a plain shlex.split does NOT do — that gap was the original bypass.
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "|&", "&", "\n"})


def _shell_segments(command: str) -> list[list[str]]:
    """Split a command line into per-command token lists, honoring real shell separators.

    ``shlex(..., punctuation_chars=True)`` tokenizes ``;`` ``|`` ``&&`` ``||`` ``&`` as their
    own tokens regardless of spacing, so ``git status;git commit`` and ``true|git commit``
    segment correctly. On a tokenization error the caller falls back to a substring check.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    tokens = list(lex)
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    segments.append(cur)
    return segments


def _is_git(tok: str) -> bool:
    return tok in ("git", "/usr/bin/git") or tok.endswith("/git")


def _commit_flags_without_message_values(post_commit: list[str]) -> list[str]:
    """The tokens AFTER `commit`, with `-m/-F/--message/--file` VALUES removed — so a message
    that happens to be `--skip`/`--abort` (``git commit -m --skip``) can't be mistaken for a
    rebase-continuation flag (codex). Mirrors require-ticket-before-commit's argv handling."""
    out: list[str] = []
    i = 0
    while i < len(post_commit):
        tok = post_commit[i]
        if tok in ("-m", "--message", "-F", "--file") and i + 1 < len(post_commit):
            i += 2  # drop the flag AND its value
            continue
        if tok.startswith(("--message=", "--file=")) or (tok.startswith("-m") and len(tok) > 2):
            i += 1  # drop -mMSG / --message=MSG / --file=PATH
            continue
        out.append(tok)
        i += 1
    return out


def _segment_is_git_commit(tokens: list[str]) -> bool:
    """True when a single command segment's executable is git and its subcommand is commit.

    The git executable must be the SEGMENT'S FIRST token — so `echo git commit` (echo is the
    command) is NOT a commit, while `git -C /r commit` is. Walks past git GLOBAL options
    (`-C dir`, `-c k=v`, …) to the subcommand. A rebase/merge step
    (`commit --continue/--abort/--skip`) is not an authoring commit — checked AFTER stripping
    `-m`/`-F` values so a literal `--skip` message doesn't falsely exempt a real commit.
    """
    if not tokens or not _is_git(tokens[0]):
        return False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens) or tokens[i] != "commit":
        return False
    rest = _commit_flags_without_message_values(tokens[i + 1:])
    return not any(r in ("--continue", "--abort", "--skip") for r in rest)


def is_git_commit(command: str) -> bool:
    """True when any segment of the command is a `git commit` (incl. `git -C <dir> commit`
    / `git -c k=v commit`). Real shell separators (`;` `|` `&&` `||` `&`) split segments so
    `git status; git commit` and `true|git commit` are detected, while `echo git commit` is
    not. A naive `\\bgit commit\\b` regex misses the global-option forms (codex bypass)."""
    try:
        segments = _shell_segments(command)
    except ValueError:
        # un-tokenizable → fall back to the literal substring (the safe direction: still gate).
        return "git" in command and "commit" in command
    return any(_segment_is_git_commit(seg) for seg in segments)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"skills-read-gate: {msg}\n")


# SYNC: keep _is_truthy identical across the five enforcement-doctrine hooks (see
# background-subagent-gate for why they can't share an import). is_subagent() is NOT used
# here — gate 4 deliberately applies to subagents too.
def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() not in ("", "0", "false", "no", "off")


def mandatory_skills() -> list[str]:
    override = os.environ.get("SKILLS_READ_MANDATORY")
    if override is not None:
        return [s.strip() for s in override.split(",") if s.strip()]
    return list(_DEFAULT_MANDATORY)


def session_id(event: dict) -> str:
    args = event.get("args") or {}
    raw = (
        (args.get("session_id") if isinstance(args, dict) else None)
        or event.get("event_id")
        or "default"
    )
    return hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def _skill_marker(sid: str, skill: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", skill)
    return MARKER_DIR / f"{sid}.skill.{safe}"


def _warn_marker(sid: str) -> Path:
    return MARKER_DIR / f"{sid}.warned"


def _fresh(p: Path, ttl: int) -> bool:
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) <= ttl
    except OSError:
        return False


def _skill_name(args: dict) -> str:
    for key in ("skill", "name", "command"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            # a `plugin:skill` form → take the skill part for matching.
            return val.strip().split(":")[-1].lstrip("/")
    return ""


def record_skill(sid: str, skill: str) -> None:
    if not skill:
        return
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        _skill_marker(sid, skill).write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        warn(f"could not record skill {skill!r}: {exc}")


def missing_mandatory(sid: str) -> list[str]:
    return [s for s in mandatory_skills() if not _fresh(_skill_marker(sid, s), READ_TTL_S)]


def _escape_reason(command: str) -> str | None:
    if _is_truthy(os.environ.get("SKIP_SKILLS_READ_GATE")):
        reason = (os.environ.get("SKIP_SKILLS_READ_GATE_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = re.search(r"#\s*skills-read-ok:\s*(\S.*)", command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def _is_substantive(point: str, args: dict) -> bool:
    """A commit, or a non-docs code write — the acts that should be skill-informed."""
    if point == "pre-bash":
        command = args.get("command") or args.get("cmd") or ""
        return bool(isinstance(command, str) and is_git_commit(command))
    if point == "pre-write":
        fp = args.get("file_path") or args.get("path") or ""
        return bool(isinstance(fp, str) and _CODE_LIKE.search(fp))
    return False


def _check(sid: str, command: str) -> int:
    missing = missing_mandatory(sid)
    if not missing:
        emit("allow")
        return 0

    reason = _escape_reason(command)
    if reason:
        warn(f"skills-read gate skipped via escape hatch ({reason})")
        emit("allow", f"skipped via escape hatch ({reason})")
        return 0

    msg = (
        "Mandatory skills were not read this session before substantive work: "
        + ", ".join(missing)
        + ". Invoke each with the Skill tool (that's what clears this gate — no marker is "
        "forged, only a real invocation counts), then proceed. Honest skip (auditable): set "
        "SKIP_SKILLS_READ_GATE=1 and SKIP_SKILLS_READ_GATE_REASON='why', or append "
        "`# skills-read-ok: why` to the commit."
    )

    # TIERED: warn on the first substantive act, block the next within the window.
    wm = _warn_marker(sid)
    if _fresh(wm, WARN_WINDOW_S):
        emit("block", msg)
        return BLOCK_EXIT_CODE
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        wm.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        warn(f"could not write warn marker: {exc}")
    warn("mandatory skills unread — warning first; a repeat will block")
    emit("allow", msg)
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    point = event.get("point") or ""
    sid = session_id(event)

    # RECORD: a Skill invocation is the honest action that satisfies the gate.
    if point == "pre-skill":
        record_skill(sid, _skill_name(args))
        emit("allow")
        return 0

    # CHECK: only on a substantive act (commit / code write).
    if not _is_substantive(point, args):
        emit("allow")
        return 0

    command = args.get("command") or args.get("cmd") or ""
    if not isinstance(command, str):
        command = str(command)
    return _check(sid, command)


if __name__ == "__main__":
    sys.exit(main())
