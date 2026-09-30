---
name: rig-detached-claude-code
description: 'Use when a Claude Code session or a script/daemon must start a NON-TRIVIAL Claude Code agent that outlives the caller (the Agent tool''s fork/isolation:"remote" background shapes cannot). Run rig-detached-claude-code — it detaches a `claude -p` child carrying RIG_AGENT_ID so the hook bridge treats its calls as a subagent.'
---

# rig-detached-claude-code — canonical detached-agent launcher for Claude Code

Claude Code's own `Agent` tool already has background shapes — `subagent_type: "fork"` and
`isolation: "remote"` — and `background-subagent-gate` allows them as-is: for MOST subagent
dispatches from inside a Claude Code session they are the sanctioned background mechanism and
need no launcher at all. This skill's launcher exists for the case they cannot cover: a TRULY
detached process that keeps running after the calling session ends, or Claude Code work started
outside any Claude Code session in the first place (rig's sysmon auto-remediation daemon, a
script, another harness). It carries the same launcher-env identity marker
`rig-detached-opencode`, `rig-detached-codex` and `rig-detached-omp` use, so every hook still
classifies its tool calls as a subagent's.

## Where the launcher lives (the carrier)

This skill IS the carrier. rig copies the whole skill dir (launcher included) to the canonical
skills dir — `~/.agents/skills/rig-detached-claude-code/rig-detached-claude-code` (default
`skills_target`, default-on via `skills.universal.all`) — mirroring `rig-detached-opencode`
exactly. `bin/` in the agent-tools repo is NOT a rig-discovered carrier, so the launcher must not
be referenced there: after `rig apply` on a provisioned machine only the skill-copy path exists.
Gate REMINDER texts name the provisioned path.

## Usage

```bash
~/.agents/skills/rig-detached-claude-code/rig-detached-claude-code <name> <brief-file> [workdir]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the HANDOFF FILE
  PATH the child must write before exiting. May be relative to the caller's cwd — the launcher
  reads its content BEFORE changing directories, so a relative brief with a different `[workdir]`
  still reaches the child in full.
- `[workdir]` — where the child runs (default: the caller's cwd; a relative path is resolved
  against the caller's cwd and reported absolute).

The launcher returns immediately (the child is `nohup`-detached in its own session); the child's
stdout/stderr append to `/tmp/agent-logs/<name>.log` (`$RIG_AGENT_LOG_DIR` overrides; the dir is
0700 and owner-checked, the log 0600, a symlink at the log path is refused). Collect the result by
polling the handoff file named in the brief — the launcher keeps no registry by design. Exit
codes: `2` bad usage/name/brief/workdir/log path, `127` no `claude` on `PATH`, `0` launched (the
last stdout line is `detached agent '<name>' launched (pid …, log …, workdir …)`).

The child runs as `claude -p --permission-mode bypassPermissions -- <brief>` in `<workdir>`:
`-p`/`--print` for non-interactive print-and-exit, `bypassPermissions` because a detached
background process has no human present to approve tool calls (agent-hooks still run — they are
hooks, not permission prompts), `--` so a brief that starts with `---` or `- ` is never parsed as
an option, and stdin from `/dev/null` so the child neither blocks on nor swallows the caller's
pipe. Claude Code has no `--cwd` flag; the launcher `cd`s into `<workdir>` before starting it.

## Why the process env is the identity source

The launcher exports `RIG_AGENT_ID=<name>` and `RIG_DETACHED_AGENT=1` into the child `claude`
process. Claude Code runs its hook commands with its own process environment, so the markers are
visible to `lib/cc_hook_bridge/dispatch.py` on every hook invocation in that session. The bridge
keeps Claude Code's own top-level `agent_id` when it sends one (a native subagent inside the
detached session), drops every model/tool-supplied `agent_id` copy (forged args can never
self-exempt), and otherwise injects `args.agent_id` from these markers (`agenttools_agent_role.launcher_agent_id`,
agent-tools#831 — the same mechanism the codex/omp/opencode bridges use), so every
subagent-exempt delegation gate (`orchestrator-stays-thin`, `background-subagent-gate`,
`no-long-inline-process`) and every subagent-only gate (`subagent-no-bg-longproc`,
`subagent-no-monitor`) classifies the child session's tool calls as a dispatched subagent's.

Never `export` these markers by hand in an interactive shell (or an rc file): every `claude`
process that inherits them is treated as a dispatched subagent for its whole session. The
launcher sets them for the child process only.

Trust reasoning: a running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the sanctioned
act of dispatching a subagent. This matches the module family's cooperative-orchestrator threat
model (`on_error: open` discipline gates, not security boundaries).
