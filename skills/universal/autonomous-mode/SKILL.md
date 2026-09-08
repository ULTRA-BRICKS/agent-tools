---
name: autonomous-mode
description: >-
  Use whenever the repo has a rig.yaml, harness mode is autonomous, or the user
  says autonomous. In autonomous mode, extra-approval for unattributed changes
  is OFF; do not turn it on; do not wait for human Approve on agent-authored
  PRs when gates are green; do not invent a hold-for-human class.
---

# Autonomous mode: extra-approval OFF, ship when gates pass

In **autonomous** mode (harness mode autonomous, or the user says autonomous):
**extra-approval-for-unattributed-changes is OFF**. Do not turn it on. A
`rig.yaml` is why this skill loads — it is not, by itself, autonomous mode.

## Rules

- Do **not** wait for a human Approve on agent-authored PRs when the ship gates
  are green. Ship.
- Do **not** invent a "gate-behavior hold for a human" class (including a
  "hold for Alex"). That class does not exist.
- Autonomous agents send key status via `tg --tag report`, not chat-only.
- If extra-approval (or another GitHub/ship **knob**) is wrongly ON, **fix that
  knob** — [rig-cli#404](https://github.com/alex-mextner/rig-cli/pull/404)
  manages it. A red test is not a broken gate: fix the work. Do not stall
  waiting for a human, and do not weaken a legitimate check.

This skill is not a dump of every ship rule. It is the extra-approval /
hold-for-human exception for autonomous mode.

## Example

An agent-authored PR is green. Merge with `gh ship`. Do not open a Telegram
"please Approve" and wait.

## Why

Autonomous mode exists so agents ship finished work. Extra-approval for
unattributed changes is a GitHub ruleset that blocks merges from unsigned or
unattributed commits; in autonomous it is off on purpose. Waiting for a human
Approve recreates the babysitting autonomous mode was meant to remove.
