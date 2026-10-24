#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a PATTERN-based kill of a SHARED process name.

Denies a shell command that kills processes by NAME/PATTERN match (`pkill -f node`, `killall
codex`, `kill $(pgrep -f "review diff")`, `pgrep -f playwright | xargs kill`) when the pattern
being matched is a generic, widely-shared tool/process name — because `pkill -f`/`killall`/
`pgrep` match against EVERY process on the machine whose command line contains the pattern,
not just the caller's own. Two real incidents motivate this (2026-07-01 agent-ecosystem
retrospective, gap G-5): a subagent's `pkill -f "review diff"` killed a DIFFERENT session's
in-flight code review (2026-06-26), and a narrow-grep-based kill nearly killed another
session's e2e matrix run (2026-06-27). Both were accidental collateral damage, not deliberate
bypasses — this hook exists to turn "kill everything matching this word" into a deliberate,
scoped, or explicitly-approved action.

Detection is ARGV-BASED (shlex), not a raw substring match (same discipline as
block-raw-pr-merge / block-reset-hard / block-no-verify). Three shapes are covered:

  1. `pkill [flags] <pattern>` / `killall [flags] <name>...` — the pattern/name argument(s).
  2. `kill $(pgrep ...)` / `` kill `pgrep ...` `` / `kill $(pidof ...)` — a PID list sourced
     from a `pgrep`/`pidof` command substitution; the substitution's own pattern is extracted
     and classified.
  3. A pipeline that resolves PIDs by pattern and feeds them to `kill`: `pgrep <pattern> |
     xargs kill`, `pidof <name> | xargs kill`, or the "narrow grep" shape `ps aux | grep
     <pattern> | ... | xargs kill`.

A pattern is DANGEROUS (and requires approval) only when it is — or contains, as a whole
word/phrase — a known SHARED tool/process name (`_SHARED_PROCESS_NAMES` below) AND it carries
no session-scoping signal (`_looks_session_scoped`: a path, a hex/uuid-looking run, or a long
digit run — the shape of a worktree path, a harness user-data-dir prefix, a port, or a PID).
This is deliberately a DENYLIST of known-ambiguous names, not a blanket "block every pattern
kill" — the sanctioned e2e-harness recipe `pkill -9 -f "<user-data-dir-prefix>"` (repo
CLAUDE.md) and any `kill <pid>` must keep working with zero friction; only the actually
collateral-prone shape (a bare shared tool name with nothing scoping it to the caller's own
process) is gated.

Allowed (let through, no approval needed):
  - `kill <pid> [<pid> ...]` (optionally with a `-SIGNAL`/`-s SIGNAL` flag) — always
    PID-targeted, cannot hit another session's process by name collision.
  - A pattern carrying a session-scoping signal even if it also mentions a shared tool name
    (`pkill -f "/Users/ultra/work/hyperide-worktrees/agent-x/node_modules/.bin/vitest"`,
    `pkill -9 -f "hvsc-3-a1b2c3d4"`) — the match set is already narrow.
  - An unrecognized pattern not on the denylist — fails OPEN on unknowns so this hook never
    blocks a legitimate, project-specific kill it has no reason to distrust; it exists to gate
    KNOWN-ambiguous names, not to police every `pkill` invocation.
  - `dev stop` (the project's own safe, repo-scoped dev-process stop command) is never touched
    by this hook (it does not invoke pkill/killall/kill itself).

External approval (deny-by-default, no self-service bypass): request a one-time Telegram
approval by setting `RIG_HATCH_REQUEST_PKILL_GUARD="<written justification>"` — routes through
the shared `agenttools_hatch_escalation` helper (the same mechanism as background-subagent-gate
/ block-raw-pr-merge), auto-recorded in `overrides.log` (G-8). A blank/bare-flag value is
rejected. Nothing set = denied; use a PID-targeted `kill <pid>` or `dev stop` instead.

Known limitations (documented, not silently missed — same bar as sibling hooks: would a
confused, non-evasive agent produce this BY ACCIDENT?):
  - The denylist is necessarily incomplete; an unlisted shared name is not caught (fails open
    toward not blocking legitimate work — see "Allowed" above).
  - Only ONE level of command-substitution nesting is resolved for `kill $(...)`; a
    substitution containing a further nested substitution is not recursed into.
  - `pkill`/`killall` value-taking flags (`-u <user>`, `-g <pgrp>`, and similar) are NOT
    recognized as flags that consume the NEXT token — `_positionals` treats every non-flag
    token as a candidate pattern, so `pkill -u node` reads `node` (the `-u` filter's VALUE, a
    username, not a kill pattern) as if it were one and blocks it. This is an OVER-block
    (false positive), never a bypass — deliberately: this hook's `_positionals` docstring states
    the design outright ("treating a flag's operand as a candidate pattern too can only make the
    guard MORE conservative, never miss a real pattern"). Implementing pkill/killall's own
    getopt table (which differs between BSD/macOS and GNU/Linux `pkill`) to exclude a
    recognized flag's value specifically would trade this conservative-by-default posture for a
    parsing surface that could itself misclassify an unfamiliar flag shape; not done here.
  - Process filtering via `awk '/pattern/{print $2}'` (instead of `grep`/`pgrep`/`pidof`) piped
    into a kill is not recognized — only `pgrep`/`grep`/`egrep`/`fgrep`/`pidof` stages are
    scanned for a pattern.
  - A RELATIVE path (`pkill -f "node scripts/dev.js"`) is treated as session-scoping even
    though it is identical across every checkout of the same repo — only a bare, well-known
    GLOBAL bin directory (`/usr/bin/node`, `/opt/homebrew/bin/python3`) is excluded from the
    path signal; a shared relative path is not. Distinguishing "this path is unique to my
    session" from "this path merely contains a slash" in general would need knowledge of the
    caller's actual worktree root, which this hook does not have.
  - A shell alias for `pkill`/`kill`/`killall` is not resolved (same documented gap as every
    sibling hook in this catalog — aliases don't expand under a harness's `bash -c` anyway).
  - `_WRAPPER_VALUE_FLAGS` only recognizes a wrapper's SEPARATE-token value flags (`sudo -u
    alice`); a CLUSTERED short-flag form with a glued value (`sudo -ualice`, `sudo -up alice`
    meaning `-u -p alice`) is not unclustered the way `block-reset-hard`'s more heavily-hardened
    parser does, and can still misread the wrapped command in that specific shape. Only reachable
    with a wrapper flag an agent would rarely type glued/clustered by accident.
  - The session-scoping check (`_looks_session_scoped`) treats "a scoping-shaped token ANYWHERE
    in the pattern" as sufficient, which a DELIBERATELY crafted pattern can defeat: `pkill -f
    "node|session-a1b2c3d4"` — a regex alternation, since pkill's `-f` pattern is a POSIX
    extended regex — still matches every `node` process, but the scoping token elsewhere in the
    string reads as "scoped" to this heuristic. Same class of gap as a variable/nested-shell
    indirection (`TARGET=node pkill -f "$TARGET"`, `sh -c 'pkill -f node'`) — the ACTUAL pattern
    is only known after shell/variable expansion this hook cannot see. Both require deliberately
    crafted input, not an accidental shape (this hook's stated threat model, see the top of this
    docstring); not fixed here.
  - The "global bin dir" exclusion (`_GLOBAL_BIN_DIR_RE`) only recognizes a HANDFUL of
    well-known system prefixes (`/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `/bin`,
    `/sbin`); an equally machine-shared interpreter reached through a version manager
    (`/opt/homebrew/Cellar/node/24.1.0/bin/node`, `~/.nvm/versions/node/v22/bin/node`,
    `~/.pyenv/versions/3.12/bin/python`, an `asdf` shim) still reads as session-scoped just
    because it contains a `/`. Enumerating every version manager's path convention is an
    unbounded list not worth chasing here; the handful of bare system prefixes covers the
    cheap, common case.
  - A multi-line command containing a heredoc whose BODY happens to mention a denylisted name
    (`cat <<'EOF' > notes.md` followed by a body line like `pkill -f node`) is treated as if
    that body line were a real command (the newline-as-`;` tokenizer has no heredoc awareness,
    unlike the much more heavily-hardened `block-raw-pr-merge`) — an OVER-block (documenting a
    kill recipe gets blocked), never a bypass, but worth knowing before reaching for the
    Telegram hatch on a legitimately blocked doc edit.
  - Command substitution is only inspected in the narrow `kill $(...)` / `` kill `...` `` shape
    (shape 2 above) — a substitution appearing OUTSIDE a direct argument to `kill` (`echo
    $(pkill -f node)`, `x=$(pkill -f node)`) is not resolved at all. This hook's substitution
    handling exists specifically to unwrap the "PID list sourced from a pattern match" idiom,
    not to generically evaluate every substitution anywhere in the command.
  - An inverted-match filtering stage ahead of the real pattern (`grep -v <shared-name> | xargs
    kill`) reads `<shared-name>` as the pattern the same as a normal (non-`-v`) `grep` would,
    even though `-v` means the shared name is being EXCLUDED, not matched — an OVER-block (the
    pipeline is probably safe), never a bypass. `_patterns_for_stage` does not special-case `-v`;
    doing so would need to distinguish it from a `grep`/`pgrep` flag that merely happens to
    precede the pattern in some other position, which is not worth the added parsing surface for
    an over-block-only gap.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a collateral pattern-kill slipping
through a broken guard is exactly the failure this hook exists to stop.
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

# The role-aware "what to do next" builder (agent-tools#994). Loaded by repo-local path like the
# hatch helper and OPTIONAL: any load failure falls back to the neutral wording in `_render_block`.
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
    # dataclasses resolves string annotations through sys.modules[cls.__module__].
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
# The event main() is deciding, so the block emitter can name the reader's role.
_CURRENT_EVENT: dict = {}


def _render_block(what: str, self_serve: str, relay: str = "", human: str = "") -> str:
    """Role-aware ladder via the shared builder; neutral two-branch text if the lib is unusable."""
    if _guard_message is not None:
        try:
            role = _guard_message.detect_role(_CURRENT_EVENT.get("event") or {})
            return _guard_message.render(
                _guard_message.Remedy(what=what, self_serve=self_serve, relay=relay, human=human),
                role,
            )
        except Exception:  # noqa: BLE001 - a wording helper must never turn a block into a crash
            pass
    top = (
        "3. If you are the top-level agent: "
        + (relay or "supply the missing thing to a blocked subagent with `write agent://<id>`, then `wait`.")
    )
    if human:
        top += f" Last resort, genuine exceptions only: {human}"
    return "\n".join(
        [
            what,
            f"1. {self_serve}",
            "2. If you were dispatched by another agent: `write agent://<parent-id>` with what is "
            "missing + what you tried, then `yield` status `awaiting-coordinator` (NOT "
            "blocked/done/failed); the coordinator's `write agent://<you>` revives you. Never "
            "retry with RIG_HATCH_REQUEST_* and never go to the human.",
            top,
        ]
    )

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "pkill-guard"

# ── shared/ambiguous process names (the denylist) ───────────────────────────────────────────
# Generic tool/process names common on an AI-agent dev machine (this repo's own toolchain plus
# the sibling repos it works alongside). Matched as a whole word/phrase, case-insensitive.
# Multi-word entries (e.g. "review diff") are the literal incident shape: review-cli's own
# process command line legitimately starts with that phrase in EVERY session running it.
_SHARED_PROCESS_NAMES = frozenset({
    "node", "bun", "deno", "python", "python3", "electron", "code", "codex", "claude",
    "opencode", "review", "review diff", "playwright", "vsce", "npm", "npx", "tsx", "ts-node",
    "vitest", "jest", "pytest", "chrome", "chromium", "vite", "webpack", "esbuild", "tg",
    "tg-ctl", "cursor", "windsurf", "java", "ruby", "cargo", "rustc", "go", "docker",
    "gh", "rig",
})

# Sorted longest-first so a multi-word entry ("review diff") is tried before its shorter
# single-word substring ("review") would otherwise match first inside the same regex scan.
_NAME_PATTERNS = [
    (name, re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE))
    for name in sorted(_SHARED_PROCESS_NAMES, key=len, reverse=True)
]

# A session-scoping signal: a path separator, a long hex/alnum run (uuid/hash-shaped, e.g. a
# worktree hash or the harness's `hvsc-` isolation prefix + suffix), or a 5+ digit run (a PID,
# a timestamp). Any one of these narrows the match set enough to trust the pattern. The digit
# run is deliberately 5+, not 4+: every common local dev port (3000, 5173, 8080, 8081, 9222,
# 9229, ...) is exactly 4 digits, and a bare port number does NOT narrow a `pkill -f` match to
# the caller's own session — it's shared by every concurrent Playwright/Chrome/dev-server
# session using the same default port.
_SCOPED_HEX_RUN = re.compile(r"[0-9a-fA-F]{6,}")
_SCOPED_DIGIT_RUN = re.compile(r"\d{5,}")

# A path that resolves to nothing MORE specific than a well-known GLOBAL system/package-manager
# bin directory (`/usr/bin/node`, `/opt/homebrew/bin/python3`) is not session-scoping — that
# exact path is shared by every process on the machine using the system interpreter, which is
# exactly the collision this hook exists to prevent. A path with anything else in it (a worktree
# directory, a project-local `node_modules/.bin/`, a harness user-data-dir) still counts.
_GLOBAL_BIN_DIR_RE = re.compile(
    r"^(?:/usr(?:/local)?|/opt/homebrew|/bin|/sbin)/(?:bin|sbin)?/?[^/]+$"
)


def _looks_session_scoped(pattern: str) -> bool:
    if "/" in pattern and not _GLOBAL_BIN_DIR_RE.match(pattern.strip()):
        return True
    return bool(_SCOPED_HEX_RUN.search(pattern)) or bool(_SCOPED_DIGIT_RUN.search(pattern))


def _dangerous_name_in(pattern: str) -> str | None:
    """The first denylisted name found as a whole word/phrase in `pattern`, or None."""
    for name, rx in _NAME_PATTERNS:
        if rx.search(pattern):
            return name
    return None


def _is_dangerous_pattern(pattern: str) -> str | None:
    """The denylisted name this pattern is dangerous for, or None if it's safe to let through
    (either it names nothing on the denylist, or it carries a session-scoping signal)."""
    if _looks_session_scoped(pattern):
        return None
    return _dangerous_name_in(pattern)


# ── argv recovery: shell segments, wrappers, inline env (same discipline as block-reset-hard) ──

_SHELL_SEPS = frozenset({"&&", "||", ";", "&"})  # "hard" separators: start a NEW pipeline group
_PIPE_SEPS = frozenset({"|", "|&"})  # separators that continue the SAME pipeline group
_ALL_SEPS = _SHELL_SEPS | _PIPE_SEPS

_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "if", "then", "do", "else", "elif"})
_MAX_LEADING_NOISE = 16

_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer",
    "command", "sudo", "doas", "exec",
})
_MAX_WRAPPER_NESTING = 16

# Wrapper flags that take a SEPARATE value (so the next token is the value, not the real
# command). Ported verbatim from block-reset-hard's audited table for every wrapper name THIS
# hook's `_WRAPPERS` set actually recognizes (nohup/setsid/unbuffer/command genuinely have no
# common value-taking flags, matching block-reset-hard's own table). A wrapper present in
# `_WRAPPERS` but MISSING here is silently treated as flag-less — its own value-taking flag's
# VALUE would then be misread as the wrapped command, and the real command after it invisible
# to every check downstream (this is exactly how the original ionice/stdbuf gap shipped:
# `ionice -c 3 pkill -f node` misread `3` as the wrapped command, and `pkill -f node` was never
# inspected at all — a P1, not a cosmetic parse wart). Keep this table a COMPLETE copy of
# block-reset-hard's, not a hand-trimmed "just the common ones" subset.
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
}
_OPERAND_DROP_WRAPPERS = frozenset({"timeout"})  # `timeout 5s <cmd>` — drop the duration operand


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _substitution_spans(text: str) -> list[tuple[int, int, str]]:
    """Every top-level `$( ... )` / `` ` ... ` `` span in `text`, as `(start, end, inner)`,
    quote-agnostic (this is a best-effort scan over raw text, before shlex tokenizing — see
    module docstring's "one level of nesting" limitation)."""
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$(", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                spans.append((i, j, text[i + 2 : j - 1]))
                i = j
                continue
            break  # unterminated — stop scanning, nothing more can be resolved
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j == -1:
                break
            spans.append((i, j + 1, text[i + 1 : j]))
            i = j + 1
            continue
        i += 1
    return spans


def _placeholder_command(command: str) -> tuple[str, dict[str, str]]:
    """Replace every top-level command substitution with an opaque placeholder token, so shlex
    tokenizes `kill $(pgrep -f X)` as `['kill', '__SUBST0__']` instead of splitting the
    substitution's own internal whitespace into separate argv tokens. Returns the rewritten
    command and a `{placeholder: inner_text}` side table."""
    spans = _substitution_spans(command)
    if not spans:
        return command, {}
    out: list[str] = []
    table: dict[str, str] = {}
    pos = 0
    for idx, (start, end, inner) in enumerate(spans):
        out.append(command[pos:start])
        placeholder = f"__PKILLGUARD_SUBST{idx}__"
        table[placeholder] = inner
        out.append(placeholder)
        pos = end
    out.append(command[pos:])
    return "".join(out), table


def _shlex_tokens(line: str, *, posix: bool) -> list[str] | None:
    """Tokenize `line` with shlex in the given quote-handling mode. Returns None on
    unbalanced quotes (caller treats this as fail-closed)."""
    lex = shlex.shlex(line, posix=posix, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE (possibly already-merged multi-line) chunk. Comments are handled MANUALLY
    (shlex's own `commenters` is disabled) — shlex's built-in commenter cuts at ANY unquoted
    `#`, even mid-word, which would truncate parsing at a benign `foo#bar`.

    A token that starts with a REAL, unquoted `#` is a comment; drop it and the rest of the
    chunk. Whether a `#` is "real" can NOT be decided from the POSIX-mode (quote-stripped)
    token alone: a quoted `'# heading'` dequotes to the same string a bare `# heading` would
    produce, so `tok.startswith("#")` on the dequoted token can't tell a genuine comment from a
    quoted argument that merely starts with `#`. Ported verbatim from `block-reset-hard`'s
    audited fix for the identical class of bug (originally found there as a `git reset --hard`
    bypass hidden behind a mid-line `#`). A second, quote-PRESERVING (`posix=False`)
    tokenization runs in parallel so the check lands on the RAW token (quotes/escapes intact):
    only a token that ITSELF starts with `#` in that raw form is a genuine, unquoted comment
    marker. Returns None on unbalanced quotes, or if the two passes disagree on token count
    (can't reliably align raw-to-dequoted; caller treats this as fail-closed) — this is also
    what closes the comment/quote-interaction bypass: a stray quote character sitting INSIDE a
    real comment on one line (`# it's a comment`) used to be treated as a genuinely unclosed
    quote, merging the comment forward into a LATER line and swallowing a real `pkill` there
    into what looked like one giant quoted argument. The dual-pass raw/posix disagreement (or
    outright parse failure) on that merged text now falls through to the caller's fail-closed
    path instead of silently classifying the swallowed command as safe.

    A `#`-free line skips the raw pass entirely and returns the posix tokens directly — the raw
    pass exists SOLELY to disambiguate a genuinely unquoted `#`, so a line with no `#` at all
    gets an identical verdict from the posix pass alone. This is not just a constant-factor
    speedup: it also means a `#`-free line with an ordinary unquoted mid-word apostrophe or two
    (`echo it's; echo don't`) can never hit the raw/posix TOKEN-COUNT-disagreement branch below
    (posix mode opens a quote at a mid-word apostrophe and merges across it, non-posix mode
    doesn't — a difference that's real but irrelevant here since there's no `#` to disambiguate),
    which would otherwise force `_tokenize`'s multi-line merge-forward loop to retokenize a
    growing chunk from scratch on every subsequent line — quadratic in the number of remaining
    lines, and now doubled by running both passes on each attempt. A line mentioning a real `#`
    still gets full dual-pass protection."""
    if "#" not in line:
        return _shlex_tokens(line, posix=True)
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


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a (possibly multi-line) command into a flat token stream where a bare
    newline is a command separator (`;`) — same discipline as `block-reset-hard`. A plain
    single-pass shlex treats `\\n` as ordinary whitespace (a word separator, not a command
    separator), so `cd /repo` + newline + `pkill -f node` would otherwise collapse into ONE
    segment (`['cd', '/repo', 'pkill', '-f', 'node']`, argv[0]=`cd`) and hide the pkill entirely
    — this tokenizes LINE BY LINE and inserts an explicit `;` between lines, merging forward
    into the next line only when a line's own quotes don't balance (a value that legitimately
    spans a real newline). Returns None only if no split of the remaining lines ever balances
    (caller fails closed)."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n").replace("\\\n", "")
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
            out.append(";")
        first = False
        out.extend(toks)
        i += 1
    return out


def _split_groups(command: str) -> list[list[list[str]]] | None:
    """Tokenize `command` and split into pipeline GROUPS (separated by `;`/`&&`/`||`/`&`), each
    a list of STAGES (separated by `|`/`|&`). Returns None on unbalanced quotes."""
    tokens = _tokenize(command)
    if tokens is None:
        return None
    groups: list[list[list[str]]] = []
    stages: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _PIPE_SEPS:
            stages.append(current)
            current = []
        elif tok in _SHELL_SEPS:
            stages.append(current)
            groups.append(stages)
            stages = []
            current = []
        else:
            current.append(tok)
    stages.append(current)
    groups.append(stages)
    return [[s for s in g if s] for g in groups if any(g)]


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_inline_env(argv: list[str]) -> list[str]:
    while argv and _INLINE_ENV.match(argv[0]):
        argv = argv[1:]
    return argv


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if argv[i] in value_flags and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        i += 1
    return argv[i:]


# Used to gate a fail-closed verdict on genuinely UNPARSEABLE text (top-level or inside a
# substitution): a dangerous name mention alone isn't enough — the text must also plausibly be
# an actual kill invocation, not e.g. a commit message mentioning "codex" with an unrelated
# unbalanced quote elsewhere in the same command.
_KILL_VERB_RE = re.compile(r"\b(pkill|killall|kill|pgrep|pidof)\b")


class _WrapperOverflow(Exception):
    """A wrapper chain still unresolved past the nesting cap — fail closed (see block below)."""


class _UnparseableSubstitution(Exception):
    """A `$(...)`/`` `...` `` substitution's inner text could not be tokenized (unbalanced
    quotes) AND the raw inner text plausibly names a denylisted process via
    pkill/killall/kill/pgrep — fail closed rather than silently treat it as safe. Without this,
    `kill $(pgrep -f 'review diff)` (a plausible accidental unclosed quote) would classify as
    "safe" purely because the SUBSTITUTION's own parse failed, even though the identical text
    OUTSIDE a substitution (`pkill -f 'review diff`) already fails closed via `_classify`'s
    top-level unbalanced-quote hint check — this exception makes the substitution path apply
    the same doctrine instead of a silent, asymmetric fail-open."""


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


# Shell redirection operators. shlex's `punctuation_chars=True` mode emits each of these as its
# own standalone token — so `pkill -f node >/dev/null 2>&1` tokenizes with `>` and `>&` as
# distinct tokens, the redirect TARGET (`/dev/null`, `1`) as an ordinary following word. Without
# removing them, the target would be picked up as an extra positional argument — appending a
# redirect is one of the most common things a command accidentally grows.
_REDIRECT_OPS = frozenset({">", ">>", "<", "<<", ">&", "&>", "<&", "<>"})

# A bare digit-only token immediately followed by a redirect operator is that operator's
# file-descriptor prefix (`2>err.log`, `1<&0`) — shlex's `punctuation_chars=True` mode splits
# `2>` into TWO tokens (`"2"`, `">"`) since the digit isn't itself part of `_REDIRECT_OPS`.
_FD_DIGITS_RE = re.compile(r"^\d+$")


def _strip_redirections(argv: list[str]) -> list[str]:
    """Remove every shell redirection operator token together with its target and any leading
    file-descriptor digit — NOT just a trailing redirect (`pkill -f node >/dev/null`) but one
    appearing anywhere in the segment (`sudo 2>/dev/null pkill -f node`). A shell redirect can
    appear before, between, or after the command's real arguments; truncating everything from
    the FIRST operator onward would discard real arguments that come after it too
    (`2>/dev/null -f node` truncated to nothing but `2` — the actual `-f node` pattern would
    never be seen at all, an under-block, not just an over-block). A LEADING fd-digit needs its
    own check: `sudo 2>/dev/null pkill -f node` tokenizes to `['sudo', '2', '>', '/dev/null',
    'pkill', '-f', 'node']`; `_skip_wrapper_args` stops peeling `sudo`'s flags at the first
    non-flag token (`'2'`, since it doesn't start with `-`), so by the time this function runs
    the orphan `'2'` would otherwise survive as `argv[0]` and hide the real `pkill` behind it."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        if (
            i + 1 < len(argv)
            and argv[i + 1] in _REDIRECT_OPS
            and _FD_DIGITS_RE.match(argv[i])
        ):
            i += 1  # the fd-digit prefix — the operator itself is handled next iteration
            continue
        if argv[i] in _REDIRECT_OPS:
            i += 2  # the operator and its target (if any) — never part of the real argv
            continue
        out.append(argv[i])
        i += 1
    return out


def _stage_argv(stage: list[str]) -> list[str]:
    """Recover a stage's real argv by stripping shell noise, inline env assignments, wrapper
    executables, and redirections — to a FIXPOINT, not a single fixed-order pass. A redirect
    can appear BEFORE the wrapper/assignment it's attached to (`2>/dev/null sudo pkill -f
    node`, `2>/dev/null FOO=1 pkill -f node`), not just after it (the shape a single
    strip-redirections-last pass already handled). A one-shot ordering — strip env/wrappers,
    THEN redirections — never revisits `argv[0]` once a leading redirect has been removed, so
    `sudo`/`FOO=1` sitting right after that redirect is never peeled and survives as `argv[0]`,
    making the whole stage invisible to the pkill/killall check downstream. Looping every strip
    until none of them change anything converges regardless of how redirects, wrappers, and
    inline assignments are interleaved. `_strip_wrappers` can raise `_WrapperOverflow`; that
    propagates out of this loop unchanged (same fail-closed handling as everywhere else)."""
    argv = list(stage)
    for _ in range(_MAX_WRAPPER_NESTING + 1):
        before = argv
        argv = _strip_redirections(argv)
        argv = _strip_leading_shell_noise(argv)
        argv = _strip_inline_env(argv)
        argv = _strip_wrappers(argv)
        if argv == before:
            return argv
    return argv


# ── per-command-shape pattern extraction ─────────────────────────────────────────────────────

_SIGNAL_FLAG = re.compile(r"^-(?:[A-Z0-9]+|s|-signal)$", re.IGNORECASE)


def _kill_is_pid_only(args: list[str]) -> bool:
    """True iff every non-flag argument to `kill` is a bare PID or job-spec (`%1`) — the always-
    safe form. A `-SIGNAL`/`-s SIGNAL` flag (and its value) is skipped."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            continue
        if a.startswith("-"):
            if a in ("-s", "--signal") and i + 1 < len(args):
                i += 2
                continue
            if _SIGNAL_FLAG.match(a):
                i += 1
                continue
            return False  # an unrecognized flag — don't claim to understand this invocation
        if a.startswith("%") or a.isdigit():
            i += 1
            continue
        return False
    return True


def _positionals(args: list[str]) -> list[str]:
    """Every non-flag token in `args` — pkill/killall accept MULTIPLE name/pattern operands
    (`killall node worker`), and a flag that itself takes a value (`pkill -f node -u root`)
    would otherwise make its value look like "the" trailing positional if only the LAST one
    were inspected. This is a best-effort scan (flags with a glued value like `-SIGKILL` are
    skipped whole; a flag's own separate-value operand, e.g. `root` after `-u`, is not
    distinguished from a real pattern) — matching this hook's "check everything, fail open only
    on an unrecognized NAME, never on an unrecognized ARGUMENT SHAPE" posture: treating a flag's
    operand as a candidate pattern too can only make the guard MORE conservative, never miss a
    real pattern sitting elsewhere in the same argument list."""
    return [a for a in args if not a.startswith("-")]


def _patterns_for_stage(argv: list[str]) -> list[str]:
    """If `argv` is a `pgrep`/`grep`/`egrep`/`fgrep`/`pidof` invocation, its pattern/name
    operands. `pidof <name>` resolves PIDs by an EXACT process name (no `-f`/regex needed) —
    the same "kill every process matching this word" shape as `pgrep`, arguably more dangerous
    by default since it needs no extra flag to match broadly."""
    if not argv:
        return []
    name = _basename(argv[0])
    if name in ("pgrep", "grep", "egrep", "fgrep", "pidof"):
        return _positionals(argv[1:])
    return []


def _any_dangerous(patterns: list[str]) -> str | None:
    """The first dangerous name found across `patterns` (checking every candidate, not just
    one), or None if none of them are dangerous."""
    for pattern in patterns:
        dangerous = _is_dangerous_pattern(pattern)
        if dangerous:
            return dangerous
    return None


# Value-taking xargs flags this hook is CONFIDENT consume a separate next token (GNU + BSD/macOS
# common ground: -I/-i replstr, -J replstr, -L/-l max-lines, -n max-args, -P max-procs,
# -R max-repl, -s max-chars, -a file, -d/--delimiter). Deliberately does NOT include -e/-E (GNU's
# optional/glued-only eof-string forms) — treating those as unconditionally consuming the next
# token would create the OPPOSITE bug (over-consuming `kill` itself as the flag's value).
_XARGS_VALUE_FLAGS = frozenset({
    "-I", "-i", "-J", "-L", "-l", "-n", "-P", "-R", "-s", "-a", "-d", "--delimiter",
})
# Flags this hook is confident take NO value at all.
_XARGS_BOOL_FLAGS = frozenset({
    "-0", "--null", "-o", "--open-tty", "-p", "--interactive", "-r", "--no-run-if-empty",
    "-t", "--verbose", "-x", "--exit",
})


_XARGS_KILL_NAMES = frozenset({"kill", "pkill", "killall"})


def _xargs_target_is_kill(argv: list[str]) -> bool:
    """True iff `argv` is an `xargs` invocation whose wrapped command resolves to a kill-capable
    executable (`kill`, `pkill`, `killall`), resolving through any wrapper executables (`env`,
    `sudo`, `timeout`, ...) the SAME way `_strip_wrappers` already does for a top-level stage.

    Walks past xargs's own flags to find the wrapped command (the first positional), then peels
    any wrapper prefix off of it. Without the wrapper-peel, `pgrep -f node | xargs env kill` /
    `xargs sudo kill` / `xargs timeout 5 kill` all execute `kill` but used to compare xargs's
    IMMEDIATE payload token (`env`/`sudo`/`timeout`) against the literal string `"kill"` and
    always fail that check — a wrapper always transparently passes `kill` through, exactly the
    bypass shape `_strip_wrappers` exists to close everywhere else in this hook. `_strip_wrappers`
    can raise `_WrapperOverflow` on a pathologically long wrapper chain; that propagates up to
    `_classify`'s existing fail-closed handler unchanged, same as every other `_strip_wrappers`
    call site in this file.

    If a flag is hit that this hook does NOT confidently know is value-taking vs. boolean (e.g.
    GNU's optional/glued-only `-e`/`-E`), the scan can't tell whether that flag's own value
    swallowed the NEXT token — misreading a flag's value as "the wrapped command" is exactly how
    `pgrep -f node | xargs -E '' kill` used to slip through as "not kill" (the empty string
    looked like the command, and the REAL `kill` after it was never reached). Rather than
    confidently conclude "not kill" on an ambiguous parse, fall back to checking whether
    `kill`/`pkill`/`killall` appears ANYWHERE later in argv as a bare (non-flag) word — the safe
    direction: it can only make this function classify MORE xargs invocations as kill-capable,
    subject to the SAME pattern-dangerousness check downstream, never fewer."""
    if not argv or _basename(argv[0]) != "xargs":
        return False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            if a in _XARGS_VALUE_FLAGS and i + 1 < len(argv):
                i += 2
                continue
            if a in _XARGS_BOOL_FLAGS:
                i += 1
                continue
            return any(
                _basename(t) in _XARGS_KILL_NAMES for t in argv[i + 1 :] if not t.startswith("-")
            )
        break
    payload = _strip_wrappers(argv[i:])
    return bool(payload) and _basename(payload[0]) in _XARGS_KILL_NAMES


def _stage_kills(argv: list[str]) -> bool:
    """True iff `argv` is a stage that actually issues the kill (bare `kill`, or `xargs kill`)."""
    if not argv:
        return False
    return _basename(argv[0]) == "kill" or _xargs_target_is_kill(argv)


def _classify_group(group: list[list[str]], subst_table: dict[str, str]) -> str | None:
    """Classify one pipeline group (list of raw stages). Returns the DANGEROUS pattern's source
    name if this group is a pattern-based kill of a shared name that isn't session-scoped, else
    None (safe or not a kill at all)."""
    stage_argvs = [_stage_argv(s) for s in group]

    # Direct pkill/killall — check EVERY positional operand (both accept multiple names, and a
    # flag's own value operand, e.g. `-u root`, must not be mistaken for THE pattern and hide a
    # real one sitting elsewhere in the same argument list).
    for argv in stage_argvs:
        if not argv:
            continue
        name = _basename(argv[0])
        if name in ("pkill", "killall"):
            dangerous = _any_dangerous(_positionals(argv[1:]))
            if dangerous:
                return dangerous

    # `kill` fed a pgrep/backtick substitution.
    for argv in stage_argvs:
        if not argv or _basename(argv[0]) != "kill":
            continue
        rest = argv[1:]
        if _kill_is_pid_only(rest):
            continue
        for tok in rest:
            if not tok.startswith("__PKILLGUARD_SUBST"):
                continue
            inner_text = subst_table.get(tok, "")
            inner_groups = _split_groups(inner_text)
            if inner_groups is None:
                hint = _dangerous_name_in(inner_text)
                if hint and _KILL_VERB_RE.search(inner_text):
                    raise _UnparseableSubstitution(
                        f"substitution {tok} could not be parsed and plausibly names {hint!r}"
                    )
                continue
            if not inner_groups:
                continue
            for inner_group in inner_groups:
                inner_argvs = [_stage_argv(s) for s in inner_group]
                dangerous = _any_dangerous(_patterns_in_group(inner_argvs))
                if dangerous:
                    return dangerous

    # Pipeline: ANY stage that actually performs the kill (bare `kill` or `xargs ... kill ...`),
    # fed by a pgrep/grep-family stage anywhere else in the same group — matches the documented
    # "narrow grep" incident shape. Checking every stage, not just the LAST one, matters: a kill
    # can genuinely execute in a MIDDLE stage with a trailing consumer after it (`pgrep -f node |
    # xargs kill | tee kill.log`) — the kill still runs; a trailing `tee`/`wc -l`/etc. reading its
    # stdout does not undo it. Restricting this to "only the last stage" was a real detection
    # regression (round-5 review finding), not a deliberate narrowing.
    if len(stage_argvs) >= 2 and any(_stage_kills(a) for a in stage_argvs):
        dangerous = _any_dangerous(_patterns_in_group(stage_argvs))
        if dangerous:
            return dangerous

    return None


def _patterns_in_group(stage_argvs: list[list[str]]) -> list[str]:
    """Every pattern named by EVERY pgrep/grep-family stage in `stage_argvs` (a pipeline's
    stages, already argv-resolved) — not just the first one. A filtering stage that appears
    BEFORE the real pattern in an accidental ordering (`grep -v noise | grep <pattern>`) must
    not hide that later pattern; checking every stage is the safe direction."""
    patterns: list[str] = []
    for argv in stage_argvs:
        patterns.extend(_patterns_for_stage(argv))
    return patterns


def _classify(command: str) -> tuple[str, str | None]:
    """Classify `command`. Returns (verdict, dangerous_name):
      "safe"        — no pattern-kill of a shared, unscoped name found.
      "dangerous"   — a pattern-kill of `dangerous_name` was found.
      "unparseable" — shlex could not tokenize AND the raw text plausibly contains a kill of
                      a denylisted name — fail-closed.
    """
    rewritten, subst_table = _placeholder_command(command)
    groups = _split_groups(rewritten)
    if groups is None:
        hint = _dangerous_name_in(command)
        if hint and _KILL_VERB_RE.search(command):
            return "unparseable", hint
        return "safe", None
    try:
        for group in groups:
            dangerous = _classify_group(group, subst_table)
            if dangerous:
                return "dangerous", dangerous
    except (_WrapperOverflow, _UnparseableSubstitution):
        return "unparseable", None
    return "safe", None


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _block_message(dangerous_name: str) -> str:
    what = (
        f"Refusing a pattern-based kill of \"{dangerous_name}\": `pkill -f`/`killall`/`pgrep` "
        "match against EVERY process on this machine whose command line contains that text, "
        "not just yours — this has already killed a DIFFERENT session's in-flight work twice "
        "(a code review, an e2e matrix run)."
    )
    self_serve = (
        "Use a PID-targeted `kill <pid>`, `dev stop` for this repo's dev/e2e processes, or "
        "scope the pattern to something session-unique (a worktree path, the harness's "
        "isolation-prefix directory)."
    )
    relay = (
        "If a subagent reported this block, do the kill yourself with a PID-targeted `kill <pid>` "
        "(or `dev stop`) when it is genuinely yours to stop, or send the subagent the exact PID or "
        "scoped pattern with `write agent://<id>`, then `wait` for its final report."
    )
    human = (
        "request a one-time Telegram approval: "
        'RIG_HATCH_REQUEST_PKILL_GUARD="<reason>" <command>.'
    )
    return _render_block(what, self_serve, relay, human)


def _gate_dangerous(dangerous_name: str, cwd: str, command: str) -> int:
    context = {"hook": HOOK_ID, "kind": "pattern-kill", "target": dangerous_name, "command": command}
    hatch = hatch_escalation.request_hatch_approval(HOOK_ID, context, cwd=cwd, command=command)
    if hatch.should_stop:
        if hatch.approved:
            warn(f"pattern-kill of {dangerous_name!r} approved via hatch escalation ({hatch.reason})")
            emit("allow", f"approved via hatch escalation ({hatch.reason})")
            return 0
        warn(f"pattern-kill of {dangerous_name!r} hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{_block_message(dangerous_name)}")
        return BLOCK_EXIT_CODE
    emit("block", _block_message(dangerous_name))
    return BLOCK_EXIT_CODE


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", f"{HOOK_ID}: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    if isinstance(event, dict):
        _CURRENT_EVENT["event"] = event
    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    verdict, dangerous_name = _classify(command)

    if verdict == "safe":
        emit("allow")
        return 0

    if verdict == "unparseable":
        warn("could not verify this command is not a pattern-kill of a shared name — blocking (fail-closed)")
        emit(
            "block",
            f"{HOOK_ID}: could not verify this command is not a pattern-based kill of a "
            "shared process name (fail-closed). Split it into a simpler Bash call.",
        )
        return BLOCK_EXIT_CODE

    assert dangerous_name is not None  # verdict == "dangerous" always carries a name
    return _gate_dangerous(dangerous_name, cwd, command)


if __name__ == "__main__":
    sys.exit(main())
