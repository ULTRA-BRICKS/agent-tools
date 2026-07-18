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

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

# The review-quorum gate (Guard-B, see the "review-quorum preflight gate" section of
# ship.sh) defaults to ENABLED in the product, but almost every test in this file predates
# it and exercises unrelated gates via a fake `gh` with no `review` CLI on PATH. Force it
# off process-wide here; the quorum-gate tests below opt back in explicitly by setting
# SHIP_REVIEW_QUORUM=1 in their own env dict (each test builds `env = dict(os.environ)`,
# so a later explicit assignment there still wins over this default).
os.environ.setdefault("SHIP_REVIEW_QUORUM", "0")

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
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          # One GREEN check so the CI gate passes on the normal (non-admin) merge path — this is
          # how tests that used to pass a bare --skip-ci (a shortcut to skip CI mocking) now run
          # after --skip-ci became a hatch-gated admin bypass. Overridable via SHIP_TEST_ROLLUP.
          if [ -n "${SHIP_TEST_ROLLUP:-}" ]; then
            printf '%s\\n' "$SHIP_TEST_ROLLUP"
          else
            printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
          fi
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
    """Base env for git helpers.

    The hook isolation is on `_git` itself via `git -c core.hooksPath= ...`, so test
    bootstrap commits cannot be blocked by a developer machine's global pre-commit gate.
    """
    return dict(os.environ)


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
        # No --skip-ci: it is now a hatch-gated admin bypass. The fake gh returns a GREEN
        # statusCheckRollup, so the normal CI gate passes and ship does a normal (non-admin) merge.
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", "--dry-run",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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


# Fake gh whose statusCheckRollup is EMPTY (`[]`) — the billing/outage shape where GitHub
# never enqueued the workflows, so NO checks register. Otherwise answers the local-gate
# sub-queries exactly like _FAKE_GH_CIDOWN (review threads → 0, body → empty, diff → clean).
_FAKE_GH_EMPTY_ROLLUP = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q baseRefName; then
          printf '%s' "${SHIP_TEST_BASE:-main}"
        elif printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '[]'
        elif printf '%s ' "$@" | grep -q reviewThreads; then
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
      merge) echo "[fake gh] merged"; [ -n "${SHIP_TEST_MERGE_LOG:-}" ] && printf '%s\\n' "$*" >> "$SHIP_TEST_MERGE_LOG" || true ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""


def _fake_gh_empty_rollup_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "biner"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_EMPTY_ROLLUP, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _run_ship_cidown(main: Path, bindir: Path, env_extra: dict | None = None):
    """Run ship.sh without --skip-ci (CI gate fires) but with --no-screenshot-ok."""
    env = dict(os.environ)
    # Deterministic target inference: clear any ambient GH_REPO/GH_SHIP_REPO the dev/CI shell
    # may carry, so a test only sees a foreign target when it sets one explicitly via env_extra.
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
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


def _add_workflow(main: Path, on_value: str, *, name: str = "ci.yml", body: str | None = None,
                  commit: bool = True, push: bool = True) -> None:
    """Write .github/workflows/<name>. By default a one-trigger file `on: <on_value>`; pass
    `body` for full custom YAML. ship reads the outage signal from `origin/main`, so the file
    must be COMMITTED and PUSHED to count — commit=False leaves it untracked, push=False leaves
    it committed-but-unpushed (both are guard-test shapes that must NOT be treated as an outage)."""
    wf = main / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(body if body is not None else f"name: ci\non: {on_value}\n", encoding="utf-8")
    if commit:
        _git("add", f".github/workflows/{name}", cwd=main)
        _git("commit", "-qm", f"add {name}", cwd=main)
        if push:
            _git("push", "-q", "origin", "main", cwd=main)


def test_empty_rollup_ci_outage_local_gate_passes_merges(repo_with_pr_worktree, tmp_path):
    """Billing/outage shape: a PULL-REQUEST-triggered workflow is committed (a check SHOULD
    register) but the statusCheckRollup is EMPTY (workflows never enqueued). ship must run the
    local fallback gate and merge when it is green — NOT hard-refuse into an ungated `--skip-ci`
    admin bypass. Uses the REAL committed `on: pull_request` signal, no env force."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    merge_log = tmp_path / "merge-args.log"
    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",  # don't wait out the register-grace window in the test
        "SHIP_LOCAL_TEST_CMD": "true",  # trivially-passing stand-in for the test suite
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_MERGE_LOG": str(merge_log),
    })

    assert r.returncode == 0, (
        f"ship must merge when CI registered no checks but the local gate is green\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "registered NO checks" in r.stderr, r.stderr
    assert "local gate PASSED (no checks registered)" in r.stdout, r.stdout
    # The outage path falls through to the NORMAL merge (no --admin bypass) — same posture as the
    # checks-failed structural path. Pin it two ways: the admin log line is absent, AND the actual
    # `gh pr merge` argv (captured by the fake) carries no `--admin` flag — the load-bearing
    # invariant "ship never silently --admin-bypasses branch protection on an outage".
    assert "admin-merging" not in r.stdout, r.stdout
    logged_merge = merge_log.read_text(encoding="utf-8") if merge_log.exists() else ""
    assert logged_merge.strip(), f"fake gh recorded no merge call:\n{r.stdout}"
    assert "--admin" not in logged_merge, f"outage merge must NOT pass --admin:\n{logged_merge}"
    assert "merged #1" in r.stdout, r.stdout


def test_empty_rollup_ci_outage_local_gate_fails_refuses(repo_with_pr_worktree, tmp_path):
    """Empty rollup classified as an outage (committed `on: pull_request` workflow), but the
    local gate FAILS (tests red) → refuse, never merge."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "[push, pull_request]")  # PR-triggered (list form)
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "false",  # local suite fails
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must refuse when the empty-rollup local gate fails\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "local fallback gates also failed" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


# Fake gh whose statusCheckRollup query FAILS (exits non-zero) — a gh/API/network error, NOT a
# successfully-read empty rollup. It still answers the preflight headRefName so ship reaches the
# CI gate, then errors on the rollup read. Guards that ship does NOT coerce an unreadable rollup
# to `[]` and fall into the empty-rollup outage branch (which would merge on a green local gate
# even though the remote check state was never known).
_FAKE_GH_ROLLUP_UNREADABLE = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q baseRefName; then
          printf '%s' "${SHIP_TEST_BASE:-main}"
        elif printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          echo "gh: API error (simulated)" >&2; exit 7
        elif printf '%s ' "$@" | grep -q reviewThreads; then
          echo "0"
        elif printf '%s ' "$@" | grep -q body; then
          echo ""
        else
          echo '[]'
        fi ;;
      diff)
        if printf '%s ' "$@" | grep -q -- --name-only; then printf 'src/a.py'; else printf '+ok\\n'; fi ;;
      comment) : ;;
      merge) echo "[fake gh] merged"; [ -n "${SHIP_TEST_MERGE_LOG:-}" ] && printf '%s\\n' "$*" >> "$SHIP_TEST_MERGE_LOG" || true ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""


def _fake_gh_rollup_unreadable_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "binur"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_ROLLUP_UNREADABLE, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def test_unreadable_rollup_refuses_never_treats_as_empty(repo_with_pr_worktree, tmp_path):
    """gh/API READ FAILURE (not an empty rollup): the statusCheckRollup query exits non-zero, so
    the remote check state is UNKNOWN. Even with a committed `on: pull_request` workflow (which
    would classify a real empty rollup as an outage) AND a green local gate, ship must REFUSE and
    never merge — a failed rollup query must not be coerced to `[]` and slipped through the
    empty-rollup outage path. Regression pin for codex P2 on PR #272."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = _fake_gh_rollup_unreadable_dir(tmp_path)

    merge_log = tmp_path / "merge-args.log"
    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_WAIT": "1",  # bound the retry loop so the refuse is fast
        "SHIP_CI_POLL": "1",
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",  # a GREEN local gate — must NOT be enough on an unknown rollup
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_MERGE_LOG": str(merge_log),
    })

    assert r.returncode != 0, (
        f"ship must refuse when the rollup query FAILS (remote state unknown)\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "remote check state is UNKNOWN" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout
    logged_merge = merge_log.read_text(encoding="utf-8") if merge_log.exists() else ""
    assert not logged_merge.strip(), f"ship must NOT merge on an unreadable rollup:\n{logged_merge}"


_FAKE_GH_GREEN_ROLLUP = """\
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
          printf '[{"name":"pytest","conclusion":"SUCCESS","status":"COMPLETED","state":"SUCCESS"}]'
        elif printf '%s ' "$@" | grep -q reviewThreads; then
          echo "0"
        elif printf '%s ' "$@" | grep -q body; then
          echo ""
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


def test_nonempty_rollup_does_not_enter_outage_path(repo_with_pr_worktree, tmp_path):
    """Regression pin (review, round 5): the empty-rollup outage branch is gated behind an EMPTY
    rollup. A normal green (non-empty) rollup must take the ordinary green-CI path and NEVER the
    outage branch, even when an `on: pull_request` workflow is present."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = tmp_path / "bingreen"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_GREEN_ROLLUP, encoding="utf-8")
    gh.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "false",  # would FAIL if the outage path wrongly ran the local gate
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"a green non-empty rollup must merge via the normal path\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "registered NO checks" not in r.stderr, r.stderr
    assert "local gate PASSED (no checks registered)" not in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_empty_rollup_no_workflows_still_refuses(repo_with_pr_worktree, tmp_path):
    """An empty rollup with NO workflow files = genuinely no CI → keep the hard refuse. An
    empty rollup must NEVER become a free ungated pass just because the local gate is green."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",  # would pass, but must not even be consulted
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must still refuse an empty rollup when the repo has no CI workflows\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_non_pr_workflow_still_refuses(repo_with_pr_worktree, tmp_path):
    """The false-positive guard (review #1): a workflow that does NOT trigger on pull requests
    (`on: [push]`, schedule, workflow_dispatch) legitimately registers NO check on a PR — that
    is correct configured behavior, NOT an outage. ship must keep the hard refuse, never
    admin-bypass branch protection just because a `*.yml` exists."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "[push]")  # NON-PR trigger
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"a push-only workflow must NOT be classified as a CI outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_untracked_pr_workflow_still_refuses(repo_with_pr_worktree, tmp_path):
    """The mutable-state guard (review, Codex): an UNTRACKED (uncommitted) `on: pull_request`
    workflow file in the worktree must NOT flip a no-CI repo into the admin-merge path — the
    outage signal is read from the pushed origin/main ref only, so a stray local file is ignored."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request", commit=False)  # present on disk, NOT committed
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"an untracked local workflow must NOT bypass the no-CI refusal\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_foreign_repo_still_refuses(repo_with_pr_worktree, tmp_path):
    """The foreign-repo guard (review #3, relates #166): a committed `on: pull_request` workflow
    would classify an empty rollup as an outage, but a foreign `--repo` invocation must KEEP the
    hard refuse — the local gate runs against THIS checkout, not the target repo, so its green is
    meaningless for the foreign PR. Uses the empty-rollup fake gh and a real --repo (origin here
    is a local path → any owner/repo is foreign)."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env.update({"SHIP_CI_GRACE": "0", "SHIP_LOCAL_TEST_CMD": "true", "SHIP_REVIEW_DWELL": "0"})
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
    r = _sh(
        "bash", str(_SHIP), "1", "--repo", "owner/repo", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )

    assert r.returncode != 0, (
        f"a foreign --repo empty rollup must NOT merge on a local-gate green\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_push_only_with_stray_pull_request_text_still_refuses(repo_with_pr_worktree, tmp_path):
    """The over-match guard (review #1, round 2): a PUSH-only workflow that merely MENTIONS
    `pull_request` outside its `on:` block — in a comment, and in an
    `if: github.event_name == 'pull_request'` job expression — must NOT be classified as
    PR-triggered. Only the top-level `on:` block is parsed, so this stays a hard refuse (never
    an admin-bypass of branch protection)."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "unused", body=(
        "name: ci\n"
        "# this workflow is not for pull_request events\n"
        "on: [push]\n"
        "jobs:\n"
        "  build:\n"
        "    if: github.event_name == 'pull_request'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    ))
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"a push-only workflow mentioning pull_request must NOT be an outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_committed_unpushed_workflow_still_refuses(repo_with_pr_worktree, tmp_path):
    """The remote-state guard (review #3, Codex): a `on: pull_request` workflow COMMITTED locally
    but NOT pushed is not what GitHub runs from — origin/main lacks it, so an empty remote rollup
    must stay a hard refuse rather than an outage classification off stale local state."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request", commit=True, push=False)  # committed, NOT pushed
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"a committed-but-unpushed workflow must NOT be treated as a live PR CI outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_block_form_pr_trigger_merges(repo_with_pr_worktree, tmp_path):
    """The dominant real-world shape (review #3): a multi-line block-form `on:` with a
    `pull_request:` key (and branch filters) is a genuine PR trigger — an empty rollup on it is
    an outage, so a green local gate merges."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "unused", body=(
        "name: ci\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    ))
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"a block-form pull_request trigger must be recognized as a CI outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "local gate PASSED (no checks registered)" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_empty_rollup_push_branches_filter_with_pull_request_value_still_refuses(repo_with_pr_worktree, tmp_path):
    """The substring-in-filter-value guard (review #1, round 3): a PUSH-only workflow whose
    `branches:` filter VALUE merely contains the text `pull_request` must NOT be classified as a
    PR trigger — the match is structural (trigger key / list item), not a free substring, so this
    stays a hard refuse (never an admin-bypass)."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "unused", body=(
        "name: ci\n"
        "on:\n"
        "  push:\n"
        "    branches: ['feature/pull_request-rework']\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    ))
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"a push-only workflow with pull_request in a branches filter must NOT be an outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_filtered_pr_trigger_is_treated_as_outage(repo_with_pr_worktree, tmp_path):
    """PIN the documented residual (review, round 7): a `pull_request` trigger NARROWED by a
    `branches:` filter is still classified as an outage — the heuristic detects the trigger's
    presence, not whether the filter matches THIS PR. This is the accepted, documented behavior
    (the local gate still runs, so the merge is verified). This test pins it so a future refactor
    that changes the residual is a deliberate, visible change — not a silent one."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "unused", body=(
        "name: ci\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [release]\n"   # a filter that would exclude a PR into main
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    ))
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    # Documented residual: the filtered trigger is treated as a CI outage → local gate → merge.
    assert r.returncode == 0, (
        f"documented residual: a filtered pull_request trigger is classified as an outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "local gate PASSED (no checks registered)" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_empty_rollup_push_bare_list_branch_named_pull_request_still_refuses(repo_with_pr_worktree, tmp_path):
    """The nesting guard (review #1, round 4): a PUSH-only workflow whose `branches:` list has a
    bare item literally named `pull_request` (a branch name, one indent level DEEPER than a
    trigger) must NOT be classified as a PR trigger. Detection is indentation-aware — only DIRECT
    children of `on:` are triggers — so this stays a hard refuse (never an admin-bypass)."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "unused", body=(
        "name: ci\n"
        "on:\n"
        "  push:\n"
        "    branches:\n"
        "      - pull_request\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    ))
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"a bare-list branch named pull_request under push must NOT be an outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_pull_request_target_trigger_merges(repo_with_pr_worktree, tmp_path):
    """The `pull_request_target` trigger (review #3, round 4) is a real PR trigger too — an empty
    rollup on it is an outage, so a green local gate merges. Guards that both trigger spellings
    reach the admin path, not just `pull_request`."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "unused", body=(
        "name: ci\n"
        "on:\n"
        "  pull_request_target:\n"
        "    types: [opened, synchronize]\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    ))
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"a pull_request_target trigger must be recognized as a CI outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout


def test_empty_rollup_foreign_via_gh_ship_repo_still_refuses(repo_with_pr_worktree, tmp_path):
    """The env-target foreign guard (review, Codex): a foreign target named via `GH_SHIP_REPO`
    (not `--repo`) must ALSO keep the hard refuse — the local gate runs against the ambient
    checkout, so its green must not authorize an admin-merge of the foreign repo. Even with a
    committed+pushed `on: pull_request` workflow here, the empty rollup stays a refuse."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "GH_SHIP_REPO": "owner/repo",  # foreign target via env, no --repo flag
    })

    assert r.returncode != 0, (
        f"a foreign GH_SHIP_REPO empty rollup must NOT merge on a local-gate green\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_foreign_via_gh_repo_still_refuses(repo_with_pr_worktree, tmp_path):
    """The env-target foreign guard, raw `GH_REPO` form (review, round 6): `gh` honours a
    pre-set `GH_REPO` as the target too, so a foreign repo named that way must ALSO keep the hard
    refuse — the local gate runs against the ambient checkout, not the target."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "GH_REPO": "owner/repo",  # foreign target via the raw env var gh honours
    })

    assert r.returncode != 0, (
        f"a foreign GH_REPO empty rollup must NOT merge on a local-gate green\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_empty_rollup_pr_base_differs_from_default_refuses(repo_with_pr_worktree, tmp_path):
    """The base-vs-default guard (review, round 6): GitHub evaluates `pull_request` workflows from
    the PR's OWN base branch, not the repo default. A PR whose base is a branch WITHOUT the
    workflow (here `release`, which has no ref/workflow) must NOT be classified as an outage just
    because the default branch has an `on: pull_request` workflow — the signal is read from the
    base ref, which here does not resolve, so ship keeps the hard refuse."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")  # on origin/main (default) only
    bindir = _fake_gh_empty_rollup_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_BASE": "release",  # PR base ≠ default; no origin/release workflow exists
    })

    assert r.returncode != 0, (
        f"a PR based on a branch without the workflow must NOT be an outage\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "has NO CI checks" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_prefers_dev_run_test_when_rig_script_exists(repo_with_pr_worktree, tmp_path):
    """When a repo declares `scripts.test` in rig.yaml and `dev` is installed, the local
    CI-down fallback should use `dev run --repo-only test` instead of guessing a package manager."""
    main, _wt = repo_with_pr_worktree
    (main / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    _git("add", "rig.yaml", cwd=main)
    _git("commit", "-qm", "add rig test script", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    dev_log = tmp_path / "dev.log"
    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = '--agenttools-dev-probe' ]; then exit 0; fi\n"
        "if [ \"$1\" = 'has-script' ]; then\n"
        "  [ \"$2\" = '--repo-only' ] && [ \"$3\" = 'test' ] || exit 98\n"
        "  exit \"${SHIP_TEST_HAS_SCRIPT_STATUS:-0}\"\n"
        "fi\n"
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
    assert dev_log.read_text(encoding="utf-8").strip() == "run --repo-only test"
    assert "[ship] local gate: running dev run --repo-only test (rig.yaml scripts.test)" in r.stdout
    assert "merged #1" in r.stdout, r.stdout


@pytest.mark.parametrize("status", [2, 127])
def test_cidown_local_gate_blocks_dev_probe_errors(repo_with_pr_worktree, tmp_path, status):
    main, _wt = repo_with_pr_worktree
    (main / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    _git("add", "rig.yaml", cwd=main)
    _git("commit", "-qm", "add rig test script", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    dev_log = tmp_path / "dev.log"
    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = '--agenttools-dev-probe' ]; then exit 0; fi\n"
        "if [ \"$1\" = 'has-script' ]; then\n"
        "  [ \"$2\" = '--repo-only' ] && [ \"$3\" = 'test' ] || exit 98\n"
        "  exit \"${SHIP_TEST_HAS_SCRIPT_STATUS:-0}\"\n"
        "fi\n"
        "printf '%s\\n' \"$*\" >> \"$SHIP_TEST_DEV_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    dev.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_TEST_DEV_LOG": str(dev_log),
        "SHIP_TEST_HAS_SCRIPT_STATUS": str(status),
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0
    assert "dev has-script --repo-only test failed" in r.stderr
    assert not dev_log.exists()


def test_cidown_local_gate_ignores_broken_dev_when_repo_has_no_rig_yaml(
    repo_with_pr_worktree, tmp_path
):
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"npm-fallback"}}\n', encoding="utf-8")
    _git("add", "package.json", cwd=main)
    _git("commit", "-qm", "add package test script", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    dev_log = tmp_path / "dev.log"
    npm_log = tmp_path / "npm.log"

    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = '--agenttools-dev-probe' ]; then exit 0; fi\n"
        "if [ \"$1\" = 'has-script' ]; then exit 127; fi\n"
        "printf '%s\\n' \"$*\" >> \"$SHIP_TEST_DEV_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    dev.chmod(0o755)

    npm = bindir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$SHIP_TEST_NPM_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_TEST_DEV_LOG": str(dev_log),
        "SHIP_TEST_NPM_LOG": str(npm_log),
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship should fall back to npm when no repo rig.yaml exists\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not dev_log.exists()
    assert npm_log.read_text(encoding="utf-8").strip() == "test"
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_ignores_foreign_dev_when_probe_fails(
    repo_with_pr_worktree, tmp_path
):
    main, _wt = repo_with_pr_worktree
    (main / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    (main / "package.json").write_text('{"scripts":{"test":"npm-fallback"}}\n', encoding="utf-8")
    _git("add", "rig.yaml", "package.json", cwd=main)
    _git("commit", "-qm", "add rig and package test scripts", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    dev_log = tmp_path / "dev.log"
    npm_log = tmp_path / "npm.log"

    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = '--agenttools-dev-probe' ]; then exit 42; fi\n"
        "printf '%s\\n' \"$*\" >> \"$SHIP_TEST_DEV_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    dev.chmod(0o755)

    npm = bindir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$SHIP_TEST_NPM_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_TEST_DEV_LOG": str(dev_log),
        "SHIP_TEST_NPM_LOG": str(npm_log),
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship should fall back to npm when dev is not agenttools-dev\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not dev_log.exists()
    assert npm_log.read_text(encoding="utf-8").strip() == "test"
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
# CI-gate stale-run DEDUP (the "PR Checklist stale FAILURE" papercut, sibling of the
# review-threads-stale family). GitHub's statusCheckRollup keeps EVERY historical run of a
# check name; it does NOT collapse to the latest. A workflow that re-runs (or reads mutable
# state like pull_request.body — the "PR Checklist" gate fails while an acceptance box is
# unchecked, then passes once ticked) leaves BOTH the old FAILURE and the new SUCCESS in the
# rollup. Counting every entry made ship refuse a PR GitHub itself reports as CLEAN
# (observed live on PR #653, 2026-07-12). ship must dedup to the LATEST run per check
# (workflowName+name / context, keyed on completedAt) before judging pass/fail.
# ---------------------------------------------------------------------------------------

# A fake `gh` whose statusCheckRollup is injected verbatim from $SHIP_TEST_ROLLUP, so each
# test drives the exact multi-run shape under test. review-threads -> 0, diff clean.
_FAKE_GH_ROLLUP = """\
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
          printf '%s' "${SHIP_TEST_ROLLUP}"
        elif printf '%s ' "$@" | grep -q body; then
          echo ""
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


def _fake_gh_rollup_dir(tmp_path: Path) -> Path:
    # The rollup itself is injected via $SHIP_TEST_ROLLUP (see _run_ship_rollup); this only
    # installs the fake `gh` that echoes it.
    bindir = tmp_path / "binroll"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_ROLLUP, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _run_ship_rollup(main: Path, bindir: Path, rollup: str):
    """Run ship.sh with the CI gate LIVE (no --skip-ci) and the rollup injected via env."""
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_ROLLUP"] = rollup
    env["SHIP_REVIEW_DWELL"] = "0"  # fake PR has no review timestamps
    return _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )


# A fake `gh` for the pending-rerun case: the statusCheckRollup answer flips based on a
# per-call counter file. On the first N polls it returns $SHIP_TEST_ROLLUP_PENDING (a check
# whose latest run is still QUEUED); afterwards $SHIP_TEST_ROLLUP_SETTLED (the re-run
# completed). Proves the pending re-run is WATCHED to completion, not dropped in favour of a
# stale completed FAILURE of the same check.
_FAKE_GH_ROLLUP_FLIP = """\
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
          n=0; [ -f "${SHIP_TEST_COUNTER}" ] && n=$(cat "${SHIP_TEST_COUNTER}")
          n=$((n+1)); printf '%s' "$n" > "${SHIP_TEST_COUNTER}"
          if [ "$n" -le "${SHIP_TEST_PENDING_POLLS:-1}" ]; then
            printf '%s' "${SHIP_TEST_ROLLUP_PENDING}"
          else
            printf '%s' "${SHIP_TEST_ROLLUP_SETTLED}"
          fi
        elif printf '%s ' "$@" | grep -q body; then
          echo ""
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


def test_pending_rerun_is_watched_not_blocked_by_stale_failure(repo_with_pr_worktree, tmp_path):
    """Regression: a check with an OLD completed FAILURE plus a NEWER re-run that is still
    QUEUED (null started/completed timestamps) must be judged by the re-run — the gate WATCHES
    the pending re-run to completion, it does NOT immediately block on the stale FAILURE.
    Ranking by completedAt alone would keep the timestamped stale FAILURE (the queued re-run
    has no timestamp) and block instantly; the pending-wins sentinel prevents that."""
    bindir = tmp_path / "binflip"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_ROLLUP_FLIP, encoding="utf-8")
    gh.chmod(0o755)

    pending = (
        '['
        '{"__typename":"CheckRun","name":"PR Checklist","workflowName":"PR Checklist",'
        '"status":"COMPLETED","conclusion":"FAILURE","completedAt":"2026-07-11T23:23:12Z"},'
        '{"__typename":"CheckRun","name":"PR Checklist","workflowName":"PR Checklist",'
        '"status":"QUEUED","conclusion":null,"startedAt":null,"completedAt":null}'
        ']'
    )
    settled = (
        '['
        '{"__typename":"CheckRun","name":"PR Checklist","workflowName":"PR Checklist",'
        '"status":"COMPLETED","conclusion":"FAILURE","completedAt":"2026-07-11T23:23:12Z"},'
        '{"__typename":"CheckRun","name":"PR Checklist","workflowName":"PR Checklist",'
        '"status":"COMPLETED","conclusion":"SUCCESS","completedAt":"2026-07-11T23:40:00Z"}'
        ']'
    )

    main, _wt = repo_with_pr_worktree
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_DWELL"] = "0"
    env["SHIP_TEST_COUNTER"] = str(tmp_path / "poll.count")
    env["SHIP_TEST_PENDING_POLLS"] = "1"  # first poll pending, then settled
    env["SHIP_TEST_ROLLUP_PENDING"] = pending
    env["SHIP_TEST_ROLLUP_SETTLED"] = settled
    env["SHIP_CI_POLL"] = "1"   # watch quickly
    env["SHIP_CI_WAIT"] = "30"  # generous ceiling; we expect settle on poll 2

    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)

    assert r.returncode == 0, (
        "ship must watch the pending re-run then merge on its SUCCESS, not block on the stale "
        f"FAILURE\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout
    # It genuinely entered the watch loop (proves the pending re-run was kept, not dropped).
    assert "still running" in r.stdout, r.stdout
    assert "not passing" not in r.stderr, r.stderr


# Two distinct checks, each with an OLD run + a NEWER run of the same name. "PR Checklist"
# went FAILURE (23:23) then SUCCESS (23:36); "Tests" went FAILURE (22:40) then SUCCESS
# (22:53). Every check's LATEST run is green even though stale FAILUREs linger.
_ROLLUP_STALE_FAIL_THEN_SUCCESS = (
    '['
    '{"__typename":"CheckRun","name":"PR Checklist","workflowName":"PR Checklist",'
    '"status":"COMPLETED","conclusion":"FAILURE","completedAt":"2026-07-11T23:23:12Z"},'
    '{"__typename":"CheckRun","name":"PR Checklist","workflowName":"PR Checklist",'
    '"status":"COMPLETED","conclusion":"SUCCESS","completedAt":"2026-07-11T23:36:30Z"},'
    '{"__typename":"CheckRun","name":"Tests","workflowName":"CI",'
    '"status":"COMPLETED","conclusion":"FAILURE","completedAt":"2026-07-11T22:40:00Z"},'
    '{"__typename":"CheckRun","name":"Tests","workflowName":"CI",'
    '"status":"COMPLETED","conclusion":"SUCCESS","completedAt":"2026-07-11T22:53:29Z"}'
    ']'
)

# "Tests" went SUCCESS (23:00) then FAILURE (23:30) — its LATEST run is red. "Lint" is a
# clean single SUCCESS. Deduped: 1 failing of 2 (50%, below the CI-down threshold) → the
# gate must still refuse via the normal CI-failure path.
_ROLLUP_LATEST_IS_FAILURE = (
    '['
    '{"__typename":"CheckRun","name":"Tests","workflowName":"CI",'
    '"status":"COMPLETED","conclusion":"SUCCESS","completedAt":"2026-07-11T23:00:00Z"},'
    '{"__typename":"CheckRun","name":"Tests","workflowName":"CI",'
    '"status":"COMPLETED","conclusion":"FAILURE","completedAt":"2026-07-11T23:30:00Z"},'
    '{"__typename":"CheckRun","name":"Lint","workflowName":"CI",'
    '"status":"COMPLETED","conclusion":"SUCCESS","completedAt":"2026-07-11T23:10:00Z"}'
    ']'
)


def test_stale_failure_with_newer_success_does_not_block(repo_with_pr_worktree, tmp_path):
    """Regression (PR #653): a check with an OLD FAILURE run + a NEWER SUCCESS run of the
    same name must be judged by its LATEST run (SUCCESS) — ship merges. Two distinct checks,
    each carrying a stale FAILURE, prove the dedup is per-check-name and covers the mixed
    case. Without the dedup, ship counts the stale FAILUREs and wrongly refuses."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_rollup_dir(tmp_path)

    r = _run_ship_rollup(main, bindir, _ROLLUP_STALE_FAIL_THEN_SUCCESS)

    assert r.returncode == 0, (
        "ship must merge when every check's LATEST run is green despite stale FAILUREs\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout
    # The gate must NOT have reported any failing check.
    assert "not passing" not in r.stderr, r.stderr


# A StatusContext and a CheckRun that share the NAME "coverage" are DISTINCT checks (the
# CheckRun here has a null workflowName, as third-party app checks do). The CheckRun is
# FAILURE; the same-named StatusContext is a newer SUCCESS. They must NOT collapse into one
# group — else the newer passing status would hide the still-failing check run and weaken the
# gate. __typename is part of the dedup key precisely to keep them separate.
_ROLLUP_STATUSCONTEXT_VS_CHECKRUN_SAME_NAME = (
    '['
    '{"__typename":"CheckRun","name":"coverage","workflowName":null,'
    '"status":"COMPLETED","conclusion":"FAILURE","completedAt":"2026-07-11T23:20:00Z"},'
    '{"__typename":"StatusContext","context":"coverage","state":"SUCCESS",'
    '"createdAt":"2026-07-11T23:30:00Z"},'
    '{"__typename":"CheckRun","name":"Lint","workflowName":"CI",'
    '"status":"COMPLETED","conclusion":"SUCCESS","completedAt":"2026-07-11T23:10:00Z"}'
    ']'
)


def test_same_name_statuscontext_and_checkrun_do_not_collapse(repo_with_pr_worktree, tmp_path):
    """A failing CheckRun and a newer passing StatusContext that share a name are distinct
    checks; the dedup must key on __typename too so the passing status does not hide the
    failing check run. ship must still refuse (the coverage CheckRun is FAILURE)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_rollup_dir(tmp_path)

    r = _run_ship_rollup(main, bindir, _ROLLUP_STATUSCONTEXT_VS_CHECKRUN_SAME_NAME)

    assert r.returncode != 0, (
        "ship must refuse: a same-named StatusContext must not hide a failing CheckRun\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not passing" in r.stderr, r.stderr
    assert "coverage" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


# A single context whose status timeline holds an OLD PENDING and a NEWER SUCCESS (both carry
# a createdAt). The newer SUCCESS must win — the pending-wins sentinel is reserved for
# TIMESTAMP-LESS queued runs only, so a timestamped stale PENDING must NOT dominate a newer
# settled SUCCESS (which would make ship wait out SHIP_CI_WAIT on a green PR).
_ROLLUP_STALE_PENDING_THEN_SUCCESS = (
    '['
    '{"__typename":"StatusContext","context":"ci","state":"PENDING",'
    '"targetUrl":"https://ci.example/1","createdAt":"2026-07-11T23:00:00Z"},'
    '{"__typename":"StatusContext","context":"ci","state":"SUCCESS",'
    '"targetUrl":"https://ci.example/2","createdAt":"2026-07-11T23:10:00Z"}'
    ']'
)

# Two DISTINCT providers both post a CheckRun named "coverage" with a null workflowName but
# different detailsUrl hosts. One is FAILURE, the other SUCCESS. They must NOT collapse — the
# host is part of the key — so the failing provider's check still gates the merge.
_ROLLUP_DISTINCT_PROVIDERS_SAME_NAME = (
    '['
    '{"__typename":"CheckRun","name":"coverage","workflowName":null,"status":"COMPLETED",'
    '"conclusion":"FAILURE","completedAt":"2026-07-11T23:20:00Z",'
    '"detailsUrl":"https://app-a.example/run/1"},'
    '{"__typename":"CheckRun","name":"coverage","workflowName":null,"status":"COMPLETED",'
    '"conclusion":"SUCCESS","completedAt":"2026-07-11T23:30:00Z",'
    '"detailsUrl":"https://app-b.example/run/2"}'
    ']'
)


# Two CheckRuns share the name "build" but come from DIFFERENT workflows (workflowName
# "CI" vs "Release"). They are distinct required checks; workflowName is in the key so they do
# not collapse. One is FAILURE, so the merge must still be refused.
_ROLLUP_SAME_NAME_DIFFERENT_WORKFLOW = (
    '['
    '{"__typename":"CheckRun","name":"build","workflowName":"CI","status":"COMPLETED",'
    '"conclusion":"SUCCESS","completedAt":"2026-07-11T23:00:00Z"},'
    '{"__typename":"CheckRun","name":"build","workflowName":"Release","status":"COMPLETED",'
    '"conclusion":"FAILURE","completedAt":"2026-07-11T23:05:00Z"}'
    ']'
)


def test_same_name_different_workflow_do_not_collapse(repo_with_pr_worktree, tmp_path):
    """Two checks that share a name but belong to different workflows are distinct; the dedup
    keys on workflowName so a passing 'build' in CI does not hide a failing 'build' in Release.
    ship must refuse."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_rollup_dir(tmp_path)

    r = _run_ship_rollup(main, bindir, _ROLLUP_SAME_NAME_DIFFERENT_WORKFLOW)

    assert r.returncode != 0, (
        "ship must refuse: a passing check must not hide a same-named check from another "
        f"workflow\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not passing" in r.stderr, r.stderr
    assert "build" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_stale_pending_statuscontext_yields_to_newer_success(repo_with_pr_worktree, tmp_path):
    """A timestamped stale PENDING status must not dominate a newer SUCCESS of the same
    context: the sentinel is only for timestamp-less queued runs. ship must merge, not sit in
    the watch loop until SHIP_CI_WAIT on a PR whose latest status is green."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_rollup_dir(tmp_path)

    r = _run_ship_rollup(main, bindir, _ROLLUP_STALE_PENDING_THEN_SUCCESS)

    assert r.returncode == 0, (
        "ship must merge: newer SUCCESS status supersedes the older PENDING of the same "
        f"context\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "merged #1" in r.stdout, r.stdout
    assert "pending check" not in r.stdout, r.stdout


def test_distinct_providers_same_name_checkrun_do_not_collapse(repo_with_pr_worktree, tmp_path):
    """Two third-party CheckRuns sharing the name 'coverage' with a null workflowName but
    different detailsUrl hosts are DISTINCT checks; the dedup keys on the host so the passing
    provider does not hide the failing one. ship must refuse."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_rollup_dir(tmp_path)

    r = _run_ship_rollup(main, bindir, _ROLLUP_DISTINCT_PROVIDERS_SAME_NAME)

    assert r.returncode != 0, (
        "ship must refuse: a passing provider must not hide a distinct failing provider of the "
        f"same check name\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not passing" in r.stderr, r.stderr
    assert "coverage" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_latest_run_failure_still_blocks(repo_with_pr_worktree, tmp_path):
    """Fail-closed: dedup keeps the LATEST run, so a check whose newest run is FAILURE (even
    after an older SUCCESS) still blocks the merge — the dedup must not weaken the gate."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_rollup_dir(tmp_path)

    r = _run_ship_rollup(main, bindir, _ROLLUP_LATEST_IS_FAILURE)

    assert r.returncode != 0, (
        "ship must refuse when a check's LATEST run is FAILURE\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not passing" in r.stderr, r.stderr
    # It's the Tests check (latest FAILURE) that blocks, not Lint (clean SUCCESS).
    assert "Tests" in r.stderr, r.stderr
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
        "bash", str(_SHIP), "1", *repo_args, "--no-screenshot-ok", "test",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
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
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
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


# --- review-quorum preflight gate (Guard-B, self-merge-authority program) -----------------
# A fake `gh` complete enough that --skip-ci + --no-screenshot-ok leaves every OTHER gate
# passing trivially (docs-only diff -> no version bump required, no UI touched; 0 unresolved
# threads; old dwell timestamps), so these tests isolate the review-quorum gate itself. Also
# answers `gh pr view --json body` (task-code-from-PR-body derivation) via SHIP_TEST_PR_BODY.
_FAKE_GH_QUORUM = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        case "$*" in
          *headRefName*) printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}" ;;
          *"--json body"*) printf '%s' "${SHIP_TEST_PR_BODY:-}" ;;
          *statusCheckRollup*) printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]' ;;
          *) echo '[]' ;;
        esac ;;
      diff) echo "README.md" ;;   # docs-only: version-bump/screenshot gates pass trivially
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    if printf '%s ' "$@" | grep -q committedDate; then
      printf '2020-01-01T00:00:00Z\\t\\t2020-01-01T00:00:00Z\\n'   # old dwell window -> passes
    else
      echo "0"   # 0 unresolved review threads
    fi ;;
  *) : ;;
esac
"""

# A fake `review` CLI answering `review task <code> [--check|--quorum-check] --min-iter N
# --min-models M --json`. Behavior is driven entirely by env vars so each test controls it:
#   SHIP_TEST_REVIEW_ITER / SHIP_TEST_REVIEW_MODELS   iteration/model counts to report (default 3/3)
#   SHIP_TEST_REVIEW_SUPPORTS_CHECK=0   reject --check with an argparse-style error (exit 2,
#                                       empty stdout) so ship.sh must fall back to --quorum-check
#   SHIP_TEST_REVIEW_BROKEN=1           fail BOTH --check and --quorum-check (simulates an
#                                       unreadable stats store) -> ship.sh must fail closed
#   SHIP_TEST_REVIEW_LOG                if set, append the flag actually used (check/quorum-check)
#                                       so a test can assert which one ship.sh invoked
#   SHIP_TEST_REVIEW_FORCE_PASSED       if set (true/false), emit `passed` VERBATIM instead of
#                                       computing it from the counts — lets a test forge a hollow
#                                       `passed:true` with 0/0 counts (an older/hostile review-cli)
#                                       to prove ship's independent arithmetic gate fails closed (#242).
#   SHIP_TEST_REVIEW_MIN_ITER_ECHO      if set, emit THIS as the JSON `min_iter`/`min_models` echo
#                                       instead of the flag values, so a test can inspect the floor
#                                       ship actually passed to review-cli.
#
# The JSON keys match review-cli's real output: `passed_iterations` / `distinct_models_passed`
# (NOT `iterations` / `distinct_models`, which review-cli never emitted — that key mismatch was
# half of the #242 hole; a fake using the wrong keys would validate a fiction).
_FAKE_REVIEW = """\
#!/usr/bin/env bash
sub="${1:-}"; shift || true
[ "$sub" = "task" ] || exit 0
code="${1:-}"; shift || true
flag=""
minit=3
minmodels=3
while [ $# -gt 0 ]; do
  case "$1" in
    --check) flag="check" ;;
    --quorum-check) flag="quorum-check" ;;
    --min-iter) shift; minit="$1" ;;
    --min-models) shift; minmodels="$1" ;;
  esac
  shift || true
done
if [ "$flag" = "check" ] && [ "${SHIP_TEST_REVIEW_SUPPORTS_CHECK:-1}" = "0" ]; then
  echo "review task: error: unrecognized arguments: --check" >&2
  exit 2
fi
[ -n "${SHIP_TEST_REVIEW_LOG:-}" ] && printf '%s\\n' "$flag" >> "${SHIP_TEST_REVIEW_LOG}"
if [ "${SHIP_TEST_REVIEW_BROKEN:-0}" = "1" ]; then
  echo "internal error: stats store unreadable" >&2
  exit 1
fi
iterations="${SHIP_TEST_REVIEW_ITER:-3}"
models_n="${SHIP_TEST_REVIEW_MODELS:-3}"
if [ -n "${SHIP_TEST_REVIEW_FORCE_PASSED:-}" ]; then
  passed="${SHIP_TEST_REVIEW_FORCE_PASSED}"
else
  passed="false"
  if [ "$iterations" -ge "$minit" ] && [ "$models_n" -ge "$minmodels" ]; then passed="true"; fi
fi
echo_min="${SHIP_TEST_REVIEW_MIN_ITER_ECHO:-}"
[ -n "$echo_min" ] && { minit="$echo_min"; minmodels="$echo_min"; }
if [ "${SHIP_TEST_REVIEW_LEGACY_KEYS:-0}" = "1" ]; then
  # Emit ONLY the never-emitted legacy key names (`iterations` / `distinct_models`) to prove ship
  # reads the REAL keys and treats a legacy-only payload as 0/0 -> fail-closed refuse (#242).
  printf '{"task_code":"%s","iterations":%s,"distinct_models":%s,"models":["claude","codex","gemini"],"min_iter":%s,"min_models":%s,"passed":%s}\\n' \\
    "$code" "$iterations" "$models_n" "$minit" "$minmodels" "$passed"
else
  printf '{"task_code":"%s","passed_iterations":%s,"total_iterations":%s,"distinct_models_passed":%s,"models":["claude","codex","gemini"],"min_iter":%s,"min_models":%s,"passed":%s}\\n' \\
    "$code" "$iterations" "$iterations" "$models_n" "$minit" "$minmodels" "$passed"
fi
"""


def _fake_gh_quorum_dir(tmp_path: Path, name: str = "bin_gh_quorum") -> Path:
    bindir = tmp_path / name
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_QUORUM, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _fake_review_dir(tmp_path: Path, name: str = "bin_review") -> Path:
    bindir = tmp_path / name
    bindir.mkdir()
    fp = bindir / "review"
    fp.write_text(_FAKE_REVIEW, encoding="utf-8")
    fp.chmod(0o755)
    return bindir


def _make_repo_with_branch(tmp_path: Path, branch: str):
    """A minimal repo on `main` with ONE feature branch (name controllable, for task-code
    derivation from the branch) checked out in a worktree, plus an origin remote."""
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
    _git("branch", branch, cwd=main)
    _git("push", "-q", "origin", branch, cwd=main)
    wt = tmp_path / "wt-quorum"
    _git("worktree", "add", "-q", str(wt), branch, cwd=main)
    return main, wt


def _minimal_hermetic_path(*bindirs) -> str:
    """A PATH built ONLY from the given fake-binary dirs plus the real dirs holding bash/git/jq
    — deliberately excluding the ambient PATH. The quorum gate's "review CLI missing" test needs
    this: this dev machine has a REAL `review` (review-cli) installed, and if the ambient PATH
    were appended it would shadow the intended absence and the test would exercise the wrong
    code path (a live query against the real store instead of a missing-binary refusal)."""
    dirs = [str(d) for d in bindirs if d is not None]
    # python3 is needed by the review-quorum hatch escalation bridge (ship.sh shells out to it
    # to call the shared agenttools_hatch_escalation lib), so it must be on the hermetic PATH.
    for tool in ("bash", "git", "jq", "python3"):
        found = shutil.which(tool)
        if found:
            d = str(Path(found).resolve().parent)
            if d not in dirs:
                dirs.append(d)
    for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if d not in dirs:
            dirs.append(d)
    return os.pathsep.join(dirs)


def _write_fake_tg_ctl(tmp_path: Path, *, name: str, body: str) -> Path:
    """A fake `tg-ctl` the hatch lib will invoke as `tg-ctl ask <question> --timeout <s>`.
    `body` is the shell after the shebang; $2 is the question text."""
    fp = tmp_path / name
    fp.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    fp.chmod(0o755)
    return fp


def _fake_home_with_tg_ctl(tmp_path: Path, tg_ctl: Path, *, name: str = "fake-home") -> Path:
    """A fake HOME dir carrying a rig.yaml whose `agent_hooks.tg_ctl_path` points at the fake
    tg-ctl. The hatch helper resolves tg-ctl from the OS account's real home (pwd.getpwuid), so
    in-process module tests monkeypatch `resolve_home` to return THIS dir — a controllable tg-ctl
    that never touches the real one or messages Alex, matching the production resolution path."""
    home = tmp_path / name
    home.mkdir()
    (home / "rig.yaml").write_text(
        f'agent_hooks:\n  tg_ctl_path: "{tg_ctl}"\n', encoding="utf-8"
    )
    return home


def _run_ship_quorum(main, gh_bindir, review_bindir, *, branch="feat", extra_args=(), env_extra=None):
    env = dict(os.environ)
    env["PATH"] = _minimal_hermetic_path(gh_bindir, review_bindir)
    env["SHIP_TEST_BRANCH"] = branch
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"   # opt back into the gate (module default is 0)
    # Never let an ambient hatch-request env var leak into a test that doesn't set one (it would
    # turn a plain refusal into a live tg-ctl call). Tests that exercise the hatch set it via
    # env_extra AFTER this pop.
    env.pop("RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM", None)
    if env_extra:
        env.update(env_extra)
    return _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
        cwd=main, env=env,
    )


def test_review_quorum_blocks_when_bar_short(tmp_path):
    """Fewer recorded iterations/models than the default 3/3 floor -> ship REFUSES."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-100", "SHIP_TEST_REVIEW_ITER": "1", "SHIP_TEST_REVIEW_MODELS": "1"},
    )
    assert r.returncode != 0, f"a short quorum must be refused\n{r.stdout}\n{r.stderr}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "HYP-100" in r.stderr, r.stderr
    # No self-service override exists; the refusal must point at the hatch env var instead.
    assert "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_passes_and_authorizes_when_bar_met(tmp_path):
    """3 iterations across 3 models (the default floor) -> AUTHORITY CONFIRMED, then merges."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-101", "SHIP_TEST_REVIEW_ITER": "3", "SHIP_TEST_REVIEW_MODELS": "3"},
    )
    assert r.returncode == 0, f"a met quorum should pass and merge\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "HYP-101" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_calls_check_flag_by_default(tmp_path):
    """The default invocation prefers --check (the review-cli rename target), not the legacy
    --quorum-check spelling."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    log = tmp_path / "review-flag.log"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-102", "SHIP_TEST_REVIEW_LOG": str(log)},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert log.read_text().strip() == "check", f"expected --check to be used, log: {log.read_text()!r}"


def test_review_quorum_falls_back_to_quorum_check_flag(tmp_path):
    """When the installed review-cli doesn't yet support --check (rename in flight), ship.sh
    falls back to the legacy --quorum-check spelling and still evaluates correctly."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    log = tmp_path / "review-flag.log"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-103",
            "SHIP_TEST_REVIEW_SUPPORTS_CHECK": "0",
            "SHIP_TEST_REVIEW_LOG": str(log),
        },
    )
    assert r.returncode == 0, f"fallback to --quorum-check should still pass a met quorum\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert log.read_text().strip() == "quorum-check", f"expected the --quorum-check fallback, log: {log.read_text()!r}"


def test_review_quorum_disabled_via_env(tmp_path):
    """SHIP_REVIEW_QUORUM=0 disables the gate entirely — merges even with no review CLI on
    PATH and no derivable task code."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    r = _run_ship_quorum(main, gh, review_bindir=None, env_extra={"SHIP_REVIEW_QUORUM": "0"})
    assert r.returncode == 0, f"disabled gate must not block the ship\n{r.stdout}\n{r.stderr}"
    assert "review-quorum gate disabled" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_fails_closed_when_review_cli_missing(tmp_path):
    """No `review` binary on PATH -> refuse rather than merge unverified (fail-closed)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    r = _run_ship_quorum(main, gh, review_bindir=None, env_extra={"REVIEW_TASK_CODE": "HYP-105"})
    assert r.returncode != 0, f"a missing review CLI must fail closed\n{r.stdout}\n{r.stderr}"
    assert "'review' CLI not found" in r.stderr, r.stderr
    assert "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_fails_closed_when_store_broken(tmp_path):
    """`review` is present but its store is unreadable (both flags fail) -> refuse rather than
    merge unverified (fail-closed)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-106", "SHIP_TEST_REVIEW_BROKEN": "1"},
    )
    assert r.returncode != 0, f"a broken review-cli store must fail closed\n{r.stdout}\n{r.stderr}"
    assert "could not query review-cli" in r.stderr, r.stderr
    assert "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_derives_code_from_branch_name(tmp_path):
    """No $REVIEW_TASK_CODE set -> ship extracts the ticket token (HYP-742) from the branch name."""
    main, _wt = _make_repo_with_branch(tmp_path, "fix/HYP-742-widget")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, branch="fix/HYP-742-widget")
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "HYP-742" in r.stdout, r.stdout


def test_review_quorum_derives_code_from_pr_body(tmp_path):
    """No $REVIEW_TASK_CODE and no ticket in the branch name -> ship falls back to a ticket
    token embedded in the PR body."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": "Fixes HYP-999 for the widget regression."},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "HYP-999" in r.stdout, r.stdout


def test_review_quorum_refuses_when_no_task_code_derivable(tmp_path):
    """No $REVIEW_TASK_CODE, no ticket in the branch, no ticket in the PR body -> refuse with
    guidance rather than silently skip the gate."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": "no ticket here"})
    assert r.returncode != 0, f"an undiscoverable task code must refuse\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "REVIEW_TASK_CODE" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_audit_log_records_authorized(tmp_path):
    """A bar-met ship appends an 'authorized' JSONL audit line with the task code, iteration,
    and model counts."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-107", "SHIP_AUDIT_FILE": str(audit)},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one audit line, got: {lines}"
    rec = json.loads(lines[0])
    assert rec["decision"] == "authorized", rec
    assert rec["task_code"] == "HYP-107", rec
    assert rec["iterations"] == 3, rec
    assert rec["models"] == 3, rec
    assert rec["pr"] == "1", rec
    assert "ts" in rec, rec


def test_review_quorum_audit_log_records_refused(tmp_path):
    """A bar-short ship (that is then blocked) still appends a 'refused' JSONL audit line."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-108", "SHIP_TEST_REVIEW_ITER": "1", "SHIP_TEST_REVIEW_MODELS": "1",
            "SHIP_AUDIT_FILE": str(audit),
        },
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    rec = json.loads(audit.read_text().strip().splitlines()[0])
    assert rec["decision"] == "refused", rec
    assert rec["task_code"] == "HYP-108", rec
    assert rec["iterations"] == 1, rec
    assert rec["models"] == 1, rec


def test_review_quorum_audit_log_skipped_in_dry_run(tmp_path):
    """--dry-run must honor its 'change nothing' contract: a bar-met (would-be authorized) run
    still evaluates and prints the gate verdict, but MUST NOT create or append to the audit file.
    Regression for the Codex P2 (#225) where the audit line was written even in dry-run."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv, extra_args=("--dry-run",),
        env_extra={"REVIEW_TASK_CODE": "HYP-109", "SHIP_AUDIT_FILE": str(audit)},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    # The gate still evaluated and reported it would authorize...
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "[dry-run] would append review-quorum audit" in r.stderr, r.stderr
    # ...but no persistent audit record was written.
    assert not audit.exists(), f"dry-run must not write the audit file, found: {audit.read_text()!r}"


# --- #242: fail-closed against a hollow / forged / mis-floored quorum ----------------------
# The #242 hole: shipping review-cli #141, the `gh ship` subprocess printed "review quorum met:
# 0 iterations across 0 models" and STILL authorized. Two compounding bugs: (a) ship parsed the
# JSON keys `.iterations` / `.distinct_models`, which review-cli never emits (it emits
# `passed_iterations` / `distinct_models_passed`), so ship's counts were ALWAYS 0; (b) ship
# authorized on the subprocess's `.passed` boolean ALONE, never re-checking the numbers against
# its own floor. Combined, a `passed:true` with a hollow 0/0 record self-merged an empty quorum.
# These tests pin the fail-closed fix: ship re-derives the verdict from the real-key counts and
# a hard >=3 floor, so a forged pass, a below-floor override, or a wrong-key payload is REFUSED.


def test_review_quorum_refuses_forged_pass_with_zero_counts(tmp_path):
    """#242 core: review-cli returns `passed:true` but 0 passed iterations across 0 models
    (an older build without the min>=1 guard, a task-code miss, or a hostile `review` on PATH).
    ship must NOT trust the boolean — its independent arithmetic sees 0/3 and REFUSES."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-242",
            "SHIP_TEST_REVIEW_ITER": "0",
            "SHIP_TEST_REVIEW_MODELS": "0",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "true",  # forge the hollow authorization
        },
    )
    assert r.returncode != 0, f"a forged 0/0 pass MUST be refused\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" not in r.stdout, f"must not authorize a hollow quorum\n{r.stdout}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout and "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_reads_real_passed_iteration_keys(tmp_path):
    """A real ≥3×3 record (review-cli's `passed_iterations` / `distinct_models_passed` keys)
    is read correctly and AUTHORIZES — proving ship parses the keys review-cli actually emits,
    not the never-emitted `.iterations` / `.distinct_models` that always parsed to 0 (#242)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-242", "SHIP_TEST_REVIEW_ITER": "4", "SHIP_TEST_REVIEW_MODELS": "3"},
    )
    assert r.returncode == 0, f"a real 4×3 record should authorize\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    # The confirmed line must show the REAL counts (4 iterations across 3 models), not 0/0.
    assert "4 iterations across 3 models" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_refuses_legacy_key_only_payload(tmp_path):
    """A payload carrying ONLY the never-emitted legacy keys (`iterations` / `distinct_models`)
    with `passed:true` and 3×3 — an old build or a hostile `review` on PATH — must read as 0/0
    (ship reads only `passed_iterations` / `distinct_models_passed`) and fail closed (#242)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-242",
            "SHIP_TEST_REVIEW_LEGACY_KEYS": "1",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_MODELS": "3",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "true",
        },
    )
    assert r.returncode != 0, f"a legacy-key-only payload must be refused\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "bar NOT met" in r.stderr, r.stderr
    assert "0/3 iterations" in r.stderr, f"legacy keys must read as 0 counts\n{r.stderr}"
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_floor_clamped_when_env_sets_zero(tmp_path):
    """An attempt to weaken the bar via SHIP_REVIEW_QUORUM_MIN_ITER/MODELS=0 must NOT resolve to
    a 0 floor (which would let a 0/0 record pass via 0>=0). ship clamps to the hard floor 3, so a
    genuinely hollow 0/0 record with the weakened env is STILL refused (#242)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-242",
            "SHIP_REVIEW_QUORUM_MIN_ITER": "0",
            "SHIP_REVIEW_QUORUM_MIN_MODELS": "0",
            "SHIP_TEST_REVIEW_ITER": "0",
            "SHIP_TEST_REVIEW_MODELS": "0",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "true",
        },
    )
    assert r.returncode != 0, f"a 0 floor must be clamped and the hollow record refused\n{r.stdout}\n{r.stderr}"
    assert "hard floor" in r.stderr, f"expected a floor-clamp warning\n{r.stderr}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_below_floor_positive_value_cannot_weaken_bar(tmp_path):
    """A below-floor POSITIVE override (MIN_ITER=2, MIN_MODELS=2) with a real 2×2 record must NOT
    authorize: the floor is clamped to 3, so ship demands 3×3 and the 2×2 record refuses with
    '2/3'. Distinct from the 0/0 test (which the >0 guard alone would catch) — this proves the
    clamp itself blocks a positive-but-sub-floor weakening, the case codex flagged (#242)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-242",
            "SHIP_REVIEW_QUORUM_MIN_ITER": "2",
            "SHIP_REVIEW_QUORUM_MIN_MODELS": "2",
            "SHIP_TEST_REVIEW_ITER": "2",
            "SHIP_TEST_REVIEW_MODELS": "2",
        },
    )
    assert r.returncode != 0, f"a 2×2 record under a clamped-to-3 floor must refuse\n{r.stdout}\n{r.stderr}"
    assert "hard floor" in r.stderr, f"expected a floor-clamp warning raising 2 to 3\n{r.stderr}"
    assert "2/3 iterations" in r.stderr, f"floor must be enforced at 3, not the weakened 2\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


# --- hatch escalation: the ONLY bypass for a not-met review-quorum bar ---------------------
# There is no self-service override flag. A short bar can only be bypassed by setting
# RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>", which routes through the
# ci/ship/review_quorum_hatch.py helper -> the shared agenttools_hatch_escalation lib ->
# `tg-ctl ask` Alex live. The helper resolves tg-ctl from the OS account's REAL home
# (pwd.getpwuid), NOT $HOME and NOT the repo, so nothing the shipper/PR controls can redirect
# approval. The hatch MECHANICS (approve/deny/timeout/empty + the bypass:* audit + the
# HOME-is-ignored guarantee) are tested IN-PROCESS against the real helper with `resolve_home`
# monkeypatched to a fake home — this is the only way to exercise a controllable tg-ctl through a
# subprocess without touching the real one (mirrors how pin-primary-worktree tests the shared
# lib). The ship.sh<->helper WIRING (proceed on exit 0, refuse otherwise, fail-closed when the
# helper is unreachable) is tested via the subprocess with a fake helper, so no real tg-ctl is
# ever invoked.

_HATCH_MOD_DIR = str(Path(__file__).resolve().parents[1] / "ci" / "ship")


def _load_hatch_module():
    """Import ci/ship/review_quorum_hatch as a module (fresh each call so a monkeypatched
    resolve_home never leaks between tests)."""
    import importlib

    if _HATCH_MOD_DIR not in sys.path:
        sys.path.insert(0, _HATCH_MOD_DIR)
    mod = importlib.import_module("review_quorum_hatch")
    return importlib.reload(mod)


def _run_hatch_main(monkeypatch, tmp_path, *, request, resolve_home, audit,
                    timeout="5", margin=None, trusted=None, dry_run=None):
    """Call review_quorum_hatch.main() with resolve_home monkeypatched to a fixed dir and the
    hatch env set. Returns the exit code. `resolve_home` is the dir the helper will treat as the
    account's real home (where it looks for a rig.yaml tg_ctl_path override)."""
    mod = _load_hatch_module()
    monkeypatch.setattr(mod, "resolve_home", lambda: str(resolve_home))
    # The shared lib now resolves tg-ctl authority from ITS OWN resolve_home (not the cwd the ship
    # passes), so the controllable fake home must be injected there too — otherwise the lib reads
    # the real account home and would contact the real tg-ctl.
    monkeypatch.setattr(mod.hatch_escalation, "resolve_home", lambda: str(resolve_home))
    if trusted is not None:
        monkeypatch.setattr(mod.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", tuple(trusted))
    monkeypatch.setenv("RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM", request)
    monkeypatch.setenv("SHIP_AUDIT_FILE", str(audit))
    monkeypatch.setenv("SHIP_HATCH_PR", "1")
    monkeypatch.setenv("SHIP_HATCH_CODE", "HYP-200")
    monkeypatch.setenv("SHIP_HATCH_ITER", "1")
    monkeypatch.setenv("SHIP_HATCH_MODELS", "1")
    monkeypatch.setenv("SHIP_HATCH_TIMEOUT_S", timeout)
    if margin is not None:
        monkeypatch.setenv("SHIP_HATCH_PROCESS_MARGIN_S", margin)
    if dry_run is None:
        monkeypatch.delenv("SHIP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("SHIP_DRY_RUN", dry_run)
    return mod.main()


def test_hatch_empty_request_is_denied_without_tg_contact(tmp_path, monkeypatch):
    """A hatch request with an EMPTY value is invalid: the lib denies it without contacting
    tg-ctl, the helper exits 1 and audits bypass:denied."""
    import json

    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(monkeypatch, tmp_path, request="", resolve_home=home, audit=audit)
    assert rc == 1, "an empty hatch value must be denied"
    assert not marker.exists(), "tg-ctl must NOT be contacted for a blank hatch request"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "bypass:denied", rec


def test_hatch_reason_triggers_tg_ask_and_approval_returns_0(tmp_path, monkeypatch):
    """A real justification runs `tg-ctl ask`; on Alex's live approval the helper returns 0,
    the question carries the hook id + justification + PR context, and it audits bypass:approved."""
    import json

    question_file = tmp_path / "question.txt"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=f'printf "%s" "$2" > "{question_file}"\nprintf "approved by Alex\\n"\nexit 0\n',
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path,
        request="Security hotfix for prod outage; review dispatched, cannot wait for quorum.",
        resolve_home=home, audit=audit,
    )
    assert rc == 0, "a live-approved hatch must return 0"
    question = question_file.read_text()
    assert "ship-review-quorum" in question, question
    assert "Security hotfix for prod outage" in question, question
    assert "HYP-200" in question, question
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "bypass:approved", rec
    assert "approved by Alex" in rec.get("override_reason", ""), rec


def test_hatch_denial_returns_1_and_audits(tmp_path, monkeypatch):
    """When Alex declines (tg-ctl exits non-zero) the helper returns 1 and audits bypass:denied."""
    import json

    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body='printf "declined\\n"\nexit 1\n')
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path, request="Please let this through, I am in a hurry.",
        resolve_home=home, audit=audit,
    )
    assert rc == 1, "a declined hatch must return 1"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "bypass:denied", rec


def test_hatch_timeout_returns_1(tmp_path, monkeypatch):
    """When Alex does not answer in time (tg-ctl hangs past the timeout) the helper returns 1 —
    silence is not approval."""
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body="sleep 10\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path, request="No response expected — testing the timeout path.",
        resolve_home=home, audit=audit, timeout="1", margin="1",
    )
    assert rc == 1, "a timed-out hatch must return 1"


def test_hatch_dry_run_skips_tg_contact_and_audit(tmp_path, monkeypatch, capsys):
    """A ship --dry-run with a hatch request must not send a live tg-ctl ask or append the
    helper-owned bypass audit line."""
    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=f"touch {marker}\nprintf 'approved despite dry-run\\n'\nexit 0\n",
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path,
        request="Dry-run validation; must not ask Alex.",
        resolve_home=home,
        audit=audit,
        dry_run="1",
    )
    out = capsys.readouterr().out
    assert rc == 1, "dry-run must not approve a hatch because no live request was sent"
    assert "DENIED" in out and "dry-run" in out, out
    assert not marker.exists(), "dry-run hatch must NOT contact tg-ctl"
    assert not audit.exists(), f"dry-run hatch must NOT write audit file: {audit.read_text()!r}"


@pytest.mark.parametrize(
    ("hatch_request", "expected"),
    [
        ("", "is blank"),
        ("1", "needs a written justification"),
        ("true", "needs a written justification"),
    ],
)
def test_hatch_dry_run_still_validates_blank_and_bare_requests(
    tmp_path, monkeypatch, capsys, hatch_request, expected
):
    """Dry-run suppresses side effects, not local validation of invalid hatch requests."""
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body="printf 'should not be called\\n'\nexit 0\n",
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path,
        request=hatch_request,
        resolve_home=home,
        audit=audit,
        dry_run="1",
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert expected in out
    assert "would request Telegram hatch escalation" not in out
    assert not audit.exists()


def test_hatch_truthy_env_contract(monkeypatch):
    """The helper's env boolean parser is deliberately narrow and case-insensitive."""
    mod = _load_hatch_module()
    for value in ("1", "true", "TRUE", " yes ", "on", "On"):
        monkeypatch.setenv("SHIP_DRY_RUN", value)
        assert mod._truthy_env("SHIP_DRY_RUN")
    for value in ("", "0", "false", "no", "off", "disabled"):
        monkeypatch.setenv("SHIP_DRY_RUN", value)
        assert not mod._truthy_env("SHIP_DRY_RUN")
    monkeypatch.delenv("SHIP_DRY_RUN", raising=False)
    assert not mod._truthy_env("SHIP_DRY_RUN")


def test_hatch_helper_import_resists_pythonpath_hijack(tmp_path):
    """P0 regression (codex review): a shipping agent must not be able to swap the shared lib via
    PYTHONPATH. The helper loads agenttools_hatch_escalation by EXPLICIT FILE PATH (and ship.sh
    additionally runs it under `python3 -I`), so a same-named module planted on PYTHONPATH is NOT
    imported — even with PYTHONPATH active and no isolation flag, the REAL lib wins."""
    mal = tmp_path / "mal"
    mal.mkdir()
    (mal / "agenttools_hatch_escalation.py").write_text(
        "MALICIOUS = True\n"
        "def request_hatch_approval(*a, **k):\n"
        "    class R:\n        approved = True\n        env_present = True\n        reason = 'EVIL'\n"
        "    return R()\n",
        encoding="utf-8",
    )
    real_lib_dir = Path(__file__).resolve().parents[1] / "lib"
    real_init = real_lib_dir / "agenttools_hatch_escalation" / "__init__.py"
    hatch_dir = str(Path(__file__).resolve().parents[1] / "ci" / "ship")
    probe = (
        f"import sys; sys.path.insert(0, {hatch_dir!r}); "
        "import review_quorum_hatch as m; "
        "print(m.hatch_escalation.__file__); "
        "print('MAL' if getattr(m.hatch_escalation, 'MALICIOUS', False) else 'REAL')"
    )
    env = dict(os.environ)
    # The exact attack shape: PYTHONPATH lists the MALICIOUS dir BEFORE the real lib dir. A naive
    # "insert real lib only if not already in sys.path" would then skip the prepend (the real lib
    # is already present via PYTHONPATH) and import the malicious module first. The by-path load
    # must beat this. No -I here: prove the by-path import alone defeats it (belt); ship.sh runs
    # the helper under -I too (suspenders).
    env["PYTHONPATH"] = os.pathsep.join([str(mal), str(real_lib_dir)])
    r = _sh("python3", "-c", probe, cwd=tmp_path, env=env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert str(real_init) in r.stdout, f"helper loaded the wrong lib:\n{r.stdout}"
    assert r.stdout.strip().splitlines()[-1] == "REAL", r.stdout


def test_audit_line_written_without_jq(tmp_path):
    """P2 regression (codex review): the audit line must not be DROPPED when jq is absent —
    jq-missing is itself a hatchable gate refusal, so its fail-closed audit must still land via the
    printf fallback. Run ship with NO jq on PATH and assert a parseable JSON audit line is written.
    (The review-quorum gate refuses with 'jq not found', then audits.)"""
    import json

    if not shutil.which("jq"):
        pytest.skip("jq not installed on this host — the jq-less path is the ambient default")
    audit = tmp_path / "audit.jsonl"
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    # jq usually lives alongside the coreutils ship needs (e.g. /usr/bin), so we can't just drop a
    # dir. Build a symlink farm of every system executable EXCEPT jq, and use only that + fakes.
    farm = tmp_path / "nojq-farm"
    farm.mkdir()
    for sysdir in ("/usr/bin", "/bin"):
        d = Path(sysdir)
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            if entry.name == "jq":
                continue
            link = farm / entry.name
            if not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(gh), str(rv), str(farm)])
    assert shutil.which("jq", path=env["PATH"]) is None, "test PATH must not contain jq"
    assert shutil.which("bash", path=env["PATH"]) and shutil.which("git", path=env["PATH"]), \
        "test PATH must still carry bash + git"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"
    env["REVIEW_TASK_CODE"] = "HYP-200"
    env["SHIP_TEST_REVIEW_ITER"] = "1"
    env["SHIP_TEST_REVIEW_MODELS"] = "1"
    env["SHIP_AUDIT_FILE"] = str(audit)
    env.pop("RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM", None)
    # A PR selector containing a double-quote: the jq-less fallback must escape it (the raw path
    # would emit invalid JSON / a corrupt line). The fake gh accepts any PR value.
    pr_arg = '9"x'
    r = _sh("bash", str(_SHIP), pr_arg, "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"jq-missing must refuse\n{r.stdout}\n{r.stderr}"
    assert audit.exists(), "audit file must exist even without jq"
    lines = [ln for ln in audit.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"exactly one audit line (no injected line from the quoted PR): {lines}"
    rec = json.loads(lines[-1])  # must be valid JSON (printf fallback, pr escaped)
    assert rec["task_code"] == "HYP-200", rec
    assert rec["decision"] == "refused", rec
    assert rec["pr"] == pr_arg, rec  # round-trips through the escaping


@pytest.mark.real_os_home
def test_resolve_home_uses_os_identity_not_HOME_env(tmp_path, monkeypatch):
    """resolve_home() must key off the OS account identity (pwd.getpwuid), NOT the $HOME env var
    — that is the P0 fix: a shipper who exports a doctored HOME cannot move the location the hatch
    trusts for tg-ctl. Guards against a regression to `os.environ["HOME"]`."""
    import pwd

    mod = _load_hatch_module()
    monkeypatch.setattr(
        mod.hatch_escalation,
        "_find_tg_ctl",
        lambda *_args, **_kwargs: pytest.fail("resolve_home test must not resolve tg-ctl"),
    )
    real = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", str(tmp_path / "doctored-home"))
    assert mod.resolve_home() == real, "resolve_home must ignore $HOME and use the OS identity"


def test_hatch_ignores_HOME_env_and_pr_repo_rig_yaml(tmp_path, monkeypatch):
    """The account's REAL home (resolve_home) is the ONLY rig.yaml consulted for tg-ctl. A
    doctored $HOME pointing at an always-approve stub — the same shape a PR could commit — must
    be IGNORED, so with the real home carrying no override and no trusted tg-ctl, the helper does
    NOT approve. This is the P0/P1 self-authorization fix: neither $HOME nor the repo can redirect
    approval."""
    # $HOME (and, equivalently, a PR-committed repo rig.yaml) points at an APPROVE stub...
    approve = _write_fake_tg_ctl(tmp_path, name="approve", body='printf "approved\\n"\nexit 0\n')
    doctored_home = _fake_home_with_tg_ctl(tmp_path, approve, name="doctored-home")
    monkeypatch.setenv("HOME", str(doctored_home))
    # ...but the REAL account home (resolve_home) has NO rig.yaml, and no trusted tg-ctl resolves.
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path, request="Attempted bypass via a doctored HOME / repo rig.yaml.",
        resolve_home=real_home, audit=audit, trusted=(),
    )
    assert rc == 1, "a doctored HOME / repo rig.yaml must NOT be able to self-approve"


# --- ship.sh <-> helper wiring (subprocess; fake helper, no real tg-ctl) -------------------

def _install_fake_hatch_helper(dst_dir: Path, *, exit_code: int, message: str = "") -> None:
    """Write a fake review_quorum_hatch.py next to a ship.sh copy — a stand-in for the real helper
    so the ship.sh wiring can be tested without invoking any tg-ctl. It emits the same stdout
    VERDICT sentinel the real helper does (ship.sh authorizes only on 'APPROVED'): exit 0 ->
    'APPROVED <msg>', exit 1 -> 'DENIED <msg>', anything else -> just the message on stderr (no
    sentinel, simulating a crashed/aborted helper)."""
    body = "import sys\n"
    if exit_code == 0:
        body += f"sys.stdout.write('APPROVED ' + {message!r} + '\\n')\n"
    elif exit_code == 1:
        body += f"sys.stdout.write('DENIED ' + {message!r} + '\\n')\n"
    elif message:
        body += f"sys.stderr.write({message!r})\n"
    body += f"sys.exit({exit_code})\n"
    (dst_dir / "review_quorum_hatch.py").write_text(body, encoding="utf-8")


def _install_fake_skip_ci_helper(dst_dir: Path) -> None:
    """Write a fake APPROVING skip_ci_hatch.py next to a ship.sh copy so a --skip-ci run's skip-ci
    hatch gate proceeds transparently (emits the 'APPROVED' verdict sentinel ship.sh gates on).
    Lets the copy-ship.sh review-quorum wiring tests keep using --skip-ci without the skip-ci gate
    interfering — a stand-in for a live-approved skip-ci hatch, no tg-ctl involved."""
    body = (
        "import sys\n"
        "sys.stdout.write('APPROVED fake skip-ci approval (test)\\n')\n"
        "sys.exit(0)\n"
    )
    (dst_dir / "skip_ci_hatch.py").write_text(body, encoding="utf-8")


def _ship_copy_with_helper(tmp_path: Path, *, helper_exit, helper_msg=""):
    """A ship.sh copied into a temp ci/ship/ dir, optionally with a fake helper beside it. When
    `helper_exit` is None NO helper is written (simulating a bare `cp ship.sh` that can't reach
    the hatch). Returns the ship.sh copy path."""
    dst = tmp_path / "toolcopy" / "ci" / "ship"
    dst.mkdir(parents=True)
    ship_copy = dst / "ship.sh"
    ship_copy.write_text(_SHIP.read_text(encoding="utf-8"), encoding="utf-8")
    ship_copy.chmod(0o755)
    if helper_exit is not None:
        _install_fake_hatch_helper(dst, exit_code=helper_exit, message=helper_msg)
        # These tests exercise the REVIEW-QUORUM hatch wiring, but they also pass --skip-ci (a
        # shortcut to skip CI mocking), which is now its own hatch-gated admin bypass. Install a
        # fake APPROVING skip_ci_hatch.py beside the copy so the skip-ci gate is transparent and
        # never masks the quorum-hatch behaviour under test. (When helper_exit is None we install
        # neither, so a bare `cp ship.sh` still fails closed on BOTH gates.)
        _install_fake_skip_ci_helper(dst)
    return ship_copy


def _run_ship_copy_short_bar(tmp_path, main, gh, rv, ship_copy, *, request, audit=None,
                             extra_args=(), env_extra=None):
    """Run a ship.sh copy against a SHORT bar (1/1) with a hatch request set."""
    env = dict(os.environ)
    env["PATH"] = _minimal_hermetic_path(gh, rv)
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"
    env["REVIEW_TASK_CODE"] = "HYP-200"
    env["SHIP_TEST_REVIEW_ITER"] = "1"
    env["SHIP_TEST_REVIEW_MODELS"] = "1"
    env["RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM"] = request
    # --skip-ci is now hatch-gated too; request it so the fake approving skip_ci_hatch.py beside the
    # copy is consulted (deny-by-default otherwise refuses before the merge, masking the quorum path).
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "test skip-ci wiring"
    env.pop("SHIP_HATCH_TIMEOUT_S", None)
    if audit is not None:
        env["SHIP_AUDIT_FILE"] = str(audit)
    if env_extra:
        env.update(env_extra)
    return _sh("bash", str(ship_copy), "1", "--skip-ci", "--no-screenshot-ok", "test",
               *extra_args,
               cwd=main, env=env)


def test_ship_proceeds_when_helper_approves(tmp_path):
    """ship.sh treats a helper exit 0 as an approved bypass and proceeds to merge."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    ship_copy = _ship_copy_with_helper(tmp_path, helper_exit=0)
    r = _run_ship_copy_short_bar(tmp_path, main, gh, rv, ship_copy,
                                 request="Approved out-of-band by Alex.")
    assert r.returncode == 0, f"helper exit 0 must proceed\n{r.stdout}\n{r.stderr}"
    assert "APPROVED by Alex" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_ship_refuses_when_helper_denies(tmp_path):
    """ship.sh treats a helper exit 1 (requested-but-not-approved) as a refusal and does not
    merge; the helper owns the bypass:denied audit so ship.sh does not double-write it."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    ship_copy = _ship_copy_with_helper(tmp_path, helper_exit=1, helper_msg="declined by Alex")
    r = _run_ship_copy_short_bar(tmp_path, main, gh, rv, ship_copy,
                                 request="Declined bypass attempt.")
    assert r.returncode != 0, f"helper exit 1 must refuse\n{r.stdout}\n{r.stderr}"
    assert "NOT approved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_ship_dry_run_passes_marker_to_hatch_helper(tmp_path):
    """ship.sh must tell the helper when --dry-run is active so the helper can avoid its own
    tg-ctl and audit side effects."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    ship_copy = _ship_copy_with_helper(tmp_path, helper_exit=None)
    log = tmp_path / "helper-dry-run-env.txt"
    audit = tmp_path / "audit.jsonl"
    (ship_copy.parent / "review_quorum_hatch.py").write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['SHIP_TEST_HATCH_ENV_LOG']).write_text("
        "os.environ.get('SHIP_DRY_RUN', '<unset>'), encoding='utf-8')\n"
        "sys.stdout.write('DENIED dry-run wiring probe\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    r = _run_ship_copy_short_bar(
        tmp_path, main, gh, rv, ship_copy,
        request="Dry-run wiring probe.",
        audit=audit,
        extra_args=("--dry-run",),
        env_extra={"SHIP_TEST_HATCH_ENV_LOG": str(log)},
    )
    assert r.returncode != 0, f"probe helper denies; ship should refuse\n{r.stdout}\n{r.stderr}"
    assert log.read_text(encoding="utf-8") == "1"
    assert "would append review-quorum audit: decision=bypass:denied" in r.stderr
    assert not audit.exists(), "ship --dry-run must not write the real audit file"


def test_ship_non_dry_run_passes_falsey_marker_to_hatch_helper(tmp_path):
    """A normal ship must still reach the helper with a falsey dry-run marker."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    ship_copy = _ship_copy_with_helper(tmp_path, helper_exit=None)
    log = tmp_path / "helper-dry-run-env.txt"
    (ship_copy.parent / "review_quorum_hatch.py").write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['SHIP_TEST_HATCH_ENV_LOG']).write_text("
        "os.environ.get('SHIP_DRY_RUN', '<unset>'), encoding='utf-8')\n"
        "sys.stdout.write('APPROVED normal wiring probe\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    # --skip-ci is hatch-gated too; give this bare (helper_exit=None) copy an approving skip-ci
    # helper so the quorum-wiring probe reaches merge instead of failing closed at the skip-ci gate.
    _install_fake_skip_ci_helper(ship_copy.parent)
    r = _run_ship_copy_short_bar(
        tmp_path, main, gh, rv, ship_copy,
        request="Normal wiring probe.",
        env_extra={"SHIP_TEST_HATCH_ENV_LOG": str(log)},
    )
    assert r.returncode == 0, f"helper approves; ship should proceed\n{r.stdout}\n{r.stderr}"
    assert log.read_text(encoding="utf-8") == "0"


def test_ship_fails_closed_when_helper_unreachable(tmp_path):
    """A bare `cp ci/ship/ship.sh` copy with NO sibling helper (the detached-install shape the
    codex review flagged) fails CLOSED on a hatch request: python3 can't run the missing helper,
    ship.sh refuses and records a fail-closed bypass:denied audit."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    ship_copy = _ship_copy_with_helper(tmp_path, helper_exit=None)  # NO helper written
    r = _run_ship_copy_short_bar(tmp_path, main, gh, rv, ship_copy,
                                 request="genuine urgent reason", audit=audit)
    assert r.returncode != 0, f"a missing helper must fail closed\n{r.stdout}\n{r.stderr}"
    assert "NOT approved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout
    rec = json.loads(audit.read_text().strip().splitlines()[-1])
    assert rec["decision"] == "bypass:denied", rec


def test_ship_fails_closed_when_python3_exits_zero_without_sentinel(tmp_path):
    """P0 regression (codex round 4): the hatch must not FAIL OPEN on a `python3` that merely
    exits 0. A fake python3 first on PATH (exit 0, no output) must NOT be read as approval —
    ship.sh authorizes only on the explicit APPROVED stdout sentinel, so this fails CLOSED and
    records bypass:denied. (Removes the fail-open asymmetry vs the other gates, which fail closed
    on a tool malfunction.)"""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    # A fake python3 that exits 0 with NO sentinel (a broken/planted interpreter). Only the hatch
    # uses python3, so shadowing it doesn't disturb the rest of the gate.
    shim = tmp_path / "fakepy"
    shim.mkdir()
    (shim / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (shim / "python3").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(shim), _minimal_hermetic_path(gh, rv)])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"
    env["REVIEW_TASK_CODE"] = "HYP-200"
    env["SHIP_TEST_REVIEW_ITER"] = "1"
    env["SHIP_TEST_REVIEW_MODELS"] = "1"
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM"] = "genuine reason but python3 is faked"
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"a fake python3 exiting 0 must fail closed\n{r.stdout}\n{r.stderr}"
    assert "NOT approved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout
    rec = json.loads(audit.read_text().strip().splitlines()[-1])
    assert rec["decision"] == "bypass:denied", rec


# --- --skip-ci hatch escalation (deny-by-default; the ONLY bypass is a live Telegram approval) --
#
# `--skip-ci` is a blind admin-merge (skips the green-CI gate + branch protection). It is
# deny-by-default: without RIG_HATCH_REQUEST_SHIP_SKIP_CI ship refuses BEFORE any merge. With a
# written justification it routes through ci/ship/skip_ci_hatch.py -> the shared
# agenttools_hatch_escalation lib -> `tg-ctl ask` Alex live. As with the review-quorum hatch, the
# live approve/deny/dry-run MECHANICS are tested IN-PROCESS against the real helper with
# resolve_home monkeypatched to a fake home (the only way to exercise a controllable tg-ctl); the
# deny-by-default refusal (env unset) is tested end-to-end via a real ship.sh subprocess.


def _load_skip_ci_hatch_module():
    """Import ci/ship/skip_ci_hatch as a module (fresh each call so a monkeypatched resolve_home
    never leaks between tests)."""
    import importlib

    if _HATCH_MOD_DIR not in sys.path:
        sys.path.insert(0, _HATCH_MOD_DIR)
    mod = importlib.import_module("skip_ci_hatch")
    return importlib.reload(mod)


def _run_skip_ci_hatch_main(monkeypatch, *, request, resolve_home, audit,
                            timeout="5", dry_run=None):
    """Call skip_ci_hatch.main() with resolve_home monkeypatched to a fixed fake home (where the
    lib looks for a rig.yaml tg_ctl_path override) and the hatch env set. Returns the exit code."""
    mod = _load_skip_ci_hatch_module()
    monkeypatch.setattr(mod, "resolve_home", lambda: str(resolve_home))
    monkeypatch.setattr(mod.hatch_escalation, "resolve_home", lambda: str(resolve_home))
    monkeypatch.setenv("RIG_HATCH_REQUEST_SHIP_SKIP_CI", request)
    monkeypatch.setenv("SHIP_AUDIT_FILE", str(audit))
    monkeypatch.setenv("SHIP_HATCH_PR", "1")
    monkeypatch.setenv("SHIP_HATCH_BRANCH", "feat")
    monkeypatch.setenv("SHIP_HATCH_TIMEOUT_S", timeout)
    if dry_run is None:
        monkeypatch.delenv("SHIP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("SHIP_DRY_RUN", dry_run)
    return mod.main()


def test_skip_ci_hatch_blank_denied_without_tg_contact(tmp_path, monkeypatch):
    """A blank RIG_HATCH_REQUEST_SHIP_SKIP_CI is invalid: denied without contacting tg-ctl,
    exit 1, audits skipci:bypass:denied."""
    import json

    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(monkeypatch, request="", resolve_home=home, audit=audit)
    assert rc == 1, "a blank hatch value must be denied"
    assert not marker.exists(), "tg-ctl must NOT be contacted for a blank hatch request"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "skipci:bypass:denied", rec
    assert rec["gate"] == "skip-ci", rec


def test_skip_ci_hatch_bare_flag_denied_without_tg_contact(tmp_path, monkeypatch):
    """A bare truthy flag ('1'/'yes'/'true') is NOT a justification: denied without tg-ctl."""
    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(monkeypatch, request="1", resolve_home=home, audit=audit)
    assert rc == 1, "a bare '1' must be denied — a self-set flag is not an approval"
    assert not marker.exists(), "tg-ctl must NOT be contacted for a bare flag"


def test_skip_ci_hatch_reason_triggers_tg_ask_and_approval_returns_0(tmp_path, monkeypatch):
    """A real justification runs `tg-ctl ask`; on Alex's live approval the helper returns 0, the
    question carries the hook id + justification + PR context, and it audits skipci:bypass:approved."""
    import json

    question_file = tmp_path / "question.txt"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=f'printf "%s" "$2" > "{question_file}"\nprintf "approved by Alex\\n"\nexit 0\n',
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(
        monkeypatch,
        request="CI Actions billing suspended; local gates green; hotfix HYP-999 must ship now.",
        resolve_home=home, audit=audit,
    )
    assert rc == 0, "a live-approved --skip-ci hatch must return 0"
    question = question_file.read_text()
    assert "ship-skip-ci" in question, question
    assert "CI Actions billing suspended" in question, question
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "skipci:bypass:approved", rec
    assert "approved by Alex" in rec.get("override_reason", ""), rec


def test_skip_ci_hatch_denial_returns_1_and_audits(tmp_path, monkeypatch):
    """When Alex declines (tg-ctl exits non-zero) the helper returns 1 and audits skipci:bypass:denied."""
    import json

    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body='printf "declined\\n"\nexit 1\n')
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(
        monkeypatch, request="Please just let this admin-merge through, I am in a hurry.",
        resolve_home=home, audit=audit,
    )
    assert rc == 1, "a declined --skip-ci hatch must return 1"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "skipci:bypass:denied", rec


def test_skip_ci_hatch_dry_run_reason_approves_without_tg_contact(tmp_path, monkeypatch):
    """`--skip-ci --dry-run` is a preview that must NOT fire a live Telegram round-trip: a written
    justification yields an APPROVED preview sentinel (exit 0) while contacting NO tg-ctl."""
    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(
        monkeypatch, request="preview: would ship the billing-blocked hotfix",
        resolve_home=home, audit=audit, dry_run="1",
    )
    assert rc == 0, "a dry-run preview with a real justification must return 0"
    assert not marker.exists(), "dry-run must NOT contact tg-ctl (no live round-trip in a preview)"
    assert not audit.exists() or audit.read_text().strip() == "", "dry-run must not write a real audit line"


def test_skip_ci_hatch_dry_run_blank_still_denied(tmp_path, monkeypatch):
    """Deny-by-default holds even in dry-run: a blank/bare justification is DENIED in preview too."""
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body="exit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(
        monkeypatch, request="", resolve_home=home, audit=audit, dry_run="1",
    )
    assert rc == 1, "a blank justification must be denied even in dry-run"


def test_skip_ci_deny_by_default_refuses_before_merge(repo_with_two_worktrees, tmp_path):
    """End-to-end: `--skip-ci` with NO RIG_HATCH_REQUEST_SHIP_SKIP_CI refuses (deny-by-default)
    AFTER the cheap preflights and BEFORE any merge, pointing the operator at the hatch env var."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    # Keep the audit write hermetic — never touch the developer's real ~/.config/agent-tools/.
    env["SHIP_AUDIT_FILE"] = str(audit)
    # Never let an ambient hatch request leak in and flip the refusal into a live tg-ctl call.
    env.pop("RIG_HATCH_REQUEST_SHIP_SKIP_CI", None)
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"deny-by-default: --skip-ci must refuse without a hatch\n{r.stdout}\n{r.stderr}"
    assert "RIG_HATCH_REQUEST_SHIP_SKIP_CI" in r.stderr, r.stderr
    assert "deny-by-default" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"
    import json
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "skipci:refused", rec
    assert rec["gate"] == "skip-ci", rec


def test_skip_ci_set_but_blank_refuses_before_merge_e2e(repo_with_two_worktrees, tmp_path):
    """End-to-end: `--skip-ci` with RIG_HATCH_REQUEST_SHIP_SKIP_CI SET BUT BLANK routes through the
    helper, which denies a blank justification WITHOUT contacting tg-ctl, and ship refuses before
    merge. (The env-unset case is covered separately; this covers the set-but-invalid branch.)"""
    import json

    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = ""  # set but blank -> helper denies, no tg-ctl
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"a blank justification must refuse\n{r.stdout}\n{r.stderr}"
    assert "NOT approved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"
    # Exactly ONE audit line: the Python helper owns the real-run denied write and the shell must
    # NOT double-write it (a future verdict-string drift would fall into the shell `*)` case and
    # duplicate — assert the single-write invariant, don't mask it with splitlines()[-1]).
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one audit line, got {lines}"
    rec = json.loads(lines[0])
    assert rec["decision"] == "skipci:bypass:denied", rec


def test_skip_ci_fails_closed_when_python3_exits_zero_without_sentinel(repo_with_two_worktrees, tmp_path):
    """Security fail-closed: a fake/broken `python3` that exits 0 but prints NO `APPROVED` sentinel
    must NOT be mistaken for approval — ship refuses and audits skipci:bypass:denied. Mirrors the
    review-quorum gate's fail-closed guard for the skip-ci gate."""
    import json

    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    # A fake python3 (found first on PATH) that exits 0 without emitting the APPROVED sentinel.
    fakepy = tmp_path / "fakepy"
    fakepy.mkdir()
    (fakepy / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fakepy / "python3").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(fakepy), str(bindir), env["PATH"]])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "genuine reason but python3 is faked"
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"a fake python3 exiting 0 must fail closed\n{r.stdout}\n{r.stderr}"
    assert "NOT approved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout
    # Exactly ONE audit line here too: the fake python3 writes nothing, so the shell fail-closed
    # `*)` branch is the sole writer — a duplicate would signal a control-flow regression.
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one audit line, got {lines}"
    rec = json.loads(lines[0])
    assert rec["decision"] == "skipci:bypass:denied", rec


def test_skip_ci_approved_hatch_reaches_admin_merge_e2e(repo_with_two_worktrees, tmp_path):
    """End-to-end happy path: with an APPROVED skip-ci hatch, a real ship.sh proceeds to the admin
    merge. The hatch helper is stood in by a fake `python3` that emits the APPROVED sentinel (a
    subprocess can't inject a fake tg-ctl — authority is the real home — so this is the e2e seam;
    the real-helper approve path + its audit line are covered in-process)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    # A fake python3 (first on PATH) that emits the APPROVED verdict sentinel ship.sh gates on.
    fakepy = tmp_path / "fakepy"
    fakepy.mkdir()
    (fakepy / "python3").write_text("#!/bin/sh\nprintf 'APPROVED live-approved (test)\\n'\nexit 0\n", encoding="utf-8")
    (fakepy / "python3").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(fakepy), str(bindir), env["PATH"]])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(tmp_path / "audit.jsonl")
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "CI billing suspended; hotfix must ship"
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode == 0, f"an approved skip-ci hatch must reach the admin merge\n{r.stdout}\n{r.stderr}"
    assert "APPROVED by Alex" in r.stdout, r.stdout
    assert "admin-merging" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout, r.stdout


def test_skip_ci_dry_run_with_justification_does_not_claim_live_approval(repo_with_two_worktrees, tmp_path):
    """`--skip-ci --dry-run` with a real justification previews WITHOUT firing a live Telegram
    round-trip and WITHOUT falsely claiming Alex approved (the P1 the review flagged). The real
    skip_ci_hatch.py dry-run path returns an APPROVED-preview sentinel but contacts NO tg-ctl, and
    ship.sh prints a dry-run-specific message, not 'APPROVED by Alex'. Writes no real audit line."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "billing outage preview; hotfix HYP-999"
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", "--dry-run",
            cwd=main, env=env)
    assert r.returncode == 0, f"dry-run preview with a justification should proceed\n{r.stdout}\n{r.stderr}"
    assert "APPROVED by Alex" not in r.stdout, "dry-run must NOT claim a live approval happened"
    assert "REAL run would request live Telegram approval" in r.stdout, r.stdout
    assert not audit.exists() or audit.read_text().strip() == "", "dry-run must write no real audit line"


def test_skip_ci_dry_run_with_blank_justification_still_refuses(repo_with_two_worktrees, tmp_path):
    """Deny-by-default holds in dry-run too: `--skip-ci --dry-run` with a blank justification is
    refused before the (no-op) merge — a preview cannot manufacture an approval from nothing."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(tmp_path / "audit.jsonl")
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = ""
    r = _sh("bash", str(_SHIP), "1", "--skip-ci", "--no-screenshot-ok", "test", "--dry-run",
            cwd=main, env=env)
    assert r.returncode != 0, f"blank justification must refuse even in dry-run\n{r.stdout}\n{r.stderr}"
    assert "APPROVED by Alex" not in r.stdout
    assert "[fake gh] merged" not in r.stdout


# --- #268: auto-resolve addressed bot-nit review threads --------------------------------

# A fake `gh` that also answers the auto-resolve path: the resolve-eligible query (routed by the
# `isOutdated` field it selects) returns the thread ids the test declares; the resolveReviewThread
# mutation logs each resolved id; and the unresolved-COUNT query returns the initial count minus the
# number resolved so far — simulating GitHub reflecting the resolutions before the gate re-reads.
_FAKE_GH_RESOLVE = """\
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
          # One GREEN check so the normal (non-admin) CI gate passes — --skip-ci is now hatch-gated.
          printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
        else echo '[]'; fi ;;
      diff) echo "src/a.py" ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    argstr="$(printf '%s ' "$@")"
    if printf '%s' "$argstr" | grep -q resolveReviewThread; then
      [ -n "${SHIP_TEST_RESOLVE_FAIL:-}" ] && exit 1   # simulate the mutation failing
      for a in "$@"; do case "$a" in id=*) printf '%s\\n' "${a#id=}" >> "${SHIP_TEST_RESOLVE_LOG}";; esac; done
      echo '{}'
    elif printf '%s' "$argstr" | grep -q isOutdated; then
      jq_filter=""
      while [ "$#" -gt 0 ]; do
        if [ "$1" = "--jq" ]; then jq_filter="$2"; break; fi
        shift
      done
      thread_bodies=${SHIP_TEST_THREAD_BODIES:-'{}'}
      jq -n --arg ids "${SHIP_TEST_ELIGIBLE_IDS:-}" --argjson bodies "$thread_bodies" '
          ($ids | split(" ") | map(select(length > 0))) as $ids
          | {data: {repository: {pullRequest: {reviewThreads: {nodes: [
              $ids[] as $id
              | {id: $id, isResolved: false, isOutdated: true,
                 comments: {totalCount: 1, nodes: [{
                   author: {login: "some-app[bot]"}, body: ($bodies[$id] // "plain nit")
                 }]}}
            ]}}}}}' | jq -r "$jq_filter"
    elif printf '%s' "$argstr" | grep -q committedDate; then
      printf '%s\\t%s\\t%s\\n' "${SHIP_TEST_CREATED:-2020-01-01T00:00:00Z}" "" "${SHIP_TEST_LASTCOMMIT:-2020-01-01T00:00:00Z}"
    else
      resolved=0
      [ -f "${SHIP_TEST_RESOLVE_LOG:-/nonexistent}" ] && resolved=$(wc -l < "${SHIP_TEST_RESOLVE_LOG}" | tr -d ' ')
      echo $(( ${SHIP_TEST_UNRESOLVED:-0} - resolved ))
    fi ;;
  *) : ;;
esac
"""


def _fake_gh_resolve_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_RESOLVE, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _run_ship_resolve(main, bindir, *, extra_args=(), env_extra=None, cwd=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    if env_extra:
        env.update(env_extra)
    return _sh(
        # No --skip-ci (now a hatch-gated admin bypass): the fake gh returns a GREEN
        # statusCheckRollup so the normal CI gate passes and ship does a normal merge.
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
        cwd=cwd or main, env=env,
    )


def test_ship_resolves_addressed_bot_thread_then_merges(repo_with_two_worktrees, tmp_path):
    """With --resolve-addressed-threads, ship resolves the eligible bot-nit thread itself and then
    the unresolved-threads gate (which now reads 0) passes → the PR merges. This is the #268 fix:
    an agent no longer has to hand-run the resolveReviewThread mutation to close its own bot nit."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_resolve_dir(tmp_path)
    log = tmp_path / "resolve.log"
    r = _run_ship_resolve(
        main, bindir, extra_args=("--resolve-addressed-threads",),
        env_extra={"SHIP_TEST_UNRESOLVED": "1", "SHIP_TEST_ELIGIBLE_IDS": "PRRT_bot1",
                   "SHIP_TEST_RESOLVE_LOG": str(log)},
    )
    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    assert log.exists() and "PRRT_bot1" in log.read_text(encoding="utf-8"), "mutation not called"
    assert "resolved addressed bot thread PRRT_bot1" in r.stdout, r.stdout


def test_ship_keeps_high_severity_bot_thread_unresolved_and_blocks_merge(
        repo_with_two_worktrees, tmp_path):
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_resolve_dir(tmp_path)
    log = tmp_path / "resolve.log"
    bodies = json.dumps({"PRRT_nit": "Nit: clearer name.", "PRRT_p1": "This is a **P1**: defect."})
    r = _run_ship_resolve(
        main, bindir, extra_args=("--resolve-addressed-threads",),
        env_extra={"SHIP_TEST_UNRESOLVED": "2", "SHIP_TEST_ELIGIBLE_IDS": "PRRT_nit PRRT_p1",
                   "SHIP_TEST_THREAD_BODIES": bodies, "SHIP_TEST_RESOLVE_LOG": str(log)},
    )
    assert r.returncode != 0, f"the unresolved P1 must block ship\n{r.stdout}\n{r.stderr}"
    assert log.read_text(encoding="utf-8").splitlines() == ["PRRT_nit"]
    assert "unresolved review thread" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


# A CI-DOWN fake gh that ALSO answers the auto-resolve path. `_local_review_threads_check` (in the
# CI-down local fallback) reads the reviewThreads count via `gh pr view --json reviewThreads`; that
# count is `initial - resolved`, so once auto-resolve closes the eligible bot thread the local gate
# reads 0 and passes. Proves auto-resolve now runs BEFORE the CI-down thread check (codex round 4).
_FAKE_GH_CIDOWN_RESOLVE = """\
#!/usr/bin/env bash
set -e
resolved_count() { [ -f "${SHIP_TEST_RESOLVE_LOG:-/nonexistent}" ] && wc -l < "${SHIP_TEST_RESOLVE_LOG}" | tr -d ' ' || echo 0; }
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '[{"name":"pytest","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"codeql","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"}]'
        elif printf '%s ' "$@" | grep -q reviewThreads; then
          echo $(( ${SHIP_TEST_UNRESOLVED:-0} - $(resolved_count) ))
        elif printf '%s ' "$@" | grep -q body; then
          echo ""
        else
          echo '[]'
        fi ;;
      diff)
        if printf '%s ' "$@" | grep -q -- --name-only; then printf 'src/a.py'; else printf '+ok'; fi ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    argstr="$(printf '%s ' "$@")"
    if printf '%s' "$argstr" | grep -q resolveReviewThread; then
      for a in "$@"; do case "$a" in id=*) printf '%s\\n' "${a#id=}" >> "${SHIP_TEST_RESOLVE_LOG}";; esac; done
      echo '{}'
    elif printf '%s' "$argstr" | grep -q isOutdated; then
      [ -n "${SHIP_TEST_ELIGIBLE_IDS:-}" ] && printf '%s\\n' ${SHIP_TEST_ELIGIBLE_IDS}
      :
    elif printf '%s' "$argstr" | grep -q reviewThreads; then
      # unresolved-count query (the local CI-down check AND the main gate, both graphql now):
      # initial minus resolved, so once auto-resolve closes the bot thread the count reads 0.
      resolved=0
      [ -f "${SHIP_TEST_RESOLVE_LOG:-/nonexistent}" ] && resolved=$(wc -l < "${SHIP_TEST_RESOLVE_LOG}" | tr -d ' ')
      echo $(( ${SHIP_TEST_UNRESOLVED:-0} - resolved ))
    else
      echo 0
    fi ;;
  *) : ;;
esac
"""


def test_ship_resolve_applies_on_ci_down_fallback_path(repo_with_pr_worktree, tmp_path):
    """The CI-down local fallback runs its OWN review-threads check; auto-resolve must run before it
    too (codex round 4). With CI structurally down and --resolve-addressed-threads, ship resolves the
    eligible bot thread first, the local review-threads check then reads 0, and ship merges."""
    main, _wt = repo_with_pr_worktree
    bindir = tmp_path / "bincdr"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_CIDOWN_RESOLVE, encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "resolve.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env.update({
        "SHIP_TEST_CI_DOWN": "1", "SHIP_LOCAL_TEST_CMD": "true", "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_UNRESOLVED": "1", "SHIP_TEST_ELIGIBLE_IDS": "PRRT_bot1",
        "SHIP_TEST_RESOLVE_LOG": str(log),
    })
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", "--resolve-addressed-threads",
            cwd=main, env=env)
    assert r.returncode == 0, f"ship must merge on CI-down after resolve\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "resolved addressed bot thread PRRT_bot1" in r.stdout, r.stdout
    assert "CI infrastructure appears structurally unavailable" in r.stderr, r.stderr
    assert "merged #1" in r.stdout, r.stdout
    assert log.exists() and "PRRT_bot1" in log.read_text(encoding="utf-8")


def test_ship_resolve_flag_dry_run_does_not_mutate(repo_with_two_worktrees, tmp_path):
    """--dry-run with --resolve-addressed-threads REPORTS what it would resolve but issues NO
    resolveReviewThread mutation (the log stays empty)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_resolve_dir(tmp_path)
    log = tmp_path / "resolve.log"
    r = _run_ship_resolve(
        main, bindir, extra_args=("--resolve-addressed-threads", "--dry-run"),
        env_extra={"SHIP_TEST_UNRESOLVED": "1", "SHIP_TEST_ELIGIBLE_IDS": "PRRT_bot1",
                   "SHIP_TEST_RESOLVE_LOG": str(log)},
    )
    assert "would resolve addressed bot thread PRRT_bot1" in r.stdout, r.stdout
    assert not log.exists(), "dry-run must not issue the resolve mutation"


def test_ship_without_resolve_flag_still_blocks_unresolved(repo_with_two_worktrees, tmp_path):
    """Without the flag, ship never auto-resolves — an unresolved thread still BLOCKS the merge
    (opt-in preserved; the gate is unchanged for the default path)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_resolve_dir(tmp_path)
    log = tmp_path / "resolve.log"
    r = _run_ship_resolve(
        main, bindir,
        env_extra={"SHIP_TEST_UNRESOLVED": "1", "SHIP_TEST_ELIGIBLE_IDS": "PRRT_bot1",
                   "SHIP_TEST_RESOLVE_LOG": str(log)},
    )
    assert r.returncode != 0, f"expected refusal\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "unresolved review thread" in r.stderr, r.stderr
    assert not log.exists(), "resolve must not run without the flag"


def test_ship_resolve_mutation_failure_leaves_thread_for_gate(repo_with_two_worktrees, tmp_path):
    """If the resolveReviewThread mutation FAILS, ship must not swallow it: it reports the failure
    and the unchanged unresolved count still BLOCKS the merge (fail-safe, not fail-open)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_resolve_dir(tmp_path)
    log = tmp_path / "resolve.log"
    r = _run_ship_resolve(
        main, bindir, extra_args=("--resolve-addressed-threads",),
        env_extra={"SHIP_TEST_UNRESOLVED": "1", "SHIP_TEST_ELIGIBLE_IDS": "PRRT_bot1",
                   "SHIP_TEST_RESOLVE_LOG": str(log), "SHIP_TEST_RESOLVE_FAIL": "1"},
    )
    assert r.returncode != 0, f"a failed resolve must not let the merge through\n{r.stdout}\n{r.stderr}"
    assert "FAILED to resolve PRRT_bot1" in r.stderr, r.stderr
    assert "unresolved review thread" in r.stderr, r.stderr
    assert not log.exists(), "the failed mutation must not have logged a resolution"


def _extract_resolve_eligible_jq() -> str:
    text = _SHIP.read_text(encoding="utf-8")
    m = re.search(r"RESOLVE_ELIGIBLE_JQ='(.*?)'", text, re.DOTALL)
    assert m, "RESOLVE_ELIGIBLE_JQ not found in ship.sh"
    return m.group(1)


def _extract_resolve_thread_query() -> str:
    text = _SHIP.read_text(encoding="utf-8")
    m = re.search(r"RESOLVE_THREAD_Q='(.*?)'", text, re.DOTALL)
    assert m, "RESOLVE_THREAD_Q not found in ship.sh"
    return m.group(1)


def test_resolve_thread_query_fetches_comment_bodies_for_severity_filter():
    assert "nodes{author{login} body}" in _extract_resolve_thread_query()


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required for the eligibility-jq test")
def test_resolve_eligible_jq_excludes_high_severity_bot_threads():
    """Any high-severity bot comment excludes its outdated thread; ordinary nits remain eligible."""
    prog = _extract_resolve_eligible_jq()

    def thread(tid, *bodies):
        nodes = [{"author": {"login": "some-app[bot]"}, "body": body} for body in bodies]
        return {"id": tid, "isResolved": False, "isOutdated": True,
                "comments": {"totalCount": len(nodes), "nodes": nodes}}

    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        thread("NIT", "Nit: consider a clearer name."),
        thread("P1", "This is a **P1**: authentication can be bypassed."),
        thread("SECURITY", "Potential [security] regression in token validation."),
        thread("P1_THEN_ACK", "P1: data loss is possible.", "Acknowledged."),
        thread("NEGATED_P1", "Not a P1, just a nit."),
        thread("NULL_BODY", None),
        thread("EMPTY_MARKDOWN_BODY", "  **`# []`**  "),
        thread("LONGER_ALNUM_WORD", "The p1ateau example is harmless."),
        thread("HYPHENATED_COMPOUND_OVEREXCLUSION", "Use a security-first-initiative checklist."),
        thread("P1_HYPHEN_SUFFIX", "P1-blocking issue found here"),
    ]}}}}}
    r = subprocess.run(["jq", "-r", prog], input=json.dumps(payload), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    selected = [line for line in r.stdout.splitlines() if line.strip()]
    assert selected == ["NIT", "LONGER_ALNUM_WORD"], selected


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required for the eligibility-jq test")
def test_resolve_eligible_jq_selects_only_addressed_bot_threads():
    """Exercise the REAL eligibility jq (extracted from ship.sh) against crafted thread data: only an
    unresolved + outdated + commented + all-bot thread is selected. A human comment (even mixed with
    a bot), a still-current (not outdated) thread, an already-resolved thread, and a comment-less
    thread are all excluded — so ship never silently closes an unaddressed or human review thread."""
    prog = _extract_resolve_eligible_jq()

    def thread(tid, *, resolved, outdated, logins, total=None):
        nodes = [{"author": None if lg is None else {"login": lg}, "body": "plain nit"}
                 for lg in logins]
        return {"id": tid, "isResolved": resolved, "isOutdated": outdated,
                "comments": {"totalCount": len(nodes) if total is None else total, "nodes": nodes}}

    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        thread("BOT_OUTDATED", resolved=False, outdated=True,
               logins=["chatgpt-codex-connector", "some-app[bot]"]),
        thread("BOT_CURRENT", resolved=False, outdated=False, logins=["some-app[bot]"]),
        thread("HUMAN_OUTDATED", resolved=False, outdated=True, logins=["alex"]),
        thread("MIXED_OUTDATED", resolved=False, outdated=True, logins=["some-app[bot]", "alex"]),
        thread("BOT_RESOLVED", resolved=True, outdated=True, logins=["some-app[bot]"]),
        thread("BOT_NOCOMMENTS", resolved=False, outdated=True, logins=[]),
        # >100 comments: only the first page fetched, a human could hide beyond it → fail closed.
        thread("BOT_TRUNCATED", resolved=False, outdated=True, logins=["some-app[bot]"], total=150),
        # a deleted/ghost author (login null) must not crash jq and must make the thread ineligible.
        thread("BOT_NULLAUTHOR", resolved=False, outdated=True, logins=[None]),
        # a null totalCount (can't prove no human hides beyond the page) → fail closed.
        {"id": "BOT_NULLTOTAL", "isResolved": False, "isOutdated": True,
         "comments": {"totalCount": None,
                      "nodes": [{"author": {"login": "some-app[bot]"}, "body": "plain nit"}]}},
    ]}}}}}
    r = subprocess.run(["jq", "-r", prog], input=json.dumps(payload), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    selected = [line for line in r.stdout.splitlines() if line.strip()]
    assert selected == ["BOT_OUTDATED"], selected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
