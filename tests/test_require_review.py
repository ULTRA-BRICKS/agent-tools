"""Tests for the require-review-before-commit agent-hook (pre-bash, commit gate).

Covers the two fixes that landed together:

  (A) ROADMAP "require-review-before-commit too broad":
        - DOCS-ONLY staged diff (every path *.md / under docs/) → ALLOW without a marker.
        - `git stash` / `git worktree` (non-commit git ops) → NOT gated.
        - a mixed diff (code + docs) with no marker still BLOCKs.

  (B) SECURITY (task #20): the skip-flag exemption (`--continue/--abort/--skip`) used to
      `re.search` the RAW command, so a skip token in a comment / commit message / pathspec /
      sibling command bypassed the gate. It is now derived from the PARSED `git commit` argv.

The OLD self-service escape hatch (`REVIEW_SKIP=1` inline env, `[skip-review: <reason>]`
commit-message trailer) is DEAD — those no longer bypass; a one-time exception is an external
Telegram hatch via `RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT` (deny-by-default).

Hermetic: a real tiny git repo is created in tmp_path so the hook's own `git diff --cached
--name-only` runs for real; the review-marker path is redirected into tmp_path. The marker
fail-OPEN path is exercised with a non-repo cwd.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_require_review.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "require-review-before-commit"
    / "require_review.py"
)
_spec = importlib.util.spec_from_file_location("require_review", _HOOK)
assert _spec and _spec.loader
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=30)


def _init_repo(repo: Path, *files: str) -> Path:
    """Init a git repo at `repo` and stage `files`."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # Isolate from any global core.hooksPath on the host so fixture commits don't run real hooks.
    _git(repo, "config", "core.hooksPath", "/dev/null")
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        _git(repo, "add", rel)
    return repo


def _mk_repo_with_staged(tmp_path: Path, *files: str) -> Path:
    """Convenience: a repo at `tmp_path/repo` with `files` staged (the common single-repo case)."""
    return _init_repo(tmp_path / "repo", *files)


def _run(command, cwd, monkeypatch, *, marker: Path,
         env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(cwd), "args": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("REVIEW_MARKER", str(marker))
    monkeypatch.delenv("REVIEW_SKIP", raising=False)
    monkeypatch.delenv("RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = rr.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _touch_marker(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("x")  # fresh mtime → counts as "review ran this session"


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


# ── BLOCK — a real authoring commit with code staged and no marker ───────────────────────

def test_block_code_commit_with_no_marker(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_allow_code_commit_with_fresh_marker(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    marker = tmp_path / "m"
    _touch_marker(marker)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=marker)
    assert c == 0 and _decision(out) == "allow"


def test_fresh_marker_short_circuits_before_git_diff(tmp_path, monkeypatch):
    """Perf: a fresh marker allows the commit WITHOUT running the docs-only `git diff` — the
    common 'ran review → commit' path must not pay a subprocess (codex LOW). Asserted by making
    `staged_files` blow up: if it were called, the test would error."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    marker = tmp_path / "m"
    _touch_marker(marker)

    def _boom(_cwd):
        raise AssertionError("staged_files must NOT run when a fresh marker exists")

    monkeypatch.setattr(rr, "staged_files", _boom)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=marker)
    assert c == 0 and _decision(out) == "allow"


def test_staged_files_timeout_forfeits_fastpath_and_blocks(tmp_path, monkeypatch):
    """If the docs `git diff` times out / errors (`staged_files`→None) with no fresh marker, the
    docs-only fast-path is forfeited and the commit BLOCKs (not an allow) (codex test gap)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")  # would be docs-only if listed
    monkeypatch.setattr(rr, "staged_files", lambda _cwd: None)  # simulate timeout/error
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── (A) docs-only diffs → ALLOW without a marker ─────────────────────────────────────────

@pytest.mark.parametrize("files", [
    ("README.md",),
    ("ROADMAP.md", "docs/specs/x.md"),
    ("docs/plans/p.txt",),                 # under docs/ + not code → docs (a .txt is fine here)
    ("CHANGELOG.md", "docs/a/b/c.mdx"),
])
def test_allow_docs_only_commit_without_marker(files, tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, *files)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0, files
    assert _decision(out) == "allow"


def _init_repo_with_unstaged_edit(repo_root: Path, staged: str, tracked_edit: str) -> Path:
    """A repo where `staged` is in the index, and `tracked_edit` is a COMMITTED-then-MODIFIED file
    whose edit is NOT staged (so only a `git commit -a/-am` would sweep it in)."""
    repo = _init_repo(repo_root, tracked_edit)            # stage + (below) commit the tracked file
    _git(repo, "commit", "-q", "-m", "base")
    (repo / tracked_edit).write_text("modified, unstaged")  # dirty working tree, not staged
    p = repo / staged
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("doc")
    _git(repo, "add", staged)                              # index now holds ONLY the docs file
    return repo


def test_commit_dash_a_bypass_with_docs_index_is_gated(tmp_path, monkeypatch):
    """codex HIGH: index holds only README.md (docs) but `git commit -am` ALSO commits an unstaged
    code edit. The docs-only fast-path (which reads the index) must NOT fire for `-a/-am`, so the
    commit is GATED (no marker → block) instead of waving the code through un-reviewed."""
    repo = _init_repo_with_unstaged_edit(tmp_path / "r", staged="README.md", tracked_edit="app.py")
    out, _e, c = _run("git commit -am x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_commit_pathspec_bypass_with_docs_index_is_gated(tmp_path, monkeypatch):
    """An explicit code PATHSPEC commits that file regardless of a docs-only index → must gate."""
    repo = _init_repo_with_unstaged_edit(tmp_path / "r", staged="README.md", tracked_edit="app.py")
    out, _e, c = _run("git commit -m x -- app.py", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("flag", [
    "--pathspec-from-file=specfile", "--pathspec-from-file specfile",
    "-p", "--patch", "--interactive",   # interactive/patch selection bypasses the index too
    "-pm", "-im",                       # patch/interactive CLUSTERED with -m (codex LOW)
])
def test_index_bypassing_flags_with_docs_index_are_gated(flag, tmp_path, monkeypatch):
    """`--pathspec-from-file`, `-p/--patch`, `--interactive` all draw content from BEYOND the staged
    index, so a docs-only index does NOT represent the commit → must gate (codex)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run(f"git commit {flag} -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, flag
    assert _decision(out) == "block"


def test_skip_review_trailer_in_message_no_longer_bypasses(tmp_path, monkeypatch):
    """REGRESSION: a `[skip-review: …]` trailer in the commit message (even glued into `-msystem…`)
    is NO LONGER read as a bypass — the self-service trailer is gone. Code staged, no marker →
    still BLOCK."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m'system [skip-review: x]'", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_commit_extends_index_unit():
    f = lambda cmd: rr.commit_extends_index(rr._commit_segment(cmd).argv)  # noqa: E731
    assert f("git commit -m x") is False
    assert f("git commit -am x") is True
    assert f("git commit --all -m x") is True
    assert f("git commit -m x -- src/util.py") is True
    assert f("git commit -m x file.py") is True
    assert f("git commit -m all") is False           # message text, not -a
    assert f("git commit --amend --no-edit") is False
    assert f("git commit -p") is True                # patch mode selects hunks at commit time
    assert f("git commit --patch -m x") is True
    assert f("git commit --interactive") is True
    assert f("git commit --include f.py") is True    # long forms of index-extending selectors
    assert f("git commit --only f.py") is True
    assert f("git commit --pathspec-from-file=specs") is True
    assert f("git commit -m x -- 2") is True         # `2` is a pathspec after `--`, not an fd
    assert f("git commit -m x 2> err") is False      # a real redirect is stripped, not a pathspec
    assert f("git commit -madd") is False            # glued message 'add' — NOT -a (codex LOW-1)
    assert f("git commit -mabc") is False
    assert f("git commit --message='add docs'") is False
    # a cluster ending in m/F (`-nm`, `-qm`, `-sm`, `-nF`) takes its value — not a pathspec (codex)
    assert f("git commit -nm 'update docs'") is False
    assert f("git commit -qm msg") is False
    assert f("git commit -nF /tmp/msg") is False
    # a GLUED short value-flag must not have its VALUE letters misread as -a/-p/-i/-o (codex)
    assert f("git commit -uno") is False        # -u value 'no' (the 'o' is value, not -o)
    assert f("git commit -Skeyid") is False     # -S value 'keyid' (the 'i' is value, not -i)
    assert f("git commit -tTPL.txt") is False   # -t template value
    assert f("git commit -ap") is True          # -a then -p — both before any value-flag
    assert f("git commit -mp") is False         # -m glued value 'p', not -p
    # separate value-flags whose VALUE must not be misread as a pathspec (codex MEDIUM)
    assert f("git commit --author 'Jane <j@x>' -m d") is False
    assert f("git commit --author=Jane -m d") is False
    assert f("git commit --date '2026-01-01' -m d") is False
    assert f("git commit --fixup HEAD~1") is False
    assert f("git commit -C HEAD~1") is False


def test_docs_commit_with_author_flag_keeps_fastpath(tmp_path, monkeypatch):
    """`git commit --author 'A <a@x>' -m d` on a docs-only index must keep the docs-fast-path
    (the author VALUE is not a pathspec) → allow without a marker (codex MEDIUM)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git commit --author 'Jane Doe <j@x>' -m d", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_code_under_docs_dir_is_NOT_docs_and_is_gated(tmp_path, monkeypatch):
    """A CODE file under docs/ (`docs/build.py`, `docs/deploy.sh`, `docs/conf.py`) is NOT treated as
    docs — review must still see executable change even when it sits under docs/ (codex). Gated."""
    for code in ("docs/build.py", "docs/deploy.sh", "docs/conf.py", "docs/Makefile.mk"):
        repo = _init_repo(tmp_path / code.replace("/", "_"), code)
        out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
        assert c == rr.BLOCK_EXIT_CODE, code
        assert _decision(out) == "block"


def test_extensionless_code_under_docs_dir_is_NOT_docs(tmp_path, monkeypatch):
    """An EXTENSIONLESS code/config file under docs/ (`docs/Makefile`, `docs/Dockerfile`,
    `docs/Jenkinsfile`) is NOT docs — CODE_EXT can't catch it by extension, so a basename denylist
    does (codex). Gated."""
    for code in ("docs/Makefile", "docs/Dockerfile", "docs/Jenkinsfile"):
        repo = _init_repo(tmp_path / code.replace("/", "_"), code)
        out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
        assert c == rr.BLOCK_EXIT_CODE, code
        assert _decision(out) == "block"


def test_docs_commit_with_no_verify_cluster_keeps_fastpath(tmp_path, monkeypatch):
    """`git commit -nm 'update docs'` (a `-n`+`-m` cluster) on a docs-only index must keep the
    fast-path — the message value must not be misread as a pathspec (codex over-block). Allow."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git commit -nm 'update docs'", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("cmd", ["git commit -uno -m x", "git commit -Skeyid -m x"])
def test_docs_commit_with_glued_value_flag_keeps_fastpath(cmd, tmp_path, monkeypatch):
    """A glued short value-flag (`-uno`, `-Skeyid`) on a docs-only index must keep the fast-path —
    the value letters (`o` of `-uno`, `i` of `-Skeyid`) must not be misread as `-o`/`-i` index-
    extending flags (codex over-block). Allow."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run(cmd, repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0, cmd
    assert _decision(out) == "allow"


def test_requirements_txt_is_NOT_docs_and_is_gated(tmp_path, monkeypatch):
    """`.txt` is no longer auto-docs — a dependency manifest (`requirements.txt`) is exactly the
    supply-chain change review should see (codex). Gated."""
    repo = _mk_repo_with_staged(tmp_path, "requirements.txt")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_non_code_asset_under_docs_dir_is_docs(tmp_path, monkeypatch):
    """A non-code asset under docs/ (an image, an html page) is still docs → allowed."""
    repo = _mk_repo_with_staged(tmp_path, "docs/diagram.svg", "docs/guide.html")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_env_wrapped_commit_is_not_gated_documented(tmp_path, monkeypatch):
    """Documented trade-off (LIMITATION in _commit_segments): a commit run through the `env`
    program (`env VAR=1 git commit`) has executable `env`, not git → NOT recognized → allow. This
    locks the KNOWN behavior so a future change to it is a conscious one (codex LOW-3)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("env FOO=1 git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_glued_message_with_letter_a_does_not_lose_docs_fastpath(tmp_path, monkeypatch):
    """`git commit -m'add'` (glued message containing `a`) must NOT be misread as `-a`, so a
    docs-only commit with such a message keeps the fast-path and is allowed (codex LOW-1)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git commit -madd", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_second_authoring_commit_in_chain_is_gated(tmp_path, monkeypatch):
    """codex MEDIUM: a skip-flag FIRST commit must not shield a real SECOND one in the same line.
    `git commit --abort && git commit -am big` — the 2nd commit is a real authoring commit with no
    skip, so the whole command is GATED (no marker → block)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit --abort && git commit -m big",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_two_skip_flag_commits_in_chain_allowed(tmp_path, monkeypatch):
    """Both commits in a chain carry a skip flag (--abort/--skip) → nothing to gate → allow."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit --abort && git commit --skip",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_staging_op_in_chain_forfeits_docs_fastpath(tmp_path, monkeypatch):
    """`git commit -m docs && git add app.py && git commit -m feat` — the index at hook time is
    docs-only, but the 2nd commit follows a `git add` that will stage code. The docs-fast-path is
    forfeited for any commit AFTER a staging op in the chain → the whole command gates (codex)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")  # index docs-only at hook time
    out, _e, c = _run("git commit -m docs && git add app.py && git commit -m feat",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("staging", ["git add app.py", "git rm old.py", "git reset HEAD~1",
                                     "git restore --staged x"])
def test_various_staging_ops_in_chain_forfeit_fastpath(staging, tmp_path, monkeypatch):
    """Any index-mutating op (`add`/`rm`/`reset`/`restore`) before a commit forfeits the fast-path."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run(f"{staging} && git commit -m docs", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, staging
    assert _decision(out) == "block"


def test_non_staging_op_in_chain_keeps_fastpath(tmp_path, monkeypatch):
    """A NON-staging command before a docs commit (`echo`, `git status`) does NOT forfeit the
    fast-path — only index-mutating ops do. Docs-only → allow."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git status && git commit -m docs", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("redir", ["> out.log", "2>&1", ">> append.txt", "&> all", "2> err"])
def test_docs_commit_with_redirect_keeps_fastpath(redir, tmp_path, monkeypatch):
    """A shell redirect after a docs-only commit must not be read as a pathspec (which would forfeit
    the docs-fast-path and over-block). The redirect tokens are stripped → allow (codex)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run(f"git commit -m x {redir}", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0, redir
    assert _decision(out) == "allow"


def test_redirect_like_text_in_quoted_message_is_preserved(tmp_path, monkeypatch):
    """A `>`/`>>` INSIDE a quoted message is part of the message, not a redirect — a docs commit
    with such a message is still allowed (the message isn't stripped)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git commit -m 'docs: pipe a > b and x >> y'", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_non_ascii_docs_path_is_classified_as_docs(tmp_path, monkeypatch):
    """A non-ASCII docs filename (`café.md`) must still classify as docs-only → allow. Without
    `core.quotePath=false`, git would emit `"caf\\303\\251.md"` (octal-escaped, trailing quote) and
    the `\\.md$` suffix match would fail → the docs commit would be wrongly BLOCKED (codex LOW-3)."""
    repo = _mk_repo_with_staged(tmp_path, "café.md")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_block_mixed_docs_and_code_without_marker(tmp_path, monkeypatch):
    """A diff that touches a .md AND a code file is NOT docs-only → still gated."""
    repo = _mk_repo_with_staged(tmp_path, "README.md", "src/util.py")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_docs_under_nested_docs_dir_is_docs(tmp_path, monkeypatch):
    """`src/docs/guide.html` lives under a docs/ dir → docs-only → allow (no marker)."""
    repo = _mk_repo_with_staged(tmp_path, "src/docs/guide.html")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_docs_classification_uses_dash_C_target_repo_not_cwd(tmp_path, monkeypatch):
    """`git -C <target> commit` stages in <target> — docs-classification must inspect <target>,
    not the event cwd (codex MEDIUM-2). cwd repo has CODE staged; the target repo is docs-only →
    allow. (If the hook had looked at cwd it would have BLOCKED on the code there.)"""
    cwd_repo = _init_repo(tmp_path / "cwd", "src/util.py")          # code in cwd
    target = _init_repo(tmp_path / "tgt", "README.md", "docs/x.md")  # docs-only target
    out, _e, c = _run(f"git -C {target} commit -m x", cwd_repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_dash_C_target_with_code_is_gated(tmp_path, monkeypatch):
    """The converse: cwd is docs-only but the `-C` target has CODE → gate (classify the target)."""
    cwd_repo = _init_repo(tmp_path / "cwd", "README.md")            # docs-only in cwd
    target = _init_repo(tmp_path / "tgt", "src/util.py")            # code in target
    out, _e, c = _run(f"git -C {target} commit -m x", cwd_repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_relative_dash_C_target_resolves_against_cwd(tmp_path, monkeypatch):
    """A RELATIVE `-C sub` resolves against the event cwd. The cwd is a plain dir; `sub` is a
    docs-only repo → docs-only → allow. Locks the os.path.join(cwd, target) resolution."""
    base = tmp_path / "base"
    base.mkdir()
    _init_repo(base / "sub", "README.md", "docs/x.md")  # docs-only repo at base/sub
    out, _e, c = _run("git -C sub commit -m x", base, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── (76) docs-only via `cd <worktree> && git commit` ─────────────────────────────────────
#
# CC's PreToolUse fires BEFORE the shell command runs: the event `cwd` is the SESSION
# default (main repo root), not the post-cd directory. The hook must detect a preceding
# `cd <dir>` in the chain and use it as the effective cwd for staging classification so
# `cd /worktree && git commit` reads the WORKTREE's index, not the main repo's (empty).

def test_allow_docs_only_via_cd_worktree(tmp_path, monkeypatch):
    """`cd <worktree> && git commit` — cwd is main (nothing staged), worktree has only .md
    staged → the docs-only fast-path must activate from the cd-dir, not from main → allow."""
    worktree = _init_repo(tmp_path / "worktree", "SKILL.md", "docs/guide.md")
    main_repo = _init_repo(tmp_path / "main")  # nothing staged
    cmd = f"cd {worktree} && git commit -m 'fix: add SKILL.md'"
    out, _e, c = _run(cmd, main_repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_cd_worktree_with_code_staged_is_gated(tmp_path, monkeypatch):
    """The converse: worktree has CODE staged — the cd-dir lookup still finds code → gate."""
    worktree = _init_repo(tmp_path / "worktree", "src/app.py")
    main_repo = _init_repo(tmp_path / "main")  # nothing staged
    cmd = f"cd {worktree} && git commit -m 'feat: new module'"
    out, _e, c = _run(cmd, main_repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_cd_then_git_add_then_commit_forfeits_docs_fastpath(tmp_path, monkeypatch):
    """`cd <worktree> && git add file && git commit` — a staging op after the cd forfeits the
    docs-only fast-path (`staging_before=True`) even though the cd-dir is a docs-only repo."""
    worktree = _init_repo(tmp_path / "worktree", "SKILL.md")
    main_repo = _init_repo(tmp_path / "main")  # nothing staged
    cmd = f"cd {worktree} && git add SKILL.md && git commit -m 'docs'"
    out, _e, c = _run(cmd, main_repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_relative_cd_dir_resolves_against_event_cwd(tmp_path, monkeypatch):
    """A RELATIVE `cd sub` in the chain resolves against the event cwd — same as `-C sub`."""
    base = tmp_path / "base"
    base.mkdir()
    _init_repo(base / "sub", "README.md")  # docs-only repo at base/sub
    out, _e, c = _run("cd sub && git commit -m docs", base, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_is_cd_segment_unit():
    """Unit-level coverage of `_is_cd_segment` edge cases."""
    assert rr._is_cd_segment(["cd", "/abs/path"]) == "/abs/path"
    assert rr._is_cd_segment(["cd", "relative/path"]) == "relative/path"
    assert rr._is_cd_segment(["cd"]) is None          # bare cd → $HOME, can't resolve statically
    assert rr._is_cd_segment(["cd", "-"]) is None     # cd - → prev dir, can't resolve statically
    assert rr._is_cd_segment(["cd", "--help"]) is None
    assert rr._is_cd_segment(["git", "commit"]) is None
    assert rr._is_cd_segment([]) is None


def test_cd_dir_not_propagated_across_pipe(tmp_path, monkeypatch):
    """`cd /docs_repo | git commit` — pipe creates a subshell for `cd`; the cwd of `git commit`
    is the event cwd (main repo), not `/docs_repo`. The docs-only fast-path must NOT fire from
    the pipe-left `cd` dir; the main repo has code staged → gate."""
    docs_repo = _init_repo(tmp_path / "docs", "README.md")  # docs-only if we looked here
    main_repo = _init_repo(tmp_path / "main", "src/app.py")  # code staged — this is the real cwd
    cmd = f"cd {docs_repo} | git commit -m x"
    out, _e, c = _run(cmd, main_repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_cd_dir_not_propagated_across_or_separator(tmp_path, monkeypatch):
    """`cd /docs_repo || git commit` — `||` only runs the right side when the left FAILED; if `cd`
    succeeds the commit never runs, and if `cd` fails the commit runs in the original cwd.
    Either way the cd-dir must NOT be used to classify staged files → gate on code in main repo."""
    docs_repo = _init_repo(tmp_path / "docs", "README.md")  # docs-only if we looked here
    main_repo = _init_repo(tmp_path / "main", "src/app.py")  # code staged — this is the real cwd
    cmd = f"cd {docs_repo} || git commit -m x"
    out, _e, c = _run(cmd, main_repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_relative_dash_C_after_cd_resolves_against_cd_dir(tmp_path, monkeypatch):
    """`cd /worktree && git -C sub commit` — the relative `-C sub` resolves against the post-cd
    effective cwd (`/worktree/sub`), not against the event cwd. If `/worktree/sub` is docs-only
    and the event cwd has code staged, the docs-only fast-path should activate → allow."""
    base = tmp_path / "base"
    base.mkdir()
    worktree = base / "worktree"
    worktree.mkdir()
    _init_repo(worktree / "sub", "SKILL.md")          # docs-only at /worktree/sub
    main_repo = _init_repo(tmp_path / "main", "src/app.py")  # code staged — the event cwd
    cmd = f"cd {worktree} && git -C sub commit -m docs"
    out, _e, c = _run(cmd, main_repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── REGRESSION: the OLD self-service per-commit skip is DEAD (inline env + trailer) ───────

def test_inline_review_skip_env_no_longer_bypasses(tmp_path, monkeypatch):
    """REGRESSION: `REVIEW_SKIP=1 git commit` used to opt out — it must NO LONGER bypass. The
    inline env is still parsed correctly (leading-assignment skip stays), but it carries no
    exemption any more. Code staged, no marker → BLOCK."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("REVIEW_SKIP=1 git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("prefix", [
    "FOO=bar",
    "GIT_AUTHOR_NAME=x",
    "REVIEW_SKIP=1 BAR=y",                  # even a once-truthy REVIEW_SKIP prefix no longer opts out
    "GIT_AUTHOR_NAME='Jane Doe'",          # a QUOTED env value with an internal space (codex find)
    "GIT_COMMITTER_DATE='2026-01-01 12:00'",
])
def test_unrelated_inline_env_prefix_still_gated(prefix, tmp_path, monkeypatch):
    """An inline-env prefix must NOT let a commit escape the gate.
    Regression: a regex pre-filter anchored to `git` could not follow a quoted env value with an
    internal space (`GIT_AUTHOR_NAME='Jane Doe' git commit`) and early-returned ALLOW before the
    parser ran. Detection is now the parser alone, which tokenizes the quoted value correctly."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(f"{prefix} git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, prefix
    assert _decision(out) == "block"


# ── (B) HIGH — a GLUED command separator must still scope the gate to the real commit ────────

@pytest.mark.parametrize("command", [
    "make test;git commit -m x",                # `;` glued to `test` (no surrounding spaces)
    ":;git commit -m x",                        # the classic null-command bypass attempt
    "true&&git commit -m x",                    # `&&` glued
    "false||git commit -m x",                   # `||` glued
    "do_thing|git commit -m x",                 # `|` glued
    "git rebase --abort&&git commit -m x",      # skip flag on a glued-sibling, commit is real
    "make |& git commit -m x",                  # bash pipe+stderr operator `|&`
    "make|&git commit -m x",                    # `|&` glued
    "do_case ;& git commit -m x",               # case fall-through `;&`
])
def test_glued_separator_commit_is_still_gated(command, tmp_path, monkeypatch):
    """`shlex.split` (default) keeps a glued `;`/`&&`/`|` welded to the adjacent word, so the
    commit segment after it was invisible → the gate was bypassed (a regression vs. the old broad
    regex, and a hole in fix B: `:;git commit` skipped review). The tokenizer now splits glued
    separators, so the real authoring commit after one is gated."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


def test_glued_separator_commit_with_review_skip_prefix_is_gated(tmp_path, monkeypatch):
    """REGRESSION: a glued-separator chain whose commit carries a `REVIEW_SKIP=1` inline env is
    still split correctly AND still gated — the inline env no longer opts out. Code staged, no
    marker → BLOCK."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("make build&&REVIEW_SKIP=1 git commit -m x",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_quoted_separator_in_message_is_not_a_split(tmp_path, monkeypatch):
    """A `;`/`&` inside a quoted commit MESSAGE must not be treated as a command separator — the
    whole thing is one commit, gated normally (here: code staged, no marker → block)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m 'fix; cleanup & polish'",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── (A) git stash / git worktree are NOT gated ───────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "git stash",
    "git stash push -m wip",
    "git worktree add ../wt -b feat",
    "git status",
    "git add -A",
])
def test_non_commit_git_ops_not_gated(command, tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0, command
    assert _decision(out) == "allow"


# ── non-commit / fail-open basics ────────────────────────────────────────────────────────

def test_allow_non_commit_command(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("ls -la", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_fail_open_when_cwd_is_not_a_git_repo_blocks_only_on_missing_marker(tmp_path, monkeypatch):
    """git can't be queried for staged files → docs-only check is skipped; with no marker the
    gate still BLOCKs (the marker check, not the diff, is the discipline signal)."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    out, _e, c = _run("git commit -m x", not_a_repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_marker_stat_error_fails_open(tmp_path, monkeypatch):
    """If the review marker exists but cannot be stat'd (OSError), the gate fails OPEN (allow) —
    a broken stat must never wedge committing (on_error=open). Covers the `_marker_is_fresh()→None`
    branch."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")

    class _Boom:
        def exists(self):
            return True

        def stat(self):
            raise OSError("simulated stat failure")

    monkeypatch.setattr(rr, "marker_path", lambda: _Boom())
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(repo), "args": {"command": "git commit -m x"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("REVIEW_SKIP", raising=False)
    assert rr.main() == 0 and _decision(out.getvalue()) == "allow"


def test_unparsable_event_fails_open(monkeypatch):
    """A non-JSON stdin event → allow (fail-open), never a crash that wedges committing."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert rr.main() == 0 and _decision(out.getvalue()) == "allow"


def test_prose_git_commit_is_not_a_commit(tmp_path, monkeypatch):
    """`echo "remember to git, then commit"` is not a commit invocation → allow."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run('echo "remember to git, then commit"', repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_git_with_global_flags_commit_is_still_gated(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(f"git -C {repo} commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_absolute_path_git_commit_is_still_gated(tmp_path, monkeypatch):
    """`/usr/bin/git commit` (absolute path to the git binary, common in agent envs) must be
    recognized as a commit and gated — the executable BASENAME is `git`."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("/usr/bin/git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_non_git_executable_named_like_git_is_not_gated(tmp_path, monkeypatch):
    """`mygit commit` / `git-foo commit` are NOT the git binary → not gated (basename != `git`)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    for cmd in ("mygit commit -m x", "git-foo commit -m x"):
        out, _e, c = _run(cmd, repo, monkeypatch, marker=tmp_path / "m")
        assert c == 0 and _decision(out) == "allow", cmd


def test_git_dash_c_global_value_flag_commit_is_still_gated(tmp_path, monkeypatch):
    """`git -c user.name=x commit` — a `-c key=val` global flag (value-taking) must be walked past
    to reach the `commit` subcommand, and the commit still gated."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git -c user.name=x commit -m y", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_unbalanced_quotes_fail_open(tmp_path, monkeypatch):
    """A tokenization failure (unbalanced quote) → `_commit_segment` returns None → allow (the gate
    fails OPEN; it is process discipline, not a security boundary)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m 'unterminated", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── the PARSER (not the regex) is authoritative: `commit`-as-substring must NOT false-block ──

@pytest.mark.parametrize("command", [
    "git stash push -m 'will commit later'",   # `commit` lives in a stash MESSAGE, not a subcmd
    "git config commit.gpgsign true",          # `commit.` setting key, not the commit subcommand
    "git help commit",                         # the help TOPIC is `commit`
    "git commit-graph write",                  # a different subcommand that starts `commit-`
    "git commit-tree HEAD^{tree}",             # ditto
    "git log --grep commit",                   # `commit` is a grep pattern
    "git branch commit-wip",                   # a branch literally named `commit-wip`
])
def test_commit_substring_in_non_commit_subcommand_is_not_gated(command, tmp_path, monkeypatch):
    """The old code used `re.search(r'\\bcommit\\b')` as the AUTHORITY, so any `git …` line that
    merely CONTAINED the word `commit` (a stash message, a config key, `git help commit`,
    `commit-graph`/`commit-tree`) got BLOCKED. The parser scopes to the real `commit` subcommand,
    so these innocent commands are allowed even with code staged and no marker."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0, command
    assert _decision(out) == "allow"


def test_commit_segment_unit_rejects_non_commit_subcommands():
    assert rr._commit_segment("git stash push -m 'commit later'") is None
    assert rr._commit_segment("git config commit.gpgsign true") is None
    assert rr._commit_segment("git help commit") is None
    assert rr._commit_segment("git commit-graph write") is None
    assert rr._commit_segment("git commit -m x") is not None


# ── (B) SECURITY — a skip token in a comment / message / pathspec / sibling must NOT bypass ──

@pytest.mark.parametrize("command", [
    "git commit -m x # --abort",                   # skip token in a trailing shell comment
    "git commit -m 'x' # leftover --skip note",    # comment after a quoted message
    "git commit -m 'support --skip in messages'",  # skip token inside the commit message
    "git commit -am 'fix --continue handling'",    # -am clusters; value carries --continue
    "git commit -am --skip",                       # -am clusters; the VALUE is literally `--skip`
    "git commit -aF --abort",                      # -aF clusters; the file-path value is `--abort`
    "git commit -- --skip",                        # `--skip` is a PATHSPEC after `--`, not a flag
    "git commit -m x -- --abort src/",             # pathspec named --abort after `--`
    "git rebase --abort && git commit -m x",       # skip flag on a SIBLING command, not the commit
])
def test_skip_token_in_comment_or_message_does_not_bypass(command, tmp_path, monkeypatch):
    """codex/#20 bypass: ``SKIP_COMMIT`` used to match the RAW string, so a normal commit could
    skip the REVIEW gate by putting ``--abort``/``--skip`` in shell text Git never runs (a
    trailing comment), inside the commit message, after ``--`` (a pathspec), or on a sibling
    command. The skip exemption now derives from the PARSED commit argv, so these are real
    authoring commits → BLOCK when code is staged with no marker."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


@pytest.mark.parametrize("command", [
    "git commit --continue",
    "git commit --abort",
    "git commit --skip",
    "git rebase --continue && git commit --continue",  # the COMMIT segment carries --continue
])
def test_real_skip_flag_still_exempt_after_parsing(command, tmp_path, monkeypatch):
    """The fix must not over-block: a genuine skip flag in the real commit argv (not a comment) is
    still exempt even with code staged and no marker."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0, command
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "FOO=a#b git commit -m x",              # `#` glued mid-token (env value) — bash: NOT a comment
    "git -c user.name=a#b commit -m x",     # `#` glued in a -c value
    "git commit -m x#y",                    # `#` glued into the message value
])
def test_glued_hash_is_not_a_comment_and_commit_is_gated(command, tmp_path, monkeypatch):
    """A `#` welded into a token (no preceding space) is a LITERAL in the shell, not a comment.
    shlex's built-in commenter would cut the line at any `#`, dropping the `git commit` from the
    stream → a silent bypass. The manual word-boundary comment handling keeps the commit visible,
    so it is gated (codex)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


def test_multiline_add_then_commit_is_gated(tmp_path, monkeypatch):
    """The dominant agent pattern — `git add` then `git commit` on SEPARATE lines — must be gated.
    A newline is a command separator; without per-line handling shlex folds both onto one segment
    (`git add … git commit …`, subcommand `add`) and the commit vanishes from the gate (codex
    HIGH). Code staged, no marker → block."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git add -A\ngit commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_comment_on_earlier_line_does_not_hide_later_commit(tmp_path, monkeypatch):
    """A `#` comment ends at END-OF-LINE, not end-of-command — a comment on an earlier line must
    not swallow a `git commit` on a LATER line (codex MEDIUM). Code staged, no marker → block."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("make build  # build first\ngit commit -am x", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_multiline_docs_only_commit_allowed(tmp_path, monkeypatch):
    """A multi-line script whose only commit is docs-only still gets the fast-path → allow."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("echo building\ngit commit -m 'update docs'", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_multiline_quoted_message_does_not_break_detection(tmp_path, monkeypatch):
    """A commit-message quote that spans newlines must not defeat detection (the per-line split
    falls back to whole-command tokenization). Code staged, no marker → block."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m 'line one\nline two'", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_command_before_a_multiline_quoted_commit_is_gated(tmp_path, monkeypatch):
    """The hard case: an EARLIER command on line 1, then a commit whose `-m` quote spans newlines.
    A naive whole-blob fallback would fold `echo`+`git commit` into one `echo` segment and lose the
    commit; the chunk-rejoin keeps newline boundaries → the commit is detected and gated (codex)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("echo building\ngit commit -m 'line one\nline two'",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_comment_then_multiline_quoted_commit_is_gated(tmp_path, monkeypatch):
    """Comment on line 1 + a multiline-quoted commit on line 2 → still gated (codex)."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("make build  # note\ngit commit -m 'a\nb'", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("env", ["GIT_DIR=/elsewhere/.git", "GIT_WORK_TREE=/elsewhere",
                                 "GIT_INDEX_FILE=/tmp/idx", "GIT_DIR=/e/.git GIT_WORK_TREE=/e"])
def test_inline_git_repo_env_forfeits_docs_fastpath(env, tmp_path, monkeypatch):
    """Inline `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` redirect which repo/index the commit hits,
    just like `--git-dir` — so the docs-only fast-path (which queries the cwd index) is forfeited.
    A docs-only cwd index must still gate (codex MEDIUM)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")  # docs-only in cwd
    out, _e, c = _run(f"{env} git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, env
    assert _decision(out) == "block"


def test_glued_dash_F_with_letter_a_keeps_docs_fastpath(tmp_path, monkeypatch):
    """`git commit -Fchanges.txt` is a glued `-F` file flag (its value contains `a`); it must NOT
    be misread as a `-a` cluster, so a docs-only commit keeps the fast-path → allow (codex LOW)."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git commit -Fmsg.txt", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_crlf_multiline_add_then_commit_is_gated(tmp_path, monkeypatch):
    """CRLF line endings must be normalized — `git add -A\\r\\ngit commit` must still detect the
    commit (a `commit\\r` token would otherwise not match the subcommand) (codex). Gated."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git add -A\r\ngit commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_backslash_newline_continuation_is_joined(tmp_path, monkeypatch):
    """A trailing backslash continues the line — `git add -A \\\\<newline> && git commit` is one
    logical command and the commit is gated."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git add -A \\\n  && git commit -m x", repo, monkeypatch,
                      marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_trailing_real_comment_is_still_stripped(tmp_path, monkeypatch):
    """A genuine trailing comment (`# …` at a word boundary) is still dropped, so a docs commit
    with a comment is allowed and a skip token in the comment does not exempt."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run("git commit -m x   # ship it", repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_git_dir_flag_forfeits_docs_fastpath(tmp_path, monkeypatch):
    """`git --git-dir=… --work-tree=… commit` aims at a repo other than the one `git diff --cached`
    would query in cwd, so the docs-only fast-path is forfeited — even a docs-only cwd index gates
    (codex MEDIUM). cwd repo is docs-only; the commit still BLOCKs with no marker."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")  # docs-only index in cwd
    out, _e, c = _run(f"git --git-dir={repo}/.git --work-tree={repo} commit -m x",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_git_dir_after_value_flag_still_forfeits_fastpath(tmp_path, monkeypatch):
    """`--git-dir` AFTER a value-flag (`-c foo=bar --git-dir=…`) must still be detected — the scan
    has to walk past `-c`'s value, not stop at it (codex). A docs-only cwd index still gates."""
    repo = _mk_repo_with_staged(tmp_path, "README.md")
    out, _e, c = _run(f"git -c foo=bar --git-dir={repo}/.git commit -m x",
                      repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_uses_alt_repo_unit():
    f = lambda cmd: rr._commit_segment(cmd).alt_repo  # noqa: E731
    assert f("git -c foo=bar --git-dir=/g commit -m x") is True
    assert f("git --work-tree=/w commit -m x") is True
    assert f("git --git-dir /g commit -m x") is True
    assert f("git -C sub --git-dir=/g commit -m x") is True
    assert f("git -C a -C b commit -m x") is True   # >1 -C: _git_dir_flag sees only the first
    assert f("git -Ca -Cb commit -m x") is True
    assert f("git -c a.b=c commit -m x") is False
    assert f("git -C only commit -m x") is False    # a single -C is classified by target, not alt
    assert f("git commit -m x") is False


# ── direct unit coverage of the parser helpers (B) ───────────────────────────────────────

def test_is_skip_commit_unit():
    assert rr.is_skip_commit("git commit --abort") is True
    assert rr.is_skip_commit("git commit -m 'has --abort in msg'") is False
    assert rr.is_skip_commit("git commit -am --skip") is False     # --skip is the -am value
    assert rr.is_skip_commit("git stash") is False                 # not a commit segment
    assert rr.is_skip_commit("git rebase --abort && git commit -m x") is False
    # a skip token in ANOTHER value-flag's VALUE must not be read as a real skip flag (codex)
    assert rr.is_skip_commit("git commit --author '--skip' -m x") is False
    assert rr.is_skip_commit("git commit --date '--abort' -m x") is False
    assert rr.is_skip_commit("git commit -C --continue") is False  # -C value is --continue
    assert rr.is_skip_commit("git commit --fixup '--skip'") is False
    assert rr.is_skip_commit("git commit --author=--skip -m x") is False  # glued author value


@pytest.mark.parametrize("command", [
    "git commit --author '--skip' -m x",     # skip token in --author VALUE
    "git commit --date '--abort' -m x",      # in --date value
    "git commit -C --continue -m x",         # in -C value
    "git commit --fixup '--skip' -m x",      # in --fixup value
    "git commit --author=--abort -m x",      # glued --author= value
])
def test_skip_token_in_value_flag_value_does_not_bypass(command, tmp_path, monkeypatch):
    """A skip token (`--skip`/`--abort`/`--continue`) carried as the VALUE of a non-message
    value-flag (`--author`, `--date`, `-C`, `--fixup`) must NOT be read as a real skip flag and
    exempt the commit — same bypass class as the comment/message tokens (codex). Code staged, no
    marker → BLOCK."""
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


# ── external Telegram hatch escalation (replaces the OLD self-service per-commit skip) ─────
#
# When a real authoring commit has no fresh marker and is not docs-only, the block is consulted
# against RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT: unset → block with a how-to; a bare
# `1` → deny WITHOUT contacting Telegram; a written justification runs the trusted tg-ctl and
# allows on exit 0 / blocks (citing "hatch escalation denied") on nonzero. Mirrors
# tests/test_pin_primary_worktree.py.
_HATCH_ENV = "RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT"


def test_hatch_unset_blocks_with_howto(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m")
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert _HATCH_ENV in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` is an invalid request: deny, and NO tg-ctl is spawned (the fake would touch a
    marker file — it must not exist)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(rr.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m",
                      env={_HATCH_ENV: "1"})
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    question = tmp_path / "question.txt"
    injected = tmp_path / "pwned-env"
    justification = f'Need "hotfix"; pipe | amp & redir < > backtick `date`; $(touch {injected}); review is $pending.'
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f'cat > "{question}"\n'
        f"touch {marker}\n" + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(rr.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m",
                      env={_HATCH_ENV: justification})
    assert c == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert not injected.exists()
    assert justification in json.loads(question.read_text())["question"]


def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The pre-bash hook must parse the documented inline hatch prefix from the command string."""
    asked = tmp_path / "asked"
    question = tmp_path / "question.txt"
    injected = tmp_path / "pwned"
    justification = f'Need "hotfix"; pipe | amp & redir < > backtick `date`; $(touch {injected}); review is $pending.'
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f'cat > "{question}"\n'
        f"touch {asked}\n" + _ALLOW_REPLY_SH,
    )
    clean_home = tmp_path / "_clean_home"
    clean_home.mkdir()
    monkeypatch.setattr(rr.hatch_escalation, "resolve_home", lambda: str(clean_home))
    monkeypatch.setattr(rr.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    command = (
        f"RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT={shlex.quote(justification)} "
        "git commit -m x"
    )
    out, _e, c = _run(command, repo, monkeypatch, marker=tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"
    assert asked.exists()
    assert not injected.exists()
    assert justification in json.loads(question.read_text())["question"]


def test_hatch_justification_exit1_blocks_citing_denied(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(rr.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/util.py")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, marker=tmp_path / "m",
                      env={_HATCH_ENV: "Need to land a hotfix; review is mid-run."})
    assert c == rr.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
