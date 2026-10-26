"""Regression tests for repo-local hatch-lib loading in agent-hook entrypoints."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_HATCH_PACKAGE = ROOT / "lib" / "agenttools_hatch_escalation"
REAL_HATCH_INIT = REAL_HATCH_PACKAGE / "__init__.py"

HATCH_HOOKS = (
    "agent-hooks/background-subagent-gate/background_subagent_gate.py",
    "agent-hooks/block-devserver-primary/block_devserver_primary.py",
    "agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py",
    "agent-hooks/block-raw-ticket/block_raw_ticket.py",
    "agent-hooks/block-raw-worktree-add/block_raw_worktree_add.py",
    "agent-hooks/block-reset-hard/block_reset_hard.py",
    "agent-hooks/decision-request-format/decision_request_format.py",
    "agent-hooks/local-path-in-gh/local_path_in_gh.py",
    "agent-hooks/no-long-inline-process/no_long_inline_process.py",
    "agent-hooks/no-shell-file-edit/no_shell_file_edit.py",
    "agent-hooks/orchestrator-stays-thin/orchestrator_stays_thin.py",
    "agent-hooks/pin-primary-worktree/pin_primary_worktree.py",
    "agent-hooks/pkill-guard/pkill_guard.py",
    "agent-hooks/require-review-before-commit/require_review.py",
    "agent-hooks/require-ticket-before-commit/require_ticket_before_commit.py",
    "agent-hooks/runpod-offload-gate/runpod_offload_gate.py",
    "agent-hooks/skills-read-gate/skills_read_gate.py",
    "agent-hooks/subagent-no-bg-longproc/subagent_no_bg_longproc.py",
    "agent-hooks/subagent-no-monitor/subagent_no_monitor.py",
    "agent-hooks/visual-proof-gate/visual_proof_gate.py",
    "agent-hooks/worktree-only-writes/worktree_only_writes.py",
)


@pytest.mark.parametrize("hook_rel", HATCH_HOOKS)
def test_hatch_hook_loads_repo_local_lib_under_hostile_sys_path(tmp_path: Path, hook_rel: str):
    """A user/site package must not shadow the catalog's own hatch-lib.

    The old hook pattern inserted ``repo/lib`` only when it was absent from ``sys.path``. If an
    isolated harness already carried that path later in ``sys.path``, a hostile earlier package
    named ``agenttools_hatch_escalation`` could win the import. Hooks should load hatch-lib by
    explicit file path instead. This subprocess deliberately does not add ``ROOT/lib`` to
    ``sys.path``; if the hatch helper depended on absolute sibling ``lib/`` imports, this import
    would fail.
    """

    hostile_pkg = tmp_path / "hostile" / "agenttools_hatch_escalation"
    hostile_pkg.mkdir(parents=True)
    (hostile_pkg / "__init__.py").write_text(
        "MALICIOUS = True\n"
        "def request_hatch_approval(*_args, **_kwargs):\n"
        "    raise AssertionError('hostile hatch package was imported')\n",
        encoding="utf-8",
    )

    code = """
import importlib.util
import json
import sys
from pathlib import Path

hook = Path({hook!r})
sys.path[:0] = [{hostile!r}]
import agenttools_hatch_escalation as preloaded_hatch
assert preloaded_hatch.MALICIOUS is True
preloaded_hatch.__file__ = {real_hatch!r}
spec = importlib.util.spec_from_file_location("hook_under_test", hook)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
hatch = mod.hatch_escalation
print(json.dumps({{
    "file": getattr(hatch, "__file__", None),
    "malicious": bool(getattr(hatch, "MALICIOUS", False)),
    "has_api": callable(getattr(hatch, "request_hatch_approval", None)),
}}))
""".format(
        hook=str(ROOT / hook_rel),
        hostile=str(hostile_pkg.parent),
        real_hatch=str(REAL_HATCH_INIT),
    )

    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout)
    assert loaded["malicious"] is False
    assert loaded["has_api"] is True
    assert Path(loaded["file"]).resolve() == REAL_HATCH_INIT


def test_inline_hatch_loader_blocks_stay_in_sync():
    """The duplicated bootstrap is intentional; this pins it against drift."""

    blocks = {_loader_block(ROOT / hook_rel) for hook_rel in HATCH_HOOKS}
    assert len(blocks) == 1


def test_hatch_hooks_keep_loader_import_dependency():
    """The sync guard includes behavior; this pins the import the bootstrap needs."""

    for hook_rel in HATCH_HOOKS:
        assert "importlib" in _absolute_import_roots(ROOT / hook_rel)


def test_hatch_helper_has_no_absolute_sibling_lib_imports():
    """Path-loading hatch-lib is valid only while it avoids top-level sibling lib imports."""

    sibling_libs = {
        path.name
        for path in (ROOT / "lib").iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    } - {"agenttools_hatch_escalation"}
    imported_roots: set[str] = set()
    for hatch_py in REAL_HATCH_PACKAGE.rglob("*.py"):
        imported_roots.update(_absolute_import_roots(hatch_py))

    assert sorted(imported_roots & sibling_libs) == []


def test_hatch_helper_imports_only_stdlib_modules():
    """Hook entrypoints run under isolated interpreters, so hatch-lib must import stdlib only."""

    imported_roots: set[str] = set()
    for hatch_py in REAL_HATCH_PACKAGE.rglob("*.py"):
        imported_roots.update(_absolute_import_roots(hatch_py))

    allowed = _stdlib_module_names() | {"__future__"}
    assert sorted(imported_roots - allowed) == []


def test_freshly_loaded_hook_helper_uses_hermetic_test_home(monkeypatch: pytest.MonkeyPatch):
    """The autouse fixture must cover hook-local helper modules loaded by file path."""

    monkeypatch.setitem(
        sys.modules,
        "agenttools_hatch_escalation",
        sys.modules["agenttools_hatch_escalation"],
    )
    hook_path = ROOT / "agent-hooks" / "block-raw-pr-merge" / "block_raw_pr_merge.py"
    spec = importlib.util.spec_from_file_location("hook_with_hermetic_hatch_home", hook_path)
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    assert hook.hatch_escalation.resolve_home() == os.environ["AGENTTOOLS_TEST_HERMETIC_HOME"]


@pytest.mark.parametrize("hook_rel", HATCH_HOOKS)
def test_hook_replaces_preloaded_module_that_spoofs_repo_file(
    hook_rel: str, monkeypatch: pytest.MonkeyPatch
):
    """A preloaded module cannot earn trust by mutating ``__file__`` to the repo path."""

    fake = types.ModuleType("agenttools_hatch_escalation")
    fake.__file__ = str(REAL_HATCH_INIT)
    fake.MALICIOUS = True
    fake_submodule = types.ModuleType("agenttools_hatch_escalation.leaked")
    fake_submodule.MALICIOUS = True
    monkeypatch.setitem(sys.modules, "agenttools_hatch_escalation", fake)
    monkeypatch.setitem(sys.modules, "agenttools_hatch_escalation.leaked", fake_submodule)
    hook_path = ROOT / hook_rel
    spec = importlib.util.spec_from_file_location("hook_with_hardened_hatch_import", hook_path)
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    assert hook.hatch_escalation is not fake
    assert sys.modules["agenttools_hatch_escalation"] is hook.hatch_escalation
    assert sys.modules.get("agenttools_hatch_escalation.leaked") is not fake_submodule
    assert not getattr(hook.hatch_escalation, "MALICIOUS", False)
    assert callable(hook.hatch_escalation.request_hatch_approval)
    assert Path(hook.hatch_escalation.__file__).resolve() == REAL_HATCH_INIT


@pytest.mark.parametrize("hook_rel", HATCH_HOOKS)
def test_missing_hatch_lib_loads_stub_instead_of_crashing(tmp_path: Path, hook_rel: str):
    """#547: a missing repo-local hatch lib must not crash the hook at import.

    A crash is what on_error:open turns into an ALLOW. The hook must load a
    stub whose request_hatch_approval never approves, so main() can still
    classify the event.
    """

    source = ROOT / hook_rel
    copied = tmp_path / "repo" / hook_rel
    copied.parent.mkdir(parents=True)
    copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    code = """
import importlib.util
import json
from pathlib import Path

hook = Path({hook!r})
spec = importlib.util.spec_from_file_location("copied_hook", hook)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
hatch = mod.hatch_escalation
result = hatch.request_hatch_approval("probe", {{}}, cwd=".")
print(json.dumps({{
    "has_api": callable(getattr(hatch, "request_hatch_approval", None)),
    "approved": bool(result.approved),
    "should_stop": bool(result.should_stop),
}}))
""".format(hook=str(copied))

    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout)
    assert loaded["has_api"] is True
    assert loaded["approved"] is False
    assert loaded["should_stop"] is False


@pytest.mark.parametrize("failure", ["raise RuntimeError('broken hatch import')", "raise SystemExit(7)"])
def test_failed_hatch_import_cleans_partial_submodules(tmp_path: Path, failure: str):
    """A broken hatch package should not leave partial package submodules in sys.modules."""

    hook_rel = "agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py"
    source = ROOT / hook_rel
    copied = tmp_path / "repo" / hook_rel
    copied.parent.mkdir(parents=True)
    copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    fake_hatch = tmp_path / "repo" / "lib" / "agenttools_hatch_escalation"
    fake_hatch.mkdir(parents=True)
    (fake_hatch / "__init__.py").write_text(
        "import agenttools_hatch_escalation.leaked\n"
        f"{failure}\n",
        encoding="utf-8",
    )
    (fake_hatch / "leaked.py").write_text("VALUE = 1\n", encoding="utf-8")

    code = """
import importlib.util
import json
import sys
from pathlib import Path

hook = Path({hook!r})
spec = importlib.util.spec_from_file_location("copied_hook", hook)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
result = mod.hatch_escalation.request_hatch_approval("probe", {{}}, cwd=".")
print(json.dumps({{
    "approved": bool(result.approved),
    "remaining": sorted(
        name for name in sys.modules
        if name == "agenttools_hatch_escalation"
        or name.startswith("agenttools_hatch_escalation.")
    ),
}}))
""".format(hook=str(copied))

    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["approved"] is False
    assert result["remaining"] == []



def test_keyboard_interrupt_during_hatch_import_restores_previous_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Even non-Exception import exits must not leave partial hatch modules behind."""

    hook_rel = "agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py"
    source = ROOT / hook_rel
    copied = tmp_path / "repo" / hook_rel
    copied.parent.mkdir(parents=True)
    copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    fake_hatch = tmp_path / "repo" / "lib" / "agenttools_hatch_escalation"
    fake_hatch.mkdir(parents=True)
    (fake_hatch / "__init__.py").write_text(
        "import agenttools_hatch_escalation.leaked\n"
        "raise KeyboardInterrupt()\n",
        encoding="utf-8",
    )
    (fake_hatch / "leaked.py").write_text("VALUE = 1\n", encoding="utf-8")

    previous = types.ModuleType("agenttools_hatch_escalation")
    previous_submodule = types.ModuleType("agenttools_hatch_escalation.leaked")
    monkeypatch.setitem(sys.modules, "agenttools_hatch_escalation", previous)
    monkeypatch.setitem(sys.modules, "agenttools_hatch_escalation.leaked", previous_submodule)

    spec = importlib.util.spec_from_file_location("copied_hook_keyboard_interrupt", copied)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(KeyboardInterrupt):
        spec.loader.exec_module(mod)

    assert sys.modules["agenttools_hatch_escalation"] is previous
    assert sys.modules["agenttools_hatch_escalation.leaked"] is previous_submodule


@pytest.mark.parametrize(
    ("hook_rel", "event", "expected_message"),
    [
        (
            "agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py",
            {
                "hook_api": "agents-hooks/v1",
                "point": "pre-bash",
                "args": {"command": "gh pr merge 123"},
                "cwd": "",
            },
            "Use `gh ship <PR>`",
        ),
        (
            "agent-hooks/no-long-inline-process/no_long_inline_process.py",
            {
                "hook_api": "agents-hooks/v1",
                "point": "pre-bash",
                "args": {"command": "pytest tests/"},
                "cwd": "",
            },
            "Run this in a BACKGROUND subagent",
        ),
    ],
)
def test_copied_catalog_layout_hatch_hook_still_enforces_gate(
    tmp_path: Path,
    hook_rel: str,
    event: dict[str, object],
    expected_message: str,
):
    """A copied install must preserve catalog lib/ beside hooks for hatch-using hooks."""

    source = ROOT / hook_rel
    copied = tmp_path / "catalog" / hook_rel
    copied.parent.mkdir(parents=True)
    copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    shutil.copytree(
        ROOT / "lib" / "agenttools_hatch_escalation",
        tmp_path / "catalog" / "lib" / "agenttools_hatch_escalation",
    )

    event["cwd"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-I", str(copied)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 10, proc.stderr
    assert expected_message in proc.stdout


@pytest.mark.parametrize(
    ("hook_rel", "gated_event"),
    [
        (
            "agent-hooks/no-long-inline-process/no_long_inline_process.py",
            {
                "hook_api": "agents-hooks/v1",
                "point": "pre-bash",
                "args": {"command": "pytest tests/"},
            },
        ),
        (
            "agent-hooks/block-raw-pr-merge/block_raw_pr_merge.py",
            {
                "hook_api": "agents-hooks/v1",
                "point": "pre-bash",
                "args": {"command": "gh pr merge 123"},
            },
        ),
        (
            "agent-hooks/block-reset-hard/block_reset_hard.py",
            {
                "hook_api": "agents-hooks/v1",
                "point": "pre-bash",
                "args": {"command": "git reset --hard"},
            },
        ),
    ],
)
def test_missing_hatch_helper_denial_class_hook_still_blocks(
    tmp_path: Path, hook_rel: str, gated_event: dict[str, object]
):
    """#547: a missing hatch helper must not fail-open a denial-class hook.

    The hook process has to classify the event even when
    lib/agenttools_hatch_escalation/__init__.py is absent. A crash (nonzero
    non-10) is what on_error:open turns into an ALLOW — the gated command
    must instead BLOCK (exit 10).
    """
    source = ROOT / hook_rel
    copied = tmp_path / "repo" / hook_rel
    copied.parent.mkdir(parents=True)
    copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    # Deliberately do NOT copy lib/agenttools_hatch_escalation.

    gated_event = dict(gated_event)
    gated_event["cwd"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-I", str(copied)],
        input=json.dumps(gated_event),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")},
    )
    assert proc.returncode == 10, (
        f"missing helper must block, not crash-open (exit {proc.returncode}): "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "block"


def test_missing_hatch_helper_still_allows_ungated_command(tmp_path: Path):
    """Classification must still run: an ungated command is allowed even when
    the hatch helper is missing (the helper is only the exception path)."""
    hook_rel = "agent-hooks/no-long-inline-process/no_long_inline_process.py"
    source = ROOT / hook_rel
    copied = tmp_path / "repo" / hook_rel
    copied.parent.mkdir(parents=True)
    copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-I", str(copied)],
        input=json.dumps(
            {
                "hook_api": "agents-hooks/v1",
                "point": "pre-bash",
                "args": {"command": "echo hello"},
                "cwd": str(tmp_path),
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("RIG_HATCH_REQUEST_")},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "allow"


def _loader_block(hook_path: Path) -> str:
    text = hook_path.read_text(encoding="utf-8")
    start = text.index("# SYNC: duplicated in every hatch-using hook")
    end_marker = "hatch_escalation = _load_hatch_escalation()"
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _stdlib_module_names() -> set[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return set(names)

    stdlib = Path(sysconfig.get_paths()["stdlib"])
    roots = set(sys.builtin_module_names)
    for path in stdlib.iterdir():
        if path.name.startswith("_") and path.name != "__future__.py":
            continue
        if path.is_dir() and (path / "__init__.py").exists():
            roots.add(path.name)
        elif path.suffix == ".py":
            roots.add(path.stem)
    dynload = sysconfig.get_config_var("DESTSHARED")
    if dynload:
        for path in Path(dynload).glob("*"):
            roots.add(path.name.split(".", 1)[0])
    return roots


def _absolute_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots.add(node.module.partition(".")[0])
    return imported_roots
