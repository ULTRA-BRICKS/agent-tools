"""Role-aware "what to do next" text for blocking guards (agent-tools#994).

Why this exists: on 2026-10-24 a subagent hit `require-ticket-before-commit`, never ran
`task new` (installed, skill present) and yielded "blocked at commit", because the block said
"No self-service bypass: ASK the human" and showed `Closes #123`. The capability was there; the
guard gave no path. A blocking guard must say WHAT TO DO NEXT, and the next step differs by WHO
reads it:

- subagent    -> self-serve the remedy; if impossible, message the coordinator and WAIT; never the human.
- coordinator -> self-serve, or supply the missing thing to the blocked subagent; only the
                 coordinator may escalate to the human.
- unknown     -> neutral text with both branches.

Hooks hand `render()` a `Remedy` (their own wording of what/self_serve/relay/human) plus the
`Role` from `detect_role()`; the ladder shape lives here once. stdlib only; single file, so a hook
can load it by path (no relative imports) exactly like `agenttools_hatch_escalation`.

Trust: identity comes ONLY from the bridge-restored `event["args"]["agent_id"]` and the launcher
process env (`RIG_AGENT_ID`, `RIG_PARENT_AGENT_ID`, `RIG_DETACHED_AGENT`, `PI_SESSION_FILE`).
Everything else in the event (`args.role`, `args.tool_input`, a top-level `agent_id`) is
model-controlled and ignored, so a forged value can neither hide a subagent from its ladder nor
hand a subagent the coordinator's human-escalation line.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = ["Role", "Remedy", "detect_role", "render"]

# omp's root agent id; a depth-1 child's parent. `write agent://Main` reaches the top-level agent.
_ROOT_AGENT_ID = "Main"

# omp Task/eval child transcript: .../<session-id>/<agent-id>.jsonl. A main session file is
# .../<session-id>.jsonl (its parent dir is the project slug, never session-id-shaped).
_SESSION_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-fA-F-]+$")


@dataclass(frozen=True)
class Role:
    kind: str  # "subagent" | "coordinator" | "unknown"
    agent_id: str = ""
    parent_id: str = ""  # best-known coordinator id, "" if unknown
    harness: str = ""


@dataclass(frozen=True)
class Remedy:
    what: str  # first line(s): what is blocked and why, SHORT (the hook keeps its stable marker first)
    self_serve: str  # step 1: concrete copy-pasteable commands, role independent
    relay: str = ""  # coordinator: how to supply the missing thing to a blocked subagent ("" -> generic)
    human: str = ""  # coordinator/unknown last line (hatch env var text); "" = no human escalation line
    last_resort: str = ""  # label before `human`; "" -> generic "Last resort, genuine exceptions only: "
    no_bypass: str = ""  # subagent/unknown no-bypass sentence; "" -> generic (hook names its own env vars)


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _session_file_shape(raw: str) -> tuple[str, str] | None:
    """`("main", "")` for a flat main-session transcript, `("child", <id>)` for a Task/eval child
    transcript at any nesting depth, None when the path is absent or not an omp transcript.

    Path shape only (no filesystem probe), the same trust model as the hook bridges.
    """
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw)
    if path.suffix.lower() != ".jsonl" or not path.stem.strip():
        return None
    if not any(_SESSION_DIR.match(parent.name) for parent in path.parents):
        return "main", ""
    return "child", path.stem.strip()


def _parent_of(agent_id: str, *, omp: bool, detached: bool, override: str) -> str:
    if override:
        return override
    if "." in agent_id:
        # Nested omp children are dot-qualified `Parent.Child`; the parent is the prefix.
        return agent_id.rsplit(".", 1)[0]
    # A depth-1 omp child is dispatched by Main. A detached launcher's parent is not in omp's
    # registry, so claiming Main there would aim `write agent://Main` at the wrong agent.
    return _ROOT_AGENT_ID if omp and not detached else ""


def detect_role(event: Mapping, env: Mapping[str, str] | None = None) -> Role:
    """Who is reading this guard's message. Never raises; any doubt -> kind "unknown"."""
    try:
        return _detect_role(event, os.environ if env is None else env)
    except Exception:  # noqa: BLE001 - guard-message quality must never break the guard itself
        return Role(kind="unknown")


def _detect_role(event: Mapping, env: Mapping[str, str]) -> Role:
    event = event if isinstance(event, Mapping) else {}
    args = event.get("args")
    args = args if isinstance(args, Mapping) else {}
    harness = _str(event.get("harness"))

    shape = _session_file_shape(_str(env.get("PI_SESSION_FILE")))
    session_kind, session_id = shape if shape else ("", "")
    detached = _str(env.get("RIG_DETACHED_AGENT")) == "1"
    override = _str(env.get("RIG_PARENT_AGENT_ID"))
    # The omp-ness of the reader decides whether `write agent://<id>` is a valid channel. A nested
    # omp child whose model backend is Claude Code still reports harness "claude-code", but its
    # PI_SESSION_FILE proves omp orchestrates it (agent-tools#676).
    omp = harness == "omp" or shape is not None

    agent_id = _str(args.get("agent_id")) or _str(env.get("RIG_AGENT_ID"))
    if not agent_id and detached:
        agent_id = "detached"
    if not agent_id and session_kind == "child":
        agent_id = session_id

    if agent_id == _ROOT_AGENT_ID or (not agent_id and session_kind == "main"):
        return Role(kind="coordinator", agent_id=agent_id, harness=harness)
    if agent_id:
        return Role(
            kind="subagent",
            agent_id=agent_id,
            parent_id=_parent_of(agent_id, omp=omp, detached=detached, override=override),
            harness=harness,
        )
    return Role(kind="unknown", harness=harness)


# A subagent cannot block-wait for a message: omp's `wait` only waits on background jobs/services
# the caller started and errors with "Nothing to wait for". The omp protocol is: message the
# coordinator, then `yield` with status `awaiting-coordinator`; the coordinator's
# `write agent://<you>` revives the subagent with its context (live trial, agent-tools#994).
_PAUSE_STATUS = "awaiting-coordinator"
_NO_BYPASS = "As a subagent never use a hatch/env bypass and never go to the human."


def _is_omp_channel(role: Role) -> bool:
    return role.harness == "omp" or bool(role.parent_id)


def _pause_clause(role: Role) -> str:
    """How a blocked subagent reports to its coordinator and pauses, per harness."""
    if _is_omp_channel(role):
        parent = role.parent_id or "<parent-id>"
        you = role.agent_id or "<you>"
        return (
            f"`write agent://{parent}` with what is missing + what you tried, then `yield` status "
            f"`{_PAUSE_STATUS}` (NOT blocked/done/failed). That pause is not terminal: the "
            f"coordinator answers with `write agent://{you}`, which revives you with your context; "
            "then continue the task."
        )
    return (
        "message your dispatcher over the harness's agent-to-agent channel with what is missing + "
        f"what you tried, then end your turn reporting status `{_PAUSE_STATUS}` (NOT "
        "blocked/done/failed); continue when it replies."
    )


def _subagent_step(role: Role, no_bypass: str) -> str:
    return (
        "If you genuinely cannot (tool missing, backend error, wrong repo): "
        f"{_pause_clause(role)} {no_bypass}"
    )


_GENERIC_RELAY = (
    "If a subagent reported this block, supply the missing thing with `write agent://<id>` so "
    "it continues (or do the step inline), then `wait` for its final report."
)
_LAST_RESORT = "Last resort, genuine exceptions only: "


def render(remedy: Remedy, role: Role) -> str:
    """Short, numbered, role-specific escalation ladder. `remedy.what` leads, verbatim."""
    steps: list[str] = [remedy.self_serve.strip()]
    no_bypass = remedy.no_bypass.strip() or _NO_BYPASS
    last_resort = remedy.last_resort or _LAST_RESORT
    if role.kind == "subagent":
        steps.append(_subagent_step(role, no_bypass))
    elif role.kind == "coordinator":
        steps.append(remedy.relay.strip() or _GENERIC_RELAY)
        if remedy.human:
            steps.append(last_resort + remedy.human.strip())
    else:
        steps.append(
            "If you were dispatched by another agent: "
            "`write agent://<parent-id>` with what is missing + what you tried, then `yield` "
            f"status `{_PAUSE_STATUS}` (NOT blocked/done/failed); the coordinator's "
            f"`write agent://<you>` revives you. {no_bypass}"
        )
        relay = remedy.relay.strip() or _GENERIC_RELAY
        top = f"If you are the top-level agent: {relay[0].lower()}{relay[1:]}"
        if remedy.human:
            top += f" {last_resort}{remedy.human.strip()}"
        steps.append(top)
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(steps, start=1))
    return f"{remedy.what.strip()}\n{numbered}"
