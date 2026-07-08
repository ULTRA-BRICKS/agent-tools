"""Tests for git hook preference of rig.yaml `scripts.test` via `dev run test`.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_git_hooks_dev_scripts.py -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PRE_COMMIT = _ROOT / "git-hooks" / "pre-commit"
_PRE_PUSH = _ROOT / "git-hooks" / "pre-push"


@pytest.fixture
def repo_with_rig_test_script(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git required")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "package.json").write_text('{"scripts":{"test":"npm-should-not-run"}}\n', encoding="utf-8")
    (repo / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    return repo


@pytest.fixture
def repo_with_only_rig_test_script(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git required")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text("scripts:\n  test: echo from rig\n", encoding="utf-8")
    return repo


@pytest.fixture
def repo_with_flow_style_rig_test_script(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git required")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "package.json").write_text('{"scripts":{"test":"npm-should-not-run"}}\n', encoding="utf-8")
    (repo / "rig.yaml").write_text("scripts: {test: echo from rig}\n", encoding="utf-8")
    return repo


@pytest.fixture
def fake_dev_and_npm(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dev_log = tmp_path / "dev.log"
    npm_log = tmp_path / "npm.log"

    dev = bindir / "dev"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = 'has-script' ] && [ \"$2\" = 'test' ]; then exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$DEV_HOOK_TEST_DEV_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    dev.chmod(0o755)

    npm = bindir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$DEV_HOOK_TEST_NPM_LOG\"\n"
        "[ \"$*\" = 'run test' ] && exit 99\n"
        "exit 0\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)
    return bindir, dev_log, npm_log


def _hook_env(bindir: Path, dev_log: Path, npm_log: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["DEV_HOOK_TEST_DEV_LOG"] = str(dev_log)
    env["DEV_HOOK_TEST_NPM_LOG"] = str(npm_log)
    return env


def _read_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_pre_push_prefers_dev_run_test(repo_with_rig_test_script, fake_dev_and_npm):
    bindir, dev_log, npm_log = fake_dev_and_npm

    result = subprocess.run(
        ["sh", str(_PRE_PUSH)],
        cwd=repo_with_rig_test_script,
        env=_hook_env(bindir, dev_log, npm_log),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dev_log.read_text(encoding="utf-8").strip() == "run test"
    assert "run test" not in _read_if_exists(npm_log)


def test_pre_commit_prefers_dev_run_test(repo_with_rig_test_script, fake_dev_and_npm):
    bindir, dev_log, npm_log = fake_dev_and_npm

    result = subprocess.run(
        ["sh", str(_PRE_COMMIT)],
        cwd=repo_with_rig_test_script,
        env=_hook_env(bindir, dev_log, npm_log),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dev_log.read_text(encoding="utf-8").strip() == "run test"
    assert "run test" not in _read_if_exists(npm_log)


def test_pre_push_uses_dev_has_script_not_shell_yaml_parser(
    repo_with_flow_style_rig_test_script, fake_dev_and_npm
):
    bindir, dev_log, npm_log = fake_dev_and_npm

    result = subprocess.run(
        ["sh", str(_PRE_PUSH)],
        cwd=repo_with_flow_style_rig_test_script,
        env=_hook_env(bindir, dev_log, npm_log),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dev_log.read_text(encoding="utf-8").strip() == "run test"
    assert "run test" not in _read_if_exists(npm_log)


def test_pre_commit_uses_dev_run_test_without_detected_toolchain(
    repo_with_only_rig_test_script, fake_dev_and_npm
):
    bindir, dev_log, npm_log = fake_dev_and_npm

    result = subprocess.run(
        ["sh", str(_PRE_COMMIT)],
        cwd=repo_with_only_rig_test_script,
        env=_hook_env(bindir, dev_log, npm_log),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dev_log.read_text(encoding="utf-8").strip() == "run test"
    assert _read_if_exists(npm_log) == ""
