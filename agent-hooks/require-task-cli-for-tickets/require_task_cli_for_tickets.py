#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — require task-cli for ticket creation/mutation.

task-cli (`task`, ~/xp/task-cli) is the ecosystem's ENFORCED ticket-creation interface: every
ticket it creates carries acceptance criteria, motivation, user-impact, and (for UI changes)
screenshots (`task.yaml`'s `enforce:` block), and every merged PR is expected to reference a
ticket task-cli produced. Creating or mutating a ticket directly through `gh issue
create/edit/close/comment` (or the `gh api` REST equivalents) skips every one of those gates —
exactly the incident this hook exists to stop (an `omp` session created a Linear issue directly,
bypassing task-cli entirely).

This hook catches the **GitHub Issues** half of that bypass: a `gh issue <mutating-subcommand>`
or a `gh api` call to a `repos/<owner>/<repo>/issues...` REST endpoint with a mutating method. It
does **not** catch a Linear MCP tool call or a raw Linear GraphQL API call — see the README's
"What's deliberately NOT covered" section for why (no MCP-tool hook point exists yet in this
family, and Linear's GraphQL API has one endpoint for both reads and mutations, so a Bash-level
heuristic there would either miss the real bypass or over-block legitimate reads).

Enforces task-cli as the ticket interface. Pairs with `require-ticket-before-commit` (which
gates the COMMIT side — a ticket reference must exist) — this hook gates the TICKET side (the
ticket itself must be created/mutated through the enforced CLI, not by hand).

Only fires when task-cli is actually usable here: the `task` binary must resolve on PATH (so a
machine that never provisioned task-cli, via rig's `tools.items.task`, is never blocked with no
alternative) AND the command's cwd must be inside a git repository (task-cli resolves its
backend/target from git context; outside a repo there is nothing for it to file against) AND the
resolved `task` binary must actually identify as task-cli (`task --help`'s own description
string) rather than a same-named Taskwarrior/go-task collision.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": a CRASH in the check fails open — process discipline, not a security
boundary, so a bug must never wedge every `gh issue`/`gh api` call. `REQUIRE_TASK_CLI_STRICT=0`
is the repo-level rollout dial back to warn-only (mirrors `require-ticket-before-commit`'s
`REQUIRE_TICKET_STRICT`); the default is strict (block).

No self-service bypass: request a one-time Telegram approval via
`RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS="<justification>"` (deny-by-default; a blank or
bare `1` is rejected — the value must be a real written justification; allowed ONLY on the
human's tg-ctl approval).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
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
        # A helper that calls sys.exit() at import must not make the hook exit 0 (allow);
        # convert it to an import failure after cleanup. Ctrl-C still propagates.
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ImportError(f"cannot execute hatch escalation helper from {hatch_init}: {exc}") from exc
    return module


hatch_escalation = _load_hatch_escalation()

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"
HOOK_ID = "require-task-cli-for-tickets"

# A STABLE marker embedded in every block message, so an external assertion can grep for it
# regardless of how the surrounding advice is reworded. Do not change it casually.
BLOCK_MARKER = "[require-task-cli] BLOCKED: direct ticket mutation"

_STRICT_FALSEY = frozenset({"0", "false", "no", "off"})
STRICT = os.environ.get("REQUIRE_TASK_CLI_STRICT", "1").strip().lower() not in _STRICT_FALSEY

# ── shell parsing (trimmed copy of require-ticket-before-commit's tokenizer/wrapper-peeler) ──
# Each hook is its own subprocess (no shared import path), so this logic is duplicated by
# design, per this family's convention (see agent-hooks/README.md). Trimmed here to what
# `gh issue`/`gh api` detection needs — no commit-message extraction, no `-C`/`-F` handling.
_SHELL_SEP = frozenset({"&&", "||", ";", "|", "&", ";;", "|&", ";&", ";;&"})
_LEADING_SHELL_NOISE = frozenset({
    "(", "{", "!", "then", "do", "else", "elif", "while", "until", "for", "case",
})
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_WRAPPERS = frozenset({
    "timeout", "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "unbuffer", "command",
    "sudo", "doas", "exec", "taskset", "chrt", "setpriv", "flock", "runuser",
})
_MAX_WRAPPER_NESTING = 16
_OPERAND_DROP_WRAPPERS = frozenset({"timeout", "taskset", "chrt", "flock"})
_MANDATORY_OPERAND_WRAPPERS = frozenset({"flock"})
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "--user", "-g", "--group", "-p", "--prompt", "-r", "--role",
        "-t", "--type", "-C", "--close-from", "-R", "--chroot", "-D", "--chdir",
        "-T", "--command-timeout", "-U", "--other-user", "-c", "--login-class",
        "-a", "--auth-type",
    }),
    "doas": frozenset({"-u", "-a", "-C"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({
        "-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid",
    }),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-P", "-a", "--argv0"}),
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "exec": frozenset({"-a"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "taskset": frozenset({"-c", "--cpu-list"}),
    "flock": frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}),
    "setpriv": frozenset({
        "--ruid", "--euid", "--reuid", "--rgid", "--egid", "--regid", "--groups", "--ptracer",
        "--securebits", "--pdeathsig", "--ambient-caps", "--inh-caps", "--bounding-set",
        "--selinux-label", "--apparmor-profile", "--landlock-access", "--landlock-rule",
    }),
    "chrt": frozenset({"-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline"}),
    "runuser": frozenset({
        "-c", "--command", "--session-command", "-g", "--group", "-G", "--supp-group",
        "-s", "--shell", "-u", "--user", "-w", "--whitelist-environment",
    }),
}
if not _OPERAND_DROP_WRAPPERS <= _WRAPPERS:
    raise RuntimeError(f"operand-drop wrappers missing from _WRAPPERS: {_OPERAND_DROP_WRAPPERS - _WRAPPERS}")
if not _MANDATORY_OPERAND_WRAPPERS <= _OPERAND_DROP_WRAPPERS:
    raise RuntimeError(f"mandatory-operand wrappers not in operand-drop: {_MANDATORY_OPERAND_WRAPPERS - _OPERAND_DROP_WRAPPERS}")
if not set(_WRAPPER_VALUE_FLAGS) <= _WRAPPERS:
    raise RuntimeError(f"value-flag wrappers missing from _WRAPPERS: {set(_WRAPPER_VALUE_FLAGS) - _WRAPPERS}")


def _short_value_letters(value_flags: frozenset[str]) -> frozenset[str]:
    return frozenset(f[1] for f in value_flags if len(f) == 2 and f.startswith("-"))


_WRAPPER_SHORT_VALUE_LETTERS = {w: _short_value_letters(f) for w, f in _WRAPPER_VALUE_FLAGS.items()}


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _is_gh_executable(tok: str) -> bool:
    return _basename(tok) == "gh"


def _strip_line_comment(line: str) -> str:
    in_single = in_double = False
    prev_ws = True
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and prev_ws and not in_single and not in_double:
            return line[:idx]
        prev_ws = ch.isspace()
    return line


def _tokenize_line(line: str) -> list[str] | None:
    lex = shlex.shlex(_strip_line_comment(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _tokenize(command: str) -> list[str] | None:
    joined = command.replace("\r\n", "\n").replace("\r", "\n")
    joined = joined.replace("\\\n", "")
    lines = joined.split("\n")
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
            return None
        if not first:
            out.append(";")
        first = False
        out.extend(toks)
        i += 1
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_SEP:
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    segs.append(cur)
    return segs


def _strip_leading_shell_noise(segment: list[str]) -> list[str]:
    i = 0
    while i < len(segment) and i < 16 and segment[i] in _LEADING_SHELL_NOISE:
        i += 1
    return segment[i:]


def _strip_redirects(segment: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    seen_ddash = False
    while i < len(segment):
        tok = segment[i]
        is_redir = (not seen_ddash and bool(tok) and tok not in _SHELL_SEP
                    and ("<" in tok or ">" in tok) and all(ch in "<>&" for ch in tok))
        if is_redir:
            if out and out[-1].isdigit():
                out.pop()
            i += 2 if i + 1 < len(segment) else 1
            continue
        if tok == "--":
            seen_ddash = True
        out.append(tok)
        i += 1
    return out


def _split_inline_env(segment: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    i = 0
    while i < len(segment):
        m = _INLINE_ENV.match(segment[i])
        if not m:
            break
        env[m.group(1)] = m.group(2)
        i += 1
    return env, segment[i:]


def _cluster_takes_next_value(tok: str, short_letters: frozenset[str]) -> bool:
    body = tok[1:]
    if not body or body[-1] not in short_letters:
        return False
    for ch in body[:-1]:
        if ch in short_letters:
            return False
    return True


def _skip_wrapper_args(wrapper: str, argv: list[str]) -> list[str]:
    value_flags = _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    short_letters = _WRAPPER_SHORT_VALUE_LETTERS.get(wrapper, frozenset())
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        consumes_next = (argv[i] in value_flags
                         or (not argv[i].startswith("--")
                             and _cluster_takes_next_value(argv[i], short_letters)))
        if consumes_next and i + 1 < len(argv):
            i += 2
            continue
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if wrapper in _OPERAND_DROP_WRAPPERS and i < len(argv) and _basename(argv[i]) not in _WRAPPERS:
        if wrapper in _MANDATORY_OPERAND_WRAPPERS or not _is_gh_executable(argv[i]):
            i += 1
    return argv[i:]


def _strip_wrappers(argv: list[str]) -> list[str]:
    guard = 0
    while argv and _basename(argv[0]) in _WRAPPERS:
        if guard >= _MAX_WRAPPER_NESTING:
            return argv
        guard += 1
        wrapper, argv = _basename(argv[0]), argv[1:]
        argv = _skip_wrapper_args(wrapper, argv)
        _, argv = _split_inline_env(argv)
    return argv


def gh_segments(command: str) -> list[list[str]]:
    """Every real `gh …` segment in `command` (a chain may hold more than one), wrapper- and
    env-peeled, starting AT the `gh` executable token. Empty on a tokenization failure (the safe
    direction: an unparseable command is not gated — this hook is on_error=open, a discipline
    reminder, not a security boundary)."""
    tokens = _tokenize(command)
    if tokens is None:
        return []
    out: list[list[str]] = []
    for raw_seg in _segments(tokens):
        seg = _strip_leading_shell_noise(_strip_redirects(raw_seg))
        _, rest = _split_inline_env(seg)
        gh_argv = _strip_wrappers(rest)
        if gh_argv and _is_gh_executable(gh_argv[0]):
            out.append(gh_argv)
    return out


# ── `gh issue`/`gh api` classification ────────────────────────────────────────────────────────
# `gh` global flags that take a SEPARATE value, appearing before the subcommand
# (`gh --repo owner/repo issue create`, `gh -R owner/repo issue list`).
_GH_GLOBAL_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})

# The `gh issue` subcommands that CREATE or MUTATE a ticket directly — exactly the bypass this
# hook exists to close. `view`/`list`/`status` (read-only) are deliberately excluded.
MUTATING_ISSUE_SUBCOMMANDS = frozenset({
    "create", "new", "edit", "close", "reopen", "comment", "delete",
    "transfer", "lock", "unlock", "pin", "unpin",
})
# `new` is gh's own documented alias for `create` (`gh issue create --help` lists it under
# ALIASES; verified live against gh 2.92.0/2.96.0) — without it, `gh issue new` bypasses the
# gate outright (review finding, agent-tools#597).

# HTTP methods on `gh api` that mutate. `gh api` defaults to GET; passing any of these (or a
# `-f`/`--field`/`--raw-field`/`--input` value, which makes gh api default to POST) is a write.
_MUTATING_HTTP_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_GH_API_METHOD_FLAGS = frozenset({"-X", "--method"})
_GH_API_FIELD_FLAGS = frozenset({"-f", "--field", "-F", "--raw-field", "--input"})
# `gh api`'s Go/pflag flag parser also accepts a field flag with its value ATTACHED, not just as
# a separate token: `--field=title=x` / `--raw-field=title=x` / `--input=file.json` (long-flag
# `=` form) and `-ftitle=x` / `-Ftitle=x` (short-flag concatenated form, no space or `=` needed).
# A separate-token-only check misses all of these (review finding, agent-tools#597; verified
# against the installed gh's own `gh api --help` flag grammar).
_GH_API_FIELD_FLAG_EQ_PREFIXES = tuple(f"{flag}=" for flag in _GH_API_FIELD_FLAGS if flag.startswith("--"))
_GH_API_SHORT_FIELD_FLAGS = tuple(flag for flag in _GH_API_FIELD_FLAGS if len(flag) == 2)

# A GitHub REST path that addresses an issue/its sub-resources:
#   - `repos/<owner>/<repo>/issues`, optionally `/<number>`, optionally a mutable sub-resource
#     (`comments`, `labels`, `assignees`, `lock`) with an optional trailing identifier
#     (`/issues/42/labels/bug` removes one specific label);
#   - OR `repos/<owner>/<repo>/issues/comments/<comment_id>` — GitHub's route for mutating ONE
#     issue comment directly, which does not nest under an issue number at all (review finding,
#     agent-tools#597: the previous pattern only matched `issues/<n>/comments`, never the
#     concrete per-comment route `issues/comments/<id>`).
_ISSUES_PATH_RE = re.compile(
    r"^/?repos/[^/\s]+/[^/\s]+/issues"
    r"(?:/comments/[^/\s]+"
    r"|/\d+(?:/(?:comments|labels|assignees|lock)(?:/[^/\s]+)?)?"
    r")?/?$",
    re.IGNORECASE,
)


def _skip_gh_global_flags(argv: list[str], start: int) -> int:
    """Return the index of the first non-global-flag token at/after `start` (`gh issue …` /
    `gh api …`), walking past `-R`/`--repo`/`--hostname` and any other leading `-…` flag."""
    i = start
    while i < len(argv):
        tok = argv[i]
        if tok in _GH_GLOBAL_VALUE_FLAGS and i + 1 < len(argv):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return i


def gh_issue_mutation(argv: list[str]) -> str | None:
    """If `argv` (a `gh …` segment, wrapper-peeled, argv[0] == gh) is a `gh issue
    <mutating-subcommand>` invocation, return that subcommand; else None. `gh issue view/list/
    status` and any unrecognized subcommand return None (fail open — not our job to catch).

    Global flags (`-R`/`--repo`/`--hostname`) are walked past both BEFORE `issue` (`gh -R o/r
    issue create`) and AFTER it (`gh issue -R o/r create`, `gh issue --repo o/r create`) — Cobra
    persistent flags are valid in either position, so both spots need the SAME value-consuming
    skip, not a bare `.startswith("-")` scan that treats `-R`'s separate value token as the
    subcommand itself (review finding, agent-tools#597)."""
    i = _skip_gh_global_flags(argv, 1)
    if i >= len(argv) or argv[i] != "issue":
        return None
    i = _skip_gh_global_flags(argv, i + 1)
    if i >= len(argv):
        return None
    sub = argv[i]
    return sub if sub in MUTATING_ISSUE_SUBCOMMANDS else None


def gh_api_issue_mutation(argv: list[str]) -> bool:
    """True when `argv` is a `gh api` call to a `repos/<owner>/<repo>/issues…` REST path with a
    mutating signal: an explicit mutating `-X`/`--method` (POST/PATCH/PUT/DELETE), or — with NO
    explicit method — any `-f`/`-F`/`--field`/`--raw-field`/`--input` (which makes `gh api`
    default to POST). A plain `gh api repos/o/r/issues` (no method, no fields) is a READ (gh
    api's own GET default) and is not gated — same read/write split as `gh issue view` vs `gh
    issue create`.

    An explicit `--method`/`-X` ALWAYS wins over field-flag inference, in both directions: `-X
    GET -f state=open` is a read (gh's own docs: `--method GET` sends fields as a query string,
    not a body) even though fields are present, and `-X POST` is a write even with no fields at
    all. Inferring the mutation from field presence alone when a method was explicitly given
    blocked legitimate filtered reads (review finding, agent-tools#597). `-X`'s compact forms
    (`-XPOST`, `-X=POST`) are recognized too, not just the separate-token/`--method=` spellings."""
    i = _skip_gh_global_flags(argv, 1)
    if i >= len(argv) or argv[i] != "api":
        return False
    i += 1
    path: str | None = None
    explicit_method: str | None = None
    has_field = False
    while i < len(argv):
        tok = argv[i]
        if tok in _GH_API_METHOD_FLAGS and i + 1 < len(argv):
            explicit_method = argv[i + 1].strip().upper()
            i += 2
            continue
        if tok.startswith("--method="):
            explicit_method = tok.split("=", 1)[1].strip().upper()
            i += 1
            continue
        # Compact short-flag forms of `-X`: `-XPOST` (concatenated, no separator) and `-X=POST`
        # (pflag also accepts a `=` after a short flag) — both verified live against the
        # installed gh (a 404 JSON body, not an "unknown flag" error, from `gh api -XGET
        # <nonexistent-path>` and `-X=GET`, proving gh parsed the method rather than ignoring
        # the token). Separate-token-or-`--method=`-only checking missed both (review-cli
        # quorum-pass finding, agent-tools#597).
        if tok.startswith("-X") and tok != "-X" and len(tok) > 2:
            value = tok[2:]
            if value.startswith("="):
                value = value[1:]
            explicit_method = value.strip().upper()
            i += 1
            continue
        if tok in _GH_API_FIELD_FLAGS and i + 1 < len(argv):
            has_field = True
            i += 2
            continue
        if tok.startswith(_GH_API_FIELD_FLAG_EQ_PREFIXES):
            has_field = True
            i += 1
            continue
        if tok.startswith(_GH_API_SHORT_FIELD_FLAGS) and len(tok) > 2:
            has_field = True
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            i += 1
            continue
        if path is None:
            path = tok
        i += 1
    if path is None or not _ISSUES_PATH_RE.match(path):
        return False
    if explicit_method is not None:
        return explicit_method in _MUTATING_HTTP_METHODS
    return has_field


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"require-task-cli: {msg}\n")


def _inside_git_repo(cwd: str | None) -> bool:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"could not check git repo: {exc}")
        return False


# A stable positive-identification marker: task-cli's own argparse description text (see
# task-cli's `tasklib/cli.py`, `description=`). `task` is a famously ambiguous binary name —
# Taskwarrior and go-task both install a `task` on PATH too — so `shutil.which("task") is not
# None` alone can resolve one of THOSE instead of task-cli on a machine that has one installed.
# This is the exact collision ship.sh's own acceptance gate already guards against
# (`ci/ship/ship.sh`'s `_acceptance_gate_looks_like_task_cli_json`, agent-tools#521): trust
# neither PATH resolution nor a zero exit code as proof of identity, only the tool's own
# distinctive output (review finding, agent-tools#597).
_TASK_CLI_SIGNATURE = "enforced ticket interface"


def _looks_like_task_cli_help(output: str) -> bool:
    return _TASK_CLI_SIGNATURE in output


def _task_binary_is_task_cli(cwd: str | None) -> bool:
    """Positively identify the `task` binary resolved on PATH as task-cli, not a same-named
    collision. Runs `task --help` — read-only, no side effects, no ticket touched — and checks
    for task-cli's own description string. Fails OPEN (returns False, so the gate does not fire)
    on any subprocess error: this hook nags, it doesn't wedge a machine that can't answer the
    question."""
    try:
        out = subprocess.run(
            ["task", "--help"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"could not verify task binary identity: {exc}")
        return False
    return _looks_like_task_cli_help(out.stdout) or _looks_like_task_cli_help(out.stderr)


def task_cli_active(cwd: str | None) -> bool:
    """task-cli is a usable alternative right now: the `task` binary resolves on PATH (a machine
    that never provisioned it via rig's `tools.items.task` is never blocked with nothing to point
    at instead) AND the command's cwd is inside a git repo (task-cli's default backend —
    github-issues — resolves the target repo from git context; task-cli falls through cleanly to
    github-issues with no `rig.yaml` `task:` block at all, so "task binary present + inside a git
    repo" already means task-cli is usable here, without duplicating task-cli's own config
    cascade resolution in this hook) AND that resolved binary is actually task-cli, not a
    same-named Taskwarrior/go-task collision (`_task_binary_is_task_cli`)."""
    if shutil.which("task") is None:
        return False
    if not _inside_git_repo(cwd):
        return False
    return _task_binary_is_task_cli(cwd)


_GUIDANCE = (
    "task-cli (`task`) is the enforced ticket-creation interface here — every ticket it files "
    "carries acceptance criteria, motivation, user-impact, and (for UI changes) screenshots, "
    "which a raw `gh issue`/`gh api` call skips entirely. Use `task new --title \"...\" "
    "--acceptance \"...\" --why \"...\" --impact \"...\"` to create a ticket, or "
    "`task classify \"<text>\" --create` to route an ambiguous request. To update an existing "
    "ticket use its task-cli workflow (e.g. `task done <id>`), not `gh issue edit/close/"
    "comment`. No self-service bypass: ASK the human, or request a one-time Telegram approval "
    'via RIG_HATCH_REQUEST_REQUIRE_TASK_CLI_FOR_TICKETS="<justification>" (deny-by-default; '
    "bare 1 rejected); or opt the whole gate back to warn-only with "
    "REQUIRE_TASK_CLI_STRICT=0."
)


def _describe_match(gh_argv: list[str]) -> str:
    sub = gh_issue_mutation(gh_argv)
    if sub is not None:
        return f"gh issue {sub}"
    return "gh api <issues endpoint>"


def _evaluate_gh_segment(gh_argv: list[str], cwd: str | None, command: str) -> int | None:
    """Decide ONE parsed `gh …` segment. Returns an exit code when the gate fires (block, or a
    warn-only allow), or None when this segment is clean (not a ticket mutation, or task-cli
    isn't active here) so the caller moves on to the next segment in the chain."""
    if gh_issue_mutation(gh_argv) is None and not gh_api_issue_mutation(gh_argv):
        return None
    if not task_cli_active(cwd):
        return None
    return _gate_direct_mutation(_describe_match(gh_argv), command, cwd)


def _gate_direct_mutation(what: str, command: str, cwd: str | None) -> int:
    if STRICT:
        hatch = hatch_escalation.request_hatch_approval(
            HOOK_ID, {"hook": HOOK_ID, "command": command}, cwd=cwd or os.getcwd(),
            command=command,
        )
        if hatch.should_stop:
            if hatch.approved:
                note = f"require-task-cli gate allowed via hatch escalation ({hatch.reason})"
                warn(note)
                emit("allow", note)
                return 0
            warn(f"require-task-cli gate hatch escalation denied: {hatch.reason}")
            emit("block", f"hatch escalation denied: {hatch.reason}\n{BLOCK_MARKER} ({what}). {_GUIDANCE}")
            return BLOCK_EXIT_CODE
        emit("block", f"{BLOCK_MARKER} ({what}). {_GUIDANCE}")
        return BLOCK_EXIT_CODE
    advisory = f"Direct ticket mutation ({what}) — allowing anyway (warn-only). {_GUIDANCE}"
    warn(advisory)
    emit("allow", advisory)
    return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError) as exc:
        warn(f"bad event JSON: {exc}")
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        emit("allow")
        return 0

    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        cwd = None

    for gh_argv in gh_segments(command):
        result = _evaluate_gh_segment(gh_argv, cwd, command)
        if result is not None:
            return result

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
