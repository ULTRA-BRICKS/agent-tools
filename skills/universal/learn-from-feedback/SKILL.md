---
name: learn-from-feedback
description: 'Use the moment a user complains, corrects you, or expresses frustration ("again", "third time", "I keep telling you", "every single time"), or you notice yourself repeating a past process mistake. Triggers on any rebuke, "you always/never", repeated rework on the same point, or a self-noticed recurring failure.'
---

# Extract the lesson from a complaint, don't just apologize and move on

A user complaint or correction — and a self-noticed *repeated* mistake — is the single
highest-signal feedback you get. The reflex is to fix the one instance, apologize, and
move on. That fixes the symptom and guarantees the same failure next session, because the
*lesson* lived only in this chat and dies at the context reset.

The instance fix is necessary but **not sufficient**. Every complaint or repeated failure
must also produce a durable lesson.

## Trigger

Run this skill whenever ANY of these is true:

- The user complains, corrects, or pushes back — explicitly ("you keep…", "again", "third
  time", "I told you", "stop", "why didn't you") or with visible frustration.
- The user re-asks for the same thing you already should have done (silent correction).
- You notice you're doing something you've been told not to, or repeating a mistake you've
  hit before — even if nobody complained this time.

A single new mistake under genuine novelty may be a one-off. A complaint, a "you always/
never", or a **second** occurrence of the same class of failure is a pattern — treat it as
one.

## The three mandatory steps (all three, every time)

1. **Name the root cause — the general one, not the surface instance.** Not "I force-pushed
   over their branch"; that's the symptom. The root cause is the class: "I run destructive
   git operations without checking state first." Ask *why* until you reach a cause that
   would prevent a whole family of failures, not just this one.

2. **Extract the generalizable lesson.** State the rule that, if followed, prevents the
   whole class — phrased so it applies to future tasks you can't foresee, not just a replay
   of this one. "Check `git status`/`git log` before any force-push, reset, or rebase" beats
   "don't force-push this branch."

3. **Record it durably — pick the right home, and write it now.** A lesson that lives only
   in this chat is already lost. One of:
   - A **MEMORY.md memory file** — for a behavioral lesson about how *you* should work
     (the default home for "I keep doing X; do Y instead"). Add an entry and link it from
     the index.
   - A **ROADMAP / ticket entry** — for a lesson that implies a concrete fix, a tooling
     gap, or follow-up work someone must do.
   - **Escalate to a hook / rule / config** — when the lesson is enforceable mechanically
     (a `git status` check, a function-length guard, a forbidden flag). A guard that *can't*
     be forgotten beats a note that can. This is the `promise-durable-action` path: if you
     say "I'll be more careful," that's the empty-promise smell — turn it into a mechanism
     in this same turn or it isn't real.

Then **confirm the write landed** (read the file back, or show the commit) — the durable
record has the same "did it actually happen" trap as the original failure.

## Quick reference

| Step | Bad (symptom only) | Good (lesson) |
|---|---|---|
| Root cause | "I overwrote their branch" | "I run destructive git ops without checking state" |
| Lesson | "Don't touch that branch" | "Always `git status`/`log` before force-push/reset/rebase" |
| Record | "I'll be more careful" (chat only) | MEMORY.md entry + (if mechanizable) a pre-push hook |

## Common mistakes

- **Apologize-and-proceed.** A graceful apology with no durable artifact is the failure
  this skill exists to stop. The user's annoyance is *because* you keep relearning the same
  thing — close the loop.
- **Fix the instance, skip the class.** Recovering the wiped commits is required, but if
  that's all you do, you force-push over the next branch too. Patch the symptom AND record
  the lesson.
- **Verbal promise, no mechanism.** "Going forward I'll check first" with nothing written
  is empty (see `promise-durable-action`). Make the durable record in the same turn.
- **Wrong home.** A behavioral how-I-work lesson buried in a ticket nobody reads, or a
  concrete tooling fix dropped into a vague memory note. Match the lesson to its home.
- **Recording the apology instead of the rule.** "I was wrong and feel bad" is not a
  lesson; the next agent can't act on it. Record the *rule*, generalized and actionable.

## Why

Complaints are expensive feedback the user already paid for by being annoyed. If the lesson
isn't captured durably, they pay again next time — and the relationship erodes on "I keep
telling you the same thing." The whole value of feedback is that it should only have to be
given once; that only holds if each complaint leaves a durable artifact that survives the
context reset.

Pairs with `promise-durable-action` (a "I'll do better" with no mechanism is empty — turn
the lesson into a file edit, hook, or tracked task now) and `task-completion-selfcheck`
(before reporting the fix done, confirm you also recorded the lesson, not just patched the
instance).
