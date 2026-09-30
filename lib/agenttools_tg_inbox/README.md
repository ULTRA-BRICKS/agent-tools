# agenttools_tg_inbox — the tg-ctl Stop-hook inbox reader

The tmux-free delivery channel for Telegram text to an omp session that `tg-ctl` cannot
reach live (agent-tools#526/#656, tg-cli#306). `tg-ctl` (the Telegram bridge in
[tg-cli](https://git.hyperide.ai/ultrabricks/tg-cli)) delivers to agents by typing into
their tmux pane (`send-keys`) or, for omp, over its omp-live endpoint. The only writer left
is tg-ctl's **grant-notice fallback**: when an omp's omp-live endpoint is unreachable, the
hatch grant notice is **queued to an inbox file**. This package is the other half:
`omp_hook_bridge` calls it on `yield` (omp's Stop equivalent), and when the inbox holds
entries it answers `{"decision":"block","reason":"<the messages>"}` — omp then feeds that
text to the agent as its next instruction.

**Claude Code and Codex are tmux-only here (tg-cli#456).** tg-ctl no longer queues for them (a
`/agent` to a cc/codex in no tmux pane and no `rig harness run` session gets a one-line
refusal naming `rig harness run <harness> --name <label>`; a paneless cc/codex hatch grant
notice is not queued, and a `rig harness run` session is reached through its PTY instead),
and it archived their stranded backlog once (`pending.jsonl` → `stranded-<stamp>.jsonl`). `cc_hook_bridge` and
`codex_hook_bridge` therefore no longer read the inbox: an unnamed cc/codex in an omp's cwd
shares that omp's `cwd-<hash>` key, so a cc/codex Stop would otherwise steal the omp's grant
notice.

## Limits, stated honestly

- A message reaches the agent only **at its next yield**. An omp that is already idle
  (waiting at the prompt) receives it when it next finishes a turn — live Telegram→idle
  omp still needs omp-live/ACP.
- Delivery is **at-most-once**: entries are archived to a `delivered-<pid>-<ns>-<rnd>.jsonl`
  batch _before_ the block is emitted. If the harness ignores the block (it never does
  today), the message is not retried — it is on disk in that batch for a human. The omp
  bridge consumes the inbox as the **last** step of a yield, after the v1 stop hooks ran:
  consuming is irreversible, and a hook that raised would otherwise have eaten a message
  the agent never saw.
- **Two sessions sharing one key share one inbox.** A helper in the same cwd shares the
  cwd-hash key with its parent omp; whichever yields first consumes the other's messages.
  Inherent to the contract.
- omp consumes the inbox on `tool_call` + `yield` (mapped to v1 `stop`) — including a
  helper completing. The key is the cwd hash: omp has no `--name`, so `omp_hook_bridge`
  calls `agent_key(None, cwd)`. pi has no session-end block contract and opencode's plugin
  bridge has no Stop point, so neither consumes the inbox.

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

| file                               | writer                                                                                                | reader                                                                             |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `pending.jsonl`                    | tg-ctl daemon (append, one JSON object per line)                                                      | `omp_hook_bridge` via this package (claims by rename)                              |
| `delivered-<pid>-<ns>-<rnd>.jsonl` | this package — ONE complete file per consumption, written as `tmp-<pid>-<ns>-<rnd>.jsonl` and renamed | tg-ctl daemon (reacts on the Telegram message, archives to `acked.jsonl`, unlinks) |
| `acked.jsonl`                      | tg-ctl daemon                                                                                         | humans                                                                             |
| `stranded-<stamp>.jsonl`           | tg-ctl daemon — one-time archive of the retired cc/codex backlog (tg-cli#456)                         | humans                                                                             |

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

tg-cli reads `--name` from a `ps` line, which flattens argv into one space-joined string, so
a `--name` with whitespace (`--name "my agent"`) reads as `my` — such a name is not a
distinct key. Use single-token names.

## API

```python
from agenttools_tg_inbox import agent_key, consume_pending, format_block_reason

key = agent_key(None, cwd)                      # omp: always "cwd-<hash>"
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

In `omp_hook_bridge` the v1 `stop` descriptors **always run**; when one blocks, its reason
follows the queued messages in the same block (a fail-closed gate on `yield` is never
starved by a busy inbox, and the messages are never deferred a turn).

Every failure path logs one `tg-inbox: …` line to the hooks debug log and returns `[]`; the
bridge treats that as "no block". stdlib only.

## Tests

```
uv run --with pytest python -m pytest tests/test_tg_inbox.py -q
```

Covers the shared key vectors, the pending → block + archive round trip (idempotent second
call), empty / missing / malformed inboxes never blocking, an unwritable archive keeping the
claim, the real `omp_hook_bridge` dispatcher delivering on `yield`, and the real
`cc_hook_bridge` + `codex_hook_bridge` dispatchers leaving a pending inbox untouched on
`Stop` — all run as subprocesses against a temp `TG_CTL_CONFIG_DIR`.
