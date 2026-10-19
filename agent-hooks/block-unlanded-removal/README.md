# block-unlanded-removal

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 41

Allows removing a worktree or deleting a branch **only when the work is landed and the tree is
clean**. Anything that could destroy unmerged or uncommitted work is denied.

## How it works

The hook is a thin shim; `rig worktree guard` is the only brain.

1. A cheap **prefilter** decides whether the command is removal-shaped. It runs on a normalized
   copy: `$'-D'` -> `-D`, an unexpanded `$VAR`/`${IFS}` tried both as a word break and as empty
   (`git br${X}anch`), then every quote and backslash removed (so `g''it branch -D x` cannot
   dodge it). It matches:
   - `worktree remove` / `worktree rm` (subcommands match by unique prefix as git resolves them:
     `bra`, `pus`, `work`, `update-r`)
   - `branch` followed later by a `--d…` long option or a short-flag cluster containing `d`/`D`
   - `push` with `--d…` (not `--dry-run`), `--m…`, `--pr…` (not `--progress`), a short-flag cluster
     containing `d`, or a delete refspec (`:ref`, `+:ref`)
   - any `update-ref` (`-d`, `--stdin` and an empty new value all delete refs unchecked)
   - expansion syntax (`$`, `{`, a backtick) anywhere in a command that mentions `git`, `branch`,
     `push`, `worktree` or `update-ref`: the shell may be assembling a destructive word (`-$D`,
     `{-D,x}`, `git br$(true)anch`). rig sees the same string and decides. Ordinary `git push
     origin "$BRANCH"` therefore costs one rig call; with rig missing it is denied (fail closed).

   No match → the hook allows silently and never spawns `rig`. The prefilter is deliberately loose;
   rig decides precisely (e.g. `git commit -m "docs: git worktree remove"` reaches rig, which
   answers `pass`).
2. On a match it runs `rig worktree guard --cwd <payload cwd>` with the exact bash command on
   stdin and reads one JSON object:

   ```json
   {"decision": "pass|allow|deny", "reason": "...", "targets": [{"kind": "worktree|local-branch|remote-branch", "target": "...", "safe": true}]}
   ```

   | decision | hook result |
   | --- | --- |
   | `pass` | allow, silent (not a removal) |
   | `allow` | allow (every target proven landed + clean; rig logged it) |
   | `deny` | block with rig's `reason`, the unsafe targets, and the rescue recipe |

## Fails closed

Once the prefilter matched, any of these denies with a clear message: `rig` not on PATH, nonzero
exit, a timeout beyond 120 s, unparseable or non-object JSON, an unknown `decision`, an unparseable
hook event. `timeout_ms` is 150000 so the host never kills the hook before its own 120 s budget
(`AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S` overrides the budget; tests only).

## No bypass

No env var, inline prefix, or Telegram hatch. The sanctioned ways forward are in the deny text:

- inspect with `rig worktree verdict <target>`;
- push the work or merge it, then retry;
- rescue it: `git diff --binary HEAD > ~/.cache/<repo>-recovery/<wt>-<ts>.patch` and push a
  followup branch.

## Relation to `block-raw-worktree-add`

That hook enforces the `.worktrees/` *location* for `git worktree add`/`move` and no longer gates
`git worktree remove`; whether a removal is safe is decided here.

## Scope limits

The hook only sees the Bash tool's literal command string. Removals hidden behind harness-native
paths or non-Bash tools are invisible to it; how deeply `bash -c '...'`/`eval` bodies are parsed is
rig's decision, since the prefilter flattens the whole string.

## Test

```bash
python -m pytest -q tests/test_block_unlanded_removal.py
```
