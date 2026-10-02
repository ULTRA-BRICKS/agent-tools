#!/usr/bin/env python3
"""agents-hooks/v1 pre-worktree-enter hook — GRANT (pre-approve) an EnterWorktree(path=...)
call whose target resolves to the repo's rig-sanctioned direct-child ``<repo-root>/.worktrees/<name>``,
suppressing Claude Code's own interactive permission prompt for that one canonical case.

Incident this closes (rig-cli#418, Alex, 2026-09-07/08): PR#298 registered an ``EnterWorktree``
PreToolUse HOOK matcher into the CC hook bridge so agent-hooks like
``enterworktree-foreign-guard`` can fire on the call — but that only wires the HOOK layer. CC
has a SEPARATE, EARLIER permission classifier that decides whether to interactively prompt for
approval BEFORE a tool call even reaches PreToolUse hooks, and nothing in rig's provisioning
ever granted ``EnterWorktree`` there. Confirmed live: an ``EnterWorktree`` call into a worktree
at the CANONICAL ``<repo>/.worktrees/<name>`` location — the ONLY sanctioned worktree location
per rig-cli's own AGENTS.md "Worktree lifecycle" section — still triggered a full interactive
"relocation outside .claude/worktrees/" approval prompt, blocking an autonomous session with
nobody around to answer it.

Why this is NOT a blanket ``EnterWorktree`` permission grant (an earlier draft of this fix,
corrected by Alex before it landed): ``EnterWorktree`` can enter ANY worktree registered in
``git worktree list`` — not only the canonical location — so broadly pre-approving the whole
tool would let a caller route around the exact same location-bypass concern
``block-raw-worktree-add`` (agent-tools#608) closes for the Bash/``git worktree add`` path. This
hook is PATH-SCOPED instead: it inspects the actual resolved target and only grants when that
target is a direct child at ``<repo-root>/.worktrees/<name>`` — the same canonical-path policy, reused
(not reimplemented) from ``block-raw-worktree-add/block_raw_worktree_add.py`` (SYNC, see below).
Anything else gets NO opinion from this hook (plain ``allow``, not a grant) and CC's own
interactive prompt still fires exactly as it does today — this hook only ever REMOVES friction
for the one sanctioned case; it never adds a new restriction. A deny/hatch-gate for a
non-canonical ``EnterWorktree`` target is a natural follow-up (mirroring
``block-raw-worktree-add``'s hatch-gated exception path for the raw-git case) but is a separate,
bigger decision than "stop prompting for the case that is already sanctioned" — deliberately not
built here; tracked as a follow-up, not silently absent.

This hook is entirely separate from, and does not overlap with, ``enterworktree-foreign-guard``
(also mapped to ``pre-worktree-enter``): that hook BLOCKS entering a worktree a DIFFERENT
dispatched agent created (a ``.claude/worktrees/agent-<id>`` ownership check); this hook only
ever GRANTS, never blocks, and only for the rig-sanctioned ``.worktrees/`` location, which is
outside that other hook's ``.claude/worktrees/`` naming convention entirely. The two can never
disagree in a way that matters: ``cc_hook_bridge``'s aggregation (see ``dispatch.py``) lets any
BLOCK from either hook win outright, regardless of priority order or whether this hook already
granted.

Scope, deliberately narrow:
  - ``args.path`` present (entering an EXISTING worktree) and it resolves to the direct-child
    ``<repo-root>/.worktrees/<name>`` shape → GRANT.
  - ``args.path`` present but resolves anywhere else, including nested paths like
    ``<repo-root>/.worktrees/sub/feat`` → plain ``allow`` (no opinion; CC's normal
    prompt still fires, unchanged from today).
  - ``args.path`` ABSENT (creating a brand-new worktree via ``args.name``) → plain ``allow``.
    CC hardcodes ``name``-mode to land inside ``.claude/worktrees/`` (its own already-trusted
    convention) — there is no friction to remove there, and this hook makes no claim about it.
  - Repo root undetermined, malformed event, or any other failure → plain ``allow`` (fail open
    on THIS hook's OPINION only — never escalates to a block; CC's own classifier decides
    exactly as if this hook were not installed at all).

Detection reuses (SYNC, copied — NOT imported; every agent-hook in this family is a standalone
script by convention) ``repo_root`` / ``_worktrees_dir_escapes_repo`` / `_target_is_sanctioned``
from ``agent-hooks/block-raw-worktree-add/block_raw_worktree_add.py`` (agent-tools#608, commit
f827442) — the same primitives that already decide "is this path the one sanctioned worktree
location" for the raw ``git worktree add``/``remove``/``move`` case, so the two never drift into
disagreeing about what counts as canonical. TRIMMED relative to the source: that file's
``repo_root``/``_git``/``_git_path`` accept an optional ``git_dir`` override captured off a
parsed ``git -C ... --git-dir ...`` Bash command — there is no such thing to parse here
(``EnterWorktree``'s JSON ``args`` carry a bare ``path``/``name``, never a shell command with
global git options), so that parameter is dropped rather than carried as permanently-unused
dead weight. If a future EnterWorktree event shape ever adds a repo-selection override, port it
back in at that point rather than guessing at it now.

No hatch escalation: unlike ``block-raw-worktree-add``, this hook never blocks or denies
anything — it only ever adds an affirmative grant for the ALREADY-sanctioned case, so there is
no "genuine one-off exception" to route to a human. A non-canonical target simply gets no
opinion from this hook (see Scope above), same as if it were never installed.

Contract (agents-hooks/v1):
  stdin  : JSON event; the EnterWorktree target is ``args.path`` (absent when creating a new
           worktree via ``args.name``)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow (plain, or an explicit ``{"decision": "grant", ...}`` for the canonical case —
           see ``agent_hooks_v1.runner._allow_outcome`` / ``cc_hook_bridge.dispatch.py``'s
           "PreToolUse grant" note for how a bridge turns that into a suppressed prompt)
  exit 10: BLOCK — never emitted by this hook (kept as a protocol constant only for parity/
           documentation; this hook has nothing to block)
  other  : error (host on_error policy)

on_error is "open": this hook only ever ADDS a convenience (skip an unnecessary prompt for an
already-sanctioned case); a crash here must never wedge or weaken anything — it just means the
canonical case goes back to prompting like it did before this hook existed, never a new block.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 — fixed git argv, no shell
import sys
from pathlib import Path

BLOCK_EXIT_CODE = 10  # unused (this hook never blocks) — kept for protocol-doc symmetry
HOOK_API = "agents-hooks/v1"
HOOK_ID = "enterworktree-canonical-path-grant"

_GIT_TIMEOUT_S = 3.0
_WORKTREES_DIR_NAME = ".worktrees"  # SYNC: riglib/config.py::WORKTREES_DIR_NAME in rig-cli,
                                     # and block-raw-worktree-add/block_raw_worktree_add.py


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


# ── git plumbing (SYNC: copied from
# agent-hooks/block-raw-worktree-add/block_raw_worktree_add.py, itself SYNC'd with
# pin-primary-worktree/pin_primary_worktree.py — the ``git_dir`` override parameter is
# deliberately TRIMMED here, see the module docstring's "Detection reuses" note) ────────────────

def _git(cwd: str, *args: str) -> str | None:
    """Run a read-only ``git -C cwd <args>`` and return trimmed stdout, or None on failure."""
    argv = ["git", "-C", cwd or ".", *args]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, no shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"git {' '.join(args)} failed: {exc}")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_path(cwd: str, *rev_parse_args: str) -> Path | None:
    out = _git(cwd, "rev-parse", *rev_parse_args)
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    try:
        return p.resolve()
    except OSError:
        return None


def repo_root(cwd: str) -> Path | None:
    """The repo's single, unambiguous root, resolved the SAME way regardless of whether ``cwd``
    sits in the primary worktree or a linked one: ``git rev-parse --git-common-dir`` always
    resolves to the primary worktree's real ``.git``. None (undetermined) on any git/resolve
    failure, or when the resolved dir isn't ``.git``-named (a bare repo — out of scope, fails
    open rather than guessing a wrong root)."""
    common_dir = _git_path(cwd, "--git-common-dir")
    if common_dir is None or common_dir.name != ".git":
        return None
    return common_dir.parent


def _worktrees_dir_escapes_repo(root: Path) -> bool:
    """True iff ``<root>/.worktrees`` is itself a symlink resolving OUTSIDE ``root`` — the one
    way the "target resolves to a direct child of ``.worktrees/``" check below could be gamed (mirrors
    ``riglib/worktree.py::_target_escapes_repo``). A dangling symlink is treated as escaped
    too — not a legitimate ``.worktrees``, don't trust it either way."""
    candidate = root / _WORKTREES_DIR_NAME
    if not candidate.is_symlink():
        return False
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return True
    try:
        resolved.relative_to(root.resolve())
        return False
    except ValueError:
        return True


def _target_is_sanctioned(root: Path, resolved_target: Path) -> bool:
    """True iff ``resolved_target`` has the direct-child ``<root>/.worktrees/<name>`` shape
    (not ``.worktrees`` itself or a nested path like ``.worktrees/sub/feat`` — neither is
    the ``rig worktree create`` shape) AND ``.worktrees`` itself
    isn't a symlink escaping ``root``."""
    if _worktrees_dir_escapes_repo(root):
        return False
    allowed_dir = (root / _WORKTREES_DIR_NAME).resolve()
    if resolved_target == allowed_dir:
        return False
    try:
        relative_target = resolved_target.relative_to(allowed_dir)
        return len(relative_target.parts) == 1
    except ValueError:
        return False


def _resolve_target(cwd: str, target: str) -> Path:
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = Path(cwd or ".") / target_path
    return target_path.resolve(strict=False)


def _grant_message(root: Path, path: str) -> str:
    return (
        f"EnterWorktree(path={path}) resolves to this repo's rig-sanctioned direct-child "
        f"{root / _WORKTREES_DIR_NAME}/<name> shape — pre-approved (rig-cli#418), no prompt needed. "
        "(Entering a DIFFERENT agent's worktree there is a separate check, enforced by the "
        "enterworktree-foreign-guard agent-hook, not this one.)"
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing, no grant (fail-open)")
        emit("allow")
        return 0
    if not isinstance(event, dict):
        warn(f"event is not a JSON object ({type(event).__name__}) — allowing, no grant (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        # No `path` → creating a brand-new worktree via `name` (CC hardcodes that into
        # `.claude/worktrees/`, already trusted, nothing for this hook to check) or a malformed
        # call the tool itself will reject either way. Nothing to grant.
        emit("allow")
        return 0
    path = path.strip()

    cwd = str(event.get("cwd") or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        # repo root undetermined → no opinion from THIS hook; CC's own classifier still
        # decides, exactly as if this hook were not installed.
        emit("allow")
        return 0

    resolved_target = _resolve_target(cwd, path)
    if not _target_is_sanctioned(root, resolved_target):
        emit("allow")  # not the canonical case — defer to CC's normal prompt, unchanged
        return 0

    emit("grant", _grant_message(root, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
