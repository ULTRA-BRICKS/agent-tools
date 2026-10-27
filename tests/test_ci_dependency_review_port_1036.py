"""dep-audit.sh -> dep_audit.py port (agent-tools#1036).

`ci/dependency-review/dep-audit.sh` is a <=40-line POSIX-sh launcher; the logic lives in
`dep_audit.py`. These tests pin the launcher contract, lint cleanliness, and the behavior
the port must keep (verdict lines, exit codes, argv handed to the scanners).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SLOT = REPO_ROOT / "ci" / "dependency-review"
LAUNCHER = SLOT / "dep-audit.sh"
PORT = SLOT / "dep_audit.py"
SYSPATH = "/usr/bin:/bin"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _env(bin_dir: Path | None = None, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("DEP_AUDIT_LEVEL", "DEP_AUDIT_ALLOW_MISSING"):
        env.pop(key, None)
    env["PATH"] = f"{bin_dir}:{SYSPATH}" if bin_dir else SYSPATH
    env.update(extra)
    return env


def _run(shell: str, tree: Path, env: dict[str, str], *args: str, cwd: Path | None = None):
    proc = subprocess.run(
        [shell, str(LAUNCHER), str(tree), *args],
        cwd=cwd or tree.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    for rel, content in files.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tree


# ---------------------------------------------------------------------------
# (a) launcher contract
# ---------------------------------------------------------------------------


def test_launcher_is_tiny_and_executable():
    assert len(LAUNCHER.read_text().splitlines()) <= 40
    assert os.access(LAUNCHER, os.X_OK)
    assert os.access(PORT, os.X_OK)
    assert LAUNCHER.read_text().startswith("#!/bin/sh\n")


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_launcher_runs_under_posix_sh_and_bash(tmp_path: Path, shell: str):
    rc, out = _run(shell, _tree(tmp_path, {}), _env())
    assert rc == 0, out
    assert "nothing to audit" in out


def test_launcher_resolves_sibling_via_relative_path_from_other_cwd(tmp_path: Path):
    tree = _tree(tmp_path, {})
    rel = os.path.relpath(LAUNCHER, tmp_path)
    proc = subprocess.run(
        ["sh", rel, str(tree)], cwd=tmp_path, env=_env(), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "nothing to audit" in proc.stderr


def test_launcher_without_audit_dir_arg_defaults_to_cwd(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "pip-audit", "exit 0\n")
    tree = _tree(tmp_path, {"requirements.txt": "# nothing\n"})
    proc = subprocess.run(
        ["sh", str(LAUNCHER)], cwd=tree, env=_env(bin_dir), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "skipping requirements.txt" in proc.stderr


def test_launcher_fails_clearly_when_python_file_is_missing(tmp_path: Path):
    copy = tmp_path / "dep-audit.sh"
    shutil.copy(LAUNCHER, copy)
    proc = subprocess.run(
        ["sh", str(copy)], cwd=tmp_path, env=_env(), capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert "dep_audit.py missing" in proc.stderr


def test_launcher_fails_clearly_when_python3_is_absent(tmp_path: Path):
    box = tmp_path / "box"
    box.mkdir()
    for tool in ("dirname", "pwd"):
        found = shutil.which(tool)
        if found:
            (box / tool).symlink_to(found)
    shell = shutil.which("sh")
    assert shell
    proc = subprocess.run(
        [shell, str(LAUNCHER)],
        cwd=tmp_path,
        env={"PATH": str(box)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "python3 not found" in proc.stderr


def test_launcher_passes_args_and_env_through(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "bun", 'echo "STUB_BUN:$*" >&2\nexit 0\n')
    tree = _tree(tmp_path, {"bun.lock": ""})
    rc, out = _run("sh", tree, _env(bin_dir, DEP_AUDIT_LEVEL="critical"))
    assert rc == 0, out
    assert "STUB_BUN:audit --audit-level=critical" in out
    assert "PASS \u2014 no advisories at critical+." in out


# ---------------------------------------------------------------------------
# (b) lint cleanliness
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck missing")
def test_launcher_is_shellcheck_clean():
    proc = subprocess.run(["shellcheck", str(LAUNCHER)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


@pytest.mark.skipif(shutil.which("shfmt") is None, reason="shfmt missing")
def test_launcher_is_shfmt_clean():
    proc = subprocess.run(
        ["shfmt", "-i", "2", "-ci", "-bn", "-d", str(LAUNCHER)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff missing")
def test_port_is_ruff_default_clean():
    check = subprocess.run(
        ["ruff", "check", "--isolated", str(PORT)], capture_output=True, text=True
    )
    fmt = subprocess.run(
        ["ruff", "format", "--isolated", "--check", str(PORT)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout
    assert fmt.returncode == 0, fmt.stdout


# ---------------------------------------------------------------------------
# (c) behavior preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bun_exit", "expected_rc", "verdict"),
    [
        (0, 0, "PASS \u2014 no advisories at high+."),
        (1, 1, "FAIL \u2014 advisories at high+ above."),
    ],
)
def test_dep_audit_verdict_and_exit_follow_the_scanner(
    tmp_path: Path, bun_exit: int, expected_rc: int, verdict: str
):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "bun", f'echo "STUB_BUN:$*" >&2\nexit {bun_exit}\n')
    rc, out = _run("bash", _tree(tmp_path, {"bun.lock": ""}), _env(bin_dir))
    assert rc == expected_rc, out
    assert "STUB_BUN:audit --audit-level=high" in out
    assert verdict in out
    other = "FAIL \u2014 advisories" if expected_rc == 0 else "PASS \u2014 no advisories"
    assert other not in out, "exactly one verdict line is printed"


def test_missing_audit_dir_fails_closed_with_message(tmp_path: Path):
    rc, out = _run("sh", tmp_path / "nope", _env())
    assert rc == 1
    assert "audit dir '" in out and "does not exist \u2014 FAILING (cannot audit)." in out


def test_empty_dep_audit_level_and_empty_dir_arg_use_defaults(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "bun", 'echo "STUB_BUN:$*" >&2\n')
    tree = _tree(tmp_path, {"bun.lockb": ""})
    proc = subprocess.run(
        ["sh", str(LAUNCHER), ""],
        cwd=tree,
        env=_env(bin_dir, DEP_AUDIT_LEVEL=""),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "STUB_BUN:audit --audit-level=high" in proc.stderr


@pytest.mark.parametrize(
    ("files", "pm", "expected"),
    [
        ({"package.json": "{}", "pnpm-lock.yaml": ""}, "pnpm", "audit --audit-level high"),
        ({"package.json": "{}", "yarn.lock": ""}, "yarn", "npm audit --severity high"),
        ({"package.json": "{}"}, "npm", "audit --audit-level=high"),
        ({"Cargo.lock": ""}, "cargo-audit", "audit"),
        ({"go.mod": ""}, "govulncheck", "./..."),
    ],
)
def test_ecosystem_dispatch_argv(tmp_path: Path, files: dict[str, str], pm: str, expected: str):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, pm, f'echo "STUB_{pm}:$*" >&2\n')
    if pm == "cargo-audit":
        _stub(bin_dir, "cargo", 'echo "STUB_cargo:$*" >&2\n')
    rc, out = _run("sh", _tree(tmp_path, files), _env(bin_dir))
    assert rc == 0, out
    stub = "cargo" if pm == "cargo-audit" else pm
    assert f"STUB_{stub}:{expected}" in out


def test_cargo_audit_subcommand_probe_without_standalone_binary(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "cargo", 'echo "STUB_cargo:$*" >&2\nexit 0\n')
    rc, out = _run("sh", _tree(tmp_path, {"Cargo.lock": ""}), _env(bin_dir))
    assert rc == 0, out
    assert "STUB_cargo:audit\n" in out


@pytest.mark.parametrize(
    ("files", "needle"),
    [
        ({"package.json": "{}"}, "package.json present but no usable node package manager"),
        ({"Cargo.lock": ""}, "Cargo.lock present but cargo-audit not installed"),
        ({"go.mod": ""}, "go.mod present but govulncheck not installed"),
    ],
)
def test_missing_scanner_fails_closed_unless_allowed(tmp_path: Path, files: dict[str, str], needle: str):
    box = tmp_path / "box"
    box.mkdir()
    env = _env()
    # Hide any host node/cargo/go tooling that lives in /usr/bin.
    env["PATH"] = str(box)
    py = shutil.which("python3")
    assert py
    runner = [py, "-B", "-I", str(PORT)]
    tree = _tree(tmp_path, files)
    proc = subprocess.run(runner + [str(tree)], env=env, capture_output=True, text=True)
    out = proc.stderr
    assert proc.returncode == 1, out
    assert needle in out
    assert "FAILING (no audit performed; set DEP_AUDIT_ALLOW_MISSING=1 to allow)." in out
    assert "FAIL \u2014 1 detected ecosystem(s) had no usable scanner (fail-closed)" in out
    env["DEP_AUDIT_ALLOW_MISSING"] = "1"
    proc = subprocess.run(runner + [str(tree)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "skipping (DEP_AUDIT_ALLOW_MISSING=1)." in proc.stderr
    assert "nothing to audit" in proc.stderr  # allow-missing: no scanner ran


def test_pip_audit_gets_data_safe_argv_only(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "pip-audit", 'echo "STUB_PIP:[$*]" >&2\nexit 0\n')
    tree = _tree(
        tmp_path,
        {
            "requirements.txt": "flask==2.0.0 ; python_version>'3' --hash=sha256:ab\n",
            "requirements/dev.txt": "# nothing\n",
        },
    )
    rc, out = _run("sh", tree, _env(bin_dir))
    assert rc == 0, out
    assert "STUB_PIP:[--no-deps -r requirements.txt]" in out
    assert "skipping requirements/dev.txt" in out
    assert out.count("STUB_PIP:") == 1


def test_lockfile_pins_go_to_temp_outside_tree_and_failures_propagate(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "pip-audit", 'echo "STUB_PIP:[$*]" >&2\ncat "$3" >&2\nexit 1\n')
    uv = (
        '[[package]]\nname = "left-pad"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "me"\nversion = "0.1"\nsource = { editable = "." }\n'
    )
    tree = _tree(tmp_path, {"uv.lock": uv})
    rc, out = _run("sh", tree, _env(bin_dir))
    assert rc == 1, out
    assert "STUB_PIP:[--no-deps -r /tmp/dep-audit-py." in out
    assert "left-pad==1.0" in out
    assert "me==0.1" not in out
    assert "FAIL \u2014 advisories at high+ above." in out


def test_poetry_lock_unparsed_record_refused_and_other_lock_still_considered(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "pip-audit", 'echo "STUB_PIP:[$*]" >&2\n')
    poetry = '[[package]]\nname = "ok"\nversion = "1.0"\n[[package]]\nname = "bad"\n'
    tree = _tree(tmp_path, {"poetry.lock": poetry})
    rc, out = _run("sh", tree, _env(bin_dir))
    assert rc == 1, out
    assert "extracted pins from poetry.lock were not data-safe" in out
    assert "STUB_PIP" not in out


def test_empty_lock_is_skipped_then_pyproject_fails_closed(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "pip-audit", 'echo "STUB_PIP:[$*]" >&2\n')
    tree = _tree(tmp_path, {"uv.lock": "version = 1\n", "pyproject.toml": ""})
    rc, out = _run("sh", tree, _env(bin_dir))
    assert rc == 1, out
    assert "skipping uv.lock \u2014 no registry/PyPI pins to audit as data" in out
    assert "python project present but no pinned requirements*.txt" in out
    assert "STUB_PIP" not in out


def test_python_manifest_without_pip_audit_fails_closed(tmp_path: Path):
    rc, out = _run("sh", _tree(tmp_path, {"requirements.txt": "a==1\n"}), _env())
    assert rc == 1, out
    assert "python manifest present but pip-audit not installed (pipx install pip-audit)" in out
