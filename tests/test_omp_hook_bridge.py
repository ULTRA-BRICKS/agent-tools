"""Clean-room proof for the agents-hooks/v1 -> omp (oh-my-pi) extension bridge.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_omp_hook_bridge.py -q

omp loads Bun-executed TS/JS extension modules that register `pi.on(eventName, handler)`
handlers. The rig-provisioned extension (`extension.ts`) calls this Python dispatcher with
omp's `tool_call` / `tool_result` payloads so installed `agents-hooks/v1` descriptors can
run for omp too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import omp_hook_bridge.dispatch as dispatch  # noqa: E402

BLOCK_RAW_PR_MERGE = _REPO / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
BLOCK_SECRETS_WRITE = _REPO / "agent-hooks" / "block-secrets-write" / "block_secrets_write.py"
BACKGROUND_SUBAGENT_GATE = _REPO / "agent-hooks" / "background-subagent-gate" / "background_subagent_gate.py"
WORKTREE_ONLY_WRITES = _REPO / "agent-hooks" / "worktree-only-writes" / "worktree_only_writes.py"

_EXTENSION_TS = _REPO / "lib" / "omp_hook_bridge" / "extension.ts"


def _install_descriptor(
    hooks_dir: Path,
    *,
    hook_id: str,
    point: str,
    cmd: Path,
    on_error: str = "closed",
    priority: int = 10,
) -> Path:
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


def _run_dispatch(event_name: str, omp_event: dict, *, hooks_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["OMP_HOOKS_DIR"] = str(hooks_dir)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "omp_hook_bridge", event_name],
        input=json.dumps(omp_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# --- point_for_event -----------------------------------------------------------------------


def test_point_for_event_maps_omp_tools_to_v1_points():
    assert dispatch.point_for_event("tool_call", "bash") == "pre-bash"
    assert dispatch.point_for_event("tool_call", "edit") == "pre-write"
    assert dispatch.point_for_event("tool_call", "write") == "pre-write"
    assert dispatch.point_for_event("tool_call", "apply_patch") == "pre-write"
    assert dispatch.point_for_event("tool_call", "notebook") == "pre-write"
    assert dispatch.point_for_event("tool_call", "task") == "pre-agent"
    assert dispatch.point_for_event("tool_result", "edit") == "post-write"
    assert dispatch.point_for_event("tool_result", "write") == "post-write"
    assert dispatch.point_for_event("tool_result", "apply_patch") == "post-write"
    assert dispatch.point_for_event("tool_result", "bash") is None
    assert dispatch.point_for_event("tool_result", "task") is None
    assert dispatch.point_for_event("tool_call", "read") is None
    assert dispatch.point_for_event("unknown_event", "bash") is None


def test_point_for_event_is_case_insensitive_and_tolerates_missing_tool():
    assert dispatch.point_for_event("tool_call", "BASH") == "pre-bash"
    assert dispatch.point_for_event("tool_call", None) is None


# --- to_v1_event: bash -----------------------------------------------------------------------


def test_to_v1_event_carries_bash_command_and_metadata():
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "gh pr merge 42 --admin"},
        "cwd": "/repo",
        "toolCallId": "call_1",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["hook_api"] == "agents-hooks/v1"
    assert v1["event_id"] == "call_1"
    assert v1["tool"] == "bash"
    assert v1["point"] == "pre-bash"
    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["cwd"] == "/repo"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"


def test_to_v1_event_tags_harness_omp():
    """agent-tools#533: every v1 event this bridge produces carries the top-level `harness`
    tag `"omp"`, unconditionally — a module constant, never derived from `omp_event`. Same
    non-forgeable signal as `codex_hook_bridge.HARNESS` / `opencode_hook_bridge.HARNESS`,
    letting orchestrator-stays-thin exempt the whole harness."""
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git status", "harness": "claude-code"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["harness"] == "omp"
    assert dispatch.HARNESS == "omp"


def test_to_v1_event_defaults_cwd_and_event_id_when_absent():
    v1 = dispatch.to_v1_event({"event": "tool_call", "toolName": "bash", "input": {"command": "ls"}}, point="pre-bash")
    assert v1["event_id"] == ""
    assert v1["cwd"] == os.getcwd()


# --- to_v1_event: forged agent identity ------------------------------------------------------


def test_to_v1_event_drops_forged_agent_identity_from_tool_args():
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "agent": "general",
            "task": "inspect the entire repository and fix the issue",
            "name": "worker-1",
            "agent_id": "forged",
            "agentId": "forged-camel",
            "agentType": "worker",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-agent")

    assert v1["point"] == "pre-agent"
    assert v1["args"]["task"] == "inspect the entire repository and fix the issue"
    assert v1["args"]["name"] == "worker-1"
    assert v1["args"]["subagent_type"] == "general"
    assert "agent_id" not in v1["args"]
    assert "agentId" not in v1["args"]
    assert "agentType" not in v1["args"]
    # `agent` itself is a forged-identity key and is stripped too — the normalized
    # `subagent_type` copy above is what a subagent-aware hook should read instead.
    assert "agent" not in v1["args"]


@pytest.fixture(autouse=True)
def _no_host_agent_markers(monkeypatch):
    """Module-wide: a rig-dispatched test run (this process) may carry RIG_AGENT_ID /
    RIG_DETACHED_AGENT; strip them for every test so in-process to_v1_event asserts never
    see an injected agent_id. Subprocess tests pass markers explicitly via `env=`."""
    _clear_agent_env_markers(monkeypatch)


def _clear_agent_env_markers(monkeypatch) -> None:
    """Drop the detached-agent env markers — mandatory before any to_v1_event test,
    because a rig-dispatched omp test run (the very process pytest lives in) carries
    them and the dispatcher injects args.agent_id from process env."""
    monkeypatch.delenv("RIG_AGENT_ID", raising=False)
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)
    monkeypatch.delenv("PI_SESSION_FILE", raising=False)


def test_to_v1_event_injects_agent_id_from_env_marker(monkeypatch):
    """agent-tools#573: the ONE trusted subagent identity source for omp — ported unchanged
    from `opencode_hook_bridge._detached_agent_id` (agent-tools#476) — is the launcher
    process environment, since omp's own tool_call/tool_result events expose none."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "probe")
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "probe"


def test_to_v1_event_without_env_marker_has_no_agent_id(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert "agent_id" not in v1["args"]


_OMP_NESTED_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e/ThinNotSub.jsonl"
)
_OMP_MAIN_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e.jsonl"
)


def test_to_v1_event_injects_agent_id_from_nested_pi_session_file(monkeypatch):
    """agent-tools#634: a native omp Task/eval child has nested PI_SESSION_FILE and no
    RIG_AGENT_ID. The bridge must inject args.agent_id so EVERY subagent-aware hook
    (not only orchestrator-stays-thin) classifies the worker."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("PI_SESSION_FILE", _OMP_NESTED_SESSION)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert v1["args"]["agent_id"] == "ThinNotSub"


def test_to_v1_event_main_pi_session_file_has_no_agent_id(monkeypatch):
    """The MAIN omp session file is not a child — do not inject agent_id."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("PI_SESSION_FILE", _OMP_MAIN_SESSION)
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "git commit -m 'fix: x'"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_to_v1_event_env_marker_wins_over_forged_agent_id(monkeypatch):
    """Strip-then-inject order: a forged args.agent_id is dropped FIRST, then the env
    marker (the only authority) supplies the identity — never the payload."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "probe")
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "agent": "general",
            "task": "do work",
            "agent_id": "forged",
            "agentId": "forged-camel",
            "agentType": "worker",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-agent")

    assert v1["args"]["agent_id"] == "probe"
    assert "agentId" not in v1["args"]
    assert "agentType" not in v1["args"]
    assert "agent" not in v1["args"]


def test_to_v1_event_detached_marker_without_id_yields_anonymous_identity(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_DETACHED_AGENT", "1")
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "detached"


def test_to_v1_event_blank_env_marker_is_not_an_identity(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "   ")
    monkeypatch.setenv("RIG_DETACHED_AGENT", "")
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")

    assert "agent_id" not in v1["args"]


def test_omp_bridge_env_marker_makes_nontrivial_task_subagent_exempt(tmp_path, monkeypatch):
    """End-to-end pre-agent pipeline: with the launcher-set env marker present, the real
    background-subagent-gate descriptor sees a trusted agent_id and allows a dispatch that
    would otherwise need to prove triviality or backgrounding."""
    _clear_agent_env_markers(monkeypatch)
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="background-subagent-gate", point="pre-agent",
        cmd=BACKGROUND_SUBAGENT_GATE, on_error="closed",
    )
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "agent": "general-purpose",
            "task": "x" * 400,  # clearly non-trivial, not inherently background
        },
        "cwd": "/repo",
    }
    env = dict(os.environ)
    env["OMP_HOOKS_DIR"] = str(hooks_dir)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["RIG_AGENT_ID"] = "LauncherChild"
    proc = subprocess.run(
        [sys.executable, "-m", "omp_hook_bridge", "tool_call"],
        input=json.dumps(omp_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # empty stdout == allow


def test_to_v1_event_task_normalization_does_not_clobber_existing_subagent_type():
    omp_event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {"agent": "general", "subagent_type": "fork", "task": "do work"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-agent")
    assert v1["args"]["subagent_type"] == "fork"


def test_task_normalization_maps_task_to_prompt_and_marks_background():
    """omp's item shape has `task` (not `prompt`) and NO per-call background lever — the
    pre-agent gate reads `prompt`/`description` for triviality and `run_in_background` for
    shape, so without this mapping every omp dispatch is judged non-trivial + foreground."""
    args = {"agent": "scout", "task": "inspect the repo\nthen fix the bug"}
    dispatch._normalize_task_args(args)
    assert args["prompt"] == "inspect the repo\nthen fix the bug"
    assert args["task"] == "inspect the repo\nthen fix the bug"  # raw field kept
    assert args["subagent_type"] == "scout"
    assert args["run_in_background"] is True


def test_task_normalization_batch_shape_joins_items_and_takes_first_agent():
    args = {
        "context": "shared background",
        "tasks": [
            {"name": "a", "agent": "scout", "task": "look at src/"},
            {"name": "b", "task": "look at tests/"},
        ],
    }
    dispatch._normalize_task_args(args)
    assert args["prompt"] == "look at src/\nlook at tests/"
    assert args["subagent_type"] == "scout"
    assert args["run_in_background"] is True
    assert args["context"] == "shared background"  # raw batch fields kept
    assert len(args["tasks"]) == 2


def test_task_normalization_never_clobbers_explicit_prompt_or_background():
    args = {"task": "x", "prompt": "explicit", "run_in_background": False}
    dispatch._normalize_task_args(args)
    assert args["prompt"] == "explicit"
    assert args["run_in_background"] is False


def test_task_normalization_is_inert_for_non_task_args():
    args = {"command": "ls"}
    dispatch._normalize_task_args(args)
    assert args == {"command": "ls"}


# --- to_v1_event: write -----------------------------------------------------------------------


def test_to_v1_event_normalizes_simple_write_path_and_content():
    omp_event = {
        "event": "tool_call",
        "toolName": "write",
        "input": {"path": "src/new.py", "content": "print('hi')\n"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "src/new.py"
    assert v1["args"]["path"] == "src/new.py"
    assert v1["args"]["content"] == "print('hi')\n"


def test_to_v1_event_write_missing_content_becomes_empty_string():
    omp_event = {
        "event": "tool_call",
        "toolName": "write",
        "input": {"path": "src/new.py"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["content"] == ""


def test_to_v1_event_write_post_execution_does_not_require_content():
    omp_event = {
        "event": "tool_result",
        "toolName": "write",
        "input": {"path": "src/new.py", "content": "print('hi')\n"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="post-write")
    assert v1["args"]["file_path"] == "src/new.py"
    # post-write never runs the pre-write-only empty-string coercion — whatever `content` was
    # already present in the raw tool args (copied verbatim into `args`) passes through as-is.
    assert v1["args"]["content"] == "print('hi')\n"


# --- to_v1_event: edit (hashline) --------------------------------------------------------------


_SINGLE_SECTION_HASHLINE = (
    "*** Begin Patch\n"
    "[greet.py#A1B2]\n"
    "PUT 1*:\n"
    "+@cache\n"
    "+def greet(name):\n"
    "+    print(f\"Hi, {name}\")\n"
    "*** End Patch\n"
)


def test_to_v1_event_hashline_single_section_extracts_path_and_content():
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _SINGLE_SECTION_HASHLINE},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_path"] == "greet.py"
    assert v1["args"]["path"] == "greet.py"
    assert v1["args"]["file_paths"] == ["greet.py"]
    assert "@cache" in v1["args"]["content"]
    assert "def greet(name):" in v1["args"]["content"]
    assert v1["command"] == _SINGLE_SECTION_HASHLINE


_MULTI_SECTION_HASHLINE = (
    "*** Begin Patch\n"
    "[greet.py#A1B2]\n"
    "CUT 1* @fn\n"
    "[lib/greet.py#3C4D]\n"
    "PUT <1 @fn\n"
    "*** End Patch\n"
)


def test_to_v1_event_hashline_multi_section_extracts_every_path():
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _MULTI_SECTION_HASHLINE},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_paths"] == ["greet.py", "lib/greet.py"]
    # Two paths and no move target: `_apply_paths` deliberately sets no single `file_path`/
    # `path` in that case (only `file_paths`) — a path-based hook must consult `file_paths`
    # for a multi-path event, not assume a single-file shape.
    assert "file_path" not in v1["args"]
    assert "path" not in v1["args"]


_MULTI_FILE_HASHLINE = (
    "*** Begin Patch\n"
    "[a.py#AAAA]\n"
    "PUT >$:\n"
    "+print('a')\n"
    "[b.py#BBBB]\n"
    "PUT >$:\n"
    "+print('b')\n"
    "*** End Patch\n"
)


def test_hashline_fan_out_splits_multi_file_content_per_path():
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _MULTI_FILE_HASHLINE},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["a.py", "b.py"]
    assert [e["args"]["content"] for e in events] == ["print('a')", "print('b')"]
    assert all("file_paths" not in e["args"] for e in events)
    # agent-tools#533: the harness tag must survive the per-file clone.
    assert all(e["harness"] == "omp" for e in events)


def test_hashline_fan_out_ignores_a_forged_patch_key(monkeypatch):
    """#556 review finding: on an `edit` call a stray/forged `patch` key in the raw args
    must NOT shadow the real hashline `input` when fanning out per-path content — otherwise
    every per-path `content` comes back "" and a secret-scanning pre-write hook sees nothing."""
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": _MULTI_FILE_HASHLINE, "patch": "x"},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["a.py", "b.py"]
    assert [e["args"]["content"] for e in events] == ["print('a')", "print('b')"]


def test_hashline_rem_and_mv_sections_still_surface_their_paths():
    patch = "*** Begin Patch\n[old.py#DEAD]\nMV new.py\n*** End Patch\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_paths"] == ["old.py"]


def test_hashline_repeated_same_path_sections_accumulate_content():
    """Codex PR-thread P1: a later benign section for `a.py` must not ERASE an earlier
    section's rows — otherwise a secret in section 1 hides behind section 3 and a content
    scanner never sees it."""
    patch = (
        "[a.py#AAAA]\nPUT >$:\n+API_KEY = 'abcd1234abcd1234'\n"
        "[b.py#BBBB]\nPUT >$:\n+print('b')\n"
        "[a.py#AAAA]\nPUT <1:\n+import os\n"
    )
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": "/repo"}
    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")
    by_path = {e["args"]["file_path"]: e["args"]["content"] for e in events}
    assert by_path["a.py"] == "API_KEY = 'abcd1234abcd1234'\nimport os"
    assert by_path["b.py"] == "print('b')"
    assert dispatch._hashline_file_paths(patch) == ["a.py", "b.py"]


def test_edit_parsed_truth_overwrites_stray_content_and_forged_file_path():
    """Sonnet review: the parsed patch is the truth about what gets written. A stray
    `content: ""` or a forged `file_path` riding along in the raw args must not shadow it
    (`block-secrets-write` reads `content`, `worktree-only-writes` reads `file_path`)."""
    patch = "[src/x.py#A1B2]\nPUT >$:\n+API_KEY = 'abcd1234abcd1234'\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": patch, "content": "", "file_path": "/tmp/elsewhere.py", "path": "/tmp/elsewhere.py"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["content"] == "API_KEY = 'abcd1234abcd1234'"
    assert v1["args"]["file_path"] == "src/x.py"
    assert v1["args"]["path"] == "src/x.py"


def test_edit_multi_path_drops_forged_single_file_path():
    patch = "[a.py#AAAA]\nPUT >$:\n+a\n[b.py#BBBB]\nPUT >$:\n+b\n"
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": patch, "file_path": "decoy.py", "file_paths": ["decoy.py"]},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_paths"] == ["a.py", "b.py"]
    assert "file_path" not in v1["args"]
    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")
    assert [e["args"]["file_path"] for e in events] == ["a.py", "b.py"]


# --- to_v1_event: edit (non-hashline `replace` / `patch` modes) ------------------------------


def test_edit_replace_mode_maps_path_and_replacement_text():
    """Codex review P1: omp can run `edit` in `replace` mode (no `[PATH#TAG]` section); such
    a call must still surface its path and its replacement text as `content`, or a secret
    written through it reaches `block-secrets-write` as `content == ""`."""
    omp_event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"path": "src/config.py", "oldText": "x = 1", "newText": "API_KEY = 'abcd1234abcd1234'"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "src/config.py"
    assert v1["args"]["path"] == "src/config.py"
    assert v1["args"]["content"] == "API_KEY = 'abcd1234abcd1234'"


def test_edit_patch_mode_maps_unified_diff_path_and_added_rows():
    diff = "--- a/src/config.py\n+++ b/src/config.py\n@@ -1 +1 @@\n-x = 1\n+API_KEY = 'abcd1234abcd1234'\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"patch": diff}, "cwd": "/repo"}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "src/config.py"
    assert v1["args"]["content"] == "API_KEY = 'abcd1234abcd1234'"


def test_omp_bridge_blocks_secret_write_via_edit_replace_mode(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"path": "src/secret.py", "oldText": "x", "newText": "API_KEY = 'abcd1234abcd1234'\n"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"


def test_task_normalization_not_applied_to_non_task_tools_with_task_key():
    """Sonnet review: a bash call whose args happen to carry a `task` key must not get the
    pre-agent `run_in_background` field injected into its pre-bash event."""
    omp_event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "sleep 300 &", "task": "background this", "agent": "x"},
        "cwd": "/repo",
    }
    v1 = dispatch.to_v1_event(omp_event, point="pre-bash")
    assert "run_in_background" not in v1["args"]
    assert "prompt" not in v1["args"]
    assert "subagent_type" not in v1["args"]


# --- to_v1_event: apply_patch (classic envelope) ------------------------------------------------


_APPLY_PATCH_ENVELOPE = (
    "*** Begin Patch\n"
    "*** Add File: src/new.py\n"
    "+API_KEY = 'abcd1234abcd1234'\n"
    "*** Update File: src/existing.py\n"
    "+print('changed')\n"
    "*** End Patch\n"
)


@pytest.mark.parametrize("field", ["patch", "patchText", "input"])
def test_to_v1_event_apply_patch_tries_all_three_field_names(field):
    omp_event = {
        "event": "tool_call",
        "toolName": "apply_patch",
        "input": {field: _APPLY_PATCH_ENVELOPE},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["command"] == _APPLY_PATCH_ENVELOPE
    assert v1["args"]["patch"] == _APPLY_PATCH_ENVELOPE
    assert v1["args"]["patchText"] == _APPLY_PATCH_ENVELOPE
    assert v1["args"]["file_paths"] == ["src/new.py", "src/existing.py"]
    assert "API_KEY = 'abcd1234abcd1234'" in v1["args"]["content"]


def test_to_v1_event_apply_patch_move_includes_source_and_target():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** End Patch\n"
    )
    omp_event = {"event": "tool_call", "toolName": "apply_patch", "input": {"patch": patch}, "cwd": "/repo"}

    v1 = dispatch.to_v1_event(omp_event, point="pre-write")

    assert v1["args"]["file_paths"] == ["src/old.py", "src/new.py"]
    assert v1["args"]["file_path"] == "src/new.py"
    assert v1["args"]["path"] == "src/new.py"


def test_apply_patch_fan_out_splits_multi_file_content():
    omp_event = {
        "event": "tool_call",
        "toolName": "apply_patch",
        "input": {"patch": _APPLY_PATCH_ENVELOPE},
        "cwd": "/repo",
    }

    events = dispatch._v1_events_for_dispatch(omp_event, point="pre-write")

    assert [e["args"]["file_path"] for e in events] == ["src/new.py", "src/existing.py"]
    assert "API_KEY = 'abcd1234abcd1234'" in events[0]["args"]["content"]
    assert events[1]["args"]["content"] == "print('changed')"


# --- hooks_dir() env precedence --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_omp_dir_overrides(monkeypatch):
    """Every hooks_dir() precedence test below starts from a clean env: a developer/CI shell
    exporting any of these (OMP_CODING_AGENT_DIR is checked FIRST) would otherwise fail them."""
    for name in ("OMP_HOOKS_DIR", "OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_hooks_dir_default(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path(os.path.expanduser("~/.omp/agent/hooks"))


def test_hooks_dir_pi_config_dir_renames_dotomp(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-work")
    assert dispatch.hooks_dir() == Path(os.path.expanduser("~/.omp-work/agent/hooks"))


def test_hooks_dir_pi_coding_agent_dir_is_full_override(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent/root")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path("/custom/agent/root/hooks")


def test_hooks_dir_pi_coding_agent_dir_wins_over_pi_config_dir(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent/root")
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-work")
    assert dispatch.hooks_dir() == Path("/custom/agent/root/hooks")


def test_hooks_dir_omp_hooks_dir_overrides_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("OMP_HOOKS_DIR", str(tmp_path))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/custom/agent/root")
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-work")
    assert dispatch.hooks_dir() == tmp_path


def test_hooks_dir_pi_coding_agent_dir_relative_is_home_anchored(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "relative/agent")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path(os.path.expanduser("~/relative/agent/hooks"))


def test_hooks_dir_pi_coding_agent_dir_expands_dollar_var(monkeypatch):
    """rig-cli's own `riglib.harness_skills.omp_agent_root` (which this is ported from)
    resolves both env vars via `expand_user_path`, i.e. `expanduser(expandvars(path))` — a
    `$VAR` reference, not just `~`, must resolve to the SAME real path rig installs the
    descriptor dir and extension symlink into, or this dispatcher silently loads zero
    descriptors from an unexpanded literal path segment and allows everything."""
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("PI_HOME_FOR_TEST", "/custom/home")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "$PI_HOME_FOR_TEST/omp-agent")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path("/custom/home/omp-agent/hooks")


def test_hooks_dir_pi_config_dir_expands_dollar_var(monkeypatch):
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.delenv("OMP_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_HOME_FOR_TEST", "/custom/home")
    monkeypatch.setenv("PI_CONFIG_DIR", "$PI_HOME_FOR_TEST/.omp-work")
    assert dispatch.hooks_dir() == Path("/custom/home/.omp-work/agent/hooks")


def test_hooks_dir_omp_coding_agent_dir_wins_over_pi_coding_agent_dir(monkeypatch):
    """omp's primary override is the OMP-prefixed var; `PI_CODING_AGENT_DIR` is the legacy
    name. This repo's own omp resolver (`lib/checker/model_freshness.py::_omp_agent_db`)
    checks OMP first — the bridge must agree or a relocated omp loads zero descriptors."""
    monkeypatch.delenv("OMP_HOOKS_DIR", raising=False)
    monkeypatch.setenv("OMP_CODING_AGENT_DIR", "/omp/primary")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/pi/legacy")
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    assert dispatch.hooks_dir() == Path("/omp/primary/hooks")


@pytest.mark.parametrize(
    ("var", "expected"),
    [
        ("OMP_CODING_AGENT_DIR", "~/custom-agent/hooks"),
        ("PI_CODING_AGENT_DIR", "~/custom-agent/hooks"),
        ("PI_CONFIG_DIR", "~/custom-agent/agent/hooks"),
    ],
)
def test_hooks_dir_relative_dollar_var_expansion_is_anchored_expanded(monkeypatch, var, expected):
    """A `$VAR` that expands to a RELATIVE path must be home-anchored AFTER expansion — the
    raw override string would leave a literal `$PROFILE_ROOT` path segment behind and the
    bridge would read a directory that does not exist (Codex/Sonnet review finding)."""
    for name in ("OMP_HOOKS_DIR", "OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROFILE_ROOT", "custom-agent")
    monkeypatch.setenv(var, "$PROFILE_ROOT")
    assert dispatch.hooks_dir() == Path(os.path.expanduser(expected))
    assert "$" not in str(dispatch.hooks_dir())


# --- main() fail-open behavior -----------------------------------------------------------------


def test_main_fails_open_on_malformed_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("{not json"))
    rc = dispatch.main(["tool_call"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_fails_open_when_dispatch_raises(monkeypatch):
    def _boom(_event_name, _event):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch, "dispatch", _boom)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({"event": "tool_call", "toolName": "bash", "input": {}})))
    rc = dispatch.main(["tool_call"])
    assert rc == 0


def test_main_fails_open_when_agent_hooks_v1_import_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OMP_HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.syspath_prepend(str(tmp_path))  # no real agent_hooks_v1 reachable here
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({
        "event": "tool_call", "toolName": "bash", "input": {"command": "ls"},
    })))
    real_import = __import__

    def _blocked_import(name, *args, **kwargs):
        if name == "agent_hooks_v1":
            raise ImportError("simulated missing agent_hooks_v1")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked_import)
    rc = dispatch.main(["tool_call"])
    assert rc == 0


def test_main_allows_when_no_result(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OMP_HOOKS_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({
        "event": "tool_call", "toolName": "read", "input": {"path": "x"},
    })))
    rc = dispatch.main([])
    assert rc == 0
    assert capsys.readouterr().out == ""


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# --- end-to-end: real descriptors ---------------------------------------------------------------


def test_omp_bridge_blocks_raw_pr_merge_with_real_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-raw-pr-merge", point="pre-bash", cmd=BLOCK_RAW_PR_MERGE)
    event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "gh pr merge 42 --admin"},
        "cwd": str(tmp_path),
        "toolCallId": "call_1",
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "gh ship" in out["reason"]


def test_omp_bridge_nontrivial_task_dispatch_passes_real_background_subagent_gate(tmp_path):
    """End-to-end bridge -> real `background-subagent-gate` (the Codex review's P1): an omp
    `task` call has no `prompt` and no per-call background flag, so an unnormalized event was
    judged non-trivial + foreground and BLOCKED every omp dispatch. The normalized event must
    pass on omp's own background-job contract."""
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir,
        hook_id="background-subagent-gate",
        point="pre-agent",
        cmd=BACKGROUND_SUBAGENT_GATE,
        on_error="open",
    )
    event = {
        "event": "tool_call",
        "toolName": "task",
        "input": {
            "context": "shared background for both workers",
            "tasks": [
                {"agent": "general", "task": "Inspect the provisioning code, implement the bridge,\nrun tests, and report."},
                {"agent": "scout", "task": "Audit every consumer hook for the fields it reads."},
            ],
        },
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_omp_bridge_blocks_secret_write_via_write_tool(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    event = {
        "event": "tool_call",
        "toolName": "write",
        "input": {"path": "src/secret.py", "content": "API_KEY = 'abcd1234abcd1234'\n"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_blocks_secret_write_via_edit_hashline(tmp_path):
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(hooks_dir, hook_id="block-secrets-write", point="pre-write", cmd=BLOCK_SECRETS_WRITE)
    patch = "*** Begin Patch\n[src/secret.py#A1B2]\nPUT >$:\n+API_KEY = 'abcd1234abcd1234'\n*** End Patch\n"
    event = {
        "event": "tool_call",
        "toolName": "edit",
        "input": {"input": patch},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_call", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"


def test_omp_bridge_dispatches_post_write_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    log_path = tmp_path / "seen.json"
    script = tmp_path / "record_hook.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(log_path)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    _install_descriptor(hooks_dir, hook_id="recorder", point="post-write", cmd=script, on_error="open")
    event = {
        "event": "tool_result",
        "toolName": "write",
        "input": {"path": "src/new.py", "content": "print(1)\n"},
        "cwd": str(tmp_path),
    }

    proc = _run_dispatch("tool_result", event, hooks_dir=hooks_dir)

    assert proc.returncode == 0, proc.stderr
    seen = json.loads(log_path.read_text(encoding="utf-8"))
    assert seen["point"] == "post-write"
    assert seen["args"]["file_path"] == "src/new.py"


def test_omp_bridge_allows_when_hooks_dir_missing(tmp_path):
    event = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "ls"},
        "cwd": str(tmp_path),
    }
    proc = _run_dispatch("tool_call", event, hooks_dir=tmp_path / "nope")
    assert proc.returncode == 0
    assert proc.stdout == ""


# --- worktree-only-writes consuming an omp-produced pre-write event -----------------------------


def test_worktree_only_writes_reads_omp_edit_file_path(tmp_path, monkeypatch):
    """worktree-only-writes only reads args.file_path/path/notebook_path — prove the omp
    bridge's hashline path extraction actually feeds it correctly end to end."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    patch = "*** Begin Patch\n[foo.py#A1B2]\nPUT >$:\n+print(1)\n*** End Patch\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": str(repo)}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "foo.py"

    proc = sp.run(
        [sys.executable, str(WORKTREE_ONLY_WRITES)],
        input=json.dumps(v1),
        capture_output=True,
        text=True,
        env={**{k: v for k, v in os.environ.items() if k != "RIG_WORKTREE_ONLY"}, "HOME": str(tmp_path)},
        timeout=10,
    )
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert proc.returncode == 10


def test_worktree_only_writes_relative_hashline_path_blocks_despite_sibling_worktree_match(tmp_path):
    """agent-tools#615/#613: a relative hashline `[foo.py#TAG]` path is exactly the bare
    string the bridge extracts (`_hashline_file_paths` never resolves it to an absolute path).
    cwd is main; a different worktree happens to track a file at the same relative path.
    Guessing that as the real target was reverted as exploitable (see the sibling
    hook-level test); this proves the safe BLOCK verdict holds through the full bridge chain."""
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    feat = tmp_path / "feat"
    sp.run(["git", "worktree", "add", "-q", "-b", "feat/x", str(feat)], cwd=repo, check=True)
    (feat / "new_helper.py").write_text("x\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=feat, check=True)
    sp.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "add helper"], cwd=feat, check=True)

    patch = "[new_helper.py#A1B2]\nPUT >$:\n+y\n"
    omp_event = {"event": "tool_call", "toolName": "edit", "input": {"input": patch}, "cwd": str(repo)}
    v1 = dispatch.to_v1_event(omp_event, point="pre-write")
    assert v1["args"]["file_path"] == "new_helper.py"  # still bare/relative — the bridge does not resolve it

    proc = sp.run(
        [sys.executable, str(WORKTREE_ONLY_WRITES)],
        input=json.dumps(v1),
        capture_output=True,
        text=True,
        env={**{k: v for k, v in os.environ.items() if k != "RIG_WORKTREE_ONLY"}, "HOME": str(tmp_path)},
        timeout=10,
    )
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert proc.returncode == 10


# --- extension.ts loads without a syntax error --------------------------------------------------


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_parses_with_bun():
    """A broken extension.ts would silently no-op every omp session (the extension loader
    just records a load error and continues) — cheap insurance that it at least parses."""
    proc = subprocess.run(
        ["bun", "build", "--no-bundle", str(_EXTENSION_TS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_registers_tool_call_and_blocks_via_stub_dispatcher(tmp_path):
    """Functional smoke test: load the real extension.ts under bun with a stub `pi`/`ctx`,
    point OMP_HOOK_BRIDGE_PYTHON at a canned stub dispatcher, and confirm the exported
    factory registers `tool_call`/`tool_result` and translates an explicit block decision
    into `{block: true, reason}` — proving the extension's wiring, not just its syntax."""
    stub = tmp_path / "stub_dispatcher.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'block', 'reason': 'stub blocked it'}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        "const ctx = { cwd: '/tmp', hasUI: false };\n"
        "const result = await handlers['tool_call']({ toolName: 'bash', input: { command: 'ls' }, toolCallId: 'c1' }, ctx);\n"
        "console.log(JSON.stringify(result ?? null));\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(stub)

    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"block": True, "reason": "stub blocked it"}


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_fails_open_when_dispatcher_python_is_missing(tmp_path):
    """The ONE deliberate divergence from opencode's plugin: a broken/missing dispatcher on
    the `tool_call` (pre-execution) path must resolve to "allow" (undefined), never throw."""
    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        "const ctx = { cwd: '/tmp', hasUI: false };\n"
        "const result = await handlers['tool_call']({ toolName: 'bash', input: { command: 'ls' }, toolCallId: 'c1' }, ctx);\n"
        "console.log(JSON.stringify(result ?? null));\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(tmp_path / "does-not-exist-python3")

    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result is None


# --- the dispatcher runs ASYNCHRONOUSLY (agent-tools#558) ----------------------------------------
#
# omp is single-threaded: a SYNCHRONOUS child-process spawn in a `tool_call` handler freezes the
# whole TUI (no repaints, no keypresses) for as long as the dispatcher runs — up to the full
# Telegram-approval window of a hatch-capable descriptor. The handler must therefore await the
# dispatcher instead, which omp supports: its `emitToolCall` awaits whatever the handler returns.
#
# These drivers stub `pi`/`ctx` and point OMP_HOOK_BRIDGE_PYTHON at a canned dispatcher, so they
# need neither omp nor the real Python dispatcher.

_DRIVER_PROLOGUE = (
    "const mod = await import(process.env.EXTENSION_PATH!);\n"
    "const handlers: Record<string, Function> = {};\n"
    "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
    "mod.default(pi);\n"
    "const ctx = { cwd: '/tmp', hasUI: false };\n"
    "const event = { toolName: 'bash', input: { command: 'ls' }, toolCallId: 'c1' };\n"
)


def _write_stub_dispatcher(tmp_path: Path, body: str) -> Path:
    """A stub that stands in for `python3 -m omp_hook_bridge`: it ignores argv, drains stdin
    (the extension always writes a JSON payload there), then does whatever `body` says. `body`
    may call `ready()` once its setup (a pid file, a signal handler) is in place — the driver
    side waits for that marker (`_WAIT_READY`) before acting, because a fixed delay raced
    python's startup whenever this suite ran alongside other work on the machine."""
    stub = tmp_path / "stub_dispatcher.py"
    ready = tmp_path / "stub.ready"
    stub.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys, time\n"
        f"def ready():\n    pathlib.Path({str(ready)!r}).touch()\n"
        "sys.stdin.read()\n" + body,
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


# Driver-side: poll for the stub's readiness marker (see `_write_stub_dispatcher`).
_WAIT_READY = (
    "const { existsSync } = await import('node:fs');\n"
    "const waitReady = async () => { const p = process.env.STUB_READY!; const t0 = Date.now();\n"
    "  while (!existsSync(p)) { if (Date.now() - t0 > 20000) throw new Error('stub never became ready');\n"
    "    await new Promise((r) => setTimeout(r, 25)); } };\n"
)
# A budget long enough that the stub's setup is in place before it fires, even under load.
_BUDGET_MS = 3000


def _run_bun_driver(tmp_path: Path, source: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    driver = tmp_path / "driver.ts"
    driver.write_text(_DRIVER_PROLOGUE + source, encoding="utf-8")
    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["STUB_READY"] = str(tmp_path / "stub.ready")
    env.update(env_overrides)
    return subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_tool_call_does_not_block_the_event_loop(tmp_path):
    """agent-tools#558 (the whole point): while the dispatcher runs, omp's event loop must keep
    turning. A timer armed BEFORE the handler is awaited fires DURING a 1.5 s dispatch; with the
    old synchronous spawn it could not fire until the child had already exited."""
    stub = _write_stub_dispatcher(tmp_path, "time.sleep(1.5)\n")
    proc = _run_bun_driver(
        tmp_path,
        "let ticked = false;\n"
        "const timer = setTimeout(() => { ticked = true; }, 200);\n"
        "const result = await handlers['tool_call'](event, ctx);\n"
        "clearTimeout(timer);\n"
        "console.log(JSON.stringify({ ticked, result: result ?? null }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ticked"] is True, "the omp event loop was blocked for the whole dispatch"
    assert out["result"] is None


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_async_path_still_propagates_block_and_allow(tmp_path):
    """The asynchronous path must carry BOTH decisions through unchanged: an explicit block
    decision becomes `{block, reason}`, a silent exit-0 dispatcher stays "no opinion" (allow)."""
    blocker = _write_stub_dispatcher(
        tmp_path, "print(json.dumps({'decision': 'block', 'reason': 'stub blocked it'}))\n"
    )
    allower = tmp_path / "allow_dispatcher.py"
    allower.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n", encoding="utf-8")
    allower.chmod(0o755)

    driver = "console.log(JSON.stringify((await handlers['tool_call'](event, ctx)) ?? null));\n"
    blocked = _run_bun_driver(tmp_path, driver, {"OMP_HOOK_BRIDGE_PYTHON": str(blocker)})
    assert blocked.returncode == 0, blocked.stderr
    assert json.loads(blocked.stdout.strip().splitlines()[-1]) == {
        "block": True,
        "reason": "stub blocked it",
    }

    allowed = _run_bun_driver(tmp_path, driver, {"OMP_HOOK_BRIDGE_PYTHON": str(allower)})
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(allowed.stdout.strip().splitlines()[-1]) is None


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_async_path_fails_open_when_the_dispatcher_crashes(tmp_path):
    """Fail-OPEN is unchanged on the asynchronous path: a dispatcher that dies with a traceback
    on stderr and a non-zero exit resolves to "allow" (undefined) and never throws — a thrown
    handler error is how omp BLOCKS a call, which would brick a live session."""
    stub = _write_stub_dispatcher(tmp_path, "sys.stderr.write('boom\\n')\nsys.exit(1)\n")
    proc = _run_bun_driver(
        tmp_path,
        "let threw: string | null = null;\n"
        "let result: unknown = 'unset';\n"
        "try { result = (await handlers['tool_call'](event, ctx)) ?? null; }\n"
        "catch (error: any) { threw = String(error?.message ?? error); }\n"
        "console.log(JSON.stringify({ threw, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"threw": None, "result": None}
    # The dispatcher's own stderr still reaches the user — tagged per line with the event and the
    # tool-call id, so two overlapping dispatches stay attributable.
    assert "omp-hook-bridge[tool_call c1] boom" in proc.stderr.splitlines(), proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_honors_the_dispatcher_timeout_budget(tmp_path):
    """The timeout budget survives the rewrite: OMP_HOOK_BRIDGE_TIMEOUT_MS bounds the wait, the
    child is killed rather than left running, and the timeout resolves to "allow" (fail-open).
    Reading the env var per call — not once at module load — is what makes this testable."""
    stub = _write_stub_dispatcher(tmp_path, "time.sleep(30)\n")
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": "500"},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert out["elapsedMs"] < 10000, f"the 500 ms budget was not honored: {out['elapsedMs']} ms"
    assert "timed out" in proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_tool_result_awaits_the_dispatcher(tmp_path):
    """The post-write path must await too. If that `await` is ever dropped, `result` is a
    pending Promise, `result?.block` is undefined, and the "would have blocked" line silently
    stops being logged — a failure no `tool_call` test would notice."""
    stub = _write_stub_dispatcher(
        tmp_path, "print(json.dumps({'decision': 'block', 'reason': 'post-write says no'}))\n"
    )
    proc = _run_bun_driver(
        tmp_path,
        "await handlers['tool_result']({ toolName: 'write', input: { path: '/tmp/x' } }, ctx);\n"
        "console.log(JSON.stringify('done'));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "post-write hook would have blocked" in proc.stderr
    assert "post-write says no" in proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_explains_a_slow_tool_call_but_stays_quiet_on_tool_result(tmp_path):
    """A long wait is now silent by itself (the freeze used to be its own explanation), so the
    bridge says once, on stderr, why the call is being held and which omp setting decides how
    long it may be held. Only for `tool_call`: that is the event omp can refuse. A slow
    `tool_result` is nobody's problem — the write already landed — so it stays quiet."""
    stub = _write_stub_dispatcher(tmp_path, "time.sleep(1.5)\n")
    env = {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_NOTICE_MS": "300"}

    called = _run_bun_driver(tmp_path, "await handlers['tool_call'](event, ctx);\n", env)
    assert called.returncode == 0, called.stderr
    assert "holding this call for" in called.stderr
    assert "extensionHandlers.toolCallTimeoutMs" in called.stderr

    result = _run_bun_driver(
        tmp_path,
        "await handlers['tool_result']({ toolName: 'write', input: { path: '/tmp/x' } }, ctx);\n",
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "holding this call for" not in result.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_runs_overlapping_dispatches_independently(tmp_path):
    """The synchronous spawn serialized every dispatch as a side effect of blocking the loop;
    awaiting means two tool calls can now be in flight at once. Each must get its own child and
    its own decision — and the pair must take about as long as ONE dispatch, not two."""
    stub = tmp_path / "concurrent_dispatcher.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "payload = json.loads(sys.stdin.read())\n"
        "time.sleep(1.0)\n"
        "if payload['input'].get('command') == 'blocked':\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'no'}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const [a, b] = await Promise.all([\n"
        "  handlers['tool_call']({ toolName: 'bash', input: { command: 'blocked' } }, ctx),\n"
        "  handlers['tool_call']({ toolName: 'bash', input: { command: 'allowed' } }, ctx),\n"
        "]);\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, a: a ?? null, b: b ?? null }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["a"] == {"block": True, "reason": "no"}
    assert out["b"] is None
    assert out["elapsedMs"] < 1900, f"the two dispatches ran serially: {out['elapsedMs']} ms"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_timeout_sends_sigterm_first_and_lets_the_dispatcher_clean_up(tmp_path):
    """On a timeout the child gets SIGTERM first (what the old synchronous spawn sent), so a
    dispatcher that cleans up after itself still can: the stub traps SIGTERM, reports it on
    stderr and exits on its own — the bridge must then NOT wait out the SIGKILL grace."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "import signal\n"
        "def bye(*_a):\n"
        "    sys.stderr.write('stub: got SIGTERM, cleaning up\\n')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": str(_BUDGET_MS)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert "stub: got SIGTERM, cleaning up" in proc.stderr
    assert "timed out" in proc.stderr
    # budget + the dispatcher's own exit; well under budget + the 2000 ms SIGKILL grace.
    assert out["elapsedMs"] < _BUDGET_MS + 1500, f"the bridge waited out the SIGKILL grace: {out['elapsedMs']} ms"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_timeout_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    """A dispatcher that ignores SIGTERM is still ended: SIGKILL follows after the grace, so the
    handler settles (fail-open, "allow") instead of hanging on a child that never exits."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": str(_BUDGET_MS)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert "timed out" in proc.stderr
    # Must outlast budget + the 2000 ms grace (SIGTERM alone did nothing), never the 30 s sleep.
    assert _BUDGET_MS + 2000 <= out["elapsedMs"] < 15000, f"SIGKILL did not follow the grace: {out['elapsedMs']} ms"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_reaps_in_flight_dispatcher_when_the_process_exits(tmp_path):
    """omp can now EXIT mid-dispatch (the loop is no longer frozen), and the budget timers are
    unref'd, so nothing would end the child: the extension must SIGTERM every in-flight dispatcher
    on process exit instead of orphaning one that may be holding a Telegram hatch open."""
    pid_file = tmp_path / "stub.pid"
    stub = _write_stub_dispatcher(
        tmp_path,
        "import os\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "ready()\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        _WAIT_READY + "void handlers['tool_call'](event, ctx);\n"
        "await waitReady();\n"
        "process.exit(0);\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "in-flight dispatcher" in proc.stderr
    pid = int(pid_file.read_text())
    assert _pid_gone_within(pid, seconds=3), f"dispatcher {pid} was orphaned by the exiting host"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_reaps_in_flight_dispatcher_on_session_shutdown(tmp_path):
    """omp's own teardown path: a `session_shutdown` handler ends the running dispatcher too, so
    the awaited `tool_call` settles (fail-open) instead of outliving the session."""
    stub = _write_stub_dispatcher(tmp_path, "ready()\ntime.sleep(30)\n")
    proc = _run_bun_driver(
        tmp_path,
        _WAIT_READY + "const pending = handlers['tool_call'](event, ctx);\n"
        "await waitReady();\n"
        "const shutdownAt = Date.now();\n"
        "void handlers['session_shutdown']({}, ctx);\n"
        "const result = (await pending) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - shutdownAt, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert out["elapsedMs"] < 1000, f"the dispatcher outlived the shutdown's SIGTERM: {out['elapsedMs']} ms"
    assert "session shutdown" in proc.stderr
    assert "terminated by SIGTERM" in proc.stderr


def _pid_gone_within(pid: int, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_session_shutdown_escalates_to_sigkill(tmp_path):
    """Shutdown must not trust SIGTERM alone (the timeout path never did): a dispatcher that
    ignores it is SIGKILLed after the 1 s shutdown grace — inside omp's 2 s shutdown-handler cap —
    instead of surviving omp as an orphan holding a hatch open."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ready()\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        _WAIT_READY + "const pending = handlers['tool_call'](event, ctx);\n"
        "await waitReady();\n"
        "const shutdownAt = Date.now();\n"
        "void handlers['session_shutdown']({}, ctx);\n"
        "const result = (await pending) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - shutdownAt, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert 1000 <= out["elapsedMs"] < 4000, f"SIGKILL did not follow the shutdown grace: {out['elapsedMs']} ms"
    assert "terminated by SIGKILL" in proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_stop_reaches_the_dispatcher_process_group(tmp_path):
    """A hook script forked by the dispatcher (same process group) goes down with it: the child
    is spawned as a group leader and signalled as a group, so a budget timeout does not leave the
    dispatcher's own helpers running after the dispatcher itself is gone."""
    grandchild_pid = tmp_path / "grandchild.pid"
    stub = _write_stub_dispatcher(
        tmp_path,
        "import subprocess\n"
        "helper = subprocess.Popen(['sleep', '30'])\n"
        f"pathlib.Path({str(grandchild_pid)!r}).write_text(str(helper.pid))\n"
        "helper.wait()\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify(result));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub), "OMP_HOOK_BRIDGE_TIMEOUT_MS": str(_BUDGET_MS)},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) is None
    assert _pid_gone_within(int(grandchild_pid.read_text()), seconds=3), "the forked helper outlived the dispatcher"


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_caps_dispatcher_stdout(tmp_path):
    """A dispatcher that never stops writing stdout must not grow omp's memory until it dies (the
    old spawnSync had a bounded buffer): past 1 MiB the child is stopped and the call resolves
    fail-open, promptly, with the reason on stderr."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "sys.stdout.write('x' * (2 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "const started = Date.now();\n"
        "const result = (await handlers['tool_call'](event, ctx)) ?? null;\n"
        "console.log(JSON.stringify({ elapsedMs: Date.now() - started, result }));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is None
    assert out["elapsedMs"] < 8000, f"the overflowing dispatcher was not stopped: {out['elapsedMs']} ms"
    assert "output limit" in proc.stderr


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")
def test_extension_ts_flushes_a_stderr_line_that_never_ends(tmp_path):
    """An unterminated stderr stream is forwarded in bounded pieces (64 KiB) rather than held in
    memory waiting for a newline; each piece still carries the call tag."""
    stub = _write_stub_dispatcher(
        tmp_path,
        "sys.stderr.write('y' * (200 * 1024))\n"
        "sys.stderr.flush()\n",
    )
    proc = _run_bun_driver(
        tmp_path,
        "console.log(JSON.stringify((await handlers['tool_call'](event, ctx)) ?? null));\n",
        {"OMP_HOOK_BRIDGE_PYTHON": str(stub)},
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip().splitlines()[-1]) is None
    tagged = [line for line in proc.stderr.splitlines() if line.startswith("omp-hook-bridge[tool_call c1] y")]
    assert len(tagged) >= 3, f"expected the 200 KiB line in bounded pieces, got {len(tagged)} piece(s)"
    assert all(len(line) <= 64 * 1024 + 64 for line in tagged)
    assert sum(line.count("y") for line in tagged) == 200 * 1024
