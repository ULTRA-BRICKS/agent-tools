"""Tests for `server-run` (agent-tools#1048): the one-liner that runs a heavy command on the
owner's server. The server is faked: a `ssh` shim on PATH drops the options and the host and
runs the rest locally with HOME pointed at a temp dir, so rsync, the snapshot commit, the slot
lock, `nice`/`timeout` and the exit-code passthrough all run for real.

    uv run --with pytest python -m pytest tests/test_server_run.py -q
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "universal" / "server-offload" / "server-run"
_MISSING = [t for t in ("rsync", "git", "flock", "bash") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_MISSING), reason=f"server-run tests need {_MISSING}")

_FAKE_SSH = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in -o|-l) shift 2;; -*) shift;; *) shift; break;; esac
done
cd "$HOME" && exec bash -c "$*"
"""


@pytest.fixture
def world(tmp_path: Path) -> dict:
    home = tmp_path / "server-home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(_FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=git_env)
    (repo / ".gitignore").write_text("ignored.txt\nnode_modules/\n")
    (repo / "tracked.txt").write_text("tracked\n")
    (repo / "sub" / "inner.txt").write_text("inner\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=git_env)
    (repo / "untracked.txt").write_text("untracked\n")
    (repo / "ignored.txt").write_text("ignored\n")
    env = {**git_env, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", "SERVER_RUN_HOST": "fake@host"}
    return {"home": home, "repo": repo, "env": env}


def run(world: dict, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_SCRIPT), *args], cwd=cwd or world["repo"], env=world["env"], capture_output=True, text=True, check=False
    )


def remote_dir(world: dict) -> Path:
    (found,) = (world["home"] / "work" / "sr").glob("repo-*")
    return found


def test_runs_command_in_synced_checkout_and_returns_its_exit_code(world):
    out = run(world, "--", "bash", "-c", "pwd; ls; exit 7")
    assert out.returncode == 7
    assert str(remote_dir(world)) in out.stdout
    names = out.stdout.split()
    assert "tracked.txt" in names and "untracked.txt" in names
    assert "ignored.txt" not in names


def test_runs_from_the_same_subdirectory(world):
    out = run(world, "--", "pwd", cwd=world["repo"] / "sub")
    assert out.returncode == 0
    assert out.stdout.strip() == str(remote_dir(world) / "sub")


def test_deleted_local_file_is_deleted_remotely_but_node_modules_survive(world):
    assert run(world, "--", "true").returncode == 0
    nm = remote_dir(world) / "node_modules"
    nm.mkdir()
    (nm / "keep").write_text("x")
    (world["repo"] / "tracked.txt").unlink()
    subprocess.run(["git", "rm", "-q", "--cached", "tracked.txt"], cwd=world["repo"], check=True, env=world["env"])
    assert run(world, "--", "true").returncode == 0
    assert not (remote_dir(world) / "tracked.txt").exists()
    assert (nm / "keep").exists()


def test_remote_checkout_gets_a_git_snapshot(world):
    out = run(world, "--", "git", "ls-files")
    assert out.returncode == 0
    assert "tracked.txt" in out.stdout.split()


def test_fetch_copies_a_remote_path_back(world):
    out = run(world, "--fetch", "results", "--", "bash", "-c", "mkdir -p results && echo ok > results/r.txt")
    assert out.returncode == 0
    assert (world["repo"] / "results" / "r.txt").read_text() == "ok\n"


def test_timeout_exits_124(world):
    out = run(world, "--timeout", "1", "--", "sleep", "5")
    assert out.returncode == 124


def test_no_free_slot_exits_22(world):
    assert run(world, "--", "true").returncode == 0
    slots = world["home"] / "work" / "sr" / ".slots"
    holders = []
    for s in (1, 2, 3):
        holders.append(subprocess.Popen(["flock", str(slots / str(s)), "sleep", "20"]))
    try:
        out = run(world, "--queue-timeout", "1", "--", "true")
        assert out.returncode == 22
        assert "no free slot" in out.stderr
    finally:
        for h in holders:
            h.kill()


def test_dry_run_touches_nothing(world):
    out = run(world, "--dry-run", "--", "bun", "test")
    assert out.returncode == 0
    assert "bun test" in out.stdout
    assert not (world["home"] / "work").exists()


def test_no_command_is_a_usage_error(world):
    assert run(world).returncode == 2


def test_outside_git_is_refused(world, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = run(world, "--", "true", cwd=plain)
    assert out.returncode != 0
    assert "not inside a git checkout" in out.stderr


def test_clean_this_removes_only_this_checkout(world):
    assert run(world, "--", "true").returncode == 0
    other = world["home"] / "work" / "sr" / "other-12345678"
    other.mkdir()
    out = run(world, "--clean", "--this")
    assert out.returncode == 0
    assert other.exists()
    assert not list((world["home"] / "work" / "sr").glob("repo-*"))


def test_clean_removes_only_idle_checkouts(world):
    assert run(world, "--", "true").returncode == 0
    old = world["home"] / "work" / "sr" / "old-12345678"
    old.mkdir()
    os.utime(old, (1_000_000_000, 1_000_000_000))
    out = run(world, "--clean", "--older-than-days", "3")
    assert out.returncode == 0
    assert not old.exists()
    assert remote_dir(world).exists()


def test_dangling_symlink_is_synced(world):
    (world["repo"] / "dangling").symlink_to("nowhere")
    out = run(world, "--", "bash", "-c", "test -L dangling && echo has-link")
    assert out.returncode == 0
    assert "has-link" in out.stdout


def test_subdirectory_without_synced_files_still_runs(world):
    empty = world["repo"] / "emptydir"
    empty.mkdir()
    out = run(world, "--", "pwd", cwd=empty)
    assert out.returncode == 0
    assert out.stdout.strip().endswith("/emptydir")


@pytest.mark.parametrize("args", [["--this", "--", "true"], ["--timeout", "0", "--", "true"]])
def test_bad_options_are_usage_errors(world, args):
    assert run(world, *args).returncode == 2


def test_fetch_copies_a_single_file(world):
    out = run(world, "--fetch", "sub/report.txt", "--", "bash", "-c", "echo one > sub/report.txt")
    assert out.returncode == 0
    assert (world["repo"] / "sub" / "report.txt").read_text() == "one\n"


@pytest.mark.parametrize("rel", ["../outside", "/etc/passwd"])
def test_fetch_outside_the_repo_is_refused(world, rel):
    assert run(world, "--fetch", rel, "--", "true").returncode == 2


def test_second_run_of_one_checkout_waits_for_the_first(world, tmp_path):
    env = {**world["env"], "XDG_CACHE_HOME": str(tmp_path / "cache")}
    first = subprocess.Popen(
        [str(_SCRIPT), "--", "bash", "-c", "echo first-start; sleep 2; echo first-end"],
        cwd=world["repo"], env=env, stdout=subprocess.PIPE, text=True,
    )
    assert first.stdout.readline().strip() == "first-start"
    second = subprocess.run(
        [str(_SCRIPT), "--", "echo", "second"], cwd=world["repo"], env=env, capture_output=True, text=True, check=False
    )
    rest = first.stdout.read()
    first.wait()
    assert "first-end" in rest
    assert second.stdout.strip() == "second"
    assert first.returncode == 0 and second.returncode == 0
