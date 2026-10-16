"""Tests for `runpod_run_policy.py`, the stdlib policy helper that ships next to `runpod-run`
in the `runpod-offload` skill (agent-tools#910): which commands are refused before any pod
is rented (a single test file, a no-op, an unreasoned big flavor or GPU), the per-run ledger
line, the daily `--report` built from it, the daily $ budget (against a local fake
billing API, never the real one), and the host's 3 pod slots with their FIFO queue.

The pure functions are exercised directly; the CLI is exercised the way the bash script
calls it. Ledgers and run logs are synthetic files under tmp_path, never the real cache.
The slot tests stand a fake /proc (`proc_root`) in for the pids they name; the CLI tests,
which read the real /proc, hold slots with real processes whose command line names
runpod-run.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_runpod_run_policy.py -q
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "skills" / "universal" / "runpod-offload" / "runpod_run_policy.py"
_spec = importlib.util.spec_from_file_location("runpod_run_policy", _HELPER)
assert _spec and _spec.loader
policy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = policy
_spec.loader.exec_module(policy)


@pytest.mark.parametrize(
    ("cmd", "trivial"),
    [
        ("bun test tests/x.test.ts", True),
        ("uv run --with pytest pytest tests/test_a.py -q", True),
        ("uv run --with 'pytest>=8,<9' --with pyyaml python -m pytest tests/test_a.py -q", True),
        ("pytest -k foo", True),
        ("python -u -m pytest tests/a.py", True),
        ("python3 -W error -X dev -m pytest tests/a.py", True),
        ("python -mpytest tests/a.py", True),
        ("npx --package vitest vitest run src/a.test.ts", True),
        ("uvx --from pytest pytest tests/a.py", True),
        ("python -u -m pytest tests", False),
        ("python script.py tests/a.py", False),
        ("pnpm exec --package=vitest vitest run tests/a.test.ts", True),
        ("pnpm exec --filter web vitest run tests/a.test.ts", True),
        ("env CI=1 pytest tests/a.py", True),
        ("env -u HOME CI=1 pytest tests/a.py", True),
        ("sh -c 'pytest tests/a.py'", True),
        ("bash -lc 'uv run pytest tests/a.py -q'", True),
        ("sh -c 'pytest tests/a.py; pytest tests/b.py'", False),
        ("sh -c 'pytest $FILES'", False),
        ("sh script.sh tests/a.py", False),
        ("env CI=1 pytest tests", False),
        ("poetry run -- pytest tests/a.py", True),
        ("pytest -kfoo", True),
        ("bun test -tfoo", True),
        ("pytest -k foo tests", False),
        ("env -S 'pytest tests/a.py'", True),
        ("env -i -S'CI=1 pytest tests/a.py'", True),
        ("env -S 'pytest tests'", False),
        ("sh -c -- 'pytest tests/a.py'", True),
        ("sh -c 'exec pytest tests/a.py'", True),
        ("command pytest tests/a.py", True),
        ("sh -c 'pytest tests/a.py # one file'", True),
        ("command -v pytest", False),
        ("pytest --capture no tests/a.py", True),
        ("pytest --color yes -x tests/a.py", True),
        ("pytest tests/test_a.py::test_one", True),
        ("poetry run pytest tests/test_a.py", True),
        ("npx vitest run src/a.test.ts", True),
        ("bunx jest -t 'renders'", True),
        ("pnpm exec jest src/a.test.ts", True),
        ("go test -run TestFoo", True),
        ("pytest tests/", False),
        ("pytest tests", False),
        ("pytest tests/a.py tests/b.py", False),
        ("go test ./...", False),
        ("go test -run TestFoo ./pkg/...", False),
        ("bun test", False),
        ("bun test ./tests", False),
        ("pytest -q", False),
        ("make test", False),
        ("pytest tests/a.py && pytest tests/b.py", False),
    ],
)
def test_trivial_single_file(cmd: str, trivial: bool, tmp_path: Path) -> None:
    assert policy.classify_trivial(shlex.split(cmd), str(tmp_path)) is trivial


def test_trivial_sees_an_existing_directory_operand(tmp_path: Path) -> None:
    # `pytest unit` is a directory when the checkout has one: a suite, not one file.
    (tmp_path / "unit.py").mkdir()
    assert policy.classify_trivial(["pytest", "unit.py"], str(tmp_path)) is False


def test_trivial_accepts_a_single_shell_string(tmp_path: Path) -> None:
    assert policy.classify_trivial(["bun test tests/x.test.ts"], str(tmp_path)) is True


@pytest.mark.parametrize(
    ("argv", "noop"),
    [
        (["true"], True),
        ([":"], True),
        (["exit", "0"], True),
        (["exit 0"], True),
        (["/bin/true"], True),
        (["/usr/bin/true"], True),
        (["true", "&&", "make"], False),
        (["true && make"], False),
        (["make"], False),
        (["exit", "1"], False),
        (["sh", "-c", "true"], True),
        (["bash", "-c", "exit 0"], True),
        (["env", "X=1", "/bin/true"], True),
        (["sh", "-c", "true; make"], False),
        (["sh", "script.sh"], False),
        (["env", "-S", "true"], True),
        (["env", "-S", "X=1 /bin/true"], True),
        (["env", "--split-string=true"], True),
        (["bash", "-c", "--", "true"], True),
        (["sh", "-c", "exit 0 # comment"], True),
        (["exec", "true"], True),
    ],
)
def test_noop(argv: list[str], noop: bool) -> None:
    assert policy.is_noop(argv) is noop


@pytest.mark.parametrize(
    ("cmd", "cuda"),
    [
        ("python train.py --device cuda", True),
        ("uv run python -c 'import torch'", True),
        ("nvcc kernel.cu -o k", True),
        ("bun test x.ts", False),
        ("pytest tests -q", False),
    ],
)
def test_needs_cuda(cmd: str, cuda: bool) -> None:
    assert policy.needs_cuda(shlex.split(cmd)) is cuda


@pytest.mark.parametrize(
    ("spec", "needs"),
    [
        ("cpu3g-4", False),
        ("cpu3c-4", False),
        ("cpu3m-2", False),
        ("cpu3c-2", False),
        ("cpu3m-4", True),
        ("cpu3c-8", True),
        ("cpu3g-8", True),
        ("gpu:NVIDIA GeForce RTX 4090", True),
    ],
)
def test_flavor_reason(spec: str, needs: bool) -> None:
    assert policy.flavor_needs_reason(spec) is needs


def _check(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HELPER), "check", "--cwd", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_check_cli_ok_and_refusals(tmp_path: Path) -> None:
    ok = _check("--shape", "cpu3g-4", "--", "bun", "test", "./tests", cwd=tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert ok.stderr == ""

    trivial = _check("--shape", "cpu3g-4", "--", "bun", "test", "tests/x.test.ts", cwd=tmp_path)
    assert trivial.returncode == 20
    assert "run it locally: one test file does not need a pod (heavy-op-memory-gate allows 1-2 files)" in trivial.stderr
    excused = _check("--shape", "cpu3g-4", "--remote-reason", "flaky only on linux", "--",
                     "bun", "test", "tests/x.test.ts", cwd=tmp_path)
    assert excused.returncode == 0, excused.stderr

    noop = _check("--shape", "cpu3g-4", "--remote-reason", "x", "--", "true", cwd=tmp_path)
    assert noop.returncode == 20 and "no-op" in noop.stderr

    big = _check("--shape", "cpu3c-8", "--", "pytest", "tests", cwd=tmp_path)
    assert big.returncode == 20 and "--reason" in big.stderr
    big_ok = _check("--shape", "cpu3c-8", "--reason", "suite parallelizes over 8 cores", "--", "pytest", "tests", cwd=tmp_path)
    assert big_ok.returncode == 0, big_ok.stderr
    blank = _check("--shape", "cpu3c-8", "--reason", "  ", "--", "pytest", "tests", cwd=tmp_path)
    assert blank.returncode == 20


def test_check_cli_gpu_needs_reason_and_a_cuda_command(tmp_path: Path) -> None:
    gpu = "gpu:NVIDIA GeForce RTX 4090"
    no_reason = _check("--shape", gpu, "--", "python", "train.py", "--device", "cuda", cwd=tmp_path)
    assert no_reason.returncode == 20 and "--reason" in no_reason.stderr
    ok = _check("--shape", gpu, "--reason", "fine-tune", "--", "python", "train.py", "--device", "cuda", cwd=tmp_path)
    assert ok.returncode == 0, ok.stderr
    not_cuda = _check("--shape", gpu, "--reason", "fast", "--", "bun", "test", "./tests", cwd=tmp_path)
    assert not_cuda.returncode == 20 and "CUDA" in not_cuda.stderr
    said_gpu = _check("--shape", gpu, "--reason", "bench needs the GPU path", "--", "python", "bench.py", cwd=tmp_path)
    assert said_gpu.returncode == 0, said_gpu.stderr


# --- ledger + report (R8) ---------------------------------------------------------------------


@pytest.fixture
def berlin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Local time = CEST, so "the local day" and the +02:00 offsets below mean what they say."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_ledger_append_creates_parents_and_appends_one_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "state" / "ledger.jsonl"
    policy.ledger_append(path, {"event": "run", "cmd": "bun test ./tests"})
    policy.ledger_append(path, {"event": "refused", "cmd": "true"})
    lines = path.read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["run", "refused"]


def test_ledger_record_fills_ts_identity_and_estimate(berlin: None) -> None:
    env = {"RIG_AGENT_ID": "Main.Worker", "OMP_SESSION_ID": "", "OMP_SESSION_DIR": "",
           "CLAUDE_SESSION_ID": "claude-1", "CODEX_SESSION_ID": "codex-1"}
    now = datetime(2026, 10, 16, 1, 30).astimezone()
    rec = policy.ledger_record(
        {"event": "run", "usd_per_h": 0.36, "boot_s": 100, "sync_s": 50, "bootstrap_s": 50,
         "command_s": 1000, "idle_s": None, "exit": 0, "reason": "", "parent": "x" * 300},
        env, now)
    assert list(rec) == list(policy.LEDGER_FIELDS)
    assert rec["ts"] == "2026-10-16T01:30:00+02:00"
    assert rec["agent"] == "Main.Worker"
    assert rec["session"] == "claude-1"  # the first NON-EMPTY of the session variables
    assert rec["est_usd"] == pytest.approx(0.12)  # $0.36/h x 1200 s
    assert rec["reason"] is None  # empty strings are "not given"
    assert len(rec["parent"]) == 200
    unpriced = policy.ledger_record({"event": "refused"}, {}, now)
    assert unpriced["est_usd"] is None and unpriced["agent"] is None
    with pytest.raises(ValueError, match="event"):
        policy.ledger_record({"event": "bogus"}, {}, now)


def _rec(ts: str, event: str, **fields: object) -> str:
    base = {"ts": ts, "event": event, "repo": "app", "repo_tag": "app-11111111", "agent": "A1",
            "usd_per_h": 0.36}
    return json.dumps({**base, **fields})


def test_report_splits_the_ledger_dollars_by_category(tmp_path: Path, berlin: None) -> None:
    # $0.36/h = $0.0001/s, so every figure below is seconds / 10000.
    day = "2026-10-15T12:00:00+02:00"
    lines = [
        _rec(day, "run", exit=0, boot_s=100, sync_s=50, bootstrap_s=50, command_s=1000),  # A .1  B .02
        _rec(day, "run", exit=1, boot_s=80, sync_s=0, bootstrap_s=0, command_s=20,  # A .002 B .008, trivial
             repo="lib", repo_tag="lib-2", agent="A2"),
        _rec(day, "run", exit=3, boot_s=300, sync_s=None, bootstrap_s=None, command_s=None),  # D .03
        _rec(day, "run", exit=124, boot_s=100, sync_s=0, bootstrap_s=0, command_s=3600),  # E .36  B .01
        _rec(day, "run", exit=0, boot_s=0, sync_s=0, bootstrap_s=0, command_s=100, idle_s=120),  # A .01 C .012
        _rec(day, "terminate", idle_s=600, agent=None),  # C .06
        _rec(day, "refused", exit=20, usd_per_h=None),
        _rec(day, "dedup", exit=0, usd_per_h=None),
        _rec(day, "queue-timeout", exit=22, usd_per_h=None),
        _rec(day, "budget", exit=21, usd_per_h=None),
        # 23:30 UTC on the 14th is 01:30 CEST on the 15th: it belongs to the 15th.
        _rec("2026-10-14T23:30:00+00:00", "run", exit=0, boot_s=0, sync_s=0, bootstrap_s=0, command_s=1000),  # A .1
        _rec("2026-10-14T23:30:00+02:00", "run", exit=0, command_s=99999),  # the 14th: not counted
        "{not json",
    ]
    (tmp_path / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    out = policy.report(tmp_path, date(2026, 10, 15), 1.0)
    assert "billed:   $1.000" in out
    expected = {
        "A command, exit 0/1": "$0.212",
        "B boot+sync+bootstrap": "$0.038",
        "C idle": "$0.072",
        "D failed before the command": "$0.030",
        "E failed run, other exits": "$0.360",
        "G unattributed": "$0.288",
    }
    for label, amount in expected.items():
        row = next((line for line in out.splitlines() if line.strip().startswith(label)), None)
        assert row is not None and row.rstrip().endswith(amount), (label, out)
    assert "ledger:   $0.712" in out
    assert "runs: 6" in out
    assert "per repo: app 5, lib 1" in out
    assert "per agent: A1 5, A2 1" in out
    assert "trivial (<30 s command): 1" in out
    assert "refused 1, dedup 1, queue-timeout 1, budget 1, terminate 1" in out


def test_report_without_billing_leaves_g_unknown(tmp_path: Path, berlin: None) -> None:
    (tmp_path / "ledger.jsonl").write_text(_rec("2026-10-15T09:00:00+02:00", "run", exit=0, command_s=36) + "\n")
    out = policy.report(tmp_path, date(2026, 10, 15), None)
    assert "billed:   unknown" in out
    g_row = next(line for line in out.splitlines() if "G unattributed" in line)
    assert "unknown" in g_row


def test_report_falls_back_to_legacy_run_logs(tmp_path: Path, berlin: None) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    logs = {  # name timestamp (local) -> seconds until the log's mtime
        "20261015-101500-app-11111111.log": 600,
        "20261015-120000-lib-22222222.log": 1800,
        "20261015-230000-app-11111111.log": 60,
        "20261014-235900-app-11111111.log": 5000,  # another day
    }
    for name, seconds in logs.items():
        path = runs / name
        path.write_text("output\n")
        start = datetime.strptime(name[:15], "%Y%m%d-%H%M%S").timestamp()
        os.utime(path, (start + seconds, start + seconds))
    out = policy.report(tmp_path, date(2026, 10, 15), 2.0)
    assert "legacy: 3 runs from run logs (no ledger) — categories B-E unavailable" in out
    assert "$0.109" in out  # 2460 s at $0.16/h
    g_row = next(line for line in out.splitlines() if "G unattributed" in line)
    assert g_row.rstrip().endswith("$1.891"), out
    assert "per repo: app-11111111 2, lib-22222222 1" in out


def test_report_ignores_non_finite_figures(tmp_path: Path, berlin: None) -> None:
    # Python's json reads a hand-edited NaN; one such line must not turn every total into $nan.
    good = _rec("2026-10-15T09:00:00+02:00", "run", exit=0, command_s=100)
    bad = '{"ts": "2026-10-15T10:00:00+02:00", "event": "run", "exit": 0, "usd_per_h": NaN, "command_s": 60}'
    (tmp_path / "ledger.jsonl").write_text(f"{good}\n{bad}\n")
    out = policy.report(tmp_path, date(2026, 10, 15), 1.0)
    assert "nan" not in out.lower()
    assert "ledger:   $0.010" in out


def test_report_of_an_empty_day_says_so(tmp_path: Path, berlin: None) -> None:
    out = policy.report(tmp_path, date(2026, 10, 15), None)
    assert "no runs recorded" in out


def _helper(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_HELPER), *args], capture_output=True, text=True,
                          timeout=30, check=False, env=env)


def test_ledger_append_and_report_cli(tmp_path: Path) -> None:
    env = {**os.environ, "RIG_AGENT_ID": "cli-agent", "TZ": "Europe/Berlin"}
    rec = json.dumps({"ts": "2026-10-15T10:00:00+02:00", "event": "run", "exit": 0, "usd_per_h": 0.36,
                      "boot_s": 10, "sync_s": 10, "bootstrap_s": 10, "command_s": 70, "repo": "app"})
    res = _helper("ledger-append", f"--state={tmp_path}", f"--json={rec}", env=env)
    assert res.returncode == 0, res.stderr
    line = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert line["agent"] == "cli-agent" and line["est_usd"] == pytest.approx(0.01)
    bad = _helper("ledger-append", f"--state={tmp_path}", "--json={\"event\": \"nope\"}", env=env)
    assert bad.returncode == 2 and "event" in bad.stderr
    rep = _helper("report", f"--state={tmp_path}", "--day=2026-10-15", "--billed=0.5", env=env)
    assert rep.returncode == 0, rep.stderr
    assert "$0.500" in rep.stdout and "$0.007" in rep.stdout  # A = 70 s at $0.36/h
    bad_day = _helper("report", f"--state={tmp_path}", "--day=15.10.2026", env=env)
    assert bad_day.returncode == 2


# --- daily budget (R3) ------------------------------------------------------------------------

_CEST = timezone(timedelta(hours=2))


class _FakeBilling(http.server.BaseHTTPRequestHandler):
    """GET /billing: hourly records whose totalAmount values are `amounts`; counts every call."""

    amounts: ClassVar[list[float | None]] = []  # None: a record without a figure
    queries: ClassVar[list[dict[str, list[str]]]] = []
    status: ClassVar[int] = 200

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        assert path == "/v2/billing", path
        if self.headers["Authorization"] != "Bearer k":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.queries.append(urllib.parse.parse_qs(query))
        data = json.dumps({"metadata": {}, "records": [{"totalAmount": a} for a in self.amounts]}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def billing() -> Iterator[str]:
    """A local billing API answering $5.90 over three hourly records; yields its base URL."""
    _FakeBilling.amounts = [2.5, 2.4, 1.0]
    _FakeBilling.queries = []
    _FakeBilling.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeBilling)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v2"
    server.shutdown()


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v2"  # nothing listens there any more: connection refused


def test_local_day_window_starts_at_local_midnight_in_utc(berlin: None) -> None:
    now = datetime(2026, 10, 16, 1, 30, tzinfo=_CEST)
    assert policy.local_day_window(now) == ("2026-10-15T22:00:00Z", "2026-10-15T23:30:00Z")


def test_daily_cap_default_file_and_dated_override(tmp_path: Path) -> None:
    assert policy.daily_cap(tmp_path / "absent", date(2026, 10, 16)) == 6.0
    (tmp_path / "budget").write_text("# owner-set\ndaily_usd = 4.5\n")
    assert policy.daily_cap(tmp_path, date(2026, 10, 16)) == 4.5
    (tmp_path / "budget-override").write_text("2026-10-16 9\n")
    assert policy.daily_cap(tmp_path, date(2026, 10, 16)) == 9.0
    assert policy.daily_cap(tmp_path, date(2026, 10, 17)) == 4.5  # an override is for its day only
    (tmp_path / "budget").write_text("daily_usd = lots\n")
    assert policy.daily_cap(tmp_path, date(2026, 10, 17)) == 6.0  # unreadable value -> the default


def test_billed_today_sums_the_hourly_records_and_caches_them(tmp_path: Path, billing: str, berlin: None) -> None:
    now = datetime(2026, 10, 16, 1, 30, tzinfo=_CEST)
    assert policy.billed_today(billing, "k", tmp_path, now) == pytest.approx(5.9)
    assert _FakeBilling.queries == [{"bucketSize": ["hour"], "startTime": ["2026-10-15T22:00:00Z"],
                                     "endTime": ["2026-10-15T23:30:00Z"]}]
    _FakeBilling.amounts = [9.0]
    # Within 5 minutes: the cache answers, no HTTP call.
    assert policy.billed_today(billing, "k", tmp_path, now + timedelta(minutes=4)) == pytest.approx(5.9)
    assert len(_FakeBilling.queries) == 1
    # Older than 5 minutes: read again.
    assert policy.billed_today(billing, "k", tmp_path, now + timedelta(minutes=6)) == pytest.approx(9.0)
    assert len(_FakeBilling.queries) == 2
    # The cache is keyed by the local day: a new day never reuses yesterday's figure.
    _FakeBilling.amounts = [0.25]
    tomorrow = datetime(2026, 10, 17, 0, 1, tzinfo=_CEST)
    assert policy.billed_today(billing, "k", tmp_path, tomorrow) == pytest.approx(0.25)
    assert _FakeBilling.queries[-1]["startTime"] == ["2026-10-16T22:00:00Z"]


def test_billed_of_a_past_day_is_never_cached(tmp_path: Path, billing: str, berlin: None) -> None:
    now = datetime(2026, 10, 16, 9, 0, tzinfo=_CEST)
    assert policy.billed_usd(billing, "k", tmp_path, date(2026, 10, 15), now) == pytest.approx(5.9)
    assert _FakeBilling.queries == [{"bucketSize": ["hour"], "startTime": ["2026-10-14T22:00:00Z"],
                                     "endTime": ["2026-10-15T22:00:00Z"]}]
    assert not (tmp_path / "billing-today.json").exists()


def test_billing_errors_raise(tmp_path: Path, billing: str, berlin: None) -> None:
    now = datetime(2026, 10, 16, 9, 0, tzinfo=_CEST)
    with pytest.raises(policy.BillingUnreadable):
        policy.billed_today(_closed_port_url(), "k", tmp_path, now)
    _FakeBilling.status = 500
    with pytest.raises(policy.BillingUnreadable):
        policy.billed_today(billing, "k", tmp_path, now)


def _budget(api: str, state: Path, price: str, live: list[dict[str, object]], *, config: Path,
            key: str = "k", timeout_s: str = "3600") -> subprocess.CompletedProcess[str]:
    live_file = state / "live.jsonl"
    state.mkdir(parents=True, exist_ok=True)
    live_file.write_text("".join(json.dumps(p) + "\n" for p in live))
    # The owner's files come through --config-dir (what runpod-run resolves from passwd); the
    # environment points elsewhere, at a decoy that would lift the cap if it were read.
    decoy = state / "decoy"
    (decoy / "runpod-run").mkdir(parents=True, exist_ok=True)
    (decoy / "runpod-run" / "budget").write_text("daily_usd = 99\n")
    env = {**os.environ, "RUNPOD_API_KEY": key, "XDG_CONFIG_HOME": str(decoy), "HOME": str(decoy),
           "TZ": "Europe/Berlin"}
    return _helper("budget", f"--api={api}", f"--state={state}", f"--price={price}", f"--timeout-s={timeout_s}",
                   f"--live-json={live_file}", f"--config-dir={config / 'runpod-run'}", env=env)


def test_budget_cli_refuses_when_billed_plus_this_run_reaches_the_cap(tmp_path: Path, billing: str) -> None:
    # $5.90 billed + a new $0.16/h pod for its 1 h --timeout = $6.06 >= the default $6 cap.
    res = _budget(billing, tmp_path / "s1", "0.16", [], config=tmp_path / "cfg")
    assert res.returncode == 21, (res.stdout, res.stderr)
    assert "$5.900 billed + $0.000 live/committed + $0.160 this run = $6.060 of $6.000 cap → REFUSE" in res.stdout
    assert ('escalate to the owner: send a decision request with tg --tag decision --title "runpod-run daily'
            ' budget reached" (decision-request format: tg help format) — today $5.900 billed, $6.060 with this'
            " run, of the $6.000 cap; the owner raises the cap by writing ~/.config/runpod-run/budget-override"
            ) in res.stderr
    # Live pods count at their $/h for this run's --timeout; EXITED and TERMINATED ones do not.
    live = [{"id": "a", "status": "RUNNING", "cost": 0.03}, {"id": "b", "status": "EXITED", "cost": 9},
            {"id": "c", "status": "TERMINATED", "cost": 9}]
    res = _budget(billing, tmp_path / "s2", "0.04", live, config=tmp_path / "cfg", timeout_s="1800")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "$5.900 billed + $0.015 live/committed + $0.020 this run = $5.935 of $6.000 cap → ok" in res.stdout
    # The owner's override for today lifts the cap; no environment variable can.
    (tmp_path / "cfg" / "runpod-run").mkdir(parents=True)
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()  # the helper runs with TZ=Europe/Berlin
    (tmp_path / "cfg" / "runpod-run" / "budget-override").write_text(f"{today.isoformat()} 9\n")
    res = _budget(billing, tmp_path / "s3", "0.16", [], config=tmp_path / "cfg")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "of $9.000 cap → ok" in res.stdout


def test_budget_cli_fails_closed(tmp_path: Path, billing: str) -> None:
    res = _budget(_closed_port_url(), tmp_path / "s1", "0.16", [], config=tmp_path / "cfg")
    assert res.returncode == 21 and "billing" in res.stderr and "tg --tag decision" in res.stderr
    assert "→ REFUSE" in res.stdout
    res = _budget(billing, tmp_path / "s2", "0.16", [], config=tmp_path / "cfg", key="")
    assert res.returncode == 21 and "API key" in res.stderr
    assert _FakeBilling.queries == []  # no key, no call
    res = _budget(billing, tmp_path / "s3", "", [], config=tmp_path / "cfg")
    assert res.returncode == 21 and "price" in res.stderr and "tg --tag decision" in res.stderr


def test_budget_cli_uses_the_cache_without_a_call(tmp_path: Path, billing: str) -> None:
    state = tmp_path / "s"
    first = _budget(billing, state, "0.01", [], config=tmp_path / "cfg")
    assert first.returncode == 0, first.stderr
    second = _budget(billing, state, "0.01", [], config=tmp_path / "cfg")
    assert second.returncode == 0, second.stderr
    assert len(_FakeBilling.queries) == 1


def test_report_cli_reads_billing_with_the_key_from_the_environment(tmp_path: Path, billing: str) -> None:
    env = {**os.environ, "RUNPOD_API_KEY": "k", "TZ": "Europe/Berlin"}
    rep = _helper("report", f"--state={tmp_path}", "--day=2024-07-15", f"--api={billing}", env=env)
    assert rep.returncode == 0, rep.stderr
    assert "billed:   $5.900" in rep.stdout
    assert _FakeBilling.queries[0]["startTime"] == ["2024-07-14T22:00:00Z"]
    down = _helper("report", f"--state={tmp_path}", "--day=2024-07-15", f"--api={_closed_port_url()}", env=env)
    assert down.returncode == 0 and "billed:   unknown" in down.stdout
    assert "billing API could not be read" in down.stderr


def test_budget_cli_refuses_a_live_pod_without_a_price(tmp_path: Path, billing: str) -> None:
    # A live pod whose $/h cannot be read may be what breaks the cap: never count it as free.
    live = [{"id": "x", "status": "RUNNING", "cost": None}]
    res = _budget(billing, tmp_path / "s", "0.01", live, config=tmp_path / "cfg")
    assert res.returncode == 21 and "'x'" in res.stderr and "tg --tag decision" in res.stderr


def test_billing_cache_is_scoped_to_the_api_key(tmp_path: Path, billing: str, berlin: None) -> None:
    now = datetime(2026, 10, 16, 9, 0, tzinfo=_CEST)
    assert policy.billed_today(billing, "k", tmp_path, now) == pytest.approx(5.9)
    _FakeBilling.amounts = [1.0]
    with pytest.raises(policy.BillingUnreadable):  # another key: the fake rejects it, no cache reuse
        policy.billed_today(billing, "other-account", tmp_path, now)
    assert len(_FakeBilling.queries) == 1


def test_the_budget_files_and_account_state_live_in_the_account_home_whatever_the_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An agent can set HOME or XDG_CONFIG_HOME; the cap's files must not move with them, nor
    # the home the script keeps the ledger and the pod slots in (`account-home`).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert policy.account_home() == home and not str(home).startswith(str(tmp_path))
    assert policy.account_config_dir() == home / ".config" / "runpod-run"
    # The CLI runs unpatched: its oracle is the real passwd entry, read in a clean interpreter.
    real = subprocess.run([sys.executable, "-c", "import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)"],
                          capture_output=True, text=True, check=True).stdout
    res = _helper("account-home", env={**os.environ, "HOME": str(tmp_path)})
    assert (res.returncode, res.stdout) == (0, real) and real.strip() != str(tmp_path), res.stderr


def test_account_home_without_a_passwd_entry_fails(monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    def no_entry(uid: int) -> object:
        raise KeyError(uid)

    monkeypatch.setattr(policy.pwd, "getpwuid", no_entry)
    assert policy.main(["account-home"]) == 2 and "no passwd entry" in capsys.readouterr().err


def test_a_billing_record_without_an_amount_is_unreadable_not_free(tmp_path: Path, billing: str, berlin: None) -> None:
    _FakeBilling.amounts = [2.0, None]
    with pytest.raises(policy.BillingUnreadable):
        policy.billed_today(billing, "k", tmp_path, datetime(2026, 10, 16, 9, 0, tzinfo=_CEST))


def test_budget_cli_wants_a_positive_timeout(tmp_path: Path, billing: str) -> None:
    for bad in ("-3600", "0", "nan"):
        res = _budget(billing, tmp_path / "s", "0.16", [], config=tmp_path / "cfg", timeout_s=bad)
        assert res.returncode == 2 and "--timeout-s" in res.stderr, (bad, res.stderr)


def test_budget_refuses_when_the_account_has_no_passwd_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    def no_entry(uid: int) -> object:
        raise KeyError(uid)

    monkeypatch.setattr(policy.pwd, "getpwuid", no_entry)
    live = tmp_path / "live.jsonl"
    live.write_text("")
    code = policy.main(["budget", "--api=http://127.0.0.1:9/v2", f"--state={tmp_path}", "--price=0.16",
                        "--timeout-s=3600", f"--live-json={live}"])
    assert code == 21 and "no passwd entry" in capsys.readouterr().err


# --- pod slots (R2) ---------------------------------------------------------------------------
# A pid is a live runpod-run while proc/<pid>/cmdline names runpod-run, as the kernel's does for
# the script (a run) or for the --ttl timer that keeps a pod (`… runpod-run --stop <id>`).
_RUN_CMDLINE = b"bash\0/home/u/.local/bin/runpod-run\0--\0make\0"
_TIMER_CMDLINE = b"bash\0/home/u/.agents/skills/runpod-offload/runpod-run\0--stop\0p1\0"


def _alive(proc: Path, pid: int, cmdline: bytes = _RUN_CMDLINE) -> int:
    (proc / str(pid)).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "cmdline").write_bytes(cmdline)
    return pid


def _die(proc: Path, pid: int) -> None:
    shutil.rmtree(proc / str(pid))


def _slot_marker(state: Path, name: str, *pids: int) -> Path:
    marker = state / "slots" / name
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("".join(f"{pid}\n" for pid in pids))
    return marker


def _three_live(state: Path, proc: Path) -> None:
    for name, pid in (("rr-h-a", 101), ("rr-h-b", 102), ("rr-h-c", 103)):
        _slot_marker(state, name, _alive(proc, pid))


def _names(slots: list[policy.Slot]) -> list[str]:
    return [slot.pod_name for slot in slots]


def _tickets(state: Path) -> list[Path]:
    return list((state / "queue").glob("*"))


class _Clock:
    """acquire's clock and sleep: each sleep advances the clock, then runs the next scripted
    event — what other runs do while this one waits."""

    def __init__(self, *events: Callable[[], object]) -> None:
        self.now = 0.0
        self.events = list(events)
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.events:
            self.events.pop(0)()


def _never_sleep(_: float) -> None:
    raise AssertionError("this caller must not queue")


def test_a_fourth_pod_queues_at_position_1_and_takes_the_first_slot_freed(tmp_path: Path) -> None:
    state, proc = tmp_path / "state", tmp_path / "proc"
    _three_live(state, proc)
    positions: list[tuple[int, int, list[str]]] = []
    # The release runs while the caller sleeps: it takes slots.lock, so this also proves the
    # caller never sleeps holding it (it would deadlock).
    clock = _Clock(lambda: None, lambda: policy.release(state, "rr-h-b", 102, proc_root=proc))
    admitted = policy.acquire(state, "rr-h-d", _alive(proc, 200), 600, proc_root=proc,
                              on_position=lambda pos, of, live: positions.append((pos, of, _names(live))),
                              clock=clock.clock, sleep=clock.sleep)
    assert admitted is True
    assert positions == [(1, 1, ["rr-h-a", "rr-h-b", "rr-h-c"])]  # once: the position never changed
    assert clock.sleeps == [5, 5]  # a check every 5 s; admitted at the first one after the release
    assert _names(policy.live_slots(state, proc_root=proc)) == ["rr-h-a", "rr-h-c", "rr-h-d"]
    assert (state / "slots" / "rr-h-d").read_text() == "200\n"
    assert _tickets(state) == []


def test_a_run_whose_own_pod_holds_a_live_slot_takes_it_over_without_a_new_one(tmp_path: Path) -> None:
    # This repo's pod was kept by --ttl and its timer holds the slot. All 3 are live, yet the
    # run passes at once: it reuses that pod, so it takes the slot over instead of a 4th.
    state, proc = tmp_path / "state", tmp_path / "proc"
    _slot_marker(state, "rr-h-a", _alive(proc, 101))
    _slot_marker(state, "rr-h-b", _alive(proc, 102))
    marker = _slot_marker(state, "rr-h-app", _alive(proc, 103, _TIMER_CMDLINE))
    assert policy.acquire(state, "rr-h-app", _alive(proc, 200), 600, proc_root=proc, sleep=_never_sleep) is True
    assert marker.read_text() == "200\n103\n"  # the run, then the timer it took the slot from
    assert policy.live_slots(state, proc_root=proc) == [
        policy.Slot("rr-h-a", 101), policy.Slot("rr-h-app", 200), policy.Slot("rr-h-b", 102)]
    assert _tickets(state) == []
    # The run fails before it reuses the pod (the timer still runs): the slot goes back to the timer.
    policy.release(state, "rr-h-app", 200, proc_root=proc)
    assert marker.read_text() == "103\n"
    # The timer's own --stop terminates the pod and gives the slot up.
    policy.release(state, "rr-h-app", 103, proc_root=proc)
    assert not marker.exists()


def test_a_kept_pods_slot_passes_from_the_run_to_its_timer_and_on_to_the_next_run(tmp_path: Path) -> None:
    state, proc = tmp_path / "state", tmp_path / "proc"
    marker = state / "slots" / "rr-h-app"
    assert policy.acquire(state, "rr-h-app", _alive(proc, 200), 600, proc_root=proc, sleep=_never_sleep)
    # The run keeps its pod under --ttl: the timer holds the slot once the run has exited.
    policy.handover(state, "rr-h-app", _alive(proc, 300, _TIMER_CMDLINE))
    _die(proc, 200)
    assert policy.live_slots(state, proc_root=proc) == [policy.Slot("rr-h-app", 300)]
    # The next run reuses the pod: it takes the slot over and ends the timer...
    assert policy.acquire(state, "rr-h-app", _alive(proc, 400), 600, proc_root=proc, sleep=_never_sleep)
    _die(proc, 300)
    assert policy.live_slots(state, proc_root=proc) == [policy.Slot("rr-h-app", 400)]
    # ...then terminates the pod: no holder is left to hand the slot back to.
    policy.release(state, "rr-h-app", 400, proc_root=proc)
    assert not marker.exists()


def test_a_slot_whose_holders_died_or_are_no_longer_runpod_run_is_free(tmp_path: Path) -> None:
    # A SIGKILLed run never releases its slot: its marker must not hold one forever.
    state, proc = tmp_path / "state", tmp_path / "proc"
    _slot_marker(state, "rr-h-a", _alive(proc, 101))
    _slot_marker(state, "rr-h-b", 102)  # no such process
    _slot_marker(state, "rr-h-c", _alive(proc, 103, b"sleep\0600\0"))  # the pid now runs something else
    _slot_marker(state, "rr-h-x", 104, 105)  # neither the run nor the timer it took over from lives
    (state / "slots" / "rr-h-y").write_text("not a pid\n")
    # A run killed after taking over a kept pod's slot: the timer still keeps that pod.
    _slot_marker(state, "rr-h-k", 106, _alive(proc, 107, _TIMER_CMDLINE))
    assert policy.live_slots(state, proc_root=proc) == [policy.Slot("rr-h-a", 101), policy.Slot("rr-h-k", 107)]
    assert policy.acquire(state, "rr-h-d", _alive(proc, 200), 600, proc_root=proc, sleep=_never_sleep) is True
    assert sorted(p.name for p in (state / "slots").iterdir()) == ["rr-h-a", "rr-h-d", "rr-h-k"]  # dead ones swept


def test_a_holder_that_gives_its_slot_up_leaves_it_even_when_listed_second(tmp_path: Path) -> None:
    # A run SIGKILLed after taking over a kept pod's slot leaves [run, timer]; when that timer
    # terminates the pod and gives the slot up, the slot goes, not back to the timer itself.
    state, proc = tmp_path / "state", tmp_path / "proc"
    marker = _slot_marker(state, "rr-h-k", 106, _alive(proc, 107, _TIMER_CMDLINE))
    policy.release(state, "rr-h-k", 107, proc_root=proc)
    assert not marker.exists() and policy.live_slots(state, proc_root=proc) == []


def test_the_queue_gives_up_after_its_timeout(tmp_path: Path) -> None:
    state, proc = tmp_path / "state", tmp_path / "proc"
    _three_live(state, proc)
    clock = _Clock()
    assert policy.acquire(state, "rr-h-d", _alive(proc, 200), 12, proc_root=proc,
                          clock=clock.clock, sleep=clock.sleep) is False
    assert clock.sleeps == [5, 5, 2]  # never sleeps past the deadline: the last check runs at it
    assert not (state / "slots" / "rr-h-d").exists() and _tickets(state) == []
    # A slot freed during the last wait is taken at that check, on the deadline, never later.
    clock = _Clock(lambda: None, lambda: None, lambda: policy.release(state, "rr-h-a", 101, proc_root=proc))
    assert policy.acquire(state, "rr-h-d", 200, 12, proc_root=proc, clock=clock.clock, sleep=clock.sleep) is True
    assert clock.sleeps == [5, 5, 2] and clock.now == 12


def test_a_run_killed_while_queued_leaves_the_queue(tmp_path: Path) -> None:
    # The helper outlives a SIGKILLed script: it must stop waiting for a run that is gone.
    state, proc = tmp_path / "state", tmp_path / "proc"
    _three_live(state, proc)
    clock = _Clock(lambda: _die(proc, 200))
    assert policy.acquire(state, "rr-h-d", _alive(proc, 200), 600, proc_root=proc,
                          clock=clock.clock, sleep=clock.sleep) is False
    assert clock.sleeps == [5] and _tickets(state) == []


def _until(condition: Callable[[], object], timeout_s: float = 10) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_two_waiters_get_slots_in_the_order_they_queued(tmp_path: Path) -> None:
    state, proc = tmp_path / "state", tmp_path / "proc"
    _three_live(state, proc)
    admitted: list[str] = []
    positions: dict[str, list[int]] = {"rr-h-d": [], "rr-h-e": []}

    def queue(name: str, pid: int) -> threading.Thread:
        def wait() -> None:
            if policy.acquire(state, name, pid, 30, proc_root=proc, poll_s=0.02,
                              on_position=lambda pos, _of, _live: positions[name].append(pos)):
                admitted.append(name)

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        _until(lambda: positions[name])  # it has queued
        return thread

    first, second = queue("rr-h-d", _alive(proc, 201)), queue("rr-h-e", _alive(proc, 202))
    policy.release(state, "rr-h-a", 101, proc_root=proc)
    first.join(10)
    time.sleep(0.2)  # ten of the second waiter's polls: the freed slot went to the first
    assert admitted == ["rr-h-d"] and second.is_alive()
    policy.release(state, "rr-h-b", 102, proc_root=proc)
    second.join(10)
    assert admitted == ["rr-h-d", "rr-h-e"]
    assert positions == {"rr-h-d": [1], "rr-h-e": [2, 1]}


@pytest.fixture
def runpod_runs() -> Iterator[Callable[..., int]]:
    """Spawns real processes whose command line names runpod-run (plus any extra words): the
    CLI reads the real /proc."""
    procs: list[subprocess.Popen[bytes]] = []

    def spawn(*words: str) -> int:
        procs.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "runpod-run", *words]))
        return procs[-1].pid

    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def test_slots_cli_queues_with_the_position_line_and_exits_22_on_timeout(tmp_path: Path,
                                                                         runpod_runs: Callable[..., int]) -> None:
    for name in ("rr-h-a", "rr-h-b", "rr-h-c"):
        _slot_marker(tmp_path, name, runpod_runs())
    res = _helper("slots", "acquire", f"--state={tmp_path}", "--name=rr-h-d", f"--pid={runpod_runs()}",
                  "--timeout-min=0.01")
    assert res.returncode == 22, res.stderr
    assert "runpod-run: queued for a pod slot (position 1 of 1; 3 live: rr-h-a, rr-h-b, rr-h-c)" in res.stderr
    assert "no pod slot came free within 0.01 min" in res.stderr
    assert not (tmp_path / "slots" / "rr-h-d").exists() and _tickets(tmp_path) == []
    listed = _helper("slots", "list", f"--state={tmp_path}", "--name=rr-h-d").stdout.splitlines()
    assert listed[0] == "slots: 3/3 live (rr-h-a, rr-h-b, rr-h-c), 0 queued → queue (position 1 of 1)"
    # A kept pod's slot is handed to its --ttl timer; the listing tells the two holders apart.
    timer = runpod_runs("--stop", "p1")
    assert _helper("slots", "handover", f"--state={tmp_path}", "--name=rr-h-c", f"--pid={timer}").returncode == 0
    listed = _helper("slots", "list", f"--state={tmp_path}", "--name=rr-h-c").stdout.splitlines()
    assert listed[0] == "slots: 3/3 live (rr-h-a, rr-h-b, rr-h-c), 0 queued → reuse (this pod holds a live slot)"
    assert [line.split() for line in listed[1:]][-1] == ["rr-h-c", "pid", str(timer), "(--ttl", "timer)"]
    assert _helper("slots", "release", f"--state={tmp_path}", "--name=rr-h-c", f"--pid={timer}").returncode == 0
    listed = _helper("slots", "list", f"--state={tmp_path}", "--name=rr-h-d").stdout.splitlines()
    assert listed[0] == "slots: 2/3 live (rr-h-a, rr-h-b), 0 queued → take a free slot"


def test_slots_cli_rejects_bad_arguments(tmp_path: Path) -> None:
    for bad in (["--name=../escape", "--pid=1", "--timeout-min=1"], ["--name=rr-h-a", "--pid=1", "--timeout-min=0"],
                ["--name=rr-h-a", "--pid=x", "--timeout-min=1"]):
        res = _helper("slots", "acquire", f"--state={tmp_path}", *bad)
        assert res.returncode == 2, (bad, res.stderr)
    # A pid that is no live runpod-run cannot queue (nothing would ever clear its ticket).
    res = _helper("slots", "acquire", f"--state={tmp_path}", "--name=rr-h-a", "--pid=999999999", "--timeout-min=1")
    assert res.returncode == 2 and "not a live runpod-run" in res.stderr, res.stderr


# --- dedup (R7) ---------------------------------------------------------------------------------


def _dedup_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / ".gitignore").write_text("build/\n")
    (path / "a.py").write_text("print(1)\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   check=True)
    return path


def test_tree_key_follows_the_synced_files_the_command_and_the_shape(tmp_path: Path) -> None:
    repo = _dedup_repo(tmp_path / "r")
    other = tmp_path / "data"  # outside git: relative paths + contents
    other.mkdir()
    (other / "x.csv").write_text("1\n")
    dirs = [repo, other]
    key = policy.tree_key(dirs, "make test", "cpu cpu3g 4")
    assert key == policy.tree_key(dirs, "make test", "cpu cpu3g 4")
    assert key != policy.tree_key(dirs, "make lint", "cpu cpu3g 4")
    assert key != policy.tree_key(dirs, "make test", "cpu cpu3g 8")
    assert key != policy.tree_key([repo], "make test", "cpu cpu3g 4")  # every dir counts
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("ignored")
    assert policy.tree_key(dirs, "make test", "cpu cpu3g 4") == key  # an ignored file does not travel
    (repo / "new.py").write_text("x = 1\n")  # untracked, not ignored: it travels
    edited = policy.tree_key(dirs, "make test", "cpu cpu3g 4")
    assert edited != key
    (repo / "new.py").write_text("x = 2\n")
    assert policy.tree_key(dirs, "make test", "cpu cpu3g 4") != edited
    (other / "x.csv").write_text("2\n")
    assert policy.tree_key(dirs, "make test", "cpu cpu3g 4") not in (key, edited)
    (repo / "a.py").unlink()  # a tracked file deleted on disk is a change, not a crash
    assert policy.tree_key(dirs, "make test", "cpu cpu3g 4") not in (key, edited)


def test_dedup_lookup_hits_within_24_hours_only(tmp_path: Path) -> None:
    now = datetime(2026, 10, 16, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    log = tmp_path / "run.log"
    log.write_text("ok\n")
    policy.store(tmp_path, "app-1234abcd", "k1", 7, log, now)
    hit = policy.lookup(tmp_path, "app-1234abcd", "k1", now + timedelta(hours=24) - timedelta(seconds=1))
    assert hit is not None and hit.exit == 7 and hit.log == str(log) and hit.ts == now.isoformat(timespec="seconds")
    assert policy.lookup(tmp_path, "app-1234abcd", "k1", now + timedelta(hours=24, seconds=1)) is None
    assert policy.lookup(tmp_path, "app-1234abcd", "k2", now) is None  # another tree/command/shape
    assert policy.lookup(tmp_path, "other-1234abcd", "k1", now) is None  # another repo
    # A run whose command never ran (refused, budget, queue, no ssh, no pod) leaves no record.
    for code in (2, 3, 20, 21, 22, 23, 255):
        policy.store(tmp_path, "skip-1234abcd", "k1", code, log, now)
        assert policy.lookup(tmp_path, "skip-1234abcd", "k1", now) is None
    (tmp_path / "dedup" / "app-1234abcd.json").write_text("{not json")
    assert policy.lookup(tmp_path, "app-1234abcd", "k1", now) is None


def test_dedup_cli_round_trip(tmp_path: Path) -> None:
    repo = _dedup_repo(tmp_path / "r")
    state = tmp_path / "state"

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(_HELPER), *args], capture_output=True, text=True, check=False)

    res = cli("dedup-key", "--shape=cpu cpu3g 4", "--cmd=make test", f"--dir={repo}")
    assert res.returncode == 0, res.stderr
    key = res.stdout.strip()
    assert key == policy.tree_key([repo], "make test", "cpu cpu3g 4")
    res = cli("dedup-lookup", f"--state={state}", "--repo-tag=r-1234abcd", f"--key={key}")
    assert res.returncode == 1 and res.stdout == ""
    assert cli("dedup-store", f"--state={state}", "--repo-tag=r-1234abcd", f"--key={key}", "--exit=0",
               f"--log={tmp_path / 'x.log'}").returncode == 0
    res = cli("dedup-lookup", f"--state={state}", "--repo-tag=r-1234abcd", f"--key={key}")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["exit"] == 0
    assert cli("dedup-lookup", f"--state={state}", "--repo-tag=../x", f"--key={key}").returncode == 2


def test_tree_key_follows_modes_and_links_and_never_opens_a_fifo(tmp_path: Path) -> None:
    repo = _dedup_repo(tmp_path / "r")
    data = tmp_path / "data"
    (data / "release-a").mkdir(parents=True)
    (data / "release-b").mkdir()
    (data / "release-b" / "f").write_text("b")
    (data / "current").symlink_to("release-a")
    dirs = [repo, data]
    key = policy.tree_key(dirs, "make", "s")
    (repo / "a.py").chmod(0o755)  # rsync -a sends the execute bit
    assert policy.tree_key(dirs, "make", "s") != key
    (repo / "a.py").chmod(0o644)
    assert policy.tree_key(dirs, "make", "s") == key
    (data / "current").unlink()
    (data / "current").symlink_to("release-b")  # a retargeted dir symlink is a change
    assert policy.tree_key(dirs, "make", "s") != key
    os.mkfifo(data / "pipe")  # read, it would block forever
    policy.tree_key(dirs, "make", "s")


def test_tree_key_of_a_checkout_with_an_uninitialized_submodule(tmp_path: Path) -> None:
    # A gitlink whose dir is empty (submodule not initialized): git run inside it answers for
    # the parent and lists the gitlink as "./" — that must not count as an entry of the dir
    # (recursing into it never ended: RecursionError on agent-tools' vendor/ai-automation).
    repo = _dedup_repo(tmp_path / "r")
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/sub"],
                   check=True)
    (repo / "vendor" / "sub").mkdir(parents=True)
    key = policy.tree_key([repo], "make", "s")
    assert key == policy.tree_key([repo], "make", "s")
    (repo / "a.py").write_text("print(2)\n")
    assert policy.tree_key([repo], "make", "s") != key
