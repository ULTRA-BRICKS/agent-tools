# block-unlanded-removal

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 41

Allows removing a worktree or deleting a branch **only when the work is landed and the tree is
clean**. Anything that could destroy unmerged or uncommitted work is denied.

## How it works

The hook is a thin shim; `rig worktree guard` is the only brain.

1. A cheap **prefilter** decides whether the command is removal-shaped. It runs on a normalized
   copy: `$'-D'` -> `-D`, an unexpanded `$VAR`/`${IFS}` tried both as a word break and as empty
   (`git br${X}anch`), then every quote and backslash removed (so `g''it branch -D x` cannot
   dodge it). A command lands in one of two classes.

   **STRICT** — removal-shaped on its face:
   - `worktree remove` / `worktree rm` (subcommands match by unique prefix as git resolves them:
     `bra`, `pus`, `work`, `update-r`)
   - `branch` followed later by a `--d…` long option or a short-flag cluster containing `d`/`D`, or
     a force/move/copy flag: `--f…`, `--m…`, `--c…` (the full `--format`, `--merged`, `--contains`,
     `--color`, `--column`, `--create-reflog` are not matched) or a cluster containing
     `f`/`M`/`C`/`m`/`c`. `git branch -m a b` and `-c a b` never overwrite without `-f`, so rig answers
     `pass` for them; they are matched anyway because a cluster can hide a force (`-fm`) and a false
     match costs one rig call.
   - `checkout` with a short-flag cluster containing `B` (reset an existing branch) and `switch`
     with `-C` / `--force-create`. `checkout -b` and `switch -c` (create, never overwrite) are not
     matched.
   - `rm` as a command word (`rm`, `sudo rm`, `xargs rm`, `\rm`, `command rm`, `/bin/rm`) with `-r`,
     `-R`, `--r…` or a cluster such as `-rf`/`-fr`/`-Rf`. `form`, `arm`, `perform`, `rmdir` and
     `docker run --rm` are not the word `rm`. `git rm -r` matches (rig answers `pass`).
   - `push` with `--d…` (not `--dry-run`), `--m…`, `--pr…` (not `--progress`), a short-flag cluster
     containing `d`, or a delete refspec (`:ref`, `+:ref`)
   - any `update-ref` (`-d`, `--stdin` and an empty new value all delete refs unchecked)

   **LOOSE** — matched only by the expansion clause: expansion syntax (`$`, `{`, a backtick)
   anywhere in a command that mentions `git`, `branch`, `push`, `worktree`, `update-ref` or the
   `rm` command word. The shell may be assembling a destructive word (`-$D`, `{-D,x}`,
   `git br$(true)anch`, `R=r; rm -$R x`); rig sees the same string and decides. Every heredoc
   `git commit -m "$(cat <<'EOF' ...)"`, ordinary `git push origin "$BRANCH"` and `rm "$tmp"` is
   LOOSE.

   No match → the hook allows silently and never spawns `rig` (not even for the version probe).
   The prefilter is deliberately loose; rig decides precisely (e.g. `git commit -m "docs: git
   worktree remove"` reaches rig, which answers `pass`).
2. On a match it checks the [pinned rig](#pinned-rig), then runs `<pinned rig> worktree guard --cwd
   <payload cwd>` with the exact bash command on stdin and reads one JSON object:

   ```json
   {"decision": "pass|allow|deny", "reason": "...", "targets": [{"kind": "worktree|local-branch|remote-branch", "target": "...", "safe": true}]}
   ```

   | decision | hook result |
   | --- | --- |
   | `pass` | allow, silent (not a removal) |
   | `allow` | allow (every target proven landed + clean; rig logged it) |
   | `deny` | block with rig's `reason`, the unsafe targets, and the rescue recipe |

## Pinned rig

The hook never resolves `rig` through `PATH` or the environment: whatever `rig` happens to come
first on `PATH` is attacker-influenced and may be stale or hostile. The descriptor ships

```json
"args": ["--rig", "/ABSOLUTE/PATH/TO/rig"]
```

and `rig apply` substitutes the placeholder with the absolute path of the rig launcher that ran it
(the same way it substitutes `/ABSOLUTE/PATH/TO/` in `cmd`). The runner appends `args` to the
hook's argv. The hook accepts exactly `--rig <path>`; the path must be absolute and name an existing
executable regular file. Before trusting it the hook runs `<path> --version`, which must print
`rig X.Y.Z`; `X.Y.Z` is compared as an integer tuple with `MIN_RIG_VERSION` = **0.64.0**, the first
rig that has `worktree guard` (so `0.100.0` is newer than `0.64.0` and `0.9.0` is older).

## Fails closed

Once the prefilter matched, **both classes** are denied, with a message naming the problem and the
fix (`rig apply commit`), when:

- `--rig` is absent (descriptor installed by an older rig), has no value, is not absolute (also an
  unsubstituted `/ABSOLUTE/PATH/TO/rig`), does not exist, is not a regular file, is not executable,
  or is followed/preceded by extra arguments;
- `<rig> --version` fails, times out (10 s), or does not print `rig X.Y.Z`;
- the guard itself exits nonzero, times out beyond 120 s, prints unparseable or non-object JSON, or
  returns an unknown `decision`;
- the hook event is unparseable or its command has the wrong type.

This is deliberate: with no trustworthy rig there is no way to tell a harmless heredoc commit from a
shell-assembled `git branch -D`, so a LOOSE command (e.g. a heredoc commit) is denied too until `rig
apply commit` re-provisions the hook. `timeout_ms` is 150000 so the host never kills the hook before
its own 120 s budget (`AGENT_TOOLS_REMOVAL_GUARD_TIMEOUT_S` and
`AGENT_TOOLS_REMOVAL_GUARD_VERSION_TIMEOUT_S` override the budgets; tests only).

## Stale rig

A pinned rig that runs but is **older than 0.64.0** predates `worktree guard`. It cannot judge
anything, so the hook degrades instead of wedging every `$`/backtick git command:

| class | stale rig |
| --- | --- |
| STRICT | **denied**: "installed rig X is older than 0.64.0 and has no `worktree guard`; upgrade rig (`rig apply commit` / update the rig checkout)" |
| LOOSE | **allowed** (the pre-guard behaviour) with a one-line warning on stderr |

A current rig (>= 0.64.0) judges both classes.

## No bypass

No env var, inline prefix, or Telegram hatch. The sanctioned ways forward are in the deny text:

- inspect with `rig worktree verdict <target>`;
- merge it, then retry; or push the commits to a branch that is not being deleted
  (`git push origin <branch>:refs/heads/archive/<name>`) and retry — a commit held by another remote
  branch that stays counts as preserved (rig's `preserved` proof), so a PR closed as superseded can
  be cleared once its commits are archived;
- rescue uncommitted work: `git diff --binary HEAD > ~/.cache/<repo>-recovery/<wt>-<ts>.patch`, commit
  it and push it to such a branch too.

## Relation to `block-raw-worktree-add`

That hook enforces the `.worktrees/` *location* for `git worktree add`/`move` and no longer gates
`git worktree remove`; whether a removal is safe is decided here.

## Scope limits

The hook only sees the Bash tool's literal command string. Removals hidden behind harness-native
paths or non-Bash tools are invisible to it; how deeply `bash -c '...'`/`eval` bodies are parsed is
rig's decision, since the prefilter flattens the whole string. Deletion routes that are not a `git`
or `rm` word (`find -delete`, `python -c 'shutil.rmtree(...)'`, `git clean`, `mv` over a directory)
are not prefiltered. The pin trusts the file at the pinned path: whoever can replace that binary can
replace the guard (the hook does not hash it). An old descriptor without `--rig` stays closed until
`rig apply commit` runs; descriptors provisioned before the pin existed need that one re-apply.

## Test

```bash
python3 -m pytest -q tests/test_block_unlanded_removal.py -p no:cacheprovider
```
