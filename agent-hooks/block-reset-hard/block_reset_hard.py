#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block `git reset --hard` and `git clean -f...`.

Denies a shell command that irreversibly wipes uncommitted/untracked work with zero undo:

  - `git reset --hard` (with or without a ref) — discards uncommitted TRACKED changes.
  - `git clean` with a real `-f`/`--force` flag (alone or clustered with `-d`/`-x`, in any
    short-flag order) — discards untracked files.

Both are the same failure class: a destructive rewrite of the working tree that git cannot
undo afterward (unlike `git reset` without `--hard`, which never touches the working tree,
or `git clean -n`, which only previews). This hook exists because an incident showed nothing
technically stops an agent from running either mid-session: a subagent working an unrelated
PR ran `git reset --hard` in a checkout shared with a different session and wiped that
session's uncommitted work. The incident was ACCIDENTAL, not a deliberate bypass — this
hook's real value is turning an accidental destructive reset/clean into a DELIBERATE,
LOGGED one. It previously shipped a self-service escape hatch (an env var / inline comment the
agent could set on its own command); that was security theater and was removed (Alex tg#6554).
The block is now DENY-BY-DEFAULT with the external approval_cmd extension point below.

Detection is ARGV-BASED, not a raw substring match (same discipline as block-raw-pr-merge /
block-no-verify / require-ticket-before-commit). The command is tokenized LINE BY LINE (a
newline is a command separator, same as `;` — a two-line Bash call is a common, entirely
accidental shape: `cd /repo` then `git reset --hard` on the next line is the literal incident
this hook guards against) with a word-boundary-aware `#` comment scan (a `#` mid-word, e.g.
`foo#bar`, is literal text to a real shell, NOT a comment — shlex's default commenter gets
this wrong and would truncate parsing there, silently hiding a later chained command; ported
fix from block-no-verify/require-review-before-commit), then split into shell segments (`;` /
`&&` / `||` / `|` separated). Each segment's real argv is recovered by stripping leading
shell-grouping/control-flow tokens (`(`, `{`, `if`, `while`, `case`, ...), inline `VAR=value`
env assignments, a set of common wrapper executables (`timeout`, `sudo`, `env`, `nice`,
`time`, `exec`, `command`, `setsid`, `nohup`, ... — ported from block-no-verify /
require-ticket-before-commit's wrapper table, since a destructive command hidden behind
`sudo -u user git reset --hard` or `time git clean -fd` is exactly the bypass shape those
sibling hooks already fought), and git's own global options (`-C`, `-c`, `--git-dir`, ...) so
the git subcommand is exposed even behind `git -C <dir> reset --hard`. This prevents a false
positive where a commit message, code comment, doc, or `grep` merely MENTIONS the phrase
"reset --hard" or "clean -fd" as text.

Allowed (let through — the safe alternatives this hook steers agents toward):
  - `git checkout -- <file>` / `git restore <file>` (discard specific tracked files, scoped)
  - `git reset` with no `--hard` (bare, `--mixed`, `--soft`) — never touches the working tree
  - `git clean -n` / `git clean --dry-run` (preview only)
  - `git clean` with no force flag at all (git itself refuses to delete without `-f`)
  - text that merely mentions "reset --hard" or "clean -fd" (commit message, comment, grep)

External approval (replaces the OLD self-service escape hatch): there is NO env-var
(ALLOW_GIT_RESET_HARD) and NO inline `# no-reset-guard:` bypass any more — an agent could set
either on its own command, so that "gate" was security theater (removed per Alex tg#6554).
The block is now DENY-BY-DEFAULT. A repo owner may wire `agent_hooks.approval_cmd` (a shell
command) in the committed, code-reviewed rig.yaml; when a reset --hard / clean -f is about to
be blocked this hook runs that command (with RIG_APPROVAL_* context in the child's env) and
allows ONLY on exit 0. Nothing configured = denied; a nonzero/error/timeout verdict = denied.
An agent with a genuine reason should ASK the human, not self-grant.

Known limitations — there is no longer a self-service hatch (removed — Alex tg#6554), so the
parser is the only line; the bar for what's fixed above vs. documented below remains: would a
confused, non-evasive agent produce this exact command BY ACCIDENT? Newline-separated commands
and mid-word `#` (fixed above) clear that bar; these don't:
  - `git reset --har` (an unambiguous long-option prefix git itself accepts) is not detected;
    only the literal `--hard` spelling is matched — no one accidentally abbreviates a
    destructive flag they're not trying to type in full.
  - `git clean -e -f` (a bare `-e` immediately followed by a SEPARATE `-f`-shaped token) is
    conservatively treated as a real `-f` (blocked) by this hook, even though real git parses
    that trailing token as `-e`'s exclude-pattern VALUE (confirmed empirically: `git clean -e
    -f` prints "clean.requireForce is true and -f not given: refusing to clean" — i.e. real
    git does NOT treat it as force). This is a FALSE BLOCK, not a bypass — the safe-by-default
    direction for a destructive-action guard, and not worth getopt-style value tracking for
    such an exotic input shape.
  - `git -c clean.requireForce=false clean -d` (or the same set as an AMBIENT gitconfig, not
    inline) makes a bare `clean -d` — no `-f` at all — actually delete, contradicting this
    hook's "no force flag = git refuses" assumption. Not detected: nobody sets this inline by
    accident, but if it's ALREADY in someone's ambient config, a bare `clean -d` silently
    slips past this hook for real. Worth knowing, not worth building a git-config-value
    detector for.
  - A shell alias for `git` (`alias g=git`) is not resolved — universal to this whole hook
    family (aliases only expand in an INTERACTIVE shell by default; a harness running via
    `bash -c "<command>"` doesn't expand them anyway, so this is rarely reachable in practice).
  - `env -S/--split-string '<command>'` (a re-tokenized inline command string), a nested
    shell-string interpreter (`bash -c 'git reset --hard'`), a command substitution
    (`$(git reset --hard)`), or a wrapper outside a brace group are not re-parsed — same
    documented gap as block-no-verify; all require deliberately crafted input, not an
    accidental shape.
  - An unlisted/exotic wrapper (`unshare`, `nsenter`, `firejail`, ...) is not peeled.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "closed": a parse failure or crash DENIES — a destructive reset/clean slipping
through a broken guard is exactly the failure this hook exists to stop.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess  # noqa: S404 — running the rig.yaml-configured approval command
import sys
import time
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

# Wall-clock budget for the approval_cmd fallback while gating EVERY dangerous segment
# `_classify` found in one chained command — comfortably under this hook's manifest `timeout_ms`
# (block-reset-hard.pre-bash.json) even when MULTIPLE segments each need their own `approval_cmd`
# call. It does NOT bound the RIG_HATCH_REQUEST_BLOCK_RESET_HARD live-Telegram path, which is
# allowed to run up to tg-ctl's own 900s cap (the reason timeout_ms was raised to 960000, with a 30s headroom over the 930s helper worst case). Unlike
# pin-primary-worktree (on_error=open), this hook is on_error=CLOSED — an external manifest-
# timeout kill already fails SAFE (deny) here, so this budget is not a security fix. It exists so
# a chained command where EVERY segment is legitimately approved doesn't spuriously get killed
# into a generic dispatcher-level denial instead of this hook's own clear "too many segments"
# message, and so the hook stops spending time on approval calls destined to be killed anyway.
# Before each approval_cmd call, the caller reserves the FULL `_APPROVAL_TIMEOUT_CEILING_S`
# against this budget (not just "has the deadline already passed") — a call merely allowed to
# START just before the deadline could still itself run the whole ceiling and blow the aggregate.
_MAIN_LOOP_BUDGET_S = 10.0

# Shell operators that separate independent command segments in a compound command line.
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})

# Shell grouping/control-flow tokens that may precede the real command name in a segment.
# E.g. `( git reset --hard )`, `{ git reset --hard; }`, `while git clean -fd; do ...`.
# Mirrors block-no-verify / require-ticket-before-commit's `_LEADING_SHELL_NOISE`.
_LEADING_SHELL_NOISE = frozenset(
    {"(", "{", "!", "if", "then", "do", "else", "elif", "while", "until", "for", "case"}
)
_MAX_LEADING_NOISE = 16  # cap so an all-noise segment can't loop

# A VAR=value inline environment assignment that may precede the executable.
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Wrapper executables that prefix the REAL command and pass the rest through unchanged.
# Ported from block-no-verify / require-ticket-before-commit's wrapper table — a destructive
# `git reset --hard`/`git clean -f...` hidden behind `sudo -u user`, `time`, `exec`, `command`,
# `setsid`, `nohup`, etc. is exactly the bypass shape those sibling hooks already fought.
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer",
    "command", "sudo", "doas", "exec", "taskset", "chrt", "setpriv", "flock", "runuser",
})
_MAX_WRAPPER_NESTING = 16

# Wrappers that take a LEADING POSITIONAL operand before the real command (timeout's
# duration, chrt's priority, taskset's mask, flock's lockfile).
_OPERAND_DROP_WRAPPERS = frozenset({"timeout", "taskset", "chrt", "flock"})
# Operand-drop wrappers whose operand is a MANDATORY arbitrary string that may legitimately be
# `git` (flock's lockfile) — dropped even when literally `git`.
_MANDATORY_OPERAND_WRAPPERS = frozenset({"flock"})

# Per-wrapper flags taking a SEPARATE value, so the NEXT token is that value, not the wrapped
# command (`sudo -u USER git ...`, `timeout -s SIG ...`). Ported verbatim from
# block-no-verify/require-ticket-before-commit's audited table (the comments there record why
# each entry exists — a missing separate-operand flag silently shifts the parse and re-opens
# the bypass).
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
# INVARIANTS (a loud import-time failure, not a silent bypass): every operand-drop /
# value-flag / mandatory-operand wrapper must first be a recognized wrapper, else its config
# is dead and the wrapped command slips through (argv[0] != git → not gated).
if not _OPERAND_DROP_WRAPPERS <= _WRAPPERS:
    raise RuntimeError(f"operand-drop wrappers missing from _WRAPPERS: {_OPERAND_DROP_WRAPPERS - _WRAPPERS}")
if not _MANDATORY_OPERAND_WRAPPERS <= _OPERAND_DROP_WRAPPERS:
    raise RuntimeError(f"mandatory-operand wrappers not in operand-drop: {_MANDATORY_OPERAND_WRAPPERS - _OPERAND_DROP_WRAPPERS}")
if not set(_WRAPPER_VALUE_FLAGS) <= _WRAPPERS:
    raise RuntimeError(f"value-flag wrappers missing from _WRAPPERS: {set(_WRAPPER_VALUE_FLAGS) - _WRAPPERS}")


def _short_value_letters(value_flags: frozenset[str]) -> frozenset[str]:
    """The single-letter forms (`-u` -> `u`) of a wrapper's value-flags, for a combined short
    cluster ending in a value-letter (`sudo -up alice` = `-u -p alice`... `-uP` = `-u P`)."""
    return frozenset(f[1] for f in value_flags if len(f) == 2 and f.startswith("-"))


_WRAPPER_SHORT_VALUE_LETTERS = {w: _short_value_letters(f) for w, f in _WRAPPER_VALUE_FLAGS.items()}

# git global options that take a separate value and may appear before the subcommand,
# e.g. `git -C /path reset --hard`. Mirrors block-no-verify's `_GIT_GLOBAL_VALUE_FLAGS`.
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

# Reset-hard hint: used to gate fail-closed on unbalanced-quote parse errors so that a
# benign command with an unbalanced quote is NOT blocked just because shlex can't parse it.
# Only if the raw string plausibly contains a reset --hard or clean -f invocation do we
# treat a parse error as fail-closed.
_RESET_HARD_HINT = re.compile(r"\bgit\b.*\breset\b.*\bhard\b", re.DOTALL)
_CLEAN_FORCE_HINT = re.compile(r"\bgit\b.*\bclean\b.*(-f|--force)", re.DOTALL)

def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-reset-hard: {msg}\n")


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/git` -> `git`, `./git` -> `git`."""
    return tok.rsplit("/", 1)[-1]


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
    """Tokenize ONE physical line. ``punctuation_chars=True`` splits real shell separators
    (`;`, `&&`, `||`, `|`, ...) into standalone tokens while honoring quotes.

    Comments are handled MANUALLY (shlex's ``commenters`` is disabled) — shlex's built-in
    commenter cuts at ANY unquoted `#`, even mid-word, which would truncate parsing at a
    benign `foo#bar` and silently hide a LATER `&& git reset --hard` on the same line (an
    accidental, non-adversarial miss — the exact class this hook exists to catch, ported
    from block-no-verify/require-review-before-commit's audited fix for the same bug).

    A token that starts with a REAL, unquoted `#` is a comment; drop it and the rest of the
    line. Whether a `#` is "real" can NOT be decided from the POSIX-mode (quote-stripped)
    token alone: a quoted `'# heading'` dequotes to the exact same string `# heading` that a
    bare, unquoted `# heading` produces, so a lone `tok.startswith("#")` check on the
    dequoted token can't tell a real comment from a quoted argument that merely starts with
    `#` — e.g. `echo '# heading' && git reset --hard` would be misparsed as ending at the
    (fake) comment, hiding the chained reset. A second, quote-PRESERVING (``posix=False``)
    tokenization is run in parallel so the check can be made on the RAW token (quotes/escapes
    intact): only a token that ITSELF starts with `#` in that raw form is a genuine, unquoted
    comment marker. Returns None on unbalanced quotes, or if the two passes disagree on token
    count (can't reliably align raw-to-dequoted; caller treats this as fail-closed).
    """
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
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a
    NEWLINE is a command separator (a `;` token between lines). A two-line Bash call
    (`cd /repo` then `git reset --hard` on the next line) is a common, entirely accidental
    shape — a single flat shlex pass over the whole string misses it (the newline is just
    whitespace to shlex, so `cd`/`echo` on line one becomes argv[0] and the real command on
    line two is invisible). Returns None only if a chunk can never balance (fail-closed).
    """
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


def _split_segments(command: str) -> list[list[str]]:
    """Tokenize `command` (possibly multi-line) and split into per-segment token lists. Each
    segment is one independent command. Raises ValueError on unbalanced quotes/unterminated
    lines (caller treats this as fail-closed).
    """
    tokens = _tokenize(command)
    if tokens is None:
        raise ValueError("unbalanced quotes or unterminated line")
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
    """Drop leading subshell/brace openers and control keywords so a command introduced by
    them is recovered: `(git reset --hard)` -> `git reset --hard`. Capped so an all-noise
    segment can't loop.
    """
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_inline_env(argv: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off `argv`, returning the rest starting at the
    executable (`HUSKY=0 sudo git reset --hard` -> `["sudo", "git", "reset", "--hard"]`)."""
    while argv and _INLINE_ENV.match(argv[0]):
        argv = argv[1:]
    return argv


def _cluster_takes_next_value(tok: str, short_letters: frozenset[str]) -> bool:
    """True when a short cluster's LAST char is a value-letter for this wrapper AND no
    earlier value-letter already consumed the cluster's tail (`-iP` -> True; `-Px` -> False)."""
    body = tok[1:]
    if not body or body[-1] not in short_letters:
        return False
    for ch in body[:-1]:
        if ch in short_letters:
            return False
    return True


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    """Drop one wrapper's own option flags + (for an operand-drop wrapper) its leading
    positional operand, returning argv positioned at the wrapped command. A flag in this
    wrapper's value set (`sudo -u alice`) consumes the following token UNCONDITIONALLY — even
    when it is literally `git` (`sudo -u git git reset --hard`: the user is named "git", the
    SECOND git is the executable).
    """
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
        if wrapper in _MANDATORY_OPERAND_WRAPPERS or _basename(argv[i]) != "git":
            i += 1
    return argv[i:]


class _WrapperOverflow(Exception):
    """Raised when a wrapper chain is still unresolved past `_MAX_WRAPPER_NESTING` — the real
    command was never reached. This hook is `on_error: closed`, so an unresolvable wrapper
    chain must be treated as UNVERIFIABLE and fail closed (block), NOT silently treated as
    safe just because peeling gave up. A sibling hook (`require-ticket-before-commit`) is
    `on_error: open` and deliberately returns argv unchanged past the cap — that choice is
    wrong FOR THIS HOOK, where "couldn't check" must mean "don't allow it".
    """


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading wrapper executables (`timeout 60`, `sudo -u alice`, `env FOO=bar`, ...) so
    the REAL command beneath is what we inspect. Stops as soon as the head is no longer a
    known wrapper, so a real `git` is never skipped. `env -S '<command>'` split-string is NOT
    recursed here (a documented best-effort limitation, matching the sibling hooks). Raises
    `_WrapperOverflow` past the nesting cap (fail-closed — see the class docstring).
    """
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            raise _WrapperOverflow(f"wrapper chain exceeds {_MAX_WRAPPER_NESTING} levels")
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        argv = _strip_inline_env(argv)  # `sudo FOO=bar git ...` / `env HUSKY=0 git ...`
    return argv


def _segment_argv(segment: list[str]) -> list[str]:
    """Return the real argv by stripping leading shell noise, inline env assignments, and
    known command wrappers, so `( TIMEOUT=1 timeout 5 sudo git reset --hard )` resolves to
    `["git", "reset", "--hard"]`.
    """
    argv = _strip_leading_shell_noise(segment)
    argv = _strip_inline_env(argv)
    argv = _strip_wrappers(argv)
    return argv


def _strip_git_globals(argv: list[str]) -> list[str]:
    """Given argv starting at the git executable, return argv starting at the subcommand,
    skipping git's own global options (`-C <dir>`, `-c key=val`, `--git-dir=...`,
    `--no-pager`, glued `-C<dir>`/`-c<k>=<v>`, ...).
    """
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if not tok.startswith("-"):
            break
        if tok in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2  # flag + its separate value
            continue
        if tok.startswith(("-C", "-c")) and len(tok) > 2:
            i += 1  # glued form: -C/path, -ckey=val
            continue
        i += 1  # any other global flag (--no-pager, -p, --literal-pathspecs, ...)
    return argv[i:]


def _git_subcommand(segment: list[str]) -> list[str] | None:
    """Return the git subcommand argv (e.g. `["reset", "--hard"]`) for this segment, or
    None if the segment is not a git invocation.
    """
    argv = _segment_argv(segment)
    if not argv or _basename(argv[0]) != "git":
        return None
    return _strip_git_globals(argv)


def _git_dash_c_dir(segment: list[str]) -> str | None:
    """The effective ``git -C <dir>`` directory a git segment targets, or None. So a destructive
    command aimed at ANOTHER repo (`git -C ../other reset --hard`) has its approval resolved
    against THAT repo's rig.yaml, not the shell cwd — mirroring pin-primary-worktree's cross-repo
    `-C` handling. argv is peeled of wrappers/inline-env first; only a real `git` invocation is
    inspected. git APPLIES SUCCESSIVE `-C` cumulatively (`git -C a -C b` == chdir a then b), so
    the values are joined in order (an absolute later `-C` resets the path, per pathlib join).

    SCOPE (documented limitation, same class as pin-primary-worktree's `cd other-repo` note):
    only `git -C` is followed. A cwd change made by a WRAPPER or a chained `cd` — `env -C <dir>`,
    `sudo --chdir <dir>`, `cd other && git reset --hard` — is NOT followed; approval for such a
    command resolves against the shell cwd's rig.yaml, not the relocated target. Deny-by-default
    still holds (an unconfigured cwd repo denies); the only residual is a cwd repo whose
    configured approver could then authorize a wrapper-relocated command against another repo.
    """
    argv = _segment_argv(segment)
    if not argv or _basename(argv[0]) != "git":
        return None
    dirs: list[str] = []
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if not tok.startswith("-"):
            break
        if tok == "-C" and i + 1 < n:
            dirs.append(argv[i + 1])
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            dirs.append(tok[2:])  # glued form: -C/path
            i += 1
            continue
        if tok in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-c") and len(tok) > 2:
            i += 1  # glued -ckey=val
            continue
        i += 1
    if not dirs:
        return None
    result = Path(dirs[0])
    for d in dirs[1:]:
        result = result / d  # pathlib: an absolute right operand resets the path (matches git)
    return str(result)


def _effective_cwd(base_cwd: str, cwd_override: str | None) -> str:
    """Resolve a segment's ``git -C <dir>`` override against the shell cwd, so approval config
    is read from the repo actually being acted on (SYNC with pin-primary-worktree)."""
    if not cwd_override:
        return base_cwd
    p = Path(cwd_override)
    if not p.is_absolute():
        p = Path(base_cwd or ".") / p
    return str(p)


def _is_reset_hard(segment: list[str]) -> bool:
    """Return True iff this segment is `git reset --hard [ref]` (globals/wrappers peeled)."""
    sub = _git_subcommand(segment)
    return bool(sub) and sub[0] == "reset" and "--hard" in sub[1:]


def _has_force_flag(tokens: list[str]) -> bool:
    """Return True iff `tokens` (the args after `git clean`) contain a real `-f`/`--force`.

    Handles clustered short flags (`-fd`, `-df`, `-fdx`, `-xdf`) in any order, and stops
    scanning a cluster at `e` since `-e<pattern>` consumes the rest of that token as its
    exclude-pattern VALUE, not further flag letters (`-ef*.o"` is `-e` with value `f*.o"`,
    not `-e` plus `-f`; `-fe*.o"` IS force — `f` comes before `e`).
    """
    for tok in tokens:
        if tok == "--":
            break  # remaining tokens are pathspecs, not flags
        if tok == "--force":
            return True
        if tok.startswith("--"):
            continue  # another long flag (--dry-run, --exclude=..., --interactive, ...)
        if tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch == "f":
                    return True
                if ch == "e":
                    break  # -e consumes the rest of this token as its pattern value
    return False


def _is_clean_force(segment: list[str]) -> bool:
    """Return True iff this segment is `git clean` with a real force flag (globals peeled)."""
    sub = _git_subcommand(segment)
    return bool(sub) and sub[0] == "clean" and _has_force_flag(sub[1:])


def _classify(command: str) -> tuple[str, list[tuple[str, str | None]]]:
    """Classify `command` for the two dangerous forms this hook guards.

    Returns (verdict, findings):
      verdict "safe"        — command parsed cleanly, no reset --hard / clean -f... found;
                              `findings` is `[]`.
      verdict "dangerous"   — command parsed cleanly and ONE OR MORE segments are one of the
                              two forms; `findings` has one `(kind, cwd_override)` entry PER
                              dangerous segment. A chained command can carry more than one
                              (e.g. `git -C a reset --hard ; git -C b clean -fd`) — ALL are
                              collected here (the loop below never returns early on the first
                              match) so the caller can require approval for every single one,
                              not just the first.
      verdict "unparseable" — shlex could not tokenize (unbalanced quotes) AND the raw text
                              plausibly contains one of the dangerous forms — fail-closed;
                              `findings` has exactly one `(kind, None)` entry.
    Each finding's `kind` is "reset --hard" or "clean -f...". Each finding's `cwd_override` is
    THAT segment's `git -C <dir>` target (else None), so approval config for that finding is
    read from the repo it actually targets, not the shell cwd.
    """
    try:
        segments = _split_segments(command)
    except ValueError:
        if _RESET_HARD_HINT.search(command):
            return "unparseable", [("reset --hard", None)]
        if _CLEAN_FORCE_HINT.search(command):
            return "unparseable", [("clean -f...", None)]
        return "safe", []
    findings: list[tuple[str, str | None]] = []
    for seg in segments:
        try:
            if _is_reset_hard(seg):
                findings.append(("reset --hard", _git_dash_c_dir(seg)))
            elif _is_clean_force(seg):
                findings.append(("clean -f...", _git_dash_c_dir(seg)))
        except _WrapperOverflow:
            # Can't resolve THIS segment's real command through the wrapper chain — it MIGHT
            # be a destructive reset/clean hidden behind it. Fail closed for the WHOLE command
            # (an unresolvable segment could be hiding either dangerous form).
            return "unparseable", [("wrapper chain too deep to verify", None)]
    if not findings:
        return "safe", []
    return "dangerous", findings


def _find_rig_yaml(cwd: str) -> Path | None:
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None). SYNC
    with pin-primary-worktree / worktree-only-writes — same walk-up helper."""
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


# ── external-approval extension point (SYNC: the functions below — _agent_hooks_raw,
# _agent_hooks_str, _approval_timeout_s, _resolve_approval_config, _kill_process_group,
# _request_approval — are BYTE-IDENTICAL in pin-primary-worktree/pin_primary_worktree.py and
# block-reset-hard/block_reset_hard.py; keep the copies in lockstep. Each agent-hook is a
# standalone script by convention, no shared import between hook dirs. (In pin, _agent_hooks_raw
# is defined just above _agent_hooks_bool, which reuses it — same text, different placement.)
# Replaces the old self-service escape hatch (an env var / inline sentinel the constrained agent
# could set on itself — security theater; Alex tg#6554) with a deny-by-default gate that only a
# committed, code-reviewed rig.yaml can open. ──────────────────────────────────────────────────

_APPROVAL_CMD_KEY = "approval_cmd"
_APPROVAL_TIMEOUT_KEY = "approval_cmd_timeout_s"
_APPROVAL_TIMEOUT_DEFAULT_S = 5.0
# Hard ceiling on the approval subprocess timeout, independent of what rig.yaml asks for. The
# dispatcher (lib/cc_hook_bridge/dispatch.py) enforces this hook's manifest timeout_ms as a HARD
# kill on the WHOLE hook process. If approval_cmd_timeout_s were honored unbounded, a repo owner
# setting e.g. `approval_cmd_timeout_s: 30` plus a hanging script would let the outer dispatcher
# kill the hook at its manifest budget — and for a hook with on_error=open that resolves to a
# SILENT ALLOW (the exact self-grant class this whole change removes). Clamp the effective
# timeout well under the manifest budget so a misconfig can never become a bypass.
_APPROVAL_TIMEOUT_CEILING_S = 6.0
_APPROVAL_DETAIL_CAP = 500  # cap the approval-cmd stdout captured as the logged reason


def _agent_hooks_raw(rig_yaml_text: str, key: str) -> str | None:
    """The raw (comment-stripped, quote-stripped) string value of ``agent_hooks.<key>``, or
    None if the key is absent from the top-level ``agent_hooks:`` block. The single
    block-scoped, indentation-aware line scanner shared by ``_agent_hooks_bool`` and
    ``_agent_hooks_str``. SYNC with worktree_only_writes / orchestrator_stays_thin — keep the
    SCANNER BEHAVIOR in lockstep if it ever changes (those two copies still inline the same
    logic inside their own ``_agent_hooks_bool``; behavior here is identical, just factored)."""
    in_block = False
    child_indent: int | None = None
    for raw in rig_yaml_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        head = line.strip().split(":", 1)[0].strip()
        if indent == 0:
            in_block = head == "agent_hooks"
            child_indent = None
            continue
        if not in_block:
            continue
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        if head == key and ":" in line.strip():
            return line.strip().split(":", 1)[1].strip().strip("\"'")
    return None


def _agent_hooks_str(rig_yaml_text: str, key: str, default: str | None) -> str | None:
    """Read a STRING value for ``agent_hooks.<key>`` from rig.yaml (quotes stripped), or
    ``default`` when the key is absent or its value is blank. Shares ``_agent_hooks_raw``'s
    block-scoped scanner with ``_agent_hooks_bool`` rather than duplicating it."""
    raw = _agent_hooks_raw(rig_yaml_text, key)
    if raw is None or raw == "":
        return default
    return raw


def _approval_timeout_s(rig_yaml_text: str) -> float:
    """The effective approval-cmd subprocess timeout: ``agent_hooks.approval_cmd_timeout_s``
    (default 5.0s), clamped to ``_APPROVAL_TIMEOUT_CEILING_S`` and floored to the default on a
    non-positive or unparseable value."""
    raw = _agent_hooks_str(rig_yaml_text, _APPROVAL_TIMEOUT_KEY, None)
    if raw is None:
        return _APPROVAL_TIMEOUT_DEFAULT_S
    try:
        val = float(raw)
    except ValueError:
        warn(f"invalid {_APPROVAL_TIMEOUT_KEY}={raw!r}; using {_APPROVAL_TIMEOUT_DEFAULT_S}s")
        return _APPROVAL_TIMEOUT_DEFAULT_S
    if val <= 0:
        return _APPROVAL_TIMEOUT_DEFAULT_S
    return min(val, _APPROVAL_TIMEOUT_CEILING_S)


def _resolve_approval_config(cwd: str) -> tuple[str | None, float, str | None]:
    """Read the repo's committed rig.yaml (walk up from ``cwd``) for the external-approval
    knobs. Returns ``(approval_cmd, timeout_s, rig_root)``. ``approval_cmd`` is None when
    unconfigured (the default-DENY path — no subprocess is ever spawned). ``rig_root`` is the
    directory holding rig.yaml, used as the child's cwd so a repo-local approval script
    resolves relative paths."""
    root = _find_rig_yaml(cwd)
    if root is None:
        return None, _APPROVAL_TIMEOUT_DEFAULT_S, None
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return None, _APPROVAL_TIMEOUT_DEFAULT_S, None
    cmd = _agent_hooks_str(text, _APPROVAL_CMD_KEY, None)
    if cmd is not None:
        cmd = cmd.strip() or None
    return cmd, _approval_timeout_s(text), str(root)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the approval command's whole process group, then reap it. ``start_new_session``
    makes the child a group leader, so a backgrounded GRANDCHILD that inherited the stdout pipe
    is killed too. Without this, ``subprocess``'s post-timeout pipe drain can block FOREVER on
    that still-open inherited pipe — defeating the internal timeout, so the dispatcher's
    manifest-timeout kill fires instead and (for an on_error=open hook) resolves to a SILENT
    ALLOW, the exact self-grant class this change removes."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _request_approval(cwd: str, context: dict[str, str]) -> tuple[bool, str | None]:
    """External-approval gate that REPLACES the old self-service escape hatch. Returns
    ``(approved, detail)``.

    Resolution:
      - approval_cmd UNCONFIGURED in the repo's committed rig.yaml → ``(False, None)``
        immediately, NO subprocess. This is the default-DENY path: "nothing configured" means
        closed, never an automatic bypass.
      - CONFIGURED → run it. Exit 0 → approved (stdout, trimmed + capped, is the logged
        ``detail``). A nonzero exit, ANY subprocess exception, OR a timeout → NOT approved. An
        approval-cmd failure is ALWAYS resolved to deny HERE, inside this function — it never
        falls through to the hook's own ``on_error`` policy (that policy is for the hook's OWN
        plumbing failing, not for an approval verdict; a broken/hanging approval_cmd must mean
        "denied", regardless of whether this hook is on_error=open or on_error=closed).

    The child runs in its OWN process group (``start_new_session=True``) and, on timeout/error,
    the whole group is SIGKILLed (see ``_kill_process_group``) — so a well-meaning-but-broken
    approval_cmd that backgrounds a child holding the stdout pipe can't hang the hook past its
    internal timeout and thereby reach the dispatcher's fail-open.

    TRUST BOUNDARY (why ``shell=True`` is deliberate and safe here): the command STRING comes
    ONLY from ``agent_hooks.approval_cmd`` in rig.yaml — never from the agent's live command,
    never from the offending bash command being checked. The security rests on rig.yaml being a
    REVIEWED config that changes go through: an agent that can already edit-and-commit rig.yaml
    is outside this hook's threat model, exactly as for the sibling ``worktree_only`` knob that
    reads the same file. (This reads the working-tree copy and does NOT itself verify the file
    is committed/clean — that is left to code review, deliberately not re-implemented here; the
    distinction vs. the OLD hatch is that a bypass now requires a reviewed config change, not an
    env var / comment the agent invents per-command.) Dynamic data about WHAT is being approved
    (target, kind, cwd, the raw command) is passed to the child as ``RIG_APPROVAL_*`` environment
    variables ONLY, never string-interpolated into the command, so there is no injection surface
    from agent-controlled data.
    """
    cmd, timeout_s, rig_root = _resolve_approval_config(cwd)
    if not cmd:
        return False, None
    child_env = {**os.environ}
    child_env["RIG_APPROVAL_HOOK"] = context.get("hook") or ""
    child_env["RIG_APPROVAL_KIND"] = context.get("kind") or ""
    child_env["RIG_APPROVAL_TARGET"] = context.get("target") or ""
    child_env["RIG_APPROVAL_CWD"] = cwd or ""
    child_env["RIG_APPROVAL_COMMAND"] = context.get("command") or ""
    try:
        proc = subprocess.Popen(  # noqa: S602 — shell=True on a COMMITTED rig.yaml string only (see trust boundary)
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,  # never inherit the hook's event-pipe stdin: a script that reads stdin gets EOF, not a hang-to-timeout
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=rig_root or None,
            env=child_env,
            start_new_session=True,  # own process group → a backgrounded child can't hold the pipe past timeout
        )
    except (OSError, ValueError) as exc:
        warn(f"approval_cmd failed to launch ({exc}) — denying")
        return False, None
    try:
        out, _err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        warn(f"approval_cmd timed out after {timeout_s:.1f}s — denying")
        return False, None
    except (OSError, ValueError) as exc:
        _kill_process_group(proc)
        warn(f"approval_cmd errored while running ({exc}) — denying")
        return False, None
    if proc.returncode != 0:
        warn(f"approval_cmd denied (exit {proc.returncode})")
        return False, None
    detail = (out or "").strip()[:_APPROVAL_DETAIL_CAP] or None
    return True, detail


def _block_message(kind: str) -> str:
    """The refusal text for an unapproved dangerous segment of `kind` ("reset --hard" or
    "clean -f..."). Factored out of `main()` so `_gate_dangerous_segments` — which may need to
    emit this once per dangerous segment in a chained command — doesn't duplicate the literal.
    """
    return (
        f"Refusing a `git {kind}`: it irreversibly wipes uncommitted/untracked work with no "
        "undo. Use a scoped, reversible alternative instead — `git checkout -- <file>` / "
        "`git restore <file>` for tracked files, `git clean -n` to preview untracked files "
        "first.\nThere is NO automatic bypass and NO self-service escape hatch. Do NOT try to "
        "self-grant via ALLOW_GIT_RESET_HARD or a `# no-reset-guard:` comment — both were "
        "removed (an agent setting its own bypass is security theater; removed per Alex "
        "tg#6554). If you genuinely need this, ASK the human directly (your usual channel to "
        "Alex), or request a one-time Telegram approval by setting "
        'RIG_HATCH_REQUEST_BLOCK_RESET_HARD="<written justification>" (deny-by-default; a '
        "bare 1 is rejected) — asking is fine, self-granting is not. A repo owner can also "
        "wire a real external-approval path via agent_hooks.approval_cmd in rig.yaml; "
        "unconfigured means denied."
    )


def _gate_dangerous_segments(
    findings: list[tuple[str, str | None]], cwd: str, command: str
) -> int:
    """Request approval for EVERY dangerous segment `_classify` found — not just the first.

    Approving one segment must never let a LATER, unapproved dangerous segment in the same
    chained command run unchecked (e.g. `git -C approved reset --hard ; git -C other clean
    -fd`): the first finding that fails approval blocks the WHOLE command immediately, without
    even requesting approval for any finding after it. Only if EVERY finding is approved does
    this emit a single `allow`, whose message lists each approval.

    Bounded by `_MAIN_LOOP_BUDGET_S` (see its module-level comment): a chained command with
    enough dangerous segments could otherwise sum to more approval-cmd time than this hook's
    manifest timeout allows, spuriously denying a command where every segment WAS approved.
    """
    approved_notes: list[str] = []
    deadline = time.monotonic() + _MAIN_LOOP_BUDGET_S
    for kind, cwd_override in findings:
        eff_cwd = _effective_cwd(cwd, cwd_override)
        context = {"hook": "block-reset-hard", "kind": kind, "target": "", "command": command}

        # RIG_HATCH_REQUEST_BLOCK_RESET_HARD (live Telegram ask) is checked first and is not
        # bounded by _MAIN_LOOP_BUDGET_S/_APPROVAL_TIMEOUT_CEILING_S — a human approval round-trip
        # legitimately runs up to tg-ctl's 900s cap, which is why this hook's manifest timeout_ms
        # was raised to 960000. It only "stops" (returns should_stop=True) when an actual hatch
        # request was made; an unset env var falls through to the approval_cmd budget below.
        hatch = hatch_escalation.request_hatch_approval(
            "block-reset-hard", context, cwd=eff_cwd, command=command
        )
        if hatch.should_stop:
            if hatch.approved:
                warn(f"{kind} approved via hatch escalation ({hatch.reason})")
                approved_notes.append(f"{kind} approved via hatch escalation ({hatch.reason})")
                continue
            warn(f"{kind} hatch escalation denied: {hatch.reason}")
            emit(
                "block",
                f"hatch escalation denied: {hatch.reason}\n{_block_message(kind)}",
            )
            return BLOCK_EXIT_CODE

        # Reserve the FULL approval-cmd ceiling before starting this call — a call merely
        # allowed to start just before `deadline` could still itself run the whole ceiling.
        if time.monotonic() + _APPROVAL_TIMEOUT_CEILING_S >= deadline:
            emit(
                "block",
                "block-reset-hard: too many chained dangerous segments to check safely within "
                "this hook's time budget — denying (fail-closed) rather than risk spurious "
                "behavior from an external timeout. Split this into separate Bash calls so "
                "each `git reset --hard`/`git clean -f...` is approved individually.",
            )
            return BLOCK_EXIT_CODE
        approved, detail = _request_approval(eff_cwd, context)
        if not approved:
            emit("block", _block_message(kind))
            return BLOCK_EXIT_CODE
        warn(f"{kind} approved via approval_cmd ({detail})")
        approved_notes.append(f"{kind} approved via external approval_cmd ({detail})")

    emit("allow", "; ".join(approved_notes))
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-reset-hard: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    verdict, findings = _classify(command)

    if verdict == "safe":
        emit("allow")
        return 0

    if verdict == "unparseable":
        kind = findings[0][0]
        warn(f"could not verify command ({kind}) — blocking (fail-closed)")
        emit(
            "block",
            f"block-reset-hard: could not verify this command is not a destructive "
            f"`git reset --hard`/`git clean -f...` ({kind}) — blocking (fail-closed).",
        )
        return BLOCK_EXIT_CODE

    # verdict == "dangerous": one or more cleanly-parsed segments are a real reset --hard /
    # clean -f... — every one of them must be approved individually (see
    # _gate_dangerous_segments); approving the first must never wave the rest through.
    return _gate_dangerous_segments(findings, cwd, command)


if __name__ == "__main__":
    sys.exit(main())
