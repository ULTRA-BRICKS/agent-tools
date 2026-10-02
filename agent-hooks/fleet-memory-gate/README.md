# fleet-memory-gate

**Point:** `pre-agent` · **Fail policy:** `open` · **Priority:** 35

Blocks DISPATCHING a new subagent — the `Agent`/`Task` tool call, opencode's `task`
tool, or omp's `task` tool — while the machine's real memory pressure is at
WARN-or-worse.

Added to close the still-open scope note from agent-tools#579: `heavy-op-memory-gate`
(agent-tools#477) already blocks a heavy *bash command* (a build, a test suite, a
multi-model `review` pass) from starting under memory pressure, but nothing gated the
act of dispatching a subagent in the first place. Two real incidents motivate this:
2026-08-27/28 (~10-15 concurrent background agents running heavy local work drove free
memory on a 24GB machine to ~65MB, hanging several VS Code windows) and 2026-09-08 (a
JetsamEvent mass-kill with 13 `omp` + 46 `node` + 20 Python processes running
concurrently and swap at 86%, where the kernel killed 35+ low-priority daemons before it
would ever have touched an agent process). rig-cli#508 closed `not_planned` on the claim
that "the 10-agent cap" already prevents recurrence, but that cap is a natural-language
convention in `~/.config/rig/MEMORY.md` / `~/.config/rig/fleet-cap-loop-prompt.md`,
self-enforced (or not) by an LLM reading a prompt each `/loop` iteration —
`~/.config/rig/fleet-cap-loop.sh` itself is documented as "snapshot-only" and "always
exit[s] 0 so `/loop --while` never halts at cap". It is not a tool-level enforced guard.
rig-cli#507 (open) is the ask for exactly what was missing: "an implementation location,
limit-enforcement test or before/after verification." This hook, its descriptor, and its
test suite are that.

## The signal

Identical machinery to `heavy-op-memory-gate` (verbatim copy — agent-hooks are
deliberately self-contained, no shared import across `agent-hooks/*` dirs):
`sysctl kern.memorystatus_vm_pressure_level` (macOS) — the same jetsam pressure level
Activity Monitor's "Memory Pressure" graph and the OS's own out-of-memory killer read.
1 = normal, 2 = warn, 4 = critical. Blocks at `>= RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL`
(default `2`, restricted to `{1,2,4}` — an out-of-range override like `999` would
otherwise make the "no bypass" contract below silently false). Deliberately its own env
var, independent of `RIG_HEAVY_OP_BLOCK_AT_LEVEL` — the two hooks gate different actions
and may need different postures. Linux gets a best-effort fallback via
`/proc/pressure/memory` (PSI `some avg10`), uncalibrated against a real incident. Any
platform without a reliable signal fails open (allows).

The still-open follow-up from agent-tools#579 — a swap-usage signal in addition to the
jetsam level — is deliberately NOT implemented here or in `heavy-op-memory-gate`.

## Why NOT a concurrency counter/semaphore/lock

Considered and rejected for the exact reason `heavy-op-memory-gate` rejected it, doubly
so here: a counting semaphore needs a slot released by whoever acquired it, and a
subagent being killed mid-run — session churn, a `TaskStop`, a crash, an orchestrator
cancel — is *routine*, arguably more routine than a bash command dying abnormally.
Reaping a stale slot correctly is itself a hard, stateful problem that risks deadlocking
every agent on the machine. This hook is **stateless**: every invocation re-reads real
pressure fresh, decides, and forgets. Nothing to leak, nothing to go stale.

## Applies to every dispatch, not just the orchestrator's

Unlike `background-subagent-gate` (the other `pre-agent` hook in this catalog, which
exempts a dispatch made from inside a subagent via `agent_id` — that hook's concern,
foreground-vs-background, is inherently an orchestrator-only question), this hook fires
identically for the orchestrator's own dispatch and a subagent's further fan-out. Whether
starting one more agent process risks tipping an already-strained machine has nothing to
do with which layer of the fan-out tree issued the call — both incidents above were
driven by total agent-process count/memory footprint. There is deliberately no
`agent_id` exemption.

## No hatch-escalation

Deliberately no Telegram `RIG_HATCH_REQUEST_*` override either (same rationale as
`heavy-op-memory-gate`) — whether the machine currently has enough free memory is an
objective, self-resolving fact a plain retry re-measures, not a human judgment call.

## Relationship to `heavy-op-memory-gate`

Two complementary layers of the same memory-safety story, reading the same real signal:

- `heavy-op-memory-gate` (`pre-bash`) blocks a heavy *command* (build/test/review)
  from starting.
- `fleet-memory-gate` (`pre-agent`, this hook) blocks the *dispatch* — a new subagent
  process — itself, regardless of what that subagent intends to run.

A machine already at WARN pressure gets protected at both the "don't start one more
heavy command" layer and the "don't start one more agent process" layer.

## Test

```bash
chmod +x fleet_memory_gate.py
echo '{"args":{}}' | ./fleet_memory_gate.py; echo "exit=$?"
# exit=0 (allow) when pressure is below the block threshold; exit=10 (block) at/above it —
# depends on the REAL machine's current pressure, since this hook reads it live on every call.

echo '{"args":{"agent_id":"sub-1"}}' | ./fleet_memory_gate.py; echo "exit=$?"
# → same decision as above; agent_id present or absent makes no difference (see "Applies to
# every dispatch" above).
```

Full suite: `uv run --with pytest python -m pytest tests/test_fleet_memory_gate.py -q`.
