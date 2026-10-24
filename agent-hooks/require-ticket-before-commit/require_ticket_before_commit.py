#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require a ticket reference on a commit.

When the agent is about to `git commit`, this checks the commit message (and the
current branch name) for a reference to a tracking ticket — a task-cli id, a
GitHub issue, or a Linear key. If none is found, it BLOCKS by default (strict),
reminding the author that every non-trivial change should start from a ticket with
acceptance criteria + motivation + user-impact.

Enforces the `strict-ticket-discipline` skill. Pairs with task-cli.

Ticket-detection heuristic (intentionally broad — false-negatives nag, they don't
wedge; default fail policy is open/warn so over-detection is the cheap error):
  - `#123`                              GitHub issue / PR number
  - `GH-123`, `org/repo#123`           qualified GitHub references
  - `ABC-123`                          Linear / Jira-style KEY-NUM (>=2 letters)
  - `task:ABC-12`, `task #12`, `T-12`  task-cli ids
  - `Refs: …`, `Closes #…`, `Fixes …`  the conventional trailer keywords
  - a full tracker URL (github.com/.../issues/123, linear.app/.../issue/…)

Exempt from the gate (no ticket expected): trivial-chore commit types
(`chore:`/`docs:`/`style:`/`ci:`/`build:`/`test:`), and `wip`/`fixup!`/`squash!`
/`amend`/merge/revert commits. Configure via env (see README).

No self-service bypass for a genuinely ticketless commit: there is NO per-commit env var /
message trailer an agent can set on its own command to skip this gate (a self-grant is
security theater). For a real one-off, use a `chore:`/`docs:` type if it is truly trivial,
or (top-level agent only) request a ONE-TIME Telegram approval via
`RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="<justification>"` (deny-by-default; a blank or
bare `1` is rejected — the value must be a real written justification; allowed ONLY on the
human's tg-ctl approval). `REQUIRE_TICKET_STRICT=0` remains a repo-level opt-out back to
warn-only (a rollout policy dial, not a per-command grant).

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": a CRASH in the check still fails open — process discipline,
not a security boundary, so a bug must never make committing impossible. But on a
SUCCESSFUL run with no ticket reference and no exemption/escape, the default is now
a hard BLOCK (exit 10). Set REQUIRE_TICKET_STRICT=0 to fall back to warn-only.

The `-F -` (message on git's STDIN) case FAILS CLOSED WITH A HINT (agent-tools#104,
reversing the #102 fail-open): the hook cannot read git's stdin to verify a ticket, so
rather than let `-F -` silently dodge the gate it BLOCKS with an actionable message —
pass the message a readable way (`-m "…Refs #N…"` or `-F <file>`), encode the ticket in
the branch name, or request a one-time Telegram approval
(`RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT`). The hatch is consulted BEFORE the `-F -`
block, so an approved hatch allows a `-F -` commit; a readable `-F <file>` is checked normally.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


class _UnavailableHatchResult:
    """Hatch result when the repo-local helper cannot be imported.

    should_stop is False so the hook falls through to its own default
    decision (block a gated event, allow otherwise). Crashing here would
    let on_error:open fail-open a denial-class hook (#547).
    """

    requested = False
    approved = False
    reason = "hatch helper unavailable"
    env_var = ""
    env_present = False
    should_stop = False
    tg_ctl_path = None


class _UnavailableHatch:
    def request_hatch_approval(self, *args, **kwargs):
        return _UnavailableHatchResult()


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        return _UnavailableHatch()
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        return _UnavailableHatch()
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
        # A helper that calls sys.exit() at import must not make the hook exit 0
        # (allow via on_error:open). Ctrl-C still propagates; any other failure
        # installs a stub so the hook can still classify the event (#547).
        if isinstance(exc, KeyboardInterrupt):
            raise
        return _UnavailableHatch()
    return module


hatch_escalation = _load_hatch_escalation()

# The role-aware "what to do next" builder (agent-tools#994). Loaded by repo-local path exactly like
# the hatch helper (a preloaded user/site package of the same name must not win) and OPTIONAL: a
# hook crash would fail open via on_error=open, so any load failure just falls back to the
# built-in neutral wording in `_render_block`.
_GUARD_MESSAGE_MODULE = "agenttools_guard_message"


def _load_guard_message():
    init = Path(__file__).resolve().parents[2] / "lib" / _GUARD_MESSAGE_MODULE / "__init__.py"
    if not init.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_GUARD_MESSAGE_MODULE, init)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_GUARD_MESSAGE_MODULE)
    # dataclasses resolves string annotations through sys.modules[cls.__module__], so the module
    # must be registered while it executes.
    sys.modules[_GUARD_MESSAGE_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        if previous is None:
            sys.modules.pop(_GUARD_MESSAGE_MODULE, None)
        else:
            sys.modules[_GUARD_MESSAGE_MODULE] = previous
        if isinstance(exc, KeyboardInterrupt):
            raise
        return None
    return module


_guard_message = _load_guard_message()
# The event main() is currently deciding, so the block emitters (several frames away) can name the
# reader's role without threading the event through every signature. One event per hook process.
_CURRENT_EVENT: dict = {}

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
# Canonical hook id for the shared Telegram hatch (RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT).
HOOK_ID = "require-ticket-before-commit"

# A STABLE marker embedded in every block message — a fixed string the task-cli Docker
# test (and any external assertion) can grep for to confirm THIS gate blocked, regardless
# of how the surrounding advice is reworded. Do not change it casually.
BLOCK_MARKER = "[require-ticket] BLOCKED: no ticket reference"

# Strict is now the DEFAULT: a missing ticket is a hard BLOCK. Set REQUIRE_TICKET_STRICT=0
# (or false/no/off) to opt back out to warn-only. Any other value — including unset — is
# strict. `os.environ.get(...)` here reads the HOOK's process env, which is the right scope
# for a repo-level on/off rollout dial (NOT a per-command self-grant — there is no per-commit
# skip env / trailer any more; a genuine one-off routes through the Telegram hatch instead).
_STRICT_FALSEY = frozenset({"0", "false", "no", "off"})
STRICT = os.environ.get("REQUIRE_TICKET_STRICT", "1").strip().lower() not in _STRICT_FALSEY

# A leading `VAR=value` inline-env assignment on the command (`env HUSKY=0 git commit …`).
# Parsed so a wrapper's inline env doesn't leak into argv; the values themselves are no longer
# consulted for any skip decision (the old per-commit `REQUIRE_TICKET_SKIP` was removed).
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

# `git commit --continue/--abort/--skip` (merge/rebase plumbing) and `--amend`
# are not authoring a fresh change → don't gate them. Matched against the PARSED commit
# argv (NOT the raw command), so a flag named in the commit MESSAGE / a sibling command
# can't falsely exempt a real commit (agent-tools#97).
SKIP_FLAGS = frozenset({"--continue", "--abort", "--skip", "--amend"})

# Commit-type prefixes that are exempt by default (conventional-commit chores).
# Override the set with REQUIRE_TICKET_EXEMPT_TYPES="chore,docs" (comma-separated).
_DEFAULT_EXEMPT_TYPES = "chore,docs,style,ci,build,test,revert"
EXEMPT_TYPES = {
    t.strip()
    for t in os.environ.get("REQUIRE_TICKET_EXEMPT_TYPES", _DEFAULT_EXEMPT_TYPES).split(",")
    if t.strip()
}
# A conventional-commit header: `type(scope)!: subject`. Capture the bare type.
# Anchored at the start of the (first) line — the type only counts on the subject,
# not on some `test:`-looking line buried in the body.
CONVENTIONAL = re.compile(r"^\s*([a-z]+)(?:\([^)]*\))?!?:", re.IGNORECASE)

# WIP / fixup / merge / revert markers that mean "not a normal authored commit".
EXEMPT_MARKERS = re.compile(
    r"^\s*(?:wip\b|fixup!|squash!|amend!|merge\b|revert\b)", re.IGNORECASE
)

# --- Ticket-reference heuristic -------------------------------------------------
# Kept deliberately permissive: a missed reference at worst nags (fail-open/warn),
# while a false hit just lets a commit through that probably had a ticket anyway.
#
# A KEY-NUM id needs >=2 uppercase letters so it can't collide with a version like
# `A1-456` or a hyphenated word; a trailer keyword (Closes/Fixes/Refs) must be
# followed by a REAL ref, not any token, so `fix: null deref` is NOT a reference.
_KEY_NUM = r"[A-Z]{2,}[A-Z0-9]*-\d+"  # ABC-123, ENG-7, GH style keys
_REF = rf"(?:#\d+|{_KEY_NUM}|T-\d+)"  # the concrete things a trailer can point at
TICKET_PATTERNS = (
    re.compile(r"(?:^|\s|\()#\d+\b"),                       # #123 (GitHub issue/PR)
    re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+\b"),  # org/repo#123
    re.compile(r"\bGH-\d+\b", re.IGNORECASE),               # GH-123
    re.compile(rf"\b{_KEY_NUM}\b"),                          # ABC-123 (Linear/Jira/task)
    re.compile(r"\btask\s*[:#]\s*\S+", re.IGNORECASE),      # task:ABC-12 / task #12
    re.compile(r"\bT-\d+\b"),                                # T-12 (short task id)
    re.compile(r"\[ticket:\s*\S[^\]]*\]", re.IGNORECASE),  # [ticket: <id-or-slug>] trailer (non-blank)
    re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|ref[s]?)\b[:\s]+{_REF}\b",
        re.IGNORECASE,
    ),  # Closes #12 / Fixes ABC-3 / Refs: T-9  (must point at a real ref)
    re.compile(
        r"https?://\S*(?:github\.com/\S+/issues/\d+|linear\.app/\S+/issue/\S+|"
        r"/browse/[A-Z]+-\d+)",
        re.IGNORECASE,
    ),  # full tracker URLs
)


# ── heredoc neutralization ────────────────────────────────────────────────────────────────────
# A `git commit` command is often built with a heredoc feeding a multi-line message — the exact
# pattern this repo's own CLAUDE.md tells every agent to use for a multi-line commit message:
#   git commit -m "$(cat <<'EOF'
#   fix: …
#   Closes #123
#   EOF
#   )"
# and its `-F <file>` cousin, where the file is written by a heredoc EARLIER in the SAME command:
#   cat > msg.txt <<'EOF'
#   fix: …
#   Refs #143
#   EOF
#   git commit -F msg.txt
# A heredoc BODY is not ordinary shell text: `"`/`'` inside it are LITERAL (never real shell
# quotes) and `#` is never a comment-starter — but the shlex-based tokenizer below has no heredoc
# awareness, so raw body characters corrupt its quote-tracking. That corruption goes EITHER way
# depending on the body's exact quote parity: a `#`-led ticket reference past a stray embedded
# quote is mistaken for a shell comment and silently dropped (agent-tools#406 — a false BLOCK on a
# correctly-ticketed commit; reproduced live: a body containing a `"paired quote"` before `Closes
# #999` truncates the extracted message to nothing past the quote), or an odd embedded-quote count
# leaves the WHOLE command looking quote-unbalanced, so it fails to tokenize at all and the
# on_error=open gate fails OPEN — silently ALLOWING a genuinely ticketless commit (the same root
# cause's other face, also reproduced live). A heredoc body LINE that happens to parse as its own
# valid shell segment can also be mistaken for a REAL command (agent-tools#570's "bonus repro": a
# `gh issue create --body-file` heredoc whose markdown body merely MENTIONS `git commit -F …` got
# gated as if it actually were one, because the tokenizer's line-by-line retry loop treats an
# independently-parseable body line as a fresh command segment).
#
# The fix: before any of the shlex-based parsing below ever runs, replace every heredoc
# (`<<WORD`/`<<-WORD` … the terminator line) with an opaque SENTINEL token holding no shell
# metacharacters at all, so its body characters can never perturb quote/comment tracking or be
# mistaken for a command segment — the same "make the dangerous span an atomic token" idea
# block-raw-pr-merge uses for a related but different purpose (salvaging a hidden merge out of a
# heredoc body instead of shielding one from it). `_resolve_message_value` then unwraps the
# documented `$(cat SENTINEL)` shape back to the real body text for a `-m` value, and
# `_heredoc_body_for_target` matches a `-F <path>` against a heredoc that WROTE that same path
# earlier in the command, reading the captured body directly instead of the disk (which does not
# have the file yet at PreToolUse hook time — the shell has not run).
_HEREDOC_SENTINEL_FMT = "\x00RTHOOK-HEREDOC-{}\x00"
# Chars that end an UNQUOTED heredoc delimiter word — mirrors block-raw-pr-merge's identical set;
# duplicated by design (each hook is its own subprocess, no shared import path).
_HEREDOC_DELIM_END = frozenset(" \t\n;&|()<>")


def _read_heredoc_delimiter(command: str, j: int) -> tuple[str, int, bool] | None:
    """Read ONE shell word starting at `command[j]` as a heredoc delimiter and return
    `(dequoted_word, end_index, quoted_delimiter)`, or None on an unterminated quote / empty word.
    `quoted_delimiter` is True when the delimiter token was single- or double-quoted (`<<'EOF'`,
    `<<"EOF"`), meaning bash will NOT expand parameters/command substitutions in the heredoc BODY —
    the captured literal is the actual message content. An unquoted `<<EOF` leaves
    `quoted_delimiter=False`; its body is expanded by the shell before any consumer sees it, so the
    literal source must NOT be treated as the final commit message for ticket recovery."""
    n = len(command)
    out: list[str] = []
    k = j
    quoted_delimiter = k < n and command[k] in ("'", '"')
    while k < n:
        ch = command[k]
        if ch in _HEREDOC_DELIM_END:
            break
        if ch == "\\" and k + 1 < n:
            out.append(command[k + 1])
            k += 2
            continue
        if ch in ("'", '"'):
            close = command.find(ch, k + 1)
            if close == -1:
                return None
            out.append(command[k + 1 : close])
            k = close + 1
            continue
        out.append(ch)
        k += 1
    word = "".join(out)
    return (word, k, quoted_delimiter) if word else None


def _read_heredoc(command: str, i: int) -> tuple[str, bool, int, bool] | None:
    """If `command[i:]` opens a heredoc (`<<WORD` / `<<-WORD`), return `(delimiter, dash,
    index_after_the_operator)`; else None. `<<<` (a here-string, one line, no body to capture) is
    not a heredoc."""
    if not command.startswith("<<", i) or command.startswith("<<<", i):
        return None
    n = len(command)
    j = i + 2
    dash = j < n and command[j] == "-"
    if dash:
        j += 1
    while j < n and command[j] in " \t":
        j += 1
    if j >= n:
        return None
    parsed = _read_heredoc_delimiter(command, j)
    if parsed is None:
        return None
    delim, end, quoted = parsed
    return delim, dash, end, quoted


def _collect_heredoc_body(
    command: str, body_start: int, delim: str, dash: bool,
) -> tuple[str, int] | None:
    """From `body_start` (the first character of the heredoc body), consume whole lines up to (NOT
    including) the terminator line's OWN trailing newline (bare `delim`, leading tabs ignored when
    the opener used `<<-`). Returns `(body_text, index_of_that_newline_or_end_of_command)`, or None
    if no terminator line is found ahead (an unterminated heredoc — the caller leaves it untouched,
    deferring to the existing fail-open behavior on an unparseable command rather than swallowing
    the rest of the input).

    Deliberately stops AT the newline rather than past it: that newline is what separated the
    heredoc's issuing command from whatever came next in the ORIGINAL text (`cat > f <<EOF\\n…\\nEOF
    \\ngit commit …`), or was itself just a line inside a still-open construct (`"$(cat <<'EOF'\\n…
    \\nEOF\\n)"`, where the terminator's newline is followed by the substitution's closing `)"` on
    the SAME logical statement). The caller (`_neutralize_heredocs`) leaves that one character in
    the output stream to be scanned normally, so `_tokenize`'s own multi-line-quote join logic
    decides which case it is — exactly as it would with no heredoc involved at all. Swallowing it
    here instead (the very first version of this function did) glues the heredoc's issuing command
    directly onto whatever follows with NO separator, corrupting segment-splitting."""
    n = len(command)
    pos = body_start
    lines: list[str] = []
    while pos <= n:
        nl = command.find("\n", pos)
        line_end = n if nl == -1 else nl
        line = command[pos:line_end]
        check = line.lstrip("\t") if dash else line
        if check == delim:
            return "\n".join(lines), line_end
        lines.append(check if dash else line)
        if nl == -1:
            return None  # ran off the end without finding the terminator
        pos = line_end + 1
    return None


def _redirect_target_before(text: str) -> str | None:
    """The `> PATH`/`>> PATH` redirect target ending the CURRENT simple command in `text` (the
    rewritten command SO FAR, up to but not including a heredoc operator just found), or None.
    Best-effort text scan (not a full parse): only looks at the tail after the last unquoted
    command separator, so a `>` belonging to an EARLIER command in a chain is never picked up.
    Good enough to recognize the documented `cat > FILE <<EOF … EOF` shape (agent-tools#570). The
    target word itself is read with `_read_heredoc_delimiter` — the SAME quote-removing shell-word
    reader a heredoc delimiter uses — so a quoted path containing a space (`cat > '/tmp/commit
    message' <<EOF`) is read whole instead of truncated at the first internal space (review round
    2: a plain whitespace-delimited regex here previously chopped it). An unrecognized shape
    simply yields no target and the `-F` value falls back to a normal disk read, same as today."""
    cut = 0
    for sep in (";", "&&", "||", "|", "\n", "("):
        idx = text.rfind(sep)
        if idx != -1 and idx + len(sep) > cut:
            cut = idx + len(sep)
    tail = text[cut:]
    target = None
    for m in re.finditer(r"(?:^|\s)>>?\s*", tail):
        parsed = _read_heredoc_delimiter(tail, m.end())
        if parsed is not None:
            target = parsed[0]  # keep the LAST `>`/`>>` on the tail — the one that actually wins  # parsed[0] is dequoted path
    return target or None


class _Heredoc:
    """One neutralized heredoc. `sentinel` is the opaque placeholder substituted into the rewritten
    command in place of `<<[-]DELIM … DELIM` (or, for a chained `<<A <<B` group on one line, in
    place of the WHOLE group — see `_read_heredoc_operators_on_line`); `body` is its EFFECTIVE
    captured literal text; `target` is the raw (unresolved) `> PATH`/`>> PATH` redirect target
    immediately preceding it on the same simple command, or None — the file a `cat > PATH <<EOF …
    EOF` heredoc would write (agent-tools#570).

    `segment_index` is set LATER, by `_assign_heredoc_segment_indices` in `main` (not at
    construction time — segments aren't parsed yet when a heredoc is captured): the index of the
    SIMPLE COMMAND segment containing this heredoc's sentinel, among ALL segments in the rewritten
    command (the SAME enumeration `commit_segments` numbers its own `CommitSegment.index` from).
    `_heredoc_body_for_target` uses it to reject a heredoc whose segment comes AT OR AFTER the
    consuming commit's own segment, so a heredoc that writes its target file only AFTER a commit
    already read it can't retroactively satisfy that commit's ticket gate. Deliberately reuses the
    real segment parser instead of an independent text search (agent-tools#570 review round 2: a
    search for a bare word like "commit" could match unrelated prose — `echo commit` — BEFORE the
    real commit segment and mis-anchor the cutoff)."""

    __slots__ = ("sentinel", "body", "target", "segment_index", "quoted_delimiter")

    def __init__(
        self, sentinel: str, body: str, target: str | None, quoted_delimiter: bool,
    ) -> None:
        self.sentinel = sentinel
        self.body = body
        self.target = target
        self.segment_index: int | None = None
        self.quoted_delimiter = quoted_delimiter


class _Frame:
    """One quote-tracking frame. `opened_by` is `"paren"` for a frame pushed by `$(` (its matching
    `)` pops it) or `"backtick"` for a frame pushed by a backtick (the NEXT unescaped backtick at
    this frame pops it); `None` marks the bare top-level frame, never popped."""

    __slots__ = ("in_single", "in_double", "opened_by")

    def __init__(self, opened_by: str | None) -> None:
        self.in_single = False
        self.in_double = False
        self.opened_by = opened_by


def _append_with_boundary(out: list[str], sentinel: str) -> None:
    """Append `sentinel` to `out`, inserting a separating space first if the preceding character
    would otherwise glue directly onto it. A COMPACT heredoc opener with no space before it
    (`cat<<'EOF'`) would otherwise neutralize to `catSENTINEL` — no longer word-boundary-matching
    `cat` at all, so `_CAT_SUBST_RE`'s `$(cat SENTINEL)` unwrap (and any human/tool reading the
    rewritten text) sees a corrupted word instead (agent-tools#570 review)."""
    if out and (out[-1].isalnum() or out[-1] in "_./-"):
        out.append(" ")
    out.append(sentinel)


def _read_heredoc_operators_on_line(command: str, i: int) -> list[tuple[str, bool, int, bool]]:
    """Collect every heredoc operator (`<<WORD`/`<<-WORD`) CHAINED on the same line starting at
    `command[i]` — bash allows `cmd <<A <<B`, each separated only by whitespace — returning each
    as `(delimiter, dash, index_after_the_operator, quoted_delimiter)` in the order declared.
    Empty if `command[i]` isn't a heredoc opener at all."""
    ops: list[tuple[str, bool, int, bool]] = []
    pos = i
    while True:
        opener = _read_heredoc(command, pos)
        if opener is None:
            break
        delim, dash, after, quoted = opener
        ops.append((delim, dash, after, quoted))
        j = after
        while j < len(command) and command[j] in " \t":
            j += 1
        if not command.startswith("<<", j) or command.startswith("<<<", j):
            break
        pos = j
    return ops


def _consume_chained_heredoc_bodies(
    command: str, ops: list[tuple[str, bool, int, bool]],
) -> tuple[str, int, bool] | None:
    """Consume the bodies of every operator in `ops` (from `_read_heredoc_operators_on_line`) IN
    THE ORDER DECLARED — real bash reads them off the input stream that way, each terminated by
    its own delimiter line, one body immediately after the previous terminator line. Returns
    `(effective_body, index_after_the_last_terminator_line)`, where `effective_body` is the LAST
    operator's body: when several heredocs are chained onto the SAME fd (`cmd <<A <<B`), shell
    redirects apply left-to-right and the LAST one wins — `cmd`'s stdin ends up wired to B, not A
    (agent-tools#570 review: modeling only the first operator let a ticket hidden in an earlier,
    UNUSED heredoc silently satisfy the gate while the command actually read a different,
    ticketless body). Returns None if any operator in the group is unterminated — the WHOLE group
    is then left untouched by the caller, matching the single-heredoc fail-safe."""
    body_start = None
    body = None
    quoted_delimiter = False
    for delim, dash, after, quoted in ops:
        if body_start is None:
            nl = command.find("\n", after)
            if nl == -1:
                return None
            body_start = nl + 1
        collected = _collect_heredoc_body(command, body_start, delim, dash)
        if collected is None:
            return None
        body, resume_at = collected
        quoted_delimiter = quoted  # chained `<<A <<B`: the LAST operator's body wins
        body_start = resume_at + 1  # the next chained heredoc's body starts right after this one
    return body, body_start - 1, quoted_delimiter  # back off the `+ 1`: caller expects the terminator's own newline


def _try_consume_heredoc(
    command: str, i: int, out: list[str], heredocs: list[_Heredoc],
) -> int | None:
    """If a real, TERMINATED heredoc (or chained group, `cmd <<A <<B`) opens at `command[i]`,
    append its sentinel to `out`, record it in `heredocs`, and return the index right after the
    group's last terminator line. Returns None (nothing consumed — the caller falls through to
    normal char handling) when `command[i]` isn't actually a heredoc opener, or any operator in
    the group has no terminator line ahead (fail-safe: an unterminated heredoc/group is left
    untouched rather than swallowing the rest of the input)."""
    ops = _read_heredoc_operators_on_line(command, i)
    if not ops:
        return None
    consumed = _consume_chained_heredoc_bodies(command, ops)
    if consumed is None:
        return None
    body, resume_at, quoted_delimiter = consumed
    sentinel = _HEREDOC_SENTINEL_FMT.format(len(heredocs))
    target = _redirect_target_before("".join(out))
    heredocs.append(_Heredoc(sentinel, body, target, quoted_delimiter))
    _append_with_boundary(out, sentinel)
    return resume_at


def _is_word_boundary(ch: str) -> bool:
    """True when `ch` is whitespace or a shell separator that a `#` comment can legally follow —
    mirrors `_strip_line_comment`'s own `prev_ws` test but also treats `;&|()` as a boundary
    (block-raw-pr-merge's `_SEGMENT_SEPS`), so `cmd; # note` and `$(# comment ...)` are recognized
    too, not only a `#` preceded by plain whitespace."""
    return ch.isspace() or ch in ";&|()"


def _neutralize_heredocs(command: str) -> tuple[str, list[_Heredoc]]:  # noqa: C901
    """Replace every heredoc (or chained `<<A <<B` group) in `command` with an opaque sentinel
    token, returning the rewritten command plus the list of captured `_Heredoc`s (in the order
    opened). A single-pass character state machine — kept as one function because the quote/
    comment/substitution invariants only make sense read together; splitting it hides how they
    interact rather than clarifying it.

    `$( … )` command substitution and a backtick substitution BOTH start a FRESH parsing context
    in real shell grammar — quoting and heredocs inside either are independent of whatever quote
    state surrounds them (`"$(cat <<'EOF' … EOF)"` is valid: the inner heredoc parses normally
    even though the WHOLE substitution sits inside outer double quotes; without this, `<<` found
    while `in_double` is already True from the outer quote is never even recognized as a heredoc
    opener). So quote AND heredoc-eligibility state is tracked per a small stack of `_Frame`s, one
    pushed for each `$(` (popped at its matching `)`) or each opening backtick (popped at the next
    unescaped backtick), rather than one flat pair of booleans.

    A `#` at a WORD BOUNDARY, outside quotes, starts a shell comment running to the next literal
    newline — during which nothing (not `<<`, not `$(`/backtick, not a quote char) is special
    (review round 2: without this, `# <<EOF` on a comment line was misread as a REAL heredoc
    opener, and the neutralizer swallowed the next real, ticketless `git commit` line as if it
    were the heredoc's body — `commit_segments` then found no commit at all and silently ALLOWED
    it; a genuine gate bypass, not just a false block).

    Fail-safe: an unterminated heredoc (no matching terminator line found) is left AS-IS — the
    existing tokenizer's fail-open behavior on an unparseable command still applies afterward;
    this hook is on_error=open, a discipline reminder, not a security boundary."""
    out: list[str] = []
    heredocs: list[_Heredoc] = []
    stack = [_Frame(opened_by=None)]
    i, n = 0, len(command)
    in_comment = False
    prev_boundary = True
    while i < n:
        ch = command[i]
        if in_comment:
            out.append(ch)
            in_comment = ch != "\n"
            prev_boundary = ch == "\n"
            i += 1
            continue
        top = stack[-1]
        if ch == "\\" and not top.in_single and i + 1 < n:
            out.append(command[i : i + 2])
            i += 2
            prev_boundary = False
            continue
        if ch == "'" and not top.in_double:
            top.in_single = not top.in_single
        elif ch == '"' and not top.in_single:
            top.in_double = not top.in_double
        elif ch == "#" and prev_boundary and not top.in_single and not top.in_double:
            in_comment = True
            out.append(ch)
            i += 1
            prev_boundary = False
            continue
        elif not top.in_single and command.startswith("$(", i):
            stack.append(_Frame(opened_by="paren"))
            out.append("$(")
            i += 2
            prev_boundary = True  # the position right after `$(` is itself a boundary
            continue
        elif (
            ch == ")" and top.opened_by == "paren" and not top.in_single and not top.in_double
            and len(stack) > 1
        ):
            stack.pop()
        elif ch == "`" and not top.in_single:
            if top.opened_by == "backtick":
                stack.pop()
            else:
                stack.append(_Frame(opened_by="backtick"))
        elif (
            not top.in_single and not top.in_double
            and command.startswith("<<", i) and not command.startswith("<<<", i)
        ):
            resume_at = _try_consume_heredoc(command, i, out, heredocs)
            if resume_at is not None:
                i = resume_at
                prev_boundary = False  # a sentinel is never whitespace
                continue
        out.append(ch)
        prev_boundary = _is_word_boundary(ch)
        i += 1
    return "".join(out), heredocs


# ── commit-segment parser ─────────────────────────────────────────────────────────────────────
# The gate fires ONLY on a real `git commit` invocation. Detecting that means PARSING the command
# into argv — NOT raw-string matching "git"…"commit", which over-blocked every benign command that
# merely mentioned both words (`gh issue create --body "...git commit..."`, `echo "git commit"`,
# `git log --grep=commit` — agent-tools#97; the exact regression block-no-verify already fixed in
# #59). This parser is adapted from block-no-verify (the authoritative copy) and trimmed to the
# subset require-ticket needs: tokenize → split on separators → strip shell noise/redirects → peel
# inline env + wrapper executables (env/sudo/runuser/timeout/…) → keep a segment whose executable is
# `git` and subcommand is `commit`. Each hook is its own subprocess (no shared import path), so the
# logic is duplicated by design; keep it in step with block-no-verify when changing separator/quote/
# wrapper handling.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
# Leading tokens that introduce a command but are NOT the command (subshell/brace openers, control
# keywords). Stripping them recovers the real `git` so a `(git commit …)` / `then git commit …` is
# still seen.
_LEADING_SHELL_NOISE = frozenset({
    "(", "{", "!", "then", "do", "else", "elif", "while", "until", "for", "case",
})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# Wrapper executables that prefix the REAL command and pass the rest through. Best-effort, mirrors
# block-no-verify's set so a wrapped `git commit` is still gated. An UNLISTED wrapper (`unshare`,
# `nsenter`, `bash -c '…'`) is not peeled — a documented limitation; the gate is process discipline.
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer", "command",
    "sudo", "doas", "exec", "taskset", "chrt", "setpriv", "flock", "runuser",
})
_MAX_WRAPPER_NESTING = 16
# Wrappers that take a LEADING POSITIONAL operand before the command (timeout's duration, chrt's
# priority, taskset's mask, flock's lockfile) — dropped so the real command is reached.
_OPERAND_DROP_WRAPPERS = frozenset({"timeout", "taskset", "chrt", "flock"})
# Operand-drop wrappers whose operand is a MANDATORY arbitrary string that may legitimately be `git`
# (flock's lockfile) — dropped even when literally `git`.
_MANDATORY_OPERAND_WRAPPERS = frozenset({"flock"})
# Per-wrapper flags taking a SEPARATE value, so the NEXT token is the value, not the wrapped command
# (`sudo -u USER git …`, `timeout -s SIG …`). PER-WRAPPER: the same letter means different things to
# different wrappers (`-s` value for timeout, boolean for sudo). Mirrors block-no-verify.
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
# INVARIANTS (loud import-time failure, not a silent bypass): every operand-drop / value-flag /
# mandatory-operand wrapper must first be a recognized wrapper, else its config is dead and the
# wrapped commit slips (argv[0] != git → not gated).
if not _OPERAND_DROP_WRAPPERS <= _WRAPPERS:
    raise RuntimeError(f"operand-drop wrappers missing from _WRAPPERS: {_OPERAND_DROP_WRAPPERS - _WRAPPERS}")
if not _MANDATORY_OPERAND_WRAPPERS <= _OPERAND_DROP_WRAPPERS:
    raise RuntimeError(f"mandatory-operand wrappers not in operand-drop: {_MANDATORY_OPERAND_WRAPPERS - _OPERAND_DROP_WRAPPERS}")
if not set(_WRAPPER_VALUE_FLAGS) <= _WRAPPERS:
    raise RuntimeError(f"value-flag wrappers missing from _WRAPPERS: {set(_WRAPPER_VALUE_FLAGS) - _WRAPPERS}")


def _short_value_letters(value_flags: frozenset[str]) -> frozenset[str]:
    """The single-letter forms (`-u` → `u`) of a wrapper's value-flags, for the cluster check."""
    return frozenset(f[1] for f in value_flags if len(f) == 2 and f.startswith("-"))


_WRAPPER_SHORT_VALUE_LETTERS = {w: _short_value_letters(f) for w, f in _WRAPPER_VALUE_FLAGS.items()}


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/git` → `git`, `./sudo` → `sudo`."""
    return tok.rsplit("/", 1)[-1]


def _is_git_executable(tok: str) -> bool:
    """True when `tok` is the git binary — bare `git` or a path to it (`/usr/bin/git`, `./git`)."""
    return _basename(tok) == "git"


def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes (so a quoted `#42` in `-m 'fix #42'`
    is kept). A `#` starts a comment only at a WORD boundary outside quotes."""
    in_single = in_double = False
    prev_ws = True
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
    separators (`x;git`, `a&&git`). `punctuation_chars=True` emits `; & | && ||` as their own tokens
    while honoring quotes. Returns None on unbalanced quotes → the caller fails safe."""
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a NEWLINE
    is a command separator. A line that fails to tokenize (a quoted string spanning newlines) is
    re-joined with following lines until it balances. Returns None only if a chunk can never balance
    → the caller fails safe."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")  # honor backslash-newline line continuations
    lines = joined.split("\n")
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
    """Drop leading subshell/brace openers and control keywords so a command introduced by them is
    recovered: `(git commit …)` → `git commit …`. Capped so an all-noise segment can't loop."""
    i = 0
    while i < len(segment) and i < 16 and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_redirects(segment: list[str]) -> list[str]:
    """Drop shell redirection operators + their targets so they don't leak into argv. A pure `<>&`
    token is a redirect operator; a `<`/`>` inside a quoted word stays a normal token. After `--`
    everything is a literal pathspec (never a redirect)."""
    out: list[str] = []
    i = 0
    seen_ddash = False
    while i < len(segment):
        tok = segment[i]
        is_redir = (not seen_ddash and bool(tok) and tok not in _SHELL_SEP
                    and ("<" in tok or ">" in tok) and all(ch in "<>&" for ch in tok))
        if is_redir:
            if out and out[-1].isdigit():
                out.pop()  # drop a bare fd digit prefixing the redirect (`2` of `2> err`)
            i += 2 if i + 1 < len(segment) else 1
            continue
        if tok == "--":
            seen_ddash = True
        out.append(tok)
        i += 1
    return out


def _split_inline_env(segment: list[str]) -> tuple[dict[str, str], list[str]]:
    """Peel leading `VAR=value` assignments off a segment → (env, rest-starting-at-executable)."""
    env: dict[str, str] = {}
    i = 0
    while i < len(segment):
        m = _INLINE_ENV.match(segment[i])
        if not m:
            break
        env[m.group(1)] = m.group(2)
        i += 1
    return env, segment[i:]


def _cluster_takes_next_value(tok: str, short_letters: frozenset[str]) -> bool:
    """True when a short cluster's LAST char is a value-letter AND no earlier value-letter already
    consumed the cluster tail (`-iP` → True; `-Px` → False; `-iv` → False)."""
    body = tok[1:]
    if not body or body[-1] not in short_letters:
        return False
    for ch in body[:-1]:
        if ch in short_letters:
            return False
    return True


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    """Drop one wrapper's own option flags + (for an operand-drop wrapper) its leading positional
    operand, returning argv positioned at the wrapped command. A flag in this wrapper's value set
    (`sudo -u alice`) consumes the following token UNCONDITIONALLY — even when it is literally `git`
    (`sudo -u git git commit …`: the user is named "git", the SECOND git is the executable)."""
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    short_letters = _WRAPPER_SHORT_VALUE_LETTERS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        consumes_next = (argv[i] in value_flags
                         or (not argv[i].startswith("--")
                             and _cluster_takes_next_value(argv[i], short_letters)))
        if consumes_next and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        if wrapper in _MANDATORY_OPERAND_WRAPPERS or not _is_git_executable(argv[i]):
            i += 1
    return argv[i:]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading wrapper executables (`timeout 60`, `env A=b`, `sudo`, `runuser -u u --`, …) so the
    REAL command beneath is what we inspect. Stops as soon as the head is no longer a known wrapper,
    so a real `git` is never skipped. An `env -S '<command>'` split-string is NOT recursed here (a
    documented best-effort limitation; the gate is process discipline, not a security boundary). Past
    the nesting cap, returns argv unchanged (a pathological wrapper chain → simply not a commit we
    gate; require-ticket is on_error=open, so under-matching obfuscation is acceptable)."""
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            return argv
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        _, argv = _split_inline_env(argv)  # `env HUSKY=0 git …` / `sudo FOO=bar git …`
    return argv


def _commit_argv(segment: list[str]) -> list[str] | None:
    """If `segment`'s executable is `git` and its subcommand is exactly `commit`, return the tokens
    AFTER `commit`; else None. Walks past git GLOBAL options (`-C dir`, `-c k=v`) to reach the
    subcommand. Wrappers + inline env must already be peeled. `commit-graph`/`commit-tree` are
    DIFFERENT subcommands and correctly do NOT match (exact `== "commit"`)."""
    if not segment or not _is_git_executable(segment[0]):
        return None
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2  # global flag + its separate value
            continue
        if tok.startswith("-"):
            i += 1  # other global flag / glued `-Cdir` / `-ck=v`
            continue
        break
    if i >= len(segment) or segment[i] != "commit":
        return None
    return segment[i + 1:]


class CommitSegment:
    """A parsed real `git commit` segment.

    - ``env``      : the inline `VAR=value` env prefixing the command (and a wrapper's `VAR=val`
                     operands), scoped to THIS segment — kept for parity with the wrapper parser
                     (no skip decision reads it any more; the per-commit skip env was removed).
    - ``argv``     : the tokens AFTER `commit` — the commit's own flags/message/pathspecs.
    - ``git_argv`` : the wrapper-stripped invocation starting at `git` (`git -C <dir> commit …`) —
                     for reading the `git -C <dir>` global that targets a different repo.
    - ``index``    : this segment's position among ALL simple-command segments in the parsed
                     command (not only the `git commit` ones) — the SAME enumeration
                     `_assign_heredoc_segment_indices` numbers a heredoc's own segment from, so a
                     `-F <path>` can be checked against a heredoc that (textually) precedes it
                     without an independent, more fragile text search (agent-tools#570 review).
    """

    __slots__ = ("env", "argv", "git_argv", "index")

    def __init__(self, env: dict[str, str], argv: list[str], git_argv: list[str], index: int) -> None:
        self.env = env
        self.argv = argv
        self.git_argv = git_argv
        self.index = index


def commit_segments(command: str) -> list[CommitSegment]:
    """Every real `git commit` segment in `command` (a chain may hold more than one). Empty on a
    tokenization failure (the safe direction: a command we can't parse is not gated — require-ticket
    is on_error=open, a discipline reminder, not a security boundary)."""
    tokens = _tokenize(command)
    if tokens is None:
        return []
    out: list[CommitSegment] = []
    for index, raw_seg in enumerate(_segments(tokens)):
        seg = _strip_leading_shell_noise(_strip_redirects(raw_seg))
        env, rest = _split_inline_env(seg)
        git_argv = _strip_wrappers(rest)  # the real `git …` invocation, wrappers peeled
        argv = _commit_argv(git_argv)
        if argv is not None:
            out.append(CommitSegment(env, argv, git_argv, index))
    return out


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"require-ticket: {msg}\n")


def has_ticket_reference(text: str) -> bool:
    return any(p.search(text) for p in TICKET_PATTERNS)


def is_exempt(message: str) -> bool:
    """A commit type / marker that doesn't need a ticket (trivial chore, WIP, merge).

    Judged from the first non-empty line of the message (the subject), so a
    `test:`-looking sentence in the body can't accidentally exempt a real change.
    """
    subject = next((ln for ln in message.splitlines() if ln.strip()), "")
    if EXEMPT_MARKERS.search(subject):
        return True
    m = CONVENTIONAL.match(subject)
    return bool(m and m.group(1).lower() in EXEMPT_TYPES)


# The `-F -` sentinel: git reads the commit message from its OWN stdin. A PreToolUse hook cannot
# read that (the hook's stdin is the JSON event, not git's), so a `-F -` value is NOT a file path to
# open — it's the unreadable-message marker that triggers the fail-CLOSED-with-hint block in
# _evaluate_commit_segment (agent-tools#104; #102 used to fail open here).
_STDIN_SENTINEL = "-"

# The short option letters of `git commit` that carry a message/file VALUE: `-m` (literal message)
# and `-F` (message file). A CLUSTERED short group (`-am`, `-aF`, `-amMSG`) must be de-clustered the
# way git does (agent-tools#109; the same clustered-short-flag class block-no-verify fixed in
# #36–#40): git reads a short cluster LEFT-TO-RIGHT and the FIRST value-consuming letter wins — it
# consumes the REST OF THE CLUSTER as its glued value if any chars follow it (`-amMSG` → `-m MSG`;
# `-amF` → `-m F`, NOT a separate `-F`; `-aFpath` → `-F path`), else (it is the cluster's last char)
# it consumes the NEXT token (`-am MSG`, `-aF file`).
_MESSAGE_VALUE_LETTERS = frozenset("mF")
# But `m`/`F` are NOT the only value-consuming short letters of `git commit`. `-C`/`-c` (reuse/reedit
# a commit), `-t` (template file), `-u` (untracked-files mode) and `-S` (optional gpg keyid) ALSO
# greedily take the rest-of-cluster (or next token) as THEIR value — so a `m`/`F` AFTER one of them is
# part of that value, not a message flag: git parses `-Cm` as `-C m` (reuse commit "m"), `-um` as the
# untracked mode "m", `-Sm` as keyid "m" — none of them a `-m` message. The de-cluster scan must
# therefore stop at the FIRST value-consuming letter of the WHOLE set and only treat it as a message
# when that letter is `m`/`F`. (Mirrors block-no-verify's `_SHORT_VALUE_LETTERS` = "mFCct" + `-S`
# optional; kept separate by design — each hook is its own subprocess with no shared import path.)
_OTHER_VALUE_LETTERS = frozenset("CctuS")
_ALL_VALUE_LETTERS = _MESSAGE_VALUE_LETTERS | _OTHER_VALUE_LETTERS


def _decluster_short_message_flag(tok: str) -> tuple[str, str | None] | None:
    """De-cluster a `-`-prefixed short option group the way git's commit parser does, for message
    extraction.

    Returns ``None`` when `tok` is not a single-dash short cluster whose FIRST value-consuming letter
    is the message/file flag `-m`/`-F` — i.e. a long `--…` flag, a positional, a bare `-`, a cluster
    with no value letter (`-a`/`-sv`), or a cluster whose first value letter is a NON-message one
    (`-Cm`/`-um`/`-Sm`: git reads `m` as the value of `-C`/`-u`/`-S`, not a `-m` message).

    Otherwise returns ``(letter, value)`` where ``letter`` is that first value letter (`"m"`/`"F"`):

      - ``value`` is the GLUED value string when chars follow that letter in the cluster
        (`-amMSG` → ``("m", "MSG")``; `-amF` → ``("m", "F")``; `-aFpath` → ``("F", "path")``);
      - ``value`` is ``None`` when the value letter is the cluster's LAST char, meaning the value is
        the NEXT argv token (`-am` → ``("m", None)``; `-aF` → ``("F", None)``).

    A boolean letter BEFORE the first value letter (`-a`/`-s`/`-v`/`-q`/…) is skipped. The scan STOPS
    at the first value-consuming letter of the whole set — git does not re-parse a value tail as
    further flags (`-amF` is message ``"F"``, never `-a -m -F`)."""
    if not tok.startswith("-") or tok.startswith("--") or len(tok) < 2:
        return None  # a long flag, a bare `-`, or a non-flag positional
    body = tok[1:]
    for idx, ch in enumerate(body):
        if ch in _ALL_VALUE_LETTERS:
            if ch not in _MESSAGE_VALUE_LETTERS:
                return None  # `-C`/`-c`/`-t`/`-u`/`-S` swallow the rest — not a `-m`/`-F` message
            glued = body[idx + 1:]
            return ch, (glued if glued else None)
    return None  # no value letter in the cluster — a pure boolean group like `-a`/`-sv`


# Non-message commit flags whose value is a MANDATORY SEPARATE token: `-C`/`-c` (reuse/reedit a
# commit), `-t` (template file), and their long spellings. When that value happens to look like `-m`/
# `-F`/`-F -` it must NOT be re-read as a real message/file flag (`git commit -t -F -`: `-t` takes
# `-F` as the template path, `-` is a pathspec, stdin is NOT read; `git commit -C -m x`: `-C` takes
# `-m` as the reuse-ref, the message is NOT "x"). Both message parsers SKIP this flag AND its value
# token so they stay in lock-step (agent-tools#109 review). `-u`/`-S` are OMITTED on purpose: their
# value is OPTIONAL and only ever GLUED (`-uno`, `-Skeyid`) — a separate `-u -m …`/`-S -m …` is `-u`
# with no value then a real `-m`, so they must NOT consume the next token (it would eat the message).
_OTHER_VALUE_SHORT_NEXT = frozenset("Cct")
# The LONG `git commit` options whose value is a MANDATORY SEPARATE next token (git-parseopt
# consumes that token even when it starts with `-`). Two groups, both treated identically — skip the
# flag AND its value so a `-m`/`-F`-looking value is never re-read as a real message/file flag:
#   - reuse/reedit/fixup/squash/template  : the commit-reference / template-file family.
#   - author/date/cleanup/trailer/pathspec-from-file (agent-tools#114): each also takes a mandatory
#     next-token value. They were MISSING before, which let `git commit --author '-m' 'ABC-1: ok'`
#     (real git: author="-m", "ABC-1: ok" is a PATHSPEC, ticketless) be misparsed as a `-m` message
#     carrying a ticket → a TICKET-GATE BYPASS. A GLUED `--author=…`/`--cleanup=…` carries its own
#     value and does NOT consume the next token (handled below by the `tok in _OTHER_VALUE_LONG`
#     exact-match: a `--author=x` token simply isn't in the set). `--gpg-sign` is OMITTED on purpose:
#     its value is OPTIONAL and only ever GLUED (`--gpg-sign=keyid`), so a separate `--gpg-sign -m …`
#     is a bare flag then a real `-m` — it must NOT consume the next token.
_OTHER_VALUE_LONG = frozenset({
    "--reuse-message", "--reedit-message", "--fixup", "--squash", "--template",
    "--author", "--date", "--cleanup", "--trailer", "--pathspec-from-file",
})

# The MESSAGE/FILE value-consuming long options — `--message`/`--file`. Their value text IS the
# commit message (read), not skipped. Kept separate from _OTHER_VALUE_LONG (whose value is skipped).
_MESSAGE_VALUE_LONG = frozenset({"--message", "--file"})

# git resolves any UNAMBIGUOUS PREFIX of a long option (`--au` → `--author`, `--clea` → `--cleanup`,
# `--reu` → `--reuse-message`), and REJECTS an ambiguous prefix that matches >1 option (`--t` could be
# --trailer or --template; `--d` could be --date or --dry-run; `--f` could be --file or --fixup). The
# exact-match sets above miss the abbreviation, so a value-bearing flag in abbreviated form was not
# recognized as value-consuming → its value de-clustered into a `-m` message and ate the next token →
# the same value-misattribution / TICKET-GATE BYPASS #114 closed for the exact spelling came back via
# the short form (agent-tools#120). The fix: resolve a `--xxx` token to its canonical option the way
# git does, then test the CANONICAL name against the value-consuming sets.
#
# Ambiguity is computed against the FULL `git commit` long-option set (NOT only the value-consuming
# ones): a prefix unique among value-consumers but ALSO a prefix of a non-value-consumer is ambiguous
# to git (git errors, the commit never runs), so it must NOT be treated as the value-consuming flag.
# This list is the canonical `git commit` long options (from `git commit -h` / the completion helper) —
# including the two first-class `--no-…` options it actually lists (`--no-post-rewrite`, `--no-verify`).
# The AUTO-negations git derives for every boolean (`--no-edit`, `--no-signoff`, `--no-all`, …) are
# omitted on purpose — none is value-consuming, they all start with `--no-` so they can't be a prefix
# of a positive value-consumer (`--au`/`--mess`/…), and the value-consumers we gate on have no boolean
# auto-negation that could shadow them. So leaving them out cannot turn a real value-consumer
# abbreviation into a (false) ambiguity. A consequence (safe, see _resolve_long_option's docstring): a
# prefix this set resolves uniquely but FULL git would call ambiguous gets value-consumed here while
# git would error — harmless, since git then never commits.
_ALL_COMMIT_LONG_OPTIONS = frozenset({
    "--ahead-behind", "--all", "--allow-empty", "--allow-empty-message", "--amend", "--author",
    "--branch", "--cleanup", "--date", "--dry-run", "--edit", "--file", "--fixup", "--gpg-sign",
    "--include", "--interactive", "--long", "--message", "--no-post-rewrite", "--no-verify",
    "--null", "--only", "--patch", "--pathspec-file-nul", "--pathspec-from-file", "--porcelain",
    "--quiet", "--reedit-message", "--reset-author", "--reuse-message", "--short", "--signoff",
    "--squash", "--status", "--template", "--trailer", "--untracked-files", "--verbose",
})
# Sanity: every value-consuming long option we gate on must be a real `git commit` option (else a
# typo in the set would silently never resolve). Loud at import, not a silent bypass.
_KNOWN_VALUE_LONG = _OTHER_VALUE_LONG | _MESSAGE_VALUE_LONG
if not _KNOWN_VALUE_LONG <= _ALL_COMMIT_LONG_OPTIONS:
    raise RuntimeError(
        f"value-consuming long flags not in the git commit option set: "
        f"{_KNOWN_VALUE_LONG - _ALL_COMMIT_LONG_OPTIONS}"
    )


def _resolve_long_option(name: str) -> str | None:
    """Resolve a long-option NAME (a `--xxx` token with any `=value` already stripped) to its canonical
    `git commit` option the way git's parseopt does: an EXACT match wins; otherwise an UNAMBIGUOUS
    prefix (a prefix of exactly one option) resolves to that option; an AMBIGUOUS prefix (matches >1)
    or an UNKNOWN token resolves to None. `--` and a bare `--x` shorter than `--` are None.

    Returning None for an ambiguous prefix is correct for the gate: real git ERRORS on it and never
    commits, so the parser must not over-consume by guessing one of the candidates.

    Not a byte-exact git simulation: because the set omits git's auto-`--no-…` negations, a prefix
    this resolver finds unique MIGHT be ambiguous to full git (an omitted option shares the prefix).
    That direction is safe — full git then errors and never commits, so an over-consumed value here
    just gates a command that wasn't going to run anyway; it can never manufacture a phantom ticket."""
    if not name.startswith("--") or len(name) <= 2:
        return None
    if name in _ALL_COMMIT_LONG_OPTIONS:
        return name  # exact match (also the canonical fast path)
    matches = [opt for opt in _ALL_COMMIT_LONG_OPTIONS if opt.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _canonical_long_value_flag(tok: str) -> str | None:
    """For a long-option token `--xxx` (NO glued `=value`), return its canonical name IF it resolves
    (exactly or via an unambiguous prefix) to a value-CONSUMING long option whose value is a mandatory
    SEPARATE next token — `--message`/`--file` or one of `_OTHER_VALUE_LONG`. Else None.

    A glued `--author=…`/`--message=…` carries its own value and never consumes the next token, so a
    token containing `=` resolves to None here (the caller handles the glued form separately)."""
    if not tok.startswith("--") or "=" in tok:
        return None
    canonical = _resolve_long_option(tok)
    return canonical if canonical in _KNOWN_VALUE_LONG else None


def _glued_message_long_flag(tok: str) -> tuple[str, str] | None:
    """For a GLUED long-option token `--xxx=value` (`--message=…`/`--file=…` and their unambiguous
    abbreviations `--mess=…`/`--fil=…`), return ``(canonical, value)`` where `canonical` is
    `--message` or `--file`; else None. The flag NAME (left of the first `=`) is resolved the way git
    does, so `--mess=Closes #1` reads as a `--message` and `--fil=path` as a `--file`."""
    if not tok.startswith("--") or "=" not in tok:
        return None
    name, value = tok.split("=", 1)
    canonical = _resolve_long_option(name)
    return (canonical, value) if canonical in _MESSAGE_VALUE_LONG else None


def _nonmessage_flag_consumes_next(tok: str) -> bool:
    """True when `tok` is a commit flag whose value is a MANDATORY SEPARATE next token but is NOT a
    `-m`/`-F` message/file flag — so both message parsers must SKIP it and its value, never re-read a
    `-m`/`-F`-looking value as a real flag. Covers separate long `--reuse-message`/`--template`/… —
    INCLUDING git's unambiguous abbreviations (`--au` → `--author`, `--clea` → `--cleanup`; an
    ambiguous prefix like `--t`/`--d` resolves to None and is NOT consumed, matching git's reject) —
    and a short cluster whose first value letter is `C`/`c`/`t` and is the cluster's LAST char (`-C`/
    `-aC` take the next token; a GLUED `-Cabc`/`-aCabc` carries its value inside the token, so it does
    NOT consume the next)."""
    if tok.startswith("--"):
        # a glued `--template=x` carries its own value → None here; an abbreviated `--templ` resolves.
        return _canonical_long_value_flag(tok) in _OTHER_VALUE_LONG
    if not tok.startswith("-") or len(tok) < 2:
        return False
    body = tok[1:]
    for idx, ch in enumerate(body):
        if ch in _ALL_VALUE_LETTERS:
            # the first value letter is C/c/t AND it is the cluster's last char → next-token value.
            return ch in _OTHER_VALUE_SHORT_NEXT and idx == len(body) - 1
    return False


# A heredoc-sourced `-m "$(cat <<'EOF' … EOF)"` reduces, after `_neutralize_heredocs` has run,
# to EXACTLY `$(cat SENTINEL)` (or the backtick spelling `` `cat SENTINEL` ``) — recognized here
# and unwrapped back to the heredoc's captured body (agent-tools#503/#406/#430).
_CAT_SUBST_RE = re.compile(r"^\$\(\s*cat\s+(\S+)\s*\)$")
_BACKTICK_CAT_SUBST_RE = re.compile(r"^`\s*cat\s+(\S+)\s*`$")

# Unquoted heredoc bodies are expanded by bash before any consumer sees them — a literal `${UNSET:+#999}`
# in the source is NOT a real `#999` reference in the final message. Strip simple parameter/command
# substitution shapes before ticket detection on an unquoted body (quoted bodies are literal).
_SHELL_PARAM_EXPANSION = re.compile(r"\$\{[^}]*\}")
_SHELL_SIMPLE_PARAM = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


def _effective_heredoc_body(body: str, quoted_delimiter: bool) -> str:
    if quoted_delimiter:
        return body
    body = _SHELL_PARAM_EXPANSION.sub("", body)
    return _SHELL_SIMPLE_PARAM.sub("", body)


def _resolve_message_value(value: str, heredocs: list[_Heredoc]) -> str:
    """Turn a raw `-m`/`--message` VALUE into the real message text after heredoc neutralization.
    The documented multi-line pattern reduces to EXACTLY `$(cat SENTINEL)` — unwrap that to the
    heredoc's captured body, the same message text an equivalent single-line `-m` would carry. Any
    OTHER shape (the sentinel wrapped in some other command, e.g. `$(some-other-cmd SENTINEL)`)
    falls back to a plain substring substitution of the sentinel for its body — not byte-exact, but
    `has_ticket_reference`'s substring search still finds a real reference inside the body, which
    is the property that actually matters for the gate."""
    if not heredocs:
        return value
    m = _CAT_SUBST_RE.match(value) or _BACKTICK_CAT_SUBST_RE.match(value)
    if m is not None:
        for hd in heredocs:
            if hd.sentinel == m.group(1):
                return _effective_heredoc_body(hd.body, hd.quoted_delimiter)
    for hd in heredocs:
        if hd.sentinel in value:
            value = value.replace(hd.sentinel, _effective_heredoc_body(hd.body, hd.quoted_delimiter))
    return value


def _resolve_path(path: str, cwd: str | None) -> str:
    """Expand `~` and resolve a relative `path` against `cwd` — shared by `_read_message_file`'s
    disk read and `_heredoc_body_for_target` below, so the two always agree on what a path means."""
    resolved = os.path.expanduser(path)
    if cwd and not os.path.isabs(resolved):
        resolved = os.path.join(cwd, resolved)
    return os.path.normpath(resolved)


def _assign_heredoc_segment_indices(parse_command: str, heredocs: list[_Heredoc]) -> None:
    """Set each heredoc's `segment_index` — the index of the SIMPLE COMMAND segment (among ALL
    segments in `parse_command`, the same enumeration `commit_segments` numbers its own
    `CommitSegment.index` from) whose tokens contain its sentinel. Re-tokenizes `parse_command`
    with the SAME real segment parser `commit_segments` uses, rather than an independent text
    search — a search for a bare word like "commit" could match unrelated prose (`echo commit`)
    or a comment BEFORE the real commit segment and mis-anchor the ordering check in
    `_heredoc_body_for_target` (agent-tools#570 review round 2). A sentinel not found in any
    segment (should not happen — every sentinel `_neutralize_heredocs` emits lands in this SAME
    text) is left at its default `None`, the permissive fallback (`_heredoc_body_for_target`
    treats an unknown segment as always eligible rather than silently excluding it)."""
    tokens = _tokenize(parse_command)
    if tokens is None:
        return
    for index, seg in enumerate(_segments(tokens)):
        for hd in heredocs:
            if hd.segment_index is None and any(hd.sentinel in tok for tok in seg):
                hd.segment_index = index


def _heredoc_body_for_target(
    path: str, cwd: str | None, heredocs: list[_Heredoc], consuming_segment_index: int | None = None,
) -> str | None:
    """The body of a heredoc-neutralized `cat > PATH <<EOF … EOF` found EARLIER in the SAME
    command, when `path` (a `-F`/`--file` VALUE) resolves to that same file — the fix for
    agent-tools#570: at PreToolUse hook time the shell has not run yet, so the file does not exist
    on disk and a normal read always fails for this shape; reading the already-captured heredoc
    body instead sidesteps the timing race entirely.

    `consuming_segment_index` (the `-F`-owning commit's own `CommitSegment.index`) restricts
    matches to a heredoc whose `segment_index` is STRICTLY LESS — i.e. one whose OWN simple-command
    segment (textually) precedes the commit reading the file. Without this a heredoc written AFTER
    the commit — `git commit -F msg; cat > msg <<'EOF'\nRefs #123\nEOF` — could retroactively
    "satisfy" a commit that, when it actually ran, read whatever `msg` held BEFORE that later write
    (a real gate bypass found in review). A heredoc with `segment_index=None` (not resolved by
    `_assign_heredoc_segment_indices`, or `consuming_segment_index=None` for a caller with no
    meaningful commit position) is treated as always eligible — permissive, not exclusionary, on
    the unexpected case."""
    if not heredocs:
        return None
    target_path = _resolve_path(path, cwd)
    matched: str | None = None
    for hd in heredocs:
        if not hd.quoted_delimiter:
            continue
        if (
            consuming_segment_index is not None
            and hd.segment_index is not None
            and hd.segment_index >= consuming_segment_index
        ):
            continue
        if hd.target is not None and _resolve_path(hd.target, cwd) == target_path:
            matched = hd.body  # keep scanning — the LAST matching write before the commit wins
    return matched


def commit_message_from_argv(
    argv: list[str],
    cwd: str | None = None,
    heredocs: list[_Heredoc] | None = None,
    consuming_segment_index: int | None = None,
) -> str:
    """Pull the commit message out of the already-parsed commit argv: -m/--message and -F/--file.

    Returns ONLY the message text — the concatenation of every -m value and the contents of any -F
    file — so the conventional-type / WIP exemption check sees the real subject line, not the `git
    commit …` argv. A relative `-F` path is resolved against `cwd` (the command's working directory
    from the event), since the hook may run elsewhere. A `-F -` (stdin) value contributes NO text —
    git streams that message on its own stdin, which the hook can't read; commit_reads_stdin_message
    detects it separately so the caller can fail closed with a hint. Stops at `--` (the rest are
    literal pathspecs, never message flags).

    `heredocs` (from `_neutralize_heredocs`, run once per command in `main`) lets a `-m` value that
    reduces to `$(cat SENTINEL)` recover the real heredoc body (agent-tools#503/#406/#430), and lets
    a `-F <path>` recover the body of a heredoc that wrote that same path EARLIER in the command,
    before the disk file exists (agent-tools#570) — `consuming_segment_index` (this segment's own
    `CommitSegment.index`; see `_heredoc_body_for_target`) is what enforces "earlier", so a heredoc
    that writes the path only AFTER this commit can't retroactively satisfy it."""
    heredocs = heredocs or []
    parts: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        # Long forms first: `--message`/`--file` (separate value) and `--message=`/`--file=` (glued),
        # each resolved through git's unambiguous-prefix rule (`--mess` → `--message`, `--fil=…` →
        # `--file=…`) so an abbreviated message/file flag still has its value read (agent-tools#120).
        glued = _glued_message_long_flag(tok)
        if glued is not None:
            canonical, value = glued
            if canonical == "--file":
                parts.append(_read_message_file(value, cwd, heredocs, consuming_segment_index))
            else:
                parts.append(_resolve_message_value(value, heredocs))
            i += 1
            continue
        canonical_sep = _canonical_long_value_flag(tok)
        if canonical_sep in _MESSAGE_VALUE_LONG and i + 1 < len(argv):
            value = argv[i + 1]
            if canonical_sep == "--file":
                parts.append(_read_message_file(value, cwd, heredocs, consuming_segment_index))
            else:
                parts.append(_resolve_message_value(value, heredocs))
            i += 2
            continue
        # Short forms, de-clustered like git: `-m`/`-F`/`-am`/`-aF` (next-token value) and
        # `-mMSG`/`-FPATH`/`-amMSG`/`-aFpath`/`-amF` (glued value) (agent-tools#109).
        declustered = _decluster_short_message_flag(tok)
        if declustered is not None:
            letter, glued = declustered
            if glued is None:  # next-token value (`-am MSG`/`-aF file`)
                if i + 1 >= len(argv):
                    break  # a trailing `-am`/`-aF` with no value — git would error; nothing to read
                value, i = argv[i + 1], i + 2
            else:  # glued value (`-amMSG`/`-aFpath`/`-amF`)
                value, i = glued, i + 1
            if letter == "F":
                parts.append(_read_message_file(value, cwd, heredocs, consuming_segment_index))
            else:
                parts.append(_resolve_message_value(value, heredocs))
            continue
        # A non-message mandatory-value flag (`-C`/`-c`/`-t`/`--reuse-message`/…): SKIP it AND its
        # value token, so a `-m`/`-F`-looking value (`-C -m x`) is not misread as a real message
        # (agent-tools#109 review). A trailing such flag with no value → git would error; stop.
        if _nonmessage_flag_consumes_next(tok):
            if i + 1 >= len(argv):
                break
            i += 2
            continue
        i += 1
    return "\n".join(parts)


def _file_flag_values(argv: list[str]) -> list[str]:
    """Every `-F`/`--file` VALUE in the parsed commit argv — separate `-F <p>`/`--file <p>`, glued
    `-F<p>`, and `--file=<p>` — in order. Stops at `--` (literal pathspecs, never message flags).

    Recognizes the SAME four spellings `commit_message_from_argv` reads from disk, so the
    stdin-sentinel detector and the message reader agree on what a `-F`/`--file` value is (a value
    that reads as a file in one MUST be seen as a file in the other, and vice versa for `-`). The two
    keep separate loops because the reader interleaves `-m` values and file CONTENTS while this one
    only collects the file VALUES; a parity test pins that they don't drift."""
    values: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        # Long `--file`/`--message` resolved through git's unambiguous-prefix rule (`--fil`/`--mess`),
        # matching commit_message_from_argv so the two stay in lock-step (agent-tools#120).
        glued = _glued_message_long_flag(tok)
        if glued is not None:
            canonical, value = glued
            if canonical == "--file":
                values.append(value)  # `--file=path`/`--fil=path` — a file VALUE
            i += 1  # `--message=…`/`--mess=…` carries its value glued; nothing to collect
            continue
        canonical_sep = _canonical_long_value_flag(tok)
        if canonical_sep == "--file" and i + 1 < len(argv):
            values.append(argv[i + 1])
            i += 2
            continue
        # A separate `--message <v>` value is collected by commit_message_from_argv; here it must be
        # SKIPPED (value + flag), or a `--message -F -` would re-parse the `-F` as a real file flag and
        # falsely flag stdin (agent-tools#109 review). `--message=` carries its value glued — no skip.
        if canonical_sep == "--message" and i + 1 < len(argv):
            i += 2
            continue
        # Short forms, de-clustered like git. A cluster's governing value letter is either `F` (a
        # message-FILE value → collect it) or `m` (a literal MESSAGE → NOT a file). Crucially, BOTH
        # must consume their value token the same way commit_message_from_argv does, so a `-m`/`-am`
        # whose VALUE happens to look like `-F` (`git commit -am -F -`: message is "-F", `-` is a
        # pathspec, stdin is NOT read) does not leave that `-F` to be re-parsed here as a real file
        # flag (agent-tools#109; keeps the two parsers in lock-step — the parity test pins it).
        declustered = _decluster_short_message_flag(tok)
        if declustered is not None:
            letter, glued = declustered
            if glued is None:  # value is the NEXT token (`-aF file`, `-F -`, `-am MSG`)
                if i + 1 >= len(argv):
                    break  # a trailing `-aF`/`-am` with no value — git would error
                if letter == "F":
                    values.append(argv[i + 1])
                i += 2  # skip the consumed value token for BOTH `F` and `m`
                continue
            if letter == "F":
                values.append(glued)  # glued file path (`-aFpath`/`-FPATH`)
            i += 1
            continue
        # A non-message mandatory-value flag (`-C`/`-c`/`-t`/`--reuse-message`/…): skip it AND its
        # value, so a `-F`-looking value (`-t -F -`) is not re-read here as a real file flag.
        if _nonmessage_flag_consumes_next(tok):
            if i + 1 >= len(argv):
                break
            i += 2
            continue
        i += 1
    return values


def commit_reads_stdin_message(argv: list[str]) -> bool:
    """True when the commit's message comes from git's STDIN via `-F -` (any spelling: `-F -`,
    `-F-`, `--file -`, `--file=-`). That message is on git's own stdin, which a PreToolUse hook
    cannot read — so the caller fails CLOSED with a hint (agent-tools#104), rather than let `-F -`
    silently dodge the ticket gate. The readable per-commit escapes are honored first, so a genuine
    no-ticket `-F -` commit still has an out."""
    return any(v == _STDIN_SENTINEL for v in _file_flag_values(argv))


def _read_message_file(
    path: str,
    cwd: str | None = None,
    heredocs: list[_Heredoc] | None = None,
    consuming_segment_index: int | None = None,
) -> str:
    # `-F -` is the stdin sentinel, not a file named "-": don't try to open it (it would fail and
    # warn spuriously). commit_reads_stdin_message handles that case at the decision layer.
    if path == _STDIN_SENTINEL:
        return ""
    # A heredoc EARLIER in the same command already wrote this exact path (`cat > path <<EOF …
    # EOF`) — read the captured body instead of the disk: at PreToolUse hook time the shell has
    # not run yet, so the file does not exist (or may hold stale content) and a disk read would
    # wrongly fail-block a correctly-ticketed commit (agent-tools#570). `consuming_segment_index` rejects a
    # heredoc that (textually) comes AFTER this commit — see `_heredoc_body_for_target`.
    heredoc_body = _heredoc_body_for_target(path, cwd, heredocs or [], consuming_segment_index)
    if heredoc_body is not None:
        return heredoc_body
    resolved = _resolve_path(path, cwd)
    try:
        with open(resolved, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        warn(f"could not read commit-message file {resolved}: {exc}")
        return ""


def current_branch(cwd: str | None) -> str:
    """Branch name — a ticket id is often encoded there (feature/ABC-12-foo).

    Uses ``git branch --show-current`` (git >=2.22), which returns the branch name even on an
    UNBORN branch — the exact case of a repo's FIRST commit, where ``git rev-parse --abbrev-ref
    HEAD`` returns the literal "HEAD" instead and the branch-encoded ticket would be missed."""
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"could not read branch: {exc}")
        return ""


def effective_cwd(segment: CommitSegment, cwd: str | None) -> str | None:
    """Honor ``git -C <dir>``: a `git -C <repo> commit …` acts on <repo>, not the shell cwd, so
    branch detection must read THAT repo. Returns the -C target (``-C dir`` or ``-Cdir``, last one
    wins, resolved against cwd) read from the PARSED commit segment if present, else cwd."""
    tokens = segment.git_argv  # the wrapper-stripped `git …` invocation
    target: str | None = None
    i = 1 if tokens and _is_git_executable(tokens[0]) else 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "commit":
            break  # past the global options — stop before the subcommand argv
        if tok == "-C" and i + 1 < len(tokens):
            target = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            target = tok[2:]
        i += 1
    if target is None:
        return cwd
    target = os.path.expanduser(target)
    if cwd and not os.path.isabs(target):
        target = os.path.join(cwd, target)
    return target


def _takes_following_message_value(tok: str) -> bool:
    """True when a commit flag token consumes the NEXT token as a VALUE — so a skip-flag-looking
    value (`-am '--amend'`, `--author '--amend'`) is NOT misread as a real flag by `is_skip_commit`.

    Covers BOTH families that take a mandatory separate next token, so the value is always skipped:
      - the MESSAGE/FILE flags `--message`/`--file` and a de-clustered short group whose first value
        letter (`m`/`F`) is the cluster's LAST char (`-m`, `-am`, `-aF`); and
      - the NON-message value-consumers via the shared `_nonmessage_flag_consumes_next` — the
        separate long `--reuse-message`/`--author`/`--cleanup`/… and the short `-C`/`-c`/`-t` whose
        value is a next token (agent-tools#114: this used to skip ONLY message values, an asymmetry
        vs the other two parsers — a `--author '--amend'` value could be misread as a real `--amend`).
    All long forms are resolved through git's unambiguous-prefix rule via `_canonical_long_value_flag`
    (`--au '--amend'` → `--author` consumes `'--amend'`; agent-tools#120), so an abbreviated
    value-bearing flag skips its value here too. A GLUED value (`-mMSG`/`-FPATH`/`-amMSG`/`-amF`/
    `-aFpath`/`--author=…`) carries its value inside the token and does NOT take the next
    (agent-tools#109). The de-clustering matches git exactly via the shared
    `_decluster_short_message_flag`, so a value letter in the MIDDLE of a cluster (`-amF` → message
    "F"; `-aFm` → file "m") is read as glued, not as a next-token consumer."""
    if tok.startswith("--"):
        # `_canonical_long_value_flag` covers BOTH the message/file and the other value-consumers,
        # exact spelling AND unambiguous abbreviation, and returns None for a glued `--xxx=value`.
        return _canonical_long_value_flag(tok) is not None
    # A message/file token is judged ENTIRELY by the de-cluster result: it takes the next token only
    # when its value is the cluster's LAST char (glued value → does NOT). Only a NON-message token
    # falls through to _nonmessage_flag_consumes_next, preserving that function's call invariant (it
    # is never handed an `m`/`F`-led token) — so a glued message ending in `c`/`C`/`t` (`-amFixIt`)
    # can never be misread as a next-token-consuming `-C`/`-c`/`-t` (review of #114).
    declustered = _decluster_short_message_flag(tok)
    if declustered is not None:
        return declustered[1] is None
    return _nonmessage_flag_consumes_next(tok)


def _is_skip_flag(tok: str) -> bool:
    """True when `tok` is one of the SKIP_FLAGS — by exact spelling OR git's unambiguous abbreviation
    (`--am` → `--amend`; agent-tools#120). Of the four, only `--amend` is a real `git commit` option
    so only it is reachable by prefix (the merge/rebase plumbing `--continue/--abort/--skip` aren't
    `git commit` options — they match by exact string only, which `_resolve_long_option` would not
    resolve, hence the explicit exact-membership fallback)."""
    if tok in SKIP_FLAGS:
        return True
    return _resolve_long_option(tok) in SKIP_FLAGS


def is_skip_commit(argv: list[str]) -> bool:
    """True when the PARSED commit argv carries --continue/--abort/--skip/--amend (merge/rebase
    plumbing or an amend — not authoring a fresh change), in any spelling git accepts including an
    unambiguous abbreviation (`--am` → `--amend`). Skips every message/file VALUE so a skip flag named
    only in the commit MESSAGE (`-m 'support --amend'`, `-am '--amend'`) does NOT falsely exempt a real
    commit. Stops at `--` (the rest are literal pathspecs, never flags)."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        if _takes_following_message_value(tok) and i + 1 < len(argv):
            i += 2  # drop the flag AND its value (a `--amend`-looking message can't be read as one)
            continue
        if _is_skip_flag(tok):
            return True
        i += 1
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # on_error=open → allow on inability to inspect; just warn.
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        return _allow()

    args = event.get("args") or {}
    _CURRENT_EVENT["event"] = event
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    event_cwd = event.get("cwd") or args.get("cwd")

    # Neutralize every heredoc BEFORE any shlex-based parsing sees the command text (agent-tools#503/
    # #406/#430/#570 — see the "heredoc neutralization" section above for why raw heredoc bodies
    # corrupt the tokenizer). `parse_command` is the heredoc-free text used for segment/argv parsing;
    # `command` (the original, unmodified) is kept for the human-facing hatch/tg-ctl display below.
    parse_command, heredocs = _neutralize_heredocs(command)

    # Detection is now ARGV-SCOPED: parse the command and gate ONLY a real `git commit` segment, so a
    # benign command that merely mentions "git"/"commit" (`gh issue create --body "...git commit..."`,
    # `echo "git commit"`, `git log --grep=commit`) is never gated (agent-tools#97). A chain may hold
    # more than one commit (`git commit … && git commit …`); gate on the FIRST that is non-exempt and
    # ticketless.
    segments = commit_segments(parse_command)
    if not segments:
        return _allow()  # no real `git commit` segment → nothing to gate

    # Assign each heredoc its OWN segment index (reusing the real segment parser — see
    # `_assign_heredoc_segment_indices`) so a `-F <path>` in a later segment can only be satisfied
    # by a heredoc whose segment (textually) precedes it, never one written by a LATER command in
    # the chain (agent-tools#570 review).
    _assign_heredoc_segment_indices(parse_command, heredocs)
    for segment in segments:
        decision = _evaluate_commit_segment(segment, event_cwd, command, heredocs)
        if decision is not None:
            return decision  # a non-exempt, ticketless commit → block (or warn-allow)
    return _allow()  # every commit segment is exempt / ticketed


# The human-facing guidance — what's wrong and WHAT TO DO NEXT, role-aware (agent-tools#994).
# Incident 2026-10-24: a subagent hit this block, never ran `task new`, and yielded "blocked at
# commit" because the old text said "ASK the human" and showed `Closes #123` (forbidden by the
# task skill: use `Refs #N`). Step 1 is now the self-service remedy; the human hatch is the LAST
# line and only the coordinator sees it. The stable BLOCK_MARKER appears ONLY on a real block
# (an external check that greps stdout for it must never false-positive on a warn-mode allow),
# so the block path puts it in `what` and the warn path uses its own marker-free `what`.
_TASK_NEW = (
    'Run `task new --title "…" --acceptance "…" (repeatable) --why "…" --impact "…" '
    '--if-not-done "…"` (if refused, fix flags, retry)'
)
_SELF_SERVE = (
    f"{_TASK_NEW}, then re-run this commit with `Refs #<id>` in the message. Do not stop here."
)
# The exempt-type remark is for the coordinator only; a dispatched agent reads it as a loophole
# (baseline trials: 7/10 subagents reworded their mandated message into `chore:` to dodge the gate).
_COORDINATOR_NOTE = "A truly trivial change can use a `chore:`/`docs:` type instead."
# A trailer paragraph keeps a mandated subject intact: baseline trial A#3 never ran `task new`
# because adding a ticket reference seemed to violate an exact-message constraint.
_SUBAGENT_NOTE = (
    "Do not reword a mandated commit message (e.g. into chore:/docs:) to dodge the gate; a "
    'trailer keeps the subject: `git commit -m "<mandated subject>" -m "Refs #<id>"`.'
)
_COORDINATOR_ASK = (
    "run `task new …` for this change and send me the ticket id; I recommit with `Refs #<id>`."
)
# `git commit -F -` streams the message on GIT's stdin, which a PreToolUse hook CANNOT read (the
# hook's stdin is the JSON event, not git's). When neither the message NOR the branch yields a
# ticket, this unreadable-message case FAILS CLOSED WITH A HINT (agent-tools#104): rather than let
# `-F -` silently dodge the gate (the #102 fail-open), BLOCK with an actionable message. The
# only remaining sanctioned out for a genuine ticketless commit is the Telegram hatch (checked
# BEFORE this block, so an approved hatch allows a `-F -`).
_STDIN_SELF_SERVE = (
    f"{_TASK_NEW}, then re-run the commit with `Refs #<id>` where the hook can read it: "
    '`-m "…Refs #<id>…"`, `-F <file>` or a ticket id in the branch name (`feature/ABC-12-foo`). '
    "Do not stop here."
)
_RELAY = (
    "If a subagent reported this block, run `task new` (above), send it the id with "
    "`write agent://<id>` so it recommits with `Refs #<id>`, then `wait` for its final report."
)
_LAST_RESORT = "Last resort, genuine trivial-ticketless cases only: "
_NO_BYPASS = (
    "As a subagent never retry with REQUIRE_TICKET_STRICT=0 or RIG_HATCH_REQUEST_* "
    "(coordinator only) and never go to the human."
)
_HUMAN = (
    "request a one-time Telegram approval via "
    'RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="<justification>" (deny-by-default).'
)
_BLOCK_WHAT = f"{BLOCK_MARKER}."
_STDIN_BLOCK_WHAT = (
    f"{BLOCK_MARKER}. The commit message is on git's stdin (`-F -`), which this hook cannot read."
)
# Warn-only advisories (REQUIRE_TICKET_STRICT=0). They deliberately say "No ticket reference
# found", not "BLOCKED", so the marker stays unambiguous proof of a real block.
_ADVISORY_WHAT = "No ticket reference found — committing anyway (warn-only)."
_STDIN_ADVISORY_WHAT = (
    "commit message is on git's stdin (`-F -`) — unreadable, so the ticket gate cannot verify a "
    "reference; committing anyway (warn-only)."
)


def _render_block(what: str, self_serve: str, human: str = "") -> str:
    """Role-aware ladder via the shared builder; neutral two-branch text if the lib is unusable."""
    if _guard_message is not None:
        try:
            role = _guard_message.detect_role(_CURRENT_EVENT.get("event") or {})
            return _guard_message.render(
                _guard_message.Remedy(
                    what=what, self_serve=self_serve, relay=_RELAY, human=human,
                    last_resort=_LAST_RESORT, no_bypass=_NO_BYPASS,
                    coordinator_note=_COORDINATOR_NOTE, subagent_note=_SUBAGENT_NOTE,
                    coordinator_ask=_COORDINATOR_ASK,
                ),
                role,
            )
        except Exception:  # noqa: BLE001 - a wording helper must never turn a block into a crash
            pass
    lines = [
        what,
        f"1. {self_serve}",
        "2. If you were dispatched by another agent: `write agent://<parent-id>` with what is "
        "missing + what you tried, then `yield` status `awaiting-coordinator` (NOT "
        "blocked/done/failed); the coordinator's `write agent://<you>` revives you. Never retry "
        "with REQUIRE_TICKET_STRICT=0 or RIG_HATCH_REQUEST_* and never go to the human.",
    ]
    top = f"3. If you are the top-level agent: {_RELAY[0].lower()}{_RELAY[1:]}"
    lines.append(f"{top} Last resort, genuine trivial-ticketless cases only: {human}" if human else top)
    return "\n".join(lines)


def _block_message(stdin: bool) -> str:
    if stdin:
        return _render_block(_STDIN_BLOCK_WHAT, _STDIN_SELF_SERVE, _HUMAN)
    return _render_block(_BLOCK_WHAT, _SELF_SERVE, _HUMAN)


def _advisory_message(stdin: bool) -> str:
    if stdin:
        return _render_block(_STDIN_ADVISORY_WHAT, _STDIN_SELF_SERVE)
    return _render_block(_ADVISORY_WHAT, _SELF_SERVE)


def _evaluate_commit_segment(
    segment: CommitSegment,
    event_cwd: str | None,
    command: str,
    heredocs: list[_Heredoc] | None = None,
) -> int | None:
    """Decide ONE parsed `git commit` segment. Returns an exit code when the gate fires (block, or a
    warn-only allow), or None when this segment is clean (exempt / ticketed) so the caller moves on
    to the next segment in the chain. `heredocs` (from `_neutralize_heredocs`, with `segment_index`
    already assigned by `_assign_heredoc_segment_indices`) lets the message extraction recover a
    heredoc-sourced `-m`/`-F` value (agent-tools#503/#406/#430/#570); `segment.index` restricts the
    `-F` match to a heredoc whose OWN segment (textually) precedes THIS commit's segment."""
    # `--amend`/`--continue`/`--abort`/`--skip` aren't authoring a fresh change → nothing to gate.
    if is_skip_commit(segment.argv):
        return None
    # Honor `git -C <repo>`: message-file + branch detection must target the repo the commit acts on.
    cwd = effective_cwd(segment, event_cwd)
    message = commit_message_from_argv(segment.argv, cwd, heredocs, segment.index)
    # Exemption is judged from the message text only (the real subject line), not the `git commit …`
    # argv — otherwise the command words could mis-trigger it.
    if is_exempt(message):
        return None  # trivial chore / WIP / merge — no ticket expected

    # Ticket detection is permissive: scan the commit message and the branch name (a ticket id is
    # often encoded as `feature/ABC-12-foo`). Scoping to the parsed message + branch is both correct
    # and tight; a `-F` file's contents are already folded into `message`. This runs BEFORE the
    # `-F -` stdin block on purpose: a `-F -` commit can still be ticketed via its BRANCH name, which
    # IS readable — same as the editor-commit path.
    branch = current_branch(cwd)
    if has_ticket_reference(message) or has_ticket_reference(branch):
        return None  # a ticket reference is present (in the message or the branch) → proceed

    return _gate_ticketless_commit(segment, cwd, command)


def _gate_ticketless_commit(segment: CommitSegment, cwd: str | None, command: str) -> int:
    """A non-exempt, ticketless commit reached the gate. Under STRICT, consult the Telegram hatch
    FIRST — before deciding between the `-F -` stdin block and the normal block — so an approved
    hatch allows EITHER shape (including a `-F -` commit). Non-strict never blocks, so the hatch is
    only relevant when STRICT would block."""
    stdin = commit_reads_stdin_message(segment.argv)
    if STRICT:
        verdict = _consult_hatch(command, cwd, stdin)
        if verdict is not None:
            return verdict
    return _emit_gate(stdin)


def _consult_hatch(command: str, cwd: str | None, stdin: bool) -> int | None:
    """Run the Telegram hatch for a ticketless commit under STRICT. Returns an exit code when the
    hatch RESOLVES the gate (approved → allow; denied → block leading with the denial reason), or
    None when no hatch was requested (fall through to the normal block)."""
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, {"hook": HOOK_ID, "command": command}, cwd=cwd or os.getcwd(),
        command=command,
    )
    if not hatch.should_stop:
        return None
    if hatch.approved:
        note = f"require-ticket gate allowed via hatch escalation ({hatch.reason})"
        warn(note)
        emit("allow", note)
        return 0
    warn(f"require-ticket gate hatch escalation denied: {hatch.reason}")
    base = _block_message(stdin)
    emit("block", f"hatch escalation denied: {hatch.reason}\n{base}")
    return BLOCK_EXIT_CODE


def _emit_gate(stdin: bool) -> int:
    """Emit the block (STRICT) or warn-only allow (REQUIRE_TICKET_STRICT=0) for a ticketless
    commit, choosing the `-F -` stdin wording when the message is on git's unreadable stdin."""
    if STRICT:
        # Marker LEADS the block message so an external assertion can grep one fixed string.
        message = _block_message(stdin)
        emit("block", message)
        return BLOCK_EXIT_CODE
    advisory = _advisory_message(stdin)
    warn(advisory)
    emit("allow", advisory)
    return 0


def _allow() -> int:
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
