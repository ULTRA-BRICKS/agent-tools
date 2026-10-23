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
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"
_SHIP_DIR = _SHIP.parent
sys.path.insert(0, str(_SHIP_DIR))

from shiplib import ci_gate, quorum_skipci, version_shot  # noqa: E402
from shiplib.ctx import Ctx, Result  # noqa: E402

# The review-quorum gate (Guard-B) defaults to ENABLED in the product. SHIP_REVIEW_QUORUM=0
# is forbidden (agent-tools#644) and no longer disables it. Unrelated tests in this file
# isolate via the test-only SHIP_TEST_DISABLE_REVIEW_QUORUM=1 (mirrors SHIP_TEST_CI_DOWN);
# quorum-gate tests pop that env and set SHIP_REVIEW_QUORUM=1.
os.environ.setdefault("SHIP_TEST_DISABLE_REVIEW_QUORUM", "1")
# The two pre-merge ticket gates (acceptance + magic-close, agent-tools#521) default ENABLED
# too and have their own fixtures in tests/test_ship_ticket_gates.py; off here so a fixture
# PR body that happens to say "Fixes HYP-999", or a fake `task` that logs every call, is not
# gated by a feature this file never set out to exercise.
os.environ.setdefault("SHIP_ACCEPTANCE_GATE", "0")
os.environ.setdefault("SHIP_MAGIC_CLOSE_GATE", "0")

# The post-merge task-cli notify step (`_ship_notify_task_cli`) also defaults to ENABLED, but
# none of the ~30 fixtures in this file stub a fake `task` binary — leaving it on here would
# invoke whatever REAL task-cli happens to be on the developer's PATH, against a fake merged
# PR carrying garbage `--pr`/`--commit` values (an unintended mutation of a real ticket store
# during a test run). Unconditional assignment, NOT `setdefault` (review finding: `setdefault`
# yields to an ambient `SHIP_TASK_NOTIFY_ENABLED=1` a developer's shell might already export,
# silently reproducing the exact hazard this line exists to prevent — unlike the quorum gate
# below, a missed override here mutates a real ticket store, not just a loud gate refusal, so
# it gets the stronger form). tests/test_ship_notify_task_cli.py exercises the feature itself
# in isolation, with its own fake `task` binary, and overrides this back to "1" per-call.
os.environ["SHIP_TASK_NOTIFY_ENABLED"] = "0"

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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # SHIP_TEST_REVIEW_QUERY_FAIL exercises the gh-failure path (expired auth / rate limit /
          # network blip): exit non-zero with nothing on stdout, exactly like a real `gh` failure.
          if [ -n "${SHIP_TEST_REVIEW_QUERY_FAIL:-}" ]; then
            echo "fake gh: simulated reviews-query failure" >&2
            exit 1
          fi
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\t%s\\tCLEAN\\n' "${SHIP_TEST_BRANCH}" "${SHIP_TEST_CROSS_REPO:-false}"
        elif printf '%s ' "$@" | grep -q -- '--json baseRefName'; then
          # SHIP_TEST_BASEREF_QUERY_FAIL exercises the gh-failure path for the external-review
          # gate's own base-branch lookup specifically (independent of SHIP_TEST_REVIEW_QUERY_FAIL
          # above, a DIFFERENT query) -- a dedicated test asserts this fails CLOSED (gate stays
          # enabled), never falls back to guessing $SHIP_DEFAULT_BRANCH.
          if [ -n "${SHIP_TEST_BASEREF_QUERY_FAIL:-}" ]; then
            echo "fake gh: simulated baseRefName-query failure" >&2
            exit 1
          fi
          printf '%s\\n' "${SHIP_TEST_BASE_BRANCH:-main}"
        elif printf '%s ' "$@" | grep -q -- '--json state,headRefOid'; then
          # Post-merge readback (cleanup confirms MERGED + the merged head before it removes the
          # PR checkout). SHIP_TEST_POST_STATE overrides the state; the head is the pushed tip of
          # the PR branch (refs/remotes/origin/<branch>), which every checkout of the repo shares.
          [ -z "${SHIP_TEST_READBACK_FAIL:-}" ] || { echo "fake gh: simulated read-back failure" >&2; exit 1; }
          printf '%s\\t%s\\n' "${SHIP_TEST_POST_STATE:-MERGED}" "${SHIP_TEST_MERGED_OID:-$(git rev-parse "refs/remotes/origin/${SHIP_TEST_BRANCH}" 2>/dev/null || true)}"
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
        # Optionally run a command at merge time (e.g. land a commit on the base branch the way a
        # real squash-merge does, or add a local commit after the clean preflight).
        [ -n "${SHIP_TEST_MERGE_CMD:-}" ] && eval "${SHIP_TEST_MERGE_CMD}"
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


def _screenshot_upload_env(main: Path, bindir: Path, uploader: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_IMAGE_UPLOAD_CMD"] = str(uploader)
    return env


def test_upload_png_quote_in_path_is_not_shell_injected(repo_with_pr_worktree, tmp_path):
    """HYP-1260 regression: upload_png() must never let a screenshot PATH re-enter `eval`.

    The pre-fix implementation did `eval "$SHIP_IMAGE_UPLOAD_CMD \\"$png\\""` — a literal `"`
    in the path could close that quote and splice arbitrary shell syntax into the eval'd
    string. Proven exploitable via PoC before the fix (a path containing
    `evil"; touch PWNED; echo "` ran `touch`). Here a screenshot path containing a double
    quote AND a `;`-separated shell command must reach the uploader as ONE inert argument —
    no command runs, and the argument the uploader receives is byte-for-byte the raw path."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    injection_marker = tmp_path / "PWNED"
    # A single path COMPONENT can't itself contain '/', so the injected payload reaches the
    # absolute marker path via an inherited env var expansion (evaluated only if the eval-
    # injection actually fires) rather than embedding a literal slash-bearing path in the
    # directory name.
    evil_dir = tmp_path / 'evil"; touch "$PWNED_TARGET"; echo "'
    evil_dir.mkdir()
    png = evil_dir / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["PWNED_TARGET"] = str(injection_marker)
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert not injection_marker.exists(), (
        "shell injection via the screenshot path executed `touch`!\n" + r.stdout + r.stderr
    )
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == [str(png)], f"uploader must receive the raw path as ONE argument, got: {got}"
    assert f"uploaded 'desc' -> https://example.invalid/uploaded.png" in r.stdout, r.stdout


@pytest.mark.parametrize("shot", ["C:/nowhere/ui.png", "C:\\nowhere\\ui.png", "\\\\srv\\share\\ui.png"])
def test_screenshot_windows_absolute_path_is_not_rebased_on_cwd(repo_with_pr_worktree, tmp_path, shot):
    """A Windows drive or UNC screenshot path is absolute on Git Bash for Windows: ship keeps it
    as given (the same rule as --task-config), never `$PWD/C:/…`."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text("#!/usr/bin/env bash\necho https://example.invalid/u.png\n", encoding="utf-8")
    uploader.chmod(0o755)
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", "--screenshot", shot, "desc",
        cwd=wt, env=_screenshot_upload_env(main, bindir, uploader),
    )
    assert f"screenshot not found: {shot} (" in r.stderr, r.stdout + r.stderr


def test_upload_png_file_token_quote_in_path_is_not_shell_injected(repo_with_pr_worktree, tmp_path):
    """Same HYP-1260 regression as above, but for the `{FILE}` token branch of upload_png()
    (`SHIP_IMAGE_UPLOAD_CMD` containing a literal `{FILE}` placeholder rather than appending
    the path as $1) — the pre-fix code built this branch via
    `eval "${SHIP_IMAGE_UPLOAD_CMD//\\{FILE\\}/$png}"`, equally vulnerable to a quote in the
    path breaking out of the template."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    injection_marker = tmp_path / "PWNED_FILE_TOKEN"
    evil_dir = tmp_path / 'evil"; touch "$PWNED_TARGET"; echo "'
    evil_dir.mkdir()
    png = evil_dir / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-file-token.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["PWNED_TARGET"] = str(injection_marker)
    # {FILE} branch: the config template embeds the placeholder plus an extra fixed arg,
    # proving both the substituted element AND the surrounding trusted words survive intact.
    env["SHIP_IMAGE_UPLOAD_CMD"] = f"{uploader} --file {{FILE}} --tag ci"
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert not injection_marker.exists(), (
        "shell injection via the screenshot path executed `touch`!\n" + r.stdout + r.stderr
    )
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == ["--file", str(png), "--tag", "ci"], (
        f"uploader must receive the raw path as ONE argument in place of {{FILE}}, got: {got}"
    )
    assert "uploaded 'desc' -> https://example.invalid/uploaded-file-token.png" in r.stdout, r.stdout


def test_upload_png_path_with_ampersand_and_backslash_is_not_corrupted(repo_with_pr_worktree, tmp_path):
    """Regression pinned from multi-model review of the HYP-1260 fix: an earlier draft spliced
    the path into a bash argv array via `${word//\\{FILE\\}/$png}`, an UNQUOTED `${var//pat/rep}`
    replacement. Under bash's `patsub_replacement` option (default-on since bash 5.2), a bare
    `&` in the replacement text is re-interpreted as "insert the matched pattern" and `\\` as an
    escape character — so a path containing `&` or `\\` would come out mangled even though no
    injection occurred. The shipped fix (`printf %q` + plain-string splice, no `${var//pat/rep}`
    anywhere) must reproduce the path byte-for-byte regardless of `&`/`\\` in it."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    tricky_dir = tmp_path / "a&b\\c"
    tricky_dir.mkdir()
    png = tricky_dir / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-ampersand.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == [str(png)], f"path with '&'/'\\\\' must survive byte-for-byte, got: {got}"


def test_upload_png_preserves_shell_pipeline_template(repo_with_pr_worktree, tmp_path):
    """Regression pinned from multi-model review of the HYP-1260 fix: an earlier draft parsed
    SHIP_IMAGE_UPLOAD_CMD into a plain argv array and ran it directly (no eval at all), which
    silently broke any operator-configured template using shell syntax — a pipe, `&&`, an
    env-var prefix, etc. (a realistic real-world config, e.g.
    `curl -sF file=@{FILE} https://uploader.example | jq -r .url`). The shipped fix keeps
    eval'ing the FULL trusted template (only the untrusted path is neutralized via `printf %q`),
    so a template built from two chained commands must still work end-to-end."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n" + b"x" * 100)

    # A template that only works if the WHOLE string is still eval'd as one pipeline: cat the
    # file, count bytes, and print a URL that embeds the count — impossible to produce via a
    # single argv-array `exec`, only via real shell chaining (| and &&).
    stage = tmp_path / "url_from_size.sh"
    stage.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f'n=$(wc -c < "{png}")\n'
        'echo "https://example.invalid/size-${n// /}.png"\n',
        encoding="utf-8",
    )
    stage.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, stage)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f"cat {{FILE}} | wc -c > /dev/null && {stage}"
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "uploaded 'desc' -> https://example.invalid/size-106.png" in r.stdout, (
        "pipe/&&-based SHIP_IMAGE_UPLOAD_CMD template did not run end-to-end:\n" + r.stdout + r.stderr
    )


def test_upload_png_preserves_quoted_multiword_arg_template(repo_with_pr_worktree, tmp_path):
    """Regression pinned from multi-model review of the HYP-1260 fix: a template with a quoted
    multi-word argument (e.g. `my-uploader --token "a b" {FILE}`) must still pass that argument
    as ONE word, exactly as the pre-fix eval-based implementation did."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-quoted-arg.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f'{uploader} --token "a b" {{FILE}}'
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == ["--token", "a b", str(png)], (
        f"quoted multi-word template arg must stay ONE word, got: {got}"
    )


@pytest.mark.parametrize("quote_style", ['"{FILE}"', "'{FILE}'"], ids=["double-quoted", "single-quoted"])
def test_upload_png_quoted_file_token_with_space_in_path(repo_with_pr_worktree, tmp_path, quote_style):
    """Regression pinned from a SECOND round of multi-model review of the HYP-1260 fix: quoting
    the `{FILE}` placeholder (`uploader "{FILE}"` / `uploader '{FILE}'`) was the RECOMMENDED way
    to handle a screenshot path containing a space under the pre-fix raw-substitution behavior.
    A naive `%q`-then-splice-into-the-template's-own-quotes fix breaks exactly this case (the
    %q token nests inside the operator's quotes and comes out corrupted, e.g. a backslash-
    escaped space that no longer re-parses as one word). The shipped fix recognizes a
    quote-wrapped `{FILE}` and replaces the WHOLE quoted form (dropping the now-redundant
    operator quotes, since the %q token supplies its own), so this must still work."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    spaced_dir = tmp_path / "my shots"
    spaced_dir.mkdir()
    png = spaced_dir / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-quoted-file-token.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f"{uploader} {quote_style}"
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == [str(png)], (
        f"a space-containing path via a quoted {{FILE}} placeholder must arrive as ONE "
        f"unmangled argument, got: {got}"
    )


def test_upload_png_quoted_file_token_blocks_injection(repo_with_pr_worktree, tmp_path):
    """The quote-wrapped-`{FILE}` code path (added for the space-in-path regression above) is a
    DISTINCT branch from the bare-`{FILE}` and appended-arg branches already covered by the
    injection tests above — it must independently resist the same HYP-1260 injection PoC."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    injection_marker = tmp_path / "PWNED_QUOTED_FILE_TOKEN"
    evil_dir = tmp_path / 'evil"; touch "$PWNED_TARGET"; echo "'
    evil_dir.mkdir()
    png = evil_dir / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-quoted-file-token-injection.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["PWNED_TARGET"] = str(injection_marker)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f'{uploader} "{{FILE}}"'
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert not injection_marker.exists(), (
        "shell injection via a quoted {FILE} placeholder executed `touch`!\n" + r.stdout + r.stderr
    )
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == [str(png)], f"uploader must receive the raw path as ONE argument, got: {got}"


def test_upload_png_mixed_quoted_and_bare_file_token_forms_both_substitute(repo_with_pr_worktree, tmp_path):
    """Regression pinned from a THIRD round of multi-model review of the HYP-1260 fix: an
    earlier draft picked exactly ONE recognized `{FILE}` form per template (via `case`/`elif`),
    so a template mixing a quoted and a bare occurrence — `--src "{FILE}" --thumb {FILE}`, a
    realistic shape for e.g. attaching both a full image and deriving a checksum from the same
    path — only substituted the first-matched form and left the other as the LITERAL string
    `{FILE}`. The pre-fix `${var//pat/rep}` replaced every occurrence (global replace); the
    shipped fix restores that: `_upload_png_compose_cmd` recognizes and substitutes every
    occurrence of any of the three `{FILE}` forms in one left-to-right pass."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-mixed-forms.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f'{uploader} --src "{{FILE}}" --thumb {{FILE}}'
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == ["--src", str(png), "--thumb", str(png)], (
        f"both the quoted AND bare {{FILE}} occurrences must be substituted, got: {got}"
    )


def test_upload_png_path_containing_literal_file_token_text_is_not_corrupted(repo_with_pr_worktree, tmp_path):
    """Regression pinned from a FOURTH round of multi-model review of the HYP-1260 fix: an
    earlier draft chained three SEPARATE replacement passes (quoted-double, quoted-single,
    bare), each operating on the OUTPUT of the previous. `printf %q` does not escape `{`/`}`,
    so if the screenshot path itself contains the literal 7-character substring `{FILE}`, that
    text survives into the %q-quoted replacement token spliced in by an earlier pass — and a
    LATER pass (scanning the accumulated string) then re-matches and re-substitutes THAT text,
    corrupting the composed command (e.g. duplicating/mangling the path). The shipped fix
    (`_upload_png_compose_cmd`) does a SINGLE pass over the ORIGINAL template, so it can never
    rescan text it already emitted. A directory literally named `{FILE}` is a real (if
    unusual) shape — e.g. an operator's own placeholder convention colliding by coincidence."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    tricky_dir = tmp_path / "{FILE}"
    tricky_dir.mkdir()
    png = tricky_dir / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-literal-file-token-path.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    # Quoted-{FILE} template: the exact shape where an earlier draft's pass ordering let a
    # later pass re-scan the quoted form's already-spliced-in replacement text.
    env = _screenshot_upload_env(main, bindir, uploader)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f'{uploader} "{{FILE}}"'
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert argv_log.exists(), f"uploader was never invoked:\n{r.stdout}\n{r.stderr}"
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == [str(png)], (
        f"a path containing the literal text '{{FILE}}' must survive unmangled, got: {got}"
    )


def test_upload_png_trailing_newline_template_still_appends_path(repo_with_pr_worktree, tmp_path):
    """Regression pinned from a FIFTH round of multi-model review of the HYP-1260 fix: an
    earlier draft detected "no {FILE} placeholder -> append the path" by comparing the composed
    output back to the template via `[ "$cmd" = "$SHIP_IMAGE_UPLOAD_CMD" ]`. `cmd=$(...)`
    (command substitution) strips trailing newlines, so a placeholder-free template ending in a
    newline would never equal its own (newline-stripped) compose output, the append branch
    would never fire, and the path argument would be silently DROPPED — the uploader runs with
    no file at all. The shipped fix detects the placeholder directly from the template
    (`case "$SHIP_IMAGE_UPLOAD_CMD" in *'{FILE}'*)`), immune to that stripping, matching the
    pre-fix `grep -q '{FILE}'` check exactly."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n")

    argv_log = tmp_path / "uploader-argv.log"
    uploader = tmp_path / "fake-uploader.sh"
    uploader.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{argv_log}"\n'
        "echo https://example.invalid/uploaded-trailing-newline.png\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)

    env = _screenshot_upload_env(main, bindir, uploader)
    env["SHIP_IMAGE_UPLOAD_CMD"] = f"{uploader}\n"  # placeholder-free, trailing newline
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test",
        "--screenshot", str(png), "desc",
        cwd=wt, env=env,
    )

    assert r.returncode == 0, f"ship exited {r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert argv_log.exists(), (
        f"uploader was never invoked (path silently dropped):\n{r.stdout}\n{r.stderr}"
    )
    got = argv_log.read_text(encoding="utf-8").splitlines()
    assert got == [str(png)], f"path must still be appended for a trailing-newline template, got: {got}"


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
# Hook-descriptor guard (agent-tools#905): an installed agent-hook descriptor (or a harness
# config rig writes) whose cmd lives INSIDE the PR worktree must keep that worktree alive —
# removing it makes every such hook fail, and an `on_error: open` hook then stops gating
# machine-wide (runpod-offload-gate after PR #904). Each test runs under the conftest's
# hermetic $HOME, so the descriptor dirs below are temp dirs, never the developer's real ones.
# ---------------------------------------------------------------------------------------

_HOOK_ROOT_ENV = ("OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR",
                  "RIG_CODEX_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
                  "OMP_HOOKS_DIR", "CC_HOOKS_DIR", "CODEX_HOOKS_DIR", "OPENCODE_HOOKS_DIR",
                  "AGENT_TOOLS_ROOT")


@pytest.fixture
def hook_home(monkeypatch):
    """The hermetic $HOME with every harness-root override cleared, so the guard resolves the
    default `~/.omp/agent`, `~/.claude`, `~/.codex`, `~/.config` roots under it."""
    for name in _HOOK_ROOT_ENV:
        monkeypatch.delenv(name, raising=False)
    return Path(os.environ["HOME"])


def _write_descriptor(hooks_dir: Path, cmd: Path, name: str = "demo-gate") -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    desc = hooks_dir / f"{name}.pre-bash.json"
    desc.write_text(json.dumps({"id": name, "point": "pre-bash", "cmd": str(cmd),
                                "on_error": "open"}, indent=2) + "\n", encoding="utf-8")
    return desc


def _assert_kept_for_hooks(r, main: Path, wt: Path, cmd: Path, reference: Path):
    assert r.returncode == 0, f"ship must exit 0 after merge; got {r.returncode}\n{r.stderr}"
    assert "merged #1" in r.stdout, r.stdout
    assert wt.exists(), f"worktree backing a live hook was removed:\n{r.stdout}\n{r.stderr}"
    assert cmd.exists(), f"hook cmd {cmd} no longer exists after ship"
    remaining = _sh("git", "worktree", "list", "--porcelain", cwd=main).stdout
    assert str(wt) in remaining, f"worktree was unlinked:\n{remaining}"
    # The refusal names the referencing file and the exact fix to run.
    assert str(reference) in r.stderr, f"guard did not name {reference}:\n{r.stderr}"
    assert "rig apply commit --only agent_hooks" in r.stderr, r.stderr
    assert f"worktree remove {wt}" in r.stderr, r.stderr


def test_kept_rig_worktree_gets_rig_worktree_remove_fix(repo_with_pr_worktree, tmp_path, hook_home):
    """A tree at the sanctioned `<main>/.worktrees/<name>` gets the `rig worktree remove <name>`
    fix (removes tree + branch; a raw `git worktree remove` there is what the hooks redirect)."""
    main, wt = repo_with_pr_worktree
    (main / ".git" / "info" / "exclude").write_text("/.worktrees/\n", encoding="utf-8")
    rig_wt = main / ".worktrees" / "feat"
    rig_wt.parent.mkdir()
    _git("worktree", "move", str(wt), str(rig_wt), cwd=main)
    bindir = _fake_gh_dir(tmp_path)
    cmd = rig_wt / "README.md"
    desc = _write_descriptor(hook_home / ".omp" / "agent" / "hooks", cmd)

    r = _run_ship(main, bindir)

    assert rig_wt.exists() and cmd.exists(), r.stderr
    assert str(desc) in r.stderr, r.stderr
    assert f"rig worktree remove feat -C {main}" in r.stderr, r.stderr


@pytest.mark.parametrize("from_inside", [False, True], ids=["from-main", "from-worktree"])
def test_ship_keeps_worktree_backing_installed_hook_descriptor(
    repo_with_pr_worktree, tmp_path, hook_home, from_inside
):
    """The #905 repro: an omp descriptor's cmd points into the PR worktree. After ship the
    descriptor's cmd must still exist (the worktree is kept), and the fix command is printed."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    cmd = wt / "README.md"  # a tracked file of the PR branch stands in for the hook script
    desc = _write_descriptor(hook_home / ".omp" / "agent" / "hooks", cmd)

    r = _run_ship(main, bindir, cwd=wt if from_inside else None)

    _assert_kept_for_hooks(r, main, wt, cmd, desc)
    assert "re-rooting into" not in r.stdout, "re-rooted to remove a tree that backs a live hook"


def test_ship_removes_worktree_when_descriptors_point_elsewhere(
    repo_with_pr_worktree, tmp_path, hook_home
):
    """No false positive: descriptors pointing at the main checkout, or at a sibling path that
    merely shares the worktree's name as a prefix (`<wt>-other/...`), do not block removal."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    hooks = hook_home / ".omp" / "agent" / "hooks"
    _write_descriptor(hooks, main / "README.md", name="main-gate")
    _write_descriptor(hooks, Path(f"{wt}-other") / "gate.py", name="sibling-gate")

    r = _run_ship(main, bindir)

    assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert not wt.exists(), f"worktree kept without a hook pointing into it:\n{r.stderr}"
    assert "rig apply commit --only agent_hooks" not in r.stderr, r.stderr


@pytest.mark.parametrize("layout", [
    "claude-descriptor", "claude-settings-bridge", "claude-account-settings",
    "claude-settings-reference-past-4mib", "codex-descriptor", "codex-config-toml",
    "omp-extension-symlink", "omp-extension-file-uri", "omp-descriptor-json-unicode-escapes",
    "opencode-descriptor", "opencode-plugin-package", "opencode-repo-local-plugin-main",
    "opencode-repo-local-plugin-registry", "opencode-repo-local-plugins-dir-symlink",
    "omp-extensions-dir-symlink", "codex-descriptor-cmd-via-external-symlink",
    "claude-settings-pythonpath-via-external-symlink", "omp-descriptor-named-like-a-backup",
    "codex-descriptor-cmd-with-space-via-symlink", "agent-tools-env",
])
def test_ship_keeps_worktree_referenced_by_any_rig_written_hook_config(
    repo_with_pr_worktree, tmp_path, hook_home, layout
):
    """Every place rig writes a path the harnesses execute is scanned — the per-harness
    descriptor dirs, the bridge commands in settings/config files, the omp/pi extension
    symlinks, and the machine-level AGENT_TOOLS_ROOT pointer the `gh ship` delegator sources."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    cmd = wt / "README.md"
    h = hook_home
    if layout == "claude-descriptor":
        ref = _write_descriptor(h / ".claude" / "hooks", cmd)
    elif layout == "claude-settings-bridge":
        ref = h / ".claude" / "settings.json"
        ref.parent.mkdir(parents=True)
        ref.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{
            "type": "command",
            "command": f"PYTHONPATH={wt}/lib python3 -m cc_hook_bridge PreToolUse"}]}]}}),
            encoding="utf-8")
    elif layout == "claude-account-settings":
        ref = h / ".claude-accounts" / "account-2" / "settings.json"
        ref.parent.mkdir(parents=True)
        ref.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": f"PYTHONPATH={wt}/lib python3 -m cc_hook_bridge Stop"}]}]}}),
            encoding="utf-8")
    elif layout == "codex-descriptor":
        ref = _write_descriptor(h / ".codex" / "hooks", cmd)
    elif layout == "codex-config-toml":
        ref = h / ".codex" / "config.toml"
        ref.parent.mkdir(parents=True)
        ref.write_text(f'[hooks]\ncommand = "PYTHONPATH={wt}/lib python3 -m codex_hook_bridge"\n',
                       encoding="utf-8")
    elif layout == "claude-settings-reference-past-4mib":
        # No read cap: a reference deep in a large settings file must not read as absent.
        ref = h / ".claude" / "settings.json"
        ref.parent.mkdir(parents=True)
        ref.write_text(json.dumps({"padding": "x" * (5 * 1024 * 1024)}) + "\n"
                       + json.dumps({"command": f"PYTHONPATH={wt}/lib python3 -m cc_hook_bridge"}),
                       encoding="utf-8")
    elif layout == "omp-extension-symlink":
        ref = h / ".omp" / "agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
        ref.parent.mkdir(parents=True)
        ref.symlink_to(cmd)
    elif layout == "omp-extension-file-uri":
        # `file://` + an absolute path puts a `/` right before the tree's path.
        ref = h / ".omp" / "agent" / "extensions" / "zz-agent-tools-hook-bridge.ts"
        ref.parent.mkdir(parents=True)
        ref.write_text(f'export {{ default }} from "file://{wt}/lib/omp_hook_bridge/x.ts";\n',
                       encoding="utf-8")
    elif layout == "omp-descriptor-json-unicode-escapes":
        # Valid JSON a descriptor reader decodes to the plain path: `\u002f` for every `/`.
        hooks = h / ".omp" / "agent" / "hooks"
        hooks.mkdir(parents=True)
        ref = hooks / "escaped-gate.pre-bash.json"
        escaped = str(cmd).replace("/", "\\u002f")
        ref.write_text(f'{{"id": "escaped-gate", "point": "pre-bash", "cmd": "{escaped}"}}\n',
                       encoding="utf-8")
        assert json.loads(ref.read_text(encoding="utf-8"))["cmd"] == str(cmd)
    elif layout == "opencode-descriptor":
        ref = _write_descriptor(h / ".config" / "opencode" / "hooks", cmd)
    elif layout == "opencode-plugin-package":
        # A packaged plugin is a real directory, not one flat file or link.
        ref = h / ".config" / "opencode" / "plugins" / "agent-tools-bridge" / "dist" / "index.js"
        ref.parent.mkdir(parents=True)
        ref.write_text(f'require("{wt}/lib/opencode_hook_bridge/plugin.js");\n', encoding="utf-8")
    elif layout == "opencode-repo-local-plugin-main":
        # rig's default opencode bridge is a REPO-local link; the shipped repo's main checkout
        # is always scanned.
        (main / ".git" / "info" / "exclude").write_text("/.opencode/\n", encoding="utf-8")
        ref = main / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
        ref.parent.mkdir(parents=True)
        ref.symlink_to(cmd)
    elif layout == "opencode-repo-local-plugin-registry":
        # ...and so is every repo in rig's registry.
        consumer = tmp_path / "consumer"
        ref = consumer / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
        ref.parent.mkdir(parents=True)
        ref.symlink_to(cmd)
        registry = h / ".config" / "rig" / "repositories.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"version": 1, "repositories": [{"path": str(consumer)}]}),
                            encoding="utf-8")
    elif layout == "codex-descriptor-cmd-with-space-via-symlink":
        # The runner execs `cmd` verbatim: a link dir with a space, never spelling the tree.
        opt = tmp_path / "opt agent hooks"
        opt.symlink_to(wt, target_is_directory=True)
        ref = _write_descriptor(h / ".codex" / "hooks", opt / "README.md")
    elif layout == "omp-descriptor-named-like-a-backup":
        # Live: the loader reads every *.json; only `<name>.rig-bak-<timestamp>` is a backup.
        ref = _write_descriptor(h / ".omp" / "agent" / "hooks", cmd, name="security.rig-bak")
    elif layout == "codex-descriptor-cmd-via-external-symlink":
        # The cmd never spells the tree's path: /…/opt-hooks is a link into the tree.
        opt = tmp_path / "opt-hooks"
        opt.symlink_to(wt, target_is_directory=True)
        ref = _write_descriptor(h / ".codex" / "hooks", opt / "README.md")
    elif layout == "claude-settings-pythonpath-via-external-symlink":
        opt = tmp_path / "opt-agent-tools"
        opt.symlink_to(wt, target_is_directory=True)
        ref = h / ".claude" / "settings.json"
        ref.parent.mkdir(parents=True)
        ref.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command",
            "command": f"PYTHONPATH=/usr/lib:{opt}/lib python3 -m cc_hook_bridge Stop"}]}]}}),
            encoding="utf-8")
    elif layout == "opencode-repo-local-plugins-dir-symlink":
        # The plugins DIR is the link; its files hold no path at all (README.md here: "# x").
        (main / ".git" / "info" / "exclude").write_text("/.opencode/\n", encoding="utf-8")
        ref = main / ".opencode" / "plugins"
        ref.parent.mkdir(parents=True)
        ref.symlink_to(wt, target_is_directory=True)
    elif layout == "omp-extensions-dir-symlink":
        ref = h / ".omp" / "agent" / "extensions"
        ref.parent.mkdir(parents=True)
        ref.symlink_to(wt, target_is_directory=True)
    else:  # agent-tools-env
        ref = h / ".config" / "agent-tools" / "env"
        ref.parent.mkdir(parents=True)
        ref.write_text(f"AGENT_TOOLS_ROOT={wt}\n", encoding="utf-8")

    r = _run_ship(main, bindir)

    _assert_kept_for_hooks(r, main, wt, cmd, ref)


@pytest.mark.parametrize("env_var", [
    "OMP_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "RIG_CODEX_HOME", "CODEX_HOME",
    "CLAUDE_CONFIG_DIR", "OMP_HOOKS_DIR", "CC_HOOKS_DIR", "CODEX_HOOKS_DIR", "OPENCODE_HOOKS_DIR",
])
def test_ship_honors_harness_root_overrides(
    repo_with_pr_worktree, tmp_path, hook_home, monkeypatch, env_var
):
    """A relocated harness root (omp's OMP_/PI_CODING_AGENT_DIR, rig's RIG_CODEX_HOME, Codex's
    own CODEX_HOME, Claude's CLAUDE_CONFIG_DIR) or a bridge's own `<HARNESS>_HOOKS_DIR` is where
    the descriptors live, so the guard looks there too."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    cmd = wt / "README.md"
    root = tmp_path / f"relocated-{env_var.lower()}"
    monkeypatch.setenv(env_var, str(root))
    desc = _write_descriptor(root if env_var.endswith("_HOOKS_DIR") else root / "hooks", cmd)

    r = _run_ship(main, bindir)

    _assert_kept_for_hooks(r, main, wt, cmd, desc)


def test_ship_keeps_worktree_an_exported_agent_tools_root_points_into(
    repo_with_pr_worktree, tmp_path, hook_home, monkeypatch
):
    """The `gh ship` delegator prefers an exported AGENT_TOOLS_ROOT over the env file."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    monkeypatch.setenv("AGENT_TOOLS_ROOT", str(wt))

    r = _run_ship(main, bindir)

    _assert_kept_for_hooks(r, main, wt, wt / "README.md", "$AGENT_TOOLS_ROOT")


def test_ship_honors_a_relative_hooks_dir_override_verbatim(
    repo_with_pr_worktree, tmp_path, hook_home, monkeypatch
):
    """The bridges use `<HARNESS>_HOOKS_DIR` verbatim (`Path(value)`), so a relative value is
    resolved against the cwd, not home — scan that location too."""
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    cmd = wt / "README.md"
    (main / ".git" / "info" / "exclude").write_text("/relhooks/\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOOKS_DIR", "relhooks")
    desc = _write_descriptor(main / "relhooks", cmd)

    r = _run_ship(main, bindir)  # cwd = main

    _assert_kept_for_hooks(r, main, wt, cmd, desc)


def test_hook_refs_matches_a_shell_escaped_worktree_path(tmp_path, hook_home):
    """A bridge command may spell a tree with a space as `feat\\ hook` (shell escaping)."""
    wt = tmp_path / "feat hook"
    (wt / "lib").mkdir(parents=True)
    settings = hook_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    escaped = str(wt).replace(" ", "\\ ")
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
        "type": "command", "command": f"PYTHONPATH={escaped}/lib python3 -m cc_hook_bridge Stop"}]}]}}),
        encoding="utf-8")

    r = _sh(sys.executable, "-I", str(_SHIP.parent / "hook_refs.py"), str(wt), cwd=tmp_path,
            env=dict(os.environ))

    assert r.returncode == 3, f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert str(settings) in r.stdout, r.stdout



@pytest.mark.parametrize("blocked", ["descriptor", "ancestor-dir"])
def test_ship_keeps_worktree_when_hook_scan_cannot_read_a_descriptor(
    repo_with_pr_worktree, tmp_path, hook_home, blocked
):
    """Fail safe: an unreadable descriptor — or a harness dir the shipping process cannot
    enter, which hides every descriptor below it — might be the one pointing into the
    worktree, so an incomplete scan keeps the tree (a leftover worktree is cheap; a
    silently-open gate is not)."""
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    desc = _write_descriptor(hook_home / ".codex" / "hooks", wt / "README.md")
    locked = desc if blocked == "descriptor" else hook_home / ".codex"
    locked.chmod(0)
    try:
        r = _run_ship(main, bindir)
    finally:
        locked.chmod(0o755 if blocked == "ancestor-dir" else 0o644)

    assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stderr}"
    assert wt.exists(), f"worktree removed although the hook scan was incomplete:\n{r.stderr}"
    assert str(locked) in r.stderr, r.stderr
    assert "unreadable" in r.stderr, r.stderr
    assert "rig apply commit --only agent_hooks" in r.stderr, r.stderr


@pytest.mark.parametrize("late", ["reference", "unreadable"])
@pytest.mark.parametrize("self_hosted", [False, True], ids=["from-main", "tree-ships-itself"])
def test_ship_warns_when_a_hook_points_into_the_tree_during_removal(
    repo_with_pr_worktree, tmp_path, hook_home, self_hosted, late
):
    """The pre-removal scan is a snapshot. A descriptor written between it and the removal (a
    concurrent `rig apply` from the tree) is caught by the post-removal re-scan and reported
    loudly with the fix — as is a late descriptor the re-scan cannot read (the scan is then
    incomplete). `tree-ships-itself` is agent-tools shipping its own PR: ship.sh and
    hook_refs.py run from INSIDE the tree, so the scanner file is gone by the re-scan. Injected
    by a post-checkout hook: ship detaches the tree's HEAD after the scan, right before
    `git worktree remove`."""
    if late == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    main, wt = repo_with_pr_worktree
    bindir = _fake_gh_dir(tmp_path)
    cmd = wt / "README.md"
    ship = _SHIP
    if self_hosted:
        shutil.copytree(_SHIP.parent, wt / "ci" / "ship",
                        ignore=shutil.ignore_patterns("__pycache__"))
        _git("add", "-A", cwd=wt)
        _git("commit", "-qm", "self-hosted ship", cwd=wt)
        _git("push", "-q", "origin", "feat", cwd=wt)
        ship = wt / "ci" / "ship" / "ship.sh"
    hooks_dir = hook_home / ".omp" / "agent" / "hooks"
    desc = hooks_dir / "late-gate.pre-bash.json"
    githooks = tmp_path / "githooks"
    githooks.mkdir()
    post_checkout = githooks / "post-checkout"
    post_checkout.write_text(
        "#!/usr/bin/env bash\n"
        f"mkdir -p {shlex.quote(str(hooks_dir))}\n"
        f"printf '{{\"cmd\": \"%s\"}}\\n' {shlex.quote(str(cmd))} > {shlex.quote(str(desc))}\n"
        + (f"chmod 000 {shlex.quote(str(desc))}\n" if late == "unreadable" else ""),
        encoding="utf-8",
    )
    post_checkout.chmod(0o755)
    _git("config", "core.hooksPath", str(githooks), cwd=main)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env.update(SHIP_TEST_BRANCH="feat", SHIP_DEFAULT_BRANCH="main", SHIP_MAIN_CHECKOUT=str(main))
    r = _sh("bash", str(ship), "1", "--no-screenshot-ok", "test",
            cwd=wt if self_hosted else main, env=env)

    try:
        assert r.returncode == 0, f"ship exited {r.returncode}\n{r.stdout}\n{r.stderr}"
        assert desc.exists(), "the injected descriptor was never written — test proves nothing"
        assert not wt.exists(), f"tree not removed — the pre-removal scan saw the late write:\n{r.stderr}"
        expected = "started pointing into" if late == "reference" else "re-scan of the installed agent-hook wiring was incomplete"
        assert expected in r.stderr, r.stderr
        assert str(desc) in r.stderr, r.stderr
        assert "rig apply commit --only agent_hooks" in r.stderr, r.stderr
    finally:
        if desc.exists():
            desc.chmod(0o644)


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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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


def test_version_bump_already_on_base_tip_is_blocked(repo_with_pyproject, tmp_path):
    """The PR diff (merge-base relative) shows 0.4.1 -> 0.4.2, but origin/main ALREADY moved
    to 0.4.2 (a sibling PR released first). Merging would change no version, so the gate
    must REFUSE and name both values (agent-tools#592: rig-cli #369/#370 shipped a feature
    under a number main already had)."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)
    (main / "pyproject.toml").write_text(
        '[project]\nname = "mytool"\nversion = "0.4.2"\n', encoding="utf-8"
    )
    _git("commit", "-qam", "release 0.4.2 (sibling PR)", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship_vbump(
        main, bindir, name_only="src/a.py\npyproject.toml", patch=_PATCH_WITH_BUMP
    )

    assert r.returncode != 0, f"ship should refuse a bump the base tip already has\n{r.stdout}\n{r.stderr}"
    assert "base branch tip ALREADY has version 0.4.2" in r.stderr, r.stderr
    assert "bumps pyproject.toml to 0.4.2" in r.stderr, r.stderr
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


def test_root_ship_config_only_pr_is_not_required_to_bump(repo_with_pyproject, tmp_path):
    """A PR that only adds/edits the REPO-ROOT .ship-config is exempt -- it's ship's own
    CI/merge-gate metadata, not product code."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    patch = (
        "diff --git a/.ship-config b/.ship-config\n"
        "--- a/.ship-config\n+++ b/.ship-config\n"
        "@@ -0,0 +1 @@\n+SHIP_LOCAL_TEST_CMD=npm test\n"
    )
    r = _run_ship_vbump(main, bindir, name_only=".ship-config", patch=patch)

    assert r.returncode == 0, f"root .ship-config-only PR must not be blocked\n{r.stdout}\n{r.stderr}"
    assert "no shippable source" in r.stdout, r.stdout


def test_nested_ship_config_named_file_still_requires_bump(repo_with_pyproject, tmp_path):
    """Regression: a NESTED file that merely shares the basename `.ship-config` (e.g.
    `src/.ship-config`) is NOT the file _ship_config_load ever reads (only the repo-root one
    is), so it must NOT get the version-bump exemption -- the exemption is an exact-path
    match, not a basename match."""
    main, _wt = repo_with_pyproject
    bindir = _fake_gh_vbump_dir(tmp_path)

    patch = (
        "diff --git a/src/.ship-config b/src/.ship-config\n"
        "--- a/src/.ship-config\n+++ b/src/.ship-config\n"
        "@@ -0,0 +1 @@\n+SHIP_LOCAL_TEST_CMD=npm test\n"
    )
    r = _run_ship_vbump(main, bindir, name_only="src/.ship-config", patch=patch)

    assert r.returncode != 0, (
        f"a nested src/.ship-config must still require a version bump\n{r.stdout}\n{r.stderr}"
    )
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr


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


# ---------------------------------------------------------------------------------------
# Dynamic versions: a pyproject with `[project] dynamic = ["version"]` keeps the real version
# in a module's `__version__` (setuptools `attr`, hatch `path`). Its own `version = { attr =
# ... }` line never moves, so the gate must follow it to that module (qa-cli PR #23 bumped
# qa_cli/__init__.py 0.4.0 -> 0.5.0 and still had to ship with --no-version-bump-ok).
# ---------------------------------------------------------------------------------------

_PYPROJECT_SETUPTOOLS_ATTR = (
    '[project]\nname = "mytool"\ndynamic = ["version"]\n\n'
    '[tool.setuptools.dynamic]\nversion = { attr = "mytool.__version__" }\n'
)
_PYPROJECT_HATCH_PATH = (
    '[project]\nname = "mytool"\ndynamic = ["version"]\n\n'
    '[tool.hatch.version]\npath = "src/mytool/__about__.py"\n'
)

# A PR diff that changes source but touches no version line.
_PATCH_DYNAMIC_SOURCE_ONLY = (
    "diff --git a/mytool/cli.py b/mytool/cli.py\n"
    "--- a/mytool/cli.py\n+++ b/mytool/cli.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


def _version_module_patch(path: str, old: str, new: str) -> str:
    """A PR diff changing source plus `path`'s `__version__` from `old` to `new`."""
    return _PATCH_DYNAMIC_SOURCE_ONLY + (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f'@@ -3 +3 @@\n-__version__ = "{old}"\n+__version__ = "{new}"\n'
    )


def _repo_with_files(tmp_path: Path, files: dict) -> Path:
    """A repo on `main` holding `files` ({relpath: text}), branch `feat` in a worktree, and an
    origin remote: the version-gate fixtures above, for an arbitrary layout. Returns main."""
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
    for rel, text in files.items():
        (main / rel).parent.mkdir(parents=True, exist_ok=True)
        (main / rel).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "wt-feat"), "feat", cwd=main)
    return main


@pytest.fixture
def repo_with_dynamic_attr_version(tmp_path):
    """qa-cli's layout: setuptools reads the version from `mytool.__version__`."""
    return _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR,
        "mytool/__init__.py": '"""mytool."""\n\n__version__ = "0.4.0"\n',
    })


def test_dynamic_attr_version_bump_passes(repo_with_dynamic_attr_version, tmp_path):
    """A setuptools `attr` version whose `__version__` moved is a bump: the gate follows the
    pyproject to mytool/__init__.py, reads the value there, and merges."""
    main = repo_with_dynamic_attr_version
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="mytool/cli.py\nmytool/__init__.py",
        patch=_version_module_patch("mytool/__init__.py", "0.4.0", "0.5.0"),
    )

    assert r.returncode == 0, f"a moved __version__ must pass the gate\n{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout
    assert "mytool/__init__.py version is bumped" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_dynamic_attr_version_unchanged_is_blocked(repo_with_dynamic_attr_version, tmp_path):
    """A source change that leaves the module's `__version__` alone is refused, and the refusal
    names the module that holds the version, not pyproject.toml."""
    main = repo_with_dynamic_attr_version
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="mytool/cli.py", patch=_PATCH_DYNAMIC_SOURCE_ONLY)

    assert r.returncode != 0, f"an unchanged __version__ must be refused\n{r.stdout}\n{r.stderr}"
    assert "version in mytool/__init__.py is UNCHANGED" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_dynamic_attr_version_already_on_base_tip_is_blocked(repo_with_dynamic_attr_version, tmp_path):
    """The PR moves `__version__` 0.4.0 -> 0.5.0, but origin/main's module already says 0.5.0:
    the base-tip read must understand `__version__ = "X"` too (agent-tools#592 shape)."""
    main = repo_with_dynamic_attr_version
    bindir = _fake_gh_vbump_dir(tmp_path)
    (main / "mytool" / "__init__.py").write_text(
        '"""mytool."""\n\n__version__ = "0.5.0"\n', encoding="utf-8"
    )
    _git("commit", "-qam", "release 0.5.0 (sibling PR)", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship_vbump(
        main, bindir, name_only="mytool/cli.py\nmytool/__init__.py",
        patch=_version_module_patch("mytool/__init__.py", "0.4.0", "0.5.0"),
    )

    assert r.returncode != 0, f"a bump the base tip already has must be refused\n{r.stdout}\n{r.stderr}"
    assert "bumps mytool/__init__.py to 0.5.0" in r.stderr, r.stderr
    assert "base branch tip ALREADY has version 0.5.0" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_dynamic_attr_base_tip_reads_dunder_version_not_code(repo_with_dynamic_attr_version, tmp_path):
    """In a Python module only `__version__` declares the version: an earlier code line such as
    `version = "0.0.0"` must not be read as the base tip's version (it would hide a taken 0.5.0)."""
    main = repo_with_dynamic_attr_version
    bindir = _fake_gh_vbump_dir(tmp_path)
    (main / "mytool" / "__init__.py").write_text(
        '"""mytool."""\n\n\ndef _fallback():\n    version = "0.0.0"\n    return version\n\n\n'
        '__version__ = "0.5.0"\n',
        encoding="utf-8",
    )
    _git("commit", "-qam", "release 0.5.0 (sibling PR)", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship_vbump(
        main, bindir, name_only="mytool/cli.py\nmytool/__init__.py",
        patch=_version_module_patch("mytool/__init__.py", "0.4.0", "0.5.0"),
    )

    assert r.returncode != 0, f"a bump the base tip already has must be refused\n{r.stdout}\n{r.stderr}"
    assert "base branch tip ALREADY has version 0.5.0" in r.stderr, r.stderr


def test_hatch_path_version_bump_passes(tmp_path):
    """Hatch's `[tool.hatch.version] path` names the version module directly (src layout
    here); a moved `__version__` there is a bump."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _PYPROJECT_HATCH_PATH,
        "src/mytool/__about__.py": '__version__ = "1.2.0"\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="mytool/cli.py\nsrc/mytool/__about__.py",
        patch=_version_module_patch("src/mytool/__about__.py", "1.2.0", "1.3.0"),
    )

    assert r.returncode == 0, f"a moved hatch __version__ must pass the gate\n{r.stdout}\n{r.stderr}"
    assert "src/mytool/__about__.py version is bumped" in r.stdout, r.stdout


def test_hatch_path_version_unchanged_is_blocked(tmp_path):
    """A hatch `path` pyproject has no `version =` line at all, so the gate used to find "no
    version file" and skip: a source change with an unchanged `__version__` merged unchecked."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _PYPROJECT_HATCH_PATH,
        "src/mytool/__about__.py": '__version__ = "1.2.0"\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="mytool/cli.py", patch=_PATCH_DYNAMIC_SOURCE_ONLY)

    assert r.returncode != 0, f"an unchanged hatch __version__ must be refused\n{r.stdout}\n{r.stderr}"
    assert "version in src/mytool/__about__.py is UNCHANGED" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_ship_version_files_pinned_module_reads_dunder_version(tmp_path):
    """SHIP_VERSION_FILES pinned at a module: its `__version__ = "X"` line is a version
    declaration, so a moved value passes (the pin used to see no version line at all)."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "mytool/__init__.py": '"""mytool."""\n\n__version__ = "0.4.0"\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(
        main, bindir, name_only="mytool/cli.py\nmytool/__init__.py",
        patch=_version_module_patch("mytool/__init__.py", "0.4.0", "0.5.0"),
        env_extra={"SHIP_VERSION_FILES": "mytool/__init__.py"},
    )

    assert r.returncode == 0, f"a pinned module's moved __version__ must pass\n{r.stdout}\n{r.stderr}"
    assert "mytool/__init__.py version is bumped" in r.stdout, r.stdout


def test_dynamic_attr_version_comparison_is_not_a_bump(repo_with_dynamic_attr_version, tmp_path):
    """An added `__version__ == "9.9.9"` line in the version module is a comparison, not an
    assignment: with `__version__` itself untouched the gate must refuse (review finding: the
    `.py` pattern used to match the first `=` of `==`)."""
    main = repo_with_dynamic_attr_version
    bindir = _fake_gh_vbump_dir(tmp_path)
    patch = _PATCH_DYNAMIC_SOURCE_ONLY + (
        "diff --git a/mytool/__init__.py b/mytool/__init__.py\n"
        "--- a/mytool/__init__.py\n+++ b/mytool/__init__.py\n"
        '@@ -3,0 +4 @@\n+__version__ == "9.9.9"\n'
    )

    r = _run_ship_vbump(main, bindir, name_only="mytool/cli.py\nmytool/__init__.py", patch=patch)

    assert r.returncode != 0, f"a comparison is not a bump\n{r.stdout}\n{r.stderr}"
    assert "version in mytool/__init__.py is UNCHANGED" in r.stderr, r.stderr


def test_dynamic_attr_annotated_version_bump_passes(tmp_path):
    """`__version__: T1 = "0.5.0"`: the value is the quoted literal after `=`, not the first
    digit on the line (the `1` of the annotation would make 0.4.0 -> 0.5.0 read as 1 -> 1)."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR,
        "mytool/__init__.py": '__version__: T1 = "0.4.0"\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)
    patch = _PATCH_DYNAMIC_SOURCE_ONLY + (
        "diff --git a/mytool/__init__.py b/mytool/__init__.py\n"
        "--- a/mytool/__init__.py\n+++ b/mytool/__init__.py\n"
        '@@ -1 +1 @@\n-__version__: T1 = "0.4.0"\n+__version__: T1 = "0.5.0"\n'
    )

    r = _run_ship_vbump(main, bindir, name_only="mytool/cli.py\nmytool/__init__.py", patch=patch)

    assert r.returncode == 0, f"an annotated moved __version__ must pass\n{r.stdout}\n{r.stderr}"
    assert "mytool/__init__.py version is bumped" in r.stdout, r.stdout


def test_dynamic_attr_renamed_version_module_bump_passes(tmp_path):
    """The PR moves the version module (mytool/__init__.py -> mytool/_version.py) and bumps it:
    git shows `diff --git a/<old> b/<new>`, so the file's diff section is keyed by its new path
    (review finding: it used to be found only when both sides named the same path)."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR.replace("mytool.__version__", "mytool._version.__version__"),
        "mytool/_version.py": '__version__ = "0.4.0"\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)
    patch = _PATCH_DYNAMIC_SOURCE_ONLY + (
        "diff --git a/mytool/__init__.py b/mytool/_version.py\n"
        "similarity index 60%\nrename from mytool/__init__.py\nrename to mytool/_version.py\n"
        "--- a/mytool/__init__.py\n+++ b/mytool/_version.py\n"
        '@@ -1 +1 @@\n-__version__ = "0.4.0"\n+__version__ = "0.5.0"\n'
    )

    r = _run_ship_vbump(main, bindir, name_only="mytool/cli.py\nmytool/_version.py", patch=patch)

    assert r.returncode == 0, f"a moved-and-bumped module must pass\n{r.stdout}\n{r.stderr}"
    assert "mytool/_version.py version is bumped" in r.stdout, r.stdout


def test_version_section_is_not_spoofed_by_a_decoy_path(repo_with_dynamic_attr_version, tmp_path):
    """A PR-added `weird b/mytool/__init__.py` gets the header `diff --git a/weird b/mytool/
    __init__.py b/weird b/mytool/__init__.py` (git does not quote a space), which ends in
    ` b/mytool/__init__.py`. Its hunk must not count as a bump of the real module (review
    finding against a header-suffix match); git writes that file's `+++` line with a TAB."""
    main = repo_with_dynamic_attr_version
    bindir = _fake_gh_vbump_dir(tmp_path)
    decoy = "weird b/mytool/__init__.py"
    patch = _PATCH_DYNAMIC_SOURCE_ONLY + (
        f"diff --git a/{decoy} b/{decoy}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{decoy}\t\n"
        '@@ -0,0 +1 @@\n+__version__ = "0.5.0"\n'
    )

    r = _run_ship_vbump(main, bindir, name_only=f"mytool/cli.py\n{decoy}", patch=patch)

    assert r.returncode != 0, f"a decoy path must not satisfy the gate\n{r.stdout}\n{r.stderr}"
    assert "version in mytool/__init__.py is UNCHANGED" in r.stderr, r.stderr


def test_unreadable_dynamic_pyproject_falls_back_to_package_json(tmp_path):
    """A polyglot repo: the pyproject's dynamic source is unreadable (a hatch path that is not
    on disk), but package.json declares the version. Auto-detection checks package.json, as
    it did before dynamic versions were read (review finding), instead of refusing outright."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _pyproject_dynamic('[tool.hatch.version]\npath = "m/_v.py"\n'),
        "package.json": '{\n  "name": "mytool",\n  "version": "1.0.0"\n}\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)

    r = _run_ship_vbump(main, bindir, name_only="src/a.js\npackage.json", patch=_PATCH_PKGJSON_BUMP)

    assert r.returncode == 0, f"package.json's bump must count\n{r.stdout}\n{r.stderr}"
    assert "package.json version is bumped" in r.stdout, r.stdout


def test_dynamic_version_unreadable_source_refuses(tmp_path):
    """A declared attr whose module is outside the probed layouts (here apps/mytool/) used to
    fall back to pyproject's own `version = { attr = ... }` line, so renaming the attr to
    `mytool2.__version__` read as a bump to `2.__version__` (review finding). Ship must refuse
    and point at SHIP_VERSION_FILES instead of reading that line."""
    main = _repo_with_files(tmp_path, {
        "README.md": "# x\n",
        "pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR,
        "apps/mytool/__init__.py": '__version__ = "0.4.0"\n',
    })
    bindir = _fake_gh_vbump_dir(tmp_path)
    patch = _PATCH_DYNAMIC_SOURCE_ONLY + (
        "diff --git a/pyproject.toml b/pyproject.toml\n"
        "--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        '@@ -6 +6 @@\n-version = { attr = "mytool.__version__" }\n'
        '+version = { attr = "mytool2.__version__" }\n'
    )

    for env_extra in (None, {"SHIP_VERSION_FILES": "pyproject.toml"}):
        r = _run_ship_vbump(
            main, bindir, name_only="mytool/cli.py\npyproject.toml", patch=patch,
            env_extra=env_extra,
        )

        assert r.returncode != 0, f"{env_extra}: must refuse\n{r.stdout}\n{r.stderr}"
        assert "version-bump gate OK" not in r.stdout, f"{env_extra}: attr churn read as a bump\n{r.stdout}"
        assert 'attr "mytool.__version__"' in r.stderr, r.stderr
        assert "SHIP_VERSION_FILES" in r.stderr, r.stderr
        assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_base_tip_version_value_is_empty_not_fatal_without_a_version_line(tmp_path):
    """The base tip has no such file yet (a PR adding its first version module) or no version
    value in it: base_tip_version_value must print nothing and succeed. Under ship.sh's
    `set -euo pipefail` a failing pipeline there killed ship silently with exit 1."""
    main = _repo_with_files(tmp_path, {"README.md": "# x\n", "mytool/cli.py": "x = 1\n"})
    ctx = Ctx(str(_SHIP_DIR))
    ctx.root = str(main)
    ctx.pr = "1"
    ctx.default_branch = "main"
    ctx.gh = lambda *a, **k: Result(0, "main\n")

    missing = version_shot.base_tip_version_value(ctx, "mytool/__init__.py")
    no_version = version_shot.base_tip_version_value(ctx, "mytool/cli.py")

    assert (missing, no_version) == ("", "")


def _pyproject_dynamic(tool_table: str) -> str:
    return '[project]\nname = "m"\ndynamic = ["version"]\n\n' + tool_table


@pytest.mark.parametrize(
    ("pyproject_rel", "files", "expected_rc", "expected_out"),
    [
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": '[project]\nname = "m"\ndynamic = [\n  "readme",\n  \'version\',\n]\n\n'
                                  "[tool.setuptools.dynamic]\nversion = {attr = 'm.sub.__version__'}\n",
                "src/m/sub.py": '__version__ = "1.0.0"\n',
            },
            0, "src/m/sub.py",
            id="multiline-dynamic-single-quotes-src-module",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR.replace("\n", "\r\n"),
                "mytool/__init__.py": '__version__ = "1.0.0"\n',
            },
            0, "mytool/__init__.py",
            id="crlf",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[tool.setuptools.dynamic.version]\nattr = "m.__version__"\n'),
                "m/__init__.py": '__version__ = "1.0.0"\n',
            },
            0, "m/__init__.py",
            id="setuptools-version-subtable",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[tool.setuptools.dynamic]\nversion.attr = "m.__version__"\n'),
                "m/__init__.py": '__version__ = "1.0.0"\n',
            },
            0, "m/__init__.py",
            id="setuptools-dotted-version-attr",
        ),
        pytest.param(
            "pkg/pyproject.toml",
            {
                "pkg/pyproject.toml": _pyproject_dynamic('[tool.hatch.version]\npath = "./m/_version.py"\n'),
                "pkg/m/_version.py": '__version__ = "1.0.0"\n',
            },
            0, "pkg/m/_version.py",
            id="nested-pyproject-hatch-path",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": '[project]\nname = "m"\nversion = "1.0.0"\n\n'
                                  '[tool.hatch.version]\npath = "m/__init__.py"\n',
                "m/__init__.py": '__version__ = "1.0.0"\n',
            },
            1, "",
            id="static-version-wins",
        ),
        pytest.param(
            "pyproject.toml",
            {"pyproject.toml": _pyproject_dynamic('[tool.hatch.version]\nsource = "vcs"\n')},
            1, "",
            id="vcs-version-has-no-file",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[tool.setuptools.dynamic]\nversion = { attr = "m.VERSION" }\n'),
                "m/__init__.py": 'VERSION = "1.0.0"\n',
            },
            2, 'pyproject.toml: attr "m.VERSION" (ship reads only __version__)',
            id="attribute-other-than-dunder-version",
        ),
        pytest.param(
            "pyproject.toml",
            {"pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR},
            2, 'pyproject.toml: attr "mytool.__version__" (module not found)',
            id="module-not-on-disk",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[tool.setuptools.dynamic]\nversion = { attr = "m.__version__" }\n'),
                "m/__init__.py": '__version_info__ = (1, 2, 4)\n__version__ = ".".join(map(str, __version_info__))\n',
            },
            2, 'pyproject.toml: attr "m.__version__" (no literal __version__ = "X" in m/__init__.py)',
            id="computed-dunder-version",
        ),
        pytest.param(
            "pyproject.toml",
            {"pyproject.toml": _pyproject_dynamic('[tool.hatch.version]\npath = "m/_v.py"\n')},
            2, 'pyproject.toml: path "m/_v.py" (not a regular file inside the repo)',
            id="hatch-path-not-on-disk",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[tool.setuptools.dynamic]\nversion = { file = "VERSION" }\n'),
                "VERSION": "1.0.0\n",
            },
            2, "pyproject.toml: [tool.setuptools.dynamic] version (not an attr)",
            id="setuptools-file-source",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[tool.hatch.version]\npath = "VERSION.txt"\n'),
                "VERSION.txt": '__version__ = "1.0.0"\n',
            },
            2, 'pyproject.toml: path "VERSION.txt" (not a .py module)',
            id="hatch-path-not-a-py-module",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _PYPROJECT_SETUPTOOLS_ATTR,
                "mytool/__init__.py": '__version__: T1 = "1.0.0"\n',
            },
            0, "mytool/__init__.py",
            id="annotated-dunder-version",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": '[project]\nname = "m"\ndynamic = [\n'
                                  "    # see docs: version[1] mirrors the changelog tag\n"
                                  '    "version",\n]\n\n'
                                  '[tool.setuptools.dynamic]\nversion = { attr = "m.__version__" }\n',
                "m/__init__.py": '__version__ = "1.0.0"\n',
            },
            0, "m/__init__.py",
            id="comment-with-bracket-inside-dynamic-array",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic('[ tool.hatch.version ]  # spaced header\npath = "m/_v.py"\n'),
                "m/_v.py": '__version__ = "1.0.0"\n',
            },
            0, "m/_v.py",
            id="spaced-section-header",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic(
                    '[tool.hatch.version]\npath = "m/_v.py"\npattern = "VERSION = \'(?P<version>[^\']+)\'"\n'
                ),
                "m/_v.py": '__version__ = "0.0.1"\nVERSION = \'1.0.0\'\n',
            },
            2, 'pyproject.toml: path "m/_v.py" (custom hatch pattern)',
            id="hatch-custom-pattern",
        ),
        pytest.param(
            "pyproject.toml",
            {
                "pyproject.toml": _pyproject_dynamic(
                    '[tool.setuptools.dynamic]\nversion = { attr = "m.__version__" }\n\n'
                    '[tool.hatch.version]\npath = "m/_v.py"\n'
                ),
                "m/__init__.py": '__version__ = "1.0.0"\n',
                "m/_v.py": '__version__ = "2.0.0"\n',
            },
            2, "pyproject.toml: both a setuptools attr and a hatch path (ambiguous)",
            id="setuptools-attr-and-hatch-path",
        ),
    ],
)
def test_dynamic_version_file_resolution(tmp_path, pyproject_rel, files, expected_rc, expected_out):
    """dynamic_version_file maps a pyproject's dynamic version to the module holding it (rc 0),
    resolves nothing for a static or VCS-derived version (rc 1), and names a declared source it
    cannot read (rc 2) so the gate refuses instead of reading the declaration line itself."""
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(text.encode("utf-8"))

    rc, out = _run_dynamic_version_file(tmp_path, pyproject_rel)

    assert rc == expected_rc, f"rc {rc}, stdout {out!r}"
    assert out == expected_out, f"{out!r} != {expected_out!r}"


def _run_dynamic_version_file(root: Path, pyproject_rel: str):
    """(rc, text) from version_shot.dynamic_version_file for a repo rooted at `root`."""
    ctx = Ctx(str(_SHIP_DIR))
    ctx.root = str(root)
    return version_shot.dynamic_version_file(ctx, pyproject_rel)


@pytest.mark.parametrize("case", ["dotdot", "absolute", "file-symlink", "dir-symlink", "attr-package-symlink"])
def test_dynamic_version_file_rejects_sources_outside_the_repo(tmp_path, case):
    """The version source comes from the PR's own pyproject.toml, so a hatch `path` (or a
    symlink in the PR) must not lead ship to stat or read files outside the checkout: every
    escape is refused with the same reason, whether or not the outside file exists."""
    root = tmp_path / "repo"
    root.mkdir()
    secret = tmp_path / "secret" / "_version.py"
    secret.parent.mkdir()
    secret.write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (tmp_path / "secret" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    try:
        if case == "file-symlink":
            (root / "m").mkdir()
            os.symlink(secret, root / "m" / "_version.py")
        elif case == "dir-symlink":
            os.symlink(secret.parent, root / "m", target_is_directory=True)
        elif case == "attr-package-symlink":
            os.symlink(secret.parent, root / "pkg", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    value = {
        "dotdot": "../secret/_version.py",
        "absolute": str(secret),
    }.get(case, "m/_version.py")
    table = (
        '[tool.setuptools.dynamic]\nversion = { attr = "pkg.__version__" }\n'
        if case == "attr-package-symlink"
        else f'[tool.hatch.version]\npath = "{value}"\n'
    )
    (root / "pyproject.toml").write_text(_pyproject_dynamic(table), encoding="utf-8")

    rc, out = _run_dynamic_version_file(root, "pyproject.toml")

    reason = "module not found" if case == "attr-package-symlink" else "not a regular file inside the repo"
    assert rc == 2, f"rc {rc}, stdout {out!r}"
    assert out.endswith(f"({reason})"), out


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
#   SHIP_TEST_DIFF       — inject custom diff text for the leftover-marker check. Pass via
#                          env_extra, not the ambient shell: _run_ship_cidown scrubs any
#                          inherited SHIP_TEST_DIFF/SHIP_TEST_DIFF_FILE first, then
#                          transparently spills a non-empty SHIP_TEST_DIFF from env_extra to
#                          a file and sets SHIP_TEST_DIFF_FILE instead (read by the fake
#                          `gh` scripts below) — a raw diff string left in the environment
#                          can trip Linux execve()'s per-argument/per-env-string limit
#                          (MAX_ARG_STRLEN, 128 KiB) for large fixtures (e.g. a whole
#                          source file).
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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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
        elif [ -n "${SHIP_TEST_DIFF_FILE:-}" ]; then
          [ -r "$SHIP_TEST_DIFF_FILE" ] || { echo "fake gh: SHIP_TEST_DIFF_FILE not readable: $SHIP_TEST_DIFF_FILE" >&2; exit 1; }
          cat < "$SHIP_TEST_DIFF_FILE"
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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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
        elif printf '%s ' "$@" | grep -q -- '--json reviews'; then
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
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
        elif [ -n "${SHIP_TEST_DIFF_FILE:-}" ]; then
          [ -r "$SHIP_TEST_DIFF_FILE" ] || { echo "fake gh: SHIP_TEST_DIFF_FILE not readable: $SHIP_TEST_DIFF_FILE" >&2; exit 1; }
          cat < "$SHIP_TEST_DIFF_FILE"
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


def _run_ship_cidown(main: Path, bindir: Path, env_extra: dict | None = None, extra_args: tuple[str, ...] = ()):
    """Run ship.sh without --skip-ci (CI gate fires) but with --no-screenshot-ok."""
    env = dict(os.environ)
    # Deterministic target inference: clear any ambient GH_REPO/GH_SHIP_REPO the dev/CI shell
    # may carry, so a test only sees a foreign target when it sets one explicitly via env_extra.
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
    # Never inherit a stale SHIP_TEST_DIFF / SHIP_TEST_DIFF_FILE from the ambient shell —
    # the fake `gh` scripts prefer the file over the inline var, so a leftover value would
    # silently override (or, if a stale file no longer exists, break) a test that sets
    # neither. A test that wants either passes it explicitly via env_extra below.
    env.pop("SHIP_TEST_DIFF", None)
    env.pop("SHIP_TEST_DIFF_FILE", None)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    if env_extra:
        env.update(env_extra)
    # No caller passes both today (env_extra is dict[str, str] and diff fixtures are the
    # ordinary source-text this fixture set always contains); fail loudly rather than
    # silently pick a precedence if that ever changes, instead of guessing.
    assert not (env.get("SHIP_TEST_DIFF") and env.get("SHIP_TEST_DIFF_FILE")), (
        "_run_ship_cidown: env_extra set both SHIP_TEST_DIFF and SHIP_TEST_DIFF_FILE — "
        "pick one (SHIP_TEST_DIFF is auto-spilled to a file for you)"
    )
    # Bash's `${SHIP_TEST_DIFF:-default}` (used by the fake `gh` scripts) falls through to
    # the default on both unset AND empty — preserve that by only spilling a non-empty value.
    diff_value = env.get("SHIP_TEST_DIFF")
    if diff_value:
        # Always spill through a file rather than a raw env string: Linux execve() rejects
        # any single argv/envp string longer than MAX_ARG_STRLEN (128 KiB, kernel constant),
        # independent of the overall ARG_MAX budget — a large fixture (e.g. this file's own
        # ~427 KB source, prefixed with ci/ship/ship.sh) blows straight past that on GitHub's
        # ubuntu-latest runners even though it silently "worked" locally (macOS has no
        # equivalent per-string cap). Spilling unconditionally — not just above a size
        # threshold — keeps exactly one code path so a small and a large fixture can never
        # diverge, and exercises the file-read branch of the fake `gh` scripts on every
        # platform and every cidown test that sets SHIP_TEST_DIFF at all (small or large),
        # not only the rare oversized one.
        # Match printf '%s\n' (what direct-env consumption used) so file-backed output is
        # byte-identical to the old inline path for any consumer that's last-line sensitive.
        # Plain strict UTF-8 (not surrogateescape): every fixture in this file is ordinary
        # ASCII/UTF-8 source text, and `_sh` decodes ship.sh's stdout/stderr with strict
        # UTF-8 too (see _sh above) — matching that keeps decode behavior consistent
        # end-to-end instead of tolerating bytes on write that would crash on readback.
        # Written into bindir (already the harness's own per-test scratch dir — unique per
        # pytest invocation via `tmp_path`, and never the `main` checkout ship.sh operates
        # on) so it can never surface as an untracked file or collide across parallel runs.
        if "\x00" in diff_value:
            # subprocess (the old direct-env path) rejects an embedded NUL in an env value
            # with ValueError: embedded null byte — a file write wouldn't, but bash command
            # substitution in ship.sh silently drops NULs, so a NUL fixture would silently
            # change meaning instead of failing loudly. Match the old failure mode.
            raise ValueError("SHIP_TEST_DIFF fixture contains an embedded NUL byte")
        diff_file = bindir / "ship_test_diff.txt"
        diff_file.write_text(diff_value + "\n", encoding="utf-8")
        del env["SHIP_TEST_DIFF"]
        env["SHIP_TEST_DIFF_FILE"] = str(diff_file)
    return _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
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


# Fake gh proving the REAL (non-escape-hatch) billing-block annotation detection path:
# no SHIP_TEST_CI_DOWN here — statusCheckRollup shows two real FAILED checks, and `gh api`
# answers the two calls _ci_billing_block_annotation_detected actually makes (list check-runs
# for the head SHA, then fetch annotations for each failed one) with GitHub's own fixed
# billing-block message. Confirmed live 2026-09-09 (sme-archiving-gc PR #401): the run-level
# timing heuristic missed this shape (run queued 2m33s before rejecting a job that itself ran
# for 2s), so this fixture deliberately leaves the actions/runs endpoint on the generic
# `api) echo 0` fallback (making _ci_runs_timing_suspicious report "0" via its own JSON-parse
# failure path) — only the annotation signal must carry detection here.
_FAKE_GH_BILLING_BLOCK = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefOid; then
          echo "deadbeef0000000000000000000000000000cafe"
        elif printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '[{"name":"pytest","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"codeql","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"}]'
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
        elif [ -n "${SHIP_TEST_DIFF_FILE:-}" ]; then
          [ -r "$SHIP_TEST_DIFF_FILE" ] || { echo "fake gh: SHIP_TEST_DIFF_FILE not readable: $SHIP_TEST_DIFF_FILE" >&2; exit 1; }
          cat < "$SHIP_TEST_DIFF_FILE"
        else
          printf '%s\\n' "${SHIP_TEST_DIFF:-+new line without markers}"
        fi ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    if printf '%s ' "$@" | grep -q 'check-runs?per_page'; then
      printf '{"check_runs":[{"id":111,"conclusion":"failure"},{"id":222,"conclusion":"failure"}]}'
    elif printf '%s ' "$@" | grep -q annotations; then
      printf '[{"message":"The job was not started because recent account payments have failed or your spending limit needs to be increased."}]'
    else
      echo 0
    fi ;;
  *) : ;;
esac
"""


def _fake_gh_billing_block_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "binbb"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_BILLING_BLOCK, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def test_billing_block_annotation_detected_without_escape_hatch(repo_with_pr_worktree, tmp_path):
    """Real (non-SHIP_TEST_CI_DOWN) path: two checks FAILED, run-level timing is NOT
    suspicious (actions/runs falls through the fake's generic fallback), but the check-run
    annotations carry GitHub's fixed billing-block message. _ci_billing_block_annotation_detected
    must catch this and let ci_appears_structurally_down fall through to the local gate, same as
    the timing/status signals do. SHIP_CI_STATUS_URL points at a closed local port so the
    real network-touching status probe fails fast and deterministically instead of depending on
    sandbox DNS/network behavior."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_billing_block_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_LOCAL_TEST_CMD": "true",  # trivially-passing stand-in for the test suite
        "SHIP_REVIEW_DWELL": "0",  # disable dwell gate: fake PR has no review timestamps
        "SHIP_CI_STATUS_URL": "http://127.0.0.1:1/unreachable",
    })

    assert r.returncode == 0, (
        f"ship must succeed once the billing-block annotation signal fires\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    # Proves the annotation signal specifically fired, not the timing/status ones.
    assert "billing_block=1" in r.stderr, r.stderr
    assert "timing_suspicious=0" in r.stderr, r.stderr
    assert "CI infrastructure appears structurally unavailable" in r.stderr, r.stderr
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


def test_merge_message_flags_reach_gh_pr_merge(repo_with_pr_worktree, tmp_path):
    """--subject/--body/--author-email reach `gh pr merge` verbatim, so a squash can carry a
    chosen message instead of every commit message; a body may start with "-"."""
    main, _wt = repo_with_pr_worktree
    _add_workflow(main, "pull_request")
    bindir = _fake_gh_empty_rollup_dir(tmp_path)
    merge_log = tmp_path / "merge-args.log"
    r = _run_ship_cidown(main, bindir, {
        "SHIP_CI_GRACE": "0",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_MERGE_LOG": str(merge_log),
    }, ("--subject", "fix: clean subject", "--body", "- clean body", "--author-email", "a@users.noreply.github.com"))
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    logged = merge_log.read_text(encoding="utf-8")
    assert ("--squash --subject fix: clean subject --body - clean body "
            "--author-email a@users.noreply.github.com") in logged, logged


def test_merge_message_flag_without_value_refuses(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_empty_rollup_dir(tmp_path)
    r = _run_ship_cidown(main, bindir, extra_args=("--subject",))
    assert r.returncode != 0
    assert "--subject needs a non-empty value" in r.stderr, r.stderr


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
        elif printf '%s ' "$@" | grep -q -- '--json reviews'; then
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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


def _fake_npm_logging_cwd(bindir: Path, log_path: Path) -> None:
    """Write a fake `npm` on bindir's PATH that appends "<args>\\n<cwd>" to log_path,
    so a test can assert both WHAT ran and WHERE it ran from."""
    npm = bindir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        f"{{ printf '%s\\n' \"$*\"; pwd; }} >> \"{log_path}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)


def test_cidown_local_gate_autodetects_e2e_subdir_when_root_has_no_manifest(
    repo_with_pr_worktree, tmp_path
):
    """Root has no pyproject.toml/package.json/Cargo.toml, but e2e/package.json does (the
    hyper-ext-e2e shape, #309): the local gate must auto-detect it and run there instead of
    failing closed with 'no recognized test runner found'."""
    main, _wt = repo_with_pr_worktree
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    _git("add", "e2e/package.json", cwd=main)
    _git("commit", "-qm", "add e2e package.json", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must auto-detect the e2e/ subdir test runner\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    # Exactly one invocation (args, cwd) -- npm must not have ALSO run a second time
    # anywhere else (e.g. a future root+subdir double-run bug).
    assert lines == ["test", str((main / "e2e").resolve())], lines
    assert "no recognized test runner found" not in r.stderr
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_dir_and_cmd_wins_over_autodetect(
    repo_with_pr_worktree, tmp_path
):
    """.ship-config declaring both SHIP_LOCAL_TEST_DIR and SHIP_LOCAL_TEST_CMD must run
    exactly that command from that directory, even when e2e/ also has a package.json that
    auto-detection would otherwise guess (proves the config file outranks auto-detect)."""
    main, _wt = repo_with_pr_worktree
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    marker = tmp_path / "custom-cmd.log"
    (main / ".ship-config").write_text(
        "# audited local-test override for hyper-ext-e2e-shaped repos\n"
        "SHIP_LOCAL_TEST_DIR=e2e\n"
        f"SHIP_LOCAL_TEST_CMD=echo custom-ran >> {marker}\n",
        encoding="utf-8",
    )
    _git("add", "e2e/package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add e2e package.json and .ship-config", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must run the .ship-config command\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert marker.read_text(encoding="utf-8").strip() == "custom-ran"
    assert ".ship-config: running in e2e" in r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_cmd_only_runs_from_root(repo_with_pr_worktree, tmp_path):
    """.ship-config with only SHIP_LOCAL_TEST_CMD (no dir) runs from the repo root."""
    main, _wt = repo_with_pr_worktree
    marker = tmp_path / "root-cmd.log"
    (main / ".ship-config").write_text(
        f"SHIP_LOCAL_TEST_CMD=pwd >> {marker}\n",
        encoding="utf-8",
    )
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "add root-only .ship-config", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must run the root-scoped .ship-config command\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert marker.read_text(encoding="utf-8").strip() == str(main.resolve())
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_env_override_still_wins_over_ship_config(
    repo_with_pr_worktree, tmp_path
):
    """Regression guard: the pre-existing SHIP_LOCAL_TEST_CMD env var (test-only escape
    hatch) must still take priority over a committed .ship-config file."""
    main, _wt = repo_with_pr_worktree
    marker = tmp_path / "should-not-run.log"
    (main / ".ship-config").write_text(
        f"SHIP_LOCAL_TEST_CMD=echo from-config >> {marker}\n",
        encoding="utf-8",
    )
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config that must be overridden", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must succeed via the env override\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not marker.exists(), "the .ship-config command must NOT have run"
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_malformed_ship_config_falls_through_to_autodetect(
    repo_with_pr_worktree, tmp_path
):
    """A present .ship-config with neither recognized key set (just a comment) is ignored;
    the gate falls through to normal auto-detection instead of erroring or blocking."""
    main, _wt = repo_with_pr_worktree
    (main / ".ship-config").write_text(
        "# no recognized keys here, just a comment\n",
        encoding="utf-8",
    )
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "add malformed .ship-config plus root package.json", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "test"
    assert lines[1] == str(main.resolve())
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_root_exit_code_2_blocks_not_falls_through(
    repo_with_pr_worktree, tmp_path
):
    """Regression for the exit-code-2 sentinel collision: a root suite that legitimately
    exits 2 (e.g. a pytest usage error) must NOT be misread as 'no manifest at root' and
    silently fall through to a DIFFERENT, passing suite in e2e/ -- that would merge a PR
    whose real root suite failed."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    _git("add", "package.json", "e2e/package.json", cwd=main)
    _git("commit", "-qm", "add root and e2e package.json", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    root_resolved = str(main.resolve())
    npm = bindir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        f"{{ printf '%s\\n' \"$*\"; pwd; }} >> \"{npm_log}\"\n"
        f"if [ \"$(pwd)\" = \"{root_resolved}\" ]; then exit 2; else exit 0; fi\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        "ship must block on the root suite's real exit-2 failure, not fall through to e2e/\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", root_resolved], lines  # npm ran exactly once, at root
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_root_manifest_wins_over_subdir_manifest(
    repo_with_pr_worktree, tmp_path
):
    """When both root AND e2e/ have a manifest, the root one wins (priority 4 before 5) --
    the subdir probe never even runs."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    _git("add", "package.json", "e2e/package.json", cwd=main)
    _git("commit", "-qm", "add root and e2e package.json", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must succeed via the root manifest\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_subdir_only_e2e_used_when_test_and_tests_also_present(
    repo_with_pr_worktree, tmp_path
):
    """When e2e/, test/, and tests/ ALL have a manifest, only e2e/ is ever used -- test/ and
    tests/ are not fallback candidates at all (narrowed scope, see the priority-5 comment in
    _local_test_runner), they simply happen to be irrelevant here since e2e/ already matches."""
    main, _wt = repo_with_pr_worktree
    for sub in ("e2e", "test", "tests"):
        (main / sub).mkdir()
        (main / sub / "package.json").write_text(
            f'{{"scripts":{{"test":"{sub}-suite"}}}}\n', encoding="utf-8"
        )
    _git("add", "e2e/package.json", "test/package.json", "tests/package.json", cwd=main)
    _git("commit", "-qm", "add three candidate subdirs", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must succeed via e2e/\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str((main / "e2e").resolve())], lines
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_wins_over_rig_yaml(repo_with_pr_worktree, tmp_path):
    """.ship-config outranks rig.yaml + `dev` too, not just auto-detect -- it exists to
    correct EITHER heuristic guessing wrong."""
    main, _wt = repo_with_pr_worktree
    (main / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    marker = tmp_path / "config-ran.log"
    (main / ".ship-config").write_text(
        f"SHIP_LOCAL_TEST_CMD=echo config-ran >> {marker}\n", encoding="utf-8"
    )
    _git("add", "rig.yaml", ".ship-config", cwd=main)
    _git("commit", "-qm", "add rig.yaml and .ship-config", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    dev_log = tmp_path / "dev.log"
    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = '--agenttools-dev-probe' ]; then exit 0; fi\n"
        "if [ \"$1\" = 'has-script' ]; then exit 0; fi\n"
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
        f"ship must run the .ship-config command\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert marker.read_text(encoding="utf-8").strip() == "config-ran"
    assert not dev_log.exists(), "dev must not have run -- .ship-config outranks rig.yaml"
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_dir_only_fails_closed_when_no_manifest(
    repo_with_pr_worktree, tmp_path
):
    """.ship-config scopes auto-detect to a real directory that has none of the three
    manifests -- fail closed with a clear message, no silent fallback elsewhere."""
    main, _wt = repo_with_pr_worktree
    (main / "empty-dir").mkdir()
    (main / "empty-dir" / "README.md").write_text("no tests here\n", encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=empty-dir\n", encoding="utf-8")
    _git("add", ".ship-config", "empty-dir/README.md", cwd=main)
    _git("commit", "-qm", "add .ship-config pointing at a dir with no manifest", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "no recognized test runner found in empty-dir" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_dir_nonexistent_fails_closed(
    repo_with_pr_worktree, tmp_path
):
    """.ship-config pointing SHIP_LOCAL_TEST_DIR at a directory that doesn't exist at all
    fails closed with a distinct, clear message (not a generic test failure)."""
    main, _wt = repo_with_pr_worktree
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=does-not-exist\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config pointing at a nonexistent dir", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "does not exist under" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_rejects_unsafe_dir_absolute(
    repo_with_pr_worktree, tmp_path
):
    """An absolute SHIP_LOCAL_TEST_DIR is rejected (logged) and ignored -- falls through to
    root auto-detect rather than scoping the gate outside the repo."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=/etc\n", encoding="utf-8")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "add .ship-config with an absolute (unsafe) dir", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"an unsafe dir must be ignored, falling through to root auto-detect\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not a safe repo-relative subdirectory" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_ship_config_rejects_unsafe_dir_dotdot(
    repo_with_pr_worktree, tmp_path
):
    """A SHIP_LOCAL_TEST_DIR containing a `..` component is rejected the same way."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=../escape\n", encoding="utf-8")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "add .ship-config with a traversal (unsafe) dir", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"an unsafe dir must be ignored, falling through to root auto-detect\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not a safe repo-relative subdirectory" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_subdir_pyproject_without_nested_tests_dir(
    repo_with_pr_worktree, tmp_path
):
    """A pyproject.toml manifest found in a non-root dir (subdir/`.ship-config`-scoped
    auto-detect) whose own directory has NO nested tests/ subdir must run bare `pytest -q`,
    not force a `tests/` path arg that would look for a nonexistent nested tests/tests/."""
    main, _wt = repo_with_pr_worktree
    (main / "e2e").mkdir()
    (main / "e2e" / "pyproject.toml").write_text("[project]\nname = 'e2e'\n", encoding="utf-8")
    _git("add", "e2e/pyproject.toml", cwd=main)
    _git("commit", "-qm", "add e2e pyproject.toml with no nested tests/ dir", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    uv_log = tmp_path / "uv.log"
    uv_fake = bindir / "uv"
    uv_fake.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> \"{uv_log}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv_fake.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must run pytest without a bogus tests/ arg\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert uv_log.read_text(encoding="utf-8").strip() == "run --with pytest pytest -q"
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_warns_on_unrecognized_line(
    repo_with_pr_worktree, tmp_path
):
    """A typo'd key (neither whitelisted key matches) is logged, not silently swallowed, and
    the gate falls through to auto-detection instead of treating the typo as a real config."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DR=e2e\n", encoding="utf-8")  # typo
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "add .ship-config with a typo'd key", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "ignoring unrecognized line" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_ship_config_trims_value_whitespace(repo_with_pr_worktree, tmp_path):
    """Whitespace directly around the `=` in a .ship-config value is trimmed, so
    `SHIP_LOCAL_TEST_DIR= e2e ` resolves to the directory `e2e`, not ` e2e `."""
    main, _wt = repo_with_pr_worktree
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR= e2e \n", encoding="utf-8")
    _git("add", ".ship-config", "e2e/package.json", cwd=main)
    _git("commit", "-qm", "add .ship-config with padded whitespace around the value", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"whitespace around the config value must be trimmed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str((main / "e2e").resolve())], lines


def test_cidown_local_gate_ship_config_cmd_exit_code_2_blocks_not_falls_through(
    repo_with_pr_worktree, tmp_path
):
    """Regression for the _ship_config_run exit-code-2 sentinel collision: a
    SHIP_LOCAL_TEST_CMD that legitimately exits 2 must NOT be misread as 'no config to act
    on' and silently fall through to a DIFFERENT, passing heuristic (root auto-detect here)
    -- the audited, explicitly-configured command's own failure must block the merge."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_CMD=exit 2\n", encoding="utf-8")
    _git("add", "package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config whose command exits 2, plus a root manifest", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        "ship must block on the .ship-config command's real exit-2 failure, not fall "
        f"through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not npm_log.exists(), "npm (root auto-detect) must never have run"
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_e2e_symlink_escape_is_rejected(repo_with_pr_worktree, tmp_path):
    """A symlinked e2e/ pointing OUTSIDE the repo must not be treated as a valid e2e/
    candidate -- the physical-descendant check catches what a lexical check on the
    configured/discovered name alone can't."""
    main, _wt = repo_with_pr_worktree
    outside = tmp_path / "outside-repo"
    outside.mkdir()
    (outside / "package.json").write_text('{"scripts":{"test":"escape-suite"}}\n', encoding="utf-8")
    (main / "e2e").symlink_to(outside)
    _git("add", "e2e", cwd=main)
    _git("commit", "-qm", "add e2e as a symlink escaping the repo", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed, not follow the symlink outside the repo\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not npm_log.exists(), "npm must never have run in the symlink target"
    assert "e2e/ resolves (via a symlink) outside the repo root" in r.stderr, (
        "the e2e/ symlink-escape skip must log a specific diagnostic (matching the "
        f".ship-config SHIP_LOCAL_TEST_DIR path), not silently fall through to the generic "
        f"'no recognized test runner found' message\nSTDERR:\n{r.stderr}"
    )


def test_cidown_local_gate_ship_config_dir_symlink_escape_is_rejected(
    repo_with_pr_worktree, tmp_path
):
    """A .ship-config SHIP_LOCAL_TEST_DIR that is lexically safe (no '..', not absolute) but
    physically resolves outside the repo via a symlink must be rejected."""
    main, _wt = repo_with_pr_worktree
    outside = tmp_path / "outside-repo2"
    outside.mkdir()
    (outside / "package.json").write_text('{"scripts":{"test":"escape-suite"}}\n', encoding="utf-8")
    (main / "escape-link").symlink_to(outside)
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=escape-link\n", encoding="utf-8")
    _git("add", "escape-link", ".ship-config", cwd=main)
    _git("commit", "-qm", "add a symlinked SHIP_LOCAL_TEST_DIR escaping the repo", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "resolves (via a symlink) outside the repo root" in r.stderr, r.stderr
    assert not npm_log.exists()


def test_cidown_local_gate_ship_config_dir_symlink_to_root_itself_is_rejected(
    repo_with_pr_worktree, tmp_path
):
    """Regression: a SHIP_LOCAL_TEST_DIR that is lexically safe (a plain subdirectory name,
    not '.'  or './') but is ITSELF a symlink resolving back to the repo root must be
    rejected -- _dir_is_real_descendant_of_root requires a STRICT descendant, equal-to-root
    is not good enough (a prior version of this check wrongly accepted root-equivalence,
    which would have let this bypass the lexical '.' rejection entirely)."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / "suite").symlink_to(".", target_is_directory=True)
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=suite\n", encoding="utf-8")
    _git("add", "suite", "package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add a SHIP_LOCAL_TEST_DIR symlinked back to the repo root", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed, not accept a symlink-to-root as a scoped subdirectory\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not npm_log.exists(), "npm must never have run via the root-equivalent symlink"


def test_cidown_local_gate_ship_config_nul_byte_is_rejected(repo_with_pr_worktree, tmp_path):
    """A .ship-config committed with a NUL byte embedded in a key name is refused outright,
    not parsed -- defends against bash silently STRIPPING a NUL when the git-show output
    becomes a variable's value, which could turn a byte sequence that never spells a
    whitelisted key in the actual committed bytes into one that does."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_bytes(b"SHIP_LOCAL_TEST_C\x00MD=echo evil\n")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "add .ship-config with an embedded NUL byte", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect, ignoring the NUL-containing config\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "contains a NUL byte" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_ship_config_tree_is_rejected(repo_with_pr_worktree, tmp_path):
    """If HEAD:.ship-config is a TREE (someone committed a `.ship-config/` directory) rather
    than a blob, ship must reject it outright instead of feeding `git show`'s tree listing to
    the KEY=value parser -- a tree entry's NAME is a plain filename that can legally contain
    '=' and spaces, so a file literally named `SHIP_LOCAL_TEST_CMD=echo evil` inside that
    directory must never be parsed as committed file CONTENT."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    cfg_dir = main / ".ship-config"
    cfg_dir.mkdir()
    (cfg_dir / "SHIP_LOCAL_TEST_CMD=echo evil").write_text("", encoding="utf-8")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "commit .ship-config as a directory (tree), not a file", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect, ignoring the tree at .ship-config\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "is not a regular file" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], (
        "the tree's entry name must never reach the eval'd command path\n"
        f"npm.log: {lines}"
    )


def test_cidown_local_gate_ship_config_symlink_is_rejected(repo_with_pr_worktree, tmp_path):
    """If .ship-config is committed as a SYMLINK (git tree mode 120000), ship must reject it
    instead of parsing its blob content -- a symlink's blob content is the link TARGET
    STRING, not test-runner config, and `git cat-file -t` reports `blob` for a symlink just
    like it does for a regular file, so a type-only check can't tell them apart. Craft the
    symlink target so it would parse as a valid KEY=value line if it reached the parser."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").symlink_to("SHIP_LOCAL_TEST_CMD=echo evil")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "commit .ship-config as a symlink", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect, ignoring the symlinked .ship-config\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "is not a regular file" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], (
        "the symlink target string must never reach the eval'd command path\n"
        f"npm.log: {lines}"
    )


def test_cidown_local_gate_ship_config_empty_file_is_treated_as_absent(
    repo_with_pr_worktree, tmp_path
):
    """A genuinely empty (0-byte) but COMMITTED .ship-config must fall through to
    auto-detect quietly -- not be misreported as 'contains a NUL byte' (an empty file also
    fails the raw grep-based NUL check for an unrelated reason, so it needs its own guard)."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("", encoding="utf-8")
    _git("add", ".ship-config", "package.json", cwd=main)
    _git("commit", "-qm", "add an empty .ship-config", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "contains a NUL byte" not in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_ship_config_dir_only_wins_over_root_manifest(
    repo_with_pr_worktree, tmp_path
):
    """.ship-config's DIR-only scoping (no explicit CMD) must win over a ROOT manifest too,
    not just over rig.yaml/auto-detect in general -- a reordering bug that put root
    auto-detect ahead of the config-file scoping would pass every other precedence test but
    fail this one."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=e2e\n", encoding="utf-8")
    _git("add", "package.json", "e2e/package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add root+e2e package.json and a DIR-only .ship-config", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must scope to e2e/ per .ship-config\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str((main / "e2e").resolve())], lines


def test_cidown_local_gate_ship_config_unsafe_dir_invalidates_whole_file(
    repo_with_pr_worktree, tmp_path
):
    """An unsafe SHIP_LOCAL_TEST_DIR alongside a SHIP_LOCAL_TEST_CMD must invalidate the
    WHOLE file, not silently relocate the command to run from the repo root instead of the
    (rejected) directory the author asked for -- that would verify a different suite than
    intended. Falls through to normal root auto-detect instead."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    marker = tmp_path / "should-not-run.log"
    (main / ".ship-config").write_text(
        "SHIP_LOCAL_TEST_DIR=../escape\n"
        f"SHIP_LOCAL_TEST_CMD=echo config-ran >> {marker}\n",
        encoding="utf-8",
    )
    _git("add", "package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config with an unsafe dir plus a cmd", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not a safe repo-relative subdirectory" in r.stderr, r.stderr
    assert not marker.exists(), "the .ship-config command must NOT have run anywhere"
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_ship_config_dir_cmd_nonexistent_dir_fails_closed(
    repo_with_pr_worktree, tmp_path
):
    """The DIR+CMD branch gets the same clear "does not exist under" diagnostic as the
    DIR-only branch, instead of an opaque raw `cd` error."""
    main, _wt = repo_with_pr_worktree
    (main / ".ship-config").write_text(
        "SHIP_LOCAL_TEST_DIR=does-not-exist\n"
        "SHIP_LOCAL_TEST_CMD=true\n",
        encoding="utf-8",
    )
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config with a nonexistent dir plus a cmd", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "does not exist under" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_untracked_ship_config_is_ignored(repo_with_pr_worktree, tmp_path):
    """An untracked (not `git add`ed) .ship-config is NOT honored -- the "audited, committed"
    trust story is enforced (read from `git show HEAD:...`, not the working tree), not just
    documented. Falls through to root auto-detect."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    _git("add", "package.json", cwd=main)
    _git("commit", "-qm", "add root package.json", cwd=main)
    marker = tmp_path / "should-not-run.log"
    # Deliberately NOT git-added -- this file is untracked in the checkout ship.sh reads.
    (main / ".ship-config").write_text(
        f"SHIP_LOCAL_TEST_CMD=echo config-ran >> {marker}\n", encoding="utf-8"
    )

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not committed at HEAD" in r.stderr, r.stderr
    assert not marker.exists(), "the untracked .ship-config command must NOT have run"
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_ship_config_reads_committed_content_not_dirty_worktree(
    repo_with_pr_worktree, tmp_path
):
    """A TRACKED .ship-config that was subsequently modified in the working tree WITHOUT a
    new commit must still be honored per its last COMMITTED content (the original command),
    not the uncommitted edit -- proves the "audited" read goes through `git show HEAD:...`,
    not the mutable worktree file."""
    main, _wt = repo_with_pr_worktree
    committed_marker = tmp_path / "committed-ran.log"
    dirty_marker = tmp_path / "dirty-ran.log"
    (main / ".ship-config").write_text(
        f"SHIP_LOCAL_TEST_CMD=echo committed-ran >> {committed_marker}\n", encoding="utf-8"
    )
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config with the ORIGINAL command", cwd=main)
    # Locally modify the tracked file WITHOUT committing -- this must be ignored.
    (main / ".ship-config").write_text(
        f"SHIP_LOCAL_TEST_CMD=echo dirty-ran >> {dirty_marker}\n", encoding="utf-8"
    )

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must run the COMMITTED command\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert committed_marker.read_text(encoding="utf-8").strip() == "committed-ran"
    assert not dirty_marker.exists(), "the uncommitted (dirty) edit must NOT have run"


def test_cidown_local_gate_e2e_without_manifest_fails_closed(repo_with_pr_worktree, tmp_path):
    """e2e/ exists but has NO manifest -- _LOCAL_TEST_MATCHED stays 0 and the gate correctly
    fails closed instead of treating the mere existence of the directory as a pass. (Priority
    5 auto-detect is narrowed to e2e/ ONLY -- test/ and tests/ are deliberately never probed,
    see the priority-5 comment in _local_test_runner -- so there is no further candidate to
    fall through to.)"""
    main, _wt = repo_with_pr_worktree
    (main / "e2e").mkdir()
    (main / "e2e" / "README.md").write_text("not a test manifest\n", encoding="utf-8")
    _git("add", "e2e/README.md", cwd=main)
    _git("commit", "-qm", "add manifestless e2e/", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "no recognized test runner found" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_test_and_tests_subdirs_are_never_auto_probed(
    repo_with_pr_worktree, tmp_path
):
    """Regression pin for the narrowed priority-5 scope: test/ and tests/ manifests are
    NEVER auto-detected (only e2e/ is), even when they are the ONLY subdirectories present
    (no e2e/ at all) -- the gate must fail closed rather than guess a fixture-risk dir."""
    main, _wt = repo_with_pr_worktree
    (main / "test").mkdir()
    (main / "test" / "package.json").write_text('{"scripts":{"test":"test-suite"}}\n', encoding="utf-8")
    (main / "tests").mkdir()
    (main / "tests" / "package.json").write_text('{"scripts":{"test":"tests-suite"}}\n', encoding="utf-8")
    _git("add", "test/package.json", "tests/package.json", cwd=main)
    _git("commit", "-qm", "add test/ and tests/ manifests, no e2e/", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must fail closed -- test/ and tests/ must never be auto-probed\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert not npm_log.exists(), "npm must never have run in test/ or tests/"
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_dir_dotslashdot_is_rejected(repo_with_pr_worktree, tmp_path):
    """SHIP_LOCAL_TEST_DIR=./. (all path segments are '.') is rejected the same way plain
    '.' is -- the safety check is component-wise, not a literal-string special case, so a
    multi-segment all-dot path can't bypass the root-equivalence rejection."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=./.\n", encoding="utf-8")
    _git("add", "package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config with DIR=./. (must be rejected)", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not a safe repo-relative" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


def test_cidown_local_gate_root_pyproject_with_tests_dir_still_uses_tests_arg(
    repo_with_pr_worktree, tmp_path
):
    """Regression pin: a root pyproject.toml WITH a tests/ dir must still run the classic
    `pytest tests/ -q` (byte-for-byte pre-existing root behavior) -- proves the "root" mode
    parameter didn't accidentally loosen the primary production path to a bare `pytest -q`."""
    main, _wt = repo_with_pr_worktree
    (main / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (main / "tests").mkdir()
    (main / "tests" / "test_x.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    _git("add", "pyproject.toml", "tests/test_x.py", cwd=main)
    _git("commit", "-qm", "add root pyproject.toml with a tests/ dir", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    uv_log = tmp_path / "uv.log"
    uv_fake = bindir / "uv"
    uv_fake.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> \"{uv_log}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv_fake.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must succeed via the root pyproject.toml\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert uv_log.read_text(encoding="utf-8").strip() == "run --with pytest pytest tests/ -q"
    assert "merged #1" in r.stdout, r.stdout


def test_cidown_local_gate_subdir_suite_failure_blocks_merge(repo_with_pr_worktree, tmp_path):
    """A matched subdirectory manifest whose test command FAILS blocks the merge -- the
    subdir branch's own failure path, distinct from the root-exit-2 collision test."""
    main, _wt = repo_with_pr_worktree
    (main / "e2e").mkdir()
    (main / "e2e" / "package.json").write_text('{"scripts":{"test":"e2e-suite"}}\n', encoding="utf-8")
    _git("add", "e2e/package.json", cwd=main)
    _git("commit", "-qm", "add e2e package.json", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm = bindir / "npm"
    npm.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    npm.chmod(0o755)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode != 0, (
        f"ship must block on the e2e/ suite's failure\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_local_gate_ship_config_dir_with_dotdot_substring_is_not_rejected(
    repo_with_pr_worktree, tmp_path
):
    """A legitimate directory whose NAME merely contains '..' as a substring (e.g. `v1..2`)
    must NOT be rejected -- the safety check is a path-COMPONENT match, not a substring
    match. Distinguishes this from an actual `../escape` traversal component."""
    main, _wt = repo_with_pr_worktree
    (main / "v1..2").mkdir()
    (main / "v1..2" / "package.json").write_text('{"scripts":{"test":"v-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=v1..2\n", encoding="utf-8")
    _git("add", "v1..2/package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add a dir whose name merely contains '..' as a substring", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"a dir named 'v1..2' must be accepted, not rejected as unsafe\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not a safe repo-relative" not in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str((main / "v1..2").resolve())], lines


def test_cidown_local_gate_ship_config_dir_dot_is_rejected(repo_with_pr_worktree, tmp_path):
    """SHIP_LOCAL_TEST_DIR=. (meaning 'the repo root itself') is rejected -- routing root
    through non-root auto-detect would bypass the mode="root" pytest-args guarantee in
    _local_test_try_dir. The author should omit the key entirely to mean root."""
    main, _wt = repo_with_pr_worktree
    (main / "package.json").write_text('{"scripts":{"test":"root-suite"}}\n', encoding="utf-8")
    (main / ".ship-config").write_text("SHIP_LOCAL_TEST_DIR=.\n", encoding="utf-8")
    _git("add", "package.json", ".ship-config", cwd=main)
    _git("commit", "-qm", "add .ship-config with DIR=. (must be rejected)", cwd=main)

    bindir = _fake_gh_cidown_dir(tmp_path)
    npm_log = tmp_path / "npm.log"
    _fake_npm_logging_cwd(bindir, npm_log)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_REVIEW_DWELL": "0",
    })

    assert r.returncode == 0, (
        f"ship must fall through to root auto-detect\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "not a safe repo-relative" in r.stderr, r.stderr
    lines = npm_log.read_text(encoding="utf-8").splitlines()
    assert lines == ["test", str(main.resolve())], lines


# Split so this file's OWN source never spells a leftover marker as one contiguous
# token — ship.sh's _local_leftover_check scans this test file's added diff lines
# too (it is one of the two files that legitimately needs to exercise these markers
# as literal test data), so a fixture/docstring that wrote a marker literally would
# self-trigger the CI-outage fallback whenever a PR touches this file
# (agent-tools#318). Same idea as the bracket-expression idiom ship.sh applies to its
# own regex definition line — construct the marker at runtime, never spell it whole
# in source.
MARK_T = "TO" "DO"
MARK_F = "FIX" "ME"
MARK_H = "HAC" "K"
MARK_X = "XX" "X"


def test_cidown_leftover_markers_block(repo_with_pr_worktree, tmp_path):
    """CI-down path: local tests pass but PR diff has an unfinished-work ([T]ODO)
    leftover -> local gate fails."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        # Inject a diff addition with a leftover marker
        "SHIP_TEST_DIFF": f"+new code  # {MARK_T}: clean this up later",
    })

    assert r.returncode != 0, (
        f"ship must refuse when leftover markers found\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_mktemp_template_not_flagged_as_leftover(repo_with_pr_worktree, tmp_path):
    """CI-down path: a diff addition using bash's conventional mktemp XXXXXX (6-X)
    template suffix (e.g. `mktemp -d "/tmp/foo.XXXXXX"`) must NOT be flagged as a
    leftover [X]XX marker — this is standard, legitimate shell code, not a debug
    marker (issue #316)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",  # disable dwell gate: fake PR has no review timestamps
        "SHIP_TEST_DIFF": '+  tmp=$(mktemp -d "/tmp/foo.' + MARK_X + MARK_X + '")',
    })

    assert r.returncode == 0, (
        f"ship must NOT block on a mktemp XXXXXX template\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" not in r.stderr, r.stderr


def test_cidown_standalone_xxx_marker_still_blocks(repo_with_pr_worktree, tmp_path):
    """CI-down path: a genuine standalone [X]XX marker (not part of a longer X run)
    must still be caught by the leftover-marker gate after the word-boundary fix."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",  # disable dwell gate: keep the marker gate the only cause
        "SHIP_TEST_DIFF": f"+// {MARK_X}: this is broken, fix before merge",
    })

    assert r.returncode != 0, (
        f"ship must still refuse on a standalone [X]XX marker\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_cidown_todo_substring_still_blocks(repo_with_pr_worktree, tmp_path):
    """CI-down path: [T]ODO/[F]IXME/[H]ACK deliberately keep plain substring matching
    (unlike [X]XX) so this fallback gate stays at least as strict as the CI-side
    ci/leftover-grep/leftover-grep.sh untracked-marker check, which also
    substring-matches — a sentinel identifier like `[T]ODO_REMOVE_BEFORE_MERGE` must
    still be caught here too, otherwise the CI-outage fallback would be weaker than
    the normal CI-up gate (agent-tools#316 review discussion)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": f"+const retryCount = 3; // {MARK_T}_REMOVE_BEFORE_MERGE",
    })

    assert r.returncode != 0, (
        f"ship must still refuse on a [T]ODO_-prefixed sentinel identifier\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr


def test_cidown_fixme_substring_still_blocks(repo_with_pr_worktree, tmp_path):
    """Companion to the [T]ODO substring test: [F]IXME must independently trip the
    same regex alternation. Not previously exercised — the original 6 tests from
    #317 only ever injected [T]ODO or [X]XX fixtures, leaving the [F]IXME and [H]ACK
    arms of the regex unverified (found in review while fixing agent-tools#318)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": f"+// {MARK_F}: this still needs a real implementation",
    })

    assert r.returncode != 0, (
        f"ship must still refuse on a [F]IXME marker\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr


def test_cidown_hack_substring_still_blocks(repo_with_pr_worktree, tmp_path):
    """Companion to the [T]ODO substring test: [H]ACK must independently trip the
    same regex alternation (see test_cidown_fixme_substring_still_blocks for why this
    was previously unverified)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": f"+// {MARK_H}: workaround for the flaky retry logic",
    })

    assert r.returncode != 0, (
        f"ship must still refuse on a [H]ACK marker\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr


def test_cidown_three_x_mktemp_template_still_blocks(repo_with_pr_worktree, tmp_path):
    """Documented residual limitation: a 3-character mktemp template (`foo.[X]XX`) is
    indistinguishable from a real standalone [X]XX marker under the word-boundary
    regex and still blocks. This pins the documented trade-off in ship.sh's
    _local_leftover_check comment so it doesn't silently change if the boundary
    mechanism is reworked later."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": '+  tmp=$(mktemp -d "/tmp/foo.' + MARK_X + '")',
    })

    assert r.returncode != 0, (
        f"a bare 3-X mktemp template is expected to still block (documented limitation)\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr


def test_cidown_xxx_sentinel_identifier_still_blocks(repo_with_pr_worktree, tmp_path):
    """The [X]XX guard only excludes it inside a longer run of X's (4+, the
    mktemp-template shape) — it must still catch a genuine marker embedded in an
    identifier, like `[X]XX_REMOVE_BEFORE_MERGE` or `foo[X]XX_debug()`, which a full
    word-boundary (\\b/-w) fix would have missed. Pins that this is deliberately
    narrower than a generic word-boundary fix (agent-tools#316 review discussion)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": f"+const x = 1; // {MARK_X}_REMOVE_BEFORE_MERGE",
    })

    assert r.returncode != 0, (
        f"ship must still refuse on an [X]XX_-prefixed sentinel identifier\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr


def test_cidown_gate_editing_its_own_source_does_not_self_trigger(repo_with_pr_worktree, tmp_path):
    """Regression test for agent-tools#318 itself: a diff that edits
    _local_leftover_check's own regex/comment block (as this exact PR does) must NOT
    self-trigger the CI-outage fallback, even though that block legitimately spells
    out example marker text using the bracket-expression idiom. This exercises the
    ACTUAL fix (de-literalized source, no exemption mechanism) rather than a
    mechanism under test — the fixture below mirrors the real shape of
    ci/ship/ship.sh's own doc comment."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    fixture_diff = "\n".join([
        "+# Scan the PR diff additions for leftover markers: an unfinished-work marker",
        '+# ("[T]ODO"/"[F]IXME"/"[H]ACK") or a standalone "[X]XX" marker.',
        "+  hits=$(printf '%s\\n' \"$diff_out\" \\",
        "+    | grep -E '^\\+' | grep -vE '^\\+\\+\\+' \\",
        "+    | grep -E '([T]ODO|[F]IXME|[H]ACK)|(^|[^X])[X]XX($|[^X])' || true)",
    ])

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": fixture_diff,
    })

    assert r.returncode == 0, (
        f"ship must NOT block on the gate's own de-literalized regex/comment block\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" not in r.stderr, r.stderr


def test_cidown_near_miss_of_gates_own_filenames_still_blocks(repo_with_pr_worktree, tmp_path):
    """Regression guard for the design this PR replaced: an EARLIER version of this gate
    exempted ci/ship/ship.sh and tests/test_ship.py by path via `awk -v re=...` with a
    backslash-escaped regex (`^(ci/ship/ship\\.sh|tests/test_ship\\.py)$`). `awk -v` runs
    its own escape-sequence processing on the assigned value, and `\\.` is not a
    recognized escape sequence, so gawk/mawk silently collapsed it to a bare `.` (regex
    wildcard) — verified directly:
        awk -v re='^(ci/ship/ship\\.sh|tests/test_ship\\.py)$' \\
          'BEGIN{print ("ci/ship/ship_sh" ~ re)}'   # => 1 (wrongly matches!)
    Under that design, a file named `ci/ship/ship_sh` (underscore instead of dot) — a
    one-character near-miss of the real path — would have been WRONGLY exempted from
    the leftover scan. That whole path-exemption mechanism has since been removed in
    favor of de-literalizing this gate's own source (see _local_leftover_check's
    comment), so there is no path-matching logic left to fool — but this test pins that
    a near-miss filename gets no special treatment: a real leftover marker on it must
    still block, exactly like any other file."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    near_miss_diff = "\n".join([
        "diff --git a/ci/ship/ship_sh b/ci/ship/ship_sh",
        "index 0000000..1111111 100644",
        "--- a/ci/ship/ship_sh",
        "+++ b/ci/ship/ship_sh",
        "@@ -1,0 +1,1 @@",
        f"+echo real leftover  # {MARK_T} forgot to remove this before merge",
    ])

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": near_miss_diff,
    })

    assert r.returncode != 0, (
        f"ship must still refuse on a marker in a near-miss-named file "
        f"(ci/ship/ship_sh, not the real ci/ship/ship.sh)\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" in r.stderr, r.stderr


_OWN_SOURCES = (
    _SHIP,
    _SHIP_DIR / "ship.py",
    *sorted((_SHIP_DIR / "shiplib").glob("*.py")),
    Path(__file__),
)


def test_cidown_real_ship_sh_and_test_ship_py_source_do_not_self_trigger(repo_with_pr_worktree, tmp_path):
    """Self-enforcing regression guard (review finding on this PR itself): the design this
    PR ships relies on a CONVENTION — never spell a leftover marker as one contiguous token
    anywhere in ci/ship/ship.sh or tests/test_ship.py — rather than a structural mechanism.
    A convention with nothing checking it can silently break (exactly what happened earlier
    in this same PR: a fixture literally spelled the unfinished-work marker as one contiguous
    token and would have self-triggered this very gate). Rather than hand-copy a snapshot of
    a comment block (which drifts the moment
    the real file is next edited), this test feeds this gate the REAL, CURRENT contents of
    both files — every line prefixed as a diff addition — and asserts a clean pass. If either
    file's source ever regains a literal, contiguous marker, this test catches it directly."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_cidown_dir(tmp_path)

    own_source_diff = "\n".join(
        f"+{line}"
        for path in _OWN_SOURCES
        for line in path.read_text().splitlines()
    )

    r = _run_ship_cidown(main, bindir, {
        "SHIP_TEST_CI_DOWN": "1",
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_REVIEW_DWELL": "0",
        "SHIP_TEST_DIFF": own_source_diff,
    })

    assert r.returncode == 0, (
        f"ship must NOT block on its own real, current source or this test file's own source\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "leftover markers" not in r.stderr, r.stderr


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
    # The refusal must point at the known-flake mechanism so an agent facing this exact
    # shape (one check red, everything else green) doesn't need to remember a doc page.
    assert "--known-flake" in r.stderr, r.stderr
    assert "does NOT need human escalation" in r.stderr, r.stderr


# ---------------------------------------------------------------------------------------
# known-flake gate (--known-flake NAME) — a narrower sibling of the CI-down path: covers "one
# specific check is a confirmed pre-existing flake, everything else is green" (below the 80%
# ci_appears_structurally_down threshold), rather than a whole-infrastructure outage.
#
# The fake `gh` below answers the SAME statusCheckRollup shape as _FAKE_GH_PARTIAL_FAIL (one
# FAILED "pytest" check, one SUCCESS "lint" check — 50%, below the outage threshold) plus:
#   - `gh pr view --json baseRefName` -> the base branch name
#   - `gh run list --branch <base> ...` -> one COMPLETED run, controllable conclusion
#   - `gh run view <id> --json jobs -q .jobs` -> that run's per-job conclusions, controllable
# so a test can dial "the base branch run also failed the same check" on or off.
# ---------------------------------------------------------------------------------------

_FAKE_GH_KNOWN_FLAKE = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if printf '%s ' "$@" | grep -q baseRefName; then
          # No colon: an explicitly-set EMPTY SHIP_TEST_BASE stays empty (simulates an
          # unreadable/blank baseRefName) — only an UNSET var falls back to "main". `:-` would
          # treat empty-and-unset the same, which the unreadable-base test needs to tell apart.
          printf '%s' "${SHIP_TEST_BASE-main}"
        elif printf '%s ' "$@" | grep -q -- '--json reviews'; then
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '[{"name":"pytest","workflowName":"ci","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"lint","workflowName":"ci","conclusion":"SUCCESS","status":"COMPLETED","state":"SUCCESS"}]'
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
      merge) echo "[fake gh] merged"; [ -n "${SHIP_TEST_MERGE_LOG:-}" ] && printf '%s\\n' "$*" >> "$SHIP_TEST_MERGE_LOG" || true ;;
      *) : ;;
    esac ;;
  run)
    action="$1"; shift || true
    case "$action" in
      list)
        printf '[{"databaseId":555,"conclusion":"%s"}]' "${SHIP_TEST_BASE_RUN_CONCLUSION:-failure}" ;;
      view)
        printf '[{"name":"pytest","conclusion":"%s"},{"name":"lint","conclusion":"success"}]' "${SHIP_TEST_BASE_JOB_CONCLUSION:-failure}" ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""


def _fake_gh_known_flake_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "binkf"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_KNOWN_FLAKE, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _run_ship_known_flake(main: Path, bindir: Path, *known_flake_names: str, env_extra: dict | None = None):
    """Like _run_ship_cidown, but the argv carries a --known-flake per name (this gate needs
    the flag on the command line, which _run_ship_cidown's hardcoded argv does not support)."""
    env = dict(os.environ)
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_DWELL"] = "0"  # disable dwell gate: fake PR has no review timestamps
    if env_extra:
        env.update(env_extra)
    argv = ["bash", str(_SHIP), "1", "--no-screenshot-ok", "test"]
    for name in known_flake_names:
        argv += ["--known-flake", name]
    return _sh(*argv, cwd=main, env=env)


def test_known_flake_confirmed_runs_local_gate_and_merges(repo_with_pr_worktree, tmp_path):
    """--known-flake pytest, and the base branch's own recent CI history shows the SAME check
    also failed there -> confirmed, local gate runs and (green) ship merges normally. Also
    pins the POSITIVE half of the audit-deferral invariant (review finding, round 3): the
    `confirmed` line is written, and written exactly once, only once a real merge happened —
    the negative half (no `confirmed` on a local-gate failure) was already covered by
    test_known_flake_confirmed_but_local_gate_fails_still_blocks, but nothing previously
    verified the deferred write actually FIRES on the success path; a regression that dropped
    the post-merge `_known_flake_audit_log confirmed` call entirely would have passed every
    test here before this assertion existed."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_known_flake_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"

    r = _run_ship_known_flake(main, bindir, "pytest", env_extra={
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_AUDIT_FILE": str(audit),
    })

    assert r.returncode == 0, (
        f"ship must merge when the sole failing check is a CONFIRMED known flake\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "CONFIRMED against main's recent CI history" in r.stderr, r.stderr
    assert "known-flake gate PASSED" in r.stderr, r.stderr
    assert "merged #1" in r.stdout, r.stdout
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    known_flake_lines = [line for line in lines if line.get("gate") == "known-flake"]
    assert len(known_flake_lines) == 1, f"expected exactly one known-flake audit line: {lines}"
    assert known_flake_lines[0]["decision"] == "confirmed", f"audit trail: {lines}"


def test_known_flake_unconfirmed_still_blocks(repo_with_pr_worktree, tmp_path):
    """--known-flake pytest, but the base branch's recent runs do NOT show pytest failing there
    (the base job conclusion is "success") -> NOT confirmed, ship still refuses. Proves this
    gate cannot be satisfied by the flag alone — it independently checks reality."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_known_flake_dir(tmp_path)

    # The base branch's own history never shows "pytest" failing (green there) — the claim a
    # shipper would be making with --known-flake pytest is simply false here.
    r = _run_ship_known_flake(main, bindir, "pytest", env_extra={
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_TEST_BASE_JOB_CONCLUSION": "success",
    })

    assert r.returncode != 0, (
        f"ship must NOT merge on an unconfirmed --known-flake claim\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "could NOT be confirmed" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_known_flake_not_asserted_for_failing_check_blocks(repo_with_pr_worktree, tmp_path):
    """--known-flake names a DIFFERENT check than the one that's actually failing -> the real
    failure ("pytest") is uncovered, ship still refuses even though the base-branch evidence
    for the (irrelevant) asserted name would have checked out."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_known_flake_dir(tmp_path)

    r = _run_ship_known_flake(main, bindir, "some-other-check", env_extra={"SHIP_LOCAL_TEST_CMD": "true"})

    assert r.returncode != 0, (
        f"ship must NOT merge when the actually-failing check has no matching --known-flake\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "was not asserted via --known-flake" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_known_flake_confirmed_but_local_gate_fails_still_blocks(repo_with_pr_worktree, tmp_path):
    """A confirmed known flake is NOT a free pass on testing: if the local fallback gate itself
    fails (e.g. the actual local test run is red), ship still refuses. The audit trail must
    distinguish this ("confirmed the claim, but never actually merged") from a clean
    `confirmed` decision — reviewing SHIP_AUDIT_FILE alone must not misread this as a merge."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_known_flake_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"

    r = _run_ship_known_flake(main, bindir, "pytest", env_extra={
        "SHIP_LOCAL_TEST_CMD": "false",
        "SHIP_AUDIT_FILE": str(audit),
    })

    assert r.returncode != 0, (
        f"ship must NOT merge when the known-flake IS confirmed but the local gate fails\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "local fallback gates also failed" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout
    lines = audit.read_text(encoding="utf-8").splitlines()
    decisions = [json.loads(line)["decision"] for line in lines if line.strip()]
    assert "confirmed-local-gate-failed" in decisions, f"audit trail: {decisions}"
    assert "confirmed" not in decisions, (
        f"audit trail must not show a bare 'confirmed' for a ship that never merged: {decisions}"
    )


# Two FAILED checks ("pytest" and "codeql"); "pytest" is confirmable against the base branch,
# "codeql" is not (its base-run job conclusion is controllable, defaulting to "success" — never
# seen failing there). Proves "no partial credit": confirming ONE of two failing checks must
# not be enough to pass the whole gate.
_FAKE_GH_KNOWN_FLAKE_TWO_CHECKS = """\
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
        elif printf '%s ' "$@" | grep -q -- '--json reviews'; then
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s ' "$@" | grep -q statusCheckRollup; then
          printf '[{"name":"pytest","workflowName":"ci","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"codeql","workflowName":"ci","conclusion":"FAILURE","status":"COMPLETED","state":"FAILURE"},{"name":"lint","workflowName":"ci","conclusion":"SUCCESS","status":"COMPLETED","state":"SUCCESS"}]'
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
      merge) echo "[fake gh] merged"; [ -n "${SHIP_TEST_MERGE_LOG:-}" ] && printf '%s\\n' "$*" >> "$SHIP_TEST_MERGE_LOG" || true ;;
      *) : ;;
    esac ;;
  run)
    action="$1"; shift || true
    case "$action" in
      list)
        printf '[{"databaseId":555,"conclusion":"failure"}]' ;;
      view)
        printf '[{"name":"pytest","conclusion":"failure"},{"name":"codeql","conclusion":"%s"},{"name":"lint","conclusion":"success"}]' "${SHIP_TEST_CODEQL_BASE_CONCLUSION:-success}" ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""


def _fake_gh_known_flake_two_checks_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "binkf2"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH_KNOWN_FLAKE_TWO_CHECKS, encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def test_known_flake_partial_coverage_of_multiple_failures_still_blocks(repo_with_pr_worktree, tmp_path):
    """Two checks are failing; only ONE ("pytest") is asserted+confirmable, the other
    ("codeql") is not confirmable on the base branch. The gate must require ALL failing
    checks to be covered — one confirmed check must not paper over another genuine failure.
    ship's own DEDUP_FILTER groups/sorts the rollup before this gate ever sees it, so which of
    the two is evaluated first is an implementation detail (also exercises the short-circuit:
    once one row is a certain loss, later rows are skipped, not individually re-verified) —
    assert on the invariant (codeql's unconfirmable claim is surfaced, nothing merges), not on
    a specific per-check message ordering."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_known_flake_two_checks_dir(tmp_path)

    r = _run_ship_known_flake(main, bindir, "pytest", "codeql", env_extra={
        "SHIP_LOCAL_TEST_CMD": "true",
        # codeql never failed on the base branch — its --known-flake assertion is false.
        "SHIP_TEST_CODEQL_BASE_CONCLUSION": "success",
    })

    assert r.returncode != 0, (
        f"ship must NOT merge when even ONE of several asserted checks is unconfirmed\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "'codeql' was asserted via --known-flake but could NOT be confirmed" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout


def test_known_flake_unreadable_base_branch_fails_closed(repo_with_pr_worktree, tmp_path):
    """If the PR's base branch can't be read (gh pr view --json baseRefName fails/empty), the
    gate must refuse rather than silently falling back to guessing a branch to verify against
    (a prior version of this gate did exactly that, review finding)."""
    main, _wt = repo_with_pr_worktree
    bindir = _fake_gh_known_flake_dir(tmp_path)
    # Force the baseRefName query to return an empty/unsafe value.
    r = _run_ship_known_flake(main, bindir, "pytest", env_extra={
        "SHIP_LOCAL_TEST_CMD": "true",
        "SHIP_TEST_BASE": "",
    })

    assert r.returncode != 0, (
        f"ship must refuse when the base branch can't be read, not guess $DEFAULT_BRANCH\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "could not read this PR's base branch" in r.stderr, r.stderr
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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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
# Main-checkout refresh never stashes: a conflicting dirty tree is skipped (#77, #962)
#
# The main-checkout refresh used to run `git pull --ff-only --autostash`: with uncommitted
# changes that conflicted with the incoming pull the stash-pop left UU-class unmerged files and
# another session's work hidden in a stash. ship now fast-forwards only a CLEAN checkout and
# otherwise skips the refresh with a notice; it never stashes and never touches the tree.
# ---------------------------------------------------------------------------------------

@pytest.fixture
def repo_with_stash_conflict(tmp_path):
    """A repo whose main checkout has an uncommitted edit that would conflict with what origin
    gained after the PR branch was cut.  Setup:
      - origin/main has a file `shared.py` with content "origin\n"
      - the main checkout has an uncommitted change to the same line ("local\n")
      - origin/main then changes the same line to "incoming\n"
    A pull with --autostash would stash the local edit, fast-forward, and pop into a UU
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


def test_dirty_main_checkout_conflicting_with_the_incoming_pull_is_left_untouched(repo_with_stash_conflict, tmp_path):
    """The main checkout holds an uncommitted edit that would conflict with what origin gained.
    ship must never stash it (a stash-pop conflict used to leave another session's work hidden in
    a stash and the tree conflicted, agent-tools#962): it skips the fast-forward, says so, leaves
    the file byte-for-byte, and still exits 0 because the merge is done."""
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

    assert "merged #1" in r.stdout, (
        f"expected fake merge to report merged #1:\n{r.stdout}\n{r.stderr}"
    )
    assert r.returncode == 0, f"the merge is done; the refresh must not fail ship\n{r.stdout}\n{r.stderr}"
    assert "not fast-forwarded" in r.stdout + r.stderr, f"no skip notice:\n{r.stdout}\n{r.stderr}"
    assert (main / "shared.py").read_text(encoding="utf-8") == "local\n"
    assert _git("stash", "list", cwd=main).stdout == "", "ship must never stash"
    assert _sh("git", "ls-files", "--unmerged", cwd=main).stdout == ""


def test_stash_pop_no_conflict_clean_tree_passes(repo_with_pr_worktree, tmp_path):
    """Happy path: a clean main checkout is fast-forwarded and ship exits 0."""
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
          *"--json reviews"*) echo "${SHIP_TEST_REVIEW_COUNT:-1}" ;;
          *"--json url"*) printf '%s\\n' "${SHIP_TEST_PR_URL-https://github.com/acme/widgets/pull/1}" ;;
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

# A fake `review` CLI answering `review task <code> --check -C <dir> --verify-repo-only
# --min-iter N [--min-models M] --min-roles R --json`. Behavior is driven entirely by env vars so
# each test controls it. Without `--check` it exits 2 with empty stdout (the real CLI would print
# task summaries, no quorum payload), so a ship that stopped sending `--check` fails every test.
#   SHIP_TEST_REVIEW_ITER / SHIP_TEST_REVIEW_MODELS / SHIP_TEST_REVIEW_ROLES
#                                       iteration/model/role counts to report (default 3/3/3)
#   SHIP_TEST_REVIEW_SUPPORTS_REPO_ONLY=0  reject --verify-repo-only with an argparse-style error
#                                       (exit 2, empty stdout) -- a review-cli that predates
#                                       review-cli#589.
#   SHIP_TEST_REVIEW_IDENTITY           emit THIS as `identity_verification` instead of what the
#                                       flags imply (repo_only / disabled / ran) -- a review-cli
#                                       or shim that ignored the scope flags; `absent` omits the key.
#   SHIP_TEST_REVIEW_EXTRA_JSON         raw `,"key":value…` appended to the payload (e.g. the
#                                       foreign_repo_iterations review-cli#589 reports).
#   SHIP_TEST_REVIEW_OMIT_MODELS=1      omit distinct_models_passed/models from the payload
#                                       entirely, even though the real review-cli always includes
#                                       them (verified against reviewlib.stats.quorum_check and a
#                                       live CLI call) -- simulates a HYPOTHETICAL non-conforming
#                                       build, proving ship's QMODELS_N -gt 0 sanity check fails
#                                       closed on a missing count rather than crashing or treating
#                                       the absent key as satisfied.
#   SHIP_TEST_REVIEW_BROKEN=1           fail every query (simulates an unreadable stats store)
#                                       -> ship.sh must fail closed
#   SHIP_TEST_REVIEW_ARGS_LOG           if set, append one line per invocation recording whether
#                                       --min-models / --min-roles were actually PASSED on the
#                                       command line (min_models_given=0|1 min_roles_given=0|1),
#                                       plus the -C dir and the identity flags (cwd=... repo_only=
#                                       0|1 no_verify=0|1) — lets a test prove ship.sh only sends
#                                       --min-models when the operator explicitly asked (role-based
#                                       is the default now, review-cli#246), while --min-roles and
#                                       the repository scope are always sent.
#   SHIP_TEST_REVIEW_FORCE_PASSED       if set (true/false), emit `passed` VERBATIM instead of
#                                       computing it from the counts — lets a test forge a hollow
#                                       `passed:true` with 0/0 counts (an older/hostile review-cli)
#                                       to prove ship's independent arithmetic gate fails closed (#242).
#   SHIP_TEST_REVIEW_MIN_ITER_ECHO      if set, emit THIS as the JSON `min_iter`/`min_models`/
#                                       `min_roles` echo instead of the flag values, so a test can
#                                       inspect the floor ship actually passed to review-cli.
#
# `passed` mirrors review-cli's own review-cli#246 AND-logic: each floor (iter always, models/
# roles only when the corresponding flag was actually GIVEN on the command line) must
# independently be met — a floor that was never passed as a flag is vacuously satisfied, exactly
# like the real review-cli distinguishes an explicit ask from "not asked at all".
#
# The JSON keys match review-cli's real output: `passed_iterations` / `distinct_models_passed` /
# `distinct_roles_passed` (NOT `iterations` / `distinct_models`, which review-cli never emitted —
# that key mismatch was half of the #242 hole; a fake using the wrong keys would validate a
# fiction).
_FAKE_REVIEW = """\
#!/usr/bin/env bash
sub="${1:-}"; shift || true
if [ "$sub" = "capacity-check" ]; then
  if [ -n "${SHIP_TEST_CAPACITY_JSON:-}" ]; then
    printf '%s' "${SHIP_TEST_CAPACITY_JSON}"; exit 0
  fi
  [ "${SHIP_TEST_CAPACITY_SUPPORTED:-0}" = "1" ] || exit 0
  if [ "${SHIP_TEST_CAPACITY_ALL_EXHAUSTED:-0}" = "1" ]; then
    printf '{"models":["claude:claude-opus-4-8","codex","gemini"],"exhausted":[{"model":"claude:claude-opus-4-8","source":"harness-limits","evidence":"harness 5-hour window exhausted (100%%)"},{"model":"codex","source":"harness-limits","evidence":"harness weekly window exhausted (100%%)"},{"model":"gemini","source":"seat-cooldown:api","evidence":"rate limited"}],"available":[],"all_exhausted":true,"checked_at":"2026-09-09T00:00:00Z"}\n'
  else
    printf '{"models":["claude:claude-opus-4-8","gemini"],"exhausted":[{"model":"claude:claude-opus-4-8","source":"harness-limits","evidence":"harness 5-hour window exhausted (100%%)"}],"available":["gemini"],"all_exhausted":false,"checked_at":"2026-09-09T00:00:00Z"}\n'
  fi
  exit 0
fi
[ "$sub" = "task" ] || exit 0
code="${1:-}"; shift || true
minit=3
minmodels=3
minroles=3
models_given=0
roles_given=0
cdir=""
repo_only=0
no_verify=0
check=0
while [ $# -gt 0 ]; do
  case "$1" in
    -C) shift; cdir="$1" ;;
    --check) check=1 ;;
    --verify-repo-only) repo_only=1 ;;
    --no-verify-identity) no_verify=1 ;;
    --min-iter) shift; minit="$1" ;;
    --min-models) shift; minmodels="$1"; models_given=1 ;;
    --min-roles) shift; minroles="$1"; roles_given=1 ;;
  esac
  shift || true
done
if [ "$check" != "1" ]; then
  echo "fake review: expected review task CODE --check" >&2
  exit 2
fi
if [ -n "${SHIP_TEST_REVIEW_ARGS_LOG:-}" ]; then
  printf 'min_models_given=%s min_roles_given=%s minmodels=%s minroles=%s cwd=%s repo_only=%s no_verify=%s\\n' \\
    "$models_given" "$roles_given" "$minmodels" "$minroles" "$cdir" "$repo_only" "$no_verify" \\
    >> "${SHIP_TEST_REVIEW_ARGS_LOG}"
fi
if [ "$repo_only" = "1" ] && [ "${SHIP_TEST_REVIEW_SUPPORTS_REPO_ONLY:-1}" = "0" ]; then
  echo "review task: error: unrecognized arguments: --verify-repo-only" >&2
  exit 2
fi
if [ "${SHIP_TEST_REVIEW_BROKEN:-0}" = "1" ]; then
  echo "internal error: stats store unreadable" >&2
  exit 1
fi
identity="ran"
[ "$repo_only" = "1" ] && identity="repo_only"
[ "$no_verify" = "1" ] && identity="disabled"
identity="${SHIP_TEST_REVIEW_IDENTITY:-$identity}"
identity_field=$(printf ',"identity_verification":"%s"' "$identity")
[ "$identity" = "absent" ] && identity_field=""
extra="${SHIP_TEST_REVIEW_EXTRA_JSON:-}"
iterations="${SHIP_TEST_REVIEW_ITER:-3}"
models_n="${SHIP_TEST_REVIEW_MODELS:-3}"
roles_n="${SHIP_TEST_REVIEW_ROLES:-3}"
if [ -n "${SHIP_TEST_REVIEW_FORCE_PASSED:-}" ]; then
  passed="${SHIP_TEST_REVIEW_FORCE_PASSED}"
else
  ok=1
  [ "$iterations" -ge "$minit" ] || ok=0
  if [ "$models_given" = "1" ] && [ "$models_n" -lt "$minmodels" ]; then ok=0; fi
  if [ "$roles_given" = "1" ] && [ "$roles_n" -lt "$minroles" ]; then ok=0; fi
  passed="false"
  [ "$ok" = "1" ] && passed="true"
fi
echo_min="${SHIP_TEST_REVIEW_MIN_ITER_ECHO:-}"
[ -n "$echo_min" ] && { minit="$echo_min"; minmodels="$echo_min"; minroles="$echo_min"; }
# A fixed pool of role names, truncated to the reported role count -- keeps the displayed
# "roles seen" list consistent with distinct_roles_passed instead of a fixed/mismatched array.
role_pool="backend security architecture frontend qa"
roles_json="["; sep=""; n=0
for r in $role_pool; do
  [ "$n" -ge "$roles_n" ] && break
  roles_json="${roles_json}${sep}\\"${r}\\""; sep=","; n=$((n+1))
done
roles_json="${roles_json}]"
# Same truncation for the models array: a self-contradictory fixture (e.g. models_n=1 alongside
# a hardcoded 3-entry array) would let a test's "(models seen: ...)" text silently disagree with
# its own count, masking the exact class of fixture/reality mismatch this fake exists to avoid.
model_pool="claude codex gemini k3 glm"
models_json="["; sep=""; n=0
for m in $model_pool; do
  [ "$n" -ge "$models_n" ] && break
  models_json="${models_json}${sep}\\"${m}\\""; sep=","; n=$((n+1))
done
models_json="${models_json}]"
# Mirror the real review-cli: distinct_roles_passed/roles/min_roles are included ONLY when
# --min-roles was actually part of THIS invocation (roles_given=1) -- a request that never asked
# about roles gets no role keys at all, not a zeroed-out placeholder (reviewlib.stats.quorum_check
# gates the whole roles block on `min_roles is not None`).
roles_fields=""
if [ "$roles_given" = "1" ]; then
  roles_fields=$(printf ',"distinct_roles_passed":%s,"roles":%s,"min_roles":%s' "$roles_n" "$roles_json" "$minroles")
fi
# The real review-cli emits distinct_models_passed/models UNCONDITIONALLY (never gated on
# --min-models, unlike roles) -- verified against both reviewlib.stats.quorum_check's source and
# a live `review task ... --check --min-roles N --json` call with NO --min-models, which still
# returned a real distinct_models_passed/models. SHIP_TEST_REVIEW_OMIT_MODELS simulates a
# HYPOTHETICAL non-conforming build that omits them anyway, proving ship's QMODELS_N -gt 0
# sanity check (not just "today's real review-cli always includes it") is what keeps that
# scenario fail-closed rather than crashing or silently authorizing on an absent count.
models_fields=$(printf ',"distinct_models_passed":%s,"models":%s' "$models_n" "$models_json")
[ "${SHIP_TEST_REVIEW_OMIT_MODELS:-0}" = "1" ] && models_fields=""
if [ "${SHIP_TEST_REVIEW_LEGACY_KEYS:-0}" = "1" ]; then
  # Emit ONLY the never-emitted legacy key names (`iterations` / `distinct_models`) to prove ship
  # reads the REAL keys and treats a legacy-only payload as 0/0 -> fail-closed refuse (#242).
  printf '{"task_code":"%s","iterations":%s,"distinct_models":%s,"models":%s,"min_iter":%s,"min_models":%s,"passed":%s%s}\\n' \\
    "$code" "$iterations" "$models_n" "$models_json" "$minit" "$minmodels" "$passed" "$identity_field"
else
  err_field=""
  if [ -n "${SHIP_TEST_REVIEW_ERROR:-}" ]; then
    err_field=$(printf ',"error":"%s"' "$SHIP_TEST_REVIEW_ERROR")
  fi
  printf '{"task_code":"%s","passed_iterations":%s,"total_iterations":%s%s%s,"min_iter":%s,"min_models":%s,"passed":%s%s%s%s}\\n' \\
    "$code" "$iterations" "$iterations" "$models_fields" "$roles_fields" "$minit" "$minmodels" "$passed" "$identity_field" "$err_field" "$extra"
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


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves.
# Note: on approval, `agenttools_hatch_escalation`'s own `result.reason` is now the fixed string
# "approved by tg-ctl ask (request <id>)" — it no longer echoes whatever the fake printed to
# stdout (that was the pre-fix behavior); assertions on the audited `override_reason` must check
# for that fixed prefix, not for text the fake happens to print.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


def _fake_home_with_tg_ctl(tmp_path: Path, tg_ctl: Path, *, name: str = "fake-home") -> Path:
    """A fake HOME dir whose machine-global rig config (`.config/rig/config.yaml`) sets
    `tg_ctl.tg_ctl_path` to the fake tg-ctl. The hatch helper resolves tg-ctl from the OS account's
    real home (pwd.getpwuid), so in-process module tests monkeypatch `resolve_home` to return THIS
    dir — a controllable tg-ctl that never touches the real one or messages Alex, matching the
    production resolution path."""
    home = tmp_path / name
    config = home / ".config" / "rig" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(f'tg_ctl:\n  tg_ctl_path: "{tg_ctl}"\n', encoding="utf-8")
    return home


def _run_ship_quorum(main, gh_bindir, review_bindir, *, branch="feat", extra_args=(), env_extra=None):
    env = dict(os.environ)
    env["PATH"] = _minimal_hermetic_path(gh_bindir, review_bindir)
    env["SHIP_TEST_BRANCH"] = branch
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"   # product default: gate on
    env.pop("SHIP_TEST_DISABLE_REVIEW_QUORUM", None)  # module default is test-only isolation
    # Never let an ambient hatch-request env var leak into a test that doesn't set one (it would
    # turn a plain refusal into a live tg-ctl call). Tests that exercise the hatch set it via
    # env_extra AFTER this pop.
    env.pop("RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM", None)
    # An ambient gh target makes every local-origin test repo a foreign-repo ship, which the
    # quorum gate refuses (agent-tools#824). Tests that exercise a target set it via env_extra.
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
    # Same hygiene for the model-floor explicitness flag: ship.sh tells "explicitly asked for a
    # model floor" apart from "not asked at all" via `${SHIP_REVIEW_QUORUM_MIN_MODELS+x}`, so an
    # ambient value leaking in from the real shell would silently turn a "default, role-only" test
    # into an "explicit model floor" one. Tests that want it explicit set it via env_extra AFTER
    # this pop.
    env.pop("SHIP_REVIEW_QUORUM_MIN_MODELS", None)
    # Same hygiene for the role-floor: an operator/CI environment with a raised
    # SHIP_REVIEW_QUORUM_MIN_ROLES would otherwise leak in and silently change what
    # a "default 3-role floor" test actually asserts (codex review, PR #416).
    env.pop("SHIP_REVIEW_QUORUM_MIN_ROLES", None)
    if env_extra:
        for key, val in env_extra.items():
            if val is None:
                env.pop(key, None)
            else:
                env[key] = val
    return _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
        cwd=main, env=env,
    )


def test_review_quorum_blocks_when_bar_short(tmp_path):
    """Fewer distinct BOARD ROLES than the default 3-role floor -> ship REFUSES."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "HYP-100", "SHIP_TEST_REVIEW_ITER": "1", "SHIP_TEST_REVIEW_MODELS": "2", "SHIP_TEST_REVIEW_ROLES": "1"},
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


def test_review_quorum_query_is_scoped_to_this_repository(tmp_path):
    """agent-tools#824: task codes are not repo-scoped, so ship asks review-cli for THIS
    checkout's repository only (-C <repo root> --verify-repo-only) and never sends
    --no-verify-identity, which counted every repository's iterations for the code."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    args_log = tmp_path / "review-args.log"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"REVIEW_TASK_CODE": "#464", "SHIP_TEST_REVIEW_ARGS_LOG": str(args_log)},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    lines = args_log.read_text().strip().splitlines()
    assert len(lines) == 1, lines
    assert f"cwd={main.resolve()} " in lines[0], lines[0]
    assert "repo_only=1" in lines[0], lines[0]
    assert "no_verify=0" in lines[0], lines[0]


def test_review_quorum_refuses_review_cli_without_repo_scope(tmp_path):
    """A review-cli that predates --verify-repo-only (review-cli#589) rejects the flag. Ship
    refuses and names the upgrade; it never retries the query without the repository scope."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    args_log = tmp_path / "review-args.log"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-103",
            "SHIP_TEST_REVIEW_SUPPORTS_REPO_ONLY": "0",
            "SHIP_TEST_REVIEW_ARGS_LOG": str(args_log),
        },
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "predates --verify-repo-only" in r.stderr, r.stderr
    assert "Upgrade review-cli" in r.stderr, r.stderr
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"
    lines = args_log.read_text().strip().splitlines()
    assert all("repo_only=1" in line for line in lines), lines


@pytest.mark.parametrize("identity", ["disabled", "skipped_unresolvable", "ran", "absent"])
def test_review_quorum_refuses_counts_not_scoped_to_the_repository(tmp_path, identity):
    """Counts are only trusted when review-cli reports it checked the repository alone
    (identity_verification=repo_only). A review-cli or shim that ignored the scope flags, or
    could not resolve the checkout, may have counted other repositories' reviews — refuse even
    on a full 3/3/3 payload, and even during a confirmed capacity outage (no auto-proceed)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-104",
            "SHIP_TEST_REVIEW_IDENTITY": identity,
            "SHIP_TEST_CAPACITY_SUPPORTED": "1",
            "SHIP_TEST_CAPACITY_ALL_EXHAUSTED": "1",
        },
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert f"identity_verification={identity}" in r.stderr, r.stderr
    assert "auto-proceeding" not in r.stdout + r.stderr
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_refusal_names_other_repositories_iterations(tmp_path):
    """The refusal lists what the repository scope dropped (repo and timestamp), and marks a
    list review-cli cut at its cap."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    foreign = (
        ',"excluded_foreign_repo_iterations":60,"foreign_repo_iterations_truncated":true,'
        '"foreign_repo_iterations":[{"repo_id":"github.com/acme/other","ts":"2026-09-08T17:58:44Z",'
        '"task_code":"464","passed":true}]'
    )
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "#464",
            "SHIP_TEST_REVIEW_ROLES": "1",
            "SHIP_TEST_REVIEW_EXTRA_JSON": foreign,
        },
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert (
        "60 iteration(s) recorded in other repositories under #464 not counted: "
        "github.com/acme/other 2026-09-08T17:58:44Z, …"
    ) in r.stderr, r.stderr


@pytest.mark.parametrize("extra_args, target_env", [
    (("--repo", "other/repo"), {}),
    ((), {"GH_SHIP_REPO": "other/repo"}),
    ((), {"GH_REPO": "other/repo"}),
])
def test_review_quorum_refuses_foreign_repo_target(extra_args, target_env, tmp_path):
    """With --repo / GH_SHIP_REPO / GH_REPO naming another repository, this checkout's review
    history is the wrong repository's: refuse (and audit the refusal) and say where to run ship,
    instead of scoping to the wrong repo."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv,
        extra_args=extra_args,
        env_extra={"REVIEW_TASK_CODE": "HYP-105", "SHIP_AUDIT_FILE": str(audit), **target_env},
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "Run ship from a checkout whose origin is other/repo" in r.stderr, r.stderr
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"
    recs = [json.loads(ln) for ln in audit.read_text().splitlines()]
    assert [rec["decision"] for rec in recs] == ["refused"], recs


@pytest.mark.parametrize("origin, extra_args, target_env", [
    ("git@github.com:Acme/Widget.git", ("--repo", "acme/widget"), {}),
    ("git@github.com:Acme/Widget.git", (), {"GH_REPO": "github.com/Acme/Widget"}),
    ("git@github.com:Acme/Widget.git", (), {"GH_SHIP_REPO": "https://github.com/acme/widget.git"}),
    ("https://github.com/acme/widget.git", (), {"GH_SHIP_REPO": "git@github.com:acme/widget.git"}),
    ("git@GitHub.com:Acme/Widget.git", ("--repo", "acme/widget"), {}),
    ("ssh://git@github.com:22/Acme/Widget.git", ("--repo", "acme/widget"), {}),
    ("git@github.com:acme/widget.git", (), {"GH_REPO": "ssh://git@github.com:443/acme/widget"}),
])
def test_review_quorum_same_repo_target_in_another_spelling_is_not_foreign(
    origin, extra_args, target_env, tmp_path
):
    """A gh target naming this checkout's own github.com repository in another spelling (case,
    host prefix, URL or SSH form, `.git`) is the same repository: the gate checks its reviews
    instead of refusing the ship as a foreign one."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    _git("remote", "set-url", "origin", origin, cwd=main)
    r = _run_ship_quorum(
        main, _fake_gh_quorum_dir(tmp_path), _fake_review_dir(tmp_path),
        extra_args=extra_args,
        env_extra={"REVIEW_TASK_CODE": "HYP-105", "GIT_ALLOW_PROTOCOL": "file", **target_env},
    )
    assert "review-quorum gate — the PR is in" not in r.stderr, r.stderr
    assert "AUTHORITY CONFIRMED" in r.stdout, f"{r.stdout}\n{r.stderr}"


# --- the real review-cli against a fixture run-stats store (agent-tools#824) ---------------

def _real_review_bindir(tmp_path: Path) -> Path:
    """A bin dir holding ONLY a `review` that runs the installed review-cli (the rest of its bin
    dir — task, tg, … — must stay off the hermetic PATH). `capacity-check` answers "not all
    exhausted": the real one reads this machine's live seat state, and a fleet-wide outage would
    turn every refusal these tests expect into an auto-proceed. Skips when the installed
    review-cli predates review-cli#589."""
    found = shutil.which("review")
    if not found:
        pytest.skip("review-cli is not installed")
    real = os.path.realpath(found)
    try:
        help_text = subprocess.run(
            [real, "task", "--help"], capture_output=True, text=True, timeout=60
        ).stdout
    except subprocess.TimeoutExpired:
        pytest.skip("installed review-cli did not answer `review task --help`")
    if "--verify-repo-only" not in help_text:
        pytest.skip("installed review-cli predates --verify-repo-only (review-cli#589)")
    bindir = tmp_path / "bin_real_review"
    bindir.mkdir()
    wrapper = bindir / "review"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "capacity-check" ]; then\n'
        '  printf \'{"models":["codex"],"exhausted":[],"available":["codex"],'
        '"all_exhausted":false}\\n\'\n'
        "  exit 0\n"
        "fi\n"
        f"exec {shlex.quote(real)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return bindir


def _write_run_stats(path: Path, records: list[dict]) -> None:
    lines = []
    for rec in records:
        full = {"v": 5, "mode": "review", "duration_seconds": 1.0, "ok_count": 1,
                "fail_count": 0, "passed": True, "models": ["codex"], **rec}
        full.setdefault("usable_roles", full.get("roles", []))
        lines.append(json.dumps(full))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_ship_real_review(tmp_path, origin_url, records, code="#464"):
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    # review-cli identifies the repository by its origin URL. The pushes above already
    # created the remote-tracking refs, and GIT_ALLOW_PROTOCOL keeps ship's best-effort
    # fetches from reaching the network.
    _git("remote", "set-url", "origin", origin_url, cwd=main)
    store = tmp_path / "run-stats.jsonl"
    _write_run_stats(store, records)
    return _run_ship_quorum(
        main, _fake_gh_quorum_dir(tmp_path), _real_review_bindir(tmp_path),
        env_extra={
            "REVIEW_TASK_CODE": code,
            "REVIEW_STATS_FILE": str(store),
            "GIT_ALLOW_PROTOCOL": "file",
            "SHIP_AUDIT_FILE": str(tmp_path / "ship-audit.jsonl"),
        },
    )


_OTHER_REPO_464 = {
    "ts": "2026-09-08T17:58:44.226392+00:00", "task_code": "464",
    "repo_id": "github.com/acme/other", "diff_files": ["x.py"],
    "roles": ["consistency", "security", "performance"],
}


def test_review_quorum_real_review_cli_drops_other_repos_same_code(tmp_path):
    """The #464 incident: another repository recorded the digit twin `464` with three roles.
    Unscoped, those roles authorized the merge; scoped, they do not count, and the refusal
    names the dropped iteration (repo and timestamp)."""
    r = _run_ship_real_review(tmp_path, "https://github.com/acme/widget.git", [
        _OTHER_REPO_464,
        {"ts": "2026-09-29T09:32:35.482321+00:00", "task_code": "#464",
         "repo_id": "github.com/acme/widget", "diff_files": ["README.md"],
         "roles": ["correctness"]},
    ])
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "bar NOT met for #464" in r.stderr, r.stderr
    assert "1/3 distinct roles (roles seen: correctness)" in r.stderr, r.stderr
    assert (
        "1 iteration(s) recorded in other repositories under #464 not counted: "
        "github.com/acme/other 2026-09-08T17:58:44.226392+00:00"
    ) in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_real_review_cli_counts_own_iterations_from_primary_checkout(tmp_path):
    """Ship runs from the primary checkout, whose diff has nothing to do with the PR: the
    PR's own iterations count by repository alone, whatever files they touched."""
    r = _run_ship_real_review(tmp_path, "git@github.com:acme/widget.git", [
        _OTHER_REPO_464,
        {"ts": "2026-09-29T09:32:35.482321+00:00", "task_code": "#464",
         "repo_id": "github.com/acme/widget", "diff_files": ["src/only_in_the_pr.py"],
         "roles": ["correctness", "quality", "architect"]},
    ])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED — review quorum met: 1 iterations across 3 roles for #464" in r.stdout
    assert "github.com/acme/other 2026-09-08T17:58:44.226392+00:00" in r.stderr, r.stderr
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_real_review_cli_keeps_history_across_repo_move(tmp_path):
    """sme-archiving-gc's reviews recorded while it lived on Forgejo as sme-archiving-gc-tmp
    still count after its origin moved back to GitHub."""
    r = _run_ship_real_review(tmp_path, "https://github.com/alex-mextner/sme-archiving-gc.git", [
        {"ts": "2026-09-29T09:32:35.482321+00:00", "task_code": "#464",
         "repo_id": "git.hyperide.ai/ultrabricks/sme-archiving-gc-tmp", "diff_files": ["a.ts"],
         "roles": ["correctness", "quality", "performance"]},
    ])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED — review quorum met: 1 iterations across 3 roles for #464" in r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_complete_roles_zero_models_authorizes_without_hatch(tmp_path):
    """Astra 2026-09-08 / agent-tools#644: distinct_models is telemetry, not the predicate.
    One padded model (review-cli#433) fills every board role; distinct_models_passed may be 0.
    Complete roles must AUTHORIZE and must NOT open the Telegram hatch."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-644",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "0",
            "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM": (
                "must not be consulted; roles are already filled"
            ),
        },
    )
    assert r.returncode == 0, (
        f"3 roles with 0 models (padded astra) must ship without hatch\n{r.stdout}\n{r.stderr}"
    )
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "3 iterations across 3 roles" in r.stdout, r.stdout
    assert "APPROVED by Alex" not in r.stdout, "hatch must not fire when roles are filled"
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_roles_met_despite_short_iter_and_failed_passed_flag(tmp_path):
    """Live proof (review-cli#433 / astra pad): distinct_roles_passed=3, distinct_models_passed=1,
    passed_iterations=2, passed=false (review-cli still ANDs default min_iter=3). Ship must
    authorize on the role floor and must not hatch."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-433",
            "SHIP_TEST_REVIEW_ITER": "2",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "1",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "false",
            "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM": (
                "must not be consulted; roles are already filled"
            ),
        },
    )
    assert r.returncode == 0, (
        f"3 roles must ship even with 2 iterations and passed=false\n{r.stdout}\n{r.stderr}"
    )
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "APPROVED by Alex" not in r.stdout, "hatch must not fire when roles are filled"
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_zero_iterations_with_roles_is_empty_evidence(tmp_path):
    """Hollow-payload guard (#242 / review finding on #644): 3 roles with 0 passed
    iterations is not astra padding — no review ran. Must refuse. Distinct from
    passed_iterations=2 / passed=false, which still authorizes."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-644D",
            "SHIP_TEST_REVIEW_ITER": "0",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "0",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "true",
        },
    )
    assert r.returncode != 0, (
        f"0 iterations with 3 roles must refuse as empty evidence\n{r.stdout}\n{r.stderr}"
    )
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_passed_false_with_error_still_authorizes_on_roles(tmp_path):
    """PR 87 hole: review --check passed=false and .error set (min_iter AND) while
    distinct_roles_passed meets the floor. Must authorize; hatch must not fire."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-648",
            "SHIP_TEST_REVIEW_ITER": "2",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "1",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "false",
            "SHIP_TEST_REVIEW_ERROR": "min_iter 2 < 3",
            "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM": "must not fire; roles are filled",
        },
    )
    assert r.returncode == 0, f"roles+error+passed=false must ship\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "APPROVED by Alex" not in r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_omitted_models_field_still_authorizes_on_roles(tmp_path):
    """Models are telemetry: a payload that omits distinct_models_passed entirely but carries
    complete role coverage must still authorize. The old QMODELS_N -gt 0 sanity check treated
    telemetry absence as empty evidence and wrongly hatched (agent-tools#644)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-644B",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_OMIT_MODELS": "1",
        },
    )
    assert r.returncode == 0, (
        f"3 roles must authorize even when the models key is absent\n{r.stdout}\n{r.stderr}"
    )
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout




def test_review_quorum_zero_env_is_rejected_at_parse(tmp_path):
    """SHIP_REVIEW_QUORUM=0 is forbidden: reject it at parse. The gate stays enabled, so a
    ship with no review CLI still fail-closes instead of merging unverified (#644)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, review_bindir=None,
        env_extra={"SHIP_REVIEW_QUORUM": "0", "REVIEW_TASK_CODE": "HYP-644C"},
    )
    assert r.returncode != 0, (
        f"SHIP_REVIEW_QUORUM=0 must not disable Guard-B\n{r.stdout}\n{r.stderr}"
    )
    assert "review-quorum gate disabled" not in r.stdout, r.stdout
    assert "forbidden" in r.stderr or "SHIP_REVIEW_QUORUM=0" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_test_disable_ignored_outside_pytest(tmp_path):
    """SHIP_TEST_DISABLE_REVIEW_QUORUM=1 without PYTEST_CURRENT_TEST must NOT disable Guard-B
    (Codex P1 on #645: a production caller must not recreate the SHIP_REVIEW_QUORUM=0 hole)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, review_bindir=None,
        env_extra={
            "SHIP_TEST_DISABLE_REVIEW_QUORUM": "1",
            "REVIEW_TASK_CODE": "HYP-644E",
            "PYTEST_CURRENT_TEST": None,
        },
    )
    assert r.returncode != 0, (
        f"test-only disable must not work outside pytest\n{r.stdout}\n{r.stderr}"
    )
    assert "review-quorum gate disabled" not in r.stdout, r.stdout
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


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


def test_review_quorum_derives_literal_github_issue_ref_from_pr_body(tmp_path):
    """A bare GitHub issue reference (`Fixes #105`) is a task code too, returned LITERAL
    (`#105`, not `GH-105`): task-cli routes a GitHub id by its leading `#`, so a rewrite
    would make the post-merge `task mark-shipped` unroutable."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": "Fixes #105 for the widget regression."},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "#105" in r.stdout, r.stdout
    assert "GH-105" not in r.stdout, r.stdout


def test_review_quorum_derives_refs_keyword_issue_ref_from_pr_body(tmp_path):
    """`Refs #N` (the non-auto-closing reference form) anchors an issue ref just like GitHub's
    closing keywords do; keyword matching is case-insensitive and tolerates a colon."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": "refs: #77 -- widget follow-up."},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "#77" in r.stdout, r.stdout


@pytest.mark.parametrize(
    "body",
    [
        # qualified cross-repo refs (incl. repo names ending in `-`/`.`) name ANOTHER repo's issue
        "Fixes other-org/other-repo#123; closes org/trailing-#124; resolves org/dotted.#125.",
        # a repo NAME/path ending in a keyword is still a qualified ref, not a keyword anchor
        "Mirrors org/hot-fix#5, org/fix#6 and org/my.fix#7 upstream.",
        # a URL fragment / Markdown anchor is not an issue reference
        "See https://example.org/docs/#105 and the notes at #L245-L250.",
        # an incidental prose mention of an (already-shipped) issue must not borrow its record
        "Follow-up tidy-up for #12, reverts (#268). Not a fix for #123abc.",
    ],
)
def test_review_quorum_ignores_non_anchored_github_issue_refs(tmp_path, body):
    """Only a keyword-anchored local ref (`Fixes #N` / `Refs #N`) is a task code. The helper also
    feeds the post-merge `task mark-shipped` call and the quorum-record lookup, so a cross-repo
    ref, a URL fragment, or a bare prose mention of a shipped issue must NOT be derived — with
    nothing else in the body ship refuses fail-closed."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": body})
    assert r.returncode != 0, f"must not derive a task code from {body!r}\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_prefixed_ticket_takes_precedence_over_github_issue_ref(tmp_path):
    """A lettered PREFIX-<n> ticket in the body wins over a `Fixes #N` aside in the same body —
    the GitHub-issue arm runs AFTER the generic numeric arm so it never shadows a real ticket."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"SHIP_TEST_PR_BODY": "Fixes #45 as part of PROJ-123 (see the epic)."},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PROJ-123" in r.stdout, r.stdout
    assert "#45" not in r.stdout, r.stdout


def test_review_quorum_descriptive_code_takes_precedence_over_github_issue_ref(tmp_path):
    """A descriptive ALL-CAPS code in the body wins over an `also fixes #N` aside too — the
    GitHub-issue arm is the LAST fallback, so a descriptive-code repo keeps deriving its own
    code (no regression) and the aside can never borrow an already-shipped issue's record."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "SHIP_TEST_PR_BODY": "SME-ROADMAP-WORKTREE-NOTE — reword the note. Also fixes #12.",
        },
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "SME-ROADMAP-WORKTREE-NOTE" in r.stdout, r.stdout
    assert "#12" not in r.stdout, r.stdout


@pytest.mark.parametrize(
    "body",
    [
        "Partially fixes #12 as a side effect.\n\nCloses #200\n",
        # no whitespace between the refs: the first match must not swallow the `,`/`;` that is
        # the second keyword's leading boundary, or the ambiguity would go undetected
        "closes #12,fixes #200",
        "Fixes #12;Refs: #200.",
    ],
)
def test_review_quorum_refuses_two_distinct_anchored_github_issue_refs(tmp_path, body):
    """Two DISTINCT keyword-anchored refs (`Partially fixes #12 ... Closes #200`) are an
    ambiguous task code. Every other branch of the gate is fail-closed, so ship derives nothing
    and refuses rather than silently taking the first ref in document order."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": body})
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_same_github_issue_ref_twice_is_not_ambiguous(tmp_path):
    """The SAME issue anchored twice (`Fixes #105 ... Refs #105`) is one task code, not two."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"SHIP_TEST_PR_BODY": "Fixes #105.\n\nRefs #105 for the follow-up notes.\n"},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "#105" in r.stdout, r.stdout


def test_review_quorum_invalid_github_issue_ref_does_not_count_toward_ambiguity(tmp_path):
    """`Fixes #12abc` is not an issue ref (digits must end at a non-word boundary), so next to a
    real `Closes #200` it neither derives `#12` nor makes the body ambiguous — `#200` wins."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": "Fixes #12abc Closes #200"},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "#200" in r.stdout, r.stdout
    assert "#12" not in r.stdout.replace("#12abc", ""), r.stdout


@pytest.mark.parametrize(
    "body,expected",
    [
        # task-cli's links gate demands a markdown link / full URL, never a bare `#548`
        ("Refs [#548](https://github.com/acme/widgets/issues/548) — widget regression.", "#548"),
        # a bare full URL, no keyword, no markdown
        ("Ticket: https://github.com/acme/widgets/issues/548", "#548"),
        # GitHub slugs are case-insensitive; a `www.` host and a trailing anchor/comment link too
        ("See https://www.github.com/ACME/Widgets/issues/548#issuecomment-1 for context.", "#548"),
        # the same issue linked twice is ONE ticket, not an ambiguity
        ("https://github.com/acme/widgets/issues/548 and again https://github.com/acme/widgets/issues/548", "#548"),
    ],
)
def test_review_quorum_derives_literal_code_from_same_repo_issue_url(tmp_path, body, expected):
    """A full GitHub issue URL of the PR's OWN repo is a task code (agent-tools#564): task-cli's
    `links` gate REQUIRES tickets to be referenced as markdown links / full URLs, so a PR body
    written correctly for that gate used to fail this one with "could not derive a task code".
    The URL yields the same literal `#<n>` a keyword-anchored `Refs #<n>` yields (#511), so
    review-cli's quorum record and task-cli's routing see ONE code regardless of body syntax."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": body})
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and expected in r.stdout, r.stdout
    assert "GH-548" not in r.stdout, r.stdout


@pytest.mark.parametrize(
    "body",
    [
        # an issue of ANOTHER repo (cross-repo companions are common: tg-cli#301 <-> agent-tools#524)
        "Companion of https://github.com/other-org/other-repo/issues/548.",
        # a PULL REQUEST link is not a ticket
        "Follow-up to https://github.com/acme/widgets/pull/548.",
        # a same-owner different-repo issue is still another repo
        "See https://github.com/acme/widgets-docs/issues/548.",
        # two DISTINCT same-repo issues are an ambiguous task code (fail-closed, like `#<n>`)
        "https://github.com/acme/widgets/issues/12 and https://github.com/acme/widgets/issues/548",
        # a non-GitHub host, and a GitHub URL that is not an issue path
        "https://gitlab.com/acme/widgets/issues/548 https://github.com/acme/widgets/blob/main/issues/548",
    ],
)
def test_review_quorum_ignores_foreign_pull_and_ambiguous_issue_urls(tmp_path, body):
    """Only an issue URL of the PR's own repo qualifies: a link to another repo's issue must not
    be mistaken for this PR's ticket (it would look up that repo's quorum record and later mark
    the wrong local issue shipped), a `/pull/<n>` link is a PR, and two distinct same-repo
    issue links are ambiguous — all fail closed with nothing else in the body."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": body})
    assert r.returncode != 0, f"must not derive a task code from {body!r}\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_issue_url_needs_a_known_own_repo(tmp_path):
    """Fail-closed when ship cannot tell which repo the PR belongs to (no github origin, no
    --repo, and `gh pr view --json url` returns nothing usable): a same-looking issue URL is
    NOT derived, because "same repo" cannot be verified."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"SHIP_TEST_PR_BODY": "https://github.com/acme/widgets/issues/548", "SHIP_TEST_PR_URL": ""},
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr


def _extract_ticket_with_own_repo(bodies, *, cwd: Path, origin_repo: str, live_pr_url: str | None):
    """Run `review_quorum_extract_ticket` on each body under ONE Ctx whose checkout origin
    slug is `origin_repo` and whose `gh pr view --json url` yields `live_pr_url` (None = the
    call fails). Returns the derived codes in order ("" = nothing derived)."""
    ctx = Ctx(str(_SHIP_DIR))
    ctx.pr = "7"
    ctx.env["GH_REPO"] = ""
    ctx.cwd_origin_repo = origin_repo
    ctx.gh = lambda *a, **k: (
        Result(1) if live_pr_url is None else Result(0, live_pr_url + "\n")
    )
    return [quorum_skipci.review_quorum_extract_ticket(ctx, b) for b in bodies]


def test_review_quorum_issue_url_accepts_live_pr_slug_after_repo_rename(tmp_path):
    """A renamed/transferred repo leaves the clone with the OLD, redirecting origin slug while the
    live PR and its issue links carry the NEW one (codex review, PR #566). An issue URL under
    EITHER slug names this PR's own repo, so a correct link is not refused as foreign; the same
    issue under both slugs is ONE ticket; another repo's issue is still foreign."""
    got = _extract_ticket_with_own_repo(
        [
            "Refs [#105](https://github.com/new-org/new-repo/issues/105)",
            "Refs [#105](https://github.com/old-org/old-repo/issues/105)",
            "https://github.com/old-org/old-repo/issues/12 https://github.com/new-org/new-repo/issues/12",
            "https://github.com/other-org/other-repo/issues/105",
        ],
        cwd=tmp_path, origin_repo="old-org/old-repo",
        live_pr_url="https://github.com/new-org/new-repo/pull/7",
    )
    assert got == ["#105", "#105", "#12", ""], got


def test_review_quorum_issue_url_origin_slug_still_works_when_gh_url_lookup_fails(tmp_path):
    """The live-PR-URL lookup is additive: when `gh pr view --json url` fails (offline), the
    checkout origin alone still identifies the own repo, exactly as before."""
    got = _extract_ticket_with_own_repo(
        ["https://github.com/acme/widgets/issues/548", "https://github.com/acme/other/issues/548"],
        cwd=tmp_path, origin_repo="acme/widgets", live_pr_url=None,
    )
    assert got == ["#548", ""], got


def test_review_quorum_keyword_anchored_ref_takes_precedence_over_issue_url(tmp_path):
    """The URL arm is the LAST fallback: an explicit `Refs #12` wins over a same-repo issue URL
    mentioned elsewhere in the body."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"SHIP_TEST_PR_BODY": "Refs #12 — see also https://github.com/acme/widgets/issues/548"},
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "#12" in r.stdout, r.stdout
    assert "#548" not in r.stdout, r.stdout


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Ticket: https://linear.app/hyperide/issue/HYP-1440/ship-url-task-code", "HYP-1440"),
        ("Ticket: [ABC-12](https://linear.app/acme/issue/ABC-12/some-slug)", "ABC-12"),
    ],
)
def test_review_quorum_derives_code_from_linear_issue_url(tmp_path, body, expected):
    """A Linear issue URL (`https://linear.app/<team>/issue/<CODE>/<slug>`) carries the ticket
    code in its path, so the HYP-<n> / PREFIX-<n> arms already derive it — pinned here (not a
    fix) so the URL forms task-cli's links gate demands stay covered end to end."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, env_extra={"SHIP_TEST_PR_BODY": body})
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and expected in r.stdout, r.stdout


def test_review_quorum_derives_descriptive_code_from_pr_body(tmp_path):
    """No $REVIEW_TASK_CODE and no ticket in the branch name -> ship also derives a purely
    descriptive (non-numeric) review-cli task code from the PR body — the real-world #384
    case: a docs-only PR whose 3 review-quorum iterations were recorded under task
    `SME-ROADMAP-WORKTREE-NOTE`, same shape as review-cli's own run-stats.jsonl."""
    main, _wt = _make_repo_with_branch(tmp_path, "roadmap-worktree-convention-note")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        branch="roadmap-worktree-convention-note",
        env_extra={
            "SHIP_TEST_PR_BODY": "Review findings addressed, task SME-ROADMAP-WORKTREE-NOTE.",
        },
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "SME-ROADMAP-WORKTREE-NOTE" in r.stdout, r.stdout


def test_review_quorum_derives_descriptive_code_from_branch_name(tmp_path):
    """A branch name carrying an all-uppercase, hyphen-joined descriptive task code (no
    digits) is picked up too, same as the numeric HYP-<n> case."""
    branch = "fix/WT-GITIGNORE-EXCLUDE-followup"
    main, _wt = _make_repo_with_branch(tmp_path, branch)
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(main, gh, rv, branch=branch)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "WT-GITIGNORE-EXCLUDE" in r.stdout, r.stdout


def test_review_quorum_descriptive_pattern_does_not_match_bare_acronyms(tmp_path):
    """The descriptive-code pattern requires 2+ hyphens (3+ segments), so ordinary PR-body
    prose full of unrelated all-caps acronyms (PASSED, PR, CI, README) — but no 3-segment
    hyphenated all-caps token — must NOT be mistaken for a task code; ship still refuses."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "SHIP_TEST_PR_BODY": "All models PASSED. See README and CI, updates in PR body.",
        },
    )
    assert r.returncode != 0, f"bare acronyms must not be treated as a task code\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_descriptive_pattern_does_not_match_two_word_prose(tmp_path):
    """Common TWO-word hyphenated English (READ-ONLY, CI-CD, PRE-COMMIT, API-KEY) is exactly
    the false-positive class two independent review-cli models flagged against an earlier,
    looser version of this pattern (#384 review round 1) — the 3-segment floor must reject all
    of it, not just hyphen-less acronyms."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "SHIP_TEST_PR_BODY": (
                "sandbox: READ-ONLY. Ran CI-CD, added a PRE-COMMIT hook, rotated the API-KEY."
            ),
        },
    )
    assert r.returncode != 0, f"two-word hyphenated prose must not be treated as a task code\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_descriptive_pattern_rejects_digit_bearing_token(tmp_path):
    """A token with a digit buried mid-segment (`SME-ROADMAP-V2-NOTE`) must be rejected
    outright, not silently truncate-matched down to a bogus prefix (`SME-ROADMAP-V`) — the
    boundary bug an earlier version of this pattern had (#384 review round 1)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"SHIP_TEST_PR_BODY": "Task code SME-ROADMAP-V2-NOTE covers this."},
    )
    assert r.returncode != 0, f"a digit-bearing token must not derive a truncated code\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "SME-ROADMAP-V" not in r.stderr, f"must not silently truncate-match: {r.stderr}"
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_descriptive_pattern_filters_per_candidate(tmp_path):
    """A rejected digit-bearing candidate must not shadow a CLEAN descriptive code appearing
    later in the same text -- pins the per-candidate (not whole-text) filtering design (#384
    review round 2): a whole-text-reject-all implementation would also pass the
    `rejects_digit_bearing_token` test above without actually filtering per candidate, so this
    is the test that distinguishes the two."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "SHIP_TEST_PR_BODY": "Task SME-ROADMAP-V2-NOTE superseded by REAL-TASK-CODE-HERE.",
        },
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "REAL-TASK-CODE-HERE" in r.stdout, r.stdout


def test_review_quorum_descriptive_code_is_case_sensitive(tmp_path):
    """A lowercase descriptive code must NOT match — only the fully-uppercase shape counts,
    same posture as the numeric generic pattern."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={"SHIP_TEST_PR_BODY": "Part of sme-roadmap-worktree-note, lowercase."},
    )
    assert r.returncode != 0, f"lowercase must not derive a task code\n{r.stdout}\n{r.stderr}"
    assert "could not derive a task code" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_review_quorum_numeric_code_takes_precedence_over_descriptive(tmp_path):
    """When a PR body carries both a numeric-suffix ticket and a descriptive code, the numeric
    arm (tried first) wins — pinning the fallback order the function's docblock describes."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "SHIP_TEST_PR_BODY": "Fixes ABC-123, related to SME-ROADMAP-WORKTREE-NOTE.",
        },
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "ABC-123" in r.stdout, r.stdout
    assert "SME-ROADMAP-WORKTREE-NOTE" not in r.stdout, r.stdout


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
    # Role-based coverage is the mandatory floor now (review-cli#246): the real 3-role count and
    # the floor it was checked against both land in the audit line. No SHIP_REVIEW_QUORUM_MIN_MODELS
    # was set, so min_models reads 0 — the unambiguous "model floor was NOT enforced" sentinel.
    assert rec["roles"] == 3, rec
    assert rec["min_roles"] == 3, rec
    assert rec["min_models"] == 0, rec
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
            "REVIEW_TASK_CODE": "HYP-108", "SHIP_TEST_REVIEW_ITER": "1", "SHIP_TEST_REVIEW_MODELS": "2",
            "SHIP_TEST_REVIEW_ROLES": "1",
            "SHIP_AUDIT_FILE": str(audit),
        },
    )
    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    rec = json.loads(audit.read_text().strip().splitlines()[0])
    assert rec["decision"] == "refused", rec
    assert rec["task_code"] == "HYP-108", rec
    assert rec["iterations"] == 1, rec
    assert rec["models"] == 2, rec
    # Refused on the role floor (1 < 3); two models so the one-model fill does not apply.
    assert rec["roles"] == 1, rec
    assert rec["min_roles"] == 3, rec
    assert rec["min_models"] == 0, rec


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


def test_review_quorum_auto_bypasses_on_confirmed_fleet_exhaustion(tmp_path):
    """When review-cli's own capacity-check confirms EVERY configured provider is exhausted
    right now, a bar-short ship auto-proceeds WITHOUT ever consulting the manual Telegram hatch
    (RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM is deliberately left unset here, proving the hatch path
    is never even reached) -- Alex's repeated complaint that every PR hits the same manual
    prompt for one already-diagnosed fleet-wide outage."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-500",
            "SHIP_TEST_REVIEW_ITER": "1", "SHIP_TEST_REVIEW_ROLES": "1",
            "SHIP_TEST_CAPACITY_SUPPORTED": "1", "SHIP_TEST_CAPACITY_ALL_EXHAUSTED": "1",
            "SHIP_AUDIT_FILE": str(audit),
        },
    )
    assert r.returncode == 0, f"a confirmed fleet-wide outage must auto-proceed\n{r.stdout}\n{r.stderr}"
    assert "auto-proceeding without a manual hatch prompt" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout
    rec = json.loads(audit.read_text().strip().splitlines()[0])
    assert rec["decision"] == "auto-proceed:capacity-outage", rec
    assert rec["task_code"] == "HYP-500", rec


def test_review_quorum_partial_exhaustion_still_blocks(tmp_path):
    """A capacity-check that reports at least one AVAILABLE provider (not everything exhausted)
    must change nothing: same bar-short refusal as without the capacity-check at all, proving
    partial exhaustion never triggers the auto-bypass."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-501",
            "SHIP_TEST_REVIEW_ITER": "1", "SHIP_TEST_REVIEW_ROLES": "1",
            "SHIP_TEST_CAPACITY_SUPPORTED": "1",
        },
    )
    assert r.returncode != 0, f"partial exhaustion must not auto-proceed\n{r.stdout}\n{r.stderr}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "HYP-501" in r.stderr, r.stderr
    assert "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM" in r.stderr, r.stderr
    assert "auto-proceeding" not in r.stdout, r.stdout
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_real_ci_failure_still_blocks_even_with_all_exhausted_capacity(
    repo_with_pr_worktree, tmp_path
):
    """A genuine (non-outage-shaped) CI failure -- one check red, one green, 50% below the 80%
    ci_appears_structurally_down threshold -- still refuses via the green-CI gate, even when
    review-cli's capacity-check would report every provider exhausted. The green-CI gate runs
    UNCONDITIONALLY earlier in ship.sh than the review-quorum gate, so this proves the new
    capacity auto-bypass can never be used to sneak past a real test failure: the review-quorum
    gate (and its capacity check) is never even reached."""
    main, _wt = repo_with_pr_worktree
    gh = _fake_gh_partial_fail_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    custom_path = f"{rv}{os.pathsep}{gh}{os.pathsep}{os.environ['PATH']}"
    r = _run_ship_cidown(
        main, gh,
        env_extra={
            "PATH": custom_path,
            "SHIP_TEST_CAPACITY_SUPPORTED": "1", "SHIP_TEST_CAPACITY_ALL_EXHAUSTED": "1",
        },
    )
    assert r.returncode != 0, (
        f"a genuine CI failure must block regardless of capacity status\n{r.stdout}\n{r.stderr}"
    )
    assert "--known-flake" in r.stderr, r.stderr
    assert "merged #1" not in r.stdout, r.stdout
    assert "auto-proceed" not in r.stdout, (
        "the capacity auto-bypass must never even be reached on a real CI failure\n" + r.stdout
    )


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
    """#242 / #644: empty evidence (0 iterations, 0 roles, 0 models) with a forged
    `passed:true` must FAIL. Distinct from the padded-astra case (3 roles, 0 models),
    which is complete role coverage and must pass."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-242",
            "SHIP_TEST_REVIEW_ITER": "0",
            "SHIP_TEST_REVIEW_MODELS": "0",
            "SHIP_TEST_REVIEW_ROLES": "0",
            "SHIP_TEST_REVIEW_FORCE_PASSED": "true",
        },
    )
    assert r.returncode != 0, f"empty evidence with a forged pass MUST be refused\n{r.stdout}\n{r.stderr}"
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
    # The confirmed line must show the REAL iteration/role counts (4 iterations across the
    # default 3 roles), not 0/0. No SHIP_REVIEW_QUORUM_MIN_MODELS was set, so the model-count
    # (still 3, real) is not part of the default role-based message (review-cli#246).
    assert "4 iterations across 3 roles" in r.stdout, r.stdout
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
            "SHIP_TEST_REVIEW_ROLES": "0",
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


# --- role-based quorum (review-cli#246): roles are now the PRIMARY/default gate, an explicit
# model floor is additionally enforced only when the operator opts in via
# SHIP_REVIEW_QUORUM_MIN_MODELS ------------------------------------------------------------

def test_review_quorum_role_based_default_passes_without_model_floor(tmp_path):
    """Enough distinct BOARD ROLES (3) but FEWER than 3 distinct models, and NO explicit
    SHIP_REVIEW_QUORUM_MIN_MODELS -> ship still AUTHORIZES: role-based coverage is the
    primary/default gate now, and there is no default model floor any more (review-cli#246)."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    args_log = tmp_path / "review-args.log"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-310",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "1",
            "SHIP_TEST_REVIEW_ARGS_LOG": str(args_log),
        },
    )
    assert r.returncode == 0, f"3 roles should authorize even with only 1 model (no model floor set)\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "3 iterations across 3 roles" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout
    # Prove ship did NOT send --min-models: passing it unconditionally would make review-cli
    # treat the model floor as explicitly requested too (it can't see "just ship's default").
    log_line = args_log.read_text().strip().splitlines()[0]
    assert "min_models_given=0" in log_line, log_line
    assert "min_roles_given=1" in log_line, log_line


def test_review_quorum_explicit_min_models_authorizes_when_both_floors_met(tmp_path):
    """The SUCCESS-side twin of the explicit-model-floor refusal test: 3 roles AND 3 models,
    with SHIP_REVIEW_QUORUM_MIN_MODELS explicitly set to 3 -> ship AUTHORIZES, the confirmed
    message names BOTH roles and models, and the audit line's min_models is the REAL enforced
    floor (3) rather than the '0 = unenforced' sentinel — the success-path branches (the
    'N roles and M models' message, and a non-zero min_models on an `authorized` line) were
    previously untested; only the refusal side was covered."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-316",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "3",
            "SHIP_REVIEW_QUORUM_MIN_MODELS": "3",
            "SHIP_AUDIT_FILE": str(audit),
        },
    )
    assert r.returncode == 0, f"3 roles and 3 models with an explicit 3-model floor must authorize\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "3 iterations across 3 roles and 3 models" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout
    rec = json.loads(audit.read_text().strip().splitlines()[0])
    assert rec["decision"] == "authorized", rec
    assert rec["min_models"] == 3, rec
    assert rec["models"] == 3, rec
    assert rec["roles"] == 3, rec


def test_review_quorum_blank_min_models_counts_as_explicit_opt_in(tmp_path):
    """SHIP_REVIEW_QUORUM_MIN_MODELS set to the EMPTY STRING (not unset) must still count as
    opting in to the model floor — the README's own subtlest claim: `${VAR+x}` distinguishes
    "genuinely unset" from "set to anything, even blank", and only genuinely unsetting the var
    skips the model floor. A regression to a plain `[ -n "$VAR" ]` truthiness check would
    silently treat a blank value as "not set" and authorize here; this pins the correct
    behavior: blank still means explicit, so a 1-model record refuses at the clamped floor 3."""
    import json

    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "ship-audit.jsonl"
    args_log = tmp_path / "review-args.log"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-317",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "1",
            "SHIP_REVIEW_QUORUM_MIN_MODELS": "",
            "SHIP_AUDIT_FILE": str(audit),
            "SHIP_TEST_REVIEW_ARGS_LOG": str(args_log),
        },
    )
    assert r.returncode != 0, f"a blank MIN_MODELS must still count as explicit and enforce the clamped floor\n{r.stdout}\n{r.stderr}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "1/3 distinct models" in r.stderr, r.stderr
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"
    rec = json.loads(audit.read_text().strip().splitlines()[0])
    assert rec["decision"] == "refused", rec
    # The CLAMPED floor (3), not the blank raw value, is what ship actually sends to review-cli.
    log_line = args_log.read_text().strip().splitlines()[0]
    assert "min_models_given=1" in log_line, log_line
    assert "minmodels=3" in log_line, log_line
    assert rec["min_models"] == 3, rec


def test_review_quorum_explicit_min_models_still_enforced_alongside_roles(tmp_path):
    """The SAME 3-roles / 1-model record as above, but WITH SHIP_REVIEW_QUORUM_MIN_MODELS
    explicitly set to 3 -> ship now REFUSES: an operator who explicitly asks for a model floor
    gets it honored in addition to the role floor (AND logic, review-cli#246 PR #246/#249/#252),
    it is never silently outvoted by role coverage."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    args_log = tmp_path / "review-args.log"
    audit = tmp_path / "ship-audit.jsonl"
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-311",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
            "SHIP_TEST_REVIEW_MODELS": "1",
            "SHIP_REVIEW_QUORUM_MIN_MODELS": "3",
            "SHIP_TEST_REVIEW_ARGS_LOG": str(args_log),
            "SHIP_AUDIT_FILE": str(audit),
        },
    )
    assert r.returncode != 0, f"an explicit min-models floor must still be enforced\n{r.stdout}\n{r.stderr}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "1/3 distinct models" in r.stderr, r.stderr
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"
    log_line = args_log.read_text().strip().splitlines()[0]
    assert "min_models_given=1" in log_line, log_line
    # The audit line's min_models is the REAL enforced floor (3), not the 0 "unenforced" sentinel
    # — proving the sentinel actually distinguishes the two cases, not just always reading 0.
    import json

    rec = json.loads(audit.read_text().strip().splitlines()[0])
    assert rec["decision"] == "refused", rec
    assert rec["min_models"] == 3, rec
    assert rec["models"] == 1, rec


def test_review_quorum_role_only_regression_matches_pre_existing_behavior(tmp_path):
    """Regression guard: a caller that doesn't care about roles at all, but has enough of BOTH
    (3 iterations / 3 models / 3 roles) and never touches SHIP_REVIEW_QUORUM_MIN_MODELS, still
    authorizes exactly as before this change — backward compatible."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-312",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_MODELS": "3",
            "SHIP_TEST_REVIEW_ROLES": "3",
        },
    )
    assert r.returncode == 0, f"a real 3x3x3 record should still authorize unchanged\n{r.stdout}\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_review_quorum_role_floor_enforced_even_with_many_models(tmp_path):
    """Distinct models are plentiful (5) but distinct ROLES are below the floor (1 of 3) -> ship
    REFUSES: the role floor is ALWAYS required now (the primary/default mechanism), independent
    of how many models were involved."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-313",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_MODELS": "5",
            "SHIP_TEST_REVIEW_ROLES": "1",
        },
    )
    assert r.returncode != 0, f"a below-floor role count must refuse regardless of model count\n{r.stdout}\n{r.stderr}"
    assert "bar NOT met" in r.stderr, r.stderr
    assert "1/3 distinct roles" in r.stderr, r.stderr
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_min_roles_below_floor_cannot_weaken_bar(tmp_path):
    """SHIP_REVIEW_QUORUM_MIN_ROLES=1 (an attempt to weaken the mandatory role floor) is clamped
    back up to the hard floor 3, same as MIN_ITER/MIN_MODELS (#242) — a genuine 1-role record
    still refuses '1/3', not '1/1'."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-314",
            "SHIP_REVIEW_QUORUM_MIN_ROLES": "1",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "1",
        },
    )
    assert r.returncode != 0, f"a 1-role record under a clamped-to-3 floor must refuse\n{r.stdout}\n{r.stderr}"
    assert "hard floor" in r.stderr, f"expected a floor-clamp warning raising 1 to 3\n{r.stderr}"
    assert "1/3 distinct roles" in r.stderr, f"floor must be enforced at 3, not the weakened 1\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" not in r.stdout, r.stdout
    assert "merged #1" not in r.stdout, "must refuse BEFORE merging"


def test_review_quorum_min_roles_raised_above_floor_is_honored(tmp_path):
    """SHIP_REVIEW_QUORUM_MIN_ROLES=5 (raising the bar, which IS allowed) with only 4 real roles
    refuses '4/5' — proving the raise-only direction works, not just the raise-only clamp."""
    main, _wt = _make_repo_with_branch(tmp_path, "feat")
    gh = _fake_gh_quorum_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    r = _run_ship_quorum(
        main, gh, rv,
        env_extra={
            "REVIEW_TASK_CODE": "HYP-315",
            "SHIP_REVIEW_QUORUM_MIN_ROLES": "5",
            "SHIP_TEST_REVIEW_ITER": "3",
            "SHIP_TEST_REVIEW_ROLES": "4",
        },
    )
    assert r.returncode != 0, f"a raised 5-role bar with only 4 real roles must refuse\n{r.stdout}\n{r.stderr}"
    assert "4/5 distinct roles" in r.stderr, r.stderr
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
                    trusted=None, dry_run=None):
    """Call review_quorum_hatch.main() with resolve_home monkeypatched to a fixed dir and the
    hatch env set. Returns the exit code. `resolve_home` is the dir the helper will treat as the
    account's real home (where it looks for the rig config's tg_ctl.tg_ctl_path override)."""
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
        body=f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
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
    assert "approved by tg-ctl ask" in rec.get("override_reason", ""), rec


def test_hatch_payload_reports_roles_not_zero_models(tmp_path, monkeypatch):
    """#644: when hatch DOES fire, Telegram context must lead with distinct_roles_passed.
    A padded-astra miss must not show `distinct_models: 0` / `iterations: 0` as the predicate
    — those are telemetry zeros that made Alex approve every ship."""
    question_file = tmp_path / "question.txt"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SHIP_HATCH_ROLES", "2")
    monkeypatch.setenv("SHIP_HATCH_ITER", "0")
    monkeypatch.setenv("SHIP_HATCH_MODELS", "0")
    rc = _run_hatch_main(
        monkeypatch, tmp_path,
        request="Roles short; asking for a one-time bypass.",
        resolve_home=home, audit=audit,
    )
    assert rc == 0, "live-approved hatch must return 0"
    question = question_file.read_text()
    assert "distinct_roles_passed" in question, question
    assert "distinct_models: 0" not in question, question
    assert "iterations: 0" not in question, question


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


def test_hatch_ask_is_critical_and_honours_a_reply_after_the_old_wait_bound(tmp_path, monkeypatch):
    """#873: the ship hatch is a CRITICAL ask. The outbound ButtonRequest JSON carries
    `"critical": true` (what lifts tg-ctl's own 115 s ASK_TOTAL_TIMEOUT_MS and enables late-approval
    grants), and the helper has no local wait bound: SHIP_HATCH_TIMEOUT_S=1 no longer cuts off a
    reply that arrives later — the approval is honoured instead of discarded."""
    import json

    payload_file = tmp_path / "payload.json"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=f'cat > "{payload_file}"\nsleep 2\n' + _ALLOW_REPLY_SH,
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SHIP_HATCH_TIMEOUT_S", "1")
    monkeypatch.setenv("SHIP_HATCH_PROCESS_MARGIN_S", "0")
    rc = _run_hatch_main(
        monkeypatch, tmp_path, request="Security hotfix; Alex answers after the old 1s bound.",
        resolve_home=home, audit=audit,
    )
    assert rc == 0, "a critical ask must wait for the reply, not time out locally"
    payload = json.loads(payload_file.read_text())
    assert payload["critical"] is True, payload
    assert payload["hookId"] == "ship-review-quorum", payload
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "bypass:approved", rec


def test_hatch_silence_is_still_denied_in_critical_mode(tmp_path, monkeypatch):
    """Critical mode changes how long the helper waits, never what counts as approval: tg-ctl
    exiting with no decision (the daemon gave up / Alex never answered) still returns 1."""
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body="exit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_hatch_main(
        monkeypatch, tmp_path, request="No response expected — testing the silence path.",
        resolve_home=home, audit=audit,
    )
    assert rc == 1, "silence is not approval"


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
    env.pop("SHIP_TEST_DISABLE_REVIEW_QUORUM", None)
    env["REVIEW_TASK_CODE"] = "HYP-200"
    env["SHIP_TEST_REVIEW_ITER"] = "1"
    env["SHIP_TEST_REVIEW_MODELS"] = "2"
    env["SHIP_TEST_REVIEW_ROLES"] = "1"
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
    # The launcher resolves ship.py + shiplib/ from its own directory, so copy all three.
    shutil.copy2(_SHIP, ship_copy)
    shutil.copy2(_SHIP_DIR / "ship.py", dst / "ship.py")
    shutil.copytree(_SHIP_DIR / "shiplib", dst / "shiplib", ignore=shutil.ignore_patterns("__pycache__"))
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


def _ship_copy_with_fake_hatch(tmp_path: Path, helper: str, body: str, *, skip_ci_ok: bool = False) -> Path:
    """A copy of the ship tree whose hatch helper `helper` (review_quorum_hatch.py,
    skip_ci_hatch.py or external_review_hatch.py) is replaced by a fake running `body`. Ship runs
    the helper from beside its own script, so this is the seam that stands in for a live Telegram
    approval, or for a broken helper that exits 0 without the APPROVED sentinel."""
    ship_copy = _ship_copy_with_helper(tmp_path, helper_exit=None)
    (ship_copy.parent / helper).write_text(body, encoding="utf-8")
    if skip_ci_ok:
        _install_fake_skip_ci_helper(ship_copy.parent)
    return ship_copy


_HELPER_EXITS_ZERO_SILENTLY = "import sys\nsys.exit(0)\n"
_HELPER_APPROVES = "import sys\nsys.stdout.write('APPROVED live-approved (test)\\n')\nsys.exit(0)\n"


def _run_ship_copy_short_bar(tmp_path, main, gh, rv, ship_copy, *, request, audit=None,
                             extra_args=(), env_extra=None):
    """Run a ship.sh copy against a SHORT bar (1/1) with a hatch request set."""
    env = dict(os.environ)
    env["PATH"] = _minimal_hermetic_path(gh, rv)
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"
    env.pop("SHIP_TEST_DISABLE_REVIEW_QUORUM", None)
    env["REVIEW_TASK_CODE"] = "HYP-200"
    env["SHIP_TEST_REVIEW_ITER"] = "1"
    env["SHIP_TEST_REVIEW_MODELS"] = "2"
    env["SHIP_TEST_REVIEW_ROLES"] = "1"
    env["RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM"] = request
    # --skip-ci is now hatch-gated too; request it so the fake approving skip_ci_hatch.py beside the
    # copy is consulted (deny-by-default otherwise refuses before the merge, masking the quorum path).
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "test skip-ci wiring"
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
    # ship.sh (not the helper) writes this fail-closed line, so it must carry the same
    # roles/min_roles/min_models fields as the other review-quorum decision types — this is the
    # ONLY non-dry-run path that exercises the shell-side bypass:denied call with real gate state
    # (the short bar now reports 1 role so the role floor is unmet and hatch fires; no explicit
    # model floor was set).
    assert rec["roles"] == 1, rec
    assert rec["min_roles"] == 3, rec
    assert rec["min_models"] == 0, rec


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
    ship_copy = _ship_copy_with_fake_hatch(tmp_path, "review_quorum_hatch.py", _HELPER_EXITS_ZERO_SILENTLY, skip_ci_ok=True)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([_minimal_hermetic_path(gh, rv)])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_QUORUM"] = "1"
    env.pop("SHIP_TEST_DISABLE_REVIEW_QUORUM", None)
    env["REVIEW_TASK_CODE"] = "HYP-200"
    env["SHIP_TEST_REVIEW_ITER"] = "1"
    env["SHIP_TEST_REVIEW_MODELS"] = "2"
    env["SHIP_TEST_REVIEW_ROLES"] = "1"
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM"] = "genuine reason but python3 is faked"
    r = _sh("bash", str(ship_copy), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
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


def _run_skip_ci_hatch_main(monkeypatch, *, request, resolve_home, audit, dry_run=None):
    """Call skip_ci_hatch.main() with resolve_home monkeypatched to a fixed fake home (where the
    lib looks for the rig config's tg_ctl.tg_ctl_path override) and the hatch env set. Returns the exit code."""
    mod = _load_skip_ci_hatch_module()
    monkeypatch.setattr(mod, "resolve_home", lambda: str(resolve_home))
    monkeypatch.setattr(mod.hatch_escalation, "resolve_home", lambda: str(resolve_home))
    monkeypatch.setenv("RIG_HATCH_REQUEST_SHIP_SKIP_CI", request)
    monkeypatch.setenv("SHIP_AUDIT_FILE", str(audit))
    monkeypatch.setenv("SHIP_HATCH_PR", "1")
    monkeypatch.setenv("SHIP_HATCH_BRANCH", "feat")
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
        body=f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
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
    assert "approved by tg-ctl ask" in rec.get("override_reason", ""), rec


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


def test_skip_ci_hatch_ask_is_critical(tmp_path, monkeypatch):
    """#873: the outbound ButtonRequest JSON carries `"critical": true` so tg-ctl drops its own
    115 s ASK_TOTAL_TIMEOUT_MS for this ask and a late Approve can mint a grant."""
    import json

    payload_file = tmp_path / "payload.json"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl", body=f'cat > "{payload_file}"\n' + _ALLOW_REPLY_SH,
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_skip_ci_hatch_main(
        monkeypatch, request="CI Actions billing suspended; local gates green; hotfix must ship.",
        resolve_home=home, audit=audit,
    )
    assert rc == 0
    payload = json.loads(payload_file.read_text())
    assert payload["critical"] is True, payload
    assert payload["hookId"] == "ship-skip-ci", payload


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
    ship_copy = _ship_copy_with_fake_hatch(tmp_path, "skip_ci_hatch.py", _HELPER_EXITS_ZERO_SILENTLY)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "genuine reason but python3 is faked"
    r = _sh("bash", str(ship_copy), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
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
    ship_copy = _ship_copy_with_fake_hatch(tmp_path, "skip_ci_hatch.py", _HELPER_APPROVES)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(tmp_path / "audit.jsonl")
    env["RIG_HATCH_REQUEST_SHIP_SKIP_CI"] = "CI billing suspended; hotfix must ship"
    r = _sh("bash", str(ship_copy), "1", "--skip-ci", "--no-screenshot-ok", "test", cwd=main, env=env)
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


# --- external-review hatch escalation (deny-by-default; mirrors the skip-ci hatch tests above) --
# Same rationale as the skip-ci block above: the live approve/deny/dry-run MECHANICS are tested
# IN-PROCESS against the real ci/ship/external_review_hatch.py helper with resolve_home
# monkeypatched to a fake home (the only way to exercise a controllable tg-ctl); deny-by-default
# refusal end-to-end through a real ship.sh subprocess is covered separately below.


def _load_external_review_hatch_module():
    """Import ci/ship/external_review_hatch as a module (fresh each call so a monkeypatched
    resolve_home never leaks between tests)."""
    import importlib

    if _HATCH_MOD_DIR not in sys.path:
        sys.path.insert(0, _HATCH_MOD_DIR)
    mod = importlib.import_module("external_review_hatch")
    return importlib.reload(mod)


def _run_external_review_hatch_main(monkeypatch, *, request, resolve_home, audit, dry_run=None):
    """Call external_review_hatch.main() with resolve_home monkeypatched to a fixed fake home and
    the hatch env set. Returns the exit code."""
    mod = _load_external_review_hatch_module()
    monkeypatch.setattr(mod, "resolve_home", lambda: str(resolve_home))
    monkeypatch.setattr(mod.hatch_escalation, "resolve_home", lambda: str(resolve_home))
    monkeypatch.setenv("RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW", request)
    monkeypatch.setenv("SHIP_AUDIT_FILE", str(audit))
    monkeypatch.setenv("SHIP_HATCH_PR", "1")
    monkeypatch.setenv("SHIP_HATCH_BRANCH", "feat")
    if dry_run is None:
        monkeypatch.delenv("SHIP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("SHIP_DRY_RUN", dry_run)
    return mod.main()


def test_external_review_hatch_blank_denied_without_tg_contact(tmp_path, monkeypatch):
    """A blank RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW is invalid: denied without contacting
    tg-ctl, exit 1, audits external-review:bypass:denied."""
    import json

    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(monkeypatch, request="", resolve_home=home, audit=audit)
    assert rc == 1, "a blank hatch value must be denied"
    assert not marker.exists(), "tg-ctl must NOT be contacted for a blank hatch request"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:bypass:denied", rec
    assert rec["gate"] == "external-review", rec


def test_external_review_hatch_bare_flag_denied_without_tg_contact(tmp_path, monkeypatch):
    """A bare truthy flag ('1'/'yes'/'true') is NOT a justification: denied without tg-ctl."""
    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(monkeypatch, request="1", resolve_home=home, audit=audit)
    assert rc == 1, "a bare '1' must be denied — a self-set flag is not an approval"
    assert not marker.exists(), "tg-ctl must NOT be contacted for a bare flag"


def test_external_review_hatch_reason_triggers_tg_ask_and_approval_returns_0(tmp_path, monkeypatch):
    """A real justification runs `tg-ctl ask`; on Alex's live approval the helper returns 0, the
    question carries the hook id + justification + PR context, and it audits
    external-review:bypass:approved."""
    import json

    question_file = tmp_path / "question.txt"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=f'cat > "{question_file}"\n' + _ALLOW_REPLY_SH,
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(
        monkeypatch,
        request="reviewer unreachable, genuinely urgent hotfix HYP-999",
        resolve_home=home, audit=audit,
    )
    assert rc == 0, "a live-approved external-review hatch must return 0"
    question = question_file.read_text()
    assert "ship-external-review" in question, question
    assert "reviewer unreachable" in question, question
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:bypass:approved", rec
    assert "approved by tg-ctl ask" in rec.get("override_reason", ""), rec


def test_external_review_hatch_denial_returns_1_and_audits(tmp_path, monkeypatch):
    """When Alex declines (tg-ctl exits non-zero) the helper returns 1 and audits
    external-review:bypass:denied."""
    import json

    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body='printf "declined\\n"\nexit 1\n')
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(
        monkeypatch, request="please just let this through", resolve_home=home, audit=audit,
    )
    assert rc == 1, "a declined external-review hatch must return 1"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:bypass:denied", rec


def test_external_review_hatch_ask_is_critical(tmp_path, monkeypatch):
    """#873: the outbound ButtonRequest JSON carries `"critical": true` so tg-ctl drops its own
    115 s ASK_TOTAL_TIMEOUT_MS for this ask and a late Approve can mint a grant."""
    import json

    payload_file = tmp_path / "payload.json"
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl", body=f'cat > "{payload_file}"\n' + _ALLOW_REPLY_SH,
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(
        monkeypatch, request="reviewer unreachable, genuinely urgent hotfix HYP-999",
        resolve_home=home, audit=audit,
    )
    assert rc == 0
    payload = json.loads(payload_file.read_text())
    assert payload["critical"] is True, payload
    assert payload["hookId"] == "ship-external-review", payload


def test_external_review_hatch_retry_redeems_late_approval_grant(tmp_path, monkeypatch):
    """#873: a retry with `RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW=approved:hgr-…` (the grant a
    late Approve minted after the first ship's ask expired) verifies the grant with
    `tg-ctl ask --verify-grant <id> --hook ship-external-review` instead of posting a NEW ask,
    returns 0 and audits external-review:bypass:approved. The retry's operation fingerprint is
    IDENTICAL to the original ask's (same PR/branch/repo) — that is what lets tg-ctl bind the
    late grant to this ship and not another."""
    import json

    grant_id = "hgr-85d3654be317"
    argv_file = tmp_path / "argv.txt"
    payload_file = tmp_path / "payload.json"
    reply = json.dumps({
        "requestId": "hatch-ship-external-review-original",
        "approvalId": grant_id,
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        },
    })
    tg = _write_fake_tg_ctl(
        tmp_path, name="tg-ctl",
        body=(
            f'printf "%s\\n" "$@" > "{argv_file}"\n'
            f'cat > "{payload_file}"\n'
            f"printf '%s' '{reply}'\nexit 0\n"
        ),
    )
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"

    # The original ship: a written justification posts a live (critical) ask.
    rc = _run_external_review_hatch_main(
        monkeypatch, request="reviewer unreachable, genuinely urgent hotfix HYP-999",
        resolve_home=home, audit=audit,
    )
    assert rc == 0
    original = json.loads(payload_file.read_text())
    assert argv_file.read_text().splitlines() == ["ask", "--agent", original["agent"]]
    assert original["critical"] is True, original

    # The retry of the same ship, carrying the grant the late Approve minted.
    rc = _run_external_review_hatch_main(
        monkeypatch, request=f"approved:{grant_id}", resolve_home=home, audit=audit,
    )
    assert rc == 0, "a verified late-approval grant must approve the retry"
    argv = argv_file.read_text().splitlines()
    assert argv == ["ask", "--verify-grant", grant_id, "--hook", "ship-external-review"], argv
    retry = json.loads(payload_file.read_text())
    assert retry["operation"]["kind"] == "ship-external-review", retry
    assert retry["operation"]["fingerprint"], retry
    assert retry["operation"]["fingerprint"] == original["operation"]["fingerprint"]
    assert retry["operation"]["cwd"] == original["operation"]["cwd"]
    rec = json.loads(audit.read_text().strip().splitlines()[-1])
    assert rec["decision"] == "external-review:bypass:approved", rec
    assert grant_id in rec.get("override_reason", ""), rec


def test_external_review_hatch_dry_run_reason_approves_without_tg_contact(tmp_path, monkeypatch):
    """`--dry-run` is a preview that must NOT fire a live Telegram round-trip: a written
    justification yields an APPROVED preview sentinel (exit 0) while contacting NO tg-ctl."""
    marker = tmp_path / "tg-called"
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body=f"touch {marker}\nexit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(
        monkeypatch, request="preview: would ship with zero reviews",
        resolve_home=home, audit=audit, dry_run="1",
    )
    assert rc == 0, "a dry-run preview with a real justification must return 0"
    assert not marker.exists(), "dry-run must NOT contact tg-ctl (no live round-trip in a preview)"
    assert not audit.exists() or audit.read_text().strip() == "", "dry-run must not write a real audit line"


def test_external_review_hatch_dry_run_blank_still_denied(tmp_path, monkeypatch):
    """Deny-by-default holds even in dry-run: a blank/bare justification is DENIED in preview too."""
    tg = _write_fake_tg_ctl(tmp_path, name="tg-ctl", body="exit 0\n")
    home = _fake_home_with_tg_ctl(tmp_path, tg)
    audit = tmp_path / "audit.jsonl"
    rc = _run_external_review_hatch_main(
        monkeypatch, request="", resolve_home=home, audit=audit, dry_run="1",
    )
    assert rc == 1, "a blank justification must be denied even in dry-run"


# --- external-review gate: refuse a merge with ZERO GitHub-side PR reviews (HYP-1380/#764) ----
# _fake_gh_dir's default answers ONE qualifying review for `--json reviews` (SHIP_TEST_REVIEW_COUNT
# defaults to 1) so every pre-existing full-pipeline test above reaches the merge unaffected; these
# tests explicitly set SHIP_TEST_REVIEW_COUNT=0 to reproduce the PR #764 signature.


def test_external_review_passes_with_a_qualifying_review(repo_with_two_worktrees, tmp_path):
    """The default fixture answer (one qualifying review) lets a normal ship proceed to merge —
    documents the happy path this gate must never block."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    r = _run_ship(main, bindir)
    assert r.returncode == 0, f"a PR with a qualifying review must not be blocked\n{r.stdout}\n{r.stderr}"
    assert "external-review gate OK" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout, r.stdout


def test_external_review_deny_by_default_refuses_before_merge(repo_with_two_worktrees, tmp_path):
    """End-to-end reproduction of the PR #764 gap: zero GitHub reviews, no
    RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW set -> ship refuses (deny-by-default) BEFORE any merge,
    and audits a plain 'refused' decision."""
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
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env.pop("RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"deny-by-default: zero reviews must refuse without a hatch\n{r.stdout}\n{r.stderr}"
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:refused", rec
    assert rec["gate"] == "external-review", rec


def test_external_review_auto_bypasses_on_confirmed_fleet_exhaustion(repo_with_two_worktrees, tmp_path):
    """Zero GitHub reviews AND review-cli's own capacity-check confirms EVERY configured
    provider is exhausted right now -> ship auto-proceeds WITHOUT ever consulting the manual
    Telegram hatch (RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW is deliberately left unset here,
    proving the hatch path is never even reached), and audits the dedicated capacity-outage
    decision."""
    import json

    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{rv}{os.pathsep}{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["SHIP_TEST_CAPACITY_SUPPORTED"] = "1"
    env["SHIP_TEST_CAPACITY_ALL_EXHAUSTED"] = "1"
    env.pop("RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode == 0, f"a confirmed fleet-wide outage must auto-proceed\n{r.stdout}\n{r.stderr}"
    assert "auto-proceeding without a manual hatch prompt" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:auto-proceed:capacity-outage", rec
    assert rec["gate"] == "external-review", rec


def test_external_review_partial_exhaustion_still_blocks(repo_with_two_worktrees, tmp_path):
    """A capacity-check that reports at least one AVAILABLE provider (not everything exhausted)
    must change nothing: zero GitHub reviews still refuses exactly as today (deny-by-default,
    no hatch consulted, no auto-bypass)."""
    import json

    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    rv = _fake_review_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{rv}{os.pathsep}{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["SHIP_TEST_CAPACITY_SUPPORTED"] = "1"
    env.pop("RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"partial exhaustion must not auto-proceed\n{r.stdout}\n{r.stderr}"
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW" in r.stderr, r.stderr
    assert "auto-proceeding" not in r.stdout, r.stdout
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:refused", rec


def test_external_review_gh_query_failure_refuses_with_friendly_message_not_silent_abort(
    repo_with_two_worktrees, tmp_path
):
    """Regression for review-cli GLM finding F1 (GH-459 iteration 2): ship.sh runs under
    `set -euo pipefail`, and the `REVIEW_COUNT=$(gh pr view ... --json reviews ...)` substitution
    was the one `$(gh ...)` call in the file with no errexit guard (every sibling gate guards
    theirs). A real `gh` failure (expired auth, rate limit, network blip) would abort the whole
    script silently at that line -- gh's raw exit code, no 'Refusing: could not query ...' message,
    no audit line -- instead of reaching the friendly fail-closed refusal a few lines down. Assert
    the refusal message and its audit line ARE reached when the gh query fails, proving the
    `|| REVIEW_COUNT=""` guard fixed it (a pre-fix run would just exit 1 with none of this)."""
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
    env["SHIP_TEST_REVIEW_QUERY_FAIL"] = "1"
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"a failed reviews query must refuse, not merge\n{r.stdout}\n{r.stderr}"
    assert "could not query PR" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"
    rec = json.loads(audit.read_text().strip())
    assert rec["decision"] == "external-review:refused", rec
    assert rec["gate"] == "external-review", rec


def test_external_review_env_enabled_zero_does_not_disable_gate(repo_with_two_worktrees, tmp_path):
    """agent-tools#624: SHIP_EXTERNAL_REVIEW_ENABLED=0 is no longer an ops off-switch.
    Env alone must not waive the zero-review refusal (the ambient-export hole #528/#609
    left open). Durable disable is a base-branch .ship-config line; one-time bypass is
    RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["SHIP_EXTERNAL_REVIEW_ENABLED"] = "0"
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, (
        f"env SHIP_EXTERNAL_REVIEW_ENABLED=0 must NOT waive a zero-review PR\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "external-review gate disabled" not in r.stdout, r.stdout
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_external_review_env_short_alias_zero_does_not_disable_gate(repo_with_two_worktrees, tmp_path):
    """agent-tools#624: SHIP_EXTERNAL_REVIEW=0 (the short alias) is likewise ignored as
    an env off-switch. Same contract as the _ENABLED form."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["SHIP_EXTERNAL_REVIEW"] = "0"
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, (
        f"env SHIP_EXTERNAL_REVIEW=0 must NOT waive a zero-review PR\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "external-review gate disabled" not in r.stdout, r.stdout
    assert "[fake gh] merged" not in r.stdout, "must refuse BEFORE merging"


def test_external_review_disabled_via_committed_ship_config(repo_with_two_worktrees, tmp_path):
    """agent-tools#600: a `.ship-config` line MERGED TO THE BASE BRANCH is a durable, per-repo
    alternative to the env var -- one a repo owner can review and land like any other code
    change, and one a different agent session or test process never inherits (unlike
    agent-tools#528's machine-wide `~/.zshenv` export, which broke this script's own gate tests by
    leaking into them). Must disable the gate with NO env var set at all."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    (main / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "disable the external-review gate for this repo", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env.pop("SHIP_EXTERNAL_REVIEW_ENABLED", None)
    env.pop("SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode == 0, f"a committed .ship-config off-switch must let a zero-review PR merge\n{r.stdout}\n{r.stderr}"
    assert "external-review gate disabled by the committed .ship-config on origin/main (SHIP_EXTERNAL_REVIEW=0)" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout, r.stdout


def test_external_review_ship_config_off_switch_wins_even_with_env_explicitly_enabled(
    repo_with_two_worktrees, tmp_path
):
    """A base-branch `.ship-config` SHIP_EXTERNAL_REVIEW=0 remains the durable per-repo
    off-switch after #624 killed the env path. An explicit SHIP_EXTERNAL_REVIEW=1 in the
    environment is ignored — env can neither disable nor resurrect the gate."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    (main / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "disable the external-review gate for this repo", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["SHIP_EXTERNAL_REVIEW"] = "1"
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode == 0, f"a committed .ship-config off-switch must still disable the gate\n{r.stdout}\n{r.stderr}"
    assert "external-review gate disabled by the committed .ship-config on origin/main (SHIP_EXTERNAL_REVIEW=0)" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout, r.stdout


def test_external_review_ship_config_ignored_when_uncommitted(repo_with_two_worktrees, tmp_path):
    """The audited-config contract applies here too (same property _ship_config_load already
    enforces for SHIP_ACCEPTANCE_GATE/SHIP_LOCAL_TEST_CMD): only content actually merged to the
    base branch counts, so a working-tree-only .ship-config cannot silently disable a
    merge-safety gate."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    (main / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env.pop("SHIP_EXTERNAL_REVIEW_ENABLED", None)
    env.pop("SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"an uncommitted .ship-config must NOT disable the gate\n{r.stdout}\n{r.stderr}"
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_external_review_ship_config_on_feature_branch_only_does_not_disable_gate(
    repo_with_two_worktrees, tmp_path
):
    """Security regression (review-cli finding, agent-tools#600 round 1): a `.ship-config`
    committed ONLY on the shipping PR's own branch -- never merged to the base -- must NOT disable
    the gate. Reading the off-switch from the PR's own HEAD would let that SAME PR grant itself an
    exemption from "at least one GitHub review exists", recreating PR #764 (HYP-1380) inside the
    fix for #764. Ships from the `feat` worktree (the realistic `gh ship` shape: run from the PR's
    own checkout), with the line committed only on `feat`, never on `main`."""
    main, wt1, wt2 = repo_with_two_worktrees
    (wt1 / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=wt1)
    _git("commit", "-qm", "attempt to disable the external-review gate from the PR itself", cwd=wt1)
    _git("push", "-q", "origin", "feat", cwd=wt1)
    # wt2 shares the `feat` branch ref (the fixture's exit-128 setup): the branch pointer moved
    # when wt1 committed, but git does not auto-sync ANOTHER worktree's index/working tree when a
    # shared branch advances elsewhere. Force wt2 back in sync with the new tip so ship.sh's OWN
    # "every worktree on this branch must be clean" preflight (unrelated to this test) does not
    # trip first and mask the assertion below.
    _git("reset", "--hard", "HEAD", cwd=wt2)
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env.pop("SHIP_EXTERNAL_REVIEW_ENABLED", None)
    env.pop("SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=wt1, env=env)
    assert r.returncode != 0, (
        f"a .ship-config committed only on the PR's own branch must NOT disable the gate\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_external_review_ship_config_ignored_on_foreign_repo_invoke(repo_with_two_worktrees, tmp_path):
    """A `.ship-config` disabling the gate lives in THIS checkout's own repo -- for a `--repo
    other/repo` invocation that is a DIFFERENT repo's policy, so it must never waive the gate for
    the foreign target (the same class of bug #600 exists to close: the wrong repo's setting
    silently taking effect) even though the line IS genuinely merged to this repo's own base
    branch. Mirrors _acceptance_gate's own foreign-repo skip."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    (main / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "disable the external-review gate for THIS repo only", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env.pop("SHIP_EXTERNAL_REVIEW_ENABLED", None)
    env.pop("SHIP_EXTERNAL_REVIEW", None)
    env.pop("GH_REPO", None)
    env.pop("GH_SHIP_REPO", None)
    r = _sh(
        "bash", str(_SHIP), "1", "--repo", "owner/repo", "--no-screenshot-ok", "test",
        cwd=main, env=env,
    )
    assert r.returncode != 0, (
        f"a foreign --repo invocation must NOT be waived by this checkout's own .ship-config\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_external_review_ship_config_skipped_when_base_branch_unreadable(repo_with_two_worktrees, tmp_path):
    """Security regression (review-cli finding, agent-tools#600 round 3): unlike
    _empty_rollup_is_ci_outage (which safely falls back to $SHIP_DEFAULT_BRANCH on the same
    gh-query failure -- guessing wrong there only costs an extra local test run), this gate must
    NOT guess a fallback branch when `gh pr view --json baseRefName` fails. A PR that legitimately
    targets some OTHER branch could otherwise inherit the DEFAULT branch's off-switch and merge
    with zero reviews. On failure the .ship-config lookup must be skipped entirely and the gate
    must stay enabled -- matching --known-flake's existing 'unreadable base -> refuse' contract."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    (main / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "disable the external-review gate for this repo", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["SHIP_TEST_BASEREF_QUERY_FAIL"] = "1"
    env.pop("SHIP_EXTERNAL_REVIEW_ENABLED", None)
    env.pop("SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, (
        f"an unreadable base branch must NOT fall back to guessing $SHIP_DEFAULT_BRANCH's config\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "could not read PR #1's base branch" in r.stderr, r.stderr
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_external_review_ship_config_skipped_when_fetch_fails_even_with_stale_local_ref(
    repo_with_two_worktrees, tmp_path
):
    """Security regression (review-cli finding, agent-tools#600 round 3): a FAILED `git fetch`
    (network/auth blip) must not fall through to reading whatever `origin/main` ref already
    happens to exist locally from an earlier successful fetch. Pre-populates a stale local
    `refs/remotes/origin/main` that DOES carry the off-switch (from a real prior fetch while the
    remote was reachable), then breaks the `origin` remote so the fresh fetch this gate performs
    fails -- the gate must stay enabled, not trust the stale cache."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    (main / ".ship-config").write_text("SHIP_EXTERNAL_REVIEW=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=main)
    _git("commit", "-qm", "disable the external-review gate for this repo", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    # Populate the stale local cache while origin is still reachable (mirrors a long-lived
    # checkout that already ran ship successfully at least once before).
    _git("fetch", "-q", "origin", "main:refs/remotes/origin/main", cwd=main)
    # Now break `origin` so THIS run's fetch fails -- a nonexistent local path is a fast, offline
    # way to force a real `git fetch` failure without touching network.
    _git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"), cwd=main)
    bindir = _fake_gh_dir(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env.pop("SHIP_EXTERNAL_REVIEW_ENABLED", None)
    env.pop("SHIP_EXTERNAL_REVIEW", None)
    r = _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, (
        f"a failed fetch must NOT fall through to a stale local .ship-config cache\n"
        f"{r.stdout}\n{r.stderr}"
    )
    assert "ZERO GitHub-side reviews" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_external_review_fails_closed_when_python3_exits_zero_without_sentinel(repo_with_two_worktrees, tmp_path):
    """Security fail-closed: a fake/broken `python3` that exits 0 but prints NO `APPROVED` sentinel
    must NOT be mistaken for approval — ship refuses and audits bypass:denied. Mirrors the
    skip-ci gate's identical fail-closed guard."""
    import json

    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    audit = tmp_path / "audit.jsonl"
    ship_copy = _ship_copy_with_fake_hatch(tmp_path, "external_review_hatch.py", _HELPER_EXITS_ZERO_SILENTLY)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(audit)
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW"] = "genuine reason but python3 is faked"
    r = _sh("bash", str(ship_copy), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode != 0, f"a fake python3 exiting 0 must fail closed\n{r.stdout}\n{r.stderr}"
    assert "NOT approved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one audit line, got {lines}"
    rec = json.loads(lines[0])
    assert rec["decision"] == "external-review:bypass:denied", rec


def test_external_review_approved_hatch_reaches_merge_e2e(repo_with_two_worktrees, tmp_path):
    """End-to-end happy path for the escape valve: with an APPROVED external-review hatch, a real
    ship.sh proceeds to merge despite zero GitHub reviews. The hatch helper is stood in by a fake
    `python3` that emits the APPROVED sentinel (same e2e seam as the skip-ci hatch test — a
    subprocess can't inject a fake tg-ctl, authority is the real home)."""
    main, _wt1, _wt2 = repo_with_two_worktrees
    bindir = _fake_gh_dir(tmp_path)
    ship_copy = _ship_copy_with_fake_hatch(tmp_path, "external_review_hatch.py", _HELPER_APPROVES)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])
    env["SHIP_TEST_BRANCH"] = "feat"
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_AUDIT_FILE"] = str(tmp_path / "audit.jsonl")
    env["SHIP_TEST_REVIEW_COUNT"] = "0"
    env["RIG_HATCH_REQUEST_SHIP_EXTERNAL_REVIEW"] = "genuinely urgent, reviewer unreachable"
    r = _sh("bash", str(ship_copy), "1", "--no-screenshot-ok", "test", cwd=main, env=env)
    assert r.returncode == 0, f"an approved external-review hatch must reach the merge\n{r.stdout}\n{r.stderr}"
    assert "APPROVED by Alex" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout, r.stdout


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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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
        if printf '%s ' "$@" | grep -q -- '--json reviews'; then
          # Default: ONE qualifying GitHub review present (the common/good case) — the
          # external-review gate refuses to merge with zero, so a full-pipeline test shouldn't
          # need to know about it. Override via SHIP_TEST_REVIEW_COUNT (a dedicated test sets 0
          # to exercise the gate's refusal/hatch paths).
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s ' "$@" | grep -q headRefName; then
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


def test_resolve_thread_query_fetches_comment_bodies_for_severity_filter():
    assert "nodes{author{login} body}" in ci_gate.RESOLVE_THREAD_Q


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required for the eligibility-jq test")
def test_resolve_eligible_jq_excludes_high_severity_bot_threads():
    """Any high-severity bot comment excludes its outdated thread; ordinary nits remain eligible."""
    prog = ci_gate.RESOLVE_ELIGIBLE_JQ

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
    prog = ci_gate.RESOLVE_ELIGIBLE_JQ

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
