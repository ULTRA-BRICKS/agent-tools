"""Codex subagent identity from the child thread's ROLLOUT ``session_meta`` (agent-tools#573).

Primary source: the 2026-09-05 audit of the codex sessions in ``~/work/private`` (codex-cli
0.153.4). Every ``orchestrator-stays-thin`` / ``no-long-inline-process`` refusal there landed on a
thread whose rollout ``session_meta`` says ``thread_source: "subagent"`` with a ``parent_thread_id``
— a delegate, refused as if it were the orchestrator, because the bridge of the day stripped every
identity. The records below are those sessions' real ``session_meta`` lines (only the ~20 KB
``base_instructions`` text is trimmed); the commands are the real refused tool calls
(``scratchpad/audit2/blocks_raw.jsonl`` rows, cited per test), replayed through the REAL bridge and
the REAL hook scripts, in-process, with every other identity source pinned off.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import codex_hook_bridge.dispatch as dispatch  # noqa: E402

_NO_LONG_INLINE = _REPO / "agent-hooks" / "no-long-inline-process" / "no_long_inline_process.py"
_ORCH_THIN = _REPO / "agent-hooks" / "orchestrator-stays-thin" / "orchestrator_stays_thin.py"

PARENT_ID = "01a07175-ba7e-70f1-b741-719469d0ffd6"
CHILD_ID = "01a0717d-2948-7992-9373-d3a3058b71d0"
CHILD_ROLLOUT = f"rollout-2026-09-05T16-13-43-{CHILD_ID}.jsonl"
PARENT_ROLLOUT = f"rollout-2026-09-05T16-05-36-{PARENT_ID}.jsonl"

# `~/.codex/sessions/2026/09/05/rollout-2026-09-05T16-13-43-01a0717d-…3b71d0.jsonl`, line 1 (the
# audit's session `16-13`, nickname "Singer", spawned by `16-05`). `session_id` is the PARENT's.
_SUBAGENT_SESSION_META = {
    "timestamp": "2026-09-05T12:13:43.158Z", "ordinal": 0, "type": "session_meta",
    "payload": {
        "session_id": PARENT_ID, "id": CHILD_ID, "forked_from_id": PARENT_ID,
        "parent_thread_id": PARENT_ID, "timestamp": "2026-09-05T12:13:43.136Z",
        "cwd": "/Users/ultra/work/private", "originator": "codex-tui", "cli_version": "0.153.4",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": PARENT_ID, "depth": 1, "agent_path": "/root/triage_base",
            "agent_nickname": "Singer", "agent_role": None}}},
        "thread_source": "subagent", "agent_nickname": "Singer", "agent_path": "/root/triage_base",
        "model_provider": "openai", "history_mode": "paginated",
        "subagent_history_start_ordinal": 10, "multi_agent_version": "v2",
        "context_window": {"window_id": "01a0717d-2948-7992-9373-d3bc16fd4c37"},
        "git": {"commit_hash": "4e2aab08e2ea2999e7fa9ea93329a10d37753ded", "branch": "conloca-app",
                "repository_url": "https://github.com/conloca/private.git"},
        "base_instructions": {"text": "<trimmed: the full codex base instructions, ~20 KB>"},
    },
}

# `…/rollout-2026-09-05T16-05-36-01a07175-…ffd6.jsonl`, line 1 — the ORCHESTRATOR (`16-05`).
_ORCHESTRATOR_SESSION_META = {
    "timestamp": "2026-09-05T12:12:07.755Z", "ordinal": 0, "type": "session_meta",
    "payload": {
        "session_id": PARENT_ID, "id": PARENT_ID, "timestamp": "2026-09-05T12:05:36.031Z",
        "cwd": "/Users/ultra/work/private", "originator": "codex-tui", "cli_version": "0.153.4",
        "source": "cli", "thread_source": "user", "model_provider": "openai",
        "history_mode": "paginated",
        "context_window": {"window_id": "01a07175-ba7e-70f1-b741-71a02a1f6994"},
        "git": {"commit_hash": "4e2aab08e2ea2999e7fa9ea93329a10d37753ded", "branch": "conloca-app",
                "repository_url": "https://github.com/conloca/private.git"},
        "base_instructions": {"text": "<trimmed>"},
    },
}

# blocks_raw.jsonl row 11 — session 16-13, 2026-09-05T13:20:37.762Z, refused by
# no-long-inline-process ("Dispatch it as an Agent with `subagent_type: \"fork\"`" — CC advice
# handed to a codex delegate).
REVIEW_DIFF_CMD = (
    "review diff -C /Users/ultra/work/private/.worktrees/ultra-typecheck-proof"
    " -o /tmp/conloca-ultra-typecheck-review.md"
)
# blocks_raw.jsonl row 2 — session 16-13, 2026-09-05T12:14:17.871Z, refused by
# orchestrator-stays-thin ("By design, not a bug: this session is the orchestrator …"): the PR
# inventory Alex had asked for, the audit's single most expensive row.
GH_PR_LIST_CMD = (
    "gh pr list --repo conloca/private --state open --limit 100 --json number,title,headRefName,"
    "baseRefName,updatedAt,createdAt,url,isDraft,mergeable,mergeStateStatus,reviewDecision,author;"
    " gh repo view conloca/private --json defaultBranchRef; gh pr list --repo conloca/conloca"
    " --state open --limit 100 --json number,title,headRefName,baseRefName,updatedAt,url,isDraft"
)


def _write_rollout(sessions_dir: Path, name: str, record: dict) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / name
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _codex_event(command: str, transcript: Path, *, cwd: str, session_id: str = PARENT_ID) -> dict:
    """A codex Bash PreToolUse as codex emits it (the captured 0.153.4 shape), WITHOUT the
    top-level ``agent_id`` — so only the rollout source can identify the child."""
    return {
        "session_id": session_id,
        "turn_id": "01a0717d-2a00-7000-8000-000000000001",
        "transcript_path": str(transcript),
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "model": "gpt-5.6-sol",
        "permission_mode": "bypassPermissions",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "exec-replay",
    }


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "codex-home" / "sessions" / "2026" / "09" / "05"


@pytest.fixture
def child_rollout(sessions_dir: Path) -> Path:
    return _write_rollout(sessions_dir, CHILD_ROLLOUT, _SUBAGENT_SESSION_META)


@pytest.fixture
def parent_rollout(sessions_dir: Path) -> Path:
    return _write_rollout(sessions_dir, PARENT_ROLLOUT, _ORCHESTRATOR_SESSION_META)


# ── the rollout source on its own ─────────────────────────────────────────────────────────────

def test_rollout_identity_from_a_real_spawned_thread_session_meta(child_rollout, tmp_path):
    event = _codex_event("echo hi", child_rollout, cwd=str(tmp_path))
    assert dispatch._rollout_subagent_identity(event) == (CHILD_ID, "subagent")

    v1 = dispatch.to_v1_event(event, point="pre-bash")
    assert v1["args"]["agent_id"] == CHILD_ID
    assert v1["args"]["agent_type"] == "subagent"


def test_rollout_identity_absent_for_the_orchestrator_session_meta(parent_rollout, tmp_path):
    event = _codex_event("echo hi", parent_rollout, cwd=str(tmp_path))
    assert dispatch._rollout_subagent_identity(event) == ("", "")
    v1 = dispatch.to_v1_event(event, point="pre-bash")
    assert "agent_id" not in v1["args"]


def test_rollout_identity_ignores_a_transcript_path_in_tool_input(child_rollout, tmp_path):
    """The path is read from the hook event ROOT (codex-written) only — a model-authored
    ``tool_input.transcript_path`` pointing at a real child rollout changes nothing."""
    event = _codex_event("echo hi", tmp_path / "nowhere.jsonl", cwd=str(tmp_path))
    event["tool_input"]["transcript_path"] = str(child_rollout)
    v1 = dispatch.to_v1_event(event, point="pre-bash")
    assert "agent_id" not in v1["args"]
    assert "transcript_path" not in v1["args"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.__setitem__("thread_source", "user"), id="thread_source-user"),
        pytest.param(lambda p: p.__setitem__("parent_thread_id", "01a00000-0000-7000-8000-000000000000"),
                     id="parent-is-not-the-event-session"),
        pytest.param(lambda p: p.__setitem__("id", PARENT_ID), id="id-equals-session_id"),
        pytest.param(lambda p: p.pop("parent_thread_id"), id="no-parent"),
        pytest.param(lambda p: p.pop("id"), id="no-id"),
    ],
)
def test_rollout_identity_requires_every_consistency_fact(sessions_dir, tmp_path, mutate):
    record = json.loads(json.dumps(_SUBAGENT_SESSION_META))
    mutate(record["payload"])
    rollout = _write_rollout(sessions_dir, CHILD_ROLLOUT, record)
    event = _codex_event("echo hi", rollout, cwd=str(tmp_path))
    assert dispatch._rollout_subagent_identity(event) == ("", "")


def test_rollout_identity_requires_the_filename_to_carry_the_thread_id(sessions_dir, tmp_path):
    """An orchestrator editing its OWN rollout's first line into a subagent record still has a
    file named after its own session id — the name/`id` mismatch keeps it governed."""
    rollout = _write_rollout(sessions_dir, PARENT_ROLLOUT, _SUBAGENT_SESSION_META)
    event = _codex_event("echo hi", rollout, cwd=str(tmp_path))
    assert dispatch._rollout_subagent_identity(event) == ("", "")


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("", id="empty-file"),
        pytest.param("not json\n", id="not-json"),
        pytest.param(json.dumps({"type": "turn_context", "payload": {"thread_source": "subagent"}}) + "\n",
                     id="not-a-session_meta"),
        pytest.param(json.dumps({"type": "session_meta", "payload": "x"}) + "\n", id="payload-not-a-dict"),
        pytest.param("[1, 2]\n", id="json-not-an-object"),
    ],
)
def test_rollout_identity_fails_closed_on_a_malformed_first_line(sessions_dir, tmp_path, first_line):
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / CHILD_ROLLOUT
    rollout.write_text(first_line, encoding="utf-8")
    event = _codex_event("echo hi", rollout, cwd=str(tmp_path))
    assert dispatch._rollout_subagent_identity(event) == ("", "")


def test_rollout_identity_fails_closed_on_missing_file_or_oversized_line(sessions_dir, tmp_path, monkeypatch):
    event = _codex_event("echo hi", sessions_dir / CHILD_ROLLOUT, cwd=str(tmp_path))
    assert dispatch._rollout_subagent_identity(event) == ("", "")

    monkeypatch.setattr(dispatch, "_ROLLOUT_META_MAX_BYTES", 64)
    _write_rollout(sessions_dir, CHILD_ROLLOUT, _SUBAGENT_SESSION_META)
    assert dispatch._rollout_subagent_identity(event) == ("", "")


def test_rollout_identity_needs_an_event_session_id(child_rollout, tmp_path):
    event = _codex_event("echo hi", child_rollout, cwd=str(tmp_path), session_id="")
    assert dispatch._rollout_subagent_identity(event) == ("", "")


def test_native_top_level_agent_id_still_wins_over_the_rollout(child_rollout, tmp_path):
    event = _codex_event("echo hi", child_rollout, cwd=str(tmp_path))
    event["agent_id"] = "native-child"
    event["agent_type"] = "default"
    v1 = dispatch.to_v1_event(event, point="pre-bash")
    assert (v1["args"]["agent_id"], v1["args"]["agent_type"]) == ("native-child", "default")


# ── the audited refusals, replayed through the real bridge + real hooks ───────────────────────

def _descriptor(hooks_dir: Path, hook_id: str, cmd: Path) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / f"{hook_id}.pre-bash.json").write_text(json.dumps({
        "id": hook_id, "point": "pre-bash", "cmd": str(cmd), "priority": 35,
        "timeout_ms": 30000, "on_error": "closed", "description": f"replay descriptor for {hook_id}",
    }) + "\n", encoding="utf-8")


@pytest.fixture
def hermetic_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The real hook scripts wired as the only descriptors; every OTHER identity source and every
    bypass/hatch env pinned off, so the rollout is the sole thing that can relax a gate."""
    hooks_dir = tmp_path / "hooks"
    _descriptor(hooks_dir, "no-long-inline-process", _NO_LONG_INLINE)
    _descriptor(hooks_dir, "orchestrator-stays-thin", _ORCH_THIN)
    monkeypatch.setenv("CODEX_HOOKS_DIR", str(hooks_dir))
    monkeypatch.setenv("ORCH_THIN_MARKER_DIR", str(tmp_path / "orch-thin-markers"))
    for key in list(os.environ):
        if key.startswith("RIG_HATCH_REQUEST_") or key.startswith("ALLOW_"):
            monkeypatch.delenv(key, raising=False)
    for key in ("RIG_AGENT_ID", "RIG_DETACHED_AGENT", "RIG_ORCHESTRATOR_ONLY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dispatch.subagent_identity, "ancestor_agent_id", lambda harness: "")
    return hooks_dir


def _replay(command: str, transcript: Path, cwd: Path) -> dict | None:
    return dispatch.dispatch("PreToolUse", _codex_event(command, transcript, cwd=str(cwd)))


def test_replay_review_diff_from_the_spawned_thread_is_allowed(hermetic_hooks, child_rollout, tmp_path):
    """Row 11: the mandated pre-commit review, run by delegate "Singer" — ALLOW now."""
    assert _replay(REVIEW_DIFF_CMD, child_rollout, tmp_path) is None


def test_replay_review_diff_from_the_orchestrator_is_refused_with_the_codex_recipe(
    hermetic_hooks, parent_rollout, tmp_path,
):
    out = _replay(REVIEW_DIFF_CMD, parent_rollout, tmp_path)
    assert out is not None and out["decision"] == "block"
    reason = out["reason"]
    assert "Run this in a BACKGROUND subagent" in reason
    assert "collaboration.spawn_agent" in reason
    assert "rig-detached-codex" in reason
    assert 'subagent_type: "fork"' not in reason  # the CC-only advice the audit's refusals carried


def test_replay_gh_pr_inventory_from_the_spawned_thread_is_allowed_on_repeat(
    hermetic_hooks, child_rollout, tmp_path,
):
    """Row 2: `orchestrator-stays-thin` is warn-then-block; a delegate is never even warned."""
    assert _replay(GH_PR_LIST_CMD, child_rollout, tmp_path) is None
    assert _replay(GH_PR_LIST_CMD, child_rollout, tmp_path) is None
    markers = tmp_path / "orch-thin-markers"
    assert not markers.exists() or not list(markers.glob("*.warned"))


def test_replay_gh_pr_inventory_from_the_orchestrator_warns_then_blocks_with_the_codex_recipe(
    hermetic_hooks, parent_rollout, tmp_path,
):
    assert _replay(GH_PR_LIST_CMD, parent_rollout, tmp_path) is None  # first offense: WARN (allow)
    out = _replay(GH_PR_LIST_CMD, parent_rollout, tmp_path)
    assert out is not None and out["decision"] == "block"
    assert "By design, not a bug: this session is the orchestrator" in out["reason"]
    assert "collaboration.spawn_agent" in out["reason"]
    assert "rig-detached-codex" in out["reason"]


def test_replay_forged_tool_input_agent_id_from_the_orchestrator_is_still_refused(
    hermetic_hooks, parent_rollout, tmp_path,
):
    event = _codex_event(REVIEW_DIFF_CMD, parent_rollout, cwd=str(tmp_path))
    event["tool_input"]["agent_id"] = CHILD_ID
    event["tool_input"]["agent_type"] = "subagent"
    out = dispatch.dispatch("PreToolUse", event)
    assert out is not None and out["decision"] == "block"
    assert "collaboration.spawn_agent" in out["reason"]
