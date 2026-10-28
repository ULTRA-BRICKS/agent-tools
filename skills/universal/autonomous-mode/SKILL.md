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
**require_extra_approval_for_unattributed_changes is OFF**. Do not turn it on. A
`rig.yaml` is why this skill loads — it is not, by itself, autonomous mode.

## Rules

- Do **not** wait for a human Approve on agent-authored PRs when the ship gates
  are green. Ship.
- Do **not** invent a "gate-behavior hold for a human" class (including a
  "hold for Alex"). That class does not exist.
- Autonomous agents send key status via `tg --tag report`, not chat-only.
- If `main` (or a release branch) is red, or a deploy is stuck, and no repair
  is in flight, **start the repair immediately; never ask the owner whether
  to**. Details and the escalation limits: `skill://repair-red-main`.
- If extra-approval is wrongly ON, **fix that knob** —
  `github.ruleset.require_extra_approval_for_unattributed_changes`, mode-gated
  by [rig-cli#404](https://git.hyperide.ai/ultrabricks/rig-cli/pull/404). A red test
  is not a broken gate: fix the work. Do not stall, and do not weaken a
  legitimate check.

## Example

An agent-authored PR is green. Merge with `gh ship`. Do not open a Telegram
"please Approve" and wait.

## Why

Autonomous mode exists so agents ship finished work. Extra-approval is a GitHub
ruleset knob; in autonomous, rig defaults it off. Waiting for a human Approve
recreates the babysitting autonomous mode was meant to remove.
