---
name: worktree-isolation
description: Use when running multiple agents or parallel tasks against the same repo, AND whenever a single subagent is about to Edit/Write in a repo whose committed rig.yaml sets agent_hooks.worktree_only — create the worktree before your first Edit/Write, before the reactive hook would otherwise catch you.
---

# Worktree isolation for parallel work

Two agents (or two parallel tasks) sharing one working tree will trample each other:
one's edits, branch switches, and uncommitted changes corrupt the other's view. Give each
its own **git worktree** so they have independent working trees over the same repo history.

**This also applies to a single, solo subagent.** Before your first Edit/Write in any repo,
check that repo's committed `rig.yaml` for `agent_hooks.worktree_only: true` (rig-provisioned
repos use this). If it's set, create your worktree before editing anything — don't wait to be
blocked by the `worktree-only-writes` hook and figure it out reactively from the error. A
read-only session (answering a question, running a review) doesn't need one — the requirement
is scoped to writing, not to every session.

Prefer, in this order: `rig worktree create <name>` where `rig` is available (the validated,
canonical convention — `<repo>/.worktrees/<name>`, auto-registered in `.git/info/exclude`);
Claude Code's `EnterWorktree` tool; otherwise a raw `git worktree add` per the pattern below.
`commandcode` has its own native equivalent — a `-w`/`--worktree` launch flag, an in-session
`/worktree` slash command, and model-driven `enter_worktree`/`exit_worktree` tools — though it
manages worktrees outside the repo, under `~/.commandcode/worktrees/`, not this convention's
`<repo>/.worktrees/<name>`. `pi` has no native worktree tooling of its own; it relies entirely
on `rig worktree create` or a raw `git worktree add`.

## Pattern

Preferred — `rig worktree create`/`rig worktree remove` (idempotently ignores `.worktrees/`
in `.git/info/exclude`, never touches the committed `.gitignore`, and `remove` also deletes
the branch `create` made, so a same-name retry doesn't collide):

```bash
run_id="$(date +%s)-$$"
rig worktree create "agent-$run_id"   # -> <repo>/.worktrees/agent-$run_id, branch agent-$run_id
trap 'rig worktree remove "agent-$run_id" 2>/dev/null' EXIT INT TERM
cd ".worktrees/agent-$run_id"

# ... do the work here — fully isolated from other runs ...
```

Fallback — only when `rig` isn't available on that machine/repo, a raw `git worktree add`
with the same signal-safe cleanup:

```bash
# Each parallel run gets a uniquely-named, isolated worktree.
run_id="$(date +%s)-$$"
worktree="../.worktrees/agent-$run_id"
git worktree add "$worktree" -b "work/$run_id"

# ... do the work in $worktree — fully isolated from other runs ...

# Clean up on exit — INCLUDING on signal, so a killed run doesn't leak worktrees.
cleanup() { git worktree remove --force "$worktree" 2>/dev/null; }
trap cleanup EXIT INT TERM
```

Key points:

- **Unique name per run** (a run id), so concurrent runs never pick the same path.
- **Separate scratch/output dirs** too — if the tasks write build output or temp files,
  those must also be per-run, not a shared dir that two runs clobber.
- **Clean up on exit, including signals.** Register the cleanup on `EXIT`/`INT`/`TERM` so
  an interrupted or killed run still removes its worktree. Leaked worktrees accumulate and
  eventually confuse `git worktree list` and waste disk.

## Why

A worktree is the right isolation primitive: cheaper than a full clone (shared object
store, shared history) but with a fully independent working tree and HEAD — exactly what
parallel agents need so their edits and branch state don't interfere. The signal-safe
cleanup is the part people forget; without it, every crashed run leaves a stale worktree
behind. Pairs with `universal/worktree-base-trap` (verify each new worktree's base) and
`monorepo/squash-merge-cleanup` (tearing them down after merge).
