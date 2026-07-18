"""Tests for the shared rig-aware install-hook delegation helper.

Run from the repo root::

    python -m pytest tests/test_rig_delegate.py -q
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import agenttools_rig_delegate as rd  # noqa: E402


def _make_fake_rig(dir_path: Path, exit_code: int = 0, marker: Path | None = None) -> Path:
    """Write an executable fake ``rig`` that records its argv and exits ``exit_code``."""
    dir_path.mkdir(parents=True, exist_ok=True)
    rig = dir_path / "rig"
    log = marker or (dir_path / "rig-invocations.log")
    # Absolute /bin/sh shebang: tests set PATH to only this bindir, so an
    # `#!/usr/bin/env bash` shebang could not resolve its interpreter.
    rig.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f"exit {exit_code}\n"
    )
    rig.chmod(rig.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return rig


# --- find_rig / rig_available -------------------------------------------------------


def test_find_rig_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RIG_BIN", raising=False)
    # Point the fallback bins at nothing by using a HOME with no rig.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert rd.find_rig() is None
    assert rd.rig_available() is False


def test_find_rig_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _make_fake_rig(bindir)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("RIG_BIN", raising=False)
    found = rd.find_rig()
    assert found is not None
    assert Path(found).name == "rig"
    assert rd.rig_available() is True


def test_find_rig_bin_override_wins(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    rig = _make_fake_rig(bindir)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("RIG_BIN", str(rig))
    assert rd.find_rig() == str(rig)


def test_find_rig_override_nonexecutable_is_absent(tmp_path, monkeypatch):
    plain = tmp_path / "not-exec"
    plain.write_text("#!/bin/sh\n")  # not chmod +x
    monkeypatch.setenv("RIG_BIN", str(plain))
    assert rd.find_rig() is None


# --- delegate -----------------------------------------------------------------------


def test_delegate_runs_rig_and_returns_code(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    _make_fake_rig(bindir, exit_code=0, marker=log)
    monkeypatch.setenv("PATH", str(bindir))
    res = rd.delegate(["apply"])
    assert res.delegated is True
    assert res.fell_back is False
    assert res.returncode == 0
    assert "apply" in log.read_text()


def test_delegate_surfaces_rig_nonzero(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _make_fake_rig(bindir, exit_code=4)
    monkeypatch.setenv("PATH", str(bindir))
    res = rd.delegate(["apply"])
    assert res.returncode == 4  # a rig failure is surfaced, not hidden


def test_delegate_raises_when_rig_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_BIN", raising=False)
    with pytest.raises(RuntimeError):
        rd.delegate(["apply"])


# --- delegate_or_fallback -----------------------------------------------------------


def test_delegate_or_fallback_delegates_when_present(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    _make_fake_rig(bindir, exit_code=0, marker=log)
    monkeypatch.setenv("PATH", str(bindir))
    called = {"fallback": False}

    def fallback() -> int:
        called["fallback"] = True
        return 99

    res = rd.delegate_or_fallback(["apply"], fallback)
    assert res.delegated is True
    assert res.fell_back is False
    assert called["fallback"] is False  # fallback MUST NOT run when rig is present
    assert "apply" in log.read_text()


def test_delegate_or_fallback_runs_fallback_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RIG_BIN", raising=False)
    called = {"fallback": False}

    def fallback() -> int:
        called["fallback"] = True
        return 0

    res = rd.delegate_or_fallback(["apply"], fallback)
    assert res.delegated is False
    assert res.fell_back is True
    assert called["fallback"] is True
    assert res.returncode == 0


def test_delegate_or_fallback_does_not_fall_back_on_rig_error(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _make_fake_rig(bindir, exit_code=5)
    monkeypatch.setenv("PATH", str(bindir))
    called = {"fallback": False}

    def fallback() -> int:
        called["fallback"] = True
        return 0

    res = rd.delegate_or_fallback(["apply"], fallback)
    assert res.returncode == 5
    assert called["fallback"] is False  # present-but-failed != absent


# --- __main__ CLI (the shell-out surface tg-ctl uses) -------------------------------


def _run_cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", "agenttools_rig_delegate", *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _cli_env(monkeypatch, extra_path=None, home=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "lib")
    if extra_path is not None:
        env["PATH"] = extra_path
    if home is not None:
        env["HOME"] = home
    env.pop("RIG_BIN", None)
    return env


def test_cli_detect_present(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _make_fake_rig(bindir)
    env = _cli_env(monkeypatch, extra_path=str(bindir))
    r = _run_cli(["detect"], env)
    assert r.returncode == 0
    assert "rig" in r.stdout


def test_cli_detect_absent(tmp_path, monkeypatch):
    env = _cli_env(monkeypatch, extra_path=str(tmp_path / "empty"), home=str(tmp_path / "home"))
    r = _run_cli(["detect"], env)
    assert r.returncode == 1


def test_cli_delegate_runs_rig(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    _make_fake_rig(bindir, exit_code=0, marker=log)
    env = _cli_env(monkeypatch, extra_path=str(bindir))
    r = _run_cli(["delegate", "apply", "--yes"], env)
    assert r.returncode == 0
    assert "apply --yes" in log.read_text()


# rig's own public exit-code contract (riglib/errors.py): 0-8 are semantic failure
# classes (3 == EXIT_DRIFT), 127 == EXIT_MISSING_DEP. The NO_RIG sentinel MUST avoid every
# one of them, else a shell caller cannot tell "rig absent" from a real rig exit — e.g. a
# `rig status` that exits 3 for config↔disk drift would be misread as "no rig" and would
# wrongly run the direct hook writer, re-introducing the double-write this helper prevents.
RIG_EXIT_CODES = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 127})


def test_no_rig_exit_does_not_collide_with_rig_contract():
    assert rd.NO_RIG_EXIT not in RIG_EXIT_CODES


def test_cli_delegate_sentinel_when_absent(tmp_path, monkeypatch):
    env = _cli_env(monkeypatch, extra_path=str(tmp_path / "empty"), home=str(tmp_path / "home"))
    r = _run_cli(["delegate", "apply"], env)
    assert r.returncode == rd.NO_RIG_EXIT  # sentinel -> caller runs its own fallback
    assert r.returncode not in RIG_EXIT_CODES  # never confusable with a real rig exit


def test_cli_delegate_surfaces_rig_code(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    _make_fake_rig(bindir, exit_code=7)
    env = _cli_env(monkeypatch, extra_path=str(bindir))
    r = _run_cli(["delegate", "apply"], env)
    assert r.returncode == 7
