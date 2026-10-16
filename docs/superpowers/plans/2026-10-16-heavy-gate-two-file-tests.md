# heavy-op-memory-gate: 1-2 file test runs locally — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a test run that names at most two test files is gated only on the emergency memory signals, so agents stop renting Runpod pods for 10-second single-file runs (agent-tools#911).

**Architecture:** `heavy_op_memory_gate.py` already has an "emergency signals only" path for reviews handed to review-cli (`decide(..., review_admission=True)` drops the budget, swap and live-runner-ceiling checks). Add a pure classifier `small_test_run(command, cwd)` and route its hits through the same path. Rename the flag to `emergency_only` (it no longer means only reviews) and keep the review message suffix keyed on the review case.

**Tech Stack:** Python 3 stdlib hook, pytest.

**Spec:** binding brief R4 (session artifact `runpod-enforce-brief.md`): "agent-tools heavy-op-memory-gate must allow ≤2-file test runs locally (still blocked only under real memory pressure: MemAvailable < 8 GB or PSI full avg10 high — reuse its own pressure signals)". Evidence: 2026-10-15 13:44Z `bun test ./tests/session-topic-integration.test.ts -t …` → "Blocked: … 7 heavy runner(s) already live".

## Global Constraints

- Hook stays stdlib-only, fail-open on internal errors (existing posture).
- Emergency signals = the hook's existing ones: MemAvailable below `_min_available_mb` (max(8 GiB, 20% MemTotal) on Linux), PSI `some avg10` above `RIG_HEAVY_OP_MAX_PSI_SOME10`, wired share, jetsam level. No new thresholds.
- "Small test run": the command's ONLY heavy simple command is one of `pytest` (incl. `uv run [--with …] pytest`, `python -m pytest`, `poetry run pytest`), `bun test`, `vitest [run]`, `jest`, `go test`, with 1 or 2 file operands (`tests/a.py`, `tests/a.py::TestX`, `x.test.ts`, `foo_test.go`) and NO directory operand and NO bare suite (no operand at all = suite). `-t`/`-k`/`-run` filters are allowed alongside the files. Operand rule identical to runpod-offload-gate's README table: an existing path is judged by what it is; a non-existent path is a directory when its last component has no extension.
- Anything else heavy in the same command (`bun test a.ts && bun test`, `pytest a.py & review diff`) → not small; full checks.
- Docs English-only; README of the hook updated with the new row.

## Review Focus

- `pytest tests/` / `bun test` (no file) / `bun test ./tests` must keep full checks.
- `pytest a.py b.py c.py` (3 files) keeps full checks.
- `cd sub && bun test x.test.ts` with cwd-relative non-existent path counts as a file (has extension).
- A runpod-run-offloaded command is unaffected (already exempt).
- Emergency path still blocks a small run under MemAvailable floor / PSI.

---

### Task 1: small test run → emergency signals only

**Files:**
- Modify: `agent-hooks/heavy-op-memory-gate/heavy_op_memory_gate.py` (`decide` flag rename, new `small_test_run`, `main` routing)
- Modify: `agent-hooks/heavy-op-memory-gate/README.md` (one section: "One or two test files")
- Test: `tests/test_heavy_op_memory_gate.py`

**Interfaces:**
- Produces: `small_test_run(command: str, cwd: str) -> bool`; `decide(..., emergency_only: bool = False)` (replaces `review_admission`; every caller and test updated).

- [ ] **Step 1: failing tests** — `test_small_test_run_classifier` (parametrized: `bun test ./tests/x.test.ts -t foo` True; `uv run --with pytest pytest tests/a.py tests/b.py::T -q` True; `pytest tests/a.py tests/b.py tests/c.py` False; `pytest tests/` False; `bun test` False; `go test ./pkg/foo_test.go` True; `bun test a.test.ts && bun test` False); `test_small_test_run_allowed_with_many_live_runners` (7 live runners, roomy MemAvailable, swap 90% full → allow via `main` seam or `decide(emergency_only=True)`); `test_small_test_run_still_blocked_on_emergency` (MemAvailable under floor → block; PSI above limit → block).
- [ ] **Step 2:** run `nice -n19 ionice -c3 uv run --with pytest python -m pytest tests/test_heavy_op_memory_gate.py -q -k small_test_run` → FAIL.
- [ ] **Step 3:** implement; `main()` computes `cwd` from the event (`event.get("cwd")` / `args.get("cwd")`, else `os.getcwd()`), sets `emergency_only = True` for a small test run.
- [ ] **Step 4:** rerun the whole file (single file allowed locally) → PASS.
- [ ] **Step 5:** commit `fix(heavy-op-memory-gate): gate 1-2 file test runs on emergency signals only (Refs #911)`.
