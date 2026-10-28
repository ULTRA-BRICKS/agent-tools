"""Tests for the server-offload-gate agent-hook (pre-bash, hard block with Telegram hatch).

In a repo whose AGENTS.md/CLAUDE.md opts in (`server-run` / "Heavy work runs on the server"),
a whole-suite shape (pytest on a dir, bun test with no file, mypy on a tree, docker build, …)
is blocked locally and the `server-run -- bash -c '<cmd>'` equivalent is named. Added after
the 2026-10-10/11 thrash incidents. See the hook module's docstring.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_server_offload_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[1] / "agent-hooks" / "server-offload-gate" / "server_offload_gate.py"
_spec = importlib.util.spec_from_file_location("server_offload_gate", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


@pytest.fixture
def repo(tmp_path):
    """An opted-in repo with a real `tests/` tree and one test file."""
    (tmp_path / "AGENTS.md").write_text("## Heavy work runs on the server\nuse `server-run -- …`\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _no_hatch(monkeypatch):
    """Never reach tg-ctl from a unit test; the hatch result is 'not requested'."""
    monkeypatch.setattr(hook.hatch_escalation, "request_hatch_approval", lambda *a, **k: hook._UnavailableHatchResult())
    monkeypatch.delenv("RIG_HATCH_REQUEST_SERVER_OFFLOAD_GATE", raising=False)


# ── repo opt-in ───────────────────────────────────────────────────────────────────

def test_opt_in_found_in_parent_agents_md(repo):
    assert hook.repo_opt_in_file(str(repo / "tests" / "unit")) == repo / "AGENTS.md"


def test_opt_in_accepts_claude_md_and_either_marker(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("Heavy work runs on the server.\n", encoding="utf-8")
    assert hook.repo_opt_in_file(str(tmp_path)) == tmp_path / "CLAUDE.md"


def test_no_opt_in_without_marker(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Run `pytest tests/` before every commit.\n", encoding="utf-8")
    assert hook.repo_opt_in_file(str(tmp_path)) is None


# ── classification ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "pytest",
    "pytest -q",
    "pytest tests/",
    "pytest tests",
    "pytest tests/unit",
    "uv run pytest tests/",
    "uv run --no-sync pytest -q tests/integration/ -x",
    "uv run --with pytest python -m pytest tests",
    "python3 -m pytest",
    "poetry run pytest",
    "coverage run -m pytest tests/",
    "timeout 600 uv run pytest tests/",
    "cd sub && CI=1 pytest tests/",
    "env CI=1 pytest",
    "pytest tests/unit/test_x.py tests/unit/test_y.py tests/unit/test_z.py",
    "pytest tests/nonexistent_dir",
    "pytest -k slow",  # `-k` consumes `slow`; no path remains
    "scripts/check.sh",
    "./scripts/check.sh --fast",
    "bash -c 'uv sync --frozen && scripts/check.sh'",
    "sh -lc 'cd /repo && bun test ./tests'",
    "command pytest tests/",
    "exec bun test",
    "exec -a x pytest",
    "builtin command pytest tests/",
    "cargo --manifest-path Cargo.toml test",
    "npm --prefix packages/web test",
    "bun --cwd packages/web test",
    "pnpm -C packages/web run test",
    "docker -H unix:///var/run/docker.sock build .",
    "bash scripts/check.sh",
    "mypy",
    "mypy src",
    "uv run mypy src/",
    "mypy -p pkg",
    "mypy --package pkg",
    "mypy src/mod.py src/other.py",
    "python -m mypy src",
    "bun test",
    "bun test ./tests",
    "bun test tests",
    "timeout 900 bun test ./tests",
    "vitest",
    "vitest run",
    "vitest run tests/",
    "npx vitest run",
    "jest",
    "jest src",
    "npm test",
    "npm t",
    "npm run test",
    "pnpm test",
    "yarn test",
    "yarn run test",
    "bun run test",
    "cargo test",
    "cargo test --workspace",
    "go test ./...",
    "go test",
    "docker build .",
    "docker build -t x .",
    "docker buildx build .",
    "docker compose up -d",
    "docker-compose up",
    "git pull && pytest tests/",
    "echo $(pytest tests/)",
    "if true; then bun test; fi",
    "npx -c 'vitest run'",
])
def test_whole_suite_shapes_are_detected(command, repo):
    assert hook.classify_whole_suite(command, str(repo)) is not None, command


@pytest.mark.parametrize("command", [
    "pytest tests/unit/test_x.py",
    "uv run pytest tests/unit/test_x.py",
    "uv run --no-sync pytest -q tests/unit/test_x.py -x -k smoke",
    "pytest tests/unit/test_x.py::TestFoo::test_bar",
    "pytest tests/unit/test_x.py tests/unit/test_y.py",  # two files: the cap
    "python -m pytest tests/unit/test_x.py",
    "pytest --version",
    "pytest --help",
    "pytest --collect-only tests/",
    "mypy src/mod.py",
    "uv run mypy --strict src/mod.py",
    "mypy --version",
    "bun test tests/x.test.ts",
    "bun test tests/a.test.ts tests/b.test.ts",
    "vitest run tests/x.test.ts",
    "jest src/x.test.ts",
    "bun run dev",
    "bun install",
    "npm run build",
    "npm run lint",
    "cargo test --test integ",
    "cargo build",
    "go test ./pkg",
    "go build ./...",
    "docker ps",
    "docker compose down",
    "docker compose logs",
    "git commit -m 'run pytest tests/ on the server'",
    "echo 'bun test'",
    "tg 'bun test ./tests froze the box'",
    "grep -rn 'pytest tests/' docs/",
    "cat <<'EOF'\npytest tests/\nEOF",
    "server-run -- bun test ./tests",  # the offload itself is not a local run
    "pytest tests/unit/test_x.py -q 2>&1 | tail -5",  # fd redirection is not an operand
    "pytest tests/unit/test_x.py >out.txt 2>&1",
    "bun test tests/x.test.ts 2>/dev/null",
    "pytest tests/unit/test_x.py 2> err.txt",
    "command -v pytest",
    "command -V bun",
    "exec pytest tests/unit/test_x.py",
    "ls tests/",
    "",
])
def test_single_file_and_unrelated_commands_are_allowed(command, repo):
    assert hook.classify_whole_suite(command, str(repo)) is None, command


def test_unbalanced_quotes_fail_open(repo):
    assert hook.classify_whole_suite("pytest tests/ 'oops", str(repo)) is None


def test_directory_operand_heuristics(repo):
    cwd = str(repo)
    assert hook._is_directory_operand("tests/", cwd)
    assert hook._is_directory_operand("tests", cwd)
    assert hook._is_directory_operand("tests/unit", cwd)
    assert not hook._is_directory_operand("tests/unit/test_x.py", cwd)
    assert not hook._is_directory_operand("tests/unit/test_x.py::TestFoo", cwd)
    assert not hook._is_directory_operand("tests/unit/test_missing.py", cwd), "a missing .py is still a file"
    assert hook._is_directory_operand("tests/missing", cwd), "a missing extensionless path is a tree"


# ── the block message ─────────────────────────────────────────────────────────────

def test_block_message_quotes_the_original_command_for_server_run(repo):
    command = "cd sub && uv run --no-sync pytest -q tests/integration/ -x"
    message = hook.block_message(command, "pytest on a directory", repo / "AGENTS.md")
    assert f"server-run -- bash -c '{command}'" in message
    assert "ONE test file is fine" in message
    assert "RIG_HATCH_REQUEST_SERVER_OFFLOAD_GATE" in message
    assert str(repo / "AGENTS.md") in message


def test_block_message_survives_single_quotes_in_the_command(repo):
    command = "pytest tests/ -k 'not slow'"
    message = hook.block_message(command, "pytest on a directory", repo / "AGENTS.md")
    assert "server-run -- bash -c 'pytest tests/ -k '\"'\"'not slow'\"'\"''" in message


# ── main() end-to-end wiring ──────────────────────────────────────────────────────

def _run(command, cwd, monkeypatch, *, args_extra=None) -> tuple[dict, int]:
    args = {"command": command, **(args_extra or {})}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args, "cwd": str(cwd)})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    code = hook.main()
    return json.loads(out.getvalue()), code


def test_main_blocks_a_directory_run_in_an_opted_in_repo(repo, monkeypatch):
    payload, code = _run("uv run pytest tests/", repo, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert payload["decision"] == "block"
    assert "server-run -- bash -c 'uv run pytest tests/'" in payload["message"]


def test_main_allows_one_file_in_an_opted_in_repo(repo, monkeypatch):
    payload, code = _run("uv run pytest tests/unit/test_x.py", repo, monkeypatch)
    assert (payload["decision"], code) == ("allow", 0)


def test_main_allows_a_whole_suite_where_the_repo_has_not_opted_in(tmp_path, monkeypatch):
    payload, code = _run("uv run pytest tests/", tmp_path, monkeypatch)
    assert (payload["decision"], code) == ("allow", 0)


def test_main_governs_subagents_too(repo, monkeypatch):
    """The 2026-10-10 swarm was subagents: no agent_id exemption here."""
    payload, code = _run("bun test ./tests", repo, monkeypatch, args_extra={"agent_id": "sub-1", "agent_type": "task"})
    assert (payload["decision"], code) == ("block", hook.BLOCK_EXIT_CODE)


def test_main_honors_an_approved_hatch(repo, monkeypatch):
    class Approved:
        should_stop = True
        approved = True
        reason = "approved by Alex"

    monkeypatch.setattr(hook.hatch_escalation, "request_hatch_approval", lambda *a, **k: Approved())
    payload, code = _run("bun test", repo, monkeypatch)
    assert (payload["decision"], code) == ("allow", 0)


def test_main_reports_a_denied_hatch_without_the_usage_blurb(repo, monkeypatch):
    class Denied:
        should_stop = True
        approved = False
        reason = "declined"

    monkeypatch.setattr(hook.hatch_escalation, "request_hatch_approval", lambda *a, **k: Denied())
    payload, code = _run("bun test", repo, monkeypatch)
    assert code == hook.BLOCK_EXIT_CODE
    assert payload["message"] == "hatch escalation denied: declined"


def test_main_fails_open_on_unparseable_stdin(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert hook.main() == 0
    assert json.loads(out.getvalue())["decision"] == "allow"
