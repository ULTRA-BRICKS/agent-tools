# skills-read-gate

**Points:** `pre-skill` + `pre-bash` + `pre-write` · **Fail policy:** `open` · **Priority:** 5/35

Enforcement-doctrine **gate 4** (and the ROADMAP "MISSING gate" item). Nothing today
verifies an agent actually INVOKED the mandatory skills before it acts — skill-loading is
purely advisory. This gate makes "read your skills first" real.

One script, three descriptors:

- `pre-skill` — the **Skill tool** is firing → **RECORD** this skill as invoked this
  session (per-session marker). This is the satisfiable, honest action: invoking the skill
  (the real Skill-tool call) writes the marker — never a forge.
- `pre-bash` — a `git commit` (a substantive act) → **CHECK** the mandatory skills were
  invoked this session; warn first, then block.
- `pre-write` — a non-docs code Edit/Write (also substantive) → same **CHECK**.

"Read" = a Skill-tool invocation **this session** (keyed by the CC session id). The
mandatory set is the universal-mandatory tier the doctrine names —
`delegate-work-to-subagents` and `visual-proof-cycle` — overridable via
`SKILLS_READ_MANDATORY`. Relevance-triggered skills are deliberately **out of scope for the
hard gate** (relevance detection is heuristic and would make the gate brittle); the gate
enforces only the always-mandatory set.

## Doctrine compliance

- **Satisfiable (a):** invoke the named skill with the Skill tool; the marker is written
  for real by the `pre-skill` half.
- **Tiered (b):** a missing mandatory skill WARNS on the first substantive act, then BLOCKS
  the next within `SKILLS_READ_WARN_WINDOW_S` (default 1800s).
- **Scoped:** applies to **both** the orchestrator and subagents — a subagent must read its
  skills too. (This is NOT one of the subagent-exempt gates 1-3.)
- **Honest escape hatch (d):** `SKIP_SKILLS_READ_GATE=1` +
  `SKIP_SKILLS_READ_GATE_REASON='why'`, or an inline `# skills-read-ok: why` in a commit.

## Configuration

- `SKILLS_READ_MANDATORY` — comma-separated mandatory skill names (default
  `delegate-work-to-subagents,visual-proof-cycle`).
- `SKILLS_READ_TTL_S` — how long a skill invocation counts as read (default `7200`).
- `SKILLS_READ_WARN_WINDOW_S` — warn→block window (default `1800`).
- `SKILLS_READ_MARKER_DIR` — marker dir (default `~/.cache/agent-tools/skills-read`).

## Test

```bash
chmod +x skills_read_gate.py
export SKILLS_READ_MARKER_DIR=$(mktemp -d) SKILLS_READ_MANDATORY=visual-proof-cycle
# commit with the skill unread → WARN first (allow), repeat → BLOCK
echo '{"point":"pre-bash","event_id":"s1","args":{"command":"git commit -m x","session_id":"s1"}}' | ./skills_read_gate.py; echo "exit=$?"
echo '{"point":"pre-bash","event_id":"s1","args":{"command":"git commit -m x","session_id":"s1"}}' | ./skills_read_gate.py; echo "exit=$?"
# record the skill → next commit allows
echo '{"point":"pre-skill","event_id":"s1","args":{"skill":"visual-proof-cycle","session_id":"s1"}}' | ./skills_read_gate.py; echo "exit=$?"
echo '{"point":"pre-bash","event_id":"s1","args":{"command":"git commit -m x","session_id":"s1"}}' | ./skills_read_gate.py; echo "exit=$?"
```
