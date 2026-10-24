"""Tests for the worktree-only-writes agent-hook (pre-write).

Covers the doctrine: on the DEFAULT branch of an ENROLLED repo an Edit/Write is DENIED; on a
feature branch it is ALLOWED; an un-enrolled repo (no env, no rig.yaml knob) is never blocked;
the escape hatch allows a deliberate main edit; detached HEAD / non-git fail OPEN. Default-branch
detection is exercised against REAL temp git repos (origin/HEAD, init.defaultBranch, and the
`master` case), not mocks — the git-subprocess path is the whole point of the hook.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_worktree_only_writes.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Isolate the throwaway test repos from the developer's GLOBAL git config — in particular the
# rig-provisioned `core.hooksPath`, whose pre-commit gates would otherwise fail `git commit` in a
# bare temp repo. The HOOK's own read-only git queries (rev-parse/symbolic-ref) are unaffected by
# hooks and deliberately use the ambient env, so this only sanitizes the test's setup commands.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "worktree-only-writes"
    / "worktree_only_writes.py"
)
_spec = importlib.util.spec_from_file_location("worktree_only_writes", _HOOK)
assert _spec and _spec.loader
wow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wow)

_ENV_KEYS = (
    "RIG_WORKTREE_ONLY",
    "RIG_ALLOW_MAIN_EDIT",
    "RIG_ALLOW_MAIN_EDIT_REASON",
    "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES",
    # Role detection env (agent-tools#994): a test run inside an agent must not inherit its role.
    "RIG_AGENT_ID",
    "RIG_PARENT_AGENT_ID",
    "RIG_DETACHED_AGENT",
    "PI_SESSION_FILE",
)


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


def _make_repo(tmp_path: Path, *, branch: str, default_via: str | None = None) -> Path:
    """A real git repo checked out on ``branch`` with one commit.

    default_via: None → rely on repo-local branch-existence detection; "origin" → point
    refs/remotes/origin/HEAD at origin/<branch> (the authoritative path).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    if default_via == "origin":
        _git(repo, "remote", "add", "origin", str(repo))
        _git(repo, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    return repo


def _run(cwd: Path, monkeypatch, env: dict | None = None, *, target: Path | None = None) -> tuple[str, int]:
    fp = str(target) if target is not None else str(cwd / "src" / "a.ts")
    event = {"cwd": str(cwd), "args": {"file_path": fp}}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = wow.main()
    return out.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── DENY on the default branch when enrolled ──────────────────────────────────────────────

def test_default_branch_enrolled_via_env_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == wow.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "worktree" in json.loads(out)["message"].lower()


def test_default_branch_enrolled_via_rigyaml_denies(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    out, code = _run(repo, monkeypatch)  # no env → enrolled purely by the committed rig.yaml
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_master_default_repo_denies_on_master(tmp_path, monkeypatch):
    """Default branch is DETECTED repo-locally, not hardcoded 'main': a master-only repo (no
    origin/HEAD, no global config consulted) resolves to master via branch existence and blocks."""
    repo = _make_repo(tmp_path, branch="master")
    assert wow.default_branch(str(repo)) == "master"
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_origin_head_detection_denies(tmp_path, monkeypatch):
    """The primary detection path — refs/remotes/origin/HEAD → origin/main → main."""
    repo = _make_repo(tmp_path, branch="main", default_via="origin")
    assert wow.default_branch(str(repo)) == "main"
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW on a feature branch ─────────────────────────────────────────────────────────────

def test_feature_branch_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "-b", "feat/x")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


# ── ALLOW when the repo is not enrolled (the 3d-cli / works-on-main case) ──────────────────

def test_default_branch_not_enrolled_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")  # no env, no rig.yaml → default OFF
    out, code = _run(repo, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_rigyaml_explicit_false_allows(tmp_path, monkeypatch):
    """A repo that works on main opts OUT explicitly (worktree_only: false) → never blocked."""
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: false\n")
    out, code = _run(repo, monkeypatch)
    assert code == 0 and _decision(out) == "allow"


def test_env_zero_overrides_rigyaml_true(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    (repo / "rig.yaml").write_text("agent_hooks:\n  worktree_only: true\n")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "0"})
    assert code == 0 and _decision(out) == "allow"


# ── the verdict follows the WRITE TARGET's checkout, not the shell cwd (codex) ────────────

def test_target_path_in_main_repo_denies_even_when_cwd_is_feature(tmp_path, monkeypatch):
    """cwd is a feature worktree, but the Write TARGETS an absolute path inside a main-branch
    checkout → BLOCK. If the guard used cwd's branch (feature) it would wrongly allow."""
    main_repo = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")  # cwd is a DIFFERENT checkout, on a feature branch
    out, code = _run(feat, monkeypatch, {"RIG_WORKTREE_ONLY": "1"},
                     target=main_repo / "src" / "a.ts")
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_target_path_in_feature_repo_allows_even_when_cwd_is_main(tmp_path, monkeypatch):
    """The inverse: cwd on main, but the Write TARGETS a feature worktree → ALLOW (authoring is
    happening where it should). If the guard used cwd's branch (main) it would falsely block."""
    main_repo = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat"
    feat.mkdir()
    _git(feat, "init", "-b", "feat/x")
    _git(feat, "config", "user.email", "t@t.t")
    _git(feat, "config", "user.name", "t")
    (feat / "seed.txt").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "seed")
    out, code = _run(main_repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"},
                     target=feat / "src" / "a.ts")
    assert code == 0 and _decision(out) == "allow"


# ── the OLD self-service escape hatch is GONE (agent-tools#213) ────────────────────────────

def test_old_self_service_env_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """`RIG_ALLOW_MAIN_EDIT=1` was the self-graded bypass an agent could set on itself. It is
    removed: setting it on the default branch of an enrolled repo no longer allows the write."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1", "RIG_ALLOW_MAIN_EDIT": "1"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── the RIG_HATCH_REQUEST_* Telegram escalation replaces it ────────────────────────────────

def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` (no written justification) is an invalid request → deny (block), and NO tg-ctl
    is ever invoked. A never-callable path proves no Telegram round-trip happens."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n")  # would ALLOW if ever called
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {
        "RIG_WORKTREE_ONLY": "1", "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES": "1",
    })
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 0 (the human approved) → allow."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES": "Hotfix on main, worktree unavailable.",
    })
    assert code == 0 and _decision(out) == "allow"
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks_citing_denial(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 1 (the human declined / timed out) → block, and the
    message leads with the denial reason."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(wow.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {
        "RIG_WORKTREE_ONLY": "1",
        "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES": "Hotfix on main, worktree unavailable.",
    })
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── fail-OPEN: detached HEAD / not a git repo ─────────────────────────────────────────────

def test_detached_head_allows(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, branch="main")
    _git(repo, "checkout", "--detach", "HEAD")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"


def test_non_git_dir_allows(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    out, code = _run(plain, monkeypatch, {"RIG_WORKTREE_ONLY": "1"})
    assert code == 0 and _decision(out) == "allow"  # current_branch None → fail-open


def test_bad_event_fails_open(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    assert wow.main() == 0 and _decision(out.getvalue()) == "allow"


# ── no path key at all is a DELIBERATE, documented cwd fallback, not a silent one (agent-tools#615) ─

def test_target_dir_falls_back_to_cwd_when_no_path_key_at_all():
    """No file_path/path/notebook_path key at all is now a deliberate, documented fallback
    to `cwd` (agent-tools#615) — not the old UNDOCUMENTED silent fallthrough. Judging `cwd`
    (rather than fail-opening) matters because codex's `apply_patch` bridge genuinely
    produces path-less pre-write events on a patch-parse miss; failing open there would
    silently bypass this guard on a real write."""
    assert wow._target_dir("/tmp", {}) == "/tmp"


@pytest.mark.parametrize("args", [
    {},
    {"file_path": ""},
    {"file_path": "   "},
    {"file_path": 123},
])
def test_target_dir_no_usable_path_always_uses_cwd(args):
    """A missing key and a present-but-blank/wrong-typed key are both "no path signal" and
    get the IDENTICAL deliberate outcome — the verdict must never depend on which of the two
    shapes a bridge happens to send."""
    assert wow._target_dir("/tmp", args) == "/tmp"


def test_target_dir_expands_tilde_before_absolute_check(monkeypatch, tmp_path):
    """agent-tools#698 / #700: a leading ~ is not absolute to pathlib — expanduser() must run
    before the is_absolute() join onto cwd."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = wow._target_dir("/some/other/cwd", {"file_path": "~/notes/a.txt"})
    assert str(home) in result
    assert "/some/other/cwd" not in result


def test_tilde_target_outside_repo_allows_even_when_cwd_is_enrolled_main(tmp_path, monkeypatch):
    """agent-tools#698 / #700: before expanduser(), a literal ~/... target was joined onto the
    enrolled main-branch cwd and BLOCKed. After the fix, the write resolves outside any git
    checkout and is ALLOWed even when cwd sits on an enrolled default branch."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run(
        repo,
        monkeypatch,
        {"RIG_WORKTREE_ONLY": "1"},
        target=Path("~/notes/a.txt"),
    )
    assert code == 0 and _decision(out) == "allow"


def test_no_path_key_blocks_on_enrolled_main_cwd(tmp_path, monkeypatch):
    """An event whose args carry NONE of file_path/path/notebook_path, on an enrolled
    default-branch cwd, still BLOCKs — the deliberate `cwd` fallback (agent-tools#615), not
    an accidental one. This is exactly the codex apply_patch-parse-miss shape: the guard
    must not silently allow a main-branch write just because the path could not be parsed."""
    repo = _make_repo(tmp_path, branch="main")
    event = {"cwd": str(repo), "args": {}}
    out_io = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out_io)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    code = wow.main()
    out = out_io.getvalue()
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── a RELATIVE path is judged against cwd, never guessed via a sibling worktree (agent-tools#615) ─

def test_relative_path_absent_from_cwd_still_blocks_despite_a_sibling_worktree_match(tmp_path, monkeypatch):
    """agent-tools#615 tried resolving a relative path against whichever OTHER git worktree
    of the same repo happened to have it (to fix the #613 false-block). Reverted: adversarial
    review found it exploitable (a coincidental same-named file in a sibling worktree would
    silently ALLOW a write really headed for main). Locks in the safe behavior: a relative
    path absent from cwd still BLOCKs on cwd's own branch despite a sibling match."""
    main_repo = _make_repo(tmp_path, branch="main")
    feat = tmp_path / "feat"
    _git(main_repo, "worktree", "add", "-b", "feat/x", str(feat))
    (feat / "new_helper.py").write_text("x")
    _git(feat, "add", "-A")
    _git(feat, "commit", "-m", "add helper")
    out, code = _run(main_repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"}, target="new_helper.py")
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── a relative path that RESOLVES TO cwd's own directory must not climb ABOVE it in a nested
# `rig worktree create` layout (`<repo>/.worktrees/<name>`) — distinct from #613/#615/#616,
# which are about a relative path judged against the wrong SIBLING checkout ──────────────────

def test_relative_dot_path_in_nested_worktree_allows_not_primary_checkout(tmp_path, monkeypatch):
    """`_target_dir` used to unconditionally take ``target.parent`` — for a relative path that
    pathlib normalizes to exactly `cwd` itself (a bare "."), that climbed ONE LEVEL ABOVE the
    nested worktree root into `.worktrees`, whose nearest git boundary is the PRIMARY checkout.
    In the sibling-directory layout the older tests use this is harmless (the level above a
    sibling worktree is not a git repo at all, so the check fails open); in a REALISTIC nested
    `<repo>/.worktrees/<name>` layout it lands squarely inside the primary checkout's OWN
    boundary — main — falsely BLOCKing a write that is genuinely happening on a feature
    branch, in the correct place."""
    main_repo = _make_repo(tmp_path, branch="main")
    wtdir = main_repo / ".worktrees" / "feat"
    _git(main_repo, "worktree", "add", "-b", "feat/x", str(wtdir))
    out, code = _run(wtdir, monkeypatch, {"RIG_WORKTREE_ONLY": "1"}, target=".")
    assert code == 0 and _decision(out) == "allow"


def test_relative_path_naming_nested_worktree_root_allows_not_primary_checkout(tmp_path, monkeypatch):
    """The same off-by-one from the other direction: a relative path that names the nested
    worktree's OWN root directory (no file component beneath it) must be judged against THAT
    worktree's branch, not one level up. `_target_dir` used to take `.parent` unconditionally
    even when the resolved target is already an existing DIRECTORY, landing on `.worktrees`
    (primary checkout boundary) instead of the worktree root itself."""
    main_repo = _make_repo(tmp_path, branch="main")
    wtdir = main_repo / ".worktrees" / "feat"
    _git(main_repo, "worktree", "add", "-b", "feat/x", str(wtdir))
    out, code = _run(main_repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1"}, target=".worktrees/feat")
    assert code == 0 and _decision(out) == "allow"


def test_target_dir_dot_path_stays_at_cwd_not_its_parent():
    """Unit-level pin: `_target_dir("/some/dir", {"file_path": "."})` must return `/some/dir`
    itself (an existing directory), never `/some/dir`'s parent."""
    assert wow._target_dir("/tmp", {"file_path": "."}) == "/tmp"


# ── omp internal URIs and path-less eval cells are not file writes (agent-tools#828) ───────

def _run_event(cwd: Path, monkeypatch, tool: str, args: dict) -> tuple[str, int]:
    """Feed one bridge-shaped pre-write event (enrolled via env) and return (stdout, exit)."""
    event = {"tool": tool, "cwd": str(cwd), "args": args}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RIG_WORKTREE_ONLY", "1")
    code = wow.main()
    return out.getvalue(), code


_HARNESS_URIS = (
    "agent://Main",
    "agent://all",
    "xd://report_issue",
    "xd://mcp__runpod_get_pod",
    "proc://bg_388/kill",
    "artifact://3155",
    "local://report.md",
    "local://notes/scout-findings.md",
    "skill://tdd-red-first",
    "history://Main",
    "mcp://runpod/guides/runpod-mcp",
    "local://%252561b",         # decodes in exactly _MAX_UNQUOTE_ROUNDS (3) rounds → `ab`
)
# Not harness channels: still judged as a path (each one resolves into the main checkout).
_NON_HARNESS_TARGETS = (
    "foo:bar/baz.txt",          # a real relative path that merely contains a colon
    "agent:/Main",              # one slash: a real `agent:` directory, not a URI
    "AGENT://Main",             # scheme match is exact; an unknown spelling is a real path
    "myagent://Main",           # a recognized scheme only as a SUFFIX of the prefix
    "file://seed.txt",          # not an omp harness scheme
    "ssh://localhost/seed.txt", # ssh:// writes real files (possibly on this host)
    "local://../../seed.txt",   # a `..` segment could climb out of the session dir
    "local://%2e%2e/seed.txt",  # … also percent-encoded
    "local://%252e%252e/seed.txt",  # … also doubly encoded
    "local://a\\..\\..\\seed.txt",  # … also with backslash separators
    "xd://ast_edit",            # file-rewriting tool devices stay judged as writes
    "xd://ast_edit/x",
    "xd://ast_edit?x",
    "xd://%61st_edit",          # … also percent-encoded
    "xd://AST_EDIT",            # … also upper-cased
    "xd://resolve",
    "xd://resolve%3Fq",
    " xd://ast_edit ",          # surrounding whitespace is stripped before matching
    "local://%25252561b",       # still changing after 3 rounds → refused as a channel
)


@pytest.mark.parametrize("uri", _HARNESS_URIS)
def test_internal_uri_write_on_enrolled_main_allows(tmp_path, monkeypatch, uri):
    """`write <scheme>://…` is a harness channel (peer message, QA report, job control, session
    artifact), not a file in the checkout. The omp bridge sets both `path` and `file_path` to
    the raw URI; joined onto cwd it used to resolve to `<main>/agent:/Main` → main → BLOCK."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run_event(repo, monkeypatch, "write", {"path": uri, "file_path": uri, "content": "hi"})
    assert code == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("target", _NON_HARNESS_TARGETS)
def test_non_harness_or_file_mutating_uri_on_enrolled_main_still_blocks(tmp_path, monkeypatch, target):
    """Only the exact allowlisted harness schemes are exempt; anything else is still a path
    judged against cwd, and the xd:// devices that rewrite files keep the cwd verdict."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run_event(repo, monkeypatch, "write", {"path": target, "file_path": target, "content": "x"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_internal_uri_in_secondary_key_does_not_mask_a_real_main_path(tmp_path, monkeypatch):
    """The verdict keys on the SAME first usable path `_target_dir` judges: a harness URI in a
    later key must not exempt a real `file_path` into the main checkout."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run_event(repo, monkeypatch, "write",
                           {"file_path": str(repo / "a.txt"), "path": "agent://Main", "content": "x"})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_eval_without_scraped_write_on_enrolled_main_allows(tmp_path, monkeypatch):
    """A read-only eval cell (here: an HTTP GET plus regex filtering) carries no path and an
    empty scraped `content` — the bridge found no file write, so there is nothing to redirect."""
    repo = _make_repo(tmp_path, branch="main")
    code_text = "import re, urllib.request\nprint(re.findall('x', urllib.request.urlopen(u).read().decode()))"
    out, code = _run_event(repo, monkeypatch, "eval", {"language": "py", "code": code_text, "content": ""})
    assert code == 0 and _decision(out) == "allow"


def test_eval_writing_a_scraped_main_path_still_blocks(tmp_path, monkeypatch):
    """An eval cell whose scraped write target lands in the main checkout is judged like `write`."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run_event(repo, monkeypatch, "eval", {
        "language": "py", "code": "open('x.txt','w').write('a')",
        "file_path": "x.txt", "path": "x.txt", "content": "a",
    })
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_eval_with_unresolved_write_target_on_enrolled_main_still_blocks(tmp_path, monkeypatch):
    """A recognized write call whose target could not be resolved (the bridge's path-less
    bucket: content scraped, no path) keeps the #615 cwd fallback — it IS a file write, the
    target is just unknown, the eval analogue of codex's apply_patch parse miss."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run_event(repo, monkeypatch, "eval", {
        "language": "py", "code": "with open(p, 'w') as f: f.write('a')", "content": "a",
    })
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_pathless_non_eval_write_still_blocks(tmp_path, monkeypatch):
    """The eval exemption is tool-scoped: a path-less `apply_patch` (codex parse miss) with
    empty content still takes the #615 cwd fallback and BLOCKs on main."""
    repo = _make_repo(tmp_path, branch="main")
    out, code = _run_event(repo, monkeypatch, "apply_patch", {"content": ""})
    assert code == wow.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_harness_uri_rules_match_orchestrator_stays_thin_copy():
    """SYNC guard: orchestrator-stays-thin carries a standalone copy of `_is_harness_uri`."""
    ost_path = _HOOK.parents[1] / "orchestrator-stays-thin" / "orchestrator_stays_thin.py"
    spec = importlib.util.spec_from_file_location("orchestrator_stays_thin_sync", ost_path)
    assert spec and spec.loader
    ost = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ost)
    assert ost._HARNESS_URI_SCHEMES == wow._HARNESS_URI_SCHEMES
    assert ost._FILE_MUTATING_XD_DEVICES == wow._FILE_MUTATING_XD_DEVICES
    assert ost._HARNESS_URI_RE.pattern == wow._HARNESS_URI_RE.pattern
    assert ost._MAX_UNQUOTE_ROUNDS == wow._MAX_UNQUOTE_ROUNDS
    assert _function_body_dump(ost_path, "_is_harness_uri") == _function_body_dump(_HOOK, "_is_harness_uri")
    for raw in _HARNESS_URIS:
        assert ost._is_harness_uri(raw) and wow._is_harness_uri(raw), raw
    for raw in _NON_HARNESS_TARGETS:
        assert not ost._is_harness_uri(raw) and not wow._is_harness_uri(raw), raw


def _function_body_dump(path: Path, name: str) -> str:
    """`ast.dump` of a top-level function's body without its docstring (the copies' differ)."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body[1:] if ast.get_docstring(node) is not None else node.body
            return ast.dump(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{name} not found in {path}")


# ── unit: the minimal rig.yaml boolean parse ──────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("agent_hooks:\n  worktree_only: true\n", True),
    ("agent_hooks:\n  worktree_only: false\n", False),
    ("agent_hooks:\n  worktree_only: yes  # inline comment\n", True),
    ("agent_hooks:\n  all: true\n", False),                       # key absent → default
    ("skills:\n  worktree_only: true\n", False),                  # wrong block → not read
    ("agent_hooks:\n  items:\n    x:\n      on_error: open\n", False),  # nested other key
    # P2b: a DEEPER-nested worktree_only (under items.<hook>) must NOT flip the guard — only a
    # DIRECT child of agent_hooks counts.
    ("agent_hooks:\n  items:\n    x:\n      worktree_only: true\n", False),
    ("agent_hooks:\n  all: true\n  worktree_only: true\n", True),  # direct child (not first) → read
    ("agent_hooks:\n  worktree_only:\n", False),                   # blank value → default (False here)
    ("", False),
])
def test_agent_hooks_bool_parse(text, expected):
    assert wow._agent_hooks_bool(text, "worktree_only", default=False) is expected


# ── agent-tools#994: role-aware escalation ladder ───────────────────────────────────────────
_SESSION_DIR = "2026-10-24T10-11-12-345Z_0a1b2c3d-1111-2222-3333-444455556666"
_SUBAGENT_ENV = {
    "RIG_AGENT_ID": "Kid",
    "PI_SESSION_FILE": f"/home/u/.omp/sessions/proj/{_SESSION_DIR}/Kid.jsonl",
}
_COORDINATOR_ENV = {"PI_SESSION_FILE": f"/home/u/.omp/sessions/proj/{_SESSION_DIR}.jsonl"}
_HATCH_VAR = "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES"


def _ladder_message(tmp_path, monkeypatch, role_env: dict) -> str:
    repo = tmp_path / "repo" if (tmp_path / "repo").exists() else _make_repo(tmp_path, branch="main")
    out, code = _run(repo, monkeypatch, {"RIG_WORKTREE_ONLY": "1", **role_env})
    assert code == wow.BLOCK_EXIT_CODE, out
    return json.loads(out)["message"]


def test_ladder_leads_with_marker_and_worktree_remedy(tmp_path, monkeypatch):
    for env in ({}, _SUBAGENT_ENV, _COORDINATOR_ENV):
        msg = _ladder_message(tmp_path, monkeypatch, env)
        assert msg.startswith("You are on the default branch (main)")
        step1 = msg[msg.index("\n1. "):msg.index("\n2. ")]
        assert "git worktree add ../wt-<feature>" in step1
        assert "ASK the human" not in msg and "Closes" not in msg and "Fixes" not in msg


def test_ladder_subagent_messages_parent_and_has_no_human_hatch(tmp_path, monkeypatch):
    msg = _ladder_message(tmp_path, monkeypatch, _SUBAGENT_ENV)
    assert "`write agent://Main`" in msg
    assert "`yield` status `awaiting-coordinator` (NOT blocked/done/failed)" in msg
    assert "`write agent://Kid`" in msg
    assert _HATCH_VAR not in msg and "Telegram" not in msg


def test_ladder_coordinator_relays_and_hatch_is_last_line(tmp_path, monkeypatch):
    msg = _ladder_message(tmp_path, monkeypatch, _COORDINATOR_ENV)
    assert "write agent://<id>" in msg
    last = msg.rstrip().splitlines()[-1]
    assert _HATCH_VAR in last and "Telegram" in last and msg.count(_HATCH_VAR) == 1


def test_ladder_unknown_role_has_both_branches(tmp_path, monkeypatch):
    msg = _ladder_message(tmp_path, monkeypatch, {})
    assert "If you were dispatched by another agent" in msg
    assert "If you are the top-level agent" in msg
    assert _HATCH_VAR in msg.rstrip().splitlines()[-1]


def test_ladder_falls_back_to_neutral_text_when_lib_is_unavailable(monkeypatch):
    monkeypatch.setattr(wow, "_guard_message", None)
    msg = wow._render_block("You are on main.", "Use `git worktree add ../wt-<feature>`.", "hatch line")
    assert msg.startswith("You are on main.") and "git worktree add" in msg
    assert "dispatched by another agent" in msg


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
