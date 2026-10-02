"""Total wall-clock budget for one cc_hook_bridge point evaluation.

Claude Code kills the bridge command at its outer settings.json timeout (1200s) and a killed
command hook FAILS OPEN. `_evaluate_point` runs every descriptor sequentially (so each hatch
hook can write its overrides.log audit line, #713), and hatch hooks carry
`timeout_ms: 960000`. Two hatch waits in one call therefore used to exceed the outer timeout.
The bridge now enforces its own budget below that wall; these tests drive it with a fake
monotonic clock (no real multi-minute waits) plus one real short-timeout subprocess.

    uv run --with pytest python -m pytest tests/test_cc_hook_bridge_budget.py -q
"""

from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cc_hook_bridge.dispatch as dispatch  # noqa: E402

BASH_EVENT = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"}}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _spec(hook_id: str, *, cost_s: float, outcome: str = "allow", reason: str = "",
          timeout_ms: int | None = 960000, on_error: str = "open") -> dict:
    spec = {"id": hook_id, "cmd": f"/abs/{hook_id}", "on_error": on_error,
            "_cost_s": cost_s, "_outcome": outcome, "_reason": reason}
    if timeout_ms is not None:
        spec["timeout_ms"] = timeout_ms
    return spec


def _install(monkeypatch, specs: list[dict]):
    """Stub the runner: each fake hook 'takes' _cost_s of fake time, and a hook whose cost
    exceeds the timeout it was handed resolves through its (possibly clamped) on_error."""
    clock = FakeClock()
    calls: list[dict] = []
    warnings: list[str] = []

    def fake_load(point, hooks_dir, *, warn):
        return specs

    def fake_run(spec, v1_event, *, warn):
        calls.append(spec)
        timeout_s = spec.get("timeout_ms", 5000) / 1000.0
        cost = spec["_cost_s"]
        if cost > timeout_s:
            clock.now += timeout_s
            if str(spec.get("on_error", "open")) == "closed":
                return "block", f"{spec['id']} timed out, fail-closed"
            return "allow", ""
        clock.now += cost
        return spec["_outcome"], spec["_reason"]

    monkeypatch.setattr(dispatch, "_load_runner", lambda: (fake_load, fake_run))
    monkeypatch.setattr(dispatch, "_monotonic", clock)
    monkeypatch.setattr(dispatch, "_warn", warnings.append)
    return calls, warnings, clock


def test_budget_is_below_the_outer_cc_timeout():
    assert dispatch.HOOK_BUDGET_S == 1100
    assert dispatch.HOOK_BUDGET_S < 1200


def test_default_timeout_mirror_matches_the_runner():
    from agent_hooks_v1 import runner

    assert dispatch._DEFAULT_TIMEOUT_MS == runner.DEFAULT_TIMEOUT_MS


_ABSENT = object()


@pytest.mark.parametrize("raw", [_ABSENT, None, 0, 1, 250, 960000, "1500", -5, True, False, "abc", 2.9])
def test_effective_timeout_agrees_with_the_real_runner(monkeypatch, raw):
    """The bridge's clamp is only safe if it reads timeout_ms exactly as run_hook does."""
    import subprocess

    from agent_hooks_v1 import runner

    seen: dict = {}

    def fake_subprocess_run(argv, **kwargs):
        seen["timeout_ms"] = round(kwargs["timeout"] * 1000)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_subprocess_run)
    spec = {"id": "x", "cmd": "/abs/x", "on_error": "open"}
    if raw is not _ABSENT:
        spec["timeout_ms"] = raw
    runner.run_hook(spec, {}, warn=lambda msg: None)
    assert dispatch._effective_timeout_ms(spec) == seen.get("timeout_ms")


def test_two_hatch_waits_second_is_clamped_and_fails_closed(monkeypatch):
    specs = [_spec("hatch-a", cost_s=960), _spec("hatch-b", cost_s=960)]
    calls, _warnings, clock = _install(monkeypatch, specs)
    result = dispatch._evaluate_point("PreToolUse", BASH_EVENT)
    assert len(calls) == 2
    assert calls[0]["timeout_ms"] == 960000 and calls[0]["on_error"] == "open"
    assert calls[1]["timeout_ms"] == 140000
    assert calls[1]["on_error"] == "closed"
    assert result is not None and result[0] == "block"
    assert clock.now - 1000.0 <= dispatch.HOOK_BUDGET_S


@pytest.mark.parametrize("grant_first", [False, True])
def test_grant_does_not_survive_an_exhausted_budget(monkeypatch, grant_first):
    slow = _spec("slow-audit", cost_s=1099 if grant_first else 1100, timeout_ms=2000000)
    granter = _spec("granter", cost_s=1, outcome="grant", reason="pre-approved")
    tail = _spec("tail", cost_s=1)
    specs = [granter, slow, tail] if grant_first else [slow, granter]
    calls, warnings, _clock = _install(monkeypatch, specs)
    result = dispatch._evaluate_point("PreToolUse", BASH_EVENT)
    assert result is not None and result[0] == "block"
    assert "budget" in result[1]
    invoked = [c["id"] for c in calls]
    assert invoked == (["granter", "slow-audit"] if grant_first else ["slow-audit"])
    assert any("budget" in w and ("tail" in w if grant_first else "granter" in w) for w in warnings)


def test_first_block_then_exhaustion_keeps_block_and_skips_rest(monkeypatch):
    specs = [_spec("blocker", cost_s=1100, outcome="block", reason="real reason", timeout_ms=2000000),
             _spec("later-1", cost_s=1), _spec("later-2", cost_s=1)]
    calls, warnings, _clock = _install(monkeypatch, specs)
    result = dispatch._evaluate_point("PreToolUse", BASH_EVENT)
    assert result == ("block", "real reason")
    assert [c["id"] for c in calls] == ["blocker"]
    joined = "\n".join(warnings)
    assert "later-1" in joined and "later-2" in joined and "budget" in joined


def test_ample_budget_behaviour_unchanged_and_specs_not_mutated(monkeypatch):
    specs = [_spec("a", cost_s=1, outcome="grant", reason="g"),
             _spec("b", cost_s=1, outcome="block", reason="b-reason"),
             _spec("c", cost_s=1, timeout_ms=None),
             _spec("d", cost_s=1, timeout_ms=0)]
    snapshot = copy.deepcopy(specs)
    calls, warnings, _clock = _install(monkeypatch, specs)
    result = dispatch._evaluate_point("PreToolUse", BASH_EVENT)
    assert result == ("block", "b-reason")
    assert [c["id"] for c in calls] == ["a", "b", "c", "d"]
    assert specs == snapshot
    assert [c.get("on_error") for c in calls] == ["open"] * 4
    assert calls[0]["timeout_ms"] == 960000
    assert not any("budget" in w for w in warnings)


def test_invalid_timeout_specs_are_passed_through_untouched(monkeypatch):
    specs = [_spec("neg", cost_s=0, timeout_ms=-5), _spec("boolish", cost_s=0, timeout_ms=None)]
    specs[1]["timeout_ms"] = True
    calls, _warnings, _clock = _install(monkeypatch, specs)
    dispatch._evaluate_point("PreToolUse", BASH_EVENT)
    assert calls[0]["timeout_ms"] == -5
    assert calls[1]["timeout_ms"] is True


def test_real_subprocess_is_cut_at_the_budget_and_blocks(monkeypatch, tmp_path):
    from agent_hooks_v1 import run_hook

    script = tmp_path / "slow_hatch.sh"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    spec = {"id": "slow-hatch", "cmd": str(script), "timeout_ms": 600000, "on_error": "open"}
    monkeypatch.setattr(dispatch, "HOOK_BUDGET_S", 1.5)
    monkeypatch.setattr(dispatch, "_load_runner", lambda: (lambda *a, **k: [spec], run_hook))
    monkeypatch.setattr(dispatch, "_warn", lambda msg: None)
    started = time.monotonic()
    result = dispatch._evaluate_point("PreToolUse", BASH_EVENT)
    elapsed = time.monotonic() - started
    assert result is not None and result[0] == "block"
    assert elapsed < 10
    assert spec["timeout_ms"] == 600000 and spec["on_error"] == "open"
