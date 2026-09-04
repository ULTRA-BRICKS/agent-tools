"""Tests for the no-long-inline-process agent-hook (pre-bash, hard block).

Covers the doctrine's four cases: BLOCK (review / --watch / build-test suite / long sleep),
ALLOW (short sleep, a path that merely contains "review", a benign read), SUBAGENT-EXEMPT
(agent_id present), and the deny-by-default Telegram hatch escalation (the old
ALLOW_INLINE_PROCESS env + `# inline-process-ok:` sentinel are DEAD;
RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS with a written justification asks tg-ctl and allows
only on exit 0, a bare `1` denies).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_no_long_inline_process.py -q
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
    / "no-long-inline-process"
    / "no_long_inline_process.py"
)
_spec = importlib.util.spec_from_file_location("no_long_inline_process", _HOOK)
assert _spec and _spec.loader
nlip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nlip)


def _run(command, monkeypatch, *, agent_id=None, env: dict | None = None) -> tuple[str, str, int]:
    args = {"command": command}
    if agent_id is not None:
        args["agent_id"] = agent_id
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"args": args})))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_INLINE_PROCESS", "ALLOW_INLINE_PROCESS_REASON",
              "RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = nlip.main()
    return out.getvalue(), err.getvalue(), code


def _decision(out: str) -> str:
    return json.loads(out)["decision"]


# ── BLOCK ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "review",
    "review -C /repo",
    "review diff -C /repo -m claude:claude-opus-4-8",  # the real diff-review invocation
    "gh pr checks 42 --watch",
    "gh pr checks 42 --watch=true",   # glued `--watch=…` value form is still a watch flag
    "gh pr checks 42 --watch-poll",   # hyphenated subform on a NON-runner pins the `--watch-` branch
    "vitest --watch",
    "vitest --watch=src",
    "vitest --watch-poll",           # hyphenated subform — the old `--watch\\b` caught it; restored
    "jest --watch-mode",
    "npm test",
    "pnpm build",
    "pytest tests/",
    "cargo test",
    "go build ./...",
    "make all",
    "make build",
    "rake test",
    "msbuild build",
    "mvn verify",                    # _TEST_BUILD_VERIFY_PACKAGE branch
    "gradle package",
    "deno test",                     # deno suite branch
    "deno run build",
    "sleep 30",
    "sleep 5m",                      # 5 minutes — a bare \\d+ would read this as 5 and pass
    "sleep 1h",                      # 1 hour
    "sleep 600",                     # 10 minutes
])
def test_block_long_running(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "BACKGROUND" in payload["message"]


# ── ALLOW ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "sleep 2",                       # short sleep is fine
    "sleep 9",                       # just under the N>=10 threshold
    "sleep 5s",                      # 5 seconds with an explicit unit is still short
    'echo "sleep 100"',              # the word inside a string is not a sleep command
    "cat docs/review-notes.md",      # "review" only as a path substring
    "ls src/review/",                # "review" only in a dir name
    "tsc --watcher-config x",        # `--watcher-…` is not the `--watch` flag (word char after watch)
    "git status",                    # plain inspection
    "echo done",
    "make clean",                    # a non-test/build make target is not a suite
    "mvn dependency:tree",           # an mvn goal that is not test|build|verify|package
    'cmd "\\"" # foo; npm test',      # POSIX `\\"` keeps in-double balanced → `#` IS a comment →
                                     # the `npm test` after it is comment text, not a command
    "find . \\( -name x -o -name y \\)",  # escaped `\\(`/`\\)` de-quote to `(`/`)` segment breaks,
                                     # but each segment's head is a `find` predicate, not a runner
    "for review in a b c; do echo $review; done",  # `for`/`case` next token is a loop VAR/word,
    "case pytest in x) :;; esac",    # not a command — stripping them must NOT make it argv[0]
])
def test_allow_benign(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── #60: a long-process KEYWORD inside a QUOTED ARGUMENT to a DIFFERENT command must ALLOW ──
# The old raw regex anchored on shell-separator chars (`(`/`|`/`;`/`&&`) and a bare `--watch`
# substring; those chars also appear INSIDE quoted argument strings, so a benign report/commit/
# echo whose TEXT mentioned review/--watch/npm test/sleep was mis-read as the invocation and
# BLOCKED. argv-awareness flags only the REAL invoked command (argv[0]), so these now ALLOW.

@pytest.mark.parametrize("command", [
    'tg --tag report --title x "text with review and review-qa in it"',
    'tg --tag report --title "x" "we ran review-qa (review) today"',  # `(review` inside quotes
    'echo "run review later"',
    'git commit -m "add review gate"',
    'git commit -m "fix; npm test now green"',          # `;` + suite inside the quoted message
    'git commit -m "wire --watch flag"',                # `--watch` substring inside the message
    'git commit -m "--watch-poll: new default"',        # message STARTS with `--watch-` but has spaces
    'echo "--watch=1 explained"',                       # message STARTS with `--watch=` but has spaces
    'tg "status: (sleep 600 elapsed) done"',            # `(sleep 600` inside the quoted arg
    'echo "ci runs | pytest fast"',                     # `|` + runner inside the quoted arg
    'tg --photo p.png "review the screenshot in review-qa"',
])
def test_allow_keyword_in_quoted_argument(command, monkeypatch):
    """#60: review / --watch / npm test / sleep appearing inside a quoted argument to a DIFFERENT
    command (tg / echo / git commit -m) must NOT trip the gate — only a real invocation does."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── codex: WRAPPED long processes (timeout/env/nice/time/…) must still BLOCK ─────────────

@pytest.mark.parametrize("command", [
    "timeout 600 npm test",          # timeout + duration wraps the suite
    "timeout 5m review",             # duration with a unit suffix
    "timeout -k 5 600 pytest",       # timeout with its own -k flag + value, then duration
    "env CI=1 pytest",               # env + a KEY=VALUE assignment
    "env CI=1 NODE_ENV=test vitest",  # multiple env assignments
    "nice -n10 review -C /repo",     # nice + joined -n10
    "nice -n 10 npm run build",      # nice + separated -n 10
    "time make build",              # bare wrapper, no args of its own
    "stdbuf -oL pytest tests/",      # stdbuf + a flag, no positional
    "nohup cargo build",            # nohup wraps directly
    "git pull && timeout 600 npm test",  # wrapper on a non-head segment
])
def test_block_wrapped_long_process(command, monkeypatch):
    """codex: ``_matched_long_process`` anchored on the runner and missed common wrappers, so
    ``timeout 600 npm test`` / ``env CI=1 pytest`` / ``timeout 5m review`` slipped through. The
    wrapper is now peeled off each segment before matching → these BLOCK."""
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    assert json.loads(out)["decision"] == "block"


@pytest.mark.parametrize("command", [
    "timeout 5 ls",                  # a wrapped SHORT/benign command is not long-running
    "env EDITOR=vim git status",     # env wrapping a benign command
    "nice -n10 git log",             # nice wrapping a benign command
    "time true",                    # time wrapping a no-op
    "timeout 30 cat big.log",        # a wrapped non-suite command
])
def test_allow_wrapped_benign_command(command, monkeypatch):
    """Unwrapping must not over-block: a wrapper in front of a BENIGN command is still allowed —
    only the wrapped command's own long-running-ness decides."""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── review finding #1: a long process nested in $(…) / <(…) / a subshell must still BLOCK ──
# The OLD raw regex caught a runner inside command/process substitution via its `(` anchor
# (`_CMD_START` matched the literal `(`). The argv rewrite must keep catching it — the substitution
# opener is now a SEGMENT break, so the inner runner becomes its own argv-inspected segment.

@pytest.mark.parametrize("command", [
    "RESULT=$(npm test)",                # command substitution assigned to a var
    "$(review)",                         # bare command substitution
    "$(sleep 600)",
    "echo $(npm run build)",             # substitution as an ARG of another command
    "cat <(playwright test)",            # process substitution
    "x=$(go build ./...)",
    "(review -C /repo)",                 # plain subshell
    "git pull && (npm test)",            # subshell on a non-head segment
    "git status;(review)",               # GLUED `;(` — shlex merges it; must still break the segment
    "git fetch&&(npm test)",             # GLUED `&&(`
    "(echo hi)&&npm test",               # GLUED `)&&` — runner after a glued closer
    "foo|(pytest)",                      # GLUED `|(`
])
def test_block_command_substitution(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    assert json.loads(out)["decision"] == "block"


# ── review finding #2: lock in the new parser branches (heredoc / multiline / fail-open / env / path)

def test_block_inline_env_without_env_wrapper(monkeypatch):
    """A bare `VAR=val runner` (no `env` wrapper) is peeled to the real runner → BLOCK."""
    out, _e, code = _run("CI=1 pytest", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_path_qualified_runner(monkeypatch):
    """A path-qualified runner is matched by basename → BLOCK."""
    out, _e, code = _run("/usr/local/bin/review -C /repo", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_heredoc_body_that_looks_like_a_command(monkeypatch):
    """A here-document BODY line that LOOKS like a long process (`npm test`) is data, not a command."""
    out, _e, code = _run("git commit -F - <<EOF\nnpm test\nreview later\nEOF", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_allow_glued_metachar_comment(monkeypatch):
    """A `#` glued right after an unescaped metachar (`;#`) opens a comment in POSIX shell, so the
    `&& npm test` in `git push ;# note && npm test` is comment text, not a command → ALLOW."""
    out, _e, code = _run("git push ;# note && npm test", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


def test_block_real_runner_with_plain_trailing_comment(monkeypatch):
    """Happy-path of the quote-aware comment strip: a real runner with an ordinary trailing `#`
    comment still BLOCKS — the comment is cut, `npm test` remains the command."""
    out, _e, code = _run("npm test  # just a note", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_real_command_after_closed_heredoc(monkeypatch):
    """A real `review` AFTER a closed heredoc is still a command → BLOCK."""
    out, _e, code = _run("git commit -F - <<EOF\nmessage body\nEOF\nreview -C /repo", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_block_multiline_newline_separates_commands(monkeypatch):
    """A NEWLINE separates commands: `git status` then `npm test` → the suite BLOCKS."""
    out, _e, code = _run("git status\nnpm test", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_allow_unbalanced_quotes_fail_open(monkeypatch):
    """An unparseable command (unbalanced quotes) fails OPEN (allow) — on_error=open policy."""
    out, _e, code = _run('review "unterminated', monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    'X="$(npm test)"',                   # documented under-block: quoted substitution not re-parsed
    'result="$(review)"',
    "bash -c 'npm test'",                # nested shell string not re-parsed (sibling limitation)
    "sh -c 'review'",                    # same nested-shell-string class as `bash -c`
    "xargs npm test",                    # `xargs` is not peeled as a wrapper → exe='xargs' (under-block)
    "`npm test`",                        # backtick substitution not re-parsed (no punctuation_chars break)
    'webpack --watch="src dir"',         # `--watch=<value with space>` → de-quoted token has whitespace
                                         # → the `_is_watch_flag` guard rejects it (documented under-block)
    "git commit -m '$(npm test) is the command to run'",  # single-quoted LITERAL must NOT over-block
])
def test_allow_quoted_substitution_documented_limitation(command, monkeypatch):
    """Boundary pin: a runner inside a QUOTED `$(…)` / backtick / `bash -c '…'` is NOT re-parsed, so
    it ALLOWS. Posix de-quoting collapses `"$(…)"` and the literal `'$(…)'` to one token, so flagging
    would re-open the #60 false-positive on the single-quoted literal — under-blocking is the safe
    direction for this on_error=open discipline gate. (An UNQUOTED `$(…)` IS still blocked, above.)"""
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


# ── review finding #1: a runner introduced by a CONTROL keyword / fused separator / `&` must BLOCK ──
# `_LEADING_SHELL_NOISE` peels the brace-group + control keywords (`if`/`then`/`do`/`while`/…) so the
# real runner under them is recovered; `_segments` splits fused separators (`;`/`&&`/`&`) with no
# surrounding whitespace. `if` was missing from the noise set (its siblings `while`/`until`/`elif`
# were present) → `if npm test; then …` under-blocked; it is now peeled symmetrically.

@pytest.mark.parametrize("command", [
    "if npm test; then echo ok; fi",     # `if`-guarded suite — the finding-#1 regression case
    "while npm test; do :; done",        # `while`-guarded suite (already worked; locked in)
    "until pytest; do sleep 1; done",    # `until`-guarded runner
    "for i in 1 2 3; do review; done",   # `review` in a for-loop body
    "git status;npm test",               # fused `;` with no surrounding whitespace
    "git fetch&&pytest",                 # fused `&&`
    "npm test &",                        # backgrounded suite (the `&` is a segment break)
])
def test_block_control_structures_and_fused_separators(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    assert json.loads(out)["decision"] == "block"


# ── review finding #2: a `#` INSIDE single quotes guarding a runner mention must ALLOW ──────

def test_allow_hash_in_single_quotes_guarding_runner(monkeypatch):
    """A `#` inside single quotes is literal (not a comment), and so is the `;` and `npm test`
    after it — the whole `-m` value is one quoted arg, so nothing is a real command. `in_single`
    suppresses both the comment-strip and the separator → ALLOW."""
    out, _e, code = _run("git commit -m 'WIP #5; npm test'", monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


# ── review finding (residual): a QUOTED token EXACTLY equal to a separator/opener over-blocks ──
# Posix shlex de-quotes a standalone `";"` / `"("` to the bare `;` / `(`, indistinguishable from a
# real separator without per-token quote provenance, so a runner RIGHT AFTER it is still flagged.
# This pins the DOCUMENTED residual over-block (see the module docstring's RESIDUAL LIMITATION):
# the trigger is pathological (a quoted arg equal to a lone separator + a bare runner next), and the
# reported #60 case — a keyword inside a NORMAL multi-word quoted message — is fully fixed above.

@pytest.mark.parametrize("command", [
    'echo ";" review',          # quoted `;` de-quotes to a bare separator → `review` its own segment
    'tg "(" review',            # quoted `(` de-quotes to a bare subshell opener
    'git fetch ";" npm test',   # quoted `;` then a real suite token
])
def test_quoted_token_equal_to_separator_documented_overblock(command, monkeypatch):
    """DOCUMENTED residual over-block (module docstring RESIDUAL LIMITATION). If a future precise
    quote-aware separator scan lands, flip these to ALLOW and update the docstring."""
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    assert json.loads(out)["decision"] == "block"


@pytest.mark.parametrize("command", [
    'echo "--watch=1"',          # single-word quoted token == a watch flag (no space → guard misses)
    'echo "--watch-x"',          # single-word quoted `--watch-` subform
    "pytest --version",          # a DIRECT runner blocks by name even on a benign subcommand/flag
    "jest --listTests",          # same — name-keyed, not subcommand-aware
])
def test_documented_overblocks_pinned(command, monkeypatch):
    """Pin the over-blocks the docstrings acknowledge (single-word quoted flag token; a direct runner
    invoked with a benign flag). They block by design given argv[0]/token identity alone; pinned so a
    future refactor can't silently shift the boundary. Flip + document if a precise fix lands."""
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    assert json.loads(out)["decision"] == "block"


# ── SUBAGENT-EXEMPT ────────────────────────────────────────────────────────────────────

def test_subagent_exempt_allows_long_process(monkeypatch):
    out, _e, code = _run("npm test", monkeypatch, agent_id="sub-3")
    assert code == 0
    assert _decision(out) == "allow"


def test_subagent_exempt_allows_review(monkeypatch):
    """The same real `review` that blocks for the orchestrator is allowed inside a subagent."""
    out, _e, code = _run("review diff -C /repo", monkeypatch, agent_id="sub-7")
    assert code == 0
    assert _decision(out) == "allow"


# ── regression: the OLD self-service escape hatch is DEAD (env AND inline) ──────────────────

def test_old_env_escape_hatch_no_longer_bypasses(monkeypatch):
    """ALLOW_INLINE_PROCESS=1 + _REASON as a real env pair must NO LONGER allow the long
    process — the self-service bypass was removed (replaced by the Telegram hatch)."""
    out, _e, code = _run(
        "review", monkeypatch,
        env={"ALLOW_INLINE_PROCESS": "1", "ALLOW_INLINE_PROCESS_REASON": "one-shot probe"},
    )
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_old_inline_sentinel_no_longer_bypasses(monkeypatch):
    """`# inline-process-ok: …` appended to a real long process must still BLOCK — the inline
    sentinel is gone."""
    out, _e, code = _run("npm test  # inline-process-ok: single fast file", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE
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


def test_hatch_unset_blocks_and_names_env_var(monkeypatch):
    out, _e, code = _run("review", monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS" in json.loads(out)["message"]


def test_hatch_bare_flag_denies_without_tg_call(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\nexit 0\n")
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run("review", monkeypatch,
                         env={"RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS": "1"})
    assert code == nlip.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert not marker.exists()


def test_hatch_justification_exit0_allows(tmp_path, monkeypatch):
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        "review", monkeypatch,
        env={"RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS": "One-shot review, output needed now."},
    )
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


def test_hatch_justification_exit1_blocks(tmp_path, monkeypatch):
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", "exit 1\n")
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        "review", monkeypatch,
        env={"RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS": "One-shot review, output needed now."},
    )
    assert code == nlip.BLOCK_EXIT_CODE and _decision(out) == "block"
    assert "hatch escalation denied" in json.loads(out)["message"].lower()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl via the new `command=` contract. Regression for the documented inline form (Codex #232)."""
    marker = tmp_path / "asked"
    question = tmp_path / "q.txt"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f'touch {marker}\ncat > "{question}"\n' + _ALLOW_REPLY_SH,
    )
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    out, _e, code = _run(
        'RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="one-shot review, output needed now" review',
        monkeypatch)  # env deliberately NOT set — only the inline prefix
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "one-shot review, output needed now" in question.read_text()
