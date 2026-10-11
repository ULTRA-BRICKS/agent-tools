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
runpod-run --cpu cpu3c-8 --root -- docker build .      # 8 vCPU compute-optimized ($0.24/h); --root: docker needs it
runpod-run --gpu "NVIDIA GeForce RTX 4090" -- python bench.py   # GPU pod (CUDA >= 12.8 host)
runpod-run --ttl 45 -- bun test ./tests                # keep the pod 45 idle minutes for the next run
runpod-run --dry-run -- bun test                       # the plan + price; creates nothing (only a price lookup)
runpod-run --status                                    # pods this machine created + every other pod on the account, $/h, age
runpod-run --stop [pod-id|all]                         # terminate them (default: this repo's pod)
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
(default 3600, exit 124), `--tty` (a pty for tools that insist on one), `--sync-dir DIR`
(another checkout).

## What one run does

1. **Pod**: finds a pod named `rr-<host>-<repo>-<hash>` (the clone's directory name plus 8
   hex of its git common dir, so two clones that share a name never share a pod; every
   worktree of one clone does) — reuses a RUNNING one, starts an EXITED one, otherwise
   creates one on Secure Cloud, printing the hourly price from the live catalog first.
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
   so tests that call `git ls-files`/`rev-parse` work.
3. **Bootstrap** (once per pod, keyed on the plan): base packages + `uv` always;
   `bun` matching the local version for `bun.lock`/`bun.lockb` (or a bun repo whose lockfile
   is ignored — `package.json` scripts calling `bun`), `node` matching the local version
   for `pnpm-lock.yaml`/`yarn.lock`/`package-lock.json`/`package.json`; then the matching
   install (`bun install --frozen-lockfile`, `pnpm install --frozen-lockfile`, `npm ci`,
   `uv sync --frozen` for `uv.lock`, `uv sync` for `pyproject.toml`, `uv pip install -r
   requirements.txt`). The install step reruns on every sync (cheap when nothing changed).
   A bun repo whose tests also shell out to `node` adds `--apt nodejs`.
4. **Run**: `cd /workspace/<repo> && timeout <timeout>s <command>` as `runner` with `CI=1`,
   `RUNPOD_RUN=1`, and `/opt/bun/bin`, `/opt/node/bin`, `/opt/uv`, `node_modules/.bin`,
   `.venv/bin` on `PATH`. Output streams to the terminal and is saved to
   `~/.cache/runpod-run/runs/<timestamp>-<repo>.log`. **The exit code is the remote
   command's.**
5. **Teardown**: the pod is terminated — on success, failure, or Ctrl-C (an `EXIT` trap
   fires even when the pod was created but never answered ssh) — unless `--ttl N`: then
   a detached local timer terminates it after N minutes and every new run on that repo
   resets the timer. The deadline is also written next to the timer's pid, so if the
   timer dies (reboot, suspend, kill) the next `runpod-run` of any mode on this machine
   reaps the expired pod — and likewise a pod whose run was SIGKILLed / OOM-killed mid-way
   (every run records its pod id while it is up). Only pods this machine's `runpod-run`
   created are ever reaped. A bare `--keep` (no deadline) is refused: no pod bills forever.
   The kept-pod line prints the pod's $/h. `--dry-run` prints steps 1-5 and exits.

Exit codes: the command's own; `124` timeout; `2` usage/config (no API key, not a git
checkout); `3` the pod could not be created/started/bootstrapped; `255` ssh failure.

## Prerequisites (once per machine)

- A Runpod API key: `export RUNPOD_API_KEY=...` or the `apikey` that `runpodctl doctor` /
  `flash login` write to `~/.runpod/config.toml`. Without one the script exits 2 with the
  instructions above, before any network call.
- An SSH key registered on the account: `runpodctl doctor` creates and registers
  `~/.runpod/ssh/runpodctl-ssh-key`; override with `RUNPOD_RUN_SSH_KEY`. Runpod injects the
  account's keys at pod boot, so register the key before the first run.
- `curl`, `jq`, `rsync`, `ssh`, `git`, `flock` on PATH.

## Rules for the agent

- **Before running anything heavy, run it through `runpod-run`.** "Heavy" = a whole test
  suite, a build, a benchmark, ML or media batch work, anything that would keep several
  cores busy for more than a minute. A single test file or a lint of changed files can stay
  local.
- **Default to the cheapest CPU pod that fits.** `cpu3g-4` ($0.16/h, 16 GB) covers most
  suites; move to `cpu3c-8` when the suite parallelizes, and only to `--gpu` when the
  command needs CUDA. `--dry-run` shows the price; quote it in your report.
- **Prefer `--ttl 30` when another run is likely within the hour**: the reused pod skips
  the 1-2 min boot + bootstrap, and the timer still ends the bill. Never leave a pod
  behind: the `runpod-run --status` "created by runpod-run" section must be empty when you finish.
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
