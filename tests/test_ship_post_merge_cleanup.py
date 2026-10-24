"""Post-merge cleanup of `gh ship` (agent-tools#974).

What a merged ship must leave behind:

* the PR's BASE branch comes from the PR (`baseRefName`), never an assumed `main`; its local ref
  is fast-forwarded only when it exists and is safe to move (no checkout switch, no stash);
* the PR head's checkout — a linked worktree OR a standalone `git clone --shared` per-PR clone —
  and its local branch are removed after the PR is confirmed MERGED and the branch carries nothing
  the merge did not take, even when ship was launched from inside that checkout;
* a dirty checkout, a checkout with extra local commits and a clone that is not a disposable
  per-PR clone are never removed.

Hermetic like tests/test_ship.py: real temp git repos, the shared fake `gh` on PATH. The fake's
`pr merge` runs $SHIP_TEST_MERGE_CMD, which is how a test lands the squash commit on the base.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from test_ship import _SHIP, _fake_gh_dir, _git, _sh, _write_descriptor, hook_home  # noqa: F401 (fixture)

BASE = "private"  # the repo's base branch; there is deliberately no `main` anywhere
# The marker a per-PR clone's creator writes; spelled out here (not imported) so renaming it in
# ship without updating the creators' recipe fails a test.
DISPOSABLE_MARKER = "agent-tools.disposableClone"


def _commit(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", msg, cwd=repo)


def _identity(repo: Path) -> None:
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)


class Setup:
    """origin (bare, base `private`), a primary clone on `private`, and one PR branch `feat`."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        _sh("git", "init", "--bare", "-b", BASE, "-q", str(self.origin), cwd=tmp_path)
        self.primary = tmp_path / "primary"
        self.primary.mkdir()
        _git("init", "-q", "-b", BASE, cwd=self.primary)
        _identity(self.primary)
        _git("remote", "add", "origin", str(self.origin), cwd=self.primary)
        _commit(self.primary, "README.md", "# x\n", "init")
        _git("push", "-q", "origin", BASE, cwd=self.primary)
        _git("branch", "feat", cwd=self.primary)
        _git("checkout", "-q", "feat", cwd=self.primary)
        _commit(self.primary, "feat.txt", "feature\n", "feat work")
        _git("push", "-q", "origin", "feat", cwd=self.primary)
        _git("checkout", "-q", BASE, cwd=self.primary)
        _git("branch", "-q", "-D", "feat", cwd=self.primary)

    def worktree(self) -> Path:
        wt = self.tmp / "wt-feat"
        _git("worktree", "add", "-q", "-b", "feat", str(wt), "origin/feat", cwd=self.primary)
        _git("branch", "-q", "--set-upstream-to=origin/feat", "feat", cwd=wt)
        return wt

    def clone(self, *, shared: bool, from_bare: bool = False, marked: bool | None = None) -> Path:
        """A per-PR checkout. `shared` borrows objects from the primary working checkout the way
        `git clone --shared <primary> <dir>` does for real per-PR clones (origin is then repointed
        at the server); `from_bare` borrows from the bare server repo instead (a mirror-style
        clone, which is NOT a disposable per-PR clone). `marked` writes the positive disposable
        marker the creator of a per-PR clone is responsible for (default: marked iff `shared`)."""
        dest = self.tmp / "pr-clone"
        source = self.origin if (from_bare or not shared) else self.primary
        flags = ["--shared"] if shared else []
        _git("clone", "-q", *flags, str(source), str(dest), cwd=self.tmp)
        _git("remote", "set-url", "origin", str(self.origin), cwd=dest)
        _git("fetch", "-q", "origin", cwd=dest)
        _identity(dest)
        _git("checkout", "-q", "-B", "feat", "origin/feat", cwd=dest)
        if shared if marked is None else marked:
            _git("config", "--local", DISPOSABLE_MARKER, "true", cwd=dest)
        return dest

    def merge_cmd(self) -> str:
        """Shell snippet the fake `gh pr merge` runs: land the squash on origin/<base>."""
        pusher = self.tmp / "pusher"
        return (
            f"if [ ! -d '{pusher}' ]; then git clone -q '{self.origin}' '{pusher}'; "
            f"git -C '{pusher}' config user.email t@t; git -C '{pusher}' config user.name t; fi; "
            f"git -C '{pusher}' checkout -q {BASE}; echo squashed > '{pusher}/squashed.txt'; "
            f"git -C '{pusher}' add -A; git -C '{pusher}' commit -qm squash; "
            f"git -C '{pusher}' push -q origin {BASE}"
        )

    def origin_tip(self, ref: str = BASE) -> str:
        return _git("rev-parse", f"refs/heads/{ref}", cwd=self.origin).stdout.strip()

    def origin_has_branch(self, name: str) -> bool:
        return _sh("git", "rev-parse", "--verify", "-q", f"refs/heads/{name}", cwd=self.origin).returncode == 0


@pytest.fixture
def setup(tmp_path):
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")
    return Setup(tmp_path)


def _ship(setup: Setup, cwd: Path, **env_extra):
    """Run ship from `cwd`. NO SHIP_MAIN_CHECKOUT / SHIP_DEFAULT_BRANCH: the base and the primary
    checkout must be derived from the PR and the repo, as in real use."""
    bindir = _fake_gh_dir(setup.tmp)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_TEST_BASE_BRANCH"] = BASE
    env["SHIP_TEST_MERGE_CMD"] = setup.merge_cmd()
    for k in ("SHIP_MAIN_CHECKOUT", "SHIP_DEFAULT_BRANCH"):
        env.pop(k, None)
    env.update(env_extra)
    return _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=cwd, env=env)


def _dump(r) -> str:
    return f"rc={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


def _branches(repo: Path) -> str:
    return _git("branch", "--list", "feat", cwd=repo).stdout


def test_base_comes_from_the_pr_and_primary_is_fast_forwarded(setup):
    """Base `private`, no `main` ref anywhere: no pathspec/remote-ref error, the primary's local
    `private` ends at the commit the merge landed, and the PR worktree + branch are gone."""
    wt = setup.worktree()
    r = _ship(setup, wt)
    assert r.returncode == 0, _dump(r)
    for needle in ("pathspec", "couldn't find remote ref", "could not fast-forward"):
        assert needle not in r.stderr + r.stdout, f"{needle!r} in output:\n{_dump(r)}"
    tip = setup.origin_tip()
    assert _git("rev-parse", BASE, cwd=setup.primary).stdout.strip() == tip, _dump(r)
    assert not wt.exists(), f"PR worktree left behind:\n{_dump(r)}"
    assert _branches(setup.primary) == "", f"local branch left behind:\n{_dump(r)}"
    assert not setup.origin_has_branch("feat"), "remote head branch was not deleted"


def test_primary_on_another_branch_is_never_switched_or_stashed(setup):
    """The primary holds somebody else's branch and uncommitted work: ship may move the local
    base ref (it is not checked out anywhere) but must not check anything out, stash, or touch
    the working tree."""
    _git("checkout", "-q", "-b", "scratch", cwd=setup.primary)
    (setup.primary / "wip.txt").write_text("someone's wip\n", encoding="utf-8")
    wt = setup.worktree()
    r = _ship(setup, wt)
    assert r.returncode == 0, _dump(r)
    assert _git("symbolic-ref", "--short", "HEAD", cwd=setup.primary).stdout.strip() == "scratch", _dump(r)
    assert (setup.primary / "wip.txt").read_text() == "someone's wip\n"
    assert _git("stash", "list", cwd=setup.primary).stdout == "", "ship must never stash"
    assert _git("rev-parse", BASE, cwd=setup.primary).stdout.strip() == setup.origin_tip(), _dump(r)
    assert not wt.exists(), _dump(r)


def test_dirty_primary_on_the_base_is_skipped_not_autostashed(setup):
    """Primary on the base branch with an uncommitted change that would conflict with the merged
    file: ship says it skipped the refresh, keeps the change byte-for-byte and exits 0."""
    (setup.primary / "squashed.txt").write_text("local edit\n", encoding="utf-8")
    wt = setup.worktree()
    r = _ship(setup, wt)
    assert r.returncode == 0, _dump(r)
    assert (setup.primary / "squashed.txt").read_text() == "local edit\n"
    assert _git("stash", "list", cwd=setup.primary).stdout == "", "ship must never stash"
    assert "not fast-forwarded" in r.stdout + r.stderr, f"no skip notice:\n{_dump(r)}"
    assert not wt.exists(), _dump(r)


def test_shared_clone_is_removed_from_inside_after_a_verified_merge(setup):
    """The per-PR `git clone --shared` checkout is the only worktree of its own repo, so there is
    no other checkout to re-root into: ship must still remove it (with its branch) and the
    remote head branch, leave the primary clone alone and tell the caller its cwd is gone."""
    clone = setup.clone(shared=True)
    assert (clone / ".git" / "objects" / "info" / "alternates").exists()
    primary_head = _git("rev-parse", "HEAD", cwd=setup.primary).stdout.strip()
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert "leaving it in place" not in r.stdout + r.stderr, _dump(r)
    assert not clone.exists(), f"per-PR clone left behind:\n{_dump(r)}"
    assert not setup.origin_has_branch("feat"), "remote head branch was not deleted"
    assert "your shell's cwd is now gone" in r.stderr, _dump(r)
    assert _git("rev-parse", "HEAD", cwd=setup.primary).stdout.strip() == primary_head, "primary moved"


def test_unmarked_reference_clone_is_kept_even_though_its_alternates_name_a_sibling_checkout(setup):
    """A developer's own long-lived `git clone --reference/--shared` looks exactly like a per-PR
    clone through objects/info/alternates: without the positive disposable marker ship must not
    rmtree it (agent-tools#979). Clean and merged, it is only moved off the merged branch."""
    clone = setup.clone(shared=True, marked=False)
    assert (clone / ".git" / "objects" / "info" / "alternates").exists()
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), f"an unmarked --shared clone was deleted:\n{_dump(r)}"
    assert _git("symbolic-ref", "--short", "HEAD", cwd=clone).stdout.strip() == BASE, _dump(r)
    assert _branches(clone) == "", _dump(r)
    assert DISPOSABLE_MARKER in r.stdout + r.stderr, f"ship did not say how to mark a disposable clone:\n{_dump(r)}"


def test_marker_set_to_anything_but_true_does_not_make_a_clone_disposable(setup):
    clone = setup.clone(shared=True, marked=False)
    _git("config", "--local", DISPOSABLE_MARKER, "false", cwd=clone)
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)


def test_marker_in_global_config_does_not_make_every_clone_disposable(setup):
    """The marker is a per-clone fact: a machine-wide `git config --global` must not turn every
    long-lived clone on the box into a deletable one."""
    clone = setup.clone(shared=True, marked=False)
    gitconfig = setup.tmp / "global-gitconfig"
    gitconfig.write_text(f"[agent-tools]\n\tdisposableClone = true\n", encoding="utf-8")
    r = _ship(setup, clone, GIT_CONFIG_GLOBAL=str(gitconfig))
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), f"a global marker deleted a clone:\n{_dump(r)}"


def test_marker_on_a_clone_with_no_alternates_is_not_enough_to_delete_it(setup):
    """Every existing keep-guard still applies: a marked plain clone has no shared object store
    to prove it is a per-PR copy of a sibling checkout, so it is parked, never deleted."""
    clone = setup.clone(shared=False, marked=True)
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)


def test_plain_clone_is_kept_but_moved_off_the_merged_branch(setup):
    """A plain `git clone` (no alternates) is somebody's working clone, not a disposable per-PR
    one: it is never deleted. Clean and merged, it is moved onto the fast-forwarded base and the
    merged branch is dropped, instead of staying on a dead branch."""
    clone = setup.clone(shared=False)
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)
    assert _git("symbolic-ref", "--short", "HEAD", cwd=clone).stdout.strip() == BASE, _dump(r)
    assert _git("rev-parse", "HEAD", cwd=clone).stdout.strip() == setup.origin_tip(), _dump(r)
    assert _branches(clone) == "", _dump(r)


def test_shared_clone_dirtied_after_the_preflight_is_kept(setup):
    clone = setup.clone(shared=True)
    r = _ship(setup, clone, SHIP_TEST_MERGE_DIRTIES=str(clone / "late.txt"))
    assert r.returncode == 0, _dump(r)
    assert (clone / "late.txt").read_text() == "post-merge unshipped\n", _dump(r)
    assert "feat" in _branches(clone), _dump(r)


def test_shared_clone_with_a_commit_the_merge_did_not_take_is_kept(setup):
    """A local commit made after the preflight is not what was merged: removing the clone would
    lose it, so ship keeps clone and branch and says why."""
    clone = setup.clone(shared=True)
    cmd = f"{setup.merge_cmd()}; git -C '{clone}' commit -q --allow-empty -m 'late local commit'"
    r = _ship(setup, clone, SHIP_TEST_MERGE_CMD=cmd)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)
    assert "feat" in _branches(clone), _dump(r)
    assert "not part of the merged" in (r.stdout + r.stderr), _dump(r)


def test_shared_clone_is_kept_when_the_pr_is_not_reported_merged(setup):
    clone = setup.clone(shared=True)
    r = _ship(setup, clone, SHIP_TEST_POST_STATE="OPEN")
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)
    assert "feat" in _branches(clone), _dump(r)
    assert "MERGED" in (r.stdout + r.stderr), _dump(r)


def test_shared_clone_holding_another_local_branch_is_kept(setup):
    """Removing the clone would also destroy its other local branches: keep it."""
    clone = setup.clone(shared=True)
    _git("branch", "scratch-work", cwd=clone)
    _git("checkout", "-q", "scratch-work", cwd=clone)
    _commit(clone, "scratch.txt", "unpushed\n", "scratch")
    _git("checkout", "-q", "feat", cwd=clone)
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)
    assert "scratch-work" in _git("branch", "--list", cwd=clone).stdout, _dump(r)


def test_remote_head_branch_is_kept_while_the_pr_is_still_open(setup):
    """A merge queue / auto-merge makes `gh pr merge` succeed without merging: deleting the head
    branch then would close the open PR and drop it from the queue."""
    clone = setup.clone(shared=True)
    r = _ship(setup, clone, SHIP_TEST_POST_STATE="OPEN")
    assert r.returncode == 0, _dump(r)
    assert setup.origin_has_branch("feat"), f"remote head of a still-open PR was deleted:\n{_dump(r)}"
    assert "still OPEN" in r.stdout, _dump(r)


def test_shared_clone_is_removed_when_the_primary_checkout_is_pinned_by_env(setup):
    """SHIP_MAIN_CHECKOUT names a DIFFERENT repository than the per-PR clone: the clone is still
    the only worktree of its own repo, so ship removes it as a directory instead of trying to
    `git worktree remove` it from the primary (which would fail and leave it detached)."""
    clone = setup.clone(shared=True)
    r = _ship(setup, clone, SHIP_MAIN_CHECKOUT=str(setup.primary))
    assert r.returncode == 0, _dump(r)
    assert not clone.exists(), f"per-PR clone left behind:\n{_dump(r)}"
    assert "could not remove worktree" not in r.stderr, _dump(r)
    assert _git("rev-parse", BASE, cwd=setup.primary).stdout.strip() == setup.origin_tip(), _dump(r)


def test_shared_clone_holding_an_unpushed_tag_is_kept(setup):
    """Removing the clone would destroy a tag that exists nowhere else."""
    clone = setup.clone(shared=True)
    _git("tag", "local-only-marker", cwd=clone)
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)
    assert "tag" in r.stdout + r.stderr, _dump(r)


def test_shared_clone_of_a_bare_mirror_is_not_a_disposable_per_pr_clone(setup):
    """Alternates pointing at a bare repository is how a long-lived clone of a mirror looks: ship
    must not rmtree it. It is clean and merged, so it is only moved off the merged branch."""
    clone = setup.clone(shared=True, from_bare=True, marked=True)
    assert (clone / ".git" / "objects" / "info" / "alternates").exists()
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), f"a mirror-style clone was deleted:\n{_dump(r)}"


def test_shared_clone_holding_an_ignored_env_file_is_kept(setup):
    """`git status` does not show ignored files, but deleting the clone would destroy them."""
    clone = setup.clone(shared=True)
    (clone / ".git" / "info" / "exclude").write_text(".env\n.env.local\nnode_modules/\n", encoding="utf-8")
    (clone / ".env.local").write_text("SECRET=1\n", encoding="utf-8")
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert (clone / ".env.local").read_text() == "SECRET=1\n", _dump(r)
    assert "cannot be rebuilt" in r.stdout + r.stderr, _dump(r)


def test_shared_clone_with_only_rebuildable_ignored_dirs_is_removed(setup):
    clone = setup.clone(shared=True)
    (clone / ".git" / "info" / "exclude").write_text("node_modules/\n", encoding="utf-8")
    (clone / "node_modules" / "pkg").mkdir(parents=True)
    (clone / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert not clone.exists(), _dump(r)


def test_shared_clone_is_kept_when_a_local_branch_only_exists_on_a_stale_tracking_ref(setup):
    """origin/stale was deleted on the server but the clone still has the remote-tracking ref: a
    local branch whose commits survive only there must not count as pushed."""
    clone = setup.clone(shared=True)
    _git("checkout", "-q", "-b", "stale", cwd=clone)
    _commit(clone, "stale.txt", "x\n", "stale work")
    _git("push", "-q", "origin", "stale", cwd=clone)
    _git("checkout", "-q", "feat", cwd=clone)
    _git("push", "-q", "origin", "--delete", "stale", cwd=setup.primary)  # the server drops it
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), f"commits that exist nowhere else were destroyed:\n{_dump(r)}"
    assert "stale" in _git("branch", "--list", cwd=clone).stdout


def test_nested_rebuildable_ignored_dirs_do_not_keep_a_shared_clone(setup):
    clone = setup.clone(shared=True)
    (clone / ".git" / "info" / "exclude").write_text("__pycache__/\nnode_modules/\n", encoding="utf-8")
    for rel in ("tests/__pycache__", "packages/web/node_modules/pkg"):
        (clone / rel).mkdir(parents=True)
        (clone / rel / "x").write_text("x\n", encoding="utf-8")
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert not clone.exists(), _dump(r)


def test_plain_clone_without_a_local_base_is_parked_on_the_merged_base(setup):
    """The clone only knows `feat`: after the merge it must land on the base that CONTAINS the
    merge, so ship fetches before it parks the checkout."""
    clone = setup.tmp / "single-branch"
    _git("clone", "-q", "--single-branch", "-b", "feat", str(setup.origin), str(clone), cwd=setup.tmp)
    _identity(clone)
    r = _ship(setup, clone)
    assert r.returncode == 0, _dump(r)
    assert clone.exists(), _dump(r)
    assert _git("rev-parse", "HEAD", cwd=clone).stdout.strip() == setup.origin_tip(), _dump(r)
    assert _branches(clone) == "", _dump(r)


def test_remote_head_branch_is_kept_when_the_read_back_fails(setup):
    """An unreadable state is not a confirmed merge: the remote head stays, and so does the clone."""
    clone = setup.clone(shared=True)
    r = _ship(setup, clone, SHIP_TEST_READBACK_FAIL="1")
    assert r.returncode == 0, _dump(r)
    assert setup.origin_has_branch("feat"), _dump(r)
    assert clone.exists(), _dump(r)


def test_fork_pr_keeps_local_state_but_still_refreshes_the_base(setup):
    """A fork PR's head name is not this repository's branch, so no local branch or checkout is
    touched, but the merge still moved the base: the primary's local base is fast-forwarded."""
    wt = setup.worktree()
    r = _ship(setup, wt, SHIP_TEST_CROSS_REPO="true")
    assert r.returncode == 0, _dump(r)
    assert wt.exists(), _dump(r)
    assert "feat" in _branches(setup.primary) or "feat" in _git("branch", "--list", cwd=wt).stdout, _dump(r)
    assert _git("rev-parse", BASE, cwd=setup.primary).stdout.strip() == setup.origin_tip(), _dump(r)


# --- checkouts of the PR that do not carry its branch name; seeded task.yaml; rig gc ---------------
def _alias_worktree(setup: Setup, *, branch: str | None, name: str = "wt-alias") -> Path:
    """A linked worktree on the PR head commit under another local branch name (or detached)."""
    wt = setup.tmp / name
    if branch is None:
        _git("worktree", "add", "-q", "--detach", str(wt), "origin/feat", cwd=setup.primary)
    else:
        _git("worktree", "add", "-q", "-b", branch, str(wt), "origin/feat", cwd=setup.primary)
    _identity(wt)
    return wt


def _base_tracks_task_yaml(setup: Setup, text: str = "tracker: x\n") -> None:
    """The base branch gained a tracked task.yaml AFTER the PR branch forked, so a PR worktree
    seeded with the same file sees it as an untracked `?? task.yaml`."""
    _commit(setup.primary, "task.yaml", text, "track task.yaml")
    _git("push", "-q", "origin", BASE, cwd=setup.primary)


def test_worktree_on_another_branch_name_is_removed_with_its_branch(setup):
    wt = _alias_worktree(setup, branch="renamed-local")
    r = _ship(setup, setup.primary)
    assert r.returncode == 0, _dump(r)
    assert not wt.exists(), f"worktree on the PR head under another name left behind:\n{_dump(r)}"
    assert _git("branch", "--list", "renamed-local", cwd=setup.primary).stdout == "", _dump(r)
    assert not setup.origin_has_branch("feat")


def test_detached_worktree_on_the_pr_head_commit_is_removed(setup):
    wt = _alias_worktree(setup, branch=None)
    r = _ship(setup, setup.primary)
    assert r.returncode == 0, _dump(r)
    assert not wt.exists(), f"detached worktree on the PR head left behind:\n{_dump(r)}"


def test_detached_worktree_with_extra_commits_is_not_the_prs_and_is_left_alone(setup):
    wt = _alias_worktree(setup, branch=None)
    _commit(wt, "mine.txt", "unshipped\n", "local only")
    r = _ship(setup, setup.primary)
    assert r.returncode == 0, _dump(r)
    assert wt.exists() and (wt / "mine.txt").exists(), f"a tree that is not at the PR head was touched:\n{_dump(r)}"


def test_dirty_alias_worktree_does_not_block_the_ship_and_is_never_forced(setup):
    wt = _alias_worktree(setup, branch="renamed-local")
    (wt / "feat.txt").write_text("edited, not committed\n", encoding="utf-8")
    r = _ship(setup, setup.primary)
    assert r.returncode == 0, _dump(r)
    assert "leaving" in r.stderr and "alone" in r.stderr, _dump(r)
    assert (wt / "feat.txt").read_text() == "edited, not committed\n"
    assert _git("branch", "--list", "renamed-local", cwd=setup.primary).stdout != "", "its branch must stay"
    assert not setup.origin_has_branch("feat")


def test_branch_that_merely_sits_on_the_pr_head_is_not_the_prs_checkout(setup):
    """The next stacked PR just started from the PR's commit: no upstream on the PR branch, so it
    is somebody else's work and ship must neither remove the tree nor delete the branch."""
    wt = setup.tmp / "wt-stacked"
    _git("worktree", "add", "-q", "--no-track", "-b", "part2", str(wt), "origin/feat", cwd=setup.primary)
    r = _ship(setup, setup.primary)
    assert r.returncode == 0, _dump(r)
    assert wt.exists(), f"a stacked branch's tree was removed:\n{_dump(r)}"
    assert _git("branch", "--list", "part2", cwd=setup.primary).stdout != ""


def test_seeded_task_yaml_identical_to_the_base_copy_does_not_make_the_tree_dirty(setup):
    _base_tracks_task_yaml(setup)
    wt = setup.worktree()
    (wt / "task.yaml").write_text("tracker: x\n", encoding="utf-8")
    assert _git("status", "--porcelain", cwd=wt).stdout == "?? task.yaml\n"
    r = _ship(setup, wt)
    assert r.returncode == 0, _dump(r)
    assert "uncommitted" not in r.stderr, _dump(r)
    assert not wt.exists(), f"tree dirty only from the seeded task.yaml was kept:\n{_dump(r)}"
    assert _branches(setup.primary) == "", _dump(r)


def test_task_yaml_that_differs_from_the_base_copy_still_counts_as_unshipped_work(setup):
    _base_tracks_task_yaml(setup)
    wt = setup.worktree()
    (wt / "task.yaml").write_text("tracker: something-i-edited\n", encoding="utf-8")
    r = _ship(setup, wt)
    assert r.returncode == 1, _dump(r)
    assert (wt / "task.yaml").read_text() == "tracker: something-i-edited\n"


def test_task_yaml_next_to_a_real_change_is_never_forced_away_at_cleanup(setup):
    """Clean (task.yaml only) at preflight, then real work lands in the tree while the merge runs:
    the cleanup-time status check must keep the tree, task.yaml or not."""
    _base_tracks_task_yaml(setup)
    wt = setup.worktree()
    (wt / "task.yaml").write_text("tracker: x\n", encoding="utf-8")
    r = _ship(setup, setup.primary, SHIP_TEST_MERGE_DIRTIES=str(wt / "late-work.txt"))
    assert r.returncode == 0, _dump(r)
    assert (wt / "late-work.txt").exists(), f"a tree with real changes was removed:\n{_dump(r)}"


def test_cleanup_hands_the_rest_to_rig_worktree_gc_on_the_primary_checkout(setup):
    log = setup.tmp / "rig.log"
    setup.worktree()
    r = _ship(setup, setup.primary, SHIP_TEST_RIG_LOG=str(log))
    assert r.returncode == 0, _dump(r)
    assert log.read_text().splitlines() == [f"worktree gc --repo {setup.primary} --yes"], _dump(r)


def test_failing_rig_worktree_gc_only_warns(setup):
    setup.worktree()
    r = _ship(setup, setup.primary, SHIP_TEST_RIG_RC="2")
    assert r.returncode == 0, _dump(r)
    assert "rig worktree gc failed" in r.stderr, _dump(r)
    assert "merged + cleaned up" in r.stdout


def test_dry_run_does_not_invoke_rig_worktree_gc(setup):
    log = setup.tmp / "rig.log"
    setup.worktree()
    bindir = _fake_gh_dir(setup.tmp)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "SHIP_TEST_BRANCH": "feat",
           "SHIP_TEST_BASE_BRANCH": BASE, "SHIP_TEST_RIG_LOG": str(log)}
    r = _sh("bash", str(_SHIP), "1", "--dry-run", "--no-screenshot-ok", "test", cwd=setup.primary, env=env)
    assert r.returncode == 0, _dump(r)
    assert not log.exists(), "dry-run must only print the rig command"
    assert "[dry-run] rig worktree gc" in r.stdout, _dump(r)


def test_rig_worktree_gc_is_skipped_when_the_pr_is_not_confirmed_merged(setup):
    log = setup.tmp / "rig.log"
    setup.worktree()
    r = _ship(setup, setup.primary, SHIP_TEST_RIG_LOG=str(log), SHIP_TEST_POST_STATE="OPEN")
    assert r.returncode == 0, _dump(r)
    assert not log.exists(), "a queued PR must not trigger a sweep of other worktrees"


def test_rig_worktree_gc_is_skipped_while_installed_hooks_run_code_from_another_worktree(setup, hook_home):
    """rig's sweeper does not check hook wiring (rig-cli#695): an unrelated landed tree that a hook
    descriptor points into must survive, so ship does not call the sweeper at all."""
    log = setup.tmp / "rig.log"
    other = setup.tmp / "wt-hooked"
    _git("worktree", "add", "-q", "--detach", str(other), BASE, cwd=setup.primary)
    _write_descriptor(hook_home / ".omp" / "agent" / "hooks", other / "README.md")
    wt = setup.worktree()
    r = _ship(setup, setup.primary, SHIP_TEST_RIG_LOG=str(log))
    assert r.returncode == 0, _dump(r)
    assert not wt.exists(), _dump(r)
    assert other.exists()
    assert not log.exists(), f"rig worktree gc ran although a hook points into {other}:\n{_dump(r)}"
    assert "skipping rig worktree gc" in r.stderr and "rig-cli#695" in r.stderr, _dump(r)
