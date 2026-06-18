# visual-proof-gate

**Points:** `pre-bash` + `stop` · **Fail policy:** `open` · **Priority:** 40

Enforcement-doctrine **gate 5**. A "done" claim or a commit on a **user-visible** change
(UI, a rendered image, a chart, generated output) with no screenshot the agent actually
LOOKED AT is not proof — "it should work" is not "it works" (`visual-proof-cycle`,
enforced).

One script, two descriptors:

- `pre-bash` — a `git commit` whose commit set touches user-visible types → **CHECK** a
  fresh screenshot artifact exists; warn first, then block.
- `stop` — the turn is ending (the "done" moment) → same CHECK, blocked at most once per
  session so a genuine no-proof stop can still finish after seeing the message.

**What counts as proof** (satisfiable by an honest action): a **real, freshly-modified
screenshot artifact** the agent can have looked at — either
1. named by an inline `screenshot: <path>` token (and **not** itself part of the commit), OR
2. a fresh image in the working tree that is **not** part of the commit set (a verification
   capture taken to check the change), modified within `VISUAL_PROOF_SHOT_FRESH_S`
   (default 3600s).

**Invoking the `visual-proof-cycle` skill is NOT by itself proof** — intent isn't evidence
(an agent could invoke it and produce nothing). Only a real file on disk with a recent mtime
counts, and it can't be the committed asset itself (no self-proof). That keeps the gate
satisfiable only by actually capturing — which is the whole point.

**What is user-visible:** changes to `.tsx/.jsx/.vue/.svelte/.css/.html/.svg/.png/...` (and
`*.stories.*`). A pure-backend/docs change is **not** gated — otherwise the gate would be
unsatisfiable and brittle.

**Commit-set scoping (not the whole working tree):** the gate inspects the files THIS commit
lands — `git diff --cached`, honoring `git -C <dir>` (the right repo), explicit pathspecs
(`git commit Button.tsx`), and `-a`/`--all`. A chained `git add … && git commit` stages
inside the same command (the index is stale when the hook fires), so that case conservatively
considers the whole working tree.

## Doctrine compliance

- **Satisfiable (a):** capture a screenshot and reference it (or leave it fresh in the tree).
- **Tiered (b):** warn on the first commit/stop without proof, block the next within
  `VISUAL_PROOF_WARN_WINDOW_S` (default 1800s).
- **Scoped:** applies to **both** orchestrator and subagents (whoever ships a visible change
  owes proof). NOT one of the subagent-exempt gates 1-3.
- **Honest escape hatch (d):** `SKIP_VISUAL_PROOF=1` + `SKIP_VISUAL_PROOF_REASON='why'`
  (e.g. headless CI), or an inline `# visual-proof-ok: why` in a commit.

## Configuration

- `VISUAL_PROOF_SHOT_FRESH_S` — how recent a screenshot file must be (default `3600`).
- `VISUAL_PROOF_WARN_WINDOW_S` — warn→block window (default `1800`).
- `VISUAL_PROOF_MARKER_DIR` — marker dir for the warn/stop markers (default
  `~/.cache/agent-tools/visual-proof`).

## Test

```bash
chmod +x visual_proof_gate.py
# (run inside a git repo with a staged .tsx change and no screenshot)
# commit a visible change, no proof → WARN first (allow), repeat → BLOCK
echo '{"point":"pre-bash","event_id":"s1","cwd":"'"$PWD"'","args":{"command":"git commit -m x","session_id":"s1"}}' | ./visual_proof_gate.py; echo "exit=$?"
# add a fresh verification screenshot OUTSIDE the commit → next commit allows
: > shot.png
echo '{"point":"pre-bash","event_id":"s1","cwd":"'"$PWD"'","args":{"command":"git commit -m x","session_id":"s1"}}' | ./visual_proof_gate.py; echo "exit=$?"
```
