"""Catalog CI scripts and workflow templates stay shellcheck/actionlint clean — agent-tools#1036.

Every `ci/<slot>/workflow*.yml` template is copied verbatim into consumer repos by rig. A
consumer running actionlint (which runs shellcheck over each `run:` script) on its managed copy
cannot fix a finding by hand — the next `rig apply` rewrites the file — so findings are fixed
here, at the source. #1036 fixed SC2155 (declare+assign `local re=...$(...)`) in the codeql
selfgate `is_source_suppressed` function.

  1. lint gate — shellcheck over every ci shell script and actionlint over every ci workflow
     template (skipped only when the tool is not installed).
  2. behavior  — the SC2155 rewrite keeps the selfgate suppression semantics.

Run::

    uv run --with pytest --with pyyaml python -m pytest tests/test_ci_shell_lint_1036.py -q
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI = REPO_ROOT / "ci"
CODEQL_SELFGATE = CI / "codeql" / "workflow-selfgate.yml"
CI_WORKFLOWS = sorted(CI.glob("*/workflow*.yml"))
CI_SCRIPTS = sorted(CI.glob("*/*.sh"))
# The Python modules the four launchers exec. Consumers lint them with their own ruff config:
# ruff defaults (88 columns) or e.g. line-length 100 + target py38 (ExpenseSyncBot ruff.toml).
GATE_MODULES = [
    CI / "dependency-review" / "dep_audit.py",
    CI / "leftover-grep" / "leftover_grep.py",
    CI / "review-threads" / "review_threads.py",
    CI / "secret-scan" / "secret_scan.py",
]
RUFF_CONFIGS = {"defaults": [], "line100-py38": ["--line-length", "100", "--target-version", "py38"]}


# ---------------------------------------------------------------------------
# 1. Lint gate — the catalog's workflow templates stay clean.
# ---------------------------------------------------------------------------


def test_workflow_glob_is_not_vacuous():
    """A renamed directory must not silently empty the parametrized gate."""
    assert CODEQL_SELFGATE in CI_WORKFLOWS
    assert CI / "tests" / "workflow.yml" in CI_WORKFLOWS
    for name in ("dependency-review/dep-audit.sh", "leftover-grep/leftover-grep.sh",
                 "review-threads/review-threads.sh", "secret-scan/secret-scan.sh"):
        assert CI / name in CI_SCRIPTS


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", CI_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_ci_script_is_shellcheck_clean(script: Path):
    """Consumers run shellcheck on their vendored copies and cannot fix a finding by hand."""
    res = subprocess.run(["shellcheck", "--", str(script)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
@pytest.mark.parametrize("config", RUFF_CONFIGS)
@pytest.mark.parametrize("module", GATE_MODULES, ids=lambda p: p.name)
def test_gate_module_is_ruff_clean_under_consumer_configs(module: Path, config: str):
    """Written so BOTH 88 and 100 column formatting leave the file unchanged (magic trailing commas
    and short strings), since vendored copies get linted with whatever config the consumer has."""
    extra = RUFF_CONFIGS[config]
    for argv in (["check"], ["format", "--check"]):
        res = subprocess.run(["ruff", *argv, "--isolated", *extra, str(module)], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("module", GATE_MODULES, ids=lambda p: p.name)
def test_gate_module_runs_on_python38(module: Path):
    """Deploy hosts may run python3.8: no 3.9+ syntax and no str.removeprefix/removesuffix."""
    tree = ast.parse(module.read_text(encoding="utf-8"), feature_version=(3, 8))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"removeprefix", "removesuffix"}


@pytest.mark.skipif(
    shutil.which("actionlint") is None or shutil.which("shellcheck") is None,
    reason="needs actionlint and shellcheck (actionlint lints run: scripts through shellcheck)",
)
@pytest.mark.parametrize("workflow", CI_WORKFLOWS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_ci_workflow_template_is_actionlint_clean(workflow: Path):
    res = subprocess.run(["actionlint", "-no-color", "--", str(workflow)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr


# ---------------------------------------------------------------------------
# 2. Behavior — codeql selfgate suppression matching (SC2155 rewrite).
# ---------------------------------------------------------------------------


def _gate_script() -> str:
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(CODEQL_SELFGATE.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["analyze"]["steps"] if s.get("name") == "Gate on SARIF findings")
    return step["run"]


def _result(rule: str, line: int, uri: str = "src/x.js") -> dict:
    return {
        "ruleId": rule,
        "message": {"text": rule},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": line}}}],
    }


def _run_gate(tmp_path: Path, source: str, rules: dict[str, str], results: list[dict]):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "x.js").write_text(source, encoding="utf-8")
    (tmp_path / "sarif-results").mkdir(exist_ok=True)
    sarif = {"runs": [{
        "tool": {"driver": {"name": "CodeQL", "rules": [
            {"id": rid, "defaultConfiguration": {"level": lvl}} for rid, lvl in rules.items()]}},
        "results": results,
    }]}
    (tmp_path / "sarif-results" / "js.sarif").write_text(json.dumps(sarif), encoding="utf-8")
    script = tmp_path / "gate.sh"
    script.write_text(_gate_script(), encoding="utf-8")
    env = dict(os.environ, LANGUAGE="javascript-typescript", GATE_LEVELS="error warning",
               GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"))
    return subprocess.run(["bash", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


def test_codeql_gate_marker_in_comment_block_above_suppresses(tmp_path: Path):
    """The marker may sit anywhere in the contiguous comment block directly above the line."""
    src = "a();\n// codeql[js/bad]\n// why: reviewed, input is a constant\nb();\n"
    proc = _run_gate(tmp_path, src, {"js/bad": "error"}, [_result("js/bad", 4)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "gated=0, suppressed=1" in proc.stdout


def test_codeql_gate_marker_does_not_leak_across_a_blank_line(tmp_path: Path):
    src = "a();\n// codeql[js/bad]\n\nb();\n"
    proc = _run_gate(tmp_path, src, {"js/bad": "error"}, [_result("js/bad", 4)])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "[error] js/bad" in proc.stdout


def test_codeql_gate_rule_id_is_matched_literally_not_as_a_regex(tmp_path: Path):
    """`codeql[js/a.b]` suppresses js/a.b only; the `.` must not match any character."""
    src = "// codeql[js/a.b]\nx();\n// codeql[js/a.b]\ny();\n"
    ok = _run_gate(tmp_path, src, {"js/a.b": "error"}, [_result("js/a.b", 2)])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    other = _run_gate(tmp_path, src, {"js/axb": "error"}, [_result("js/axb", 4)])
    assert other.returncode == 1, other.stdout + other.stderr
    assert "[error] js/axb" in other.stdout
