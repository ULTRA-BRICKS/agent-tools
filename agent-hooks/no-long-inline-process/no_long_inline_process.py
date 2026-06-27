#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — keep long-running processes off the main thread.

A long-running process started inline by the ORCHESTRATOR blocks the main thread for
minutes — a multi-model `review`, a `--watch` loop, a full build/test suite, a long
`sleep`. This gate hard-BLOCKs such a command and tells the orchestrator to dispatch it
to a BACKGROUND subagent instead. It enforces the "orchestrator stays responsive" half of
`delegate-work-to-subagents` (the most clear-cut case → straight block, not warn-first).

Decided from the PARSED command (argv-aware), NEVER a raw substring of the whole string —
the #59 doctrine (block-no-verify / require-review / visual-proof / skills-read). The old
raw-regex anchored its patterns on shell-separator characters (`(`, `|`, `;`, `&&`); those
same characters appear INSIDE quoted argument strings, so a benign `tg --tag report "…we
ran review-qa (review) today…"` or `git commit -m "wire --watch flag"` was mis-read as a
`review`/`--watch` invocation and BLOCKED (agent-tools#60). The command is now shell-
tokenized (quotes/heredocs honored) and split into segments; a long process is flagged ONLY
when the REAL invoked command — `argv[0]` after leading shell noise, inline env, and no-op
wrappers (`timeout`/`env`/`nice`/`time`/…) are peeled off a segment — is the long-running
runner. A keyword that merely appears inside an argument to a DIFFERENT command (tg/echo/git
commit -m/…) no longer trips it, while a genuine inline `review`/`npm test`/`--watch`/long
`sleep` still does.

Flagged, decided per real command SEGMENT (executable parsed, not substring-matched):
  - the `review` CLI as the invoked command (`argv[0]` is `review`) — multi-model, minutes-long
  - a `--watch` flag token in the real args (gh pr checks --watch, vitest --watch, tsc --watch)
  - a build/test SUITE: npm/pnpm/yarn/bun/deno test|build, pytest/vitest/jest/cypress/playwright,
    cargo/go test|build, make/rake/msbuild test|build|all, mvn/gradle test|build|verify|package
  - `sleep N[unit]` as the invoked command with a duration >= 10s (`sleep 5m`/`sleep 1h` count)

Subagent-exempt: a dispatched subagent (``agent_id`` present) is EXPECTED to run these in
the background, so it is always allowed — this gate governs the orchestrator only.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_INLINE_PROCESS=1            — disable the guard for this session
  - env  ALLOW_INLINE_PROCESS_REASON=...   — REQUIRED with the override; logged
  - inline  `# inline-process-ok: <reason>`  — self-documenting per-command
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": responsiveness discipline, not a security boundary — a crash must never
wedge the ability to run a command. An UNPARSEABLE command (unbalanced quotes) likewise
ALLOWS (fail-open), the opposite of the fail-closed sibling block-no-verify.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

INLINE_SENTINEL = re.compile(r"#\s*inline-process-ok:\s*(\S.*)")

# ── what counts as a long-running runner (argv-keyed, not substring) ─────────────────────────
# Test/CI runners that are a long process by the NAME ALONE — `pytest`, `vitest`, … as argv[0].
_DIRECT_RUNNERS = frozenset({"pytest", "vitest", "jest", "cypress", "playwright"})
# Runners that are a suite only WITH a build/test subcommand. The word-boundary patterns mirror
# the original SUITE regex (`\btest\b` matches `test`, `test:unit`, `test-helper`; not `tests`),
# now applied per real arg TOKEN so a quoted multi-word arg of a DIFFERENT command can't match.
_TEST_BUILD = re.compile(r"\b(?:test|build)\b")
_TEST_BUILD_ALL = re.compile(r"\b(?:test|build|all)\b")
_TEST_BUILD_VERIFY_PACKAGE = re.compile(r"\b(?:test|build|verify|package)\b")
_SUITE_RUNNERS: dict[str, re.Pattern[str]] = {
    "npm": _TEST_BUILD, "pnpm": _TEST_BUILD, "yarn": _TEST_BUILD, "bun": _TEST_BUILD,
    "deno": _TEST_BUILD, "cargo": _TEST_BUILD, "go": _TEST_BUILD,
    "make": _TEST_BUILD_ALL, "rake": _TEST_BUILD_ALL, "msbuild": _TEST_BUILD_ALL,
    "mvn": _TEST_BUILD_VERIFY_PACKAGE, "gradle": _TEST_BUILD_VERIFY_PACKAGE,
}
_SLEEP_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
# `sleep N[unit]` operand — the optional unit is parsed to seconds so `sleep 5m` reads as 300s,
# not 5 (a bare \d+ would wave a five-minute sleep through).
_SLEEP_OPERAND = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")

# ── shell tokenization (SYNC: adapted from block-no-verify/block_no_verify.py, itself adapted
# from require-review / visual-proof-gate / skills-read-gate). Each hook is a standalone script
# run as its own subprocess (no shared import path), so the parser is duplicated by design — keep
# the separator/quote/comment/heredoc handling in step with the siblings when changing it. ──────
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
# Leading tokens that INTRODUCE a command but are not it — subshell/brace openers and control
# keywords. Stripping them recovers the real runner: `(review)` → `review`, `if x; then npm test`
# → (the `then` segment) `npm test`. Trailing closers sit on their own post-separator segment.
_LEADING_SHELL_NOISE = frozenset({
    "(", "{", "!", "then", "do", "else", "elif", "while", "until", "for", "case",
})
# A `VAR=value` token before the executable in a segment is an inline env assignment.
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
# No-op leading WRAPPERS that prefix the real command without changing what it does
# (`timeout 600 npm test`, `env CI=1 pytest`, `nice -n10 review`, `time make build`). The real
# runner sits AFTER the wrapper + its own args, so it is peeled off before inspecting argv[0].
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer"})
# A wrapper flag that takes its value as a SEPARATE token (`-k 5`, `--signal SIGTERM`); a glued
# form (`-n10`, `--kill-after=5`) consumes no extra token.
_WRAPPER_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after", "-n", "-u"})
_MAX_WRAPPER_NESTING = 16


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"no-long-inline-process: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present)."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _override_reason(command: str) -> str | None:
    if os.environ.get("ALLOW_INLINE_PROCESS") == "1":
        reason = (os.environ.get("ALLOW_INLINE_PROCESS_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = INLINE_SENTINEL.search(command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes (the `#` is found in the RAW text,
    before shlex de-quotes). A `#` starts a comment only at a WORD boundary and only OUTSIDE quotes,
    so a quoted `-m 'fix #42'` keeps its `#`."""
    in_single = in_double = False
    prev_ws = True  # start-of-line counts as a word boundary
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and prev_ws and not in_single and not in_double:
            return line[:idx]
        prev_ws = ch.isspace()
    return line


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line: drop a word-boundary `#` comment (quote-aware) then split GLUED
    separators (`x;npm`, `a&&pytest`) into standalone tokens. `punctuation_chars=True` emits
    `; & | && ||` as their own tokens while honoring quotes. Returns None on unbalanced quotes →
    the caller fails open."""
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # comment already stripped from the raw line (quote-aware)
    try:
        return list(lex)
    except ValueError:
        return None


def _heredoc_terminators(line: str) -> list[tuple[str, bool]]:
    """The here-documents opened on `line` as (terminator-word, dash-form) pairs, FIFO. Detection
    runs on the TOKENIZED line so a `<<WORD` inside a quoted string is absorbed, not a false opener.
    `dash-form` is True for `<<-WORD` (leading TABS allowed on the terminator line)."""
    toks = _tokenize_line(line)
    if toks is None:
        return []
    terms: list[tuple[str, bool]] = []
    for idx, tok in enumerate(toks):
        if tok == "<<" and idx + 1 < len(toks):
            raw = toks[idx + 1]
            dash = raw.startswith("-")
            word = raw.lstrip("-") if dash else raw
            if word:
                terms.append((word, dash))
    return terms


def _heredoc_body_ended(line: str, term: str, dash: bool) -> bool:
    """True when `line` is the here-document terminator. A plain `<<WORD` needs an exact match; only
    `<<-WORD` strips leading TABS."""
    candidate = line.lstrip("\t") if dash else line
    return candidate == term


def _strip_heredocs(lines: list[str]) -> list[str]:
    """Drop here-document BODY lines (data between `<<WORD` and its terminator) so a body line that
    LOOKS like a command (`npm test` inside an `-F -` heredoc message) is not parsed as a segment.
    The opener line is kept; only body + terminator lines are removed. Multiple heredocs on one line
    are consumed FIFO; an unterminated heredoc consumes to EOF."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        terminators = _heredoc_terminators(line)
        i += 1
        for term, dash in terminators:
            while i < len(lines) and not _heredoc_body_ended(lines[i], term, dash):
                i += 1  # a body (data) line — drop it
            if i < len(lines):
                i += 1  # drop the terminator line too
    return out


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a NEWLINE
    is a command separator (a `;` token between lines). Here-document bodies are stripped first. A
    line that fails to tokenize on its own (a quoted string spanning a newline) is re-joined with
    following lines until it balances. Returns None only if a chunk can never balance → fail open."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")  # honor backslash-newline line continuations
    lines = _strip_heredocs(joined.split("\n"))
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
            return None  # never balances → fail open
        if not first:
            out.append(";")  # the newline that started this chunk ends the previous command
        first = False
        out.extend(toks)
        i += 1
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &, fused forms)."""
    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEP:
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    segs.append(cur)
    return segs


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Drop leading subshell/brace openers (`(`, `{`, `!`) and control keywords (`then`/`do`/…) so a
    command introduced by them is recovered: `(review)` → `review`. Capped so an all-noise segment
    can't loop."""
    i = 0
    while i < len(segment) and i < 16 and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _split_inline_env(segment: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off a segment, returning the rest starting at the real
    executable (`CI=1 pytest` → `pytest`). The env values themselves don't affect this gate, so only
    the remainder is returned."""
    i = 0
    while i < len(segment) and _INLINE_ENV.match(segment[i]):
        i += 1
    return segment[i:]


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/timeout` → `timeout`."""
    return tok.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading no-op wrapper executables (`timeout 600`, `env CI=1`, `nice -n10`, `time`, …) so
    the REAL command beneath is what argv[0] inspection sees. Peels repeatedly (`time timeout 5m
    review`). For a recognized wrapper, its option flags (and their separate values), `env`'s leading
    `VAR=value` operands, and `timeout`'s leading DURATION positional are skipped; a bare wrapper with
    no args of its own (`time make build`, `nohup cargo build`) is dropped to expose the next token.

    Deliberately conservative: a wrapper with a POSITIONAL operand we don't model is NOT in the set, so
    we never peel one only partway and leave a non-command at the head. A non-wrapper head is returned
    unchanged. Guarded against a pathological wrapper chain by a nesting cap (then returned as-is →
    allow, the fail-open default for this discipline gate)."""
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS and guard < _MAX_WRAPPER_NESTING:
        guard += 1
        wrapper, rest = _basename(argv[0]), argv[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if "=" not in tok and tok in _WRAPPER_VALUE_FLAGS:
                    i += 1  # `--signal SIGTERM` / `-k 5` take a separate value token
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional (the wrapped command follows)
            break
        if i:  # consumed the wrapper's own args → real command starts at rest[i:]
            argv = rest[i:]
        elif rest:  # bare wrapper with no args of its own (`time make build`, `nohup cargo build`)
            argv = rest
        else:
            break  # wrapper with nothing after it
    return argv


def _is_suite(exe: str, rest: list[str]) -> bool:
    """True when the segment is a build/test suite: a direct runner by name, or a package/build tool
    with a build/test subcommand token in its real args."""
    if exe in _DIRECT_RUNNERS:
        return True
    pat = _SUITE_RUNNERS.get(exe)
    if pat is None:
        return False
    return any(pat.search(tok) for tok in rest)


def _sleep_label(exe: str, rest: list[str]) -> str | None:
    """Label for a long `sleep` (>= 10s) invoked as the command, else None. The first operand is the
    duration; its unit suffix is parsed to seconds (`sleep 5m` = 300s)."""
    if exe != "sleep" or not rest:
        return None
    m = _SLEEP_OPERAND.match(rest[0])
    if not m:
        return None
    seconds = float(m.group(1)) * _SLEEP_UNIT_S[m.group(2)]
    if seconds >= 10:
        return f"sleep {m.group(1)}{m.group(2)} (long sleep)"
    return None


def _is_watch_flag(tok: str) -> bool:
    """True for a real `--watch` flag token (`--watch` or `--watch=…`). A quoted argument that merely
    CONTAINS `--watch` as part of a longer string (`git commit -m 'wire --watch flag'`) is a single
    token unequal to the flag, so it does not match."""
    return tok == "--watch" or tok.startswith("--watch=")


def _segment_long_process(segment: list[str]) -> str | None:
    """Inspect one already-split command segment and return a long-process label, or None. Leading
    shell noise, inline env assignments, and no-op wrappers are peeled so argv[0] is the REAL
    invoked command before any keyword check runs."""
    argv = _strip_wrappers(_split_inline_env(_strip_leading_shell_noise(segment)))
    if not argv:
        return None
    exe = _basename(argv[0])
    rest = argv[1:]
    if exe == "review":
        return "review (multi-model, minutes-long)"
    if any(_is_watch_flag(tok) for tok in rest):
        return "--watch (a watch loop never exits)"
    if _is_suite(exe, rest):
        return "a build/test suite"
    return _sleep_label(exe, rest)


def _matched_long_process(command: str) -> str | None:
    """Return a short label of the matched long-running process, or None. The command is shell-
    tokenized (quotes/heredocs honored) and split into segments; each segment is inspected by its
    REAL invoked command (`argv[0]` after peeling). A keyword inside a quoted argument to a different
    command never trips the guard. An unparseable command (unbalanced quotes) returns None →
    fail-open, consistent with this hook's on_error=open policy."""
    tokens = _tokenize(command)
    if tokens is None:
        return None
    for segment in _segments(tokens):
        label = _segment_long_process(segment)
        if label:
            return label
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # A subagent is EXPECTED to run these in the background → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    matched = _matched_long_process(command)
    if matched is None:
        emit("allow")
        return 0

    reason = _override_reason(command)
    if reason:
        warn(f"inline long process allowed via escape hatch ({reason})")
        emit("allow", f"inline long process allowed via escape hatch ({reason})")
        return 0

    emit(
        "block",
        f"Run this in a BACKGROUND subagent, not the orchestrator: `{matched}` is a "
        "long-running process (review / --watch / build-or-test suite / long sleep) that "
        "would block the main thread. Dispatch an Agent with run_in_background: true to run "
        "it. Override only with a reason: ALLOW_INLINE_PROCESS=1 + "
        "ALLOW_INLINE_PROCESS_REASON='why', or append `# inline-process-ok: why`.",
    )
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
