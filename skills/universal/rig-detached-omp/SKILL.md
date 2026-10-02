---
name: rig-detached-omp
description: >-
  Use when an omp (oh-my-pi) orchestrator must dispatch a NON-TRIVIAL subagent in the
  background and cannot use the native `task` tool's async job for it (e.g. it needs to
  survive the current session ending, or run outside a task-tool call entirely). Run the
  bundled rig-detached-omp launcher - it detaches an `omp -p` child carrying
  RIG_AGENT_ID/RIG_DETACHED_AGENT so the hook bridge classifies every tool call in the child
  session as a dispatched subagent.
---

# rig-detached-omp — canonical detached-agent launcher for omp (oh-my-pi)

omp's own `task` tool already dispatches a non-blocking spawn as a background
`AsyncJobManager` job by omp's own documented contract (docs/tools/task.md) — for MOST
subagent dispatches from inside an omp session, `task` itself is the sanctioned background
mechanism and needs no launcher at all (`background-subagent-gate` already allows it
unconditionally: see `omp_hook_bridge`'s `_normalize_task_args`). This skill's launcher exists
for the narrower case a `task` dispatch cannot cover: a TRULY detached process that keeps
running after the calling session itself ends, or delegated work started outside any omp
session's `task` call in the first place (e.g. a script or another harness kicking off omp
work). It carries the same launcher-env identity marker `rig-detached-opencode` and
`rig-detached-codex` use, so every hook still classifies its tool calls as a subagent's.

## Where the launcher lives (the carrier)

This skill IS the carrier. rig copies the whole skill dir (launcher included) to the canonical
skills dir — `~/.agents/skills/rig-detached-omp/rig-detached-omp` (default `skills_target`,
default-on via `skills.universal.all`) — mirroring `rig-detached-opencode` exactly. `bin/` in
the agent-tools repo is NOT a rig-discovered carrier, so the launcher must not be referenced
there: after `rig apply` on a provisioned machine only the skill-copy path exists. Gate REMINDER
texts name the provisioned path.

## Usage

```bash
~/.agents/skills/rig-detached-omp/rig-detached-omp <name> <brief-file> [workdir] [model]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the HANDOFF FILE
  PATH the child must write before exiting. May be relative to the caller's cwd — the launcher
  reads its content BEFORE changing directories, so a relative brief with a different `[workdir]`
  still reaches the child in full.
- `[workdir]` — where the child runs (default: the caller's cwd).
- `[model]` — optional model/provider/account to pin this child to (forwarded verbatim as omp's
  own `--model` flag, e.g. `opus`, `opencode-go/mimo-v2.5`, `anthropic/claude-opus-4-5`; omp
  fuzzy-matches it). Omit it (or pass `""`) to keep the child on the orchestrator's config.yml
  `default` role. Use this when a caller needs a SPECIFIC model/account for one dispatch —
  config.yml routing does not hot-reload into already-dispatched sessions (agent-tools#687), so
  editing the config after the fact never reaches a live or already-launched child.

The launcher returns immediately (the child is `nohup`-detached); the child's stdout/stderr
append to `/tmp/agent-logs/<name>.log`. Collect the result by polling the handoff file named in
the brief — the launcher keeps no registry by design.

The child runs as `omp -p --auto-approve --no-session --cwd <workdir> [--model <model>] -- <brief>`:
`-p`/`--print` for non-interactive process-and-exit, `--auto-approve` because a detached
background process has no human present to approve tool calls, `--no-session` since a
one-shot detached dispatch doesn't need session persistence (the handoff file named in the
brief is the actual result contract, not the omp session log), and `--model` only when the
optional `[model]` arg is given.

## Why the process env is the identity source

The launcher exports `RIG_AGENT_ID=<name>` and `RIG_DETACHED_AGENT=1` into the child `omp`
process. omp's extension loader spawns `omp_hook_bridge` with the extension process's own
environment (`extension.ts`'s `spawnSync(..., { env: { ...process.env, ... } })`), so the
markers set when the child omp process was launched are visible to
`lib/omp_hook_bridge/dispatch.py` on every tool call in that session. The bridge strips every
model/tool-supplied `agent_id`-shaped key (forged args can never self-exempt) and injects
`args.agent_id` from these markers instead (`_detached_agent_id`, agent-tools#573, ported
unchanged from `opencode_hook_bridge`'s agent-tools#476), so every subagent-exempt delegation
gate (`orchestrator-stays-thin`, `background-subagent-gate`, `no-long-inline-process`,
`subagent-no-bg-longproc`) classifies the child session's tool calls as a dispatched
subagent's.

Never `export` these markers by hand in an interactive shell (or an rc file): every omp process
that inherits them is treated as a dispatched subagent for its whole session. The launcher sets
them for the child process only.

Trust reasoning: a running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the sanctioned
act of dispatching a subagent. This matches the module family's cooperative-orchestrator threat
model (`on_error: open` discipline gates, not security boundaries).
