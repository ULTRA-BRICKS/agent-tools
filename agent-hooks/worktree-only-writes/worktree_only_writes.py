#!/usr/bin/env python3
"""agents-hooks/v1 pre-write hook — enforce the worktree-only workflow.

On a PR-workflow repo the DEFAULT branch (main/master) is for merge / pull / read-only
operations ONLY: all file-authoring work belongs in a separate git worktree on a feature
branch. This gate DENIES an Edit/Write/MultiEdit/NotebookEdit while the checkout is sitting
on the repo's default branch, telling the agent to create/enter a worktree first. It is the
mid-session complement to the pre-push `protect-main` git-hook (agent-tools#157): that hook
stops a direct PUSH to main; this one stops the AUTHORING that precedes it, so the agent is
redirected to a worktree BEFORE it writes, not after it tries to push. (Alex tg#5742.)

Read-only tools and `git merge` / `git pull` are unaffected — they are not Edit/Write tool
calls, so this pre-write point never sees them.

PER-REPO, opt-in (this must NOT break a repo that legitimately works on main — e.g. 3d-cli):
the guard enforces ONLY where the repo opts in. Opt-in signal, first match wins:
  1. env  RIG_WORKTREE_ONLY=1 (force on) / =0 (force off)   — session/CI override + tests.
  2. the repo's committed rig.yaml → `agent_hooks.worktree_only: true`  (rig-provisioned).
  3. default OFF — a repo with no signal is never blocked.
So hyperide + the agent-ecosystem repos set `worktree_only: true` in their rig.yaml; 3d-cli
leaves it absent and is exempt automatically.

External approval (replaces the OLD self-service escape hatch): there is NO env-var bypass for
this guard any more — an agent could set `RIG_ALLOW_MAIN_EDIT=1` on itself, so that merely
self-granted the very edit the guard exists to redirect. A one-time deliberate main edit is now
requested by exporting `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES="<written justification>"`, which
routes a single approval request to the human via Telegram (deny-by-default; a bare `1` is
rejected). On the human's approval the write is allowed; otherwise it is blocked, leading with
the denial reason. (Mirrors the converted twin pin-primary-worktree; agent-tools#213.)

Contract (agents-hooks/v1):
  stdin  : JSON event; target path in args.file_path/path/notebook_path; event.cwd is the shell dir.
           The verdict is keyed to the checkout the TARGET FILE lives in (its directory), not the
           shell cwd — they can differ (cwd in a worktree, an absolute write into the main checkout).
           See `_target_dir`'s docstring for the no-path-key and relative-path resolution rules
           (agent-tools#615). Two event shapes are not file writes and are ALLOWED outright
           (agent-tools#828): a target that is an omp harness URI (`agent://`, `local://`,
           `proc://`, … — see `_is_harness_uri`), and an omp `eval` cell in which the bridge
           scraped no file write at all (no path, empty `content`).
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": workflow discipline, not a security boundary — a git/parse failure must
never wedge the agent's ability to write. If the branch cannot be determined, we ALLOW.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess  # noqa: S404 — a few read-only `git` queries are the whole job
import sys
from pathlib import Path
from urllib.parse import unquote

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
HOOK_ID = "worktree-only-writes"

# The rig.yaml key (under agent_hooks) that opts a repo INTO worktree-only enforcement.
CONFIG_KEY = "worktree_only"
# Fallback default branch when neither origin/HEAD nor a local main/master branch is resolvable.
_FALLBACK_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT_S = 3.0

# omp harness URI schemes (agent-tools#828). A `write` to one of these is a harness channel —
# a peer message (`agent://`), a session artifact outside the checkout (`local://`), job
# control (`proc://<id>/kill`), a tool device (`xd://`), … — not a file in the checkout.
# Joined onto cwd as a relative path, `agent://Main` used to resolve to `<main>/agent:/Main`
# and was refused as a main-branch write. Exact lowercase match only: any other spelling
# (`AGENT://`, `file://`, `ssh://`) is not recognized and stays judged as a real path. SYNC with
# orchestrator-stays-thin `_is_harness_uri` (same set and exclusions; no shared import by design).
_HARNESS_URI_SCHEMES = frozenset({"agent", "xd", "proc", "artifact", "local", "skill", "history", "mcp"})
# `xd://` devices that stage or apply rewrites of real files keep the normal (cwd) verdict.
_FILE_MUTATING_XD_DEVICES = frozenset({"ast_edit", "resolve"})
_HARNESS_URI_RE = re.compile(r"([a-z]+)://(.*)", re.DOTALL)
# Percent-decoding rounds accepted (`%252e` → `%2e` → `.` is two); a remainder that still
# changes after this many is refused as a channel.
_MAX_UNQUOTE_ROUNDS = 3

# Role-aware "what to do next" builder (agent-tools#994): loaded by repo-local path like the hatch
# helper and OPTIONAL — a load failure falls back to the neutral text in `_render_block` (a crash
# would fail open via on_error and silently drop this block).
_GUARD_MESSAGE_MODULE = "agenttools_guard_message"


def _load_guard_message():
    init = Path(__file__).resolve().parents[2] / "lib" / _GUARD_MESSAGE_MODULE / "__init__.py"
    if not init.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_GUARD_MESSAGE_MODULE, init)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_GUARD_MESSAGE_MODULE)
    # dataclasses resolves string annotations via sys.modules[cls.__module__] while executing.
    sys.modules[_GUARD_MESSAGE_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        if previous is None:
            sys.modules.pop(_GUARD_MESSAGE_MODULE, None)
        else:
            sys.modules[_GUARD_MESSAGE_MODULE] = previous
        if isinstance(exc, KeyboardInterrupt):
            raise
        return None
    return module


_guard_message = _load_guard_message()
# The event main() is deciding, so block emitters can name the reader's role without threading it
# through every signature. One event per hook process.
_CURRENT_EVENT: dict = {}

_BLOCK_WHAT = (
    "You are on the default branch ({branch}) — the worktree-only workflow forbids authoring "
    "files here. main is for merge / pull / read-only operations only "
    "(worktree-only workflow, rig-provisioned; Alex tg#5742). There is NO self-service env-var "
    "bypass."
)
_BLOCK_SELF_SERVE = (
    "Do the work in a separate worktree on a feature branch first, e.g.:\n"
    "    git worktree add ../wt-<feature> -b <feature> origin/{branch}\n"
    "    cd ../wt-<feature>   # then Edit/Write there"
)
_BLOCK_RELAY = (
    "If a subagent reported this block, tell it to work in its own worktree "
    "(`git worktree add ../wt-<feature>`) with `write agent://<id>`, then `wait` for its report."
)
_BLOCK_HUMAN = (
    "a genuine one-off main edit only — request a one-time Telegram approval via "
    'RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES="<justification>" (deny-by-default; bare 1 rejected).'
)


def _render_block(what: str, self_serve: str, human: str = "", relay: str = "") -> str:
    """Role-aware ladder via the shared builder; neutral two-branch text if the lib is unusable."""
    if _guard_message is not None:
        try:
            role = _guard_message.detect_role(_CURRENT_EVENT.get("event") or {})
            return _guard_message.render(
                _guard_message.Remedy(what=what, self_serve=self_serve, relay=relay, human=human),
                role,
            )
        except Exception:  # noqa: BLE001 - a wording helper must never turn a block into a crash
            pass
    top = "3. If you are the top-level agent: " + (relay or "supply the missing step to the blocked subagent.")
    if human:
        top += f" Last resort, genuine exceptions only: {human}"
    return "\n".join([
        what,
        f"1. {self_serve}",
        "2. If you were dispatched by another agent: `write agent://<parent-id>` with what is "
        "missing + what you tried, then `yield` status `awaiting-coordinator` (NOT "
        "blocked/done/failed); the coordinator's `write agent://<you>` revives you. Never retry "
        "with RIG_HATCH_REQUEST_* and never go to the human.",
        top,
    ])


def _block_message(branch: str) -> str:
    return _render_block(
        _BLOCK_WHAT.format(branch=branch),
        _BLOCK_SELF_SERVE.format(branch=branch),
        _BLOCK_HUMAN,
        _BLOCK_RELAY,
    )


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"worktree-only-writes: {msg}\n")


def _git(cwd: str, *args: str) -> str | None:
    """Run a read-only ``git -C cwd <args>`` and return trimmed stdout, or None on any failure.

    None means "could not determine" → the caller fails OPEN (allows). We never raise: a repo
    that is not a git checkout, a missing git, or a timeout must not wedge writing.
    """
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


def current_branch(cwd: str) -> str | None:
    """The checked-out branch name, or None if detached/unknown (→ fail-open allow)."""
    name = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not name or name == "HEAD":  # "HEAD" = detached → cannot compare, allow
        return None
    return name


def default_branch(cwd: str) -> str:
    """The repo's default branch — detected repo-locally, never hardcoded and never machine-derived.

    Resolution order (each repo-local, so enforcement never swings with the developer's machine):
      1. ``git symbolic-ref refs/remotes/origin/HEAD`` (authoritative for any repo with a remote),
      2. a REPO-LOCAL ``init.defaultBranch`` (``git config --local`` — NOT the global setting, which
         would make enforcement machine-dependent (codex)),
      3. a local branch that exists — ``main`` preferred, then ``master``,
      4. the conventional ``main``.
    So a ``master``-only repo is judged against master. A repo whose default is neither and lacks
    origin/HEAD is a corner case (an enrolled PR-workflow repo has origin/HEAD) — set the repo-local
    ``init.defaultBranch`` to disambiguate.
    """
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
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None).

    Pure filesystem walk — no ``git`` subprocess — so the cheap opt-in check runs first and a
    non-enrolled repo never pays for a git query.
    """
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """Read the boolean ``agent_hooks.<key>`` from rig.yaml text — deliberately minimal parse.

    Stdlib-only (the hook carries no PyYAML): scan for the top-level ``agent_hooks:`` block
    (indent 0) and read the key ONLY as a DIRECT child of it — a deeper-nested ``<key>:`` (e.g.
    under ``items.<hook>``) must NOT flip the guard repo-wide. A malformed / absent key returns
    the default (fail-open). This intentionally trades full-YAML fidelity for zero deps — rig.yaml
    files are rig-generated and well-formed, and a parse miss only means "don't enforce", which
    is the safe direction for a discipline gate. Kept in lockstep with the copy in
    orchestrator-stays-thin (SYNC: agent-hooks/orchestrator-stays-thin/orchestrator_stays_thin.py).
    """
    in_block = False
    child_indent: int | None = None  # the indent of agent_hooks' DIRECT children
    for raw in rig_yaml_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()  # drop trailing comment (values here are booleans)
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
            child_indent = indent  # first child fixes the direct-child level
        if indent != child_indent:
            continue  # deeper-nested key (items.<hook>.<k>) → not the block-level knob
        if head == key and ":" in line.strip():
            val = line.strip().split(":", 1)[1].strip().strip("\"'").lower()
            if val in ("true", "yes", "on", "1"):
                return True
            if val in ("false", "no", "off", "0"):
                return False
            return default  # empty (`key:`) or unrecognized value → the default (codex P2)
    return default


def worktree_only_enabled(cwd: str) -> bool:
    """Whether worktree-only enforcement is ON for this repo. env override > rig.yaml > OFF."""
    env = os.environ.get("RIG_WORKTREE_ONLY")
    if env is not None:
        return env.strip() == "1"
    root = _find_rig_yaml(cwd)
    if root is None:
        return False  # no rig.yaml → not enrolled → never block
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return False
    return _agent_hooks_bool(text, CONFIG_KEY, default=False)


def _decide_block(cwd: str, branch: str) -> int:
    """The gate has decided this on-default-branch write must be redirected. Consult the Telegram
    hatch: an unset request is the normal BLOCK; a present request allows on the human's approval
    (tg-ctl exit 0) and blocks (leading with the denial reason) otherwise.

    A pre-write hook has NO shell command string (the event is an Edit/Write), so the
    justification is read purely from the process environment (`RIG_HATCH_REQUEST_…`); `command`
    is not passed."""
    message = _block_message(branch)
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, {"hook": HOOK_ID, "branch": branch}, cwd=cwd,
    )
    if hatch.should_stop:
        if hatch.approved:
            note = f"main edit allowed via hatch escalation ({hatch.reason})"
            warn(note)
            emit("allow", note)
            return 0
        warn(f"main edit hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{message}")
        return BLOCK_EXIT_CODE
    emit("block", message)
    return BLOCK_EXIT_CODE


def _existing_ancestor(start: Path, base: str) -> str:
    """The nearest existing directory at or above ``start`` — the target may not exist yet
    (a create), so walk up to a real directory the git query can run against. Falls back to
    ``base`` if nothing above ``start`` exists either; a downstream git query on a bogus
    ``base`` fails open via this hook's own `on_error: open` contract, not a crash."""
    for cand in (start, *start.parents):
        if cand.exists():
            return str(cand)
    return base


def _is_harness_uri(raw: str) -> bool:
    """Whether ``raw`` names an omp harness channel rather than a file (agent-tools#828).

    Both disqualifiers are judged on the fully percent-decoded remainder, so an encoded form
    cannot slip past either: a `..` segment (`local://../../x` could climb out of the session
    directory into a checkout) and a file-rewriting xd device (`xd://%61st_edit`, and
    `xd://AST_EDIT` — the device name is compared case-insensitively)."""
    match = _HARNESS_URI_RE.fullmatch(raw.strip())
    if match is None or match.group(1) not in _HARNESS_URI_SCHEMES:
        return False
    scheme, rest = match.group(1), match.group(2)
    for _ in range(_MAX_UNQUOTE_ROUNDS + 1):  # the extra pass only confirms stability
        decoded = unquote(rest)
        if decoded == rest:
            break
        rest = decoded
    else:
        return False  # still changing after the last round → refuse to call it a channel
    if ".." in rest.replace("\\", "/").split("/"):
        return False
    device = re.split(r"[/?#]", rest, maxsplit=1)[0].lower()
    return not (scheme == "xd" and device in _FILE_MUTATING_XD_DEVICES)


def _target_path(args: dict) -> str | None:
    """The first usable file_path/path/notebook_path value — the one `_target_dir` judges —
    or None when no key carries a non-blank string (agent-tools#615)."""
    for key in ("file_path", "path", "notebook_path"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def _target_dir(cwd: str, args: dict) -> str:
    """The directory whose branch decides the verdict: the WRITE TARGET's dir, else `cwd`.

    Resolves the branch from the target file's directory, not the shell's cwd — an absolute
    write into the main checkout must be judged as main even when cwd is a feature worktree
    (and vice-versa). A relative path is judged against `cwd`. The file may not exist yet (a
    create) → walk up to the nearest existing ancestor for the git query.

    The target is `.parent`ed ONLY when it does not already resolve to an existing directory
    (agent-tools#616 follow-up). A naive unconditional `.parent` is wrong whenever the raw
    path itself denotes a directory rather than a not-yet-created file: a bare "." collapses
    (pathlib drops single-dot components) to exactly `cwd`, and `cwd.parent` then climbs ONE
    LEVEL ABOVE it; a relative path that happens to name an existing directory (e.g. another
    worktree reached via `.worktrees/<name>`, no file component beneath it) has the same
    problem. In a sibling-worktree layout that overshoot is harmless (the directory above a
    sibling worktree is not a git repo, so the check fails open) — but in the REALISTIC
    nested `rig worktree create` layout (`<repo>/.worktrees/<name>`), the directory one level
    above a worktree root (`<repo>/.worktrees`) sits squarely inside the PRIMARY checkout's
    own git boundary, so the overshoot silently swapped in main's branch for a write that was
    genuinely happening on a feature branch, in the correct place. Treating an existing
    directory as its OWN target (not its parent's) fixes both directions without touching the
    documented not-yet-created-file walk (`_existing_ancestor`) or the still-intentional
    behavior of a genuine `..` that escapes `cwd` into a different real checkout.

    DELIBERATE fallback to `cwd` when NO file_path/path/notebook_path key is usable at all —
    whether a key is missing entirely or present but blank/wrong-typed, both are "no path
    signal" and get the identical, tested outcome (agent-tools#615, replacing the prior
    UNDOCUMENTED silent fallthrough with this explicit, deliberate, tested one). codex's
    `apply_patch` bridge (its only write tool) genuinely produces a path-less pre-write event
    when its patch-envelope parser finds no file path in the command text
    (lib/codex_hook_bridge/dispatch.py's `to_v1_event`) — failing OPEN in that case would
    turn a real parse hiccup into a silent bypass of the very guard this hook exists to
    enforce. Judging `cwd` directly keeps this hook's normal posture (BLOCK on main, ALLOW on
    a feature branch) even when the target path itself could not be determined.

    Known limitation (agent-tools#613/#615, tracked in #616): a relative hashline path is
    judged against `cwd` even when the file really lives in a different worktree — a
    same-repo sibling-worktree existence guess was tried and reverted as exploitable. Use an
    absolute path, or the `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES` escape hatch, for that case.
    """
    base = cwd or os.getcwd()
    raw = _target_path(args)
    if raw is None:
        return base
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = Path(base) / target
    start = target if target.is_dir() else target.parent
    return _existing_ancestor(start, base)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    _CURRENT_EVENT["event"] = event
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    raw_target = _target_path(args)
    # agent-tools#828: a harness URI is not a file in any checkout, and an omp `eval` cell in
    # which the bridge scraped no write (no path, empty content) authors nothing. A path-less
    # eval WITH scraped content is a write to an unresolved target → the #615 cwd fallback.
    if raw_target is not None and _is_harness_uri(raw_target):
        emit("allow")
        return 0
    if raw_target is None and event.get("tool") == "eval" and not args.get("content"):
        emit("allow")
        return 0
    # Judge the checkout the TARGET FILE lives in, not the shell cwd (they can differ).
    where = _target_dir(cwd, args)

    # 1. Cheapest gate first: is this repo enrolled? (env / rig.yaml). No git cost if not.
    if not worktree_only_enabled(where):
        emit("allow")
        return 0

    # 2. Only enforce when actually sitting on the default branch.
    branch = current_branch(where)
    if branch is None:
        emit("allow")  # detached / not a git repo / git error → fail-open
        return 0
    if branch != default_branch(where):
        emit("allow")  # on a feature branch → exactly where authoring belongs
        return 0

    # 3. On the default branch: BLOCK, unless a one-time Telegram hatch request is approved.
    return _decide_block(where, branch)


if __name__ == "__main__":
    sys.exit(main())
