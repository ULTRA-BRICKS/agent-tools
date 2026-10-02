#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block a raw git worktree add/remove/move outside .worktrees/.

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

This hook closes that gap directly: it inspects `git worktree add` / `git worktree remove` /
`git worktree move` invocations and BLOCKS one whose target path does not resolve under the
repo's `<repo-root>/.worktrees/` — the one location rig's own reconciler (`rig worktree gc`) and
sibling commands (`rig worktree create`/`remove`) know about. `move` relocates an EXISTING
worktree (`git worktree move <worktree> <new-path>`) and is gated on its destination (the second
positional) exactly like `add`/`remove` are gated on their single path argument — a legitimate
migration INTO `.worktrees/` (`git worktree move /tmp/old .worktrees/old`) stays allowed; only a
destination outside `.worktrees/` is blocked. `git worktree list`, `lock`/`unlock`/`repair`, and
`git worktree prune` are all unaffected (see "Deliberately out of scope" below).

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
lock`/`unlock`/`repair`/`list` don't create or relocate a worktree relative to the repo root the
way `add`/`remove`/`move` do and are likewise out of scope. (`move` was originally grouped with
these as "doesn't relocate a worktree" — that premise was WRONG, `move` is exactly a relocate
primitive, and it is now gated; see the review note in git history/PR discussion if this class
of mistake recurs — the fix here was to actually re-derive, not assume, what each subcommand
does before excluding it.)

Detection is ARGV-BASED (shlex, chain-aware — `_split_chain` is copied from
`pin-primary-worktree/pin_primary_worktree.py`, itself SYNC'd with
`orchestrator-stays-thin/orchestrator_stays_thin.py._split_chain`), not a substring match:
`_classify_git_segment` (also copied from pin-primary-worktree, EXTENDED to also capture a
`--git-dir`/`GIT_DIR=` override — see its own docstring) finds the real `git` invocation past
any leading `VAR=value` assignments and global git options (`-C`, `-c`, `--git-dir`, ...),
honoring a `-C <dir>` override so a cross-repo `git -C <other-repo> worktree add ...` is judged
against THAT repo. `_worktree_target` then walks the `worktree add`/`remove`/`move` argv via
`_worktree_positionals`, skipping recognized flags (`-b`/`-B`/`--reason` for `add`, which take a
value; `-f`/`--force` and every other boolean flag for all three), to find the checked path —
the FIRST positional for `add`/`remove`, the SECOND for `move` — in EITHER flag order
(`git worktree add -b feat path` and `git worktree add path -b feat` both resolve the same
target).

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
option grammar; a REPEATED `-C`/`--git-dir` (`git -C a -C b ...`, which real git composes — the
second is resolved relative to the first, not to the original cwd) is not composed here either —
`_classify_git_segment` just keeps the LAST occurrence's raw value, so a doubly-`-C`'d segment can
be judged against a directory git itself would not actually use; inside a SUBMODULE,
`--git-common-dir` resolves to `<parent>/.git/modules/<sub>` (name `<sub>`, not `.git`), so
`repo_root` returns None and a `git worktree add` run from a submodule of a rig-managed repo is
NOT gated — undocumented in an earlier draft, which incorrectly claimed `--git-common-dir` "always"
resolves to the primary's real `.git`; a `~/...`/`$HOME/...` target is never tilde/env-expanded
(this hook works on the raw argv token, exactly as git receives it before shell expansion for a
QUOTED path — an unquoted one IS shell-expanded before this hook ever sees it), so a SANCTIONED
target spelled with `~` resolves to a literal `<cwd>/~/...` and is over-blocked (safe direction,
but costs a real Telegram round-trip for a false positive).

A chained command with MORE THAN ONE gate-worthy `worktree add`/`remove`/`move` segment gets at
most ONE Telegram hatch round-trip for the whole invocation — the second (and any later) such
segment is blocked outright, without its own hatch call: `tg-ctl ask` legitimately runs up to
~900s; two sequential hatch calls could together exceed this hook's `timeout_ms` manifest budget
— sized for ONE call — and the dispatcher's own timeout-kill would then resolve to
`on_error: open`'s silent allow for the WHOLE chain. Capping at one hatch call per invocation
closes this rather than merely documenting it; see `_evaluate_worktree_segment`'s `allow_hatch`
parameter. Split a multi-worktree-op chain into separate Bash calls so each gets its own approval.

Known, DELIBERATELY UNCLOSED bypasses (documented, not silently accepted — a reviewer or a future
change should see these named, not discover them):

- A `git worktree add`/`remove`/`move` hidden behind a shell interpreter or `eval` — `bash -c
  'git worktree add /tmp/x'`, `sh -c "..."`, `eval "git worktree add /tmp/x"` — is NOT detected:
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
- A `.worktrees` directory that is a symlink is closed FOR A PRE-EXISTING symlink —
  `_worktrees_dir_is_untrusted` refuses to treat ANY target as sanctioned once `.worktrees` is a
  symlink at all, checked at the moment this hook evaluates the command. The residual is TOCTOU
  (time-of-check to time-of-use): this hook evaluates every segment of a chained command BEFORE
  any of them actually run, so a symlink PLANTED by an EARLIER segment of the SAME chained command
  is invisible to this check — `ln -s /tmp/sprawl .worktrees && git worktree add .worktrees/x
  feat/x` sees `.worktrees` not yet a symlink at evaluation time, judges the target as sanctioned,
  and only after this hook allows the command does the shell actually create the symlink and let
  git follow it. This is the SAME class of residual as the `cd other-repo &&` scope limit above
  (an earlier segment changes what a later one means) and the `bash -c`/`eval` residual just
  above (a determined adversarial agent has other routes already out of scope) — not closed here
  for the same reason: a targeted "scan earlier segments for anything touching `.worktrees`"
  check would still be evadable (`python -c`, `mv`, `cp -s`, ...) and would over-block the benign
  `mkdir -p .worktrees && git worktree add .worktrees/x feat/x`. A SEPARATE, later Bash call
  hitting an already-planted symlink IS caught (the symlink now exists when this hook evaluates
  it) — only the SAME-chain planting is the gap.
- A raw target token carrying an unexpanded `$VAR`/`` $( … ) ``/backtick is treated as NOT
  sanctioned (see `_target_has_unexpanded_shell_syntax`) rather than lexically resolved and
  potentially misjudged safe — this closes the class where a literal token like
  `.worktrees/$(printf ../outside-wt)` has no `..` component until the SHELL expands it, which
  would otherwise let a target that lexically looks like a child of `.worktrees/` actually resolve
  outside it once the real shell runs the command.
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


class _UnavailableHatchResult:
    """Hatch result when the repo-local helper cannot be imported.

    should_stop is False so the hook falls through to its own default
    decision (block a gated event, allow otherwise). Crashing here would
    let on_error:open fail-open a denial-class hook (#547).
    """

    requested = False
    approved = False
    reason = "hatch helper unavailable"
    env_var = ""
    env_present = False
    should_stop = False
    tg_ctl_path = None


class _UnavailableHatch:
    def request_hatch_approval(self, *args, **kwargs):
        return _UnavailableHatchResult()


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        return _UnavailableHatch()
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        return _UnavailableHatch()
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
        # A helper that calls sys.exit() at import must not make the hook exit 0
        # (allow via on_error:open). Ctrl-C still propagates; any other failure
        # installs a stub so the hook can still classify the event (#547).
        if isinstance(exc, KeyboardInterrupt):
            raise
        return _UnavailableHatch()
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

def _git(cwd: str, *args: str, git_dir: str | None = None) -> str | None:
    """Run a read-only ``git [--git-dir=<git_dir>] -C cwd <args>`` and return trimmed stdout, or
    None on any failure. ``git_dir`` (when given) is passed through to the REAL `git` binary
    exactly as `--git-dir` — letting git itself resolve it (relative-to-cwd or absolute) rather
    than hand-rolling that resolution here."""
    argv = ["git"]
    if git_dir:
        argv.append(f"--git-dir={git_dir}")
    argv += ["-C", cwd or ".", *args]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, no shell
            argv,
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


def _git_path(cwd: str, *rev_parse_args: str, git_dir: str | None = None) -> Path | None:
    out = _git(cwd, "rev-parse", *rev_parse_args, git_dir=git_dir)
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    try:
        return p.resolve()
    except OSError:
        return None


def repo_root(cwd: str, git_dir: str | None = None) -> Path | None:
    """The repo's single, unambiguous root directory, resolved the SAME way regardless of
    whether ``cwd`` sits in the primary worktree or a linked one (see module docstring): `git
    rev-parse --git-common-dir` resolves to the primary worktree's real `.git` for both. Returns
    None (undetermined, fails open on the calling segment) on any git/resolve failure, or when
    the resolved `--git-common-dir` isn't a `.git`-named directory — a BARE repo (no worktree
    root at all) or a SUBMODULE (`--git-common-dir` there resolves to
    `<parent>/.git/modules/<sub>`, named `<sub>`, not `.git`) are both out of scope this way: a
    `git worktree add` run from inside a submodule of a rig-managed repo is not gated by this
    hook.

    ``git_dir`` (a `--git-dir`/`GIT_DIR=` override captured off the gated command itself) is
    forwarded to the underlying `git rev-parse` call so a segment like `git -C /tmp --git-dir
    /real/repo/.git worktree add /tmp/x` — which git itself honors and executes against the REAL
    repo despite `-C` pointing at a non-repo directory — resolves its true root instead of
    reading as "repo root undetermined" and fail-opening (that combination previously let an
    outside-`.worktrees/` target through unblocked)."""
    common_dir = _git_path(cwd, "--git-common-dir", git_dir=git_dir)
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


def _leading_git_dir_assignment(toks: list[str], stop: int) -> str | None:
    """The value of a leading ``GIT_DIR=<value>`` shell assignment among ``toks[:stop]`` (the
    assignments `_classify_git_segment` skips over before finding the real `git` token), or None.
    A bare `VAR=value` prefix is otherwise discarded entirely by `_classify_git_segment` (matching
    pin-primary-worktree's own `_ASSIGN_RE` skip) — but `GIT_DIR` specifically changes which repo
    `git` operates on, exactly like `--git-dir`, so it must feed the same repo-root resolution:
    `GIT_DIR=/real/.git git -C /tmp worktree add /tmp/x` was otherwise classified with an
    unresolvable `/tmp` root and fail-opened even though git itself honors `GIT_DIR` and creates
    the worktree."""
    for tok in toks[:stop]:
        key, sep, val = tok.partition("=")
        if sep and key == "GIT_DIR":
            return val
    return None


def _classify_git_segment(
    segment: str,
) -> tuple[str, list[str], str | None, str | None] | None:
    """If ``segment``'s head is ``git`` (after any leading assignments / global options), return
    ``(subcommand, rest_tokens, -C override, --git-dir/GIT_DIR override)``, else None. SYNC:
    based on pin-primary-worktree/pin_primary_worktree.py._classify_git_segment (same rationale:
    leading `VAR=val` assignments and global `-C`/`-c`/... options must be skipped to find the
    real `git` subcommand and to honor a cross-repo `-C <dir>` override) — EXTENDED here to also
    capture `--git-dir`/`GIT_DIR=` (pin-primary-worktree only needs the `-C` form; this hook
    additionally tracks `--git-dir` because a `worktree add` command that resolves an explicit,
    real repo via `--git-dir`/`GIT_DIR` while `-C`/cwd point somewhere ELSE (a non-repo directory)
    must not be treated as "repo root undetermined" — see `repo_root`'s `git_dir_override`
    parameter)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    git_dir_override = _leading_git_dir_assignment(toks, i)
    if i >= len(toks) or toks[i].rsplit("/", 1)[-1] != "git":
        return None
    i += 1
    cwd_override: str | None = None
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        if opt == "-C" and i + 1 < len(toks):
            cwd_override = toks[i + 1]
        elif opt == "--git-dir" and i + 1 < len(toks):
            git_dir_override = toks[i + 1]
        elif opt.startswith("--git-dir="):
            git_dir_override = opt.split("=", 1)[1]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):
            i += 1  # skip this option's value token (glued `--git-dir=x` isn't in the set, so
                     # this only fires for a genuinely DETACHED value like `--git-dir x`)
    if i >= len(toks):
        return None
    return toks[i], toks[i + 1:], cwd_override, git_dir_override


# Flags that consume the NEXT token as a value, per `worktree add`'s own grammar (`-b <branch>`,
# `-B <branch>`, `--lock --reason <string>`). Everything else `-`-prefixed is treated as boolean
# (`-f`/`--force`, `--detach`, `--checkout`/`--no-checkout`, `--lock`, `--orphan`,
# `--guess-remote`/`--no-guess-remote`, `--track`/`--no-track`, `-q`/`--quiet`, ...).
_ADD_VALUE_FLAGS = frozenset({"-b", "-B", "--reason"})
# `worktree remove` has no value-taking flags of its own (`-f`/`--force` is boolean).
_REMOVE_VALUE_FLAGS: frozenset[str] = frozenset()
# `worktree move <worktree> <new-path>` — RELOCATES an existing worktree, exactly the sprawl
# move `add`/`remove` already gate. The ONLY flag it takes is `-f`/`--force`, no value.
_MOVE_VALUE_FLAGS: frozenset[str] = frozenset()


def _worktree_positionals(toks: list[str], value_flags: frozenset[str]) -> list[str]:
    """Every bare positional token in `toks` (the args after `worktree add`/`remove`/`move`), in
    order. Skips a recognized value flag and its following token, a `--long=value` glued flag,
    and any other `-`-prefixed boolean flag; a literal `--` ends flag scanning (everything after
    is positional)."""
    positionals: list[str] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t == "--":
            positionals.extend(toks[i + 1:])
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
        positionals.append(t)
        i += 1
    return positionals


def _worktree_target(kind: str, toks: list[str], value_flags: frozenset[str]) -> str | None:
    """The target path this hook must check for `kind` (`add`/`remove`/`move`), in EITHER flag
    order. For `add`/`remove` that's the FIRST positional (the worktree path itself). For `move`
    it's the SECOND positional (`<worktree> <new-path>` — the source isn't gated, only where it
    ends up; a legitimate migration `git worktree move /tmp/old .worktrees/old` stays allowed).
    Returns None when there aren't enough positionals (an invalid/incomplete invocation git
    itself would reject)."""
    positionals = _worktree_positionals(toks, value_flags)
    if kind == "move":
        return positionals[1] if len(positionals) >= 2 else None
    return positionals[0] if positionals else None


def _resolve_effective_cwd(base_cwd: str, cwd_override: str | None) -> str:
    if not cwd_override:
        return base_cwd
    p = Path(cwd_override)
    if not p.is_absolute():
        p = Path(base_cwd or ".") / p
    return str(p)


def _target_has_unexpanded_shell_syntax(raw_target: str) -> bool:
    """True iff the RAW (unresolved) target token still carries a `$` or backtick — a shell
    expansion (`$VAR`, `$( … )`, backticks) this hook cannot read at pre-exec time. The literal
    argv token this hook sees is PRE-expansion; only the real shell later substitutes the actual
    value. A token like `.worktrees/$(printf ../outside-wt)` lexically has no `..` component and
    would otherwise pass the plain path check as "under `.worktrees/`", while the shell expands it
    to `.worktrees/../outside-wt` and git lands the worktree OUTSIDE `.worktrees/` entirely. Any
    such token is therefore NEVER treated as sanctioned, regardless of where it lexically
    resolves — the normal gate (block/hatch) applies instead. Over-blocks a SINGLE-QUOTED literal
    dollar sign the shell would never expand (`'.worktrees/$FOO'` as a literal directory name) —
    accepted, safe direction, same posture as the rest of this hook's argv heuristics."""
    return "$" in raw_target or "`" in raw_target


def _resolve_target(eff_cwd: str, target: str) -> Path:
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = Path(eff_cwd or ".") / target_path
    return target_path.resolve(strict=False)


def _worktrees_dir_is_untrusted(root: Path) -> bool:
    """True iff `<root>/.worktrees` is a symlink at all — the one way the "target resolves under
    `.worktrees/`" check below could be gamed (mirrors `riglib/worktree.py::_target_escapes_repo`:
    the only way a target under `<root>/.worktrees/<name>` could land somewhere `rig` didn't put
    it is if `.worktrees` ITSELF is a symlink — `.worktrees` not existing yet, or being a plain
    directory, cannot). ANY symlinked `.worktrees` is untrusted, full stop — this deliberately does
    NOT call `.resolve()` to check WHERE the symlink points: a self-referential loop
    (`.worktrees -> .worktrees`) or a `.worktrees -> .`/`.worktrees -> <repo-root>` self-symlink
    both defeated an earlier version of this check (a symlink loop raised an uncaught
    `RuntimeError` on Python <=3.12 — `Path.resolve()`'s loop detection was only reclassified to
    `OSError` in 3.13 — which crashed the hook and, under `on_error: open`, silently allowed the
    whole command; a `.worktrees -> .` self-symlink resolved to `root` itself, at which point
    `relative_to` reported "does not escape" and every in-repo path became "sanctioned"). Not
    calling `.resolve()` here closes both: there is no path left to resolve, so no loop to
    detect and no self-referential "escapes to nowhere" case to misjudge."""
    return (root / _WORKTREES_DIR_NAME).is_symlink()


def _target_is_sanctioned(root: Path, raw_target: str, resolved_target: Path) -> bool:
    """True iff `resolved_target` is a direct-child canonical `<root>/.worktrees/<name>` target
    (not a nested path such as `.worktrees/sub/feat`, nor
    `.worktrees` itself — `Path.relative_to` on an identical path returns `.` without raising, so
    a bare `git worktree add .worktrees feat/x` — no `<name>` subdirectory at all, not the
    `rig worktree create` shape — must not be waved through as "already sanctioned"), the raw
    argv token carries no unexpanded shell syntax the resolved path can't see (see
    `_target_has_unexpanded_shell_syntax`), AND `.worktrees` itself isn't a symlink (see
    `_worktrees_dir_is_untrusted`)."""
    if _target_has_unexpanded_shell_syntax(raw_target):
        return False
    if _worktrees_dir_is_untrusted(root):
        return False
    allowed_dir = (root / _WORKTREES_DIR_NAME).resolve()
    if resolved_target == allowed_dir:
        return False
    try:
        relative_target = resolved_target.relative_to(allowed_dir)
        return len(relative_target.parts) == 1
    except ValueError:
        return False


# The next-step guidance for each `kind`, appended to `MESSAGE`. `add`/`remove` map straight onto
# their `rig worktree` counterpart; `move` fully relocates an existing worktree — the exact
# GH-329 failure mode — but has no direct rig equivalent (rig-cli doesn't offer a relocate verb),
# so it gets a two-step recipe instead.
_GUIDANCE = {
    "add": "    rig worktree create <name>\n",
    "remove": "    rig worktree remove <name>\n",
    "move": (
        "    rig worktree create <name>   # at the destination\n"
        "    rig worktree remove <old-name>   # retire the old one\n"
    ),
}

MESSAGE = (
    "BLOCKED — raw `git worktree {kind}` outside `{worktrees_dir}/` is not the sanctioned door "
    "(agent-tools#608 / rig-cli AGENTS.md: a real incident — GH-329 — left ~70 leftover "
    "worktrees scattered across five different ad-hoc locations, none tracked, tens of GB of "
    "duplicated node_modules/.venv). `{repo_worktrees}` is the ONLY sanctioned location.\n"
    "Use the rig door instead:\n"
    "{guidance}"
    "or, if `{target}` is a worktree that predates this convention, `rig worktree gc` to "
    "reconcile it.\n"
    "There is NO self-service bypass. If you have a genuine one-off reason (migrating an "
    "existing worktree, a documented non-agent use case), ASK the human — request a single "
    'Telegram approval via RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="<why>" (deny-by-default; a '
    "bare `1` is rejected)."
)


def _block_message(kind: str, root: Path, target_display: str) -> str:
    return MESSAGE.format(
        kind=kind,
        worktrees_dir=_WORKTREES_DIR_NAME,
        repo_worktrees=str(root / _WORKTREES_DIR_NAME),
        guidance=_GUIDANCE[kind],
        target=target_display,
    )


def _resolve_and_check_target(
    root: Path, eff_cwd: str, raw_target: str
) -> tuple[str, bool, str | None]:
    """Resolve `raw_target` and decide whether it's sanctioned, WITHOUT letting an exception
    escape to the host's `on_error` policy. From `_classify_worktree_call` onward this IS a
    worktree op with a real target — any failure here must resolve to "not sanctioned" (the
    caller blocks), never to a silent host-level allow. Returns
    `(target_display, sanctioned, error)`: `target_display` is the resolved path as a string on
    success, or the raw unresolved token on failure (for the block message); `error` is the
    exception text on failure, else None.

    This is the fix for a real incident found in review: an uncaught symlink-loop `RuntimeError`
    (`.worktrees` pointing at itself — `Path.resolve()`'s loop detection was only reclassified
    from `RuntimeError` to `OSError` in Python 3.13) used to crash the hook process here, and
    under this hook's `on_error: open` that crash silently ALLOWED the whole gated command —
    exactly the GH-329 failure mode this hook exists to stop. Catching broadly and blocking is
    deliberate: an unresolvable target is never treated as safe."""
    try:
        resolved_target = _resolve_target(eff_cwd, raw_target)
        sanctioned = _target_is_sanctioned(root, raw_target, resolved_target)
    except (OSError, RuntimeError, ValueError) as exc:
        return raw_target, False, str(exc)
    return str(resolved_target), sanctioned, None


_WORKTREE_VALUE_FLAGS = {"add": _ADD_VALUE_FLAGS, "remove": _REMOVE_VALUE_FLAGS, "move": _MOVE_VALUE_FLAGS}


def _classify_worktree_call(
    segment: str, base_cwd: str
) -> tuple[str, Path, str, str] | None:
    """If `segment` is a `git worktree add`/`remove`/`move` invocation with a resolvable target,
    return `(kind, root, raw_target, eff_cwd)` — deliberately the RAW, unresolved target string,
    not a resolved `Path` (see `_evaluate_worktree_segment`, which resolves it itself inside a
    try/except so a resolution failure blocks rather than silently escaping to the host's
    `on_error` policy). Returns None when nothing needs gating — not `git`, not
    `worktree add`/`remove`/`move`, no resolvable target, or repo root undetermined (this last
    one — an unresolvable/non-git/bare-repo cwd — is the one legitimate "nothing to gate" case
    left; everything AFTER this point in the pipeline is "yes, this is a worktree op with a
    target" and must fail toward block, not open)."""
    classified = _classify_git_segment(segment)
    if classified is None:
        return None
    subcommand, rest, cwd_override, git_dir_override = classified
    if subcommand != "worktree" or not rest:
        return None
    kind = rest[0]
    if kind not in _WORKTREE_VALUE_FLAGS:
        return None
    target = _worktree_target(kind, rest[1:], _WORKTREE_VALUE_FLAGS[kind])
    if target is None:
        return None

    eff_cwd = _resolve_effective_cwd(base_cwd, cwd_override)
    root = repo_root(eff_cwd, git_dir=git_dir_override)
    if root is None:
        return None  # undetermined repo root → fail open on this segment
    return kind, root, target, eff_cwd


def _evaluate_worktree_segment(
    segment: str, base_cwd: str, full_command: str, *, allow_hatch: bool
) -> tuple[str, str] | None:
    """Evaluate ONE chained-command segment. Returns None if it needs no gating (not `git`, not
    `worktree add`/`remove`/`move`, no resolvable target, repo root undetermined, or the target
    already resolves under `<repo-root>/.worktrees/`). Returns `("block", message)` when it must
    block, `("approved", note)` when a hatch escalation approved it.

    ``allow_hatch=False`` skips the hatch escalation entirely and blocks outright — used by
    `main()` for every segment AFTER the first one that already consumed a hatch round-trip in
    THIS invocation: `tg-ctl ask` legitimately runs up to ~900s per call; a chain with two-plus
    gate-worthy segments could sum past this hook's `timeout_ms` manifest budget — sized for ONE
    such call — and the dispatcher's own timeout-kill would then fail OPEN on `on_error: open`,
    silently allowing the whole chain including a segment that was never actually approved.
    Capping at one hatch call per invocation keeps the worst case bounded by a single `tg-ctl ask`
    round-trip and turns "ran out of budget" into an explicit block instead of a silent bypass.

    The hatch escalation (when allowed) is asked with the FULL `full_command` (not just this
    segment) — the human approver sees the whole chained command, and an inline
    `RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="why" <cmd>` prefix on ANY segment of it is found
    (mirrors `pin-primary-worktree`)."""
    classified = _classify_worktree_call(segment, base_cwd)
    if classified is None:
        return None
    kind, root, raw_target, eff_cwd = classified
    target_display, sanctioned, resolve_error = _resolve_and_check_target(root, eff_cwd, raw_target)
    if sanctioned:
        return None

    message = _block_message(kind, root, target_display)
    if resolve_error:
        message = f"could not verify this target is safe ({resolve_error}) — blocking.\n{message}"
    if not allow_hatch:
        return "block", (
            "a second worktree operation in this chained command still needs its own "
            "approval — split it into a separate command so each can be approved "
            f"individually.\n{message}"
        )

    context = {"hook": HOOK_ID, "kind": kind, "target": target_display, "command": full_command}
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, context, cwd=eff_cwd, command=full_command
    )
    if hatch.should_stop and hatch.approved:
        warn(f"raw worktree {kind} approved via hatch escalation ({hatch.reason})")
        note = f"worktree {kind} {target_display} approved via hatch escalation ({hatch.reason})"
        return "approved", note

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
    if not isinstance(event, dict):
        # Syntactically valid JSON that isn't an object (`null`, `5`, `[]`, `"oops"`) — `.get()`
        # below would raise an uncaught AttributeError otherwise. The host's on_error=open would
        # still resolve that crash to allow, but this makes the fail-open EXPLICIT and tested
        # instead of an accidental byproduct of an unhandled exception.
        warn(f"event is not a JSON object ({type(event).__name__}) — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0

    approved_notes: list[str] = []
    hatch_used = False
    for segment in _split_chain(command):
        result = _evaluate_worktree_segment(segment, cwd, command, allow_hatch=not hatch_used)
        if result is None:
            continue
        hatch_used = True
        verdict, message = result
        if verdict == "block":
            emit("block", message)
            return BLOCK_EXIT_CODE
        approved_notes.append(message)

    emit("allow", "; ".join(approved_notes) if approved_notes else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
