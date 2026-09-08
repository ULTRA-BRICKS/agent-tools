"""Tests for the agent-tools#129 CI-gate fixes — the SHIPPED catalog gate scripts/workflows
that rig copies verbatim into every consumer, so a bug here is a bug in every rigged repo.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ci_gate_bugs_129.py -q

Three classes of bug were fixed; each is asserted here against the REAL scripts/workflows:

1. dependency-review (SECURITY): the blocking gate ran the PR-checked-out script under plain
   `pull_request`, so a malicious PR could weaken the very gate it must pass. Fixed to the
   trusted-base `pull_request_target` model (mirrors leftover-grep / review-threads): the
   base-branch script runs; the PR's lockfiles are audited as DATA in a side worktree. We
   assert the workflow uses pull_request_target, runs the script from $GITHUB_WORKSPACE
   (the trusted base checkout), never `npm install`/`bun install`/checks out PR head onto the
   workspace, and that dep-audit.sh accepts an audit-dir arg and fails closed on a missing dir.
   The SECOND half of the bug: `setup-bun` was commented out, so a `bun.lock` repo fail-CLOSED
   (no `bun` on PATH -> dep-audit.sh's miss() reds CI). Fixed by shipping setup-bun ENABLED and
   SHA-pinned; we assert it is an active (uncommented), SHA-pinned step.

2. leftover-grep: a shallow head broke the three-dot merge-base diff and the script read the
   diff via a process substitution that swallowed `git diff` errors -> a block-tier gate could
   silently PASS having scanned nothing. Fixed to materialize the lines + check $? (fail
   closed), fail closed on an explicitly-requested base that doesn't resolve, and the workflow
   deepens the head fetch + verifies a merge-base. agent-tools#130 then closes the remaining
   gap: rather than fatal-and-block EVERY shallow PR on the unreachable three-dot merge base,
   the script falls back to the two-dot `base..HEAD` diff (no merge base needed) and keeps
   scanning; only a truly uncomputable head (a missing object) still fails closed. Also the
   `=======` conflict-marker false positive on a 7-`=` source separator is dropped (start/end
   markers still catch a real conflict). We exercise the real script for each.

3. codeql self-gate: the language-detect `git ls-files | grep -qiE` under `pipefail` returns
   141 (SIGPIPE) when grep matches early -> the `if` takes the else branch -> CodeQL silently
   SKIPS a language whose source IS present (the gate self-disables). Fixed to materialize the
   file list and grep a here-string with no -q pipe. We assert the buggy pattern is gone and
   reproduce that the new pattern detects.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# NOTE: `yaml` is imported per-test via importorskip (NOT at module level) so the many
# behavioral tests below — which exercise the real shipped shell scripts and need no YAML
# parser — still RUN in CI, where the dependency-free `uv run --with pytest` env has no
# PyYAML. Only the two structural tests that parse a workflow with yaml.safe_load skip when
# it's absent. (The repo's lib is stdlib-only at import time; PyYAML is a lazy/optional dep.)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEP_AUDIT = REPO_ROOT / "ci" / "dependency-review" / "dep-audit.sh"
DEP_WF = REPO_ROOT / "ci" / "dependency-review" / "workflow.yml"
LEFTOVER = REPO_ROOT / "ci" / "leftover-grep" / "leftover-grep.sh"
LEFTOVER_WF = REPO_ROOT / "ci" / "leftover-grep" / "workflow.yml"
CODEQL_WF = REPO_ROOT / "ci" / "codeql" / "workflow-selfgate.yml"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    # `--no-verify` bypasses any machine-global git hooks — these are throwaway fixtures.
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", msg],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _run(script: Path, cwd: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    # Invoke via bash, matching how CI runs these scripts (leftover-grep.yml: `bash …`). The
    # scripts carry a `#!/usr/bin/env bash` shebang and use bash-only constructs
    # (`set -o pipefail`, `IFS=$'\t'`). Running them through `sh` would pass on macOS (where
    # /bin/sh is bash) but FAIL on the Ubuntu CI runner (where /bin/sh is dash) — a false
    # green locally that goes red in CI. dep-audit.sh is POSIX-clean and works under bash too.
    proc = subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _stub_pip_audit(bin_dir: Path, exit_code: int = 0) -> None:
    """Fake ``pip-audit`` on PATH that ECHOES its own argv with a distinctive marker (so a test
    can assert the REAL invocation's flags, not the script's `note` echo) and exits *exit_code*."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "pip-audit"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "STUB_PIP_AUDIT_ARGV:[%s]\\n" "$*" >&2\n'
        f"exit {exit_code}\n"
    )
    os.chmod(stub, 0o755)


# ---------------------------------------------------------------------------
# 1. dependency-review — tamper-resistant trusted-base model.
# ---------------------------------------------------------------------------

def test_dependency_review_runs_under_pull_request_target():
    """The blocking gate must run under pull_request_target (base-trusted), not plain
    pull_request (where the PR can edit the gate script it has to pass)."""
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(DEP_WF.read_text())
    on = wf[True] if True in wf else wf["on"]  # PyYAML parses bare `on:` as boolean True
    assert "pull_request_target" in on, "dependency-review must use pull_request_target"
    assert "pull_request" not in on, "must NOT also trigger plain pull_request (PR-trusted)"


def test_dependency_review_runs_trusted_base_script_not_pr_copy():
    """The run: must invoke the script from $GITHUB_WORKSPACE — the trusted base checkout —
    so the PR's edited copy never runs."""
    text = DEP_WF.read_text()
    assert "$GITHUB_WORKSPACE/ci/dependency-review/dep-audit.sh" in text


def _executable_lines(text: str) -> str:
    """Lines that aren't YAML comments — so an assertion can't be fooled by a forbidden
    pattern quoted inside an explanatory `#` comment."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _permissions_blocks(text: str) -> list[list[str]]:
    """Return every YAML permissions mapping block, including job-level overrides."""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)permissions:\s*(.*)$", line)
        if not match:
            continue
        inline = match.group(2).strip()
        if inline:
            blocks.append([inline])
            continue
        indent = len(match.group(1))
        block: list[str] = []
        for next_line in lines[i + 1:]:
            if not next_line.strip() or next_line.lstrip().startswith("#"):
                continue
            next_indent = len(next_line) - len(next_line.lstrip())
            if next_indent <= indent:
                break
            block.append(next_line.split("#", 1)[0].strip())
        blocks.append(block)
    return blocks


def test_dependency_review_never_executes_pr_code():
    """Hard rule: no install/build step and no checkout of the PR head onto the workspace
    under the privileged trigger — that would execute PR-controlled code. Asserted against
    executable (non-comment) lines so the documented prohibition doesn't trip the check."""
    code = _executable_lines(DEP_WF.read_text())
    assert "npm install" not in code
    assert "bun install" not in code
    assert "npm ci" not in code
    text = DEP_WF.read_text()
    # The PR head is fetched as a git object + side worktree, never checked out onto the tree.
    assert "git fetch --no-tags --depth=1 origin" in text
    assert "git worktree add --detach" in text


def test_dependency_review_installs_bun_toolchain():
    """The second half of agent-tools#129: a `bun.lock` repo fail-CLOSED because `setup-bun`
    was commented out (dep-audit.sh found the lockfile, couldn't find `bun`, and red'd CI).
    The toolchain must now ship ENABLED (an active `uses:` step, not a comment) and SHA-pinned
    so the audit actually RUNS instead of fail-closing."""
    uses = [
        ln.strip()
        for ln in DEP_WF.read_text().splitlines()
        if not ln.lstrip().startswith("#")
        and ln.lstrip().startswith("uses:")
        and "setup-bun" in ln
    ]
    assert uses, "setup-bun must be an active (uncommented) `uses:` step, not a comment example"
    for ln in uses:
        assert re.search(r"@[0-9a-f]{40}\b", ln), f"setup-bun must be SHA-pinned: {ln!r}"


@pytest.mark.parametrize("workflow", [DEP_WF, LEFTOVER_WF])
def test_dependency_and_leftover_workflows_are_read_only_without_explicit_secret_env(workflow: Path):
    """The #130 trusted-base gates stay least-privileged: read-only permissions and no explicit
    secret env. PR data fetches rely on actions/checkout's persisted read-only git credential,
    not a GH_TOKEN env var, gh CLI call, or tokenized URL."""
    text = workflow.read_text()
    permissions = _permissions_blocks(text)
    assert permissions, "workflow must declare explicit read-only permissions"
    assert any("contents: read" in block for block in permissions), permissions
    for block in permissions:
        assert "write-all" not in block, block
        assert all(ln.endswith(": read") for ln in block), block

    code = _executable_lines(text)
    assert "env:" in code, "_executable_lines must include YAML env blocks for this guard"
    if "with:" in text:
        assert "with:" in code, "_executable_lines must include YAML with blocks for this guard"
    assert "secrets." not in code
    assert "GH_TOKEN" not in code
    assert "persist-credentials: false" not in code
    assert not re.search(r"(?m)^\s*gh\s+", code), "fetch must not rely on gh CLI auth"
    assert "x-access-token" not in code


def test_dep_audit_fails_closed_on_bun_lock_without_bun(tmp_path: Path):
    """Proves WHY setup-bun must be installed: a bun.lock tree with no `bun` on PATH fails
    CLOSED (rc 1) — exactly the red CI the toolchain install removes. DEP_AUDIT_ALLOW_MISSING=1
    is the only intentional escape."""
    tree = tmp_path / "bunrepo"
    tree.mkdir()
    (tree / "bun.lock").write_text("# lockfile\n")
    env = dict(os.environ, PATH="/usr/bin:/bin")  # no bun resolvable
    proc = subprocess.run(
        ["bash", str(DEP_AUDIT), str(tree)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "bun" in (proc.stdout + proc.stderr).lower()
    proc_open = subprocess.run(
        ["bash", str(DEP_AUDIT), str(tree)],
        env=dict(env, DEP_AUDIT_ALLOW_MISSING="1"), capture_output=True, text=True, timeout=60,
    )
    assert proc_open.returncode == 0, proc_open.stdout + proc_open.stderr


def test_dep_audit_accepts_audit_dir_and_fails_closed_on_missing(tmp_path: Path):
    """dep-audit.sh takes the audit dir as $1 and fails CLOSED if it doesn't exist — a
    vanished audit target must not masquerade as 'no manifests, nothing to audit'."""
    rc, out = _run(DEP_AUDIT, tmp_path, str(tmp_path / "does-not-exist"))
    assert rc == 1, out
    assert "does not exist" in out


def test_dep_audit_empty_tree_passes(tmp_path: Path):
    """No supported manifest in the audited dir -> nothing to audit, clean pass (rc 0)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, out = _run(DEP_AUDIT, tmp_path, str(empty))
    assert rc == 0, out
    assert "nothing to audit" in out


def test_dep_audit_pip_audit_is_not_resolving():
    """SECURITY (agent-tools#129): under pull_request_target dep-audit.sh runs against the PR
    tree as DATA. pip-audit is the one auditor that can execute input — a resolving run
    (`-r`/`-e`/`--requirement` without `--no-deps`) builds the PR's sdists (runs setup.py) ->
    arbitrary PR code under a privileged trigger (RCE). Pin that the invocation never gains a
    resolving flag without `--no-deps`, so a future edit can't silently reopen the hole."""
    code = _executable_lines(DEP_AUDIT.read_text())
    for m in re.finditer(r"pip-audit\b[^\n|;]*(?:\s-r\b|\s-e\b|--requirement\b)", code):
        assert "--no-deps" in m.group(0), (
            f"resolving pip-audit without --no-deps (RCE under pull_request_target): {m.group(0)!r}"
        )


def test_dep_audit_python_audits_pinned_requirements_as_data(tmp_path: Path):
    """COVERAGE (agent-tools#131 / Codex P1): the Python path must audit the audited TREE's
    requirements*.txt as DATA — `pip-audit --no-deps -r <file>` — not the runner's installed
    environment via a bare `pip-audit`. We stub pip-audit so the assertion proves the REAL argv
    the script passed (`--no-deps -r requirements.txt`), distinct from the script's `note` echo."""
    tree = tmp_path / "pyreq"
    tree.mkdir()
    (tree / "requirements.txt").write_text("flask==2.0.0\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements.txt]" in out, out
    # The bare no-arg form (audits the runner env, misses the PR's deps) must never run.
    assert "STUB_PIP_AUDIT_ARGV:[]" not in out, f"bare pip-audit (no args) must not run:\n{out}"


def test_dep_audit_python_audits_every_requirements_file(tmp_path: Path):
    """Multiple manifests: each `requirements*.txt` (e.g. `-dev`) is audited as data, so a vuln
    declared only in a secondary requirements file is not missed."""
    tree = tmp_path / "pymulti"
    tree.mkdir()
    (tree / "requirements.txt").write_text("requests==2.31.0\n")
    (tree / "requirements-dev.txt").write_text("pytest==7.0.0\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements.txt]" in out, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements-dev.txt]" in out, out


def test_dep_audit_python_requirements_vuln_fails(tmp_path: Path):
    """A non-zero pip-audit (a found advisory) on a requirements file must FAIL the gate."""
    tree = tmp_path / "pyvuln"
    tree.mkdir()
    (tree / "requirements.txt").write_text("badpkg==1.0.0\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=1)  # pip-audit exits non-zero when it finds a vuln
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, out


def test_dep_audit_pyproject_only_fails_closed(tmp_path: Path):
    """SECURITY/COVERAGE (agent-tools#131 / task-cli#144): a pyproject.toml with NO pinned
    requirements*.txt and no uv.lock/poetry.lock can be audited only by BUILDING the project
    (RCE under pull_request_target), so it fails CLOSED rather than falling back to the env-only
    `pip-audit` (which would audit the runner's packages, not the PR's). pip-audit is present on
    PATH but must NOT be invoked for a project-only tree. DEP_AUDIT_ALLOW_MISSING=1 is the escape."""
    tree = tmp_path / "pyproj"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, out
    assert "no pinned requirements" in out
    assert "STUB_PIP_AUDIT_ARGV" not in out, "pip-audit must NOT run for a project-only tree"
    rc_ok, _ = _run(
        DEP_AUDIT, tmp_path, str(tree),
        env_extra={"PATH": path, "DEP_AUDIT_ALLOW_MISSING": "1"},
    )
    assert rc_ok == 0, "DEP_AUDIT_ALLOW_MISSING=1 must turn the project-only fail-closed into a skip"


# uv.lock [[package]] shape copied from rig-cli (registry pin vs local editable).
_UV_LOCK_REGISTRY = (
    "version = 1\n"
    "\n"
    "[[package]]\n"
    'name = "colorama"\n'
    'version = "0.4.6"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
)
_UV_LOCK_EDITABLE_ONLY = (
    "version = 1\n"
    "\n"
    "[[package]]\n"
    'name = "mypkg"\n'
    'version = "0.1.0"\n'
    'source = { editable = "." }\n'
)
_POETRY_LOCK_PYPI = (
    "[[package]]\n"
    'name = "colorama"\n'
    'version = "0.4.6"\n'
    'description = "Cross-platform colored terminal text."\n'
    "optional = false\n"
    'python-versions = "*"\n'
)


def _assert_lockfile_data_audit_argv(out: str) -> None:
    """Lockfile pins are written to a temp -r file: --no-deps -r, never the lock/project path."""
    stub = [ln for ln in out.splitlines() if "STUB_PIP_AUDIT_ARGV:" in ln]
    assert stub, f"pip-audit must run for an auditable lockfile:\n{out}"
    joined = "\n".join(stub)
    assert "--no-deps -r" in joined, out
    assert "uv.lock" not in joined, out
    assert "poetry.lock" not in joined, out
    assert "pyproject.toml" not in joined, out
    assert "STUB_PIP_AUDIT_ARGV:[]" not in out, f"bare pip-audit must not run:\n{out}"


def test_dep_audit_python_uv_lock_registry_audits_as_data(tmp_path: Path):
    """task-cli#144: pyproject + uv.lock with a registry pin is auditable as data (no build)."""
    tree = tmp_path / "pyuv"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    (tree / "uv.lock").write_text(_UV_LOCK_REGISTRY)
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    _assert_lockfile_data_audit_argv(out)


def test_dep_audit_python_uv_lock_registry_vuln_fails(tmp_path: Path):
    """A non-zero pip-audit on uv.lock-extracted pins must FAIL the gate."""
    tree = tmp_path / "pyuvvuln"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    (tree / "uv.lock").write_text(_UV_LOCK_REGISTRY)
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=1)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, out
    _assert_lockfile_data_audit_argv(out)


def test_dep_audit_python_uv_lock_editable_only_fails_closed(tmp_path: Path):
    """uv.lock whose only [[package]] is the local editable project has no registry pin to
    audit as data — fail closed, and do not invoke pip-audit (nothing safe to pass it)."""
    tree = tmp_path / "pyuvedit"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    (tree / "uv.lock").write_text(_UV_LOCK_EDITABLE_ONLY)
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, out
    assert "STUB_PIP_AUDIT_ARGV" not in out, f"pip-audit must NOT run for editable-only lock:\n{out}"


def test_dep_audit_python_lockfile_does_not_invoke_uv_or_poetry(tmp_path: Path):
    """Parse lockfiles as text; never shell out to uv/poetry (export would resolve/build)."""
    tree = tmp_path / "pynoinvo"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    (tree / "uv.lock").write_text(_UV_LOCK_REGISTRY)
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    for name, marker in (("uv", "UV_INVOKED"), ("poetry", "POETRY_INVOKED")):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{marker}' >&2\nexit 1\n")
        os.chmod(stub, 0o755)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "UV_INVOKED" not in out, out
    assert "POETRY_INVOKED" not in out, out
    _assert_lockfile_data_audit_argv(out)


def test_dep_audit_python_poetry_lock_pypi_audits_as_data(tmp_path: Path):
    """poetry.lock [[package]] name/version with no [package.source] is a PyPI pin — data audit."""
    tree = tmp_path / "pypoetry"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[tool.poetry]\nname='x'\nversion='0.1.0'\n")
    (tree / "poetry.lock").write_text(_POETRY_LOCK_PYPI)
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    _assert_lockfile_data_audit_argv(out)



# A requirements line that forces pip-audit to BUILD the input (RCE under pull_request_target).
# `--no-deps` does NOT prevent the build of a DIRECT reference, so dep-audit.sh must refuse the
# file before invoking pip-audit. Each row is a single dangerous line.
_BUILD_TRIGGER_LINES = [
    "-e .",                                     # editable local
    "--editable ./pkg",                         # editable local (long form)
    "./localpkg",                               # local path
    "/abs/localpkg",                            # absolute local path
    "git+https://github.com/evil/pkg",          # VCS
    "evilpkg @ https://example.com/pkg.tar.gz",  # PEP 508 direct URL
    "https://example.com/pkg-1.0.tar.gz",       # bare URL
    "-r other-requirements.txt",                # include (could smuggle any of the above)
    "-c constraints.txt",                       # constraint include
    "--index-url https://evil.example/simple",  # foreign package index
    "flask>=2.0",                               # unpinned (can't audit as data)
    "flask",                                    # bare name (unpinned)
    "flask==2.*",                               # prefix pin, not an exact pin (review P3)
]


@pytest.mark.parametrize("danger", _BUILD_TRIGGER_LINES)
def test_dep_audit_python_refuses_build_triggering_requirement(tmp_path: Path, danger: str):
    """SECURITY (agent-tools#131 review P1): a PR-controlled requirements file with a DIRECT
    reference (editable / URL / VCS / `@ url` / local path / `-r` include) or an UNPINNED spec
    would make pip-audit BUILD it (RCE under pull_request_target). `--no-deps` does not stop a
    direct-entry build, so dep-audit.sh must fail CLOSED and NOT invoke pip-audit at all."""
    tree = tmp_path / "pybad"
    tree.mkdir()
    (tree / "requirements.txt").write_text(f"# pinned ok\nrequests==2.31.0\n{danger}\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)  # present, but must NOT be called for an unsafe file
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, f"a build-triggering line {danger!r} must fail closed:\n{out}"
    assert "STUB_PIP_AUDIT_ARGV" not in out, (
        f"pip-audit must NOT run on a file with a build-triggering line {danger!r}:\n{out}"
    )
    # The explicit escape hatch downgrades it to a skip.
    rc_ok, _ = _run(
        DEP_AUDIT, tmp_path, str(tree),
        env_extra={"PATH": path, "DEP_AUDIT_ALLOW_MISSING": "1"},
    )
    assert rc_ok == 0, f"DEP_AUDIT_ALLOW_MISSING=1 must skip the refused file {danger!r}"


def test_dep_audit_python_accepts_hashed_pinned_requirements(tmp_path: Path):
    """A `pip-compile --generate-hashes` style file (pinned spec + multi-line `--hash`, markers,
    extras) is data-safe and IS audited — the scan must not false-reject the common pinned form."""
    tree = tmp_path / "pyhash"
    tree.mkdir()
    (tree / "requirements.txt").write_text(
        "# generated\n"
        "--require-hashes\n"  # the hardened global option must NOT false-reject the file
        "requests[security]==2.31.0 \\\n"
        "    --hash=sha256:aaaa \\\n"
        "    --hash=sha256:bbbb\n"
        'jinja2==3.1.2 ; python_version >= "3.7"\n'
    )
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements.txt]" in out, out


def test_dep_audit_python_audits_requirements_dir_layout(tmp_path: Path):
    """COVERAGE (agent-tools#131 review P3): the `requirements/<env>.txt` layout is audited too,
    so a Python repo using `requirements/base.txt` isn't silently skipped."""
    tree = tmp_path / "pydir"
    (tree / "requirements").mkdir(parents=True)
    (tree / "requirements" / "base.txt").write_text("flask==2.0.0\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements/base.txt]" in out, out


def test_dep_audit_empty_requirements_does_not_mask_pyproject(tmp_path: Path):
    """SECURITY (agent-tools#131 review): a PR could drop an empty/comment-only `requirements.txt`
    next to real deps in `pyproject.toml`; if the empty file counted as 'python audited', the
    pyproject fail-closed would be suppressed and the gate would pass having checked NOTHING. The
    empty file must not mask the un-auditable pyproject -> still fail closed, pip-audit not run."""
    tree = tmp_path / "pymask"
    tree.mkdir()
    (tree / "requirements.txt").write_text("# only a comment, no pinned spec\n\n")
    (tree / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0.1.0'\ndependencies=['flask']\n"
    )
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, out
    assert "no pinned requirements" in out
    assert "STUB_PIP_AUDIT_ARGV" not in out, "pip-audit must not run when only an empty req + pyproject exist"


def test_dep_audit_empty_req_plus_uv_lock_covers(tmp_path: Path):
    """Empty/stub requirements.txt must not mask pyproject, but a uv.lock with registry pins
    is the data source — the lockfile covers, so the gate must NOT fail closed."""
    tree = tmp_path / "pyemptyuv"
    tree.mkdir()
    (tree / "requirements.txt").write_text("# only a comment, no pinned spec\n\n")
    (tree / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0.1.0'\ndependencies=['colorama']\n"
    )
    (tree / "uv.lock").write_text(_UV_LOCK_REGISTRY)
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "no pinned requirements" not in out
    _assert_lockfile_data_audit_argv(out)


def test_dep_audit_empty_requirements_alone_passes(tmp_path: Path):
    """An empty/comment-only `requirements.txt` with NO pyproject/poetry declares no real Python
    deps anywhere -> nothing to audit -> clean pass (it must not false-fail)."""
    tree = tmp_path / "pyemptyonly"
    tree.mkdir()
    (tree / "requirements.txt").write_text("# nothing pinned here\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV" not in out


def test_dep_audit_pinned_requirements_with_pyproject_audits_requirements(tmp_path: Path):
    """Happy-path coexistence: a pinned `requirements.txt` alongside `pyproject.toml` is the data
    source (audited via `--no-deps -r`); the pyproject must NOT additionally fail closed."""
    tree = tmp_path / "pyboth"
    tree.mkdir()
    (tree / "requirements.txt").write_text("flask==2.0.0\n")
    (tree / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0.1.0'\ndependencies=['flask']\n"
    )
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements.txt]" in out, out
    assert "no pinned requirements" not in out, "pyproject must not fail closed when a pinned req covers it"


def test_dep_audit_python_inline_comment_after_pin_stays_pinned(tmp_path: Path):
    """A pinned spec with a trailing inline comment (`flask==2.0.0  # note`) must still classify
    as pinned and be audited — the comment-strip must not turn a valid spec unsafe."""
    tree = tmp_path / "pycomment"
    tree.mkdir()
    (tree / "requirements.txt").write_text("flask==2.0.0  # pin for CVE-x\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 0, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements.txt]" in out, out


def test_dep_audit_python_pip_audit_not_installed_fails_closed(tmp_path: Path):
    """A detected python manifest with NO pip-audit on PATH fails CLOSED (the new glob+pyproject
    detection must still reach the 'pip-audit not installed' miss). DEP_AUDIT_ALLOW_MISSING=1
    relaxes it."""
    tree = tmp_path / "pynoaudit"
    tree.mkdir()
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": "/usr/bin:/bin"})
    assert rc == 1, out
    assert "pip-audit not installed" in out
    rc_ok, _ = _run(
        DEP_AUDIT, tmp_path, str(tree),
        env_extra={"PATH": "/usr/bin:/bin", "DEP_AUDIT_ALLOW_MISSING": "1"},
    )
    assert rc_ok == 0, "DEP_AUDIT_ALLOW_MISSING=1 must skip a missing pip-audit"


def test_dep_audit_python_mixed_pinned_and_unsafe_fails(tmp_path: Path):
    """Mixed outcome: a pinned `requirements.txt` is audited while a sibling `requirements-dev.txt`
    is refused (unsafe). The gate fails (rc 1), the pinned file is still audited, and a sibling
    pyproject must NOT additionally fail-closed (the refusal already gated it)."""
    tree = tmp_path / "pymixed"
    tree.mkdir()
    (tree / "requirements.txt").write_text("flask==2.0.0\n")
    (tree / "requirements-dev.txt").write_text("pytest==7.0.0\n-e .\n")  # the -e makes it unsafe
    (tree / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
    bin_dir = tmp_path / "bin"
    _stub_pip_audit(bin_dir, exit_code=0)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    rc, out = _run(DEP_AUDIT, tmp_path, str(tree), env_extra={"PATH": path})
    assert rc == 1, out
    assert "STUB_PIP_AUDIT_ARGV:[--no-deps -r requirements.txt]" in out, out  # pinned one still audited
    assert "requirements-dev.txt has a non-pinned or direct-reference line" in out
    assert "no pinned requirements" not in out, "the refusal already gated; pyproject must not double-fail"


# ---------------------------------------------------------------------------
# 2. leftover-grep — fail closed on a missing/unresolvable base; no swallowed diff error.
# ---------------------------------------------------------------------------

def _leftover_repo(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("a = 1\n")
    _commit(repo, "base")
    _git(repo, "branch", "-M", "main")
    return repo


def test_leftover_fails_closed_on_unresolvable_explicit_base(tmp_path: Path):
    """An explicitly-requested base that doesn't resolve must FAIL the gate, not silently
    fall back to a full-tree scan (flood) or a no-op."""
    repo = _leftover_repo(tmp_path)
    rc, out = _run(LEFTOVER, repo, env_extra={
        "LEFTOVER_BASE": "origin/does-not-exist", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "does not resolve" in out


def _orphan_repo(tmp_path: Path, head_file_body: str) -> Path:
    """A repo whose HEAD shares NO merge-base with `main` (orphan branch = unrelated history),
    modelling a shallow `--depth=1` CI checkout where the merge base is unreachable. `head_file_body`
    is committed as `b.py` on the orphan branch (plant or omit a leftover via its contents)."""
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("a = 1\n")
    _commit(repo, "a")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    _git(repo, "rm", "-q", "-rf", ".")
    (repo / "b.py").write_text(head_file_body)
    _commit(repo, "orphan")
    return repo


def test_leftover_no_merge_base_falls_back_to_two_dot_and_still_gates(tmp_path: Path):
    """agent-tools#130: under a shallow checkout the three-dot `base...HEAD` diff has no
    reachable merge base. The gate must NOT fatal-and-block every such PR — it falls back to the
    two-dot `base..HEAD` diff (no merge base needed) and KEEPS SCANNING. A planted leftover on the
    orphan head must still be caught via that two-dot fallback (a real catch, not a silent pass)."""
    repo = _orphan_repo(tmp_path, "b = 2  # TODO no ticket\n")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "two-dot diff" in out, "must announce the two-dot fallback on an unreachable merge base"
    assert "untracked-todo" in out, "the planted leftover must still be caught via two-dot"


def test_leftover_no_merge_base_clean_diff_passes_via_two_dot(tmp_path: Path):
    """agent-tools#130 (don't over-fail-closed): a CLEAN orphan head with no merge base must
    PASS via the two-dot fallback — a legitimate shallow PR with no leftover must not be blocked
    by a fatal three-dot diff. Proves the fallback keeps the gate USABLE, not just safe."""
    repo = _orphan_repo(tmp_path, "b = 2\n")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out
    assert "two-dot diff" in out
    assert "PASS" in out


def test_leftover_fails_closed_on_uncomputable_head(tmp_path: Path):
    """The #129 core bug, post two-dot fallback: a head SHA whose object is missing makes BOTH
    the three-dot and the two-dot `git diff` error. That error must FAIL the gate (it used to be
    swallowed by `done < <(emit_lines)`, so the gate printed PASS having scanned nothing), and the
    fail-closed message must point at the shallow-checkout remedy (fetch-depth: 0)."""
    repo = _leftover_repo(tmp_path)
    bogus = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"  # well-formed SHA, no such object
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": bogus})
    assert rc == 1, out
    assert "could not compute the lines to scan" in out
    assert "fetch-depth: 0" in out


def test_leftover_script_has_two_dot_fallback(tmp_path: Path):
    """Guard the fix against regression: the script must check for a reachable merge base and,
    when there is none, switch to the two-dot range — so the diff range can't silently revert to
    an unconditional three-dot that fatals every shallow PR."""
    code = "\n".join(
        ln for ln in LEFTOVER.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "git merge-base" in code, "must probe for a reachable merge base"
    assert '"$base..$LEFTOVER_HEAD"' in code, "must fall back to the two-dot range"


def test_leftover_workflow_pr_head_fetch_is_not_shallow():
    """agent-tools#130: the pull_request_target workflow must fetch the PR head with enough
    history for merge-base, not `--depth=1`, so the real three-dot added-lines diff can run."""
    code = _executable_lines(LEFTOVER_WF.read_text())
    head_fetch_lines = [ln.strip() for ln in code.splitlines() if "git fetch" in ln and '"$HEAD_SHA"' in ln]
    assert head_fetch_lines, "workflow must fetch the PR head object before scanning"
    assert all("--depth=1" not in ln for ln in head_fetch_lines), head_fetch_lines


def test_leftover_docs_match_full_fetch_and_script_fallback_contract():
    """The shipped workflow should promise full-fetch three-dot semantics, while the script
    documents two-dot as a fallback only for direct/local or stale copied callers."""
    readme = LEFTOVER_WF.with_name("README.md").read_text()
    script = LEFTOVER.read_text()
    workflow = LEFTOVER_WF.read_text()
    readme_flat = " ".join(readme.split())
    assert "official workflow fetches" in readme
    assert "fallback for direct callers or older copied workflows" in readme
    assert "do not depend on an unreachable merge-base producing a no-op scan" in readme_flat
    assert "official workflow fetches" in script
    assert "older copied workflows" in script
    assert "two-dot fallback is for direct/local callers or older copied workflows" in workflow
    assert "PR head is fetched with --depth=1 under pull_request_target" not in script


def test_leftover_two_dot_fallback_catches_leftover_in_real_shallow_checkout(tmp_path: Path):
    """The faithful CI scenario (agent-tools#130), not just an orphan stand-in: an origin with a
    SHARED ancestor `base0`, `main` advanced past it, and a `pr` branch off `base0` carrying a
    leftover. A `--depth=1` clone of main + a `--depth=1` fetch of the PR head leaves the shared
    ancestor unfetched, so the merge base is genuinely unreachable (exactly pull_request_target's
    shallow checkout) while the histories still OVERLAP. The gate must take the two-dot fallback
    and still catch the planted leftover — proving the fix works on representative shallow content,
    where the unconditional three-dot diff would have fataled and (pre-fix) silently passed."""
    (tmp_path / "origin").mkdir()  # _make_repo appends /repo and mkdirs non-recursively
    up = _make_repo(tmp_path / "origin")
    _git(up, "checkout", "-q", "-B", "main")
    (up / "a.py").write_text("a = 1\n")
    _commit(up, "base0")  # the shared ancestor / true branch point
    _git(up, "checkout", "-q", "-b", "pr")
    (up / "b.py").write_text("c = 3  # TODO no ticket\n")  # the planted leftover on the PR head
    _commit(up, "pr-with-leftover")
    pr_sha = subprocess.run(
        ["git", "rev-parse", "pr"], cwd=up, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(up, "checkout", "-q", "main")
    (up / "a.py").write_text("a = 1\nd = 4\n")  # main advances past base0
    _commit(up, "base1")

    work = tmp_path / "work"
    url = f"file://{up}"  # file:// forces a real shallow clone (no full-history hardlink)
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", url, str(work)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "fetch", "-q", "--depth=1", "origin", pr_sha],
        cwd=work, check=True, capture_output=True, text=True,
    )
    # Sanity: the merge base really is unreachable in the shallow clone.
    mb = subprocess.run(
        ["git", "merge-base", "origin/main", pr_sha], cwd=work, capture_output=True, text=True
    )
    assert mb.returncode != 0, f"fixture broken: merge base unexpectedly reachable\n{mb.stdout}"

    rc, out = _run(LEFTOVER, work, env_extra={"LEFTOVER_BASE": "origin/main", "LEFTOVER_HEAD": pr_sha})
    assert rc == 1, out
    assert "two-dot diff" in out, "an unreachable merge base must trigger the two-dot fallback"
    assert "untracked-todo" in out, "the PR's leftover must be caught via the two-dot fallback"


def test_leftover_empty_diff_passes(tmp_path: Path):
    """No added lines at all (head == base) -> emit_lines yields nothing and returns 0. The
    new `if ! emit_lines >file` invariant must PASS cleanly, not false-fail with 'could not
    compute the lines to scan' on the legitimate empty-diff path (review finding #2)."""
    repo = _leftover_repo(tmp_path)
    # A branch identical to main: a real, resolvable base + head, but an empty added-lines diff.
    _git(repo, "checkout", "-q", "-b", "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out
    assert "PASS" in out


def test_leftover_untracked_todo_blocks(tmp_path: Path):
    """Sanity: a real leftover (untracked TODO on an added line) still blocks."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("a = 1\nb = 2  # TODO no ticket\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "untracked-todo" in out


def test_leftover_tracked_todo_passes(tmp_path: Path):
    """A TODO WITH a tracker ref passes."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("a = 1\nb = 2  # TODO(#42) tracked\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out


@pytest.mark.parametrize("rel_path", ["ci/ship/ship.sh", "tests/test_ship.py"])
def test_leftover_scans_ship_sh_and_test_ship_py_with_no_exemption(tmp_path: Path, rel_path: str):
    """agent-tools#317/#318 review follow-up: the CI-up leftover-grep gate must scan
    ci/ship/ship.sh and tests/test_ship.py themselves exactly like any other file — it
    has no path-based exemption for the local ship.sh CI-outage fallback's own
    implementation/test files (that fallback's design is now: never spell a marker as
    one contiguous token in its own source, not a path carve-out — see ship.sh's
    _local_leftover_check). Parametrized so EACH path is planted and asserted on its
    OWN run — a single combined-commit version (planting both, asserting one pass/fail)
    could not tell "neither path is exempt" from "exactly one of the two is exempt but
    the other still trips the gate", which would silently hide half a regression.

    The marker text below is split ("TO" "DO") rather than written as one literal —
    this file becomes part of THIS SAME repo's own PR diff, so a bare "[T]ODO" here
    would self-trigger ship.sh's CI-outage leftover-marker fallback on this exact
    change, the very self-reference class of bug agent-tools#318 fixed."""
    marker_todo = "TO" "DO"
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"x = 1  # {marker_todo} no ticket\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "untracked-todo" in out
    assert rel_path in out, f"expected {rel_path} named in the gate's own output, got:\n{out}"


def test_leftover_bare_seven_equals_is_not_a_conflict_marker(tmp_path: Path):
    """A source line of exactly 7 `=` is a common decorative separator, not a merge marker —
    it must NOT block (agent-tools#129 false positive)."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "sep.py").write_text("x = 1\n=======\ny = 2\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 0, out
    assert "merge-marker" not in out


def test_leftover_real_conflict_start_marker_blocks(tmp_path: Path):
    """A genuine `<<<<<<<` start marker still blocks — the conflict is caught by its
    unambiguous start/end markers even though the bare `=======` middle is no longer flagged."""
    repo = _leftover_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "c.py").write_text("<<<<<<< HEAD\nx = 1\n")
    _commit(repo, "feat")
    rc, out = _run(LEFTOVER, repo, env_extra={"LEFTOVER_BASE": "main", "LEFTOVER_HEAD": "HEAD"})
    assert rc == 1, out
    assert "merge-marker" in out


def test_leftover_script_does_not_read_emit_lines_via_process_substitution():
    """Guard the fix: the script must NOT pipe emit_lines through `< <(...)` (which swallows
    its failure). It writes to a temp file and gates on the exit status."""
    code = "\n".join(
        ln for ln in LEFTOVER.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "done < <(emit_lines)" not in code
    assert "if ! emit_lines >" in code


# ---------------------------------------------------------------------------
# 3. codeql self-gate — language-detect must not self-disable via SIGPIPE.
# ---------------------------------------------------------------------------

def test_codeql_detect_does_not_use_pipe_grep_q():
    """The buggy `git ls-files | grep -qiE` (dies of SIGPIPE under pipefail -> false negative)
    must be gone; the detect materializes the list and uses a full-consuming count."""
    code = "\n".join(
        ln for ln in CODEQL_WF.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    detect = code.split("- name: Detect source for this language", 1)[1].split("- name: Initialize CodeQL", 1)[0]
    assert "git ls-files | grep -qiE" not in detect
    assert "grep -q" not in detect
    assert 'tracked="$(git ls-files)"' in detect
    assert 'match_count="$(grep -ciE "$pattern" <<<"$tracked")"' in detect


def test_codeql_detect_pattern_detects_present_source_under_pipefail():
    """Reproduce the fix end-to-end: under `set -uo pipefail`, the new detect pattern must
    report DETECTED when many matching files exist (the old pipe pattern reported MISSED
    because git ls-files died of SIGPIPE when grep -q exited early)."""
    script = r'''
    set -uo pipefail
    tracked=$(seq 1 100000 | sed "s/.*/file&.ts/")
    match_count="$(grep -ciE "\.ts$" <<<"$tracked")"
    if [ "$match_count" -gt 0 ]; then echo DETECTED
    else echo MISSED; fi
    '''
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "DETECTED", proc.stdout + proc.stderr


def test_codeql_detect_pattern_reports_absent_source():
    """The full-consuming count must preserve the no-match branch: grep rc 1 with count 0 is a
    clean absence signal, not an error and not a false positive."""
    script = r'''
    set -uo pipefail
    tracked="README.md"
    grep_rc=0
    match_count="$(grep -ciE "\.ts$" <<<"$tracked")" || grep_rc=$?
    if [ "$grep_rc" -gt 1 ]; then echo ERROR
    elif [ "${match_count:-0}" -gt 0 ]; then echo DETECTED
    else echo MISSED; fi
    '''
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "MISSED", proc.stdout + proc.stderr


def test_codeql_detect_pattern_fails_closed_on_grep_error():
    """A grep rc > 1 is a detector error and must fail closed, not look like absent source."""
    script = r'''
    set -uo pipefail
    tracked="file.ts"
    grep_rc=0
    match_count="$(grep -ciE "[" <<<"$tracked" 2>/dev/null)" || grep_rc=$?
    if [ "$grep_rc" -gt 1 ]; then echo ERROR
    elif [ "${match_count:-0}" -gt 0 ]; then echo DETECTED
    else echo MISSED; fi
    '''
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "ERROR", proc.stdout + proc.stderr


def test_codeql_old_pipe_pattern_self_disables_proving_the_bug():
    """Pin the regression: the OLD `printf|grep -q` pipe pattern DOES self-disable (reports
    MISSED) under pipefail — proving the fix above is load-bearing, not cosmetic."""
    script = r'''
    set -uo pipefail
    big=$(seq 1 100000 | sed "s/.*/file&.ts/")
    if printf "%s\n" "$big" | grep -qiE "\.ts$"; then echo DETECTED; else echo MISSED; fi
    '''
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.stdout.strip() == "MISSED", proc.stdout + proc.stderr


def test_codeql_doc_matches_block_walk_impl():
    """Doc/code drift fix: the header must describe the contiguous-comment-block suppression
    walk (what the impl does), not 'the line directly above'."""
    text = CODEQL_WF.read_text()
    # The header now says "ANY line in the contiguous comment block".
    assert "contiguous comment block" in text
    # The impl still walks the block.
    assert "Walk upward through the contiguous comment block" in text


def test_all_touched_workflows_parse():
    """Every workflow YAML touched here must still parse."""
    yaml = pytest.importorskip("yaml")
    for wf in (DEP_WF, LEFTOVER_WF, CODEQL_WF):
        yaml.safe_load(wf.read_text())
