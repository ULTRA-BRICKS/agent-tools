# worktree-only-writes

**Point:** `pre-write` · **Fail policy:** `open` · **Priority:** 40 (runs before the other pre-write gates)

Enforces the **worktree-only workflow**: on a PR-workflow repo the **default branch**
(main/master) is for **merge / pull / read-only** operations only. All file-authoring work
belongs in a **separate git worktree on a feature branch**. This gate DENIES an
`Edit/Write/MultiEdit/NotebookEdit` while the checkout is sitting on the default branch and
tells the agent to create/enter a worktree first. (Alex tg#5742.)

It is the mid-session complement to the pre-push **`protect-main`** git-hook
(agent-tools#157): that hook blocks a direct *push* to main; this one blocks the *authoring*
that would precede it — redirecting the agent to a worktree **before** it writes, not after
it tries to push. Read-only tools and `git merge` / `git pull` are unaffected (they are not
Edit/Write tool calls, so this point never sees them).

**This gate alone does NOT stop a bare `git checkout <feature-branch>`** run directly in the
repo's PRIMARY worktree — a `git checkout`/`switch` isn't an Edit/Write tool call, so this
`pre-write` point never sees it, and once the checkout has moved to a feature branch this
gate's own branch check treats that as "exactly where authoring belongs" (true for a *linked*
worktree, not for the *primary* one — it has no notion of which worktree it's in at all). See
the sibling `pin-primary-worktree` (a `pre-bash` gate) for that half: it blocks moving the
PRIMARY worktree itself off the default branch. (Alex tg#6462/tg#6477 — the incident that
surfaced this gap.)

## Default-branch detection (not hardcoded, never machine-derived)

1. `git symbolic-ref --short refs/remotes/origin/HEAD` → `origin/main` → `main` (authoritative)
2. repo-local `git config --local --get init.defaultBranch` (NOT the global setting)
3. a local branch that exists — `main` preferred, then `master`
4. fallback `main`

A `master`-only repo is judged against `master`. Each step is repo-local, so enforcement never
swings with the developer's global git config.

## Per-repo opt-in (this must NOT break repos that work on main)

Enforcement is **opt-in**, first match wins:

1. `RIG_WORKTREE_ONLY=1` (force on) / `=0` (force off) — session/CI override, and how the
   tests drive it.
2. the repo's committed `rig.yaml` → `agent_hooks.worktree_only: true` — the rig-provisioned
   signal.
3. **default OFF** — a repo with no signal is never blocked.

So `hyperide` and the agent-ecosystem repos set `worktree_only: true` in their `rig.yaml`;
`3d-cli` (which legitimately works directly on main) leaves it absent and is exempt
automatically. See rig-cli `docs/config-schema.md` for the knob.

## No self-service bypass — external approval only (the rare, deliberate main edit)

There is **no** env-var self-service bypass any more — the old `RIG_ALLOW_MAIN_EDIT=1` is
**removed** (an auto-mode agent could set it on itself, so it never actually gated anything).
The block is now **deny-by-default**. A genuine one-off main edit requests a one-time Telegram
approval by exporting a written justification:

```bash
RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES="hotfix, worktree overkill for this one edit"
```

The request routes to the human via trusted `tg-ctl` and the write is allowed **only** on an
approval tap; a bare `1` (no real justification) is rejected, and an unanswered/denied prompt
blocks. This mirrors `block-raw-pr-merge` / `block-reset-hard`.

A late approval is not wasted: if the human approves after the ask window expires, the tg-ctl daemon keeps it as a single-use `approved:<id>` grant bound to this exact operation. This hook has no shell command to carry an inline prefix, so the retry form is: simply retry the IDENTICAL operation in the same directory and the shared lib consumes the grant automatically via its pull — which requires this session's route identity to match the original ask; if the retry still denies, ask the human via `tg --tag decision`. A changed edit or a different directory is denied and does not consume the grant. See the shared lib docstring (`lib/agenttools_hatch_escalation/`) for the full Late-Approval Grant Protocol (agent-tools#742).

Because the approval can wait up to `tg-ctl ask`'s 900s window, the descriptor's `timeout_ms`
is **960000** (900s cap + 30s margin), not the 3s default: a slow/unanswered prompt must resolve
to the hook's own deny **before** the bridge's descriptor-timeout kill would fire `on_error:
open` and silently allow the edit. A Claude Code settings entry launching `cc_hook_bridge` must
likewise set `timeout > 960s`.

## Why an agent-hook

Advice in `AGENTS.md` cannot stop an auto-mode agent from editing on main. A git-hook fires
only at commit/push — too late, and the pre-push guard already covers that stage. The only
place to redirect *authoring* to a worktree is **before the write**, which is what a
`pre-write` agent-hook does.

## Fail-open

`on_error: "open"`. If the branch can't be determined (not a git checkout, git missing, a
timeout) the write is **allowed** — a workflow nudge must never wedge the agent.

## Test

```bash
chmod +x worktree_only_writes.py
# on the default branch, enrolled → BLOCK
RIG_WORKTREE_ONLY=1 sh -c 'echo "{\"cwd\":\"$PWD\",\"args\":{\"file_path\":\"a.ts\"}}" | ./worktree_only_writes.py'; echo "exit=$?"
# not enrolled (default) → allow, exit=0
echo '{"cwd":"/tmp","args":{"file_path":"a.ts"}}' | ./worktree_only_writes.py; echo "exit=$?"
```
