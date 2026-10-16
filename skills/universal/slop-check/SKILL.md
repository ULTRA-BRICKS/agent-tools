---
name: slop-check
description: Use before posting any public, external or owner-facing prose (English or Russian), such as issue and PR bodies, release notes, review comments, Telegram reports and decision requests. Run `slop-check` on the text and fix the machine-written tells it finds.
---

# Check prose for AI tells before it ships

Text an agent writes for a human (an issue, a PR body, a release note, a review comment, a
Telegram report) gets read by the owner and by outsiders. Stock phrases, flattery, hedging and
"all done" claims without evidence make it read machine-written and hide the facts. `slop-check`
(from `ultrabricks/slop-cli`) flags those tells locally, in English and Russian, before the
text leaves the machine.

This skill is about prose. Code-level slop in TypeScript is the separate Oxlint `anti-slop`
policy.

## When

Run it on the final draft of every:

- Forgejo/GitHub issue or PR body, release note, changelog entry meant for users;
- review comment or reply posted on someone else's work;
- Telegram report, decision request or status update to the owner.

Not needed for commit-message subjects, code comments or scratch notes nobody else reads.

## How

```sh
slop-check --context issue body.md       # issue text
slop-check --context pr pr-body.md       # PR body (adds commit-style rules)
printf '%s\n' "$report" | slop-check --context tg -   # stdin: no path or "-"
slop-check --context docs --format json notes.md   # machine-readable findings
```

- `--lang auto|en|ru` (default `auto`: Cyrillic paragraphs go to the Russian engine, the rest to
  Vale; mixed text runs both).
- `--ignore RULE_ID` (repeatable) for a rule you have a stated reason to skip in this text.
- `--laya` adds an advisory model score through an already-running classifier socket. It never
  changes the exit code; `laya: unavailable` is not an error.
- Code spans, fenced blocks and URLs are never flagged.

Each finding: `path:line:col rule_id message → suggestion`, then a score line.

## Acting on findings

- Rewrite with concrete facts: what changed, the command that proved it, the number, the link.
  `All tests pass` becomes the test command and its result; flattery and offers to help go.
- A finding may stay only with a stated reason (a quoted phrase, a term of art). Say why in the
  text or pass `--ignore RULE_ID` and name the reason in your report.
- Never reword only to get past a rule. A synonym that keeps the empty claim is still slop.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | clean |
| 1 | findings: fix them or justify each one |
| 2 | usage error, or an engine (Vale, aiw-ru) is missing or failed; for a missing engine the message names its install command |

## Install

- Via rig: clone `ultrabricks/slop-cli`, add it to `~/.config/rig/config.yaml`, then run
  `rig apply commit --only tools,skills` (a bare `rig apply` only previews). `tools` installs
  the CLI; `skills` copies this skill to `~/.agents/skills/slop-check/`:

  ```yaml
  tools:
    enabled: true
    items:
      slop-check: {repo: ~/dev/slop-cli, brew: [vale]}
  ```
- By hand: `brew install vale && uv tool install git+https://git.hyperide.ai/ultrabricks/slop-cli`
  (the Russian engine `aiw-ru` comes in as a Python dependency).

`command not found` means the tool is not installed: install it, do not skip the check. Exit 2
for a missing engine prints that engine's install command.

## Not the same as the tg dictionary gate

`tg` has its own gate: a list of words the owner banned (`~/.claude/DICT.json`). It is a
separate owner word list, enforced at send time. Passing `slop-check` does not mean the tg gate
passes, and the other way round.
