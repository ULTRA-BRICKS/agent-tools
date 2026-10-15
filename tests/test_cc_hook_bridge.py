"""Clean-room proof that the agents-hooks/v1 → Claude Code bridge actually BLOCKS.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_cc_hook_bridge.py -q

The whole point of issue #18: agent-hook descriptors in ``~/.claude/hooks/*.json`` are
INERT in Claude Code (CC only runs ``settings.json`` PreToolUse/PostToolUse/Stop hooks).
This suite drives the bridge dispatcher in an isolated ``$HOME``, installs a real
agent-hook descriptor + script, feeds the dispatcher a CC tool-call event that the guard
should block, and asserts the dispatcher emits the CC block signal we confirmed against
the live docs (https://code.claude.com/docs/en/hooks, CC 2.1.177):

  - PreToolUse block  → exit 0 + ``hookSpecificOutput.permissionDecision == "deny"``
  - Stop block        → exit 0 + top-level ``decision == "block"``
  - benign call       → no deny (the tool proceeds through normal permission flow)

Every case runs the dispatcher as a SUBPROCESS (exactly how CC invokes it), so the test
exercises the real entrypoint + the real agent-hook scripts, not a mock.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cc_hook_bridge.dispatch as dispatch  # noqa: E402

# The shipped guard the orchestrator named: block-raw-pr-merge.
BLOCK_RAW_PR_MERGE = (
    _REPO / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
)
# The pre-agent guard: background-subagent-gate (blocks a foreground non-trivial dispatch).
BACKGROUND_SUBAGENT_GATE = (
    _REPO / "agent-hooks" / "background-subagent-gate" / "background_subagent_gate.py"
)
# The post-write pair: format-on-write reformats silently, lint-on-write feeds findings
# back as PostToolUse feedback.
FORMAT_ON_WRITE = _REPO / "agent-hooks" / "format-on-write" / "format_on_write.py"
LINT_ON_WRITE = _REPO / "agent-hooks" / "lint-on-write" / "lint_on_write.py"
# The pre-skill guard: skills-marker-writer (touches the skills-read-gate freshness marker).
SKILLS_MARKER_WRITER = (
    _REPO / "agent-hooks" / "skills-marker-writer" / "skills_marker_writer.py"
)
# The pre-monitor guard: subagent-no-monitor (blocks a subagent's Monitor tool call outright).
SUBAGENT_NO_MONITOR = (
    _REPO / "agent-hooks" / "subagent-no-monitor" / "subagent_no_monitor.py"
)
# The pre-workflow guard: workflow-danger-gate (blocks destructive Workflow scripts).
WORKFLOW_DANGER_GATE = (
    _REPO / "agent-hooks" / "workflow-danger-gate" / "workflow_danger_gate.py"
)
# orchestrator-stays-thin (pre-bash half): the gate at the center of agent-tools#676 — a
# nested omp Task/eval subagent's PI_SESSION_FILE must exempt it regardless of which harness
# (`claude-code`, `codex`, `opencode`) actually fired the tool-call event.
ORCHESTRATOR_STAYS_THIN = (
    _REPO / "agent-hooks" / "orchestrator-stays-thin" / "orchestrator_stays_thin.py"
)

# The pre-worktree-enter grant: enterworktree-canonical-path-grant (rig-cli#418; suppresses
# CC's interactive prompt only for a target under <repo>/.worktrees/<name>).
ENTERWORKTREE_CANONICAL_PATH_GRANT = (
    _REPO / "agent-hooks" / "enterworktree-canonical-path-grant"
    / "enterworktree_canonical_path_grant.py"
)


@pytest.fixture(autouse=True)
def _no_host_agent_markers(monkeypatch):
    """Module-wide: a rig-dispatched test run (this process) may carry RIG_AGENT_ID /
    RIG_DETACHED_AGENT, which the bridge now honours (agent-tools#831); strip them for every
    test so in-process `to_v1_event` asserts and `_run_dispatch` subprocesses (which copy
    os.environ) never see an injected agent_id. Tests that want a marker set it explicitly."""
    monkeypatch.delenv("RIG_AGENT_ID", raising=False)
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)


def _install_descriptor(hooks_dir: Path, *, hook_id: str, point: str, cmd: Path,
                        on_error: str = "closed", priority: int = 10) -> Path:
    """Write a `<id>.<point>.json` descriptor with an absolute `cmd`, as rig installs it."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "id": hook_id,
        "point": point,
        "cmd": str(cmd),
        "priority": priority,
        "timeout_ms": 3000,
        "on_error": on_error,
        "description": f"test descriptor for {hook_id}",
    }
    path = hooks_dir / f"{hook_id}.{point}.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


def _run_dispatch(event: str, cc_event: dict, *, home: Path) -> subprocess.CompletedProcess:
    """Invoke the dispatcher exactly as CC's settings.json would: `python -m cc_hook_bridge <event>`.

    Strips every ambient `RIG_HATCH_REQUEST_*` var before launching. This subprocess crosses
    a real process boundary, so conftest's `pwd.getpwuid`/`resolve_home` monkeypatches (which
    only patch the CURRENT interpreter) never reach it — a hatch-using hook driven through here
    (e.g. subagent-no-monitor's clean-room test) would resolve the REAL OS home and could find a
    real `tg-ctl` in `_TRUSTED_TG_CTL_PATHS`. If the developer running pytest happens to have any
    `RIG_HATCH_REQUEST_*` var exported in their shell (the documented way to arm a hatch — "set
    in the process environment before the tool call"), an un-stripped env would let a routine
    test run fire a REAL Telegram approval request at a human, then hang up to the hatch's ~900s
    cap and blow this call's 30s timeout. Centralized here (not per-test) so every current and
    future clean-room hatch test run through this helper is covered, not just one call site.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    env["HOME"] = str(home)
    # Hermetic file log: bridge diagnostics must never touch process stderr (TUI
    # noise), so they go to this file; default it into the test sandbox unless
    # the caller overrides it explicitly.
    env.setdefault("HOOKS_DEBUG_LOG", str(home / "hooks-debug.log"))
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cc_hook_bridge", event],
        input=json.dumps(cc_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ── pure mapping unit tests (fast, no subprocess) ─────────────────────────────────────

def test_point_for_event_maps_tool_to_logical_point():
    assert dispatch.point_for_event("PreToolUse", "Bash") == "pre-bash"
    assert dispatch.point_for_event("PreToolUse", "Write") == "pre-write"
    assert dispatch.point_for_event("PreToolUse", "Edit") == "pre-write"
    assert dispatch.point_for_event("PreToolUse", "MultiEdit") == "pre-write"
    assert dispatch.point_for_event("PreToolUse", "NotebookEdit") == "pre-write"
    # the subagent-dispatch tools map to the new pre-agent point (CC calls them Agent/Task)
    assert dispatch.point_for_event("PreToolUse", "Agent") == "pre-agent"
    assert dispatch.point_for_event("PreToolUse", "Task") == "pre-agent"
    # invoking a skill maps to pre-skill (the skills-invoked marker-writer point)
    assert dispatch.point_for_event("PreToolUse", "Skill") == "pre-skill"
    # calling Monitor maps to pre-monitor (the subagent-no-monitor point)
    assert dispatch.point_for_event("PreToolUse", "Monitor") == "pre-monitor"
    # Workflow (ultracode) maps to pre-workflow so workflow-danger-gate can inspect the script
    assert dispatch.point_for_event("PreToolUse", "Workflow") == "pre-workflow"
    assert dispatch.point_for_event("Stop", None) == "stop"
    # a COMPLETED write maps to the reactive post-write point (format-on-write, lint-on-write)
    assert dispatch.point_for_event("PostToolUse", "Write") == "post-write"
    assert dispatch.point_for_event("PostToolUse", "Edit") == "post-write"
    assert dispatch.point_for_event("PostToolUse", "MultiEdit") == "post-write"
    assert dispatch.point_for_event("PostToolUse", "NotebookEdit") == "post-write"
    # PostToolUse on a non-write tool has no logical point — pre-bash guards must never
    # re-fire after the command already ran
    assert dispatch.point_for_event("PostToolUse", "Bash") is None
    assert dispatch.point_for_event("PostToolUse", "Read") is None
    # an unmapped tool (e.g. Read) on PreToolUse has no logical point → nothing fires
    assert dispatch.point_for_event("PreToolUse", "Read") is None


def test_to_v1_event_forwards_agent_id_and_type_for_pre_agent():
    """The subagent signal (agent_id/agent_type) must be surfaced under args so a
    subagent-exempt gate can tell a subagent's tool use apart from the main thread's."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing", "run_in_background": False},
          "agent_id": "sub-42", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert v1["point"] == "pre-agent"
    assert v1["args"]["agent_id"] == "sub-42"
    assert v1["args"]["agent_type"] == "general-purpose"
    # the dispatch payload (incl. run_in_background) rides along in args from tool_input
    assert v1["args"]["run_in_background"] is False
    assert v1["args"]["prompt"] == "do the thing"


def test_to_v1_event_tags_harness_claude_code():
    """agent-tools#533: every v1 event this bridge produces carries the top-level `harness`
    tag `"claude-code"`, unconditionally — set from the bridge's own module constant, never
    from `cc_event`. A forgeable `tool_input.harness` (if a hook ever misread `args`) must not
    override it."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "ls", "harness": "codex"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["harness"] == "claude-code"
    assert dispatch.HARNESS == "claude-code"


def test_to_v1_event_main_thread_has_no_agent_id():
    """A main-thread Agent dispatch carries NO agent_id (the signal is absent) → the gate
    treats it as the orchestrator, not a subagent."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_top_level_agent_id_overrides_tool_input():
    """CC's TOP-LEVEL agent_id is authoritative: a (possibly stale/forged) tool_input.agent_id
    must be OVERWRITTEN by the real top-level value, not win over it (T2)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing", "agent_id": "forged", "agent_type": "x"},
          "agent_id": "real-sub", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert v1["args"]["agent_id"] == "real-sub"
    assert v1["args"]["agent_type"] == "general-purpose"


def test_to_v1_event_forged_tool_input_agent_id_does_not_exempt():
    """A forged tool_input.agent_id with NO top-level signal must be DROPPED — a main-thread
    dispatch that injects agent_id into tool_input must NOT exempt itself (T2)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing", "agent_id": "forged", "agent_type": "forged-t"},
          "cwd": "/repo"}  # ← no top-level agent_id/agent_type
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert "agent_id" not in v1["args"]
    assert "agent_type" not in v1["args"]


def test_to_v1_event_forwards_agent_id_for_pre_bash():
    """The subagent signal must ride through for pre-bash too, not only pre-agent — so
    no-long-inline-process / orchestrator-stays-thin receive it for a subagent's Bash (T5)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test"},
          "agent_id": "sub-7", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["args"]["agent_id"] == "sub-7"
    assert v1["args"]["agent_type"] == "general-purpose"


def test_to_v1_event_forged_tool_input_agent_id_dropped_for_pre_bash():
    """The same forged-signal protection applies to pre-bash: a forged tool_input.agent_id on a
    main-thread Bash (no top-level signal) must NOT exempt it from the inline-process gate."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "review", "agent_id": "forged"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_forged_tool_input_agent_id_dropped_for_pre_write():
    """The forged-signal protection must also hold for pre-WRITE: an Edit carrying
    `agent_id:"forged"` in tool_input with NO top-level signal must have it DROPPED, so
    orchestrator-stays-thin still judges it as a main-thread code write (#7)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
          "tool_input": {"file_path": "/repo/src/a.ts", "old_string": "a",
                         "new_string": "b", "agent_id": "forged", "agent_type": "forged-t"},
          "cwd": "/repo"}  # ← no top-level agent_id/agent_type
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert "agent_id" not in v1["args"]
    assert "agent_type" not in v1["args"]


# ── detached-launcher identity (agent-tools#831): RIG_AGENT_ID / RIG_DETACHED_AGENT ─────────
# `rig-detached-claude-code` starts `claude -p` with these markers in its process env; CC runs
# hook commands with that env, so the bridge sees them on every call and must restore
# `args.agent_id` from them when CC itself sends no top-level agent_id — the same
# `_detached_agent_id` contract as the codex/omp/opencode bridges.

def test_to_v1_event_injects_agent_id_from_rig_agent_id(monkeypatch):
    monkeypatch.setenv("RIG_AGENT_ID", "LauncherChild")
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["args"]["agent_id"] == "LauncherChild"


def test_to_v1_event_anonymous_detached_marker_injects_detached(monkeypatch):
    monkeypatch.setenv("RIG_DETACHED_AGENT", "1")
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
          "tool_input": {"prompt": "do the thing"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-agent")
    assert v1["args"]["agent_id"] == "detached"


def test_to_v1_event_blank_markers_are_not_an_identity(monkeypatch):
    monkeypatch.setenv("RIG_AGENT_ID", "   ")
    monkeypatch.setenv("RIG_DETACHED_AGENT", "true")  # only the exact value 1 is a marker
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_cc_top_level_agent_id_wins_over_the_launcher_marker(monkeypatch):
    """A native CC subagent INSIDE a detached session keeps CC's own, more specific id."""
    monkeypatch.setenv("RIG_AGENT_ID", "LauncherChild")
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test"},
          "agent_id": "sub-7", "agent_type": "general-purpose", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["args"]["agent_id"] == "sub-7"
    assert v1["args"]["agent_type"] == "general-purpose"


def test_to_v1_event_forged_tool_input_agent_id_never_beats_the_marker(monkeypatch):
    """The forged copy is still dropped; the injected value is the launcher's, not the model's."""
    monkeypatch.setenv("RIG_AGENT_ID", "LauncherChild")
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test", "agent_id": "forged", "agent_type": "forged-t"},
          "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["args"]["agent_id"] == "LauncherChild"
    assert "agent_type" not in v1["args"]


def test_to_v1_event_top_level_session_id_overrides_tool_input():
    """session_id gets the SAME T2 precedence as agent_id: CC's TOP-LEVEL session id is
    authoritative, a (possibly forged) tool_input.session_id must be OVERWRITTEN, not win
    over it. This matters for any hook that uses args.session_id to SCOPE a decision
    (skills-read-gate's freshness marker), not just record it — a forged value could
    otherwise let one session borrow another session's fresh marker."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Skill",
          "tool_input": {"skill": "delegate-work-to-subagents", "session_id": "forged"},
          "session_id": "real-session", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-skill")
    assert v1["args"]["session_id"] == "real-session"


def test_to_v1_event_forged_tool_input_session_id_dropped_when_top_level_absent():
    """A forged tool_input.session_id with NO top-level session id must be DROPPED, not
    passed through — mirrors the agent_id forged-signal protection (T2)."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Skill",
          "tool_input": {"skill": "delegate-work-to-subagents", "session_id": "forged"},
          "cwd": "/repo"}  # ← no top-level session_id
    v1 = dispatch.to_v1_event(cc, point="pre-skill")
    assert "session_id" not in v1["args"]


def test_to_v1_event_forwards_session_id_for_stop():
    """The pre-existing, narrower use of session_id (a stop hook keying its own marker) must
    keep working under the now-unified T2 loop: `stop` has no tool_input, so args starts
    empty and the top-level session id lands in args unconditionally."""
    cc = {"hook_event_name": "Stop", "stop_hook_active": True,
          "session_id": "sess-xyz", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="stop")
    assert v1["args"]["session_id"] == "sess-xyz"


def test_to_v1_event_forwards_transcript_path_for_stop():
    """stop-completion-selfcheck reads this to classify what the turn actually did instead
    of firing the same static prompt regardless of content — must round-trip unchanged."""
    cc = {"hook_event_name": "Stop", "stop_hook_active": True,
          "session_id": "sess-xyz", "cwd": "/repo",
          "transcript_path": "/Users/x/.claude/projects/p/sess-xyz.jsonl"}
    v1 = dispatch.to_v1_event(cc, point="stop")
    assert v1["transcript_path"] == "/Users/x/.claude/projects/p/sess-xyz.jsonl"


def test_to_v1_event_transcript_path_defaults_to_empty_string_when_absent():
    cc = {"hook_event_name": "Stop", "stop_hook_active": True,
          "session_id": "sess-xyz", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="stop")
    assert v1["transcript_path"] == ""


def test_to_v1_event_empty_string_top_level_session_id_overwrites_tool_input():
    """The T2 loop tests identity (`is not None`), not truthiness, so a top-level
    session_id == "" now OVERWRITES a tool_input copy too (the old standalone rule used
    truthiness and would have skipped this case). Harmless downstream — an empty string
    fails `_sanitize_session_id` on both hooks and falls back to the global marker path —
    but pin the T2 loop's actual behavior explicitly since it now gates a real decision."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Skill",
          "tool_input": {"skill": "delegate-work-to-subagents", "session_id": "tool-input-copy"},
          "session_id": "", "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-skill")
    assert v1["args"]["session_id"] == ""


def test_to_v1_event_emits_the_point_for_each_logical_point():
    """`point` must round-trip into the v1 event for every point. orchestrator-stays-thin reads
    `event.get("point")`; if the bridge ever stopped setting it the hook would silently
    downgrade to allow. Lock it for pre-bash, pre-write, and pre-agent (#10)."""
    cases = [
        ("pre-bash", "Bash", {"command": "npm test"}),
        ("pre-write", "Write", {"file_path": "/repo/x.py", "content": "x"}),
        ("pre-agent", "Agent", {"prompt": "do the thing"}),
        ("pre-workflow", "Workflow", {"script": "return 1;"}),
    ]
    for point, tool, tool_input in cases:
        cc = {"hook_event_name": "PreToolUse", "tool_name": tool,
              "tool_input": tool_input, "cwd": "/repo"}
        v1 = dispatch.to_v1_event(cc, point=point)
        assert v1["point"] == point


def test_to_v1_event_carries_the_bash_command():
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "gh pr merge 5 --admin"}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(cc, point="pre-bash")
    assert v1["hook_api"] == "agents-hooks/v1"
    assert v1["point"] == "pre-bash"
    assert v1["tool"] == "Bash"
    assert v1["args"]["command"] == "gh pr merge 5 --admin"
    assert v1["cwd"] == "/repo"


def test_to_v1_event_carries_write_path_and_content():
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "/repo/x.py", "content": "API_KEY=..."}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert v1["args"]["file_path"] == "/repo/x.py"
    assert v1["args"]["content"] == "API_KEY=..."
    # non-Bash tool → the top-level `command` fallback is empty (locks the bash-only contract)
    assert v1["command"] == ""


def test_to_v1_event_normalizes_multiedit_into_content():
    """A MultiEdit's edits[].new_string must surface in args.content so flat-content
    pre-write hooks (block-secrets-write) can scan it — else a secret slips through."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "MultiEdit",
          "tool_input": {"file_path": "/repo/x.py",
                         "edits": [{"old_string": "a", "new_string": "TOKEN=sk-live-xxxx"},
                                   {"old_string": "b", "new_string": "harmless"}]}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert "TOKEN=sk-live-xxxx" in v1["args"]["content"]
    assert "harmless" in v1["args"]["content"]


def test_to_v1_event_normalizes_notebook_new_source():
    cc = {"hook_event_name": "PreToolUse", "tool_name": "NotebookEdit",
          "tool_input": {"notebook_path": "/n.ipynb", "new_source": "SECRET=abc123"}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert "SECRET=abc123" in v1["args"]["content"]
    # The NotebookEdit `notebook_path` must alias onto the standard path fields the shipped
    # pre-write hooks scope on — else path-scoped raw-env/secret-scan checks see an empty path
    # and wave the write through (guard bypass).
    assert v1["args"]["file_path"] == "/n.ipynb"
    assert v1["args"]["path"] == "/n.ipynb"


def test_to_v1_event_does_not_clobber_explicit_path():
    """A tool that already provides file_path keeps it; the notebook alias only FILLS a gap."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "/real.py", "notebook_path": "/wrong.ipynb", "content": "x"}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert v1["args"]["file_path"] == "/real.py"


def test_to_v1_event_preserves_explicit_content_over_normalization():
    """When the tool already provides a string `content` (Write), keep it verbatim."""
    cc = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "/x", "content": "exact", "edits": [{"new_string": "other"}]}}
    v1 = dispatch.to_v1_event(cc, point="pre-write")
    assert v1["args"]["content"] == "exact"


def test_cc_block_output_pretooluse_is_permission_deny():
    out = dispatch.cc_block_output("PreToolUse", "nope")
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


def test_cc_block_output_stop_is_decision_block():
    out = dispatch.cc_block_output("Stop", "stay")
    assert out["decision"] == "block"
    assert out["reason"] == "stay"


def test_cc_block_output_posttooluse_is_decision_block_feedback():
    """PostToolUse cannot deny a tool that already ran — its block shape is the FEEDBACK
    form {decision, reason} (CC surfaces the reason to the model). A permissionDecision
    here would be silently ignored by CC, turning lint feedback into a no-op."""
    out = dispatch.cc_block_output("PostToolUse", "lint errors in a.ts")
    assert out == {"decision": "block", "reason": "lint errors in a.ts"}
    assert "hookSpecificOutput" not in out


def test_to_v1_event_post_write_normalizes_notebook_path():
    """post-write hooks resolve the written file from args.file_path/path — a NotebookEdit
    must get the same path aliasing pre-write gets, or notebooks are never formatted/linted."""
    cc = {"hook_event_name": "PostToolUse", "tool_name": "NotebookEdit",
          "tool_input": {"notebook_path": "/n.ipynb", "new_source": "x=1"}}
    v1 = dispatch.to_v1_event(cc, point="post-write")
    assert v1["point"] == "post-write"
    assert v1["args"]["file_path"] == "/n.ipynb"
    assert v1["args"]["path"] == "/n.ipynb"
    # …but NO content normalization post-write: the content is already ON DISK, and a
    # synthesized args.content (from new_source/edits) would tempt post-write hooks into
    # scanning the proposed text instead of the real file. Guards the pre-write/post-write
    # split in to_v1_event.
    assert "content" not in v1["args"]


# ── clean-room subprocess proof: a guard BLOCKS, a benign call PASSES ──────────────────

def test_real_guard_blocks_raw_pr_merge(tmp_path):
    """block-raw-pr-merge installed as a CC PreToolUse hook actually DENIES a raw merge."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="block-raw-pr-merge", point="pre-bash",
                        cmd=BLOCK_RAW_PR_MERGE, on_error="closed")

    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 42 --admin"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert "gh pr merge" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "ship" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_real_guard_passes_benign_bash(tmp_path):
    """The same guard installed, but a benign `gh ship` is NOT blocked."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="block-raw-pr-merge", point="pre-bash",
                        cmd=BLOCK_RAW_PR_MERGE, on_error="closed")

    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh ship 42"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)

    assert proc.returncode == 0, proc.stderr
    # benign → either empty stdout (allow) or a decision that is NOT deny
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision != "deny", out


# ── clean-room proof: the pre-agent point routes + the subagent signal is honored ─────────

def test_pre_agent_foreground_dispatch_is_blocked(tmp_path):
    """An `Agent` PreToolUse with no run_in_background routes to a pre-agent descriptor and
    BLOCKS (the orchestrator must dispatch non-trivial subagents in the background)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        # a clearly non-trivial single-line prompt (> 200 chars) run in the FOREGROUND
        "tool_input": {"prompt": "implement the feature: " + "x" * 220},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert "BACKGROUND" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_agent_cc_task_explore_foreground_still_blocked(tmp_path):
    """CC's Task tool name is `Task` (capital T). The opencode allow is keyed on exact
    lowercase `task`, so a CC Explore dispatch through the real bridge must still BLOCK
    — if the bridge ever lowercased tool_name, this would start allowing."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"subagent_type": "Explore", "prompt": "implement the feature: " + "x" * 220},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_pre_agent_background_dispatch_passes(tmp_path):
    """The same gate, but a `run_in_background: true` dispatch is NOT blocked."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": "implement the feature: " + "x" * 220, "run_in_background": True},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


def test_pre_agent_subagent_signal_forwarded_exempts(tmp_path):
    """A foreground dispatch made INSIDE a subagent (agent_id present) must be ALLOWED — proving
    the bridge forwards agent_id and the subagent-exempt gate reads it."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"prompt": "implement the feature: " + "x" * 220},
        "agent_id": "sub-99",  # ← the subagent signal CC adds inside a dispatched agent
        "agent_type": "general-purpose",
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


def test_forged_tool_input_agent_id_does_not_exempt_clean_room(tmp_path):
    """End-to-end (T2): a main-thread foreground dispatch that injects `agent_id` INTO
    tool_input — with NO top-level CC agent signal — must STILL be BLOCKED. The bridge drops
    the forged signal, so the subagent-exempt gate does not let the orchestrator off the hook."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        # forged agent_id inside tool_input; CC top-level carries NO agent_id (main thread)
        "tool_input": {"prompt": "implement the feature: " + "x" * 220, "agent_id": "forged"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_detached_launcher_child_foreground_dispatch_is_subagent_exempt_clean_room(tmp_path):
    """End-to-end (agent-tools#831): a `claude -p` started by rig-detached-claude-code carries
    RIG_AGENT_ID in the process env the REAL bridge subprocess inherits, and CC sends no
    top-level agent_id (it is CC's main thread). The same non-trivial foreground dispatch the
    forged test above DENIES must be ALLOWED here: the bridge injects the launcher identity and
    the REAL background-subagent-gate treats the detached child as a subagent, not the
    orchestrator. Would FAIL before the fix (the bridge ignored the markers)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="background-subagent-gate", point="pre-agent",
                        cmd=BACKGROUND_SUBAGENT_GATE, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": "implement the feature: " + "x" * 220},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch_with_env("PreToolUse", cc_event, home=home,
                                  extra_env={"RIG_AGENT_ID": "sysmon-remediate-x"})
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


def test_detached_launcher_child_monitor_is_denied_like_a_subagent_clean_room(tmp_path):
    """The inverse direction: subagent-only gates must SEE the detached child as a subagent
    too. An anonymous `RIG_DETACHED_AGENT=1` child calling Monitor is DENIED by the REAL
    subagent-no-monitor (a one-shot `claude -p` exits at end of turn, so nothing would ever
    receive the Monitor notification). Would FAIL before the fix (allowed as orchestrator)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="subagent-no-monitor", point="pre-monitor",
                        cmd=SUBAGENT_NO_MONITOR, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Monitor",
        "tool_input": {"description": "watch my background test run", "timeout_ms": 300000,
                       "persistent": False},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch_with_env("PreToolUse", cc_event, home=home,
                                  extra_env={"RIG_DETACHED_AGENT": "1"})
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_pre_skill_writes_the_skills_read_gate_marker_clean_room(tmp_path):
    """End-to-end: a `Skill` PreToolUse event, driven through the REAL bridge subprocess +
    the REAL skills-marker-writer script (not called in-process), touches the marker
    skills-read-gate's freshness check reads — proving the whole caller-to-callee path:
    CC event -> bridge to_v1_event() -> descriptor loading -> subprocess hook ->
    $HOME/.cache/agent-tools/skills-invoked/<skill>."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="skills-marker-writer", point="pre-skill",
                        cmd=SKILLS_MARKER_WRITER, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Skill",
        "tool_input": {"skill": "delegate-work-to-subagents"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out
    marker = home / ".cache" / "agent-tools" / "skills-invoked" / "delegate-work-to-subagents"
    assert marker.is_file(), "skills-marker-writer did not touch the freshness marker"


def test_pre_skill_marker_is_session_scoped_clean_room(tmp_path):
    """Same end-to-end path as above, but with a CC session id on the event — the real
    subprocess hook must nest the marker under it (session-scoping), and a forged
    tool_input.session_id must not override CC's own top-level value."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="skills-marker-writer", point="pre-skill",
                        cmd=SKILLS_MARKER_WRITER, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Skill",
        "tool_input": {"skill": "visual-proof-cycle", "session_id": "forged"},
        "session_id": "sess-real",
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    marker = home / ".cache" / "agent-tools" / "skills-invoked" / "sess-real" / "visual-proof-cycle"
    assert marker.is_file(), "marker was not written under the real (top-level) session id"
    forged = home / ".cache" / "agent-tools" / "skills-invoked" / "forged" / "visual-proof-cycle"
    assert not forged.exists(), "a forged tool_input.session_id must not have been used"


def test_pre_monitor_subagent_call_is_blocked_clean_room(tmp_path):
    """End-to-end: a `Monitor` PreToolUse event WITH agent_id (a dispatched subagent), driven
    through the REAL bridge subprocess + the REAL subagent-no-monitor script, is DENIED — the
    fix for HYP-1350's retrospective (a subagent Monitor-ing its own child then wedging on a
    notification only the main loop receives)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="subagent-no-monitor", point="pre-monitor",
                        cmd=SUBAGENT_NO_MONITOR, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Monitor",
        "tool_input": {"description": "watch my background test run", "timeout_ms": 300000,
                        "persistent": False},
        "agent_id": "sub-77",
        "agent_type": "general-purpose",
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert "Monitor" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_monitor_orchestrator_call_passes_clean_room(tmp_path):
    """The same descriptor, but the SAME event with NO agent_id (the top-level orchestrator
    watching a backgrounded subagent) must NOT be denied — its Monitor use is legitimate."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="subagent-no-monitor", point="pre-monitor",
                        cmd=SUBAGENT_NO_MONITOR, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Monitor",
        "tool_input": {"description": "watch a dispatched subagent", "timeout_ms": 300000,
                        "persistent": False},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out


def test_pre_monitor_forged_tool_input_agent_id_does_not_deny_orchestrator_clean_room(tmp_path):
    """End-to-end, the INVERTED direction from the forged-agent_id test above: the orchestrator's
    own legitimate Monitor call (no top-level CC agent_id) must NOT be denied even when
    `tool_input` happens to carry an agent_id-shaped field — the bridge's T2 drop loop
    (`dispatch.py`) must not let a forged/incidental `tool_input.agent_id` fool the gate into
    treating the orchestrator as a subagent. Symmetric coverage to
    `test_forged_tool_input_agent_id_does_not_exempt_clean_room`, which proves the drop in the
    exempt direction; this proves it in the deny-availability direction for pre-monitor."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="subagent-no-monitor", point="pre-monitor",
                        cmd=SUBAGENT_NO_MONITOR, on_error="open")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Monitor",
        "tool_input": {"description": "watch a dispatched subagent", "timeout_ms": 300000,
                        "persistent": False, "agent_id": "forged"},
        # no top-level agent_id — the real CC signal for a main-thread orchestrator call
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out
def test_pre_workflow_dangerous_script_is_blocked(tmp_path):
    """A Workflow PreToolUse whose script runs `gh pr merge` routes to pre-workflow
    and DENIES — the harness allow for Workflow is not a blank cheque."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="workflow-danger-gate", point="pre-workflow",
                        cmd=WORKFLOW_DANGER_GATE, on_error="closed")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Workflow",
        "tool_input": {"script": "await $`gh pr merge 12 --admin`;"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "raw-pr-merge" in reason


def test_pre_workflow_ultracode_script_passes(tmp_path):
    """The same gate, but an ultracode agent() dispatch (even one whose prompt
    *mentions* gh pr merge) is NOT blocked."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="workflow-danger-gate", point="pre-workflow",
                        cmd=WORKFLOW_DANGER_GATE, on_error="closed")
    cc_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Workflow",
        "tool_input": {
            "script": "return await agent({ prompt: 'never gh pr merge; use gh ship' });"
        },
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", out




def test_unmatched_tool_does_not_run_pre_bash_hook(tmp_path):
    """A pre-bash hook must NOT fire on a Read (no logical point) — selection by point."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="block-raw-pr-merge", point="pre-bash",
                        cmd=BLOCK_RAW_PR_MERGE, on_error="closed")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Read",
                "tool_input": {"file_path": "/x"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_no_descriptors_is_allow(tmp_path):
    """Empty hooks dir (or none) → dispatcher is a clean no-op (fail-open)."""
    home = tmp_path / "home"
    (home / ".claude" / "hooks").mkdir(parents=True)
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "gh pr merge 1 --admin"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    # nothing installed → no deny
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_dispatch_fails_open_when_shared_runner_import_fails(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "agent_hooks_v1":
            raise ImportError("missing runner")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = dispatch.dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 42 --admin"},
            "cwd": "/repo",
        },
    )

    assert result is None


def test_posttooluse_postwrite_block_uses_decision_feedback(tmp_path):
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    blocker = tmp_path / "post_write_blocker.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "assert event['point'] == 'post-write'\n"
        "print(json.dumps({'message': 'post-write feedback'}))\n"
        "sys.exit(10)\n",
        encoding="utf-8",
    )
    blocker.chmod(0o755)
    _install_descriptor(
        hooks,
        hook_id="post-write-blocker",
        point="post-write",
        cmd=blocker,
        on_error="closed",
    )
    cc_event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "x.py")},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("PostToolUse", cc_event, home=home)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "decision": "block",
        "reason": "post-write feedback",
    }


def test_fail_open_on_garbage_stdin(tmp_path):
    """A broken/garbage stdin must NOT wedge every tool call — dispatcher fails OPEN."""
    home = tmp_path / "home"
    (home / ".claude" / "hooks").mkdir(parents=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "cc_hook_bridge", "PreToolUse"],
        input="this is not json{{{",
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0
    # no deny emitted
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_hook_error_fail_closed_blocks(tmp_path):
    """A hook that ERRORS (non-0/non-10 exit) with on_error=closed must DENY the call."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    # a script that always crashes (exit 1) — simulates a broken security gate
    crasher = tmp_path / "crasher.py"
    crasher.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    crasher.chmod(0o755)
    _install_descriptor(hooks, hook_id="crasher", point="pre-bash",
                        cmd=crasher, on_error="closed")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_bad_timeout_ms_on_closed_gate_blocks(tmp_path):
    """A fail-closed gate with a non-numeric timeout_ms must DENY (descriptor error), not be
    silently skipped by the dispatcher's fail-open."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "broken-timeout", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
            "priority": 10, "timeout_ms": "5s", "on_error": "closed"}
    (hooks / "broken-timeout.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_non_utf8_block_output_on_OPEN_gate_still_blocks(tmp_path):
    """A hook that emits NON-UTF-8 bytes AND a deliberate block (exit 10) must BLOCK even on a
    fail-OPEN gate: the decode must not raise (errors='replace') and turn a real block into a
    hook error → on_error=open → allow. on_error='open' isolates this from the fail-closed
    path (where any error denies anyway). Regression: #18 / non-UTF-8 fail-open."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    blocker = tmp_path / "binary_blocker.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
        "sys.stdout.flush()\n"
        "sys.exit(10)\n"
    )
    blocker.chmod(0o755)
    _install_descriptor(hooks, hook_id="binblk", point="pre-bash", cmd=blocker, on_error="open")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_negative_timeout_ms_on_closed_gate_blocks(tmp_path):
    """A NEGATIVE timeout_ms is a descriptor typo (not 'unset') → a fail-closed gate must DENY,
    consistent with how a non-numeric timeout_ms is handled."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "neg-timeout", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
            "priority": 10, "timeout_ms": -100, "on_error": "closed"}
    (hooks / "neg-timeout.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


@pytest.mark.parametrize("bad", [True, False])
def test_bool_timeout_ms_on_closed_gate_blocks(tmp_path, bad):
    """A BOOLEAN timeout_ms is a descriptor typo, not a number — bool is an int subclass, so
    int(True)==1 / int(False)==0 would otherwise sneak past as a 1 ms / 'unset' timeout. A
    fail-closed gate must DENY, like any other bad descriptor field."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "bool-timeout", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
            "priority": 10, "timeout_ms": bad, "on_error": "closed"}
    (hooks / "bool-timeout.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def _benign_exit0(tmp_path: Path) -> Path:
    script = tmp_path / "benign.py"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("timeout_ms", [0, None])
def test_zero_or_null_timeout_ms_uses_default_not_a_spurious_timeout(tmp_path, timeout_ms):
    """timeout_ms of 0 (or null) means 'unset' → the default, NOT a 1 ms floor that would make
    every such hook spuriously time out and, on a fail-closed gate, block a benign call."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    spec = {"id": "fast", "point": "pre-bash", "cmd": str(_benign_exit0(tmp_path)),
            "priority": 10, "timeout_ms": timeout_ms, "on_error": "closed"}
    (hooks / "fast.pre-bash.json").write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    assert "deny" not in proc.stdout  # benign call allowed, not a spurious timeout-block


def test_bad_priority_does_not_crash_dispatch(tmp_path):
    """A non-numeric priority must NOT bubble to the top-level fail-open and skip OTHER hooks
    (including a fail-closed gate that should block)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    # one descriptor has a garbage priority; the real guard (good priority) must still block.
    bad = {"id": "bad-prio", "point": "pre-bash", "cmd": str(BLOCK_RAW_PR_MERGE),
           "priority": "high", "on_error": "open"}
    (hooks / "bad-prio.pre-bash.json").write_text(json.dumps(bad))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "gh pr merge 9 --admin"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out


def test_hook_error_fail_open_allows(tmp_path):
    """A hook that ERRORS with on_error=open must NOT block (advisory hook stays out of the way)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    crasher = tmp_path / "crasher.py"
    crasher.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    crasher.chmod(0o755)
    _install_descriptor(hooks, hook_id="crasher", point="pre-bash",
                        cmd=crasher, on_error="open")
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_stop_block_emits_decision_block(tmp_path):
    """A stop hook that exit-10 blocks → CC top-level decision:block (different from PreToolUse)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    # a minimal stop hook that always blocks once with exit 10
    blocker = tmp_path / "stopper.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "sys.stdout.write(json.dumps({'hook_api':'agents-hooks/v1','decision':'block','message':'finish the checklist'}))\n"
        "sys.exit(10)\n"
    )
    blocker.chmod(0o755)
    _install_descriptor(hooks, hook_id="stopper", point="stop",
                        cmd=blocker, on_error="open")
    cc_event = {"hook_event_name": "Stop", "stop_hook_active": True, "cwd": str(tmp_path)}
    proc = _run_dispatch("Stop", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "checklist" in out["reason"]


def test_first_block_wins_collects_reason(tmp_path):
    """Two pre-bash guards, both would block → first (by priority) wins, its reason surfaces."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    mk = tmp_path / "mk.py"
    mk.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "tag=sys.argv[1]\n"
        "sys.stdout.write(json.dumps({'hook_api':'agents-hooks/v1','decision':'block','message':tag}))\n"
        "sys.exit(10)\n"
    )
    mk.chmod(0o755)
    # priority 5 runs before priority 50 → "first" reason wins
    d1 = _install_descriptor(hooks, hook_id="first", point="pre-bash", cmd=mk, priority=5)
    d2 = _install_descriptor(hooks, hook_id="second", point="pre-bash", cmd=mk, priority=50)
    # patch args so each prints its own tag
    for path, tag in ((d1, "first-reason"), (d2, "second-reason")):
        spec = json.loads(path.read_text())
        spec["args"] = [tag]
        path.write_text(json.dumps(spec))
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "anything"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PreToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "first-reason" in reason, out


def test_multiple_hatch_vars_in_one_invocation_are_all_audited(tmp_path):
    """Regression for agent-tools#713: overrides.log (the RIG_HATCH_REQUEST_* audit sink,
    gap G-8) must record EVERY hatch attempt made during one dispatch, not just the first.

    Two pre-bash descriptors each call the REAL `agenttools_hatch_escalation.request_hatch_approval`
    with their own hook id. Both hatch env vars carry a blank value — a real "use" of the
    mechanism (env_present=True) that is denied LOCALLY, without ever contacting tg-ctl (see
    `_request_present_hatch_approval`'s blank-value early return), so this test cannot reach a
    real Telegram approval regardless of what's on the host machine. The first descriptor
    (lower priority number) denies and blocks the tool call; pre-fix, `_first_hook_block`
    returned as soon as that happened, so the second descriptor's own hatch attempt never ran
    and never got audited."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    checker = tmp_path / "hatch_checker.py"
    checker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['HATCH_LIB_DIR'])\n"
        "import agenttools_hatch_escalation as hatch_escalation\n"
        "json.load(sys.stdin)\n"
        "hook_id = sys.argv[1]\n"
        "result = hatch_escalation.request_hatch_approval(hook_id, {'hook': hook_id}, cwd=os.getcwd())\n"
        "if result.approved:\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'message': f'blocked by {hook_id}'}))\n"
        "sys.exit(10)\n"
    )
    checker.chmod(0o755)
    d1 = _install_descriptor(hooks, hook_id="hatch-alpha", point="pre-bash", cmd=checker, priority=5)
    d2 = _install_descriptor(hooks, hook_id="hatch-beta", point="pre-bash", cmd=checker, priority=50)
    for path, hook_id in ((d1, "hatch-alpha"), (d2, "hatch-beta")):
        spec = json.loads(path.read_text())
        spec["args"] = [hook_id]
        path.write_text(json.dumps(spec))

    overrides_log = home / ".config" / "agent-tools" / "overrides.log"
    cc_event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "anything"}, "cwd": str(tmp_path)}
    proc = _run_dispatch_with_env(
        "PreToolUse", cc_event, home=home,
        extra_env={
            "HATCH_LIB_DIR": str(_LIB),
            "AGENT_TOOLS_OVERRIDES_LOG": str(overrides_log),
            # Blank values: a real hatch USE (env_present=True) that is denied without ever
            # calling tg-ctl, so this can never fire a real Telegram approval on any machine.
            "RIG_HATCH_REQUEST_HATCH_ALPHA": "",
            "RIG_HATCH_REQUEST_HATCH_BETA": "",
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", proc.stdout

    assert overrides_log.is_file(), "expected overrides.log to be written at all"
    entries = [json.loads(line) for line in overrides_log.read_text().splitlines() if line.strip()]
    audited_hatches = {entry["hatch"] for entry in entries}
    assert audited_hatches == {"hatch-alpha", "hatch-beta"}, (
        f"expected BOTH simultaneous hatch attempts audited, got {audited_hatches} from {entries}"
    )


def _mk_lint_repo(tmp_path: Path, *, findings: bool) -> Path:
    """A throwaway repo with a fake local oxlint (findings or clean) and one .ts file."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    binp = repo / "node_modules" / ".bin" / "oxlint"
    binp.parent.mkdir(parents=True)
    body = (
        '#!/bin/sh\necho "a.ts:1:7 no-unused-vars: x is never used"\nexit 1\n'
        if findings
        else "#!/bin/sh\nexit 0\n"
    )
    binp.write_text(body)
    binp.chmod(0o755)
    (repo / "a.ts").write_text("const x=1\n")
    return repo


def test_post_write_lint_findings_surface_as_posttooluse_feedback(tmp_path):
    """END-TO-END for the previously-dead post-write point (#160): the REAL lint-on-write
    hook, installed as rig installs it, fired through the REAL dispatcher on a CC
    PostToolUse Write event, feeds the linter findings back as {decision:block, reason}."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="lint-on-write", point="post-write",
                        cmd=LINT_ON_WRITE, on_error="open")
    repo = _mk_lint_repo(tmp_path, findings=True)
    cc_event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "a.ts"), "content": "const x=1\n"},
        "cwd": str(repo),
    }
    proc = _run_dispatch("PostToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "no-unused-vars" in out["reason"]
    assert "a.ts" in out["reason"]


def test_post_write_clean_file_emits_nothing(tmp_path):
    """Same pipeline, clean file → the dispatcher stays silent (no feedback, no noise)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="lint-on-write", point="post-write",
                        cmd=LINT_ON_WRITE, on_error="open")
    repo = _mk_lint_repo(tmp_path, findings=False)
    cc_event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "a.ts"), "content": "const x = 1\n"},
        "cwd": str(repo),
    }
    proc = _run_dispatch("PostToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout


def test_post_write_format_then_lint_ordering(tmp_path):
    """BOTH real post-write hooks coexist: format-on-write (priority 60) runs BEFORE
    lint-on-write (70), so the linter sees the FORMATTED file; format-on-write never
    blocks, so lint's feedback is what surfaces. The fake oxlint only reports findings
    when the fake oxfmt's marker is present — proving the ordering, not just coexistence."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="format-on-write", point="post-write",
                        cmd=FORMAT_ON_WRITE, on_error="open", priority=60)
    _install_descriptor(hooks, hook_id="lint-on-write", point="post-write",
                        cmd=LINT_ON_WRITE, on_error="open", priority=70)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".oxfmtrc.json").write_text("{}")  # oxfmt is the repo's configured formatter
    bind = repo / "node_modules" / ".bin"
    bind.mkdir(parents=True)
    # fake formatter: append a marker to the (last-arg) file, exit 0
    oxfmt = bind / "oxfmt"
    oxfmt.write_text('#!/bin/sh\neval "f=\\${$#}"\necho "FORMATTED" >> "$f"\nexit 0\n')
    oxfmt.chmod(0o755)
    # fake linter: findings ONLY if the formatter's marker is already in the file
    oxlint = bind / "oxlint"
    oxlint.write_text(
        '#!/bin/sh\neval "f=\\${$#}"\n'
        'if grep -q FORMATTED "$f"; then echo "lint-finding-after-format"; exit 1; fi\nexit 0\n'
    )
    oxlint.chmod(0o755)
    f = repo / "a.ts"
    f.write_text("const x=1\n")
    cc_event = {"hook_event_name": "PostToolUse", "tool_name": "Write",
                "tool_input": {"file_path": str(f), "content": "const x=1\n"},
                "cwd": str(repo)}
    proc = _run_dispatch("PostToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", (proc.stdout, proc.stderr)
    assert "lint-finding-after-format" in out["reason"]
    assert "FORMATTED" in f.read_text()  # the formatter really ran first


def test_post_write_format_on_write_stays_silent_even_on_formatter_error(tmp_path):
    """format-on-write going LIVE must never turn a formatter failure into feedback noise:
    a formatter that exits non-zero still yields a silent allow (empty dispatcher stdout)."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="format-on-write", point="post-write",
                        cmd=FORMAT_ON_WRITE, on_error="open", priority=60)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".oxfmtrc.json").write_text("{}")  # oxfmt is the repo's configured formatter
    bind = repo / "node_modules" / ".bin"
    bind.mkdir(parents=True)
    oxfmt = bind / "oxfmt"
    oxfmt.write_text('#!/bin/sh\necho "syntax error" >&2\nexit 2\n')
    oxfmt.chmod(0o755)
    f = repo / "a.ts"
    f.write_text("const x=1\n")
    cc_event = {"hook_event_name": "PostToolUse", "tool_name": "Write",
                "tool_input": {"file_path": str(f), "content": "const x=1\n"},
                "cwd": str(repo)}
    proc = _run_dispatch("PostToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout


def test_post_write_hook_not_fired_on_posttooluse_bash(tmp_path):
    """PostToolUse Bash has no logical point — a post-write hook must NOT fire on it."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    # a post-write hook that would ALWAYS block if (wrongly) invoked
    blocker = tmp_path / "always_block.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "sys.stdout.write(json.dumps({'hook_api':'agents-hooks/v1','decision':'block','message':'should never fire'}))\n"
        "sys.exit(10)\n"
    )
    blocker.chmod(0o755)
    _install_descriptor(hooks, hook_id="always-block", point="post-write",
                        cmd=blocker, on_error="open")
    cc_event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    proc = _run_dispatch("PostToolUse", cc_event, home=home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout


# ── agent-tools#676: nested omp subagent under a Claude-Code-backed harness ───────────────
#
# Live incident this reproduces: a top-level omp session backed by Claude Code
# (`rig.yaml` default `harness.kind: claude-code`) dispatches a nested Task/eval subagent via
# omp's OWN `task` tool (not CC's native `Agent`/`Task` tool — so CC never sets a top-level
# `agent_id`, and no `RIG_AGENT_ID` launcher marker is set either). That subagent's own tool
# calls still fire through CC's hook system, so `cc_hook_bridge` — which stamps
# `event["harness"] == "claude-code"` UNCONDITIONALLY (its own module-literal `HARNESS`,
# never derived from the event) — is what actually reaches `orchestrator-stays-thin`. Before
# this fix, that hook's nested-`PI_SESSION_FILE` subagent-identity tier only fired when
# `event["harness"] == "omp"`, which `cc_hook_bridge` can never produce — so the subagent was
# misclassified as the orchestrator and its `git commit` was denied (agent-tools#676).
#
# Every case below drives the REAL `cc_hook_bridge` subprocess with the REAL
# `orchestrator-stays-thin` script installed exactly as `rig apply` installs it (not a mock),
# against a REAL temp git repo, and — on the allowed path — actually performs the `git commit`
# to prove the underlying action succeeds, not just that the dispatcher's JSON says "allow".

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, env=env, timeout=30)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.email", "t@t.t").returncode == 0
    assert _git(repo, "config", "user.name", "t").returncode == 0
    (repo / "README.md").write_text("hello\n")
    assert _git(repo, "add", "README.md").returncode == 0
    assert _git(repo, "commit", "-m", "init").returncode == 0
    return repo


def _run_dispatch_with_env(event: str, cc_event: dict, *, home: Path,
                            extra_env: dict) -> subprocess.CompletedProcess:
    """Same contract as `_run_dispatch`, plus caller-supplied env (PI_SESSION_FILE, etc.)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    for k in ("PI_SESSION_FILE", "RIG_AGENT_ID", "RIG_DETACHED_AGENT"):
        env.pop(k, None)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "cc_hook_bridge", event],
        input=json.dumps(cc_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_nested_omp_subagent_under_claude_code_harness_git_commit_allowed_clean_room(tmp_path):
    """THE agent-tools#676 repro, end to end. A nested omp Task/eval child (real
    `PI_SESSION_FILE` shaped `.../<session-id>/<agent>.jsonl`, no `args.agent_id`, no
    `RIG_AGENT_ID`) fires a `git commit` PreToolUse event through the REAL `cc_hook_bridge`
    subprocess — which always tags `event["harness"] == "claude-code"` — into the REAL
    `orchestrator-stays-thin` script. Must be ALLOWED (empty stdout, exit 0, no deny), and the
    real `git commit` that follows must actually succeed. Would FAIL on the pre-fix code,
    which only exempted a nested `PI_SESSION_FILE` when `event["harness"] == "omp"` — a value
    `cc_hook_bridge` never produces."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="orchestrator-stays-thin", point="pre-bash",
                        cmd=ORCHESTRATOR_STAYS_THIN, on_error="closed")
    repo = _make_repo(tmp_path)
    nested_session_file = (
        str(tmp_path / "omp-sessions" / "-repo" /
            "2026-09-08T18-00-00-000Z_01a07dbc-040a-7089-9724-7f37bfdba15e" /
            "CommitReconcile.jsonl")
    )
    (repo / "a.py").write_text("x = 1\n")
    assert _git(repo, "add", "a.py").returncode == 0
    cc_event = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'fix: x'"}, "cwd": str(repo),
    }
    # Call TWICE with the identical event/cwd: a genuinely subagent-exempt call allows on
    # EVERY call (no tiering at all — `_is_subagent` short-circuits before the WARN/BLOCK
    # marker logic even runs), whereas a merely-first-offense WARN (the bug's fingerprint: an
    # ungated call that looks like "allow" only once) would BLOCK on this second call. A
    # single call cannot tell those two apart; this can.
    for attempt in (1, 2):
        proc = _run_dispatch_with_env(
            "PreToolUse", cc_event, home=home,
            extra_env={"PI_SESSION_FILE": nested_session_file},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", (
            f"nested omp subagent under claude-code harness was DENIED on call {attempt}: {proc.stdout}"
        )
    commit = _git(repo, "commit", "-m", "fix: x")
    assert commit.returncode == 0, commit.stderr
    assert "fix: x" in _git(repo, "log", "-1", "--format=%s").stdout


def test_bare_orchestrator_git_commit_still_blocked_clean_room(tmp_path):
    """Precision control for the same fix: a plain top-level Claude Code orchestrator (no
    `agent_id`, no `RIG_AGENT_ID`, no nested `PI_SESSION_FILE` at all) driving the SAME REAL
    subprocess pipeline must still be WARN-then-BLOCKED — broadening the nested-session tier
    must not weaken real-orchestrator detection."""
    home = tmp_path / "home"
    hooks = home / ".claude" / "hooks"
    _install_descriptor(hooks, hook_id="orchestrator-stays-thin", point="pre-bash",
                        cmd=ORCHESTRATOR_STAYS_THIN, on_error="closed")
    repo = _make_repo(tmp_path)
    cc_event = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'fix: x'"}, "cwd": str(repo),
    }
    first = _run_dispatch_with_env("PreToolUse", cc_event, home=home, extra_env={})
    assert first.returncode == 0 and first.stdout.strip() == "", first.stdout  # first: WARN
    second = _run_dispatch_with_env("PreToolUse", cc_event, home=home, extra_env={})
    assert second.returncode == 0, second.stderr
    out = json.loads(second.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", second.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
