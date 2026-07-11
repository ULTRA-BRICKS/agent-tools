#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw `gh pr merge` that bypasses ship.

Denies a shell command that merges a PR directly with `gh pr merge` (including
`gh pr merge --admin`), because that skips the project's ship gate: `gh ship <PR>`
(the green-CI-gated merge + mandatory-screenshot command) is the only sanctioned path.
A raw merge lands code without the green-CI check and the required-screenshot check —
exactly the gates ship exists to enforce.

This is the enforcement counterpart of the doc-only "use `gh ship`, never a raw merge"
rule: advice in AGENTS.md cannot stop an autonomous (auto-mode) agent from running
`gh pr merge`, so the rule only becomes real as a mid-session guard.

Detection is ARGV-BASED, not a raw substring match.  The command source is first reduced
to executable shell text (bare newlines become separators; comments, quoted newlines, and
heredoc bodies keep their shell meaning), then parsed via shlex into shell segments
(`;` / `&&` / `||` / `|` separated).  Each segment is inspected for
`basename(argv[0])=="gh" && argv[1]=="pr" && argv[2]=="merge"` after stripping leading
inline VAR=value assignments and shell grouping tokens (`(`, `{`).  Using basename
handles path-qualified invocations (`/usr/local/bin/gh pr merge`).  This prevents a false
positive where the body of an unrelated command (e.g. `gh pr create --body "run gh pr
merge to land it"`) triggers the guard — with a raw regex that body text would match and
the command would be blocked even though no actual merge is occurring.

Threat model: this hook is a guardrail for autonomous/honest agents, not a complete
adversarial shell interpreter. It models common executable shell constructs and wrappers,
fails closed for merge-like parse ambiguity, and uses conservative best-effort handling
for static shell text producers; fully dynamic string construction remains out of scope.

Allowed (let through):
  - `gh ship <PR>` / a `gh alias` that runs ship
  - `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
  - any non-merge `gh pr` subcommand (view, list, checkout, create, comment, ...)
  - commands whose body/arguments merely contain the text "gh pr merge" as a string

External approval (replaces the OLD self-service escape hatch): there is NO
`ALLOW_RAW_PR_MERGE`(+`_REASON`) env and NO `# no-ship-guard: <reason>` inline sentinel any
more — an agent could set either on its own command, so those merely let the guarded agent
grant itself the bypass this hook exists to stop. The block is now DENY-BY-DEFAULT. Use
`gh ship <PR>` (the green-CI-gated, screenshot-checked path). For a genuine one-time exception,
ASK Alex, or request a single approval by setting
`RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="<written justification>"`, which routes one Telegram
approval request to Alex (deny-by-default; a bare `1`/`true` is rejected). Only an approved
`tg-ctl ask` (exit 0) allows the raw merge; anything else denies.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a raw merge slipping through a
broken guard is exactly the failure this hook exists to stop.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import NamedTuple

_LIB_DIR = Path(__file__).resolve().parents[2] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import agenttools_hatch_escalation as hatch_escalation  # noqa: E402

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Shell operators that separate independent command segments in a compound command line.
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_COMMAND_SEPS = frozenset({"&&", "||", ";", "&", ";;", ";&", ";;&"})
_PIPE_SEPS = frozenset({"|", "|&"})
_REDIRECT_OPS = frozenset({"<", ">", ">>", "<>", "<<", "<<<", ">|", "&>", "&>>", ">&", "<&"})

# Shell grouping/control-flow tokens that may precede the real command name in a segment.
# E.g. `( gh pr merge 5 )` or `{ gh pr merge 5; }`.
_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "then", "do", "else", "elif"})
_SOURCE_COMMAND_START_WORDS = frozenset({"then", "do", "else", "elif"})

# A VAR=value inline environment assignment that may precede the executable.
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Merge-hint pattern: used to gate fail-closed on unbalanced-quote parse errors so that
# a benign command with an unbalanced quote (e.g. `grep won't file`) is NOT blocked just
# because shlex can't parse it.  Only if the raw string plausibly contains a merge
# invocation do we treat a parse error as fail-closed.
_MAX_DETECTION_DEPTH = 40
_MAX_COMMAND_CHARS = 65536
_DETECTION_NODE_BUDGET = 4000
_DETECTION_BUDGET: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "block_raw_pr_merge_detection_budget",
    default=None,
)
_FUNCTION_DEFINITION_RE = re.compile(
    r"\s*(?:(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*\(\)\s*(?P<opener>[{(])|"
    r"function\s+(?P<function_name>[A-Za-z_][A-Za-z0-9_-]*)"
    r"\s*(?:\(\))?\s*(?P<function_opener>[{(]))"
)


class _HereDoc(NamedTuple):
    delimiter: str
    strip_tabs: bool = False
    inspect_body: bool = False


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-pr-merge: {msg}\n")


def _has_merge_hint(command: str, *, max_gap: int | None = None) -> bool:
    """Linear ordered-word check for text that plausibly spells `gh pr merge`."""
    index = 0
    previous_end = 0
    for word in re.finditer(r"[A-Za-z0-9_/-]+", command):
        token = os.path.basename(word.group(0))
        if index == 0 and token == "gh":
            index = 1
            previous_end = word.end()
        elif index == 1 and token == "pr":
            if max_gap is not None and word.start() - previous_end > max_gap:
                index = 0
                previous_end = 0
                continue
            index = 2
            previous_end = word.end()
        elif index == 2 and token == "merge":
            if max_gap is not None and word.start() - previous_end > max_gap:
                index = 0
                previous_end = 0
                continue
            return True
        elif token == "gh":
            index = 1
            previous_end = word.end()
    return False


def _is_comment_start(line: str, index: int, emitted: list[str]) -> bool:
    if line[index] != "#":
        return False
    if index > 1 and line[:index].endswith("))") and emitted and not emitted[-1].isspace():
        return False
    if not emitted:
        return True
    last = emitted[-1]
    return last.isspace() or (len(last) == 1 and last in ";|&()")


def _arithmetic_span_end(source: str, start: int) -> int:
    if source.startswith("$((", start):
        i = start + 3
    elif source.startswith("((", start):
        i = start + 2
    else:
        return start

    depth = 2
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(source)


def _backtick_span_end(source: str, start: int) -> int:
    i = start + 1
    while i < len(source):
        if source[i] == "\\" and i + 1 < len(source):
            i += 2
            continue
        if source[i] == "`":
            return i + 1
        i += 1
    return len(source)


def _normalize_dollar_quotes(source: str) -> str:
    """Let shlex see shell $'...' / $"..." words as ordinary quoted words."""
    emitted: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(source):
        ch = source[i]
        if quote == "single":
            emitted.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            emitted.append(ch)
            if ch == "\\" and i + 1 < len(source):
                i += 1
                emitted.append(source[i])
            elif ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(source):
            emitted.extend((ch, source[i + 1]))
            i += 2
            continue
        if ch == "$" and i + 1 < len(source) and source[i + 1] == "'":
            value, end = _read_ansi_c_quoted_value(source, i + 1)
            emitted.append(shlex.quote(value))
            i = end
            continue
        if ch == "$" and i + 1 < len(source) and source[i + 1] == '"':
            i += 1
            ch = source[i]
        if ch == "'":
            quote = "single"
        elif ch == '"':
            quote = "double"
        emitted.append(ch)
        i += 1
    return "".join(emitted)


def _read_ansi_c_quoted_value(source: str, quote_index: int) -> tuple[str, int]:
    raw: list[str] = []
    i = quote_index + 1
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            raw.append(ch)
            raw.append(source[i + 1])
            i += 2
            continue
        if ch == "'":
            return _decode_shell_escapes("".join(raw)), i + 1
        raw.append(ch)
        i += 1
    return _decode_shell_escapes("".join(raw)), len(source)


def _decode_shell_escapes(value: str) -> str:
    decoded: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\" or i + 1 >= len(value):
            decoded.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        simple = {
            "a": "\a",
            "b": "\b",
            "e": "\x1b",
            "E": "\x1b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            "'": "'",
            '"': '"',
            " ": " ",
        }
        if nxt in simple:
            decoded.append(simple[nxt])
            i += 2
            continue
        if nxt == "x":
            j = i + 2
            while j < min(i + 4, len(value)) and value[j] in "0123456789abcdefABCDEF":
                j += 1
            if j > i + 2:
                decoded.append(chr(int(value[i + 2 : j], 16)))
                i = j
                continue
        if nxt in {"u", "U"}:
            width = 4 if nxt == "u" else 8
            start = i + 2
            end = start + width
            if end <= len(value) and all(ch in "0123456789abcdefABCDEF" for ch in value[start:end]):
                codepoint = int(value[start:end], 16)
                if codepoint <= 0x10FFFF:
                    decoded.append(chr(codepoint))
                    i = end
                    continue
        if nxt == "c" and i + 2 < len(value):
            decoded.append(chr(ord(value[i + 2].upper()) & 0x1F))
            i += 3
            continue
        if nxt in "01234567":
            j = i + 1
            while j < min(i + 4, len(value)) and value[j] in "01234567":
                j += 1
            decoded.append(chr(int(value[i + 1 : j], 8)))
            i = j
            continue
        decoded.append(nxt)
        i += 2
    return "".join(decoded)


def _strip_line_comment(line: str, quote: str | None) -> tuple[str, str | None]:
    """Remove a shell line comment while preserving quote state across physical lines."""
    emitted: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "single":
            emitted.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue

        if quote == "double":
            emitted.append(ch)
            if ch == "\\" and i + 1 < len(line):
                i += 1
                emitted.append(line[i])
            elif ch == '"':
                quote = None
            i += 1
            continue

        if ch == "\\" and i + 1 < len(line):
            emitted.extend((ch, line[i + 1]))
            i += 2
            continue
        if line.startswith("$((", i) or line.startswith("((", i):
            end = _arithmetic_span_end(line, i)
            emitted.append(line[i:end])
            i = end
            continue
        if line.startswith("$(", i):
            _body, end = _extract_balanced_shell_text(line, i, "$(", ")")
            emitted.append(line[i:end])
            i = end
            continue
        if line.startswith("<(", i) or line.startswith(">(", i):
            _body, end = _extract_balanced_shell_text(line, i, line[i : i + 2], ")")
            emitted.append(line[i:end])
            i = end
            continue
        if ch == "`":
            end = _backtick_span_end(line, i)
            emitted.append(line[i:end])
            i = end
            continue
        if ch == "'":
            quote = "single"
        elif ch == '"':
            quote = "double"
        elif _is_comment_start(line, i, emitted):
            break
        emitted.append(ch)
        i += 1
    return "".join(emitted), quote


def _has_line_continuation(code_line: str) -> bool:
    backslashes = 0
    for ch in reversed(code_line):
        if ch != "\\":
            break
        backslashes += 1
    return backslashes % 2 == 1


def _unquote_heredoc_delimiter(raw: str) -> str:
    try:
        parts = shlex.split(raw)
    except ValueError:
        return raw.strip("'\"")
    return parts[0] if parts else raw


def _heredoc_redirect_fd(code_line: str, heredoc_index: int) -> str | None:
    i = heredoc_index - 1
    while i >= 0 and code_line[i].isdigit():
        i -= 1
    if i == heredoc_index - 1:
        return None
    if i >= 0 and not (code_line[i].isspace() or code_line[i] in ";|&({"):
        return None
    return code_line[i + 1 : heredoc_index]


_SHELL_INTERPRETERS = frozenset(
    {"ash", "bash", "csh", "dash", "fish", "ksh", "ksh93", "mksh", "pdksh", "rbash", "sh", "tcsh", "zsh"}
)
_STDIN_SHELL_SCRIPT_PATHS = frozenset({"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})


def _is_shell_interpreter_name(command: str) -> bool:
    name = os.path.basename(command)
    return name in _SHELL_INTERPRETERS or re.fullmatch(r"(?:ba|da|mk|pdk|z|k)?sh-?[0-9.]*", name) is not None


def _is_shell_c_option(tok: str) -> bool:
    if tok == "-c":
        return True
    if not tok.startswith("-") or tok.startswith("--"):
        return False
    return "c" in tok[1:]


def _last_simple_command(tokens: list[str]) -> list[str]:
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPS:
            current = []
        else:
            current.append(tok)
    return current


def _unwrap_command_argv(argv: list[str]) -> list[str]:
    argv = _execution_argv(argv)
    while argv:
        cmd = os.path.basename(argv[0])
        if cmd in {"!", "coproc"}:
            argv = _execution_argv(argv[1:])
            continue
        if cmd == "busybox":
            argv = _execution_argv(argv[1:])
            continue
        if cmd == "builtin":
            if len(argv) > 1 and argv[1] in {"command", "exec", "eval", "source", ".", "trap"}:
                argv = argv[1:]
                continue
            return argv
        if cmd == "env":
            i = 1
            split_string = False
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if _INLINE_ENV.match(tok):
                    i += 1
                    continue
                if tok in {"-S", "--split-string"}:
                    if i + 1 >= len(argv):
                        return []
                    try:
                        argv = _segment_argv(shlex.split(argv[i + 1]) + argv[i + 2:])
                    except ValueError:
                        return []
                    split_string = True
                    break
                if tok.startswith("-") and not tok.startswith("--") and "S" in tok[1:]:
                    optchars = tok[1:]
                    split_index = optchars.find("S")
                    attached = optchars[split_index + 1 :]
                    if attached:
                        split_arg = attached
                        remaining = argv[i + 1 :]
                    elif i + 1 < len(argv):
                        split_arg = argv[i + 1]
                        remaining = argv[i + 2 :]
                    else:
                        return []
                    try:
                        argv = _segment_argv(shlex.split(split_arg) + remaining)
                    except ValueError:
                        return []
                    split_string = True
                    break
                if tok.startswith("--split-string="):
                    try:
                        argv = _segment_argv(shlex.split(tok.split("=", 1)[1]) + argv[i + 1:])
                    except ValueError:
                        return []
                    split_string = True
                    break
                if tok.startswith("--unset=") or tok.startswith("--chdir="):
                    i += 1
                    continue
                if tok.startswith("-"):
                    if tok in {"-u", "--unset", "-C", "--chdir"} and i + 1 < len(argv):
                        i += 2
                    else:
                        i += 1
                    continue
                break
            if not split_string:
                argv = argv[i:]
            continue
        if cmd == "command":
            i = 1
            while i < len(argv):
                if argv[i] == "--":
                    i += 1
                    break
                if argv[i] == "-p":
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "exec":
            i = 1
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok == "-a" and i + 1 < len(argv):
                    i += 2
                    continue
                if tok in {"-c", "-l"}:
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "noglob":
            argv = argv[1:]
            continue
        if cmd == "nohup":
            argv = argv[1:]
            continue
        if cmd == "watch":
            i = 1
            options_with_args = {"-n", "--interval", "--precise", "--color"}
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if tok.startswith(("--interval=", "--precise=", "--color=")):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            argv = _execution_argv(argv[i:])
            continue
        if cmd == "parallel":
            i = 1
            options_with_args = {
                "-a",
                "--arg-file",
                "-j",
                "--jobs",
                "--joblog",
                "--results",
                "--colsep",
                "--sshlogin",
                "-S",
            }
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args if opt.startswith("--")):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            command_template: list[str] = []
            while i < len(argv) and argv[i] not in {":::", "::::", ":::+", "::::+"}:
                command_template.append(argv[i])
                i += 1
            argv = _execution_argv(command_template)
            continue
        if cmd == "unbuffer":
            i = 1
            while i < len(argv):
                if argv[i] == "--":
                    i += 1
                    break
                if argv[i] == "-p":
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "setsid":
            i = 1
            while i < len(argv):
                if argv[i] == "--":
                    i += 1
                    break
                if argv[i] in {"-w", "-f", "-c", "--wait", "--fork", "--ctty"}:
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "stdbuf":
            i = 1
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-i", "-o", "-e", "--input", "--output", "--error"} and i + 1 < len(argv):
                    i += 2
                    continue
                if re.fullmatch(r"-[ioe].+", tok) or tok.startswith(
                    ("--input=", "--output=", "--error=")
                ):
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "ionice":
            i = 1
            options_with_args = {"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args if opt.startswith("--")):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "taskset":
            i = 1
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-c", "--cpu-list"} and i + 1 < len(argv):
                    i += 2
                    continue
                if tok.startswith("--cpu-list="):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                if os.path.basename(tok) not in {"gh", *(_SHELL_INTERPRETERS), "sudo", "doas", "env", "command"}:
                    i += 1
                break
            argv = argv[i:]
            continue
        if cmd == "chrt":
            i = 1
            options_with_args = {"-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline"}
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args if opt.startswith("--")):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                if os.path.basename(tok) not in {"gh", *(_SHELL_INTERPRETERS), "sudo", "doas", "env", "command"}:
                    i += 1
                break
            argv = argv[i:]
            continue
        if cmd == "flock":
            i = 1
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-c", "--command"} and i + 1 < len(argv):
                    return ["sh", "-c", argv[i + 1]]
                if tok.startswith("--command="):
                    return ["sh", "-c", tok.split("=", 1)[1]]
                if tok in {"-w", "--timeout", "-E", "--conflict-exit-code"} and i + 1 < len(argv):
                    i += 2
                    continue
                if tok.startswith("--timeout=") or tok.startswith("--conflict-exit-code="):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                i += 1
                break
            argv = argv[i:]
            continue
        if cmd == "setpriv":
            i = 1
            options_with_args = {
                "--ruid",
                "--euid",
                "--reuid",
                "--rgid",
                "--egid",
                "--regid",
                "--groups",
                "--ptracer",
                "--securebits",
                "--pdeathsig",
                "--ambient-caps",
                "--inh-caps",
                "--bounding-set",
                "--selinux-label",
                "--apparmor-profile",
                "--landlock-access",
                "--landlock-rule",
            }
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "xargs":
            i = 1
            options_with_args = {
                "-a",
                "-d",
                "-E",
                "-I",
                "-J",
                "-L",
                "-P",
                "-S",
                "-n",
                "-s",
                "--arg-file",
                "--delimiter",
                "--eof",
                "--max-args",
                "--max-chars",
                "--max-lines",
                "--max-procs",
                "--replace",
            }
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-I", "-J", "--replace"} and i + 1 < len(argv):
                    _replacement, i = _xargs_replacement_value(argv, i + 1)
                    continue
                if tok in options_with_args and i + 1 < len(argv):
                    i += 1 if _xargs_option_operand_missing(argv[i + 1]) else 2
                    continue
                if tok.startswith("--") and "=" in tok:
                    name = tok.split("=", 1)[0]
                    if name in options_with_args:
                        i += 1
                        continue
                if re.fullmatch(r"-(?:[adIJLPSEns].+|[0-9]+)", tok):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            argv = argv[i:]
            continue
        if cmd == "time":
            i = 1
            options_with_args = {"-o", "-f", "--output", "--format"}
            bare_options = {"-p", "-v", "-a", "-l", "-h", "--verbose", "--append", "--portability"}
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args if opt.startswith("--")):
                    i += 1
                    continue
                if tok in bare_options:
                    i += 1
                    continue
                break
            argv = _execution_argv(argv[i:])
            continue
        if cmd in {"sudo", "doas"}:
            i = 1
            shell_mode = False
            options_with_args = {
                "-u",
                "-g",
                "-h",
                "-p",
                "-C",
                "-D",
                "-r",
                "-R",
                "-t",
                "-T",
                "-a",
                "-U",
                "--user",
                "--group",
                "--host",
                "--prompt",
                "--close-from",
                "--chdir",
                "--role",
                "--chroot",
                "--type",
                "--command-timeout",
                "--auth-type",
                "--other-user",
            }
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if _INLINE_ENV.match(tok):
                    i += 1
                    continue
                if tok in {"-s", "-i", "--shell", "--login"}:
                    shell_mode = True
                    i += 1
                    continue
                if not tok.startswith("-") or tok == "-":
                    break
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args if opt.startswith("--")):
                    i += 1
                    continue
                i += 1
            if shell_mode and i < len(argv):
                return ["sh", "-c", " ".join(argv[i:])]
            argv = argv[i:]
            continue
        if cmd == "runuser":
            i = 1
            saw_user_form = False
            options_with_args = {
                "-c",
                "--command",
                "--session-command",
                "-g",
                "--group",
                "-G",
                "--supp-group",
                "-s",
                "--shell",
                "-u",
                "--user",
                "-w",
                "--whitelist-environment",
            }
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-u", "--user"}:
                    saw_user_form = True
                    i += 2 if i + 1 < len(argv) else 1
                    continue
                if tok.startswith("--user="):
                    saw_user_form = True
                    i += 1
                    continue
                if tok in {"-c", "--command", "--session-command"} and i + 1 < len(argv):
                    return ["sh", "-c", argv[i + 1]]
                if tok.startswith("--command=") or tok.startswith("--session-command="):
                    return ["sh", "-c", tok.split("=", 1)[1]]
                if tok in options_with_args and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in options_with_args if opt.startswith("--")):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            if not saw_user_form:
                return argv
            argv = argv[i:]
            continue
        if cmd == "su":
            i = 1
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-c", "--command", "--session-command"} and i + 1 < len(argv):
                    return ["sh", "-c", argv[i + 1]]
                if tok.startswith("--command=") or tok.startswith("--session-command="):
                    return ["sh", "-c", tok.split("=", 1)[1]]
                if tok in {"-s", "--shell", "-g", "--group", "-G", "--supp-group"} and i + 1 < len(argv):
                    i += 2
                    continue
                if any(tok.startswith(f"{opt}=") for opt in {"--shell", "--group", "--supp-group"}):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                i += 1
            argv = argv[i:]
            continue
        if cmd == "nice":
            i = 1
            if i < len(argv) and argv[i] == "-n":
                i += 2
            elif i < len(argv) and re.fullmatch(r"-\d+", argv[i]):
                i += 1
            elif i < len(argv) and argv[i] == "--adjustment":
                i += 2
            elif i < len(argv) and argv[i].startswith("--adjustment="):
                i += 1
            argv = argv[i:]
            continue
        if cmd in {"timeout", "gtimeout"}:
            i = 1
            while i < len(argv):
                tok = argv[i]
                if tok == "--":
                    i += 1
                    break
                if tok in {"-k", "--kill-after", "-s", "--signal"} and i + 1 < len(argv):
                    i += 2
                    continue
                if tok.startswith("--kill-after=") or tok.startswith("--signal="):
                    i += 1
                    continue
                if tok.startswith("-"):
                    i += 1
                    continue
                break
            if i < len(argv):
                i += 1
            argv = argv[i:]
            continue
        return argv
    return argv


def _execution_argv(segment: list[str]) -> list[str]:
    argv = _segment_argv(segment)
    while argv:
        cmd = argv[0]
        if cmd in {"if", "while", "until", "then", "do", "else", "elif", "!", "coproc"}:
            argv = _segment_argv(argv[1:])
            continue
        return argv
    return argv


def _shell_args(argv: list[str]) -> list[str] | None:
    argv = _unwrap_command_argv(argv)
    if not argv or not _is_shell_interpreter_name(argv[0]):
        return None
    return argv[1:]


def _shell_c_script(argv: list[str]) -> str | None:
    args = _shell_args(argv)
    if args is None:
        return None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return None
        if tok == "-c":
            return args[i + 1] if i + 1 < len(args) else ""
        if _is_shell_c_option(tok):
            return args[i + 1] if i + 1 < len(args) else ""
        if not tok.startswith("-"):
            return None
        i = _next_shell_option_index(args, i)
    return None


def _shell_reads_stdin(argv: list[str]) -> bool:
    args = _shell_args(argv)
    if args is None:
        return False
    forced_stdin = False
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return forced_stdin or i + 1 == len(args)
        if _is_shell_c_option(tok):
            return False
        if _is_shell_stdin_option(tok):
            forced_stdin = True
            i = _next_shell_option_index(args, i)
            continue
        if not tok.startswith("-"):
            return forced_stdin or tok in _STDIN_SHELL_SCRIPT_PATHS
        i = _next_shell_option_index(args, i)
    return True


def _shell_c_script_reads_stdin_as_shell(argv: list[str], depth: int = 0) -> bool:
    script = _shell_c_script(argv)
    if script is None:
        return False
    try:
        source = _command_source_for_detection(script, _depth=depth + 1)
        segments = _split_segments(source, prepared=True)
    except ValueError:
        return _has_merge_hint(script, max_gap=None)
    for segment in segments:
        clean = _tokens_without_redirections(segment)
        if _shell_reads_stdin(clean):
            return True
        if _source_command_reads_stdin_path(clean):
            return True
    return False


def _is_shell_stdin_option(tok: str) -> bool:
    return tok == "-s" or (tok.startswith("-") and not tok.startswith("--") and "s" in tok[1:])


def _next_shell_option_index(args: list[str], i: int) -> int:
    tok = args[i]
    if tok in {"-o", "-O", "+O", "--rcfile", "--init-file"} and i + 1 < len(args):
        return i + 2
    if tok.startswith(("-O", "+O")) and tok not in {"-O", "+O"}:
        return i + 1
    if tok.startswith(("-", "+")) and not tok.startswith("--") and "o" in tok[1:] and i + 1 < len(args):
        return i + 2
    if tok.startswith("--rcfile=") or tok.startswith("--init-file="):
        return i + 1
    return i + 1


def _line_runs_shell_interpreter(code_line: str, heredoc_index: int, depth: int = 0) -> bool:
    try:
        lex = shlex.shlex(code_line, posix=True, punctuation_chars=True)
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return False
    tokens = [tok for tok in tokens if tok.strip()]
    command_tokens, heredoc_token_index = _command_tokens_around_heredoc(code_line, heredoc_index, tokens)
    components = _pipeline_components_with_spans(command_tokens)
    owner_index = _component_index_for_token(components, heredoc_token_index)
    if owner_index is None:
        return False
    for index, (component, _start, _end) in enumerate(components[owner_index:], start=owner_index):
        clean_component = _tokens_without_redirections(component)
        if _shell_reads_stdin(clean_component) or _shell_c_script_reads_stdin_as_shell(clean_component, depth):
            if index == owner_index and _has_later_stdin_redirection(component, heredoc_token_index - _start):
                return False
            if index != owner_index and _component_has_stdin_redirection(component):
                continue
            return True
    return False


def _command_tokens_around_heredoc(code_line: str, heredoc_index: int, tokens: list[str]) -> tuple[list[str], int]:
    span_start, span_end = _command_span_around_index(code_line, heredoc_index)
    prefix_tokens = _source_tokens(code_line[:span_start])
    span_tokens = _source_tokens(code_line[span_start:span_end])
    heredoc_prefix_tokens = _source_tokens(code_line[span_start:heredoc_index])
    return tokens[len(prefix_tokens) : len(prefix_tokens) + len(span_tokens)], len(heredoc_prefix_tokens)


def _command_span_around_index(source: str, index: int) -> tuple[int, int]:
    quote: str | None = None
    start = 0
    end = len(source)
    i = 0
    while i < len(source):
        ch = source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "'":
            quote = "single"
            i += 1
            continue
        if ch == '"':
            quote = "double"
            i += 1
            continue
        sep_len = _command_separator_len(source, i)
        if sep_len:
            if i < index:
                start = i + sep_len
            elif i > index:
                end = i
                break
            i += sep_len
            continue
        i += 1
    return start, end


def _command_separator_len(source: str, i: int) -> int:
    if source.startswith(";;&", i):
        return 3
    if (
        source.startswith("&&", i)
        or source.startswith("||", i)
        or source.startswith(";;", i)
        or source.startswith(";&", i)
    ):
        return 2
    ch = source[i]
    if ch == ";":
        return 1
    if ch == "&":
        if (
            (i > 0 and source[i - 1] in "<>|")
            or (i + 1 < len(source) and source[i + 1] == ">")
        ):
            return 0
        return 1
    return 0


def _pipeline_components(tokens: list[str]) -> list[list[str]]:
    return [component for component, _start, _end in _pipeline_components_with_spans(tokens)]


def _pipeline_components_with_spans(tokens: list[str]) -> list[tuple[list[str], int, int]]:
    components: list[tuple[list[str], int, int]] = []
    start = 0
    current: list[str] = []
    for index, tok in enumerate(tokens):
        if tok in _PIPE_SEPS:
            if current:
                components.append((current, start, index))
                current = []
            start = index + 1
        else:
            current.append(tok)
    if current:
        components.append((current, start, len(tokens)))
    return components


def _component_index_for_token(components: list[tuple[list[str], int, int]], token_index: int) -> int | None:
    for index, (_component, start, end) in enumerate(components):
        if start <= token_index < end:
            return index
    return None


def _tokens_without_redirections(tokens: list[str]) -> list[str]:
    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isdigit() and i + 1 < len(tokens) and tokens[i + 1] in _REDIRECT_OPS:
            if tokens[i + 1] == "<<" and i + 2 < len(tokens) and tokens[i + 2] == "-":
                i += 4
                continue
            i += 3
            continue
        if tok in _REDIRECT_OPS or tok.startswith("<<"):
            if tok == "<<" and i + 1 < len(tokens) and tokens[i + 1] == "-":
                i += 3
                continue
            i += 2
            continue
        cleaned.append(tok)
        i += 1
    return cleaned


def _component_has_stdin_redirection(tokens: list[str]) -> bool:
    return bool(_stdin_redirection_indices(tokens))


def _has_later_stdin_redirection(tokens: list[str], index: int) -> bool:
    return any(redirect_index > index for redirect_index in _stdin_redirection_indices(tokens))


def _stdin_redirection_indices(tokens: list[str]) -> list[int]:
    indices: list[int] = []
    stdin_ops = {"<", "<<", "<<<", "<>", "<&"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isdigit() and i + 1 < len(tokens) and tokens[i + 1] in stdin_ops:
            if tok == "0":
                indices.append(i + 1)
            i += 3
            continue
        if tok in stdin_ops or tok.startswith("<<"):
            if i == 0 or not tokens[i - 1].isdigit():
                indices.append(i)
            i += 2
            continue
        i += 1
    return indices


def _heredocs_from_code_line(code_line: str, quote: str | None = None, depth: int = 0) -> list[_HereDoc]:
    heredocs: list[_HereDoc] = []
    i = 0
    while i < len(code_line):
        ch = code_line[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(code_line):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue

        if ch == "\\" and i + 1 < len(code_line):
            i += 2
            continue
        if code_line.startswith("$((", i) or code_line.startswith("((", i):
            i = _arithmetic_span_end(code_line, i)
            continue
        if ch == "`":
            i = _backtick_span_end(code_line, i)
            continue
        if ch == "'":
            quote = "single"
            i += 1
            continue
        if ch == '"':
            quote = "double"
            i += 1
            continue
        if ch != "<" or i + 1 >= len(code_line) or code_line[i + 1] != "<":
            i += 1
            continue

        j = i + 2
        strip_tabs = False
        if j < len(code_line) and code_line[j] == "-":
            strip_tabs = True
            j += 1
        while j < len(code_line) and code_line[j] in " \t":
            j += 1

        start = j
        delim_quote: str | None = None
        while j < len(code_line):
            dch = code_line[j]
            if delim_quote == "single":
                if dch == "'":
                    delim_quote = None
                j += 1
                continue
            if delim_quote == "double":
                if dch == "\\" and j + 1 < len(code_line):
                    j += 2
                    continue
                if dch == '"':
                    delim_quote = None
                j += 1
                continue
            if dch == "\\" and j + 1 < len(code_line):
                j += 2
                continue
            if dch == "'":
                delim_quote = "single"
                j += 1
                continue
            if dch == '"':
                delim_quote = "double"
                j += 1
                continue
            if dch.isspace() or dch in ";|&()<>":
                break
            j += 1

        raw_delimiter = code_line[start:j]
        if raw_delimiter:
            redirect_fd = _heredoc_redirect_fd(code_line, i)
            inspect_body = redirect_fd in {None, "0"} and _line_runs_shell_interpreter(code_line, i, depth)
            heredocs.append(
                _HereDoc(
                    _unquote_heredoc_delimiter(raw_delimiter),
                    strip_tabs,
                    inspect_body,
                )
            )
        i = j
    return heredocs


def _extract_balanced_shell_text(source: str, start: int, opener: str, closer: str) -> tuple[str, int]:
    depth = 1
    quote: str | None = None
    body_start = start + len(opener)
    i = body_start
    while i < len(source):
        ch = source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "'":
            quote = "single"
            i += 1
            continue
        if ch == '"':
            quote = "double"
            i += 1
            continue
        if source.startswith(opener, i):
            depth += 1
            i += len(opener)
            continue
        if ch == "(" and opener.endswith("("):
            depth += 1
            i += 1
            continue
        if source.startswith(closer, i):
            depth -= 1
            if depth == 0:
                return source[body_start:i], i + len(closer)
            i += len(closer)
            continue
        i += 1
    return source[body_start:], len(source)


def _source_contains_nested_gh_pr_merge(source: str, depth: int) -> bool | None:
    quote: str | None = None
    i = 0
    while i < len(source):
        ch = source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(source):
                i += 2
                continue
            if source.startswith("$(", i) and not source.startswith("$((", i):
                body, end = _extract_balanced_shell_text(source, i, "$(", ")")
                nested = _command_contains_gh_pr_merge(body, _depth=depth + 1)
                if nested is not False:
                    return nested
                i = end
                continue
            if ch == "`":
                end = source.find("`", i + 1)
                body = source[i + 1 :] if end == -1 else source[i + 1 : end]
                nested = _command_contains_gh_pr_merge(body, _depth=depth + 1)
                if nested is not False:
                    return nested
                i = len(source) if end == -1 else end + 1
                continue
            if ch == '"':
                quote = None
            i += 1
            continue

        function_end = _function_definition_text_end(source, i)
        if function_end is not None:
            i = function_end
            continue
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "'":
            quote = "single"
            i += 1
            continue
        if ch == '"':
            quote = "double"
            i += 1
            continue
        if source.startswith("$(", i) and not source.startswith("$((", i):
            body, end = _extract_balanced_shell_text(source, i, "$(", ")")
            nested = _command_contains_gh_pr_merge(body, _depth=depth + 1)
            if nested is not False:
                return nested
            i = end
            continue
        if source.startswith("<(", i) or source.startswith(">(", i):
            body, end = _extract_balanced_shell_text(source, i, source[i : i + 2], ")")
            nested = _command_contains_gh_pr_merge(body, _depth=depth + 1)
            if nested is not False:
                return nested
            i = end
            continue
        if ch == "`":
            end = source.find("`", i + 1)
            body = source[i + 1 :] if end == -1 else source[i + 1 : end]
            nested = _command_contains_gh_pr_merge(body, _depth=depth + 1)
            if nested is not False:
                return nested
            i = len(source) if end == -1 else end + 1
            continue
        i += 1
    return False


def _source_for_raw_nested_scan(command: str) -> str:
    """Strip top-level comments/heredoc bodies before raw nested-shell scanning."""
    prepared: list[str] = []
    pending_heredocs: list[_HereDoc] = []
    quote: str | None = None
    for raw_line in command.splitlines(keepends=True):
        has_newline = raw_line.endswith("\n")
        line = raw_line[:-1] if has_newline else raw_line
        if pending_heredocs:
            candidate = line.lstrip("\t") if pending_heredocs[0].strip_tabs else line
            if candidate == pending_heredocs[0].delimiter:
                pending_heredocs.pop(0)
            continue
        quote_in = quote
        code_line, quote = _strip_line_comment(_normalize_dollar_quotes(line), quote)
        prepared.append(code_line)
        if has_newline:
            prepared.append("\n")
        if quote is None:
            masked = _mask_nested_shell_spans(code_line)
            pending_heredocs.extend(_heredocs_from_code_line(masked, quote_in))
    return "".join(prepared)


def _mask_nested_shell_spans(source: str) -> str:
    chars = list(source)
    quote: str | None = None
    i = 0
    while i < len(source):
        ch = source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "'":
            quote = "single"
            i += 1
            continue
        if ch == '"':
            quote = "double"
            i += 1
            continue
        if source.startswith("$((", i) or source.startswith("((", i):
            i = _arithmetic_span_end(source, i)
            continue
        if source.startswith("$(", i) or source.startswith("<(", i) or source.startswith(">(", i):
            opener = "$(" if source.startswith("$(", i) else source[i : i + 2]
            _body, end = _extract_balanced_shell_text(source, i, opener, ")")
            for j in range(i, end):
                chars[j] = " "
            i = end
            continue
        if ch == "`":
            end = _backtick_span_end(source, i)
            for j in range(i, end):
                chars[j] = " "
            i = end
            continue
        i += 1
    return "".join(chars)


def _command_source_for_detection(command: str, *, _depth: int = 0, allow_oversize: bool = False) -> str:
    """Return shell source with only executable command text kept for shlex.

    Bare newlines become command separators, comments end at their physical line, quoted
    newlines remain literal, and heredoc bodies are skipped because they are stdin text.
    """
    if _depth > _MAX_DETECTION_DEPTH or (len(command) > _MAX_COMMAND_CHARS and not allow_oversize):
        raise ValueError("command exceeds block-raw-pr-merge detection budget")
    command = _normalize_dollar_quotes(command)
    prepared: list[str] = []
    pending_heredocs: list[_HereDoc] = []
    pending_bodies: list[list[str]] = []
    continued_heredocs: list[_HereDoc] = []
    quote: str | None = None

    for raw_line in command.splitlines(keepends=True):
        has_newline = raw_line.endswith("\n")
        line = raw_line[:-1] if has_newline else raw_line

        if pending_heredocs:
            candidate = line.lstrip("\t") if pending_heredocs[0].strip_tabs else line
            if candidate == pending_heredocs[0].delimiter:
                heredoc = pending_heredocs.pop(0)
                body = pending_bodies.pop(0)
                if heredoc.inspect_body:
                    prepared.append(_command_source_for_detection("".join(body), _depth=_depth + 1))
            elif pending_heredocs[0].inspect_body:
                pending_bodies[0].append(raw_line)
            continue

        quote_in = quote
        code_line, quote = _strip_line_comment(line, quote)
        line_heredocs = _heredocs_from_code_line(code_line, quote_in, _depth) if quote is None else []

        if has_newline and quote is None and _has_line_continuation(code_line):
            prepared.append(code_line[:-1])
            continued_heredocs.extend(line_heredocs)
            continue

        prepared.append(code_line)
        if has_newline:
            prepared.append("\n" if quote is not None else " ; ")
        if quote is None:
            pending_heredocs.extend(continued_heredocs)
            pending_bodies.extend([] for _ in continued_heredocs)
            pending_heredocs.extend(line_heredocs)
            pending_bodies.extend([] for _ in line_heredocs)
            continued_heredocs = []

    for heredoc, body in zip(pending_heredocs, pending_bodies):
        if heredoc.inspect_body:
            prepared.append(_command_source_for_detection("".join(body), _depth=_depth + 1))
    return _normalize_dollar_quotes("".join(prepared))


def _split_segments(command: str, *, prepared: bool = False) -> list[list[str]]:
    """Tokenize `command` via shlex and split into per-segment token lists.

    Uses ``punctuation_chars=True`` so that `;`, `&&`, `||`, `|` etc. are
    returned as standalone separator tokens rather than being glued to adjacent
    words (the default shlex.split() behaviour leaves `done;` as one token,
    missing the `;` separator entirely).

    Each segment is one independent command (the shell atoms between separators).
    Returns a list of token lists; raises ValueError on unbalanced quotes
    (caller treats this as fail-closed).
    """
    command_source = command if prepared else _command_source_for_detection(command)
    tokens = _source_tokens(command_source)  # raises ValueError on unbalanced quotes
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPS:
            if current:
                segments.append(current)
                current = []
        elif tok.strip():
            current.append(tok)
        # A pure-whitespace token is never a real argv element. shlex (whitespace_split=False)
        # emits a standalone `'\n'` token for a `\`-newline line continuation; dropping it keeps
        # `VAR=x \`+newline+`gh pr merge` from hiding the merge behind that stray token, which
        # would else strip the `VAR=` assignment, see argv[0] == '\n', and ALLOW the raw merge.
    if current:
        segments.append(current)
    return segments


def _segment_argv(segment: list[str]) -> list[str]:
    """Return the real argv by stripping leading shell noise and VAR=value assignments.

    Strips leading grouping/control tokens (`(`, `{`, `!`, …) so that
    `( gh pr merge 5 )` or `{ gh pr merge 5; }` are correctly detected.
    Then strips leading VAR=value inline env assignments so that
    `GH_TOKEN=x gh pr merge 5` is correctly detected.
    """
    i = 0
    while i < len(segment) and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    while i < len(segment) and _INLINE_ENV.match(segment[i]):
        i += 1
    return segment[i:]


def _is_gh_pr_merge(segment: list[str]) -> bool:
    """Return True iff this segment's argv is a `gh pr merge` invocation.

    Uses ``os.path.basename`` on argv[0] so that path-qualified invocations
    such as ``/opt/homebrew/bin/gh pr merge 5`` are correctly detected.
    """
    argv = _gh_command_argv(_unwrap_command_argv(_tokens_without_redirections(segment)))
    return _gh_argv_is_pr_merge(argv)


def _gh_argv_is_pr_merge(argv: list[str]) -> bool:
    return (
        len(argv) >= 3
        and os.path.basename(argv[0]) == "gh"
        and argv[1] == "pr"
        and argv[2] == "merge"
    )


def _gh_command_argv(argv: list[str]) -> list[str]:
    if not argv or os.path.basename(argv[0]) != "gh":
        return argv
    if _gh_help_or_version_requested(argv[1:]):
        return []
    return [argv[0], *_strip_gh_inherited_flags(argv[1:])]


def _gh_help_or_version_requested(args: list[str]) -> bool:
    value_flags = {
        "-A",
        "-b",
        "-F",
        "-R",
        "-t",
        "--repo",
        "--hostname",
        "--config-dir",
        "--body",
        "--body-file",
        "--subject",
        "--author-email",
        "--match-head-commit",
    }
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return False
        if tok in value_flags:
            i += 2
            continue
        if any(tok.startswith(f"{flag}=") for flag in value_flags if flag.startswith("--")):
            i += 1
            continue
        if any(tok.startswith(flag) and tok != flag for flag in {"-A", "-b", "-F", "-R", "-t"}):
            i += 1
            continue
        if tok in {"--help", "-h", "--version"}:
            return True
        i += 1
    return False


def _strip_gh_inherited_flags(args: list[str]) -> list[str]:
    stripped: list[str] = []
    i = 0
    flags_with_args = {"-R", "--repo", "--hostname", "--config-dir"}
    bare_flags = {"--verbose", "--paginate", "--no-pager"}
    while i < len(args):
        tok = args[i]
        if tok == "--":
            if not stripped:
                return ["--", *args[i + 1 :]]
            stripped.extend(args[i + 1 :])
            break
        if tok in flags_with_args:
            i += 2
            continue
        if any(tok.startswith(f"{flag}=") for flag in flags_with_args if flag.startswith("--")):
            i += 1
            continue
        if tok.startswith("-R") and tok != "-R":
            i += 1
            continue
        if tok in bare_flags:
            i += 1
            continue
        stripped.append(tok)
        i += 1
    return stripped


def _source_has_case_arm_gh_pr_merge(command_source: str, depth: int) -> bool | None:
    tokens = _source_tokens(_source_without_function_definitions(command_source))
    case_patterns: list[bool] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        function_end = _function_definition_end(tokens, i)
        if function_end is not None:
            i = function_end
            continue
        if tok == "case":
            case_patterns.append(False)
            i += 1
            continue
        if tok == "esac":
            if case_patterns:
                case_patterns.pop()
            i += 1
            continue
        if not case_patterns:
            i += 1
            continue
        if tok == "in":
            case_patterns[-1] = True
            i += 1
            continue
        if case_patterns[-1] and tok == ")":
            result = _case_arm_tokens_contain_gh_pr_merge(tokens[i + 1 :], depth)
            if result is not False:
                return result
        i += 1
    return False


def _case_arm_tokens_contain_gh_pr_merge(tokens: list[str], depth: int) -> bool | None:
    arm: list[str] = []
    nested_case_depth = 0
    for tok in tokens:
        if tok == "case":
            nested_case_depth += 1
        elif tok == "esac":
            if nested_case_depth == 0:
                break
            nested_case_depth -= 1
        if nested_case_depth == 0 and tok in {";;", ";&", ";;&"}:
            break
        arm.append(tok)
    if not arm:
        return False
    return _command_contains_gh_pr_merge(shlex.join(arm), _depth=depth + 1)


def _source_pipeline_feeds_shell_with_gh_pr_merge(command_source: str, depth: int) -> bool | None:
    tokens = _source_tokens(command_source)
    for pipeline_tokens in _pipeline_groups_from_tokens(tokens):
        result = _pipeline_tokens_feed_shell_with_gh_pr_merge(pipeline_tokens, depth)
        if result is not False:
            return result
    return False


def _pipeline_tokens_feed_shell_with_gh_pr_merge(pipeline_tokens: list[str], depth: int) -> bool | None:
    components = _pipeline_components(pipeline_tokens)
    if len(components) < 2:
        return False
    for index, component in enumerate(components[1:], start=1):
        xargs_result = _component_xargs_static_gh_pr_merge(component, components[:index], depth)
        if xargs_result is not False:
            return xargs_result
        if _component_has_stdin_redirection(component):
            continue
        clean_component = _tokens_without_redirections(component)
        if not (
            _shell_reads_stdin(clean_component)
            or _shell_c_script_reads_stdin_as_shell(clean_component, depth)
            or _source_command_reads_stdin_path(clean_component)
        ):
            continue
        # Conservative: an upstream static producer may survive pass-through filters
        # like `grep`, so inspect all earlier producers rather than only the adjacent one.
        for producer in components[:index]:
            script = _static_pipeline_script_from_component(producer)
            if script is None:
                continue
            result = _command_contains_gh_pr_merge(script, _depth=depth + 1)
            if result is not False:
                return result
    return False


def _component_xargs_static_gh_pr_merge(
    component: list[str],
    producers: list[list[str]],
    depth: int,
) -> bool | None:
    argv = _execution_argv(_tokens_without_redirections(component))
    if not argv or os.path.basename(argv[0]) != "xargs":
        return False
    base_argv, replacement = _xargs_command_argv(argv)
    if _gh_argv_is_pr_merge(base_argv):
        return True
    for producer in producers:
        script = _static_pipeline_script_from_component(producer)
        if script is None:
            continue
        try:
            words = shlex.split(script)
        except ValueError:
            return None if _has_merge_hint(script, max_gap=None) else False
        if replacement is not None:
            argv = [arg.replace(replacement, " ".join(words)) for arg in base_argv]
        else:
            argv = base_argv + words
            if _shell_c_script(base_argv) == "" and _has_merge_hint(script, max_gap=None):
                return True
        if _gh_argv_is_pr_merge(_gh_command_argv(argv)):
            return True
        result = _command_contains_gh_pr_merge(
            " ".join(shlex.quote(arg) for arg in argv),
            _depth=depth + 1,
        )
        if result is not False:
            return result
    return False


def _xargs_command_argv(argv: list[str]) -> tuple[list[str], str | None]:
    replacement: str | None = None
    i = 1
    options_with_args = {
        "-a",
        "-d",
        "-E",
        "-L",
        "-P",
        "-S",
        "-n",
        "-s",
        "--arg-file",
        "--delimiter",
        "--eof",
        "--max-args",
        "--max-chars",
        "--max-lines",
        "--max-procs",
    }
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            i += 1
            break
        if tok in {"-I", "-J", "--replace"} and i + 1 < len(argv):
            replacement, i = _xargs_replacement_value(argv, i + 1)
            continue
        if (tok.startswith("-I") and tok != "-I") or (tok.startswith("-J") and tok != "-J"):
            replacement = tok[2:]
            i += 1
            continue
        if tok.startswith("--replace="):
            replacement = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in options_with_args and i + 1 < len(argv):
            i += 1 if _xargs_option_operand_missing(argv[i + 1]) else 2
            continue
        if tok.startswith("--") and "=" in tok:
            name = tok.split("=", 1)[0]
            if name in options_with_args:
                i += 1
                continue
        if re.fullmatch(r"-(?:[adLPSns].+|[0-9]+)", tok):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return _combine_split_brace_tokens(_unwrap_command_argv(argv[i:])), replacement


def _xargs_option_operand_missing(token: str) -> bool:
    return os.path.basename(token) in {
        "bash",
        "dash",
        "env",
        "gh",
        "sh",
        "sudo",
        "zsh",
    }


def _xargs_replacement_value(argv: list[str], index: int) -> tuple[str, int]:
    if index + 1 < len(argv) and argv[index] == "{" and argv[index + 1] == "}":
        return "{}", index + 2
    return argv[index], index + 1


def _combine_split_brace_tokens(argv: list[str]) -> list[str]:
    combined: list[str] = []
    i = 0
    while i < len(argv):
        if i + 1 < len(argv) and argv[i] == "{" and argv[i + 1] == "}":
            combined.append("{}")
            i += 2
            continue
        combined.append(argv[i])
        i += 1
    return combined


def _pipeline_groups_from_tokens(tokens: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _COMMAND_SEPS:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        groups.append(current)
    return groups


def _static_pipeline_script_from_component(component: list[str]) -> str | None:
    argv = _unwrap_command_argv(_tokens_without_redirections(component))
    if not argv:
        return None
    cmd = os.path.basename(argv[0])
    if cmd == "cat":
        for i, tok in enumerate(component):
            if tok == "<<<" and i + 1 < len(component):
                return component[i + 1]
    if cmd == "echo":
        decode_escapes = False
        args: list[str] = []
        for arg in argv[1:]:
            if re.fullmatch(r"-[neE]+", arg):
                for flag in arg[1:]:
                    if flag == "e":
                        decode_escapes = True
                    elif flag == "E":
                        decode_escapes = False
                continue
            if arg == "-e":
                decode_escapes = True
                continue
            if arg == "-E":
                decode_escapes = False
                continue
            if arg == "-n":
                continue
            args.append(arg)
        if not args:
            return None
        rendered = " ".join(args)
        return _decode_shell_escapes(rendered) if decode_escapes else rendered
    if cmd == "printf":
        args = argv[1:]
        if args and args[0] == "--":
            args = args[1:]
        if not args:
            return None
        return _render_static_printf(args[0], args[1:])
    return None


def _render_static_printf(format_string: str, args: list[str]) -> str:
    rendered = _decode_shell_escapes(format_string)
    pattern = re.compile(
        r"%%|%(?:(?P<position>\d+)\$)?[-+#0 ]*(?P<width>\d+|\*)?(?:\.(?P<precision>\d+|\*))?(?P<conv>[sbcdiouxX])"
    )

    def render_once(start_index: int) -> tuple[str, int]:
        arg_index = start_index

        def replace(match: re.Match[str]) -> str:
            nonlocal arg_index
            if match.group(0) == "%%":
                return "%"
            position = match.group("position")
            if position is not None:
                index = int(position) - 1
            else:
                if match.group("width") == "*":
                    arg_index += 1
                if match.group("precision") == "*":
                    arg_index += 1
                index = arg_index
                arg_index += 1
            value = args[index] if 0 <= index < len(args) else ""
            conv = match.group("conv")
            if conv == "c":
                return value[:1]
            if conv == "b":
                return _decode_shell_escapes(value)
            return value

        return pattern.sub(replace, rendered), arg_index

    if not args or re.search(r"%\d+\$", rendered):
        return render_once(0)[0]

    parts: list[str] = []
    arg_index = 0
    while arg_index < len(args):
        text, next_index = render_once(arg_index)
        parts.append(text)
        if next_index <= arg_index:
            break
        arg_index = next_index
    return "".join(parts)


def _source_has_called_function_with_gh_pr_merge(
    command_source: str,
    depth: int,
    functions: dict[str, str] | None = None,
    call_stack: frozenset[str] = frozenset(),
) -> bool:
    if depth > _MAX_DETECTION_DEPTH:
        raise ValueError("function scan exceeds block-raw-pr-merge detection budget")
    current_functions = dict(functions or {})
    chunk_start = 0
    i = 0
    quote: str | None = None
    command_start = True

    while i < len(command_source):
        ch = command_source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(command_source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command_source):
            i += 2
            command_start = False
            continue
        if ch == "'":
            quote = "single"
            command_start = False
            i += 1
            continue
        if ch == '"':
            quote = "double"
            command_start = False
            i += 1
            continue

        if command_start:
            definition = _source_function_definition_at(command_source, i)
            if definition is not None:
                name, body, end = definition
                if _source_chunk_calls_tainted_function(
                    command_source[chunk_start:i],
                    current_functions,
                    depth,
                    call_stack,
                ):
                    return True
                current_functions[name] = body
                i = end
                chunk_start = end
                command_start = False
                continue

        boundary_end = _source_command_boundary_end(command_source, i)
        if boundary_end is not None:
            command_start = True
            i = boundary_end
            continue
        if ch.isspace():
            i += 1
            continue
        command_start = False
        i += 1

    return _source_chunk_calls_tainted_function(
        command_source[chunk_start:],
        current_functions,
        depth,
        call_stack,
    )


def _source_chunk_calls_tainted_function(
    source: str,
    functions: dict[str, str],
    depth: int,
    call_stack: frozenset[str],
) -> bool:
    if not source.strip() or not functions:
        return False
    executable_source = _source_without_function_definitions(source)
    tokens = _source_tokens(executable_source)
    if not tokens:
        return False
    for name, body in list(functions.items()):
        if _tokens_call_function(tokens, name):
            if _function_body_source_contains_gh_pr_merge(name, body, functions, depth + 1, call_stack):
                return True
    for segment in _segments_from_tokens(tokens):
        script = _eval_script_from_segment(segment)
        if script is None:
            continue
        rendered = _render_static_command_substitutions(script, depth)
        for eval_source in {script, rendered}:
            if _source_has_called_function_with_gh_pr_merge(eval_source, depth + 1, functions, call_stack):
                return True
    return False


def _source_command_boundary_end(source: str, i: int) -> int | None:
    if source.startswith(("&&", "||"), i):
        return i + 2
    if source[i] in ";|&\n(){}":
        return i + 1
    for word in _SOURCE_COMMAND_START_WORDS:
        end = i + len(word)
        if not source.startswith(word, i):
            continue
        before_ok = i == 0 or not _is_shell_name_char(source[i - 1])
        after_ok = end == len(source) or not _is_shell_name_char(source[end])
        if before_ok and after_ok:
            return end
    return None


def _is_shell_name_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _function_body_source_contains_gh_pr_merge(
    name: str,
    body: str,
    functions: dict[str, str],
    depth: int,
    call_stack: frozenset[str],
) -> bool:
    if name in call_stack:
        return False
    if depth > _MAX_DETECTION_DEPTH:
        raise ValueError("function body scan exceeds block-raw-pr-merge detection budget")
    result = _command_contains_gh_pr_merge(body, _depth=depth + 1)
    if result is not False:
        return True
    return _source_has_called_function_with_gh_pr_merge(
        body,
        depth + 1,
        functions,
        call_stack | frozenset({name}),
    )


def _tokens_call_function(tokens: list[str], name: str, depth: int = 0) -> bool:
    if depth > _MAX_DETECTION_DEPTH:
        raise ValueError("function call scan exceeds block-raw-pr-merge detection budget")
    executable_tokens = _tokens_without_function_definitions(tokens)
    for segment in _segments_from_tokens(executable_tokens):
        if _segment_calls_function(segment, name):
            return True

    i = 0
    while i < len(executable_tokens):
        tok = executable_tokens[i]
        if tok == "$" and i + 1 < len(executable_tokens) and executable_tokens[i + 1] == "(":
            end = _matching_token(executable_tokens, i + 1, "(", ")")
            if end is None:
                return False
            if _tokens_call_function(executable_tokens[i + 2 : end], name, depth + 1):
                return True
            i = end + 1
            continue
        if tok in {"<(", ">("}:
            end = _matching_token(executable_tokens, i, tok, ")")
            if end is None:
                return False
            if _tokens_call_function(executable_tokens[i + 1 : end], name, depth + 1):
                return True
            i = end + 1
            continue
        if tok == "`":
            try:
                end = executable_tokens.index("`", i + 1)
            except ValueError:
                return False
            if _tokens_call_function(executable_tokens[i + 1 : end], name, depth + 1):
                return True
            i = end + 1
            continue
        i += 1
    return False


def _segment_calls_function(segment: list[str], name: str) -> bool:
    command_start_after = _LEADING_SHELL_NOISE | {"then", "do", "else", "elif", ")"}
    for i, tok in enumerate(segment):
        if i > 0 and segment[i - 1] not in command_start_after:
            continue
        argv = _unwrap_command_argv(_tokens_without_redirections(segment[i:]))
        if argv and argv[0] == name:
            return True
    return False


def _matching_token(tokens: list[str], start: int, opener: str, closer: str) -> int | None:
    depth = 1
    i = start + 1
    while i < len(tokens):
        if tokens[i] == opener:
            depth += 1
        elif tokens[i] == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _has_case_arm_gh_pr_merge(segment: list[str], depth: int) -> bool | None:
    if _function_definition_end(segment, 0) is not None:
        return False
    for i, tok in enumerate(segment):
        if tok == ")" and "case" in segment[:i] and "in" in segment[:i]:
            result = _case_arm_tokens_contain_gh_pr_merge(segment[i + 1 :], depth)
            if result is not False:
                return result
    return False


def _source_tokens(command_source: str) -> list[str]:
    lex = shlex.shlex(command_source, posix=True, punctuation_chars=True)
    lex.commenters = ""
    lex.whitespace_split = False
    tokens: list[str] = []
    for tok in lex:
        if not tok.strip():
            continue
        tokens.extend(_expand_punctuation_token(tok))
    return tokens


def _expand_punctuation_token(token: str) -> list[str]:
    if not token or any(ch not in "();|&<>{}" for ch in token):
        return [token]
    pieces: list[str] = []
    operators = (";;&", "&&", "||", "|&", ";;", ";&", "&>>", "<<<", "&>", ">>", "<<", "<>", ">|", ">&", "<&")
    i = 0
    while i < len(token):
        for op in operators:
            if token.startswith(op, i):
                pieces.append(op)
                i += len(op)
                break
        else:
            pieces.append(token[i])
            i += 1
    return pieces


def _segments_from_tokens(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _tokens_without_function_definitions(tokens: list[str]) -> list[str]:
    executable: list[str] = []
    i = 0
    while i < len(tokens):
        shape = _function_definition_shape(tokens, i)
        if shape is not None:
            i = shape[2]
            continue
        executable.append(tokens[i])
        i += 1
    return executable


def _source_without_function_definitions(source: str) -> str:
    stripped: list[str] = []
    i = 0
    quote: str | None = None
    command_start = True
    while i < len(source):
        ch = source[i]
        if quote == "single":
            stripped.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            stripped.append(ch)
            if ch == "\\" and i + 1 < len(source):
                i += 1
                stripped.append(source[i])
            elif ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(source):
            stripped.extend((ch, source[i + 1]))
            i += 2
            command_start = False
            continue
        if ch == "'":
            quote = "single"
            stripped.append(ch)
            command_start = False
            i += 1
            continue
        if ch == '"':
            quote = "double"
            stripped.append(ch)
            command_start = False
            i += 1
            continue
        if command_start:
            definition = _source_function_definition_at(source, i)
            if definition is not None:
                _name, _body, end = definition
                stripped.append(" ; ")
                i = end
                command_start = False
                continue
        boundary_end = _source_command_boundary_end(source, i)
        if boundary_end is not None:
            stripped.append(source[i:boundary_end])
            command_start = True
            i = boundary_end
            continue
        stripped.append(ch)
        if ch.isspace():
            i += 1
            continue
        command_start = False
        i += 1
    return "".join(stripped)


def _function_definitions(tokens: list[str]) -> list[tuple[str, list[str], int]]:
    definitions: list[tuple[str, list[str], int]] = []
    i = 0
    while i < len(tokens):
        shape = _function_definition_shape(tokens, i)
        if shape is None:
            i += 1
            continue
        name, opener_index, end = shape
        definitions.append((name, tokens[opener_index + 1 : max(opener_index + 1, end - 1)], end))
        i = end
    return definitions


def _function_definition_end(tokens: list[str], i: int) -> int | None:
    shape = _function_definition_shape(tokens, i)
    return None if shape is None else shape[2]


def _function_definition_shape(tokens: list[str], i: int) -> tuple[str, int, int] | None:
    if i + 3 < len(tokens) and tokens[i + 1 : i + 3] == ["(", ")"] and tokens[i + 3] in {"{", "("}:
        name = tokens[i]
        opener_index = i + 3
    elif (
        i + 4 < len(tokens)
        and tokens[i] == "function"
        and tokens[i + 2 : i + 4] == ["(", ")"]
        and tokens[i + 4] in {"{", "("}
    ):
        name = tokens[i + 1]
        opener_index = i + 4
    elif (
        i + 2 < len(tokens)
        and tokens[i] == "function"
        and tokens[i + 2] in {"{", "("}
        and not (i + 3 < len(tokens) and tokens[i + 2 : i + 4] == ["(", ")"])
    ):
        name = tokens[i + 1]
        opener_index = i + 2
    else:
        return None

    opener = tokens[opener_index]
    closer = "}" if opener == "{" else ")"
    if opener == "(":
        for j in range(opener_index + 1, len(tokens)):
            if tokens[j] == closer and (j + 1 == len(tokens) or tokens[j + 1] in _SHELL_SEPS):
                return (name, opener_index, j + 1)
        return (name, opener_index, len(tokens))

    depth = 1
    j = opener_index + 1
    while j < len(tokens) and depth:
        if tokens[j] == opener:
            depth += 1
        elif tokens[j] == closer:
            depth -= 1
        j += 1
    return (name, opener_index, j)


def _function_definition_text_end(source: str, i: int) -> int | None:
    definition = _source_function_definition_at(source, i)
    return None if definition is None else definition[2]


def _source_function_definition_at(source: str, i: int) -> tuple[str, str, int] | None:
    match = _FUNCTION_DEFINITION_RE.match(source, i)
    if not match:
        return None
    opener = match.group("opener") or match.group("function_opener")
    opener_start = match.end() - 1
    body, end = _extract_function_definition_body(source, opener_start, opener)
    if end == len(source) and (not source or source[end - 1] != ("}" if opener == "{" else ")")):
        return None
    return match.group("name") or match.group("function_name") or "", body, end


def _extract_function_definition_body(source: str, opener_start: int, opener: str) -> tuple[str, int]:
    if opener == "{":
        return _extract_balanced_shell_text(source, opener_start, "{", "}")

    depth = 1
    quote: str | None = None
    body_start = opener_start + 1
    i = body_start
    while i < len(source):
        ch = source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(source):
            i += 2
            continue
        if ch == "'":
            quote = "single"
            i += 1
            continue
        if ch == '"':
            quote = "double"
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            if depth > 1:
                depth -= 1
                i += 1
                continue
            j = i + 1
            while j < len(source) and source[j].isspace():
                j += 1
            if j == len(source) or source.startswith(("&&", "||"), j) or source[j] in ";|&\n":
                return source[body_start:i], i + 1
        i += 1
    return source[body_start:], len(source)


def _segment_shell_c_contains_gh_pr_merge(segment: list[str], depth: int) -> bool | None:
    script = _shell_c_script(segment)
    if script is None:
        return False
    result = _command_contains_gh_pr_merge(script, _depth=depth + 1)
    if result is not False:
        return result
    rendered = _render_static_command_substitutions(script, depth)
    if rendered != script:
        return _command_contains_gh_pr_merge(rendered, _depth=depth + 1)
    return False


def _segment_here_string_contains_gh_pr_merge(segment: list[str], depth: int) -> bool | None:
    for i, tok in enumerate(segment):
        if tok != "<<<":
            continue
        if not _shell_reads_stdin(_tokens_without_redirections(segment[:i])):
            continue
        if i + 1 >= len(segment):
            return False
        return _command_contains_gh_pr_merge(segment[i + 1], _depth=depth + 1)
    return False


def _segment_eval_contains_gh_pr_merge(segment: list[str], depth: int) -> bool | None:
    script = _eval_script_from_segment(segment)
    if script is None:
        return False
    result = _command_contains_gh_pr_merge(script, _depth=depth + 1)
    if result is not False:
        return result
    rendered = _render_static_command_substitutions(script, depth)
    if rendered != script:
        return _command_contains_gh_pr_merge(rendered, _depth=depth + 1)
    return False


def _segment_trap_contains_gh_pr_merge(segment: list[str], depth: int) -> bool | None:
    argv = _unwrap_command_argv(_tokens_without_redirections(segment))
    if not argv or argv[0] != "trap":
        return False
    i = 1
    while i < len(argv) and argv[i] == "--":
        i += 1
    if i >= len(argv) or argv[i] in {"", "-", "-p", "-l"}:
        return False
    if argv[i] == "`":
        try:
            end = argv.index("`", i + 1)
        except ValueError:
            return False
        script = "`" + shlex.join(argv[i + 1 : end]) + "`"
    else:
        script = argv[i]
    result = _command_contains_gh_pr_merge(script, _depth=depth + 1)
    if result is not False:
        return result
    rendered = _render_static_command_substitutions(script, depth)
    if rendered != script:
        return _command_contains_gh_pr_merge(rendered, _depth=depth + 1)
    return False


def _eval_script_from_segment(segment: list[str]) -> str | None:
    argv = _unwrap_command_argv(segment)
    if argv and argv[0] == "builtin" and len(argv) > 1:
        argv = argv[1:]
    if not argv or argv[0] != "eval":
        return None
    return " ".join(argv[1:])


def _render_static_command_substitutions(source: str, depth: int) -> str:
    rendered: list[str] = []
    i = 0
    changed = False
    while i < len(source):
        if source.startswith("$(", i) and not source.startswith("$((", i):
            body, end = _extract_balanced_shell_text(source, i, "$(", ")")
            script = _static_script_from_shell_source(body, depth + 1)
            if script is not None:
                rendered.append(script)
                changed = True
                i = end
                continue
        if source[i] == "`":
            end = source.find("`", i + 1)
            body = source[i + 1 :] if end == -1 else source[i + 1 : end]
            script = _static_script_from_shell_source(body, depth + 1)
            if script is not None:
                rendered.append(script)
                changed = True
                i = len(source) if end == -1 else end + 1
                continue
        rendered.append(source[i])
        i += 1
    return "".join(rendered) if changed else source


def _static_script_from_shell_source(source: str, depth: int) -> str | None:
    heredoc_output = _static_cat_heredoc_output(source)
    if heredoc_output is not None:
        return heredoc_output
    try:
        prepared = _command_source_for_detection(source, _depth=depth + 1)
        segments = _split_segments(prepared, prepared=True)
    except Exception:
        return None
    if len(segments) != 1:
        return None
    return _static_pipeline_script_from_component(segments[0])


def _static_cat_heredoc_output(source: str) -> str | None:
    lines = source.splitlines(keepends=True)
    if not lines:
        return None
    first = lines[0][:-1] if lines[0].endswith("\n") else lines[0]
    code_line, quote = _strip_line_comment(_normalize_dollar_quotes(first), None)
    if quote is not None:
        return None
    heredocs = _heredocs_from_code_line(code_line)
    if not heredocs:
        return None
    try:
        tokens = _source_tokens(code_line)
    except ValueError:
        return None
    if any(tok in _SHELL_SEPS for tok in tokens):
        return None
    argv = _unwrap_command_argv(_tokens_without_redirections(tokens))
    if not argv or os.path.basename(argv[0]) != "cat":
        return None

    pending = list(heredocs)
    bodies = [[] for _ in pending]
    for raw_line in lines[1:]:
        if not pending:
            break
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        candidate = line.lstrip("\t") if pending[0].strip_tabs else line
        if candidate == pending[0].delimiter:
            pending.pop(0)
            continue
        bodies[len(heredocs) - len(pending)].append(raw_line)
    return "".join(part for body in bodies for part in body)


def _segment_process_substitution_feeds_shell_contains_gh_pr_merge(segment: list[str], depth: int) -> bool | None:
    for i, tok in enumerate(segment):
        if tok != "<(":
            continue
        end = _matching_token(segment, i, "<(", ")")
        if end is None:
            return False
        if not _process_substitution_feeds_executable_shell(segment, i, depth):
            continue
        script = _static_pipeline_script_from_component(segment[i + 1 : end])
        if script is None:
            continue
        result = _command_contains_gh_pr_merge(script, _depth=depth + 1)
        if result is not False:
            return result
    return False


def _raw_process_substitution_feeds_shell_contains_gh_pr_merge(source: str, depth: int) -> bool | None:
    i = 0
    while i < len(source):
        start = source.find("<(", i)
        if start == -1:
            return False
        body, end = _extract_balanced_shell_text(source, start, "<(", ")")
        try:
            span_start, span_end = _command_span_around_index(source, start)
            prefix_tokens = _source_tokens(_normalize_dollar_quotes(source[span_start:start]))
            suffix_tokens = _source_tokens(_normalize_dollar_quotes(source[end:span_end]))
        except ValueError:
            i = end
            continue
        segment_tokens = [*prefix_tokens, "<(", *suffix_tokens]
        if _process_substitution_feeds_executable_shell(segment_tokens, len(prefix_tokens), depth):
            script = _static_script_from_shell_source(body, depth + 1)
            if script is not None:
                result = _command_contains_gh_pr_merge(script, _depth=depth + 1)
                if result is not False:
                    return result
        i = end
    return False


def _oversize_parseable_result(command_source: str) -> bool | None:
    try:
        segments = _split_segments(command_source, prepared=True)
    except ValueError:
        return None
    for segment in segments:
        if _is_gh_pr_merge(segment):
            return True
    for segment in segments:
        clean = _tokens_without_redirections(segment)
        argv = _unwrap_command_argv(clean)
        if _shell_c_script(argv) is not None and _has_merge_hint(" ".join(argv[1:]), max_gap=None):
            return None
        if argv and argv[0] in {"eval", "trap", "xargs"} and _has_merge_hint(" ".join(argv[1:]), max_gap=None):
            return None
        if any(tok in {"$", "`", "<(", ">("} for tok in segment) and _has_merge_hint(shlex.join(segment), max_gap=None):
            return None
    try:
        if _source_has_called_function_with_gh_pr_merge(command_source, 0):
            return True
    except Exception:
        if _has_merge_hint(command_source, max_gap=None):
            return None
    return False


def _oversize_executable_shell_merge_hint(raw_command: str, command_source: str, depth: int) -> bool:
    try:
        raw_process_subst = _raw_process_substitution_feeds_shell_contains_gh_pr_merge(command_source, depth)
        if raw_process_subst is not False:
            return True
    except Exception:
        if _has_merge_hint(command_source, max_gap=None):
            return True
    try:
        executable_source = _source_without_function_definitions(command_source)
        piped_shell = _source_pipeline_feeds_shell_with_gh_pr_merge(executable_source, depth)
        if piped_shell is not False:
            return True
    except Exception:
        if _has_merge_hint(command_source, max_gap=None):
            return True
    return _raw_executable_heredoc_body_has_merge_hint(raw_command, depth)


def _raw_executable_heredoc_body_has_merge_hint(command: str, depth: int) -> bool:
    lines = command.splitlines(keepends=True)
    quote: str | None = None
    line_index = 0
    while line_index < len(lines):
        raw_line = lines[line_index]
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        quote_in = quote
        code_line, quote = _strip_line_comment(_normalize_dollar_quotes(line), quote)
        if quote is not None:
            line_index += 1
            continue
        heredocs = _heredocs_from_code_line(code_line, quote_in, depth)
        if any(heredoc.inspect_body for heredoc in heredocs) and _raw_heredoc_bodies_have_merge_hint(
            lines[line_index + 1 :],
            heredocs,
        ):
            return True
        line_index = _line_index_after_heredoc_bodies(lines, line_index + 1, heredocs)
    return False


def _raw_heredoc_bodies_have_merge_hint(lines: list[str], heredocs: list[_HereDoc]) -> bool:
    pending = list(heredocs)
    bodies = [[] for _ in pending]
    for raw_line in lines:
        if not pending:
            break
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        candidate = line.lstrip("\t") if pending[0].strip_tabs else line
        if candidate == pending[0].delimiter:
            pending.pop(0)
            continue
        bodies[len(heredocs) - len(pending)].append(raw_line)
    return any(
        heredoc.inspect_body and _has_merge_hint("".join(body), max_gap=None)
        for heredoc, body in zip(heredocs, bodies)
    )


def _process_substitution_feeds_executable_shell(segment: list[str], subst_index: int, depth: int) -> bool:
    prefix = segment[:subst_index]
    if prefix and prefix[-1] == "<":
        if _has_later_stdin_redirection(segment, subst_index):
            return False
        clean_prefix = _tokens_without_redirections(prefix[:-1])
        return _shell_reads_stdin(clean_prefix) or _shell_c_script_reads_stdin_as_shell(clean_prefix, depth)
    argv = _unwrap_command_argv(_tokens_without_redirections(prefix))
    if argv and argv[0] in {"source", "."}:
        return True
    return _shell_uses_next_arg_as_script(argv)


def _source_command_reads_stdin_path(argv: list[str]) -> bool:
    exec_argv = _unwrap_command_argv(argv)
    return bool(
        exec_argv
        and exec_argv[0] in {"source", "."}
        and any(arg in _STDIN_SHELL_SCRIPT_PATHS for arg in exec_argv[1:])
    )


def _shell_uses_next_arg_as_script(argv: list[str]) -> bool:
    args = _shell_args(argv)
    if args is None:
        return False
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return i + 1 == len(args)
        if _is_shell_c_option(tok) or _is_shell_stdin_option(tok):
            return False
        if not tok.startswith("-"):
            return False
        i = _next_shell_option_index(args, i)
    return True


def _command_contains_gh_pr_merge(command: str, *, _depth: int = 0) -> bool | None:
    budget = _DETECTION_BUDGET.get()
    token: contextvars.Token[list[int] | None] | None = None
    if budget is None:
        budget = [_DETECTION_NODE_BUDGET]
        token = _DETECTION_BUDGET.set(budget)
    try:
        budget[0] -= 1
        if budget[0] < 0:
            return None if _fail_closed_merge_hint(command) else False
        return _command_contains_gh_pr_merge_impl(command, _depth=_depth)
    finally:
        if token is not None:
            _DETECTION_BUDGET.reset(token)


def _fail_closed_merge_hint(command: str) -> bool:
    if _has_merge_hint(command, max_gap=None):
        return True
    try:
        source = _command_source_for_detection(command)
    except Exception:
        source = _normalize_dollar_quotes(command)
    return _has_merge_hint(source, max_gap=None)


def _command_contains_gh_pr_merge_impl(command: str, *, _depth: int = 0) -> bool | None:
    """Return True if any parsed segment of `command` is a `gh pr merge` call.

    Returns None (fail-closed) when the command cannot be parsed AND the raw
    text looks like a merge invocation.  Commands whose parse fails but contain
    no merge-like pattern (e.g. ``grep won't file`` with an unbalanced quote)
    return False so they are not spuriously blocked.
    """
    if _depth > _MAX_DETECTION_DEPTH:
        return None if _fail_closed_merge_hint(command) else False
    if len(command) > _MAX_COMMAND_CHARS:
        try:
            reduced_source = _command_source_for_detection(command, allow_oversize=True)
        except Exception:
            reduced_source = command
        if len(reduced_source) <= _MAX_COMMAND_CHARS and len(reduced_source) < len(command):
            return _command_contains_gh_pr_merge(reduced_source, _depth=_depth + 1)
        if _oversize_executable_shell_merge_hint(command, reduced_source, _depth):
            return None
        parseable = _oversize_parseable_result(reduced_source)
        if parseable is not None:
            return parseable
        return None if _has_merge_hint(reduced_source, max_gap=None) else False
    try:
        raw_nested_source = _source_for_raw_nested_scan(command)
        command_source = _command_source_for_detection(command)
        raw_process_subst = _raw_process_substitution_feeds_shell_contains_gh_pr_merge(raw_nested_source, _depth)
        if raw_process_subst is not False:
            return raw_process_subst
        raw_nested = _source_contains_nested_gh_pr_merge(raw_nested_source, _depth)
        if raw_nested is not False:
            return raw_nested
        if _source_has_called_function_with_gh_pr_merge(command_source, _depth):
            return True
        nested = _source_contains_nested_gh_pr_merge(command_source, _depth)
        if nested is not False:
            return nested
        case_arm = _source_has_case_arm_gh_pr_merge(command_source, _depth)
        if case_arm is not False:
            return case_arm
        executable_source = _source_without_function_definitions(command_source)
        piped_shell = _source_pipeline_feeds_shell_with_gh_pr_merge(executable_source, _depth)
        if piped_shell is not False:
            return piped_shell
        segments = _split_segments(executable_source, prepared=True)
        for seg in segments:
            if _is_gh_pr_merge(seg):
                return True
            case_arm = _has_case_arm_gh_pr_merge(seg, _depth)
            if case_arm is not False:
                return case_arm
            shell_c = _segment_shell_c_contains_gh_pr_merge(seg, _depth)
            if shell_c is not False:
                return shell_c
            here_string = _segment_here_string_contains_gh_pr_merge(seg, _depth)
            if here_string is not False:
                return here_string
            process_subst = _segment_process_substitution_feeds_shell_contains_gh_pr_merge(seg, _depth)
            if process_subst is not False:
                return process_subst
            eval_result = _segment_eval_contains_gh_pr_merge(seg, _depth)
            if eval_result is not False:
                return eval_result
            trap_result = _segment_trap_contains_gh_pr_merge(seg, _depth)
            if trap_result is not False:
                return trap_result
    except ValueError:
        if _fail_closed_merge_hint(command):
            return None
        return False
    except Exception as exc:
        warn(f"detector raised {type(exc).__name__}: {exc}")
        return None if _fail_closed_merge_hint(command) else False
    return False


def _block(prefix: str | None = None) -> int:
    body = (
        "Refusing a raw `gh pr merge` (incl. --admin): it bypasses the ship gates "
        "(green CI + required screenshots). Use `gh ship <PR>` instead — it merges only "
        "once CI is green and the mandatory screenshots are present. There is NO self-service "
        "override any more (`ALLOW_RAW_PR_MERGE` / `# no-ship-guard:` are gone). For a genuine "
        "one-time exception, ASK Alex, or request a single approval via "
        'RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE="<why>" — that routes a Telegram approval request '
        "to Alex (deny-by-default; a bare `1` is rejected)."
    )
    emit("block", f"{prefix}\n{body}" if prefix else body)
    return BLOCK_EXIT_CODE


def _hatch_env_for_command(command: str) -> dict[str, str] | None:
    env_var = _hatch_env_var()
    try:
        command_source = _command_source_for_detection(command)
    except ValueError:
        command_source = ""
    try:
        merge_source_count = _hatch_merge_source_count(command_source)
    except Exception:
        merge_source_count = 2
    if env_var in os.environ:
        if merge_source_count != 1:
            return None
        return {**os.environ}
    inline = _parse_inline_hatch_value(env_var, command_source)
    if inline is None:
        inline = _parse_inline_hatch_value_from_raw_heredoc(env_var, command)
    if inline is None:
        return None
    if merge_source_count != 1:
        return None
    return {**os.environ, env_var: inline}


def _hatch_env_var() -> str:
    if hasattr(hatch_escalation, "hatch_env_var"):
        try:
            return hatch_escalation.hatch_env_var("block-raw-pr-merge")
        except Exception:
            pass
    return "RIG_HATCH_REQUEST_BLOCK_RAW_PR_MERGE"


def _request_hatch_approval(
    hatch_context: dict[str, str],
    *,
    cwd: str,
    hatch_env: dict[str, str] | None,
    env_var: str,
    command: str,
):
    kwargs = {"cwd": cwd, "command": command}
    try:
        supports_env = "env" in inspect.signature(hatch_escalation.request_hatch_approval).parameters
    except Exception:
        supports_env = False
    if supports_env and hatch_env is not None:
        kwargs["env"] = hatch_env
    elif hatch_env is not None and env_var not in os.environ and env_var in hatch_env:
        original = os.environ.get(env_var)
        os.environ[env_var] = hatch_env[env_var]
        try:
            return hatch_escalation.request_hatch_approval(
                "block-raw-pr-merge",
                hatch_context,
                **kwargs,
            )
        finally:
            if original is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = original
    return hatch_escalation.request_hatch_approval(
        "block-raw-pr-merge",
        hatch_context,
        **kwargs,
    )


def _parse_inline_hatch_value(env_var: str, command_source: str) -> str | None:
    """Find an inline hatch attached to the executable raw-merge source.

    This hook can identify the exact executable segment/body that triggered
    `block-raw-pr-merge`, so inline approval is scoped to that source while the raw
    command is still passed unchanged to hatch approval for display and audit.
    """
    try:
        segments = _split_segments(command_source, prepared=True)
    except ValueError:
        return None
    inline: str | None = None
    for segment in segments:
        if not _segment_contains_gh_pr_merge_for_hatch(segment):
            continue
        segment_inline = _leading_inline_assignment_value(env_var, segment)
        if segment_inline is None:
            return None
        inline = segment_inline
    if inline is not None:
        if _source_pipeline_feeds_shell_with_gh_pr_merge(command_source, 0) is not False:
            return None
        if _source_has_called_function_with_gh_pr_merge(command_source, 0):
            return None
        if _source_has_case_arm_gh_pr_merge(command_source, 0) is not False:
            return None
        return inline
    inline = _source_inline_hatch_value_for_case_arm(env_var, command_source)
    if inline is not None:
        return inline
    inline = _source_inline_hatch_value_for_pipeline(env_var, command_source)
    if inline is not None:
        return inline
    inline = _source_inline_hatch_value_for_process_substitution(env_var, command_source)
    if inline is not None:
        return inline
    inline = _source_inline_hatch_value_for_quoted_nested(env_var, command_source)
    if inline is not None:
        return inline
    inline = _source_inline_hatch_value_for_called_function(env_var, command_source)
    if inline is not None:
        return inline
    return None


def _hatch_merge_source_count(command_source: str) -> int:
    count = 0
    executable_source = _source_without_function_definitions(command_source)
    try:
        for segment in _split_segments(executable_source, prepared=True):
            count += _segment_merge_source_count_for_hatch(segment, remaining=2 - count)
            if count > 1:
                return count
        tokens = _source_tokens(executable_source)
    except ValueError:
        return 2

    if _raw_process_substitution_feeds_shell_contains_gh_pr_merge(command_source, 0) is not False:
        count += 1
        if count > 1:
            return count

    count += _quoted_nested_merge_source_count_for_hatch(command_source, remaining=2 - count)
    if count > 1:
        return count

    for pipeline_tokens in _pipeline_groups_from_tokens(tokens):
        if _pipeline_tokens_feed_shell_with_gh_pr_merge(pipeline_tokens, 0) is not False:
            count += 1
            if count > 1:
                return count

    count += _source_case_arm_merge_count_for_hatch(command_source, remaining=2 - count)
    if count > 1:
        return count

    count += _source_called_function_merge_count_for_hatch(command_source, remaining=2 - count)
    return count


def _source_case_arm_merge_count_for_hatch(command_source: str, *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    try:
        tokens = _source_tokens(_source_without_function_definitions(command_source))
    except ValueError:
        return remaining
    count = 0
    case_patterns: list[bool] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "case":
            case_patterns.append(False)
            i += 1
            continue
        if tok == "esac":
            if case_patterns:
                case_patterns.pop()
            i += 1
            continue
        if not case_patterns:
            i += 1
            continue
        if tok == "in":
            case_patterns[-1] = True
            i += 1
            continue
        if case_patterns[-1] and tok == ")" and _case_arm_contains_gh_pr_merge_for_hatch(tokens[i + 1 :]):
            count += 1
            if count >= remaining:
                return count
        i += 1
    return count


def _case_arm_contains_gh_pr_merge_for_hatch(tokens: list[str]) -> bool:
    arm: list[str] = []
    nested_case_depth = 0
    for tok in tokens:
        if tok == "case":
            nested_case_depth += 1
        elif tok == "esac":
            if nested_case_depth == 0:
                break
            nested_case_depth -= 1
        if nested_case_depth == 0 and tok in {";;", ";&", ";;&"}:
            break
        arm.append(tok)
    return any(_segment_merge_source_count_for_hatch(segment, remaining=1) for segment in _segments_from_tokens(arm))


def _source_called_function_merge_count_for_hatch(command_source: str, *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    current_functions: dict[str, str] = {}
    chunk_start = 0
    i = 0
    quote: str | None = None
    command_start = True
    count = 0

    while i < len(command_source):
        ch = command_source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(command_source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command_source):
            i += 2
            command_start = False
            continue
        if ch == "'":
            quote = "single"
            command_start = False
            i += 1
            continue
        if ch == '"':
            quote = "double"
            command_start = False
            i += 1
            continue

        if command_start:
            definition = _source_function_definition_at(command_source, i)
            if definition is not None:
                count += _source_chunk_tainted_function_call_count(
                    command_source[chunk_start:i],
                    current_functions,
                    remaining=remaining - count,
                )
                if count >= remaining:
                    return count
                name, body, end = definition
                current_functions[name] = body
                i = end
                chunk_start = end
                command_start = False
                continue

        if ch.isspace():
            i += 1
            continue
        boundary_end = _source_command_boundary_end(command_source, i)
        if boundary_end is not None:
            command_start = True
            i = boundary_end
            continue
        command_start = False
        i += 1

    count += _source_chunk_tainted_function_call_count(
        command_source[chunk_start:],
        current_functions,
        remaining=remaining - count,
    )
    return count


def _source_chunk_tainted_function_call_count(
    source: str,
    functions: dict[str, str],
    *,
    remaining: int,
) -> int:
    if remaining <= 0 or not source.strip() or not functions:
        return 0
    try:
        tokens = _source_tokens(_source_without_function_definitions(source))
    except ValueError:
        return remaining
    count = 0
    for segment in _segments_from_tokens(tokens):
        for name, body in list(functions.items()):
            if not _tokens_call_function(segment, name):
                continue
            if not _function_body_source_contains_gh_pr_merge(name, body, functions, 0, frozenset()):
                continue
            count += min(
                remaining - count,
                max(1, _command_string_merge_source_count_for_hatch(body, remaining=remaining - count)),
            )
            if count >= remaining:
                return count
            break
    return count


def _segment_merge_source_count_for_hatch(segment: list[str], *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    count = 0
    if _is_gh_pr_merge(segment):
        count += 1
        if count >= remaining:
            return count
    count += _segment_command_substitution_merge_count_for_hatch(segment, remaining=remaining - count)
    if count >= remaining:
        return count

    for script in _segment_executable_shell_texts_for_hatch(segment):
        count += _command_string_merge_source_count_for_hatch(script, remaining=remaining - count)
        if count >= remaining:
            return count
    return count


def _segment_command_substitution_merge_count_for_hatch(segment: list[str], *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    count = 0
    i = 0
    while i < len(segment):
        tok = segment[i]
        if tok == "$" and i + 1 < len(segment) and segment[i + 1] == "(":
            end = _matching_token(segment, i + 1, "(", ")")
            if end is None:
                return count
            count += _command_string_merge_source_count_for_hatch(
                shlex.join(segment[i + 2 : end]),
                remaining=remaining - count,
            )
            if count >= remaining:
                return count
            i = end + 1
            continue
        if tok == "`":
            try:
                end = segment.index("`", i + 1)
            except ValueError:
                return count
            count += _command_string_merge_source_count_for_hatch(
                shlex.join(segment[i + 1 : end]),
                remaining=remaining - count,
            )
            if count >= remaining:
                return count
            i = end + 1
            continue
        if tok in {"<", ">"} and i + 1 < len(segment) and segment[i + 1] == "(":
            end = _matching_token(segment, i + 1, "(", ")")
            if end is None:
                return count
            count += _command_string_merge_source_count_for_hatch(
                shlex.join(segment[i + 2 : end]),
                remaining=remaining - count,
            )
            if count >= remaining:
                return count
            i = end + 1
            continue
        i += 1
    return count


def _segment_executable_shell_texts_for_hatch(segment: list[str]) -> list[str]:
    scripts: list[str] = []
    shell_c = _shell_c_script(segment)
    if shell_c is not None:
        scripts.append(shell_c)
    eval_script = _eval_script_from_segment(segment)
    if eval_script is not None:
        scripts.append(eval_script)
    trap_script = _trap_script_from_segment(segment)
    if trap_script is not None:
        scripts.append(trap_script)
    for i, tok in enumerate(segment):
        if tok != "<<<":
            continue
        if _shell_reads_stdin(_tokens_without_redirections(segment[:i])) and i + 1 < len(segment):
            scripts.append(segment[i + 1])
    return scripts


def _trap_script_from_segment(segment: list[str]) -> str | None:
    argv = _unwrap_command_argv(_tokens_without_redirections(segment))
    if not argv or argv[0] != "trap":
        return None
    i = 1
    while i < len(argv) and argv[i] == "--":
        i += 1
    if i >= len(argv) or argv[i] in {"", "-", "-p", "-l"}:
        return None
    if argv[i] == "`":
        try:
            end = argv.index("`", i + 1)
        except ValueError:
            return None
        return "`" + shlex.join(argv[i + 1 : end]) + "`"
    return argv[i]


def _command_string_merge_source_count_for_hatch(command: str, *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    try:
        source = _command_source_for_detection(command)
    except Exception:
        return remaining if _has_merge_hint(command, max_gap=None) else 0
    return min(remaining, _hatch_merge_source_count(source))


def _source_inline_hatch_value_for_case_arm(env_var: str, command_source: str) -> str | None:
    try:
        tokens = _source_tokens(_source_without_function_definitions(command_source))
    except ValueError:
        return None
    case_patterns: list[bool] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "case":
            case_patterns.append(False)
            i += 1
            continue
        if tok == "esac":
            if case_patterns:
                case_patterns.pop()
            i += 1
            continue
        if not case_patterns:
            i += 1
            continue
        if tok == "in":
            case_patterns[-1] = True
            i += 1
            continue
        if case_patterns[-1] and tok == ")":
            value = _case_arm_inline_hatch_value(env_var, tokens[i + 1 :])
            if value is not None:
                return value
        i += 1
    return None


def _case_arm_inline_hatch_value(env_var: str, tokens: list[str]) -> str | None:
    arm: list[str] = []
    nested_case_depth = 0
    for tok in tokens:
        if tok == "case":
            nested_case_depth += 1
        elif tok == "esac":
            if nested_case_depth == 0:
                break
            nested_case_depth -= 1
        if nested_case_depth == 0 and tok in {";;", ";&", ";;&"}:
            break
        arm.append(tok)
    for segment in _segments_from_tokens(arm):
        if _segment_contains_gh_pr_merge_for_hatch(segment):
            return _leading_inline_assignment_value(env_var, segment)
    return None


def _source_inline_hatch_value_for_pipeline(env_var: str, command_source: str) -> str | None:
    try:
        tokens = _source_tokens(command_source)
    except ValueError:
        return None
    for pipeline_tokens in _pipeline_groups_from_tokens(tokens):
        value = _leading_inline_assignment_value(env_var, pipeline_tokens)
        if value is None:
            continue
        if _pipeline_tokens_feed_shell_with_gh_pr_merge(pipeline_tokens, 0) is not False:
            return value
    return None


def _source_inline_hatch_value_for_process_substitution(env_var: str, command_source: str) -> str | None:
    i = 0
    while i < len(command_source):
        start = command_source.find("<(", i)
        if start == -1:
            return None
        body, end = _extract_balanced_shell_text(command_source, start, "<(", ")")
        try:
            span_start, span_end = _command_span_around_index(command_source, start)
            prefix_tokens = _source_tokens(_normalize_dollar_quotes(command_source[span_start:start]))
            suffix_tokens = _source_tokens(_normalize_dollar_quotes(command_source[end:span_end]))
        except ValueError:
            i = end
            continue
        segment_tokens = [*prefix_tokens, "<(", *suffix_tokens]
        value = _leading_inline_assignment_value(env_var, prefix_tokens)
        if value is not None and _process_substitution_feeds_executable_shell(segment_tokens, len(prefix_tokens), 0):
            script = _static_script_from_shell_source(body, 1)
            if script is not None and _command_contains_gh_pr_merge(script, _depth=1) is not False:
                return value
        i = end
    return None


def _source_inline_hatch_value_for_quoted_nested(env_var: str, command_source: str) -> str | None:
    for start, body, _end in _quoted_nested_shell_bodies(command_source):
        if _command_string_merge_source_count_for_hatch(body, remaining=1) == 0:
            continue
        try:
            span_start, span_end = _command_span_around_index(command_source, start)
            segment_tokens = _source_tokens(_normalize_dollar_quotes(command_source[span_start:span_end]))
        except ValueError:
            continue
        value = _leading_inline_assignment_value(env_var, segment_tokens)
        if value is not None:
            return value
    return None


def _quoted_nested_merge_source_count_for_hatch(command_source: str, *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    count = 0
    for _start, body, _end in _quoted_nested_shell_bodies(command_source):
        count += _command_string_merge_source_count_for_hatch(body, remaining=remaining - count)
        if count >= remaining:
            return count
    return count


def _quoted_nested_shell_bodies(command_source: str) -> list[tuple[int, str, int]]:
    bodies: list[tuple[int, str, int]] = []
    quote: str | None = None
    i = 0
    while i < len(command_source):
        ch = command_source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(command_source):
                i += 2
                continue
            if command_source.startswith("$(", i) and not command_source.startswith("$((", i):
                body, end = _extract_balanced_shell_text(command_source, i, "$(", ")")
                bodies.append((i, body, end))
                i = end
                continue
            if ch == "`":
                end = _backtick_span_end(command_source, i)
                bodies.append((i, command_source[i + 1 : max(i + 1, end - 1)], end))
                i = end
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command_source):
            i += 2
            continue
        if ch == "'":
            quote = "single"
        elif ch == '"':
            quote = "double"
        i += 1
    return bodies


def _source_inline_hatch_value_for_called_function(env_var: str, command_source: str) -> str | None:
    current_functions: dict[str, str] = {}
    chunk_start = 0
    i = 0
    quote: str | None = None
    command_start = True

    while i < len(command_source):
        ch = command_source[i]
        if quote == "single":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == "double":
            if ch == "\\" and i + 1 < len(command_source):
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command_source):
            i += 2
            command_start = False
            continue
        if ch == "'":
            quote = "single"
            command_start = False
            i += 1
            continue
        if ch == '"':
            quote = "double"
            command_start = False
            i += 1
            continue

        if command_start:
            definition = _source_function_definition_at(command_source, i)
            if definition is not None:
                value = _source_chunk_inline_hatch_value_for_tainted_function(
                    env_var,
                    command_source[chunk_start:i],
                    current_functions,
                )
                if value is not None:
                    return value
                name, body, end = definition
                current_functions[name] = body
                i = end
                chunk_start = end
                command_start = False
                continue

        if ch.isspace():
            i += 1
            continue
        boundary_end = _source_command_boundary_end(command_source, i)
        if boundary_end is not None:
            command_start = True
            i = boundary_end
            continue
        command_start = False
        i += 1

    return _source_chunk_inline_hatch_value_for_tainted_function(
        env_var,
        command_source[chunk_start:],
        current_functions,
    )


def _source_chunk_inline_hatch_value_for_tainted_function(
    env_var: str,
    source: str,
    functions: dict[str, str],
) -> str | None:
    if not source.strip() or not functions:
        return None
    try:
        tokens = _source_tokens(_source_without_function_definitions(source))
    except ValueError:
        return None
    for segment in _segments_from_tokens(tokens):
        value = _leading_inline_assignment_value(env_var, segment)
        if value is None:
            continue
        for name, body in list(functions.items()):
            if _tokens_call_function(segment, name) and _function_body_source_contains_gh_pr_merge(
                name,
                body,
                functions,
                0,
                frozenset(),
            ):
                return value
    return None


def _parse_inline_hatch_value_from_raw_heredoc(env_var: str, command: str) -> str | None:
    lines = command.splitlines(keepends=True)
    quote: str | None = None
    line_index = 0
    while line_index < len(lines):
        raw_line = lines[line_index]
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        quote_in = quote
        code_line, quote = _strip_line_comment(_normalize_dollar_quotes(line), quote)
        if quote is not None:
            line_index += 1
            continue
        heredocs = _heredocs_from_code_line(code_line, quote_in)
        next_line_index = _line_index_after_heredoc_bodies(lines, line_index + 1, heredocs)
        if not any(heredoc.inspect_body for heredoc in heredocs):
            line_index = next_line_index
            continue
        try:
            segments = _split_segments(code_line, prepared=True)
        except ValueError:
            line_index = next_line_index
            continue
        heredoc_offset = 0
        for segment in segments:
            heredoc_count = _segment_heredoc_count(segment)
            heredoc_indexes = range(heredoc_offset, heredoc_offset + heredoc_count)
            heredoc_offset += heredoc_count
            if heredoc_count == 0:
                continue
            value = _leading_inline_assignment_value(env_var, segment)
            if value is not None and _raw_heredoc_bodies_contain_merge(
                lines[line_index + 1 :],
                heredocs,
                inspect_indexes=heredoc_indexes,
            ):
                return value
        line_index = next_line_index
    return None


def _line_index_after_heredoc_bodies(lines: list[str], start_index: int, heredocs: list[_HereDoc]) -> int:
    pending = list(heredocs)
    index = start_index
    while index < len(lines) and pending:
        line = lines[index][:-1] if lines[index].endswith("\n") else lines[index]
        candidate = line.lstrip("\t") if pending[0].strip_tabs else line
        if candidate == pending[0].delimiter:
            pending.pop(0)
        index += 1
    return index


def _segment_heredoc_count(segment: list[str]) -> int:
    return sum(
        1
        for tok in segment
        if (tok in {"<<", "<<-"} or tok.startswith(("<<", "<<-"))) and not tok.startswith("<<<")
    )


def _raw_heredoc_bodies_contain_merge(
    lines: list[str],
    heredocs: list[_HereDoc],
    *,
    inspect_indexes: range | None = None,
) -> bool:
    selected = set(inspect_indexes if inspect_indexes is not None else range(len(heredocs)))
    pending = list(heredocs)
    bodies = [[] for _ in pending]
    for raw_line in lines:
        if not pending:
            break
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        candidate = line.lstrip("\t") if pending[0].strip_tabs else line
        if candidate == pending[0].delimiter:
            pending.pop(0)
            continue
        bodies[len(heredocs) - len(pending)].append(raw_line)
    for index, (heredoc, body) in enumerate(zip(heredocs, bodies)):
        if (
            index in selected
            and heredoc.inspect_body
            and _command_contains_gh_pr_merge("".join(body)) is not False
        ):
            return True
    return False


def _leading_inline_assignment_value(env_var: str, segment: list[str]) -> str | None:
    i = 0
    while i < len(segment) and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    inline: str | None = None
    for token in segment[i:]:
        name, sep, value = token.partition("=")
        if not sep or not _INLINE_ENV.match(token):
            break
        if name == env_var:
            inline = value
    return inline


def _segment_contains_gh_pr_merge_for_hatch(segment: list[str]) -> bool:
    if _is_gh_pr_merge(segment):
        return True
    if _segment_command_substitution_contains_gh_pr_merge(segment):
        return True
    for detector in (
        _segment_shell_c_contains_gh_pr_merge,
        _segment_here_string_contains_gh_pr_merge,
        _segment_process_substitution_feeds_shell_contains_gh_pr_merge,
        _segment_eval_contains_gh_pr_merge,
        _segment_trap_contains_gh_pr_merge,
    ):
        result = detector(segment, 0)
        if result is not False:
            return True
    return False


def _segment_command_substitution_contains_gh_pr_merge(segment: list[str]) -> bool:
    i = 0
    while i < len(segment):
        tok = segment[i]
        if tok == "$" and i + 1 < len(segment) and segment[i + 1] == "(":
            end = _matching_token(segment, i + 1, "(", ")")
            if end is None:
                return False
            result = _command_contains_gh_pr_merge(shlex.join(segment[i + 2 : end]))
            if result is not False:
                return True
            i = end + 1
            continue
        if tok == "`":
            try:
                end = segment.index("`", i + 1)
            except ValueError:
                return False
            result = _command_contains_gh_pr_merge(shlex.join(segment[i + 1 : end]))
            if result is not False:
                return True
            i = end + 1
            continue
        i += 1
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-raw-pr-merge: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE
    if not isinstance(event, dict):
        warn("event payload was not an object — blocking (fail-closed)")
        emit("block", "block-raw-pr-merge: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    raw_args = event.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    try:
        cwd = str(event.get("cwd") or os.getcwd())
    except OSError:
        cwd = "."

    try:
        is_merge = _command_contains_gh_pr_merge(command)
    except Exception as exc:
        warn(f"detector raised {type(exc).__name__}: {exc}")
        is_merge = None if _fail_closed_merge_hint(command) else False

    if is_merge is None:
        warn("could not fully inspect command — blocking (fail-closed)")
        emit(
            "block",
            "block-raw-pr-merge: could not fully inspect the command; cannot verify "
            "it is not a raw merge, blocking (fail-closed).",
        )
        return BLOCK_EXIT_CODE

    if not is_merge:
        emit("allow")
        return 0

    # A raw merge is deny-by-default. The ONLY exception is an approved external Telegram hatch
    # (deny-by-default); an unset env falls straight through to the block.
    try:
        hatch_env = _hatch_env_for_command(command)
        env_var = _hatch_env_var()
        if hatch_env is not None:
            if hatch_env.get(env_var) == "":
                return _block(prefix=f"hatch escalation denied: {env_var} is blank")
            hatch_context = {"hook": "block-raw-pr-merge", "command": command}
            hatch = _request_hatch_approval(
                hatch_context,
                cwd=cwd,
                hatch_env=hatch_env,
                env_var=env_var,
                command=command,
            )
            if hatch.should_stop:
                if hatch.approved:
                    warn(f"raw `gh pr merge` allowed via hatch escalation ({hatch.reason})")
                    emit("allow", f"raw `gh pr merge` allowed via hatch escalation ({hatch.reason})")
                    return 0
                warn(f"raw `gh pr merge` hatch escalation denied: {hatch.reason}")
                return _block(prefix=f"hatch escalation denied: {hatch.reason}")
    except Exception as exc:
        warn(f"hatch escalation path raised {type(exc).__name__}: {exc} — blocking (fail-closed)")
        return _block(prefix="hatch escalation check failed (fail-closed)")

    return _block()


if __name__ == "__main__":
    sys.exit(main())
