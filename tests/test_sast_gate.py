"""Tests for the ci/sast/ gate — the semgrep SAST CI slot (agent-tools GL-264).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_sast_gate.py -q

The gate is a portable POSIX shell runner (``ci/sast/sast.sh``) wrapping ``semgrep scan``.
These tests shell out to the real script with a STUBBED ``semgrep`` on PATH (so no network /
no real scanner is needed) and assert the gate's contract:

- **block tier (default)**: when semgrep exits non-zero the runner FAILS with the SAME exit
  code. This is the load-bearing regression (GL-264): the runner gated on
  ``if semgrep …; then … exit 0; fi`` and read ``rc=$?`` in the fall-through, where — after an
  ``if`` whose condition is false and has no ``else`` — POSIX ``$?`` is the *if statement's*
  status, which is 0. So a real finding (semgrep exit 1) or a scanner infrastructure failure
  (semgrep exit 2) printed FAIL but ``exit 0``, leaving CI green;
- **pass**: a clean scan (semgrep exit 0) → runner exit 0;
- **warn tier** (``SAST_WARN=1``): never fails the build — exits 0 regardless of the scanner's
  exit code (the documented "warn tier" policy), including on an infrastructure failure;
- **arg construction**: each ``SEMGREP_CONFIG`` word becomes a repeated ``--config``, ``--error``
  is always passed, ``SEMGREP_EXTRA`` flows through word-split, and ``SEMGREP_TARGET`` is the
  final positional.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ci" / "sast" / "sast.sh"


def _stub_semgrep(bin_dir: Path, *, scan_exit: int) -> Path:
    """Write a fake ``semgrep`` on PATH: ``--version`` prints + exits 0; a scan echoes its argv
    to a sidecar file and exits *scan_exit*."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv_log = bin_dir / "semgrep-argv.txt"
    stub = bin_dir / "semgrep"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "0.0.0-stub"; exit 0; fi\n'
        f'printf "%s\\n" "$*" > "{argv_log}"\n'
        f"exit {scan_exit}\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return argv_log


def _run(cwd: Path, bin_dir: Path, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["sh", str(SCRIPT)], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )
    return proc.returncode, proc.stderr


def test_block_tier_passes_when_clean(tmp_path: Path):
    """A clean scan (semgrep exit 0) → runner passes."""
    bin_dir = tmp_path / "bin"
    _stub_semgrep(bin_dir, scan_exit=0)
    rc, err = _run(tmp_path, bin_dir)
    assert rc == 0, err
    assert "PASS" in err


def test_block_tier_fails_on_finding(tmp_path: Path):
    """REGRESSION (GL-264): a finding (semgrep exit 1) must FAIL the runner with exit 1, not 0."""
    bin_dir = tmp_path / "bin"
    _stub_semgrep(bin_dir, scan_exit=1)
    rc, err = _run(tmp_path, bin_dir)
    assert rc == 1, f"block tier must propagate semgrep's exit 1 on a finding; got {rc}\n{err}"
    assert "FAIL" in err


def test_block_tier_fails_on_scanner_infrastructure_error(tmp_path: Path):
    """REGRESSION (GL-264): a scanner infrastructure failure (semgrep exit 2) must FAIL with
    exit 2 — a broken scan must not pass as clean."""
    bin_dir = tmp_path / "bin"
    _stub_semgrep(bin_dir, scan_exit=2)
    rc, err = _run(tmp_path, bin_dir)
    assert rc == 2, f"block tier must propagate semgrep's exit 2 on a scan error; got {rc}\n{err}"
    assert "FAIL" in err


def test_warn_tier_never_fails_on_finding(tmp_path: Path):
    """SAST_WARN=1: a finding (semgrep exit 1) is reported but the build passes (exit 0)."""
    bin_dir = tmp_path / "bin"
    _stub_semgrep(bin_dir, scan_exit=1)
    rc, err = _run(tmp_path, bin_dir, env_extra={"SAST_WARN": "1"})
    assert rc == 0, f"warn tier must not fail on a finding; got {rc}\n{err}"
    assert "WARN tier" in err


def test_warn_tier_never_fails_on_scanner_error(tmp_path: Path):
    """SAST_WARN=1 must NOT fail even when semgrep exits with an infrastructure error (exit 2).

    The documented warn-tier contract is 'never fail the build'; that is unconditional, so an
    operational semgrep error must still exit 0 in warn tier.
    """
    bin_dir = tmp_path / "bin"
    _stub_semgrep(bin_dir, scan_exit=2)
    rc, err = _run(tmp_path, bin_dir, env_extra={"SAST_WARN": "1"})
    assert rc == 0, f"warn tier must not fail on a scanner error; got {rc}\n{err}"
    assert "WARN tier" in err


def test_default_args(tmp_path: Path):
    """Default invocation builds the expected semgrep scan argv: --config auto, --error, and
    the target as the final positional."""
    bin_dir = tmp_path / "bin"
    argv_log = _stub_semgrep(bin_dir, scan_exit=0)
    _run(tmp_path, bin_dir)
    argv = argv_log.read_text().strip()
    assert argv.startswith("scan "), argv
    assert "--config auto" in argv
    assert "--error" in argv
    assert argv.endswith(" ."), argv  # default target is the last positional


def test_config_target_and_extra_flow_into_invocation(tmp_path: Path):
    """Each SEMGREP_CONFIG word becomes a repeated --config; SEMGREP_EXTRA is word-split through;
    SEMGREP_TARGET lands as the final positional."""
    bin_dir = tmp_path / "bin"
    argv_log = _stub_semgrep(bin_dir, scan_exit=0)
    sub = tmp_path / "sub"
    sub.mkdir()
    _run(
        tmp_path,
        bin_dir,
        env_extra={
            "SEMGREP_CONFIG": "p/security-audit p/secrets",
            "SEMGREP_TARGET": "sub",
            "SEMGREP_EXTRA": "--max-target-bytes 1000000 --quiet",
        },
    )
    argv = argv_log.read_text().strip()
    assert "--config p/security-audit" in argv
    assert "--config p/secrets" in argv
    assert "--error" in argv
    assert "--max-target-bytes 1000000" in argv
    assert "--quiet" in argv
    assert argv.endswith(" sub"), argv  # target is the last positional


def test_script_is_executable_and_shellcheck_clean():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "sast.sh must be executable"
    import shutil

    if shutil.which("shellcheck"):
        proc = subprocess.run(
            ["shellcheck", str(SCRIPT)], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
