---
name: deferred-findings-tracking
description: Use when you notice a bug, gap, or improvement that you're deliberately not fixing right now. Create a tracking issue immediately — never silently drop the finding or leave it only in a chat message.
---

# Deferred findings become tracked issues, immediately

In the middle of a task you'll spot things you shouldn't fix right now — an unrelated
bug, a follow-up, a risky refactor that's out of scope. The right move is to *defer*
it, not to *forget* it. A finding that lives only in your head or in a chat scrollback
is a finding that's gone the moment the session ends.

## Rule

The instant you decide "not now", create a durable tracking record — an issue, a
ticket, a TODO in the project's tracker — with:

- **What** the finding is, concretely (not "improve error handling" but "the upload
  handler swallows network errors and returns 200; user sees a silent failure").
- **Why** it matters / the impact.
- **Where** it lives (file, function) so the next person can find it.

Then continue the original task. Don't inline-fix it (that's scope creep — see
`smallest-change`), and don't leave it as a floating `// TODO` with no owner (see
`comment-hygiene`).

## Why

This is the same principle as `promise-durable-action`: an intention without a
mechanism evaporates. A tracked issue survives the context reset, is visible to the
team, and can be prioritized. "I'll remember to come back to it" is how real bugs ship
to production — the deferral was correct, the lack of a record was the mistake.

## Bugs in tools

Every bug you hit in a tool you use gets a ticket in **that tool's own tracker**, right
away — not in the project you happened to be working on, and not "later". This applies
most of all to our own tools and the rig ecosystem: `rig`, `task-cli`, `tg-cli`,
`review-cli`, the agent-tools hooks and `gh ship`, `dev-cli`, `smoo`/`nx-plugin`,
`mem-sentinel`. A tool bug you don't report gets hit again by every later session.

- **Dedupe first.** Search the tool's tracker (`task find "<symptom>"` in the tool's repo).
  If a ticket already exists, comment on it with your new evidence (version, command,
  output) instead of opening a duplicate.
- **Make it reproducible.** The ticket carries the exact command / repro, the expected
  behavior vs the actual output, the tool version, and the workaround you used. Redact
  secrets first — tokens, auth headers, env values and credential paths never go into a
  ticket.
- **A workaround never replaces the ticket.** Working around the bug to finish your task
  is fine; file the ticket first, then continue with the workaround.
- **The broken tool is the tracker client.** If `task` itself fails, do not route around
  its gates with a raw tracker write. After any failure that is not a plain policy-gate
  refusal (exit 9, or a backend error on exit 2: the create may have landed), search for it
  by title (`task list`, `task find "<title>"`) before you
  retry the create. If `task` keeps failing, queue the report durably (see
  `queued-report-durability`), say in your report that it is queued, and file the
  `task-cli` failure itself as a bug once `task` works again.

## Common mistakes

- **Worked around a tool bug and moved on.** You hit a broken `rig`/`task`/`gh ship`
  behavior, found a workaround, and finished the task — with no ticket. The next agent
  burns the same time rediscovering it. File it in the tool's tracker (repro, expected vs
  actual, workaround) before you continue.
- **Left the finding only in chat.** A note in a message or a final report is not a
  tracking record; open the issue.
