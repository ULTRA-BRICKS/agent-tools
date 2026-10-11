"""Tests for the `runpod-offload` universal skill's `runpod-run` script (agent-tools#865):
the one-liner that moves a heavy command (test suite, build, benchmark) onto a Runpod pod.

These tests never reach Runpod: they exercise the offline surfaces — `--dry-run` (the plan
and the lockfile-driven bootstrap detection), the no-API-key refusal, and `--help` — with
HOME pointed at an empty directory (no `~/.runpod/config.toml` to pick a key from) and the
API base pointed at an unroutable address, so a regression that makes an offline path call
the API fails loudly instead of silently reaching the real account.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_runpod_offload.py -q
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "universal" / "runpod-offload" / "runpod-run"
# The script refuses to start without these (`needs X on PATH`); a CI image that lacks one
# must skip, not fail, the offline tests — they exercise runpod-run, not the image.
_MISSING_TOOLS = [t for t in ("curl", "jq", "rsync", "ssh", "git", "flock") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_MISSING_TOOLS), reason=f"runpod-run needs {_MISSING_TOOLS} on PATH")


def _run(args: list[str], cwd: Path, home: Path, key: str = "") -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "RUNPOD_API_KEY": key,
        # RFC 5737 TEST-NET: nothing answers, so an accidental API call fails fast.
        "RUNPOD_API_URL": "http://192.0.2.1:9/v2",
        "RUNPOD_RUN_STATE": str(home / "state"),
    }
    return subprocess.run(
        [str(_SCRIPT), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    for name, body in files.items():
        (path / name).write_text(body)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )
    return path


def test_script_is_executable_and_help_exits_zero(tmp_path: Path) -> None:
    assert os.access(_SCRIPT, os.X_OK)
    res = _run(["--help"], cwd=tmp_path, home=tmp_path)
    assert res.returncode == 0
    assert "runpod-run -- bun test" in res.stdout
    assert "--status" in res.stdout and "--stop" in res.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    res = subprocess.run(["shellcheck", "-S", "style", str(_SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_missing_api_key_exits_2_with_instructions(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", {"package.json": "{}"})
    res = _run(["--", "bun", "test"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 2
    assert "RUNPOD_API_KEY" in res.stderr
    assert "runpodctl doctor" in res.stderr


def test_dry_run_needs_no_key_and_prints_the_plan(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "my-repo", {"bun.lock": "", "package.json": "{}"})
    res = _run(["--dry-run", "--", "bun", "test", "./tests"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "dry run" in out
    assert "rr-" in out and "-my-repo" in out  # pod name = rr-<host>-<repo>
    assert "cpu3g, 4 vCPU" in out  # the default compute
    assert "bun install --frozen-lockfile" in out
    assert "timeout 3600s bun test ./tests" in out
    assert "terminate pod" in out
    assert "NO API KEY" in out


def test_cpu_disk_default_respects_the_10gb_per_vcpu_cap(tmp_path: Path) -> None:
    # Runpod rejects a cpu3c-2 pod with the 40 GB default ("Container Disk must be <= 20").
    repo = _git_repo(tmp_path / "small", {"README.md": ""})
    res = _run(["--dry-run", "--cpu", "cpu3c-2", "--", "true"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "cpu3c, 2 vCPU" in res.stdout and "20 GB disk" in res.stdout
    res = _run(["--dry-run", "--cpu", "cpu3c-2", "--disk", "15", "--", "true"], cwd=repo, home=tmp_path / "home")
    assert "15 GB disk" in res.stdout
    res = _run(["--dry-run", "--", "true"], cwd=repo, home=tmp_path / "home")
    assert "cpu3g, 4 vCPU" in res.stdout and "40 GB disk" in res.stdout
    res = _run(["--dry-run", "--cpu", "cpu3c-2", "--disk", "21", "--", "true"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 2 and "exceeds Runpod's cap of 10 GB per vCPU" in res.stderr


def test_keep_without_ttl_is_refused(tmp_path: Path) -> None:
    # A kept pod bills until terminated; every kept pod must carry a deadline.
    repo = _git_repo(tmp_path / "keep", {"README.md": ""})
    res = _run(["--dry-run", "--keep", "--", "true"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 2
    assert "--keep needs --ttl" in res.stderr
    res = _run(["--dry-run", "--keep", "--ttl", "30", "--", "true"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "terminate after 30 idle minutes" in res.stdout


def test_malformed_cpu_spec_is_a_usage_error(tmp_path: Path) -> None:
    # Never swallowed into the command line, never discovered only at POST /pods.
    repo = _git_repo(tmp_path / "cpu", {"README.md": ""})
    for spec, msg in (("cpu3g4", "--cpu wants FLAVOR-VCPUS"), ("cpu3c-3", "power of two"), ("cpu3c-1", "power of two from 2 up")):
        res = _run(["--dry-run", "--cpu", spec, "--", "true"], cwd=repo, home=tmp_path / "home")
        assert res.returncode == 2, (spec, res.stdout)
        assert msg in res.stderr, (spec, res.stderr)


def test_dry_run_detects_uv_and_bun_without_lockfile(tmp_path: Path) -> None:
    repo = _git_repo(
        tmp_path / "py-bun",
        {
            "uv.lock": "",
            "pyproject.toml": "[project]\nname='x'\n",
            "package.json": '{"scripts":{"test":"bun test"}}',
            ".gitignore": "bun.lock\n",
        },
    )
    res = _run(["--dry-run", "--cpu", "cpu3c-8", "--ttl", "30", "--", "pytest", "-q"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "cpu3c, 8 vCPU" in res.stdout
    bootstrap = next(line for line in res.stdout.splitlines() if line.lstrip().startswith("bootstrap:"))
    assert "uv + uv sync --frozen" in bootstrap
    assert "+ bun install" in bootstrap and "bun install --frozen-lockfile" not in bootstrap
    assert "terminate after 30 idle minutes" in res.stdout


def test_run_outside_a_git_checkout_exits_2(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    res = _run(["--dry-run", "--", "true"], cwd=plain, home=tmp_path / "home")
    assert res.returncode == 2
    assert "not inside a git checkout" in res.stderr


def test_nothing_to_run_is_a_usage_error(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r", {"a": "1"})
    res = _run([], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 2
    assert "nothing to run" in res.stderr


class _StubRunpod(http.server.BaseHTTPRequestHandler):
    """Two pages of GET /pods behind a cursor; records every DELETE. One owner tag ("home")."""

    pages: dict[str, dict[str, object]] = {}
    deleted: list[str] = []

    def _send(self, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        path, _, query = self.path.partition("?")
        assert path == "/v2/pods", path
        cursor = urllib.parse.unquote(query.removeprefix("cursor=")) if query else ""
        self._send(self.pages[cursor])

    def do_DELETE(self) -> None:  # noqa: N802
        self.deleted.append(self.path.removeprefix("/v2/pods/"))
        self._send({})

    def log_message(self, *_: object) -> None:
        pass


def _pod(pid: str, name: str, owner: str | None) -> dict[str, object]:
    env = {"RUNPOD_RUN_OWNER": owner} if owner else {}
    return {"id": pid, "name": name, "status": "RUNNING", "cost": 0.1, "createdAt": "2026-10-11T00:00:00.5Z", "env": env}


def test_status_pages_through_pods_and_reaps_an_expired_ttl(tmp_path: Path) -> None:
    # The same transform as the script's slug(): non-alphanumerics -> "-", lowercased.
    # Exactly what the script does: `hostname -s` through slug(), "host" when that fails.
    try:
        short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        short = ""
    host = re.sub(r"[^A-Za-z0-9]", "-", short).lower() or "host"
    mine, impostor, old = f"rr-{host}-a-11111111", f"rr-{host}-copycat", f"rr-{host}-b-22222222"
    _StubRunpod.pages = {
        # page one hands the cursor out; the impostor copies the name prefix but has no owner tag
        # an opaque cursor carries +, / and =: the script must percent-encode it on the way out
        "": {"pods": [_pod("p1", mine, host), _pod("p2", impostor, None)], "pagination": {"hasNextPage": True, "nextCursor": "c2+x/y=="}},
        "c2+x/y==": {"pods": [_pod("p3", old, host), _pod("p4", "unrelated", None)], "pagination": {"hasNextPage": False, "nextCursor": None}},
    }
    _StubRunpod.deleted = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubRunpod)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        home = tmp_path / "home"
        state = home / "state"
        state.mkdir(parents=True)
        (state / "p3.ttl").write_text("999999999 1\n")  # dead timer pid, deadline long past
        (state / "p1.ttl").write_text(f"999999999 {2**31}\n")  # dead timer, deadline far ahead
        (state / "p4.ttl").write_text("999999999 1\n")  # expired, but p4 is not ours: never touched
        (state / "running.999999998").write_text("p2\n")  # a dead run's pod, but p2 is not ours either
        (state / "creating.999999997").write_text(f"{impostor}\n")  # dead run, name resolves to the impostor: not ours
        env = {**os.environ, "HOME": str(home), "RUNPOD_API_KEY": "k", "RUNPOD_RUN_STATE": str(state),
               "RUNPOD_API_URL": f"http://127.0.0.1:{server.server_port}/v2"}
        res = subprocess.run([str(_SCRIPT), "--status"], env=env, capture_output=True, text=True, timeout=60)
    finally:
        server.shutdown()
    assert res.returncode == 0, res.stderr
    assert _StubRunpod.deleted == ["p3"], (res.stdout, res.stderr)  # only the expired, owned one
    assert "passed its --ttl deadline" in res.stderr
    assert not (state / "p3.ttl").exists() and not (state / "p4.ttl").exists()
    assert not (state / "running.999999998").exists() and not (state / "creating.999999997").exists()
    created, _, others = res.stdout.partition("other pods on this account")
    assert "p1" in created and "p3" in created and "no timer: reaped on next run" in created
    assert "p2" in others and "p4" in others and "p2" not in created
    assert "account total: $0.4/h across 4 pod(s)" in res.stdout
