"""Tests for lib/agenttools_memory_pressure (agent-tools#519)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib" / "agenttools_memory_pressure" / "__init__.py"
_spec = importlib.util.spec_from_file_location("agenttools_memory_pressure", _LIB)
assert _spec and _spec.loader
mp = importlib.util.module_from_spec(_spec)
sys.modules["agenttools_memory_pressure"] = mp
_spec.loader.exec_module(mp)


def test_assess_blocks_on_high_swap_even_when_jetsam_normal():
    snap = mp.MemorySnapshot(pressure_level=1, free_bytes=2 * 1024 ** 3, swap_used_bytes=900, swap_total_bytes=1000)
    cfg = mp.MemoryGateConfig(max_swap_used_pct=85.0)
    result = mp.assess(snap, cfg, action_label="task dispatch")
    assert result.decision == "block"
    assert "swap" in result.message.lower()


def test_assess_blocks_on_low_free_ram():
    snap = mp.MemorySnapshot(pressure_level=1, free_bytes=100 * 1024 ** 2, swap_used_bytes=100, swap_total_bytes=1000)
    cfg = mp.MemoryGateConfig(min_free_bytes=512 * 1024 ** 2)
    result = mp.assess(snap, cfg, action_label="task dispatch")
    assert result.decision == "block"
    assert "free memory" in result.message.lower()


def test_assess_allows_when_signals_missing():
    snap = mp.MemorySnapshot(None, None, None, None)
    result = mp.assess(snap, mp.MemoryGateConfig(), action_label="task dispatch")
    assert result.decision == "allow"


def test_assess_allows_healthy_machine():
    snap = mp.MemorySnapshot(pressure_level=1, free_bytes=8 * 1024 ** 3, swap_used_bytes=100, swap_total_bytes=10000)
    result = mp.assess(snap, mp.MemoryGateConfig(), action_label="task dispatch")
    assert result.decision == "allow"


def test_load_config_reads_rig_yaml_memory_gate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        """
agent_hooks:
  memory_gate:
    min_free_mb: 256
    max_swap_used_pct: 70
    block_at_pressure_level: 4
""".lstrip(),
        encoding="utf-8",
    )
    cfg = mp.load_config(str(repo))
    assert cfg.min_free_bytes == 256 * 1024 * 1024
    assert cfg.max_swap_used_pct == 70.0
    assert cfg.block_at_level == 4


def test_parse_vm_stat_malformed_returns_none():
    def fake_run(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = "Mach Virtual Memory Statistics:\n(no pages free line)"
        return P()

    assert mp._read_macos_free_bytes(fake_run) is None
