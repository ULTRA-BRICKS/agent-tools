"""The agents-hooks/v1 -> opencode plugin bridge dispatcher.

opencode loads JavaScript plugins from config/plugin directories. The companion
``plugin.js`` file calls this module for ``tool.execute.before`` and
``tool.execute.after`` events. This dispatcher maps opencode's tool payload to the
shared agents-hooks/v1 contract and returns a plain ``{"decision":"block"}`` JSON
object to the plugin. The plugin throws for pre-tool blocks and logs post-write
feedback because completed writes cannot be un-run.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

HOOK_API = "agents-hooks/v1"
# The v1 event's `harness` tag for every event this bridge produces — a MODULE LITERAL, not
# derived from `opencode_event`, so it cannot be forged via tool args the way `args.harness`
# could be. See `codex_hook_bridge.HARNESS` for the same reasoning: opencode exposes no
# TRUSTED per-tool-call subagent identity in the plugin payload either (forged
# agent_id/agent_type keys are stripped below). The ONE trusted identity source is the
# process environment set by the rig detached launcher (`_detached_agent_id`,
# agent-tools#476) — and that covers only a rig-launched detached child; a plain opencode
# session still carries no identity. A hook that wants to tailor a message (or otherwise
# branch) on the source harness reads `event["harness"]` instead — see
# `agent-hooks/orchestrator-stays-thin`'s per-harness delegation-recipe text for the first
# consumer (agent-tools#533; unified into a single subagent-identity story across every
# harness by agent-tools#573).
HARNESS = "opencode"
_KNOWN_EVENTS = frozenset({"tool.execute.before", "tool.execute.after"})
_WRITE_TOOLS = frozenset({"edit", "write", "apply_patch"})
_TASK_TOOL = "task"
_SKILL_TOOL = "skill"
_ID_KEYS = (
    "event_id",
    "sessionID",
    "sessionId",
    "session_id",
    "messageID",
    "messageId",
    "toolCallID",
    "toolCallId",
    "callID",
    "callId",
)
_FORGED_AGENT_KEYS = (
    "agent_id",
    "agent_type",
    "agentID",
    "agentId",
    "agentType",
    "agent",
)


def point_for_event(hook_name: str, tool_name: str | None) -> str | None:
    """Map an opencode plugin hook/tool pair to a logical v1 point."""
    tool = (tool_name or "").lower()
    if hook_name == "tool.execute.before":
        if tool == "bash":
            return "pre-bash"
        if tool in _WRITE_TOOLS:
            return "pre-write"
        if tool == _TASK_TOOL:
            return "pre-agent"
        if tool == _SKILL_TOOL:
            return "pre-skill"
    if hook_name == "tool.execute.after" and tool in _WRITE_TOOLS:
        return "post-write"
    return None


def _detached_agent_id() -> str:
    """The ONE authoritative subagent identity source for opencode sessions.

    The forge-strip in ``to_v1_event`` removes any model/tool-supplied agent_id
    (the same trust boundary as the CC bridge's T2 precedence: an event may carry
    model-influenced fields, so they can never self-exempt). CC has an
    authoritative top-level agent field to restore it from; opencode has NONE —
    which made every opencode session, including a rig-dispatched detached
    ``opencode run`` agent, look like "the orchestrator" to every gate.

    The sanctioned source here is the opencode PROCESS ENVIRONMENT, read at
    launch: ``RIG_AGENT_ID=<name>`` (identity) or ``RIG_DETACHED_AGENT=1``
    (anonymous marker), set by the rig detached-agent launcher. plugin.js spawns
    this dispatcher with ``{...process.env}``, so the marker set when the child
    opencode was launched is visible here on every tool call.

    Trust reasoning: a running orchestrator cannot retroactively mutate its own
    process environment — it can only set these vars for a CHILD process it
    dispatches, which is exactly the sanctioned act of dispatching a subagent
    (and the very act the delegation gates exist to encourage). This matches the
    module family's stated threat model: a cooperative orchestrator, discipline
    rather than a security boundary, on_error=open. A bare/whitespace value is
    not a marker.
    """
    val = os.environ.get("RIG_AGENT_ID", "").strip()
    if val:
        return val
    if os.environ.get("RIG_DETACHED_AGENT", "").strip() == "1":
        return "detached"
    return ""


def to_v1_event(opencode_event: dict, *, point: str) -> dict:
    """Translate the opencode plugin payload into agents-hooks/v1."""
    tool = _tool_name(opencode_event)
    raw_args = _tool_args(opencode_event)
    args = dict(raw_args)
    for key in _FORGED_AGENT_KEYS:
        args.pop(key, None)
    detached_id = _detached_agent_id()
    if detached_id:
        args["agent_id"] = detached_id
    _normalize_task_args(args)
    if tool == _SKILL_TOOL:
        _normalize_skill_args(args)
    # T2: session_id on tool args is model-controlled. skills-marker-writer nests
    # the freshness marker under args.session_id, so a forged value would let one
    # session satisfy another. Drop every session-id spelling from args, then
    # restore only a plugin-payload id (input.sessionID / top-level), never
    # output.args.
    for key in ("session_id", "sessionID", "sessionId"):
        args.pop(key, None)
    trusted_sid = _trusted_session_id(opencode_event)
    if trusted_sid:
        args["session_id"] = trusted_sid

    command = _tool_command(tool, raw_args)
    if command:
        args["command"] = command

    if point in ("pre-write", "post-write"):
        if tool == "apply_patch":
            if command:
                args.setdefault("patch", command)
                args.setdefault("patchText", command)
                paths = _patch_file_paths(command)
                if paths:
                    args.setdefault("file_paths", paths)
                if len(paths) == 1:
                    args.setdefault("file_path", paths[0])
                    args.setdefault("path", paths[0])
                elif _patch_move_target(command):
                    args.setdefault("file_path", paths[-1])
                    args.setdefault("path", paths[-1])
                if point == "pre-write":
                    args["content"] = _patch_added_content(command)
        else:
            path = _write_path(raw_args)
            if path:
                args.setdefault("file_path", path)
                args.setdefault("path", path)
            if point == "pre-write":
                content = _write_content(raw_args)
                if not isinstance(args.get("content"), str):
                    args["content"] = content

    return {
        "hook_api": HOOK_API,
        "event_id": _event_id(opencode_event),
        "tool": tool,
        "point": point,
        "harness": HARNESS,
        "command": command,
        "cwd": _cwd(opencode_event),
        "args": args,
    }


def opencode_block_output(reason: str) -> dict:
    """The plugin consumes this shape and throws an Error with ``reason``."""
    return {"decision": "block", "reason": reason}


def hooks_dir() -> Path:
    """Where opencode-installed v1 descriptors live, overridable for tests."""
    override = os.environ.get("OPENCODE_HOOKS_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.config/opencode/hooks"))


def dispatch(hook_name: str, opencode_event: dict) -> dict | None:
    """Run applicable v1 hooks for this opencode event."""
    tool = _tool_name(opencode_event)
    point = point_for_event(hook_name, tool)
    if point is None:
        return None
    runner = _load_runner()
    if runner is None:
        return None
    load_descriptors, run_hook = runner
    specs = load_descriptors(point, hooks_dir(), warn=_warn)
    if not specs:
        return None
    for spec in specs:
        for v1_event in _v1_events_for_dispatch(opencode_event, point=point):
            outcome, reason = run_hook(spec, v1_event, warn=_warn)
            if outcome == "block":
                return opencode_block_output(reason)
    return None


def _load_runner():
    try:
        from agent_hooks_v1 import load_descriptors, run_hook
    except Exception as exc:  # noqa: BLE001 - bridge-level failures fail open
        _warn(f"could not import shared v1 runner, failing OPEN (allowing the call): {exc}")
        return None
    return load_descriptors, run_hook


def main(argv: list[str]) -> int:
    """CLI entry: ``opencode_hook_bridge <plugin-event>``.

    Top-level dispatcher failures fail open: a broken bridge must not wedge every
    opencode tool call. Deliberate hook blocks still return block JSON on stdout.
    """
    hook_name = argv[0] if argv else "tool.execute.before"
    if hook_name not in _KNOWN_EVENTS:
        _warn(f"unknown event arg {hook_name!r}; stdin's hook field will be used if present")
    try:
        raw = sys.stdin.read()
        opencode_event = json.loads(raw) if raw.strip() else {}
        if not isinstance(opencode_event, dict):
            opencode_event = {}
        hook_name = str(opencode_event.get("hook") or hook_name)
        result = dispatch(hook_name, opencode_event)
    except Exception as exc:  # noqa: BLE001 - fail-open is the bridge contract
        _warn(f"dispatcher error, failing OPEN (allowing the call): {exc}")
        return 0
    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0


def _tool_name(event: dict) -> str:
    inp = event.get("input") if isinstance(event.get("input"), dict) else {}
    tool = inp.get("tool") or event.get("tool")
    return str(tool or "").lower()


def _tool_args(event: dict) -> dict:
    out = event.get("output") if isinstance(event.get("output"), dict) else {}
    args = out.get("args")
    if isinstance(args, dict):
        return dict(args)
    inp = event.get("input") if isinstance(event.get("input"), dict) else {}
    args = inp.get("args")
    return dict(args) if isinstance(args, dict) else {}


def _tool_command(tool: str, args: dict) -> str:
    if tool == "apply_patch":
        for key in ("patchText", "patch", "command"):
            val = args.get(key)
            if isinstance(val, str):
                return val
        return ""
    val = args.get("command")
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return shlex.join(str(part) for part in val)
    return ""


def _cwd(event: dict) -> str:
    for key in ("cwd", "directory", "worktree"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    inp = event.get("input") if isinstance(event.get("input"), dict) else {}
    for key in ("cwd", "directory", "worktree"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            return val
    return os.getcwd()


def _event_id(event: dict) -> str:
    sources = [event]
    for key in ("input", "output"):
        val = event.get(key)
        if isinstance(val, dict):
            sources.append(val)
            nested = val.get("session")
            if isinstance(nested, dict):
                sources.append(nested)
    for source in sources:
        for key in _ID_KEYS:
            val = source.get(key)
            if val:
                return str(val)
    return ""


_TRUTHY_ENV_FLAG = frozenset({"1", "true", "yes", "on"})


def _background_subagents_enabled() -> bool:
    """True when the opencode process hosting this bridge honors background subagents.

    opencode 1.18.20's task tool carries a native ``background`` boolean (verified against
    the v1.18.20 source, ``packages/opencode/src/tool/task.ts``), but it is EXPERIMENTAL:
    the field is only advertised in the model-facing schema — and only honored at execute
    time — when the server runs with ``OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=<truthy>``
    or the broad ``OPENCODE_EXPERIMENTAL=<truthy>`` (``RuntimeFlags.enabledByExperimental``).
    A default build both hides the field and fails a task that sets it anyway ("Background
    subagents require OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true"). plugin.js spawns
    this dispatcher with ``{...process.env}``, so the env of the hosting opencode process
    is visible here. Truthiness accepts Effect ``Config.boolean`` spellings; anything else
    reads as off (the safe direction: an unproven field is not a background signal).
    """
    def on(env: str) -> bool:
        return os.environ.get(env, "").strip().lower() in _TRUTHY_ENV_FLAG

    return on("OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS") or on("OPENCODE_EXPERIMENTAL")


def _trusted_session_id(opencode_event: dict) -> str:
    """Session id from the plugin payload, never from model-controlled tool args."""
    sources = [opencode_event]
    inp = opencode_event.get("input")
    if isinstance(inp, dict):
        sources.append(inp)
        nested = inp.get("session")
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for key in ("sessionID", "sessionId", "session_id"):
            val = source.get(key)
            if val:
                return str(val)
    return ""


def _normalize_skill_args(args: dict) -> None:
    """Map opencode skill-tool ``name`` onto ``args.skill``.

    skills-marker-writer reads ``args.skill`` (the CC Skill ``tool_input`` key).
    OpenCode's native parameter is ``name`` (``skill({ name: "git-release" })``,
    packages/opencode/src/tool/skill.ts). Drop a model-forged ``skill`` key
    first; only a non-empty native ``name`` may set ``args.skill``, so a
    CC-shaped or blank-name call cannot record a skill OpenCode did not load.
    """
    args.pop("skill", None)
    name = args.get("name")
    if isinstance(name, str) and name.strip():
        args["skill"] = name


def _normalize_task_args(args: dict) -> None:
    """Normalize opencode task-tool spellings into the CC-shaped keys the gates read.

    ``background`` -> ``run_in_background`` maps opencode's NATIVE task-tool field — which
    exists only behind the experimental flag (see ``_background_subagents_enabled``). The
    mapping is applied only in that configuration, so a default build's task tool (which
    would reject ``background: true`` with an error) never gets presented to the gates as
    a live background signal it does not actually have.
    """
    if "subagent_type" not in args and isinstance(args.get("subagentType"), str):
        args["subagent_type"] = args["subagentType"]
    if not isinstance(args.get("run_in_background"), bool) and isinstance(args.get("background"), bool):
        if _background_subagents_enabled():
            args["run_in_background"] = args["background"]


def _write_path(args: dict) -> str:
    for key in ("filePath", "file_path", "path"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _write_content(args: dict) -> str:
    parts: list[str] = []
    for key in ("content", "newString", "new_string", "text"):
        val = args.get(key)
        if isinstance(val, str):
            parts.append(val)
    return "\n".join(parts)


def _v1_events_for_dispatch(opencode_event: dict, *, point: str) -> list[dict]:
    base = to_v1_event(opencode_event, point=point)
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
    sys.stderr.write(f"opencode-hook-bridge: {msg}\n")
