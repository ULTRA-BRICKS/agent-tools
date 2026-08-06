"""Regression tests for three real bugs in the shipped ci/<slot>/ gate workflows (#130).

These slots are what rig provisions into consumer/bot repos, so a wrong fix ships to every
repo. Each bug here made a provisioned gate either fail-CLOSED (red CI on a clean repo) or —
worse — silently fail-OPEN (green CI while the gate scanned nothing). The tests exercise the
real shipped scripts / the real workflow YAML and assert the verdict is honest.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_ci_gate_workflows.py -q

The three bugs:

1. ``ci/dependency-review/`` — under the plain ``pull_request`` trigger, a PR's own copy of
   the workflow + ``dep-audit.sh`` ran as its OWN gate, so a PR could weaken the gate gating
   it. AND ``setup-bun`` was commented out, so a ``bun.lock`` repo fail-CLOSED (no ``bun`` →
   the script's fail-closed branch reds CI). Fix: ``pull_request_target`` running the trusted
   BASE copy of the script against the PR's deps-as-data, with ``setup-bun`` installed.
2. ``ci/leftover-grep/`` — the PR-head was fetched ``--depth=1``; that grafts it as a shallow
   boundary so ``git merge-base`` fails, the three-dot ``base...head`` diff collapses to
   nothing, and the scan no-ops (fail-OPEN). Fix: full fetch so the merge-base resolves.
3. ``ci/codeql/workflow-selfgate.yml`` — the language-detect step piped ``git ls-files`` into
   ``grep -q`` under ``set -o pipefail``; ``grep -q`` exits on the first match and SIGPIPEs
   ``git ls-files`` (exit 141), which pipefail turns into the pipeline status, so the ``if``
   falsely takes the "no source" branch and CodeQL self-disables on a large repo (fail-OPEN).
   Fix: ``grep -c`` consumes all of stdin (no early exit → no SIGPIPE).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI = REPO_ROOT / "ci"
DEP_WF = CI / "dependency-review" / "workflow.yml"
DEP_SCRIPT = CI / "dependency-review" / "dep-audit.sh"
LEFTOVER_WF = CI / "leftover-grep" / "workflow.yml"
LEFTOVER_SCRIPT = CI / "leftover-grep" / "leftover-grep.sh"
CODEQL_SELFGATE = CI / "codeql" / "workflow-selfgate.yml"

_HAVE_GIT = shutil.which("git") is not None
_HAVE_BASH = shutil.which("bash") is not None
requires_git_bash = pytest.mark.skipif(
    not (_HAVE_GIT and _HAVE_BASH), reason="git + bash required"
)


# ── shared helpers ──────────────────────────────────────────────────────────────────────────
def _clean_git_env() -> dict[str, str]:
    """A git env immune to the dev box's global config/hooks: no global/system config (so
    init.templateDir can't copy a host pre-commit hook into the test repo) and REVIEW_SKIP for
    belt-and-suspenders. Mirrors the isolation in test_ship.py."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["REVIEW_SKIP"] = "1"
    return env


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", "-c", "core.hooksPath=", *args],
        cwd=cwd,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


def _extract_run_block(yaml_text: str, marker: str) -> str:
    """Pull the multi-line ``run: |`` script body of the step whose block contains *marker*
    (a stable handle such as ``id: detect``). Raw-text/indentation parse so the test needs no
    YAML dependency in CI."""
    lines = yaml_text.splitlines()
    i = next(k for k, ln in enumerate(lines) if marker in ln)
    j = next(k for k in range(i, len(lines)) if lines[k].strip().startswith("run:"))
    run_indent = len(lines[j]) - len(lines[j].lstrip())
    body: list[str] = []
    for k in range(j + 1, len(lines)):
        ln = lines[k]
        if ln.strip() == "":
            body.append("")
            continue
        if (len(ln) - len(ln.lstrip())) <= run_indent:
            break
        body.append(ln)
    base = min((len(b) - len(b.lstrip()) for b in body if b.strip()), default=0)
    return "\n".join(b[base:] if b.strip() else "" for b in body)


def _uses_lines(yaml_text: str) -> list[str]:
    return [ln.strip() for ln in yaml_text.splitlines() if ln.strip().startswith("uses:")]


def _extract_step_block(yaml_text: str, name_marker: str) -> str:
    """The full text of the step whose ``name:`` line contains *name_marker*, up to (not
    including) the next step at the same indentation."""
    lines = yaml_text.splitlines()
    i = next(k for k, ln in enumerate(lines) if "name:" in ln and name_marker in ln)
    start = i
    while start > 0 and not lines[start].lstrip().startswith("- "):
        start -= 1
    step_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for k in range(start + 1, len(lines)):
        ln = lines[k]
        if ln.strip() == "":
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent <= step_indent and ln.lstrip().startswith("- "):
            end = k
            break
    return "\n".join(lines[start:end])


# ══════════════════════════════════════════════════════════════════════════════════════════
# Bug 3 — CodeQL self-gate: SIGPIPE-under-pipefail self-disable
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_codeql_detect_drops_grep_q_antipattern():
    """Static guard: the detect step must NOT pipe ``git ls-files`` into an early-exiting
    ``grep -q`` (the SIGPIPE-under-pipefail self-disable). It must full-consume via ``grep -c``."""
    blk = _extract_run_block(CODEQL_SELFGATE.read_text(), "id: detect")
    assert "git ls-files | grep -qiE" not in blk, "early-exit grep -q on git ls-files re-introduced"
    assert "grep -ciE" in blk, "detect must count with grep -c (full-consume, no SIGPIPE)"


def test_pipefail_sigpipe_mechanism_old_breaks_new_holds():
    """The bug MECHANISM, deterministically: a large producer + ``grep -q`` under pipefail
    self-disables (rc 141 → 'else'); the ``grep -c`` fix holds. Uses ``seq`` (not git) so
    SIGPIPE is guaranteed, proving the fix targets the real failure mode."""
    pattern = "^3$"
    old = (
        "set -uo pipefail\n"
        f'if seq 1 500000 | grep -qE "{pattern}"; then echo true; else echo false; fi'
    )
    new = (
        "set -uo pipefail\n"
        f'c="$(seq 1 500000 | grep -cE "{pattern}" || true)"\n'
        'if [ "${c:-0}" -gt 0 ]; then echo true; else echo false; fi'
    )
    old_out = subprocess.run(["bash", "-c", old], capture_output=True, text=True).stdout.strip()
    new_out = subprocess.run(["bash", "-c", new], capture_output=True, text=True).stdout.strip()
    assert old_out == "false", "expected the buggy grep -q form to self-disable (it didn't repro)"
    assert new_out == "true", "the grep -c fix must report source present"


@requires_git_bash
def test_codeql_detect_finds_source_in_a_large_repo(tmp_path: Path):
    """Behavioral: run the REAL shipped detect script over a many-file git repo where the
    matching workflow file sorts FIRST (the worst case for SIGPIPE). It must report
    has_source=true — not self-disable."""
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    bulk = repo / "zzz"
    bulk.mkdir()
    # Enough long-named files that git ls-files keeps writing well past grep's first match.
    for n in range(1500):
        (bulk / f"a_rather_long_padded_filename_to_fill_the_pipe_buffer_{n:05d}.txt").write_text("x")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)

    script = _extract_run_block(CODEQL_SELFGATE.read_text(), "id: detect")
    out_file = tmp_path / "ghout"
    out_file.write_text("")
    env = _clean_git_env()
    env["LANGUAGE"] = "actions"
    env["GITHUB_OUTPUT"] = str(out_file)
    proc = subprocess.run(
        ["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "has_source=true" in out_file.read_text(), (
        "detect self-disabled on a large repo that HAS source — the SIGPIPE bug is back.\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


@requires_git_bash
def test_codeql_detect_skips_cleanly_when_language_absent(tmp_path: Path):
    """Behavioral: a repo with no Python source → has_source=false (clean skip preserved)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.txt").write_text("no python here")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    script = _extract_run_block(CODEQL_SELFGATE.read_text(), "id: detect")
    out_file = tmp_path / "ghout"
    out_file.write_text("")
    env = _clean_git_env()
    env["LANGUAGE"] = "python"
    env["GITHUB_OUTPUT"] = str(out_file)
    proc = subprocess.run(
        ["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "has_source=false" in out_file.read_text()


# ══════════════════════════════════════════════════════════════════════════════════════════
# Bug 2 — leftover-grep: shallow PR-head fetch makes the diff a no-op
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_leftover_pr_fetch_is_not_shallow():
    """Static guard: the PR-head fetch must NOT be ``--depth=1`` (which breaks merge-base /
    the three-dot diff), and the base checkout must keep ``fetch-depth: 0``."""
    wf = LEFTOVER_WF.read_text()
    fetch_lines = [ln for ln in wf.splitlines() if "git fetch" in ln and "$HEAD_SHA" in ln]
    assert fetch_lines, "expected a `git fetch ... $HEAD_SHA` step"
    for ln in fetch_lines:
        assert "--depth" not in ln, f"PR-head fetch must not be shallow: {ln!r}"
    assert "fetch-depth: 0" in wf, "base checkout must fetch full history (fetch-depth: 0)"


@pytest.fixture
def leftover_pr_fixture(tmp_path: Path):
    """origin (main: A,B) + a separate 'fork' branching off B with a planted console.log (C).
    Mirrors a fork PR: the head SHA is fetchable but its ancestry is NOT in the fresh clone
    until we fetch it. Returns (make_ci_clone, head_sha)."""
    if not (_HAVE_GIT and _HAVE_BASH):
        pytest.skip("git + bash required")
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(origin), cwd=tmp_path)
    up = tmp_path / "up"
    up.mkdir()
    _git("init", "-q", "-b", "main", cwd=up)
    _git("config", "user.email", "t@t", cwd=up)
    _git("config", "user.name", "t", cwd=up)
    for i in range(30):
        (up / f"base{i}.txt").write_text(f"base {i}\n")
    _git("add", "-A", cwd=up)
    _git("commit", "-qm", "A", cwd=up)
    (up / "b.txt").write_text("b\n")
    _git("add", "-A", cwd=up)
    _git("commit", "-qm", "B", cwd=up)
    _git("push", "-q", str(origin), "main", cwd=up)

    fork = tmp_path / "fork"
    _git("clone", "-q", str(origin), str(fork), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=fork)
    _git("config", "user.name", "t", cwd=fork)
    _git("checkout", "-qb", "pr", cwd=fork)
    (fork / "leak.js").write_text("console.log('LEFTOVER')\n")
    _git("add", "-A", cwd=fork)
    _git("commit", "-qm", "C add leftover", cwd=fork)
    head_sha = _git("rev-parse", "HEAD", cwd=fork).stdout.strip()

    def make_ci_clone(name: str, depth_args: list[str]):
        clone = tmp_path / name
        _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
        _git("config", "user.email", "t@t", cwd=clone)
        _git("config", "user.name", "t", cwd=clone)
        _git("fetch", "--no-tags", *depth_args, str(fork), head_sha, cwd=clone)
        return clone

    return make_ci_clone, head_sha


def _run_leftover(clone: Path, head_sha: str) -> subprocess.CompletedProcess:
    env = _clean_git_env()
    env["LEFTOVER_BASE"] = "origin/main"
    env["LEFTOVER_HEAD"] = head_sha
    return subprocess.run(
        ["bash", str(LEFTOVER_SCRIPT)], cwd=clone, env=env, capture_output=True, text=True, timeout=60
    )


def test_leftover_full_fetch_catches_planted_leftover(leftover_pr_fixture):
    """With the FIXED full fetch, merge-base resolves, the diff sees the added line, and the
    planted console.log is BLOCKED (exit 1)."""
    make_ci_clone, head_sha = leftover_pr_fixture
    clone = make_ci_clone("ci_full", [])  # no --depth → full ancestry
    proc = _run_leftover(clone, head_sha)
    assert proc.returncode == 1, f"full fetch must catch the leftover; got rc={proc.returncode}\n{proc.stderr}"
    assert "leak.js" in proc.stderr and "console" in proc.stderr


def test_leftover_shallow_fetch_falls_back_to_twodot_and_still_catches(leftover_pr_fixture):
    """Defense in depth beyond the fetch-depth fix: the shipped script also falls back to a
    two-dot diff when the merge-base is unreachable (a shallow/direct-caller case the
    workflow's ``fetch-depth: 0`` doesn't cover on its own), so the planted leftover is still
    BLOCKED (exit 1) instead of silently missed (agent-tools#130) — a stronger guarantee than
    just pinning the shallow fetch as broken."""
    make_ci_clone, head_sha = leftover_pr_fixture
    clone = make_ci_clone("ci_shallow", ["--depth=1"])
    proc = _run_leftover(clone, head_sha)
    assert proc.returncode == 1, (
        "shallow fetch must still be caught via the two-dot fallback; got "
        f"rc={proc.returncode}\n{proc.stderr}"
    )
    assert "two-dot diff" in proc.stderr, "expected the two-dot fallback path to engage"
    assert "leak.js" in proc.stderr and "console" in proc.stderr


# ══════════════════════════════════════════════════════════════════════════════════════════
# Bug 1 — dependency-review: self-weakenable gate + missing toolchain
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_dep_review_trigger_is_pull_request_target():
    """The gate must run on ``pull_request_target`` (workflow def from the trusted base) — not
    the plain ``pull_request`` that let a PR weaken its own gate."""
    wf = DEP_WF.read_text()
    assert re.search(r"^\s*pull_request_target:", wf, re.M), "must trigger on pull_request_target"
    # The bare `pull_request:` gate trigger must be gone (pull_request_target is a distinct key).
    assert not re.search(r"^\s*pull_request:\s*$", wf, re.M), (
        "the self-weakenable bare `pull_request:` trigger must be replaced by pull_request_target"
    )


def test_dep_review_runs_base_copy_not_pr_copy():
    """The audit must execute the BASE-checked-out script (trusted): the workspace checkout
    carries no explicit PR-head ref (so it defaults to the base under pull_request_target),
    the PR head is only ever materialized as a side git-worktree (data, never checked out onto
    the workspace), and the script that runs is the trusted ``$GITHUB_WORKSPACE`` copy scanning
    that side directory — so a PR's edited dep-audit.sh is never run as its own gate."""
    wf = DEP_WF.read_text()
    checkout_block = _extract_step_block(wf, "Checkout BASE branch")
    assert "ref:" not in checkout_block, (
        "the workspace checkout must not pin an explicit ref — pull_request_target defaults "
        "to the base branch, which is the whole point"
    )
    assert "git worktree add --detach" in wf, (
        "the PR head must be materialized as a side worktree (data), never checked out onto "
        "the workspace"
    )
    assert 'run: sh "$GITHUB_WORKSPACE/ci/dependency-review/dep-audit.sh" "$AUDIT_DIR"' in wf, (
        "the script that runs must be the trusted base copy ($GITHUB_WORKSPACE) scanning the "
        "PR's fetched data ($AUDIT_DIR) — not a bare relative path that could resolve inside a "
        "PR-controlled checkout"
    )
    # The old self-weakenable invocation (PR's own working-tree copy) must be gone.
    assert not re.search(r"run:\s*sh ci/dependency-review/dep-audit\.sh\s*$", wf, re.M)


def test_dep_review_installs_bun_toolchain():
    """setup-bun must be ENABLED and SHA-pinned so a bun.lock repo audits instead of
    fail-closing on a missing `bun`."""
    wf = DEP_WF.read_text()
    bun_uses = [ln for ln in _uses_lines(wf) if "setup-bun" in ln]
    assert bun_uses, "setup-bun must be an active (uncommented) step, not a comment example"
    for ln in bun_uses:
        assert re.search(r"@[0-9a-f]{40}\b", ln), f"setup-bun must be SHA-pinned: {ln!r}"


def test_dep_review_no_secrets_and_read_only_token():
    """No elevated context to steal: contents:read and zero `secrets.` references — the
    neutralizer for the pull_request_target pwn-request RCE."""
    wf = DEP_WF.read_text()
    assert "contents: read" in wf
    assert "secrets." not in wf, "the dep-review job must reference no secrets (RCE-safe)"


def test_dep_review_actions_sha_pinned():
    """Every `uses:` must pin a 40-hex commit SHA (supply-chain hygiene)."""
    uses = _uses_lines(DEP_WF.read_text())
    assert uses, "expected at least one `uses:`"
    for ln in uses:
        assert re.search(r"uses:\s*\S+@[0-9a-f]{40}\b", ln), f"action not SHA-pinned: {ln!r}"


@pytest.mark.skipif(not _HAVE_BASH, reason="bash required")
def test_dep_audit_fail_closed_on_missing_bun_proves_toolchain_need(tmp_path: Path):
    """The reason setup-bun must be installed: a bun.lock with no `bun` on PATH fail-CLOSES
    (exit 1). DEP_AUDIT_ALLOW_MISSING=1 is the only intentional escape."""
    (tmp_path / "bun.lock").write_text("# lockfile\n")
    base_env = dict(os.environ)
    base_env["PATH"] = "/usr/bin:/bin"  # no bun resolvable
    closed = subprocess.run(
        ["sh", str(DEP_SCRIPT)], cwd=tmp_path, env=base_env, capture_output=True, text=True, timeout=60
    )
    assert closed.returncode == 1, f"missing bun must fail closed; got {closed.returncode}\n{closed.stderr}"
    assert "bun" in closed.stderr.lower()
    open_env = dict(base_env)
    open_env["DEP_AUDIT_ALLOW_MISSING"] = "1"
    opened = subprocess.run(
        ["sh", str(DEP_SCRIPT)], cwd=tmp_path, env=open_env, capture_output=True, text=True, timeout=60
    )
    assert opened.returncode == 0, opened.stderr
