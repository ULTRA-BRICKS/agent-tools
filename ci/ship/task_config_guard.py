#!/usr/bin/env python3
"""Routing-only check for ship.sh `--task-config <path>`.

What it is: `--task-config` hands task-cli an explicit `--config` file for the ship's ticket.
A task-cli config can also carry enforcement/policy settings (e.g. turning the acceptance gate
off), so a caller-supplied file would otherwise be a self-service way to make `task gate` answer
"ok". This guard accepts ONLY a routing file: `backend` plus that backend's routing block.

How it is reached: ship.sh validates `--task-config` right after argument parsing, before any
gate runs: `python3 -I task_config_guard.py <path>`; a non-zero exit refuses the ship.

Accepted shape (a strict YAML subset, parsed without a YAML library):

    backend: tasks            # tasks | forgejo
    tasks:                    # the block named by `backend`
      url: https://...
      team: UB
    forgejo:
      repo: owner/name
      url: https://...

Everything else refuses: unknown or duplicate keys, tabs, flow/anchor/tag/multi-line syntax,
inline comments, more than one document, or a `backend` without its block.

Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_BYTES = 64 * 1024
ROUTING_KEYS = {"tasks": {"url", "team"}, "forgejo": {"repo", "url"}}
LINE_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?: (?P<value>.*))?$")
UNSAFE_VALUE_START = tuple("{[&*!|>%@`")


class ConfigRefused(Exception):
    pass


def _scalar(raw: str, where: str) -> str:
    value = raw.strip()
    if not value:
        raise ConfigRefused(f"{where}: empty value")
    if value.startswith(UNSAFE_VALUE_START) or " #" in value:
        raise ConfigRefused(f"{where}: only plain scalar values are allowed")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _meaningful_lines(text: str):
    for number, line in enumerate(text.splitlines(), 1):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if "\t" in line or line in ("---", "...") or "<<" in line:
            raise ConfigRefused(f"line {number}: unsupported YAML syntax")
        yield number, line


def parse_routing(text: str) -> dict:
    config: dict = {}
    block: str | None = None
    for number, line in _meaningful_lines(text):
        m = LINE_RE.match(line)
        if not m or len(m.group("indent")) not in (0, 2):
            raise ConfigRefused(f"line {number}: not a routing key")
        key, value, nested = m.group("key"), m.group("value"), len(m.group("indent")) == 2
        if not nested:
            block = _top_level(config, key, value, number)
            continue
        if block is None or key not in ROUTING_KEYS[block]:
            raise ConfigRefused(f"line {number}: '{key}' is not a routing key of '{block}'")
        if key in config[block]:
            raise ConfigRefused(f"line {number}: duplicate key '{block}.{key}'")
        config[block][key] = _scalar(value or "", f"line {number}")
    _require_backend_block(config)
    return config


def _top_level(config: dict, key: str, value: str | None, number: int) -> str | None:
    if key in config:
        raise ConfigRefused(f"line {number}: duplicate key '{key}'")
    if key == "backend":
        config["backend"] = _scalar(value or "", f"line {number}")
        return None
    if key not in ROUTING_KEYS:
        raise ConfigRefused(f"line {number}: '{key}' is not a routing key (only backend, "
                            f"{', '.join(sorted(ROUTING_KEYS))}) — policy/enforcement settings are refused")
    if value and value.strip():
        raise ConfigRefused(f"line {number}: '{key}' must be a block of routing keys")
    config[key] = {}
    return key


def _require_backend_block(config: dict) -> None:
    backend = config.get("backend")
    if backend not in ROUTING_KEYS:
        raise ConfigRefused(f"backend must be one of {', '.join(sorted(ROUTING_KEYS))}")
    if not config.get(backend):
        raise ConfigRefused(f"backend '{backend}' has no '{backend}:' routing block")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: task_config_guard.py <task-config.yaml>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            raise ConfigRefused("file is too large for a routing config")
        parse_routing(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"--task-config {path}: unreadable ({exc.__class__.__name__})", file=sys.stderr)
        return 1
    except ConfigRefused as exc:
        print(f"--task-config {path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
