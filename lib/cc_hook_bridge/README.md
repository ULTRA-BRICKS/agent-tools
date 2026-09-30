# cc-hook-bridge — fire `agents-hooks/v1` hooks inside Claude Code

## The problem this fixes (agent-tools#18)

agent-tools ships **agent-hooks** under `agent-hooks/<name>/` — out-of-process guards
(`block-raw-pr-merge`, `block-secrets-write`, `enforce-timeout-on-bash`, …) that speak the
`agents-hooks/v1` contract: a JSON descriptor + an executable, installed into
`~/.claude/hooks/`, signalling **BLOCK via exit code 10**.

**Claude Code never runs those descriptors.** CC only runs hooks declared in
`settings.json` under `PreToolUse` / `PostToolUse` / `Stop` (each = a matcher + a shell
command fed the tool call as JSON on stdin). Nothing reads `~/.claude/hooks/*.json` and
invokes it. So **every agent-hook is inert in CC** until something bridges the two
contracts. This package is that bridge.

## What it does

A single dispatcher CC calls once per event:

```
python3 -m cc_hook_bridge PreToolUse   # for the Bash, Edit|Write, Agent|Task, Skill, Monitor matchers
python3 -m cc_hook_bridge PostToolUse  # for the Edit|Write matcher (post-write)
python3 -m cc_hook_bridge Stop
```

On each call it:

1. reads the CC tool-call JSON from stdin (`tool_name`, `tool_input`, `cwd`, …);
2. maps the `(event, tool)` to a logical `agents-hooks/v1` **point**
   (PreToolUse: `Bash`→`pre-bash`, `Write|Edit|MultiEdit|NotebookEdit`→`pre-write`,
   `Agent|Task`→`pre-agent`, `Skill`→`pre-skill`, `Monitor`→`pre-monitor`, `Workflow`→`pre-workflow`;
   PostToolUse: `Write|Edit|MultiEdit|NotebookEdit`→`post-write`; `Stop`→`stop`);
3. enumerates the installed descriptors in `~/.claude/hooks/*.json` for that point
   (sorted by `priority`, then `id`);
4. translates the CC event into the v1 event each hook script reads
   (`{hook_api, event_id, tool, point, harness, command, cwd, args}` — `harness` is always
   `"claude-code"` here, a module constant, not derived from the CC event) and runs each script;
5. translates the v1 **exit-10 BLOCK** into CC's own block signal — **first block wins**,
   its reason is surfaced to the model.

## The CC block contract (confirmed, not assumed)

Confirmed against the live docs — <https://code.claude.com/docs/en/hooks> — with the
installed CC **2.1.177**:

| CC event      | How the bridge blocks                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------- |
| `PreToolUse`  | exit 0 + `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}` |
| `Stop`        | exit 0 + `{"decision":"block","reason":"…"}`                                                             |
| `PostToolUse` | exit 0 + `{"decision":"block","reason":"…"}` — **feedback**: the tool already ran, the reason is surfaced to the model |

**Why the structured JSON form, not exit 2?** The docs are explicit: *"You must choose one
approach per hook… Claude Code only processes JSON on exit 0. If you exit 2, any JSON is
ignored."* Exit 2 blocks too, but discards the reason and only shows stderr. The
`permissionDecision: "deny"` form carries the full, human-readable reason from the v1
hook's `message` straight to the model — strictly richer, and it matches the existing
installed `rtk-rewrite.sh` PreToolUse hook's style.

> **PostToolUse note.** The tool has already run by then, so PostToolUse *cannot* un-run
> it (the docs are explicit) — its `{"decision":"block"}` is **advisory feedback**: CC
> surfaces the reason to the model. That is exactly the contract the `post-write` hooks
> want (`format-on-write` reacts silently; `lint-on-write` uses the feedback channel to
> put lint findings in front of the agent right after the write).

## Subagent identity (`args.agent_id`)

Subagent-aware hooks (`background-subagent-gate`, `orchestrator-stays-thin`,
`no-long-inline-process`, `subagent-no-bg-longproc`, `subagent-no-monitor`,
`skills-read-gate`) read `args.agent_id`. The bridge fills it from, in order:

1. CC's own TOP-LEVEL `agent_id` (present only when the call fires inside a CC-dispatched
   subagent) — `agent_type`/`session_id` get the same treatment;
2. otherwise the launcher process env: `RIG_AGENT_ID=<name>`, or `RIG_DETACHED_AGENT=1`
   (anonymous `detached`), set by `rig-detached-claude-code` for the `claude -p` child it
   detaches (agent-tools#831). CC runs hook commands with its own process env, so the markers
   reach the bridge on every call of that session — the same `_detached_agent_id` contract as
   the codex/omp/opencode bridges.

A copy riding in `tool_input` is always dropped (model-controlled), and a blank/whitespace
marker is not an identity. Trust model: a running orchestrator cannot retroactively set its own
process env, only a child's — which is exactly the sanctioned act of dispatching a subagent.

## Fail policy

- **Dispatcher-level: fail-OPEN.** Any unexpected error in the bridge itself (bad stdin,
  unreadable descriptor dir, internal bug) is swallowed, logged to stderr, and the call is
  **allowed**. A broken bridge must never wedge every tool call.
- **Hook-level: honors `on_error`.** A hook that exits 10 blocks. A hook that errors
  (any other non-zero exit, a timeout, an unrunnable `cmd`) is resolved by its descriptor's
  `on_error`: `closed` → **block** ("I couldn't check" means "don't do it", for security
  gates); `open` (default) → allow (advisory hooks stay out of the way).

## The tg-ctl inbox on `Stop` (agent-tools#526 / tg-cli#306)

Alongside the `stop` descriptors, the `Stop` dispatch checks the **tg-ctl Stop-hook
inbox** — the tmux-free channel by which the Telegram bridge reaches an agent that runs
OUTSIDE tmux (no pane → no `send-keys`). If `<tg-cli config dir>/inbox/<key>/pending.jsonl`
holds entries for THIS agent, the dispatcher archives them and answers
`{"decision":"block","reason":"<the queued messages>"}`, so CC feeds the text
(`[TG from <name> tg#<id>] …`, the same wrap a tmux inject uses) to the agent as its next
instruction. The daemon then reacts on the Telegram message.

- `<key>` is the agent's `--name` (sanitized), else `cwd-<sha256(cwd)[:16]>` — the exact
  algorithm, file layout and test vectors live in
  [`lib/agenttools_tg_inbox/README.md`](../agenttools_tg_inbox/README.md) and are mirrored
  byte-for-byte in tg-cli (`features/tg-ctl/unreachable.ts`). The agent process is found
  via `CLAUDE_PID` (CC exports it to hooks), else the parent chain.
- **Never blocks on an empty, missing or malformed inbox** (fail-open, one stderr line);
  at-most-once (published as a complete `delivered-…jsonl` batch before the block is
  emitted). The `stop` descriptors still run on the same `Stop`; a blocking one's reason
  follows the messages in the same block — neither side is starved or deferred.
- **Limit:** an idle agent gets the message only at its next turn end. The same check runs
  in `codex_hook_bridge` (cwd key — Codex has no `--name`); opencode has no Stop point.

## Installation (via rig)

`rig apply` registers the dispatcher in the harness `settings.json` (harness-keyed) when a
`claude-code` `harness:` block is present and `agent_hooks` is enabled — the
`register_hook_bridge` action, opt-out via `harness.hook_bridge: { enabled: false }`. That
wiring ships in **rig-cli** (a separate PR), not in this package; this package is the
dispatcher rig points at. Manual equivalent (what rig writes):

```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] },
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] },
      { "matcher": "Agent|Task",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] },
      { "matcher": "Skill",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] },
      { "matcher": "Monitor",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] },
      { "matcher": "Workflow",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PostToolUse" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge Stop" }] }
    ]
  }
}
```

The `command` must run with `cc_hook_bridge` importable — rig writes it as
`PYTHONPATH=<agent-tools>/lib python3 -m cc_hook_bridge <Event>` so it resolves against the
agent-tools checkout (the same checkout whose `agent-hooks/` scripts the descriptors point
at).

## Environment overrides

| Env var         | Effect                                                              |
| --------------- | ------------------------------------------------------------------- |
| `CC_HOOKS_DIR`  | Override the descriptor dir (default `~/.claude/hooks`). For tests. |

## Extensibility — what to change when CC changes

The bridge is tied to CC's hook surface in three places. A new CC tool or event means
updating all of them together:

- **`point_for_event`** — maps a CC `(event, tool_name)` to a logical v1 point. A new
  file-edit tool also needs adding to `_WRITE_TOOLS` *and* its payload field taught to
  **`_proposed_write_text`** (else its content is never scanned by the pre-write guards),
  *and* added to the rig-cli matcher `Edit|Write|MultiEdit|NotebookEdit`. A whole new logical
  point (like `pre-agent` for `Agent|Task`, `pre-skill` for `Skill`, `pre-monitor` for
  `Monitor`, `pre-workflow` for `Workflow`) needs its own rig-cli matcher registered in
  `hook_bridge_entries` too — the mapping here is inert on its own until rig-cli's matcher
  change ships (a two-repo change, same split those points used).
- **`cc_block_output`** — the per-event block JSON. A new blocking event (beyond
  PreToolUse / PostToolUse / Stop) needs its block shape added here.
- **`_KNOWN_EVENTS`** — the typo guard in `main`.

## Proof it actually blocks

`tests/test_cc_hook_bridge.py` installs the real `block-raw-pr-merge` guard in an isolated
`$HOME`, runs the dispatcher as a subprocess (exactly how CC invokes it), and asserts a raw
`gh pr merge --admin` gets `permissionDecision: "deny"` while `gh ship 42` passes through.
It also covers the pre-write normalization (a secret in a `MultiEdit`'s `edits[].new_string`
is flattened into `args.content` so `block-secrets-write` catches it), fail-open (garbage
stdin), fail-closed (a crashing security gate, a bad `timeout_ms` on a closed gate, a bad
`priority` not crashing the dispatch), the Stop `decision:block` path, and first-block-wins
ordering. `tests/test_tg_inbox.py` covers the tg-ctl inbox delivery on `Stop` (pending →
block + archive, idempotent; empty/malformed never block) through both bridges.

```
$ uv run --with pytest python -m pytest tests/test_cc_hook_bridge.py -q
...................                                                      [100%]
19 passed
```
