#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require mandatory skills before doing WORK.

Before a work-shaped action (a `git commit`, or a build/test command), this checks that the
mandatory/relevant skills were INVOKED this session. A skill encodes the rules the action
must follow (e.g. `delegate-work-to-subagents` before dispatching, `visual-proof-cycle`
before a UI "done"); doing the work without first reading the skill skips those rules.

How it knows a skill was invoked — the MARKER CONTRACT:
  A skill-invocation wrapper touches one file per invoked skill, nested under the CC session
  id that invoked it, in a marker dir:
      ~/.cache/agent-tools/skills-invoked/<session-id>/<skill-name>  (mtime = invocation time)
  A skill counts as invoked if its marker is FRESH (within SKILLS_FRESH_WINDOW_S, default
  7200s) and EITHER was written by THIS session, OR (lower precedence) the pre-session-
  scoping GLOBAL marker is fresh — see `_missing_skills`/`_marker_path`. The session-scoped
  check is what closes the leak: without it, one Claude Code session invoking a skill would
  silently satisfy every other concurrent session's gate too, and in practice CC's own
  writer never touches the global path anymore once a session id is available. The global
  fallback stays only for a harness with no session-aware writer of its own (Codex/opencode
  today) or a manual `touch`. The wrapper that touches it is the honest, satisfiable
  action — see the README. Configure the dir with SKILLS_INVOKED_DIR; the mandatory set with
  MANDATORY_SKILLS (comma-separated, default `delegate-work-to-subagents,visual-proof-cycle`).

TIERED (warn → block): if a mandatory skill has no fresh marker, the FIRST work action in
the window WARNs (allow + message); a REPEAT BLOCKs (tracked by a marker keyed by cwd, like
orchestrator-stays-thin). Default tier is WARN-FIRST so it never wedges while the
marker-writer is still being wired everywhere.

NOTE: a subagent doing work should still read its PROJECT skills, so this gate is NOT a blanket
subagent-exempt. BUT the two orchestration/visual defaults (delegate-work-to-subagents,
visual-proof-cycle) are STRUCTURALLY N/A for a dispatched subagent — the subagent IS the delegate
(re-delegation hangs), and a non-UI commit has no visual proof — so they are dropped from the
demanded set when agent_id is present (mirrors orchestrator-stays-thin's subagent detection).
Any project-specific MANDATORY_SKILLS entry still applies to subagents.

No self-service bypass. There is NO env var / inline sentinel an agent can set on its own
command to skip this gate — a self-grant is security theater. (Previously an
`ALLOW_SKIP_SKILLS=1` override was routinely forced onto EVERY subagent commit to dodge the
orchestration/visual defaults; that dominant false-block is now handled structurally — those
two defaults are dropped for a dispatched subagent, see above — so no blanket override is
needed.) For a genuine one-off, ASK the human, or request a ONE-TIME Telegram approval via
`RIG_HATCH_REQUEST_SKILLS_READ_GATE="<justification>"` (deny-by-default; a blank or bare `1` is
rejected — the value must be a real written justification). The request routes to the human
over Telegram (tg-ctl) and allows ONLY on their approval.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": process discipline, not a security boundary — a crash must never wedge
the ability to commit/build.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
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

INVOKED_DIR = Path(os.path.expanduser(os.environ.get(
    "SKILLS_INVOKED_DIR", "~/.cache/agent-tools/skills-invoked")))
TIER_DIR = Path(os.path.expanduser(os.environ.get(
    "SKILLS_TIER_DIR", "~/.cache/agent-tools/skills-read-tier")))
FRESH_WINDOW_S = int(os.environ.get("SKILLS_FRESH_WINDOW_S", "7200"))

DEFAULT_MANDATORY = "delegate-work-to-subagents,visual-proof-cycle"

# Orchestration/visual defaults dropped from a dispatched subagent's demanded set (agent_id
# present); any OTHER (project-specific) mandatory skill still applies. Rationale + an accepted
# tradeoff:
#   - delegate-work-to-subagents — the subagent IS the delegated work; demanding it delegate again
#     is wrong (a sub-subagent dispatch hangs). Unconditionally N/A for a subagent.
#   - visual-proof-cycle — this gate fires on the command SHAPE (commit/build/test), not the
#     commit's content, so it cannot tell a subagent's UI commit from a non-UI one. We drop the
#     default for ALL subagent commits, which means a subagent doing REAL UI work also bypasses the
#     visual-proof reminder. That is the accepted cost of fixing the dominant false-block (issue
#     #112): the prior state forced an ALLOW_SKIP_SKILLS override on EVERY subagent commit. A
#     project that wants visual proof enforced on subagents can add a project-specific UI skill to
#     MANDATORY_SKILLS (those are NOT dropped). The orchestrator (no agent_id) still gets both.
#
# OPEN QUESTION, documented rather than silently assumed: whether a dispatched subagent's CC
# events carry the SAME top-level session_id as its parent orchestrator's, or a distinct one, is
# not settled by anything in this repo (no existing fixture sets both agent_id and session_id
# together). If it is the SAME id, session-scoping (see `_marker_path`) is transparent here —
# orchestrator and subagent share markers within one conversation, as before. If it is DISTINCT,
# a subagent's own project-specific mandatory skill (the ones NOT in SUBAGENT_NA_SKILLS) would
# need its own fresh marker written from inside that subagent's session — a subagent that relies
# on the orchestrator having invoked it would now see it as missing.
#
# CORRECTION vs. an earlier version of this comment: the worst case is NOT "an extra advisory
# WARN, never a hard block" — the WARN/BLOCK escalation TIER is ALSO session-scoped (see
# `_tier_marker`), so a subagent that repeats a work action within its OWN session while
# genuinely missing a project-specific skill escalates to a real BLOCK on the second action,
# same as any other session would. This is arguably the CORRECT behavior, not a bug: if the
# subagent's session truly never invoked the skill, it plausibly never had that skill's rules
# loaded either, so demanding it invoke its own copy is consistent with what the gate is FOR —
# but it is a real behavior change from the pre-session-scoping global-marker world (where the
# parent's invocation transparently satisfied the subagent via the shared global marker), not
# the harmless "extra WARN" this comment previously claimed. Same mitigation as any other false
# positive: `RIG_HATCH_REQUEST_SKILLS_READ_GATE` (see `_block_message`) is the escape valve, not
# a code-level exemption for the DISTINCT-session-id case.
SUBAGENT_NA_SKILLS = frozenset({"delegate-work-to-subagents", "visual-proof-cycle"})

# Work-shaped actions this gate fires on: a commit, or a build/test command.
# Anchored to a COMMAND invocation (line start, or after a |/&/; separator) with `commit` as
# git's subcommand. Global flags AND their values (`git -C /repo commit`, `git -c k=v commit`)
# are allowed between `git` and `commit`, but the run may NOT cross a command separator — so
# plain text such as `echo "remember to git, then commit"` does NOT trip it (B2).
GIT_COMMIT = re.compile(r"(?:^|[|&;]\s*)git(?:[ \t]+[^\s;&|]+)*?[ \t]+commit\b")
# A rebase/merge plumbing step (`git commit --continue/--abort/--skip`) is not a fresh authoring
# action → not gated. Detected from the PARSED argv (see ``is_skip_commit``), NOT the raw string:
# a token that only appears in a shell COMMENT (`git commit -m x # --abort`) or in the commit
# MESSAGE (`git commit -m 'support --skip'`) must NOT exempt a real commit from the skills gate.
SKIP_FLAGS = frozenset({"--continue", "--abort", "--skip"})
# The build/test runner must be the REAL invoked command of a segment — not a word buried in a
# string argument — so that `git commit -m "fix: npm test was flaky"` and `echo "see npm test
# output"` are NOT mis-classified as build/test work.
#
# This pattern is matched at the HEAD of an already-PARSED segment (see `_is_build_or_test`),
# never against the raw command string. The raw-string form it replaces anchored each
# alternative on a shell separator character (`|`, `&&`, `;`, `(`), and those characters appear
# INSIDE quoted arguments too — so a read-only `rg -n 'storybook|vitest' package.json` had its
# quoted alternation read as a pipe into a `vitest` command and was BLOCKED as build/test work
# (agent-tools#578: 31 such refusals across four codex sessions, every one a read-only `rg`,
# `sed` or `ps`). Deciding from the parsed argv is the same #59-doctrine correction the sibling
# `no-long-inline-process` already made for its own copy of this problem (agent-tools#60).
_BUILD_OR_TEST_HEAD = re.compile(
    r"(?:npm|pnpm|yarn|bun|deno)\b[^&;|]*\b(?:test|build)\b"
    r"|(?:pytest|vitest|jest)\b"
    r"|cargo\b[^&;|]*\b(?:test|build)\b"
    r"|go\b[^&;|]*\b(?:test|build)\b"
    r"|(?:make|rake|msbuild)\b[^&;|]*\b(?:test|build|all)\b"
    r"|(?:mvn|gradle)\b[^&;|]*\b(?:test|build|verify|package)\b"
)

# Leading no-op wrappers peeled off a segment head so the REAL runner beneath is what gets
# matched (`timeout 600 npm test`, `env CI=1 pytest`). Only wrappers whose own operands we
# model are listed — peeling one only partway would leave a non-command at the head. Mirrors
# the sibling hook's `_strip_wrappers` set.
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer"})
_WRAPPER_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after", "-n", "-u"})
_MAX_WRAPPER_NESTING = 16
# A `VAR=value` token at a segment head is an inline env assignment, transparent to WHICH
# command runs (`CI=1 vitest run` invokes `vitest`).
_INLINE_ENV_TOKEN = re.compile(r"^[A-Za-z_]\w*=")

# Canonical hook id for the shared Telegram hatch (RIG_HATCH_REQUEST_SKILLS_READ_GATE).
HOOK_ID = "skills-read-gate"


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"skills-read-gate: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present).

    TRUST BOUNDARY — this gate uses agent_id to RELAX (drop two mandatory skills), so the read
    surface must be exactly the one the bridge sanitizes, and no wider. We read ONLY
    `args.agent_id`: lib/cc_hook_bridge enforces T2 precedence before the event reaches us — it
    OVERWRITES args.agent_id from CC's authoritative top-level field when CC supplies it and DROPS
    any model/tool_input-supplied copy when CC does not. So the only way a truthy `args.agent_id`
    survives is if CC itself dispatched a subagent; a main-thread agent cannot forge it.

    DELIBERATE DIVERGENCE from orchestrator-stays-thin (which also reads a top-level
    `event.get("agent_id")` fallback): the bridge NEVER writes a top-level `agent_id` (its v1 event
    has no such key), so that fallback is a DEAD path that is nonetheless a TRUSTED, unsanitized
    relax-surface — if any non-bridge producer ever set a top-level agent_id from a model-influenced
    field, the orchestrator would self-exempt. Because this gate relaxes on the signal, we narrow
    the read to the sanitized `args.agent_id` only. (Follow-up: orchestrator-stays-thin should adopt
    the same narrowing — agent-tools#115.) An empty/whitespace value is not a subagent."""
    args = event.get("args") or {}
    aid = args.get("agent_id")
    return bool(aid and str(aid).strip())


def _mandatory_skills(*, subagent: bool = False) -> list[str]:
    raw = os.environ.get("MANDATORY_SKILLS", DEFAULT_MANDATORY)
    skills = [s.strip() for s in raw.split(",") if s.strip()]
    if subagent:
        # Drop the orchestration/visual defaults that are N/A for a dispatched subagent; keep any
        # project-specific mandatory skill — a subagent doing work should still read those.
        skills = [s for s in skills if s not in SUBAGENT_NA_SKILLS]
    return skills


def _strip_shell_comment(command: str) -> str:
    """Drop a trailing shell comment (`# …`) the shell never executes.

    Only an UNQUOTED `#` that starts a word begins a comment, so a `#` inside a quoted commit
    message (`-m 'fix #42'`) is preserved. Best-effort: on a tokenization failure the raw
    command is returned unchanged."""
    try:
        return " ".join(shlex.split(command, comments=True))
    except ValueError:
        return command


# SYNC: the commit-segment parser below (_segments / _commit_flags / is_skip_commit) is mirrored
# in visual-proof-gate/visual_proof_gate.py — each hook is a self-contained standalone script run
# as its own subprocess (no shared import path), so the logic is duplicated by design. Keep both
# in step when changing skip-flag handling.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


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
    the command is NOT skip-exempt and must be gated normally. Ports the identical, already
    review-approved fix from visual_proof_gate.py's mirrored parser (agent-tools#172/#176) —
    keeps both hooks' skip-flag handling in step, per the SYNC comment above."""
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


# A leading inline `VAR=value` env-assignment run at a command head (line start or right after a
# `|`/`&`/`;` separator). A real shell applies it as environment to the following command, so it
# is transparent to WHICH command runs — but it pushes `git` off the command head and defeats the
# `GIT_COMMIT` anchor. Chief case: the documented inline hatch form
# `RIG_HATCH_REQUEST_SKILLS_READ_GATE="why" git commit …`, which must still be detected as a
# commit (else the gate silently allows it, never reaching the Telegram hatch). Stripped only for
# detection; the value/quote handling covers "double"/'single'/bare forms.
_INLINE_ENV_PREFIX = re.compile(
    r"(?P<sep>^|[|&;]\s*)"
    r"(?:[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|[^\s|&;]+)[ \t]+)+"
)


def _strip_leading_inline_env(command: str) -> str:
    """Drop leading `VAR=value` env-assignment runs at each command head, so a command whose real
    executable is prefixed by inline env (`RIG_HATCH_REQUEST_…="why" git commit`) is detected as
    that executable. Prose stays safe: assignments inside quotes are not at a command head."""

    return _INLINE_ENV_PREFIX.sub(lambda m: m.group("sep"), command)


def _basename(tok: str) -> str:
    """`./node_modules/.bin/vitest` → `vitest`: a path-qualified runner is still the runner."""
    return tok.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading no-op wrappers (`timeout 600`, `env CI=1`, `nice -n10`, `time`, …) so the REAL
    command beneath is what head inspection sees. For a recognized wrapper its option flags (and
    their separate values), `env`'s `VAR=value` operands and `timeout`'s DURATION positional are
    skipped; a bare wrapper (`time make build`) is dropped to expose the next token. A non-wrapper
    head is returned unchanged; a pathological chain stops at the nesting cap (→ no match, the
    fail-open default of this discipline gate)."""
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
            if wrapper == "env" and "=" in tok:
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional (the wrapped command follows)
            break
        if i:
            argv = rest[i:]  # consumed the wrapper's own args → real command starts here
        elif rest:
            argv = rest  # bare wrapper with no args of its own
        else:
            break  # wrapper with nothing after it
    return argv


def _segment_head(segment: list[str]) -> list[str]:
    """The argv a shell would actually EXEC for one segment: inline `VAR=value` assignments and
    no-op wrappers peeled, argv[0] reduced to its basename."""
    i = 0
    while i < len(segment) and _INLINE_ENV_TOKEN.match(segment[i]):
        i += 1
    argv = _strip_wrappers(segment[i:])
    return [_basename(argv[0]), *argv[1:]] if argv else []


def _is_build_or_test(command: str) -> bool:
    """True when some segment's REAL invoked command is a build/test runner.

    Decided from the PARSED argv, never the raw string: `shlex` keeps a quoted `'a|vitest'` as
    ONE argument, so a runner name inside it can never be read as a command head (#578). A
    tokenization failure (unbalanced quote) returns False — this gate is process discipline
    (on_error=open), so a parse failure must never invent a work action."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False
    for segment in _segments(tokens):
        head = _segment_head(segment)
        if head and _BUILD_OR_TEST_HEAD.match(" ".join(head)):
            return True
    return False


def _is_work_action(command: str) -> bool:
    # Strip leading inline env first (so `RIG_HATCH_REQUEST_…="why" git commit` still trips the
    # gate), then detect the commit on the comment-stripped command and judge skip-ness from the
    # parsed argv — so a skip token in a trailing comment / commit message can't bypass the gate.
    env_free = _strip_leading_inline_env(command)
    stripped = _strip_shell_comment(env_free)
    if GIT_COMMIT.search(stripped) and not is_skip_commit(env_free):
        return True
    return _is_build_or_test(command)


def _fresh(p: Path) -> bool:
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) <= FRESH_WINDOW_S
    except OSError:
        return False


_MAX_SESSION_ID_LEN = 128


# SYNC: duplicated (near-verbatim) from skills-marker-writer's own `_sanitize_session_id` —
# same "each hook is a self-contained standalone script, no shared import path" convention as
# the hatch-escalation loader above. Edit both copies together.
def _sanitize_session_id(session_id: str) -> str | None:
    """Return `session_id` if safe to use as a SINGLE path segment, else None (caller falls
    back to the pre-existing global, non-session-scoped marker path). A session id is never
    expected to contain `/` — CC generates it, not the model."""
    session_id = session_id.strip()
    if not session_id or len(session_id) > _MAX_SESSION_ID_LEN or "\x00" in session_id:
        return None
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        return None
    return session_id


def _marker_path(skill: str, session_seg: str | None) -> Path:
    """The freshness-marker path for `skill`, nested under `session_seg` when one is present,
    else the pre-session-scoping global path. `session_seg` is CC's own session id, forwarded
    with T2 precedence in lib/cc_hook_bridge/dispatch.py (the same treatment as `agent_id`) —
    a value riding in via `tool_input` cannot spoof it into borrowing another session's
    marker."""
    if session_seg:
        return INVOKED_DIR / session_seg / skill
    return INVOKED_DIR / skill


def _missing_skills(*, subagent: bool = False, session_seg: str | None = None) -> list[str]:
    """A skill is satisfied if EITHER its session-scoped marker is fresh, OR (when a session
    is known) its global marker is fresh. Two reasons this is an OR, not "session only":

    - Without session-scoping at all, the marker is one mtime file shared by every concurrent
      Claude Code session for this user — session A invoking a mandatory skill would silently
      satisfy session B's gate even though B never read it. The session-scoped check closes
      that: `skills-marker-writer` (CC's own producer) always writes to the session-scoped
      path when a real CC session id is available, which it always is on live CC traffic, so
      in practice CC's own writes never land on the global path at all.
    - The global path is still the ONLY marker path any harness/workflow WITHOUT a session-
      aware producer can write to — e.g. Codex/opencode (no `pre-skill` mapping yet, see
      `agent-hooks/README.md`'s `pre-skill` section), or the documented manual `touch` recipe
      in this hook's own README. Making the session-scoped check exclusive would silently
      break that pre-existing, still-relied-on fallback the moment ANY session id rides along
      on the event (Codex's own bridge forwards one on `pre-bash` too) even though nothing
      ever writes the session-scoped marker for it. So the global marker stays a valid,
      lower-precedence signal — it just never wins for CC because CC has moved off it."""
    missing = []
    for s in _mandatory_skills(subagent=subagent):
        if _fresh(_marker_path(s, session_seg)):
            continue
        if session_seg and _fresh(_marker_path(s, None)):
            continue
        missing.append(s)
    return missing


def _tier_marker(event: dict, session_seg: str | None) -> Path:
    """The WARN/BLOCK escalation-tier marker for this cwd, keyed by session when one is
    known. Without `session_seg` in the key, a WARN in session A's cwd would make session
    B's FIRST action in that same cwd escalate straight to BLOCK — B never got its own WARN,
    which defeats the tiering doctrine (first offense warns, repeat blocks) just as surely
    as the freshness marker leaking cross-session did. Unlike the freshness marker
    (`_missing_skills`), there is no global-fallback OR-check here: no harness/workflow has
    ever relied on a shared, non-session-scoped tier marker (it is pure local escalation
    state, not something a human manually primes), so there is nothing to preserve
    compatibility with."""
    cwd = str(event.get("cwd") or "default")
    key = f"{session_seg}\x1f{cwd}" if session_seg else cwd
    sid = hashlib.sha256(key.encode()).hexdigest()[:16]
    return TIER_DIR / f"{sid}.warned"


def _is_repeat(event: dict, session_seg: str | None) -> bool:
    """True if a WARN already fired in this (session, cwd) within the window (→ now BLOCK)."""
    m = _tier_marker(event, session_seg)
    try:
        if m.exists() and (time.time() - m.stat().st_mtime) <= FRESH_WINDOW_S:
            return True
    except OSError:
        return False
    try:
        TIER_DIR.mkdir(parents=True, exist_ok=True)
        m.write_text(str(time.time()))
    except OSError as exc:
        warn(f"could not write tier marker {m}: {exc} — staying in WARN tier")
    return False


_SKILL_EXAMPLE = {
    "delegate-work-to-subagents": "delegate-work-to-subagents before dispatching",
    "visual-proof-cycle": "visual-proof-cycle before a UI 'done'",
}


def _block_message(missing: list[str]) -> str:
    """The WARN/BLOCK message naming the missing skills + how to invoke/override.

    The example tail is derived from the ACTUAL missing set, not a flag: it cites only the
    orchestration/visual defaults that are genuinely in `missing`. So a subagent (those defaults
    dropped → not in `missing`) gets no misleading example, and an orchestrator whose project
    redefined MANDATORY_SKILLS without those defaults likewise won't see a stale example."""
    head = (
        f"Mandatory/relevant skills not invoked before this work: {', '.join(missing)}. "
        "Invoke them (Skill tool) first — they encode the rules this action must follow."
    )
    examples = [_SKILL_EXAMPLE[s] for s in missing if s in _SKILL_EXAMPLE]
    example = f" (e.g. {', '.join(examples)}.)" if examples else ""
    override = (
        " No self-service bypass. ASK the human, or request a one-time Telegram approval via "
        'RIG_HATCH_REQUEST_SKILLS_READ_GATE="<justification>" (deny-by-default; bare 1 rejected).'
    )
    return head + example + override


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

    if not _is_work_action(command):
        emit("allow")  # not a work-shaped action → nothing to gate
        return 0

    # A dispatched subagent IS the delegated work and may have no UI to prove, so the two
    # orchestration/visual defaults are dropped from its demanded set (project skills still apply).
    subagent = _is_subagent(event)
    raw_session_id = args.get("session_id")
    session_seg = _sanitize_session_id(raw_session_id) if isinstance(raw_session_id, str) else None
    missing = _missing_skills(subagent=subagent, session_seg=session_seg)
    if not missing:
        emit("allow")  # every demanded skill has a fresh marker → satisfied
        return 0

    message = _block_message(missing)
    cwd = str(event.get("cwd") or os.getcwd())

    # A present hatch request is authoritative: it resolves the gate immediately (approved →
    # allow; denied → block, regardless of the WARN/BLOCK tier — an explicit human "no" is a
    # hard deny). An UNSET request falls through to the normal WARN-first tier below.
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, {"hook": HOOK_ID, "command": command}, cwd=cwd, command=command,
    )
    if hatch.should_stop:
        if hatch.approved:
            note = f"skills gate allowed via hatch escalation ({hatch.reason})"
            warn(note)
            emit("allow", note)
            return 0
        warn(f"skills gate hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{message}")
        return BLOCK_EXIT_CODE

    # WARN first, BLOCK on repeat within the window.
    if _is_repeat(event, session_seg):
        emit("block", message)
        return BLOCK_EXIT_CODE
    warn(message)
    emit("allow", message)  # advisory first offense (marker-writer may not be wired yet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
