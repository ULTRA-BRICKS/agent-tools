# enterworktree-foreign-guard

**Point:** `pre-worktree-enter` · **Fail policy:** `open` · **Priority:** 36

Stops an agent (orchestrator or a dispatched subagent) from `EnterWorktree`-ing, via `path`,
into a worktree a **DIFFERENT** agent created. Closes a recurring, confirmed harness bug: the
call reports **SUCCESS**, and then **permanently bricks the calling agent's Bash tool** for
the rest of that agent's session — every subsequent command, even `pwd`, gets refused, with
no recovery path (`ExitWorktree` does not help). This hit the same project's session history
at least four times, always in the same shape: an agent trying to resume or ship a PR/branch
that a different, earlier agent built in its own worktree.

## Why a hook, not just a documented rule

A prior fix documented "never `EnterWorktree` into a worktree you didn't create; use
`gh pr checkout <N>` instead" in `AGENTS.md`/`AGENTS-CORE.md` and the global
`worktree-isolation` skill. That depends on every future agent happening to read and remember
a rule buried in a docs file — it is not durable. This hook makes the failure mode
structurally impossible instead: it intercepts the `EnterWorktree` call **before the tool
ever runs** and refuses it outright when the target is foreign, rather than letting the tool
report success and brick the session afterward.

## How ownership is determined

CC names a dispatched subagent's isolated worktree `.claude/worktrees/agent-<agent_id>` — its
own convention, not something this hook invents (every worktree directory observed in the
incident's project follows it exactly). `cc_hook_bridge` forwards the CALLING agent's own
`agent_id` at the top level of the event **only** when the call fires inside a dispatched
subagent, and — per `dispatch.py`'s T2 precedence — drops any copy forged inside
`args`/`tool_input` whenever that top-level field is absent, so a forged `args.agent_id` can
never fake ownership here either.

| `path` shape | own `agent_id` | Decision |
| --- | --- | --- |
| embeds `agent-<id>` | matches `<id>` | **ALLOW** — re-entering / switching into your own worktree |
| embeds `agent-<id>` | absent (orchestrator) or a *different* id | **BLOCK** — foreign worktree |
| does not embed `agent-<id>` at all | (any) | **ALLOW** — fail-open on an unfamiliar naming scheme, out of this guard's understood scope |
| no `path` (creating a fresh worktree via `name`) | (any) | **ALLOW** — untouched, always the caller's own |

The orchestrator is treated the same as a mismatched subagent: it never owns a dispatched
subagent's worktree either, so any orchestrator `EnterWorktree(path=".../agent-<id>")` call is
blocked just like a foreign subagent-to-subagent one would be.

## The correct alternative

From your **own** worktree, pull the other agent's branch into it instead of entering their
directory:

```bash
gh pr checkout <N> --branch <local-name>
# or, without a PR yet:
git checkout -b <local-name> <their-branch>
```

## No self-service bypass — external Telegram approval only

Same deny-by-default contract as the other subagent-facing gates in this catalog: no env-var
self-grant. For a genuine exception, ASK the human, or request one-time Telegram approval:

```bash
RIG_HATCH_REQUEST_ENTERWORKTREE_FOREIGN_GUARD="resuming my own earlier worktree under a new agent id after a session restart"
```

Set in the process environment before the tool call — `EnterWorktree` has no shell `command`
string whose leading `VAR=value` prefix a pre-worktree-enter hook could parse the way
`pin-primary-worktree` does for Bash (it takes a `path`/`name` field, not an invoked
executable line), so only the process-env source applies here, matching how the pre-write
hooks read this var. If unset, no Telegram call is made and the call simply blocks. If present
but blank/bare (`1`/`true`/`yes`), the hook denies without contacting Telegram.

## Registration gap (read before assuming this fires)

Mapping `EnterWorktree` → `pre-worktree-enter` in `lib/cc_hook_bridge/dispatch.py` is **not**,
on its own, enough for CC to ever invoke the bridge for an `EnterWorktree` tool call — Claude
Code only runs `PreToolUse` hooks it has an explicit **matcher** for in `settings.json`, and
matchers are written by **rig-cli**'s `hook_bridge_entries` (a separate repo), not by this
dispatcher. This is the identical two-repo split `pre-agent`/`pre-skill`/`pre-monitor` went
through. Until rig-cli's `EnterWorktree` matcher change ships **and** `rig apply` (or an
equivalent manual `settings.json` edit) runs on a given machine, this descriptor is installed
but inert — CC never calls the bridge for `EnterWorktree` at all, so
`enterworktree_foreign_guard.py` never runs.

## What's unaffected

- Creating a brand-new worktree (`EnterWorktree(name=...)`, no `path`) — always the caller's
  own, never gated.
- Re-entering / switching into a worktree the SAME agent (matching `agent_id`) already owns.
- Any `path` that does not follow the `.claude/worktrees/agent-<id>` naming convention at all
  (a manually created or differently-named worktree) — this guard understands exactly the one
  shape every confirmed incident used, and fails open rather than guess beyond that.
- `ExitWorktree` — already scoped to worktrees the CURRENT session's own `EnterWorktree` calls
  created, and a no-op otherwise, per its own tool contract.

## Known scope limits (heuristic, not a sandbox)

- This hook trusts CC's own `agent-<id>` naming convention for a subagent's worktree
  directory. If a worktree were created or renamed outside that convention (manually, or by a
  future harness version with a different naming scheme), this guard would not recognize it
  and fails open — it is a targeted fix for the confirmed incident shape, not a general
  ownership-tracking system.
- It does not (and cannot, from a stateless pre-tool hook) verify that the calling agent
  itself created the specific worktree matching its own `agent_id` — it trusts CC's own
  `agent_id` assignment as the source of truth for "whose worktree is this," the same trust
  boundary `subagent-no-bg-longproc`/`subagent-no-monitor` place in CC's `agent_id` forwarding.

## Fail-open, on purpose

`on_error: "open"`. A crash in this check must never wedge an agent's ability to call a
legitimate `EnterWorktree`. An unparseable event, a `path` that doesn't match the understood
naming convention, and an unset hatch env var all resolve to `allow`.

## Test

```bash
uv run --with pytest python -m pytest tests/test_enterworktree_foreign_guard.py -q
```

```bash
chmod +x enterworktree_foreign_guard.py

# a subagent enters a DIFFERENT agent's worktree → BLOCK
echo '{"args":{"path":"/repo/.claude/worktrees/agent-deadbeef01","agent_id":"sub-1"}}' \
  | ./enterworktree_foreign_guard.py
rc=$?; echo "exit=$rc"   # → "decision":"block" ...  exit=10

# a subagent re-enters its OWN worktree → allow
echo '{"args":{"path":"/repo/.claude/worktrees/agent-sub-1","agent_id":"sub-1"}}' \
  | ./enterworktree_foreign_guard.py
rc=$?; echo "exit=$rc"   # → "decision":"allow"  exit=0

# creating a brand-new worktree (name, no path) → allow
echo '{"args":{"name":"my-new-worktree"}}' | ./enterworktree_foreign_guard.py
rc=$?; echo "exit=$rc"   # → "decision":"allow"  exit=0
```
