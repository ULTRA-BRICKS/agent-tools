"""The agents-hooks/v1 → Claude Code bridge dispatcher.

Reads a Claude Code hook event on stdin, runs the installed ``agents-hooks/v1``
descriptors that apply, and translates the v1 exit-10 BLOCK into CC's own block signal.

Confirmed CC contract (https://code.claude.com/docs/en/hooks, CC 2.1.177):
  - PreToolUse stdin : {tool_name, tool_input{}, cwd, permission_mode, hook_event_name, …};
                       when the action fires INSIDE a dispatched subagent the event ALSO
                       carries {agent_id, agent_type} — the reliable main-vs-subagent signal.
  - PreToolUse block : exit 0 + {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                       "permissionDecision": "deny", "permissionDecisionReason": "..."}}
                       (we use the STRUCTURED JSON form, not exit 2, because it carries a
                       rich reason; exit 2 also blocks but discards any JSON.)
  - PostToolUse      : the tool already ran — cannot block; feedback via
                       {"decision": "block", "reason": "..."} (surfaces the reason to the
                       model, not un-running the tool). Maps to the `post-write` point for
                       the file-edit tools (format-on-write, lint-on-write).
  - Stop stdin       : {hook_event_name: "Stop", session_id, transcript_path, cwd,
                       stop_hook_active, …} — `transcript_path` points at the session's
                       JSONL transcript; forwarded into the v1 event so a `stop` hook can
                       read what actually happened this turn.
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
import sys
from pathlib import Path

HOOK_API = "agents-hooks/v1"
# The v1 event's `harness` tag for every event this bridge produces. A MODULE LITERAL, not
# derived from any field of `cc_event` — so it cannot be forged by a model/tool_input value the
# way `args.harness` could be. A hook that wants to tailor a message (or otherwise branch) on
# one harness reads `event["harness"]`, never `args` — see orchestrator-stays-thin's
# per-harness delegation-recipe text for a consumer (agent-tools#573).
HARNESS = "claude-code"
# CC events this bridge knows how to register/handle (typo guard in main()).
_KNOWN_EVENTS = frozenset({"PreToolUse", "Stop", "PostToolUse"})

# A CC tool name → the logical agents-hooks/v1 point it maps to. Only tools that have a
# v1 point are dispatched; everything else is a clean no-op (the tool proceeds normally).
# pre-write covers every file-mutating tool CC exposes. EXTENSIBILITY: a new CC file-edit
# tool must be added here AND its payload field taught to `_proposed_write_text`, or its
# content won't be scanned by the pre-write guards. Keep this set in sync with the README's
# point table and the rig-cli matcher `Edit|Write|MultiEdit|NotebookEdit`.
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
# The subagent-dispatch tools CC exposes. CC's prompt calls them "Agent/Task"; we match BOTH
# for forward-compat. A PreToolUse on either maps to the `pre-agent` point, the gate that
# governs HOW the orchestrator fans work out (background vs. foreground, trivial vs. not).
# NOTE (rig-cli follow-up, out of scope here): for CC to actually FIRE pre-agent the
# settings.json matcher must be wired in rig-cli's `hook_bridge_entries` — an `Agent|Task`
# PreToolUse matcher carrying `cc_hook_bridge PreToolUse`. That is a separate repo; this
# bridge half (the point mapping + agent_id forwarding) lives here.
_AGENT_TOOLS = frozenset({"Agent", "Task"})
# CC's Skill tool (invoking a named skill). A PreToolUse on it maps to `pre-skill`, the point
# a skill-invocation marker writer uses to satisfy skills-read-gate's freshness check (that
# gate's marker contract — agent-hooks/skills-read-gate/README.md — needs SOMETHING to touch
# `~/.cache/agent-tools/skills-invoked/<session-id>/<skill-name>` (session-scoped, using the
# same T2-hardened `session_id` this file forwards below) on every real invocation; before
# this, nothing did, so the gate could never leave its WARN-forever tier). Same rig-cli follow-up
# note as `_AGENT_TOOLS` above applies: this half only maps the point, rig-cli's
# `hook_bridge_entries` must register a `Skill` PreToolUse matcher for it to actually fire.
_SKILL_TOOLS = frozenset({"Skill"})
# CC's Monitor tool (a background event-stream watch: `tail -f`, a poll loop, a websocket —
# started, then the caller keeps working and gets a notification per output line/event). A
# PreToolUse on it maps to `pre-monitor`, the point subagent-no-monitor uses to block a
# SUBAGENT from ever calling it: Monitor is categorically fire-and-forget (that is the whole
# point of the tool), and a dispatched subagent is NOT re-invoked by a MONITOR-EVENT
# notification — only the main loop is (Monitor has no harness-tracked child at all, unlike a
# Bash `run_in_background: true` child, which the harness DOES use to re-invoke its calling
# subagent — verified empirically; see subagent_no_monitor.py's docstring). Hit in HYP-1350's
# retrospective: a subagent called Monitor on its own spawned child process, then ended its
# turn awaiting a notification that only the top-level orchestrator ever receives. NOTE: the
# stated rationale of the sibling `subagent-no-bg-longproc` hook/AGENTS.md entry ("a subagent
# is never re-invoked by a background-completion notification") is broader than what this
# comment claims and does not hold for an ordinary backgrounded Bash command — tracked
# separately as agent-tools#546, out of scope for this mapping. The orchestrator's own Monitor
# use (watching a backgrounded subagent) is legitimate and unaffected — this point only
# governs subagent tool calls. Same two-repo split as `_AGENT_TOOLS`/`_SKILL_TOOLS` above:
# this half only maps the point, and rig-cli's `hook_bridge_entries` registers the `Monitor`
# PreToolUse matcher that makes it actually fire (rig-cli#296, shipped) — a given machine
# needs `rig apply` to have run after both merged, plus a fresh CC session (hook config is
# read at session start, not live), and the bridge itself enabled at all (see this module's
# README's Installation section) for the block to actually take effect.
_MONITOR_TOOLS = frozenset({"Monitor"})
# CC's Workflow tool (ultracode / dynamic multi-agent scripts). A PreToolUse on it
# maps to `pre-workflow`, the point workflow-danger-gate uses to inspect the script
# BEFORE it runs. Workflow is auto-allowed at the harness permissions layer so
# ultracode is not prompted on every spawn; this point is the script-level backstop
# for destructive commands. Same two-repo split as pre-skill: the mapping here is
# inert in a live CC session until rig-cli registers a `Workflow` PreToolUse matcher.
_WORKFLOW_TOOLS = frozenset({"Workflow"})


def point_for_event(hook_event_name: str, tool_name: str | None) -> str | None:
    """Map a CC (event, tool) pair to a logical v1 point, or None if nothing applies."""
    if hook_event_name == "Stop":
        return "stop"
    if hook_event_name == "PreToolUse":
        if tool_name == "Bash":
            return "pre-bash"
        if tool_name in _WRITE_TOOLS:
            return "pre-write"
        if tool_name in _AGENT_TOOLS:
            return "pre-agent"
        if tool_name in _SKILL_TOOLS:
            return "pre-skill"
        if tool_name in _MONITOR_TOOLS:
            return "pre-monitor"
        if tool_name in _WORKFLOW_TOOLS:
            return "pre-workflow"
    if hook_event_name == "PostToolUse" and tool_name in _WRITE_TOOLS:
        # The write already landed on disk → the REACTIVE point (format-on-write,
        # lint-on-write). A post-write hook's exit-10 is FEEDBACK to the model, not
        # prevention — the tool already ran (see cc_block_output).
        return "post-write"
    return None


def to_v1_event(cc_event: dict, *, point: str) -> dict:
    """Translate a CC hook event into the agents-hooks/v1 event a hook script reads.

    ``args`` carries the action payload the v1 hooks look for: ``args.command`` for a bash
    command, ``args.file_path``/``args.content`` for a write, ``args.session_id`` (CC's own
    session id, at every point, not just stop — see the T2 precedence note below),
    ``args.run_in_background``/``args.prompt`` for a pre-agent dispatch, and (when CC fires
    inside a subagent) ``args.agent_id``/``args.agent_type``. We pass the WHOLE ``tool_input``
    through under ``args`` so a hook can read any field, and additionally surface ``command``
    at the top level (a couple of v1 hooks read it there as a fallback).

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
    if point in ("pre-write", "post-write"):
        # Normalize the target PATH too: NotebookEdit carries `notebook_path`, not the
        # `file_path` the shipped pre-write hooks scope on. Without this a NotebookEdit
        # presents an empty path, so path-scoped raw-env checks and secret-scan allowlists
        # wave the write through — the same bypass the content-normalization above prevents.
        # post-write hooks (format-on-write, lint-on-write) resolve the written file from
        # the same fields, so they get the identical normalization; there is no proposed
        # CONTENT to normalize post-write — the content is already on disk.
        path = _proposed_write_path(tool_input)
        if path:
            args.setdefault("file_path", path)
            args.setdefault("path", path)
    # Forward the subagent signal: when CC fires this event INSIDE a dispatched subagent it
    # carries TOP-LEVEL {agent_id, agent_type}. Surfacing them under `args` is what lets a
    # subagent-exempt gate (background-subagent-gate, orchestrator-stays-thin,
    # no-long-inline-process) tell a subagent's own tool use apart from the main thread's via
    # `_is_subagent`. (The Agent/Task tool_input — incl. `run_in_background` — is already in
    # `args` from `dict(tool_input)`.)
    #
    # `session_id` gets the SAME treatment, for the SAME reason. It used to be forwarded with a
    # weaker "add only if `args` doesn't already have one" rule (`stop` has no `tool_input`, so
    # `args` starts empty there and the weak rule was harmless in that one context). That rule is
    # NOT safe for any point where `args = dict(tool_input)` is non-empty (pre-skill, pre-bash,
    # ...): a `session_id` key riding in via `tool_input` would win over CC's own value, because
    # "already present" was true before we ever looked at `cc_event`. That is fine for hooks that
    # only use `args.session_id` for BOOKKEEPING (model-error-fallback, stop-completion-selfcheck
    # — both fire on `stop`, where this was never reachable anyway), but a hook that uses
    # `args.session_id` to SCOPE a gating decision (skills-read-gate keys its freshness marker on
    # it) must not trust a value the model can set on its own tool call — a spoofed `session_id`
    # would let one session claim another session's fresh marker on purpose, which is worse than
    # the cross-session leak it exists to close.
    #
    # PRECEDENCE (T2): CC's TOP-LEVEL agent_id/agent_type/session_id are the ONLY authoritative
    # source. A value sitting in tool_input is attacker/prompt-controllable — a forged
    # `tool_input.agent_id` (or `.session_id`) must NOT exempt a main-thread dispatch or borrow
    # another session's identity. So: if the signal is present at the top level, it OVERWRITES
    # whatever was in args; if it is ABSENT at the top level, we DROP any copy that rode in via
    # tool_input. The exemption/identity can only come from CC itself.
    for key in ("agent_id", "agent_type", "session_id"):
        if cc_event.get(key) is not None:
            args[key] = cc_event[key]
        else:
            args.pop(key, None)
    return {
        "hook_api": HOOK_API,
        "event_id": cc_event.get("session_id", ""),
        "tool": cc_event.get("tool_name"),
        "point": point,
        "harness": HARNESS,
        "command": tool_input.get("command", ""),
        "cwd": cc_event.get("cwd", os.getcwd()),
        # CC's Stop payload carries this pointing at the session's own JSONL transcript
        # (https://code.claude.com/docs/en/hooks). Forwarded unconditionally (empty string
        # when absent) so the `stop` point's hooks — currently only
        # stop-completion-selfcheck — can read what actually happened this turn instead of
        # firing the same static text regardless of content. Not part of the T2 identity
        # loop above: it isn't attacker-controlled (no tool_input carries it for `stop`,
        # which has no tool_input at all) and it isn't used to scope a gating DECISION, only
        # to pick which prompt text to show.
        "transcript_path": cc_event.get("transcript_path", ""),
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
    """The CC block JSON for an event — PreToolUse uses permissionDecision, Stop uses decision.

    PostToolUse uses Stop's ``{"decision": "block", "reason": …}`` shape: the tool already
    ran, so CC treats it as FEEDBACK — the reason is surfaced to the model (which is exactly
    what a post-write linter wants) rather than denying an action that already happened.
    A ``permissionDecision`` on PostToolUse would be silently ignored by CC.
    """
    if hook_event_name in ("Stop", "PostToolUse"):
        return {"decision": "block", "reason": reason}
    # PreToolUse (and any future pre-action event) → structured permission deny.
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


def _warn(msg: str) -> None:
    """Human log to stderr — CC shows the first stderr line as a hook notice; never parsed."""
    sys.stderr.write(f"cc-hook-bridge: {msg}\n")


def _inbox_text(hook_event_name: str, cc_event: dict) -> str:
    """The tg-ctl Stop-hook inbox (agent-tools#526 / tg-cli#306): the pending Telegram
    messages for THIS agent, consumed (archived, at-most-once) and rendered as block-
    reason text, so CC feeds them to the agent as its next instruction — the tmux-free
    delivery channel for a session tg-ctl cannot reach with send-keys. "" when there is
    nothing: empty / missing / malformed inbox, the reader lib absent, or any failure
    locating the agent (never blocks on its own; one stderr line).
    """
    if hook_event_name != "Stop":
        return ""
    try:
        from agenttools_tg_inbox import agent_key_for_process, stop_inbox_text
    except Exception as exc:  # noqa: BLE001 - optional lib; its absence must not block a stop
        _warn(f"tg inbox reader unavailable, skipping inbox check: {exc}")
        return ""
    cwd = cc_event.get("cwd") or os.getcwd()
    session_id = str(cc_event.get("session_id") or "")
    return stop_inbox_text(lambda: agent_key_for_process(cwd), session_id=session_id, warn=_warn)


def _combine_block(hook_event_name: str, inbox_text: str, hook_reason: str | None) -> dict | None:
    """One block carrying BOTH the inbox messages and a blocking stop hook's reason.

    The v1 `stop` descriptors always run (a fail-closed gate there must never be starved
    by a busy inbox); when one blocks, its reason follows the human's messages in the
    same block, so neither is deferred a turn. The rule itself (incl. "an empty-reason
    block still blocks") is `agenttools_tg_inbox.combine_stop_parts`, shared with the
    Codex bridge; without the lib, a hook block passes through unchanged.
    """
    try:
        from agenttools_tg_inbox import combine_stop_parts
    except Exception:  # noqa: BLE001 - optional lib: no inbox, plain hook semantics
        return None if hook_reason is None else cc_block_output(hook_event_name, hook_reason)
    reason = combine_stop_parts(inbox_text, hook_reason)
    return None if reason is None else cc_block_output(hook_event_name, reason)


def dispatch(hook_event_name: str, cc_event: dict) -> dict | None:
    """Run the applicable v1 hooks for this CC event; return the CC block JSON or None.

    None = no hook blocked → emit nothing (the tool proceeds through CC's normal flow).
    First block wins; its reason is surfaced to the model. Errors here are caught by the
    caller and turned into a fail-OPEN allow. On Stop, pending tg-ctl inbox messages are
    delivered in the same block (see `_inbox_text` / `_combine_block`) — consumed LAST,
    after the hooks ran: consuming is irreversible, and a hook that raises would
    otherwise have eaten a message the agent never saw.
    """
    hook_reason = _first_hook_block(hook_event_name, cc_event)
    return _combine_block(hook_event_name, _inbox_text(hook_event_name, cc_event), hook_reason)


def _first_hook_block(hook_event_name: str, cc_event: dict) -> str | None:
    """The reason of the first blocking v1 hook for this event, or None."""
    tool_name = cc_event.get("tool_name")
    point = point_for_event(hook_event_name, tool_name)
    if point is None:
        return None  # nothing maps to this (event, tool) → no-op
    runner = _load_runner()
    if runner is None:
        return None
    load_descriptors, run_hook = runner
    specs = load_descriptors(point, hooks_dir(), warn=_warn)
    if not specs:
        return None
    v1_event = to_v1_event(cc_event, point=point)
    for spec in specs:
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
