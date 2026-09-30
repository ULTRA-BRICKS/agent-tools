# opencode-hook-bridge - fire `agents-hooks/v1` hooks inside opencode

opencode has a native plugin surface, but it does not read the agent-tools
`agents-hooks/v1` descriptor directory by itself. This bridge is the carrier rig
symlinks into opencode's project plugin directory so the same installed descriptors
can run under opencode.

## What it does

The JavaScript plugin handles:

```
tool.execute.before
tool.execute.after
```

The plugin module exports a single named plugin function, `AgentToolsHookBridge`.
It intentionally does not export a default plugin object; rig provisions this file
as the opencode plugin entrypoint and the bridge tests pin the export shape.

For each event it invokes:

```
python3 -m opencode_hook_bridge <event>
```

The Python dispatcher reads the plugin payload from stdin, maps it to a logical v1
point, runs matching descriptors from `~/.config/opencode/hooks/*.json`, and returns
plain block JSON to the plugin:

```json
{"decision":"block","reason":"..."}
```

The plugin throws `Error(reason)` when it receives a block decision for
`tool.execute.before`, which is the opencode plugin contract for blocking a tool
call before the side effect. For `tool.execute.after`, the write has already
happened, so a block decision is logged as feedback and the plugin fails open.

## Mapping

| opencode plugin event/tool | v1 point | Notes |
| --- | --- | --- |
| `tool.execute.before` + `bash` | `pre-bash` | `output.args.command` becomes `event.command` and `args.command`. |
| `tool.execute.before` + `edit` / `write` | `pre-write` | `output.args.filePath` is normalized to `args.file_path` / `args.path`; proposed content is normalized to `args.content`. |
| `tool.execute.before` + `apply_patch` | `pre-write` | `output.args.patchText` becomes raw `args.patch`; added patch lines become `args.content`; patch marker paths become `args.file_path` / `args.path`. |
| `tool.execute.before` + `task` | `pre-agent` | Carries the task payload (`subagent_type`, `prompt`, `description`) for orchestration guards. opencode's task tool has no `background` field in a default build (foreground; the native field exists only behind `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS`, mapped to `run_in_background` only then); `general`/`explore` pass the background-subagent gate anyway because opencode has no in-tool background path — the truly-detached path is the `rig-detached-opencode` launcher. |
| `tool.execute.before` + `skill` | `pre-skill` | Native opencode `skill` tool (`skill({ name })`). `output.args.name` is copied onto `args.skill` so `skills-marker-writer` records the invocation. A forged `args.skill` is dropped; only a non-empty native `name` sets `args.skill`. Forged `args.session_id` is dropped; a plugin `input.sessionID` is restored as trusted `args.session_id`. |
| `tool.execute.after` + `edit` / `write` / `apply_patch` | `post-write` | Runs path-based hooks after the write; exit 10 is logged as feedback because opencode cannot un-run the completed write. |

opencode has session events such as `session.idle`, but this bridge does not map
them to the v1 `stop` point: opencode documents them as plugin notifications, not
as a pre-stop blocking contract.

For multi-file `apply_patch` calls, the dispatcher fans out one v1 event per file
path. If one patch contains multiple separate blocks for the same file path, the
per-file `args.content` value reflects the last block seen for that path. For move
patches, both the source and destination paths are surfaced for path-based gates;
only the destination receives the added-content payload because the source path is
not being written.

## Subagent identity (identity contract)

The role rule and its primitives live in the shared
[`lib/agenttools_agent_role`](../agenttools_agent_role/README.md) package
(`opencode_child_agent_id`, `launcher_agent_id`; hooks ask `is_subagent`).

| Harness            | Hooks (tool-call time)                                                                                                            | Bash env of the agent (e.g. `tg`) |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| opencode 1.18.30   | subagent = child session: `session.parentID != null` (the bridge plugin resolves it from `input.sessionID` via the plugin client) | same process → explicit label     |
| any detached child | launcher env on the child: `RIG_AGENT_ID=<name>` or `RIG_DETACHED_AGENT=1`                                                        | inherited into bash               |

The bridge strips all opencode tool-argument fields that look like agent identity
(`agent_id`, `agent_type`, camelCase variants, and `agent`) — model-controlled tool args
can never self-exempt a call, so a forged `agent_id` inside `tool_input` is dropped
before anything else runs. It then restores a TRUSTED `args.agent_id` from two sources, in
order:

1. **Child session** (opencode `task` subagent). opencode's `tool.execute.*` payload carries no
   subagent marker, so `plugin.js` asks opencode: `ctx.client.session.get({ path: { id } })` with
   `id = input.sessionID` (the `PluginInput.client` from `@opencode-ai/sdk`, SDK v1 flavor,
   opencode 1.18.x; it returns `{ data: Session, error }` and `Session.parentID` is set for a
   child session, absent for a top-level one). The parent id is cached per session id (parentage
   is fixed at creation; failures are not cached) and sent to `dispatch.py` as the TOP-LEVEL
   payload field `sessionParentID`, which only the plugin writes — never read from
   `output.args`/`input.args`, which the model controls. When it is non-blank the dispatcher sets
   `args.agent_id` to the child's own session id. The lookup **fails open**: a missing client,
   an HTTP error, or a timeout (`OPENCODE_HOOK_BRIDGE_SESSION_LOOKUP_TIMEOUT_MS`, default 5000) resolves to unknown = coordinator, is logged to the debug file, and never blocks the
   tool call.
2. **Launcher env**, only when the session has no parent: the opencode PROCESS ENVIRONMENT,
   read at launch — `RIG_AGENT_ID=<name>` (identity) or `RIG_DETACHED_AGENT=1` (anonymous
   marker) via `launcher_agent_id`. When a marker is present the dispatcher injects
   `args.agent_id`, so every subagent-exempt hook
   (`orchestrator-stays-thin`, `background-subagent-gate`, `no-long-inline-process`,
   `subagent-no-bg-longproc`) treats the session's tool calls as a dispatched subagent's
   instead of the orchestrator's. The markers are set by the canonical detached launcher
   shipped as the `rig-detached-opencode` universal skill (`plugin.js` spawns this
   dispatcher with `{...process.env}`, so the marker set at child launch is visible on
   every tool call).

Trust reasoning: a running orchestrator cannot retroactively mutate its own process
environment — it can only set these vars for a CHILD process, which is exactly the
sanctioned act of dispatching a subagent. This matches the module family's
cooperative-orchestrator threat model (`on_error: open` discipline gates, not security
boundaries). A bare/whitespace `RIG_AGENT_ID` is not a marker; `RIG_DETACHED_AGENT=1`
alone yields the anonymous id `detached`.

Known limitation, by the same threat model: the markers are honored for the WHOLE
session of any opencode process that inherits them. An `export RIG_AGENT_ID=...` left in
an interactive shell (or persisted into a shell rc file) makes the next interactive
opencode session subagent-exempt too. That is a self-inflicted misconfiguration, not a
bypass an adversarial model can reach from inside a governed session (it cannot edit its
own environment; an rc-file write is itself a governed `pre-write`) — never export the
markers by hand, let the launcher set them for the child only.

Every translated v1 event also carries a top-level `harness: "opencode"` — a hardcoded
module constant (`dispatch.HARNESS`), never derived from the opencode event, so it can't be
forged via tool args the way the identity fields above must be actively stripped. A hook
that needs to scope a policy to (or exempt) one harness reads `event["harness"]` directly
(see `agent-hooks/orchestrator-stays-thin`'s harness-exempt list for the first consumer,
agent-tools#533) instead of trying to reconstruct a trusted subagent identity.

## Descriptor directory

By default the bridge reads:

```
~/.config/opencode/hooks
```

Tests and manual runs can override it:

```
OPENCODE_HOOKS_DIR=/tmp/hooks python3 -m opencode_hook_bridge tool.execute.before
```

## Installation (via rig)

Rig provisions two opencode paths when `harness.kind: opencode` and
`harness.hook_bridge.enabled` are active:

- descriptor JSON files in `~/.config/opencode/hooks`
- this plugin symlinked into the repo-local `.opencode/plugins/zz-agent-tools-hook-bridge.js`

The repo-local ordered plugin path is intentional. opencode's documented plugin
source order runs global plugins before project `.opencode/plugins`; a global-only
security bridge could approve tool arguments before a later project plugin mutates
them. Rig therefore installs the bridge as a `zz-` project plugin so it runs after
normal project plugins and observes the final tool arguments before execution.

opencode loads plugins only at startup, so restart opencode after `rig apply`.

## Fail policy

The JS plugin gives the dispatcher a bounded top-level timeout (default 1,000,000 ms,
override with `OPENCODE_HOOK_BRIDGE_TIMEOUT_MS`). The default deliberately exceeds
the longest shipped descriptor budget so it catches a wedged dispatcher without
preempting fail-closed hooks that intentionally wait for approval. If the dispatcher
times out before a tool executes, the plugin blocks because it cannot prove the v1
checks ran. After a write has already landed, timeout is logged as feedback and the
plugin fails open. Malformed stdin, invalid dispatcher JSON, a dispatcher bug, or an
unreadable descriptor directory logs to stderr and allows the opencode action. A
loaded descriptor still honors v1 semantics: exit 10 blocks pre-tool calls, exit 0
allows, and other hook failures resolve through the descriptor's `on_error` policy.
