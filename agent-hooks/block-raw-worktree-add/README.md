# block-raw-worktree-add

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Enforces `<repo-root>/.worktrees/` as the **ONLY** sanctioned worktree location: BLOCKS a
`git worktree add` / `git worktree remove` whose target does not resolve there, and points the
agent at `rig worktree create` / `rig worktree remove` / `rig worktree gc` instead.
(agent-tools#608, Alex, 2026-09-07.)

## The gap this closes

rig-cli's own `AGENTS.md` already documents `rig worktree create <name>` as the only way an
agent (or a human) should create a worktree in a rig-managed repo, and `<repo>/.worktrees/<name>`
as the only location — citing **GH-329**: ~70 leftover worktrees accumulated across at least
five different ad-hoc conventions (a bare in-repo `.worktrees/`, a sibling-of-repo `.worktrees/`,
Claude Code's own `.claude/worktrees/`, `<repo>-worktrees/`, `<repo>-wt-*`), none tracked,
nothing ever cleaned them up, tens of GB of duplicated `node_modules`/`.venv`.

That rule was **documentation only**. Nothing stopped a raw `git worktree add
<path-outside-.worktrees/>` from an agent or a script, and the exact same problem was confirmed
still happening, live, the same day this hook was written: `git worktree list` in a rig-managed
repo showed worktrees in `/private/tmp`, a sibling `-worktrees/` directory, a `-wt-*` directory,
and `.claude/worktrees/agent-*` — the five-conventions problem, unguarded.

## What's blocked / what's allowed

- `git worktree add <path>` — BLOCKED if `<path>` does not resolve under `<repo-root>/.worktrees/`.
- `git worktree add -b <branch> <path>` / `git worktree add <path> -b <branch>` — either flag
  order; `-b`/`-B`/`--reason` are recognized as value-taking flags so their argument is never
  mistaken for the target path.
- `git worktree remove <path>` — same check, same rationale (the sanctioned inverse is
  `rig worktree remove`, or `rig worktree gc` for a worktree that predates this convention).
- `git -C <other-repo> worktree add ...` — judged against `<other-repo>`'s own `.worktrees/`,
  not the shell's cwd.
- A chained command (`cmd1 && git worktree add /tmp/x`) — every segment is scanned; one blocked
  segment blocks the whole chain.
- **Allowed, unaffected:** `git worktree add`/`remove` targeting anything already under
  `<repo-root>/.worktrees/` (i.e. what `rig worktree create`/`remove` themselves produce);
  `git worktree list` / `lock` / `unlock` / `move` / `repair`; `git worktree prune` (see
  "Deliberately out of scope" below).

## Why `rig worktree create` itself is never blocked (no process-ancestry check needed)

`rig worktree create` is a plain Python CLI. Its own worktree-creation call
(`riglib/worktree.py::_run_git_worktree_add`) runs `subprocess.run(["git", "-C", repo_root,
"worktree", "add", ...])` **directly from inside the `rig` process** — an ordinary child process
`rig` spawns on its own, never routed back through the agent's Bash tool. An agents-hooks/v1
`pre-bash` hook only ever fires on a command the harness is about to run *because the agent
asked it to run it* — the event payload this hook reads is literally the Bash-tool command
string.

So when an agent runs `rig worktree create <name>`, the event this hook sees carries the literal
text `"rig worktree create <name>"`. That text does not parse as `git ... worktree add ...` at
all — `_classify_git_segment` returns `None` and the hook has nothing to gate. `rig`'s own,
later, internal `git worktree add` call happens entirely inside `rig`'s process, invisible to
any agent-hook dispatcher (which wraps the agent harness's tool-call boundary, not arbitrary
child processes an already-allowed command goes on to spawn).

This was verified by reading `riglib/worktree.py` before writing this hook, rather than assumed.
There is no separate "trust `rig`'s own invocation" mechanism to build or keep in sync — as long
as `rig worktree create` keeps calling git directly (it does, and has no reason to route through
anything that re-enters the agent's own Bash tool), this falls out of the argv-classification
design for free.

## Repo-root resolution

`git rev-parse --git-common-dir` always resolves to the **primary** worktree's real `.git`
directory, regardless of whether the command runs from the primary worktree or a linked one —
the same primitive `pin-primary-worktree` already established for this exact hook family (see
its README for the primary-vs-linked distinction this hook does NOT need — it only needs the
root, not "which worktree is this"). `<git-common-dir>.parent` is therefore always the repo's
one, unambiguous root, so `<repo-root>/.worktrees/` names the same directory no matter where the
agent's cwd happens to be. A bare repo (where `--git-common-dir` doesn't end in `.git`) is
out of scope — undetermined, fails open.

## Deliberately out of scope

- **`git worktree prune`** — takes no path argument at all; it's git's own administrative
  cleanup, functionally the same *direction* as `rig worktree gc` (it removes stale state, it
  doesn't create sprawl). Gating it would add a second detection shape for zero benefit against
  GH-329's actual failure mode (a worktree landing in the wrong *place*), and would fight
  `rig worktree gc`'s own remediation path.
- **`git worktree list`/`lock`/`unlock`/`move`/`repair`** — none of these create or relocate a
  worktree relative to the repo root the way `add`/`remove` do.

## No opt-in flag

Unlike `worktree_only` (the `pin-primary-worktree`/`worktree-only-writes` knob, which changes
*where authoring is allowed*), this hook enforces a repo-hygiene invariant that has no legitimate
opt-out per repo — it carries no `rig.yaml` flag at all. It is installed everywhere the
agent-tools catalog is applied (`agent_hooks.all: true`, the default), the same way
`block-raw-pr-merge`/`block-no-verify` are always active once installed, and is simply absent
(a no-op) in any repo where it isn't installed.

## No self-service bypass — Telegram hatch only

There is no env-var/inline escape hatch. The block is deny-by-default. For a genuine one-time
exception — migrating a pre-existing worktree that predates this hook, a documented non-agent
`git worktree` use case that legitimately lives elsewhere — request a single Telegram approval:

```
RIG_HATCH_REQUEST_BLOCK_RAW_WORKTREE_ADD="<written justification>" git worktree add /elsewhere x
```

This routes one approval request to Alex via `tg-ctl ask` (deny-by-default; a bare `1`/`true`/
`yes`/`on` is rejected outright, no Telegram round-trip). Only an approved reply (exit 0, decision
`allow`) lets the raw command through — copied verbatim from `block-raw-pr-merge`'s hatch usage
pattern (see that hook's README for the full mechanics: trusted absolute `tg-ctl` resolution,
audit sink, inline-vs-exported precedence).

## `on_error: open` — why this diverges from `block-raw-pr-merge`

This hook follows the sibling worktree-family hooks (`pin-primary-worktree`,
`worktree-only-writes`, both `on_error: open`) rather than `block-raw-pr-merge`'s
`on_error: closed`. `block-raw-pr-merge` guards an irreversible security boundary — a merge that
skips CI/review gates. This hook guards a hygiene convention: its failure mode is "a worktree
ends up in the wrong directory", not data loss or a bypassed gate. A crash in this hook's own
logic (not a hatch-escalation timeout, which the hook resolves itself) must never wedge the
agent's ability to run an unrelated Bash command.

## Known scope limits (heuristic, not a sandbox)

- A `cd other-repo && git worktree add X` chain is judged against the **original** cwd's repo
  root unless the segment itself carries `git -C <dir>` (which IS honored).
- An unrecognized *future* `git worktree add`/`remove` flag that happens to take a value not in
  this hook's known value-flag set (`-b`/`-B`/`--reason` for `add`; none for `remove`) could be
  misread as the target path, or vice versa — the same class of residual every argv-heuristic
  hook in this family accepts and documents rather than hand-parsing git's full option grammar.

## Known, deliberately unclosed bypasses

Named explicitly rather than left for a reviewer to rediscover:

- **`bash -c '...'` / `sh -c "..."` / `eval "..."`** hiding a `git worktree add`/`remove` is NOT
  detected — this hook does not re-scan interpreter argument strings the way `block-raw-pr-merge`
  does for its narrower merge detector. Same residual `pin-primary-worktree` already accepts for
  `checkout`/`switch`. A follow-up could port `block-raw-pr-merge`'s
  `_SHELL_INTERPRETERS`/`_SHELL_C_OPT` machinery; not done in this first cut to avoid
  scope-creeping a hygiene gate into a second full command-injection surface.
- **A harness-native worktree path** (e.g. Claude Code's own `EnterWorktree` /
  `.claude/worktrees/agent-*` convention) is not a Bash tool call at all, so no `pre-bash` hook —
  this one included — can see or gate it. This was one of the five ad-hoc conventions the live
  GH-329 incident actually observed.
- **A `.worktrees` directory that is already a symlink pointing outside the repo root** would let
  a target that resolves "under `.worktrees/`" actually land elsewhere. This one IS closed —
  `_worktrees_dir_escapes_repo` (mirrors `riglib/worktree.py::_target_escapes_repo`) refuses to
  treat ANY target as sanctioned once `.worktrees` itself is such a symlink — named here because
  it's the kind of gap this class of hook is prone to and worth a reviewer's explicit attention.

## Test

```bash
python -m pytest -q tests/test_block_raw_worktree_add.py
```
