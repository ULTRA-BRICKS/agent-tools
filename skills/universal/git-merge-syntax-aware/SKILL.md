---
name: git-merge-syntax-aware
description: 'Use when a git merge/rebase/cherry-pick conflict is purely structural — reordered imports/methods, formatting-only, moved code — in TS/JS/JSON/PY/RS/GO/YAML/TOML and more. The mergiraf tree-sitter driver merges the AST instead of lines. Covers wiring it as the driver, manual resolution, and that you must still typecheck after.'
---

# Syntax-aware git merge with a tree-sitter driver

Line-based git conflicts on changes that aren't actually in conflict: two branches
reorder the same imports, reformat the same block, or move a method — git sees
overlapping lines and stops. A tree-sitter merge driver (`mergiraf`,
<https://mergiraf.org>) merges the **AST** instead of the text, so it resolves these
structural collisions automatically and only surfaces conflicts that are genuinely
semantic. It supports TS/TSX/JS/JSX/JSON/PY/RS/GO/YAML/TOML and more
(`mergiraf languages`).

## Wire it as the merge driver (once, globally)

Configure the driver and map file types to it, then every `git merge` / `git rebase` /
`git cherry-pick` uses it automatically for those types — no per-command flag:

```ini
# ~/.gitconfig
[merge "mergiraf"]
    name = mergiraf syntax-aware merge
    driver = mergiraf merge --git %O %A %B -p %P -l %L
[core]
    attributesFile = ~/.config/git/attributes
```

```gitattributes
# ~/.config/git/attributes
*.ts  merge=mergiraf
*.tsx merge=mergiraf
*.js  merge=mergiraf
*.json merge=mergiraf
*.py  merge=mergiraf
# …one line per supported extension
```

## Manual use — one conflicted file, or an ad-hoc 3-way merge

```sh
mergiraf solve path/to/conflicted_file.ts   # resolve markers in-place on a file git left conflicted
mergiraf merge BASE LEFT RIGHT -p out.ts    # ad-hoc 3-way merge from three inputs
mergiraf review <merge>                      # diff mergiraf's resolution vs the line-based one
```

## Caveats — always typecheck after

- It **falls back to git's line-based algorithm** on a parse error or a timeout, so a
  bad-syntax file silently behaves as before.
- It resolves *structural* conflicts safely but can still leave **genuine semantic
  conflicts** for you to settle by hand.
- **No cross-file refactors.** It merges one file's AST in isolation, so a symbol
  moved, renamed, or extracted into another module is invisible to it — those conflicts
  fall back to line-based git and need manual resolution.
- **AST-merge is not semantic correctness.** Always run the typechecker / build after a
  driver-assisted merge. A structurally valid merge can still be logically wrong.

Use the tool's bug-report path (`mergiraf report`) on a bad merge so the resolver
improves.

## Why

Most "conflicts" in a fast-moving repo are formatting churn and reordering, not real
disagreements — exactly the class a line-based merge can't tell apart from a real
collision. An AST-aware driver removes that whole category of busywork while leaving the
genuinely conflicting changes for a human, and the global wiring means it just happens
on every merge without anyone remembering a flag.
