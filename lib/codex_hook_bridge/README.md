# codex-hook-bridge - fire `agents-hooks/v1` hooks inside Codex

Codex CLI has native hook events, but it does not read the agent-tools
`agents-hooks/v1` descriptor directory by itself. This bridge is the dispatcher Codex
hook commands call so the same installed descriptors can run under Codex.

## What it does

Codex calls the bridge once per configured hook event:

```
python3 -m codex_hook_bridge PreToolUse
python3 -m codex_hook_bridge PostToolUse
python3 -m codex_hook_bridge Stop
```

The bridge reads Codex's hook JSON from stdin, maps it to a logical v1 point, runs the
matching descriptors from `~/.codex/hooks/*.json`, and translates v1 exit code 10 into
Codex's plain block JSON:

```json
{"decision":"block","reason":"..."}
```

## Mapping

| Codex event/tool | v1 point | Notes |
| --- | --- | --- |
| `PreToolUse` + `Bash` | `pre-bash` | `tool_input.command` becomes `event.command` and `args.command`. |
| `PreToolUse` + `apply_patch` | `pre-write` | `tool_input.command` becomes raw `args.patch` / `args.command`; `args.content` is the added patch content with leading `+` markers stripped; each patch target is exposed to hooks as `args.file_path` / `args.path`. |
| `PostToolUse` + `apply_patch` | `post-write` | Runs path-based hooks once per patch target; carries raw `args.patch` but no synthetic `args.content`; exit 10 is feedback surfaced through Codex's block JSON shape. |
| `Stop` | `stop` | Stable session id from `session_id` / `conversation_id` / detached agent / cwd even when Codex sends a unique `event_id`/`turn_id`; so stop-completion-selfcheck cooldown works (agent-tools#655). Native session wins over a reused launcher `RIG_AGENT_ID`. Does not read the tg-ctl inbox: Codex is tmux-only (tg-cli#456), and a Codex in an omp's cwd would steal the omp's grant notice. |

Multi-file `apply_patch` calls expose `args.file_paths` in the base translated event and
fan out descriptor execution so existing singular-path hooks receive one `args.file_path`
/ `args.path` at a time. That keeps current path-scoped hooks useful without changing the
v1 event contract.

## Subagent identity (`args.agent_id`)

Role rule and primitives: [`lib/agenttools_agent_role`](../agenttools_agent_role/README.md)
(`native_payload_agent`, `launcher_agent_id`; hooks ask `is_subagent`).

| Harness            | Hooks (tool-call time)                                                                                                                                                                                                                                                                             | Bash env of the agent (e.g. `tg`)        |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Codex 0.153.4      | top-level `agent_id`/`agent_type` (optional in PreToolUse/PermissionRequest schema; required in SubagentStart/Stop). Docs: "Subagent hooks use the parent session id" → `session_id` can't tell. A spawned thread's OWN rollout has `session_meta.thread_source=="subagent"` + `parent_thread_id`. | no per-thread env found → explicit label |
| any detached child | launcher env on the child: `RIG_AGENT_ID=<name>` or `RIG_DETACHED_AGENT=1`                                                                                                                                                                                                                         | inherited into bash                      |

The bridge strips `agent_id` / `agent_type` from Codex `tool_input` (model-controlled) and
restores a TRUSTED `args.agent_id` (+ `args.agent_type`) from, in order:

1. Codex's own TOP-LEVEL `agent_id` / `agent_type` of the hook payload, present only when
   the call fires inside a spawned (`spawn_agent` / review / guardian) thread and absent on
   the coordinator thread;
2. only when Codex sent none, the launcher process environment (`RIG_AGENT_ID` /
   `RIG_DETACHED_AGENT=1`, set by the `rig-detached-codex` launcher skill) via
   `launcher_agent_id` — the mechanism `opencode_hook_bridge` introduced (agent-tools#476,
   ported to codex/omp by agent-tools#573).

The top-level fields are schema-verified (extracted from the 0.153.4 binary), NOT yet a live
capture — a live hook capture was blocked by a Codex usage limit; the fixture is
`tests/fixtures/agent-role/codex-0.153.4-hooks.json` and is labelled schema-shaped. A plain
Codex coordinator thread carries neither signal and looks like a main-thread call to
subagent-exempt hooks, as intended.

Every translated v1 event also carries a top-level `harness: "codex"` — a hardcoded module
constant (`dispatch.HARNESS`), never derived from the Codex event, so it can't be forged via
`tool_input`. A hook that needs to scope a policy to (or exempt) one harness reads
`event["harness"]` directly (see `agent-hooks/orchestrator-stays-thin`'s harness-exempt list
for the first consumer, agent-tools#533) instead of trying to reconstruct a trusted subagent
identity from the fields above.

Patch paths are extracted from patch text and are hints, not a trust boundary. Hook authors
that use `args.file_path` / `args.path` for filesystem access must resolve and validate the
path against the intended repository root before reading or writing.

## Not mapped yet: pre-agent

Codex has native `SubagentStart` / `SubagentStop` events, but this bridge does **not** map
them to `pre-agent` yet. The bridge needs a trustworthy captured Codex payload fixture
before it can safely decide which fields represent the dispatch prompt, background mode,
and non-forgeable subagent identity. Until then, `pre-agent` remains a documented gap for
Codex rather than a guessed mapping.

## Descriptor directory

By default the bridge reads:

```
~/.codex/hooks
```

Tests and manual runs can override it:

```
CODEX_HOOKS_DIR=/tmp/hooks python3 -m codex_hook_bridge PreToolUse
```

## Manual hook shape

Current Codex accepts TOML hook entries shaped like:

```toml
[hooks]
PreToolUse = [
  { matcher = "Bash", hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge PreToolUse" }] },
  { matcher = "apply_patch", hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge PreToolUse" }] },
]
PostToolUse = [
  { matcher = "apply_patch", hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge PostToolUse" }] },
]
Stop = [
  { hooks = [{ type = "command", command = "PYTHONPATH=/path/to/agent-tools/lib python3 -m codex_hook_bridge Stop" }] },
]
```

Rig wiring is a separate consumer concern; this package is only the stdlib dispatcher.

## Fail policy

The top-level dispatcher fails open: malformed stdin, a dispatcher bug, or an unreadable
descriptor directory logs to stderr and allows the Codex action. A loaded descriptor still
honors v1 semantics: exit 10 blocks, exit 0 allows, and other hook failures resolve through
the descriptor's `on_error` policy.
