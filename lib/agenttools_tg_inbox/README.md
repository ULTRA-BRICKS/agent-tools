# agenttools_tg_inbox — the tg-ctl Stop-hook inbox reader

The tmux-free delivery channel for Telegram messages to an agent that `tg-ctl` cannot reach
with `tmux send-keys` (agent-tools#526, tg-cli#306). `tg-ctl` (the Telegram bridge in
[tg-cli](https://git.hyperide.ai/ultrabricks/tg-cli)) discovers agents via `tmux list-panes` and
delivers by typing into the pane. An interactive `claude` / `codex` started from a plain
terminal tab has no pane; `tg-ctl status` now lists it as `unreachable: not in tmux (tty …,
cwd …, name …)` and a `/agent <name> <text>` addressed to it is **queued to an inbox file**.
This package is the other half: the harness Stop bridges (`cc_hook_bridge`,
`codex_hook_bridge`, and `omp_hook_bridge` on yield) call it at every Stop-equivalent, and
when the inbox holds entries they answer
`{"decision":"block","reason":"<the messages>"}` — the harness then feeds that text to the
agent as its next instruction, exactly like a `[TG from … tg#<id>] …` line typed into a tmux
pane.

## Limits, stated honestly

- A message reaches the agent only **at its next turn end**. An agent that is already idle
  (waiting at the prompt) receives it when it next finishes a turn — i.e. after the user
  types something locally. `tg-ctl`'s reply says so ("delivery deferred until the agent is
  active"); there is no way to wake an idle Claude Code without a tty.
- Delivery is **at-most-once**: entries are archived to a `delivered-<pid>-<ns>-<rnd>.jsonl`
  batch *before* the block is emitted. If the harness ignores the block (it never does
  today), the message is not retried — it is on disk in that batch for a human. The bridges
  consume the inbox as the **last** step of a Stop, after the v1 stop hooks ran: consuming
  is irreversible, and a hook that raised would otherwise have eaten a message the agent
  never saw.
- **Two sessions sharing one key share one inbox.** Two unnamed agents in the same cwd (an
  unnamed Claude next to a Codex included) both key on `cwd-<hash>`; whichever Stops first
  consumes the other's messages. Inherent to the contract — tg-ctl refuses to queue when it
  sees such a live collision, and distinct `--name` values avoid it.
- Claude Code and Codex consume the inbox on their Stop hook. omp consumes it on
  `tool_call` + `yield` (mapped to v1 `stop`) — omp's Stop equivalent, including a helper
  completing. The inbox key is the cwd hash: omp has no `--name`, so `omp_hook_bridge`
  calls `agent_key(None, cwd)` and never `agent_key_for_process` (an omp started from a
  Claude-owned shell inherits `CLAUDE_PID`). A helper in the same cwd shares that key with
  its parent (the two-sessions-one-key contract above). Delivery is at the next yield, not
  while idle — live Telegram→idle omp still needs ACP. pi still has no session-end block
  contract, so a pi session outside tmux stays listed as unreachable with no fallback
  (tmux send-keys or ACP). opencode's plugin bridge has no Stop point either.

## The inbox key contract (shared with tg-cli — keep both sides identical)

```
key = sanitized(--name value)           when the agent's argv carries `--name X`
    = "cwd-" + sha256(cwd)[:16] (hex)    otherwise
sanitized(x) = every char outside [A-Za-z0-9._-] -> "_", truncated to 64 chars;
               an empty or dots-only result (".", "..") counts as "no name" — the key is
               a path segment under inbox/, so it must never escape it
cwd          = the agent process's working directory, trailing "/" stripped ("/" stays)
inbox dir    = <tg-cli config dir>/inbox/<key>/
               config dir = $TG_CTL_CONFIG_DIR, else ~/.config/tg-cli
```

| file | writer | reader |
| --- | --- | --- |
| `pending.jsonl` | tg-ctl daemon (append, one JSON object per line) | this package (claims by rename) |
| `delivered-<pid>-<ns>-<rnd>.jsonl` | this package — ONE complete file per consumption, written as `tmp-<pid>-<ns>-<rnd>.jsonl` and renamed | tg-ctl daemon (reacts on the Telegram message, archives to `acked.jsonl`, unlinks) |
| `acked.jsonl` | tg-ctl daemon | humans |

Directories are `0700`, files `0600` — the content is the user's private Telegram text.
The hook→daemon direction deliberately has **no shared append file**: a shared
`delivered.jsonl` the daemon claims by rename could be renamed and unlinked between the
hook's `open()` and `write()`, and the record would land on a dead inode. A complete batch
that appears atomically cannot be torn or lost.

Entry shape: `{"id": <telegram message_id or null>, "ts": "<ISO-8601>", "from": "<sender>",
"text": "<raw text>", "wrapped": "<the exact text to hand the agent>"}`. The reader only
needs `wrapped`; the tg-ctl side applies its inject wrap template before queueing so the
agent sees the same `[TG from <name> tg#<id>] <text>` shape either way.

The key derivation is implemented twice on purpose (Bun/TS in tg-cli
`features/tg-ctl/unreachable.ts`, Python here) with the **same test vectors** in
`tests/test_tg_inbox.py` and tg-cli's `tests/ctl-unreachable.test.ts`: `landing` →
`landing`; `my agent/1` → `my_agent_1`; no name + `/Users/ultra/work/landing` →
`cwd-ccfe64bae2f277d7`. Change one side, change the other, keep the vectors green.

`ps` flattens argv into one space-joined line, so a `--name` with whitespace (`--name "my
agent"`) reads as `my` on BOTH sides — consistent, but such a name is not a distinct key.
Use single-token names.

## How the reader finds "this agent"

The hook process has to key on the *agent it runs under*. Claude Code exports `CLAUDE_PID`
to its children (hooks included) — that pid's argv is read with `ps -o ppid=,args= -p` and
the `--name` parsed from it. Without `CLAUDE_PID` the reader climbs the parent chain from
its own parent (a few hops: `sh -c` → the agent) to the first argv that looks like an agent
binary. When nothing is found the key is the cwd hash of the event's `cwd`.

Codex and omp have no `--name`, so `codex_hook_bridge` and `omp_hook_bridge` call
`agent_key(None, cwd)` directly and never `agent_key_for_process`: a Codex/omp started
from a Claude-owned shell inherits `CLAUDE_PID`, and the process lookup would otherwise
key it on the Claude session's name.

## API

```python
from agenttools_tg_inbox import agent_key_for_process, consume_pending, format_block_reason

key = agent_key_for_process(cwd)                 # "--name" or "cwd-<hash>"
entries = consume_pending(key, session_id=sid)  # [] when empty/missing/malformed (fail-open)
if entries:
    reason = format_block_reason(entries)         # wrapped texts, oldest first, blank-line separated
```

`consume_pending` claims `pending.jsonl` by **rename** to a unique `claim-<pid>-<ns>-<rnd>.jsonl`
(atomic; the daemon's later appends land in a fresh file; a stale claim left by an earlier
failure can never be overwritten by a later rename), publishes every claimed line as one
complete `delivered-…jsonl` batch stamped with `delivered_ts` + `session_id` (malformed lines
flagged, never silently dropped), deletes the claim and only then returns the entries. If the
batch write fails it returns `[]` and leaves the claim on disk — delivering without a record
could re-deliver.

In the bridges the v1 `stop` descriptors **always run**; when one blocks, its reason follows
the queued messages in the same block (a fail-closed gate on `Stop` is never starved by a
busy inbox, and the messages are never deferred a turn).

Every failure path prints one `tg-inbox: …` line to stderr and returns `[]`; the bridges
treat that as "no block". stdlib only.

## Tests

```
uv run --with pytest python -m pytest tests/test_tg_inbox.py -q
```

Covers the shared key vectors, `CLAUDE_PID` / ancestry / fallback key resolution, the
pending → block + archive round trip (idempotent second call), empty / missing / malformed
inboxes never blocking, an unwritable archive keeping the claim, and the real
`cc_hook_bridge` + `codex_hook_bridge` + `omp_hook_bridge` dispatchers run as subprocesses
against a temp `TG_CTL_CONFIG_DIR`.
