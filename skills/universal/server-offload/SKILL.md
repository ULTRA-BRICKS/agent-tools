---
name: server-offload
description: "Use whenever a command would load the workstation for more than a minute — a full test suite (`bun test`, `pytest`), a whole-repo typecheck, knip, a build, a benchmark, a render or a big `bun install`. Run it on the owner's server 65.21.40.51 with one line, `server-run -- <command>`, instead of locally: it rsyncs the checkout, runs the command there under nice, streams the output and returns the exit code. Locally: a single test file at most."
---

# server-offload — heavy work runs on the server, not on the workstation

The shared workstation runs many agent sessions, a review queue and the owner's desktop. A
full suite fanned out over every core froze it twice in one day. **Rule: locally run at most
one test file or a lint of the changed files; anything heavier goes to the owner's server
`ultra@65.21.40.51`** (x86_64, Ubuntu 26.04, 16 cores, ~30 GB RAM; bun, uv, node, git, rsync
preinstalled; **no docker**). Owner order tg#25309: Runpod is gone, never rent pods.

```bash
server-run -- bun test                                  # full suite of the checkout in the cwd
server-run -- bash -c 'bun install && bun run test'     # several steps: one bash -c
server-run -- uv run --with pytest pytest tests -q
server-run --fetch coverage -- bun run test:coverage    # copy ./coverage back afterwards
server-run --dry-run -- bun test                        # print the plan, touch nothing
server-run --status                                     # load, free memory, busy slots, remote checkouts, disk
server-run --clean [--older-than-days 3]                # delete idle remote checkouts (default 3 days)
server-run --clean --this                               # delete this checkout's remote copy
```

Provisioned path: `~/.agents/skills/server-offload/server-run` (rig copies the skill dir).
Make it a plain command once per machine: `ln -sf ~/.agents/skills/server-offload/server-run ~/.local/bin/server-run`.
Without the wrapper the same thing by hand: `rsync -a --delete --exclude .git --exclude node_modules ./ ultra@65.21.40.51:work/sr/<name>/`
then `ssh ultra@65.21.40.51 'cd work/sr/<name> && nice -n 10 bun test'`.

## What one run does

1. **Sync**: rsync of the git checkout (tracked + untracked-not-ignored files, uncommitted work
   included) to `~/work/sr/<repo>-<8 hex of the checkout path>` on the server; files deleted
   locally are deleted there; `.git`, `node_modules` and `.venv` never travel (the server keeps its
   own, so `bun install` is incremental). One git snapshot commit is made there (hooks off), so
   tests that call `git ls-files` work. Each worktree gets its own remote copy.
2. **Slot**: waits for one of 3 job slots (flock files in `~/work/sr/.slots`), up to
   `--queue-timeout` seconds (default 3600; exit 22). The server is shared with other jobs: never
   start more than one run per task yourself.
3. **Run**: `nice -n 10 timeout <--timeout, default 3600> bash -c '<command>'` in the same
   sub-directory you called it from, output streamed back, exit code returned (124 on timeout).
   PATH has `~/.bun/bin` and `~/.local/bin`.
4. **Fetch**: each `--fetch PATH` (repo-relative) is copied back, even when the command failed.

## Rules

- **Heavy = server**: full test suites, whole-repo `tsc`/`mypy`, knip, benchmarks, renders, big
  `bun install`, builds. **Local = fine**: one test file, lint of the changed files, `--help`.
  The `server-offload-gate` hook blocks whole-suite shapes locally and prints the exact
  `server-run -- bash -c '…'` line.
- **Never the odroid** (3.7 GB RAM, the production host) and never Runpod. The laptop
  (`ultra@100.74.26.95`) is a fallback ONLY for what the server cannot do (docker builds, macOS-only).
- Chromium/Playwright on the server (Ubuntu 26.04): export `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64`
  inside the command and `bunx playwright install chromium-headless-shell` first.
- **Scratch and cleanup**: everything lives under `~/work/sr/`; keep scratch there, copy back
  only final artifacts with `--fetch`, run `server-run --clean --this` when you finish with a
  worktree (`--clean` alone removes copies idle for 3+ days). The disk is ~90 % full: do not
  leave large artifacts, check `--status` for `disk:` before big jobs.
- No root on the server: install tools user-level (`~/.bun`, `~/.local`, uv). Needing root →
  prepare the exact command and ask the owner.
- `SERVER_RUN_HOST` overrides the host (default `ultra@65.21.40.51`); ssh must work key-only
  (`BatchMode`).

Exit codes of its own: 2 usage, 22 no slot free, 124 timeout; anything else is the command's.
