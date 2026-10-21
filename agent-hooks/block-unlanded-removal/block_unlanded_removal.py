#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — allow removing only what is landed and clean; deny the rest.

Removing a worktree, deleting/clobbering a branch or recursively deleting files destroys work when
that work exists nowhere else (unpushed commits, unmerged branch, dirty tree). This hook is a thin,
FAIL-CLOSED shim in front of `rig worktree guard`, which is the ONLY brain: it parses the bash
command, resolves every removal target against real git plus the forge's PR state, and answers one
JSON object::

    {"decision": "pass"|"allow"|"deny", "reason": "...", "targets": [{"kind", "target", "safe"}]}

- ``pass``  — not a removal → this hook stays silent (allow).
- ``allow`` — every target is proven landed + clean → allow.
- ``deny``  — at least one target is unsafe, or the form is not safely parseable → block with
  rig's ``reason`` (which names the sanctioned rescue).

Pinned rig. The hook NEVER resolves `rig` through PATH or the environment. The descriptor carries
``"args": ["--rig", "<absolute path>"]``, written by the `rig apply` that provisioned the hook
(rig substitutes the shipped ``/ABSOLUTE/PATH/TO/rig`` placeholder with the launcher that ran it).
The hook accepts exactly that argv, requires an absolute path to an existing executable regular
file, and runs ``<pinned> --version``: the output must be ``rig X.Y.Z``. X.Y.Z is compared as an
integer tuple against MIN_RIG_VERSION (0.64.0, the first rig with `worktree guard`).

Prefilter (deliberately loose — rig decides precisely; a false match costs one `rig` call, a
false miss is a hole). Matched on a COPY of the command normalized like the shell would
(``$'-D'`` -> ``-D``, ``${IFS}``/``$VAR`` -> a space and again as nothing, then every quote and
backslash removed), so `g''it branch -D x` / `git wor\\ktree remove x` / `git${IFS}branch${IFS}-D`
cannot dodge it. It sorts a command into one of two classes.

STRICT — removal-shaped on its face:

- ``worktree (remove|rm)``
- ``branch`` followed later by ``--d…`` (any prefix of ``--delete`` and friends) or a short-flag
  cluster containing ``d``/``D``; or a force/move/copy flag: ``--f…``, ``--m…``, ``--c…`` (not the
  full ``--format``/``--merged``/``--contains``/``--color``/``--column``/``--create-reflog``) or a
  cluster containing ``f``/``M``/``C``/``m``/``c`` (``-m``/``-c`` never overwrite without ``-f`` and
  rig answers ``pass``; matching them costs one rig call)
- ``checkout`` with a short-flag cluster containing ``B``; ``switch`` with ``--f…`` or a cluster
  containing ``C`` (``--force-create``); ``-b``/``-c`` are not matched
- ``rm`` as a command word (``sudo rm``, ``xargs rm``, ``\\rm``, ``/bin/rm``, ``command rm``) with
  ``-r``/``-R``/``--r…`` or a cluster containing ``r``/``R``; ``form``, ``arm``, ``rmdir`` and
  ``docker run --rm`` are not the word ``rm``
- ``push`` with ``--d…`` (not ``--dry-run``), ``--m…``, ``--pr…`` (not ``--progress``/``--precise``),
  a short-flag cluster containing ``d``, or a delete refspec (``:ref`` / ``+:ref``)
- any ``update-ref`` (``-d``, ``--stdin`` and an empty new value all delete refs unchecked)
- subcommands match by unique prefix as git resolves them (``bra``, ``pus``, ``work``, ``update-r``)

LOOSE — matched ONLY by the expansion clause: expansion syntax (``$``, ``{``, a backtick) anywhere
in a command that mentions ``git``, ``branch``, ``push``, ``worktree``, ``update-ref`` or the ``rm``
command word. The shell may be assembling a destructive word (``-$D``, ``{-D,x}``,
``git br$(true)anch``, ``R=r; rm -$R x``) — rig sees the same string and decides. Every heredoc
``git commit -m "$(cat <<EOF ...)"`` and every ``rm "$tmp"`` is LOOSE.

No match → allow, and no rig is spawned (not even for the version probe). What the rig state does
to a matched command:

- current pinned rig (>= MIN_RIG_VERSION): STRICT and LOOSE both go to `rig worktree guard`;
- pinned rig OLDER than MIN_RIG_VERSION (has no `worktree guard`): STRICT is denied with an
  "upgrade rig" message; LOOSE is ALLOWED with a one-line stderr warning (the old behaviour — a
  stale rig must not wedge every heredoc commit);
- no usable pin (``--rig`` absent — descriptor installed by an older rig — not absolute, missing,
  not executable) or a version that cannot be verified (probe fails, times out, unparseable): BOTH
  classes are denied, naming the problem and the fix (`rig apply commit`). Deliberate fail closed.

Anything else — nonzero guard exit, timeout, unparseable/foreign JSON, an unknown decision — is a
DENY with an explicit message. A payload whose ``args``/command has the wrong type is a DENY too.

There is NO env/inline/Telegram escape hatch: deny-by-default. The sanctioned alternatives are in
the deny text — inspect with `rig worktree verdict <target>`, merge the work, or push the commits to
a branch that is not being deleted (`archive/<name>`: a commit held by another remote branch that
stays counts as preserved), plus `git diff --binary HEAD > ~/.cache/<repo>-recovery/<wt>-<ts>.patch`
for uncommitted work.

Knobs (tests only): ``AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S`` overrides the 120 s guard timeout,
``AGENT_TOOLS_REMOVAL_GUARD_VERSION_TIMEOUT_S`` the 10 s `--version` probe timeout.

Contract (agents-hooks/v1):
  argv   : ``--rig <absolute path to rig>`` (the descriptor's ``args``)
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

# The first rig release that has `rig worktree guard`.
MIN_RIG_VERSION = (0, 64, 0)
_MIN_RIG_VERSION_TEXT = ".".join(str(n) for n in MIN_RIG_VERSION)

_DEFAULT_TIMEOUT_S = 120.0
_TIMEOUT_ENV = "AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S"
_DEFAULT_VERSION_TIMEOUT_S = 10.0
_VERSION_TIMEOUT_ENV = "AGENT_TOOLS_REMOVAL_GUARD_VERSION_TIMEOUT_S"
_DECISIONS = frozenset({"pass", "allow", "deny"})
_RIG_VERSION_OUTPUT = re.compile(r"rig (\d+)\.(\d+)\.(\d+)(?:[-+]\S*)?")

STRICT = "strict"
LOOSE = "loose"

_QUOTE_CHARS = re.compile(r"[\"'\\`]")
_DOLLAR_BEFORE_QUOTE = re.compile(r"\$(?=[\"'])")  # $'-D' / $"-D" (ANSI-C / locale quoting)
_SHELL_VARIABLE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")  # ${IFS}, $IFS, $HOME
# git resolves any unambiguous prefix of a subcommand (`git bra -D`, `git pus --delete`, `git work
# remove`, `git update-r`) as well as of a long option (`--d`, `--mirr`), so match prefixes and any
# short-flag cluster that CONTAINS the letter (`-df`, `-Df`, `-dr`). Loose on purpose: a false match
# costs one `rig` call, a false miss is a hole.
_WORKTREE_REMOVE = re.compile(r"\bw[a-z]*\s+(?:remove|rm)\b")
# delete (`-d`/`-D`/`--d…`), force (`-f`/`--f…`), move (`-m`/`-M`/`--m…`), copy (`-c`/`-C`/`--c…`).
# `-m`/`-c` without force never overwrite (rig answers `pass`), but a cluster can hide a `-f`. The
# negative lookaheads skip the full benign long options (`--format`, `--merged`, `--contains`,
# `--color`, `--column`, `--create-reflog`); a prefix of one (`--merg`) still matches.
_BRANCH_MUTATE = re.compile(
    r"\bbr[a-z]*\b[\s\S]*?(?:"
    r"\s--(?:d|f(?!ormat\b)|m(?!erged\b)|c(?!ontains\b|olor\b|olumn\b|reate-reflog\b))"
    r"|\s-[A-Za-z]*[dDfMCmc])"
)
# `git checkout -B` resets an existing branch; `git switch -C`/`--force-create` does the same. `chec`
# is the shortest unambiguous `checkout` prefix. `switch` has no other `sw…` subcommand.
_CHECKOUT_RESET = re.compile(r"\bchec[a-z]*\b[\s\S]*?\s-[A-Za-z]*B")
_SWITCH_RESET = re.compile(r"\bsw[a-z]*\b[\s\S]*?(?:\s--f|\s-[A-Za-z]*C)")
# `rm` as a command word (not `form`/`arm`/`rmdir`/`docker --rm`, but `sudo rm`, `/bin/rm`, `$(which
# rm)`) followed anywhere later by a recursive flag: `-r`/`-R`/a cluster, or `--r…`.
_RM_RECURSIVE = re.compile(r"(?<![\w.-])rm(?=[\s)])[\s\S]*?\s(?:--r|-[A-Za-z]*[rR])")
# `--d(?!ry)`: `--dry-run` is by far the commonest benign push flag.
_PUSH_DELETE = re.compile(r"\bpus[a-z]*\b[\s\S]*?(?:\s--(?:d(?!ry)|m|pr(?!og|ec))|\s-[A-Za-z]*d|\s\+?:\S)")
# Every `update-ref` mutates refs (`-d`, `--stdin`, or an empty new value deletes), so any use matches.
_UPDATE_REF = re.compile(r"\bupdate-r[a-z]*\b")
_STRICT_PATTERNS = (
    _WORKTREE_REMOVE,
    _BRANCH_MUTATE,
    _CHECKOUT_RESET,
    _SWITCH_RESET,
    _RM_RECURSIVE,
    _PUSH_DELETE,
    _UPDATE_REF,
)
# A word or flag can also be ASSEMBLED by the shell (`-$D`, `{-D,x}`, `git br$(true)anch`,
# `git -C . ${X:-branch}`, `` `printf -D` ``, `R=r; rm -$R x`), which no static pattern sees.
# Expansion syntax anywhere in a command that mentions git, a ref-mutating word or the `rm` command
# word therefore matches (rig sees the same string and decides; `rm "$tmp"` costs one rig call).
# Deliberately NOT scoped to a command segment: quoting/escaping can fake a separator. Quotes are
# removed first so `bra''nch` cannot dodge it.
_EXPANSION = re.compile(r"[$`{]")
_GIT_WORD = re.compile(r"\b(?:git|branch|push|worktree|update-ref)\b|(?<![\w.-])rm\b")
# `_QUOTE_CHARS` minus the backtick, which is an expansion signal here.
_QUOTES_ONLY = re.compile(r"[\"'\\]")
_LINE_CONTINUATION = re.compile(r"\\\n")

_RESCUE_HINT = (
    "Inspect with `rig worktree verdict <target>`. Sanctioned ways forward: merge it (then retry), "
    "or push the commits to a branch that is not being deleted — `git push origin "
    "<branch>:refs/heads/archive/<name>` — and retry (a commit held by another remote branch that "
    "stays counts as preserved); uncommitted work: `git diff --binary HEAD > "
    "~/.cache/<repo>-recovery/<wt>-<ts>.patch`, commit it and push it to such a branch. "
    "There is NO bypass for this guard."
)
_PIN_FIX_HINT = (
    "Fix: run `rig apply commit` with a current rig; it re-provisions this hook with its own absolute "
    "`--rig` path. This hook never looks `rig` up on PATH. There is NO bypass for this guard."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _strict_match(flat: str) -> bool:
    return any(pattern.search(flat) for pattern in _STRICT_PATTERNS)


def classify(command: str) -> str | None:
    """`STRICT` if `command` is removal-shaped on its face, `LOOSE` if it only contains shell
    expansion syntax next to a git word (it could assemble a removal), else `None`. Loose on purpose."""
    # Normalize what the shell would: drop `\<newline>`, `$'-D'` -> `-D`, then an unexpanded
    # variable once as a word break (`git${IFS}branch`) and once as nothing (`git br${X}anch`),
    # then drop quotes and backslashes. `$(...)` is left alone.
    base = _DOLLAR_BEFORE_QUOTE.sub("", _LINE_CONTINUATION.sub("", command))
    for replacement in (" ", ""):
        if _strict_match(_QUOTE_CHARS.sub("", _SHELL_VARIABLE.sub(replacement, base))):
            return STRICT
    unquoted = _QUOTES_ONLY.sub("", base)
    if _EXPANSION.search(command) and _GIT_WORD.search(unquoted):
        return LOOSE
    return None


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _deny(detail: str) -> int:
    emit("block", f"BLOCKED — worktree/branch removal guard failed closed: {detail}\n{_RESCUE_HINT}")
    return BLOCK_EXIT_CODE


def _deny_untrusted_rig(problem: str) -> int:
    emit(
        "block",
        f"BLOCKED — {HOOK_ID} cannot verify this command, so it fails closed: {problem}\n{_PIN_FIX_HINT}",
    )
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


def _pinned_rig(argv: list[str]) -> tuple[str | None, str]:
    """The rig the provisioning `rig apply` pinned: exactly `--rig <absolute path>`. Returns
    `(path, "")` or `(None, why)`. No PATH lookup, no environment override, no fallback."""
    if len(argv) != 2 or argv[0] != "--rig":
        return None, (
            "the descriptor passes no `--rig <absolute path>` (installed by a rig older than the "
            "pinned-path release, or edited by hand)"
        )
    path = argv[1]
    if not os.path.isabs(path):
        return None, f"the pinned rig path `{path}` is not absolute"
    if not os.path.isfile(path):
        return None, f"the pinned rig `{path}` does not exist or is not a regular file"
    if not os.access(path, os.X_OK):
        return None, f"the pinned rig `{path}` is not executable"
    return path, ""


def _rig_version(rig: str) -> tuple[tuple[int, int, int] | None, str]:
    """Run `<rig> --version`. Returns `(version, "")` or `(None, why)` when it cannot be verified."""
    timeout = _env_seconds(_VERSION_TIMEOUT_ENV, _DEFAULT_VERSION_TIMEOUT_S)
    try:
        proc = subprocess.run(  # noqa: S603 — pinned absolute path, fixed argv, no shell
            [rig, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"`{rig} --version` timed out after {timeout:g}s, so the rig version cannot be verified"
    except OSError as exc:
        return None, f"could not run `{rig} --version` ({exc}), so the rig version cannot be verified"
    lines = proc.stdout.strip().splitlines()
    match = _RIG_VERSION_OUTPUT.fullmatch(lines[0].strip()) if lines else None
    if proc.returncode != 0 or match is None:
        return None, (
            f"`{rig} --version` did not print `rig X.Y.Z` (exit {proc.returncode}), so the rig version "
            "cannot be verified"
        )
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch)), ""


def _ask_rig(rig: str, command: str, cwd: str) -> tuple[dict | None, str]:
    """Run `<rig> worktree guard`. Returns `(verdict, "")` on a well-formed answer, else
    `(None, why)` — every failure mode is reported, none is swallowed."""
    timeout = _env_seconds(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S)
    try:
        proc = subprocess.run(  # noqa: S603 — pinned absolute path, fixed argv, no shell
            [rig, "worktree", "guard", "--cwd", cwd],
            input=command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
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


def main(argv: list[str] | None = None) -> int:
    hook_argv = sys.argv[1:] if argv is None else argv
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
    shape = classify(command)
    if shape is None:
        emit("allow")
        return 0

    rig, why = _pinned_rig(hook_argv)
    if rig is None:
        warn(why)
        return _deny_untrusted_rig(why)
    version, why = _rig_version(rig)
    if version is None:
        warn(why)
        return _deny_untrusted_rig(why)
    if version < MIN_RIG_VERSION:
        shown = ".".join(str(n) for n in version)
        stale = (
            f"the pinned rig `{rig}` is {shown}, older than {_MIN_RIG_VERSION_TEXT}, and has no "
            "`worktree guard`"
        )
        if shape == LOOSE:
            warn(f"{stale}; allowing a command that only matched the shell-expansion prefilter. Upgrade rig.")
            emit("allow")
            return 0
        emit(
            "block",
            f"BLOCKED — {stale}, so this removal-shaped command cannot be verified. Upgrade rig "
            "(`rig apply commit` / update the rig checkout) and retry. There is NO bypass for this guard.",
        )
        return BLOCK_EXIT_CODE

    verdict, why = _ask_rig(rig, command, cwd)
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
