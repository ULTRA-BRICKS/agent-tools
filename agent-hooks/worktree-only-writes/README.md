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

## Not file writes: omp harness URIs and write-free `eval` cells (agent-tools#828)

The omp bridge routes `write` and `eval` to `pre-write`, but two shapes author nothing in a
checkout, so they are **allowed** even on an enrolled default branch:

- **Harness URIs.** A target whose first usable `file_path`/`path`/`notebook_path` value is
  `agent://`, `xd://`, `proc://`, `artifact://`, `local://`, `skill://`, `history://` or
  `mcp://` — peer messages (`agent://<id>`, `agent://all`), session artifacts
  (`local://<name>.md`), job control (`proc://<id>/kill`), tool devices (`xd://report_issue`).
  The scheme must match exactly in lowercase. Still judged as a path (so still blocked on
  main): any other spelling or scheme (`AGENT://`, `file://`, `ssh://`), a relative path that
  merely contains a colon (`foo:bar/baz.txt`), a URI with a `..` segment (`local://../x`), and
  the file-rewriting devices `xd://ast_edit` and `xd://resolve`. The `..` and device checks run
  on the fully percent-decoded remainder, case-insensitively for the device name, so
  `local://%2e%2e/x` and `xd://%61st_edit` are still blocked.
- **`eval` with no scraped write.** An omp `eval` cell where the bridge found no file write
  (no path, empty `content`) — a read-only HTTP GET, a computation — is allowed. A scraped
  write is judged like `write`: a path in the main checkout blocks, and a recognized write to
  an unresolved target (path-less bucket with content) keeps the cwd fallback and blocks.
  The scrape is best-effort (see `lib/omp_hook_bridge/dispatch.py`), so this stays a workflow
  nudge, not a sandbox.

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
