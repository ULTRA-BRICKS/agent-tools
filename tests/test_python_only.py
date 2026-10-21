"""The python-only gate (ci/python-only): rig-ecosystem tools are written in Python.

Owner decision 2026-06-14, restated 2026-10-17 (agent-tools#945). One stdlib checker runs
as the global pre-commit fragment (staged diff) and as the CI slot (base...head diff). It
fails on added/changed non-Python source in a rig-ecosystem tool repo unless the path is on
the central allowlist with a reason; tg-cli is warn-only until its port lands.

Run from the repo root::

    uv run --with pytest pytest tests/test_python_only.py -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "ci" / "python-only" / "python_only.py"
FRAGMENT = REPO_ROOT / "git-hooks" / "global-dispatcher" / "global-hooks.d" / "pre-commit" / "20-python-only"
WORKFLOW = REPO_ROOT / "ci" / "python-only" / "workflow.yml"

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

PY_ONLY_ORIGIN = "https://git.hyperide.ai/ultrabricks/demo-cli.git"
AGENT_TOOLS_ORIGIN = "https://git.hyperide.ai/ultrabricks/agent-tools.git"
TG_ORIGIN = "git@git.hyperide.ai:ultrabricks/tg-cli.git"
PRODUCT_ORIGIN = "https://git.hyperide.ai/ultrabricks/boards-app.git"


def _load():
    spec = importlib.util.spec_from_file_location("python_only", CHECKER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["python_only"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def po():
    return _load()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV, stdin=subprocess.DEVNULL,
    ).stdout


def _repo(tmp_path: Path, origin: str | None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    if origin:
        _git(repo, "remote", "add", "origin", origin)
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _put(repo: Path, rel: str, text: str, *, mode: int | None = None) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        p.chmod(mode)
    _git(repo, "add", "-A")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=repo, capture_output=True, text=True, env=_GIT_ENV, stdin=subprocess.DEVNULL,
    )


def _shell(lines: int) -> str:
    return "#!/bin/sh\n" + "".join(f"echo {i}\n" for i in range(lines - 1))


# --- scope ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://git.hyperide.ai/ultrabricks/tg-cli.git", ("git.hyperide.ai/ultrabricks", "tg-cli")),
        ("https://git.hyperide.ai/ultrabricks/tg-cli", ("git.hyperide.ai/ultrabricks", "tg-cli")),
        ("git@git.hyperide.ai:ultrabricks/tg-cli.git", ("git.hyperide.ai/ultrabricks", "tg-cli")),
        ("ssh://git@git.hyperide.ai:2222/ultrabricks/tg-cli.git", ("git.hyperide.ai/ultrabricks", "tg-cli")),
        ("https://token@github.com/Alex-Mextner/router-cli/", ("github.com/alex-mextner", "router-cli")),
        ("git.hyperide.ai:ultrabricks/x-cli.git", ("git.hyperide.ai/ultrabricks", "x-cli")),
        ("git@github.com-work:ultrabricks/X-CLI.GIT", ("github.com/ultrabricks", "x-cli")),
        ("/local/path/repo", None),
        ("", None),
    ],
)
def test_repo_identity_parses_remote_forms(po, url, expected):
    assert po.repo_identity(url) == expected


def test_scope_covers_owner_cli_repos_and_named_tools_only(po):
    assert po.policy_for("https://github.com/alex-mextner/router-cli.git").mode == "block"
    assert po.policy_for("https://git.hyperide.ai/ultrabricks/brand-new-cli").mode == "block"
    assert po.policy_for(AGENT_TOOLS_ORIGIN).name == "agent-tools"
    assert po.policy_for("https://git.hyperide.ai/ultrabricks/text-lint.git").mode == "block"
    tg = po.policy_for(TG_ORIGIN)
    assert tg.mode == "warn" and tg.port_ticket.endswith("/tg-cli/issues/768")
    # product repos and third-party *-cli repos are out of scope
    assert po.policy_for(PRODUCT_ORIGIN) is None
    assert po.policy_for("https://github.com/someone-else/gh-cli.git") is None
    assert po.policy_for("") is None


def test_shipped_registry_is_valid(po):
    assert po.validate_registry() == []


def test_validate_registry_flags_a_reasonless_entry_and_an_unticketed_warn(po):
    bad = {
        "x-cli": po.RepoPolicy("x-cli", allow=(po.Allow("a/**", " "),)),
        "y-cli": po.RepoPolicy("y-cli", mode="warn"),
    }
    problems = po.validate_registry(bad)
    assert any("x-cli" in p and "reason" in p for p in problems)
    assert any("y-cli" in p and "port_ticket" in p for p in problems)


# --- classification ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "a.ts", "b/c.tsx", "d.mjs", "e.cjs", "f.js", "g.go", "h.rs", "i.mts", "j.swift", "k.cpp", "l.c",
        "m.vue", "n.zig", "o.ps1", "p.awk",
    ],
)
def test_non_python_extensions_are_flagged_at_any_length(po, path):
    assert po.classify(path, b"x\n") is not None


@pytest.mark.parametrize("path", ["a.py", "README.md", "w.yml", "c.json", "p.toml", "t.txt", "bin/tool"])
def test_python_and_non_source_files_pass(po, path):
    assert po.classify(path, b"plain text\n" * 200) is None


@pytest.mark.parametrize(
    "body, flagged",
    [
        (b"#!/usr/bin/env python3\n" + b"x\n" * 200, False),
        (b"import os\n" * 200, False),
        (b"#!/usr/bin/env node\nconsole.log(1)\n", True),
        (b"#!/bin/bash\n" + b"x\n" * 200, True),
    ],
)
def test_a_py_file_is_python_only_if_it_runs_under_python(po, body, flagged):
    assert (po.classify("tool.py", body) is not None) is flagged


def test_shell_passes_up_to_the_line_limit_and_fails_above_it(po):
    assert po.SHELL_LINE_LIMIT == 40
    assert po.classify("glue.sh", _shell(40).encode()) is None
    found = po.classify("big.sh", _shell(41).encode())
    assert found is not None and "41 lines" in found
    assert po.classify("big.ksh", b"x\n" * 41) is not None


@pytest.mark.parametrize(
    "shebang, flagged",
    [
        ("#!/usr/bin/env bash", True),
        ("#!/bin/sh", True),
        ("#!/usr/bin/env -S bun run", True),
        ("#!/usr/bin/env node", True),
        ("#!/home/u/.bun/bin/bun", True),
        ("#!/usr/bin/env -u LC_ALL sh", True),
        ("\ufeff#!/bin/bash", True),
        ("#!/usr/bin/env python3", False),
        ("#!/usr/bin/python3.12 -u", False),
        ("#!/usr/bin/env -S uv run --script", False),
        ('#!/usr/bin/env -S "node --no-warnings"', True),
        ("#!/usr/bin/npx tsx", True),
        ("#!/usr/bin/env pwsh", True),
        # The option operand is a variable name, not the program env runs.
        ("#!/usr/bin/env -S -u python3 node", True),
        ("#!/usr/bin/env -S -u node python3", False),
        ("#!/usr/bin/env -unode python3", False),
        ("#!/usr/bin/env -i -C /tmp FOO=1 node", True),
        ("#!/usr/bin/env --unset node python3", False),
        # An attached split string is the command.
        ("#!/usr/bin/env -Sbash", True),
        ("#!/usr/bin/env --split-string=bash", True),
        ("#!/usr/bin/env -S'node --no-warnings'", True),
        ("#!/usr/bin/env -Spython3", False),
    ],
)
def test_scripts_without_a_source_extension_are_classified_by_shebang(po, shebang, flagged):
    body = (shebang + "\n" + "x\n" * 60).encode()
    for path in ("bin/tool", "scripts/run.command", "notes.txt", "bin/tg.ctl"):
        assert (po.classify(path, body) is not None) is flagged, path


def test_short_extensionless_node_script_still_fails(po):
    assert po.classify("tg", b"#!/usr/bin/env bun\nconsole.log(1)\n") is not None


@pytest.mark.parametrize(
    "glob, path, matches",
    [
        ("pi-extensions/**", "pi-extensions/permission-guard/policy.ts", True),
        ("ci/*/*.sh", "ci/leftover-grep/leftover-grep.sh", True),
        ("ci/*/*.sh", "ci/a/b/c.sh", False),
        ("lib/omp_hook_bridge/extension.ts", "lib/omp_hook_bridge/extension.ts", True),
        ("lib/omp_hook_bridge/extension.ts", "lib/omp_hook_bridge/other.ts", False),
        ("a/tests/**/check.js", "a/tests/check.js", True),
        ("a/tests/**/check.js", "a/tests/x/y/check.js", True),
        ("site/**", "site", False),
    ],
)
def test_allowlist_globs(po, glob, path, matches):
    assert po.glob_match(glob, path) is matches


@pytest.mark.parametrize(
    "repo, path, exempt",
    [
        ("rig-cli", "riglib/templates/rig-permissions-guard.ts", True),
        ("rig-cli", "riglib/other.ts", False),
        ("demo-cli", "gnome-shell/extensions/dock-tweaks@example.com/extension.js", True),
        ("demo-cli", "data/gnome-shell/extensions/dock-tweaks@example.com/lib/util.js", True),
        ("demo-cli", "gnome/dock-tweaks/extension.js", False),
        ("demo-cli", "deps/pkg@1.2/index.js", False),
        ("demo-cli", "oxlint.config.ts", True),
        ("rig-cli", "tools/oxlint/anti-slop/rules/no-reflect-get.ts", True),
        ("demo-cli", "sub/oxlint.config.ts", False),
        ("demo-cli", "tools/oxlint/mine.ts", False),
        ("agent-tools", "pi-extensions/permission-guard/index.ts", True),
        ("agent-tools", "lib/opencode_hook_bridge/plugin.js", True),
    ],
)
def test_host_forced_code_is_exempt(po, repo, path, exempt):
    policy = po.policy_for(f"https://git.hyperide.ai/ultrabricks/{repo}.git")
    assert po.allowed(policy, path) is exempt


GUARDIAN_ASSETS = (
    "lib/agenttools_chatgpt/dom.js",
    "lib/agenttools_chatgpt/live_dom.js",
    "lib/agenttools_chatgpt/monitor_dom.js",
    "lib/agenttools_chatgpt/conversation_root.js",
)


@pytest.mark.parametrize("path", GUARDIAN_ASSETS)
def test_guardian_browser_assets_are_allowed_in_agent_tools_by_exact_path(po, path):
    policy = po.policy_for("https://git.hyperide.ai/ultrabricks/agent-tools.git")
    assert po.allowed(policy, path) is True
    assert policy.mode == "block"


@pytest.mark.parametrize(
    "path",
    [
        "lib/agenttools_chatgpt/other.js",
        "lib/agenttools_chatgpt/sub/dom.js",
        "lib/agenttools_chatgpt/dom.ts",
        "lib/agenttools_chatgpt/service.ts",
        "lib/other_pkg/dom.js",
    ],
)
def test_other_js_near_the_guardian_assets_stays_blocked(po, path):
    policy = po.policy_for("https://git.hyperide.ai/ultrabricks/agent-tools.git")
    assert po.allowed(policy, path) is False


def test_guardian_asset_paths_are_not_allowed_in_other_repos(po):
    policy = po.policy_for("https://git.hyperide.ai/ultrabricks/demo-cli.git")
    assert po.allowed(policy, GUARDIAN_ASSETS[0]) is False


def test_staged_guardian_assets_pass_but_unrelated_js_in_the_package_is_refused(tmp_path):
    ok = _repo(tmp_path / "ok", AGENT_TOOLS_ORIGIN)
    for rel in GUARDIAN_ASSETS:
        _put(ok, rel, "export const x = 1;\n")
    proc = _run(ok)
    assert proc.returncode == 0, proc.stderr

    bad = _repo(tmp_path / "bad", AGENT_TOOLS_ORIGIN)
    _put(bad, GUARDIAN_ASSETS[0], "export const x = 1;\n")
    _put(bad, "lib/agenttools_chatgpt/extra.js", "export const y = 2;\n")
    proc = _run(bad)
    assert proc.returncode == 1
    assert "extra.js" in proc.stderr


def test_non_python_tool_code_in_the_guardian_package_still_blocks(tmp_path):
    repo = _repo(tmp_path, AGENT_TOOLS_ORIGIN)
    _put(repo, "lib/agenttools_chatgpt/cli.ts", "export const z = 3;\n")
    proc = _run(repo)
    assert proc.returncode == 1 and "cli.ts" in proc.stderr


# --- pre-commit (staged) end to end -------------------------------------------------------


def test_new_ts_in_python_only_repo_is_blocked_citing_the_decision(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "src/sample.ts", "export const x = 1;\n")
    proc = _run(repo)
    assert proc.returncode == 1, proc.stderr
    assert "src/sample.ts" in proc.stderr
    assert "2026-06-14" in proc.stderr
    assert "BLOCKED" in proc.stderr


def test_python_change_passes_silently(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "tool/cli.py", "print(1)\n")
    proc = _run(repo)
    assert proc.returncode == 0 and proc.stderr == ""


def test_allowlisted_host_forced_file_passes_in_agent_tools(tmp_path):
    repo = _repo(tmp_path, AGENT_TOOLS_ORIGIN)
    _put(repo, "lib/omp_hook_bridge/extension.ts", "export default function () {}\n")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr


def test_vendored_ci_shell_is_exempt_downstream_but_not_at_its_agent_tools_source(tmp_path):
    down = _repo(tmp_path / "d", PY_ONLY_ORIGIN)
    _put(down, "ci/leftover-grep/leftover-grep.sh", _shell(150))
    assert _run(down).returncode == 0

    src = _repo(tmp_path / "s", AGENT_TOOLS_ORIGIN)
    _put(src, "ci/leftover-grep/leftover-grep.sh", _shell(150))
    proc = _run(src)
    assert proc.returncode == 1
    assert "150 lines" in proc.stderr


def test_a_repo_cannot_hide_its_own_shell_tool_under_ci(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "ci/deploy/deploy.sh", _shell(100))
    assert _run(repo).returncode == 1


def test_vendored_list_matches_what_agent_tools_ships(po):
    """Every .sh/.mjs companion rig vendors from ci/<slot>/ (rig-cli runner._ci_companion_files)."""
    shipped = set()
    for slot in (REPO_ROOT / "ci").iterdir():
        if not slot.is_dir() or slot.name == "ship":  # ship installs as ~/bin/ship, not a companion
            continue
        for f in slot.iterdir():
            if f.suffix in (".sh", ".mjs") and not f.name.endswith(".test.mjs"):
                where = ".github/scripts" if f.name == "checklist-gate.mjs" else f"ci/{slot.name}"
                shipped.add(f"{where}/{f.name}")
    assert set(po.VENDORED_FILES) == shipped


def test_tg_cli_warns_but_allows_until_its_port_lands(tmp_path):
    repo = _repo(tmp_path, TG_ORIGIN)
    _put(repo, "features/new-thing.ts", "export {};\n")
    proc = _run(repo)
    assert proc.returncode == 0
    assert "WARNING" in proc.stderr
    assert "features/new-thing.ts" in proc.stderr
    assert "tg-cli/issues/768" in proc.stderr


def test_product_repo_is_out_of_scope(tmp_path):
    repo = _repo(tmp_path, PRODUCT_ORIGIN)
    _put(repo, "app/page.tsx", "export default 1;\n")
    proc = _run(repo)
    assert proc.returncode == 0 and proc.stderr == ""


def test_repo_without_origin_is_out_of_scope(tmp_path):
    repo = _repo(tmp_path, None)
    _put(repo, "x.ts", "1\n")
    assert _run(repo).returncode == 0


def test_deletion_and_moving_already_noncompliant_code_pass_but_editing_it_fails(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "old/legacy.ts", "export const legacy = 1;\n" * 5)
    _put(repo, "old/gone.ts", "export const gone = 1;\n")
    _git(repo, "commit", "-q", "-m", "legacy")

    (repo / "new").mkdir()
    _git(repo, "mv", "old/legacy.ts", "new/legacy.ts")
    _git(repo, "rm", "-q", "old/gone.ts")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr

    _put(repo, "new/legacy.ts", "export const legacy = 2;\n")
    assert _run(repo).returncode == 1


@pytest.mark.parametrize(
    "old, new, body",
    [
        ("run.sh", "run.ts", _shell(100)),
        ("bin/tg", "bin/tg.ts", "#!/usr/bin/env bun\nconsole.log(1);\n"),
    ],
)
def test_an_unchanged_move_of_already_noncompliant_code_passes_even_into_a_blocked_extension(tmp_path, old, new, body):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, old, body)
    _git(repo, "commit", "-q", "-m", "legacy")
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", old, new)
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr


def test_renaming_into_a_blocked_extension_fails(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "notes.txt", "export const sneaky = 1;\n" * 5)
    _git(repo, "commit", "-q", "-m", "notes")
    _git(repo, "mv", "notes.txt", "notes.ts")
    proc = _run(repo)
    assert proc.returncode == 1 and "notes.ts" in proc.stderr


def test_moving_exempt_code_out_of_its_exemption_fails(tmp_path):
    repo = _repo(tmp_path, AGENT_TOOLS_ORIGIN)
    _put(repo, "pi-extensions/guard/policy.ts", "export const policy = 1;\n" * 5)
    _git(repo, "commit", "-q", "-m", "pi")
    _git(repo, "mv", "pi-extensions/guard/policy.ts", "policy.ts")
    proc = _run(repo)
    assert proc.returncode == 1 and "policy.ts" in proc.stderr


def test_a_mode_only_change_to_legacy_shell_passes(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "legacy.sh", _shell(100))
    _git(repo, "commit", "-q", "-m", "legacy")
    _git(repo, "update-index", "--chmod=+x", "legacy.sh")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr


def test_scope_follows_insteadof_rewrites(tmp_path):
    repo = _repo(tmp_path, "hyp:ultrabricks/demo-cli")
    _git(repo, "config", "url.https://git.hyperide.ai/.insteadOf", "hyp:")
    _put(repo, "x.ts", "1\n")
    assert _run(repo).returncode == 1


def test_a_scope_detection_surprise_never_blocks_a_commit(po, tmp_path, monkeypatch):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "x.ts", "1\n")

    def boom(origin):
        raise AttributeError("simulated old-python failure")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(po, "policy_for", boom)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    assert po.main([]) == 0


def test_symlink_is_not_source(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    (repo / "link.ts").symlink_to("README.md")
    _git(repo, "add", "-A")
    assert _run(repo).returncode == 0


def test_symlink_turned_into_same_bytes_file_is_checked(tmp_path):
    # The symlink's target string and the new file's content hash to the same blob id, so git
    # reports a typechange with old blob == new blob; that is new code, not a mode-only change.
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    (repo / "tool.js").symlink_to("console.log(1)")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "link")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / "tool.js").unlink()
    (repo / "tool.js").write_text("console.log(1)")
    _git(repo, "add", "-A")
    proc = _run(repo)
    assert proc.returncode == 1 and "tool.js" in proc.stderr
    _git(repo, "commit", "-q", "--no-verify", "-m", "file")
    proc = _run(repo, "--base", "main", "--head", "HEAD")
    assert proc.returncode == 1 and "tool.js" in proc.stderr


def test_merge_resolution_only_checks_content_new_to_both_parents(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _put(repo, "conflict.txt", "base\n")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _put(repo, "conflict.txt", "feature\n")
    _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", "main")
    # main already carries a TS file (landed before the gate); merging it into feature must
    # not re-flag it, since the resolution commit does not introduce it.
    _put(repo, "conflict.txt", "main\n")
    _put(repo, "landed.ts", "export {};\n")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "checkout", "-q", "feature")
    subprocess.run(["git", "-C", str(repo), "merge", "main"], capture_output=True, env=_GIT_ENV)
    _put(repo, "conflict.txt", "resolved\n")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr

    _put(repo, "resolution.ts", "export {};\n")
    assert _run(repo).returncode == 1


# --- CI (range) mode ----------------------------------------------------------------------


def test_range_mode_checks_base_to_head(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    _git(repo, "checkout", "-q", "-b", "pr")
    _put(repo, "tool.py", "print(1)\n")
    _git(repo, "commit", "-q", "-m", "py")
    assert _run(repo, "--base", "main", "--head", "HEAD").returncode == 0
    _put(repo, "tool.mjs", "1\n")
    _git(repo, "commit", "-q", "-m", "mjs")
    proc = _run(repo, "--base", "main", "--head", "HEAD")
    assert proc.returncode == 1 and "tool.mjs" in proc.stderr


def test_origin_override_sets_scope_in_ci(tmp_path):
    repo = _repo(tmp_path, None)
    _git(repo, "checkout", "-q", "-b", "pr")
    _put(repo, "x.ts", "1\n")
    _git(repo, "commit", "-q", "-m", "ts")
    assert _run(repo, "--base", "main", "--head", "HEAD").returncode == 0
    assert _run(repo, "--base", "main", "--head", "HEAD", "--origin", PY_ONLY_ORIGIN).returncode == 1


def test_bad_ref_fails_closed(tmp_path):
    repo = _repo(tmp_path, PY_ONLY_ORIGIN)
    proc = _run(repo, "--base", "no-such-ref", "--head", "HEAD")
    assert proc.returncode == 2


# --- carriers -----------------------------------------------------------------------------


def test_fragment_is_a_byte_identical_twin_of_the_checker():
    # A regular file, not a symlink: a relative link would dangle once copied into
    # ~/.config/git/global-hooks.d by any symlink-preserving installer.
    assert not FRAGMENT.is_symlink()
    assert FRAGMENT.read_bytes() == CHECKER.read_bytes()
    assert os.access(FRAGMENT, os.X_OK) and os.access(CHECKER, os.X_OK)
    head = CHECKER.read_text().splitlines()[:12]
    assert head[0] == "#!/usr/bin/env python3"
    assert "# global-hook-id: python-only" in head
    assert "# global-hook-protected: true" in head


def test_installed_fragment_blocks_a_commit_through_the_dispatcher(tmp_path):
    """End to end: the fragment copied into a hooks dir, run by run-global-hooks."""
    hooks = tmp_path / "global-hooks.d" / "pre-commit"
    hooks.mkdir(parents=True)
    installed = hooks / FRAGMENT.name
    installed.write_bytes(FRAGMENT.read_bytes())
    installed.chmod(0o755)
    repo = _repo(tmp_path / "r", PY_ONLY_ORIGIN)
    _put(repo, "sample.ts", "export {};\n")
    runner = REPO_ROOT / "git-hooks" / "global-dispatcher" / "run-global-hooks"
    proc = subprocess.run(
        ["sh", str(runner), "pre-commit"], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        env={**_GIT_ENV, "GLOBAL_HOOKS_DIR": str(tmp_path / "global-hooks.d")},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "python-only: BLOCKED" in proc.stderr and "sample.ts" in proc.stderr


def test_workflow_runs_the_trusted_base_checker_on_the_pr_diff():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf[True] if True in wf else wf["on"]
    assert "pull_request_target" in on
    steps = {s.get("name", ""): s for s in wf["jobs"]["python-only"]["steps"]}
    checkout = steps["Checkout BASE branch (trusted checker) with full history"]
    assert checkout["uses"].startswith("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683")
    fetch = steps["Fetch the PR head commit as DATA (not checked out / not executed)"]["run"]
    assert "refs/pull/${PR_NUMBER}/head" in fetch, "fork PR heads are only reachable through the PR ref"
    run = "\n".join(str(s.get("run", "")) for s in steps.values())
    assert "python3 ci/python-only/python_only.py --base" in run
    assert "--origin" in run
    for forbidden in ("pip install", "npm ", "bun ", "uv run"):
        assert forbidden not in run, "no PR code may run under pull_request_target"


def test_installed_workflow_matches_the_slot():
    assert (REPO_ROOT / ".github" / "workflows" / "python-only.yml").read_bytes() == WORKFLOW.read_bytes()
