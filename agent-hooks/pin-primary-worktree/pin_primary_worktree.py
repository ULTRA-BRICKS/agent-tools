#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — pin the repo's PRIMARY worktree to its default branch.

Incident this closes (Alex tg#6462/tg#6477, 2026-07-04): an agent doing HYP-917 work ran
`git checkout feat/hyp-autofix-unsupported-framework` in the SHARED main checkout
(/Users/ultra/work/hyperide) instead of its own isolated worktree. No file damage happened
(the agent caught it immediately and had authored everything in its real worktree), but a
SECOND concurrent agent then also checked out (and committed on) a different feature branch
in that same shared checkout before switching back to main — a real collision waiting to
happen, since any agent relying on the primary checkout sitting on main can have the rug
pulled from under it mid-operation.

`worktree-only-writes` (the sibling pre-write gate) already denies an Edit/Write while the
checkout sits on the default branch — but it never sees a bare `git checkout <branch>` (not
an Edit/Write tool call), and once the checkout HAS moved to a feature branch, that gate's own
logic treats "on a feature branch" as "exactly where authoring belongs" — true for a linked
worktree, false for the primary one. Neither gate distinguishes "which worktree is this" from
"which branch is checked out", which is exactly the blind spot this incident exploited.

This hook closes that gap directly: it inspects `git checkout` / `git switch` invocations and
BLOCKS one that would move the PRIMARY worktree (never a `git worktree add`-created linked
worktree — see `is_primary_worktree`) onto anything other than the repo's default branch.
Checking OUT of a feature branch back to default, `git worktree add` (a different worktree
entirely), `git merge`/`git pull`/`git fetch`, and any checkout inside a LINKED worktree are
all unaffected. Complements (does not replace) `worktree-only-writes`.

PER-REPO, opt-in — reuses the SAME `agent_hooks.worktree_only` knob as `worktree-only-writes`
(one feature, one flag; see that hook's docstring for the resolution order: env
RIG_WORKTREE_ONLY > rig.yaml agent_hooks.worktree_only > default OFF).

External approval (replaces the OLD self-service escape hatch): there is NO env-var / inline
bypass for THIS checkout guard any more — an agent could set `RIG_ALLOW_MAIN_EDIT=1` on its own
command, so that "gate" was security theater (removed here per Alex tg#6554; the sibling
worktree-only-writes pre-write hook still reads that var — a separate cleanup). The block is
now DENY-BY-DEFAULT.
A repo owner may wire `agent_hooks.approval_cmd` (a shell command) in the committed,
code-reviewed rig.yaml; when a primary-worktree checkout is about to be blocked this hook runs
that command (with RIG_APPROVAL_* context in the child's env) and allows ONLY on exit 0.
Nothing configured = denied; a nonzero/error/timeout verdict = denied. An agent with a genuine
reason should ASK the human, not self-grant.

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command/cmd (the bash string); event.cwd is the shell dir.
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": workflow discipline, not a security boundary — any git/parse failure (or
an ambiguous target this hook cannot resolve, e.g. `git checkout -`'s previous-branch when
`@{-1}` itself fails to resolve) must never wedge the agent's ability to run a command.

Known, deliberate scope limits (heuristic, not a sandbox — see worktree-only-writes for the
same tradeoff): a `cd other-repo && git checkout X` chain is judged against the ORIGINAL cwd's
enrollment unless the segment itself carries `git -C <dir>` (which IS honored); `git reset`/
`git rebase` (branch-tip mutation without a HEAD-ref change) are out of scope for v1 — the
incident this closes was a `checkout`, and scope-creeping into every ref-mutating command
risks false positives for a first cut. A bare `git checkout .` / `git checkout <path>` restore
(no branch involved) is excluded via the `--`/`.` checks in `_switch_target`, but an unusual
`git checkout <treeish> <path>` (no `--`, ambiguous even to git itself) can still be
misclassified as a branch switch — a false block on that rare shape now needs a linked worktree
or a repo-owner `approval_cmd` (there is no self-service override any more; see the external
approval note above).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess  # noqa: S404 — fixed git argv, no shell
import sys
import time
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

CONFIG_KEY = "worktree_only"  # SYNC: same knob as worktree-only-writes/worktree_only_writes.py
_FALLBACK_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT_S = 3.0

# Wall-clock budget for the approval_cmd fallback in the WHOLE chained-command gate (main()'s
# loop over every segment), comfortably under this hook's manifest `timeout_ms` (pin-primary-
# worktree.pre-bash.json) even when MULTIPLE segments each need their own `approval_cmd` call. It
# does NOT bound the RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE live-Telegram path, which is allowed
# to run up to tg-ctl's own 900s cap (the reason timeout_ms was raised to 960000, with a 30s headroom over the 930s helper worst case). This hook is
# `on_error: open` (an external manifest-timeout kill on the dispatcher side fails OPEN — allows)
# — that's fine for a single approval_cmd call, since `_APPROVAL_TIMEOUT_CEILING_S` (6s) already
# sits well under the 12s manifest budget. But fixing the chained-bypass bug means this loop can
# now call `_request_approval` once per gate-worthy segment: two or more slow-but-still-denying
# approval_cmd invocations could sum to MORE than 12s, and the dispatcher's own kill-into-
# fail-open would then let an unapproved LATER segment through — not via this hook's logic, but
# via the external timeout. Before starting each approval_cmd call, the caller reserves the FULL
# `_APPROVAL_TIMEOUT_CEILING_S` against this budget (not just "has the deadline already passed")
# — a call that's merely allowed to START just before the deadline could still itself run for the
# whole ceiling and blow the aggregate anyway. That closes the window: a command with too many
# gate-worthy segments to safely clear in time is denied by THIS hook, deliberately, before the
# external kill can ever fire.
_MAIN_LOOP_BUDGET_S = 10.0

MESSAGE = (
    "BLOCKED — this is the repo's PRIMARY worktree; switching it to '{target}' risks colliding "
    "with other concurrent agents/sessions that share this checkout (this exact collision "
    "already happened once — Alex tg#6462/tg#6477). The primary worktree is for merge / pull / "
    "read-only only; checkout/switch there is blocked outside the default branch ({default}).\n"
    "Do the work in a separate worktree instead:\n"
    "    git worktree add ../wt-<feature> -b {target} origin/{default}\n"
    "    cd ../wt-<feature>   # then git checkout/switch there\n"
    "There is NO automatic bypass and NO self-service escape hatch for THIS checkout guard. Do "
    "NOT try to self-grant via any environment variable — RIG_ALLOW_MAIN_EDIT no longer opens "
    "this hook (an agent setting its own bypass is security theater; removed per Alex tg#6554). "
    "If you have a genuine reason for an exception, ASK the human directly (your usual channel "
    "to Alex), or request a one-time Telegram approval by setting "
    'RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE="<written justification>" (deny-by-default; a '
    "bare 1 is rejected) — asking is fine, self-granting is not. A repo owner can also wire a "
    "real external-approval path via agent_hooks.approval_cmd in rig.yaml; unconfigured means "
    "denied."
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"pin-primary-worktree: {msg}\n")


# ── git plumbing (SYNC: copied from worktree-only-writes/worktree_only_writes.py — each
# agent-hook is a standalone script by convention, no shared import between hook dirs) ──────

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


def default_branch(cwd: str) -> str:
    """The repo's default branch — repo-local detection, never hardcoded (SYNC, see the
    sibling worktree_only_writes.default_branch docstring for the full resolution-order
    rationale: origin/HEAD → repo-local init.defaultBranch → main/master existence → "main")."""
    ref = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        return ref.split("/", 1)[1] if "/" in ref else ref
    cfg = _git(cwd, "config", "--local", "--get", "init.defaultBranch")
    if cfg:
        return cfg
    for cand in ("main", "master"):
        if _git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}") is not None:
            return cand
    return _FALLBACK_DEFAULT_BRANCH


def _find_rig_yaml(cwd: str) -> Path | None:
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None). SYNC."""
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _agent_hooks_raw(rig_yaml_text: str, key: str) -> str | None:
    """The raw (comment-stripped, quote-stripped) string value of ``agent_hooks.<key>``, or
    None if the key is absent from the top-level ``agent_hooks:`` block. The single
    block-scoped, indentation-aware line scanner shared by ``_agent_hooks_bool`` and
    ``_agent_hooks_str``. SYNC with worktree_only_writes / orchestrator_stays_thin — keep the
    SCANNER BEHAVIOR in lockstep if it ever changes (those two copies still inline the same
    logic inside their own ``_agent_hooks_bool``; behavior here is identical, just factored)."""
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


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """Minimal stdlib rig.yaml boolean reader. Delegates to ``_agent_hooks_raw`` for the shared
    block-scoped scanner. SYNC with worktree_only_writes / orchestrator_stays_thin — keep the
    scanner behavior in lockstep if this parse ever changes."""
    raw = _agent_hooks_raw(rig_yaml_text, key)
    if raw is None:
        return default
    val = raw.lower()
    if val in ("true", "yes", "on", "1"):
        return True
    if val in ("false", "no", "off", "0"):
        return False
    return default


def worktree_only_enabled(cwd: str) -> bool:
    """Whether worktree-only enforcement is ON for this repo. env override > rig.yaml > OFF."""
    env = os.environ.get("RIG_WORKTREE_ONLY")
    if env is not None:
        return env.strip() == "1"
    root = _find_rig_yaml(cwd)
    if root is None:
        return False
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return False
    return _agent_hooks_bool(text, CONFIG_KEY, default=False)


# ── external-approval extension point (SYNC: the functions below — _agent_hooks_raw,
# _agent_hooks_str, _approval_timeout_s, _resolve_approval_config, _kill_process_group,
# _request_approval — are BYTE-IDENTICAL in pin-primary-worktree/pin_primary_worktree.py and
# block-reset-hard/block_reset_hard.py; keep the copies in lockstep. Each agent-hook is a
# standalone script by convention, no shared import between hook dirs. (In pin, _agent_hooks_raw
# is defined just above _agent_hooks_bool, which reuses it — same text, different placement.)
# Replaces the old self-service escape hatch (an env var / inline sentinel the constrained agent
# could set on itself — security theater; Alex tg#6554) with a deny-by-default gate that only a
# committed, code-reviewed rig.yaml can open. ──────────────────────────────────────────────────

_APPROVAL_CMD_KEY = "approval_cmd"
_APPROVAL_TIMEOUT_KEY = "approval_cmd_timeout_s"
_APPROVAL_TIMEOUT_DEFAULT_S = 5.0
# Hard ceiling on the approval subprocess timeout, independent of what rig.yaml asks for. The
# dispatcher (lib/cc_hook_bridge/dispatch.py) enforces this hook's manifest timeout_ms as a HARD
# kill on the WHOLE hook process. If approval_cmd_timeout_s were honored unbounded, a repo owner
# setting e.g. `approval_cmd_timeout_s: 30` plus a hanging script would let the outer dispatcher
# kill the hook at its manifest budget — and for a hook with on_error=open that resolves to a
# SILENT ALLOW (the exact self-grant class this whole change removes). Clamp the effective
# timeout well under the manifest budget so a misconfig can never become a bypass.
_APPROVAL_TIMEOUT_CEILING_S = 6.0
_APPROVAL_DETAIL_CAP = 500  # cap the approval-cmd stdout captured as the logged reason


def _agent_hooks_str(rig_yaml_text: str, key: str, default: str | None) -> str | None:
    """Read a STRING value for ``agent_hooks.<key>`` from rig.yaml (quotes stripped), or
    ``default`` when the key is absent or its value is blank. Shares ``_agent_hooks_raw``'s
    block-scoped scanner with ``_agent_hooks_bool`` rather than duplicating it."""
    raw = _agent_hooks_raw(rig_yaml_text, key)
    if raw is None or raw == "":
        return default
    return raw


def _approval_timeout_s(rig_yaml_text: str) -> float:
    """The effective approval-cmd subprocess timeout: ``agent_hooks.approval_cmd_timeout_s``
    (default 5.0s), clamped to ``_APPROVAL_TIMEOUT_CEILING_S`` and floored to the default on a
    non-positive or unparseable value."""
    raw = _agent_hooks_str(rig_yaml_text, _APPROVAL_TIMEOUT_KEY, None)
    if raw is None:
        return _APPROVAL_TIMEOUT_DEFAULT_S
    try:
        val = float(raw)
    except ValueError:
        warn(f"invalid {_APPROVAL_TIMEOUT_KEY}={raw!r}; using {_APPROVAL_TIMEOUT_DEFAULT_S}s")
        return _APPROVAL_TIMEOUT_DEFAULT_S
    if val <= 0:
        return _APPROVAL_TIMEOUT_DEFAULT_S
    return min(val, _APPROVAL_TIMEOUT_CEILING_S)


def _resolve_approval_config(cwd: str) -> tuple[str | None, float, str | None]:
    """Read the repo's committed rig.yaml (walk up from ``cwd``) for the external-approval
    knobs. Returns ``(approval_cmd, timeout_s, rig_root)``. ``approval_cmd`` is None when
    unconfigured (the default-DENY path — no subprocess is ever spawned). ``rig_root`` is the
    directory holding rig.yaml, used as the child's cwd so a repo-local approval script
    resolves relative paths."""
    root = _find_rig_yaml(cwd)
    if root is None:
        return None, _APPROVAL_TIMEOUT_DEFAULT_S, None
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return None, _APPROVAL_TIMEOUT_DEFAULT_S, None
    cmd = _agent_hooks_str(text, _APPROVAL_CMD_KEY, None)
    if cmd is not None:
        cmd = cmd.strip() or None
    return cmd, _approval_timeout_s(text), str(root)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the approval command's whole process group, then reap it. ``start_new_session``
    makes the child a group leader, so a backgrounded GRANDCHILD that inherited the stdout pipe
    is killed too. Without this, ``subprocess``'s post-timeout pipe drain can block FOREVER on
    that still-open inherited pipe — defeating the internal timeout, so the dispatcher's
    manifest-timeout kill fires instead and (for an on_error=open hook) resolves to a SILENT
    ALLOW, the exact self-grant class this change removes."""
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


def _request_approval(cwd: str, context: dict[str, str]) -> tuple[bool, str | None]:
    """External-approval gate that REPLACES the old self-service escape hatch. Returns
    ``(approved, detail)``.

    Resolution:
      - approval_cmd UNCONFIGURED in the repo's committed rig.yaml → ``(False, None)``
        immediately, NO subprocess. This is the default-DENY path: "nothing configured" means
        closed, never an automatic bypass.
      - CONFIGURED → run it. Exit 0 → approved (stdout, trimmed + capped, is the logged
        ``detail``). A nonzero exit, ANY subprocess exception, OR a timeout → NOT approved. An
        approval-cmd failure is ALWAYS resolved to deny HERE, inside this function — it never
        falls through to the hook's own ``on_error`` policy (that policy is for the hook's OWN
        plumbing failing, not for an approval verdict; a broken/hanging approval_cmd must mean
        "denied", regardless of whether this hook is on_error=open or on_error=closed).

    The child runs in its OWN process group (``start_new_session=True``) and, on timeout/error,
    the whole group is SIGKILLed (see ``_kill_process_group``) — so a well-meaning-but-broken
    approval_cmd that backgrounds a child holding the stdout pipe can't hang the hook past its
    internal timeout and thereby reach the dispatcher's fail-open.

    TRUST BOUNDARY (why ``shell=True`` is deliberate and safe here): the command STRING comes
    ONLY from ``agent_hooks.approval_cmd`` in rig.yaml — never from the agent's live command,
    never from the offending bash command being checked. The security rests on rig.yaml being a
    REVIEWED config that changes go through: an agent that can already edit-and-commit rig.yaml
    is outside this hook's threat model, exactly as for the sibling ``worktree_only`` knob that
    reads the same file. (This reads the working-tree copy and does NOT itself verify the file
    is committed/clean — that is left to code review, deliberately not re-implemented here; the
    distinction vs. the OLD hatch is that a bypass now requires a reviewed config change, not an
    env var / comment the agent invents per-command.) Dynamic data about WHAT is being approved
    (target, kind, cwd, the raw command) is passed to the child as ``RIG_APPROVAL_*`` environment
    variables ONLY, never string-interpolated into the command, so there is no injection surface
    from agent-controlled data.
    """
    cmd, timeout_s, rig_root = _resolve_approval_config(cwd)
    if not cmd:
        return False, None
    child_env = {**os.environ}
    child_env["RIG_APPROVAL_HOOK"] = context.get("hook") or ""
    child_env["RIG_APPROVAL_KIND"] = context.get("kind") or ""
    child_env["RIG_APPROVAL_TARGET"] = context.get("target") or ""
    child_env["RIG_APPROVAL_CWD"] = cwd or ""
    child_env["RIG_APPROVAL_COMMAND"] = context.get("command") or ""
    try:
        proc = subprocess.Popen(  # noqa: S602 — shell=True on a COMMITTED rig.yaml string only (see trust boundary)
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,  # never inherit the hook's event-pipe stdin: a script that reads stdin gets EOF, not a hang-to-timeout
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=rig_root or None,
            env=child_env,
            start_new_session=True,  # own process group → a backgrounded child can't hold the pipe past timeout
        )
    except (OSError, ValueError) as exc:
        warn(f"approval_cmd failed to launch ({exc}) — denying")
        return False, None
    try:
        out, _err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        warn(f"approval_cmd timed out after {timeout_s:.1f}s — denying")
        return False, None
    except (OSError, ValueError) as exc:
        _kill_process_group(proc)
        warn(f"approval_cmd errored while running ({exc}) — denying")
        return False, None
    if proc.returncode != 0:
        warn(f"approval_cmd denied (exit {proc.returncode})")
        return False, None
    detail = (out or "").strip()[:_APPROVAL_DETAIL_CAP] or None
    return True, detail


# ── primary-vs-linked worktree detection (the piece worktree-only-writes lacks) ─────────────

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


def is_primary_worktree(cwd: str) -> bool | None:
    """True iff ``cwd`` sits in the repo's PRIMARY worktree (not a `git worktree add` linked one).

    git's own distinction: in the primary worktree ``--git-dir`` and ``--git-common-dir``
    resolve to the SAME directory (both the real ``.git``). In a linked worktree ``--git-dir``
    is ``<common>/.git/worktrees/<name>`` while ``--git-common-dir`` stays at the shared
    ``.git`` — they diverge. Returns None (undetermined) on any git/resolve failure so the
    caller can fail OPEN specifically on this signal.
    """
    gd = _git_path(cwd, "--git-dir")
    cd = _git_path(cwd, "--git-common-dir")
    if gd is None or cd is None:
        return None
    return gd == cd


# ── bash chain parsing (SYNC: chain-split copied from
# orchestrator-stays-thin/orchestrator_stays_thin.py._split_chain — same quote-aware algorithm) ─

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


# ── git checkout/switch classification ───────────────────────────────────────────────────────

_GIT_GLOBAL_OPT_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)
# A leading shell assignment (`VAR=val`) in front of the real command — e.g. `GIT_TRACE=1 git
# checkout x`.
_ASSIGN_RE = re.compile(r"^\w+=")


def _classify_git_segment(
    segment: str,
) -> tuple[str, list[str], str | None] | None:
    """If ``segment``'s head is ``git`` (after any leading assignments), return (subcommand,
    rest_tokens, -C override), else None.

    Leading `VAR=val` shell assignments are skipped BEFORE looking for the `git` token — a
    pre-bash event sees command-local assignments verbatim in `args.command` (they are never
    stripped by a shell, since no shell has run yet), so without this skip a segment like
    `GIT_TRACE=1 git checkout x` has `toks[0] == "GIT_TRACE=1"`, is classified as "not git" and
    ignored entirely — silently ALLOWING the exact checkout this hook exists to block, for ANY
    unrelated leading assignment. The skipped assignments are discarded (the old inline
    `RIG_ALLOW_MAIN_EDIT` bypass they used to feed was removed — Alex tg#6554); skipping them is
    still required so the real `git` token is found.

    Also skips leading global options (so `git -C d checkout x` and `git -c k=v checkout x`
    still find the real subcommand); records a `-C <dir>` override so a cross-repo checkout is
    judged against THAT repo, not the shell cwd (mirrors worktree-only-writes' target-dir
    principle).
    """
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


def _switch_target(subcommand: str, toks: list[str]) -> str | None:
    """The branch/ref a `checkout`/`switch` segment would move HEAD to, or None if it isn't a
    branch switch at all (a path-restore, a bare detach, or a `.`/no-arg form)."""
    if "--" in toks:
        return None  # `checkout <ref> -- <path>` / `checkout -- <path>` — restores a path only
    create_flags = {"-b", "-B"} if subcommand == "checkout" else {"-c", "-C"}
    positional: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in create_flags and i + 1 < len(toks):
            return toks[i + 1]  # explicit create-and-switch target wins outright
        if t == "-":
            positional.append(t)  # "previous branch" — a real target, NOT a flag despite the dash
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        positional.append(t)
        i += 1
    if not positional or positional[0] == ".":
        return None  # `checkout .` (bulk path-restore) / no target at all
    return positional[0]


def _resolve_effective_cwd(base_cwd: str, cwd_override: str | None) -> str:
    if not cwd_override:
        return base_cwd
    p = Path(cwd_override)
    if not p.is_absolute():
        p = Path(base_cwd or ".") / p
    return str(p)


def _evaluate_checkout_segment(
    segment: str, base_cwd: str, command: str, deadline: float
) -> tuple[str, str] | None:
    """Evaluate ONE chained-command segment for a primary-worktree checkout/switch violation.

    Returns None if this segment doesn't need gating — not a git invocation, not a
    checkout/switch, no resolvable branch target (a path-restore / bare `checkout .` / an
    unresolvable `-`), the target repo isn't `worktree_only`-enrolled, the target IS the
    default branch, or this isn't the PRIMARY worktree (undetermined or a linked worktree —
    both fail open). Returns `("block", message)` if this segment must block the WHOLE chained
    command (approval was requested and denied, OR the `deadline` has already passed — see
    `_MAIN_LOOP_BUDGET_S`). Returns `("approved", note)` if this segment required and received
    external approval.

    `main()` keeps scanning LATER segments after an "approved" result — only a
    `("block", ...)` result stops the scan early. That split is the fix for the bug this
    function replaces inline loop-body logic for: approving segment 1 must never skip
    evaluating a segment 2 in the same chained command.

    `deadline` is a `time.monotonic()` cutoff: if it has already passed by the time this
    segment would need an `approval_cmd` call, this returns `("block", ...)` WITHOUT spawning
    that subprocess (see `_MAIN_LOOP_BUDGET_S`'s module-level comment for why — a chained
    command with enough gate-worthy segments could otherwise push the AGGREGATE approval time
    past this hook's manifest timeout, and this hook's `on_error` is "open": an external kill
    fails OPEN, exactly the bypass this closes).
    """
    classified = _classify_git_segment(segment)
    if classified is None:
        return None
    subcommand, rest, cwd_override = classified
    if subcommand not in ("checkout", "switch"):
        return None
    target = _switch_target(subcommand, rest)
    if target is None:
        return None

    eff_cwd = _resolve_effective_cwd(base_cwd, cwd_override)

    if target == "-":
        resolved = _git(eff_cwd, "rev-parse", "--abbrev-ref", "@{-1}")
        if not resolved:
            return None  # can't resolve "previous branch" → fail open on this segment
        target = resolved

    # Cheapest remaining gate: is this repo enrolled? (per the segment's OWN effective cwd, so
    # a cross-repo `git -C <other-repo> checkout` is judged by THAT repo's rig.yaml.)
    if not worktree_only_enabled(eff_cwd):
        return None

    default = default_branch(eff_cwd)
    if target == default:
        return None  # switching (back) to the default branch — always fine

    if not is_primary_worktree(eff_cwd):
        return None  # None (undetermined) or False (a linked worktree) → fail open / allowed

    # Reserve the FULL approval-cmd ceiling before starting this call, not just check whether
    # the deadline has already passed: a call that's allowed to START just before `deadline`
    # can still RUN for up to `_APPROVAL_TIMEOUT_CEILING_S` more, which alone could push the
    # aggregate past the manifest timeout. Only start this approval call if it can finish (at
    # its absolute worst case) before `deadline`.
    context = {"hook": "pin-primary-worktree", "kind": subcommand, "target": target, "command": command}

    # RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE (live Telegram ask) is checked first and is not
    # bounded by _MAIN_LOOP_BUDGET_S/_APPROVAL_TIMEOUT_CEILING_S — a human approval round-trip
    # legitimately runs up to tg-ctl's 900s cap, which is why this hook's manifest timeout_ms was
    # raised to 960000. It only "stops" (should_stop=True) when an actual hatch request was made;
    # an unset env var falls through to the approval_cmd budget below.
    hatch = hatch_escalation.request_hatch_approval(
        "pin-primary-worktree", context, cwd=eff_cwd, command=command
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"primary-worktree checkout approved via hatch escalation ({hatch.reason})")
            return (
                "approved",
                f"{subcommand} {target} approved via hatch escalation ({hatch.reason})",
            )
        warn(f"primary-worktree checkout hatch escalation denied: {hatch.reason}")
        return "block", (
            f"hatch escalation denied: {hatch.reason}\n"
            f"{MESSAGE.format(target=target, default=default)}"
        )

    # Reserve the FULL approval-cmd ceiling before starting this call, not just check whether
    # the deadline has already passed: a call that's allowed to START just before `deadline`
    # can still RUN for up to `_APPROVAL_TIMEOUT_CEILING_S` more, which alone could push the
    # aggregate past the manifest timeout. Only start this approval call if it can finish (at
    # its absolute worst case) before `deadline`.
    if time.monotonic() + _APPROVAL_TIMEOUT_CEILING_S >= deadline:
        warn(
            f"denying '{target}' — chained-command approval budget "
            f"({_MAIN_LOOP_BUDGET_S:.1f}s) would be exhausted before this segment's "
            "approval_cmd could safely finish"
        )
        return "block", (
            f"BLOCKED — too many chained segments needed approval to check safely within this "
            f"hook's time budget (denying '{target}' rather than risk an external-timeout "
            "fail-open). Split this into separate Bash calls so each is approved individually."
        )

    approved, detail = _request_approval(eff_cwd, context)
    if not approved:
        return "block", MESSAGE.format(target=target, default=default)

    warn(f"primary-worktree checkout approved via approval_cmd ({detail})")
    return "approved", f"{subcommand} {target} approved via external approval_cmd ({detail})"


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

    # Every segment of a chained command is evaluated — approving one segment must NEVER
    # short-circuit the check for a LATER segment in the same command (the bug this fixes:
    # `git -C approved checkout feat ; git -C other checkout feat` let the second checkout
    # bypass the guard entirely once the first was approved). `deadline` bounds the AGGREGATE
    # time spent across every segment's approval_cmd call (see _MAIN_LOOP_BUDGET_S).
    approved_notes: list[str] = []
    deadline = time.monotonic() + _MAIN_LOOP_BUDGET_S
    for segment in _split_chain(command):
        result = _evaluate_checkout_segment(segment, cwd, command, deadline)
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
