"""Shared Telegram hatch escalation helper for agent-hook scripts.

The helper is intentionally small and stdlib-only: hook scripts import it directly from the
agent-tools checkout, before package installation can be assumed. It turns a per-hook env var
(`RIG_HATCH_REQUEST_<HOOK_ID>`) into a one-time `tg-ctl ask` call through a trusted absolute
path. It never consults ambient PATH.

Every attempt where the hatch env var carried ANY value is also appended, best-effort, as one
JSON line to `overrides.log` (retrospective 2026-07-01, section 5.2.3 item 3 / gap G-8: "escape
hatches have no audit sink"). See `_append_overrides_log` for the record shape and trust model.
"""

from __future__ import annotations

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
    """Result of checking one hook's Telegram hatch request."""

    requested: bool
    approved: bool
    reason: str
    env_var: str
    env_present: bool = False
    tg_ctl_path: str | None = None

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

    Unset (in both sources) means "hatch not requested" and does not block later mechanisms. A
    present but blank/bare-flag value is an invalid request: no Telegram call is made and the
    hook should deny rather than falling through to `approval_cmd`.

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
    tg_ctl_candidates: Sequence[Path | str] | None,
    timeout_s: float,
    process_margin_s: float,
) -> HatchApprovalResult:
    justification = raw.strip()
    if not justification:
        return HatchApprovalResult(
            requested=False,
            approved=False,
            reason=f"{env_var} is blank; Telegram hatch escalation denied",
            env_var=env_var,
            env_present=True,
        )
    if justification.lower() in _BARE_FLAG_VALUES:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"{env_var} needs a written justification, not bare {justification!r}",
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
    question = _question(hook_id, justification, context, cwd)
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
    # tg-ctl) regardless of what is requested here; `effective_timeout` still bounds
    # how long THIS process waits for tg-ctl to give up and exit.
    request_id = f"hatch-{hook_id}-{uuid.uuid4().hex[:12]}"
    button_request = {
        "requestId": request_id,
        "agent": _ASK_AGENT,
        "kind": "permission",
        "question": question,
        "title": f"Hatch: {hook_id}",
        "decisionLabels": {"allow": "Approve", "deny": "Deny"},
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
    "hatch_env_var",
    "request_hatch_approval",
    "resolve_home",
]
