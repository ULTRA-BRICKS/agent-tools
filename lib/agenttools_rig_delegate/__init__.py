"""Rig-aware install-hook delegation — the one place every ecosystem CLI asks
"is rig here? then let rig own the hooks."

WHAT / WHY
==========
Every ecosystem CLI (tg-ctl, review, ...) ships an ``install-hooks`` command that
writes agent-harness hooks directly (``~/.claude/settings.json``,
``~/.codex/hooks.json``, ...). When ``rig`` is also installed on the machine, those
direct writers become a SECOND source of truth for the same hooks — the exact
duplication that makes codex warn ``loading hooks from both ...`` and that lets two
tools fight over one file.

This module is the shared decision every such command makes at its top:

    if rig is present  -> DELEGATE to rig (``rig apply`` / ``rig config set``); rig is
                          the single source of truth and provisions the hooks.
    if rig is absent   -> run the tool's own direct installer (the FALLBACK), so a
                          machine without rig still gets working hooks.

HOW IT IS REACHED AT RUNTIME
============================
- Python CLIs (review-cli, ...) ``import agenttools_rig_delegate`` and call
  :func:`delegate_or_fallback`.
- Non-Python CLIs (tg-ctl is a bun/TS binary) cannot import Python, so they shell out
  to the ``__main__`` CLI: ``python3 -m agenttools_rig_delegate ...``. The CLI mirrors
  the library contract exactly (see ``__main__.py``); keep the two in sync.

INVARIANTS
==========
- Stdlib only. No third-party imports at module load (mirrors the ecosystem rule).
- Detection is robust: rig is "present" iff a runnable ``rig`` resolves — either on
  ``PATH`` or at a well-known install location. A rig *checkout* on disk is NOT enough
  on its own (a source tree is not an installed CLI); we only treat an executable as
  present. This keeps the fallback firing on machines that merely cloned the repo.
- Delegation NEVER silently falls back on a rig error. Fallback is for "no rig", not
  "rig failed" — a rig that is present but errors is a real failure to surface, else we
  would double-write the very hooks we set out to consolidate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

__all__ = [
    "find_rig",
    "rig_available",
    "delegate",
    "delegate_or_fallback",
    "DelegateResult",
    "NO_RIG_EXIT",
]

# Exit code the ``__main__`` CLI returns when rig is ABSENT, so a shell caller (tg-ctl)
# can branch to its own fallback installer. It MUST NOT collide with any code rig itself
# can exit with, or the caller cannot distinguish "rig absent" from a real rig outcome.
# rig's public contract (riglib/errors.py) uses 0-8 for semantic failure classes
# (notably 3 == EXIT_DRIFT from `rig status`) and 127 for a missing dep; the
# shell reserves 126/127 and 128+n for signals. 97 sits clear of all of those, so a
# non-Python installer that shells out to ``delegate`` can treat this one value — and only
# this value — as "no rig, run my own installer", never mistaking a rig drift/error for it.
NO_RIG_EXIT = 97

# Well-known install locations checked when ``rig`` is not on ``PATH`` (a login shell's
# PATH is often richer than a hook/CLI subprocess env). Kept small and explicit.
_RIG_FALLBACK_BINS = (
    "~/.local/bin/rig",
    "/usr/local/bin/rig",
    "/opt/homebrew/bin/rig",
)


def find_rig(env: dict | None = None) -> str | None:
    """Return an absolute path to a runnable ``rig`` executable, or ``None``.

    Robust to a sparse subprocess ``PATH``: falls back to well-known install locations
    when ``shutil.which`` comes up empty. A ``RIG_BIN`` override wins (tests, unusual
    installs). Returns ``None`` when rig is genuinely absent — the fallback signal.
    """
    environ = env if env is not None else os.environ
    override = environ.get("RIG_BIN")
    if override:
        p = Path(override).expanduser()
        return str(p) if _is_executable(p) else None
    on_path = shutil.which("rig", path=environ.get("PATH"))
    if on_path:
        return on_path
    for candidate in _RIG_FALLBACK_BINS:
        p = Path(candidate).expanduser()
        if _is_executable(p):
            return str(p)
    return None


def _is_executable(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def rig_available(env: dict | None = None) -> bool:
    """True iff a runnable ``rig`` resolves — the "delegate, don't self-install" gate."""
    return find_rig(env) is not None


@dataclass
class DelegateResult:
    """Outcome of a delegation attempt.

    ``delegated`` is True when rig was invoked (regardless of rig's own exit code);
    ``returncode`` is rig's exit code (0 on success). ``fell_back`` is True when rig was
    absent and the caller's fallback ran instead. Exactly one of the two is True.
    """

    delegated: bool
    fell_back: bool
    returncode: int
    rig_path: str | None = None
    command: tuple[str, ...] = ()


def delegate(
    rig_args: Sequence[str],
    *,
    env: dict | None = None,
    cwd: str | None = None,
    check: bool = False,
) -> DelegateResult:
    """Run ``rig <rig_args>`` and return the outcome. Assumes rig is present.

    Raises :class:`RuntimeError` if rig cannot be resolved (callers should gate on
    :func:`rig_available` / use :func:`delegate_or_fallback`). With ``check=True`` a
    non-zero rig exit raises :class:`subprocess.CalledProcessError`.
    """
    rig_path = find_rig(env)
    if rig_path is None:
        raise RuntimeError("rig is not available; call rig_available() before delegate()")
    command = (rig_path, *rig_args)
    proc = subprocess.run(command, cwd=cwd, env=env)  # noqa: S603 - args are internal
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, command)
    return DelegateResult(
        delegated=True,
        fell_back=False,
        returncode=proc.returncode,
        rig_path=rig_path,
        command=command,
    )


def delegate_or_fallback(
    rig_args: Sequence[str],
    fallback: Callable[[], int],
    *,
    env: dict | None = None,
    cwd: str | None = None,
) -> DelegateResult:
    """The single decision an ``install-hooks`` command makes.

    rig present -> delegate (``rig <rig_args>``); rig's exit code is returned as-is (a
    rig failure is surfaced, never swallowed into a fallback). rig absent -> run
    ``fallback`` (the tool's own direct installer) and return its int exit code.
    """
    if rig_available(env):
        return delegate(rig_args, env=env, cwd=cwd)
    code = fallback()
    return DelegateResult(delegated=False, fell_back=True, returncode=int(code))
