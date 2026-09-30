"""The tg-ctl Stop-hook inbox (agent-tools#526 / tg-cli#306).

Two layers:
  - the shared KEY contract — the vectors here are byte-identical to tg-cli's
    ``tests/ctl-unreachable.test.ts`` (both sides derive the same inbox directory or the
    channel silently splits);
  - the consuming reader — pending → block reason + archive (at-most-once), and the
    fail-open cases: empty, missing, malformed, unwritable archive.
The last tests run the real ``omp_hook_bridge`` dispatcher (the inbox's only consumer)
and the ``cc_hook_bridge`` / ``codex_hook_bridge`` dispatchers (which must NOT consume it:
cc/codex are tmux-only, tg-cli#456) as a SUBPROCESS (how the harness invokes them) against
a temp ``TG_CTL_CONFIG_DIR``.
"""

from __future__ import annotations

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

from agenttools_tg_inbox import (  # noqa: E402
    agent_key,
    consume_pending,
    format_block_reason,
    inbox_dir,
    sanitize_agent_name,
    stop_inbox_text,
)
from agenttools_tg_inbox import core as inbox_core  # noqa: E402
import omp_hook_bridge.dispatch as omp_dispatch  # noqa: E402


def _entry(i: int, wrapped: str) -> str:
    return json.dumps({"id": i, "ts": "2026-09-06T00:00:00Z", "from": "Alex", "text": wrapped, "wrapped": wrapped})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_CTL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOOKS_DEBUG_LOG", str(tmp_path / "hooks-debug.log"))
    return tmp_path


def _hooks_debug_log(cfg: Path) -> str:
    path = Path(os.environ.get("HOOKS_DEBUG_LOG", str(cfg / "hooks-debug.log")))
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- the shared key contract (mirror of ctl-unreachable.test.ts) ---


def test_agent_key_name_wins_and_is_sanitized():
    assert agent_key("landing", "/x") == "landing"
    assert agent_key("my agent/1", "/x") == "my_agent_1"
    assert agent_key("rig-fable.v2", "/x") == "rig-fable.v2"
    assert agent_key("x" * 100, "/x") == "x" * 64


def test_agent_key_cwd_hash_normalizes_trailing_slash():
    assert agent_key(None, "/Users/ultra/work/landing") == "cwd-ccfe64bae2f277d7"
    assert agent_key(None, "/Users/ultra/work/landing/") == "cwd-ccfe64bae2f277d7"
    assert agent_key("", "/") == "cwd-8a5edab282632443"


def test_sanitize_agent_name_empty_and_dots_only_are_none():
    assert sanitize_agent_name("") is None
    assert sanitize_agent_name(".") is None
    assert sanitize_agent_name("..") is None  # never a path escape out of inbox/
    assert sanitize_agent_name("ok-1") == "ok-1"
    assert agent_key("..", "/") == "cwd-8a5edab282632443"
    assert inbox_dir(agent_key("..", "/")).parent.name == "inbox"


# --- the consuming reader ---


def test_consume_pending_returns_entries_and_archives_at_most_once(cfg):
    d = inbox_dir("landing")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "[TG from Alex tg#1] first") + "\n" + _entry(2, "[TG from Alex tg#2] second") + "\n")
    entries = consume_pending("landing", session_id="sess-1")
    assert [e["wrapped"] for e in entries] == ["[TG from Alex tg#1] first", "[TG from Alex tg#2] second"]
    assert format_block_reason(entries) == "[TG from Alex tg#1] first\n\n[TG from Alex tg#2] second"
    assert not (d / "pending.jsonl").exists()
    assert not list(d.glob("claim-*"))
    assert not list(d.glob("tmp-*"))
    batches = list(d.glob("delivered-*.jsonl"))
    assert len(batches) == 1  # ONE complete batch file per consumption
    assert batches[0].stat().st_mode & 0o777 == 0o600
    assert d.stat().st_mode & 0o777 == 0o700
    delivered = [json.loads(line) for line in batches[0].read_text().splitlines()]
    assert [x["id"] for x in delivered] == [1, 2]
    assert all(x["session_id"] == "sess-1" and x["delivered_ts"] for x in delivered)
    # second call: nothing pending → nothing delivered again, no second batch
    assert consume_pending("landing") == []
    assert len(list(d.glob("delivered-*.jsonl"))) == 1


def test_consume_pending_missing_or_empty_never_blocks(cfg):
    assert consume_pending("nobody") == []
    d = inbox_dir("empty")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("")
    assert consume_pending("empty") == []
    assert not (d / "pending.jsonl").exists()


def test_consume_pending_malformed_lines_are_archived_flagged_not_delivered(cfg):
    d = inbox_dir("mixed")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("not json\n" + json.dumps({"nope": 1}) + "\n" + _entry(7, "ok") + "\n")
    entries = consume_pending("mixed")
    assert [e["wrapped"] for e in entries] == ["ok"]
    (batch,) = d.glob("delivered-*.jsonl")
    delivered = [json.loads(line) for line in batch.read_text().splitlines()]
    assert [x.get("malformed", False) for x in delivered] == [True, True, False]
    assert "2 malformed" in _hooks_debug_log(cfg)


def test_consume_pending_archive_failure_delivers_nothing_and_keeps_claim(cfg, monkeypatch):
    d = inbox_dir("stuck")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "x") + "\n")
    monkeypatch.setattr(inbox_core, "_write_private", lambda path, text: (_ for _ in ()).throw(OSError("disk full")))
    assert consume_pending("stuck") == []
    assert list(d.glob("claim-*"))  # records preserved for a human, not lost
    assert not list(d.glob("delivered-*"))
    assert "could not publish" in _hooks_debug_log(cfg)


def test_consume_pending_never_overwrites_a_stale_claim(cfg):
    # A claim left by an earlier archive failure must survive a later consume even if the
    # pid repeats: claim names are unique (pid + nanoseconds + random), so no rename can
    # land on it.
    d = inbox_dir("reuse")
    d.mkdir(parents=True)
    stale = d / f"claim-{os.getpid()}-1-deadbeef.jsonl"
    stale.write_text(_entry(9, "old, never archived") + "\n")
    (d / "pending.jsonl").write_text(_entry(1, "new") + "\n")
    assert [e["id"] for e in consume_pending("reuse")] == [1]
    assert stale.exists() and "old, never archived" in stale.read_text()


# --- through the real bridges, as a subprocess ---


def _run_bridge(module: str, event: dict, *, cfg: Path, home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    env.update({"HOME": str(home), "TG_CTL_CONFIG_DIR": str(cfg), "PYTHONPATH": str(_LIB), "HOOKS_DEBUG_LOG": str(cfg / "hooks-debug.log")})
    return subprocess.run(
        [sys.executable, "-m", module, "Stop"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _run_omp_bridge(event: dict, *, cfg: Path, home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    hooks = home / ".omp" / "agent" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home),
        "TG_CTL_CONFIG_DIR": str(cfg),
        "PYTHONPATH": str(_LIB),
        "OMP_HOOKS_DIR": str(hooks),
        "HOOKS_DEBUG_LOG": str(cfg / "hooks-debug.log"),
    })
    return subprocess.run(
        [sys.executable, "-m", "omp_hook_bridge", "tool_call"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize("module", ["cc_hook_bridge", "codex_hook_bridge"])
def test_cc_codex_stop_leaves_the_omp_inbox_untouched(tmp_path, module):
    # cc/codex are tmux-only (tg-cli#456): tg-ctl never queues for them, and an unnamed
    # cc/codex in an omp's cwd shares the omp's cwd-<hash> key — its Stop must not steal
    # the omp's grant notice.
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    pending = _entry(42, "[TG from tg-ctl] hatch granted") + "\n"
    (d / "pending.jsonl").write_text(pending)
    event = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/Users/ultra/work/landing"}
    res = _run_bridge(module, event, cfg=cfg, home=home)
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert (d / "pending.jsonl").read_text() == pending
    assert sorted(p.name for p in d.iterdir()) == ["pending.jsonl"]


def test_omp_bridge_yield_delivers_pending_inbox(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(7, "[TG from Alex tg#7] hello omp") + "\n")
    event = {"toolName": "yield", "cwd": cwd, "input": {}, "toolCallId": "y1"}
    res = _run_omp_bridge(event, cfg=cfg, home=home)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"decision": "block", "reason": "[TG from Alex tg#7] hello omp"}
    assert len(list(d.glob("delivered-*.jsonl"))) == 1
    res2 = _run_omp_bridge(event, cfg=cfg, home=home)
    assert res2.stdout == ""


def test_omp_bridge_bash_does_not_consume_inbox(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    pending = d / "pending.jsonl"
    pending.write_text(_entry(7, "[TG from Alex tg#7] hello omp") + "\n")
    res = _run_omp_bridge(
        {"toolName": "bash", "cwd": cwd, "input": {"command": "true"}, "toolCallId": "b1"},
        cfg=cfg,
        home=home,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert pending.exists()
    assert not list(d.glob("delivered-*"))


def test_omp_bridge_yield_empty_and_malformed_never_block(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    cwd = "/Users/ultra/work/landing"
    event = {"toolName": "yield", "cwd": cwd, "input": {}, "toolCallId": "y1"}
    res = _run_omp_bridge(event, cfg=cfg, home=home)
    assert res.returncode == 0 and res.stdout == ""
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("{{{ garbage\n")
    res = _run_omp_bridge(event, cfg=cfg, home=home)
    assert res.returncode == 0 and res.stdout == ""
    assert "malformed" in (cfg / "hooks-debug.log").read_text(encoding="utf-8")


def test_omp_bridge_ignores_inherited_claude_pid_and_keys_on_cwd(tmp_path):
    # omp has no --name; an omp started from a Claude-owned shell inherits CLAUDE_PID.
    # Nothing reads CLAUDE_PID today — this pins the cwd-hash key against a reintroduced
    # process lookup keying the omp on the Claude session's --name.
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    home.mkdir()
    hooks = home / ".omp" / "agent" / "hooks"
    hooks.mkdir(parents=True)
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(7, "for omp") + "\n")
    named = cfg / "inbox" / "rig-fable"
    named.mkdir(parents=True)
    (named / "pending.jsonl").write_text(_entry(6, "for the claude session") + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")}
    env.update({
        "HOME": str(home),
        "TG_CTL_CONFIG_DIR": str(cfg),
        "PYTHONPATH": str(_LIB),
        "OMP_HOOKS_DIR": str(hooks),
        "CLAUDE_PID": str(os.getppid()),
    })
    res = subprocess.run(
        [sys.executable, "-m", "omp_hook_bridge", "tool_call"],
        input=json.dumps({"toolName": "yield", "cwd": cwd, "input": {}, "toolCallId": "y1"}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert json.loads(res.stdout) == {"decision": "block", "reason": "for omp"}
    assert (named / "pending.jsonl").exists()  # untouched


def test_omp_bridge_stop_hook_block_and_inbox_ride_in_one_block(tmp_path):
    cfg = tmp_path / "cfg"
    home = tmp_path / "home"
    hooks = home / ".omp" / "agent" / "hooks"
    hooks.mkdir(parents=True)
    gate = tmp_path / "gate.py"
    gate.write_text("#!/usr/bin/env python3\nimport sys, json; print(json.dumps({'message': 'stay: gate says no'})); sys.exit(10)\n")
    gate.chmod(0o755)
    (hooks / "gate.stop.json").write_text(json.dumps({
        "id": "gate", "point": "stop", "cmd": str(gate), "priority": 10, "timeout_ms": 3000, "on_error": "closed",
    }))
    cwd = "/Users/ultra/work/landing"
    d = cfg / "inbox" / "cwd-ccfe64bae2f277d7"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "[TG from Alex tg#1] hi") + "\n")
    event = {"toolName": "yield", "cwd": cwd, "input": {}, "toolCallId": "y1"}
    res = _run_omp_bridge(event, cfg=cfg, home=home)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["decision"] == "block"
    assert out["reason"].startswith("[TG from Alex tg#1] hi\n\n")
    assert "gate says no" in out["reason"]
    res2 = _run_omp_bridge(event, cfg=cfg, home=home)
    assert json.loads(res2.stdout)["reason"].strip() == "stay: gate says no"


# --- review-round hardening (PR #563, round 3) ---


def test_consume_pending_invalid_utf8_keeps_claim_and_warns(cfg):
    d = inbox_dir("landing")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_bytes(b'{"wrapped": "\xff\xfe broken"}\n')
    assert consume_pending("landing") == []
    assert "could not read" in _hooks_debug_log(cfg)
    assert len(list(d.glob("claim-*"))) == 1  # kept on disk for a human, not lost


def test_consume_pending_blank_wrapped_is_malformed_not_silently_eaten(cfg):
    d = inbox_dir("landing")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(json.dumps({"id": 1, "wrapped": "   "}) + "\n")
    assert consume_pending("landing") == []
    assert "1 malformed" in _hooks_debug_log(cfg)
    batch = next(d.glob("delivered-*.jsonl"))
    assert json.loads(batch.read_text())["malformed"] is True


def test_stop_inbox_text_never_calls_key_fn_without_an_inbox_root(cfg):
    def boom() -> str:
        raise AssertionError("key lookup must not run when no inbox exists")

    warnings: list[str] = []
    assert stop_inbox_text(boom, session_id="", warn=warnings.append) == ""
    assert warnings == []


def test_stop_inbox_text_is_fail_open_on_a_broken_key_lookup(cfg):
    inbox_dir("x").mkdir(parents=True)

    def boom() -> str:
        raise RuntimeError("key lookup exploded")

    warnings: list[str] = []
    assert stop_inbox_text(boom, session_id="", warn=warnings.append) == ""
    assert warnings and "fail-open" in warnings[0]


def test_omp_dispatch_runs_the_stop_hooks_before_consuming_the_inbox(cfg, monkeypatch):
    # Consuming is irreversible; a hook that raises must not eat a message the agent
    # never saw (all three round-3 reviewers).
    d = inbox_dir("cwd-ccfe64bae2f277d7")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "[TG from Alex tg#1] hi") + "\n")

    def exploding_hooks(hook_event_name, ev):
        raise RuntimeError("hook discovery crashed")

    monkeypatch.setattr(omp_dispatch, "_first_hook_block", exploding_hooks)
    event = {"toolName": "yield", "cwd": "/Users/ultra/work/landing", "input": {}, "toolCallId": "y1"}
    with pytest.raises(RuntimeError):
        omp_dispatch.dispatch("tool_call", event)
    assert (d / "pending.jsonl").exists()
    assert not list(d.glob("delivered-*"))


def test_combine_stop_parts_is_the_one_shared_rule():
    from agenttools_tg_inbox import combine_stop_parts

    assert combine_stop_parts("", None) is None
    assert combine_stop_parts("", "") == ""  # an empty-reason block still blocks
    assert combine_stop_parts("msg", None) == "msg"
    assert combine_stop_parts("msg", "gate") == "msg\n\ngate"


def test_combine_block_empty_hook_reason_still_blocks():
    # `("block", "")` from a fail-closed gate must not become an allow (round-3 finding).
    assert omp_dispatch._combine_block("", "") == omp_dispatch.omp_block_output("")
    assert omp_dispatch._combine_block("", None) is None
    assert omp_dispatch._combine_block("msg", "gate") == omp_dispatch.omp_block_output("msg\n\ngate")


def test_consume_pending_requeues_a_record_appended_to_the_claim_mid_consume(cfg, monkeypatch):
    # The daemon appends with open-then-write; a claim rename between the two lands the
    # record on the CLAIMED inode after the reader's snapshot (round-1 finding). It must
    # come back as a fresh pending.jsonl, not die with the claim.
    d = inbox_dir("landing")
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text(_entry(1, "[TG from Alex tg#1] first") + "\n")
    real_write = inbox_core._write_private

    def late_append_then_write(path, text):
        claim = next(d.glob("claim-*"))
        with claim.open("a") as fh:
            fh.write(_entry(2, "[TG from Alex tg#2] late") + "\n")
        real_write(path, text)

    monkeypatch.setattr(inbox_core, "_write_private", late_append_then_write)
    assert [e["id"] for e in consume_pending("landing")] == [1]
    assert "requeued to pending.jsonl" in _hooks_debug_log(cfg)
    assert not list(d.glob("claim-*"))
    assert (d / "pending.jsonl").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(inbox_core, "_write_private", real_write)
    assert [e["id"] for e in consume_pending("landing")] == [2]
    assert not (d / "pending.jsonl").exists()
