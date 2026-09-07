"""Subagent identity for harnesses whose hook payload does not carry one (agent-tools#573).

Claude Code and codex put a TOP-LEVEL ``agent_id``/``agent_type`` on every hook event fired
inside a spawned subagent, so their bridges just forward that field (the codex bridge also reads
the spawned thread's rollout ``session_meta`` behind the event's ``transcript_path`` — see
``codex_hook_bridge.dispatch._rollout_subagent_identity``). opencode's plugin payload
and omp's (root-level) extension payload carry nothing of the kind, and a child started as a
separate PROCESS (``codex exec`` / ``omp -p`` / ``opencode run`` launched from a parent session's
shell tool) is invisible to the parent harness altogether. This module gives every bridge the same
two fall-back sources, both outside the reach of a model-authored ``tool_input``:

1. **Launcher env markers** — ``RIG_AGENT_ID=<name>`` (identity) or ``RIG_DETACHED_AGENT=1``
   (anonymous marker), exported by the ``rig-detached-<harness>`` launcher skills into the CHILD
   process only. A running orchestrator cannot retroactively mutate its own environment; it can
   only set these for a child it dispatches — which is exactly the sanctioned act of delegating.
   The bridge process inherits the harness's env (every bridge is spawned with the harness's own
   ``process.env``/``environ``), so the marker set at launch is visible on every tool call.

2. **Process ancestry** — the bridge walks its own parent chain (one ``ps`` snapshot). The first
   contiguous run of same-harness processes is the session that dispatched this hook (a codex
   invocation is TWO contiguous processes: the ``node`` wrapper script plus the vendor binary).
   A same-harness process found ABOVE that run, past at least one non-harness process (the
   parent's shell tool), is a parent session: this hook fires inside a delegated child. The
   process tree is kernel-maintained; nothing in a tool call's arguments can rewrite it.

Both sources RELAX gates (a subagent is exempt from the orchestrator-only gates and governed by the
subagent-only ones), so every failure path here — no ``ps``, a broken table, a missing parent —
yields ``""``: the call stays GOVERNED (fail closed in the relax direction, the same convention as
every ``_is_subagent`` reader). Cooperative-orchestrator threat model, like the rest of the family:
discipline, not a security boundary.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import PurePosixPath

_MAX_HOPS = 48
_PS_TIMEOUT_S = 5.0


def env_marker_agent_id(environ: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    """The launcher-env identity, or ``""`` when no marker is set.

    ``RIG_AGENT_ID`` (non-blank) wins; ``RIG_DETACHED_AGENT=1`` alone yields the anonymous id
    ``detached``. Any other value (blank, ``0``, ``true``) is not a marker."""
    env = os.environ if environ is None else environ
    name = str(env.get("RIG_AGENT_ID", "")).strip()
    if name:
        return name
    if str(env.get("RIG_DETACHED_AGENT", "")).strip() == "1":
        return "detached"
    return ""


def ancestor_agent_id(harness: str, *, pid: int | None = None,
                      table: dict[int, tuple[int, str]] | None = None) -> str:
    """``ancestor:<harness>:<pid>`` when a parent session of the SAME harness sits above the one
    that dispatched this hook, else ``""``. See the module docstring for the walk."""
    procs = _process_table() if table is None else table
    if not procs:
        return ""
    cur = os.getpid() if pid is None else pid
    seen: set[int] = set()
    phase = "below-self"  # → "in-self-run" → "past-self"
    for _ in range(_MAX_HOPS):
        entry = procs.get(cur)
        if entry is None or cur in seen:
            return ""
        seen.add(cur)
        ppid, args = entry
        matches = _is_harness_process(args, harness)
        if phase == "below-self":
            if matches:
                phase = "in-self-run"
        elif phase == "in-self-run":
            if not matches:
                phase = "past-self"
        elif matches:
            return f"ancestor:{harness}:{cur}"
        if ppid <= 1 or ppid == cur:
            return ""
        cur = ppid
    return ""


def detect_subagent(harness: str, environ: "os._Environ[str] | dict[str, str] | None" = None
                    ) -> tuple[str, str]:
    """``(agent_id, agent_type)`` from the env markers first, then ancestry; ``("", "")`` when
    neither applies. ``agent_type`` is ``detached`` for a launcher-env child and ``ancestor`` for
    a process-tree child."""
    marker = env_marker_agent_id(environ)
    if marker:
        return marker, "detached"
    ancestor = ancestor_agent_id(harness)
    if ancestor:
        return ancestor, "ancestor"
    return "", ""


# Interpreters whose argv[1] is the real program: `node /opt/homebrew/bin/codex` (the codex npm
# wrapper) and `bun /path/opencode`. Only these promote argv[1]; `python3 codex` (a same-named
# local script) or `sh omp` never count (review finding, #573 round 1).
_INTERPRETER_WRAPPERS = frozenset({"node", "bun", "deno"})


def _is_harness_process(args: str, harness: str) -> bool:
    """True when the process's executable IS the harness: the basename of argv[0] equals the
    harness name exactly, or argv[0] is a known JS interpreter wrapper and argv[1]'s basename
    does. A substring or a hyphenated lookalike never matches."""
    tokens = args.split()
    if not tokens:
        return False
    if PurePosixPath(tokens[0]).name == harness:
        return True
    return (
        len(tokens) > 1
        and PurePosixPath(tokens[0]).name in _INTERPRETER_WRAPPERS
        and PurePosixPath(tokens[1]).name == harness
    )


def _process_table() -> dict[int, tuple[int, str]]:
    """``{pid: (ppid, args)}`` from one ``ps`` snapshot; ``{}`` on any failure."""
    try:
        out = subprocess.run(
            # `-ww`: unlimited width. Without it procps (Linux) clips `args` to 80 columns when
            # stdout is not a terminal, so a harness under a long install path is never matched.
            ["ps", "-axwwo", "pid=,ppid=,args="],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_S, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2] if len(parts) > 2 else "")
    return table
