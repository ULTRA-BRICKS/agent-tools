"""The ONE place agent-tools decides "is this agent run a subagent?" (stdlib-only, pure).

Role model (coordinator vs subagent): a run is a SUBAGENT iff a TRUSTED subagent signal is
present; everything else is a coordinator. A signal is trusted only when the harness or the
launcher wrote it and the model cannot forge it: a hook-payload TOP-LEVEL field, a harness
plugin/extension context value, harness-managed env, or launcher env set on the child at
exec. Anything inside ``tool_input``/tool args is model-controlled and never a signal.

The four hook bridges (``cc_hook_bridge``, ``codex_hook_bridge``, ``omp_hook_bridge``,
``opencode_hook_bridge``) each turn their harness' native signal into ``args.agent_id`` using
the primitives below; every agent-hook then asks :func:`is_subagent` instead of carrying its
own copy of the env / ``PI_SESSION_FILE`` logic.

Every function is pure: the environment is an explicit ``environ`` argument that defaults to
``os.environ``, so callers and tests never need to monkeypatch global state.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

# Label for an anonymous detached child (``RIG_DETACHED_AGENT=1`` carries no name).
DETACHED_LABEL = "detached"

# Prefix of the synthetic id :func:`omp_nested_agent_id` builds from ``isMainSession is False``
# — one source of truth so producer and any consumer/test never drift on the format.
NESTED_SESSION_MARKER_PREFIX = "omp-nested:"

# omp Task/eval child transcript: ``.../<session-id>/<agent-id>.jsonl`` where the session-id
# directory is ``<ISO-ish timestamp>_<uuid>``. A MAIN session file is ``.../<session-id>.jsonl``
# (its parent is the project slug, never session-id-shaped).
_OMP_SESSION_DIR = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-fA-F-]+$"
)


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def launcher_agent_id(environ: Mapping[str, str] | None = None) -> str:
    """The launcher identity of a DETACHED child, from its process environment, or ``""``.

    ``RIG_AGENT_ID=<name>`` (stripped, non-empty) is the identity; else the exact anonymous
    marker ``RIG_DETACHED_AGENT=1`` yields :data:`DETACHED_LABEL`. A bare/whitespace value is
    not a marker.

    Trust (agent-tools#476/#573/#831): a running agent cannot retroactively mutate its OWN
    process environment — it can only set these vars for a CHILD process it dispatches, which
    is exactly the sanctioned act of dispatching a subagent. Cooperative-orchestrator threat
    model, ``on_error=open``: discipline, not a security boundary. omp additionally ``unset``s
    both on its MAIN session's bash, so an inherited marker cannot leak into a coordinator.
    """
    env = _env(environ)
    val = (env.get("RIG_AGENT_ID") or "").strip()
    if val:
        return val
    if (env.get("RIG_DETACHED_AGENT") or "").strip() == "1":
        return DETACHED_LABEL
    return ""


def native_payload_agent(payload: Mapping[str, object] | None) -> tuple[str, str]:
    """``(agent_id, agent_type)`` from the TOP LEVEL of a raw Claude Code / Codex hook payload.

    Both harnesses write ``agent_id``/``agent_type`` at the top level of the hook payload ONLY
    when the tool call fires inside a dispatched subagent (Claude Code Task/Agent; Codex
    ``spawn_agent`` thread) and omit them on the coordinator thread. ``tool_input`` is never
    consulted — it is model-controlled. ``("", "")`` when absent or blank.
    """
    if not isinstance(payload, Mapping):
        return "", ""
    agent_id = payload.get("agent_id")
    agent_type = payload.get("agent_type")
    return (
        "" if agent_id is None else str(agent_id).strip(),
        "" if agent_type is None else str(agent_type).strip(),
    )


def has_omp_session_ancestor(path: Path) -> bool:
    """True when ANY ancestor directory of ``path`` is session-id-shaped, not only the parent.

    A depth-1 Task/eval child's transcript is ``.../<session-id>/<agent-id>.jsonl`` and its
    immediate parent matches directly. A NESTED child — dispatched via ``task`` from WITHIN an
    already-dispatched subagent, depth >= 2 — nests one more directory per level (oh-my-pi
    derives each level's ``artifactsDir`` from THAT session's own session file), so its
    immediate parent is another AGENT id, never the root session id. Walking the full
    ancestor chain finds the root session-id directory at any depth. A bare main session
    file's ancestors are the project-slug / ``sessions`` tree, never session-id-shaped, so a
    main session stays ``False``.
    """
    return any(_OMP_SESSION_DIR.match(parent.name) for parent in path.parents)


def nested_id_from_session_file(raw: str) -> str:
    """Agent id (the file stem) of a nested omp transcript path, or ``""``. Path shape only —
    no filesystem probe, so callers and tests stay hermetic."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if path.suffix.lower() != ".jsonl" or not path.stem.strip():
        return ""
    if not has_omp_session_ancestor(path):
        return ""
    return path.stem.strip()


def omp_nested_agent_id(
    omp_event: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Agent id from a nested omp Task/eval transcript, or ``""``.

    Main:  ``.../<session-id>.jsonl``            (not a child)
    Child: ``.../<session-id>/<agent>.jsonl``    (stem is the worker id)

    Prefers ``omp_event["sessionFile"]`` (the omp extension sources it from
    ``ctx.sessionManager.getSessionFile()`` — omp's ExtensionContext has no top-level session
    getters, oh-my-pi#11214 / agent-tools#743) over process env ``PI_SESSION_FILE``: the Node
    hook process inherits the PARENT session's env, so env alone never sees the nested child
    path. Either source, once PRESENT, is authoritative on its own path shape — a present-but
    flat (main-shaped) file is real evidence this is NOT a child and short-circuits without
    falling through further.

    FALLBACK: a genuinely nested child's OWN session can still be in-memory — no session file
    at all, not even a main-shaped one — for its first few tool calls. Only in that
    no-file-evidence-at-all state does ``omp_event["isMainSession"] is False`` synthesize a
    marker id (:data:`NESTED_SESSION_MARKER_PREFIX` + the extension-supplied ``sessionId``,
    never model/tool-controlled input). An event with no ``isMainSession`` key falls through
    unchanged.
    """
    event = omp_event or {}
    session_file = str(event.get("sessionFile") or "").strip()
    if session_file:
        return nested_id_from_session_file(session_file)
    env_file = (_env(environ).get("PI_SESSION_FILE") or "").strip()
    if env_file:
        return nested_id_from_session_file(env_file)
    if event.get("isMainSession") is False:
        session_id = str(event.get("sessionId") or "").strip()
        if session_id:
            return f"{NESTED_SESSION_MARKER_PREFIX}{session_id}"
    return ""


def opencode_child_agent_id(session_id: object, parent_id: object) -> str:
    """The subagent id of an opencode session: the session id itself when the session is a
    CHILD (``session.parentID`` non-blank), else ``""``.

    opencode's ``task`` subagent runs as a child session of the coordinator's session. The
    parent id must come from the plugin's own lookup of the session (harness data), never
    from tool args.
    """
    if not str(parent_id or "").strip():
        return ""
    return str(session_id or "").strip()


def is_subagent(args: Mapping[str, object] | None, environ: Mapping[str, str] | None = None) -> bool:
    """The predicate agent-hooks ask: is this tool call fired by a subagent?

    True when ANY trusted source says so:
      1. sanitized ``args.agent_id`` non-blank — written by the harness bridge from the
         harness' own signal (the bridge strips every model-supplied copy first);
      2. launcher env (:func:`launcher_agent_id`) — a detached child;
      3. nested omp ``PI_SESSION_FILE`` (:func:`nested_id_from_session_file`) — an omp
         Task/eval child, at any depth.

    A top-level event ``agent_id`` and anything under ``tool_input`` are deliberately NOT read
    here: the predicate relaxes gates, so its read surface is exactly what the bridges
    sanitize. The nested-``PI_SESSION_FILE`` tier is NOT restricted to the omp harness tag
    (agent-tools#676): omp sets it on every Task/eval child's process regardless of which
    harness backs that omp session (a Claude-Code-backed omp child fires through
    ``cc_hook_bridge``). A blank / whitespace value is not a signal.
    """
    if isinstance(args, Mapping):
        agent_id = args.get("agent_id")
        if agent_id and str(agent_id).strip():
            return True
    env = _env(environ)
    if launcher_agent_id(env):
        return True
    return bool(nested_id_from_session_file(env.get("PI_SESSION_FILE") or ""))
