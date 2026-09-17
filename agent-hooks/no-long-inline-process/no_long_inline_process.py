#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — keep long-running processes off the main thread.

A long-running process started inline by the ORCHESTRATOR blocks the main thread for
minutes — a multi-model `review`, a `--watch` loop, a full build/test suite, a long
`sleep`. This gate hard-BLOCKs such a command and tells the orchestrator to dispatch it
to a BACKGROUND subagent instead. It enforces the "orchestrator stays responsive" half of
`delegate-work-to-subagents` (the most clear-cut case → straight block, not warn-first).

Decided from the PARSED command (argv-aware), NEVER a raw substring of the whole string —
the #59 doctrine (block-no-verify / require-review / visual-proof / skills-read). The old
raw-regex anchored its patterns on shell-separator characters (`(`, `|`, `;`, `&&`); those
same characters appear INSIDE quoted argument strings, so a benign `tg --tag report "…we
ran review-qa (review) today…"` or `git commit -m "wire --watch flag"` was mis-read as a
`review`/`--watch` invocation and BLOCKED (agent-tools#60). The command is now shell-
tokenized (quotes/heredocs honored) and split into segments; a long process is flagged ONLY
when the REAL invoked command — `argv[0]` after leading shell noise, inline env, and no-op
wrappers (`timeout`/`env`/`nice`/`time`/…) are peeled off a segment — is the long-running
runner. A keyword that merely appears inside an argument to a DIFFERENT command (tg/echo/git
commit -m/…) no longer trips it, while a genuine inline `review`/`npm test`/`--watch`/long
`sleep` still does.

Flagged, decided per real command SEGMENT (executable parsed, not substring-matched):
  - the `review` CLI as the invoked command (`argv[0]` is `review`) — multi-model, minutes-long
  - a `--watch` flag token in the real args (gh pr checks --watch, vitest --watch, tsc --watch)
  - a build/test SUITE: npm/pnpm/yarn/bun/deno test|build, pytest/vitest/jest/cypress/playwright/
    unittest/tox/nose2, cargo/go test|build, make/rake/msbuild test|build|all, mvn/gradle
    test|build|verify|package — ALSO when it hides behind a LAUNCHER that executes it
    (agent-tools#584): `python[3[.X]] -m pytest|unittest`, `uv run [--with …] pytest`, `uvx`,
    `poetry|pipenv|pdm run`, `coverage run -m`, `npx|bunx`, `npm exec`, `pnpm exec|dlx`,
    `yarn exec|dlx|run`, `bun run|x`. Launchers peel recursively (`uv run python -m pytest`).
  - `sleep N[unit]` as the invoked command with a duration >= 10s (`sleep 5m`/`sleep 1h` count)

LIMITATION (documented under-block, the same precision trade as the sibling hooks' nested
shell-string limit): a runner hidden inside a QUOTED command substitution (`X="$(npm test)"`),
a backtick (`` `npm test` ``), or a nested shell string (`bash -c 'npm test'`, `sh -c 'review'`)
is NOT re-parsed and so is NOT flagged; likewise a runner passed to a NON-peeled forwarder
(`xargs npm test`) keeps that forwarder as argv[0] and is not flagged (`xargs`/`sh`/`bash` are
not in the no-op wrapper set — unlike `timeout`/`env`/`nice` they change execution semantics). Posix tokenization de-quotes a `"$(…)"` and a `'$(…)'` to the SAME token,
so the active (double-quoted) form is indistinguishable from a literal (single-quoted) one
without full quote-context tracking; flagging it would re-open the #60 FALSE-POSITIVE on a
literal `git commit -m '$(npm test) is the command'`. An UNQUOTED substitution/subshell
(`$(npm test)`, `(review)`) IS caught — its opener is a real segment break. Under-blocking is the
safe direction here: this gate is responsiveness discipline (on_error=open), not a boundary.

RESIDUAL LIMITATION (rare OVER-block, same de-quoting collision): a QUOTED token whose value is
EXACTLY a shell separator/opener (`echo ";" review`, `tg "(" review`) de-quotes under posix shlex to
a token equal to the bare `;` / `(`, indistinguishable from a real unquoted separator without
per-token quote-provenance tracking. So such a quoted break still splits the segment and a runner
RIGHT AFTER it (`tg "(" review`) is flagged. The trigger is pathological (a quoted argument equal to
a lone separator, immediately followed by a bare runner token), and the reported #60 case (a keyword
inside a NORMAL multi-word quoted message, `tg --title x "...review..."`) is fully fixed. A precise
fix means a quote-aware separator scan replacing punctuation_chars across this and the sibling hooks'
shared parser (follow-up, not this change). Pinned by `test_quoted_token_equal_to_separator_*`.

Subagent-exempt: a dispatched subagent is EXPECTED to run these in the background,
so it is always allowed — this gate governs the orchestrator only. Identity (any one):
sanitized ``args.agent_id`` (Claude Code child), process env ``RIG_AGENT_ID`` /
``RIG_DETACHED_AGENT=1`` (detached launcher), or a nested omp ``PI_SESSION_FILE``
(``.../<session-id>/<agent>.jsonl``, Task/eval child). omp Task children share the
orchestrator's pre-bash hook with no ``args.agent_id`` — without the env/session
signals they were gated as the orchestrator (agent-tools#635). Do not harness-allowlist
omp/codex/opencode: the orchestrator must still be blocked; omp dispatch is ``hub start name=<id> application=<cmd> args=[...]``
(argv[0] is hub, so this hook does not fire).

Harness-aware remediation (agent-tools#573): this hook has ALWAYS governed every harness (it
never had a `codex`/`opencode`/`omp` carve-out), but its BLOCK message named only Claude
Code's `Agent` tool — a live codex session collected 184 refusals with no delegation path it
could actually follow (the motivating incident for agent-tools#573). The message now reads
`event["harness"]` (bridge-set, unforgeable) and names that harness's own delegation recipe
instead.

External approval (deny-by-default): there is NO self-service bypass. For a genuine exception,
ASK the human, or request a one-time Telegram approval by setting
`RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="<written justification>"` — the hook asks via a
trusted `tg-ctl` and allows ONLY on an explicit approval tap. A blank value, a bare `1`/`true`,
or a single token like `pytest` is rejected (deny), no Telegram call is made. An agent can
request, not self-grant — the human decides.


Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": responsiveness discipline, not a security boundary — a crash must never
wedge the ability to run a command. An UNPARSEABLE command (unbalanced quotes) likewise
ALLOWS (fail-open), the opposite of the fail-closed sibling block-no-verify.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from typing import NamedTuple
import shlex
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


class _UnavailableHatchResult:
    """Hatch result when the repo-local helper cannot be imported.

    should_stop is False so the hook falls through to its own default
    decision (block a gated event, allow otherwise). Crashing here would
    let on_error:open fail-open a denial-class hook (#547).
    """

    requested = False
    approved = False
    reason = "hatch helper unavailable"
    env_var = ""
    env_present = False
    should_stop = False
    tg_ctl_path = None


class _UnavailableHatch:
    def request_hatch_approval(self, *args, **kwargs):
        return _UnavailableHatchResult()


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        return _UnavailableHatch()
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        return _UnavailableHatch()
    module = importlib.util.module_from_spec(spec)
    previous_modules = {
        name: sys.modules[name]
        for name in tuple(sys.modules)
        if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}.")
    }
    for name in previous_modules:
        if name != _HATCH_MODULE:
            sys.modules.pop(name, None)
    sys.modules[_HATCH_MODULE] = module
    # Leave the repo-local module installed on success so later imports in this
    # hook process cannot regain a preloaded user/site package or submodule.
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        for name in tuple(sys.modules):
            if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        # A helper that calls sys.exit() at import must not make the hook exit 0
        # (allow via on_error:open). Ctrl-C still propagates; any other failure
        # installs a stub so the hook can still classify the event (#547).
        if isinstance(exc, KeyboardInterrupt):
            raise
        return _UnavailableHatch()
    return module


hatch_escalation = _load_hatch_escalation()

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Unescaped shell metachars that, like whitespace, are a word boundary at which a `#` opens a comment
# (`cmd ;# note`, `a |# note`) — used by `_strip_line_comment` to match POSIX comment semantics.
_COMMENT_BOUNDARY_METACHARS = frozenset(";&|()")

# ── what counts as a long-running runner (argv-keyed, not substring) ─────────────────────────
# Test/CI runners that are a long process by the NAME ALONE — `pytest`, `vitest`, … as argv[0].
# `unittest` is only ever reached as `python -m unittest`; `tox` and `nose2` are real executables.
_DIRECT_RUNNERS = frozenset({"pytest", "vitest", "jest", "cypress", "playwright", "unittest", "tox", "nose2"})
# Runners that are a suite only WITH a build/test subcommand. The word-boundary patterns mirror
# the original SUITE regex (`\btest\b` matches `test`, `test:unit`, `test-helper`; not `tests`),
# now applied per real arg TOKEN so a quoted multi-word arg of a DIFFERENT command can't match.
_TEST_BUILD = re.compile(r"\b(?:test|build)\b")
_TEST_BUILD_ALL = re.compile(r"\b(?:test|build|all)\b")
_TEST_BUILD_VERIFY_PACKAGE = re.compile(r"\b(?:test|build|verify|package)\b")
_SUITE_RUNNERS: dict[str, re.Pattern[str]] = {
    "npm": _TEST_BUILD, "pnpm": _TEST_BUILD, "yarn": _TEST_BUILD, "bun": _TEST_BUILD,
    "deno": _TEST_BUILD, "cargo": _TEST_BUILD, "go": _TEST_BUILD,
    "make": _TEST_BUILD_ALL, "rake": _TEST_BUILD_ALL, "msbuild": _TEST_BUILD_ALL,
    "mvn": _TEST_BUILD_VERIFY_PACKAGE, "gradle": _TEST_BUILD_VERIFY_PACKAGE,
}
_SLEEP_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
# `sleep N[unit]` operand — the optional unit is parsed to seconds so `sleep 5m` reads as 300s,
# not 5 (a bare \d+ would wave a five-minute sleep through).
_SLEEP_OPERAND = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")

# ── shell tokenization (SYNC: adapted from block-no-verify/block_no_verify.py, itself adapted
# from require-review / visual-proof-gate / skills-read-gate). Each hook is a standalone script
# run as its own subprocess (no shared import path), so the parser is duplicated by design — keep
# the separator/quote/comment/heredoc handling in step with the siblings when changing it. ──────
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
# Command/process-substitution & subshell opener/closer tokens. The OLD raw regex recognized a
# command START right after a bare `(` (its `_CMD_START` anchor), so a long process nested in
# `$(…)`, `<(…)`, or a subshell `( … )` was caught. Tokenized, `$(npm test)` →
# `['$', '(', 'npm', 'test', ')']` and `<(playwright test)` → `['<(', 'playwright', 'test', ')']`,
# so treating these as SEGMENT BREAKS makes the inner runner its OWN segment, inspected by argv[0] —
# restoring the true positives the #60 argv rewrite would otherwise drop (review finding #1). A `(`
# INSIDE quotes stays part of its quoted token (one token, no break) — exactly the #60 false-pos fix.
_SUBSTITUTION_BREAKS = frozenset({"(", ")", "<(", ">("})
_SEGMENT_BREAKS = _SHELL_SEP | _SUBSTITUTION_BREAKS
# shlex(punctuation_chars=True) MERGES consecutive punctuation chars (its default set `();<>|&`) into
# ONE token, so a separator GLUED to a subshell/substitution opener (`git status;(review)` → `;(`,
# `git fetch&&(npm test)` → `&&(`, `(echo hi)&&npm test` → `)&&`) yields a composite token absent from
# _SEGMENT_BREAKS — and the inner runner then never becomes its own segment (an UNDER-block the old
# `\(`-anchored regex did NOT have). `_decompose_punct_token` greedily re-splits an all-punctuation
# token into known break tokens (longest-match first) so each real break is recovered.
_PUNCT_CHARS = frozenset("();<>|&")
_BREAKS_LONGEST_FIRST = sorted(_SEGMENT_BREAKS, key=len, reverse=True)
# Leading tokens that INTRODUCE a command but are not it — the brace-group opener and the control
# keywords whose next token IS a command (a condition: `if`/`elif`/`while`/`until`; a body opener:
# `then`/`do`/`else`). Stripping them recovers the real runner: `if x; then npm test` → (the `then`
# segment) `npm test`. (A `(` subshell opener is handled as a segment break above, not here.)
# `for`/`case` are DELIBERATELY EXCLUDED: their next token is a loop VARIABLE (`for VAR in …`) or a
# selector WORD (`case WORD in …`), NOT a command — stripping them would make that name argv[0] and
# false-block `for review in a b c; …`. Their loop/case BODY is still recovered via the `do`/`then`
# that remain in the set, so excluding them loses no true positive.
_LEADING_SHELL_NOISE = frozenset({
    "{", "!", "if", "then", "do", "else", "elif", "while", "until",
})
# A `VAR=value` token before the executable in a segment is an inline env assignment.
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
# No-op leading WRAPPERS that prefix the real command without changing what it does
# (`timeout 600 npm test`, `env CI=1 pytest`, `nice -n10 review`, `time make build`). The real
# runner sits AFTER the wrapper + its own args, so it is peeled off before inspecting argv[0].
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer"})
# A wrapper flag that takes its value as a SEPARATE token (`-k 5`, `--signal SIGTERM`); a glued
# form (`-n10`, `--kill-after=5`) consumes no extra token.
_WRAPPER_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after", "-n", "-u"})
_MAX_WRAPPER_NESTING = 16

# ── launchers that EXECUTE an operand as the real command (agent-tools#584) ──────────────────
# Unlike a no-op WRAPPER (`timeout 600 npm test`), a launcher is a real tool whose OPERAND is the
# command that actually runs: `python -m pytest`, `uv run pytest`, `npx vitest`, `poetry run pytest`.
# The suite hides behind the launcher's own options, so argv[0] alone never names it — which is how
# `python3 -m pytest -q` (the replay command this repo documents) and `uv run --with pytest pytest
# tests/` (every rig.yaml test script) passed this gate ungated. A launcher's VALUE-taking options
# must be skipped WITH their value or the value would be read as the command (`uv run --with pytest
# ruff check .` runs ruff, not pytest). The table is deliberately incomplete and the residual runs
# both ways, but stays small: an option it does not model leaves that option's VALUE at argv[0],
# which is an allow (the fail-open direction of this discipline gate) unless the value happens to
# spell a runner name (`--future-flag pytest`), a false block on a spelling nobody uses.
class _LauncherSpec(NamedTuple):
    """How a launcher runs its operand. `subcommands`: the executing subcommands (None = the
    launcher itself executes its first operand). `value_flags`: its own options that take a
    SEPARATE value token. `shell_flags`: options whose value is a whole SHELL STRING the launcher
    runs (`npx -c "vitest run"`), classified as a nested command line rather than skipped."""

    subcommands: frozenset[str] | None
    value_flags: frozenset[str]
    shell_flags: frozenset[str] = frozenset()


_UV_VALUE_FLAGS = frozenset({
    "--with", "-w", "--with-requirements", "--with-editable", "--python", "-p", "--project",
    "--directory", "--group", "--only-group", "--extra", "--env-file", "--index", "--default-index",
    "--find-links", "-f", "--package", "--exclude-newer", "--config-file", "--cache-dir",
    "--index-url", "-i", "--extra-index-url", "--constraints", "-c", "--build-constraints", "-b",
    "--overrides", "--link-mode", "--resolution", "--prerelease", "--python-preference", "--color",
    "--config-setting", "-C", "--refresh-package", "--reinstall-package", "--upgrade-package", "-P",
    "--fork-strategy", "--index-strategy", "--keyring-provider", "--python-platform",
})
_NPM_SHELL_FLAGS = frozenset({"-c", "--call"})
_LAUNCHERS: dict[str, _LauncherSpec] = {
    "uv": _LauncherSpec(frozenset({"run"}), _UV_VALUE_FLAGS),
    "uvx": _LauncherSpec(None, _UV_VALUE_FLAGS | {"--from"}),
    "poetry": _LauncherSpec(frozenset({"run"}), frozenset({"-C", "--directory", "-P", "--project"})),
    "pipenv": _LauncherSpec(frozenset({"run"}), frozenset({"--python"})),
    "pdm": _LauncherSpec(frozenset({"run"}), frozenset({"-p", "--project", "-k", "--skip"})),
    # `coverage run -m pytest`: `-m` is skipped like any other option, which lands the module at
    # argv[0] — exactly what coverage's `-m` means.
    "coverage": _LauncherSpec(frozenset({"run"}), frozenset({
        "--source", "--include", "--omit", "--data-file", "--context", "--rcfile", "--concurrency",
    })),
    # `-c/--call` runs its value as a SHELL STRING (`npx -c "vitest run"`): a nested command line,
    # classified as one — skipping it as a plain value would be a one-flag bypass of the gate.
    "npx": _LauncherSpec(None, frozenset({
        "-p", "--package", "--shell", "--cache", "--userconfig", "--registry", "--loglevel", "-w",
        "--workspace", "--prefix",
    }), _NPM_SHELL_FLAGS),
    "bunx": _LauncherSpec(None, frozenset({"-p", "--package", "--shell"})),
    "npm": _LauncherSpec(frozenset({"exec", "x"}), frozenset({
        "-p", "--package", "-w", "--workspace", "--prefix", "-C", "--loglevel", "--registry",
    }), _NPM_SHELL_FLAGS),
    "pnpm": _LauncherSpec(frozenset({"exec", "dlx"}), frozenset({
        "-C", "--dir", "--filter", "-F", "-p", "--package", "--reporter", "--loglevel",
    })),
    "yarn": _LauncherSpec(frozenset({"exec", "dlx", "run"}), frozenset({"--cwd", "-p", "--package"})),
    "bun": _LauncherSpec(frozenset({"run", "x"}), frozenset({"-p", "--package", "--cwd", "--shell"})),
}
# Python interpreters run a suite as a MODULE: `python -m pytest`, `python3.12 -m unittest`.
_PYTHON_EXE = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)?)?$")
# Interpreter short options that take a value (`-W ignore`, `-X dev`, glued `-Wignore`); every other
# short option is a boolean and may be clustered (`-um pytest`). `-m`/`-c` end the option list.
_PYTHON_VALUE_OPTS = frozenset({"W", "X"})
_PYTHON_LONG_VALUE_OPTS = frozenset({"--check-hash-based-pycs"})
_MAX_LAUNCHER_NESTING = 8
# Cap on leading shell-noise tokens peeled from one segment (its own concept, not the wrapper cap) —
# named so the literal can't silently desync from the bound it represents.
_MAX_LEADING_NOISE = 16


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"no-long-inline-process: {msg}\n")


# omp Task/eval child transcript: .../<session-id>/<agent-id>.jsonl
# Main session file is .../<session-id>.jsonl (parent dir is the project slug, not the id).
# Same path-shape detector as the #634 identity work on orchestrator-stays-thin
# (duplicated: each hook is a standalone script; agent-tools#635).
_OMP_SESSION_DIR = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-fA-F-]+$"
)


def _has_omp_session_ancestor(path: Path) -> bool:
    """True when ANY ancestor directory of ``path`` is session-id-shaped, not just the
    immediate parent (live finding, 2026-09-18; SYNC with orchestrator-stays-thin's copy of
    this helper, same rationale): a NESTED Task/eval child — dispatched via `task` from
    WITHIN an already-dispatched subagent, depth >= 2 — nests one directory per additional
    level (oh-my-pi derives each level's own `artifactsDir` from THAT session's own
    `sessionManager.getSessionFile().slice(0, -6)`), so its IMMEDIATE parent is another agent
    id, never the root session id. Walking the full ancestor chain finds the root session-id
    directory at whatever depth it sits. A bare main session file's ancestors are the
    project-slug/`sessions` tree and never session-id-shaped, so this stays False for it."""
    return any(_OMP_SESSION_DIR.match(parent.name) for parent in path.parents)


def _is_omp_nested_session() -> bool:
    """True when PI_SESSION_FILE is an omp Task/eval child transcript, at any nesting depth.

    Main:            .../<session-id>.jsonl
    Direct child:     .../<session-id>/<agent-id>.jsonl
    Nested (depth 2+): .../<session-id>/<agent-id>/<agent-id>/....jsonl

    omp sets PI_SESSION_FILE on the process; a running orchestrator cannot retroactively
    mutate its own process environment (same trust model as RIG_AGENT_ID). Path shape
    only — no filesystem probe, so tests stay hermetic."""
    raw = os.environ.get("PI_SESSION_FILE", "").strip()
    if not raw:
        return False
    path = Path(raw)
    if path.suffix.lower() != ".jsonl" or not path.stem.strip():
        return False
    return _has_omp_session_ancestor(path)


def _is_subagent(event: dict) -> bool:
    """True when this tool use fires INSIDE a dispatched subagent.

    Trusted identity sources — any one is enough (agent-tools#635):
      1. sanitized ``args.agent_id`` — Claude Code child (bridge T2-sanitized)
      2. process env ``RIG_AGENT_ID`` / ``RIG_DETACHED_AGENT=1`` — detached launcher
      3. nested ``PI_SESSION_FILE`` — omp Task/eval child

    TRUST BOUNDARY — these RELAX the gate, so they must not be model-controlled.
    Top-level ``event.agent_id`` is also read (same as args). Empty/whitespace is
    not a subagent."""
    args = event.get("args") or {}
    aid = args.get("agent_id") or event.get("agent_id")
    if aid and str(aid).strip():
        return True
    if os.environ.get("RIG_AGENT_ID", "").strip():
        return True
    if os.environ.get("RIG_DETACHED_AGENT", "").strip() == "1":
        return True
    return _is_omp_nested_session()


def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes (the `#` is found in the RAW text,
    before shlex de-quotes). A `#` starts a comment only at a WORD boundary and only OUTSIDE quotes,
    so a quoted `-m 'fix #42'` keeps its `#`. POSIX backslash-escaping is honored to stay in step with
    shlex (`posix=True` ⇒ `escape='\\'`, `escapedquotes='"'`): outside single quotes a `\\` escapes
    the next char, so a `\\"` does NOT toggle the double-quote state and a `\\#` is not a comment — the
    old loop ignored `\\` and so diverged from shlex, over-blocking `cmd "\\"" # foo; npm test`."""
    in_single = in_double = False
    prev_ws = True  # start-of-line counts as a word boundary
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and not in_single:
            i += 2  # escaped char: skip it so `\"`/`\\`/`\#` can't toggle quoting or open a comment
            prev_ws = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and prev_ws and not in_single and not in_double:
            return line[:i]
        # A `#` opens a comment at a word boundary: after whitespace / line start, but in POSIX shell
        # ALSO right after an unescaped metachar (`;& |()`) outside quotes — `git push ;# note && npm
        # test` is `git push` + a comment in bash. Treating a metachar as a boundary stops the glued
        # `;#` comment from surviving and false-blocking the runner mentioned in the comment text.
        prev_ws = ch.isspace() or (
            ch in _COMMENT_BOUNDARY_METACHARS and not in_single and not in_double
        )
        i += 1
    return line


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line: drop a word-boundary `#` comment (quote-aware) then split GLUED
    separators (`x;npm`, `a&&pytest`) into standalone tokens. `punctuation_chars=True` emits
    `; & | && ||` as their own tokens while honoring quotes. Returns None on unbalanced quotes →
    the caller fails open."""
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # comment already stripped from the raw line (quote-aware)
    try:
        return list(lex)
    except ValueError:
        return None


def _heredoc_terminators(line: str) -> list[tuple[str, bool]]:
    """The here-documents opened on `line` as (terminator-word, dash-form) pairs, FIFO. Detection
    runs on the TOKENIZED line so a `<<WORD` inside a quoted string is absorbed, not a false opener.
    `dash-form` is True for `<<-WORD` (leading TABS allowed on the terminator line)."""
    toks = _tokenize_line(line)
    if toks is None:
        return []
    terms: list[tuple[str, bool]] = []
    for idx, tok in enumerate(toks):
        if tok == "<<" and idx + 1 < len(toks):
            raw = toks[idx + 1]
            dash = raw.startswith("-")
            word = raw.lstrip("-") if dash else raw
            if word:
                terms.append((word, dash))
    return terms


def _heredoc_body_ended(line: str, term: str, dash: bool) -> bool:
    """True when `line` is the here-document terminator. A plain `<<WORD` needs an exact match; only
    `<<-WORD` strips leading TABS."""
    candidate = line.lstrip("\t") if dash else line
    return candidate == term


def _strip_heredocs(lines: list[str]) -> list[str]:
    """Drop here-document BODY lines (data between `<<WORD` and its terminator) so a body line that
    LOOKS like a command (`npm test` inside an `-F -` heredoc message) is not parsed as a segment.
    The opener line is kept; only body + terminator lines are removed. Multiple heredocs on one line
    are consumed FIFO; an unterminated heredoc consumes to EOF."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        terminators = _heredoc_terminators(line)
        i += 1
        for term, dash in terminators:
            while i < len(lines) and not _heredoc_body_ended(lines[i], term, dash):
                i += 1  # a body (data) line — drop it
            if i < len(lines):
                i += 1  # drop the terminator line too
    return out


def _tokenize(command: str) -> list[str] | None:
    """Shell-tokenize a whole (possibly MULTI-LINE) command into a flat token stream where a NEWLINE
    is a command separator (a `;` token between lines). Here-document bodies are stripped first. A
    line that fails to tokenize on its own (a quoted string spanning a newline) is re-joined with
    following lines until it balances. Returns None only if a chunk can never balance → fail open."""
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    # Honor backslash-newline line continuations. NOTE (diverges from bash, safe direction): this is a
    # global raw-text join done BEFORE heredoc stripping and quote tracking, so a literal trailing `\`
    # inside single quotes or inside a quoted-delimiter heredoc body (`<<'EOF'`) is joined as a
    # continuation when bash would not. Worst case it swallows a heredoc terminator → the body is read
    # as data (UNDER-block) — the safe direction for this on_error=open gate; the case is marginal.
    joined = joined.replace("\\\n", "")
    lines = _strip_heredocs(joined.split("\n"))
    out: list[str] = []
    first = True
    i = 0
    while i < len(lines):
        chunk = lines[i]
        toks = _tokenize_line(chunk)
        while toks is None and i + 1 < len(lines):
            i += 1
            chunk = f"{chunk}\n{lines[i]}"
            toks = _tokenize_line(chunk)
        if toks is None:
            return None  # never balances → fail open
        if not first:
            out.append(";")  # the newline that started this chunk ends the previous command
        first = False
        out.extend(toks)
        i += 1
    return out


def _decompose_punct_token(tok: str) -> list[str]:
    """Re-split an ALL-punctuation token (every char in `_PUNCT_CHARS`) into known break tokens by
    greedy longest-match (`;(` → `[';', '(']`, `&&(` → `['&&', '(']`, `)&&` → `[')', '&&']`); a
    recognized break passes through unchanged (`&&` → `['&&']`), an unknown leftover punct char is
    emitted as-is. A token that is NOT all-punctuation is returned as `[tok]` untouched — a redirect
    like `2>` or a quoted word keeps its shape. (A QUOTED token that de-quotes to glued separators,
    `tg ";(" review`, is the documented RESIDUAL over-block class, same as a lone quoted separator.)"""
    if not tok or any(ch not in _PUNCT_CHARS for ch in tok):
        return [tok]
    pieces: list[str] = []
    i = 0
    while i < len(tok):
        for br in _BREAKS_LONGEST_FIRST:
            if tok.startswith(br, i):
                pieces.append(br)
                i += len(br)
                break
        else:
            pieces.append(tok[i])  # unknown lone punct char — keep it, it is not a break
            i += 1
    return pieces


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell command separators (&&, ||, ;, |, &, fused forms) AND on
    command/process-substitution & subshell breaks (`(`, `)`, `<(`, `>(`), so a long process nested
    in `$(…)` / `<(…)` / a subshell becomes its own argv-inspectable segment. A composite punctuation
    token (a separator GLUED to an opener, `;(`/`&&(`/`)&&`) is first decomposed into its real breaks."""
    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        for piece in _decompose_punct_token(tok):
            if piece in _SEGMENT_BREAKS:
                segs.append(cur)
                cur = []
            else:
                cur.append(piece)
    segs.append(cur)
    return segs


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    """Drop leading brace-group openers (`{`, `!`) and control keywords (`then`/`do`/…) so a command
    introduced by them is recovered: `if x; then npm test` → `npm test`. (A `(` subshell opener is
    handled as a segment break in `_segments`.) Capped so an all-noise segment can't loop."""
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _split_inline_env(segment: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off a segment, returning the rest starting at the real
    executable (`CI=1 pytest` → `pytest`). The env values themselves don't affect this gate, so only
    the remainder is returned."""
    i = 0
    while i < len(segment) and _INLINE_ENV.match(segment[i]):
        i += 1
    return segment[i:]


def _basename(tok: str) -> str:
    """The executable name without a leading path — `/usr/bin/timeout` → `timeout`."""
    return tok.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Peel leading no-op wrapper executables (`timeout 600`, `env CI=1`, `nice -n10`, `time`, …) so
    the REAL command beneath is what argv[0] inspection sees. Peels repeatedly (`time timeout 5m
    review`). For a recognized wrapper, its option flags (and their separate values), `env`'s leading
    `VAR=value` operands, and `timeout`'s leading DURATION positional are skipped; a bare wrapper with
    no args of its own (`time make build`, `nohup cargo build`) is dropped to expose the next token.

    Deliberately conservative: a wrapper with a POSITIONAL operand we don't model is NOT in the set, so
    we never peel one only partway and leave a non-command at the head. A non-wrapper head is returned
    unchanged. Guarded against a pathological wrapper chain by a nesting cap (then returned as-is →
    allow, the fail-open default for this discipline gate)."""
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS and guard < _MAX_WRAPPER_NESTING:
        guard += 1
        wrapper, rest = _basename(argv[0]), argv[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if "=" not in tok and tok in _WRAPPER_VALUE_FLAGS:
                    i += 1  # `--signal SIGTERM` / `-k 5` take a separate value token
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional (the wrapped command follows)
            break
        if i:  # consumed the wrapper's own args → real command starts at rest[i:]
            argv = rest[i:]
        elif rest:  # bare wrapper with no args of its own (`time make build`, `nohup cargo build`)
            argv = rest
        else:
            break  # wrapper with nothing after it
    return argv


def _skip_launcher_flags(rest: list[str], spec: _LauncherSpec) -> int:
    """Index of the first token of `rest` that is NOT one of the launcher's own options. A value-
    taking flag skips its separate value (`--with pytest`); a glued `--flag=value` and any other
    `-x` token skip one; a bare `--` ends the options (`npx -- vitest`)."""
    consumes_value = spec.value_flags | spec.shell_flags
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return i + 1
        if not tok.startswith("-"):
            return i
        if "=" not in tok and tok in consumes_value:
            i += 1
        i += 1
    return i


def _launcher_operands(argv: list[str]) -> tuple[_LauncherSpec, list[str]] | None:
    """`(spec, tokens after the executing subcommand)` when argv[0] is a launcher that runs
    something here, else None (not a launcher; or its executing subcommand is missing — the
    launcher's global options may precede it: `uv --directory x run pytest`)."""
    spec = _LAUNCHERS.get(_basename(argv[0]))
    if spec is None:
        return None
    rest = argv[1:]
    if spec.subcommands is not None:
        i = _skip_launcher_flags(rest, spec)
        if i >= len(rest) or rest[i] not in spec.subcommands:
            return None
        rest = rest[i + 1:]
    return spec, rest


def _launcher_shell_command(operands: list[str], spec: _LauncherSpec) -> str | None:
    """The SHELL STRING a launcher's `-c/--call` option runs (`npx -c "vitest run"`,
    `npm exec --call="vitest run"`), when one sits among the launcher's options; else None."""
    if not spec.shell_flags:
        return None
    i = 0
    while i < len(operands):
        tok = operands[i]
        if tok == "--" or not tok.startswith("-"):
            return None
        if tok in spec.shell_flags:
            return operands[i + 1] if i + 1 < len(operands) else None
        for flag in spec.shell_flags:
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1:]
        if "=" not in tok and tok in spec.value_flags:
            i += 1
        i += 1
    return None


# Separate from `_skip_launcher_flags` on purpose: the interpreter CLUSTERS and GLUES short options
# (`-um pytest`, `-Wignore`), which the table-driven whole-token launcher loop has no equivalent of.
def _strip_python_module(argv: list[str]) -> list[str] | None:
    """`<module> <args…>` for an interpreter run of a MODULE (`python3 -m pytest -q`, `python -um
    pytest`, `python -Wignore -m unittest`), else None: a script path, `-c code`, stdin, or a bare
    interpreter is not a module run. Only `-m` is modelled — it is how every Python test runner is
    invoked when it is not on PATH."""
    if not _PYTHON_EXE.match(_basename(argv[0])):
        return None
    rest = argv[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok.startswith("-") or tok in ("-", "--"):
            return None  # a script path (or stdin) runs, not a module
        if tok.startswith("--"):
            i += 2 if tok in _PYTHON_LONG_VALUE_OPTS else 1
            continue
        for j, ch in enumerate(tok[1:]):
            glued = tok[j + 2:]
            if ch == "m":
                return [glued, *rest[i + 1:]] if glued else (rest[i + 1:] or None)
            if ch == "c":
                return None  # inline code: not a module run
            if ch in _PYTHON_VALUE_OPTS:
                if not glued:
                    i += 1  # the value is the next token
                break
        i += 1
    return None


def _is_suite(exe: str, rest: list[str]) -> bool:
    """True when the segment is a build/test suite: a direct runner by name, or a package/build tool
    with a build/test subcommand token in its real args."""
    if exe in _DIRECT_RUNNERS:
        return True
    pat = _SUITE_RUNNERS.get(exe)
    if pat is None:
        return False
    return any(pat.search(tok) for tok in rest)


def _sleep_label(exe: str, rest: list[str]) -> str | None:
    """Label for a long `sleep` (>= 10s) invoked as the command, else None. The first operand is the
    duration; its unit suffix is parsed to seconds (`sleep 5m` = 300s)."""
    if exe != "sleep" or not rest:
        return None
    m = _SLEEP_OPERAND.match(rest[0])
    if not m:
        return None
    seconds = float(m.group(1)) * _SLEEP_UNIT_S[m.group(2)]
    if seconds >= 10:
        return f"sleep {m.group(1)}{m.group(2)} (long sleep)"
    return None


def _is_watch_flag(tok: str) -> bool:
    """True for a real `--watch` flag token: `--watch`, `--watch=…`, or a hyphenated subform
    `--watch-poll`/`--watch-mode`. This mirrors the old `--watch\\b` word-boundary match (a `-` is a
    boundary) WITHOUT its substring leak: applied per real ARG TOKEN, a quoted argument that merely
    CONTAINS `--watch` mid-string (`git commit -m 'wire --watch flag'`) is one token unequal to any of
    these. A real flag token has NO whitespace, so a de-quoted MULTI-WORD message that happens to START
    with `--watch-`/`--watch=` (`git commit -m '--watch-poll: new default'`) is rejected by the
    whitespace guard — without it the prefix checks would re-open the #60 false-positive on such a
    message. `--watcher`/`--watchAll` (a word char after `watch`, no boundary) are not flags, as before.
    Residual (same quote-provenance class as the module docstring's RESIDUAL LIMITATION): a SINGLE-word
    quoted arg that is exactly a flag (`echo "--watch=1"`) is indistinguishable from the real flag; and
    conversely the whitespace guard UNDER-blocks a genuine `--watch="src dir"` value that contains a
    space (de-quoted to one token WITH whitespace). Under-block is the safe direction for this
    on_error=open discipline gate, and a watch flag taking a space-bearing value is rare."""
    if any(ch.isspace() for ch in tok):  # de-quoted multi-word message, not a flag token
        return False
    return tok == "--watch" or tok.startswith("--watch=") or tok.startswith("--watch-")


def _segment_long_process(segment: list[str], depth: int = 0) -> str | None:
    """Inspect one already-split command segment and return a long-process label, or None. Leading
    shell noise, inline env assignments, and no-op wrappers are peeled so argv[0] is the REAL
    invoked command before any keyword check runs."""
    argv = _strip_wrappers(_split_inline_env(_strip_leading_shell_noise(segment)))
    return _long_process_label(argv, depth)


def _long_process_label(argv: list[str], depth: int = 0) -> str | None:
    """Return a long-process label for an already-peeled argv (review/--watch/suite/sleep). When
    the REAL command is a launcher or an interpreter module run, peel it too and classify what it
    EXECUTES (`uv run python -m pytest` → `python -m pytest` → `pytest`), re-peeling wrappers at
    each step (`uv run env CI=1 pytest`); a launcher's `-c/--call` shell string is classified as a
    nested command line. Bounded by `_MAX_LAUNCHER_NESTING`, then allow (fail-open)."""
    if not argv:
        return None
    exe = _basename(argv[0])
    rest = argv[1:]
    if exe == "review":
        return "review (multi-model, minutes-long)"
    if any(_is_watch_flag(tok) for tok in rest):
        return "--watch (a watch loop never exits)"
    if _is_suite(exe, rest):
        return "a build/test suite"
    label = _sleep_label(exe, rest)
    if label or depth >= _MAX_LAUNCHER_NESTING:
        return label
    found = _launcher_operands(argv)
    if found is None:
        inner = _strip_python_module(argv)
    else:
        spec, operands = found
        shell = _launcher_shell_command(operands, spec)
        if shell is not None:
            return _nested_shell_label(shell, depth + 1)
        inner = operands[_skip_launcher_flags(operands, spec):] or None
    if inner is None:
        return None
    return _long_process_label(_strip_wrappers(inner), depth + 1)


def _nested_shell_label(shell: str, depth: int) -> str | None:
    """Classify the shell string a launcher runs (`npx -c "vitest run"`) as its own command line."""
    return _matched_long_process(shell, depth)


def _matched_long_process(command: str, depth: int = 0) -> str | None:
    """Return a short label of the matched long-running process, or None. The command is shell-
    tokenized (quotes/heredocs honored) and split into segments; each segment is inspected by its
    REAL invoked command (`argv[0]` after peeling). A keyword inside a quoted argument to a different
    command never trips the guard. An unparseable command (unbalanced quotes) returns None →
    fail-open, consistent with this hook's on_error=open policy."""
    tokens = _tokenize(command)
    if tokens is None:
        return None
    for segment in _segments(tokens):
        label = _segment_long_process(segment, depth)
        if label:
            return label
    return None


# Per-harness delegation recipe named in the block message (agent-tools#573): this hook has
# ALWAYS governed every harness (no EXEMPT_HARNESSES carve-out here), but its block message
# named only Claude Code's `Agent` tool — a live codex session collected 184 refusals with no
# delegation path it could actually follow. SYNC: the same recipe map is duplicated in
# `agent-hooks/orchestrator-stays-thin` and `agent-hooks/background-subagent-gate` (each
# agent-hook is self-contained, no shared import across agent-hooks/ dirs) — keep the four
# harness values in step if a new one is added.
_HARNESS_RECIPES = {
    "claude-code": (
        "Dispatch it as an Agent with `subagent_type: \"fork\"` or `isolation: \"remote\"` "
        "(both run in the background)"
    ),
    "codex": (
        "Dispatch it via the `rig-detached-codex` launcher skill for a truly detached child, "
        "or the native `spawn_agent` tool where your build has one"
    ),
    "opencode": (
        "Dispatch it with Task (subagent_type general or explore), or the "
        "rig-detached-opencode launcher for a truly detached child"
    ),
    "omp": (
        "Dispatch it with `hub start name=<id> application=<cmd> args=[...]` "
        "(argv[0] is hub, so this hook does not fire), or a Task/eval subagent"
    ),
}


def _delegation_recipe(harness) -> str:
    """The delegation recipe text for `harness`, falling back to Claude Code's."""
    return _HARNESS_RECIPES.get(str(harness or "").strip(), _HARNESS_RECIPES["claude-code"])


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    # A subagent is EXPECTED to run these in the background → always allowed.
    if _is_subagent(event):
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    matched = _matched_long_process(command)
    if matched is None:
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or args.get("cwd") or os.getcwd())
    block_message = (
        f"Run this in a BACKGROUND subagent, not the orchestrator: `{matched}` is a "
        "long-running process (review / --watch / build-or-test suite / long sleep) that "
        f"would block the main thread. {_delegation_recipe(event.get('harness'))}. There is "
        "NO self-service bypass. For a genuine exception, ASK the human, or "
        "request a one-time Telegram approval by setting "
        "RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS=\"<written justification>\" "
        "(deny-by-default; a bare 1 or a single token like pytest is rejected)."
    )

    ctx = {"hook": "no-long-inline-process", "command": command}
    hatch = hatch_escalation.request_hatch_approval(
        "no-long-inline-process", ctx, cwd=cwd, command=command
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"no-long-inline-process allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        # Hatch already ran. Do not reprint the "set RIG_HATCH_REQUEST_…" how-to —
        # the agent followed it and needs the deny reason (unreachable, too short,
        # declined), not the same usage blurb.
        emit("block", f"hatch escalation denied: {hatch.reason}")
        return BLOCK_EXIT_CODE

    emit("block", block_message)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
