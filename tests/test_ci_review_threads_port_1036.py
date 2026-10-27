"""agent-tools#1036: review-threads.sh is a POSIX launcher for review_threads.py.

Pins the launcher contract (tiny, POSIX sh, clean under shellcheck/shfmt, fails clearly),
ruff-default cleanliness of the Python, and behavior the shell script had: the GraphQL
query and gh argv are passed literally, plus the branches of the port not covered in
tests/test_review_threads_gate.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SLOT = Path(__file__).resolve().parent.parent / "ci" / "review-threads"
SH = SLOT / "review-threads.sh"
PY = SLOT / "review_threads.py"

EXPECTED_QUERY = """query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$pr){
      reviewThreads(first:100,after:$endCursor){
        pageInfo{hasNextPage endCursor}
        nodes{isResolved isOutdated}
      }
    }
  }
}"""

AMBIENT = (
    "GITHUB_SERVER_URL",
    "GITHUB_API_URL",
    "GITHUB_REPOSITORY",
    "GH_REPO",
    "REVIEW_THREADS_PROVIDER",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "IGNORE_OUTDATED",
    "PR_NUMBER",
)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in AMBIENT}
    env.update(extra)
    return env


def _with_path(bin_dir: Path, **extra: str) -> dict[str, str]:
    return _env(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **extra)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def _run(argv: list[str], env: dict[str, str], cwd: Path | None = None):
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=30
    )


# --- launcher ---------------------------------------------------------------


def test_launcher_is_tiny_posix_sh_and_executable():
    text = SH.read_text()
    assert len(text.splitlines()) <= 40
    assert text.startswith("#!/bin/sh\n")
    assert os.access(SH, os.X_OK) and os.access(PY, os.X_OK)
    assert "BASH_SOURCE" not in text


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_launcher_runs_under_sh_and_bash_from_other_cwd(tmp_path: Path, shell: str):
    res = _run([shell, os.path.relpath(SH, tmp_path)], _env(), tmp_path)
    assert res.returncode == 2
    assert "Usage" in res.stderr


def test_launcher_runs_via_relative_exec_path(tmp_path: Path):
    res = _run([os.path.relpath(SH, tmp_path)], _env(), tmp_path)
    assert res.returncode == 2 and "Usage" in res.stderr


def test_launcher_passes_args_and_env_through(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "gh", "echo 0\n")
    res = _run(["sh", str(SH), "77"], _with_path(bin_dir))
    assert res.returncode == 0, res.stderr
    assert "PR #77 has no unresolved" in res.stdout
    res = _run(["sh", str(SH)], _with_path(bin_dir, PR_NUMBER="78"))
    assert "PR #78" in res.stdout


def test_launcher_fails_clearly_when_py_missing(tmp_path: Path):
    copy = tmp_path / "review-threads.sh"
    shutil.copy(SH, copy)
    res = _run(["sh", str(copy), "1"], dict(os.environ))
    assert res.returncode == 1
    assert "review_threads.py not found" in res.stderr


def test_launcher_fails_clearly_when_python3_missing(tmp_path: Path):
    shutil.copy(SH, tmp_path / "review-threads.sh")
    shutil.copy(PY, tmp_path / "review_threads.py")
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("dirname",):
        found = shutil.which(tool)
        assert found
        (tools / tool).symlink_to(found)
    res = _run(
        ["/bin/sh", str(tmp_path / "review-threads.sh"), "1"], {"PATH": str(tools)}
    )
    assert res.returncode == 1
    assert "python3 not found" in res.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="no shellcheck")
def test_launcher_is_shellcheck_clean():
    res = _run(["shellcheck", str(SH)], dict(os.environ))
    assert res.returncode == 0, res.stdout


@pytest.mark.skipif(shutil.which("shfmt") is None, reason="no shfmt")
def test_launcher_is_shfmt_clean():
    res = _run(["shfmt", "-i", "2", "-ci", "-bn", "-d", str(SH)], dict(os.environ))
    assert res.returncode == 0, res.stdout


@pytest.mark.skipif(shutil.which("ruff") is None, reason="no ruff")
def test_python_is_ruff_default_clean():
    for cmd in (["check"], ["format", "--check"]):
        res = _run(["ruff", *cmd, "--isolated", str(PY)], dict(os.environ))
        assert res.returncode == 0, res.stdout + res.stderr


# --- behavior ---------------------------------------------------------------


def test_graphql_query_and_argv_reach_gh_literally(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "gh.argv"
    _stub(bin_dir, "gh", f"printf '%s\\0' \"$@\" > {log}\necho 0\n")
    res = _run(
        ["bash", str(SH), "123"], _with_path(bin_dir, REVIEW_THREADS_PROVIDER="github")
    )
    assert res.returncode == 0, res.stderr
    assert "PASS: PR #123 has no unresolved review threads." in res.stdout
    argv = log.read_bytes().decode().split("\0")[:-1]
    jq = (
        "[.data.repository.pullRequest.reviewThreads.nodes[] "
        "| select(.isResolved|not)] | length"
    )
    assert argv == [
        "api", "graphql", "--paginate",
        "-F", "owner={owner}", "-F", "name={repo}", "-F", "pr=123",
        "-f", "query=" + EXPECTED_QUERY, "--jq", jq,
    ]  # fmt: skip


def test_ignore_outdated_changes_the_jq_filter(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "gh.argv"
    _stub(bin_dir, "gh", f"printf '%s\\0' \"$@\" > {log}\necho 0\n")
    _run(["bash", str(SH), "5"], _with_path(bin_dir, IGNORE_OUTDATED="1"))
    argv = log.read_bytes().decode().split("\0")[:-1]
    assert "(.isResolved|not) and (.isOutdated|not)" in argv[argv.index("--jq") + 1]


def test_missing_gh_exits_2(tmp_path: Path):
    py = shutil.which("python3")
    assert py
    res = _run([py, "-B", str(PY), "1"], {"PATH": str(tmp_path)})
    assert res.returncode == 2
    assert "gh CLI not found" in res.stderr


def test_unknown_provider_exits_2():
    res = _run(["sh", str(SH), "1"], _env(REVIEW_THREADS_PROVIDER="svn"))
    assert res.returncode == 2
    assert (
        "unknown REVIEW_THREADS_PROVIDER 'svn' (github | forgejo | auto)" in res.stderr
    )


def test_gh_failure_fails_closed_with_annotation(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "gh", "echo 0\nexit 4\n")
    res = _run(["sh", str(SH), "9"], _with_path(bin_dir))
    assert res.returncode == 1
    assert "::error::could not query review threads for PR #9" in res.stderr
    assert "FAIL: review-thread query failed for PR #9." in res.stderr
    assert "PASS" not in res.stdout


@pytest.mark.parametrize(
    "extra, expect",
    [
        ({}, "neither GITHUB_API_URL nor GITHUB_SERVER_URL is set"),
        (
            {"GITHUB_SERVER_URL": "http://127.0.0.1:1"},
            "no repository: set GH_REPO or GITHUB_REPOSITORY",
        ),
        (
            {"GITHUB_SERVER_URL": "http://127.0.0.1:1", "GH_REPO": "badrepo"},
            "repository must be owner/repo",
        ),
    ],
)
def test_forgejo_config_errors_fail_closed(extra: dict, expect: str):
    res = _run(["sh", str(SH), "3"], _env(REVIEW_THREADS_PROVIDER="forgejo", **extra))
    assert res.returncode == 1
    assert expect in res.stderr
    assert "(Forgejo API failed) — failing closed." in res.stderr


def test_forgejo_non_numeric_pr_and_outdated_note():
    env = _env(
        GITHUB_SERVER_URL="http://127.0.0.1:1",
        GITHUB_REPOSITORY="o/r",
        IGNORE_OUTDATED="1",
    )
    res = _run(["sh", str(SH), "x1"], env)
    assert res.returncode == 1
    assert "PR number must be numeric, got 'x1'" in res.stderr
    assert "note: IGNORE_OUTDATED=1 has no Forgejo equivalent" in res.stderr
