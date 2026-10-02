"""Tests for the fleet-memory-gate agent-hook (pre-agent, hard block).

Prevents DISPATCHING a new subagent (Agent/Task/opencode or omp task tool) while the
machine's real memory pressure (macOS jetsam level via
`sysctl kern.memorystatus_vm_pressure_level`) is at WARN-or-worse — closes the
still-open "pre-agent gate for Agent/Task dispatch (fan-out itself)" scope note from
agent-tools#579, after the 2026-08-27/28 and 2026-09-08 fleet-driven memory-pressure
incidents. Deliberately stateless (no counter/lock/queue): every call re-reads
pressure fresh. See the hook module's own docstring for the full rationale, including
why this is NOT a concurrency semaphore, carries no hatch-escalation, and — unlike
`background-subagent-gate` — applies to BOTH the orchestrator and a subagent's own
further fan-out (no `agent_id` exemption).

This suite mirrors `tests/test_heavy_op_memory_gate.py`'s structure and rigor, minus
the `classify_heavy_operation`-related tests: there is no classification step here,
since every `pre-agent` event IS the operation this hook governs.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_fleet_memory_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "fleet-memory-gate"
    / "fleet_memory_gate.py"
)
_spec = importlib.util.spec_from_file_location("fleet_memory_gate", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# ── read_pressure_level — platform DISPATCH (not fallback-chain) ─────────────────

def test_read_pressure_level_dispatches_to_macos_reader_on_darwin():
    calls = []
    hook.read_pressure_level(
        macos_reader=lambda: (calls.append("macos"), 4)[1],
        linux_reader=lambda: (calls.append("linux"), 1)[1],
        platform_name="Darwin",
    )
    assert calls == ["macos"]


def test_read_pressure_level_dispatches_to_linux_reader_on_linux():
    calls = []
    hook.read_pressure_level(
        macos_reader=lambda: (calls.append("macos"), 4)[1],
        linux_reader=lambda: (calls.append("linux"), 2)[1],
        platform_name="Linux",
    )
    assert calls == ["linux"]


def test_read_pressure_level_does_not_cross_fall_back():
    """A None from the platform's own reader is NOT chased with the other
    platform's reader — dispatch is exclusive, by design (see docstring)."""
    result = hook.read_pressure_level(macos_reader=lambda: None, linux_reader=lambda: 2, platform_name="Darwin")
    assert result is None


def test_read_pressure_level_none_when_reader_unavailable():
    assert hook.read_pressure_level(macos_reader=lambda: None, linux_reader=lambda: None, platform_name="Darwin") is None


def test_linux_pressure_reader_maps_avg10_to_levels():
    def make_reader(text):
        return lambda path: text

    critical_text = "some avg10=75.00 avg60=50.00 avg300=20.00 total=123\nfull avg10=10.00\n"
    warn_text = "some avg10=25.00 avg60=10.00 avg300=5.00 total=123\nfull avg10=1.00\n"
    normal_text = "some avg10=2.00 avg60=1.00 avg300=0.50 total=123\nfull avg10=0.00\n"

    assert hook._read_linux_pressure_level(read_text=make_reader(critical_text)) == 4
    assert hook._read_linux_pressure_level(read_text=make_reader(warn_text)) == 2
    assert hook._read_linux_pressure_level(read_text=make_reader(normal_text)) == 1


def test_linux_pressure_reader_none_on_missing_file():
    def raiser(path):
        raise OSError("no such file")

    assert hook._read_linux_pressure_level(read_text=raiser) is None


def test_linux_pressure_reader_none_on_unparseable_content():
    assert hook._read_linux_pressure_level(read_text=lambda p: "garbage, no psi fields here") is None


# ── macOS reader — the one function that actually runs in production ─────────────

class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_macos_reader_parses_a_valid_level():
    assert hook._read_macos_pressure_level(run=lambda *a, **k: _FakeCompletedProcess("2\n")) == 2


def test_macos_reader_none_on_nonzero_returncode():
    assert hook._read_macos_pressure_level(run=lambda *a, **k: _FakeCompletedProcess("", returncode=1)) is None


def test_macos_reader_none_on_unparseable_stdout():
    assert hook._read_macos_pressure_level(run=lambda *a, **k: _FakeCompletedProcess("not-a-number\n")) is None


def test_macos_reader_none_when_sysctl_missing():
    def raiser(*a, **k):
        raise FileNotFoundError("sysctl not found")

    assert hook._read_macos_pressure_level(run=raiser) is None


# ── _block_at_level — env override, restricted to the real jetsam levels ─────────

def test_block_at_level_default_when_unset(monkeypatch):
    monkeypatch.delenv("RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL", raising=False)
    assert hook._block_at_level() == hook.DEFAULT_BLOCK_AT_LEVEL


@pytest.mark.parametrize("value", [1, 2, 4])
def test_block_at_level_accepts_real_jetsam_levels(monkeypatch, value):
    monkeypatch.setenv("RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL", str(value))
    assert hook._block_at_level() == value


@pytest.mark.parametrize("value", ["3", "5", "999", "0", "-1"])
def test_block_at_level_rejects_out_of_range_values(monkeypatch, value):
    """A value outside {1,2,4} would make `level < block_at` true for every
    real jetsam reading — a silent, total bypass. Must fall back to default."""
    monkeypatch.setenv("RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL", value)
    assert hook._block_at_level() == hook.DEFAULT_BLOCK_AT_LEVEL


def test_block_at_level_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL", "not-a-number")
    assert hook._block_at_level() == hook.DEFAULT_BLOCK_AT_LEVEL


# ── decide — the pure decision core (level + block_at, no label) ─────────────────

def test_allows_when_level_is_none_regardless_of_block_at():
    for block_at in (1, 2, 4):
        decision, message = hook.decide(None, block_at=block_at)
        assert decision == "allow"
        assert message is None


def test_allows_when_pressure_is_below_threshold():
    decision, _message = hook.decide(1, block_at=2)
    assert decision == "allow"


def test_blocks_at_exactly_the_warn_threshold():
    decision, message = hook.decide(2, block_at=2)
    assert decision == "block"
    assert "no bypass" in message.lower()
    assert "subagent" in message.lower() or "dispatch" in message.lower()


def test_blocks_at_critical():
    decision, message = hook.decide(4, block_at=2)
    assert decision == "block"
    assert "critical" in message.lower()


def test_does_not_block_below_a_raised_threshold():
    """A custom, stricter block_at=4 should allow a merely-warn (level=2) machine."""
    decision, _message = hook.decide(2, block_at=4)
    assert decision == "allow"


def test_block_message_does_not_hardcode_a_specific_person():
    """This ships to every repo/machine in the catalog — must stay generic."""
    _decision, message = hook.decide(4, block_at=2)
    assert "alex" not in message.lower()


def test_block_message_names_dispatch_not_bash_command():
    """The block message must be legible for what it actually gates — a NEW
    subagent dispatch — not read like a copy-pasted bash-command message."""
    _decision, message = hook.decide(4, block_at=2)
    lowered = message.lower()
    assert "subagent" in lowered
    assert "bash" not in lowered


# ── main() end-to-end wiring (stdin/stdout protocol, env override) ────────────────

def _run(args, monkeypatch, *, macos_level, env: dict | None = None) -> tuple[str, str, int]:
    monkeypatch.setattr(hook, "_read_macos_pressure_level", lambda run=None: macos_level)
    monkeypatch.setattr(hook, "_read_linux_pressure_level", lambda read_text=None: None)
    monkeypatch.setattr(hook.platform, "system", lambda: "Darwin")
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = hook.main()
    return out.getvalue(), err.getvalue(), code


def test_main_blocks_dispatch_under_warn_pressure(monkeypatch):
    out, _err, code = _run({}, monkeypatch, macos_level=2)
    assert code == hook.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert payload["hook_api"] == hook.HOOK_API


def test_main_allows_dispatch_under_normal_pressure(monkeypatch):
    out, _err, code = _run({}, monkeypatch, macos_level=1)
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_respects_env_override_for_block_threshold(monkeypatch):
    # block_at=4 (critical only) — a merely-warn (2) machine should now ALLOW.
    out, _err, code = _run({}, monkeypatch, macos_level=2, env={"RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL": "4"})
    assert code == 0
    assert json.loads(out)["decision"] == "allow"


def test_main_invalid_env_override_falls_back_to_default(monkeypatch):
    out, _err, code = _run({}, monkeypatch, macos_level=2, env={"RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL": "not-a-number"})
    assert code == hook.BLOCK_EXIT_CODE  # default threshold (2) still applies
    assert json.loads(out)["decision"] == "block"


def test_main_out_of_range_env_override_cannot_bypass_a_critical_machine(monkeypatch):
    """The bypass this closes: RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL=999 used to make
    even a critical (4) machine ALLOW. Must still BLOCK at the safe default."""
    out, _err, code = _run({}, monkeypatch, macos_level=4, env={"RIG_FLEET_DISPATCH_BLOCK_AT_LEVEL": "999"})
    assert code == hook.BLOCK_EXIT_CODE
    assert json.loads(out)["decision"] == "block"


def test_main_fails_open_on_unparseable_stdin(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_main_applies_to_orchestrator_and_subagent_alike(monkeypatch):
    """Deliberate behavioral DIFFERENCE from `background-subagent-gate`: this hook
    has no `agent_id` exemption, because the risk ("does starting one more agent
    process tip an already-strained machine") does not depend on which layer of the
    fan-out tree issues the dispatch. Both a bare orchestrator dispatch (no
    agent_id) and a subagent's own further fan-out (agent_id present) must block
    identically under WARN pressure."""
    monkeypatch.setattr(hook, "_read_macos_pressure_level", lambda run=None: 2)
    monkeypatch.setattr(hook, "_read_linux_pressure_level", lambda read_text=None: None)
    monkeypatch.setattr(hook.platform, "system", lambda: "Darwin")
    for agent_id in (None, "sub-1"):
        args = {}
        if agent_id is not None:
            args["agent_id"] = agent_id
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        code = hook.main()
        assert code == hook.BLOCK_EXIT_CODE, f"agent_id={agent_id!r}"
        assert json.loads(out.getvalue())["decision"] == "block"


def test_main_non_string_args_does_not_crash(monkeypatch):
    """A harness that ever sends `args` as a list/int instead of a dict must
    still fail open, not raise — mirrors the unparseable-stdin guarantee."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": ["not", "a", "dict"]})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_main_non_dict_event_fails_open(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(["not", "a", "dict"])))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = hook.main()
    assert code == 0
    assert json.loads(out.getvalue())["decision"] == "allow"
