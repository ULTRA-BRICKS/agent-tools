"""gh ship on a PR whose diff GitHub refuses to serve (agent-tools#1023).

GitHub answers `gh pr diff` — including `--name-only` — with HTTP 406 "diff exceeded the maximum
number of lines (20000)" for a big PR, so the version-bump gate and the screenshot gate could not
evaluate it at all (ExpenseSyncBot PR 217: 226 files). ship now falls back to the paginated
pull-request files API for the file list, and to the version file's contents at the PR head and
merge-base for the version value (the files API drops `patch` for most files of a huge PR).

Every test runs the real `ci/ship/ship.sh` end to end in a temp git repo against a fake `gh` that
answers 406 to every `pr diff` and serves the files API / compare / contents calls from a JSON
state file (the `--jq` expressions are run through real `jq`, so they are exercised too). No gate
override of the gates under test is used anywhere in this file (the SHIP_* variables set below only
switch off unrelated gates, as tests/test_ship.py does).

Requires bash + git + jq. Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ship_large_pr.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SHIP = Path(__file__).resolve().parents[1] / "ci" / "ship" / "ship.sh"
sys.path.insert(0, str(_SHIP.parent))

from shiplib.ctx import Ctx, Result  # noqa: E402
from shiplib.pr_files import pr_changed_files, version_file_diff  # noqa: E402

os.environ.setdefault("SHIP_TEST_DISABLE_REVIEW_QUORUM", "1")
os.environ.setdefault("SHIP_ACCEPTANCE_GATE", "0")
os.environ.setdefault("SHIP_MAGIC_CLOSE_GATE", "0")
os.environ["SHIP_TASK_NOTIFY_ENABLED"] = "0"

HEAD = "a" * 40
MERGE_BASE = "b" * 40
TOO_LARGE = (
    "could not find pull request diff: HTTP 406: Sorry, the diff exceeded the maximum number of "
    "lines (20000) (https://api.github.com/repos/o/r/pulls/1)\nPullRequest.diff too_large"
)

_FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys

state_path = os.environ["SHIP_TEST_STATE"]
with open(state_path, encoding="utf-8") as fh:
    st = json.load(fh)
args = sys.argv[1:]
joined = " ".join(args)
with open(os.environ["SHIP_TEST_CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(joined + "\n")


def jq(expr, payload):
    return subprocess.run(["jq", "-r", expr], input=json.dumps(payload), capture_output=True, text=True, check=True).stdout


def flag(name):
    return args[args.index(name) + 1] if name in args else None


def bump(key):
    st[key] = st.get(key, 0) + 1
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    return st[key]


sub = args[0] if args else ""
if sub == "pr":
    action = args[1]
    if action == "view":
        if "--json reviews" in joined:
            print(1)
        elif "headRefOid,changedFiles,baseRefName" in joined:
            n = bump("meta_calls")
            head = st["head_after"] if n >= st.get("head_after_call", 2) and st.get("head_after") else st["head"]
            print(json.dumps({"headRefOid": head, "changedFiles": st["changed_files"], "baseRefName": "main"}))
        elif "headRefName" in joined:
            print("%s\tOPEN\tMERGEABLE\tfalse\tCLEAN" % st["branch"])
        elif "--json baseRefName" in joined:
            print("main")
        elif "--json state,headRefOid" in joined:
            print("MERGED\t" + st["head"])
        elif "--json headRefOid" in joined:
            print(st["head"])
        elif "statusCheckRollup" in joined:
            print('[{"__typename":"CheckRun","name":"ci","status":"COMPLETED","conclusion":"SUCCESS","workflowName":"CI"}]')
        elif "body,comments" in joined:
            print(st.get("body", ""))
        else:
            print("[]")
    elif action == "diff":
        if "--name-only" in args and st.get("name_only"):
            print("\n".join(st["name_only"]))
            sys.exit(0)
        sys.stderr.write(__TOO_LARGE__ + "\n")
        sys.exit(1)
    elif action == "merge":
        print("[fake gh] merged")
elif sub == "api":
    path = next((a for a in args[1:] if a.startswith("repos/")), "")
    expr = flag("--jq")
    if "committedDate" in joined:
        print("2020-01-01T00:00:00Z\t\t2020-01-01T00:00:00Z")
    elif "/pulls/1/files" in path and "--paginate" in args:
        if st.get("files_api_fail"):
            sys.stderr.write("gh: files API unavailable (HTTP 502)\n")
            sys.exit(1)
        for page in st["pages"]:
            sys.stdout.write(jq(expr, page))
    elif "/compare/" in path:
        if st.get("compare_fail"):
            sys.stderr.write("gh: compare unavailable (HTTP 500)\n")
            sys.exit(1)
        sys.stdout.write(jq(expr, {"merge_base_commit": {"sha": st["merge_base"]}}))
    elif "/contents/" in path:
        file_path, _, ref = path.split("/contents/", 1)[1].partition("?ref=")
        if st.get("contents_error"):
            sys.stderr.write("gh: contents API unavailable (HTTP 500)\n")
            sys.exit(1)
        key = ref + ":" + file_path
        if key in st["contents"]:
            sys.stdout.write(st["contents"][key])
        else:
            sys.stdout.write('{"message":"Not Found","status":"404"}')
            sys.stderr.write("gh: Not Found (HTTP 404)\n")
            sys.exit(1)
    else:
        print(0)
'''.replace("__TOO_LARGE__", repr(TOO_LARGE))


def _sh(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def _git(*args, cwd):
    r = _sh("git", "-c", "core.hooksPath=", *args, cwd=cwd, env=dict(os.environ))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture
def repo(tmp_path):
    """A repo on `main` carrying a pyproject.toml at version 0.4.1, branch `feat` checked out in a
    worktree, and a bare origin."""
    for tool in ("bash", "git", "jq"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} required")
    origin = tmp_path / "origin.git"
    _sh("git", "init", "--bare", "-b", "main", "-q", str(origin), cwd=tmp_path)
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    _git("remote", "add", "origin", str(origin), cwd=main)
    (main / "README.md").write_text("# x\n", encoding="utf-8")
    (main / "pyproject.toml").write_text('[project]\nname = "mytool"\nversion = "0.4.1"\n', encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("push", "-q", "origin", "main", cwd=main)
    _git("branch", "feat", cwd=main)
    _git("push", "-q", "origin", "feat", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "wt-feat"), "feat", cwd=main)
    return main


def _pyproject(version: str) -> str:
    return f'[project]\nname = "mytool"\nversion = "{version}"\n'


def _pages(names, per_page=100, entries_by_name=None):
    """The files API as GitHub serves it for a huge PR: names only, no `patch`."""
    entries_by_name = entries_by_name or {}
    entries = [entries_by_name.get(n, {"filename": n, "status": "modified"}) for n in names]
    return [entries[i:i + per_page] for i in range(0, len(entries), per_page)]


def _ship(repo, tmp_path, *, names, changed_files=None, head_version="0.4.2", merge_base_version="0.4.1",
          entries_by_name=None, **over):
    """Run ship against a PR whose every `pr diff` is HTTP 406. Returns (result, gh call log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    state = {
        "branch": "feat",
        "head": HEAD,
        "merge_base": MERGE_BASE,
        "changed_files": len(names) if changed_files is None else changed_files,
        "pages": _pages(names, entries_by_name=entries_by_name),
        "contents": {f"{HEAD}:pyproject.toml": _pyproject(head_version)},
    }
    if merge_base_version is not None:
        state["contents"][f"{MERGE_BASE}:pyproject.toml"] = _pyproject(merge_base_version)
    state.update(over)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    call_log = tmp_path / "gh-calls.log"
    env = dict(os.environ)
    env.update({
        "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
        "SHIP_TEST_STATE": str(state_path),
        "SHIP_TEST_CALL_LOG": str(call_log),
        "SHIP_DEFAULT_BRANCH": "main",
        "SHIP_MAIN_CHECKOUT": str(repo),
    })
    r = _sh("bash", str(_SHIP), "1", cwd=repo, env=env)
    return r, call_log.read_text(encoding="utf-8") if call_log.exists() else ""


def _many_sources(count):
    return ["src/mod_%03d.py" % i for i in range(count - 1)] + ["pyproject.toml"]


def test_huge_pr_with_version_bump_ships_across_files_api_pages(repo, tmp_path):
    names = _many_sources(226)  # three pages of 100/100/26, exactly like ExpenseSyncBot PR 217
    r, calls = _ship(repo, tmp_path, names=names)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout
    assert "pulls/1/files" in calls and "per_page=100" in calls, calls
    assert f"ref={HEAD}" in calls and f"ref={MERGE_BASE}" in calls, calls


def test_huge_pr_without_version_bump_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(226), head_version="0.4.1")

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_huge_pr_whose_version_the_base_tip_already_has_is_refused(repo, tmp_path):
    (repo / "pyproject.toml").write_text(_pyproject("0.4.2"), encoding="utf-8")
    _git("commit", "-qam", "release 0.4.2 (sibling PR)", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    r, _calls = _ship(repo, tmp_path, names=_many_sources(226))

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "base branch tip ALREADY has version 0.4.2" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_version_file_added_by_the_pr_counts_as_bumped(repo, tmp_path):
    """No version file at the merge-base (HTTP 404) for a file the PR adds is an empty old side."""
    added = {"pyproject.toml": {"filename": "pyproject.toml", "status": "added"}}
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), merge_base_version=None, entries_by_name=added)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout


def _renamed_from_old_path():
    return {"pyproject.toml": {"filename": "pyproject.toml", "status": "renamed", "previous_filename": "old/pyproject.toml"}}


def test_renamed_version_file_is_compared_with_its_previous_path(repo, tmp_path):
    state = {"contents": {f"{HEAD}:pyproject.toml": _pyproject("0.4.2"), f"{MERGE_BASE}:old/pyproject.toml": _pyproject("0.4.1")}}
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), entries_by_name=_renamed_from_old_path(), **state)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout


def test_renamed_version_file_with_an_unchanged_version_is_refused(repo, tmp_path):
    """A move is not a release: reading the missing old path as 'new file' would call 0.4.1 a bump."""
    state = {"contents": {f"{HEAD}:pyproject.toml": _pyproject("0.4.1"), f"{MERGE_BASE}:old/pyproject.toml": _pyproject("0.4.1")}}
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), entries_by_name=_renamed_from_old_path(), **state)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "version in pyproject.toml is UNCHANGED" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_version_file_missing_at_the_merge_base_without_a_matching_status_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), merge_base_version=None)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "missing at its merge-base" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_version_file_unreadable_at_the_pr_head_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), contents={f"{MERGE_BASE}:pyproject.toml": _pyproject("0.4.1")})

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not read pyproject.toml at the PR head" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_merge_base_lookup_failure_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), compare_fail=True)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not resolve the PR's merge-base" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_name_only_listing_with_a_refused_full_diff_is_pinned_to_the_head(repo, tmp_path):
    """`--name-only` still works (under 20000 name lines) while the full diff is 406: the version is
    read at a head the files were re-listed at, so the same PR passes only when the lists agree."""
    names = _many_sources(10)
    r, calls = _ship(repo, tmp_path, names=names, name_only=names)

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout
    assert "pulls/1/files" in calls, calls


def test_name_only_listing_that_disagrees_with_the_files_api_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), name_only=_many_sources(9)[:-1] + ["src/other.py", "pyproject.toml"])

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "files changed since they were listed" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_huge_docs_only_pr_needs_no_bump(repo, tmp_path):
    names = ["docs/page_%03d.md" % i for i in range(150)] + ["README.md"]
    r, _calls = _ship(repo, tmp_path, names=names, head_version="0.4.1")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "no shippable source" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_truncated_file_list_is_refused_not_passed(repo, tmp_path):
    """The PR has 226 files but the API pages only carry 150 of them: a partial list must never
    pass the gate, even though every listed file is docs (which would otherwise pass)."""
    names = ["docs/page_%03d.md" % i for i in range(150)]
    r, _calls = _ship(repo, tmp_path, names=names, changed_files=226)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not list changed files for PR #1" in r.stderr, r.stderr
    assert "226" in r.stderr and "150" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_pr_above_the_files_api_cap_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=["docs/a.md"], changed_files=3001)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "3000" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_files_api_failure_is_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), files_api_fail=True)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not list changed files for PR #1" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_head_moving_during_the_listing_is_refused(repo, tmp_path):
    # Docs only, so nothing after the listing could refuse for another reason.
    names = ["docs/page_%03d.md" % i for i in range(10)]
    r, _calls = _ship(repo, tmp_path, names=names, head_after="c" * 40)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "head moved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_head_moving_while_the_version_file_is_read_is_refused(repo, tmp_path):
    # Meta reads: 1-2 bracket the listing, 3-4 bracket the version-file read.
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), head_after="c" * 40, head_after_call=4)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "head moved" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_unreadable_version_file_contents_are_refused(repo, tmp_path):
    r, _calls = _ship(repo, tmp_path, names=_many_sources(10), contents_error=True)

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "could not read the diff for PR #1" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_huge_ui_pr_without_screenshot_is_refused(repo, tmp_path):
    names = ["src/components/Button.tsx", "pyproject.toml"] + ["src/mod_%03d.py" % i for i in range(120)]
    r, _calls = _ship(repo, tmp_path, names=names, body="refactor, no image")

    assert r.returncode != 0, f"{r.stdout}\n{r.stderr}"
    assert "touches UI but has NO embedded screenshot" in r.stderr, r.stderr
    assert "[fake gh] merged" not in r.stdout


def test_huge_ui_pr_with_embedded_screenshot_ships(repo, tmp_path):
    names = ["src/components/Button.tsx", "pyproject.toml"] + ["src/mod_%03d.py" % i for i in range(120)]
    r, _calls = _ship(repo, tmp_path, names=names, body="![ui](https://example.test/shot.png)")

    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "version-bump gate OK" in r.stdout, r.stdout
    assert "merged #1" in r.stdout, r.stdout


def test_file_list_is_fetched_once_for_both_gates(repo, tmp_path):
    _r, calls = _ship(repo, tmp_path, names=_many_sources(226))

    assert calls.count("pulls/1/files") == 1, calls


def test_forgejo_never_takes_the_github_fallback(tmp_path):
    """Forgejo serves `--name-only` and the full diff without a line limit and has no REST
    passthrough: a failing `pr diff` there stays a plain failure and never reaches `gh api`."""
    ctx = Ctx(str(_SHIP.parent), ["1"])
    ctx.pr = "1"
    ctx.provider_resolved = "forgejo"
    ctx.gh = lambda *args, **kw: Result(1)

    def no_subprocess(*args, **kw):
        raise AssertionError("the GitHub fallback ran on a Forgejo PR: %r" % (args,))

    ctx.cmd = no_subprocess

    files, why = pr_changed_files(ctx)
    diff, diff_why = version_file_diff(ctx, "pyproject.toml")

    assert files is None and "failed" in why, why
    assert diff is None and "GitHub-only" in diff_why, diff_why
