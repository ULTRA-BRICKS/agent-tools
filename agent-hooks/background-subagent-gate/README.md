# background-subagent-gate

**Point:** `pre-tool` (the `Task`/`Agent` dispatch tool) · **Fail policy:** `open` · **Priority:** 20

Enforcement-doctrine **gate 1** (ROADMAP "DOCTRINE: enforce correct agent behavior with
PreToolUse hooks"). The orchestrator (the **main thread**) must dispatch subagents in the
**BACKGROUND**. A foreground `Task`/`Agent` call blocks the main thread until the subagent
finishes — serializing what should run in parallel and stalling the session.

Detects: a subagent-dispatch tool call (`Task`, or `Agent` in some builds) where
`run_in_background` is not truthy → a non-trivial dispatch is **BLOCKED** with the 3-part
reminder (why / set `run_in_background: true` or a Workflow / orchestrator-plans-dispatches-
verifies); a trivial one (short prompt) only warns.

## Doctrine compliance

- **Satisfiable (a):** the honest fix is one keystroke — set `run_in_background: true`.
- **Tiered (b):** a trivial scout (`prompt` ≤ `BG_SUBAGENT_TRIVIAL_CHARS`, default 240)
  WARNS; a non-trivial dispatch BLOCKS.
- **Scoped (c):** **subagents are EXEMPT.** A subagent legitimately fans out its own
  children synchronously. Signal: CC sets `CLAUDE_CODE_CHILD_SESSION` in every child/
  subagent session and leaves it unset on the main thread.
- **Honest escape hatch (d):** `ALLOW_FOREGROUND_SUBAGENT=1` +
  `ALLOW_FOREGROUND_SUBAGENT_REASON='why'`, or an inline `foreground-subagent-ok: why`
  token in the prompt. A reasonless override still blocks.

## Configuration

- `BG_SUBAGENT_TRIVIAL_CHARS` — prompt length at/below which a foreground dispatch only
  warns (default `240`).
- `CLAUDE_AGENT_IS_SUBAGENT` — explicit subagent override (for harnesses/tests that set
  their own flag instead of `CLAUDE_CODE_CHILD_SESSION`).

## Why an agent-hook

A prose rule ("dispatch in the background") regresses within a few turns — the doctrine's
whole point. The hook makes it structural: the wrong dispatch is refused at the moment it's
attempted, with the right way in the reason.

## Test

```bash
chmod +x background_subagent_gate.py
# foreground, non-trivial → BLOCK
echo '{"point":"pre-tool","args":{"run_in_background":false,"prompt":"'"$(printf 'x%.0s' {1..300})"'"}}' | ./background_subagent_gate.py; echo "exit=$?"
# background → allow
echo '{"point":"pre-tool","args":{"run_in_background":true,"prompt":"build X"}}' | ./background_subagent_gate.py; echo "exit=$?"
# inside a subagent → allow (exempt)
CLAUDE_CODE_CHILD_SESSION=1 sh -c 'echo "{\"point\":\"pre-tool\",\"args\":{\"run_in_background\":false,\"prompt\":\"'"$(printf 'x%.0s' {1..300})"'\"}}" | ./background_subagent_gate.py'; echo "exit=$?"
```
