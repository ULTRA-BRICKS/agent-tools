#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw PR merge that bypasses ship.

Denies a shell command that merges a PR directly, because that skips the project's ship gate:
`gh ship <PR>` (the green-CI-gated merge + mandatory-screenshot command) is the only sanctioned
path. A raw merge lands code without the green-CI check and the required-screenshot check —
exactly the gates ship exists to enforce. The blocked routes (all when `gh` is the ACTUALLY-invoked
command) are `gh pr merge` (incl. `--admin` and a `gh -R o/r` global flag), the `gh api` REST merge
(`…/pulls/<n>/merge` with a PUT/POST method), and a `gh api graphql` merge mutation
(`mergePullRequest` / `enablePullRequestAutoMerge`). Each is also caught when hidden in a command or
process substitution — `` `…` ``, `$( … )` (even inside double quotes), `<( … )` — whose body a real
shell executes; the body is extracted and re-scanned (#248).

This is the enforcement counterpart of the doc-only "use `gh ship`, never a raw merge"
rule: advice in AGENTS.md cannot stop an autonomous (auto-mode) agent from running
`gh pr merge`, so the rule only becomes real as a mid-session guard.

Detection is ARGV-BASED, not a raw substring match.  The command is parsed via shlex
into shell segments (`;` / `&&` / `||` / `|` separated), and each segment is inspected
for `basename(argv[0])=="gh" && argv[1]=="pr" && argv[2]=="merge"` after stripping
leading inline VAR=value assignments and shell grouping tokens (`(`, `{`).  Using
basename handles path-qualified invocations (`/usr/local/bin/gh pr merge`).  This
prevents a false positive where the body of an unrelated command (e.g. `gh pr create
--body "run gh pr merge to land it"`) triggers the guard — with a raw regex that body
text would match and the command would be blocked even though no actual merge is
occurring. If unbalanced quotes prevent a full parse, the raw merge-hint fallback is
likewise gated on a best-effort argv prefix resolving to `gh`, a shell / `eval`, or a known
wrapper around one; merge-like prose in an unparseable non-gh argument stays allowed (#289).

Allowed (let through):
  - `gh ship <PR>` / a `gh alias` that runs ship
  - `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
  - any non-merge `gh pr` subcommand (view, list, checkout, create, comment, ...)
  - a PROSE mention of "gh pr merge" as text — in an argument (`gh pr create --body "run gh
    pr merge to land it"`), a commit message, or a heredoc body — where `gh` is NOT the
    command word (argv[0]) of a segment / body line

Deliberate over-block (a rare, accepted false-positive — the SAFE direction for a security gate):
a `gh pr merge` sitting at the EXECUTABLE position (argv[0]) of a HEREDOC BODY line IS blocked,
even though a heredoc body is data, not an executed command. This is defense-in-depth: the command
parser cannot perfectly classify every shell construct, and a crafted heredoc can plant a matching
terminator to make a real merge LOOK like body text (`(( 0 << merge ))` / `gh pr merge 1` / `merge`).
Rather than chase every mis-classification, any executable-position `gh pr merge` on a skipped body
line is re-injected and blocked. A legitimate heredoc almost never opens a line with a literal
`gh pr merge`; a prose mention (gh not at argv[0]) still passes. This is a conscious divergence from
the "heredoc bodies are pure data" convention, made because the cost of a bypass here is a gate
breach and the cost of the false-positive is re-phrasing a heredoc (or using the Telegram hatch).
An expandable GraphQL field key or bare post-endpoint argument is likewise blocked because it may
become a merge-carrying `query=` field or arbitrary flags only after the hook has run (#284).

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

# Shell operators that separate independent command segments in a compound command line.
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})

# Shell grouping/control-flow tokens that may precede the real command name in a segment.
# E.g. `( gh pr merge 5 )` or `{ gh pr merge 5; }`.
_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "then", "do", "else", "elif"})

# A VAR=value inline environment assignment that may precede the executable.
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Merge-hint pattern: used to gate fail-closed on unbalanced-quote parse errors. The fallback also
# requires a best-effort argv prefix that resolves to a merge-capable invoked head; otherwise prose
# in an unrelated command's unparseable argument could match this deliberately broad regex (#289).
_MERGE_HINT = re.compile(
    r"\bgh\b.*(\bpr\b.*\bmerge\b|pulls/[^/]+/merge|mergePullRequest|enablePullRequestAutoMerge"
    r"|enqueuePullRequest|mergeBranch)",
    re.DOTALL,
)

# ── gh api merge-route detection (#248) ──────────────────────────────────────────────────────
# `gh api` reaches the same PR-merge that `gh pr merge` does, via two routes that also skip ship:
#   REST:    `gh api …/pulls/<n>/merge` with a write method (PUT/POST); a GET is a status read.
#   GraphQL: `gh api graphql` running a `mergePullRequest` / `enablePullRequestAutoMerge` mutation.
# `gh` global flags (`-R owner/repo`, `--hostname h`) may sit before the `pr merge` / `api`
# subcommand and must be skipped (`gh -R o/r pr merge 5` was evaded by the argv[1]=='pr' check).
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
# Wrapper commands that PREFIX and then EXECUTE another command (`env FOO=x gh …`, `sudo -u root gh
# …`, `timeout 60 gh …`). Rather than model each wrapper's flag arities, scan the remaining tokens
# for the wrapped invoked head — an `env gh pr merge` is an ACTUALLY-invoked merge, the same threat
# class this hook covers. This is a BEST-EFFORT list of common exec-wrappers, NOT exhaustive: an
# obscure wrapper not listed here (and deliberate circumvention generally) is a documented residual
# — the scan must stay keyed to KNOWN executors, because treating ANY leading token as a wrapper
# would re-block a prose mention (`see the gh pr merge docs`) that anchoring on the invoked command
# exists to allow.
_WRAPPERS = frozenset(
    {"env", "command", "builtin", "exec", "sudo", "doas", "nice", "ionice", "stdbuf", "nohup",
     "setsid", "time", "timeout", "xargs", "flock", "chronic", "firejail", "strace", "ltrace",
     "valgrind", "taskset", "setarch", "numactl", "chrt", "unbuffer", "proxychains", "proxychains4",
     "catchsegv", "setpriv", "rlwrap", "watch", "script", "torify", "cpulimit"}
)
# Interpreters that run a command from a STRING argument (not the token stream): a shell's `-c
# <cmd>` and `eval <args…>`. A merge hidden in that quoted string can't be reached by the wrapper
# token see-through, so the string is extracted and re-scanned as a command (`bash -c 'gh pr merge
# 1'`, `eval "gh pr merge 1"`).
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh"})
# A shell short-option group that includes `c` (`-c`, `-cx`, `-xc`) — its following token is the
# command string. Over-block: scan the next token regardless of `c`'s position in the group.
_SHELL_C_OPT = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
# Every `gh api` flag whose NEXT token is a value must appear here. Besides endpoint discovery,
# `_graphql_argument_role_is_unprovable` relies on consuming these values; an omission fails closed
# by misclassifying a legitimate expandable value as stray bare input.
_API_VALUE_FLAGS = frozenset(
    {"-H", "--header", "-F", "--field", "-f", "--raw-field", "-q", "--jq", "-X", "--method",
     "--input", "--hostname", "-p", "--preview", "-t", "--template", "--cache"}
)
# The GraphQL mutations that LAND code without `gh ship`: merge a PR (`mergePullRequest`), turn on
# auto-merge (`enablePullRequestAutoMerge`), add a PR to the merge queue (`enqueuePullRequest`), or
# merge a branch directly (`mergeBranch`). A literal occurrence in any graphql arg blocks regardless
# of quoting (codex review: these are alternate landing routes the quote-aware allowance must not open).
_MERGE_MUTATION = re.compile(
    r"mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest|mergeBranch"
)
_REST_MERGE_PATH = re.compile(r"pulls/[^/]+/merge")
_WRITE_METHOD_EQ = re.compile(r"^(--method|-X)=(PUT|POST)$", re.IGNORECASE)
# A `query` field fed from a file (`@f`), stdin (`@-`) or a substitution — its text can't be read
# at pre-exec time, so a graphql call carrying it is over-blocked (fail closed).
_FIELDISH = frozenset({"-F", "--field", "-f", "--raw-field"})


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-pr-merge: {msg}\n")


# Separators after which a `#` can begin a comment (an unquoted word boundary).
_SEGMENT_SEPS = frozenset(";&|()")
# Chars that end an UNQUOTED heredoc delimiter word (whitespace or a shell metacharacter).
_HEREDOC_DELIM_END = frozenset(" \t\n;&|()<>")


def _read_heredoc_delimiter(command: str, j: int) -> tuple[str, int, bool] | None:
    """Read ONE shell word starting at `command[j]` and return `(dequoted_word, end_index, quoted)`,
    or None on an unterminated quote / empty word. The word is quote-removed exactly as the shell
    does for a heredoc delimiter: `\\EOF`, `E"OF"`, `'EOF'` and `EOF` all yield the terminator `EOF`.
    Quoting the WHOLE word (not just a `'…'`/`"…"` prefix) matters — otherwise `<<\\EOF` / `<<E"OF"`
    never match the unquoted `EOF` terminator, the body skips to end of input, and a real merge after
    the heredoc is ALLOWED (Codex review).

    `quoted` is True when ANY part of the delimiter was quoted/escaped (`<<'EOF'`, `<<\\EOF`,
    `<<E"OF"`): per POSIX that makes the heredoc body LITERAL — `$( … )`/backticks are NOT expanded —
    so the caller must not scan the body for executed substitutions (an UNQUOTED `<<EOF` body IS
    expanded and MUST be scanned)."""
    n = len(command)
    out: list[str] = []
    quoted = False
    k = j
    while k < n:
        ch = command[k]
        if ch in _HEREDOC_DELIM_END:
            break
        if ch == "\\" and k + 1 < n:
            out.append(command[k + 1])  # backslash-escaped char → literal
            quoted = True
            k += 2
            continue
        if ch in ("'", '"'):
            close = command.find(ch, k + 1)
            if close == -1:
                return None  # unterminated quote in the delimiter word → decline (fail toward block)
            out.append(command[k + 1 : close])  # single quotes: verbatim; good enough for a delim
            quoted = True
            k = close + 1
            continue
        out.append(ch)
        k += 1
    word = "".join(out)
    return (word, k, quoted) if word else None


def _read_heredoc(command: str, i: int) -> tuple[str, bool, bool, int] | None:
    """If `command[i:]` opens a heredoc (`<<WORD` / `<<-WORD`), return `(delimiter, dash, quoted,
    index_after_the_operator)`; else None. `<<<` (a here-STRING, one line) is not a heredoc. The
    delimiter is the quote-removed shell word; `quoted` marks a literal (non-expanding) body (see
    `_read_heredoc_delimiter`)."""
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
    return delim, dash, quoted, end


def _extract_expanding_substitutions(text: str) -> list[str]:
    r"""Extract `$( … )` and backtick bodies from UNQUOTED-heredoc-body text. In such a body quotes
    (`'`, `"`) and `#` are LITERAL DATA and only `$( … )` / backticks (and `$var`, not detected)
    EXPAND — unlike a normal command line. So, unlike `_extract_command_substitutions`, this does
    NOT suppress single-quoted spans or strip comments; it only honours a backslash escape (`\$`,
    `` \` `` prevent expansion). This closes the asymmetry where `# $(gh pr merge 1)` /
    `'$(gh pr merge 1)'` in an unquoted heredoc body were wrongly allowed (coordinator ship review)."""
    subs: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:  # `\$` / `` \` `` → escaped, no expansion
            i += 2
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            inner, i = _read_paren_substitution(text, i + 2)
            subs.append(inner)
            continue
        if ch == "`":
            inner, i = _read_backtick_substitution(text, i + 1)
            subs.append(inner)
            continue
        i += 1
    return subs


def _body_line_is_merge(line: str, quoted: bool) -> bool:
    """True iff a SKIPPED heredoc body line should be salvaged as a merge. For a QUOTED (literal)
    delimiter only an ARGV-position merge line counts (`$( … )` in the body is literal, not
    executed). For an UNQUOTED delimiter the body IS expanded, so an argv-position merge OR a
    `$( … )` / backtick that runs a merge counts too. The expansion scan uses
    `_extract_expanding_substitutions` (quotes and `#` are literal DATA in a heredoc body — only
    `$()`/backticks expand), so `# $(gh pr merge 1)` and `'$(gh pr merge 1)'` are caught while a
    prose/apostrophe line without a substitution is not (codex reviews 3 & 4; coordinator review)."""
    if quoted:
        # A QUOTED delimiter means the OUTER shell does not expand the body — but if the heredoc feeds
        # an INTERPRETER (`bash <<'EOF' … gh api graphql -f query="$Q" … EOF`), that inner shell DOES
        # expand `$Q` when it executes the line. We can't tell interpreter-fed from data-fed here, so
        # scan an argv-position `gh api graphql` query STRICTLY (any `$`/backtick blocks) — the same
        # safe over-block the salvage already applies to a bare `gh pr merge` line in a quoted body
        # (#268 regression guard: chatgpt-codex-connector P1 on the PR). `$( … )` stays literal for a
        # quoted delimiter (not re-scanned), matching the outer shell.
        return _line_has_executable_merge(line, strict=True)
    # UNQUOTED heredoc body: the shell EXPANDS `$`, so scan strictly (a `gh api graphql -f query="$Q"`
    # here is an expandable-query merge — #268 regression guard) and also follow substitutions.
    if _line_has_executable_merge(line, strict=True):
        return True
    # Fail CLOSED (`is not False`, not `is True`): a merge-like but unparseable expanded body (an
    # unbalanced quote inside `$( … )`) returns None and must still BLOCK, matching the top-level
    # `return sub` fail-closed contract — otherwise `$(gh pr merge 1 "x)` in an unquoted heredoc
    # would slip through (Opus ship review). A benign unparseable body returns False and passes.
    return any(
        _command_contains_gh_pr_merge(inner, strict=True) is not False
        for inner in _extract_expanding_substitutions(line)
    )


def _skip_heredoc_bodies(
    command: str, i: int, delimiters: list[tuple[str, bool, bool]]
) -> tuple[int, int]:
    """Advance past the bodies of the heredocs opened on the just-ended line, returning
    `(new_index, salvaged_merge_line_count)`. `i` points at the first body character (right after
    the line's newline). Each delimiter is `(delim, dash, quoted)`; for each, consume whole lines
    until a line whose content equals the delimiter (leading tabs ignored when the heredoc used
    `<<-`).

    Two safety nets, both erring toward BLOCK:
    - FAIL-CLOSED: if a terminator line is NOT found ahead, skip NOTHING (return the original `i`, 0).
      A `<<` that was NOT really a heredoc opener — arithmetic left-shift, an unterminated heredoc —
      then does not swallow the rest of the input; the following lines are scanned normally.
    - DEFENSE-IN-DEPTH: every SKIPPED body line is still scanned for a merge (see
      `_body_line_is_merge`: argv-position for a quoted delimiter, argv-or-substitution for an
      unquoted, expanding one), and each hit is counted. A crafted heredoc can plant a matching
      terminator AFTER a real merge (`(( 0 << merge ))` / `gh pr merge 1` / `merge`) to skip past it —
      counting the merge lines lets the caller re-inject a detectable merge so the gate still blocks.
      A merge at a body line's executable position is over-blocked (safe); a prose mention is not."""
    n = len(command)
    pos = i
    salvaged = 0
    for delim, dash, quoted in delimiters:
        found = False
        local = 0
        while pos < n:
            nl = command.find("\n", pos)
            line_end = n if nl == -1 else nl
            line = command[pos:line_end]
            pos = line_end + 1 if nl != -1 else n
            if (line.lstrip("\t") if dash else line) == delim:
                found = True
                break
            if _body_line_is_merge(line, quoted):
                local += 1
        if not found:
            return i, 0
        salvaged += local
    return pos, salvaged


def _normalize_newlines(command: str) -> str:  # noqa: C901 — a small quote/escape/comment scanner
    r"""Remove `\`-newline line continuations (the shell joins the parts with no space) and turn
    each BARE (unquoted) newline into a `;` command separator, quote-, escape-, comment- and
    heredoc-aware.

    In a real shell a bare newline separates commands (like `;`) and a `#` comment runs only to the
    end of its LINE. shlex, fed the whole blob, instead (a) consumes a bare newline as whitespace —
    so `echo ok`+newline+`gh pr merge` collapses into one `echo`-headed segment and the merge on the
    second line evades detection — and (b) runs a `#` comment to the end of the whole INPUT once the
    newlines are gone, so a `# comment` on line 1 would swallow a merge on line 2. This pass fixes
    both BEFORE tokenizing: it drops `#` comments per-line and converts bare newlines to `;`, while
    leaving newlines/`#`/`;` that sit INSIDE quotes literal (so a two-line commit message merely
    mentioning a merge stays one quoted token and is not mis-detected).

    A `#` starts a comment ONLY at an unquoted word boundary (start, or after unescaped whitespace /
    a `;&|()` separator) — tracked by `boundary`, NOT by inspecting the last emitted char, so an
    escaped space (`echo foo\ # x`) keeps the `#` in-word and does not hide a later merge.

    Backslash escaping is honored the way the shell does: an escaped quote can't spoof quote state
    (`echo \"`+newline+`gh pr merge` runs a real merge on line 2 — `\"` is a literal `"`, not an
    opening quote). Outside quotes and inside double quotes a `\` escapes the next char (emitted
    verbatim so shlex, in posix mode, applies the same escape); inside single quotes a `\` is literal.

    Heredoc bodies (`cat <<EOF` … `EOF`) are NOT command lines, so they are skipped whole — else the
    body's newlines would become `;` separators and a `gh pr merge` mentioned in a commit-message
    heredoc would be falsely blocked. Two backstops keep that from becoming a bypass: `<<` inside an
    arithmetic context (`(( … ))` / `$(( … ))`) is a left-shift, NOT a heredoc opener (tracked by
    `arith_depth`); and any executable `gh pr merge` on a SKIPPED body line is re-injected so the
    gate still blocks (see `_skip_heredoc_bodies`), defeating a crafted `(( 0 << merge ))` / `gh pr
    merge` / `merge` that plants a matching terminator after the real merge.
    """
    out: list[str] = []
    quote: str | None = None  # "'" or '"' when inside that quote
    boundary = True  # at an unquoted word boundary (where a `#` may start a comment)
    arith_depth = 0  # inside `((`/`$((` — a `<<` there is left-shift, not a heredoc opener
    pending_heredocs: list[tuple[str, bool, bool]] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n and quote != "'":  # backslash escape (not in single quotes)
            if command[i + 1] == "\n":  # `\`-newline continuation: REMOVED (parts join, no space)
                i += 2
                continue
            out.append(ch)  # `\<char>`: emit both verbatim; the escaped char is inert and in-word
            out.append(command[i + 1])
            boundary = False
            i += 2
            continue
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            boundary = False
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            boundary = False
        elif ch == "#" and boundary:
            while i < n and command[i] != "\n":  # comment → end of line (drop it)
                i += 1
            continue
        elif ch == "(" and i + 1 < n and command[i + 1] == "(":  # `((` / `$((` — arithmetic opens
            arith_depth += 1
            out.append("((")
            boundary = True
            i += 2
            continue
        elif ch == ")" and i + 1 < n and command[i + 1] == ")" and arith_depth > 0:
            arith_depth -= 1
            out.append("))")
            boundary = True
            i += 2
            continue
        elif ch == "\n":
            # bare newline → command separator. Spaces around the `;` keep shlex from GLUING it to a
            # neighbouring punctuation char into one run — `echo $(true)`+newline+`gh pr merge` would
            # else tokenize as `);` (a run `_split_segments` doesn't treat as a separator), keeping
            # the merge inside the `echo` segment and ALLOWING it (Codex review).
            out.append(" ; ")
            boundary = True
            # A bare newline is a command boundary — reset arithmetic depth so an unbalanced or
            # whitespace-split `((` (e.g. `(( x = 1 ) )`) cannot leave `arith_depth > 0` and pollute
            # heredoc detection for the REST of the input (which could blind a later real merge or
            # over-block a later heredoc). Arithmetic `(( … ))` never spans an unquoted newline in
            # practice, so per-line scoping is both safe and correct (claude/gemini review).
            arith_depth = 0
            i += 1
            if pending_heredocs:
                i, salvaged = _skip_heredoc_bodies(command, i, pending_heredocs)
                # Re-inject a detectable merge segment for each executable merge found on a skipped
                # body line — so a crafted heredoc can't hide a real `gh pr merge` in its body.
                out.append(" ; gh pr merge ; " * salvaged)
                pending_heredocs = []
            continue
        elif arith_depth == 0 and (heredoc := _read_heredoc(command, i)) is not None:
            delim, dash, quoted, i = heredoc
            pending_heredocs.append((delim, dash, quoted))
            boundary = False
            continue
        elif ch in " \t":
            out.append(ch)
            boundary = True
        elif ch in _SEGMENT_SEPS:
            out.append(ch)
            boundary = True
        else:
            out.append(ch)
            boundary = False
        i += 1
    return "".join(out)


def _split_segments(command: str) -> list[list[str]]:
    r"""Tokenize `command` via shlex and split into per-segment token lists.

    Uses ``punctuation_chars=True`` so that `;`, `&&`, `||`, `|` etc. are
    returned as standalone separator tokens rather than being glued to adjacent
    words (the default shlex.split() behaviour leaves `done;` as one token,
    missing the `;` separator entirely).

    Each segment is one independent command (the shell atoms between separators).
    Returns a list of token lists; raises ValueError on unbalanced quotes
    (caller treats this as fail-closed).

    `_normalize_newlines` first folds `\`-newline continuations and turns bare newlines into `;`
    (quote- and comment-aware) so a raw merge on a second line cannot evade detection while shell
    comment semantics are preserved.
    """
    command = _normalize_newlines(command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = False
    # Keep chars that are LITERAL inside a shell word but not already word-chars as part of the
    # token, so a gh-api field/flag value does not fragment: without this, `punctuation_chars=True`
    # splits `-F query=@merge.graphql` into `query=`, `@`, `merge.graphql` (→ `_gh_api_endpoint`
    # reads `@` as the endpoint, codex review 5) and `--method=$METHOD` into `--method=`, `$`,
    # `METHOD` (→ the glued-value regex sees an empty method and the shell-expanded `$METHOD` slips
    # through, coordinator ship review). Adding `$` keeps `--method=$METHOD` / `query=$Q` whole so
    # the unprovable-value checks fire. A `$( … )` command substitution is still detected by the raw
    # `_extract_command_substitutions` scan (independent of shlex). Separators (`;&|()<>`) live in
    # punctuation_chars and are unaffected.
    lex.wordchars += "@:,{}%+$"
    # `_normalize_newlines` already stripped real (word-boundary, to-end-of-line) `#` comments, so
    # shlex's own commenter must be OFF — otherwise it truncates a MID-WORD `#` (literal in a real
    # shell) to end of input, e.g. `echo foo#bar && gh pr merge 1` would parse as just `['echo',
    # 'foo']` and ALLOW the raw merge. `block-reset-hard` disables it for the same reason.
    lex.commenters = ""
    tokens = list(lex)  # raises ValueError on unbalanced quotes
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


def _gh_subargs(argv: list[str]) -> list[str]:
    """`argv[0]` is `gh`; skip `gh` global flags (incl. `-R owner/repo`) to reach the subcommand.
    So `gh -R o/r pr merge 5` resolves to `['pr', 'merge', '5']`, not `['-R', ...]`.

    `_GH_VALUE_FLAGS` is a whitelist of the value-taking global flags (`-R/--repo/--hostname`); any
    other `-flag` is treated as boolean. If a future `gh` adds a value-taking global flag NOT in the
    set, `gh --newflag val pr merge` would mis-parse (`val` seen as the subcommand) and MISS the
    merge — keep this set in sync with `gh`'s global flags."""
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in _GH_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):  # boolean / glued `--repo=o/r`
            i += 1
            continue
        break
    return argv[i:]


def _gh_api_endpoint(rest: list[str]) -> str | None:
    """The endpoint positional of `gh api <endpoint> …` — the first bare token that is neither a
    flag nor a value-taking flag's value. Used so a `/graphql` substring inside some field VALUE
    can't misclassify a REST call as GraphQL."""
    i, n = 0, len(rest)
    while i < n:
        t = rest[i]
        if t in _API_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1  # boolean flag or glued `--flag=value` / `-Fk=v`
            continue
        return t
    return None


def _rest_has_write_method(rest: list[str]) -> bool:
    """True iff a PUT/POST method flag is present: `-X PUT`, `--method PUT`, `-XPUT`, `-X=PUT`,
    `--method=PUT`."""
    for j, a in enumerate(rest):
        if a in ("-X", "--method") and j + 1 < len(rest) and rest[j + 1].upper() in ("PUT", "POST"):
            return True
        if _WRITE_METHOD_EQ.match(a):
            return True
        if re.match(r"^-X(PUT|POST)$", a, re.IGNORECASE):  # glued short form
            return True
    return False


def _rest_method_is_unprovable(rest: list[str]) -> bool:
    """True iff a `-X`/`--method` flag's VALUE is a shell expansion (`$var`, `${v}`, `$( … )`,
    backtick) the hook cannot resolve at pre-exec time. On a literal merge endpoint such a method
    MAY be `PUT`, so it is over-blocked (fail closed) — the same posture as an unprovable graphql
    query, and consistent with `-X $METHOD gh api …/pulls/<n>/merge` being a real merge (codex
    review 4)."""

    def value_is_expansion(v: str) -> bool:
        return "$" in v or "`" in v

    for j, a in enumerate(rest):
        if a in ("-X", "--method") and j + 1 < len(rest) and value_is_expansion(rest[j + 1]):
            return True
        m = re.match(r"^(?:--method|-X)=(.*)$", a)
        if m:
            val = m.group(1)
            # An EMPTY glued value (`--method=` / `-X=`) means the real value was fragmented off by
            # a `$( … )` / backtick that the tokenizer split away (e.g. `--method=`+`` ` ``+`echo`);
            # on a merge endpoint that method is unprovable → fail closed (codex ship review). A `$`
            # stays glued via wordchars, so this fires for the backtick-fragmented form.
            if val == "" or value_is_expansion(val):
                return True
        if a.startswith("-X") and len(a) > 2 and value_is_expansion(a[2:]):  # glued `-X$METHOD`
            return True
    return False


def _graphql_query_field_values(rest: list[str]) -> list[str]:
    """The `query` field VALUES in `rest` (= args after `gh api`), across every flag spelling:
    `-f query=<v>` / `-F query=<v>` (detached), and `-fquery=<v>` / `--field=query=<v>` /
    `--raw-field=query=<v>` (glued). Non-`query` fields are graphql VARIABLES that cannot execute a
    mutation, so they are ignored."""
    values: list[str] = []
    i, n = 0, len(rest)
    while i < n:
        a = rest[i]
        if a in _FIELDISH and i + 1 < n:
            key, _, val = rest[i + 1].partition("=")
            if key == "query":
                values.append(val)
            i += 2
            continue
        m = re.match(r"^(?:--field|--raw-field|-[fF])=?(.*)$", a)
        if m and m.group(1):
            key, _, val = m.group(1).partition("=")
            if key == "query":
                values.append(val)
        i += 1
    return values


def _graphql_argument_role_is_unprovable(rest: list[str]) -> bool:
    """True iff shell expansion can decide a GraphQL field KEY or a bare argument's role.

    A whole expanded field (`-f $FIELD` / `--raw-field=$FIELD`) may become
    `query=<merge mutation>`, while a bare expansion after the endpoint may inject the same field
    flags. A literal non-`query` key such as `-F id=$NODE_ID` is provably a GraphQL variable and
    remains allowed even though its VALUE expands (#284).
    """
    endpoint_seen = False
    i, n = 0, len(rest)
    while i < n:
        a = rest[i]
        if a in _FIELDISH:
            if i + 1 < n:
                key = rest[i + 1].partition("=")[0]
                if "$" in key or "`" in key:
                    return True
            i += 2
            continue
        m = re.match(r"^(?:--field|--raw-field|-[fF])=?(.*)$", a)
        if m and m.group(1):
            key = m.group(1).partition("=")[0]
            if "$" in key or "`" in key:
                return True
            i += 1
            continue
        if a in _API_VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if not endpoint_seen:
            endpoint_seen = True
        elif "$" in a or "`" in a:
            return True
        i += 1
    return False


def _graphql_query_is_unprovable(rest: list[str], strict: bool = False) -> bool:
    """True iff a `gh api graphql` call feeds its `query` (or the whole request body) from a source
    this hook CANNOT READ at pre-exec time — a file (`query=@f`), stdin (`query=@-` / `--input …`), or
    an empty value — or lets shell expansion decide a field key / bare argument role — so a merge
    mutation could hide in it (fail closed, #284).

    `strict` (used only in NESTED contexts — a wrapper `-c`/`eval` string, a substitution body, an
    UNQUOTED heredoc body) ALSO blocks any inline `$`/backtick in a query value, restoring the old
    "block every `$`" rule there. In those contexts the quoting the hook sees is NOT the quoting the
    shell applied (an outer double-quoted `bash -c "…'$Q'…"` already expanded `$Q`; an unquoted
    heredoc body expands `$Q`), so a `$` cannot be trusted as a literal GraphQL variable. The
    quote-aware single-quote allowance (`_graphql_query_field_is_expandable`) is only sound at the
    TOP LEVEL, where the raw quoting is the real quoting (#268 regression guard: opus/codex)."""
    if _graphql_argument_role_is_unprovable(rest):
        return True
    for val in _graphql_query_field_values(rest):
        if val.startswith("@") or val == "":  # file/stdin-backed (`@f`/`@-`) or empty: unreadable
            return True
        if strict and ("$" in val or "`" in val):  # nested context: any $/backtick may expand → block
            return True
    return any(a == "--input" or a.startswith("--input=") for a in rest)  # whole body from file/stdin


def _gh_api_is_merge(rest: list[str], strict: bool = False) -> bool:
    """`rest` = args after `gh api`. True iff this is a PR-merge REST call (`…/pulls/<n>/merge` +
    write method) or a graphql merge mutation (inline, or a file/stdin/substitution-backed query
    that can't be proven safe). `strict` — see `_graphql_query_is_unprovable`."""
    endpoint = _gh_api_endpoint(rest)
    is_graphql = endpoint == "graphql" or (endpoint or "").endswith("/graphql")
    if is_graphql:
        # A merge mutation token ANYWHERE in a graphql call blocks — this is a deliberate over-block:
        # a graphql invocation carrying `mergePullRequest`/`enablePullRequestAutoMerge` in any field
        # is almost certainly the mutation, and over-blocking is the safe direction (Opus review 2).
        if _MERGE_MUTATION.search(" ".join(rest)):
            return True
        if _graphql_query_is_unprovable(rest, strict=strict):
            return True
    # Match the merge path against the ENDPOINT positional — not every arg — so a `pulls/<n>/merge`
    # substring inside a field VALUE (`-f body='see pulls/1/merge'`) does not false-block an
    # unrelated `gh api` call (Opus review 1). If the endpoint could not be resolved (a degraded
    # parse, e.g. a value-flag mis-classification swallowed it), fall back to scanning all args so a
    # real merge is not MISSED — fail-closed direction (Opus review 2).
    rest_path_hit = (
        bool(_REST_MERGE_PATH.search(endpoint))
        if endpoint is not None
        else any(_REST_MERGE_PATH.search(a) for a in rest)
    )
    # A write method (`-X PUT`) merges; an UNPROVABLE method (`-X $METHOD`) may be PUT → fail closed.
    return rest_path_hit and (_rest_has_write_method(rest) or _rest_method_is_unprovable(rest))


def _is_invoked_head(base: str) -> bool:
    """True iff a token's basename names a command whose merge we detect directly: `gh`, a shell
    interpreter (its `-c` string is re-scanned), or `eval`."""
    return base == "gh" or base == "eval" or base in _SHELL_INTERPRETERS


def _resolve_invoked_argv(argv: list[str]) -> list[str] | None:
    """Return the argv of the actually-invoked command, seeing through ONE wrapper prefix. Returns
    the tail starting at the first token whose basename is `gh`, a shell interpreter, or `eval` —
    so `env gh pr merge`, `env bash -c '…'` (wrapper + interpreter), and `/usr/bin/timeout 60 gh …`
    (path-qualified) all resolve. Returns None when no such invocation is at the command position.
    A quoted `gh pr merge` inside another program's arg (`echo "gh pr merge"`) is one token whose
    basename is not a head, so it is not matched (Opus/codex reviews 4/6/7)."""
    if not argv:
        return None
    if _is_invoked_head(os.path.basename(argv[0])):
        return argv
    if os.path.basename(argv[0]) in _WRAPPERS:  # basename so `/usr/bin/env …` matches too
        # Scan for the wrapped invoked head without modelling each wrapper's flag arity. This can
        # OVER-block a token that is merely DATA to a non-head command under a wrapper (`nice echo
        # gh pr merge`) — the safe direction for a security gate; a merge is never let through.
        for k in range(1, len(argv)):
            if _is_invoked_head(os.path.basename(argv[k])):
                return argv[k:]
    return None


def _unparseable_command_has_invoked_head(command: str) -> bool:
    """Best-effort argv anchoring after `_split_segments` fails on an unbalanced quote.

    Consume every complete token before the lexer error and resolve each complete or partial
    segment exactly as the normal path does. This keeps the raw `_MERGE_HINT` fail-closed for a real
    `gh`, shell / `eval`, or wrapper-resolved invocation, while an unrelated head such as `tg` does
    not inherit merge-like prose from its malformed argument (#289).
    """
    lex = shlex.shlex(_normalize_newlines(command), posix=True, punctuation_chars=True)
    lex.whitespace_split = False
    lex.wordchars += "@:,{}%+$"
    lex.commenters = ""
    segments: list[list[str]] = []
    current: list[str] = []
    try:
        for tok in lex:
            if tok in _SHELL_SEPS:
                if current:
                    segments.append(current)
                    current = []
            elif tok.strip():
                current.append(tok)
    except ValueError:
        pass
    if current:
        segments.append(current)
    return any(
        _resolve_invoked_argv(_segment_argv(segment)) is not None for segment in segments
    )


def _wrapped_command_strings(argv: list[str]) -> list[str]:
    """Command strings that `argv` executes from a STRING ARGUMENT rather than the token stream: a
    shell interpreter's `-c <cmd>` (incl. combined short options `-cx` / `-xc`) and every argument
    of `eval` (joined and re-parsed). A merge hidden in such a quoted string (`bash -c 'gh pr merge
    1'`, `eval "gh pr merge 1"`) is not reachable by the token see-through, so the caller re-scans
    each returned string as a command (Opus/codex reviews 6/7)."""
    if not argv:
        return []
    base = os.path.basename(argv[0])
    if base in _SHELL_INTERPRETERS:
        out: list[str] = []
        i = 1
        while i < len(argv):
            if _SHELL_C_OPT.match(argv[i]) and i + 1 < len(argv):
                out.append(argv[i + 1])
                i += 2
                continue
            i += 1
        return out
    if base == "eval":
        return [" ".join(argv[1:])] if len(argv) > 1 else []
    return []


def _is_merge_route(segment: list[str], strict: bool = False) -> bool:
    """True iff this segment's argv is a direct PR-merge route that skips `gh ship`: `gh pr merge …`
    (incl. behind a `gh -R o/r` global flag, a wrapper like `env`/`sudo`/`timeout`, or a wrapper +
    interpreter like `env bash -c '…'`), or a `gh api` REST/GraphQL merge (see `_gh_api_is_merge`),
    or a merge inside a shell interpreter's `-c` string / `eval` argument. Anchored on the
    ACTUALLY-invoked command, so a prose mention in another program's args (`tg "…mergePullRequest…"`,
    `echo mergePullRequest`) and `git merge main` all pass. `strict` — see `_graphql_query_is_unprovable`."""
    argv = _resolve_invoked_argv(_segment_argv(segment))
    if argv is None:
        return False
    if os.path.basename(argv[0]) == "gh":
        sub = _gh_subargs(argv)
        if len(sub) >= 2 and sub[0] == "pr" and sub[1] == "merge":
            return True
        if sub and sub[0] == "api":
            return _gh_api_is_merge(sub[1:], strict=strict)
        return False
    # A shell interpreter / `eval`: re-scan the merge hidden in its `-c` / string arguments. That
    # string is a NESTED context whose inner quoting the outer shell may already have processed
    # (`bash -c "…query='$Q'…"` expands `$Q` at the outer level), so scan it strictly.
    for cmd in _wrapped_command_strings(argv):
        if _command_contains_gh_pr_merge(cmd, strict=True) is True:
            return True
    return False


def _read_paren_substitution(command: str, start: int) -> tuple[str, int]:
    """`command[start:]` is the body right after a `$(`. Return `(inner_body, index_after_close)`,
    finding the matching `)` while honouring nested `(`/`)`, quoted spans, and `#` COMMENTS. A `)`
    inside a `#` comment does not close the substitution (`$(echo ok # )`+newline+`gh pr merge`+
    newline+`)` runs the merge — the `# )` is a comment, the real `)` is after the merge line), so
    the reader skips a comment (from an unquoted word boundary to end of line). If no close is found,
    return the remainder and `len(command)` — over-block-safe: the tail is still re-scanned (codex
    review 5)."""
    depth = 1
    i, n = start, len(command)
    quote: str | None = None
    boundary = True  # at an unquoted word boundary (where a `#` may start a comment)
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            boundary = False
            continue
        if ch == "#" and boundary:
            nl = command.find("\n", i)
            i = n if nl == -1 else nl  # skip the comment to end of line (the newline stays)
            continue
        if ch in ("'", '"'):
            quote = ch
            boundary = False
        elif ch == "(":
            depth += 1
            boundary = True
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return command[start:i], i + 1
            boundary = True
        elif ch in " \t\n;&|":
            boundary = True
        else:
            boundary = False
        i += 1
    return command[start:], n


def _unescape_backtick_body(body: str) -> str:
    r"""POSIX un-escaping of a backtick substitution body: `` \` `` → `` ` ``, `\\` → `\`, `\$` →
    `$` (any other `\x` is left verbatim). A nested backtick substitution is written `` \`…\` ``
    inside the outer backticks, so without this un-escape the re-scan of the body would treat the
    `` \` `` as inert and MISS the nested merge — the #248 bypass one level deeper (Opus review)."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] == "\\" and i + 1 < n and body[i + 1] in ("`", "\\", "$"):
            out.append(body[i + 1])
            i += 2
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def _read_backtick_substitution(command: str, start: int) -> tuple[str, int]:
    r"""`command[start:]` is the body right after an opening backtick. Return `(inner_body,
    index_after_close)`, ending at the next unescaped backtick. The returned body is POSIX-unescaped
    (see `_unescape_backtick_body`) so a nested `` \`…\` `` substitution is visible when the caller
    re-scans it. If no close is found, return the (unescaped) remainder and `len(command)`
    (over-block-safe)."""
    i, n = start, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            return _unescape_backtick_body(command[start:i]), i + 1
        i += 1
    return _unescape_backtick_body(command[start:]), n


def _extract_command_substitutions(command: str) -> list[str]:
    r"""Return the bodies of every command/process substitution a real shell would EXECUTE: `$( … )`
    and backtick `` `…` `` (both OUTSIDE quotes and INSIDE double quotes — `echo "$(gh pr merge 1)"`
    runs the merge), plus process substitution `<( … )` / `>( … )` (unquoted only). Single-quoted
    spans SUPPRESS substitution, so they are kept literal and skipped (`echo '$(gh pr merge 1)'` is
    inert). Nested/inner substitutions are handled by the caller re-scanning each returned body
    recursively.

    Why this exists (#248): the argv scanner keeps a substitution body inside ONE token — a
    double-quoted `"$( … )"` stays a single literal token, and a bare/backtick body is `$`/`` ` ``
    prefixed — so `argv[0]` is never `gh` and the wrapped merge sails past the gate. Extracting each
    executed body and re-scanning it with the SAME detector closes that, over-blocking in the safe
    direction while a single-quoted (inert) body and a plain string arg still pass."""
    subs: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        ch = command[i]
        if quote == "'":  # single quotes: fully literal, no substitution
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:  # escape (outside single quotes): next char is inert
            i += 2
            continue
        if ch == "'" and quote is None:
            quote = "'"
            i += 1
            continue
        if ch == '"':
            quote = None if quote == '"' else '"'
            i += 1
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            inner, i = _read_paren_substitution(command, i + 2)
            subs.append(inner)
            continue
        if ch in ("<", ">") and i + 1 < n and command[i + 1] == "(" and quote is None:
            # Process substitution `<( … )` / `>( … )` also EXECUTES its body (`diff <(gh pr merge
            # 1) x`). Unlike `$(…)`/backticks it is NOT expanded inside double quotes, so only fire
            # when unquoted.
            inner, i = _read_paren_substitution(command, i + 2)
            subs.append(inner)
            continue
        if ch == "`":
            inner, i = _read_backtick_substitution(command, i + 1)
            subs.append(inner)
            continue
        i += 1
    return subs


def _line_has_executable_merge(line: str, strict: bool = False) -> bool:
    """True iff a segment of `line` is a merge route at an executable (argv) position. Segment-only
    — it does NOT recurse into command substitutions — so the heredoc-body salvage counts only a
    literal executable-position merge planted before a crafted terminator, not a `$( … )` that a
    quoted heredoc body keeps literal. A parse failure counts as no merge (fail toward allow, so a
    prose/apostrophe body line is not over-blocked). `strict` — see `_graphql_query_is_unprovable`."""
    try:
        segments = _split_segments(line)
    except ValueError:
        return False
    return any(_is_merge_route(seg, strict=strict) for seg in segments)


def _substitutions_contain_merge(command: str) -> bool | None:
    """Scan every EXECUTED command substitution in `command` (recursively). Returns True if any
    body is (or contains) a raw merge route; None if a merge-like body cannot be parsed (fail
    closed); False otherwise.

    Scans the NORMALIZED command (`_normalize_newlines`), not the raw text, so a `$( … )` that a
    real shell never executes is not over-blocked: `_normalize_newlines` drops `#` comments (`echo
    ok # $(gh pr merge 1)`) and skips heredoc bodies (`<<'EOF'` … `$(gh pr merge 1)` … `EOF`), which
    are data, not executed substitutions (codex review round 3). An executable-position `gh pr
    merge` LINE inside a heredoc body is still salvaged by `_skip_heredoc_bodies`.

    A substitution body is a NESTED context (the outer quoting the hook read may not be what the shell
    applied), so it is scanned strictly — any `$`/backtick in a graphql `query=` there blocks (#268
    regression guard)."""
    result: bool | None = False
    for inner in _extract_command_substitutions(_normalize_newlines(command)):
        r = _command_contains_gh_pr_merge(inner, strict=True)
        if r is True:
            return True
        if r is None:
            result = None
    return result


def _blank_single_quoted_expansions(command: str) -> str:
    r"""Replace `$` and backtick INSIDE single-quoted spans with `\0`, so that after shlex dequotes a
    token, a surviving `$`/backtick means the shell would EXPAND it (it was double-quoted / unquoted /
    concatenated), while a single-quoted literal (`'…$t…'`) leaves no `$`. This recovers the
    expand-vs-literal distinction that shlex erases, WITHOUT parsing GraphQL — the sound basis for
    allowing `-f query='…resolveReviewThread($t)…'` while blocking `-f query="$Q"` and the
    concatenation `-f query='mutation{ '$OP'(…)}'` (#268). Behaviour-preserving for everything else:
    `$`/backtick outside single quotes is untouched, and a `$(…)` / `` `…` `` that was single-quoted is
    inert anyway (`_extract_command_substitutions` already skips single-quoted spans). Honours double
    quotes (single-quote chars inside are literal) and backslash escapes."""
    out: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append("\0" if ch in ("$", "`") else ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _graphql_query_field_is_expandable(blanked_command: str) -> bool:
    r"""Argv-ANCHORED: True iff a `gh api graphql` segment of the single-quote-BLANKED command carries
    a `query=` value with a surviving `$` or backtick — one the shell would EXPAND. A single-quoted
    literal (`'…$t…'`) was blanked to `\0` by `_blank_single_quoted_expansions`, so a `$`/backtick
    here was double-quoted / unquoted / concatenated and gets rewritten at runtime — it can splice a
    `mergePullRequest` field past the literal scan (`-f query="$Q"`, `-fquery="$Q"`,
    `-f query='…'$OP'…'`), so it is a merge (fail closed).

    Anchoring to a real `gh api graphql` argv keeps a prose `query=$Q` in a NON-gh command from being
    blocked (`echo graphql query=$Q`), and parsing via shlex tokens covers every flag spelling. Kept
    separate from `_command_contains_gh_pr_merge` (which runs on the ORIGINAL command) so the blanking
    — which is not heredoc-aware — cannot disturb heredoc/substitution merge detection. An unparseable
    command fails closed (block)."""
    try:
        segments = _split_segments(blanked_command)
    except ValueError:
        # Unparseable (unbalanced quotes): the shell would reject it too, so it cannot execute a
        # merge. Return False and let `_command_contains_gh_pr_merge`'s own None/`_MERGE_HINT`
        # fail-closed logic decide — independently blocking here over-blocks benign `grep won't file`.
        return False
    for seg in segments:
        argv = _resolve_invoked_argv(_segment_argv(seg))
        if not argv or os.path.basename(argv[0]) != "gh":
            continue
        sub = _gh_subargs(argv)
        if not sub or sub[0] != "api":
            continue
        rest = sub[1:]
        endpoint = _gh_api_endpoint(rest)
        if endpoint == "graphql" or (endpoint or "").endswith("/graphql"):
            if any("$" in val or "`" in val for val in _graphql_query_field_values(rest)):
                return True
    return False


def _command_contains_gh_pr_merge(command: str, strict: bool = False) -> bool | None:
    """Return True if `command` is (or hides) a raw merge route: a `gh pr merge` / `gh api` merge at
    an executable position in any segment (a `gh api graphql` merge is `mergePullRequest` /
    `enablePullRequestAutoMerge` / a file/stdin-backed query — a shell-expandable inline query is
    handled separately by `_graphql_query_field_is_expandable`), OR the same inside an executed
    command substitution (`$( … )` / backticks, incl. inside double quotes).

    `strict` is True for a NESTED scan — a substitution body, a wrapper `-c`/`eval` string, an
    unquoted heredoc body — where the quoting the hook reads is NOT what the shell applied, so any
    inline `$`/backtick in a graphql `query=` blocks (the old "block every `$`" rule; #268 guard).
    The TOP-LEVEL call (from `main`) is non-strict — there the raw quoting IS the real quoting, so a
    single-quoted GraphQL variable is allowed and only a shell-expandable `$` (caught by
    `_graphql_query_field_is_expandable`) blocks.

    Returns None (fail-closed) when the command — or a merge-like substitution body — cannot be
    parsed. A top-level raw merge hint is considered only when the best-effort parsed argv prefix
    resolves to a merge-capable invoked head (#289); other parse failures return False so an
    unrelated command's prose is not spuriously blocked.
    """
    # A shell-expandable inline graphql `query=` is a merge route too. Run it HERE (not only at the
    # top level) so it applies at every recursion depth — inside a `$( … )` / backtick substitution
    # and inside a `bash -c '…'` / `eval` rescan — matching the contract that the blocked routes are
    # caught in substitutions and wrappers (codex review). NORMALIZE first (drops `#` comments and
    # REMOVES heredoc bodies) THEN blank single-quoted `$`/backtick on a LOCAL copy — otherwise an
    # unmatched `'` in a heredoc body would leave the (heredoc-unaware) blanker stuck in single-quote
    # mode and mask a real `query="$Q"` on a later line (codex review). The heredoc/substitution merge
    # detection below still runs on the ORIGINAL `command`.
    if _graphql_query_field_is_expandable(
        _blank_single_quoted_expansions(_normalize_newlines(command))
    ):
        return True
    try:
        segments: list[list[str]] | None = _split_segments(command)
    except ValueError:
        segments = None
    if segments is not None and any(_is_merge_route(seg, strict=strict) for seg in segments):
        return True
    sub = _substitutions_contain_merge(command)
    if sub is True:
        return True
    if segments is None:
        # Top-level parse failed: fail-closed if an inner substitution was merge-like-but-unparseable
        # OR the raw text plausibly contains a merge attempt at a merge-capable invoked head;
        # otherwise it is a benign parse failure (#289).
        if sub is None or (
            _MERGE_HINT.search(command) and _unparseable_command_has_invoked_head(command)
        ):
            return None
        return False
    return sub  # False, or None (a merge-like substitution body that could not be parsed)


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


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", "block-raw-pr-merge: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    cwd = str(event.get("cwd") or os.getcwd())

    is_merge = _command_contains_gh_pr_merge(command)

    if is_merge is None:
        warn("could not parse command (unbalanced quotes) — blocking (fail-closed)")
        emit(
            "block",
            "block-raw-pr-merge: command has unbalanced quotes — cannot verify "
            "it is not a raw merge, blocking (fail-closed).",
        )
        return BLOCK_EXIT_CODE

    if not is_merge:
        emit("allow")
        return 0

    # A raw merge is deny-by-default. The ONLY exception is an approved external Telegram hatch
    # (deny-by-default); an unset env falls straight through to the block. The hatch call never
    # raises (it catches internally), so it does not affect this hook's fail-closed contract.
    hatch = hatch_escalation.request_hatch_approval(
        "block-raw-pr-merge",
        {"hook": "block-raw-pr-merge", "command": command},
        cwd=cwd,
        command=command,
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"raw `gh pr merge` allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"raw `gh pr merge` allowed via hatch escalation ({hatch.reason})")
            return 0
        warn(f"raw `gh pr merge` hatch escalation denied: {hatch.reason}")
        return _block(prefix=f"hatch escalation denied: {hatch.reason}")

    return _block()


if __name__ == "__main__":
    sys.exit(main())
