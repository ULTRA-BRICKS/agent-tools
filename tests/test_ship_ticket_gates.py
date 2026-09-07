"""Tests for ci/ship/ship.sh's two PRE-MERGE ticket gates (agent-tools#521, task-cli#115):

* the ACCEPTANCE gate — `task gate <code> --json` (task-cli) must exit 0 before the merge, or the
  ticket records a post-merge-acceptance opt-out; and
* the MAGIC-CLOSE gate — a close/fix/resolve keyword targeting an issue (#N, owner/repo#N) or a
  ticket code (ABC-123) in the PR title/body is refused (or rewritten to "Refs" with
  `--rewrite-magic-close`), because GitHub and Linear would close the ticket on merge behind
  task-cli's acceptance gates.

Own file, own fixtures (same hermetic shape as tests/test_ship_notify_task_cli.py: a real temp git
repo + a fake `gh` + a fake `task` on PATH, no network). The fake `task` answers `task gate` with
whatever JSON/exit code the test sets, so every branch of the contract is driven explicitly.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship_ticket_gates.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

# The review-quorum gate defaults ENABLED in the product but has no fixture here — force it off
# process-wide (same rationale as tests/test_ship.py). The post-merge notify is off too: this
# file asserts on the fake `task`'s call log and only wants the GATE's calls in it.
os.environ.setdefault("SHIP_REVIEW_QUORUM", "0")
os.environ.setdefault("SHIP_TASK_NOTIFY_ENABLED", "0")

# A fake `gh` answering exactly the calls ship.sh makes up to and including the merge: the
# preflight, the CI rollup (green), the reviews count, the unresolved-threads query (0), the
# title / body reads the magic-close gate does, `pr edit` (logged, so a test can assert the
# rewrite), the merge itself (logged), and the url+mergeCommit read the notify step would do.
_FAKE_GH = """\
#!/usr/bin/env bash
set -e
# Stateful title/body (review finding, round 4, GLM: _magic_close_rewrite_pr now RE-FETCHES
# title/body after a `gh pr edit` to verify the rewrite actually landed — a fake `gh` that
# always answers with the original $SHIP_TEST_PR_TITLE/$SHIP_TEST_PR_BODY, ignoring `edit`
# entirely, would make every successful-rewrite test look like a failed re-verify). State
# lives in two files under $SHIP_TEST_STATE_DIR, seeded from the env vars on first read.
TITLE_FILE="${SHIP_TEST_STATE_DIR:-.}/fake-gh-title"
BODY_FILE="${SHIP_TEST_STATE_DIR:-.}/fake-gh-body"
[ -f "$TITLE_FILE" ] || printf '%s' "${SHIP_TEST_PR_TITLE:-}" > "$TITLE_FILE"
[ -f "$BODY_FILE" ] || printf '%s' "${SHIP_TEST_PR_BODY:-}" > "$BODY_FILE"
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        args="$*"
        if printf '%s' "$args" | grep -q -- '--json reviews'; then
          echo 1
        elif printf '%s' "$args" | grep -q headRefName; then
          printf '%s\\tOPEN\\tMERGEABLE\\tfalse\\tCLEAN\\n' "${SHIP_TEST_BRANCH}"
        elif printf '%s' "$args" | grep -q statusCheckRollup; then
          printf '%s\\n' '[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
        elif printf '%s' "$args" | grep -q -- '--json title,body'; then
          # Mirrors the real query's `gsub("\\n";" ")`: ship reads ONE tab-separated line, so a
          # multi-line body (the PR #560 fixture) must be flattened here as jq flattens it live.
          printf '%s\\t%s\\n' "$(tr '\\n' ' ' < "$TITLE_FILE")" "$(tr '\\n' ' ' < "$BODY_FILE")"
        elif printf '%s' "$args" | grep -q -- '--json title'; then
          if [ "${SHIP_TEST_GH_PR_VIEW_TITLE_FAIL:-0}" = "1" ]; then exit 1; fi
          cat "$TITLE_FILE"; echo
        elif printf '%s' "$args" | grep -q -- '--json body'; then
          cat "$BODY_FILE"; echo
        elif printf '%s' "$args" | grep -q -- '--json url,mergeCommit'; then
          printf '%s\\t%s\\n' "https://github.com/acme/widgets/pull/1" ""
        elif printf '%s' "$args" | grep -q -- '--json url'; then
          if [ "${SHIP_TEST_GH_PR_VIEW_URL_FAIL:-0}" = "1" ]; then exit 1; fi
          printf '%s\\n' "https://github.com/acme/widgets/pull/1"
        elif printf '%s' "$args" | grep -q -- '--json commits'; then
          printf '%s\\n' "${SHIP_TEST_PR_COMMITS:-}"
        else
          echo '[]'
        fi ;;
      diff) echo "src/a.py" ;;
      comment) : ;;
      edit)
        printf 'edit %s\\n' "$*" >> "${GH_LOG}"
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --title) printf '%s' "$2" > "$TITLE_FILE"; shift 2 ;;
            --body) printf '%s' "$2" > "$BODY_FILE"; shift 2 ;;
            *) shift ;;
          esac
        done ;;
      merge) printf 'merge %s\\n' "$*" >> "${GH_LOG}"; echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api) echo 0 ;;
  *) : ;;
esac
"""

# A fake `task`: logs its argv (one line) to $TASK_LOG; `task gate` prints $SHIP_TEST_TASK_GATE_JSON
# (stdout) and $SHIP_TEST_TASK_GATE_ERR (stderr — where the real task-cli's `error: …` goes on exit 2)
# and exits $SHIP_TEST_TASK_GATE_EXIT; `task done` exits $SHIP_TEST_TASK_DONE_EXIT (default 0);
# every other subcommand exits 0 silently.
_FAKE_TASK = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${TASK_LOG}"
if [ "$1" = "gate" ]; then
  printf '%s\\n' "${SHIP_TEST_TASK_GATE_JSON:-}"
  [ -n "${SHIP_TEST_TASK_GATE_ERR:-}" ] && printf '%s\\n' "${SHIP_TEST_TASK_GATE_ERR}" >&2
  exit "${SHIP_TEST_TASK_GATE_EXIT:-0}"
fi
if [ "$1" = "done" ]; then
  exit "${SHIP_TEST_TASK_DONE_EXIT:-0}"
fi
if [ "$1" = "mark-shipped" ]; then
  exit "${SHIP_TEST_TASK_MARK_SHIPPED_EXIT:-0}"
fi
exit 0
"""

# A minimal fake `review` for the tests that run the review-quorum gate AND the acceptance gate
# together: `review task <code> …` answers a PASSING 3/3/3 record for the codes listed in
# $SHIP_TEST_REVIEW_KNOWN_CODES (space-separated) and review-cli's real no-record answer (zero
# counts, `error`, exit 1) for every other code. Every other subcommand exits 0 silently.
_FAKE_REVIEW = """\
#!/usr/bin/env bash
[ "${1:-}" = "task" ] || exit 0
code="${2:-}"
for k in ${SHIP_TEST_REVIEW_KNOWN_CODES:-}; do
  if [ "$k" = "$code" ]; then
    printf '{"task_code":"%s","passed_iterations":3,"total_iterations":3,"distinct_models_passed":3,"models":["a","b","c"],"min_iter":3,"passed":true,"distinct_roles_passed":3,"roles":["correctness","consistency","quality"],"min_roles":3}\\n' "$code"
    exit 0
  fi
done
printf '{"task_code":"%s","passed_iterations":0,"total_iterations":0,"distinct_models_passed":0,"models":[],"min_iter":3,"passed":false,"distinct_roles_passed":0,"roles":[],"min_roles":3,"error":"no recorded review iterations for %s"}\\n' "$code" "$code"
exit 1
"""


def _ship_copy_with_approving_hatch(tmp_path: Path) -> Path:
    """A ship.sh copy with a fake review_quorum_hatch.py beside it that APPROVES (the stdout
    sentinel ship.sh gates on), so a quorum refusal proceeds as on a live Telegram approval — no
    tg-ctl involved. Same shape as tests/test_ship.py's copy helper."""
    dst = tmp_path / "toolcopy" / "ci" / "ship"
    dst.mkdir(parents=True)
    ship_copy = dst / "ship.sh"
    ship_copy.write_text(_SHIP.read_text(encoding="utf-8"), encoding="utf-8")
    ship_copy.chmod(0o755)
    (dst / "review_quorum_hatch.py").write_text(
        "import sys\nsys.stdout.write('APPROVED test approval\\n')\nsys.exit(0)\n", encoding="utf-8")
    return ship_copy


_NOT_ACCEPTED = json.dumps({
    "id": "HYP-931", "ok": False, "state": "in_progress", "gate_enabled": True,
    "post_merge_acceptance": None, "criteria": 3, "below_minimum": False,
    "unchecked": [{"index": 3, "text": "survives a restart"}],
    "proofless": [{"index": 2, "text": "handles the empty case"}],
})
_ACCEPTED = json.dumps({
    "id": "HYP-931", "ok": True, "state": "in_progress", "gate_enabled": True,
    "post_merge_acceptance": None, "criteria": 3, "below_minimum": False,
    "unchecked": [], "proofless": [],
})
_OPTED_OUT = json.dumps({
    "id": "HYP-931", "ok": True, "state": "todo", "gate_enabled": True,
    "post_merge_acceptance": "the extension is published to the registry after the merge",
    "criteria": 2,
    "unchecked": [{"index": 1, "text": "v0.17.4 is on the marketplace"}, {"index": 2, "text": "changelog rendered"}],
    "proofless": [],
})
# task-cli#115 round 2: a ticket BELOW its configured acceptance_min refuses `task gate` on
# count alone — `unchecked`/`proofless` are both EMPTY even though `ok` is false, since the
# one criterion it DOES have is fully proven. ship.sh must read `below_minimum`, not just the
# two gap arrays, to explain a refusal like this.
_BELOW_MINIMUM = json.dumps({
    "id": "HYP-931", "ok": False, "state": "in_progress", "gate_enabled": True,
    "post_merge_acceptance": None, "criteria": 1, "below_minimum": True,
    "unchecked": [], "proofless": [],
})


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=dict(os.environ))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def repo(tmp_path):
    """A repo on `main` with branch `feat` in a worktree plus an `origin` — the shape the other
    ship test modules use."""
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
    _git("worktree", "add", "-q", str(tmp_path / "wt-feat"), "feat", cwd=main)
    return main


def _bindir(tmp_path: Path, *, with_task: bool = True, with_review: bool = False) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "gh").write_text(_FAKE_GH, encoding="utf-8")
    (bindir / "gh").chmod(0o755)
    if with_task:
        (bindir / "task").write_text(_FAKE_TASK, encoding="utf-8")
        (bindir / "task").chmod(0o755)
    if with_review:
        (bindir / "review").write_text(_FAKE_REVIEW, encoding="utf-8")
        (bindir / "review").chmod(0o755)
    return bindir


def _path_without_real_task() -> str:
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(d for d in dirs if not (d and Path(d, "task").exists()))


def _run(main: Path, tmp_path: Path, *, branch="hyp-931-fix-thing", with_task=True, env=None, args=(), pr="1",
         with_review=False, ship: Path = _SHIP):
    bindir = _bindir(tmp_path, with_task=with_task, with_review=with_review)
    e = dict(os.environ)
    e["PATH"] = f"{bindir}{os.pathsep}{_path_without_real_task() if not with_task else e['PATH']}"
    e.update({
        "SHIP_TEST_BRANCH": branch,
        "SHIP_DEFAULT_BRANCH": "main",
        "SHIP_MAIN_CHECKOUT": str(main),
        "SHIP_REVIEW_DWELL": "0",
        "TASK_LOG": str(tmp_path / "task.log"),
        "GH_LOG": str(tmp_path / "gh.log"),
        "SHIP_TEST_STATE_DIR": str(tmp_path),
        "SHIP_AUDIT_FILE": str(tmp_path / "audit.jsonl"),
        # this file exercises the gates themselves — always on unless a test turns one off
        "SHIP_ACCEPTANCE_GATE": "1",
        "SHIP_MAGIC_CLOSE_GATE": "1",
        "SHIP_TEST_PR_TITLE": "feat: the thing",
        "SHIP_TEST_PR_BODY": "Refs HYP-931",
        "SHIP_TEST_TASK_GATE_JSON": _ACCEPTED,
        "SHIP_TEST_TASK_GATE_EXIT": "0",
    })
    if env:
        e.update(env)
    r = _sh("bash", str(ship), pr, "--no-screenshot-ok", "test", *args, cwd=main, env=e)
    return r


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _task_calls(tmp_path: Path) -> list[str]:
    return _lines(tmp_path / "task.log")


def _gh_log(tmp_path: Path) -> list[str]:
    return _lines(tmp_path / "gh.log")


def _audit(tmp_path: Path, gate: str) -> list[dict]:
    return [json.loads(l) for l in _lines(tmp_path / "audit.jsonl") if json.loads(l).get("gate") == gate]


def _merged(tmp_path: Path) -> bool:
    return any(l.startswith("merge ") for l in _gh_log(tmp_path))


# ── acceptance gate ─────────────────────────────────────────────────────────────────


def test_acceptance_gate_refuses_and_lists_the_open_criteria(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "Refusing: acceptance gate — ticket HYP-931 is NOT accepted" in r.stderr
    assert "[3] survives a restart  (unchecked)" in r.stderr
    assert "[2] handles the empty case  (checked without a proof)" in r.stderr
    assert "task accept HYP-931" in r.stderr and "--post-merge-acceptance" in r.stderr
    assert not _merged(tmp_path), "a refused PR must never reach gh pr merge"
    assert _task_calls(tmp_path) == ["gate HYP-931 --json"]
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "refused" and line["task_code"] == "HYP-931" and line["pr"] == "1"
    assert line["detail"] == "criteria=3 below_minimum=false unchecked=3 proofless=2"


def test_acceptance_gate_refuses_below_minimum_with_empty_gap_arrays(repo, tmp_path):
    """task-cli#115 round 2: a ticket below its configured acceptance_min refuses `task gate`
    on count alone — unchecked/proofless are BOTH empty (the one criterion it has IS proven).
    ship.sh must read `below_minimum`, not just the two gap arrays, to explain the refusal —
    otherwise a shipper sees "NOT accepted" with nothing listed under it."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _BELOW_MINIMUM, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "Refusing: acceptance gate — ticket HYP-931 is NOT accepted" in r.stderr
    assert "only 1 acceptance criteria — below this repo's configured minimum" in r.stderr
    assert not _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "refused"
    assert line["detail"] == "criteria=1 below_minimum=true unchecked= proofless="


def test_acceptance_gate_passes_and_merges_when_accepted(repo, tmp_path):
    r = _run(repo, tmp_path)
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "acceptance gate: HYP-931 accepted" in r.stdout
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "authorized" and line["task_code"] == "HYP-931" and "detail" not in line


def test_acceptance_gate_honours_the_ticket_opt_out_and_reports_the_reason(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _OPTED_OUT})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "passes on a recorded post-merge-acceptance opt-out" in r.stdout
    assert "the extension is published to the registry after the merge" in r.stdout
    assert "still owed after the merge: task accept HYP-931" in r.stdout
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "authorized:post-merge-opt-out"
    assert line["detail"] == "the extension is published to the registry after the merge"


def test_acceptance_gate_skips_when_task_cli_is_absent(repo, tmp_path):
    r = _run(repo, tmp_path, with_task=False)
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "acceptance gate: task-cli not on PATH — skipping" in r.stderr
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "skipped" and line["detail"] == "task-cli not on PATH" and "task_code" not in line


def test_acceptance_gate_skips_when_no_ticket_code_is_derivable(repo, tmp_path):
    r = _run(repo, tmp_path, branch="feat", env={"SHIP_TEST_PR_BODY": "no ticket here"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "could not derive a task code for #1 — skipping" in r.stderr
    assert _task_calls(tmp_path) == []
    assert _merged(tmp_path)
    assert _audit(tmp_path, "acceptance")[0]["detail"] == "no task code"


def test_acceptance_gate_derives_the_code_from_a_same_repo_issue_url(repo, tmp_path):
    """A body that links the ticket as a full same-repo issue URL (agent-tools#564) gates on
    that ticket: `task gate #115 --json` is asked, and a refusal names `#115`."""
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_BODY": "Refs [#115](https://github.com/acme/widgets/issues/115)",
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED.replace("HYP-931", "#115"),
        "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate #115 --json"]
    assert "Refusing: acceptance gate — ticket #115 is NOT accepted" in r.stderr
    assert not _merged(tmp_path)


def test_acceptance_gate_reuses_the_notify_derivation_title_then_body(repo, tmp_path):
    """The code comes from `_ship_derive_task_code_for_notify` — branch, then PR title, then
    body — not a second matcher: a code only in the title must still gate."""
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_TITLE": "Fix the thing (HYP-777)", "SHIP_TEST_PR_BODY": "no code here",
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1
    assert _task_calls(tmp_path) == ["gate HYP-777 --json"]


def test_acceptance_gate_refuses_when_task_gate_cannot_resolve_a_derived_code(repo, tmp_path):
    """agent-tools#569: a code WAS derived (HYP-931, well-formed) but task-cli answers exit 2 —
    a typo'd ticket, or a hyphenated word that passed the grammar. A PR that names a ticket
    task-cli cannot find is a BROKEN PR, not an unguarded one: refuse, name the code and the
    exit, echo task-cli's own message, and point at the two remedies. Never a merge."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": "", "SHIP_TEST_TASK_GATE_ERR": "error: unknown ticket HYP-931", "SHIP_TEST_TASK_GATE_EXIT": "2"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "Refusing: acceptance gate — task-cli could not resolve ticket HYP-931 (task gate HYP-931 exit 2)" in r.stderr, r.stderr
    assert "unknown ticket HYP-931" in r.stderr
    assert "REVIEW_TASK_CODE=" in r.stderr and "SHIP_ACCEPTANCE_GATE=0" in r.stderr, r.stderr
    assert "The code came from the branch name." in r.stderr, r.stderr   # where the bad code came from
    assert not _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "refused:unresolvable" and line["task_code"] == "HYP-931"
    assert line["detail"] == "task gate exit 2: error: unknown ticket HYP-931"


def test_acceptance_gate_exit_2_error_line_on_stdout_is_task_cli_too(repo, tmp_path):
    """A shim that prints task-cli's `error: …` line on STDOUT (the real binary uses stderr)
    is still task-cli's answer: the line is found on either stream, first occurrence, and a
    mid-line "error:" mention elsewhere is not what identifies it."""
    r = _run(repo, tmp_path, env={
        "SHIP_TEST_TASK_GATE_JSON": "warning: cache miss (no error: yet)\nerror: unknown ticket HYP-931",
        "SHIP_TEST_TASK_GATE_EXIT": "2",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    (line,) = _audit(tmp_path, "acceptance")
    assert line["detail"] == "task gate exit 2: error: unknown ticket HYP-931", line
    assert not _merged(tmp_path)


def test_acceptance_gate_exit_2_prefers_the_stderr_error_line_over_stdout(repo, tmp_path):
    """Both streams carry a `^error: ` line: stderr's wins (where the real binary prints it)."""
    r = _run(repo, tmp_path, env={
        "SHIP_TEST_TASK_GATE_JSON": "error: stdout chatter", "SHIP_TEST_TASK_GATE_ERR": "error: unknown ticket HYP-931",
        "SHIP_TEST_TASK_GATE_EXIT": "2",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    (line,) = _audit(tmp_path, "acceptance")
    assert line["detail"] == "task gate exit 2: error: unknown ticket HYP-931", line


def test_acceptance_gate_exit_2_refuses_when_stderr_cannot_be_captured(repo, tmp_path):
    """mktemp fails (TMPDIR unusable) -> task-cli's stderr goes to /dev/null and its `error:`
    line is never seen. That is not evidence of a foreign binary: with a derived code, exit 2
    refuses and says the message could not be captured (codex review on PR #577)."""
    bindir = tmp_path / "bin"   # _run's fake-binary dir (first on PATH); mkdir'd exist_ok by _bindir
    bindir.mkdir(exist_ok=True)
    (bindir / "mktemp").write_text("#!/usr/bin/env bash\necho 'mktemp: cannot create temp file' >&2; exit 1\n", encoding="utf-8")
    (bindir / "mktemp").chmod(0o755)
    r = _run(repo, tmp_path, env={
        "SHIP_TEST_TASK_GATE_ERR": "error: unknown ticket HYP-931", "SHIP_TEST_TASK_GATE_EXIT": "2",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "could not resolve ticket HYP-931" in r.stderr and "could not be captured" in r.stderr, r.stderr
    assert not _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "refused:unresolvable", line


def test_acceptance_gate_exit_2_with_only_a_mid_line_error_mention_still_skips(repo, tmp_path):
    """The dropped substring match, pinned: "error:" MID-line (a foreign binary's usage text,
    a wrapper's chatter) on exit 2 is not task-cli's `error: …` line — the PATH-collision skip
    applies, not a refusal of every merge on that machine."""
    r = _run(repo, tmp_path, env={
        "SHIP_TEST_TASK_GATE_JSON": "usage: task <command> (see 'task help'; an unknown command is an error: it exits 2)",
        "SHIP_TEST_TASK_GATE_ERR": "task: no such command 'gate' — error: unknown", "SHIP_TEST_TASK_GATE_EXIT": "2",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "did not return task-cli's expected" in r.stderr, r.stderr
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "skipped" and line["detail"] == "'task' on PATH does not look like task-cli"


def test_acceptance_gate_exit_2_refusal_is_the_same_under_dry_run_without_audit(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--dry-run",), env={"SHIP_TEST_TASK_GATE_ERR": "error: unknown ticket HYP-931", "SHIP_TEST_TASK_GATE_EXIT": "2"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "task-cli could not resolve ticket HYP-931" in r.stderr
    assert not (tmp_path / "audit.jsonl").exists()
    assert "would append acceptance audit: decision=refused:unresolvable" in r.stderr


def test_acceptance_gate_exit_2_without_task_cli_error_shape_still_skips_as_not_task_cli(repo, tmp_path):
    """The PATH-collision guard keeps its reach: an exit 2 that carries NO `error:` line on
    either stream is not task-cli's contract (task-cli always prints `error: …` on exit 2) —
    a foreign `task` (Taskwarrior, go-task) must not turn into a refusal of every merge."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": "Unknown command 'gate'", "SHIP_TEST_TASK_GATE_EXIT": "2"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "did not return task-cli's expected" in r.stderr, r.stderr
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "skipped" and line["detail"] == "'task' on PATH does not look like task-cli"


# ── auto-close on full acceptance (agent-tools#521 follow-up, tg-cli#301/#305 incident) ─────
# A pre-merge gate that only REFUSES a bad merge does not by itself close the "nobody remembered
# task done" gap: tg-cli#301 shipped for real (PR #305, every criterion independently verified
# against the merged code) yet sat OPEN for days. When the gate finds every criterion already
# checked with a proof (not merely opted-out post-merge), ship must also DO the close.


def test_auto_close_runs_task_done_after_a_fully_accepted_merge(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "closing it now: task done HYP-931" in r.stdout
    assert "HYP-931 closed — task done succeeded" in r.stdout
    calls = _task_calls(tmp_path)
    assert calls == [
        "gate HYP-931 --json",
        "mark-shipped HYP-931 --pr https://github.com/acme/widgets/pull/1",
        "done HYP-931",
    ], calls
    lines = _audit(tmp_path, "acceptance")
    assert [l["decision"] for l in lines] == ["authorized", "auto-closed"]
    assert lines[-1]["task_code"] == "HYP-931"


def test_auto_close_skipped_when_mark_shipped_itself_fails(repo, tmp_path):
    """Codex finding (round 3, PR review): when `task mark-shipped` fails — an older task-cli
    lacking the subcommand, a transient backend error — the merged PR/commit link never got
    recorded on the ticket. Auto-closing anyway recreates the exact task/repository divergence
    this notify step exists to prevent: a Done ticket with no record of what shipped it."""
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1", "SHIP_TEST_TASK_MARK_SHIPPED_EXIT": "1"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "'task mark-shipped HYP-931' failed" in r.stderr
    assert "closing it now" not in r.stdout
    calls = _task_calls(tmp_path)
    assert "done HYP-931" not in " ".join(calls)
    assert calls == ["gate HYP-931 --json", "mark-shipped HYP-931 --pr https://github.com/acme/widgets/pull/1"]


def test_auto_close_skipped_on_a_cancelled_ticket(repo, tmp_path):
    """Opus + GLM finding (round 1): `task gate` also exits 0 for a CANCELLED ticket (nothing
    to accept, by definition) — that is not "fully proven", and `task done` on a cancelled
    ticket is nonsensical. Must not attempt an auto-close."""
    cancelled = json.dumps({
        "id": "HYP-931", "ok": True, "state": "cancelled", "gate_enabled": True,
        "post_merge_acceptance": None, "criteria": 3, "below_minimum": False,
        "unchecked": [], "proofless": [],
    })
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1", "SHIP_TEST_TASK_GATE_JSON": cancelled})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "closing it now" not in r.stdout
    assert "done HYP-931" not in " ".join(_task_calls(tmp_path))
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "authorized"
    assert "cancelled" in line["detail"]


def test_auto_close_skipped_on_an_already_done_ticket(repo, tmp_path):
    """Review finding (round 2): `task gate` also exits 0 for a ticket already in the `done`
    state (task-cli's own README contract example shows exactly this shape) — a follow-up PR
    referencing an already-closed ticket must not trigger a doomed `task done` attempt either.
    `done` and `cancelled` are task-cli's only two terminal states."""
    already_done = json.dumps({
        "id": "HYP-931", "ok": True, "state": "done", "gate_enabled": True,
        "post_merge_acceptance": None, "criteria": 3, "below_minimum": False,
        "unchecked": [], "proofless": [],
    })
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1", "SHIP_TEST_TASK_GATE_JSON": already_done})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "closing it now" not in r.stdout
    assert "done HYP-931" not in " ".join(_task_calls(tmp_path))
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "authorized"
    assert "already done" in line["detail"]


def test_acceptance_gate_skips_instead_of_refusing_when_task_on_path_is_not_task_cli(repo, tmp_path):
    """Opus finding (round 2): `task` is an ambiguous binary name (Taskwarrior, go-task also
    install as `task`). Its exit-1-for-unknown-subcommand looks identical to a genuine
    task-cli refusal — an earlier version treated ANY exit 1 as "ticket not accepted" and
    blocked every merge, with a message that doesn't hint at the real cause. Only exit 1 with
    task-cli's OWN JSON shape (id/ok/criteria) is a real refusal; anything else is a skip."""
    r = _run(repo, tmp_path, env={
        "SHIP_TEST_TASK_GATE_JSON": "error: unknown command \"gate\" for \"task\"",
        "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "did not return task-cli's expected JSON shape" in r.stderr
    assert "NOT accepted" not in r.stderr
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "skipped"
    assert "does not look like task-cli" in line["detail"]


def test_acceptance_gate_still_refuses_a_genuine_task_cli_exit_1(repo, tmp_path):
    """The fix above must not swallow a REAL refusal — exit 1 with task-cli's own JSON shape
    (id/ok/criteria present) still refuses exactly as before."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 1
    assert "Refusing: acceptance gate — ticket HYP-931 is NOT accepted" in r.stderr
    assert not _merged(tmp_path)


def test_acceptance_gate_skips_when_a_non_task_cli_task_exits_0_on_garbage(repo, tmp_path):
    """Opus finding (round 3): the PATH-collision guard was applied only on the exit-1 arm —
    a foreign `task` (Taskwarrior/go-task) that happens to exit 0 with non-JSON stdout on an
    unknown `gate` subcommand fell straight into `_acceptance_gate_pass`, which parsed the
    garbage as "no opt-out reason" and audited a false `authorized`, letting the merge proceed
    with NOTHING actually verified — a fail-OPEN gate. Exit 0 must be shape-checked too."""
    r = _run(repo, tmp_path, env={
        "SHIP_TEST_TASK_GATE_JSON": "Type 'task' for usage.",
        "SHIP_TEST_TASK_GATE_EXIT": "0",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "did not return task-cli's expected JSON shape" in r.stderr
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "skipped"
    assert "does not look like task-cli" in line["detail"]


def test_acceptance_gate_still_accepts_a_genuine_task_cli_exit_0(repo, tmp_path):
    """The fix above must not swallow a REAL acceptance — exit 0 with task-cli's own JSON
    shape still authorizes exactly as before."""
    r = _run(repo, tmp_path)
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "acceptance gate: HYP-931 accepted" in r.stdout
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "authorized"


def test_magic_close_refuses_when_gh_pr_view_fails(repo, tmp_path):
    """Fable finding (round 2): an earlier version's `|| title=""` made a genuine `gh` failure
    (rate-limited, network blip) indistinguishable from "the title is legitimately empty" —
    the exact incident class this gate exists to stop could sail through the instant the
    fetch itself failed. Must fail CLOSED, like the review-quorum gate does on an unreadable
    store."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_GH_PR_VIEW_TITLE_FAIL": "1"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "could not read PR #1's title/body/commits from GitHub" in r.stderr
    assert not _merged(tmp_path)
    (line,) = _audit(tmp_path, "magic-close")
    assert line["decision"] == "refused" and "gh pr view failed" in line["detail"]


def test_magic_close_catches_a_keyword_split_across_lines(repo, tmp_path):
    """GLM finding (round 4): GitHub's own close-keyword parser is whitespace-tolerant across
    a linebreak (the review-quorum ticket-code derivation already normalizes newlines to
    spaces for the identical reason) — a plain per-line grep/sed scan would silently miss
    "Fixes\\n#115" split across two lines. Detection is normalized; the reference case for
    the (non-rewrite) refusal."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_BODY": "Summary\n\nFixes\n#115"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert '  in the body:  "Fixes #115"' in r.stderr
    assert not _merged(tmp_path)


def test_magic_close_rewrite_refuses_when_a_cross_line_phrase_survives_the_rewrite(repo, tmp_path):
    """The line-based rewrite sed cannot reach a phrase split across lines — the gate must
    verify the REWRITE RESULT before ever touching the PR, and refuse (never edit, never
    merge) rather than trust the line-based sed blindly."""
    r = _run(repo, tmp_path, args=("--rewrite-magic-close",), env={"SHIP_TEST_PR_BODY": "Summary\n\nFixes\n#115"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "would still leave a close keyword" in r.stderr
    assert not _merged(tmp_path)
    assert [l for l in _gh_log(tmp_path) if l.startswith("edit ")] == [], "must refuse BEFORE editing, not after"
    (line,) = _audit(tmp_path, "magic-close")
    assert line["decision"] == "refused" and "rewrite-incomplete" in line["detail"]


def test_magic_close_catches_a_keyword_in_a_commit_message(repo, tmp_path):
    """Opus + Fable finding (round 2): GitHub also honours a close keyword in a PR's commit
    messages — with the default squash merge, GitHub's own squash-message template includes
    each commit's message in the final commit body unless the repo customizes it, so a clean
    title/body with a keyword buried in one commit can still close the ticket on merge."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_COMMITS": "wip\n\nCloses HYP-1295 for real this time"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert '  in a commit: "Closes HYP-1295"' in r.stderr
    assert not _merged(tmp_path)


def test_magic_close_rewrite_refuses_a_commit_only_hit_with_no_automatic_fix(repo, tmp_path):
    """`--rewrite-magic-close` can only edit the PR title/body via `gh pr edit` — it can never
    rewrite commit history (this script never rewrites or rebases a branch) — so a keyword
    found ONLY in a commit message must still refuse, explaining there is no automatic fix."""
    r = _run(repo, tmp_path, args=("--rewrite-magic-close",), env={
        "SHIP_TEST_PR_COMMITS": "Closes HYP-1295",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "can only edit the PR title/body, never commit history" in r.stderr
    assert [l for l in _gh_log(tmp_path) if l.startswith("edit ")] == []
    assert not _merged(tmp_path)


def test_magic_close_rewrite_still_refuses_when_a_commit_hit_coexists_with_a_title_body_hit(repo, tmp_path):
    """Opus + Fable finding (round 3): an earlier version rewrote the title/body and let the
    merge PROCEED when a commit hit coexisted — but rewriting title/body does nothing to stop
    the un-rewritable commit-message keyword from still closing the ticket on merge. The
    title/body rewrite is harmless and worth keeping, but the ship must still refuse; the audit
    `detail` must list only what was ACTUALLY rewritten (title/body), never claim the commit
    phrase was fixed too."""
    r = _run(repo, tmp_path, args=("--rewrite-magic-close",), env={
        "SHIP_TEST_PR_BODY": "Closes #115",
        "SHIP_TEST_PR_COMMITS": "Closes HYP-1295",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "the commit message keyword below was NOT" in r.stderr
    assert '  in a commit: "Closes HYP-1295"' in r.stderr
    # the title/body rewrite still lands — it's harmless and worth keeping
    edits = [l for l in _gh_log(tmp_path) if l.startswith("edit ")]
    assert edits == ["edit 1 --body Refs #115"]
    assert not _merged(tmp_path)
    (line,) = _audit(tmp_path, "magic-close")
    assert line["decision"] == "refused"
    assert "Closes HYP-1295" in line["detail"]  # the un-fixed hit IS in the refusal detail
    assert "Refs #115" not in line["detail"]  # but never claims the rewritten form as a hit


def test_acceptance_gate_ignores_stderr_noise_mixed_with_the_json(repo, tmp_path):
    """Opus + GLM finding (round 1): an earlier version merged task-cli's stderr into the same
    capture the JSON was parsed from (`2>&1`) — any stderr line (a warning, a deprecation
    notice) corrupted the jq parse silently, defaulting the opt-out reason to empty and
    treating an opted-out ticket as fully accepted. task-cli here emits a stderr line on the
    SAME invocation that returns the opt-out JSON on stdout; the opt-out must still be honored."""
    noisy_task = "#!/usr/bin/env bash\n" + _FAKE_TASK[len("#!/usr/bin/env bash\n"):].replace(
        'printf \'%s\\n\' "${SHIP_TEST_TASK_GATE_JSON:-}"',
        'echo "warning: a deprecation notice" >&2\n  printf \'%s\\n\' "${SHIP_TEST_TASK_GATE_JSON:-}"',
    )
    # A SEPARATE dir, prepended ahead of `_run`'s own auto-created bindir (which `_run` always
    # (re)writes with the plain _FAKE_TASK) — this one shadows it for both `gh` and `task`.
    custom_bin = tmp_path / "custom-bin"
    custom_bin.mkdir(exist_ok=True)
    (custom_bin / "gh").write_text(_FAKE_GH, encoding="utf-8")
    (custom_bin / "gh").chmod(0o755)
    (custom_bin / "task").write_text(noisy_task, encoding="utf-8")
    (custom_bin / "task").chmod(0o755)
    r = _run(repo, tmp_path, env={
        "SHIP_TASK_NOTIFY_ENABLED": "1",
        "PATH": f"{custom_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "SHIP_TEST_TASK_GATE_JSON": _OPTED_OUT,
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "passes on a recorded post-merge-acceptance opt-out" in r.stdout
    assert "closing it now" not in r.stdout
    (line,) = _audit(tmp_path, "acceptance")
    assert line["decision"] == "authorized:post-merge-opt-out"


def test_auto_close_skipped_on_the_post_merge_opt_out(repo, tmp_path):
    """The opt-out exists exactly because acceptance is NOT yet true — auto-closing here would
    either fail loudly or, worse, close a ticket whose criteria are genuinely still open."""
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1", "SHIP_TEST_TASK_GATE_JSON": _OPTED_OUT})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "closing it now" not in r.stdout
    assert _task_calls(tmp_path) == [
        "gate HYP-931 --json",
        "mark-shipped HYP-931 --pr https://github.com/acme/widgets/pull/1",
    ]


def test_auto_close_warns_without_failing_the_ship_when_task_done_still_refuses(repo, tmp_path):
    """`task done` still enforces every OTHER close gate (formatting/links/screenshots/…) — a
    genuine refusal there is expected sometimes and must only warn, never fail the ship (the
    merge already succeeded and is durable)."""
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1", "SHIP_TEST_TASK_DONE_EXIT": "2"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "WARNING: acceptance criteria were fully proven pre-merge, but 'task done HYP-931' still failed" in r.stderr
    assert "Close it by hand: task done HYP-931" in r.stderr
    lines = _audit(tmp_path, "acceptance")
    assert [l["decision"] for l in lines] == ["authorized", "auto-close-failed"]


def test_auto_close_does_not_run_when_the_acceptance_gate_is_disabled(repo, tmp_path):
    """No gate ran => nothing was proven => nothing to auto-close."""
    r = _run(repo, tmp_path, env={"SHIP_TASK_NOTIFY_ENABLED": "1", "SHIP_ACCEPTANCE_GATE": "0"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "done HYP-931" not in " ".join(_task_calls(tmp_path))


def test_acceptance_gate_env_off_switch(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_ACCEPTANCE_GATE": "0", "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "acceptance gate disabled (SHIP_ACCEPTANCE_GATE=0)" in r.stdout
    assert _task_calls(tmp_path) == []
    assert _audit(tmp_path, "acceptance") == [], "an off-switch only echoes, like the other gates"


def test_acceptance_gate_committed_ship_config_off_switch(repo, tmp_path):
    (repo / ".ship-config").write_text("# ops\nSHIP_ACCEPTANCE_GATE=0\n", encoding="utf-8")
    _git("add", ".ship-config", cwd=repo)
    _git("commit", "-qm", "disable the acceptance gate", cwd=repo)
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "acceptance gate disabled by the committed .ship-config (SHIP_ACCEPTANCE_GATE=0)" in r.stdout
    assert _task_calls(tmp_path) == []
    assert _audit(tmp_path, "acceptance") == []


def test_acceptance_gate_ignores_an_uncommitted_ship_config(repo, tmp_path):
    """The audited-config contract: only the content committed at HEAD counts."""
    (repo / ".ship-config").write_text("SHIP_ACCEPTANCE_GATE=0\n", encoding="utf-8")
    r = _run(repo, tmp_path, env={"SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 1
    assert "Refusing: acceptance gate" in r.stderr


def test_acceptance_gate_dry_run_shows_the_same_refusal_without_writing_audit(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--dry-run",), env={"SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1"})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "Refusing: acceptance gate — ticket HYP-931 is NOT accepted" in r.stderr
    assert "[dry-run] would append acceptance audit: decision=refused task=HYP-931" in r.stderr
    assert not (tmp_path / "audit.jsonl").exists()


# ── magic-close gate ────────────────────────────────────────────────────────────────


def test_magic_close_refuses_a_body_keyword_with_the_exact_text(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_BODY": "Summary of the change.\n\nCloses #115 and fixes acme/widgets#9."})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "Refusing: magic-close keyword in PR #1" in r.stderr
    assert '  in the body:  "Closes #115"' in r.stderr
    assert '  in the body:  "fixes acme/widgets#9"' in r.stderr
    assert 'write "Refs <ref>" instead' in r.stderr and "--rewrite-magic-close" in r.stderr
    assert not _merged(tmp_path)
    assert _task_calls(tmp_path) == [], "the magic-close gate runs before the acceptance gate"
    (line,) = _audit(tmp_path, "magic-close")
    assert line["decision"] == "refused" and line["detail"] == "Closes #115;fixes acme/widgets#9"


def test_magic_close_refuses_a_title_keyword_targeting_a_ticket_code(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_TITLE": "Resolves HYP-1295: release cut", "SHIP_TEST_PR_BODY": "Refs HYP-1295"})
    assert r.returncode == 1
    assert '  in the title: "Resolves HYP-1295"' in r.stderr
    assert not _merged(tmp_path)


@pytest.mark.parametrize("keyword", ["close", "closes", "closed", "closing", "fix", "fixes", "fixed", "fixing", "resolve", "resolves", "resolved", "resolving", "CLOSES", "Fixed"])
def test_magic_close_covers_every_keyword_case_insensitively(repo, tmp_path, keyword):
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_BODY": f"{keyword} #42"})
    assert r.returncode == 1, f"{keyword}: STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert f'"{keyword} #42"' in r.stderr


@pytest.mark.parametrize("text,expected_phrase", [
    ("Fixes https://github.com/acme/widgets/issues/115", "Fixes https://github.com/acme/widgets/issues/115"),
    ("Closes https://github.com/acme/widgets/pull/9", "Closes https://github.com/acme/widgets/pull/9"),
])
def test_magic_close_catches_the_github_full_url_close_form(repo, tmp_path, text, expected_phrase):
    """GLM finding (round 1): GitHub documents the full issue/PR URL as an equally valid close
    target ("Fixes https://github.com/.../issues/N") — the #N/owner#N/HYP-N patterns alone
    missed it entirely, letting a PR merge with this exact close-behind-the-gates shape."""
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_BODY": text})
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert f'"{expected_phrase}"' in r.stderr
    assert not _merged(tmp_path)


def test_magic_close_rewrite_handles_the_github_full_url_close_form(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--rewrite-magic-close",), env={
        "SHIP_TEST_PR_BODY": "Fixes https://github.com/acme/widgets/issues/115",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    edits = [l for l in _gh_log(tmp_path) if l.startswith("edit ")]
    assert edits == ["edit 1 --body Refs https://github.com/acme/widgets/issues/115"]
    assert _merged(tmp_path)


@pytest.mark.parametrize("text", [
    "Refs #115",
    "Refs https://github.com/acme/widgets/issues/115",          # the full-URL form task-cli's links gate demands
    "Refs [#115](https://github.com/acme/widgets/issues/115)",  # ... and its markdown-link shape
    "Refs HYP-1295 — see the ticket",
    "fixes the bug in the parser",           # keyword without a reference
    "prefixes #1 with a marker",             # not a word boundary
    "fix: HYP-1295 do the thing",            # conventional-commit title: a colon is not a close
    "closed-form #3",                        # keyword not followed by whitespace
])
def test_magic_close_passes_clean_text(repo, tmp_path, text):
    r = _run(repo, tmp_path, env={"SHIP_TEST_PR_TITLE": text, "SHIP_TEST_PR_BODY": text})
    assert r.returncode == 0, f"{text!r}: STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "magic-close gate: no close/fix/resolve keyword" in r.stdout
    assert _merged(tmp_path)
    assert _audit(tmp_path, "magic-close") == []


def test_magic_close_rewrite_flag_edits_the_pr_and_continues(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--rewrite-magic-close",), env={
        "SHIP_TEST_PR_TITLE": "Fixes HYP-931: the thing",
        "SHIP_TEST_PR_BODY": "Closes #115.\nAlso resolves acme/widgets#9 (see Refs #7).",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "rewriting to Refs in #1: Fixes HYP-931;Closes #115;resolves acme/widgets#9" in r.stdout
    # the body is multi-line, so read the fake gh's log raw rather than line by line
    gh_log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "edit 1 --title Refs HYP-931: the thing --body Refs #115.\nAlso Refs acme/widgets#9 (see Refs #7).\n" in gh_log
    assert gh_log.count("edit ") == 1
    assert _merged(tmp_path)
    (line,) = _audit(tmp_path, "magic-close")
    assert line["decision"] == "rewritten" and line["detail"] == "Fixes HYP-931;Closes #115;resolves acme/widgets#9"


def test_magic_close_rewrite_touches_only_the_field_that_matched(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--rewrite-magic-close",), env={"SHIP_TEST_PR_BODY": "Closes #115"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert [l for l in _gh_log(tmp_path) if l.startswith("edit ")] == ["edit 1 --body Refs #115"]


def test_magic_close_dry_run_refuses_without_the_flag_and_only_prints_the_edit_with_it(repo, tmp_path):
    r = _run(repo, tmp_path, args=("--dry-run",), env={"SHIP_TEST_PR_BODY": "Closes #115"})
    assert r.returncode == 1
    assert "Refusing: magic-close keyword" in r.stderr
    assert "[dry-run] would append magic-close audit: decision=refused" in r.stderr
    assert not (tmp_path / "audit.jsonl").exists()

    r = _run(repo, tmp_path, args=("--dry-run", "--rewrite-magic-close"), env={"SHIP_TEST_PR_BODY": "Closes #115"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "[dry-run] gh pr edit 1 --body Refs #115" in r.stdout
    assert [l for l in _gh_log(tmp_path) if l.startswith("edit ")] == [], "dry-run must not edit the PR"
    assert "[dry-run] would append magic-close audit: decision=rewritten" in r.stderr


def test_magic_close_env_off_switch(repo, tmp_path):
    r = _run(repo, tmp_path, env={"SHIP_MAGIC_CLOSE_GATE": "0", "SHIP_TEST_PR_BODY": "Closes #115"})
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "magic-close gate disabled (SHIP_MAGIC_CLOSE_GATE=0)" in r.stdout
    assert _merged(tmp_path)


# ── the derived code must actually be a task-cli TICKET id (agent-tools#565) ─────────────────
# The acceptance gate is a MERGE gate, so what it asks `task gate` about has to be a ticket.
# The old shape check was "contains a digit", which every one of these passes:
#   * review-cli CHECK codes — `rig-cli-341`, `OC476-OPENCODE-BACKGROUND-TRUTH` — a different
#     entity entirely (a recorded review run, not a tracked ticket). `task gate` 404s on them,
#     exits 2, and the gate SKIPS: the merge proceeds with NOTHING verified (four real merges
#     shipped this way on 2026-09-06: PRs 349, 556, 352, 497).
#   * the PULL REQUEST's own number — `GH-<PR>` / `#<PR>`. PR #499's real ticket was #495 (in
#     both its branch and its title), but the gate asked about `#499`, an unrelated issue with
#     no criteria, and refused; the merge was only unblocked by recording a false
#     post-merge-acceptance opt-out on #499.
# task-cli's id grammar (tasklib/cli.py::_route_id_to_project) is exactly: `#<n>`, a bare `<n>`
# (GitHub issues), or ONE alphanumeric team prefix + `-` + digits (`HYP-931`, Linear). A
# candidate that does not fit falls through to the NEXT source, same as any other rejection.

_FELL_THROUGH_BODY = "Refs HYP-931"


def test_acceptance_gate_rejects_a_review_check_code_and_falls_through(repo, tmp_path):
    """`rig-cli-341` is a review-cli check code, not a ticket: two hyphens, no team-prefix
    shape. It must be rejected and the PR body's real ticket used instead."""
    r = _run(repo, tmp_path, branch="feat", env={
        "TASK_CODE": "rig-cli-341",
        "SHIP_TEST_PR_TITLE": "feat: the thing",
        "SHIP_TEST_PR_BODY": _FELL_THROUGH_BODY,
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate HYP-931 --json"], _task_calls(tmp_path)
    # The substitution is announced, with the source, not made silently.
    assert "rejected 'rig-cli-341' from $TASK_CODE" in r.stderr, r.stderr
    assert "not a task-cli id shape" in r.stderr, r.stderr


def test_acceptance_gate_rejects_a_descriptive_review_code_carrying_digits(repo, tmp_path):
    """`OC476-OPENCODE-BACKGROUND-TRUTH` has digits (so the old check passed it) but its
    trailing segment is not numeric — it is review-cli's descriptive arm, never a ticket."""
    r = _run(repo, tmp_path, branch="feat", env={
        "TASK_CODE": "OC476-OPENCODE-BACKGROUND-TRUTH",
        "SHIP_TEST_PR_TITLE": "feat: the thing",
        "SHIP_TEST_PR_BODY": _FELL_THROUGH_BODY,
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate HYP-931 --json"], _task_calls(tmp_path)


def test_acceptance_gate_rejects_the_pull_request_number_as_a_ticket_code(repo, tmp_path):
    """`GH-1` under PR #1 names the pull request, not a ticket (the PR #499/#495 incident).
    It must fall through to the body's real ticket."""
    r = _run(repo, tmp_path, branch="feat", env={
        "TASK_CODE": "GH-1",
        "SHIP_TEST_PR_TITLE": "feat: the thing",
        "SHIP_TEST_PR_BODY": _FELL_THROUGH_BODY,
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate HYP-931 --json"], _task_calls(tmp_path)
    assert "rejected 'GH-1' from $TASK_CODE" in r.stderr, r.stderr
    assert "names this pull request (#1)" in r.stderr, r.stderr


def test_acceptance_gate_skips_when_only_the_pull_request_number_is_derivable(repo, tmp_path):
    """With nothing but the PR's own number anywhere, the gate must report an honest "no task
    code" skip — never gate the PR against itself."""
    r = _run(repo, tmp_path, branch="feat", env={
        "TASK_CODE": "#1",
        "SHIP_TEST_PR_TITLE": "feat: the thing",
        "SHIP_TEST_PR_BODY": "no ticket here",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "could not derive a task code for #1 — skipping" in r.stderr
    assert _task_calls(tmp_path) == []
    assert _audit(tmp_path, "acceptance")[0]["detail"] == "no task code"


def test_acceptance_gate_rejects_the_bare_pull_request_number_as_a_ticket_code(repo, tmp_path):
    """The self-PR check accepts three spellings — `#<n>`, `GH-<n>` and a BARE `<n>`. The bare
    one is reached by falling through the `case` with no pattern matching, so it is the arm a
    reordering refactor could break silently; pin it alongside the other two."""
    r = _run(repo, tmp_path, branch="feat", env={
        "TASK_CODE": "1",
        "SHIP_TEST_PR_TITLE": "feat: the thing",
        "SHIP_TEST_PR_BODY": _FELL_THROUGH_BODY,
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate HYP-931 --json"], _task_calls(tmp_path)


def test_acceptance_gate_rejects_a_zero_padded_pull_request_number(repo, tmp_path):
    """`#01` / `GH-01` under PR #1 are the SAME pull request, but a literal string compare would
    call them "not this PR" and gate the PR against itself — the #499 hole, reopened by
    zero-padding (review round 2, Codex P2). The comparison is numeric, so they are rejected."""
    for code in ("#01", "GH-01", "001"):
        r = _run(repo, tmp_path, branch="feat", env={
            "TASK_CODE": code,
            "SHIP_TEST_PR_TITLE": "feat: the thing",
            "SHIP_TEST_PR_BODY": _FELL_THROUGH_BODY,
            "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
        })
        assert r.returncode == 1, f"{code}: STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        assert _task_calls(tmp_path) == ["gate HYP-931 --json"], (code, _task_calls(tmp_path))
        (tmp_path / "task.log").unlink(missing_ok=True)
        (tmp_path / "audit.jsonl").unlink(missing_ok=True)


def test_acceptance_gate_still_accepts_a_bare_issue_number(repo, tmp_path):
    """A bare `382` IS a task-cli id (GitHub issue) — the real shape must not over-reject it
    (the audit shows ship gating on exactly this form)."""
    r = _run(repo, tmp_path, branch="feat", env={
        "TASK_CODE": "382",
        "SHIP_TEST_PR_TITLE": "feat: the thing", "SHIP_TEST_PR_BODY": _FELL_THROUGH_BODY,
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED, "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate 382 --json"], _task_calls(tmp_path)


# ── agent-tools#571: a rejected candidate never hides a later one in the SAME source ─────────

_PR560_BODY = (Path(__file__).resolve().parent / "fixtures" / "pr560_body.md").read_text(encoding="utf-8")


def test_acceptance_gate_scans_past_a_self_pr_candidate_to_the_refs_line_in_the_same_body(repo, tmp_path):
    """The live 2026-09-06 incident, on PR #560's ORIGINAL body: an incidental `GH-560` (an
    example in the acceptance proofs) precedes the real `Refs #541`. The self-PR rule rightly
    rejects GH-560 — and the scan must go on to #541 in the same text instead of skipping the
    gate. The body also links agent-tools#559 in full; the keyword arm's single `Refs #541`
    answers first, so the URL arm is never consulted and that link is never derived."""
    r = _run(repo, tmp_path, branch="fix/hook-doctrine-541-542-546", pr="560", env={
        "SHIP_TEST_PR_TITLE": "fix(agent-hooks): read-only sed -n, shared harness exemption, reconciled no-bg-longproc doctrine",
        "SHIP_TEST_PR_BODY": _PR560_BODY,
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED.replace("HYP-931", "#541"), "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate #541 --json"], _task_calls(tmp_path)
    assert "rejected 'GH-560' from the PR body — it names this pull request (#560)" in r.stderr, r.stderr
    assert "Refusing: acceptance gate — ticket #541 is NOT accepted" in r.stderr
    assert "issues/559" not in r.stderr, r.stderr
    assert not _merged(tmp_path)


def test_acceptance_gate_reports_a_foreign_repo_issue_link_it_ignores(repo, tmp_path):
    """A full issue URL of ANOTHER repo is never this PR's ticket (agent-tools#564); with no
    other candidate the gate skips — and the transcript says WHICH link was set aside, so a
    "could not derive" is traceable to the text that caused it."""
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_BODY": "Companion of https://github.com/other-org/other-repo/issues/548.",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "ignoring 1 issue link(s) of another repo" in r.stderr, r.stderr
    assert "other-org/other-repo/issues/548" in r.stderr
    assert _task_calls(tmp_path) == []
    assert "could not derive a task code for #1 — skipping" in r.stderr


def test_acceptance_gate_says_own_repo_unknown_instead_of_calling_a_link_foreign(repo, tmp_path):
    """With NO own-repo slug at all (a non-GitHub origin, no --repo, and `gh pr view` giving no
    URL) an issue link cannot be told from another repo's — the note must say THAT, not send
    the shipper to fix a link that may well be this repo's (Opus review on PR #577)."""
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_BODY": "Companion of https://github.com/acme/widgets/issues/548.",
        "SHIP_TEST_GH_PR_VIEW_URL_FAIL": "1",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "not using 1 issue link(s) — this PR's own repo could not be established" in r.stderr, r.stderr
    assert "of another repo" not in r.stderr, r.stderr
    assert "acme/widgets/issues/548" in r.stderr
    assert _task_calls(tmp_path) == []


def test_acceptance_gate_reuses_the_quorum_code_with_its_real_source(repo, tmp_path):
    """The review-quorum gate found the ticket in the PR BODY (a passing record for #999); the
    acceptance gate reuses that code, and when task-cli cannot resolve it the refusal must
    blame the PR body — not "$TASK_CODE", a label nothing in the PR text matches (codex review
    on PR #577)."""
    r = _run(repo, tmp_path, branch="feat", with_review=True, env={
        "SHIP_REVIEW_QUORUM": "1", "SHIP_TEST_REVIEW_KNOWN_CODES": "#999",
        "SHIP_TEST_PR_BODY": "Refs #999",
        "SHIP_TEST_TASK_GATE_JSON": "", "SHIP_TEST_TASK_GATE_ERR": "error: github: HTTP 404 for issue 999",
        "SHIP_TEST_TASK_GATE_EXIT": "2",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "AUTHORITY CONFIRMED" in r.stdout and "for #999" in r.stdout, r.stdout
    assert "task-code: using '#999' from the PR body." in r.stderr, r.stderr
    assert "Refusing: acceptance gate — task-cli could not resolve ticket #999" in r.stderr, r.stderr
    assert "The code came from the PR body." in r.stderr, r.stderr
    assert "$TASK_CODE" not in r.stderr, r.stderr
    assert _task_calls(tmp_path) == ["gate #999 --json"]
    assert not _merged(tmp_path)


def test_acceptance_gate_does_not_reuse_a_record_less_quorum_spelling_after_a_hatch_approval(repo, tmp_path):
    """The quorum gate found NO record for any candidate (the body's incidental `UTF-8`), Alex
    approved the ship live through the hatch — the acceptance gate must then derive afresh
    (here: HYP-777 from the PR TITLE, which the quorum gate never reads), not ask task-cli
    about the stale record-less `UTF-8` and refuse right after the approval (Opus review on
    PR #577)."""
    ship_copy = _ship_copy_with_approving_hatch(tmp_path)
    r = _run(repo, tmp_path, branch="feat", with_review=True, ship=ship_copy, env={
        "SHIP_REVIEW_QUORUM": "1", "SHIP_TEST_REVIEW_KNOWN_CODES": "NONE",
        "RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM": "test: hotfix, review dispatched",
        "SHIP_TEST_PR_TITLE": "HYP-777: fix the thing", "SHIP_TEST_PR_BODY": "Decodes UTF-8 properly now.",
        "SHIP_TEST_TASK_GATE_JSON": _ACCEPTED.replace("HYP-931", "HYP-777"), "SHIP_TEST_TASK_GATE_EXIT": "0",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "hatch escalation was APPROVED" in r.stdout, r.stdout
    assert "no recorded review iterations for UTF-8" in r.stderr, r.stderr
    assert _task_calls(tmp_path) == ["gate HYP-777 --json"], _task_calls(tmp_path)
    assert "task-code: using 'HYP-777' from the PR title." in r.stderr, r.stderr
    assert "could not resolve ticket UTF-8" not in r.stderr, r.stderr
    assert _merged(tmp_path)


def test_acceptance_gate_scans_past_a_grammar_rejected_candidate_in_the_same_body(repo, tmp_path):
    """A descriptive review-cli code (the extractor's third arm, ahead of the keyword arm) is
    not a task-cli id; the keyword-anchored ref later in the SAME body must still be found."""
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_BODY": "Reviewed as SME-ROADMAP-WORKTREE-NOTE (3 rounds). Refs #77",
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED.replace("HYP-931", "#77"), "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate #77 --json"], _task_calls(tmp_path)
    assert "rejected 'SME-ROADMAP-WORKTREE-NOTE' from the PR body — not a task-cli id shape" in r.stderr, r.stderr


def test_acceptance_gate_source_precedence_survives_the_continuation(repo, tmp_path):
    """Continuation is WITHIN a source: a surviving title candidate still beats a body one
    (explicit env -> branch -> title -> body is unchanged). Both title tokens sit in the same
    extractor arm (generic PREFIX-<n>), so they are tried in document order: GH-1 names this
    PR (#1) and is rejected, PROJ-777 survives."""
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_TITLE": "GH-1 then PROJ-777: the thing",
        "SHIP_TEST_PR_BODY": "Refs #77",
        "SHIP_TEST_TASK_GATE_JSON": _NOT_ACCEPTED.replace("HYP-931", "PROJ-777"), "SHIP_TEST_TASK_GATE_EXIT": "1",
    })
    assert r.returncode == 1, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert _task_calls(tmp_path) == ["gate PROJ-777 --json"], _task_calls(tmp_path)
    assert "rejected 'GH-1' from the PR title" in r.stderr, r.stderr
    assert "task-code: using 'PROJ-777' from the PR title" in r.stderr, r.stderr


def test_acceptance_gate_skips_when_every_candidate_in_every_source_is_rejected(repo, tmp_path):
    r = _run(repo, tmp_path, branch="feat", env={
        "SHIP_TEST_PR_TITLE": "GH-1: the thing", "SHIP_TEST_PR_BODY": "Refs #1 and SME-ROADMAP-WORKTREE-NOTE",
    })
    assert r.returncode == 0, f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    assert "could not derive a task code for #1 — skipping" in r.stderr
    assert _task_calls(tmp_path) == []
    assert _audit(tmp_path, "acceptance")[0]["detail"] == "no task code"
