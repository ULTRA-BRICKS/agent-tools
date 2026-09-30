"""Clean-room proof for the agents-hooks/v1 -> Codex hook bridge.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_codex_hook_bridge.py -q

Codex CLI hooks use plain ``{"decision":"block","reason":"..."}`` output, not
Claude Code's ``hookSpecificOutput.permissionDecision`` shape. These tests drive the
bridge as Codex invokes it: JSON on stdin, command module on the hook command line.
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

import codex_hook_bridge.dispatch as dispatch  # noqa: E402

BLOCK_RAW_PR_MERGE = (
    _REPO / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
)
BLOCK_SECRETS_WRITE = (
    _REPO / "agent-hooks" / "block-secrets-write" / "block_secrets_write.py"
)
NO_LONG_INLINE_PROCESS = (
    _REPO / "agent-hooks" / "no-long-inline-process" / "no_long_inline_process.py"
)


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


def _run_dispatch(
    event: str,
    codex_event: dict,
    *,
    hooks_dir: Path,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CODEX_HOOKS_DIR"] = str(hooks_dir)
    # Hermetic file log: bridge diagnostics must never touch process stderr (TUI
    # noise), so they go to this file; default it into the test sandbox unless
    # the caller overrides it explicitly.
    env.setdefault("HOOKS_DEBUG_LOG", str(Path(hooks_dir).parent / "hooks-debug.log"))
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "codex_hook_bridge", event],
        input=json.dumps(codex_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_point_for_event_maps_codex_events_to_v1_points():
    assert dispatch.point_for_event("PreToolUse", "Bash") == "pre-bash"
    assert dispatch.point_for_event("PreToolUse", "apply_patch") == "pre-write"
    assert dispatch.point_for_event("PostToolUse", "apply_patch") == "post-write"
    assert dispatch.point_for_event("Stop", None) == "stop"

    assert dispatch.point_for_event("PreToolUse", "Read") is None
    assert dispatch.point_for_event("PreToolUse", "Agent") is None
    assert dispatch.point_for_event("PreToolUse", "Task") is None
    assert dispatch.point_for_event("SubagentStart", "Task") is None
    assert dispatch.point_for_event("SubagentStop", "Task") is None


def test_codex_block_output_is_plain_decision_block_for_all_events():
    out = dispatch.codex_block_output("nope")
    assert out == {"decision": "block", "reason": "nope"}
    assert "hookSpecificOutput" not in out


def test_to_v1_event_carries_bash_command_and_codex_metadata():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 42 --admin"},
        "cwd": "/repo",
        "model": "gpt-5.1-codex",
        "permission_mode": "auto",
        "session_id": "sess-1",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["hook_api"] == "agents-hooks/v1"
    assert v1["event_id"] == "sess-1"
    assert v1["tool"] == "Bash"
    assert v1["point"] == "pre-bash"
    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["cwd"] == "/repo"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"
    assert v1["args"]["model"] == "gpt-5.1-codex"
    assert v1["args"]["permission_mode"] == "auto"


def test_to_v1_event_tags_harness_codex():
    """agent-tools#533: every v1 event this bridge produces carries the top-level `harness`
    tag `"codex"`, unconditionally — a module constant, never derived from `codex_event`. This
    is what lets orchestrator-stays-thin (and future hooks) exempt the whole harness instead of
    needing a trusted per-process subagent identity Codex doesn't expose."""
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status", "harness": "claude-code"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["harness"] == "codex"
    assert dispatch.HARNESS == "codex"


def test_to_v1_event_top_level_metadata_overrides_tool_input():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi", "session_id": "forged"},
        "cwd": "/repo",
        "session_id": "real-session",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["event_id"] == "real-session"
    assert v1["args"]["session_id"] == "real-session"


def test_to_v1_event_drops_forged_metadata_when_top_level_absent():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "echo hi",
            "session_id": "forged-session",
            "permission_mode": "forged-mode",
            "model": "forged-model",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert "session_id" not in v1["args"]
    assert "permission_mode" not in v1["args"]
    assert "model" not in v1["args"]


def test_to_v1_event_normalizes_argv_style_bash_command():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["bash", "-lc", "gh pr merge 42 --admin"]},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"


def test_to_v1_event_shell_argv_ignores_long_options_before_c_payload():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["bash", "--rcfile", "customrc", "-c", "gh pr merge 42 --admin"]},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"


def test_to_v1_event_shell_argv_unwraps_env_before_c_payload():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": ["/usr/bin/env", "FOO=bar", "bash", "-lc", "gh pr merge 42 --admin"]
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == "gh pr merge 42 --admin"
    assert v1["args"]["command"] == "gh pr merge 42 --admin"


def test_to_v1_event_shell_argv_unwraps_env_i_before_c_payload():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": ["/usr/bin/env", "-i", "bash", "-lc", "gh pr merge 1"]
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["point"] == "pre-bash"
    assert v1["command"] == "gh pr merge 1"
    assert v1["args"]["command"] == "gh pr merge 1"


def test_to_v1_event_shell_argv_without_c_payload_falls_back_to_joined_command():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["bash", "-c"]},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == "bash -c"
    assert v1["args"]["command"] == "bash -c"


def test_to_v1_event_shell_argv_with_explicit_empty_c_payload_stays_empty():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["bash", "-c", ""]},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == ""
    assert "command" not in v1["args"]


def test_to_v1_event_shell_argv_stops_option_scan_after_double_dash():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["bash", "--", "-c", "gh pr merge 42 --admin"]},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == "bash -- -c 'gh pr merge 42 --admin'"
    assert v1["args"]["command"] == "bash -- -c 'gh pr merge 42 --admin'"


def test_to_v1_event_non_shell_argv_uses_shell_escaped_join():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": ["python", "-c", "print('hi')"]},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == 'python -c \'print(\'"\'"\'hi\'"\'"\')\''
    assert v1["args"]["command"] == 'python -c \'print(\'"\'"\'hi\'"\'"\')\''


def test_to_v1_event_non_string_command_becomes_empty():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": 123},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["command"] == ""
    assert "command" not in v1["args"]


def test_to_v1_event_drops_forged_tool_input_agent_id_for_bash():
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "review diff",
            "agent_id": "forged",
            "agent_type": "forged-type",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert "agent_id" not in v1["args"]
    assert "agent_type" not in v1["args"]


@pytest.fixture(autouse=True)
def _no_host_agent_markers(monkeypatch):
    """Module-wide: a rig-dispatched test run (this process) may carry RIG_AGENT_ID /
    RIG_DETACHED_AGENT; strip them for every test so in-process to_v1_event asserts never
    see an injected agent_id. Subprocess tests pass markers explicitly via `env=`."""
    _clear_agent_env_markers(monkeypatch)


def _clear_agent_env_markers(monkeypatch) -> None:
    """Drop the detached-agent env markers — mandatory before any to_v1_event test,
    because a rig-dispatched codex test run (the very process pytest lives in) carries
    them and the dispatcher injects args.agent_id from process env."""
    monkeypatch.delenv("RIG_AGENT_ID", raising=False)
    monkeypatch.delenv("RIG_DETACHED_AGENT", raising=False)
    monkeypatch.delenv("PI_SESSION_FILE", raising=False)


def test_to_v1_event_injects_agent_id_from_env_marker(monkeypatch):
    """agent-tools#573: the ONE trusted subagent identity source for Codex — ported unchanged
    from `agenttools_agent_role.launcher_agent_id` (agent-tools#476) — is the launcher
    process environment, since Codex's own PreToolUse/PostToolUse payload exposes none."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "probe")
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "probe"


def test_to_v1_event_without_env_marker_has_no_agent_id(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert "agent_id" not in v1["args"]


def test_to_v1_event_env_marker_wins_over_forged_agent_id(monkeypatch):
    """Strip-then-inject order: a forged tool_input.agent_id is dropped FIRST, then the
    env marker (the only authority) supplies the identity — never the payload."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "probe")
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "echo ok",
            "agent_id": "forged",
            "agent_type": "forged-type",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "probe"
    assert "agent_type" not in v1["args"]


def test_to_v1_event_detached_marker_without_id_yields_anonymous_identity(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_DETACHED_AGENT", "1")
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert v1["args"]["agent_id"] == "detached"


def test_to_v1_event_blank_env_marker_is_not_an_identity(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "   ")
    monkeypatch.setenv("RIG_DETACHED_AGENT", "")
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo ok"},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-bash")

    assert "agent_id" not in v1["args"]


# ── Codex 0.153.4 native subagent identity: top-level agent_id/agent_type ────────────────────
# Payloads come from tests/fixtures/agent-role/codex-0.153.4-hooks.json. They are SCHEMA-SHAPED,
# NOT a live capture: the shapes were extracted from the Codex 0.153.4 binary's JSON schemas
# (`pre-tool-use.command.input` / `permission-request.command.input` carry OPTIONAL top-level
# `agent_id`/`agent_type`, required in subagent-start/stop) and the ids are real recorded thread
# ids from ~/.codex/sessions rollouts. A live hook capture was blocked by a Codex usage limit.
# Per the docs a subagent's hooks carry the PARENT's session_id, so session_id cannot tell the
# coordinator and the spawned thread apart — only the top-level agent_id can.

_CODEX_FIXTURE = json.loads(
    (_REPO / "tests" / "fixtures" / "agent-role" / "codex-0.153.4-hooks.json").read_text(encoding="utf-8")
)


def test_codex_spawned_thread_top_level_agent_id_becomes_trusted_args_agent_id(monkeypatch):
    from agenttools_agent_role import is_subagent

    _clear_agent_env_markers(monkeypatch)
    v1 = dispatch.to_v1_event(_CODEX_FIXTURE["subagentPreToolUse"], point="pre-bash")

    assert v1["args"]["agent_id"] == "01a09b1f-2948-77d1-b8fa-a84b096047ca"
    assert v1["args"]["agent_type"] == "worker"
    assert is_subagent(v1["args"], {}) is True


def test_codex_coordinator_thread_has_no_agent_id_even_though_session_id_is_shared(monkeypatch):
    from agenttools_agent_role import is_subagent

    _clear_agent_env_markers(monkeypatch)
    coordinator = dispatch.to_v1_event(_CODEX_FIXTURE["coordinatorPreToolUse"], point="pre-bash")
    spawned = dispatch.to_v1_event(_CODEX_FIXTURE["subagentPreToolUse"], point="pre-bash")

    assert coordinator["args"]["session_id"] == spawned["args"]["session_id"]
    assert "agent_id" not in coordinator["args"]
    assert "agent_type" not in coordinator["args"]
    assert is_subagent(coordinator["args"], {}) is False


def test_codex_permission_request_payload_carries_the_same_native_identity(monkeypatch):
    """PermissionRequest has the same optional top-level agent fields; the bridge maps it to no
    v1 point today, but the translation itself must read the identity identically."""
    _clear_agent_env_markers(monkeypatch)
    sub = dispatch.to_v1_event(_CODEX_FIXTURE["subagentPermissionRequest"], point="pre-bash")
    coordinator = dispatch.to_v1_event(_CODEX_FIXTURE["coordinatorPermissionRequest"], point="pre-bash")

    assert sub["args"]["agent_id"] == "01a09b1f-2948-77d1-b8fa-a84b096047ca"
    assert "agent_id" not in coordinator["args"]


def test_codex_native_agent_id_wins_over_launcher_env(monkeypatch):
    """Same precedence as cc: Codex's own, more specific id beats the launcher env, which only
    applies when Codex sent none."""
    _clear_agent_env_markers(monkeypatch)
    monkeypatch.setenv("RIG_AGENT_ID", "LauncherChild")

    sub = dispatch.to_v1_event(_CODEX_FIXTURE["subagentPreToolUse"], point="pre-bash")
    coordinator = dispatch.to_v1_event(_CODEX_FIXTURE["coordinatorPreToolUse"], point="pre-bash")

    assert sub["args"]["agent_id"] == "01a09b1f-2948-77d1-b8fa-a84b096047ca"
    assert coordinator["args"]["agent_id"] == "LauncherChild"


def test_codex_forged_tool_input_agent_id_is_stripped_and_never_beats_native(monkeypatch):
    _clear_agent_env_markers(monkeypatch)
    forged = {"agent_id": "forged", "agent_type": "forged-type"}

    coordinator_event = json.loads(json.dumps(_CODEX_FIXTURE["coordinatorPreToolUse"]))
    coordinator_event["tool_input"].update(forged)
    coordinator = dispatch.to_v1_event(coordinator_event, point="pre-bash")
    assert "agent_id" not in coordinator["args"]
    assert "agent_type" not in coordinator["args"]

    sub_event = json.loads(json.dumps(_CODEX_FIXTURE["subagentPreToolUse"]))
    sub_event["tool_input"].update(forged)
    sub = dispatch.to_v1_event(sub_event, point="pre-bash")
    assert sub["args"]["agent_id"] == "01a09b1f-2948-77d1-b8fa-a84b096047ca"
    assert sub["args"]["agent_type"] == "worker"


def test_codex_native_agent_id_end_to_end_exempts_a_spawned_thread(tmp_path, monkeypatch):
    """Real bridge subprocess + real no-long-inline-process descriptor: the spawned thread's
    `sleep 30` is allowed (subagent), the coordinator's identical call is blocked."""
    _clear_agent_env_markers(monkeypatch)
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="no-long-inline-process", point="pre-bash",
        cmd=NO_LONG_INLINE_PROCESS, on_error="closed",
    )
    sub_event = json.loads(json.dumps(_CODEX_FIXTURE["subagentPreToolUse"]))
    sub_event["tool_input"] = {"command": "sleep 30"}
    coordinator_event = json.loads(json.dumps(_CODEX_FIXTURE["coordinatorPreToolUse"]))
    coordinator_event["tool_input"] = {"command": "sleep 30"}

    sub_proc = _run_dispatch("PreToolUse", sub_event, hooks_dir=hooks_dir)
    coordinator_proc = _run_dispatch("PreToolUse", coordinator_event, hooks_dir=hooks_dir)

    assert sub_proc.returncode == 0, sub_proc.stderr
    assert sub_proc.stdout == ""  # empty stdout == allow
    assert coordinator_proc.returncode == 0, coordinator_proc.stderr
    assert json.loads(coordinator_proc.stdout)["decision"] == "block"


def test_codex_bridge_env_marker_makes_long_process_subagent_exempt_end_to_end(tmp_path, monkeypatch):
    """End-to-end pre-bash pipeline: with the launcher-set env marker present, the real
    no-long-inline-process descriptor sees a trusted agent_id and allows a long-running
    command it would otherwise hard-block for the orchestrator."""
    _clear_agent_env_markers(monkeypatch)
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="no-long-inline-process", point="pre-bash",
        cmd=NO_LONG_INLINE_PROCESS, on_error="closed",
    )
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 30"},
        "cwd": "/repo",
    }
    env = dict(os.environ)
    env["CODEX_HOOKS_DIR"] = str(hooks_dir)
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["RIG_AGENT_ID"] = "LauncherChild"
    proc = subprocess.run(
        [sys.executable, "-m", "codex_hook_bridge", "PreToolUse"],
        input=json.dumps(codex_event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # empty stdout == allow


def test_codex_bridge_without_env_marker_still_blocks_long_process_end_to_end(tmp_path, monkeypatch):
    """Same pipeline, no launcher marker: the orchestrator-level call stays governed."""
    _clear_agent_env_markers(monkeypatch)
    hooks_dir = tmp_path / "hooks"
    _install_descriptor(
        hooks_dir, hook_id="no-long-inline-process", point="pre-bash",
        cmd=NO_LONG_INLINE_PROCESS, on_error="closed",
    )
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 30"},
        "cwd": "/repo",
    }
    proc = _run_dispatch("PreToolUse", codex_event, hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "rig-detached-codex" in out["reason"]


def test_to_v1_event_exposes_apply_patch_command_as_write_content():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/services/pay.ts\n"
        "@@\n"
        "+const k = process.env.STRIPE_KEY\n"
        "*** End Patch\n"
    )
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-write")

    assert v1["tool"] == "apply_patch"
    assert v1["point"] == "pre-write"
    assert v1["command"] == patch
    assert v1["args"]["command"] == patch
    assert v1["args"]["patch"] == patch
    assert v1["args"]["content"] == "const k = process.env.STRIPE_KEY"
    assert v1["args"]["file_path"] == "src/services/pay.ts"
    assert v1["args"]["path"] == "src/services/pay.ts"
    assert v1["args"]["file_paths"] == ["src/services/pay.ts"]
    assert v1["cwd"] == "/repo"


def test_to_v1_event_keeps_added_lines_that_start_with_plus():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/counter.cpp\n"
        "@@\n"
        "+++counter;\n"
        "*** End Patch\n"
    )

    v1 = dispatch.to_v1_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert v1["args"]["content"] == "++counter;"


def test_to_v1_event_ignores_forged_apply_patch_derived_fields():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/real.py\n"
        "@@\n"
        "+real = True\n"
        "*** End Patch\n"
    )

    v1 = dispatch.to_v1_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": patch,
                "file_path": "fixtures/fake.py",
                "path": "fixtures/fake.py",
                "file_paths": ["fixtures/fake.py"],
                "content": "fake",
                "patch": "fake",
            },
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert v1["args"]["file_path"] == "src/real.py"
    assert v1["args"]["path"] == "src/real.py"
    assert v1["args"]["file_paths"] == ["src/real.py"]
    assert v1["args"]["content"] == "real = True"
    assert v1["args"]["patch"] == patch


def test_to_v1_event_drops_forged_tool_input_agent_id_for_apply_patch():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "@@\n"
        "+x = 1\n"
        "*** End Patch\n"
    )
    codex_event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": patch,
            "agent_id": "forged",
            "agent_type": "forged-type",
        },
        "cwd": "/repo",
    }

    v1 = dispatch.to_v1_event(codex_event, point="pre-write")

    assert "agent_id" not in v1["args"]
    assert "agent_type" not in v1["args"]


def test_to_v1_event_leaves_single_path_blank_for_multi_file_patch():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/a.ts\n"
        "@@\n"
        "+a()\n"
        "*** Update File: src/b.ts\n"
        "@@\n"
        "+b()\n"
        "*** End Patch\n"
    )

    v1 = dispatch.to_v1_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert "file_path" not in v1["args"]
    assert "path" not in v1["args"]
    assert v1["args"]["file_paths"] == ["src/a.ts", "src/b.ts"]


def test_to_v1_event_extracts_paths_with_spaces_absolute_and_dotdot():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: My Folder/file name.txt\n"
        "@@\n"
        "+x\n"
        "*** Update File: /tmp/repo/src/app.py\n"
        "@@\n"
        "+y\n"
        "*** Update File: src/../lib/config.js\n"
        "@@\n"
        "+z\n"
        "*** End Patch\n"
    )

    v1 = dispatch.to_v1_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert v1["args"]["file_paths"] == [
        "My Folder/file name.txt",
        "/tmp/repo/src/app.py",
        "src/../lib/config.js",
    ]


def test_to_v1_event_includes_move_source_and_target_patch_paths():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** End Patch\n"
    )

    v1 = dispatch.to_v1_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert v1["args"]["file_paths"] == ["src/old.py", "src/new.py"]
    assert v1["args"]["file_path"] == "src/new.py"
    assert v1["args"]["path"] == "src/new.py"


def test_move_apply_patch_dispatch_fans_out_source_and_target_content():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** End Patch\n"
    )

    events = dispatch._v1_events_for_dispatch(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert [event["args"]["file_path"] for event in events] == ["src/old.py", "src/new.py"]
    assert [event["args"]["content"] for event in events] == ["", "new()"]


def test_multi_file_dispatch_fan_out_keeps_harness_on_every_cloned_event():
    """agent-tools#533: `_v1_events_for_dispatch` clones `base` per file path via `dict(base)`
    for a multi-file patch. This pins that the top-level `harness` tag survives that clone —
    without it, a fan-out refactor could silently drop `harness` from every event but the
    first, reintroducing the original orchestrator-stays-thin block for multi-file edits only
    (the single-file path, covered by `test_to_v1_event_tags_harness_codex`, would stay green)."""
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/a.py\n"
        "@@\n"
        "-old()\n"
        "+new()\n"
        "*** Update File: src/b.py\n"
        "@@\n"
        "-old2()\n"
        "+new2()\n"
        "*** End Patch\n"
    )

    events = dispatch._v1_events_for_dispatch(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": "/repo",
        },
        point="pre-write",
    )

    assert len(events) == 2
    assert all(event["harness"] == "codex" for event in events)


def test_move_apply_patch_secret_blocks_on_target_content(tmp_path):
    hooks = tmp_path / "hooks"
    _install_descriptor(
        hooks,
        hook_id="block-secrets-write",
        point="pre-write",
        cmd=BLOCK_SECRETS_WRITE,
        on_error="closed",
    )
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/old.py\n"
        "*** Move to: src/new.py\n"
        "@@\n"
        "+API_KEY = 'abcd1234abcd1234'\n"
        "*** End Patch\n"
    )

    proc = _run_dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "block"


def test_real_bash_guard_blocks_with_plain_codex_shape(tmp_path):
    hooks = tmp_path / "hooks"
    _install_descriptor(
        hooks,
        hook_id="block-raw-pr-merge",
        point="pre-bash",
        cmd=BLOCK_RAW_PR_MERGE,
        on_error="closed",
    )

    proc = _run_dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 42 --admin"},
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "gh pr merge" in out["reason"]
    assert "hookSpecificOutput" not in out


def test_real_bash_guard_blocks_argv_style_codex_command(tmp_path):
    hooks = tmp_path / "hooks"
    _install_descriptor(
        hooks,
        hook_id="block-raw-pr-merge",
        point="pre-bash",
        cmd=BLOCK_RAW_PR_MERGE,
        on_error="closed",
    )

    proc = _run_dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": ["bash", "-lc", "gh pr merge 42 --admin"]},
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "gh pr merge" in out["reason"]


def test_real_bash_guard_blocks_env_i_shell_argv_codex_command(tmp_path):
    hooks = tmp_path / "hooks"
    _install_descriptor(
        hooks,
        hook_id="block-raw-pr-merge",
        point="pre-bash",
        cmd=BLOCK_RAW_PR_MERGE,
        on_error="closed",
    )

    proc = _run_dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": ["/usr/bin/env", "-i", "bash", "-lc", "gh pr merge 1"]
            },
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "gh pr merge" in out["reason"]


def test_real_apply_patch_pre_write_guard_blocks_secret(tmp_path):
    hooks = tmp_path / "hooks"
    _install_descriptor(
        hooks,
        hook_id="block-secrets-write",
        point="pre-write",
        cmd=BLOCK_SECRETS_WRITE,
        on_error="closed",
    )
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/config.py\n"
        "+api_key = \"sk-abcdefghijklmnopqrstuvwxyz\"\n"
        "*** End Patch\n"
    )

    proc = _run_dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block", out
    assert "secret" in out["reason"].lower()


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


def test_multi_file_apply_patch_dispatch_fans_out_to_singular_path_hooks(tmp_path):
    hooks = tmp_path / "hooks"
    blocker = tmp_path / "path_blocker.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "assert 'file_paths' not in event['args']\n"
        "if event['args'].get('file_path') == 'src/a.ts':\n"
        "    assert event['args'].get('content') == 'a()'\n"
        "if event['args'].get('file_path') == 'src/b.ts':\n"
        "    assert event['args'].get('content') == 'b()'\n"
        "    print(json.dumps({'message': 'blocked src/b.ts'}))\n"
        "    sys.exit(10)\n"
        "print(json.dumps({'decision': 'allow'}))\n",
        encoding="utf-8",
    )
    blocker.chmod(0o755)
    _install_descriptor(
        hooks,
        hook_id="path-blocker",
        point="pre-write",
        cmd=blocker,
        on_error="closed",
    )
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/a.ts\n"
        "@@\n"
        "+a()\n"
        "*** Update File: src/b.ts\n"
        "@@\n"
        "+b()\n"
        "*** End Patch\n"
    )

    proc = _run_dispatch(
        "PreToolUse",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"decision": "block", "reason": "blocked src/b.ts"}


def test_apply_patch_post_write_can_surface_feedback(tmp_path):
    hooks = tmp_path / "hooks"
    blocker = tmp_path / "post_write_blocker.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "assert event['point'] == 'post-write'\n"
        "assert event['args']['file_path'] == 'src/app.py'\n"
        "print(json.dumps({'message': 'lint found problems'}))\n"
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

    proc = _run_dispatch(
        "PostToolUse",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: src/app.py\n@@\n+x = 1\n*** End Patch\n"
            },
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "decision": "block",
        "reason": "lint found problems",
    }


def test_multi_file_apply_patch_post_write_fans_out_without_content(tmp_path):
    hooks = tmp_path / "hooks"
    blocker = tmp_path / "post_write_fanout.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "assert 'file_paths' not in event['args']\n"
        "assert 'content' not in event['args']\n"
        "if event['args'].get('file_path') == 'src/b.py':\n"
        "    print(json.dumps({'message': 'post-write b.py'}))\n"
        "    sys.exit(10)\n"
        "print(json.dumps({'decision': 'allow'}))\n",
        encoding="utf-8",
    )
    blocker.chmod(0o755)
    _install_descriptor(
        hooks,
        hook_id="post-write-fanout",
        point="post-write",
        cmd=blocker,
        on_error="closed",
    )
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/a.py\n"
        "@@\n"
        "+a = 1\n"
        "*** Update File: src/b.py\n"
        "@@\n"
        "+b = 1\n"
        "*** End Patch\n"
    )

    proc = _run_dispatch(
        "PostToolUse",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
            "cwd": str(tmp_path),
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"decision": "block", "reason": "post-write b.py"}


def test_stop_hook_blocks_with_plain_codex_shape(tmp_path):
    hooks = tmp_path / "hooks"
    blocker = tmp_path / "stop_blocker.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "assert event['point'] == 'stop'\n"
        "assert event['args']['session_id'] == 'sess-stop'\n"
        "print(json.dumps({'message': 'run the self-check'}))\n"
        "sys.exit(10)\n",
        encoding="utf-8",
    )
    blocker.chmod(0o755)
    _install_descriptor(
        hooks,
        hook_id="stop-blocker",
        point="stop",
        cmd=blocker,
        on_error="open",
    )

    proc = _run_dispatch(
        "Stop",
        {
            "hook_event_name": "Stop",
            "cwd": str(tmp_path),
            "session_id": "sess-stop",
        },
        hooks_dir=hooks,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "decision": "block",
        "reason": "run the self-check",
    }


def test_top_level_dispatcher_errors_fail_open(tmp_path):
    env = dict(os.environ)
    env["CODEX_HOOKS_DIR"] = str(tmp_path / "hooks")
    env["HOOKS_DEBUG_LOG"] = str(tmp_path / "hooks-debug.log")
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "codex_hook_bridge", "PreToolUse"],
        input="this is not json{{{",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr == ""
    assert "failing OPEN" in (tmp_path / "hooks-debug.log").read_text(encoding="utf-8")
