#!/usr/bin/env python3
"""agents-hooks/v1 pre-write + pre-bash hook — keep the orchestrator thin.

The orchestrator plans, dispatches, and verifies — it does NOT implement inline. When the
MAIN thread is about to do implementation-shaped work itself (a non-docs code Edit/Write, or
a multi-step implementation Bash) this gate nudges it to delegate to a subagent or a
Workflow. It enforces `delegate-work-to-subagents`.

ONE script binds TWO points via two descriptors; it branches on ``event["point"]``:
  - pre-write : a CODE Edit/Write (non-docs) by the main thread → warn-then-block
  - pre-bash  : a clearly multi-step / implementation-shaped Bash by the main thread →
                warn-then-block. Read-only inspection AND sanctioned ORCHESTRATION are NEVER
                blocked — a single one-liner (git status, ls, cat, grep, find) OR a chain of any
                length whose every segment is read-only OR an orchestration command
                (gh pr list/view/checks, gh run, gh api, `gh ship`, tg, review, git worktree
                list). This is the flapping fix (Alex tg#5743 / agent-tools#23): `gh pr list &&
                gh pr view` and `gh ship 5 && tg done` used to trip the ">=2 operators" rule.
                Commits/pushes and test runs (git commit/push, pytest, npm/bun/cargo test) ARE
                treated as implementation and warn-then-block.

TIERED (warn → block): the FIRST offense in the TTL window WARNs (allow + message); a REPEAT
in the window BLOCKs. The tier is tracked by a marker file keyed by a hash of cwd. This gives
the doctrine's "WARN then BLOCK" instead of a hard wall on the first inline edit.

Subagent-exempt: a dispatched subagent (``agent_id`` present) does the actual work, so it is
always allowed — this gate governs the orchestrator only.

Per-repo opt-out (Alex tg#5743): default ON; a repo that legitimately works inline on main
(e.g. 3d-cli) sets `agent_hooks.orchestrator_only: false` in its rig.yaml, or exports
RIG_ORCHESTRATOR_ONLY=0. Default ON means an un-enrolled repo keeps the current always-on
behavior (no regression). Mirrors the opt-IN worktree-only-writes guard's per-repo knob.

Escape hatch (controllable — mirrors block-raw-pr-merge):
  - env  ALLOW_ORCHESTRATOR_WORK=1            — disable the guard for this session (both points)
  - env  ALLOW_ORCHESTRATOR_WORK_REASON=...   — REQUIRED with the override; logged
  - inline (PRE-BASH ONLY)  `# orchestrator-ok: <reason>`  — self-documenting per-command.
    A pre-write carries no shell string, so the inline sentinel can only fire for a bash
    command; for a write use the ENV hatch.
  A reasonless override still blocks.

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command (bash) or args.file_path/path (write); event.point
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "ORCH_THIN_MARKER_DIR", "~/.cache/agent-tools/orchestrator-thin")))
# How long a first-offense WARN suppresses the next WARN before a REPEAT becomes a BLOCK.
TTL_S = int(os.environ.get("ORCH_THIN_TTL_S", "900"))

# A write to one of these is documentation, never implementation → always allow.
DOCS_PATH = re.compile(r"\.(?:md|mdx|txt|rst)$", re.IGNORECASE)
DOCS_DIR = re.compile(r"(?:^|/)docs/", re.IGNORECASE)

# Inspection / read-only one-liners that the orchestrator legitimately runs itself.
READ_ONLY_BASH = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch)\b|ls\b|cat\b|less\b|head\b|tail\b|"
    r"grep\b|rg\b|find\b|pwd\b|echo\b|which\b|env\b|wc\b|stat\b|tree\b|file\b|jq\b|"
    # read-only system-info + text-filter verification tools (SYNC with fix/159 READ_ONLY_BASH):
    # so a verify pipe `df -h | grep /dev | head` / `gh pr view | jq | head` is not blocked by the
    # >=3-segment rule (coordinator: verification is the orchestrator's own altitude).
    r"sort\b|uniq\b|cut\b|column\b|df\b|du\b|lsblk\b|free\b|ps\b|uname\b|"
    r"uptime\b|whoami\b|hostname\b|nproc\b|date\b)"
)
# Sanctioned ORCHESTRATION commands the main thread legitimately runs ITSELF — coordination,
# reporting, CI/PR inspection, and the sanctioned release path `gh ship` (agent-tools#23). These
# are NOT implementation, so a chain composed entirely of them (or read-only inspection) must
# never flap into a BLOCK. This is the flapping fix (Alex tg#5743): before it, `gh pr list && gh
# pr view` or `gh ship 5 && tg "shipped"` tripped the ">=2 operators" rule because gh/tg were in
# no allow-list. Head-anchored (`^\s*`) exactly like READ_ONLY_BASH so a build/edit head with
# `gh`/`tg` as an ARGUMENT is not waved through (the same anchor invariant, #5/#80).
ORCH_ALLOW = re.compile(
    r"^\s*(?:"
    r"gh\s+ship\b"                                        # the sanctioned release (#23)
    r"|gh\s+pr\s+(?:list|view|checks|status|diff)\b"      # PR inspection (NOT checkout — it mutates
                                                          # the local worktree/branch)
    r"|gh\s+run\s+(?:list|view)\b"                        # CI status (NOT `watch` — it is a
                                                          # long-running block; the orchestrator
                                                          # should delegate a watch, and it is not
                                                          # a quick inspection that this allow-list
                                                          # is for. no-long-inline-process follow-up.)
    r"|tg\b"                                              # Telegram status reports
    r"|review\b"                                          # multi-model review CLI (read-only)
    r"|git\s+worktree\s+list\b"                           # worktree inspection
    r")"
)
_DEV_ALLOWED_SUBCOMMANDS = frozenset({"start", "run", "list", "status", "logs", "stop", "env", "help"})
_DEV_ALLOWED_E2E_SUBCOMMANDS = frozenset({"run", "status", "logs", "stop", "help"})
# Intentionally narrower than `dev run <script>` itself: only common test/e2e script names are
# orchestration; other script names remain implementation-shaped so `dev run deploy` is not a bypass.
_DEV_ALLOWED_RUN_SCRIPTS = frozenset({"test", "tests", "e2e", "smoke"})
# `gh api` is orchestration ONLY when GET-shaped. Rather than a fragile inline lookahead, detect
# it with two token-aware regexes: the HEAD (`gh api`) and any MUTATION signal — a method flag
# (`-X`/`--method`), a POST field (`-f`/`-F`/`--field`/`--raw-field`/`--input`, incl. ATTACHED
# short forms like `-fkey=val` / `-XPOST`), or `graphql` (mutation-capable). A `gh api` segment
# with NO mutation signal is a read (allowed); one WITH a signal is implementation-shaped and
# warn-then-blocks EVEN as a single command (a mutation is a subagent's job — codex P1).
_GH_API_HEAD = re.compile(r"^\s*gh\s+api\b")
# gh api mutation-vs-GET is decided by shlex-parsing the args (`_gh_api_is_mutation`), not a raw
# regex — so `--method=GET`, `-XGET`, `-fkey=val`, and `--method GET --method POST` (last wins) are
# all read correctly.
#
# WRAPPER STRIPPING (`_strip_wrappers`): a leading `VAR=val` assignment or a passthrough command
# wrapper (env / time / timeout / nice / nohup / stdbuf / ionice / setsid, basename-matched so
# `/usr/bin/env` counts) hides the REAL command head — `timeout 60 pytest` (the mandated timeout
# wrapper!), `env git commit`, `CI=1 pytest`, `time git push`, `nice -n10 npm build` — so they are
# stripped before classification. This is a BEST-EFFORT discipline heuristic, NOT a security
# boundary: a determined obfuscation (`env -S 'cmd'`, `sh -c 'cmd'`) can still evade it. The
# sanctioned way to do deliberate inline work is the escape hatch, never evasion. A BARE `env`
# (print environment) is preserved as read-only.
_ASSIGN_RE = re.compile(r"^\w+=")
# Per-wrapper option flags that consume a following OPERAND. Wrapper-SPECIFIC on purpose: the same
# flag means different things per tool (env `-i` = ignore-env, NO operand; stdbuf `-i` = an operand),
# so a shared set would make `env -i git commit` wrongly swallow `git` and bypass the deny (codex).
_WRAPPER_OPT_ARGS = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    # `gtimeout` is GNU coreutils `timeout` on macOS (Homebrew) — enforce-timeout-on-bash accepts it
    # as a valid timeout wrapper, so this hook must strip it too, else `gtimeout 60 git commit`
    # slips past implementation detection (codex review).
    "gtimeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "ionice": frozenset({"-c", "-n"}),
    "time": frozenset(),
    "nohup": frozenset(),
    "setsid": frozenset(),
}
_WRAPPERS = frozenset(_WRAPPER_OPT_ARGS)
# A `uv run … pytest`/`tox` test wrapper is detected by shlex-parsing (`_is_uv_test`) so the test
# token must be the COMMAND uv runs, not an argument — `uv run rg pytest docs` (a search) is NOT a
# test run. Chain-splitting is quote-aware (`_split_chain`) so a `|` inside a quoted jq program is
# not a split point.
# A command starts at the line start or right after a &&/;/|/( separator. Anchoring the
# build-tool tokens here means the runner must be the COMMAND, not an argument/needle —
# the same anchoring the no-long-inline-process sibling uses (#5).
_CMD_START = r"(?:^|&&|\|\||;|\||\()\s*"
# A heredoc, or an obvious build/edit invocation, marks implementation-shaped shell.
HEREDOC = re.compile(r"<<-?\s*['\"]?\w+")
# In-place edit / build invocations. A bare `>`/`>>` redirect is NOT here: a redirect alone
# ("python foo.py > out.log") is not implementation — the in-place editors (sed -i, tee) and
# the build tools are the real signals (B7). The build-tool RUNNERS are anchored at a command
# head so a substring needle in an inspection pipe (`cat notes.md | grep npm`, `git log | rg
# yarn`, `find . -name cargo.toml | wc -l`) is NOT mis-read as implementation (#5). The
# in-place editors (sed -i, tee) keep a bare `\b` anchor: they are a content signal wherever
# they appear (e.g. `git status && sed -i ...`).
# `git commit`/`git push` and the test runners (pytest, go test) are ADDED here (Alex tg#5743):
# the orchestrator must not commit/push or run test suites inline — that is a subagent's job.
# They are anchored at a command head via _CMD_START (npm/bun/cargo already cover `npm test`,
# `bun test`, `cargo test`). `git commit`/`git push` do NOT match READ_ONLY_BASH, so a chain that
# starts read-only (`git status && git commit`) is still judged on its full content and blocks.
BUILD_EDIT = re.compile(
    r"\b(?:sed\s+-i|tee)\b"
    r"|" + _CMD_START + r"(?:npm|pnpm|yarn|bun|cargo|go\s+build|go\s+test|make|"
    r"python\s+setup|pip\s+install|pytest|python\s+-m\s+(?:pytest|unittest)|tox|"
    r"git\s+(?:commit|push))\b"
)
# `uv run … pytest` (the repo's own documented test command) is handled separately by _UV_TEST
# below — targeted so a `uv run` of a read-only tool is not swept in. Other runner wrappers with a
# non-build head are still not caught; the orchestrator is meant to DELEGATE the suite to a subagent
# (which is exempt) anyway, and the tiered warn-then-block + escape hatch cover the residue.
INLINE_SENTINEL = re.compile(r"#\s*orchestrator-ok:\s*(\S.*)")

# ── companion-safety vetoes for the sanctioned-orchestration allow-list ──────────────────────
# SYNC with the sibling `gh ship` release carve-out (agent-tools#159 / fix/159-gh-ship-carveout):
# both hooks judge per segment head, so a benign head must not launder a mutation hidden elsewhere
# in the line. The allow-list (`_seg_is_allowed`) is HEAD-anchored, so a command/process
# substitution (`gh ship $(sed -i …)`, `cat <(git push) && tg done`) or a bare background `&`
# (`tg done & git push` — NOT a chain split) would smuggle a mutation past it. These veto the
# free pass wholesale (scanned quote-blanked so a metachar INSIDE a quoted arg does not trip them).
SUBSTITUTION = re.compile(r"\$\(|`|[<>]\(")
# Bare `&` backgrounding. `&&`, `2>&1`, `&>`, `>&` are excluded (not a control `&`).
BG_AMP = re.compile(r"(?<![&>])&(?![&>])")
# `cd` changes no repo state — the one extra companion a sanctioned chain may carry besides
# read-only inspection (`cd <repo> && gh ship <PR> | tail`). Argv-boundary anchored (`cd(?=\s|$)`),
# NOT `\b`, so `cd-clean` / `cd/foo` are not mistaken for `cd`.
CD_HEAD = re.compile(r"^\s*cd(?=\s|$)")
# find's mutating primaries — the delete/exec family AND the file-WRITE primaries
# (`-fprint`/`-fprintf`/`-fprint0`/`-fls`, which write an arbitrary path) — are a content signal
# anywhere in a segment: a read-only `find` head must not launder them into the allow-list.
FIND_MUTATION = re.compile(r"\s-(?:delete|exec|execdir|ok|okdir|fprintf|fprint0|fprint|fls)\b")

MESSAGE = (
    "Delegate to a subagent: the orchestrator plans, dispatches, and verifies — it does not "
    "implement inline. Launch an Agent (run_in_background: true) or a Workflow to do this "
    "Edit/Write/Bash. (delegate-work-to-subagents, enforced.)"
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"orchestrator-stays-thin: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present).

    TRUST BOUNDARY — this gate uses agent_id to RELAX (a subagent is exempt from the
    thin-orchestrator block), so the read surface must be exactly the one the bridge sanitizes,
    and no wider. We read ONLY `args.agent_id`: lib/cc_hook_bridge enforces T2 precedence before
    the event reaches us — it OVERWRITES args.agent_id from CC's authoritative top-level field
    when CC supplies it and DROPS any model/tool_input-supplied copy when CC does not. So the only
    way a truthy `args.agent_id` survives is if CC itself dispatched a subagent; a main-thread
    agent cannot forge it.

    We deliberately DO NOT fall back to a top-level `event.get("agent_id")`: the bridge NEVER
    writes a top-level `agent_id` (its v1 event has no such key), so that fallback was a DEAD path
    that was nonetheless a TRUSTED, unsanitized relax-surface — if any non-bridge producer ever set
    a top-level agent_id from a model-influenced field, the orchestrator would self-exempt. This
    narrows the read to the sanitized `args.agent_id` only, matching the sibling skills-read-gate
    (agent-tools#115, bringing the two gates into line per #112/#116). An empty/whitespace value is
    not a subagent."""
    args = event.get("args") or {}
    aid = args.get("agent_id")
    return bool(aid and str(aid).strip())


def _override_reason(command: str, point: str) -> str | None:
    """The override reason if a valid escape hatch is present, else None.

    Honored ONLY with a reason:
      - env ALLOW_ORCHESTRATOR_WORK=1 + ALLOW_ORCHESTRATOR_WORK_REASON (both points), OR
      - an inline ``# orchestrator-ok: <reason>`` — PRE-BASH ONLY. A pre-write carries no shell
        string, so the inline sentinel genuinely cannot fire for a write; only the ENV hatch
        applies there (B4). A reasonless override is ignored.
    """
    if os.environ.get("ALLOW_ORCHESTRATOR_WORK") == "1":
        reason = (os.environ.get("ALLOW_ORCHESTRATOR_WORK_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    if point == "pre-bash":
        m = INLINE_SENTINEL.search(command)
        if m:
            return f"inline override: {m.group(1).strip()}"
    return None


def _marker(event: dict) -> Path:
    # Key by (cwd, point) so a pre-write WARN does not prime a pre-bash BLOCK (or vice versa):
    # each point tiers independently (B5).
    cwd = str(event.get("cwd") or "default")
    point = str(event.get("point") or "")
    sid = hashlib.sha256(f"{cwd}\0{point}".encode()).hexdigest()[:16]
    return MARKER_DIR / f"{sid}.warned"


def _is_repeat(event: dict) -> bool:
    """True if a WARN already fired in this cwd within the TTL window (→ now BLOCK)."""
    m = _marker(event)
    try:
        if m.exists() and (time.time() - m.stat().st_mtime) <= TTL_S:
            return True
    except OSError:
        return False
    # First offense → record the warn marker so the next one in the window blocks.
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        m.write_text(str(time.time()))
    except OSError as exc:
        warn(f"could not write warn marker {m}: {exc} — staying in WARN tier")
    return False


def _is_code_write(args: dict) -> bool:
    # NotebookEdit carries `notebook_path`; the bridge also aliases it onto file_path/path, but
    # read it directly too so a `.ipynb` edit is judged even if only notebook_path is set (B3).
    path = args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
    if not isinstance(path, str) or not path.strip():
        return False  # no path to judge → don't claim it's a code write
    if DOCS_PATH.search(path) or DOCS_DIR.search(path):
        return False  # docs are not implementation
    return True


def _strip_wrappers(segment: str) -> str:
    """Drop leading ``VAR=val`` assignments and passthrough command wrappers (env/time/timeout/…)
    so the REAL command head gets classified (codex). shlex-tokenized with basename matching, so
    `/usr/bin/env git commit` and `timeout 60 pytest` are seen as `git commit` / `pytest`.

    Best-effort: if the strip leaves nothing (a bare `env`) the original is kept (read-only), and a
    determined `env -S 'cmd'` / `sh -c 'cmd'` obfuscation is out of scope (see the module note).
    """
    try:
        toks = shlex.split(segment)
    except ValueError:
        return segment  # unbalanced quotes → don't transform
    i, changed = 0, False
    while i < len(toks):
        if _ASSIGN_RE.match(toks[i]):  # VAR=val
            i += 1
            changed = True
            continue
        base = toks[i].rsplit("/", 1)[-1]  # /usr/bin/env → env
        if base not in _WRAPPERS:
            break
        changed = True
        is_timeout = base in ("timeout", "gtimeout")
        opt_args = _WRAPPER_OPT_ARGS[base]  # operand-taking flags for THIS wrapper only
        i += 1
        while i < len(toks) and toks[i].startswith("-"):
            opt = toks[i]
            i += 1
            if opt in opt_args and i < len(toks):
                i += 1  # this option consumes an operand
        while i < len(toks) and _ASSIGN_RE.match(toks[i]):  # env VAR=val …
            i += 1
        if is_timeout and i < len(toks):
            i += 1  # `timeout`'s first positional is the DURATION; the command follows
    if not changed:
        return segment
    rest = toks[i:]
    return " ".join(shlex.quote(t) for t in rest) if rest else segment


def _split_chain(command: str) -> list[str]:
    """Split a command into segments on shell operators (``&&`` ``||`` ``;`` ``|`` newline AND a
    bare control ``&``) that lie OUTSIDE quotes. Quote-aware so a ``|`` inside a quoted jq program
    (``jq '.a | .b'``) is NOT a split point — that mis-split used to flap a read chain (codex).

    A bare ``&`` (backgrounding) IS a real segment separator, so `tg done & git commit` splits into
    two segments and the smuggled `git commit` is judged on its own — without this it was one
    segment with a benign `tg` head and slipped past the impl scan (codex review). A redirect ``&``
    (`2>&1`, `&>`, `>&`) and the already-handled `&&` are NOT splits (BG_AMP semantics)."""
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
            segs.append("".join(buf))  # bare control `&` — a background separator, not a redirect
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


def _norm_segments(command: str) -> list[str]:
    """Chain-split (quote-aware) then strip wrappers from each segment (the unit of classification)."""
    return [_strip_wrappers(s) for s in _split_chain(command)]


def _blank_single_quoted(command: str) -> str:
    """Blank the CONTENT of SINGLE-quoted spans only (keeping the quote chars).

    Feeds the substitution scan (`_substitution_inners`), so the blanking must follow shell quote
    SEMANTICS (#164 review P2): inside single quotes everything is literal — a `tg 'saw $(x)'` is
    text, blank it. Inside DOUBLE quotes `$(…)` and backticks still EXECUTE, so double-quoted
    content is kept INTACT for the scan — `gh ship "$(gh api … -X POST)"` must have its inner
    command extracted and judged, not erased. (An earlier version blanked both quote kinds, which
    erased exactly the common quoted-mutation form.) Conservative side effect: a literal `<(…)`
    inside double quotes (where process substitution does NOT execute) is still extracted and may
    over-flag — the safe direction for a gate. Best-effort, matching `_split_chain`:
    backslash-escapes are out of scope (discipline heuristic, not a security boundary).
    SYNC agent-tools#159/#162."""
    out: list[str] = []
    quote: str | None = None
    for c in command:
        if quote is not None:
            if c == quote:
                quote = None
                out.append(c)
            else:
                out.append(" " if quote == "'" else c)
        elif c in ("'", '"'):
            quote = c
            out.append(c)
        else:
            out.append(c)
    return "".join(out)


def _seg_is_allowed(segment: str) -> bool:
    """A single (env-stripped) segment head is read-only inspection OR sanctioned orchestration.

    Head-anchored, so a build/edit token appearing only as an ARGUMENT/needle (`cat tee.log`,
    `git log | rg gh`) stays allowed on its real head, not the needle (the anchor invariant, #5/#80).
    `gh api` counts ONLY when GET-shaped (no mutation signal).

    A benign head does NOT launder a mutation the head-anchor cannot see: find's mutating primaries
    (delete/exec/file-write) and a `git branch` WITH an argument (its `-D`/create forms) are a read
    head that carries a write, so they forfeit the allow-list (agent-tools#159). `cd` (no repo
    state) is an allowed companion so `cd <repo> && gh ship … | tail` stays a release chain.
    """
    if FIND_MUTATION.search(segment):
        return False
    if _git_subcommand(segment) == "branch" and _git_branch_has_arg(segment):
        return False
    if (
        READ_ONLY_BASH.search(segment)
        or ORCH_ALLOW.search(segment)
        or _dev_segment_is_allowed(segment)
        or CD_HEAD.match(segment)
    ):
        return True
    if _git_subcommand(segment) in _GIT_READ_SUBS:  # `git -C d status`, `/usr/bin/git log`, …
        return True
    return bool(_GH_API_HEAD.match(segment)) and not _gh_api_is_mutation(segment)


def _gh_api_is_mutation(segment: str) -> bool:
    """Decide if a `gh api` segment MUTATES by shlex-parsing its flags and computing the EFFECTIVE
    method — robust to `--method=GET` / `-XGET` / `-fkey=val` / a later `--method POST` overriding an
    earlier GET. A POST field (`-f`/`-F`/`--field`/`--raw-field`/`--input`) with no method implies a
    write; an explicit non-GET method (or `graphql` with fields) mutates; a plain read does not."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return True  # unbalanced quotes → don't whitelist (conservative)
    method: str | None = None
    has_field = False
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-X", "--method"):
            if i + 1 < len(toks):
                method = toks[i + 1].upper()
            i += 2
            continue
        if t.startswith("--method="):
            method = t.split("=", 1)[1].upper()
        elif t.startswith("-X") and len(t) > 2:  # attached, e.g. -XPOST
            method = t[2:].upper()
        elif t in ("-f", "-F", "--field", "--raw-field", "--input"):
            has_field = True
            i += 2  # skip the flag's value operand
            continue
        elif t.startswith(("-f", "-F")) and len(t) > 2:  # attached, e.g. -fkey=val
            has_field = True
        elif t.startswith(("--field=", "--raw-field=", "--input=")):
            has_field = True
        i += 1
    if method is not None:
        return method != "GET"  # explicit method: GET is a read, anything else mutates
    return has_field  # no method + POST fields → an implicit write


def _seg_is_impl_signal(segment: str) -> bool:
    """A single (env-stripped) segment is implementation on its own: a build/edit head (sed -i, tee,
    npm/…), a `git commit`/`push` or a `pytest`/`python -m pytest` test (all spellings), a
    `uv run … pytest` wrapper, OR a `gh api` MUTATION. Caught even unchained (codex P1)."""
    if (
        BUILD_EDIT.search(segment)
        or _is_uv_test(segment)
        or _is_git_or_python_impl(segment)
        or _dev_segment_is_unknown(segment)
    ):
        return True
    return bool(_GH_API_HEAD.match(segment)) and _gh_api_is_mutation(segment)


def _dev_tokens(segment: str) -> list[str] | None:
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    if not toks or toks[0].rsplit("/", 1)[-1] != "dev":
        return None
    return toks


def _dev_segment_is_allowed(segment: str) -> bool:
    toks = _dev_tokens(segment)
    if not toks:
        return False
    if len(toks) == 1 or toks[1] in ("-h", "--help"):
        return True
    if toks[1] == "run":
        return len(toks) >= 3 and (toks[2] in _DEV_ALLOWED_RUN_SCRIPTS or toks[2] in ("-h", "--help"))
    if toks[1] == "e2e":
        return len(toks) == 2 or toks[2] in _DEV_ALLOWED_E2E_SUBCOMMANDS or toks[2] in ("-h", "--help")
    return toks[1] in _DEV_ALLOWED_SUBCOMMANDS


def _dev_segment_is_unknown(segment: str) -> bool:
    toks = _dev_tokens(segment)
    return toks is not None and not _dev_segment_is_allowed(segment)


# git global options that take a SEPARATE operand (so `git -C /repo commit` finds `commit`, not `/repo`).
_GIT_GLOBAL_OPT_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)
_GIT_READ_SUBS = frozenset({"status", "log", "diff", "show", "branch"})


def _git_subcommand(segment: str) -> str | None:
    """The git subcommand after skipping global options, or None if the segment isn't `git`.

    shlex + basename, so it sees the subcommand across spellings the head regexes miss:
    path-qualified (`/usr/bin/git`) and global options before the subcommand (`git -C d status`,
    `git -c k=v commit`, `git --git-dir=.git commit`) (codex round 8). Used by BOTH the read
    carve-out and the mutation deny so the two never disagree on what a git command is.
    """
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    if not toks or toks[0].rsplit("/", 1)[-1] != "git":
        return None
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):  # attached --opt=val has no operand
            i += 1
    return toks[i] if i < len(toks) else None


def _git_branch_has_arg(segment: str) -> bool:
    """True when a `git branch` segment carries ANY argument. `git branch` bare is a read-only
    LIST; `git branch <arg>` covers the mutating forms (`-D`/`-d`/`-m`/`-M`/create) AND read flags
    like `-a`/`-v` — conservatively ALL forfeit the allow-list (a lone read `git branch -a` still
    passes via the `< 3 segments` fallback). shlex + global-option aware like `_git_subcommand`, so
    the `-C` form (`git -C /repo branch -D`) is covered too. agent-tools#159."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return True  # unparseable → do not grant the allow-list
    if not toks or toks[0].rsplit("/", 1)[-1] != "git":
        return False
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        i += 1
        if opt in _GIT_GLOBAL_OPT_ARG and i < len(toks):
            i += 1
    if i >= len(toks) or toks[i] != "branch":
        return False
    return i + 1 < len(toks)  # any token after `branch`


def _is_git_or_python_impl(segment: str) -> bool:
    """shlex-based detection of `git commit`/`git push` and `pytest`/`python -m pytest|unittest`
    across spellings the head regex misses: path-qualified (`/usr/bin/git`, `/usr/bin/python3`),
    `python3`/`pythonX.Y`, and git GLOBAL options before the subcommand (codex round 8)."""
    if _git_subcommand(segment) in ("commit", "push"):
        return True
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    if not toks:
        return False
    cmd = toks[0].rsplit("/", 1)[-1]  # basename
    if re.fullmatch(r"python3?(?:\.\d+)?", cmd):
        return toks[1:3] in (["-m", "pytest"], ["-m", "unittest"])
    return cmd in ("pytest", "tox")


def _is_uv_test(segment: str) -> bool:
    """True when a segment is a `uv run … pytest`/`tox`/`python -m pytest|unittest` TEST run.

    shlex-parsed so the test token must be the COMMAND uv runs (after `uv run` + its options), not
    an argument — `uv run rg pytest docs` (searching for "pytest") is NOT a test run (codex)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    if len(toks) < 3 or toks[0] != "uv" or toks[1] != "run":
        return False
    i = 2
    _UV_OPTS_WITH_ARG = (
        "--with", "--with-editable", "--python", "-p", "--project", "--directory", "--env-file",
    )
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        i += 1
        if opt in _UV_OPTS_WITH_ARG and i < len(toks):
            i += 1  # skip the option's operand
    if i >= len(toks):
        return False
    cmd = toks[i]
    if cmd in ("pytest", "tox"):
        return True
    return cmd == "python" and toks[i + 1:i + 3] in (["-m", "pytest"], ["-m", "unittest"])


def _is_all_read_only(command: str) -> bool:
    """True when EVERY (env-stripped) chain segment head is a read-only inspection command.

    A fully read-only pipe (`find ... | grep ... | head`) is the orchestrator's bread-and-butter
    inspection and must never be blocked, no matter how many segments it has (#80).
    """
    if HEREDOC.search(command):
        return False
    segs = _norm_segments(command)
    return bool(segs) and all(READ_ONLY_BASH.search(s) for s in segs)


def _is_all_inline_allowed(command: str) -> bool:
    """True when EVERY (env-stripped) segment is read-only inspection OR sanctioned orchestration.

    The superset used to decide "not implementation": a chain of only `gh pr list`/`gh ship`/`tg`/…
    (or read-only inspection) is orchestration, never blocked. The flapping fix + the `gh ship`
    whitelist (Alex tg#5743 / agent-tools#23).

    Rejects a chain whose substitution smuggles a MUTATION (`gh ship $(gh api -X POST …)`) — the
    head-anchored allow check cannot see inside `$(…)`, so this is the only place to catch it before
    the fast path returns. A BENIGN substitution (`cat $(find …) | grep | head`) is NOT rejected, so
    the #80 read-only-pipe-of-any-length invariant holds (agent-tools#159, Opus review). A bare `&`
    is handled by `_split_chain` splitting it, so its segments are judged individually.
    """
    if HEREDOC.search(command):
        return False
    if _has_mutating_substitution(command):
        return False
    segs = _norm_segments(command)
    return bool(segs) and all(_seg_is_allowed(s) for s in segs)


# A single-level substitution — `$(…)`, backticks, `<(…)`/`>(…)` — capturing its INNER command.
# One level (no nested parens) on purpose: a discipline heuristic, not a shell parser.
_SUBST_INNER = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|[<>]\(([^()]*)\)")


def _substitution_inners(command: str) -> list[str]:
    """Inner commands of the LIVE substitutions in ``command``. Scanned on a copy with only
    SINGLE-quoted spans blanked: single-quoted text is literal (never a substitution), but a
    substitution inside DOUBLE quotes still EXECUTES (`gh ship "$(gh api … -X POST)"`) and must be
    extracted and judged (#164 review P2). Lets a mutation smuggled in a substitution head-position
    (`gh ship $(gh api -X POST …)`) still be judged: the outer segment head is `gh ship`, so only
    scanning the inner catches the `gh api` mutation (codex review)."""
    inners: list[str] = []
    for m in _SUBST_INNER.finditer(_blank_single_quoted(command)):
        inner = next((g for g in m.groups() if g), "")
        if inner.strip():
            inners.append(inner)
    return inners


def _has_mutating_substitution(command: str) -> bool:
    """True when an UNQUOTED substitution's own command is implementation — a mutation smuggled
    where no segment head can see it (`gh ship $(gh api -X POST …)`, `cat $(git commit)`). A BENIGN
    substitution (`cat $(find …)`) is not, so the #80 read-only-pipe invariant holds (Opus review)."""
    return any(_seg_is_impl_signal(s)
               for inner in _substitution_inners(command)
               for s in _norm_segments(inner))


def _is_implementation_bash(command: str) -> bool:
    if not command.strip():
        return False
    # A fully read-only / sanctioned-orchestration chain of ANY length is inspection, never
    # implementation (#80 + the tg#5743 flapping fix). Checked FIRST so a build-token that is only
    # an ARGUMENT of a read-only head (`cat tee.log`) is not mis-flagged by the UNANCHORED BUILD_EDIT
    # needle below — the head-anchored allow-list shields it (#5/#80). `_is_all_inline_allowed`
    # itself rejects a chain whose substitution smuggles a mutation, so the fast path is safe.
    if _is_all_inline_allowed(command):
        return False
    if HEREDOC.search(command):
        return True
    # A build/edit head or a gh api mutation in ANY (env-stripped) segment is implementation — even
    # a single unchained one (`git commit`, `env pytest`, `gh api -X POST`). `_split_chain` splits a
    # bare `&`, so `tg done & git commit` is judged on its `git commit` segment.
    if any(_seg_is_impl_signal(s) for s in _norm_segments(command)):
        return True
    # ...and a mutation smuggled inside a substitution head-position, which no segment head sees.
    if _has_mutating_substitution(command):
        return True
    # Otherwise, multiple chained steps (>= 3 segments, i.e. >= 2 operators) is impl-shaped. Counted
    # from the quote-aware split so a `|` inside a quoted arg is not counted as an operator.
    return len(_split_chain(command)) >= 3


# ── per-repo opt-out (rig.yaml) ─────────────────────────────────────────────────────────
# SYNC: the two helpers below are duplicated in
# agent-hooks/worktree-only-writes/worktree_only_writes.py (each agent-hook dir is deliberately
# self-contained so it installs standalone — no shared import). Keep the parse logic identical.

def _find_rig_yaml(cwd: str) -> Path | None:
    """Walk up from ``cwd`` to the first directory containing a ``rig.yaml`` (or None)."""
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _target_dir(cwd: str, args: dict) -> str:
    """The dir whose rig.yaml governs a WRITE — the target file's dir, else ``cwd``.

    Mirrors worktree-only-writes._target_dir (SYNC): a pre-write's opt-out must follow the checkout
    the FILE lives in, not the shell cwd (they can differ — an absolute write into another repo).
    The file may not exist yet (a create) → walk up to the nearest existing ancestor.
    """
    base = cwd or os.getcwd()
    if not isinstance(args, dict):
        return base
    for key in ("file_path", "path", "notebook_path"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            target = Path(raw)
            if not target.is_absolute():
                target = Path(base) / target
            for cand in (target.parent, *target.parent.parents):
                if cand.exists():
                    return str(cand)
            return base
    return base


def _agent_hooks_bool(rig_yaml_text: str, key: str, default: bool) -> bool:
    """Read the boolean ``agent_hooks.<key>`` from rig.yaml text — deliberately minimal parse.

    Stdlib-only (no PyYAML): find the top-level ``agent_hooks:`` block (indent 0) and read the
    key ONLY as a DIRECT child of it — a deeper-nested ``<key>:`` (e.g. under ``items.<hook>``)
    must NOT flip the guard repo-wide. A malformed/absent key returns the default (fail-open) — a
    parse miss only means "keep the default", the safe direction for a discipline gate.
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


def _orchestrator_only_enabled(cwd: str) -> bool:
    """Whether the thin-orchestrator gate is ON for this repo. env override > rig.yaml > ON.

    Default ON (opt-OUT), unlike the opt-IN worktree-only guard: this gate has always been
    always-on, so an un-enrolled repo must keep firing (no regression). Any falsy
    `RIG_ORCHESTRATOR_ONLY` (`0`/`false`/`no`/`off`) or `agent_hooks.orchestrator_only: false`
    exempts a repo (e.g. 3d-cli). The env falsy set matches the rig.yaml one so `=false` behaves
    like the YAML `false` — an env-only `!= "0"` check surprised users who set `=false` (Opus review).
    """
    env = os.environ.get("RIG_ORCHESTRATOR_ONLY")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    root = _find_rig_yaml(cwd)
    if root is None:
        return True
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {root / 'rig.yaml'}: {exc}")
        return True
    return _agent_hooks_bool(text, "orchestrator_only", default=True)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # Subagents do the actual work → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    point = event.get("point") or ""
    cwd = str(event.get("cwd") or "")

    # Per-repo opt-out (Alex tg#5743): a repo that legitimately does inline work on main
    # (e.g. 3d-cli) sets `agent_hooks.orchestrator_only: false` in its rig.yaml. Default ON, so an
    # un-enrolled repo keeps the current always-on behavior (no regression). For a pre-write the
    # governing repo is the TARGET file's, not the shell cwd (codex) — they can differ.
    cfg_dir = _target_dir(cwd, args) if point == "pre-write" else cwd
    if not _orchestrator_only_enabled(cfg_dir):
        emit("allow")
        return 0

    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    if point == "pre-write":
        offending = _is_code_write(args)
    elif point == "pre-bash":
        offending = _is_implementation_bash(command)
    else:
        offending = False

    if not offending:
        emit("allow")
        return 0

    reason = _override_reason(command, point)
    if reason:
        warn(f"orchestrator work allowed via escape hatch ({reason})")
        emit("allow", f"orchestrator work allowed via escape hatch ({reason})")
        return 0

    # WARN first, BLOCK on repeat within the window.
    if _is_repeat(event):
        emit("block", MESSAGE)
        return BLOCK_EXIT_CODE
    warn(MESSAGE)
    emit("allow", MESSAGE)  # advisory first offense
    return 0


if __name__ == "__main__":
    sys.exit(main())
