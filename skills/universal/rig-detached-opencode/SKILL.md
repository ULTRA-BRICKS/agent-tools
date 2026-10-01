---
name: rig-detached-opencode
description: 'Use when an opencode orchestrator must dispatch a NON-TRIVIAL subagent in the background on a default build (no native background task-tool field; background-subagent-gate blocks foreground). Run rig-detached-opencode — it nohup-detaches an `opencode run` child so the hook bridge treats its calls as a subagent.'
---

# rig-detached-opencode — canonical detached-agent launcher for opencode

The pre-agent gate `background-subagent-gate` blocks a non-trivial FOREGROUND subagent
dispatch: it wedges the main thread until the child finishes. Claude Code has real
background paths (`subagent_type: "fork"`, `isolation: "remote"`); a default opencode
build has NONE — its task tool carries no background field (verified on 1.18.20:
`description`/`prompt`/`subagent_type`/`task_id`/`command` only; the native
`background: true` exists only behind `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`,
and the bridge maps that field only when the hosting opencode process carries the flag).
The sanctioned background mechanism on a default build is this skill's bundled launcher.

## Where the launcher lives (the carrier)

This skill IS the carrier. rig copies the whole skill dir (launcher included) to the
canonical skills dir — `~/.agents/skills/rig-detached-opencode/rig-detached-opencode`
(default `skills_target`, default-on via `skills.universal.all`) — and opencode scans
that dir natively. `bin/` in the agent-tools repo is NOT a rig-discovered carrier, so
the launcher must not be referenced there: after `rig apply` on a provisioned machine
only the skill-copy path exists. Gate REMINDER texts name the provisioned path.

## Usage

```bash
~/.agents/skills/rig-detached-opencode/rig-detached-opencode <name> <brief-file> [workdir]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the
  HANDOFF FILE PATH the child must write before exiting. May be relative to the caller's
  cwd — the launcher reads its content BEFORE changing directories, so a relative brief
  with a different `[workdir]` still reaches the child in full.
- `[workdir]` — where the child runs (default: the caller's cwd).

The launcher returns immediately (the child is `nohup`-detached); the child's
stdout/stderr append to `/tmp/agent-logs/<name>.log`. Collect the result by polling the
handoff file named in the brief — the launcher keeps no registry by design.

## Why the process env is the identity source

The launcher exports `RIG_AGENT_ID=<name>` and `RIG_DETACHED_AGENT=1` into the child
opencode process. The opencode hook bridge (`lib/opencode_hook_bridge/dispatch.py`) strips
every model/tool-supplied `agent_id`-shaped key (forged args can never self-exempt) and
injects `args.agent_id` from these markers instead, so every subagent-exempt delegation
gate (`orchestrator-stays-thin`, `background-subagent-gate`, `no-long-inline-process`,
`subagent-no-bg-longproc`) classifies the child session's tool calls as a dispatched
subagent's.

The launcher also asks `tg --session-topic` (tg-cli#553) for the calling session's forum topic before detaching and exports a numeric answer as `TG_TOPIC` into the child (agent-tools#848), so the child's `tg` calls — and those of tools that shell out to `tg`, like the task-cli `[task created]` notifier — post into that topic instead of General. No session, no `tg`, or an unusable answer sets nothing (a `TG_TOPIC` the caller already exports passes through) and the launch is unaffected.

Never `export` these markers by hand in an interactive shell (or an rc file): every
opencode process that inherits them is treated as a dispatched subagent for its whole
session. The launcher sets them for the child process only.

Trust reasoning: a running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the
sanctioned act of dispatching a subagent. This matches the module family's
cooperative-orchestrator threat model (`on_error: open` discipline gates, not security
boundaries).

