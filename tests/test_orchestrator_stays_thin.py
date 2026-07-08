"""Tests for the orchestrator-stays-thin agent-hook (pre-write + pre-bash).

Covers the doctrine's four cases for BOTH points: BLOCK (a repeat code write / impl bash by
the main thread), ALLOW (docs path / read-only one-liner / first-offense WARN), SUBAGENT-EXEMPT
(agent_id present), and the ESCAPE hatch (env+reason and inline sentinel; reasonless still
blocks). Hermetic: the warn/block tier marker dir is redirected into tmp_path via env, so the
two-call warn→block sequence is exercised without touching the real cache.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_orchestrator_stays_thin.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "orchestrator-stays-thin"
    / "orchestrator_stays_thin.py"
)
_spec = importlib.util.spec_from_file_location("orchestrator_stays_thin", _HOOK)
assert _spec and _spec.loader
ost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ost)


def _run(event, monkeypatch, marker_dir: Path, env: dict | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    # Redirect the tier marker dir into the test sandbox and re-read the module constant.
    monkeypatch.setenv("ORCH_THIN_MARKER_DIR", str(marker_dir))
    monkeypatch.setattr(ost, "MARKER_DIR", marker_dir)
    for k in ("ALLOW_ORCHESTRATOR_WORK", "ALLOW_ORCHESTRATOR_WORK_REASON", "RIG_ORCHESTRATOR_ONLY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = ost.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK (warn first, then block on repeat) ───────────────────────────────────────────

def test_block_code_write_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    # first offense → WARN (allow + message)
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    # repeat in the same cwd within TTL → BLOCK
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE
    assert _decision(out2) == "block"
    assert "delegate" in json.loads(out2)["message"].lower()


def test_block_impl_bash_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "sed -i 's/a/b/' f && npm run build && echo done"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── ALLOW (docs / read-only / non-offending) ───────────────────────────────────────────

def test_allow_docs_write(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/README.md"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_allow_docs_dir_write(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/docs/plan.json"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_allow_read_only_bash(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git status && ls -la"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── SUBAGENT-EXEMPT ────────────────────────────────────────────────────────────────────

def test_subagent_exempt_code_write(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"agent_id": "sub-7", "file_path": "/repo/src/a.ts"}}
    # even on a repeat it must allow, because a subagent does the actual work
    _run(event, monkeypatch, tmp_path / "m")
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_empty_agent_id_does_not_exempt(tmp_path, monkeypatch):
    """An EMPTY/whitespace `args.agent_id` is NOT a subagent — it must NOT exempt the
    orchestrator. A blank signal can't relax the gate, so a repeat impl write still BLOCKs."""
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"agent_id": "   ", "file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_only_args_agent_id_exempts_not_top_level_or_tool_input(tmp_path, monkeypatch):
    """TRUST BOUNDARY regression guard (agent-tools#115). `_is_subagent` reads ONLY
    `args.agent_id` — the single surface lib/cc_hook_bridge sanitizes (T2 precedence: it drops
    any model/tool_input-supplied copy and NEVER writes a top-level `agent_id`). This gate uses
    agent_id to RELAX (exempt a subagent), so a forged agent_id sitting ANYWHERE ELSE must NOT
    exempt the orchestrator:
      - at the event TOP LEVEL (the old `or event.get('agent_id')` fallback — an unsanitized
        relax-surface, now dropped), and
      - nested under `args.tool_input` (a model-controllable surface).
    With no real `args.agent_id` the orchestrator gate applies and a repeat impl write BLOCKs.
    If a future edit widened the read to either surface, the orchestrator could self-exempt and
    this test would catch it."""
    event = {"point": "pre-write", "cwd": "/repo", "agent_id": "top-level-decoy",
             "args": {"file_path": "/repo/src/a.ts",
                      "tool_input": {"agent_id": "nested-decoy"}}}
    _run(event, monkeypatch, tmp_path / "m")  # warn (NOT exempted by the decoys)
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── ESCAPE ─────────────────────────────────────────────────────────────────────────────

def test_escape_env_reason_allows(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # prime the warn marker
    out, _e, c = _run(
        event, monkeypatch, tmp_path / "m",
        {"ALLOW_ORCHESTRATOR_WORK": "1", "ALLOW_ORCHESTRATOR_WORK_REASON": "trivial tweak"},
    )
    assert c == 0 and _decision(out) == "allow"


def test_escape_inline_sentinel_allows_bash(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "sed -i s/a/b/ f && echo x && echo y  # orchestrator-ok: one-off"}}
    _run(event, monkeypatch, tmp_path / "m")  # prime the warn marker
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_reasonless_override_still_blocks_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m", {"ALLOW_ORCHESTRATOR_WORK": "1"})  # no reason
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── B1: a chain that merely STARTS read-only is judged on its full content ───────────────

@pytest.mark.parametrize("command", [
    "git status && sed -i 's/a/b/' f.py",  # read-only prefix + in-place edit
    "ls; npm run build",                   # read-only prefix + build
])
def test_read_only_prefix_chain_blocks_on_repeat(command, tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_bare_read_only_still_allows(tmp_path, monkeypatch):
    """A single unchained read-only command keeps its carve-out (no warn, no block)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git status"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


# ── B7: a bare redirect is not implementation ────────────────────────────────────────────

def test_plain_redirect_is_not_implementation(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "python foo.py > out.log"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # still not implementation
    assert c2 == 0 and _decision(out2) == "allow"


# ── #80: a FULLY read-only pipe of ANY length is never blocked ───────────────────────────

@pytest.mark.parametrize("command", [
    "find . -name foo | grep bar | head",      # the live-session repro
    "tail -100 log | grep err | wc -l",        # 3-segment inspection
    "find . | grep x | head -5",               # 3 segments, trailing args
    "cat a.txt | grep -i warn | grep -v ok | head -20",  # 4 segments
])
def test_read_only_pipe_any_length_allows(command, tmp_path, monkeypatch):
    """A pipe where EVERY segment is read-only inspection must never warn or block, even
    with 3+ segments (it tripped `len(CHAIN.findall()) >= 2` before #80)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "find . | grep x | sed -i 's/a/b/' f.py",     # read-only segments + in-place edit
    "tail -50 log | grep err | npm install pkg",  # read-only segments + installer
    "cat a | grep b | tee out.txt",               # read-only segments + tee write
])
def test_read_only_pipe_with_one_impl_segment_blocks_on_repeat(command, tmp_path, monkeypatch):
    """One build/edit segment anywhere in an otherwise-read-only pipe still blocks (#80)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_read_only_pipe_with_heredoc_segment_blocks_on_repeat(tmp_path, monkeypatch):
    """A heredoc inside an otherwise-read-only pipe still blocks (#80)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "cat <<EOF > f\nbody\nEOF\ngrep x f | head"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_pipe_with_non_read_only_segment_unchanged(tmp_path, monkeypatch):
    """A pipe with a segment that is neither read-only nor build/edit keeps the old
    `>= 2 operators is implementation` behavior — the carve-out only covers ALL-read-only."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "find . | python score.py | head"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    "cat tee.log",                       # `tee` is the FILE being read, not a write
    "grep tee notes.txt",                # `tee` is the search NEEDLE
    "cat Cargo.toml",                    # build-manifest is an inspection target
    "head package.json",                 # ditto
    "grep dep Cargo.toml | head",        # read-only pipe, build-token as an argument
])
def test_read_only_with_build_token_argument_allows(command, tmp_path, monkeypatch):
    """A build/edit token appearing only as the ARGUMENT/needle of a read-only command keeps
    the carve-out (#80 review #1) — judgement is per-segment-HEAD, not a whole-string scan.
    `tee`/`sed -i` are unanchored in BUILD_EDIT, so a whole-string veto would mis-flag these."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


def test_single_read_only_via_new_path_still_allows(tmp_path, monkeypatch):
    """The old single-command carve-out is now a subset of _is_all_read_only — pin it (#80
    review #2): a bare `git status` still routes through the new path and is never blocked."""
    assert ost._is_all_read_only("git status") is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git status"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "git status && ls && cat x",          # all-read-only && chain (>= 2 operators)
    "ls; cat foo.txt; grep err foo.txt",  # all-read-only ; chain
])
def test_read_only_non_pipe_chain_allows(command, tmp_path, monkeypatch):
    """The carve-out covers ANY operator, not just `|` — a read-only `&&`/`;` chain of any
    length is inspection too (#80 review #3). `git` is narrowly scoped in READ_ONLY_BASH
    (status/log/diff/show/branch only), so `git add && git commit && git push` is NOT covered
    — see test_git_mutating_chain_still_blocks_on_repeat."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


def test_git_mutating_chain_still_blocks_on_repeat(tmp_path, monkeypatch):
    """`git` is narrowly scoped to read-only subcommands, so a mutating git chain is NOT
    waved through as all-read-only and still blocks on repeat (#80 review #1 — no security
    regression: add/commit/push do not match READ_ONLY_BASH)."""
    assert ost._is_all_read_only("git add -A && git commit -m x && git push") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "git add -A && git commit -m x && git push"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_build_edit_head_with_read_only_needle_blocks(tmp_path, monkeypatch):
    """ANCHOR INVARIANT (#80 review #1): the per-segment-HEAD contract holds only because
    READ_ONLY_BASH is head-anchored (`^\\s*`). A segment whose HEAD is a build/edit command
    (`sed -i ...`) but whose ARGUMENT contains a read-only word (`grep.py`) must NOT be waved
    through. If READ_ONLY_BASH were ever de-anchored, `.search()` would match the `grep` needle
    mid-segment and silently allow an in-place edit — this test fails loud on that regression."""
    cmd = "find . | sed -i 's/a/b/' grep.py"
    assert ost._is_all_read_only(cmd) is False
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": cmd}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── T3: heredoc-to-file is implementation ────────────────────────────────────────────────

def test_heredoc_to_file_blocks_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "cat <<EOF > f\nbody\nEOF"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── B3: a NotebookEdit carrying only notebook_path is judged ─────────────────────────────

def test_notebook_path_only_code_write_blocks_on_repeat(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"notebook_path": "/repo/src/explore.ipynb"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_notebook_path_under_docs_allows(tmp_path, monkeypatch):
    event = {"point": "pre-write", "cwd": "/repo",
             "args": {"notebook_path": "/repo/docs/notes.ipynb"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── B5: tiers are keyed by (cwd, point) — a pre-write warn does NOT prime a pre-bash block ─

def test_pre_write_warn_does_not_prime_pre_bash_block(tmp_path, monkeypatch):
    marker = tmp_path / "m"
    write_ev = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    bash_ev = {"point": "pre-bash", "cwd": "/repo",
               "args": {"command": "sed -i s/a/b/ f && echo x && echo y"}}
    out1, _e1, c1 = _run(write_ev, monkeypatch, marker)  # pre-write WARN
    assert c1 == 0 and _decision(out1) == "allow"
    # the FIRST pre-bash offense in the same cwd must still only WARN (independent tier)
    out2, _e2, c2 = _run(bash_ev, monkeypatch, marker)
    assert c2 == 0 and _decision(out2) == "allow"
    # the SECOND pre-bash offense now blocks (its own tier matured)
    out3, _e3, c3 = _run(bash_ev, monkeypatch, marker)
    assert c3 == ost.BLOCK_EXIT_CODE and _decision(out3) == "block"


# ── #5: BUILD_EDIT tool tokens must be anchored at a command head, not match as a needle ─

@pytest.mark.parametrize("command", [
    "cat notes.md | grep npm",        # npm is a grep needle, not the command
    "git log | rg yarn",              # yarn is an rg needle
    "find . -name cargo.toml | wc -l",  # cargo.toml is a find argument
])
def test_build_tool_as_pipe_needle_is_not_implementation(command, tmp_path, monkeypatch):
    """A build-tool NAME appearing as an argument/needle in an inspection pipe must NOT be
    classified as implementation — only a build tool at the COMMAND head counts (#5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    # never primes a block, because it is not an offending action at all
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", ["npm run build", "cargo build", "ls; npm run build"])
def test_real_build_at_head_blocks_on_repeat(command, tmp_path, monkeypatch):
    """A real build tool at the command head (or after a separator) is implementation (#5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_sed_in_place_anywhere_still_implementation(tmp_path, monkeypatch):
    """`sed -i` keeps its position-free signal: it is implementation even unchained (#5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "sed -i 's/a/b/' f.py"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# ── tg#5743: the flapping fix — sanctioned orchestration is NEVER blocked, even chained ─────

@pytest.mark.parametrize("command", [
    "gh ship 5",                                   # the sanctioned release (agent-tools#23)
    "gh ship 5 && tg 'shipped'",                   # ship + report (used to flap: >=2 operators)
    "gh pr list && gh pr view 5",                  # PR inspection chain
    "gh pr checks 5 | grep fail",                  # gh read piped into a read-only filter
    "gh run list && gh run view 9 && tg done",     # 3-segment orchestration chain
    "gh api repos/o/r/pulls | jq '.[].number'",    # gh api GET piped into jq (read-only)
    "gh api repos/o/r/pulls | jq '.[]' | head",    # 2 pipes — the jq fix (was blocked before)
    "git worktree list",                           # worktree inspection
    "review diff",                                  # multi-model review CLI
    "dev start web",                                # configured dev/e2e lifecycle
    "dev list",                                     # inspect configured/running dev targets
    "dev status smoke",                             # e2e/dev progress/status
    "dev logs smoke --tail 50",                     # configured logs, not raw docker logs
    "dev e2e run smoke",                            # first-class e2e run
    "dev e2e status smoke",                         # first-class e2e status
    "dev e2e logs smoke",                           # first-class e2e logs
    "dev run test",                                  # project-scoped rig.yaml scripts
    "dev stop --port 5173",                          # project-scoped dev process control
    "dev stop --pgid 5001",                          # validated dev/e2e process group stop
    "dev env --add-project ../api",                  # session-scoped multi-project setup
])
def test_orchestration_chain_never_blocks(command, tmp_path, monkeypatch):
    """`gh` reads / `gh ship` / `tg` / `review` / `git worktree list` are orchestration, never
    implementation — a chain of only these must not warn OR block (Alex tg#5743 / #23). `jq`/`grep`
    tails have read-only HEADS, so the whole chain stays allowed even with 2+ pipes."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow", command


def test_gh_ship_is_all_inline_allowed():
    assert ost._is_all_inline_allowed("gh ship 5 && tg 'done'") is True
    assert ost._is_all_read_only("gh ship 5 && tg 'done'") is False  # not read-only, but allowed


def test_dev_head_does_not_launder_impl_tail(tmp_path, monkeypatch):
    """`dev` is allowed only as its own sanctioned segment; a real build chained after it
    is still implementation-shaped and warn-then-blocks."""
    assert ost._is_all_inline_allowed("dev run test && npm run build") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "dev run test && npm run build"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_dev_run_only_known_safe_scripts_are_orchestration(tmp_path, monkeypatch):
    """`dev run test` is sanctioned, but `dev run <anything>` must not become a blanket
    implementation bypass for configured scripts with side effects."""
    assert ost._is_all_inline_allowed("dev run test") is True
    assert ost._is_all_inline_allowed("dev run build") is False
    assert ost._is_implementation_bash("dev run build") is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "dev run build"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_dev_e2e_is_allowed_inside_command_substitution():
    assert ost._dev_segment_is_unknown("dev e2e run smoke") is False
    assert ost._is_implementation_bash('tg "$(dev e2e status smoke)"') is False


def test_dev_unknown_subcommand_is_not_allowlisted(tmp_path, monkeypatch):
    """A blanket `dev` head would launder arbitrary argv; only known orchestration
    subcommands get the carve-out."""
    assert ost._is_all_inline_allowed("dev npm run build") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "dev npm run build && cat out"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_dev_e2e_unknown_nested_subcommand_is_not_allowlisted(tmp_path, monkeypatch):
    """`dev e2e` is first-class, but it cannot launder arbitrary nested argv."""
    assert ost._is_all_inline_allowed("dev e2e npm run build") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "dev e2e npm run build && cat out"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_head_with_impl_tail_still_blocks_on_repeat(tmp_path, monkeypatch):
    """A chain that STARTS orchestration but mixes in real work is judged on its full content —
    `gh pr view && git commit` still blocks on repeat (the prefix does not wave it through)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh pr view 5 && git commit -m x"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    "gh api graphql -f query='mutation{x}'",  # graphql mutation, single (0 operators)
    "gh api repos/o/r/issues -X POST -f title=x",  # explicit POST
    "gh api repos/o/r -fkey=value && tg done",     # ATTACHED short field flag (codex)
    "gh api repos/o/r -XPOST",                     # ATTACHED method flag
])
def test_gh_api_mutation_is_implementation(command, tmp_path, monkeypatch):
    """`gh api` is whitelisted ONLY when GET-shaped — a mutation (graphql / -f field / -X method,
    incl. ATTACHED short forms) is implementation and warn-then-blocks even UNCHAINED (codex P1)."""
    assert ost._seg_is_impl_signal(command.split("&&")[0].strip()) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_api_explicit_get_with_fields_is_read(tmp_path, monkeypatch):
    """An EXPLICIT GET with query fields (`--method GET -f state=open`) is a read, not a mutation —
    it must not warn/block (that would re-introduce flapping) (codex round 4)."""
    cmd = "gh api repos/o/r/issues --method GET -f state=open"
    assert ost._seg_is_impl_signal(cmd) is False
    assert ost._is_all_inline_allowed(cmd) is True
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": cmd}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


def test_prewrite_opt_out_follows_target_repo(tmp_path, monkeypatch):
    """The orchestrator opt-out for a pre-write follows the TARGET file's repo, not cwd (codex
    round 4). cwd is an opted-OUT repo, but the write TARGETS a strict (default-on) repo → still
    gated (blocks on repeat)."""
    strict = tmp_path / "strict"
    (strict / "src").mkdir(parents=True)
    (strict / "rig.yaml").write_text("agent_hooks:\n  all: true\n")  # no opt-out → default ON
    optout = tmp_path / "optout"
    optout.mkdir()
    (optout / "rig.yaml").write_text("agent_hooks:\n  orchestrator_only: false\n")
    event = {"point": "pre-write", "cwd": str(optout),
             "args": {"file_path": str(strict / "src" / "a.ts")}}
    _run(event, monkeypatch, tmp_path / "m")  # warn (gated because TARGET repo is strict)
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


def test_gh_pr_checkout_not_whitelisted(tmp_path, monkeypatch):
    """`gh pr checkout` mutates the local worktree/branch — it is NOT in the read-only/orchestration
    allow-list, so a chain built around it is judged on its full content (codex P2)."""
    assert ost._is_all_inline_allowed("gh pr checkout 5 && gh pr view 6 && tg x") is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh pr checkout 5 && gh pr view 6 && tg x"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_needle_in_read_only_pipe_allows(tmp_path, monkeypatch):
    """ANCHOR INVARIANT: `gh`/`tg` as an ARGUMENT of a read-only command is not orchestration —
    `git log | rg gh` stays allowed because the segment HEADS are read-only, not because of `gh`."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": "git log | rg gh"}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


# ── agent-tools#159: the allow-list must NOT launder a mutation past its head-anchor ─────────
# The head-anchored allow-list (`gh ship`/`gh pr`/`tg`/`review`/read-only) once waved through a
# mutation smuggled where the head cannot see it: a command/process substitution, a bare `&`
# background, a `git branch <arg>`, or a find delete/exec/file-write primary. Each of these was a
# REGRESSION vs the pre-existing hook (which blocked them all). Pin them shut.

@pytest.mark.parametrize("command", [
    "gh ship 605 $(sed -i 's/a/b/' f)",              # edit hidden in a command substitution
    "gh ship 605 & sed -i 's/a/b/' f.py",            # edit behind a bare `&` (not a chain split)
    "gh ship 605 & git push origin main; ls; cat x",  # push behind a bare `&`
    "cat <(git push origin main) && gh ship 605 | tail -3",  # push in a process substitution
    "gh ship 605; git branch -D tmp; ls",            # `git branch` mutating form
    "gh ship 605; git -C /repo branch -D tmp; ls",   # ...incl. the `git -C <dir> branch` form
    "gh ship 605 | find . -delete | tail -3",        # find delete primary
    "gh ship 605 | find . -fprintf evil.sh 'x' | tail",  # find file-WRITE primary
    "tg 'done' && npm run build",                    # a build does not ride a `tg` prefix
])
def test_allow_list_does_not_launder_smuggled_mutation(command, tmp_path, monkeypatch):
    """A benign orchestration head must not exempt a mutation the head-anchor cannot see — each of
    these warn-then-blocks, exactly as the pre-#159 hook did (regression guard)."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    "cd /repo && gh ship 605 | tail -40",            # `cd` companion (was wrongly blocked)
    "cd /repo && gh ship 605 --repo o/r --no-screenshot-ok | tail -3",  # cd + cross-repo + flag
    "gh ship 605 --title 'a & b' | tail",            # a quoted `&` must not trip the bare-`&` veto
    "gh ship 605 --note 'fix; reship' | tail",       # a quoted `;` must not split the segment
    "git branch",                                    # a bare `git branch` LIST stays read-only
])
def test_allow_list_covers_cd_and_quoted_reason(command, tmp_path, monkeypatch):
    """The `cd` companion and quote-aware handling must keep a legit ship/orchestration line
    allowed — it must never warn or prime a block (the whole point of the flapping fix)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow", command


def test_seg_and_inline_allowed_predicate_surface():
    """Pin the predicate surface for the #159 hardening at the unit level."""
    # cd is an allowed companion; `cd-clean`/`cd/foo` (argv boundary) are NOT.
    assert ost._seg_is_allowed("cd /repo") is True
    assert ost._seg_is_allowed("cd-clean") is False
    assert ost._seg_is_allowed("cd/foo") is False
    # find mutating primaries and `git branch <arg>` forfeit the allow-list; bare reads keep it.
    assert ost._seg_is_allowed("find . -delete") is False
    assert ost._seg_is_allowed("find . -fprintf out.sh x") is False
    assert ost._seg_is_allowed("git branch -D tmp") is False
    assert ost._seg_is_allowed("git -C /repo branch -D tmp") is False
    assert ost._seg_is_allowed("git branch") is True
    # A smuggled mutation is caught by _is_implementation_bash (impl / substitution-inner scan runs
    # BEFORE the fast path), NOT by a blanket veto in _is_all_inline_allowed — which honestly reports
    # head-allowance, so a benign read-only substitution pipe still passes it (the #80 fix).
    assert ost._is_implementation_bash("gh ship 605 $(sed -i x)") is True
    assert ost._is_implementation_bash("gh ship 605 & git push") is True
    assert ost._is_implementation_bash("gh ship 605 --title 'a & b' | tail") is False
    assert ost._is_implementation_bash("gh ship 605 --note 'a; b' | tail") is False
    assert ost._is_all_inline_allowed("cat $(find . -name x) | grep k | head") is True


def test_read_only_pipe_with_benign_substitution_not_blocked():
    """#80 invariant: a read-only pipe of ANY length is never blocked, even carrying a benign
    substitution — the substitution must not fall it into the >=3 fallback (Opus review)."""
    for cmd in ["cat $(find . -name conf.yaml) | grep -i key | head",
                "git log $(git merge-base a b) | grep foo | head"]:
        assert ost._is_implementation_bash(cmd) is False, cmd


def test_orchestrator_only_env_falsy_values_disable(monkeypatch):
    """RIG_ORCHESTRATOR_ONLY accepts the same falsy set as rig.yaml (0/false/no/off) — an env-only
    `!= "0"` check surprised users who set `=false` expecting it to exempt the repo (Opus review)."""
    for val in ("0", "false", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv("RIG_ORCHESTRATOR_ONLY", val)
        assert ost._orchestrator_only_enabled("/repo") is False, val
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("RIG_ORCHESTRATOR_ONLY", val)
        assert ost._orchestrator_only_enabled("/repo") is True, val


# ── codex review: the veto must MAKE it offending, not just drop the fast path ───────────────

@pytest.mark.parametrize("command", [
    "tg done & git commit -m x",          # bare-`&` background smuggles a commit (own segment now)
    "gh ship 605 & git push origin main",  # ...a push
    "gh ship 605 $(gh api repos/o/r/x -X POST)",  # a gh api MUTATION inside a substitution head
    "gtimeout 60 git commit -m x",        # gtimeout wrapper stripped like timeout (macOS coreutils)
    "gtimeout 60 pytest tests/",          # ...on a test run
    "gtimeout -k 5 60 git commit -m x",   # gtimeout with an option + duration positional
])
def test_smuggled_mutation_is_now_offending(command, tmp_path, monkeypatch):
    """A bare-`&`/substitution/gtimeout form that previously only lost the fast path but stayed
    non-offending now warn-then-blocks — the veto MAKES it implementation (codex review)."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_gh_run_watch_not_whitelisted_but_list_view_are():
    """`gh run watch` is a long-running block — dropped from the orchestration allow-list; the quick
    `gh run list`/`gh run view` inspections stay (codex review)."""
    assert ost.ORCH_ALLOW.search("gh run list") is not None
    assert ost.ORCH_ALLOW.search("gh run view 9") is not None
    assert ost.ORCH_ALLOW.search("gh run watch 123") is None
    # a read-only substitution and gtimeout-wrapped ship must stay allowed (no over-block).
    assert ost._is_implementation_bash("cat $(ls -t | head -1)") is False
    assert ost._is_implementation_bash("gtimeout 60 gh ship 605 | tail") is False


@pytest.mark.parametrize("command", [
    "df -h | grep /dev | head",           # read-only system-info verification pipe
    "lsblk | grep sda | wc -l",           # ...another
    "gh pr view 5 | jq .title | head",    # PR verification through jq/head
    "free -m | tail -1",                  # memory info
])
def test_read_only_system_verification_pipes_allow(command, tmp_path, monkeypatch):
    """Read-only system-info + filter verification tools (df/lsblk/free/…, jq/…) added to
    READ_ONLY_BASH so a multi-step verify pipe is not blocked by the >=3-segment rule (coordinator;
    SYNC with fix/159)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow", command


# ── tg#5743: commits / pushes / test runs by the orchestrator ARE implementation ────────────

@pytest.mark.parametrize("command", [
    "git commit -m x",       # commit inline (was allowed before — 0 chains, not a build)
    "git push",              # push inline
    "pytest tests/",         # test run
    "go test ./...",         # go test run
    "python -m pytest tests/",  # wrapper form (codex P2c)
    "tox",                   # tox test run
    "env git commit -m x",   # env-prefix bypass (codex P1)
    "CI=1 pytest tests/",    # leading VAR= assignment bypass (codex P1)
    "GIT_AUTHOR_NAME=x git commit -m x",  # leading VAR= assignment bypass (codex P1)
    "env pytest tests/",     # env wrapper on a test run (codex P1)
    'FOO="bar baz" git commit -m x',  # QUOTED env value with a space (codex)
    "BAR='a b' pytest tests/",        # single-quoted env value with a space (codex)
    "uv run --with pytest pytest tests/",       # uv-wrapped test run (codex)
    "uv run --with pytest python -m pytest",    # uv-wrapped python -m pytest (codex)
    "uv run tox",                                # uv-wrapped tox
    "env -u FOO git commit -m x",   # env option WITH operand (codex round 4)
    "env -C /tmp pytest tests/",    # env --chdir operand
    "timeout 60 pytest tests/",     # the MANDATED timeout wrapper (codex round 6)
    "timeout -k 5 60 git commit -m x",  # timeout with --kill-after option + duration
    "/usr/bin/env git commit -m x",     # absolute-path env (basename-matched)
    "time git push",                # time wrapper
    "nice -n 10 git commit -m x",   # nice with -n operand
    "uv run --env-file .env pytest tests/",  # uv --env-file operand skipped
    "env -i git commit -m x",       # env -i = ignore-env, NO operand (codex round 7)
    "env -i pytest tests/",         # env -i must not swallow pytest
    "python3 -m pytest tests/",     # python3 spelling — the repo's own form (codex round 8)
    "/usr/bin/python3 -m pytest",   # path-qualified python
    "python3 -m unittest discover",
    "git -C /repo commit -m x",     # git global option before subcommand (codex round 8)
    "git -c user.name=x commit -m x",
    "git --git-dir=.git --work-tree=. commit -m x",
    "/usr/bin/git commit -m x",     # path-qualified git
    "git -C /repo push",
])
def test_commit_push_test_blocks_on_repeat(command, tmp_path, monkeypatch):
    """Commits, pushes and test runs are a subagent's job — they warn-then-block for the
    orchestrator (Alex tg#5743), including behind a leading `env`/`VAR=val` wrapper (codex P1).
    Each was NOT caught before (a bare `git commit` had 0 chain operators and matched no build
    token; `env git commit` presented `env` as a read-only head)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


@pytest.mark.parametrize("command", [
    "uv run rig status",              # read-only tool, not a test
    "uv run rg pytest docs",         # SEARCHING for "pytest" — not running it (codex round 5)
])
def test_uv_run_readonly_tool_not_overblocked(command, tmp_path, monkeypatch):
    """The uv-test detector is shlex-based: only the COMMAND uv runs counts, not an argument.
    `uv run rig status` / `uv run rg pytest docs` must NOT be swept in as test runs (codex)."""
    assert ost._is_implementation_bash(command) is False, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "gh api repos/o/r/pulls | jq '.[] | select(.state==\"open\")'",  # quoted `|` inside jq
    "git log --oneline | rg 'feat|fix' | head",                       # quoted alternation
])
def test_quoted_pipe_inside_arg_not_split(command, tmp_path, monkeypatch):
    """A `|` inside a QUOTED argument (a jq program, an rg alternation) is not a chain operator —
    quote-aware splitting keeps such a read chain allowed instead of flapping (codex round 5)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow", command


@pytest.mark.parametrize("command", [
    "gh api repos/o/r/issues --method=GET -f state=open",  # --method=GET spelling
    "gh api repos/o/r/issues -X GET -q '.[]'",             # -X GET spelling
])
def test_gh_api_get_spellings_are_reads(command, tmp_path, monkeypatch):
    """All GET spellings (`--method=GET`, `-X GET`) are reads even with query fields — shlex-parsed
    effective-method detection, no false block (codex round 5)."""
    assert ost._gh_api_is_mutation(command) is False, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow", command


def test_gh_api_double_method_last_wins():
    """`--method GET --method POST` → the LAST method wins (POST) → mutation (codex round 5)."""
    assert ost._gh_api_is_mutation("gh api repos/o/r --method GET --method POST") is True


@pytest.mark.parametrize("command", [
    "timeout 60 git status",        # the mandated timeout wrapper on a READ-ONLY command
    "timeout 60 gh pr list && timeout 60 gh pr view 5",  # timeout on orchestration, chained
    "/usr/bin/env git status",      # absolute-path env on a read-only command
    "git -C /r status && git -C /r log && git -C /r diff",  # read-only git with -C, 3-chain (round 8)
    "/usr/bin/git log --oneline",   # path-qualified read-only git
])
def test_wrappers_do_not_break_read_or_orchestration(command, tmp_path, monkeypatch):
    """Stripping wrappers must not turn a read/orchestration command into an offense — `timeout N
    git status` and `timeout N gh pr list && …` stay allowed, never warn/block (codex round 6)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow", command


# ── tg#5743: per-repo opt-out (default ON, no regression) ───────────────────────────────────

def test_opt_out_via_env_allows_code_write(tmp_path, monkeypatch):
    """RIG_ORCHESTRATOR_ONLY=0 exempts a repo entirely — even a repeat code write allows."""
    event = {"point": "pre-write", "cwd": "/repo", "args": {"file_path": "/repo/src/a.ts"}}
    _run(event, monkeypatch, tmp_path / "m", {"RIG_ORCHESTRATOR_ONLY": "0"})  # would-be warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m", {"RIG_ORCHESTRATOR_ONLY": "0"})
    assert c == 0 and _decision(out) == "allow"


def test_opt_out_via_rigyaml_allows_code_write(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text("agent_hooks:\n  orchestrator_only: false\n")
    event = {"point": "pre-write", "cwd": str(repo), "args": {"file_path": str(repo / "src/a.ts")}}
    _run(event, monkeypatch, tmp_path / "m")
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == 0 and _decision(out) == "allow"


def test_blank_yaml_value_keeps_the_default(tmp_path, monkeypatch):
    """A blank value (`orchestrator_only:`) must return the DEFAULT, not silently disable a
    default-ON gate (codex P2). Default-off worktree_only stays off for a blank too."""
    assert ost._agent_hooks_bool(
        "agent_hooks:\n  orchestrator_only:\n", "orchestrator_only", default=True) is True
    assert ost._agent_hooks_bool(
        "agent_hooks:\n  worktree_only:\n", "worktree_only", default=False) is False


def test_default_on_still_blocks_when_no_rigyaml(tmp_path, monkeypatch):
    """No env, no rig.yaml → gate stays ON (opt-OUT default) — no regression vs prior always-on."""
    event = {"point": "pre-write", "cwd": str(tmp_path / "nowhere"),
             "args": {"file_path": str(tmp_path / "nowhere/src/a.ts")}}
    _run(event, monkeypatch, tmp_path / "m")  # warn
    out, _e, c = _run(event, monkeypatch, tmp_path / "m")
    assert c == ost.BLOCK_EXIT_CODE and _decision(out) == "block"


# ── #159: `gh ship` is RELEASE, not implementation — the orchestrator's own carve-out ────

@pytest.mark.parametrize("command", [
    "gh ship 605",                                             # bare ship (pins the trivial case)
    "gh ship 605 2>&1 | tail -30 | grep -i merged",            # ship + read-only plumbing (2 ops)
    "gh ship 605 --skip-ci | tail -20; git log --oneline -3",  # ship + post-merge inspection
    "GH_PAGER=cat GH_TOKEN=x gh ship 605 | tail -5 | head -1",  # env-var prefixes on the ship head
    "cd /repo && gh ship 605 | tail -40",                      # `cd` companion segment
    "git status && gh ship 605 && git log --oneline -1",       # read-only companions via &&
    "gh ship 605 > ship.log 2>&1",                             # the sanctioned logging shape
    "gh ship 605 --repo alex-mextner/agent-tools",             # cross-repo ship: `--repo <owner/repo>`
    "gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok",  # + the no-screenshot flag
    "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3",  # all three
    "gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3",  # quoted `;` in a reason arg (F2/P2)
])
def test_gh_ship_release_chain_allows(command, tmp_path, monkeypatch):
    """A `gh ship` line (plus read-only/`cd` plumbing) is the sanctioned RELEASE action at
    ORCHESTRATOR altitude (#159): the auto-mode classifier denies subagents the gated merge,
    so the orchestrator must run it itself — it must never warn and never prime the block
    tier (the warn-then-block flapping on repeated ships was the gated-ship deadlock)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    assert "message" not in json.loads(out1)  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow"


@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' f.py && gh ship 605",   # ship does not launder an in-place edit
    "npm run build && gh ship 605",          # ...nor a build
    "gh ship 605; tee out.txt; ls",          # ...nor a tee write
    "gh ship 605 $(sed -i 's/a/b/' f)",      # ...nor an edit hidden in a substitution
    "cd $(npm run build) && gh ship 605",    # ...nor a build inside the cd companion
    "gh ship 605 & sed -i 's/a/b/' f.py",    # ...nor behind a single `&` (CHAIN can't split it)
    "cd $(git push origin main) && gh ship 605 | tail -3",  # non-BUILD_EDIT mutation in $() (2 ops)
    "gh ship 605 & git push origin main; ls; cat x",  # bare `&` hides a push in the ship segment
    "cat <(git push origin main) && gh ship 605 | tail -3",  # process substitution smuggles a push
    "gh ship 605 | find . -delete | tail -3",           # read-only HEAD, mutating flag (#80 gap)
    "gh ship 605 | env git push origin main | tail -3",  # env wrapper launders a push
    "gh ship 605; git branch -D tmp; ls",                # git branch mutating form
    "gh ship 605 | find . -fprintf evil.sh 'x' | tail -3",  # find file-WRITE primary (review F1)
    "cd-clean && gh ship 605 | tail -3",                    # `cd-clean` is a different command (P1)
])
def test_gh_ship_does_not_launder_impl_segments(command, tmp_path, monkeypatch):
    """The allowance is per-segment: a `gh ship` tacked onto an implementation chain must NOT
    exempt the rest of the line (#159) — only all-(ship|read-only|cd) lines pass. A mutation
    smuggled where no head can see it (inside `$()`/`<()`/backticks, behind a bare `&`, a
    mutating find/git-branch form) is caught by the substitution-inner scan, the `&` split and
    the companion guards — the exemption must not regress what was blocked pre-carve-out."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


@pytest.mark.parametrize("command", [
    "python x.py | grep 'gh ship' | python y.py",  # needle in a non-read-only pipe
    "gh shipwreck 1 | python x.py | head",         # word boundary: not the ship subcommand
])
def test_gh_ship_needle_or_prefix_word_is_not_exempt(command, tmp_path, monkeypatch):
    """`gh ship` counts only at a segment HEAD (argv), never as a substring in text (#159) —
    a grep needle or a `gh ship*`-prefixed other word must not self-exempt a chain."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


# NOTE (rebase over #162): the old #162 test "bare-& zero-operator line keeps inherited allow"
# (`gh ship 605 & git push origin main` allowed) is deliberately DROPPED here: this branch's
# `_split_chain` splits a bare control `&`, so the smuggled `git push` is judged on its own
# segment and the line warn-then-blocks — pinned by test_smuggled_mutation_is_now_offending.
# Stricter than #162's inherited behavior, in the safe direction.


def test_gh_ship_with_non_build_mutation_companion_blocks(tmp_path, monkeypatch):
    """A companion mutation that BUILD_EDIT does not know about (git push) still does not
    ride the carve-out: it is not a benign head, so a >=2-operator chain stays
    implementation-shaped and warn-then-blocks exactly as before (#159 review)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "gh ship 605 && git push origin main && git push --tags"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_gh_ship_with_heredoc_still_blocks(tmp_path, monkeypatch):
    """A heredoc anywhere vetoes the release carve-out, exactly as it vetoes read-only (#159)."""
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": "cat <<EOF > notes\nbody\nEOF\ngh ship 605 | tail -5"}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"


def test_release_judgement_matrix():
    """The #162 release-carve-out guarantees, re-pinned against THIS branch's design (there is
    no `_is_sanctioned_release_chain` here — `gh ship` rides the head-anchored ORCH_ALLOW
    allow-list + the smuggling guards). Asserted on `_is_implementation_bash` (True = blocked).
    Where this branch is deliberately STRICTER than #162's inherited behavior, the stricter
    verdict is pinned (safe direction) — noted inline."""
    impl = ost._is_implementation_bash
    # sanctioned release shapes stay allowed
    assert impl("gh ship 605 | tail -3 | head -1") is False
    assert impl("gh ship 605 2>&1 | tail -3") is False
    assert impl("grep 'gh ship' log | head | wc -l") is False  # needle in a read-only pipe
    # `gh ship` forms the orchestrator actually runs (task #23) — the gate does NOT validate
    # ship's own flags (that is ship's job downstream): `--repo <owner/repo>`, `--no-screenshot-ok`.
    assert impl("gh ship 605 --repo alex-mextner/agent-tools") is False
    assert impl("gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok") is False
    assert impl(
        "cd /repo && gh ship 605 --repo alex-mextner/agent-tools --no-screenshot-ok | tail -3") is False
    # a BENIGN read-only substitution no longer forfeits the pass (stricter-only where it
    # matters: #162 vetoed ALL substitutions out of the carve-out; here the inner command is
    # actually judged, so a read-only one passes and a mutating one blocks).
    assert impl("cd $(git rev-parse --show-toplevel) && gh ship 605") is False
    # non-allowed companion heads forfeit it (>=3 segments, no benign judgement)
    assert impl("gh ship 605 | writer cat | tool grep") is True
    # a companion mutation is caught — incl. forms #162 could not see:
    assert impl("gh ship 605 && git push") is True  # STRICTER than #162 (impl-signal per segment)
    assert impl("gh ship 605 & git push origin main") is True  # STRICTER: bare `&` is a split here
    assert impl("gh ship 605 | cat <(git push origin main)") is True  # process-subst inner judged
    assert impl("tee >(wc -l) | gh ship 605") is True  # tee is BUILD_EDIT
    assert impl("gh ship 605 | find . -delete | tail -3") is True
    assert impl("gh ship 605 | find . -name x | head") is False  # read-only find companion is fine
    assert impl("gh ship 605 | find . -fprintf evil.sh 'x' | tail -3") is True  # find file-WRITE
    assert impl("cd-clean && gh ship 605 | tail -3") is True  # `cd-clean` is not the `cd` companion
    # quoted metachars in a ship reason keep the pass; LIVE double-quoted substitutions do not
    # (quote semantics, #162/#164 review P2): `$()`/backticks execute inside double quotes.
    assert impl("gh ship 605 --no-screenshot-ok 'revert; reship' | tail -3") is False
    assert impl('gh ship 605 --title "a & b" | tail -3') is False
    assert impl("gh ship 605 --note 'ran $(build) earlier' | tail -3") is False
    assert impl('gh ship 605 --note "$(git push origin main)" | tail -3') is True
    assert impl('gh ship 605 --note "`git push origin main`" | tail -3') is True
    assert impl("gh ship 605 --note '$(git push origin main)' | tail -3") is False


# ── coordinator: report (`tg`) + read-only verification are orchestrator altitude, not impl ──────

@pytest.mark.parametrize("command", [
    "tg 'msg'",                                           # plain report (the mandatory case)
    "tg --format html '<b>done</b>' | tail -3",           # report + read-only plumbing (a pipe)
    "tg --format html 'x' | tail -3 | grep merged",       # 2-operator report chain (used to block)
    "gh pr view 5 | jq .title | head -1",                 # PR verification piped through jq/head
    "tg done; gh pr view 5; gh run list",                 # report + verify, 3 segments
    "df -h | grep /dev | head",                           # read-only system verification
    "lsblk | grep sda | wc -l",                           # ...another
    "cd /repo && tg 'done' | tail",                       # `cd` companion on a report line
])
def test_report_or_verify_chain_allows(command, tmp_path, monkeypatch):
    """`tg` reporting and read-only PR/CI/system verification are the orchestrator's OWN altitude —
    a multi-step report/verify chain must never warn OR prime a block (coordinator directive)."""
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command
    assert "message" not in json.loads(out1), command  # does not even warn
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")  # never primes a block
    assert c2 == 0 and _decision(out2) == "allow", command


@pytest.mark.parametrize("command", [
    "tg done && sed -i 's/a/b/' f.py",    # report does not launder an in-place edit
    "tg done; tee out.txt; ls",           # ...nor a tee write
    "tg done $(sed -i 's/a/b/' f)",       # ...nor an edit hidden in a substitution
    "tg done; git branch -D tmp; ls",     # ...nor a git branch mutation (>=2 operators)
    "gh pr view 5 && git push && git push --tags",  # ...nor a >=2-operator push chain
])
def test_report_or_verify_does_not_launder_impl(command, tmp_path, monkeypatch):
    """The report/verify allowance is per-segment like the release one: a `tg`/gh-read head does
    not exempt a mutation elsewhere on the line (coordinator directive) — it warn-then-blocks."""
    assert ost._is_implementation_bash(command) is True, command
    event = {"point": "pre-bash", "cwd": "/repo", "args": {"command": command}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow", command  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block", command


def test_report_verify_allowlist_surface():
    """Pin the doctrine (re-based over #162): `tg` and the gh reads are sanctioned orchestration
    heads (ORCH_ALLOW), while `curl` and `ssh` are NOT — curl can POST and ssh runs any remote
    command, so neither is reliably read-only; they never ride the allow-list."""
    assert ost.ORCH_ALLOW.search("tg 'x'") is not None
    assert ost.ORCH_ALLOW.search("gh pr checks 5") is not None
    assert ost.ORCH_ALLOW.search("curl -X POST http://h/api") is None
    assert ost.ORCH_ALLOW.search("ssh root@h 'df -h'") is None
    impl = ost._is_implementation_bash
    assert impl("tg 'x' | tail") is False
    assert impl("gh pr checks 5 | grep fail | head") is False
    assert impl("git log | grep x | head") is False  # plain read-only path, no tg/gh-read needed
    # a chain fronted by a non-sanctioned head is judged on its full content — a 3-segment curl
    # chain is impl-shaped, and a bare-`&` push behind a tg report is caught by the `&` split.
    assert impl("curl -X POST http://h/api | tg done | tail") is True
    assert impl("tg done & git push origin main; ls") is True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_double_quoted_substitution_is_live_and_judged(tmp_path, monkeypatch):
    """Quote SEMANTICS (#164 review P2): `$(…)`/backticks EXECUTE inside DOUBLE quotes, so a
    double-quoted mutating substitution must be extracted and judged (warn-then-block); a
    SINGLE-quoted one is literal text and stays allowed."""
    assert ost._is_implementation_bash('gh ship "$(gh api repos/o/r/issues -X POST -f title=x)"') is True
    assert ost._is_implementation_bash('gh ship 605 --note "$(git push origin main)" | tail -3') is True
    assert ost._is_implementation_bash('tg "`git commit -m x`"') is True
    assert ost._is_implementation_bash("tg 'saw $(git push) in logs'") is False
    # read-only substitutions stay allowed in either quote form
    assert ost._is_implementation_bash('gh pr view "$(gh pr list --json number -q .n)"') is False
    event = {"point": "pre-bash", "cwd": "/repo",
             "args": {"command": 'gh ship "$(gh api repos/o/r/issues -X POST -f title=x)"'}}
    out1, _e1, c1 = _run(event, monkeypatch, tmp_path / "m")
    assert c1 == 0 and _decision(out1) == "allow"  # first offense WARNs
    out2, _e2, c2 = _run(event, monkeypatch, tmp_path / "m")
    assert c2 == ost.BLOCK_EXIT_CODE and _decision(out2) == "block"
