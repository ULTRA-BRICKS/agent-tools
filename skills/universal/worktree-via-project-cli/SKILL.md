---
name: worktree-via-project-cli
description: Use when creating a git worktree (or a fresh checkout) for a repo that ships its own worktree/bootstrap command — call the project's CLI instead of raw `git worktree add`, so the new tree gets its dependencies provisioned and doesn't fail pre-commit, tests, or the toolchain later with missing-dep errors.
---

# Create worktrees through the project's CLI, not raw git

## Overview

Many repos wrap worktree creation in their own command (e.g. `<project> worktree
create <branch>`) that does more than `git worktree add`: it **provisions the new
checkout's dependencies** (installs dev deps, syncs the venv, wires the env) and often
verifies the result. A raw `git worktree add` gives you a tree with only whatever is
already vendored — typically runtime deps only — and the gap surfaces *later* as a
pre-commit, test, or typecheck failure that looks like a code problem but is really a
missing-environment problem.

## Rule

Before reaching for `git worktree add`, check whether the repo provides its own
worktree/bootstrap command — look in `README`, `AGENTS.md`/`CLAUDE.md`, or
`<cli> --help`. Docs go stale, so **confirm the command actually exists** (`<cli> --help`)
before relying on it. If it's there, use it; then confirm deps landed — run its
`doctor`/verify step, or if it has none, run the dev gate (lint + test + typecheck) once:

```bash
<cli> --help | grep worktree                       # confirm the command exists first
<project> worktree create <branch> --base main      # provisions deps in the new tree
cd <the path it prints>
<project> worktree doctor . || <project> test       # verify deps/env are complete
```

Drop to raw `git worktree add` **only** when you are repairing the project's own
worktree command itself (you can't bootstrap with the thing you're fixing). If the
command exists but errors at create time, fix it (or report the blocker) rather than
silently falling back to a raw, under-provisioned worktree.

**JS dependencies in a new worktree:** install them *in* the worktree with the repo's own
package manager — for a bun repo (`bun.lock`), `bun install --frozen-lockfile`, which links into
the shared global store in seconds unless the repo's own `bunfig.toml` asks for a local layout.
Never symlink `node_modules` to another checkout (`bun install` writes through the link and
corrupts it), never `npm install`/`npm ci` in a bun repo (a full real copy), and never clear a
package-manager cache or store to make room — reclaim disk by removing stale worktrees.
`package-store-guard` blocks all three.

## Why

The failure is silent and delayed: `git worktree add` succeeds, the code looks fine,
and only when pre-commit / the test runner / the typechecker reaches for a dev-only
dependency does it blow up — deep into the work, far from the cause. The project's
worktree command exists precisely to provision those deps at creation time. Using it
removes a whole class of "why is this dependency missing only in the worktree" confusion.

Distinct from `worktree-base-trap` (the new tree is on the wrong *base ref*) and
`worktree-isolation` (each parallel agent needs its *own* tree). Pairs with both —
verify the base, provision via the project CLI, isolate per agent.
