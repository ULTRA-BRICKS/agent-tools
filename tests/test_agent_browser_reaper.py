"""Tests for the agent-browser session cleanup pair.

Covers all three shipped pieces:
  * ops/agent-browser-session-reaper/agent_browser_reaper.py — the periodic sweep
  * ops/agent-browser-session-reaper/session_end_close.py    — the SessionEnd hook
  * agent-hooks/agent-browser-session-claim/claim_agent_browser_session.py — the
    pre-bash ownership recorder that makes the SessionEnd hook safe

The safety properties under test are the ones that matter in the incident this was
built for (2026-09-04 memory exhaustion from abandoned browser sessions):

  1. A fresh session is never closed — the age gate is real, and `last_seen` takes the
     MAX of every known timestamp so an unknown always errs toward "recent".
  2. A name in KEEP is never closed by either mechanism.
  3. A failed/empty `session list` closes NOTHING (a broken probe must not read as
     "everything is stale").
  4. The SessionEnd hook closes only names the finishing session CLAIMED, that are
     LIVE, and that no other claim directory also claims.
  5. The claim parser never claims a read-only agent-browser invocation, and never
     writes outside the claim directory for a hostile session id / session name.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agent_browser_reaper.py -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_OPS = _ROOT / "ops" / "agent-browser-session-reaper"
_HOOK = _ROOT / "agent-hooks" / "agent-browser-session-claim"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reaper = _load("agent_browser_reaper", _OPS / "agent_browser_reaper.py")
session_end = _load("session_end_close", _OPS / "session_end_close.py")
claim = _load("claim_agent_browser_session",
              _HOOK / "claim_agent_browser_session.py")


# --- fakes -----------------------------------------------------------------------


class FakeRun:
    """Stands in for subprocess.run: canned `session list`, recorded `close` calls."""

    def __init__(self, sessions: list[str], *, list_ok: bool = True,
                 etime: str | None = None):
        self.sessions = sessions
        self.list_ok = list_ok
        self.etime = etime
        self.closed: list[str] = []

    def __call__(self, argv, **kwargs):
        if argv[0] == "ps":
            out = f"    {self.etime}\n" if self.etime else ""
            return subprocess.CompletedProcess(argv, 0 if self.etime else 1, out, "")
        if argv[1:3] == ["session", "list"]:
            if not self.list_ok:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            payload = json.dumps({"success": True, "data": {"sessions": self.sessions}})
            return subprocess.CompletedProcess(argv, 0, payload, "")
        if "close" in argv:
            self.closed.append(argv[argv.index("--session") + 1])
            return subprocess.CompletedProcess(argv, 0, '{"success":true}', "")
        raise AssertionError(f"unexpected call: {argv}")


@pytest.fixture(autouse=True)
def _fake_binary(monkeypatch):
    monkeypatch.setattr(reaper, "_agent_browser_bin", lambda: "/fake/agent-browser")


def _state_dir(tmp_path: Path, name: str, age_s: float) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    sidecar = state / f"{name}.target"
    sidecar.write_text("{}", encoding="utf-8")
    stamp = time.time() - age_s
    import os
    os.utime(sidecar, (stamp, stamp))
    return state


# --- sweep -----------------------------------------------------------------------


def test_fresh_session_is_not_closed(tmp_path):
    run = FakeRun(["fresh"])
    state = _state_dir(tmp_path, "fresh", age_s=60)
    reaper.sweep(reaper.Config(max_age_min=180), state_dir=state,
                 claim_dir=tmp_path / "claims", run=run)
    assert run.closed == []


def test_stale_session_is_closed(tmp_path):
    run = FakeRun(["stale"])
    state = _state_dir(tmp_path, "stale", age_s=4 * 3600)
    reaper.sweep(reaper.Config(max_age_min=180), state_dir=state,
                 claim_dir=tmp_path / "claims", run=run)
    assert run.closed == ["stale"]


def test_keep_list_exempts_a_stale_session(tmp_path):
    run = FakeRun(["stale"])
    state = _state_dir(tmp_path, "stale", age_s=4 * 3600)
    cfg = reaper.Config(max_age_min=180, keep=frozenset({"stale"}))
    reaper.sweep(cfg, state_dir=state, claim_dir=tmp_path / "claims", run=run)
    assert run.closed == []


def test_dry_run_closes_nothing(tmp_path):
    run = FakeRun(["stale"])
    state = _state_dir(tmp_path, "stale", age_s=4 * 3600)
    reaper.sweep(reaper.Config(max_age_min=180), dry_run=True, state_dir=state,
                 claim_dir=tmp_path / "claims", run=run)
    assert run.closed == []


def test_disabled_closes_nothing(tmp_path):
    run = FakeRun(["stale"])
    state = _state_dir(tmp_path, "stale", age_s=4 * 3600)
    reaper.sweep(reaper.Config(enabled=False, max_age_min=180), state_dir=state,
                 claim_dir=tmp_path / "claims", run=run)
    assert run.closed == []


def test_failed_session_list_closes_nothing(tmp_path):
    """A broken probe must never be read as 'everything is stale'."""
    run = FakeRun(["stale"], list_ok=False)
    state = _state_dir(tmp_path, "stale", age_s=4 * 3600)
    reaper.sweep(reaper.Config(max_age_min=180), state_dir=state,
                 claim_dir=tmp_path / "claims", run=run)
    assert run.closed == []


def test_session_with_no_timestamp_at_all_is_skipped(tmp_path):
    run = FakeRun(["ghost"])
    state = tmp_path / "empty"
    state.mkdir()
    reaper.sweep(reaper.Config(max_age_min=180), state_dir=state,
                 claim_dir=tmp_path / "claims", run=run)
    assert run.closed == []


def test_last_seen_takes_the_max_so_a_young_daemon_protects_an_old_sidecar(tmp_path):
    """A reused session name has an OLD .target and a NEW daemon — keep it."""
    import os
    run = FakeRun(["reused"], etime="02:00")  # daemon up 2 minutes
    state = _state_dir(tmp_path, "reused", age_s=10 * 3600)
    pid_file = state / "reused.pid"
    pid_file.write_text("4242", encoding="utf-8")
    stale = time.time() - 10 * 3600
    os.utime(pid_file, (stale, stale))
    last_seen = reaper.session_last_seen("reused", state, run=run)
    assert time.time() - last_seen < 300


def test_close_rejects_an_unsafe_session_name():
    run = FakeRun([])
    assert reaper.close_session("evil; rm -rf /", run=run) is False
    assert run.closed == []


@pytest.mark.parametrize("etime,expected", [
    ("30", 30), ("01:30", 90), ("02:01:30", 7290), ("1-02:01:30", 93690),
    ("", None), ("nope", None),
])
def test_parse_bsd_elapsed(etime, expected):
    assert reaper._parse_bsd_elapsed(etime) == expected


# --- configuration ---------------------------------------------------------------


def test_config_file_overrides_defaults_and_env_overrides_the_file(tmp_path, monkeypatch):
    conf = tmp_path / "reaper.conf"
    conf.write_text("# comment\nMAX_AGE_MIN=45\nKEEP=a, b\nENABLED=0\n", encoding="utf-8")
    monkeypatch.delenv("AGENT_BROWSER_REAPER_MAX_AGE_MIN", raising=False)
    cfg = reaper.load_config(conf)
    assert (cfg.max_age_min, cfg.enabled, cfg.keep) == (45, False, frozenset({"a", "b"}))

    monkeypatch.setenv("AGENT_BROWSER_REAPER_MAX_AGE_MIN", "600")
    assert reaper.load_config(conf).max_age_min == 600


def test_nonsense_values_fall_back_to_defaults(tmp_path):
    conf = tmp_path / "reaper.conf"
    conf.write_text("MAX_AGE_MIN=-5\n", encoding="utf-8")
    assert reaper.load_config(conf).max_age_min == reaper.DEFAULT_MAX_AGE_MIN


def test_missing_config_file_is_all_defaults(tmp_path):
    cfg = reaper.load_config(tmp_path / "nope.conf")
    assert cfg.enabled and cfg.max_age_min == reaper.DEFAULT_MAX_AGE_MIN


# --- claim recorder (pre-bash hook) ----------------------------------------------


@pytest.mark.parametrize("command,expected", [
    ("agent-browser --session foo open https://x", ["foo"]),
    ("agent-browser --session=foo open https://x", ["foo"]),
    ("AGENT_BROWSER_SESSION=foo agent-browser open https://x", ["foo"]),
    ("agent-browser open https://x", ["default"]),
    ("/opt/homebrew/bin/agent-browser screenshot out.png", ["default"]),
    ("agent-browser --session a open x && agent-browser --session b open y", ["a", "b"]),
    ("echo hello", []),
    ("my-agent-browserish --session foo", []),
    ("agent-browser session list --json", []),
    ("agent-browser doctor", []),
    ("agent-browser --help", []),
])
def test_session_names_parsing(command, expected):
    assert claim.session_names(command) == expected


def test_claim_writes_one_marker_per_name(tmp_path):
    written = claim.write_claims("sess-1", ["alpha", "beta"], claim_dir=tmp_path)
    assert written == 2
    assert {p.name for p in (tmp_path / "sess-1").iterdir()} == {"alpha", "beta"}


def test_claim_refuses_path_traversal_in_a_session_name(tmp_path):
    assert claim.write_claims("sess-1", ["../../escape", "ok"], claim_dir=tmp_path) == 1
    assert not (tmp_path.parent / "escape").exists()
    assert {p.name for p in (tmp_path / "sess-1").iterdir()} == {"ok"}


def test_claim_refuses_an_unsafe_claude_session_id():
    assert claim._session_id_from({"args": {"session_id": "../evil"}}) == ""
    assert claim._session_id_from({"args": {"session_id": "a" * 500}}) == ""
    assert claim._session_id_from({"args": {"session_id": "ok-1"}}) == "ok-1"


def test_claim_hook_always_allows(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(claim, "CLAIM_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdin", _Stdin("not json at all"))
    assert claim.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


# --- session-end hook -------------------------------------------------------------


def _claim(claim_dir: Path, session_id: str, *names: str) -> None:
    target = claim_dir / session_id
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        (target / name).touch()


def test_session_end_closes_only_claimed_and_live(tmp_path, monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(session_end, "list_live_sessions",
                        lambda: ["mine", "someone-elses"])
    monkeypatch.setattr(session_end, "close_session",
                        lambda n: closed.append(n) or True)
    monkeypatch.setattr(session_end, "load_config", lambda: reaper.Config())
    _claim(tmp_path, "sess-1", "mine", "already-gone")
    assert session_end.run({"session_id": "sess-1", "reason": "other"}, tmp_path) == 1
    assert closed == ["mine"]
    assert not (tmp_path / "sess-1").exists()


def test_session_end_leaves_a_name_another_session_also_claims(tmp_path, monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(session_end, "list_live_sessions", lambda: ["default"])
    monkeypatch.setattr(session_end, "close_session",
                        lambda n: closed.append(n) or True)
    monkeypatch.setattr(session_end, "load_config", lambda: reaper.Config())
    _claim(tmp_path, "sess-1", "default")
    _claim(tmp_path, "sess-2", "default")
    assert session_end.run({"session_id": "sess-1", "reason": "other"}, tmp_path) == 0
    assert closed == []


def test_session_end_respects_keep(tmp_path, monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(session_end, "list_live_sessions", lambda: ["mine"])
    monkeypatch.setattr(session_end, "close_session",
                        lambda n: closed.append(n) or True)
    monkeypatch.setattr(session_end, "load_config",
                        lambda: reaper.Config(keep=frozenset({"mine"})))
    _claim(tmp_path, "sess-1", "mine")
    assert session_end.run({"session_id": "sess-1", "reason": "other"}, tmp_path) == 0
    assert closed == []


def test_session_end_ignores_clear_and_resume(tmp_path, monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(session_end, "list_live_sessions", lambda: ["mine"])
    monkeypatch.setattr(session_end, "close_session",
                        lambda n: closed.append(n) or True)
    monkeypatch.setattr(session_end, "load_config", lambda: reaper.Config())
    _claim(tmp_path, "sess-1", "mine")
    assert session_end.run({"session_id": "sess-1", "reason": "clear"}, tmp_path) == 0
    assert closed == []
    assert (tmp_path / "sess-1").exists()  # claim survives for the next exit


def test_session_end_disabled_switch(tmp_path, monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(session_end, "list_live_sessions", lambda: ["mine"])
    monkeypatch.setattr(session_end, "close_session",
                        lambda n: closed.append(n) or True)
    monkeypatch.setattr(session_end, "load_config",
                        lambda: reaper.Config(session_end_enabled=False))
    _claim(tmp_path, "sess-1", "mine")
    assert session_end.run({"session_id": "sess-1", "reason": "other"}, tmp_path) == 0
    assert closed == []


def test_session_end_rejects_an_unsafe_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(session_end, "list_live_sessions", lambda: ["mine"])
    assert session_end.run({"session_id": "../../etc", "reason": "other"}, tmp_path) == 0


# --- claim-dir pruning ------------------------------------------------------------


def test_prune_removes_only_old_claim_dirs(tmp_path):
    import os
    _claim(tmp_path, "old", "a")
    _claim(tmp_path, "new", "b")
    stale = time.time() - 48 * 3600
    os.utime(tmp_path / "old", (stale, stale))
    assert reaper.prune_claim_dirs(reaper.Config(), time.time(), tmp_path) == 1
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new").exists()
