"""The agents-hooks/v1 -> omp (oh-my-pi) extension bridge dispatcher.

omp loads Bun-executed TS/JS extension modules that register handlers via
``pi.on(eventName, handler)``. The companion ``extension.ts`` file calls this module
for the ``tool_call`` (pre-execution) and ``tool_result`` (post-execution) events. This
dispatcher maps omp's flat tool-event payload to the shared agents-hooks/v1 contract
and returns a plain ``{"decision":"block"}`` JSON object to the extension.

Unlike the codex/opencode bridges, the extension NEVER throws on a bridge-infrastructure
failure (broken dispatcher, timeout, malformed JSON) — see ``extension.ts`` and its
README section "Fail policy" for why: omp's own contract is that ANY thrown error from a
``tool_call`` handler blocks the call, so a broken bridge would silently brick every tool
call in an interactive omp session. Only an explicit block decision from a hook is
translated into a block; every other failure here (and in the extension) fails open.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import sys
from pathlib import Path

HOOK_API = "agents-hooks/v1"
# The v1 event's `harness` tag for every event this bridge produces — a MODULE LITERAL,
# never derived from `omp_event`, so it cannot be forged via tool args. A hook that wants to
# tailor a message (or otherwise branch) on the source harness reads this tag — see
# `agent-hooks/orchestrator-stays-thin`'s per-harness delegation-recipe text for a consumer
# (agent-tools#573). omp exposes no TRUSTED per-tool-call subagent identity of its OWN in the
# documented `tool_call`/`tool_result` extension event shape (confirmed absent from
# `extension.ts`'s payload: `toolName`/`input`/`cwd`/`toolCallId` only — no session/task-spawn
# marker distinguishing a dispatched child from the top-level session), so a forged
# agent_id/agent_type is stripped below and restored ONLY from the launcher process
# environment or a nested transcript path via `_detached_agent_id` — the same trust boundary
# `opencode_hook_bridge._detached_agent_id` uses (agent-tools#476), ported here unchanged
# (agent-tools#573). The nested path itself reaches this dispatcher from omp's extension ctx:
# `ExtensionContext` carries no top-level `getSessionFile()`/`agentId` (oh-my-pi#11214), so
# `extension.ts` reads `ctx.sessionManager.getSessionFile()` — for a Task/eval child that is
# the CHILD's own session manager and returns the nested transcript
# `.../<session-id>/<ChildName>.jsonl` (agent-tools#743).
HARNESS = "omp"

_KNOWN_EVENTS = frozenset({"tool_call", "tool_result"})
# `eval` (agent-tools#614): an omp eval cell can write a file directly via
# `Path(...).write_text(...)` (Python) or `Bun.write(...)` (JS) inside its `code` string,
# bypassing every path-based pre-write hook (worktree-only-writes, block-secrets-write) the
# same way a raw `edit`/`write` call would land on disk. Treating it as write-shaped means
# `_normalize_write_args` best-effort scrapes a written path/content out of the code text —
# see its `eval` branch and `_eval_write_target` for exactly what is (and is not) recognized.
_WRITE_TOOLS = frozenset({"edit", "write", "apply_patch", "notebook", "eval"})
_TASK_TOOL = "task"
# `hub` (agent-tools#614): `op: "start"` launches `application`+`args` as a long-running
# background process the same way a raw `bash` call launches one, so `no-long-inline-process`
# must see it too — see `_hub_command` for the wire shape and why only `op == "start"`
# synthesizes a command string.
_PRE_BASH_TOOLS = frozenset({"bash", "hub"})

_FORGED_AGENT_KEYS = (
    "agent_id",
    "agent_type",
    "agentID",
    "agentId",
    "agentType",
    "agent",
)

# Session-id spellings a model can put directly in a tool call's `input` dict — kept
# separate from `_FORGED_AGENT_KEYS` (that tuple's name and its one call site are
# agent-id-specific) rather than folded in, matching `opencode_hook_bridge.dispatch`'s own
# separate inline strip for the same reason (agent-tools#743 RC-2 continuation): a raw
# `session_id`/`sessionId`/`sessionID` key surviving `args = dict(raw_args)` into
# `args["session_id"]` unstripped is exactly what let a model rotate
# `orchestrator-stays-thin`'s per-session marker key on every call (warn forever, never
# block) on THIS bridge specifically — `_FORGED_AGENT_KEYS` never covered it, and unlike
# `cc_hook_bridge`/`codex_hook_bridge`/`opencode_hook_bridge`, this bridge never restored a
# trusted value at any point other than `stop`.
_FORGED_SESSION_KEYS = ("session_id", "sessionId", "sessionID")


# omp Task/eval child transcript: .../<session-id>/<agent-id>.jsonl
# (same shape orchestrator-stays-thin._OMP_SESSION_DIR matches; keep in lockstep).
_OMP_SESSION_DIR = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-fA-F-]+$"
)


def _nested_id_from_session_file(raw: str) -> str:
    """Agent id from a nested omp transcript path, or empty. Path shape only."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if path.suffix.lower() != ".jsonl" or not path.stem.strip():
        return ""
    if not _OMP_SESSION_DIR.match(path.parent.name):
        return ""
    return path.stem.strip()


def _omp_nested_agent_id(omp_event: dict | None = None) -> str:
    """Agent id from a nested omp Task/eval transcript, or empty.

    Main:  .../<session-id>.jsonl          (not a child)
    Child: .../<session-id>/<agent>.jsonl  (stem is the worker id)

    Prefer ``omp_event["sessionFile"]`` (extension.ts sources it from
    ``ctx.sessionManager.getSessionFile()`` — omp's ExtensionContext has no top-level
    session getters, oh-my-pi#11214 / agent-tools#743) over process env
    ``PI_SESSION_FILE``. The Node hook process inherits the parent session env,
    so env alone never sees the nested child path.
    """
    if omp_event:
        nested = _nested_id_from_session_file(str(omp_event.get("sessionFile") or ""))
        if nested:
            return nested
    return _nested_id_from_session_file(os.environ.get("PI_SESSION_FILE", ""))


def _detached_agent_id(omp_event: dict | None = None) -> str:
    """Trusted subagent identity for omp sessions (agent-tools#573 / #634).

    The forge-strip above removes any model/tool-supplied agent_id; omp's own `tool_call`/
    `tool_result` extension events carry no session/task-spawn marker to restore it from
    (the event itself is `toolName`/`input`/`cwd`/`toolCallId` only; the payload's
    `sessionFile` is extension.ts-sourced ctx session state, not event data). Identity is
    restored, in order:
      1. process env ``RIG_AGENT_ID=<name>`` (rig-detached-omp launcher)
      2. process env ``RIG_DETACHED_AGENT=1`` (anonymous ``detached``)
      3. a nested transcript path — payload ``sessionFile`` (``ctx.sessionManager``
         of a native omp Task/eval child, agent-tools#743) or process env
         ``PI_SESSION_FILE`` (agent-tools#634)

    ``extension.ts`` spawns this dispatcher with ``{...process.env}`` plus the ctx-derived
    session file, so a marker set when the child was launched (or omp's own nested session
    file) is visible here on every tool call. A running orchestrator cannot retroactively
    mutate its process environment. Cooperative-orchestrator threat model, on_error=open.
    A bare/whitespace value is not a marker.
    """
    val = os.environ.get("RIG_AGENT_ID", "").strip()
    if val:
        return val
    if os.environ.get("RIG_DETACHED_AGENT", "").strip() == "1":
        return "detached"
    return _omp_nested_agent_id(omp_event)


# A hashline section header: `[RELATIVE/PATH#TAG]` where TAG is 4 uppercase hex chars.
_HASHLINE_HEADER = re.compile(r"^\[(?P<path>.+)#(?P<tag>[0-9A-F]{4})\]$")
# Only a `PUT ...:` operation header carries body (`+TEXT`) rows; CUT/REM/MV/register
# pastes never do (docs/tools/edit.md).
_HASHLINE_PUT_WITH_BODY = re.compile(r"^PUT\b.*:\s*$")


def point_for_event(event_name: str, tool_name: str | None) -> str | None:
    """Map an omp extension event/tool pair to a logical v1 point."""
    tool = (tool_name or "").lower()
    if event_name == "tool_call":
        if tool in _PRE_BASH_TOOLS:
            return "pre-bash"
        if tool in _WRITE_TOOLS:
            return "pre-write"
        if tool == _TASK_TOOL:
            return "pre-agent"
        if tool == "yield":
            # Hidden completion tool (docs/tools/task.md). tool_call can return
            # {block:true}; session_shutdown/agent_end cannot. Maps to v1 stop so
            # stop-completion-selfcheck fires before the helper is parked.
            return "stop"
    if event_name == "tool_result" and tool in _WRITE_TOOLS:
        return "post-write"
    return None



def _trusted_session_id(omp_event: dict, detached_id: str | None = None) -> str:
    """Per-session id from harness-trusted sources ONLY — never `args`/raw tool input,
    which a model fully controls. Prefers a detached-launcher identity (pass an
    already-computed one via `detached_id` to avoid re-deriving it; `None` computes it here),
    then the extension's own trusted `sessionId`/`agentId` payload fields
    (`ctx.sessionManager`-derived, see extension.ts's `sessionManagerString`/`ctxString`
    chain), else "" — callers that need a non-empty fallback add their own
    (`_stop_session_id` below falls back to cwd; `to_v1_event` leaves `args["session_id"]`
    unset, which collapses `orchestrator-stays-thin`'s marker key to cwd+point alone, the
    same conservative default an anonymous session already got)."""
    if detached_id is None:
        detached_id = _detached_agent_id(omp_event)
    if detached_id:
        return detached_id
    for key in ("sessionId", "session_id", "agentId", "agent_id"):
        val = omp_event.get(key)
        if val:
            return str(val)
    return ""


def _stop_session_id(omp_event: dict, detached_id: str | None = None) -> str:
    """Stable per-agent id for v1 stop events (yield retries must share one cooldown)."""
    return _trusted_session_id(omp_event, detached_id) or _cwd(omp_event)


def to_v1_event(omp_event: dict, *, point: str) -> dict:
    """Translate the omp extension payload into agents-hooks/v1."""
    tool = _tool_name(omp_event)
    raw_args = _tool_args(omp_event)
    args = dict(raw_args)
    # Task-only, and BEFORE stripping: `agent` is both the source field for `subagent_type`
    # and a forged-identity key that must not survive into `args` itself. Gated on the tool
    # so a bash/write call whose args happen to carry a `task` key never gets a pre-agent
    # field (`run_in_background`) injected into a pre-bash/pre-write event.
    if tool == _TASK_TOOL:
        _normalize_task_args(args)
    for key in _FORGED_AGENT_KEYS:
        args.pop(key, None)
    # T2 hardening (agent-tools#743 RC-2 continuation): a raw `session_id`/`sessionId`/
    # `sessionID` key in the model-controlled tool input must never survive into
    # `args["session_id"]` — `orchestrator-stays-thin` keys its warn->block marker on this
    # field at pre-write/pre-bash, and this bridge previously restored a trusted value only
    # for `stop`, leaving those two points to leak the raw one through unstripped.
    for key in _FORGED_SESSION_KEYS:
        args.pop(key, None)
    detached_id = _detached_agent_id(omp_event)
    ctx_id = str(omp_event.get("agentId") or omp_event.get("agent_id") or "")
    trusted_agent_id = detached_id or ctx_id
    if trusted_agent_id:
        args["agent_id"] = trusted_agent_id

    command = _tool_command(tool, raw_args)
    if command:
        args["command"] = command

    if point in ("pre-write", "post-write"):
        _normalize_write_args(tool, raw_args, args, point=point)

    event_id = _event_id(omp_event)
    if point == "stop":
        # stop-completion-selfcheck keys its cooldown on args.session_id / event_id.
        # omp yield retries are new tool_call ids, so a per-call event_id would block
        # forever. Prefer a trusted agent id, then a top-level omp session/agent field,
        # then cwd — never a value from tool args (forgeable).
        sid = _stop_session_id(omp_event, detached_id)
        args["session_id"] = sid
        event_id = sid
    else:
        # Every OTHER point (pre-write/pre-bash/post-write/pre-agent): restore the same
        # trusted value if one resolves, so `orchestrator-stays-thin`'s marker key is stable
        # per real session there too — never leave the stripped key silently absent while a
        # trusted identity IS available. No trusted value resolves -> stays absent, and the
        # marker key falls back to cwd+point alone, the existing conservative default.
        trusted_sid = _trusted_session_id(omp_event, detached_id)
        if trusted_sid:
            args["session_id"] = trusted_sid

    return {
        "hook_api": HOOK_API,
        "event_id": event_id,
        "tool": tool,
        "point": point,
        "harness": HARNESS,
        "command": command,
        "cwd": _cwd(omp_event),
        "args": args,
    }


def omp_block_output(reason: str) -> dict:
    """The extension consumes this shape and returns ``{block: true, reason}`` to omp."""
    return {"decision": "block", "reason": reason}


def hooks_dir() -> Path:
    """Where omp-installed v1 descriptors live, overridable for tests.

    Precedence: ``OMP_HOOKS_DIR`` (test/session override, highest priority) > the omp agent
    root (``OMP_CODING_AGENT_DIR`` > ``PI_CODING_AGENT_DIR`` full override > ``PI_CONFIG_DIR``
    config-dirname rename > default ``~/.omp/agent``) > ``hooks``.
    """
    override = os.environ.get("OMP_HOOKS_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser(_omp_agent_root())) / "hooks"


def _omp_agent_root() -> str:
    """omp's agent dir (``~``-anchored string), honoring omp's own env overrides.

    Ported from ``riglib.harness_skills.omp_agent_root`` (rig-cli) — this dispatcher
    cannot import rig-cli code, so the precedence logic is duplicated here; keep the two in
    lockstep if omp's env-var contract changes. Two deliberate additions over that port:

    - ``OMP_CODING_AGENT_DIR`` is checked BEFORE ``PI_CODING_AGENT_DIR``, matching this
      repo's own omp resolver (``lib/checker/model_freshness.py::_omp_agent_db``), which
      reads omp's primary OMP-prefixed override first and the legacy PI-prefixed one second.
    - Each override is expanded with ``$VAR`` substitution then ``~`` (rig-cli's
      ``expand_user_path`` = ``expanduser(expandvars(path))``) and, when the EXPANDED value is
      relative, it is the expanded value that gets home-anchored — never the raw override
      string, which would leave a literal ``$VAR`` path segment behind. Either slip makes the
      bridge read a directory that does not exist, load zero descriptors, and silently allow
      everything: rig installs the descriptor dir and extension symlink under the REAL path.
    """
    for var in ("OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR"):
        agent_dir = _expanded_override(var)
        if agent_dir is not None:
            return _home_anchored(agent_dir)
    config_dir = _expanded_override("PI_CONFIG_DIR")
    if config_dir is not None:
        return _home_anchored(config_dir / "agent")
    return "~/.omp/agent"


def _expanded_override(var: str) -> Path | None:
    """The env override ``var`` with ``$VAR`` then ``~`` expanded, or None when unset/blank."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def _home_anchored(p: Path) -> str:
    return str(p) if p.is_absolute() else f"~/{p}"


def _inbox_text(event_name: str, omp_event: dict) -> str:
    """Pending tg-ctl inbox messages for this omp session, consumed and rendered as
    block-reason text (agent-tools#656); "" when nothing / on any failure (fail-open).

    omp has no ``--name``, so the key is ALWAYS the cwd hash — computed directly with
    ``agent_key(None, cwd)``, never via ``agent_key_for_process``: an omp started from a
    Claude-owned shell inherits ``CLAUDE_PID`` and would otherwise key on the Claude
    session's name. Only yield (the v1 ``stop`` point) consumes the inbox.
    """
    if point_for_event(event_name, _tool_name(omp_event)) != "stop":
        return ""
    try:
        from agenttools_tg_inbox import agent_key, stop_inbox_text
    except Exception as exc:  # noqa: BLE001 - optional lib; never block a yield over it
        _warn(f"tg inbox reader unavailable, skipping inbox check: {exc}")
        return ""
    cwd = _cwd(omp_event)
    return stop_inbox_text(
        lambda: agent_key(None, cwd),
        session_id=_stop_session_id(omp_event),
        warn=_warn,
    )


def _combine_block(inbox_text: str, hook_reason: str | None) -> dict | None:
    """One block for BOTH the inbox messages and a blocking hook's reason — the rule is
    `agenttools_tg_inbox.combine_stop_parts`, shared with the Codex bridge (an empty-reason
    block still blocks); without the lib, a hook block passes through unchanged."""
    try:
        from agenttools_tg_inbox import combine_stop_parts
    except Exception:  # noqa: BLE001 - optional lib: no inbox, plain hook semantics
        return None if hook_reason is None else omp_block_output(hook_reason)
    reason = combine_stop_parts(inbox_text, hook_reason)
    return None if reason is None else omp_block_output(reason)


def dispatch(event_name: str, omp_event: dict) -> dict | None:
    """Run applicable v1 hooks for this omp event; on yield→stop, pending tg-ctl inbox
    messages ride in the same block as a blocking hook's reason (the hooks always run,
    and run FIRST — consuming the inbox is irreversible, so it is the last step).

    Zero stop descriptors still take the yield path so a queued inbox can deliver; an
    unmapped event returns None without touching the inbox.
    """
    tool = _tool_name(omp_event)
    point = point_for_event(event_name, tool)
    if point is None:
        return None
    hook_reason = _first_hook_block(event_name, omp_event)
    if point == "stop":
        return _combine_block(_inbox_text(event_name, omp_event), hook_reason)
    return omp_block_output(hook_reason) if hook_reason is not None else None


def _first_hook_block(event_name: str, omp_event: dict) -> str | None:
    """The reason of the FIRST blocking v1 hook for this event, or None.

    Runs EVERY descriptor (and every v1 event fanned out for it) to completion,
    even after one already blocked: a hatch-using hook records its
    `RIG_HATCH_REQUEST_*` attempt to `overrides.log` as a side effect of its own
    subprocess running (`agenttools_hatch_escalation._append_overrides_log`,
    gap G-8). Stopping at the first block would silently skip that audit line
    for every hatch var set on a LATER descriptor in the same invocation
    (agent-tools#713) — the omp-visible decision is unaffected, since only the
    first block's reason is ever returned."""
    tool = _tool_name(omp_event)
    primary_point = point_for_event(event_name, tool)
    if primary_point is None:
        return None
    runner = _load_runner()
    if runner is None:
        return None
    load_descriptors, run_hook = runner
    first_block_reason: str | None = None
    for point in _dispatch_points(event_name, tool, primary_point, _tool_args(omp_event)):
        specs = load_descriptors(point, hooks_dir(), warn=_warn)
        if not specs:
            continue
        v1_events = _v1_events_for_dispatch(omp_event, point=point)
        for spec in specs:
            for v1_event in v1_events:
                outcome, reason = run_hook(spec, v1_event, warn=_warn)
                if outcome == "block" and first_block_reason is None:
                    first_block_reason = reason
    return first_block_reason


def _dispatch_points(
    event_name: str,
    tool: str,
    primary_point: str,
    raw_args: dict,
) -> list[str]:
    """Hook points to run for one omp event, in order.

    `eval` is write-shaped for file I/O (agent-tools#614) but can also launch shell/git
    mutations via Python/JS subprocess helpers inside its `code` string (agent-tools#692).
    When literal shell commands are recognized, run `pre-bash` hooks before `pre-write`.
    """
    if event_name != "tool_call" or tool != "eval":
        return [primary_point]
    points: list[str] = []
    if _eval_shell_commands(raw_args):
        points.append("pre-bash")
    if primary_point == "pre-write":
        points.append("pre-write")
    return points or [primary_point]


def _load_runner():
    try:
        from agent_hooks_v1 import load_descriptors, run_hook
    except Exception as exc:  # noqa: BLE001 - bridge-level failures fail open
        _warn(f"could not import shared v1 runner, failing OPEN (allowing the call): {exc}")
        return None
    return load_descriptors, run_hook


def main(argv: list[str]) -> int:
    """CLI entry: ``omp_hook_bridge <event-name>``.

    Top-level dispatcher failures fail open: a broken bridge must not wedge every omp tool
    call. Deliberate hook blocks still return block JSON on stdout. This is the SAME
    fail-open contract as the codex/opencode dispatchers' ``main`` — the divergence from
    them is entirely in ``extension.ts``, which (unlike opencode's plugin) never turns a
    bridge failure into a throw on the pre-execution path either. See the module and
    extension.ts docstrings for why.
    """
    event_name = argv[0] if argv else "tool_call"
    if event_name not in _KNOWN_EVENTS:
        _warn(f"unknown event arg {event_name!r}; stdin's event field will be used if present")
    try:
        raw = sys.stdin.read()
        omp_event = json.loads(raw) if raw.strip() else {}
        if not isinstance(omp_event, dict):
            omp_event = {}
        event_name = str(omp_event.get("event") or event_name)
        result = dispatch(event_name, omp_event)
    except Exception as exc:  # noqa: BLE001 - fail-open is the bridge contract
        _warn(f"dispatcher error, failing OPEN (allowing the call): {exc}")
        return 0
    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0


def _tool_name(event: dict) -> str:
    return str(event.get("toolName") or "").lower()


def _tool_args(event: dict) -> dict:
    args = event.get("input")
    return dict(args) if isinstance(args, dict) else {}


def _tool_command(tool: str, args: dict) -> str:
    if tool in ("edit", "apply_patch"):
        return _edit_patch_text(tool, args)
    if tool == "hub":
        return _hub_command(args)
    if tool == "eval":
        # An eval call's `command` MUST stay "" unconditionally — never fall through to the
        # generic `args.get("command")` tail below. `block_secrets_write.py`'s path fallback is
        # `args.path or args.file_path or event.command or ""`; eval very often has neither
        # `path` nor `file_path` set (the scrape found nothing, or `_normalize_write_args`
        # deliberately cleared a forged one), so `event.command` is the fallback that actually
        # gets consulted far more often here than for `bash`/`write`. Reading a raw `command`
        # key from `args` would let a forged `input.command: "x.example"` on an eval tool_call
        # slip an unrelated write past `ALLOW_PATH` (a security-review finding on this change);
        # `_normalize_write_args`'s eval branch also pops any such forged key from `args` itself
        # so it can't survive as `args.command` either.
        return ""
    val = args.get("command")
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return " ".join(str(part) for part in val)
    return ""


def _hub_command(args: dict) -> str:
    """Best-effort argv string for a `hub` tool call, so `pre-bash` hooks (e.g.
    `no-long-inline-process`) can reason about a `hub start` the same way they reason about a
    raw `bash` command. omp's `hub` tool argument shape (its own tool schema) is
    `{op, application?, args?, name?, cwd?, env?, ready?, ...}` — there is no single
    `command` string field the way `bash` has one. Only `op == "start"` actually launches a
    NEW long-running process (`application` run with argv `args`); every other op (`send`,
    `wait`, `inbox`, `list`, `jobs`, `cancel`, `ps`, `logs`, `stop`, `restart`, `describe`)
    manipulates or inspects an EXISTING hub-managed process/job and launches nothing new, so
    no command is synthesized for them — an empty string here still lets `point_for_event`
    map the call to `pre-bash` (so a command-keyed hook sees the call), it just has no argv
    to match against, the same as it would for a `bash` call with a blank command.

    Every field read here is normalized/coerced rather than matched by rigid `is`-shape
    checks, matching the `eval` scrape's own fail-CLOSED philosophy elsewhere in this module
    (a security-review finding on agent-tools#614): a case-varied `op` (`"Start"`), a
    non-string `application`, or `args` passed as a bare string instead of a list previously
    made this function silently return `""` — an EMPTY synthesized command that sails straight
    past a command-keyed hook like `no-long-inline-process` the same way an unrecognized eval
    write used to sail past `block-secrets-write`. Widening what's recognized here (case-
    insensitive `op`, a `str()`-coerced `application`, a single-element `args` list from a bare
    string) means a malformed-but-real `hub start` call still produces SOME command text for a
    hook to judge, rather than none.
    """
    op = args.get("op")
    if not (isinstance(op, str) and op.lower() == "start"):
        return ""
    application = args.get("application")
    if isinstance(application, str):
        app = application
    elif application:
        app = str(application)
    else:
        app = ""
    if not app:
        return ""
    extra = args.get("args")
    if isinstance(extra, list):
        parts = [str(part) for part in extra]
    elif extra:
        parts = [str(extra)]
    else:
        parts = []
    return shlex.join([app, *parts])


def _edit_patch_text(tool: str, args: dict) -> str:
    """The raw patch/section text for `edit` (hashline) or `apply_patch` (classic)."""
    if tool == "edit":
        val = args.get("input")
        return val if isinstance(val, str) else ""
    for key in ("patch", "patchText", "input"):
        val = args.get(key)
        if isinstance(val, str):
            return val
    return ""


def _cwd(event: dict) -> str:
    """The v1 event's `cwd` — the directory a cwd-sensitive hook should judge, not
    unconditionally the SESSION's cwd. `hub`'s own `op: "start"` call can launch a process in a
    DIFFERENT `input.cwd` than the session's (`hub`'s own tool schema: "start: cwd defaults to
    the session directory" — i.e. an explicit one overrides it); reporting only the session cwd
    here let a `hub start` whose own `cwd` argument points somewhere else evade any
    cwd-sensitive pre-bash hook entirely (a security-review finding on agent-tools#614). A
    relative `input.cwd` is resolved against the session cwd, matching that documented default
    semantics — the same relative-path convention `worktree_only_writes.py`'s `_target_dir`
    already uses for a write's target path. `os.path.normpath` collapses any `..`/`.` segments
    in the joined result — an un-normalized `session_cwd/../../../tmp/evil` string still starts
    with `session_cwd` textually, which would fool a cwd-sensitive hook that prefix-matches the
    reported cwd instead of resolving it (a review finding on agent-tools#614).

    Gated on `op == "start"` (case-insensitive, matching `_hub_command`'s own gate) — every
    OTHER `hub` op (`send`, `wait`, `list`, `logs`, `describe`, ...) manipulates or inspects an
    EXISTING process and launches nothing new, so it has no "where does this launch" semantics
    to override; without this gate, a non-`start` op carrying an unrelated/forged `cwd` field
    (e.g. `hub list` scoped to a directory) would silently override the reported cwd for a
    call that never launches anything — a review-quorum finding that this function's own
    docstring already promised `op: "start"` scoping but never checked it (agent-tools#614).
    """
    session_val = event.get("cwd")
    session_cwd = session_val if isinstance(session_val, str) and session_val else os.getcwd()
    if _tool_name(event) == "hub":
        hub_args = _tool_args(event)
        op = hub_args.get("op")
        if isinstance(op, str) and op.lower() == "start":
            hub_cwd = hub_args.get("cwd")
            if isinstance(hub_cwd, str) and hub_cwd:
                joined = hub_cwd if os.path.isabs(hub_cwd) else os.path.join(session_cwd, hub_cwd)
                return os.path.normpath(joined)
    return session_cwd


def _event_id(event: dict) -> str:
    val = event.get("toolCallId")
    return str(val) if val else ""


def _normalize_task_args(args: dict) -> None:
    """Normalize omp's `task` tool arguments onto the shared pre-agent contract.

    omp's item shape is `{name?, agent?, task, effort?, outputSchema?, schemaMode?, isolated?}`
    (docs/tools/task.md) — not `subagent_type`/`prompt`/`run_in_background` like CC/opencode —
    and with `task.batch` on (the default) the wire shape is `{context, tasks: item[]}`. The
    pre-agent consumer (`agent-hooks/background-subagent-gate`) reads only the shared keys, so
    without this mapping EVERY omp dispatch looked non-trivial AND foreground and was blocked:

    - `agent` -> `subagent_type` (first item's `agent` for a batch).
    - `task` -> `prompt` (the batch joins every item's `task` with newlines — a multi-item
      batch is genuinely non-trivial and the gate's triviality check treats a newline as such).
    - `run_in_background` -> True. omp exposes NO per-call background lever: a non-blocking
      spawn becomes an `AsyncJobManager` background job by omp's own contract (sync only when
      the SESSION setting `async.enabled` is false or the agent's frontmatter says
      `blocking: true` — neither is a tool argument the model could ever set), so the gate's
      remediation ("dispatch it in the background") is un-followable from a tool call, exactly
      the situation the gate's own docstring already resolves for CC's `fork`/`isolation: remote`
      by trusting the harness contract. This trusts that contract as-is; an `async.enabled=false`
      session is treated the same, acceptable for an on_error=open discipline gate (see README).
    Raw `task`/`name`/`tasks`/`context` fields are kept alongside the normalized copies.
    """
    items = args.get("tasks")
    item_list = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
    if "subagent_type" not in args:
        agent = args.get("agent")
        if not isinstance(agent, str) and item_list:
            agent = item_list[0].get("agent")
        if isinstance(agent, str):
            args["subagent_type"] = agent
    if not isinstance(args.get("prompt"), str):
        texts = [it.get("task") for it in item_list if isinstance(it.get("task"), str)]
        if not texts and isinstance(args.get("task"), str):
            texts = [args["task"]]
        if texts:
            args["prompt"] = "\n".join(texts)
    if not isinstance(args.get("run_in_background"), bool) and (item_list or "task" in args):
        args["run_in_background"] = True


def _normalize_write_args(tool: str, raw_args: dict, args: dict, *, point: str) -> None:
    """Set `args.file_path`/`path`/`file_paths`(/`content`) for a write-shaped tool.

    Derived values OVERWRITE same-named keys copied from the raw tool args (never
    `setdefault`): the parsed patch text is the truth about what gets written, and a stray
    or forged `content: ""` / `file_path` riding along in the raw args must not shadow it —
    the same reason `_FORGED_AGENT_KEYS` is stripped for every tool.
    """
    if tool == "write":
        _set_paths(args, _first_str(raw_args, "path", "filePath", "file_path"))
        if point == "pre-write":
            content = raw_args.get("content")
            args["content"] = content if isinstance(content, str) else ""
        return
    if tool == "edit":
        text = _edit_patch_text(tool, raw_args)
        paths = _hashline_file_paths(text)
        if paths:
            _apply_paths(args, paths)
            if point == "pre-write":
                args["content"] = _hashline_added_content(text)
            return
        _normalize_non_hashline_edit(raw_args, args, point=point)
        return
    if tool == "eval":
        code = _first_str(raw_args, "code", "content")
        # A forged `file_paths`/`command` riding along in `args` (copied from `raw_args` at the
        # top of `to_v1_event`) is dangerous specifically for `eval`, more so than for any other
        # write-shaped tool — see the two comments below and `_tool_command`'s eval branch:
        # neither key is ever legitimately present on omp's real `eval` schema, so either one
        # surviving here can only be an attempt to steer a downstream hook.
        args.pop("file_paths", None)
        args.pop("command", None)
        buckets = _eval_content_buckets(code)
        if len(buckets) > 1:
            # A multi-target eval call (more than one recognized write call resolving to
            # DIFFERENT targets — a real path counts as one target, the path-less bucket for
            # any bare `.write(...)` counts as one more if non-empty) must fan out one event
            # per target, exactly like a multi-section `edit` or multi-file `apply_patch` call.
            # `_v1_events_for_dispatch`'s eval branch fully re-derives and REPLACES this base
            # event with that fan-out (re-scanning `code` itself, not this `args` dict), so
            # what this branch sets below for the multi-target case is never actually seen by a
            # hook — only `file_paths` (the REAL scraped paths, for any other consumer that
            # inspects it) matters here. Two security-review findings on earlier versions both
            # trace back to reporting a SINGLE target for a multi-target call: (1) a harmless
            # first path's `ALLOW_PATH` allowlist verdict governed the WHOLE aggregated body,
            # hiding a later, different path's real secret; (2) fanning out over REAL paths only
            # (ignoring the path-less bucket) silently dropped a bare `.write(...)` secret's
            # content entirely whenever the same eval cell also had 2+ named write calls.
            _apply_paths(args, [path for path in buckets if path])
            if point == "pre-write":
                # Never seen by a hook (replaced by the fan-out above) — cleared rather than
                # left as a stray/forged value copied from `raw_args` regardless, same
                # never-`setdefault` rule as every other branch here.
                args["content"] = ""
        else:
            path = next(iter(buckets), "")
            if path:
                _set_paths(args, path)
            else:
                # No recognized write call found (`buckets` empty), or the only bucket is the
                # path-less one (`path == ""`): an eval call's raw args have no legitimate
                # `file_path`/`path` key (omp's `eval` schema is `{language, code, ...}`), so a
                # `path`/`file_path` surviving from `args = dict(raw_args)` can only be a forged
                # key trying to slip a real write past `worktree-only-writes`/`block-secrets-write`
                # under a fake, ALLOW_PATH-matching label — clear it, the same defense
                # `_apply_paths`'s multi-path branch and every other branch here already apply.
                args.pop("file_path", None)
                args.pop("path", None)
            if point == "pre-write":
                args["content"] = buckets.get(path, "")
        return
    if tool == "apply_patch":
        text = _edit_patch_text(tool, raw_args)
        args["patch"] = text
        args["patchText"] = text
        _apply_paths(args, _patch_file_paths(text), move_target=_patch_move_target(text))
        if point == "pre-write":
            args["content"] = _patch_added_content(text)
        return
    # `notebook`: no confirmed omp tool schema for this today — best-effort passthrough of
    # whatever path-shaped field is present so a path-based hook is not blind to it.
    path = _first_str(raw_args, "path", "notebook_path", "file_path")
    if path:
        args["notebook_path"] = path
        _set_paths(args, path)


# agent-tools#692: an eval cell can mutate git state via subprocess/os.system/Bun.$ the same way
# a raw `bash` call would, bypassing every `pre-bash` hook (`require-review-before-commit`,
# `orchestrator-stays-thin`, …) when the bridge only mapped `eval` to `pre-write`.
# `_eval_shell_commands` is a best-effort literal scrape — same trust model as
# `_eval_content_buckets`: only statically-known argv/string shapes are projected.
_SUBPROCESS_RUN_METHODS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
_EVAL_BUN_SHELL = re.compile(r"(?<![.\w])Bun\.\$\s*`([^`]*)`", re.DOTALL)
_EVAL_DOLLAR_SHELL = re.compile(r"(?<![.\w])\$\s*`([^`]*)`", re.DOTALL)
_EVAL_EXEC_SYNC_OPENER = re.compile(
    r"(?<![.\w])(?:execSync|execFileSync|spawnSync)\s*\(\s*(['\"])",
)


def _eval_shell_commands(raw_args: dict) -> list[str]:
    """Literal shell commands embedded in an eval cell, for `pre-bash` hook projection."""
    code = _first_str(raw_args, "code", "content")
    if not code:
        return []
    language = str(raw_args.get("language") or "").lower()
    if language in ("py", "python", ""):
        return _python_eval_shell_commands(code)
    return _js_eval_shell_commands(code)


def _python_eval_shell_commands(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    commands: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        cmd = _python_call_to_shell_command(node)
        if cmd:
            commands.append(cmd)
    return commands


def _python_call_to_shell_command(node: ast.Call) -> str | None:
    module, attr = _python_call_target(node.func)
    if module == "os" and attr == "system":
        return _literal_string_node(node.args[0]) if node.args else None
    if module == "subprocess" and attr in _SUBPROCESS_RUN_METHODS:
        if not node.args:
            return None
        argv = _literal_list_to_argv(node.args[0])
        if argv is not None:
            return shlex.join(argv)
        shell_cmd = _literal_string_node(node.args[0])
        if shell_cmd is not None and _python_call_uses_shell(node):
            return shell_cmd
    return None


def _python_call_target(func: ast.AST) -> tuple[str | None, str | None]:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _python_call_uses_shell(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _literal_list_to_argv(node: ast.AST) -> list[str] | None:
    if not isinstance(node, ast.List):
        return None
    argv: list[str] = []
    for elt in node.elts:
        val = _literal_string_node(elt)
        if val is None:
            return None
        argv.append(val)
    return argv


def _literal_string_node(node: ast.AST) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _js_quoted_literal(raw: str, start: int, quote: str) -> str | None:
    """Scan a JS single/double-quoted literal from `start`; return content or None if unclosed."""
    i = start
    out: list[str] = []
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= len(raw):
                return None
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == quote:
            return "".join(out)
        out.append(ch)
        i += 1
    return None


def _js_eval_shell_commands(code: str) -> list[str]:
    commands: list[str] = []
    for pattern in (_EVAL_BUN_SHELL, _EVAL_DOLLAR_SHELL):
        for match in pattern.finditer(code):
            body = match.group(1).strip()
            if body:
                commands.append(body)
    for match in _EVAL_EXEC_SYNC_OPENER.finditer(code):
        quote = match.group(1)
        body = _js_quoted_literal(code, match.end(), quote)
        if body is not None and body.strip():
            commands.append(body)
    return commands


# `eval`'s written path/content live inside the `code` string itself (a Python/JS program the
# eval cell executes), not as clean structured args — omp's `eval` tool schema exposes no
# `path`/`content` key for what the code WILL write, only `code` (or, per one observed wire
# variant, `content`) holding the whole program text. `_eval_content_buckets` is a heuristic
# TEXT scrape for the write idioms named in agent-tools#614 (`Path(<literal>).write_text(<literal>)`,
# `Bun.write(<literal>, <literal>)`) plus real-world idioms two rounds of security review of
# earlier versions flagged as an easy bypass otherwise (`Path(<literal>).write_bytes(<literal>)`,
# `open(<literal>, ...).write(<literal>)`, JS `fs.writeFileSync(<literal>, <literal>)`), plus a
# path-less `.write(...)`/`.write_text(...)` fallback for any OTHER receiver so a secret is
# still visible to `block-secrets-write` even when this bridge can't resolve which file it
# targets.
#
# This is explicitly NOT a code interpreter and NOT a full parser:
# - Only a STRING LITERAL argument is recognized (single/double/triple-quoted, with an
#   optional 1-2 letter prefix like `r`/`b`/`f`/`rb`). The literal body is escape-aware
#   (`(?:\\.|(?!<closing-quote>).)*?` — an escaped `\"` no longer truncates a `"`-quoted
#   literal early, the exact bypass a security review of the first version found). A raw
#   prefix's "backslashes aren't escapes" semantics ARE honored for a resolved PATH
#   (`_confident_eval_path`, round 3 finding: decoding a raw path's escapes let a
#   suffix-anchored `ALLOW_PATH` pattern match text the real write never targets) but NOT for
#   CONTENT (`_eval_unescape`/`_eval_unescape_run` still decode a raw content literal's escapes
#   as if it were a normal string) -- a known, accepted imprecision in the safe direction:
#   over-decoding content can only make MORE of it resemble a secret pattern, never hide one,
#   while a PATH's decoded-vs-raw form can flip an allowlist verdict either way. A path or
#   content built from a variable, an f-string/template literal, string concatenation, a
#   function call (`write_text(json.dumps(...))`), or any other expression is still INVISIBLE
#   to it — the same honesty the hashline content-extraction section of README.md gives for
#   `edit`. This is a fundamental, permanent limit of a regex scrape rather than a real
#   interpreter, not a to-do: closing it for one more expression shape does not close it for
#   the next one a model could reach for instead.
# - EVERY match across ALL patterns is aggregated (`finditer`, not `search`), not just the
#   first: a security review found that a harmless decoy write before the real secret write
#   made the decoy's content the ONLY thing scanned, hiding the secret from
#   `block-secrets-write` entirely. `_eval_content_buckets`'s value for a key is every matched
#   literal targeting that key, newline-joined in source order. A call scraping MORE THAN ONE
#   distinct bucket (a real path, or the path-less `""` bucket, each count as one) instead fans
#   out one v1 event per bucket (`_normalize_write_args`'s eval branch,
#   `_v1_events_for_dispatch`'s eval branch) — two further security-review findings were that
#   (1) reporting only the FIRST real path let a harmless `ALLOW_PATH`-allowlisted decoy path
#   decide the allow/block verdict for the WHOLE aggregated body, hiding a real secret under a
#   different, later path entirely, and (2) fanning out over REAL paths only (ignoring the
#   path-less bucket) silently DROPPED a bare `.write(...)` secret's content whenever the same
#   eval cell also had 2+ named write calls. The bare fallback pattern only contributes a match
#   whose start position isn't already inside a more specific pattern's match span, so
#   `Path(p).write_text(c)` is not double-counted via the fallback.
# - A write split across MULTIPLE statements (e.g. `p = Path(x); p.write_bytes(y)`) is not
#   recognized — each pattern above matches ONE call expression, not a value flowing between
#   statements. A content-writing eval cell that matches NOTHING is silently NOT normalized:
#   `content` becomes `""` for `pre-write` (never left as a stray/forged value from `raw_args`
#   — same never-`setdefault` rule this function's docstring states) and `file_path`/`path` are
#   cleared rather than left unset, per the forged-key note on the branch above.
_EVAL_QUOTE = r"'''|\"\"\"|'|\"|`"
_EVAL_PREFIX = r"(?:[rRbBuUfF]{1,2})?"


def _eval_literal(name: str) -> str:
    """A regex fragment matching one Python/JS string literal into named group ``name``, with
    the string's prefix (`r`/`b`/`f`/... or "") captured into ``{name}prefix`` and the
    opening/closing quote captured into ``{name}q`` for the closing backreference. See the
    comment above for exactly what this does and does not recognize.

    The literal body's non-escape alternative is `[^\\\\]` (any char but a backslash), NOT the
    bare `.` an earlier version used. `.` overlaps `\\\\.`'s FIRST character on a lone
    backslash (it can consume that `\\\\` either as the start of an escape pair via the first
    alternative, or by itself via the second), and `(?:\\\\.|.)*?` backtracking over that
    ambiguity on a long run of backslashes before an unterminated string is catastrophic
    (quadratic-plus re-tries per added backslash) -- a security-review finding on
    agent-tools#614: adversarial eval code with many backslashes and no closing quote could
    hang the hook bridge itself. Excluding `\\\\` from the plain alternative removes the
    ambiguity: a backslash can now ONLY be consumed by `\\\\.` (the escape-pair alternative),
    so there is exactly one way to partition any given input, and matching stays linear.
    """
    return (
        rf"(?P<{name}prefix>{_EVAL_PREFIX})(?P<{name}q>{_EVAL_QUOTE})"
        rf"(?P<{name}>(?:\\.|(?!(?P={name}q))[^\\])*?)"
        rf"(?P={name}q)"
    )


# A single string-literal CHUNK with no captures of its own -- the building block for both
# `_eval_literal_run` (a regex FRAGMENT embedded into the write-call patterns below) and
# `_EVAL_LITERAL_CHUNK` (a standalone compiled pattern `_eval_unescape_run` re-walks a matched
# run with). Written out per-quote-family (rather than the single-named-group backreference
# form `_eval_literal` uses) because this fragment is embedded via a REPEATED group
# (`(?:{_EVAL_LITERAL_JOIN}{chunk})*` below) -- Python's `re` rejects a pattern that defines the
# same named group twice, which a backreference-based chunk would need on every repetition.
_EVAL_LITERAL_CHUNK_BODY = (
    r"'''(?:\\.|(?!''')[^\\])*?'''"
    r'|"""(?:\\.|(?!""")[^\\])*?"""'
    r"|'(?:\\.|(?!')[^\\])*?'"
    r'|"(?:\\.|(?!")[^\\])*?"'
    r"|`(?:\\.|(?!`)[^\\])*?`"
)
_EVAL_LITERAL_CHUNK_FRAGMENT = rf"(?:{_EVAL_PREFIX}(?:{_EVAL_LITERAL_CHUNK_BODY}))"
# Two adjacent literal chunks join either via Python/JS `+` (optional surrounding whitespace,
# including newlines) or via Python's bare-adjacency concatenation (`"a" "b"`, whitespace --
# possibly none at all, e.g. `"a""b"` -- between them; JS has no bare-adjacency form, only `+`,
# but tolerating it there too is harmless since a genuine JS write call never happens to place
# two quoted literals back to back any other way).
_EVAL_LITERAL_JOIN = r"(?:\s*\+\s*|\s*)"


def _eval_literal_run(name: str) -> str:
    """Like `_eval_literal`, but captures a RUN of one-or-more adjacent string-literal chunks
    into a single `{name}` group -- Python's bare-adjacency concatenation (`"a" "b"`) and a
    `+`-joined concatenation (`"a" + "b"`, Python or JS) both fold into ONE value at
    parse/runtime, so a content scrape that only read the FIRST chunk missed everything after
    it (agent-tools#614, round 2 review). The captured group still contains each chunk's own
    quotes and the joining `+`/whitespace between them -- `_eval_unescape_run` (not
    `_eval_unescape`) is the correct decoder for it, re-walking the span chunk-by-chunk via
    `_EVAL_LITERAL_CHUNK` rather than treating the whole span as one literal body."""
    return rf"(?P<{name}>{_EVAL_LITERAL_CHUNK_FRAGMENT}(?:{_EVAL_LITERAL_JOIN}{_EVAL_LITERAL_CHUNK_FRAGMENT})*)"


# `data=` keyword recognition (round 2) only tolerated `data=` as the FIRST token after `(` --
# pathlib's real signature (`write_text(data, encoding=None, errors=None, newline=None)`)
# resolves keyword arguments by name, not position, so `write_text(encoding="utf-8",
# data=secret)` is exactly as valid a call as `write_text(data=secret, encoding="utf-8")` (a
# security-review finding, round 3 of agent-tools#614): any OTHER keyword argument appearing
# before `data=` made the whole call fail to match (content silently dropped to `""`) because
# nothing after `(` matched `(?:data\s*=\s*)?` immediately followed by a literal.
# `_EVAL_OTHER_KWARG` skips ONE `name=value` pair that is provably not `data=` (the leading
# negative lookahead), so `_EVAL_DATA_KWARG_PREFIX`'s `(?:_EVAL_OTHER_KWARG)*` skips an
# arbitrary number of them, in any order, before the optional `data=` marker --
# `_EVAL_KWARG_VALUE` only needs to recognize what pathlib's OTHER keyword arguments look like
# (a quoted literal, a bare identifier like `errors=strict`, or a number/`None`/`True`/`False`),
# never the secret-bearing value itself.
# Round-6 security-review finding (agent-tools#614): `_EVAL_KWARG_VALUE` above only recognized
# a quoted literal, a bare identifier, or a number as ANOTHER keyword argument's value sitting
# before `data=` -- a CALL EXPRESSION there (`write_text(encoding=locale.getpreferredencoding(),
# data="API_KEY=...")`) matched none of its alternatives, so `_EVAL_OTHER_KWARG` failed to
# consume that keyword pair at all, which made the whole `_EVAL_DATA_KWARG_PREFIX` fail to
# reach the (correctly-positioned) `data=` marker -- the call went entirely unrecognized
# (content silently dropped, buckets `{}`), contradicting this module's own "`data=` recognized
# regardless of position" claim. The new alternative below recognizes a call expression with up
# to one level of nested parens, tolerating embedded commas/parens inside a quoted argument the
# same way `_EVAL_ARG_SKIP` (see its own comment further down) does -- inlined here rather than
# forward-referenced, since `_EVAL_ARG_SKIP` is defined later in this file. It is listed BEFORE
# the bare-identifier alternative because a call like `foo(...)` also starts like an identifier
# and greedier alternatives should be tried first, though regex alternation order only affects
# which alternative matches first, not whether the whole value is recognized.
_EVAL_KWARG_VALUE = (
    r"(?:'''(?:\\.|(?!''')[^\\])*?'''"
    r'|"""(?:\\.|(?!""")[^\\])*?"""'
    r"|'(?:\\.|(?!')[^\\])*?'"
    r'|"(?:\\.|(?!")[^\\])*?"'
    r"|[A-Za-z_][A-Za-z0-9_.]*\((?:[^()'\"`]|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|\([^()]*\))*\)"
    r"|[A-Za-z_][A-Za-z0-9_.]*"
    r"|[+-]?[0-9]+(?:\.[0-9]+)?)"
)
_EVAL_OTHER_KWARG = rf"(?!data\s*=)[A-Za-z_][A-Za-z0-9_]*\s*=\s*{_EVAL_KWARG_VALUE}\s*,\s*"
_EVAL_DATA_KWARG_PREFIX = rf"(?:{_EVAL_OTHER_KWARG})*(?:data\s*=\s*)?"
_EVAL_PATH_WRITE_TEXT = re.compile(
    # `\b` before `Path\(`: without an identifier boundary, `Path(` also matched mid-identifier
    # inside a decoy receiver like `myPath(...)` (a security-review finding, round 2) -- the
    # match still "succeeded", misrouting a secret write under whatever text followed `Path(`
    # in the decoy call AND suppressing the path-less fallback that would otherwise have caught
    # it as unrecognized. `_EVAL_DATA_KWARG_PREFIX` tolerates pathlib's real
    # `write_text(data=...)` keyword-argument form in ANY keyword position (round 2 found only
    # the first-token case; round 3 found `data=` after another keyword still fell through it).
    r"\bPath\(\s*" + _eval_literal("path") + r"\s*\)\s*\.write_text\(\s*" + _EVAL_DATA_KWARG_PREFIX
    + _eval_literal_run("content"),
    re.DOTALL,
)
_EVAL_PATH_WRITE_BYTES = re.compile(
    r"\bPath\(\s*" + _eval_literal("path") + r"\s*\)\s*\.write_bytes\(\s*" + _EVAL_DATA_KWARG_PREFIX
    + _eval_literal_run("content"),
    re.DOTALL,
)
_EVAL_BUN_WRITE = re.compile(
    r"\bBun\.write\(\s*" + _eval_literal("path") + r"\s*,\s*" + _eval_literal_run("content"),
    re.DOTALL,
)
# The Eval tool's own documented global builtin -- `write(path, content) -> str`, listed as the
# FIRST function in the Eval tool's own prelude and identical for both Python and JS eval cells
# (agent-tools#614, review-quorum finding: two independent review seats corroborated this was a
# TOTAL blind spot -- every pattern above requires a `Path(`/`open(`/`Bun.`/`fs.`-prefixed or a
# dotted `.write(...)`-style call, so a bare `write("/etc/cron.d/job", "...")` call matched
# NOTHING at all, worse than every other known bypass: those still produced some scanned text).
# `(?<![.\w])` (not a plain `\bwrite\(`) is required immediately before `write\(` for two
# reasons at once: it excludes a preceding identifier character the way `\b` alone already would
# (`mywrite(` never matches), AND it excludes a preceding `.` that `\b` alone would NOT exclude
# (a `.`-to-word-character transition IS itself a `\b` boundary) -- without it, an ordinary
# receiver'd `f.write(...)` call (already recognized, correctly, by `_EVAL_BARE_WRITE` below)
# would ALSO match here, reporting its first argument (the mode/data, not a path) as though it
# were this builtin's resolved path. `_EVAL_BUN_WRITE` above never needs this guard because its
# own literal `Bun.` prefix already anchors the match to that one receiver.
_EVAL_BUILTIN_WRITE = re.compile(
    r"(?<![.\w])write\(\s*" + _eval_literal("path") + r"\s*,\s*" + _eval_literal_run("content"),
    re.DOTALL,
)
# Round-5 security-review finding: the permissive `,[^)]*` tail immediately below was ALSO
# permissive enough to swallow a nested CALL EXPRESSION sitting in the mode/tail position
# instead of a plain scalar mode/encoding argument -- `open("foo.example", open("src/secret.py",
# "w").write("API_KEY=..."))` let `[^)]*` consume up to the FIRST unmatched `)` (the inner
# `open(...)` call's own closing paren), so the tail swallowed `open("src/secret.py","w"`
# whole and the match's `.write(` anchor landed on the NESTED call's `.write(`, attributing
# its content to the OUTER call's allowlist-friendly decoy path `foo.example` -- an outright
# allowlist BYPASS (not merely an under-scan): `block-secrets-write` never saw the secret under
# ANY bucket, real or path-less. Round 5's fix re-checked, at every character inside the
# optional tail, that the remaining text does not begin an `identifier(`/`identifier.identifier(`
# call sequence; the moment it does, the negative lookahead fails and the whole optional tail
# group backtracks to empty, which then makes the following mandatory `\)` fail too (a real `,`
# sits there, not `)`) -- so the WHOLE outer match fails at that start position instead of
# silently swallowing the nested call. `re.finditer` then resumes scanning from the next
# character, reaching the nested `open("src/secret.py", "w").write(...)` call and matching IT
# independently, attributing the secret to its own real path -- the same "the whole-string scan
# finds a nested call on its own" recovery every other pattern here already relies on.
#
# Round-6 security-review finding (agent-tools#614): round 5's guard only rejected a tail
# beginning with the SHAPE `identifier(` -- a decoy like `open("foo.example", [open][0]
# ("src/secret.py","w").write("API_KEY=..."))` (subscript-then-call, `[open][0](...)`) begins
# with `[`, not an identifier character, so it didn't match that shape and the tail was NOT
# rejected: the outer match succeeded and the secret was misattributed to the allowlist-
# friendly decoy path `foo.example` again, the identical bypass class via a different call
# shape. The reject condition below is widened from "an identifier immediately followed by
# `(`" to "a `(` or `[` appears here at all" -- this module's documented safe-direction
# doctrine (see above) prefers UNDER-recognizing (falling through to the path-less scan) over
# mis-parsing a nested call/subscript expression it can't fully model. A benign tail like
# `, encoding="utf-8"` still contains neither `(` nor `[`, so it is unaffected; a tail like
# `, errors=get_errors()` or `, mode=MODES[0]` now honestly falls through to the path-less
# fallback instead of a (still correct, just less precise) resolved path -- the same tradeoff
# round 5 already accepted, just drawn around a wider set of shapes.
_EVAL_OPEN_MODE_TAIL = r"(?:,(?:(?!\s*[([])[^)])*)?"
_EVAL_OPEN_WRITE = re.compile(
    # After the path literal, only an OPTIONAL comma-separated tail (mode/encoding args) then
    # the closing paren — NOT arbitrary trailing text. A security-review finding: the earlier
    # `[^)]*\)` was permissive enough to also match a string-CONCATENATED or `os.path.join(...)`
    # path expression whose first operand happens to be a literal (`open("foo.example" +
    # "/../src/secret.py", "w")`), capturing only that first literal as "path" — a real path
    # different from what Python actually opens. Because that match still SUCCEEDS, it gets
    # added to `_eval_matches`'s "covered" spans, which then stops the path-less bare-write
    # fallback from independently scanning the same call's content — so a decoy first literal
    # (`foo.example`, `ALLOW_PATH`-allowlisted) mislabels a real secret write instead of the
    # write going through the path-less bucket (which is unaffected by `ALLOW_PATH`). Requiring
    # `,[^)]*` immediately after the literal (never a bare `[^)]*`) means a concatenated/composed
    # path expression FAILS this pattern entirely — falling through to the path-less bare-write
    # fallback instead, which is the safe direction (unscoped content still gets scanned).
    # `\b` before `open\(` guards the same mid-identifier match a decoy receiver like
    # `myopen(...)` would otherwise cause (round 2 security-review finding, same class as the
    # `\bPath\(` guard above). `_EVAL_OPEN_MODE_TAIL` (round 5, see its own comment above)
    # replaces the bare `(?:,[^)]*)?` to additionally reject a nested call expression hiding in
    # this tail position.
    r"\bopen\(\s*" + _eval_literal("path") + r"\s*" + _EVAL_OPEN_MODE_TAIL + r"\)\s*\.write(?:lines)?\(\s*"
    + _eval_literal_run("content"),
    re.DOTALL,
)
_EVAL_FS_WRITE = re.compile(
    # `\b` precedes the WHOLE optional `(?:fs\.)?` prefix, not just the bare `write`/`append`
    # tail -- a lone `\b` right before `(?:fs\.)?` still checks the boundary at whichever
    # character the alternative actually starts matching from (the `f` of `fs.` when present,
    # the `w`/`a` of `write`/`append` otherwise), so a decoy receiver like `myfs.writeFileSync(`
    # (the `fs.` substring sitting mid-identifier) no longer matches via the `fs.`-anchored
    # branch (round 2 security-review finding, same identifier-boundary class as `Path`/`open`
    # above). The bare `write`/`append`+`File(Sync)?` branch can still match right after the
    # receiver's own `.` (e.g. `myfs.writeFileSync(`, `obj.writeFileSync(`) -- deliberately, the
    # same as any other dotted-method-call recognition (`test_eval_bare_write_recognizes_...`
    # already requires a prefix-less `writeFileSync(` call, with no receiver at all, to match);
    # unlike `Path(`/`open(`, this pattern's path/content always come from the call's OWN two
    # literal arguments, never from surrounding decoy text, so there is no swap/misrouting risk
    # from recognizing it on an arbitrary receiver -- only the safe-direction extra scanning
    # this module's doctrine already accepts elsewhere (the `fs`-suffixed-receiver fix above).
    r"\b(?:fs\.)?(?:write|append)File(?:Sync)?\(\s*" + _eval_literal("path") + r"\s*,\s*"
    + _eval_literal_run("content"),
    re.DOTALL,
)
# `.writelines(...)` takes a LIST of strings, not a single string literal -- the `(?:lines)?`
# suffix on `_EVAL_OPEN_WRITE` above only ever matched a bare `.writelines("...")` call (never
# seen in real code), so a real `open(p, "w").writelines([...])` call fell through this
# pattern entirely and was invisible to every scan, including the path-less fallback (a
# security-review finding on agent-tools#614). This sibling pattern scrapes the RAW text
# between the list's `[` and `]` as "content" rather than parsing individual item literals --
# `block-secrets-write`'s regex scans by substring, so a secret sitting inside any one item's
# quotes is still found in that raw blob the same way it would be found in a single literal.
# Shares `_EVAL_OPEN_MODE_TAIL` (round 5, see `_EVAL_OPEN_WRITE`'s comment above) for its own
# mode/encoding tail -- the identical nested-call-in-the-tail allowlist bypass applies here too
# (a decoy `open("foo.example", open("src/secret.py", "w").writelines([...]))`).
#
# Round-6 security-review finding (agent-tools#614): the earlier `[^\]]*` content scan stopped
# at the FIRST `]`, including one sitting INSIDE a quoted list item's own string body -- a list
# like `.writelines(["]", "API_KEY=..."])` truncated `content` to just the opening quote of the
# first item, silently dropping the real secret in the second item entirely (previously
# documented above as an "accepted imprecision"; this closes it). `_EVAL_WRITELINES_LIST_BODY`
# treats each quoted chunk ATOMICALLY -- a `]` (or any other character) inside a `'...'`,
# `"..."`, or `` `...` `` span no longer ends the scan early, the same quote-aware-scanning
# technique `_eval_literal`'s own body pattern already uses for a single literal.
_EVAL_WRITELINES_LIST_BODY = (
    r"(?:[^\]'\"`]"
    r"|'(?:\\.|[^'\\])*'"
    r'|"(?:\\.|[^"\\])*"'
    r"|`(?:\\.|[^`\\])*`)*"
)
_EVAL_OPEN_WRITELINES_LIST = re.compile(
    r"\bopen\(\s*" + _eval_literal("path") + r"\s*" + _EVAL_OPEN_MODE_TAIL
    + r"\)\s*\.writelines\(\s*\[(?P<content>" + _EVAL_WRITELINES_LIST_BODY + r")\]",
    re.DOTALL,
)
# Path-less fallback: ANY `.write(...)`/`.write_text(...)` call on an unrecognized receiver,
# content only (no `path` group at all — `_eval_content_buckets` files it under the `""`
# key). Deliberately broad: a false positive here means an eval cell's non-file `.write(...)`
# call gets its literal argument scanned as candidate written content, which is the same
# conservative direction `block-secrets-write`'s own `on_error: closed` policy already takes.
# EXCLUDES a receiver that is the WHOLE identifier `Bun` (`(?<!\bBun)`, a fixed-width
# lookbehind whose `\b` zero-width anchor still requires the preceding boundary -- `Bun.write(`
# is excluded but `myBun.write(` is not): `Bun.write(path, content)` is a TWO-argument call
# whose own dedicated pattern above already covers a successful parse -- if its SECOND argument
# fails to parse (e.g. an unsupported quote form) this fallback's `\.write(` would otherwise
# match starting at the SAME call and capture the FIRST argument (the PATH) as "content", a
# genuine path/content SWAP a security review flagged, not merely a missed idiom:
# `block-secrets-write` then scans the harmless path string instead of the real (unparsed)
# secret and allows the write. Excluding this receiver means such a call is honestly
# UNRECOGNIZED (no bucket at all) rather than recognized with swapped data.
#
# There is deliberately NO equivalent `fs` exclusion. `_EVAL_FS_WRITE` above only matches a
# receiver followed by `write`/`append` PLUS a literal `File` suffix (`writeFileSync`,
# `writeFile`, `appendFileSync`, `appendFile`) -- never bare `.write(`/`.write_text(`, which is
# exactly (and only) what this fallback's own `\.write(?:_text)?\(` matches. The two patterns
# can therefore never match the SAME call, so there is no swap risk to guard against for `fs`.
# An earlier version excluded ANY receiver merely ENDING in the two letters `f`/`s` (a bare
# `(?<!fs)` lookbehind with no identifier-boundary check) -- a security-review finding on
# agent-tools#614: that silently hid a genuine `fs.write(<secret>)` call (e.g. a plain Python
# file handle a script happens to name `fs`) and any OTHER receiver merely ending in "fs"
# (`myfs.write(...)`) from every scan, producing an empty content bucket for a real secret
# write. A real multi-argument Node `fs.write(fd, buffer, ...)` call is still unrecognized by
# this fallback (its first argument is a file descriptor/buffer, not a string literal, so
# `_eval_literal` never matches there) -- an honest "no match", not an active exclusion, and
# the safe default per this module's own doctrine: when in doubt, scan more, not less.
# Content is `_eval_literal_run` (round 2 finding) so a `data=`-keyword `.write_text(data="a"
# "b")` call on an unrecognized receiver still concatenates every chunk, not just the first.
_EVAL_BARE_WRITE = re.compile(
    r"(?<!\bBun)\.write(?:_text)?\(\s*" + _EVAL_DATA_KWARG_PREFIX + _eval_literal_run("content"), re.DOTALL
)
# `Bun.write`/`fs.writeFile*`/`fs.appendFile*` (a security-review finding, round 3 of
# agent-tools#614): unlike `Path(...)`/`open(...)`, whose own dedicated patterns above fall
# through to `_EVAL_BARE_WRITE`'s path-less fallback whenever the path argument isn't a
# confidently-resolved literal (round 2's `,[^)]*` design on `_EVAL_OPEN_WRITE`),
# `_EVAL_BUN_WRITE`/`_EVAL_FS_WRITE` require BOTH arguments to be a recognized string literal --
# a computed/concatenated first argument (`Bun.write('a' + 'b', secret)`) makes the WHOLE call
# fail to match, and `_EVAL_BARE_WRITE` deliberately EXCLUDES a `Bun` receiver (see its own
# comment above) and never matches `.writeFile(`/`.appendFile(` at all (only bare
# `.write(`/`.write_text(`) -- so an unresolvable first argument silently dropped the SECOND
# argument's content too, instead of falling back to path-less scanning the way `Path`/`open`
# already do. These two siblings recognize the same call HEAD (receiver + method name) as the
# dedicated patterns above but do not require the first argument to be a literal at all --
# `_EVAL_ARG_SKIP` consumes it as opaque text, tolerating one level of nested parens and
# embedded commas inside a quoted sub-string (so `path.join(a, b)` or a literal `"a,b"` don't
# truncate the skip at the wrong comma), and reports no `path` group at all, filing the match
# under the same path-less (`""`) bucket `_EVAL_BARE_WRITE` already uses. Only a match NOT
# already covered by the dedicated two-literal pattern's span counts (see `_eval_matches`), so a
# normal `Bun.write("a.txt", "x")` call is still reported under its real resolved path, never
# double-counted here too. A first argument containing MORE than one level of nested parens can
# still make `_EVAL_ARG_SKIP` stop at the wrong `)` -- an accepted imprecision in the safe
# direction: the call becomes unrecognized rather than mis-parsed, and an unrecognized call has
# always meant "not scanned" for this heuristic, never "scanned with swapped data".
_EVAL_ARG_SKIP = (
    r"""(?:[^(),'"`]"""
    r'|"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|`(?:\\.|[^`\\])*`"
    r"|\([^()]*\))*"
)
_EVAL_BUN_WRITE_FALLBACK = re.compile(
    r"\bBun\.write\(" + _EVAL_ARG_SKIP + r",\s*" + _eval_literal_run("content"), re.DOTALL
)
_EVAL_FS_WRITE_FALLBACK = re.compile(
    r"\b(?:fs\.)?(?:write|append)File(?:Sync)?\(" + _EVAL_ARG_SKIP + r",\s*" + _eval_literal_run("content"),
    re.DOTALL,
)
# `write(<computed>, <secret>)` (round 5 security-review finding): `_EVAL_BUILTIN_WRITE` above
# requires BOTH arguments to be a recognized string literal, same as `_EVAL_BUN_WRITE`/
# `_EVAL_FS_WRITE` before their own round-3 fallback siblings -- a computed/concatenated first
# argument (`write(some_computed_path_var, "API_KEY=...")`) made the WHOLE call fail to match,
# silently dropping the second argument's content instead of falling back to path-less scanning
# the way `Path(...)`/`open(...)` already do via `_EVAL_BARE_WRITE`. This sibling reuses the same
# `(?<![.\w])write\(` receiver-less/non-dotted guard as `_EVAL_BUILTIN_WRITE` itself (so it never
# matches a dotted `f.write(...)` call, already `_EVAL_BARE_WRITE`'s job, or a decoy `mywrite(`)
# and `_EVAL_ARG_SKIP` to consume the unresolvable first argument as opaque text, filing the
# match under the same path-less (`""`) bucket. Only a match NOT already covered by
# `_EVAL_BUILTIN_WRITE`'s own two-literal span counts (see `_eval_matches`), so a normal
# `write("a.txt", "x")` call is still reported under its real resolved path, never
# double-counted here too.
_EVAL_BUILTIN_WRITE_FALLBACK = re.compile(
    r"(?<![.\w])write\(" + _EVAL_ARG_SKIP + r",\s*" + _eval_literal_run("content"), re.DOTALL
)
_EVAL_PATHLESS_FALLBACK_PATTERNS = (
    _EVAL_BARE_WRITE,
    _EVAL_BUN_WRITE_FALLBACK,
    _EVAL_FS_WRITE_FALLBACK,
    _EVAL_BUILTIN_WRITE_FALLBACK,
)

_EVAL_PATH_PATTERNS = (
    _EVAL_PATH_WRITE_TEXT,
    _EVAL_PATH_WRITE_BYTES,
    _EVAL_BUN_WRITE,
    _EVAL_BUILTIN_WRITE,
    _EVAL_OPEN_WRITE,
    _EVAL_OPEN_WRITELINES_LIST,
    _EVAL_FS_WRITE,
)


def _eval_matches(code: str) -> list[re.Match]:
    """Every recognized write-call match in ``code``, in source order — the ONE scan
    `_eval_content_buckets` re-derives from, so a single-target event, a multi-target fan-out,
    and the real-path list always agree on what was found. See the comment above for exactly
    what is (and is not) recognized.

    Each path-less fallback pattern (`_EVAL_PATHLESS_FALLBACK_PATTERNS`) only contributes a
    match whose start isn't already covered by an EARLIER pattern's span -- `covered`
    accumulates as each fallback is applied, so `_EVAL_BUN_WRITE_FALLBACK`/
    `_EVAL_FS_WRITE_FALLBACK` (round 3) never re-count a call `_EVAL_BARE_WRITE` or a dedicated
    `_EVAL_PATH_PATTERNS` entry already matched, the same non-double-counting guarantee the
    original `_EVAL_BARE_WRITE` check gave."""
    matches = [m for pattern in _EVAL_PATH_PATTERNS for m in pattern.finditer(code)]
    covered = [(m.start(), m.end()) for m in matches]
    for pattern in _EVAL_PATHLESS_FALLBACK_PATTERNS:
        fresh = [m for m in pattern.finditer(code) if not any(s <= m.start() < e for s, e in covered)]
        matches.extend(fresh)
        covered.extend((m.start(), m.end()) for m in fresh)
    matches.sort(key=lambda m: m.start())
    return matches


def _eval_literal_is_interpolated(groups: dict, name: str) -> bool:
    """True when the ``name`` literal `_eval_literal` captured is an f-string/JS template
    literal containing a REAL (non-doubled) interpolation -- an f-string's `{expr}` (with
    `{{`/`}}` as the escaped-literal-brace form) or a template literal's `` ${expr} `` -- so
    its captured text is a TEMPLATE, not the value Python/JS actually produces at runtime.
    `_EVAL_PREFIX` recognizing `f`/`F` was always about tolerating the prefix CHARACTER so the
    quote/body still matches; it never meant an f-string's literal template text is a
    trustworthy resolved value. A security-review finding on agent-tools#614: `Path(f"{p}/x.py")`
    reported the raw text `{p}/x.py` as if it were the real path Python opens."""
    value = groups.get(name)
    if not value:
        return False
    if groups.get(f"{name}q") == "`":
        return "${" in value
    if "f" not in (groups.get(f"{name}prefix") or "").lower():
        return False
    return "{" in value.replace("{{", "").replace("}}", "")


def _confident_eval_path(groups: dict) -> str:
    """The `path` group `_eval_literal` scraped -- DECODED the same way content is (round 2
    security-review finding) -- or `""` when it is an f-string/template literal with a real
    interpolation, or when it is empty.

    A raw, still-escaped path (`Path('fixtures/\\x2e\\x2e/secret.py')`) let a prefix-shaped
    allowlist check see the literal source text `fixtures/\\x2e\\x2e/...`, not the string
    Python actually opens (`fixtures/../secret.py`, after backslash-escape decoding resolves
    `\\x2e\\x2e` to `..`) -- a raw-text comparison could reach a verdict for a path the write
    never targets. Decoding here means every caller of `_confident_eval_path`/
    `_eval_content_buckets` compares against the same escape-decoded text content already does.

    A RAW-STRING path (`r"..."`/`R"..."`, including a combined `rb`/`br`-family prefix) is the
    OPPOSITE case (a security-review finding, round 3): Python's raw-string semantics mean NO
    escape sequence in it is ever interpreted at runtime, so `Path(r"secret\\x2eexample")` opens
    the literal path `secret\\x2eexample` -- decoding it here the same way a normal string is
    decoded would report the DECODED `secret.example` instead, which a suffix-anchored allowlist
    pattern like `.example$` matches even though the real (undecoded) path never would.
    `pathprefix` is `_eval_literal`'s own captured prefix (round 2 already threads it through
    for `_eval_literal_is_interpolated`'s `f`-string check) -- an `r`/`R` anywhere in it means
    the literal's body is returned VERBATIM, never escape-decoded.

    An f-string/template literal with a real interpolation is still reported as `""` (not
    decoded then returned) -- that raw template text is NOT the actual path Python/JS resolves
    at runtime, so treating it as a confident literal path (decoded or not) would let
    fabricated path text decide an `ALLOW_PATH` verdict for a write whose real target is
    unknown. Falling through to the path-less (`""`) bucket is the same 'no confident path'
    treatment a variable-built or concatenated path already gets -- content is still scanned,
    just not tagged with a possibly-wrong path."""
    path = groups.get("path") or ""
    if not path or _eval_literal_is_interpolated(groups, "path"):
        return ""
    if "r" in (groups.get("pathprefix") or "").lower():
        return path
    return _eval_unescape(path)


def _eval_content_buckets(code: str) -> dict[str, str]:
    """Every write call's content in ``code``, aggregated by its resolved path, PLUS a
    ``""``-keyed bucket for every PATH-LESS bare `.write(...)`/`.write_text(...)` match — never
    dropped, in source order, one newline-joined string per key. A security-review finding on
    an earlier version: aggregating path-less content into whichever REAL path happened to be
    scraped first (or fanning out over real paths only, ignoring path-less matches entirely)
    both silently hid a path-less secret — either behind an unrelated (possibly
    `ALLOW_PATH`-allowlisted) path's single verdict, or by dropping it outright when 2+ real
    paths triggered a real-paths-only fan-out. Callers treat EVERY distinct key here (including
    ``""``) as its own target: `_normalize_write_args`'s eval branch and
    `_v1_events_for_dispatch`'s eval branch both fan out over `len(buckets) > 1`, not over the
    real-path count alone — the ``""`` bucket, when it fans out, becomes its own event with NO
    `file_path`/`path` set, so unscoped content is never at the mercy of a DIFFERENT path's
    `ALLOW_PATH` verdict.

    `_EVAL_OPEN_WRITELINES_LIST`'s ``content`` group is the odd one out: a RAW, unvalidated
    blob between a list's `[`/`]` (its own comment explains why), not a `_eval_literal_run`
    match -- re-parsing it chunk-by-chunk via `_eval_unescape_run` can silently DROP text that
    falls between what look like adjacent quoted items but are not really well-formed adjacent
    literals (e.g. a value containing an embedded, unescaped quote character breaks the raw
    blob into chunks that don't cover the whole original text). It keeps the plain whole-blob
    `_eval_unescape` this module used before `_eval_literal_run` existed -- less precise about
    escape boundaries, but never loses a substring the way chunk re-parsing of unvalidated text
    could."""
    content: dict[str, list[str]] = {}
    for m in _eval_matches(code):
        groups = m.groupdict()
        key = _confident_eval_path(groups)
        raw_content = groups.get("content") or ""
        decode = _eval_unescape if m.re is _EVAL_OPEN_WRITELINES_LIST else _eval_unescape_run
        content.setdefault(key, []).append(decode(raw_content))
    return {key: "\n".join(parts) for key, parts in content.items()}


# A single escape TOKEN -- either the JS Unicode CODE-POINT form (`\u{HEX}`, 1-6 hex digits) or
# any other backslash-led escape a matched literal's body may contain (`\n`, `\"`, `\\`, `\xHH`,
# `\uHHHH`, a 1-3 digit octal escape, `\N{NAME}`, or an unrecognized `\X` that decodes to
# itself). `_eval_unescape` decodes ONE token at a time via `re.sub`, never re-scanning a
# token's OWN decoded output for further escapes -- this single-pass design replaces an earlier
# two-pass one (widen `\u{HEX}` to a real character FIRST, then run the WHOLE widened string
# through Python's `unicode_escape` codec) that a security review found could DOUBLE-DECODE:
# widening `\u{5c}` (hex `5c` = backslash) produces a literal backslash character, and when the
# very next source character happened to be, say, `n`, the SUBSEQUENT `unicode_escape` pass saw
# that now-adjacent literal backslash + `n` as a BRAND NEW `\n` escape and turned it into an
# actual newline -- even though the real JS/Python value of `\u{5c}n` is the two literal
# characters backslash-then-`n`, never a newline (agent-tools#614, round 3 finding: a secret
# written via a `\u{HEX}` escape chosen to decode to a backslash could make the SCANNED content
# diverge from the ACTUAL written content, a secret-scan bypass). Because `re.sub` only ever
# matches against the ORIGINAL input text -- never against text a replacement callback just
# returned -- a widened backslash (or any other decoded character) can never be picked back up
# as the start of a second escape sequence here.
#
# Round-6 security-review finding (agent-tools#614): Python's 8-digit code-point escape
# (`\UHHHHHHHH`, distinct from the JS `\u{HEX}` form and the 4-digit `\uHHHH` form above) was
# not recognized as its own token -- it fell through to the generic `\.` catch-all, which
# consumes only the leading `\U` (two characters), leaving the 8 hex digits behind as untouched
# literal text. `Path('src/secret.py').write_text('\U00000041PI_KEY = ...')` then scanned as
# the literal text `\U00000041PI_KEY = ...` instead of decoding to `API_KEY = ...` (`\U00000041`
# is `A`) -- `PI_KEY` alone never matches `block-secrets-write`'s `api[_-]?key` pattern, a
# secret-scan bypass. The new alternative below tokenizes the full 8-digit escape as one unit;
# `_decode_escape_token`'s existing `token.encode("ascii").decode("unicode_escape")` fallback
# already decodes a `\UHHHHHHHH` token correctly once it arrives as one unit, so no change is
# needed there.
_EVAL_ESCAPE_TOKEN = re.compile(
    r"\\u\{[0-9A-Fa-f]{1,6}\}"  # JS code-point escape: `\u{HEX}`
    r"|\\N\{[^}]*\}"  # Python named-character escape: `\N{NAME}`
    r"|\\x[0-9A-Fa-f]{2}"  # `\xHH`
    r"|\\u[0-9A-Fa-f]{4}"  # `\uHHHH`
    r"|\\U[0-9A-Fa-f]{8}"  # `\UHHHHHHHH` (round 6, agent-tools#614)
    r"|\\[0-7]{1,3}"  # a 1-3 digit octal escape
    r"|\\.",  # any other single-character escape, or an unrecognized `\X` (decodes to itself)
    re.DOTALL,
)


def _decode_escape_token(match: re.Match) -> str:
    token = match.group(0)
    if token.startswith("\\u{"):
        try:
            return chr(int(token[3:-1], 16))
        except (ValueError, OverflowError):
            return token
    try:
        return token.encode("ascii").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return token


# A literal chunk with its own named groups (`q`/`body`), standalone-compiled rather than built
# via `_eval_literal`/`_eval_literal_run`'s f-string embedding -- this pattern is re-applied
# independently (via `finditer`) to an ALREADY-MATCHED run span, not embedded inside a larger
# pattern, so reusing the group names `q`/`body` across matches raises no "redefinition"
# conflict the way embedding it into `_eval_literal_run`'s repeated group would.
_EVAL_LITERAL_CHUNK = re.compile(
    r"(?:[rRbBuUfF]{1,2})?(?P<q>'''|\"\"\"|'|\"|`)(?P<body>(?:\\.|(?!(?P=q))[^\\])*?)(?P=q)",
    re.DOTALL,
)


def _eval_unescape_run(run_text: str) -> str:
    """Decode and concatenate every literal chunk `_eval_literal_run` captured -- Python's
    bare-adjacency string concatenation (`"a" "b"`) and `+`-joined concatenation (`"a" + "b"`)
    both fold into ONE value at parse/runtime, so scanning only the FIRST chunk (what a plain
    `_eval_unescape(groups["content"])` call would do on a multi-chunk run) missed every
    subsequent chunk of the actual written content -- a security-review finding, round 2 of
    agent-tools#614. Each chunk is unescaped independently before joining (an escape sequence
    is always fully contained within the ONE literal chunk that has it, by Python/JS syntax, so
    per-chunk decoding cannot mis-split one). Text between chunks (the joining `+`/whitespace,
    and each chunk's own quotes) is walked over, not included -- only the ``body`` group of
    each recognized chunk contributes to the result."""
    return "".join(_eval_unescape(m.group("body")) for m in _EVAL_LITERAL_CHUNK.finditer(run_text))


def _eval_unescape(text: str) -> str:
    """Best-effort DECODE of the escape sequences (`\\n`, `\\"`, `\\\\`, `\\xHH`, `\\uHHHH`,
    `\\u{HHHH}`, ...) a matched literal's body may contain, so the scanned `content` matches
    what actually lands on disk. A security review found that matching being escape-AWARE
    (correctly not truncating early on an embedded `\\"`) without also decoding it left the raw
    escape sequence in `content` — `\\x61bcd1234abcd1234` does not match
    `block-secrets-write`'s `[A-Za-z0-9_\\-+/=]{16,}` value class the way the actual written
    `abcd1234abcd1234` would, silently exempting an encoded secret.

    Decodes via `_EVAL_ESCAPE_TOKEN.sub`, one escape token at a time (see its comment for why a
    single pass over the ORIGINAL text, never re-scanning a token's own decoded output, is what
    closes the round-3 double-decode finding). A non-escape character (including any non-ASCII
    one) between tokens is left untouched by `re.sub` automatically -- no separate ASCII-only
    gate is needed the way the old whole-string/whole-run `unicode_escape` pass required one. A
    malformed/partial escape token (`\\x` with no hex digits following, `\\u{}` with an
    out-of-range code point, ...) falls back to that token's own original text rather than
    raising, and a failure is now localized to the ONE malformed token rather than an entire
    run or the entire string.

    A raw prefix's "backslashes aren't escapes" semantics are still a known, accepted
    imprecision here for CONTENT (see the module comment above) -- `_confident_eval_path`
    handles the PATH side of that distinction separately, since only a mis-decoded PATH can
    flip an `ALLOW_PATH` allowlist verdict.
    """
    return _EVAL_ESCAPE_TOKEN.sub(_decode_escape_token, text)


# Replacement-text keys an `edit` call may carry in its non-hashline `replace` mode (omp's
# `resolveEditMode()` can select `replace`/`patch` per model or via `PI_EDIT_VARIANT`,
# docs/tools/edit.md). The exact wire names are NOT pinned by the docs consulted, so this is
# the same best-effort key family the opencode bridge's `_write_content` accepts, plus the
# camel/snake variants — see README.md "Non-hashline edit modes".
_REPLACE_CONTENT_KEYS = ("newText", "new_text", "newString", "new_string", "replacement", "text")
_UNIFIED_DIFF_KEYS = ("patch", "diff")


def _normalize_non_hashline_edit(raw_args: dict, args: dict, *, point: str) -> None:
    """An `edit` call with no `[PATH#TAG]` section: omp's `replace` / `patch` edit modes.

    Path from the `path` family (or the `+++ b/<path>` header of a unified diff); content
    from the replacement-text family plus the added rows of a unified diff. Without this,
    every non-hashline edit reached content scanners (`block-secrets-write`) with
    `content == ""` and was allowed.
    """
    diff_texts = [raw_args[k] for k in _UNIFIED_DIFF_KEYS if isinstance(raw_args.get(k), str)]
    path = _first_str(raw_args, "path", "filePath", "file_path")
    if not path:
        for text in diff_texts:
            path = _unified_diff_path(text)
            if path:
                break
    _set_paths(args, path)
    if point != "pre-write":
        return
    parts = [raw_args[k] for k in _REPLACE_CONTENT_KEYS if isinstance(raw_args.get(k), str)]
    if isinstance(raw_args.get("content"), str):
        parts.append(raw_args["content"])
    parts.extend(_unified_diff_added_content(text) for text in diff_texts)
    args["content"] = "\n".join(parts)


def _unified_diff_path(diff: str) -> str:
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            return "" if target == "/dev/null" else target
    return ""


def _unified_diff_added_content(diff: str) -> str:
    return "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++ ")
    )


def _first_str(args: dict, *keys: str) -> str:
    for key in keys:
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _set_paths(args: dict, path: str) -> None:
    if path:
        args["file_path"] = path
        args["path"] = path


def _apply_paths(args: dict, paths: list[str], *, move_target: str = "") -> None:
    if not paths:
        return
    args["file_paths"] = paths
    if len(paths) == 1:
        _set_paths(args, paths[0])
    elif move_target:
        _set_paths(args, move_target)
    else:
        # Multi-path, no single target: drop any raw single-path key so a forged `file_path`
        # cannot shadow the per-path fan-out `_v1_events_for_dispatch` performs.
        args.pop("file_path", None)
        args.pop("path", None)


def _hashline_file_paths(patch: str) -> list[str]:
    """Every `[PATH#TAG]` section path in a hashline payload, in order, de-duplicated."""
    paths: list[str] = []
    seen: set[str] = set()
    for line in patch.splitlines():
        m = _HASHLINE_HEADER.match(line.strip())
        if m:
            path = m.group("path")
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _hashline_added_content(patch: str) -> str:
    """Best-effort concatenation of every `+`-prefixed body row across all sections.

    APPROXIMATION, documented in README.md: this does not perfectly reconstruct final
    per-file content when named registers or CUT-then-PUT paste are used across sections
    (a register paste carries no body row at all) — it only recovers the literal body rows
    a `PUT ...:` operation carries inline. Good enough for the consumer hooks' actual need
    (path-based gating), not a faithful hashline interpreter.
    """
    lines: list[str] = []
    in_body = False
    for raw_line in patch.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if _HASHLINE_HEADER.match(stripped) or stripped in ("*** Begin Patch", "*** End Patch"):
            in_body = False
            continue
        if _HASHLINE_PUT_WITH_BODY.match(stripped):
            in_body = True
            continue
        if in_body and line.startswith("+"):
            lines.append(line[1:])
        else:
            in_body = False
    return "\n".join(lines)


def _v1_events_for_dispatch(omp_event: dict, *, point: str) -> list[dict]:
    """Fan out one v1 event per write target for a multi-target write-shaped call.

    Mirrors `opencode_hook_bridge.dispatch._v1_events_for_dispatch`: a path-based hook
    (worktree-only-writes) must see every touched path, not just the first.
    """
    base = to_v1_event(omp_event, point=point)
    args = base.get("args")
    if not isinstance(args, dict):
        return [base]

    if point == "pre-bash" and base.get("tool") == "eval":
        commands = _eval_shell_commands(_tool_args(omp_event))
        if not commands:
            return [base]
        events: list[dict] = []
        for command in commands:
            event = dict(base)
            event_args = dict(args)
            event_args["command"] = command
            event["command"] = command
            event["args"] = event_args
            events.append(event)
        return events

    if point not in ("pre-write", "post-write"):
        return [base]

    if base.get("tool") == "eval":
        # `eval` gets its OWN fan-out, entirely separate from the `file_paths`-driven logic
        # below: its targets can include a PATH-LESS bucket (a bare `.write(...)` this bridge
        # couldn't resolve a path for), which the generic loop below would silently skip (its
        # `if not raw_path: continue` guard exists precisely because every OTHER tool's
        # `file_paths` entries are always real paths). Re-scanning the untouched `code`/`content`
        # raw arg (never `base["command"]`, which `_tool_command`'s eval branch deliberately
        # keeps "" — see that branch's comment) keeps this in lockstep with
        # `_normalize_write_args`'s eval branch, which decided the SAME way whether this call is
        # single- or multi-target.
        buckets = _eval_content_buckets(_first_str(_tool_args(omp_event), "code", "content"))
        if len(buckets) <= 1:
            return [base]
        events: list[dict] = []
        for target in buckets:
            event = dict(base)
            event_args = dict(args)
            event_args.pop("file_paths", None)
            if target:
                event_args["file_path"] = target
                event_args["path"] = target
            else:
                event_args.pop("file_path", None)
                event_args.pop("path", None)
            if point == "pre-write":
                event_args["content"] = buckets[target]
            event["args"] = event_args
            events.append(event)
        return events

    file_paths = args.get("file_paths")
    if not isinstance(file_paths, list) or len(file_paths) <= 1:
        return [base]

    tool = base.get("tool")
    # Re-split the SAME trusted text `_normalize_write_args` derived `file_paths`/`content`
    # from — `base["command"]` is `_edit_patch_text(tool, raw_args)`. Re-picking a raw key here
    # with a different precedence let a stray/forged `patch` key on an `edit` call shadow the
    # real hashline `input`, so every fanned-out per-path `content` came back "" and a
    # secret-scanning pre-write hook saw nothing (review finding on #556).
    text = str(base.get("command") or "")
    content_by_path = (
        _patch_added_content_by_path(text) if tool == "apply_patch" else _hashline_content_by_path(text)
    )

    events: list[dict] = []
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


def _hashline_content_by_path(patch: str) -> dict[str, str]:
    """Per-path best-effort body content for a multi-section hashline payload.

    Same approximation caveats as `_hashline_added_content`; sections sharing a path
    accumulate their body rows in order.
    """
    content: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    in_body = False

    def finish() -> None:
        # ACCUMULATE, never replace: a later same-path section must not erase the rows of an
        # earlier one, or a secret written in section 1 of `a.py` vanishes behind a benign
        # section 3 of `a.py` and a content scanner never sees it.
        if current_path is not None:
            rows = [content[current_path]] if content.get(current_path) else []
            rows.extend(current_lines)
            content[current_path] = "\n".join(rows) if rows else content.get(current_path, "")

    for raw_line in patch.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        m = _HASHLINE_HEADER.match(stripped)
        if m:
            finish()
            current_path = m.group("path")
            current_lines = []
            in_body = False
            continue
        if stripped in ("*** Begin Patch", "*** End Patch"):
            in_body = False
            continue
        if _HASHLINE_PUT_WITH_BODY.match(stripped):
            in_body = True
            continue
        if in_body and line.startswith("+"):
            current_lines.append(line[1:])
        else:
            in_body = False
    finish()
    return content


# --- Classic apply_patch envelope parsing -----------------------------------------------
# Ported from `opencode_hook_bridge.dispatch` (`_patch_file_paths` / `_patch_added_content` /
# `_patch_added_content_by_path` / `_patch_move_target`) rather than re-derived: omp's
# documented `apply_patch` custom-tool wire mode uses the SAME classic `*** Update File: ` /
# `*** Add File: ` / `*** Delete File: ` / `*** Move to: ` envelope family opencode's
# `apply_patch` tool uses. Keep the two copies in lockstep if that envelope format changes;
# see README.md for why this bridge does not simply import the opencode module.


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


def _debug_log_path() -> Path:
    """File log for bridge diagnostics, overridable for tests."""
    override = os.environ.get("HOOKS_DEBUG_LOG") or os.environ.get("OMP_HOOK_BRIDGE_DEBUG_LOG")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.cache/agent-tools/hooks-debug.log"))


def _warn(msg: str) -> None:
    """File-only diagnostics: every harness surfaces dispatcher stderr as TUI
    noise, so the bridge must never write there. The agent receives decisions
    via stdout JSON; everything else lands in the debug file log."""
    try:
        path = _debug_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"omp-hook-bridge: {msg}\n")
    except OSError:
        pass
