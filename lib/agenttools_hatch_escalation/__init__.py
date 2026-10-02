"""Shared Telegram hatch escalation helper for agent-hook scripts.

The helper is intentionally small and stdlib-only: hook scripts import it directly from the
agent-tools checkout, before package installation can be assumed. It turns a per-hook env var
(`RIG_HATCH_REQUEST_<HOOK_ID>`) into a one-time `tg-ctl ask` call through a trusted absolute
path. It never consults ambient PATH.

Every attempt where the hatch env var carried ANY value is also appended, best-effort, as one
JSON line to `overrides.log` (retrospective 2026-07-01, section 5.2.3 item 3 / gap G-8: "escape
hatches have no audit sink"). See `_append_overrides_log` for the record shape and trust model.

Late-Approval Grant Protocol (agent-tools#742)
----------------------------------------------
A hatch ask can expire (tg-ctl's ~115s window) BEFORE the human taps Approve. The tg-ctl daemon
persists such a belated approval as a single-use grant (`approved:<grant-id>`, 24h expiry) bound
to the operation that was asked about. This lib consumes those grants two ways:

1. Token path (explicit): the agent retries the IDENTICAL operation in the SAME directory with
   `RIG_HATCH_REQUEST_<HOOK_ID>="approved:<grant-id>"` (inline prefix for pre-bash hooks;
   exported var where the harness permits). The lib verifies the grant via
   `tg-ctl ask --verify-grant <id> --hook <hook_id>` and allows ONLY on an explicit daemon
   "allow". The token path is route-agnostic: the id itself was delivered to this agent.
2. Pull path (automatic): when NO hatch env var / inline value is present at all and the hook
   would otherwise deny, the lib makes ONE bounded (~15s) pull query,
   `tg-ctl ask --verify-grant --pull --hook <hook_id>`. The daemon matches an unconsumed,
   unexpired grant on (hook_id, operation fingerprint, cwd) with ROUTE EQUALITY REQUIRED — when
   no stable route identity exists on either side the daemon denies the auto-pull (another agent
   in the repo must not consume a grant addressed to a specific subagent) and its deny reason —
   surfaced VERBATIM — points at the explicit token path. Any pull infra failure (tg-ctl
   missing/error/timeout/unparseable) fails CLOSED: the original deny proceeds unchanged.

Operation binding: every ask/verify/pull payload carries
`operation: {kind, fingerprint, cwd, repoRoot}`. For pre-bash hooks the fingerprint is the
canonicalized command (the leading `RIG_HATCH_REQUEST_<HOOK>=...` assignment — justification OR
`approved:<id>` — is stripped, so the retry prefix is invisible to the comparison). For pre-write
hooks the fingerprint is `sha256:<hex>` over a canonical JSON serialization of the operation's
ACTUAL tool inputs ({tool_name, file_path, ...content fields...}); raw file content NEVER leaves
the hook process (not into the payload, the Telegram question, or logs — the daemon compares
digest strings opaquely). background-subagent-gate threads its normalized dispatch identity
through `hatch_dispatch_op_fields`, so its fingerprint binds the exact dispatch (prompt,
subagent_type, description, dispatch mode — digested, never rendered raw). Hooks whose context
carries no file target AND no dispatch identity (subagent-no-monitor) fall back to a digest of
the canonical context JSON — hook+context granularity, deliberately coarser for exactly that
hook, and documented as such. A retry with a CHANGED command/edit/cwd is denied WITHOUT
consuming the grant; a byte-identical retry consumes it exactly once.

Fail-closed binding (ruling 2026-09-12, agent-tools#742): background-subagent-gate is
DISPATCH-BOUND — an ask or verify for that hook whose context carries no dispatch identity is
REJECTED before any tg-ctl contact, and its pull sends no query at all, so an older or partial
caller can never mint or consume a grant at hook-only granularity even if the gate-side check
is missing or bypassed. The gate itself denies one layer earlier with the same effect
(belt-and-suspenders); see `_dispatch_binding_reason`.

Retry guidance surfaced to agents: do this only if the goal is still relevant (not already
achieved another way); the grant is single-use. Known limitation: CC auto-mode blocks EXPORTING
hatch vars for pre-write hooks (agent-tools#336) — there the pull path is the reachable one.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shlex
import signal
import subprocess  # noqa: S404 - runs an already-resolved absolute tg-ctl path
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

# A valid shell env-assignment variable name (`RIG_HATCH_REQUEST_FOO`, `LANG`). Used to tell a
# leading `VAR=value` inline assignment apart from the executable / a `--flag=value` argument
# when peeling the documented inline hatch form off a Bash command string.
_ASSIGNMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The shell control-operator characters that separate one simple command from the next. A token
# made up solely of these (`;`, `&`, `|`, `&&`, `||`, `|&`, `;;`) is a separator, so a leading
# `VAR=value` inline assignment can appear at the head of ANY resulting segment (`cd repo &&
# VAR=x git commit`, `foo ; VAR=x bar`, `sleep 1 & VAR=x cmd`, `VAR=x cmd | pager`). shlex tags
# these OUTSIDE quotes only, so a `;`/`&`/`|` inside a quoted value is never mistaken for one.
_SEPARATOR_CHARS = frozenset(";&|")

MAX_TG_CTL_TIMEOUT_S = 900.0
DEFAULT_TG_CTL_TIMEOUT_S = MAX_TG_CTL_TIMEOUT_S
DEFAULT_PROCESS_MARGIN_S = 30.0
_DETAIL_CAP = 500
# The agent identity `tg-ctl ask` labels the Telegram prompt with. Sent ONCE, on argv — the
# ButtonRequest payload's own `agent` field must carry the same value, so both read this constant
# rather than spelling the string twice (a silent mismatch between the two channels is exactly the
# protocol-drift class the stdin-JSON contract below was rewritten to close).
_ASK_AGENT = "claude"
_RIG_TG_CTL_KEY = "tg_ctl_path"
_BARE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})
# Long enough to reject a command name used as the value (`pytest`) and
# short enough that a real phrase still qualifies.
_MIN_JUSTIFICATION_CHARS = 8

# Late-Approval Grant Protocol (agent-tools#742 — see the module docstring). A hatch value of
# `approved:<grant-id>` routes to `tg-ctl ask --verify-grant` instead of posting a new ask.
_APPROVED_GRANT_RE = re.compile(r"^approved:([A-Za-z0-9-]+)$")
# The canonical token-path guidance JUNCTION the daemon's designated no-stable-route deny
# carries (`RIG_HATCH_REQUEST_<HOOK>="approved:<id>"`) — the fallback identification when the
# daemon does not mark the deny structurally (`designatedDeny: "no-stable-route"`).
_TOKEN_PATH_GUIDANCE_RE = re.compile(r'RIG_HATCH_REQUEST_[A-Z0-9_]+="approved:')
# Ask-window the daemon is asked to honor for a NORMAL hatch ask (10 min; the daemon may cap at
# 15). Long enough that a human who is actually around can answer; short enough that a wedged
# hook still resolves inside its descriptor budget (bounded by MAX_TG_CTL_TIMEOUT_S anyway).
_ASK_TIMEOUT_MS = 600_000
# BOTH daemon-side grant lookups — the explicit `--verify-grant <id>` consume and the automatic
# `--verify-grant --pull` match — are daemon-LOCAL store lookups, not human round trips. They
# must NEVER inherit the 900s ask budget, or a wedged daemon would stall the encouraged
# `approved:<id>` retry (or every guarded deny) for minutes. Bounded short; any over-run fails
# closed to a deny.
_GRANT_LOOKUP_TIMEOUT_S = 15.0
# Per-PROCESS negative pull memo — see `_pull_late_grant._no_grant`. A hook process is
# one-shot, so this never outlives one gated command.
_PULL_NEGATIVE: set[tuple[str, str]] = set()
# The context/tool_input keys that can name a pre-write TARGET file, in precedence order.
# Shared by `hatch_write_op_fields` and `_operation_descriptor` (the hooks' own `_target_dir`
# helpers keep their pre-existing copies, tied by their SYNC comments).
_WRITE_TARGET_KEYS = ("file_path", "path", "notebook_path")
# The exact tool_input fields that canonicalize a pre-write operation for its sha256
# fingerprint, keyed by lowercased tool name (contract v1.2 item 2a — a grant for one edit must
# NOT authorize a different edit to the same file). Keys are INCLUDED with null when the tool
# input lacked them, so "absent" and "present-but-empty" never collide across harnesses.
_WRITE_OP_INPUT_KEYS = {
    "edit": ("file_path", "old_string", "new_string", "replace_all"),
    "write": ("file_path", "content"),
    "multiedit": ("file_path", "edits"),
    "notebookedit": ("notebook_path", "cell_id", "new_source", "edit_mode"),
}
# Context keys holding the proposed write's raw CONTENT or a dispatch's raw prompt text. They
# feed the fingerprint digest (in-process) but are stripped before the Telegram question is
# rendered — raw edit content and raw dispatch text must never leave the hook process into a
# Telegram card.
_QUESTION_CONTENT_KEYS = frozenset(
    {"old_string", "new_string", "content", "edits", "new_source", "prompt", "description", "task"}
)
# Late-approval grant binding profiles (ruling 2, 2026-09-12): hooks whose grants are
# DISPATCH-BOUND — every ask/verify/pull for one of these hooks REQUIRES dispatch identity
# fields in the context (threaded by `hatch_dispatch_op_fields`). A payload for such a hook
# that carries none is rejected fail-closed BEFORE any tg-ctl contact: hook-only granularity
# would let a grant minted for one blocked dispatch be consumed by a later, unrelated one
# behind the same hook_id — the exact P1 class this closes. The gate-side check in
# background_subagent_gate.py denies one layer earlier; THIS is the belt-and-suspenders layer
# that holds even for an older or partial caller whose gate check is missing or bypassed.
_DISPATCH_BOUND_HOOKS = frozenset({"background-subagent-gate"})
# What counts as dispatch IDENTITY. `run_in_background` is deliberately absent — every
# dispatch carries it (explicitly normalized), so it distinguishes nothing. SYNC: the gate's
# own `_DISPATCH_IDENTITY_KEYS` copy (agent-hooks stay self-contained, no shared import).
_DISPATCH_IDENTITY_KEYS = (
    "tool_name",
    "subagent_type",
    "agent",
    "description",
    "prompt",
    "task",
    "isolation",
)
# Retry contract appended to ask-expired denials and documented in the module docstring.
_LATE_APPROVAL_RETRY_HINT = (
    "If a belated approval arrives afterwards as approved:<id>: retry the IDENTICAL operation "
    "ONCE in the SAME directory — pre-bash: the SAME command with "
    'RIG_HATCH_REQUEST_<HOOK>="approved:<id>" inline; pre-write (or any hook): simply retry the '
    "identical operation, the grant is consumed automatically (the automatic pull requires this "
    "session's route identity to match the original ask; if the retry still denies, ask the "
    "human via tg --tag decision). Byte-for-byte only: a changed command/edit is denied and "
    "does NOT consume the single-use grant. Retry only if the goal is still relevant (not "
    "already achieved another way)."
)

def _is_written_justification(text: str) -> bool:
    """True when `text` is a real written reason, not a flag or a stub.

    Agents have used the gated command name itself (`pytest`) as the hatch
    value; that is not a justification and must not open a Telegram round-trip.
    """
    if not text:
        return False
    if text.lower() in _BARE_FLAG_VALUES:
        return False
    return len(text) >= _MIN_JUSTIFICATION_CHARS


_TRUSTED_TG_CTL_PATHS = (
    Path("/Users/ultra/.files/bin/tg-ctl"),
    Path("/usr/local/bin/tg-ctl"),
    Path("/opt/homebrew/bin/tg-ctl"),
)

# Default location of the escape-hatch audit sink, relative to the account's REAL home
# (`resolve_home()`) — see `_resolve_overrides_log_path` for why it is rooted there and not at
# `$HOME`/cwd. Mirrors the existing `ci/ship/ship.sh` convention of `~/.config/agent-tools/*` for
# this repo's own audit artifacts (`ship-audit.jsonl`).
_OVERRIDES_LOG_RELATIVE = Path(".config") / "agent-tools" / "overrides.log"
# Optional full-path override for the audit sink, mirroring `ship.sh`'s `SHIP_AUDIT_FILE`. See
# `_resolve_overrides_log_path` for the precedence order and its trust-level trade-off.
_OVERRIDES_LOG_ENV = "AGENT_TOOLS_OVERRIDES_LOG"
# Best-effort session/agent identifiers to check, in order, when no caller-supplied context value
# is present. None of these are guaranteed to exist in a given hook's process env — that's why the
# chain ends in a `pid:<getpid()>` fallback that is never empty.
_SESSION_ID_ENV_VARS = (
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "RIG_SESSION_ID",
    "TMUX_PANE",
)


@dataclass(frozen=True)
class HatchApprovalResult:
    """Result of checking one hook's Telegram hatch request.

    `env_present` means "a hatch MECHANISM engaged", not literally "the env var was set": it is
    also True for a pull-path verdict (a route-matched grant consumed, or the daemon's
    designated no-stable-route deny surfaced) — any outcome that must suppress the caller's
    later approval mechanisms (`should_stop`). The result's `reason` always carries the precise
    provenance (token consume, pull consume, surfaced deny)."""

    requested: bool
    approved: bool
    reason: str
    env_var: str
    env_present: bool = False
    tg_ctl_path: str | None = None
    # Set whenever a late-approval grant (`approved:<id>` token, or a pull-matched grant) was
    # involved — allow OR deny — so the audit line can record WHICH grant was tried.
    approval_id: str | None = None
    # True only when approval came from the automatic PULL path (no hatch env var was present);
    # lets the audit line use the distinct `approved-late-pull` decision.
    grant_pull: bool = False

    @property
    def should_stop(self) -> bool:
        """True when the hook should not fall through to later approval mechanisms."""

        return self.env_present


def hatch_env_var(hook_id: str) -> str:
    """The env var that requests a Telegram hatch for a canonical hook id."""

    canonical = hook_id.strip().upper().replace("-", "_")
    return f"RIG_HATCH_REQUEST_{canonical}"


def _parse_inline_hatch_value(env_var: str, command: str) -> str | None:
    """Extract the value of a leading inline `<env_var>=<value>` assignment from a Bash command.

    Mirrors how a POSIX shell applies a `VAR=value cmd` prefix: only assignments at the very
    start of a simple command — before its executable token — are environment assignments;
    anything after the executable is an argument, not an assignment. The command is tokenized
    quote-aware (shlex, honoring quotes and `\\`-newline line continuations) and split into simple
    commands at the shell control operators (`;`, `&`, `|`, `&&`, `||`, `|&`), so the documented
    inline form works even when the gated command is not the first one on the line
    (`cd repo && RIG_HATCH_REQUEST_X="why" git commit …`, `RIG_HATCH_REQUEST_X="why" \\` + newline
    + ` gh pr merge …`). A command that cannot be tokenized (unbalanced quotes) yields None.

    The hook gates and — on approval — allows the ENTIRE Bash command as one unit, and the
    resolved justification is shown verbatim to the human approver (the full `command` is echoed
    in the tg-ctl question), so a leading `<env_var>=…` assignment on ANY segment is treated as a
    request to approve the whole command. ONLY the hook's own `env_var` is honored; other leading
    assignments are skipped, never consumed. Returns None when the var is not present as any
    segment's leading assignment.

    SCOPE: this recognizes the documented bare inline form (`RIG_HATCH_REQUEST_X="why" <cmd>`,
    including behind `&&`/`||`/`;`/`&`/`|`/newline separators and `\\`-newline continuations). It
    deliberately does NOT peel command wrappers (`env VAR=x cmd`, `sudo`, `timeout …`) or a
    subshell/`export` prefix — for those, export the variable into the harness environment (the
    `os.environ` path), which always works.
    """

    segments = _split_command_segments(command)
    if segments is None:
        return None
    for tokens in segments:
        value = _leading_assignment_value(env_var, tokens)
        if value is not None:
            return value
    return None


def _tokenize_command(command: str) -> list[str] | None:
    """Tokenize `command` for hatch VALUE parsing (posix shlex — quotes are UNQUOTED, so the
    inline hatch value reaches the Telegram question as plain text), or None if it cannot be
    tokenized. Used by `_split_command_segments`; the fingerprint path deliberately uses the
    quote-PRESERVING `_quote_preserving_tokens` instead. See `_fold_command_continuations`
    for the continuation/newline normalization."""
    normalized = _fold_command_continuations(command)
    lexer = shlex.shlex(
        normalized, posix=True, punctuation_chars="".join(_SEPARATOR_CHARS)
    )
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _split_command_segments(command: str) -> list[list[str]] | None:
    """Tokenize `command` quote-aware and split it into per-simple-command token lists at the
    shell control operators that begin a new command. Returns None if it cannot be tokenized.

    Uses `shlex` with `punctuation_chars` so `;`/`&`/`|` (and their runs `&&`/`||`/`|&`) are
    recognized as operators only OUTSIDE quotes — a separator inside a quoted argument stays part
    of that token. See `_tokenize_command` for the continuation/newline normalization."""
    tokens = _tokenize_command(command)
    if tokens is None:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _SEPARATOR_CHARS:
            segments.append([])  # a control operator ends the current simple command.
        else:
            segments[-1].append(token)
    return segments


def _leading_assignment_value(env_var: str, tokens: list[str]) -> str | None:
    """The value of a leading `<env_var>=<value>` assignment among one segment's leading tokens."""

    for token in tokens:
        name, sep, value = token.partition("=")
        if not sep or not _ASSIGNMENT_NAME_RE.match(name):
            break  # reached the executable / a non-assignment argument — stop scanning.
        if name == env_var:
            return value
    return None


def hatch_write_op_fields(
    tool: str | None, args: Mapping[str, object]
) -> dict[str, object]:
    """The exact tool_input fields a pre-write hook threads into its hatch context so a
    late-approval grant binds to THIS byte-exact operation (agent-tools#742, contract v1.2).

    Selected by case-insensitive tool name — Edit: file_path/old_string/new_string/replace_all;
    Write: file_path/content; MultiEdit: file_path/edits — INCLUDING false/empty values
    (`replace_all` defaults to False, Edit's semantic default when the harness omits the flag),
    because omitting a key would change the canonical object the lib digests. An unrecognized
    write tool (a harness-specific edit envelope) contributes only tool_name + file_path: the
    grant then binds at path granularity, which the lib docstring documents as coarser. The lib
    digests these fields in ONE place (`_operation_descriptor`); hooks stay dumb pass-throughs
    and NEVER render them into the Telegram question (`_redacted_context`).
    """

    fields: dict[str, object] = {}
    tool_name = str(tool or "").strip()
    if tool_name:
        fields["tool_name"] = tool_name
    keys = _WRITE_OP_INPUT_KEYS.get(tool_name.lower())
    if keys is None:
        # The SAME target-key triple the pre-write hooks and bridges use (`_operation_descriptor`
        # mirrors it) — a `notebook_path`-only event must still bind at PATH granularity, not
        # degrade to a branch-level hook-event digest.
        for key in _WRITE_TARGET_KEYS:
            file_path = args.get(key)
            if isinstance(file_path, str) and file_path:
                fields["file_path"] = file_path
                break
        return fields
    for key in keys:
        value = args.get(key)
        if key == "replace_all" and value is None:
            value = False
        fields[key] = value
    return fields


def hatch_dispatch_op_fields(
    tool: str | None, args: Mapping[str, object]
) -> dict[str, object]:
    """The dispatch identity fields a subagent-gating hook threads into its hatch context so
    a late-approval grant binds to THIS exact dispatch (agent-tools#742, P1 amendment
    2026-09-10) — not to every blocked prompt behind the same hook_id.

    `tool_name` (stripped, omitted when empty) plus the non-empty string fields
    subagent_type/agent/description/prompt/task/isolation (stripped) and `run_in_background`
    ALWAYS explicit (bool as-is; "true"/"false" case-insensitive; anything else or absent →
    False — the same absent-vs-empty discipline as `replace_all`). The lib digests them via
    the hook-event branch of `_operation_descriptor` in ONE place; hooks stay dumb
    pass-throughs and raw prompt/description text NEVER renders into the Telegram question
    (`_redacted_context`) — digest strings only. A context enriched with these fields also
    satisfies the dispatch-bound binding profile: `_dispatch_binding_reason` rejects a
    background-subagent-gate ask/verify/pull whose context carries none (ruling 2, 2026-09-12).
    """

    fields: dict[str, object] = {}
    tool_name = str(tool or "").strip()
    if tool_name:
        fields["tool_name"] = tool_name
    for key in ("subagent_type", "agent", "description", "prompt", "task", "isolation"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    flag = args.get("run_in_background")
    if isinstance(flag, bool):
        fields["run_in_background"] = flag
    elif isinstance(flag, str) and flag.strip().lower() == "true":
        fields["run_in_background"] = True
    else:
        fields["run_in_background"] = False
    return fields


def _dispatch_binding_reason(hook_id: str, context: Mapping[str, object]) -> str | None:
    """The fail-closed deny reason when a DISPATCH-BOUND hook's payload carries no dispatch
    identity (ruling 2 refinement, 2026-09-12), or None when the binding profile is satisfied
    (or the hook is not dispatch-bound). Ask and verify paths deny with this reason BEFORE any
    tg-ctl contact; the pull path returns no verdict at all. Hook-only granularity for
    background-subagent-gate would let a grant minted for one blocked dispatch be consumed by
    a later, unrelated one behind the same hook_id — the exact P1 class. The gate denies one
    layer earlier; this lib-side layer holds even for an older or partial caller whose gate
    check is missing or bypassed, so no caller can ever reach issuance or consume weakly."""

    if hook_id not in _DISPATCH_BOUND_HOOKS:
        return None
    if any(context.get(key) for key in _DISPATCH_IDENTITY_KEYS):
        return None
    return (
        f"hook {hook_id} is dispatch-bound: its hatch context carries no dispatch identity "
        "(expected tool_name/subagent_type/description/prompt from hatch_dispatch_op_fields — "
        "older caller or missing enrichment), so a late-approval grant cannot be bound to "
        "THIS dispatch; denied without contacting tg-ctl — ask/verify/pull fail closed on "
        "weak hook-only binding (agent-tools#742)"
    )


def _canonical_digest(payload: object) -> str:
    """`sha256:<hex>` over a canonical JSON serialization (sorted keys, no incidental
    whitespace, UTF-8). The ONLY form in which proposed file content may leave this process:
    the daemon compares digest strings opaquely and never sees the raw inputs."""

    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _fold_command_continuations(command: str) -> str:
    """Fold `\\`-newline continuations to a space and normalize bare newlines to `;` (a real
    shell separates commands on them, same as `;`). Shared by both command tokenizers; a
    newline INSIDE quotes survives this only because both tokenizers track quotes."""
    return command.replace("\\\n", " ").replace("\n", ";")


def _quote_preserving_tokens(command: str) -> list[str] | None:
    """Tokenize like a shell WOULD SEE a command, but KEEP every quote character in the token
    text — the fingerprint form (`_canonical_bash_fingerprint`): quote style is shell SEMANTICS
    (`'$x'` is a literal argument, `"$x"` expands it), so the two must stay distinct tokens.
    Splits on unquoted whitespace and on runs of the separator chars (`;&|`); a backslash
    outside quotes escapes the next char (both kept); inside `'…'` nothing escapes and every
    character — including a literal newline — is verbatim; inside `"…"` only `\\"`/`\\\\`/
    `\\$`/`` \\` `` escape (a `\\`-newline continuation is dropped, as the shell does). An
    UNQUOTED newline is a command separator (normalized to `;`), an unquoted `\\`-newline a
    continuation (folded to a space) — so `'a\\nb'` and `'a;b'` NEVER collide. Returns None on
    unbalanced quotes (the caller falls back to the deterministic raw string)."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0

    def _flush() -> None:
        if current:
            tokens.append("".join(current))
            current.clear()

    while i < len(command):
        ch = command[i]
        nxt = command[i + 1] if i + 1 < len(command) else ""
        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and nxt:
                if nxt == "\n":
                    i += 2  # line continuation inside double quotes — removed, as the shell does
                    continue
                current.append(ch)
                current.append(nxt)
                i += 2
                continue
            current.append(ch)
            if ch == '"':
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "\\" and nxt == "\n":
            i += 2  # unquoted continuation — both chars removed, as the shell does
            continue
        if ch == "\n":
            _flush()  # unquoted newline — a command separator, same as `;`
            tokens.append(";")
            i += 1
            continue
        if ch.isspace():
            _flush()
            i += 1
            continue
        if ch in _SEPARATOR_CHARS:
            _flush()
            end = i
            while end < len(command) and command[end] in _SEPARATOR_CHARS:
                end += 1
            tokens.append(command[i:end])
            i = end
            continue
        if ch == "\\" and nxt:
            current.append(ch)
            current.append(nxt)
            i += 2
            continue
        current.append(ch)
        i += 1
    if quote:
        return None  # unbalanced quotes — not tokenizable
    _flush()
    return tokens


def _canonical_bash_fingerprint(env_var: str, command: str) -> str:
    """The canonical form of a gated Bash command for grant binding: the leading
    `RIG_HATCH_REQUEST_<HOOK>=...` inline assignment (a written justification OR an
    `approved:<id>` token) is stripped, so `VAR="why" cmd`, `VAR=approved:<id> cmd`, and bare
    `cmd` ALL canonicalize identically and the retry prefix is invisible to the daemon's
    comparison. Separators are KEPT as tokens (`a && b` must not collide with `a; b`); other
    leading assignments are preserved (they change what the shell runs).

    Tokens are taken QUOTE-PRESERVING (`_quote_preserving_tokens`): quote style is
    SHELL SEMANTICS, not formatting — `-- '$(x)'` passes a literal `$(x)` argument while
    `-- "$(x)"` EXECUTES the substitution — so the two must NEVER share a fingerprint
    (a grant for one must not verify the other). Whitespace between tokens normalizes away;
    a `VAR='why'` vs `VAR="why"` prefix difference vanishes with the dropped assignment.
    Untokenizable input (unbalanced quotes) falls back to the raw string — still deterministic
    across an ask/retry of the same event."""

    raw_tokens = _quote_preserving_tokens(command)
    if raw_tokens is None:
        return command
    out: list[str] = []
    at_segment_head = True
    for token in raw_tokens:
        if token and set(token) <= _SEPARATOR_CHARS:
            out.append(token)
            at_segment_head = True
            continue
        if at_segment_head:
            name, sep, _value = token.partition("=")
            if sep and _ASSIGNMENT_NAME_RE.match(name):
                if name != env_var:
                    out.append(token)  # another leading assignment stays — it changes the run
                continue  # keep scanning this segment's leading assignment chain
            at_segment_head = False
        out.append(token)
    # Tokens already carry their original quoting (posix=False); separator tokens are bare.
    return " ".join(out)


def _git_repo_root(cwd: str) -> str | None:
    """The git toplevel containing `cwd`, or None. Feeds ONLY a daemon-side comparison — a
    spoofed value fails CLOSED (deny) because cwd is compared too."""

    try:
        proc = subprocess.run(  # noqa: S603,S607 - benign rev-parse; PATH git acceptable here
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _operation_descriptor(
    hook_id: str,
    env_var: str,
    context: Mapping[str, object],
    cwd: str,
    command: str | None,
) -> dict[str, object]:
    """The `operation: {kind, fingerprint, cwd, repoRoot}` block carried by EVERY ask/verify/
    pull payload so a grant authorizes the ORIGINAL operation only (contract v1.2 item 2/2a).

    - pre-bash (`command` given): kind "bash-command", fingerprint = canonical command.
    - pre-write (a file target in context): kind "file-write", fingerprint = sha256 digest of
      the operation's ACTUAL inputs per `_WRITE_OP_INPUT_KEYS`. Raw content NEVER leaves the
      hook process.
    - anything else (no file target and no dispatch identity — subagent-no-monitor): kind
      "hook-event", fingerprint = sha256 digest of the canonical context JSON. This is
      hook+context granularity — deliberately COARSER for exactly that hook (a grant
      authorizes a repeat of the same hook event, not one specific file/command), documented
      in the module docstring. background-subagent-gate no longer falls back here: it threads
      its normalized dispatch identity (`hatch_dispatch_op_fields`) into the context, so the
      same digest binds the exact dispatch (a changed prompt or subagent_type denies) — and a
      dispatch-bound context carrying NO identity never reaches this branch: the ask/verify/
      pull paths reject it fail-closed first (`_dispatch_binding_reason`, ruling 2026-09-12).
    """

    abs_cwd = os.path.abspath(cwd)
    if command:
        kind = "bash-command"
        fingerprint = _canonical_bash_fingerprint(env_var, command)
    else:
        target = next(
            (
                str(context[key])
                for key in _WRITE_TARGET_KEYS
                if isinstance(context.get(key), str) and str(context.get(key) or "").strip()
            ),
            None,
        )
        if target is not None:
            kind = "file-write"
            tool_name = str(context.get("tool_name") or "").strip()
            keys = _WRITE_OP_INPUT_KEYS.get(tool_name.lower())
            if keys is None:
                # Unrecognized write tool: bind at path granularity (coarser — see docstring).
                payload: dict[str, object] = {
                    "tool_name": tool_name,
                    "file_path": target,
                }
            else:
                payload = {"tool_name": tool_name}
                for key in keys:
                    payload[key] = context.get(key)
            fingerprint = _canonical_digest(payload)
        else:
            kind = "hook-event"
            fingerprint = _canonical_digest({"hook": hook_id, "context": dict(context)})
    return {
        "kind": kind,
        "fingerprint": fingerprint,
        "cwd": abs_cwd,
        "repoRoot": _git_repo_root(abs_cwd),
    }


_OMP_SESSION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-fA-F-]+$")
# ^ keep in lockstep with orchestrator-stays-thin / omp_hook_bridge's _OMP_SESSION_DIR
# (agent-tools#634).


def _origin_context(
    context: Mapping[str, object], env_map: Mapping[str, str]
) -> dict[str, object]:
    """Best-effort `origin: {agent, sessionId, subagent}` for the ask/verify/pull payloads.
    Absent evidence is normal (None subagent); the daemon treats origin as routing hints, never
    as an authorization input."""

    session_id = _resolve_session_id(context, env_map)
    omp_session = ""
    omp_sub = ""
    pi = (env_map.get("PI_SESSION_FILE") or "").strip()  # omp: .../<session-dir>/<agent>.jsonl
    if pi:
        pi_path = Path(pi)
        if _OMP_SESSION_DIR_RE.match(pi_path.parent.name):
            omp_session, omp_sub = pi_path.parent.name, pi_path.stem.strip()
    if session_id.startswith("pid:") and omp_session:
        session_id = omp_session
    agent = (env_map.get("TG_AGENT") or "").strip() or _ASK_AGENT
    subagent = (env_map.get("RIG_AGENT_ID") or "").strip()
    if not subagent and (env_map.get("RIG_DETACHED_AGENT") or "").strip() == "1":
        subagent = "detached"
    if not subagent:
        subagent = omp_sub
    return {"agent": agent, "sessionId": session_id, "subagent": subagent or None}


def _route_evidence(env_map: Mapping[str, str]) -> dict[str, object]:
    """The hook process's route/session identity EVIDENCE (tmux pane + the session-id env
    chain values) for the pull query. Evidence, not proof: the daemon derives its OWN observed
    route and compares — ROUTE EQUALITY REQUIRED, and when no stable route identity exists on
    either side the daemon denies the auto-pull (contract v1.2 item 4)."""

    evidence: dict[str, object] = {}
    pane = (env_map.get("TMUX_PANE") or "").strip()
    if pane:
        evidence["tmuxPane"] = pane
    session_env = {
        var: value.strip()
        for var in _SESSION_ID_ENV_VARS
        if (value := env_map.get(var) or "").strip()
    }
    if session_env:
        evidence["sessionEnv"] = session_env
    return evidence


def _redacted_context(context: Mapping[str, object]) -> dict[str, object]:
    """The context MINUS raw proposed-write content — what `_question` may render into a
    Telegram card. The full context still feeds the in-process fingerprint digest."""

    return {k: v for k, v in context.items() if k not in _QUESTION_CONTENT_KEYS}


def _reply_field(reply: object, key: str) -> str | None:
    """Tolerant extraction of a string field from a tg-ctl reply — top level first, then
    nested under hookSpecificOutput (the exact daemon reply shape lands via the sibling tg-cli
    PR; cover both)."""

    if not isinstance(reply, dict):
        return None
    value = reply.get(key)
    if isinstance(value, str) and value:
        return value
    hook_output = reply.get("hookSpecificOutput")
    if isinstance(hook_output, dict):
        value = hook_output.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _verify_hatch_grant(
    hook_id: str,
    approval_id: str,
    context: Mapping[str, object],
    *,
    cwd: str,
    env_var: str,
    command: str | None,
    env_map: Mapping[str, str],
    tg_ctl_candidates: Sequence[Path | str] | None,
) -> HatchApprovalResult:
    """The `approved:<id>` TOKEN path: ask the daemon to validate+consume a late-approval
    grant for the CURRENT operation. Allows ONLY on an explicit "allow"; every other outcome
    (deny, rc!=0, empty/unparseable stdout, timeout, launch failure) denies — never fail open,
    mirroring the ask path's SECURITY note that tg-ctl's exit 0 is NOT approval. No new
    Telegram ask is posted on this path. Route-agnostic: the id itself was delivered to this
    agent, so cross-route consume is allowed (contract v1.2 item 4).

    Bounded by `_GRANT_LOOKUP_TIMEOUT_S`, NOT the caller's ask budget: this is the same
    daemon-local store lookup the pull path makes, and the retry contract actively encourages
    agents to re-run with `approved:<id>` — a wedged daemon must deny in seconds, not minutes."""

    tg_ctl = _find_tg_ctl(tg_ctl_candidates)
    if tg_ctl is None:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=(
                "tg-ctl is not available at a trusted executable path; "
                f"late-approval grant {approval_id} cannot be verified"
            ),
            env_var=env_var,
            env_present=True,
            approval_id=approval_id,
        )

    request_id = f"hatch-verify-{hook_id}-{uuid.uuid4().hex[:12]}"
    question = _question(
        hook_id,
        f"Consume belated Telegram approval {approval_id} for this exact operation?",
        _redacted_context(context),
        cwd,
    )
    payload = {
        "requestId": request_id,
        "agent": _ASK_AGENT,
        "kind": "permission",
        "question": question,  # daemon-side only — never posted to Telegram on this path
        "title": f"Hatch grant: {hook_id}",
        "decisionLabels": {"allow": "Approve", "deny": "Deny"},
        "hookId": hook_id,
        "approvalId": approval_id,
        "operation": _operation_descriptor(hook_id, env_var, context, cwd, command),
        "origin": _origin_context(context, env_map),
        "routeEvidence": _route_evidence(env_map),
    }
    argv = [str(tg_ctl), "ask", "--verify-grant", approval_id, "--hook", hook_id]

    def _deny(reason: str) -> HatchApprovalResult:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=reason,
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
            approval_id=approval_id,
        )

    outcome = _run_tg_ctl_ask(
        argv,
        json.dumps(payload),
        _GRANT_LOOKUP_TIMEOUT_S,
        requested_timeout=_GRANT_LOOKUP_TIMEOUT_S,
    )
    if isinstance(outcome, str):
        return _deny(f"{outcome} (verifying late-approval grant {approval_id})")
    returncode, out, err = outcome

    detail = ((out or "").strip() or (err or "").strip())[:_DETAIL_CAP]
    if returncode != 0:
        reason = f"tg-ctl ask --verify-grant {approval_id} denied (exit {returncode})"
        return _deny(f"{reason}: {detail}" if detail else reason)

    stdout_text = (out or "").strip()
    if not stdout_text:
        verdict = _ask_forward_verdict(err or "")
        if verdict:
            return _deny(f"tg-ctl ask --verify-grant {approval_id}: {verdict[:_DETAIL_CAP]}")
        reason = f"tg-ctl ask --verify-grant {approval_id} returned no reply"
        return _deny(f"{reason}: {detail}" if detail else reason)

    try:
        reply = json.loads(stdout_text)
    except ValueError:
        return _deny(
            f"tg-ctl ask --verify-grant {approval_id} returned unparseable reply: "
            f"{stdout_text[:_DETAIL_CAP]}"
        )

    original_request = _reply_field(reply, "requestId")
    behavior = _parse_ask_decision(reply)
    if behavior != "allow":
        reason = (
            f"tg-ctl ask --verify-grant {approval_id} decision was {behavior!r} "
            "(expected 'allow')"
        )
        if original_request:
            reason += f" (original request {original_request})"
        daemon_reason = _reply_field(reply, "reason")
        if daemon_reason:
            reason += f": {daemon_reason[:_DETAIL_CAP]}"
        return _deny(reason)
    # GRANT-SHAPE guard (identical strictness to the pull path's): an OLD/fake tg-ctl that
    # ignores `--verify-grant` degrades the call to a plain `ask` and can answer with the
    # GENERIC ask-allow shape. A human tapping Allow on the spurious card must NOT authorize
    # the operation on zero verified grant, so the reply must be demonstrably a grant-consume
    # reply: it carries the ORIGINAL request id AND echoes back the very approval id asked
    # about. Anything else denies (fail closed). Daemon-side contract (sibling tg-cli PR):
    # a verify-allow reply carries requestId + approvalId.
    echoed_approval = _reply_field(reply, "approvalId")
    if not original_request or echoed_approval != approval_id:
        return _deny(
            f"tg-ctl ask --verify-grant {approval_id} replied allow without a grant-consume "
            "shape (no original requestId, or no matching approvalId echo); denied — the "
            "reply does not verifiably consume the named grant"
        )

    reason = f"late-approval grant {approval_id} accepted by tg-ctl ask --verify-grant"
    if original_request:
        reason += f" (original request {original_request})"
    return HatchApprovalResult(
        requested=True,
        approved=True,
        reason=reason,
        env_var=env_var,
        env_present=True,
        tg_ctl_path=str(tg_ctl),
        approval_id=approval_id,
    )


def _pull_late_grant(
    hook_id: str,
    context: Mapping[str, object],
    *,
    cwd: str,
    env_var: str,
    command: str | None,
    env_map: Mapping[str, str],
    tg_ctl_candidates: Sequence[Path | str] | None,
) -> HatchApprovalResult | None:
    """The PULL path (contract v1.2 item 4): no hatch env var / inline value is present, the
    hook would otherwise deny — make ONE bounded (~`_GRANT_LOOKUP_TIMEOUT_S`) query asking the
    daemon to match+consume an unconsumed, unexpired grant for this exact operation and route.

    Returns a RESULT only when the daemon gave a real verdict: an explicit allow (grant
    consumed; reason names approval_id + original requestId) or the DESIGNATED no-stable-route
    deny — a daemon deny whose reason carries the token-path guidance marker
    (`RIG_HATCH_REQUEST_<HOOK>="approved:<id>"`), surfaced VERBATIM so the agent can act on it.
    Returns None for every other outcome: infra failures (tg-ctl missing, launch/timeout/IO
    error, rc!=0, empty/unparseable stdout) AND ordinary no-match / consumed / mismatch denies
    — fail CLOSED with the original deny proceeding UNCHANGED (should_stop stays False, so any
    approval_cmd fallback is not suppressed), identical to today.
    """

    # Ruling 2 (2026-09-12), lib-side belt-and-suspenders: a DISPATCH-BOUND hook must never
    # even QUERY on a hook-only context — a match would consume a grant minted for a
    # different dispatch. Fail closed before the memo, the tg-ctl scan, everything.
    if _dispatch_binding_reason(hook_id, context) is not None:
        return None
    if (hook_id, os.path.abspath(cwd)) in _PULL_NEGATIVE:
        # Already pulled empty for this (hook, cwd) in THIS process (see `_no_grant` below) —
        # one pull per gated command, not one per segment. Checked BEFORE the tg-ctl path scan
        # so repeat consults stay free. Fail-closed skip.
        return None
    tg_ctl = _find_tg_ctl(tg_ctl_candidates)
    if tg_ctl is None:
        return None
    if tg_ctl_candidates is None and os.environ.get("AGENTTOOLS_TEST_HERMETIC_HOME"):
        # TEST SEAM (tests/conftest.py exports this into every test process AND its
        # subprocesses — a subprocess inherits env but NOT monkeypatches): without it, any
        # no-env deny under pytest would spawn the developer's REAL tg-ctl from the hardcoded
        # trusted list. Scoped to the PULL path only (the new fire-on-every-deny exposure);
        # the ask/verify paths keep full resolution so tests can inject fakes via candidates
        # OR a patched `_TRUSTED_TG_CTL_PATHS`. Explicit `candidates` bypass the seam.
        # Fail-CLOSED direction only: it can never approve anything.
        return None

    request_id = f"hatch-pull-{hook_id}-{uuid.uuid4().hex[:12]}"
    question = _question(
        hook_id,
        "Consume a matching belated Telegram approval for this exact operation, if one "
        "exists?",
        _redacted_context(context),
        cwd,
    )
    payload = {
        "requestId": request_id,
        "agent": _ASK_AGENT,
        "kind": "permission",
        "question": question,  # daemon-side only — never posted to Telegram on this path
        "title": f"Hatch grant pull: {hook_id}",
        "decisionLabels": {"allow": "Approve", "deny": "Deny"},
        "hookId": hook_id,
        "operation": _operation_descriptor(hook_id, env_var, context, cwd, command),
        "origin": _origin_context(context, env_map),
        "routeEvidence": _route_evidence(env_map),
    }
    argv = [str(tg_ctl), "ask", "--verify-grant", "--pull", "--hook", hook_id]

    def _no_grant() -> None:
        # Per-PROCESS negative memo: one empty pull per (hook, cwd) per invocation. A hook
        # process is one-shot, so this bounds a multi-segment gated command (block-reset-hard
        # consults the hatch per dangerous segment) to ONE pull instead of one per segment.
        # Fail-closed direction only — a skipped pull can never approve anything.
        _PULL_NEGATIVE.add((hook_id, os.path.abspath(cwd)))

    outcome = _run_tg_ctl_ask(
        argv,
        json.dumps(payload),
        _GRANT_LOOKUP_TIMEOUT_S,
        requested_timeout=_GRANT_LOOKUP_TIMEOUT_S,
    )
    if isinstance(outcome, str):
        _no_grant()
        return None
    returncode, out, _err = outcome
    if returncode != 0:
        _no_grant()
        return None
    stdout_text = (out or "").strip()
    if not stdout_text:
        _no_grant()
        return None
    try:
        reply = json.loads(stdout_text)
    except ValueError:
        _no_grant()
        return None
    behavior = _parse_ask_decision(reply)
    if behavior == "allow":
        approval_id = _reply_field(reply, "approvalId")
        original_request = _reply_field(reply, "requestId")
        if not approval_id or not original_request:
            # NOT a grant-consume reply: an old/fake tg-ctl that ignores
            # `--verify-grant --pull` answers with the generic ask-allow shape (no grant id, no
            # original request id) WITHOUT consuming any grant. Treating that as approval would
            # authorize the operation on nothing (OLD daemon + NEW lib must fail closed).
            _no_grant()
            return None
        reason = (
            f"late-approval grant {approval_id} accepted via automatic pull "
            f"(original request {original_request})"
        )
        return HatchApprovalResult(
            requested=True,
            approved=True,
            reason=reason,
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
            approval_id=approval_id,
            grant_pull=True,
        )
    daemon_reason = _reply_field(reply, "reason")
    if behavior is not None and daemon_reason:
        # Only the designated no-stable-route deny is surfaced (contract v1.2 item 4): its
        # reason points the agent at the explicit token path. Identified structurally when the
        # daemon marks it (`designatedDeny`, the preferred sibling-contract form), else by the
        # canonical guidance JUNCTION `RIG_HATCH_REQUEST_…="approved:<id>` in the reason text —
        # the exact form the contract's wording prescribes for that one deny. An ordinary
        # no-match / consumed / mismatch / expired deny returns None so the caller's original
        # deny path proceeds UNCHANGED (and any approval_cmd fallback is not suppressed).
        designated = _reply_field(reply, "designatedDeny") == "no-stable-route"
        if designated or _TOKEN_PATH_GUIDANCE_RE.search(daemon_reason):
            return HatchApprovalResult(
                requested=True,
                approved=False,
                reason=daemon_reason,
                env_var=env_var,
                env_present=True,
                tg_ctl_path=str(tg_ctl),
            )
    _no_grant()
    return None


def request_hatch_approval(
    hook_id: str,
    context: Mapping[str, object] | None,
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    command: str | None = None,
    tg_ctl_candidates: Sequence[Path | str] | None = None,
    timeout_s: float = DEFAULT_TG_CTL_TIMEOUT_S,
    process_margin_s: float = DEFAULT_PROCESS_MARGIN_S,
    overrides_log_path: Path | str | None = None,
    pull: bool = True,
) -> HatchApprovalResult:
    """Request Telegram approval when this hook's hatch env var carries a real justification.

    SECURITY: the approval binary (`tg-ctl`) is resolved from the account's REAL home
    (`resolve_home`) plus a hardcoded trusted-path list — NEVER from `cwd`, which the guarded
    agent controls. `cwd` here is used ONLY for the human-facing question context (the "CWD:"
    line). This is what stops a guarded agent from committing a repo-local `rig.yaml` with
    `agent_hooks.tg_ctl_path: /tmp/always-exit-0` and self-approving.

    The justification is read from EITHER of two sources, checked in this precedence order:

    1. The hook process environment (`env`, defaulting to `os.environ`) — set when the agent
       harness itself was launched with the var exported. This is the ONLY source available to
       pre-write (Edit/Write) hooks, which have no shell command string to parse.
    2. A leading inline `RIG_HATCH_REQUEST_<HOOK_ID>=<value>` assignment on the Bash `command`
       string (pass `command=` from a pre-bash hook). This is the documented inline form
       (`RIG_HATCH_REQUEST_X="why" <gated-command>`). A pre-bash hook runs in its OWN process
       BEFORE the shell evaluates that `VAR=x cmd` prefix, so the value never reaches the hook's
       `os.environ` — it must be parsed out of the command string that the event carries. Only
       this hook's specific var is honored; arbitrary leading assignments are ignored.

    A process-env value takes precedence over an inline one (an explicit export is the more
    deliberate signal), so passing `command=` is purely additive and never changes the behavior
    of an already-exported var.

    Unset (in both sources) means "hatch not requested". That is when the PULL path fires
    (contract v1.2 item 4): ONE bounded (~15s) `tg-ctl ask --verify-grant --pull` query lets the
    daemon match+consume a late-approval grant for this exact operation and route — a matched
    grant approves, a reasoned daemon deny is surfaced verbatim, and every other outcome returns
    the plain "not requested" result so the original deny proceeds UNCHANGED (fail closed). The
    pull NEVER fires when any hatch env/inline value is present — token paths take precedence.
    `pull=False` opts a call site OUT of the pull: for hooks that consult the hatch at an
    ADVISORY tier (before deciding warn vs block — the pull belongs on would-deny consults
    only, per the contract's wording), so an early-session advisory check does not pay a
    daemon lookup per event.
    A present but blank, bare-flag, or too-short value (a command name like `pytest`) is an
    invalid request: no Telegram call is made and the hook should deny rather than falling
    through to `approval_cmd`.

    Every attempt where the env var carries ANY value (blank, bare-flag, or a real justification —
    i.e. `env_present` ends up `True`) is a hatch USE and is appended, best-effort, to the
    escape-hatch audit sink as one JSON line (`overrides_log_path`, defaulting to
    `<real-home>/.config/agent-tools/overrides.log`). See `_append_overrides_log`.
    """

    env_map = env if env is not None else os.environ
    env_var = hatch_env_var(hook_id)
    ctx = context or {}
    raw = env_map.get(env_var)
    if raw is None and command is not None:
        raw = _parse_inline_hatch_value(env_var, command)
    if raw is None:
        try:
            pull_result = (
                _pull_late_grant(
                    hook_id,
                    ctx,
                    cwd=cwd,
                    env_var=env_var,
                    command=command,
                    env_map=env_map,
                    tg_ctl_candidates=tg_ctl_candidates,
                )
                if pull
                else None
            )
        except Exception:  # noqa: BLE001 - a pull must never fail open or break the deny.
            pull_result = None
        if pull_result is not None:
            # A real daemon verdict (allow, or a reasoned deny to surface) IS a hatch use: audit
            # it like any env-present attempt (justification field stays empty — no hatch value
            # was set; the verdict lives in `detail`).
            _append_overrides_log(
                hook_id,
                env_var,
                pull_result,
                command=command,
                cwd=cwd,
                justification="",
                context=ctx,
                env_map=env_map,
                overrides_log_path=overrides_log_path,
            )
            return pull_result
        return HatchApprovalResult(
            requested=False,
            approved=False,
            reason=f"{env_var} is not set; Telegram hatch escalation not requested",
            env_var=env_var,
            env_present=False,
        )
    try:
        result = _request_present_hatch_approval(
            hook_id,
            ctx,
            cwd=cwd,
            env_var=env_var,
            raw=raw,
            command=command,
            env_map=env_map,
            tg_ctl_candidates=tg_ctl_candidates,
            timeout_s=timeout_s,
            process_margin_s=process_margin_s,
        )
    except Exception as exc:  # noqa: BLE001 - a hatch request must never fail open.
        result = HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"Telegram hatch escalation errored: {exc}",
            env_var=env_var,
            env_present=True,
        )
    _append_overrides_log(
        hook_id,
        env_var,
        result,
        command=command,
        cwd=cwd,
        justification=raw,
        context=ctx,
        env_map=env_map,
        overrides_log_path=overrides_log_path,
    )
    return result


def _request_present_hatch_approval(
    hook_id: str,
    context: Mapping[str, object],
    *,
    cwd: str,
    env_var: str,
    raw: str,
    command: str | None,
    env_map: Mapping[str, str],
    tg_ctl_candidates: Sequence[Path | str] | None,
    timeout_s: float,
    process_margin_s: float,
) -> HatchApprovalResult:
    # Ruling 2 (2026-09-12), lib-side belt-and-suspenders: a DISPATCH-BOUND hook's grant path
    # must NEVER proceed at hook-only granularity — deny BEFORE grading the value or resolving
    # tg-ctl, so neither an ask is minted nor a grant verified under weak binding. Flows
    # through the caller's normal audit path (a weak-binding attempt is a hatch USE).
    binding_reason = _dispatch_binding_reason(hook_id, context)
    if binding_reason is not None:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=binding_reason,
            env_var=env_var,
            env_present=True,
        )
    justification = raw.strip()
    if not justification:
        return HatchApprovalResult(
            requested=False,
            approved=False,
            reason=f"{env_var} is blank; Telegram hatch escalation denied",
            env_var=env_var,
            env_present=True,
        )
    # Late-Approval Grant Protocol (module docstring): an `approved:<id>` value is NOT a
    # written justification — it routes to the daemon's validate+consume path, BEFORE the
    # `_is_written_justification` gate below would reject it as a stub.
    grant_match = _APPROVED_GRANT_RE.match(justification)
    if grant_match is not None:
        return _verify_hatch_grant(
            hook_id,
            grant_match.group(1),
            context,
            cwd=cwd,
            env_var=env_var,
            command=command,
            env_map=env_map,
            tg_ctl_candidates=tg_ctl_candidates,
        )
    if justification.startswith("approved:") and not any(c.isspace() for c in justification):
        # Matched nothing above → a whitespace-free `approved:…` blob is unmistakably a fumbled
        # grant reference. Deny LOCALLY: do not post a new Telegram ask whose "justification"
        # is a garbage token. A value with SPACES ("approved: per Alex tg#123, safe to re-run")
        # is a real sentence that merely starts with the word — it falls through to the normal
        # justification grading below.
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=(
                f"{env_var} carries a malformed late-approval grant reference (expected "
                "approved:<id>, [A-Za-z0-9-]+); denied without contacting tg-ctl"
            ),
            env_var=env_var,
            env_present=True,
        )
    if not _is_written_justification(justification):
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=(
                f"{env_var} needs a written justification, not {justification!r}"
            ),
            env_var=env_var,
            env_present=True,
        )

    tg_ctl = _find_tg_ctl(tg_ctl_candidates)
    if tg_ctl is None:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason="tg-ctl is not available at a trusted executable path",
            env_var=env_var,
            env_present=True,
        )

    effective_timeout = _bounded_timeout(timeout_s)
    question = _question(hook_id, justification, _redacted_context(context), cwd)
    # `tg-ctl ask` is the internal hook client documented in its own usage text as
    # "reads a ButtonRequest JSON from stdin" — it is NOT a generic "ask a plain-text
    # question" CLI. It takes no positional question argument and no `--timeout` flag
    # (verified against features/tg-ctl/hook-normalize.ts and tg-ctl's own argv
    # parsing, which only recognises `--agent`); its request/response protocol is a
    # single JSON object on stdin and a JSON reply on stdout. `normalizeHookPayload`
    # has an explicit "already a normalized ButtonRequest (back-compat / manual
    # callers)" branch (hook-normalize.ts) that trusts a payload carrying `requestId`
    # + `question` + `kind` directly — that is the sanctioned path for a manual
    # caller like this one, not synthesizing a fake harness hook_event payload.
    #
    # The daemon ask window: the NEW grant-aware daemon (sibling tg-cli PR, agent-tools#742)
    # honors the per-ask `timeoutMs` sent below (capped at 15 min); the DEPLOYED daemon
    # hard-codes 115s (ASK_TOTAL_TIMEOUT_MS in tg-ctl) and ignores `timeoutMs` — until it
    # ships, the effective window is whichever daemon is installed. `effective_timeout` still
    # bounds how long THIS process waits for tg-ctl to give up and exit.
    request_id = f"hatch-{hook_id}-{uuid.uuid4().hex[:12]}"
    button_request = {
        "requestId": request_id,
        "agent": _ASK_AGENT,
        "kind": "permission",
        "question": question,
        "title": f"Hatch: {hook_id}",
        "decisionLabels": {"allow": "Approve", "deny": "Deny"},
        # Late-Approval Grant Protocol fields (agent-tools#742): the daemon keys its grant
        # store on hookId + the operation fingerprint, honors timeoutMs as the ask window, and
        # routes a belated-approval message back via origin. `operation` binds any grant to
        # THIS operation only.
        "hookId": hook_id,
        "timeoutMs": _ASK_TIMEOUT_MS,
        "operation": _operation_descriptor(hook_id, env_var, context, cwd, command),
        "origin": _origin_context(context, env_map),
        "routeEvidence": _route_evidence(env_map),
    }
    argv = [str(tg_ctl), "ask", "--agent", _ASK_AGENT]
    proc_timeout = effective_timeout + max(process_margin_s, 0.0)

    def _deny(reason: str) -> HatchApprovalResult:
        # Every deny below differs ONLY in its reason; one constructor keeps the
        # requested/env_present/tg_ctl_path triple from drifting between copies.
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=reason,
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )

    outcome = _run_tg_ctl_ask(
        argv, json.dumps(button_request), proc_timeout, requested_timeout=effective_timeout
    )
    if isinstance(outcome, str):
        return _deny(outcome)
    returncode, out, err = outcome

    detail = ((out or "").strip() or (err or "").strip())[:_DETAIL_CAP]

    # SECURITY: `tg-ctl ask` exits 0 unconditionally (its own dispatcher does
    # `process.exit(0)` after `askDaemon()` regardless of outcome) — a clean exit is
    # NOT evidence of approval. A declined, timed-out, or unreachable-daemon request
    # ALSO exits 0 with empty stdout (askDaemon returns early and only logs to
    # stderr). The only trustworthy signal is a well-formed hookSpecificOutput reply
    # on stdout whose decision is explicitly "allow" — anything else (empty output,
    # unparseable JSON, an explicit "deny", a nonzero exit) must deny. This was
    # previously inverted (nonzero exit -> deny, EVERYTHING else -> approve), which
    # silently self-approved on stdin/argv protocol mismatches with zero Telegram
    # round trip (found via ~/.config/tg-cli/tg-ctl.*.log showing no daemon activity
    # at the "approved" timestamps in ~/.config/agent-tools/ship-audit.jsonl).
    if returncode != 0:
        reason = f"tg-ctl ask denied (exit {returncode})"
        return _deny(f"{reason}: {detail}" if detail else reason)

    stdout_text = (out or "").strip()
    if not stdout_text:
        verdict = _ask_forward_verdict(err or "")
        if verdict:
            # The client's own outcome line is the truth (tg-cli#317): a card that was POSTED
            # and then expired must reach the agent as exactly that, never as "no reply".
            if verdict.startswith("ask-forward expired"):
                # An EXPIRED card is exactly the case a belated approval can still land for —
                # append the late-approval retry contract (module docstring).
                return _deny(
                    f"tg-ctl ask: {verdict[:_DETAIL_CAP]}\n{_LATE_APPROVAL_RETRY_HINT}"
                )
            return _deny(f"tg-ctl ask: {verdict[:_DETAIL_CAP]}")
        reason = "tg-ctl ask returned no reply (declined, timed out, or daemon unreachable)"
        return _deny(f"{reason}: {detail}" if detail else reason)

    try:
        reply = json.loads(stdout_text)
    except ValueError:  # json.JSONDecodeError is a ValueError; loads(str) can raise nothing else
        return _deny(f"tg-ctl ask returned unparseable reply: {stdout_text[:_DETAIL_CAP]}")

    behavior = _parse_ask_decision(reply)
    if behavior != "allow":
        return _deny(f"tg-ctl ask decision was {behavior!r} (expected 'allow')")

    return HatchApprovalResult(
        requested=True,
        approved=True,
        reason=f"approved by tg-ctl ask (request {request_id})",
        env_var=env_var,
        env_present=True,
        tg_ctl_path=str(tg_ctl),
    )


# `tg-ctl ask` logs ONE outcome line for a request that produced no usable reply (tg-cli#317):
# `ask-forward expired: … messageId=N — posted to Telegram; no decision within …`,
# `ask-forward not-posted: … daemon declined: reason=not-registered`, `ask-forward timed-out: …`,
# `ask-forward unreachable: …`. It is what the asking agent must be told, verbatim.
# Any client-side outcome kind (`expired`, `timed-out`, `not-posted`, `unreachable`, a future
# one) is forwarded as-is — the daemon-side progress lines (`arrived`, `posted`, `replayed-
# answer`, …) never reach the client's stderr, so no exclusion list is needed (review note).
_ASK_FORWARD_VERDICT_RE = re.compile(r"ask-forward [a-z][a-z-]*(?: [a-z-]+)*:[^\n]*")


def _ask_forward_verdict(stderr_text: str) -> str | None:
    """The LAST `ask-forward …` outcome line on tg-ctl's stderr, without the timestamp."""
    matches = _ASK_FORWARD_VERDICT_RE.findall(stderr_text)
    return matches[-1].strip() if matches else None


def _run_tg_ctl_ask(
    argv: list[str], stdin_payload: str, proc_timeout: float, *, requested_timeout: float
) -> tuple[int, str, str] | str:
    """Run `tg-ctl ask` with the ButtonRequest on stdin. Returns (returncode, stdout,
    stderr) on completion, or a deny REASON string when the process could not be run
    to completion (launch failure, timeout, I/O error) — the caller treats either as
    untrusted input, never as approval. `proc_timeout` is the padded wait actually
    enforced; `requested_timeout` is the unpadded budget quoted in the timeout reason."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv[0] is an absolute, executable tg-ctl path
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return f"tg-ctl failed to launch: {exc}"
    try:
        out, err = proc.communicate(input=stdin_payload, timeout=proc_timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        return f"tg-ctl ask timed out after {requested_timeout:.0f}s"
    except (OSError, ValueError) as exc:
        _kill_process_group(proc)
        return f"tg-ctl ask errored: {exc}"
    return proc.returncode, out, err


def _parse_ask_decision(reply: object) -> str | None:
    """Extract the decision string from a parsed `tg-ctl ask` reply, or None when the
    reply carries no recognisable decision. Two reply shapes exist (tg-cli's
    features/tg-ctl/questions.ts emits one or the other depending on the hook event
    the ButtonRequest names):

      PreToolUse:        {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "allow"|"deny"}}
      PermissionRequest: {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                 "decision": {"behavior": "allow"|"deny"}}}

    The latter is the default for a manual/back-compat ButtonRequest like ours. Only the
    literal string "allow" ever approves; the caller compares, this only extracts."""
    if not isinstance(reply, dict):
        return None
    hook_output = reply.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        return None
    permission_decision = hook_output.get("permissionDecision")
    if isinstance(permission_decision, str):
        return permission_decision
    decision = hook_output.get("decision")
    if isinstance(decision, dict) and isinstance(decision.get("behavior"), str):
        return decision["behavior"]
    return None


def _append_overrides_log(
    hook_id: str,
    env_var: str,
    result: HatchApprovalResult,
    *,
    command: str | None,
    cwd: str,
    justification: str,
    context: Mapping[str, object],
    env_map: Mapping[str, str],
    overrides_log_path: Path | str | None,
) -> None:
    """Best-effort append of one JSON line recording this hatch USE to the audit sink (gap G-8,
    retrospective 2026-07-01 section 5.2.3 item 3: "every escape-hatch use appends {ts, session,
    hatch, command, reason} to overrides.log"). Called for every attempt where the env var carried
    ANY value — blank, bare-flag, denied, or approved — because each of those is a real use of the
    mechanism, not only a successful bypass (a denied/blank attempt is exactly the kind of pressure
    a hidden hatch would otherwise mask). MUST NEVER raise or block the caller: an audit-log
    failure (disk full, read-only home, permissions) must not become a hatch outage, mirroring
    `ci/ship/ship.sh`'s own `_review_quorum_audit_log` best-effort contract.
    """

    try:
        path = _resolve_overrides_log_path(overrides_log_path)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # `approved-late`: a late-approval grant was consumed via the `approved:<id>`
        # token path; `approved-late-pull`: via the automatic pull query. Both carry the
        # grant id in `approval_id` below.
        if result.approved and result.grant_pull:
            decision = "approved-late-pull"
        elif result.approved and result.approval_id:
            decision = "approved-late"
        elif result.approved:
            decision = "approved"
        else:
            decision = "denied"
        entry = {
            "ts": _utc_now_iso(),
            "session": _resolve_session_id(context, env_map),
            "hatch": hook_id,
            "env_var": env_var,
            # Fall back to context["command"] when the caller didn't also pass the command=
            # kwarg — most hook call sites pass both (redundantly), but a caller that only
            # threads it through `context` (e.g. a future hook) must not ship a blank audit
            # `command` field just because the kwarg was omitted.
            "command": str(command or context.get("command") or "")[:_DETAIL_CAP],
            "reason": justification.strip()[:_DETAIL_CAP],
            "decision": decision,
            "detail": result.reason[:_DETAIL_CAP],
            "cwd": cwd,
        }
        if result.approval_id:
            entry["approval_id"] = result.approval_id
        _append_json_line_0600(path, entry)
    except Exception:  # noqa: BLE001 - audit logging must never break the hatch flow.
        return


def _append_json_line_0600(path: Path, entry: dict[str, object]) -> None:
    """Append one JSON line to `path`, forcing owner-only `0600` permissions — the audit line can
    carry a free-text justification and a full shell command, so it deserves the same privacy
    posture `lib/agenttools_log` already applies to its own file sink. `O_APPEND` makes the write
    itself atomic against concurrent appenders (multiple hooks/hatches writing the same file);
    `fchmod` re-tightens permissions on a PRE-EXISTING file too, not only one this call creates."""

    line = (json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        _write_all(fd, line)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """`os.write` is not guaranteed to write the whole buffer in one call (POSIX allows a short
    write, e.g. on a full disk or a signal interrupt) — loop until every byte is written rather
    than assuming one call suffices, even though a single audit line is always small. A `0`
    return is also POSIX-legal (distinct from a short write) and would otherwise spin the loop
    forever without shrinking `view`; raise so the caller's best-effort `except Exception`
    swallows it like any other audit-log failure instead of hanging the hook."""

    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("os.write returned a non-positive byte count")
        view = view[written:]


def _resolve_overrides_log_path(explicit: Path | str | None) -> Path | None:
    """The overrides.log path, checked in this order:

    1. `explicit` (the `overrides_log_path=` kwarg — tests / advanced callers).
    2. the `AGENT_TOOLS_OVERRIDES_LOG` env var (a full file path), mirroring
       `ci/ship/ship.sh`'s own `SHIP_AUDIT_FILE` override for its ship-specific audit log — the
       SAME precedent, same trust level. This tier exists for hook scripts invoked as a fresh
       SUBPROCESS (every `agent-hooks/*` script, run by the real harness): a subprocess gets a
       brand-new interpreter, so an in-process `monkeypatch.setattr(resolve_home, ...)` never
       reaches it, but an exported env var does. `tests/conftest.py`'s autouse hermetic-home
       fixture exports it for exactly this reason.
    3. `<real-home>/.config/agent-tools/overrides.log`, `resolve_home()` — the SAME
       non-agent-controllable trust anchor used to resolve `tg_ctl_path`.

    SECURITY NOTE: unlike `tg_ctl_path`, tier 2 here IS environment-controllable — an agent could
    in principle redirect its own audit line. This is an accepted, precedented trade-off (see
    `SHIP_AUDIT_FILE` above): the audit sink is a best-effort SECONDARY record, never the
    enforcement mechanism itself — the live `tg-ctl ask` round trip in
    `_request_present_hatch_approval` is what actually gates the bypass, and an agent cannot
    redirect or suppress THAT. Returns None when neither an override applies nor `resolve_home()`
    resolves (fail-quiet: skip the write rather than fall back to an untrusted anchor).
    """

    if explicit is not None:
        return Path(explicit)
    env_override = os.environ.get(_OVERRIDES_LOG_ENV)
    if env_override:
        return Path(env_override)
    home = resolve_home()
    if home is None:
        return None
    return Path(home) / _OVERRIDES_LOG_RELATIVE


def default_overrides_log_path() -> Path | None:
    """Public accessor for the escape-hatch audit sink path a caller with no explicit
    `overrides_log_path=` would actually write to, for consumers other than
    `request_hatch_approval` itself (e.g. a future `rig status` "overrides this week" section or a
    weekly tg digest, retrospective 5.2.3 item 3). This is the SAME tier-2/tier-3 resolution
    `_resolve_overrides_log_path` uses: honors `AGENT_TOOLS_OVERRIDES_LOG` when set, else falls
    back to `resolve_home()`-rooted. Returns None only when neither tier resolves (no env override
    AND `resolve_home()` is None)."""

    return _resolve_overrides_log_path(None)


def _resolve_session_id(
    context: Mapping[str, object], env_map: Mapping[str, str]
) -> str:
    """Best-effort session/agent identifier for the audit line.

    No existing call site threads a real session id into `context` today (every hook passes only
    `{"hook": ..., "command": ...}`-shaped dicts), so this checks a `session_id`/`session` context
    key first (future callers can supply a precise value with zero changes here), then a short
    list of plausible harness-exported env vars, then falls back to `pid:<getpid()>` — the field is
    NEVER empty, because a session-less audit line is still worth having.
    """

    for key in ("session_id", "session"):
        value = context.get(key)
        if value:
            return str(value)
    for var in _SESSION_ID_ENV_VARS:
        value = env_map.get(var)
        if value:
            return value
    return f"pid:{os.getpid()}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded_timeout(timeout_s: float) -> float:
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        return DEFAULT_TG_CTL_TIMEOUT_S
    if value <= 0:
        return DEFAULT_TG_CTL_TIMEOUT_S
    return min(value, MAX_TG_CTL_TIMEOUT_S)


def resolve_home() -> str | None:
    """The account's REAL home directory, from the OS identity (`pwd.getpwuid`) — deliberately
    NOT `$HOME` and NOT the repo the guarded agent is acting in. Returns None when the OS account
    home cannot be resolved (e.g. a no-passwd container).

    This is the trust anchor for resolving the approval binary (see `_candidate_paths`): a
    `rig.yaml agent_hooks.tg_ctl_path` override is honored ONLY from this home, never from the
    agent-controlled working directory. Keying off `getpwuid` (not the environment) means a
    guarded agent cannot redirect the approval binary by committing a repo-local `rig.yaml`, by
    running the hook from a doctored `cwd`, or by exporting `HOME` — which would otherwise
    reopen the exact self-service bypass this hatch exists to close (mirrors the ship
    review-quorum gate's `resolve_home`, PR #225).

    It deliberately does NOT fall back to `os.path.expanduser("~")` on failure: that would read
    `$HOME` (agent-exportable) or, if `$HOME` is unset, resolve `~` relative to the (agent-
    controlled) cwd and walk up into the repo — exactly the untrusted anchors this exists to
    exclude. Returning None makes the caller SKIP the rig.yaml override entirely (fail closed to
    the hardcoded trusted paths). Overridable in tests via monkeypatch (never via the environment)."""

    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return None


def _find_tg_ctl(candidates: Sequence[Path | str] | None) -> Path | None:
    seen: set[Path] = set()
    for candidate in _candidate_paths(candidates):
        resolved = _resolve_executable(candidate)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        return resolved
    return None


def _candidate_paths(candidates: Sequence[Path | str] | None) -> list[Path | str]:
    out: list[Path | str] = []
    # SECURITY: the rig.yaml `tg_ctl_path` override is read from the account's REAL home, NEVER
    # from the (agent-controlled) working directory the hook was invoked in — otherwise a guarded
    # agent could commit `rig.yaml` with `tg_ctl_path: /tmp/always-exit-0` and self-approve. When
    # the home can't be resolved (`resolve_home()` is None) the override is SKIPPED entirely (fail
    # closed to the hardcoded trusted paths) rather than falling back to an $HOME/cwd anchor.
    home = resolve_home()
    rig_path = _rig_tg_ctl_path(home) if home else None
    if rig_path is not None:
        out.append(rig_path)
    out.extend(_TRUSTED_TG_CTL_PATHS if candidates is None else candidates)
    return out


def _resolve_executable(candidate: Path | str) -> Path | None:
    # SECURITY: a candidate MUST be a genuine absolute path. Do NOT `expanduser()` it — that would
    # expand a `~/…` value (e.g. a home rig.yaml `tg_ctl_path: "~/bin/tg-ctl"`) against the
    # agent-exportable `$HOME`, reintroducing an agent-controllable anchor. A `~`-prefixed or
    # otherwise non-absolute path is rejected outright.
    path = Path(str(candidate))
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _rig_tg_ctl_path(home: str) -> str | None:
    """The `agent_hooks.tg_ctl_path` override read from EXACTLY `<home>/rig.yaml`.

    SECURITY: this reads only the home dir's own rig.yaml — it deliberately does NOT walk UP into
    parent directories. If the account home is nested under a workspace (or any other
    agent-controlled dir), a parent `rig.yaml` must NOT be able to redirect the approval binary.
    """

    try:
        text = (Path(home) / "rig.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    value = _agent_hooks_raw(text, _RIG_TG_CTL_KEY)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _agent_hooks_raw(rig_yaml_text: str, key: str) -> str | None:
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


def _question(
    hook_id: str,
    justification: str,
    context: Mapping[str, object],
    cwd: str,
) -> str:
    lines = [
        "Approve this one-time agent-hook hatch request?",
        f"Hook: {hook_id}",
        f"Justification: {justification}",
        f"CWD: {cwd}",
    ]
    for key in sorted(context):
        value = context[key]
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_context_value(value)}")
    return "\n".join(lines)


def _context_value(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > 1000:
        return f"{text[:1000]}..."
    return text


def _kill_process_group(proc: subprocess.Popen) -> None:
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


__all__ = [
    "DEFAULT_TG_CTL_TIMEOUT_S",
    "HatchApprovalResult",
    "MAX_TG_CTL_TIMEOUT_S",
    "default_overrides_log_path",
    "hatch_dispatch_op_fields",
    "hatch_env_var",
    "hatch_write_op_fields",
    "request_hatch_approval",
    "resolve_home",
]
