"""Tests for the no-shell-file-edit agent-hook (pre-bash, hard block).

Covers the rule "edits only via Edit/Write; no sed/perl/awk for editing files":
  BLOCK     — `sed -i`/`perl -i`/`gawk -i inplace` on a tracked source file, a `> file` /
              `>> file` redirect onto a tracked source file, the same wrapped in `bash -c`.
  ALLOW     — read-only sed/awk/grep filters, a redirect to /tmp or a new/non-source file,
              `-i` on an untracked path, and the raw-string DECOYS that must NOT trip it
              (the flag inside a string operand / a comment / a commit message).
  HATCH     — deny-by-default Telegram escalation. The old ALLOW_SHELL_FILE_EDIT env +
              `# shell-file-edit-ok:` sentinel are DEAD; RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT
              with a written justification asks tg-ctl and allows only on exit 0, a bare `1` denies.

The tracked-source detection is exercised against a REAL temp git repo (no mock), so the
`git ls-files` boundary is what's actually under test.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_no_shell_file_edit.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "no-shell-file-edit"
    / "no_shell_file_edit.py"
)
_spec = importlib.util.spec_from_file_location("no_shell_file_edit", _HOOK)
assert _spec and _spec.loader
nsfe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nsfe)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A temp git repo with `app.ts` (source) and `notes.md` (non-source) TRACKED, plus an
    untracked `scratch.ts`. The redirect/-i cases resolve tracked-ness against this real repo.

    `core.hooksPath=` empties the hooks dir so the machine's GLOBAL pre-commit gate
    (review-before-commit etc.) can't block this fixture's own bootstrap commit."""
    def git(*argv: str) -> None:
        subprocess.run(
            ["git", "-c", "core.hooksPath=", *argv], cwd=tmp_path, check=True,
            capture_output=True, text=True,
        )

    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (tmp_path / "app.ts").write_text("export const a = 1\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x = 1\n")
    git("add", "app.ts", "notes.md", "src/x.py")
    git("commit", "-qm", "init")
    (tmp_path / "scratch.ts").write_text("// untracked\n")  # exists but NOT tracked
    return tmp_path


def _run(command, repo: Path, monkeypatch, *, env: dict | None = None) -> tuple[str, str, int]:
    event = {"args": {"command": command}, "cwd": str(repo)}
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_SHELL_FILE_EDIT", "ALLOW_SHELL_FILE_EDIT_REASON",
              "RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = nsfe.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK: in-place editor on a tracked source file ──────────────────────────────────────

@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' app.ts",
    "sed -i.bak 's/a/b/' app.ts",
    "sed --in-place 's/a/b/' app.ts",
    "perl -i -pe 's/a/b/' app.ts",
    "perl -pi -e 's/a/b/' app.ts",           # `-i` bundled into a `-pi` cluster
    "perl -i.orig -pe 's/a/b/' app.ts",
    "gawk -i inplace '{print}' app.ts",
    "awk -i inplace '{print}' app.ts",       # review round 6: awk (not just gawk) in-place
    "mawk -i inplace '{print}' app.ts",      # mawk family, same conservative in-place block
    "sed -i 's/x/y/' src/x.py",              # nested tracked source path
    "timeout 5 sed -i 's/a/b/' app.ts",      # wrapped — still caught
    "env X=1 perl -i -pe 's/a/b/' app.ts",
    # codex F1: `-i` clustered AFTER another short flag (not first) — these slipped through.
    "sed -Ei 's/a/b/' app.ts",               # -E (extended regex) + -i
    "sed -ri 's/a/b/' app.ts",               # -r (GNU extended regex) + -i
    "sed -ni 's/a/b/p' app.ts",              # -n (quiet) + -i
    "sed -Ei.bak 's/a/b/' app.ts",           # cluster + in-place extension
    # codex round 4 (HIGH): a leading VAR=val assignment prefix is the most common bypass —
    # `LANG=C`/`LC_ALL=C` in front of the editor. The assignment run is peeled before matching.
    "LANG=C sed -i 's/a/b/' app.ts",
    "LC_ALL=C FOO=1 sed -i 's/a/b/' app.ts",  # several assignments
    "LANG=C timeout 5 sed -i 's/a/b/' app.ts",  # assignment + a wrapper
    "bash -c \"X=1 sed -i 's/a/b/' app.ts\"",  # same root inside bash -c
    # codex round 4: more wrappers from the README, none previously asserted.
    "nice -n10 gawk -i inplace '{print}' app.ts",
    "nohup perl -i -pe 's/a/b/' app.ts",
    "stdbuf -oL sed -i 's/a/b/' app.ts",     # joined `-oL`, head must still resolve to sed
    # codex round 7: a GLOB operand expanded against cwd, blocking if any match is tracked source.
    "sed -i 's/a/b/' *.ts",                  # the canonical bulk-edit idiom — matches app.ts
    "sed -i 's/a/b/' app.*",                 # a different glob shape that still hits app.ts
    "sed -i 's/a/b/' src/*.py",              # glob in a subdir
    # review round 3 (MED): a brace group is a bulk-edit idiom the shell expands — Python's glob
    # does NOT, so `{app,}.ts` slipped past both the glob check and the literal-exists check → it
    # bypassed even the central single-edit block. Now expanded ourselves before the track check.
    "sed -i 's/a/b/' {app,}.ts",             # bash expands to `app.ts` (+ `.ts`) → edits app.ts
    "sed -i 's/a/b/' src/{x,y}.py",          # expands to src/x.py, src/y.py — both tracked
])
def test_block_inplace_on_tracked_source(command, repo, monkeypatch):
    out, _e, code = _run(command, repo, monkeypatch)
    assert code == nsfe.BLOCK_EXIT_CODE, command
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "Edit/Write" in payload["message"]


# ── BLOCK: redirect overwriting a tracked source file ────────────────────────────────────

@pytest.mark.parametrize("command", [
    "awk '{print}' app.ts > app.ts",
    "sed 's/a/b/' app.ts > app.ts",
    "grep -v drop app.ts > app.ts",
    "cat app.ts | sed 's/a/b/' > app.ts",    # redirect on the last segment of a pipe
    "sort src/x.py > src/x.py",
    "awk '{print}' app.ts >> app.ts",        # `>>` appends to (writes) the tracked source
    "bash -c \"sed -i 's/a/b/' app.ts\"",     # in-place hidden inside bash -c
    "sh -c 'awk \"{print}\" app.ts > app.ts'",  # redirect hidden inside sh -c
    # codex F2: redirect operator GLUED to the source/target word (no surrounding spaces).
    "awk '{print}' app.ts >app.ts",          # `>` glued to the target
    "awk '{print}' app.ts> app.ts",          # `>` glued to the source word
    "awk '{print}' app.ts>app.ts",           # fully glued, one shlex token
    "sed 's/a/b/' app.ts>>app.ts",           # glued append
    # codex round 2: outer redirect of a bash -c, nested bash -c, >| clobber.
    "bash -c 'cat x' > app.ts",              # the redirect is OUTSIDE the bash -c
    "bash -c \"bash -c 'sed -i s/a/b/ app.ts'\"",  # double-nested bash -c
    "cat app.ts >| app.ts",                   # `>|` force-clobber, not a pipe
    "cat app.ts >|app.ts",                    # `>|` glued
    # codex round 3: tee/dd write-to-file workarounds, and the `>& file` redirect.
    "echo x | tee app.ts",                    # tee writes the tracked source
    "echo x | tee -a app.ts",                 # tee --append
    "dd of=app.ts",                           # dd of= writes the tracked source
    "cat x >& app.ts",                        # `>&` stdout+stderr to a tracked file
    "cat x >&app.ts",                         # `>&` glued
    # codex round 4: an fd-numbered redirect to a tracked source (`2> app.ts`).
    "grep x app.ts 2> app.ts",                # stderr redirected onto the tracked source
    "make 1> app.ts",                         # explicit fd-1 redirect
    # codex round 6: `&>` / `&>>` (redirect both stdout+stderr) onto a tracked source.
    "cat x &> app.ts",                        # &> redirects both streams to the tracked file
    "cat x &>> app.ts",                       # &>> append form
    # codex round 5: a command substitution actually runs the editor — recursed like bash -c.
    "echo $(sed -i 's/a/b/' app.ts)",         # $(…) substitution edits the tracked source
    "x=`perl -i -pe 's/a/b/' app.ts`",        # backtick substitution
    "echo $(awk '{print}' app.ts > app.ts)",  # redirect inside a substitution
    'echo "$(sed -i \'s/a/b/\' app.ts)"',     # $(…) inside DOUBLE quotes IS expanded by the shell
    # codex round 8: `-c` CLUSTERED with other shell flags (`bash -ec`, `sh -lc`, `bash -xc`).
    "bash -ec 'sed -i s/a/b/ app.ts'",        # -e + -c cluster
    "sh -lc 'sed -i s/a/b/ app.ts'",          # -l + -c cluster
    "bash -xc 'awk \"{print}\" app.ts > app.ts'",  # -x + -c, redirect inside
    "bash -c \"bash -ec 'sed -i s/a/b/ app.ts'\"",  # nested plain-c then clustered-c
    # codex round 9: a MULTI-LINE command — a comment ends at its own newline, later lines still run.
    "ls # note\nsed -i 's/a/b/' app.ts",      # the edit is on the line AFTER a commented one
    "echo hi\nsed -i 's/a/b/' app.ts",        # plain second line
    "sed -i 's/a/b/' app.ts # x\necho ok",    # edit on the first line, comment then another line
    # codex round 10: a command substitution INSIDE a bash -c inner script — the inner shell expands
    # it on re-exec, so the edit really runs. Routed through the full _matched_edit now.
    "bash -c 'echo $(sed -i s/a/b/ app.ts)'",
    "bash -ec 'x=`perl -i -pe s/a/b/ app.ts`'",  # backtick sub inside a clustered -c
    # codex round 11: `command`/`exec`/`builtin` are no-op shell prefixes — peel them to the editor.
    "command sed -i 's/a/b/' app.ts",
    "exec perl -i -pe 's/a/b/' app.ts",
    "builtin echo x\nsed -i 's/a/b/' app.ts",  # a real edit after a builtin line
    # codex round 11: a real edit on a line AFTER a heredoc body is still the command the shell runs.
    "cat <<EOF\ndata\nEOF\nsed -i 's/a/b/' app.ts",
    # review (opus) finding: a `<<WORD` INSIDE a quoted string / commit message is NOT a heredoc —
    # the real shell runs the edit on the next line. The bare-regex heredoc stripper wrongly treated
    # it as an opener and dropped every following line up to a never-seen terminator → silent ALLOW
    # (the exact #59 raw-substring bypass the gate claims to close). Now quote-aware → BLOCK.
    'echo "report << TODO"\nsed -i \'s/a/b/\' app.ts',       # `<<` inside a double-quoted string
    'git commit -m "fix << EOF"\nsed -i \'s/a/b/\' app.ts',  # `<<` inside a commit message
    "echo 'log << END'\nawk '{print}' app.ts > app.ts",     # `<<` inside single quotes, then redirect
    # review round 2: a `<<WORD` inside a trailing COMMENT is not a heredoc opener either — the
    # comment-unaware heredoc stripper dropped the next real edit line up to a never-seen terminator.
    "echo ok # template <<EOF\nsed -i 's/a/b/' app.ts",      # `<<EOF` in a comment, edit on next line
    "ls # note <<END\nawk '{print}' app.ts > app.ts",       # `<<END` in a comment, then a redirect
    # review round 3 (LOW-MED): zsh's `>!` / `>>!` force-clobber (the platform shell is zsh, which
    # is in _SHELL_RUNNERS) overwrites like bash's `>|` — the `!` is not the target. It left `app.ts`
    # out of the redirect targets → silent allow. Now the `!` is dropped after the operator like `|`.
    "cat x >! app.ts",                       # zsh force-clobber, spaced
    "cat x >!app.ts",                        # glued
    "cat x >>! app.ts",                      # append force-clobber
    # review round 6: multiple redirect operators in one segment (fd-numbered + dup) — the tracked
    # target must still be caught past the `2>&1` noise.
    "make > app.ts 2>&1",                    # stdout to the tracked source, then stderr→stdout dup
])
def test_block_redirect_onto_tracked_source(command, repo, monkeypatch):
    out, _e, code = _run(command, repo, monkeypatch)
    assert code == nsfe.BLOCK_EXIT_CODE, command
    assert _decision(out) == "block"


# ── ALLOW: read-only filters (no -i, no redirect to a tracked source file) ───────────────

@pytest.mark.parametrize("command", [
    "sed -n '1,5p' app.ts",
    "sed 's/a/b/' app.ts",                   # prints to stdout, no -i, no redirect
    "awk '{print $1}' app.ts | sort",
    "grep -v drop app.ts",
    "cat app.ts | sed 's/a/b/' | head",
    # codex F3: a flag whose ARGUMENT contains an `i` must NOT be read as in-place.
    "perl -Ilib -pe 'print' app.ts",         # -I<dir> include path with an `i`
    "perl -MList::Util -pe 'print' app.ts",  # -M<module> name with an `i`
    "perl -ne 'print' app.ts",               # -ne, no i at all
    "sed -e 's/i/x/' app.ts",                # `i` is inside the -e script, not a flag
    "sed -Ee 's/a/b/' app.ts",               # -E -e cluster, no i
    "awk -v inplace=1 '{print}' app.ts",     # -v var whose name starts with `i`
    # codex round 2: gawk's `-i` loads a library; in-place ONLY when the library is `inplace`.
    "gawk -i json '{print}' app.ts",         # -i json = load the json extension, read-only
    "gawk -i readfile '{print}' app.ts",     # -i readfile = load that extension, read-only
    # codex round 3: `>&2` / `>&-` are fd dups, not a file edit; tee/dd to a NON-tracked file.
    "grep -v x app.ts >&2",                   # redirect stdout to stderr, not a file
    "sed -n '1p' app.ts >&-",                 # close-stdout, not a file
    "echo x | tee /tmp/scratch.ts",          # tee to an untracked /tmp file
    "dd of=/tmp/scratch.ts",                  # dd to an untracked /tmp file
])
def test_allow_read_only_filters(command, repo, monkeypatch):
    out, _e, code = _run(command, repo, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── ALLOW: redirect to /tmp, a new file, or a non-source file (generating, not editing) ──

@pytest.mark.parametrize("command", [
    "awk '{print}' app.ts > /tmp/out.ts",
    "sed 's/a/b/' app.ts > brand_new.ts",    # target not tracked yet → generating
    "awk '{print}' app.ts > out.log",        # non-source extension
    "grep x app.ts > notes.md",              # markdown is not source
    "sed -i 's/a/b/' scratch.ts",            # `-i` but the target is UNTRACKED
    "sed -i 's/a/b/' /tmp/throwaway.ts",     # `-i` on /tmp
    "echo hi > /dev/null",                    # fd / dev sink
    "cat x &> /tmp/out.ts",                   # codex round 6: &> to an untracked /tmp file
    # review round 3: a brace group that expands ONLY to untracked/non-existent paths must not
    # over-block (the expander's allow direction), and zsh `>!` to /tmp is still generating.
    "sed -i 's/a/b/' {new,gone}.ts",          # neither new.ts nor gone.ts is tracked
    "cat x >! /tmp/out.ts",                   # zsh force-clobber to a /tmp scratch file
])
def test_allow_generating_or_untracked(command, repo, monkeypatch):
    out, _e, code = _run(command, repo, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── ALLOW: raw-string DECOYS — the flag in a string / comment / message must NOT trip it ──

@pytest.mark.parametrize("command", [
    'echo "use sed -i to patch app.ts"',     # the words live inside a string operand
    "ls -la app.ts  # remember: sed -i later",  # a comment tail, dropped before parsing
    "git commit -m 'switch off sed -i'",     # the flag is inside the commit message
    "sed 's/-i//' app.ts",                    # `-i` is part of the s/// operand, not a flag
    "grep 'foo > app.ts' app.ts",             # the redirect-looking text is a grep pattern
    "grep 'a>b' app.ts",                       # a glued `>` INSIDE a quoted pattern is not a redirect
    # codex round 4: a leading VAR=val peel must not over-block a read-only command, and a `=`
    # inside an operand (not a leading identifier=) is not an assignment.
    "LANG=C grep x app.ts",                   # assignment prefix on a read-only command
    "LC_ALL=C sed -n '1p' app.ts",            # assignment prefix, sed without -i
    "sed 's/a=b/c/' app.ts",                  # `=` lives in the s/// operand, not a prefix
    "FOO=bar",                                 # a bare assignment, no command at all
    # codex round 6: a separator INSIDE a trailing comment must not split off the comment text as a
    # standalone edit command. The real shell runs only `echo hi`; the hook must not false-block.
    "echo hi # note; sed -i 's/a/b/' app.ts",
    "ls app.ts  # then | sed -i 's/a/b/' app.ts later",
    # codex round 7: a glob that matches NO tracked source must not block.
    "sed -i 's/a/b/' *.md",                  # matches notes.md — non-source extension
    "sed -i 's/a/b/' nope*.ts",              # matches nothing
    # codex round 8: a `$(…)` inside SINGLE quotes is NOT expanded by the shell — literal text, no
    # edit. Scanning it would falsely block (echoing example text into a doc).
    "echo 'example: $(sed -i s/a/b/ app.ts)' >> notes.md",
    "echo 'see `perl -i -pe x app.ts`' >> notes.md",  # literal backticks in single quotes
    "bash -lc 'grep x app.ts'",              # clustered -c on a READ-only inner command
    # codex round 9: a `$(…)` / backtick sitting INSIDE a comment is not run — must not block.
    "echo done # $(sed -i 's/a/b/' app.ts)",
    "git add -A # TODO replace the `perl -i -pe 's/a/b/' app.ts` step",
    "bash -c 'grep x app.ts # note'",        # a comment inside a bash -c inner script
    "gawk -isomeinplace '{print}' app.ts",   # a joined lib name merely ENDING in 'inplace'
    # codex round 10: a read-only command substitution inside bash -c must still allow.
    "bash -c 'echo $(grep x app.ts)'",
    # codex round 11: a heredoc BODY is data fed to stdin, not commands — a `sed -i` line inside it
    # (generating a script/doc) must not be scanned as a real edit.
    "cat <<EOF\nsed -i 's/a/b/' app.ts\nEOF",         # body goes to stdout
    "cat > script.sh <<'EOF'\nsed -i 's/a/b/' app.ts\nEOF",  # body written to a NEW file
])
def test_allow_raw_string_decoys(command, repo, monkeypatch):
    """The #59 raw-string bypass class: a flag/redirect spotted inside a string, a comment, or a
    message must NOT block. Decided from the PARSED argv, so these all ALLOW."""
    out, _e, code = _run(command, repo, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


def test_real_edit_before_a_comment_still_blocks(repo, monkeypatch):
    """The comment-aware split must not UNDER-block: a real in-place edit BEFORE a trailing comment
    is still the command the shell runs, so it blocks (counterpart to the decoy above)."""
    out, _e, code = _run("sed -i 's/a/b/' app.ts  # done", repo, monkeypatch)
    assert code == nsfe.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_unparseable_command_fails_open(repo, monkeypatch):
    """on_error=open invariant: a command a segment can't shlex-tokenize (unbalanced quote) and
    that matched nothing must ALLOW (exit 0) with a stderr warning — never wedge the tool call."""
    out, err, code = _run("sed -i 's/a app.ts", repo, monkeypatch)  # unterminated single quote
    assert code == 0
    assert _decision(out) == "allow"
    assert "tokenize" in err.lower() or "fail-open" in err.lower()


def test_invalid_event_json_fails_open(repo, monkeypatch):
    """review round 5: main()'s `json.load` except-branch is part of the on_error=open contract —
    a malformed event on stdin must ALLOW (exit 0) with a warning, never wedge the tool call. Drive
    main() directly with non-JSON stdin (bypassing `_run`, which always emits valid JSON)."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("}{ not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = nsfe.main()
    assert code == 0
    assert _decision(out.getvalue()) == "allow"
    assert "parse" in err.getvalue().lower() or "fail-open" in err.getvalue().lower()


def test_scan_exception_fails_open(repo, monkeypatch):
    """review round 5: main()'s broad `except Exception` around `_matched_edit` is the last-resort
    on_error=open guard — if the scan raises unexpectedly, the command must still ALLOW, never wedge.
    Force `_matched_edit` to raise so the branch is actually exercised."""
    def boom(*_a, **_k):
        raise RuntimeError("forced scan error")
    monkeypatch.setattr(nsfe, "_matched_edit", boom)
    out, _e, code = _run("sed -i 's/a/b/' app.ts", repo, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_inline_sentinel_inside_quotes_does_not_self_exempt(repo, monkeypatch):
    """codex F5: the override sentinel is read from the actual COMMENT, not the raw string, so a
    `# shell-file-edit-ok: …` hidden inside a quoted operand cannot self-exempt a real edit."""
    out, _e, code = _run(
        "sed -i 's/x/y # shell-file-edit-ok: forged/' app.ts", repo, monkeypatch,
    )
    assert code == nsfe.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_untracked_tmp_target_allows(repo, monkeypatch):
    """A redirect to an untracked /tmp scratch path ALLOWs — via git-trackedness, not a /tmp string
    guard (codex F4: the old `startswith('/tmp')` guard was dead on macOS after `resolve()` AND
    over-broad). An untracked path is exempt wherever it lives."""
    out, _e, code = _run("awk '{print}' app.ts > /tmp/scratch.ts", repo, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_tracked_source_under_tmp_still_blocks(monkeypatch):
    """codex F4, the over-broad half: a real codebase can live under /tmp (CI runners, agent
    worktrees). A git-TRACKED source file there is a real edit and must BLOCK — no /tmp exemption.

    Built under a REAL `/tmp` dir (not pytest's `tmp_path`, which on macOS lands in
    `/private/var/folders/…`) so the invariant that justifies dropping the `startswith('/tmp')`
    guard is actually exercised on the dev platform, not skipped (codex round 5)."""
    repo = Path(tempfile.mkdtemp(prefix="nsfe-tmp-", dir="/tmp"))
    try:
        def git(*argv: str) -> None:  # capture_output keeps git noise out of the test's stdout
            subprocess.run(["git", "-c", "core.hooksPath=", *argv], cwd=repo, check=True,
                           capture_output=True, text=True)

        for argv in (("init", "-q"), ("config", "user.email", "t@t.t"),
                     ("config", "user.name", "t")):
            git(*argv)
        (repo / "real.ts").write_text("x\n")
        git("add", "real.ts")
        git("commit", "-qm", "i")
        out, _e, code = _run("sed -i 's/a/b/' real.ts", repo, monkeypatch)
        assert code == nsfe.BLOCK_EXIT_CODE
        assert _decision(out) == "block"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_deeply_nested_bash_c_does_not_crash(repo, monkeypatch):
    """codex round 11: the `_MAX_SHELL_DEPTH` cap must actually fire — before threading `_depth`
    into `_scan_segment` it was a no-op, so a deep `bash -c "bash -c …"` recursed to a RecursionError
    (then fail-open). With the cap working, a nest far past the cap returns a clean decision (allow,
    since the edit is below the cap) and NEVER raises. The 2-level case (within the cap) still blocks
    (asserted by the parametrized `bash -c "bash -c '…'"` redirect-block case).

    The nest is built with SINGLE-QUOTE wrapping around a quote-free inner script so its length
    grows LINEARLY (~one `bash -c '…'` per level). The earlier `"bash -c %r" % deep` form re-`repr`'d
    at every level, escaping each embedded quote and DOUBLING the string per level — 40 levels is a
    ~1TB string the test hung allocating, long before the hook ever ran. The cap is what's under
    test, not the parser's tolerance for a pathological megastring."""
    deep = "sed -i s/a/b/ app.ts"  # no single quotes, so each wrap stays a fixed size
    for _ in range(40):  # well past _MAX_SHELL_DEPTH
        deep = f"bash -c '{deep}'"
    out, _e, code = _run(deep, repo, monkeypatch)
    assert code == 0  # below-cap edit not reached → allow; the point is it does not crash
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    # codex round 5/7: documented "not caught" boundary — pin current behavior so a future change
    # that starts catching one of these is a deliberate doc update, not a silent drift.
    "find . -name '*.ts' -exec sed -i 's/a/b/' {} +",  # operand is the `{}` placeholder
    "ls *.ts | xargs sed -i 's/a/b/'",       # operands arrive on stdin
    'sed -i "s/a/b/" "$FILE"',               # a variable operand, value unknown statically
    "cp /tmp/new.ts app.ts",                 # cp overwriting a tracked source (non-editor idiom)
    "mv /tmp/new.ts app.ts",                 # mv overwriting a tracked source
    "install -m 644 /tmp/new.ts app.ts",     # install overwriting a tracked source
    "patch app.ts < changes.diff",           # patch applying to a tracked source
    # review round 4: three more README-declared boundaries, previously unpinned — pin them so a
    # future behavior change is a deliberate doc update, not a silent drift (like find/xargs above).
    "sudo sed -i 's/a/b/' app.ts",           # sudo is not peeled → head=sudo, not an editor
    "echo x | sudo tee app.ts",              # sudo tee, same — sudo not peeled
    "eval \"sed -i 's/a/b/' app.ts\"",       # eval is not recursed into
    "cat <(sed -i 's/a/b/' app.ts)",         # process substitution is not recursed into
    "awk '{print > \"app.ts\"}' in.txt",     # `>` lives INSIDE awk's single-quoted program
])
def test_documented_scope_boundary_not_caught(command, repo, monkeypatch):
    """Pins the README 'Known scope boundary': find/xargs (operands not in the command), a variable
    operand, non-editor overwrite idioms (cp/mv/install/patch), and the un-peeled `sudo` / un-recursed
    `eval` / process-substitution cases are intentionally NOT blocked."""
    out, _e, code = _run(command, repo, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline) ──────────────────

def test_old_env_escape_hatch_no_longer_bypasses(repo, monkeypatch):
    """ALLOW_SHELL_FILE_EDIT=1 + _REASON as a real env pair must NO LONGER allow the shell edit
    — the self-service bypass was removed (replaced by the Telegram hatch)."""
    out, _e, code = _run(
        "sed -i 's/a/b/' app.ts", repo, monkeypatch,
        env={"ALLOW_SHELL_FILE_EDIT": "1", "ALLOW_SHELL_FILE_EDIT_REASON": "vetted codemod"},
    )
    assert code == nsfe.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_old_inline_sentinel_no_longer_bypasses(repo, monkeypatch):
    """A `# shell-file-edit-ok: …` appended to a real edit must still BLOCK — the inline sentinel
    is gone."""
    out, _e, code = _run(
        "sed -i 's/a/b/' app.ts  # shell-file-edit-ok: regenerated each build",
        repo, monkeypatch,
    )
    assert code == nsfe.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


# ── Telegram hatch escalation (deny-by-default) ────────────────────────────────────────────

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


def test_hatch_unset_blocks_and_names_env_var(repo, monkeypatch):
    out, _e, code = _run("sed -i 's/a/b/' app.ts", repo, monkeypatch)
    assert code == nsfe.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(repo, tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(nsfe.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run("sed -i 's/a/b/' app.ts", repo, monkeypatch,
                         env={"RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT": "1"})
    assert code == nsfe.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(repo, tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(nsfe.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        "sed -i 's/a/b/' app.ts", repo, monkeypatch,
        env={"RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT": "Vetted bulk codemod, reviewed."},
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(repo, tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(nsfe.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        "sed -i 's/a/b/' app.ts", repo, monkeypatch,
        env={"RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT": "Vetted bulk codemod, reviewed."},
    )
    assert code == nsfe.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_hatch_inline_command_justification_allows(repo, tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl via the new `command=` contract. Regression for the documented inline form (Codex #232)."""
    marker = tmp_path / "asked"
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f'touch {marker}\ncat > "{question}"\n' + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(nsfe.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        'RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT="vetted bulk codemod, reviewed" sed -i \'s/a/b/\' app.ts',
        repo, monkeypatch)  # env deliberately NOT set — only the inline prefix
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "vetted bulk codemod, reviewed" in question.read_text()
