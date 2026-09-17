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

_REPO = Path(__file__).resolve().parents[1]
_LIB = _REPO / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import omp_hook_bridge.dispatch as dispatch  # noqa: E402

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


def _run(command, monkeypatch, *, agent_id=None, harness=None, env: dict | None = None) -> tuple[str, str, int]:
    args = {"command": command}
    if agent_id is not None:
        args["agent_id"] = agent_id
    event = {"args": args}
    if harness is not None:
        event["harness"] = harness
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k in ("ALLOW_INLINE_PROCESS", "ALLOW_INLINE_PROCESS_REASON",
              "RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS",
              "RIG_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE"):
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


# ── per-harness delegation recipe (agent-tools#573) ──────────────────────────────────────

@pytest.mark.parametrize("harness,fragment", [
    (None, "Agent with `subagent_type"),               # missing harness -> Claude Code fallback
    ("claude-code", "Agent with `subagent_type"),
    ("codex", "rig-detached-codex"),
    ("opencode", "rig-detached-opencode"),
    ("omp", "hub start"),
    ("some-future-harness", "Agent with `subagent_type"),  # unrecognized -> same fallback
])
def test_block_message_names_correct_recipe_for_harness(monkeypatch, harness, fragment):
    """A live codex session collected 184 refusals with CC-only advice it could not follow
    (agent-tools#573's motivating incident) — the block message must name a recipe the ACTUAL
    firing harness can act on."""
    out, _e, code = _run("npm test", monkeypatch, harness=harness)
    assert code == nlip.BLOCK_EXIT_CODE
    assert fragment in json.loads(out)["message"]


def test_codex_harness_still_governed_no_carveout(monkeypatch):
    """This hook never had a harness exemption — a codex-tagged long process still BLOCKs,
    now with codex-actionable advice instead of CC's Agent tool."""
    out, _e, code = _run("review -C /repo", monkeypatch, harness="codex")
    assert code == nlip.BLOCK_EXIT_CODE
    assert "rig-detached-codex" in json.loads(out)["message"]


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


_OMP_NESTED_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e/LongInlineFix.jsonl"
)
_OMP_MAIN_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e.jsonl"
)


def test_subagent_review_allowed_via_rig_agent_id(monkeypatch):
    """No args.agent_id; identity via RIG_AGENT_ID. review diff must allow
    (agent-tools#635). Would FAIL if the classifier still required args.agent_id."""
    out, _e, code = _run(
        "review diff", monkeypatch, harness="omp",
        env={"RIG_AGENT_ID": "LongInlineFix"},
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_subagent_review_allowed_via_omp_nested_session(monkeypatch):
    """omp Task/eval child: PI_SESSION_FILE is nested .../<session-id>/<agent>.jsonl.
    No args.agent_id, no RIG_AGENT_ID. review diff must allow (agent-tools#635)."""
    out, _e, code = _run(
        "review diff", monkeypatch, harness="omp",
        env={"PI_SESSION_FILE": _OMP_NESTED_SESSION},
    )
    assert code == 0
    assert _decision(out) == "allow"


_OMP_GRANDCHILD_SESSION = (
    "/tmp/omp-sessions/-xp-rig-cli/"
    "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e/"
    "LongInlineFix/GrandchildWorker.jsonl"
)


def test_subagent_review_allowed_via_omp_grandchild_nested_session(monkeypatch):
    """Live finding (2026-09-18), same root cause as orchestrator-stays-thin's identical
    bug: a NESTED omp Task-tool subagent (a grandchild dispatched via `task` from WITHIN an
    already-dispatched subagent, task depth >= 2) has its own transcript one directory
    deeper than a direct child — `.../<session-id>/<child-id>/<grandchild-id>.jsonl` — so its
    immediate parent directory is another agent id, never the root session id. No
    args.agent_id, no RIG_AGENT_ID. review diff must still allow. Would FAIL if
    `_is_omp_nested_session` still checked only the immediate parent."""
    out, _e, code = _run(
        "review diff", monkeypatch, harness="omp",
        env={"PI_SESSION_FILE": _OMP_GRANDCHILD_SESSION},
    )
    assert code == 0
    assert _decision(out) == "allow"


def test_omp_orchestrator_inline_review_still_blocks(monkeypatch):
    """Main thread, harness=omp, no subagent identity: inline review still
    blocks. Do not harness-allowlist omp. Recipe names hub start."""
    out, _e, code = _run("review diff", monkeypatch, harness="omp")
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"
    assert "hub start" in json.loads(out)["message"]


def test_omp_main_session_file_review_still_blocks(monkeypatch):
    """PI_SESSION_FILE is the main session jsonl (not nested): still the
    orchestrator, so inline review blocks."""
    out, _e, code = _run(
        "review diff", monkeypatch, harness="omp",
        env={"PI_SESSION_FILE": _OMP_MAIN_SESSION},
    )
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_inline_rig_agent_id_prefix_does_not_exempt(monkeypatch):
    """A command-string RIG_AGENT_ID=… prefix is argv noise, not hook process
    env — the orchestrator cannot self-exempt by forging identity on the
    command line (agent-tools#635)."""
    out, _e, code = _run(
        "RIG_AGENT_ID=forged review diff", monkeypatch, harness="omp",
    )
    assert code == nlip.BLOCK_EXIT_CODE
    assert _decision(out) == "block"


def test_hub_start_application_review_allowed(monkeypatch):
    """Orchestrator dispatch via hub: argv[0] is hub, so review as an
    application= value is not a long inline process."""
    out, _e, code = _run(
        "hub start name=rev application=review args=[diff]",
        monkeypatch, harness="omp",
    )
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



def test_hatch_inline_command_justification_allows(tmp_path, monkeypatch):
    """The justification supplied as an inline command PREFIX (env var NOT exported) must reach
    tg-ctl via the `command=` contract. This is the classifier-safe invocation (#336): auto-mode
    may block exporting RIG_HATCH_REQUEST_* into the harness, so the prefix on the command is
    the sanctioned path (Codex #232)."""
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
_USAGE_BLURB = (
    'RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="<written justification>"'
)
_WRITTEN_JUSTIFICATION = (
    "one-shot smoke build, output needed now because the orchestrator "
    "must see the result this turn"
)


def test_inline_written_justification_unreachable_does_not_reprint_usage(
    tmp_path, monkeypatch
):
    """Screenshot 2026-09-08: inline hatch prefix + written justification +
    unreachable tg-ctl must deny with unreachable/denied, NOT reprint the
    'set RIG_HATCH_REQUEST_…' usage blurb the agent already followed."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(
        tmp_path / "tg-ctl",
        f"touch {marker}\n"
        "echo 'Telegram hatch daemon unreachable' >&2\n"
        "exit 0\n",
    )
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        f'RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="{_WRITTEN_JUSTIFICATION}" '
        "pytest"
    )
    out, _e, code = _run(command, monkeypatch)  # env not exported
    payload = json.loads(out)
    message = payload["message"]
    assert code == nlip.BLOCK_EXIT_CODE and payload["decision"] == "block"
    assert marker.exists(), "written justification must reach tg-ctl"
    lowered = message.lower()
    assert "hatch escalation denied" in lowered
    assert "unreachable" in lowered
    assert _USAGE_BLURB not in message


def test_inline_written_justification_approve_allows(tmp_path, monkeypatch):
    """Written justification + tg-ctl ask protocol allow (#513) on the inline
    prefix must allow the long process."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = (
        f'RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="{_WRITTEN_JUSTIFICATION}" '
        "pytest"
    )
    out, _e, code = _run(command, monkeypatch)
    assert code == 0 and _decision(out) == "allow"
    assert marker.exists()
    assert "hatch escalation" in json.loads(out)["message"].lower()


@pytest.mark.parametrize("value", ["1", "pytest"])
def test_bare_or_too_short_justification_denies_without_telegram(
    tmp_path, monkeypatch, value
):
    """A bare flag or a single token like 'pytest' is not a written
    justification — deny without Telegram, and say why (screenshot set
    RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS=pytest)."""
    marker = tmp_path / "asked"
    tg_ctl = _fake_tg_ctl(tmp_path / "tg-ctl", f"touch {marker}\n" + _ALLOW_REPLY_SH)
    monkeypatch.setattr(nlip.hatch_escalation, "_TRUSTED_TG_CTL_PATHS", (tg_ctl,))
    command = f'RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="{value}" pytest'
    out, _e, code = _run(command, monkeypatch)
    payload = json.loads(out)
    message = payload["message"]
    assert code == nlip.BLOCK_EXIT_CODE and payload["decision"] == "block"
    assert not marker.exists(), f"{value!r} must not contact Telegram"
    lowered = message.lower()
    assert "hatch escalation denied" in lowered
    assert "justification" in lowered
    assert value in message
    assert _USAGE_BLURB not in message


# ── #584: a suite behind a LAUNCHER that executes it must BLOCK ──────────────────────────────
# `_DIRECT_RUNNERS` matched only a bare `pytest` at argv[0], so the most common spellings of a
# test run — `python3 -m pytest -q` (this repo's documented replay command), `uv run --with pytest
# pytest tests/` (every rig.yaml test script), `npx vitest` — passed the gate ungated. The launcher
# is peeled (its own value-taking options skipped WITH their value) and what it executes is
# classified, recursively.

@pytest.mark.parametrize("command", [
    "python -m pytest",
    "python3 -m pytest -q",
    "python3.12 -m pytest tests/",
    "python -m unittest discover",
    "python -um pytest",
    "python -mpytest",
    "python -Wignore -m pytest",
    "python -W ignore -m pytest",
    "python -X dev -m pytest",
    "PYTHONPATH=lib python3 -m pytest -q",
    "timeout 600 python3 -m pytest",
    "uv run pytest tests/",
    "uv run --with pytest pytest tests/",
    "uv run --with pytest python -m pytest tests/x.py -q",
    "uv --directory /repo run pytest",
    "uv run env CI=1 pytest",
    "uvx pytest",
    "poetry run pytest",
    "pipenv run pytest",
    "pdm run pytest",
    "coverage run -m pytest",
    "coverage run --source=lib -m pytest",
    "tox",
    "tox -e py312",
    "npx vitest run",
    "npx -- vitest",
    "npx playwright test",
    "bunx vitest",
    "pnpm exec vitest",
    "pnpm dlx vitest",
    "yarn dlx jest",
    "yarn run vitest",
    "bun run vitest",
    "npm exec vitest",
    'npm exec --call "vitest run"',               # `-c/--call` runs a SHELL STRING: classified,
    'npx -c "vitest run"',                        # not skipped as a value (round-1 bypass)
    'npx --call="vitest run"',
    "npx -c 'echo hi && pytest'",                 # any command in the nested string counts
    "pnpm test",
    "bun run build",
    "yarn run test",
])
def test_block_suite_behind_launcher(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == nlip.BLOCK_EXIT_CODE, command
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "build/test suite" in payload["message"]


@pytest.mark.parametrize("command", [
    "python3 -m cc_hook_bridge PreToolUse",
    "python -m http.server",
    "python -m pip install x",
    "python tests/smoke.py",
    "python -c 'import pytest'",
    "python - < x",
    "uv run ruff check .",
    "uv run --with pytest ruff check .",
    "uv run --python 3.12 ruff check .",
    "uv run script.py",
    "uvx ruff check .",
    "poetry run ruff check .",
    "pdm run lint",
    "coverage run script.py",
    "npx cowsay hi",
    "npm run dev",                                # `npm run <script>` is not modelled as a launcher
    'npx -c "echo hi"',                           # a nested shell string that is not a suite
    "uv run --unknown-boolean-flag ruff check .", # an option the table does not model: its
                                                  # value (here: ruff) lands at argv[0] → allow
    "bun x cowsay hi",
])
def test_allow_launcher_running_something_else(command, monkeypatch):
    out, _e, code = _run(command, monkeypatch)
    assert code == 0, command
    assert _decision(out) == "allow"


@pytest.mark.parametrize("command", [
    "python3 -m pytest -q",
    "uv run --with pytest pytest tests/",
    "npx vitest run",
])
def test_subagent_exempt_allows_suite_behind_launcher(command, monkeypatch):
    """The subagent exemption is decided BEFORE classification, so the new launcher forms are
    exempt exactly like a bare `pytest` — a dispatched agent is expected to run them."""
    out, _e, code = _run(command, monkeypatch, agent_id="agent-abc123")
    assert code == 0, command
    assert _decision(out) == "allow"


def test_launcher_peel_is_bounded(monkeypatch):
    """A pathological launcher chain stops at the nesting cap and falls through to allow (the
    gate's fail-open default) instead of recursing without bound."""
    command = " ".join(["uv run"] * (nlip._MAX_LAUNCHER_NESTING + 2)) + " pytest"
    out, _e, code = _run(command, monkeypatch)
    assert code == 0
    assert _decision(out) == "allow"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# --- RC-9 regression (agent-tools#743) ------------------------------------------------------
# ≥15 hatch-flood denies were a SANCTIONED subagent's synchronous pre-commit
# `review diff --staged` tripping this guard: the omp task child had lost its identity
# (oh-my-pi#11214) and was gated as the orchestrator. The RC-1 bridge fix (the extension
# reads ctx.sessionManager.getSessionFile(), so the nested child transcript reaches the
# dispatcher payload) restores subagent classification — and _is_subagent already allows a
# subagent's long process outright. No new carve-out in this hook was needed; this test pins
# that assessment end to end with a REAL bridge-produced event.

def test_rc9_omp_task_child_runs_synchronous_review_diff_unblocked(monkeypatch):
    # Identity must come from the PAYLOAD sessionFile alone — scrub every env marker BEFORE
    # building the event so a host-carried value cannot smuggle the classification in.
    for k in ("RIG_AGENT_ID", "RIG_DETACHED_AGENT", "PI_SESSION_FILE",
              "ALLOW_INLINE_PROCESS", "ALLOW_INLINE_PROCESS_REASON",
              "RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS"):
        monkeypatch.delenv(k, raising=False)
    raw = {
        "event": "tool_call",
        "toolName": "bash",
        "input": {"command": "review diff --staged"},
        "cwd": "/repo",
        "toolCallId": "call_1",
        "sessionFile": (
            "/tmp/omp-sessions/-xp-rig-cli/"
            "2026-09-07T21-17-48-938Z_01a07dbc-040a-7089-9724-7f37bfdba15e/ReviewChild.jsonl"
        ),
    }
    v1 = dispatch.to_v1_event(raw, point="pre-bash")
    assert v1["args"]["agent_id"] == "ReviewChild"
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(v1)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = nlip.main()
    assert code == 0 and _decision(out.getvalue()) == "allow"


def test_rc9_orchestrator_review_diff_still_blocked(monkeypatch):
    """The other half of the RC-9 assessment: the SAME command with NO subagent identity
    stays hard-blocked — the fix relaxes nothing for the main-thread orchestrator."""
    out, _e, code = _run("review diff --staged", monkeypatch, harness="omp")
    assert code == nlip.BLOCK_EXIT_CODE and _decision(out) == "block"
