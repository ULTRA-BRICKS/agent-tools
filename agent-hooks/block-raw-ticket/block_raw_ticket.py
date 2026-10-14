#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block raw issue-tracker writes when task-cli is configured.

Accessed via: harness PreToolUse bash -> this script (rig apply installs the
descriptor from the agent-tools catalog when agent_hooks.all is true). The
installed descriptor points straight at this file in the primary agent-tools
checkout, so merging a change here IS activation — no separate install step.

Assumptions: task-cli is the enforced ticket interface. The gate arms only when
`task` on PATH is actually task-cli (the file contains `tasklib` — not
Taskwarrior/go-task). Once armed, these raw routes that skip task-cli's gates
are blocked (and only these — Linear's own GraphQL HTTP API, GitLab, and the
other residuals in README are NOT covered):

- `gh issue <write>` and `linear issue <write>` (the original coverage);
- Forgejo's `fj issue <write>` / `fj repo labels <write>` and Gitea's
  `tea issues <write>` / `tea comment` / `tea label <write>`;
- a raw REST write to a tracker endpoint (`[api/v1|api/v3/]repos/<o>/<r>/issues...`
  or `.../labels...`) through `curl` / `wget` / HTTPie / `gh api` — on ANY host,
  GitHub included (API parity with the `gh issue` rule);
- `gh api graphql` carrying an issue/label mutation (`createIssue`, ...);
- the same REST write from an inline Python/Node program (`python3 -c`,
  `node -e`, a here-string, or a `<<EOF` heredoc fed to the interpreter);
- any of the above wrapped in `sh -c` / `bash -c` / `eval` / a shell heredoc.

Read-only calls, `--help`, PR commands, and `task …` stay allowed. An explicit
GET/HEAD method always wins over an implied write (`curl -X GET -d …` and
`http GET url field=x` are reads).

A "Review uncommitted …" title is refused on ticket create even when task-cli
is absent: that shape must never become a GitHub/Linear/task ticket (ask 041).

Escape hatch: `RIG_HATCH_REQUEST_BLOCK_RAW_TICKET="<justification>"` requests
one Telegram approval (deny-by-default) through the shared
`agenttools_hatch_escalation` helper. One hatch round-trip per whole command;
no subagent exemption.

This is a pre-tool guard over the literal command text, not a sandbox: a
script file, a URL assembled at runtime, or an unrecognized interpreter is out
of reach — README lists the residuals explicitly.

Past bugs: require-ticket-before-commit explicitly allowed `gh issue create`
because it is not a git commit. shutil.which("task") alone matched a foreign
`task` binary (review on #636). main() early-allowed every command when
task-cli was absent, so `gh issue create --title "Review uncommitted …"` still
minted GitHub tickets. The first Forgejo cut matched the issues path anywhere
in the joined argv and parsed only detached `-X POST`, so `curl -XPOST`,
`curl -T`, `gh api --input` and an absolute `gh api` URL all slipped through
while `http GET url field=x` was over-blocked (UB-25 coordinator review).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import posixpath
import re
import shlex
import shutil
import sys
import urllib.parse
from collections.abc import Iterable
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
HOOK_ID = "block-raw-ticket"

_SHELL_SEPS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_LEADING_SHELL_NOISE = frozenset({"(", "{", "!", "then", "do", "else", "elif"})
_INLINE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer", "command",
    "sudo", "doas", "xargs",
})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "sudo": frozenset({
        "-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir", "-p", "--prompt",
        "-r", "--role", "-t", "--type", "-U", "--other-user",
    }),
    "doas": frozenset({"-u", "-C", "-a"}),
    "xargs": frozenset({
        "-I", "-d", "--delimiter", "-n", "--max-args", "-s", "--max-chars", "-E", "-L",
        "--max-lines", "-P", "--max-procs", "-a", "--arg-file", "--replace", "--eof",
        "--process-slot-var",
    }),
}
# Project runners whose `run` subcommand executes the rest of argv (`uv run python -c …`).
_RUNNERS = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch", "rye"})
_RUNNER_VALUE_FLAGS = frozenset({
    "--with", "-w", "--with-requirements", "--with-editable", "--python", "-p", "--project",
    "--directory", "--env-file", "--extra", "--group", "--only-group", "--package", "--index",
    "--index-url", "--config-file", "-C", "-e", "--env",
})
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
_GH_ISSUE_WRITES = frozenset({
    "create", "new", "close", "reopen", "edit", "comment", "delete",
    "transfer", "pin", "unpin", "lock", "unlock", "develop",
})
_LINEAR_ISSUE_WRITES = frozenset({
    "create", "new", "update", "edit", "delete", "archive", "close", "comment",
})
_CREATE_VERBS = frozenset({"create", "new"})
_RAW_HINT = re.compile(r"\b(?:gh\s+issue|linear\s+issue)\b", re.IGNORECASE)
_JUNK_UNCOMMITTED = re.compile(r"\breview\s+uncommitted\b", re.IGNORECASE)
_TASK_CLI_MARKERS = (b"tasklib", b"task-cli")
_INHERITED_VALUE_FLAGS = frozenset({
    "-R", "--repo", "--hostname", "-C", "--cwd", "--backend", "--config",
})
_MAX_DEPTH = 4

_BLOCK_MSG = (
    "[block-raw-ticket] BLOCKED: raw tracker write while task-cli is configured. "
    "Use `task new` / `task create` / `task change` / `task done` instead of "
    "`gh issue`, `linear issue`, `fj issue`, `tea issues`, or a raw REST/GraphQL "
    "issue/label write (curl, wget, HTTPie, gh api, inline Python/Node). task-cli "
    "enforces acceptance criteria, motivation, user-impact, and screenshots; a raw "
    "tracker command skips them. Reads (`gh issue list`/`view`, `fj issue view`, a "
    "GET on the same endpoint), `--help`, and PR commands are allowed. There is no "
    "self-service bypass; for a genuine exception request one Telegram approval via "
    'RIG_HATCH_REQUEST_BLOCK_RAW_TICKET="<why>" (deny-by-default).'
)

_JUNK_BLOCK_MSG = (
    "[block-raw-ticket] BLOCKED: a 'Review uncommitted' title must not create a "
    "ticket. That shape never leads to a merge — run `review diff` or post a PR "
    "comment instead of filing a GitHub/Linear/`task` ticket. There is no "
    "self-service bypass."
)


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


def _looks_like_task_cli(path: str) -> bool:
    """True iff `path` is task-cli, not Taskwarrior/go-task (both also named `task`)."""
    try:
        with open(os.path.realpath(path), "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return any(m in head for m in _TASK_CLI_MARKERS)


def task_cli_configured(cwd: str | None = None) -> bool:
    """Activated AND configured: a real task-cli binary is on PATH."""
    del cwd
    path = shutil.which("task")
    return bool(path) and _looks_like_task_cli(path)


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _strip_leading(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and (argv[i] in _LEADING_SHELL_NOISE or _INLINE_ENV.match(argv[i])):
        i += 1
    return argv[i:]


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    i = 0
    while i < len(argv):
        tok = argv[i]
        if wrapper == "env" and re.match(r"^-[iv0]+S", tok):
            return ["<unresolved-env-split>"]
        if wrapper == "env" and (tok in {"-S", "--split-string"} or tok.startswith("--split-string=")
                                 or (tok.startswith("-S") and len(tok) > 2)):
            if tok in {"-S", "--split-string"}:
                if i + 1 >= len(argv):
                    return ["<unresolved-env-split>"]
                split, rest = argv[i + 1], argv[i + 2:]
            else:
                split = tok.split("=", 1)[1] if tok.startswith("--") else tok[2:]
                rest = argv[i + 1:]
            # GNU env expands variables and has its own backslash escapes; those cannot be
            # classified by shell tokenization without knowing the execution environment.
            if "$" in split or "\\" in split:
                return ["<unresolved-env-split>"]
            try:
                return _strip_leading(shlex.split(split) + rest)
            except ValueError:
                return ["<unresolved-env-split>"]
        if tok == "--":
            return argv[i + 1:]
        if _INLINE_ENV.match(tok):
            i += 1
            continue
        if tok in value_flags:
            i += 2
            continue
        if any(tok.startswith(f"{flag}=") for flag in value_flags):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    rest = argv[i:]
    if wrapper == "timeout" and rest and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", rest[0]):
        rest = rest[1:]
    return rest


def _skip_runner_to_command(args: list[str]) -> list[str] | None:
    """`uv [flags] run [flags] CMD …` → `CMD …`; None when the runner is not running a command
    (`uv sync`, `poetry install`), so the caller leaves the argv alone."""
    i = 0
    seen_run = False
    while i < len(args):
        tok = args[i]
        if tok in _RUNNER_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if seen_run:
            return args[i:]
        if tok != "run":
            return None
        seen_run = True
        i += 1
    return None


def _strip_wrappers(argv: list[str]) -> list[str]:
    guard = 0
    while argv and guard < 16:
        guard += 1
        name = _basename(argv[0])
        if name in _WRAPPERS:
            argv = _skip_wrapper_args(name, argv[1:])
        elif name in _RUNNERS:
            inner = _skip_runner_to_command(argv[1:])
            if inner is None:
                break
            argv = inner
        else:
            break
    return argv


def _skip_gh_flags(argv: list[str]) -> list[str]:
    """Skip gh inherited flags (`-R`/`--repo`/`--hostname`) and other dash options."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GH_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("--repo=") or tok.startswith("--hostname=") or tok.startswith("-R="):
            i += 1
            continue
        if tok.startswith("-") and tok not in {"-h", "--help"}:
            i += 1
            continue
        break
    return argv[i:]


def _segment_is_raw_write(segment: list[str]) -> bool:
    argv = _strip_wrappers(_strip_leading(segment))
    if not argv:
        return False
    head = _basename(argv[0])
    rest = argv[1:]
    if head == "gh":
        rest = _skip_gh_flags(rest)
        if not rest or rest[0] != "issue":
            return False
        sub = _skip_gh_flags(rest[1:])
        return bool(sub) and sub[0] in _GH_ISSUE_WRITES
    if head == "linear":
        rest = _skip_gh_flags(rest)  # linear also takes dash flags before the verb
        if not rest or rest[0] != "issue":
            return False
        sub = _skip_gh_flags(rest[1:])
        return bool(sub) and sub[0] in _LINEAR_ISSUE_WRITES
    return False


# ── shared argv parsing for the API/CLI detectors (UB-25) ────────────────────────────────────

_HELP_FLAGS = frozenset({"-h", "--help"})


def _split_args(argv: list[str], value_flags: frozenset[str]) -> tuple[list[str], list[str]]:
    """Positionals and boolean flags of `argv`, skipping every recognized value flag's operand
    (detached `-x VALUE` or glued `--long=VALUE`). Used for the subcommand CLIs (`fj`, `tea`),
    where a mis-skipped operand would otherwise be read as the verb — or a `--body -h` value
    would be read as a request for help."""
    positionals: list[str] = []
    flags: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            positionals.extend(argv[i + 1:])
            break
        if tok in value_flags:
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1:
            if not (tok.startswith("--") and "=" in tok):
                flags.append(tok)
            i += 1
            continue
        positionals.append(tok)
        i += 1
    return positionals, flags


def _expand_short_cluster(
    body: str, value_chars: frozenset[str], nxt: str | None
) -> tuple[list[tuple[str, str | None]], int]:
    """getopt-style short-option group: `-sXPOST` == `-s -X POST`, `-dJSON` == `-d JSON`,
    `-sX POST` == `-s -X POST`. The first value-taking char ends the group and takes the rest
    of the token (or the next token) as its value. Returns (options, tokens consumed)."""
    opts: list[tuple[str, str | None]] = []
    for j, ch in enumerate(body):
        if ch not in value_chars:
            opts.append((f"-{ch}", None))
            continue
        glued = body[j + 1:]
        if glued:
            opts.append((f"-{ch}", glued))
            return opts, 1
        opts.append((f"-{ch}", nxt))
        return opts, 2 if nxt is not None else 1
    return opts, 1


def _parse_options(
    argv: list[str], short_value: frozenset[str], long_value: frozenset[str]
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Generic option parser for the HTTP clients: returns ([(option, value|None)], positionals).
    Unknown long options are treated as boolean — a misclassification there can only add a
    positional (a URL candidate), never hide a method flag."""
    opts: list[tuple[str, str | None]] = []
    positionals: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            positionals.extend(argv[i + 1:])
            break
        if tok.startswith("--"):
            name, eq, val = tok.partition("=")
            if eq:
                opts.append((name, val))
                i += 1
            elif name in long_value and i + 1 < len(argv):
                opts.append((name, argv[i + 1]))
                i += 2
            else:
                opts.append((name, None))
                i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            cluster, used = _expand_short_cluster(tok[1:], short_value, nxt)
            opts.extend(cluster)
            i += used
            continue
        positionals.append(tok)
        i += 1
    return opts, positionals


# ── tracker REST endpoint shape ──────────────────────────────────────────────────────────────

# `[api/v1/|api/v3/]repos/<owner>/<repo>/(issues|labels)[/...]` — Forgejo/Gitea (`api/v1`),
# GitHub Enterprise (`api/v3`), github.com / `gh api` relative endpoints (no prefix), and a
# reverse proxy that strips the prefix. Host-independent on purpose: the shared requirement is
# API parity, so a GitHub REST issue write is blocked exactly like a Forgejo one. Pull-request
# endpoints (`/pulls`) never match.
_TRACKER_API_PATH = re.compile(
    r"(?<![A-Za-z0-9_.~-])(?:api/v[13]/)?repos/[^/\s'\"?#]+/[^/\s'\"?#]+/(?:issues|labels)"
    r"(?=$|[/?#\s'\"])",
    re.IGNORECASE,
)
_HTTP_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_KNOWN_HTTP_METHODS = frozenset({
    "GET", "HEAD", "OPTIONS", "TRACE", "CONNECT", "POST", "PATCH", "PUT", "DELETE",
})
# An explicit method this hook cannot resolve to a known verb (`-X$'POST'`, `-X "$M"`). It must
# not disable the implied-write check, so it counts as a write (fail closed).
_UNRESOLVED_METHOD = "<unresolved>"


def _explicit_method(value: str) -> str:
    upper = value.upper()
    return upper if upper in _KNOWN_HTTP_METHODS else _UNRESOLVED_METHOD


def _is_write_method(method: str) -> bool:
    return method in _HTTP_WRITE_METHODS or method == _UNRESOLVED_METHOD


_URL_PREFIX = re.compile(r"^([a-z][a-z0-9+.-]*://[^/?#\s]*)?(.*)$", re.IGNORECASE | re.DOTALL)


def _normalize_url_path(text: str) -> str:
    """Collapse `.`, `..` and `//` in the path the way curl/wget/HTTPie do before sending, so
    `repos/o/r/x/../issues` is judged as `repos/o/r/issues`."""
    match = _URL_PREFIX.match(text)
    if match is None:
        return text
    prefix, rest = match.group(1) or "", match.group(2)
    cut = re.search(r"[?#]", rest)
    path, tail = (rest[:cut.start()], rest[cut.start():]) if cut else (rest, "")
    if not path:
        return text
    normalized = posixpath.normpath(re.sub(r"/{2,}", "/", path))
    return f"{prefix}{normalized}{tail}"


def is_tracker_api_url(text: str) -> bool:
    """True iff `text` carries the tracker REST path, as-is or after up to three rounds of
    URL-decoding (a `r%2Fissues` / `r%252Fissues` segment is still the issues endpoint), each
    also checked with its path normalized (`./`, `x/../`, `//`)."""
    candidate = text
    for _ in range(4):
        if _TRACKER_API_PATH.search(candidate) or _TRACKER_API_PATH.search(_normalize_url_path(candidate)):
            return True
        try:
            decoded = urllib.parse.unquote(candidate)
        except (ValueError, UnicodeDecodeError):
            return False
        if decoded == candidate:
            return False
        candidate = decoded
    return False


# ── curl ─────────────────────────────────────────────────────────────────────────────────────

_CURL_SHORT_VALUE = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
_CURL_LONG_VALUE = frozenset({
    "--request", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii",
    "--json", "--form", "--form-string", "--upload-file", "--url", "--header", "--user",
    "--output", "--user-agent", "--referer", "--cookie", "--cookie-jar", "--config",
    "--max-time", "--connect-timeout", "--proxy", "--write-out", "--range", "--cert", "--key",
    "--cacert", "--resolve", "--retry", "--limit-rate", "--oauth2-bearer", "--variable",
    "--expand-data", "--expand-url", "--dump-header", "--proxy-user", "--interface",
})
_CURL_KIND = {
    "-X": "method", "--request": "method",
    "-T": "upload", "--upload-file": "upload",
    "-G": "get", "--get": "get",
    "-I": "head", "--head": "head",
    "-d": "data", "-F": "data", "--data": "data", "--data-raw": "data",
    "--data-binary": "data", "--data-urlencode": "data", "--data-ascii": "data",
    "--json": "data", "--form": "data", "--form-string": "data", "--expand-data": "data",
}


def _curl_request(rest: list[str]) -> tuple[str, list[str]]:
    """curl's effective method and URL candidates. An explicit `-X`/`--request` (the last one)
    always wins; otherwise `-T`/`--upload-file` is PUT, a data/form flag is POST unless
    `-G`/`--get` moves the data into the query string, and only a body-less `-I` is HEAD."""
    opts, urls = _parse_options(rest, _CURL_SHORT_VALUE, _CURL_LONG_VALUE)
    explicit: str | None = None
    kinds: set[str] = set()
    for name, val in opts:
        kind = _CURL_KIND.get(name)
        if kind == "method" and val:
            explicit = _explicit_method(val)
        elif kind:
            kinds.add(kind)
        if name in ("--url", "--expand-url") and val:
            urls.append(val)
    if explicit:
        return explicit, urls
    # curl resolves conflicting method-setting flags by argv order; rather than model that, a
    # body-carrying flag always outranks `-I` (fail closed: `curl -I -d …` is a write).
    if "upload" in kinds:
        return "PUT", urls
    if "data" in kinds and "get" not in kinds:
        return "POST", urls
    if "head" in kinds:
        return "HEAD", urls
    return "GET", urls


# ── wget ─────────────────────────────────────────────────────────────────────────────────────

_WGET_SHORT_VALUE = frozenset("OoaeitTwQPUlARDIXB")
_WGET_LONG_VALUE = frozenset({
    "--method", "--body-data", "--body-file", "--post-data", "--post-file", "--header",
    "--output-document", "--output-file", "--user", "--password", "--http-user",
    "--http-password", "--user-agent", "--referer", "--timeout", "--tries", "--input-file",
})


def _wget_request(rest: list[str]) -> tuple[str, list[str]]:
    """Explicit `--method` wins; `--post-data`/`--post-file` are POST; a `--body-data`/
    `--body-file` without an explicit method is treated as POST (wget itself requires a method
    there — conservative reading)."""
    opts, urls = _parse_options(rest, _WGET_SHORT_VALUE, _WGET_LONG_VALUE)
    explicit: str | None = None
    has_body = False
    for name, val in opts:
        if name == "--method" and val:
            explicit = _explicit_method(val)
        elif name in ("--post-data", "--post-file", "--body-data", "--body-file"):
            has_body = True
    if explicit:
        return explicit, urls
    return ("POST" if has_body else "GET"), urls


# ── HTTPie (http / https / xh) ───────────────────────────────────────────────────────────────

_HTTPIE_HEADS = frozenset({"http", "https", "httpie", "xh", "xhs"})
_HTTPIE_SHORT_VALUE = frozenset("aAops")
_HTTPIE_LONG_VALUE = frozenset({
    "--auth", "--auth-type", "--session", "--session-read-only", "--output", "--verify",
    "--cert", "--cert-key", "--proxy", "--timeout", "--max-redirects", "--print", "--pretty",
    "--style", "--boundary", "--format-options", "--response-charset", "--response-mime",
    "--ssl", "--ciphers", "--default-scheme", "--raw",
})
_HTTPIE_METHOD_WORD = re.compile(r"^[A-Za-z]+$")
_HTTPIE_KNOWN_METHODS = frozenset({
    "GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT",
})
# Request-item separators, longest first at each position (HTTPie's own precedence).
_HTTPIE_SEPS = (":=@", "=@", "==", ":=", "=", "@", ":")
_HTTPIE_DATA_SEPS = frozenset({":=@", "=@", ":=", "=", "@"})


def _httpie_item_is_data(item: str) -> bool:
    """`field=value`, `field:=json`, `field@file` (form upload), `field=@file` are body data;
    `name==value` (query) and `Header:value` are not."""
    for idx in range(len(item)):
        for sep in _HTTPIE_SEPS:
            if item.startswith(sep, idx):
                return sep in _HTTPIE_DATA_SEPS
    return False


def _httpie_request(rest: list[str]) -> tuple[str, list[str]]:
    """`http [flags] [METHOD] URL [ITEM...]`. An explicit METHOD always wins; otherwise any body
    item or `--raw` implies POST (HTTPie's own default), else GET — `--form`, `--download`,
    `--ignore-stdin` alone do not make a request a write."""
    opts, pos = _parse_options(rest, _HTTPIE_SHORT_VALUE, _HTTPIE_LONG_VALUE)
    method: str | None = None
    if len(pos) >= 2 and _HTTPIE_METHOD_WORD.match(pos[0]) and pos[0].upper() in _HTTPIE_KNOWN_METHODS:
        method = pos[0].upper()
        pos = pos[1:]
    elif len(pos) >= 2 and ("$" in pos[0] or "`" in pos[0]) and "/" not in pos[0]:
        method = _UNRESOLVED_METHOD  # `http $'POST' url …` — a method word the hook can't resolve
        pos = pos[1:]
    urls, items = pos[:1], pos[1:]
    if method:
        return method, urls
    if any(name == "--raw" for name, _ in opts) or any(_httpie_item_is_data(i) for i in items):
        return "POST", urls
    return "GET", urls


# ── gh api ───────────────────────────────────────────────────────────────────────────────────

_GH_API_SHORT_VALUE = frozenset("fFHqXpt")
_GH_API_LONG_VALUE = frozenset({
    "--field", "--raw-field", "--header", "--jq", "--method", "--input", "--hostname",
    "--preview", "--template", "--cache",
})
_GH_API_FIELD_OPTS = frozenset({"-f", "-F", "--field", "--raw-field"})
# Issue/label mutations only. `addComment`/`addLabelsToLabelable` also target pull requests
# and cannot be told apart from the query text — a documented residual, not a silent gap.
_GRAPHQL_TRACKER_MUTATION = re.compile(
    r"\b(?:createIssue|updateIssue|closeIssue|reopenIssue|deleteIssue|transferIssue|"
    r"pinIssue|unpinIssue|createLabel|updateLabel|deleteLabel)\b"
)


def _gh_api_request(args: list[str]) -> tuple[str, list[str], list[tuple[str, str | None]]]:
    """gh api's effective method: an explicit `-X`/`--method` wins; else `--input` or any
    `-f`/`-F` field switches the default to POST (gh's documented rule); else GET. The endpoint
    (first positional) may be relative (`repos/o/r/issues`) or an absolute URL on any host."""
    opts, pos = _parse_options(args, _GH_API_SHORT_VALUE, _GH_API_LONG_VALUE)
    explicit: str | None = None
    implied_post = False
    for name, val in opts:
        if name in ("-X", "--method") and val:
            explicit = _explicit_method(val)
        elif name == "--input" or name in _GH_API_FIELD_OPTS:
            implied_post = True
    method = explicit or ("POST" if implied_post else "GET")
    return method, pos[:1], opts


def _gh_api_is_write(argv: list[str]) -> bool:
    rest = _skip_gh_flags(argv[1:])
    if not rest or rest[0] != "api":
        return False
    method, endpoints, opts = _gh_api_request(rest[1:])
    if not endpoints:
        return False
    endpoint = endpoints[0]
    if endpoint == "graphql" or endpoint.rstrip("/").endswith("/graphql"):
        return any(
            name in _GH_API_FIELD_OPTS and val is not None
            and val.startswith("query=") and bool(_GRAPHQL_TRACKER_MUTATION.search(val))
            for name, val in opts
        )
    return _is_write_method(method) and is_tracker_api_url(endpoint)


_HTTP_CLIENT_PARSERS = {"curl": _curl_request, "wget": _wget_request}
_HTTP_CLIENT_PARSERS.update(dict.fromkeys(_HTTPIE_HEADS, _httpie_request))


def _curl_request_groups(rest: list[str]) -> list[list[str]]:
    """curl's `--next` / `-:` start an independent request with its own method and data, so
    each group is judged on its own — a `-X GET` on request 1 must not cover a write in 2."""
    groups: list[list[str]] = [[]]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            groups[-1].extend(rest[i:])
            break
        if tok in ("--next", "-:"):
            groups.append([])
            i += 1
            continue
        used = 1
        if tok in _CURL_LONG_VALUE and i + 1 < len(rest):
            used = 2
        elif tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            nxt = rest[i + 1] if i + 1 < len(rest) else None
            cluster, used = _expand_short_cluster(tok[1:], _CURL_SHORT_VALUE, nxt)
            if ("-:", None) in cluster:
                for name, value in cluster:
                    if name == "-:" and value is None:
                        groups.append([])
                    else:
                        groups[-1].append(name)
                        if value is not None:
                            groups[-1].append(value)
                i += used
                continue
        groups[-1].extend(rest[i:i + used])
        i += used
    return groups


def _http_client_is_write(head: str, rest: list[str]) -> bool:
    parse = _HTTP_CLIENT_PARSERS.get(head)
    if parse is None:
        return False
    groups = _curl_request_groups(rest) if head == "curl" else [rest]
    for group in groups:
        method, urls = parse(group)
        if _is_write_method(method) and any(is_tracker_api_url(u) for u in urls):
            return True
    return False


# ── fj (Forgejo CLI) / tea (Gitea CLI) ───────────────────────────────────────────────────────

# Verified live (2026-09-22) against `fj --help`, the full `fj issue --help` verb list (create,
# edit, comment, assign, unassign, close, search, view, templates, browse — there is no
# reopen/delete/transfer today), `fj issue <verb> --help` for each write verb, `fj repo --help`
# and `fj repo labels --help` (view, create, delete, edit). All value flags across those
# subcommands are folded into one skip-set: over-skipping at a position where fj would not
# accept the flag is harmless for classification; under-skipping would read a value as a verb.
_FJ_VALUE_FLAGS = frozenset({
    "-H", "--host", "-C", "--cwd", "-R", "--remote", "-r", "--repo",
    "--body", "--body-file", "--template", "-w", "--with-msg", "--style",
})
# The verified write verbs plus gh-style verbs fj does not have yet, so a future
# `fj issue reopen|delete|…` is blocked the day it ships rather than silently allowed.
_FJ_ISSUE_WRITE_VERBS = frozenset({
    "create", "edit", "comment", "assign", "unassign", "close",
    "reopen", "delete", "transfer", "lock", "unlock", "pin", "unpin",
})
_FJ_LABEL_WRITE_VERBS = frozenset({"create", "delete", "edit"})

# `tea` is NOT installed on the machine this was written on: this shape comes from Gitea's
# published tea docs (gitea.com/gitea/tea), unverified locally — see README residuals.
_TEA_ISSUE_HEADS = frozenset({"issue", "issues", "i"})
_TEA_ISSUE_WRITE_VERBS = frozenset({"create", "close", "reopen", "edit"})
_TEA_LABEL_WRITE_VERBS = frozenset({"create", "delete", "update"})
_TEA_VALUE_FLAGS = frozenset({
    "-r", "--repo", "-l", "--login", "--remote", "-t", "--title", "-d", "--description",
    "--output", "-o",
})


def _fj_is_write(rest: list[str]) -> bool:
    pos, flags = _split_args(rest, _FJ_VALUE_FLAGS)
    if _HELP_FLAGS.intersection(flags) or not pos:
        return False
    if pos[0] == "issue":
        return len(pos) > 1 and pos[1] in _FJ_ISSUE_WRITE_VERBS
    if pos[0] == "repo" and len(pos) > 1 and pos[1] == "labels":
        # `fj repo labels [REPO] <verb>` — the optional REPO shifts the verb by one.
        return any(p in _FJ_LABEL_WRITE_VERBS for p in pos[2:4])
    return False


def _tea_is_write(rest: list[str]) -> bool:
    pos, flags = _split_args(rest, _TEA_VALUE_FLAGS)
    if _HELP_FLAGS.intersection(flags) or not pos:
        return False
    head = pos[0]
    if head == "comment":
        return True
    if head in _TEA_ISSUE_HEADS:
        return len(pos) > 1 and pos[1] in _TEA_ISSUE_WRITE_VERBS
    if head in ("label", "labels"):
        return len(pos) > 1 and pos[1] in _TEA_LABEL_WRITE_VERBS
    return False


# ── inline Python / Node programs ────────────────────────────────────────────────────────────

_PY_HEAD = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)*)?$")  # python, python3.11, pypy3
_JS_HEADS = frozenset({"node", "nodejs", "bun"})
_SHELL_HEADS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_PY_CODE_OPT = re.compile(r"^-[A-Za-z]*c$")
_JS_CODE_OPT = re.compile(r"^-[A-Za-z]*[ep]$")
_SHELL_CODE_OPT = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")

_STRING_LITERAL = re.compile(
    r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
    r"|`(?:\\.|[^`\\])*`"
)
_LITERAL_REF = r'"S(\d+)"'
_NAMED_WRITE_CALL = re.compile(
    r"(?:\b(?:requests|httpx|axios|session|client|s|ky|got|superagent|http_client)|\))"
    r"\s*\.\s*(?:post|patch|put|delete)\s*\(",
    re.IGNORECASE,
)
_METHOD_KWARG = re.compile(r"\bmethod\s*[:=]\s*" + _LITERAL_REF, re.IGNORECASE)
_METHOD_KEY = re.compile(_LITERAL_REF + r"\s*:\s*" + _LITERAL_REF)
_REQUEST_CALL_METHOD = re.compile(r"\brequest\s*\(\s*" + _LITERAL_REF, re.IGNORECASE)
_URLLIB_CALL = re.compile(r"\b(?:Request|urlopen)\s*\(")
_KWARG_START = re.compile(r"^\s*\*{0,2}[A-Za-z_]\w*\s*=(?!=)")


def _split_code_and_literals(script: str) -> tuple[str, list[str]]:
    """Replace every string literal with a `"S<n>"` placeholder so call/method detection runs on
    CODE only — `print("requests.post(...)")` / `console.log("method: 'POST'")` are inert text,
    not calls. The literal values are kept for the URL and method-name checks."""
    literals: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        text = match.group(0)
        q = 3 if text[:3] in ('"""', "'''") else 1
        literals.append(text[q:-q])
        return f'"S{len(literals) - 1}"'

    return _STRING_LITERAL.sub(_sub, script), literals


def _call_args(code: str, open_paren: int) -> str:
    depth = 0
    for k in range(open_paren, len(code)):
        if code[k] in "([{":
            depth += 1
        elif code[k] in ")]}":
            depth -= 1
            if depth == 0:
                return code[open_paren + 1:k]
    return code[open_paren + 1:]


def _top_level_args(args: str) -> list[str]:
    parts, depth, buf = [], 0, []
    for ch in args:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def _urllib_call_is_write(code: str, literals: list[str]) -> bool:
    """`urllib.request.Request(url, data)` / `urlopen(url, data)` default to POST when a body is
    given and no method is; an explicit `method=` decides otherwise."""
    for match in _URLLIB_CALL.finditer(code):
        args = _call_args(code, match.end() - 1)
        explicit = _METHOD_KWARG.search(args)
        if explicit:
            if literals[int(explicit.group(1))].upper() in _HTTP_WRITE_METHODS:
                return True
            continue
        parts = _top_level_args(args)
        positional = [p for p in parts if not _KWARG_START.match(p)]
        has_data_kw = any(re.match(r"^\s*data\s*=\s*(?!None\b)", p) for p in parts)
        if has_data_kw or len(positional) >= 2:
            return True
    return False


def _code_names_write(code: str, literals: list[str]) -> bool:
    if _NAMED_WRITE_CALL.search(code):
        return True
    for rx in (_METHOD_KWARG, _REQUEST_CALL_METHOD):
        for m in rx.finditer(code):
            if literals[int(m.group(1))].upper() in _HTTP_WRITE_METHODS:
                return True
    for m in _METHOD_KEY.finditer(code):
        key, val = literals[int(m.group(1))], literals[int(m.group(2))]
        if key.lower() == "method" and val.upper() in _HTTP_WRITE_METHODS:
            return True
    return _urllib_call_is_write(code, literals)


def script_is_tracker_api_write(script: str) -> bool:
    """An inline program that (a) mentions a tracker REST path in a string and (b) makes a
    write-method call in CODE (not inside a string)."""
    code, literals = _split_code_and_literals(script)
    if not (any(is_tracker_api_url(lit) for lit in literals) or is_tracker_api_url(code)):
        return False
    return _code_names_write(code, literals)


def _script_after(argv: list[str], code_opt: re.Pattern[str], long_opts: frozenset[str]) -> str | None:
    """The program text passed via `-c`/`-e` (incl. clustered `-Ic`, `-lc`, `-pe`), a long
    `--eval[=...]`, or a here-string `<<< text`."""
    for i, tok in enumerate(argv):
        if (code_opt.match(tok) or tok in long_opts or tok == "<<<") and i + 1 < len(argv):
            return argv[i + 1]
        for opt in long_opts:
            if tok.startswith(f"{opt}="):
                return tok.split("=", 1)[1]
    return None


def _node_scripts(argv: list[str]) -> list[str] | None:
    scripts: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        if _JS_CODE_OPT.match(tok) or tok in {"--eval", "--print"}:
            if i + 1 >= len(argv):
                return None
            scripts.append(argv[i + 1])
            i += 2
            continue
        if tok.startswith(("--eval=", "--print=")):
            scripts.append(tok.split("=", 1)[1])
        i += 1
    return scripts


def _segment_is_api_write(segment: list[str], depth: int) -> bool | None:
    argv = _strip_wrappers(_strip_leading(segment))
    if not argv:
        return False
    head = _basename(argv[0])
    rest = argv[1:]
    if head == "<unresolved-env-split>":
        return None
    if head == "fj":
        return _fj_is_write(rest)
    if head == "tea":
        return _tea_is_write(rest)
    if head in _HTTP_CLIENT_PARSERS:
        return _http_client_is_write(head, rest)
    if head == "gh":
        return _gh_api_is_write(argv)
    if _PY_HEAD.match(head):
        script = _script_after(rest, _PY_CODE_OPT, frozenset())
        return bool(script) and script_is_tracker_api_write(script)
    if head in _JS_HEADS:
        scripts = _node_scripts(rest)
        return None if scripts is None else any(script_is_tracker_api_write(script) for script in scripts)
    if head in _SHELL_HEADS or head == "eval":
        script = " ".join(rest) if head == "eval" else _script_after(rest, _SHELL_CODE_OPT, frozenset({"--command"}))
        # Tri-state on purpose: an unclassifiable inner script stays None (hard block, never
        # hatchable) instead of being flattened into a hatchable True.
        return _is_raw_tracker_write(script, depth + 1) if script else False
    return False


# ── heredocs fed to an interpreter ───────────────────────────────────────────────────────────


def _interpreter_kind(argv: list[str]) -> str | None:
    """Which interpreter READS STDIN as its program. `python3 -c …` / `node -e …` take their
    program from argv, so a heredoc there is data, not code."""
    if not argv:
        return None
    head = _basename(argv[0])
    rest = argv[1:]
    if _PY_HEAD.match(head) and _script_after(rest, _PY_CODE_OPT, frozenset()) is None:
        return "script"
    if head in _JS_HEADS and _script_after(rest, _JS_CODE_OPT, frozenset({"--eval", "--print"})) is None:
        return "script"
    if head in _SHELL_HEADS and _script_after(rest, _SHELL_CODE_OPT, frozenset({"--command"})) is None:
        return "shell"
    return None


# `$(( … ))` / `(( … ))` arithmetic (one nesting level of parens inside). A `<<` there is a
# left shift, not a heredoc opener; treating it as one would swallow every following line.
_ARITHMETIC = re.compile(r"\$?\(\((?:[^()]|\([^()]*\))*\)\)")


def _heredoc_openers(line: str) -> list[tuple[str | None, bool, str]]:
    """Every `<<DELIM` / `<<-DELIM` on one shell line, tagged with the interpreter kind of the
    command it feeds (None = plain data heredoc such as `cat <<EOF`)."""
    tokens = _tokenize_line(_ARITHMETIC.sub(" 0 ", line))
    if not tokens:
        return []
    found: list[tuple[str | None, bool, str]] = []
    for seg in _segments(tokens):
        kind = _interpreter_kind(_strip_wrappers(_strip_leading(seg)))
        for j, tok in enumerate(seg):
            if tok == "<<" and j + 1 < len(seg):
                word = seg[j + 1]
                dash = word.startswith("-")
                found.append((kind, dash, word[1:] if dash else word))
    return found


def _split_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Pull interpreter-fed heredoc bodies out of the shell text so they are analysed as
    programs (and not mis-tokenized as shell lines). A data heredoc's body is left in place,
    preserving the original line-by-line scan for it."""
    if "<<" not in command:
        return command, []  # hot path: no heredoc, no second per-line shlex pass
    lines = command.split("\n")
    kept: list[str] = []
    programs: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for kind, dash, delim in _heredoc_openers(line):
            body: list[str] = []
            while i < len(lines):
                cur = lines[i].rstrip("\r")  # CRLF input: `EOF\r` still closes the heredoc
                i += 1
                if (cur.lstrip("\t") if dash else cur) == delim:
                    break
                body.append(cur)
            if kind is None:
                kept.extend(body)
            else:
                programs.append((kind, "\n".join(body)))
    return "\n".join(kept), programs


# ── tokenizing + top-level predicate ─────────────────────────────────────────────────────────

# Plausibility check for the unparseable (fail-closed) path. The GraphQL alternative is derived
# from `_GRAPHQL_TRACKER_MUTATION` so the two lists can never drift apart.
_TRACKER_HINT = re.compile(
    r"\bfj\s+(?:issue|repo\s+labels)\b|\btea\s+(?:issues?|i|comment|labels?)\b"
    r"|repos/[^/\s]+/[^/\s]+/(?:issues|labels)\b"
    r"|" + _GRAPHQL_TRACKER_MUTATION.pattern,
    re.IGNORECASE,
)


def _tokenize_line(line: str) -> list[str] | None:
    """One shell line. wordchars EXTEND block-raw-pr-merge's `_split_segments` set
    (`@:,{}%+$`) with `/.=~`, so a URL (`https://host/api/v1/repos/o/r/issues`) or JSON
    (`{"a":1}`) stays one token — the tracker-path match needs the whole URL in one token."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.wordchars += "@:,{}%+$/.=~"
        return list(lex)
    except ValueError:
        return None


def _tokenize(command: str) -> list[str] | None:
    """punctuation_chars so `true;gh` / `true&&gh` split; newlines are command separators."""
    out: list[str] = []
    for i, line in enumerate(command.splitlines()):
        if not line.strip():
            continue
        toks = _tokenize_line(line)
        if toks is None:
            return None
        if i and out and toks:
            out.append(";")
        out.extend(toks)
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SHELL_SEPS:
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def _program_is_write(kind: str, body: str, depth: int) -> bool | None:
    if kind == "script":
        return script_is_tracker_api_write(body)
    return _is_raw_tracker_write(body, depth + 1)


def _combine(results: Iterable[bool | None]) -> bool | None:
    """Tri-state OR where unclassifiable dominates: any None -> None (hard block, never
    hatchable), else any True -> True (blocked, hatchable), else False (allowed). A single
    unclassifiable part makes the whole command unclassifiable, so an approval can never cover
    text the hook could not analyse — even when another part is a clear write."""
    seen_write = False
    for result in results:
        if result is None:
            return None
        seen_write = seen_write or result
    return seen_write


def _is_raw_tracker_write(command: str, depth: int) -> bool | None:
    if depth > _MAX_DEPTH:
        return None  # wrapper nesting this deep cannot be classified — hard block
    shell_text, programs = _split_heredocs(command)
    tokens = _tokenize(shell_text)
    if tokens is None:
        plausible = _RAW_HINT.search(shell_text) or _TRACKER_HINT.search(shell_text)
        shell_result: bool | None = None if plausible else False
        return _combine([*(_program_is_write(k, b, depth) for k, b in programs), shell_result])
    program_results = (_program_is_write(kind, body, depth) for kind, body in programs)
    segment_results = (
        True if _segment_is_raw_write(seg) else _segment_is_api_write(seg, depth)
        for seg in _segments(tokens)
    )
    return _combine(itertools.chain(program_results, segment_results))


def is_raw_tracker_write(command: str) -> bool | None:
    """True if command is a raw tracker write; None if unparseable-and-plausible."""
    return _is_raw_tracker_write(command, 0)


def _extract_title_flag(argv: list[str]) -> str | None:
    """`--title VALUE`, `-t VALUE`, or `--title=VALUE` from a tokenized argv."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-t", "--title"):
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("--title="):
            return tok.split("=", 1)[1]
        if tok.startswith("-t=") and len(tok) > 3:
            return tok[3:]
        if tok.startswith("-t") and tok != "-t" and not tok.startswith("--"):
            return tok[2:]
        i += 1
    return None


def _nonflag_words(argv: list[str]) -> list[str]:
    """Non-flag tokens after argv[0], skipping inherited value-flag operands."""
    words: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in _INHERITED_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-") and tok not in {"-", "--"}:
            i += 1
            continue
        words.append(tok)
        i += 1
    return words


def _fj_create_title(rest: list[str]) -> str | None:
    """`fj issue create <TITLE>` — fj takes the title as a positional, not a `--title` flag."""
    pos, _ = _split_args(rest, _FJ_VALUE_FLAGS)
    if len(pos) >= 3 and pos[0] == "issue" and pos[1] == "create":
        return pos[2]
    return None


def _segment_is_junk_ticket_create(segment: list[str]) -> bool:
    argv = _strip_wrappers(_strip_leading(segment))
    if not argv:
        return False
    head = _basename(argv[0])
    if head == "fj":
        fj_title = _fj_create_title(argv[1:])
        return bool(fj_title) and bool(_JUNK_UNCOMMITTED.search(fj_title))
    title = _extract_title_flag(argv)
    if not title or not _JUNK_UNCOMMITTED.search(title):
        return False
    words = _nonflag_words(argv)
    if head == "gh":
        return len(words) >= 2 and words[0] == "issue" and words[1] in _CREATE_VERBS
    if head == "linear":
        return len(words) >= 2 and words[0] == "issue" and words[1] in _CREATE_VERBS
    if head == "task":
        return bool(words) and words[0] in _CREATE_VERBS
    if head == "tea":
        pos, _ = _split_args(argv[1:], _TEA_VALUE_FLAGS)
        return len(pos) >= 2 and pos[0] in _TEA_ISSUE_HEADS and pos[1] in _CREATE_VERBS
    return False


_JUNK_CREATE_HINT = re.compile(
    r"\b(?:gh\s+issue|linear\s+issue|fj\s+issue|tea\s+(?:issues?|i)|task\s+(?:new|create))\b",
    re.IGNORECASE,
)


def is_junk_review_ticket_create(command: str) -> bool | None:
    """True if a ticket-create command carries a Review-uncommitted title.

    None if the command is unparseable and still looks like that create.
    """
    tokens = _tokenize(command)
    if tokens is None:
        if _JUNK_UNCOMMITTED.search(command) and _JUNK_CREATE_HINT.search(command):
            return None
        return False
    return any(_segment_is_junk_ticket_create(seg) for seg in _segments(tokens))


def _block_or_hatch(command: str, cwd: str) -> int:
    """One hatch round-trip for the whole command. Only an explicit approval allows; a missing,
    blank/bare, denied, expired, or unavailable hatch all fall through to the block."""
    hatch = hatch_escalation.request_hatch_approval(
        HOOK_ID, {"hook": HOOK_ID, "command": command}, cwd=cwd, command=command
    )
    if hatch.should_stop and hatch.approved:
        warn(f"raw tracker write approved via hatch escalation ({hatch.reason})")
        emit("allow", f"raw tracker write approved via hatch escalation ({hatch.reason})")
        return 0
    if hatch.should_stop:
        warn(f"raw tracker write hatch escalation denied: {hatch.reason}")
        emit("block", f"hatch escalation denied: {hatch.reason}\n{_BLOCK_MSG}")
        return BLOCK_EXIT_CODE
    emit("block", _BLOCK_MSG)
    return BLOCK_EXIT_CODE


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — blocking (fail-closed)")
        emit("block", f"{HOOK_ID}: could not inspect the command (fail-closed)")
        return BLOCK_EXIT_CODE

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    try:
        junk = is_junk_review_ticket_create(command)
    except Exception as exc:  # noqa: BLE001
        if _JUNK_UNCOMMITTED.search(command) and (
            _RAW_HINT.search(command) or re.search(r"\btask\s+(?:new|create)\b", command)
        ):
            warn(f"junk-title inspection failed ({exc}) — blocking")
            emit("block", _JUNK_BLOCK_MSG)
            return BLOCK_EXIT_CODE
        junk = False
    if junk is None:
        warn("unparseable command looks like a Review-uncommitted ticket create — blocking")
        emit("block", _JUNK_BLOCK_MSG)
        return BLOCK_EXIT_CODE
    if junk:
        emit("block", _JUNK_BLOCK_MSG)
        return BLOCK_EXIT_CODE

    if not task_cli_configured(cwd):
        emit("allow")
        return 0

    try:
        hit = is_raw_tracker_write(command)
    except Exception as exc:  # noqa: BLE001
        if not (_RAW_HINT.search(command) or _TRACKER_HINT.search(command)):
            warn(f"inspection failed ({exc}) — allowing")
            emit("allow")
            return 0
        warn(f"inspection failed on a plausible raw tracker write ({exc}) — blocking")
        hit = None

    if hit is False:
        emit("allow")
        return 0
    if hit is None:
        # Unparseable but plausibly a write: the hook cannot classify it, so no hatch approval
        # may cover it — hard block (fail-closed).
        warn("unparseable command looks like a raw tracker write — blocking (fail-closed)")
        emit("block", _BLOCK_MSG)
        return BLOCK_EXIT_CODE
    # A parsed tracker write: blocked, hatchable unless it is the categorical junk class.
    if _JUNK_UNCOMMITTED.search(command):
        # A tracker write carrying the junk phrase in any shape (a wrapped create, a REST body
        # title) is the categorical Review-uncommitted class — never offered to the hatch.
        emit("block", _JUNK_BLOCK_MSG)
        return BLOCK_EXIT_CODE
    return _block_or_hatch(command, cwd)


if __name__ == "__main__":
    sys.exit(main())
