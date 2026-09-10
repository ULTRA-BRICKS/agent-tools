"""Run installed agents-hooks/v1 descriptors for harness bridge dispatchers.

Accessed via: a harness bridge maps its native event to a v1 point, builds the v1
event payload, then calls ``load_descriptors`` and ``run_hook`` from this module.

Assumptions: the harness bridge decides fail-open behavior at its top level; this
module only resolves descriptor and hook runtime failures according to each
descriptor's ``on_error`` policy.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - descriptor execution is the purpose of this module
from collections.abc import Callable
from pathlib import Path

V1_BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
DEFAULT_TIMEOUT_MS = 5000
_DEFAULT_PRIORITY = 50  # the agents-hooks/v1 default (agent-hooks/README.md)

Warn = Callable[[str], None]


def load_descriptors(point: str, descriptor_dir: Path, *, warn: Warn) -> list[dict]:
    """Read every descriptor for ``point``, sorted by priority then id.

    A malformed or unreadable descriptor is skipped with a warning. The per-hook
    ``on_error`` policy governs runtime failures after a descriptor has loaded.
    """
    specs: list[dict] = []
    if not descriptor_dir.is_dir():
        return specs
    for path in sorted(descriptor_dir.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warn(f"skipping unreadable descriptor {path}: {exc}")
            continue
        if not isinstance(spec, dict) or spec.get("point") != point:
            continue
        specs.append(spec)
    specs.sort(
        key=lambda spec: (
            _safe_int(spec.get("priority"), _DEFAULT_PRIORITY),
            str(spec.get("id", "")),
        )
    )
    return specs


def run_hook(spec: dict, v1_event: dict, *, warn: Warn) -> tuple[str, str]:
    """Run one descriptor's script with the v1 event on stdin.

    Returns ``(outcome, reason)`` where outcome is ``"allow"``, ``"grant"``, or ``"block"``.
    Exit code 10 is the canonical v1 block signal. Exit code 0 allows — and, unlike a plain
    silent allow, a hook may additionally OPT IN to ``"grant"`` (agent-tools#610-adjacent,
    rig-cli#418) by emitting ``{"decision": "grant", "message": "..."}`` on stdout: an
    AFFIRMATIVE pre-approval a bridge can translate into its harness's OWN "suppress the
    interactive prompt" signal (e.g. Claude Code's ``permissionDecision: "allow"``), as
    opposed to plain ``"allow"`` (exit 0, no opinion), which only means "this hook did not
    object" and leaves the harness's own classifier/prompt to decide as it would with no hook
    installed at all. See :func:`_allow_outcome` for the parse. Any other exit code, timeout,
    or launch problem is resolved by the descriptor's ``on_error`` policy.
    """
    hook_id = str(spec.get("id", "?"))
    cmd = str(spec.get("cmd", ""))
    if not cmd or not os.path.isabs(cmd):
        return _on_error_outcome(spec, hook_id, "descriptor cmd is not absolute", warn)
    raw_args = spec.get("args")
    argv = [cmd, *(str(a) for a in raw_args)] if isinstance(raw_args, list) else [cmd]

    raw_timeout = spec.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if raw_timeout is None:
        raw_timeout = DEFAULT_TIMEOUT_MS
    # bool is an int subclass, so int(True) would otherwise become a 1 ms timeout.
    if isinstance(raw_timeout, bool):
        return _on_error_outcome(spec, hook_id, f"non-numeric timeout_ms {raw_timeout!r}", warn)
    timeout_ms = _safe_int(raw_timeout, None)
    if timeout_ms is None:
        return _on_error_outcome(spec, hook_id, f"non-numeric timeout_ms {raw_timeout!r}", warn)
    # 0 is the v1 "unset" value; negative numbers are descriptor typos.
    if timeout_ms == 0:
        timeout_ms = DEFAULT_TIMEOUT_MS
    elif timeout_ms < 0:
        return _on_error_outcome(spec, hook_id, f"negative timeout_ms {timeout_ms}", warn)
    timeout_s = timeout_ms / 1000.0

    # Keep serialization outside the subprocess try: json.dumps failures are bridge bugs
    # that the caller's top-level fail-open policy should handle, not hook errors.
    payload = json.dumps(v1_event)
    try:
        proc = subprocess.run(  # noqa: S603 - cmd comes from a trusted local descriptor dir
            argv,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _on_error_outcome(spec, hook_id, f"timed out after {timeout_s:.1f}s", warn)
    except (OSError, ValueError) as exc:
        # Do not catch bare Exception here. Dispatcher bugs must bubble to the bridge's
        # fail-open guard instead of masquerading as descriptor failures.
        return _on_error_outcome(spec, hook_id, f"could not run {cmd}: {exc}", warn)

    if proc.stderr:
        warn(f"{hook_id}: {proc.stderr.strip()}")

    if proc.returncode == V1_BLOCK_EXIT_CODE:
        return "block", _block_reason(proc.stdout, hook_id)
    if proc.returncode == 0:
        return _allow_outcome(proc.stdout)
    return _on_error_outcome(spec, hook_id, f"exited {proc.returncode}", warn)


def _safe_int(value: object, default: int | None) -> int | None:
    """``int(value)`` or ``default``; descriptor typos never crash sorting/parsing."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _allow_outcome(stdout: str) -> tuple[str, str]:
    """Distinguish a plain exit-0 ``"allow"`` from an explicit exit-0 ``"grant"``.

    A hook that wants to actively PRE-APPROVE a case it has positively vetted (not merely
    "no objection") emits ``{"decision": "grant", "message": "..."}`` on stdout; a bridge that
    understands ``"grant"`` (today: ``cc_hook_bridge`` for ``pre-worktree-enter``) can
    translate it into the harness's own "suppress the interactive prompt" signal. Any other
    ``decision`` value — absent, ``"allow"``, malformed JSON, empty stdout, or an unrecognized
    string — MUST fall back to a plain ``"allow"``: this keeps every existing hook (none of
    which emit ``"decision"`` on their allow path today) byte-for-byte unaffected, and keeps a
    forward-incompatible/typo'd decision value from silently escalating into anything more
    than today's silent allow.
    """
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("decision") == "grant":
        msg = payload.get("message")
        return "grant", msg.strip() if isinstance(msg, str) and msg.strip() else ""
    return "allow", ""


def _block_reason(stdout: str, hook_id: str) -> str:
    """Pull the human reason out of a hook's v1 protocol JSON."""
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except ValueError:
        payload = {}
    msg = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return f"Blocked by agent-hook '{hook_id}'."


def _on_error_outcome(spec: dict, hook_id: str, detail: str, warn: Warn) -> tuple[str, str]:
    """Resolve a hook error via the descriptor's ``on_error`` policy."""
    policy = str(spec.get("on_error", "open")).lower()
    warn(f"{hook_id}: {detail} (on_error={policy})")
    if policy == "closed":
        return "block", (
            f"agent-hook '{hook_id}' could not complete its check and is fail-closed "
            f"(on_error=closed): {detail}. Denying rather than letting the action through."
        )
    return "allow", ""
