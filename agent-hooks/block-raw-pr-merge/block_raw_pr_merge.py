#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw `gh pr merge` that bypasses ship.

Denies a shell command that merges a PR directly with `gh pr merge` (including
`gh pr merge --admin`), because that skips the project's ship gate: `gh ship <PR>`
(the green-CI-gated merge + mandatory-screenshot command) is the only sanctioned path.
A raw merge lands code without the green-CI check and the required-screenshot check —
exactly the gates ship exists to enforce.

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
occurring.

Allowed (let through):
  - `gh ship <PR>` / a `gh alias` that runs ship
  - `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
  - any non-merge `gh pr` subcommand (view, list, checkout, create, comment, ...)
  - commands whose body/arguments merely contain the text "gh pr merge" as a string

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

import json
import os
import re
import shlex
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parents[2] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import agenttools_hatch_escalation as hatch_escalation  # noqa: E402

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Shell operators that separate independent command segments in a compound command line.
_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})

# Shell grouping/control-flow tokens that may precede the real command name in a segment.
# E.g. `( gh pr merge 5 )` or `{ gh pr merge 5; }`.
_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "then", "do", "else", "elif"})

# A VAR=value inline environment assignment that may precede the executable.
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Merge-hint pattern: used to gate fail-closed on unbalanced-quote parse errors so that
# a benign command with an unbalanced quote (e.g. `grep won't file`) is NOT blocked just
# because shlex can't parse it.  Only if the raw string plausibly contains a merge
# invocation do we treat a parse error as fail-closed.
_MERGE_HINT = re.compile(r"\bgh\b.*\bpr\b.*\bmerge\b", re.DOTALL)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-pr-merge: {msg}\n")


def _split_segments(command: str) -> list[list[str]]:
    """Tokenize `command` via shlex and split into per-segment token lists.

    Uses ``punctuation_chars=True`` so that `;`, `&&`, `||`, `|` etc. are
    returned as standalone separator tokens rather than being glued to adjacent
    words (the default shlex.split() behaviour leaves `done;` as one token,
    missing the `;` separator entirely).

    Each segment is one independent command (the shell atoms between separators).
    Returns a list of token lists; raises ValueError on unbalanced quotes
    (caller treats this as fail-closed).

    A `\`-newline line continuation is first folded to a space, and a remaining BARE newline is
    normalized to `;` — in a real shell a newline separates commands (like `;`), but shlex
    consumes it as whitespace, so without this `echo ok`+newline+`gh pr merge` collapses into one
    `echo`-headed segment and the raw merge on the second line evades detection. shlex keeps a
    newline inside quotes literal, so neither normalization splits a quoted argument.
    """
    command = command.replace("\\\n", " ").replace("\n", ";")
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = False
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


def _is_gh_pr_merge(segment: list[str]) -> bool:
    """Return True iff this segment's argv is a `gh pr merge` invocation.

    Uses ``os.path.basename`` on argv[0] so that path-qualified invocations
    such as ``/opt/homebrew/bin/gh pr merge 5`` are correctly detected.
    """
    argv = _segment_argv(segment)
    return (
        len(argv) >= 3
        and os.path.basename(argv[0]) == "gh"
        and argv[1] == "pr"
        and argv[2] == "merge"
    )


def _command_contains_gh_pr_merge(command: str) -> bool | None:
    """Return True if any parsed segment of `command` is a `gh pr merge` call.

    Returns None (fail-closed) when the command cannot be parsed AND the raw
    text looks like a merge invocation.  Commands whose parse fails but contain
    no merge-like pattern (e.g. ``grep won't file`` with an unbalanced quote)
    return False so they are not spuriously blocked.
    """
    try:
        segments = _split_segments(command)
    except ValueError:
        # Fail-closed ONLY if the raw text plausibly contains a merge attempt.
        if _MERGE_HINT.search(command):
            return None
        return False
    return any(_is_gh_pr_merge(seg) for seg in segments)


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
