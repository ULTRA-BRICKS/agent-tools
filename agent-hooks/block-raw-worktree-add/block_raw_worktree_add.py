#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw `git worktree add`/`remove` outside `.worktrees/`.

Incident this closes (agent-tools#608, Alex, 2026-09-07): rig-cli's own AGENTS.md documents
`rig worktree create <name>` as "the ONLY way an agent (or a human) should create a worktree in
a rig-managed repo, and `<repo>/.worktrees/<name>` is the ONLY location" — citing GH-329, a real
incident where ~70 leftover worktrees accumulated across at least five different ad-hoc
locations (a bare in-repo `.worktrees/`, a sibling-of-repo `.worktrees/`, Claude Code's own
`.claude/worktrees/`, `<repo>-worktrees/`, `<repo>-wt-*`), none tracked, none ever cleaned up,
tens of GB of duplicated `node_modules`/`.venv`. That rule was DOCUMENTATION ONLY — nothing
stopped a raw `git worktree add <path-outside-.worktrees/>` from an agent or a script, and the
exact five-conventions problem was confirmed still happening, live, the same day this hook was
written (`git worktree list` in `~/xp/rig-cli` showed worktrees in `/private/tmp`, a sibling
`-worktrees/` dir, a `-wt-*` dir, and `.claude/worktrees/agent-*`).

This hook closes that gap directly: it inspects `git worktree add` / `git worktree remove`
invocations and BLOCKS one whose target path does not resolve under the repo's
`<repo-root>/.worktrees/` — the one location rig's own reconciler (`rig worktree gc`) and
sibling commands (`rig worktree create`/`remove`) know about. `git worktree list`, `git worktree
lock`/`unlock`/`move`/`repair`, and `git worktree prune` are all unaffected (see "Deliberately
out of scope" below).

Why `rig worktree create`'s OWN internal `git worktree add` call is never blocked — no
process-ancestry trust needed: `rig worktree create` is a plain Python CLI (`riglib/worktree.py`,
`_run_git_worktree_add`) that calls `subprocess.run(["git", "-C", repo_root, "worktree", "add",
...])` directly from inside the `rig` process. That call is never dispatched through the AGENT's
own Bash tool — it is an ordinary OS subprocess spawned by `rig`'s own code, and agents-hooks/v1
`pre-bash` only ever fires on a command the harness is about to run *because the agent asked it
to* (the event this hook receives carries the literal Bash-tool command string). So when an agent
runs `rig worktree create <name>`, the event this hook sees is the literal text `"rig worktree
create <name>"` — it does not parse as `git ... worktree add ...` at all, `_classify_git_segment`
returns None, and the hook has nothing to gate. `rig`'s subsequent internal `git worktree add`
call happens entirely inside `rig`'s own process, invisible to any agent-hook dispatcher (which
only wraps the agent harness's tool-call boundary, not arbitrary child processes a already-allowed
command goes on to spawn). This was verified by reading `riglib/worktree.py` before writing this
hook — there is no separate identity check to build or maintain, and none to keep in sync as
`rig` evolves, as long as `rig worktree create` keeps calling git directly rather than shelling
out through something that re-enters the agent's own Bash tool (it does not, and has no reason
to).

Deliberately out of scope: `git worktree prune` is NOT gated. `prune` takes no path argument at
all — it is git's own cleanup step, functionally the same direction as `rig worktree gc` (it
REMOVES stale administrative state, it doesn't create sprawl). Gating it would add a second
detection shape for zero benefit against GH-329's actual failure mode (worktrees landing in the
wrong PLACE), and it would fight `rig worktree gc`'s own remediation path. `git worktree
lock`/`unlock`/`move`/`repair`/`list` don't create or relocate a worktree relative to the repo
root the way `add`/`remove` do and are likewise out of scope.

Detection is ARGV-BASED (shlex, chain-aware — `_split_chain` is copied from
`pin-primary-worktree/pin_primary_worktree.py`, itself SYNC'd with
`orchestrator-stays-thin/orchestrator_stays_thin.py._split_chain`), not a substring match:
`_classify_git_segment` (also copied from pin-primary-worktree) finds the real `git` invocation
past any leading `VAR=value` assignments and global git options (`-C`, `-c`, ...), honoring a
`-C <dir>` override so a cross-repo `git -C <other-repo> worktree add ...` is judged against
THAT repo. `_worktree_target` then walks the `worktree add`/`worktree remove` argv, skipping
recognized flags (`-b`/`-B`/`--reason` for `add`, which take a value; `-f`/`--force` and every
other boolean flag for both), to find the first bare positional token — the target path — in
EITHER flag order (`git worktree add -b feat path` and `git worktree add path -b feat` both
resolve the same target).

Repo-root resolution reuses the SAME git primitive pin-primary-worktree already established for
this exact hook family: `git rev-parse --git-common-dir` resolves to the primary worktree's real
`.git` directory REGARDLESS of which worktree (primary or linked) the command is run from — so
`<git-common-dir>.parent` is always the repo's single, unambiguous root, and
`<repo-root>/.worktrees/` is always the same directory no matter where the agent's cwd happens
to be. This hook does not need pin-primary-worktree's primary-vs-linked DISTINCTION (it never
asks "is this worktree the primary one"), only the root-resolution primitive underneath it.

Per rig-cli's own AGENTS.md ("Repos that AREN'T rig-managed ... shouldn't be affected — this
only fires where the hook is actually installed via catalog/rig apply, so no repo-detection
logic needed inside the hook itself"), this hook carries NO opt-in flag and no repo-shape
detection of its own — it is installed everywhere the agent-tools catalog is applied
(`agent_hooks.all: true`, the default), exactly like `block-raw-pr-merge`/`block-no-verify`, and
is a no-op wherever it is not installed.

External approval (same pattern as `block-raw-pr-merge`, copied verbatim): there is no
self-service env-var bypass. The block is deny-by-default. For a genuine one-time exception —
migrating a pre-existing worktree, a documented non-agent git-worktree use case that legitimately
lives elsewhere — request a single Telegram approval via
`RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="<written justification>"`, which routes one approval
to Alex (deny-by-default; a bare `1`/`true` is rejected). Only an approved `tg-ctl ask` (exit 0)
allows the raw command through.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command (a few fallbacks below)
  stdout : protocol JSON only
  stderr : human logs
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": this is workflow discipline (WHERE a worktree lands), not a security
boundary (no data loss, no CI/review bypass) — a git/parse failure must never wedge the agent's
ability to run an unrelated command. This deliberately follows the sibling worktree-family hooks
(`pin-primary-worktree`, `worktree-only-writes`, both `on_error: open`) rather than
`block-raw-pr-merge`'s `on_error: closed` — that hook guards an irreversible security boundary
(a merge that skips CI/review gates); this one guards a hygiene convention whose failure mode is
"a worktree in the wrong directory", not a blast-radius incident.

Known, deliberate scope limits (heuristic, not a sandbox — same tradeoff as the rest of this
hook family): a `cd other-repo && git worktree add X` chain is judged against the ORIGINAL cwd's
repo root unless the segment itself carries `git -C <dir>` (which IS honored); an unrecognized
FUTURE git-worktree flag that happens to take a value not in this hook's known value-flag set
could be misread as the target path (or vice versa) — the same class of residual every
argv-heuristic hook in this family accepts and documents rather than hand-parsing git's full
option grammar.

Known, DELIBERATELY UNCLOSED bypasses (documented, not silently accepted — a reviewer or a future
change should see these named, not discover them):

- A `git worktree add`/`remove` hidden behind a shell interpreter or `eval` — `bash -c 'git
  worktree add /tmp/x'`, `sh -c "..."`, `eval "git worktree add /tmp/x"` — is NOT detected:
  `_classify_git_segment` sees `bash`/`sh`/`eval` as the invoked head, not `git`, and this hook
  (unlike `block-raw-pr-merge`, which re-scans a wrapper/`-c`-string body for its narrower merge
  detector) does not re-scan interpreter argument strings. This is the SAME residual
  `pin-primary-worktree` already accepts for `git checkout`/`switch`; closing it here would mean
  porting `block-raw-pr-merge`'s `_SHELL_INTERPRETERS`/`_SHELL_C_OPT` machinery, deferred to a
  follow-up rather than scope-creeping this hook's first cut.
- `EnterWorktree` / any harness-NATIVE worktree-creation path (e.g. Claude Code's own
  `.claude/worktrees/agent-*` convention) is not a Bash tool call at all — this hook, like every
  agents-hooks/v1 `pre-bash` hook, only ever sees a command the agent explicitly runs via its own
  Bash tool. It cannot see, and does not attempt to gate, a harness feature that creates a
  worktree through its own separate mechanism. That is one of the five conventions the live
  incident this hook responds to actually observed.
- A `.worktrees` directory that is ALREADY a symlink pointing outside the repo root would let a
  target that resolves "under `.worktrees/`" actually land elsewhere; `_worktrees_dir_escapes_repo`
  closes this specific case (mirrors `riglib/worktree.py::_target_escapes_repo`) by refusing to
  treat ANY target as sanctioned once `.worktrees` itself is such a symlink — so this one is
  closed, not merely documented, but is called out here because it is the kind of gap this class
  of hook is prone to and worth a reviewer's explicit attention.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess  # noqa: S404 — fixed git argv, no shell
import sys
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
HOOK_ID = "block-raw-worktree-add"

_GIT_TIMEOUT_S = 3.0
_WORKTREES_DIR_NAME = ".worktrees"  # SYNC: riglib/config.py::WORKTREES_DIR_NAME in rig-cli


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


# ── git plumbing (SYNC: copied from pin-primary-worktree/pin_primary_worktree.py — each
# agent-hook is a standalone script by convention, no shared import between hook dirs) ──────────

def _git(cwd: str, *args: str) -> str | None:
    """Run a read-only ``git -C cwd <args>`` and return trimmed stdout, or None on any failure."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, no shell
            ["git", "-C", cwd or ".", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"git {' '.join(args)} failed: {exc}")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_path(cwd: str, *rev_parse_args: str) -> Path | None:
    out = _git(cwd, "rev-parse", *rev_parse_args)
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    try:
        return p.resolve()
    except OSError:
        return None


def repo_root(cwd: str) -> Path | None:
    """The repo's single, unambiguous root directory, resolved the SAME way regardless of
    whether ``cwd`` sits in the primary worktree or a linked one (see module docstring): `git
    rev-parse --git-common-dir` always resolves to the primary worktree's real `.git`. Returns
    None (undetermined) on any git/resolve failure, or when the resolved `--git-common-dir`
    isn't a `.git`-named directory (a bare repo — out of scope, fails open rather than guessing
    a wrong root)."""
    common_dir = _git_path(cwd, "--git-common-dir")
    if common_dir is None or common_dir.name != ".git":
        return None
    return common_dir.parent


# ── bash chain parsing (SYNC: copied from pin-primary-worktree/pin_primary_worktree.py, itself
# SYNC'd with orchestrator-stays-thin/orchestrator_stays_thin.py._split_chain) ──────────────────

def _split_chain(command: str) -> list[str]:
    """Split on shell operators (&&, ||, ;, |, newline, a bare control &) that lie OUTSIDE quotes."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        prev = command[i - 1] if i > 0 else ""
        nxt = command[i + 1] if i + 1 < n else ""
        if quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
        elif command[i:i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
        elif c == "&" and prev not in ("&", ">") and nxt not in ("&", ">"):
            segs.append("".join(buf))
            buf = []
            i += 1
        elif c in (";", "|", "\n"):
            segs.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    segs.append("".join(buf))
    return [s for s in segs if s.strip()]


# ── git worktree add/remove classification ───────────────────────────────────────────────────

_GIT_GLOBAL_OPT_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)
# A leading shell assignment (`VAR=val`) in front of the real command.
_ASSIGN_RE = re.compile(r"^\w+=")


def _classify_git_segment(
    segment: str,
) -> tuple[str, list[str], str | None] | None:
    """If ``segment``'s head is ``git`` (after any leading assignments / global options), return
    ``(subcommand, rest_tokens, -C override)``, else None. SYNC: copied from
    pin-primary-worktree/pin_primary_worktree.py._classify_git_segment (same rationale: leading
    `VAR=val` assignments and global `-C`/`-c`/... options must be skipped to find the real `git`
    subcommand and to honor a cross-repo `-C <dir>` override)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    if i >= len(toks) or toks[i].rsplit("/", 1)[-1] != "git":
        return None
    i += 1
    cwd_override: str | None = None
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        if opt == "-C" and i + 1 < len(toks):
            cwd_override = toks[i + 1]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):
            i += 1
    if i >= len(toks):
        return None
    return toks[i], toks[i + 1:], cwd_override


# Flags that consume the NEXT token as a value, per `worktree add`'s own grammar (`-b <branch>`,
# `-B <branch>`, `--lock --reason <string>`). Everything else `-`-prefixed is treated as boolean
# (`-f`/`--force`, `--detach`, `--checkout`/`--no-checkout`, `--lock`, `--orphan`,
# `--guess-remote`/`--no-guess-remote`, `--track`/`--no-track`, `-q`/`--quiet`, ...).
_ADD_VALUE_FLAGS = frozenset({"-b", "-B", "--reason"})
# `worktree remove` has no value-taking flags of its own (`-f`/`--force` is boolean).
_REMOVE_VALUE_FLAGS: frozenset[str] = frozenset()


def _worktree_target(toks: list[str], value_flags: frozenset[str]) -> str | None:
    """The first bare positional token in `toks` (the args after `worktree add`/`worktree
    remove`) — the target path/worktree, in EITHER flag order. Skips a recognized value flag and
    its following token, a `--long=value` glued flag, and any other `-`-prefixed boolean flag; a
    literal `--` ends flag scanning (everything after is positional). Returns None if there is no
    positional token at all (an invalid/incomplete invocation git itself would reject)."""
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t == "--":
            i += 1
            break
        if t in value_flags and i + 1 < n:
            i += 2
            continue
        if t.startswith("--") and "=" in t:
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            i += 1
            continue
        return t
    return toks[i] if i < n else None


def _resolve_effective_cwd(base_cwd: str, cwd_override: str | None) -> str:
    if not cwd_override:
        return base_cwd
    p = Path(cwd_override)
    if not p.is_absolute():
        p = Path(base_cwd or ".") / p
    return str(p)


def _resolve_target(eff_cwd: str, target: str) -> Path:
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = Path(eff_cwd or ".") / target_path
    return target_path.resolve(strict=False)


def _worktrees_dir_escapes_repo(root: Path) -> bool:
    """True iff `<root>/.worktrees` is itself a symlink resolving OUTSIDE `root` — the one way
    the "target resolves under `.worktrees/`" check below could be gamed (mirrors
    `riglib/worktree.py::_target_escapes_repo`'s own reasoning: the only way a target under
    `<root>/.worktrees/<name>` could land outside `root` is if `.worktrees` ITSELF is a symlink
    pointing elsewhere — `.worktrees` not existing yet, or being a plain directory, cannot
    escape). A dangling symlink is treated as escaped too — not a legitimate `.worktrees`,
    don't trust it either way."""
    candidate = root / _WORKTREES_DIR_NAME
    if not candidate.is_symlink():
        return False
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return True
    try:
        resolved.relative_to(root.resolve())
        return False
    except ValueError:
        return True


def _target_is_sanctioned(root: Path, resolved_target: Path) -> bool:
    """True iff `resolved_target` resolves under `<root>/.worktrees/` AND `.worktrees` itself
    isn't a symlink escaping `root` (see `_worktrees_dir_escapes_repo`)."""
    if _worktrees_dir_escapes_repo(root):
        return False
    allowed_dir = (root / _WORKTREES_DIR_NAME).resolve()
    try:
        resolved_target.relative_to(allowed_dir)
        return True
    except ValueError:
        return False


MESSAGE = (
    "BLOCKED — raw `git worktree {kind}` outside `{worktrees_dir}/` is not the sanctioned door "
    "(agent-tools#608 / rig-cli AGENTS.md: a real incident — GH-329 — left ~70 leftover "
    "worktrees scattered across five different ad-hoc locations, none tracked, tens of GB of "
    "duplicated node_modules/.venv). `{repo_worktrees}` is the ONLY sanctioned location.\n"
    "Use the rig door instead:\n"
    "    rig worktree {rig_verb} <name>\n"
    "or, if `{target}` is a worktree that predates this convention, `rig worktree gc` to "
    "reconcile it.\n"
    "There is NO self-service bypass. If you have a genuine one-off reason (migrating an "
    "existing worktree, a documented non-agent use case), ASK the human — request a single "
    'Telegram approval via RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="<why>" (deny-by-default; a '
    "bare `1` is rejected)."
)


def _block_message(kind: str, root: Path, target: Path) -> str:
    return MESSAGE.format(
        kind=kind,
        worktrees_dir=_WORKTREES_DIR_NAME,
        repo_worktrees=str(root / _WORKTREES_DIR_NAME),
        rig_verb="create" if kind == "add" else "remove",
        target=target,
    )


def _classify_worktree_call(
    segment: str, base_cwd: str
) -> tuple[str, Path, Path, str] | None:
    """If `segment` is a `git worktree add`/`remove` invocation with a resolvable target,
    return `(kind, root, resolved_target, eff_cwd)`; else None (nothing to gate — see
    `_evaluate_worktree_segment`'s docstring for every "nothing to gate" case)."""
    classified = _classify_git_segment(segment)
    if classified is None:
        return None
    subcommand, rest, cwd_override = classified
    if subcommand != "worktree" or not rest:
        return None
    kind = rest[0]
    if kind not in ("add", "remove"):
        return None
    value_flags = _ADD_VALUE_FLAGS if kind == "add" else _REMOVE_VALUE_FLAGS
    target = _worktree_target(rest[1:], value_flags)
    if target is None:
        return None

    eff_cwd = _resolve_effective_cwd(base_cwd, cwd_override)
    root = repo_root(eff_cwd)
    if root is None:
        return None  # undetermined repo root → fail open on this segment
    return kind, root, _resolve_target(eff_cwd, target), eff_cwd


def _evaluate_worktree_segment(
    segment: str, base_cwd: str, full_command: str
) -> tuple[str, str] | None:
    """Evaluate ONE chained-command segment. Returns None if it needs no gating (not `git`, not
    `worktree add`/`worktree remove`, no resolvable target, repo root undetermined, or the target
    already resolves under `<repo-root>/.worktrees/`). Returns `("block", message)` when it must
    block, `("approved", note)` when a hatch escalation approved it.

    The hatch escalation is asked with the FULL `full_command` (not just this segment) — the
    human approver sees the whole chained command, and an inline
    `RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="why" <cmd>` prefix on ANY segment of it is found
    (mirrors `pin-primary-worktree`)."""
    classified = _classify_worktree_call(segment, base_cwd)
    if classified is None:
        return None
    kind, root, resolved_target, eff_cwd = classified
    if _target_is_sanctioned(root, resolved_target):
        return None

    context = {"hook": HOOK_ID, "kind": kind, "target": str(resolved_target), "command": full_command}
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, context, cwd=eff_cwd, command=full_command
    )
    if hatch.should_stop and hatch.approved:
        warn(f"raw worktree {kind} approved via hatch escalation ({hatch.reason})")
        note = f"worktree {kind} {resolved_target} approved via hatch escalation ({hatch.reason})"
        return "approved", note

    message = _block_message(kind, root, resolved_target)
    if hatch.should_stop:
        warn(f"raw worktree {kind} hatch escalation denied: {hatch.reason}")
        return "block", f"hatch escalation denied: {hatch.reason}\n{message}"
    return "block", message


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0

    approved_notes: list[str] = []
    for segment in _split_chain(command):
        result = _evaluate_worktree_segment(segment, cwd, command)
        if result is None:
            continue
        verdict, message = result
        if verdict == "block":
            emit("block", message)
            return BLOCK_EXIT_CODE
        approved_notes.append(message)

    emit("allow", "; ".join(approved_notes) if approved_notes else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
