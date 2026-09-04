"""Tests for the block-devserver-primary agent-hook (pre-bash).

Covers the doctrine: launching a known dev-server command (npm run dev, vite, ...) is DENIED
while the effective cwd sits on the DEFAULT branch of an ENROLLED repo; a feature branch, an
un-enrolled repo, and a non-dev-server command are all ALLOWED; a literal leading `cd <dir> &&`
prefix is tracked so the command is judged against the TARGET directory, not the shell's own
cwd; the hatch escalation replaces the (nonexistent) self-service bypass; detached HEAD /
non-git fails OPEN.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_block_devserver_primary.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "block-devserver-primary"
    / "block_devserver_primary.py"
)
_spec = importlib.util.spec_from_file_location("block_devserver_primary", _HOOK)
assert _spec and _spec.loader
bdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdp)

_ENV_KEYS = ("RIG_WORKTREE_ONLY", "RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY")


def test_hook_file_is_executable_and_runs_standalone(tmp_path):
    """Regression (Codex review, P0): the agents-hooks/v1 runner (`lib/agent_hooks_v1/runner.py`
    `run_hook`) executes the descriptor's `cmd` DIRECTLY as `argv[0]` — `subprocess.run([cmd,
    ...])`, never `python3 cmd` — relying on the file's own shebang AND its executable bit. Every
    OTHER test in this file loads the module in-process via `importlib` (bypassing the OS exec
    path entirely) and so CANNOT catch a missing `+x` bit. A non-executable file raises `OSError:
    Permission denied` on launch, which this hook's own `on_error: "open"` descriptor policy
    resolves to a SILENT ALLOW — i.e. a missing `+x` bit turns this hook into a permanent no-op
    that blocks nothing, with no error visible anywhere except a debug-level warning. This test
    invokes the real file exactly the way the runner does (no python3 prefix) and asserts it
    actually runs and blocks."""
    assert os.access(_HOOK, os.X_OK), f"{_HOOK} is not executable — the runner execs it directly"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True, env=_GIT_ENV,
    )
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True, env=_GIT_ENV)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, env=_GIT_ENV)
    (repo / "seed.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True, env=_GIT_ENV)
    event = json.dumps({"cwd": str(repo), "args": {"command": "npm run dev"}})
    proc = subprocess.run(
        [str(_HOOK)],  # exactly argv[0] the way runner.py's `argv = [cmd, ...]` invokes it
        input=event,
        capture_output=True,
        text=True,
        env={**os.environ, "RIG_WORKTREE_ONLY": "1"},
        timeout=10,
    )
    assert proc.returncode == 10, (
        f"expected BLOCK_EXIT_CODE (10) from a direct exec, got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert json.loads(proc.stdout)["decision"] == "block"


def _fake_tg_ctl(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# Real `tg-ctl ask` speaks a stdin-JSON-in / stdout-JSON-out protocol; a fake standing in for an
# "approved" answer must reply with the real hookSpecificOutput shape the helper parses
# (`decision.behavior == "allow"`) — printing arbitrary text and exiting 0 no longer approves.
_ALLOW_REPLY_SH = (
    'printf \'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
    '"decision":{"behavior":"allow"}}}\'\nexit 0\n'
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    )


def _make_repo(tmp_path: Path, *, branch: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _run(cwd: Path, monkeypatch, command: str, env: dict | None = None) -> tuple[str, int]:
    event = {"cwd": str(cwd), "args": {"command": command}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = bdp.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── DENY: known dev-server commands on the default branch, enrolled ───────────────────────

@pytest.mark.parametrize("command", [
    "npm run dev",
    "npm start",
    "npm run preview",
    "yarn dev",
    "yarn run start",
    "pnpm dev",
    "pnpm run serve",
    "bun run dev",
    "bun start",
    "npx vite",
    "npx vite dev",
    "npx next dev",
    "bunx astro dev",
    "vite",
    "next dev",
    "astro dev",
    "webpack serve",
    "webpack-dev-server",
    "npm run dev:client",
    "bun run start:debug",
])
def test_devserver_commands_denied_on_default_branch(tmp_path, monkeypatch, command):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "worktree" in json.loads(out)["message"].lower()


# ── ALLOW: non-server subcommands of the same tools ────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "npm run build",
    "npm test",
    "npm install",
    "yarn build",
    "pnpm lint",
    "bun run build",
    "npx vite build",
    "next build",
    "next start",
    "astro build",
    "webpack build",
    "npm run devtools-build",  # not colon-namespaced — no false positive from the prefix match
])
def test_non_server_commands_allowed(tmp_path, monkeypatch, command):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: on a feature branch, exactly where dev servers belong ──────────────────────────

def test_feature_branch_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "-b", "feat/x")
    out, code = _run(repo, monkeypatch, "npm run dev", {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW: not enrolled ─────────────────────────────────────────────────────────────────

def test_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")  # no env, no rig.yaml → default OFF
    out, code = _run(repo, monkeypatch, "npm run dev")
    assert code == 0 and _decision(out) == "allow"


def test_rigyaml_enrollment_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    out, code = _run(repo, monkeypatch, "vite")
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_env_zero_overrides_rigyaml_true(tmp_path, monkeypatch):
    """RIG_WORKTREE_ONLY=0 force-off wins over a committed rig.yaml opt-in — matches the
    sibling hooks' precedence (env > rig.yaml > default off)."""
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    out, code = _run(repo, monkeypatch, "npm run dev", {"RIG_WORKTREE_ONLY": "0"})
    assert code == 0 and _decision(out) == "allow"


# ── `cd <dir> &&` prefix tracking — the exact incident shape ───────────────────────────────

def test_cd_prefix_into_shared_checkout_denies(tmp_path, monkeypatch):
    """The agent's shell cwd is a feature worktree, but the command `cd`s INTO the shared
    default-branch checkout before launching the dev server — must still be denied."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    command = f"cd {shared} && npm run dev"
    out, code = _run(feat, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_cd_prefix_into_feature_checkout_allows(tmp_path, monkeypatch):
    """The inverse: cwd is the shared main checkout, but the command `cd`s into a feature
    checkout first — the dev server runs where authoring belongs, must be allowed.

    The feature repo is seeded with a real commit (mirrors _make_repo) so `current_branch`
    resolves to the real `feat/x` branch and this exercises the ACTUAL branch != default-branch
    comparison — an unborn-HEAD repo would instead take the "branch undetermined" fail-open
    path and never touch that comparison at all, silently passing even if it were broken."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    _git(feat, "config", "user.email", "t@t.t")
    _git(feat, "config", "user.name", "t")
    (feat / "seed.txt").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "seed")
    assert bdp.current_branch(str(feat)) == "feat/x"  # sanity: NOT the fail-open path
    command = f"cd {feat} && npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_cd_prefix_via_semicolon_denies(tmp_path, monkeypatch):
    """`_split_chain` treats `;` the same as `&&` — the `cd` tracking must not be `&&`-specific."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    command = f"cd {shared}; npm run dev"
    out, code = _run(feat, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_failed_cd_does_not_move_effective_cwd(tmp_path, monkeypatch):
    """Regression (Codex review): in a REAL shell, `cd /does-not-exist; npm run dev` leaves the
    shell in its ORIGINAL directory when `cd` fails — the `;` still runs `npm run dev` there. A
    version of `_resolve_cd_target` that unconditionally "succeeds" would move `effective_cwd`
    to the nonexistent path, `_resolve_gate` would find no git repo there, fail open, and ALLOW
    a launch that actually starts in the still-protected shared checkout. Must still BLOCK."""
    shared = _make_repo(tmp_path, branch="main")
    command = "cd /this/path/does/not/exist; npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_failed_cd_via_double_pipe_does_not_move_effective_cwd(tmp_path, monkeypatch):
    """Same failure mode via `||` — real shell semantics run the right-hand side ONLY when the
    left-hand side fails, so a failed `cd` here also leaves the launch in the original dir."""
    shared = _make_repo(tmp_path, branch="main")
    command = "cd /this/path/does/not/exist || npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_successful_cd_to_real_directory_still_tracked(tmp_path, monkeypatch):
    """Sanity: the disk-existence check must not break the legitimate, already-covered case — a
    `cd` to a directory that DOES exist is still tracked normally. `feat` is seeded with a real
    commit (not just `mkdir`) so this exercises the ACTUAL branch != default-branch comparison,
    not the separate "branch undetermined" fail-open path (same fail-open-masking pitfall as
    `test_cd_prefix_into_feature_checkout_allows`)."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    _git(feat, "config", "user.email", "t@t.t")
    _git(feat, "config", "user.name", "t")
    (feat / "seed.txt").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "seed")
    command = f"cd {feat}; npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_cd_tilde_into_shared_checkout_denies(monkeypatch, tmp_path):
    """`cd ~/...` (the idiomatic home-relative spelling) must be tilde-expanded, not treated as
    a literal `~` path segment that fails to resolve to the real shared checkout."""
    repo = _make_repo(tmp_path, branch="main")
    monkeypatch.setenv("HOME", str(tmp_path))
    rel = repo.relative_to(tmp_path)
    command = f"cd ~/{rel} && npm run dev"
    out, code = _run(tmp_path, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_backslash_newline_continuation_denies(tmp_path, monkeypatch):
    """A `\\`-continued command (`npm run dev \\` + newline + `--host 3000`) is one logical
    command — must still be recognized, not silently split into two unclassifiable segments."""
    repo = _make_repo(tmp_path, branch="main")
    command = "npm run dev \\\n  --host 3000"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_npx_next_start_allowed_same_as_direct(tmp_path, monkeypatch):
    """`npx next start` and the direct `next start` must agree (both serve an already-built
    production bundle, neither is a source-writing dev watcher) — regression for an npx/bunx
    branch that used to over-match by ignoring the direct-binary dev-only rule."""
    repo = _make_repo(tmp_path, branch="main")
    for command in ("npx next start", "next start"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == 0 and _decision(out) == "allow", command


def test_npx_next_dev_denied_same_as_direct(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    for command in ("npx next dev", "next dev"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_npx_webpack_denied_same_as_direct(tmp_path, monkeypatch):
    """`npx webpack serve` is webpack-dev-server's own documented canonical invocation — must
    agree with the direct `webpack serve` / `webpack-dev-server` forms."""
    repo = _make_repo(tmp_path, branch="main")
    for command in ("npx webpack serve", "webpack serve", "npx webpack-dev-server", "webpack-dev-server"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_npx_webpack_build_allowed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    for command in ("npx webpack build", "webpack build", "npx webpack"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == 0 and _decision(out) == "allow", command


# ── info flags never start a dev server (`vite --version`, `--help`) ───────────────────────

@pytest.mark.parametrize("command", [
    "vite --version",
    "vite --help",
    "vite -v",
    "npx vite --version",
    "webpack-dev-server --help",
])
def test_info_flags_allowed(tmp_path, monkeypatch, command):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow", command


# ── directory-flag forms (`--prefix`/`-C`/`--dir`/`--cwd`) reproduce the incident shape ────

@pytest.mark.parametrize("flag", ["--prefix", "-C", "--dir", "--cwd"])
def test_pm_directory_flag_targets_shared_checkout_denies(tmp_path, monkeypatch, flag):
    """`npm --prefix <shared-checkout> run dev` (etc.) sets the dev server's cwd WITHOUT a `cd`
    segment for the tracker to see — this is the exact incident shape via a flag instead of
    `cd`. Command runs from a feature worktree; the flag alone must be enough to catch it."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    command = f"npm {flag} {shared} run dev"
    out, code = _run(feat, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pm_directory_flag_equals_form_denies(tmp_path, monkeypatch):
    """Regression (Codex review, P1): `npm --prefix=<dir> run dev` (the `=`-joined form) really
    does make npm operate against `<dir>` — a version of `_peel_dir_flag` that only recognized
    the split `--prefix <dir>` form would leave `cwd_override` unset here, fall back to judging
    the harmless cwd the command was RUN from, and wrongly ALLOW a launch that actually targets
    the shared checkout. Command runs from a feature worktree; only the `=`-joined flag value
    (never seen as an actual `cd`) tells the hook where the launch really targets."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    command = f"npm --prefix={shared} run dev"
    out, code = _run(feat, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("command", [
    "nohup npm run dev",
    "nohup npm run dev &",
    "env npm run dev",
    "env NODE_ENV=development npm run dev",
    "sudo npm run dev",
    "time npm run dev",
    "nice npm run dev",
    "setsid npm run dev",
    "command npm run dev",
    "exec npm run dev",
])
def test_bare_command_wrappers_denied(tmp_path, monkeypatch, command):
    """Regression (Codex/Fable/GLM review, escalated to P1 across three independent passes):
    `nohup npm run dev &` — start a dev server and detach it — is arguably the single MOST
    realistic real-world shape of this hook's own incident (a quick verification pass that
    outlives the current Bash call). A hook that only inspects the segment's literal first
    token misses every wrapped form entirely."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_flagged_command_wrappers_not_peeled(tmp_path, monkeypatch):
    """A wrapper followed by what looks like ITS OWN flag (`sudo -u dev npm run dev`, `nice
    -n10 npm run dev`) is deliberately left UNPEELED — a documented, conservative miss (safe
    direction: worse coverage, never a false allow from a mis-parsed peel) rather than guessing
    at every wrapper's own flag grammar. Tracked as agent-tools#463 for the full flag-aware
    peel."""
    repo = _make_repo(tmp_path, branch="main")
    for command in ("sudo -u dev npm run dev", "nice -n10 npm run dev"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == 0 and _decision(out) == "allow", command


def test_pm_directory_flag_override_is_one_shot_not_persisted(tmp_path, monkeypatch):
    """Unlike a real `cd`, a directory flag scopes ONLY the command it's attached to. cwd is the
    SHARED default-branch checkout; segment 1 uses `--prefix` to (harmlessly) target the
    feature worktree instead — allowed, since that's exactly where a dev server belongs.
    Segment 2 has no flag at all, so it must be judged against the REAL `effective_cwd`
    (`shared`, unchanged) and BLOCKED — if the flag's target wrongly persisted the way a `cd`
    does, segment 2 would incorrectly inherit `feat` and be allowed instead."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    _git(feat, "config", "user.email", "t@t.t")
    _git(feat, "config", "user.name", "t")
    (feat / "seed.txt").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "seed")
    command = f"npm --prefix {feat} run dev; npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pm_exec_dlx_denies_like_npx(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    for command in ("npm exec vite", "pnpm dlx vite", "yarn dlx vite"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_pm_dir_flag_after_double_dash_is_forwarded_not_npms_own(tmp_path, monkeypatch):
    """Regression (Codex review): `npm run dev -- --prefix <dir>` forwards `--prefix <dir>` to
    the DEV SCRIPT as an argument — npm's own cwd is untouched, the server still starts in the
    checkout the command actually runs in. A `_peel_dir_flag` that scans the whole argv (not
    just the leading global-options region before `run`/`--`) would misread that FORWARDED flag
    as npm's own `--prefix`, wrongly compute a cwd override pointing at the harmless value, and
    ALLOW a launch that really runs in the protected checkout. Must still BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    harmless = tmp_path / "unrelated-dir"
    harmless.mkdir()
    command = f"npm run dev -- --prefix {harmless}"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pm_dir_flag_only_recognized_before_run(tmp_path, monkeypatch):
    """A directory flag appearing AFTER the script name but with no `--` separator (an unusual
    but not impossible shape) is likewise not npm's own global option — only a LEADING flag
    (before `run`/the script name) is trusted as a real cwd override."""
    repo = _make_repo(tmp_path, branch="main")
    harmless = tmp_path / "unrelated-dir"
    harmless.mkdir()
    command = f"npm run dev --prefix {harmless}"  # no `--`, flag trails the script name
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("command", [
    "pnpm --filter web dev",
    "npm -w client run dev",
    "npm run --if-present dev",
    "npm run -s dev",
])
def test_pm_value_flags_dont_defeat_script_detection(tmp_path, monkeypatch, command):
    """Regression (Fable/GLM review): a KNOWN value-taking flag (`--filter`/`-w`) or a valueless
    flag positioned between `run` and the script name (`--if-present`) must not be mistaken for
    the script-name position itself — that would silently miss the launch entirely (a real
    under-block, not the safe over-block direction)."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_pm_exec_double_dash_form_denies(tmp_path, monkeypatch):
    """`npm exec -- vite` / `pnpm dlx -- vite` — npm's own documented canonical `--` form for
    `exec`/`dlx` — must agree with the bare `npm exec vite` form."""
    repo = _make_repo(tmp_path, branch="main")
    for command in ("npm exec -- vite", "pnpm dlx -- vite"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


def test_bun_x_denies_like_bunx(tmp_path, monkeypatch):
    """`bun x <tool>` is bun's own documented alias for `bunx <tool>`."""
    repo = _make_repo(tmp_path, branch="main")
    for command in ("bun x vite", "bunx vite"):
        out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
        assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


# ── performance: `_resolve_gate` memoizes per cwd, not per matching segment ────────────────

def test_resolve_gate_memoizes_per_cwd(tmp_path, monkeypatch):
    """Regression pin for the memoization `_resolve_gate` claims: a chain with TWO
    dev-server-shaped segments targeting the SAME directory must resolve the
    enrollment/branch git queries only ONCE, not once per matching segment.

    MUST run on a FEATURE branch (allowed), not the default branch: `_decide_block` returns on
    the FIRST gate-worthy segment, so a BLOCKING scenario only ever reaches `_resolve_gate`
    once regardless of caching — that shape makes `len(calls) == 1` pass vacuously even with
    `gate_cache` deleted entirely (a real bug in an earlier version of this test, caught by
    independent review). On a feature branch nothing short-circuits, so the loop actually
    reaches `_resolve_gate` for BOTH matching segments — only the cache prevents a second
    `current_branch()` call for the second one."""
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "-b", "feat/x")
    calls = []
    real_current_branch = bdp.current_branch

    def counting_current_branch(cwd):
        calls.append(cwd)
        return real_current_branch(cwd)

    monkeypatch.setattr(bdp, "current_branch", counting_current_branch)
    command = "npm run dev; npm start"  # two matching segments, same (untouched) cwd
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"
    assert len(calls) == 1, f"expected exactly one current_branch() resolution, got {calls}"


# ── unrelated commands are never touched ───────────────────────────────────────────────────

def test_unrelated_command_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, "ls -la", {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_git_commit_allows(tmp_path, monkeypatch):
    """git commit is explicitly OUT OF SCOPE for this hook (see README/docstring) — never
    touched here, regardless of branch."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, 'git commit -m "x"', {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── the RIG_HATCH_REQUEST_* Telegram escalation ─────────────────────────────────────────

def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n")  # would ALLOW if ever called
    monkeypatch.setattr(bdp.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, "npm run dev", {
        "RIG_WORKTREE_ONLY": "1", "RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY": "1",
    })
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    monkeypatch.setattr(bdp.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, "npm run dev", {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY": "One-off verification, worktree busy.",
    })
    assert code == 0 and _decision(out) == "allow"
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_question_shows_full_command_not_just_label(tmp_path, monkeypatch):
    """Regression: the Telegram QUESTION itself (not just the inline-parsing kwarg) must carry
    the full original command — `_decide_block` returns on the first gate-worthy segment of a
    chain, and that's only safe if the human approving is shown every segment, not just the one
    that happened to trigger the block. A prior version put only the short classifier label
    (e.g. "npm run dev") in the hatch `context` dict, so a chained command with a SECOND,
    unclassified dev-server launch later in the same string would get silently waved through
    on an approval that only ever displayed the first, innocuous-looking launch."""
    capture = tmp_path / "captured-argv"
    # `tg-ctl ask` reads the ButtonRequest JSON (question included) on stdin — the fake
    # captures that stream; there is no positional question argument.
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl", f'cat > "{capture}"\n' + _ALLOW_REPLY_SH
    )
    monkeypatch.setattr(bdp.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    command = "npm run dev; cd /some/other/shared-checkout && vite"
    out, code = _run(repo, monkeypatch, command, {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY": "One-off verification, worktree busy.",
    })
    assert code == 0 and _decision(out) == "allow"
    question = capture.read_text()
    assert command in question, (
        "the human-facing hatch question must contain the FULL original command string, "
        "not just the short classifier label for the segment that triggered the block"
    )


def test_hatch_justification_exit1_blocks_citing_denial(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(bdp.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, "npm run dev", {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY": "One-off verification, worktree busy.",
    })
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


def test_inline_hatch_form_reaches_full_command_not_label(tmp_path, monkeypatch):
    """Regression: `_decide_block` must pass the FULL original command string to
    `request_hatch_approval`, not the short classifier label — the documented INLINE hatch
    form embeds the justification directly on the gated command line
    (`RIG_HATCH_REQUEST_X="..." npm run dev`), which a label like "npm run dev" can never
    contain, so passing the label would make the inline form permanently unusable."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    monkeypatch.setattr(bdp.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    command = 'RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY="One-off, worktree busy." npm run dev'
    # Deliberately NOT exported via env — present ONLY inline on the command string itself.
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"
    assert "hatch escalation" in json.loads(out)["message"].lower()


# ── fail-OPEN: detached HEAD / not a git repo / bad event ──────────────────────────────────

def test_detached_head_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "--detach", "HEAD")
    out, code = _run(repo, monkeypatch, "npm run dev", {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_non_git_dir_allows(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    out, code = _run(plain, monkeypatch, "npm run dev", {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_bad_event_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    assert bdp.main() == 0 and _decision(out.getvalue()) == "allow"


def test_empty_command_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, "", {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── Codex review round (PR agent-tools#469): conditional-`cd` + parser hardening ───────────

def test_conditional_cd_via_and_and_not_trusted(tmp_path, monkeypatch):
    """Regression (Codex P1): `false && cd /feature; npm run dev` from a protected default-branch
    checkout. The `;` puts `npm run dev` OUTSIDE the `&&` group — in a REAL shell it always runs,
    regardless of whether `false` (and therefore `cd`) actually executed, and since `false`
    always fails, `cd` never runs there — the dev server launches in the STILL-PROTECTED original
    checkout. A version that unconditionally trusts a reached `cd` (an earlier version of this
    hook did) moves `effective_cwd` to `/feature` anyway, and wrongly ALLOWS. Must BLOCK."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f"false && cd {feat}; npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_conditional_cd_via_or_not_trusted(tmp_path, monkeypatch):
    """Same failure mode, mirrored through `||`: `true || cd /feature; npm run dev`. `||`'s
    right-hand side runs ONLY when the left-hand side FAILS — `true` always succeeds, so `cd`
    never runs, and `npm run dev` (unconditional via `;`) launches in the original, still-
    protected checkout. Must BLOCK, not fall through to the untouched `/feature` reading."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f"true || cd {feat}; npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_unconditional_cd_as_first_segment_still_trusted(tmp_path, monkeypatch):
    """The conditional-`cd` fix must not regress the ordinary, already-covered case: a `cd` that
    IS the first segment of the whole command has no preceding operator at all (unconditionally
    reached, whatever operator follows it) — still trusted normally."""
    shared = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    _git(feat, "config", "user.email", "t@t.t")
    _git(feat, "config", "user.name", "t")
    (feat / "seed.txt").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "seed")
    command = f"cd {feat} && npm run dev"
    out, code = _run(shared, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_escaped_quote_inside_double_quotes_doesnt_split_chain(tmp_path, monkeypatch):
    """Regression (Codex P2): `printf '%s' "a\\"b" && npm run dev` — the backslash-escaped `"`
    inside the double-quoted region must NOT be read as the end of the string. A quote-scanner
    naive about escaping closes the string one character early, leaving `&&` inside the
    remaining unquoted tail treated as a real chain-operator split — which can hide the
    `npm run dev` member of the chain from classification entirely. Must still BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = 'printf \'%s\' "a\\"b" && npm run dev'
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pm_run_consumes_multiple_leading_run_flags(tmp_path, monkeypatch):
    """Regression (Codex P2): `npm run --silent --if-present dev` — TWO valid `_RUN_FLAGS`
    stacked before the script name. Stopping after the first (`--silent`) reads `--if-present` as
    the script-name position and misses `dev` entirely — an under-block. Must BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = "npm run --silent --if-present dev"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("command", [
    "npm exec -- webpack serve",
    "pnpm dlx webpack-dev-server",
    "npm exec webpack serve",
])
def test_pm_exec_dlx_classifies_webpack_like_npx(tmp_path, monkeypatch, command):
    """Regression (Codex P2): the package-manager `exec`/`dlx` form of launching a tool (already
    covered for `_RUNNER_TOOLS`, i.e. `npm exec vite`) must agree with `npx`/`bunx`, which
    already recognizes webpack too. Before this fix, `npx webpack serve` blocked but the
    equivalent `npm exec -- webpack serve` did not — same watcher, inconsistent coverage."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block", command


# ── Codex review round 2 (PR agent-tools#469): both cd-branches + isolation + more parsers ──

def test_conditional_cd_toward_protected_checkout_also_blocked(tmp_path, monkeypatch):
    """Regression (Codex P1, round 2): the MIRROR of the round-1 fix. `true && cd
    /path/to/enrolled-main; npm run dev` from a FEATURE worktree. `true` always succeeds, so
    `cd` DOES run, and `npm run dev` (unconditional via `;`) really launches in the now-current,
    protected checkout. A fix that assumes a conditional `cd` "never happened" (the round-1 fix,
    taken alone) gets this direction wrong — it would leave `effective_cwd` at the harmless
    feature worktree and ALLOW. Must BLOCK: tracking BOTH possible outcomes of an unevaluable
    `&&`/`||` is required, not just one fixed assumption."""
    protected = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f"true && cd {protected}; npm run dev"
    out, code = _run(feat, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_piped_cd_does_not_persist(tmp_path, monkeypatch):
    """Regression (Codex P1, round 2): `cd /feature | cat; npm run dev` from a protected
    checkout. A `cd` that is itself piped into another command runs in a subshell bash forks
    for the pipeline stage — its directory change is discarded when that subshell exits and
    never reaches the parent shell. `npm run dev` (unconditional via `;`) really launches in the
    still-protected original checkout. Must BLOCK, not fall through to the harmless `/feature`
    reading."""
    protected = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f"cd {feat} | cat; npm run dev"
    out, code = _run(protected, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_backgrounded_cd_does_not_persist(tmp_path, monkeypatch):
    """Regression (Codex P1, round 2): `cd /feature & npm run dev` from a protected checkout.
    Backgrounding a `cd` forks a subshell to run it asynchronously — the parent shell continues
    immediately with `npm run dev`, which still runs in the ORIGINAL, still-protected checkout
    (the backgrounded `cd`'s effect never reaches it). Must BLOCK."""
    protected = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f"cd {feat} & npm run dev"
    out, code = _run(protected, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_escaped_quote_opening_outside_quotes_doesnt_hide_launch(tmp_path, monkeypatch):
    """Regression (Codex P2, round 2): `echo \\" && npm run dev` — the backslash-escaped `"` is a
    literal character in a real shell, not the start of a new quoted region. Unconditionally
    opening a quote there swallows the real `&&` that follows into an "unterminated string",
    hiding the `npm run dev` member from ever being split out and classified. Must BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = 'echo \\" && npm run dev'
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_bun_run_consumes_leading_run_flags(tmp_path, monkeypatch):
    """Regression (Codex P2, round 2): `bun run --silent dev` — bun's own `run` documents
    `--silent`/`--if-present` too. Stopping before consuming the flag reads it as the
    script-name position and misses `dev` entirely. Must BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = "bun run --silent dev"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pnpm_dlx_consumes_flags_before_tool(tmp_path, monkeypatch):
    """Regression (Codex P2, round 2): `pnpm dlx --silent vite` — `--silent` is a documented
    `dlx` option. Reading it as the tool name itself misses `vite` entirely. Must BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = "pnpm dlx --silent vite"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── Codex review round 3 (PR agent-tools#469): comments, escaped ops, cap overflow, run flags ──

def test_shell_comment_hides_operators_and_cd(tmp_path, monkeypatch):
    """Regression (Codex P1, round 3): `: # comment ; cd /feature` + newline + `npm run dev` from
    a protected checkout. Bash discards everything from an unquoted `#` (at the start of a word)
    to the next real newline as a comment — the `;` and `cd /feature` inside it are just comment
    TEXT, `cd` never runs. The real newline then separates that whole (no-op) first command from
    the unconditional `npm run dev`, which launches in the ORIGINAL, still-protected checkout.
    Treating the commented `;` as a real chain operator wrongly tracks a `cd` that never executes
    and would ALLOW. Must BLOCK."""
    protected = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f": # comment ; cd {feat}\nnpm run dev"
    out, code = _run(protected, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_escaped_semicolon_does_not_split_and_hide_real_cd(tmp_path, monkeypatch):
    """Regression (Codex P1, round 3): `echo \\; cd /feature; npm run dev` from a protected
    checkout. `\\;` is a LITERAL semicolon passed as an argument to `echo`, not a real chain
    operator — the whole `echo \\; cd /feature` is ONE command (printing text), `cd` never
    actually runs. Only the trailing UNESCAPED `;` really separates it from `npm run dev`, which
    launches in the ORIGINAL, still-protected checkout. Splitting on the escaped `;` anyway makes
    `cd /feature` look like its own unconditionally-reached segment and would wrongly ALLOW.
    Must BLOCK."""
    protected = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    command = f"echo \\; cd {feat}; npm run dev"
    out, code = _run(protected, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_candidate_cap_never_drops_a_gated_target(tmp_path, monkeypatch):
    """Regression (Codex P2, round 3): from a feature worktree, 8 harmless conditional `cd`
    branches (filling the candidate cap) followed by a 9th conditional `cd` INTO the protected
    checkout, then an unconditional `npm run dev`. The original cap implementation dropped any
    target once the cap was full purely by arrival order — including a target that is itself
    gated. A dropped candidate must never be one that would cause a BLOCK; the 9th, gated target
    must still be tracked even though the cap was already full. Must BLOCK."""
    feat = tmp_path / "feat-worktree"
    feat.mkdir()
    protected = _make_repo(tmp_path, branch="main")
    harmless_branches = " && ".join(f"cd /tmp/does-not-exist-{i}" for i in range(8))
    command = f"{harmless_branches}; true && cd {protected}; npm run dev"
    out, code = _run(feat, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pm_run_workspace_flag_consumed_before_script(tmp_path, monkeypatch):
    """Regression (Codex P2, round 3): `npm run --workspace web dev` — `--workspace`/`-w` is a
    documented VALUE-TAKING `npm run` option (npm 11.4.2 `--help`). Stopping at `web` (not
    `-`-prefixed) without consuming it as the flag's value misreads it as the script-name
    position and misses `dev` entirely. Must BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = "npm run --workspace web dev"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_even_backslash_before_newline_is_not_a_continuation(tmp_path, monkeypatch):
    """Regression (Codex P2, round 3): two backslashes immediately before a newline collapse to
    ONE literal backslash in a real shell — the newline that follows is a REAL separator, not
    consumed by a continuation. `echo \\\\` + newline + `npm run dev` is `echo \\` (printing a
    literal backslash) as its own command, then `npm run dev` as a genuinely SEPARATE launch on
    the next line. Folding this into one `echo`-headed segment (parity-blind continuation
    handling) hides the real launch. Must BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    command = "echo \\\\\nnpm run dev"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_odd_backslash_before_newline_still_folds_as_continuation(tmp_path, monkeypatch):
    """The parity fix must not regress the ordinary, already-covered case: ONE backslash before a
    newline (odd run) is a genuine line continuation and must still fold into a single segment,
    same as `test_backslash_newline_continuation_denies` already covers."""
    repo = _make_repo(tmp_path, branch="main")
    command = "npm run dev \\\n--host 3000"
    out, code = _run(repo, monkeypatch, command, {"RIG_WORKTREE_ONLY": "1"})
    assert code == bdp.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── unit: segment classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("segment,expected_matched", [
    ("npm run dev", True),
    ("npm run dev -- --host", True),
    ("VAR=1 npm run dev", True),
    ("npm run build", False),
    ("npm ci", False),
    ("npm run dev:watch", True),  # colon-namespaced variant of a known dev script — matched
    ("npm run devtools-build", False),  # NOT colon-namespaced — no false positive from prefix match
    ("vite build", False),
    ("vite --port 3000", True),
    ("next start", False),
    ("astro preview", False),
    ("git checkout main", False),
    ("echo hello", False),
])
def test_classify_devserver_segment(segment, expected_matched):
    """_classify_devserver_segment takes ALREADY-TOKENIZED, assignment-stripped tokens (main()
    tokenizes each segment once and reuses it for both the `cd` check and classification)."""
    toks = bdp._strip_leading_assignments(bdp.shlex.split(segment))
    result = bdp._classify_devserver_segment(toks)
    assert (result is not None) == expected_matched


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
