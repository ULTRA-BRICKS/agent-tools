#!/usr/bin/env python3
import importlib
import sys

def main() -> int:
    m = importlib.import_module("pytest")
    return m.main([
        "-q",
        "tests/test_agenttools_memory_pressure.py",
        "tests/test_agent_fan_out_memory_gate.py",
        "tests/test_heavy_op_memory_gate.py",
    ])

if __name__ == "__main__":
    raise SystemExit(main())
