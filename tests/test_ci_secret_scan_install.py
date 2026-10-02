"""The secret-scan catalog workflow must run on non-root self-hosted runners (Forgejo / Gitea
act runners, GARM pools) and must only ever execute the pinned, checksum-verified OSS gitleaks
binary — while still failing the job on a finding.

Run from the repo root::

    uv run --with pytest --with pyyaml python -m pytest tests/test_ci_secret_scan_install.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SECRET_WF = Path(__file__).resolve().parent.parent / "ci" / "secret-scan" / "secret-scan.yml"


def _workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(SECRET_WF.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _workflow()["jobs"]["gitleaks"]["steps"]


def _install_run() -> str:
    return next(s["run"] for s in _steps() if "curl" in str(s.get("run", "")))


def test_installs_into_a_user_writable_dir_on_path():
    run = _install_run()
    assert "/usr/local/bin" not in run, "a non-root runner user cannot write /usr/local/bin"
    assert "RUNNER_TEMP" in run
    assert '>> "$GITHUB_PATH"' in run


def test_tarball_checksum_is_pinned_and_verified_before_extraction():
    env = _workflow()["env"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["GITLEAKS_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["GITLEAKS_SHA256"])
    run = _install_run()
    assert "sha256sum -c" in run, "the downloaded tarball must be checked against the pinned digest"
    assert "| tar" not in run, "never stream an unverified download straight into tar"
    assert run.index("sha256sum -c") < run.index("tar -xz")


def test_scan_still_fails_the_job_on_a_finding():
    scan = next(s for s in _steps() if "gitleaks dir" in str(s.get("run", "")))
    assert "--exit-code 1" in scan["run"]
    for step in _steps():
        assert not step.get("continue-on-error"), step.get("name")
    assert "gitleaks/gitleaks-action" not in SECRET_WF.read_text(encoding="utf-8").split("\njobs:", 1)[1]
