# no-long-inline-process

**Point:** `pre-bash` (MAIN thread only) · **Fail policy:** `open` · **Priority:** 25

Enforcement-doctrine **gate 3**. A long-running command in the orchestrator (main thread)
holds it hostage — the agent can't dispatch or collect anything else until it returns. This
gate detects the long-running shapes the CTO named and **BLOCKS** them on the main thread:

- `review` (multi-model, runs for **minutes**),
- anything `--watch` (`gh pr checks --watch`, `tsc --watch`, dev servers),
- a build/test **suite** run (`npm test`, `pytest`, `cargo test`, ...),
- `sleep N`.

Narrower and stricter than gate 2 (orchestrator-stays-thin): gate 2 is about *implementing*
inline; this is specifically about a process that **blocks for a long time**. Because the
cost is a wedged main thread, the default tier is BLOCK — but it stays satisfiable.

## Doctrine compliance

- **Satisfiable (a):** dispatch the long command to a background subagent
  (`run_in_background: true`) — always clears it.
- **Tiered (b):** a command that's already backgrounded (`... &`, `nohup`, `setsid`,
  `disown`) is allowed outright; a short, self-bounded `timeout N ...`
  (N ≤ `LONG_INLINE_TIMEOUT_OK_S`, default 120) only WARNS.
- **Scoped (c):** **subagents are EXEMPT** — a subagent legitimately runs `review` before
  it commits and watches its own CI. Signal: `CLAUDE_CODE_CHILD_SESSION`.
- **Honest escape hatch (d):** `ALLOW_LONG_INLINE=1` + `ALLOW_LONG_INLINE_REASON='why'`, or
  an inline `# long-inline-ok: why`.

## Configuration

- `LONG_INLINE_TIMEOUT_OK_S` — a `timeout N` at/below this bound only warns (default `120`).

## Test

```bash
chmod +x no_long_inline_process.py
# `review` on main thread → BLOCK
echo '{"point":"pre-bash","args":{"command":"review -C /repo"}}' | ./no_long_inline_process.py; echo "exit=$?"
# backgrounded → allow
echo '{"point":"pre-bash","args":{"command":"pytest tests/ &"}}' | ./no_long_inline_process.py; echo "exit=$?"
# subagent → allow (exempt)
CLAUDE_CODE_CHILD_SESSION=1 sh -c 'echo "{\"point\":\"pre-bash\",\"args\":{\"command\":\"review -C /repo\"}}" | ./no_long_inline_process.py'; echo "exit=$?"
# escape hatch → allow
ALLOW_LONG_INLINE=1 ALLOW_LONG_INLINE_REASON="one-off" sh -c 'echo "{\"point\":\"pre-bash\",\"args\":{\"command\":\"review -C /repo\"}}" | ./no_long_inline_process.py'; echo "exit=$?"
```
