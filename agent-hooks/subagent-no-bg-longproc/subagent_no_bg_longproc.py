#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — stop a SUBAGENT from backgrounding a long process.

The wedge this exists to kill (agent-tools#52, seen ~6x in one session): a dispatched
subagent runs a long process — a multi-model ``review``, a full build/test suite, a
``--watch`` loop, a long ``sleep`` — with ``run_in_background: true`` (or a shell ``&`` /
``setsid``), then ENDS ITS TURN saying "I'll wait for the completion notification". For a
detached job or a ``--watch`` loop no completion can ever arrive (the mechanism below), so it
idles FOREVER with uncommitted work and no PR, and the orchestrator has to catch the
rest-notification, kill the stray process, and salvage the half-done work by hand.

WHICH notifications re-invoke a subagent (the precise mechanism, agent-tools#546 — an earlier
revision of this docstring claimed, as a blanket, that no background-completion notification
ever re-invokes a subagent — FALSE for the general case; the sibling
``subagent-no-monitor`` gate (agent-tools#439) proved the split empirically):
  - a Bash tool call with ``run_in_background: true`` DOES resume the calling subagent with the
    command's output once the child exits — the harness tracks that child against the agent
    that started it (verified: a 40 s backgrounded ``python3`` sleep resumed its subagent after
    ~43 s with no further message sent to it). So an ORDINARY, non-labeled command backgrounded
    this way is a perfectly good shape for a subagent, and this gate allows it.
  - a **Monitor** watch NEVER resumes a subagent (Monitor has no harness-tracked child at all) —
    ``subagent-no-monitor`` blocks every subagent Monitor call for exactly that reason.
  - a shell-DETACHED job — a trailing ``&``, ``nohup … &``, or a ``setsid`` that forks (which
    it does, without ``-w``, whenever the caller is a process-group leader; a ``setsid`` that
    merely execs is an ordinary foreground run) — NEVER resumes a subagent either: the harness
    knows nothing about a job the shell forked behind its back, so the Bash call returns
    immediately and no completion ever arrives.
  - a ``--watch`` loop never exits, so no completion can arrive by ANY route.

What this gate BLOCKS, given that: a subagent backgrounding a LABELED long process — ``review``,
``--watch``, a build/test suite, ``sleep N>=10`` — by ANY of the three backgrounding shapes,
``run_in_background: true`` included. For a detached job or a ``--watch`` loop the wedge is
certain; for a bounded labeled process under ``run_in_background: true`` the harness WOULD
resume the subagent, and whether that case should keep being blocked (the labeled runners are
long, and a subagent waiting on its own ``review`` is the classic wedge shape regardless of the
mechanism) is a SCOPE question deliberately left to a follow-up ticket (agent-tools#559) rather than
folded into this wording fix — the gated scope here is unchanged from agent-tools#52.

This gate is the exact INVERSE of two sibling gates:
  - ``no-long-inline-process`` blocks the ORCHESTRATOR from running a long process inline
    and tells it to dispatch the work to a BACKGROUND subagent — it is subagent-EXEMPT.
  - ``background-subagent-gate`` (pre-agent) makes the orchestrator dispatch non-trivial
    subagents in the BACKGROUND.
  - THIS gate makes a SUBAGENT run its OWN long work in the FOREGROUND and block on it: a
    worker must NOT background a labeled long process (see the mechanism above for which
    backgrounding shapes never wake it).
  - ``subagent-no-monitor`` (pre-monitor) blocks a SUBAGENT's Monitor call outright — the same
    wedge via the one tool that has no foreground mode at all.

It fires ONLY for a subagent (``agent_id`` present) AND only when the command is BOTH a
long-running process AND backgrounded. A subagent that runs ``review`` in the foreground
(the correct shape) is allowed; the orchestrator is allowed (its long-process discipline is
``no-long-inline-process``'s job, not this one).

Backgrounding is bound to the long-process command, with correct bash JOB semantics (the wedge
is "the long process ITSELF is detached"), not to the whole line — so ``echo started & review
…`` (the ``echo`` job is backgrounded, ``review`` runs FOREGROUND) is allowed, while ``review …
&`` is blocked. A command counts as backgrounded when:
  - the CC Bash tool's ``run_in_background: true`` flag is set (forwarded into ``args`` by the
    bridge) — it backgrounds the WHOLE command line, so every job is then detached; the
    primary, unambiguous wedge signal;
  - a ``&`` backgrounds the JOB it terminates. ``&`` (like ``;``) launches the entire preceding
    pipeline / AND-OR list, so ``review | tee log &`` and ``review && git commit &`` both
    background ``review`` (the ``&`` is not bound to only the last simple command). ``&&`` is
    logical AND (its own token), and a ``&`` fused into a redirection (``2>&1`` → ``>&``,
    ``&>file``) is a redirect, not a background — both correctly ignored;
  - the command is led by ``setsid`` (genuinely detaches into a new session, returning
    immediately). ``nohup`` alone does NOT background — ``nohup cmd`` runs in the FOREGROUND
    and blocks; the real ``nohup cmd &`` form is caught by the ``&``.
A long process inside a backgrounded SUBSHELL (``(review)&``) is a documented under-block (the
subshell parens end the inner job before the ``&``) — under-block is the safe direction.

What counts as a "long process" is decided by the PARSED command (argv-aware), reusing the
exact detection of ``no-long-inline-process``: the ``review`` CLI as argv[0], a ``--watch``
flag, a build/test suite (npm/pnpm/yarn/bun/deno test|build, pytest/vitest/jest/cypress/
playwright, cargo/go test|build, make/rake/msbuild test|build|all, mvn/gradle
test|build|verify|package), or ``sleep N`` with N>=10s. A keyword inside a quoted argument
to a DIFFERENT command (``tg "…review…" &``) never trips it.

External approval (deny-by-default): there is NO self-service bypass. For a genuine exception,
ASK the human, or request a one-time Telegram approval by setting
`RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC="<written justification>"` — the hook asks via a
trusted `tg-ctl` and allows ONLY on an explicit approval tap. A blank value or a bare `1`/`true`
is rejected (deny), no Telegram call is made. An agent can request, not self-grant — the human
decides.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the bg flag in
           args.run_in_background, the subagent signal in args.agent_id
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": this is responsiveness/anti-wedge discipline, not a security boundary —
a crash here must never wedge a subagent's ability to run a command. An UNPARSEABLE command
(unbalanced quotes) likewise ALLOWS (fail-open).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    module = importlib.util.module_from_spec(spec)
    previous_modules = {
        name: sys.modules[name]
        for name in tuple(sys.modules)
        if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}.")
    }
    for name in previous_modules:
        if name != _HATCH_MODULE:
            sys.modules.pop(name, None)
    sys.modules[_HATCH_MODULE] = module
    # Leave the repo-local module installed on success so later imports in this
    # hook process cannot regain a preloaded user/site package or submodule.
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        for name in tuple(sys.modules):
            if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        # A helper that calls sys.exit() at import must not make the hook exit 0 (allow);
        # convert it to an import failure after cleanup. Ctrl-C still propagates.
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ImportError(f"cannot execute hatch escalation helper from {hatch_init}: {exc}") from exc
    return module


hatch_escalation = _load_hatch_escalation()

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Unescaped shell metachars that, like whitespace, are a word boundary at which a `#` opens a comment
# (`cmd ;# note`, `a |# note`) — used by `_strip_line_comment` to match POSIX comment semantics.
_COMMENT_BOUNDARY_METACHARS = frozenset(";&|()")

# ── what counts as a long-running runner (argv-keyed, not substring) ─────────────────────────
# SYNC: the long-process LABELING surface is duplicated VERBATIM from the canonical source
# agent-hooks/no-long-inline-process/no_long_inline_process.py — the constants below, the
# tokenizer (`_tokenize`/`_strip_*`/`_decompose_punct_token`), the wrapper/env/noise peelers, and
# the label helpers (`_long_process_label`/`_is_suite`/`_sleep_label`/`_is_watch_flag`). Each
# agent-hook is a standalone script run as its own subprocess with no shared import path (see that
# file's tokenization SYNC note), so this parser is duplicated by design — keep it in step with the
# sibling when its detection changes (a drift-guard test compares the constants). NOT copied: the
# BACKGROUND/job segmentation (`_split_jobs`/`_within_job_commands`/`_command_wedge`/`_wedge_label`)
# is NEW here — it implements bash JOB semantics for the `&` background operator, which the sibling
# (an orchestrator foreground-vs-background gate) has no equivalent of.
_DIRECT_RUNNERS = frozenset({"pytest", "vitest", "jest", "cypress", "playwright"})
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
_SLEEP_OPERAND = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")

_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_SUBSTITUTION_BREAKS = frozenset({"(", ")", "<(", ">("})
_SEGMENT_BREAKS = _SHELL_SEP | _SUBSTITUTION_BREAKS
_PUNCT_CHARS = frozenset("();<>|&")
_BREAKS_LONGEST_FIRST = sorted(_SEGMENT_BREAKS, key=len, reverse=True)
_LEADING_SHELL_NOISE = frozenset({
    "{", "!", "if", "then", "do", "else", "elif", "while", "until",
})
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer"})
_WRAPPER_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after", "-n", "-u"})
_MAX_WRAPPER_NESTING = 16
_MAX_LEADING_NOISE = 16

# ── what counts as "backgrounded" (this hook's own signal) ───────────────────────────────────
# A standalone `&` is the shell background operator. `&&` (logical AND) tokenizes to its OWN
# distinct token (never a lone `&`). The trap is REDIRECTIONS: `2>&1`/`>&word`/`&>file` decompose
# to a `&` piece glued to a `>`/`<` — a foreground redirect, NOT a background. So a `&` piece
# counts as background ONLY when it is NOT adjacent to a redirection char (`_REDIR_CHARS`); that
# rejects every redirect form while still catching `&`, `cmd & other`, and a trailing `)&`.
_BACKGROUND_OP = "&"
_REDIR_CHARS = frozenset({">", "<"})
# A `&` backgrounds the whole JOB to its left; these separators END a job WITHOUT backgrounding
# it (the `;`-family list separators + subshell/substitution context boundaries). The within-job
# operators below stay INSIDE the job — `&` backgrounds the entire pipeline / AND-OR list.
_JOB_SEP_NONBG = frozenset({";", ";;", ";&", ";;&", "(", ")", "<(", ">("})
_WITHIN_JOB_OPS = frozenset({"|", "|&", "&&", "||"})
# `setsid` detaches into a new session and the parent returns immediately — a background even
# without a trailing `&`. (`nohup` does NOT: `nohup cmd` runs foreground and blocks.)
_DETACHERS = frozenset({"setsid"})


# The bounded foreground heartbeat loop both subagent anti-wedge gates point at as the universal
# alternative to backgrounding. SYNC: identical constant in subagent-no-monitor
# (tests/test_subagent_no_bg_longproc.py pins the two equal), so the two hooks never drift on the
# one remedy they share.
HEARTBEAT_LOOP_EXAMPLE = (
    "`timeout 540 bash -c 'i=0; until <condition-check> || [ \"$i\" -ge 26 ]; do sleep "
    "20; i=$((i+1)); echo \"[wait] tick $i ($((i*20))s)\"; done'`"
)

# `{matched}` is the long-process label the classifier found (`review`, `--watch (...)`, …).
# The two alternatives are the SAME two subagent-no-monitor offers, so a blocked subagent is never
# pinballed between the two gates with no legal move (agent-tools#546).
BLOCK_MESSAGE = (
    "You are a SUBAGENT — run this long process ({matched}) in the FOREGROUND and BLOCK on it; "
    "do NOT background it. Remove `run_in_background: true` (and any trailing `&` / `setsid` / "
    "`nohup … &`) and run it inline so this tool call blocks until it finishes. Why: a shell-"
    "detached job (`&`/`nohup`/a forking `setsid`) never wakes you — the harness knows nothing about it — "
    "and a `--watch` loop never exits, so ending your turn on it wedges you FOREVER with "
    "uncommitted work and no PR; a labeled long process (review/--watch/build-test suite/long "
    "sleep) is blocked from backgrounding by ANY shape, `run_in_background: true` included. "
    "Use one of these instead: (1) waiting on a SINGLE ORDINARY command (NOT one of those "
    "labeled long processes) — `run_in_background: true` on the Bash tool call is fine: the "
    "harness tracks that child and auto-resumes you with its output when it exits. (2) waiting "
    "on anything else (a labeled long process, a condition, a file, several things at once) — "
    "block on it yourself in the FOREGROUND with a heartbeat loop: echo a line at least every "
    "~20s and keep each Bash call under ~540s, repeating the same bounded call until the wait is "
    "over, e.g.: " + HEARTBEAT_LOOP_EXAMPLE + ". Never Monitor — subagent-no-monitor blocks it "
    "for a subagent (it never resumes you). There is NO self-service bypass. For a genuine "
    "exception, ASK the human, or request a one-time Telegram approval by setting "
    "RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC=\"<written justification>\" "
    "(deny-by-default; a bare 1 is rejected)."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"subagent-no-bg-longproc: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present).

    The signal is authoritative ONLY because `cc_hook_bridge` takes agent_id from CC's
    top-level event and DROPS any copy forged in `tool_input` — a worker can't be spoofed in,
    and (the trust direction that matters here) the orchestrator can't be spoofed OUT of the
    gate by a forged `args.agent_id`.
    """
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


def _is_run_in_background(event: dict) -> bool:
    """The Bash tool's ``run_in_background`` flag, truthily, from ``args`` or the top-level event.

    CC sends a JSON bool under ``args``; we also accept the common stringified / numeric truthy
    forms a wrapping harness might emit (``"true"``/``"1"``/``"yes"``, int/float != 0) so a
    backgrounded long process is never mis-read as foreground. Reads ``args`` first then the
    top-level event — the same fallback used for ``command``/``agent_id`` — so a harness that
    surfaces the flag at the top level isn't missed. Only an explicit truthy value backgrounds,
    so this can't false-positive.
    """
    args = event.get("args") or {}
    val = args.get("run_in_background")
    if val is None:
        val = event.get("run_in_background")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):  # bool handled above → this is a real int/float
        return val != 0
    return isinstance(val, str) and val.strip().lower() in {"true", "1", "yes"}


def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes and POSIX backslash-escaping.

    A `#` starts a comment only at a WORD boundary (after whitespace / line start / an
    unescaped metachar) and only OUTSIDE quotes, so a quoted `-m 'fix #42'` keeps its `#`.
    """
    in_single = in_double = False
    prev_ws = True  # start-of-line counts as a word boundary
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and not in_single:
            i += 2  # escaped char: skip it so `\"`/`\\`/`\#` can't toggle quoting or open a comment
            prev_ws = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and prev_ws and not in_single and not in_double:
            return line[:i]
        prev_ws = ch.isspace() or (
            ch in _COMMENT_BOUNDARY_METACHARS and not in_single and not in_double
        )
        i += 1
    return line


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line (quote-aware comment stripped first); None on unbalanced quotes."""
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # comment already stripped from the raw line (quote-aware)
    try:
        return list(lex)
    except ValueError:
        return None


def _heredoc_terminators(line: str) -> list[tuple[str, bool]]:
    """The here-documents opened on `line` as (terminator-word, dash-form) pairs, FIFO."""
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
    """True when `line` is the here-document terminator (`<<-WORD` strips leading TABS)."""
    candidate = line.lstrip("\t") if dash else line
    return candidate == term


def _strip_heredocs(lines: list[str]) -> list[str]:
    """Drop here-document BODY lines so a body line that LOOKS like a command is not parsed."""
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
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream.

    A NEWLINE is a command separator (a `;` token between lines). Here-document bodies are
    stripped first; a line that fails to tokenize on its own is re-joined with following lines
    until it balances. Returns None only if a chunk can never balance → fail open.
    """
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


def _decompose_punct_token(tok: str) -> list[str]:
    """Re-split an ALL-punctuation token into known break tokens by greedy longest-match."""
    if not tok or any(ch not in _PUNCT_CHARS for ch in tok):
        return [tok]
    pieces: list[str] = []
    i = 0
    while i < len(tok):
        for br in _BREAKS_LONGEST_FIRST:
            if tok.startswith(br, i):
                pieces.append(br)
                i += len(br)
                break
        else:
            pieces.append(tok[i])  # unknown lone punct char — keep it, it is not a break
            i += 1
    return pieces


def _split_jobs(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Split tokens into shell JOBS (the unit a `&` backgrounds), each tagged backgrounded.

    Correct bash semantics: `&` (like `;` / a newline) is a LIST separator that launches the
    ENTIRE preceding AND-OR-list / pipeline — so `&` backgrounds the whole job to its left, not
    just the last simple command. Thus `review | tee log &` and `review && git commit &` both
    BACKGROUND `review`. The within-job operators `|`, `|&`, `&&`, `||` stay INSIDE the job (a
    later step splits the job into individual commands on them). A `&` adjacent to a redirection
    char (`2>&1` → `>&`, `&>file`) is a redirect, not a terminator. The `;`-family and
    subshell/substitution parens end a job WITHOUT backgrounding it. Returns
    ``[(job_tokens, backgrounded)]``.
    """
    jobs: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    for tok in tokens:
        pieces = _decompose_punct_token(tok)
        for i, piece in enumerate(pieces):
            if piece == _BACKGROUND_OP:
                prev_piece = pieces[i - 1] if i > 0 else None
                next_piece = pieces[i + 1] if i + 1 < len(pieces) else None
                if prev_piece in _REDIR_CHARS or next_piece in _REDIR_CHARS:
                    cur.append(piece)  # a redirection's `&` (`>&`/`&>`), not a job terminator
                    continue
                jobs.append((cur, True))  # `&` backgrounds the WHOLE preceding job
                cur = []
            elif piece in _JOB_SEP_NONBG:
                jobs.append((cur, False))  # `;`-family / subshell boundary ends a job, no bg
                cur = []
            else:
                cur.append(piece)  # words AND within-job ops (`|`/`&&`/…) stay in the job
    jobs.append((cur, False))
    return jobs


def _within_job_commands(job_tokens: list[str]) -> list[list[str]]:
    """Split a job's tokens into the individual commands joined by `|` / `|&` / `&&` / `||`."""
    commands: list[list[str]] = []
    cur: list[str] = []
    for piece in job_tokens:
        if piece in _WITHIN_JOB_OPS:
            commands.append(cur)
            cur = []
        else:
            cur.append(piece)
    commands.append(cur)
    return commands


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Drop leading brace-group openers (`{`, `!`) and control keywords (`then`/`do`/…)."""
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _split_inline_env(segment: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off a segment, returning the rest at the executable."""
    i = 0
    while i < len(segment) and _INLINE_ENV.match(segment[i]):
        i += 1
    return segment[i:]


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/timeout` → `timeout`."""
    return tok.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading no-op wrapper executables (`timeout 600`, `env CI=1`, `nice -n10`, …)."""
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
    """True when the segment is a build/test suite (direct runner, or pkg tool + test/build sub)."""
    if exe in _DIRECT_RUNNERS:
        return True
    pat = _SUITE_RUNNERS.get(exe)
    if pat is None:
        return False
    return any(pat.search(tok) for tok in rest)


def _sleep_label(exe: str, rest: list[str]) -> str | None:
    """Label for a long `sleep` (>= 10s) invoked as the command, else None."""
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
    """True for a real `--watch` flag token: `--watch`, `--watch=…`, or `--watch-*`."""
    if any(ch.isspace() for ch in tok):  # de-quoted multi-word message, not a flag token
        return False
    return tok == "--watch" or tok.startswith("--watch=") or tok.startswith("--watch-")


def _long_process_label(argv: list[str]) -> str | None:
    """Return a long-process label for an already-peeled argv (review/--watch/suite/sleep)."""
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


def _command_wedge(command_tokens: list[str], backgrounded: bool) -> str | None:
    """Return a long-process label when this command is a long process AND detached, else None.

    Detached = the whole job is backgrounded (`backgrounded`) OR the command is led by a
    `setsid` detacher. A foreground command (neither) is the correct shape → None.
    """
    head = _split_inline_env(_strip_leading_shell_noise(command_tokens))
    argv = _strip_wrappers(head)
    label = _long_process_label(argv)
    if label is None:
        return None
    # `setsid` is peeled by `_strip_wrappers`; detect it in the prefix that got peeled so a
    # `setsid` sitting as a non-leading ARGUMENT (`make build setsid`) never counts as a detach.
    peeled_prefix = head[: len(head) - len(argv)]
    setsid_detached = any(_basename(tok) in _DETACHERS for tok in peeled_prefix)
    if backgrounded or setsid_detached:
        return label
    return None


def _wedge_label(tokens: list[str], run_in_background: bool) -> str | None:
    """The label of a BACKGROUNDED long-process command, or None if none is a wedge.

    A `&` backgrounds the whole JOB (pipeline / AND-OR list) to its left, so EVERY command in a
    backgrounded job is detached — `review | tee log &` and `review && git commit &` are both
    flagged. `run_in_background: true` backgrounds the WHOLE command line, so every job is
    detached. LIMITATION (documented under-block, same precision class as the sibling's
    nested-shell limit): a long process wrapped in a NESTED shell construct right before a
    trailing `&` — a subshell `(review)&`, a process substitution `review <(git diff) &`, a
    command substitution `foo $(review) &` — is NOT flagged, because the construct's
    paren/opener is a job boundary that ends the inner job before the `&`. The realistic direct
    wedge forms (`review … &`, `review | tee log &`, `review && commit &`,
    `run_in_background: true`, `setsid review`) are caught; under-block is the safe direction
    for this on_error=open discipline gate.
    """
    for job_tokens, job_bg in _split_jobs(tokens):
        backgrounded = run_in_background or job_bg
        for command_tokens in _within_job_commands(job_tokens):
            label = _command_wedge(command_tokens, backgrounded)
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

    # This gate governs WORKERS only. The orchestrator's long-process discipline is
    # `no-long-inline-process`'s job; here a non-subagent (no agent_id) is always allowed.
    if not _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    tokens = _tokenize(command)
    if tokens is None:
        # unparseable (unbalanced quotes) → fail open, consistent with on_error=open
        emit("allow")
        return 0

    # The wedge is "a LONG-PROCESS command is itself backgrounded". Backgrounding binds to the
    # whole JOB a `&` launches, so `echo x & review …` (review FOREGROUND) is allowed while
    # `review … &`, `review | tee log &`, and `review && commit &` (review backgrounded) are
    # blocked. A subagent running its long work in the FOREGROUND is the correct shape; a
    # backgrounded SHORT command is no wedge risk.
    matched = _wedge_label(tokens, _is_run_in_background(event))
    if matched is None:
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or args.get("cwd") or os.getcwd())
    block_message = BLOCK_MESSAGE.format(matched=matched)

    ctx = {"hook": "subagent-no-bg-longproc", "command": command}
    hatch = hatch_escalation.request_hatch_approval(
        "subagent-no-bg-longproc", ctx, cwd=cwd, command=command
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"subagent-no-bg-longproc allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        emit("block", f"hatch escalation denied: {hatch.reason}\n{block_message}")
        return BLOCK_EXIT_CODE

    emit("block", block_message)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
