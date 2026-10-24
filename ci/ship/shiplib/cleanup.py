"""Post-merge cleanup (the old ship.sh `# --- cleanup` section) — best-effort housekeeping.

The merge already succeeded when this runs: nothing here may flip the exit status or mask the
merge, so every step warns instead of aborting. What it does, in order:

1. read the PR back once (state + merged head) and resolve its REAL base branch (`baseRefName`;
   `SHIP_DEFAULT_BRANCH` is only the fallback when gh cannot answer);
2. delete the remote head branch;
3. remove the checkout(s) holding the PR branch and then the local branch — only once the PR is
   confirmed MERGED, the checkout is clean, and the branch tip carries nothing the merge did not
   take. A linked worktree is removed from the main checkout (ship re-roots there when launched
   from inside it); a standalone per-PR clone — the only worktree of its own repository, borrowing
   objects from a sibling checkout (`git clone --shared`) AND marked disposable by its creator
   (`agent-tools.disposableClone=true` in its own .git/config, agent-tools#979) — is removed as a
   directory from its parent; any other clone, however it was made, is never deleted, only moved
   off the merged branch;
4. fast-forward the LOCAL base ref when it exists and is safe to move: never a checkout switch,
   never a stash, never a dirty tree.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass, field

from shiplib.ctx import Ctx, Result
from shiplib.wt_state import wt_status


# ---- agent-hook wiring that may still point into a worktree (agent-tools#905) -----------------
def ship_hook_refs(ctx: Ctx, wt: str) -> Result:
    """Scan the installed hook wiring for references to `wt` (hook_refs.py: rc 0 none / 3 found /
    4 incomplete). A missing scanner is incomplete, never "none"."""
    if not ctx.ship_hook_refs_src:
        return Result(4, f"{os.path.join(ctx.self_dir, 'hook_refs.py')}: missing or unreadable\n")
    repos = [ctx.main_checkout] if os.path.isdir(ctx.main_checkout) else []
    res = ctx.cmd([ctx.python, "-I", "-c", ctx.ship_hook_refs_src, wt, *repos], stderr="stdout")
    res.out = res.out.rstrip("\n") + "\n" if res.out else res.out
    return res


def shell_quote(path: str) -> str:
    return shlex.quote(path)


def ship_report_hook_refs(ctx: Ctx, wt: str, res: Result, found_msg: str, incomplete_msg: str) -> None:
    ctx.err(found_msg if res.rc == 3 else f"{incomplete_msg} (scanner exit {res.rc})")
    for line in res.out.rstrip("\n").split("\n"):
        ctx.err(f"[ship]   {line}")
    main_q = shell_quote(ctx.main_checkout)
    ctx.err(f"[ship]   Fix, once {ctx.default_branch} contains this merge — re-point the hooks at the main checkout:")
    ctx.err(f"[ship]     rig apply commit --only agent_hooks -C {main_q}")
    ctx.err(f"[ship]   (a settings bridge or agent-tools/env line belongs to other rig areas: run a full rig apply commit -C {main_q})")
    checker = os.path.join(ctx.main_checkout, "ci", "ship", "hook_refs.py")
    if not os.path.isfile(checker):
        checker = os.path.join(ctx.self_dir, "hook_refs.py")
    if os.path.isfile(checker):
        ctx.err("[ship]   then confirm nothing references the tree (exit 0):")
        ctx.err(f"[ship]     python3 {shell_quote(checker)} {shell_quote(wt)} {main_q}")


def report_keep_hook_backed_worktree(ctx: Ctx, wt: str, res: Result) -> None:
    """Say why `wt` is KEPT (installed hooks still run code from it, or the scan was incomplete)
    and how to remove it once that is fixed. Any scanner failure keeps the tree."""
    ship_report_hook_refs(
        ctx, wt, res,
        f"[ship] keeping worktree {wt}: installed agent hooks still execute code from it — removing it would break them (an on_error: open gate would stop gating machine-wide, agent-tools#905):",
        f"[ship] keeping worktree {wt}: could not fully scan the installed agent-hook wiring for references to it, so it may still back a live hook",
    )
    ctx.err("[ship]   and only then remove it:")
    main_real = os.path.realpath(ctx.main_checkout) if os.path.isdir(ctx.main_checkout) else ""
    if main_real and os.path.dirname(wt) == f"{main_real}/.worktrees":
        ctx.err(f"[ship]     rig worktree remove {shell_quote(os.path.basename(wt))} -C {shell_quote(ctx.main_checkout)}")
    else:
        ctx.err(f"[ship]     git -C {shell_quote(ctx.main_checkout)} worktree remove {shell_quote(wt)}")
        ctx.err(f"[ship]     git -C {shell_quote(ctx.main_checkout)} branch -D {shell_quote(ctx.branch)}")


def ship_warn_hooks_left_dangling(ctx: Ctx, wt: str) -> None:
    """Re-scan after removal: a `rig apply` run from the tree while it was being removed can still
    point a hook into it."""
    res = ship_hook_refs(ctx, wt)
    if res.rc == 0:
        return
    ship_report_hook_refs(
        ctx, wt, res,
        f"[ship] WARNING: installed agent hooks started pointing into {wt} while it was being removed — they now fail (an on_error: open gate is open machine-wide until fixed):",
        f"[ship] WARNING: removed {wt}, but the re-scan of the installed agent-hook wiring was incomplete — a hook may now point at the deleted tree",
    )


# ---- what the PR's merge actually took ----------------------------------------------------------
def resolve_base_branch(ctx: Ctx) -> str:
    """The PR's real base (`baseRefName`); SHIP_DEFAULT_BRANCH only when gh cannot answer."""
    res = ctx.gh("pr", "view", ctx.pr, "--json", "baseRefName", "-q", ".baseRefName", stderr="devnull")
    base = res.out.strip() if res.rc == 0 else ""
    if not base or base.startswith("-") or re.search(r"[^A-Za-z0-9._/-]", base):
        return ctx.default_branch
    return base


def read_back_merge(ctx: Ctx) -> tuple[str, str]:
    """(state, merged head oid) of the PR as the platform reports it now; ("", "") if unreadable."""
    res = ctx.gh("pr", "view", ctx.pr, "--json", "state,headRefOid", "-q", r'[.state,.headRefOid]|@tsv', stderr="devnull")
    if res.rc != 0:
        return "", ""
    state, _, oid = res.out.rstrip("\n").partition("\t")
    return state, oid


def unmerged_reason(ctx: Ctx, repo: str, state: str, head_oid: str, ref: str = "") -> str:
    """Why the local branch may NOT be discarded ("" = it carries nothing the merge did not take).

    A squash-merge never makes the branch tip an ancestor of the base, so ask the platform:
    the PR must be MERGED and the local tip must be the merged head (or lie behind it).
    `ref` names what to judge (default: the PR branch; `HEAD` for a checkout found by commit)."""
    ref = ref or f"refs/heads/{ctx.branch}"
    if state != "MERGED":
        return f"the platform does not report PR #{ctx.pr} as MERGED (state: {state or 'unreadable'})"
    if not head_oid:
        return f"the platform did not report the merged head of PR #{ctx.pr}"
    tip = ctx.git("-C", repo, "rev-parse", "--verify", "-q", ref, stderr="devnull")
    if tip.rc != 0:
        return ""
    tip_oid = tip.out.strip()
    if tip_oid == head_oid:
        return ""
    if ctx.git("-C", repo, "merge-base", "--is-ancestor", tip_oid, head_oid, stderr="devnull").rc == 0:
        return ""
    return f"local {ref.removeprefix('refs/heads/')} ({tip_oid[:12]}) has commits that are not part of the merged head {head_oid[:12]}"


def other_local_work(ctx: Ctx, repo: str) -> str:
    """What a standalone clone holds besides the PR branch that deleting it would destroy."""
    if ctx.git("-C", repo, "fetch", "--quiet", "--prune", "origin", stderr="devnull").rc != 0:
        return "origin cannot be reached to verify that every local commit is pushed"
    refs = ctx.git("-C", repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", stderr="devnull").out.split()
    for ref in refs:
        if ref == ctx.branch:
            continue
        contained = ctx.git("-C", repo, "branch", "-r", "--contains", f"refs/heads/{ref}", stderr="devnull")
        if contained.rc != 0 or not contained.out.strip():
            return f"local branch {ref} has commits that exist on no remote branch"
    if ctx.git("-C", repo, "stash", "list", stderr="devnull").out.strip():
        return "it holds a stash"
    local_tags = set(ctx.git("-C", repo, "tag", "--list", stderr="devnull").out.split())
    if local_tags:
        remote = ctx.git("-C", repo, "ls-remote", "--tags", "--refs", "origin", stderr="devnull")
        remote_tags = {line.split("refs/tags/", 1)[1] for line in remote.out.split("\n") if "refs/tags/" in line}
        if remote.rc != 0 or local_tags - remote_tags:
            return "it holds a local tag that exists on no remote"
    return ""


def worktree_paths(ctx: Ctx, repo: str) -> list[str]:
    res = ctx.git("-C", repo, "worktree", "list", "--porcelain", stderr="devnull")
    return [line[len("worktree "):] for line in res.out.split("\n") if line.startswith("worktree ")]


# Ignored directories that are rebuilt from the tracked tree: losing them loses nothing.
REBUILDABLE_IGNORED = frozenset({
    "node_modules", ".nx", "dist", "build", "out", "target", "coverage", ".turbo", ".next", ".astro",
    ".cache", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".DS_Store",
})


# The positive fact "this clone was made for one PR and may be deleted with it", written into the
# clone's OWN .git/config by whatever creates such clones (the Conloca clone recipe, agent-tools#979).
# Read with --local so a machine-wide `git config --global` cannot mark every clone on the box.
DISPOSABLE_CLONE_KEY = "agent-tools.disposableClone"


def borrows_from_sibling_checkout(repo: str) -> bool:
    """`git clone --shared/--reference` from another LOCAL working checkout: the clone's
    objects/info/alternates names the object store of a sibling checkout that still exists. A
    clone of a mirror (a bare repository) does not qualify. A necessary condition only — a
    developer's own long-lived --reference clone looks exactly the same."""
    alt = os.path.join(repo, ".git", "objects", "info", "alternates")
    try:
        with open(alt, encoding="utf-8", errors="surrogateescape") as fh:
            targets = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    except OSError:
        return False
    repo_real = os.path.realpath(repo)
    for target in targets:
        objects = os.path.realpath(target if os.path.isabs(target) else os.path.join(repo, ".git", "objects", target))
        git_dir = os.path.dirname(objects)
        sibling = os.path.dirname(git_dir)
        if os.path.basename(git_dir) == ".git" and os.path.isdir(sibling) and sibling != repo_real:
            return True
    return False


def is_marked_disposable(ctx: Ctx, repo: str) -> bool:
    res = ctx.git("-C", repo, "config", "--local", "--type=bool", "--get", DISPOSABLE_CLONE_KEY, stderr="devnull")
    return res.rc == 0 and res.out.strip() == "true"


def ignored_data(ctx: Ctx, repo: str) -> str:
    """The first ignored path a deletion would destroy and a rebuild would not bring back (.env
    files, local notes, credentials); "" when only rebuildable artifacts are ignored."""
    res = ctx.git("-C", repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", stderr="devnull")
    if res.rc != 0:
        return "its ignored files could not be listed"
    for line in res.out.split("\n"):
        path = line.rstrip("/")
        if not path or REBUILDABLE_IGNORED.intersection(path.split("/")):
            continue
        if not line.endswith("/"):
            return f"it holds an ignored file that cannot be rebuilt ({path})"
        # `--directory` collapsed a directory that is ignored as a whole: look inside it, a
        # rebuildable artifact may sit below an otherwise ordinary path (packages/web/node_modules).
        inner = ctx.git("-C", repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--", path, stderr="devnull")
        if inner.rc != 0:
            return f"its ignored files could not be listed ({path})"
        for found in inner.out.split("\n"):
            if found and not REBUILDABLE_IGNORED.intersection(found.split("/")):
                return f"it holds an ignored file that cannot be rebuilt ({found})"
    return ""


# ---- base-branch refresh ------------------------------------------------------------------------
def refresh_base(ctx: Ctx, base: str, repo: str) -> None:
    """Fast-forward the local `base` ref to origin/<base> when it exists and is safe.

    Safe = no checkout is switched, nothing is stashed, and a checkout that holds `base` with any
    uncommitted change is skipped. A base that is checked out nowhere is moved with update-ref."""
    ctx.out(f"[ship] refreshing main checkout at {repo} ...")
    ctx.run(["git", "-C", repo, "fetch", "origin"], check=False)
    if ctx.dry_run:
        return
    if ctx.git("-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{base}").rc != 0:
        ctx.out(f"[ship] no local branch {base} in {repo} — nothing to fast-forward.")
        return
    remote = f"refs/remotes/origin/{base}"
    if ctx.git("-C", repo, "show-ref", "--verify", "--quiet", remote).rc != 0:
        ctx.err(f"[ship] WARNING: origin has no {base} — local {base} not fast-forwarded.")
        return
    holder = ""
    listing = ctx.git("-C", repo, "worktree", "list", "--porcelain", stderr="devnull").out.split("\n")
    current = ""
    for line in listing:
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch refs/heads/{base}":
            holder = current
            break
    if holder:
        if ctx.git("-C", holder, "status", "--porcelain", "--untracked-files=normal").out.strip():
            ctx.out(f"[ship] local {base} in {holder} not fast-forwarded: the checkout has uncommitted changes — skipped; run `git -C {shell_quote(holder)} pull --ff-only` there when it is clean.")
            return
        if ctx.git("-C", holder, "merge", "--ff-only", "-q", remote).rc != 0:
            ctx.err(f"[ship] WARNING: could not fast-forward {base} in {holder} — pull manually.")
        return
    old = ctx.git("-C", repo, "rev-parse", f"refs/heads/{base}").out.strip()
    if ctx.git("-C", repo, "merge-base", "--is-ancestor", old, remote).rc != 0:
        ctx.err(f"[ship] WARNING: local {base} has diverged from origin/{base} — not fast-forwarded.")
        return
    new = ctx.git("-C", repo, "rev-parse", remote).out.strip()
    if ctx.git("-C", repo, "update-ref", "-m", "ship: fast-forward after merge", f"refs/heads/{base}", new, old).rc != 0:
        ctx.err(f"[ship] WARNING: could not fast-forward {base} in {repo} — pull manually.")


# ---- the step -----------------------------------------------------------------------------------
def delete_remote_branch(ctx: Ctx) -> None:
    if ctx.provider_resolved == "forgejo":
        # Through the API with the credential that merged, not `git push --delete` (tg-cli #513).
        rc = ctx.run([ctx.python, "-I", ctx.ship_forgejo_py, "delete-head", ctx.pr], check=False)
        if rc not in (0, 3):
            ctx.err(f"[ship] WARNING: could not delete remote branch {ctx.branch} through the Forgejo API (see above) — #{ctx.pr} IS merged. Delete it by hand, or sweep every merged branch: python3 {shell_quote(ctx.ship_forgejo_py)} prune-merged --apply")
    elif str(ctx.cross_repo).lower() == "true":
        ctx.out("[ship] PR head is a fork — leaving its branch to the fork owner.")
    elif ctx.git("ls-remote", "--exit-code", "--heads", "origin", ctx.branch, stdout="devnull", stderr="devnull").rc == 0:
        ctx.out(f"[ship] deleting remote branch origin/{ctx.branch} ...")
        if ctx.run(["git", "push", "-q", "origin", "--delete", ctx.branch], check=False) != 0:
            ctx.err(f"[ship] WARNING: could not delete remote branch origin/{ctx.branch} — delete it manually.")


def remove_worktree(ctx: Ctx, wt: str, is_clean: bool) -> bool:
    """git worktree remove (plain first; --force only for a confirmed-clean tree)."""
    if ctx.run(["git", "worktree", "remove", wt], check=False, stderr="devnull") == 0:
        return True
    return is_clean and ctx.run(["git", "worktree", "remove", "--force", wt], check=False) == 0


def remove_clone_directory(ctx: Ctx, wt: str) -> bool:
    if ctx.dry_run:
        ctx.out(f"[dry-run] rm -rf {wt}")
        return True
    try:
        shutil.rmtree(wt)
    except OSError as exc:
        ctx.err(f"[ship] WARNING: could not delete clone {wt} ({exc}) — remove it manually.")
        return False
    return True


def park_on_base(ctx: Ctx, wt: str, base: str) -> bool:
    """Move a kept, clean, merged checkout off the merged branch so the branch can be deleted:
    onto the local base when there is one, else detached at origin/<base> (fetched first, so the
    merge is in it)."""
    ctx.run(["git", "-C", wt, "fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], check=False, stderr="devnull")
    if ctx.git("-C", wt, "show-ref", "--verify", "--quiet", f"refs/heads/{base}").rc == 0:
        return ctx.run(["git", "-C", wt, "checkout", "--quiet", base], check=False) == 0
    if ctx.git("-C", wt, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base}").rc == 0:
        return ctx.run(["git", "-C", wt, "checkout", "--quiet", "--detach", f"origin/{base}"], check=False) == 0
    return False


@dataclass
class CheckoutPlan:
    """What to do with one checkout that holds the PR branch, decided without changing anything.

    `notices` are the reasons and warnings to print (stream, text) before acting; every `keep`
    plan has at least one, so no checkout is left behind silently."""

    wt: str
    wt_real: str
    action: str  # "keep" | "keep_hook_backed" | "remove_clone" | "park" | "remove_worktree"
    notices: list[tuple[str, str]] = field(default_factory=list)
    kept_branch_reason: str = ""  # why the local branch must stay too ("" = no objection)
    hook_refs: Result | None = None  # the failing scan, for "keep_hook_backed"
    inside: bool = False  # ship was launched from inside this checkout
    is_clean: bool = False
    reroot_target: str = ""  # main checkout to chdir into before removing a worktree we are inside


@dataclass
class CheckoutOutcome:
    self_removed: str = ""  # directory to cd into when the checkout we launched from is gone
    kept_branch_reason: str = ""


def plan_checkout(ctx: Ctx, wt: str, state: str, head_oid: str, base: str) -> CheckoutPlan:
    """Decide how to handle `wt`. Inspects git state; mutates nothing and prints nothing."""
    wt_real = os.path.realpath(wt) if os.path.isdir(wt) else wt
    st = wt_status(ctx, wt, base)
    is_clean = st.rc == 0 and not st.out.strip()

    scan = ship_hook_refs(ctx, wt)
    if scan.rc != 0:
        return CheckoutPlan(wt, wt_real, "keep_hook_backed", hook_refs=scan)

    inside = ctx.orig_pwd == wt_real or ctx.orig_pwd.startswith(wt_real + "/")
    main_real = os.path.realpath(ctx.main_checkout) if os.path.isdir(ctx.main_checkout) else ""
    sole = len(worktree_paths(ctx, wt)) == 1
    common = {"wt": wt, "wt_real": wt_real, "inside": inside, "is_clean": is_clean}

    if not is_clean and (inside or sole):
        # Never re-root into, force away or delete a tree whose state cannot be proven clean.
        return CheckoutPlan(
            **common, action="keep",
            notices=[("err", f"[ship] WARNING: PR worktree {wt_real} has uncommitted/unverifiable changes — leaving it in place (refusing to --force-remove unshipped work).")],
        )

    # A checkout found by commit (another branch name / detached) is judged by its own HEAD.
    ref = "HEAD" if wt in ctx.wt_aliases else ""
    reason = "" if (ctx.dry_run or not is_clean) else unmerged_reason(ctx, wt, state, head_oid, ref)
    if reason:
        return CheckoutPlan(**common, action="keep", kept_branch_reason=reason,
                            notices=[("err", f"[ship] keeping {wt}: {reason}.")])

    if sole:
        # The PR checkout is the only worktree of its repository: nothing to re-root into.
        # Disposable = its creator marked it (`git config --local agent-tools.disposableClone
        # true`) AND it is a `--shared/--reference` copy of a sibling working checkout. The marker
        # alone is not enough (a marked clone with a private object store holds the only copy of
        # its objects) and the alternates alone is not enough (a developer's long-lived
        # --reference clone has them too).
        borrows = borrows_from_sibling_checkout(wt_real)
        marked = is_marked_disposable(ctx, wt_real)
        if borrows and marked:
            leftover = "" if ctx.dry_run else (other_local_work(ctx, wt) or ignored_data(ctx, wt))
            if leftover:
                return CheckoutPlan(**common, action="keep", kept_branch_reason=leftover,
                                    notices=[("err", f"[ship] keeping clone {wt}: {leftover}.")])
            return CheckoutPlan(**common, action="remove_clone")
        # A plain or unmarked clone is somebody's working copy: never deleted, only moved off the dead branch.
        notices = []
        if borrows and not marked:
            notices.append(("out", f"[ship] {wt} borrows objects from a sibling checkout but is not marked as a disposable per-PR clone — keeping the directory (ship deletes only a marked clone: git -C {shell_quote(wt_real)} config --local {DISPOSABLE_CLONE_KEY} true)."))
        return CheckoutPlan(**common, action="park", notices=notices)

    reroot_target = ""
    if inside:
        if not main_real or main_real == wt_real or main_real.startswith(wt_real + "/"):
            return CheckoutPlan(
                **common, action="keep",
                notices=[("err", f"[ship] running inside the PR's worktree ({wt_real}) and no separate main checkout to re-root into — leaving it in place; remove it manually.")],
            )
        reroot_target = main_real
    return CheckoutPlan(**common, action="remove_worktree", reroot_target=reroot_target)


def execute_checkout_plan(ctx: Ctx, plan: CheckoutPlan, base: str) -> CheckoutOutcome:
    """Carry out one `CheckoutPlan`: print its notices, then act. Only warns, never raises."""
    wt, wt_real = plan.wt, plan.wt_real
    outcome = CheckoutOutcome(kept_branch_reason=plan.kept_branch_reason)
    if plan.action == "keep_hook_backed":
        report_keep_hook_backed_worktree(ctx, wt, plan.hook_refs)
        return outcome
    for stream, text in plan.notices:
        (ctx.err if stream == "err" else ctx.out)(text)
    if plan.action == "keep":
        return outcome

    if plan.action == "remove_clone":
        parent = os.path.dirname(wt_real)
        if plan.inside:
            ctx.out(f"[ship] inside the PR's clone ({wt_real}) — leaving it for {parent} to remove it ...")
            os.chdir(parent)
        ctx.out(f"[ship] removing clone {wt} ...")
        if remove_clone_directory(ctx, wt):
            if plan.inside and not ctx.dry_run:
                outcome.self_removed = parent
            if not ctx.dry_run:
                ship_warn_hooks_left_dangling(ctx, wt)
        else:
            outcome.kept_branch_reason = "clone could not be removed"
        return outcome

    if plan.action == "park":
        ctx.out(f"[ship] {wt} is a standalone clone — keeping the directory, moving it off {ctx.branch} ...")
        if not park_on_base(ctx, wt, base):
            outcome.kept_branch_reason = f"could not move {wt} off {ctx.branch}"
            ctx.err(f"[ship] WARNING: {outcome.kept_branch_reason} — switch branches and delete {ctx.branch} manually.")
        return outcome

    # remove_worktree
    if plan.inside:
        ctx.out(f"[ship] inside the PR's worktree ({wt_real}) — re-rooting into {plan.reroot_target} to remove it ...")
        try:
            os.chdir(plan.reroot_target)
        except OSError:
            ctx.err(f"[ship] WARNING: could not cd into {plan.reroot_target} — leaving worktree {wt_real} in place.")
            return outcome
    ctx.out(f"[ship] removing worktree {wt} ...")
    head = ctx.git("-C", wt, "symbolic-ref", "--quiet", "--short", "HEAD", stderr="devnull").out.strip()
    if plan.is_clean and head:  # every checkout here holds the PR head: under the PR branch or another name
        ctx.run(["git", "-C", wt, "checkout", "--detach", "--quiet"], check=False)
    if remove_worktree(ctx, wt, plan.is_clean):
        if plan.reroot_target and not ctx.dry_run:
            outcome.self_removed = plan.reroot_target
        if not ctx.dry_run:
            ship_warn_hooks_left_dangling(ctx, wt)
    else:
        ctx.err(f"[ship] WARNING: could not remove worktree {wt} — remove it manually (git worktree remove --force).")
    return outcome


def cleanup(ctx: Ctx) -> None:
    if ctx.foreign_repo_invoke:
        # --repo targets a foreign remote: every action below would hit THIS checkout's repo.
        origin = f" {ctx.cwd_origin_repo}" if ctx.cwd_origin_repo else ""
        gh_repo = ctx.env.get("GH_REPO", "")
        ctx.out(
            f"[ship] --repo targets {gh_repo} (not this checkout's origin{origin}) — skipping remote branch deletion AND all local branch/worktree cleanup: any local '{ctx.branch}' here belongs to this checkout, not {gh_repo}. GitHub auto-deletes {ctx.branch} in {gh_repo} if auto-delete-head-branch is enabled; otherwise delete it manually."
        )
        return

    # Read the PR back BEFORE the remote branch goes away: some providers drop the head with it.
    if ctx.dry_run:
        state, head_oid = "MERGED", ""
    else:
        state, head_oid = read_back_merge(ctx)
    base = resolve_base_branch(ctx)
    if state == "MERGED":
        delete_remote_branch(ctx)
    else:
        # `gh pr merge` can succeed by ENQUEUEING (merge queue, auto-merge): the PR is still open
        # and deleting its head branch would close it and drop it from the queue. An unreadable
        # state is treated the same way: only a platform-confirmed merge deletes the head.
        shown = state or "unreadable"
        ctx.out(f"[ship] PR #{ctx.pr} is not confirmed MERGED (state: {shown}; still OPEN after the merge call in a merge queue / auto-merge) — keeping remote branch origin/{ctx.branch}.")

    fork = str(ctx.cross_repo).lower() == "true"
    if fork:
        # A fork PR's head name says nothing about a same-named LOCAL branch: never touch it.
        ctx.out(f"[ship] keeping any local '{ctx.branch}' and its checkout: the PR head is a fork's branch, not this repository's.")

    self_removed = ""
    kept_branch_reason = ""
    sweep_ok = True
    wts = [] if fork else list(ctx.wts)
    for wt in wts:
        if not wt:
            continue
        # Plan and execute ONE checkout at a time: removing the first one changes what the next
        # looks like (`sole` in plan_checkout), so the plans cannot all be made up front.
        plan = plan_checkout(ctx, wt, state, head_oid, base)
        outcome = execute_checkout_plan(ctx, plan, base)
        # A tree ship kept on purpose (hooks run from it, we are inside it) must not be swept by rig either.
        if plan.action == "keep_hook_backed" or (plan.action == "keep" and plan.inside):
            sweep_ok = False
        self_removed = outcome.self_removed or self_removed
        if wt not in ctx.wt_aliases:  # a kept alias says nothing about the PR branch itself
            kept_branch_reason = outcome.kept_branch_reason or kept_branch_reason

    repo = ctx.main_checkout if os.path.isdir(ctx.main_checkout) else ""
    if repo:
        if not fork:
            delete_local_branch(ctx, repo, state, head_oid, kept_branch_reason)
            delete_alias_branches(ctx, repo, state, head_oid)
        ctx.run(["git", "-C", repo, "worktree", "prune"], check=False)
        refresh_base(ctx, base, repo)
        if state == "MERGED" and not fork and sweep_ok:  # a queued PR or a fork's head says nothing about local trees
            sweep_landed_worktrees(ctx, repo)

    if self_removed:
        ctx.err(f"[ship] NOTE: removed the worktree you launched from — your shell's cwd is now gone. cd into {self_removed} before your next command.")


def delete_local_branch(ctx: Ctx, repo: str, state: str, head_oid: str, kept_reason: str) -> None:
    """Drop the local branch once no checkout holds it — and only if the merge took all of it."""
    if ctx.git("-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{ctx.branch}").rc != 0:
        return
    reason = kept_reason or ("" if ctx.dry_run else unmerged_reason(ctx, repo, state, head_oid))
    if reason:
        ctx.err(f"[ship] keeping local branch {ctx.branch}: {reason}.")
        return
    if ctx.run(["git", "-C", repo, "branch", "-D", ctx.branch], check=False) != 0:
        ctx.err(f"[ship] WARNING: could not delete local branch {ctx.branch} (still checked out somewhere?) — delete it manually.")


def delete_alias_branches(ctx: Ctx, repo: str, state: str, head_oid: str) -> None:
    """Drop the other-named local branches whose checkout was just removed — same rule as the PR
    branch: only when the merge took everything on them, and never while still checked out."""
    for branch in sorted({b for b in ctx.wt_aliases.values() if b}):
        ref = f"refs/heads/{branch}"
        if ctx.git("-C", repo, "show-ref", "--verify", "--quiet", ref).rc != 0:
            continue
        reason = "" if ctx.dry_run else unmerged_reason(ctx, repo, state, head_oid, ref)
        if reason:
            ctx.err(f"[ship] keeping local branch {branch}: {reason}.")
        elif ctx.run(["git", "-C", repo, "branch", "-D", branch], check=False, stderr="devnull") != 0:
            ctx.err(f"[ship] keeping local branch {branch}: still checked out or protected — delete it manually if wanted.")


def sweep_landed_worktrees(ctx: Ctx, repo: str) -> None:
    """Hand whatever ship cannot attribute to this PR (renamed branches with extra commits, review
    copies, other merged PRs' leftovers) to rig's repo-agnostic sweeper, after the base refresh so it
    judges against the fresh origin/<base>. rig removes only clean trees whose commits all landed
    there or sit on a remote branch that stays; ship never forces anything itself here."""
    if not ctx.which("rig"):
        return
    # rig's sweeper does not yet check installed hook wiring (rig-cli#695): a landed, clean tree that
    # hooks still run code from would be deleted and the hooks would break machine-wide. Run ship's own
    # scan on every other worktree and skip the sweep while any is referenced or unreadable.
    for other in worktree_paths(ctx, repo)[1:]:
        other_real = os.path.realpath(other)
        if ctx.orig_pwd == other_real or ctx.orig_pwd.startswith(other_real + "/"):
            ctx.err(f"[ship] skipping rig worktree gc: ship was launched from {other}, which the sweep could remove.")
            return
        scan = ship_hook_refs(ctx, other)
        if scan.rc != 0:
            ctx.err(f"[ship] skipping rig worktree gc: installed hooks reference {other} (or the hook scan was incomplete, exit {scan.rc}) and rig's sweeper does not check that yet (rig-cli#695).")
            return
    if ctx.run(["rig", "worktree", "gc", "--repo", repo, "--yes"], check=False) != 0:
        ctx.err(f"[ship] WARNING: rig worktree gc failed — run it by hand: rig worktree gc --repo {shell_quote(repo)} --yes")


def run(ctx: Ctx) -> None:
    # Read now: shipping agent-tools from its own PR worktree runs THAT tree's ci/ship, and the
    # post-removal re-scan runs after the tree (and this file) is gone.
    try:
        with open(os.path.join(ctx.self_dir, "hook_refs.py"), encoding="utf-8", errors="surrogateescape") as fh:
            ctx.ship_hook_refs_src = fh.read()
    except OSError:
        ctx.ship_hook_refs_src = ""
    # Report the merge FIRST (the durable result), then clean up in a way that can only warn.
    if ctx.dry_run:
        ctx.out(f"[ship] [dry-run] would merge #{ctx.pr}, then clean up.")
    else:
        ctx.out(f"[ship] merged #{ctx.pr} ({ctx.branch}) — running best-effort cleanup ...")
    try:
        cleanup(ctx)
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask a merged PR
        ctx.err(f"[ship] WARNING: post-merge cleanup hit an error ({exc}) — #{ctx.pr} IS merged; finish cleanup manually.")
    if ctx.dry_run:
        ctx.out(f"[ship] [dry-run] complete — nothing changed for #{ctx.pr}.")
    else:
        ctx.out(f"[ship] shipped #{ctx.pr} — merged + cleaned up.")
