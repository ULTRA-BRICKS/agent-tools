#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require an AI review before a commit.

When the agent is about to `git commit`, this checks that an AI code review ran for
the current uncommitted state, by looking for a fresh marker file that the review
tool writes when it runs (and whose mtime is at least as new as the last change to
the index/working tree). If no review marker is found, it blocks with a reminder.

Wiring: have your review tool `touch` the marker on a successful run, e.g.
  review --uncommitted && touch "$REVIEW_MARKER"
The marker path is configurable via the REVIEW_MARKER env var; default below.

What is NOT gated (so the reminder stays honest and unobtrusive):
  - Anything that is not a real `git commit` SEGMENT — `git stash`, `git worktree`,
    `git status`, etc. (detection scopes to the parsed `git commit` argv, not the raw
    string, so a `commit` in a comment / message / pathspec never trips it).
  - A DOCS-ONLY commit — every staged path matches `*.md` or lives under a `docs/`
    directory. The project rule explicitly allows skipping review for docs.

External approval (replaces the OLD self-service escape hatch): there is NO env-var
(`REVIEW_SKIP`) or commit-message-trailer (`[skip-review: …]`) bypass any more — an agent
could set either on its own commit, so that "gate" was security theater. The block is now
DENY-BY-DEFAULT. A one-time exception is requested by setting
`RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="<written justification>"`, which routes a
single approval request to the human via Telegram (deny-by-default; a bare `1`/`true` is
rejected — it needs a real justification). An agent with a genuine reason should ASK the
human, not self-grant.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the repo cwd in event.cwd
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": a crash here must never wedge the ability to commit — this is a
discipline reminder, not a security boundary. (Contrast block-no-verify, fail-closed.)
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess  # noqa: S404 — listing staged files is the whole job
import sys
import time
from pathlib import Path
from typing import NamedTuple

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

# `--continue/--abort/--skip` are the rebase/merge/cherry-pick plumbing flags. `git commit` itself
# has NO such option, so on a real commit this set never matches and the exemption is INERT (git
# rejects `git commit --abort`, so no commit is produced — there is no un-reviewed-code bypass). It
# is kept only to stay in lock-step with the shared commit-segment parser in the sibling hooks (see
# the SYNC note below), where the SAME flags ARE meaningful on the gated subcommand. Critically, it
# is matched from the PARSED `git commit` argv — NOT the raw string — so a token that only appears
# in a shell COMMENT (`git commit -m x # --abort`), in the commit MESSAGE (`git commit -m 'support
# --skip'`), after `--` (a pathspec), or on a SIBLING command must NOT exempt a real commit.
SKIP_FLAGS = frozenset({"--continue", "--abort", "--skip"})

DEFAULT_MARKER = "~/.cache/agent-tools/last-review"
# How recent the review marker must be to count as "this session" (seconds).
FRESH_WINDOW_S = int(os.environ.get("REVIEW_FRESH_WINDOW_S", "3600"))
# Intentionally < the descriptor's `timeout_ms` so ONE slow lister yields a clean "can't classify
# docs" (→ the segment falls through to the marker gate) rather than the bridge killing the whole
# hook on its own timeout. A command with MANY chained docs-only commits could in principle sum
# several `git diff` calls past `timeout_ms` (each segment lists once); if the bridge then kills the
# hook, on_error=open lets the commit through — acceptable, since that path needs a contrived chain
# of docs-only commits on a huge repo. NOTE: a `git diff` failure does NOT allow the commit — it
# only forfeits the docs-only fast-path; the marker check still runs and BLOCKs when no fresh marker
# exists. (The true fail-OPEN points are an unparsable event and a marker that can't be stat'd.)
GIT_DIFF_TIMEOUT_S = 2

# Docs-only classification. A staged path is "docs" if it ends in a DOC extension, or sits under a
# `docs/` directory AND is not itself code/config. `.txt` is intentionally EXCLUDED — a `.txt` is
# commonly a dependency manifest (`requirements.txt`) or config, not prose, and a dependency change
# is exactly what review should see (codex supply-chain). A CODE file under `docs/` (e.g.
# `docs/conf.py`, `docs/build.py`, `docs/deploy.sh`) is NOT auto-skipped either: it always
# requires review (there is no per-commit self-service skip any more — see the module docstring).
DOCS_EXT = re.compile(r"\.(?:md|mdx|markdown|rst|adoc|rdoc|pod)$", re.IGNORECASE)
DOCS_DIR = re.compile(r"(?:^|/)docs/", re.IGNORECASE)
# Extensions that are NEVER docs even under a `docs/` dir — source, scripts, and config that can
# carry executable / security-relevant change.
CODE_EXT = re.compile(
    r"\.(?:py|pyi|js|mjs|cjs|jsx|ts|tsx|sh|bash|zsh|fish|rb|go|rs|java|kt|kts|c|cc|cpp|cxx|h|hpp|"
    r"cs|php|pl|pm|lua|swift|scala|clj|ex|exs|erl|r|jl|dart|groovy|gradle|ps1|psm1|bat|cmd|"
    r"sql|toml|yaml|yml|json|ini|cfg|conf|env|lock|tf|dockerfile|mk|cmake)$",
    re.IGNORECASE,
)
# Extensionless basenames that are code/config (CODE_EXT can't catch them — no dot). A file with
# one of these names under `docs/` is NOT docs.
CODE_BASENAMES = frozenset({
    "makefile", "dockerfile", "containerfile", "jenkinsfile", "vagrantfile", "rakefile",
    "gemfile", "procfile", "brewfile", "justfile", "cmakelists.txt",
})


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"require-review: {msg}\n")


def marker_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("REVIEW_MARKER", DEFAULT_MARKER)))


# ── argv parsing — scope skip/env/message detection to the real `git commit` segment ─────────
# SYNC: the commit-segment parser (_segments / _commit_flags / is_skip_commit) is adapted from the
# one in visual-proof-gate/visual_proof_gate.py and skills-read-gate/skills_read_gate.py — each hook
# is a self-contained standalone script run as its own subprocess (no shared import path), so the
# logic is duplicated by design. NOTE: this copy tokenizes with `punctuation_chars=True` (see
# ``_tokenize``) so a GLUED separator (`foo;git commit`, `a&&git commit`) is split correctly — the
# sibling hooks use plain ``shlex.split``, which does NOT split glued separators and so under-splits
# such chains; they should adopt this tokenizer too. Keep the skip-flag / message handling in step.
# Every shell command separator `punctuation_chars=True` can emit as a standalone token, incl. the
# bash compounds `|&` (pipe+stderr) and `;&`/`;;&` (case fall-through). Missing one would weld the
# following `git commit` into the previous segment and hide it from the gate.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
# Separators where the preceding segment's cwd effects carry to the next segment.
# `&&` and `;`/`;;`/`;;&`/`;&` (incl. case fall-through) all run in the SAME shell process,
# so a preceding `cd <dir>` does change the shell's cwd for the following command.
# `|`/`|&` create subshells (both sides), `&` backgrounds the left side into a subshell,
# and `||` only runs the right side when the LEFT side failed — so `cd` either did not
# execute (the side is skipped) or ran in a subshell and cannot change the parent cwd.
_CD_CARRY_SEP = frozenset({"&&", ";", ";;", ";;&", ";&"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# A `VAR=value` token before the `git` executable in a segment is an inline env assignment.
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line: split GLUED separators (`foo;git`, `a&&git`) into standalone
    tokens and drop a word-boundary `#` comment to end-of-LINE. `shlex.split`'s default
    whitespace_split keeps `;`/`&&` welded to the adjacent word, which would hide a `git commit`
    chained after a separator with no surrounding spaces. `punctuation_chars=True` makes shlex emit
    `; & | && ||` as their own tokens while still honoring quotes.

    Comments are handled MANUALLY (shlex's `commenters` is disabled) so a `#` only begins a comment
    at a WORD boundary — matching the shell, where `FOO=a#b git commit` runs the commit (the `#` is
    a literal inside the word) and is NOT swallowed as a comment. shlex's built-in commenter would
    cut at any `#`, dropping a glued-`#` commit → a bypass (codex). A `#` inside a quoted message
    (`-m 'fix #42'`) stays in the token, so it is not a comment-start either. Returns None on a
    tokenization failure (unbalanced quotes) → the caller fails safe."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True  # keep flags/paths intact; only the punctuation chars split words
    lex.commenters = ""          # do NOT let shlex cut at a glued `#`; we strip comments below
    try:
        tokens = list(lex)
    except ValueError:
        return None
    out: list[str] = []
    for tok in tokens:
        if tok.startswith("#"):
            break  # a token that STARTS with `#` is a word-initial comment → drop it and the rest
        out.append(tok)
    return out


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a NEWLINE
    is a command separator (a `;` token between lines). Multi-line agent commands — `git add -A`
    then `git commit -m x` on the NEXT line — are the common case; without per-line handling shlex
    would fold both onto one segment (`git add … git commit …`, executable `git`, subcommand `add`)
    and the commit would vanish from the gate. Each line also has its own `#`-to-end-of-line comment
    scope. A trailing backslash continues a line (the `\\`+newline is removed before splitting).

    If a line fails to tokenize on its own — most likely a quoted string that spans newlines, e.g.
    `git commit -m 'line1<newline>line2'` — it is re-joined (with the real newline restored, since
    the newline is INSIDE the quote) with the FOLLOWING line(s) until it tokenizes. This preserves
    newline command-boundaries for the surrounding script while keeping a multi-line quoted message
    intact. Returns None only if a chunk can never be balanced → the caller fails safe."""
    # Normalize CRLF/CR line endings to LF first, so a token isn't left as `commit\r` (which would
    # not match the `commit` subcommand and would let a real commit slip the gate).
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")  # honor backslash-newline line continuations
    lines = joined.split("\n")
    out: list[str] = []
    first = True
    i = 0
    while i < len(lines):
        chunk = lines[i]
        toks = _tokenize_line(chunk)
        # An unbalanced line is a quote spanning a newline: re-attach the next line(s) (newline
        # belongs INSIDE the quote) until it balances, so we don't lose the later commands.
        while toks is None and i + 1 < len(lines):
            i += 1
            chunk = f"{chunk}\n{lines[i]}"
            toks = _tokenize_line(chunk)
        if toks is None:
            return None  # never balances → fail safe
        if not first:
            out.append(";")  # the newline that started this chunk ends the previous command
        first = False
        out.extend(toks)
        i += 1
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &)."""
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


def _strip_redirects(segment: list[str]) -> list[str]:
    """Drop shell redirection from a segment so its operator/target tokens don't leak into the
    commit argv (where `>` `log` would be mis-read as pathspecs → a false docs-fast-path forfeit).

    `punctuation_chars` emits a redirect OPERATOR as a token made up SOLELY of the redirect
    punctuation `< > &` (e.g. `>`, `>>`, `>&`, `&>`), optionally preceded by a bare fd digit (`2`)
    and followed by a target word — all of which are removed. A `<`/`>` INSIDE a quoted word (a
    commit message like `-m 'a > b'`) yields a normal word token (not pure punctuation), so it is
    preserved — only pure-operator tokens are treated as redirects."""
    out: list[str] = []
    i = 0
    seen_ddash = False
    while i < len(segment):
        tok = segment[i]
        # After `--`, everything is a literal pathspec — never a redirect, and a preceding digit is
        # a real pathspec (don't pop it). Before `--`, a pure `<>&` token is a redirect operator.
        is_redir = (not seen_ddash and bool(tok) and tok not in _SHELL_SEP
                    and ("<" in tok or ">" in tok) and all(ch in "<>&" for ch in tok))
        if is_redir:
            if out and out[-1].isdigit():
                out.pop()  # drop a bare fd digit that prefixes the redirect (`2` of `2> err`)
            i += 2 if i + 1 < len(segment) else 1  # drop the operator AND its target
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


def _takes_following_value(tok: str) -> bool:
    """True when a `git commit` flag token consumes the NEXT token as its value.

    Long forms `--message`/`--file` (without `=`); and any short cluster ENDING in `m` or `F`
    (`-m`, `-am`, `-aF`) — the typical `git commit -am 'msg'`, where the message is the following
    token. Stripping it is what stops `git commit -am --skip` (message == a skip flag) from
    falsely reading `--skip` as a continuation flag and exempting a real commit.

    A GLUED short value (`-mMSG`, `-FPATH`) carries its value INSIDE the token and so does NOT take
    the following one — return False for those (the glued form is handled separately)."""
    if tok.startswith("--"):
        return tok in ("--message", "--file")  # `--message=…`/`--file=…` carry their own value
    if tok.startswith("-") and len(tok) > 1:
        if len(tok) > 2 and tok[1] in ("m", "F"):
            return False  # a glued `-mMSG` / `-FPATH` — value is in the token, not the next one
        return tok[-1] in ("m", "F")  # short cluster like -m / -am / -aF takes the next token
    return False


def _is_git_executable(tok: str) -> bool:
    """True when `tok` is the git binary — bare `git` or an absolute/relative path to it
    (`/usr/bin/git`, `./git`), which agent environments frequently use. NOT `mygit` / `git-foo`."""
    return os.path.basename(tok) == "git"


def _commit_argv(segment: list[str]) -> list[str] | None:
    """If `segment`'s executable is `git` and its subcommand is `commit`, return the tokens AFTER
    `commit`; otherwise None. Walks past git GLOBAL options (`-C dir`, `-c k=v`, …) to reach the
    subcommand. Inline env assignments must already be peeled off (see ``_split_inline_env``)."""
    if not segment or not _is_git_executable(segment[0]):
        return None
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2  # global flag + its separate value
            continue
        if tok.startswith("-"):
            i += 1  # other global flag / `-Cdir` / `-ck=v` joined form
            continue
        break
    if i >= len(segment) or segment[i] != "commit":
        return None
    return segment[i + 1:]


def _git_dir_flag(segment: list[str]) -> str | None:
    """The directory from a `git -C <dir>` global flag (separate `-C dir` or glued `-Cdir`) on the
    commit segment, or None. Used so docs-classification runs in the repo the commit TARGETS, not
    blindly in the event cwd — `git -C /other/repo commit` stages in /other/repo (codex MEDIUM)."""
    if not segment or not _is_git_executable(segment[0]):
        return None
    i = 1
    while i < len(segment) and i < 64:  # cap the walk; the subcommand is always near the front
        tok = segment[i]
        if tok == "-C" and i + 1 < len(segment):
            return segment[i + 1]
        if tok.startswith("-C") and len(tok) > 2:
            return tok[2:]  # glued `-Cdir`
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break  # reached the subcommand
    return None


def _commit_flags(argv: list[str]) -> list[str]:
    """Tokens after `commit` with EVERY value-flag AND its value removed, stopping at `--`
    (everything after `--` is a literal PATHSPEC, never a flag).

    Stripping ALL value-flag values — not just `-m`/`-F` — is a SECURITY requirement: a skip token
    sitting in another value-flag's value (`git commit --author '--skip' -m 'real'`) must NOT be
    read as a real `--skip` flag and falsely exempt the commit (codex). Covers separate values
    (`--author X`, `-m X`, message-bearing short clusters `-am X`), glued long values
    (`--author=X`), and glued short values (`-mX`, `-FX`)."""
    out: list[str] = []
    j = 0
    while j < len(argv):
        tok = argv[j]
        if tok == "--":
            break
        flag_name = tok.split("=", 1)[0]
        # A SEPARATE value: a message-bearing short cluster (`-m`/`-am`/`-aF`) or any long/short
        # value-flag in `_COMMIT_VALUE_FLAGS` (`--author X`, `-C X`, `--date X`).
        if (_takes_following_value(tok) or tok in _COMMIT_VALUE_FLAGS) and j + 1 < len(argv):
            j += 2  # drop the flag AND its value
            continue
        # A GLUED value: `-mX`/`-FX`/`--message=X`/`--author=X` etc.
        if (tok.startswith(("-m", "-F")) and len(tok) > 2) or (
                "=" in tok and flag_name in _COMMIT_VALUE_FLAGS):
            j += 1  # drop the flag-with-glued-value
            continue
        out.append(tok)
        j += 1
    return out


class CommitSegment(NamedTuple):
    env: dict[str, str]   # inline `VAR=value` env assignments prefixing the commit
    argv: list[str]       # tokens AFTER `commit`
    target_dir: str | None  # the `git -C <dir>` directory, if any (else None → use event cwd)
    alt_repo: bool        # uses `--git-dir`/`--work-tree` → the cwd index may not be this commit's
    staging_before: bool  # an index-mutating op (`git add`/…) preceded it in the chain
    cd_dir: str | None = None  # the last `cd <dir>` seen BEFORE this commit in the chain


# git subcommands that MUTATE the index before a later commit in the same chain. If one precedes a
# commit segment, the index we'd query at hook time does NOT reflect what that commit will stage —
# so the docs-only fast-path is forfeited (`git commit -m docs && git add app.py && git commit …`).
# LIMITATION: only DIRECT `git` staging segments are seen — a NON-git stager (`python stage.py &&
# git commit`, where the script runs `git add`) is invisible to this heuristic; that commit's
# docs-only fast-path would still apply. Contrived, and a process-discipline gate (on_error=open)
# — the explicit escape hatches are the intended bypass, not this.
_STAGING_SUBCMDS = frozenset({"add", "rm", "mv", "reset", "restore", "stash", "apply", "checkout"})


def _is_staging_segment(segment: list[str]) -> bool:
    """True when `segment` is a `git <staging-subcommand>` that can change the index (`git add`,
    `git rm`, `git reset`, `git restore`, …). Walks past git global options to reach the
    subcommand, like ``_commit_argv``."""
    if not segment or not _is_git_executable(segment[0]):
        return False
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return i < len(segment) and segment[i] in _STAGING_SUBCMDS


# git env vars that redirect which repo/index a command operates on. Inline (`GIT_DIR=… git
# commit`) they make the event-cwd index unrepresentative of the commit — same as `--git-dir`.
_ALT_REPO_ENV = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})


def _uses_alt_repo(segment: list[str], env: dict[str, str]) -> bool:
    """True when this commit points at a DIFFERENT repo/index than the event cwd — via the
    `--git-dir`/`--work-tree` global flags (with or without `=`) OR an inline `GIT_DIR`/
    `GIT_WORK_TREE`/`GIT_INDEX_FILE` env assignment. The docs-only fast-path queries the cwd index,
    which then would not represent this commit — so the caller forfeits the fast-path (safe dir).

    Walks past `_GIT_GLOBAL_VALUE_FLAGS` AND their SEPARATE values (like ``_commit_argv``), so a
    value such as `-c foo=bar` does not terminate the scan before reaching a later `--git-dir`."""
    if any(k in _ALT_REPO_ENV for k in env):
        return True
    dash_c = 0
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok.split("=", 1)[0] in ("--git-dir", "--work-tree"):
            return True
        if tok == "-C" or (tok.startswith("-C") and len(tok) > 2):
            dash_c += 1  # `git -C a -C b` resolves to a/b; ``_git_dir_flag`` only sees the first,
            if dash_c > 1:  # so >1 `-C` means the cwd we'd classify can differ from the real target
                return True
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2  # global flag + its separate value (don't read the value as a flag)
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break  # reached the subcommand / a positional
    return False


def _is_cd_segment(segment: list[str]) -> str | None:
    """Return the target directory from a simple `cd <dir>` segment, or None.

    Only the common positional form ``cd /path/to/dir`` (exactly one non-flag argument) is
    recognised — the typical pattern agents use to enter a worktree before committing.
    Bare ``cd`` (→ $HOME), ``cd -`` (→ prev dir), and any flag-style argument are left as
    None because they cannot be statically resolved to an absolute path."""
    if not segment or segment[0] != "cd":
        return None
    if len(segment) == 2 and not segment[1].startswith("-"):
        return segment[1]
    return None


def _commit_segments(command: str) -> list[CommitSegment]:
    """Every real `git commit` segment in `command` (a chain may hold more than one, e.g.
    `git commit … && git commit …`). Empty list when there are none / on a tokenization failure.

    Parses the argv after stripping shell comments, splits on separators (GLUED ones too, via
    ``_tokenize``), peels inline env, and keeps each segment whose executable IS git (see
    ``_is_git_executable``) and subcommand is `commit`.

    Also tracks the last simple `cd <dir>` segment seen before a commit in the chain, so that
    ``cd /worktree && git commit`` correctly resolves the effective cwd for docs-only
    classification (the CC PreToolUse event carries the SESSION cwd, not the post-cd cwd).

    LIMITATION: a WRAPPED commit run through another program — `time git commit`, `sudo git
    commit`, `bash -c 'git commit …'`, a `(git commit …)` subshell, `env VAR=1 git commit` — is not
    recognized and is therefore NOT gated. This is the deliberate trade for precision: matching
    `commit` as a bare substring (the old behavior) false-blocked innocent commands like `git help
    commit` / `git config commit.gpgsign` / `git commit-graph`. The gate is process discipline
    (on_error=open), not a security boundary, so under-matching an unusual wrapper is acceptable;
    the common direct + absolute-path forms are fully covered."""
    tokens = _tokenize(command)
    if tokens is None:
        return []
    out: list[CommitSegment] = []
    staging_seen = False
    cd_dir_seen: str | None = None

    # Inline the token iteration (rather than calling _segments) so we can inspect the
    # separator token BETWEEN segments and decide whether to carry `cd_dir_seen` forward.
    # Only `&&` / `;` / case fall-through separators run the next segment in the same shell
    # process; pipes and `&` use subshells, so a preceding `cd` cannot affect the next cwd.
    def _flush(raw_seg: list[str]) -> None:
        nonlocal staging_seen, cd_dir_seen
        seg = _strip_redirects(raw_seg)  # drop `> log` etc. so they don't leak into the argv
        env, rest = _split_inline_env(seg)
        argv = _commit_argv(rest)
        if argv is not None:
            out.append(CommitSegment(env, argv, _git_dir_flag(rest),
                                     _uses_alt_repo(rest, env), staging_seen, cd_dir_seen))
        elif _is_staging_segment(rest):
            staging_seen = True  # a later commit's index will differ from the one we'd query now
        else:
            cd = _is_cd_segment(rest)
            if cd is not None:
                cd_dir_seen = cd  # propagate to subsequent commit segments in this chain

    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEP:
            _flush(cur)
            if tok not in _CD_CARRY_SEP:
                cd_dir_seen = None  # pipe/background/|| can't propagate a preceding `cd`
            cur = []
        else:
            cur.append(tok)
    _flush(cur)
    return out


def _commit_segment(command: str) -> CommitSegment | None:
    """The FIRST real `git commit` segment in `command`, or None. Convenience for the string-level
    wrappers / unit tests; ``main`` iterates ALL segments via ``_commit_segments``."""
    segs = _commit_segments(command)
    return segs[0] if segs else None


def is_skip_commit_argv(argv: list[str]) -> bool:
    """True when the parsed `git commit` argv carries --continue/--abort/--skip.

    A skip token that lives only in a comment (`git commit -m x # --abort`), in the commit message
    (`git commit -m 'support --skip'`), or on a SIBLING command (`git rebase --abort && git commit
    -m x`) is not part of this argv (the caller scopes it to the real commit segment), so it does
    NOT exempt an authoring commit."""
    return any(tok in SKIP_FLAGS for tok in _commit_flags(argv))


# Thin string-level wrapper (parse + delegate) — convenient for unit tests and external callers.
def is_skip_commit(command: str) -> bool:
    """True only when the real `git commit` SEGMENT carries --continue/--abort/--skip. On a
    tokenization failure / non-commit → False (a real commit is then GATED, the safe way)."""
    seg = _commit_segment(command)
    return seg is not None and is_skip_commit_argv(seg.argv)


# ── docs-only classification ─────────────────────────────────────────────────────────────────


def staged_files(cwd: str) -> list[str] | None:
    """Names of files staged for commit (the INDEX), or None if git could not be queried (→ fail
    open). Note: this reads the index only, so a `git commit -a/-am` (which stages working-tree
    changes at commit time) shows nothing here → the docs-only fast-path does not fire and the gate
    stays — the safe direction (a docs `git commit -am` just isn't auto-skipped)."""
    try:
        proc = subprocess.run(  # noqa: S603,S607 — fixed git argv, trusted
            # `-z` → NUL-separated, VERBATIM paths: no octal-escaped quoting of non-ASCII names
            # (`café.md`) and no breakage on a (pathological) newline-in-filename — both of which a
            # plain `--name-only` line split would mis-handle and so defeat the docs-suffix match.
            ["git", "-C", cwd, "diff", "--cached", "--name-only", "-z"],
            capture_output=True,
            text=True,
            errors="replace",  # a non-UTF-8 path under a non-UTF-8 locale must not crash the decode
            timeout=GIT_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # None = "could not classify" → caller forfeits the docs-only fast-path (NOT an allow);
        # the marker gate still runs.
        warn(f"could not list staged files: {exc} — skipping docs-only fast-path")
        return None
    if proc.returncode != 0:
        warn(f"git diff --cached exited {proc.returncode}: {proc.stderr.strip()} — "
             "skipping docs-only fast-path")
        return None
    return [name for name in proc.stdout.split("\0") if name.strip()]


def is_docs_path(path: str) -> bool:
    """True for a documentation path: a doc EXTENSION anywhere, or a file under a `docs/` directory
    that is NOT itself code/config. A code/config file (even named like docs, e.g. `docs/conf.py`,
    `docs/Makefile`, `requirements.txt`) is never docs — review should still see it."""
    if CODE_EXT.search(path) or os.path.basename(path).lower() in CODE_BASENAMES:
        return False  # source/script/config is never docs, even under docs/
    if DOCS_EXT.search(path):
        return True
    return bool(DOCS_DIR.search(path))


def is_docs_only(files: list[str]) -> bool:
    """True when there is at least one staged file and EVERY staged path is a docs path."""
    return bool(files) and all(is_docs_path(f) for f in files)


# `git commit` options that consume a SEPARATE following value which is NOT a pathspec. Listing
# them stops the value (`--author 'Jane <j@x>'`, `--date …`, `-C <commit>`) from being misread as a
# pathspec by ``commit_extends_index`` and wrongly disabling the docs-only fast-path (codex MEDIUM).
# NOTE: only flags whose value is MANDATORY and is NOT a pathspec are listed. Flags with an
# OPTIONAL argument (`-S/--gpg-sign`, `-u/--untracked-files`, `--cleanup`) are deliberately omitted:
# if we assumed they took the next token and it were actually a pathspec, we'd UNDER-block. So is
# `--pathspec-from-file` — its value IS a pathspec source (it commits paths listed in that file,
# bypassing the index), so it must make ``commit_extends_index`` return True, not be skipped.
# Omitting these errs to over-block (the safe direction for a review gate).
_COMMIT_VALUE_FLAGS = frozenset({
    "-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
    "--fixup", "--squash", "--author", "--date", "-t", "--template",
})
# Flags meaning the commit draws from BEYOND the staged index — pathspecs from a file, or an
# interactive/patch/only/include selection made at commit time — so `git diff --cached` (the index)
# does not represent what gets committed and the docs-only fast-path cannot be trusted.
_INDEX_EXTENDING_FLAGS = frozenset({
    "--pathspec-from-file", "-p", "--patch", "-i", "--interactive", "--include", "--only", "-o",
})
# Short single-letter `git commit` flags whose VALUE is GLUED to the rest of the cluster (so the
# letters AFTER them are the value, not more flags): `-mMSG`, `-FPATH`, `-CCOMMIT`, `-tTPL`,
# `-Skeyid`, `-uMODE`. Reaching one of these in a cluster ends the index-extension scan.
_SHORT_VALUE_LETTERS = frozenset("mFCctSu")
# Short flags that, alone or anywhere in a no-value cluster, mean the commit draws beyond the index.
_INDEX_EXTENDING_LETTERS = frozenset("apio")


def _cluster_extends_index(tok: str) -> bool:
    """For a short-flag cluster (`-am`, `-pm`, `-uno`, `-Skeyid`), True iff a flag that draws beyond
    the index (`a`/`p`/`i`/`o`) appears BEFORE any glued-value flag. A glued-value flag (`m`, `F`,
    `C`, `t`, `S`, `u`, …) consumes the REST of the cluster as its value, so letters after it (e.g.
    the `o` of `-uno`, the `i` of `-Skeyid`) are NOT flags and must not trip the gate (codex)."""
    for ch in tok[1:]:
        if ch in _INDEX_EXTENDING_LETTERS:
            return True
        if ch in _SHORT_VALUE_LETTERS:
            return False  # rest of the cluster is this flag's value
    return False


def commit_extends_index(argv: list[str]) -> bool:
    """True when the commit will include content the staged-index list does NOT capture — i.e.
    `git commit -a/--all` (auto-stages tracked working-tree edits) or an explicit PATHSPEC (which
    commits the named files regardless of the index). In either case `git diff --cached` (the
    index) is an INCOMPLETE picture of what gets committed, so the docs-only fast-path must NOT be
    trusted: a docs-only index plus an unstaged code edit (`git commit -am`) or a code pathspec
    (`git commit -- src/x.py`) would otherwise wave code through un-reviewed (codex HIGH).

    Value-flags (message, `--author`, `--date`, …) and their values are skipped first, so a benign
    `-m all`/`--author 'A'` value is not misread as `-a` or a pathspec. A short cluster that
    contains `a` (`-am`, `-av`) IS detected even though it also carries a message. Anything after a
    literal `--` is a pathspec."""
    j = 0
    while j < len(argv):
        tok = argv[j]
        if tok == "--":
            return j + 1 < len(argv)  # at least one path follows `--`
        # Skip value-flags (and glued values) BEFORE the -a-cluster test, so a glued `-madd`
        # (message "add") / `-Fchanges.txt` / `--author 'A'` value is not misread as `-a` or a
        # pathspec via a stray `a` in the glued value.
        if (tok.startswith(("--message=", "--file="))
                or (tok.startswith(("-m", "-F")) and len(tok) > 2)):
            j += 1  # glued `-mMSG` / `-FPATH` / `--message=MSG` / `--file=PATH`
            continue
        flag_name = tok.split("=", 1)[0]
        if tok in _INDEX_EXTENDING_FLAGS or flag_name in _INDEX_EXTENDING_FLAGS:
            return True  # `--pathspec-from-file` commits paths from a file, bypassing the index
        if "=" in tok and flag_name in _COMMIT_VALUE_FLAGS:
            j += 1  # glued long value-flag, e.g. `--author=Jane`
            continue
        if tok in ("-a", "--all"):
            return True
        if tok.startswith("-") and not tok.startswith("--") and _cluster_extends_index(tok):
            return True  # a short cluster with -a/-p/-i/-o (auto-stage / patch / interactive / only)
        if tok in _COMMIT_VALUE_FLAGS and j + 1 < len(argv):
            j += 2  # value-flag with a SEPARATE value (`-m MSG`, `--author A`, `-C <commit>`)
            continue
        if _takes_following_value(tok) and j + 1 < len(argv):
            j += 2  # a short cluster ending in m/F (`-nm 'msg'`, `-qF path`) takes the NEXT token —
            continue  # don't read that message/path value as a pathspec (codex over-block fix)
        if not tok.startswith("-"):
            return True  # a bare positional = a pathspec (`git commit file`)
        j += 1  # some other no-value option flag (e.g. --amend, --no-edit, --signoff)
    return False


# ── skip resolution + marker freshness ───────────────────────────────────────────────────────


def _marker_is_fresh() -> bool | None:
    """True/False if the review marker exists-and-is-fresh; None if it could not be stat'd."""
    marker = marker_path()
    try:
        if marker.exists() and (time.time() - marker.stat().st_mtime) <= FRESH_WINDOW_S:
            return True
    except OSError as exc:
        warn(f"could not stat review marker {marker}: {exc} — allowing (fail-open)")
        return None
    return False


def _block(prefix: str | None = None) -> int:
    marker = marker_path()
    body = (
        "No recent AI code review found for this change. Run a review on the "
        "uncommitted diff (e.g. `review` / `codex exec review --uncommitted`) and "
        f"address its findings before committing. (Set/touch {marker} on a successful "
        "review, or set REVIEW_MARKER. A PURE-docs commit auto-allows — but only the simple "
        "form: `git commit -a`/`-am`, a trailing pathspec, or a preceding `git add` of other "
        "files FORFEITS that fast-path (the commit may include un-reviewed non-docs changes), "
        "so a docs-only diff can still land here — stage just the docs and `git commit` them "
        "alone.) There is NO self-service skip any more (`REVIEW_SKIP` / `[skip-review: …]` are "
        "gone). For a genuine one-time exception, ASK the human, or request a single approval "
        'via RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="<why>" — that routes a Telegram '
        "approval request to Alex (deny-by-default; a bare `1` is rejected)."
    )
    emit("block", f"{prefix}\n{body}" if prefix else body)
    return BLOCK_EXIT_CODE


def _allow() -> int:
    emit("allow")
    return 0


def _is_exempt_skip(seg: CommitSegment) -> bool:
    """A CHEAP (no subprocess) check: this commit segment opts out of review via a skip flag
    (`--continue`/`--abort`/`--skip`). Does NOT cover docs-only — that needs a `git diff` and is
    only worth running when no fresh marker exists. There is no per-commit self-service skip any
    more; a one-time exception is an external Telegram hatch (see `main`)."""
    return is_skip_commit_argv(seg.argv)  # inert for git commit; kept for sibling-parser SYNC parity


def _is_docs_only_commit(seg: CommitSegment, cwd: str) -> bool:
    """True when this commit is a TRUSTWORTHY docs-only commit (runs a `git diff` — call only when
    it actually decides the outcome, i.e. no fresh marker).

    The docs-only fast-path trusts the staged INDEX to represent the commit. That trust holds only
    for a plain `git commit` (commit == index). It is forfeited when: the commit draws beyond the
    index (`-a`/pathspec/`-p`/`--pathspec-from-file`); it targets another repo (`--git-dir`/inline
    `GIT_DIR=`); or a staging op earlier in the chain will change the index before this commit runs
    (`git commit -m docs && git add app.py && git commit …`)."""
    if commit_extends_index(seg.argv) or seg.alt_repo or seg.staging_before:
        return False
    # Classify docs-only in the repo the commit TARGETS.
    # Priority: explicit `git -C <dir>` flag > preceding `cd <dir>` in the chain > event cwd.
    # The CC PreToolUse event fires BEFORE the shell command runs, so its `cwd` is the SESSION
    # default (main repo root), not the post-cd directory.  A `cd /worktree && git commit`
    # chain means the commit's effective cwd is /worktree — use it for index classification.
    #
    # Effective-cwd resolution order:
    # 1. The post-cd effective cwd (from a preceding `cd` in the chain; may be relative to event cwd).
    # 2. The event cwd (if no cd preceded this commit).
    # Then, if `git -C <dir>` is present, it is applied ON TOP of the effective cwd — so a
    # relative `-C sub` after `cd /worktree` targets `/worktree/sub`, not `<event-cwd>/sub`.
    effective_cwd = cwd
    if seg.cd_dir:
        effective_cwd = (seg.cd_dir if os.path.isabs(seg.cd_dir)
                         else os.path.join(cwd, seg.cd_dir))
    git_cwd = effective_cwd
    if seg.target_dir:
        git_cwd = (seg.target_dir if os.path.isabs(seg.target_dir)
                   else os.path.join(effective_cwd, seg.target_dir))
    files = staged_files(git_cwd)
    if files is not None and is_docs_only(files):
        warn("docs-only commit — review not required")
        return True
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
    cwd = str(event.get("cwd") or os.getcwd())

    # The argv PARSER is the sole authority: `_commit_segments` returns EVERY real `git commit`
    # segment (a chain may have several) — never a `git stash` / `git config commit.gpgsign` / `git
    # help commit` / `git commit-graph`, a `commit` token in a comment / message / pathspec, or a
    # non-`git` executable. It strips comments and splits glued separators itself, so no regex
    # pre-filter is needed (a pre-filter that early-returns `allow` would re-introduce
    # miss-classification bugs, e.g. `GIT_AUTHOR_NAME='Jane Doe' git commit`, a spaced env value a
    # regex anchor can't follow). Gate if ANY segment is a real authoring commit needing review —
    # a skip-flag first commit (`git rebase --abort && git commit -am big`) must not shield a
    # second authoring one (codex MEDIUM).
    segments = _commit_segments(command)
    # CHEAP first: drop the segments that opt out via a skip flag (--continue/--abort/--skip).
    authoring = [seg for seg in segments if not _is_exempt_skip(seg)]
    if not authoring:
        return _allow()  # no segment, or every commit segment is skip-exempt → nothing to gate

    # A fresh marker allows ANY commit → check it (a cheap stat) BEFORE the docs-only `git diff`, so
    # the common "ran review → commit" path pays no subprocess. None = marker un-stat'able → fail open.
    fresh = _marker_is_fresh()
    if fresh is None or fresh:
        return _allow()

    # No fresh marker: the only thing that can still exempt a commit is being a trustworthy
    # docs-only one. If EVERY authoring segment is docs-only → allow; otherwise consult the
    # external Telegram hatch before blocking (deny-by-default; env unset → normal block).
    if all(_is_docs_only_commit(seg, cwd) for seg in authoring):
        return _allow()

    hatch = hatch_escalation.request_hatch_approval(
        "require-review-before-commit",
        {"hook": "require-review-before-commit", "command": command},
        cwd=cwd,
        command=command,
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"review gate allowed via hatch escalation ({hatch.reason})")
            return _allow()
        warn(f"review gate hatch escalation denied: {hatch.reason}")
        return _block(prefix=f"hatch escalation denied: {hatch.reason}")
    return _block()


if __name__ == "__main__":
    sys.exit(main())
