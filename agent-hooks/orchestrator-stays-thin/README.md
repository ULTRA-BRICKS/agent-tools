# orchestrator-stays-thin

**Points:** `pre-write` + `pre-bash` (MAIN thread only) · **Fail policy:** `open` · **Priority:** 30

Enforcement-doctrine **gate 2**. The main thread (orchestrator) plans, dispatches, and
verifies — it does **not** implement inline. When it tries to do non-trivial work itself —
a code Edit/Write to a non-docs file, or a multi-step/heavy Bash command — this gate
**WARNS, then BLOCKS**, with "delegate to a subagent" (`delegate-work-to-subagents`,
enforced).

One script, two descriptors:

- `pre-write` — a non-docs source edit (`.py/.ts/.go/...`). Docs/config/lockfiles
  (`.md/.json/.yaml/...`, README/ROADMAP/AGENTS) are exempt — editing those is not
  "implementing a feature".
- `pre-bash` — a multi-step (`&&`, `||`, `;`, `|`, loop, `make`) or heavy
  (build/test/install runner) command. Read-only inspection (`git status`, `gh pr view`,
  `ls/cat/grep/jq`) is the orchestrator's verify job and is never gated.

## Doctrine compliance

- **Satisfiable (a):** hand the edit/command to a subagent — always clears it.
- **Tiered (b):** a true two-stage escalation keyed per target — the FIRST non-trivial
  attempt at a given file/command WARNS (and is allowed); a REPEAT within
  `ORCH_THIN_WARN_WINDOW_S` (default 900s) BLOCKS. No first-keystroke wall.
- **Scoped (c):** **subagents are EXEMPT** — they legitimately do the work. Signal:
  `CLAUDE_CODE_CHILD_SESSION`.
- **Honest escape hatch (d):** `ALLOW_ORCHESTRATOR_WORK=1` +
  `ALLOW_ORCHESTRATOR_WORK_REASON='why'` (works for both points), or an inline
  `# orchestrator-work-ok: why` in a bash command.

## Configuration

- `ORCH_THIN_MARKER_DIR` — where the per-target warn markers live
  (default `~/.cache/agent-tools/orch-thin`).
- `ORCH_THIN_WARN_WINDOW_S` — warn→block escalation window (default `900`).

## Test

```bash
chmod +x orchestrator_stays_thin.py
# code write on main thread → WARN first (allow), repeat → BLOCK
echo '{"point":"pre-write","args":{"file_path":"src/app.py","content":"x=1"}}' | ./orchestrator_stays_thin.py; echo "exit=$?"
echo '{"point":"pre-write","args":{"file_path":"src/app.py","content":"x=1"}}' | ./orchestrator_stays_thin.py; echo "exit=$?"
# docs write → allow
echo '{"point":"pre-write","args":{"file_path":"README.md","content":"hi"}}' | ./orchestrator_stays_thin.py; echo "exit=$?"
# subagent → allow (exempt)
CLAUDE_CODE_CHILD_SESSION=1 sh -c 'echo "{\"point\":\"pre-write\",\"args\":{\"file_path\":\"src/app.py\"}}" | ./orchestrator_stays_thin.py'; echo "exit=$?"
```
