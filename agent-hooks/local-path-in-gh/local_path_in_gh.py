#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — block local filesystem paths in text written to GitHub.

Incident this closes (agent-tools#1002): an acceptance criterion on a fork ticket was "proven"
with a local tmp-dir path rendered as an image link. Nobody reading GitHub can open that path
and it is gone after a reboot, so the gate was satisfied by an artifact that never existed for
anyone but the machine that wrote it. The owner called it unacceptable and ordered a mechanical
guard: a local path is never a proof and never a useful reference in a PR / issue / comment.

What is gated (argv-parsed with shlex, chain-aware — never a raw substring match on the whole
command, so `ls /tmp && gh pr comment 1 --body ok` is fine):

- `gh pr|issue create|edit|comment|review|close|reopen|merge`, `gh release create|edit`:
  `--title/-t`, `--body/-b`, `--notes/-n`, `--comment/-c`, `--subject`, and the file flags
  `--body-file/--notes-file/-F` (the FILE'S CONTENT is scanned, never its name; `-` reads stdin
  from a heredoc, `< file`, `cat file |`, `echo|printf |`, or a here-string).
- `gh api` WRITES (explicit POST/PUT/PATCH/DELETE, or the implied POST of any `-f/-F/--field/
  --raw-field/--input`) to an issues / pulls / comments / reviews / releases / discussions
  endpoint or `graphql`: every field value, `-F key=@file` contents, `--input` contents. The
  endpoint path itself is never scanned. Reads (GET) and other endpoints are ignored.
- task-cli `task new|create|change|check|attach|done|comment|status|accept`: the prose fields
  (`--title --what --acceptance --why --impact --if-not-done --from-message --force
  --skip-* --deployed --post-merge-acceptance`, comment text, `--body-file` contents) and the
  proof references (`--proof --screenshot`, `attach` files), because task-cli writes them to the
  tracker. `-C/--cwd`, `--config`, `--repo`, `--backend` are not text and are never scanned.

What counts as a local path (anywhere in the text, outside a URL): `/tmp`, `/var/tmp`,
`/var/folders`, `/private/tmp`, `/dev/shm`, `/run/user`, `/root`, `/home/<user>`, `/Users/<user>`
(a `<placeholder>` / `${USER}` user segment is a generic example, not a path, and passes), `~/`,
`$HOME`/`${HOME}`/`$TMPDIR`, Windows `X:\\` / `X:/` and `%USERPROFILE%`/`%TEMP%`, `file://`
URLs, the literal value of the CURRENT home directory at runtime (printed as `<home>`), and
markdown / HTML image and link targets that are not http(s), `#anchor`, `mailto:`, a relative
path (an absolute `/x` target is a site-root link on GitHub, never repo-relative, so it is
flagged). A token is only a path when it
starts at a boundary: `src/foo.cpp`, `./tmp/x`, `docs/home/x.md`, `https://x/tmp/y`,
`user@host:/p` are not.

Deliberately simple and strict (owner: "prefer predictable"): there is NO fenced-code-block
exemption — a path inside ``` is still a path that will be posted, and the remediation is to
paste CONTENT (scrubbed of paths), not a pointer. What a reader needs is the output itself.

The only bypass is the hatch (deny-by-default, one-time Telegram approval):
`RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH="<written justification>" <command>` (a bare `1`/`true` is
rejected, no Telegram call). One hatch round-trip covers the whole invocation: findings from
every gated segment of a chained command are gathered first, then ONE approval is requested.

Known, deliberately unclosed residuals (heuristic, same class as the sibling hooks): a command
substitution that builds the text (`--body "$(some-script)"`) is not executed or inspected; a
piped producer other than `cat`/`echo`/`printf` is opaque; a body file that does not exist at
hook time (and is not written earlier in the same command) is skipped — gh itself will fail on
it; `bash -c`/`sh -c`/`eval` strings are re-parsed to depth 3, deeper nesting is not.

Contract (agents-hooks/v1): stdin JSON event (shell command in args.command), stdout protocol
JSON, stderr human log. exit 0 allow, exit 10 BLOCK. on_error is "open": this is a hygiene
guard on prose, not a security boundary, so a hook bug must never wedge unrelated commands.
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

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "local-path-in-gh"
HATCH_ENV_VAR = "RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH"

_MAX_FILE_BYTES = 2_000_000
_MAX_SHOWN = 3
_MAX_TOKEN_CHARS = 90
_MAX_NESTING = 3


def emit(decision: str, message: str | None = None) -> None:
    out: dict[str, str] = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"{HOOK_ID}: {msg}\n")


# ── local-path detection ──────────────────────────────────────────────────────────────────────

_PATH_CHARS = r"[^\s\"'`<>)\]]*"
# A path only starts at a boundary: `docs/home/x`, `./tmp/x`, `x.com/tmp/y`, `$HOMEBREW` are not.
_BOUND = r"(?<![\w.\-~$@%/\\])"

_SYSTEM_RE = re.compile(
    _BOUND
    + r"/(?:private/)?(?:tmp|var/tmp|var/folders|dev/shm|run/user|root"
    + r"|(?:home|Users)(?!/[<{$%]))(?![\w\-])"
    + _PATH_CHARS
)
_TILDE_RE = re.compile(_BOUND + "~/" + _PATH_CHARS)
_ENV_RE = re.compile(
    r"(?<![\w$%])(?:\$\{?(?:HOME|TMPDIR)\}?(?!\w)"
    r"|%(?:USERPROFILE|HOMEPATH|TEMP|TMP|APPDATA|LOCALAPPDATA)%"
    r"|\$env:(?:USERPROFILE|TEMP|TMP|HOME)(?!\w))" + _PATH_CHARS
)
_WINDOWS_RE = re.compile(r"(?<![\w/\\.\-])[A-Za-z]:[\\/]" + _PATH_CHARS)
_FILE_URL_RE = re.compile(r"(?<![\w])file://" + _PATH_CHARS, re.IGNORECASE)
_URL_RE = re.compile(r"(?<![\w])[A-Za-z][A-Za-z0-9+.\-]*://[^\s<>\"')\]]*")
_REMOTE_RE = re.compile(r"[\w.\-]+@[\w.\-]+:\S*")
_MD_TARGET_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^)]*[\"'])?\s*\)")
_MD_REF_RE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*<?(\S+?)>?(?:\s|$)", re.MULTILINE)
_HTML_TARGET_RE = re.compile(r"""\b(?:src|href)\s*=\s*(?:["']([^"']+)["']|([^\s>"']+))""", re.IGNORECASE)
_OK_TARGET_RE = re.compile(r"^(?:#|https?:|mailto:|tel:|data:|[A-Za-z][A-Za-z0-9+.\-]*://)", re.IGNORECASE)
_LOCAL_TARGET_RE = re.compile(r"^(?:/|~|\\\\|[A-Za-z]:[\\/]|file:)", re.IGNORECASE)

_KIND_BY_PATTERN = (
    (_FILE_URL_RE, "file:// URL"),
    (_ENV_RE, "home/tmp env path"),
    (_TILDE_RE, "home dir (~/)"),
    (_WINDOWS_RE, "Windows path"),
    (_SYSTEM_RE, "local system path"),
)


def _home_dirs() -> list[str]:
    homes: set[str] = set()
    candidates = [os.environ.get("HOME"), os.path.expanduser("~")]
    try:
        import pwd

        candidates.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        pass
    for home in candidates:
        if home and home not in ("~", "/") and len(home.rstrip("/")) > 1:
            homes.add(home.rstrip("/"))
    return sorted(homes, key=len, reverse=True)


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " " * (span[1] - span[0]) + text[span[1] :]


def _strip_trailing(token: str) -> str:
    return token.rstrip(".,;:!?")


def find_local_paths(text: str, *, is_ref: bool) -> list[tuple[str, str]]:
    """Every local-path token in ``text`` as ``(token, kind)``, in order of appearance.

    ``is_ref``: the whole text is itself a file reference (a task-cli ``--proof`` value), so ANY
    absolute local path counts, not just the well-known roots."""
    found: list[tuple[int, str, str]] = []
    work = text

    for match in _FILE_URL_RE.finditer(work):
        found.append((match.start(), _strip_trailing(match.group(0)), "file:// URL"))
    for match in list(_FILE_URL_RE.finditer(work)):
        work = _blank(work, match.span())

    for regex in (_MD_TARGET_RE, _MD_REF_RE, _HTML_TARGET_RE):
        for match in regex.finditer(work):
            target = match.group(1) or (match.group(2) if regex.groups > 1 else None)
            if not target:
                continue
            if _OK_TARGET_RE.match(target) or not _LOCAL_TARGET_RE.match(target):
                continue
            if re.match(r"^/(?:home|Users)/[<{$%]", target):
                continue
            found.append((match.start(), _strip_trailing(target), "local link target"))

    for regex in (_URL_RE, _REMOTE_RE):
        for match in list(regex.finditer(work)):
            work = _blank(work, match.span())

    spans: list[tuple[int, int]] = []
    for home in _home_dirs():
        for match in re.finditer(r"(?<![\w.\-])" + re.escape(home) + r"(?![\w\-])" + _PATH_CHARS, work):
            found.append((match.start(), _strip_trailing(match.group(0)), "current home dir"))
            spans.append(match.span())
    for regex, kind in _KIND_BY_PATTERN[1:]:
        for match in regex.finditer(work):
            if any(s <= match.start() < e for s, e in spans):
                continue
            found.append((match.start(), _strip_trailing(match.group(0)), kind))
            spans.append(match.span())

    stripped = text.strip()
    if is_ref and stripped.startswith("/") and not any(f[1] == stripped for f in found):
        found.append((0, stripped, "absolute local path"))

    found.sort(key=lambda item: item[0])
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for _, token, kind in found:
        if token and token not in seen and token not in ("/", "~/"):
            seen.add(token)
            result.append((token, kind))
    return result


def mask(token: str) -> str:
    """Display form of a token: the current home dir becomes `<home>`, any other `/home/<name>`
    or `/Users/<name>` has its account segment replaced, and long tokens are shortened."""
    for home in _home_dirs():
        token = token.replace(home, "<home>")
    token = re.sub(r"(?<![\w.\-])/(home|Users)/(?![<{$%])[^/\s]+", r"/\1/<user>", token)
    if len(token) > _MAX_TOKEN_CHARS:
        token = token[: _MAX_TOKEN_CHARS - 1] + "…"
    return token


# ── command parsing ───────────────────────────────────────────────────────────────────────────

_HEREDOC_OP = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_]\w*))")
_HD_REF = re.compile(r"__HD(\d+)__")


def _extract_heredocs(command: str) -> tuple[str, list[str]]:
    """Cut each terminated heredoc body out of ``command`` and leave a `<<__HD<n>__` marker, so
    the chain splitter never sees the body's lines as commands. Returns (command, bodies)."""
    lines = command.split("\n")
    out: list[str] = []
    bodies: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        i += 1
        cursor = i
        valid: list[tuple[re.Match[str], int]] = []
        for match in _HEREDOC_OP.finditer(line):
            word = match.group(2) or match.group(3) or match.group(4)
            dashed = bool(match.group(1))
            body: list[str] = []
            j = cursor
            while j < n:
                candidate = lines[j].lstrip("\t") if dashed else lines[j]
                if candidate == word:
                    break
                body.append(lines[j])
                j += 1
            else:
                continue  # never terminated: not a heredoc (e.g. `a << b` inside a quoted string)
            valid.append((match, len(bodies)))
            bodies.append("\n".join(body))
            cursor = j + 1
        for match, idx in reversed(valid):
            line = line[: match.start()] + f"<<__HD{idx}__" + line[match.end() :]
        out.append(line)
        if valid:
            i = cursor
    return "\n".join(out), bodies


def _split_chain(command: str) -> list[tuple[str, str]]:
    """Split on shell operators outside quotes, as `(segment, operator_before_it)`. Honors
    backslash escapes and backslash-newline continuations (joined into one line)."""
    command = command.replace("\\\n", " ")
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    sep = ""
    quote: str | None = None
    i, n = 0, len(command)

    def flush(next_sep: str) -> None:
        nonlocal buf, sep
        segments.append(("".join(buf), sep))
        buf = []
        sep = next_sep

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        prev = command[i - 1] if i > 0 else ""
        if quote is not None:
            buf.append(c)
            if c == "\\" and quote == '"' and nxt:
                buf.append(nxt)
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        elif c == "\\" and nxt:
            buf.append(c + nxt)
            i += 2
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
        elif command[i : i + 2] in ("&&", "||"):
            flush(command[i : i + 2])
            i += 2
        elif c == "&" and prev not in ("&", ">", "<") and nxt not in ("&", ">"):
            flush("&")
            i += 1
        elif c in (";", "|", "\n"):
            flush(c)
            i += 1
        else:
            buf.append(c)
            i += 1
    flush("")
    return [(s, p) for s, p in segments if s.strip()]


_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_WRAPPERS = frozenset({"env", "command", "nohup", "time", "exec", "builtin"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_REDIRECT_OPS = frozenset({"<", "<<", "<<<", ">", ">>"})


class Redirects:
    def __init__(self) -> None:
        self.stdin_file: str | None = None
        self.here_string: str | None = None
        self.out_files: list[str] = []
        self.append_files: list[str] = []


def _split_redirects(tokens: list[str]) -> tuple[list[str], Redirects]:
    """Pull `< f`, `<<< s`, `> f`, `>> f`, `2>&1` out of argv. The tokenizer only emits a bare
    `<`/`>` run for an UNQUOTED operator, so a quoted body such as '<img src=...>' stays a value.
    `<<` is a heredoc whose body was already cut out; its `__HD<n>__` marker is dropped."""
    rest: list[str] = []
    redirects = Redirects()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok not in _REDIRECT_OPS:
            rest.append(tok)
            i += 1
            continue
        target = tokens[i + 1] if i + 1 < len(tokens) else ""
        i += 2
        if tok == "<":
            redirects.stdin_file = target
        elif tok == "<<<":
            redirects.here_string = target
        elif tok != "<<" and target and not target.startswith("&") and target != "/dev/null":
            redirects.out_files.append(target)
            if tok == ">>":
                redirects.append_files.append(target)
    return rest, redirects


def _tokenize(segment: str) -> list[str] | None:
    try:
        lex = shlex.shlex(segment, posix=True, punctuation_chars="<>")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return None


_TIMEOUT_WRAPPERS = frozenset({"timeout", "gtimeout"})
_TIMEOUT_VALUE_FLAGS = frozenset({"-s", "--signal", "--kill-after", "-" + "k"})


def _skip_timeout_args(tokens: list[str], i: int) -> int:
    """Index of the wrapped command after `timeout [opts] DURATION`."""
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in _TIMEOUT_VALUE_FLAGS else 1
    return i + 1  # the DURATION operand


def _strip_prefix(tokens: list[str]) -> list[str]:
    """argv after leading `VAR=value` assignments and `env`/`command`/`nohup`/`time` wrappers."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGN_RE.match(tok):
            i += 1
        elif os.path.basename(tok) in _WRAPPERS:
            i += 1
        elif os.path.basename(tok) in _TIMEOUT_WRAPPERS:
            i = _skip_timeout_args(tokens, i + 1)
        elif tok.startswith("-") and i > 0 and os.path.basename(tokens[i - 1]) in _WRAPPERS:
            i += 1
        else:
            break
    return tokens[i:]


class Source(NamedTuple):
    """One piece of text this command would write to GitHub, and where it came from."""

    command: str  # `gh pr create`
    flag: str  # `--body`, `--body-file x (contents)`, `heredoc`
    text: str
    is_ref: bool = False


class Context:
    """What the segment parser may consult: the event cwd and files written earlier in the SAME
    command (their future content, not whatever stale file sits on disk)."""

    def __init__(self, cwd: str, heredocs: list[str]) -> None:
        self.cwd = cwd
        self.heredocs = heredocs
        self.written: dict[str, str] = {}
        self.segment_heredocs: list[str] = []
        self.command_label = ""

    def resolve(self, name: str) -> str:
        expanded = os.path.expanduser(name)
        return os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(self.cwd, expanded))

    def read(self, name: str) -> str | None:
        path = self.resolve(name)
        if path in self.written:
            return self.written[path]
        try:
            if os.path.getsize(path) > _MAX_FILE_BYTES:
                return None
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None


def _short(flag: str, token: str) -> str | None:
    """The attached value of a clustered short flag (`-bTEXT` → `TEXT`), or None."""
    if len(token) > 2 and token.startswith(flag) and not token.startswith("--"):
        return token[2:]
    return None


def _flag_values(
    args: list[str], value_flags: frozenset[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Walk ``args``: `([(flag, value)…], positionals)` for flags in ``value_flags`` (long
    `--f v`, `--f=v`, short `-f v`, `-fv`). Unknown flags are treated as booleans."""
    pairs: list[tuple[str, str]] = []
    positionals: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok == "-" or not tok.startswith("-"):
            positionals.append(tok)
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name in value_flags:
                if eq:
                    pairs.append((name, value))
                elif i < len(args):
                    pairs.append((name, args[i]))
                    i += 1
            continue
        flag = tok[:2]
        if flag in value_flags:
            attached = _short(flag, tok)
            if attached is not None:
                pairs.append((flag, attached.lstrip("=") if attached.startswith("=") else attached))
            elif i < len(args):
                pairs.append((flag, args[i]))
                i += 1
    return pairs, positionals


# ── gh ────────────────────────────────────────────────────────────────────────────────────────

_GH_GATED = {
    "pr": frozenset({"create", "edit", "comment", "review", "close", "reopen", "merge"}),
    "issue": frozenset({"create", "edit", "comment", "close", "reopen"}),
    "release": frozenset({"create", "edit"}),
}
_GH_TEXT_FLAGS = frozenset({"--title", "-t", "--body", "-b", "--notes", "-n", "--subject"})
# `--comment/-c` carries a value only on close/reopen (on `gh pr review` it is a boolean).
_GH_COMMENT_FLAGS = frozenset({"--comment", "-c"})
_GH_FILE_FLAGS = frozenset({"--body-file", "--notes-file", "-F"})
_GH_VALUE_FLAGS = _GH_TEXT_FLAGS | _GH_FILE_FLAGS | frozenset(
    {"-R", "--repo", "--hostname", "-a", "--assignee", "-l", "--label", "-m", "--milestone",
     "-p", "--project", "-r", "--reviewer", "-B", "--base", "-H", "--head", "-T", "--template",
     "--add-label", "--remove-label", "--add-assignee", "--remove-assignee", "--target",
     "--discussion-category", "--notes-start-tag", "--reason", "-d", "--delete-branch-name"}
)
_GH_API_VALUE_FLAGS = frozenset(
    {"-X", "--method", "-f", "--raw-field", "-F", "--field", "--input", "-H", "--header", "-q", "--jq",
     "-t", "--template", "--hostname", "--cache", "-p", "--preview"}
)
_GH_API_TEXT_ENDPOINT = re.compile(r"(?:^|/)(?:issues|pulls|comments|reviews|releases|discussions)(?:/|\?|$)|(?:^|/)graphql(?:\?|$)")


def _gh_words(args: list[str]) -> tuple[str | None, int, str | None, int]:
    """(group, index after group, sub, index after sub) skipping `-R x`/`--hostname x` first."""
    words: list[tuple[str, int]] = []
    i = 0
    while i < len(args) and len(words) < 2:
        tok = args[i]
        i += 1
        if tok in ("-R", "--repo", "--hostname"):
            i += 1
        elif not tok.startswith("-"):
            words.append((tok, i))
    group, after_group = words[0] if words else (None, len(args))
    sub, after_sub = words[1] if len(words) > 1 else (None, len(args))
    return group, after_group, sub, after_sub


def _stdin_text(ctx: Context, redirects: Redirects, prev: tuple[list[str], str] | None) -> str | None:
    if redirects.here_string is not None:
        return redirects.here_string
    if redirects.stdin_file:
        return ctx.read(redirects.stdin_file)
    if ctx.segment_heredocs:
        return None  # this segment's own heredoc is scanned as its own source; never twice
    if prev is None:
        return None
    toks = _strip_prefix(prev[0])
    if not toks:
        return None
    head, rest = os.path.basename(toks[0]), toks[1:]
    piped_heredocs = [ctx.heredocs[int(n)] for n in _HD_REF.findall(prev[1]) if int(n) < len(ctx.heredocs)]
    if head == "cat" and piped_heredocs:
        operands = [t for t in rest if not t.startswith(("-", "<")) and not _HD_REF.fullmatch(t)]
        if not operands:
            return "\n".join(piped_heredocs)
    if head in ("echo", "printf"):
        return "\n".join(t for t in rest if not t.startswith("-") or head == "printf")
    if head == "cat":
        files = [t for t in rest if not t.startswith("-")]
        pieces = [ctx.read(f) for f in files]
        return "\n".join(p for p in pieces if p is not None) if files else None
    return None


def _file_sources(
    command: str, flag: str, name: str, ctx: Context, stdin: str | None
) -> list[Source]:
    if name == "-":
        return [Source(command, f"{flag} - (stdin)", stdin)] if stdin is not None else []
    content = ctx.read(name)
    if content is None:
        return []
    return [Source(command, f"{flag} {os.path.basename(name) or name} (contents)", content)]


def _gh_sources(
    args: list[str], ctx: Context, redirects: Redirects, prev: tuple[list[str], str] | None
) -> list[Source] | None:
    """Sources of a gated `gh` write, `[]` when gated but carrying no text, None when ungated."""
    group, after_group, sub, after_sub = _gh_words(args)
    if group == "api":
        return _gh_api_sources(args[after_group:], ctx, redirects, prev)
    if group not in _GH_GATED or sub not in _GH_GATED[group]:
        return None
    command = ctx.command_label = f"gh {group} {sub}"
    comment_flags = _GH_COMMENT_FLAGS if sub in ("close", "reopen") else frozenset()
    pairs, _ = _flag_values(args[after_sub:], _GH_VALUE_FLAGS | comment_flags)
    stdin = _stdin_text(ctx, redirects, prev) if any(f in _GH_FILE_FLAGS and v == "-" for f, v in pairs) else None
    sources: list[Source] = []
    for flag, value in pairs:
        if flag in _GH_TEXT_FLAGS or flag in comment_flags:
            sources.append(Source(command, flag, value))
        elif flag in _GH_FILE_FLAGS:
            sources += _file_sources(command, flag, value, ctx, stdin)
    return sources


def _gh_api_sources(
    args: list[str], ctx: Context, redirects: Redirects, prev: tuple[list[str], str] | None
) -> list[Source] | None:
    pairs, positionals = _flag_values(args, _GH_API_VALUE_FLAGS)
    endpoint = positionals[0] if positionals else ""
    method = None
    for flag, value in pairs:
        if flag in ("-X", "--method"):
            method = value.upper()
    has_body = any(f in ("-f", "--raw-field", "-F", "--field", "--input") for f, _ in pairs)
    is_write = method in ("POST", "PUT", "PATCH", "DELETE") if method else has_body
    if not is_write or not _GH_API_TEXT_ENDPOINT.search(endpoint):
        return None
    command = ctx.command_label = "gh api"
    needs_stdin = any(
        (f in ("-F", "--field") and v.partition("=")[2] == "@-") or (f == "--input" and v == "-")
        for f, v in pairs
    )
    stdin = _stdin_text(ctx, redirects, prev) if needs_stdin else None
    sources: list[Source] = []
    for flag, value in pairs:
        if flag in ("-f", "--raw-field"):
            sources.append(Source(command, f"{flag} {value.partition('=')[0]}=", value.partition("=")[2]))
        elif flag in ("-F", "--field"):
            key, _, raw = value.partition("=")
            label = f"{flag} {key}="
            if raw.startswith("@"):
                sources += _file_sources(command, label, raw[1:], ctx, stdin)
            else:
                sources.append(Source(command, label, raw))
        elif flag == "--input":
            sources += _file_sources(command, flag, value, ctx, stdin)
    return sources


# ── task-cli ──────────────────────────────────────────────────────────────────────────────────

_TASK_GATED = frozenset({"new", "create", "change", "check", "attach", "done", "comment", "status", "accept"})
_TASK_TEXT_FLAGS = frozenset(
    {"--title", "--what", "--acceptance", "--why", "--impact", "--if-not-done", "--from-message",
     "--deployed", "--post-merge-acceptance"}
)
_TASK_REF_FLAGS = frozenset({"--proof", "--screenshot"})
_TASK_OTHER_VALUE_FLAGS = frozenset(
    {"-C", "--cwd", "--backend", "--repo", "--config", "--label", "--unlabel", "--due", "--body-file"}
)


def _task_value_flags(sub: str, args: list[str]) -> frozenset[str]:
    flags = set(_TASK_TEXT_FLAGS | _TASK_REF_FLAGS | _TASK_OTHER_VALUE_FLAGS)
    flags |= {a.partition("=")[0] for a in args if a.startswith("--skip-")}
    if sub in ("new", "create", "check"):
        flags.add("--force")  # a reason here, a boolean on change/done/status
    return frozenset(flags)


def _task_sources(
    args: list[str], ctx: Context, redirects: Redirects, prev: tuple[list[str], str] | None
) -> list[Source] | None:
    sub = None
    task_cwd = None
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok in ("-C", "--cwd", "--backend", "--repo", "--config"):
            if tok in ("-C", "--cwd") and i < len(args):
                task_cwd = args[i]
            i += 1
        elif tok.startswith("--cwd="):
            task_cwd = tok.partition("=")[2]
        elif not tok.startswith("-"):
            sub = tok
            break
    if sub not in _TASK_GATED:
        return None
    command = ctx.command_label = f"task {sub}"
    if task_cwd:  # task reads its --body-file relative to -C/--cwd, so the hook must too
        outer_cwd, ctx.cwd = ctx.cwd, ctx.resolve(task_cwd)
        try:
            return _task_sources_in_cwd(sub, args[i:], command, ctx, redirects, prev)
        finally:
            ctx.cwd = outer_cwd
    return _task_sources_in_cwd(sub, args[i:], command, ctx, redirects, prev)


def _task_sources_in_cwd(
    sub: str, rest: list[str], command: str, ctx: Context, redirects: Redirects,
    prev: tuple[list[str], str] | None,
) -> list[Source]:
    pairs, positionals = _flag_values(rest, _task_value_flags(sub, rest))
    stdin = _stdin_text(ctx, redirects, prev) if any(f == "--body-file" and v == "-" for f, v in pairs) else None
    sources: list[Source] = []
    for flag, value in pairs:
        if flag in _TASK_REF_FLAGS:
            sources.append(Source(command, flag, value, is_ref=True))
        elif flag == "--body-file":
            sources += _file_sources(command, flag, value, ctx, stdin)
        elif flag in _TASK_TEXT_FLAGS or flag.startswith("--skip-") or flag == "--force":
            sources.append(Source(command, flag, value))
    if sub in ("new", "create"):
        sources += [Source(command, "title", text) for text in positionals]
    elif sub == "comment":
        sources += [Source(command, "comment text", text) for text in positionals[1:]]
    elif sub == "attach":
        sources += [Source(command, "attached file", ref, is_ref=True) for ref in positionals[1:]]
    return sources


# ── tg (Telegram sender) ──────────────────────────────────────────────────────────────────────

# `--photo/--file` upload a local file on purpose, so they are not text and never scanned.
_TG_UPLOAD_FLAGS = frozenset({"--photo", "--file"})
_TG_OTHER_VALUE_FLAGS = frozenset({"--reply-to", "--subagent", "--topic", "--tag", "--ticket", "--pr", "--verify-cmd"})
_TG_TEXT_FLAGS = frozenset({"--title", "--impact", "--verify-result", "--evidence", "--summary"})


def _tg_sources(
    args: list[str], ctx: Context, redirects: Redirects, prev: tuple[list[str], str] | None
) -> list[Source]:
    command = ctx.command_label = "tg report" if "report" in args else "tg"
    pairs, positionals = _flag_values(args, _TG_UPLOAD_FLAGS | _TG_OTHER_VALUE_FLAGS | _TG_TEXT_FLAGS)
    sources = [Source(command, flag, value) for flag, value in pairs if flag in _TG_TEXT_FLAGS]
    sources += [Source(command, "message text", text) for text in positionals if text != "report"]
    stdin = _stdin_text(ctx, redirects, prev)
    if stdin is not None:
        sources.append(Source(command, "stdin", stdin))
    return sources


# ── segment driver ────────────────────────────────────────────────────────────────────────────

def _producer_text(
    argv: list[str], ctx: Context, redirects: Redirects, prev: tuple[list[str], str] | None
) -> list[str]:
    """The text a known producer writes: `echo`/`printf` arguments, `cat` operands' CONTENT (the
    operand NAMES are inputs, not posted text). Anything else yields nothing."""
    head, rest = os.path.basename(argv[0]), argv[1:]
    if head in ("echo", "printf"):
        return [t for t in rest if head == "printf" or not t.startswith("-")]
    if head == "cat":
        operands = [t for t in rest if not t.startswith("-")]
        if not operands:
            piped = _stdin_text(ctx, redirects, prev)
            return [piped] if piped else []
        return [text for text in (ctx.read(f) for f in operands) if text]
    return []


def _process_substitution_sources(segment: str, ctx: Context) -> list[Source]:
    """`--body-file <(printf …)` / `<(cat f)`: judged by what the producer prints."""
    sources: list[Source] = []
    for inner in re.findall(r"<\(([^()]*)\)", segment):
        toks = _tokenize(inner)
        if not toks:
            continue
        redirects = Redirects()
        for text in _producer_text(_strip_prefix(toks), ctx, redirects, None):
            sources.append(Source(ctx.command_label, "process substitution", text))
    return sources


def _record_written_files(
    tokens: list[str], redirects: Redirects, segment: str, ctx: Context, prev: tuple[list[str], str] | None
) -> None:
    """Remember what a `> f` / `>> f` / `tee f` in this command will put in `f`."""
    targets = list(redirects.out_files)
    appends = list(redirects.append_files)
    argv = _strip_prefix(tokens)
    text_parts = [ctx.heredocs[int(n)] for n in _HD_REF.findall(segment) if int(n) < len(ctx.heredocs)]
    if argv and os.path.basename(argv[0]) == "tee":
        tee_targets = [t for t in argv[1:] if not t.startswith("-")]
        targets += tee_targets
        if "-a" in argv[1:] or "--append" in argv[1:]:
            appends += tee_targets
        piped = _stdin_text(ctx, redirects, prev)
        if piped:
            text_parts.append(piped)
    elif targets:
        text_parts += _producer_text(argv, ctx, redirects, prev)
        if not text_parts:
            return  # an unknown producer (jq, cp, a script…): what lands in the file is opaque
    for target in targets:
        # `>>` and `tee -a` keep what the file already holds
        existing = ctx.read(target) if target in appends else None
        ctx.written[ctx.resolve(target)] = "\n".join(([existing] if existing else []) + text_parts)


_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _substituted_sources(segment: str, ctx: Context, depth: int) -> list[Source]:
    """`echo $(gh pr comment … --body '/tmp/x')` / backticks: the substituted command runs too."""
    if depth >= _MAX_NESTING:
        return []
    sources: list[Source] = []
    for match in _SUBST_RE.finditer(segment):
        sources += _command_sources(match.group(1) or match.group(2), ctx, depth + 1)
    return sources


def _segment_sources(
    segment: str, sep: str, prev: tuple[list[str], str] | None, ctx: Context, depth: int
) -> tuple[list[Source], list[str] | None]:
    segment = re.sub(r"^\s*[({]\s*", "", segment)  # `( gh … )` / `{ gh …; }` groups
    tokens = _tokenize(segment)
    if tokens is None:
        raw = re.sub(r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*", "", segment)
        if re.match(r"(?:\S*/)?(?:gh|task|tg)\s", raw):
            return [Source(raw.split()[0], "raw command text", segment)], None
        return [], None
    ctx.segment_heredocs = [
        ctx.heredocs[int(n)] for n in _HD_REF.findall(segment) if int(n) < len(ctx.heredocs)
    ]
    argv, redirects = _split_redirects(tokens)
    _record_written_files(tokens, redirects, segment, ctx, prev if sep == "|" else None)
    argv = _strip_prefix(argv)
    if not argv:
        return [], tokens
    head = os.path.basename(argv[0])
    if head in ("cd", "pushd") and len(argv) > 1 and argv[1] != "-":
        ctx.cwd = ctx.resolve(argv[1])  # later relative body files resolve from the new directory
        return [], tokens
    pipe_prev = prev if sep == "|" else None
    sources: list[Source] | None = None
    if head == "gh":
        sources = _gh_sources(argv[1:], ctx, redirects, pipe_prev)
    elif head == "task":
        sources = _task_sources(argv[1:], ctx, redirects, pipe_prev)
    elif head == "tg":
        sources = _tg_sources(argv[1:], ctx, redirects, pipe_prev)
    elif depth < _MAX_NESTING and head in _SHELLS and "-c" in argv[1:]:
        idx = argv.index("-c")
        if idx + 1 < len(argv):
            return _command_sources(argv[idx + 1], ctx, depth + 1), tokens
    elif depth < _MAX_NESTING and head == "eval" and len(argv) > 1:
        return _command_sources(" ".join(argv[1:]), ctx, depth + 1), tokens
    if sources is None:
        return _substituted_sources(segment, ctx, depth), tokens
    sources += [Source(ctx.command_label, "heredoc", body) for body in ctx.segment_heredocs]
    sources += _process_substitution_sources(segment, ctx)
    return sources, tokens


def _command_sources(command: str, ctx: Context, depth: int = 0) -> list[Source]:
    body, heredocs = _extract_heredocs(command)
    if depth == 0:
        ctx.heredocs = heredocs
    else:
        offset = len(ctx.heredocs)
        ctx.heredocs.extend(heredocs)
        body = _HD_REF.sub(lambda m: f"__HD{int(m.group(1)) + offset}__", body)
    sources: list[Source] = []
    prev: tuple[list[str], str] | None = None
    for segment, sep in _split_chain(body):
        try:
            found, tokens = _segment_sources(segment, sep, prev, ctx, depth)
        except Exception as exc:  # noqa: BLE001 — a parser bug must not wedge an unrelated command
            warn(f"could not analyse a segment ({type(exc).__name__}: {exc}) — skipping it")
            found, tokens = [], None
        sources += found
        prev = (tokens, segment) if tokens is not None else None
    return sources


class Finding(NamedTuple):
    command: str
    flag: str
    token: str
    kind: str


_CAT_SUBST_RE = re.compile(r"\$\(\s*cat\s+([^\s)]+)\s*\)|`\s*cat\s+([^\s`]+)\s*`")


def _expand_cat_substitutions(text: str, ctx: Context) -> str:
    """`--body "$(cat notes.md)"` posts the file's CONTENT: judge that, not the file's name (the
    sanctioned remediation is exactly 'put the text in a file, then read it'). An unreadable
    file expands to nothing — gh/the shell will fail on it themselves."""

    def read(match: re.Match[str]) -> str:
        return ctx.read(match.group(1) or match.group(2)) or ""

    return _CAT_SUBST_RE.sub(read, text)


def analyse(command: str, cwd: str) -> list[Finding]:
    ctx = Context(cwd, [])
    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for source in _command_sources(command, ctx):
        text = _expand_cat_substitutions(source.text, ctx)
        for token, kind in find_local_paths(text, is_ref=source.is_ref):
            shown = mask(token)
            key = (source.command, source.flag, shown)
            if key not in seen:
                seen.add(key)
                findings.append(Finding(source.command, source.flag, shown, kind))
    return findings


# ── message ───────────────────────────────────────────────────────────────────────────────────

def _trigger(findings: list[Finding]) -> str:
    shown = [f"{f.command} {f.flag}: `{f.token}` ({f.kind})" for f in findings[:_MAX_SHOWN]]
    extra = len(findings) - _MAX_SHOWN
    tail = f" (+{extra} more)" if extra > 0 else ""
    return "; ".join(shown) + tail


def _do_this(findings: list[Finding]) -> str:
    lines = [
        "DO THIS: attach the content itself, never the name of a local file.",
        "  - text / log / command output: paste an excerpt (or the whole file) as a fenced block "
        "```text ... ``` (```json, ```diff, ```cpp ... for code) or inside "
        "<details><summary>log</summary> ... </details>; scrub paths out of what you paste "
        "(use <tmp>, <home> or a repo-relative path).",
        "  - image / binary: upload it and link the https URL (push it to the repo's assets or "
        "pr-assets branch, `gh gist create`, `gh release upload`, or drag-drop it into the GitHub "
        "UI), then write ![name](https://...).",
        "  - command or file references: use repo-relative paths (src/foo.cpp, scripts/x.sh) or a "
        "<path> placeholder.",
    ]
    if any(f.command.startswith("task ") for f in findings):
        lines.append(
            "  - task-cli proofs: pass an https URL, or a path relative to the working directory "
            "to an existing image (task uploads it and records the hosted URL); for a non-image "
            "proof tick the box with --force '<reason>' that pastes the evidence or links the CI "
            "run."
        )
    return "\n".join(lines)


def build_message(findings: list[Finding], denied: str | None = None) -> str:
    denied_note = f"hatch escalation denied: {denied} — " if denied else ""
    return (
        f"{HOOK_ID} BLOCKED.\n"
        f"TRIGGER: {_trigger(findings)}.\n"
        "WHY: a local path means nothing to anyone reading GitHub — it cannot be opened there "
        "and is gone after a reboot — so as a proof or a reference it is fake.\n"
        f"{_do_this(findings)}\n"
        f"EXCEPTION: {denied_note}deny-by-default, no self-service bypass — "
        f'{HATCH_ENV_VAR}="<justification>" <command> asks for a one-time Telegram approval '
        "(a bare 1 is rejected)."
    )


def _printable(text: str) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:200]


# ── entry point ───────────────────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0
    if not isinstance(event, dict):
        warn(f"event is not a JSON object ({type(event).__name__}) — allowing (fail-open)")
        emit("allow")
        return 0

    cwd = str(event.get("cwd") or os.getcwd())
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0

    findings = analyse(command, cwd)
    if not findings:
        emit("allow")
        return 0

    context = {
        "hook": HOOK_ID,
        "command": command,
        "findings": [f"{f.command} {f.flag}: {f.token}" for f in findings[:10]],
    }
    hatch = hatch_escalation.request_hatch_approval(HOOK_ID, context, cwd=cwd, command=command)
    if hatch.should_stop and hatch.approved:
        warn(f"local path in GitHub text approved via hatch escalation ({hatch.reason})")
        emit("allow", f"{HOOK_ID}: approved via hatch escalation ({_printable(hatch.reason)})")
        return 0
    if hatch.should_stop:
        warn(f"hatch escalation denied: {hatch.reason}")
        emit("block", build_message(findings, denied=_printable(hatch.reason)))
        return BLOCK_EXIT_CODE
    emit("block", build_message(findings))
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
