"""Shared Telegram hatch escalation helper for agent-hook scripts.

The helper is intentionally small and stdlib-only: hook scripts import it directly from the
agent-tools checkout, before package installation can be assumed. It turns a per-hook env var
(`RIG_HATCH_REQUEST_<HOOK_ID>`) into a one-time `tg-ctl ask` call through a trusted absolute
path. It never consults ambient PATH.

It also owns the machine-trust primitives other `python3 -I` gate helpers load by fixed path
(`ci/ship/rig_external_review_policy.py`): `path_trust_problem` / `resolve_trusted_file` (is a
path owned by you or root and writable by nobody else, on macOS, Linux and Cygwin alike) and
`machine_config_mapping` (a PyYAML-free reader for the machine-global rig config, because an
isolated `python3 -I` interpreter never sees a user-site PyYAML).

Every attempt where the hatch env var carried ANY value is also appended, best-effort, as one
JSON line to `overrides.log` (retrospective 2026-07-01, section 5.2.3 item 3 / gap G-8: "escape
hatches have no audit sink"). See `_append_overrides_log` for the record shape and trust model.
"""

from __future__ import annotations

import functools
import grp
import hashlib
import html
import json
import os
import pwd
import re
import shlex
import signal
import stat
import subprocess  # noqa: S404 - runs an already-resolved absolute tg-ctl path
import sys
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
_BARE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})
# Long enough to reject a command name used as the value (`pytest`) and
# short enough that a real phrase still qualifies.
_MIN_JUSTIFICATION_CHARS = 8

# Late-approval grant token minted by tg-ctl after an approval arrives after the
# original ask has already expired. This exact format is the cross-repo contract
# with tg-cli's features/tg-ctl/grants.ts.
_APPROVED_GRANT_RE = re.compile(r"^approved:(hgr-[0-9a-f]{12})$")
_GRANT_LOOKUP_TIMEOUT_S = 15.0


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


# tg-ctl install locations searched when the machine-global rig config names no explicit
# `tg_ctl.tg_ctl_path`. Home-relative dirs are joined onto the account's REAL home
# (`resolve_home`), never `$HOME`: `~/.local/bin` (XDG user bin, the tg-cli installer's target on
# Linux), `~/bin`, `~/.files/bin` (rig's own `DEFAULT_TG_CTL_PATH`), `~/.linuxbrew/bin`
# (per-user Linuxbrew).
_HOME_TG_CTL_DIRS = (
    Path(".local") / "bin",
    Path("bin"),
    Path(".files") / "bin",
    Path(".linuxbrew") / "bin",
)
# System-wide prefixes: Apple-silicon Homebrew, Intel Homebrew / manual installs (also Cygwin's
# `/usr/local/bin`), shared Linuxbrew, distro packages.
_TRUSTED_TG_CTL_PATHS = (
    Path("/opt/homebrew/bin/tg-ctl"),
    Path("/usr/local/bin/tg-ctl"),
    Path("/home/linuxbrew/.linuxbrew/bin/tg-ctl"),
    Path("/usr/bin/tg-ctl"),
)
_TG_CTL_NAME = "tg-ctl"
# The machine-global rig config, joined onto the REAL home. `$XDG_CONFIG_HOME` is deliberately NOT
# honored here (unlike rig itself): it is an environment variable the guarded agent can export, so
# it would let the agent point the approval binary at a config it wrote.
_RIG_GLOBAL_CONFIG_RELATIVE = Path(".config") / "rig" / "config.yaml"
# rig's own key for "where tg-ctl lives" (riglib/tg_ctl.py) — the same value rig installs the
# tg-ctl daemon from, so the hatch and the daemon can never disagree on which binary is tg-ctl.
_RIG_TG_CTL_BLOCK = "tg_ctl"
_RIG_TG_CTL_KEY = "tg_ctl_path"
_MAX_SYMLINK_HOPS = 40
_IS_CYGWIN = sys.platform == "cygwin"
_IS_DARWIN = sys.platform == "darwin"
# Cygwin maps the Windows SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544) principals to these
# ids; files installed by an elevated installer are owned by / group-writable for them.
_CYGWIN_TRUSTED_IDS = frozenset({18, 544})
# macOS `admin`: every member can already sudo, so its group-write grants nothing new.
_DARWIN_ADMIN_GID = 80
_DEFAULT_CYGDRIVE_PREFIX = "/cygdrive"
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]")

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
    """Result of checking one hook's Telegram hatch request."""

    requested: bool
    approved: bool
    reason: str
    env_var: str
    env_present: bool = False
    tg_ctl_path: str | None = None
    # Echoes the `critical=` kwarg the caller passed to `request_hatch_approval` (agent-tools#754
    # / tg-cli#403). Purely for caller introspection/logging — the daemon round-trip only needs
    # `critical` on the OUTBOUND ButtonRequest JSON (set in `_request_present_hatch_approval`
    # below), never on this inbound result, so nothing reads this field today. Defaults to `False`
    # so every pre-existing construction of this frozen dataclass (positional or keyword) stays
    # valid with zero call-site changes.
    critical: bool = False

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


def _split_command_segments(command: str) -> list[list[str]] | None:
    """Tokenize `command` quote-aware and split it into per-simple-command token lists at the
    shell control operators that begin a new command. Returns None if it cannot be tokenized.

    Uses `shlex` with `punctuation_chars` so `;`/`&`/`|` (and their runs `&&`/`||`/`|&`) are
    recognized as operators only OUTSIDE quotes — a separator inside a quoted argument stays part
    of that token. A `\\`-newline line continuation is first folded to a space, and a remaining
    bare newline (a real shell separates commands on it, same as `;`) is normalized to `;`;
    shlex still keeps a newline inside quotes literal, so neither normalization can split a
    quoted argument into a spurious leading assignment.
    """

    normalized = command.replace("\\\n", " ").replace("\n", ";")
    lexer = shlex.shlex(
        normalized, posix=True, punctuation_chars="".join(_SEPARATOR_CHARS)
    )
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
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
    critical: bool = False,
) -> HatchApprovalResult:
    """Request Telegram approval when this hook's hatch env var carries a real justification.

    SECURITY: the approval binary (`tg-ctl`) is resolved from the account's REAL home
    (`resolve_home`: the machine-global rig config's `tg_ctl.tg_ctl_path`, then the home-relative
    and system install dirs) — NEVER from `cwd`, `$HOME`, `$XDG_CONFIG_HOME` or PATH, and every
    candidate must pass `resolve_trusted_file` (owned by you or root, writable by nobody else)
    and lie outside the git checkout the guarded agent works in. `cwd` is otherwise used ONLY for
    the human-facing question context (the "CWD:" line). This is what stops a guarded agent from
    committing a repo-local config or binary and self-approving. The boundary is REPO content
    and OTHER accounts: an agent running as the same uid whose writes are not confined to its
    worktree can overwrite a home-dir tg-ctl like any other file it owns — confining those writes
    is the job of the write hooks (`worktree-only-writes`, `no-shell-file-edit`), not this lookup.

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

    Unset (in both sources) means "hatch not requested" and does not block later mechanisms. A
    present but blank, bare-flag, or too-short value (a command name like `pytest`) is an
    invalid request: no Telegram call is made and the hook should deny rather than falling
    through to `approval_cmd`.

    `critical` (agent-tools#754, companion to tg-cli#403's daemon-side "critical/no-timeout hatch
    approval mode"): opt-in, defaults `False`. When `True`, this call is for a decision Alex
    genuinely cannot lose to a client-side clock — the live incident that motivated this ticket
    was a hatch approval tapped AFTER the calling process had already given up waiting on its own
    bounded budget, so the tap landed on a process that no longer existed. Setting `critical=True`:

      - Removes the LOCAL (this-process) wait bound entirely — see `_request_present_hatch_
        approval` for the `_bounded_timeout`/`MAX_TG_CTL_TIMEOUT_S` bypass detail.
      - Adds `"critical": true` to the outbound `tg-ctl ask` ButtonRequest JSON, which is what
        tells the tg-ctl daemon (tg-cli#403) to also drop ITS OWN `ASK_TOTAL_TIMEOUT_MS` bound for
        this specific ask, schedule quiet-hours-aware reminder pings while it stays pending, and
        proactively push a late decision into the caller's live agent session instead of leaving
        it only passively retry-discoverable.

    Every existing call site that does not pass `critical=` is completely unaffected: default
    `False` keeps `_bounded_timeout`/`MAX_TG_CTL_TIMEOUT_S`/the padded subprocess wait exactly as
    they were before this kwarg existed — this is a strictly additive, opt-in mode.

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
            pull_result = _pull_late_grant(
                hook_id, ctx, cwd=cwd, env_var=env_var, command=command, env_map=env_map,
                tg_ctl_candidates=tg_ctl_candidates, critical=critical,
            )
        except Exception:
            pull_result = None
        if pull_result is not None:
            _append_overrides_log(
                hook_id, env_var, pull_result, command=command, cwd=cwd, justification="",
                context=ctx, env_map=env_map, overrides_log_path=overrides_log_path,
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
            tg_ctl_candidates=tg_ctl_candidates,
            timeout_s=timeout_s,
            process_margin_s=process_margin_s,
            critical=critical,
            env_map=env_map,
        )
    except Exception as exc:  # noqa: BLE001 - a hatch request must never fail open.
        result = HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"Telegram hatch escalation errored: {exc}",
            env_var=env_var,
            env_present=True,
            critical=critical,
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


def _operation_fingerprint(
    hook_id: str,
    context: Mapping[str, object],
    *,
    env_var: str | None = None,
    command: str | None = None,
) -> str:
    """Deterministic identity for the OPERATION a hatch ask is about (tg-cli#398's late-approval
    grant protocol): `hookId` + this fingerprint is what `tg-ctl`'s `isGrantableRequest` needs to
    mint a grant when a human taps Approve after the request itself has already expired.

    Deliberately excludes the free-text justification: two asks for the SAME operation (a retry
    with a differently-worded reason) must fingerprint identically, or a late approval could never
    match the retried request. `context` is dumped with `sort_keys=True` so the same `hook_id` +
    `context` always hashes the same across separate Python process invocations (each hook run is
    a fresh interpreter — there is no in-memory state to carry this identity another way).
    """
    stable_context = dict(context)
    if env_var is not None and command is not None:
        canonical = _canonical_bash_fingerprint(env_var, command)
        if "command" in stable_context:
            stable_context["command"] = canonical
        else:
            stable_context["__command__"] = canonical
    return hashlib.sha256(
        "\x00".join([hook_id, json.dumps(stable_context, sort_keys=True, default=str)]).encode(
            "utf-8", "surrogatepass"
        )
    ).hexdigest()



def _grant_operation(
    hook_id: str,
    env_var: str,
    context: Mapping[str, object],
    cwd: str,
    command: str | None,
) -> dict[str, str]:
    return {
        "kind": hook_id,
        "fingerprint": _operation_fingerprint(
            hook_id, context, env_var=env_var, command=command
        ),
        "cwd": cwd,
    }


def _grant_reply_field(reply: object, key: str) -> str | None:
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


def _grant_payload(
    hook_id: str,
    env_var: str,
    context: Mapping[str, object],
    cwd: str,
    command: str | None,
    env_map: Mapping[str, str],
    *,
    request_id: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "requestId": request_id,
        "agent": _ASK_AGENT,
        "kind": "permission",
        "question": f"Verify a previously approved hatch grant for {hook_id}",
        "title": f"Hatch grant: {hook_id}",
        "decisionLabels": {"allow": "Approve", "deny": "Deny"},
        "cwd": cwd,
        "hookId": hook_id,
        "operation": _grant_operation(hook_id, env_var, context, cwd, command),
    }
    pane_id = env_map.get("TMUX_PANE")
    if pane_id:
        payload["paneId"] = pane_id
    return payload


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
    critical: bool,
) -> HatchApprovalResult:
    lookup = _find_tg_ctl(tg_ctl_candidates, cwd)
    tg_ctl = lookup.path

    def deny(reason: str) -> HatchApprovalResult:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=reason,
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl) if tg_ctl is not None else None,
            critical=critical,
        )

    if tg_ctl is None:
        return deny(
            lookup.unavailable_reason("tg-ctl is not available; late-approval grant cannot be verified")
        )

    payload = _grant_payload(
        hook_id, env_var, context, cwd, command, env_map,
        request_id=f"hatch-verify-{hook_id}-{uuid.uuid4().hex[:12]}",
    )
    outcome = _run_tg_ctl_ask(
        [str(tg_ctl), "ask", "--verify-grant", approval_id, "--hook", hook_id],
        json.dumps(payload),
        _GRANT_LOOKUP_TIMEOUT_S,
        requested_timeout=_GRANT_LOOKUP_TIMEOUT_S,
    )
    if isinstance(outcome, str):
        return deny(outcome)
    returncode, out, err = outcome
    if returncode != 0:
        return deny(f"tg-ctl grant verification failed (exit {returncode})")
    stdout_text = (out or "").strip()
    if not stdout_text:
        return deny("tg-ctl grant verification returned no reply")
    try:
        reply = json.loads(stdout_text)
    except ValueError:
        return deny("tg-ctl grant verification returned an invalid reply")
    if _parse_ask_decision(reply) != "allow":
        return deny("tg-ctl rejected the late-approval grant")
    if _grant_reply_field(reply, "approvalId") != approval_id:
        return deny("tg-ctl allow reply did not confirm the requested grant id")
    original_request = _grant_reply_field(reply, "requestId")
    if not original_request:
        return deny("tg-ctl allow reply did not identify the original request")
    return HatchApprovalResult(
        requested=True,
        approved=True,
        reason=f"late-approval grant {approval_id} consumed (request {original_request})",
        env_var=env_var,
        env_present=True,
        tg_ctl_path=str(tg_ctl),
        critical=critical,
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
    critical: bool,
) -> HatchApprovalResult | None:
    if tg_ctl_candidates is None and os.environ.get("AGENTTOOLS_TEST_HERMETIC_HOME"):
        return None
    tg_ctl = _find_tg_ctl(tg_ctl_candidates, cwd).path
    if tg_ctl is None:
        return None
    payload = _grant_payload(
        hook_id, env_var, context, cwd, command, env_map,
        request_id=f"hatch-pull-{hook_id}-{uuid.uuid4().hex[:12]}",
    )
    outcome = _run_tg_ctl_ask(
        [str(tg_ctl), "ask", "--verify-grant", "--pull", "--hook", hook_id],
        json.dumps(payload),
        _GRANT_LOOKUP_TIMEOUT_S,
        requested_timeout=_GRANT_LOOKUP_TIMEOUT_S,
    )
    if isinstance(outcome, str):
        return None
    returncode, out, _err = outcome
    if returncode != 0 or not (out or "").strip():
        return None
    try:
        reply = json.loads((out or "").strip())
    except ValueError:
        return None
    if _parse_ask_decision(reply) != "allow":
        return None
    approval_id = _grant_reply_field(reply, "approvalId")
    original_request = _grant_reply_field(reply, "requestId")
    if not approval_id or not re.fullmatch(r"hgr-[0-9a-f]{12}", approval_id):
        return None
    if not original_request:
        return None
    return HatchApprovalResult(
        requested=True,
        approved=True,
        reason=f"late-approval grant {approval_id} consumed automatically (request {original_request})",
        env_var=env_var,
        env_present=True,
        tg_ctl_path=str(tg_ctl),
        critical=critical,
    )

def _request_present_hatch_approval(
    hook_id: str,
    context: Mapping[str, object],
    *,
    cwd: str,
    env_var: str,
    raw: str,
    command: str | None,
    tg_ctl_candidates: Sequence[Path | str] | None,
    timeout_s: float,
    process_margin_s: float,
    env_map: Mapping[str, str],
    critical: bool = False,
) -> HatchApprovalResult:
    justification = raw.strip()
    if not justification:
        return HatchApprovalResult(
            requested=False,
            approved=False,
            reason=f"{env_var} is blank; Telegram hatch escalation denied",
            env_var=env_var,
            env_present=True,
            critical=critical,
        )
    grant_match = _APPROVED_GRANT_RE.fullmatch(justification)
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
            critical=critical,
        )
    if justification.startswith("approved:") and not any(ch.isspace() for ch in justification):
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"{env_var} contains a malformed late-approval grant token",
            env_var=env_var,
            env_present=True,
            critical=critical,
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
            critical=critical,
        )


    lookup = _find_tg_ctl(tg_ctl_candidates, cwd)
    tg_ctl = lookup.path
    if tg_ctl is None:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=lookup.unavailable_reason("tg-ctl is not available at a trusted executable path"),
            env_var=env_var,
            env_present=True,
            critical=critical,
        )

    # SECURITY / agent-tools#754: a `critical=True` caller gets NO local timeout at all — not a
    # very large one, an ACTUAL unbounded wait, because a real critical decision (Alex reviewing
    # something while away from his phone) may take hours, and any finite number here would just
    # relocate the exact bug this ticket exists to fix (a client-side clock outliving the human's
    # attention, then discarding a tap that arrives after it). `_bounded_timeout` is deliberately
    # NEVER called on this path — feeding a caller-supplied `timeout_s` through it and then
    # overriding the result would leave a foot-gun for a future refactor to accidentally re-cap
    # the critical path. `proc_timeout=None` reaches `subprocess.Popen.communicate` unmodified,
    # whose own contract is to block until the child exits with no deadline at all. This does NOT
    # weaken the approval boundary itself: `tg_ctl` is still resolved exclusively from the trusted
    # absolute-path allowlist above (never from `cwd`), and the reply is still parsed by the same
    # fail-closed `_parse_ask_decision` check below — critical mode changes how long we wait for a
    # trustworthy answer, never what counts as one.
    if critical:
        effective_timeout: float | None = None
        proc_timeout: float | None = None
    else:
        effective_timeout = _bounded_timeout(timeout_s)
        proc_timeout = effective_timeout + max(process_margin_s, 0.0)

    branch = _git_branch(cwd)
    session_id = _resolve_session_id(context, env_map)
    question = _question(hook_id, justification, context, cwd, branch=branch, session_id=session_id)
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
    # The daemon's own per-ask deadline is a hard-coded 115s (ASK_TOTAL_TIMEOUT_MS in
    # tg-ctl) regardless of what is requested here UNLESS `critical` is set below — a
    # critical ButtonRequest tells the daemon itself (tg-cli#403) to also drop that
    # bound; `effective_timeout` only ever bounds how long THIS process waits for
    # tg-ctl to give up and exit, never the daemon's own deadline.
    request_id = f"hatch-{hook_id}-{uuid.uuid4().hex[:12]}"
    # tg-cli#398/PR#400 shipped the daemon-side "late-approval grant" protocol: a Telegram
    # Approve tap that arrives after this request has already expired can still mint a grant
    # for a plain retry of the SAME operation, but only if `isGrantableRequest` on the tg-ctl
    # side finds `hookId` + `operation.fingerprint` + `operation.cwd` all present below. This
    # fingerprint is what identifies "the same operation" across the two separate `tg-ctl ask`
    # invocations (the original, now-expired ask and the later retry) — see
    # `_operation_fingerprint`'s docstring for why the justification text is excluded.
    operation_fingerprint = _operation_fingerprint(hook_id, context, env_var=env_var, command=command)
    button_request = {
        "requestId": request_id,
        "agent": _ASK_AGENT,
        "kind": "permission",
        "question": question,
        "title": f"Hatch: {hook_id}",
        "decisionLabels": {"allow": "Approve", "deny": "Deny"},
        # Structural `cwd` (agent-tools#754 / tg-cli#403): previously `cwd` only ever reached the
        # daemon embedded inside the free-text `question` body (the "CWD: …" line above), which is
        # unparseable as an identity key. A late critical decision is proactively pushed into the
        # requesting agent's live session by matching THIS field against `listLiveOmpAgents()` on
        # the tg-cli side — reusing that existing live-session lookup, not a new identity scheme.
        "cwd": cwd,
        # `hookId` + `operation`: required by tg-ctl's `isGrantableRequest` to mint a
        # late-approval grant (tg-cli#398/PR#400). Without these, EVERY hatch ask —
        # however recent — was silently ungrantable on a late Approve tap; see
        # `_operation_fingerprint` for the identity scheme.
        "hookId": hook_id,
        "operation": {
            "kind": hook_id,
            "fingerprint": operation_fingerprint,
            "cwd": cwd,
        },
    }
    # Best-effort tie-breaker alongside `cwd` for the same proactive-delivery match (tg-cli#403
    # design decision 4): mirrors exactly how tg-ctl's own `askDaemon()` captures `env.paneId`
    # from `TMUX_PANE` for its non-hatch asks. Read from `env_map` (the same resolved environment
    # `request_hatch_approval` already uses for the hatch env var and `_resolve_session_id`), not
    # bare `os.environ`, so a caller that passes an explicit `env=` mapping (every existing test in
    # this suite) gets deterministic, hermetic behavior instead of leaking the real test-runner's
    # own tmux pane. Omitted entirely when unset — tg-ctl's `normalizeButtonRequest` already
    # treats a missing field as "no pane", so there is nothing to default it to.
    pane_id = env_map.get("TMUX_PANE")
    if pane_id:
        button_request["paneId"] = pane_id
    # Only set when True — see `HatchApprovalResult.critical`'s docstring and AC #4 of
    # agent-tools#754: a non-critical caller's JSON payload must be byte-identical to today's
    # (no `critical` key at all), not merely `"critical": false`, so an old tg-ctl build (or a
    # test asserting the exact payload shape) sees no observable difference whatsoever.
    if critical:
        button_request["critical"] = True
    argv = [str(tg_ctl), "ask", "--agent", _ASK_AGENT]

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
            critical=critical,
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
        critical=critical,
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
    argv: list[str],
    stdin_payload: str,
    proc_timeout: float | None,
    *,
    requested_timeout: float | None,
) -> tuple[int, str, str] | str:
    """Run `tg-ctl ask` with the ButtonRequest on stdin. Returns (returncode, stdout,
    stderr) on completion, or a deny REASON string when the process could not be run
    to completion (launch failure, timeout, I/O error) — the caller treats either as
    untrusted input, never as approval. `proc_timeout` is the padded wait actually
    enforced; `requested_timeout` is the unpadded budget quoted in the timeout reason.

    `proc_timeout=None` (agent-tools#754 critical mode) reaches `Popen.communicate` unmodified:
    per its own documented contract, passing `timeout=None` blocks until the child exits with NO
    deadline — `subprocess.TimeoutExpired` can never be raised on this path, so `requested_timeout`
    is also `None` and the `except subprocess.TimeoutExpired` branch below is simply unreachable
    for a critical call, not merely never expected to fire.
    """
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
            "decision": "approved" if result.approved else "denied",
            "detail": result.reason[:_DETAIL_CAP],
            "cwd": cwd,
        }
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

    This is the trust anchor for resolving the approval binary (see `_candidate_paths`): the
    machine-global rig config (`<home>/.config/rig/config.yaml`, `tg_ctl.tg_ctl_path`) and the
    home-relative install dirs are read ONLY from this home, never from the agent-controlled
    working directory. Keying off `getpwuid` (not the environment) means a guarded agent cannot
    redirect the approval binary by committing a repo-local config, by running the hook from a
    doctored `cwd`, or by exporting `HOME` — which would otherwise reopen the exact self-service
    bypass this hatch exists to close (mirrors the ship review-quorum gate's `resolve_home`,
    PR #225).

    It deliberately does NOT fall back to `os.path.expanduser("~")` on failure: that would read
    `$HOME` (agent-exportable) or, if `$HOME` is unset, resolve `~` relative to the (agent-
    controlled) cwd and walk up into the repo — exactly the untrusted anchors this exists to
    exclude. Returning None makes the caller SKIP the rig config and home dirs entirely (fail
    closed to the system install dirs). Overridable in tests via monkeypatch (never via the
    environment)."""

    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return None


@dataclass(frozen=True)
class _TgCtlLookup:
    """The trusted tg-ctl (or None) plus every candidate passed over and why, so a denial can
    tell the human exactly which path to fix instead of a bare "not available"."""

    path: Path | None
    rejected: tuple[str, ...] = ()

    def unavailable_reason(self, summary: str) -> str:
        if not self.rejected:
            return summary
        return f"{summary} (searched: {'; '.join(self.rejected)})"


def _find_tg_ctl(candidates: Sequence[Path | str] | None, cwd: str | None) -> _TgCtlLookup:
    """The first candidate that `resolve_trusted_file` accepts AND that lies outside every git
    checkout the guarded agent is working in (the hook's `cwd` and this process's own cwd) — a
    binary inside that checkout is something the agent's own PR could rewrite to auto-approve."""

    sandbox = _test_sandbox()
    home = resolve_home()
    if home and sandbox is not None and not _is_within(Path(os.path.realpath(home)), sandbox):
        home = None
    rejected: list[str] = []
    checkouts = _checkout_roots(_working_dirs(cwd), home)
    for candidate in _candidate_paths(candidates, home, rejected):
        links: list[Path] = []
        resolved, problem = resolve_trusted_file(candidate, executable=True, links=links)
        if resolved is not None:
            visited = [Path(_native_path(str(candidate))), *links, resolved]
            problem = _placement_problem(visited, checkouts, sandbox)
            if problem is None:
                return _TgCtlLookup(resolved, tuple(rejected))
        rejected.append(f"{candidate}: {problem}")
    return _TgCtlLookup(None, tuple(rejected))


def _placement_problem(
    visited: Sequence[Path], checkouts: Sequence[Path], sandbox: Path | None
) -> str | None:
    """`visited` is the candidate, every symlink its resolution passed through, then the final
    binary: a link inside the agent's checkout is as rewritable by its PR as the binary itself."""

    resolved = visited[-1]
    for root in checkouts:
        inside = next((path for path in visited if _is_within(path, root)), None)
        if inside is not None:
            return f"{inside} is inside the git checkout {root} the guarded agent works in"
    if sandbox is not None and not _is_within(resolved, sandbox):
        return f"{resolved} is outside the test sandbox {sandbox}"
    return None


def _test_sandbox() -> Path | None:
    """Test-suite safety net: tests/conftest.py exports `AGENTTOOLS_TEST_HERMETIC_HOME` (a dir in
    pytest's basetemp) and every hook SUBPROCESS a test spawns inherits it, while the in-process
    `resolve_home` monkeypatch does not reach those subprocesses. Under it, only a tg-ctl inside
    that basetemp may run, so no test can ever message the human through the machine's real
    tg-ctl. It can only REMOVE candidates, so an agent exporting it merely denies itself."""

    hermetic = os.environ.get("AGENTTOOLS_TEST_HERMETIC_HOME")
    return Path(os.path.realpath(hermetic)).parent if hermetic else None


def _candidate_paths(
    candidates: Sequence[Path | str] | None, home: str | None, rejected: list[str]
) -> list[Path | str]:
    """Search order: the machine-global rig config's explicit `tg_ctl.tg_ctl_path` (ALWAYS first,
    even when `candidates` is given), then the caller's explicit `candidates` or, when None, the
    default home-relative + system install paths.

    SECURITY: every home-anchored entry hangs off the account's REAL home (`resolve_home`), NEVER
    `$HOME`/`$XDG_CONFIG_HOME`/cwd — otherwise a guarded agent could commit or export a config
    pointing `tg_ctl_path` at `/tmp/always-exit-0` and self-approve. When the home can't be
    resolved the home-anchored entries are SKIPPED (fail closed to the system dirs)."""

    out: list[Path | str] = []
    if home:
        override, problem = _rig_tg_ctl_path(home)
        if override is not None:
            out.append(override)
        elif problem is not None:
            rejected.append(problem)
    if candidates is not None:
        out.extend(candidates)
        return out
    if home:
        out.extend(Path(home) / directory / _TG_CTL_NAME for directory in _HOME_TG_CTL_DIRS)
    out.extend(_TRUSTED_TG_CTL_PATHS)
    return out


def _rig_tg_ctl_path(home: str) -> tuple[str | None, str | None]:
    """`(override, problem)` for `tg_ctl.tg_ctl_path` in EXACTLY `<home>/.config/rig/config.yaml`.

    `(None, None)` when the config or the key is absent. The config itself must pass
    `resolve_trusted_file` — a config another account can rewrite is no authority. A `~/` value is
    expanded against the REAL `home` (rig's own `_expand_home` semantics), never `$HOME`; a Cygwin
    `C:\\...` value is translated later by `resolve_trusted_file`."""

    config = Path(home) / _RIG_GLOBAL_CONFIG_RELATIVE
    if not os.path.lexists(config):
        return None, None
    resolved, problem = resolve_trusted_file(config, executable=False)
    if resolved is None:
        return None, f"{config}: ignored ({problem})"
    try:
        block = machine_config_mapping(_read_trusted_text(resolved), (_RIG_TG_CTL_BLOCK,))
    except (OSError, UnicodeDecodeError, MachineConfigError) as exc:
        return None, f"{config}: ignored ({exc})"
    value = (block or {}).get(_RIG_TG_CTL_KEY)
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, f"{config}: ignored (tg_ctl.tg_ctl_path must be a path string)"
    value = value.strip()
    if value.startswith("~/"):
        value = str(Path(home) / value[2:])
    return value, None


def _read_trusted_text(path: Path) -> str:
    """Re-check `path` on the opened descriptor (O_NOFOLLOW + fstat) so a swap between
    `resolve_trusted_file` and the read cannot substitute another file."""

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        problem = None if stat.S_ISREG(info.st_mode) else f"{path} is not a regular file"
        problem = problem or path_trust_problem(path, info)
        if problem:
            raise OSError(problem)
        return stream.read().decode("utf-8")


def _working_dirs(cwd: str | None) -> list[str]:
    dirs = [cwd] if cwd else []
    try:
        dirs.append(os.getcwd())
    except OSError:
        pass
    return dirs


def _checkout_roots(start_dirs: Sequence[str], home: str | None) -> list[Path]:
    """Every git worktree root at or above each of `start_dirs`, plus the main worktree a linked
    worktree belongs to (an agent in `.worktrees/x` can still edit the primary checkout). Pure
    filesystem walk — no `git` subprocess, so ambient PATH plays no part in this decision.

    The walk stops at the real home: a dotfiles repo rooted AT `~` (yadm-style) would otherwise
    swallow `~/.local/bin` and every other home install dir."""

    stop = Path(os.path.realpath(home)) if home else None
    roots: list[Path] = []
    for start in start_dirs:
        current = Path(os.path.realpath(start))
        for directory in (current, *current.parents):
            if stop is not None and (directory == stop or directory in stop.parents):
                break
            dotgit = directory / ".git"
            if dotgit.is_dir():
                roots.append(directory)
            elif dotgit.is_file():
                roots.append(directory)
                main = _main_worktree(dotgit)
                if main is not None:
                    roots.append(main)
    return roots


def _main_worktree(dotgit_file: Path) -> Path | None:
    """The primary checkout behind a linked worktree's `.git` file (`gitdir: <common>/worktrees/x`
    whose `commondir` names `<main>/.git`), or None for a submodule / unreadable pointer."""

    try:
        pointer = dotgit_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not pointer.startswith("gitdir:"):
        return None
    gitdir = Path(pointer[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = dotgit_file.parent / gitdir
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    common_dir = Path(os.path.realpath(gitdir / common))
    return common_dir.parent if common_dir.name == ".git" else None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def path_trust_problem(path: Path | str, info: os.stat_result) -> str | None:
    """Why `path` (whose own, non-followed stat is `info`) cannot anchor a security decision, or
    None when it can: owned by you or root (plus Cygwin's SYSTEM/Administrators), and writable by
    nobody else.

    Group-write is accepted only for a group whose members are already as trusted as the owner:
    your user-private group (Debian/Ubuntu/Fedora's default umask 002 leaves `~/.local/bin` and
    friends group-writable for exactly that group), root's gid 0, macOS `admin`, Cygwin
    Administrators/SYSTEM. A sticky directory (`/tmp`) is fine to traverse: nobody else can
    rename or replace an entry we own in it, and every entry below it is checked on its own."""

    if info.st_uid not in _trusted_owner_ids():
        return f"{path} is owned by uid {info.st_uid}, not by you or root"
    mode = info.st_mode
    sticky_dir = stat.S_ISDIR(mode) and bool(mode & stat.S_ISVTX)
    if stat.S_ISLNK(mode) or sticky_dir:
        return None
    if mode & stat.S_IWOTH:
        return f"{path} is world-writable"
    if mode & stat.S_IWGRP and not _group_is_trusted(info.st_gid):
        return f"{path} is group-writable by gid {info.st_gid}, which is not your private group"
    return None


def _trusted_owner_ids() -> frozenset[int]:
    owners = {os.geteuid(), 0}
    if _IS_CYGWIN:
        owners |= _CYGWIN_TRUSTED_IDS
    return frozenset(owners)


def _group_is_trusted(gid: int) -> bool:
    if gid == 0:
        return True
    if _IS_DARWIN and gid == _DARWIN_ADMIN_GID:
        return True
    if _IS_CYGWIN and gid in _CYGWIN_TRUSTED_IDS:
        return True
    return _is_private_group(gid)


@functools.lru_cache(maxsize=None)
def _is_private_group(gid: int) -> bool:
    """True for a user-private group: named after you, your primary group, no other member and
    no other account's primary group. Looked up by group NAME (`getpwnam`), not `getpwuid`.

    Cached per process (the euid and account database do not change under a hook): one lookup
    visits several group-writable dirs of the same gid, and `getpwall` can be a full directory
    enumeration on an LDAP/SSSD host, so it only runs after the cheap checks pass, once.

    `gr_mem` lists only the members the NSS backend reports; a directory service that resolves
    membership elsewhere could hide one. The target is a developer machine's local accounts."""

    uid = os.geteuid()
    try:
        group = grp.getgrgid(gid)
        owner = pwd.getpwnam(group.gr_name)
    except (KeyError, OSError):
        return False
    if owner.pw_uid != uid or owner.pw_gid != gid:
        return False
    if any(member != group.gr_name for member in group.gr_mem):
        return False
    try:
        accounts = pwd.getpwall()
    except OSError:
        return False
    return not any(account.pw_gid == gid and account.pw_uid != uid for account in accounts)


def resolve_trusted_file(
    path: Path | str, *, executable: bool, links: list[Path] | None = None
) -> tuple[Path | None, str]:
    """Resolve absolute `path` to a regular file one component at a time, following symlinks by
    hand, so EVERY directory the lookup traverses (each symlink's own directory, each target's
    directories) and the final file pass `path_trust_problem`. Returns `(real_path, "")` or
    `(None, why)`; every symlink followed on the way is appended to `links` when given.

    `os.path.realpath` + a check of the final file alone is not enough: a world-writable
    directory anywhere on the way, or a symlink planted in one, lets another account swap the
    binary after the check. On Cygwin a missing final `name` is retried as `name.exe`, and a
    Windows drive path (`C:\\...`) is translated to its cygdrive form first."""

    raw = Path(_native_path(str(path)))
    if not raw.is_absolute():
        return None, f"{path} is not an absolute path"
    current = Path(raw.anchor)
    try:
        problem = path_trust_problem(current, os.lstat(current))
    except OSError as exc:
        return None, f"{current} cannot be inspected ({exc.strerror or exc})"
    if problem:
        return None, problem
    pending = list(raw.parts[1:])
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        entry = current / part
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            if pending or not _IS_CYGWIN or part.lower().endswith(".exe"):
                return None, f"{entry} does not exist"
            entry = current / f"{part}.exe"
            try:
                info = os.lstat(entry)
            except OSError:
                return None, f"{current / part} (or {entry.name}) does not exist"
        except OSError as exc:
            return None, f"{entry} cannot be inspected ({exc.strerror or exc})"
        problem = path_trust_problem(entry, info)
        if problem:
            return None, problem
        if stat.S_ISLNK(info.st_mode):
            if links is not None:
                links.append(entry)
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                return None, f"{path} has more than {_MAX_SYMLINK_HOPS} levels of symlinks"
            target = Path(os.readlink(entry))
            if target.is_absolute():
                current = Path(target.anchor)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
            continue
        if pending and not stat.S_ISDIR(info.st_mode):
            return None, f"{entry} is not a directory"
        current = entry
    try:
        final = os.lstat(current)
    except OSError as exc:
        return None, f"{current} cannot be inspected ({exc.strerror or exc})"
    if not stat.S_ISREG(final.st_mode):
        return None, f"{current} is not a regular file"
    if executable and not os.access(current, os.X_OK):
        return None, f"{current} is not executable"
    return current, ""


def _native_path(value: str) -> str:
    """On Cygwin, `C:\\Users\\me\\bin\\tg-ctl.exe` -> `<cygdrive>/c/Users/me/bin/tg-ctl.exe`;
    anything else unchanged (a POSIX interpreter cannot open a drive path directly)."""

    match = _WINDOWS_DRIVE_PATH_RE.match(value) if _IS_CYGWIN else None
    if match is None:
        return value
    rest = value[3:].replace("\\", "/")
    return f"{_cygdrive_prefix()}/{match.group(1).lower()}/{rest}"


def _cygdrive_prefix() -> str:
    # Cygwin exposes the configured cygdrive prefix (default `/cygdrive`, often `/`) as a symlink.
    try:
        return os.readlink("/proc/cygdrive").rstrip("/")
    except OSError:
        return _DEFAULT_CYGDRIVE_PREFIX


class MachineConfigError(ValueError):
    """The machine config cannot be read unambiguously — callers treat it as absent authority."""


class _Unparsed:
    """A value this reader deliberately does not interpret (anchors, tags, block scalars,
    numbers, nested collections, YAML-1.1-only booleans). Never equal to any real setting, so a
    caller's type check rejects it — fail closed rather than guess."""

    def __repr__(self) -> str:
        return "<unparsed>"


UNPARSED = _Unparsed()
_YAML_KEY_RE = re.compile(
    r"""(?:"([^"\\]*)"|'((?:[^']|'')*)'|([A-Za-z0-9_][A-Za-z0-9_.\-/]*))[ ]*:(?:[ ]+(.*))?$"""
)
_YAML_TRUE = frozenset({"true", "True", "TRUE"})
_YAML_FALSE = frozenset({"false", "False", "FALSE"})
_YAML_NULL = frozenset({"null", "Null", "NULL", "~"})
# Plain words YAML 1.1 (PyYAML) reads as booleans but YAML 1.2 reads as strings: ambiguous.
_YAML_11_BOOLS = frozenset({"yes", "no", "on", "off", "y", "n"})
_YAML_NUMBER_RE = re.compile(
    r"[-+]?(?:\.?[0-9][0-9_.]*(?:[eE][-+]?[0-9]+)?|0x[0-9a-fA-F_]+|0o[0-7_]+|\.(?:inf|Inf|INF))"
    r"|\.(?:nan|NaN|NAN)"
)
_YAML_PLAIN_FORBIDDEN_START = frozenset("&*!|>%@`{}[],#'\"?:")


def machine_config_mapping(text: str, path: Sequence[str]) -> dict[str, object] | None:
    """The mapping at `path` (e.g. `("ci", "items", "ship")`) in a machine config, without PyYAML.

    Gate helpers run under `python3 -I`, which hides user-site packages — on most machines that
    is where PyYAML lives, so an `import yaml` there silently fails (and fails CLOSED, which made
    every owner's config inert). rig writes a plain block-style subset, so this reads exactly that:
    JSON documents, or block mappings with plain/quoted scalar values and scalar lists (block or
    one-line `[a, b]`). Subtrees off `path` are skipped by indentation without being interpreted.

    Returns None when a key on `path` is absent. Raises `MachineConfigError` when the structure ON
    `path` is ambiguous (duplicate key, flow mapping, anchor, odd indentation, multiple documents);
    a value it will not interpret comes back as `UNPARSED`."""

    if text.lstrip().startswith(("{", "[")):
        return _json_mapping(text, path)
    lines = _yaml_lines(text)
    start, end = 0, len(lines)
    for depth, key in enumerate(path):
        entry = _yaml_block_entries(lines, start, end).get(key)
        if entry is None:
            return None
        rest, start, end, number = entry
        if rest:
            dotted = ".".join(path[: depth + 1])
            raise MachineConfigError(f"line {number}: {dotted} must be a block mapping")
    return {key: _yaml_value(lines, entry) for key, entry in _yaml_block_entries(lines, start, end).items()}


def _json_mapping(text: str, path: Sequence[str]) -> dict[str, object] | None:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                raise MachineConfigError(f"duplicate key {key!r}")
            out[key] = value
        return out

    try:
        data = json.loads(text, object_pairs_hook=unique)
    except MachineConfigError:
        raise
    except ValueError as exc:
        raise MachineConfigError(f"invalid JSON ({exc.msg})") from None
    for depth, key in enumerate(path):
        if not isinstance(data, dict):
            raise MachineConfigError(f"{'.'.join(path[:depth]) or 'document'} is not a mapping")
        data = data.get(key)
        if data is None:
            return None
    if not isinstance(data, dict):
        raise MachineConfigError(f"{'.'.join(path)} is not a mapping")
    return data


def _yaml_lines(text: str) -> list[tuple[int, str, int]]:
    """`(indent, content, line_number)` for every line that is not blank or a whole-line comment."""

    out: list[tuple[int, str, int]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        body = raw.rstrip()
        content = body.lstrip(" ")
        if not content or content.startswith("#"):
            continue
        if content.startswith("\t"):
            raise MachineConfigError(f"line {number}: tab indentation")
        indent = len(body) - len(content)
        if indent == 0 and (content in ("---", "...") or content.startswith(("--- ", "%"))):
            if not out and content == "---":
                continue
            raise MachineConfigError(f"line {number}: multiple documents or directives")
        out.append((indent, content, number))
    return out


def _yaml_block_entries(
    lines: list[tuple[int, str, int]], start: int, end: int
) -> dict[str, tuple[str, int, int, int]]:
    """`key -> (inline_value, child_start, child_end, line_number)` for the block mapping that
    occupies `lines[start:end]`. A value-less key owns every following deeper-indented line, plus
    an indentless `- item` sequence at its own indent."""

    entries: dict[str, tuple[str, int, int, int]] = {}
    if start >= end:
        return entries
    indent = lines[start][0]
    i = start
    while i < end:
        line_indent, content, number = lines[i]
        if line_indent != indent:
            raise MachineConfigError(f"line {number}: inconsistent indentation")
        match = _YAML_KEY_RE.match(content)
        if match is None:
            raise MachineConfigError(f"line {number}: expected `key: value`")
        double, single, plain, rest = match.groups()
        key = double if double is not None else single.replace("''", "'") if single is not None else plain
        if key in entries:
            raise MachineConfigError(f"line {number}: duplicate key {key!r}")
        rest = _strip_yaml_comment(rest or "")
        j = i + 1
        while j < end and (
            lines[j][0] > indent or (not rest and lines[j][0] == indent and _is_yaml_item(lines[j][1]))
        ):
            j += 1
        entries[key] = (rest, i + 1, j, number)
        i = j
    return entries


def _yaml_value(lines: list[tuple[int, str, int]], entry: tuple[str, int, int, int]) -> object:
    rest, start, end, _number = entry
    if rest:
        return _yaml_scalar(rest)
    if start == end:
        return None
    indent = lines[start][0]
    items: list[object] = []
    for line_indent, content, _ in lines[start:end]:
        if line_indent != indent or not _is_yaml_item(content):
            return UNPARSED
        item = _yaml_scalar(content[1:])
        if item is UNPARSED:
            return UNPARSED
        items.append(item)
    return items


def _is_yaml_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _strip_yaml_comment(text: str) -> str:
    """`text` without a trailing ` # comment` that sits outside quotes."""

    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote == "'" and text[index + 1:index + 2] == "'":
                index += 1  # `''` is an escaped quote inside a single-quoted scalar
            elif char == quote:
                quote = ""
        elif char in "\"'" and (index == 0 or text[index - 1] in " [,"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] == " "):
            return text[:index].rstrip()
        index += 1
    return text.strip()


def _yaml_scalar(text: str) -> object:
    text = _strip_yaml_comment(text)
    if not text:
        return None
    if text[0] == '"':
        close = text.find('"', 1)
        if close < 0 or "\\" in text[1:close] or text[close + 1:].strip():
            return UNPARSED
        return text[1:close]
    if text[0] == "'":
        match = re.fullmatch(r"'((?:[^']|'')*)'", text)
        return match.group(1).replace("''", "'") if match else UNPARSED
    if text[0] == "[":
        if not text.endswith("]") or any(char in text[1:-1] for char in "[]{}"):
            return UNPARSED
        inner = text[1:-1].strip()
        if not inner:
            return []
        items = [_yaml_scalar(part) for part in inner.split(",")]
        return UNPARSED if any(item is UNPARSED or item is None for item in items) else items
    if text[0] in _YAML_PLAIN_FORBIDDEN_START or text.startswith("- ") or ": " in text or text.endswith(":"):
        return UNPARSED
    if text in _YAML_TRUE:
        return True
    if text in _YAML_FALSE:
        return False
    if text in _YAML_NULL:
        return None
    if text.lower() in _YAML_11_BOOLS or _YAML_NUMBER_RE.fullmatch(text):
        return UNPARSED
    return text


def _git_branch(cwd: str) -> str:
    """The current git branch at `cwd`, for the human-facing "Branch:" line -- informational
    only, never a security decision (unlike the trusted absolute `tg-ctl` path used elsewhere in
    this module, this intentionally consults ambient PATH `git`; worst case it reports "unknown",
    and nothing in the hatch approval decision path ever reads this value). Falls back to
    "unknown" when `cwd` is not inside a git repo, `git` is unavailable, HEAD is detached
    (`rev-parse --abbrev-ref HEAD` prints the literal string "HEAD" rather than erroring for a
    detached HEAD), or the call otherwise fails for any reason -- never raises.
    """
    try:
        result = subprocess.run(  # noqa: S603, S607 - informational display only, never gates approval
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "unknown"
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch or branch == "HEAD":
        return "unknown"
    return branch


def _html_escape(value: object) -> str:
    """Escape a dynamic value for Telegram HTML (`<`, `>`, `&`) -- see
    https://core.telegram.org/bots/api#html-style. Telegram does not require escaping quotes
    outside an attribute value, so quotes are left untouched (`quote=False`) rather than turned
    into `&quot;`/`&#x27;` noise inside a justification or command that legitimately quotes
    something."""
    return html.escape(str(value), quote=False)


def _question(
    hook_id: str,
    justification: str,
    context: Mapping[str, object],
    cwd: str,
    *,
    branch: str,
    session_id: str,
) -> str:
    """The human-facing hatch approval prompt, sent verbatim as `ButtonRequest.question` with
    `parse_mode=HTML` -- tg-cli's `buildQuestionText` renders a hatch permission's `question`
    AS-IS instead of wrapping it in the generic heading/source concatenation it uses for every
    other permission/question (see `isHatchPermissionRequest`), because this string already IS
    the whole card. Ordered so the facts a human needs to decide render without scrolling or
    expanding anything: a bold title, then the requesting agent/session and its path/branch,
    then the hook id and the written justification. Everything else (the raw command and any
    remaining context fields) goes inside a collapsed `<blockquote expandable>` so it stays one
    tap away without crowding out the part that matters.
    """
    top = [
        "<b>\U0001f513 Hatch approval request</b>",
        "",
        f"<b>Agent:</b> {_html_escape(_ASK_AGENT)} (session {_html_escape(session_id)})",
        f"<b>Path:</b> <code>{_html_escape(cwd)}</code>",
        f"<b>Branch:</b> <code>{_html_escape(branch)}</code>",
        "",
        f"<b>Hook:</b> <code>{_html_escape(hook_id)}</code>",
        f"<b>Justification:</b> {_html_escape(justification)}",
    ]

    details: list[str] = []
    command = context.get("command")
    if command:
        details.append(f"<b>Command:</b>\n{_html_escape(_context_value(command))}")
    extra_lines: list[str] = []
    for key in sorted(context):
        if key in ("command", "hook"):
            continue
        value = context[key]
        if value is None or value == "":
            continue
        extra_lines.append(f"<b>{_html_escape(str(key))}:</b> {_html_escape(_context_value(value))}")
    if extra_lines:
        details.append("\n".join(extra_lines))

    if details:
        detail_block = "\n\n".join(details)
        top.append("")
        top.append(f"<blockquote expandable>{detail_block}</blockquote>")

    return "\n".join(top)


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
    "hatch_env_var",
    "request_hatch_approval",
    "resolve_home",
]
