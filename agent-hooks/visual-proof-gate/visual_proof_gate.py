#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash + stop hook — visual proof for visible changes.

Enforcement-doctrine gate 5 (ROADMAP "DOCTRINE"): a "done" claim or a commit on a
USER-VISIBLE change (UI, a rendered image, a chart, generated output) with NO screenshot
the agent actually LOOKED AT is not proof — "it should work" is not "it works"
(visual-proof-cycle, enforced).

How it works — ONE script, TWO points (a descriptor per point points here):
  - pre-bash  : a ``git commit`` whose commit set touches user-visible files → CHECK a
    fresh screenshot artifact exists; warn first, then block.
  - stop      : the turn is ending (the "done" moment) → same CHECK, keyed per session so
    it doesn't loop.

What counts as PROOF (satisfiable by an honest action, clause a): a REAL, freshly-modified
screenshot/image artifact the agent can have looked at — either
  1. named by an inline ``screenshot: <path>`` token (and not itself part of the commit), OR
  2. a fresh image in the working tree that is NOT part of the commit set (a verification
     capture taken to check the change).
Crucially, INVOKING the ``visual-proof-cycle`` skill is NOT by itself proof — intent isn't
evidence (an agent could invoke it and produce nothing). Only a real file on disk with a
recent mtime counts, and it can't be the committed asset itself (no self-proof). This makes
the gate satisfiable only by actually capturing — which is the whole point.

What is USER-VISIBLE: changes to UI/asset/render files (``.tsx/.jsx/.vue/.svelte/.css/
.html/.svg/.png/...``) or a chart/render output. A pure-backend/docs change is NOT gated
(the gate would be unsatisfiable and brittle otherwise).

TIERED (clause b): warn on the first commit/stop without proof, block the next within the
window. SCOPED: applies to BOTH orchestrator and subagents (whoever ships a visible change
owes proof) — NOT one of the subagent-exempt gates 1-3. HONEST escape hatch (clause d):
``SKIP_VISUAL_PROOF=1`` + ``SKIP_VISUAL_PROOF_REASON='why'`` (e.g. headless CI), or an
inline ``# visual-proof-ok: why`` in a commit — a real, auditable skip.

Contract (agents-hooks/v1):
  stdin  : JSON event; point in event.point, command in args.command, cwd in event.cwd
  stdout : protocol JSON only   exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a discipline reminder must never trap the agent unable to commit/stop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess  # noqa: S404 — `git diff --cached --name-only` to scope the commit
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

MARKER_DIR = Path(
    os.path.expanduser(os.environ.get("VISUAL_PROOF_MARKER_DIR", "~/.cache/agent-tools/visual-proof"))
)
WARN_WINDOW_S = int(os.environ.get("VISUAL_PROOF_WARN_WINDOW_S", "1800"))
# How recent a screenshot file must be to count as "the one the agent just looked at".
SHOT_FRESH_S = int(os.environ.get("VISUAL_PROOF_SHOT_FRESH_S", "3600"))

# User-visible source/asset files — a change here is something a human SEES.
_VISIBLE_FILE = re.compile(
    r"\.(tsx|jsx|vue|svelte|css|scss|sass|less|html|htm|svg|png|jpg|jpeg|gif|webp|pdf|"
    r"mdx|stories\.[tj]sx?)$",
    re.IGNORECASE,
)
# Image/screenshot files that count as the proof artifact.
_SHOT_FILE = re.compile(r"\.(png|jpg|jpeg|gif|webp|pdf|svg)$", re.IGNORECASE)


# SYNC: the shell-segmenter + is_git_commit mirror skills-read-gate (and the argv parsing in
# require-ticket-before-commit). A naive `\bgit commit\b` misses `git -C /repo commit`, and a
# plain shlex.split misses `git status;git commit`. Keep all in step.
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "|&", "&", "\n"})


def _shell_segments(command: str) -> list[list[str]]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    tokens = list(lex)
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEPARATORS:
            segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    segments.append(cur)
    return segments


def _is_git(tok: str) -> bool:
    return tok in ("git", "/usr/bin/git") or tok.endswith("/git")


def _subcommand_index(tokens: list[str]) -> int:
    """Index of a git segment's SUBCOMMAND (the first token after the git global options),
    or -1 if the segment isn't a git invocation. Walks past `-C dir`, `-c k=v`, etc."""
    if not tokens or not _is_git(tokens[0]):
        return -1
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return i
    return -1


def _commit_subindex(tokens: list[str]) -> int:
    """Index of the `commit` subcommand in a git segment, or -1 if this isn't a git commit.
    A rebase/merge step (`commit --continue/--abort/--skip`) returns -1 (not authoring)."""
    i = _subcommand_index(tokens)
    if i < 0 or tokens[i] != "commit":
        return -1
    if any(r in ("--continue", "--abort", "--skip") for r in tokens[i + 1:]):
        return -1
    return i


def _git_commit_segment(command: str) -> list[str] | None:
    """The first `git commit` segment's tokens, or None — the unit the gate scopes to."""
    try:
        segments = _shell_segments(command)
    except ValueError:
        return None
    for seg in segments:
        if _commit_subindex(seg) >= 0:
            return seg
    return None


def is_git_commit(command: str) -> bool:
    try:
        return _git_commit_segment(command) is not None
    except ValueError:
        return "git" in command and "commit" in command


def _git_dash_C(tokens: list[str]) -> str | None:
    """The `-C <dir>` target of a git segment (last wins), resolved by the caller against cwd."""
    target = None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-C" and i + 1 < len(tokens):
            target = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            target = tok[2:]
        i += 1
    return target


def _commit_pathspecs(tokens: list[str], commit_idx: int) -> list[str]:
    """Explicit pathspecs after `git commit` (`git commit Button.tsx`). These restrict the
    commit to those paths — the gate must scope to them, not the whole index."""
    paths: list[str] = []
    i = commit_idx + 1
    saw_ddash = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            saw_ddash = True
            i += 1
            continue
        if not saw_ddash and tok in ("-m", "--message", "-F", "--file", "--author", "--date", "-C", "-c"):
            i += 2  # flag + its value
            continue
        if not saw_ddash and tok.startswith("-"):
            i += 1
            continue
        paths.append(tok)
        i += 1
    return paths


def _commits_all_tracked(tokens: list[str]) -> bool:
    """A `git commit -a`/`--all` also lands modified-but-unstaged tracked files."""
    for tok in tokens:
        if tok == "--all" or (tok.startswith("-") and not tok.startswith("--") and "a" in tok[1:]):
            return True
    return False


def _stages_before_commit(command: str) -> bool:
    """True when the command STAGES files in the same line as the commit, so the index is
    stale when this pre-bash hook fires (codex): a chained ``git add … && git commit`` or a
    ``git commit -a``. In that case the gate must consider the whole working tree, since the
    files about to be staged aren't in ``git diff --cached`` yet."""
    try:
        segments = _shell_segments(command)
    except ValueError:
        return "git add" in command or "git stage" in command
    for seg in segments:
        idx = _subcommand_index(seg)
        if idx < 0:
            continue
        sub = seg[idx]
        # `git add` / `git stage` anywhere in the line, or `git commit -a` itself.
        if sub in ("add", "stage"):
            return True
        if sub == "commit" and _commits_all_tracked(seg):
            return True
    return False


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"visual-proof-gate: {msg}\n")


# SYNC: keep _is_truthy identical across the five enforcement-doctrine hooks (see
# background-subagent-gate). is_subagent() is NOT used here — gate 5 applies to subagents too.
def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() not in ("", "0", "false", "no", "off")


def session_id(event: dict) -> str:
    args = event.get("args") or {}
    raw = (
        (args.get("session_id") if isinstance(args, dict) else None)
        or event.get("event_id")
        or event.get("cwd")
        or "default"
    )
    return hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def _stop_marker(sid: str) -> Path:
    return MARKER_DIR / f"{sid}.stopped"


def _warn_marker(sid: str) -> Path:
    return MARKER_DIR / f"{sid}.warned"


def _fresh(p: Path, ttl: int) -> bool:
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) <= ttl
    except OSError:
        return False


def _git_lines(cwd: str, args: list[str]) -> list[str]:
    try:
        proc = subprocess.run(  # noqa: S603,S607 — fixed git argv in a trusted dir
            ["git", *args],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"git {' '.join(args)} failed ({exc}); treating as non-visible")
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _effective_cwd(cwd: str, segment: list[str]) -> str:
    """The repo the commit acts on — `git -C <dir>` overrides the event cwd (codex P2)."""
    target = _git_dash_C(segment)
    if not target:
        return cwd
    target = os.path.expanduser(target)
    if cwd and not os.path.isabs(target):
        target = os.path.join(cwd, target)
    return target


def commit_files(cwd: str, segment: list[str]) -> list[str]:
    """The files THIS commit will actually land — honoring `git -C`, explicit pathspecs, and
    `-a`/`--all`. Scoping to the commit set (not the whole working tree, and the RIGHT repo)
    is what stops an unrelated/wrong-cwd file from mis-gating the commit (codex P2)."""
    repo_cwd = _effective_cwd(cwd, segment)
    commit_idx = _commit_subindex(segment)
    pathspecs = _commit_pathspecs(segment, commit_idx) if commit_idx >= 0 else []
    if pathspecs:
        # an explicit `git commit <paths>` commits ONLY those paths (plus the index for them);
        # the pathspecs ARE the commit set the human will see.
        return pathspecs
    files = [p for p in (ln.strip() for ln in _git_lines(repo_cwd, ["diff", "--cached", "--name-only"])) if p]
    if _commits_all_tracked(segment):
        files += [p for p in (ln.strip() for ln in _git_lines(repo_cwd, ["diff", "--name-only"])) if p]
    return files


def working_tree_files(cwd: str) -> list[str]:
    """Every changed path in the working tree (staged + unstaged + untracked) — used for the
    Stop gate, where there is no specific commit to scope to."""
    files: list[str] = []
    for line in _git_lines(cwd, ["status", "--porcelain"]):
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            files.append(path)
    return files


def touches_visible(files: list[str]) -> bool:
    return any(_VISIBLE_FILE.search(f) for f in files)


def fresh_screenshot(cwd: str, commit_set: set[str], command: str) -> bool:
    """A real, freshly-modified screenshot the agent LOOKED AT = proof.

    Honest AND non-circular (codex P2): an image that is ITSELF part of the commit does NOT
    count — editing ``logo.png`` can't be its own proof. Proof is either:
      (a) an explicit ``screenshot: <path>`` token — the capture the agent names; OR
      (b) a fresh screenshot in the working tree that is NOT part of the commit set (a
          verification capture the agent took to check the change, sitting outside the diff).
    Either way the file must exist on disk with a recent mtime — not a claim.
    """
    candidates: list[str] = []
    m = re.search(r"screenshot:\s*(\S+)", command)
    if m and m.group(1) not in commit_set:
        # an explicitly-named capture counts ONLY if it isn't itself part of the commit —
        # otherwise `git commit logo.png # screenshot: logo.png` would be its own proof (codex P2).
        candidates.append(m.group(1))
    # a fresh image present in the working tree but NOT in this commit = a verification shot.
    for f in working_tree_files(cwd):
        if _SHOT_FILE.search(f) and f not in commit_set:
            candidates.append(f)
    base = Path(cwd) if cwd else Path.cwd()
    for c in candidates:
        p = Path(c)
        if not p.is_absolute():
            p = base / c
        try:
            if p.is_file() and (time.time() - p.stat().st_mtime) <= SHOT_FRESH_S:
                return True
        except OSError:
            continue
    return False


def _escape_reason(command: str) -> str | None:
    if _is_truthy(os.environ.get("SKIP_VISUAL_PROOF")):
        reason = (os.environ.get("SKIP_VISUAL_PROOF_REASON") or "").strip()
        if reason:
            return f"env override: {reason}"
    m = re.search(r"#\s*visual-proof-ok:\s*(\S.*)", command)
    if m:
        return f"inline override: {m.group(1).strip()}"
    return None


def _has_proof(sid: str, cwd: str, commit_set: set[str], command: str) -> bool:
    """Proof is a REAL fresh screenshot artifact — never a bare intent marker.

    A `visual-proof-cycle` skill invocation alone does NOT clear the gate (codex): invoking
    the skill is intent, not evidence; the agent could invoke it and produce nothing. The
    only thing that counts is an actual, freshly-modified screenshot file the agent can have
    looked at — named via `screenshot:` or sitting in the working tree outside the commit.
    """
    return fresh_screenshot(cwd, commit_set, command)


def _gate(sid: str, cwd: str, files: list[str], command: str, *, is_stop: bool) -> int:
    if not touches_visible(files):
        emit("allow")
        return 0
    if _has_proof(sid, cwd, set(files), command):
        emit("allow")
        return 0

    reason = _escape_reason(command)
    if reason:
        warn(f"visual-proof gate skipped via escape hatch ({reason})")
        emit("allow", f"skipped via escape hatch ({reason})")
        return 0

    what = "finishing" if is_stop else "committing"
    msg = (
        f"You are {what} a user-visible change with no screenshot you actually looked at. "
        "'It should work' is not 'it works'. Run the visual-proof-cycle skill (capture → "
        "read the capture back → critique → re-capture) — that records the proof — or add a "
        "freshly-captured screenshot file and reference it. Honest skip (auditable, e.g. "
        "headless CI): set SKIP_VISUAL_PROOF=1 and SKIP_VISUAL_PROOF_REASON='why', or append "
        "`# visual-proof-ok: why` to the commit."
    )

    # TIERED: warn first, block the repeat within the window. (The bridge maps the v1 block
    # to the right CC shape per event — commit vs stop — so we just exit 10 + message.)
    wm = _warn_marker(sid)
    if _fresh(wm, WARN_WINDOW_S):
        emit("block", msg)
        return BLOCK_EXIT_CODE
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        wm.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        warn(f"could not write warn marker: {exc}")
    warn("visible change without visual proof — warning first; a repeat will block")
    emit("allow", msg)
    return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    point = event.get("point") or ""
    cwd = event.get("cwd") or args.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = str(cwd)
    sid = session_id(event)

    if point == "pre-bash":
        command = args.get("command") or args.get("cmd") or ""
        if not isinstance(command, str):
            command = str(command)
        segment = _git_commit_segment(command)
        if segment is None:
            emit("allow")
            return 0
        # scope to the COMMIT set (staged + tracked-modified for `-a` + explicit pathspecs),
        # in the RIGHT repo (`git -C`), not the whole working tree — so an unrelated/wrong-cwd
        # file can't mis-gate the commit (codex P2).
        repo_cwd = _effective_cwd(cwd, segment)
        if _stages_before_commit(command):
            # a chained `git add … && git commit` (or `git commit -a`) hasn't staged yet when
            # this pre-bash hook fires, so the index is STALE. Conservatively consider the
            # whole working tree — else the visible files the `add` is about to stage slip
            # past the gate (codex).
            files = working_tree_files(repo_cwd)
        else:
            files = commit_files(cwd, segment)
        return _gate(sid, repo_cwd, files, command, is_stop=False)

    if point == "stop":
        # block the stop AT MOST ONCE per session, like stop-completion-selfcheck, so a
        # genuine no-proof stop can still finish after the agent sees the message once.
        sm = _stop_marker(sid)
        if _fresh(sm, WARN_WINDOW_S):
            emit("allow")
            return 0
        # at stop there is no specific commit → consider the whole working tree, and a fresh
        # screenshot anywhere in it (none of it is "committed" yet) can serve as the proof.
        files = working_tree_files(cwd)
        if not touches_visible(files) or _has_proof(sid, cwd, set(), ""):
            emit("allow")
            return 0
        if _escape_reason(""):
            emit("allow")
            return 0
        try:
            MARKER_DIR.mkdir(parents=True, exist_ok=True)
            sm.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            warn(f"could not write stop marker: {exc}")
        emit(
            "block",
            "Before finishing: there's a user-visible change with no screenshot you looked "
            "at. Run the visual-proof-cycle skill (capture → read it back → critique) and "
            "confirm it renders, or skip with SKIP_VISUAL_PROOF=1 + SKIP_VISUAL_PROOF_REASON.",
        )
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
