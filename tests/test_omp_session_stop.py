"""omp's main-session `session_stop` event must reach the v1 `stop` point.

Until now only the hidden `yield` tool (a subagent's last call) mapped to `stop`, so an
interactive/orchestrator omp session — which ends its turn with plain text and never yields —
never ran a Stop hook (agent-tools#1003). omp documents `session_stop` for main sessions
(omp://extensions.md: awaited before settle, `{decision: "block", reason}` keeps the session
going, never fires for task/subagent sessions).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

import omp_hook_bridge.dispatch as dispatch

from test_omp_hook_bridge import _EXTENSION_TS, _install_descriptor, _run_dispatch


def _session_stop_event(tmp_path, **extra):
    event = {
        "event": "session_stop",
        "cwd": str(tmp_path),
        "sessionId": "01a16493-main",
        "sessionFile": "/home/u/.omp/agent/sessions/-proj/2026-10-24_01a16493-main.jsonl",
        "lastAssistantMessage": "All done, PR merged.",
        "turnId": 4,
        "stopHookActive": False,
        "isMainSession": True,
    }
    event.update(extra)
    return event


def test_point_for_event_maps_session_stop_to_stop():
    assert dispatch.point_for_event("session_stop", None) == "stop"
    assert dispatch.point_for_event("session_stop", "") == "stop"


def test_session_stop_is_a_known_event():
    assert "session_stop" in dispatch._KNOWN_EVENTS


def test_session_stop_v1_event_carries_trusted_identity_and_turn_facts(tmp_path):
    ev = dispatch.to_v1_event(_session_stop_event(tmp_path), point="stop")
    assert ev["point"] == "stop"
    assert ev["harness"] == "omp"
    assert ev["tool"] == "session_stop"
    assert ev["event_id"] == "01a16493-main"
    args = ev["args"]
    assert args["session_id"] == "01a16493-main"
    assert args["transcript_path"].endswith("01a16493-main.jsonl")
    assert args["last_assistant_message"] == "All done, PR merged."
    assert args["turn_id"] == 4
    assert args["stop_hook_active"] is False
    # a main session is never a subagent: no agent identity is invented for it
    assert "agent_id" not in args


def test_yield_v1_event_also_forwards_the_transcript_path(tmp_path):
    ev = dispatch.to_v1_event(
        {
            "toolName": "yield",
            "toolCallId": "y1",
            "cwd": str(tmp_path),
            "input": {},
            "sessionId": "child-1",
            "sessionFile": "/s/main/Helper.jsonl",
        },
        point="stop",
    )
    assert ev["args"]["transcript_path"] == "/s/main/Helper.jsonl"
    # an agent-name session_id may be shared by helpers of different parents; the real omp
    # session id is forwarded separately so a ledger key can be unique
    assert ev["args"]["harness_session_id"] == "child-1"


def test_forged_harness_session_id_in_tool_input_is_dropped(tmp_path):
    ev = dispatch.to_v1_event(
        {"toolName": "yield", "toolCallId": "y1", "cwd": str(tmp_path), "input": {"harness_session_id": "forged"}},
        point="stop",
    )
    assert "harness_session_id" not in ev["args"]


def test_forged_transcript_path_in_tool_input_never_reaches_the_stop_event(tmp_path):
    ev = dispatch.to_v1_event(
        {
            "toolName": "yield",
            "toolCallId": "y1",
            "cwd": str(tmp_path),
            "input": {"transcript_path": "/etc/passwd", "last_assistant_message": "forged"},
            "sessionId": "child-1",
        },
        point="stop",
    )
    assert "transcript_path" not in ev["args"]
    assert "last_assistant_message" not in ev["args"]


def test_bridge_blocks_session_stop_via_stop_descriptor(tmp_path):
    hooks_dir = tmp_path / "hooks"
    blocker = tmp_path / "block_stop.py"
    blocker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "ev = json.load(sys.stdin)\n"
        "assert ev['point'] == 'stop' and ev['args']['session_id'] == '01a16493-main', ev\n"
        "print(json.dumps({'hook_api': 'agents-hooks/v1', 'decision': 'block', 'message': 'finish the todo'}))\n"
        "sys.exit(10)\n",
        encoding="utf-8",
    )
    blocker.chmod(0o755)
    _install_descriptor(hooks_dir, hook_id="stop-block", point="stop", cmd=blocker)
    proc = _run_dispatch("session_stop", _session_stop_event(tmp_path), hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "finish the todo" in out["reason"]


def test_bridge_allows_session_stop_when_the_stop_hook_allows(tmp_path):
    hooks_dir = tmp_path / "hooks"
    allower = tmp_path / "allow_stop.py"
    allower.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    allower.chmod(0o755)
    _install_descriptor(hooks_dir, hook_id="stop-allow", point="stop", cmd=allower)
    proc = _run_dispatch("session_stop", _session_stop_event(tmp_path), hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_session_stop_ignores_non_stop_descriptors(tmp_path):
    hooks_dir = tmp_path / "hooks"
    blocker = tmp_path / "block_bash.py"
    blocker.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(10)\n", encoding="utf-8")
    blocker.chmod(0o755)
    _install_descriptor(hooks_dir, hook_id="bash-block", point="pre-bash", cmd=blocker)
    proc = _run_dispatch("session_stop", _session_stop_event(tmp_path), hooks_dir=hooks_dir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# --- extension.ts wiring (bun) -------------------------------------------------------------------

_needs_bun = pytest.mark.skipif(shutil.which("bun") is None, reason="bun not on PATH")


def _run_stop_driver(tmp_path, *, stub_body: str, event_ts: str, ctx_ts: str = "{ cwd: '/tmp', hasUI: false }"):
    """Load the real extension.ts, call its registered `session_stop` handler with
    `event_ts`, and return (handler result, payload the stub dispatcher received or None)."""
    dump = tmp_path / "payload.json"
    stub = tmp_path / "stub_dispatcher.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        f"open({str(dump)!r}, 'w').write(json.dumps(payload))\n" + stub_body,
        encoding="utf-8",
    )
    stub.chmod(0o755)
    driver = tmp_path / "driver.ts"
    driver.write_text(
        "const mod = await import(process.env.EXTENSION_PATH!);\n"
        "const handlers: Record<string, Function> = {};\n"
        "const pi = { on: (name: string, cb: Function) => { handlers[name] = cb; } };\n"
        "mod.default(pi);\n"
        "if (typeof handlers['session_stop'] !== 'function') { console.log('NO_HANDLER'); process.exit(0); }\n"
        f"const ctx = {ctx_ts};\n"
        f"const result = await handlers['session_stop']({event_ts}, ctx);\n"
        "console.log(JSON.stringify(result ?? null));\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["EXTENSION_PATH"] = str(_EXTENSION_TS)
    env["OMP_HOOK_BRIDGE_PYTHON"] = str(stub)
    proc = subprocess.run(["bun", "run", str(driver)], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert last != "NO_HANDLER", "extension.ts does not register session_stop"
    payload = json.loads(dump.read_text()) if dump.exists() else None
    return json.loads(last), payload


_BLOCK_STUB = "print(json.dumps({'decision': 'block', 'reason': 'unfinished todo: ship it'}))\n"
_ALLOW_STUB = "print(json.dumps({'decision': 'allow'}))\n"

_STOP_EVENT_TS = (
    "{ type: 'session_stop', messages: [{ role: 'user' }], turn_id: 3,"
    " last_assistant_message: { role: 'assistant', content: ["
    "{ type: 'thinking', thinking: 'secret' }, { type: 'text', text: 'Done.' }, { type: 'text', text: 'Merged.' }] },"
    " session_id: 'sess-main-1', session_file: '/s/-proj/sess-main-1.jsonl', stop_hook_active: true,"
    " signal: new AbortController().signal }"
)


@_needs_bun
def test_extension_registers_session_stop_and_returns_block_decision(tmp_path):
    result, _ = _run_stop_driver(tmp_path, stub_body=_BLOCK_STUB, event_ts=_STOP_EVENT_TS)
    assert result == {"decision": "block", "reason": "unfinished todo: ship it"}


@_needs_bun
def test_extension_session_stop_allows_when_dispatcher_allows(tmp_path):
    result, _ = _run_stop_driver(tmp_path, stub_body=_ALLOW_STUB, event_ts=_STOP_EVENT_TS)
    assert result is None


@_needs_bun
def test_extension_session_stop_payload_carries_event_identity_and_assistant_text(tmp_path):
    _, payload = _run_stop_driver(tmp_path, stub_body=_ALLOW_STUB, event_ts=_STOP_EVENT_TS)
    assert payload["event"] == "session_stop"
    assert payload["sessionId"] == "sess-main-1"
    assert payload["sessionFile"] == "/s/-proj/sess-main-1.jsonl"
    assert payload["lastAssistantMessage"] == "Done.\nMerged."
    assert payload["turnId"] == 3
    assert payload["stopHookActive"] is True
    # the full message history is never shipped to the dispatcher
    assert "messages" not in payload


@_needs_bun
def test_extension_session_stop_aborted_signal_skips_the_dispatcher(tmp_path):
    event_ts = (
        "(() => { const c = new AbortController(); c.abort();"
        " return { type: 'session_stop', messages: [], turn_id: 1, session_id: 's', signal: c.signal }; })()"
    )
    result, payload = _run_stop_driver(tmp_path, stub_body=_BLOCK_STUB, event_ts=event_ts)
    assert result is None
    assert payload is None, "an interrupted session must not run (or block on) Stop hooks"


@_needs_bun
def test_extension_session_stop_fails_open_when_dispatcher_crashes(tmp_path):
    result, _ = _run_stop_driver(tmp_path, stub_body="sys.exit(3)\n", event_ts=_STOP_EVENT_TS)
    assert result is None
