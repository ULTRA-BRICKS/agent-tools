"""Tests for ci/ship/ship.sh — the merge-time version auto-bump (#518).

The version-bump gate used to make every parallel PR bump the same line to the same next
version, so the second one to merge was CONFLICTING. Now, when that gate would refuse, ship
bumps the PATCH version itself on the PR head branch through the GitHub Contents API (after
updating the branch from base when base's version already moved), then carries on through the
pipeline treating its own commit correctly at every gate.

Hermetic, but REAL where it matters: the fake `gh` below is backed by a real bare `origin` +
a private clone, so `pr diff`, the Contents API GET/PUT, `update-branch` and `pr merge` are
real git operations (a squash merge that would conflict really fails). ship.sh's own local
half (branch sanity, fast-forward, cleanup) runs against a separate clone of the same origin.
The graphql fakes run the REAL `--jq` filter ship passes over a canned payload, so the dwell
and thread filters are exercised, not stubbed.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship_auto_bump.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# The review-quorum gate defaults ENABLED. SHIP_REVIEW_QUORUM=0 is forbidden
# (agent-tools#644) and no longer disables it. Isolate via the test-only
# SHIP_TEST_DISABLE_REVIEW_QUORUM=1 (mirrors tests/test_ship.py).
os.environ.setdefault("SHIP_TEST_DISABLE_REVIEW_QUORUM", "1")
# Pre-merge ticket gates default ENABLED too and live in tests/test_ship_ticket_gates.py;
# off here so a fixture PR body is not gated by a feature this file never set out to exercise.
os.environ.setdefault("SHIP_ACCEPTANCE_GATE", "0")
os.environ.setdefault("SHIP_MAGIC_CLOSE_GATE", "0")
# Unconditional: an ambient SHIP_TASK_NOTIFY_ENABLED=1 would invoke a real `task` binary.
os.environ["SHIP_TASK_NOTIFY_ENABLED"] = "0"

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"

_OLD_DATE = "2020-01-01T00:00:00Z"

_FAKE_GH = r'''#!/usr/bin/env bash
# Fake gh for the auto-bump suite: state lives in $SHIP_TEST_STATE (a clone of the bare origin
# under clone/, threads.json, gh.log). See the module docstring of the test file.
set -e
S="$SHIP_TEST_STATE"; C="$S/clone"; LOG="$S/gh.log"; B="$SHIP_TEST_BRANCH"
argstr="$*"
log() { printf '%s\n' "$*" >> "$LOG"; }
git_c() { git -C "$C" -c core.hooksPath= -c user.email=gh@fake -c user.name=fakegh "$@"; }
green='[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]'
red='[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"FAILURE","workflowName":"CI"}]'
sub="$1"; shift || true
case "$sub" in
  pr)
    action="$1"; shift || true
    case "$action" in
      view)
        if grep -q -- '--json reviews' <<<"$argstr"; then echo "${SHIP_TEST_REVIEW_COUNT:-1}"
        elif grep -q -- '--json mergeStateStatus' <<<"$argstr"; then
          if [ -n "${SHIP_TEST_MERGE_STATE_REREAD_FAIL:-}" ]; then echo "fake gh: simulated read failure" >&2; exit 1; fi
          echo "${SHIP_TEST_MERGE_STATE_AFTER:-CLEAN}"
        elif grep -q headRefName <<<"$argstr"; then printf '%s\tOPEN\tMERGEABLE\t%s\t%s\n' "$B" "${SHIP_TEST_CROSS_REPO:-false}" "${SHIP_TEST_MERGE_STATE:-CLEAN}"
        elif grep -q statusCheckRollup <<<"$argstr"; then
          git_c fetch -q origin
          msg=$(git_c log -1 --format=%s "origin/$B")
          if [ -n "${SHIP_TEST_RED_AFTER_BUMP:-}" ] && grep -q 'ship auto-bump' <<<"$msg"; then echo "$red"; else echo "$green"; fi
        elif grep -q headRefOid <<<"$argstr"; then
          # SHIP_TEST_STALL_HEAD_AFTER_BUMP: once the bump commit lands (marker written by the
          # PUT handler below), keep reporting the PRE-bump sha forever -- simulates GitHub
          # eventual-consistency lag outliving ship's own wait window (or another push racing
          # past it), independent of whether the real remote write actually succeeded.
          # SHIP_TEST_FAIL_HEADREFOID_CALL_N: fail exactly the Nth headRefOid call this run
          # (1-based) with a transient error; every other call succeeds normally -- lets a test
          # target one SPECIFIC read (e.g. the post-merge re-read inside
          # behind_clear_pr_branch_via_update_api) without disturbing any other call site.
          _n=$(( $(cat "$S/headrefoid_calls" 2>/dev/null || echo 0) + 1 ))
          printf '%s' "$_n" > "$S/headrefoid_calls"
          if [ -n "${SHIP_TEST_FAIL_HEADREFOID_CALL_N:-}" ] && [ "$_n" = "$SHIP_TEST_FAIL_HEADREFOID_CALL_N" ]; then
            echo "fake gh: simulated transient headRefOid read failure (call $_n)" >&2; exit 1
          elif [ -n "${SHIP_TEST_STALL_HEAD_AFTER_BUMP:-}" ] && [ -f "$S/pre_bump_sha" ]; then
            cat "$S/pre_bump_sha"
          else
            git_c fetch -q origin; git_c rev-parse "origin/$B"
          fi
        elif grep -q baseRefName <<<"$argstr"; then echo main
        elif grep -q -- '--json commits' <<<"$argstr"; then git_c fetch -q origin; git_c log -1 --format=%s "origin/$B"
        else echo '[]'; fi ;;
      diff)
        git_c fetch -q origin
        if grep -q -- '--name-only' <<<"$argstr"; then git_c diff --name-only "origin/main...origin/$B"
        else git_c diff "origin/main...origin/$B"; fi ;;
      merge)
        log "merge $*"
        git_c fetch -q origin
        git_c checkout -q -B main origin/main
        if ! git_c merge --squash -q "origin/$B" >/dev/null 2>&1; then
          echo "fake gh: squash merge CONFLICT" >&2; git_c reset -q --hard; exit 1
        fi
        git_c commit -q -m "squash PR ($B)"
        git_c push -q origin main
        echo "[fake gh] merged" ;;
      *) : ;;
    esac ;;
  api)
    method=GET; endpoint=""; jqexpr=""
    F_message=""; F_content=""; F_sha=""; F_branch=""; F_expected=""; F_query=""; F_id=""; F_oid=""
    F_committer_email=""; F_committer_name=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -X) method="$2"; shift 2 ;;
        -f|-F) kv="$2"; shift 2; k="${kv%%=*}"; v="${kv#*=}"
          case "$k" in
            message) F_message="$v" ;; content) F_content="$v" ;; sha) F_sha="$v" ;; branch) F_branch="$v" ;;
            expected_head_sha) F_expected="$v" ;; query) F_query="$v" ;; id) F_id="$v" ;; oid) F_oid="$v" ;;
            committer\[email\]) F_committer_email="$v" ;; committer\[name\]) F_committer_name="$v" ;;
          esac ;;
        --jq) jqexpr="$2"; shift 2 ;;
        -*) shift ;;
        *) endpoint="$1"; shift ;;
      esac
    done
    case "$endpoint" in
      graphql)
        if grep -q resolveReviewThread <<<"$F_query"; then
          log "resolve $F_id"
          tmp=$(jq --arg id "$F_id" '(.data.repository.pullRequest.reviewThreads.nodes[] | select(.id==$id) | .isResolved) = true' "$S/threads.json")
          printf '%s' "$tmp" > "$S/threads.json"; echo '{"data":{}}'
        elif grep -q reviewThreads <<<"$F_query"; then
          if [ -f "$S/threads.json" ]; then jq -r "$jqexpr" "$S/threads.json"
          else printf '%s' '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false},"nodes":[]}}}}}' | jq -r "$jqexpr"; fi
        elif grep -q committedDate <<<"$F_query"; then
          git_c fetch -q origin
          # A PR's commit list = commits reachable from head but not from base (a merge FROM base
          # is in it; base's own commits are not), chronological; ship asks for the last 15. Build
          # each node's JSON via jq (one commit at a time) rather than a hand-rolled control-byte
          # field separator in the git-log format string -- a literal separator byte embedded in a
          # bash script is invisible and fragile (a real review finding on this file, #518).
          nodes='[]'
          for _sha in $(git_c log --reverse --format=%H "origin/main..origin/$B" | tail -15); do
            _msg=$(git_c log -1 --format=%s "$_sha")
            _cdate=$(TZ=UTC git_c log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd "$_sha")
            _cemail=$(git_c log -1 --format=%ce "$_sha")
            nodes=$(jq --arg oid "$_sha" --arg msg "$_msg" --arg cd "$_cdate" --arg ce "$_cemail" \
              '. + [{commit:{oid:$oid, message:$msg, committedDate:$cd, pushedDate:null, committer:{email:$ce}}}]' <<<"$nodes")
          done
          jq -n --argjson nodes "$nodes" --arg created "${SHIP_TEST_CREATED:-2020-01-01T00:00:00Z}" \
            '{data:{repository:{pullRequest:{createdAt:$created, commits:{nodes:$nodes}, timelineItems:{nodes:[]}}}}}' | jq -r "$jqexpr"
        elif grep -q messageHeadline <<<"$F_query"; then
          git_c fetch -q origin
          _headline=$(git_c log -1 --format=%s "origin/$B" 2>/dev/null || echo "")
          jq -n --arg h "$_headline" \
            '{data:{repository:{pullRequest:{commits:{nodes:[{commit:{messageHeadline:$h}}]}}}}}' | jq -r "$jqexpr"
        elif grep -q parents <<<"$F_query"; then
          git_c fetch -q origin
          _pset=$(git_c log -1 --format=%P "$F_oid" 2>/dev/null || echo "")
          _pnodes=$(printf '%s\n' $_pset | jq -R -s -c 'split("\n") | map(select(length>0)) | map({oid:.})')
          jq -n --argjson pn "$_pnodes" \
            '{data:{repository:{object:{parents:{totalCount:($pn|length), nodes:$pn}}}}}' | jq -r "$jqexpr"
        else echo 0; fi ;;
      repos/*/contents/*)
        path="${endpoint#*/contents/}"; file="${path%%\?*}"; ref="${path#*ref=}"; [ "$ref" = "$path" ] && ref=""
        git_c fetch -q origin
        if [ "$method" = "GET" ]; then
          log "GET contents file=$file ref=$ref"
          blob=$(git_c rev-parse "origin/$ref:$file" 2>/dev/null) || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
          content=$(git_c show "origin/$ref:$file" | base64 | tr -d '\n')
          jq -n --arg sha "$blob" --arg content "$content" '{sha:$sha, content:($content+"\n"), encoding:"base64"}' | jq -r "$jqexpr"
        else
          log "PUT contents file=$file branch=$F_branch sha=$F_sha message=$F_message"
          [ -n "${SHIP_TEST_BUMP_PUT_FAIL:-}" ] && { echo "gh: Update is not a fast forward (HTTP 409)" >&2; exit 1; }
          cur=$(git_c rev-parse "origin/$F_branch:$file")
          [ "$cur" = "$F_sha" ] || { echo "gh: $file does not match $F_sha (HTTP 409)" >&2; exit 1; }
          [ -n "${SHIP_TEST_STALL_HEAD_AFTER_BUMP:-}" ] && git_c rev-parse "origin/$F_branch" > "$S/pre_bump_sha"
          git_c checkout -q -B "$F_branch" "origin/$F_branch"
          printf '%s' "$F_content" | base64 -d > "$C/$file"
          git_c add -- "$file"
          # Real GitHub Contents API honours an explicit author/committer object -- ship.sh
          # (#518) always sends one so the review-dwell gate can recognize its own commit by
          # COMMITTER IDENTITY rather than by message text (see SHIP_AUTO_BUMP_COMMITTER_EMAIL
          # in ship.sh). GIT_COMMITTER_* env wins over the `-c user.email=` baked into git_c().
          GIT_AUTHOR_NAME="${F_committer_name:-t}" GIT_AUTHOR_EMAIL="${F_committer_email:-t@t}" \
          GIT_COMMITTER_NAME="${F_committer_name:-t}" GIT_COMMITTER_EMAIL="${F_committer_email:-t@t}" \
          git_c commit -q -m "$F_message"
          git_c push -q origin "$F_branch"
          jq -n --arg sha "$(git_c rev-parse HEAD)" '{commit:{sha:$sha}}' | jq -r "$jqexpr"
        fi ;;
      repos/*/pulls/*/update-branch)
        log "PUT update-branch expected=$F_expected"
        git_c fetch -q origin
        git_c checkout -q -B "$B" "origin/$B"
        if ! git_c merge -q --no-ff -m "Merge branch 'main' into $B" origin/main >/dev/null 2>&1; then
          git_c merge --abort 2>/dev/null || true; echo "gh: merge conflict (HTTP 422)" >&2; exit 1
        fi
        git_c push -q origin "$B"
        echo '{"message":"Updating pull request branch.","url":""}' ;;
      *) echo 0 ;;
    esac ;;
  *) : ;;
esac
'''

_PYPROJECT = '[project]\nname = "mytool"\nversion = "1.0.0"\n'
_VERSION_LINE = 3  # 1-based line of `version = ...` in _PYPROJECT

def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)

def _git(*args, cwd, date=_OLD_DATE):
    """Bootstrap git op with hooks neutralized and an OLD commit date (so the review-dwell gate
    is satisfied by the PR's own commits and only ship's fresh commits are 'now')."""
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    r = _sh("git", "-c", "core.hooksPath=", "-c", "user.email=t@t", "-c", "user.name=t", *args, cwd=cwd, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r

def _commit_file(repo: Path, rel: str, text: str, msg: str, date=_OLD_DATE):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo, date=date)
    _git("commit", "-qm", msg, cwd=repo, date=date)

def _make_world(tmp_path: Path, *, with_version_file: bool = True):
    """A bare origin, ship's local clone `main` (branch main), and the fake gh's state dir."""
    if not shutil.which("bash") or not shutil.which("git") or not shutil.which("jq"):
        pytest.skip("bash/git/jq required")
    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    if with_version_file:
        (main / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    state = tmp_path / "state"
    state.mkdir()
    _sh("git", "clone", "-q", str(origin), str(state / "clone"), cwd=tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    return main, origin, state, bindir

def _make_pr_branch(main: Path, branch: str, rel: str, text: str, *, date=_OLD_DATE) -> Path:
    """Branch off main's current tip in a worktree, commit one file, push. Returns the worktree."""
    wt = main.parent / f"wt-{branch}"
    _git("worktree", "add", "-q", "-b", branch, str(wt), "main", cwd=main)
    _commit_file(wt, rel, text, f"feat: {rel}", date=date)
    _git("push", "-q", "origin", branch, cwd=wt)
    return wt

def _run_ship(main: Path, bindir: Path, state: Path, branch: str, *extra, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["SHIP_TEST_STATE"] = str(state)
    env["SHIP_TEST_BRANCH"] = branch
    env["SHIP_DEFAULT_BRANCH"] = "main"
    env["SHIP_MAIN_CHECKOUT"] = str(main)
    env["SHIP_TEST_DISABLE_REVIEW_QUORUM"] = "1"
    env.pop("SHIP_REVIEW_QUORUM", None)  # forbidden (agent-tools#644); isolation is the test-only env above
    env["SHIP_TASK_NOTIFY_ENABLED"] = "0"
    env.setdefault("SHIP_ACCEPTANCE_GATE", "0")
    env.setdefault("SHIP_MAGIC_CLOSE_GATE", "0")
    env["SHIP_AUDIT_FILE"] = str(state / "audit.jsonl")
    env["SHIP_AUTO_BUMP_HEAD_WAIT"] = "5"
    env["SHIP_AUTO_BUMP_HEAD_POLL"] = "1"
    env.pop("SHIP_AUTO_BUMP", None)  # the feature under test defaults ON
    if env_extra:
        env.update(env_extra)
    return _sh("bash", str(_SHIP), "1", "--no-screenshot-ok", "test", *extra, cwd=cwd or main, env=env)

def _origin_file(origin: Path, ref: str, rel: str) -> str:
    r = _sh("git", "show", f"{ref}:{rel}", cwd=origin)
    assert r.returncode == 0, r.stderr
    return r.stdout

def _origin_log(origin: Path, ref: str) -> list[str]:
    r = _sh("git", "log", "--format=%s", ref, cwd=origin)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()

def _gh_log(state: Path) -> str:
    p = state / "gh.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""

def _thread(tid: str, *, path: str, line: int, login: str, resolved: bool = False,
            outdated: bool = True, body: str = "nit: version") -> dict:
    """Default outdated=True matches --resolve-addressed-threads: only outdated bot nits
    are eligible. Pass outdated=False / a P1 body to prove those filters still apply
    on the bump line (P1 from #536)."""
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated,
        "path": path, "line": line, "originalLine": line,
        "comments": {"totalCount": 1, "nodes": [{"author": {"login": login}, "body": body}]},
    }

def _write_threads(state: Path, *threads: dict) -> None:
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(threads)}}}}}
    (state / "threads.json").write_text(json.dumps(payload), encoding="utf-8")

# ---------------------------------------------------------------------------------------
# The core: a source PR with no bump is auto-bumped and merged.
# ---------------------------------------------------------------------------------------

def test_source_pr_without_bump_is_auto_bumped_and_merged(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "auto-bump: pyproject.toml 1.0.0 -> 1.0.1 committed as" in r.stdout, r.stdout
    assert "version-bump gate OK — pyproject.toml version auto-bumped 1.0.0 -> 1.0.1" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout
    log = _gh_log(state)
    assert "PUT contents file=pyproject.toml branch=feat" in log, log
    assert "chore(release): bump version 1.0.0 -> 1.0.1 (ship auto-bump for #1)" in log, log
    assert "PUT update-branch" not in log, "base had not moved: no branch update expected"
    assert 'version = "1.0.1"' in _origin_file(origin, "main", "pyproject.toml")
    # The bump commit went onto the PR HEAD branch, not main directly.
    assert "PUT contents file=pyproject.toml branch=feat" in log
    # The worktree was fast-forwarded to the bumped head before the (second) sanity check.
    assert "fast-forwarded worktree" in r.stdout, r.stdout
    assert not wt.exists(), "cleanup should have removed the PR worktree"

def test_back_to_back_prs_neither_touching_version_file_both_merge(tmp_path):
    """The acceptance criterion of #518: two PRs branched from the same main, neither touching
    pyproject.toml. The first is auto-bumped 1.0.0 -> 1.0.1. The second's merge-base still
    reads 1.0.0 while main reads 1.0.1 — a naive bump on its head (1.0.0 -> 1.0.2) would
    CONFLICT in the squash three-way merge on the version line. ship updates the branch from
    main first, then bumps 1.0.1 -> 1.0.2, and the real squash merge succeeds."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat-a", "src/a.py", "a = 1\n")
    _make_pr_branch(main, "feat-b", "src/b.py", "b = 1\n")

    ra = _run_ship(main, bindir, state, "feat-a")
    assert ra.returncode == 0, f"{ra.stdout}\n{ra.stderr}"
    assert 'version = "1.0.1"' in _origin_file(origin, "main", "pyproject.toml")

    rb = _run_ship(main, bindir, state, "feat-b")
    assert rb.returncode == 0, f"{rb.stdout}\n{rb.stderr}"
    assert "updating feat-b from main first" in rb.stdout, rb.stdout
    assert "auto-bump: pyproject.toml 1.0.1 -> 1.0.2 committed as" in rb.stdout, rb.stdout
    assert "[fake gh] merged" in rb.stdout
    log = _gh_log(state)
    assert "PUT update-branch" in log, log
    assert 'version = "1.0.2"' in _origin_file(origin, "main", "pyproject.toml")
    # Both PRs' files are on main: neither merge was lost to a conflict.
    assert _origin_file(origin, "main", "src/a.py") == "a = 1\n"
    assert _origin_file(origin, "main", "src/b.py") == "b = 1\n"

def test_api_push_rejected_refuses_and_merges_nothing(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_BUMP_PUT_FAIL": "1"})

    assert r.returncode != 0, r.stdout
    assert "version-bump commit was REJECTED by GitHub" in r.stderr, r.stderr
    assert "HTTP 409" in r.stderr, r.stderr
    assert "Nothing was merged" in r.stderr
    assert "merge" not in _gh_log(state).replace("PUT", ""), _gh_log(state)
    assert "[fake gh] merged" not in r.stdout
    assert 'version = "1.0.0"' in _origin_file(origin, "feat", "pyproject.toml"), "branch must be untouched"

def test_ci_red_after_bump_refuses_with_guidance_and_rerun_needs_no_second_bump(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_RED_AFTER_BUMP": "1"})

    assert r.returncode != 0, r.stdout
    assert "PUT contents file=pyproject.toml branch=feat" in _gh_log(state)
    assert "waiting on the checks of ship's own push" in r.stdout, r.stdout
    assert "check(s) not passing" in r.stderr, r.stderr
    assert "Fix CI, then re-run" in r.stderr
    assert "[fake gh] merged" not in r.stdout
    # The bump commit is left on the branch (the refusal message points at fixing CI, not at
    # the bump), so a re-run sees the PR as already bumped and makes NO second bump.
    assert _origin_log(origin, "feat")[0].startswith("chore(release): bump version 1.0.0 -> 1.0.1")
    put_count_before = _gh_log(state).count("PUT contents")

    r2 = _run_ship(main, bindir, state, "feat")

    assert r2.returncode == 0, f"{r2.stdout}\n{r2.stderr}"
    assert "PR already bumps pyproject.toml (1.0.0 -> 1.0.1" in r2.stdout, r2.stdout
    assert _gh_log(state).count("PUT contents") == put_count_before, "no second bump on re-run"
    assert 'version = "1.0.1"' in _origin_file(origin, "main", "pyproject.toml")

def test_pr_that_already_bumps_gets_no_second_bump(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.1.0"), "chore: minor bump")
    _git("push", "-q", "origin", "feat", cwd=wt)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "not needed — PR already bumps pyproject.toml (1.0.0 -> 1.1.0, main at 1.0.0)" in r.stdout, r.stdout
    assert "version-bump gate OK — pyproject.toml version is bumped in PR #1" in r.stdout, r.stdout
    assert "PUT contents" not in _gh_log(state)
    assert 'version = "1.1.0"' in _origin_file(origin, "main", "pyproject.toml")

def test_hand_bump_raced_by_main_is_rebumped_on_top_of_base(tmp_path):
    """The parallel-PR race the issue describes: the PR hand-bumped to 1.0.1, but main already
    shipped 1.0.1 (another PR). Squashing as-is would conflict, and merging clean would land
    NO bump. ship updates the branch (the two 1.0.1 lines merge clean) and bumps 1.0.1 -> 1.0.2."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore: patch bump")
    _git("push", "-q", "origin", "feat", cwd=wt)
    # main moves to 1.0.1 independently (another PR shipped).
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "bumps pyproject.toml 1.0.0 -> 1.0.1 but main is already at 1.0.1 — re-bumping" in r.stdout, r.stdout
    assert "PUT update-branch" in _gh_log(state)
    assert 'version = "1.0.2"' in _origin_file(origin, "main", "pyproject.toml")

def test_hand_bump_race_already_merged_bumps_the_current_head_cleanly(tmp_path):
    """A hand-bumped PR (1.0.0 -> 1.0.1) raced by main (also now 1.0.1) whose branch has
    ALREADY been merged with base (a human's own `git merge main`, or clicking GitHub's
    "Update branch" banner) before ship ever runs -- the head's version file already reads
    1.0.1, same as base. A real git merge of two commits that independently reach the SAME
    version value collapses to no file-level diff at all for that line, so
    version_line_bumped reports "not bumped" (empty VB_OLDV/VB_NEWV) and the PR is correctly
    treated as a plain (non-hand-bumped) auto-bump: ship must bump the CURRENT head directly
    (1.0.1 -> 1.0.2) with no update-branch call (the branches are already reconciled).

    (A prior investigation into this scenario considered skipping update-branch whenever
    $AB_HEADV already equals $AB_BASEV as a general rule, independent of whether the PR's own
    diff shows a hand bump -- that was reverted: it broke the sibling test below, whose
    branches are NOT git-ancestrally reconciled even though their file CONTENT coincidentally
    matches, by skipping the update-branch merge the later squash needs to avoid a real
    conflict. Content matching is not the same as history being reconciled.)"""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore: patch bump")
    # main moves to 1.0.1 independently (another PR shipped) BEFORE feat merges it in.
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)
    # feat already merged main in (a human's own `git merge main`, or an earlier BEHIND-clearing
    # preflight this same run) -- both sides agree on 1.0.1, so the merge is clean and adds no
    # conflict; feat's head now reads the SAME version as base.
    _git("fetch", "-q", "origin", "main", cwd=wt)
    _git("merge", "-q", "--no-ff", "-m", "Merge branch 'main' into feat", "origin/main", cwd=wt)
    _git("push", "-q", "origin", "feat", cwd=wt)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PUT update-branch" not in _gh_log(state), "the head already reflects base -- nothing to update"
    assert "PUT contents" in _gh_log(state), "the bump must still proceed against the current head"
    assert 'version = "1.0.2"' in _origin_file(origin, "main", "pyproject.toml")

def test_opt_out_via_env_restores_the_refusal(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_AUTO_BUMP": "0"})

    assert r.returncode != 0, r.stdout
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "did not here because: SHIP_AUTO_BUMP=0" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state)
    assert "[fake gh] merged" not in r.stdout

@pytest.mark.parametrize("spelling", ["FALSE", "Off", "NO"])
def test_opt_out_case_insensitive_spellings(tmp_path, spelling):
    """Review finding, #518: a case-SENSITIVE `0|false|no` match left `SHIP_AUTO_BUMP=FALSE` /
    `=OFF` reading as "not recognized" -> the feature stayed ON -- backwards from every natural
    spelling of "off" an operator would reach for, on the one knob that gates remote writes."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_AUTO_BUMP": spelling})

    assert r.returncode != 0, r.stdout
    assert "did not here because: SHIP_AUTO_BUMP=0" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state), _gh_log(state)

def test_opt_out_via_committed_ship_config(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _commit_file(main, ".ship-config", "SHIP_AUTO_BUMP=0\n", "chore: opt out of auto-bump")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "SHIP_AUTO_BUMP=0 (env or .ship-config)" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state)

def test_ship_config_opt_out_must_be_committed_at_head(tmp_path):
    """An uncommitted .ship-config is ignored (audited-config rule), so the auto-bump stays on."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    (main / ".ship-config").write_text("SHIP_AUTO_BUMP=0\n", encoding="utf-8")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PUT contents" in _gh_log(state)
    (main / ".ship-config").unlink()

def test_repo_without_version_file_keeps_note_and_skip(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "no version file (pyproject.toml/package.json) found at repo root — skipping" in r.stdout, r.stdout
    assert "PUT contents" not in _gh_log(state)
    assert "[fake gh] merged" in r.stdout

def test_dry_run_prints_the_plan_and_pushes_nothing(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", "--dry-run")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "[dry-run] would bump pyproject.toml 1.0.0 -> 1.0.1 on feat via the GitHub Contents API" in r.stdout, r.stdout
    assert "nothing pushed" in r.stdout
    assert "[dry-run] version-bump gate would be satisfied by the planned auto-bump" in r.stdout, r.stdout
    assert "would append version-bump audit: decision=version-bump:auto" in r.stderr, r.stderr
    assert "PUT" not in _gh_log(state), _gh_log(state)
    assert "merge" not in _gh_log(state)
    assert not (state / "audit.jsonl").exists()
    assert _origin_log(origin, "feat")[0] == "feat: src/a.py"

def test_dry_run_plans_the_branch_update_when_base_moved(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", "--dry-run")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "[dry-run] would update feat from main first (main is at 1.0.1, this PR's merge-base at 1.0.0)" in r.stdout, r.stdout
    assert "[dry-run] would bump pyproject.toml 1.0.1 -> 1.0.2" in r.stdout, r.stdout
    assert "PUT" not in _gh_log(state)

def test_review_dwell_is_measured_from_the_last_non_ship_push(tmp_path):
    """The PR's own commits are old (2020); ship's bump commit is 'now'. With the default
    600s window the merge passes only because the dwell query skips ship's trailing bump."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "review-dwell gate OK" in r.stdout, r.stdout
    assert "[fake gh] merged" in r.stdout

def test_review_dwell_still_counts_a_fresh_human_push(tmp_path):
    """Negative control for the test above: a fresh (now) commit by the PR author — not ship —
    is a real push and the dwell gate refuses on it (the skip is narrow, not a blanket bypass)."""
    import datetime as _dt
    main, origin, state, bindir = _make_world(tmp_path)
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n", date=now)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode != 0, r.stdout
    assert "review-dwell window is 600s" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout

def test_review_dwell_skips_ship_update_branch_merge_before_the_bump(tmp_path):
    """When base moved, ship's update-branch merge commit (also 'now') sits right before the
    bump; both are ship's and neither resets the window."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "src/other.py", "o = 1\n", "feat: other")
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.1"), "chore(release): 1.0.1")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PUT update-branch" in _gh_log(state)
    assert "review-dwell gate OK" in r.stdout, r.stdout

def test_bot_thread_on_the_bump_line_is_auto_resolved(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(state, _thread("T_BOT", path="pyproject.toml", line=_VERSION_LINE, login="chatgpt-codex-connector"))

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "auto-bump threads: resolved bot thread T_BOT on ship's bump line pyproject.toml:3" in r.stdout, r.stdout
    assert "resolve T_BOT" in _gh_log(state)
    assert "[fake gh] merged" in r.stdout

def test_human_thread_on_the_bump_line_still_blocks(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(state, _thread("T_HUMAN", path="pyproject.toml", line=_VERSION_LINE, login="alex"))

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "resolve T_HUMAN" not in _gh_log(state)
    assert "1 unresolved review thread(s)" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout

def test_bot_thread_elsewhere_in_the_version_file_is_not_touched(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(state, _thread("T_OTHER", path="pyproject.toml", line=1, login="chatgpt-codex-connector"))

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "resolve T_OTHER" not in _gh_log(state)
    assert "1 unresolved review thread(s)" in r.stderr, r.stderr

def test_audit_line_records_the_auto_bump(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    lines = [json.loads(l) for l in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    bump = [l for l in lines if l.get("decision") == "version-bump:auto"]
    assert len(bump) == 1, lines
    rec = bump[0]
    assert rec["pr"] == "1" and rec["gate"] == "version-bump" and rec["file"] == "pyproject.toml"
    assert rec["old"] == "1.0.0" and rec["new"] == "1.0.1" and rec["branch"] == "feat"
    assert len(rec["sha"]) == 40 and all(c in "0123456789abcdef" for c in rec["sha"]), rec
    # The recorded sha is the real bump commit ship authored on the PR branch (it survives in
    # the origin object store even after the squash + branch delete).
    assert _sh("git", "cat-file", "-t", rec["sha"], cwd=origin).stdout.strip() == "commit"
    subject = _sh("git", "log", "-1", "--format=%s", rec["sha"], cwd=origin).stdout.strip()
    assert subject == "chore(release): bump version 1.0.0 -> 1.0.1 (ship auto-bump for #1)", subject

def test_audit_line_survives_a_head_poll_timeout_after_a_successful_push(tmp_path):
    """Review finding, #518: the audit line used to be written only after the head-oid wait
    succeeded. If the Contents PUT itself succeeds (a real, durable remote commit) but the
    wait that follows times out -- eventual-consistency lag, or another push racing past it --
    the commit must still be audited: it is durable on GitHub the moment the PUT returns."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_STALL_HEAD_AFTER_BUMP": "1"})

    assert r.returncode != 0, r.stdout
    assert "did not reflect it within" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout
    # The commit is real (the PUT succeeded) even though ship refused on the stalled wait.
    assert 'version = "1.0.1"' in _origin_file(origin, "feat", "pyproject.toml")
    lines = [json.loads(l) for l in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    bump = [l for l in lines if l.get("decision") == "version-bump:auto"]
    assert len(bump) == 1, "the audit line must be written even though the wait timed out"
    assert bump[0]["old"] == "1.0.0" and bump[0]["new"] == "1.0.1"
    assert len(bump[0]["sha"]) == 40

def test_diverged_local_branch_refuses_before_anything_is_pushed(tmp_path):
    """The branch-sanity check runs BEFORE the bump: a local commit that was never pushed must
    refuse the ship with nothing written to the remote (ship would otherwise diverge it further)."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "src/b.py", "b = 1\n", "feat: unpushed")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "unpushed commit(s)" in r.stderr, r.stderr
    assert "PUT" not in _gh_log(state), _gh_log(state)

def test_package_json_is_bumped_too(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _commit_file(main, "package.json", '{\n  "name": "mytool",\n  "version": "2.3.9"\n}\n', "chore: package.json")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.js", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "auto-bump: package.json 2.3.9 -> 2.3.10 committed as" in r.stdout, r.stdout
    assert '"version": "2.3.10"' in _origin_file(origin, "main", "package.json")
    assert _origin_file(origin, "main", "package.json").endswith("}\n"), "byte layout preserved"

# ---------------------------------------------------------------------------------------
# Generic BEHIND-clearing (#518): mergeStateStatus=BEHIND fires whenever ANY prior merge
# moved base ahead under a require-up-to-date-branches ruleset, independent of whether the
# version file itself diverged — the acceptance criterion's own headline scenario ("two PRs
# shipped back to back both merge without a conflict") would otherwise still need a human
# `gh pr update-branch` the instant the first of the two merges, on such a repo.
# ---------------------------------------------------------------------------------------

def test_behind_pr_is_updated_and_merges_without_a_manual_rebase(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    # main must genuinely advance past feat's merge-base -- otherwise update-branch's merge is
    # a no-op ("already up to date", no new commit) and the head-oid wait below never sees a
    # change, regardless of what mergeStateStatus the fake claims.
    _commit_file(main, "src/other.py", "o = 1\n", "feat: unrelated change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={
        "SHIP_TEST_MERGE_STATE": "BEHIND", "SHIP_TEST_MERGE_STATE_AFTER": "CLEAN",
    })

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "PR #1 is BEHIND its base — attempting to update it via the GitHub API" in r.stderr, r.stderr
    assert "no longer BEHIND" in r.stderr, r.stderr
    assert "PUT update-branch" in _gh_log(state)
    assert "[fake gh] merged" in r.stdout
    assert "head is BEHIND its base. Update it" not in r.stderr

def test_behind_pr_with_a_real_conflict_still_refuses(tmp_path):
    """The update-branch attempt can genuinely conflict (base and the branch both edited the
    same file differently) -- that still refuses with the original guidance, nothing merged."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "src/a.py", "x = 2\n", "feat: change a.py differently")
    _git("push", "-q", "-f", "origin", "feat", cwd=wt)
    _commit_file(main, "src/a.py", "x = 3\n", "chore: conflicting change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_MERGE_STATE": "BEHIND"})

    assert r.returncode != 0, r.stdout
    assert "attempting to update it via the GitHub API" in r.stderr, r.stderr
    assert "falling through to the BEHIND refusal" in r.stderr, r.stderr
    assert "head is BEHIND its base. Update it (gh pr update-branch" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout

def test_behind_with_unpushed_local_commit_refuses_before_any_remote_write(tmp_path):
    """Local sanity runs BEFORE the BEHIND-clearing remote write (review finding, #518): an
    unpushed local commit must refuse with the existing 'push first' guidance, and nothing on
    the remote branch may be touched -- writing there first would leave local and remote
    diverged in a NEW way that guidance doesn't cover."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(wt, "src/b.py", "b = 1\n", "feat: unpushed")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_MERGE_STATE": "BEHIND"})

    assert r.returncode != 0, r.stdout
    assert "unpushed commit(s)" in r.stderr, r.stderr
    assert "attempting to update it via the GitHub API" not in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state), _gh_log(state)

def test_behind_docs_only_pr_merges_without_a_spurious_dwell_refusal(tmp_path):
    """The standalone BEHIND-clearing merge must not itself look like a fresh human push to the
    review-dwell gate when NO bump follows it -- the common case: docs-only, test-only, a
    hand-bumped minor/major, no version file, an opted-out repo (review finding, #518: the
    original fix only recognized ship's own commit via the BUMP commit's identity, so a
    standalone merge with nothing behind it reset the dwell clock to 'now' and refused)."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "README.md", "docs change\n")
    _commit_file(main, "src/other.py", "o = 1\n", "feat: unrelated change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={
        "SHIP_TEST_MERGE_STATE": "BEHIND", "SHIP_REVIEW_DWELL": "600",
    })

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "no longer BEHIND" in r.stderr, r.stderr
    assert "review-dwell gate OK" in r.stdout, r.stdout
    assert "review-dwell window is 600s" not in r.stderr, r.stderr
    assert "[fake gh] merged" in r.stdout
    assert "PUT contents" not in _gh_log(state), "docs-only PR: no bump should have been made"

def test_behind_docs_only_pr_widens_ci_grace_too(tmp_path):
    """Same standalone-merge scenario as above, from the green-CI gate's side: the
    registration-grace widening used to key on AUTO_BUMP_DONE alone, which a standalone
    BEHIND-clearing merge never sets."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "README.md", "docs change\n")
    _commit_file(main, "src/other.py", "o = 1\n", "feat: unrelated change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_MERGE_STATE": "BEHIND"})

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "waiting on the checks of ship's own push" in r.stdout, r.stdout

def test_ship_config_typo_does_not_silently_re_enable_auto_bump(tmp_path):
    """Review finding, #518: rejecting a malformed SHIP_LOCAL_TEST_DIR used to invalidate the
    WHOLE .ship-config file, including an unrelated, otherwise-valid SHIP_AUTO_BUMP=0 -- an
    operator's explicit opt-out of ship's remote writes must survive a typo in a DIFFERENT key."""
    main, origin, state, bindir = _make_world(tmp_path)
    _commit_file(main, ".ship-config", "SHIP_LOCAL_TEST_DIR=./\nSHIP_AUTO_BUMP=0\n", "chore: ship-config")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "ignoring SHIP_LOCAL_TEST_DIR/SHIP_LOCAL_TEST_CMD" in r.stderr, r.stderr
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state), "SHIP_AUTO_BUMP=0 must still apply"

def test_behind_mergestate_reread_failure_does_not_clobber_the_refusal(tmp_path):
    """Review finding, #518: reading mergeStateStatus back into a bare global with `|| true`
    turned a FAILED re-read into an empty string, which then read as 'not BEHIND anymore' and
    let ship proceed on an UNKNOWN state. A failed re-read must leave the original BEHIND
    refusal exactly as if the update attempt was never made."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "src/other.py", "o = 1\n", "feat: unrelated change on main")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat", env_extra={
        "SHIP_TEST_MERGE_STATE": "BEHIND", "SHIP_TEST_MERGE_STATE_REREAD_FAIL": "1",
    })

    assert r.returncode != 0, r.stdout
    assert "could not re-read its merge state" in r.stderr, r.stderr
    assert "head is BEHIND its base. Update it (gh pr update-branch" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout

def test_leading_zero_base_version_refuses_before_any_remote_write(tmp_path):
    """Review finding, #518: a leading-zero component (`1.0.08`, CalVer-style `2026.08.09`) was
    accepted by _semver_parts's shape check, so the pre-write guard waved it through -- only for
    _semver_next_patch's bash `$(( ))` arithmetic to choke on it as an invalid octal literal one
    step later, AFTER a real update-branch write had already landed. _semver_parts must reject
    a leading-zero component itself, before any remote write is attempted."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.08"), "chore(release): calver-ish")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "is not a plain X.Y.Z — bump it by hand" in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state), _gh_log(state)
    assert "PUT contents" not in _gh_log(state)

def test_non_semver_base_version_refuses_before_any_remote_write(tmp_path):
    """Review finding, #518: when base's version has moved to something non-X.Y.Z, ship used to
    push the update-branch merge FIRST and only discover it couldn't finish the bump afterward
    -- a real remote write for nothing. The semver check must happen before that write."""
    main, origin, state, bindir = _make_world(tmp_path)
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.0rc1"), "chore(release): rc")
    _git("push", "-q", "origin", "main", cwd=main)

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "is not a plain X.Y.Z — bump it by hand" in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state), _gh_log(state)
    assert "PUT contents" not in _gh_log(state)

def test_behind_resolution_skipped_when_auto_bump_opted_out(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={
        "SHIP_TEST_MERGE_STATE": "BEHIND", "SHIP_AUTO_BUMP": "0",
    })

    assert r.returncode != 0, r.stdout
    assert "attempting to update it via the GitHub API" not in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state)
    assert "head is BEHIND its base. Update it" in r.stderr, r.stderr

def test_behind_resolution_skipped_under_dry_run(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", "--dry-run", env_extra={"SHIP_TEST_MERGE_STATE": "BEHIND"})

    assert r.returncode != 0, r.stdout
    assert "attempting to update it via the GitHub API" not in r.stderr, r.stderr
    assert "PUT update-branch" not in _gh_log(state)
    assert "head is BEHIND its base. Update it" in r.stderr, r.stderr

# P1 (_record_ship_oid never returns 1 on empty/non-hex): covered by the helper's
# `case ... return 0` — a call-count-pinned scenario is not a stable contract.

def test_fork_pr_is_never_written_to(tmp_path):
    """A fork PR's head lives in the fork: ship must not (and could not) commit to it."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_TEST_CROSS_REPO": "true"})

    assert r.returncode != 0, r.stdout
    assert "PR head lives in a fork — ship cannot commit to it" in r.stderr, r.stderr
    assert "PUT" not in _gh_log(state), _gh_log(state)

def test_non_semver_version_is_left_to_a_human(tmp_path):
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _commit_file(main, "pyproject.toml", _PYPROJECT.replace("1.0.0", "1.0.0rc1"), "chore: rc")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "is not a plain X.Y.Z — bump it by hand" in r.stderr, r.stderr
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "PUT contents" not in _gh_log(state)

# ---------------------------------------------------------------------------------------
# P1s folded from #536: [project]/top-level version only; provenance by committer email
# + recorded oids (never a forgeable headline); bump-line threads keep severity/outdated.
# ---------------------------------------------------------------------------------------

_PYPROJECT_WITH_TOOL_TABLE = (
    "[tool.poetry]\nversion = \"9.9.9\"\n\n"
    "[project]\nname = \"mytool\"\nversion = \"1.0.0\"\n"
)


def test_tool_table_version_is_not_bumped(tmp_path):
    """P1-2: version parse/rewrite ONLY the [project] table, never [tool.*] version."""
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    _commit_file(main, "pyproject.toml", _PYPROJECT_WITH_TOOL_TABLE, "chore: project+tool versions")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    body = _origin_file(origin, "main", "pyproject.toml")
    assert 'version = "1.0.1"' in body, body
    assert 'version = "9.9.9"' in body, body
    assert 'version = "9.9.10"' not in body, body
    assert "auto-bump: pyproject.toml 1.0.0 -> 1.0.1" in r.stdout, r.stdout


def test_nested_package_json_version_is_not_bumped(tmp_path):
    """P1-2: package.json bump is the top-level version only, not a nested version key."""
    main, origin, state, bindir = _make_world(tmp_path, with_version_file=False)
    pkg = (
        "{\n"
        '  "name": "mytool",\n'
        '  "version": "2.3.9",\n'
        '  "dependencies": {\n'
        '    "foo": {\n'
        '      "version": "1.2.3"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    _commit_file(main, "package.json", pkg, "chore: package.json with nested version")
    _git("push", "-q", "origin", "main", cwd=main)
    _make_pr_branch(main, "feat", "src/a.js", "x = 1\n")

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    body = _origin_file(origin, "main", "package.json")
    assert '"version": "2.3.10"' in body, body
    assert '"version": "1.2.3"' in body, body
    assert '"version": "1.2.4"' not in body, body


def test_human_commit_forging_auto_bump_headline_still_counts_for_dwell(tmp_path):
    """Provenance is committer email + recorded oids, NEVER a forgeable headline substring.
    A fresh human commit that copies ship's auto-bump subject must still reset dwell."""
    import datetime as _dt
    main, origin, state, bindir = _make_world(tmp_path)
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wt = _make_pr_branch(main, "feat", "src/a.py", "x = 1\n", date=now)
    _commit_file(
        wt, "src/b.py", "b = 1\n",
        "chore(release): bump version 1.0.0 -> 1.0.1 (ship auto-bump for #1)",
        date=now,
    )
    _git("push", "-q", "origin", "feat", cwd=wt)

    r = _run_ship(main, bindir, state, "feat", env_extra={"SHIP_REVIEW_DWELL": "600"})

    assert r.returncode != 0, r.stdout
    assert "review-dwell window is 600s" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_p1_bot_thread_on_the_bump_line_still_blocks(tmp_path):
    """Bump-line auto-resolve keeps the high-severity filter: a bot P1/security finding
    on the version line must NOT be closed and must still refuse the ship."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(
        state,
        _thread(
            "T_P1", path="pyproject.toml", line=_VERSION_LINE,
            login="chatgpt-codex-connector",
            body="P1: this must be a MAJOR bump, do not merge as patch",
        ),
    )

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "resolve T_P1" not in _gh_log(state)
    assert "1 unresolved review thread(s)" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_non_outdated_bot_thread_on_the_bump_line_still_blocks(tmp_path):
    """Bump-line auto-resolve keeps the isOutdated filter: a still-current bot nit on the
    version line is not closed (same as --resolve-addressed-threads)."""
    main, origin, state, bindir = _make_world(tmp_path)
    _make_pr_branch(main, "feat", "src/a.py", "x = 1\n")
    _write_threads(
        state,
        _thread(
            "T_CURRENT", path="pyproject.toml", line=_VERSION_LINE,
            login="chatgpt-codex-connector", outdated=False,
        ),
    )

    r = _run_ship(main, bindir, state, "feat")

    assert r.returncode != 0, r.stdout
    assert "resolve T_CURRENT" not in _gh_log(state)
    assert "1 unresolved review thread(s)" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout
