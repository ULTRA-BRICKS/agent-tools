#!/usr/bin/env python3
"""agents-hooks/v1 pre-agent hook — refuse to DISPATCH a new subagent while the
machine is genuinely under memory pressure.

WHY THIS EXISTS: the 2026-08-27/28 incident (see `agent-hooks/heavy-op-memory-gate`'s
own docstring) had roughly 10-15 background agents running CONCURRENTLY, several doing
heavy local work at once, and drove free memory on a 24GB machine to ~65MB. A second,
independent incident on 2026-09-08 reproduced the same failure mode from the fan-out
side specifically: a JetsamEvent mass-kill with 13 `omp` + 46 `node` + 20 Python
processes running concurrently and swap at 86%, where the kernel killed 35+ low-priority
daemons before it would ever have touched the omp/node/python processes themselves —
i.e. the machine was already deep in survival mode well before anything agent-visible
looked "critical".

`heavy-op-memory-gate` (agent-tools#477) closed HALF of this gap: it blocks a heavy
*bash command* (a build, a test suite, a multi-model `review` pass) from starting under
memory pressure. It fires on `pre-bash`, keyed on the shape of the command string — it
has no opinion on, and never fires for, the act of DISPATCHING a subagent in the first
place (the `Agent`/`Task` tool call, or omp's `task` tool). agent-tools#579's own
resolution note is explicit that this is exactly the remaining scope: "a pre-agent gate
for Agent/Task dispatch (fan-out itself)" (plus a swap-usage signal alongside the
jetsam level — that second half is a still-open follow-up, deliberately NOT done here;
see this hook's README).

rig-cli#508 was closed `not_planned` on the claim that "the 10-agent cap" already
prevents recurrence. That cap is NOT a tool-level enforced guard — it is a natural-
language convention living in `~/.config/rig/MEMORY.md` / `~/.config/rig/fleet-cap-loop-
prompt.md` that the orchestrator's own `/loop` is supposed to read and self-enforce each
iteration by calling `~/.config/rig/fleet-cap-loop.sh`. That script's own top comment
says it is "snapshot-only" and that "cap gating lives in the prompt", and it "always
exit[s] 0 so /loop --while never halts at cap" — i.e. it is advisory telemetry an LLM
might or might not act on, not a mechanism that can block a tool call. Nothing about it
is enforceable at the point a subagent is actually dispatched, and it lives in
user-machine-local config this catalog does not ship or control. rig-cli#507 (open) asks
for exactly what is missing: "an implementation location, limit-enforcement test or
before/after verification" — this hook, its descriptor, and its test suite are that.

THIS IS DELIBERATELY NOT A CONCURRENCY COUNTER OR A LOCK/SEMAPHORE — the same reasoning
`heavy-op-memory-gate` documents applies here, doubly so: a counting semaphore needs a
slot released by whoever acquired it, and a subagent being killed mid-run (routine —
session churn, a `TaskStop`, a crash, an orchestrator cancel) leaks its slot forever;
reaping a stale slot correctly is itself a hard, stateful problem that risks deadlocking
every agent on the machine. Subagents die far more routinely than bash commands finish
abnormally, so a counter here would be even more failure-prone than the one already
rejected for the bash-level hook. This hook carries **zero persistent state** — no lock
file, no counter, no queue position, no "N agents currently running" anywhere. Every
invocation re-reads the machine's REAL memory pressure at that instant and decides
fresh. Nothing to leak, nothing to go stale, nothing that can deadlock a second hook.

The signal is macOS's own kernel jetsam pressure level
(`sysctl kern.memorystatus_vm_pressure_level`) — the exact value Activity Monitor's
"Memory Pressure" graph and the OS's own out-of-memory killer read, NOT a raw
"free pages" heuristic (see `heavy-op-memory-gate`'s docstring for why a raw byte
threshold false-positives on macOS). 1 = normal, 2 = warn, 4 = critical. This hook
blocks at WARN-or-worse by default (level >= 2) — proactive, since the goal is to stop
the NEXT subagent dispatch from tipping an already-strained machine into either incident
above, not to wait until the machine is already critical. Tune via
`RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL` (own name, independently tunable from
`RIG_HEAVY_OP_BLOCK_AT_LEVEL` — the two hooks gate different actions and may need
different postures) if that default proves too aggressive in practice. On any platform
where this sysctl is unavailable (Linux, sandboxed environments), the hook has no
reliable signal and ALLOWS (fail-open) — see `_read_linux_pressure_level` for the
best-effort `/proc/pressure/memory` fallback tried first.

Unlike `background-subagent-gate` (the other `pre-agent` hook in this catalog, which
exempts a dispatch made FROM inside a subagent via `agent_id` — that hook's concern,
foreground-vs-background, is inherently an orchestrator-only question), THIS hook
applies to every dispatch regardless of who issues it. Whether starting one more agent
process risks tipping an already-strained machine has nothing to do with whether the
caller is the orchestrator or a worker fanning out further — both incidents above were
driven by total agent-process count/memory footprint, not by which layer of the fan-out
tree issued the call. There is deliberately no `agent_id` exemption here.

No self-service bypass and deliberately NO Telegram hatch-escalation machinery either
(same rationale as `heavy-op-memory-gate`) — that machinery exists to get a HUMAN
JUDGMENT call on a decision only a human can make; whether the machine currently has
enough free memory is not a judgment call, it is an objective, self-resolving fact that
a plain retry re-measures. If pressure genuinely never clears (every other agent is
idle, nothing is going to free memory) that is itself a signal worth telling the
machine's operator about directly, not something to hatch-approve past.

Contract (agents-hooks/v1):
  stdin  : JSON event; a subagent dispatch (Agent/Task/omp task tool call)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash or an unreadable pressure signal here must never wedge an
agent's ability to dispatch work — this is an availability circuit-breaker, not a
security boundary.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# jetsam pressure levels (macOS kern.memorystatus_vm_pressure_level): 1=normal,
# 2=warn, 4=critical. Default blocks at WARN-or-worse; override for a looser or
# stricter posture without touching code.
DEFAULT_BLOCK_AT_LEVEL = 2
# The ONLY levels the sysctl actually reports (see module docstring). Restricting
# the env override to this set closes an otherwise-real bypass: an operator (or a
# confused agent) setting RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL=5 or =999 would make
# even jetsam level 4 (critical) pass, silently defeating the hook's own "no
# bypass" claim in `decide`'s block message.
_VALID_PRESSURE_LEVELS = frozenset({1, 2, 4})


# ── memory-pressure reading (verbatim copy of heavy-op-memory-gate's machinery —
# agent-hooks are deliberately self-contained with no shared import across
# agent-hooks/* dirs; see agent-hooks/README.md and heavy-op-memory-gate/README.md) ──
_RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


def _read_macos_pressure_level(run: _RunFn = subprocess.run) -> Optional[int]:
    """The XNU jetsam pressure level (1/2/4), or None when unavailable/unparseable."""
    try:
        proc = run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _default_read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _read_linux_pressure_level(read_text: Callable[[str], str] = _default_read_text) -> Optional[int]:
    """Best-effort Linux fallback via PSI (`/proc/pressure/memory`'s `some avg10`).

    Maps the percentage onto the same 1/2/4 scale as macOS so callers share one
    threshold: <10% -> 1 (normal), <60% -> 2 (warn), >=60% -> 4 (critical). Not
    calibrated against a real incident (no Linux repro available) — deliberately
    conservative thresholds; returns None (fail-open) on any read/parse failure,
    including "file doesn't exist" (non-Linux, or PSI disabled in the kernel).
    """
    try:
        text = read_text("/proc/pressure/memory")
    except OSError:
        return None
    match = re.search(r"^some\s+.*\bavg10=([\d.]+)", text, re.MULTILINE)
    if not match:
        return None
    try:
        avg10 = float(match.group(1))
    except ValueError:
        return None
    if avg10 >= 60:
        return 4
    if avg10 >= 10:
        return 2
    return 1


def read_pressure_level(
    macos_reader: Optional[Callable[[], Optional[int]]] = None,
    linux_reader: Optional[Callable[[], Optional[int]]] = None,
    platform_name: Optional[str] = None,
) -> Optional[int]:
    """Platform-DISPATCHING (not fallback-chaining) pressure read. None means
    "no reliable signal" — callers MUST treat that as fail-open, never as
    "normal".

    Dispatches on the real platform rather than "try macOS, then fall back to
    Linux" — that chain was dead weight in practice (each reader already fails
    fast when its OS-specific binary/file is absent) but still cost one extra
    subprocess spawn attempt on every single check on Linux, which is pure
    waste on a platform that never has `sysctl kern.memorystatus_*`.

    Defaults are resolved INSIDE the body (not bound as parameter defaults) so
    that monkeypatching the module-level `_read_macos_pressure_level` /
    `_read_linux_pressure_level` names (as `main()`'s tests do, exercising the
    real end-to-end call path) takes effect — a parameter default would bind
    the original function object at import time and silently ignore a patch.
    """
    macos_reader = macos_reader or _read_macos_pressure_level
    linux_reader = linux_reader or _read_linux_pressure_level
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name == "Darwin":
        return macos_reader()
    return linux_reader()


def emit(decision: str, message: Optional[str] = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"fleet-memory-gate: {msg}\n")


def _block_at_level() -> int:
    raw = os.environ.get("RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL")
    if not raw:
        return DEFAULT_BLOCK_AT_LEVEL
    try:
        value = int(raw)
    except ValueError:
        warn(f"RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL={raw!r} is not an int — using default {DEFAULT_BLOCK_AT_LEVEL}")
        return DEFAULT_BLOCK_AT_LEVEL
    if value not in _VALID_PRESSURE_LEVELS:
        # A value outside {1,2,4} (e.g. 5 or 999) would make `level < block_at`
        # true for every real jetsam reading — a silent, total bypass of the
        # "no bypass" contract documented in `decide`'s block message. Reject it
        # rather than honor a value that can never actually block.
        warn(
            f"RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL={value} is not one of the real jetsam "
            f"levels {sorted(_VALID_PRESSURE_LEVELS)} — using default {DEFAULT_BLOCK_AT_LEVEL} "
            "(an out-of-range value would silently bypass every block)"
        )
        return DEFAULT_BLOCK_AT_LEVEL
    return value


def decide(level: Optional[int], block_at: int) -> tuple[str, Optional[str]]:
    """Pure decision core — the seam every test in this suite exercises directly.

    Unlike `heavy-op-memory-gate.decide`, there is no `label` parameter: every
    `pre-agent` event IS a subagent dispatch, so there is no "is this heavy"
    pre-filter step to skip past for a non-matching action.
    """
    if level is None:
        return "allow", None  # no reliable pressure signal on this platform — fail open
    if level < block_at:
        return "allow", None

    level_name = {1: "normal", 2: "warn", 4: "critical"}.get(level, str(level))
    message = (
        f"Blocked: the machine's memory pressure is currently '{level_name}' "
        f"(jetsam level {level}, block threshold {block_at}). Dispatching a NEW "
        "SUBAGENT now risks repeating the 2026-08-27/28 near-total-memory-exhaustion "
        "incident (10-15 concurrent background agents drove free memory to ~65MB and "
        "hung several VS Code windows) or the 2026-09-08 JetsamEvent mass-kill (13 omp "
        "+ 46 node + 20 Python processes concurrently, swap at 86%, 35+ low-priority "
        "daemons killed before the kernel would have touched an agent process). There "
        "is no bypass for this check — wait a few minutes for other concurrent work to "
        "finish or be reaped, then retry; the pressure level is re-read fresh on every "
        "attempt, so a retry after genuine memory frees up will pass. If pressure never "
        "clears even with nothing else obviously running, that itself is worth "
        "reporting to the machine's operator directly rather than working around this "
        "gate."
    )
    return "block", message


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    if not isinstance(event, dict):
        warn(f"event is a {type(event).__name__}, not an object — allowing (fail-open)")
        emit("allow")
        return 0

    # Every pre-agent event is inherently the operation this hook governs — unlike
    # heavy-op-memory-gate there is no command-shape classification step, so pressure
    # is read unconditionally. pre-agent events are comparatively rare next to
    # pre-bash, so the "skip the sysctl spawn on the common non-matching case" perf
    # argument that shapes heavy-op-memory-gate's ordering does not apply here.
    level = read_pressure_level()
    decision, message = decide(level, _block_at_level())

    if decision == "block":
        emit("block", message)
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
