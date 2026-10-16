#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — refuse to START a heavy operation while the
machine is genuinely under memory pressure.

WHY THIS EXISTS (2026-08-27/28 incident): a single long-running orchestrator session
had roughly 10-15 background agents running CONCURRENTLY, several doing heavy local
work at once — extension rebuilds (`build-and-install.sh`), multi-model `review`
passes (each spawning several `opencode run`/`codex exec` subprocess trees), full
test suites. Free memory on a 24GB machine dropped to ~65MB
(`vm_stat` showed ~4000 free pages at 16KB/page) and several real VS Code windows
hung on native "reload required" / "app not responding" modals. Nothing on the
machine was checking "is there room for one more heavy thing" before starting one.

THE HOOK ITSELF IS NOT A CONCURRENCY COUNTER AND NEVER WAITS. Every existing pre-bash
hook in this ecosystem decides ALLOW/BLOCK FAST (this file included) — a hook that
WAITS either blocks past the dispatcher's timeout and then silently ALLOWS anyway
(useless), or turns "machine busy" into a hard failure with no visibility. A
counting semaphore built from pid files or counters also needs a slot released by
whoever acquired it; an agent killed mid-operation (routine in this ecosystem —
session churn, `TaskStop`, a crash) leaks its slot forever, and reaping a stale slot
correctly is itself a hard, stateful problem — get it wrong and every agent on the
machine deadlocks on a phantom "queue" (see the "Double-block deadlock configs" /
"Never break depended-on tool" lessons this ecosystem has already paid for). So this
hook carries **zero persistent state of its own** — no counter, no queue position.
Every invocation re-reads the machine's REAL memory state at that instant and
decides fresh. Nothing to leak, nothing to go stale, nothing that can deadlock a
second hook.

The ONE piece of shared state it looks at is kernel-managed: the machine-wide
GPU-model slot pool (`lib/agenttools_gpu_slots`, agent-tools#817) — N `flock` lock
files a model run holds for its lifetime. The kernel drops an flock the instant its
holder dies (SIGKILL included), so that pool cannot leak a slot, which is exactly
what the rejected semaphore could not promise. The waiting lives in the model
command itself (the `agenttools_gpu_slots run -- <cmd>` wrapper, and stt-cli's own
acquire); this hook only PROBES the pool without waiting and refuses to start an
un-queued GPU model load while every slot is taken.

Signal 1 is macOS's own kernel jetsam pressure level
(`sysctl kern.memorystatus_vm_pressure_level`) — the exact value Activity Monitor's
"Memory Pressure" graph and the OS's own out-of-memory killer read. Raw free-page
counts alone are noisy on macOS (the OS deliberately keeps free low by using spare
RAM as disk cache — a healthy machine often shows a few hundred MB "free" with
plenty of headroom), so free pages are never judged on their own. The jetsam level
collapses that noise into three values: 1 = normal, 2 = warn, 4 = critical. This
hook blocks at WARN-or-worse by default (level >= 2) — deliberately proactive,
since the goal is to stop the SECOND heavy operation from tipping an already-
strained machine into the incident above. Tune via `RIG_HEAVY_OP_BLOCK_AT_LEVEL`.

Signal 2 (agent-tools#817) is the memory the jetsam level cannot see. On 2026-09-29
thirteen concurrent `whisper-cli` runs wired 24.65 of 25.77 GB (95.7%) into Metal
buffers and kernel-panicked the Mac with the jetsam level still at "normal":
wired memory is neither pageable nor compressible, so it never registers as
pressure. From `vm_stat` (macOS) or `/proc/meminfo` (Linux) the hook computes the
WIRED SHARE of RAM (file cache is never wired, so unlike raw free pages this has no
cache noise) and FREE PLUS RECLAIMABLE memory (free + inactive + speculative +
purgeable pages on macOS, `MemAvailable` on Linux — a healthy low-free Mac has GBs
of inactive/cache here; the panic had 112 MB). It blocks when wired share is above
`RIG_HEAVY_OP_MAX_WIRED_PCT` (default 75) or free-plus-reclaimable is below
`RIG_HEAVY_OP_MIN_AVAILABLE_MB` (default 512), whatever the jetsam level says.

Either signal alone is enough to block; when BOTH are unavailable (sandboxed or
unsupported platform) the hook has no reliable signal and ALLOWS (fail-open).

LINUX (2026-10-10 and 2026-10-11 incidents, shared 48 GB / 16 GB-swap workstation):
many omp sessions in parallel; three concurrent `review diff` runs (each up to four
fat agent CLIs), `uv run mypy` + `pytest` with testcontainers docker and several
`bun test` full suites lost 26 GB in two minutes. Swap had been 100% full since the
night before, so there was zero headroom; the kernel OOM killer never fired (the box
thrashed on file cache instead, MemAvailable floored at ~1.9 GB), systemd-oomd was
starved, and the machine needed a manual reset after 18 minutes. The old Linux
thresholds (512 MB available, uncalibrated) never fired. Linux therefore judges:
  * `MemAvailable` below max(8 GiB, 20% of MemTotal) — ~9.6 GB on the 48 GB box; one
    `review diff` fan-out alone takes 8+ GB, so less than that is no room for one more
    (`RIG_HEAVY_OP_MIN_AVAILABLE_MB`);
  * PSI `/proc/pressure/memory` `some avg10` above 10% — the kernel is already
    stalling tasks on reclaim (`RIG_HEAVY_OP_MAX_PSI_SOME10`);
  * a MEMORY BUDGET that does not cover the new operation (needs the live-runner scan,
    so Linux only): `MemAvailable` − Σ over the heavy runners ALREADY live of
    max(0, expected cost − its process tree's PSS) − the new operation's expected cost below
    `RIG_HEAVY_OP_HEADROOM_MB` (default 4 GiB). Expected costs: a `docker build` 8 GiB, a
    `review` run and any other suite/runner/type-check/GPU load 3 GiB
    (`RIG_HEAVY_OP_COST_REVIEW_MB` / `_DOCKER_MB` / `_SUITE_MB`). The live runners come
    from a stateless `/proc` scan for pytest / bun test / vitest / jest / mypy / tsc -b /
    cargo test / go test / make test|check / docker build and the fat agent CLIs a
    `review` process fans out (one entry per review, its seats summed). The reservation
    covers what a runner has not grown into yet: a review whose seats sit at 300 MB still
    reserves 2.7 GiB. Both incidents were the THIRD and FOURTH concurrent suite on a
    FULL swap; the budget rejects those because the earlier ones' reservations eat the
    available memory. The scan is a read of the live process table, not a counter:
    nothing is acquired, so nothing can leak (see above);
  * `RIG_HEAVY_OP_MAX_LIVE` (default 5) heavy runners already live — a hard backstop for
    when the cost estimates are wrong;
  * SwapFree below 30% of SwapTotal (`RIG_HEAVY_OP_MIN_SWAP_FREE_PCT`), but only while
    the budget leaves less than twice the headroom. Swap holds stale pages for hours:
    on 2026-10-14 swap was 83% full with 37 GB `MemAvailable` and PSI 0, and the
    unconditioned check blocked an idle machine; a full swap matters when RAM is short
    too (2026-10-11), and then the budget sees it. Without a live-runner scan the budget
    knows no reservations, so the swap check is unconditional then.
PSS (`/proc/<pid>/smaps_rollup`), not RSS: RSS counts a page shared by forked workers once
per process, which inflated a tree's footprint and shrank its reservation. The block
message shows the budget arithmetic and names each live runner with its pid, tree PSS, its
reservation and the omp session it belongs to, so the agent can tell the operator (or
`omp-steer`) who to wait for.
A `review` pass run by the INSTALLED review-cli whose admission controller is present
(review-cli#614) is handed to it: review-cli queues each run until host CPU/RAM and every
provider fit with 20% headroom, so this hook skips the budget, swap and live-runner-ceiling
checks for it and blocks it only on the emergency signals (MemAvailable floor, PSI limit,
wired share, jetsam level). Live reviews still count as runners in every OTHER heavy
operation's budget. See `review_cli_handoff` for how "installed" is pinned.
A run of ONE OR TWO test files (`bun test ./tests/x.test.ts -t foo`, `pytest a.py b.py::T`,
`go test ./pkg/foo_test.go`; see `small_test_run`) is gated the same way, on the emergency
signals only: it is the local workflow the block message itself recommends, and the
ceiling, budget and swap checks exist for whole suites (agent-tools#911: one test file was
blocked for "7 heavy runner(s) already live" on a box with tens of GB free).

"Heavy operation" is intentionally scoped to the operations actually implicated in
the incidents: the extension rebuild script (`build-and-install.sh`),
`@vscode/vsce package`, the multi-model `review` CLI's heavy subcommands
(`diff`/`quorum`/`brainstorm`/`just-ask` — each spawns multiple provider
subprocess trees), build/test-suite invocations (npm/pnpm/yarn/bun/deno/
cargo/go/make/rake/mvn/gradle/msbuild test|build|..., and the direct runners
playwright/cypress/jest/vitest/pytest), and GPU model loads (stt, whisper-cli,
whisper-server, llama-cli, llama-server, the mlx_lm/mlx_whisper entry points, or any
command handed a .bin/.gguf/.ggml/.safetensors model file). A pre-bash hook sees one
agent command once: `for f in *.wav; do stt "$f"; done` is checked when the loop
starts, not per iteration — the per-load cap for loops is the slot pool the model
command itself waits on.

Detection is TOKEN-based (`shlex.split`, exact token/adjacency matching), not a
raw substring search over the whole command string — deliberately NOT the
`enforce-timeout-on-bash` posture. That sibling hook's regex-over-raw-string
approach is safe there because it's advisory (warns and still allows); THIS hook
hard-blocks with no bypass, so the same approach would turn `git commit -m "make
all tests green"` or `task new --title "fix go build flake"` into a false-positive
hard block on a WARN-pressure machine (a real bug caught in review before this
hook shipped) — the quoted commit message becomes one shlex token ("make all
tests green"), not the bare word "make", so it never matches. The real siblings
that detect the SAME command classes (`no-long-inline-process`,
`subagent-no-bg-longproc`) are argv-aware for exactly this reason; this hook
doesn't port their full tokenizer (wrapper/env/job-boundary peeling) since it
doesn't need backgrounding semantics, just "don't match words trapped inside a
quoted argument" — plain `shlex.split` with exact-token comparison covers that.
An unparseable command (unbalanced quotes) falls back to whitespace-splitting,
which is still token-shaped (never a raw substring search).

No self-service bypass and deliberately NO Telegram hatch-escalation machinery
either (unlike most hard-block hooks in this ecosystem) — that machinery exists to
get a HUMAN JUDGMENT call on a decision only a human can make; whether the machine
currently has enough free memory is not a judgment call, it is an objective,
self-resolving fact that a plain retry re-measures. Wiring `tg-ctl` in here would
also reintroduce exactly the complexity/latency risk this hook was written to
avoid for a check that must stay fast and stateless. If pressure genuinely never
clears (every other agent is idle, nothing is going to free memory) that is itself
a signal worth telling the machine's operator about directly, not something to
hatch-approve past.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash or an unreadable pressure signal here must never wedge
an agent's ability to run a command — this is an availability circuit-breaker, not
a security boundary.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple, Optional

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# jetsam pressure levels (macOS kern.memorystatus_vm_pressure_level): 1=normal,
# 2=warn, 4=critical. Default blocks at WARN-or-worse; override for a looser or
# stricter posture without touching code.
DEFAULT_BLOCK_AT_LEVEL = 2
# The ONLY levels the sysctl actually reports (see module docstring). Restricting
# the env override to this set closes an otherwise-real bypass: an operator (or a
# confused agent) setting RIG_HEAVY_OP_BLOCK_AT_LEVEL=5 or =999 would make even
# jetsam level 4 (critical) pass, silently defeating the hook's own "no bypass"
# claim in `decide`'s block message.
_VALID_PRESSURE_LEVELS = frozenset({1, 2, 4})

# Signal 2 thresholds (agent-tools#817, see module docstring). 75% wired leaves a quarter
# of RAM for everything pageable; the 2026-09-29 panic sat at 95.7%. 512 MB free plus
# reclaimable is the cliff edge (the panic had 112 MB); a healthy low-free Mac has GBs.
DEFAULT_MAX_WIRED_PCT = 75
DEFAULT_MIN_AVAILABLE_MB = 512
_MIB = 1024 * 1024

# Linux thresholds (2026-10-10/11 thrash, see module docstring). The incidents never got
# MemAvailable under 1.9 GB and never tripped the 512 MB floor: Linux keeps reclaiming file
# cache long after the box is unusable. A review fan-out or a docker-backed suite takes
# 8+ GB, so "room for one more" is 8 GiB — or a fifth of RAM on a bigger box.
DEFAULT_LINUX_MIN_AVAILABLE_MB = 8192
DEFAULT_LINUX_MIN_AVAILABLE_PCT = 20
# PSI `some avg10`: the share of the last 10 s in which at least one task stalled on
# memory. 10% is where the thrash became visible on 2026-10-11 (before the 26 GB cliff).
DEFAULT_MAX_PSI_SOME10 = 10.0
# Swap was 100% full all night before 2026-10-11, so the first burst had nowhere to go.
# Checked only when the budget leaves less than 2 x the headroom (see module docstring).
DEFAULT_MIN_SWAP_FREE_PCT = 30
# The memory budget (see module docstring). Expected peak memory per class. A review run
# (whole process tree, seats included) measured by review-cli's own sampler over 294 runs on
# the 48 GB workstation (2026-10-13..15, review-cli costs.jsonl): peak RSS p50 678 MB, p95
# 1125 MB, max 3195 MB — 3 GiB is ~2.7x the p95 and just under the single worst run (a live
# review's own PSS is subtracted anyway). A docker build/docker-backed suite takes 8+ GB;
# a single test/type-check runner ~3 GiB. The headroom is the margin left AFTER the new
# operation's own cost is reserved, so it is smaller than the "room for one more" floor.
DEFAULT_COST_REVIEW_MB = 3072
DEFAULT_COST_DOCKER_MB = 8192
DEFAULT_COST_SUITE_MB = 3072
DEFAULT_HEADROOM_MB = 4096
# Hard backstop on concurrent heavy runners; the budget does the real admission control.
DEFAULT_MAX_LIVE = 5

# ── heavy-operation detection (token-based — see module docstring for why this
# hard-block hook does NOT use the raw-substring style of enforce-timeout-on-bash) ──
_SUITE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun", "deno", "cargo", "go", "make", "rake", "mvn", "gradle", "msbuild"})
_SUITE_SUBCOMMANDS = frozenset({"test", "build", "verify", "package", "all"})
_DIRECT_TEST_RUNNERS = frozenset({"playwright", "cypress", "jest", "vitest", "pytest"})
_REVIEW_HEAVY_SUBCOMMANDS = frozenset({"diff", "quorum", "brainstorm", "just-ask"})

# Shell chain/pipe/background operators that shlex.split leaves as their own
# tokens (when space-separated, the common style). Used to scope a
# `--help`/`-h` exemption to the SAME segment as the matched invocation — see
# `_segment_requests_help`. Includes `&` (backgrounds the preceding command —
# `review diff -C . & grep -h pattern` must NOT let the trailing `-h` exempt
# the backgrounded `review diff`) and `|&` (bash's stdout+stderr pipe) as well
# as the plain chain/pipe operators — a real code-review catch (P2) on the
# first version of this fix, which only covered `|`/`||`/`&&`/`;`.
_SHELL_BOUNDARY_TOKENS = frozenset({"|", "||", "&", "|&", "&&", ";"})

# ── GPU model loads (agent-tools#817) — each one wires its model into unpageable GPU
# memory the jetsam level never sees; 13 at once kernel-panicked a 24 GB Mac. ──
GPU_MODEL_LABEL = "GPU model load (speech-to-text / local model runtime)"
# Same load, but queued through the slot wrapper, which waits for a free slot itself —
# still memory-gated, never refused for "all slots taken".
GPU_MODEL_SLOTTED_LABEL = "GPU model load queued through the GPU slot wrapper"
_GPU_MODEL_COMMANDS = frozenset({
    "stt", "whisper-cli", "whisper-server", "llama-cli", "llama-server", "mlx_lm", "mlx_whisper",
})
_GPU_MODEL_ENTRY_PREFIXES = ("mlx_lm.", "mlx_whisper.")  # mlx_lm.generate, mlx_lm.server, ...
_MODEL_FILE_SUFFIXES = (".bin", ".gguf", ".ggml", ".safetensors")
_GPU_SLOT_WRAPPERS = frozenset({"agenttools_gpu_slots", "agenttools-gpu-slot"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# A simple command starting with one of these only lists/moves/hashes/downloads/builds
# model-named files, looks a runtime up, or inspects/signals processes, and never loads a
# model onto the GPU (`ls models/*.bin`, `curl -o ggml.bin ...`, `which whisper-cli`,
# `gcc -o fw.bin`). Process control matters most: `pkill -f whisper-cli` is the
# remediation for a runaway load and must never be refused while every slot is taken.
# Its command substitutions still run (`echo "$(stt a.wav)"`) and are classified on
# their own. `find` never loads a model either (`find -name '*.gguf'`), but the command
# its `-exec` runs can, so `_gpu_load_in_simple_command` classifies that command instead
# (`_FIND_EXEC_ACTIONS`). Deliberately excludes anything else that can run another
# program (`xargs`).
_NEVER_LOADS_A_MODEL = frozenset({
    "ls", "cat", "cp", "mv", "rm", "ln", "cd", "pushd", "du", "df", "stat", "file", "touch",
    "chmod", "chown", "mkdir", "shasum", "sha1sum", "sha256sum", "sha512sum", "md5", "md5sum",
    "b3sum", "cksum", "head", "tail", "less", "more", "xxd", "hexdump", "od", "wc", "curl",
    "wget", "echo", "printf", "test", "[", "git", "tar", "zip", "unzip", "gzip", "gunzip",
    "rsync", "scp", "open", "xattr", "basename", "dirname", "realpath", "readlink", "pip",
    "pip3", "pipx", "brew", "grep", "egrep", "fgrep", "rg", "diff", "cmp", "strings", "which",
    "whereis", "type", "man", "tree", "dd", "install", "truncate",
    "kill", "pkill", "killall", "pgrep", "pidof", "ps", "top", "lsof",
    "cc", "gcc", "g++", "clang", "clang++", "ld", "objdump", "nm", "otool", "strip",
})
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# Words that precede the real command word of a simple command.
_COMMAND_PREFIX_WORDS = frozenset({
    "do", "then", "else", "elif", "if", "while", "until", "!", "{", "time", "sudo", "env",
    "nohup", "nice", "command", "exec",
})
_SHELL_PUNCTUATION = "();<>|&\n"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _tokenize(command: str) -> list[str]:
    """Best-effort shell tokenization. Falls back to a bare whitespace split on
    unbalanced quotes — still token-shaped (never a raw substring search), so an
    unparseable command degrades to a slightly looser but still argv-like match
    rather than reopening the quoted-prose false-positive this replaced."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _segment_requests_help(tokens: list[str], start: int) -> bool:
    """True if `-h`/`--help` appears between `tokens[start]` and the next shell
    chain/pipe operator (or end of command) — i.e. in the SAME segment as the
    invocation matched at `start`. A real CLI's `-h`/`--help` short-circuits to
    printing usage and exiting, never actually starting the heavy work, so
    `review diff --help` (confirmed hard-blocking a genuinely non-heavy `--help`
    call in a 2026-09 incident, agent-tools follow-up) should never match.

    Deliberately SEGMENT-scoped, not "anywhere in the whole command": an
    unrelated tool's own `-h` further down a pipe (`review diff -C . | grep -h
    pattern`) must not spuriously exempt a segment that genuinely starts a
    heavy operation — see the module docstring on why a false NEGATIVE (a
    genuinely heavy op running unthrottled) is the more dangerous failure
    direction for this hard-block hook than an occasional unnecessary block.
    """
    for tok in tokens[start:]:
        if tok in _SHELL_BOUNDARY_TOKENS:
            break
        if tok in ("-h", "--help"):
            return True
    return False


_HEREDOC_OPENER = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _paren_group_end(line: str, start: int) -> int:
    """Index of the `)` closing the `(` at `start`, or the last index when unterminated."""
    depth = 0
    for j in range(start, len(line)):
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(line) - 1


def _heredoc_opener(line: str) -> Optional[re.Match[str]]:
    """The heredoc opener on `line` (group 1: the delimiter's quote, group 2: the
    terminator word), or None. A `<<WORD` counts only in an unquoted, non-comment
    position: `git commit -m "fix << EOF"` is literal text. A here-string (`<<< word`)
    and a shift inside arithmetic (`$((1<<n))`, `(( m = 1<<bits ))`) are not openers:
    treating them as one would drop every later line as a "body" and hide a model load
    there. Derived from no-shell-file-edit's `_heredoc_terminator`, which does not skip
    those two yet."""
    quote: Optional[str] = None
    prev_space = True  # start-of-line is a word boundary for a leading `#`
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(line):
                i += 1
            prev_space = False
        elif ch in ("'", '"'):
            quote = ch
            prev_space = False
        elif ch == "\\" and i + 1 < len(line):
            i += 1
            prev_space = False
        elif ch == "#" and prev_space:
            return None  # the rest of the line is a comment
        elif ch == "(" and line[i : i + 2] == "((" or ch == "$" and line[i : i + 3] == "$((":
            i = _paren_group_end(line, i + (ch == "$"))  # arithmetic: `<<` is a shift
            prev_space = False
        elif ch == "<" and line[i : i + 3] == "<<<":
            while i + 1 < len(line) and line[i + 1] == "<":
                i += 1  # here-string: its word is data on stdin, no body follows
            prev_space = False
        elif ch == "<" and line[i : i + 2] == "<<":
            match = _HEREDOC_OPENER.match(line, i)
            if match:
                return match
            i += 1  # `<<` not followed by a word: keep looking for a real opener
            prev_space = False
        else:
            prev_space = ch.isspace()
        i += 1
    return None


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: a body is data on the command's stdin, not commands, so a PR
    body or notes file that mentions `stt a.wav` loads nothing. The body is kept when the
    opener line hands it to a shell (`bash <<EOF`, `cat <<EOF | sh`), which runs it. An
    unquoted delimiter (`<<EOF`, not `<<'EOF'`) expands the body like a double-quoted
    string, so its `$(…)` and backtick substitutions run and are kept, one per line. An
    unterminated body is dropped to the end, as the shell never runs it either. Mirrors
    no-shell-file-edit's `_strip_heredocs`."""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        opener = _heredoc_opener(line)
        if opener is not None and not any(_basename(word) in _SHELLS for word in _tokenize(line)):
            term, expands = opener.group(2), not opener.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                if expands:  # quotes are literal in a body; only the substitutions run
                    literal = lines[i].replace("'", " ").replace('"', " ")
                    out.extend(f"$({body})" for body in _command_subs(literal))
                i += 1
            if i < len(lines):
                out.append(lines[i])  # keep the terminator line
        i += 1
    return "\n".join(out)


def _strip_comments(command: str) -> str:
    """Drop shell comments: an unquoted `#` that starts a word, up to (not including)
    the newline that ends it, so the next line stays a separate command and a quoted
    `#` (`--prompt "#1"`) stays an argument."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            out.append(command[i : i + 2])
            i += 2
            continue
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            if end == -1:
                break
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _command_subs(command: str) -> list[str]:
    """The bodies of every `$(…)` and `` `…` `` command substitution the shell would run:
    unquoted or inside double quotes, never inside single quotes. The body runs as a command
    of its own before the command it sits in, so `echo "$(stt a.wav)"` loads a model although
    `echo` never does. Nested `$(…)` is depth-counted; backticks don't nest. Mirrors
    no-shell-file-edit's `_command_subs`."""
    bodies: list[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2  # an escaped char (`\$`, `\``) is literal, never a substitution
            continue
        if ch in ("'", '"') and quote is None:
            quote = ch
            i += 1
            continue
        if ch == '"' and quote == '"':
            quote = None
            i += 1
            continue
        if ch == "$" and i + 1 < len(command) and command[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < len(command) and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(command[i + 2 : j - 1] if depth == 0 else command[i + 2 :])
            i = j
            continue
        if ch == "`":
            j = command.find("`", i + 1)
            if j == -1:
                break
            bodies.append(command[i + 1 : j])
            i = j + 1
            continue
        i += 1
    return bodies


def _shell_tokens(command: str) -> list[str]:
    """shlex words and shell punctuation (`;` `&&` `|` `(` `()` `>&` newline …), quotes removed
    as the shell removes them. Unbalanced quotes degrade to a whitespace split."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"  # a newline separates commands, so it stays punctuation
    lexer.whitespace_split = True
    lexer.commenters = ""  # already stripped; shlex's own would swallow the newline too
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _is_shell_punctuation(token: str) -> bool:
    return bool(token) and all(ch in _SHELL_PUNCTUATION for ch in token)


def _simple_commands(command: str) -> list[list[str]]:
    """Split comment-free `command` into its simple commands' words: at `;` `&` `&&` `|`
    `||` `|&` `(` `)` and newlines, dropping redirect targets (`> out.txt`).
    Quote-aware via shlex; unbalanced quotes degrade to one whitespace-split command."""
    tokens = _shell_tokens(command)
    commands: list[list[str]] = []
    current: list[str] = []
    skip_redirect_target = False
    for tok in tokens:
        if _is_shell_punctuation(tok):
            # `>`, `2>&1`'s `>&`, `&>`: the next word is a file. `<(`/`>(` open a process
            # substitution instead, whose next word is a command — a boundary.
            if ("<" in tok or ">" in tok) and "(" not in tok:
                skip_redirect_target = True
                continue
            if current:
                commands.append(current)
            current = []
        elif skip_redirect_target:
            skip_redirect_target = False
        else:
            current.append(tok)
    if current:
        commands.append(current)
    return commands


def _is_gpu_model_word(word: str) -> bool:
    name = _basename(word.strip("`"))
    if name in _GPU_MODEL_COMMANDS or name.startswith(_GPU_MODEL_ENTRY_PREFIXES):
        return True
    leaf = _basename(word.rsplit("=", 1)[-1]).lower()  # `--model=/m/x.gguf` too
    return any(leaf.endswith(suffix) and len(leaf) > len(suffix) for suffix in _MODEL_FILE_SUFFIXES)


def _shell_script_arg(words: list[str], shell_at: int) -> Optional[str]:
    """The script of `sh [options] -c '<script>'` whose shell word is at `shell_at`,
    skipping options (`-eu`, `-o pipefail`, `--login`) before the `-c`; None when the
    shell runs a script file or reads stdin instead."""
    j = shell_at + 1
    while j < len(words):
        opt = words[j]
        if opt in ("-o", "+o"):
            j += 2
        elif opt == "--" or len(opt) < 2 or opt[0] not in "-+":
            return None
        elif opt[0] == "-" and opt[1] != "-" and "c" in opt[1:]:
            return words[j + 1] if j + 1 < len(words) else None
        else:
            j += 1
    return None


def _merge_gpu(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    """An un-queued load (False) outranks a queued one (True), which outranks none (None)."""
    if a is False or b is False:
        return False
    return True if (a or b) else None


def _asks_for_help(words: list[str], start: int) -> bool:
    """True when `-h`/`--help` is a flag of the command at `words[start]`: it follows the
    command word or a non-option word, and no `--` ends the options first. After an option
    it is that option's value (`llama-cli -p "-h"` prompts with "-h" and loads the model)."""
    for k in range(start + 1, len(words)):
        if words[k] == "--":
            return False
        if words[k] in ("-h", "--help") and (k == start + 1 or not words[k - 1].startswith("-")):
            return True
    return False


def _gpu_load_in_simple_command(words: list[str], queued: bool) -> Optional[bool]:
    start = 0
    while start < len(words) and (words[start] in _COMMAND_PREFIX_WORDS or _ASSIGNMENT.match(words[start])):
        start += 1
    if start == len(words):
        return None
    command_word = _basename(words[start])
    if command_word == "find":  # the command `-exec` runs is a simple command of its own
        found_exec: Optional[bool] = None
        for k in range(start + 1, len(words)):
            if words[k] in _FIND_EXEC_ACTIONS:
                found_exec = _merge_gpu(found_exec, _gpu_load_in_simple_command(words[k + 1 :], queued))
        return found_exec
    if command_word in _NEVER_LOADS_A_MODEL or _asks_for_help(words, start):
        return None
    found: Optional[bool] = None
    for i, word in enumerate(words):
        if _basename(word.rstrip("/")) in _GPU_SLOT_WRAPPERS and words[i + 1 : i + 2] == ["run"]:
            queued = True
        elif _basename(word) in _SHELLS:
            script = _shell_script_arg(words, i)  # `bash -c '<script>'`: classify the script
            if script is not None:
                return _merge_gpu(found, _gpu_load_in(script, queued))
        elif _is_gpu_model_word(word):
            found = _merge_gpu(found, queued)
    return found


def _gpu_load_in(command: str, queued: bool = False) -> Optional[bool]:
    """None: `command` loads no GPU model; True: every load in it is queued through the
    slot wrapper; False: at least one load would start without waiting for a slot.

    Matched per SIMPLE COMMAND, by argv (known model runtimes by basename, anywhere in
    the command so `cd x && stt a.wav` / `sudo whisper-cli` / `nohup stt` all match; or
    any model-file argument), so a loop body, a `bash -c` script, or a path-qualified
    binary is seen too. A simple command that starts with a pure file utility (`ls`,
    `shasum`, `curl -o`) or asks for `--help` loads nothing; its command substitutions
    (`echo "$(stt a.wav)"`) are classified on their own, in the calling shell's queue state.
    Heredoc bodies are stdin data, not commands, unless a shell reads them."""
    command = _strip_comments(_strip_heredocs(command))
    result: Optional[bool] = None
    for body in _command_subs(command):
        result = _merge_gpu(result, _gpu_load_in(body, queued))
    for words in _simple_commands(command):
        result = _merge_gpu(result, _gpu_load_in_simple_command(words, queued))
    return result


# ── offloaded commands: `runpod-run` (skill runpod-offload) ──────────────────────
# This hook's own block message sends a suite to `runpod-run -- <command>`: it rsyncs the
# checkout to a Runpod pod and runs `<command>` THERE over ssh, so its words describe remote
# work and the local process is an rsync + ssh. Exempted per SIMPLE COMMAND, so a heavy
# command chained around it (`runpod-run -- x && bun test`) and a command substitution the
# local shell expands into its argv (`runpod-run -- echo "$(bun test)"`) are still classified.
# Only the INSTALLED script is exempt (review findings: a name alone is spoofable — a shell
# function `runpod-run() { shift; "$@"; }`, or a `/tmp/runpod-run` that forwards to python —
# and a header check is forgeable, drifts with the script and would open a FIFO): the program
# must resolve, after symlinks, to the runpod-offload skill script rig installs
# (`~/.agents/skills/runpod-offload/runpod-run`) or the copy in this hook's own agent-tools
# checkout or one of its registered git worktrees. A bare name resolves via PATH (what the
# shell does), an absolute or `~/` path as is; a relative path is never exempt. A command that
# could re-point ANY name in its LOCAL part gets no exemption at all: one that defines a
# function (whatever its name: zsh expands `function runpod${dash}run(){…}` to `runpod-run`;
# review finding) or an alias, assigns PATH, or uses `hash -p`. What follows a runpod-run's
# `--` runs on the pod and is not scanned (`_local_text`). Residual: a function or PATH change made
# by an EARLIER call of a persistent shell, or by a sourced file, is invisible to a pre-bash
# hook; this is a memory gate, not a sandbox.
_OFFLOAD_COMMANDS = frozenset({"runpod-run"})
_OFFLOAD_SKILL = Path("runpod-offload") / "runpod-run"  # under a skills dir
_OFFLOAD_RIG_INSTALL = Path("~") / ".agents" / "skills" / _OFFLOAD_SKILL
_OFFLOAD_IN_CHECKOUT = Path("skills") / "universal" / _OFFLOAD_SKILL
# Re-pointing syntax, matched on the LOCAL tokens (quotes removed, as the shell removes them
# from a function or alias name; a runpod-run's remote tail cut, see `_local_text`).
_DEFINES_FUNCTION = r"\bfunction\s|\(\s*\)\s*[{(]"  # `function x`, `x()`, `x ( ) {`
_DEFINES_ALIAS = r"\balias\b"
# PATH, zsh's `path` array (not a `--path=` flag), `fpath`/`FPATH` (autoload lookup) and HOME
# (which `~` and config lookups follow), also element-wise (`path[1]=…`, `PATH[0]=…`), and any
# of them named as an argument of a declaration/array builtin (`set -A path …`, `typeset path`,
# `export PATH`, `unset path`, zsh `integer`/`float`; review findings). The argument scan runs
# to the end of the simple command, so a name inside an unrelated value (`export M="PATH"`)
# also revokes the exemption — deliberately erring toward gating.
_REPOINTABLE_NAMES = r"(?:PATH|path|FPATH|fpath|HOME|XDG_CONFIG_HOME)"
_ASSIGNS_NAME = r"(?:^|[^A-Za-z0-9_-])" + _REPOINTABLE_NAMES + r"(?:\[[^\]]*\])?\+?="
_DECLARES_NAME = (
    r"\b(?:set|typeset|declare|local|export|readonly|unset|integer|float)\s"
    r"(?:[^;&|]*?\s)?" + _REPOINTABLE_NAMES + r"(?![A-Za-z0-9_-])"
)
_PINS_A_PATH = r"\bhash\s+(?:-\S+\s+)*-p\b"
_ZSH_FUNCTIONS_TABLE = r"\bfunctions\["  # zsh `functions[name]='body'`
_REPOINTING = re.compile(
    "|".join((
        _DEFINES_FUNCTION, _DEFINES_ALIAS, _ASSIGNS_NAME, _DECLARES_NAME, _PINS_A_PATH,
        _ZSH_FUNCTIONS_TABLE,
    ))
)
# Programs that run shell code this hook cannot see, which may define functions or change
# PATH (`source ~/x; runpod-run -- bun test`, `builtin source ~/x`; review findings).
_RUNS_UNSEEN_SHELL_CODE = frozenset({"source", ".", "eval", "autoload", "emulate", "builtin"})
# Wrapper options (`timeout -k 30`, `nice -n 5`, `sudo -u x`, `env -u X`) whose value is the
# next word, so the value is not mistaken for the program. The `/proc` side of the same walk
# is `_is_offload_process`, which skips `_LIVE_PASSTHROUGH_EXES` words instead (an argv has
# no assignments-as-shell-syntax or shell keywords); keep the two in step.
_WRAPPER_VALUE_OPTIONS = frozenset({
    "-n", "-s", "-k", "-u", "-g", "-C", "--signal", "--kill-after", "--adjustment", "--unset",
    "--user", "--group", "--chdir",
})


def _git_common_dir(root: Path) -> Optional[Path]:
    """The shared `.git` dir of the repository `root` is a checkout of: `root/.git` itself, or,
    for a linked worktree (`.git` is a `gitdir: <common>/worktrees/<name>` file), `<common>`."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        pointer = dot_git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not pointer.startswith("gitdir:"):
        return None
    return Path(os.path.realpath(root / pointer[len("gitdir:"):].strip())).parents[1]


def _install_and_worktrees(root: Path) -> set[Path]:
    """`root`, its repository's main checkout and every worktree git has registered for it
    (`<common>/worktrees/*/gitdir` names each one's `.git` file): the checkouts a trusted
    install may run from. A directory merely placed under `.worktrees/` is not one (review
    finding); `root` being a linked worktree itself works too (review finding)."""
    checkouts = {root}
    common = _git_common_dir(root)
    if common is None:
        return checkouts
    checkouts.add(common.parent)
    try:
        entries = list((common / "worktrees").iterdir())
    except OSError:
        return checkouts
    for entry in entries:
        try:
            gitdir = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        checkouts.add(Path(os.path.realpath(gitdir)).parent)
    return checkouts


def _trusted_offload_scripts() -> set[str]:
    """Real paths of the runpod-run scripts the exemption trusts (see the block comment): the
    rig install, and the copy in this checkout or one of its registered git worktrees (the
    same reach the review-cli handoff gives review-cli's)."""
    checkout = Path(__file__).resolve().parents[2]
    candidates = [Path(os.path.expanduser(_OFFLOAD_RIG_INSTALL))]
    candidates += [root / _OFFLOAD_IN_CHECKOUT for root in _install_and_worktrees(checkout)]
    return {os.path.realpath(path) for path in candidates}


def _resolve_program(program: str) -> Optional[str]:
    """The real path a command word runs: a bare name via PATH (what the shell does), an
    absolute or `~/` path as is, symlinks resolved; None for a relative path (it depends on
    a cwd this hook cannot pin) or a name not on PATH. No file is opened."""
    if "/" not in program:
        program = shutil.which(program) or ""
    elif not program.startswith(("/", "~/")):
        return None
    return os.path.realpath(os.path.expanduser(program)) if program else None


def _is_trusted_offload_program(program: str) -> bool:
    """`program` — a command word or argv word naming runpod-run — is the installed script."""
    return _resolve_program(program) in _trusted_offload_scripts()


def _local_text(command: str, remote_from: Callable[[list[str]], int]) -> str:
    """`command`'s tokens, space-joined, without what runs elsewhere: each simple command's
    words from `remote_from(words)` on (a runpod-run's `-- <command>` runs on the pod, so a
    `PATH=` or function there re-points nothing here; review finding)."""
    out: list[str] = []
    words: list[str] = []
    for tok in _shell_tokens(command) + [";"]:
        if _is_shell_punctuation(tok):
            out += words[:remote_from(words)] if words else []
            out.append(tok)
            words = []
        else:
            words.append(tok)
    return " ".join(out)


def _without_simple_commands(
    command: str,
    hint: str,
    matches: Callable[[list[str]], bool],
    remote_from: Callable[[list[str]], int] = len,
) -> str:
    """`command` without the simple commands `matches` accepts, or unchanged when it has
    none (`hint` must occur in it at all — a cheap pre-filter). Every other simple command,
    and every command substitution (the local shell runs those even inside a matched
    command's argv), is kept, `;`-separated so the segment-scoped `--help` exemption cannot
    leak across them, for the usual classification. A command whose local part could
    re-point a name (`_REPOINTING` over `_local_text`) is returned unchanged."""
    cleaned = _strip_comments(_strip_heredocs(command))
    if hint not in command or _REPOINTING.search(_local_text(cleaned, remote_from)):
        return command
    commands = _simple_commands(cleaned)
    if any(_program_word(words) in _RUNS_UNSEEN_SHELL_CODE for words in commands):
        return command
    dropped = [matches(words) for words in commands]
    if not any(dropped):
        return command
    kept = [shlex.join(words) for words, drop in zip(commands, dropped) if not drop]
    kept += [_without_simple_commands(body, hint, matches, remote_from) for body in _command_subs(cleaned)]
    return " ; ".join(part for part in kept if part)


def _program_index(words: list[str]) -> int:
    """Index of the program a simple command runs: past env assignments, the prefix words
    `_COMMAND_PREFIX_WORDS` names, `timeout DURATION`, and those wrappers' options."""
    i = 0
    while i < len(words):
        word = words[i]
        if not (word in _COMMAND_PREFIX_WORDS or word == "timeout" or _ASSIGNMENT.match(word)):
            return i
        i += 1
        if _ASSIGNMENT.match(word):
            continue
        while i < len(words) and words[i].startswith("-"):
            option = words[i]
            i += 1
            if option == "--":
                break
            if option in _WRAPPER_VALUE_OPTIONS:
                i += 1
        if word == "timeout" and i < len(words) and _LIVE_DURATION.match(words[i]):
            i += 1
    return i


def _program_word(words: list[str]) -> str:
    """The program word of a simple command (`_program_index`), or "" when there is none."""
    i = _program_index(words)
    return words[i] if i < len(words) else ""


# Wrappers that run the program as another user, in another environment or through another
# PATH (sudo's `secure_path`, `command -p`'s default PATH) and read another HOME: no
# exemption, no handoff behind them (review findings).
_IDENTITY_WRAPPERS = frozenset({"sudo", "doas", "env", "su", "runuser", "pkexec"})


def _behind_identity_wrapper(words: list[str], program_at: int) -> bool:
    prefix = words[:program_at]
    default_path = "command" in prefix and any(w.startswith("-") and not w.startswith("--") and "p" in w for w in prefix)
    return default_path or any(word in _IDENTITY_WRAPPERS for word in prefix)


def _is_offloaded(words: list[str]) -> bool:
    """The simple command runs the installed `runpod-run` script (see `_OFFLOAD_COMMANDS`)."""
    i = _program_index(words)
    return (
        i < len(words)
        and _basename(words[i]) in _OFFLOAD_COMMANDS
        and not _behind_identity_wrapper(words, i)
        and _is_trusted_offload_program(words[i])
    )


def _offload_remote_from(words: list[str]) -> int:
    """Where an installed runpod-run's remote command starts (after its `--`); `len(words)`
    for anything else, which runs here in full."""
    if not _is_offloaded(words):
        return len(words)
    i = _program_index(words)
    return words.index("--", i + 1) + 1 if "--" in words[i + 1:] else len(words)


def _drop_offloaded_commands(command: str) -> str:
    """`command` without its offloaded `runpod-run` simple commands (`_without_simple_commands`)."""
    return _without_simple_commands(command, "runpod-run", _is_offloaded, _offload_remote_from)


# ── review-cli handoff: review-cli admits its own runs (review-cli#614) ────────────
# review-cli's admission controller (`reviewlib/admission.py`) queues every review run until
# the host's projected CPU and RAM, and each AI provider's concurrency, fit with 20%
# headroom. A review this hook would block on the budget, the swap check or the live-runner
# ceiling is therefore QUEUED there instead — two gates for the same thing only made agents
# retry. Such a review is still blocked here on the emergency signals (MemAvailable floor,
# PSI limit, wired share, jetsam level), which mean "do not start anything now".
# Same rigor as runpod-run: the `review` program must resolve (PATH lookup or absolute/`~/`
# path, symlinks resolved) to `<root>/bin/review` of the review-cli the operator installed
# — the checkout `~/.local/bin/review` resolves to — or of one of its registered git
# worktrees, with no function/alias/PATH/`hash -p` re-pointing in the command. Whether
# admission is on is asked from review-cli ITSELF: its `reviewlib.admission.load_settings()`
# (its own YAML and env semantics, ~0.1 s, review path only) must report `enabled`; a
# checkout without it, or any failure there, means no handoff (review finding: a regex over
# its config missed valid YAML and would miss a renamed key). It is asked with THIS
# environment, so a command that may change what the review process sees gets no handoff:
# one that mentions REVIEW_ADMISSION at all (`=0`, `=$OFF`, `unset`, `env -u`, `export` in
# any order; review findings), or a review prefixed with any assignment or `env` option
# (`HOME=… review` reads another config) or behind `sudo`/`env`. Residuals of this memory gate
# (not a sandbox): a `/proc` hidden by `hidepid`, worktree metadata forged inside a repo's
# `.git`, a `load_settings()` that hangs past the hook timeout (`on_error: open`), review-cli
# itself proceeding without admission when its registry lock cannot be had (its documented
# fallback), and shell code nested in `sh -c '…'`, which this hook does not parse.
REVIEW_LABEL = "multi-model review-cli pass"
_REVIEW_CLI_LINK = Path("~") / ".local" / "bin" / "review"


def _review_cli_root(program: str) -> Optional[Path]:
    """The review-cli checkout `program` runs from, when it is the installed one or one of its
    registered worktrees; else None."""
    real = _resolve_program(program)
    installed = Path(os.path.realpath(os.path.expanduser(_REVIEW_CLI_LINK)))
    if real is None or not installed.is_file():
        return None
    if not (_is_bin_review(Path(real)) and _is_bin_review(installed)):
        return None
    root = Path(real).parents[1]
    return root if root in _install_and_worktrees(installed.parents[1]) else None


def _is_bin_review(path: Path) -> bool:
    return path.name == "review" and path.parent.name == "bin"


def _review_cli_admission_on(root: Path) -> bool:
    """review-cli's own answer: `reviewlib.admission.load_settings().enabled`, imported from
    `root` (this hook is a one-shot process, so the import stays). False when it is missing,
    imports from elsewhere, or fails in any way (said on stderr). Anything it prints goes to
    stderr: stdout carries only this hook's protocol JSON."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            sys.path.insert(0, str(root))
            try:
                admission = importlib.import_module("reviewlib.admission")
            finally:
                sys.path.remove(str(root))
            if Path(admission.__file__).resolve().parents[1] != root:
                warn(f"review-cli admission: reviewlib was already imported from elsewhere, not {root}")
                return False
            return bool(admission.load_settings().enabled)
    except (Exception, SystemExit) as exc:
        warn(f"review-cli admission: could not read {root}'s settings ({exc!r}) — gating the review here")
        return False


def review_cli_handoff(command: str) -> Optional[str]:
    """None when `command`'s review passes are not all handed to review-cli's admission
    controller (see the block comment); else what is left of `command` without them, for
    classification ("" when nothing is). All handed-off passes must run from one checkout."""
    if "REVIEW_ADMISSION" in command:
        return None
    roots: set[Path] = set()  # the checkouts the handed-off passes run from; must be one

    def admitted(words: list[str]) -> bool:
        """`matches` for `_without_simple_commands`; records each admitted pass's checkout."""
        i = _program_index(words)
        if i + 1 >= len(words) or _basename(words[i]) != "review" or words[i + 1] not in _REVIEW_HEAVY_SUBCOMMANDS:
            return False
        if _behind_identity_wrapper(words, i) or any(_ASSIGNMENT.match(word) or word.startswith("-") for word in words[:i]):
            return False  # its environment may differ from the one review-cli is asked with
        root = _review_cli_root(words[i])
        if root is None:
            return False
        roots.add(root)
        return True

    # `_offload_remote_from`: a runpod-run's remote tail elsewhere in the command runs on the
    # pod, so re-pointing syntax there must not stop the handoff either.
    rest = _without_simple_commands(command, "review", admitted, _offload_remote_from)
    if rest == command or len(roots) != 1:
        return None
    return rest if _review_cli_admission_on(next(iter(roots))) else None


def classify_heavy_operation(command: str) -> Optional[str]:
    """Return a short human label for the heavy operation `command` runs, or None.

    Kept free of subprocesses (no sysctl, no `/proc` scan) so it's directly unit-testable;
    its only I/O is the `runpod-run` provenance check (a PATH lookup and realpath, only for a
    command that names runpod-run). Token-based: a heavy-operation word trapped
    inside a MULTI-WORD quoted argument (a commit message, a `--title`) is one
    shlex token and never matches a bare-word check — see module docstring. A
    SINGLE-WORD quoted argument (`git commit -m "pytest"`) is NOT distinguishable
    from a real bare invocation after shlex strips its quotes — that ambiguity,
    AND the analogous ambiguity for an unrelated command's PATH ARGUMENT that
    happens to share a runner's basename (`git add ./vitest`), are documented,
    accepted trade-offs (same posture as `echo review diff`, see
    `test_bare_unquoted_tokens_are_still_flagged_documented_edge_case`), not bugs
    this function tries to close. A stricter, command-position-aware version
    was tried and reverted after review found it traded this false-positive
    class for a worse false-NEGATIVE class (`cd /repo && pnpm test`, `sudo
    pytest`, `env CI=1 npm test`, `npm --prefix x test` all stopped matching) —
    for a hard-block memory-safety gate, silently ALLOWING a genuinely heavy
    operation is the more dangerous failure direction than an occasional
    unnecessary block, so the loose-membership version is the deliberate choice
    here, not an oversight.
    """
    command = _drop_offloaded_commands(command)
    # GPU loads first: their label is what turns on the machine-wide slot check.
    gpu = _gpu_load_in(command)
    if gpu is False:
        return GPU_MODEL_LABEL
    if gpu is True:
        return GPU_MODEL_SLOTTED_LABEL

    tokens = _tokenize(command)
    basenames = [_basename(t) for t in tokens]

    for i, b in enumerate(basenames):
        if b == "build-and-install.sh" and not _segment_requests_help(tokens, i):
            return "VS Code extension rebuild/package"
    for i, tok in enumerate(tokens[:-1]):
        if _basename(tok) == "vsce" and tokens[i + 1] == "package" and not _segment_requests_help(tokens, i):
            return "VS Code extension rebuild/package"
    for i, tok in enumerate(tokens[:-1]):
        if (
            _basename(tok) == "review"
            and tokens[i + 1] in _REVIEW_HEAVY_SUBCOMMANDS
            and not _segment_requests_help(tokens, i)
        ):
            return REVIEW_LABEL

    # Match on BASENAMES, not raw tokens (review-caught P2): the earlier
    # `set(tokens) & _SUITE_RUNNERS` / `& _DIRECT_TEST_RUNNERS` held only bare
    # names ("npm", "vitest", ...), so a path-qualified real invocation like
    # `/usr/bin/npm test` or `./node_modules/.bin/vitest run` never matched and
    # silently bypassed the gate entirely — a false NEGATIVE, the dangerous
    # direction (a genuinely heavy op runs unthrottled), unlike the accepted
    # false-positive ambiguity documented above. Kept as membership over ALL
    # basenames (not anchored to a resolved executable position) so wrapper
    # invocations `npx <tool>` / `pnpm dlx <tool>` / `npm run <script>` /
    # `sudo <tool>` / `cd x && <tool>` — where the real runner/subcommand isn't
    # in position 0 or 1 — still match, same as before this fix.
    basename_set = set(basenames)
    if basename_set & _SUITE_RUNNERS and basename_set & _SUITE_SUBCOMMANDS:
        return "build/test suite"
    if basename_set & _DIRECT_TEST_RUNNERS:
        return "test-suite runner"
    return None


# ── one or two test files: emergency signals only (agent-tools#911) ───────────────
# The block message told agents to "locally run one test file at most"; the live-runner
# ceiling, budget and swap checks then refused exactly that (2026-10-15 13:44Z: `bun test
# ./tests/session-topic-integration.test.ts -t …` blocked for "7 heavy runner(s) already
# live"). Such a run is gated on the emergency signals only, like a review handed to
# review-cli. The operand rule is runpod-offload-gate's (`_pytest_verdict` and friends,
# copied: hooks are standalone stdlib scripts): an existing path is judged by what it is; a
# path that does not exist here (another checkout, a `cd` earlier in the chain) is a
# directory when its last component has no extension. No operand at all is the whole suite.
_SMALL_RUN_MAX_FILES = 2
# The `classify_heavy_operation` labels a test run gets; anything else (a GPU load, a review,
# an extension rebuild) is never a small test run.
_SMALL_RUN_LABELS = frozenset({"build/test suite", "test-suite runner"})
# Wrappers `_program_index` does not peel (`nice -n19 ionice -c3 uv run …`, the invocation
# this repo's AGENTS.md prescribes), each with its value-taking options (`setsid` has none).
_SMALL_RUN_WRAPPERS = {
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "setsid": frozenset(),
}
# An operand path the shell may expand into many words (`tests/*.py`, `tests/{a,b,c}.py`,
# `$FILES`): its pre-shell spelling says nothing about how many files run (review finding). A
# pytest node id's `[param]` after `::` is not checked.
_SHELL_EXPANSION_CHARS = frozenset("*?[{$")


class _RunnerOptions(NamedTuple):
    """The options a small run may carry — an ALLOWLIST, so a mode that collects more than the
    files named (`pytest --pyargs`, `jest --findRelatedTests`/`--projects`, `vitest --workspace`,
    `go test -args`; review findings) or anything else unknown means full checks. `flags` take no
    separate value (a glued `--bail=3` is fine); `values` take one, separate or glued (`-k slow`,
    `-kslow`, `--tb=short`). Name filters, verbosity, fail-fast and timeouts only."""

    flags: frozenset[str]
    values: frozenset[str]


_RUNNER_OPTIONS = {
    "pytest": _RunnerOptions(
        frozenset({
            "-q", "-v", "-x", "-s", "-l", "--quiet", "--verbose", "--exitfirst", "--lf",
            "--last-failed", "--ff", "--failed-first", "--nf", "--new-first", "--sw", "--stepwise",
            "--stepwise-skip", "--no-header", "--no-summary", "--showlocals", "--disable-warnings",
            "--runxfail", "--strict-markers",
        }),
        # No `-o`/`--override-ini`/`-p`: `-o addopts=tests/` or a plugin can add a whole tree
        # (round-3 review finding). No `-n`/`--numprocesses`: xdist forks a worker per CPU
        # (round-4 review finding).
        frozenset({
            "-k", "-m", "-W", "-r", "--tb", "--maxfail", "--durations", "--timeout",
            "--color", "--capture", "--import-mode",
            "--basetemp", "--junitxml", "--junit-xml", "--log-level", "--log-cli-level", "--deselect",
        }),
    ),
    "bun test": _RunnerOptions(
        frozenset({"--bail", "--only", "--todo", "--update-snapshots", "--coverage"}),
        frozenset({"-t", "--test-name-pattern", "--timeout"}),
    ),
    "vitest": _RunnerOptions(
        frozenset({"--run", "--silent", "--passWithNoTests", "--update", "-u", "--allowOnly", "--globals", "--bail"}),
        frozenset({"-t", "--testNamePattern", "--reporter", "--retry", "--testTimeout", "--hookTimeout"}),
    ),
    "jest": _RunnerOptions(
        frozenset({
            "--ci", "--runInBand", "-i", "--silent", "--verbose", "--bail", "-b", "--no-cache",
            "--detectOpenHandles", "--forceExit", "--passWithNoTests", "-u", "--updateSnapshot",
            "--colors", "--json", "--useStderr",
        }),
        frozenset({"-t", "--testNamePattern", "--testTimeout", "--outputFile"}),
    ),
    # Go's flag package spells every option with one or two dashes; matched without them. No
    # `-parallel`/`-p`/`-cpu`: worker fan-out, like pytest's `-n` (round-7 review finding).
    "go test": _RunnerOptions(
        frozenset({"v", "race", "failfast", "json", "cover", "short"}),
        frozenset({"run", "skip", "count", "timeout", "tags", "shuffle", "vet", "coverprofile", "covermode"}),
    ),
}
# vitest subcommands other than `run` (`related` runs every test importing a file; `bench`,
# `watch`, `dev` stay resident; review finding): never small.
_VITEST_OTHER_SUBCOMMANDS = frozenset({"related", "bench", "watch", "dev", "list", "init", "typecheck"})
# Launchers whose `run` subcommand executes its operand, with their value-taking options, and
# the options that move the runner to another directory (`uv run --directory sub pytest
# suite.py` resolves `suite.py` there; round-3 review finding): with one of those, never small.
_SMALL_RUN_LAUNCHERS = {
    "uv": frozenset({
        "--with", "-w", "--with-requirements", "--with-editable", "--python", "-p", "--project",
        "--directory", "--group", "--only-group", "--extra", "--env-file", "--index",
        "--default-index", "--find-links", "-f", "--package", "--exclude-newer", "--config-file",
        "--cache-dir", "--index-url", "-i", "--extra-index-url", "--constraints", "-c",
        "--build-constraints", "-b", "--overrides", "--link-mode", "--resolution", "--prerelease",
        "--python-preference", "--color", "--config-setting", "-C",
    }),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
}
_LAUNCHER_CWD_FLAGS = {"uv": frozenset({"--directory"}), "poetry": frozenset({"-C", "--directory"})}
# `PYTEST_ADDOPTS=tests/ pytest a.py` prepends `tests/` to pytest's arguments and
# `PYTEST_PLUGINS=x` loads a plugin that may add more (round-3/5 review findings): a command that
# mentions any `PYTEST_` variable, or a hook environment that carries one, is never small. A
# variable exported by an EARLIER tool call lives only in the agent's shell, which a pre-bash hook
# cannot see (residual of a memory gate). So is a pytest run under a config file whose `addopts`
# carries anything but options (`addopts = tests/`; round-4 review finding).
_PYTEST_ENV_PREFIX = "PYTEST_"
_PYTEST_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
_PYTEST_CONFIG_FILES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
_ADDOPTS_LINE = re.compile(r"^\s*addopts\s*=\s*(.*)$")
# jest's operands are regexes and vitest's and `bun test`'s substring filters (bun's only when
# not spelled as a path, `./x.test.ts`): `jest .+` or `vitest run .test.ts` runs every test
# (round-4 review finding). Such an operand must be a literal test-file name (not `.test.ts`)
# that exists here — or, where the directory is unknown or does not exist here (`cd sub && bun
# test x.test.ts` from another checkout), just the literal name. Residual (round-5 review): a
# filter is unanchored, so `a.test.ts` also runs `data.test.ts` and `src/a.test.js` also
# `src/a.test.jsx` — a few more files, never the whole tree.
_PATTERN_RUNNERS = frozenset({"jest", "vitest", "bun test"})
_LITERAL_PATH = re.compile(r"^[\w./@-]+$")
_JS_TEST_FILE = re.compile(r"^[\w@-][\w.@-]*\.[cm]?[jt]sx?$")
# `cd`/`pushd` before the runner move where its operands resolve; a target that cannot be
# resolved statically leaves only the extension rule (`_is_directory_operand` with no cwd). The
# walk is linear, so it trusts only a leading chain of directory changes joined by `&&`/`;`. A
# directory change anywhere else — in a subshell, a pipeline, the background, behind `||`, or
# after another command that may fail and skip it (`false && cd sub; bun test x.test.ts`) —
# leaves no telling where the runner runs: never small (round-4/6 review findings).
_CHANGES_DIRECTORY = frozenset({"cd", "pushd", "popd"})
_UNSEQUENCED = frozenset({"(", ")", "||", "|", "&", "|&"})
_PYTHON_EXE = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)?)?$")
_PYTHON_VALUE_OPTS = frozenset({"W", "X"})  # `-W ignore`, `-X dev`; every other short option is a boolean
_MAX_LAUNCHER_NESTING = 8


def _skip_options(rest: list[str], value_flags: frozenset[str]) -> int:
    """Index of the first word of `rest` that is not an option: a value-taking option skips its
    separate value; a glued `--flag=value` and any other `-x` skip one; `--` ends the options."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return i + 1
        if not tok.startswith("-"):
            return i
        if "=" not in tok and tok in value_flags:
            i += 1
        i += 1
    return i


def _python_module_argv(argv: list[str]) -> Optional[list[str]]:
    """`<module> <args…>` for `python -m <module>` (`python3 -um pytest`, `python -Wignore -m
    pytest`), else None: a script path, `-c code`, stdin or a bare interpreter."""
    if not _PYTHON_EXE.match(_basename(argv[0])):
        return None
    rest = argv[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok.startswith("-") or tok in ("-", "--"):
            return None
        if tok.startswith("--"):
            i += 1
            continue
        for j, ch in enumerate(tok[1:]):
            glued = tok[j + 2:]
            if ch == "m":
                return [glued, *rest[i + 1:]] if glued else (rest[i + 1:] or None)
            if ch == "c":
                return None
            if ch in _PYTHON_VALUE_OPTS:
                if not glued:
                    i += 1
                break
        i += 1
    return None


def _peel_program(words: list[str]) -> list[str]:
    """The argv a simple command runs: past `_program_index`'s prefixes and the
    `_SMALL_RUN_WRAPPERS` it does not know (`nice -n19 ionice -c3 uv run …`)."""
    for _ in range(_MAX_LAUNCHER_NESTING):
        words = words[_program_index(words):]
        if not words or _basename(words[0]) not in _SMALL_RUN_WRAPPERS:
            return words
        words = words[1:][_skip_options(words[1:], _SMALL_RUN_WRAPPERS[_basename(words[0])]):]
    return words


def _test_runner_operands(argv: list[str], depth: int = 0) -> Optional[tuple[str, list[str]]]:
    """`(runner, arguments)` when `argv` runs pytest, `bun test`, vitest, jest or `go test` —
    directly or through `uv run`/`poetry run`/`python -m` — else None."""
    argv = _peel_program(argv)
    if not argv or depth >= _MAX_LAUNCHER_NESTING:
        return None
    exe, rest = _basename(argv[0]), argv[1:]
    if exe in ("pytest", "py.test"):
        return "pytest", rest
    if exe == "jest":
        return "jest", rest
    if exe == "vitest":
        # Without `run`/`--run` vitest may start in watch mode and stay resident (round-3 review
        # finding); `related`/`bench`/`watch`/`dev` never run just the files named.
        if rest[:1] == ["run"]:
            return "vitest", rest[1:]
        if rest[:1] and rest[0] in _VITEST_OTHER_SUBCOMMANDS or "--run" not in rest:
            return None
        return "vitest", rest
    if exe in ("bun", "go") and rest[:1] == ["test"]:
        return f"{exe} test", rest[1:]
    if exe in _SMALL_RUN_LAUNCHERS:
        value_flags = _SMALL_RUN_LAUNCHERS[exe]
        i = _skip_options(rest, value_flags)
        if rest[i:i + 1] != ["run"]:
            return None
        inner = rest[i + 1:]
        j = _skip_options(inner, value_flags)
        if any(tok.split("=", 1)[0] in _LAUNCHER_CWD_FLAGS[exe] for tok in rest[:i] + inner[:j]):
            return None
        inner = inner[j:]
        return _test_runner_operands(inner, depth + 1) if inner else None
    module = _python_module_argv(argv)
    return _test_runner_operands(module, depth + 1) if module else None


def _file_operands(runner: str, rest: list[str]) -> Optional[list[str]]:
    """The operands of a runner invocation, or None when it carries an option outside the
    runner's `_RUNNER_OPTIONS` allowlist. `--` ends the options."""
    allowed = _RUNNER_OPTIONS[runner]
    out: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        i += 1
        if tok == "--":
            return out + rest[i:]
        if not tok.startswith("-") or tok == "-":
            out.append(tok)
            continue
        if runner == "go test" or tok.startswith("--"):
            name, glued = tok.split("=", 1)[0], "=" in tok
            if runner == "go test":
                name = name.lstrip("-")
            if name in allowed.flags:
                continue
            if name not in allowed.values:
                return None
            i += 0 if glued else 1
            continue
        for j, ch in enumerate(tok[1:], start=2):  # a short-option cluster: `-xvs`, `-kslow`, `-rA`
            if f"-{ch}" in allowed.flags:
                continue
            if f"-{ch}" not in allowed.values:
                return None
            i += 0 if tok[j:] else 1
            break
    return out


def _is_directory_operand(operand: str, cwd: Optional[str]) -> bool:
    """A test operand names a DIRECTORY (a whole tree) when it ends in `/`, exists as a directory
    under `cwd`, or — for a path that does not exist there, or an unknown `cwd` — has no
    extension in its last component. `tests/x.py::TestFoo::test_bar` is a file: the node-id
    part is ignored."""
    path = operand.split("::", 1)[0]
    if not path or path.endswith("/"):
        return True
    if cwd is not None:
        try:
            full = os.path.join(cwd, os.path.expanduser(path))
            if os.path.isdir(full):
                return True
            if os.path.isfile(full):
                return False
        except (OSError, ValueError):
            pass
    return "." not in _basename(path)


def _addopts_open(value: list[str]) -> bool:
    """Whether an `addopts` value continues on the next line: a TOML multiline string or array
    not yet closed, or a trailing backslash (round-5 review finding)."""
    joined, head = "\n".join(value), value[0].lstrip()
    if head.startswith(('"""', "'''")):
        return joined.count(head[:3]) < 2
    if head.startswith("["):
        return "]" not in joined
    return joined.rstrip().endswith("\\")


def _addopts_values(text: str) -> list[str]:
    """Every word of every `addopts = …` setting in a pytest config file's `text`: the rest of the
    line plus an INI continuation (indented lines), a TOML array or multiline string spanning
    lines, or a backslash continuation."""
    lines = text.splitlines()
    words: list[str] = []
    for n, line in enumerate(lines):
        match = _ADDOPTS_LINE.match(line)
        if not match:
            continue
        value = [match.group(1)]
        for following in lines[n + 1:]:
            if not (_addopts_open(value) or (following[:1].isspace() and following.strip())):
                break
            value.append(following)
        words += re.sub(r"[\[\],\"'\\]", " ", " ".join(value)).split()
    return words


def _pytest_may_add_operands(files: list[str], cwd: Optional[str]) -> bool:
    """True when pytest may collect more than `files` or fan out: `_PYTEST_ENV` set in this
    environment, an unknown directory, or a config file at or above an operand whose `addopts`
    holds an operand (`tests/`) or an option outside the pytest allowlist (`-n auto`, `-p x`).
    An unreadable config file counts as one that adds."""
    if any(os.environ.get(name, "").strip() for name in _PYTEST_ENV):
        return True
    if cwd is None:
        return True
    seen: set[str] = set()
    for operand in files:
        directory = os.path.dirname(os.path.normpath(os.path.join(cwd, operand.split("::", 1)[0])))
        while directory not in seen:
            seen.add(directory)
            for name in _PYTEST_CONFIG_FILES:
                path = os.path.join(directory, name)
                if not os.path.isfile(path):
                    continue
                try:
                    text = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return True
                # Held to the command line's allowlist: `addopts = tests/` adds a tree and
                # `addopts = -n auto` an xdist worker per CPU (round-4/7 review findings).
                if _file_operands("pytest", _addopts_values(text)) != []:
                    return True
            directory = os.path.dirname(directory)
    return False


def _is_pattern_operand_small(operand: str, cwd: Optional[str]) -> bool:
    """A jest/vitest/`bun test` filter that names a literal test file (see `_PATTERN_RUNNERS`)."""
    if not _LITERAL_PATH.match(operand) or not _JS_TEST_FILE.match(_basename(operand)):
        return False
    return cwd is None or os.path.isfile(os.path.join(cwd, operand))


def _is_small_test_invocation(words: list[str], cwd: Optional[str]) -> bool:
    """`cwd`: where the operands resolve, or None when unknown or not a directory here."""
    found = _test_runner_operands(words)
    if found is None:
        return False
    runner, rest = found
    files = _file_operands(runner, rest)
    if files is None or not 1 <= len(files) <= _SMALL_RUN_MAX_FILES:
        return False
    if any(_SHELL_EXPANSION_CHARS.intersection(f.split("::", 1)[0]) for f in files):
        return False
    if runner == "go test" and not all(f.endswith(".go") for f in files):
        return False  # `./pkg`, `./...`, `example.com/mod/pkg`: packages, not files
    if runner == "pytest" and _pytest_may_add_operands(files, cwd):
        return False
    for f in files:
        is_path = runner == "bun test" and f.startswith(("./", "../", "/"))
        if runner in _PATTERN_RUNNERS and not is_path and not _is_pattern_operand_small(f, cwd):
            return False
    return not any(_is_directory_operand(f, cwd) for f in files)


def _directory_after(words: list[str], cwd: Optional[str]) -> Optional[str]:
    """Where a `cd`/`pushd` simple command leaves the shell: a single literal target resolved
    against `cwd`; None (unknown) for `popd`, `cd -`, a bare `cd`, options or an expansion."""
    i = _program_index(words)
    args = words[i + 1:]
    if (
        cwd is None or _basename(words[i]) == "popd" or len(args) != 1
        or args[0].startswith("-") or _SHELL_EXPANSION_CHARS.intersection(args[0])
    ):
        return None
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(args[0])))


def small_test_run(command: str, cwd: str) -> bool:
    """True when the ONLY heavy simple command in `command` runs one or two test FILES (see the
    block comment above): every other simple command and command substitution classifies as
    not heavy. Operand paths are looked up in `cwd`, as moved by any `cd`/`pushd` before the
    runner (`_directory_after`)."""
    if _PYTEST_ENV_PREFIX in command:
        return False
    command = _strip_comments(_strip_heredocs(_drop_offloaded_commands(command)))
    if any(classify_heavy_operation(body) is not None for body in _command_subs(command)):
        return False
    unsequenced = bool(_UNSEQUENCED.intersection(_shell_tokens(command)))
    run_cwd: Optional[str] = cwd
    leading = True  # only directory changes so far
    heavy = []
    for words in _simple_commands(command):
        label = classify_heavy_operation(shlex.join(words))
        changes_directory = (
            label is None and _program_index(words) < len(words)
            and _basename(_program_word(words)) in _CHANGES_DIRECTORY
        )
        if changes_directory:
            if unsequenced or not leading:
                return False
            run_cwd = _directory_after(words, run_cwd)
            continue
        leading = False
        if label is not None:
            heavy.append((words, label, run_cwd))
    if len(heavy) != 1:
        return False
    words, label, run_cwd = heavy[0]
    if run_cwd is not None and not os.path.isdir(run_cwd):
        run_cwd = None  # another checkout's path: nothing to look up here
    return label in _SMALL_RUN_LABELS and _is_small_test_invocation(words, run_cwd)


# ── memory-pressure reading ───────────────────────────────────────────────────
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


def _read_linux_psi_some10(read_text: Callable[[str], str] = _default_read_text) -> Optional[float]:
    """PSI `/proc/pressure/memory` `some avg10`: the percentage of the last 10 s in which
    at least one task stalled waiting for memory. Judged directly against
    `RIG_HEAVY_OP_MAX_PSI_SOME10` (not mapped onto macOS's jetsam scale — the 2026-10-11
    thrash needed its own calibration). None (fail-open) on any read/parse failure,
    including "file doesn't exist" (non-Linux, or PSI disabled in the kernel)."""
    try:
        text = read_text("/proc/pressure/memory")
    except OSError:
        return None
    match = re.search(r"^some\s+.*\bavg10=([\d.]+)", text, re.MULTILINE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_pressure_level(
    macos_reader: Optional[Callable[[], Optional[int]]] = None,
    platform_name: Optional[str] = None,
) -> Optional[int]:
    """The jetsam level on macOS; None everywhere else (Linux has no jetsam — its
    pressure signal is `read_linux_psi_some10`). None means "no reliable signal":
    callers MUST treat that as fail-open, never as "normal".

    The default reader is resolved INSIDE the body (not bound as a parameter default)
    so that monkeypatching the module-level `_read_macos_pressure_level` name (as
    `main()`'s tests do, exercising the real end-to-end call path) takes effect — a
    parameter default would bind the original function object at import time and
    silently ignore a patch.
    """
    macos_reader = macos_reader or _read_macos_pressure_level
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name == "Darwin":
        return macos_reader()
    return None


def read_linux_psi_some10(platform_name: Optional[str] = None) -> Optional[float]:
    """Platform-dispatching PSI read (Linux only), resolving its default in the body for
    the same monkeypatching reason as `read_pressure_level`."""
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name != "Linux":
        return None
    return _read_linux_psi_some10()


# ── memory snapshot: wired share + free/reclaimable (agent-tools#817) ─────────────
# NamedTuple, not @dataclass: a loader that execs this file without registering it in
# sys.modules (this repo's test suite does) breaks dataclass annotation resolution.
class MemorySnapshot(NamedTuple):
    total_bytes: int
    wired_bytes: int  # neither pageable nor compressible: GPU model buffers, kernel
    available_bytes: int  # free + reclaimable on demand
    swap_total_bytes: int = 0  # Linux only; 0 = no swap / unknown (the swap check skips)
    swap_free_bytes: int = 0

    @property
    def wired_pct(self) -> float:
        return 100.0 * self.wired_bytes / self.total_bytes


_VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_COUNT = re.compile(r'^"?([^":\n]+?)"?:\s+(\d+)\.?\s*$', re.MULTILINE)
# Reclaimable without paging anything out: the ticket's (and the panic report's) formula.
_VM_STAT_AVAILABLE = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
# Linux has no single "wired" counter: unevictable (mlocked included) + unreclaimable
# kernel slab + kernel stacks + page tables is what the kernel can neither swap nor drop.
_MEMINFO_WIRED = ("Unevictable", "SUnreclaim", "KernelStack", "PageTables")


def _physical_memory() -> Optional[int]:
    """Installed RAM in bytes (hw.memsize on macOS) without spawning a process."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return None


def _read_macos_memory_snapshot(
    run: _RunFn = subprocess.run,
    physical_memory: Callable[[], Optional[int]] = _physical_memory,
) -> Optional[MemorySnapshot]:
    """`vm_stat` page counts against installed RAM, or None when either is unreadable.

    One short-timeout subprocess: the pressure-level `sysctl` (3 s) plus this must stay
    inside the descriptor's 5 s timeout, because a hook killed at its timeout fails
    OPEN — exactly on the slow, memory-starved machine this gate exists for."""
    try:
        vm_stat = run(["vm_stat"], capture_output=True, text=True, timeout=1)
    except Exception:
        return None
    total = physical_memory()
    if vm_stat.returncode != 0 or not total or total <= 0:
        return None
    page_size = _VM_STAT_PAGE_SIZE.search(vm_stat.stdout)
    counts = {key.strip(): int(value) for key, value in _VM_STAT_COUNT.findall(vm_stat.stdout)}
    if not page_size or "Pages wired down" not in counts:
        return None
    if any(key not in counts for key in _VM_STAT_AVAILABLE):
        return None
    page = int(page_size.group(1))
    return MemorySnapshot(
        total_bytes=total,
        wired_bytes=counts["Pages wired down"] * page,
        available_bytes=sum(counts[key] for key in _VM_STAT_AVAILABLE) * page,
    )


def _read_linux_memory_snapshot(read_text: Callable[[str], str] = _default_read_text) -> Optional[MemorySnapshot]:
    """`/proc/meminfo`, using the kernel's own `MemAvailable` estimate for free + reclaimable."""
    try:
        text = read_text("/proc/meminfo")
    except OSError:
        return None
    kib = {key: int(value) for key, value in re.findall(r"^([\w()]+):\s+(\d+)", text, re.MULTILINE)}
    if kib.get("MemTotal", 0) <= 0 or "MemAvailable" not in kib:
        return None
    return MemorySnapshot(
        total_bytes=kib["MemTotal"] * 1024,
        wired_bytes=sum(kib.get(key, 0) for key in _MEMINFO_WIRED) * 1024,
        available_bytes=kib["MemAvailable"] * 1024,
        swap_total_bytes=kib.get("SwapTotal", 0) * 1024,
        swap_free_bytes=kib.get("SwapFree", 0) * 1024,
    )


def read_memory_snapshot(
    macos_reader: Optional[Callable[[], Optional[MemorySnapshot]]] = None,
    linux_reader: Optional[Callable[[], Optional[MemorySnapshot]]] = None,
    platform_name: Optional[str] = None,
) -> Optional[MemorySnapshot]:
    """Platform-dispatching, exactly like `read_pressure_level` (and resolving its
    defaults in the body for the same monkeypatching reason). None = no signal."""
    macos_reader = macos_reader or _read_macos_memory_snapshot
    linux_reader = linux_reader or _read_linux_memory_snapshot
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name == "Darwin":
        return macos_reader()
    return linux_reader()


# ── machine-wide GPU slot pool (lib/agenttools_gpu_slots, agent-tools#817) ───────
_GPU_SLOTS_PKG = Path(__file__).resolve().parents[2] / "lib" / "agenttools_gpu_slots"


class GpuSlots(NamedTuple):
    free: int
    total: int
    holders: tuple[str, ...]


def _load_gpu_slots():
    """The shared slot-pool lib, loaded by path from the same agent-tools checkout or
    release this hook lives in — the ONE definition of the slot dir, count and lock
    protocol. None when it is missing or broken."""
    init = _GPU_SLOTS_PKG / "__init__.py"
    if not init.is_file():
        return None
    name = "_heavy_op_memory_gate_gpu_slots"
    spec = importlib.util.spec_from_file_location(name, init)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        return None
    return module


def _probe_gpu_slots() -> Optional[GpuSlots]:
    """How many slots are free right now. Never waits and never keeps a lock: each
    probe lock is dropped the moment it is taken. None (skip the slot check; memory
    checks still apply) when the lib is missing or the probe fails."""
    slots = _load_gpu_slots()
    if slots is None:
        warn(f"GPU slot lib not found at {_GPU_SLOTS_PKG} — skipping the slot check")
        return None
    try:
        total = slots.slot_count()
        states = slots.slot_states(slots.slot_dir(), total)
    except Exception as exc:
        warn(f"could not probe the GPU slots ({exc}) — skipping the slot check")
        return None
    return GpuSlots(
        free=sum(1 for state in states if not state.held),
        total=total,
        holders=tuple(state.holder or state.path.name for state in states if state.held),
    )

# ── live heavy runners: a stateless read of the Linux process table ──────────────
# Both 2026-10 incidents were the third/fourth concurrent suite on the box. Counting what is
# ALREADY running (not what this hook has let through — that would be the leaking counter the
# module docstring rejects) tells the N+1th apart from the first without any shared state.
class LiveRunner(NamedTuple):
    pid: int
    pss_mb: int  # the whole runner tree's PSS (launcher, runner, workers; a review's seats)
    label: str  # `pytest`, `bun test`, `review (2 seats: claude, codex)`, ...
    session: str  # `omp 53502 (~/xp/foo) "title"`, or `omp 53502`, or `` (no omp ancestor)
    omp_pid: Optional[int] = None  # the root omp session's pid; `session` is derived from it


# A process whose argv names one of these IN AN EXECUTABLE POSITION is a suite/type-check
# runner: `pytest tests/`, `python -m pytest`, `.venv/bin/mypy src`, `node …/vitest`,
# `uv run --with pytest pytest`. Executable position = argv[0], or any later word reached only
# through interpreters/launchers/wrappers, their options and operands (`_LIVE_PASSTHROUGH`) —
# so `grep pytest README.md` and `echo mypy` are not runners (review finding).
_LIVE_DIRECT_RUNNERS = frozenset({"pytest", "py.test", "mypy", "vitest", "jest"})
_LIVE_PASSTHROUGH_EXES = re.compile(
    r"^(?:python|pypy)(?:\d+(?:\.\d+)?)?$|^(?:node|bun|deno|uv|uvx|npx|bunx|npm|pnpm|yarn|poetry|"
    r"pipenv|pdm|coverage|timeout|env|nice|time|stdbuf|nohup|setsid|unbuffer|sh|bash|zsh)$"
)
_LIVE_PASSTHROUGH_WORDS = frozenset({"run", "exec", "x", "dlx", "--"})
_LIVE_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
# `<tool> <subcommand>`: the subcommand must sit in the first few argv words.
_LIVE_TOOL_SUBCOMMANDS = {
    "bun": frozenset({"test"}),
    "cargo": frozenset({"test"}),
    "go": frozenset({"test"}),
    "make": frozenset({"test", "check"}),
    "docker": frozenset({"build"}),  # `docker buildx build` has `build` within reach too
}
_LIVE_TSC_BUILD_FLAGS = frozenset({"-b", "--build"})
# The fat agent CLIs `review` fans out per seat (review-cli, REVIEW_MAX_CONCURRENCY=4): each is
# a multi-GB node/bun process. Counted only under a `review` ancestor — an interactive
# `claude` in the operator's terminal is not a heavy runner.
_REVIEW_SEAT_CLIS = frozenset({"claude", "codex", "opencode"})
_LIVE_ARGV_WINDOW = 4  # how many leading argv words a tool/subcommand or seat CLI may sit in
_PROC = Path("/proc")
_OMP_LIVE_STATUS_TIMEOUT_S = 2.0


class _ProcInfo(NamedTuple):
    ppid: int
    comm: str
    argv: list[str]


def _read_proc_table() -> dict[int, _ProcInfo]:
    """Every live process's parent, comm and argv from `/proc`. One pass; a process that
    vanishes mid-read is simply skipped. Zombies have an empty cmdline and are skipped too."""
    table: dict[int, _ProcInfo] = {}
    try:
        entries = list(_PROC.iterdir())
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw:
            continue
        # `pid (comm) state ppid …` — comm may contain spaces/parens, so split at the LAST `)`.
        close = stat.rfind(")")
        open_ = stat.find("(")
        if close < 0 or open_ < 0:
            continue
        fields = stat[close + 2:].split()
        if len(fields) < 2 or not fields[1].lstrip("-").isdigit():
            continue
        argv = [word.decode("utf-8", "replace") for word in raw.split(b"\0") if word]
        table[int(entry.name)] = _ProcInfo(ppid=int(fields[1]), comm=stat[open_ + 1:close], argv=argv)
    return table


def _pss_mb(pid: int) -> Optional[int]:
    """Proportional set size: shared pages split between their users, so summing a tree
    (forked workers, shared libraries) never double-counts (review finding: summed RSS did,
    which shrank a runner's reservation below what it can still grow by). None when
    `smaps_rollup` is unreadable — readable only for this user's processes."""
    try:
        for line in (_PROC / str(pid) / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _rss_mb(pid: int) -> int:
    """Resident set size from the world-readable `statm`; 0 for a process that is gone."""
    try:
        resident_pages = int((_PROC / str(pid) / "statm").read_text().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") // _MIB
    except (OSError, ValueError, IndexError):
        return 0


def _direct_runner_in(argv: list[str]) -> Optional[str]:
    """The first direct runner reached through executable positions only (see
    `_LIVE_DIRECT_RUNNERS`), else None."""
    for word in argv:
        name = _basename(word)
        if name in _LIVE_DIRECT_RUNNERS:
            return name
        if (
            _LIVE_PASSTHROUGH_EXES.match(name)
            or word in _LIVE_PASSTHROUGH_WORDS
            or word.startswith("-")
            or _LIVE_DURATION.match(word)
            or _ASSIGNMENT.match(word)
        ):
            continue
        return None
    return None


def _runner_label(argv: list[str]) -> Optional[str]:
    """What heavy runner this argv is, or None. Launcher processes (`uv run pytest`, `npm
    test`) match too through the runner name in their argv — `scan_live_runners` collapses
    a matched child under a matched parent so a launcher + its runner count once."""
    direct = _direct_runner_in(argv)
    if direct:
        return direct
    head = [_basename(word) for word in argv[:_LIVE_ARGV_WINDOW]]
    for i, name in enumerate(head):
        subcommands = _LIVE_TOOL_SUBCOMMANDS.get(name)
        if subcommands:
            for word in head[i + 1:]:
                if word in subcommands:
                    return f"{name} {word}"
        if name == "tsc" and any(word in _LIVE_TSC_BUILD_FLAGS for word in argv[i + 1:]):
            return "tsc --build"
    return None


def _is_offload_process(info: _ProcInfo) -> bool:
    """A real `runpod-run` process (`bash /…/runpod-run -- bun test`): the suite in its argv,
    and the ssh/rsync children carrying it, run on a Runpod pod, not here. The script word
    must be ABSOLUTE (how the kernel passes a shebang script reached via PATH or a full path)
    and pass `_is_trusted_offload_program`; a bare or relative word depends on a cwd/PATH
    this scan cannot pin, the same rule the command side applies to relative paths. A
    process merely NAMED runpod-run is counted like any other."""
    for word in info.argv:
        if _basename(word) in _OFFLOAD_COMMANDS:
            return word.startswith("/") and _is_trusted_offload_program(word)
        if not (
            _LIVE_PASSTHROUGH_EXES.match(_basename(word))
            or word.startswith("-")
            or _LIVE_DURATION.match(word)
            or _ASSIGNMENT.match(word)
        ):
            return False
    return False


def _is_review_process(info: _ProcInfo) -> bool:
    return info.comm == "review" or any(_basename(word) == "review" for word in info.argv[:_LIVE_ARGV_WINDOW])


class ReviewSeat(NamedTuple):
    review_pid: int  # the nearest `review` ancestor
    cli: str  # `claude`, `codex`, `opencode`


def _review_seat(pid: int, table: dict[int, _ProcInfo]) -> Optional[ReviewSeat]:
    """The seat when `pid` is a seat CLI a `review` process fanned out, else None."""
    info = table[pid]
    cli = next(
        (name for name in (_basename(word) for word in info.argv[:_LIVE_ARGV_WINDOW])
         if name.removesuffix(".js").removesuffix(".mjs") in _REVIEW_SEAT_CLIS),
        None,
    )
    if cli is None:
        return None
    for ancestor in _ancestors(pid, table):
        if _is_review_process(table[ancestor]):
            return ReviewSeat(ancestor, cli.removesuffix(".js").removesuffix(".mjs"))
    return None


def _ancestors(pid: int, table: dict[int, _ProcInfo]) -> list[int]:
    """Parent, grandparent, … up to init (bounded; a `/proc` snapshot can contain a ppid cycle
    only through reuse, but a bound costs nothing)."""
    out: list[int] = []
    seen = {pid}
    while pid in table and len(out) < 64:
        pid = table[pid].ppid
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        out.append(pid)
    return out


def _is_omp_session_process(info: _ProcInfo) -> bool:
    """An omp TUI session process: comm `omp`, not one of omp's `__omp_worker_*` daemons."""
    return info.comm == "omp" and not any(word.startswith("__omp_worker") for word in info.argv[1:])


def _omp_session_pid(pid: int, table: dict[int, _ProcInfo]) -> Optional[int]:
    """The ROOT omp session a process belongs to: the topmost omp ancestor (a subagent's omp
    is a child of its session's omp), or None when there is no omp in the ancestry."""
    omp_pids = [ancestor for ancestor in _ancestors(pid, table) if _is_omp_session_process(table[ancestor])]
    return omp_pids[-1] if omp_pids else None


def _omp_live_sessions(run: _RunFn = subprocess.run) -> dict[int, dict]:
    """`omp-steer list`'s source: `tg-ctl omp-live status --json` → {pid: {cwd, title, …}}.
    Empty on any failure (no tg-ctl, timeout, bad JSON) — the caller falls back to `/proc`."""
    try:
        proc = run(
            ["tg-ctl", "omp-live", "status", "--json"],
            capture_output=True, text=True, timeout=_OMP_LIVE_STATUS_TIMEOUT_S,
        )
        hosts = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
    except Exception:
        return {}
    if not isinstance(hosts, list):
        return {}
    return {int(h["pid"]): h for h in hosts if isinstance(h, dict) and str(h.get("pid", "")).isdigit()}


def _describe_session(omp_pid: Optional[int], live: dict[int, dict]) -> str:
    if omp_pid is None:
        return ""
    host = live.get(omp_pid)
    cwd = str(host.get("cwd") or "") if host else ""
    if not cwd:
        try:
            cwd = os.readlink(_PROC / str(omp_pid) / "cwd")
        except OSError:
            cwd = ""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    out = f"omp {omp_pid}"
    if cwd:
        out += f" ({cwd})"
    title = str(host.get("title") or "") if host else ""
    if title:
        out += f' "{title}"'
    return out


def scan_live_runners(
    table: Optional[dict[int, _ProcInfo]] = None,
    pss: Callable[[int], Optional[int]] = _pss_mb,
    rss: Callable[[int], int] = _rss_mb,
) -> list[LiveRunner]:
    """Every heavy runner live on the machine right now, one entry per runner TREE (a matched
    process under another matched process — `uv run pytest` → `python -m pytest`, pytest-xdist
    workers — collapses into its topmost match; a review's seat CLIs collapse into the review
    process, since the budget's cost is per review fan-out). Each entry's footprint is its
    whole tree's (`tree_footprint`). This hook's own ancestry is excluded: an agent running the hook from inside a suite
    would otherwise count itself. So is every real `runpod-run` tree: its suite runs remotely.
    Sessions are identified by pid only; `describe_live_runners` adds the titles (a `tg-ctl`
    subprocess) and is called only on the block path."""
    if table is None:
        table = _read_proc_table()
        if not table:
            # This very process is in a readable /proc, so an empty table means /proc could not
            # be read: a failed scan, not "nothing running" (review finding — the swap check's
            # no-scan fallback must apply).
            raise OSError("/proc could not be read")
    own = set(_ancestors(os.getpid(), table)) | {os.getpid()}
    offloads = {pid for pid, info in table.items() if _is_offload_process(info)}
    matched: dict[int, str] = {}
    seats: dict[int, list[str]] = {}
    for pid, info in table.items():
        if pid in own or (offloads and (pid in offloads or any(a in offloads for a in _ancestors(pid, table)))):
            continue
        label = _runner_label(info.argv)
        if label:
            matched[pid] = label
            continue
        seat = _review_seat(pid, table)
        if seat and seat.review_pid not in own:
            seats.setdefault(seat.review_pid, []).append(seat.cli)
    for review_pid, clis in seats.items():
        matched.setdefault(review_pid, f"review ({len(clis)} seat(s): {', '.join(sorted(clis))})")
    roots = [pid for pid in matched if not any(a in matched for a in _ancestors(pid, table))]
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        children.setdefault(info.ppid, []).append(pid)

    def tree_footprint(root: int) -> int:
        """Σ PSS over the tree. Where PSS is unreadable (another user's or root's processes),
        RSS stands in, but summed RSS double-counts shared pages and would shrink the
        reservation (review finding); one process's RSS is roughly a floor of what the tree
        holds (bar libraries it shares outside the tree), so the footprint is max(Σ readable
        PSS, max unreadable RSS) — an underestimate, so the reservation errs high."""
        readable, fallback, stack, seen = 0, 0, [root], {root}
        while stack:
            pid = stack.pop()
            mb = pss(pid)
            if mb is None:
                fallback = max(fallback, rss(pid))
            else:
                readable += mb
            for child in children.get(pid, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return max(readable, fallback)

    return sorted(
        (LiveRunner(pid, tree_footprint(pid), matched[pid], "", _omp_session_pid(pid, table)) for pid in roots),
        key=lambda r: r.pid,
    )


def describe_live_runners(
    runners: list[LiveRunner], sessions: Optional[Callable[[], dict[int, dict]]] = None
) -> list[LiveRunner]:
    """The same runners with their `session` text filled in — `omp-steer list`'s titles
    (one `tg-ctl` call, skipped when no runner has an omp ancestor) and `/proc` cwd fallback."""
    if not any(r.omp_pid for r in runners):
        return runners
    live = (sessions or _omp_live_sessions)()
    return [r._replace(session=_describe_session(r.omp_pid, live)) for r in runners]


def _live_runners_guarded(platform_name: Optional[str] = None) -> Optional[list[LiveRunner]]:
    """The live-runner scan as `main()` uses it: Linux only (`/proc` layout), and any
    failure inside the scan is a missing signal (None), never a crash — fail-open."""
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name != "Linux":
        return None
    try:
        return scan_live_runners()
    except Exception as exc:
        warn(f"could not scan live heavy runners ({exc}) — skipping the live-runner check")
        return None


def emit(decision: str, message: Optional[str] = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"heavy-op-memory-gate: {msg}\n")


def _block_at_level() -> int:
    raw = os.environ.get("RIG_HEAVY_OP_BLOCK_AT_LEVEL")
    if not raw:
        return DEFAULT_BLOCK_AT_LEVEL
    try:
        value = int(raw)
    except ValueError:
        warn(f"RIG_HEAVY_OP_BLOCK_AT_LEVEL={raw!r} is not an int — using default {DEFAULT_BLOCK_AT_LEVEL}")
        return DEFAULT_BLOCK_AT_LEVEL
    if value not in _VALID_PRESSURE_LEVELS:
        # A value outside {1,2,4} (e.g. 5 or 999) would make `level < block_at`
        # true for every real jetsam reading — a silent, total bypass of the
        # "no bypass" contract documented in `decide`'s block message. Reject it
        # rather than honor a value that can never actually block.
        warn(
            f"RIG_HEAVY_OP_BLOCK_AT_LEVEL={value} is not one of the real jetsam "
            f"levels {sorted(_VALID_PRESSURE_LEVELS)} — using default {DEFAULT_BLOCK_AT_LEVEL} "
            "(an out-of-range value would silently bypass every block)"
        )
        return DEFAULT_BLOCK_AT_LEVEL
    return value


def _positive_int_env(name: str, default: int, upper: Optional[int] = None) -> int:
    """An env override that must stay a real, blocking threshold: a value that would
    disable the check (0, negative, >= 100 for a percentage) falls back to the
    default, the same no-silent-bypass posture as `_block_at_level`."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        warn(f"{name}={raw!r} is not an int — using default {default}")
        return default
    if value < 1 or (upper is not None and value > upper):
        allowed = f"1..{upper}" if upper is not None else ">= 1"
        warn(f"{name}={value} is outside {allowed} and would disable this check — using default {default}")
        return default
    return value


def _max_wired_pct() -> int:
    return _positive_int_env("RIG_HEAVY_OP_MAX_WIRED_PCT", DEFAULT_MAX_WIRED_PCT, upper=99)


def _min_available_mb(memory: Optional[MemorySnapshot] = None, platform_name: Optional[str] = None) -> int:
    """Linux: max(8 GiB, 20% of MemTotal) — see the Linux section of the module docstring;
    macOS keeps the jetsam-era 512 MB. The env override wins on both."""
    platform_name = platform_name if platform_name is not None else platform.system()
    default = DEFAULT_MIN_AVAILABLE_MB
    if platform_name == "Linux":
        total_mb = memory.total_bytes // _MIB if memory is not None else 0
        default = max(DEFAULT_LINUX_MIN_AVAILABLE_MB, total_mb * DEFAULT_LINUX_MIN_AVAILABLE_PCT // 100)
    return _positive_int_env("RIG_HEAVY_OP_MIN_AVAILABLE_MB", default)


def _max_psi_some10() -> float:
    raw = os.environ.get("RIG_HEAVY_OP_MAX_PSI_SOME10")
    if not raw:
        return DEFAULT_MAX_PSI_SOME10
    try:
        value = float(raw)
    except ValueError:
        warn(f"RIG_HEAVY_OP_MAX_PSI_SOME10={raw!r} is not a number — using default {DEFAULT_MAX_PSI_SOME10}")
        return DEFAULT_MAX_PSI_SOME10
    if not 0 < value < 100:
        warn(f"RIG_HEAVY_OP_MAX_PSI_SOME10={value} is outside 0..100 and would disable this check — using default {DEFAULT_MAX_PSI_SOME10}")
        return DEFAULT_MAX_PSI_SOME10
    return value


def _min_swap_free_pct() -> int:
    return _positive_int_env("RIG_HEAVY_OP_MIN_SWAP_FREE_PCT", DEFAULT_MIN_SWAP_FREE_PCT, upper=99)


def _max_live() -> int:
    return _positive_int_env("RIG_HEAVY_OP_MAX_LIVE", DEFAULT_MAX_LIVE)


class CostTable(NamedTuple):
    """Expected peak memory (MiB) per heavy-operation class — see the module docstring."""
    review_mb: int = DEFAULT_COST_REVIEW_MB
    docker_mb: int = DEFAULT_COST_DOCKER_MB
    suite_mb: int = DEFAULT_COST_SUITE_MB


def _cost_table() -> CostTable:
    return CostTable(
        _positive_int_env("RIG_HEAVY_OP_COST_REVIEW_MB", DEFAULT_COST_REVIEW_MB),
        _positive_int_env("RIG_HEAVY_OP_COST_DOCKER_MB", DEFAULT_COST_DOCKER_MB),
        _positive_int_env("RIG_HEAVY_OP_COST_SUITE_MB", DEFAULT_COST_SUITE_MB),
    )


def _headroom_mb() -> int:
    return _positive_int_env("RIG_HEAVY_OP_HEADROOM_MB", DEFAULT_HEADROOM_MB)


def _expected_cost_mb(label: str, costs: CostTable) -> int:
    """A classify label (`multi-model review-cli pass`, …) or a live-runner label
    (`review (3 seat(s): …)`, `docker build`, `pytest`, …) → its class's expected cost."""
    if label.startswith("review") or label == REVIEW_LABEL:
        return costs.review_mb
    if label.startswith("docker"):
        return costs.docker_mb
    return costs.suite_mb


def _reserved_mb(runner: LiveRunner, costs: CostTable) -> int:
    """What a live runner may still grow into: its expected cost minus what it already holds
    (already counted out of MemAvailable)."""
    return max(0, _expected_cost_mb(runner.label, costs) - runner.pss_mb)


class MemoryBudget(NamedTuple):
    available_mb: int
    reserved_mb: int
    runners: int
    cost_mb: int
    headroom_mb: int

    @property
    def left_mb(self) -> int:
        return self.available_mb - self.reserved_mb - self.cost_mb

    def describe(self, label: str) -> str:
        return (
            f"{self.available_mb} MB available − {self.reserved_mb} MB reserved for "
            f"{self.runners} live runner(s) − {self.cost_mb} MB for this {label} = "
            f"{self.left_mb} MB left (headroom {self.headroom_mb} MB)"
        )


def memory_budget(
    label: str,
    memory: Optional[MemorySnapshot],
    live_runners: Optional[list[LiveRunner]],
    costs: CostTable,
    headroom_mb: int,
) -> Optional[MemoryBudget]:
    """The budget arithmetic, or None without a memory snapshot. Unknown live runners (no
    scan: macOS, or the scan failed) reserve nothing."""
    if memory is None:
        return None
    runners = live_runners or []
    return MemoryBudget(
        available_mb=memory.available_bytes // _MIB,
        reserved_mb=sum(_reserved_mb(r, costs) for r in runners),
        runners=len(runners),
        cost_mb=_expected_cost_mb(label, costs),
        headroom_mb=headroom_mb,
    )


def _gib(n_bytes: int) -> str:
    return f"{n_bytes / 1024**3:.2f} GiB"


def _format_live_runners(runners: list[LiveRunner], costs: CostTable) -> str:
    return "\n".join(
        f"  - pid {r.pid}  {r.pss_mb} MB  {r.label}  (reserves {_reserved_mb(r, costs)} MB)"
        + (f"  ← {r.session}" if r.session else "")
        for r in runners
    )


def decide(
    label: Optional[str],
    level: Optional[int],
    block_at: int,
    memory: Optional[MemorySnapshot] = None,
    max_wired_pct: int = DEFAULT_MAX_WIRED_PCT,
    min_available_mb: int = DEFAULT_MIN_AVAILABLE_MB,
    gpu_slots: Optional[GpuSlots] = None,
    psi_some10: Optional[float] = None,
    max_psi_some10: float = DEFAULT_MAX_PSI_SOME10,
    min_swap_free_pct: int = DEFAULT_MIN_SWAP_FREE_PCT,
    live_runners: Optional[list[LiveRunner]] = None,
    max_live: int = DEFAULT_MAX_LIVE,
    costs: CostTable = CostTable(),
    headroom_mb: int = DEFAULT_HEADROOM_MB,
    emergency_only: bool = False,
) -> tuple[str, Optional[str]]:
    """Pure decision core — the seam every test in this suite exercises directly.

    Takes the already-computed `classify_heavy_operation` label (not the raw
    command) so `main()` can skip reading memory entirely for a non-heavy
    command — see `main()` for why that ordering matters. Any one reason
    blocks; a missing signal (None) contributes no reason (fail-open per signal).
    The memory budget applies only with a live-runner scan (`live_runners` not None,
    Linux): without one it would turn the new operation's cost into a far higher
    floor than macOS's own thresholds. `emergency_only` (a review handed to review-cli's
    admission controller, see `review_cli_handoff`, or a run of one or two test files, see
    `small_test_run`) drops the budget, swap and live-runner-ceiling checks and keeps the
    emergency ones (MemAvailable floor, PSI, wired share, jetsam level, GPU slots).
    """
    if label is None:
        return "allow", None
    reasons = []
    budget = memory_budget(label, memory, live_runners, costs, headroom_mb)
    if level is not None and level >= block_at:
        level_name = {1: "normal", 2: "warn", 4: "critical"}.get(level, str(level))
        reasons.append(
            f"the machine's memory pressure is currently '{level_name}' "
            f"(jetsam level {level}, block threshold {block_at})"
        )
    if memory is not None and memory.wired_pct > max_wired_pct:
        reasons.append(
            f"{memory.wired_pct:.1f}% of RAM is wired ({_gib(memory.wired_bytes)} of "
            f"{_gib(memory.total_bytes)}; threshold {max_wired_pct}%, RIG_HEAVY_OP_MAX_WIRED_PCT) — "
            "wired memory such as GPU model buffers can be neither paged out nor compressed, "
            "so the jetsam pressure level does not see it"
        )
    if memory is not None and memory.available_bytes < min_available_mb * _MIB:
        reasons.append(
            f"only {memory.available_bytes // _MIB} MB is free or reclaimable "
            f"(threshold {min_available_mb} MB, RIG_HEAVY_OP_MIN_AVAILABLE_MB)"
        )
    if psi_some10 is not None and psi_some10 > max_psi_some10:
        reasons.append(
            f"tasks stalled on memory {psi_some10:.1f}% of the last 10 s "
            f"(PSI some avg10; threshold {max_psi_some10:g}%, RIG_HEAVY_OP_MAX_PSI_SOME10) — "
            "the kernel is already reclaiming under load"
        )
    if not emergency_only and live_runners is not None and budget is not None and budget.left_mb < headroom_mb:
        reasons.append(
            f"the memory budget does not cover it: {budget.describe(label)} "
            "(RIG_HEAVY_OP_HEADROOM_MB, RIG_HEAVY_OP_COST_REVIEW_MB/_DOCKER_MB/_SUITE_MB)"
        )
    # Without a live-runner scan (it failed) the budget knows no reservations, so a
    # roomy-looking budget proves nothing: the swap check then stays unconditional, as before
    # the budget existed (review finding). macOS has no scan either, but its snapshot carries
    # no swap figures (`swap_total_bytes` is Linux-only, 0), so this never fires there.
    if not emergency_only and memory is not None and memory.swap_total_bytes > 0 and (
        live_runners is None or budget is None or budget.left_mb < 2 * headroom_mb
    ):
        swap_free_pct = 100.0 * memory.swap_free_bytes / memory.swap_total_bytes
        if swap_free_pct < min_swap_free_pct:
            context = (
                f"while the budget leaves only {budget.left_mb} MB (under 2 x the {headroom_mb} MB headroom)"
                if live_runners is not None and budget is not None
                else "and no live-runner scan is available to show the memory budget"
            )
            reasons.append(
                f"swap is {100 - swap_free_pct:.0f}% full ({_gib(memory.swap_total_bytes - memory.swap_free_bytes)} "
                f"of {_gib(memory.swap_total_bytes)}; free threshold {min_swap_free_pct}%, "
                f"RIG_HEAVY_OP_MIN_SWAP_FREE_PCT) {context} — a full swap leaves the next memory "
                "burst nowhere to go"
            )
    if not emergency_only and live_runners is not None and len(live_runners) >= max_live:
        reasons.append(
            f"{len(live_runners)} heavy runner(s) are already live on the machine "
            f"(hard ceiling {max_live}, RIG_HEAVY_OP_MAX_LIVE; listed below)"
        )
    slots_full = label == GPU_MODEL_LABEL and gpu_slots is not None and gpu_slots.free == 0
    if slots_full:
        reasons.append(
            f"all {gpu_slots.total} of {gpu_slots.total} machine-wide GPU model slots are taken "
            f"(holders: {'; '.join(gpu_slots.holders)})"
        )
    if not reasons:
        return "allow", None

    message = (
        f"Blocked: this looks like a {label}, and " + "; and ".join(reasons) + ". "
        "Starting it now risks another memory-exhaustion incident (2026-08-27/28: free "
        "memory ~65MB, VS Code windows hung; 2026-09-29: 13 concurrent whisper-cli model "
        "loads wired 95.7% of RAM and kernel-panicked the Mac; 2026-10-10 and 2026-10-11: "
        "three concurrent review fan-outs plus docker-backed suites lost 26 GB in two minutes "
        "on a full swap and thrashed the Linux workstation to a manual reset). There is no "
        "bypass for this check — wait for other concurrent work to finish or be reaped, then "
        "retry; memory, swap, pressure and the live-runner list are re-read fresh on every "
        "attempt, so a retry after they genuinely free up will pass. "
    )
    if not emergency_only:
        # Not for a review handed to review-cli or a 1-2 file run: the advice would name the very
        # command just blocked on an emergency signal (round-3 review finding).
        message += (
            "A full test suite, mypy or a docker-backed integration run belongs on a Runpod pod "
            "anyway (`runpod-run -- <command>`, skill runpod-offload); locally run one or two "
            "test files at most. "
        )
    message += (
        "If it never clears even with nothing else obviously running, that itself is worth "
        "reporting to the machine's operator directly rather than working around this gate."
    )
    if emergency_only and label == REVIEW_LABEL:
        message += (
            " review-cli's admission controller would queue this review for CPU, RAM and "
            "provider capacity, but these are emergency signals: nothing new should start now."
        )
    if slots_full:
        message += (
            " To queue for a slot instead of failing, run the model command through the "
            f"slot wrapper, which waits for a free slot: `python3 {_GPU_SLOTS_PKG} run -- "
            f"<command>` (`python3 {_GPU_SLOTS_PKG} status` shows who holds the slots)."
        )
    if not emergency_only and live_runners is not None and budget is not None:
        message += f"\nMemory budget: {budget.describe(label)}."
    if live_runners:
        # Who the agent is competing with — also when another signal blocked: the list is
        # what an agent needs to decide whether to wait, offload, or ask the operator.
        message += (
            f"\nHeavy runners live right now ({len(live_runners)}; ceiling {max_live}):\n"
            f"{_format_live_runners(live_runners, costs)}"
        )
    return "block", message


def _event_cwd(event: dict, args: dict) -> str:
    """Where the command runs: the event's `cwd`, else this hook's own working directory."""
    for cwd in (event.get("cwd"), args.get("cwd")):
        if isinstance(cwd, str) and cwd:
            return cwd
    return os.getcwd()


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

    args = event.get("args")
    if not isinstance(args, dict):
        args = {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    # Classify BEFORE touching memory pressure: the vast majority of Bash calls
    # are not heavy operations, and reading pressure means spawning `sysctl`
    # (or reading /proc/pressure/memory) on every single one. Skipping that for
    # every non-heavy command also matters most exactly when it's most
    # expensive: process spawn is slowest precisely when the machine is under
    # the memory pressure this hook is watching for.
    label = classify_heavy_operation(command)
    if label is None:
        emit("allow")
        return 0
    emergency_only = False
    if label == REVIEW_LABEL:
        # Hand the review passes to review-cli's own admission controller when it is the
        # installed one. Anything else heavy in the same command starts next to the review
        # (`review diff & bun test`), so then nothing is handed off: the command is gated as a
        # whole, budgeted at the costlier of the two (review finding).
        rest = review_cli_handoff(command)
        if rest is not None:
            rest_label = classify_heavy_operation(rest) if rest else None
            if rest_label is None:
                emergency_only = True
            else:
                costs = _cost_table()
                # On a tie the rest's label wins: a GPU load's label is what turns on the slot check.
                label = max((rest_label, label), key=lambda candidate: _expected_cost_mb(candidate, costs))
    elif small_test_run(command, _event_cwd(event, args)):
        emergency_only = True

    # A GPU load while every slot is taken is refused on the slot probe alone (a few
    # lock attempts, no subprocess), so the refusal can never wait on a memory read.
    gpu_slots = _probe_gpu_slots() if label == GPU_MODEL_LABEL else None
    if gpu_slots is not None and gpu_slots.free == 0:
        decision, message = decide(label, None, _block_at_level(), gpu_slots=gpu_slots)
    else:
        memory = read_memory_snapshot()
        live_runners = _live_runners_guarded()
        signals = dict(
            memory=memory,
            max_wired_pct=_max_wired_pct(),
            min_available_mb=_min_available_mb(memory),
            gpu_slots=gpu_slots,
            psi_some10=read_linux_psi_some10(),
            max_psi_some10=_max_psi_some10(),
            min_swap_free_pct=_min_swap_free_pct(),
            max_live=_max_live(),
            costs=_cost_table(),
            headroom_mb=_headroom_mb(),
            emergency_only=emergency_only,
        )
        level, block_at = read_pressure_level(), _block_at_level()
        decision, message = decide(label, level, block_at, live_runners=live_runners, **signals)
        if decision == "block" and live_runners:
            # The session titles cost a `tg-ctl` subprocess (up to 2 s): paid only for a block,
            # where the agent needs to know whom to wait for — never on the allow path.
            try:
                described = describe_live_runners(live_runners)
            except Exception as exc:
                warn(f"could not resolve omp sessions for the live runners ({exc})")
                described = live_runners
            decision, message = decide(label, level, block_at, live_runners=described, **signals)

    if decision == "block":
        emit("block", message)
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
