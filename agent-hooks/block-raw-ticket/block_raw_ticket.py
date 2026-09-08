#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block raw gh issue / Linear when task-cli is configured.

Accessed via: harness PreToolUse bash -> this script (rig apply installs the
descriptor from the agent-tools catalog when agent_hooks.all is true).

Assumptions: task-cli is the enforced ticket interface. The gate arms only when
`task` on PATH is actually task-cli (the file contains `tasklib` — not
Taskwarrior/go-task). Then a raw `gh issue` write or `linear issue` write
skips the gates. Read-only `gh issue list/view` and `task …` stay allowed.

A "Review uncommitted …" title is refused on ticket create even when task-cli
is absent: that shape must never become a GitHub/Linear/task ticket (ask 041).
`gh pr` is not a ticket and is left alone.

Past bugs: require-ticket-before-commit explicitly allowed `gh issue create`
because it is not a git commit. shutil.which("task") alone matched a foreign
`task` binary (review on #636). main() early-allowed every command when
task-cli was absent, so `gh issue create --title "Review uncommitted …"` still
minted GitHub tickets.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "block-raw-ticket"

_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "then", "do", "else", "elif"})
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer", "command",
})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "nice": frozenset({"-n", "--adjustment"}),
}
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
_GH_ISSUE_WRITES = frozenset({
    "create", "new", "close", "reopen", "edit", "comment", "delete",
    "transfer", "pin", "unpin", "lock", "unlock", "develop",
})
_LINEAR_ISSUE_WRITES = frozenset({
    "create", "new", "update", "edit", "delete", "archive", "close", "comment",
})
_CREATE_VERBS = frozenset({"create", "new"})
_RAW_HINT = re.compile(r"\b(?:gh\s+issue|linear\s+issue)\b", re.IGNORECASE)
_JUNK_UNCOMMITTED = re.compile(r"\breview\s+uncommitted\b", re.IGNORECASE)
_TASK_CLI_MARKERS = (b"tasklib", b"task-cli")
_INHERITED_VALUE_FLAGS = frozenset({
    "-R", "--repo", "--hostname", "-C", "--cwd", "--backend", "--config",
})

_BLOCK_MSG = (
    "[block-raw-ticket] BLOCKED: raw tracker write while task-cli is configured. "
    "Use `task new` / `task create` / `task change` / `task done` instead of "
    "`gh issue` or `linear issue`. task-cli enforces acceptance criteria, "
    "motivation, user-impact, and screenshots; a raw tracker command skips them. "
    "Read-only `gh issue list`/`view` is allowed. There is no self-service bypass; "
    "ask the human for a genuine exception."
)

_JUNK_BLOCK_MSG = (
    "[block-raw-ticket] BLOCKED: a 'Review uncommitted' title must not create a "
    "ticket. That shape never leads to a merge — run `review diff` or post a PR "
    "comment instead of filing a GitHub/Linear/`task` ticket. There is no "
    "self-service bypass."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _looks_like_task_cli(path: str) -> bool:
    """True iff `path` is task-cli, not Taskwarrior/go-task (both also named `task`)."""
    try:
        with open(os.path.realpath(path), "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return any(m in head for m in _TASK_CLI_MARKERS)


def task_cli_configured(cwd: str | None = None) -> bool:
    """Activated AND configured: a real task-cli binary is on PATH."""
    del cwd
    path = shutil.which("task")
    return bool(path) and _looks_like_task_cli(path)


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _strip_leading(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and (argv[i] in _LEADING_SHELL_NOISE or _INLINE_ENV.match(argv[i])):
        i += 1
    return argv[i:]


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    i = 0
    while i < len(argv):
        tok = argv[i]
        if _INLINE_ENV.match(tok):
            i += 1
            continue
        if tok in value_flags:
            i += 2
            continue
        if any(tok.startswith(f"{flag}=") for flag in value_flags):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    rest = argv[i:]
    if wrapper == "timeout" and rest and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", rest[0]):
        rest = rest[1:]
    return rest


def _strip_wrappers(argv: list[str]) -> list[str]:
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS and guard < 16:
        guard += 1
        wrapper = _basename(argv[0])
        argv = _skip_wrapper_args(wrapper, argv[1:])
    return argv


def _skip_gh_flags(argv: list[str]) -> list[str]:
    """Skip gh inherited flags (`-R`/`--repo`/`--hostname`) and other dash options."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GH_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("--repo=") or tok.startswith("--hostname=") or tok.startswith("-R="):
            i += 1
            continue
        if tok.startswith("-") and tok not in {"-h", "--help"}:
            i += 1
            continue
        break
    return argv[i:]


def _segment_is_raw_write(segment: list[str]) -> bool:
    argv = _strip_wrappers(_strip_leading(segment))
    if not argv:
        return False
    head = _basename(argv[0])
    rest = argv[1:]
    if head == "gh":
        rest = _skip_gh_flags(rest)
        if not rest or rest[0] != "issue":
            return False
        sub = _skip_gh_flags(rest[1:])
        return bool(sub) and sub[0] in _GH_ISSUE_WRITES
    if head == "linear":
        rest = _skip_gh_flags(rest)  # linear also takes dash flags before the verb
        if not rest or rest[0] != "issue":
            return False
        sub = _skip_gh_flags(rest[1:])
        return bool(sub) and sub[0] in _LINEAR_ISSUE_WRITES
    return False


def _tokenize(command: str) -> list[str] | None:
    """punctuation_chars so `true;gh` / `true&&gh` split; newlines are command separators."""
    try:
        out: list[str] = []
        for i, line in enumerate(command.splitlines()):
            if not line.strip():
                continue
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            toks = list(lex)
            if i and out and toks:
                out.append(";")
            out.extend(toks)
        return out
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SHELL_SEPS:
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def is_raw_tracker_write(command: str) -> bool | None:
    """True if command is a raw gh/linear issue write; None if unparseable-and-plausible."""
    tokens = _tokenize(command)
    if tokens is None:
        return None if _RAW_HINT.search(command) else False
    return any(_segment_is_raw_write(seg) for seg in _segments(tokens))


def _extract_title_flag(argv: list[str]) -> str | None:
    """`--title VALUE`, `-t VALUE`, or `--title=VALUE` from a tokenized argv."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-t", "--title"):
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("--title="):
            return tok.split("=", 1)[1]
        i += 1
    return None


def _nonflag_words(argv: list[str]) -> list[str]:
    """Non-flag tokens after argv[0], skipping inherited value-flag operands."""
    words: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in _INHERITED_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-") and tok not in {"-", "--"}:
            i += 1
            continue
        words.append(tok)
        i += 1
    return words


def _segment_is_junk_ticket_create(segment: list[str]) -> bool:
    argv = _strip_wrappers(_strip_leading(segment))
    if not argv:
        return False
    title = _extract_title_flag(argv)
    if not title or not _JUNK_UNCOMMITTED.search(title):
        return False
    head = _basename(argv[0])
    words = _nonflag_words(argv)
    if head == "gh":
        return len(words) >= 2 and words[0] == "issue" and words[1] in _CREATE_VERBS
    if head == "linear":
        return len(words) >= 2 and words[0] == "issue" and words[1] in _CREATE_VERBS
    if head == "task":
        return bool(words) and words[0] in _CREATE_VERBS
    return False


def is_junk_review_ticket_create(command: str) -> bool | None:
    """True if a ticket-create command carries a Review-uncommitted title.

    None if the command is unparseable and still looks like that create.
    """
    tokens = _tokenize(command)
    if tokens is None:
        if _JUNK_UNCOMMITTED.search(command) and (
            _RAW_HINT.search(command) or re.search(r"\btask\s+(?:new|create)\b", command)
        ):
            return None
        return False
    return any(_segment_is_junk_ticket_create(seg) for seg in _segments(tokens))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", f"{HOOK_ID}: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    try:
        junk = is_junk_review_ticket_create(command)
    except Exception as exc:  # noqa: BLE001
        if _JUNK_UNCOMMITTED.search(command) and (
            _RAW_HINT.search(command) or re.search(r"\btask\s+(?:new|create)\b", command)
        ):
            warn(f"junk-title inspection failed ({exc}) — blocking")
            emit("block", _JUNK_BLOCK_MSG)
            return BLOCK_EXIT_CODE
        junk = False
    if junk is None:
        warn("unparseable command looks like a Review-uncommitted ticket create — blocking")
        emit("block", _JUNK_BLOCK_MSG)
        return BLOCK_EXIT_CODE
    if junk:
        emit("block", _JUNK_BLOCK_MSG)
        return BLOCK_EXIT_CODE

    if not task_cli_configured(cwd):
        emit("allow")
        return 0

    try:
        hit = is_raw_tracker_write(command)
    except Exception as exc:  # noqa: BLE001
        if _RAW_HINT.search(command):
            warn(f"inspection failed on a plausible raw tracker write ({exc}) — blocking")
            emit("block", _BLOCK_MSG)
            return BLOCK_EXIT_CODE
        warn(f"inspection failed ({exc}) — allowing")
        emit("allow")
        return 0

    if hit is None:
        warn("unparseable command looks like a raw tracker write — blocking (fail-closed)")
        emit("block", _BLOCK_MSG)
        return BLOCK_EXIT_CODE
    if hit:
        emit("block", _BLOCK_MSG)
        return BLOCK_EXIT_CODE
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
