"""Tests for the ``agenttools_dev`` CLI.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_dev.py -q
"""

from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


def _import_cli():
    from agenttools_dev import cli

    return cli


def test_package_import_is_stdlib_only():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_LIB)!r})\n"
        "import agenttools_dev\n"
        "assert 'yaml' not in sys.modules\n"
        "assert 'agenttools_config' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_run_string_script_appends_shell_quoted_args(tmp_path, monkeypatch):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {"scripts": {"echo": "python tool.py"}},
    )
    monkeypatch.setattr(
        cli,
        "_run_shell",
        lambda command, cwd: calls.append((command, cwd)) or 17,
    )

    code = cli.main(["run", "echo", "--", "two words", "$(rm -rf /)"])

    assert code == 17
    assert calls == [("python tool.py 'two words' '$(rm -rf /)'", tmp_path)]


def test_run_mapping_script_uses_cmd(tmp_path, monkeypatch):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {"scripts": {"test": {"cmd": "uv run pytest"}}},
    )
    monkeypatch.setattr(
        cli,
        "_run_shell",
        lambda command, cwd: calls.append((command, cwd)) or 0,
    )

    assert cli.main(["run", "test"]) == 0
    assert calls == [("uv run pytest", tmp_path)]


def test_run_rejects_destructive_script_command(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(cli, "_load_rig_config", lambda _root: {"scripts": {"test": "rm -rf ."}})
    monkeypatch.setattr(cli, "_run_shell", lambda command, cwd: calls.append((command, cwd)) or 0)

    assert cli.main(["run", "test"]) == 2

    captured = capsys.readouterr()
    assert "not a permitted development/e2e command" in captured.err
    assert calls == []


def test_run_allows_project_local_shell_wrapper(tmp_path, monkeypatch):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []
    script = tmp_path / "scripts" / "test.sh"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\npytest\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(cli, "_load_rig_config", lambda _root: {"scripts": {"test": "bash scripts/test.sh"}})
    monkeypatch.setattr(cli, "_run_shell", lambda command, cwd: calls.append((command, cwd)) or 0)

    assert cli.main(["run", "test"]) == 0
    assert calls == [("bash scripts/test.sh", tmp_path)]


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'rm -rf .'",
        "uv run bash -euo pipefail -c 'rm -rf .'",
        "npm exec bash -lc 'rm -rf .'",
        "python -c 'open(\"pwned\", \"w\").write(\"x\")'",
        "node -e 'require(\"fs\").writeFileSync(\"pwned\", \"x\")'",
        "uv run python -c 'open(\"pwned\", \"w\").write(\"x\")'",
        "pnpm exec node -e 'require(\"fs\").writeFileSync(\"pwned\", \"x\")'",
        "npm test > ~/.zshrc",
        "node app.js >> secrets.txt",
    ],
)
def test_run_rejects_unsafe_lifecycle_payloads(tmp_path, monkeypatch, capsys, command):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(cli, "_load_rig_config", lambda _root: {"scripts": {"test": command}})
    monkeypatch.setattr(cli, "_run_shell", lambda command, cwd: calls.append((command, cwd)) or 0)

    assert cli.main(["run", "test"]) == 2

    captured = capsys.readouterr()
    assert "not a permitted development/e2e command" in captured.err
    assert calls == []


@pytest.mark.parametrize("command", ["bash -lc 'npm test'", "uv run bash -lc 'pytest tests/'"])
def test_run_allows_safe_shell_c_payloads(tmp_path, monkeypatch, command):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(cli, "_load_rig_config", lambda _root: {"scripts": {"test": command}})
    monkeypatch.setattr(cli, "_run_shell", lambda command, cwd: calls.append((command, cwd)) or 0)

    assert cli.main(["run", "test"]) == 0
    assert calls == [(command, tmp_path)]


def test_run_missing_script_exits_2_with_actionable_error(tmp_path, monkeypatch, capsys):
    cli = _import_cli()

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(cli, "_load_rig_config", lambda _root: {"scripts": {"test": "pytest"}})

    code = cli.main(["run", "lint"])

    captured = capsys.readouterr()
    assert code == 2
    assert "script 'lint' is not defined" in captured.err
    assert "rig.yaml" in captured.err


def test_has_script_checks_top_level_scripts(tmp_path, monkeypatch):
    cli = _import_cli()

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(cli, "_load_rig_config", lambda _root: {"scripts": {"test": "pytest"}})

    assert cli.main(["has-script", "test"]) == 0
    assert cli.main(["has-script", "lint"]) == 1


def test_run_e2e_job_from_dev_metadata(tmp_path, monkeypatch):
    cli = _import_cli()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: tmp_path)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {"e2e": {"jobs": {"smoke": {"script": "e2e-smoke"}}}},
        },
    )
    monkeypatch.setattr(
        cli,
        "_run_shell",
        lambda command, cwd: calls.append((command, cwd)) or 0,
    )

    assert cli.main(["run", "smoke", "--", "--project=chromium"]) == 0
    assert calls == [("pnpm exec playwright test --project=chromium", tmp_path)]


def test_start_server_writes_state_file(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    started: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": "pnpm run dev"},
            "dev": {"server": {"script": "server", "ports": [5173]}},
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(
        cli,
        "_start_process",
        lambda command, cwd: started.append((command, cwd)) or 2468,
    )

    assert cli.main(["start", "server"]) == 0

    captured = capsys.readouterr()
    assert "started server server pid 2468" in captured.out
    assert started == [("pnpm run dev", repo)]
    record = json.loads((state / "server-server.json").read_text(encoding="utf-8"))
    assert record["pid"] == 2468
    assert record["pgid"] == 2468
    assert record["kind"] == "server"
    assert record["name"] == "server"
    assert record["port"] == 5173
    assert record["ports"] == [5173]


def test_start_e2e_job_writes_state_file(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {"e2e": {"jobs": {"smoke": {"script": "e2e-smoke"}}}},
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_start_process", lambda command, cwd: 1357)

    assert cli.main(["start", "smoke", "--", "--debug"]) == 0

    record = json.loads((state / "e2e-smoke.json").read_text(encoding="utf-8"))
    assert record["kind"] == "e2e"
    assert record["command"] == "pnpm exec playwright test --debug"


def test_list_reports_configured_and_running_targets(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    (state / "server-server.json").write_text(
        json.dumps({
            "kind": "server",
            "name": "server",
            "pid": 2468,
            "pgid": 2468,
            "command": "pnpm run dev",
            "cwd": str(repo),
            "port": 5173,
            "ports": [5173],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {
                "server": "pnpm run dev",
                "e2e-smoke": "pnpm exec playwright test",
            },
            "dev": {
                "server": {"script": "server", "ports": [5173]},
                "e2e": {"jobs": {"smoke": {"script": "e2e-smoke"}}},
            }
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 2468)

    assert cli.main(["list"]) == 0

    captured = capsys.readouterr()
    assert "server server running pid=2468 ports=5173" in captured.out
    assert "e2e smoke configured" in captured.out


def test_stop_named_server_uses_state_and_validates_before_signal(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    (state / "server-server.json").write_text(
        json.dumps({
            "kind": "server",
            "name": "server",
            "pid": 2468,
            "pgid": 2468,
            "command": "pnpm run dev",
            "cwd": str(repo),
            "port": 5173,
            "ports": [5173],
        }),
        encoding="utf-8",
    )
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": "pnpm run dev"},
            "dev": {"server": {"script": "server", "ports": [5173]}},
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 2468)
    monkeypatch.setattr(cli, "_process_group_members", lambda pgid: [2468])
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "pnpm run dev")
    monkeypatch.setattr(cli, "_send_process_group_signal", lambda pgid, sig: sent.append((pgid, sig)))

    assert cli.main(["stop", "server"]) == 0
    assert sent == [(2468, signal.SIGTERM)]
    assert not (state / "server-server.json").exists()


def test_stop_named_server_can_resolve_configured_port(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": "pnpm run dev"},
            "dev": {"server": {"script": "server", "ports": [5173]}},
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_pid_for_port", lambda port: 8642)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "pnpm run dev")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    assert cli.main(["stop", "server"]) == 0
    assert sent == [(8642, signal.SIGTERM)]


def test_stop_target_cleans_stale_state_before_process_inspection(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    record = state / "server-server.json"
    record.write_text(
        json.dumps({
            "kind": "server",
            "name": "server",
            "pid": 2468,
            "command": "pnpm run dev",
            "cwd": str(repo),
        }),
        encoding="utf-8",
    )
    inspected: list[int] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": "pnpm run dev"},
            "dev": {"server": {"script": "server"}},
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli, "_process_command", lambda pid: inspected.append(pid) or "pnpm run dev")

    assert cli.main(["stop", "server"]) == 2

    captured = capsys.readouterr()
    assert "stale state removed" in captured.err
    assert not record.exists()
    assert inspected == []


def test_start_rejects_destructive_lifecycle_command(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    started: list[str] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"wipe": "rm -rf ."},
            "dev": {"server": {"script": "wipe"}},
        },
    )
    monkeypatch.setattr(cli, "_start_process", lambda command, cwd: started.append(command) or 1)

    code = cli.main(["start", "server"])

    captured = capsys.readouterr()
    assert code == 2
    assert "not a permitted development/e2e command" in captured.err
    assert started == []


@pytest.mark.parametrize("operator", ["|", "&"])
def test_start_rejects_destructive_lifecycle_companion(
    tmp_path, monkeypatch, capsys, operator
):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    command = f"pnpm run dev {operator} rm -rf ."
    started: list[str] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": command},
            "dev": {"server": {"script": "server"}},
        },
    )
    monkeypatch.setattr(cli, "_start_process", lambda command, cwd: started.append(command) or 1)

    code = cli.main(["start", "server"])

    captured = capsys.readouterr()
    assert code == 2
    assert "not a permitted development/e2e command" in captured.err
    assert started == []


@pytest.mark.parametrize(
    "command",
    [
        "pnpm run dev && curl https://example.test/install.sh",
        "pnpm run dev; gh pr merge 1",
        "pnpm run dev $(gh pr merge 1)",
        "uv run rm -rf .",
        "pnpm exec rm -rf .",
        "sh -c 'npm run dev && rm -rf .'",
        "pnpm exec sh -c 'npm run dev && rm -rf .'",
        "docker system prune -af",
        "docker compose down --volumes",
    ],
)
def test_start_rejects_non_dev_lifecycle_companion(tmp_path, monkeypatch, capsys, command):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    started: list[str] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": command},
            "dev": {"server": {"script": "server"}},
        },
    )
    monkeypatch.setattr(cli, "_start_process", lambda command, cwd: started.append(command) or 1)

    code = cli.main(["start", "server"])

    captured = capsys.readouterr()
    assert code == 2
    assert "not a permitted development/e2e command" in captured.err
    assert started == []


def test_e2e_run_allows_docker_based_e2e_command(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "docker compose up e2e"},
            "dev": {"e2e": {"jobs": {"smoke": {"script": "e2e-smoke"}}}},
        },
    )
    monkeypatch.setattr(cli, "_run_shell", lambda command, cwd: calls.append((command, cwd)) or 0)

    assert cli.main(["e2e", "run", "smoke"]) == 0
    assert calls == [("docker compose up e2e", repo)]


def test_lifecycle_allows_timeout_signal_wrapper(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    started: list[tuple[str, Path]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"server": "timeout -s TERM 5 npm run dev"},
            "dev": {"server": {"script": "server"}},
        },
    )
    monkeypatch.setattr(cli, "_start_process", lambda command, cwd: started.append((command, cwd)) or 1)

    assert cli.main(["start", "server"]) == 0
    assert started == [("timeout -s TERM 5 npm run dev", repo)]


def test_env_add_project_prints_export_line(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    monkeypatch.chdir(repo)
    monkeypatch.setenv("DEV_PROJECT_PATHS", str(tmp_path / "already"))

    code = cli.main(["env", "--add-project", "../other"])

    captured = capsys.readouterr()
    assert code == 0
    expected = os.pathsep.join([str(tmp_path / "already"), str(other.resolve())])
    assert captured.out.strip() == f"export DEV_PROJECT_PATHS='{expected}'"


def test_env_add_project_resolves_relative_path_from_repo_root(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    subdir = repo / "subdir"
    other = tmp_path / "api"
    (repo / ".git").mkdir(parents=True)
    subdir.mkdir()
    other.mkdir()

    monkeypatch.chdir(subdir)

    assert cli.main(["env", "--add-project", "../api"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == f"export DEV_PROJECT_PATHS='{other.resolve()}'"


def test_stop_pid_validates_dev_tool_and_project_before_terminating(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    app = repo / "app"
    app.mkdir(parents=True)
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: app)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "npm run dev")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    assert cli.main(["stop", "--pid", "1234"]) == 0
    assert sent == [(1234, signal.SIGTERM)]


def test_stop_pid_rejects_unrelated_process_before_terminating(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: outside)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "/Applications/Slack.app/Slack")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    code = cli.main(["stop", "--pid", "999"])

    captured = capsys.readouterr()
    assert code == 2
    assert "not recognized as a development tool" in captured.err
    assert sent == []


def test_stop_pid_rejects_dev_tool_outside_project(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: outside)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "npm run dev")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    code = cli.main(["stop", "--pid", "1000"])

    captured = capsys.readouterr()
    assert code == 2
    assert "does not appear scoped to" in captured.err
    assert sent == []


def test_stop_pid_rejects_outside_cwd_even_if_command_mentions_repo(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: outside)
    monkeypatch.setattr(cli, "_process_command", lambda pid: f"node /elsewhere/app.js {repo / 'config.json'}")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    code = cli.main(["stop", "--pid", "1001"])

    captured = capsys.readouterr()
    assert code == 2
    assert "does not appear scoped to" in captured.err
    assert sent == []


def test_stop_pid_rejects_unknown_cwd_even_if_command_mentions_repo(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: None)
    monkeypatch.setattr(cli, "_process_command", lambda pid: f"node /elsewhere/app.js {repo / 'config.json'}")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    code = cli.main(["stop", "--pid", "1002"])

    captured = capsys.readouterr()
    assert code == 2
    assert "does not appear scoped to" in captured.err
    assert sent == []


def test_stop_pid_allows_extra_project_paths(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    sibling = tmp_path / "sibling"
    repo.mkdir()
    sibling.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setenv("DEV_PROJECT_PATHS", str(sibling))
    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: sibling)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "vite --host 0.0.0.0")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    assert cli.main(["stop", "--pid", "4321"]) == 0
    assert sent == [(4321, signal.SIGTERM)]


def test_stop_port_maps_to_pid_then_validates_and_terminates(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_pid_for_port", lambda port: 2468)
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "python -m http.server 8000")
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))

    assert cli.main(["stop", "--port", "8000"]) == 0
    assert sent == [(2468, signal.SIGTERM)]


def test_stop_pgid_validates_group_members_before_terminating(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_group_members", lambda pgid: [101, 102])
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo / "app")
    monkeypatch.setattr(
        cli,
        "_process_command",
        lambda pid: "pnpm run dev" if pid == 101 else "node ./node_modules/vite/bin/vite.js",
    )
    monkeypatch.setattr(cli, "_send_process_group_signal", lambda pgid, sig: sent.append((pgid, sig)))

    assert cli.main(["stop", "--pgid", "5001"]) == 0
    assert sent == [(5001, signal.SIGTERM)]


def test_stop_pgid_skips_member_that_exits_during_inspection(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    repo.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_group_members", lambda pgid: [101, 102])
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo / "app")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 101)

    def process_command(pid: int) -> str:
        if pid == 102:
            raise cli.DevError("cannot inspect process command for pid 102")
        return "pnpm run dev"

    monkeypatch.setattr(cli, "_process_command", process_command)
    monkeypatch.setattr(cli, "_send_process_group_signal", lambda pgid, sig: sent.append((pgid, sig)))

    assert cli.main(["stop", "--pgid", "5001"]) == 0
    assert sent == [(5001, signal.SIGTERM)]


def test_stop_pgid_rejects_unscoped_member_before_terminating(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_process_group_members", lambda pgid: [201, 202])
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo if pid == 201 else outside)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "pnpm run dev")
    monkeypatch.setattr(cli, "_send_process_group_signal", lambda pgid, sig: sent.append((pgid, sig)))

    code = cli.main(["stop", "--pgid", "5002"])

    captured = capsys.readouterr()
    assert code == 2
    assert "pid 202 does not appear scoped" in captured.err
    assert sent == []


def test_status_e2e_reports_state_and_artifacts(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    latest = repo / "e2e" / "docker-artifacts" / "run-grp-002"
    latest.mkdir(parents=True)
    (latest / "playwright.log").write_text("passed=3\nfailed=0\n", encoding="utf-8")
    (latest / "exit-code").write_text("0\n", encoding="utf-8")
    state.mkdir()
    (state / "e2e-smoke.json").write_text(
        json.dumps({
            "kind": "e2e",
            "name": "smoke",
            "pid": 1357,
            "command": "pnpm exec playwright test",
            "cwd": str(repo),
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {
                "e2e": {
                    "jobs": {
                        "smoke": {
                            "script": "e2e-smoke",
                            "artifacts_root": "e2e/docker-artifacts",
                            "logs_root": "e2e/docker-artifacts",
                        },
                    }
                }
            }
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 1357)

    assert cli.main(["status", "smoke"]) == 0

    captured = capsys.readouterr()
    assert "e2e smoke running pid=1357" in captured.out
    assert f"latest_run={latest}" in captured.out
    assert "exit_code=0" in captured.out
    assert f"log={latest / 'playwright.log'}" in captured.out


def test_e2e_logs_prints_configured_log_tail(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    latest = repo / "e2e" / "docker-artifacts" / "run-grp-002"
    latest.mkdir(parents=True)
    (latest / "playwright.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {
                "e2e": {
                    "jobs": {
                        "smoke": {
                            "script": "e2e-smoke",
                            "artifacts_root": "e2e/docker-artifacts",
                            "logs_root": "e2e/docker-artifacts",
                        },
                    }
                }
            }
        },
    )

    assert cli.main(["e2e", "logs", "smoke", "--tail", "2"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["two", "three"]


def test_e2e_logs_prefers_e2e_target_when_server_name_matches(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    latest = repo / "e2e" / "docker-artifacts" / "run-grp-002"
    latest.mkdir(parents=True)
    (repo / "server.log").write_text("server\n", encoding="utf-8")
    (latest / "playwright.log").write_text("e2e\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {
                "server": "pnpm run dev",
                "e2e-smoke": "pnpm exec playwright test",
            },
            "dev": {
                "server": {"script": "server", "logs_root": "."},
                "e2e": {
                    "jobs": {
                        "smoke": {
                            "script": "e2e-smoke",
                            "artifacts_root": "e2e/docker-artifacts",
                            "logs_root": "e2e/docker-artifacts",
                        },
                    }
                },
            }
        },
    )

    assert cli.main(["e2e", "logs", "smoke"]) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["e2e"]


def test_e2e_logs_rejects_status_log_outside_project(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.log"
    repo.mkdir()
    outside.write_text("secret\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {
                "e2e": {
                    "jobs": {"smoke": {"script": "e2e-smoke", "logs_root": str(outside)}}
                }
            }
        },
    )

    assert cli.main(["e2e", "logs", "smoke"]) == 2

    captured = capsys.readouterr()
    assert "outside the current project" in captured.err
    assert "secret" not in captured.out


def test_e2e_status_rejects_artifacts_root_outside_project(tmp_path, monkeypatch, capsys):
    cli = _import_cli()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {
                "e2e": {
                    "jobs": {"smoke": {"script": "e2e-smoke", "artifacts_root": "../outside"}}
                }
            }
        },
    )

    assert cli.main(["status", "smoke"]) == 2

    captured = capsys.readouterr()
    assert "outside the current project" in captured.err


def test_e2e_run_and_stop_aliases(tmp_path, monkeypatch):
    cli = _import_cli()
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    calls: list[tuple[str, Path]] = []
    sent: list[tuple[int, signal.Signals]] = []
    (state / "e2e-smoke.json").write_text(
        json.dumps({
            "kind": "e2e",
            "name": "smoke",
            "pid": 4242,
            "pgid": 4242,
            "command": "pnpm exec playwright test",
            "cwd": str(repo),
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(
        cli,
        "_load_rig_config",
        lambda _root: {
            "scripts": {"e2e-smoke": "pnpm exec playwright test"},
            "dev": {"e2e": {"jobs": {"smoke": {"script": "e2e-smoke"}}}},
        },
    )
    monkeypatch.setattr(cli, "_state_dir", lambda _root: state)
    monkeypatch.setattr(cli, "_run_shell", lambda command, cwd: calls.append((command, cwd)) or 0)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(cli, "_process_group_members", lambda pgid: [4242])
    monkeypatch.setattr(cli, "_process_cwd", lambda pid: repo)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "pnpm exec playwright test")
    monkeypatch.setattr(cli, "_send_process_group_signal", lambda pgid, sig: sent.append((pgid, sig)))

    assert cli.main(["e2e", "run", "smoke", "--", "--headed"]) == 0
    assert cli.main(["e2e", "stop", "smoke"]) == 0
    assert calls == [("pnpm exec playwright test --headed", repo)]
    assert sent == [(4242, signal.SIGTERM)]


def test_pid_for_port_uses_lsof_output():
    cli = _import_cli()

    def fake_run(args, **_kwargs):
        assert args[:4] == ["lsof", "-nP", "-iTCP:5173", "-sTCP:LISTEN"]
        return SimpleNamespace(returncode=0, stdout="13579\n", stderr="")

    assert cli._pid_for_port(5173, runner=fake_run) == 13579


def test_pid_for_port_rejects_multiple_unique_lsof_pids():
    cli = _import_cli()

    def fake_run(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="13579\n24680\n13579\n", stderr="")

    with pytest.raises(cli.DevError, match="multiple listening processes"):
        cli._pid_for_port(5173, runner=fake_run)
