"""Shared Telegram hatch escalation helper for agent-hook scripts.

The helper is intentionally small and stdlib-only: hook scripts import it directly from the
agent-tools checkout, before package installation can be assumed. It turns a per-hook env var
(`RIG_HATCH_REQUEST_<HOOK_ID>`) into a one-time `tg-ctl ask` call through a trusted absolute
path. It never consults ambient PATH.
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import signal
import subprocess  # noqa: S404 - runs an already-resolved absolute tg-ctl path
from dataclasses import dataclass
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
_RIG_TG_CTL_KEY = "tg_ctl_path"
_BARE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})
_TRUSTED_TG_CTL_PATHS = (
    Path("/Users/ultra/.files/bin/tg-ctl"),
    Path("/usr/local/bin/tg-ctl"),
    Path("/opt/homebrew/bin/tg-ctl"),
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
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars="".join(_SEPARATOR_CHARS))
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
    """

    env_map = env if env is not None else os.environ
    env_var = hatch_env_var(hook_id)
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
        return _request_present_hatch_approval(
            hook_id,
            context or {},
            cwd=cwd,
            env_var=env_var,
            raw=raw,
            tg_ctl_candidates=tg_ctl_candidates,
            timeout_s=timeout_s,
            process_margin_s=process_margin_s,
        )
    except Exception as exc:  # noqa: BLE001 - a hatch request must never fail open.
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"Telegram hatch escalation errored: {exc}",
            env_var=env_var,
            env_present=True,
        )


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
    argv = [str(tg_ctl), "ask", question, "--timeout", _format_seconds(effective_timeout)]
    proc_timeout = effective_timeout + max(process_margin_s, 0.0)

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv[0] is an absolute, executable tg-ctl path
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"tg-ctl failed to launch: {exc}",
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )
    try:
        out, err = proc.communicate(timeout=proc_timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"tg-ctl ask timed out after {effective_timeout:.0f}s",
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )
    except (OSError, ValueError) as exc:
        _kill_process_group(proc)
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"tg-ctl ask errored: {exc}",
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )

    detail = ((out or "").strip() or (err or "").strip())[:_DETAIL_CAP]
    if proc.returncode != 0:
        reason = f"tg-ctl ask denied (exit {proc.returncode})"
        if detail:
            reason = f"{reason}: {detail}"
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=reason,
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )

    return HatchApprovalResult(
        requested=True,
        approved=True,
        reason=detail or "approved by tg-ctl ask",
        env_var=env_var,
        env_present=True,
        tg_ctl_path=str(tg_ctl),
    )


def _bounded_timeout(timeout_s: float) -> float:
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        return DEFAULT_TG_CTL_TIMEOUT_S
    if value <= 0:
        return DEFAULT_TG_CTL_TIMEOUT_S
    return min(value, MAX_TG_CTL_TIMEOUT_S)


def _format_seconds(timeout_s: float) -> str:
    if timeout_s.is_integer():
        return str(int(timeout_s))
    return str(timeout_s)


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
    "hatch_env_var",
    "request_hatch_approval",
    "resolve_home",
]
