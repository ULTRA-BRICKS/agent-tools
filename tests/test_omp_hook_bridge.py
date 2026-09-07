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
