"""The agents-hooks/v1 → Claude Code bridge dispatcher.

Reads a Claude Code hook event on stdin, runs the installed ``agents-hooks/v1``
descriptors that apply, and translates the v1 exit-10 BLOCK into CC's own block signal.

Confirmed CC contract (https://code.claude.com/docs/en/hooks, CC 2.1.177):
  - PreToolUse stdin : {tool_name, tool_input{}, cwd, permission_mode, hook_event_name, …}
  - PreToolUse block : exit 0 + {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                       "permissionDecision": "deny", "permissionDecisionReason": "..."}}
                       (we use the STRUCTURED JSON form, not exit 2, because it carries a
                       rich reason; exit 2 also blocks but discards any JSON.)
  - PostToolUse      : the tool already ran — cannot block; feedback via
                       {"decision": "block", "reason": "..."} (stops the turn, not the tool).
  - Stop block       : exit 0 + {"decision": "block", "reason": "..."}.
  - matcher          : matched by CC against tool_name BEFORE we run; the settings.json entry
                       carries the matcher, so the dispatcher only needs the logical point.

agents-hooks/v1 (agent-hooks/README.md):
  - descriptor : {id, point, cmd (ABSOLUTE), args[], priority, timeout_ms, on_error}
  - hook stdin : {hook_api, event_id, tool, point, command, cwd, args{}}
  - hook exit  : 0 allow · 10 BLOCK (canonical, even if stdout malformed) · other → on_error
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 — running the installed hook scripts is the whole job
import sys
from pathlib import Path

V1_BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
DEFAULT_TIMEOUT_MS = 5000
_DEFAULT_PRIORITY = 50  # the agents-hooks/v1 default (agent-hooks/README.md)
# CC events this bridge knows how to register/handle (typo guard in main()).
_KNOWN_EVENTS = frozenset({"PreToolUse", "Stop", "PostToolUse"})

# A CC tool name → the logical agents-hooks/v1 point it maps to. Only tools that have a
# v1 point are dispatched; everything else is a clean no-op (the tool proceeds normally).
# pre-write covers every file-mutating tool CC exposes. EXTENSIBILITY: a new CC file-edit
# tool must be added here AND its payload field taught to `_proposed_write_text`, or its
# content won't be scanned by the pre-write guards. Keep this set in sync with the README's
# point table and the rig-cli matcher `Edit|Write|MultiEdit|NotebookEdit`.
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# The subagent-dispatch tools CC exposes. CC's first-party name is `Task`; some harness
# builds/SDKs surface it as `Agent`. The enforcement-doctrine `background-subagent` gate
# (agent-hooks/background-subagent-gate) hangs off the `pre-tool` point below so it can
# inspect the WHOLE tool_input (e.g. `run_in_background`) — the bridge passes tool_input
# through under `args` verbatim, so a hook reads any field it needs. Keep this set in sync
# with the README's point table and the rig-cli matcher `Task|Agent`.
_SUBAGENT_DISPATCH_TOOLS = frozenset({"Task", "Agent"})


def point_for_event(hook_event_name: str, tool_name: str | None) -> str | None:
    """Map a CC (event, tool) pair to a logical v1 point, or None if nothing applies."""
    if hook_event_name == "Stop":
        return "stop"
    if hook_event_name == "PreToolUse":
        if tool_name == "Bash":
            return "pre-bash"
        if tool_name in _WRITE_TOOLS:
            return "pre-write"
        if tool_name in _SUBAGENT_DISPATCH_TOOLS:
            return "pre-tool"
        if tool_name == "Skill":
            return "pre-skill"
    return None


def to_v1_event(cc_event: dict, *, point: str) -> dict:
    """Translate a CC hook event into the agents-hooks/v1 event a hook script reads.

    ``args`` carries the action payload the v1 hooks look for: ``args.command`` for a bash
    command, ``args.file_path``/``args.content`` for a write, ``args.session_id`` for stop.
    We pass the WHOLE ``tool_input`` through under ``args`` so a hook can read any field,
    and additionally surface ``command`` at the top level (a couple of v1 hooks read it
    there as a fallback).

    For pre-write we NORMALIZE the proposed text into ``args.content`` regardless of which
    edit tool fired, because the shipped pre-write hooks (block-secrets-write,
    block-raw-process-env) only scan flat ``content``/``new_string``/``text``. Without this
    a secret hidden in a ``MultiEdit`` (``edits[].new_string``) or a ``NotebookEdit``
    (``new_source``) would slip past the very gate that exists to catch it. Normalizing in
    ONE place keeps every current and future pre-write hook correct without touching them.
    """
    tool_input = cc_event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    args = dict(tool_input)
    if point == "pre-write":
        proposed = _proposed_write_text(tool_input)
        if proposed and not isinstance(args.get("content"), str):
            args["content"] = proposed
        # Normalize the target PATH too: NotebookEdit carries `notebook_path`, not the
        # `file_path` the shipped pre-write hooks scope on. Without this a NotebookEdit
        # presents an empty path, so path-scoped raw-env checks and secret-scan allowlists
        # wave the write through — the same bypass the content-normalization above prevents.
        path = _proposed_write_path(tool_input)
        if path:
            args.setdefault("file_path", path)
            args.setdefault("path", path)
    # stop has no tool_input; carry the CC session id so a stop hook can key its marker.
    if "session_id" not in args and cc_event.get("session_id"):
        args["session_id"] = cc_event["session_id"]
    return {
        "hook_api": HOOK_API,
        "event_id": cc_event.get("session_id", ""),
        "tool": cc_event.get("tool_name"),
        "point": point,
        "command": tool_input.get("command", ""),
        "cwd": cc_event.get("cwd", os.getcwd()),
        "args": args,
    }


def _proposed_write_text(tool_input: dict) -> str:
    """All the text a write/edit tool is about to put on disk, flattened for scanning.

    Covers every CC file-mutating tool's payload shape:
      - Write       → ``content``
      - Edit        → ``new_string``
      - MultiEdit   → ``edits[].new_string`` (joined)
      - NotebookEdit→ ``new_source`` (CC's notebook cell field)
    Returns "" when there's nothing to scan. Joining keeps the existing flat-``content``
    hooks correct for the multi-edit case (the secret-scanner regexes match line-wise).
    """
    parts: list[str] = []
    for key in ("content", "new_string", "text", "new_source"):
        val = tool_input.get(key)
        if isinstance(val, str):
            parts.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                parts.append(edit["new_string"])
    return "\n".join(parts)


def _proposed_write_path(tool_input: dict) -> str:
    """The on-disk path a write/edit tool targets, normalized across CC's tool payloads.

    Write/Edit/MultiEdit use ``file_path``; NotebookEdit uses ``notebook_path``. The shipped
    pre-write hooks scope on ``args.file_path``/``args.path``, so a NotebookEdit would otherwise
    present an EMPTY path and slip past path-scoped checks. Returns "" when there's no path.
    """
    for key in ("file_path", "path", "notebook_path"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def cc_block_output(hook_event_name: str, reason: str) -> dict:
    """The CC block JSON for an event — PreToolUse uses permissionDecision, Stop uses decision."""
    if hook_event_name == "Stop":
        return {"decision": "block", "reason": reason}
    # PreToolUse (and any future tool event) → structured permission deny.
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def hooks_dir() -> Path:
    """Where CC-installed v1 descriptors live (overridable for tests)."""
    override = os.environ.get("CC_HOOKS_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.claude/hooks"))


def _load_descriptors(point: str) -> list[dict]:
    """Read every ``*.json`` descriptor for this point, sorted by priority then id.

    A malformed or unreadable descriptor is skipped with a warning — one bad file must not
    take down the whole dispatch (fail-open at the enumeration layer). The per-hook
    ``on_error`` only governs the hook's RUNTIME error, not a descriptor parse error here.
    """
    specs: list[dict] = []
    d = hooks_dir()
    if not d.is_dir():
        return specs
    for path in sorted(d.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _warn(f"skipping unreadable descriptor {path}: {exc}")
            continue
        if not isinstance(spec, dict) or spec.get("point") != point:
            continue
        specs.append(spec)
    # A non-int `priority` must never crash the sort (which would bubble to the top-level
    # fail-open and skip EVERY hook — including fail-closed gates). _safe_int falls back to
    # the default and the bad descriptor still RUNS in default-priority order.
    specs.sort(key=lambda spec: (_safe_int(spec.get("priority"), _DEFAULT_PRIORITY), str(spec.get("id", ""))))
    return specs


def _run_hook(spec: dict, v1_event: dict) -> tuple[str, str]:
    """Run one descriptor's script with the v1 event on stdin.

    Returns ``(outcome, reason)`` where outcome is ``"allow"`` | ``"block"`` — the v1
    exit code is canonical: 10 = block, 0 = allow, any other = error resolved by the
    descriptor's ``on_error`` (``closed`` → block, ``open`` → allow). The block reason is
    taken from the hook's stdout ``message`` when present, else a generic fallback.
    """
    hook_id = str(spec.get("id", "?"))
    cmd = str(spec.get("cmd", ""))
    if not cmd or not os.path.isabs(cmd):
        # the v1 contract REQUIRES an absolute cmd; a relative/empty one is unrunnable.
        return _on_error_outcome(spec, hook_id, "descriptor cmd is not absolute")
    raw_args = spec.get("args")
    argv = [cmd, *(str(a) for a in raw_args)] if isinstance(raw_args, list) else [cmd]
    # A MISSING or NULL timeout_ms is fine → the default. A PRESENT-but-bad one (non-numeric,
    # bool, or negative) is a DESCRIPTOR error, resolved per-hook: a fail-closed gate with a
    # typo'd timeout must DENY, not be silently skipped by the dispatcher's fail-open.
    raw_timeout = spec.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if raw_timeout is None:
        raw_timeout = DEFAULT_TIMEOUT_MS
    # bool is an int subclass, so int(True)==1 / int(False)==0 would sneak past _safe_int and
    # be silently mis-read as a 1 ms timeout / "unset". A boolean is a descriptor typo.
    if isinstance(raw_timeout, bool):
        return _on_error_outcome(spec, hook_id, f"non-numeric timeout_ms {raw_timeout!r}")
    timeout_ms = _safe_int(raw_timeout, None)
    if timeout_ms is None:
        return _on_error_outcome(spec, hook_id, f"non-numeric timeout_ms {raw_timeout!r}")
    # 0 = unset → the default; a NEGATIVE timeout is a descriptor typo, not "unset", so it is
    # a descriptor error (a fail-closed gate must DENY, like any other bad descriptor field).
    # This also avoids the old 1 ms floor that made such hooks spuriously time out.
    if timeout_ms == 0:
        timeout_ms = DEFAULT_TIMEOUT_MS
    elif timeout_ms < 0:
        return _on_error_outcome(spec, hook_id, f"negative timeout_ms {timeout_ms}")
    timeout_s = timeout_ms / 1000.0
    # Serialize OUTSIDE the try: a json.dumps failure here is a DISPATCHER bug (must fail open),
    # not a hook error — keeping it out of the try keeps the ValueError catch below scoped to
    # the subprocess launch (embedded-NUL argv), per the fail-policy contract.
    payload = json.dumps(v1_event)
    try:
        proc = subprocess.run(  # noqa: S603 — cmd comes from a trusted local descriptor dir
            argv,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",  # pin decode so it's platform-independent, not locale-default
            errors="replace",  # non-UTF-8 hook output must not raise → it would escape to the
                               # dispatcher's fail-open and turn a deliberate block into an allow
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _on_error_outcome(spec, hook_id, f"timed out after {timeout_s:.1f}s")
    except (OSError, ValueError) as exc:
        # OSError: an unrunnable cmd. ValueError: an embedded NUL in cmd/args. Both are
        # hook/descriptor failures resolved per-hook via on_error. We deliberately do NOT
        # catch bare Exception: a bug in the DISPATCHER itself must fail OPEN per the contract
        # (README), not masquerade as a hook error and block on a fail-closed gate.
        return _on_error_outcome(spec, hook_id, f"could not run {cmd}: {exc}")

    if proc.stderr:
        _warn(f"{hook_id}: {proc.stderr.strip()}")

    if proc.returncode == V1_BLOCK_EXIT_CODE:
        return "block", _block_reason(proc.stdout, hook_id)
    if proc.returncode == 0:
        return "allow", ""
    # any other exit code is a hook ERROR — resolve by the descriptor's on_error policy.
    return _on_error_outcome(spec, hook_id, f"exited {proc.returncode}")


def _safe_int(value, default):
    """``int(value)`` or ``default`` — a non-numeric descriptor field never raises."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _block_reason(stdout: str, hook_id: str) -> str:
    """Pull the human reason out of a hook's v1 protocol JSON; fall back to a generic one."""
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except ValueError:
        payload = {}
    msg = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return f"Blocked by agent-hook '{hook_id}'."


def _on_error_outcome(spec: dict, hook_id: str, detail: str) -> tuple[str, str]:
    """Resolve a hook ERROR via its ``on_error`` policy. Default 'open' (advisory)."""
    policy = str(spec.get("on_error", "open")).lower()
    _warn(f"{hook_id}: {detail} (on_error={policy})")
    if policy == "closed":
        return "block", (
            f"agent-hook '{hook_id}' could not complete its check and is fail-closed "
            f"(on_error=closed): {detail}. Denying rather than letting the action through."
        )
    return "allow", ""


def _warn(msg: str) -> None:
    """Human log to stderr — CC shows the first stderr line as a hook notice; never parsed."""
    sys.stderr.write(f"cc-hook-bridge: {msg}\n")


def dispatch(hook_event_name: str, cc_event: dict) -> dict | None:
    """Run the applicable v1 hooks for this CC event; return the CC block JSON or None.

    None = no hook blocked → emit nothing (the tool proceeds through CC's normal flow).
    First block wins; its reason is surfaced to the model. Errors here are caught by the
    caller and turned into a fail-OPEN allow.
    """
    tool_name = cc_event.get("tool_name")
    point = point_for_event(hook_event_name, tool_name)
    if point is None:
        return None  # nothing maps to this (event, tool) → no-op
    specs = _load_descriptors(point)
    if not specs:
        return None
    v1_event = to_v1_event(cc_event, point=point)
    for spec in specs:
        outcome, reason = _run_hook(spec, v1_event)
        if outcome == "block":
            return cc_block_output(hook_event_name, reason)
    return None


def main(argv: list[str]) -> int:
    """CLI entry: ``cc_hook_bridge <EventName>``. Always exit 0 (block is via JSON stdout).

    Fail-OPEN is the top-level contract: ANY unexpected error in the dispatcher itself is
    swallowed (logged to stderr) and the call is allowed, so a broken bridge can never wedge
    every tool call. A hook that DELIBERATELY blocks still blocks — that path returns the CC
    block JSON above and never raises.
    """
    hook_event_name = argv[0] if argv else "PreToolUse"
    if hook_event_name not in _KNOWN_EVENTS:
        # a typo in the settings.json command (`PreToolUsr`) would otherwise be invisible.
        _warn(f"unknown event arg {hook_event_name!r}; stdin's hook_event_name will be used if present")
    try:
        raw = sys.stdin.read()
        cc_event = json.loads(raw) if raw.strip() else {}
        if not isinstance(cc_event, dict):
            cc_event = {}
        # the event name on stdin (if present) is authoritative over argv.
        hook_event_name = cc_event.get("hook_event_name") or hook_event_name
        result = dispatch(hook_event_name, cc_event)
    except Exception as exc:  # noqa: BLE001 — fail-open is the explicit safety contract
        _warn(f"dispatcher error, failing OPEN (allowing the call): {exc}")
        return 0
    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0
