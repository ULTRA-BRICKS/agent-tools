---
name: runpod-offload
description: 'Use whenever a command would load the workstation for more than a minute — a full test suite (`bun test`, `pytest`), a build (docker, OrcaSlicer, native), a benchmark, ML training/inference, or a media batch. Run it on a Runpod pod with one line, `runpod-run -- <command>`, instead of locally: the script rents the cheapest pod that fits, syncs the checkout, bootstraps the toolchain, streams the output, returns the exit code and deletes the pod. Locally: a single test file at most.'
---

# runpod-offload — heavy work runs on Runpod, not on the workstation

The shared workstation runs many agent sessions, a review queue and the owner's own
desktop. A full test suite fanned out over every core, a docker build or a benchmark
starves all of them at once (it froze the machine twice in one day). **The rule: locally,
run at most one test file or a quick check; anything heavier goes to a Runpod pod** with the
one-liner this skill ships. Billing is per second on the cheapest pod that fits, and the
pod is gone when the command ends.

## Usage

```bash
runpod-run -- bun test ./tests                         # full suite, 4-vCPU CPU pod ($0.16/h), pod deleted after
runpod-run -- uv run --with pytest pytest tests -q     # pytest; uv is always on the pod
runpod-run --cpu cpu3c-8 --root --reason "build uses 8 cores" -- docker build .   # 8 vCPU ($0.24/h); above 4 vCPU / 16 GB needs --reason
runpod-run --gpu "NVIDIA GeForce RTX 4090" --reason "cuda bench" -- python bench.py   # GPU pod (CUDA >= 12.8 host)
runpod-run --ttl 10 -- bun test ./tests                # keep the pod 10 idle minutes for the next run (the max)
runpod-run --sync-dir ~/src/app --sync-dir ~/src/lib -- make test   # a second checkout travels in the same run
runpod-run --dry-run -- bun test                       # the plan, price, pod slots, dedup and budget line; creates nothing (read-only lookups)
runpod-run --rerun -- bun test                         # run again although an identical run finished within 24 h
runpod-run --status                                    # pods this machine created + every other pod on the account, $/h, age; the host's pod slots
runpod-run --stop [pod-id|all]                         # terminate them (default: this repo's pod)
runpod-run --report [--day 2026-10-15]                 # a local day's spend: billed $ vs. the ledger, by category
```

The provisioned path is `~/.agents/skills/runpod-offload/runpod-run` (rig copies the whole
skill dir there). Make it a plain command once per machine:

```bash
ln -sf ~/.agents/skills/runpod-offload/runpod-run ~/.local/bin/runpod-run
```

All options: `runpod-run --help`. The ones that matter day to day: `--cpu FLAVOR-VCPUS`
(`cpu3c` 2 GB/vCPU, `cpu3g` 4 GB/vCPU — the default `cpu3g-4` is 4 vCPU / 16 GB, `cpu3m`
8 GB/vCPU; vCPUs a power of two, 2 or more; container disk at most 10 GB per vCPU), `--gpu GPU_TYPE_ID` (ids from `runpodctl gpu list`),
`--apt pkg,pkg` (extra system packages), `--bootstrap CMD` / `--no-bootstrap` (replace or
skip the lockfile-detected toolchain install), `--no-js` / `--no-python` (skip one half — a
bun repo whose `uv.lock` pulls torch does not need it for `bun test`), `--timeout SECONDS`
(default 3600, exit 124), `--queue-timeout MINUTES` (how long to wait for a pod slot, default
60, exit 22), `--tty` (a pty for tools that insist on one), `--sync-dir DIR` (repeatable:
the first is the run's checkout, each further one lands in `/workspace/<its repo>`),
`--reason TEXT` (why a run needs more than `cpu3g-4`),
`--remote-reason TEXT` (why a single test file must run on a pod).

## What one run does

1. **Pod**: takes one of the host's 3 pod slots (below), then finds a pod named
   `rr-<host>-<repo>-<hash>` (the clone's directory name plus 8
   hex of its git common dir, so two clones that share a name never share a pod; every
   worktree of one clone does) — reuses a RUNNING one, starts an EXITED one, otherwise
   creates one on Secure Cloud, printing the hourly price from the live catalog first.
   A create that finds no free machine of the flavor (`no longer any instances`) is retried
   up to 3 times, 15/30/60 s apart, always with the same flavor; then exit 3. The pod must
   answer ssh within 3 minutes, else it is terminated and the run exits 23.
   A found pod whose hardware differs from this run's `--cpu`/`--gpu` is replaced, so a
   GPU job never lands on last run's CPU pod. A bare `--stop` ends every pod this
   checkout created (a `--name` one too, by the repo tag stamped on it at create time).
   Every pod it makes carries the `rr-<host>-` prefix (a `--name` is prefixed too), so
   `--stop` only ever touches pods this machine's `runpod-run` created (`--status` lists
   those first, then every other pod on the account with its $/h and age, and the
   account's total $/h — the number a watcher quotes). Runs
   on one pod from this machine are serialized by a local lock, so two sessions on the
   same repo queue instead of syncing over each other.
2. **Sync**: `rsync` of the checkout to `/workspace/<repo>` on the pod, owned by an
   unprivileged `runner` user (as root, permission tests cannot fail the way they must;
   `--root` for a docker build) — tracked plus untracked-not-ignored files, uncommitted
   work included; everything git ignores (all `.gitignore`s, `.git/info/exclude`, the
   global ignore file) plus `.git`, `node_modules`, `.venv` and `.worktrees` never travel.
   On a reused pod the sync is incremental and mirrors deletions; the pod's own
   `node_modules`/`.venv` survive. A git snapshot commit is made there after every sync,
   so tests that call `git ls-files`/`rev-parse` work. Every further `--sync-dir` is synced
   the same way to `/workspace/<its repo tag>` (a dir outside git: `<name>-<8 hex of its
   path>`, without a snapshot), and the log prints `<local> -> <remote>` for each. Two dirs
   that would land in one remote dir (the same checkout twice, two worktrees of one clone)
   are refused (exit 2).
3. **Bootstrap** (once per pod, keyed on the plan): base packages + `uv` always;
   `bun` matching the local version for `bun.lock`/`bun.lockb` (or a bun repo whose lockfile
   is ignored — `package.json` scripts calling `bun`), `node` matching the local version
   for `pnpm-lock.yaml`/`yarn.lock`/`package-lock.json`/`package.json`; then the matching
   install (`bun install --frozen-lockfile`, `pnpm install --frozen-lockfile`, `npm ci`,
   `uv sync --frozen` for `uv.lock`, `uv sync` for `pyproject.toml`, `uv pip install -r
   requirements.txt`). The install step reruns on every sync (cheap when nothing changed).
   A bun repo whose tests also shell out to `node` adds `--apt nodejs`.
4. **Run**: `cd /workspace/<repo> && timeout <timeout>s <command>` as `runner` with `CI=1`,
   `RUNPOD_RUN=1`, `RUNPOD_RUN_SYNC_DIRS` (every synced dir's remote path in argument order,
   `:`-joined), and `/opt/bun/bin`, `/opt/node/bin`, `/opt/uv`, `node_modules/.bin`,
   `.venv/bin` on `PATH`. Output streams to the terminal and is saved to
   `~/.cache/runpod-run/runs/<timestamp>-<repo>.log`. **The exit code is the remote
   command's.**
5. **Teardown**: the pod is terminated — on success, failure, or Ctrl-C (an `EXIT` trap
   fires even when the pod was created but never answered ssh) — unless `--ttl N` (N at
   most 10; a larger value is clamped to 10 with a note on stderr): then
   a detached local timer terminates it after N idle minutes and every new run on that repo
   resets the timer. The deadline is also written next to the timer's pid, so if the
   timer dies (reboot, suspend, kill) the next `runpod-run` of any mode on this machine
   reaps the expired pod — and likewise a pod whose run was SIGKILLed / OOM-killed mid-way
   (every run records its pod id while it is up). Only pods this machine's `runpod-run`
   created are ever reaped, and never one whose per-pod lock a live run holds (that run is
   on it). A bare `--keep` (no deadline) is refused: no pod bills forever.
   The kept-pod line prints the pod's $/h. `--dry-run` prints steps 1-5, the pod slots
   (live, queued, and whether this run would reuse its pod's slot, take a free one or queue),
   the dedup line (`dedup: hit (would replay exit N from <ts>)` or `dedup: miss`) and the
   budget line a start or create would face (with a key it reads the catalog, the pod list
   and today's billing, all read-only) and exits.
6. **Ledger**: every run appends one JSON line to `~/.cache/runpod-run/ledger.jsonl` — also
   a refusal, a usage error or a Ctrl-C, never a `--dry-run` — written by the script itself,
   so `runpod-run … 2>/dev/null` still leaves it. Keys: `ts` (local, with offset), `event`
   (`run`, `refused`, `terminate`, …), `repo`, `repo_tag`, `cmd`, `pod_id`, `pod_name`,
   `flavor`, `usd_per_h`, `boot_s`/`sync_s`/`bootstrap_s`/`command_s`/`idle_s`, `exit`,
   `est_usd` ($/h × those seconds), `agent` (`$RIG_AGENT_ID`), `session` (the first of
   `$OMP_SESSION_ID`, `$OMP_SESSION_DIR`, `$CLAUDE_SESSION_ID`, `$CODEX_SESSION_ID`),
   `parent` (the caller's command line), `reason`, `remote_reason`. A `--stop` (the `--ttl`
   timer's too) that terminates a pod, and the reaper ending a kept pod whose timer died,
   append `event: terminate` with the pod's idle seconds since its last run; a run that
   reuses a kept pod books that idle time on its own line.

`runpod-run --report [--day YYYY-MM-DD]` (default today, local day) prints the day's billed $
(billing API, when a key is present) against the ledger: A command time of runs that exited
0/1, B boot+sync+bootstrap of runs that reached their command, C idle time of `--ttl`-kept
pods, D runs that failed before their command, E command time of runs that exited otherwise,
G unattributed = billed − (A..E); then runs per repo and per agent, trivial (<30 s)
commands, and the refused / dedup / queue-timeout / budget / terminate counts. A day with no
ledger line (before the ledger existed) is read from the run logs
`~/.cache/runpod-run/runs/YYYYMMDD-HHMMSS-<repo>.log` instead: command time only, at $0.16/h.

Exit codes: the command's own; `124` timeout; `2` usage/config (no API key, not a git
checkout); `3` the pod could not be created (no capacity after 3 retries included),
started or bootstrapped; `255` ssh failure; `20` policy refusal (below); `21` daily budget
reached or unverifiable (below); `22` no pod slot came free within `--queue-timeout`
(below); `23` the pod did not answer ssh within 3 minutes (it is terminated, `--ttl` or
not).

**Daily budget (exit 21)**, checked before every create and every start of a stopped pod
(reusing a RUNNING pod is neither): today's billed $ (the billing API from local midnight,
cached 5 min in `~/.cache/runpod-run/billing-today.json`) + every live pod's $/h on the
account × this run's `--timeout` + the new pod's $/h × `--timeout` must stay below the cap,
else the run is refused before any pod bills. Billing or the price unreadable refuses too
(fail closed). The cap is the owner's: $6 per local day, unless `~/.config/runpod-run/budget`
says `daily_usd = <number>`; a line `<YYYY-MM-DD> <usd>` in `~/.config/runpod-run/budget-override`
sets it for that day only. `~` is the account's home from the passwd database: `HOME` and
`XDG_CONFIG_HOME` are ignored, so no environment variable moves or raises the cap. **Agents
never create or edit these files**: on exit 21, send the owner a decision request with
`tg --tag decision --title "runpod-run daily budget reached"` in the decision-request format
(`tg help format`), quoting the figures the refusal prints, and wait for the answer. A
shorter `--timeout` commits less, when the command really needs less.

**Pod slots (exit 22)**: at most 3 `runpod-run` pods are live on this host at once. A run
takes a slot before it reuses, starts or creates its pod (so before the budget check) and
holds it until the pod is terminated; a pod kept by `--ttl` keeps its slot until its timer
ends it, and the next run of that repo reuses the pod and its slot. A run that finds 3 live
queues in FIFO order, printing `runpod-run: queued for a pod slot (position N of M; 3 live:
<names>)` whenever its position changes, and after `--queue-timeout` minutes (default 60)
exits 22 without starting anything. A slot is the marker `~/.cache/runpod-run/slots/<pod
name>` naming the pid of the `runpod-run` that holds it, so a killed run's slot frees itself;
`--status` lists the slots and their holders.

**Account state**: the ledger, the pod slots and their queue, the billing cache and the
budget lock live in `~/.cache/runpod-run` of the account's passwd home, like the budget
files: `HOME` and `RUNPOD_RUN_STATE` do not move them, so no environment variable gives a run
fresh slots or an empty ledger. `RUNPOD_RUN_STATE` relocates only known_hosts, run logs,
`--ttl` timers and per-pod locks.

**Dedup (24 h)**: a run identical to one that finished within the last 24 hours is not run
again. Identical means the same files with the same contents in every `--sync-dir` (what
the sync sends: tracked + untracked-not-ignored), the same command and the same settings
(flavor, image, `--root`, `--timeout`, bootstrap, the set of dirs). Such a run prints the
last 40 lines of the earlier run's log and `runpod-run: identical to the run at <ts> (exit
N); no pod started — pass --rerun to run again`, books
an `event: dedup` ledger line and exits with the earlier exit code: no API call, no pod
slot, no pod. The check comes after the refusals below and before the slot and budget.
Only runs whose command ran are recorded (not exits 2, 3, 20-23, 255), one per repo, in
`~/.cache/runpod-run/dedup/<repo tag>.json` of the account home. `--rerun` skips the check
(a flaky test, an outside service that changed); its result becomes the one replayed next.

Refused before any network call, `--dry-run` included (exit 20):

- a no-op command (`true`, `:`, `/bin/true`, `/usr/bin/true`, `exit 0`), also behind `env
  VAR=…` or `sh -c '…'`;
- a single test-file invocation — `bun test`, `pytest` (also via `uv run`, `python -m`,
  `poetry run`), `vitest`, `jest` (also via `npx`/`bunx`/`pnpm exec`), `go test` with no
  directory operand and exactly one file or a `-t`/`-k`/`-run` filter — also behind `env
  VAR=…` or a one-command `sh -c '…'`: run it locally, unless `--remote-reason "<text>"`
  says why it needs a pod;
- a flavor above 4 vCPU or 16 GB, or any `--gpu`, without `--reason "<text>"`;
- a `--gpu` run whose command shows no sign of CUDA (torch, nvcc, vllm, transformers, …)
  unless the `--reason` mentions `cuda` or `gpu`.

## Prerequisites (once per machine)

- A Runpod API key: `export RUNPOD_API_KEY=...` or the `apikey` that `runpodctl doctor` /
  `flash login` write to `~/.runpod/config.toml`. Without one the script exits 2 with the
  instructions above, before any network call.
- An SSH key registered on the account: `runpodctl doctor` creates and registers
  `~/.runpod/ssh/runpodctl-ssh-key`; override with `RUNPOD_RUN_SSH_KEY`. Runpod injects the
  account's keys at pod boot, so register the key before the first run.
- `curl`, `jq`, `rsync`, `ssh`, `git`, `flock`, `python3` on PATH.

## Rules for the agent

- **Before running anything heavy, run it through `runpod-run`.** "Heavy" = a whole test
  suite, a build, a benchmark, ML or media batch work, anything that would keep several
  cores busy for more than a minute. A single test file or a lint of changed files can stay
  local.
- **Default to the cheapest CPU pod that fits.** `cpu3g-4` ($0.16/h, 16 GB) covers most
  suites; move to `cpu3c-8` when the suite parallelizes, and only to `--gpu` when the
  command needs CUDA — both with a `--reason`. `--dry-run` shows the price; quote it in your report.
- **`--ttl` only when you will run again within 10 min**: the reused pod skips the 1-2 min
  boot + bootstrap, and the timer still ends the bill. Never leave a pod
  behind: the `runpod-run --status` "created by runpod-run" section must be empty when you finish.
- **Exit 21 is the owner's call**: do not retry, switch accounts, or touch the budget
  files; send the decision request the refusal describes and stop the heavy work until answered.
- **Exit 22 means the host already runs 3 pods**: do not loop on retries or touch the slot
  markers. Run it later, end a `--ttl`-kept pod of yours that you no longer need
  (`runpod-run --status`, then `runpod-run --stop <id>`), or wait longer with
  `--queue-timeout`.
- **No capacity (exit 3 after 3 retries) or no ssh (exit 23)**: the script already waited.
  Run it later; another flavor is your deliberate choice (above `cpu3g-4` it needs a
  `--reason`), never an automatic one.
- **A second checkout goes in the same run**: `runpod-run --sync-dir <this repo> --sync-dir
  <the other> -- <command>`; the command finds both in `RUNPOD_RUN_SYNC_DIRS`. Never stage a
  dir with a separate run first (`runpod-run --sync-dir <other> -- true` is refused as a
  no-op, and a terminated pod keeps nothing for the next run).
- **Repo-specific needs go in the repo's AGENTS.md**, not in forks of this script: extra
  system packages (`--apt tmux,ffmpeg`), a different flavor, a pre-step. A repo may wrap
  the one-liner in a thin `scripts/test-runpod.sh` that only adds those flags.
- **Secrets**: the pod gets the checkout's files, so anything git ignores (`.env`) stays
  home. The command line is logged (dry-run plan, run log, process list on this shared
  machine), so never put a secret value in it. A suite that needs one reads it on the pod
  from a file: an untracked file no ignore rule matches travels with the sync (drop a
  `ci.env` next to the suite, never commit it, then `runpod-run -- sh -c 'set -a; .
  ./ci.env; bun test'`), or `--bootstrap` fetches it from your secret store on the pod.

## Why a pod and not Runpod serverless / flash

The workload is "run this repo's command, as is" — a shell on a Linux box with the repo's
toolchain, not a Python handler. A pod gives rsync + ssh + the real command line, bills per
second and disappears after. `flash` (code-first serverless) is the right lane when you
are *writing* GPU functions; see `skill://runpod` for the full routing.
