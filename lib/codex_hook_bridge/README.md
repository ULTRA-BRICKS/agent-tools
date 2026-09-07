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
| `Stop` | `stop` | Carries the session id when Codex provides one. |

Multi-file `apply_patch` calls expose `args.file_paths` in the base translated event and
fan out descriptor execution so existing singular-path hooks receive one `args.file_path`
/ `args.path` at a time. That keeps current path-scoped hooks useful without changing the
v1 event contract.

## Identity contract (subagent exemption, agent-tools#573)

The bridge strips `agent_id` / `agent_type` / `transcript_path` from Codex `tool_input`
(model-authored, never trusted) and repopulates `args.agent_id` / `args.agent_type` from four
trusted sources, in order (`_apply_subagent_identity`):

1. **Codex's own top-level `agent_id` / `agent_type`.** Every hook event fired inside a
   `collaboration.spawn_agent` child thread carries them at the TOP level of the hook JSON
   (captured verbatim on codex 0.153.4 — `tests/test_codex_hook_bridge.py` keeps the fixture;
   `session_id` stays the parent's, as documented, and `transcript_path` points at the child's
   rollout). The parent thread's events carry neither. Codex writes these from its thread
   bookkeeping; they are not part of the tool call the model composed, so nothing in
   `tool_input` can set or change them — the same T2 precedence `cc_hook_bridge` applies.
2. **The child thread's rollout `session_meta`** behind the event's top-level
   `transcript_path` (`_rollout_subagent_identity`). Codex writes one rollout per thread under
   `$CODEX_HOME/sessions/`; a spawned thread's first line carries `thread_source: "subagent"`,
   `parent_thread_id` (the spawner) and `id` (the child thread — also the filename's trailing
   uuid), while the event's `session_id` is the PARENT's. This is the record the 2026-09-05
   `~/work/private` audit found on every thread the old bridge had refused as "the
   orchestrator" (sessions `16-13`/`16-13b`/`16-14`, codex 0.153.4), so it identifies a
   delegate whether or not the codex build also emits source 1. `transcript_path` is a field of
   the hook event root, written by codex — the bridge never reads it from `tool_input`. The
   record is trusted only when it is CONSISTENT with facts codex owns: filename uuid ==
   `payload.id`, `payload.parent_thread_id` == event `session_id`, `payload.id` != event
   `session_id`. An orchestrator's own rollout is named after its own session id (== the
   event's `session_id`), so editing its first line cannot satisfy all three at once, and
   renaming the open file leaves `transcript_path` dangling → no identity → governed. The read
   is the first line only, capped at 256 KB (a real `session_meta` line is ~20 KB with the full
   `base_instructions` text); any IO/JSON/shape failure → no identity.
   `tests/test_codex_subagent_replay.py` replays the audit's real refused tool calls against
   the real `session_meta` records through this bridge and the real hooks.
   *Ruled out:* an env-var source. The hook process's environment carries only `CODEX_HOME` /
   `CODEX_MANAGED_*` (captured on 0.153.4); `CODEX_THREAD_ID` / `CODEX_SESSION_ID` exist in the
   binary but are exported to the shell tool's child, not to hook commands.
3. **Launcher env markers** `RIG_AGENT_ID` / `RIG_DETACHED_AGENT` — a `codex exec` child
   started by the `rig-detached-codex` launcher skill (Codex runs hook commands with its own
   environment, so the marker set at launch is visible on every tool call).
4. **Process ancestry** — a `codex` process above the one that dispatched this hook, past the
   dispatching invocation's own contiguous run (a codex invocation is two processes, the
   `node` wrapper and the vendor binary): a child `codex exec` started from a parent session's
   shell tool without the launcher.

Sources 3 and 4 live in the shared `lib/agent_hooks_v1/subagent_identity.py` (read by the
opencode and omp bridges too); its docstring carries the trust reasoning. No source → no key
at all, so every `_is_subagent` reader treats the call as the orchestrator's (fail closed in
the relax direction).

Every translated v1 event also carries a top-level `harness: "codex"` — a hardcoded module
constant (`dispatch.HARNESS`), never derived from the Codex event, so it can't be forged via
`tool_input`. Hooks read it to name THIS harness's delegation recipe in their refusal text
(`agenttools_hatch_escalation.delegation_recipe`), never to exempt the harness — the #533/#544
`EXEMPT_HARNESSES` shortcut is gone.

**`codex exec` runs no hooks without `--dangerously-bypass-hook-trust`** (verified on
0.153.4: no PreToolUse/Stop hook fires in exec mode, even in a trusted project, until that
flag is passed). The `rig-detached-codex` launcher passes it so the child stays governed; a
hand-run `codex exec` without it is simply ungoverned.

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
