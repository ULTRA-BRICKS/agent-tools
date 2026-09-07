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
                blocked — a single one-liner (git status, ls, cat, grep, find, a print-only
                `sed -n`) OR a chain of any
                length whose every segment is read-only OR an orchestration command (tg, review,
                git worktree list). ALMOST ALL `gh` is DELEGATED (Alex tg#7103): CI/PR verification
                (`gh pr checks/view`, `gh run`, `gh api`) and every other gh subcommand are a
                subagent's job, not inline orchestrator work — treated as implementation and
                warn-then-block, exactly like a commit. The ONE exception, restored narrowly (Alex
                tg#9977, agent-tools#159): a genuinely UNCHAINED `gh ship <PR#>` — the gated merge
                itself as the WHOLE, sole command on the line, matched only on that exact shape (a
                bare PR-number argument, no `&&`/`;`/`||`/`|`/bare-`&`/newline anywhere on the
                line) — is sanctioned for the orchestrator to run inline, same as a subagent; every
                other gh shape, including `gh ship` with no PR number, any non-numeric argument, OR
                `gh ship <PR#>` sharing a line with ANYTHING else (even a trailing `| tail` or a
                leading `cd repo &&`), still delegates — the carve-out is a per-LINE grant, not a
                per-segment one (agent-tools#363, closing a `gh ship 205; rm -rf /`-shaped gap a
                per-segment grant left open: see `_is_unchained_gh_ship`'s own docstring). Commits/
                pushes and test runs (git commit/push, pytest, npm/bun/cargo test) are likewise
                treated as implementation and warn-then-block. A dispatched subagent (agent_id
                present) is exempt and runs gh/ship freely regardless.

                `tg` (the tg-cli Telegram reporting tool) is sanctioned for the orchestrator to run
                inline — ALL subcommands, ALL flags, AT ANY CHAIN LENGTH (Alex, direct Telegram
                authorization: "все команды tg-cli" / "ALL tg-cli commands") — this is intentionally
                BROADER than `gh ship`'s single-shape, single-segment carve-out, not the same
                treatment; see `_is_tg_command`'s own docstring for the scope this grants and why it
                stays per-segment where `gh ship` does not. This has been true since agent-tools#164;
                this revision REPLACES the old `ORCH_ALLOW` plain-regex `tg\\b` with `_is_tg_command`,
                a shlex-based predicate (bare `tg` + `VAR=val` env-prefix skip only — deliberately
                NO path-qualification, see the predicate's own docstring for why two review rounds
                landed there) that is now the SOLE authority for the tg allowance and, unlike the
                old regex, correctly rejects a hyphen-suffixed lookalike like `tg-foo`
                (agent-tools#370) — the same laundering-resistance a mutation elsewhere on the line
                still gets (head match only, never a substring/needle; chain-splitting and the
                substitution-liveness scanner still catch a mutation smuggled alongside it).

TIERED (warn → block): the FIRST offense in the TTL window WARNs (allow + message); a REPEAT
in the window BLOCKs. The tier is tracked by a marker file keyed by a hash of cwd. This gives
the doctrine's "WARN then BLOCK" instead of a hard wall on the first inline edit.

Subagent-exempt: a dispatched subagent (``agent_id`` present) does the actual work, so it is
always allowed — this gate governs the orchestrator only.

Harness-exempt (agent-tools#533): this gate's whole premise — a main thread that must delegate
implementation work to a CC ``Agent``/``Task`` subagent instead of doing it inline — is a Claude
Code concept. Codex/opencode DO model a subagent lifecycle of their own (Codex's
``SubagentStart``/``SubagentStop`` events, opencode's ``task`` tool) but NEITHER bridge exposes a
TRUSTED per-tool-call subagent identity from the harness's OWN payload today — see
``lib/codex_hook_bridge`` and ``lib/opencode_hook_bridge`` (the opencode bridge injects one only
for a rig-launched DETACHED child, from the process env set by the ``rig-detached-opencode``
launcher, agent-tools#476; a plain opencode session still has none) — so every Codex/opencode-sourced event used to look like "the
orchestrator" to ``_is_subagent`` — including a bare Codex CLI session Alex runs directly (which
is not a CC orchestrator refusing to delegate at all) and a Codex/opencode process spawned by
another tool as a delegated worker (review-cli's read-only reviewer backend, a CC-dispatched
subagent). Blocking either one is wrong, and the block message's remediation ("Dispatch a subagent
... Agent tool with subagent_type: 'fork'") does not even apply outside Claude Code. omp has the
identical problem: it models its own subagent lifecycle via the ``task`` tool (item shape
``{name?, agent?, task, ...}``, docs/tools/task.md), but ``lib/omp_hook_bridge`` exposes no
TRUSTED per-tool-call subagent identity from omp's ``tool_call``/``tool_result`` extension events
either (confirmed absent from the documented event shape) — so a bare omp CLI session run
directly, and an omp process spawned as a delegated worker by another tool, would both look like
"the orchestrator" to ``_is_subagent`` for the same reason the Codex/opencode cases do, and the
CC-specific remediation text is equally inapplicable there. So a v1 event tagged with a
``harness`` of ``codex``, ``opencode``, or ``omp`` specifically (set by the bridge, never by
``args`` — see ``EXEMPT_HARNESSES``) is exempt from this gate entirely, same as a dispatched
subagent. Deliberately an ALLOWLIST of the three known-safe values, not "any non-CC harness": a
future bridge (``HARNESS = "gemini"``, say) is UNPROVEN here and stays governed until someone
reviews and adds it — a present-but-unrecognized value is not automatically evidence the gate's
CC-specific premise doesn't apply to it, only that nobody has confirmed that yet. If a
Codex/opencode session is ever meant to BE the thin orchestrator in someone's setup, that needs an
explicit config knob, not a change to this hardcoded exemption.

Per-repo opt-out (Alex tg#5743): default ON; a repo that legitimately works inline on main
(e.g. 3d-cli) sets `agent_hooks.orchestrator_only: false` in its rig.yaml, or exports
RIG_ORCHESTRATOR_ONLY=0. Default ON means an un-enrolled repo keeps the current always-on
behavior (no regression). Mirrors the opt-IN worktree-only-writes guard's per-repo knob.

External approval (deny-by-default): there is NO self-service bypass. For a genuine exception,
ASK the human, or request a one-time Telegram approval by setting
`RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="<written justification>"` — when the tiered guard
would BLOCK (a REPEAT offense), the hook asks via a trusted `tg-ctl` and allows ONLY on an
explicit approval tap. A blank value or a bare `1`/`true` is rejected (deny), no Telegram call
is made. An agent can request, not self-grant — the human decides. (This is distinct from the
per-repo ENABLE knob RIG_ORCHESTRATOR_ONLY / agent_hooks.orchestrator_only, which a repo owner,
not the constrained agent, sets to opt a repo out entirely.)

Contract (agents-hooks/v1):
  stdin  : JSON event; args.command (bash) or args.file_path/path (write); event.point
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Iterable

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

# Harnesses whose events are NEVER subject to this gate (agent-tools#533) — see the module
# docstring's "Harness-exempt" section. Deliberately an ALLOWLIST, not `!= "claude-code"`: an
# event with no `harness` field (every fixture written before this change, and any future bridge
# that doesn't set one) stays GOVERNED — the relax direction fails closed, same as `_is_subagent`.
EXEMPT_HARNESSES = frozenset({"codex", "opencode", "omp"})

MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "ORCH_THIN_MARKER_DIR", "~/.cache/agent-tools/orchestrator-thin")))
# How long a first-offense WARN suppresses the next WARN before a REPEAT becomes a BLOCK.
TTL_S = int(os.environ.get("ORCH_THIN_TTL_S", "900"))

# A write to one of these is documentation, never implementation → always allow.
DOCS_PATH = re.compile(r"\.(?:md|mdx|txt|rst)$", re.IGNORECASE)
DOCS_DIR = re.compile(r"(?:^|/)docs/", re.IGNORECASE)

# Inspection / read-only one-liners that the orchestrator legitimately runs itself.
# `sed` is DELIBERATELY ABSENT from this head-only regex (agent-tools#541): a plain `sed\b` would
# also grant `sed -i` (an in-place EDIT) and `sed 'w file'` (a WRITE command), and this regex has
# no argument awareness. A read-only `sed` (a `-n`/print-style invocation with no in-place flag
# and no `w`/`W`/`e` script command) is instead recognized by `_is_read_only_sed`, its own
# shlex-based predicate, consulted alongside this regex by `_seg_is_allowed` and
# `_is_all_read_only` — so `sed -n '1,240p' f && git status` is inspection exactly like `head`,
# while `sed -i` keeps tripping `BUILD_EDIT` as before.
READ_ONLY_BASH = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch)\b|ls\b|cat\b|less\b|head\b|tail\b|"
    r"grep\b|rg\b|find\b|pwd\b|echo\b|which\b|env\b|wc\b|stat\b|tree\b|file\b|jq\b|"
    # read-only system-info + text-filter verification tools (SYNC with fix/159 READ_ONLY_BASH):
    # so a verify pipe `df -h | grep /dev | head` / `gh pr view | jq | head` is not blocked by the
    # >=3-segment rule (coordinator: verification is the orchestrator's own altitude).
    r"sort\b|uniq\b|cut\b|column\b|df\b|du\b|lsblk\b|free\b|ps\b|uname\b|"
    r"uptime\b|whoami\b|hostname\b|nproc\b|date\b)"
)
# Sanctioned ORCHESTRATION commands the main thread legitimately runs ITSELF — coordination and
# reporting only. A chain composed entirely of them (or read-only inspection) must never flap into
# a BLOCK. Head-anchored (`^\s*`) exactly like READ_ONLY_BASH so one of these tokens as an ARGUMENT
# (`cat review.log`, `git log | rg tg`) is not waved through (the anchor invariant, #5/#80).
#
# `gh` IS DELIBERATELY ABSENT from THIS head-only regex (Alex tg#7103: revert the #159/#162 gh-ship
# carve-out). The orchestrator delegates almost all `gh` to a subagent — CI/PR verification (`gh pr
# checks/view`, `gh run`, `gh api`) and every other subcommand are a subagent's job, not inline
# orchestrator work. `gh` is instead an implementation signal by default (`_is_gh_command`, defined
# with the other head-normalization helpers), so a generic gh invocation warn-then-blocks for the
# orchestrator exactly like `git commit`. `gh` delegation subsumes the old `gh api` GET-vs-mutation
# distinction: it no longer matters whether a gh call reads or writes; the orchestrator runs no gh
# (except the one exception below). A dispatched subagent (`agent_id` present) is exempt and runs gh
# freely regardless — the gate governs the orchestrator only.
#
# ONE narrow exception, restored (Alex tg#9977, agent-tools#159): a genuinely UNCHAINED
# `gh ship <PR#>` — the WHOLE, sole command on the line — is sanctioned for the orchestrator to run
# inline. It is NOT added to this head-only regex (ORCH_ALLOW has no argument awareness — it cannot
# tell `gh ship 605` from `gh ship` or `gh pr merge`); instead it is its own shlex-based predicate,
# `_is_gh_ship_command`, consulted ONLY by the top-level, whole-LINE `_is_unchained_gh_ship` (NOT by
# `_seg_is_allowed`/`_seg_is_impl_signal` — agent-tools#363 moved it out of those two per-segment
# predicates after a per-segment grant let `gh ship 205; rm -rf /` slip through unblocked; see
# `_is_unchained_gh_ship`'s own docstring for the full story). Every OTHER gh shape — `gh ship` with
# no PR number, `gh pr merge`, `gh run`, `gh pr checks/view`, `gh api`, AND `gh ship <PR#>` chained
# with anything else at all, even a trailing `| tail` — still delegates.
#
# `tg` is DELIBERATELY ABSENT from THIS head-only regex (moved out, tg-carveout-159, Fable review
# round 3): it used to live here as a plain `tg\b` since agent-tools#164, but a plain regex has no
# argument-position awareness and — worse — its `\b` boundary also matches a hyphen-suffixed
# lookalike (`tg-foo`, agent-tools#370). ALL tg-cli commands (Alex, direct Telegram authorization:
# "все команды tg-cli" / "ALL tg-cli commands", explicitly broader than `gh ship`'s single-shape,
# single-segment carve-out — `tg` stays sanctioned at ANY chain length, `gh ship` only when
# unchained) are instead sanctioned by `_is_tg_command`, its own shlex-based predicate (defined with
# the other head-normalization helpers), consulted directly by `_seg_is_allowed` (per-segment, same
# as before) — the SOLE authority for "is this segment `tg`", not a redundant addition alongside a
# regex. `review\b`/`git worktree list` remain here unchanged; `review\b` has the SAME `\b`-boundary
# quirk (`review-foo` also matches) — out of scope for this PR, tracked as the surviving half of
# agent-tools#370.
ORCH_ALLOW = re.compile(
    r"^\s*(?:"
    r"review\b"                                           # multi-model review CLI (read-only)
    r"|git\s+worktree\s+list\b"                           # worktree inspection
    r")"
)
_DEV_ALLOWED_SUBCOMMANDS = frozenset({"start", "run", "list", "status", "logs", "has-script", "stop", "e2e", "env"})
_DEV_ALLOWED_E2E_SUBCOMMANDS = frozenset({"run", "status", "logs", "stop"})
# Intentionally narrower than `dev run <script>` itself: only common test/e2e script names are
# orchestration; other script names remain implementation-shaped so `dev run deploy` is not a bypass.
_DEV_ALLOWED_RUN_SCRIPTS = frozenset({"test", "tests", "e2e", "smoke"})
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
# ── companion-safety vetoes for the sanctioned-orchestration allow-list ──────────────────────
# The allow-list (`_seg_is_allowed`) judges per segment HEAD, so a benign head (`tg`, `review`, a
# read-only inspection) must not launder a mutation hidden elsewhere in the line: a command/process
# substitution (`tg done $(sed -i …)`, `cat <(git push) && tg done`) or a bare background `&`
# (`tg done & git push` — NOT a chain split) would smuggle a mutation past it. These veto the
# free pass wholesale (scanned quote-blanked so a metachar INSIDE a quoted arg does not trip them).
SUBSTITUTION = re.compile(r"\$\(|`|[<>]\(")
# Bare `&` backgrounding. `&&`, `2>&1`, `&>`, `>&` are excluded (not a control `&`).
BG_AMP = re.compile(r"(?<![&>])&(?![&>])")
# `cd` changes no repo state — the one extra companion a sanctioned chain may carry besides
# read-only inspection (`cd <repo> && tg 'done' | tail`). Argv-boundary anchored (`cd(?=\s|$)`),
# NOT `\b`, so `cd-clean` / `cd/foo` are not mistaken for `cd`.
CD_HEAD = re.compile(r"^\s*cd(?=\s|$)")
# find's mutating primaries — the delete/exec family AND the file-WRITE primaries
# (`-fprint`/`-fprintf`/`-fprint0`/`-fls`, which write an arbitrary path) — are a content signal
# anywhere in a segment: a read-only `find` head must not launder them into the allow-list.
FIND_MUTATION = re.compile(r"\s-(?:delete|exec|execdir|ok|okdir|fprintf|fprint0|fprint|fls)\b")

# Rewritten per the CTO's explicit clarification (tg thread, 2026-07-08/#7103): this hook is an
# INTENTIONAL, jointly-accepted operating model — not friction an agent should route around. The
# orchestrator plans and dispatches; a subagent does the implementation-shaped work (an edit, a
# build, a raw `git commit`/`git push`, a test run — AND nearly all `gh`, including CI/PR
# verification). A prior draft framed this as a terse rule ("does not implement inline") that read
# as adversarial and invited unwanted workarounds. The message now states WHY (the agreed model,
# not an error), HOW to proceed (dispatch a subagent), and that fighting it is out of scope. The
# `gh ship` / read-only-`gh` carve-out (agent-tools#159/#162) was REMOVED here per tg#7103
# (shipping and CI verification were both delegated), then PARTIALLY RESTORED (Alex tg#9977,
# agent-tools#159): narrowly, an UNCHAINED `gh ship <PR#>` only (agent-tools#363 narrowed this
# further to a per-LINE grant — see `_is_unchained_gh_ship`). The orchestrator may run that one
# exact, unchained shape inline again, same as a subagent ("the orchestrator CAN gh ship like
# agents can, but BY DEFAULT agents do it"); CI/PR verification, every other gh subcommand, and a
# `gh ship <PR#>` chained with anything else all stay delegated, so this MESSAGE still fires for
# those.
MESSAGE = (
    "By design, not a bug: this session is the orchestrator, and the operating model — agreed "
    "with the CTO — is that it never implements inline. Planning, dispatching, and reading "
    "reports back is its job; doing the Edit/Write/Bash itself is not — that includes an edit, a "
    "build, a raw `git commit`/`git push`, a test run, AND nearly all `gh` (CI/PR verification "
    "like `gh pr checks`/`gh run` are a subagent's job too, not the orchestrator's — an unchained "
    "`gh ship <PR#>`, alone on the line, is the one sanctioned exception, agent-tools#159/#363). "
    "Dispatch a subagent to do this "
    "(Claude Code: Agent tool with `subagent_type: \"fork\"` "
    "or `isolation: \"remote\"` — both run in the background; opencode: Task with "
    "subagent_type general or explore, or the rig-detached-opencode launcher for a truly "
    "detached child) or model it as a Workflow, then "
    "read its report. This isn't friction to route around — if it's "
    "genuinely wrong for a case, raise that, don't bypass it. There is NO self-service bypass; for "
    "a genuine exception ASK the human, or request a one-time Telegram approval by setting "
    "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN=\"<written justification>\" (deny-by-default; a "
    "bare 1 is rejected). (delegate-work-to-subagents, enforced.)"
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


def _is_exempt_harness(event: dict) -> bool:
    """True when the event came from a bridge whose harness this gate never governs
    (agent-tools#533 — see the module docstring's "Harness-exempt" section).

    TRUST BOUNDARY — read ONLY the TOP-LEVEL `event["harness"]`, never `args`. Each bridge
    (lib/cc_hook_bridge, lib/codex_hook_bridge, lib/opencode_hook_bridge, lib/omp_hook_bridge)
    sets this from a hardcoded module constant
    (`HARNESS = "claude-code" | "codex" | "opencode" | "omp"`) that is not
    derived from ANY field of the underlying tool event — it cannot be forged by a model/
    tool_input value the way a same-named key sitting in `args` could be. Which bridge actually
    fires is decided by which harness's own hook system invoked it: a Claude Code session's Bash
    call is dispatched by CC's own PreToolUse machinery straight into `cc_hook_bridge` and has no
    way to reroute itself through `codex_hook_bridge` instead, so an orchestrator cannot dodge
    this gate by claiming to be a different harness.

    Deliberately an ALLOWLIST (`harness in EXEMPT_HARNESSES`), not `harness != "claude-code"`: a
    missing/unrecognized `harness` (every event built before this change, and any future bridge
    that doesn't set one) must stay GOVERNED — the relax direction fails closed, same as
    `_is_subagent` above."""
    harness = event.get("harness")
    return bool(harness) and str(harness) in EXEMPT_HARNESSES


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
    (`2>&1`, `&>`, `>&`) and the already-handled `&&` are NOT splits (BG_AMP semantics).

    A BACKSLASH-ESCAPED quote character OUTSIDE single quotes does NOT open/close a quoted span —
    e.g. ``gh ship 605 \\' ; rm -rf /`` (agent-tools#363, Fable review round 3): bash treats a
    lone ``\\'`` outside quotes as a literal apostrophe, so the ``;`` that follows is a REAL,
    unescaped command separator (confirmed against `shlex.split`, which treats it identically —
    the trailing tokens are `["'", ";", "rm", "-rf", "/"]`, not a quoted span swallowing them). The
    PRE-fix version of this function had no backslash awareness at all: it saw the bare ``\\'`` as
    an ordinary character followed by a real quote-OPEN, entered quote mode, and then swallowed
    the genuine ``;``/`` rm -rf /`` into the SAME segment as `gh ship 605` — i.e. `_split_chain`
    itself, not just `_is_unchained_gh_ship`, was fooled into reporting ONE segment for a line
    that bash actually runs as TWO commands. Since `_is_unchained_gh_ship`'s whole security
    guarantee rests on `_split_chain` finding EVERY real separator, this was a live bypass of the
    ship carve-out via a single escaped quote character, independent of the substitution veto.
    Handling: a `\\` immediately followed by another character, seen OUTSIDE an open single-quoted
    span, consumes BOTH characters as one literal unit without toggling `quote` — matching bash's
    actual escape semantics closely enough to prevent this class of quote-state confusion (inside
    an OPEN single-quoted span a backslash has no special meaning at all in real bash either, so
    that case is deliberately excluded — see the `quote != "'"` guard below). Full generalized
    shell-escaping (ANSI-C `$'...'` quoting, `\\n` sequences, etc.) remains out of scope — a
    discipline heuristic, not a full shell parser — but the specific "escaped quote hides a real
    separator" class is now closed for both this function and `_blank_single_quoted` (SYNC:
    the two functions intentionally share this exact backslash-handling shape).

    `prev_escaped` guards a SECOND bug the backslash fix above introduced on its own (Opus + Fable
    review round 4 of #363, both independently caught it — the round-3 fix regressed this):
    consuming `\\&` as one literal unit leaves a genuinely UNESCAPED, standalone `&` immediately
    after it — e.g. `gh ship 605 \\&& rm -rf /`, which real bash parses as `gh ship 605 &`
    (backgrounded — confirmed against `bash -x`) followed by a separate `rm -rf /`. The bare-`&`
    check below excludes a `&` whose `prev` char is also `&` (so it doesn't re-split the second
    half of an already-consumed `&&`, or misread `2>&1`/`&>` redirects) — but `prev` here was
    computed from the RAW string, so the just-escaped `&` (now sitting in `buf`, not a real
    adjacent operator character from bash's point of view) wrongly satisfied that exclusion,
    swallowing the genuine standalone `&` into the SAME segment and re-opening exactly the class
    of bypass round 3 closed. `prev_escaped` tracks whether the char immediately behind the cursor
    was consumed as an ESCAPED target (not a real, independently-meaningful operator char), and
    forces `prev` to a neutral empty string in that case, so the following operator char is judged
    on its own merits."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    prev_escaped = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        prev = "" if prev_escaped else (command[i - 1] if i > 0 else "")
        nxt = command[i + 1] if i + 1 < n else ""
        if c == "\\" and i + 1 < n and quote != "'":
            # Escapes the NEXT char outside single quotes (bash semantics) — including an escaped
            # quote, which must NOT toggle `quote` state. Consume both as one literal unit.
            buf.append(c)
            buf.append(command[i + 1])
            i += 2
            prev_escaped = True
            continue
        prev_escaped = False
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

    Feeds the substitution scan (`_substitution_inners`) and, since agent-tools#363, the
    whole-line `_is_unchained_gh_ship` veto directly — so the blanking must follow shell quote
    SEMANTICS (#164 review P2): inside single quotes everything is literal — a `tg 'saw $(x)'` is
    text, blank it. Inside DOUBLE quotes `$(…)` and backticks still EXECUTE, so double-quoted
    content is kept INTACT for the scan — `gh ship "$(gh api … -X POST)"` must have its inner
    command extracted and judged, not erased. (An earlier version blanked both quote kinds, which
    erased exactly the common quoted-mutation form.) Conservative side effect: a literal `<(…)`
    inside double quotes (where process substitution does NOT execute) is still extracted and may
    over-flag — the safe direction for a gate.

    A BACKSLASH-ESCAPED quote character OUTSIDE single quotes does NOT open/close a span (Opus +
    Fable review round 2/3 of #363): `gh ship 605 "it's $(rm -rf /)"` and
    `gh ship 605 --note \\' $(rm -rf /)` were BOTH concrete bypasses of an earlier version of this
    function that had zero backslash awareness — a naive quote-toggle either treated an apostrophe
    inside an already-open `"..."` span as a new toggle (the first case — not actually a bug here,
    since this function only tracks ONE quote type at a time and never blanks double-quoted
    content, but the SAME naive-toggle class of bug DID bite `\\'` outside any quotes: it opened
    phantom single-quote blanking, erasing the live `$(rm -rf /)` that follows and hiding it from
    the `SUBSTITUTION` scan entirely). Handling now matches `_split_chain`'s (SYNC, intentional —
    the two functions share this exact shape): a `\\` immediately followed by another character,
    seen OUTSIDE an open single-quoted span, consumes BOTH characters as one literal unit without
    toggling `quote` — so an escaped quote can never masquerade as a real one. Inside an OPEN
    single-quoted span a backslash has no special meaning at all in real bash (still just literal
    content to blank), so that case is deliberately excluded via the `quote != "'"` guard. Full
    generalized shell-escaping remains out of scope — a discipline heuristic, not a full shell
    parser — but this specific "escaped quote hides live content" class is now closed."""
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n and quote != "'":
            out.append(c)
            out.append(command[i + 1])
            i += 2
            continue
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
        i += 1
    return "".join(out)


# ── read-only `sed` (agent-tools#541) ─────────────────────────────────────────────────────────
# The ONLY `sed` long options a read-only grant tolerates, spelled out in FULL. GNU sed also
# accepts any unambiguous PREFIX of a long option (`--in-pla=.bak` is `--in-place`, `--fil=x` is
# `--file`), so this is an allowlist of exact spellings and EVERY other `--…` token — an
# abbreviation, `--in-place`, `--file`, a future option — makes the invocation not read-only
# (review round 1, codex P1). `--expression`/`--line-length` consume the next token when given
# without `=`.
_SED_SAFE_LONG_NO_VALUE = frozenset({
    "--quiet", "--silent", "--regexp-extended", "--posix", "--debug", "--sandbox",
    "--null-data", "--zero-terminated", "--unbuffered", "--separate", "--binary",
    "--follow-symlinks", "--help", "--version",
})
_SED_SCRIPT_LONG = frozenset({"--expression"})
_SED_LONG_WITH_VALUE = frozenset({"--expression", "--line-length"})
# Short flags (as cluster letters) that make a `sed` NOT read-only: `i` (GNU in-place, with any
# suffix glued on), `I` (BSD/macOS in-place), `f` (script from a file — unknowable). `l` is
# refused too: GNU `-l N` consumes an operand while BSD `-l` does not, so a cluster with it cannot
# be tokenized portably — under-granting is the safe direction for a GRANT predicate.
_SED_REFUSED_SHORT = frozenset("iIfl")
# Script commands / `s`-flags that write or execute: `w`/`W` (write a file), `e` (GNU: execute).
_SED_MUTATING_CMDS = frozenset("wWe")
_SED_MUTATING_S_FLAGS = frozenset("we")


def _is_read_only_sed(segment: str) -> bool:
    """True when a segment's COMMAND head is `sed` used as a read-only PRINT filter (agent-tools#541):
    no in-place flag (`-i`/`-i.bak`/`--in-place`, BSD `-I`), no script read from a FILE (`-f`,
    `--file` — its contents are unknowable here), and no script command that writes or executes
    (`w`/`W` at a command position, incl. after an address or inside a `{` block; the `w`/`e` flags
    of an `s` command; the GNU `e` command). `sed -n '1,240p' f`, `sed -n '/re/p' f | head`,
    `sed 's/a/b/' f` are then inspection exactly like `head`/`tail`/`grep`.

    GRANT-direction predicate, so every ambiguity resolves to "not read-only" (which merely leaves
    the segment to the pre-existing rules — the `sed -i` `BUILD_EDIT` signal, the >=3-segment
    fallback — exactly as before this predicate existed): an unbalanced-quote segment (shlex
    raises), a `-l` cluster (GNU/BSD operand mismatch), a script file, a SHELL-EXPANDED script
    (review round 3, Opus): `shlex` does not expand `$VAR` / `$(…)` / backticks, so `sed -n "$S" f`
    or `sed -n "$(cat s)" f` is exactly as unknowable as `-f script` and forfeits the grant the
    same way — only a `$` OUTSIDE single quotes counts (`sed -n '$p' f`, sed's own last-line
    address, is literal and stays read-only). Script scanning is a small
    hand-rolled walk over sed's grammar (addresses, `s`/`y` delimiters, bracket expressions in a
    regex, `a`/`i`/`c` text, `#` comments, labels/filenames to end-of-line) — a discipline
    heuristic like the rest of this file,
    not a sed parser; where it cannot tell, it errs toward "writes". Basename-exact head match
    (`sed`, `/usr/bin/sed`), never `\b`, so `sed-foo` is not `sed`."""
    expandable = _blank_single_quoted(segment)
    if "$" in expandable or "`" in expandable:
        return False
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    if i >= len(toks) or toks[i].rsplit("/", 1)[-1] != "sed":
        return False
    scripts = _sed_scripts(toks[i + 1:])
    return scripts is not None and not any(_sed_script_mutates(sc) for sc in scripts)


def _sed_scripts(argv: list[str]) -> list[str] | None:
    """The script texts a `sed` argv will run, or None when the invocation is not read-only by
    its OPTIONS alone (in-place, script file, an untokenizable `-l`). Without any `-e`/`-f`, the
    first positional is the script (GNU accepts it before OR after options)."""
    scripts: list[str] = []
    positionals: list[str] = []
    has_explicit_script = False
    j = 0
    while j < len(argv):
        tok = argv[j]
        j += 1
        if tok == "--":
            positionals.extend(argv[j:])
            break
        if tok.startswith("--"):
            name, has_eq, value = tok.partition("=")
            if name not in _SED_SAFE_LONG_NO_VALUE and name not in _SED_LONG_WITH_VALUE:
                return None  # `--in-place`, `--file`, ANY abbreviation, anything unknown
            if name in _SED_LONG_WITH_VALUE and not has_eq:
                if j >= len(argv):
                    return None
                value = argv[j]
                j += 1
            if name in _SED_SCRIPT_LONG:
                scripts.append(value)
                has_explicit_script = True
            continue
        if tok.startswith("-") and len(tok) > 1:
            cluster = tok[1:]
            if _SED_REFUSED_SHORT & set(cluster):
                return None
            e_at = cluster.find("e")
            if e_at >= 0:
                has_explicit_script = True
                glued = cluster[e_at + 1:]
                if glued:
                    scripts.append(glued)
                elif j < len(argv):
                    scripts.append(argv[j])
                    j += 1
                else:
                    return None
            continue
        positionals.append(tok)
    if not has_explicit_script and positionals:
        scripts.append(positionals[0])
    return scripts


def _skip_bracket_expression(script: str, i: int) -> int:
    """From ``script[i] == "["`` inside a REGEX, advance past the whole POSIX bracket expression:
    an optional `^`, an optional leading literal `]`, and `[:class:]` / `[=equiv=]` / `[.coll.]`
    members. Returns the index just after the closing `]`, or -1 when it never closes (sed
    rejects the script: "unterminated address regex")."""
    n = len(script)
    i += 1
    if i < n and script[i] == "^":
        i += 1
    if i < n and script[i] == "]":
        i += 1
    while i < n:
        if script[i] == "[" and i + 1 < n and script[i + 1] in ":=.":
            close = script.find(script[i + 1] + "]", i + 2)
            if close < 0:
                return -1
            i = close + 2
            continue
        if script[i] == "]":
            return i + 1
        i += 1
    return -1


def _skip_regex(script: str, i: int, delim: str) -> int:
    """From ``script[i]`` (the first char INSIDE a regex closed by ``delim``), advance past the
    closing delimiter, honouring backslash escapes and bracket expressions — GNU and BSD sed
    both accept the bare delimiter inside `[...]` (`/[/]/w out` is ONE address then a write,
    review round 2). Returns the index just after the delimiter, or -1 when the regex never
    closes."""
    n = len(script)
    while i < n:
        c = script[i]
        if c == delim:
            return i + 1
        if c == "\\":
            i += 2
        elif c == "[":
            i = _skip_bracket_expression(script, i)
            if i < 0:
                return -1
        else:
            i += 1
    return -1


def _skip_plain_delimited(script: str, i: int, delim: str) -> int:
    """From ``script[i]``, advance past the next unescaped ``delim`` — the `s` REPLACEMENT and
    both `y` operands, where `[` has no meaning (`s/a/[/w out` and `y/[/]/;w out.txt` both
    write, verified on GNU + BSD). Returns the index just after it, or -1 when absent."""
    n = len(script)
    while i < n:
        if script[i] == delim:
            return i + 1
        i += 2 if script[i] == "\\" else 1
    return -1


def _skip_one_sed_address(script: str, i: int) -> int:
    r"""Advance past ONE address at ``script[i]``: `N`, `$`, `/re/`, `\cREc` (with `I`/`M` flags),
    plus GNU's `first~step` / `addr,+N` / `addr,~N` numeric forms. -1 for an unterminated regex."""
    n = len(script)
    if i < n and (script[i].isdigit() or script[i] in "+~"):
        while i < n and (script[i].isdigit() or script[i] in "+~"):
            i += 1
        return i
    if i < n and script[i] == "$":
        return i + 1
    if i < n and script[i] in "/\\":
        if script[i] == "\\":
            if i + 1 >= n:
                return -1
            delim, i = script[i + 1], i + 2
        else:
            delim, i = "/", i + 1
        i = _skip_regex(script, i, delim)
        while 0 <= i < n and script[i] in "IM":
            i += 1
    return i


def _skip_sed_address_clause(script: str, i: int) -> int:
    """Advance past the optional `addr1[,addr2]` + optional `!` in front of a sed command.
    -1 when an address regex is unterminated."""
    n = len(script)
    i = _skip_one_sed_address(script, i)
    if i < 0:
        return -1
    while i < n and script[i] in " \t":
        i += 1
    if i < n and script[i] == ",":
        i = _skip_one_sed_address(script, i + 1)
        if i < 0:
            return -1
        while i < n and script[i] in " \t":
            i += 1
    while i < n and script[i] in "! \t":
        i += 1
    return i


def _sed_script_mutates(script: str) -> bool:
    """True when a sed SCRIPT text contains a command that writes a file or executes: `w`/`W`/`e`
    at a command position, or a `w`/`e` flag on an `s` command. Walks the grammar just enough to
    tell a command position from pattern/replacement/address text — `/w/p` (an address) and
    `s/w/x/` (a pattern) are NOT writes; `1,3w out`, `s/a/b/w out`, `2{w out`, `5q;w out`,
    `:a;w out`, `/[/]/w out` are. Only a/i/c text, r/R filenames and `#` comments run to end of
    line; a label/optional-operand command (`: b t T l q Q`) ends at `;`/newline/`}`. A regex
    (address, `s` pattern) honours bracket expressions, so a delimiter inside `[...]` does not
    end it; anything sed itself rejects (unterminated regex/bracket/`s`, an address without a
    command) counts as a write — the grant-direction default."""
    n, i = len(script), 0
    while i < n:
        c = script[i]
        if c in " \t\n;{}":
            i += 1
            continue
        i = _skip_sed_address_clause(script, i)
        if i < 0 or i >= n:
            # An unterminated address regex, or an address with no command: sed rejects both
            # ("unterminated address regex" / "missing command"). Grant-direction: not read-only.
            return True
        c = script[i]
        if c in _SED_MUTATING_CMDS:
            return True
        if c in "sy":
            # `s<d>pat<d>rep<d>flags` / `y<d>src<d>dst<d>`: the opening delimiter, then TWO more.
            # Only the `s` PATTERN is a regex (bracket expressions honoured); the replacement and
            # both `y` operands are plain text.
            if i + 1 >= n:
                return True
            delim = script[i + 1]
            i = (_skip_regex if c == "s" else _skip_plain_delimited)(script, i + 2, delim)
            if i >= 0:
                i = _skip_plain_delimited(script, i, delim)
            if i < 0:
                return True  # unterminated `s`/`y` — a sed error; grant-direction
            if c == "s":
                while i < n and script[i] not in ";\n}":
                    if script[i] in _SED_MUTATING_S_FLAGS:
                        return True
                    i += 1
            continue
        if c in "aic#rR":
            # a/i/c TEXT, r/R FILENAME, `#` comment: GNU runs these to end of line — a `;` inside
            # is text/filename, not a separator — so a `w` after one on the same line is not a
            # command (and `w`/`W` themselves returned True above before reaching here).
            nl = script.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if c in ":btTlqQ":
            # label (`:x`, `b x`, `t x`, `T x`) and optional-operand (`l N`, `q N`, `Q N`)
            # commands END at `;`/newline/`}` — `sed -n '5q;w out'` / `':a;w out'` / `'l;w out'`
            # DO run the write for every line before the quit/branch (review round 1, Opus +
            # Sonnet), so the scan must resume at the separator, not skip to end of line.
            while i < n and script[i] not in ";\n}":
                i += 1
            continue
        i += 1  # a single-letter command (p, d, n, N, g, G, h, H, x, =, z, D, P, F)
    return False


def _seg_is_allowed(segment: str) -> bool:
    """A single (env-stripped) segment head is read-only inspection OR sanctioned orchestration.

    Head-anchored, so a build/edit token appearing only as an ARGUMENT/needle (`cat tee.log`,
    `git log | rg gh`) stays allowed on its real head, not the needle (the anchor invariant, #5/#80).
    `gh` (ANY subcommand, INCLUDING `ship`) is NOT allowed here — it is delegated (`_is_gh_command`);
    this predicate covers `review`/`git worktree list` (via `ORCH_ALLOW`), `tg` (via its own
    shlex-based `_is_tg_command`), read-only inspection, and the `cd` companion.

    `gh ship <PR#>` is DELIBERATELY ABSENT from this per-segment allow-list (agent-tools#363,
    reverting a #159/#162 regression): the ship carve-out is granted ONLY by
    `_is_unchained_gh_ship` at the whole-LINE level, in `_is_all_inline_allowed`, for a genuinely
    single-segment `gh ship <PR#>` command — never per-segment here. A prior revision consulted
    `_is_gh_ship_command` directly in this OR-chain, which made EVERY segment matching the ship
    shape allowed regardless of chain position; combined with `_seg_is_impl_signal`'s matching
    per-segment exception, that let `gh ship 205; rm -rf /` (or any companion this file has no
    named pattern for — `chmod`, `scp`, `mv`, `dd`, …) sail through silently: the ship segment
    passed the allow-list, the unrecognized companion matched no impl-signal regex either, and a
    2-segment chain doesn't reach the `>= 3` chain-length fallback. Reverting to a per-LINE grant
    closes that gap: the moment `gh ship <PR#>` sits in ANY multi-segment chain, `_seg_is_allowed`
    denies it here exactly as it denies any other `gh` subcommand, so the chain is judged on its
    OTHER segments' own merits (or the `_seg_is_impl_signal` trip below), not waved through by a
    recognized-pattern allow-list that cannot enumerate every possible mutation.

    A benign head does NOT launder a mutation the head-anchor cannot see: find's mutating primaries
    (delete/exec/file-write) and a `git branch` WITH an argument (its `-D`/create forms) are a read
    head that carries a write, so they forfeit the allow-list (agent-tools#159). `cd` (no repo
    state) is an allowed companion so `cd <repo> && tg 'done' | tail` stays an orchestration chain.

    Deliberately NOT gated on `BUILD_EDIT`/other impl-signal regexes THIS FUNCTION DOESN'T ITSELF
    ENFORCE: a sanctioned head/shape (`tg`, `review`, …) is allowed here even when its OWN argument
    TEXT happens to match a mutation-shaped regex this predicate has no explicit veto for (`tg 'saw
    $(git push) in logs'` — `BUILD_EDIT` is never checked by THIS function at all) — that contract
    is "does this segment's HEAD/SHAPE look sanctioned", not "does this segment's text contain no
    mutation-shaped substring anywhere THIS FUNCTION SCANS FOR". It is NOT a blanket "sanctioned
    shape always wins": the two vetoes THIS FUNCTION DOES check (`FIND_MUTATION`, `git branch
    <arg>`) still forfeit the allow-list even for an otherwise-sanctioned segment. For genuine
    mutations THIS function has no veto for at all, the REAL protection is elsewhere and
    unconditional: chain-splitting (a real `&&`/`;`/`|`/bare-`&` makes it a SEPARATE segment, judged
    on its own) and the substitution-liveness scanner (a LIVE `$()`/backtick/`<()`/`>()` executes
    and is caught by `_has_mutating_substitution` regardless of what this predicate says about the
    outer segment).
    """
    if FIND_MUTATION.search(segment):
        return False
    if _git_subcommand(segment) == "branch" and _git_branch_has_arg(segment):
        return False
    if (
        READ_ONLY_BASH.search(segment)
        or _is_read_only_sed(segment)
        or ORCH_ALLOW.search(segment)
        or _dev_segment_is_allowed(segment)
        or CD_HEAD.match(segment)
        or _is_tg_command(segment)
    ):
        return True
    return _git_subcommand(segment) in _GIT_READ_SUBS  # `git -C d status`, `/usr/bin/git log`, …


# The gh head as a plain regex, robust to an UNBALANCED-quote segment that shlex cannot parse:
# optional leading `VAR=val` env-prefixes (`_strip_wrappers` also bails on the bad quote, so they
# survive), then an optional path prefix (`/usr/bin/`), then `gh` at an argv boundary (`(?=\s|$)`,
# NOT `\b`, so `gh-foo` is not `gh`). Mirrors the old `_GH_API_HEAD` regex's quote-robustness. A
# command-WRAPPER (`timeout 60 gh …`) combined with an unbalanced quote is out of scope — best-effort
# discipline, and bash rejects an unbalanced quote before the command ever runs (see module note).
_GH_HEAD_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*(?:\S*/)?gh(?=\s|$)")


def _gh_argv_after_env(toks: list[str]) -> list[str] | None:
    """Skip leading `VAR=val` env-prefix tokens, then confirm the (now-first) token is `gh`
    (basename-normalized, e.g. `/usr/bin/gh` -> `gh`). Returns the argv AFTER `gh` (an empty list
    for a bare `gh` with no subcommand), or `None` when the env-stripped head is not `gh`.

    Shared by `_is_gh_command` and `_is_gh_ship_command` so BOTH the env-skip AND the basename
    check are written ONCE (Fable review: an earlier version of this helper skipped env-prefixes
    only, leaving the basename check duplicated in both callers — a drift risk this closes). Each
    caller still does its OWN try/except around this, because the two need DIFFERENT behavior on
    an unparseable segment (deny falls back to a conservative head regex; the ship carve-out just
    says no)."""
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    if i >= len(toks) or toks[i].rsplit("/", 1)[-1] != "gh":
        return None
    return toks[i + 1:]


def _is_gh_command(segment: str) -> bool:
    """True when a segment's COMMAND head is `gh` (any subcommand) — the orchestrator delegates
    ALMOST ALL gh (tg#7103); a caller needing the ONE narrow exception (`gh ship <PR#>`,
    agent-tools#159) consults `_is_gh_ship_command` separately, this predicate alone does not
    carve it out. shlex + basename, so a path-qualified head (`/usr/bin/gh ship`) is caught too —
    the SAME normalization `_git_subcommand` applies to git, closing the asymmetry a bare `^gh\\b`
    regex left open (Opus review). `gh` only as an ARGUMENT/needle (`cat gh.md`, `git log | rg gh`,
    `grep 'gh ship' log`) is NOT a gh command: only `toks[0]` (the head) is inspected.

    Leading `VAR=val` env-prefixes are skipped in BOTH branches so the predicate is self-contained
    and symmetric — a caller that forgets to `_strip_wrappers` first still classifies
    `GH_PAGER=cat gh ship` correctly (the shlex and unbalanced-quote paths agreed only after this;
    Opus review). On an UNBALANCED-quote segment shlex cannot parse, fall back to the head regex
    rather than returning False — the old `gh api` path was CONSERVATIVE here (block on unparseable),
    so `gh ship 605 'oops` must still register as gh (the safe direction for a delegation gate)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return bool(_GH_HEAD_RE.match(segment))  # unbalanced quotes → conservative head match
    return _gh_argv_after_env(toks) is not None


# A bare PR number: ASCII digits only (never a unicode digit look-alike, never a URL/branch name).
# `\Z` (not `$`) so a trailing newline in the token can't sneak a match (Opus review) — a discipline
# heuristic detail, not exploitable (any real newline in the raw command chain-splits first), but
# the stricter anchor matches intent in a security-relevant predicate.
_PR_NUMBER_RE = re.compile(r"^[0-9]+\Z")
def _is_gh_ship_command(segment: str) -> bool:
    """True when a segment's COMMAND is narrowly `gh ship <PR#>` — the ONE gh mutation carved back
    out for the orchestrator (agent-tools#159, restored; Alex tg#9977: "the orchestrator CAN gh
    ship like agents can, but BY DEFAULT agents do it"). Deliberately narrow: the head must be
    `gh` (basename/env-prefix normalized exactly like `_is_gh_command`, via the shared
    `_gh_argv_after_env`), the very next token must be the literal `ship`, and the token after THAT
    must be a bare PR number (ASCII digits only, non-empty). `gh ship` alone (no PR number),
    `gh ship --help`, `gh ship abc`, and every OTHER gh subcommand (`gh pr merge`, `gh run`,
    `gh pr checks`/`view`, `gh api`, …) do NOT match and fall through to `_is_gh_command`'s general
    (still-delegated) deny.

    PURE ARGV-SHAPE predicate, deliberately chain-unaware — it says nothing about WHERE `segment`
    sits on the command line. `_seg_is_allowed`/`_seg_is_impl_signal` do NOT consult this directly
    (agent-tools#363): the actual carve-out gate is `_is_unchained_gh_ship`, which wraps this call
    with the whole-LINE "is there only one segment at all" check that decides whether the ship
    exception actually applies. See that function's docstring for why a per-segment consultation of
    THIS predicate alone used to be the security gap.

    Anything AFTER the PR number — flags (`--screenshot`, `--repo`, even `--skip-ci`), a trailing
    redirect token (`2>&1`) — is unrestricted: this predicate recognizes the ship *shape* only and
    does not vet ship's own flags. `--skip-ci` in particular has its OWN separate,
    independently-gated live-Telegram hatch inside `ci/ship/ship.sh` (`RIG_HATCH_REQUEST_SHIP_SKIP_CI`)
    that this gate does not duplicate — an inline `gh ship 605 --skip-ci` passes THIS gate the same
    as any other ship, and ship.sh's own gate is the sole control on the skip-ci bypass itself.

    Basename/env-prefix normalization intentionally mirrors `_is_gh_command`'s DENY-side behavior
    reused on the ALLOW side (Fable review flagged the asymmetry: a deny-side conservatism becomes
    permissive when reused for a grant, e.g. a `gh` binary at a nonstandard path or under an
    unrelated env-prefix also qualifies). Accepted as consistent with this module's stated threat
    model — a cooperative orchestrator, not an adversarial one; `on_error: open` and the whole gate
    are discipline, not a security boundary (see the module docstring) — rather than diverging the
    two predicates' head-matching and risking the drift this shared helper exists to prevent.

    On an UNPARSEABLE (unbalanced-quote) segment, shlex raises — return False (not recognized as
    ship) rather than guessing, so the segment falls through to `_is_gh_command`'s OWN conservative
    head-regex fallback and stays delegated. This is the mirror image of `_is_gh_command`'s own
    unbalanced-quote fallback (which blocks): the safe default for GRANTING an allowance is "no
    match", while the safe default for a DENY is "match" — same direction of caution, opposite
    boolean outcome."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    argv = _gh_argv_after_env(toks)  # None if the (env-stripped) head isn't `gh`
    if argv is None:
        return False
    return len(argv) >= 2 and argv[0] == "ship" and bool(_PR_NUMBER_RE.match(argv[1]))


def _is_unchained_gh_ship(command: str) -> bool:
    """True when the ENTIRE command line — including any TRAILING separator, not only an internal
    one — is free of `&&`/`||`/`;`/`|`/bare-`&`/newline, free of a heredoc, and free of a LIVE
    substitution, whose (wrapper-stripped) content is exactly `gh ship <PR#>`
    (`_is_gh_ship_command`). THE sole grant point for the ship carve-out (agent-tools#363): a
    per-LINE grant for a genuinely unchained, single-purpose invocation, not a per-segment one.

    This is deliberately a top-level, whole-command check rather than something `_seg_is_allowed`/
    `_seg_is_impl_signal` decide per segment. The PRIOR design (agent-tools#159/#162, restored)
    wired the ship exception into those two per-segment predicates directly, which made EVERY
    segment matching the ship shape exempt regardless of where it sat in a chain — so `gh ship
    205; rm -rf /` passed silently: the ship segment was allowed, `rm -rf /` matched no impl-signal
    regex this file recognizes (BUILD_EDIT/uv-test/git-python-impl/dev/gh are all narrow,
    named-pattern lists — they cannot enumerate every destructive command), and a 2-segment chain
    never reaches the `>= 3` chain-length fallback. Gating the grant on "is this line free of every
    separator" closes THAT specific hole — a `gh ship <PR#>` sharing a line with anything else,
    even a single trailing `| tail` or a leading `cd repo &&`, returns False here, `gh` reverts to
    being an ordinary (still-delegated) impl-signal via `_seg_is_impl_signal`, and the WHOLE line
    is judged by the pre-#159 rules: a `gh` segment anywhere trips it, independent of what the
    other segments are or whether THEY happen to match a recognized mutation pattern. It does NOT
    close the broader, pre-existing "an unrecognized head with no impl-signal in a <3-segment
    chain silently falls through to allowed" behavior that this whole file has always had for
    every OTHER sanctioned head (`tg`, `review`, read-only inspection) — see the module's own
    per-segment allow-list docs and the README's "Known, PRE-EXISTING gap" note for that boundary;
    fixing it is a separate, unscoped change this PR deliberately does not make (Fable review,
    round 2: an earlier draft of this docstring said the fix closes the gap "unconditionally",
    which overstated it — it closes it for lines a `gh ship` grant would otherwise have waved
    through, not for the file's fall-through-to-allowed default in general).

    SELF-CONTAINED on HEREDOC/substitution, not delegated to caller discipline (Opus + Fable
    review, round 2 of #363): an earlier revision relied on `_is_all_inline_allowed` checking
    `HEREDOC`/`_has_mutating_substitution` BEFORE ever consulting this function, documented only
    as a "callers MUST" docstring warning. That left an identical hole ONE LEVEL DOWN from the
    chain-operator gap this function exists to close: `gh ship 605 $(rm -rf /)` is a single
    segment (`_split_chain` sees no `;`/`&&`/…), so it reached this function; `_is_gh_ship_command`
    matches (`605` is clean, everything after — including the whole `$(rm -rf /)` token — is
    unrestricted trailing text by design); and `_has_mutating_substitution`'s OWN narrow-pattern
    scan does not recognize `rm -rf /` as a mutation any more than `_seg_is_impl_signal` recognizes
    it chained — the exact same "unenumerable companion" flaw, reached via `$()` instead of `;`.
    Rather than trying to enumerate every destructive command a substitution could carry (the
    unenumerable-list problem this whole fix exists to get away from), this function now refuses
    the grant on ANY live substitution marker at all — `$(`, a backtick, `<(`, or `>(` (process
    substitution executes too, same as command substitution) — found OUTSIDE single quotes
    (scanned via `SUBSTITUTION` against the single-quote-blanked command, the same quote-semantics
    `_substitution_inners` uses: a literal `$(...)` inside single quotes never executes and does
    not disqualify the grant, e.g. `gh ship 605 --note 'ran $(build) earlier'` stays allowed; a
    live one inside DOUBLE quotes still executes and IS caught, including when an unrelated
    apostrophe sits inside the same double-quoted span, e.g. `gh ship 605 "it's $(rm -rf /)"` —
    `_blank_single_quoted` only enters blanking mode on a `'` seen OUTSIDE an open `"..."` span, so
    the apostrophe here never toggles quote state and the live `$(...)` stays visible to the scan).
    This also makes the function itself the sole, self-contained authority its docstring already
    claimed it was — a future caller that consults it directly, without first replicating
    `_is_all_inline_allowed`'s HEREDOC/substitution ordering, gets the same safe answer, instead of
    silently reopening this class of gap.

    TRAILING separators disqualify too, not only internal ones (Fable review, round 2): a bare
    `gh ship 605;` or `gh ship 605 &` has no companion command after the separator, so
    `_split_chain` drops the resulting empty segment and `len(segs) == 1` alone would still call
    it unchained — contradicting this docstring's own "no separator anywhere" contract and, worse,
    silently BACKGROUNDING the sanctioned merge for the trailing-`&` case (the orchestrator would
    lose the synchronous exit status of the one mutation it is allowed to run inline — the same
    backgrounding concern the sibling `subagent-no-bg-longproc` hook polices elsewhere). Checking
    `segs[0] == command` (not just `len(segs) == 1`) catches this: `_split_chain` only returns the
    ORIGINAL string unchanged, byte-for-byte, when it found zero separators to split on; any real
    separator — leading, trailing, or internal — changes what comes back, even after empty-segment
    filtering collapses the segment COUNT back down to one."""
    if HEREDOC.search(command):
        return False
    if SUBSTITUTION.search(_blank_single_quoted(command)):
        return False
    segs = _split_chain(command)
    return len(segs) == 1 and segs[0] == command and _is_gh_ship_command(_strip_wrappers(segs[0]))


def _is_tg_command(segment: str) -> bool:
    """True when a segment's COMMAND head is `tg` (the tg-cli Telegram reporting tool) — ANY
    subcommand, ANY flag combination is sanctioned: plain text, `--file`, `--photo`, `--format
    html`, `--tag`, `--reply-to`, `voice setup`, and everything else tg-cli exposes. Authorized
    explicitly and broadly by Alex (Telegram, direct reply, this thread): "оркестратор ещё и tg:*
    должен уметь, т.е. все команды tg-cli" — "the orchestrator should ALSO be able to run tg:*,
    i.e. ALL tg-cli commands". Unlike `gh ship <PR#>`'s PR-number-shape narrowing, this predicate
    does NOT narrow by subcommand or argument position: tg is a leaf reporting/notification tool
    with no subcommand this gate has reason to withhold from the orchestrator.

    THE SOLE authority for "is this segment `tg`" (tg-carveout-159, Fable review round 3) —
    `ORCH_ALLOW` used to carry a plain `tg\\b` regex alternative for this (agent-tools#164); that
    regex is now REMOVED from `ORCH_ALLOW` and this predicate replaces it outright, not sitting
    alongside it as a redundant addition (an earlier draft of this PR kept both — Fable correctly
    flagged that `ORCH_ALLOW` being checked FIRST in `_seg_is_allowed`'s OR-chain made this
    predicate unreachable dead code end-to-end, since the plain regex matched a strict superset of
    what it could ever grant). Consolidating ALSO fixes agent-tools#370 for the `tg` case: `\\b` is
    a WORD-boundary, which also fires before a hyphen, so the old regex incorrectly matched
    `tg-foo` too — this predicate's basename-EXACT token check (`toks[i] == "tg"`) does not have
    that quirk, and now that it is the only gate, `tg-foo` is correctly rejected end-to-end (see
    `test_tg_foo_is_now_rejected_end_to_end`, which supersedes the pinned-quirk test an earlier
    draft had). `review`'s matching `\\b` quirk (`review-foo`) is UNCHANGED and out of scope here —
    the surviving half of agent-tools#370.

    shlex-based, with a self-contained `VAR=val` env-prefix skip (matching `_is_gh_command`'s own
    precedent, so a caller that invokes this predicate directly, without going through
    `_strip_wrappers` first, still classifies `TG_BOT_TOKEN=x tg 'msg'` correctly).

    **Deliberately NO path-qualification** (`/opt/homebrew/bin/tg`), despite `_is_gh_command`
    accepting any path — resolved across two review rounds, not shipped as a guess. `_is_gh_command`
    is a DENY-direction predicate (over-matching there only routes more things to a subagent, the
    safe failure mode); this predicate is GRANT-direction (a match means "never even warn"), so the
    same blanket path normalization would over-match in the UNSAFE direction — a relative path
    (`./tg`) is trivial to place anywhere near the working directory, and narrowing to "absolute
    paths only" does not actually narrow anything real (`/tmp/tg` is just as easy to write as
    `./tg` — disproved empirically in round 2). A hardcoded bin-dir allowlist was also rejected: it
    would be unreliable in practice (this predicate's own authoring machine resolves its real `tg`
    to a non-standard, dotfiles-managed path, not any short "standard" list). Given this gate's own
    stated threat model (discipline, not a security boundary — see the module docstring's
    "Fail-open, on purpose") and that the practical need (Alex's ask) is fully met by subcommand
    breadth alone, path-qualification was dropped entirely.

    Deliberately does NOT exclude argument text that happens to look like a mutation (`tg 'ran
    sed -i on it'`) — the SAME "sanctioned shape wins over argument text this function doesn't scan
    for" precedent `review` already has (see `_seg_is_allowed`'s own docstring). `gh ship <PR#>`
    used to share this exact per-segment precedent too (agent-tools#159), but agent-tools#363 moved
    its grant to the whole-LINE `_is_unchained_gh_ship` instead — see
    `test_incidental_mutation_shaped_text_in_a_ship_segment_matches_tg_precedent`, which still pins
    the `gh ship` case but now documents the DIFFERENT (line-level, not segment-level) mechanism
    that grants it. The real protection against a genuine mutation is unconditional and elsewhere:
    chain-splitting (a real `&&`/`;`/`|`/bare-`&` makes it a SEPARATE segment) and the
    substitution-liveness scanner (a LIVE `$()`/backtick/`<()`/`>()` executes and is caught
    regardless of what this predicate says about the outer segment).

    On an UNPARSEABLE (unbalanced-quote) segment, shlex raises — return `False`, mirroring
    `_is_gh_ship_command`'s conservative-fallback DIRECTION (deny-by-default; a discipline gate
    should not grant on an input it cannot confidently classify), NOT the old regex's "recognize
    it anyway" fallback. That old direction was justified ONLY by parity with the (now-removed)
    `tg\\b` regex, which had no notion of "unparseable" at all; now that this predicate is the sole
    authority rather than a redundant addition, the parity argument no longer applies, and the safe
    default for a GRANT-direction predicate facing uncertain input is to not grant (Opus/Fable
    round 3: decide the fallback direction on its own merits once the predicate is load-bearing,
    not by inheriting an old regex's behavior)."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    return i < len(toks) and toks[i] == "tg"


def _seg_is_impl_signal(segment: str) -> bool:
    """A single (env-stripped) segment is implementation on its own: a build/edit head (sed -i, tee,
    npm/…), a `git commit`/`push` or a `pytest`/`python -m pytest` test (all spellings), a
    `uv run … pytest` wrapper, an unknown `dev` command, OR ANY `gh` command at all (pr/run/api/ship
    — delegated, tg#7103). Caught even unchained (codex P1).

    `gh ship <PR#>` has NO exception here (agent-tools#363, reverting a #159/#162 regression): a
    prior revision special-cased it (`_is_gh_command(segment) and not _is_gh_ship_command(segment)`)
    so a ship segment was never a trip signal regardless of chain position — combined with the same
    exception in `_seg_is_allowed`, that let a chained `gh ship 205; rm -rf /` slip through, since
    neither the ship segment (allow-listed) nor the unrecognized `rm -rf /` companion (matches no
    impl-signal regex this file has a pattern for) ever tripped, and a 2-segment chain never reaches
    the `>= 3` chain-length fallback either. The carve-out now lives EXCLUSIVELY in
    `_is_unchained_gh_ship`, consulted by `_is_all_inline_allowed` at the whole-line level, for a
    genuinely single-segment `gh ship <PR#>` command — never here. This function's job is simpler
    and matches its PRE-#159 form: ANY `gh` head, ship included, is a trip signal, so a `gh ship`
    segment sharing a line with anything else makes the WHOLE line implementation-shaped, exactly
    like every other `gh` subcommand."""
    if (
        BUILD_EDIT.search(segment)
        or _is_uv_test(segment)
        or _is_git_or_python_impl(segment)
        or _dev_segment_is_unknown(segment)
    ):
        return True
    return _is_gh_command(segment)


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
        args = toks[2:]
        while args and args[0] == "--repo-only":
            args = args[1:]
        return bool(args) and (args[0] in _DEV_ALLOWED_RUN_SCRIPTS or args[0] in ("-h", "--help"))
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


_UV_TEST_SHORT_FLAGS_WITH_VALUE = frozenset({"C", "P", "b", "c", "f", "i", "p", "w"})
# Keep these uv option tables in sync with dev-cli's dev_cli/cli.py _UV_RUN_* copies
# (the `dev` CLI was extracted from agent-tools into the standalone alex-mextner/dev-cli repo).
_UV_TEST_BOOLEAN_FLAGS = frozenset({
    "--active",
    "--all-extras",
    "--all-groups",
    "--all-packages",
    "--compile-bytecode",
    "--exact",
    "--frozen",
    "--gui-script",
    "--help",
    "--isolated",
    "--locked",
    "--managed-python",
    "--module",
    "--native-tls",
    "--no-build",
    "--no-build-isolation",
    "--no-binary",
    "--no-cache",
    "--no-config",
    "--no-default-groups",
    "--no-dev",
    "--no-editable",
    "--no-env-file",
    "--no-index",
    "--no-managed-python",
    "--no-progress",
    "--no-project",
    "--no-python-downloads",
    "--no-sources",
    "--no-sync",
    "--offline",
    "--only-dev",
    "--refresh",
    "--reinstall",
    "--script",
    "--upgrade",
    "-U",
    "-h",
    "-m",
    "-n",
    "-q",
    "-s",
    "-v",
})
_UV_TEST_FLAGS_WITH_VALUE = frozenset({
    "--allow-insecure-host",
    "--build-constraint",
    "--cache-dir",
    "--color",
    "--config-file",
    "--config-setting",
    "--config-settings-package",
    "--constraint",
    "--default-index",
    "--directory",
    "--env-file",
    "--exclude-newer",
    "--exclude-newer-package",
    "--extra",
    "--extra-index-url",
    "--find-links",
    "--fork-strategy",
    "--group",
    "--index",
    "--index-strategy",
    "--index-url",
    "--keyring-provider",
    "--link-mode",
    "--no-binary-package",
    "--no-build-isolation-package",
    "--no-build-package",
    "--no-extra",
    "--no-group",
    "--only-group",
    "--package",
    "--prerelease",
    "--project",
    "--python",
    "--python-preference",
    "--python-platform",
    "--refresh-package",
    "--reinstall-package",
    "--resolution",
    "--upgrade-package",
    "--with",
    "--with-editable",
    "--with-requirements",
})


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
    for i in _uv_test_payload_starts(toks, 2):
        if i >= len(toks):
            continue
        cmd = toks[i]
        if cmd in ("pytest", "tox"):
            return True
        if cmd == "python" and toks[i + 1:i + 3] in (["-m", "pytest"], ["-m", "unittest"]):
            return True
    return False


def _uv_test_payload_starts(toks: list[str], index: int) -> Iterable[int]:
    pending = [index]
    visited: set[int] = set()
    yielded: set[int] = set()
    while pending:
        cursor = min(pending.pop(), len(toks))
        if cursor in visited:
            continue
        visited.add(cursor)
        while cursor < len(toks):
            opt = toks[cursor]
            if opt == "--":
                cursor += 1
                break
            if opt == "-" or not opt.startswith("-"):
                break
            if opt.startswith("--"):
                flag = opt.split("=", 1)[0]
                if "=" in opt:
                    cursor += 1
                elif flag in _UV_TEST_FLAGS_WITH_VALUE:
                    cursor += 2
                elif flag in _UV_TEST_BOOLEAN_FLAGS:
                    cursor += 1
                else:
                    pending.append(cursor + 2)
                    cursor += 1
            elif _uv_test_short_flag_takes_following_value(opt):
                cursor += 2
            elif _uv_test_short_flag_has_attached_value(opt):
                cursor += 1
            elif _uv_test_short_flag_is_known_boolean(opt):
                cursor += 1
            else:
                pending.append(cursor + 2)
                cursor += 1
        cursor = min(cursor, len(toks))
        if cursor not in yielded:
            yielded.add(cursor)
            yield cursor


def _uv_test_short_flag_takes_following_value(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    flags = token[1:]
    for pos, flag in enumerate(flags):
        if flag in _UV_TEST_SHORT_FLAGS_WITH_VALUE:
            return pos == len(flags) - 1
    return False


def _uv_test_short_flag_has_attached_value(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    flags = token[1:]
    return any(
        flag in _UV_TEST_SHORT_FLAGS_WITH_VALUE and pos < len(flags) - 1
        for pos, flag in enumerate(flags)
    )


def _uv_test_short_flag_is_known_boolean(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    return all(f"-{flag}" in _UV_TEST_BOOLEAN_FLAGS for flag in token[1:])


def _is_all_read_only(command: str) -> bool:
    """True when EVERY (env-stripped) chain segment head is a read-only inspection command.

    A fully read-only pipe (`find ... | grep ... | head`) is the orchestrator's bread-and-butter
    inspection and must never be blocked, no matter how many segments it has (#80).
    """
    if HEREDOC.search(command):
        return False
    segs = _norm_segments(command)
    return bool(segs) and all(READ_ONLY_BASH.search(s) or _is_read_only_sed(s) for s in segs)


def _is_all_inline_allowed(command: str) -> bool:
    """True when EVERY (env-stripped) segment is read-only inspection OR sanctioned orchestration,
    OR the whole line is a genuinely unchained `gh ship <PR#>` (`_is_unchained_gh_ship`).

    The superset used to decide "not implementation": a chain of only `gh pr list`/`tg`/… (or
    read-only inspection) is orchestration, never blocked. The flapping fix (Alex tg#5743 /
    agent-tools#23).

    Rejects a chain whose substitution smuggles a MUTATION (`tg done $(gh api -X POST …)`) — the
    head-anchored allow check cannot see inside `$(…)`, so this is the only place to catch it before
    the fast path returns. A BENIGN substitution (`cat $(find …) | grep | head`) is NOT rejected, so
    the #80 read-only-pipe-of-any-length invariant holds (agent-tools#159, Opus review). A bare `&`
    is handled by `_split_chain` splitting it, so its segments are judged individually.

    The `_is_unchained_gh_ship` check sits AFTER these two vetoes, but — unlike an earlier revision
    of this comment claimed — that ordering is no longer LOAD-BEARING (Fable review, round 2 of
    #363): `_is_unchained_gh_ship` is now self-contained and independently refuses HEREDOC and any
    live substitution itself (see its own docstring), so `gh ship 605 $(sed -i x)` is caught either
    way — by this function's own veto above, OR by `_is_unchained_gh_ship`'s internal one, should a
    future edit ever reorder these three checks. The ordering here is kept for readability (vetoes
    before grants) and as defense-in-depth, not because correctness depends on it.

    NOT a "does this line contain at least one orchestration head" check (tg-carveout-159, Opus +
    Fable review round 4 both independently misread it that way): `all(_seg_is_allowed(s) for s in
    segs)` is a per-segment universal check with no minimum-count or specific-source requirement —
    a chain whose ONLY orchestration head is `_is_tg_command`-matched (not `ORCH_ALLOW`-matched),
    or a chain that is PURELY read-only with zero orchestration heads at all, both satisfy this the
    SAME way. Moving `tg` out of `ORCH_ALLOW` and into its own predicate therefore does not change
    which chains get the any-length exemption — every `_seg_is_allowed` source (`READ_ONLY_BASH`,
    `ORCH_ALLOW`, `_is_tg_command`, `_dev_segment_is_allowed`, `CD_HEAD`) contributes equally to
    this `all(...)`. `gh ship <PR#>` is deliberately NOT one of those sources any more
    (agent-tools#363) — it is granted only by the separate `_is_unchained_gh_ship` check below,
    which is per-LINE, not per-segment; see that function's own docstring for why.
    """
    if HEREDOC.search(command):
        return False
    if _has_mutating_substitution(command):
        return False
    if _is_unchained_gh_ship(command):
        return True
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
    # ORDERING IS LOAD-BEARING for the unchained `gh ship <PR#>` grant (agent-tools#363, Opus
    # review round 5): `_seg_is_impl_signal("gh ship 605")` is unconditionally True (no per-segment
    # ship exception any more), so a bare `gh ship 605` is allowed ONLY because this check runs
    # BEFORE the impl-signal scan below. Do not reorder these two checks.
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

    # Codex/opencode/omp events → this gate's CC-specific orchestrator/subagent premise doesn't
    # apply, and its remediation (CC's Agent tool) doesn't exist there either (agent-tools#533).
    if _is_exempt_harness(event):
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

    # WARN first, BLOCK on repeat within the window. Only a would-be BLOCK consults the hatch —
    # a first-offense WARN is advisory (allow) and needs no escalation.
    if _is_repeat(event):
        ctx = {"hook": "orchestrator-stays-thin", "point": point, "command": command}
        # Resolve the hatch config (rig.yaml / tg_ctl_path) from the GOVERNING repo — for a
        # pre-write that is the TARGET file's repo (`cfg_dir`), which can differ from the shell
        # `cwd`; using `cwd` would route/deny an approval for a cross-repo write via the wrong
        # repo's config. The inline `RIG_HATCH_REQUEST_*=` form only exists for the pre-bash
        # point (a pre-write has no shell command to carry it — the var must be exported there).
        hatch = hatch_escalation.request_hatch_approval(
            "orchestrator-stays-thin",
            ctx,
            cwd=cfg_dir,
            command=command if point == "pre-bash" else None,
        )
        if hatch.should_stop:
            if hatch.approved:
                warn(f"orchestrator-stays-thin allowed via hatch escalation ({hatch.reason})")
                emit("allow", f"allowed via hatch escalation ({hatch.reason})")
                return 0
            emit("block", f"hatch escalation denied: {hatch.reason}\n{MESSAGE}")
            return BLOCK_EXIT_CODE
        emit("block", MESSAGE)
        return BLOCK_EXIT_CODE
    warn(MESSAGE)
    emit("allow", MESSAGE)  # advisory first offense
    return 0


if __name__ == "__main__":
    sys.exit(main())
