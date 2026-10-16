# runpod-run spend caps — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** make inefficient Runpod spend impossible inside `runpod-run` (agent-tools#910): TTL ≤ 10 min, ≤ 3 live pods per host with a queue, a daily $ budget, trivial/no-op run refusal, bounded failures, multi-dir sync, dedup, and an always-written ledger with `--report`.

**Architecture:** `skills/universal/runpod-offload/runpod-run` stays the bash entry point (pod lifecycle, ssh, rsync). All POLICY moves into a new stdlib-only Python helper in the same skill dir, `skills/universal/runpod-offload/runpod_run_policy.py`, invoked as `python3 "$(dirname "$(readlink -f "$0")")/runpod_run_policy.py" <subcommand> …` (rig copies the whole skill dir, so the helper travels with the script). Each subcommand is a pure function plus a thin CLI, so pytest exercises the policy directly (the "unit-level harness") and the bash tests stay offline.

**Tech Stack:** bash 5, jq, curl, flock; Python 3 stdlib (no third-party imports); pytest.

**Spec:** binding brief (session artifact `runpod-enforce-brief.md`, rulings R1–R9) and evidence `runpod-spend-2026-10-15.md` §2/§4. Excerpts that bind are copied into Global Constraints below.

## Global Constraints

- R1: `--ttl` default 0 (terminate after run). Max 10 min; a larger value is clamped to 10 with a stderr note `runpod-run: --ttl N clamped to 10 min (max)`. Idle is counted from the end of the last run (current behavior: the timer is armed in cleanup). SKILL.md: remove "Prefer `--ttl 30`"; say "`--ttl` only when you will run again within 10 min". AGENTS.md `## Heavy work runs on Runpod` example `--ttl 30` → `--ttl 10`.
- R2: at most 3 live runpod-run pods per host. Extra runs QUEUE (FIFO), print `runpod-run: queued for a pod slot (position N of M; 3 live: <names>)` when the position changes, wait up to `--queue-timeout MINUTES` (default 60), then exit 22. A run whose repo's pod is already live (its `rr-<host>-<repo>` marker is live) reuses it and takes no new slot.
- R3: daily budget, default $6 per LOCAL day. Config `~/.config/runpod-run/budget` where `~` is the account's passwd home (amended in fix round 1: `HOME` and `XDG_CONFIG_HOME` are ignored so no env var can move the files; tests use a fake `getent` on PATH), line `daily_usd = <number>`. Owner override `…/runpod-run/budget-override` containing `<YYYY-MM-DD> <usd>`; applies only when the date is today. No env var may raise the cap. Before every create AND every start of a stopped pod: `billed_today + Σ live account pods' cost/h × T + new pod $/h × T ≥ cap` → refuse, exit 21, message ends with an escalation that names `tg --tag decision --title "runpod-run daily budget reached"` and the decision-request format (amended in fix round 1: a bare pasteable `tg --tag decision "…"` fails tg's decision gate). T = this run's `--timeout` in hours. billed_today = Σ `records[].totalAmount` of `GET $API/billing?bucketSize=hour&startTime=<local midnight, UTC ISO Z>&endTime=<now UTC>` (verified 2026-10-16: the local day 2026-10-15 sums to $14.44), cached in `$STATE/billing-today.json` for ≤ 5 min. Billing unreadable → refuse (fail closed, exit 21, says why). Reusing an already RUNNING pod is not a create/start → no budget check.
- R4: refuse (exit 20) a command that is a single test-file invocation — `bun test` / `pytest` (also via `uv run [--with …]`, `python -m`, `poetry run`) / `vitest [run]` / `jest` (also via `npx`/`bunx`/`pnpm exec`) / `go test` with no directory operand AND (exactly one file operand OR a `-t`/`-k`/`-run` filter) — message `run it locally: one test file does not need a pod (heavy-op-memory-gate allows 1-2 files)`, unless `--remote-reason "<text>"`.
- R5: ssh wait max 180 s → terminate the pod, exit 23. `POST /pods` capacity errors (HTTP 400 whose message contains `no longer any instances` or `no instances available`, case-insensitive) retry the SAME flavor, at most 3 retries, backoff 15/30/60 s; never change flavor. Flavors with vCPUs > 4 OR memory > 16 GB (cpu3c 2 GB/vCPU, cpu3g 4, cpu3m 8) and any `--gpu` require `--reason "<text>"` (else exit 20). `--gpu` additionally refused (exit 20) when the command text does not match the CUDA heuristic (`cuda|torch|nvidia|nvcc|gpu|tensorflow|jax|triton|vllm|llama|whisper|diffusers|transformers|comfy|ollama|onnxruntime-gpu|\.cu\b`, case-insensitive) unless the `--reason` text matches `cuda|gpu`.
- R6: `--sync-dir` repeatable. The FIRST is the primary (pod identity, command cwd, as today); each extra dir syncs to `/workspace/<its repo_tag>` (non-git dir: `slug(basename)-<8 hex sha256 of abs path>`). The command gets `RUNPOD_RUN_SYNC_DIRS` = remote paths in argument order, `:`-joined, and the log prints `local -> remote` per dir. Refuse (exit 20) a no-op command: exactly `true`, `:`, `/bin/true`, `/usr/bin/true`, or `exit 0`.
- R7: dedup key = sha256 over (each synced dir's file list `git ls-files -z --cached --others --exclude-standard` with per-file content hash via `git hash-object --stdin-paths`, sorted; non-git dir: sorted relative paths + sha256 of contents) + the quoted command + the compute shape (`wanted_shape`). Stored per repo_tag in `$STATE/dedup/<repo_tag>.json` = `{key, ts, exit, log}` after any run whose command actually ran (exit not in 2,3,20,21,22,23,255). If the new key equals the stored key and ts < 24 h ago: print the last 40 lines of the stored log, then `runpod-run: identical to the run at <ts> (exit N); no pod started — pass --rerun to run again`, exit N. `--rerun` skips the lookup. `--dry-run` reports `dedup: hit (would replay exit N from <ts>)` or `dedup: miss`.
- R8: every invocation in run mode appends exactly one JSON line to `$STATE/ledger.jsonl` (written by the script itself, never via stderr) with keys: `ts` (ISO local with offset), `event` (`run`|`refused`|`dedup`|`queue-timeout`|`budget`|`terminate`), `repo`, `repo_tag`, `cmd`, `pod_id`, `pod_name`, `flavor`, `usd_per_h`, `boot_s`, `sync_s`, `bootstrap_s`, `command_s`, `idle_s`, `exit`, `est_usd`, `agent` (`$RIG_AGENT_ID`), `session` (first non-empty of `$OMP_SESSION_ID`, `$OMP_SESSION_DIR`, `$CLAUDE_SESSION_ID`, `$CODEX_SESSION_ID`), `parent` (parent process cmdline, 200 chars), `reason`, `remote_reason`. A TTL timer's `--stop` appends `event: terminate` with `idle_s` = now − end of the last run and its `est_usd`. `est_usd` = usd_per_h × (boot+sync+bootstrap+command [+idle]) / 3600. `runpod-run --report [--day YYYY-MM-DD]` (default today, local) prints: billed $ (billing API, if a key is present), ledger $ by category A command (exit 0/1) / B boot+sync+bootstrap / C idle / D failed before command / E failed run (other exits) / G unattributed (billed − ledger), runs per repo and per agent, trivial (<30 s command) count, refused/dedup/queue/budget counts. A day with no ledger lines falls back to the legacy run logs `$STATE/runs/YYYYMMDD-HHMMSS-<repo_tag>.log` (start = name timestamp, end = mtime, command_s = end − start) and prints `legacy: N runs from run logs (no ledger) — categories B-E unavailable`, plus billed $ and G = billed − (command $ at $0.16/h).
- R9: `reap_leftovers` never terminates a pod whose per-pod lock `$STATE/locks/<name>.lock` is held (`flock -n` probe on a spare fd, released immediately). The TTL timer's own `--stop` logs `pod <id> reached its --ttl deadline; terminating it` instead of `… no timer alive (reboot?)`; the reboot line stays only when the recorded timer pid is not `$$`.
- Exit codes (document in the script header): 20 policy refusal, 21 daily budget, 22 queue timeout, 23 pod not reachable over ssh within 3 min; existing 2/3/124/255 unchanged.
- Account state (amended in Task 4 fix round 1): the enforcement state — `ledger.jsonl`, `slots/`, `queue/`, `slots.lock`, `budget.lock`, `billing-today.json`, and Task 6's `dedup/` — lives in `$ACCOUNT_STATE` = `<passwd home>/.cache/runpod-run` (getent, else the helper's `account-home`), never under `$RUNPOD_RUN_STATE`/`$HOME`; read every `$STATE/<those>` above as `$ACCOUNT_STATE/<those>`. `RUNPOD_RUN_STATE` keeps only known_hosts, run logs, `ttl.log`, `<id>.ttl`/`<id>.lastrun`, `creating.*`/`running.*` and the per-pod locks (`locks/<name>.lock`, which R9's reaper probes).
- Offline tests only: `RUNPOD_API_URL` points at a local fake HTTP server or TEST-NET (see `tests/test_runpod_offload.py`), `HOME`/`RUNPOD_RUN_STATE` in tmp, and the account home on the fake-`getent` seam (`_account_env`), so no test writes the real account state. Never reach the real account.
- shellcheck clean (`test_shellcheck_clean`). Docs English-only. Local test runs: ONE test file at a time under `nice -n19 ionice -c3`; the full suite is the PR CI gate.

## Review Focus

- A caller that pipes stderr away (`runpod-run … 2>/dev/null | tail`) still gets a ledger line (R8 writes to the file directly).
- A killed run (SIGKILL) leaves no slot held forever: a slot marker whose owner pid is dead or no longer `runpod-run` is ignored.
- Clock: "today" is the LOCAL day; local midnight converts correctly to UTC (CEST +02:00).
- `--dry-run` never creates/starts a pod, but does read billing + catalog (read-only) when a key exists.
- A `--ttl`-kept pod still counts as one of the 3 live slots until its timer terminates it.

---

### Task 1: policy helper — classifiers and refusals (R1, R4, R5 reason/gpu, R6 no-op)

**Files:**
- Create: `skills/universal/runpod-offload/runpod_run_policy.py`
- Modify: `skills/universal/runpod-offload/runpod-run` (flags `--reason`, `--remote-reason`; TTL clamp; refusals before any network call; header exit-code table)
- Modify: `skills/universal/runpod-offload/SKILL.md` (TTL wording per R1, refusal rules), `AGENTS.md` (`--ttl 30` example → `--ttl 10`)
- Test: `tests/test_runpod_run_policy.py` (new), `tests/test_runpod_offload.py`

**Interfaces:**
- Produces (Python): `classify_trivial(argv: list[str], cwd: str) -> bool`, `is_noop(argv: list[str]) -> bool`, `needs_cuda(argv: list[str]) -> bool`, `flavor_needs_reason(spec: str) -> bool`; CLI `runpod_run_policy.py check --cwd DIR --shape cpu3g-4|gpu:<id> [--reason T] [--remote-reason T] -- <cmd…>` → exit 0 ok, exit 20 with the refusal text on stderr.
- Produces (bash): `policy()` helper that runs the Python file next to the resolved script path.

- [ ] Step 1: failing tests — `test_trivial_single_file` (`bun test tests/x.test.ts` → True; `uv run --with pytest pytest tests/test_a.py -q` → True; `pytest -k foo` → True; `pytest tests/` → False; `pytest tests/a.py tests/b.py` → False; `go test ./...` → False), `test_noop` (`true`, `:`, `exit 0` → True; `true && make` → False), `test_needs_cuda` (`python train.py --device cuda` True; `bun test x.ts` False), `test_flavor_reason` (cpu3g-4 False, cpu3c-4 False, cpu3m-2 False, cpu3m-4 True, cpu3c-8 True); script-level: `--ttl 30 --dry-run` prints the clamp note and `terminate after 10 idle minutes`; `--dry-run -- bun test tests/x.test.ts` exits 20 with "run it locally"; same with `--remote-reason "flaky only on linux"` exits 0; `-- true` exits 20; `--cpu cpu3c-8 --dry-run -- pytest tests` exits 20 without `--reason`.
- [ ] Step 2: run the two test files → FAIL.
- [ ] Step 3: implement; refusals happen in run mode right after argument parsing (dry-run included), before any API call.
- [ ] Step 4: tests PASS (one file at a time).
- [ ] Step 5: commit `feat(runpod-run): refuse trivial, no-op and unreasoned big runs; clamp --ttl to 10 min (Refs #910)`.

### Task 2: ledger + report (R8)

**Files:** modify `runpod_run_policy.py` (`ledger-append`, `report`), `runpod-run` (phase timers boot/sync/bootstrap/command; one ledger line on every exit path via the EXIT trap, including refusals from Task 1; the `--stop` path appends `terminate`; new mode `--report [--day D]`), SKILL.md; tests in `tests/test_runpod_run_policy.py` + `tests/test_runpod_offload.py`.

**Interfaces:** Consumes Task 1's `policy()`. Produces `ledger_append(path: Path, record: dict) -> None` (O_APPEND single write, creates parents), `report(state: Path, day: date, billed_usd: float | None) -> str`, CLI `runpod_run_policy.py ledger-append --state DIR --json '<obj>'` and `report --state DIR --day YYYY-MM-DD [--billed USD]`.

- [ ] Step 1: failing tests — refusal run with `2>/dev/null` still appends one `event: refused` line with `agent` from `RIG_AGENT_ID`; `report` over a synthetic ledger yields the exact A/B/C/D/E/G $ split; legacy fallback over three fake `runs/20261015-*.log` files with set mtimes reports `legacy: 3 runs`.
- [ ] Step 2: FAIL. Step 3: implement (report billed $ via the same billing fetch Task 3 adds — in this task pass `--billed` only when a key is present, using a minimal `billing_today` call in bash; Task 3 replaces it with the cached helper). Step 4: PASS.
- [ ] Step 5: commit `feat(runpod-run): append every run to ledger.jsonl and add --report (Refs #910)`.

### Task 3: daily budget (R3)

**Files:** `runpod_run_policy.py` (`budget`), `runpod-run` (call before `POST /pods` and before `pod_action start`; `--dry-run` prints `budget: today $B billed + $L live/committed + $N this run = $T of $CAP cap → ok|REFUSE`), SKILL.md; tests.

**Interfaces:** Produces `local_day_window(now: datetime) -> tuple[str, str]` (UTC ISO Z strings), `billed_today(api: str, key: str, state: Path, now) -> float` (5-min cache file `billing-today.json` keyed by local date), `daily_cap(config_dir: Path, today: date) -> float`, CLI `budget --api URL --state DIR --price P --timeout-s S --live-json FILE` → prints the line, exit 0 or 21.

- [ ] Step 1: failing tests — `local_day_window` for 2026-10-16 01:30 CEST → (`2026-10-15T22:00:00Z`, now); cap default 6.0, file `daily_usd = 4.5` → 4.5, override `2026-10-16 9` applies only on 2026-10-16; fake billing server returning hourly records summing $5.90 + new $0.16/h × 1 h → refuse 21 with `tg --tag decision`; cache hit within 5 min makes no HTTP call; unreachable billing → 21.
- [ ] Step 2 FAIL → Step 3 implement → Step 4 PASS.
- [ ] Step 5: commit `feat(runpod-run): enforce a daily $ budget from live billing (Refs #910)`.

### Task 4: pod slots + queue (R2)

**Files:** `runpod_run_policy.py` (`slots`), `runpod-run` (acquire before `ensure_pod` when the repo's pod is not already live; release in cleanup unless the pod is kept by `--ttl`, in which case the marker's owner becomes the timer pid; `--queue-timeout`), SKILL.md; tests.

**Interfaces:** Produces `live_slots(state: Path) -> list[Slot]` (marker dir `$STATE/slots/<pod_name>` containing `<owner pid>`; live iff `/proc/<pid>/cmdline` contains `runpod-run`), `acquire(state, pod_name, pid, timeout_s, max_pods=3, on_position=callable) -> bool` (global `flock $STATE/slots.lock` around read-modify; FIFO tickets `$STATE/queue/<ns-timestamp>-<pid>`; a live marker for `pod_name` → immediate True without a new slot), `handover(state, pod_name, new_pid)`, `release(state, pod_name, pid)`. CLI `slots acquire|handover|release|list`.

- [ ] Step 1: failing tests (unit harness with fake pids via a `proc_root` seam): three live markers → 4th caller waits and reports position 1; releasing one admits it; same-pod_name caller passes immediately; dead-owner marker ignored; timeout → False and the CLI exits 22; FIFO order of two waiters.
- [ ] Step 2 FAIL → Step 3 implement → Step 4 PASS.
- [ ] Step 5: commit `feat(runpod-run): cap live pods at 3 per host with a FIFO queue (Refs #910)`.

### Task 5: failures, sync dirs, reaper (R5 ssh/capacity, R6 multi --sync-dir, R9)

**Files:** `runpod-run` (`wait_ready` 180 s → exit 23; capacity retry loop in `ensure_pod`; repeatable `--sync-dir` with per-dir rsync + `RUNPOD_RUN_SYNC_DIRS`; `reap_leftovers` lock probe; ttl.log wording), SKILL.md; tests in `tests/test_runpod_offload.py` using the existing fake HTTP server pattern.

- [ ] Step 1: failing tests — fake API answers `POST /pods` 400 "no longer any instances" 4×: exactly 4 POSTs, all with the same `cpu.id`, exit 3; `--dry-run --sync-dir A --sync-dir B` lists both `local -> remote` lines; `reap_leftovers` with a `running.<deadpid>` marker whose pod lock is held by the test does not DELETE (fake server records no DELETE); the timer-path message test (ttl file pid == script pid) prints `reached its --ttl deadline`.
- [ ] Step 2 FAIL → Step 3 implement → Step 4 PASS.
- [ ] Step 5: commit `fix(runpod-run): 3-min ssh wait, same-flavor capacity retries, multi-dir sync, lock-aware reaper (Refs #910)`.

### Task 6: dedup (R7)

**Files:** `runpod_run_policy.py` (`dedup-key`, `dedup-lookup`, `dedup-store`), `runpod-run` (`--rerun`; lookup after policy refusals and before slots/budget; store after a run whose command ran), SKILL.md; tests.

**Interfaces:** Produces `tree_key(dirs: list[Path], cmd: str, shape: str) -> str`, `lookup(state, repo_tag, key, now) -> Hit | None`, `store(state, repo_tag, key, exit_code, log_path, now)`.

- [ ] Step 1: failing tests — same tree+cmd+shape → same key; editing an untracked-not-ignored file changes it; ignored file does not; lookup within 24 h hits, at 24 h + 1 s misses; script `--dry-run` reports `dedup: hit` after a stored record; a real (non-dry) run against the fake API with a stored hit prints the log tail, exits with the stored code and makes no API call; `--rerun` bypasses.
- [ ] Step 2 FAIL → Step 3 implement → Step 4 PASS.
- [ ] Step 5: commit `feat(runpod-run): replay identical runs from a 24 h dedup cache (Refs #910)`.
