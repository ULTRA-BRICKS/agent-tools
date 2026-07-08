"""Tests for ci/ship/ship.sh — the post-merge worktree cleanup.

Focus: the exit-128 regression where TWO worktrees checked out the same branch and the
script concatenated both paths into one `git worktree remove`. ship.sh must:
  • collect ALL worktrees for the branch and remove each (not crash on the second);
  • report the merge as done even when cleanup hits a problem, never masking a
    successful squash-merge behind a non-zero exit.

Hermetic: a real temp git repo with two worktrees on one branch + a fake `gh` on PATH
(so no network / real PR). ship.sh's own merge step is the fake `gh pr merge` (a no-op);
the worktree/branch cleanup runs for real against the temp repo.

Requires bash + git. Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

# A fake `gh` that answers exactly the calls ship.sh makes (with --skip-ci the CI rollup
# is not queried). Branch name is read from $SHIP_TEST_BRANCH so the test controls it.
_FAKE_GH = """\
#!/usr/bin/env bash
set -e
# When SHIP_TEST_GHREPO_LOG is set, record the GH_REPO gh sees on every invocation. ship.sh
# threads --repo through gh via GH_REPO, so this log proves the flag reached the gh calls
# (and that the no-repo path leaves GH_REPO unset). No-op when the env var is unset.
[ -n "${SHIP_TEST_GHREPO_LOG:-}" ] && printf '%s\\n' "${GH_REPO:-<unset>}" >> "$SHIP_TEST_GHREPO_LOG"
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        # --json headRefName,state,mergeable,isCrossRepository,mergeStateStatus
        if printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        else
          echo '[]'
        fi ;;
      diff) echo "src/a.py" ;;            # --name-only (non-UI path)
      comment) : ;;
      merge)
        # Optionally dirty a worktree AT MERGE TIME (after the clean preflight passed, before
        # cleanup runs) — simulates a merge/post-merge hook writing into the tree. Used to
        # exercise ship.sh's cleanup-time clean-check in isolation from the preflight one.
        [ -n "${SHIP_TEST_MERGE_DIRTIES:-}" ] && printf 'post-merge unshipped\\n' > "${SHIP_TEST_MERGE_DIRTIES}"
        echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    # ship makes two graphql calls: review-threads (unresolved count) and the review-dwell
    # window ([createdAt, pushedDate, committedDate] TSV). Route by query content (--jq ignored).
    if printf '%s ' "$@" | grep -q committedDate; then
      # Defaults OLD so the dwell window is satisfied; a dwell test injects fresh timestamps.
      # SHIP_TEST_PUSHED defaults EMPTY (GitHub pushedDate is often null) → committedDate fallback.
      printf '%s\\t%s\\t%s\\n' "${SHIP_TEST_CREATED:-2020-01-01T00:00:00Z}" "${SHIP_TEST_PUSHED:-}" "${SHIP_TEST_LASTCOMMIT:-2020-01-01T00:00:00Z}"
    else
      echo "${SHIP_TEST_UNRESOLVED:-0}"   # review-threads -> unresolved count
    fi ;;
  *) : ;;
esac
"""


def _clean_git_env() -> dict:
    """A git env immune to the developer box's GLOBAL hooks / config: an empty
    core.hooksPath (so a host `review`-install pre-commit can't block the test's own
    commits) and REVIEW_SKIP set for belt-and-suspenders."""
    env = dict(os.environ)
    env["REVIEW_SKIP"] = "1"
    return env


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    # Neutralize any global core.hooksPath for the test's bootstrap commits.
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=_clean_git_env())
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def repo_with_two_worktrees(tmp_path):
    """A repo on `main` with a branch `feat` checked out in TWO worktrees (the exit-128
    trigger), plus an `origin` remote so ship.sh's remote-branch ops have a target."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    # Branch `feat`, pushed, then checked out in TWO separate worktrees.
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    _git("worktree", "add", "-q", str(wt1), "feat", cwd=main)
    # A second worktree on the SAME branch: git allows it with --force.
    _git("worktree", "add", "-q", "--force", str(wt2), "feat", cwd=main)
    return main, wt1, wt2


def _run_ship(main: Path, bindir: Path, cwd: Path | None = None):
    """Run ship.sh. By default cwd is the main checkout; pass `cwd` to run it from INSIDE a
    worktree (the self-clean path). SHIP_MAIN_CHECKOUT is pinned to the main checkout so the
    re-root target is deterministic regardless of which worktree we launch from."""
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    return _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=cwd or main, env=env,
    )


def _fake_gh_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def test_two_worktrees_one_branch_does_not_exit_128(repo_with_two_worktrees, tmp_path):
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)

    r = _run_ship(main, bindir)

    # The exact regression: two worktrees used to make cleanup exit 128.
    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # Both worktrees for the branch must be gone (removed individually).
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt1) not in remaining, f"wt1 still present:\n{remaining}"
    assert str(wt2) not in remaining, f"wt2 still present:\n{remaining}"


def test_merge_reported_even_if_cleanup_cannot_remove_branch(repo_with_two_worktrees, tmp_path):
    """If a worktree can't be removed (e.g. left checked out / busy), ship must still exit
    0 and report the merge — cleanup failure must NOT mask a successful merge."""
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)

    # Corrupt wt2 so `git worktree remove` (and --force) both fail, simulating a stuck
    # removal. ship.sh must still exit 0 and report the merge.
    gitfile = wt2 / ".git"
    if gitfile.exists():
        gitfile.write_text("gitdir: /nonexistent/broken\n", encoding="utf-8")

    r = _run_ship(main, bindir)

    assert r.returncode == 0, f"ship must exit 0 after a successful merge; got {r.returncode}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # Assert the MASKING-prevention path actually fired — otherwise this test could go
    # false-green on a git where `worktree remove --force` happens to drop the broken tree
    # (the cleanup would then succeed and never exercise the warn-don't-abort branch).
    assert "could not remove worktree" in r.stderr, (
        "the un-removable-worktree path did not fire — test proves nothing:\n" + r.stderr
    )


def test_worktree_path_with_space_is_collected(tmp_path):
    """The fix swapped awk `$2` (splits on whitespace) for `substr($0,10)` so a worktree
    whose path contains a space is parsed whole. With `$2` the path was truncated and the
    branch's worktree went uncollected — cleanup then left it behind (and `git -C` on the
    truncated path exit-128'd). Here a single worktree under a spaced path must be removed."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)

    wt_spaced = tmp_path / "work tree with spaces"
    _git("worktree", "add", "-q", str(wt_spaced), "feat", cwd=main)

    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship(main, bindir)

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # The spaced-path worktree must have been collected and removed — proof the full path
    # (not a `$2`-truncated prefix) was parsed.
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt_spaced) not in remaining, f"spaced worktree still present:\n{remaining}"


@pytest.fixture
def repo_with_pr_worktree(tmp_path):
    """A repo on `main` with branch `feat` checked out in ONE worktree, plus an `origin`
    remote. Mirrors the real swarm setup: ship.sh is invoked FROM INSIDE that worktree."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    wt = tmp_path / "wt-feat"
    _git("worktree", "add", "-q", str(wt), "feat", cwd=main)
    return main, wt


def test_ship_from_inside_pr_worktree_self_cleans(repo_with_pr_worktree, tmp_path):
    """Running ship FROM INSIDE the PR's own worktree must, after a (faked) successful merge,
    re-root into the main checkout and remove BOTH the worktree and the local branch — and
    still exit 0 with a valid cwd (the re-root makes the deleted cwd harmless)."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    # cwd = the PR worktree: this is the path the SELF-guard used to refuse to clean.
    r = _run_ship(main, bindir, cwd=wt)

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # Prove it took the re-root path (not the old "leaving it in place" giveup).
    assert "re-rooting into" in r.stdout, f"self-clean re-root did not fire:\n{r.stdout}\n{r.stderr}"
    assert "leaving it in place" not in r.stdout, f"ship gave up instead of self-cleaning:\n{r.stdout}"
    # CALLER CONTRACT: ship must warn that the launch cwd is now gone (finding from review) so
    # the parent shell knows to cd out before its next command.
    assert "your shell's cwd is now gone" in r.stderr, (
        "missing caller-contract warning after self-removal:\n" + r.stderr
    )

    # The worktree is gone (queried from the main checkout, which still exists).
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt) not in remaining, f"PR worktree still present after self-clean:\n{remaining}"
    assert not wt.exists(), f"PR worktree dir still on disk: {wt}"
    # The local branch is gone too.
    branches = _sh("git", "branch", "--list", "feat", cwd=main).stdout
    assert "feat" not in branches, f"local branch feat still present:\n{branches}"


def test_ship_from_inside_dirty_pr_worktree_is_not_removed(repo_with_pr_worktree, tmp_path):
    """Safety: a PR worktree with uncommitted changes is NEVER destroyed by the self-clean
    path. Two guards enforce this, and this test pins the FIRST one — the pre-merge preflight
    (`worktree … has uncommitted changes`) which refuses to merge at all (exit 1) when the
    worktree is dirty, so cleanup never runs and the unshipped edit survives. (The cleanup
    loop carries a second, defence-in-depth clean-check before re-rooting + removing the
    self worktree, in case a tree is dirtied after the preflight — see ship.sh.) The
    load-bearing property either way: a dirty PR worktree is not unlinked and its edit is
    kept."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    # Dirty the worktree with an uncommitted modification (tracked file change).
    (wt / "README.md").write_text("# dirty unshipped work\n", encoding="utf-8")

    r = _run_ship(main, bindir, cwd=wt)

    # The preflight refuses a dirty worktree (exit 1) BEFORE merging — nothing is merged or
    # removed, which is strictly safer than merging then trying to preserve the tree.
    assert r.returncode != 0, f"ship should refuse a dirty worktree; got 0\n{r.stdout}\n{r.stderr}"
    assert "uncommitted changes" in r.stderr, f"expected dirty-worktree refusal:\n{r.stderr}"
    # The worktree (and its unshipped edit) survives intact.
    assert wt.exists(), "dirty worktree was removed — unshipped work destroyed"
    assert "dirty unshipped work" in (wt / "README.md").read_text(encoding="utf-8")
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt) in remaining, f"dirty worktree was unlinked:\n{remaining}"


def test_cleanup_guard_leaves_worktree_dirtied_after_preflight(repo_with_pr_worktree, tmp_path):
    """Directly exercise the CLEANUP-time clean-check (not the preflight one): the worktree is
    clean at preflight, then the (faked) merge writes an untracked file into it, so by the
    time cleanup tries to remove the self worktree it is dirty. ship must NOT --force it away
    — it warns, leaves the tree (with its file) in place, and still exits 0."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    dirty_file = wt / "post_merge_artifact.txt"

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_MERGE_DIRTIES"] = str(dirty_file)  # dirty the tree at merge time
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship must exit 0 after merge; got {r.returncode}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # The cleanup-time guard (NOT the preflight) must have fired.
    assert "uncommitted/unverifiable changes" in r.stderr, (
        "cleanup-time dirty guard did not fire — guard is untested/dead:\n" + r.stderr
    )
    assert "re-rooting into" not in r.stdout, "should not have re-rooted/removed a dirty tree"
    # The worktree and its post-merge artifact survive.
    assert wt.exists(), "dirty worktree removed at cleanup — guard failed"
    assert dirty_file.exists(), "post-merge artifact destroyed"
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt) in remaining, f"dirty worktree was unlinked at cleanup:\n{remaining}"


# ---------------------------------------------------------------------------------------
# Version-bump gate: a ship of shippable SOURCE must bump the declared version (skill:
# bump-version-on-release). The canonical failure is a version that never moves across
# releases (`rig --version` stuck on a hardcoded 0.1.0). These tests pin the four required
# behaviours: source-change-without-bump is BLOCKED; with-bump PASSES; a docs-only PR is NOT
# required to bump; the override works with a reason.
# ---------------------------------------------------------------------------------------

# A fake `gh` that lets the test drive both `gh pr diff --name-only` (changed paths) and the
# full `gh pr diff` patch, via env vars — so the version-bump gate sees exactly the shape
# under test. SHIP_TEST_NAME_ONLY = newline list of changed paths; SHIP_TEST_PATCH = the full
# unified diff text. (--skip-ci so the CI rollup isn't queried; review-threads -> 0.)
_FAKE_GH_VBUMP = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        else
          echo '[]'
        fi ;;
      diff)
        if printf '%s ' "$@" | grep -q -- --name-only; then
          printf '%s' "${SHIP_TEST_NAME_ONLY:-src/a.py}"
        else
          printf '%s' "${SHIP_TEST_PATCH:-}"
        fi ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    if printf '%s ' "$@" | grep -q committedDate; then
      printf '%s\\t%s\\t%s\\n' "${SHIP_TEST_CREATED:-2020-01-01T00:00:00Z}" "${SHIP_TEST_PUSHED:-}" "${SHIP_TEST_LASTCOMMIT:-2020-01-01T00:00:00Z}"
    else
      echo "${SHIP_TEST_UNRESOLVED:-0}"
    fi ;;
  *) : ;;
esac
"""


def _fake_gh_vbump_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "binvb"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_VBUMP, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


@pytest.fixture
def repo_with_pyproject(tmp_path):
    """A repo on `main` carrying a pyproject.toml with a version, branch `feat` in a worktree,
    and an origin remote. The version-bump gate auto-detects pyproject.toml at the root."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    (main / "pyproject.toml").write_text(
        '[project]\nname = "mytool"\nversion = "0.4.1"\n', encoding="utf-8"
    )
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    wt = tmp_path / "wt-feat"
    _git("worktree", "add", "-q", str(wt), "feat", cwd=main)
    return main, wt


def _run_ship_vbump(main, bindir, *, name_only, patch, extra_args=(), env_extra=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_NAME_ONLY"] = name_only
    env["SHIP_TEST_PATCH"] = patch
    if env_extra:
        env.update(env_extra)
    return _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", *extra_args,
        cwd=main, env=env,
    )


# A PR diff that BUMPS pyproject's version (0.4.1 -> 0.4.2) alongside a source change.
_PATCH_WITH_BUMP = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n+++ b/src/a.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/pyproject.toml b/pyproject.toml\n"
    "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
    '@@ -3 +3 @@\n-version = "0.4.1"\n+version = "0.4.2"\n'
)

# A PR diff that changes source but does NOT touch the version line.
_PATCH_NO_BUMP = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n+++ b/src/a.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)

# A docs-only PR diff (README only — no shippable source, no version change).
_PATCH_DOCS_ONLY = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n+++ b/README.md\n"
    "@@ -1 +1,2 @@\n # x\n+more docs\n"
)


def test_source_change_without_version_bump_is_blocked(repo_with_pyproject, tmp_path):
    """A PR that changes shippable source but leaves the declared version unchanged must be
    REFUSED — this ship is a release, the version must move."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="src/a.py", patch=_PATCH_NO_BUMP)

    assert r.returncode != 0, f"ship should refuse a source change with no version bump\n{r.stdout}\n{r.stderr}"
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_source_change_with_version_bump_passes(repo_with_pyproject, tmp_path):
    """A PR that changes shippable source AND bumps the version passes the gate and merges."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="src/a.py\npyproject.toml", patch=_PATCH_WITH_BUMP
    )

    assert r.returncode == 0, f"ship should pass with a version bump\n{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_docs_only_pr_is_not_required_to_bump(repo_with_pyproject, tmp_path):
    """A docs-only PR (no shippable source) must NOT be forced to bump the version."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="README.md", patch=_PATCH_DOCS_ONLY)

    assert r.returncode == 0, f"docs-only PR must not be blocked\n{r.stdout}\n{r.stderr}"
    assert "no shippable source" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_version_bump_override_with_reason(repo_with_pyproject, tmp_path):
    """The --no-version-bump-ok <reason> override lets a genuine no-release ship of source
    through (e.g. a revert), recording the reason — and merges."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="src/a.py", patch=_PATCH_NO_BUMP,
        extra_args=("--no-version-bump-ok", "pure revert of #99, no behavior change"),
    )

    assert r.returncode == 0, f"override should allow the ship\n{r.stdout}\n{r.stderr}"
    assert "version-bump gate OVERRIDDEN" in r.stdout, r.stdout
    assert "pure revert of #99" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_version_bump_override_via_env(repo_with_pyproject, tmp_path):
    """SHIP_SKIP_VERSION_BUMP=1 is the env-driven equivalent of the override flag."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="src/a.py", patch=_PATCH_NO_BUMP,
        env_extra={"SHIP_SKIP_VERSION_BUMP": "1"},
    )

    assert r.returncode == 0, f"env override should allow the ship\n{r.stdout}\n{r.stderr}"
    assert "version-bump gate OVERRIDDEN" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_no_version_file_skips_gate(repo_with_pr_worktree, tmp_path):
    """A repo with no pyproject.toml/package.json at the root: the gate has nothing to check,
    so it skips (does not block) — and ship still merges."""
    main, _wt = repo_with_pr_worktree  # this fixture has only README.md, no version file
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="src/a.py", patch=_PATCH_NO_BUMP)

    assert r.returncode == 0, f"no version file -> gate skips\n{r.stdout}\n{r.stderr}"
    assert "no version file" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


# A PR diff that touches the version LINE cosmetically (quote style) WITHOUT changing the
# value — must NOT count as a bump (the value must actually move).
_PATCH_COSMETIC_VERSION = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n+++ b/src/a.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/pyproject.toml b/pyproject.toml\n"
    "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
    "@@ -3 +3 @@\n-version = \"0.4.1\"\n+version  =  \"0.4.1\"\n"
)


def test_cosmetic_version_edit_is_not_a_bump(repo_with_pyproject, tmp_path):
    """A whitespace/quote-only edit to the version line, with the VALUE unchanged, must be
    treated as NOT bumped — the gate requires the version value to actually move, not merely
    that a `+version` line appears in the diff."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="src/a.py\npyproject.toml", patch=_PATCH_COSMETIC_VERSION
    )

    assert r.returncode != 0, f"cosmetic version edit must not pass as a bump\n{r.stdout}\n{r.stderr}"
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr


@pytest.fixture
def repo_with_package_json(tmp_path):
    """A repo whose version file is package.json (the Node path), to cover that code branch
    independently of pyproject.toml."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    (main / "package.json").write_text(
        '{\n  "name": "mytool",\n  "version": "1.0.0"\n}\n', encoding="utf-8"
    )
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    wt = tmp_path / "wt-feat"
    _git("worktree", "add", "-q", str(wt), "feat", cwd=main)
    return main, wt


_PATCH_PKGJSON_BUMP = (
    "diff --git a/src/a.js b/src/a.js\n"
    "--- a/src/a.js\n+++ b/src/a.js\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/package.json b/package.json\n"
    "--- a/package.json\n+++ b/package.json\n"
    '@@ -3 +3 @@\n-  "version": "1.0.0"\n+  "version": "1.0.1"\n'
)
_PATCH_PKGJSON_NO_BUMP = (
    "diff --git a/src/a.js b/src/a.js\n"
    "--- a/src/a.js\n+++ b/src/a.js\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


def test_package_json_source_without_bump_is_blocked(repo_with_package_json, tmp_path):
    """The package.json (Node) code path: source change with no version bump is BLOCKED."""
    main, _wt = repo_with_package_json
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="src/a.js", patch=_PATCH_PKGJSON_NO_BUMP)

    assert r.returncode != 0, f"package.json source w/o bump must be blocked\n{r.stdout}\n{r.stderr}"
    assert "version in package.json is UNCHANGED" in r.stderr, r.stderr


def test_package_json_with_bump_passes(repo_with_package_json, tmp_path):
    """The package.json (Node) code path: a real version bump passes the gate and merges."""
    main, _wt = repo_with_package_json
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="src/a.js\npackage.json", patch=_PATCH_PKGJSON_BUMP
    )

    assert r.returncode == 0, f"package.json bump should pass\n{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_mixed_docs_and_source_without_bump_is_blocked(repo_with_pyproject, tmp_path):
    """A PR mixing a docs change AND a shippable source change, with no version bump, must
    still be BLOCKED — one shippable path is enough to make the ship a release."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="README.md\nsrc/a.py", patch=_PATCH_NO_BUMP
    )

    assert r.returncode != 0, f"mixed docs+source w/o bump must be blocked\n{r.stdout}\n{r.stderr}"
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr


def test_ci_only_pr_is_not_required_to_bump(repo_with_pyproject, tmp_path):
    """A pure-CI PR (e.g. a GitLab CI config) is exempt — CI-only is not a release."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    patch = (
        "diff --git a/.gitlab-ci.yml b/.gitlab-ci.yml\n"
        "--- a/.gitlab-ci.yml\n+++ b/.gitlab-ci.yml\n"
        "@@ -1 +1,2 @@\n stages:\n+  - lint\n"
    )
    r = _run_ship_vbump(main, bindir, name_only=".gitlab-ci.yml", patch=patch)

    assert r.returncode == 0, f"CI-only PR must not be blocked\n{r.stdout}\n{r.stderr}"
    assert "no shippable source" in r.stdout, r.stdout


def test_ship_version_files_override_locates_nested_manifest(tmp_path):
    """SHIP_VERSION_FILES pins a non-standard version file; the gate checks THAT file. Here a
    nested package's pyproject is the version source and a source change without bumping it is
    blocked."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    main = tmp_path / "main"
    (main / "pkg").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    (main / "pkg" / "pyproject.toml").write_text(
        '[project]\nname = "m"\nversion = "2.0.0"\n', encoding="utf-8"
    )
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "wt"), "feat", cwd=main)

    bindir = _fake_gh_vbump_dir(tmp_path)
    r = _run_ship_vbump(
        main, bindir, name_only="pkg/app.py", patch=_PATCH_NO_BUMP,
        env_extra={"SHIP_VERSION_FILES": "pkg/pyproject.toml"},
    )

    assert r.returncode != 0, f"pinned version file w/o bump must block\n{r.stdout}\n{r.stderr}"
    assert "version in pkg/pyproject.toml is UNCHANGED" in r.stderr, r.stderr


def test_dry_run_self_clean_does_not_warn_or_remove(repo_with_pr_worktree, tmp_path):
    """--dry-run from inside the PR worktree must NOT remove the tree and must NOT emit the
    caller-contract 'cwd is now gone' warning (the worktree is still there). Guards against
    setting SELF_REMOVED before/regardless of an actual removal."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", "--dry-run",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"dry-run ship exited {r.returncode}\n{r.stderr}"
    # No false caller-contract warning: nothing was actually removed.
    assert "your shell's cwd is now gone" not in r.stderr, (
        "dry-run falsely warned the cwd is gone — SELF_REMOVED set without a real removal:\n"
        + r.stderr
    )
    # The worktree survives dry-run.
    assert wt.exists(), "dry-run removed the worktree"
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt) in remaining, f"dry-run unlinked the worktree:\n{remaining}"


def test_self_clean_among_two_worktrees_removes_both(repo_with_two_worktrees, tmp_path):
    """Session inside ONE of two worktrees on the branch: the self tree re-roots + is removed,
    AND the other (non-self) tree is removed too. Proves the in-loop `cd` into the main
    checkout doesn't break removal of subsequent worktrees in the same loop."""
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)

    # Launch from INSIDE wt1 (one of the two worktrees on `feat`).
    r = _run_ship(main, bindir, cwd=wt1)

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    assert "re-rooting into" in r.stdout, f"self-clean re-root did not fire:\n{r.stdout}"
    # Both worktrees gone (the self one via re-root, the other directly).
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt1) not in remaining, f"self worktree still present:\n{remaining}"
    assert str(wt2) not in remaining, f"second worktree still present:\n{remaining}"
    assert not wt1.exists() and not wt2.exists(), "a worktree dir survived on disk"
    # Branch deleted once no worktree holds it.
    branches = _sh("git", "branch", "--list", "feat", cwd=main).stdout
    assert "feat" not in branches, f"local branch feat still present:\n{branches}"


def test_self_removed_but_branch_kept_when_other_tree_dirty(repo_with_two_worktrees, tmp_path):
    """Combined interaction: session inside wt1 (clean) — it self-cleans (re-root + removed);
    wt2 is dirtied at merge time so it stays, still holding `feat` checked out. Branch-delete
    is therefore skipped (with a warning), wt2 is left intact, and ship still exits 0."""
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    dirty_file = wt2 / "post_merge_artifact.txt"

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_MERGE_DIRTIES"] = str(dirty_file)  # dirty wt2 at merge time
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=wt1, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # Self tree (wt1) was re-rooted + removed; the caller-contract NOTE fired.
    assert "re-rooting into" in r.stdout, f"self-clean did not fire:\n{r.stdout}"
    assert "your shell's cwd is now gone" in r.stderr, f"missing caller-contract NOTE:\n{r.stderr}"
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt1) not in remaining, f"self worktree wt1 not removed:\n{remaining}"
    # Dirty wt2 left in place — and the branch could NOT be deleted (wt2 still has it out).
    assert str(wt2) in remaining, f"dirty wt2 was force-removed:\n{remaining}"
    assert dirty_file.exists(), "dirty file in wt2 destroyed"
    branches = _sh("git", "branch", "--list", "feat", cwd=main).stdout
    assert "feat" in branches, f"branch feat deleted while wt2 still holds it:\n{branches}"


def test_cleanup_guard_leaves_dirty_non_self_worktree(repo_with_two_worktrees, tmp_path):
    """The never---force-a-dirty-tree guard applies to NON-self worktrees too. Run ship from
    the main checkout (neither worktree is self); the faked merge dirties wt2 (post-preflight,
    pre-cleanup). wt1 (clean) is removed; wt2 (dirty) is left in place — not force-nuked. ship
    still exits 0."""
    main, wt1, wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    dirty_file = wt2 / "post_merge_artifact.txt"

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_MERGE_DIRTIES"] = str(dirty_file)  # dirty wt2 at merge time
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    assert "could not remove worktree" in r.stderr, (
        "dirty non-self worktree was not left in place (guard did not fire):\n" + r.stderr
    )
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt1) not in remaining, f"clean worktree wt1 should have been removed:\n{remaining}"
    assert str(wt2) in remaining, f"dirty worktree wt2 was force-removed — unshipped work lost:\n{remaining}"
    assert dirty_file.exists(), "dirty file in wt2 destroyed"


def test_ship_from_inside_worktree_without_reroot_target_leaves_it(repo_with_pr_worktree, tmp_path):
    """If the session is inside the PR worktree but there is no DISTINCT main checkout to
    re-root into (SHIP_MAIN_CHECKOUT points at the worktree itself), self-clean cannot run —
    removing a worktree from inside itself is impossible. ship must leave it in place and
    still exit 0 (cleanup is best-effort; the merge is already done)."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(wt)  # degenerate: re-root target IS the worktree
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship must exit 0 after merge; got {r.returncode}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    assert "no separate main checkout to re-root into" in r.stderr, (
        "the no-reroot-target guard did not fire:\n" + r.stderr
    )
    # The worktree is left in place (not removed out from under the running session).
    assert wt.exists(), "worktree removed despite no re-root target"


def _bare_corrupt_checkout(tmp_path: Path, name: str = "corrupt") -> Path:
    """A WORKING checkout (has a `.git` dir + a commit) deliberately corrupted with
    `core.bare=true` — the rig-cli #19/#52 class. Every git op inside it then fails fatal
    with "this operation must be run in a work tree"; ship.sh's guard must catch it."""
    d = tmp_path / name
    d.mkdir()
    _git("init", "-q", "-b", "main", cwd=d)
    _git("config", "user.email", "t@t", cwd=d)
    _git("config", "user.name", "t", cwd=d)
    (d / "README.md").write_text("# x\n", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "f.txt").write_text("nested\n", encoding="utf-8")
    _git("add", "-A", cwd=d)
    _git("commit", "-qm", "init", cwd=d)
    # The corruption: flip core.bare on a non-bare working checkout.
    _git("config", "core.bare", "true", cwd=d)
    return d


def test_core_bare_main_checkout_aborts_early(repo_with_pr_worktree, tmp_path):
    """A main checkout corrupted with core.bare=true must make ship ABORT EARLY (before the
    merge) with a clear diagnostic naming the repo + the one-line fix, and a nonzero exit —
    not fail confusingly mid-ship in the post-merge main-refresh. ship is run from a HEALTHY
    cwd (the PR worktree's repo) with SHIP_MAIN_CHECKOUT pointed at the corrupt checkout, so
    the test isolates the main-checkout guard deterministically."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    corrupt = _bare_corrupt_checkout(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(corrupt)  # the corrupted checkout to guard
    # Run from the healthy main checkout so `git rev-parse --show-toplevel` (cwd-scoped) is
    # fine and execution reaches the MAIN_CHECKOUT guard.
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )

    assert r.returncode != 0, (
        f"ship must abort on a core.bare main checkout; got 0\n{r.stdout}\n{r.stderr}"
    )
    # Aborts EARLY — before any merge happens.
    assert "merged #1" not in r.stdout, f"ship merged despite the corrupt checkout:\n{r.stdout}"
    # The diagnostic names the corruption, the repo path, and the one-line fix.
    assert "core.bare=true" in r.stderr, f"missing core.bare diagnostic:\n{r.stderr}"
    assert str(corrupt) in r.stderr, f"diagnostic does not name the repo path:\n{r.stderr}"
    assert "config core.bare false" in r.stderr, f"missing the one-line fix:\n{r.stderr}"
    assert "rig doctor --fix" in r.stderr, f"missing the rig doctor fix hint:\n{r.stderr}"


def test_core_bare_self_cwd_main_checkout_aborts(tmp_path):
    """The most realistic shape: ship is launched from INSIDE the corrupt checkout (cwd ==
    corrupt). The cwd guard must fire BEFORE `git rev-parse --show-toplevel` (which under
    core.bare dies with the bare 'must be run in a work tree' and `set -e` aborts — the exact
    confusing failure, with no diagnostic). So we assert not just nonzero + no-merge, but that
    the guard's DIAGNOSTIC actually fired (naming the corruption + the one-line fix) — proving
    the cwd guard, not the incidental rev-parse failure, did the abort."""
    corrupt = _bare_corrupt_checkout(tmp_path, name="corrupt-cwd")
    bindir = _fake_gh_dir(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "main"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    # No SHIP_MAIN_CHECKOUT override: ship derives it from `git worktree list` in the cwd.
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=corrupt, env=env,
    )

    assert r.returncode != 0, f"ship must abort in a core.bare checkout; got 0\n{r.stdout}"
    assert "merged #1" not in r.stdout, f"ship merged despite the corrupt cwd:\n{r.stdout}"
    # The cwd guard fired with its diagnostic — NOT a bare rev-parse failure.
    assert "core.bare=true" in r.stderr, f"cwd guard diagnostic did not fire:\n{r.stderr}"
    assert str(corrupt) in r.stderr, f"diagnostic does not name the corrupt cwd:\n{r.stderr}"
    assert "config core.bare false" in r.stderr, f"missing the one-line fix:\n{r.stderr}"


def test_core_bare_cwd_subdir_aborts_with_diagnostic(tmp_path):
    """Sharper shape of the realistic case: ship launched from a SUBDIRECTORY of the corrupt
    checkout (not its root). `.git` lives only at the worktree root, so a layout-gated cwd check
    would miss this — but `git rev-parse --is-bare-repository` reports `true` from a subdir of a
    core.bare checkout, so the cwd guard must STILL fire with its diagnostic before the
    rev-parse --show-toplevel that would otherwise die bare. Pins review finding #1."""
    corrupt = _bare_corrupt_checkout(tmp_path, name="corrupt-subdir")
    subdir = corrupt / "sub"  # a real subdirectory created by the fixture
    bindir = _fake_gh_dir(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "main"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=subdir, env=env,
    )

    assert r.returncode != 0, f"ship must abort when launched from a corrupt subdir; got 0\n{r.stdout}"
    assert "merged #1" not in r.stdout, f"ship merged despite the corrupt subdir cwd:\n{r.stdout}"
    # The guard's diagnostic fired — NOT the bare rev-parse failure.
    assert "core.bare=true" in r.stderr, f"cwd guard did not fire from a subdir:\n{r.stderr}"
    assert "config core.bare false" in r.stderr, f"missing the one-line fix:\n{r.stderr}"


def test_core_bare_guard_no_false_positive_on_healthy_repo(repo_with_pr_worktree, tmp_path):
    """A HEALTHY main checkout (and a healthy linked worktree) must NOT trip the guard. ship
    runs to a normal successful (faked) merge — proving the guard doesn't fire on a legitimate
    working checkout nor on a normal linked worktree (whose `.git` is a file, not a dir)."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    r = _run_ship(main, bindir)

    assert r.returncode == 0, (
        f"guard false-positived on a healthy repo; ship exited {r.returncode}\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout
    # The guard's refusal text must be absent — proof it stayed silent on a healthy checkout.
    assert "has core.bare=true" not in r.stderr, f"guard fired on a healthy repo:\n{r.stderr}"


def test_core_bare_guard_no_false_positive_on_genuine_bare_repo_worktree(tmp_path):
    """The load-bearing assumption: a LINKED worktree of a GENUINE bare repo (the bare repo's
    config legitimately has core.bare=true, and that config is shared with its worktrees) must
    NOT trip the guard. `git rev-parse --is-bare-repository` reports such a linked worktree as
    NOT bare even though `git config core.bare` reads true — which is exactly why the guard
    keys off the per-path rev-parse verdict, not a raw config read. If this assumption were
    wrong on some git, a legitimate ship would be refused; this test pins it.

    Setup: a genuine bare repo with a commit, plus a worktree added from the bare repo. ship is
    run from that worktree (SHIP_MAIN_CHECKOUT also points at it) and must reach a normal merge.
    """
    bare = tmp_path / "genuine.git"
    _sh("git", "init", "--bare", "-q", "-b", "main", str(bare), cwd=tmp_path)

    # Seed the bare repo with one commit on `feat` via a throwaway checkout, then a worktree.
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    (seed / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    _git("push", "-q", "origin", "main:feat", cwd=seed)

    # Sanity-check the premise: the bare repo's shared config IS core.bare=true ...
    cfg = _sh("git", "-C", str(bare), "config", "core.bare", cwd=tmp_path).stdout.strip()
    assert cfg == "true", f"premise broken: genuine bare repo not core.bare=true (got {cfg!r})"

    # ... but a LINKED worktree of it reports NOT-bare via rev-parse (the whole assumption).
    wt = tmp_path / "bare-wt"
    _git("worktree", "add", "-q", str(wt), "feat", cwd=bare)
    isbare = _sh(
        "git", "-C", str(wt), "rev-parse", "--is-bare-repository", cwd=tmp_path
    ).stdout.strip()
    assert isbare == "false", (
        f"assumption broken on this git: a genuine-bare-repo worktree reports is-bare={isbare!r}"
        " — the guard would false-positive a legitimate ship here"
    )

    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(wt)
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, (
        f"guard false-positived on a genuine-bare-repo worktree; ship exited {r.returncode}\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has core.bare=true" not in r.stderr, (
        f"guard wrongly fired on a genuine-bare-repo linked worktree:\n{r.stderr}"
    )


@pytest.mark.parametrize("where", ["root", "subdir"])
def test_cwd_guard_no_false_positive_inside_genuine_bare_repo(tmp_path, where):
    """The symmetric counterpart to test_core_bare_cwd_subdir_aborts_with_diagnostic: the cwd
    is the ROOT of (where=root) or a SUBDIR of (where=subdir) a GENUINE bare repo, where
    `core.bare=true` is LEGITIMATE. The cwd guard reports rev-parse=bare in both, so a naive
    rev-parse-only check would FIRE and print a "corruption" diagnostic recommending `git config
    core.bare false` — which would BREAK the real bare repo. The guard must recognise the
    genuine-bare case (via --is-inside-git-dir / no-ancestor-.git) and NOT fire. Both cwds are
    exercised: the root hits --is-inside-git-dir on the git-dir itself; the subdir hits it from
    within. ship then proceeds past the cwd guard (failing later for unrelated reasons — no PR);
    we only assert the cwd guard did not emit its corruption diagnostic / destructive fix."""
    bare = tmp_path / "genuine.git"
    _sh("git", "init", "--bare", "-q", "-b", "main", str(bare), cwd=tmp_path)
    if where == "root":
        cwd = bare
    else:
        cwd = bare / "refs"  # a real subdir that exists inside every bare repo
        assert cwd.is_dir(), "expected refs/ inside the bare repo"

    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "main"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    # Point SHIP_MAIN_CHECKOUT elsewhere so the MAIN-checkout guard isn't what we measure.
    env["SHIP_MAIN_CHECKOUT"] = str(tmp_path)
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=cwd, env=env,
    )

    # The cwd guard must NOT have fired: no corruption diagnostic, no destructive fix advice.
    assert "has core.bare=true" not in r.stderr, (
        f"cwd guard wrongly flagged a genuine bare repo as corrupt:\n{r.stderr}"
    )
    # It must NOT have recommended the bare-breaking `config core.bare false` for the bare repo.
    assert not ("config core.bare false" in r.stderr and str(bare) in r.stderr), (
        f"cwd guard recommended the destructive bare-breaking fix for a genuine bare repo:\n{r.stderr}"
    )
    # Positive anchor (guards against a vacuous pass): execution must have REACHED a point AFTER
    # the cwd guard. ship's next git op is `git rev-parse --show-toplevel`, which under a bare
    # repo fails with the bare "must be run in a work tree" — its presence proves the cwd guard
    # was reached and CORRECTLY passed (had it wrongly fired, ship would have exited with the
    # corruption diagnostic instead, and this string would be absent).
    assert "must be run in a work tree" in r.stderr, (
        "ship did not reach the post-cwd-guard rev-parse — the no-false-positive assertion "
        f"could be passing vacuously:\n{r.stderr}"
    )


def test_core_bare_pr_worktree_aborts(repo_with_pr_worktree, tmp_path):
    """The per-worktree guard's real, non-exotic trigger: a LINKED worktree corrupted with
    WORKTREE-SCOPED core.bare=true (extensions.worktreeConfig + `config --worktree core.bare
    true`). Unlike core.bare on the MAIN config (which leaves linked worktrees healthy), this
    DOES make the worktree itself report rev-parse=bare with `status` failing — so the loop's
    `git -C "$wt" status --short 2>/dev/null` would return empty (fatal swallowed) and the
    worktree would look CLEAN, risking removal of unshipped work. ship is run from the MAIN
    checkout (so the cwd/main guards pass); the per-worktree guard must catch the corrupt
    worktree and ABORT before the fooled dirty-check, with the diagnostic + nonzero exit."""
    main, wt = repo_with_pr_worktree

    # Worktree-scoped core.bare corruption (the supported-git mechanism, not hand-corruption).
    _git("config", "extensions.worktreeConfig", "true", cwd=main)
    _git("config", "--worktree", "core.bare", "true", cwd=wt)
    # Sanity-check the premise actually took: the worktree now reports bare + status fails.
    isbare = _sh("git", "-C", str(wt), "rev-parse", "--is-bare-repository", cwd=main).stdout.strip()
    if isbare != "true":
        pytest.skip(f"this git does not honor worktree-scoped core.bare (is-bare={isbare!r})")

    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship(main, bindir)  # cwd = main (healthy); the corrupt tree is the linked worktree

    assert r.returncode != 0, (
        f"ship must abort on a bare-corrupted linked worktree; got 0\n{r.stdout}\n{r.stderr}"
    )
    assert "merged #1" not in r.stdout, f"ship merged despite the corrupt worktree:\n{r.stdout}"
    # The per-worktree guard fired (labelled "PR worktree"), not a swallowed dirty-check.
    assert "PR worktree" in r.stderr, f"per-worktree guard did not fire:\n{r.stderr}"
    assert "core.bare=true" in r.stderr, f"missing core.bare diagnostic:\n{r.stderr}"
    assert str(wt) in r.stderr, f"diagnostic does not name the corrupt worktree:\n{r.stderr}"
    # The worktree (and any unshipped work) survives — ship aborted before any removal.
    assert wt.exists(), "corrupt worktree was removed despite the abort"

    # ROUND-TRIP (the assertion that catches a wrong fix): the printed fix MUST be the
    # WORKTREE-SCOPED form. A plain `config core.bare false` writes the shared config, which the
    # worktree-scoped `true` shadows — so the suggested command would NOT repair it and the next
    # ship would fail again. The fix must use `--worktree`. Verify the advice is right AND that
    # actually running it repairs the worktree (rev-parse no longer bare).
    assert "config --worktree core.bare false" in r.stderr, (
        "per-worktree fix is not worktree-scoped — applying it would NOT repair worktree-scoped "
        f"core.bare, and the next ship would fail again:\n{r.stderr}"
    )
    _git("config", "--worktree", "core.bare", "false", cwd=wt)
    repaired = _sh("git", "-C", str(wt), "rev-parse", "--is-bare-repository", cwd=main).stdout.strip()
    assert repaired == "false", (
        f"applying the printed fix did NOT repair the worktree (still is-bare={repaired!r})"
    )


def test_core_bare_fix_command_is_paste_safe_for_special_path(tmp_path):
    """Regression anchor for shell_squote — the one non-trivial encoding in the guard. The
    corrupt checkout lives under a dir whose name has a SPACE, a `$`, AND a single quote `'` (all
    legal on Unix). The single quote specifically exercises shell_squote's `'\\''` escaping branch
    — without it that trickiest, easiest-to-break line has zero coverage. The printed fix must be
    safely single-quoted so a user can copy-paste-run it verbatim. We assert: (a) the diagnostic
    contains the path wrapped in single quotes (not bare/double-quoted, which would mis-split on
    the space or expand `$x`), and (b) running the EXACT printed command repairs the repo (a
    true round-trip through a shell), proving the quoting is correct end-to-end."""
    base = tmp_path / "weird $x 'dir"  # space + `$` + a single quote in the path
    base.mkdir()
    corrupt = base / "repo"
    corrupt.mkdir()
    _git("init", "-q", "-b", "main", cwd=corrupt)
    _git("config", "user.email", "t@t", cwd=corrupt)
    _git("config", "user.name", "t", cwd=corrupt)
    (corrupt / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=corrupt)
    _git("commit", "-qm", "init", cwd=corrupt)
    _git("config", "core.bare", "true", cwd=corrupt)

    # A separate HEALTHY repo to run ship from, so `git rev-parse --show-toplevel` (cwd-scoped)
    # succeeds and execution reaches the MAIN-checkout guard (which points at the corrupt repo).
    runner = tmp_path / "runner"
    runner.mkdir()
    _git("init", "-q", "-b", "main", cwd=runner)
    _git("config", "user.email", "t@t", cwd=runner)
    _git("config", "user.name", "t", cwd=runner)
    (runner / "README.md").write_text("# x\n", encoding="utf-8")
    _git("add", "-A", cwd=runner)
    _git("commit", "-qm", "init", cwd=runner)

    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "main"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(corrupt)
    # Run from the healthy runner so the MAIN-checkout guard (not the cwd guard) emits the
    # diagnostic naming the special-char corrupt path.
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=runner, env=env,
    )

    assert r.returncode != 0, f"ship must abort on the corrupt checkout; got 0\n{r.stdout}"
    # (a) The path appears in its safely shell-quoted form (single-quoted, with any embedded `'`
    #     escaped via the '\'' idiom) — not bare/double-quoted, which would mis-split on the space,
    #     expand `$x`, or be cut off at the `'`.
    expected_quote = "'" + str(corrupt).replace("'", "'\\''") + "'"
    assert expected_quote in r.stderr, (
        f"fix command did not shell-quote the special-char path (paste-unsafe).\n"
        f"expected quoted form: {expected_quote}\nstderr:\n{r.stderr}"
    )
    # (b) Extract the exact `git -C ... config core.bare false` line and run it through a shell;
    #     it must repair the repo (rev-parse no longer bare), proving the quoting round-trips.
    fix_line = ""
    for line in r.stderr.splitlines():
        if "config core.bare false" in line:
            fix_line = line.strip()
            break
    assert fix_line, f"no fix command found in diagnostic:\n{r.stderr}"
    rc = _sh("bash", "-c", fix_line, cwd=tmp_path)
    assert rc.returncode == 0, f"pasted fix command failed to run: {rc.stderr}\ncmd: {fix_line}"
    repaired = _sh("git", "-C", str(corrupt), "rev-parse", "--is-bare-repository", cwd=tmp_path).stdout.strip()
    assert repaired == "false", (
        f"the pasted fix did not repair the special-char-path repo (is-bare={repaired!r})\n"
        f"cmd: {fix_line}"
    )


# ---------------------------------------------------------------------------------------
# CI-down detection and local fallback gate (task #75)
#
# ship.sh can detect when ALL (or ≥ 80%) CI checks fail due to a structural GitHub Actions
# outage and run a local fallback gate instead of blocking the merge. Tests here use:
#   SHIP_TEST_CI_DOWN=1  — force-trigger ci_appears_structurally_down() without network
#   SHIP_LOCAL_TEST_CMD  — override the auto-detected test runner command
#   SHIP_TEST_DIFF       — inject custom diff text for the leftover-marker check
#
# The fake gh for these tests returns two FAILED checks (100% failure rate), answers
# the local-gate sub-queries (review threads → 0, PR body → empty, diff → configurable),
# and the GraphQL review-threads gate (→ 0 unresolved).
# ---------------------------------------------------------------------------------------

# Fake gh that returns two failed CI checks + clean local-gate answers.
# SHIP_TEST_DIFF controls what `gh pr diff` returns (defaults to a clean line).
_FAKE_GH_CIDOWN = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          # Two checks, both FAILED — 100% failure rate.
          printf '[{"name":"pytest","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"codeql","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"}]'
        elif printf '%s ' "$@" | grep -q reviewThreads; then
          # _local_review_threads_check uses --jq (not processed by fake); output the count.
          echo "0"
        elif printf '%s ' "$@" | grep -q body; then
          echo ""
        else
          echo '[]'
        fi ;;
      diff)
        if printf '%s ' "$@" | grep -q -- --name-only; then
          printf 'src/a.py'
        else
          printf '%s\\n' "${SHIP_TEST_DIFF:-+new line without markers}"
        fi ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""

# Fake gh that returns ONE failed + ONE passing check (50% failure — below 80% threshold).
_FAKE_GH_PARTIAL_FAIL = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '[{"name":"pytest","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"lint","conclusion":"SUCCESS","status":"COMPLETED","state":"SUCCESS"}]'
        else
          echo '[]'
        fi ;;
      diff)
        if printf '%s ' "$@" | grep -q -- --name-only; then printf 'src/a.py'; else printf '+ok'; fi ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""


def _fake_gh_cidown_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "bincd"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_CIDOWN, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _fake_gh_partial_fail_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "binpf"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_PARTIAL_FAIL, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _run_ship_cidown(main: Path, bindir: Path, env_extra: dict | None = None):
    """Run ship.sh without --skip-ci (CI gate fires) but with --no-screenshot-ok."""
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    if env_extra:
        env.update(env_extra)
    return _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )


def test_cidown_local_gate_passes_merges(repo_with_pr_worktree, tmp_path):
    """CI-down path: all checks fail, SHIP_TEST_CI_DOWN=1 forces detection, local tests
    pass (SHIP_LOCAL_TEST_CMD=true) → ship falls through to merge."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",  # trivially-passing stand-in for the test suite
        "SHIP_REVIEW_DWELL": "0",  # disable dwell gate: fake PR has no review timestamps
    })

    assert r.returncode == 0, (
        f"ship must succeed when CI-down gate passes\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    # The detection path must have fired — proves we took the right branch.
    assert "CI infrastructure appears structurally unavailable" in r.stderr, r.stderr
    # The local gate must have reported success (success message goes to stdout).
    assert "ALL gates passed" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_prefers_dev_run_test_when_rig_script_exists(repo_with_pr_worktree, tmp_path):
    """When a repo declares `scripts.test` in rig.yaml and `dev` is installed, the local
    CI-down fallback should use `dev run test` instead of guessing a package manager."""
    main, _wt = repo_with_pr_worktree
    (main / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    _git("add", "rig.yaml", cwd=main)
    _git("commit", "-qm", "add rig test script", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    dev_log = tmp_path / "dev.log"
    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = 'has-script' ] && [ \"$2\" = 'test' ]; then exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$SHIP_TEST_DEV_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    dev.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_TEST_DEV_LOG": str(dev_log),
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must succeed when the dev-backed local gate passes\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert dev_log.read_text(encoding="utf-8").strip() == "run test"
    assert "[ship] local gate: running dev run test (rig.yaml scripts.test)" in r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_tests_fail_blocks(repo_with_pr_worktree, tmp_path):
    """CI-down path: SHIP_TEST_CI_DOWN=1, but local tests FAIL → ship refuses."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "false",  # always fails
    })

    assert r.returncode != 0, (
        f"ship must refuse when local tests fail\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "Local CI fallback: FAILED" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_leftover_markers_block(repo_with_pr_worktree, tmp_path):
    """CI-down path: local tests pass but PR diff has a TODO leftover → local gate fails."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        # Inject a diff addition with a TODO leftover marker
        "SHIP_TEST_DIFF": "+new code  # TODO: clean this up later",
    })

    assert r.returncode != 0, (
        f"ship must refuse when leftover markers found\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_partial_ci_failure_below_threshold_blocks_normally(repo_with_pr_worktree, tmp_path):
    """One of two checks failed (50%): below the 80% threshold, so ci_appears_structurally_down
    returns false and ship blocks with the normal CI-failure message (no local fallback)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_partial_fail_dir(tmp_path)

    r = _run_ship_cidown(main, bindir)

    assert r.returncode != 0, (
        f"ship must refuse on a genuine partial CI failure\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    # Normal CI-failure block: no local gate or CI-down messaging.
    assert "CI infrastructure appears structurally unavailable" not in r.stderr, r.stderr
    assert "local fallback" not in r.stderr.lower(), r.stderr
    assert "merged #1" not in r.stdout, r.stdout


# ---------------------------------------------------------------------------------------
# -R/--repo cross-repo support (restored; previously rejected)
#
# ship.sh USED to reject -R/--repo outright ("does not support -R/--repo"). That broke the
# only way an orchestrator (forbidden a `cd && gh ship`) can ship a PR into a non-CWD repo
# (agent-tools / rig-cli / tg-cli). --repo is now accepted and threaded through every gh call
# via GH_REPO, and remote-branch deletion is skipped for a foreign target (wrong-remote guard).
# ---------------------------------------------------------------------------------------

def _run_ship_repo(main: Path, bindir: Path, repo_args, env_extra: dict | None = None,
                   cwd: Path | None = None):
    """Run ship.sh with an explicit set of repo-flag args (a tuple of tokens inserted after
    the PR number). Returns the CompletedProcess."""
    env = dict(os.environ)
    # Deterministic cross-repo state: clear any GH_REPO/GH_SHIP_REPO the dev/CI env may carry,
    # so the no-repo path genuinely leaves gh's inference untouched (fake gh logs "<unset>").
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    if env_extra:
        env.update(env_extra)
    return _sh(
        "bash", str(_SHIP), "1", *repo_args, "--skip-ci", "--no-screenshot-ok", "test",
        cwd=cwd or main, env=env,
    )


@pytest.mark.parametrize("repo_args", [
    ("-R", "owner/repo"),
    ("--repo", "owner/repo"),
    ("-R=owner/repo",),
    ("--repo=owner/repo",),
    ("-Rowner/repo",),
])
def test_repo_flag_is_accepted_and_threads_gh_repo(repo_with_pr_worktree, tmp_path, repo_args):
    """Every gh-compatible spelling of --repo is ACCEPTED (no 'Unknown flag', no 'does not
    support' rejection), the ship completes, and GH_REPO=owner/repo reached the gh calls —
    proving --repo is pinned onto pr view / checks / merge for the cross-repo target."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    ghlog = tmp_path / "ghrepo.log"

    r = _run_ship_repo(main, bindir, repo_args, {"SHIP_TEST_GHREPO_LOG": str(ghlog)})

    assert r.returncode == 0, (
        f"ship must accept {repo_args!r}; got {r.returncode}\n{r.stdout}\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout
    assert "does not support -R/--repo" not in r.stderr, (
        f"the old rejection still fired for {repo_args!r}:\n{r.stderr}"
    )
    assert "Unknown flag" not in r.stderr, (
        f"generic 'Unknown flag' fired for {repo_args!r}:\n{r.stderr}"
    )
    # GH_REPO must have been exported to every gh call (the thread-through mechanism).
    logged = ghlog.read_text(encoding="utf-8") if ghlog.exists() else ""
    assert logged, "fake gh recorded no invocations — GH_REPO log is empty"
    assert all(line == "owner/repo" for line in logged.splitlines()), (
        f"expected every gh call to see GH_REPO=owner/repo, got:\n{logged}"
    )


def test_repo_flag_foreign_skips_remote_branch_deletion(repo_with_pr_worktree, tmp_path):
    """Cross-repo guard: with --repo targeting a repo that is NOT this checkout's origin,
    ship must SKIP remote-branch deletion (deleting via `git push origin --delete` would hit
    the WRONG remote) — AND all LOCAL cleanup (worktree removal, `git branch -D`): a
    same-named local branch/worktree belongs to THIS checkout, not the foreign PR, and
    deleting it would destroy unrelated local state (#167 review P1). The skip message fires
    and the origin branch, the local branch, and the local worktree all survive."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    # Sanity: the branch exists on origin before the ship.
    before = _sh("git", "ls-remote", "--heads", "origin", "feat", cwd=main).stdout
    assert "refs/heads/feat" in before, f"precondition: feat should be on origin:\n{before}"

    r = _run_ship_repo(main, bindir, ("--repo", "owner/repo"))

    assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # The guard message must fire (stdout).
    assert "skipping remote branch deletion" in r.stdout, (
        f"foreign-repo branch-deletion guard did not fire:\n{r.stdout}\n{r.stderr}"
    )
    # The origin branch must be UNTOUCHED (we never pushed --delete to the wrong remote).
    after = _sh("git", "ls-remote", "--heads", "origin", "feat", cwd=main).stdout
    assert "refs/heads/feat" in after, (
        f"origin's feat branch was deleted despite the foreign-repo guard:\n{after}"
    )
    # LOCAL state must be untouched too (#167 review P1): the ambient checkout's same-named
    # branch and its worktree are NOT the foreign PR's — cleanup must not touch them.
    assert "removing worktree" not in r.stdout, (
        f"foreign --repo must not remove local worktrees:\n{r.stdout}"
    )
    local_branch = _sh("git", "show-ref", "--verify", "refs/heads/feat", cwd=main)
    assert local_branch.returncode == 0, (
        f"local feat branch was deleted despite the foreign-repo guard:\n{r.stdout}"
    )
    assert wt.exists(), f"local worktree {wt} was removed despite the foreign-repo guard"


def test_no_repo_flag_path_is_unchanged(repo_with_pr_worktree, tmp_path):
    """The no-`--repo` path is byte-for-byte unchanged: GH_REPO is NOT exported (gh keeps its
    cwd inference), no skip message fires, and the origin `feat` branch IS deleted normally."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    ghlog = tmp_path / "ghrepo.log"

    r = _run_ship_repo(main, bindir, (), {"SHIP_TEST_GHREPO_LOG": str(ghlog)})

    assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    # No cross-repo messaging at all.
    assert "skipping remote branch deletion" not in r.stdout, r.stdout
    # GH_REPO stays unset for gh — the no-repo path does not pin it.
    logged = ghlog.read_text(encoding="utf-8") if ghlog.exists() else ""
    assert logged, "fake gh recorded no invocations"
    assert all(line == "<unset>" for line in logged.splitlines()), (
        f"no-repo path must leave GH_REPO unset for gh, got:\n{logged}"
    )
    # Remote branch deleted normally (the standard cleanup path).
    after = _sh("git", "ls-remote", "--heads", "origin", "feat", cwd=main).stdout
    assert "refs/heads/feat" not in after, (
        f"no-repo path should delete origin's feat branch, but it survived:\n{after}"
    )


# ---------------------------------------------------------------------------------------
# Stash-pop conflict detection after main-checkout refresh (#77)
#
# ship.sh now uses `git pull --ff-only --autostash` for the main-checkout refresh.
# --autostash is a no-op on a clean tree; when the checkout has uncommitted changes and
# those conflict with the incoming pull, git stash pop exits non-zero and the index
# contains UU-class unmerged files.  ship.sh must detect that and exit 1 with a clear
# diagnostic (naming the conflicting files and the PR merge status), not silently leave
# the main checkout broken.
# ---------------------------------------------------------------------------------------

@pytest.fixture
def repo_with_stash_conflict(tmp_path):
    """A repo where the main checkout will have a stash-pop conflict after the post-merge
    pull.  Setup:
      - origin/main has a file `shared.py` with content "origin\n"
      - the main checkout has an uncommitted change to the same line ("local\n")
      - the incoming pull changes the same line to "incoming\n"
    When `git pull --autostash` runs it stashes the local edit, fast-forwards, then tries
    to pop — but "local" conflicts with "incoming" on the same line, producing a UU
    unmerged entry."""
    if not shutil.which("bash") or not shutil.which("git"):
        pytest.skip("bash/git required")

    # --- bare origin ---
    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)

    # --- main checkout seeded at origin/main ---
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "shared.py").write_text("origin\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    # --- push a conflicting commit to origin/main ---
    # We push via a sibling clone so the main checkout stays at the old SHA.
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    _git("clone", "-q", str(origin), str(sibling), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=sibling)
    _git("config", "user.name", "t", cwd=sibling)
    (sibling / "shared.py").write_text("incoming\n", encoding="utf-8")
    _git("add", "-A", cwd=sibling)
    _git("commit", "-qm", "advance origin/main", cwd=sibling)
    _git("push", "-q", "origin", "main", cwd=sibling)

    # --- dirty the main checkout with a conflicting local edit ---
    # This is uncommitted — autostash will stash it, pull advances, pop conflicts.
    (main / "shared.py").write_text("local\n", encoding="utf-8")

    # --- PR branch (`feat`) — diverges from init via an unrelated file, pushed ---
    _git("fetch", "-q", "origin", cwd=main)
    # Start feat from the init commit (before origin/main advanced).
    _git("checkout", "-q", "-b", "feat", "HEAD", cwd=main)
    (main / "feat.py").write_text("# feat work\n", encoding="utf-8")
    _git("add", "feat.py", cwd=main)
    _git("commit", "-qm", "feat: add feature", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)

    # --- go back to main and apply the conflicting uncommitted edit ---
    _git("checkout", "-q", "main", cwd=main)
    # main is still at the init commit (origin/main is now one ahead via sibling).
    # Dirty shared.py with local content — autostash will stash it, pull advances, pop conflicts.
    (main / "shared.py").write_text("local\n", encoding="utf-8")
    # Leave it uncommitted so autostash picks it up.

    # Add a linked worktree for `feat` so ship.sh can run its preflight checks.
    wt = tmp_path / "wt-feat"
    _git("worktree", "add", "-q", str(wt), "feat", cwd=main)

    return main


def test_stash_pop_conflict_exits_nonzero_with_diagnostic(repo_with_stash_conflict, tmp_path):
    """When git pull --autostash produces a stash-pop conflict in the main checkout, ship
    must exit 1 with a diagnostic that:
      - confirms the PR IS merged (so the user doesn't re-run ship and double-merge),
      - names the conflicting files,
      - tells the user how to resolve manually.
    The conflict must NOT be silently swallowed."""
    main = repo_with_stash_conflict
    bindir = _fake_gh_dir(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    r = _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )

    # The merge itself succeeded — only the post-merge stash-pop conflicted.
    assert "merged #1" in r.stdout, (
        f"expected fake merge to report merged #1:\n{r.stdout}\n{r.stderr}"
    )
    # ship must exit non-zero to signal that the main checkout needs manual attention.
    assert r.returncode != 0, (
        f"ship should exit non-zero on a stash-pop conflict; got 0\n{r.stdout}\n{r.stderr}"
    )
    # Diagnostic confirms the merge is done (so the user doesn't double-merge).
    assert "IS merged" in r.stderr, (
        f"diagnostic must confirm PR is merged:\n{r.stderr}"
    )
    # Diagnostic names the conflicting file.
    assert "shared.py" in r.stderr, (
        f"diagnostic must name the conflicting file(s):\n{r.stderr}"
    )
    # Diagnostic tells the user how to resolve.
    assert "stash drop" in r.stderr, (
        f"diagnostic must mention stash drop for resolution:\n{r.stderr}"
    )


def test_stash_pop_no_conflict_clean_tree_passes(repo_with_pr_worktree, tmp_path):
    """Happy path: a clean main checkout → --autostash is a no-op, pull fast-forwards,
    ship exits 0.  Proves --autostash doesn't break the normal case."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    r = _run_ship(main, bindir)

    assert r.returncode == 0, (
        f"clean main checkout should not trigger stash-pop conflict\n{r.stdout}\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout
    assert "stash-pop" not in r.stderr.lower(), (
        f"unexpected stash-pop error on a clean checkout:\n{r.stderr}"
    )


# ── review-dwell gate ──────────────────────────────────────────────────────────────────────
# The gate that closes the "merged before review questions could form" gap: a PR younger than
# SHIP_REVIEW_DWELL seconds (since its last push) is refused so async review has time to post.
# Measured from max(createdAt, head-commit committedDate); the fake gh emits both as a TSV.


def _now_iso() -> str:
    """An ISO-8601 UTC timestamp for 'right now' — a fresh PR the dwell gate must refuse."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_ship_dwell(main, bindir, *, created=None, lastcommit=None, pushed=None, dwell=None,
                    extra_args=()):
    """Run ship from the main checkout, injecting the dwell-gate timestamps (createdAt /
    committedDate / pushedDate) + window. Keeps --skip-ci (the dwell gate runs INDEPENDENTLY of
    --skip-ci) and --no-screenshot-ok."""
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    if created is not None:
        env["SHIP_TEST_CREATED"] = created
    if lastcommit is not None:
        env["SHIP_TEST_LASTCOMMIT"] = lastcommit
    if pushed is not None:
        env["SHIP_TEST_PUSHED"] = pushed
    if dwell is not None:
        env["SHIP_REVIEW_DWELL"] = str(dwell)
    return _sh(
        "bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", *extra_args,
        cwd=main, env=env,
    )


def test_review_dwell_blocks_a_fresh_pr(repo_with_two_worktrees, tmp_path):
    """A PR whose last push is 'now' is younger than the window — ship must REFUSE (this is the
    premature-merge gap: 0 unresolved threads is vacuous before any review posts)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    now = _now_iso()
    r = _run_ship_dwell(main, bindir, created=now, lastcommit=now)
    assert r.returncode != 0, f"fresh PR must be blocked by the dwell gate\n{r.stdout}\n{r.stderr}"
    assert "review-dwell window" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "fresh PR must NOT have merged"


def test_review_dwell_passes_an_aged_pr(repo_with_two_worktrees, tmp_path):
    """A PR pushed long ago is past the window — ship proceeds and reports the gate OK."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship_dwell(main, bindir, created="2020-01-01T00:00:00Z", lastcommit="2020-01-01T00:00:00Z")
    assert r.returncode == 0, f"aged PR should pass the dwell gate\n{r.stdout}\n{r.stderr}"
    assert "review-dwell gate OK" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_dwell_override_flag_allows_fresh_pr(repo_with_two_worktrees, tmp_path):
    """--no-review-dwell-ok <reason> fast-tracks a fresh PR, logging the reason."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    now = _now_iso()
    r = _run_ship_dwell(main, bindir, created=now, lastcommit=now,
                        extra_args=("--no-review-dwell-ok", "urgent hotfix"))
    assert r.returncode == 0, f"override must allow a fresh PR\n{r.stdout}\n{r.stderr}"
    assert "review-dwell gate OVERRIDDEN" in r.stdout and "urgent hotfix" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_dwell_zero_disables_gate(repo_with_two_worktrees, tmp_path):
    """SHIP_REVIEW_DWELL=0 disables the gate entirely (a fresh PR merges)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    now = _now_iso()
    r = _run_ship_dwell(main, bindir, created=now, lastcommit=now, dwell=0)
    assert r.returncode == 0, f"dwell=0 must disable the gate\n{r.stdout}\n{r.stderr}"
    assert "review-dwell gate disabled" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_dwell_uses_pr_open_when_commit_is_stale(repo_with_two_worktrees, tmp_path):
    """A stale-authored commit on a freshly-OPENED PR still waits: the window starts at the
    later of createdAt / committedDate, so a fresh createdAt blocks even with an old commit."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship_dwell(main, bindir, created=_now_iso(), lastcommit="2020-01-01T00:00:00Z")
    assert r.returncode != 0, f"a freshly-opened PR must be blocked even with an old commit\n{r.stdout}\n{r.stderr}"
    assert "review-dwell window" in r.stderr, r.stderr


def test_review_dwell_blocks_on_fresh_pushed_date_with_old_commit(repo_with_two_worktrees, tmp_path):
    """The force-push case (codex review finding): an already-open PR (old createdAt) whose head
    was just force-pushed — old committedDate, but GitHub's pushedDate is FRESH — must still be
    blocked. The window keys off pushedDate (GitHub-controlled), not the commit's embedded date."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship_dwell(
        main, bindir,
        created="2020-01-01T00:00:00Z",      # PR opened long ago
        lastcommit="2020-01-01T00:00:00Z",   # head commit's embedded date is old
        pushed=_now_iso(),                    # ...but GitHub received the (force-)push just now
    )
    assert r.returncode != 0, f"a fresh force-push must restart the window\n{r.stdout}\n{r.stderr}"
    assert "review-dwell window" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_dwell_fails_closed_on_unparseable_timestamps(repo_with_two_worktrees, tmp_path):
    """If the timestamps can't be parsed, ship REFUSES rather than merging un-waited."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship_dwell(main, bindir, created="not-a-date", lastcommit="not-a-date")
    assert r.returncode != 0, f"unparseable timestamps must fail closed\n{r.stdout}\n{r.stderr}"
    assert "could not parse" in r.stderr and "review-dwell" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_dwell_rejects_non_numeric_window(repo_with_two_worktrees, tmp_path):
    """A malformed SHIP_REVIEW_DWELL is refused outright (no silent fall-through to merge)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_DWELL"] = "ten-minutes"
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"non-numeric window must be refused\n{r.stdout}\n{r.stderr}"
    assert "must be a non-negative integer" in r.stderr, r.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
