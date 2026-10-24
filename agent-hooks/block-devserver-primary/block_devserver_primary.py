#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block launching a dev server / build watcher against the
checkout sitting on the repo's DEFAULT branch, on an ENROLLED repo.

Incident this closes (2026-08-28, hyperide session internal task #191, agent-tools#454): agents
doing a quick "live verification" pass — start a dev server, check a fix renders — kept doing it
directly against the SHARED checkout instead of a disposable worktree. In hyperide this is not
hypothetical: the canvas preview generator (`lib/preview-generator`) OVERWRITES `client/App.tsx`
and the git-TRACKED `client/__canvas_preview__.tsx` (see hyperide's own `.gitignore` comment: it is
"deliberately TRACKED... hyperide self-hosts inside HyperIDE") as a SIDE EFFECT of the dev server
running — no `git` command, no `Edit`/`Write` tool call, nothing the sibling hooks below can see.
One such pass corrupted both files with 2000+ generated lines, requiring a dedicated cleanup pass.

This hook is the Bash-launch counterpart to the two existing worktree-only-workflow hooks:
  - `worktree-only-writes` (pre-write) blocks an Edit/Write while on the default branch, but a
    dev server's own writes are not Edit/Write tool calls — invisible to it.
  - `pin-primary-worktree` (pre-bash) blocks a `git checkout`/`switch` off the default branch, but
    `npm run dev` is not a `git` command — invisible to it.
  - Claude Code's OWN built-in worktree-isolation Bash guard (a separate, upstream feature) only
    inspects commands that reach into the checkout via `cd`/`-C` AND run `git` — verified live: a
    plain `cd <shared-checkout> && npm run dev` (zero git) sails through it untouched.
None of the three sees "a dev server about to run against the checkout that must stay on main."
This hook closes exactly that gap: it recognizes common dev-server / dev-watch launch commands
(`npm run dev`, `vite`, `next dev`, ...) and BLOCKS them when the effective working directory (the
event `cwd`, adjusted for a literal leading `cd <dir> &&` prefix in the SAME command) sits on the
enrolled repo's default branch — the same condition `worktree-only-writes` already uses for
Edit/Write, applied to the one Bash shape that bypasses it.

Deliberately NOT covered: generic file redirection (`>`, `tee`), `sed -i`/`perl -i`, or `rm` run
directly in Bash targeting a tracked source file — the sibling `no-shell-file-edit` hook (also
`pre-bash`, unconditional, NOT tied to `worktree_only` enrollment) already hard-blocks exactly
that class repo-wide, everywhere, on every branch — see its own README for the full parsed-not-
raw-matched command coverage (`sed -i`, `perl -i`, `gawk -i inplace`, `> file`/`>> file`/`tee`/
`dd of=`, wrapped in `bash -c`/`sh -c`, VAR= prefixes peeled). A worktree-scoped reimplementation
here would be redundant with it, not a gap. `git commit` directly in the primary checkout is
likewise handled by DESIGN elsewhere in the layering, not left open: `require-review-before-
commit`/`require-ticket-before-commit` gate every commit regardless of location, and even a
committed-but-unpushed change on the primary checkout's main is caught at push time by the
pre-push `protect-main` git-hook (agent-tools#157) — `worktree-only-writes`'s own docstring
documents this split (pre-write blocks the AUTHORING, pre-push blocks the PUSH) — so adding a
`git commit`-specific check here would duplicate an existing, deliberate gate rather than close
a real one. What remained genuinely uncovered — confirmed by testing this repo's own machine,
not assumed — was a dev-server LAUNCH: no existing hook (this repo's own or Claude Code's
built-in worktree-isolation Bash guard, which is git-command-focused) recognizes `npm run dev`/
`vite`/etc. at all, on any branch, in any checkout. That is the one gap this hook exists to
close, with a narrow, low-false-positive signature (a fixed handful of well-known
package-manager scripts and CLI binaries) rather than a general "any Bash mutation" detector.

PER-REPO, opt-in: reuses the EXACT SAME `agent_hooks.worktree_only` knob as `worktree-only-writes`
and `pin-primary-worktree` (env `RIG_WORKTREE_ONLY` > rig.yaml `agent_hooks.worktree_only` >
default OFF) — one feature, one flag, three enforcement points.

External approval: same deny-by-default hatch pattern as the sibling hooks. No self-service
env-var bypass. `RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY="<justification>"` asks the human via
Telegram once; approved → allow, otherwise (or unset) → block.

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command/cmd (the bash string); event.cwd is the shell dir.
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": workflow discipline, not a security boundary — a git/parse failure must never
wedge the agent's ability to run a command.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess  # noqa: S404 — a few read-only `git` queries are the whole job
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
HOOK_ID = "block-devserver-primary"

CONFIG_KEY = "worktree_only"  # SYNC: same knob as worktree-only-writes / pin-primary-worktree
_FALLBACK_DEFAULT_BRANCH = "main"
_GIT_TIMEOUT_S = 3.0

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
    "You are on the default branch ({branch}) of an enrolled repo — starting a dev server / "
    "build watcher here is blocked by the worktree-only workflow. A dev server's own writes "
    "(HMR output, a preview/codegen generator, etc.) can silently corrupt tracked files in the "
    "SHARED checkout with no Edit/Write tool call to catch — that already happened once here. "
    "There is NO self-service env-var bypass."
)
_BLOCK_SELF_SERVE = (
    "Run it from a separate worktree instead, e.g.:\n"
    "    git worktree add ../wt-<feature> -b <feature> origin/{branch}\n"
    "    cd ../wt-<feature> && {command}"
)
_BLOCK_RELAY = (
    "If a subagent reported this block, tell it to run the server from its own worktree "
    "(`git worktree add ../wt-<feature>`) with `write agent://<id>`, then `wait` for its report."
)
_BLOCK_HUMAN = (
    "a genuine one-off need only — request a one-time Telegram approval via "
    'RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY="<justification>" '
    "(deny-by-default; a bare 1 is rejected)."
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


def _block_message(branch: str, label: str) -> str:
    return _render_block(
        _BLOCK_WHAT.format(branch=branch),
        _BLOCK_SELF_SERVE.format(branch=branch, command=label),
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
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _git(cwd: str, *args: str) -> str | None:
    """Run a read-only ``git -C cwd <args>``; None on any failure (caller fails OPEN)."""
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
    name = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if not name or name == "HEAD":
        return None
    return name


def default_branch(cwd: str) -> str:
    """SYNC: identical resolution order to worktree-only-writes.default_branch — see that
    docstring for the full rationale (repo-local, never machine-derived)."""
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
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """SYNC: identical minimal parse to worktree-only-writes._agent_hooks_bool — see that
    docstring for the full rationale (direct-child-only, stdlib-only, fail-open on any miss)."""
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
            val = line.strip().split(":", 1)[1].strip().strip("\"'").lower()
            if val in ("true", "yes", "on", "1"):
                return True
            if val in ("false", "no", "off", "0"):
                return False
            return default
    return default


def worktree_only_enabled(cwd: str) -> bool:
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


# ── bash chain + `cd` tracking ────────────────────────────────────────────────────────────────
# NOTE: originally a byte-identical SYNC copy of pin-primary-worktree._split_chain (itself
# copied from orchestrator-stays-thin._split_chain). It has since DIVERGED — this copy now also
# returns each segment's preceding operator (needed for conditional-`cd` tracking, see `main()`)
# and honors backslash-escaped double quotes (Codex review, PR agent-tools#469) — neither fix
# has been backported to the siblings. No test enforces byte-identity across the three copies.

def _split_chain(command: str) -> list[tuple[str | None, str]]:
    """Split on shell operators (&&, ||, ;, |, newline, a bare control &) that lie OUTSIDE quotes.

    Returns ``(preceding_operator, segment_text)`` pairs — the operator that separates this
    segment from the one before it (``None`` for the very first segment). `main()` uses this
    two ways: (1) to tell an UNCONDITIONALLY-reached `cd` (the first segment, or one following
    `;`/newline/`&`, which always runs regardless of any prior command's exit code) from a
    CONDITIONALLY-reached one (following `&&`/`||`, whose execution depends on an exit code this
    hook has no way to evaluate — see `main()`'s docstring note for how it handles this
    uncertainty); (2) alongside the FOLLOWING segment's own preceding-operator, to tell a `cd`
    that runs in an isolated subshell (piped into/out of via `|`, or backgrounded via `&`, either
    of which discards its directory change once that subshell exits) from one that actually
    affects the parent shell's cwd.

    A double-quoted region honors a backslash-escaped quote (``\\"``) as a literal character, not
    the end of the string — an odd number of trailing backslashes immediately before a `"` escapes
    it (Codex review, PR agent-tools#469). Without this, ``printf '%s' "a\\"b" && npm run dev``
    closes the quoted region one character early ('a\\"' reads as a complete, escaped-quote-naive
    string), leaving the trailing ``b" && npm run dev`` parsed as UNQUOTED text — the `&&` inside
    it is then treated as a real chain-operator split point mid-string, silently misplacing where
    the `npm run dev` member actually starts. The same escape-awareness also applies when
    OPENING a quote from outside any quoted region: `echo \\" && npm run dev`'s `\\"` is a literal
    escaped quote in a real shell, not a region opener — unconditionally opening one there (an
    earlier version of this function did) swallows the real `&&` that follows into the
    "unterminated string", hiding the `npm run dev` member from ever being split out at all.
    Single quotes need no closing-escape handling — POSIX single quotes are fully literal,
    backslash has no special meaning inside them, so the existing generic "close on matching
    quote char" branch is already correct for closing one; OPENING one is still escape-checked
    (`\\'` outside quotes is equally a literal character, not a region opener).

    Two more escape/comment-awareness fixes (Codex review, PR agent-tools#469, round 3):

    - A backslash-escaped chain-operator character OUTSIDE any quoted region (`\\;`, `\\&`, `\\|`)
      is a LITERAL character passed as a plain argument, not a real operator. `echo \\; cd
      /feature; npm run dev` in a real shell is `echo` given the literal argument tokens `;`,
      `cd`, `/feature` (`\\;` never separates anything) — the command is just `echo ; cd
      /feature`, printed, and `cd` never actually runs; only the trailing UNESCAPED `;` really
      separates that whole `echo ...` command from `npm run dev`. Splitting on the escaped `;`
      anyway (an earlier version of this function did) makes `cd /feature` look like its own,
      unconditionally-reached segment — the hook then wrongly moves `effective_cwd` to
      `/feature` for a `cd` that never actually executes, and judges the real `npm run dev`
      (which really launches in the ORIGINAL, still-protected checkout) against the wrong,
      harmless directory instead.
    - An unquoted, unescaped `#` at the START OF A WORD (buf is empty, or its last character is
      a space/tab — i.e. not partway through an existing token, matching real shell comment
      syntax) begins a shell COMMENT that runs to the next real newline; nothing inside it —
      including what LOOKS like a chain operator — is real shell syntax. `: # comment ; cd
      /feature` + newline + `npm run dev` is bash's no-op command `:`, then a comment consuming
      the rest of the line (the `;` and `cd /feature` inside it are just comment TEXT, `cd` never
      runs), then a real newline separates it from the always-run `npm run dev` — which launches
      in the ORIGINAL, still-protected checkout. Treating the commented `;` as a real operator
      (an earlier version of this function did) again wrongly tracks a `cd` that never executes."""
    _WORD_START_CHARS = (" ", "\t")

    def _escaped(buf: list[str]) -> bool:
        bs = 0
        j = len(buf) - 1
        while j >= 0 and buf[j] == "\\":
            bs += 1
            j -= 1
        return bs % 2 == 1  # odd trailing run — the character about to be read is escaped

    segs: list[tuple[str | None, str]] = []
    buf: list[str] = []
    quote: str | None = None
    pending_op: str | None = None
    in_comment = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        prev = command[i - 1] if i > 0 else ""
        nxt = command[i + 1] if i + 1 < n else ""
        if in_comment:
            i += 1
            if c == "\n":
                in_comment = False
                segs.append((pending_op, "".join(buf)))
                pending_op = "\n"
                buf = []
            # comment text itself is discarded, not appended to buf — it is not real shell
            # syntax and could otherwise confuse the later shlex.split of this segment (e.g. an
            # unbalanced quote inside human-written comment text).
        elif quote == '"' and c == '"':
            # Handled fully here whether escaped or not — must NOT fall through to the generic
            # `quote is not None` branch below, which closes unconditionally on any matching
            # quote char: that would silently undo the escape-awareness this branch exists for.
            closes = not _escaped(buf)
            buf.append(c)
            i += 1
            if closes:
                quote = None
        elif quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
        elif c in ("'", '"') and not _escaped(buf):
            buf.append(c)
            i += 1
            quote = c
        elif c == "#" and not _escaped(buf) and (not buf or buf[-1] in _WORD_START_CHARS):
            in_comment = True
            i += 1
        elif command[i:i + 2] in ("&&", "||") and not _escaped(buf):
            segs.append((pending_op, "".join(buf)))
            pending_op = command[i:i + 2]
            buf = []
            i += 2
        elif c == "&" and prev not in ("&", ">") and nxt not in ("&", ">") and not _escaped(buf):
            segs.append((pending_op, "".join(buf)))
            pending_op = "&"
            buf = []
            i += 1
        elif c == "\n":
            # ALWAYS a real separator here, unconditionally — NOT escape-gated like `;`/`|`
            # below. A real `\<newline>` line continuation is a DIFFERENT bash mechanism (removes
            # both characters entirely) already fully resolved by `_normalize_line_continuations`
            # before this function ever runs; by the time a `\n` reaches this scanner, any real
            # continuation has already been folded away, and what backslash(es) remain
            # immediately before a surviving `\n` (from an EVEN run — see that function's own
            # docstring) are literal characters already accounted for in `buf`, not an escape of
            # THIS newline. Re-checking `_escaped(buf)` here double-applies the same backslash
            # and wrongly treats a genuine separator newline as escaped, silently merging two
            # real, separate commands into one segment.
            segs.append((pending_op, "".join(buf)))
            pending_op = c
            buf = []
            i += 1
        elif c in (";", "|") and not _escaped(buf):
            segs.append((pending_op, "".join(buf)))
            pending_op = c
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    segs.append((pending_op, "".join(buf)))
    return [(op, s) for op, s in segs if s.strip()]


# A segment preceded by one of these operators only runs if the PRECEDING segment's exit code
# meets a condition (`&&` = succeeded, `||` = failed) that this hook has no shell to evaluate —
# see the `cd`-conditional-tracking note in `main()`. Every other preceding separator (`;`,
# newline, background `&`, or no separator at all — the first segment) runs unconditionally.
# `|` is handled separately, by `_ISOLATING_OPS` below — a segment either side of a pipe is not
# "conditional" in the &&/|| sense (it always RUNS), but its `cd` effect never reaches the
# parent shell either way, for a different reason (subshell isolation, not uncertain exit code).
_CONDITIONAL_OPS = frozenset({"&&", "||"})

# A `cd` whose OWN following operator is one of these runs in a subshell that bash forks to
# execute it — a pipeline stage (`cd X | cat`) or a backgrounded job (`cd X &`) — so its
# directory change is discarded when that subshell exits and never reaches the parent shell,
# REGARDLESS of the operator preceding it. Same for a `cd` that is itself the RECEIVING end of a
# pipe (preceding operator `|`): by default (no `shopt -s lastpipe`, which this hook cannot
# detect and does not assume), every pipeline stage — including the last — runs in a subshell.
# Deterministic, unlike `_CONDITIONAL_OPS`: a piped/backgrounded `cd` NEVER persists, there is no
# "maybe it did" branch to account for.
_ISOLATING_OPS = frozenset({"&", "|"})

_ASSIGN_RE = re.compile(r"^\w+=")


def _strip_leading_assignments(toks: list[str]) -> list[str]:
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    return toks[i:]


# Bare (no-flag) no-op command wrappers that prefix the REAL command — the same wrapper family
# the sibling `no-shell-file-edit` hook peels (see its own `_WRAPPERS`/`_peel_wrappers` for the
# fuller, flag-aware version this one deliberately does NOT replicate in full — see
# `_peel_command_wrappers`'s docstring). `nohup npm run dev &` is the single most realistic real
# shape of the incident this hook exists to prevent — "start a dev server and detach it" — so
# leaving it unhandled was a real gap, not a cosmetic one. `timeout` is deliberately EXCLUDED:
# that's `no-long-inline-process`'s own concern (does this bash call need `run_in_background`),
# not this hook's.
_WRAPPER_HEADS = frozenset({"nohup", "env", "sudo", "time", "nice", "setsid", "command", "exec"})


def _peel_command_wrappers(toks: list[str]) -> list[str]:
    """Strip a leading run of BARE `_WRAPPER_HEADS` wrappers (and any `VAR=val` assignments
    interleaved with them — `env`'s own arguments look exactly like the assignment prefix
    `_strip_leading_assignments` already handles, so re-running that stripper each iteration
    naturally unwraps `env VAR=val npm run dev` with no `env`-specific code) so the real
    dev-server-launching command sits at the head.

    Deliberately conservative, NOT a full flag-aware unwrap like the sibling's: a wrapper token
    immediately followed by something that LOOKS like a flag (`sudo -u dev npm run dev`, `nice
    -n10 npm run dev`) is left ALONE — unpeeled, hence unclassified, hence a MISS (the safe
    direction: worse coverage, never a false allow from a mis-parsed peel). Tracked as a
    follow-up ([agent-tools#463](https://git.hyperide.ai/ultrabricks/agent-tools/issues/463)) to
    extend this to the sibling's full flag-aware peel and to `bash -c`/`sh -c` re-parsing —
    this bare form alone already closes the `nohup .../env .../sudo ... &` shape without that
    added complexity."""
    toks = list(toks)
    while True:
        toks = _strip_leading_assignments(toks)
        if toks and toks[0] in _WRAPPER_HEADS and len(toks) > 1 and not toks[1].startswith("-"):
            toks = toks[1:]
            continue
        break
    return toks


def _resolve_relative_path(raw: str, base_cwd: str) -> str:
    """A ``cd``/directory-flag argument, resolved to an absolute path against ``base_cwd``.
    `~`/`~user` is expanded first — the idiomatic spelling for both a `cd` target and a flag
    value like `--prefix ~/work/hyperide`."""
    target = Path(os.path.expanduser(raw))
    if not target.is_absolute():
        target = Path(base_cwd or ".") / target
    return str(target)


def _resolve_cd_target(toks: list[str], base_cwd: str) -> str | None:
    """``toks`` is a `cd`-headed segment's tokens (after stripping assignments). Returns the
    resolved absolute target directory ONLY IF IT EXISTS ON DISK RIGHT NOW, or None otherwise —
    None means "don't update `effective_cwd`", so the caller keeps judging later segments
    against wherever it already was.

    The disk-existence check is load-bearing, not cosmetic: a naive version that always
    "succeeds" here is a real bypass. `cd /does-not-exist; npm run dev` (or `... || npm run
    dev`) in a REAL shell leaves the shell in its ORIGINAL directory when `cd` fails — but a
    version of this function that unconditionally returned the (nonexistent) target would move
    `effective_cwd` there, and the following `npm run dev` would then be judged against a
    directory with no git repo at all → `_resolve_gate` fails open → ALLOW, even though the real
    dev server is about to start in the still-protected original checkout. Checking `is_dir()`
    closes this: an unresolvable target is treated the same as a bare `cd`/`cd -` — untracked,
    so the caller keeps evaluating against the REAL (unchanged) cwd, which is the safe direction
    even in the false-positive case (a `mkdir -p new && cd new && ...` where `new` doesn't exist
    at hook-eval time yet — this hook has no shell to run the `mkdir` first, so `new` looks
    nonexistent and a later segment is judged against the stale cwd instead; over-blocking, not
    under-blocking).

    A bare `cd` (→ $HOME) or `cd -` (→ `$OLDPWD`, a value this hook never sees) are likewise
    both untracked: the running cwd is left AS-IS (fail open on TRACKING specifically, not on
    the gate decision — see the module docstring's fail-open note), so a later segment in the
    same chain is judged against whatever cwd was already tracked, which may be stale after
    either shape."""
    if toks[0] != "cd":
        return None
    args = [t for t in toks[1:] if not t.startswith("-")]
    if not args:
        return None  # bare `cd` / `cd -` — not resolvable here, next segment judged on stale cwd
    resolved = _resolve_relative_path(args[0], base_cwd)
    if not Path(resolved).is_dir():
        return None  # this `cd` would FAIL in a real shell — don't move effective_cwd there
    return resolved


# ── dev-server / dev-watch launch detection ─────────────────────────────────────────────────
#
# Deliberate asymmetry: `npm start` / `npm run preview` block, while the semantically identical
# `next start` / `vite preview` invoked DIRECTLY are allowed (see `_is_runner_devserver`). A
# package.json script name is OPAQUE — `"start": "next dev"` and `"start": "node
# server-that-doesnt-touch-source.js"` are indistinguishable from the command line alone, so
# every name in `_PM_DEV_SCRIPTS` is treated as potentially a dev watcher. A DIRECT runner-tool
# invocation has no such ambiguity: `next start` / `vite preview` are KNOWN to serve an
# already-built production bundle, not write source, so they're excluded there specifically.

_PM_DEV_SCRIPTS = frozenset({"dev", "start", "preview", "serve"})
# bun has no `preview`/`serve` script convention of its own — only `dev`/`start` are recognized.
_BUN_DEV_SCRIPTS = frozenset({"dev", "start"})
_PM_NAMES = frozenset({"npm", "yarn", "pnpm"})
_RUNNER_TOOLS = frozenset({"vite", "next", "astro"})
_WEBPACK_TOOLS = frozenset({"webpack", "webpack-dev-server"})
_NON_SERVER_SUBCOMMANDS = frozenset({"build", "preview", "optimize", "lint", "check", "generate"})
# Read-only / informational flags that never start a dev server, even for a tool whose bare
# invocation otherwise IS one (`vite --version`, `vite --help`).
_INFO_FLAGS = frozenset({"--help", "-h", "--version", "-v"})


def _is_runner_devserver(tool: str, sub: str | None) -> bool:
    """True iff invoking ``tool`` with subcommand/flag-or-None ``sub`` starts a dev server.

    `vite`'s bare/flag form IS the dev server — any subcommand OTHER than a known non-server
    one (`build`, `preview`, ...) or an info flag (`--help`, `--version`) counts. `next`/`astro`
    are the opposite shape: only the explicit `dev` subcommand is a server —
    `build`/`start`/`preview`/anything unrecognized serves or builds an already-built
    production bundle, not a source-writing dev watcher. Applied identically whether the tool
    is invoked directly (`vite`, `next dev`) or through `npx`/`bunx`/`npm exec`/`pnpm dlx`
    (`npx vite`, `npx next dev`) — the call shapes must agree, since a dev-server launch is
    exactly as risky either way."""
    if tool == "vite":
        return sub not in _NON_SERVER_SUBCOMMANDS and sub not in _INFO_FLAGS
    return sub == "dev"


def _is_dev_script(script: str, known: frozenset[str] = _PM_DEV_SCRIPTS) -> bool:
    """True for an exact known dev-server script name, OR a colon-namespaced variant of one
    (`dev:client`, `start:watch`) — a common convention (`npm run dev:client`) this hook would
    otherwise miss entirely since the exact-match set can't enumerate every project's naming.
    The prefix before the FIRST `:` is what's checked, so `dev:client:debug` still matches on
    `dev` and an unrelated script (`devtools-build`, `predev`) does not, since neither has a
    `:`-delimited `dev`/`start`/`preview`/`serve` prefix segment."""
    return script in known or script.split(":", 1)[0] in known


# Directory-changing flags recognized on a package-manager invocation, mapped to a `cd`-like
# cwd override for THAT ONE launch only (never persisted to the running `effective_cwd`, mirrors
# how `git -C <dir>` is scoped in `pin_primary_worktree`). Only the two-token `--flag value`
# form is recognized (not `--flag=value`) — this is a discipline heuristic, not a full CLI
# parser, and the two-token form is what the documented incident shape actually looks like:
# `npm --prefix <shared-checkout> run dev`.
_DIR_FLAGS = frozenset({"--prefix", "-C", "--dir", "--cwd"})
# Other KNOWN value-taking global flags on npm/yarn/pnpm that would otherwise defeat script-name
# detection by being mistaken for the `run`/script-name position itself (they don't change cwd,
# so no override — just skip the flag+value pair so scanning reaches the real script token):
# workspace/monorepo filtering (`pnpm --filter web dev`, `npm -w client run dev`, `yarn workspace
# web dev` is a DIFFERENT shape — not covered, see README).
_PM_SKIP_VALUE_FLAGS = frozenset({"--filter", "-F", "--workspace", "-w"})
# A flag recognized specifically between `run` and the script name (`npm run --if-present dev`)
# — valueless, so no pairing needed, just skipped in that one position.
_RUN_FLAGS = frozenset({"--if-present", "--silent", "-s"})
# A VALUE-TAKING flag recognized specifically between `run` and the script name (`npm run
# --workspace web dev`, `npm run --script-shell /bin/bash dev`) — checked against npm 11.4.2's
# own `npm run --help` (Codex review, PR agent-tools#469, round 3): consumed as a flag+value
# PAIR, split or `=`-joined, same treatment `_peel_dir_flag` already gives `_PM_SKIP_VALUE_FLAGS`
# in the run's own GLOBAL-OPTIONS region. Missing this made `npm run --workspace web dev` stop
# scanning at `web` (not `-`-prefixed) and misread it as the script-name position — an
# under-block, the real `dev` script never gets recognized.
_RUN_VALUE_FLAGS = frozenset({"--workspace", "-w", "--script-shell"})
# `npx`/`bunx`-equivalent subcommands recognized on npm/yarn/pnpm ("run a package once"), so
# `npm exec vite` / `pnpm dlx vite` are classified exactly like `npx vite`.
_PM_EXEC_SUBCOMMANDS = frozenset({"exec", "dlx"})


def _peel_dir_flag(rest: list[str]) -> tuple[list[str], str | None]:
    """Remove leading global-option tokens from ``rest`` — a directory-changing flag(+value),
    split (`--prefix /dir`) OR `=`-joined (`--prefix=/dir`) (`_DIR_FLAGS`, becomes the returned
    override), another KNOWN value-taking flag+value pair, split or `=`-joined
    (`_PM_SKIP_VALUE_FLAGS`, skipped with no override), or any other single valueless-looking
    flag token — stopping at the first REAL non-flag token (the `run`/script-name itself) or a
    literal `--` (npm's own "everything after this is forwarded verbatim to the script, not
    npm's own option" separator). This is npm/yarn/pnpm's own GLOBAL-OPTIONS region, which comes
    BEFORE `run`/the script name in real usage (`npm --prefix <dir> run dev`, `pnpm -C <dir>
    dev`, `pnpm --filter web dev`).

    Scanning the WHOLE array (a prior version of this function did) is a real bypass, not just
    imprecise: `npm run dev -- --prefix /feature-worktree` forwards `--prefix /feature-worktree`
    to the `dev` SCRIPT as an argument — npm's own cwd is untouched, the dev server still starts
    in the invoking checkout — but a whole-array scan reads that forwarded flag as npm's own
    `--prefix`, computes a `cwd_override` pointing at the harmless feature worktree, and the
    gate wrongly judges (and allows) a launch that actually runs in the protected checkout.
    Restricting the scan to the LEADING run, stopping at `--`, closes this: a flag appearing
    after `run <script>`/`--` is never treated as npm's own option.

    The `=`-joined form is ALSO a real bypass, not just imprecision, if unhandled: from a
    harmless feature worktree, `npm --prefix=/path/to/shared-main run dev` really does make npm
    operate against the shared checkout — but the split-form-only version of this function
    couldn't recognize `--prefix=/path/to/shared-main` as `_DIR_FLAGS`, left `cwd_override`
    unset, and the gate fell back to `effective_cwd` (the harmless feature worktree) — wrongly
    ALLOWING a launch that actually targets the protected checkout.

    `_PM_SKIP_VALUE_FLAGS` exists because a NAIVE "skip one token per flag" (the fallback below,
    for any OTHER unrecognized flag) breaks on a value-taking flag whose value doesn't itself
    start with `-`: `pnpm --filter web dev` would otherwise stop at `web` (not `-`-prefixed) and
    misread it as the script-name position, missing `dev` entirely — an under-block (safe: a
    MISS, not a wrong-cwd allow), not a bypass. This is not a general CLI parser: an UNKNOWN
    SPLIT-form value-taking flag (anything outside `_DIR_FLAGS`/`_PM_SKIP_VALUE_FLAGS`, e.g.
    `npm --userconfig /tmp/npmrc run dev`) still has this exact miss — see the README's scope
    limits; the `=`-joined form of an unknown flag is self-contained in one token and does NOT
    have this problem regardless of whether the flag is recognized."""
    override: str | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        flag, eq, value = tok.partition("=")
        if tok == "--":
            break
        if flag in _DIR_FLAGS and eq and override is None:  # `--prefix=/dir` — one token
            override = value
            i += 1
            continue
        if flag in _DIR_FLAGS and not eq and i + 1 < len(rest) and override is None:
            override = rest[i + 1]  # `--prefix /dir` — two tokens
            i += 2
            continue
        if flag in _PM_SKIP_VALUE_FLAGS and eq:  # `--filter=web` — one token
            i += 1
            continue
        if flag in _PM_SKIP_VALUE_FLAGS and not eq and i + 1 < len(rest):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1  # some other leading flag this hook doesn't track — keep scanning past it
            continue
        break  # first non-flag token — this is `run`/the script name; stop peeling
    return rest[i:], override


def _peel_run_flags(script_toks: list[str]) -> list[str]:
    """Consume every leading flag recognized in the `run [flags] <script>` position — both the
    valueless `_RUN_FLAGS` (`--if-present`, `--silent`) and the value-taking `_RUN_VALUE_FLAGS`
    (`--workspace`/`-w`, `--script-shell`), split OR `=`-joined for the latter — stopping at the
    first token that is neither, which is the script name itself. Shared between npm/yarn/pnpm's
    `_classify_pm_segment` and bun's `_classify_bun_segment`'s own `run` branch (Codex review, PR
    agent-tools#469, round 3) so both agree on the same recognized-flags set."""
    i = 0
    while i < len(script_toks):
        tok = script_toks[i]
        flag, eq, _value = tok.partition("=")
        if flag in _RUN_FLAGS:
            i += 1
            continue
        if flag in _RUN_VALUE_FLAGS and eq:  # `--workspace=web` — one token
            i += 1
            continue
        if flag in _RUN_VALUE_FLAGS and not eq and i + 1 < len(script_toks):  # `--workspace web`
            i += 2
            continue
        break
    return script_toks[i:]


def _skip_flags(toks: list[str]) -> int:
    """Index of the first non-flag token in ``toks`` (flag VALUES are not skipped — good enough
    for the narrow `npx`/`bunx [flags] <tool> [subcommand]` shape this hook targets)."""
    i = 0
    while i < len(toks) and toks[i].startswith("-"):
        i += 1
    return i


def _classify_pm_segment(head: str, rest: list[str]) -> tuple[str, str | None] | None:
    """npm/yarn/pnpm: `run [flag] <script>`, a bare known script name, or `exec`/`dlx [--]
    <tool>` (the package-manager equivalent of `npx`/`bunx`). Returns ``(label, cwd_override)``."""
    rest, cwd_override = _peel_dir_flag(rest)
    if rest[:1] == ["run"]:
        # Consume EVERY leading `_RUN_FLAGS`/`_RUN_VALUE_FLAGS` token, not just one, and not
        # just the valueless ones (Codex review, PR agent-tools#469, two rounds): `npm run
        # --silent --if-present dev` stopping after `--silent` reads `--if-present` as the
        # script-name position and misses `dev`; `npm run --workspace web dev` similarly stops
        # at `web` (not `-`-prefixed) without consuming its value. Both are a real under-block
        # (the launch is allowed on the protected default branch), not the safe over-block
        # direction — these are ordinary, independently-documented `npm run` usages.
        script_toks = _peel_run_flags(rest[1:])
        if script_toks and _is_dev_script(script_toks[0]):
            return f"{head} run {script_toks[0]}", cwd_override
        return None
    if rest[:1] and rest[0] in _PM_EXEC_SUBCOMMANDS and len(rest) > 1:
        # `_skip_flags` (shared with `_classify_npx_segment`) eats the literal `--` separator
        # AND any recognized-or-not valueless flag before the tool name in one pass — covers
        # both `exec -- <tool>` and a documented exec/dlx option like `--silent` (Codex review,
        # PR agent-tools#469: `pnpm dlx --silent vite` previously read `--silent` itself as the
        # tool and missed `vite` entirely — an under-block, the launch allowed on protected main).
        exec_toks = rest[1:]
        exec_toks = exec_toks[_skip_flags(exec_toks):]
        tool = exec_toks[0] if exec_toks else None
        sub = exec_toks[1] if len(exec_toks) > 1 else None
        if tool in _RUNNER_TOOLS and _is_runner_devserver(tool, sub):
            return f"{head} {rest[0]} {tool}" + (f" {sub}" if sub else ""), cwd_override
        # Agree with `_classify_npx_segment`, which covers webpack too (Codex review, PR
        # agent-tools#469): `npm exec -- webpack serve` / `pnpm dlx webpack-dev-server` start
        # the exact same watcher `npx webpack serve` and the direct `webpack serve` form already
        # block — omitting them here let the package-manager `exec`/`dlx` spelling evade the
        # gate on a protected default branch while the `npx` spelling of the same command caught it.
        if tool in _WEBPACK_TOOLS and _is_webpack_devserver(tool, exec_toks[1:]):
            return f"{head} {rest[0]} {_webpack_label(tool)}", cwd_override
        return None
    if rest[:1] and _is_dev_script(rest[0]):
        return f"{head} {rest[0]}", cwd_override
    return None


def _classify_bun_segment(rest: list[str]) -> tuple[str, str | None] | None:
    """`bun run dev|start` / `bun dev|start` — bun's own script-runner shape. `bun x <tool>` is
    bun's own documented alias for `bunx <tool>` — delegated to `_classify_npx_segment` so the
    two spellings agree (the same reasoning `_is_runner_devserver` states for `npx`/`bunx`)."""
    if rest[:1] == ["x"]:
        return _classify_npx_segment("bun x", rest[1:])
    rest, cwd_override = _peel_dir_flag(rest)
    if rest[:1] == ["run"]:
        script_toks = rest[1:]
        # Same fix as `_classify_pm_segment`'s `run` branch (Codex review, PR agent-tools#469):
        # `bun run --silent dev` — bun's own `run` documents `--silent`/`--if-present` too
        # (checked bun 1.2.14 --help) — stopping before consuming the flag reads it as the
        # script-name position and misses `dev`, an under-block on protected main.
        while script_toks[:1] and script_toks[0] in _RUN_FLAGS:
            script_toks = script_toks[1:]
        if script_toks and _is_dev_script(script_toks[0], _BUN_DEV_SCRIPTS):
            return f"bun run {script_toks[0]}", cwd_override
        return None
    if rest[:1] and _is_dev_script(rest[0], _BUN_DEV_SCRIPTS):
        return f"bun {rest[0]}", cwd_override
    return None


def _is_webpack_devserver(tool: str, tail: list[str]) -> bool:
    if tail[:1] and tail[0] in _INFO_FLAGS:
        return False
    if tool == "webpack-dev-server":
        return True
    return tail[:1] == ["serve"]


def _webpack_label(tool: str) -> str:
    return "webpack-dev-server" if tool == "webpack-dev-server" else "webpack serve"


def _classify_npx_segment(head: str, rest: list[str]) -> tuple[str, str | None] | None:
    """`npx`/`bunx [flags] <tool> [subcommand]` — flag VALUES are not distinguished from
    valueless flags (a known, documented scope limit: `npx -p cowsay vite` misreads `cowsay` as
    the tool). Covers the same tool set as a direct-binary invocation (`_RUNNER_TOOLS` and
    webpack) — the two call shapes must agree, see `_is_runner_devserver`."""
    i = _skip_flags(rest)
    if i >= len(rest):
        return None
    tool = rest[i]
    tail = rest[i + 1:]
    sub = tail[0] if tail else None
    if tool in _RUNNER_TOOLS and _is_runner_devserver(tool, sub):
        return f"{head} {tool}" + (f" {sub}" if sub else ""), None
    if tool in _WEBPACK_TOOLS and _is_webpack_devserver(tool, tail):
        return f"{head} {_webpack_label(tool)}", None
    return None


def _classify_runner_segment(head: str, rest: list[str]) -> tuple[str, str | None] | None:
    """A direct runner-tool binary invocation: `vite [flags]`, `next dev`, `astro dev`."""
    sub = rest[0] if rest else None
    if _is_runner_devserver(head, sub):
        return (head if head == "vite" else f"{head} dev"), None
    return None


def _classify_webpack_segment(head: str, rest: list[str]) -> tuple[str, str | None] | None:
    if _is_webpack_devserver(head, rest):
        return _webpack_label(head), None
    return None


def _classify_devserver_segment(toks: list[str]) -> tuple[str, str | None] | None:
    """Return ``(label, cwd_override)`` if the ALREADY-TOKENIZED, already assignment-stripped
    ``toks`` launch a dev server / dev-watch process, else None. ``label`` is a short
    human-readable name (e.g. "npm run dev"); ``cwd_override`` is set only for a
    directory-changing flag (`npm --prefix <dir> run dev`, `pnpm -C <dir> dev`) — a ONE-SHOT
    override for THIS launch, never persisted to the running `effective_cwd` the way a real
    `cd` segment is.

    Takes tokens rather than a raw string so a caller that already shlex-split the segment
    (e.g. to check for a leading `cd`) does not pay for a second parse of the same text —
    `main()` tokenizes each segment exactly once. Token-based classification (not
    regex-on-raw-text) for the same reason `pin_primary_worktree._classify_git_segment` is:
    robust to quoting and extra flags without a fragile regex.

    Scope is intentionally a fixed, well-known list — see the module docstring's "Deliberately
    NOT covered" section for why this doesn't try to be a general "any long-running process"
    detector.
    """
    if not toks:
        return None
    head = toks[0].rsplit("/", 1)[-1]
    rest = toks[1:]

    if head in _PM_NAMES:
        return _classify_pm_segment(head, rest)
    if head == "bun":
        return _classify_bun_segment(rest)
    if head in ("npx", "bunx"):
        return _classify_npx_segment(head, rest)
    if head in _RUNNER_TOOLS:
        return _classify_runner_segment(head, rest)
    if head in _WEBPACK_TOOLS:
        return _classify_webpack_segment(head, rest)
    return None


_TRAILING_BACKSLASH_RUN_RE = re.compile(r"\\+\n")


def _normalize_line_continuations(command: str) -> str:
    """Fold a literal `\\<newline>` shell line continuation into a single space before chain
    splitting — mirrors the exact normalization `agenttools_hatch_escalation` already applies
    for its own inline-hatch parsing (`_split_command_segments`). Without this, a real single
    command such as ``npm run dev \\`` + newline + ``--host 3000`` is split by `_split_chain`
    (which treats a bare newline as a separator) into TWO segments — a trailing-backslash
    segment `shlex` cannot tokenize, and a flags-only segment with no head token — silently
    missing the launch entirely.

    Only an ODD run of backslashes immediately before the newline is a real continuation —
    parity-checked (Codex review, PR agent-tools#469): two backslashes before a newline is one
    escaped, LITERAL backslash followed by a real, unescaped newline in a real shell (each pair
    of `\\\\` collapses to one literal `\\`), so the line does NOT continue and the newline still
    separates two real commands. `echo \\\\` + newline + ``npm run dev`` is `echo \\` (printing a
    literal backslash) as its own command, then `npm run dev` as a SEPARATE, real launch on the
    next line — folding it into one `echo`-headed segment anyway (an even-backslash-blind
    version of this function did) hides that real launch from ever being split out and
    classified. A single backslash (odd run) is the ordinary continuation case and folds as
    before; three backslashes is again odd (one literal pair + one real continuation) and folds;
    the parity check, not a fixed count, is what's correct in general."""

    def _fold(m: re.Match[str]) -> str:
        n_backslashes = len(m.group()) - 1  # exclude the trailing \n itself
        if n_backslashes % 2 == 1:  # odd — real continuation, (n-1)/2 literal backslashes remain
            return "\\" * ((n_backslashes - 1) // 2) + " "
        return "\\" * (n_backslashes // 2) + "\n"  # even — no continuation, newline stays real

    return _TRAILING_BACKSLASH_RUN_RE.sub(_fold, command)


def _resolve_gate(effective_cwd: str, cache: dict[str, str | None]) -> str | None:
    """The branch to block a dev-server launch on for ``effective_cwd`` — i.e. the repo IS
    `worktree_only`-enrolled AND is currently checked out on ITS OWN default branch — or None
    if this directory should not be gated at all (not enrolled, on a feature branch, or
    branch/enrollment undetermined).

    Memoized per ``effective_cwd`` in ``cache`` for the lifetime of one hook invocation: the
    gated command has not run yet, so a given directory's enrollment and checked-out branch
    cannot change mid-scan, which makes memoizing this git-subprocess-heavy resolution safe —
    without it, a chain with N segments targeting the same directory re-runs up to 5 `git`
    spawns per segment instead of once.
    """
    if effective_cwd in cache:
        return cache[effective_cwd]
    result: str | None = None
    if worktree_only_enabled(effective_cwd):
        branch = current_branch(effective_cwd)
        if branch is not None and branch == default_branch(effective_cwd):
            result = branch
    cache[effective_cwd] = result
    return result


def _decide_block(cwd: str, branch: str, label: str, full_command: str) -> int:
    """``label`` is the short human-readable dev-server name (e.g. "npm run dev") shown in the
    block MESSAGE only. ``full_command`` is the ENTIRE original Bash command string, and MUST
    reach the human approver two separate ways — both are load-bearing, not redundant:

    1. As the ``command=`` kwarg, so the documented INLINE hatch form
       (``RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY="…" npm run dev``) can be parsed out of it
       (a short label can never contain that assignment).
    2. Inside the ``context`` dict's own ``"command"`` key — the Telegram question is rendered
       EXCLUSIVELY from ``context`` (see ``agenttools_hatch_escalation._question``); the
       ``command=`` kwarg above is invisible to the human and used only for inline parsing and
       the audit log. Putting just the short ``label`` in ``context`` (a prior version of this
       hook did exactly that) makes the "the human sees the FULL command" justification for
       returning on the FIRST gate-worthy segment (below) FALSE: a chain like `npm run dev; cd
       /other-shared-checkout && vite` would show only `npm run dev` in the question, and one
       approval would silently also allow the unseen `vite` launch against a second shared
       checkout — laundering a real bypass through a legitimate-looking approval prompt.

    Unlike `pin_primary_worktree`'s aggregate per-chain approval budget, this function returns
    on the FIRST gate-worthy segment — an approved hatch for `cd repoA && npm run dev; cd repoB
    && vite` never evaluates repoB as a SEPARATE gate. This is safe only because point 2 above
    makes the ENTIRE original command string (not just the first launch) part of what the human
    is asked to approve.
    """
    message = _block_message(branch, label)
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID,
        {"hook": HOOK_ID, "branch": branch, "launch": label, "command": full_command},
        cwd=cwd,
        command=full_command,
    )
    if hatch.should_stop:
        if hatch.approved:
            note = f"dev-server launch allowed via hatch escalation ({hatch.reason})"
            warn(note)
            emit("allow", note)
            return 0
        warn(f"dev-server launch hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{message}")
        return BLOCK_EXIT_CODE
    emit("block", message)
    return BLOCK_EXIT_CODE


# Real commands rarely chain more than a couple of conditional `cd`s; without a cap, N
# conditional `cd`s to N distinct new targets could branch the candidate set to up to 2**N.
# Once the cap is hit, `_apply_cd_to_candidates` stops adding new NON-GATED branches — see its
# own docstring for why a gated branch is NEVER dropped regardless of the cap.
_MAX_CANDIDATE_CWDS = 8


def _apply_cd_to_candidates(
    candidates: set[str],
    toks: list[str],
    op: str | None,
    following_op: str | None,
    gate_cache: dict[str, str | None],
) -> set[str]:
    """Given the current set of POSSIBLE `effective_cwd` states — see `main()`'s docstring for
    why there can be more than one — apply one `cd` segment's tokens and return the updated set.
    ``op`` is the operator preceding this `cd` (``None``/``;``/newline/``&`` = unconditional,
    ``&&``/``||`` = conditional); ``following_op`` is the operator that terminates it (``|``/``&``
    = isolated in a subshell whose directory change never reaches the parent shell at all).

    - Isolated (Codex review, PR agent-tools#469): a `cd` piped into/out of another command
      (`cd X | cat`) or backgrounded (`cd X &`) runs in a subshell bash forks for that purpose —
      its effect is discarded when the subshell exits and NEVER reaches the parent shell,
      regardless of what precedes it. The candidate set is returned UNCHANGED.
    - Unconditional, not isolated: the `cd` DEFINITELY runs — each candidate is DETERMINISTICALLY
      replaced by its own resolved target (or left as-is if the target doesn't exist — a failed
      `cd` leaves the shell where it was, see `_resolve_cd_target`'s own docstring).
    - Conditional, not isolated (Codex review, PR agent-tools#469): the `cd` MAY OR MAY NOT run
      — this hook cannot evaluate the preceding command's exit code — so each candidate BRANCHES
      into both possible successor states (itself, unchanged: the `cd` didn't run; and its
      resolved target: the `cd` did run). A single always-safe direction (e.g. "assume it never
      ran") is NOT sound here: `false && cd /feature; npm run dev` from a protected checkout
      needs "didn't run" to correctly BLOCK (the launch stays in the protected checkout), but
      `true && cd /protected-main; npm run dev` from a feature worktree needs "did run" to
      correctly BLOCK (the launch reaches the protected checkout) — the same fixed assumption
      gets one of the two wrong. Tracking both keeps both correct.

    Cap handling (Codex review, PR agent-tools#469, round 3): the ORIGINAL cap simply stopped
    adding new branch targets once ``_MAX_CANDIDATE_CWDS`` was reached — silently DROPPING any
    further conditional-`cd` target, including one that is itself gated. From a feature
    worktree, a chain with 7 harmless conditional branches ending in `true && cd
    /protected-main; npm run dev` would have its gated 8th target dropped purely by ARRIVAL
    ORDER, wrongly allowing a launch that really does reach the protected checkout. A dropped
    candidate must never be one that would have caused a BLOCK — so once the cap is reached,
    each new target is checked against the gate BEFORE being considered for dropping: a target
    that resolves to a gate is added REGARDLESS of the cap (a real, if rare, potential to exceed
    ``_MAX_CANDIDATE_CWDS`` by a bounded amount — one over-cap slot per already-processed `cd`
    segment, still finite and small in practice); only a target that is CONFIRMED SAFE
    (`_resolve_gate` returns ``None``) is ever dropped for being over the cap."""
    if following_op in _ISOLATING_OPS or op == "|":
        return candidates
    if op in _CONDITIONAL_OPS:
        branched = set(candidates)
        for c in candidates:
            target = _resolve_cd_target(toks, c)
            if target is None or target in branched:
                continue
            if len(branched) < _MAX_CANDIDATE_CWDS or _resolve_gate(target, gate_cache) is not None:
                branched.add(target)
        return branched
    return {_resolve_cd_target(toks, c) or c for c in candidates}


def _gate_for_any_candidate(
    candidates: set[str], cwd_override: str | None, cache: dict[str, str | None],
) -> tuple[str, str] | None:
    """``(check_cwd, branch)`` for the first of the possibly-several candidate `effective_cwd`
    states (see `main()`'s docstring) that resolves to a gate, or ``None`` if none do — a
    dev-server segment is blocked if ANY possible state it might actually run in is gated.
    ``cwd_override`` (a directory flag like `--prefix`) is resolved against EACH candidate
    rather than replacing the set outright — it scopes only THIS launch, same as the single-
    candidate case, just applied per-branch."""
    for c in candidates:
        check_cwd = _resolve_relative_path(cwd_override, c) if cwd_override is not None else c
        branch = _resolve_gate(check_cwd, cache)
        if branch is not None:
            return check_cwd, branch
    return None


def main() -> int:
    """Tracks a SET of possible `effective_cwd` states, not a single one, because a `cd` gated
    behind `&&`/`||` may or may not have actually executed in the real shell — this hook has no
    way to evaluate the preceding exit code (see `_apply_cd_to_candidates`). A later dev-server
    segment is blocked if ANY candidate state is gated (`_gate_for_any_candidate`) — the only
    sound choice when the real state is genuinely ambiguous, consistent with this hook's
    over-block-not-under-block philosophy throughout."""
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    _CURRENT_EVENT["event"] = event
    raw_args = event.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0

    segments = _split_chain(_normalize_line_continuations(command))
    candidate_cwds: set[str] = {cwd}
    gate_cache: dict[str, str | None] = {}
    for idx, (op, segment) in enumerate(segments):
        try:
            toks = _strip_leading_assignments(shlex.split(segment))
        except ValueError:
            toks = []

        if toks and toks[0] == "cd":
            following_op = segments[idx + 1][0] if idx + 1 < len(segments) else None
            candidate_cwds = _apply_cd_to_candidates(
                candidate_cwds, toks, op, following_op, gate_cache,
            )
            continue

        # `cd` is never a wrapper head, so peeling here is a no-op for the `cd` branch above —
        # this only affects the classify path below.
        toks = _peel_command_wrappers(toks)
        classified = _classify_devserver_segment(toks)
        if classified is None:
            continue
        label, cwd_override = classified

        gate = _gate_for_any_candidate(candidate_cwds, cwd_override, gate_cache)
        if gate is None:
            continue  # not enrolled, on a feature branch, or undetermined → fail-open
        check_cwd, branch = gate
        return _decide_block(check_cwd, branch, label, command)

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
