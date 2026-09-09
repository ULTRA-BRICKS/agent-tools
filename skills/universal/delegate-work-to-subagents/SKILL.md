---
name: delegate-work-to-subagents
description: Use when handed any task beyond a trivial one-liner — the main thread is an orchestrator; delegate execution to subagents (the Agent tool) or a dynamic workflow, do not implement inline. Triggers on any multi-step coding, research, or repo mutation.
---

# Delegate work to subagents

The main thread is an **orchestrator**, not a worker. The moment a task is more than a
trivial one-liner — multi-step coding, a research pass, anything that mutates a repo —
it gets **planned, decomposed, dispatched to subagents (or a dynamic workflow), and
verified**. The orchestrator does not roll up its sleeves and do the work itself.

## What the orchestrator does

1. **Plan.** Turn the request into a small set of concrete units of work with a clear
   acceptance check each. Decide what can run in parallel and what must serialize.
2. **Decompose.** Split into independent units that don't share a working tree
   (separate worktrees / scratch dirs). Group by repo and by file ownership so two
   units never fight over the same files. When a unit will edit files, tell it explicitly
   to create its own worktree first, before its first Edit/Write — see `worktree-isolation`.
   This is a real requirement, not a nice-to-have: a repo's `worktree_only` agent-hook
   only catches a missing worktree reactively (after a blocked Edit/Write), so a
   subagent that isn't told up front may waste a turn recovering from the block, or
   worse, request an unnecessary bypass.
3. **Dispatch.** Hand each unit to a subagent (the `Agent` tool) or a dynamic workflow.
   Give it a sharp brief and require a **fixed handoff contract** back so you can act
   without re-investigating: branch/commit, changed files, tests run, review status,
   merge blockers, next action. (The `subagent-handoff-contract` skill, where installed,
   spells this out.)
4. **Verify.** Read what came back and check it against the acceptance criteria — don't
   rubber-stamp. If a subagent claims green, confirm the claim — a subagent's "tests
   pass" is usually its own file, not the full suite. Re-dispatch on a gap.

## What the orchestrator does NOT do

- Implement a feature, debug, refactor, or do a research deep-dive **inline**. That work
  belongs in a subagent with its own fresh context.
- Mutate a repo's working tree directly for anything non-trivial. Even a "quick fix"
  that grows two steps should have been a subagent.
- Declare a user-visible change "done" without a subagent (or itself, via
  `visual-proof-cycle`) capturing and looking at the rendered result.

## Trivial exceptions (do inline)

A genuine one-liner the orchestrator can finish in a single tool call without context
cost — reading one file to answer a question, a one-line edit, a single status command.
If you find yourself on step three of a "quick" thing, stop: it was a subagent.

## Why

- **Context hygiene.** Execution burns context — file dumps, build logs, dead ends. Keep
  that in the subagent's window so the orchestrator's stays clean and able to reason about
  the whole job. A polluted orchestrator loses the plot and starts contradicting itself.
- **Parallelism.** Independent units (no shared files, no real dependency) run at once
  across subagents instead of queuing behind each other on one thread.
- **The orchestrator stays in the loop.** Planning, dispatching, and verifying is exactly
  the altitude a coordinator should hold — it sees every unit's result and steers, instead
  of disappearing for an hour into one unit's weeds and losing oversight of the rest.

## Common failure

The orchestrator reads the task, thinks "I'll just do this one quickly," and starts
editing files. Three steps in, its context is full of one unit's detail, the other units
haven't started, and there's no clean handoff to verify against. The fix is structural:
plan and dispatch **first**, before touching the work — not after you're already in it.
