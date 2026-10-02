#!/usr/bin/env python3
"""agents-hooks/v1 pre-agent hook — refuse subagent fan-out under memory pressure.

Complements heavy-op-memory-gate (pre-bash, heavy commands) by blocking Agent/Task dispatch
itself when jetsam pressure, free RAM, or swap use crosses configured thresholds — the
remaining gap called out in agent-tools#519 after #477 shipped only the pre-bash gate.

Contract (agents-hooks/v1):
  stdin  : JSON event for a subagent dispatch (Agent/Task/opencode task/omp task)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": availability circuit-breaker, not a security boundary.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_MEMORY_MODULE = "agenttools_memory_pressure"


def _load_memory_pressure():
    init = Path(__file__).resolve().parents[2] / "lib" / _MEMORY_MODULE / "__init__.py"
    spec = importlib.util.spec_from_file_location(_MEMORY_MODULE, init)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_MEMORY_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MEMORY_MODULE] = module
    spec.loader.exec_module(module)
    return module


mp = _load_memory_pressure()
HOOK_API = mp.HOOK_API
BLOCK_EXIT_CODE = mp.BLOCK_EXIT_CODE


def warn(msg: str) -> None:
    sys.stderr.write(f"agent-fan-out-memory-gate: {msg}\n")


def _dispatch_label(event: dict) -> str:
    tool = event.get("tool") or event.get("tool_name") or "subagent"
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    kind = args.get("subagent_type") or args.get("agent") or args.get("type")
    if kind:
        return f"{tool} dispatch ({kind})"
    return f"{tool} dispatch"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        mp.emit("allow")
        return 0

    if not isinstance(event, dict):
        warn(f"event is a {type(event).__name__}, not an object — allowing (fail-open)")
        mp.emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    config = mp.load_config(cwd)
    snapshot = mp.read_snapshot()
    label = _dispatch_label(event)
    result = mp.assess(snapshot, config, action_label=label)

    if result.decision == "block":
        mp.emit("block", result.message)
        return BLOCK_EXIT_CODE

    mp.emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
