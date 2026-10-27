"""secret-scan.sh is a <=40-line POSIX launcher around secret_scan.py (agent-tools#1036).

Launcher behavior, lint cleanliness, and every exit-code branch of the port, using a
gitleaks stub on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SLOT = Path(__file__).resolve().parent.parent / "ci" / "secret-scan"
LAUNCHER = SLOT / "secret-scan.sh"
PY = SLOT / "secret_scan.py"


def _env(tmp_path: Path, gitleaks_rc: int | None, warn_rc: int | None = None) -> dict:
    """PATH = stub dir + system PATH. The stub logs argv; warn pass uses -c warn.toml."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if gitleaks_rc is not None:
        stub = bindir / "gitleaks"
        warn = 0 if warn_rc is None else warn_rc
        stub.write_text(
            "#!/bin/sh\n"
            'echo "$*" >> "$STUB_LOG"\n'
            'case "$*" in *warn.toml*) exit %d;; esac\n'
            "exit %d\n" % (warn, gitleaks_rc)
        )
        stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SECRET_SCAN")}
    env["PATH"] = f"{bindir}:{os.environ['PATH']}"
    env["STUB_LOG"] = str(tmp_path / "argv.log")
    return env


def _run(tmp_path, rc, warn_rc=None, extra=None, cmd=None):
    env = _env(tmp_path, rc, warn_rc)
    env.update(extra or {})
    return subprocess.run(
        cmd or ["sh", str(LAUNCHER)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def _calls(tmp_path) -> list:
    log = tmp_path / "argv.log"
    return log.read_text().splitlines() if log.exists() else []


# ---- launcher ----------------------------------------------------------------


def test_launcher_is_short_posix_sh():
    text = LAUNCHER.read_text()
    assert len(text.splitlines()) <= 40
    assert text.startswith("#!/bin/sh\n")
    assert os.access(LAUNCHER, os.X_OK) and os.access(PY, os.X_OK)


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_launcher_runs_under_sh_and_bash_from_other_cwd(tmp_path, shell):
    rel = os.path.relpath(LAUNCHER, tmp_path)
    r = _run(tmp_path, 0, cmd=[shell, rel])
    assert r.returncode == 0, r.stderr
    assert "clean — no high-confidence secrets found." in r.stderr


def test_launcher_direct_exec_via_relative_path(tmp_path):
    r = _run(tmp_path, 0, cmd=[os.path.relpath(LAUNCHER, tmp_path)])
    assert r.returncode == 0, r.stderr


def test_launcher_fails_clearly_when_py_missing(tmp_path):
    copy = tmp_path / "secret-scan.sh"
    shutil.copy(LAUNCHER, copy)
    r = _run(tmp_path, 0, cmd=["sh", str(copy)])
    assert r.returncode == 1
    assert "missing" in r.stderr and "secret_scan.py" in r.stderr


def test_launcher_fails_clearly_when_python3_missing(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname",):
        found = shutil.which(name)
        assert found
        (tools / name).symlink_to(found)
    r = subprocess.run(
        ["/bin/sh", str(LAUNCHER)],
        env={"PATH": str(tools)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert "python3 is required" in r.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck missing")
def test_launcher_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(LAUNCHER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not shutil.which("shfmt"), reason="shfmt missing")
def test_launcher_shfmt_clean():
    r = subprocess.run(
        ["shfmt", "-i", "2", "-ci", "-bn", "-d", str(LAUNCHER)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff missing")
def test_py_ruff_default_clean():
    for sub in (["check"], ["format", "--check"]):
        r = subprocess.run(
            ["ruff", *sub, "--no-cache", "--isolated", str(PY)],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stdout + r.stderr


# ---- behavior ----------------------------------------------------------------


def test_clean_scan_exits_0_with_exact_argv(tmp_path):
    r = _run(tmp_path, 0)
    assert r.returncode == 0
    assert _calls(tmp_path) == ["git --redact --no-banner -v"]


def test_finding_blocks_with_exit_1_and_messages(tmp_path):
    r = _run(tmp_path, 1)
    assert r.returncode == 1
    assert "secret-scan: BLOCKED — a high-confidence secret was found." in r.stderr
    assert "gitleaks:allow" in r.stderr
    assert "clean —" not in r.stderr


def test_staged_scope_and_explicit_config_argv(tmp_path):
    r = _run(
        tmp_path,
        0,
        extra={"SECRET_SCAN_SCOPE": "staged", "SECRET_SCAN_CONFIG": "my.toml"},
    )
    assert r.returncode == 0
    assert _calls(tmp_path) == ["git --staged --redact --no-banner -v -c my.toml"]


def test_unknown_scope_falls_back_to_full_history(tmp_path):
    _run(tmp_path, 0, extra={"SECRET_SCAN_SCOPE": "bogus"})
    assert _calls(tmp_path) == ["git --redact --no-banner -v"]


def test_warn_tier_never_fails_the_build(tmp_path):
    r = _run(tmp_path, 0, warn_rc=1, extra={"SECRET_SCAN_WARN_CONFIG": "warn.toml"})
    assert r.returncode == 0
    assert "WARNING — suspicious string(s) found (not failing the build)" in r.stderr
    assert _calls(tmp_path) == [
        "git --redact --no-banner -v -c warn.toml",
        "git --redact --no-banner -v",
    ]


def test_warn_tier_clean_prints_no_warning_and_block_still_runs(tmp_path):
    r = _run(tmp_path, 1, warn_rc=0, extra={"SECRET_SCAN_WARN_CONFIG": "warn.toml"})
    assert r.returncode == 1
    assert "WARNING" not in r.stderr
    assert "BLOCKED" in r.stderr


def test_missing_gitleaks_and_no_installers_exits_1(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "python3"):
        found = shutil.which(name)
        assert found
        (tools / name).symlink_to(found)
    r = subprocess.run(
        ["/bin/sh", str(LAUNCHER)],
        env={"PATH": str(tools), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert "gitleaks not found — attempting install (v8.30.1)." in r.stderr
    assert "could not install gitleaks automatically" in r.stderr


def test_gitleaks_version_knob_is_reported(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "python3"):
        (tools / name).symlink_to(shutil.which(name))
    r = subprocess.run(
        ["/bin/sh", str(LAUNCHER)],
        env={"PATH": str(tools), "GITLEAKS_VERSION": "9.9.9"},
        capture_output=True,
        text=True,
    )
    assert "(v9.9.9)" in r.stderr
