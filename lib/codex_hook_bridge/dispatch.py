"""The agents-hooks/v1 -> Codex hook bridge dispatcher.

Accessed via: Codex hook commands call ``python3 -m codex_hook_bridge <Event>`` and
send the native hook event JSON on stdin.

Assumptions: Codex hook events use ``hook_event_name``, ``tool_name``,
``tool_input``, ``cwd``, ``model``, and ``permission_mode`` fields; blocking is
the plain top-level ``{"decision":"block","reason":"..."}`` JSON shape; file
edits arrive as the ``apply_patch`` tool with patch text in ``tool_input.command``.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

try:
    from agent_hooks_v1 import subagent_identity
except Exception:  # noqa: BLE001 - a missing helper must fail CLOSED (no identity → governed)
    subagent_identity = None

HOOK_API = "agents-hooks/v1"
# The v1 event's `harness` tag for every event this bridge produces — a MODULE LITERAL, not
# derived from `codex_event`, so it cannot be forged via tool_input the way `args.harness` could
# be. Hooks read it to pick the delegation recipe for THIS harness in their refusal text
# (agent-tools#573) — never to exempt the harness (the #533/#544 `EXEMPT_HARNESSES` shortcut is
# gone: every harness is governed identically, and the subagent identity below is what tells an
# orchestrator apart from a delegated child).
HARNESS = "codex"
_KNOWN_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "Stop", "SubagentStart", "SubagentStop"}
)
_APPLY_PATCH_TOOL = "apply_patch"
_ENV_OPTIONS_WITH_ARG = frozenset({"-u", "--unset", "-C", "--chdir", "-P", "--path"})
_ENV_OPTIONS_WITH_ARG_PREFIXES = ("--unset=", "--chdir=", "--path=")
_ENV_OPTIONS_WITHOUT_ARG = frozenset({"-", "-0", "-i", "--ignore-environment", "--null"})
_ID_KEYS = ("event_id", "session_id", "conversation_id", "turn_id", "tool_call_id", "call_id")
_METADATA_KEYS = (
    "model",
    "permission_mode",
    *_ID_KEYS,
)


def point_for_event(hook_event_name: str, tool_name: str | None) -> str | None:
    """Map a Codex (event, tool) pair to a logical v1 point."""
    if hook_event_name == "Stop":
        return "stop"
    if hook_event_name == "PreToolUse":
        if tool_name == "Bash":
            return "pre-bash"
        if tool_name == _APPLY_PATCH_TOOL:
            return "pre-write"
    if hook_event_name == "PostToolUse" and tool_name == _APPLY_PATCH_TOOL:
        return "post-write"
    return None


def to_v1_event(codex_event: dict, *, point: str) -> dict:
    """Translate a Codex hook event into the agents-hooks/v1 event shape."""
    tool_input = codex_event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    args = dict(tool_input)
    args.pop("command", None)
    for key in (
        "agent_id",
        "agent_type",
        "transcript_path",
        "file_path",
        "file_paths",
        "path",
        "content",
        "patch",
        *_METADATA_KEYS,
    ):
        args.pop(key, None)
    for key in _METADATA_KEYS:
        if codex_event.get(key) is not None:
            args[key] = codex_event[key]
    _apply_subagent_identity(codex_event, args)

    command = _tool_command(tool_input)
    if command:
        args["command"] = command
    if point in ("pre-write", "post-write") and codex_event.get("tool_name") == _APPLY_PATCH_TOOL:
        if command:
            args.setdefault("patch", command)
            if point == "pre-write":
                args["content"] = _patch_added_content(command)
            paths = _patch_file_paths(command)
            if paths:
                args.setdefault("file_paths", paths)
            if len(paths) == 1:
                args.setdefault("file_path", paths[0])
                args.setdefault("path", paths[0])
            elif _patch_move_target(command):
                args.setdefault("file_path", paths[-1])
                args.setdefault("path", paths[-1])

    return {
        "hook_api": HOOK_API,
        "event_id": _event_id(codex_event),
        "tool": codex_event.get("tool_name"),
        "point": point,
        "harness": HARNESS,
        "command": command,
        "cwd": codex_event.get("cwd") or os.getcwd(),
        "args": args,
    }


def _apply_subagent_identity(codex_event: dict, args: dict) -> None:
    """Populate ``args.agent_id``/``args.agent_type`` from the trusted sources ONLY (#573).

    ``to_v1_event`` has already dropped any ``agent_id``/``agent_type`` that rode in via
    ``tool_input`` (model-authored, never trusted). Four sources may restore them, in order:

    1. Codex's OWN top-level ``agent_id``/``agent_type`` — present on every hook event fired
       inside a ``collaboration.spawn_agent`` child thread (captured on codex 0.153.4: the
       child's Bash PreToolUse carries them, the parent's does not), the same shape Claude Code
       emits. Codex writes these from its thread bookkeeping; they are not part of the tool
       call the model composed, so nothing in ``tool_input`` can set or change them.
    2. The child thread's ROLLOUT ``session_meta`` behind the event's top-level
       ``transcript_path`` — ``thread_source: "subagent"`` with ``parent_thread_id`` equal to the
       event's ``session_id`` (see ``_rollout_subagent_identity``). This is the record every
       spawned codex thread carries, independent of whether that codex build also emits (1).
    3. The launcher env markers ``RIG_AGENT_ID``/``RIG_DETACHED_AGENT`` — a ``codex exec``
       child started by the ``rig-detached-codex`` launcher skill.
    4. Process ancestry — a ``codex`` process of the same harness above the one that dispatched
       this hook (a child ``codex exec`` run from a parent session's shell tool).

    See ``agent_hooks_v1.subagent_identity`` for why 3 and 4 are outside ``tool_input``'s reach.
    No source → no key at all (never an empty string), so every ``_is_subagent`` reader treats
    the call as the orchestrator's — fail closed in the relax direction."""
    native_id = codex_event.get("agent_id")
    if native_id is not None and str(native_id).strip():
        args["agent_id"] = native_id
        if codex_event.get("agent_type") is not None:
            args["agent_type"] = codex_event["agent_type"]
        return
    rollout_id, rollout_type = _rollout_subagent_identity(codex_event)
    if rollout_id:
        args["agent_id"] = rollout_id
        args["agent_type"] = rollout_type
        return
    if subagent_identity is None:
        return
    agent_id, agent_type = subagent_identity.detect_subagent(HARNESS)
    if agent_id:
        args["agent_id"] = agent_id
        args["agent_type"] = agent_type


# A rollout's first line is its `session_meta` record; with the full `base_instructions` text it
# runs to ~20 KB on codex 0.153.4, so the cap leaves headroom without ever reading a transcript.
_ROLLOUT_META_MAX_BYTES = 256 * 1024
_ROLLOUT_SUBAGENT_TYPE = "subagent"


def _rollout_subagent_identity(codex_event: dict) -> tuple[str, str]:
    """``(child_thread_id, "subagent")`` when the event's ``transcript_path`` is the rollout of a
    codex-spawned child thread, else ``("", "")``.

    Codex writes a rollout per thread under ``$CODEX_HOME/sessions/``; the first line is a
    ``session_meta`` record and a spawned child's carries ``thread_source: "subagent"``,
    ``parent_thread_id`` (the spawner) and its own ``id`` — the child thread id (audit of the
    2026-09-05 ``~/work/private`` sessions, codex 0.153.4: every ``orchestrator-stays-thin`` /
    ``no-long-inline-process`` refusal there landed on exactly such a thread). ``transcript_path``
    is a top-level field of the hook event, written by codex — the model's tool call cannot set
    it (``to_v1_event`` reads it from the event root only, never from ``tool_input``).

    What a cooperative-but-mistaken orchestrator CANNOT do with its own shell tool is make its
    own rollout pass this check: the rollout content alone is not trusted, the record must also
    be CONSISTENT with two facts codex owns — (a) the filename's trailing uuid equals the
    record's ``id`` (an orchestrator's rollout is named after its own session id, which is
    also the event's ``session_id``), and (b) ``parent_thread_id`` equals the event's
    ``session_id`` while ``id`` differs from it (a child's ``session_id`` is the PARENT's —
    codex's documented "subagent hooks use the parent session id", confirmed by the captured
    child-thread fixture and the audited session_meta records). Editing the first line of one's
    own rollout cannot satisfy (a) and (b) at once; renaming the file codex holds open leaves
    ``transcript_path`` pointing nowhere → no identity → governed. Every failure path (no path,
    unreadable, not JSON, an over-long first line, any field mismatch) yields ``("", "")``."""
    path = codex_event.get("transcript_path")
    session_id = str(codex_event.get("session_id") or "").strip()
    if not isinstance(path, str) or not path or not session_id:
        return "", ""
    payload = _rollout_session_meta_payload(path)
    if payload is None or payload.get("thread_source") != _ROLLOUT_SUBAGENT_TYPE:
        return "", ""
    thread_id = str(payload.get("id") or "").strip()
    parent_id = str(payload.get("parent_thread_id") or "").strip()
    if not thread_id or not parent_id:
        return "", ""
    if parent_id != session_id or thread_id == session_id:
        return "", ""
    if not Path(path).name.endswith(f"{thread_id}.jsonl"):
        return "", ""
    return thread_id, _ROLLOUT_SUBAGENT_TYPE


def _rollout_session_meta_payload(path: str) -> dict | None:
    """The ``payload`` dict of a rollout's first line when it is a ``session_meta`` record within
    the size cap, else ``None`` (any IO/JSON failure included)."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(_ROLLOUT_META_MAX_BYTES + 1)
    except OSError:
        return None
    if len(first) > _ROLLOUT_META_MAX_BYTES:
        return None
    try:
        record = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def codex_block_output(reason: str) -> dict:
    """Codex uses the same plain block shape for PreToolUse, PostToolUse, and Stop."""
    return {"decision": "block", "reason": reason}


def hooks_dir() -> Path:
    """Where Codex-installed v1 descriptors live, overridable for tests."""
    override = os.environ.get("CODEX_HOOKS_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.codex/hooks"))


def _inbox_text(hook_event_name: str, codex_event: dict) -> str:
    """Pending tg-ctl inbox messages for this Codex session, consumed and rendered as
    block-reason text (agent-tools#526); "" when nothing / on any failure (fail-open).

    Codex has no ``--name``, so the key is ALWAYS the cwd hash — computed directly with
    ``agent_key(None, cwd)``, never via ``agent_key_for_process``: a Codex started from a
    Claude-owned shell inherits ``CLAUDE_PID`` and would otherwise key on the Claude
    session's name.
    """
    if hook_event_name != "Stop":
        return ""
    try:
        from agenttools_tg_inbox import agent_key, stop_inbox_text
    except Exception as exc:  # noqa: BLE001 - optional lib; never block a stop over it
        _warn(f"tg inbox reader unavailable, skipping inbox check: {exc}")
        return ""
    cwd = codex_event.get("cwd") or os.getcwd()
    session_id = str(_event_id(codex_event) or "")
    return stop_inbox_text(lambda: agent_key(None, cwd), session_id=session_id, warn=_warn)


def _combine_block(inbox_text: str, hook_reason: str | None) -> dict | None:
    """One block for BOTH the inbox messages and a blocking hook's reason — the rule is
    `agenttools_tg_inbox.combine_stop_parts`, shared with the CC bridge (an empty-reason
    block still blocks); without the lib, a hook block passes through unchanged."""
    try:
        from agenttools_tg_inbox import combine_stop_parts
    except Exception:  # noqa: BLE001 - optional lib: no inbox, plain hook semantics
        return None if hook_reason is None else codex_block_output(hook_reason)
    reason = combine_stop_parts(inbox_text, hook_reason)
    return None if reason is None else codex_block_output(reason)


def dispatch(hook_event_name: str, codex_event: dict) -> dict | None:
    """Run the applicable v1 hooks for this Codex event; on Stop, pending tg-ctl inbox
    messages ride in the same block as a blocking hook's reason (the hooks always run,
    and run FIRST — consuming the inbox is irreversible, so it is the last step)."""
    hook_reason = _first_hook_block(hook_event_name, codex_event)
    return _combine_block(_inbox_text(hook_event_name, codex_event), hook_reason)


def _first_hook_block(hook_event_name: str, codex_event: dict) -> str | None:
    """The reason of the first blocking v1 hook for this event, or None."""
    tool_name = codex_event.get("tool_name")
    point = point_for_event(hook_event_name, tool_name)
    if point is None:
        return None
    runner = _load_runner()
    if runner is None:
        return None
    load_descriptors, run_hook = runner
    specs = load_descriptors(point, hooks_dir(), warn=_warn)
    if not specs:
        return None
    v1_events = _v1_events_for_dispatch(codex_event, point=point)
    for spec in specs:
        for v1_event in v1_events:
            outcome, reason = run_hook(spec, v1_event, warn=_warn)
            if outcome == "block":
                return reason
    return None


def _load_runner():
    try:
        from agent_hooks_v1 import load_descriptors, run_hook
    except Exception as exc:  # noqa: BLE001 - a missing shared runner must fail open
        _warn(f"could not import shared v1 runner, failing OPEN (allowing the call): {exc}")
        return None
    return load_descriptors, run_hook


def main(argv: list[str]) -> int:
    """CLI entry: ``codex_hook_bridge <EventName>``.

    Top-level dispatcher failures fail open: a broken bridge must not wedge every
    Codex tool call. Deliberate hook blocks still return block JSON on stdout.
    """
    hook_event_name = argv[0] if argv else "PreToolUse"
    if hook_event_name not in _KNOWN_EVENTS:
        _warn(f"unknown event arg {hook_event_name!r}; stdin's hook_event_name will be used if present")
    try:
        raw = sys.stdin.read()
        codex_event = json.loads(raw) if raw.strip() else {}
        if not isinstance(codex_event, dict):
            codex_event = {}
        hook_event_name = codex_event.get("hook_event_name") or hook_event_name
        result = dispatch(hook_event_name, codex_event)
    except Exception as exc:  # noqa: BLE001 - fail-open is the bridge contract
        _warn(f"dispatcher error, failing OPEN (allowing the call): {exc}")
        return 0
    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0


def _tool_command(tool_input: dict) -> str:
    command = tool_input.get("command", "")
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        parts = [str(part) for part in command]
        parts = _strip_env_wrapper(parts)
        shell_payload = _shell_command_payload(parts)
        return shell_payload if shell_payload is not None else shlex.join(parts)
    if command is None:
        return ""
    return ""


def _shell_command_payload(parts: list[str]) -> str | None:
    if not parts or Path(parts[0]).name not in {"bash", "sh", "zsh"}:
        return None
    for index, part in enumerate(parts[1:], start=1):
        if part == "--":
            break
        if part == "-c" or (part.startswith("-") and not part.startswith("--") and "c" in part[1:]):
            if index + 1 < len(parts):
                return parts[index + 1]
            return None
    return None


def _strip_env_wrapper(parts: list[str]) -> list[str]:
    if not parts or Path(parts[0]).name != "env":
        return parts
    index = 1
    while index < len(parts):
        part = parts[index]
        if part == "--":
            index += 1
            break
        if part in _ENV_OPTIONS_WITHOUT_ARG:
            index += 1
            continue
        if part in _ENV_OPTIONS_WITH_ARG:
            if index + 1 >= len(parts):
                return parts
            index += 2
            continue
        if any(part.startswith(prefix) for prefix in _ENV_OPTIONS_WITH_ARG_PREFIXES):
            index += 1
            continue
        if "=" not in part or part.startswith("-"):
            break
        index += 1
    return parts[index:] if index < len(parts) else parts


def _event_id(codex_event: dict) -> str:
    for key in _ID_KEYS:
        val = codex_event.get(key)
        if val:
            return str(val)
    return ""


def _v1_events_for_dispatch(codex_event: dict, *, point: str) -> list[dict]:
    base = to_v1_event(codex_event, point=point)
    args = base.get("args")
    if not isinstance(args, dict):
        return [base]
    file_paths = args.get("file_paths")
    if (
        point not in ("pre-write", "post-write")
        or not isinstance(file_paths, list)
        or len(file_paths) <= 1
    ):
        return [base]

    events: list[dict] = []
    content_by_path = _patch_added_content_by_path(str(args.get("patch") or ""))
    for raw_path in file_paths:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        event = dict(base)
        event_args = dict(args)
        event_args["file_path"] = raw_path
        event_args["path"] = raw_path
        event_args.pop("file_paths", None)
        if point == "pre-write":
            event_args["content"] = content_by_path.get(raw_path, "")
        event["args"] = event_args
        events.append(event)
    return events or [base]


def _patch_file_paths(patch: str) -> list[str]:
    """Extract file paths from apply_patch text, preserving first-seen order."""
    paths: list[str] = []
    seen: set[str] = set()
    pending_update: str | None = None

    def add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            paths.append(path)

    def flush_update() -> None:
        nonlocal pending_update
        if pending_update:
            add(pending_update)
            pending_update = None

    for line in patch.splitlines():
        if line.startswith("*** Update File: "):
            flush_update()
            pending_update = line[len("*** Update File: ") :].strip()
        elif line.startswith("*** Move to: "):
            flush_update()
            pending_update = line[len("*** Move to: ") :].strip()
        elif line.startswith("*** Add File: "):
            flush_update()
            add(line[len("*** Add File: ") :].strip())
        elif line.startswith("*** Delete File: "):
            flush_update()
            add(line[len("*** Delete File: ") :].strip())
    flush_update()
    return paths


def _patch_added_content(patch: str) -> str:
    """Return added patch lines without their leading diff marker."""
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines)


def _patch_added_content_by_path(patch: str) -> dict[str, str]:
    content: dict[str, str] = {}
    current_paths: list[str] = []
    current_lines: list[str] = []

    def finish() -> None:
        nonlocal current_paths, current_lines
        for path in current_paths:
            content[path] = "\n".join(current_lines)
        current_paths = []
        current_lines = []

    for line in patch.splitlines():
        if line.startswith("*** Update File: "):
            finish()
            current_paths = [line[len("*** Update File: ") :].strip()]
        elif line.startswith("*** Move to: "):
            finish()
            moved_to = line[len("*** Move to: ") :].strip()
            current_paths = [moved_to] if moved_to else []
        elif line.startswith("*** Add File: "):
            finish()
            current_paths = [line[len("*** Add File: ") :].strip()]
        elif line.startswith("*** Delete File: "):
            finish()
            current_paths = [line[len("*** Delete File: ") :].strip()]
        elif current_paths and line.startswith("+"):
            current_lines.append(line[1:])
    finish()
    return content


def _patch_move_target(patch: str) -> str:
    for line in patch.splitlines():
        if line.startswith("*** Move to: "):
            return line[len("*** Move to: ") :].strip()
    return ""


def _warn(msg: str) -> None:
    """Human log to stderr; Codex hook output parsing only reads stdout JSON."""
    sys.stderr.write(f"codex-hook-bridge: {msg}\n")
