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
ASK the human, or request a ONE-TIME Telegram approval via
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
pass the message a readable way (`-m "…Closes #N…"` or `-F <file>`), encode the ticket in
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
    """

    __slots__ = ("env", "argv", "git_argv")

    def __init__(self, env: dict[str, str], argv: list[str], git_argv: list[str]) -> None:
        self.env = env
        self.argv = argv
        self.git_argv = git_argv


def commit_segments(command: str) -> list[CommitSegment]:
    """Every real `git commit` segment in `command` (a chain may hold more than one). Empty on a
    tokenization failure (the safe direction: a command we can't parse is not gated — require-ticket
    is on_error=open, a discipline reminder, not a security boundary)."""
    tokens = _tokenize(command)
    if tokens is None:
        return []
    out: list[CommitSegment] = []
    for raw_seg in _segments(tokens):
        seg = _strip_leading_shell_noise(_strip_redirects(raw_seg))
        env, rest = _split_inline_env(seg)
        git_argv = _strip_wrappers(rest)  # the real `git …` invocation, wrappers peeled
        argv = _commit_argv(git_argv)
        if argv is not None:
            out.append(CommitSegment(env, argv, git_argv))
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


def commit_message_from_argv(argv: list[str], cwd: str | None = None) -> str:
    """Pull the commit message out of the already-parsed commit argv: -m/--message and -F/--file.

    Returns ONLY the message text — the concatenation of every -m value and the contents of any -F
    file — so the conventional-type / WIP exemption check sees the real subject line, not the `git
    commit …` argv. A relative `-F` path is resolved against `cwd` (the command's working directory
    from the event), since the hook may run elsewhere. A `-F -` (stdin) value contributes NO text —
    git streams that message on its own stdin, which the hook can't read; commit_reads_stdin_message
    detects it separately so the caller can fail closed with a hint. Stops at `--` (the rest are
    literal pathspecs, never message flags)."""
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
            parts.append(_read_message_file(value, cwd) if canonical == "--file" else value)
            i += 1
            continue
        canonical_sep = _canonical_long_value_flag(tok)
        if canonical_sep in _MESSAGE_VALUE_LONG and i + 1 < len(argv):
            value = argv[i + 1]
            parts.append(_read_message_file(value, cwd) if canonical_sep == "--file" else value)
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
            parts.append(_read_message_file(value, cwd) if letter == "F" else value)
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


def _read_message_file(path: str, cwd: str | None = None) -> str:
    # `-F -` is the stdin sentinel, not a file named "-": don't try to open it (it would fail and
    # warn spuriously). commit_reads_stdin_message handles that case at the decision layer.
    if path == _STDIN_SENTINEL:
        return ""
    resolved = os.path.expanduser(path)
    if cwd and not os.path.isabs(resolved):
        resolved = os.path.join(cwd, resolved)
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
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    event_cwd = event.get("cwd") or args.get("cwd")

    # Detection is now ARGV-SCOPED: parse the command and gate ONLY a real `git commit` segment, so a
    # benign command that merely mentions "git"/"commit" (`gh issue create --body "...git commit..."`,
    # `echo "git commit"`, `git log --grep=commit`) is never gated (agent-tools#97). A chain may hold
    # more than one commit (`git commit … && git commit …`); gate on the FIRST that is non-exempt and
    # ticketless.
    segments = commit_segments(command)
    if not segments:
        return _allow()  # no real `git commit` segment → nothing to gate

    for segment in segments:
        decision = _evaluate_commit_segment(segment, event_cwd, command)
        if decision is not None:
            return decision  # a non-exempt, ticketless commit → block (or warn-allow)
    return _allow()  # every commit segment is exempt / ticketed


# The shared human-facing guidance — what's wrong and how to satisfy the gate. It carries NO
# marker: the stable BLOCK_MARKER must appear ONLY on a real block, so an external check that
# greps stdout for the marker (the task-cli Docker test) never gets a false positive on a
# warn-mode allow. The block path prepends the marker; the warn path uses the guidance alone.
_GUIDANCE = (
    "Non-trivial changes should start from a ticket "
    "(task-cli / GitHub Issue / Linear) with acceptance criteria, motivation, and "
    "user-impact — then reference it in the commit (e.g. `Closes #123`, `Fixes #4`, "
    "`task:ABC-12`, `ENG-456`). If this is a trivial chore, use a `chore:`/`docs:` type "
    "(exempt). No self-service bypass: ASK the human, or request a one-time Telegram approval "
    'via RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="<justification>" (deny-by-default; '
    "bare 1 rejected); opt the whole gate back to warn-only with REQUIRE_TICKET_STRICT=0."
)

# `git commit -F -` streams the message on GIT's stdin, which a PreToolUse hook CANNOT read (the
# hook's stdin is the JSON event, not git's). When neither the message NOR the branch yields a
# ticket, this unreadable-message case FAILS CLOSED WITH A HINT (agent-tools#104): rather than let
# `-F -` silently dodge the gate (the #102 fail-open), BLOCK with an actionable message. The
# trailer/inline-env escapes are gone; the only remaining sanctioned out for a genuine ticketless
# commit is the Telegram hatch (checked BEFORE this block, so an approved hatch allows a `-F -`).
_STDIN_HINT = (
    f"{BLOCK_MARKER}. The commit message is on git's stdin (`-F -`), which a PreToolUse hook "
    "cannot read — so the ticket gate cannot verify a reference (and the branch name carried "
    "none either) and will not let `-F -` dodge it. Put the ticket somewhere readable: pass "
    "the message via `-m \"…Closes #123…\"` or `-F <file>` (both are checked), or encode the "
    "ticket in the branch name (e.g. `feature/ABC-12-foo`). No self-service bypass: ASK the "
    "human, or request a one-time Telegram approval via "
    'RIG_HATCH_REQUEST_REQUIRE_TICKET_BEFORE_COMMIT="<justification>" (deny-by-default; bare 1 '
    "rejected); or opt the whole gate back to warn-only with REQUIRE_TICKET_STRICT=0."
)

# Warn-only opt-out advisory for a `-F -` commit (REQUIRE_TICKET_STRICT=0). Never leaks the marker.
_STDIN_ADVISORY = (
    "commit message is on git's stdin (`-F -`) — unreadable, so the ticket gate cannot "
    "verify a reference; committing anyway (warn-only). Prefer `-m`/`-F <file>` so the gate "
    "can check the ticket."
)


def _evaluate_commit_segment(
    segment: CommitSegment, event_cwd: str | None, command: str,
) -> int | None:
    """Decide ONE parsed `git commit` segment. Returns an exit code when the gate fires (block, or a
    warn-only allow), or None when this segment is clean (exempt / ticketed) so the caller moves on
    to the next segment in the chain."""
    # `--amend`/`--continue`/`--abort`/`--skip` aren't authoring a fresh change → nothing to gate.
    if is_skip_commit(segment.argv):
        return None
    # Honor `git -C <repo>`: message-file + branch detection must target the repo the commit acts on.
    cwd = effective_cwd(segment, event_cwd)
    message = commit_message_from_argv(segment.argv, cwd)
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
    base = _STDIN_HINT if stdin else f"{BLOCK_MARKER}. {_GUIDANCE}"
    emit("block", f"hatch escalation denied: {hatch.reason}\n{base}")
    return BLOCK_EXIT_CODE


def _emit_gate(stdin: bool) -> int:
    """Emit the block (STRICT) or warn-only allow (REQUIRE_TICKET_STRICT=0) for a ticketless
    commit, choosing the `-F -` stdin wording when the message is on git's unreadable stdin."""
    if stdin:
        if STRICT:
            warn(_STDIN_HINT)
            emit("block", _STDIN_HINT)
            return BLOCK_EXIT_CODE
        warn(_STDIN_ADVISORY)
        emit("allow", _STDIN_ADVISORY)
        return 0
    if STRICT:
        # Marker LEADS the block message so an external assertion can grep one fixed string.
        emit("block", f"{BLOCK_MARKER}. {_GUIDANCE}")
        return BLOCK_EXIT_CODE
    # Warn-only opt-out: the advisory deliberately says "no ticket reference found", not "BLOCKED",
    # so the marker is unambiguous proof of a real block.
    advisory = f"No ticket reference found — committing anyway (warn-only). {_GUIDANCE}"
    warn(advisory)
    emit("allow", advisory)
    return 0


def _allow() -> int:
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
