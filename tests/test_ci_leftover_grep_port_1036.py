"""leftover-grep.sh is a POSIX-sh launcher around leftover_grep.py — agent-tools#1036.

The managed copies consumers receive must be clean under shellcheck/shfmt (launcher) and
default ruff (Python), while keeping the exact behavior of the former bash gate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SLOT = REPO_ROOT / "ci" / "leftover-grep"
LAUNCHER = SLOT / "leftover-grep.sh"
PY = SLOT / "leftover_grep.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}
KNOBS = ("LEFTOVER_BASE", "LEFTOVER_HEAD", "LEFTOVER_FULLTREE", "ALLOW_CONSOLE")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=dict(os.environ, **GIT_ENV),
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path, base: dict[str, str], feat: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name, text in {"seed.txt": "s\n", **base}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    for name, text in feat.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat")
    return repo


def _run(
    cwd: Path, env_extra: dict[str, str] | None = None, shell: str = "bash"
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in KNOBS}
    env.update(env_extra or {})
    return subprocess.run(
        [shell, str(LAUNCHER)], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


# --- (a) launcher ----------------------------------------------------------------


def test_launcher_is_tiny_posix_sh_and_executable():
    text = LAUNCHER.read_text()
    assert len(text.splitlines()) <= 40
    assert text.startswith("#!/bin/sh\n")
    assert "BASH_SOURCE" not in text
    assert os.access(LAUNCHER, os.X_OK) and os.access(PY, os.X_OK)


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_launcher_resolves_sibling_from_other_cwd_and_relative_path(tmp_path: Path, shell: str):
    repo = _repo(tmp_path, {"a.js": "a\n"}, {"a.js": "a\nconsole.log(1);\n"})
    rel = os.path.relpath(LAUNCHER, repo)
    env = dict(os.environ, LEFTOVER_BASE="main")
    proc = subprocess.run(
        [shell, rel], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 1, proc.stderr
    assert "::error file=a.js,line=2::[console]" in proc.stdout


def test_launcher_fails_clearly_when_py_missing(tmp_path: Path):
    copy = tmp_path / "leftover-grep.sh"
    shutil.copy(LAUNCHER, copy)
    proc = subprocess.run(["sh", str(copy)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "leftover_grep.py is missing" in proc.stderr


def test_launcher_fails_clearly_when_python3_missing(tmp_path: Path):
    copy = tmp_path / "leftover-grep.sh"
    shutil.copy(LAUNCHER, copy)
    shutil.copy(PY, tmp_path / "leftover_grep.py")
    tools = tmp_path / "bin"
    tools.mkdir()
    dirname = shutil.which("dirname")
    assert dirname
    (tools / "dirname").symlink_to(dirname)
    proc = subprocess.run(
        ["/bin/sh", str(copy)], env={"PATH": str(tools)}, capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert "python3 is required" in proc.stderr


# --- (b) lint --------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck missing")
def test_launcher_shellcheck_clean():
    proc = subprocess.run(["shellcheck", str(LAUNCHER)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


@pytest.mark.skipif(shutil.which("shfmt") is None, reason="shfmt missing")
def test_launcher_shfmt_clean():
    proc = subprocess.run(
        ["shfmt", "-i", "2", "-ci", "-bn", "-d", str(LAUNCHER)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff missing")
def test_python_is_default_ruff_clean():
    check = subprocess.run(["ruff", "check", "--isolated", str(PY)], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout
    fmt = subprocess.run(
        ["ruff", "format", "--isolated", "--check", str(PY)], capture_output=True, text=True
    )
    assert fmt.returncode == 0, fmt.stdout


# --- (c) behavior ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("allow_console", "expected_rc", "marker", "absent"),
    [
        ("0", 1, "BLOCK [console] a.js:2", "warn  [console]"),
        ("1", 0, "warn  [console] a.js:2", "BLOCK [console]"),
    ],
)
def test_leftover_console_rule_warns_or_blocks(
    tmp_path: Path, allow_console: str, expected_rc: int, marker: str, absent: str
):
    repo = _repo(tmp_path, {"a.js": "const a = 1;\n"}, {"a.js": "const a = 1;\nconsole.log(a);\n"})
    proc = _run(repo, {"LEFTOVER_BASE": "main", "ALLOW_CONSOLE": allow_console})
    out = proc.stdout + proc.stderr
    assert proc.returncode == expected_rc, out
    assert marker in out
    assert absent not in out
    if expected_rc == 1:
        assert "::error file=a.js,line=2::[console]" in proc.stdout
        assert "0 blocking" not in out
    else:
        assert "::error" not in proc.stdout
        assert "0 blocking, 1 warning(s)." in out
        assert "[leftover] PASS." in proc.stdout


def test_every_rule_blocks_with_line_numbers(tmp_path: Path):
    body = "a\nit.only(1)\nfit(2)\n  debugger;\n<<<<<<< HEAD\n>>>>>>> x\nTODO x\n"
    repo = _repo(tmp_path, {"a.ts": "a\n"}, {"a.ts": body})
    proc = _run(repo, {"LEFTOVER_BASE": "main"})
    assert proc.returncode == 1
    for line, rule in [
        (2, "focused-test"),
        (3, "focused-test"),
        (4, "debugger"),
        (5, "merge-marker"),
        (6, "merge-marker"),
        (7, "untracked-todo"),
    ]:
        assert f"::error file=a.ts,line={line}::[{rule}]" in proc.stdout
    assert "[leftover] 6 blocking, 0 warning(s)." in proc.stderr
    assert "FAIL — remove the leftovers above" in proc.stderr


def test_default_ticket_forms_exclude_and_include(tmp_path: Path):
    feat = {
        "a.ts": "// TODO(AB-1)\n// FIXME #12\n// TODO https://x/y\n",
        "node_modules/x.js": "debugger;\n",
        "notes.txt": "debugger;\n",
        "x.min.js": "debugger;\n",
    }
    repo = _repo(tmp_path, {}, feat)
    proc = _run(repo, {"LEFTOVER_BASE": "main"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_posix_classes_in_user_regexes(tmp_path: Path):
    repo = _repo(tmp_path, {"a.ts": "a\n"}, {"a.ts": "a\n// TODO AB-1\n// TODO(x)\n"})
    env = {"LEFTOVER_BASE": "main", "TICKET_REGEX": "[[:upper:]]+-[[:digit:]]+"}
    proc = _run(repo, env)
    assert proc.returncode == 1
    assert "line=3::[untracked-todo]" in proc.stdout
    assert "line=2::" not in proc.stdout
    env = {"LEFTOVER_BASE": "main", "LEFTOVER_INCLUDE": "^[[:alpha:]]+\\.ts$"}
    assert _run(repo, env).returncode == 1
    env = {"LEFTOVER_BASE": "main", "LEFTOVER_EXCLUDE": "^[[:alnum:]]+\\.ts$"}
    assert _run(repo, env).returncode == 0


def test_invalid_include_regex_matches_nothing_like_grep(tmp_path: Path):
    repo = _repo(tmp_path, {"a.ts": "a\n"}, {"a.ts": "a\ndebugger;\n"})
    proc = _run(repo, {"LEFTOVER_BASE": "main", "LEFTOVER_INCLUDE": "(("})
    assert proc.returncode == 0
    assert "[leftover] PASS." in proc.stdout


def test_fulltree_scans_tracked_files_only(tmp_path: Path):
    repo = _repo(tmp_path, {"a.ts": "ok\n"}, {"b.ts": "one\n  debugger;\n"})
    (repo / "untracked.ts").write_text("debugger;\n")
    proc = _run(repo, {"LEFTOVER_FULLTREE": "1"})
    assert proc.returncode == 1
    assert "scanning full tree" in proc.stderr
    assert "::error file=b.ts,line=2::[debugger]" in proc.stdout
    assert "untracked.ts" not in proc.stdout


def test_default_without_base_falls_back_to_main_then_fulltree(tmp_path: Path):
    repo = _repo(tmp_path, {"a.ts": "ok\n"}, {"a.ts": "ok\ndebugger;\n"})
    proc = _run(repo)
    assert "scanning diff vs main" in proc.stderr
    assert proc.returncode == 1
    _git(repo, "branch", "-q", "-m", "main", "trunk")
    proc = _run(repo)
    assert "scanning full tree" in proc.stderr
    assert proc.returncode == 1


def test_unresolvable_explicit_base_fails_closed(tmp_path: Path):
    repo = _repo(tmp_path, {"a.ts": "ok\n"}, {"a.ts": "ok2\n"})
    proc = _run(repo, {"LEFTOVER_BASE": "nope"})
    assert proc.returncode == 1
    assert "requested diff base 'nope' does not resolve" in proc.stderr
    assert "refusing to scan nothing" in proc.stderr


def test_diff_parser_handles_hunk_numbers_and_deleted_files(tmp_path: Path):
    base = "l1\nl2\nl3\nl4\n"
    feat = "l1\nl2\nTODO new\nl3\nl4\ndebugger;\n"
    repo = _repo(tmp_path, {"a.ts": base, "gone.ts": "debugger;\n"}, {"a.ts": feat})
    (repo / "gone.ts").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rm")
    proc = _run(repo, {"LEFTOVER_BASE": "main"})
    assert proc.returncode == 1
    assert "line=3::[untracked-todo]" in proc.stdout
    assert "line=6::[debugger]" in proc.stdout
    assert "gone.ts" not in proc.stdout


def test_tab_edges_of_text_are_stripped_like_shell_read(tmp_path: Path):
    repo = _repo(tmp_path, {"a.ts": "a\n"}, {"a.ts": "a\n\t\tdebugger;\t\n"})
    proc = _run(repo, {"LEFTOVER_BASE": "main"})
    assert proc.returncode == 1
    assert "::error file=a.ts,line=2::[debugger] debugger;\n" in proc.stdout


def test_gate_does_not_trip_on_the_vendored_gate_files_it_ships_with(tmp_path: Path):
    """rig vendors every launcher + module of these four slots into a consumer; that PR's diff is
    scanned by this same gate, so no vendored file may contain a literal rule marker (focused-test
    syntax, an untracked open-item word, a conflict marker, ...)."""
    slots = ("dependency-review", "leftover-grep", "review-threads", "secret-scan")
    vendored = {
        f"ci/{slot}/{f.name}": f.read_text(encoding="utf-8")
        for slot in slots
        for f in sorted((REPO_ROOT / "ci" / slot).iterdir())
        if f.suffix in (".py", ".sh") and not f.name.startswith("test_")
    }
    assert len(vendored) == 8
    repo = _repo(tmp_path, {}, vendored)
    proc = _run(repo, {"LEFTOVER_BASE": "main"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 blocking" in proc.stderr
