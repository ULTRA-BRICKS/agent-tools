# enterworktree-canonical-path-grant

**Point:** `pre-worktree-enter` · **Fail policy:** `open` · **Priority:** 40

GRANTs (pre-approves, suppressing Claude Code's own interactive permission prompt) an
`EnterWorktree(path=...)` call whose target resolves to this repo's rig-sanctioned direct-child
`<repo-root>/.worktrees/<name>` location — the ONLY sanctioned worktree location per rig-cli's
own AGENTS.md "Worktree lifecycle" section. Never blocks anything.

## The gap this closes (rig-cli#418)

PR#298 registered an `EnterWorktree` PreToolUse HOOK matcher into the CC hook bridge, so hooks
like `enterworktree-foreign-guard` can fire on the call. That only wires the HOOK layer. Claude
Code has a SEPARATE, EARLIER permission classifier that decides whether to interactively prompt
for approval BEFORE a tool call even reaches PreToolUse hooks — and nothing in rig's
provisioning ever granted `EnterWorktree` there. Confirmed live: an `EnterWorktree` call into
the canonical `<repo>/.worktrees/<name>` location still triggered a full interactive
"relocation outside .claude/worktrees/" approval prompt, blocking an autonomous session with
nobody around to answer it.

## Why this is NOT a blanket `EnterWorktree` grant

An earlier draft of this fix (corrected before it landed) considered pre-approving
`EnterWorktree` broadly. That would have been wrong: `EnterWorktree(path=...)` can switch into
ANY worktree already registered in `git worktree list`, not only the canonical location — a
blanket grant would let a caller route around the exact same worktree-location-bypass concern
`block-raw-worktree-add` (agent-tools#608) closes for the raw `git worktree add`/`remove`/`move`
Bash path, just via a different tool. This hook is PATH-SCOPED instead:

- `args.path` resolves to the direct-child `<repo-root>/.worktrees/<name>` shape → **GRANT**
  (suppress the prompt).
- `args.path` resolves to a nested path such as `<repo-root>/.worktrees/sub/feat` → plain
  `allow` (no grant).
- `args.path` resolves anywhere else → plain `allow` (no opinion; CC's own interactive prompt
  still fires, exactly as it does today — this hook only ever *removes* friction for the
  sanctioned case, it never adds a new restriction).
- `args.path` absent (creating a brand-new worktree via `args.name`) → plain `allow`. CC
  hardcodes `name`-mode into `.claude/worktrees/` (its own already-trusted convention); there is
  no friction to remove there.

A deny/hatch-gate for a non-canonical `EnterWorktree` target — mirroring
`block-raw-worktree-add`'s hatch-gated exception path — is a natural follow-up but a separate,
bigger decision than "stop prompting for the case that is already sanctioned." Deliberately not
built here; tracked as a follow-up, not silently absent.

## Reused, not reimplemented

Detection copies (SYNC, not imported — every agent-hook in this family is a standalone script by
convention) `repo_root` / `_worktrees_dir_escapes_repo` / `_target_is_sanctioned` from
`agent-hooks/block-raw-worktree-add/block_raw_worktree_add.py` (agent-tools#608) — the same
primitives that already decide "is this path the one sanctioned worktree location" for the raw
`git worktree add` case, so the two never drift into disagreeing about what counts as canonical.
The `git_dir` override parameter that source carries (for a parsed `git -C ... --git-dir ...`
Bash command) is deliberately trimmed here: `EnterWorktree`'s JSON `args` carry a bare
`path`/`name`, never a shell command with global git options, so there is nothing to capture.

## Relationship to `enterworktree-foreign-guard`

Both hooks target `pre-worktree-enter`, but they don't overlap: `enterworktree-foreign-guard`
BLOCKS entering a worktree a DIFFERENT dispatched agent owns (a `.claude/worktrees/agent-<id>`
ownership check). This hook only ever GRANTS, and only for the rig `.worktrees/` location, which
sits outside that other hook's naming convention entirely. `cc_hook_bridge`'s aggregation lets a
BLOCK from either hook win outright over this one's grant, regardless of priority order.

## No self-service bypass needed

Unlike `block-raw-worktree-add`, this hook never blocks or denies anything — it only ever adds
an affirmative grant for the ALREADY-sanctioned case. There is no "genuine one-off exception" to
route to a human: a non-canonical target simply gets no opinion from this hook, same as if it
were never installed.

## Registration note

Firing requires BOTH: a rig-cli `EnterWorktree` PreToolUse matcher (shipped, rig-cli#298) AND a
`cc_hook_bridge` build that understands the agents-hooks/v1 `"grant"` verdict (this same
change — see `lib/agent_hooks_v1/runner.py`'s `_allow_outcome` and
`lib/cc_hook_bridge/dispatch.py`'s "PreToolUse grant" note). On an older bridge that predates
the `"grant"` verdict, this hook's exit-0 output is read as a plain, silent allow — inert, not
broken (the same safety property every other point-mapping addition in this bridge relies on).
