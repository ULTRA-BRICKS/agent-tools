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
python3 -m cc_hook_bridge PreToolUse   # for the Bash + Edit|Write matchers
python3 -m cc_hook_bridge Stop
```

On each call it:

1. reads the CC tool-call JSON from stdin (`tool_name`, `tool_input`, `cwd`, …);
2. maps the `(event, tool)` to a logical `agents-hooks/v1` **point**
   (`Bash`→`pre-bash`, `Write|Edit|MultiEdit|NotebookEdit`→`pre-write`,
   `Task|Agent`→`pre-tool`, `Skill`→`pre-skill`, `Stop`→`stop`);
3. enumerates the installed descriptors in `~/.claude/hooks/*.json` for that point
   (sorted by `priority`, then `id`);
4. translates the CC event into the v1 event each hook script reads
   (`{hook_api, event_id, tool, point, command, cwd, args}`) and runs each script;
5. translates the v1 **exit-10 BLOCK** into CC's own block signal — **first block wins**,
   its reason is surfaced to the model.

## The CC block contract (confirmed, not assumed)

Confirmed against the live docs — <https://code.claude.com/docs/en/hooks> — with the
installed CC **2.1.177**:

| CC event      | How the bridge blocks                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------- |
| `PreToolUse`  | exit 0 + `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}` |
| `Stop`        | exit 0 + `{"decision":"block","reason":"…"}`                                                             |

**Why the structured JSON form, not exit 2?** The docs are explicit: *"You must choose one
approach per hook… Claude Code only processes JSON on exit 0. If you exit 2, any JSON is
ignored."* Exit 2 blocks too, but discards the reason and only shows stderr. The
`permissionDecision: "deny"` form carries the full, human-readable reason from the v1
hook's `message` straight to the model — strictly richer, and it matches the existing
installed `rtk-rewrite.sh` PreToolUse hook's style.

> **PostToolUse note.** The tool has already run by then, so PostToolUse *cannot* block it
> (the docs are explicit). The bridge therefore wires only `PreToolUse` (real prevention)
> and `Stop`. A future advisory PostToolUse point can map to `{"decision":"block"}` (stops
> the turn, surfaces feedback) without un-running the tool — not wired today because no
> shipped agent-hook needs it.

## Fail policy

- **Dispatcher-level: fail-OPEN.** Any unexpected error in the bridge itself (bad stdin,
  unreadable descriptor dir, internal bug) is swallowed, logged to stderr, and the call is
  **allowed**. A broken bridge must never wedge every tool call.
- **Hook-level: honors `on_error`.** A hook that exits 10 blocks. A hook that errors
  (any other non-zero exit, a timeout, an unrunnable `cmd`) is resolved by its descriptor's
  `on_error`: `closed` → **block** ("I couldn't check" means "don't do it", for security
  gates); `open` (default) → allow (advisory hooks stay out of the way).

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
      { "matcher": "Task|Agent",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] },
      { "matcher": "Skill",
        "hooks": [{ "type": "command",
          "command": "python3 -m cc_hook_bridge PreToolUse" }] }
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
  *and* added to the rig-cli matcher `Edit|Write|MultiEdit|NotebookEdit`. The
  subagent-dispatch tools (`_SUBAGENT_DISPATCH_TOOLS` → `pre-tool`) and the `Skill` tool
  (→ `pre-skill`) each have their own matcher in rig-cli (`Task|Agent`, `Skill`); a renamed
  CC tool means updating the set here AND the rig-cli matcher together.
- **`cc_block_output`** — the per-event block JSON. A new blocking event (beyond PreToolUse
  / Stop) needs its block shape added here.
- **`_KNOWN_EVENTS`** — the typo guard in `main`.

## Proof it actually blocks

`tests/test_cc_hook_bridge.py` installs the real `block-raw-pr-merge` guard in an isolated
`$HOME`, runs the dispatcher as a subprocess (exactly how CC invokes it), and asserts a raw
`gh pr merge --admin` gets `permissionDecision: "deny"` while `gh ship 42` passes through.
It also covers the pre-write normalization (a secret in a `MultiEdit`'s `edits[].new_string`
is flattened into `args.content` so `block-secrets-write` catches it), fail-open (garbage
stdin), fail-closed (a crashing security gate, a bad `timeout_ms` on a closed gate, a bad
`priority` not crashing the dispatch), the Stop `decision:block` path, and first-block-wins
ordering.

```
$ uv run --with pytest python -m pytest tests/test_cc_hook_bridge.py -q
...................                                                      [100%]
19 passed
```
