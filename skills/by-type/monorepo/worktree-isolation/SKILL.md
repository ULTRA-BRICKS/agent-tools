---
name: worktree-isolation
description: Use when running multiple agents or parallel tasks against the same repo, AND whenever a single subagent is about to Edit/Write in a repo whose committed rig.yaml sets agent_hooks.worktree_only — create the worktree before your first Edit/Write, before the reactive hook would otherwise catch you.
---

# Worktree isolation for parallel work

Two agents (or two parallel tasks) sharing one working tree will trample each other:
one's edits, branch switches, and uncommitted changes corrupt the other's view. Give each
its own **git worktree** so they have independent working trees over the same repo history.

**This also applies to a single, solo subagent.** Before your first Edit/Write in any repo,
check that repo's committed `rig.yaml` for `agent_hooks.worktree_only: true` (rig-provisioned
repos use this). If it's set, create your worktree before editing anything — don't wait to be
blocked by the `worktree-only-writes` hook and figure it out reactively from the error. A
read-only session (answering a question, running a review) doesn't need one — the requirement
is scoped to writing, not to every session.

Prefer `rig worktree create <name>` when `rig` is available and manages the repository
(the mandatory creation door — it uses the canonical `<repo>/.worktrees/<name>` path and
registers it in `.git/info/exclude`). When `rig` is unavailable, use the raw
`git worktree add` fallback below, targeting the repository-local `.worktrees/<name>` path.

## Fallback pattern (rig unavailable)

```bash
# Each parallel run gets a uniquely-named, isolated worktree.
run_id="$(date +%s)-$$"
name="agent-$run_id"
git_common_dir="$(git -C "$PWD" rev-parse --git-common-dir)"
case "$git_common_dir" in
  /*) ;;
  *) git_common_dir="$PWD/$git_common_dir" ;;
esac
if ! resolved_git_common_dir="$(cd "$git_common_dir" 2>/dev/null && pwd -P)"; then
  printf 'could not resolve the shared git directory: %s\n' "$git_common_dir" >&2
  exit 1
fi
git_common_dir="$resolved_git_common_dir"
if [ "$(basename "$git_common_dir")" != ".git" ]; then
  printf 'expected the shared git directory to end in .git: %s\n' "$git_common_dir" >&2
  exit 1
fi
repo_root="$(dirname "$git_common_dir")"
worktree_root="$repo_root/.worktrees"
worktree="$worktree_root/$name"
if [ -L "$worktree_root" ] || { [ -e "$worktree_root" ] && [ ! -d "$worktree_root" ]; }; then
  printf 'refusing non-directory .worktrees path: %s\n' "$worktree_root" >&2
  exit 1
fi
if ! mkdir -p "$worktree_root"; then
  printf 'could not create canonical worktree directory: %s\n' "$worktree_root" >&2
  exit 1
fi
if [ -e "$worktree" ] || [ -L "$worktree" ]; then
  printf 'refusing existing worktree target: %s\n' "$worktree" >&2
  exit 1
fi

# Register the canonical directory in this clone's shared exclude file, idempotently.
exclude_file="$git_common_dir/info/exclude"
if ! grep -Fqx '/.worktrees/' "$exclude_file" 2>/dev/null; then
  if [ -s "$exclude_file" ]; then
    if ! last_byte="$(tail -c 1 "$exclude_file" 2>/dev/null)"; then
      printf 'could not read git exclude: %s\n' "$exclude_file" >&2
      exit 1
    fi
    if [ -n "$last_byte" ] && ! printf '\n' >> "$exclude_file"; then
      printf 'could not terminate git exclude line: %s\n' "$exclude_file" >&2
      exit 1
    fi
  fi
  if ! printf '/.worktrees/\n' >> "$exclude_file"; then
    printf 'could not register .worktrees in git exclude: %s\n' "$exclude_file" >&2
    exit 1
  fi
fi

# Clean up on exit — INCLUDING on signal, so a killed run gets a safe removal attempt.
worktree_created=0
creation_in_progress=0
cleanup_done=0
cleanup() {
  if [ "$worktree_created" -ne 1 ] && [ "$creation_in_progress" -ne 1 ]; then
    return
  fi
  if [ "$cleanup_done" -eq 1 ]; then
    return
  fi
  cleanup_done=1
  if git -C "$repo_root" worktree remove "$worktree" 2>/dev/null; then
    git -C "$repo_root" branch -d "work/$run_id"
  fi
}
on_signal() {
  signal="$1"
  cleanup
  trap - INT TERM
  case "$signal" in
    INT) exit 130 ;;
    TERM) exit 143 ;;
  esac
}
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

creation_in_progress=1
if ! git -C "$repo_root" worktree add "$worktree" -b "work/$run_id"; then
  creation_in_progress=0
  trap - EXIT INT TERM
  printf 'worktree creation failed; leaving any partial target for inspection: %s\n' "$worktree" >&2
  exit 1
fi
worktree_created=1
creation_in_progress=0

# ... do the work in $worktree — fully isolated from other runs ...
```

The non-force removal intentionally leaves a dirty worktree for inspection and later
`rig worktree gc` classification when `rig` is available.

When `rig` is available, use its lifecycle commands instead of raw git cleanup:

```bash
rig worktree remove "$name"  # removes the tree and its branch
rig worktree gc --dry-run    # reports classifications without removing anything
rig worktree gc --yes        # removes only merged/closed/prunable worktrees
```

A bare `rig worktree gc` only reports classifications; `--dry-run` makes that preview
explicit, while `--yes` is required for removal and only removes its safe-to-clean
classes.

For a fallback-created worktree when `rig` is unavailable, use the signal-safe,
non-force `git -C "$repo_root" worktree remove "$worktree"` cleanup shown above. It
refuses to remove a dirty tree, leaving it for inspection and later `rig worktree gc
--dry-run` classification when `rig` is available. After reviewing that report, use
`rig worktree gc --yes` only for its safe-to-clean classes. Delete the fallback branch
only after a successful clean removal, using `git -C "$repo_root" branch -d
"work/$run_id"`.

Key points:

- **Unique name per run** (a run id), so concurrent runs never pick the same path.
- **Separate scratch/output dirs** too — if the tasks write build output or temp files,
  those must also be per-run, not a shared dir that two runs clobber.
- **Clean up on exit, including signals.** Register the cleanup on `EXIT` and signal traps
  so an interrupted or killed run gets a non-force removal attempt. Clean trees are
  removed; dirty trees remain for inspection rather than being force-deleted.

## Why

A worktree is the right isolation primitive: cheaper than a full clone (shared object
store, shared history) but with a fully independent working tree and HEAD — exactly what
parallel agents need so their edits and branch state don't interfere. The signal-safe
cleanup is the part people forget; without it, every crashed run leaves a stale worktree
behind. Pairs with `universal/worktree-base-trap` (verify each new worktree's base) and
`monorepo/squash-merge-cleanup` (tearing them down after merge).
