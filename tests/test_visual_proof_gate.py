"""Tests for the visual-proof-gate agent-hook (pre-bash, commit gate).

Covers the doctrine's four cases. Hook 5 has no subagent exemption; the third case is the
SATISFIED-MARKER path (a fresh "looked at a screenshot" marker => allow). So:
  BLOCK   — a commit with staged user-visible files and no fresh marker.
  ALLOW   — a commit with NO user-visible files staged (nothing to prove).
  MARKER  — staged visual files but a fresh proof marker => allow.
  ESCAPE  — env+reason and inline sentinel allow; reasonless still blocks.

Hermetic: a real tiny git repo is created in tmp_path so the hook's own `git diff --cached
--name-only` subprocess runs for real (no monkeypatching the lister); the proof-marker dir is
redirected into tmp_path. The `git diff` fail-OPEN path is also tested with a non-repo cwd.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_visual_proof_gate.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "visual-proof-gate"
    / "visual_proof_gate.py"
)
_spec = importlib.util.spec_from_file_location("visual_proof_gate", _HOOK)
assert _spec and _spec.loader
vpg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vpg)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=30)


def _mk_repo_with_staged(tmp_path: Path, *files: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        _git(repo, "add", rel)
    return repo


def _run(command, cwd, monkeypatch, *, proof_dir: Path,
         env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(cwd), "args": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(vpg, "PROOF_DIR", proof_dir)
    monkeypatch.delenv("RIG_HATCH_REQUEST_VISUAL_PROOF_GATE", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = vpg.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


def _touch_proof(proof_dir: Path) -> None:
    """The OLD, now-invalid contract: a blind, content-free touch. Kept as a helper because it
    is exactly the shape agent-tools#475's regression tests need to prove no longer works."""
    proof_dir.mkdir(parents=True, exist_ok=True)
    (proof_dir / "looked").write_text("x")


def _write_manual_marker(proof_dir: Path, repo: Path, *, name: str = "looked") -> Path:
    """A correctly-scoped FALLBACK marker: content is `json.dumps(repo_toplevel)` — the shape
    `--write-marker` / `_manual_marker_satisfies` expect (round 10: JSON-encoded, not a raw
    line, so an embedded newline or non-UTF-8 byte in the path round-trips unambiguously)."""
    proof_dir.mkdir(parents=True, exist_ok=True)
    top = str(Path(subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()).resolve())
    marker = proof_dir / name
    marker.write_text(json.dumps(top))
    return marker


def _staged_diff_sha256(repo: Path) -> str:
    """The exact hash `_staged_diff_hash`/dev-cli's `staged_diff_hash` compute, for building a
    genuine (or deliberately stale) attestation in tests."""
    import hashlib as _hashlib
    proc = subprocess.run(["git", "-C", str(repo), "diff", "--cached"],  # noqa: S603,S607
                          capture_output=True, timeout=10, check=True)
    return _hashlib.sha256(proc.stdout).hexdigest()


def _write_attestation(
    proof_dir: Path, repo: Path, *, staged_sha256: str | None = None,
    repo_override: str | None = None, name: str | None = None,
    worktree_dirty: object = False, capture_sha256: str | None = None,
    write_capture_file: bool = True, omit_fields: tuple[str, ...] = (),
    version: object = 2, tool: object = "dev shot",
) -> Path:
    """A `dev shot`-shaped attestation JSON. Defaults to a GENUINE, fully-valid record (real
    repo toplevel, real current staged-diff hash, worktree_dirty=False, version 2, tool "dev
    shot", and a real on-disk capture file whose sha256 matches `capture_sha256`) — every knob
    below exists to build a deliberately INVALID variant for the negative tests:
    `staged_sha256`/`repo_override` for a repo/diff mismatch, `worktree_dirty=True`/`None` for
    a dirty-worktree-at-capture-time record, `capture_sha256=`/`write_capture_file=False` for a
    forged-without-a-real-file record, `version=`/`tool=` for a foreign/legacy-producer record,
    `omit_fields` for a record missing a required key entirely."""
    import hashlib as _hashlib
    proof_dir.mkdir(parents=True, exist_ok=True)
    top = repo_override or str(Path(subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()).resolve())
    capture_path = proof_dir / f"shot-{int(time.time() * 1000)}.png"
    capture_bytes = b"not a real png, just needs a stable hash for the test"
    if write_capture_file:
        capture_path.write_bytes(capture_bytes)
    record = {
        "version": version,
        "tool": tool,
        "captured_at": int(time.time()),
        "repo": top,
        "staged_sha256": staged_sha256 if staged_sha256 is not None else _staged_diff_sha256(repo),
        "worktree_dirty": worktree_dirty,
        "capture_path": str(capture_path),
        "capture_sha256": (
            capture_sha256 if capture_sha256 is not None
            else _hashlib.sha256(capture_bytes).hexdigest()
        ),
        "url": "http://localhost:5173/",
    }
    for field in omit_fields:
        record.pop(field, None)
    marker = proof_dir / (name or f"attest-{int(time.time() * 1000)}.json")
    marker.write_text(json.dumps(record))
    return marker


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


# ── EFFECTIVE_CWD (cross-repo `cd`/`-C` resolution) ────────────────────────────────────
#
# `effective_cwd` decides WHICH repo's staged files get checked when the commit command
# itself changes directory first (`cd other-repo && git commit`) or passes git a `-C`. Unit
# tests below exercise the parser directly; the integration test at the bottom proves the
# end-to-end effect with two REAL git repos (the actual bug class this function fixes: a
# session rooted in repo A committing into repo B was graded against A's staged files).

def test_effective_cwd_plain_absolute_cd(tmp_path):
    cmd = "cd /abs/other-repo && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other-repo"


def test_effective_cwd_relative_cd_joins_session_cwd(tmp_path):
    cmd = "cd sub/dir && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path / "sub" / "dir")


def test_effective_cwd_tilde_cd_expands_home(tmp_path, monkeypatch):
    """Regression: `cd ~/repo` used to glue the literal `~` onto session_cwd
    (`<session_cwd>/~/repo`, a path that can't exist), which made the hook's own
    `git -C <bogus> diff --cached` fail and silently fail the WHOLE gate open. The shell
    expands `~` before `cd` ever sees it — this hook must mimic that, not treat it as a
    plain relative path segment."""
    monkeypatch.setenv("HOME", "/Users/fakehome")
    cmd = "cd ~/repos/other && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/Users/fakehome/repos/other"


def test_effective_cwd_quoted_cd_path(tmp_path):
    cmd = 'cd "/abs/other repo" && git commit -m x'
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other repo"


@pytest.mark.parametrize("cmd", [
    "(cd /abs/other-repo && git commit -m x)",
    "( cd /abs/other-repo && git commit -m x )",
])
def test_effective_cwd_parenthesized_subshell_cd(cmd, tmp_path):
    """Regression (PR #176 review finding, agent-tools#201): the common subshell idiom
    `(cd <worktree> && git commit ...)` — used so the `cd` doesn't leak into the caller's
    shell — used to defeat `cd`-detection entirely. `shlex.split` tokenizes the opening `(`
    two different ways depending on whether it has a following space:
      - glued, no space (`(cd repoB ...)`)  -> one token: `"(cd"`, never bare `"cd"`
      - spaced (`( cd repoB ... )`)         -> `"("` as its own standalone token before `"cd"`
    Before the fix, `seg[0] == "cd"` matched neither shape, so `effective_cwd` fell back to
    `session_cwd` instead of the real target repo — silently checking the wrong repo's
    staged files. Both shapes must resolve to the actual `cd` target."""
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other-repo"


@pytest.mark.parametrize("cmd", [
    "( cd /abs/other-repo && true ) ; git commit -m x",
    "(cd /abs/other-repo) && git commit -m x",
    "(cd /abs/other-repo && true); git commit -m x",
])
def test_effective_cwd_subshell_cd_that_closes_before_the_commit_is_not_trusted(cmd, tmp_path):
    """Regression (PR #176 review finding on the subshell fix itself, agent-tools#201): a
    `(...)` subshell forks a CHILD process — a `cd` inside it never persists once the
    subshell's own `)` closes. `(cd repoB && true) ; git commit -m x` (do something in
    repoB, then come back and commit in the ORIGINAL directory — a realistic idiom for
    exactly the reason a subshell is used at all) must resolve to `session_cwd`, not
    `repoB`: naively trusting any `(`-recognized `cd` regardless of whether its subshell
    already closed would resolve to a real, existing, but IRRELEVANT other repo instead of
    the one the commit actually lands in — the very failure class this function exists to
    prevent, self-inflicted by over-trusting a closed subshell."""
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_effective_cwd_group_command_cd_persists_past_its_closing_brace(tmp_path):
    """Contrast case: unlike `(...)`, a `{ ...; }` GROUP command runs in the CURRENT shell
    (no fork) — a `cd` inside it genuinely persists past the closing `}`. Proves the fix
    distinguishes `(` (subshell, vetoed once closed) from `{` (group command, trusted
    regardless) rather than blanket-distrusting any closed grouping construct."""
    cmd = "{ cd /abs/other-repo ; } ; git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other-repo"


def test_effective_cwd_closed_subshell_cd_does_not_veto_a_later_dash_c_on_the_commit(tmp_path):
    """Regression (review finding, round 2, on the closed-subshell veto itself): an EARLIER,
    already-closed decoy subshell `cd` must not discard a LATER, legitimately-resolved `-C`
    on the commit's own invocation. `(cd /decoy && true) ; git -C /target commit -m x` must
    resolve to `/target` — the decoy `cd` is correctly distrusted (its subshell already
    closed), but that distrust must reset to session_cwd and let the commit's own `-C`
    resolve normally from there, not blanket-veto the whole resolution to session_cwd and
    discard a `-C` that has nothing to do with the closed subshell."""
    cmd = "(cd /decoy && true) ; git -C /target commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/target"


def test_effective_cwd_unrelated_sibling_subshell_cannot_resurrect_a_closed_cd(tmp_path):
    """Regression (review finding, round 2): a plain open/closed check at the commit boundary
    can be fooled by an UNRELATED sibling subshell reopening to the SAME nesting depth after
    the real one (with the trusted `cd`) already closed. `(cd /decoy && true) ; (echo ok &&
    git commit -m x)` — the first subshell (containing the `cd`) closes; a wholly separate
    second subshell (no `cd` inside it at all) happens to open before the commit, landing at
    the same depth. Must resolve to session_cwd, not `/decoy` — the running-minimum-depth
    check (not just the depth value right before the commit) is what tells these apart."""
    cmd = "(cd /decoy && true) ; (echo ok && git commit -m x)"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_effective_cwd_git_dash_c_flag(tmp_path):
    cmd = "git -C /abs/other-repo commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other-repo"


def test_effective_cwd_last_dash_c_wins_when_absolute(tmp_path):
    """Repeated `-C`, both absolute: the last one wins (each replaces `cur` outright since an
    absolute path short-circuits the join — chaining is moot here)."""
    cmd = "git -C /abs/a -C /abs/b commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/b"


def test_effective_cwd_relative_dash_c_chains_off_the_previous_one(tmp_path):
    """Regression (review finding): git chains repeated `-C` — each successive RELATIVE `-C`
    is resolved against the PREVIOUS one, not against the ambient session cwd directly.
    `git -C a -C b commit` runs in `<session_cwd>/a/b`, not `<session_cwd>/b`."""
    cmd = "git -C a -C b commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path / "a" / "b")


def test_effective_cwd_command_substitution_in_cd_target_leaves_cur_unchanged(tmp_path):
    """Regression (review finding, [High]): `shlex.split` never performs command
    substitution/variable expansion/globbing — `$(...)`, `` `...` ``, `$VAR`, and glob
    metacharacters all arrive here STILL LITERAL. Treating them as a literal directory name
    fabricates a path that (almost always) doesn't exist, which — the same failure shape as
    the tilde bug — made the hook's own `git -C <bogus> diff --cached` fail and the WHOLE
    gate fail open (silently skip the check) instead of at least falling back to
    session_cwd. `cur` must stay at session_cwd when the `cd` target can't be safely
    resolved, not be replaced by a fabricated bogus path."""
    cmd = 'cd "$(git rev-parse --show-toplevel)" && git commit -m x'
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("cmd", [
    "cd $HOME/proj && git commit -m x",
    "cd /abs/repo* && git commit -m x",
    "cd `pwd`/proj && git commit -m x",
    "cd /abs/repo[1] && git commit -m x",
])
def test_effective_cwd_other_unexpanded_shell_forms_leave_cur_unchanged(cmd, tmp_path):
    """Same regression class as above: `$VAR`, backticks, and glob metacharacters (`*`,
    `[...]`) are shell features this hook can't run — none of them should fabricate a bogus
    path that defeats the gate."""
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_cross_repo_command_substitution_cd_still_checks_session_cwd_not_bypassed(tmp_path, monkeypatch):
    """End-to-end version of the regression above: session_cwd itself has a staged .tsx; the
    command's `cd` target is an unexpanded `$(...)` this hook can't resolve. Must still
    BLOCK on session_cwd's own staged file — not silently ALLOW because the (unresolvable)
    cd target doesn't exist as a directory."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run('cd "$(git rev-parse --show-toplevel)" && git commit -m x', repo,
                      monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_effective_cwd_no_cd_or_dash_c_passes_through_session_cwd(tmp_path):
    """No `cd`/`-C` in the command at all → the only signal available is session_cwd
    itself; effective_cwd must not invent a different directory."""
    cmd = "git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_effective_cwd_unparseable_command_falls_back_to_session_cwd(tmp_path):
    """An unbalanced quote defeats shlex entirely → best-effort fallback to session_cwd
    (fail open, matching the rest of the hook's error philosophy), not a crash."""
    cmd = "cd /abs/other-repo && git commit -m 'unterminated"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("cmd", [
    'git -C /other-repo commit -m x && git commit -m "feat: Button"',
    'git commit -m x && git -C /other-repo commit -m y',
])
def test_effective_cwd_two_commits_in_one_command_falls_back_to_session_cwd(cmd, tmp_path):
    """Regression (review finding, [High]): a command chaining TWO commit invocations is
    ambiguous — resolving to just the FIRST one's directory would let a harmless-looking
    decoy commit (`git -C /decoy commit -m x && git commit -m real`) hide a second, REAL
    commit that lands somewhere this function returns nothing for. When there isn't exactly
    ONE commit segment, fall back to session_cwd (the same safe default this function
    refines for the unambiguous, single-commit case) rather than guess."""
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_decoy_first_commit_cannot_hide_a_real_second_commit(tmp_path, monkeypatch):
    """End-to-end version of the regression above: session_cwd has a staged .tsx; the
    command commits into an EMPTY decoy repo first, then commits again (for real) in
    session_cwd. Must BLOCK on session_cwd's own staged file — a decoy first commit must not
    make the gate check (and clear on) the wrong repo."""
    (tmp_path / "real").mkdir()
    (tmp_path / "decoy").mkdir()
    repo = _mk_repo_with_staged(tmp_path / "real", "src/Button.tsx")
    decoy = _mk_repo_with_staged(tmp_path / "decoy")
    out, _e, c = _run(f'git -C {decoy} commit -m x && git commit -m "feat: Button"', repo,
                      monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.parametrize("cmd_template", [
    "cd {bad} ; git commit -m x",
    "cd {bad} || git commit -m x",
])
def test_cd_to_nonexistent_dir_before_semicolon_or_or_falls_back_to_session_cwd(
    cmd_template, tmp_path, monkeypatch,
):
    """Regression (review finding, [High]): a real shell keeps running the commit in the
    ORIGINAL directory when a preceding `cd` FAILS — `;` doesn't care about `cd`'s exit
    status, and `||` runs its right side only because `cd` failed. `effective_cwd` has no
    shell to actually run `cd` in, so it can't know the target doesn't exist; it resolves to
    that (nonexistent) directory regardless. Before this fix, `staged_files()` on a
    nonexistent directory returned None and the WHOLE gate failed open — letting a real,
    unproven commit of a staged .tsx in session_cwd slip through silently. The fix
    (`_resolve_staged_files`) falls back to checking session_cwd when the resolved `cwd`
    isn't queryable, so this must still BLOCK."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    bad_dir = tmp_path / "does-not-exist"
    out, _e, c = _run(cmd_template.format(bad=bad_dir), repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_cd_to_existing_non_repo_dir_does_not_fall_back_stays_pure_fail_open(tmp_path, monkeypatch):
    """Regression (review finding, [Medium]): the session_cwd fallback is scoped to a `cd`
    target that DOESN'T EXIST — the one case we can be sure a real shell's `cd` would also
    have failed. If the target DOES exist (just isn't a git repo), a real `cd` would have
    SUCCEEDED, so `effective_cwd`'s resolution is trustworthy — a `staged_files` failure
    there is a genuine 'can't query this repo' situation and must stay pure fail-open
    (ALLOW), not substitute an unrelated repo's (session_cwd's) staged files. session_cwd
    HAS a staged .tsx here, but must NOT be consulted — proving the fallback doesn't fire
    for this case."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    real_non_repo_dir = tmp_path / "plain-dir"
    real_non_repo_dir.mkdir()
    out, _e, c = _run(f"cd {real_non_repo_dir} ; git commit -m x", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("sep", ["||", "|", "&"])
def test_effective_cwd_cd_gated_behind_unreliable_sep_is_not_trusted(sep, tmp_path):
    """Regression (review finding, [High]): a `cd` preceded by `||` only runs when an
    EARLIER command FAILED — if the commit after it actually executes, that `cd` did NOT
    succeed, so trusting its target would be backwards. A `cd` preceded by `|`/`&` runs (if
    at all) in a SUBSHELL whose cwd change never reaches the parent shell. Trusting either
    would resolve to a real, existing, but IRRELEVANT repo instead of session_cwd (where the
    commit actually lands). Must abort to session_cwd."""
    cmd = f"somecmd {sep} cd /real-other-repo ; git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("sep", ["&&", ";"])
def test_effective_cwd_cd_preceded_by_and_or_semicolon_is_trusted(sep, tmp_path):
    """`&&` and `;` both run the next command in the SAME shell process, and if the eventual
    commit runs at all via an unbroken `&&` chain, every link up to it (including the `cd`)
    necessarily succeeded — this is the common, benign `setup && cd target && git commit`
    shape, which MUST resolve to the real target, not fall back to session_cwd."""
    cmd = f"somecmd {sep} cd /real-other-repo ; git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/real-other-repo"


def test_common_setup_and_cd_and_commit_chain_resolves_to_the_real_target(tmp_path, monkeypatch):
    """End-to-end: the overwhelmingly common `mkdir -p target && cd target && git commit`
    (or `git worktree add target && cd target && git commit`) shape MUST resolve to the real
    target directory — session_cwd has a staged .tsx that is IRRELEVANT to this commit, and
    must NOT be what gets checked. This is the primary case the whole cwd-detection fix
    exists for; treating `&&` as untrustworthy would break it."""
    (tmp_path / "session").mkdir()
    (tmp_path / "target").mkdir()
    session_repo = _mk_repo_with_staged(tmp_path / "session", "src/Button.tsx")
    target = _mk_repo_with_staged(tmp_path / "target")
    out, _e, c = _run(f"mkdir -p sub && cd {target} && git commit -m x", session_repo,
                      monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_known_gap_conditionally_gated_cd_via_and_into_existing_decoy_repo(tmp_path, monkeypatch):
    """KNOWN, ACCEPTED GAP (agent-tools#173), pinned rather than silently left undocumented:
    `false && cd <a REAL, existing decoy repo> ; git commit -m x` — a real shell never runs
    that `cd` (the predecessor `false` fails, short-circuiting the `&&`), so the commit
    actually lands in session_cwd. Because `&&` is (deliberately) trusted here — it's what
    makes the common `setup && cd target && git commit` pattern work at all — this resolves
    to the decoy instead of session_cwd. This test PINS that documented trade-off (ALLOW,
    not the theoretically-ideal BLOCK) so a future change to this behavior is a deliberate,
    reviewed decision, not an accidental regression in either direction."""
    (tmp_path / "real").mkdir()
    (tmp_path / "decoy").mkdir()
    repo = _mk_repo_with_staged(tmp_path / "real", "src/Button.tsx")
    decoy = _mk_repo_with_staged(tmp_path / "decoy")
    out, _e, c = _run(f"false && cd {decoy} ; git commit -m x", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.parametrize("sep", ["|", "&"])
def test_effective_cwd_leading_cd_followed_by_pipe_or_background_is_not_trusted(sep, tmp_path):
    """Regression (review finding, [High]): a LEADING `cd` (trusted by the preceding-
    separator check — it's the first segment) can still be untrustworthy if IT ITSELF is
    followed by `|` or `&` — `cd /x | git commit` puts the `cd` in a pipeline's subshell,
    and `cd /x & git commit` backgrounds it; either way the `cd`'s cwd change never reaches
    the shell that actually runs the following `git commit`, which lands in session_cwd
    instead. The preceding-separator check alone (only looking backward) misses this."""
    cmd = f"cd /real-other-repo {sep} git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_leading_cd_piped_into_existing_decoy_repo_cannot_bypass_the_gate(tmp_path, monkeypatch):
    """End-to-end version of the regression above: session_cwd has a staged .tsx; the
    command is `cd <a REAL, empty, existing decoy repo> | git commit -m x`. A real shell
    runs that `cd` in a pipeline subshell — it never changes the cwd the commit actually
    runs in (session_cwd). Must BLOCK on session_cwd's own staged file, not ALLOW via the
    decoy the `cd` only reached in its own subshell."""
    (tmp_path / "real").mkdir()
    (tmp_path / "decoy").mkdir()
    repo = _mk_repo_with_staged(tmp_path / "real", "src/Button.tsx")
    decoy = _mk_repo_with_staged(tmp_path / "decoy")
    out, _e, c = _run(f"cd {decoy} | git commit -m x", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_effective_cwd_trailing_cd_after_commit_is_ignored(tmp_path):
    """Regression (review finding): a `cd` AFTER the commit segment runs only once the
    commit has already happened — it must NOT retroactively change which repo got resolved.
    `git commit -m x && cd /other-repo` commits in session_cwd, then merely changes the
    shell's cwd for whatever (if anything) comes next."""
    cmd = "git commit -m x && cd /other-repo"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_effective_cwd_dash_c_on_non_commit_invocation_does_not_persist(tmp_path):
    """Regression (review finding): `git -C <dir>` scopes only THAT one git invocation — it
    does not change the shell's cwd. A `-C` on a non-commit git call (`git -C /other status`)
    must not leak forward onto the later commit segment's own resolution."""
    cmd = "git -C /other status && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_effective_cwd_dash_c_relative_paths_do_not_chain_across_invocations(tmp_path):
    """Regression (review finding): each git invocation's `-C` is relative to the REAL shell
    cwd, not to a previous (non-persistent) git -C's value. `git -C a fetch && git -C b
    commit` must resolve `b` against session_cwd, not against `session_cwd/a`."""
    cmd = "git -C a fetch && git -C b commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path / "b")


def test_effective_cwd_cd_with_option_flag(tmp_path):
    """`cd -P /repo` (physical-path option) must resolve to `/repo`, not glue the `-P` flag
    itself onto session_cwd as if it were the target."""
    cmd = "cd -P /abs/other-repo && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other-repo"


def test_effective_cwd_cd_double_dash_then_literal_dir(tmp_path):
    cmd = "cd -- /abs/other-repo && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == "/abs/other-repo"


def test_effective_cwd_cd_dash_oldpwd_is_unresolvable_leaves_cur_unchanged(tmp_path):
    """`cd -` returns to `$OLDPWD`, which this stateless hook has no way to know — it must
    leave `cur` at session_cwd rather than guessing (e.g. treating `-` as a literal dirname)."""
    cmd = "cd - && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("second_cd", ['cd -', 'cd "$(pwd)"'])
def test_effective_cwd_unresolvable_cd_after_a_resolved_one_aborts_to_session_cwd(
    second_cd, tmp_path,
):
    """Regression (review finding, [Medium]): an unresolvable `cd` (bare `cd -`, or a target
    with an unexpanded shell metacharacter) means we've LOST TRACK of the real shell's cwd
    from here on — even if an EARLIER `cd` in the same chain resolved cleanly. `cd
    /clean-repo && cd - && git commit` really ends up back in `$OLDPWD` (session_cwd) in a
    real shell, NOT `/clean-repo` — leaving `cur` at the earlier, now-stale `/clean-repo`
    would check a directory the commit never actually targets. Must abort to session_cwd,
    not keep the stale prior resolution."""
    cmd = f"cd /clean-repo && {second_cd} && git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("second_cd", ['cd -', 'cd "$(pwd)"'])
def test_effective_cwd_unresolvable_target_via_trusted_semicolon_still_aborts(
    second_cd, tmp_path,
):
    """Same regression as above, but reaching the `resolved is None` branch specifically
    (not the trust-check branch): both `cd`s here are `;`-separated, so BOTH pass the
    trust check — the second one's target itself is what's unresolvable. Confirms the
    'lost track, abort to session_cwd' guard fires on target-unresolvability alone, not
    just as a side effect of the trust check."""
    cmd = f"cd /clean-repo ; {second_cd} ; git commit -m x"
    assert vpg.effective_cwd(cmd, str(tmp_path)) == str(tmp_path)


def test_cross_repo_cd_checks_the_cd_target_not_session_cwd(tmp_path, monkeypatch):
    """End-to-end: session_cwd is repo A (nothing visual staged); the command `cd`s into
    repo B, which HAS a staged .tsx. The gate must BLOCK on repo B's file, proving it
    resolved and checked the `cd` target — not repo A (the bug this hook exists to fix)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = _mk_repo_with_staged(tmp_path / "a", "README.md")
    repo_b = _mk_repo_with_staged(tmp_path / "b", "src/Button.tsx")
    out, _e, c = _run(f"cd {repo_b} && git commit -m x", repo_a, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Button.tsx" in payload["message"]


def test_cross_repo_cd_does_not_leak_session_cwds_staged_files(tmp_path, monkeypatch):
    """Inverse of the above: session_cwd (repo A) HAS a staged .tsx, but the command `cd`s
    into repo B, which has nothing visual staged. Must ALLOW — proves session_cwd's own
    staged files are NOT what gets checked once the command changes directory."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = _mk_repo_with_staged(tmp_path / "a", "src/Button.tsx")
    repo_b = _mk_repo_with_staged(tmp_path / "b", "README.md")
    out, _e, c = _run(f"cd {repo_b} && git commit -m x", repo_a, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_cross_repo_parenthesized_subshell_cd_checks_the_cd_target_not_session_cwd(
    tmp_path, monkeypatch,
):
    """End-to-end version of the parenthesized-subshell regression above: session_cwd is
    repo A (nothing visual staged); the command subshells a `cd` into repo B, which HAS a
    staged .tsx, via the exact exploit shape from PR #176's review (`(cd repoB && git
    commit -m x)`). Before the fix this ALLOWed — repo A (session_cwd) was checked instead
    of repo B, so the visual-proof gate was silently bypassed for the repo the commit
    actually lands in. Must BLOCK on repo B's staged file."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = _mk_repo_with_staged(tmp_path / "a", "README.md")
    repo_b = _mk_repo_with_staged(tmp_path / "b", "src/Button.tsx")
    out, _e, c = _run(f"(cd {repo_b} && git commit -m x)", repo_a, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Button.tsx" in payload["message"]


def test_cross_repo_closed_subshell_cd_cannot_hide_session_cwds_own_staged_file(
    tmp_path, monkeypatch,
):
    """End-to-end version of the closed-subshell regression: session_cwd (repo A) HAS a
    staged .tsx; the command subshells a `cd` into an EMPTY repo B, does something there,
    then closes the subshell and commits back in repo A via a bare `;` (`(cd repoB &&
    true) ; git commit -m x`). A naive fix that trusts ANY `(`-recognized `cd` regardless
    of whether its subshell already closed would resolve to repo B (empty, nothing staged)
    and wrongly ALLOW — hiding repo A's own real, unproven staged .tsx. Must BLOCK on repo
    A's staged file, proving the closed subshell's `cd` is NOT trusted here."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = _mk_repo_with_staged(tmp_path / "a", "src/Button.tsx")
    repo_b = _mk_repo_with_staged(tmp_path / "b")
    out, _e, c = _run(f"(cd {repo_b} && true) ; git commit -m x", repo_a, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Button.tsx" in payload["message"]


def test_cross_repo_unrelated_sibling_subshell_cannot_hide_session_cwds_own_staged_file(
    tmp_path, monkeypatch,
):
    """End-to-end version of the sibling-subshell regression (review finding, round 2):
    session_cwd (repo A) HAS a staged .tsx; the command subshells a `cd` into an EMPTY repo
    B, closes that subshell, then commits inside a wholly SEPARATE second subshell that
    never `cd`s anywhere (`(cd repoB && true) ; (echo ok && git commit -m x)`). A depth
    check that only compares the value right before the commit (rather than the running
    minimum since the `cd`) would be fooled by the second subshell reopening to the same
    nesting depth and wrongly ALLOW via repo B. Must BLOCK on repo A's staged file."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = _mk_repo_with_staged(tmp_path / "a", "src/Button.tsx")
    repo_b = _mk_repo_with_staged(tmp_path / "b")
    out, _e, c = _run(f"(cd {repo_b} && true) ; (echo ok && git commit -m x)", repo_a,
                      monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Button.tsx" in payload["message"]


def test_trailing_cd_after_commit_cannot_bypass_the_gate(tmp_path, monkeypatch):
    """Regression (review finding, [High]): appending `&& cd <somewhere-empty>` after the
    real commit must NOT let it dodge the gate. Before the fix, effective_cwd kept walking
    segments past the commit and re-pointed `cur` at the trailing `cd` target — here an
    UNSTAGED, non-repo directory — so `staged_files()` returned None (not a repo) and the
    hook failed open, waving through a real commit of an unproven staged .tsx."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    empty_dir = tmp_path / "not-a-repo"
    empty_dir.mkdir()
    out, _e, c = _run(f"git commit -m x && cd {empty_dir}", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── BLOCK ──────────────────────────────────────────────────────────────────────────────

def test_block_commit_with_staged_component_and_no_proof(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Button.tsx" in payload["message"]


def test_block_commit_with_staged_css_under_components_dir(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "components/card.css")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ALLOW (no user-visible files staged) ───────────────────────────────────────────────

def test_allow_commit_with_no_visual_files(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/util.ts", "README.md")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_allow_non_commit_command(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git status", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_fail_open_when_cwd_is_not_a_git_repo(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    out, _e, c = _run("git commit -m x", not_a_repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_repo_toplevel_survives_non_utf8_git_output(monkeypatch):
    """Regression, review round 8 (PR #484): `_repo_toplevel` used to capture git's stdout with
    `text=True`, which decodes using the strict error handler by default — a repo toplevel path
    containing bytes that aren't valid UTF-8 (a legal POSIX path; not reproducible via a real
    on-disk directory on this test machine's own filesystem, which requires valid UTF-8 names,
    so it's injected directly at the subprocess boundary instead) then raised an uncaught
    `UnicodeDecodeError` that escaped `_repo_toplevel`, `_resolved_repo_root`, and `main()`
    entirely. This hook's descriptor is `on_error: "open"`, so a CRASHED hook is an ALLOWED
    commit — the exact bypass class this whole file exists to close, reached via a decode
    error instead of a wrong field value. Must not raise; `os.fsdecode`'s `surrogateescape`
    handling degrades an exotic path to a merely-unmatchable string instead of crashing the
    hook."""

    class _FakeCompletedProcess:
        returncode = 0
        stdout = b"/tmp/repo-\xff\xfe-name"  # not valid UTF-8

    monkeypatch.setattr(vpg.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess())
    result = vpg._repo_toplevel("/tmp")  # must not raise UnicodeDecodeError
    assert isinstance(result, str) and result  # some surrogate-escaped string, not a crash


def test_repo_toplevel_preserves_trailing_whitespace_in_path(monkeypatch):
    """Regression, review round 9 (PR #484): `_repo_toplevel` used a blanket `.strip()` on
    git's stdout, which removes ALL leading/trailing whitespace, not just git's own trailing
    newline terminator. A repo path that legitimately ends in a space (rare but legal on
    POSIX) collided with the same path minus the space — `/tmp/space-repo ` and
    `/tmp/space-repo` both resolved to the identical `repo_root`, letting a marker written for
    one repo satisfy a completely different repo's unrelated commit. Fixed: strip only the
    trailing `\n` git always appends (`rstrip(b"\n")`), not generic whitespace."""

    class _FakeCompletedProcess:
        returncode = 0
        stdout = b"/tmp/space-repo \n"  # trailing space is part of the path, not padding

    monkeypatch.setattr(vpg.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess())
    result = vpg._repo_toplevel("/tmp")
    assert result is not None and result.endswith("space-repo ")


def test_repo_toplevel_strips_exactly_one_trailing_newline(monkeypatch):
    """Regression, review round 10 (PR #484): the round-9 fix replaced `.strip()` with
    `rstrip(b"\n")` — but `rstrip` removes EVERY trailing occurrence of the byte, not just
    git's single output terminator. A repo whose path itself legitimately ends in a newline
    (POSIX allows any byte but NUL and `/` in a filename) makes git emit `<path>\n\n` (the
    path's own trailing `\n` plus git's terminator); `rstrip(b"\n")` strips BOTH, collapsing
    `repo` and `repo\n` to the identical `repo_root` — the same collision class round 9 closed
    for trailing spaces, just one byte-count off. Fixed: slice off exactly one trailing `\n`,
    leaving any newline that's genuinely part of the path untouched."""

    class _FakeCompletedProcess:
        returncode = 0
        stdout = b"/tmp/newline-repo\n\n"  # the path's own "\n" + git's own terminator "\n"

    monkeypatch.setattr(vpg.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess())
    result = vpg._repo_toplevel("/tmp")
    assert result is not None and result.endswith("newline-repo\n")


def test_attestation_json_parse_survives_recursion_error(tmp_path, monkeypatch):
    """Regression, review round 10 (PR #484): a deeply-nested JSON `.json` marker (300,000
    nested arrays here — ~600KB, comfortably under `_MAX_MARKER_BYTES`'s 1MB cap; reliably
    triggers RecursionError on CPython's C-accelerated json scanner, where the exact threshold
    depth is interpreter/stack-state dependent — Codex's own finding cited ~100,000/200KB as
    triggering it, which this file picks a larger, more robust depth well above) makes
    `json.loads` raise `RecursionError`, a `RuntimeError` subclass NOT caught by the
    `except ValueError` this hook used around its attestation parse. An uncaught
    `RecursionError` would escape `_proof_fresh` and `main()` entirely, crashing the hook into
    `on_error: "open"` — an allowed commit reached via a hostile marker shape rather than a
    wrong field value, the same bypass class this whole file exists to close. Must resolve to
    BLOCK, not crash."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    depth = 300_000
    (proof / "attest-evil.json").write_text("[" * depth + "]" * depth)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_manual_marker_json_parse_survives_recursion_error(tmp_path, monkeypatch):
    """Same as `test_attestation_json_parse_survives_recursion_error` above, for the OTHER
    `json.loads` call this fix added in round 10 — `_manual_marker_satisfies`'s own JSON
    decode (the round-10 rewrite of the fallback-marker format). A deeply-nested JSON payload
    dropped as a non-`.json`-suffixed fallback marker must not crash the hook either."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    top = str(vpg._repo_toplevel(str(repo)))
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    depth = 300_000
    (proof / "looked").write_text("[" * depth + "]" * depth)
    assert vpg._manual_marker_satisfies(proof / "looked", top) is False


def test_write_marker_round_trips_non_utf8_repo_root(tmp_path, monkeypatch):
    """Regression, review round 9 (PR #484): after the round-8 fix, `_repo_toplevel` decodes a
    non-UTF-8 repo path with `os.fsdecode` (surrogateescape), producing a string with lone
    surrogate codepoints. `_cli_write_marker` used to `.encode()` that string with the default
    STRICT UTF-8 encoder, which raises `UnicodeEncodeError` on a lone surrogate — a `ValueError`
    subclass, not `OSError`, so it was NOT caught and crashed the CLI outright instead of
    writing the fallback marker. Separately, `_manual_marker_satisfies` used to
    `.decode("utf-8", errors="replace")` when reading a marker back, which collapses those same
    surrogates to literal U+FFFD characters — even a successfully-written marker could then
    never compare equal to `repo_root` again. Fixed: write with `os.fsencode` (the exact
    inverse of `os.fsdecode`), read back with `os.fsdecode` too, so the two round-trip.
    Simulates the non-UTF-8 repo (not reproducible via a real on-disk path on this test
    machine's own filesystem, which requires valid UTF-8 names) by monkeypatching
    `_repo_toplevel` to return a string with a genuine lone surrogate — exactly what
    `os.fsdecode` hands back for a raw 0xFF byte."""
    fake_root = "/tmp/repo-\udcff-name"  # os.fsdecode's surrogateescape encoding of byte 0xFF
    monkeypatch.setattr(vpg, "_repo_toplevel", lambda cwd: fake_root)
    proof = tmp_path / "proof"
    monkeypatch.setattr(vpg, "PROOF_DIR", proof)

    rc = vpg._cli_write_marker(["/anything"])
    assert rc == 0  # must not raise UnicodeEncodeError

    written = list(proof.iterdir())
    assert len(written) == 1
    assert vpg._manual_marker_satisfies(written[0], fake_root) is True


# ── SATISFIED MARKER (agent-tools#475: scoped + content-checked, not just "a file exists") ──

def test_allow_when_proof_marker_fresh(tmp_path, monkeypatch):
    """(c) A genuine, correctly-scoped FALLBACK marker (repo toplevel as content) DOES still
    satisfy the gate — the legitimate manual path keeps working."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_manual_marker(proof, repo)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == 0 and _decision(out) == "allow"


def test_bare_touch_junk_marker_no_longer_satisfies_the_gate(tmp_path, monkeypatch):
    """(b) The OLD contract — any fresh file, any content — must no longer satisfy the gate.
    A blind `touch` (empty content, doesn't name any repo) is exactly the shape agent-tools#475
    describes as a junk file that used to pass."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _touch_proof(proof)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_arbitrary_content_marker_no_longer_satisfies_the_gate(tmp_path, monkeypatch):
    """(b) A file with unrelated, non-junk-but-still-wrong content (not a repo path, not valid
    attestation JSON) must not satisfy the gate either — content is actually CHECKED, not just
    "non-empty"."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    (proof / "looked").write_text("I definitely looked at a screenshot, I promise\n")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_manual_marker_scoped_to_a_different_repo_does_not_satisfy(tmp_path, monkeypatch):
    """(a) A marker whose content names a DIFFERENT repo's toplevel must not satisfy THIS
    repo's gate — the core agent-tools#475 cross-repo leak: repo A's marker satisfying repo
    B's commit, machine-wide, via a shared marker directory."""
    repo_a = _mk_repo_with_staged(tmp_path, "unused.tsx")
    repo_b_root = tmp_path / "repo_b"
    repo_b_root.mkdir()
    repo_b = _mk_repo_with_staged(repo_b_root, "src/Button.tsx")  # distinct repo, own subdir
    proof = tmp_path / "proof"  # ONE shared marker dir, as it is in real machine-global usage
    _write_manual_marker(proof, repo_a)  # marker names repo A's toplevel
    out, _e, c = _run("git commit -m x", repo_b, monkeypatch, proof_dir=proof)  # commit in B
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_matching_repo_and_staged_diff_satisfies(tmp_path, monkeypatch):
    """(c) A genuine `dev shot` attestation — repo toplevel AND staged-diff hash both correct —
    satisfies the gate. This is the PRIMARY path."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo)  # defaults: real repo + real current staged-diff hash
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == 0 and _decision(out) == "allow"


def test_dev_shot_attestation_for_a_different_repo_does_not_satisfy(tmp_path, monkeypatch):
    """(a) An attestation whose `repo` field names a DIFFERENT repo must not satisfy THIS
    repo's gate, even with a byte-identical staged diff (both repos stage the same file)."""
    repo_a = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    repo_b_root = tmp_path / "repo_b"
    repo_b_root.mkdir()
    repo_b = _mk_repo_with_staged(repo_b_root, "src/Button.tsx")  # identical staged content
    proof = tmp_path / "proof"
    _write_attestation(proof, repo_a)  # attests repo A's diff
    out, _e, c = _run("git commit -m x", repo_b, monkeypatch, proof_dir=proof)  # commit in B
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_stale_staged_hash_does_not_satisfy(tmp_path, monkeypatch):
    """(a)/(b) An attestation for the RIGHT repo but a staged-diff hash that no longer matches
    (the staged content moved on since the screenshot was taken) must not satisfy the gate —
    the binding is to a specific diff, not just a specific repo."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, staged_sha256="0" * 64)  # correct repo, wrong/stale hash
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_malformed_json_does_not_satisfy(tmp_path, monkeypatch):
    """(b) A `.json`-suffixed junk file (invalid JSON, or valid JSON missing the required
    fields) must not satisfy the gate — parsing/field failure is treated as "not a marker",
    not as a free pass."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    (proof / "attest-junk.json").write_text("not even json {{{")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── review findings (round 4): legacy version/tool, and a future-dated marker ──────────────

def test_dev_shot_attestation_with_wrong_version_does_not_satisfy(tmp_path, monkeypatch):
    """An otherwise-perfect record from a producer version OTHER than
    `_EXPECTED_ATTESTATION_VERSION` must not satisfy the gate — dev-cli's own docs say an
    older version measured blankness and worktree-dirt differently, so its guarantees are not
    equivalent even when its repo/staged-hash happen to still line up."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, version=1)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_wrong_tool_does_not_satisfy(tmp_path, monkeypatch):
    """A record claiming a different (or absent) producer tool must not satisfy the gate — the
    attestation contract is specific to dev-cli's `dev shot`, not an open schema any producer
    can claim to speak."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, tool="some other tool")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_future_dated_marker_does_not_satisfy_the_gate(tmp_path, monkeypatch):
    """(b) A marker whose mtime is set in the FUTURE has a NEGATIVE age — a one-sided freshness
    check (`age <= WINDOW`) would treat it as fresh forever, right up until that future date
    actually arrives. Must be rejected the same as a stale marker."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    marker = _write_manual_marker(proof, repo)
    future = time.time() + 10 * vpg.PROOF_WINDOW_S
    os.utime(marker, (future, future))
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── review findings (round 2): repo+staged-hash alone is a forgeable "primary" path ────────

def test_dev_shot_attestation_missing_capture_fields_does_not_satisfy(tmp_path, monkeypatch):
    """A record with the CORRECT repo and staged-diff hash but missing `capture_path` /
    `capture_sha256` must not satisfy the gate — those two fields are exactly what an agent
    CANNOT produce by just running `git rev-parse`/`git diff --cached` itself, so accepting a
    record without them would make the 'primary' path no stronger than typing two git
    commands (the review finding this closes)."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, omit_fields=("capture_path", "capture_sha256"))
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_no_real_capture_file_does_not_satisfy(tmp_path, monkeypatch):
    """A record naming a `capture_path` that doesn't actually exist on disk — the exact shape
    of a hand-typed forgery that knows the right repo/diff/hash values but never ran `dev
    shot` — must not satisfy the gate."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, write_capture_file=False)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_embedded_nul_capture_path_blocks_not_crashes(
    tmp_path, monkeypatch,
):
    """(b) A `capture_path` containing an embedded NUL byte makes `Path(...).read_bytes()`
    raise `ValueError`, not `OSError` — an uncaught `ValueError` would unwind out of `main()`
    entirely, and this hook's descriptor is `on_error: "open"`, so a CRASHED gate would be an
    ALLOWED commit: the exact bypass this fix exists to close, reached via a different route
    (review finding, round 3). This must resolve to a normal BLOCK, not an exception."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    record = {
        "version": 2, "tool": "dev shot", "captured_at": int(time.time()),
        "repo": str(vpg._repo_toplevel(str(repo))),
        "staged_sha256": _staged_diff_sha256(repo),
        "worktree_dirty": False,
        "capture_path": "\x00",
        "capture_sha256": "0" * 64,
        "url": "http://localhost:5173/",
    }
    (proof / "attest-nul.json").write_text(json.dumps(record))
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)  # must not raise
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_wrong_capture_hash_does_not_satisfy(tmp_path, monkeypatch):
    """A record whose `capture_sha256` does NOT match the actual bytes at `capture_path` —
    someone edited the file after attesting, or fabricated the digest — must not satisfy."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, capture_sha256="0" * 64)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_dirty_worktree_does_not_satisfy(tmp_path, monkeypatch):
    """A record with `worktree_dirty: true` — the worktree had already diverged from the index
    when the screenshot was taken, so the browser may have rendered content that was never
    staged at all — must not satisfy the gate, even with a correct repo/staged-hash/capture."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, worktree_dirty=True)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_dev_shot_attestation_with_missing_worktree_dirty_field_does_not_satisfy(
    tmp_path, monkeypatch,
):
    """`worktree_dirty` absent entirely (an older/foreign producer's record) must be treated
    the same as `true` — unknown is not proof of clean, so it does not satisfy the gate."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo, omit_fields=("worktree_dirty",))
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_fully_genuine_dev_shot_attestation_still_satisfies_the_gate(tmp_path, monkeypatch):
    """(c) The FULL positive path, now with every field the tightened checks require: correct
    repo, correct staged-diff hash, worktree_dirty=False, and a real capture file whose hash
    matches. This must still pass — the tightening must not have broken the honest case."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_attestation(proof, repo)  # all defaults are the fully-valid shape
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == 0 and _decision(out) == "allow"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_fifo_manual_marker_does_not_hang_and_does_not_satisfy(tmp_path, monkeypatch):
    """(b), review finding round 3: a FIFO placed at a marker path — with no writer, so a naive
    `read_text()` would block FOREVER — must resolve to BLOCK promptly, not hang. A hang past
    this hook's timeout is treated as a crash, and the descriptor is `on_error: "open"`, so a
    hang IS a bypass by a different route. pytest's own default (no timeout plugin) means a
    real hang here would wedge the whole test run, which is itself the strongest possible
    proof this regression is closed if the test completes at all."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    os.mkfifo(str(proof / "looked"))  # a manual-marker-shaped FIFO, never opened by a writer
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_fifo_capture_path_in_attestation_does_not_hang_and_does_not_satisfy(
    tmp_path, monkeypatch,
):
    """Same hazard, reached through an attestation's `capture_path` instead of the marker file
    itself — a fully-valid-looking record pointing `capture_path` at a FIFO must also resolve
    to BLOCK promptly."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    fifo = proof / "shot.png"
    os.mkfifo(str(fifo))
    _write_attestation(proof, repo, write_capture_file=False, capture_sha256="0" * 64)
    # Overwrite capture_path in the just-written record to point at the FIFO instead.
    marker = next(proof.glob("attest-*.json"))
    record = json.loads(marker.read_text())
    record["capture_path"] = str(fifo)
    marker.write_text(json.dumps(record))
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_manual_marker_rejects_oversized_file(tmp_path, monkeypatch):
    """(b) A fallback marker larger than `_MAX_MARKER_BYTES` must not satisfy the gate, even if
    its first line would otherwise be a valid repo path — bounds a maliciously (or
    accidentally) huge file, not just its content shape."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    top = str(vpg._repo_toplevel(str(repo)))
    padding = "\n" + ("x" * (vpg._MAX_MARKER_BYTES + 1))
    (proof / "looked").write_text(top + padding)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_safe_regular_file_bytes_rejects_growth_racing_past_the_stat_check(tmp_path, monkeypatch):
    """review finding round 5: even when `fstat` reports a size WITHIN the limit, if the
    actual read comes back with MORE bytes than that (the shape a concurrent writer
    appending between the stat and the read completing would produce), the result must be
    rejected outright — not silently truncated-and-accepted. Simulates the race by making
    `os.fstat` under-report the real file's size, rather than relying on genuine concurrent
    timing (which would be flaky here)."""
    p = tmp_path / "grown.bin"
    p.write_bytes(b"x" * 100)  # the REAL size — bigger than the limit below
    real_fstat = os.fstat

    def lying_fstat(fd):
        real = real_fstat(fd)
        fields = (real.st_mode, real.st_ino, real.st_dev, real.st_nlink, real.st_uid,
                  real.st_gid, 10, real.st_atime, real.st_mtime, real.st_ctime)
        return os.stat_result(fields)  # reports 10 bytes, well within the limit

    monkeypatch.setattr(vpg.os, "fstat", lying_fstat)
    assert vpg._safe_regular_file_bytes(p, 10) is None


def test_marker_scan_cap_stops_before_validating_a_marker(tmp_path, monkeypatch):
    """review finding round 6: unbounded per-file size caps don't bound TOTAL work when there
    can be many files — thousands of fresh, valid-shaped candidates could each cost a real
    read-and-hash before being rejected, pushing total scan time past this hook's own timeout
    (which fails open). `_MAX_MARKERS_SCANNED` bounds the number of candidates inspected.
    Verify it's actually WIRED (not a decorative unused constant) by dropping the cap to 0 —
    even a single, fully-valid marker must then no longer satisfy the gate, since the very
    first candidate encountered already exceeds a zero budget regardless of file order."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    _write_manual_marker(proof, repo)  # genuinely valid — would satisfy at the real cap
    monkeypatch.setattr(vpg, "_MAX_MARKERS_SCANNED", 0)
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_many_junk_markers_resolve_promptly_not_unboundedly(tmp_path, monkeypatch):
    """A directory flooded with more fresh junk markers than `_MAX_MARKERS_SCANNED` must still
    resolve (BLOCK, since none of them validate) rather than working through an unbounded
    pile — a basic throughput sanity check for the cap added above."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    for i in range(vpg._MAX_MARKERS_SCANNED + 10):
        (proof / f"junk-{i}").write_text("not a repo path")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_scan_cap_bounds_stale_entries_too(tmp_path, monkeypatch):
    """Regression, review round 7 (PR #484): `scanned += 1` used to run only AFTER the
    freshness `continue`, so entries filtered out as stale (or whose `stat()` raised OSError)
    never counted toward the scan budget — a directory flooded with expired/broken markers
    still cost one real `stat()` per entry, completely unbounded, defeating the whole point of
    the cap (only FRESH candidates were ever bounded). Flood PROOF_DIR with far more STALE
    entries than `_MAX_ENTRIES_SCANNED` and assert the number of `stat()` calls made is bounded
    by that cap, not by the directory size — under the pre-round-7 code this would call
    `stat()` once per flooded file; after round 7 it stops at a cap.

    Bounds against `_MAX_ENTRIES_SCANNED` (the cheap-pass cap), not `_MAX_MARKERS_SCANNED` (the
    round 8 fix, PR #484): these used to be the SAME shared counter, but round 8 split them —
    see `test_fresh_marker_after_many_stale_entries_is_still_found` for why a single small
    counter shared across both cheap and expensive work made a genuinely fresh marker
    unreachable on a long-lived, cluttered PROOF_DIR."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    stale_mtime = time.time() - vpg.PROOF_WINDOW_S - 3600
    flood = vpg._MAX_ENTRIES_SCANNED + 500
    for i in range(flood):
        p = proof / f"stale-{i}"
        p.write_text("stale")
        os.utime(p, (stale_mtime, stale_mtime))

    calls = 0
    real_stat = Path.stat

    def counting_stat(self, *a, **kw):
        nonlocal calls
        if self.parent == proof:
            calls += 1
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", counting_stat)
    monkeypatch.setattr(vpg, "PROOF_DIR", proof)
    top = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()

    assert vpg._proof_fresh(top) is False
    assert calls <= vpg._MAX_ENTRIES_SCANNED
    assert calls < flood  # proves the flood didn't get fully traversed


def test_fresh_marker_after_many_stale_entries_is_still_found(tmp_path, monkeypatch):
    """Regression, review round 8 (PR #484): the round-7 fix above bounded a stale-entry flood
    by charging EVERY iterated entry (fresh or stale) against the SAME small
    `_MAX_MARKERS_SCANNED` (200) budget. On a long-lived, never-pruned PROOF_DIR that
    accumulates more than 200 stale entries, that made a genuinely fresh, valid marker landing
    AFTER them in `iterdir()`'s arbitrary order unreachable — the scan gave up before ever
    inspecting it, and a commit with a REAL, correctly-reviewed screenshot got BLOCKED anyway.

    Reproduces exactly that shape deterministically: write more than `_MAX_MARKERS_SCANNED`
    stale entries, then a genuinely valid, fresh manual marker, and FORCE iteration to visit
    every stale entry before the real one — `iterdir()`'s actual order is filesystem-dependent
    and not guaranteed to reproduce the worst case on its own, so this pins it rather than
    leaving the regression to chance. Must still resolve to ALLOW: the split cheap/expensive
    budget (`_MAX_ENTRIES_SCANNED` vs `_MAX_MARKERS_SCANNED`) means the stale entries only cost
    cheap `stat()` calls and don't consume the expensive-validation budget the real marker
    needs. Against the pre-round-8 code (one shared 200-entry counter) this exact ordering
    would BLOCK instead."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    stale_mtime = time.time() - vpg.PROOF_WINDOW_S - 3600
    for i in range(vpg._MAX_MARKERS_SCANNED + 50):
        p = proof / f"stale-{i:05d}"
        p.write_text("stale")
        os.utime(p, (stale_mtime, stale_mtime))
    real_marker = _write_manual_marker(proof, repo, name="real-marker")

    real_iterdir = Path.iterdir

    def stale_entries_first_iterdir(self):
        entries = list(real_iterdir(self))
        if self == proof:
            entries.sort(key=lambda p: p == real_marker)  # False (stale) sorts before True
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", stale_entries_first_iterdir)

    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == 0 and _decision(out) == "allow"


def test_write_marker_cli_produces_a_marker_that_satisfies_the_gate(tmp_path, monkeypatch):
    """The `--write-marker` CLI fallback (the sanctioned replacement for a bare `touch`,
    documented in the README and the BLOCK message) actually produces a marker the gate
    accepts, end to end."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    monkeypatch.setattr(vpg, "PROOF_DIR", proof)
    rc = vpg._cli_write_marker([str(repo)])
    assert rc == 0
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=proof)
    assert c == 0 and _decision(out) == "allow"


def test_write_marker_cli_uses_unpredictable_names_and_never_writes_through_a_symlink(
    tmp_path, monkeypatch,
):
    """review finding round 6: the OLD implementation wrote to a predictable
    `looked-<millisecond-timestamp>` name with a plain, symlink-following create — in a
    directory shared by every local agent, another process could pre-plant a symlink at a
    guessable name pointing at an arbitrary user-writable file, and the write would silently
    clobber that target instead of creating a marker. `tempfile.mkstemp` (`O_CREAT|O_EXCL`)
    refuses to open ANY pre-existing path, symlink or not. Proof: a symlink already sitting in
    PROOF_DIR, pointing at a "victim" file OUTSIDE it, is left completely untouched — its
    target's content is unchanged — after `_cli_write_marker` runs, and the marker it actually
    writes is a distinct, freshly-created regular file, not a name that could plausibly have
    collided with the pre-planted symlink."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    proof = tmp_path / "proof"
    proof.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch me")
    decoy_symlink = proof / "looked-decoy"
    os.symlink(str(victim), str(decoy_symlink))
    monkeypatch.setattr(vpg, "PROOF_DIR", proof)
    rc = vpg._cli_write_marker([str(repo)])
    assert rc == 0
    assert victim.read_text() == "do not touch me"  # the pre-planted symlink's target: intact
    assert decoy_symlink.is_symlink()  # the symlink itself: still just a symlink, never opened
    written = [p for p in proof.iterdir() if p.name != "looked-decoy"]
    assert len(written) == 1 and written[0].is_file() and not written[0].is_symlink()
    top = str(vpg._repo_toplevel(str(repo)))
    assert json.loads(written[0].read_text()) == top


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline sentinel) ───────

def test_old_env_escape_hatch_no_longer_bypasses(tmp_path, monkeypatch):
    """`ALLOW_NO_VISUAL_PROOF=1` (+ _REASON) as a real process env must NO LONGER allow the
    commit — the self-service bypass was removed in favor of the Telegram hatch."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(
        "git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof",
        env={"ALLOW_NO_VISUAL_PROOF": "1", "ALLOW_NO_VISUAL_PROOF_REASON": "css var rename"},
    )
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_old_inline_sentinel_no_longer_bypasses(tmp_path, monkeypatch):
    """An inline `# visual-proof-ok: <reason>` comment must NO LONGER allow the commit — the
    self-documenting per-command sentinel is gone."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x  # visual-proof-ok: deleting a dead component",
                      repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── Telegram hatch escalation (RIG_HATCH_REQUEST_VISUAL_PROOF_GATE) ──────────────────────

def test_hatch_unset_blocks_and_names_env_var(tmp_path, monkeypatch):
    """No hatch requested → the normal block, and the message names the hatch env var so an
    agent knows the only sanctioned escape."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_VISUAL_PROOF_GATE" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    """A bare `1` (no written justification) is an invalid request → deny (block), and NO
    tg-ctl is ever invoked. A never-callable path proves no Telegram round-trip happens."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 0\n")  # would ALLOW if ever called
    monkeypatch.setattr(vpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof",
                      env={"RIG_HATCH_REQUEST_VISUAL_PROOF_GATE": "1"})
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 0 (the human approved) → allow."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", _ALLOW_REPLY_SH)
    monkeypatch.setattr(vpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof",
                      env={"RIG_HATCH_REQUEST_VISUAL_PROOF_GATE": "Deleting a dead component."})
    assert c == 0 and _decision(out) == "allow"
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks_citing_denial(tmp_path, monkeypatch):
    """A written justification + tg-ctl exit 1 (the human declined / timed out) → block, and
    the message leads with the denial reason."""
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(vpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit -m x", repo, monkeypatch, proof_dir=tmp_path / "proof",
                      env={"RIG_HATCH_REQUEST_VISUAL_PROOF_GATE": "Deleting a dead component."})
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


# ── B2: the GIT_COMMIT regex must NOT match `git`+`commit` in plain prose ────────────────

def test_git_commit_prose_is_not_a_commit(tmp_path, monkeypatch):
    """`echo "... git ... commit"` is not a commit invocation → allow even with staged UI."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run('echo "remember to git, then commit"', repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_git_with_global_flags_commit_is_still_gated(tmp_path, monkeypatch):
    """`git -C path commit` (global flag before subcommand) must still be gated."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(f"git -C {repo} commit -m x", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── #12: --amend is a real commit (gated); --continue is skipped (allowed) ───────────────

def test_commit_amend_with_staged_ui_is_gated(tmp_path, monkeypatch):
    """`git commit --amend` that re-touches user-visible files still needs proof → BLOCK. An
    amend is a real commit; it must NOT be treated as a skip like --continue/--abort (#12)."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit --amend --no-edit", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_commit_continue_is_allowed(tmp_path, monkeypatch):
    """`git commit --continue` (mid rebase/merge) carries no new authored change to prove → it
    is a skip flag (is_skip_commit) and allowed even with staged UI files (#12)."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit --continue", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"

@pytest.mark.parametrize("command", [
    "git commit --continue && git commit -m x",
    "git commit --continue ; git commit -m x",
])
def test_skip_commit_followed_by_real_commit_is_not_exempt(command, tmp_path, monkeypatch):
    """A rebase-plumbing ``--continue`` chained with a SECOND, REAL commit used to exempt the
    WHOLE command from the gate — is_skip_commit returned on the FIRST commit segment found
    (the plumbing one) and never looked at the second, authoring commit. One real commit
    anywhere in the chain must force gating (BLOCK when UI is staged with no proof)."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(command, repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


def test_two_skip_commits_chained_are_still_exempt(tmp_path, monkeypatch):
    """Inverse of the above: if EVERY commit segment in the chain carries a skip flag, the
    whole command really is just plumbing and stays exempt — ALLOW even with staged UI files
    and no proof marker."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit --continue && git commit --continue", repo, monkeypatch,
                      proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


# ── codex P2: a skip token in a COMMENT / MESSAGE must NOT bypass the gate ───────────────

@pytest.mark.parametrize("command", [
    "git commit -m x # --abort",                  # skip token in a trailing shell comment
    "git commit -m 'x' # leftover --skip note",   # comment after a quoted message
    "git commit -m 'support --skip in messages'",  # skip token inside the commit message
    "git commit -am 'fix --continue handling'",   # -am clusters; value carries --continue
    "git commit -am --skip",                      # -am clusters; the VALUE is literally `--skip`
    "git commit -aF --abort",                     # -aF clusters; the file-path value is `--abort`
    "git commit -- --skip",                       # `--skip` is a PATHSPEC after `--`, not a flag
    "git commit -m x -- --abort src/",            # pathspec named --abort after `--`
    "git rebase --abort && git commit -m x",      # skip flag on a SIBLING command, not the commit
    "git commit --trailer --skip -m x",           # --trailer's VALUE leaks as --skip (single segment)
])
def test_skip_token_in_comment_or_message_does_not_bypass(command, tmp_path, monkeypatch):
    """codex P2 bypass: ``SKIP_COMMIT`` used to match the RAW string, so a normal commit could
    skip the visual-proof gate by putting ``--abort``/``--skip`` in shell text Git never runs
    (a trailing comment) or inside the commit message. The skip exemption now derives from the
    PARSED argv, so these are real authoring commits → BLOCK when UI is staged with no proof."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(command, repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


def test_real_skip_flag_still_exempt_after_parsing(tmp_path, monkeypatch):
    """The fix must not over-block: a genuine ``git commit --abort`` (skip flag in the real
    argv, not a comment) is still exempt even with staged UI files."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run("git commit --abort", repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == 0 and _decision(out) == "allow"


def test_is_skip_commit_direct_trailer_value_does_not_leak_as_skip_flag():
    """Direct, gate-plumbing-free pin (mirrors the equivalent test in
    tests/test_skills_read_gate.py): ``--trailer --skip`` puts the LITERAL string ``--skip`` in
    argv as the trailer's VALUE, not a real skip flag — it must not leak into ``_commit_flags``'
    output and falsely satisfy ``any(tok in SKIP_FLAGS ...)``. Single commit segment: the chained
    `--continue && ... --trailer --skip` variant additionally needs the all-segments fix (see the
    agent-tools#174 tests below) and is pinned there instead."""
    assert vpg.is_skip_commit("git commit --trailer --skip -m x") is False
    assert vpg.is_skip_commit("git commit --trailer=foo -m x") is False


# ── agent-tools#174: a chain must not be exempted on its FIRST commit segment alone ───────
#
# NOTE: agent-tools#174's own issue body claimed this file was already fixed by agent-tools#172
# — that was a misattribution (#172 was an unrelated effective_cwd() tilde-expansion fix), and
# the original first-segment-only bug was still live here until this PR. Mirrors the identical
# tests already pinning this behavior in tests/test_skills_read_gate.py.


def test_chained_trailer_bypass_closed_by_both_fixes_together(tmp_path, monkeypatch):
    """The EXACT repro from chatgpt-codex-connector's PR #197 review comment: a rebase-plumbing
    ``--continue`` chained with a real commit whose ``--trailer``'s VALUE is literally
    ``--skip``. Needs BOTH fixes to close — the trailer fix alone still short-circuits on the
    first (``--continue``) segment before ever reaching the trailer segment; the all-segments
    fix alone still lets the trailer's value leak as a skip flag on the second segment. Only
    together do they force gating on this exact chain."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    cmd = "git commit --continue && git commit --trailer --skip -m x"
    out, _e, c = _run(cmd, repo, monkeypatch, proof_dir=tmp_path / "proof")
    assert c == vpg.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── T1: NOT subagent-exempt — an agent_id present must STILL block (locks the doctrine) ──

def test_blocks_even_with_agent_id_present(tmp_path, monkeypatch):
    """visual-proof-gate is NOT subagent-exempt: a subagent committing UI work must also have
    looked at the result. An `agent_id` in the event must NOT exempt the commit."""
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, err = io.StringIO(), io.StringIO()
    event = {"cwd": str(repo), "agent_id": "sub-x",
             "args": {"command": "git commit -m x", "agent_id": "sub-x"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(vpg, "PROOF_DIR", tmp_path / "proof")
    monkeypatch.delenv("RIG_HATCH_REQUEST_VISUAL_PROOF_GATE", raising=False)
    code = vpg.main()
    assert code == vpg.BLOCK_EXIT_CODE and _decision(out.getvalue()) == "block"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl via the new `command=` contract. Regression for the documented inline form (Codex #233)."""
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl", f'cat > "{question}"\n' + _ALLOW_REPLY_SH
    )
    monkeypatch.setattr(vpg.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    repo = _mk_repo_with_staged(tmp_path, "src/Button.tsx")
    out, _e, c = _run(
        'RIG_HATCH_REQUEST_VISUAL_PROOF_GATE="deleting a dead component, nothing to render" git commit -m x',
        repo, monkeypatch, proof_dir=tmp_path / "proof")  # env NOT set — inline only
    assert c == 0 and _decision(out) == "allow"
    assert "deleting a dead component, nothing to render" in question.read_text()
