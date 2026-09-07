# heavy-op-memory-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 38

Blocks STARTING a heavy operation — a VS Code extension rebuild/`vsce package`, a
multi-model `review diff`/`quorum`/`brainstorm`/`just-ask` pass, or a build/test-suite
run (npm/pnpm/yarn/bun/deno/cargo/go/make/rake/mvn/gradle/msbuild, or a direct
playwright/cypress/jest/vitest/pytest invocation) — while the machine's real memory
pressure is at WARN-or-worse.

Added after a 2026-08-27/28 incident: ~10-15 concurrent background agents running heavy
local work (extension rebuilds, multi-model review passes, full test suites) drove free
memory on a 24GB dev machine to ~65MB, hanging several real VS Code windows.

## The signal

`sysctl kern.memorystatus_vm_pressure_level` (macOS) — the same jetsam pressure level
Activity Monitor's "Memory Pressure" graph and the OS's own out-of-memory killer read.
1 = normal, 2 = warn, 4 = critical. Blocks at `>= RIG_HEAVY_OP_BLOCK_AT_LEVEL` (default
`2`, restricted to `{1,2,4}` — an out-of-range override like `999` would otherwise make
the "no bypass" contract below silently false). Linux gets a best-effort fallback via
`/proc/pressure/memory` (PSI `some avg10`), uncalibrated against a real incident. Any
platform without a reliable signal fails open (allows).

## Why NOT a concurrency counter/semaphore/lock

Considered and rejected — see the hook module's own top-of-file docstring for the full
reasoning. Short version: every pre-bash hook here decides fast; a hook that *waits*
either blocks past the dispatcher timeout and silently allows anyway, or turns "machine
busy" into a hard failure with no visibility. A counter also needs a slot released by
whoever acquired it, and an agent killed mid-operation (routine here) leaks it forever —
reaping a stale slot correctly is itself a hard, stateful problem that risks deadlocking
every agent on the machine. This hook is **stateless**: every invocation re-reads real
pressure fresh, decides, and forgets. Nothing to leak, nothing to go stale.

## What is never heavy: `--help` probes and read-only segments

A `tool --help` / `--version` probe prints usage and exits, and a segment whose command is a
plain file reader (`ls`, `cat`, `head`, `tail`, `sed`, `rg`, `grep`, `wc`, `stat`) cannot start
a suite no matter what its arguments are named — `ls -l node_modules/.bin/vitest` lists a file.
Both were refused under memory warn in the codex sessions of 2026-09-05/06 (agent-tools#579),
so the classifier now judges each shell SEGMENT (one per line, then per `;`/`&&`/`|`) and skips
those two shapes. The `--help` carve-out applies only when the flag precedes any `--`:
`npm run build -- --help` hands the flag to the build script and is still gated. Wrappers are
never exempt (`sudo pytest`, `env CI=1 npm test`, `cd x && pnpm test` all still block), and
within a segment the check is the same loose token membership as before.

## Detection is token-based, not a raw substring search

Unlike the advisory `enforce-timeout-on-bash`, this hook hard-blocks with **no bypass** —
so a raw-substring match over the whole command string would false-positive on a heavy
word trapped inside a QUOTED argument, e.g. `git commit -m "make all tests green"`. That
exact bug was caught in review before this hook shipped. Detection uses `shlex.split` +
exact-token/adjacency matching instead, so a quoted commit message/PR body/title is one
token and never matches a bare runner word.

## No hatch-escalation

Deliberately no Telegram `RIG_HATCH_REQUEST_*` override either (unlike most hard-block
hooks in this catalog) — whether the machine currently has enough free memory is an
objective, self-resolving fact a plain retry re-measures, not a human judgment call.

## Test

```bash
chmod +x heavy_op_memory_gate.py
echo '{"args":{"command":"npm test"}}' | ./heavy_op_memory_gate.py; echo "exit=$?"
# exit=0 (allow) when pressure is below the block threshold; exit=10 (block) at/above it —
# depends on the REAL machine's current pressure, since this hook reads it live.

echo '{"args":{"command":"git status"}}' | ./heavy_op_memory_gate.py; echo "exit=$?"
# → allow, exit=0 always (not a heavy operation — never even reads pressure)
```

Full suite: `uv run --with pytest python -m pytest tests/test_heavy_op_memory_gate.py -q`
(78 tests: detection, platform dispatch, threshold validation, `main()` wiring).
