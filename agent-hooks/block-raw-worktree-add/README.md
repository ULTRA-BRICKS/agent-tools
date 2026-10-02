# block-raw-worktree-add

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Enforces direct-child canonical `<repo-root>/.worktrees/<name>` targets as the **ONLY**
sanctioned worktree locations: BLOCKS a
`git worktree add` / `remove` / `move` whose target does not resolve there, and points the
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

- `git worktree add <path>` — BLOCKED if `<path>` does not resolve to a direct-child canonical
  `<repo-root>/.worktrees/<name>` target.
- `git worktree add -b <branch> <path>` / `git worktree add <path> -b <branch>` — either flag
  order; `-b`/`-B`/`--reason` are recognized as value-taking flags so their argument is never
  mistaken for the target path.
- `git worktree remove <path>` — same check, same rationale (the sanctioned inverse is
  `rig worktree remove`, or `rig worktree gc` for a worktree that predates this convention).
- `git worktree move <worktree> <new-path>` — gated on the DESTINATION (the second positional);
  the source isn't checked, so a legitimate migration `git worktree move /tmp/old .worktrees/old`
  still works. `move` fully relocates an existing worktree — exactly the GH-329 failure mode —
  and was originally, incorrectly, left out of scope in an earlier draft of this hook (a review
  caught it before merge; see "Deliberately out of scope" below for what's genuinely excluded).
- `git -C <other-repo> worktree add ...` and `git --git-dir=<real-repo>/.git worktree add ...` /
  a leading `GIT_DIR=<real-repo>/.git` assignment — all judged against the REAL target repo, not
  the shell's cwd (a `-C <non-repo-dir>` combined with an explicit `--git-dir`/`GIT_DIR` pointing
  at a real repo is still resolved correctly, not treated as "repo root undetermined").
- A chained command (`cmd1 && git worktree add /tmp/x`) — every segment is scanned; one blocked
  segment blocks the whole chain. At most ONE segment in a single chained command gets a Telegram
  hatch round-trip (see "No self-service bypass" below) — a second gate-worthy segment in the
  same chain is blocked outright, no hatch call.
- **Allowed, unaffected:** `git worktree add`/`remove`/`move` targeting a direct-child canonical
  `<repo-root>/.worktrees/<name>` (i.e. what `rig worktree create`/`remove` themselves produce, or a
  destination `.worktrees/<name>` migration lands at — but NOT `.worktrees` itself with no
  `<name>`, or nested paths such as `.worktrees/sub/feat`, which are not valid
  `rig worktree create` shapes and are still blocked);
  `git worktree list` / `lock` / `unlock` / `repair`; `git worktree prune` (see "Deliberately out
  of scope" below).

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
- **`git worktree list`/`lock`/`unlock`/`repair`** — none of these create or relocate a worktree
  relative to the repo root the way `add`/`remove`/`move` do.

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

**At most one hatch round-trip per invocation.** `tg-ctl ask` legitimately runs up to ~900s. A
chained command with two or more gate-worthy `worktree add`/`remove`/`move` segments could sum
past this hook's `timeout_ms` manifest budget (sized for ONE such call), which would let the
dispatcher's own timeout-kill resolve to `on_error: open`'s silent allow for the WHOLE chain —
including a segment that was never actually approved. So only the FIRST gate-worthy segment in a
chain gets a hatch call; any later one is blocked outright with no hatch call at all. Split a
multi-worktree-op chain into separate Bash calls so each gets its own approval.

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
- An unrecognized *future* `git worktree add`/`remove`/`move` flag that happens to take a value
  not in this hook's known value-flag set (`-b`/`-B`/`--reason` for `add`; none for `remove`/
  `move`) could be misread as the target path, or vice versa — the same class of residual every
  argv-heuristic hook in this family accepts and documents rather than hand-parsing git's full
  option grammar.
- A **repeated** `-C`/`--git-dir` (`git -C a -C b ...`) is not composed the way real git composes
  it (a second `-C` resolves relative to the first, not to the original cwd) — this hook just
  keeps the LAST occurrence's raw value, so a doubly-`-C`'d segment can be judged against a
  directory git itself would not actually use.
- Inside a **submodule**, `--git-common-dir` resolves to `<parent>/.git/modules/<sub>` (named
  `<sub>`, not `.git`), so `repo_root()` returns None and a `git worktree add` run from inside a
  submodule of a rig-managed repo is NOT gated by this hook (tested:
  `test_submodule_worktree_add_fails_open_documented_scope_limit`).
- A `~/...`/`$HOME/...` target is never tilde/env-expanded — this hook works on the raw argv
  token exactly as git receives it before shell expansion for a QUOTED path (an unquoted one is
  already shell-expanded before this hook ever sees it). A SANCTIONED target spelled with `~`
  resolves to a literal `<cwd>/~/...` and is over-blocked — safe direction, but costs a real
  Telegram round-trip for a false positive.

## Known, deliberately unclosed bypasses

Named explicitly rather than left for a reviewer to rediscover:

- **`bash -c '...'` / `sh -c "..."` / `eval "..."`** hiding a `git worktree add`/`remove`/`move`
  is NOT detected — this hook does not re-scan interpreter argument strings the way `block-raw-pr-merge`
  does for its narrower merge detector. Same residual `pin-primary-worktree` already accepts for
  `checkout`/`switch`. A follow-up could port `block-raw-pr-merge`'s
  `_SHELL_INTERPRETERS`/`_SHELL_C_OPT` machinery; not done in this first cut to avoid
  scope-creeping a hygiene gate into a second full command-injection surface.
- **A harness-native worktree path** (e.g. Claude Code's own `EnterWorktree` /
  `.claude/worktrees/agent-*` convention) is not a Bash tool call at all, so no `pre-bash` hook —
  this one included — can see or gate it. This was one of the five ad-hoc conventions the live
  GH-329 incident actually observed.
- **A `.worktrees` that is ALREADY a symlink** (pointing anywhere, including at itself or at the
  repo root) is closed for a PRE-EXISTING symlink — `_worktrees_dir_is_untrusted` refuses to
  treat ANY target as sanctioned once `.worktrees` is a symlink at all, checked at the moment
  this hook evaluates the command.
- **The same symlink, planted by an EARLIER segment of the SAME chained command, is a TOCTOU
  residual — NOT closed.** This hook evaluates every segment of a chained command before any of
  them actually run. `ln -s /tmp/sprawl .worktrees && git worktree add .worktrees/x feat/x` sees
  `.worktrees` not yet a symlink at evaluation time, judges the target as sanctioned, and only
  AFTER this hook allows the command does the shell create the symlink and let git follow it. A
  SEPARATE, later Bash call hitting an already-planted symlink IS caught (the symlink exists by
  then); only same-chain planting evades it. Same class as the `cd other-repo &&` scope limit
  above (an earlier segment changes what a later one means) — a targeted "scan earlier segments
  for anything touching `.worktrees`" check would still be evadable (`python -c`, `mv`, `cp -s`,
  ...) and would over-block the benign `mkdir -p .worktrees && git worktree add .worktrees/x
  feat/x`, so it's documented rather than half-closed.
- **A raw target token carrying an unexpanded `$VAR`/`$( … )`/backtick** is closed — treated as
  NOT sanctioned regardless of where it lexically resolves, because the literal argv token this
  hook sees is pre-shell-expansion (`.worktrees/$(printf ../outside-wt)` has no `..` component
  until the real shell expands it, at which point git lands the worktree outside `.worktrees/`).

## Test

```bash
python -m pytest -q tests/test_block_raw_worktree_add.py
```
