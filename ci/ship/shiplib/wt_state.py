"""Which worktrees belong to a PR, and whether one is really clean (shared by the pre-merge sanity
check in ci_gate.py and the post-merge cleanup in cleanup.py).

Two facts the branch name alone gets wrong:

* a PR's work is not always checked out under the PR's branch name: a worktree made with a different
  local branch name (pushed as `HEAD:<pr-branch>`), or a detached HEAD, holds the very same commit.
  Those are matched by the PR head's commit SHA (`headRefOid`) and handled like the named checkout;
* tooling seeds a `task.yaml` (the tracker routing file) into every worktree. On a PR branch forked
  before the base began tracking it, it shows up as an untracked `?? task.yaml`, which would make an
  otherwise clean PR worktree "dirty" (refused pre-merge, kept post-merge). A copy byte-identical to
  the base branch's own task.yaml is not unshipped work; anything else still counts as dirt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from shiplib.ctx import Ctx, Result

SEEDED_FILE = "task.yaml"


@dataclass(frozen=True)
class Checkout:
    path: str
    head: str  # commit the checkout is at
    branch: str  # short local branch name, "" when detached
    locked: bool = False


def list_checkouts(ctx: Ctx, cwd: str | None) -> list[Checkout]:
    res = ctx.git("worktree", "list", "--porcelain", cwd=cwd, stderr="devnull")
    out: list[Checkout] = []
    for block in res.out.split("\n\n"):
        path = head = branch = ""
        locked = bare = False
        for line in block.split("\n"):
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line.startswith("HEAD "):
                head = line[len("HEAD "):]
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):]
            elif line == "locked" or line.startswith("locked "):
                locked = True
            elif line == "bare":
                bare = True
        if path and not bare:
            out.append(Checkout(path, head, branch, locked))
    return out


def pr_head_oid(ctx: Ctx) -> str:
    """The PR's current head commit as the platform reports it ("" if unreadable)."""
    res = ctx.gh("pr", "view", ctx.pr, "--json", "headRefOid", "-q", ".headRefOid", stderr="devnull")
    return res.out.strip() if res.rc == 0 else ""


def _tracks_pr_branch(ctx: Ctx, c: Checkout) -> bool:
    """A renamed local branch of the PR is the one whose upstream is `origin/<pr-branch>`; another
    branch that merely sits on the same commit (the next stacked PR just started from it) is not."""
    res = ctx.git("-C", c.path, "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{c.branch}", stderr="devnull")
    return res.rc == 0 and res.out.strip() == f"origin/{ctx.branch}"


def head_alias_checkouts(ctx: Ctx, checkouts: list[Checkout], head_oid: str, base: str) -> list[Checkout]:
    """Checkouts of the PR that do not hold its branch by name: a detached HEAD on the PR head commit,
    or a differently named local branch on it that tracks `origin/<pr-branch>`. Never the primary
    checkout (the first entry git lists), a locked tree, or a checkout of the base branch."""
    if not head_oid or not checkouts:
        return []
    primary = os.path.realpath(checkouts[0].path)
    return [
        c for c in checkouts[1:]
        if c.head == head_oid and c.branch != ctx.branch and c.branch != base and not c.locked
        and os.path.realpath(c.path) != primary and (not c.branch or _tracks_pr_branch(ctx, c))
    ]


def _seeded_file_matches_base(ctx: Ctx, wt: str, base: str) -> bool:
    path = os.path.join(wt, SEEDED_FILE)
    if not base or not os.path.isfile(path) or os.path.islink(path):
        return False
    theirs = ctx.git("-C", wt, "rev-parse", "--verify", "-q", f"origin/{base}:{SEEDED_FILE}", stderr="devnull")
    mine = ctx.git("-C", wt, "hash-object", "--", SEEDED_FILE, stderr="devnull")
    return theirs.rc == 0 and mine.rc == 0 and theirs.out.strip() == mine.out.strip()


def wt_status(ctx: Ctx, wt: str, base: str) -> Result:
    """`git status --porcelain` of `wt` with a seeded task.yaml identical to origin/<base>'s dropped.
    The rc is git's own, so a tree whose status cannot be read is never reported clean."""
    res = ctx.git("-C", wt, "status", "--porcelain", stderr="devnull")
    if res.rc != 0:
        return res
    lines = res.out.split("\n")
    seeded = f"?? {SEEDED_FILE}"
    if seeded in lines and _seeded_file_matches_base(ctx, wt, base):
        lines = [ln for ln in lines if ln != seeded]
    return Result(res.rc, "\n".join(lines), res.err)
