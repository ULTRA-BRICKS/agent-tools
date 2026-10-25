# pin-primary-worktree

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Pins the repo's **PRIMARY worktree** to its default branch: BLOCKS a `git checkout` / `git
switch` that would move the primary checkout — never a `git worktree add`-created **linked**
worktree — onto anything other than the default branch (main/master), AND BLOCKS history-writing
commands (`git commit` incl. `--amend`, `cherry-pick`, `merge`, `rebase`, `revert`, `am`) while the
primary checkout sits ON the default branch. (Alex tg#6462/tg#6477; commit side: 2026-10-24.)

## Commit / cherry-pick / merge / rebase on the primary checkout's default branch

**The incident (2026-10-24):** two omp subagents ran plain `git commit` in
`garage-band/projects/wall-plugs` — the primary checkout, on `main` — and four commits landed on
local main. The write guard was off (no `worktree_only` key) and this hook only looked at
checkout/switch, so nothing saw it. Now the command is denied with the exact fix:

```
rig worktree create <feature> --from origin/main -C <repo root>
cd <repo root>/.worktrees/<feature>   # then git commit, push the branch, open a PR
```

Allowed (they write no history): `merge --ff-only`, `--abort` / `--quit`, `commit --dry-run`,
`--help`, `git push`, `git add`. `git pull` is NOT gated either (it is the everyday sync of
main); it can create a merge commit or rebase when local main has diverged, so use `git pull
--ff-only`. Linked worktrees, a primary on a feature branch, a
detached HEAD and non-enrolled repos are never judged here. `cd <dir> &&` earlier in the
chain is followed, and `git -C <dir>` is honored, so the recommended `cd .worktrees/x && git
commit` passes. Same hatch / `approval_cmd` external-approval path as the checkout guard.

## The gap this closes

`worktree-only-writes` (the sibling `pre-write` gate) already denies an Edit/Write while the
checkout sits on the default branch. But it has two blind spots this hook covers:

1. It never sees a bare `git checkout <branch>` — that's a Bash call, not an Edit/Write tool
   call, so the pre-write point never fires on it.
2. Once the checkout HAS moved to a feature branch, `worktree-only-writes`' own logic treats
   "on a feature branch" as "exactly where authoring belongs" — true for a **linked** worktree,
   false for the **primary** one. Neither gate previously distinguished *which worktree* from
   *which branch is checked out*.

**The incident (2026-07-04):** an agent doing HYP-917 work ran `git checkout
feat/hyp-autofix-unsupported-framework` in the shared main checkout
(`/Users/ultra/work/hyperide`) instead of its own isolated worktree. No file damage happened
this time — the agent caught it and had authored everything in its real worktree — but a
second, concurrent agent then also checked out (and committed on) a different feature branch
in that same shared checkout before switching back to main. Any agent relying on the primary
checkout sitting on `main` can have it pulled out from under it mid-operation.

## Primary vs. linked worktree detection

Uses git's own distinction: in the primary worktree `git rev-parse --git-dir` and `--git-dir
--git-common-dir` resolve to the SAME directory (both the real `.git`). In a linked worktree
(`git worktree add`) `--git-dir` is `<common>/.git/worktrees/<name>` while `--git-common-dir`
stays at the shared `.git` — they diverge. Undetermined (git/resolve failure) fails OPEN.

## What's unaffected

- Checking OUT of a feature branch back to the default branch (the safe direction).
- `git worktree add` (a different worktree entirely — recommended alternative, shown in the
  block message).
- Any `git checkout`/`git switch` inside a LINKED worktree (exactly where branch work belongs).
- `git pull` / `git fetch` / `git worktree list` / `git merge --ff-only` (not checkout/switch and
  no new commits).

## Per-repo opt-in

Shares the **same** knob as `worktree-only-writes` — one feature, one flag:

1. `RIG_WORKTREE_ONLY=1` (force on) / `=0` (force off).
2. the repo's committed `rig.yaml` → `agent_hooks.worktree_only: false` opts OUT.
3. a `rig.yaml` without the key → **ON** (default); no `rig.yaml` up the tree → off.

## No self-service bypass — external approval only

There is **no** env-var or inline escape hatch for this checkout guard any more. The old
`RIG_ALLOW_MAIN_EDIT=1` bypass let the very agent this hook constrains grant itself an
exception — security theater, not a permission gate (Alex tg#6554). It was removed here.
(The sibling `worktree-only-writes` pre-write hook still honors `RIG_ALLOW_MAIN_EDIT` — the
same self-service pattern, tracked as a separate follow-up cleanup; this PR is scoped to the
two `pre-bash` guards.)

The block is now **deny-by-default**. A repo owner can wire a real external-approval path in
the committed, code-reviewed `rig.yaml`. A one-time Telegram hatch can also be requested
with a written justification in `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE`; that request path
runs before `approval_cmd`, so an invalid/denied Telegram request does not fall through to a
configured `approval_cmd`. Because this is a **pre-bash** hook, the justification can be
supplied inline on the gated command itself
(`RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE="…" git checkout …`): the hook parses the leading
assignment out of the command string the event carries (a pre-bash hook runs in its own process
*before* the shell evaluates that prefix, so the value never reaches its `os.environ`). An
exported value takes precedence over an inline one.

```yaml
agent_hooks:
  worktree_only: true
  approval_cmd: "/path/to/approve.sh"   # optional; run when a block would fire
  approval_cmd_timeout_s: 5             # optional; default 5.0, capped at 6.0
```

`agent_hooks.approval_cmd` is a **single, shared key** — the same `approval_cmd` is read by
this hook AND by `block-reset-hard`. A repo that wants different handling per guard should
point `approval_cmd` at one dispatcher script that branches on `RIG_APPROVAL_HOOK`
(`pin-primary-worktree` vs `block-reset-hard`) and `RIG_APPROVAL_KIND`.

When `approval_cmd` is set, the hook runs it as the block is about to fire and **allows only on exit
0**; a nonzero exit, an error, or a timeout all mean **denied**. With nothing configured, the
block simply stands. The command string comes only from `rig.yaml` (never from the agent or
the offending command), and context is passed to it as environment variables —
`RIG_APPROVAL_HOOK`, `RIG_APPROVAL_KIND` (`checkout`/`switch`), `RIG_APPROVAL_TARGET` (the
branch), `RIG_APPROVAL_CWD`, `RIG_APPROVAL_COMMAND` — never string-interpolated, so there is no
injection surface.

The Telegram hatch is intentionally not a self-service bypass. `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE`
must contain a nonblank written justification. If the env var is unset, the hook does not contact
Telegram and falls through to `approval_cmd` / default deny. If the env var is present but blank,
whitespace-only, or a bare flag value such as `1`, `true`, `yes`, or `on`, the hook does not contact
Telegram and denies. A real justification runs `tg-ctl ask` with the question as a JSON
ButtonRequest on stdin; only a well-formed reply on stdout whose decision is explicitly `allow`
allows — an empty or unparseable reply, an explicit deny, any nonzero exit, launch errors, and
timeouts all deny (a clean exit alone is NOT approval: `tg-ctl ask` exits 0 regardless of
outcome). The helper never resolves
`tg-ctl` from ambient `PATH`. **The approval binary is a trust anchor resolved from the
account's REAL home only** (home located via `pwd.getpwuid` — never `$HOME`, `$XDG_CONFIG_HOME`,
or the repo/`cwd` the hook runs in): first `tg_ctl.tg_ctl_path` in the machine-global rig config
`~/.config/rig/config.yaml` (rig's own key for where tg-ctl lives; `~/` expands against the real
home, a Cygwin `C:\…` path is translated), then `~/.local/bin`, `~/bin`, `~/.files/bin`,
`~/.linuxbrew/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/home/linuxbrew/.linuxbrew/bin`,
`/usr/bin` (on Cygwin `tg-ctl.exe` also matches). Every directory on the way (symlinks followed
hop by hop) and the binary itself must be owned by you or root and writable by nobody else —
group-write is accepted only for your user-private group (umask 002 distros), gid 0, macOS
`admin`, or Cygwin Administrators/SYSTEM — and the binary must not live inside the git checkout
(or its primary worktree) the agent is working in. A denial lists every path searched and why it
was passed over. A **repo-local** `tg_ctl_path` is deliberately IGNORED — otherwise a guarded
agent could commit config pointing it at an always-exit-0 binary and self-approve (the exact
self-service bypass this gate closes).

> `approval_cmd` is read with the same minimal, stdlib-only rig.yaml scanner used for
> `worktree_only` (these hooks import no YAML library). It is a single-line scalar: quote the
> value, and avoid a literal `#` or a trailing nested-quote in it (point `approval_cmd` at a
> script path instead of inlining a complex shell one-liner).

> **Claude Code outer timeout:** this descriptor sets `timeout_ms: 960000`, which STRICTLY
> exceeds the helper's 930s worst case (`tg-ctl` ask's 900s cap plus a 30s cleanup margin) by a
> 30s headroom. That headroom is a SAFETY margin, not cosmetic: this hook is `on_error: open`, so
> if the descriptor budget merely *equalled* the helper's worst case, a hung or unanswered
> `tg-ctl` could race the bridge's descriptor-timeout kill into a SILENT ALLOW instead of the
> helper's intended deny. The 30s headroom guarantees the helper emits its own deny first.
> Note this is only the agents-hooks/v1 descriptor budget enforced by `cc_hook_bridge`. Claude
> Code's own command-hook `timeout` defaults to 600s, and the live `~/.claude/settings.json` /
> rig-cli `hook_bridge_entries` currently register `cc_hook_bridge` without a `timeout`. Until
> rig-cli or settings add a hook `timeout` above 960s, Claude Code can still kill the bridge
> before a full Telegram wait finishes — and because that outer kill happens *before* the helper
> returns, it is decided by Claude Code's own expired-hook behavior, not this descriptor's
> `on_error`. Raising the settings `timeout` above 960s is required for the guard to hold on a
> full-length Telegram wait.

**Agents: ask, don't self-grant.** If you have a genuine reason for a primary-worktree
checkout, ask the human directly — you can no longer flip your own bypass.

## Known scope limits (heuristic, not a sandbox)

- `cd <dir>` earlier in the chain is followed for both the history guard and the checkout
  guard, but only when it is certain to run: a conditional (`false && cd x`, `a || cd x`),
  piped/backgrounded, `cd -` or `popd` makes every possible directory a candidate and a git
  segment is denied if ANY candidate is a primary checkout on its default branch. `git -C <dir>`
  is honored and re-resolves enrollment and default-branch/primary-worktree detection.
- A `cd` into a worktree that does not exist yet when the hook runs (`rig worktree create f && cd
  .worktrees/f && git commit`) cannot be followed, so that chained commit is denied: create the
  worktree in one Bash call and commit from it in the next.
- `git merge --ff-only <local-branch>` is allowed (it is the pull-like sync of main) but it also
  lands an unpushed local branch on main; only a merge that creates a commit is denied.
- `git reset` (branch-tip mutation without a commit) stays out of scope, as do `git commit`
  spellings that hide the verb from a plain `git [-C d] <verb>` parse (`sh -c`, aliases, a git
  wrapper script); the per-session omp/Claude bridge sees only the literal command string.
- `git checkout .` / a bare path restore is excluded; an unusual `git checkout <treeish>
  <path>` with no `--` (ambiguous even to git itself) can still be misclassified as a branch
  switch — a false block on that rare shape now requires either doing the checkout in a linked
  worktree or a repo-owner `approval_cmd`, since there is no longer a self-service override.

## Test

```bash
python -m pytest -q tests/test_pin_primary_worktree.py
```
