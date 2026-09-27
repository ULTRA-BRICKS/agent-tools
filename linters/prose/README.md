# Prose baseline

Settings for `rig lint text`, the check Rig runs on Markdown drafts (PR descriptions, issues,
replies, docs) before they are published. Rig reads this directory from the agent-tools
checkout; nothing here is copied into target repositories.

| File | Used by | What it controls |
| --- | --- | --- |
| `vale.ini` | Vale | Pinned style packages and the rules turned off as noise. |
| `languagetool.json` | LanguageTool | Language codes and rules disabled after Markdown stripping. |
| `dict/*.json` | Rig dictionary check | Extra banned-phrase rules in the `~/.claude/DICT.json` format. |
| `prompt.md` | `review just-ask` | The copy-editing prompt sent to the model seats. Rig fills in `{draft}` and `{findings}` in one pass and replaces the `DRAFT` marker word with a random one per run, so text inside a draft cannot close the frame. |

## Checks and what they catch

- **Vale** with four packages:
  - `ai-tells` (tbhb/vale-ai-tells): stock vocabulary ("delve", "seamless", "robust"),
    filler, hedging, consequence clauses, contrast by negation, sets of three, closing
    flourishes, em-dash and semicolon tics.
  - `proselint`: clichés, redundancy, jargon, hedging, typography.
  - `write-good`: passive voice, weasel words, wordy phrases, "There is" openers.
  - `Readability`: Flesch-Kincaid grade and Gunning Fog index for the whole document.
- **slopless** (npm): deterministic textlint rules for padded, vague or formulaic English
  prose. English only.
- **LanguageTool**: grammar, spelling and style for English and Russian. Runs offline through
  the Homebrew `languagetool` command.
- **Dictionary**: `~/.claude/DICT.json` (the banned-word list `tg` enforces) plus the packs in
  `dict/`. `dict/ru-plain.json` covers the Russian bureaucratic register: copula verbs, split
  predicates, filler prepositions, official-document demonstratives and set phrases. Each
  rule's `why` explains the ban in English.
- **Model review**: `review just-ask` with the prompt in `prompt.md`. Each seat returns
  findings and a revised draft. Rig checks that the revised draft keeps every code span,
  link and number of the original; other facts (wording of claims, table values, commands in
  prose) are left to the person reviewing the draft.

## Install the tools (macOS)

```bash
brew install vale languagetool
npm install -g slopless
```

`rig lint text` reports a missing tool as a skipped check and runs the others.

## Tuning

Turn off a noisy Vale rule with `<Style>.<Rule> = NO` in `vale.ini`, and a LanguageTool rule by
adding its id to `languagetool.json`. Add a phrase ban as a rule in `dict/` with `hit` and
`miss` examples; `tests/test_prose_linter_carrier.py` checks every example.
