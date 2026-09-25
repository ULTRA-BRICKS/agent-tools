"""CI catalog gates must run on Forgejo / Gitea runners, not only on GitHub-hosted ones.

The ultrabricks repos run these workflows on Forgejo Actions (GARM pool, stock Ubuntu image,
jobs run as a NON-root user). Four GitHub-only assumptions made the gates red on every PR:

  * secret-scan untarred gitleaks into /usr/local/bin (not writable by a non-root job user);
  * dependency-review needed pip-audit for any python manifest but never installed it (and
    the hand-added `pipx install pip-audit` fails: no pipx on the image);
  * pr-title-lint used amannn/action-semantic-pull-request, which calls the GitHub REST API
    and crashes on Forgejo ("Cannot read properties of undefined (reading 'login')");
  * review-threads needed gh + GraphQL (covered in tests/test_review_threads_gate.py).

Run from the repo root::

    uv run --with pytest --with pyyaml python -m pytest tests/test_ci_forgejo_runner_compat.py -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_WF = REPO_ROOT / "ci" / "secret-scan" / "secret-scan.yml"
DEP_WF = REPO_ROOT / "ci" / "dependency-review" / "workflow.yml"
TITLE_WF = REPO_ROOT / "ci" / "pr-title-lint" / "workflow.yml"


def _on_block(wf: dict) -> dict:
    return wf[True] if True in wf else wf["on"]


def _steps(path: Path, job: str) -> list[dict]:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"][job]["steps"]


# --- secret-scan ---------------------------------------------------------------------------


def test_secret_scan_installs_gitleaks_into_a_user_writable_dir_on_path():
    step = next(s for s in _steps(SECRET_WF, "gitleaks") if "gitleaks" in str(s.get("run", "")) and "curl" in str(s.get("run", "")))
    run = step["run"]
    assert "/usr/local/bin" not in run, "a non-root runner user cannot write /usr/local/bin"
    assert "RUNNER_TEMP" in run
    assert '>> "$GITHUB_PATH"' in run, "the install dir must be put on PATH for the scan step"


# --- dependency-review ---------------------------------------------------------------------


def test_dependency_review_installs_pip_audit_without_pipx_before_the_pr_fetch():
    steps = _steps(DEP_WF, "dependency-audit")
    names = [s.get("name", "") for s in steps]
    setup = next(i for i, s in enumerate(steps) if "setup-python" in str(s.get("uses", "")))
    install = next(i for i, s in enumerate(steps) if "pip-audit" in str(s.get("run", "")))
    fetch = next(i for i, n in enumerate(names) if n.startswith("Fetch the PR head"))
    assert setup < install < fetch, "the toolchain must be installed before any PR data is fetched"
    assert re.search(r"@[0-9a-f]{40}\b", steps[setup]["uses"]), "setup-python must be SHA-pinned"
    run = steps[install]["run"]
    assert "pipx" not in run, "pipx is not on every runner image"
    assert re.search(r"pip-audit==\$\{PIP_AUDIT_VERSION\}", run)
    assert re.fullmatch(r"\d+\.\d+\.\d+", steps[install]["env"]["PIP_AUDIT_VERSION"]), "pin pip-audit"
    assert '>> "$GITHUB_PATH"' in run
    for i in (setup, install):
        cond = steps[i].get("if", "")
        for manifest in ("pyproject.toml", "requirements*.txt", "uv.lock", "poetry.lock"):
            assert manifest in cond, f"python toolchain gated on the manifests dep-audit.sh detects ({manifest})"


# --- pr-title-lint -------------------------------------------------------------------------


def test_pr_title_lint_uses_no_action_and_no_api_token():
    steps = _steps(TITLE_WF, "pr-title-lint")
    assert all("uses" not in s for s in steps), "no third-party action (the amannn one needs the GitHub API)"
    code = TITLE_WF.read_text(encoding="utf-8")
    assert "GITHUB_EVENT_PATH" in code
    assert "secrets." not in code and "GITHUB_TOKEN" not in code


def test_pr_title_lint_keeps_pull_request_target_and_adds_manual_dispatch():
    yaml = pytest.importorskip("yaml")
    on = _on_block(yaml.safe_load(TITLE_WF.read_text(encoding="utf-8")))
    assert "pull_request_target" in on
    assert on["workflow_dispatch"]["inputs"]["title"]["required"] is True


def _lint(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess:
    step = _steps(TITLE_WF, "pr-title-lint")[0]
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload), encoding="utf-8")
    env = dict(os.environ, GITHUB_EVENT_PATH=str(event), **{k: str(v) for k, v in step["env"].items()})
    return subprocess.run(["bash", "-c", step["run"]], capture_output=True, text=True, env=env, timeout=60)


needs_shell = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                                 reason="needs bash + python3")


@needs_shell
@pytest.mark.parametrize("title", [
    "feat: add pagination",
    "fix(api): handle empty pages",
    "ci(dependency-review)!: install pip-audit",
    "chore(deps,ci): bump pins",
    "refactor: rename Foo to Bar",
])
def test_pr_title_lint_accepts_conventional_titles(tmp_path: Path, title: str):
    res = _lint(tmp_path, {"pull_request": {"title": title}})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "PR title OK" in res.stdout


@needs_shell
@pytest.mark.parametrize("title,needle", [
    ("add pagination", "No release type found"),
    ("feature: add pagination", 'Unknown release type "feature"'),
    ("Feat: add pagination", 'Unknown release type "Feat"'),
    ("feat: Add pagination", "must start lowercase"),
    ("feat(api): add pagination.", "not%0Aend with a period"),
    ("feat:add pagination", "No release type found"),
])
def test_pr_title_lint_rejects_bad_titles(tmp_path: Path, title: str, needle: str):
    res = _lint(tmp_path, {"pull_request": {"title": title}})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "::error::" in res.stdout
    assert needle in res.stdout


@needs_shell
def test_pr_title_lint_reads_the_manual_dispatch_input(tmp_path: Path):
    assert _lint(tmp_path, {"inputs": {"title": "docs: explain the gate"}}).returncode == 0
    assert _lint(tmp_path, {"inputs": {"title": "Docs: nope"}}).returncode == 1


@needs_shell
def test_pr_title_lint_fails_closed_without_a_title(tmp_path: Path):
    res = _lint(tmp_path, {"pull_request": {}})
    assert res.returncode == 1
    assert "failing closed" in res.stdout
