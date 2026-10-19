#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — allow removing only what is landed and clean; deny the rest.

Removing a worktree or deleting a branch destroys work when that work exists nowhere else
(unpushed commits, unmerged branch, dirty tree). This hook is a thin, FAIL-CLOSED shim in front
of `rig worktree guard`, which is the ONLY brain: it parses the bash command, resolves every
removal target against real git plus the forge's PR state, and answers one JSON object::

    {"decision": "pass"|"allow"|"deny", "reason": "...", "targets": [{"kind", "target", "safe"}]}

- ``pass``  — not a worktree/branch removal → this hook stays silent (allow).
- ``allow`` — every target is proven landed + clean → allow.
- ``deny``  — at least one target is unsafe, or the form is not safely parseable → block with
  rig's ``reason`` (which names the sanctioned rescue).

Anything else — `rig` not on PATH, nonzero exit, timeout, unparseable/foreign JSON, an unknown
decision — is a DENY with an explicit message. That only applies when the cheap prefilter below
matched; a command the prefilter does not match never spawns `rig` at all.

Prefilter (deliberately loose — rig decides precisely; a false match costs one `rig` call, a
false miss is a hole). Matched on a COPY of the command normalized like the shell would (``$'-D'`` -> ``-D``, ``${IFS}``/``$VAR`` -> a space, then every quote and backslash removed),
so `g''it branch -D x` / `git wor\\ktree remove x` / `git${IFS}branch${IFS}-D` cannot dodge it:

- ``worktree (remove|rm)``
- ``branch`` followed later by ``--d…`` (any prefix of ``--delete`` and friends) or a short-flag
  cluster containing ``d``/``D`` (``-d``, ``-fd``, ``-df``)
- ``push`` with ``--d…`` (not ``--dry-run``), ``--m…``, ``--pr…`` (not ``--progress``/``--precise``), a short-flag
  cluster containing ``d``, or a delete refspec (``:ref`` / ``+:ref``)
- any ``update-ref`` (``-d``, ``--stdin`` and an empty new value all delete refs unchecked)
- subcommands match by unique prefix as git resolves them (``bra``, ``pus``, ``work``, ``update-r``)
- expansion syntax (``$``, ``{``, a backtick) anywhere in a command that mentions ``git``,
  ``branch``, ``push``, ``worktree`` or ``update-ref``: the shell may be assembling a destructive
  word (``-$D``, ``{-D,x}``, ``git br$(true)anch``) — rig sees the same string and decides

A payload whose ``args``/command has the wrong type is a DENY, not an allow.

There is NO env/inline/Telegram escape hatch: deny-by-default. The sanctioned alternatives are in
the deny text — inspect with `rig worktree verdict <target>`, push/merge the work, or rescue it
with `git diff --binary HEAD > ~/.cache/<repo>-recovery/<wt>-<ts>.patch` plus pushing a followup
branch.

Knobs (tests only): ``AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S`` overrides the 120 s rig timeout.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (fallbacks: args.cmd, command)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy = closed)
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 — fixed argv, no shell
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "block-unlanded-removal"

_DEFAULT_TIMEOUT_S = 120.0
_TIMEOUT_ENV = "AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S"
_DECISIONS = frozenset({"pass", "allow", "deny"})

_QUOTE_CHARS = re.compile(r"[\"'\\`]")
_DOLLAR_BEFORE_QUOTE = re.compile(r"\$(?=[\"'])")  # $'-D' / $"-D" (ANSI-C / locale quoting)
_SHELL_VARIABLE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")  # ${IFS}, $IFS, $HOME
# git resolves any unambiguous prefix of a subcommand (`git bra -D`, `git pus --delete`, `git work
# remove`, `git update-r`) as well as of a long option (`--d`, `--mirr`), so match prefixes and any
# short-flag cluster that CONTAINS d/D (`-df`, `-Df`, `-dr`). Loose on purpose: a false match costs
# one `rig` call, a false miss is a hole.
_WORKTREE_REMOVE = re.compile(r"\bw[a-z]*\s+(?:remove|rm)\b")
_BRANCH_DELETE = re.compile(r"\bbr[a-z]*\b[\s\S]*?(?:\s--d|\s-[A-Za-z]*[dD])")
# `--d(?!ry)`: `--dry-run` is by far the commonest benign push flag.
_PUSH_DELETE = re.compile(r"\bpus[a-z]*\b[\s\S]*?(?:\s--(?:d(?!ry)|m|pr(?!og|ec))|\s-[A-Za-z]*d|\s\+?:\S)")
# Every `update-ref` mutates refs (`-d`, `--stdin`, or an empty new value deletes), so any use matches.
_UPDATE_REF = re.compile(r"\bupdate-r[a-z]*\b")
# A word or flag can also be ASSEMBLED by the shell (`-$D`, `{-D,x}`, `git br$(true)anch`,
# `git -C . ${X:-branch}`, `` `printf -D` ``), which no static pattern sees. Expansion syntax
# anywhere in a command that mentions git or a ref-mutating word therefore matches (rig sees the
# same string and decides). Deliberately NOT scoped to a command segment: quoting/escaping can fake
# a separator. Quotes are removed first so `bra''nch` cannot dodge it.
_EXPANSION = re.compile(r"[$`{]")
_GIT_WORD = re.compile(r"\b(?:git|branch|push|worktree|update-ref)\b")
# `_QUOTE_CHARS` minus the backtick, which is an expansion signal here.
_QUOTES_ONLY = re.compile(r"[\"'\\]")
_LINE_CONTINUATION = re.compile(r"\\\n")

_RESCUE_HINT = (
    "Inspect with `rig worktree verdict <target>`. Sanctioned ways forward: push the work or "
    "merge it (then retry), or rescue it — `git diff --binary HEAD > "
    "~/.cache/<repo>-recovery/<wt>-<ts>.patch` and push a followup branch. "
    "There is NO bypass for this guard."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _patterns_match(flat: str) -> bool:
    return bool(
        _WORKTREE_REMOVE.search(flat)
        or _BRANCH_DELETE.search(flat)
        or _PUSH_DELETE.search(flat)
        or _UPDATE_REF.search(flat)
    )


def prefilter_matches(command: str) -> bool:
    """True iff `command` could remove a worktree or delete a branch (loose on purpose)."""
    # Normalize what the shell would: drop `\<newline>`, `$'-D'` -> `-D`, then an unexpanded
    # variable once as a word break (`git${IFS}branch`) and once as nothing (`git br${X}anch`),
    # then drop quotes and backslashes. `$(...)` is left alone.
    base = _DOLLAR_BEFORE_QUOTE.sub("", _LINE_CONTINUATION.sub("", command))
    for replacement in (" ", ""):
        if _patterns_match(_QUOTE_CHARS.sub("", _SHELL_VARIABLE.sub(replacement, base))):
            return True
    unquoted = _QUOTES_ONLY.sub("", base)
    return bool(_EXPANSION.search(command) and _GIT_WORD.search(unquoted))


def _timeout_s() -> float:
    raw = os.environ.get(_TIMEOUT_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return value if value > 0 else _DEFAULT_TIMEOUT_S


def _deny(detail: str) -> int:
    emit("block", f"BLOCKED — worktree/branch removal guard failed closed: {detail}\n{_RESCUE_HINT}")
    return BLOCK_EXIT_CODE


def _unsafe_targets(verdict: dict) -> list[str]:
    targets = verdict.get("targets")
    if not isinstance(targets, list):
        return []
    return [
        f"{t.get('kind', '?')}: {t.get('target', '?')}"
        for t in targets
        if isinstance(t, dict) and t.get("safe") is False
    ]


def _ask_rig(command: str, cwd: str) -> tuple[dict | None, str]:
    """Run `rig worktree guard`. Returns `(verdict, "")` on a well-formed answer, else
    `(None, why)` — every failure mode is reported, none is swallowed."""
    timeout = _timeout_s()
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["rig", "worktree", "guard", "--cwd", cwd],
            input=command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, "`rig` is not on PATH, so the removal cannot be verified"
    except subprocess.TimeoutExpired:
        return None, f"`rig worktree guard` timed out after {timeout:g}s"
    except OSError as exc:
        return None, f"could not run `rig worktree guard`: {exc}"

    if proc.returncode != 0:
        detail = proc.stderr.strip()[:500]
        return None, f"`rig worktree guard` exited {proc.returncode}" + (f": {detail}" if detail else "")
    try:
        verdict = json.loads(proc.stdout)
    except ValueError:
        return None, "`rig worktree guard` printed unparseable output"
    decision = verdict.get("decision") if isinstance(verdict, dict) else None
    if not isinstance(decision, str) or decision not in _DECISIONS:
        return None, "`rig worktree guard` returned an unrecognized verdict"
    return verdict, ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError as exc:
        return _deny(f"could not parse the hook event ({exc})")
    if not isinstance(event, dict):
        return _deny(f"hook event is not a JSON object ({type(event).__name__})")

    cwd = str(event.get("cwd") or os.getcwd())
    raw_args = event.get("args")
    if raw_args is not None and not isinstance(raw_args, dict):
        return _deny(f"hook event `args` is not an object ({type(raw_args).__name__})")
    args = raw_args or {}
    command = next(
        (v for v in (args.get("command"), args.get("cmd"), event.get("command")) if v not in (None, "")),  # 0/False/[] pass through on purpose: the type check denies them
        None,
    )
    if command is None:
        emit("allow")
        return 0
    if not isinstance(command, str):
        return _deny(f"hook event command is not a string ({type(command).__name__})")
    if not prefilter_matches(command):
        emit("allow")
        return 0

    verdict, why = _ask_rig(command, cwd)
    if verdict is None:
        warn(why)
        return _deny(why)

    decision = verdict["decision"]
    reason = verdict.get("reason")
    reason = reason if isinstance(reason, str) else ""
    if decision == "pass":
        emit("allow")
        return 0
    if decision == "allow":
        emit("allow", reason or None)
        return 0

    unsafe = _unsafe_targets(verdict)
    parts = [f"BLOCKED — {reason or 'rig refused this removal (no reason given)'}"]
    if unsafe:
        parts.append("Unsafe targets:\n" + "\n".join(f"  - {u}" for u in unsafe))
    parts.append(_RESCUE_HINT)
    emit("block", "\n".join(parts))
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
