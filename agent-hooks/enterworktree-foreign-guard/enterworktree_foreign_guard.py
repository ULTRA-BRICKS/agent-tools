#!/usr/bin/env python3
"""agents-hooks/v1 pre-worktree-enter hook — block entering a FOREIGN agent's worktree.

The wedge this closes: an agent (orchestrator or a dispatched subagent) needing to
resume/ship a PR or branch that a DIFFERENT agent built calls
``EnterWorktree(path=<that other agent's worktree>)``. The tool's own validation only
confirms the path is a real, registered worktree of the repo (``git worktree list``) — never
that the CALLER owns it — so the call reports SUCCESS. Every confirmed occurrence then
permanently bricked the calling agent's Bash tool for the rest of its session: every
subsequent command, even ``pwd``, was refused, with no recovery path (``ExitWorktree`` does
not help either). This has hit at least four times in one project's session history
(HYP-1384's retrospective), always in the same shape.

A prior fix documented the "never do this" rule in AGENTS.md/AGENTS-CORE.md and the
worktree-isolation skill (a text rule any future agent has to happen to read and remember).
Alex's explicit follow-up direction was to make the failure mode structurally impossible
instead: this hook is that mechanism — it intercepts the call BEFORE the tool ever runs and
refuses it outright when the target is a worktree a different agent owns, rather than letting
the tool "succeed" and brick the session afterward.

## How ownership is determined

CC names a dispatched subagent's isolated worktree ``.claude/worktrees/agent-<agent_id>`` —
that convention is CC's own, not something this hook invents (every observed worktree in this
project's ``.claude/worktrees/`` follows it). ``cc_hook_bridge`` forwards the CALLING agent's
own ``agent_id`` at the TOP LEVEL of the event ONLY when the call fires inside a dispatched
subagent (dispatch.py's T2 precedence: a value inside ``tool_input``/``args`` is
attacker/prompt-controllable and is dropped whenever the top-level field is absent) — so a
forged ``args.agent_id`` can never impersonate a different agent's ownership here either.

  - ``path`` embeds ``agent-<id>`` AND the caller's own ``agent_id`` == ``<id>`` → the caller
    is re-entering / switching into a worktree it owns → ALLOW.
  - ``path`` embeds ``agent-<id>`` AND the caller's own ``agent_id`` is absent (the
    orchestrator / top-level session — which never owns a dispatched subagent's worktree
    either) OR differs from ``<id>`` → BLOCK.
  - ``path`` does not match the ``agent-<id>`` convention at all (a manually created or
    differently-named worktree) → ALLOW. This hook only understands the ONE naming shape every
    confirmed incident actually used; guessing at an unfamiliar shape would risk false
    positives on a legitimate, differently-named worktree, so it fails open there rather than
    invent a heuristic beyond what's been observed.
  - ``args.path`` absent (creating a brand-new worktree via ``name``) → ALLOW, untouched — a
    freshly created worktree is always the calling agent's own.

Contract (agents-hooks/v1):
  stdin  : JSON event; ``args.path`` is the EnterWorktree target, the subagent signal is in
           ``args.agent_id`` (or the top-level event)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash here must never wedge an agent's ability to call a legitimate
tool — this is a structural safety net for the one confirmed foreign-worktree shape, not a
general-purpose sandbox.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path, PurePath

# SYNC: duplicated in every hatch-using hook so each hook does not need a shared helper file
# under agent-hooks/ (each hook script is a standalone subprocess with no shared import path).
# Edit every copy together; tests/test_hatch_import_hardening.py guards the shared behavior.
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
    # Leave the repo-local module installed on success so later imports in this hook process
    # cannot regain a preloaded user/site package or submodule.
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
HOOK_ID = "enterworktree-foreign-guard"

# CC's own convention for a dispatched subagent's isolated worktree directory name. Anchored
# to the whole path-segment (no partial match) so e.g. "agent-foo-extra" never partially
# matches; case-insensitive because a hex agent id could plausibly appear either case.
_WORKTREE_AGENT_SEGMENT_RE = re.compile(r"^agent-([0-9a-f]{6,64})$", re.IGNORECASE)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _extract_worktree_agent_id(path_str: str) -> str | None:
    """The agent id embedded in a `.claude/worktrees/agent-<id>` path segment, or None.

    Walks EVERY path segment (not just the last) so a path like
    `/repo/.claude/worktrees/agent-abc123/nested/dir` still resolves to `abc123` even when
    EnterWorktree's `path` points somewhere inside the worktree rather than at its root.
    Deliberately requires the LITERAL `.claude/worktrees/` parent segments — a directory that
    merely happens to be named `agent-<hex>` elsewhere on disk (outside that convention) is not
    treated as an owned/foreign worktree signal.
    """
    parts = PurePath(path_str).parts
    for i in range(len(parts) - 2):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            match = _WORKTREE_AGENT_SEGMENT_RE.match(parts[i + 2])
            if match:
                return match.group(1)
    return None


def _own_agent_id(event: dict) -> str | None:
    """The CALLING agent's own id, or None (the orchestrator / top-level session).

    Trustworthy ONLY because `cc_hook_bridge` takes `agent_id` from CC's own top-level event
    field and DROPS any copy forged in `tool_input`/`args` when that top-level field is absent
    (dispatch.py's T2 precedence) — a worker can't be spoofed in, and the orchestrator can't be
    spoofed OUT of this gate by a forged `args.agent_id`. SYNC: identical pattern to
    subagent-no-bg-longproc's / subagent-no-monitor's own helper (returns the id, not a bool).
    """
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    if aid and str(aid).strip():
        return str(aid).strip()
    return None


def _block_message(target_agent_id: str, path: str) -> str:
    return (
        f"BLOCKED: this EnterWorktree(path=...) targets .claude/worktrees/agent-{target_agent_id}, "
        "a worktree a DIFFERENT agent created (or the top-level orchestrator, which never owns a "
        "dispatched subagent's worktree either). EnterWorktree's own validation only confirms the "
        "path is a real worktree of this repo — never that YOU own it — and reaching into a foreign "
        "one has repeatedly reported SUCCESS and then permanently bricked the calling agent's Bash "
        "tool for the rest of its session, with no recovery path (not even ExitWorktree). "
        f"Target: {path}. "
        "Use the safe alternative instead: from YOUR OWN worktree, run "
        "`gh pr checkout <N> --branch <local-name>` (or `git checkout -b <local-name> <their-branch>`) "
        "to pull that agent's branch into your own isolated worktree. There is NO self-service "
        "bypass. For a genuine exception, ASK the human, or request a one-time Telegram approval by "
        f"setting {hatch_escalation.hatch_env_var(HOOK_ID)}=\"<written justification>\" "
        "(deny-by-default; a bare 1 is rejected)."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        # No `path` → either a brand-new worktree via `name` (always the caller's own) or a
        # malformed call the tool itself will reject. Out of scope for this guard either way.
        emit("allow")
        return 0
    path = path.strip()

    target_agent_id = _extract_worktree_agent_id(path)
    if target_agent_id is None:
        # Doesn't match the one naming convention every confirmed incident actually used —
        # fail open rather than guess at an unfamiliar worktree-naming scheme.
        emit("allow")
        return 0

    own_agent_id = _own_agent_id(event)
    if own_agent_id is not None and own_agent_id == target_agent_id:
        # Re-entering / switching into a worktree THIS agent owns — legitimate, unaffected.
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or args.get("cwd") or os.getcwd())
    ctx = {
        "hook": HOOK_ID,
        "path": path,
        "target_agent_id": target_agent_id,
        "own_agent_id": own_agent_id or "(none - orchestrator/top-level session)",
    }
    hatch = hatch_escalation.request_hatch_approval(HOOK_ID, ctx, cwd=cwd)
    if hatch.should_stop:
        if hatch.approved:
            warn(f"{HOOK_ID} allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        emit("block", f"hatch escalation denied: {hatch.reason}\n{_block_message(target_agent_id, path)}")
        return BLOCK_EXIT_CODE

    emit("block", _block_message(target_agent_id, path))
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
