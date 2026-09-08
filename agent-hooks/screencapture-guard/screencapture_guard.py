#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block macOS `screencapture` (retrospective G-6).

Accessed via: every harness Bash/tool-call that the cc/codex/opencode/omp hook
bridge maps to `pre-bash`. The outer permissions.deny glob (`Bash(screencapture:*)`)
is prefix-only; this hook is the argv-aware belt.

Assumptions: the event carries `args.command` as the shell string the agent asked
to run. Wrappers (`timeout`/`sudo`/`env`) are peeled; a command that only mentions
the word in a message is not a screencapture invocation.

Past bugs: G-6 in the 2026-07-01 agent-ecosystem retrospective — the rule lived as
prose + a permission glob, so wrappers and a path-qualified binary slipped through
and the block never named the Playwright/CDP recipe.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "screencapture-guard"

_SCREENCAPTURE = "screencapture"
_HINT = re.compile(r"\bscreencapture\b")

_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_LEADING_SHELL_NOISE = frozenset(
    {"(", "{", "!", "if", "then", "do", "else", "elif", "while", "until", "for", "case"}
)
_MAX_LEADING_NOISE = 16
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# SYNC: wrapper table copied from block-reset-hard (standalone hook, no shared import).
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer",
    "command", "sudo", "doas", "exec", "taskset", "chrt", "setpriv", "flock", "runuser",
})
_MAX_WRAPPER_NESTING = 16
_OPERAND_DROP_WRAPPERS = frozenset({"timeout", "taskset", "chrt", "flock"})
_MANDATORY_OPERAND_WRAPPERS = frozenset({"flock"})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "--user", "-g", "--group", "-p", "--prompt", "-r", "--role",
        "-t", "--type", "-C", "--close-from", "-R", "--chroot", "-D", "--chdir",
        "-T", "--command-timeout", "-U", "--other-user", "-c", "--login-class",
        "-a", "--auth-type",
    }),
    "doas": frozenset({"-u", "-a", "-C"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({
        "-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid",
    }),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"}),
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "exec": frozenset({"-a"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "taskset": frozenset({"-c", "--cpu-list"}),
    "flock": frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}),
    "setpriv": frozenset({
        "--ruid", "--euid", "--reuid", "--rgid", "--egid", "--regid", "--groups", "--ptracer",
        "--securebits", "--pdeathsig", "--ambient-caps", "--inh-caps", "--bounding-set",
        "--selinux-label", "--apparmor-profile", "--landlock-access", "--landlock-rule",
    }),
    "chrt": frozenset({"-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline"}),
    "runuser": frozenset({
        "-c", "--command", "--session-command", "-g", "--group", "-G", "--supp-group",
        "-s", "--shell", "-u", "--user", "-w", "--whitelist-environment",
    }),
}
if not _OPERAND_DROP_WRAPPERS <= _WRAPPERS:
    raise RuntimeError("operand-drop wrappers missing from _WRAPPERS")
if not _MANDATORY_OPERAND_WRAPPERS <= _OPERAND_DROP_WRAPPERS:
    raise RuntimeError("mandatory-operand wrappers not in operand-drop")
if not set(_WRAPPER_VALUE_FLAGS) <= _WRAPPERS:
    raise RuntimeError("value-flag wrappers missing from _WRAPPERS")


def _short_value_letters(value_flags: frozenset[str]) -> frozenset[str]:
    return frozenset(f[1] for f in value_flags if len(f) == 2 and f.startswith("-"))


_WRAPPER_SHORT_VALUE_LETTERS = {w: _short_value_letters(f) for w, f in _WRAPPER_VALUE_FLAGS.items()}

BLOCK_MESSAGE = (
    f"{HOOK_ID}: macOS screencapture is blocked. Screenshots go through Playwright or CDP "
    "(agent-browser screenshot, or the visual-proof-cycle skill) — screencapture black-frames "
    "windows on other Spaces and trips Screen Recording grants."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _shlex_tokens(line: str, *, posix: bool) -> list[str] | None:
    lex = shlex.shlex(line, posix=posix, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _tokenize_line(line: str) -> list[str] | None:
    posix_tokens = _shlex_tokens(line, posix=True)
    if posix_tokens is None:
        return None
    raw_tokens = _shlex_tokens(line, posix=False)
    if raw_tokens is None or len(raw_tokens) != len(posix_tokens):
        return None
    out: list[str] = []
    for raw, tok in zip(raw_tokens, posix_tokens):
        if raw.startswith("#"):
            break
        out.append(tok)
    return out


_REDIRECT_OPS = frozenset({">", ">>", "<", "<<", ">&", "&>", "<&", "<>"})
_FD_DIGITS_RE = re.compile(r"^\d+$")
_HEREDOC_OPEN = re.compile(r"<<[-]?(['\"]?)(\w+)\1")


def _drop_heredoc_bodies(lines: list[str]) -> list[str]:
    """Keep heredoc openers, drop body lines so a mention inside a here-doc is not a command."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC_OPEN.search(line)
        out.append(line)
        i += 1
        if not m:
            continue
        delim = m.group(2)
        while i < len(lines):
            body = lines[i]
            i += 1
            if body == delim or body.strip() == delim:
                break
    return out


def _strip_redirections(argv: list[str]) -> list[str]:
    """Remove redirection operator tokens, their targets, and a leading fd-digit prefix.

    SYNC: same shape as pkill-guard._strip_redirections. A leading `2>/dev/null cmd`
    tokenizes to ['2', '>', '/dev/null', 'cmd']; without this, argv[0] is '2'.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        if (
            i + 1 < len(argv)
            and argv[i + 1] in _REDIRECT_OPS
            and _FD_DIGITS_RE.match(argv[i])
        ):
            i += 1
            continue
        if argv[i] in _REDIRECT_OPS:
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out

def _tokenize(command: str) -> list[str] | None:
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")
    lines = _drop_heredoc_bodies(joined.split("\n"))
    out: list[str] = []
    first = True
    i = 0
    while i < len(lines):
        chunk = lines[i]
        toks = _tokenize_line(chunk)
        while toks is None and i + 1 < len(lines):
            i += 1
            chunk = f"{chunk}\n{lines[i]}"
            toks = _tokenize_line(chunk)
        if toks is None:
            return None
        if not first:
            out.append(";")
        first = False
        out.extend(toks)
        i += 1
    return out


def _split_segments(command: str) -> list[list[str]] | None:
    tokens = _tokenize(command)
    if tokens is None:
        return None
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


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_inline_env(argv: list[str]) -> list[str]:
    while argv and _INLINE_ENV.match(argv[0]):
        argv = argv[1:]
    return argv


def _cluster_takes_next_value(tok: str, short_letters: frozenset[str]) -> bool:
    body = tok[1:]
    if not body or body[-1] not in short_letters:
        return False
    for ch in body[:-1]:
        if ch in short_letters:
            return False
    return True


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    short_letters = _WRAPPER_SHORT_VALUE_LETTERS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        consumes_next = (
            argv[i] in value_flags
            or (not argv[i].startswith("--") and _cluster_takes_next_value(argv[i], short_letters))
        )
        if consumes_next and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        if wrapper in _MANDATORY_OPERAND_WRAPPERS or _basename(argv[i]) != _SCREENCAPTURE:
            i += 1
    return argv[i:]


class _WrapperOverflow(Exception):
    """Unresolvable wrapper chain — fail closed."""


def _strip_wrappers(argv: list[str]) -> list[str]:
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            raise _WrapperOverflow(f"wrapper chain exceeds {_MAX_WRAPPER_NESTING} levels")
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        argv = _strip_inline_env(argv)
    return argv


def _segment_argv(segment: list[str]) -> list[str]:
    argv = list(segment)
    for _ in range(_MAX_WRAPPER_NESTING + 1):
        before = argv
        argv = _strip_redirections(argv)
        argv = _strip_leading_shell_noise(argv)
        argv = _strip_inline_env(argv)
        argv = _strip_wrappers(argv)
        if argv == before:
            return argv
    return argv


def _is_screencapture(segment: list[str]) -> bool:
    try:
        argv = _segment_argv(segment)
    except _WrapperOverflow:
        return True
    return bool(argv) and _basename(argv[0]) == _SCREENCAPTURE


def _classify(command: str) -> str:
    """Return 'block' or 'allow'."""
    segments = _split_segments(command)
    if segments is None:
        return "block" if _HINT.search(command) else "allow"
    for segment in segments:
        if _is_screencapture(segment):
            return "block"
    return "allow"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        emit("block", f"{HOOK_ID}: malformed event")
        return BLOCK_EXIT_CODE
    if not isinstance(event, dict):
        emit("block", f"{HOOK_ID}: malformed event")
        return BLOCK_EXIT_CODE
    args = event.get("args") or {}
    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0
    if _classify(command) == "block":
        emit("block", BLOCK_MESSAGE)
        return BLOCK_EXIT_CODE
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
