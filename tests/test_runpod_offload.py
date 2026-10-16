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

import contextlib
import fcntl
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "universal" / "runpod-offload" / "runpod-run"
# The script refuses to start without these (`needs X on PATH`); a CI image that lacks one
# must skip, not fail, the offline tests — they exercise runpod-run, not the image.
_MISSING_TOOLS = [t for t in ("curl", "jq", "rsync", "ssh", "git", "flock", "python3") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_MISSING_TOOLS), reason=f"runpod-run needs {_MISSING_TOOLS} on PATH")


def _account_env(home: Path) -> dict[str, str]:
    """The script's environment for a test home: HOME, RUNPOD_RUN_STATE = home/state, and a fake
    `getent` first on PATH that names home/account as the account's passwd home. The script
    keeps the account state (ledger, pod slots, billing cache, budget lock) and reads the
    budget files there, never under HOME; without the fake it would use the real account's."""
    bindir = home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    getent = bindir / "getent"
    getent.write_text(f"#!/bin/sh\necho \"runner:x:$(id -u):$(id -g)::{home / 'account'}:/bin/bash\"\n")
    getent.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home),
            "RUNPOD_RUN_STATE": str(home / "state")}


def _account_state(home: Path) -> Path:
    """Where the script keeps the account state under _account_env(home)."""
    return home / "account" / ".cache" / "runpod-run"


def _run(args: list[str], cwd: Path, home: Path, key: str = "") -> subprocess.CompletedProcess[str]:
    # RFC 5737 TEST-NET: nothing answers, so an accidental API call fails fast.
    env = {**_account_env(home), "RUNPOD_API_KEY": key, "RUNPOD_API_URL": "http://192.0.2.1:9/v2"}
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


@pytest.fixture
def runpod_runs() -> Iterator[Callable[..., int]]:
    """Spawns real processes whose command line names runpod-run: what a pod slot marker's
    holder must be for the script to count it as live (it reads /proc/<pid>/cmdline)."""
    procs: list[subprocess.Popen[bytes]] = []

    def spawn(*words: str, ignore_term: bool = False) -> int:
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else "import time"
        procs.append(subprocess.Popen([sys.executable, "-c", f"{code}; time.sleep(120)", "runpod-run", *words]))
        pid = procs[-1].pid
        if ignore_term:  # ready once the kernel lists SIGTERM (bit 15) among its ignored signals
            _until(lambda: int(re.findall(r"SigIgn:\s*(\w+)", Path(f"/proc/{pid}/status").read_text())[0], 16)
                   & (1 << (signal.SIGTERM - 1)))
        return pid

    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def _until(condition: Callable[[], object], timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


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
    res = _run(["--dry-run", "--cpu", "cpu3c-2", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "cpu3c, 2 vCPU" in res.stdout and "20 GB disk" in res.stdout
    res = _run(["--dry-run", "--cpu", "cpu3c-2", "--disk", "15", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert "15 GB disk" in res.stdout
    res = _run(["--dry-run", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert "cpu3g, 4 vCPU" in res.stdout and "40 GB disk" in res.stdout
    res = _run(["--dry-run", "--cpu", "cpu3c-2", "--disk", "21", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 2 and "exceeds Runpod's cap of 10 GB per vCPU" in res.stderr


def test_keep_without_ttl_is_refused(tmp_path: Path) -> None:
    # A kept pod bills until terminated; every kept pod must carry a deadline.
    repo = _git_repo(tmp_path / "keep", {"README.md": ""})
    res = _run(["--dry-run", "--keep", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 2
    assert "--keep needs --ttl" in res.stderr
    res = _run(["--dry-run", "--keep", "--ttl", "10", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "terminate after 10 idle minutes" in res.stdout
    assert "clamped" not in res.stderr


def test_ttl_above_ten_minutes_is_clamped(tmp_path: Path) -> None:
    # An idle kept pod bills like a busy one: 10 idle minutes is the most a run may buy.
    repo = _git_repo(tmp_path / "ttl", {"README.md": ""})
    res = _run(["--ttl", "30", "--dry-run", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "runpod-run: --ttl 30 clamped to 10 min (max)" in res.stderr
    assert "terminate after 10 idle minutes" in res.stdout
    # Past bash's integer range: still clamped, never an "integer expected" skip of the cap.
    res = _run(["--ttl", "999999999999999999999999", "--dry-run", "--", "make"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "integer" not in res.stderr and "clamped to 10 min (max)" in res.stderr
    assert "terminate after 10 idle minutes" in res.stdout


def test_policy_refusals_happen_before_any_network_call(tmp_path: Path) -> None:
    # TEST-NET API URL + a key: a refusal that reached the network would time out, not exit 20.
    repo = _git_repo(tmp_path / "pol", {"README.md": ""})
    home = tmp_path / "home"
    res = _run(["--dry-run", "--", "bun", "test", "tests/x.test.ts"], cwd=repo, home=home)
    assert res.returncode == 20, (res.stdout, res.stderr)
    assert "run it locally" in res.stderr and res.stdout == ""
    res = _run(["--dry-run", "--remote-reason", "flaky only on linux", "--", "bun", "test", "tests/x.test.ts"], cwd=repo, home=home)
    assert res.returncode == 0, res.stderr
    res = _run(["--", "true"], cwd=repo, home=home, key="k")
    assert res.returncode == 20 and "no-op" in res.stderr
    res = _run(["--dry-run", "--", "true"], cwd=repo, home=home)
    assert res.returncode == 20
    res = _run(["--dry-run", "--", "bash", "-c", "exit 0"], cwd=repo, home=home)
    assert res.returncode == 20 and "no-op" in res.stderr
    res = _run(["--cpu", "cpu3c-8", "--dry-run", "--", "pytest", "tests"], cwd=repo, home=home)
    assert res.returncode == 20 and "--reason" in res.stderr
    res = _run(["--cpu", "cpu3c-8", "--reason", "suite uses 8 workers", "--dry-run", "--", "pytest", "tests"], cwd=repo, home=home)
    assert res.returncode == 0, res.stderr
    res = _run(["--gpu", "NVIDIA GeForce RTX 4090", "--reason", "fast", "--dry-run", "--", "bun", "test", "./tests"], cwd=repo, home=home)
    assert res.returncode == 20 and "CUDA" in res.stderr
    res = _run(["--dry-run", "--", "sh", "-c", "env CI=1 pytest tests/a.py"], cwd=repo, home=home)
    assert res.returncode == 20 and "run it locally" in res.stderr
    # A reason that starts with a dash is still a reason, not an option of the policy helper.
    res = _run(["--cpu", "cpu3c-8", "--reason", "-j8", "--dry-run", "--", "pytest", "tests"], cwd=repo, home=home)
    assert res.returncode == 0, res.stderr


def test_policy_helper_resolves_through_a_symlinked_install(tmp_path: Path) -> None:
    # ~/.local/bin/runpod-run -> ~/.agents/skills/runpod-offload/runpod-run: the helper sits
    # next to the TARGET, not next to the symlink.
    repo = _git_repo(tmp_path / "link", {"README.md": ""})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "runpod-run").symlink_to(_SCRIPT)
    env = {**_account_env(tmp_path / "home"), "RUNPOD_API_KEY": "", "RUNPOD_API_URL": "http://192.0.2.1:9/v2"}
    res = subprocess.run([str(bindir / "runpod-run"), "--dry-run", "--", "true"], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 20 and "no-op" in res.stderr, res.stderr


def _ledger(home: Path) -> list[dict[str, Any]]:
    path = _account_state(home) / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_every_run_appends_one_ledger_line_even_with_stderr_discarded(tmp_path: Path) -> None:
    # `runpod-run … 2>/dev/null | tail` must still leave its trace: the script writes the
    # ledger file itself. A refusal happens before any network call (TEST-NET API + key).
    repo = _git_repo(tmp_path / "ledg", {"README.md": ""})
    home = tmp_path / "home"
    env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": "http://192.0.2.1:9/v2",
           "RIG_AGENT_ID": "Main.Tester", "OMP_SESSION_ID": "sess-1"}
    res = subprocess.run(["bash", "-c", f"{_SCRIPT} --reason 'why not' -- true 2>/dev/null"], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 20 and res.stderr == ""
    lines = _ledger(home)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line["event"] == "refused" and line["exit"] == 20
    assert line["agent"] == "Main.Tester" and line["session"] == "sess-1"
    assert line["repo"] == "ledg" and line["cmd"] == "true" and line["reason"] == "why not"
    assert line["pod_id"] is None and line["command_s"] is None and line["flavor"] == "cpu3g-4"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", str(line["ts"])), line["ts"]
    # A run that fails before any pod (no API key) is a run too, exit 2, and so is a malformed
    # option; a dry run, --help, --status and --report are not runs.
    res = _run(["--", "make"], cwd=repo, home=home)
    assert res.returncode == 2
    res = _run(["--ttl", "nope", "--", "make"], cwd=repo, home=home)
    assert res.returncode == 2 and "--ttl" in res.stderr
    res = _run(["--dry-run", "--", "make"], cwd=repo, home=home)
    assert res.returncode == 0, res.stderr
    res = _run(["--dry-run", "--", "true"], cwd=repo, home=home)
    assert res.returncode == 20
    res = _run(["--help"], cwd=repo, home=home)
    assert res.returncode == 0
    res = _run(["--report"], cwd=repo, home=home)
    assert res.returncode == 0, res.stderr
    lines = _ledger(home)
    assert [(x["event"], x["exit"]) for x in lines] == [("refused", 20), ("run", 2), ("run", 2)], lines


class _StubBilling(http.server.BaseHTTPRequestHandler):
    """GET /billing: records the query, answers two hourly records."""

    queries: list[dict[str, list[str]]] = []

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        path, _, query = self.path.partition("?")
        assert path == "/v2/billing", path
        self.queries.append(urllib.parse.parse_qs(query))
        data = json.dumps({"records": [{"totalAmount": 1.25}, {"totalAmount": 0.75}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: object) -> None:
        pass


def test_report_reads_the_local_day_from_billing_and_the_ledger(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = _account_state(home)  # the account's ledger, whatever RUNPOD_RUN_STATE says
    state.mkdir(parents=True)
    # A day safely in the past on any CI clock, so its window ends at the next midnight, not now.
    (state / "ledger.jsonl").write_text(json.dumps(
        {"ts": "2024-07-15T12:00:00+02:00", "event": "run", "repo": "app", "exit": 0, "usd_per_h": 0.36,
         "boot_s": 0, "sync_s": 0, "bootstrap_s": 0, "command_s": 5000}) + "\n")
    env = {**_account_env(home), "TZ": "Europe/Berlin"}
    # No key: no network call at all (TEST-NET would hang), billed $ unknown.
    res = subprocess.run([str(_SCRIPT), "--report", "--day", "2024-07-15"], cwd=tmp_path, capture_output=True, text=True,
                         timeout=60, check=False, env={**env, "RUNPOD_API_KEY": "", "RUNPOD_API_URL": "http://192.0.2.1:9/v2"})
    assert res.returncode == 0, res.stderr
    assert "billed:   unknown" in res.stdout and "$0.500" in res.stdout
    _StubBilling.queries = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubBilling)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        res = subprocess.run([str(_SCRIPT), "--report", "--day", "2024-07-15"], cwd=tmp_path, capture_output=True,
                             text=True, timeout=60, check=False,
                             env={**env, "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{server.server_port}/v2"})
    finally:
        server.shutdown()
    assert res.returncode == 0, res.stderr
    # The local day 2024-07-15 in CEST is 22:00Z on the 14th to 22:00Z on the 15th.
    assert _StubBilling.queries == [{"bucketSize": ["hour"], "startTime": ["2024-07-14T22:00:00Z"],
                                     "endTime": ["2024-07-15T22:00:00Z"]}]
    assert "billed:   $2.000" in res.stdout
    g_row = next(line for line in res.stdout.splitlines() if "G unattributed" in line)
    assert g_row.rstrip().endswith("$1.500"), res.stdout
    bad = subprocess.run([str(_SCRIPT), "--report", "--day", "yesterday"], cwd=tmp_path, capture_output=True,
                         text=True, timeout=60, check=False, env={**env, "RUNPOD_API_KEY": ""})
    assert bad.returncode == 2 and "--day" in bad.stderr
    # A day without ledger lines is read from the run logs, which stay in RUNPOD_RUN_STATE.
    runs = home / "state" / "runs"
    runs.mkdir(parents=True)
    log = runs / "20240716-120000-app-11111111.log"
    log.write_text("ok\n")
    started = datetime(2024, 7, 16, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    os.utime(log, (started + 600, started + 600))
    res = subprocess.run([str(_SCRIPT), "--report", "--day", "2024-07-16"], cwd=tmp_path, capture_output=True,
                         text=True, timeout=60, check=False, env={**env, "RUNPOD_API_KEY": ""})
    assert res.returncode == 0 and "legacy: 1 runs from run logs" in res.stdout, res.stdout + res.stderr


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
    res = _run(["--dry-run", "--cpu", "cpu3c-8", "--reason", "suite parallelizes", "--ttl", "10", "--", "pytest", "-q"], cwd=repo, home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    assert "cpu3c, 8 vCPU" in res.stdout
    bootstrap = next(line for line in res.stdout.splitlines() if line.lstrip().startswith("bootstrap:"))
    assert "uv + uv sync --frozen" in bootstrap
    assert "+ bun install" in bootstrap and "bun install --frozen-lockfile" not in bootstrap
    assert "terminate after 10 idle minutes" in res.stdout


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


def _host_tag() -> str:
    # The same transform as the script's slug(): non-alphanumerics -> "-", lowercased.
    # Exactly what the script does: `hostname -s` through slug(), "host" when that fails.
    try:
        short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        short = ""
    return re.sub(r"[^A-Za-z0-9]", "-", short).lower() or "host"


def test_status_pages_through_pods_and_reaps_an_expired_ttl(tmp_path: Path, runpod_runs: Callable[..., int]) -> None:
    host = _host_tag()
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
        # The run that kept p3 noted when it went idle: the reaper books that idle time.
        (state / "p3.lastrun").write_text(json.dumps({"ended": int(time.time()) - 900, "usd_per_h": 0.1, "repo": "b"}))
        (state / "p1.ttl").write_text(f"999999999 {2**31}\n")  # dead timer, deadline far ahead
        (state / "p4.ttl").write_text("999999999 1\n")  # expired, but p4 is not ours: never touched
        (state / "running.999999998").write_text("p2\n")  # a dead run's pod, but p2 is not ours either
        (state / "creating.999999997").write_text(f"{impostor}\n")  # dead run, name resolves to the impostor: not ours
        slots = _account_state(home) / "slots"
        slots.mkdir(parents=True)
        holder = runpod_runs()
        (slots / mine).write_text(f"{holder}\n")  # a run on this host holds p1's slot
        env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{server.server_port}/v2"}
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
    [line] = _ledger(home)  # --status is no run: the reaped pod's terminate line is the only one
    assert line["event"] == "terminate" and line["pod_id"] == "p3" and line["pod_name"] == old and line["repo"] == "b"
    assert 900 <= line["idle_s"] < 960, line
    assert not (state / "p3.lastrun").exists()
    assert f"slots: 1/3 live ({mine}), 0 queued" in res.stdout
    assert re.search(rf"^  {re.escape(mine)} +pid {holder} \(run\)$", res.stdout, re.MULTILINE), res.stdout


def test_stop_of_a_kept_pod_appends_a_terminate_line_with_its_idle_time(tmp_path: Path,
                                                                         runpod_runs: Callable[..., int]) -> None:
    host = _host_tag()
    name = f"rr-{host}-app-11111111"
    _StubRunpod.pages = {"": {"pods": [_pod("p1", name, host)], "pagination": {"hasNextPage": False}}}
    _StubRunpod.deleted = []
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    # What a --ttl run leaves behind when it keeps its pod: the end of the run and its price.
    ended = int(time.time()) - 300
    (state / "p1.lastrun").write_text(json.dumps(
        {"ended": ended, "usd_per_h": 0.36, "repo": "app", "repo_tag": "app-11111111", "pod_name": name, "flavor": "cpu3g-4"}))
    # Its slot, held by its --ttl timer. --stop cancels the timer first, yet the release must
    # not hinge on that kill landing in time: this timer shrugs SIGTERM off and stays alive.
    timer = runpod_runs("--stop", "p1", ignore_term=True)
    (state / "p1.ttl").write_text(f"{timer} {int(time.time()) + 600}\n")
    slots = _account_state(home) / "slots"
    slots.mkdir(parents=True)
    (slots / name).write_text(f"{timer}\n")
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubRunpod)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{server.server_port}/v2",
               "RIG_AGENT_ID": "timer-owner"}
        res = subprocess.run([str(_SCRIPT), "--stop", "p1"], env=env, capture_output=True, text=True, timeout=60, check=False)
    finally:
        server.shutdown()
    assert res.returncode == 0, res.stderr
    assert _StubRunpod.deleted == ["p1"]
    [line] = _ledger(home)
    assert line["event"] == "terminate" and line["pod_id"] == "p1" and line["pod_name"] == name
    assert line["repo"] == "app" and line["agent"] == "timer-owner" and line["exit"] is None
    assert 300 <= line["idle_s"] < 360, line
    assert line["est_usd"] == pytest.approx(0.36 * line["idle_s"] / 3600)
    assert not (state / "p1.lastrun").exists()
    assert not (slots / name).exists()  # the terminated pod no longer holds a slot


class _StubBudget(http.server.BaseHTTPRequestHandler):
    """The API a budget-refused run touches: GET /pods, the CPU catalog ($0.04/vCPU-h) and
    /billing ($5.90 so far today). Records every POST/DELETE: a refusal must make none."""

    pods: ClassVar[list[dict[str, object]]] = []
    mutations: ClassVar[list[str]] = []
    billing_reads: ClassVar[int] = 0

    def _send(self, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        path = self.path.partition("?")[0]
        if path == "/v2/pods":
            self._send({"pods": self.pods, "pagination": {"hasNextPage": False}})
        elif path == "/v2/catalog/cpus":
            self._send({"cpus": [{"id": "cpu3g", "price": {"securePerVcpu": 0.04}}]})
        elif path == "/v2/billing":
            type(self).billing_reads += 1
            self._send({"metadata": {}, "records": [{"totalAmount": 3.4}, {"totalAmount": 2.5}]})
        else:
            raise AssertionError(path)

    def do_POST(self) -> None:  # noqa: N802
        self.mutations.append(f"POST {self.path}")
        self._send({"id": "created"})

    def do_DELETE(self) -> None:  # noqa: N802
        self.mutations.append(f"DELETE {self.path}")
        self._send({})

    def log_message(self, *_: object) -> None:
        pass


def _app_pod() -> str:
    """The pod name `--name app` gives (the managed prefix kept), as _budget_run passes it."""
    return f"rr-{_host_tag()}-app"


def _budget_env(home: Path, port: int) -> dict[str, str]:
    """The run's environment against the stub on `port`, on the account-home seam of
    _account_env (the budget files live in home/account). XDG_CONFIG_HOME points elsewhere,
    as an agent might set it."""
    return {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{port}/v2",
            "RUNPOD_RUN_SSH_KEY": str(home / "id"), "XDG_CONFIG_HOME": str(home / "cfg")}


def _budget_run(args: list[str], repo: Path, home: Path) -> subprocess.CompletedProcess[str]:
    _StubBudget.mutations = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubBudget)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return subprocess.run([str(_SCRIPT), "--name", "app", *args], cwd=repo,
                              env=_budget_env(home, server.server_port), capture_output=True, text=True, timeout=60,
                              check=False)
    finally:
        server.shutdown()


def test_daily_budget_refuses_a_create_with_exit_21_and_books_it(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "bud", {"README.md": ""})
    home = tmp_path / "home"
    # $5.90 billed today + a $0.16/h cpu3g-4 for its 1 h --timeout = $6.06 >= the $6 default cap.
    _StubBudget.pods = []
    res = _budget_run(["--", "make"], repo, home)
    assert res.returncode == 21, res.stderr
    assert _StubBudget.mutations == [], res.stderr  # no POST /pods
    assert "$5.900 billed + $0.000 live/committed + $0.160 this run = $6.060 of $6.000 cap → REFUSE" in res.stderr
    assert 'send a decision request with tg --tag decision --title "runpod-run daily budget reached"' in res.stderr
    [line] = _ledger(home)
    assert (line["event"], line["exit"]) == ("budget", 21)
    # Today's billed $ is cached in the account state, not in RUNPOD_RUN_STATE.
    assert (_account_state(home) / "billing-today.json").exists()
    assert not (home / "state" / "billing-today.json").exists()
    # A shorter --timeout commits less: $5.90 + $0.16/h for 1 min fits (dry run: nothing created).
    res = _budget_run(["--timeout", "60", "--dry-run", "--", "make"], repo, home)
    assert res.returncode == 0, res.stderr
    assert "budget:     today $5.900 billed + $0.000 live/committed + $0.003 this run = $5.903 of $6.000 cap → ok" \
        in res.stdout


def test_daily_budget_refuses_starting_a_stopped_pod(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "bud", {"README.md": ""})
    home = tmp_path / "home"
    name = _app_pod()
    # The stopped pod's own $/h is not "live"; a running pod elsewhere on the account is.
    _StubBudget.pods = [
        {"id": "p1", "name": name, "status": "EXITED", "cost": 0.16, "cpu": {"id": "cpu3g", "vcpuCount": 4},
         "env": {"RUNPOD_RUN_OWNER": _host_tag()}},
        {"id": "p2", "name": "someone-else", "status": "RUNNING", "cost": 0.5, "env": {}},
    ]
    res = _budget_run(["--", "make"], repo, home)
    assert res.returncode == 21, res.stderr
    assert _StubBudget.mutations == [], res.stderr  # no POST /pods/p1/action
    assert "$5.900 billed + $0.500 live/committed + $0.160 this run = $6.560 of $6.000 cap → REFUSE" in res.stderr
    [line] = _ledger(home)
    assert (line["event"], line["exit"]) == ("budget", 21)
    # The dry run reads the same figures (read-only) and says what a run would hit.
    res = _budget_run(["--dry-run", "--", "make"], repo, home)
    assert res.returncode == 0 and _StubBudget.mutations == [], res.stderr
    assert "→ REFUSE" in res.stdout and len(_ledger(home)) == 1


def test_a_budget_refused_replacement_keeps_the_old_pod(tmp_path: Path) -> None:
    # This repo's pod is a GPU pod; the run wants the default CPU shape, so it would replace
    # it. The budget refuses the create: the old pod must still be there (no DELETE).
    repo = _git_repo(tmp_path / "bud", {"README.md": ""})
    home = tmp_path / "home"
    _StubBudget.pods = [{"id": "g1", "name": _app_pod(), "status": "RUNNING", "cost": 0.69,
                         "gpu": {"id": "NVIDIA GeForce RTX 4090"}, "env": {"RUNPOD_RUN_OWNER": _host_tag()}}]
    res = _budget_run(["--", "make"], repo, home)
    assert res.returncode == 21, res.stderr
    assert _StubBudget.mutations == [], res.stderr
    # The pod being replaced does not count as live: it would be gone before the create.
    assert "$5.900 billed + $0.000 live/committed + $0.160 this run" in res.stderr
    # The dry run takes the same path and says the same.
    res = _budget_run(["--dry-run", "--", "make"], repo, home)
    assert "budget:     today $5.900 billed + $0.000 live/committed + $0.160 this run" in res.stdout, res.stdout
    # A matching RUNNING pod is reused: no start, no create, so no budget check.
    _StubBudget.pods = [{"id": "c1", "name": _app_pod(), "status": "RUNNING", "cost": 0.16,
                         "cpu": {"id": "cpu3g", "vcpuCount": 4}, "env": {"RUNPOD_RUN_OWNER": _host_tag()}}]
    res = _budget_run(["--dry-run", "--", "make"], repo, home)
    assert "budget:     not checked: this run reuses running pod c1" in res.stdout, res.stdout


def test_the_budget_files_are_read_from_the_account_home_not_home_or_xdg(tmp_path: Path) -> None:
    # An agent that points HOME or XDG_CONFIG_HOME at a dir with its own override must not
    # lift the cap: only the passwd home's ~/.config/runpod-run counts.
    repo = _git_repo(tmp_path / "bud", {"README.md": ""})
    home = tmp_path / "home"
    today = date.today().isoformat()  # the script runs with this process's TZ
    for decoy in (home / ".config" / "runpod-run", home / "cfg" / "runpod-run"):
        decoy.mkdir(parents=True)
        (decoy / "budget-override").write_text(f"{today} 99\n")
    _StubBudget.pods = []
    res = _budget_run(["--", "make"], repo, home)
    assert res.returncode == 21 and "of $6.000 cap → REFUSE" in res.stderr, res.stderr
    account = home / "account" / ".config" / "runpod-run"
    account.mkdir(parents=True)
    (account / "budget-override").write_text(f"{today} 9\n")
    res = _budget_run(["--dry-run", "--", "make"], repo, home)
    assert "of $9.000 cap → ok" in res.stdout, res.stdout


def test_the_budget_check_waits_for_the_machine_wide_budget_lock(tmp_path: Path) -> None:
    # Two runs must not pass the check on the same snapshot: while another run holds the
    # budget lock (between its check and its create), this run reads no billing at all. The
    # lock is the account's: this run's RUNPOD_RUN_STATE (home/state) does not move it.
    repo = _git_repo(tmp_path / "bud", {"README.md": ""})
    home = tmp_path / "home"
    _account_state(home).mkdir(parents=True)
    _StubBudget.pods = []
    _StubBudget.mutations = []
    _StubBudget.billing_reads = 0
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubBudget)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = _budget_env(home, server.server_port)
    with (_account_state(home) / "budget.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        proc = subprocess.Popen([str(_SCRIPT), "--", "make"], cwd=repo, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(3)
            assert proc.poll() is None and _StubBudget.billing_reads == 0
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    try:
        _, err = proc.communicate(timeout=60)
    finally:
        server.shutdown()
    assert proc.returncode == 21 and _StubBudget.billing_reads == 1, err


def _hold_slots(state: Path, spawn: Callable[..., int], *names: str) -> dict[str, int]:
    """Slot markers for pods that runs on this host hold, each by a live runpod-run process."""
    (state / "slots").mkdir(parents=True, exist_ok=True)
    holders = {name: spawn() for name in names}
    for name, pid in holders.items():
        (state / "slots" / name).write_text(f"{pid}\n")
    return holders


def test_dry_run_shows_the_hosts_pod_slots_and_what_this_run_would_do(tmp_path: Path,
                                                                     runpod_runs: Callable[..., int]) -> None:
    repo = _git_repo(tmp_path / "r", {"README.md": ""})
    home = tmp_path / "home"
    slots = _account_state(home) / "slots"
    a, b, c, app = (f"rr-{_host_tag()}-{n}" for n in ("a", "b", "c", "app"))
    _hold_slots(_account_state(home), runpod_runs, a, b, c)
    (slots / f"{a}-dead").write_text("999999999\n")  # a SIGKILLed run's marker holds nothing
    res = _run(["--name", "app", "--queue-timeout", "5", "--dry-run", "--", "make"], cwd=repo, home=home)
    assert res.returncode == 0, res.stderr
    assert f"  slots:      3/3 live ({a}, {b}, {c}), 0 queued → queue (position 1 of 1); exit 22 after 5 min\n" \
        in res.stdout, res.stdout
    # This repo's pod is one of the three (kept by --ttl): the run reuses it and its slot.
    (slots / c).rename(slots / app)
    res = _run(["--name", "app", "--dry-run", "--", "make"], cwd=repo, home=home)
    assert f"  slots:      3/3 live ({a}, {app}, {b}), 0 queued → reuse (this pod holds a live slot)\n" in res.stdout
    (slots / app).unlink()
    res = _run(["--name", "app", "--dry-run", "--", "make"], cwd=repo, home=home)
    assert f"  slots:      2/3 live ({a}, {b}), 0 queued → take a free slot\n" in res.stdout
    # A dry run only reads the slots: it takes none, joins no queue, sweeps nothing.
    assert sorted(p.name for p in slots.iterdir()) == [a, f"{a}-dead", b]
    assert not (_account_state(home) / "queue").exists()
    for bad in ("0", "abc", "-5"):
        res = _run(["--queue-timeout", bad, "--dry-run", "--", "make"], cwd=repo, home=home)
        assert res.returncode == 2 and "--queue-timeout" in res.stderr, (bad, res.stderr)


def test_a_fourth_run_waits_for_a_pod_slot_before_its_budget_check(tmp_path: Path,
                                                                   runpod_runs: Callable[..., int]) -> None:
    # Three pods are live on this host: the run queues and reads no billing meanwhile (the
    # budget is checked once a slot is held, so it sees the spend as of then). A slot frees
    # because its run was killed, with that run's pod still up: the run reaps that pod before
    # it goes on — here into a budget refusal ($5.90 + $0.16 >= $6) — and gives the slot up
    # on exit.
    repo = _git_repo(tmp_path / "q", {"README.md": ""})
    home = tmp_path / "home"
    state = home / "state"  # this run's RUNPOD_RUN_STATE: the reaper's running.<pid> markers
    account = _account_state(home)  # the pod slots and their queue
    a, b, c = (f"rr-{_host_tag()}-{n}" for n in ("a", "b", "c"))
    holders = _hold_slots(account, runpod_runs, a, b, c)
    state.mkdir(parents=True)
    (state / f"running.{holders[b]}").write_text("pb\n")  # what run b records while its pod is up
    _StubBudget.pods = [{"id": "pb", "name": b, "status": "RUNNING", "cost": 0.16,
                         "cpu": {"id": "cpu3g", "vcpuCount": 4}, "env": {"RUNPOD_RUN_OWNER": _host_tag()}}]
    _StubBudget.mutations = []
    _StubBudget.billing_reads = 0
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubBudget)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = subprocess.Popen([str(_SCRIPT), "--name", "app", "--", "make"], cwd=repo,
                            env=_budget_env(home, server.server_port), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        _until(lambda: list((account / "queue").glob("*")))  # it has queued
        time.sleep(1)
        assert proc.poll() is None and _StubBudget.billing_reads == 0
        os.kill(holders[b], signal.SIGKILL)  # that run is gone: its slot frees itself
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
        server.shutdown()
    assert proc.returncode == 21, err
    assert f"runpod-run: queued for a pod slot (position 1 of 1; 3 live: {a}, {b}, {c})" in err
    assert _StubBudget.billing_reads == 1 and _StubBudget.mutations == ["DELETE /v2/pods/pb"], err
    assert err.index("pod pb terminated") < err.index("budget: today"), err  # reaped before the budget check
    assert not (account / "slots" / f"rr-{_host_tag()}-app").exists()  # given up on exit
    assert list((account / "queue").iterdir()) == []
    [line] = _ledger(home)
    assert (line["event"], line["exit"]) == ("budget", 21)


def test_home_or_runpod_run_state_cannot_give_a_run_fresh_slots_or_an_empty_ledger(
        tmp_path: Path, runpod_runs: Callable[..., int]) -> None:
    # `RUNPOD_RUN_STATE=/tmp/x runpod-run …` (or another HOME) must still see the host's 3 live
    # pods and book into the account's ledger: both live under the passwd home.
    repo = _git_repo(tmp_path / "r", {"README.md": ""})
    home = tmp_path / "home"
    a, b, c = (f"rr-{_host_tag()}-{n}" for n in ("a", "b", "c"))
    _hold_slots(_account_state(home), runpod_runs, a, b, c)
    decoy = tmp_path / "decoy"
    env = {**_account_env(home), "HOME": str(decoy), "RUNPOD_RUN_STATE": str(decoy / "state"),
           "RUNPOD_API_KEY": "", "RUNPOD_API_URL": "http://192.0.2.1:9/v2"}
    res = subprocess.run([str(_SCRIPT), "--name", "app", "--dry-run", "--", "make"], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=60, check=False)
    assert f"  slots:      3/3 live ({a}, {b}, {c}), 0 queued → queue (position 1 of 1)" in res.stdout, res.stdout
    res = subprocess.run([str(_SCRIPT), "--", "true"], cwd=repo, env=env, capture_output=True, text=True,
                         timeout=60, check=False)
    assert res.returncode == 20, res.stderr
    assert [line["event"] for line in _ledger(home)] == ["refused"]
    assert not list(decoy.rglob("ledger.jsonl")) and not list(decoy.rglob("slots"))


@contextlib.contextmanager
def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> Iterator[int]:
    """A stub API on a free local port for the duration of the block."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_port
    finally:
        server.shutdown()


class _StubPods(http.server.BaseHTTPRequestHandler):
    """The API a whole run touches: GET /pods (the pods POST created), GET /pods/<id>, the CPU
    catalog ($0.04/vCPU-h) and $0 billed today. POST /pods first answers `post_errors` in
    order, then creates pod `new1` (with `ssh` as its ssh block, None: never published).
    Records every POST body and DELETE."""

    pods: ClassVar[list[dict[str, object]]] = []
    post_errors: ClassVar[list[tuple[int, dict[str, object]]]] = []
    posts: ClassVar[list[dict[str, Any]]] = []
    deleted: ClassVar[list[str]] = []
    ssh: ClassVar[dict[str, object] | None] = None

    @classmethod
    def reset(cls, post_errors: list[tuple[int, dict[str, object]]], ssh: dict[str, object] | None = None) -> None:
        cls.pods, cls.posts, cls.deleted = [], [], []
        cls.post_errors, cls.ssh = list(post_errors), ssh

    def _send(self, body: object, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        path = self.path.partition("?")[0]
        if path == "/v2/pods":
            self._send({"pods": self.pods, "pagination": {"hasNextPage": False}})
        elif path.startswith("/v2/pods/"):
            pod = next((p for p in self.pods if p["id"] == path.removeprefix("/v2/pods/")), None)
            self._send(pod if pod else {"detail": "pod not found"}, 200 if pod else 404)
        elif path == "/v2/catalog/cpus":
            self._send({"cpus": [{"id": "cpu3g", "price": {"securePerVcpu": 0.04}}]})
        elif path == "/v2/billing":
            self._send({"metadata": {}, "records": []})
        else:
            raise AssertionError(path)

    def do_POST(self) -> None:  # noqa: N802
        assert self.path == "/v2/pods", self.path
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).posts.append(body)
        if self.post_errors:
            status, error = type(self).post_errors.pop(0)
            self._send(error, status)
            return
        pod: dict[str, object] = {"id": "new1", "name": body["name"], "status": "RUNNING", "cost": 0.16,
                                  "cpu": body.get("cpu"), "env": body["env"]}
        if self.ssh:
            pod["ssh"] = self.ssh
        type(self).pods.append(pod)
        self._send({"id": "new1"})

    def do_DELETE(self) -> None:  # noqa: N802
        type(self).deleted.append(self.path.removeprefix("/v2/pods/"))
        self._send({})

    def log_message(self, *_: object) -> None:
        pass


# What Runpod answers when the flavor has no free machine (R5: retried, same flavor).
_NO_CAPACITY = (400, {"error": "create pod: There are no longer any instances available with the requested "
                               "specifications. Please refresh and try again."})


def _pods_run(args: list[str], cwd: Path, home: Path, port: int) -> subprocess.CompletedProcess[str]:
    # RUNPOD_RUN_TEST_TIME_SCALE: the ssh wait and the capacity backoff at 1/100.
    env = {**_budget_env(home, port), "RUNPOD_RUN_TEST_TIME_SCALE": "0.01"}
    return subprocess.run([str(_SCRIPT), *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60, check=False)


def test_no_capacity_retries_the_same_flavor_three_times_then_exits_3(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "cap", {"README.md": ""})
    home = tmp_path / "home"
    _StubPods.reset([_NO_CAPACITY] * 4)
    with _serve(_StubPods) as port:
        res = _pods_run(["--", "make"], repo, home, port)
    assert res.returncode == 3, res.stderr
    assert len(_StubPods.posts) == 4, res.stderr
    assert all(body["cpu"] == {"id": "cpu3g", "vcpuCount": 4} for body in _StubPods.posts), _StubPods.posts
    for attempt, wait in ((1, 15), (2, 30), (3, 60)):
        assert f"runpod-run: no capacity for cpu3g-4; retry {attempt}/3 in {wait}s (same flavor)\n" in res.stderr
    assert "retry 4/3" not in res.stderr and "no longer any instances" in res.stderr
    assert _StubPods.deleted == []  # nothing was created
    [line] = _ledger(home)
    assert (line["event"], line["exit"], line["command_s"]) == ("run", 3, None)
    # "No instances available", in any case, is a capacity error too; any other 400 is not retried.
    _StubPods.reset([(400, {"error": "No Instances Available"}), (400, {"error": "Container Disk must be <= 20"})])
    with _serve(_StubPods) as port:
        res = _pods_run(["--", "make"], repo, home, port)
    assert res.returncode == 3 and len(_StubPods.posts) == 2, res.stderr
    assert "retry 1/3 in 15s" in res.stderr and "retry 2/3" not in res.stderr
    assert "Container Disk must be <= 20" in res.stderr


def test_a_pod_that_never_answers_ssh_is_terminated_with_exit_23(tmp_path: Path) -> None:
    # One capacity retry, then the pod is created but never publishes an ssh endpoint.
    repo = _git_repo(tmp_path / "ssh", {"README.md": ""})
    home = tmp_path / "home"
    _StubPods.reset([_NO_CAPACITY])
    with _serve(_StubPods) as port:
        res = _pods_run(["--", "make"], repo, home, port)
    assert res.returncode == 23, res.stderr
    assert len(_StubPods.posts) == 2 and "retry 1/3 in 15s (same flavor)" in res.stderr
    assert "pod new1 not reachable over ssh within 3 min" in res.stderr
    assert _StubPods.deleted == ["new1"], res.stderr  # terminated, --ttl or not
    [line] = _ledger(home)
    assert (line["event"], line["exit"], line["pod_id"], line["command_s"]) == ("run", 23, "new1", None)
    # Under --ttl too: a pod that never answered is no pod to keep, and no timer is armed.
    _StubPods.reset([])
    with _serve(_StubPods) as port:
        res = _pods_run(["--ttl", "5", "--", "make"], repo, home, port)
    assert res.returncode == 23 and _StubPods.deleted == ["new1"], res.stderr
    assert not list((home / "state").glob("*.ttl")) and "kept at" not in res.stderr


def _repo_tag(path: Path) -> str:
    """The script's repo_tag of a git checkout: slug(clone dir name)-<8 hex of its common dir>."""
    common = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, check=True).stdout.strip().removesuffix("/.git")
    return f"{re.sub(r'[^A-Za-z0-9]', '-', Path(common).name).lower()}-{hashlib.sha256(common.encode()).hexdigest()[:8]}"


def test_dry_run_lists_every_sync_dir_and_refuses_two_that_share_a_remote_dir(tmp_path: Path) -> None:
    a = _git_repo(tmp_path / "alpha", {"README.md": ""})
    b = tmp_path / "Beta Data"  # no git checkout: slug(basename)-<8 hex of its absolute path>
    b.mkdir()
    home = tmp_path / "home"
    tag_a, tag_b = _repo_tag(a), f"beta-data-{hashlib.sha256(str(b).encode()).hexdigest()[:8]}"
    res = _run(["--dry-run", "--sync-dir", str(a), "--sync-dir", str(b), "--", "make"], cwd=tmp_path, home=home)
    assert res.returncode == 0, res.stderr
    assert f"  checkout:   {a}  (repo \"alpha\"" in res.stdout  # the first is the primary
    assert re.search(rf"^ +{re.escape(str(a))} -> /workspace/{tag_a}$", res.stdout, re.MULTILINE), res.stdout
    assert re.search(rf"^ +{re.escape(str(b))} -> /workspace/{tag_b}$", res.stdout, re.MULTILINE), res.stdout
    assert f"cd /workspace/{tag_a} && RUNPOD_RUN_SYNC_DIRS=/workspace/{tag_a}:/workspace/{tag_b} timeout 3600s make" \
        in res.stdout
    # Two dirs that land in one remote dir would rsync --delete over each other: the same dir
    # twice, or a worktree of the primary's clone (one repo_tag).
    subprocess.run(["git", "-C", str(a), "worktree", "add", "-q", "-b", "wt", str(tmp_path / "alpha-wt")], check=True)
    for dup in (a, tmp_path / "alpha-wt"):
        res = _run(["--dry-run", "--sync-dir", str(a), "--sync-dir", str(dup), "--", "make"], cwd=tmp_path, home=home)
        assert res.returncode == 2 and f"both sync to /workspace/{tag_a}" in res.stderr, (dup, res.stderr)
    res = _run(["--dry-run", "--sync-dir", str(a), "--sync-dir", str(tmp_path / "nope"), "--", "make"],
               cwd=tmp_path, home=home)
    assert res.returncode == 2 and "no such directory" in res.stderr


def _fake_remote(bindir: Path, log: Path) -> None:
    """ssh and rsync stand-ins first on PATH. ssh logs its arguments and swallows the pod-side
    script on stdin; rsync logs its source and destination, then its exclude list."""
    (bindir / "ssh").write_text(f'#!/bin/sh\nprintf "ssh %s\\n" "$*" >>"{log}"\ncat >/dev/null\n')
    (bindir / "rsync").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in --exclude-from=*) ex=${a#--exclude-from=} ;; esac; src=$dst; dst=$a; done\n'
        f'printf "rsync %s %s\\n" "$src" "$dst" >>"{log}"\n'
        f"tr '\\0' '\\n' <\"$ex\" | sed 's/^/  exclude /' >>\"{log}\"\n")
    for tool in ("ssh", "rsync"):
        (bindir / tool).chmod(0o755)


def test_every_sync_dir_is_synced_and_named_to_the_command(tmp_path: Path) -> None:
    a = _git_repo(tmp_path / "alpha", {"README.md": ""})
    b = _git_repo(tmp_path / "beta", {".gitignore": "secret.txt\n"})
    (b / "secret.txt").write_text("x")  # ignored in beta: it stays local
    c = tmp_path / "gamma"
    c.mkdir()
    home = tmp_path / "home"
    remote = [f"/workspace/{_repo_tag(a)}", f"/workspace/{_repo_tag(b)}",
              f"/workspace/gamma-{hashlib.sha256(str(c).encode()).hexdigest()[:8]}"]
    log = tmp_path / "remote.log"
    _account_env(home)  # creates home/bin, where the stand-ins go
    _fake_remote(home / "bin", log)
    _StubPods.reset([], ssh={"direct": {"host": "127.0.0.1", "port": 2222}})
    with _serve(_StubPods) as port:
        res = _pods_run(["--sync-dir", str(a), "--sync-dir", str(b), "--sync-dir", str(c), "--", "make"],
                        tmp_path, home, port)
    assert res.returncode == 0, res.stderr
    text = log.read_text()
    rsyncs = [chunk.splitlines() for chunk in text.split("rsync ")[1:]]
    assert [lines[0] for lines in rsyncs] == [f"{d}/ runner@127.0.0.1:{r}/" for d, r in zip((a, b, c), remote)], text
    assert "  exclude /secret.txt" in rsyncs[1] and "  exclude /secret.txt" not in rsyncs[0], text
    assert "  exclude .git" in rsyncs[2], text  # the fixed excludes hold outside git, too
    for d, r in zip((a, b, c), remote):
        assert f"runpod-run: syncing {d} -> runner@pod:{r}\n" in res.stderr
    # remote() and the run line %q-quote their words, each followed by a space.
    assert f"bash -s -- runner {' '.join(remote)} \n" in text  # every remote dir is made runner's
    assert f"runpod-run {remote[0]} {':'.join(remote)} 3600s make \n" in text  # cwd, RUNPOD_RUN_SYNC_DIRS
    # The further git checkout gets its own git snapshot (the pod-side DEPS script with no
    # install steps); the dir outside git gets none.
    assert f"bash -s -- {remote[1]} \n" in text and f"bash -s -- {remote[2]} \n" not in text, text
    assert _StubPods.deleted == ["new1"]
    [line] = _ledger(home)
    assert (line["event"], line["exit"]) == ("run", 0) and line["command_s"] is not None


class _StubRunpodLockProbe(_StubRunpod):
    """_StubRunpod whose DELETE also records whether the deleted pod's lock was held then."""

    lock_dir: ClassVar[Path] = Path()
    names: ClassVar[dict[str, str]] = {}
    locked_at_delete: ClassVar[list[bool]] = []

    def do_DELETE(self) -> None:  # noqa: N802
        with (self.lock_dir / f"{self.names[self.path.removeprefix('/v2/pods/')]}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.locked_at_delete.append(True)
            else:
                self.locked_at_delete.append(False)
        super().do_DELETE()


def test_the_reaper_leaves_a_pod_whose_lock_a_live_run_holds(tmp_path: Path) -> None:
    # A dead run's running.<pid> and an expired --ttl without a timer point at pods another
    # runpod-run on this machine now holds the lock of: neither may be terminated under it.
    host = _host_tag()
    a, b = f"rr-{host}-a-11111111", f"rr-{host}-b-22222222"
    _StubRunpod.pages = {"": {"pods": [_pod("p1", a, host), _pod("p2", b, host)], "pagination": {"hasNextPage": False}}}
    _StubRunpod.deleted = []
    home = tmp_path / "home"
    locks = home / "state" / "locks"
    locks.mkdir(parents=True)
    (home / "state" / "running.999999998").write_text("p1\n")
    (home / "state" / "p2.ttl").write_text("999999999 1\n")
    with _serve(_StubRunpod) as port, (locks / f"{a}.lock").open("w") as lock_a, (locks / f"{b}.lock").open("w") as lock_b:
        env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{port}/v2"}
        fcntl.flock(lock_a, fcntl.LOCK_EX)
        fcntl.flock(lock_b, fcntl.LOCK_EX)
        res = subprocess.run([str(_SCRIPT), "--status"], env=env, capture_output=True, text=True, timeout=60, check=False)
        assert res.returncode == 0, res.stderr
        assert _StubRunpod.deleted == [], res.stderr
        assert f"pod p1 ({a}) is in use by a runpod-run on this machine; not reaping it" in res.stderr
        assert f"pod p2 ({b}) is in use by a runpod-run on this machine; not reaping it" in res.stderr
    # The markers stay for the next reaper, which ends both pods once the locks are free, and
    # holds each pod's lock while it terminates it: no run can start on it in between.
    assert (home / "state" / "running.999999998").exists() and (home / "state" / "p2.ttl").exists()
    _StubRunpodLockProbe.lock_dir, _StubRunpodLockProbe.names = locks, {"p1": a, "p2": b}
    _StubRunpodLockProbe.locked_at_delete = []
    with _serve(_StubRunpodLockProbe) as port:
        env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{port}/v2"}
        res = subprocess.run([str(_SCRIPT), "--status"], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 0 and sorted(_StubRunpod.deleted) == ["p1", "p2"], res.stderr
    assert _StubRunpodLockProbe.locked_at_delete == [True, True]
    # Released again afterwards.
    for name in (a, b):
        with (locks / f"{name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_the_ttl_timers_own_stop_says_the_deadline_was_reached(tmp_path: Path) -> None:
    # The timer is `sleep N && exec runpod-run --stop <id>`: that --stop runs under the pid the
    # <id>.ttl file records. It is no "reboot": it ends the pod once, under the right words.
    host = _host_tag()
    name = f"rr-{host}-app-11111111"
    _StubRunpod.pages = {"": {"pods": [_pod("p1", name, host)], "pagination": {"hasNextPage": False}}}
    _StubRunpod.deleted = []
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    timer = f'echo "$$ {int(time.time()) - 1}" >"{state}/p1.ttl" && exec "{_SCRIPT}" --stop p1'
    with _serve(_StubRunpod) as port:
        env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{port}/v2"}
        res = subprocess.run(["bash", "-c", timer], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 0, res.stderr
    assert "runpod-run: pod p1 reached its --ttl deadline; terminating it\n" in res.stderr
    assert "reboot" not in res.stderr
    assert _StubRunpod.deleted == ["p1"]
    assert [line["event"] for line in _ledger(home)] == ["terminate"]


class _StubPodsCounting(_StubPods):
    """_StubPods that also counts every request: a dedup replay must make none."""

    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        type(self).requests.append(f"GET {self.path}")
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        type(self).requests.append(f"POST {self.path}")
        super().do_POST()


def test_an_identical_run_within_24_hours_replays_its_result_without_a_pod(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "dd", {"README.md": ""})
    home = tmp_path / "home"
    log = tmp_path / "remote.log"
    _account_env(home)
    _fake_remote(home / "bin", log)
    # The run line (the RUN script) prints 50 lines and exits 7; every other ssh call succeeds.
    (home / "bin" / "ssh").write_text(
        f'#!/bin/sh\nprintf "ssh %s\\n" "$*" >>"{log}"\ncat >/dev/null\n'
        'case "$*" in *" runpod-run /workspace/"*) seq 1 50; exit 7 ;; esac\n')
    _StubPodsCounting.reset([], ssh={"direct": {"host": "127.0.0.1", "port": 2222}})
    _StubPodsCounting.requests = []
    with _serve(_StubPodsCounting) as port:
        res = _pods_run(["--", "make"], repo, home, port)
        assert res.returncode == 7, res.stderr
        assert len(_StubPodsCounting.posts) == 1
        dry = _pods_run(["--dry-run", "--", "make"], repo, home, port)
        assert dry.returncode == 0, dry.stderr
        assert re.search(r"^  dedup: +hit \(would replay exit 7 from \d{4}-\d\d-\d\dT[\d:]+[+-]\d\d:\d\d\)$",
                         dry.stdout, re.MULTILINE), dry.stdout
        _StubPodsCounting.requests = []
        again = _pods_run(["--", "make"], repo, home, port)
        assert again.returncode == 7, again.stderr
        assert _StubPodsCounting.requests == [], _StubPodsCounting.requests  # no API call at all
        assert again.stdout == "".join(f"{n}\n" for n in range(11, 51))  # the stored log's last 40 lines
        assert re.search(r"runpod-run: identical to the run at \S+ \(exit 7\); no pod started — pass --rerun to "
                         r"run again\n", again.stderr), again.stderr
        assert not list((_account_state(home) / "slots").glob("*"))  # no slot taken
        # Another command is another run; --rerun skips the lookup.
        dry = _pods_run(["--dry-run", "--", "make", "lint"], repo, home, port)
        assert re.search(r"^  dedup: +miss$", dry.stdout, re.MULTILINE), dry.stdout
        dry = _pods_run(["--dry-run", "--timeout", "60", "--", "make"], repo, home, port)
        assert re.search(r"^  dedup: +miss$", dry.stdout, re.MULTILINE), dry.stdout  # other settings
        _StubPodsCounting.requests = []
        rerun = _pods_run(["--rerun", "--", "make"], repo, home, port)
        # (The stub keeps listing new1 after its DELETE, so the rerun reuses it: API calls, no POST.)
        assert rerun.returncode == 7 and _StubPodsCounting.requests, rerun.stderr
        assert "identical to the run" not in rerun.stderr
    assert [(x["event"], x["exit"]) for x in _ledger(home)] == [("run", 7), ("dedup", 7), ("run", 7)]


def test_a_reaper_pass_over_dead_pid_markers_prints_no_proc_errors(tmp_path: Path) -> None:
    # A marker whose pid is gone makes the reaper read /proc/<pid>/cmdline, which does not
    # exist: that read must stay silent (the redirect order decides whether bash reports it).
    host = _host_tag()
    _StubRunpod.pages = {"": {"pods": [_pod("p1", f"rr-{host}-a-11111111", host)],
                              "pagination": {"hasNextPage": False}}}
    _StubRunpod.deleted = []
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    (state / "p1.ttl").write_text(f"999999999 {2**31}\n")  # dead timer, deadline ahead
    (state / "running.999999998").write_text("p9\n")  # dead run, pod not listed
    with _serve(_StubRunpod) as port:
        env = {**_account_env(home), "RUNPOD_API_KEY": "k", "RUNPOD_API_URL": f"http://127.0.0.1:{port}/v2"}
        res = subprocess.run([str(_SCRIPT), "--status"], env=env, capture_output=True, text=True, timeout=60,
                             check=False)
    assert res.returncode == 0, res.stderr
    assert "No such file" not in res.stderr and "/proc/" not in res.stderr, res.stderr
    # Both markers were judged: the dead run's marker is consumed, the live-deadline one kept.
    assert not (state / "running.999999998").exists() and (state / "p1.ttl").exists()


def test_an_orphaned_run_books_its_ledger_line_without_proc_errors(tmp_path: Path) -> None:
    # $PPID is fixed at startup: when the launching shell exits while the run waits (here on
    # its per-pod lock), parent_cmdline later reads a /proc entry that is gone, silently.
    repo = _git_repo(tmp_path / "orph", {"README.md": ""})
    home = tmp_path / "home"
    err = tmp_path / "err"
    locks = home / "state" / "locks"
    locks.mkdir(parents=True)
    _StubPods.reset([(400, {"error": "Container Disk must be <= 20"})])  # the create fails: exit 3
    with _serve(_StubPods) as port, (locks / f"rr-{_host_tag()}-orphan.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        launcher = subprocess.Popen(["bash", "-c", f'"{_SCRIPT}" --name orphan -- make 2>"{err}" </dev/null &\n'
                                    f'until grep -q "waiting for it" "{err}" 2>/dev/null; do sleep 0.05; done'],
                                    cwd=repo, env=_budget_env(home, port))
        launcher.wait(timeout=30)  # the launcher is gone: the run's $PPID names no process now
        fcntl.flock(lock, fcntl.LOCK_UN)
        _until(lambda: _ledger(home))
    assert _ledger(home)[0]["exit"] == 3
    assert "/proc/" not in err.read_text(), err.read_text()


class _StubPodsEditingTheTree(_StubPods):
    """_StubPods whose POST /pods also rewrites a file of the checkout: the tree changes while
    the run waits for its pod, after the launch-time dedup key and before the sync."""

    edit: ClassVar[tuple[Path, str]] = (Path(), "")

    def do_POST(self) -> None:  # noqa: N802
        path, body = self.edit
        path.write_text(body)
        super().do_POST()


def test_a_run_records_its_result_under_the_key_of_the_tree_it_synced(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "dk", {"README.md": "old\n"})
    home = tmp_path / "home"
    log = tmp_path / "remote.log"
    _account_env(home)
    _fake_remote(home / "bin", log)
    (home / "bin" / "ssh").write_text(
        f'#!/bin/sh\nprintf "ssh %s\\n" "$*" >>"{log}"\ncat >/dev/null\n'
        'case "$*" in *" runpod-run /workspace/"*) exit 7 ;; esac\n')
    _StubPodsEditingTheTree.reset([], ssh={"direct": {"host": "127.0.0.1", "port": 2222}})
    _StubPodsEditingTheTree.edit = (repo / "README.md", "new\n")
    with _serve(_StubPodsEditingTheTree) as port:
        res = _pods_run(["--", "make"], repo, home, port)
        assert res.returncode == 7, res.stderr
        assert "the checkout changed while this run waited" in res.stderr
        # The synced (new) tree replays; the launch-time (old) tree never ran, so it does not.
        dry = _pods_run(["--dry-run", "--", "make"], repo, home, port)
        assert re.search(r"^  dedup: +hit \(would replay exit 7 ", dry.stdout, re.MULTILINE), dry.stdout
        (repo / "README.md").write_text("old\n")
        dry = _pods_run(["--dry-run", "--", "make"], repo, home, port)
        assert re.search(r"^  dedup: +miss$", dry.stdout, re.MULTILINE), dry.stdout
        # A file changed while rsync reads the tree: the pod may hold either version, so the
        # result is recorded under neither key. (The stub still lists new1: reused, no POST.)
        rsync = home / "bin" / "rsync"
        rsync.write_text(rsync.read_text() + f'printf "mixed\\n" >"{repo / "README.md"}"\n')
        res = _pods_run(["--", "make"], repo, home, port)
        assert res.returncode == 7 and "the checkout changed during the sync" in res.stderr, res.stderr
        for body in ("mixed\n", "old\n"):
            (repo / "README.md").write_text(body)
            dry = _pods_run(["--dry-run", "--", "make"], repo, home, port)
            assert re.search(r"^  dedup: +miss$", dry.stdout, re.MULTILINE), (body, dry.stdout)


def _waits_on_the_budget_lock(home: Path) -> bool:
    """A run under _account_env(home) is blocked in `flock -w 300` on the budget lock (so past
    its reaper)."""
    mark = f"RUNPOD_RUN_STATE={home / 'state'}\0".encode()
    for proc in Path("/proc").iterdir():
        try:
            if (proc / "cmdline").read_bytes().startswith(b"flock\x00-w\x00300\x00") and mark in (proc / "environ").read_bytes():
                return True
        except OSError:
            continue
    return False


def test_a_run_that_ends_before_its_kept_pod_rearms_the_timer_that_skipped_it(tmp_path: Path) -> None:
    # The kept pod's --ttl timer fired while this run held the pod lock, so it skipped the pod
    # and died (its <id>.ttl names a dead pid). The run is then refused by the budget before it
    # took the pod over: it re-arms the timer for what is left of the deadline.
    repo = _git_repo(tmp_path / "ra", {"README.md": ""})
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    name = _app_pod()
    deadline = int(time.time()) + 300
    (state / "pk.ttl").write_text(f"999999999 {deadline}\n")
    (state / "pk.lastrun").write_text(json.dumps({"ended": int(time.time()) - 60, "pod_name": name}))
    _StubBudget.pods = []
    res = _budget_run(["--", "make"], repo, home)
    assert res.returncode == 21, res.stderr
    assert "pod pk's --ttl timer fired while this run held it; re-arming it" in res.stderr
    pid_s, deadline_s = (state / "pk.ttl").read_text().split()
    timer = int(pid_s)
    try:
        assert abs(int(deadline_s) - deadline) <= 2  # the old deadline, not a fresh --ttl
        assert "--stop pk" in Path(f"/proc/{timer}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        assert (_account_state(home) / "slots" / name).read_text().split() == [pid_s]  # the timer holds the slot
        assert _StubBudget.mutations == []
    finally:
        os.kill(timer, signal.SIGTERM)
    # The deadline passes while the run waits on the budget lock, after its reaper looked:
    # the run terminates the pod the timer would have ended — once the API lists it as this
    # host's (a pod it does not list is never deleted on a state file's word).
    (state / "pk.ttl").write_text(f"999999999 {2**31}\n")
    _StubBudget.pods = [{"id": "pk", "name": f"{name}-kept", "status": "RUNNING", "cost": 0.16,
                         "cpu": {"id": "cpu3g", "vcpuCount": 4}, "env": {"RUNPOD_RUN_OWNER": _host_tag()}}]
    _StubBudget.mutations = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubBudget)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with (_account_state(home) / "budget.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.Popen([str(_SCRIPT), "--name", "app", "--", "make"], cwd=repo,
                                    env=_budget_env(home, server.server_port), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            _until(lambda: _waits_on_the_budget_lock(home))
            (state / "pk.ttl").write_text("999999999 1\n")
            fcntl.flock(lock, fcntl.LOCK_UN)
        _, err = proc.communicate(timeout=60)
    finally:
        server.shutdown()
    assert proc.returncode == 21, err
    assert "pod pk passed its --ttl deadline while this run held it; terminating it" in err
    assert _StubBudget.mutations == ["DELETE /v2/pods/pk"], err
    assert [x["event"] for x in _ledger(home)][-1] == "terminate"
