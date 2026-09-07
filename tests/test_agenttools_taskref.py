"""Tests for backend-aware task-code validation (``agenttools_taskref``, agent-tools#607).

Every ``gh api`` / ``git remote get-url`` call is mocked via ``monkeypatch.setattr(subprocess,
"run", ...)`` — this suite never touches the network or a real ``gh``/``git`` installation.
Config files (``task.yaml``/``rig.yaml``) are real files on disk under ``tmp_path``, parsed
with real PyYAML (skipped, not failed, when PyYAML is absent — same convention as
``test_agenttools_config.py``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_taskref import (  # noqa: E402
    RepoTaskConfig,
    TaskRefError,
    ValidationResult,
    load_repo_task_config,
    validate,
)


# ── fixtures / fakes ─────────────────────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _no_origin(argv, **kwargs):
    """``git remote get-url origin`` fails — no origin remote / not a git repo."""
    return _FakeProc(returncode=128, stderr="fatal: No such remote 'origin'")


def _origin(owner: str, repo: str):
    def _run(argv, **kwargs):
        return _FakeProc(returncode=0, stdout=f"https://github.com/{owner}/{repo}.git\n")

    return _run


def _gh_found(owner: str, repo: str, number: int):
    url = f"https://github.com/{owner}/{repo}/issues/{number}"

    def _run(argv, **kwargs):
        if argv[0] == "git":
            return _origin(owner, repo)(argv, **kwargs)
        return _FakeProc(returncode=0, stdout=f'"{url}"\n')

    return _run


def _gh_404(argv, **kwargs):
    if argv[0] == "git":
        return _origin("alex-mextner", "agent-tools")(argv, **kwargs)
    return _FakeProc(returncode=1, stderr="gh: Not Found (HTTP 404)")


def _gh_network_error(argv, **kwargs):
    if argv[0] == "git":
        return _origin("alex-mextner", "agent-tools")(argv, **kwargs)
    return _FakeProc(returncode=1, stderr="gh: connection refused")


def _gh_missing(argv, **kwargs):
    if argv[0] == "gh":
        raise FileNotFoundError("gh not found")
    return _origin("alex-mextner", "agent-tools")(argv, **kwargs)


def _fail_if_called(argv, **kwargs):
    raise AssertionError(f"subprocess.run should not have been called with {argv!r}")


@pytest.fixture
def repo(tmp_path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ── github-issues: a real resolvable number passes ─────────────────────────────────────────


def test_github_bare_number_resolves(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _gh_found("alex-mextner", "agent-tools", 607))
    result = validate("607", repo)
    assert result.ok
    assert bool(result) is True
    assert result.canonical == "607"
    assert result.url == "https://github.com/alex-mextner/agent-tools/issues/607"
    assert result.backend == "github-issues"


def test_github_hash_prefixed_form_resolves(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _gh_found("alex-mextner", "agent-tools", 607))
    result = validate("#607", repo)
    assert result.ok
    assert result.canonical == "607"


def test_github_explicit_cross_repo_form_resolves(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _gh_found("alex-mextner", "review-cli", 419))
    result = validate("alex-mextner/review-cli#419", repo)
    assert result.ok
    assert result.canonical == "alex-mextner/review-cli#419"
    assert result.url == "https://github.com/alex-mextner/review-cli/issues/419"


def test_github_explicit_repo_in_task_yaml_used_over_auto(repo, monkeypatch):
    _write(repo / "task.yaml", "backend: github-issues\ngithub:\n  repo: some/other-repo\n")
    monkeypatch.setattr(subprocess, "run", _gh_found("some", "other-repo", 12))
    result = validate("12", repo)
    assert result.ok
    assert result.canonical == "12"


# ── github-issues: rejections ───────────────────────────────────────────────────────────────


def test_github_free_text_slug_rejected_without_network_call(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("ambiguous-create-dedup", repo)
    assert not result.ok
    assert result.backend == "github-issues"
    assert "task find" in result.reason
    assert "task new" in result.reason


def test_github_bare_placeholder_rejected(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    for placeholder in ("CODE", "TEST-1"):
        result = validate(placeholder, repo)
        assert not result.ok, placeholder
        assert "task find" in result.reason


def test_github_numeric_shaped_but_nonexistent_rejected(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _gh_404)
    result = validate("999999", repo)
    assert not result.ok
    assert "does not exist" in result.reason
    assert "task find" in result.reason


def test_github_unverifiable_does_not_claim_nonexistence(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _gh_network_error)
    result = validate("607", repo)
    assert not result.ok
    assert "does not exist" not in result.reason
    assert "task find" not in result.reason  # must not tell the user it's fake -- it might be real
    assert "could not verify" in result.reason


def test_github_gh_missing_rejected_cleanly(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _gh_missing)
    result = validate("607", repo)
    assert not result.ok
    assert "gh CLI not found" in result.reason


def test_github_no_origin_and_no_config_rejected_not_crashed(repo, monkeypatch):
    """The literal 'no task.yaml present' case: a plain, non-git tmp_path with zero config."""
    monkeypatch.setattr(subprocess, "run", _no_origin)
    result = validate("607", repo)
    assert not result.ok
    assert "cannot resolve github repo" in result.reason


def test_empty_code_rejected(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("   ", repo)
    assert not result.ok
    assert "empty" in result.reason


# ── linear ───────────────────────────────────────────────────────────────────────────────────


def test_linear_correct_prefix_passes(repo, monkeypatch):
    _write(repo / "task.yaml", "backend: linear\nlinear:\n  team: HYP\n")
    monkeypatch.setattr(subprocess, "run", _fail_if_called)  # linear path never shells out
    result = validate("HYP-1234", repo)
    assert result.ok
    assert result.canonical == "HYP-1234"
    assert result.backend == "linear"
    assert result.url is None


def test_linear_wrong_prefix_names_correct_one(repo, monkeypatch):
    _write(repo / "task.yaml", "backend: linear\nlinear:\n  team: HYP\n")
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("RIG-CLI-298-VERSION-BUMP", repo)  # the real incident from this session
    assert not result.ok
    assert result.backend == "linear"

    result2 = validate("ABC-12", repo)
    assert not result2.ok
    assert "HYP" in result2.reason


def test_linear_lowercase_prefix_matches_case_insensitively(repo, monkeypatch):
    _write(repo / "task.yaml", "backend: linear\nlinear:\n  team: HYP\n")
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("hyp-42", repo)
    assert result.ok
    assert result.canonical == "HYP-42"


def test_linear_no_team_configured_rejected(repo, monkeypatch):
    _write(repo / "task.yaml", "backend: linear\n")
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("HYP-1", repo)
    assert not result.ok
    assert "team key" in result.reason


def test_rig_yaml_task_block_selects_linear(repo, monkeypatch):
    _write(repo / "rig.yaml", "task:\n  backend: linear\n  team: HYP\n")
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("HYP-7", repo)
    assert result.ok
    cfg = load_repo_task_config(repo)
    assert cfg.backend == "linear"
    assert cfg.linear_team == "HYP"
    assert cfg.sources == ("builtin-defaults", "rig.yaml")


def test_task_yaml_overrides_rig_yaml(repo, monkeypatch):
    _write(repo / "rig.yaml", "task:\n  backend: linear\n  team: WRONG\n")
    _write(repo / "task.yaml", "backend: linear\nlinear:\n  team: HYP\n")
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    cfg = load_repo_task_config(repo)
    assert cfg.linear_team == "HYP"
    assert cfg.sources == ("builtin-defaults", "rig.yaml", "task.yaml")


# ── malformed config: fail-closed for task.yaml, tolerated for rig.yaml ────────────────────


def test_malformed_task_yaml_rejected_not_crashed(repo, monkeypatch):
    _write(repo / "task.yaml", "backend: [unclosed\n")  # unclosed flow sequence -> YAMLError
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    result = validate("607", repo)
    assert not result.ok
    assert result.backend is None  # config never resolved at all


def test_load_repo_task_config_raises_for_malformed_task_yaml(repo):
    _write(repo / "task.yaml", "backend: [unclosed\n")
    with pytest.raises(TaskRefError):
        load_repo_task_config(repo)


def test_malformed_rig_yaml_is_tolerated_like_a_foreign_file(repo, monkeypatch):
    _write(repo / "rig.yaml", "task: [unclosed\n")
    monkeypatch.setattr(subprocess, "run", _no_origin)
    # Falls through to the same zero-config default as if rig.yaml did not exist at all --
    # the rejection is "cannot resolve", never a rig.yaml parse-error crash/leak.
    result = validate("607", repo)
    assert not result.ok
    assert "cannot resolve github repo" in result.reason


# ── dataclasses ──────────────────────────────────────────────────────────────────────────────


def test_repo_task_config_defaults_with_zero_files(repo):
    cfg = load_repo_task_config(repo)
    assert cfg == RepoTaskConfig(
        backend="github-issues", github_repo="auto", linear_team="", sources=("builtin-defaults",)
    )


def test_validation_result_is_frozen_and_truthy():
    ok = ValidationResult(True, "607", "607", "https://x", "github-issues", "ok")
    bad = ValidationResult(False, "x", None, None, None, "no")
    assert bool(ok) is True
    assert bool(bad) is False
    with pytest.raises(Exception):
        ok.ok = False  # frozen dataclass
