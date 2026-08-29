#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require a looked-at screenshot before a UI commit.

Fires on a `git commit`. If the staged change touches USER-VISIBLE files (a component, a
stylesheet, an image, a page/view) it BLOCKs unless a fresh "I looked at a screenshot"
marker exists. It enforces `visual-proof-cycle`: capture the rendered result, read the
capture back, verify it — THEN commit. A "done" claim on a UI change with no screenshot you
actually looked at is the exact failure this gate stops.

What counts as user-visible (staged file inspection):
  - an extension match: .tsx/.jsx/.vue/.svelte/.css/.scss/.less/.html/.svg/.png/.jpg/.jpeg/.gif/.webp
  - OR a path under components/ ui/ pages/ app/ views/ public/ assets/
  If NO user-visible file is staged → allow (nothing to prove).

The marker contract (how it knows a screenshot was looked at — agent-tools#475):
  Two kinds of marker, both scanned from ~/.cache/agent-tools/visual-proof/ (configure the dir
  with VISUAL_PROOF_DIR), both must be FRESH (within VISUAL_PROOF_WINDOW_S, default 3600s):

  1. PRIMARY — a dev-cli `dev shot <url> --out /tmp/shot.png` attestation (`attest-*.json`,
     written by dev-cli's `write_attestation()`; the output path MUST be OUTSIDE the repo — see
     the README for why). This gate accepts the record only when ALL of:
     `repo` is this commit's resolved git toplevel; `staged_sha256` is the sha256 of THIS
     commit's `git diff --cached` right now; `worktree_dirty` was recorded as exactly `False`
     (a dirty worktree at capture time means the browser may have rendered content that was
     never staged at all); and `capture_sha256` matches a FRESH re-hash of the file at
     `capture_path` (proves an actual capture file exists, not just two `git`-derived facts a
     forger could type by hand). A record failing any one of these does not satisfy the gate.
  2. FALLBACK (no URL to shoot — a docs-only visual change, a generated image, a schematic per
     visual-proof-cycle) — `visual_proof_gate.py --write-marker` writes a plain marker whose
     content is this commit's resolved git toplevel path. Weaker than (1) — it is not bound to
     the staged diff — but still repo-scoped and content-checked, unlike the old bare `touch`.

  BOTH close the same hole: previously ANY fresh file, of ANY content, anywhere in the shared
  marker directory satisfied EVERY repo's gate machine-wide (agent-tools#475) — an agent
  screenshotting repo A silently satisfied repo B's unrelated commit for up to an hour, and a
  junk file dropped there by anything else worked just as well as a real screenshot. See
  `_proof_fresh` for the validation.

  KNOWN, ACCEPTED GAP (review finding, round 4): this hook is a `pre-bash` hook — it inspects
  the command about to run and returns a verdict BEFORE the shell actually executes `git
  commit`. There is no lock between "this hook approved the currently-staged diff" and "the
  shell's `git commit` reads the index a moment later" — a concurrent `git add`/`git reset` in
  that gap can change what actually gets committed after the check already passed. This is not
  specific to the diff-hash binding added here; it is inherent to every check this whole file
  performs (`staged_files`, the skip-flag parse, all of it) and to the pre-bash hook model in
  general — a hook that only sees an event over stdin has no way to make the subsequent shell
  execution atomic with its own check. Closing it would need the actual `git commit` process
  itself to be wrapped/locked, which is out of a pre-bash hook's reach. Matches this file's
  stated doctrine (`on_error: "open"`: process discipline, not a security boundary) — accepted
  here rather than silently ignored.

This gate straight-BLOCKs (doctrine: "block a commit ... with no attached screenshot"), but
is satisfiable (touch the marker after you VIEW the capture).

NOTE: NOT subagent-exempt — a subagent committing UI work must also have looked at the result.

No self-service bypass. There is NO env var / inline sentinel an agent can set on its own
command to skip this gate — a self-grant is security theater. If you have a genuine reason to
commit UI work without a fresh screenshot marker, ASK the human, or request a ONE-TIME Telegram
approval via `RIG_HATCH_REQUEST_VISUAL_PROOF_GATE="<justification>"` (deny-by-default; a blank
or bare `1` is rejected — the value must be a real written justification). The request routes to
the human over Telegram (tg-ctl) and allows ONLY on their approval.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the repo cwd in event.cwd
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": process discipline, not a security boundary. The `git diff --cached`
subprocess is timeout-bounded and fails OPEN (if git errors, allow) — a broken stat must
never wedge committing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess  # noqa: S404 — listing staged files is the whole job
import sys
import tempfile
import time
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

PROOF_DIR = Path(os.path.expanduser(os.environ.get(
    "VISUAL_PROOF_DIR", "~/.cache/agent-tools/visual-proof")))
PROOF_WINDOW_S = int(os.environ.get("VISUAL_PROOF_WINDOW_S", "3600"))
# Intentionally < the descriptor's `timeout_ms` (8000): the inner python `git diff` timeout
# must fire FIRST and fail OPEN (allow), rather than the bridge killing the whole hook on its
# own timeout. Don't tighten the descriptor to 5000 — the gap is the safety margin (#14).
GIT_DIFF_TIMEOUT_S = 5

# Anchored to a COMMAND invocation (line start, or after a |/&/; separator) with `commit` as
# git's subcommand. Global flags AND their values (`git -C /repo commit`, `git -c k=v commit`)
# are allowed between `git` and `commit`, but the run may NOT cross a command separator — so
# plain text such as `echo "remember to git, then commit"` does NOT trip it (B2).
GIT_COMMIT = re.compile(r"(?:^|[|&;]\s*)git(?:[ \t]+[^\s;&|]+)*?[ \t]+commit\b")
# A rebase/merge plumbing step (`git commit --continue/--abort/--skip`) is not authoring a UI
# change → not gated. This is detected from the PARSED argv (see ``is_skip_commit``), NOT from
# the raw string: a token that only appears in a shell COMMENT (`git commit -m x # --abort`) or
# in the commit MESSAGE (`git commit -m 'support --skip'`) must NOT exempt a real commit.
SKIP_FLAGS = frozenset({"--continue", "--abort", "--skip"})

VISUAL_EXT = re.compile(
    r"\.(?:tsx|jsx|vue|svelte|css|scss|less|html|svg|png|jpg|jpeg|gif|webp)$",
    re.IGNORECASE,
)
VISUAL_DIR = re.compile(r"(?:^|/)(?:components|ui|pages|app|views|public|assets)/", re.IGNORECASE)

# Canonical hook id for the shared Telegram hatch (RIG_HATCH_REQUEST_VISUAL_PROOF_GATE).
HOOK_ID = "visual-proof-gate"


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"visual-proof-gate: {msg}\n")


def _strip_shell_comment(command: str) -> str:
    """Drop a trailing shell comment (`# …`) that the shell never executes.

    Only an UNQUOTED `#` that starts a word begins a comment, so a `#` inside a quoted commit
    message (`-m 'fix #42'`) or glued to a token (`color=#fff`) is preserved. Best-effort: on a
    tokenization failure (unbalanced quotes) the raw command is returned unchanged."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return command
    return " ".join(tokens)


# A leading inline `VAR=value` env-assignment run at a command head (line start or right after a
# `|`/`&`/`;` separator). A real shell applies it as environment to the following command, so it
# is transparent to WHICH command runs — but it pushes `git` off the command head and defeats the
# `GIT_COMMIT` anchor. Chief case: the documented inline hatch form
# `RIG_HATCH_REQUEST_VISUAL_PROOF_GATE="why" git commit …`, which must still be detected as a
# commit (else the gate silently allows it, never reaching the Telegram hatch). Stripped only for
# detection. SYNC with skills_read_gate.py's `_strip_leading_inline_env`.
_INLINE_ENV_PREFIX = re.compile(
    r"(?P<sep>^|[|&;]\s*)"
    r"(?:[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|[^\s|&;]+)[ \t]+)+"
)


def _strip_leading_inline_env(command: str) -> str:
    """Drop leading `VAR=value` env-assignment runs at each command head, so a command whose real
    executable is prefixed by inline env (`RIG_HATCH_REQUEST_…="why" git commit`) is detected as
    that executable. Prose stays safe: assignments inside quotes are not at a command head."""

    return _INLINE_ENV_PREFIX.sub(lambda m: m.group("sep"), command)


# SYNC: the commit-segment parser below (_segments / _commit_flags / is_skip_commit) is mirrored
# in skills-read-gate/skills_read_gate.py — each hook is a self-contained standalone script run as
# its own subprocess (no shared import path), so the logic is duplicated by design. Keep both in
# step when changing skip-flag handling.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&"})
# The separators after which a `cd` runs in the SAME shell process (so its cwd change actually
# persists forward) rather than a subshell — matches require_review.py's validated `_CD_CARRY_SEP`
# model. `&&`/`;` both qualify: the overwhelmingly common, benign pattern IS `setup && cd target
# && git commit` (or `mkdir -p dir && cd dir && ...`), where — if the commit actually runs at
# all — every `&&` link up to it, including the `cd`, necessarily succeeded; treating `&&` as
# untrustworthy here would break that common case for a narrow, deliberately-adversarial
# construction this hook's own doctrine doesn't try to defend against (`on_error: open` —
# process discipline, not a security boundary). `||` is excluded: it runs its right side only
# when the LEFT side FAILED, so a `cd X || next` commit that actually executes means `cd X`
# did NOT succeed — trusting `X` there would be backwards.
_CD_TRUSTED_SEP = frozenset({"&&", ";"})
# The separator immediately AFTER a `cd` that puts the `cd` itself into a subshell/background
# job — `cd /x | next` (pipeline: the left side is a subshell) and `cd /x & next` (backgrounded)
# both mean the `cd`'s cwd change never reaches the shell that runs whatever follows, even
# though the `cd` was otherwise a trusted, leading/`;`-preceded segment. See the cd-trust check.
_CD_SUBSHELL_SEP = frozenset({"|", "&"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# A leading `(` (subshell) — or `{` (group command), included defensively even though `{`
# requires a following space in valid shell syntax and so never glues — immediately before a
# segment's first real word. Matches only a RUN of pure grouping punctuation, so it strips a
# whole standalone token (`"("`) but never eats into an actual word that merely starts with one.
_LEADING_GROUPING = re.compile(r"^[({]+$")
# Same characters, but for stripping a GLUED prefix off a token that also has a real word
# attached (`"(cd"` -> `"cd"`) — `shlex.split` glues shell-grouping punctuation onto the very
# next word when there's no space between them (`(cd repoB`), while a spaced form (`( cd
# repoB`) emits the marker as its own token instead (caught by `_LEADING_GROUPING` above).
_LEADING_GROUPING_PREFIX = re.compile(r"^[({]+")


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &)."""
    return [seg for _sep, seg in _segments_with_preceding_sep(tokens)]


def _segments_with_preceding_sep(tokens: list[str]) -> list[tuple[str | None, list[str]]]:
    """Like `_segments`, but each segment is paired with the separator token immediately
    BEFORE it (`None` for the first segment). `effective_cwd` needs this to tell whether a
    `cd` was unconditionally reached or only gated behind `&&`/`||`/a subshell — see
    `_CD_TRUSTED_SEP` at its call site."""
    out: list[tuple[str | None, list[str]]] = []
    cur: list[str] = []
    prev_sep: str | None = None
    for tok in tokens:
        if tok in _SHELL_SEP:
            out.append((prev_sep, cur))
            prev_sep = tok
            cur = []
        else:
            cur.append(tok)
    out.append((prev_sep, cur))
    return out


def _strip_leading_grouping(seg: list[str]) -> list[str]:
    """Drop/strip a leading shell-grouping marker (`(`/`{`) so a `cd` right inside a subshell
    or group command — `(cd repoB && ...)` / `( cd repoB && ... )` — is recognized as `cd`,
    not left glued to (or hidden behind) the grouping punctuation.

    `shlex.split` produces two different token shapes for the SAME construct depending on
    whether the marker has a following space (agent-tools#201, PR #176 review finding):
      - glued   (`(cd repoB ...`)  -> ONE token, `"(cd"` — the marker is glued onto `cd`.
      - spaced  (`( cd repoB ...`) -> the marker is its OWN token, `"("`, ahead of `"cd"`.
    Before this, `seg[0] == "cd"` matched neither shape, so a subshell-wrapped `cd` was
    silently untracked and `effective_cwd` fell back to `session_cwd` — checking the wrong
    repo's staged files instead of the one the commit actually runs in. Handles a run of
    more than one marker (`((cd`, `(( cd`) the same way; a `(`/`{` that isn't a PURE leading
    marker (i.e. anywhere but the very front of the segment) is left untouched."""
    while seg and _LEADING_GROUPING.fullmatch(seg[0]):
        seg = seg[1:]
    if seg:
        stripped = _LEADING_GROUPING_PREFIX.sub("", seg[0])
        if stripped != seg[0]:
            seg = [stripped, *seg[1:]]
    return seg


_LEADING_OPEN_PARENS = re.compile(r"^\(*")


def _leading_open_paren_count(tok: str) -> int:
    """How many `(` characters `tok` OPENS with (0 for a token like `repoB)` that only
    closes). Used to compute the paren-depth threshold at the exact moment a subshell-`cd`
    is recognized — see `_subshell_cd_still_trusted_at_commit`."""
    return len(_LEADING_OPEN_PARENS.match(tok).group())


def _subshell_cd_still_trusted_at_commit(
    segments_with_sep: list[tuple[str | None, list[str]]], cd_index: int, commit_index: int,
) -> bool:
    """True if the `(` subshell that scoped a trusted `cd` at segment `cd_index` is STILL the
    innermost open one continuously from that `cd`'s own opening token through to (not
    including) the commit segment at `commit_index`.

    Regression (review finding on agent-tools#201's own fix): a `cd` recognized because its
    segment carried a leading `(` is only actually reached BY THE COMMIT if the SAME subshell
    that `cd` ran inside is still open when the commit executes — `(cd repoB && git commit -m
    x)` (subshell wraps the commit too) resolves to repoB correctly, but `(cd repoB && true) ;
    git commit -m x` (subshell CLOSES before the commit, a realistic "cd elsewhere, do a
    thing, then commit back home" idiom) must NOT resolve to repoB: a real shell's subshell is
    a forked child process, so its `cd` never persists past the subshell's own closing `)` —
    the commit after it actually lands in the ORIGINAL directory.

    Tracks paren depth token-by-token across the WHOLE command (not just before/after
    snapshots): `depth_at_cd` is the depth right after the `cd` segment's own opening
    marker; from that token onward — through the rest of ITS OWN segment, then every later
    segment up to the commit — the RUNNING MINIMUM depth reached must never dip below
    `depth_at_cd`. A single dip means the subshell closed at some point, EVEN IF an
    unrelated SIBLING subshell later reopens back to the same nesting depth (review finding,
    round 2): `(cd /decoy && true) ; (echo ok && git commit -m x)` — the first subshell
    (with the `cd`) closes, then a wholly separate second subshell (no `cd` inside it at
    all) happens to reopen to the SAME depth before the commit. Comparing only the depth
    value right before the commit can't tell these apart (both read "depth 1"); the RUNNING
    MINIMUM can, because it dips to `depth_at_cd - 1` in between regardless of what
    re-opens afterward. Still not full nesting-aware matching (which exact subshell — by
    position — reopened), but sufficient for the shapes review has actually surfaced; any
    deeper imprecision is scoped identically to this file's other documented gaps.

    Deliberately `(`/`)` ONLY, NOT `{`/`}`: unlike a subshell, `{ cmds; }` is a plain GROUP
    command that runs in the CURRENT shell (no fork) — a `cd` inside it genuinely persists
    past the closing `}`, so counting `}` as a closer here would wrongly distrust a `{ cd
    repoB; } ; git commit -m x` chain that a real shell resolves to repoB just fine."""
    depth = 0
    depth_at_cd: int | None = None
    min_after: int | None = None
    for i in range(commit_index):
        for j, tok in enumerate(segments_with_sep[i][1]):
            depth += _leading_open_paren_count(tok)
            if i == cd_index and j == 0:
                depth_at_cd = depth  # threshold recorded BEFORE this same token's own close
            if tok.endswith(")"):
                depth -= 1
            if depth_at_cd is not None:
                min_after = depth if min_after is None else min(min_after, depth)
    if depth_at_cd is None:
        return True  # cd_index wasn't reached before commit_index — caller-guarded, shouldn't happen
    return min_after is None or min_after >= depth_at_cd


def _takes_following_value(tok: str) -> bool:
    """True when a `git commit` flag token consumes the NEXT token as its value.

    Long forms `--message`/`--file`/`--trailer` (without `=`); and any short cluster ENDING in
    `m` or `F` (`-m`, `-am`, `-aF`) — the typical `git commit -am 'msg'`, where the message is
    the following token. Stripping it is what stops `git commit -am --skip` (message == a skip
    flag) from falsely reading `--skip` as a continuation flag and exempting a real commit
    (codex). `--trailer <token>[(=|:)<value>]` is the same shape: per `git commit -h`, the
    whole bracketed value is ONE following token, so `git commit --trailer --skip -m x` must not
    let the trailer's VALUE (`--skip`) leak out and be misread as a real skip flag (codex)."""
    if tok.startswith("--"):
        return tok in ("--message", "--file", "--trailer")  # `=`-glued forms carry their own value
    if tok.startswith("-") and len(tok) > 1:
        return tok[-1] in ("m", "F")  # short cluster like -m / -am / -aF takes the next token
    return False


def _commit_flags(segment: list[str]) -> list[str] | None:
    """If `segment`'s executable is `git` and its subcommand is `commit`, return the tokens AFTER
    `commit` with message-carrying flags AND their values removed; otherwise None. Walks past git
    GLOBAL options (`-C dir`, `-c k=v`, …) to reach the subcommand."""
    if not segment or segment[0] != "git":
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
    out: list[str] = []
    j = i + 1
    while j < len(segment):
        tok = segment[j]
        if tok == "--":
            break  # everything after `--` is a literal PATHSPEC, never a flag — stop collecting
        if _takes_following_value(tok) and j + 1 < len(segment):
            j += 2  # drop the flag AND its value (-m MSG / -am MSG / --message MSG / -F PATH)
            continue
        if tok.startswith(("--message=", "--file=", "--trailer=")) or (
            tok.startswith("-m") and len(tok) > 2
        ):
            j += 1  # drop -mMSG / --message=MSG / --file=PATH / --trailer=VAL (glued to the flag)
            continue
        out.append(tok)
        j += 1
    return out


def _git_c_values(segment: list[str]) -> list[str]:
    """Return every `-C <dir>` (or `-C<dir>` joined form) global flag on a git invocation, IN
    ORDER, or [] if there are none. git chains repeated `-C`: each successive one is resolved
    RELATIVE TO THE PREVIOUS one (`git -C a -C b commit` runs in `<cwd>/a/b`, not `<cwd>/b`) —
    so the caller must fold these in sequence through `_resolve_dir`, not just take the last
    raw string (that silently mis-resolved a relative chain, though an absolute last `-C`
    happens to land right either way)."""
    if not segment or segment[0] != "git":
        return []
    found: list[str] = []
    i = 1
    while i < len(segment):
        tok = segment[i]
        if tok == "-C" and i + 1 < len(segment):
            found.append(segment[i + 1])
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            found.append(tok[2:])
            i += 1
            continue
        if tok in _GIT_GLOBAL_VALUE_FLAGS and i + 1 < len(segment):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return found


def _cd_target(args: list[str]) -> str | None:
    """Extract the directory word from a `cd`'s argument list (everything after `cd`).

    Skips leading OPTION flags (`-L`, `-P`, `-e`, `-@`) so `cd -P /repo` resolves to `/repo`,
    not a bogus join with the literal flag; `--` ends option parsing so a dir that happens to
    start with `-` after it is still taken literally. `cd -` alone (return to `$OLDPWD`)
    can't be resolved statically — this hook has no memory of a previous directory across
    tool calls — so it returns None (leave `cur` unchanged) rather than guessing wrong."""
    seen_double_dash = False
    for tok in args:
        if not seen_double_dash and tok == "--":
            seen_double_dash = True
            continue
        if not seen_double_dash and tok == "-":
            return None  # `cd -` (OLDPWD) — unresolvable without shell history
        if not seen_double_dash and tok.startswith("-"):
            continue  # an option flag, not the target
        return tok
    return None


_UNEXPANDED_SHELL_META = re.compile(r"[$`*?\[{]")


def _resolve_dir(word: str, cur: str) -> str | None:
    """Resolve one `cd`/`-C` argument against the running `cur` dir, mimicking the shell, or
    None if `word` can't be safely resolved (caller should then leave `cur` unchanged).

    The shell expands a leading `~` or `~user` to a home directory BEFORE the program (cd,
    git) ever sees the word — `cd ~/repo` never receives a literal tilde. Skipping that step
    here left `~` un-expanded and glued onto `cur` as a plain relative path segment
    (`/session/cwd/~/repo`, a directory that doesn't exist), which made the subsequent
    `git -C <bogus path> diff --cached` fail and the whole gate fail OPEN — silently skipping
    the visual-proof check instead of resolving the real target repo. Expand first, THEN
    apply the absolute-vs-relative check so an absolute `~`-expansion short-circuits the join.

    `shlex.split` does NOT perform command substitution (`$(...)`, backticks), variable
    expansion (`$VAR`), or globbing (`*`/`?`/`[.]`/`{...}`) — those are real shell features
    this hook has no shell to run, so a word like `cd "$(git rev-parse --show-toplevel)"` or
    `cd $HOME/proj` arrives here STILL LITERAL. Treating it as a literal path fabricates a
    directory that (almost always) doesn't exist, which — same failure shape as the tilde
    bug above — makes `git -C <bogus> diff --cached` fail and the WHOLE gate fail open. When
    any such metacharacter survives past the `~`-expansion above, this returns None so the
    caller falls back to the current `cur` (the conservative pre-existing behavior) instead
    of fabricating a broken path that defeats the gate entirely."""
    if word.startswith("~"):
        word = os.path.expanduser(word)
    if _UNEXPANDED_SHELL_META.search(word):
        return None
    return word if word.startswith("/") else os.path.normpath(os.path.join(cur, word))


def effective_cwd(command: str, session_cwd: str) -> str:
    """Resolve the ACTUAL directory a `git commit` in `command` runs against.

    A command like `cd /other-repo && git commit ...` or `git -C /other-repo commit ...`
    targets a DIFFERENT repo than the session's own working directory (`event.cwd`) — trusting
    the raw session cwd here would check the wrong repo's staged files entirely (a session
    rooted in repo A committing into repo B via `cd`/`-C` was silently graded against A's
    staged files, producing false positives/negatives unrelated to the actual commit).

    Walks command segments (split on shell separators) left to right, applying each leading
    `cd <dir>` (a real, persistent shell cwd change) UP TO the commit segment, then applies
    that segment's OWN `-C <dir>` if present (git's own precedence: `-C` wins over ambient cwd
    for that one invocation) and stops there. Two things a naive whole-command walk gets
    wrong, both fixed by stopping at the commit segment:
      - a `cd` AFTER the commit (`git commit -m x && cd /elsewhere`) runs only once the commit
        has already happened — it must NOT retroactively change which repo got checked;
      - a `-C` on some OTHER, non-commit git invocation (`git -C /other status && git commit`)
        only scopes that single git call, not the shell's cwd — it must not leak forward onto
        the commit segment's own resolution.
    Best-effort: any parse failure or unrecognized segment shape falls back to `session_cwd`
    unchanged (fail open, matches the rest of this hook's error-handling philosophy). NOTE:
    this can only see a `cd`/`-C` that appears IN this command string — a `cd` issued as a
    separate, earlier shell invocation (persisted shell state across two distinct tool calls)
    is invisible here; that case relies entirely on the caller's `session_cwd` already being
    correct.

    TWO OR MORE commit segments in one command (`git -C /decoy commit -m x && git commit -m
    y`) are AMBIGUOUS: resolving to just the first one's directory would let a harmless-
    looking decoy commit (or a legitimate "commit in repo A, then commit in repo B" chain)
    hide a REAL, unproven commit that lands wherever this call returns nothing for. Rather
    than guess which commit `main()` should actually be gating, this falls back to
    `session_cwd` unchanged whenever there isn't EXACTLY ONE commit segment — the same safe
    default this whole function is a refinement of, restored for the case it can't safely
    resolve on its own. KNOWN GAP (agent-tools#175): this closes the decoy-first bypass but
    does not evaluate every commit segment's OWN target — two real commits into two
    different non-session_cwd repos in one chain still only get session_cwd checked.

    A `cd` is only TRUSTED (applied to `cur`) when it is the first segment or is preceded by
    `&&`/`;` (see `_CD_TRUSTED_SEP`) — the separators where a preceding `cd`'s cwd change
    stays in the SAME shell process as what follows. A `cd` preceded by `||` is only reached
    when an EARLIER command FAILED, so if the commit after it actually runs, that `cd` did
    NOT succeed — trusting its target would be backwards. A `cd` preceded by `|`/`&` runs (if
    at all) in a SUBSHELL whose cwd change never reaches the parent shell.

    The SAME subshell problem applies looking FORWARD, not just backward: `cd /x | git
    commit` and `cd /x & git commit` put the `cd` itself into a subshell/background job (a
    pipeline's left side, or anything before `&`), so even a LEADING `cd` (trusted by the
    preceding-separator check above) never changes the cwd the following `git commit`
    actually runs in — the commit still lands in `session_cwd`. So a `cd` immediately
    FOLLOWED BY `|`/`&` is untrusted too, regardless of what precedes it.

    Trusting an untrusted `cd` anyway would resolve to a real, existing, but IRRELEVANT other
    repo and check it instead of `session_cwd` (where the actual commit lands). So an
    untrusted `cd` (by either check) aborts the whole resolution to `session_cwd`, the same
    as an unresolvable target.

    KNOWN GAP: `&&` is trusted even though, in principle, an adversarial predecessor could be
    CRAFTED to fail on purpose and reach a REAL, existing, but irrelevant repo via a `;` that
    then decouples the eventual commit's execution from whether that `cd` actually ran
    (`false && cd /real-but-irrelevant-repo ; git commit` — a real shell never runs that
    `cd`, so the commit lands in `session_cwd`, but this function would still resolve to the
    other repo). Closing this precisely requires validating the ENTIRE path from a `cd` to
    the commit segment is unbroken `&&` (agent-tools#173), not just the single separator
    immediately around each `cd`. Accepted as a documented, narrow residual gap — this hook's
    own doctrine is `on_error: open` / process discipline, not a security boundary, and the
    common, benign `setup && cd target && git commit` pattern (which trusting `&&` is what
    makes work at all) is far higher value than defending this specific adversarial shape.

    KNOWN GAP (agent-tools#173): tokenization here is plain `shlex.split`, which glues a
    separator with no surrounding whitespace to the adjacent word (`cd /repo;git commit` ->
    one token `/repo;git`, never segmented) — `require_review.py` already solved this with a
    `punctuation_chars`-based tokenizer that this function should eventually adopt too.

    FIXED (agent-tools#201, PR #176 review finding): the parenthesized-subshell idiom
    `(cd <worktree> && git commit ...)` used to defeat `cd`-detection entirely (`shlex.split`
    glues the `(` onto `cd`, so `seg[0] == "cd"` never matched) — see `_strip_leading_grouping`.
    A second finding on that same fix (also #201): a subshell that CLOSES before the commit
    (`(cd repoB && true) ; git commit -m x`) must NOT be trusted, including when a wholly
    unrelated SIBLING subshell later reopens to the same nesting depth before the commit —
    see `_subshell_cd_still_trusted_at_commit`.

    KNOWN GAP (agent-tools#201, deferred — not the confirmed exploit shape, and low-realism):
    a subshell-wrapped commit with NO `-m`/`-F` value (`(cd repoB && git commit)`) glues the
    closing `)` onto `commit` itself (`"commit)"`), which `_commit_flags`' exact `segment[i]
    != "commit"` comparison doesn't recognize — this function then sees zero commit segments
    and falls back to session_cwd. Not chased here: a bare `git commit` with no message opens
    an interactive editor, which isn't a realistic agent-driven shell invocation (same
    tokenization-gluing family as #173; a `punctuation_chars` tokenizer would close this too)."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return session_cwd

    segments_with_sep = _segments_with_preceding_sep(tokens)
    if sum(1 for _sep, seg in segments_with_sep if seg and _commit_flags(seg) is not None) != 1:
        return session_cwd

    cur = session_cwd
    cur_from_open_subshell = False  # did the LATEST `cur` update come from a subshell-`cd`?
    open_subshell_cd_index = -1  # segment index of that subshell-`cd`, for the veto check below
    for i, (sep, seg) in enumerate(segments_with_sep):
        if not seg:
            continue
        # Strip a leading `(`/`{` (subshell / group command) before the `cd` check ONLY — a
        # `(cd repoB && ...)` / `( cd repoB && ... )` must be recognized as `cd`, same as the
        # bare form (agent-tools#201). Deliberately NOT applied to the commit-segment check
        # below: a subshell-wrapped commit with no `cd` (`(git commit -m x)`) is a DIFFERENT,
        # separately-tracked gap in the earlier `GIT_COMMIT` detection regex in `main()` (that
        # regex already fails to recognize the command as a commit at all, so this function is
        # never even reached for it) — folding a fix in here would be a no-op for that shape and
        # would blur the two independent bugs' fixes together.
        cd_seg = _strip_leading_grouping(seg)
        if cd_seg and cd_seg[0] == "cd":
            # `(` (subshell, forks a child process) needs the closed-subshell veto below;
            # `{` (group command, no fork — a `cd` inside genuinely persists past its `}`)
            # does NOT, so this must check specifically for `(`, not just "was anything
            # stripped" — `seg[0]` here is still the ORIGINAL (pre-strip) first token.
            cur_from_open_subshell = cd_seg != seg and seg[0].startswith("(")
            open_subshell_cd_index = i
            seg = cd_seg
            if sep is not None and sep not in _CD_TRUSTED_SEP:
                return session_cwd  # conditionally/subshell-reached `cd` — can't trust it
            following_sep = (
                segments_with_sep[i + 1][0] if i + 1 < len(segments_with_sep) else None
            )
            if following_sep in _CD_SUBSHELL_SEP:
                return session_cwd  # `cd` itself ran (if at all) in a subshell/background
                                     # job — its cwd change never reached the shell that
                                     # runs whatever comes after
            target = _cd_target(seg[1:])
            resolved = _resolve_dir(target, cur) if target is not None else None
            if resolved is None:
                # Regression guard: an unresolvable `cd` (bare `cd -`, or a target this hook
                # can't expand) means we've LOST TRACK of the real shell's cwd from here on
                # — even if an EARLIER `cd` in this same chain resolved cleanly. Leaving
                # `cur` at that earlier, now-stale value (`cd /clean-repo && cd - && git
                # commit`, where the real shell ends up back in session_cwd via $OLDPWD, not
                # /clean-repo) would check a directory the commit never actually targets.
                # Abort to session_cwd — the one directory we can still trust — rather than
                # keep walking on a cwd we're no longer sure of.
                return session_cwd
            cur = resolved
            continue
        if _commit_flags(seg) is not None:
            if cur_from_open_subshell and not _subshell_cd_still_trusted_at_commit(
                segments_with_sep, open_subshell_cd_index, i,
            ):
                # The subshell that produced `cur` via a trusted `cd` already CLOSED before
                # this commit segment — a real shell's subshell is a forked child process,
                # so that `cd` never persisted this far. Reset to session_cwd BEFORE applying
                # this invocation's own `-C` (below) rather than discarding it outright —
                # `git -C /target commit` after an unrelated closed decoy subshell must still
                # resolve to /target, not get vetoed to session_cwd wholesale (review finding).
                cur = session_cwd
            # Repeated `-C` on the SAME invocation chains: each is resolved relative to the
            # PREVIOUS one (see _git_c_values) — fold them in order, same as the `cd` case.
            # Same staleness guard applies: an unresolvable `-C` value means the invocation's
            # real target can't be trusted either, so abort to session_cwd rather than keep
            # the previous `-C`'s (possibly stale) directory.
            for c_value in _git_c_values(seg):
                resolved = _resolve_dir(c_value, cur)
                if resolved is None:
                    return session_cwd
                cur = resolved
            return cur  # this IS the (one and only) commit — anything after it is irrelevant
    return cur


def is_skip_commit(command: str) -> bool:
    """True only when EVERY `git commit` segment in `command` carries --continue/--abort/--skip.

    Parses the argv after stripping shell comments, scopes to each `git commit` segment, and
    removes `-m`/`-F` message VALUES — so a skip token that lives only in a comment
    (`git commit -m x # --abort`), in the commit message (`git commit -m 'support --skip'`), or on
    a SIBLING command (`git rebase --abort && git commit -m x`) does NOT exempt an authoring
    commit. On a tokenization failure this returns False → the commit is GATED (the safe way).

    Regression this guards against (agent-tools#174): a command chaining a rebase-plumbing
    commit with a REAL one (`git commit --continue && git commit -m x`) used to exempt the
    WHOLE command, because this only inspected the FIRST commit segment found (the plumbing
    one) and returned on it — the second, authoring commit never got checked at all. Requiring
    EVERY commit segment to be a skip closes that: one real commit anywhere in the chain means
    the command is NOT skip-exempt and must be gated normally. NOTE: agent-tools#174's own issue
    body claimed this file was already fixed by agent-tools#172 — that was a misattribution;
    #172 was actually an unrelated `effective_cwd()` tilde-expansion fix, and this file kept the
    original first-segment-only bug live until now. Mirrors the identical, already
    review-approved fix in skills_read_gate.py's `is_skip_commit` — keeps both hooks' skip-flag
    handling in step, per the SYNC comment above."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False
    commit_segments_flags = [
        flags for seg in _segments(tokens) if (flags := _commit_flags(seg)) is not None
    ]
    if not commit_segments_flags:
        return False
    return all(any(tok in SKIP_FLAGS for tok in flags) for flags in commit_segments_flags)


def staged_files(cwd: str) -> list[str] | None:
    """Names of files staged for commit, or None if git could not be queried (→ fail open)."""
    try:
        proc = subprocess.run(  # noqa: S603,S607 — fixed git argv, trusted
            ["git", "-C", cwd, "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=GIT_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"could not list staged files: {exc} — allowing (fail-open)")
        return None
    if proc.returncode != 0:
        warn(f"git diff --cached exited {proc.returncode}: {proc.stderr.strip()} — allowing")
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _resolve_staged_files(cwd: str, session_cwd: str) -> list[str] | None:
    """`staged_files(cwd)`, but falling back to `session_cwd` specifically when `cwd` is a
    `cd`/`-C` target that DOESN'T EXIST ON DISK — the one case where we can be sure a real
    shell's `cd` would not have landed there either (whatever the surrounding operator,
    `cd <bad-dir>` itself fails the same way whether followed by `;`, `&&`, or `||`).
    `effective_cwd` has no shell to run `cd` in and can't know it would have failed, so it
    resolves to the (nonexistent) target anyway; failing open right there would let a real,
    unproven commit in `session_cwd` slip through silently. Falling back to `session_cwd`
    re-checks the one directory we KNOW is real before giving up.

    This does NOT model `&&`/`;`/`||` operator semantics for a cd whose TARGET DOES exist
    (agent-tools#173: e.g. `falsecmd && cd /real-but-irrelevant-repo ; git commit`, where a
    real shell never runs that `cd` at all because `falsecmd` failed) — an existing-but-
    wrong other repo is indistinguishable, from here, from a legitimately reached one, so
    this fallback can't help there. A directory that merely EXISTS but errors on `staged_files`
    for another reason (not a git repo, a transient git error, the subprocess timeout) is
    treated as a genuine 'can't query this repo' situation and stays pure fail-open (allow)
    per the hook's documented policy, rather than substituting an unrelated repo's staged
    files (which could produce an unrelated false BLOCK, or mask the real target's error
    behind an unrelated false ALLOW). A LEGITIMATE `cd <valid-other-repo> && git commit` with
    nothing staged there returns `[]` (not `None`) from `staged_files`, so this fallback
    never fires for it either way."""
    files = staged_files(cwd)
    if files is None and cwd != session_cwd and not os.path.isdir(cwd):
        files = staged_files(session_cwd)
    return files


def visual_staged(files: list[str]) -> list[str]:
    return [f for f in files if VISUAL_EXT.search(f) or VISUAL_DIR.search(f)]


# Every path this hook reads MARKER CONTENT from — a file in the shared PROOF_DIR, or a
# `capture_path` referenced from inside a JSON attestation — is untrusted, attacker/agent-
# controlled input (review finding, round 3). A FIFO (or a symlink to one) placed at such a
# path makes a naive `read_bytes()`/`read_text()` BLOCK FOREVER waiting for a writer that never
# comes; this hook's own descriptor is `on_error: "open"`, so a hook that hangs past its
# timeout is treated as a CRASHED hook, which ALLOWS the commit — the exact bypass this whole
# fix exists to close, reached by making the CHECK itself hang instead of defeating its logic.
# A character device (`/dev/zero`) risks unbounded memory instead. `_MAX_MARKER_BYTES` bounds a
# plain-text fallback marker or a JSON attestation (both are small structured text).
#
# `_MAX_CAPTURE_BYTES` must not reject a capture dev-cli's own producer would legitimately
# permit (review finding, round 5): dev-cli's `visual_proof.MAX_PIXELS` is 40,000,000 and
# `MAX_DIMENSION` is 60,000 — a full-page capture at that ceiling is up to 160MB as raw RGBA
# (40M px * 4 bytes), and PNG's DEFLATE never expands data by more than a small constant
# factor even on a near-incompressible (photo/noise-heavy) page, so 200MB comfortably covers
# even a pathological worst-case capture dev-cli would still accept — not just the common case
# (an ordinary UI screenshot, mostly flat colour/text, compresses far smaller than this). Still
# a firm, bounded ceiling for the DoS concern this whole helper exists for.
_MAX_MARKER_BYTES = 1_000_000
_MAX_CAPTURE_BYTES = 200_000_000

# A per-file size cap alone doesn't bound TOTAL work when there are many files (review finding,
# round 6): an agent can drop thousands of fresh, valid-SHAPED `.json` markers in PROOF_DIR,
# each pointing `capture_path` at a large (up to `_MAX_CAPTURE_BYTES`) regular file with a
# deliberately wrong hash — every one still costs a real read-and-hash before it's rejected.
# Enough of them can push total scan time past this hook's own timeout, and `on_error: "open"`
# turns a timed-out hook into an allowed commit — the same class of bypass the FIFO/size guards
# above exist to close, reached by volume instead of a single hostile file. Capping the number
# of FRESH candidates actually inspected bounds worst-case work to `_MAX_MARKERS_SCANNED`
# reads/hashes AT MOST — a fixed count, independent of how many junk files exist in the
# directory — NOT a fixed byte total: each of those reads can still be up to
# `_MAX_CAPTURE_BYTES`, so the true worst case is `_MAX_MARKERS_SCANNED *
# _MAX_CAPTURE_BYTES` of I/O, not "one file's cost". That is still a finite, known ceiling
# (unlike the pre-fix unbounded case), just not as tight as "a small multiple" might suggest;
# lowering either constant trades against the legitimate multi-marker / large-capture cases
# this fix is not trying to break. Exceeding the cap does NOT fail open: `_proof_fresh` simply
# stops looking and returns False (BLOCK/hatch), the same as if none of the remaining
# candidates had validated — plenty of markers is not itself proof, and this hook has no
# reason to trust a directory this cluttered.
#
# This cap applies ONLY to candidates that already passed the (cheap) freshness check — i.e.
# it bounds the EXPENSIVE work (JSON parse + capture-file read+hash, or a manual-marker byte
# read), not raw directory traversal. See `_MAX_ENTRIES_SCANNED` below for the separate cap on
# total entries iterated, and `_proof_fresh`'s docstring for why the two are deliberately not
# the same budget (review finding, round 8).
_MAX_MARKERS_SCANNED = 200

# Bounds the CHEAP part of the scan — directory iteration + one `stat()` per entry for the
# freshness check — separately from `_MAX_MARKERS_SCANNED` above, which bounds only the
# EXPENSIVE part (review finding, round 8): a shared, single budget applied to every entry
# regardless of freshness (the round-7 fix) means a long-lived, never-pruned PROOF_DIR with
# more than `_MAX_MARKERS_SCANNED` accumulated STALE entries can place a genuinely fresh,
# valid marker AFTER that many entries in `iterdir()`'s arbitrary order — the scan gives up
# before ever reaching it, and a real screenshot that was actually captured and reviewed
# still gets BLOCKED. A bare `stat()` is orders of magnitude cheaper than the expensive
# per-candidate work `_MAX_MARKERS_SCANNED` exists to bound (no file content is read), so this
# ceiling can be — and needs to be — much higher: high enough that an ordinary accumulation of
# expired markers on a shared, long-lived machine doesn't starve a legitimate fresh one, while
# still bounding worst-case total iteration for a truly pathological (multi-million-entry)
# flood. Exceeding EITHER cap does not fail open — both stop looking and return False, same as
# a directory with no valid markers at all.
_MAX_ENTRIES_SCANNED = 5_000

# dev-cli's `visual_proof.ATTEST_VERSION` (review finding, round 4). dev-cli's own docstring
# for that constant says v1 measured blankness with plain exact-pixel-equality (scored a
# gradient-disguised blank page 0% featureless) and recorded the INVOCATION directory as
# `repo` rather than the resolved toplevel — both silently different from what this gate
# assumes a record means. An older/foreign producer's record must not be treated as if it
# carried today's guarantees just because its `repo`/`staged_sha256` happen to still line up
# (invoking from the repo root makes v1's invocation-dir `repo` value identical to the
# toplevel, so that alone would NOT have caught it). Exact-match, not `>=`: a hypothetical
# future version's semantics are unknown until dev-cli's producer and this constant are
# updated together — same "keep both sides in step" discipline as this repo's other
# cross-file/cross-repo SYNC contracts.
_EXPECTED_ATTESTATION_VERSION = 2
_EXPECTED_ATTESTATION_TOOL = "dev shot"


def _safe_regular_file_bytes(path: Path, max_bytes: int) -> bytes | None:
    """Read `path` ONLY if it IS (at the moment of the read, not a moment earlier) a REGULAR
    file no larger than `max_bytes`; else None without ever blocking on a read.

    Deliberately NOT `stat()`-then-`read_bytes()` (review finding, round 4 — TOCTOU): checking
    the type first and opening separately leaves a gap where a concurrent process can swap a
    checked regular file for a FIFO between the two calls, reintroducing the exact hang this
    function exists to prevent. Instead: open the SAME fd with `O_NONBLOCK` (so opening a FIFO
    with no writer returns immediately instead of blocking — the effect this flag exists for;
    it is a no-op for a regular file) and without following a symlink (`O_NOFOLLOW`, since a
    regular file reached only by resolving a symlink is a target this function was never asked
    to trust), THEN `fstat` that exact fd. A file swapped in after the open() call is opening a
    DIFFERENT inode — this fd's `fstat` still describes what was actually opened, so the type
    check is authoritative for the bytes actually read next, not for some earlier or later state
    of the path.

    Also NOT a bare `f.read()` after the size check passes (review finding, round 5 — a second
    TOCTOU on the SAME fd): `fstat`'s reported size is a snapshot, but a concurrent writer can
    keep appending to that exact file WHILE `read()` is still consuming it, so an unbounded read
    can grow past `max_bytes` anyway if the append race outruns us to EOF. Reading exactly
    `max_bytes + 1` bytes bounds the read regardless of what happens after the size check —
    getting back more than `max_bytes` (impossible for a genuinely `<= max_bytes` file, since
    there's nothing left to read past its true end) is itself treated as a rejection, not a
    truncation-and-accept."""
    fd = None
    nofollow = getattr(os, "O_NOFOLLOW", 0)  # not defined on non-POSIX platforms; 0 is a no-op
    nonblock = getattr(os, "O_NONBLOCK", 0)  # not defined on Windows; 0 is a no-op (no FIFO guard)
    try:
        fd = os.open(str(path), os.O_RDONLY | nonblock | nofollow)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb") as f:
            fd = None  # ownership transferred to the file object; don't double-close below
            data = f.read(max_bytes + 1)
            return None if len(data) > max_bytes else data
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _repo_toplevel(cwd: str) -> str | None:
    """The real (symlink-resolved) git repo toplevel containing `cwd`, or None if git can't
    answer. Used to scope the proof marker to a REPO, not a bare directory — a screenshot
    looked at from the repo root still scopes the same as a commit made from a subdirectory
    (monorepo packages, `vscode-extension/`, …), because both resolve to the same toplevel;
    matches dev-cli's own `_git_toplevel` binding in `write_attestation` (agent-tools#475).

    Captures RAW BYTES and decodes with `os.fsdecode` — NOT `text=True` (review finding,
    round 8): on a POSIX filesystem a repo path is an arbitrary byte string, not guaranteed
    valid UTF-8. `text=True` decodes with the strict error handler by default, so a non-UTF-8
    toplevel path raises an uncaught `UnicodeDecodeError` that escapes this function (and
    `main()`), crashing the hook — `on_error: "open"` then turns that crash into an ALLOWED
    commit, the exact bypass class this whole file exists to close, just reached through a
    decode error instead of a wrong field value or a hanging read. `os.fsdecode` uses the
    same `surrogateescape` handling the OS itself uses for non-decodable path bytes and never
    raises, so an exotic path degrades to a merely-unmatchable `repo_root` (fails the later
    equality check, same as any other legitimate mismatch) rather than crashing the hook."""
    try:
        proc = subprocess.run(  # noqa: S603,S607 — fixed git argv, trusted
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=GIT_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = os.fsdecode(proc.stdout.strip())
    return os.path.realpath(top) if top else None


def _resolved_repo_root(cwd: str, session_cwd: str) -> str | None:
    """`_repo_toplevel(cwd)`, falling back to `session_cwd` in the SAME shape as
    `_resolve_staged_files` — try the resolved commit target first, then the session's own
    cwd when the target doesn't exist on disk. Keeps the proof-marker scope aligned with
    whichever directory `staged_files` actually succeeded against."""
    top = _repo_toplevel(cwd)
    if top is not None:
        return top
    if cwd != session_cwd and not os.path.isdir(cwd):
        return _repo_toplevel(session_cwd)
    return None


def _staged_diff_hash(repo_root: str) -> str | None:
    """sha256 of the FULL `git diff --cached` (not `--name-only`) for `repo_root`, or None if
    git can't be queried or nothing is staged. Deliberately reproduces dev-cli's own
    `staged_diff_hash` computation exactly (same argv, same raw-bytes hash) — this is the
    value a `dev shot` attestation's `staged_sha256` field must match to be accepted. An empty
    staged diff hashes to a real, stable value that would trivially match a repo's frequent
    "nothing staged" state, so that case is treated as unbound (None), same as dev-cli does."""
    try:
        proc = subprocess.run(  # noqa: S603,S607 — fixed git argv, trusted
            ["git", "-C", repo_root, "diff", "--cached"],
            capture_output=True, timeout=GIT_DIFF_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def _attestation_satisfies(record: object, repo_root: str, staged_hash: str) -> bool:
    """True if a parsed `dev shot` attestation JSON (`write_attestation`'s record shape) is
    bound to EXACTLY this repo, EXACTLY this staged diff, AND an actual capture file that still
    exists with the exact bytes it was recorded for.

    `repo` + `staged_sha256` alone are NOT enough (review finding, round 1): both are plain
    `git` queries any agent can run directly, so a record built from just those two fields is a
    forgery an agent can hand-write with zero screenshot involved — no stronger than the manual
    fallback marker below, despite claiming to be. Recomputing `capture_sha256` from the file at
    `capture_path` right now closes that: passing requires a real file on disk whose bytes hash
    to the recorded digest, not just knowledge of two `git` outputs. `record["repo"]` is
    realpath-normalized the same way `_repo_toplevel` normalizes ours, so the comparison isn't
    defeated by a trailing slash or an unresolved symlink component. (This still cannot judge
    whether the capture was BLANK/degenerate — that verdict lives in dev-cli's own
    `verdict_for`/`measure_png`, which only runs at capture time and is not re-run here; a
    forger who fabricates a plausible-looking non-blank PNG and hand-computes every field still
    gets through. That is a materially higher bar than "type two git commands", and matches
    this whole marker family's stated honesty-contract, not cryptographic-proof, doctrine.)

    `worktree_dirty` must be exactly `False` (review finding, round 2): dev-cli's `write_attestation`
    records it because the BROWSER renders the WORKING TREE, but the binding is to the INDEX
    (`git diff --cached`) — if the worktree had already diverged from the index at capture time
    (an unstaged edit made after staging, or an untracked asset), the screenshot may show content
    that was never staged at all, even though `repo` and `staged_sha256` both still check out
    (they describe the index at capture time, not what the browser actually rendered). Missing,
    `None`, or `True` are all rejected — only a positively-confirmed clean worktree passes.

    Every field is UNTRUSTED, attacker/agent-controlled JSON — the whole point of this function
    is judging a record an adversarial caller may have hand-crafted to defeat the gate (review
    finding, round 3). Two distinct hazards follow from that, both closed here rather than left
    to propagate:
      - A `capture_path` containing an embedded NUL (`"\\u0000"`) makes `os.path.realpath` /
        `Path(...)` raise `ValueError`, not `OSError`. Left uncaught, that propagates out of
        this function, `_proof_fresh`, and `main()` entirely — and this hook's descriptor is
        `on_error: "open"`, so a CRASHED gate is an ALLOWED commit, the exact bypass this whole
        fix exists to close, just reached by a different route than a wrong field value. Every
        extraction/read here catches `ValueError` alongside the expected `KeyError`/`TypeError`.
      - A `capture_path` pointing at a FIFO (or a symlink to one) makes a naive read BLOCK
        FOREVER; past this hook's own timeout that is ALSO an allowed commit (a hung hook is a
        crashed hook, same `on_error: "open"` consequence). `_safe_regular_file_bytes` rejects
        anything that isn't a plain, size-bounded regular file before ever attempting to read
        it — see its own docstring.

    Requires `tool == "dev shot"` and `version == _EXPECTED_ATTESTATION_VERSION` exactly
    (review finding, round 4): an older/foreign producer's record measured blankness and
    `worktree_dirty` differently (see the constant's own comment) — accepting it just because
    its `repo`/`staged_sha256` happen to line up would silently trust guarantees it never
    actually made."""
    if not isinstance(record, dict):
        return False
    if (
        record.get("tool") != _EXPECTED_ATTESTATION_TOOL
        or record.get("version") != _EXPECTED_ATTESTATION_VERSION
    ):
        return False
    try:
        rec_repo = os.path.realpath(str(record["repo"]))
        rec_hash = str(record["staged_sha256"])
        capture_path = str(record["capture_path"])
        rec_capture_hash = str(record["capture_sha256"])
    except (KeyError, TypeError, ValueError):
        return False
    if rec_repo != repo_root or rec_hash != staged_hash:
        return False
    if record.get("worktree_dirty") is not False:
        return False
    try:
        capture_bytes = _safe_regular_file_bytes(Path(capture_path), _MAX_CAPTURE_BYTES)
    except ValueError:
        return False  # e.g. an embedded NUL byte — Path() itself can raise before .stat()
    if capture_bytes is None:
        return False  # missing, a FIFO/device/symlink target, or oversized — see the helper
    return hashlib.sha256(capture_bytes).hexdigest() == rec_capture_hash


def _manual_marker_satisfies(path: Path, repo_root: str) -> bool:
    """Fallback path for when no `dev shot` capture exists (no URL to shoot — docs-only visual
    change, a generated image, a schematic). The marker's FIRST LINE must equal `repo_root`
    exactly — this is what kills the junk-file case a bare `touch` used to pass: an empty file,
    or any unrelated file that happens to land in this shared directory, no longer satisfies
    the gate. It does NOT bind to the staged diff (there is no attestation to draw that hash
    from), so it is strictly weaker than `_attestation_satisfies` — `--write-marker` is meant
    as the exception, `dev shot` as the norm whenever a URL exists.

    Reads via `_safe_regular_file_bytes` (not a bare `read_text`), for the same reason
    `_attestation_satisfies` does: `path` is a filesystem entry from a shared, world-writable-
    by-any-local-agent directory, so it could be a FIFO/device/oversized file, not just a plain
    marker."""
    data = _safe_regular_file_bytes(path, _MAX_MARKER_BYTES)
    if data is None:
        return False
    lines = data.decode("utf-8", errors="replace").splitlines()
    return bool(lines) and lines[0].strip() == repo_root


def _proof_fresh(repo_root: str | None) -> bool:
    """True if a FRESH, correctly-bound proof marker exists for `repo_root` (agent-tools#475).

    `repo_root` is None only when `_resolved_repo_root` itself failed after `staged_files`
    already succeeded against this same directory — a narrow, self-inflicted git-environment
    failure, not the common path (see its docstring). Unlike the rest of this hook's git
    queries, that is NOT treated as fail-open here: this function's whole job is proving a
    marker is bound to a specific repo, and a None repo means there is nothing to bind against
    — silently allowing would hand back exactly the "any file, anywhere, satisfies every repo"
    hole this exists to close. It falls through to the normal BLOCK/hatch path instead, which
    still isn't a wedge — the Telegram hatch is always available.

    Every fresh (mtime-windowed) file in PROOF_DIR is inspected — `.json` files as a `dev shot`
    attestation (see `_attestation_satisfies`), everything else as a manual marker (see
    `_manual_marker_satisfies`) — and this returns True on the first one that validates against
    `repo_root`. The staged-diff hash is computed at most once per call (lazily, only if a
    `.json` marker is actually present), since it costs a `git diff --cached` subprocess.

    Freshness requires `0 <= age <= PROOF_WINDOW_S`, not just `age <= PROOF_WINDOW_S` (review
    finding, round 4): a marker whose mtime is set in the FUTURE has a NEGATIVE age, which the
    one-sided check would treat as fresh forever — right up until that future date actually
    arrives. A future mtime is never legitimate (nothing in this hook's own writers, nor
    dev-cli's, ever backdates or postdates a marker), so it is rejected the same as a stale
    one rather than silently trusted.

    Inspects at most `_MAX_ENTRIES_SCANNED` directory entries total (the cheap stat-only pass,
    review finding round 7) and, within that, expensively validates at most
    `_MAX_MARKERS_SCANNED` of the ones that pass the freshness check (round 6). These are TWO
    SEPARATE budgets, not one shared counter (round 8 finding on the round-7 fix): applying a
    single small cap to every iterated entry regardless of freshness closed round 7's
    stale-flood DoS, but on a long-lived, never-pruned PROOF_DIR it also meant a genuinely
    fresh, valid marker sitting after `_MAX_MARKERS_SCANNED` STALE entries in `iterdir()`'s
    arbitrary order was never reached — the scan gave up before finding a real screenshot that
    really was captured and reviewed. Splitting the budgets fixes that: the outer, much larger
    `_MAX_ENTRIES_SCANNED` still bounds total `stat()` calls against a flood of any kind
    (fresh, stale, or broken-symlink entries all count against it), while the inner, smaller
    `_MAX_MARKERS_SCANNED` bounds only the entries that actually reach the expensive
    read-and-hash/parse step — restoring round 6's original intent (bound expensive work) as
    its own independent limit rather than conflating it with round 7's traversal limit.
    Exceeding EITHER cap does not fail open — both just stop looking and return False, same as
    if nothing further had validated."""
    if repo_root is None:
        return False
    try:
        if not PROOF_DIR.is_dir():
            return False
        now = time.time()
        entries_scanned = 0
        markers_validated = 0
        staged_hash: str | None = None
        staged_hash_tried = False
        for child in PROOF_DIR.iterdir():
            entries_scanned += 1
            if entries_scanned > _MAX_ENTRIES_SCANNED:
                return False  # too many total entries to keep iterating — stop, don't fail open
            try:
                age = now - child.stat().st_mtime
                if age < 0 or age > PROOF_WINDOW_S:
                    continue
            except OSError:
                continue
            markers_validated += 1
            if markers_validated > _MAX_MARKERS_SCANNED:
                return False  # too many FRESH candidates to keep expensively validating — stop
            if child.suffix == ".json":
                if not staged_hash_tried:
                    staged_hash = _staged_diff_hash(repo_root)
                    staged_hash_tried = True
                if staged_hash is None:
                    continue  # nothing staged / git unavailable → no diff to bind against
                # _safe_regular_file_bytes (not a bare read_text): `child` is a filesystem
                # entry from a shared, world-writable-by-any-local-agent directory, so a
                # `.json`-named FIFO/device/oversized file is exactly as possible as a real
                # attestation — see its docstring for why a naive read is a hang-then-fail-open
                # hazard, not just a parse-error one.
                data = _safe_regular_file_bytes(child, _MAX_MARKER_BYTES)
                if data is None:
                    continue
                try:
                    record = json.loads(data)
                except ValueError:
                    continue
                if _attestation_satisfies(record, repo_root, staged_hash):
                    return True
            elif _manual_marker_satisfies(child, repo_root):
                return True
    except OSError:
        return False
    return False


def _block_message(visual: list[str]) -> str:
    """The BLOCK message: what's wrong, how to satisfy the marker, and the hatch how-to."""
    sample = ", ".join(visual[:3]) + (", …" if len(visual) > 3 else "")
    return (
        f"This commit changes user-visible files ({sample}) but no screenshot was captured "
        "and looked at. Per visual-proof-cycle: capture the rendered result, read the capture "
        "back, verify it, THEN commit. PRIMARY: `dev shot '<url>' --out /tmp/shot.png` (output "
        "path OUTSIDE the repo — see README) — it writes "
        "a proof record bound to THIS repo and THIS staged diff, no manual marker needed. "
        "FALLBACK (no URL to shoot): `python3 "
        f"{shlex.quote(str(Path(__file__).resolve()))} --write-marker` from inside the repo, after you've "
        f"actually reviewed a capture some other way — a bare `touch` under {PROOF_DIR} no "
        "longer satisfies this gate (agent-tools#475: it used to be content-blind and "
        "machine-global). No self-service bypass. ASK the human, or request a one-time "
        'Telegram approval via RIG_HATCH_REQUEST_VISUAL_PROOF_GATE="<justification>" '
        "(deny-by-default; bare 1 rejected)."
    )


def _decide_block(command: str, cwd: str, visual: list[str]) -> int:
    """The gate has decided this commit needs proof and none is present. Consult the Telegram
    hatch: an unset request is the normal BLOCK; a present request allows on the human's
    approval (tg-ctl exit 0) and blocks (leading with the denial reason) otherwise."""
    message = _block_message(visual)
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, {"hook": HOOK_ID, "command": command}, cwd=cwd, command=command,
    )
    if hatch.should_stop:
        if hatch.approved:
            note = f"visual-proof gate allowed via hatch escalation ({hatch.reason})"
            warn(note)
            emit("allow", note)
            return 0
        warn(f"visual-proof gate hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{message}")
        return BLOCK_EXIT_CODE
    emit("block", message)
    return BLOCK_EXIT_CODE


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    session_cwd = str(event.get("cwd") or os.getcwd())

    # Strip leading inline env first (so `RIG_HATCH_REQUEST_…="why" git commit` still trips the
    # gate), then detect the commit on the comment-stripped command and judge skip-ness from the
    # parsed argv — so a skip token in a trailing comment / commit message can't bypass the gate.
    env_free = _strip_leading_inline_env(command)
    if not GIT_COMMIT.search(_strip_shell_comment(env_free)) or is_skip_commit(env_free):
        emit("allow")  # not a normal commit → nothing to gate
        return 0

    # A `cd <other-repo> && git commit` or `git -C <other-repo> commit` targets a DIFFERENT
    # repo than the session's own cwd — check staged files in THAT repo, not session_cwd.
    cwd = effective_cwd(command, session_cwd)

    files = _resolve_staged_files(cwd, session_cwd)
    if files is None:
        emit("allow")  # git could not be queried anywhere → fail open
        return 0

    visual = visual_staged(files)
    if not visual:
        emit("allow")  # no user-visible files staged → nothing to prove
        return 0

    repo_root = _resolved_repo_root(cwd, session_cwd)
    if _proof_fresh(repo_root):
        emit("allow")  # a correctly-scoped, correctly-bound marker is fresh → satisfied
        return 0

    return _decide_block(command, cwd, visual)


def _cli_write_marker(argv: list[str]) -> int:
    """`visual_proof_gate.py --write-marker [cwd]` — the FALLBACK way to satisfy this gate when
    there is no URL to `dev shot` (docs-only visual change, a generated image, a schematic per
    visual-proof-cycle). Writes a marker whose content is the resolved repo toplevel of `cwd`
    (default: current directory) — the same binding `_manual_marker_satisfies` checks for.

    Uses `tempfile.mkstemp` (`O_CREAT|O_EXCL`, an unpredictable random suffix, auto-retry on a
    name collision) rather than a predictable `looked-<millisecond-timestamp>` name written
    with a plain, symlink-following create (review finding, round 6): PROOF_DIR is a directory
    shared by every local agent, so a millisecond-granularity filename is guessable/sprayable —
    another process could pre-create `looked-<ts>` as a symlink pointing at an arbitrary
    user-writable file, and a plain create would follow it and overwrite that target instead of
    writing a marker. `O_EXCL` refuses to open ANY existing path (including a symlink, even a
    dangling one) when combined with `O_CREAT` — POSIX-specified, not a NOFOLLOW nuance — so
    this can only ever create a brand-new, never-before-existing regular file.

    This is a CLI entry point, not the pre-bash hook contract (agents-hooks/v1 events arrive on
    stdin with no argv) — the harness never invokes the script this way, so this branch is
    inert for every normal `pre-bash` dispatch and only runs when explicitly invoked by hand."""
    cwd = argv[0] if argv else os.getcwd()
    top = _repo_toplevel(cwd)
    if top is None:
        warn(f"could not resolve a git repo toplevel for {cwd} — nothing written")
        return 1
    try:
        PROOF_DIR.mkdir(parents=True, exist_ok=True)
        fd, marker_path = tempfile.mkstemp(prefix="looked-", dir=str(PROOF_DIR))
        try:
            os.write(fd, (top + "\n").encode())
        finally:
            os.close(fd)
    except OSError as exc:
        warn(f"could not write proof marker: {exc}")
        return 1
    sys.stderr.write(f"visual-proof-gate: wrote proof marker for {top} -> {marker_path}\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--write-marker":
        sys.exit(_cli_write_marker(sys.argv[2:]))
    sys.exit(main())
