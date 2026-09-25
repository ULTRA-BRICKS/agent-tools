# omp-hook-bridge - fire `agents-hooks/v1` hooks inside omp (oh-my-pi)

omp has a genuine, documented extension/hook API (Bun-executed TS/JS modules that call
`pi.on(eventName, handler)`), but it does not read the agent-tools `agents-hooks/v1`
descriptor directory by itself. This bridge is the carrier rig symlinks into omp's
extension directory so the same installed descriptors can run under omp.

## What it does

The TypeScript extension (`extension.ts`) handles:

```
tool_call    (pre-execution)
tool_result  (post-execution)
```

It default-exports a single factory function, `(pi) => { pi.on("tool_call", ...); pi.on("tool_result", ...); }`
— the shape omp's extension loader expects (docs/hooks.md / docs/extension-loading.md).

For each event it invokes, and **awaits**:

```
python3 -m omp_hook_bridge <event>
```

The spawn is asynchronous (`node:child_process`'s `spawn`, awaited to the child's `close`),
never synchronous. omp is single-threaded, so a synchronous spawn froze the whole interface —
no repaints, no keypresses — for the entire dispatch, up to the full Telegram-approval window
of a hatch-capable descriptor (agent-tools#558). omp's `emitToolCall` awaits whatever the
handler returns, so the decision still arrives before the tool runs. The dispatcher's stderr is
streamed through as it arrives instead of being buffered to the end, so a hook that explains
itself while it waits is visible while it is still waiting; every forwarded line is tagged
`omp-hook-bridge[<event> <toolCallId>]`, because dispatches can now overlap (two tool calls in
flight at once, each with its own child, buffer and timers) and two hatches both explaining a
wait must stay attributable.

Because omp stays usable during a dispatch, it can now also **exit** during one (Ctrl-C, `/exit`,
a crash) — something the frozen loop made impossible. The extension keeps every running
dispatcher in an in-flight registry (shared across reloads of the module) and stops them on
omp's `session_shutdown` — SIGTERM, then SIGKILL after 1 s, inside omp's 2 s shutdown-handler
cap — and, for a crash or signal that skips it, on the process `exit` event (which cannot wait:
SIGTERM, or SIGKILL for a child already asked to stop). Each dispatcher is spawned as its own
process-group leader and signalled as a group, so the hook scripts it forks go with it. Out of
reach by design: a descendant that started a new session — the hatch's `tg-ctl ask` does — which
the dispatcher's own SIGTERM cleanup must end; SIGTERM therefore always comes first, on every
path (budget timeout, shutdown, exit).

Output is bounded: a dispatcher that writes more than 1 MiB to stdout is stopped and the call
resolves through the fail-open path; a stderr line that never ends is flushed in 64 KiB pieces.
The old `spawnSync` had a bounded buffer for free; an awaited stream needs these caps explicitly.

The Python dispatcher reads the extension's payload from stdin, maps it to a logical v1
point, runs matching descriptors from `~/.omp/agent/hooks/*.json`, and returns plain block
JSON to the extension:

```json
{"decision":"block","reason":"..."}
```

## Mapping

| omp event + toolName | v1 point | Notes |
| --- | --- | --- |
| `tool_call` + `bash` | `pre-bash` | `input.command` becomes `event.command` and `args.command`. |
| `tool_call` + `hub` | `pre-bash` | **(agent-tools#614)** omp's `hub` tool has no `command` field (its schema is `{op, application?, args?, name?, cwd?, env?, ready?, ...}`, inferred from the tool's own JSON-Schema description since no `docs/tools/hub.md` was found — same "no docs consulted" caveat as `notebook`/`apply_patch` below). Only `op: "start"` (launching `application` + `args` as a new background process) synthesizes an argv string via `shlex.join` onto `event.command`/`args.command`; every other op (`send`/`wait`/`inbox`/`list`/`jobs`/`cancel`/`ps`/`logs`/`stop`/`restart`/`describe`) manipulates an existing hub-managed process and synthesizes no command — see `_hub_command`. A `hub start` call's own `input.cwd` (defaulting to the session directory per `hub`'s own schema) overrides the session `cwd` on the synthesized v1 event — see `_cwd` — so a cwd-sensitive pre-bash hook judges the directory the process actually launches in, not wherever the session happens to be; a relative `input.cwd` resolves against the session cwd. |
| `tool_call` + `edit` | `pre-write` | `input.input` (the hashline patch text) is parsed for every `[PATH#TAG]` section path → `args.file_path`/`path`/`file_paths`; a best-effort reconstruction of added body rows becomes `args.content` (same-path sections accumulate). No section found → the `replace`/`patch` fallback below. |
| `tool_call` + `write` | `pre-write` | `input.path` / `input.content` normalize to `args.file_path`/`path`/`content` directly — same shape family as opencode's `write` tool. |
| `tool_call` + `eval` | `pre-write` (+ `pre-bash` when literal shell/git commands are scraped from `input.code`; agent-tools#692) | **(agent-tools#614)** omp's `eval` tool has no `path`/`content` field — the whole program lives in `input.code` (observed alternate key: `content`), and `command` is FORCED to `""` regardless of any raw `command` key (a forged one could otherwise steer `block-secrets-write`'s `event.command` path fallback). `_eval_content_buckets` heuristically scrapes every write call by text-matching LITERAL arguments (single/double/triple-quoted OR backtick, escape-decoded) to `Path(<str>).write_text(<str>)`/`Path(<str>).write_bytes(<str>)` (Python), `Bun.write(<str>, <str>)` / `(fs.)?(write\|append)File(Sync)?(<str>, <str>)` (JS), `open(<str>, ...).write(<str>)` (Python), and a path-less `.write(<str>)`/`.write_text(<str>)` fallback (excluding a `Bun`/`fs` receiver, to avoid swapping a two-argument call's PATH in as content when its own second argument fails to parse) — aggregating EVERY match (not just …
| `tool_call` + `apply_patch` | `pre-write` | The wire name omp uses for `edit` in apply_patch custom-tool mode (docs/tools/edit.md); the payload is the classic `*** Update File:` / `*** Add File:` / `*** Delete File:` / `*** Move to:` envelope, not hashline. Parsed with logic ported from the opencode bridge (see below). |
| `tool_call` + `notebook` | `pre-write` | omp has no confirmed public tool schema for a dedicated notebook tool in the docs consulted while building this bridge. Best-effort passthrough only: whichever of `path`/`notebook_path`/`file_path` is present in `input` is normalized onto `args.notebook_path`/`file_path`/`path`. Kept as a mapped point (not dropped) on the assumption that IF omp ships one, its shape is path-like enough for this passthrough to still work; revisit once the real schema is confirmed. |
| `tool_call` + `task` | `pre-agent` | omp's task item shape is `{name?, agent?, task, effort?, outputSchema?, schemaMode?, isolated?}` (flat) or `{context, tasks: item[]}` (batch, the default). Normalized onto the shared pre-agent contract the `background-subagent-gate` reads: `agent` → `args.subagent_type`, `task` → `args.prompt` (a batch joins every item's `task` with newlines), and `args.run_in_background: true` — see "omp `task` dispatches are background by contract" below. Raw `task`/`name`/`tasks`/`context` fields are kept alongside. |
| `tool_call` + `yield` | `stop` | The hidden completion tool a helper must call (docs/tools/task.md). `tool_call` can return `{block: true, reason}` (docs/hooks.md); this is the omp Stop equivalent so `stop-completion-selfcheck` can block a premature yield, **and** so the tg-ctl Stop-hook inbox can deliver without tmux (agent-tools#656). The inbox key is the cwd hash (omp has no `--name`). |
| `tool_result` + `edit`/`write`/`apply_patch`/`eval` | `post-write` | Runs path-based hooks after the write; a block decision is logged only because omp cannot un-run a completed write. |

omp's session-lifecycle events (`session_shutdown`, `agent_end`, etc.) have no documented
blocking return contract in docs/hooks.md — this bridge does not map any of them to the v1
`stop` point, same reasoning the opencode bridge's README gives for `session.idle`.
The hidden `yield` tool is a `tool_call`, so it *does* map to `stop` (agent-tools#640) and
consumes the tg-ctl inbox on that path (agent-tools#656). Delivery is at the next yield,
not while idle — live Telegram→idle omp still needs ACP. A helper in the same cwd shares
the cwd-hash inbox with its parent. pi has no equivalent session-end block, so a pi
session outside tmux stays unreachable without ACP.

For a multi-section `edit` call or a multi-file `apply_patch` call, the dispatcher fans out
one v1 event per file path, mirroring `opencode_hook_bridge`'s `_v1_events_for_dispatch`.

The bridge strips all omp tool-argument fields that look like agent identity (`agent_id`,
`agent_type`, camelCase variants, and `agent`) before forwarding args — the same
`_FORGED_AGENT_KEYS` treatment as the codex/opencode bridges, because omp exposes no
TRUSTED per-tool-call subagent identity in the extension event either (confirmed absent
from the documented `tool_call`/`tool_result` shape). Every translated v1 event also
carries a top-level `harness: "omp"` — a hardcoded module constant (`dispatch.HARNESS`),
never derived from the omp event, so it can't be forged the way the identity fields above
must be actively stripped. The bridge restores a TRUSTED `args.agent_id` from, in order: `RIG_AGENT_ID` /
`RIG_DETACHED_AGENT` (rig-detached-omp, agent-tools#573); a nested transcript path
(`.../<session-id>/<agent>.jsonl` → stem) from **payload `sessionFile`** or process env
`PI_SESSION_FILE` (agent-tools#634); a synthetic id from **payload `isMainSession`**
(live finding, 2026-09-18, see below); then payload `agentId`. omp's `ExtensionContext`
exposes NO top-level `getSessionFile()`/`getAgentId()`/`agentId` (oh-my-pi#11214), so
`extension.ts` sources the session file from `ctx.sessionManager.getSessionFile()`
(agent-tools#743) — for a native Task/eval child that is the CHILD's own session manager
and returns the nested transcript. The Node hook process inherits the **parent**
session env, so env `PI_SESSION_FILE` alone is the main session file — the nested
child path has to be forwarded. The main session file is not a child and does
not inject. omp's `tool_call` payload still has no per-call spawn marker of its
own; identity comes from ctx session state and launcher env.

**`isMainSession` (live finding, 2026-09-18)**: a nested child's own session can still be
in-memory — not yet persisted to disk — for its first few tool calls (`SessionManager`
mints `getSessionId()` synchronously at creation, but `getSessionFile()` stays undefined
until the session is actually written). With no session file at all, the path-shape check
above has nothing to walk, and the child was silently classified as the orchestrator —
misfiring `orchestrator-stays-thin`'s warn-then-block tier on its own first couple of
pre-write/pre-bash calls despite being a genuine subagent throughout. `extension.ts` latches
the FIRST `getSessionId()` it ever sees in this process (on `globalThis`, reload-safe like
the in-flight dispatcher registry above) as the root/orchestrator session — sound because a
child cannot exist before its own parent's `task` tool_call has already fired through this
same handler — and forwards `isMainSession: sessionId === thatRoot` on the payload whenever
both are known. `dispatch.py` consults it ONLY when the file-based nested check above found
nothing: `isMainSession === false` synthesizes `args.agent_id = "omp-nested:<sessionId>"`
(a marker, not a real transcript stem) so `_is_subagent` still exempts the call; a positive
file-based result is never second-guessed, and an event with no `isMainSession` key at all
(an older bridge build, or an existing test fixture) falls through unchanged.

## Hashline content extraction is an approximation

omp's default `edit` mode is `hashline`, not old/new-string pairs or a unified diff
(docs/tools/edit.md). The consumer hooks this bridge exists for
(`worktree-only-writes`, `orchestrator-stays-thin`) only need the touched file **paths**
for pre-write, which `_hashline_file_paths` extracts reliably from every `[PATH#TAG]`
section header. Reconstructing final file **content** from a hashline payload is a
nice-to-have this bridge attempts (`_hashline_added_content` / `_hashline_content_by_path`)
by concatenating the literal `+`-prefixed body rows that follow a body-bearing
`PUT ...:` operation header. This is explicitly NOT a faithful hashline interpreter:

- `CUT`, `REM`, `MV DEST`, and register-backed `PUT <N @name` / `PUT >N @name` /
  `PUT N.=M @name` operations carry no body rows at all, so content moved via a named
  register (cut in one section, pasted in another) is invisible to this extraction.
- Multiple `PUT ...:` operations touching the same section are concatenated in
  document order, not merged the way hashline actually applies them against original
  line numbers.
- `MV DEST` (rename) only surfaces the SOURCE section's `[PATH#TAG]` path, not `DEST` —
  unlike the classic `apply_patch` envelope below, where `*** Move to:` gives this bridge
  an explicit destination line to parse. Hashline's `MV DEST` destination is a bare token
  on the operation line with no header-level marker, so it is deliberately left unparsed
  rather than guessed at; a path-based hook sees the file being moved FROM, not TO.

This mirrors the same honesty the opencode bridge's README already states for its own
`apply_patch` added-content approximation — good enough for a path-based gate, not a
patch applier.

## Non-hashline `edit` modes (`replace` / `patch`)

omp's `resolveEditMode()` can select `replace` or `patch` instead of `hashline` (per model,
via `PI_EDIT_VARIANT`, or the model exclusion list — docs/tools/edit.md). Those payloads
carry no `[PATH#TAG]` section, so when the hashline parser finds none the dispatcher falls
back to a `write`-like normalization: the path from the `path`/`filePath`/`file_path` family
(or the `+++ b/<path>` header of a unified diff), and `content` from the replacement-text
family (`newText`/`new_text`/`newString`/`new_string`/`replacement`/`text`/`content`) plus
the added rows of a unified diff (`patch`/`diff`). The exact wire field names of those two
modes are NOT pinned by the docs consulted, so this is the same best-effort key family the
opencode bridge accepts — without it a secret written through a replace-mode edit reached
`block-secrets-write` as `content == ""` and was allowed.

Derived values always OVERWRITE same-named keys that arrived in the raw tool args: the parsed
patch is the truth about what gets written, and a stray or forged `content: ""` / `file_path`
must not shadow it. For a multi-path edit with no single target the raw `file_path`/`path`
keys are dropped so a forged single path cannot shadow the per-path fan-out.

## omp `task` dispatches are background by contract

The only shipped `pre-agent` consumer, `agent-hooks/background-subagent-gate`, reads the
shared keys `prompt`/`description` (triviality) and `subagent_type`/`isolation`/
`run_in_background` (shape). omp's `task` tool has none of them: its item is
`{name?, agent?, task, ...}`, and — crucially — omp exposes **no per-call background lever**.
By omp's own contract (docs/tools/task.md) a non-blocking spawn becomes an `AsyncJobManager`
background job; a call runs synchronously only when the SESSION setting `async.enabled` is
false or the agent's frontmatter declares `blocking: true` — neither is a tool argument the
model could ever set. So an unnormalized omp dispatch was judged non-trivial AND foreground,
and the gate blocked every one with a remediation ("dispatch it in the background") that
cannot be followed from a tool call.

The dispatcher therefore normalizes `task` → `prompt` (so a genuinely trivial one-liner is
still allowed as trivial, and a multi-item batch is judged non-trivial via its newlines) and
sets `run_in_background: true`, trusting omp's documented async-job contract exactly the way
the gate's own docstring trusts CC's `fork`/`isolation: "remote"` contract. This is deliberate
and honest about its limit: the bridge cannot see the session's `async.enabled` value, so a
session that turned it off is treated as background anyway. That is acceptable for an
`on_error: open` orchestration-discipline gate (the same acceptability argument the gate itself
makes), and strictly better than hard-blocking every omp subagent dispatch. An explicit
`run_in_background`/`prompt` already present in the args is never overwritten.

## `apply_patch` field name and the ported parser

omp's own docs describe the `apply_patch` wire name only as "the wire name is
`apply_patch`" in custom-tool mode, without pinning the exact input field name in the
excerpt available at authoring time. The dispatcher tries `patch`, then `patchText`,
then `input`, in that order, and uses whichever is a string. Once found, that text is
parsed as the CLASSIC `*** Update File: ` / `*** Add File: ` / `*** Delete File: ` /
`*** Move to: ` envelope — the same family opencode's own `apply_patch` tool uses — with
logic PORTED (copied, not imported) from `lib/opencode_hook_bridge/dispatch.py`'s
`_patch_file_paths` / `_patch_added_content` / `_patch_added_content_by_path` /
`_patch_move_target`. It is a deliberate copy rather than a cross-package import: each
harness bridge in this repo is self-contained (no bridge imports another today), so a
future change to one envelope parser does not silently ripple into an unrelated harness's
bridge. If the classic envelope format ever changes, update both copies.

## Descriptor directory

By default the bridge reads:

```
~/.omp/agent/hooks
```

Resolution honors omp's own environment overrides, in this precedence order:

1. `OMP_HOOKS_DIR` — bridge-only override, for tests and manual runs.
2. `OMP_CODING_AGENT_DIR`, then the legacy `PI_CODING_AGENT_DIR` — a full override of omp's
   agent directory (omp profiles re-point the agent dir through it); `hooks` is appended to
   whatever it resolves to. The OMP-prefixed name is checked first, matching this repo's own
   omp resolver in `lib/checker/model_freshness.py`.
3. `PI_CONFIG_DIR` — renames the `.omp` config-root dirname under home; the agent dir
   becomes `~/<PI_CONFIG_DIR>/agent`, then `hooks` is appended.
4. The default, `~/.omp/agent/hooks`.

Every override is expanded `$VAR`-then-`~`; a value that expands to a relative path is
home-anchored AFTER expansion (never the raw string, which would leave a literal `$VAR`
segment). omp `--profile <name>` sessions live under `~/.omp/profiles/<name>/agent`; the
bridge follows them only through the env overrides above (omp re-points the agent dir via
`PI_CODING_AGENT_DIR` for a profile) — it does not read omp's profile config itself.

This mirrors rig-cli's `riglib.harness_skills.omp_agent_root` precedence exactly (ported,
not imported, since this dispatcher cannot depend on rig-cli). Tests and manual runs can
also override directly:

```
OMP_HOOKS_DIR=/tmp/hooks python3 -m omp_hook_bridge tool_call
```

## Installation (via rig)

Rig provisions two omp paths when `harness.kind: omp` and `harness.hook_bridge.enabled`
are active:

- descriptor JSON files in `~/.omp/agent/hooks`
- this extension symlinked into omp's extension directory (default
  `~/.omp/agent/extensions`, honoring the same `PI_CODING_AGENT_DIR` / `PI_CONFIG_DIR`
  overrides as the descriptor directory above)

omp's extension loader re-imports a changed extension file using an `?mtime=<n>`
cache-buster (docs/extension-loading.md), so an edited/re-provisioned bridge is picked up
without restarting omp — unlike opencode, which only loads plugins at startup.

## Fail policy — the ONE deliberate divergence from codex/opencode

**`tool_call` bridge-infrastructure failures fail OPEN here, unlike opencode's `plugin.js`,
which throws to fail CLOSED on the same class of failure.** Per docs/hooks.md, a thrown
error from a `tool_call` handler blocks the tool call — the identical mechanism opencode's
plugin deliberately uses to fail closed on a wedged dispatcher. omp is Alex's interactive
daily-driver CLI, so a broken dispatcher process (missing `python3`, a bug in this bridge,
an unreachable descriptor directory, a timeout) must never silently brick every
bash/edit/write/task call in a live session by throwing. `extension.ts` therefore NEVER
throws from its `tool_call` handler for a bridge failure — it always returns `undefined`
(omp's "no opinion, allow" result) and logs to stderr instead. Only an EXPLICIT
`{"decision":"block","reason":...}` on the dispatcher's stdout becomes a real
`{block: true, reason}` return. `tool_result` (post-execution) fails open the same way in
every bridge, including this one, because the write has already landed by the time that
event fires.

**Because a bridge failure here means "allow", the dispatcher timeout must be set well
ABOVE the longest fail-closed descriptor's own budget, not below it — the opposite
trade-off from opencode.** In opencode a short timeout degrades to a block (safe-ish); here
it degrades to an allow, silently converting an `on_error: closed` hook (a Telegram-approval
hatch like `block-raw-pr-merge` or `worktree-only-writes`, budgeted at 960000 ms for tg-ctl's
own approval window) into "no opinion" the moment the extension's own timeout fires first.
The timeout bounds the WHOLE dispatch (every matching descriptor, run serially, for every
fanned-out file path), not one descriptor. `extension.ts`'s `DEFAULT_DISPATCHER_TIMEOUT_MS`
is therefore `2000000` ms — two full 960,000 ms approval windows plus margin, so two
hatch-capable hooks (say `worktree-only-writes` and `orchestrator-stays-thin` on one
pre-write) can each wait for a human in a single call without the extension's own timeout
converting the second, still-running fail-closed hook into an allow. A hatch wait only
happens when the agent explicitly set a `RIG_HATCH_REQUEST_*` variable, so even two in one
call is the rare case; three or more serial approval waits in ONE tool call remain a
documented residual of the fail-open design. Overridable via `OMP_HOOK_BRIDGE_TIMEOUT_MS`.
Re-verify this default against `agent-hooks/*/*.json` if a future descriptor ships a larger
`timeout_ms`.

### omp's own handler timeout is the real ceiling — raise it

omp bounds a `tool_call` handler independently of this bridge, with the setting
`extensionHandlers.toolCallTimeoutMs` (**default 30,000 ms**). When it fires, omp resolves the
tool call to `{block: true, reason: "Extension <path> timed out after 30000ms"}` — it fails
**CLOSED**, the opposite direction from this bridge, and the dispatcher child is left running.

The old synchronous spawn evaded that timeout by accident: a blocked event loop cannot run
omp's own timer, so the handler always won the race. The awaited spawn does not evade it. So
the budgets must be ordered:

```
longest descriptor timeout_ms (960,000)  <  OMP_HOOK_BRIDGE_TIMEOUT_MS (2,000,000)  <  extensionHandlers.toolCallTimeoutMs
```

Set the last one in `~/.omp/agent/config.yml`
([rig-cli#363](https://git.hyperide.ai/ultrabricks/rig-cli/issues/363) tracks provisioning it):

```yaml
extensionHandlers:
  toolCallTimeoutMs: 2100000
```

**Without that setting, any hook that holds a call longer than 30 s — i.e. every Telegram
approval hatch — ends as an omp-generated block naming this extension's path, not as the
hook's own decision.** That is the safe direction (a fail-closed hook still does not become an
⚠ 1 unresolved conflict detected
- ours = HEAD
- theirs = 0db8c6a (fix(omp-hook-bridge): map eval/hub tool calls to pre-write/pre-bash hook points)
- base = parent of 0db8c6a (fix(omp-hook-bridge): map eval/hub tool calls to pre-write/pre-bash hook points)
NOTICE: Inspect a block by reading `conflict://<N>` (add `/ours` / `/theirs` / `/base` to render a single side). Resolve with `write({ path: "conflict://<N>", content })`, or bulk-resolve every registered conflict with `write({ path: "conflict://*", content })`. Writes replace ONLY the marker block (markers + all sides) — never repeat the lines before/after it; they stay in place.
`content` shorthand: a line that is exactly `@ours` / `@theirs` / `@base` / `@both` expands to that recorded section. `@both` is ours-then-theirs with no separator — only for additive conflicts where each side adds something different; NEVER for competing edits of the same lines (pick a side or write the combined text). Lines that are not a token pass through verbatim, so `"// keep both\n@ours\n@theirs"` literally writes the comment, then ours, then theirs.
Per-id bulk: `write({ path: "conflict://*", content: "1: @ours\n2: @theirs\n…" })` resolves each listed id with that side in ONE call — the cheapest way through many pick-one conflicts; unlisted ids stay registered.
Resolve each block faithfully: keep one side (`@ours`/`@theirs`), or combine them when both intents apply — never invent content beyond the recorded sides, and never stack both sides of competing edits. Resolve several conflicts in a single turn by issuing multiple `write` calls at once; ids stay valid as earlier blocks are resolved.

──── #1  L72-79 ────
<<< ours
| `tool_call` + `yield` | `stop` | The hidden completion tool a helper must call (docs/tools/task.md). `tool_call` can return `{block: true, reason}` (docs/hooks.md); this is the omp Stop equivalent so `stop-completion-selfcheck` can block a premature yield, **and** so the tg-ctl Stop-hook inbox can deliver without tmux (agent-tools#656). The inbox key is the cwd hash (omp has no `--name`). |
| `tool_result` + `edit`/`write`/`apply_patch` | `post-write` | Runs path-based hooks after the write; a block decision is logged only because omp cannot un-run a completed write. |
=== base
| `tool_result` + `edit`/`write`/`apply_patch` | `post-write` | Runs path-based hooks after the write; a block decision is logged only because omp cannot un-run a completed write. |
>>> theirs
| `tool_result` + `edit`/`write`/`apply_patch`/`eval` | `post-write` | Runs path-based hooks after the write; a block decision is logged only because omp cannot un-run a completed write. |

[Showing lines 1-300 of 312. Use :301 to continue]