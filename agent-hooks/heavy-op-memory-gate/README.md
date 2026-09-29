# heavy-op-memory-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 38

Blocks STARTING a heavy operation while the machine is short of memory. Heavy operations are:

- a VS Code extension rebuild/`vsce package`
- a multi-model `review diff`/`quorum`/`brainstorm`/`just-ask` pass
- a build/test-suite run (npm/pnpm/yarn/bun/deno/cargo/go/make/rake/mvn/gradle/msbuild, or a
  direct playwright/cypress/jest/vitest/pytest invocation)
- a **GPU model load**: `stt`, `whisper-cli`, `whisper-server`, `llama-cli`, `llama-server`, the
  `mlx_lm`/`mlx_whisper` entry points (`mlx_lm.generate`, `python -m mlx_whisper`, …), or any
  command handed a `.bin`/`.gguf`/`.ggml`/`.safetensors` model file

Added after two incidents. On 2026-08-27/28 ~10-15 concurrent background agents running heavy
local work drove free memory on a 24GB dev machine to ~65MB, hanging several real VS Code
windows. On 2026-09-29 thirteen concurrent `whisper-cli` runs, each loading the 1.6 GB
large-v3-turbo model into Metal buffers, wired 24.65 of 25.77 GB (95.7%) and kernel-panicked
the Mac, while the jetsam pressure level (then this hook's only signal) stayed "normal"
(agent-tools#817).

## The signals

Either signal blocks. A signal that cannot be read contributes nothing; when neither can be read
the hook allows (fail-open).

**1. Jetsam pressure level.** `sysctl kern.memorystatus_vm_pressure_level` (macOS), the same
level Activity Monitor's "Memory Pressure" graph and the OS's own out-of-memory killer read.
1 = normal, 2 = warn, 4 = critical. Blocks at `>= RIG_HEAVY_OP_BLOCK_AT_LEVEL` (default `2`,
restricted to `{1,2,4}`: an out-of-range override like `999` would otherwise make the "no bypass"
contract below silently false). Linux gets a best-effort fallback via `/proc/pressure/memory`
(PSI `some avg10`).

**2. Wired share and free-plus-reclaimable memory.** Wired memory (GPU model buffers, the kernel)
can be neither paged out nor compressed, so it never raises the jetsam level. From `vm_stat`
against installed RAM (macOS) or `/proc/meminfo` (Linux) the hook computes:

| Measure            | macOS                                           | Linux                                                       | Blocks when | Default | Override                               |
| ------------------ | ----------------------------------------------- | ----------------------------------------------------------- | ----------- | ------- | -------------------------------------- |
| wired share of RAM | `Pages wired down`                              | `Unevictable` + `SUnreclaim` + `KernelStack` + `PageTables` | above       | 75%     | `RIG_HEAVY_OP_MAX_WIRED_PCT` (1..99)   |
| free + reclaimable | free + inactive + speculative + purgeable pages | `MemAvailable`                                              | below       | 512 MB  | `RIG_HEAVY_OP_MIN_AVAILABLE_MB` (>= 1) |

Raw free pages alone would be noisy on macOS (the OS keeps free low on purpose and uses spare
RAM as file cache), which is why neither measure uses them alone: file cache is never wired, and
inactive/speculative/purgeable pages count as available. A healthy busy Mac shows ~100 MB free
but GBs of inactive memory; the 2026-09-29 panic showed 95.7% wired and 112 MB free plus
reclaimable. An override that would disable a check (`0`, a negative number, a percentage of 100
or more) falls back to the default.

Neither threshold is calibrated against a real Linux incident.

## The GPU model slot pool

A pre-bash hook sees one agent command once: `for f in *.wav; do stt "$f"; done` is checked when
the loop starts, not per model load. The per-load cap is the machine-wide GPU slot pool in
[`lib/agenttools_gpu_slots`](../../lib/agenttools_gpu_slots/README.md): `RIG_GPU_SLOTS` (default 2) `flock` lock files shared by every agent and harness on the machine, one held by each running
model for its lifetime. The model command waits for a slot itself, through the wrapper or through
stt-cli's own acquire:

```bash
python3 <agent-tools>/lib/agenttools_gpu_slots run -- whisper-cli -m ggml-large-v3-turbo.bin -f a.wav
python3 <agent-tools>/lib/agenttools_gpu_slots status   # who holds the slots
```

This hook only probes the pool, without waiting: it refuses an un-queued GPU model load at once
while every slot is taken, and the block message names the holders and the wrapper command. A load
already queued through the wrapper is not refused (the wrapper waits), but the memory signals
above still apply to it.

## Why the hook itself is not a counter and never waits

Considered and rejected. See the hook module's own top-of-file docstring for the full
reasoning. Short version: every pre-bash hook here decides fast; a hook that _waits_ either
blocks past the dispatcher timeout and silently allows anyway, or turns "machine busy" into a
hard failure with no visibility. A counter built from pid files needs a slot released by
whoever acquired it, and an agent killed mid-operation (routine here) leaks it forever.

The slot pool avoids that because the locks are kernel-managed: the kernel drops a holder's
`flock` the instant it dies, SIGKILL included, so no slot can go stale and nothing needs reaping.
The hook keeps **no state of its own**: every call re-reads memory and probes the locks fresh,
decides, and forgets.

## Detection is token-based, not a raw substring search

Unlike the advisory `enforce-timeout-on-bash`, this hook hard-blocks with **no bypass**, so a
raw-substring match over the whole command string would false-positive on a heavy word trapped
inside a QUOTED argument, e.g. `git commit -m "make all tests green"`. That exact bug was caught
in review before this hook shipped. Detection uses `shlex` + exact-token/adjacency matching
instead, so a quoted commit message/PR body/title is one token and never matches a bare runner
word. GPU loads are matched per simple command (split at `;`, `&&`, `|`, newlines, …): a loop
body, a `bash -c '…'` script, a process substitution (`diff <(stt a.wav) b.txt`), a command
substitution (`echo "$(stt a.wav)"`, backticks; not inside single quotes), or a path-qualified
binary is seen, while a simple command that starts with a pure file utility, build tool or
lookup (`ls models/*.bin`, `shasum ggml.bin`, `curl -o ggml.bin …`, `gcc -o fw.bin`,
`which whisper-cli`), or with process control or inspection (`pkill -f whisper-cli`, `pgrep`,
`kill`, `killall`, `ps` — the remediation for a runaway load is never refused), is not a load.
`find` is not a load either, but the command its `-exec`/`-execdir` runs is classified. `-h`/
`--help` exempts a command only as its own flag, not as an option's value (`llama-cli -p "-h"`
still loads). A heredoc body is stdin data, not commands (a PR body written with
`<<'EOF'` that mentions `stt a.wav` loads nothing), unless the heredoc goes to a shell
(`bash <<EOF`, `cat <<EOF | sh`); with an unquoted delimiter (`<<EOF`) the shell still runs the
body's `$(…)` and backtick substitutions, so those count. A here-string (`<<< word`) and an
arithmetic shift (`$((1<<n))`) open no heredoc, so the lines after them are still commands.

## No hatch-escalation

Deliberately no Telegram `RIG_HATCH_REQUEST_*` override either (unlike most hard-block hooks in
this catalog): whether the machine currently has enough free memory or a free GPU slot is an
objective, self-resolving fact a plain retry re-measures, not a human judgment call.

## Test

```bash
chmod +x heavy_op_memory_gate.py
echo '{"args":{"command":"npm test"}}' | ./heavy_op_memory_gate.py; echo "exit=$?"
# exit=0 (allow) when memory is healthy; exit=10 (block) when a signal crosses its threshold —
# depends on the REAL machine's current memory, since this hook reads it live.

echo '{"args":{"command":"git status"}}' | ./heavy_op_memory_gate.py; echo "exit=$?"
# → allow, exit=0 always (not a heavy operation — never even reads memory)
```

Full suite: `uv run --with pytest python -m pytest tests/test_heavy_op_memory_gate.py
tests/test_agenttools_gpu_slots.py -q` (detection, both memory readers against the 2026-09-29
panic page counts, threshold validation, the slot pool with real holder processes, and `main()`
wiring).
