#!/usr/bin/env python3
"""agents-hooks/v1 pre-agent hook — push non-trivial subagent dispatches to the background.

Fires when the MAIN thread is about to dispatch a subagent (the CC `Agent`/`Task` tool).
A non-trivial subagent run in the FOREGROUND blocks the orchestrator until it finishes —
which defeats the whole point of fanning work out. This gate blocks such a foreground
dispatch and tells the orchestrator to dispatch it as a `fork`, `isolation: "remote"`, or a
dynamic Workflow instead. It enforces the orchestration half of `delegate-work-to-subagents`.

Allowed (let through):
  - ``subagent_type: "fork"`` — CC's own `Agent` tool description states a fork "runs in the
    background... you get a completion notification", so it is inherently the desired shape
  - ``isolation: "remote"`` — CC's own tool description states this "always runs in
    background"
  - an opencode ``tool: "task"`` dispatch with ``subagent_type`` general or explore — the
    discriminator is the exact lowercase tool name ``task``; CC uses ``Agent``/``Task`` and
    still needs fork/remote. Verified truth (agent-tools#476, opencode 1.18.20): the task
    tool's model-facing schema is ONLY description/prompt/subagent_type/task_id/command —
    there is NO background field, a default-build dispatch runs in the FOREGROUND, and the
    native ``background`` boolean exists ONLY behind
    ``OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true``. It is allowed anyway, deliberately:
    opencode has no in-tool background path at all, so demanding one left a plain opencode
    session with NO executable dispatch (the deadlock in agent-tools#495). This gate's
    thin-orchestrator premise is Claude-Code-specific — the same reasoning that exempts
    opencode from orchestrator-stays-thin by ``event["harness"]`` (agent-tools#533/#544).
    The sanctioned TRULY detached path on a default build is the ``rig-detached-opencode``
    launcher (next bullet); an explicit ``run_in_background: false`` still counts as
    foreground.
  - a dispatch already marked ``run_in_background: true`` — dead on CC (see NOTE below); for
    opencode live only in the experimental configuration: the bridge
    (`lib/opencode_hook_bridge/dispatch.py`) maps the native ``background`` field into
    ``run_in_background`` only when the hosting opencode process carries the flag, so this
    allow path is live exactly when opencode would actually honor it. For a default opencode
    build the sanctioned detached mechanism is the canonical launcher provisioned from the
    `rig-detached-opencode` universal skill to
    ``~/.agents/skills/rig-detached-opencode/rig-detached-opencode`` (the default
    skills_target — a machine that customizes it sees its own target in ``rig status``;
    `bin/` is not a rig-discovered carrier, so the REMINDER must name the provisioned
    skill copy): RIG_AGENT_ID/RIG_DETACHED_AGENT markers -> bridge injects agent_id -> the
    detached child session is subagent-exempt, not a foreground dispatch at all.
  - a TRIVIAL one-liner dispatch (short, single-line prompt) — cheap enough to run inline
  - a dispatch made BY a subagent itself (subagent-exempt: ``agent_id`` present) — a subagent
    may fan out further, and this gate governs the orchestrator, not the workers

NOTE (CC-specific): as of CC 2.1.177 the `Agent` tool's JSON schema carries no
`run_in_background` property at all (only `description`, `isolation`, `model`, `prompt`,
`subagent_type`) — under schema-constrained tool calling the model cannot produce that field,
so treating it as the ONLY non-trivial allow path hard-blocked every fork/remote dispatch with
no way through short of a Telegram hatch call every single time. Recognizing
`subagent_type: "fork"` and `isolation: "remote"` — the two shapes CC's own docs already
guarantee run in the background — is what actually satisfies this hook's intent instead.

External approval (deny-by-default): there is NO self-service bypass. For a genuine exception,
ASK the human, or request a one-time Telegram approval by setting
`RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE="<written justification>"` — the hook asks via a
trusted `tg-ctl` and allows ONLY on an explicit approval tap. A blank value or a bare `1`/`true`
is rejected (deny), and no Telegram call is made. An agent setting its own env var can request,
not self-grant — the human decides.

Contract (agents-hooks/v1):
  stdin  : JSON event; the dispatch payload is in args (subagent_type, isolation, prompt,
           description, run_in_background)
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": this is an orchestration-discipline boundary, not a security gate — a
crash here must never wedge the ability to dispatch work.
"""

from __future__ import annotations

import importlib.util
import json
import os
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

# A prompt is "trivial" (cheap to run inline) only if it is short AND single-line. Kept
# deliberately conservative: we block only a CLEARLY non-trivial foreground dispatch, so a
# false negative (a borderline one slipping through) is preferred to nagging on quick tasks.
TRIVIAL_MAX_CHARS = 200

# Per-harness delegation recipe named in the REMINDER (agent-tools#573): keyed by the
# bridge-set, unforgeable `event["harness"]` (never `args`). A missing/unrecognized harness
# falls back to Claude Code's recipe. SYNC: the same recipe family is duplicated in
# `agent-hooks/orchestrator-stays-thin` and `agent-hooks/no-long-inline-process` (each
# agent-hook is self-contained, no shared import across agent-hooks/ dirs).
_HARNESS_RECIPES = {
    "claude-code": (
        "Claude Code: subagent_type=\"fork\" or isolation=\"remote\" (both background per CC's "
        "tool contract), or a dynamic Workflow. run_in_background is NOT a real field on CC's "
        "Agent tool — it does nothing. For a truly detached child that outlives this session, use\n"
        "the canonical launcher ~/.agents/skills/rig-detached-claude-code/rig-detached-claude-code\n"
        "(RIG_AGENT_ID markers make the detached child session subagent-exempt)."
    ),
    "opencode": (
        "opencode: dispatch with subagent_type general or explore — allowed as-is. Its task tool\n"
        "has NO background field in a default build (1.18.20: only description/prompt/\n"
        "subagent_type/task_id/command) and runs in the foreground; tolerated because opencode\n"
        "has no in-tool background path — do not invent a background flag (the schema rejects\n"
        "it; the native background: true exists only behind\n"
        "OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true). For a truly detached child use the\n"
        "canonical launcher ~/.agents/skills/rig-detached-opencode/rig-detached-opencode (the\n"
        "rig-detached-opencode skill's provisioned copy — default skills target, `rig status`\n"
        "shows yours; RIG_AGENT_ID markers make the child session subagent-exempt)."
    ),
    "codex": (
        "codex: dispatch a `spawn_agent` call where your build has one (background per its own\n"
        "contract). On a default build with no such tool, use the canonical launcher\n"
        "~/.agents/skills/rig-detached-codex/rig-detached-codex (the rig-detached-codex skill's\n"
        "provisioned copy; RIG_AGENT_ID markers make the detached child session subagent-exempt)."
    ),
    "omp": (
        "omp: dispatch with the task tool — omp's own contract makes a non-blocking spawn a\n"
        "background AsyncJobManager job by default (docs/tools/task.md), so this is already the\n"
        "background shape. For a truly detached child outside a task-tool dispatch, use the\n"
        "canonical launcher ~/.agents/skills/rig-detached-omp/rig-detached-omp (RIG_AGENT_ID\n"
        "markers make the detached child session subagent-exempt)."
    ),
}


def _reminder_for(harness) -> str:
    """The BLOCK reminder text for `harness`, falling back to Claude Code's recipe."""
    recipe = _HARNESS_RECIPES.get(str(harness or "").strip(), _HARNESS_RECIPES["claude-code"])
    return (
        "Dispatch this subagent in the BACKGROUND — foreground blocks the main thread until "
        "it finishes.\n"
        f"{recipe}\n"
        "No self-service bypass — ask the human, or request one-time Telegram approval via "
        "RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE=\"<justification>\" (deny-by-default; bare "
        "\"1\" rejected)."
    )


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"background-subagent-gate: {msg}\n")


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent (agent_id present)."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    return bool(aid and str(aid).strip())


_OPENCODE_ASYNC_TYPES = frozenset({"general", "explore"})


def _is_background(args: dict, event: dict | None = None) -> bool:
    """True for any dispatch shape that is inherently — or explicitly marked — background.

    `fork` and `isolation: "remote"` are background by CC's own `Agent` tool contract, so they
    satisfy this gate's intent without any extra flag. This trusts that contract as-is (CC
    2.1.177); it is not independently re-verified here, so a future CC version that permits a
    non-background fork or a non-background `isolation: "remote"` would need this hook updated
    too — acceptable given on_error=open (an orchestration-discipline gate, not a security one).

    `run_in_background: true` is also honored — dead for a real CC dispatch, and for opencode
    live only in the experimental configuration (see the module docstring's "Allowed" list:
    the bridge maps the native `background` field only when the hosting opencode enables it).

    An explicit `run_in_background: false` (bool or string) ALWAYS wins and stays foreground —
    checked before the fork/remote/opencode inference, not after. A carrier that sends both a
    `fork`/`remote` shape AND an explicit `false` is telling us the shape's usual background
    guarantee does not apply this time; honoring the explicit signal over the inferred one is
    the documented contract (README: "still counts as foreground"). Confirmed as a real gap by
    review (2026-09-02, PR #499 GH-499 iteration): `{"subagent_type":"fork",
    "run_in_background":false}` was being allowed because the fork check ran first.

    opencode Task (`event["tool"] == "task"`, exact lowercase — CC uses `Agent`/`Task`) with
    `subagent_type` general or explore is allowed WITHOUT a flag: that tool's schema has no
    `background` field (a default build runs the task in the foreground — see the module
    docstring for why the gate tolerates that), so demanding one deadlocked every dispatch.
    """
    val = args.get("run_in_background")
    explicit: bool | None
    if isinstance(val, bool):
        explicit = val
    elif isinstance(val, str) and val.strip().lower() == "true":
        explicit = True
    elif isinstance(val, str) and val.strip().lower() == "false":
        explicit = False
    else:
        explicit = None

    if explicit is False:
        return False
    if args.get("subagent_type") == "fork":
        return True
    if args.get("isolation") == "remote":
        return True
    if explicit is True:
        return True
    tool = (event or {}).get("tool")
    kind = args.get("subagent_type")
    if tool == "task" and isinstance(kind, str) and kind.lower() in _OPENCODE_ASYNC_TYPES:
        return True
    return False


def _is_trivial(args: dict) -> bool:
    """Trivial (cheap to run inline) only if BOTH prompt and description are short AND
    single-line. Judging on only the FIRST non-empty of the two let a `prompt:"x"` + a long
    `description` slip through as trivial and run in the foreground — defeating the gate. So we
    judge on the LONGEST of the two and require neither to carry a newline (#6)."""
    texts = []
    for key in ("prompt", "description"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            texts.append(val)
    if not texts:
        # No prompt/description to judge → don't claim triviality; let the other checks decide.
        return False
    if any("\n" in t.strip() for t in texts):
        return False  # a multi-line prompt OR description is non-trivial
    return max(len(t) for t in texts) < TRIVIAL_MAX_CHARS


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # A subagent may itself fan out — this gate governs the orchestrator, not the workers.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}

    if _is_background(args, event):
        emit("allow")  # already the desired shape
        return 0

    if _is_trivial(args):
        emit("allow")  # cheap one-liner — inline is fine
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    reminder = _reminder_for(event.get("harness"))
    ctx = {"hook": "background-subagent-gate"}
    hatch = hatch_escalation.request_hatch_approval("background-subagent-gate", ctx, cwd=cwd)
    if hatch.should_stop:
        if hatch.approved:
            warn(f"background-subagent-gate allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        emit("block", f"hatch escalation denied: {hatch.reason}\n{reminder}")
        return BLOCK_EXIT_CODE

    emit("block", reminder)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
