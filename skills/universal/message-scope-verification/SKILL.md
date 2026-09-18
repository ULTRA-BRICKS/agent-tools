---
name: message-scope-verification
description: 'Use when a NEW inbound message arrives — a chat reply, an injected "[TG from ...]"/"[from ...]" wrapper, a pasted tmux/pane message — while your work is unfinished (uncommitted changes, an open ticket). Check if it continues your work or is from an unrelated project/topic; if misdirected, confirm with the user first.'
---

# Confirm a message is actually yours before you drop unfinished work for it

A new inbound message feels like the highest-priority thing in the room, so the reflex is
to treat it as a new instruction and act on it immediately — including abandoning whatever
you were mid-way through. That reflex is wrong when the message wasn't addressed to *you*
at all: multi-pane / multi-agent setups (several tmux panes, several harness sessions, a
phone that fans a message out to more than one place) make it easy for a message meant for
a different task or a different agent to land in your input. If you silently switch on it,
you abandon real unfinished work on a guess, and the user has to notice, correct you, and
re-explain — after the damage (a dropped branch, a half-done task) is already done.

## Trigger

Run this check whenever BOTH are true:

1. **Your current work is unfinished** — uncommitted changes, an open/active ticket, a
   running build or background task, a question you're still waiting on an answer for.
2. **The new message looks thematically unrelated** to that work — it names a different
   project, repo, ticket/task ID, or topic that has not come up anywhere in your current
   context, or it assumes shared context you don't have (a decision, a screenshot, a
   summary you never produced).

If either is false — your work is already done/committed, or the message is clearly about
what you're doing — this skill has nothing to add; proceed normally.

## Signals the message may be misdirected

- Names a different repo/project than the one you're currently in.
- References a ticket/task ID or number that doesn't match anything in this thread.
- Assumes context you don't have ("as we discussed", "the fix you sent" — for something
  that never happened in *this* session).
- Reads as a reply to a question you never asked, or an update on work you never started.
- Arrives on a channel/pane that, per project memory or prior pattern, sometimes misroutes
  (e.g. a shared Telegram thread fanning a message out to several agent panes at once).

## Signals it's clearly on-topic — don't ask, just proceed

- A short reply to a question you just asked ("yes", "go ahead", a number, a bare
  correction).
- Names the same file, ticket, PR, or branch you're already touching.
- A natural next step in the thread you're already in.

Don't over-apply this: asking about every inbound message, including ones that are
obviously continuations, defeats the point and is just as annoying as never asking.

## The rule

When both trigger conditions hold, do **not** silently drop your current work and switch.
Ask the user explicitly, e.g.:

> "This message looks like it might be for a different task or session — is it for me
> (currently working on **\<X\>**), or was it meant elsewhere? Let me know before I switch,
> so I don't drop unfinished work by mistake."

Then **wait for an explicit answer** before abandoning or switching context. Silence is not
confirmation — if the user hasn't replied, keep working on what you had, or park the new
message and say you're holding it pending confirmation. Don't treat "no objection yet" as
a green light to switch.

If the user confirms the message is for you, switch normally (and, where the
`monorepo/parallelize-independent` skill is installed, decide whether the new item can run
alongside your current work or genuinely needs it paused first). If they say it was
misdirected, ignore it and continue your current work — and let them know you're
continuing, so they can redirect it properly.

## Common mistakes

- **Silently switching on a new message by default.** Treating "new message = new top
  priority" without checking whether it was even meant for you — this is the exact failure
  this skill exists to prevent.
- **Over-asking.** Interrogating the user about a message that obviously continues the
  current thread. If nothing in it is unrelated, don't ask.
- **Treating silence as consent.** Proceeding to switch because the user didn't object
  within some window — silence confirms nothing; wait for an explicit answer (see
  `decision-request-discipline` for how to ask a question the user can answer fast).
- **Asking vaguely.** "Is this for me?" with no mention of what you're currently doing
  gives the user nothing to check against. Name the current task in the question.

## Why

Multi-agent, multi-pane setups make misdirected messages a recurring, not hypothetical,
failure mode: a message about one task can land in a session running a completely
different one, and an agent that reacts to every inbound message as gospel will drop
real in-progress work on a bad guess. The fix costs one short question and, once
confirmed, zero further friction — that's a good trade against silently abandoning
unfinished work and forcing the user to catch and correct it after the fact.

Pairs with `monorepo/parallelize-independent` (once scope is confirmed, decide serial vs.
parallel — where that by-type skill is installed), `decision-request-discipline` (the
decision-request format for anything you escalate), and `learn-from-feedback` (this skill is
itself the durable fix for a misdirected-message incident, not just a one-off apology).
