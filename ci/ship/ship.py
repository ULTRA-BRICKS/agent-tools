#!/usr/bin/env python3
"""gh ship — green-CI-gated PR merge with pre-merge safety checks, then branch/worktree cleanup.

Entry point behind ci/ship/ship.sh (a thin launcher: the python-only rule bounds shell at 40
lines, agent-tools#946). The behaviour, flags, messages and exit codes are those of the former
4.6k-line bash script; the usage/knob documentation lives in ci/ship/README.md and AGENTS.md.

Every module in `shiplib/` owns one contiguous segment of the old script and exposes
`run(ctx)`, which executes that segment's top-level statements in order. The segments are run in
file order, so the gate sequence is exactly the bash script's.
"""
from __future__ import annotations

import importlib
import os
import sys

sys.dont_write_bytecode = True  # never litter the checkout ship runs from (it would turn the PR tree dirty)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from shiplib.ctx import Ctx, ShipExit  # noqa: E402

# (module, owns the old ship.sh lines) — merge happens inside accept_merge, cleanup is last.
STEPS = (
    "shiplib.setup",           # args, core.bare guard, provider dispatch, --repo, main checkout, PR state
    "shiplib.ci_fallback",     # CI-down detection, .ship-config, local test gate, known-flake gate
    "shiplib.ci_gate",         # bot-thread resolve, green-CI gate, review threads, dwell, local branch sanity
    "shiplib.version_shot",    # version-bump gate, screenshot gate
    "shiplib.quorum_skipci",   # review-quorum gate, --skip-ci hatch gate
    "shiplib.extrev_ticket",   # external-review gate, ticket gates preamble, magic-close gate
    "shiplib.accept_merge",    # acceptance gate, evidence decision, Forgejo final preflight, merge, task-cli notify
    "shiplib.cleanup",         # post-merge cleanup
)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="surrogateescape", line_buffering=True)
    ctx = Ctx(os.path.dirname(os.path.realpath(__file__)), argv)
    # Import every step up front: a broken module must fail before the first gate, never after the
    # merge has happened (cleanup is the last step).
    steps = [importlib.import_module(name) for name in STEPS]
    code = 1  # what the exit handlers see if a step dies on anything but ShipExit
    try:
        for step in steps:
            step.run(ctx)
        code = 0
    except ShipExit as exc:
        code = exc.code
    except BrokenPipeError:
        # The reader went away: point stdout at /dev/null so interpreter shutdown cannot fail on it.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
        code = 141
    finally:
        ctx.run_exit_handlers(code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
