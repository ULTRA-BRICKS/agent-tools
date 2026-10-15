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

Not heavy: `runpod-run [options] -- <command>` (skill `runpod-offload`, the place this hook's own
block message sends a suite). Everything after `--` runs on a Runpod pod over ssh; locally it is
an rsync + ssh. The program is found past env assignments, `nice`/`nohup`/`time`,
`timeout [opts] DURATION` and their options (not behind `sudo`/`env`/`command -p`, which may
resolve another `runpod-run`; see below). Only the installed script is exempt: a bare
`runpod-run` must resolve via `PATH` (an absolute or `~/` path directly), after symlinks, to
`~/.agents/skills/runpod-offload/runpod-run` (where rig installs the skill) or to
`skills/universal/runpod-offload/runpod-run` in this hook's own agent-tools checkout (or one of its
registered git worktrees) — a name or a
copied header proves nothing. A relative path gets no exemption, and neither does a command that
could re-point a name in its local part: one that defines any function (the shell may expand the
name — `function runpod${dash}run(){…}` is `runpod-run` in zsh; zsh's `functions[…]` table too) or
alias, assigns `PATH`, zsh's `path`, `fpath`/`FPATH`, `HOME` or `XDG_CONFIG_HOME` (also element-wise or
`+=`) or names one of them as an argument of `set`/`typeset`/`declare`/`local`/`export`/`readonly`/`unset`/`integer`/`float`
(`set -A path …`), uses `hash -p`, or runs shell code
the hook cannot see (`source`, `.`, `eval`, `autoload`, `emulate`) —
a spoofed `runpod-run() { shift; "$@"; }` would run the suite locally. What follows a
`runpod-run`'s `--` runs on the pod and is not scanned (`runpod-run -- bash -c 'export PATH=…; make
test'` stays exempt). (A function or PATH
change left behind by an earlier call of a persistent shell, or by a sourced file, is invisible
to a pre-bash hook; this is a memory-safety gate, not a security boundary.)
The exemption is per simple command: `runpod-run -- x && bun
test`, `bun test; runpod-run …` and a command substitution the local shell expands inside its
argv (`runpod-run -- echo "$(npm test)"`) are still classified and gated.

Handed to review-cli: a `review diff`/`quorum`/`brainstorm`/`just-ask` pass run by the installed
review-cli with its admission controller (`reviewlib/admission.py`, review-cli#614). review-cli
queues every run until host CPU/RAM and each AI provider fit with 20% headroom, so gating the
same thing here twice only made agents retry. Such a review skips the memory budget, the swap
check and the live-runner ceiling, and is blocked only on the emergency signals: `MemAvailable`
below its floor, PSI above its limit, the wired share, the jetsam level. "Installed" is pinned like
`runpod-run`: the `review` program must resolve (PATH lookup or absolute/`~/` path, symlinks
resolved) to `<root>/bin/review`, where `<root>` is the checkout `~/.local/bin/review` resolves to,
its repository's main checkout, or one of its registered git worktrees (`<common .git>/worktrees/*/gitdir`;
a directory merely put under `.worktrees/` is not one). Whether admission is on is asked from
review-cli itself: the hook imports `reviewlib.admission` from that checkout and calls its
`load_settings()` (review-cli's own YAML and env rules, ~0.1 s, only for a review command;
anything it prints goes to stderr). A checkout without it, any failure there, or `enabled` false
means no handoff. It is asked with the hook's own environment, so no handoff either for a command
that may give the review another one: any mention of `REVIEW_ADMISSION` (`=0`, `=$OFF`, `unset`,
`export` in any order), or a review prefixed with an assignment, an `env` option, or run behind
`sudo`/`doas`/`env`/`su` (another user's PATH and config; `sudo runpod-run` gets no exemption either).
Nor for a relative path, an interpreter in front (`python3 …/review`), or a command
that defines any function or alias, assigns or declares (`set -A`/`typeset`/`export`/`unset`…)
PATH/`path`/`fpath`/HOME/XDG_CONFIG_HOME, uses `hash -p` or
runs unseen shell code (`source`, `.`, `eval`). If the command also starts other heavy work
(`review diff & bun test`), nothing is handed off — neither gate would reserve the other — and the
whole command is gated at the costlier of the two. Live reviews keep counting in every other
operation's budget. Residuals of a memory gate, not a sandbox: a `/proc` hidden by `hidepid`,
worktree metadata forged inside a repo's `.git`, a `load_settings()` hanging past the hook timeout
(`on_error: open`), review-cli running without admission when its registry lock cannot be had (its
documented fallback), and shell code nested in `sh -c '…'`, which this hook does not parse.

Added after two incidents. On 2026-08-27/28 ~10-15 concurrent background agents running heavy
local work drove free memory on a 24GB dev machine to ~65MB, hanging several real VS Code
windows. On 2026-09-29 thirteen concurrent `whisper-cli` runs, each loading the 1.6 GB
large-v3-turbo model into Metal buffers, wired 24.65 of 25.77 GB (95.7%) and kernel-panicked
the Mac, while the jetsam pressure level (then this hook's only signal) stayed "normal"
(agent-tools#817).

## The signals

Either signal blocks. A signal that cannot be read contributes nothing; when neither can be read
the hook allows (fail-open).

**1. Jetsam pressure level (macOS).** `sysctl kern.memorystatus_vm_pressure_level`, the same
level Activity Monitor's "Memory Pressure" graph and the OS's own out-of-memory killer read.
1 = normal, 2 = warn, 4 = critical. Blocks at `>= RIG_HEAVY_OP_BLOCK_AT_LEVEL` (default `2`,
restricted to `{1,2,4}`: an out-of-range override like `999` would otherwise make the "no bypass"
contract below silently false). Linux has no jetsam; its pressure signal is PSI, below.

**2. Wired share and free-plus-reclaimable memory.** Wired memory (GPU model buffers, the kernel)
can be neither paged out nor compressed, so it never raises the jetsam level. From `vm_stat`
against installed RAM (macOS) or `/proc/meminfo` (Linux) the hook computes:

| Measure            | macOS                                           | Linux                                                       | Blocks when | Default | Override                               |
| ------------------ | ----------------------------------------------- | ----------------------------------------------------------- | ----------- | ------- | -------------------------------------- |
| wired share of RAM | `Pages wired down`                              | `Unevictable` + `SUnreclaim` + `KernelStack` + `PageTables` | above       | 75%     | `RIG_HEAVY_OP_MAX_WIRED_PCT` (1..99)   |
| free + reclaimable | free + inactive + speculative + purgeable pages | `MemAvailable`                                              | below       | 512 MB (macOS) / max(8 GiB, 20% of MemTotal) (Linux) | `RIG_HEAVY_OP_MIN_AVAILABLE_MB` (>= 1) |

Raw free pages alone would be noisy on macOS (the OS keeps free low on purpose and uses spare
RAM as file cache), which is why neither measure uses them alone: file cache is never wired, and
inactive/speculative/purgeable pages count as available. A healthy busy Mac shows ~100 MB free
but GBs of inactive memory; the 2026-09-29 panic showed 95.7% wired and 112 MB free plus
reclaimable. An override that would disable a check (`0`, a negative number, a percentage of 100
or more) falls back to the default.

### Linux (2026-10-10 / 2026-10-11)

Calibrated against two incidents on a shared 48 GB / 16 GB-swap Linux workstation running many
omp agent sessions: three concurrent `review diff` runs (up to four fat agent CLIs each), `uv run
mypy` + `pytest` with testcontainers docker and several `bun test` suites lost 26 GB in two
minutes; swap had been 100% full since the night before; the kernel OOM killer never fired (the
box thrashed on file cache, MemAvailable floored at ~1.9 GB) and systemd-oomd was starved; manual
reset after 18 minutes. The old 512 MB floor never fired. On Linux any of these blocks:

| Signal | Source | Blocks when | Default | Override |
| --- | --- | --- | --- | --- |
| free + reclaimable | `MemAvailable` | below | max(8 GiB, 20% of MemTotal) — ~9.6 GB on 48 GB (one review fan-out alone takes 8+ GB) | `RIG_HEAVY_OP_MIN_AVAILABLE_MB` |
| memory stall share | `/proc/pressure/memory` `some avg10` | above | 10% | `RIG_HEAVY_OP_MAX_PSI_SOME10` (0 < x < 100) |
| memory budget | `MemAvailable` + live-runner scan | left < headroom | headroom 4 GiB; costs: docker 8 GiB, review 3 GiB, other 3 GiB | `RIG_HEAVY_OP_HEADROOM_MB`, `RIG_HEAVY_OP_COST_REVIEW_MB` / `_DOCKER_MB` / `_SUITE_MB` |
| swap headroom | `SwapFree` / `SwapTotal` | below, AND the budget leaves < 2 × headroom | 30% free (skipped with no swap) | `RIG_HEAVY_OP_MIN_SWAP_FREE_PCT` (1..99) |
| heavy runners already live (hard ceiling) | `/proc/*/cmdline` scan | at or above | 5 | `RIG_HEAVY_OP_MAX_LIVE` (>= 1) |

**The memory budget** admits a new heavy operation only if

    MemAvailable − Σ over live runners max(0, cost(runner) − PSS(runner tree)) − cost(new op) ≥ headroom

A live runner reserves what it has not grown into yet: four reviews whose seats still sit at
300 MB reserve 4 × 2.7 GiB, so a `pytest` is refused with 10 GiB available, while two test
runners next to 23 GB available leave room for a review. A review's 3 GiB is measured: review-cli's
own sampler saw a whole review tree (seats included) peak at p50 678 MB, p95 1125 MB, max 3195 MB
over 294 runs on the 48 GB workstation (2026-10-13..15). Both incidents were the third/fourth
concurrent suite; a fixed cap of 2 caught them, but also blocked an idle 48 GB box on 2026-10-14
(23 GB available, PSI 0). The ceiling of 5 is a backstop for wrong cost estimates. The budget
needs the scan, so it is Linux only. A tree's footprint is its PSS (`/proc/<pid>/smaps_rollup`),
not RSS: RSS counts pages shared by forked workers once per process, which inflated the
footprint and shrank the reservation. Where PSS is unreadable (another user's or root's processes)
the largest single RSS in the tree stands in — summed RSS would shrink the reservation again, 0
would reserve the full cost.

**Swap** is checked only while the budget leaves less than twice the headroom: swap keeps stale
pages for hours — on 2026-10-14 it was 83% full with 37 GB `MemAvailable` and PSI 0 — and only
matters when RAM is short too, as on 2026-10-11. Without a live-runner scan (it failed) the
budget knows no reservations, so the swap check is unconditional then.

The live-runner scan is stateless (a read of the process table at that instant, never a counter
this hook maintains — see the next section for why). It counts one entry per runner tree —
`pytest`/`py.test`, `mypy`, `vitest`, `jest`, `bun test`, `cargo test`, `go test`, `make
test|check`, `docker build`, `tsc -b/--build`, and a `review` process with the
`claude`/`codex`/`opencode` seats it fans out (one entry per review; an interactive `claude` with
no `review` ancestor is not a seat) — and a launcher plus its runner (`uv run pytest` → `python -m
pytest` → xdist workers) counts once, its PSS summed over the tree. A
runner name counts only in an EXECUTABLE position (argv[0], or reached through interpreters,
launchers, wrappers and their options: `python -m pytest`, `uv run --with pytest pytest`, `node
…/vitest`); `grep pytest README.md` or `echo mypy` is not a runner. Each entry is attributed to
its omp session by walking the parent chain to the topmost `omp` process (a subagent's omp is a
child of its session's); the session title comes from `tg-ctl omp-live status --json` (what
`omp-steer list` prints; 2 s timeout) and is looked up ONLY on the block path — the allow path
never spawns a subprocess — falling back to `/proc/<pid>/cwd`, then to the pid alone. The block
message shows the budget arithmetic (`Memory budget: A MB available − R MB reserved for N live
runner(s) − C MB for this <op> = L MB left (headroom H MB)`) and lists the runners as `pid  PSS
runner  (reserves R MB)  ← omp <pid> (<cwd>) "<title>"` — also when another signal blocked, so
the agent knows who it is competing with. A real `runpod-run` process (its script argv word is
absolute and resolves to an installed copy; a bare or relative word is counted, since the scan
cannot pin its cwd or PATH) and everything under it (its ssh child carries the remote
suite's command line) is never counted: the suite runs on the pod. The scan itself takes ~20 ms on a 600-process box; an allow
decision stays under 100 ms.

Accepted residual: two agents that start a heavy op in the same instant both see the table
before either runner exists and both pass. Closing that needs a cross-process reservation —
exactly the leaking counter rejected below — and the incidents were third/fourth suites started
minutes apart, not simultaneous starts; the memory/PSI/swap signals and the per-session cgroups
cover the simultaneous case.

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
