---
name: repair-red-main
description: >-
  Use when main (or a release branch) is red, or a deploy is stuck, held or
  failing, and no repair is in flight. Start the repair immediately; never ask
  the owner whether to. Escalate only for money, irreversible data deletion, or
  credentials only the owner can issue.
---

# Red main / stuck deploy: start the repair, do not ask

If `main` (or a release branch) is red, or a deploy is stuck, held or failing,
and **no repair is in flight**, start the repair **now**. Do not ask the owner
"should I fix it?", "may I start?" or "which option?". The answer is always
yes: waiting for a go-ahead only leaves the branch red longer.

## Rules

1. **Check for an in-flight repair first** (an open PR, ticket or agent touching
   the failure). If one is active, help or wait; do not duplicate. If it shows no
   commits or activity for 2 hours, take it over. If none exists, you own the
   repair from this moment.
2. **Dispatch the repair to a subagent** if you are the orchestrator (see
   `skill://delegate-work-to-subagents`):
   file or reuse the ticket, worktree, TDD red-first, review, PR, `gh ship`. A
   red gate is a reason to fix the gate or the work, never to bypass a guard.
3. **If the gate that blocks the fix is itself the broken part**, request the
   hatch that guard documents (the inline `RIG_HATCH_REQUEST_*=…` prefix). The
   approval is a guard step, not a question about whether to repair. A declined
   or unreachable hatch is a deny: never self-grant a bypass.
4. **Stuck deploys are the same**: find the cause, unstick or re-run, and verify
   the deployed digest equals the merge SHA and the service is healthy.
5. **Decide safe options yourself** (`review brainstorm` / `review just-ask`).
   Ask the owner only for money, irreversible data deletion, or credentials
   only they can issue.
6. **Report after the fix is verified** (`tg report`: how it was fixed, proof,
   when any remainder lands), not as a question before it.

## Example

a repo's `main` fails its `test` job since the last merge and no PR is open for it. Do
not send "main is red, shall I fix?". Open the ticket, dispatch a worktree
agent, red test first, fix, review, ship, then report with the green run link.

## Why

Asking about an obviously required repair costs the owner attention and leaves
the repository red for hours. Autonomous agents exist to remove that wait.
