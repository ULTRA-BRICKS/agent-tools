"""Tests for ci/ship/ship.sh's post-merge task-cli notify hook (`_ship_notify_task_cli`).

Kept in its own file (not tests/test_ship.py) so this feature doesn't collide with the many
other in-flight worktrees editing that file — same hermetic approach: a real temp git repo +
a fake `gh` on PATH (no network) + a fake `task` binary that logs its own invocation so the
test can assert exactly what ship called it with, without a real task-cli install.

Requires bash + git. Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship_notify_task_cli.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

# See tests/test_ship.py's identical rationale: the review-quorum gate defaults ENABLED in the
# product but this file never exercises it (no `review`/`jq` quorum fixture here). Isolate via
# the test-only SHIP_TEST_DISABLE_REVIEW_QUORUM=1 — SHIP_REVIEW_QUORUM=0 is forbidden (#644).
os.environ.setdefault("SHIP_TEST_DISABLE_REVIEW_QUORUM", "1")
# The two pre-merge ticket gates (acceptance + magic-close, agent-tools#521) default ENABLED
# too and have their own fixtures in tests/test_ship_ticket_gates.py; off here so a fixture
# PR body that happens to say "Fixes HYP-999", or a fake `task` that logs every call, is not
# gated by a feature this file never set out to exercise.
os.environ.setdefault("SHIP_ACCEPTANCE_GATE", "0")
os.environ.setdefault("SHIP_MAGIC_CLOSE_GATE", "0")

# A minimal fake `gh` answering exactly the calls ship.sh makes on the notify path: the
# preflight (headRefName,...), the CI rollup (green, so the normal merge path is taken), the
# unresolved-review-threads graphql query (0), the merge itself, and the TWO batched `pr view`
# queries the notify hook adds (title+body combined, url+mergeCommit combined — ship.sh fetches
# each pair in a single `gh pr view --json a,b` call, not two separate ones). Each source is
# controlled by an env var so a test can pick exactly which one carries the ticket code, and
# whether a merge commit SHA is available. When SHIP_TEST_GH_CALL_LOG is set, every `pr view`
# call's raw args are appended there (one line each) so a test can assert the call COUNT.
_FAKE_GH = """\
#!/usr/bin/env bash
set -e
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        args="$*"
        [ -n "${SHIP_TEST_GH_CALL_LOG:-}" ] && printf '%s\\n' "$args" >> "${SHIP_TEST_GH_CALL_LOG}"
        if printf '%s' "$args" | grep -q -- '--json reviews'; then
          # See tests/test_ship.py's identical fixture rationale: default ONE qualifying
          # review so the external-review gate never blocks this file's notify-path tests.
          echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif printf '%s' "$args" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s' "$args" | grep -q statusCheckRollup; then
          printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
        elif printf '%s' "$args" | grep -q -- '--json title,body'; then
          printf '%s\\t%s\\n' "${SHIP_TEST_PR_TITLE:-}" "${SHIP_TEST_PR_BODY:-}"
        elif printf '%s' "$args" | grep -q -- '--json url,mergeCommit'; then
          printf '%s\\t%s\\n' "${SHIP_TEST_PR_URL-https://github.com/acme/widgets/pull/1}" "${SHIP_TEST_MERGE_SHA:-}"
        elif printf '%s' "$args" | grep -q -- '--json url'; then
          printf '%s\\n' "${SHIP_TEST_PR_URL-https://github.com/acme/widgets/pull/1}"
        else
          echo '[]'
        fi ;;
      diff) echo "src/a.py" ;;
      comment) : ;;
      merge) echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    # The only `gh api` call reached on this path (dwell is disabled via SHIP_REVIEW_DWELL=0):
    # the unresolved-review-threads query. Always answer 0 unresolved.
    echo "${SHIP_TEST_UNRESOLVED:-0}" ;;
  *) : ;;
esac
"""

# A fake `task` that logs its own argv (one line, space-joined) to $TASK_LOG and its OWN cwd
# (one line) to $TASK_LOG.pwd — a separate file, so the existing plain-argv format (and every
# test that does `calls[0].split()` expecting pure argv) is untouched — and exits
# $SHIP_TEST_TASK_EXIT (default 0). Lets a test assert exactly what ship called it with AND
# from where (ship.sh runs it via a subshell `cd "$ROOT"`, not a `-C` flag — see the ordering
# bug this guards against), and exercise "task itself fails" without a real task-cli install.
_FAKE_TASK = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${TASK_LOG}"
printf '%s\\n' "$PWD" >> "${TASK_LOG}.pwd"
exit "${SHIP_TEST_TASK_EXIT:-0}"
"""


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=dict(os.environ))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def repo_with_pr_worktree(tmp_path):
    """A repo on `main` with branch `feat` checked out in one worktree, plus an `origin`
    remote — the same shape tests/test_ship.py's fixture of the same name uses."""
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


def _bindir(tmp_path: Path, *, with_task: bool) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    if with_task:
        task = bindir / "task"
        task.write_text(_FAKE_TASK, encoding="utf-8")
        task.chmod(0o755)
    return bindir


def _path_without_real_task() -> str:
    """The ambient PATH with EVERY directory that contains a real `task` binary stripped out —
    not just the first `shutil.which` hit — so a "task not on PATH" test isn't accidentally
    satisfied by a developer machine that has more than one task-cli install shadowing each
    other on PATH (this machine has both ~/.local/bin/task and ~/.files/bin/task), which would
    make the test pass for the wrong reason (or fail outright, since a REAL task-cli doesn't
    understand this test's fake ticket codes/args)."""
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(d for d in dirs if not (d and Path(d, "task").exists()))


def _run_ship(main: Path, bindir: Path, tmp_path: Path, *, branch="feat", extra_env=None, extra_args=(), base_path=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{base_path if base_path is not None else env['PATH']}"
    env["SHIP_TEST_BRANCH"] = branch
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_REVIEW_DWELL"] = "0"
    env["TASK_LOG"] = str(tmp_path / "task.log")
    env["SHIP_TEST_GH_CALL_LOG"] = str(tmp_path / "gh-pr-view.log")
    # Explicitly override the process-wide default other test modules in this suite may have
    # already applied (tests/test_ship.py sets SHIP_TASK_NOTIFY_ENABLED=0 via os.environ.setdefault
    # so its own un-stubbed fixtures never invoke a real task-cli) — this file's whole point is
    # to exercise the notify feature itself, always with its OWN fake `task` on PATH.
    env["SHIP_TASK_NOTIFY_ENABLED"] = "1"
    if extra_env:
        env.update(extra_env)
    r = _sh(
        "bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra_args,
        cwd=main, env=env,
    )
    log_path = Path(env["TASK_LOG"])
    calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return r, calls


def _gh_pr_view_calls(tmp_path: Path) -> list[str]:
    """The `gh pr view ...` argv lines logged by the fake gh for the just-run `_run_ship` call
    (same tmp_path passed to it) — lets a test assert the batched-call-count property."""
    log_path = tmp_path / "gh-pr-view.log"
    return log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []


def test_notify_calls_mark_shipped_with_branch_derived_code(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing",
        extra_env={"SHIP_TEST_PR_URL": "https://github.com/acme/widgets/pull/1"},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "merged #1" in r.stdout
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]
    assert "--pr https://github.com/acme/widgets/pull/1" in calls[0]
    assert "notifying task-cli: HYP-931" in r.stdout


def test_notify_includes_commit_when_merge_commit_present(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing",
        extra_env={"SHIP_TEST_MERGE_SHA": "deadbeef1234"},
    )

    assert r.returncode == 0
    assert len(calls) == 1
    assert "--commit deadbeef1234" in calls[0]


def test_notify_falls_back_to_pr_title_when_branch_has_no_code(repo_with_pr_worktree, tmp_path):
    """The gap the review-quorum gate has (branch/body only, never title) must NOT be
    inherited here: a generic branch name with the ticket code only in the PR title must
    still be found."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "HYP-931: fix the thing", "SHIP_TEST_PR_BODY": "no code here"},
    )

    assert r.returncode == 0
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_finds_the_title_code_even_when_it_is_not_the_first_word(repo_with_pr_worktree, tmp_path):
    """Regression: a bare `read -r pr_title pr_body_local` (default IFS) splits on ANY
    whitespace, not just the tab `@tsv` inserts — it silently truncated pr_title to its FIRST
    WORD and dumped the rest into pr_body_local. A title like "Fix the thing (HYP-931)" would
    read as pr_title="Fix" (no match), and worse, a title like "UTF-8 fix for HYP-931" would
    give pr_title="UTF-8" — matching the digit-containing-but-wrong arm-2 pattern and reaching
    `task mark-shipped UTF-8` instead of the real HYP-931 (the exact wrong-ticket hazard the
    digit filter exists to catch, except the real code never even reached it). Must split on
    the tab alone (IFS=$'\\t')."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "Fix the thing (HYP-931)", "SHIP_TEST_PR_BODY": "no code here"},
    )

    assert r.returncode == 0
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_falls_back_to_pr_body_when_branch_and_title_have_no_code(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "refs HYP-931 for details"},
    )

    assert r.returncode == 0
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_passes_github_issue_ref_literal_to_mark_shipped(repo_with_pr_worktree, tmp_path):
    """A `Fixes #105` PR body (a repo that tracks work as plain GitHub issues) reaches
    `task mark-shipped` as the LITERAL `#105`, never a `GH-105` rewrite: task-cli routes a
    GitHub id by its leading `#` (`_route_id_to_project` checks `tid.startswith("#")` first), so
    a rewritten code would fall through to the Linear-prefix branch and be unroutable."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "Fixes #105 for real."},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped #105" in calls[0]
    assert "GH-105" not in calls[0]


def test_notify_passes_url_derived_issue_code_literal_to_mark_shipped(repo_with_pr_worktree, tmp_path):
    """A PR body that links its ticket as a full same-repo issue URL (the shape task-cli's
    `links` gate demands, agent-tools#564) reaches `task mark-shipped` as the literal `#105` —
    the same code a bare `Fixes #105` yields — never a `GH-105` rewrite."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={
            "SHIP_TEST_PR_TITLE": "fix the thing",
            "SHIP_TEST_PR_BODY": "Refs [#105](https://github.com/acme/widgets/issues/105) for real.",
            "SHIP_TEST_PR_URL": "https://github.com/acme/widgets/pull/1",
        },
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped #105" in calls[0]
    assert "GH-105" not in calls[0]


def test_notify_skips_an_issue_url_of_another_repo(repo_with_pr_worktree, tmp_path):
    """A link to ANOTHER repo's issue (a cross-repo companion) is not this PR's ticket: nothing
    is derived, so `task mark-shipped` is never called with a foreign issue number."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={
            "SHIP_TEST_PR_TITLE": "fix the thing",
            "SHIP_TEST_PR_BODY": "Companion of https://github.com/other-org/tg-cli/issues/301.",
            "SHIP_TEST_PR_URL": "https://github.com/acme/widgets/pull/1",
        },
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert calls == [], calls


def test_notify_normalizes_a_reused_gh_synthetic_code_to_the_task_cli_id_format(repo_with_pr_worktree, tmp_path):
    """Regression (real 404 hit live on tg-cli PR #305 and agent-tools PR #527): a `$TASK_CODE`
    already in the synthetic "GH-<n>" shape -- the convention
    agent-hooks/require-ticket-before-commit recognizes as a valid ticket reference, and the
    shape the review-quorum gate's own ticket matcher used to derive internally before #511 --
    reaches this function via the SAME reuse path `_ship_derive_task_code_for_notify` tries
    first (`candidate="${TASK_CODE:-}"`, to avoid a redundant `gh pr view` call when the gate
    already ran). Passing "GH-301" straight to `task mark-shipped` 404s: task-cli's
    `_route_id_to_project` routes a GitHub id by checking `tid.startswith("#")` first, so
    "GH-301" falls through to the Linear-team-prefix branch and is unroutable. It must reach
    `task mark-shipped` as the literal "#301" instead."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"TASK_CODE": "GH-301"},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped #301" in calls[0]
    assert "GH-301" not in calls[0]
    assert "normalizing task-cli notify code: GH-301 -> #301" in r.stderr


def test_notify_rejects_a_gh_prefixed_non_matching_code_as_a_ticket_shape(repo_with_pr_worktree, tmp_path):
    """"GH-105A" is not a task-cli id in ANY of its three shapes (`#<n>`, `<n>`,
    `<PREFIX>-<digits>`) -- its tail is not digits-only. Before agent-tools#565 the shape check
    was merely "contains a digit", so this code survived it and was handed to `task
    mark-shipped` verbatim, where it could only 404; now it is rejected at derivation and the
    NEXT source is tried, exactly like any other rejected candidate.

    The regex-anchor property this test used to pin (`^[Gg][Hh]-([0-9]+)$` must not rewrite
    "GH-105A") is now pinned directly on the normalization function by
    test_normalize_gh_code_only_rewrites_the_exact_gh_number_shape below -- a unit assertion,
    since a code this malformed no longer reaches the normalization step at all."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={
            "TASK_CODE": "GH-105A",
            "SHIP_TEST_PR_TITLE": "fix the thing",
            "SHIP_TEST_PR_BODY": "no ticket mentioned",
        },
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert calls == [], calls


def test_normalize_gh_code_only_rewrites_the_exact_gh_number_shape(tmp_path):
    """`_ship_normalize_gh_code_for_task_cli` rewrites ONLY `GH-<digits>` (case-insensitive) into
    task-cli's `#<n>` form. Pins the `$` anchor and the digits-only tail directly, so a future
    edit that loosens either is caught here rather than by a code shape that no longer reaches
    this function."""
    script = (
        f'eval "$(sed -n \'/^_ship_normalize_gh_code_for_task_cli()/,/^}}/p\' {_SHIP})"\n'
        'for c in GH-105 gh-105 GH-105A GH- GHX-105 HYP-931 "#105"; do '
        'printf "%s=>%s\\n" "$c" "$(_ship_normalize_gh_code_for_task_cli "$c" 2>/dev/null)"; done\n'
    )
    r = _sh("bash", "-c", script, cwd=tmp_path, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    got = dict(line.split("=>", 1) for line in r.stdout.strip().splitlines())
    assert got == {
        "GH-105": "#105",      # the exact shape -> rewritten
        "gh-105": "#105",      # case-insensitive
        "GH-105A": "GH-105A",  # non-digit tail -> untouched ($ anchor)
        "GH-": "GH-",          # no digits at all -> untouched
        "GHX-105": "GHX-105",  # not the GH- prefix -> untouched
        "HYP-931": "HYP-931",  # an ordinary Linear id -> untouched
        "#105": "#105",        # already normalized -> untouched
    }, got


def test_looks_like_a_ticket_id_pins_task_cli_grammar(tmp_path):
    """`_ship_looks_like_a_ticket_id` is a hand transcription of task-cli's id routing
    (`tasklib/cli.py::_route_id_to_project`: `#<n>` / bare `<n>` -> GitHub issues, `PREFIX-<n>`
    -> the Linear team named by the prefix), so it is pinned as a table here. Deliberate edges:
    a lowercase prefix is accepted (task-cli upper-cases it before routing); a digit-led prefix
    is rejected (Linear team keys are letter-led); `UTF-8` is well-formed for a team that does
    not exist (the documented known limit) and is accepted."""
    cases = [
        "#105", "105", "007", "HYP-931", "hyp-931", "PROJ-12", "GH-105", "OC476-123", "UTF-8",
        "", "#", "#12a", "-931", "HYP-", "HYP-931-2", "1X-5", "rig-cli-341",
        "OC476-OPENCODE-BACKGROUND-TRUTH", "GH-105A", "SME-ROADMAP-NOTE-42", "HYP 931",
    ]
    quoted = " ".join(f"'{c}'" for c in cases)
    script = (
        f'eval "$(sed -n \'/^_ship_looks_like_a_ticket_id()/,/^}}/p\' {_SHIP})"\n'
        f"for c in {quoted}; do "
        'if _ship_looks_like_a_ticket_id "$c"; then v=yes; else v=no; fi; '
        'printf "%s=>%s\\n" "$c" "$v"; done\n'
    )
    r = _sh("bash", "-c", script, cwd=tmp_path, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    got = dict(line.split("=>", 1) for line in r.stdout.splitlines())
    accepted = {"#105", "105", "007", "HYP-931", "hyp-931", "PROJ-12", "GH-105", "OC476-123", "UTF-8"}
    assert got == {c: ("yes" if c in accepted else "no") for c in cases}, got


def test_notify_normalizes_a_gh_code_derived_literally_from_the_branch_name(repo_with_pr_worktree, tmp_path):
    """Regression on the SAME bug, reached via a DIFFERENT path than the $TASK_CODE reuse above:
    a branch literally named "GH-105-..." is matched by _review_quorum_extract_ticket's generic
    PREFIX-<n> arm (`[A-Z][A-Z]+-[0-9]+`, the same arm that matches this repo's own HYP-<n>
    convention) and derived as "GH-105" -- #511 only stopped that matcher from SYNTHESIZING
    "GH-<n>" out of a bare "Fixes #105"; it never taught the generic arm to reject a "GH-<n>"
    token that was typed literally into the branch name itself. This must still normalize to
    "#105", the exact same way the reused-$TASK_CODE case does."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(main, bindir, tmp_path, branch="GH-105-fix-crash")

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped #105" in calls[0]
    assert "GH-105" not in calls[0]


def test_notify_normalizes_a_lowercase_gh_code(repo_with_pr_worktree, tmp_path):
    """The convention require-ticket-before-commit recognizes is case-insensitive
    (`re.IGNORECASE` on the same "GH-<n>" pattern) -- a lowercase "gh-301", however it arrives,
    must normalize exactly like the uppercase form."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"TASK_CODE": "gh-301"},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped #301" in calls[0]
    assert "gh-301" not in calls[0]


def test_notify_leaves_a_reused_literal_hash_code_unaffected_by_gh_normalization(repo_with_pr_worktree, tmp_path):
    """A reused $TASK_CODE already in the literal "#<n>" shape (the form the keyword-anchored
    GitHub-issue-reference arm has returned since #511) must not be double-rewritten -- it does
    not match the "GH-<n>" case pattern at all, so it passes through untouched."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"TASK_CODE": "#301"},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped #301" in calls[0]


def test_notify_leaves_a_reused_hyp_code_unaffected_by_gh_normalization(repo_with_pr_worktree, tmp_path):
    """A reused $TASK_CODE in the normal HYP-<n> shape must not be touched by the GH-<n>
    normalization added for the regression above."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"TASK_CODE": "HYP-931"},
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert len(calls) == 1, calls
    assert "mark-shipped HYP-931" in calls[0]


def test_notify_rejects_a_reused_descriptive_review_code(repo_with_pr_worktree, tmp_path):
    """A reused $TASK_CODE in review-cli's hyphenated descriptive shape ("SME-ROADMAP-NOTE-42")
    is a review CHECK code, not a task-cli ticket id -- a different entity, and unroutable by
    `_route_id_to_project` (its prefix would have to be a Linear team, and a Linear identifier is
    TEAM-<number>, never TEAM-WORD-WORD-<number>).

    Before agent-tools#565 the "contains a digit" shape check passed it straight through to `task
    mark-shipped`, which could only 404 -- the same class that made the ACCEPTANCE gate silently
    skip on `rig-cli-341` / `OC476-OPENCODE-BACKGROUND-TRUTH` on 2026-09-06. It must now be
    rejected, and with no other code anywhere the notify step skips rather than calling task-cli
    with a code it cannot resolve."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={
            "TASK_CODE": "SME-ROADMAP-NOTE-42",
            "SHIP_TEST_PR_TITLE": "fix the thing",
            "SHIP_TEST_PR_BODY": "no ticket mentioned",
        },
    )

    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert calls == [], calls


def test_notify_skips_when_no_code_derivable_anywhere(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "no ticket mentioned"},
    )

    assert r.returncode == 0, "a missing task code must never block the ship"
    assert "merged #1" in r.stdout
    assert calls == []
    assert "could not derive a task code" in r.stderr


def test_notify_skipped_when_task_not_on_path(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=False)  # no `task` binary

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", base_path=_path_without_real_task(),
    )

    assert r.returncode == 0
    assert "merged #1" in r.stdout
    assert calls == []
    assert "task-cli not on PATH" in r.stderr


def test_notify_failure_does_not_fail_the_ship(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing",
        extra_env={"SHIP_TEST_TASK_EXIT": "1"},
    )

    assert r.returncode == 0, "a failing task-cli call must never fail an already-successful ship"
    assert "merged #1" in r.stdout
    assert len(calls) == 1  # task WAS invoked; it just failed
    assert "task mark-shipped" in r.stderr and "failed" in r.stderr


def test_notify_skipped_in_dry_run(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", extra_args=["--dry-run"],
    )

    assert r.returncode == 0
    assert calls == []
    assert "would notify task-cli" in r.stdout


def test_notify_skipped_for_a_foreign_repo_invocation(repo_with_pr_worktree, tmp_path):
    """--repo targeting a DIFFERENT repo than this checkout's own origin must never let a
    local `task` write into the wrong project — the one guard standing between this feature
    and a cross-project ticket-store corruption, so it gets its own explicit test (review
    finding: it was previously exercised by nothing)."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", extra_args=["--repo", "acme/other"],
    )

    assert r.returncode == 0
    assert calls == []
    assert "foreign remote" in r.stderr


def test_notify_skipped_when_pr_url_cannot_be_resolved(repo_with_pr_worktree, tmp_path):
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="hyp-931-fix-thing", extra_env={"SHIP_TEST_PR_URL": ""},
    )

    assert r.returncode == 0, "an unresolvable PR URL must never block an already-successful merge"
    assert "merged #1" in r.stdout
    assert calls == []
    assert "could not resolve" in r.stderr


def test_notify_rejects_a_digit_free_false_positive_code(repo_with_pr_worktree, tmp_path):
    """A generic PR title can trip the matcher's purely-descriptive arm (e.g. "DO-NOT-MERGE
    experimental spike" -> "DO-NOT-MERGE", a documented accepted false positive of the
    matcher — see ship.sh's own comment on _review_quorum_extract_ticket) — real task-cli
    ticket ids always carry a numeric suffix (HYP-931, #123), so a digit-free "code" must
    never reach `task mark-shipped` (review finding: it would either fail confusingly against
    an unrelated existing ticket, or in the worst case silently mark the WRONG ticket
    "shipped" if one happens to share that literal id)."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "DO-NOT-MERGE experimental spike", "SHIP_TEST_PR_BODY": "no ticket here"},
    )

    assert r.returncode == 0
    assert calls == []
    assert "could not derive a task code" in r.stderr


def test_notify_batches_title_body_and_url_mergecommit_into_two_gh_calls(repo_with_pr_worktree, tmp_path):
    """The worst case (no $TASK_CODE reuse, code only in the PR body) must cost exactly TWO
    `gh pr view` round trips — one combined title+body query, one combined url+mergeCommit
    query — not four separate ones."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(
        main, bindir, tmp_path, branch="fix-the-thing",
        extra_env={"SHIP_TEST_PR_TITLE": "fix the thing", "SHIP_TEST_PR_BODY": "refs HYP-931 for details"},
    )

    assert r.returncode == 0
    assert len(calls) == 1
    # Only the two NOTIFY calls are counted (title,body + url,mergeCommit) — the pre-existing
    # preflight/rollup `pr view` calls (headRefName, statusCheckRollup) are a separate, already
    # fixed cost this test doesn't care about and deliberately doesn't assert a total for
    # (that total would break on any unrelated preflight change).
    gh_calls = _gh_pr_view_calls(tmp_path)
    notify_calls = [c for c in gh_calls if "--json title,body" in c or "--json url,mergeCommit" in c]
    assert len(notify_calls) == 2, gh_calls


def test_notify_runs_task_cli_from_the_local_repo_root(repo_with_pr_worktree, tmp_path):
    """ship.sh runs `task mark-shipped` FROM "$ROOT" (a subshell `cd`) rather than passing
    `-C "$ROOT"` as an argument — task-cli's argparse adds `-C`/`--cwd` as a PER-SUBCOMMAND
    flag (via a `parents=[common]` subparser), not a top-level one, so `task -C <dir>
    mark-shipped ...` is rejected ("invalid choice: '<dir>'"). A REAL, un-faked, previous
    version of this exact bug shipped live and failed silently on its own merge (best-effort
    logging swallowed it). Running from the target directory sidesteps the ordering question
    entirely rather than merely fixing its position — verified here via the fake `task`'s own
    logged `$PWD`, and `test_notify_mark_shipped_argv_is_accepted_by_the_real_task_cli` (below)
    additionally validates the argv shape against the REAL installed `task` binary, since a
    fake that only logs argv can never catch a real argparse rejection."""
    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)

    r, calls = _run_ship(main, bindir, tmp_path, branch="hyp-931-fix-thing")

    assert r.returncode == 0
    assert len(calls) == 1
    argv = calls[0].split()
    assert argv[:2] == ["mark-shipped", "HYP-931"]
    assert "-C" not in argv and "--cwd" not in argv

    pwd_log = tmp_path / "task.log.pwd"
    logged_pwds = [Path(line).resolve() for line in pwd_log.read_text(encoding="utf-8").splitlines()]
    assert logged_pwds == [main.resolve()]


def test_notify_mark_shipped_argv_is_accepted_by_the_real_task_cli(repo_with_pr_worktree, tmp_path):
    """Regression guard for the real argument-shape bug above: run the EXACT argv ship.sh
    builds through the real, installed `task` binary's own argparse (not the logging fake).
    `--help` short-circuits argparse's LEFT-TO-RIGHT consumption before any ticket lookup or
    network/backend call — zero execution, zero risk of touching a real ticket store — but
    that also SCOPES what this proves: argparse's `--help` action fires (and exits 0) the
    moment it's consumed, before `parse_args` reaches its end-of-parse checks, so this test
    catches an invalid SUBCOMMAND CHOICE (the original bug — `mark-shipped` misplaced after a
    top-level `-C`) but would NOT catch `--pr`/`--commit` becoming unrecognized options or a
    missing-required-argument error, both of which are reported only at end-of-parse. Skipped
    if `task` isn't installed — a real-binary integration check, not a hermetic unit test.
    HOME/XDG_CONFIG_HOME are pointed at an empty tmp dir (belt-and-suspenders): task-cli's own
    architecture guarantees `--help` stays fast/dependency-light with no eager backend init,
    but scrubbing the environment means this assertion doesn't SILENTLY depend on that staying
    true."""
    real_task = shutil.which("task")
    if not real_task:
        pytest.skip("task-cli not installed on this machine")

    main, _wt = repo_with_pr_worktree
    bindir = _bindir(tmp_path, with_task=True)
    r, calls = _run_ship(main, bindir, tmp_path, branch="hyp-931-fix-thing")
    assert r.returncode == 0 and len(calls) == 1
    argv = calls[0].split()
    assert argv[:2] == ["mark-shipped", "HYP-931"]  # pin what argv shape --help below validates

    # --help short-circuits argparse before any ticket lookup/network/backend call — zero
    # execution, zero risk of touching a real ticket store. It's appended AFTER the full argv
    # (not standalone) so the check still exercises the actual flag positions ship.sh builds.
    # HOME/XDG_CONFIG_HOME scrubbed to an empty tmp dir — see the docstring's belt-and-suspenders
    # note — so this can't reach the developer's real task-cli config/credentials either way.
    scrub_home = tmp_path / "real-task-help-home"
    scrub_home.mkdir(exist_ok=True)
    check_env = {**os.environ, "HOME": str(scrub_home), "XDG_CONFIG_HOME": str(scrub_home / ".config")}
    check = subprocess.run([real_task, *argv, "--help"], capture_output=True, text=True, env=check_env)
    assert check.returncode == 0, check.stderr
    assert "invalid choice" not in check.stderr, check.stderr
    # `shutil.which("task")` only proves SOME binary named `task` exists — `task` is also the
    # name of the unrelated go-task/Taskfile runner, commonly installed on the same machines
    # (review finding). Assert task-cli's own subparser usage line actually printed, so a
    # wrong binary can't make this pass vacuously (an unrelated `task` that also happens to
    # accept `--help` and never print the literal string "invalid choice").
    assert "mark-shipped" in check.stdout, check.stdout
    # Closes part of the admitted --help blind spot above (two independent review findings):
    # the subparser's own help text lists its options, so this also catches --pr/--commit
    # being renamed or removed — end-of-parse errors --help itself can't reach.
    assert "--pr" in check.stdout and "--commit" in check.stdout, check.stdout
