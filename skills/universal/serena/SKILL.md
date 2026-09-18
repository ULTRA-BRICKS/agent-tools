---
name: serena
description: 'Use as an OPT-IN exception to grep for LSP-precise navigation/edits in a sizeable codebase — "where is symbol X defined/who calls it/rename it repo-wide", or edit by symbol not line. Serena is an LSP-backed code-intelligence MCP. NOT a memory system — memory lives in agent-tools MEMORY.md.'
---

# Serena — LSP-backed code intelligence (opt-in)

Serena is a language-server-backed MCP that turns the editor's "go to definition / find
references / rename symbol" power into agent tools. It answers structural questions and makes
structural edits **by symbol**, not by reading and rewriting whole files — so it's both cheaper in
context and more correct than grep + full-file read + string replace.

It is an **opt-in exception to grep**, not a replacement. Grep stays the default fast path; reach
for Serena for the cases below where the language server beats a string match (find-references,
rename-across-repo, symbol overview). This is the routing the `tool-routing` doctrine
(`docs/specs/2026-06-15-thirdparty-tool-triage.md` §5.1) names for it.

## When to reach for it

**Navigate / understand (read):**
- `get_symbols_overview` — the symbols a file defines (signatures + members) without dumping
  the whole file into context. The first thing to run on an unfamiliar file.
- `find_symbol` — jump straight to a definition by name/path (no grep-then-scan).
- `find_referencing_symbols` / `find_implementations` — every caller / every impl, including the
  ones grep misses (re-exports, aliased imports, typed-value method calls, interface impls).
- `get_diagnostics_for_file` — the language server's own errors/warnings for a file.

**Edit (structural, by symbol — safer than line/string edits):**
- `replace_symbol_body`, `insert_after_symbol` / `insert_before_symbol`, `rename_symbol`,
  `safe_delete_symbol` — edit a function/class/method as a unit. `rename_symbol` updates every
  reference; a string replace would miss some and clobber look-alikes.

## Memory — use MEMORY.md, NOT Serena's store

Serena ships `write_memory` / `read_memory` / `onboarding`, but **do not route durable agent
knowledge there.** The single source of agent memory is **agent-tools `MEMORY.md`**
(`~/.claude/projects/<proj>/memory/`); Serena's `.serena/memories/` is an onboarding artifact, not
a memory system — `docs/specs/2026-06-15-thirdparty-tool-triage.md` §4.1/§5.5/§6 declares this
explicitly. Record lessons, gotchas, and where-things-live in `MEMORY.md`.

`.serena/project.yml` (the project config) is still committed so a fresh checkout activates Serena
without re-onboarding; its `.serena/.gitignore` excludes the volatile `cache/` and the local-only
`project.local.yml`. That is config, not the durable-memory path.

## When NOT to

- A **literal string** hunt (an error message, a magic constant, a config key) → plain grep.
- A language/repo serena can't index → grep + read.
- A tiny one-file change you're already looking at → just edit it.

## Why

**Context cost:** a symbol overview returns one signature, not 2000 lines you'll regret loading.
**Correctness:** references/rename understand the language's binding rules, so they catch the
call grep's string match overlooks and skip the comment it falsely hits. This pairs with
`semantic-code-search` (the general "prefer an index over grep" rule) and
`dead-code-investigation` (a references query is stronger evidence than grep before you call
something unused).
