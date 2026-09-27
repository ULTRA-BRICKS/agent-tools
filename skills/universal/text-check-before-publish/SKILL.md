---
name: text-check-before-publish
description: Use before publishing any text other people will read (a PR description, issue, discussion post, review reply, reply to a maintainer, README or doc page), and before handing code for review. Run `rig lint text <draft.md>` on the draft and `rig lint code` on the change, fix what they find without changing facts, and only then publish.
---

# Check text and code before anyone else reads them

Filler, stock phrases, hedging and marketing tone cost the reader time and cost the author
credibility, most of all with outside maintainers. Linters catch the mechanical part; a
copy-edit pass catches the rest. Run both before the text leaves your machine.

## Text

1. Write the draft to a Markdown file outside the repository, in a scratch directory
   (`$TMPDIR/pr-body.md`), so neither the draft nor its outputs can end up in a commit.
2. Run `rig lint text "$TMPDIR/pr-body.md"`. It writes two files next to the draft:
   - `pr-body.report.md`: each check, what it looks for, and its findings (Vale styles,
     slopless, LanguageTool, the phrase dictionaries, two model copy-edits, and a guard that
     the revised draft kept every code span, link and number);
   - `pr-body.checked.md`: the revised draft.
3. Read the report. Take the revised draft or edit by hand; linters produce false positives,
   and a finding you reject needs no action.
4. Never change facts to satisfy a check: numbers, identifiers, commands, test results and
   claims stay as they were verified. If the fact guard lists a missing token, restore it.
   The guard only compares code spans, links and numbers; diff the revised draft against the
   original for everything else (a changed claim, table value or command written in prose).
5. Publish only the checked text. When the user reviews texts before publishing, hand them
   the checked draft and the report together.

## Code

Run `rig lint code` in the repository before review. It runs the repository's Oxlint
configuration, including the anti-slop rules Rig provisions (`linters.rules.groups.anti-slop`).
Fix findings in the code; do not weaken the rule or add casts to silence it.

## Why

Deterministic linters are fast and repeatable but only see patterns; the model copy-edit sees
meaning but can drift from the facts. Running both, with a fact guard and a diff between
them, gives a tighter text without new claims.
