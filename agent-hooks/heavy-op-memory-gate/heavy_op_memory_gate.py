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
unsupported platform) the hook has no reliable signal and ALLOWS (fail-open). Linux
gets `/proc/pressure/memory` (PSI) for signal 1 and `/proc/meminfo` for signal 2,
neither calibrated against a real Linux incident.

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

import importlib.util
import json
import os
import platform
import re
import shlex
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


def _simple_commands(command: str) -> list[list[str]]:
    """Split comment-free `command` into its simple commands' words: at `;` `&` `&&` `|`
    `||` `|&` `(` `)` and newlines, dropping redirect targets (`> out.txt`).
    Quote-aware via shlex; unbalanced quotes degrade to one whitespace-split command."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"  # a newline separates commands, so it stays punctuation
    lexer.whitespace_split = True
    lexer.commenters = ""  # already stripped; shlex's own would swallow the newline too
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    commands: list[list[str]] = []
    current: list[str] = []
    skip_redirect_target = False
    for tok in tokens:
        if tok and all(ch in _SHELL_PUNCTUATION for ch in tok):
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


def classify_heavy_operation(command: str) -> Optional[str]:
    """Return a short human label for the heavy operation `command` runs, or None.

    Kept as a pure function (no I/O) so it's directly unit-testable without
    mocking subprocess/sysctl. Token-based: a heavy-operation word trapped
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
            return "multi-model review-cli pass"

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
    subprocess spawn attempt on every single heavy-op check on Linux, which is
    pure waste on a platform that never has `sysctl kern.memorystatus_*`.

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


# ── memory snapshot: wired share + free/reclaimable (agent-tools#817) ─────────────
# NamedTuple, not @dataclass: a loader that execs this file without registering it in
# sys.modules (this repo's test suite does) breaks dataclass annotation resolution.
class MemorySnapshot(NamedTuple):
    total_bytes: int
    wired_bytes: int  # neither pageable nor compressible: GPU model buffers, kernel
    available_bytes: int  # free + reclaimable on demand

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


def _min_available_mb() -> int:
    return _positive_int_env("RIG_HEAVY_OP_MIN_AVAILABLE_MB", DEFAULT_MIN_AVAILABLE_MB)


def _gib(n_bytes: int) -> str:
    return f"{n_bytes / 1024**3:.2f} GiB"


def decide(
    label: Optional[str],
    level: Optional[int],
    block_at: int,
    memory: Optional[MemorySnapshot] = None,
    max_wired_pct: int = DEFAULT_MAX_WIRED_PCT,
    min_available_mb: int = DEFAULT_MIN_AVAILABLE_MB,
    gpu_slots: Optional[GpuSlots] = None,
) -> tuple[str, Optional[str]]:
    """Pure decision core — the seam every test in this suite exercises directly.

    Takes the already-computed `classify_heavy_operation` label (not the raw
    command) so `main()` can skip reading memory entirely for a non-heavy
    command — see `main()` for why that ordering matters. Any one reason
    blocks; a missing signal (None) contributes no reason (fail-open per signal).
    """
    if label is None:
        return "allow", None
    reasons = []
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
        "loads wired 95.7% of RAM and kernel-panicked the Mac). There is no bypass for "
        "this check — wait for other concurrent work to finish or be reaped, then retry; "
        "memory and slots are re-read fresh on every attempt, so a retry after they "
        "genuinely free up will pass. If it never clears even with nothing else obviously "
        "running, that itself is worth reporting to the machine's operator directly "
        "rather than working around this gate."
    )
    if slots_full:
        message += (
            " To queue for a slot instead of failing, run the model command through the "
            f"slot wrapper, which waits for a free slot: `python3 {_GPU_SLOTS_PKG} run -- "
            f"<command>` (`python3 {_GPU_SLOTS_PKG} status` shows who holds the slots)."
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

    # A GPU load while every slot is taken is refused on the slot probe alone (a few
    # lock attempts, no subprocess), so the refusal can never wait on a memory read.
    gpu_slots = _probe_gpu_slots() if label == GPU_MODEL_LABEL else None
    if gpu_slots is not None and gpu_slots.free == 0:
        decision, message = decide(label, None, _block_at_level(), gpu_slots=gpu_slots)
    else:
        decision, message = decide(
            label,
            read_pressure_level(),
            _block_at_level(),
            memory=read_memory_snapshot(),
            max_wired_pct=_max_wired_pct(),
            min_available_mb=_min_available_mb(),
            gpu_slots=gpu_slots,
        )

    if decision == "block":
        emit("block", message)
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
