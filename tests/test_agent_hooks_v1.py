"""Direct tests for the shared agents-hooks/v1 descriptor runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agent_hooks_v1 import load_descriptors, run_hook  # noqa: E402


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "hook.py"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _warnings() -> tuple[list[str], object]:
    seen: list[str] = []

    def warn(msg: str) -> None:
        seen.append(msg)

    return seen, warn


def test_load_descriptors_filters_point_and_sorts_bad_priority_at_default(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    cmd = _script(tmp_path, "import sys\nsys.exit(0)\n")
    (hooks / "bad-json.json").write_text("{", encoding="utf-8")
    (hooks / "late.pre-bash.json").write_text(
        json.dumps({"id": "late", "point": "pre-bash", "cmd": str(cmd), "priority": "high"}),
        encoding="utf-8",
    )
    (hooks / "early.pre-bash.json").write_text(
        json.dumps({"id": "early", "point": "pre-bash", "cmd": str(cmd), "priority": 10}),
        encoding="utf-8",
    )
    (hooks / "other.stop.json").write_text(
        json.dumps({"id": "other", "point": "stop", "cmd": str(cmd), "priority": 1}),
        encoding="utf-8",
    )
    seen, warn = _warnings()

    specs = load_descriptors("pre-bash", hooks, warn=warn)

    assert [spec["id"] for spec in specs] == ["early", "late"]
    assert any("skipping unreadable descriptor" in msg for msg in seen)


def test_run_hook_exit10_uses_protocol_message(tmp_path):
    cmd = _script(
        tmp_path,
        "import json, sys\n"
        "print(json.dumps({'message': 'blocked for test'}))\n"
        "sys.exit(10)\n",
    )
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "blocker", "point": "pre-bash", "cmd": str(cmd), "on_error": "open"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert outcome == "block"
    assert reason == "blocked for test"
    assert seen == []


def test_run_hook_on_error_closed_blocks_crash(tmp_path):
    cmd = _script(tmp_path, "import sys\nsys.exit(1)\n")
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "crasher", "point": "pre-bash", "cmd": str(cmd), "on_error": "closed"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert outcome == "block"
    assert "fail-closed" in reason
    assert any("exited 1" in msg for msg in seen)


def test_run_hook_on_error_open_allows_non_absolute_cmd():
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "relative", "point": "pre-bash", "cmd": "hook.py", "on_error": "open"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert (outcome, reason) == ("allow", "")
    assert any("descriptor cmd is not absolute" in msg for msg in seen)


def test_run_hook_bad_bool_timeout_on_closed_gate_blocks(tmp_path):
    cmd = _script(tmp_path, "import sys\nsys.exit(0)\n")
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {
            "id": "bad-timeout",
            "point": "pre-bash",
            "cmd": str(cmd),
            "timeout_ms": True,
            "on_error": "closed",
        },
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert outcome == "block"
    assert "non-numeric timeout_ms" in reason
    assert any("non-numeric timeout_ms" in msg for msg in seen)


def test_run_hook_negative_timeout_on_closed_gate_blocks(tmp_path):
    cmd = _script(tmp_path, "import sys\nsys.exit(0)\n")
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {
            "id": "negative-timeout",
            "point": "pre-bash",
            "cmd": str(cmd),
            "timeout_ms": -1,
            "on_error": "closed",
        },
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert outcome == "block"
    assert "negative timeout_ms" in reason
    assert any("negative timeout_ms" in msg for msg in seen)


def test_run_hook_zero_timeout_uses_default(tmp_path):
    cmd = _script(tmp_path, "import sys\nsys.exit(0)\n")
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {
            "id": "zero-timeout",
            "point": "pre-bash",
            "cmd": str(cmd),
            "timeout_ms": 0,
            "on_error": "closed",
        },
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert (outcome, reason) == ("allow", "")
    assert seen == []


def test_run_hook_non_utf8_exit10_still_blocks(tmp_path):
    cmd = _script(
        tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n"
        "sys.stdout.flush()\n"
        "sys.exit(10)\n",
    )
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "binary", "point": "pre-bash", "cmd": str(cmd), "on_error": "open"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert outcome == "block"
    assert reason == "Blocked by agent-hook 'binary'."
    assert seen == []


def test_run_hook_clean_allow_stderr_stays_silent(tmp_path):
    """Exit-0 allow with advisory stderr chatter (e.g. first-offense WARNs that
    also emit allow) must stay silent: no warning recorded."""
    cmd = _script(
        tmp_path,
        "import sys\n"
        "sys.stderr.write('By design advisory chatter\\n')\n"
        "sys.exit(0)\n",
    )
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "chatty-allow", "point": "pre-bash", "cmd": str(cmd), "on_error": "open"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert (outcome, reason) == ("allow", "")
    assert seen == []
def test_run_hook_block_stderr_stays_silent(tmp_path):
    """Exit-10 block with stderr chatter (hooks duplicating the reason on stderr):
    no warning recorded — the reason travels in stdout JSON, never as TUI trash."""
    cmd = _script(
        tmp_path,
        "import json, sys\n"
        "sys.stderr.write('block-secrets-write: chatter duplicated from stdout\\n')\n"
        "print(json.dumps({'message': 'blocked for test'}))\n"
        "sys.exit(10)\n",
    )
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "chatty-blocker", "point": "pre-bash", "cmd": str(cmd), "on_error": "closed"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert outcome == "block"
    assert reason == "blocked for test"
    assert seen == []


def test_run_hook_error_keeps_structured_warn_without_raw_chatter(tmp_path):
    """A crashing hook (nonzero, non-10) still warns structurally via the callback
    (`exited N (on_error=…)`), but its raw stderr text is never forwarded."""
    cmd = _script(
        tmp_path,
        "import sys\n"
        "sys.stderr.write('traceback-ish raw chatter\\n')\n"
        "sys.exit(1)\n",
    )
    seen, warn = _warnings()

    outcome, reason = run_hook(
        {"id": "crasher", "point": "pre-bash", "cmd": str(cmd), "on_error": "open"},
        {"hook_api": "agents-hooks/v1"},
        warn=warn,
    )

    assert (outcome, reason) == ("allow", "")
    assert any("exited 1" in msg for msg in seen)
    assert not any("traceback-ish" in msg for msg in seen)
