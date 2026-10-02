"""Machine memory-pressure signals shared by agent-hooks (agent-tools#519)."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

HOOK_API = "agents-hooks/v1"
BLOCK_EXIT_CODE = 10

DEFAULT_BLOCK_AT_LEVEL = 2
_DEFAULT_MIN_FREE_MB = 512
_DEFAULT_MAX_SWAP_USED_PCT = 85.0
_VALID_PRESSURE_LEVELS = frozenset({1, 2, 4})

_DARWIN_PAGE_SIZE: int | None = None


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    pressure_level: int | None
    free_bytes: int | None
    swap_used_bytes: int | None
    swap_total_bytes: int | None

    @property
    def swap_used_pct(self) -> float | None:
        if self.swap_used_bytes is None or self.swap_total_bytes is None or self.swap_total_bytes <= 0:
            return None
        return 100.0 * self.swap_used_bytes / self.swap_total_bytes


@dataclass(frozen=True, slots=True)
class MemoryGateConfig:
    block_at_level: int = DEFAULT_BLOCK_AT_LEVEL
    min_free_bytes: int = _DEFAULT_MIN_FREE_MB * 1024 * 1024
    max_swap_used_pct: float = _DEFAULT_MAX_SWAP_USED_PCT


@dataclass(frozen=True, slots=True)
class MemoryAssessment:
    decision: Literal["allow", "block"]
    message: str | None
    reasons: tuple[str, ...] = ()


def _run_fn() -> Callable[..., subprocess.CompletedProcess[str]]:
    return subprocess.run


def _read_macos_pressure_level(run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> int | None:
    run = run or _run_fn()
    try:
        proc = run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _read_linux_pressure_level(read_text: Callable[[str], str] | None = None) -> int | None:
    read_text = read_text or (lambda path: Path(path).read_text(encoding="utf-8"))
    try:
        text = read_text("/proc/pressure/memory")
    except OSError:
        return None
    match = re.search(r"^some\s+.*\bavg10=([\d.]+)", text, re.MULTILINE)
    if not match:
        return None
    try:
        avg10 = float(match.group(1))
    except ValueError:
        return None
    if avg10 >= 60:
        return 4
    if avg10 >= 10:
        return 2
    return 1


def _darwin_page_size(run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> int:
    global _DARWIN_PAGE_SIZE
    if _DARWIN_PAGE_SIZE is not None:
        return _DARWIN_PAGE_SIZE
    run = run or _run_fn()
    try:
        proc = run(["sysctl", "-n", "hw.pagesize"], capture_output=True, text=True, timeout=3)
        if proc.returncode == 0:
            _DARWIN_PAGE_SIZE = int(proc.stdout.strip())
            return _DARWIN_PAGE_SIZE
    except Exception:
        pass
    _DARWIN_PAGE_SIZE = 16384
    return _DARWIN_PAGE_SIZE


def _read_macos_free_bytes(run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> int | None:
    run = run or _run_fn()
    try:
        proc = run(["vm_stat"], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"Pages free:\s+(\d+)", proc.stdout)
    if not match:
        return None
    try:
        pages = int(match.group(1))
    except ValueError:
        return None
    return pages * _darwin_page_size(run)


def _parse_mib(value: str) -> int | None:
    text = value.strip().rstrip("M").strip()
    try:
        return int(float(text) * 1024 * 1024)
    except ValueError:
        return None


def _read_macos_swap_bytes(run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> tuple[int | None, int | None]:
    run = run or _run_fn()
    try:
        proc = run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3)
    except Exception:
        return None, None
    if proc.returncode != 0:
        return None, None
    match = re.search(r"total\s*=\s*([\d.]+)M\s+used\s*=\s*([\d.]+)M\s+free\s*=\s*([\d.]+)M", proc.stdout, re.IGNORECASE)
    if not match:
        return None, None
    total = _parse_mib(match.group(1))
    used = _parse_mib(match.group(2))
    if total is None or used is None:
        return None, None
    return used, total


def read_snapshot(*, macos_reader=None, linux_reader=None, platform_name=None, run=None) -> MemorySnapshot:
    macos_reader = macos_reader or (lambda: _read_macos_pressure_level(run))
    linux_reader = linux_reader or _read_linux_pressure_level
    platform_name = platform_name if platform_name is not None else platform.system()
    free_bytes = None
    swap_used = None
    swap_total = None
    if platform_name == "Darwin":
        pressure = macos_reader()
        free_bytes = _read_macos_free_bytes(run)
        swap_used, swap_total = _read_macos_swap_bytes(run)
    else:
        pressure = linux_reader()
    return MemorySnapshot(pressure_level=pressure, free_bytes=free_bytes, swap_used_bytes=swap_used, swap_total_bytes=swap_total)


def _find_rig_yaml(cwd: str) -> Path | None:
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / "rig.yaml").is_file():
            return d
    return None


def _parse_nested_int(block: str, key: str, default: int) -> int:
    in_block = False
    child_indent = None
    for raw in block.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        head = line.strip().split(":", 1)[0].strip()
        if indent == 0:
            in_block = head == "memory_gate"
            child_indent = None
            continue
        if not in_block:
            continue
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        if head == key and ":" in line.strip():
            val = line.strip().split(":", 1)[1].strip().strip("\"'")
            if not val:
                return default
            try:
                return int(float(val))
            except ValueError:
                return default
    return default


def _parse_nested_float(block: str, key: str, default: float) -> float:
    in_block = False
    child_indent = None
    for raw in block.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        head = line.strip().split(":", 1)[0].strip()
        if indent == 0:
            in_block = head == "memory_gate"
            child_indent = None
            continue
        if not in_block:
            continue
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        if head == key and ":" in line.strip():
            val = line.strip().split(":", 1)[1].strip().strip("\"'")
            if not val:
                return default
            try:
                return float(val)
            except ValueError:
                return default
    return default


def _memory_gate_block_text(rig_yaml_text: str) -> str:
    in_agent_hooks = False
    child_indent = None
    lines = []
    for raw in rig_yaml_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        head = line.strip().split(":", 1)[0].strip()
        if indent == 0:
            in_agent_hooks = head == "agent_hooks"
            child_indent = None
            if in_agent_hooks:
                lines = []
            continue
        if not in_agent_hooks:
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent and head == "memory_gate":
            lines = [line.strip()]
            continue
        if lines and indent > child_indent:
            lines.append(line.strip())
            continue
        if lines and indent <= child_indent:
            break
    return "\n".join(lines)


def _coerce_block_at(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value not in _VALID_PRESSURE_LEVELS:
        return default
    return value


def load_config(cwd: str | None = None) -> MemoryGateConfig:
    block_at = _coerce_block_at(os.environ.get("RIG_MEMORY_BLOCK_AT_LEVEL"), DEFAULT_BLOCK_AT_LEVEL)
    min_free_mb = _DEFAULT_MIN_FREE_MB
    max_swap = _DEFAULT_MAX_SWAP_USED_PCT
    env_min = os.environ.get("RIG_MEMORY_MIN_FREE_MB")
    if env_min:
        try:
            min_free_mb = max(0, int(env_min))
        except ValueError:
            pass
    env_swap = os.environ.get("RIG_MEMORY_MAX_SWAP_USED_PCT")
    if env_swap:
        try:
            max_swap = float(env_swap)
        except ValueError:
            pass
    root = _find_rig_yaml(cwd or os.getcwd())
    if root is not None:
        try:
            text = (root / "rig.yaml").read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            block = _memory_gate_block_text(text)
            if block:
                if not os.environ.get("RIG_MEMORY_BLOCK_AT_LEVEL"):
                    block_at = _parse_nested_int(block, "block_at_pressure_level", block_at)
                if not env_min:
                    min_free_mb = _parse_nested_int(block, "min_free_mb", min_free_mb)
                if not env_swap:
                    max_swap = _parse_nested_float(block, "max_swap_used_pct", max_swap)
    return MemoryGateConfig(block_at_level=block_at, min_free_bytes=min_free_mb * 1024 * 1024, max_swap_used_pct=max_swap)


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.1f} GiB"
    return f"{n / (1024 ** 2):.0f} MiB"


def assess(snapshot: MemorySnapshot, config: MemoryGateConfig, *, action_label: str) -> MemoryAssessment:
    if snapshot.pressure_level is None and snapshot.free_bytes is None and snapshot.swap_used_pct is None:
        return MemoryAssessment("allow", None)
    reasons = []
    level = snapshot.pressure_level
    if level is not None and level >= config.block_at_level:
        level_name = {1: "normal", 2: "warn", 4: "critical"}.get(level, str(level))
        reasons.append(f"memory pressure is '{level_name}' (jetsam level {level}, threshold {config.block_at_level})")
    if snapshot.free_bytes is not None and snapshot.free_bytes < config.min_free_bytes:
        reasons.append(f"free memory is {_fmt_bytes(snapshot.free_bytes)} (minimum {_fmt_bytes(config.min_free_bytes)})")
    swap_pct = snapshot.swap_used_pct
    if swap_pct is not None and swap_pct >= config.max_swap_used_pct:
        reasons.append(f"swap use is {swap_pct:.0f}% ({_fmt_bytes(snapshot.swap_used_bytes)} of {_fmt_bytes(snapshot.swap_total_bytes)}, maximum {config.max_swap_used_pct:.0f}%)")
    if not reasons:
        return MemoryAssessment("allow", None)
    swap_part = f"swap={swap_pct:.0f}% used" if swap_pct is not None else "swap=n/a"
    numbers = f"Current: jetsam={snapshot.pressure_level if snapshot.pressure_level is not None else 'n/a'}, free={_fmt_bytes(snapshot.free_bytes)}, {swap_part}"
    message = (
        f"Blocked: refusing {action_label} while the machine is under memory pressure — "
        + "; ".join(reasons)
        + f". {numbers}. Wait for concurrent work to finish or be reaped, then retry; readings are fresh on every attempt. There is no bypass for this check."
    )
    return MemoryAssessment("block", message, tuple(reasons))


def emit(decision: str, message: str | None = None) -> None:
    import json
    import sys
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
