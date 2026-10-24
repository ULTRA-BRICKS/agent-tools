#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — in a repo that says "heavy work runs on Runpod", a
whole test suite, a type-check of the whole tree or a docker build does not run locally.

WHY THIS EXISTS (2026-10-10 and 2026-10-11): the shared Linux workstation (48 GB RAM,
16 GB swap, many omp agent sessions in parallel) thrashed to a manual reset twice in two
days. On 10-10 one session's subagent swarm ran dozens of concurrent `bun test ./tests`
full suites plus `review diff`; on 10-11 three concurrent `review diff` runs, `uv run
mypy` + `pytest` with testcontainers docker and several `bun test` runs lost 26 GB in two
minutes on an already-full swap. The projects' AGENTS.md files ALREADY said "Heavy work
runs on Runpod: locally a single test file at most; full suite / mypy / integration tests
go to a Runpod pod via `runpod-run`" — and the agents ignored it, partly because a
`runpod-run` had hung on ssh earlier that day. A rule in a doc is advice; this hook is
the rule.

WHAT IT GATES. Only when the current repo opts in — an `AGENTS.md` or `CLAUDE.md` at the
cwd or any parent mentions `runpod-run` or the phrase `Heavy work runs on Runpod` — it
blocks the whole-suite shapes locally:
  * `pytest` (also `uv run pytest`, `python -m pytest`, `poetry run pytest`, …) with no
    path, with a directory path, or with more than two files;
  * `scripts/check.sh` (a repo's whole check pipeline);
  * `mypy` with anything other than exactly one file (`-p`/`-m` package runs included);
  * `bun test` / `vitest` / `jest` with no file args or a directory;
  * `npm|pnpm|yarn|bun test` and `… run test` (the package's whole test script);
  * `cargo test` without `--test <name>`, `go test ./...` / `go test` with no package;
  * `docker build`, `docker buildx build`, `docker compose up`, `docker-compose up`.
A single test file stays allowed (`pytest tests/unit/test_x.py`, `bun test
tests/x.test.ts`, `mypy src/mod.py`), as do `--help`/`--version`. The block message carries
the exact `runpod-run -- bash -c '<original command>'` equivalent.

Detection is argv-aware, not a raw substring search: the command is shell-tokenized
(quotes, comments and heredocs honored), split into segments, and each segment's REAL
command is found after peeling `cd x &&`-style chains (each side is its own segment),
inline `VAR=val`, no-op wrappers (`timeout N`, `env`, `nice`, …) and launchers (`uv run`,
`python -m`, `npx`, …). The tokenizer is the same duplicated parser the sibling hooks
carry (no-long-inline-process, block-no-verify, …): each hook is a standalone script.

Escape hatch: the same deny-by-default Telegram hatch every hard-block hook here uses —
`RIG_HATCH_REQUEST_RUNPOD_OFFLOAD_GATE="<written justification>"` asks the human live;
a blank, a bare `1`, or a single token is rejected without a call.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the working dir in cwd
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash here must never wedge an agent's ability to run a command;
the gate is a workstation-protection discipline, not a security boundary. Applies to
every session alike — the orchestrator AND its subagents (the 10-10 swarm was subagents).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import NamedTuple

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

# The role-aware "what to do next" builder (agent-tools#994). Loaded by repo-local path like the
# hatch helper and OPTIONAL: any load failure falls back to the neutral wording in `_render_block`.
_GUARD_MESSAGE_MODULE = "agenttools_guard_message"


def _load_guard_message():
    init = Path(__file__).resolve().parents[2] / "lib" / _GUARD_MESSAGE_MODULE / "__init__.py"
    if not init.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_GUARD_MESSAGE_MODULE, init)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_GUARD_MESSAGE_MODULE)
    # dataclasses resolves string annotations through sys.modules[cls.__module__].
    sys.modules[_GUARD_MESSAGE_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        if previous is None:
            sys.modules.pop(_GUARD_MESSAGE_MODULE, None)
        else:
            sys.modules[_GUARD_MESSAGE_MODULE] = previous
        if isinstance(exc, KeyboardInterrupt):
            raise
        return None
    return module


_guard_message = _load_guard_message()
# The event main() is deciding, so the block emitter can name the reader's role.
_CURRENT_EVENT: dict = {}


def _render_block(what: str, self_serve: str, relay: str = "", human: str = "") -> str:
    """Role-aware ladder via the shared builder; neutral two-branch text if the lib is unusable."""
    if _guard_message is not None:
        try:
            role = _guard_message.detect_role(_CURRENT_EVENT.get("event") or {})
            return _guard_message.render(
                _guard_message.Remedy(what=what, self_serve=self_serve, relay=relay, human=human),
                role,
            )
        except Exception:  # noqa: BLE001 - a wording helper must never turn a block into a crash
            pass
    top = (
        "3. If you are the top-level agent: "
        + (relay or "supply the missing thing to a blocked subagent with `write agent://<id>`, then `wait`.")
    )
    if human:
        top += f" Last resort, genuine exceptions only: {human}"
    return "\n".join(
        [
            what,
            f"1. {self_serve}",
            "2. If you were dispatched by another agent: `write agent://<parent-id>` with what is "
            "missing + what you tried, then `yield` status `awaiting-coordinator` (NOT "
            "blocked/done/failed); the coordinator's `write agent://<you>` revives you. Never "
            "retry with RIG_HATCH_REQUEST_* and never go to the human.",
            top,
        ]
    )

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "runpod-offload-gate"

# ── the repo opt-in ───────────────────────────────────────────────────────────────────────────
# A repo is gated when its agent instructions name the offload script or state the rule. Both
# phrases come from the projects' own AGENTS.md "Heavy work runs on Runpod" section and the
# runpod-offload skill; a repo without either keeps running its suites locally, unchanged.
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
_OPT_IN_MARKERS = ("runpod-run", "Heavy work runs on Runpod")
_MAX_INSTRUCTION_BYTES = 512 * 1024

# Unescaped shell metachars that, like whitespace, are a word boundary at which a `#` opens a comment
# (`cmd ;# note`, `a |# note`) — used by `_strip_line_comment` to match POSIX comment semantics.
_COMMENT_BOUNDARY_METACHARS = frozenset(";&|()")

# ── shell tokenization (SYNC: adapted from no-long-inline-process/no_long_inline_process.py, itself
# adapted from block-no-verify / require-review / visual-proof-gate / skills-read-gate). Each hook is
# a standalone script run as its own subprocess (no shared import path), so the parser is duplicated
# by design — keep the separator/quote/comment/heredoc handling in step with the siblings. ──────
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
# Command/process-substitution & subshell opener/closer tokens: a runner nested in `$(…)`, `<(…)`,
# or a subshell `( … )` becomes its OWN segment, inspected by argv[0]. A `(` INSIDE quotes stays
# part of its quoted token (one token, no break).
_SUBSTITUTION_BREAKS = frozenset({"(", ")", "<(", ">("})
_SEGMENT_BREAKS = _SHELL_SEP | _SUBSTITUTION_BREAKS
# shlex(punctuation_chars=True) MERGES consecutive punctuation chars (its default set `();<>|&`) into
# ONE token, so a separator GLUED to a subshell/substitution opener (`git status;(pytest)` → `;(`)
# yields a composite token absent from _SEGMENT_BREAKS. `_decompose_punct_token` greedily re-splits
# an all-punctuation token into known break tokens (longest-match first) so each break is recovered.
_PUNCT_CHARS = frozenset("();<>|&")
_BREAKS_LONGEST_FIRST = sorted(_SEGMENT_BREAKS, key=len, reverse=True)
# Leading tokens that INTRODUCE a command but are not it — the brace-group opener and the control
# keywords whose next token IS a command. `for`/`case` are DELIBERATELY EXCLUDED: their next token
# is a loop VARIABLE or a selector WORD, not a command; their body is still recovered via `do`/`then`.
_LEADING_SHELL_NOISE = frozenset({
    "{", "!", "if", "then", "do", "else", "elif", "while", "until",
})
# A `VAR=value` token before the executable in a segment is an inline env assignment.
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
# No-op leading WRAPPERS that prefix the real command without changing what it does
# (`timeout 600 bun test`, `env CI=1 pytest`, `nice -n10 mypy`, `time make check`). The real
# runner sits AFTER the wrapper + its own args, so it is peeled off before inspecting argv[0].
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer"})
# A wrapper flag that takes its value as a SEPARATE token (`-k 5`, `--signal SIGTERM`); a glued
# form (`-n10`, `--kill-after=5`) consumes no extra token.
_WRAPPER_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after", "-n", "-u"})
_MAX_WRAPPER_NESTING = 16


# ── launchers that EXECUTE an operand as the real command ──────────────────────────────────────
# A launcher is a real tool whose OPERAND is the command that actually runs: `python -m pytest`,
# `uv run pytest`, `npx vitest`, `poetry run pytest`. A launcher's VALUE-taking options must be
# skipped WITH their value or the value would be read as the command (`uv run --with pytest ruff
# check .` runs ruff, not pytest). An option the table does not model leaves that option's VALUE at
# argv[0], which is an allow (the fail-open direction of this discipline gate).
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
    "yarn": _LauncherSpec(frozenset({"exec", "dlx"}), frozenset({"--cwd", "-p", "--package"})),
    "bun": _LauncherSpec(frozenset({"x"}), frozenset({"-p", "--package", "--cwd", "--shell"})),
}
# Python interpreters run a suite as a MODULE: `python -m pytest`, `python3.12 -m mypy`.
_PYTHON_EXE = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)?)?$")
# Interpreter short options that take a value (`-W ignore`, `-X dev`, glued `-Wignore`); every other
# short option is a boolean and may be clustered (`-um pytest`). `-m`/`-c` end the option list.
_PYTHON_VALUE_OPTS = frozenset({"W", "X"})
_PYTHON_LONG_VALUE_OPTS = frozenset({"--check-hash-based-pycs"})
_MAX_LAUNCHER_NESTING = 8
# Cap on leading shell-noise tokens peeled from one segment (its own concept, not the wrapper cap).
_MAX_LEADING_NOISE = 16

# ── the whole-suite shapes ───────────────────────────────────────────────────────────────────
# Options of each runner that take their value as a SEPARATE token; the value must not be read
# as a test path (`pytest -k slow tests/x.py` selects by keyword, `slow` is not a file).
_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "-p", "-o", "-c", "-W", "-n", "--rootdir", "--confcutdir", "--basetemp",
    "--junitxml", "--junit-xml", "--maxfail", "--tb", "--log-level", "--log-cli-level",
    "--durations", "--timeout", "--deselect", "--ignore", "--ignore-glob", "--override-ini",
    "--import-mode", "--dist", "--numprocesses", "--color", "--capture", "--pdbcls", "-r",
})
_MYPY_VALUE_FLAGS = frozenset({
    "-p", "--package", "-m", "--module", "-c", "--command", "--config-file", "--python-version",
    "--python-executable", "--platform", "--cache-dir", "--always-true", "--always-false",
    "--shadow-file", "--junit-xml", "--exclude", "--custom-typeshed-dir", "--enable-error-code",
    "--disable-error-code", "--cobertura-xml-report", "--html-report", "--txt-report",
    "--any-exprs-report", "--linecount-report", "--lineprecision-report", "--xml-report",
})
# `-p pkg` / `-m mod` type-check a whole package/module tree, never one file.
_MYPY_TREE_FLAGS = frozenset({"-p", "--package", "-m", "--module"})
_JS_RUNNER_VALUE_FLAGS = {
    "vitest": frozenset({
        "-c", "--config", "-r", "--root", "--dir", "--project", "--reporter", "--outputFile",
        "-t", "--testNamePattern", "--pool", "--environment", "--mode", "--shard", "--maxWorkers",
        "--minWorkers", "--silent",
    }),
    "jest": frozenset({
        "-c", "--config", "-t", "--testNamePattern", "--rootDir", "--testPathPattern",
        "--maxWorkers", "-w", "--selectProjects", "--reporters", "--coverageDirectory",
        "--testTimeout", "--shard", "--outputFile",
    }),
    "bun test": frozenset({
        "-t", "--test-name-pattern", "--timeout", "--rerun-each", "--coverage-reporter",
        "--coverage-dir", "--preload", "-r", "--cwd", "--env-file", "--reporter",
        "--reporter-outfile", "--shard",
    }),
}
_VITEST_SUBCOMMANDS = frozenset({"run", "watch", "dev", "related", "bench", "list"})
_PYTEST_EXES = frozenset({"pytest", "py.test"})
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_INFO_FLAGS = frozenset({"--help", "-h", "--version", "-V", "--co", "--collect-only"})
_MAX_PYTEST_FILES = 2
# Shell builtins that run their operand as the command: a one-word bypass if not peeled.
_SHELL_BUILTIN_RUNNERS = frozenset({"command", "exec", "builtin"})
# GLOBAL options (before the subcommand) that take a separate value, per tool — the value must
# not be read as the subcommand (`cargo --manifest-path Cargo.toml test` runs `test`).
_CARGO_GLOBAL_VALUE_FLAGS = frozenset({"--manifest-path", "--config", "-Z", "--target-dir", "--color", "-j", "--jobs", "-C"})
_GO_GLOBAL_VALUE_FLAGS = frozenset({"-C"})
_PM_GLOBAL_VALUE_FLAGS = frozenset({
    "--prefix", "-C", "--cwd", "--dir", "--filter", "-F", "--loglevel", "--registry", "-w",
    "--workspace", "--reporter", "--userconfig", "--cache", "--config", "-c", "--env-file",
})
_DOCKER_GLOBAL_VALUE_FLAGS = frozenset({"-H", "--host", "--context", "-c", "--config", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"})


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


# ── repo opt-in ──────────────────────────────────────────────────────────────────────────────

def repo_opt_in_file(cwd: str) -> Path | None:
    """The nearest `AGENTS.md`/`CLAUDE.md` at or above `cwd` that carries an opt-in marker, or
    None. Every parent is checked (a sub-package's own AGENTS.md may say nothing while the repo
    root's states the rule). Unreadable files are skipped — fail-open per file."""
    try:
        here = Path(cwd).resolve()
    except OSError:
        return None
    for directory in (here, *here.parents):
        for name in _INSTRUCTION_FILES:
            candidate = directory / name
            try:
                if not candidate.is_file():
                    continue
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(_MAX_INSTRUCTION_BYTES)
            except OSError:
                continue
            if any(marker in text for marker in _OPT_IN_MARKERS):
                return candidate
    return None


# ── tokenizer ────────────────────────────────────────────────────────────────────────────────

def _strip_line_comment(line: str) -> str:
    """Cut a `#` shell comment to end-of-line, RESPECTING quotes (the `#` is found in the RAW text,
    before shlex de-quotes). A `#` starts a comment only at a WORD boundary and only OUTSIDE quotes,
    so a quoted `-m 'fix #42'` keeps its `#`. POSIX backslash-escaping is honored to stay in step with
    shlex (`posix=True` ⇒ `escape='\\'`, `escapedquotes='"'`): outside single quotes a `\\` escapes
    the next char, so a `\\"` does NOT toggle the double-quote state and a `\\#` is not a comment."""
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
        # ALSO right after an unescaped metachar (`;& |()`) outside quotes.
        prev_ws = ch.isspace() or (
            ch in _COMMENT_BOUNDARY_METACHARS and not in_single and not in_double
        )
        i += 1
    return line


def _tokenize_line(line: str) -> list[str] | None:
    """Tokenize ONE physical line: drop a word-boundary `#` comment (quote-aware) then split GLUED
    separators (`x;pytest`, `a&&pytest`) into standalone tokens. `punctuation_chars=True` emits
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
    LOOKS like a command (`bun test` inside an `-F -` heredoc message) is not parsed as a segment.
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
    # Honor backslash-newline line continuations (a global raw-text join, same as the siblings).
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
    greedy longest-match (`;(` → `[';', '(']`, `&&(` → `['&&', '(']`); a recognized break passes
    through unchanged, an unknown leftover punct char is emitted as-is. A token that is NOT
    all-punctuation is returned as `[tok]` untouched — a redirect like `2>` keeps its shape."""
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
    command/process-substitution & subshell breaks (`(`, `)`, `<(`, `>(`), so a runner nested in
    `$(…)` / `<(…)` / a subshell becomes its own argv-inspectable segment."""
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
    introduced by them is recovered: `if x; then bun test` → `bun test`. Capped so an all-noise
    segment can't loop."""
    i = 0
    while i < len(segment) and i < _MAX_LEADING_NOISE and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _split_inline_env(segment: list[str]) -> list[str]:
    """Peel leading `VAR=value` assignments off a segment, returning the rest starting at the real
    executable (`CI=1 pytest` → `pytest`)."""
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
    pytest`). For a recognized wrapper, its option flags (and their separate values), `env`'s leading
    `VAR=value` operands, and `timeout`'s leading DURATION positional are skipped; a bare wrapper with
    no args of its own (`time make check`, `nohup cargo test`) is dropped to expose the next token.
    Guarded against a pathological wrapper chain by a nesting cap (then returned as-is → allow)."""
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
        elif rest:  # bare wrapper with no args of its own (`time make check`, `nohup cargo test`)
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


def _strip_python_module(argv: list[str]) -> list[str] | None:
    """`<module> <args…>` for an interpreter run of a MODULE (`python3 -m pytest -q`, `python -um
    pytest`, `python -Wignore -m mypy`), else None: a script path, `-c code`, stdin, or a bare
    interpreter is not a module run."""
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


# ── whole-suite classification ───────────────────────────────────────────────────────────────

def _positionals(rest: list[str], value_flags: frozenset[str]) -> list[str]:
    """The non-option operands of a runner invocation: a value-taking option consumes its separate
    value token; `--flag=value` and every other `-x` consume one; `--` ends the options."""
    out: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            out.extend(rest[i + 1:])
            break
        if tok.startswith("-") and len(tok) > 1:
            if "=" not in tok and tok in value_flags:
                i += 1
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _is_directory_operand(operand: str, cwd: str) -> bool:
    """A test operand names a DIRECTORY (a whole tree) when it ends in `/`, exists as a directory
    under `cwd`, or — for a path that does not exist here (another checkout, a `cd` earlier in
    the chain) — has no extension in its last component. `tests/x.py::TestFoo::test_bar` is a
    file: the node-id part after `::` is ignored."""
    path = operand.split("::", 1)[0]
    if not path or path.endswith("/"):
        return True
    try:
        if os.path.isdir(os.path.join(cwd, os.path.expanduser(path))):
            return True
        if os.path.isfile(os.path.join(cwd, os.path.expanduser(path))):
            return False
    except (OSError, ValueError):
        pass
    return "." not in _basename(path)


def _pytest_verdict(rest: list[str], cwd: str) -> str | None:
    if any(tok in _INFO_FLAGS for tok in rest):
        return None
    paths = _positionals(rest, _PYTEST_VALUE_FLAGS)
    if not paths:
        return "pytest with no path (the whole suite)"
    dirs = [p for p in paths if _is_directory_operand(p, cwd)]
    if dirs:
        return f"pytest on a directory ({dirs[0]!r}: a whole tree of tests)"
    if len(paths) > _MAX_PYTEST_FILES:
        return f"pytest on {len(paths)} files (more than {_MAX_PYTEST_FILES})"
    return None


def _mypy_verdict(rest: list[str], cwd: str) -> str | None:
    if any(tok in _INFO_FLAGS for tok in rest):
        return None
    if any(tok in _MYPY_TREE_FLAGS or tok.split("=", 1)[0] in _MYPY_TREE_FLAGS for tok in rest):
        return "mypy on a whole package/module tree (-p / -m)"
    paths = _positionals(rest, _MYPY_VALUE_FLAGS)
    if not paths:
        return "mypy with no file (the configured whole tree)"
    if len(paths) > 1:
        return f"mypy on {len(paths)} targets (more than one file)"
    if _is_directory_operand(paths[0], cwd):
        return f"mypy on a directory ({paths[0]!r})"
    return None


def _js_runner_verdict(runner: str, rest: list[str], cwd: str) -> str | None:
    if any(tok in _INFO_FLAGS for tok in rest):
        return None
    if runner == "vitest" and rest and rest[0] in _VITEST_SUBCOMMANDS:
        rest = rest[1:]
    files = _positionals(rest, _JS_RUNNER_VALUE_FLAGS[runner])
    if not files:
        return f"{runner} with no file (the whole suite)"
    dirs = [f for f in files if _is_directory_operand(f, cwd)]
    if dirs:
        return f"{runner} on a directory ({dirs[0]!r}: a whole tree of tests)"
    return None


def _after_global_options(rest: list[str], value_flags: frozenset[str]) -> list[str]:
    """`rest` from its first real subcommand/script word on, skipping the tool's GLOBAL options
    that may precede it — a value-taking one with its separate value (`cargo --manifest-path
    Cargo.toml test`, `npm --prefix packages/web test`, `bun --cwd pkg test`; review finding)."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return rest[i + 1:]
        if not tok.startswith("-"):
            return rest[i:]
        if "=" not in tok and tok in value_flags:
            i += 1
        i += 1
    return []


def _package_script_verdict(exe: str, rest: list[str]) -> str | None:
    """`npm test`, `npm t`, `pnpm run test`, `yarn test`, `bun run test`: the package's whole test
    script. Global options before the script name (`npm --prefix pkg test`) are skipped."""
    words = _after_global_options(rest, _PM_GLOBAL_VALUE_FLAGS)
    if words and words[0] in {"run", "run-script"}:
        words = _after_global_options(words[1:], _PM_GLOBAL_VALUE_FLAGS)
    if words and words[0] in {"test", "t", "tst"}:
        return f"{exe} test (the package's whole test script)"
    return None


def _docker_verdict(exe: str, rest: list[str]) -> str | None:
    words = [tok for tok in _after_global_options(rest, _DOCKER_GLOBAL_VALUE_FLAGS) if not tok.startswith("-")][:3]
    if exe == "docker-compose":
        return "docker-compose up (an integration stack)" if "up" in words else None
    if "build" in words[:2]:  # `docker build .`, `docker buildx build .`, `docker image build`
        return "docker build (an image build on the workstation)"
    if words[:1] == ["compose"] and "up" in words[1:3]:
        return "docker compose up (an integration stack)"
    return None


def _cargo_verdict(rest: list[str]) -> str | None:
    rest = _after_global_options(rest, _CARGO_GLOBAL_VALUE_FLAGS)
    if rest[:1] != ["test"]:
        return None
    if any(tok == "--test" or tok.startswith("--test=") for tok in rest):
        return None  # one integration-test target
    return "cargo test (the whole crate)"


def _go_verdict(rest: list[str]) -> str | None:
    rest = _after_global_options(rest, _GO_GLOBAL_VALUE_FLAGS)
    if rest[:1] != ["test"]:
        return None
    packages = [tok for tok in rest[1:] if not tok.startswith("-")]
    if not packages or any(tok.endswith("...") for tok in packages):
        return "go test ./... (the whole module)"
    return None


def _strip_shell_builtins(argv: list[str]) -> list[str] | None:
    """`command pytest tests/`, `exec bun test`, `builtin …`: the shell runs the operand (review
    finding — a one-word bypass otherwise). `command -v/-V name` only LOOKS a command up, so it
    is not a run (None → allow); `exec`'s own `-a NAME`/`-c`/`-l` options are skipped."""
    guard = 0
    while argv and argv[0] in _SHELL_BUILTIN_RUNNERS and guard < _MAX_WRAPPER_NESTING:
        guard += 1
        builtin, rest = argv[0], argv[1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-") and rest[i] != "--":
            if builtin == "command" and rest[i] in ("-v", "-V"):
                return None
            if builtin == "exec" and rest[i] == "-a":
                i += 1
            i += 1
        if i < len(rest) and rest[i] == "--":
            i += 1
        argv = _strip_wrappers(rest[i:])
    return argv


def _is_check_script(word: str) -> bool:
    """`scripts/check.sh` by any path (`./scripts/check.sh`, `/repo/scripts/check.sh`)."""
    parts = word.split("/")
    return parts[-1] == "check.sh" and "scripts" in parts[:-1]


def _shell_verdict(rest: list[str], cwd: str, depth: int) -> str | None:
    """`bash scripts/check.sh`, `sh -c '<script>'`: the script a shell runs is classified — the
    `-c` string as a nested command line, a script PATH only when it is the check pipeline."""
    if depth >= _MAX_LAUNCHER_NESTING:
        return None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:] and i + 1 < len(rest):
            return classify_whole_suite(rest[i + 1], cwd, depth + 1)
        if tok == "--":
            i += 1
            break
        if tok.startswith("-"):
            i += 1
            continue
        break
    if i < len(rest) and _is_check_script(rest[i]):
        return "scripts/check.sh (the repo's whole check pipeline)"
    return None


def _classify_argv(argv: list[str], cwd: str, depth: int = 0) -> str | None:
    """What whole-suite shape an already-peeled argv runs, or None. A launcher or an interpreter
    module run is peeled and what it EXECUTES is classified (`uv run python -m pytest` → `pytest`),
    re-peeling wrappers at each step; a launcher's `-c/--call` shell string is classified as a
    nested command line. Bounded by `_MAX_LAUNCHER_NESTING`, then allow (fail-open)."""
    stripped = _strip_shell_builtins(argv)
    if not stripped:
        return None
    argv = stripped
    exe = _basename(argv[0])
    rest = argv[1:]
    if exe in _PYTEST_EXES:
        return _pytest_verdict(rest, cwd)
    if exe == "mypy":
        return _mypy_verdict(rest, cwd)
    if _is_check_script(argv[0]):
        return "scripts/check.sh (the repo's whole check pipeline)"
    if exe in _SHELLS:
        return _shell_verdict(rest, cwd, depth)
    if exe in ("vitest", "jest"):
        return _js_runner_verdict(exe, rest, cwd)
    if exe == "bun" and _after_global_options(rest, _PM_GLOBAL_VALUE_FLAGS)[:1] == ["test"]:
        return _js_runner_verdict("bun test", _after_global_options(rest, _PM_GLOBAL_VALUE_FLAGS)[1:], cwd)
    if exe in _PACKAGE_MANAGERS:
        label = _package_script_verdict(exe, rest)
        if label:
            return label
    if exe == "cargo":
        return _cargo_verdict(rest)
    if exe == "go":
        return _go_verdict(rest)
    if exe in ("docker", "docker-compose"):
        return _docker_verdict(exe, rest)
    if depth >= _MAX_LAUNCHER_NESTING:
        return None
    found = _launcher_operands(argv)
    if found is None:
        inner = _strip_python_module(argv)
    else:
        spec, operands = found
        shell = _launcher_shell_command(operands, spec)
        if shell is not None:
            return classify_whole_suite(shell, cwd, depth + 1)
        inner = operands[_skip_launcher_flags(operands, spec):] or None
    if inner is None:
        return None
    return _classify_argv(_strip_wrappers(inner), cwd, depth + 1)


def classify_whole_suite(command: str, cwd: str, depth: int = 0) -> str | None:
    """A short label of the first whole-suite shape in `command`, or None. The command is
    shell-tokenized (quotes/heredocs honored) and split into segments; each segment is inspected
    by its REAL command after peeling shell noise, inline env, wrappers and launchers. A runner
    name inside a quoted argument to a different command never matches. An unparseable command
    (unbalanced quotes) returns None → fail-open, consistent with on_error=open."""
    tokens = _tokenize(command)
    if tokens is None:
        return None
    for segment in _segments(tokens):
        argv = _strip_wrappers(_split_inline_env(_strip_leading_shell_noise(segment)))
        label = _classify_argv(argv, cwd, depth)
        if label:
            return label
    return None


def block_message(command: str, label: str, opt_in: Path) -> str:
    offload = f"runpod-run -- bash -c {shlex.quote(command)}"
    what = (
        f"Blocked ({HOOK_ID}): this runs {label} on the workstation, and this repo's "
        f"{opt_in.name} says heavy work runs on Runpod ({opt_in}). A whole suite, a tree-wide "
        "mypy or a docker build next to the other agent sessions is exactly what thrashed this "
        "shared machine to a manual reset on 2026-10-10 and 2026-10-11."
    )
    self_serve = (
        "Run the same command on a pod instead — same checkout synced, output streamed back, "
        f"pod deleted after: {offload} "
        "(`runpod-run --dry-run -- …` prints the plan and price; `runpod-run --ttl 10 -- …` keeps "
        "the pod warm for the next run; skill runpod-offload.) Locally, ONE test file is fine "
        "(`pytest tests/unit/test_x.py`, `bun test tests/x.test.ts`, `mypy src/mod.py`)."
    )
    relay = (
        "If a subagent reported this block, run the command above via `runpod-run` yourself and "
        "send it the result (or tell it to run that exact command) with `write agent://<id>`, "
        "then `wait` for its final report."
    )
    human = (
        "request a one-time Telegram approval by setting "
        'RIG_HATCH_REQUEST_RUNPOD_OFFLOAD_GATE="<written justification>" '
        "(deny-by-default; a bare 1 or a single token like pytest is rejected)."
    )
    return _render_block(what, self_serve, relay, human)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0
    if not isinstance(event, dict):
        emit("allow")
        return 0

    _CURRENT_EVENT["event"] = event

    args = event.get("args")
    if not isinstance(args, dict):
        args = {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    cwd = str(event.get("cwd") or args.get("cwd") or os.getcwd())

    # Cheapest check first: most commands are not a suite at all, so the AGENTS.md walk (a few
    # stat calls) is only paid for the ones that are.
    label = classify_whole_suite(command, cwd)
    if label is None:
        emit("allow")
        return 0
    opt_in = repo_opt_in_file(cwd)
    if opt_in is None:
        emit("allow")
        return 0

    ctx = {"hook": HOOK_ID, "command": command, "cwd": cwd, "what": label}
    hatch = hatch_escalation.request_hatch_approval(HOOK_ID, ctx, cwd=cwd, command=command)
    if hatch.should_stop:
        if hatch.approved:
            warn(f"allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        # Hatch already ran: the agent needs the deny reason, not the usage blurb again.
        emit("block", f"hatch escalation denied: {hatch.reason}")
        return BLOCK_EXIT_CODE

    emit("block", block_message(command, label, opt_in))
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
