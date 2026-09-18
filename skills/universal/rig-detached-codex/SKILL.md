---
name: rig-detached-codex
description: 'Use when a codex orchestrator must dispatch a NON-TRIVIAL subagent in the background — Codex has no fork/isolation:"remote"; background-subagent-gate hooks block foreground dispatch. Run rig-detached-codex — it detaches a `codex exec` child so the hook bridge treats its calls as a subagent.'
---

# rig-detached-codex — canonical detached-agent launcher for Codex CLI

The delegation gates (`orchestrator-stays-thin`, `no-long-inline-process`,
`background-subagent-gate`) block the main Codex session's own inline implementation work and
foreground subagent dispatches. Codex CLI's own subagent lifecycle (`SubagentStart`/
`SubagentStop`) exists but this repo's `codex_hook_bridge` does not yet map it to a trustworthy
per-call identity (no captured, confirmed fixture — see `lib/codex_hook_bridge/README.md`'s
"Not mapped yet: pre-agent"). A `spawn_agent`-style native tool, where a Codex build has one, is
the OTHER sanctioned recipe named in the gates' block text; this skill's launcher is the
sanctioned mechanism on any build, native tool or not — a real detached `codex exec` process,
identified to every hook via the same launcher-env marker `rig-detached-opencode` already uses.

## Where the launcher lives (the carrier)

This skill IS the carrier. rig copies the whole skill dir (launcher included) to the canonical
skills dir — `~/.agents/skills/rig-detached-codex/rig-detached-codex` (default `skills_target`,
default-on via `skills.universal.all`) — mirroring `rig-detached-opencode` exactly. `bin/` in
the agent-tools repo is NOT a rig-discovered carrier, so the launcher must not be referenced
there: after `rig apply` on a provisioned machine only the skill-copy path exists. Gate REMINDER
texts name the provisioned path.

## Usage

```bash
~/.agents/skills/rig-detached-codex/rig-detached-codex <name> <brief-file> [workdir]
```

- `<name>` — agent id (letters, digits, `.`, `_`, `-`; becomes `RIG_AGENT_ID`).
- `<brief-file>` — UTF-8 markdown brief: the mission, acceptance criteria, and the HANDOFF FILE
  PATH the child must write before exiting. May be relative to the caller's cwd — the launcher
  reads its content BEFORE changing directories, so a relative brief with a different `[workdir]`
  still reaches the child in full.
- `[workdir]` — where the child runs (default: the caller's cwd).

The launcher returns immediately (the child is `nohup`-detached); the child's stdout/stderr
append to `/tmp/agent-logs/<name>.log`. Collect the result by polling the handoff file named in
the brief — the launcher keeps no registry by design.

The child runs as `codex exec --sandbox workspace-write --skip-git-repo-check -C <workdir> --
<brief>` — workspace-write so the dispatched agent can actually make the edits/commands its
brief asks for (a read-only sandbox would silently fail every write), and
`--skip-git-repo-check` so a throwaway/scratch `[workdir]` that isn't (yet) a git repo doesn't
abort the launch.

## Why the process env is the identity source

The launcher exports `RIG_AGENT_ID=<name>` and `RIG_DETACHED_AGENT=1` into the child `codex`
process. Codex's hook commands inherit that process environment, so `codex_hook_bridge`
(`lib/codex_hook_bridge/dispatch.py`) sees the markers on every hook invocation for that
session. The bridge strips every model/tool-supplied `agent_id`-shaped key (forged args can
never self-exempt) and injects `args.agent_id` from these markers instead
(`_detached_agent_id`, agent-tools#573, ported unchanged from `opencode_hook_bridge`'s
agent-tools#476), so every subagent-exempt delegation gate (`orchestrator-stays-thin`,
`background-subagent-gate`, `no-long-inline-process`, `subagent-no-bg-longproc`) classifies the
child session's tool calls as a dispatched subagent's.

Never `export` these markers by hand in an interactive shell (or an rc file): every codex
process that inherits them is treated as a dispatched subagent for its whole session. The
launcher sets them for the child process only.

Trust reasoning: a running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the sanctioned
act of dispatching a subagent. This matches the module family's cooperative-orchestrator threat
model (`on_error: open` discipline gates, not security boundaries).

## Known Codex stdin-hang workaround

`codex exec "<prompt>"` hangs indefinitely if stdin is a non-TTY pipe with no writer, even
though the prompt was supplied as a positional argument (openai/codex#20919). The launcher
redirects the child's stdin from `/dev/null` for exactly this reason — the same fix
`rig-detached-opencode` applies to `opencode run`.
