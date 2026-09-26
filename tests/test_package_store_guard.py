"""Tests for agent-hooks/package-store-guard — shared package stores and store-backed installs.

The cruxes (each red if the command were allowed):
- ``bun pm cache rm`` and ``rm -rf ~/.bun/install/cache`` wipe the bun global store every
  worktree's node_modules links into (2026-09-15 incident);
- ``ln -s <main>/node_modules node_modules`` lets ``bun install`` write through into the main
  checkout (hypercalendarbot, 2026-09-09);
- ``npm install`` in a bun project makes a full real copy of node_modules.
And the allow side: npm in an npm-only subproject, global installs, ordinary rm/ln.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "package-store-guard"
    / "package_store_guard.py"
)
_DESCRIPTOR = _HOOK.parent / "package-store-guard.pre-bash.json"

_spec = importlib.util.spec_from_file_location("package_store_guard", _HOOK)
assert _spec and _spec.loader
psg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(psg)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    (h / ".bun" / "install" / "cache" / "links").mkdir(parents=True)
    (h / ".cache" / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for var in ("BUN_INSTALL", "BUN_INSTALL_CACHE_DIR", "UV_CACHE_DIR", "XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def bun_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text("{}\n", encoding="utf-8")
    (repo / "bun.lock").write_text("{}\n", encoding="utf-8")
    return repo


def _run(command: str, monkeypatch, cwd: Path) -> tuple[dict, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"hook_api": "agents-hooks/v1", "point": "pre-bash", "args": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = psg.main()
    return json.loads(out.getvalue()), code


def _blocked(command: str, monkeypatch, cwd: Path) -> str:
    payload, code = _run(command, monkeypatch, cwd)
    assert code == psg.BLOCK_EXIT_CODE, (command, payload)
    assert payload["decision"] == "block"
    return payload["message"]


def _allowed(command: str, monkeypatch, cwd: Path) -> None:
    payload, code = _run(command, monkeypatch, cwd)
    assert code == 0, (command, payload)
    assert payload["decision"] == "allow"


# ── store wipes ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "bun pm cache rm",
        "bun pm cache rm --verbose",
        "cd /tmp && bun pm cache rm",
        "bash -c 'bun pm cache rm'",
        "rm -rf ~/.bun/install/cache",
        "rm -rf ~/.bun/install/cache/",
        "rm -rf ~/.bun/install/cache/links",
        "rm -rf ~/.bun/install/cache/*",
        "rm -rf ~/.bun",
        "rm -rf $HOME/.bun/install/cache",
        "rm -rf ${HOME}/.bun/install/cache",
        'rm -rf "$(bun pm cache)"',
        "rm -rf $(bun pm cache)",
        "sudo rm -rf ~/.bun/install/cache",
        "du -sh ~/.bun && rm -r -f ~/.bun/install/cache",
        "trash ~/.bun/install/cache",
        "mv ~/.bun/install/cache /tmp/old-bun-cache",
        "find ~/.bun/install/cache -type d -name 'links' -exec rm -rf {} +",
        "find ~/.bun/install/cache/links -mindepth 1 -delete",
        "ssh mac 'rm -rf ~/.bun/install/cache'",
    ],
)
def test_bun_store_wipes_block(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "shared package store" in msg
    assert "rig worktree gc" in msg
    assert "bun install --frozen-lockfile" in msg


def test_relative_rm_resolves_against_cd(home, monkeypatch, tmp_path):
    _blocked("cd ~/.bun/install && rm -rf cache", monkeypatch, tmp_path)
    _blocked("rm -rf install/cache", monkeypatch, home / ".bun")


def test_bun_install_cache_dir_env_is_protected(home, tmp_path, monkeypatch):
    custom = tmp_path / "custom-bun-cache"
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(custom))
    _blocked(f"rm -rf {custom}", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "uv cache clean",
        "uv cache clean requests",
        "uv --quiet cache clean",
        "uv --cache-dir /x cache clean",
        "uv cache prune --all",
        "uv cache prune --ci",
        "rm -rf ~/.cache/uv",
        "rm -rf ~/.cache/*",
        "rm -rf ~/.cache",
    ],
)
def test_uv_cache_wipes_block(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "uv" in msg


def test_uv_cache_dir_env_is_protected(home, tmp_path, monkeypatch):
    custom = tmp_path / "uvc"
    monkeypatch.setenv("UV_CACHE_DIR", str(custom))
    _blocked(f"rm -rf {custom}", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "bun pm cache",
        "bun install --frozen-lockfile",
        "uv cache dir",
        "uv cache prune",
        "uv sync",
        "rm -rf node_modules",
        "rm -rf ~/.bun/bin/old-bun",
        "rm -rf ~/.cache/pip",
        "rm -rf ~/.cache/pip*",
        "rm -rf /tmp/scratch",
        "find ~ -name '*.orig' -delete",
        "find ~/.bun/install/cache -name '*.tgz' -print",
        "ls ~/.bun/install/cache | head",
        "du -sh ~/.bun/install/cache",
        "mv /tmp/a ~/.cache/uv-notes",
        "echo 'rm -rf ~/.bun/install/cache' > note.txt",
        "git commit -m 'never run bun pm cache rm'",
    ],
)
def test_store_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


def test_unparseable_command_still_blocks_literal_store_wipe(home, tmp_path, monkeypatch):
    _blocked("bun pm cache rm; echo 'unterminated", monkeypatch, tmp_path)
    _allowed("echo 'unterminated", monkeypatch, tmp_path)


# ── node_modules symlinks ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /Users/ultra/xp/oh-my-pi/node_modules node_modules",
        "ln -sfn /Users/ultra/xp/oh-my-pi/node_modules node_modules",
        "ln -snf ../../node_modules ./node_modules",
        "ln -sf ../../node_modules node_modules/",
        "ln --symbolic /main/node_modules node_modules",
        "ln -s /main/node_modules .",
        "ln -s /main/node_modules",
        "ln -s -t . /main/node_modules",
        "cd .worktrees/x && ln -s ../../node_modules node_modules && trap 'rm node_modules' EXIT",
        "test -e node_modules || ln -s /Users/ultra/xp/hypercalendarbot/node_modules node_modules",
    ],
)
def test_node_modules_symlink_blocks(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "symlink node_modules" in msg
    assert "bun install --frozen-lockfile" in msg


@pytest.mark.parametrize(
    "command",
    [
        "ln -s ../pkg node_modules/pkg",
        "ln -s ../pkg node_modules/",
        "ln -s /main/node_modules/.bin/tsc ./tsc",
        "ln /a/file hardlink",
        "ln -s ~/.config/foo.toml foo.toml",
    ],
)
def test_other_symlinks_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


# ── npm / pnpm / yarn in bun projects ────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "npm i",
        "npm ci",
        "npm i -D typescript",
        "npm install --save-dev vitest",
        "npm --loglevel warn install",
        "pnpm install",
        "pnpm i --frozen-lockfile",
        "pnpm add zod",
        "yarn",
        "yarn install",
        "yarn add zod",
        "timeout 600 npm ci",
        "NODE_ENV=development npm install",
    ],
)
def test_foreign_pm_install_in_bun_repo_blocks(command, bun_repo, home, monkeypatch):
    msg = _blocked(command, monkeypatch, bun_repo)
    assert "bun.lock" in msg
    assert "bun install --frozen-lockfile" in msg


def test_npm_install_in_bun_subdir_without_own_lockfile_blocks(bun_repo, home, monkeypatch):
    sub = bun_repo / "packages" / "web"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text("{}\n", encoding="utf-8")
    _blocked("npm install", monkeypatch, sub)


def test_cd_and_prefix_redirect_the_lockfile_lookup(bun_repo, home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _blocked(f"cd {bun_repo} && npm install", monkeypatch, elsewhere)
    _blocked(f"npm install --prefix {bun_repo}", monkeypatch, elsewhere)
    _blocked(f"npm install --prefix={bun_repo}", monkeypatch, elsewhere)
    _blocked(f"pnpm --dir {bun_repo} install", monkeypatch, elsewhere)
    _blocked(f"pnpm -C {bun_repo} install", monkeypatch, elsewhere)
    _blocked(f"yarn --cwd {bun_repo} install", monkeypatch, elsewhere)


def test_bun_lockb_counts_as_bun(tmp_path, home, monkeypatch):
    repo = tmp_path / "legacy"
    repo.mkdir()
    (repo / "bun.lockb").write_bytes(b"\0")
    _blocked("npm ci", monkeypatch, repo)


def test_bun_lock_beside_package_lock_still_blocks(bun_repo, home, monkeypatch):
    (bun_repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _blocked("npm ci", monkeypatch, bun_repo)


def test_npm_only_subproject_inside_bun_repo_allowed(bun_repo, home, monkeypatch):
    sub = bun_repo / "tools" / "legacy-npm"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text("{}\n", encoding="utf-8")
    (sub / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _allowed("npm ci", monkeypatch, sub)
    _allowed("npm install", monkeypatch, sub)


@pytest.mark.parametrize(
    "command",
    [
        "npm i -g @anthropic-ai/claude-code",
        "npm install --global pnpm",
        "npm install --location=global pnpm",
        "npm run build",
        "npm test",
        "npm --version",
        "npx tsc --noEmit",
        "yarn test",
        "yarn --version",
        "yarn run lint",
        "pnpm run build",
        "pnpm dlx create-vite",
        "bun install --frozen-lockfile",
        "bun add zod",
    ],
)
def test_non_install_pm_commands_allowed_in_bun_repo(command, bun_repo, home, monkeypatch):
    _allowed(command, monkeypatch, bun_repo)


def test_npm_install_without_any_lockfile_allowed(tmp_path, home, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _allowed("npm install", monkeypatch, plain)


def test_npm_only_repo_allowed(tmp_path, home, monkeypatch):
    repo = tmp_path / "npm-repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _allowed("npm ci", monkeypatch, repo)


# ── protocol ──────────────────────────────────────────────────────────────────


def test_bad_event_json_allows(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert psg.main() == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_internal_error_allows(home, tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(psg, "inspect", _boom)
    _allowed("npm install", monkeypatch, tmp_path)


def test_script_exit_code_is_protocol_block(home, tmp_path):
    """Run the executable the way a bridge does: exit 10 + a block JSON on stdout."""
    event = {"args": {"command": "bun pm cache rm"}, "cwd": str(tmp_path)}
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 10, res.stderr
    assert json.loads(res.stdout)["decision"] == "block"


def test_descriptor_shape():
    spec = json.loads(_DESCRIPTOR.read_text(encoding="utf-8"))
    assert spec["id"] == "package-store-guard"
    assert spec["point"] == "pre-bash"
    assert spec["cmd"].endswith("/agent-hooks/package-store-guard/package_store_guard.py")
    assert spec["on_error"] in {"open", "closed"}


def test_script_is_executable():
    if os.name == "nt":
        pytest.skip("POSIX mode bits")
    assert os.access(_HOOK, os.X_OK)
