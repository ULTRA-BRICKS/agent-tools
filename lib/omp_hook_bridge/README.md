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
itself while it waits is visible while it is still waiting.

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
| `tool_call` + `edit` | `pre-write` | `input.input` (the hashline patch text) is parsed for every `[PATH#TAG]` section path → `args.file_path`/`path`/`file_paths`; a best-effort reconstruction of added body rows becomes `args.content` (same-path sections accumulate). No section found → the `replace`/`patch` fallback below. |
| `tool_call` + `write` | `pre-write` | `input.path` / `input.content` normalize to `args.file_path`/`path`/`content` directly — same shape family as opencode's `write` tool. |
| `tool_call` + `apply_patch` | `pre-write` | The wire name omp uses for `edit` in apply_patch custom-tool mode (docs/tools/edit.md); the payload is the classic `*** Update File:` / `*** Add File:` / `*** Delete File:` / `*** Move to:` envelope, not hashline. Parsed with logic ported from the opencode bridge (see below). |
| `tool_call` + `notebook` | `pre-write` | omp has no confirmed public tool schema for a dedicated notebook tool in the docs consulted while building this bridge. Best-effort passthrough only: whichever of `path`/`notebook_path`/`file_path` is present in `input` is normalized onto `args.notebook_path`/`file_path`/`path`. Kept as a mapped point (not dropped) on the assumption that IF omp ships one, its shape is path-like enough for this passthrough to still work; revisit once the real schema is confirmed. |
| `tool_call` + `task` | `pre-agent` | omp's task item shape is `{name?, agent?, task, effort?, outputSchema?, schemaMode?, isolated?}` (flat) or `{context, tasks: item[]}` (batch, the default). Normalized onto the shared pre-agent contract the `background-subagent-gate` reads: `agent` → `args.subagent_type`, `task` → `args.prompt` (a batch joins every item's `task` with newlines), and `args.run_in_background: true` — see "omp `task` dispatches are background by contract" below. Raw `task`/`name`/`tasks`/`context` fields are kept alongside. |
| `tool_result` + `edit`/`write`/`apply_patch` | `post-write` | Runs path-based hooks after the write; a block decision is logged only because omp cannot un-run a completed write. |

omp's session-lifecycle events (`session_shutdown`, `agent_end`, etc.) have no documented
blocking return contract in docs/hooks.md — this bridge does not map any of them to the v1
`stop` point, same reasoning the opencode bridge's README gives for `session.idle`.

For a multi-section `edit` call or a multi-file `apply_patch` call, the dispatcher fans out
one v1 event per file path, mirroring `opencode_hook_bridge`'s `_v1_events_for_dispatch`.

The bridge strips all omp tool-argument fields that look like agent identity (`agent_id`,
`agent_type`, camelCase variants, and `agent`) before forwarding args — the same
`_FORGED_AGENT_KEYS` treatment as the codex/opencode bridges, because omp exposes no
TRUSTED per-tool-call subagent identity in the extension event either (confirmed absent
from the documented `tool_call`/`tool_result` shape). Every translated v1 event also
carries a top-level `harness: "omp"` — a hardcoded module constant (`dispatch.HARNESS`),
never derived from the omp event, so it can't be forged the way the identity fields above
must be actively stripped. `agent-hooks/orchestrator-stays-thin`'s `EXEMPT_HARNESSES`
reads this tag to exempt omp from that gate (agent-tools#533) for the same reason it
already exempts codex and opencode: none of the three expose a trusted per-call subagent
identity, so the gate's "main thread must delegate to a CC subagent" premise does not
transplant cleanly onto any of them.

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
([rig-cli#363](https://github.com/alex-mextner/rig-cli/issues/363) tracks provisioning it):

```yaml
extensionHandlers:
  toolCallTimeoutMs: 2100000
```

**Without that setting, any hook that holds a call longer than 30 s — i.e. every Telegram
approval hatch — ends as an omp-generated block naming this extension's path, not as the
hook's own decision.** That is the safe direction (a fail-closed hook still does not become an
allow) but it is not the hook's verdict, and the human's later approval tap lands on a call omp
has already refused. Do NOT "fix" it by lowering `OMP_HOOK_BRIDGE_TIMEOUT_MS` under 30 s: this
bridge fails OPEN, so that turns a denied hatch into an allow — strictly worse. The extension
prints a one-shot stderr line naming this setting once a dispatch passes 10 s, so a user who
hits the ceiling is told why.

On the Python side, `main()` keeps the same fail-open contract as the codex/opencode
dispatchers: malformed stdin, a missing `agent_hooks_v1` import, or any dispatcher-internal
exception logs to stderr and exits 0 with no result on stdout (equivalent to "allow"). A
loaded descriptor still honors full v1 semantics: exit 10 blocks pre-tool calls, exit 0
allows, and any other outcome resolves through the descriptor's own `on_error` policy.
