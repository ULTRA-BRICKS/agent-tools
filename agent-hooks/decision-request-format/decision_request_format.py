#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — send-time escalation-format gate.

When the agent escalates to the human's out-of-band channel — a ``tg --tag decision`` /
``--tag problem`` invocation — this inspects the message body for the structural markers the
`decision-request-discipline` skill requires (Context, Options / pros-cons table,
Recommendation). Both tags are the same escalation shape, so the check applies to each — an
agent picking ``problem`` instead of ``decision`` no longer skips it (agent-tools#213/#12).

There is NO ``question`` tag any more (agent-tools#524, tg-cli#301, Alex 2026-09-05): an open
question for the human IS a decision request and goes out as ``--tag decision`` in the
decision-request format. ``tg`` itself refuses the ``question`` tag; this hook BLOCKS the command
one step earlier (exit 10) with the same one-line hint, so the agent is redirected before a
doomed send ever runs. That block is unconditional — no hatch applies, because there is nothing
to force through: the tag does not exist.

Graduated, deliberately lenient verdict (Alex: hard-block a bare decision request, but a false positive
would wedge his ONLY comms channel to his agents, so err HARD toward passing):

  - COMPLETE body (all three markers)                 → silent allow (exit 0)
  - PARTIAL body (some structure, some missing)        → allow + an exit-0 advisory nudge
  - genuinely BARE body — a bare "A or B?" with NO
    table and NONE of the three markers               → **BLOCK (exit 10)**, the send does not
    run; the message shows the exact skeleton to re-send. A justified
    ``RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT`` hatch still forces it through.

ONLY the bare case blocks. A message that carries a pros/cons table (markdown OR HTML) — or any
one of Context / Options / Recommendation — is never blocked. Table detection is generous on
purpose: over-detecting only lets a message THROUGH.

Matching is by proper parsing, not raw-string search: the command is split into ``&&``/
``;``/``|`` segments, leading no-op wrappers (``env``, ``timeout``, ``nice``, …) are peeled,
and each segment is shlex-tokenized so a ``tg`` substring in a path or another flag's value
never trips it — only an actual ``tg`` command carrying an escalation ``--tag`` does.

NOT subagent-exempt: the check binds every agent, including the subagent that (per the skill)
drafts the request.

Escape (replaces the OLD self-service hatch): there is NO ``ALLOW_RAW_DECISION_REQUEST`` env or
``# decision-request-ok:`` inline sentinel any more — an agent could set either on its own
command. A one-time force-through of a bare body is requested by setting
``RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<written justification>"``, which routes a single
approval to the human via Telegram (deny-by-default; a bare ``1`` is rejected).

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow
                                    exit 10 : BLOCK (a bare body — hatchable; or the removed
                                              ``question`` tag — unconditional, no hatch)

on_error is "open": a crash here must never wedge the ability to send a message. EVERY error
path allows; the only hard stops are the deliberate, narrow bare-body block and the removed-tag
block.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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

HOOK_API = "agents-hooks/v1"
BLOCK_EXIT_CODE = 10

# Cheap pre-filter: a `tg` token appears at all (word-boundary). It deliberately does NOT
# anchor at a command start — a wrapped `timeout 30 tg …` has `tg` mid-line, and the
# authoritative check is the per-segment shlex unwrap + `tg`-head match below, not this. This
# only decides whether tokenizing is worth it; a path like `/opt/tg/bin` that survives here is
# rejected by the argv-head fullmatch.
_TG_CMD = re.compile(r"\btg\b")

# Leading no-op wrappers that prefix the real command without changing it (`env TG_AI_MODEL=
# claude tg …`, `timeout 30 tg …`). The real runner sits after the wrapper + its args, so we
# peel them before tokenizing. (Same conservative set as no-long-inline-process.)
_WRAPPERS = re.compile(r"^(?:timeout|env|nice|time|stdbuf|nohup|setsid|unbuffer)$")

# A bare leading `NAME=VALUE` assignment prefix (`TG_AI_MODEL=claude tg …`) — NO `env` keyword.
# This is the DOMINANT form: the decision-request-discipline skill literally tells the agent to
# run `TG_AI_MODEL=claude tg --tag decision …`, so without peeling this the gate would miss its
# own prescribed call. Mirrors the same peel in no-shell-file-edit. (`env NAME=VALUE …` is
# handled by the wrapper loop; this is the keyword-less shell assignment.)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Structural markers the self-check requires in a decision-request body. Each maps a
# human-facing label to the regex that detects its presence (case-insensitive). Kept
# deliberately permissive — the goal is to catch a body that mentions NONE of a dimension,
# not to grade phrasing.
_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `\S+\.\w+:\d+` matches the `file.ext:line` form the skill's format point 1 prescribes
    # (`app.py:42`, `src/loader.ts:128`), so a body that gives a code ref instead of the literal
    # word "context" is not falsely flagged as missing Context. The `.ext` requirement keeps a
    # bare `host:port` / `12:30` from being mistaken for a file ref and silencing the advisory.
    ("Context", re.compile(r"\bcontext\b|\bbackground\b|file:|\S+\.\w+:\d+|where the code", re.I)),
    ("Options", re.compile(r"\boptions?\b|\bvariants?\b|\bpros?\b|\bcons?\b|\btrade-?offs?\b", re.I)),
    ("Recommendation", re.compile(r"\brecommend|\bi suggest\b|\bi'd (?:go|pick)\b|\bproposal\b", re.I)),
)

# The escalation-shaped `tg` tags this hook inspects. Both route a message to the human's
# out-of-band channel expecting a structured escalation (context + pros/cons + a recommendation),
# per the decision-request-discipline skill — so the same self-check applies to each. `decision`
# was the original tag; `problem` is the same escalation shape and was added so the self-check is
# not silently skipped just because the agent picked a different tag word. (agent-tools#213/#12.)
# `question` USED to be a third member; it was removed (agent-tools#524) — see _REMOVED_TAG.
_ESCALATION_TAGS = frozenset({"decision", "problem"})

# The tag word REMOVED from `tg` (tg-cli#301): an open question is a decision request. It is
# still recognized here — not as an escalation to grade, but to BLOCK the command outright with
# the redirect below, so an agent with stale habits is corrected before `tg` refuses the send.
_REMOVED_TAG = "question"
_REMOVED_TAG_HINT = (
    "BLOCKED: the `question` tag was removed from tg — an open question is a decision request. "
    "Re-send it as `tg --tag decision` in the decision-request format (Context, a pros/cons "
    "table of the options, a Recommendation; read the decision-request-discipline skill)."
)


def _inspected_tag(word: str) -> str | None:
    """The lowercase tag this hook acts on for a ``--tag`` value — an escalation tag to grade, or
    the removed ``question`` tag to block — else None. Case-insensitive, like `tg` itself."""
    lowered = word.lower()
    if lowered in _ESCALATION_TAGS or lowered == _REMOVED_TAG:
        return lowered
    return None


# A pros/cons TABLE — markdown or HTML — is the strongest signal of a real, structured escalation
# and must ALWAYS let the message through (a false block of the human's only comms channel is far
# worse than a false pass). Detection is deliberately GENEROUS: over-detecting a table only makes
# the gate PASS more, which is the safe direction. Any of:
#   - an HTML `<table…>` open tag,
#   - a markdown delimiter row (`|---|---|`, `| :--: |`), the canonical table separator,
#   - two or more lines that each carry >=2 unescaped `|` (a piped grid).
_HTML_TABLE = re.compile(r"<table[\s/>]", re.I)
_MD_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
_MD_TABLE_ROW = re.compile(r"^[^\n|]*\|[^\n|]*\|", re.M)


def _has_table(body: str) -> bool:
    """Whether the body carries a pros/cons table (markdown or HTML). Generous by design: a false
    positive here only lets a message THROUGH, never blocks one."""
    if _HTML_TABLE.search(body):
        return True
    if _MD_TABLE_DELIM.search(body):
        return True
    return len(_MD_TABLE_ROW.findall(body)) >= 2


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"decision-request-format: {msg}\n")


def _allow(message: str | None = None) -> int:
    emit("allow", message)
    return 0


def _unwrap_segment(segment: str) -> list[str]:
    """Tokenize one command segment and strip leading no-op wrappers, returning the wrapped
    command's tokens (`env TG_AI_MODEL=claude tg "x"` → `['tg', 'x']`). A non-wrapper segment
    is returned tokenized as-is. Returns ``[]`` if the segment can't be tokenized."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return []
    # Peel a bare `NAME=VALUE` assignment prefix (`TG_AI_MODEL=claude tg …`) — possibly several
    # (`A=1 B=2 tg …`) — so the real command head is exposed. Done before the wrapper loop so
    # `A=1 timeout 30 tg …` also unwraps fully.
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    while tokens and _WRAPPERS.match(tokens[0]):
        wrapper, rest = tokens[0], tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if "=" not in tok and tok in ("-s", "-k", "--signal", "--kill-after", "-n", "-u"):
                    i += 1
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1  # a NAME=VALUE assignment env consumes before the command
                continue
            if wrapper == "timeout":
                i += 1  # the DURATION positional
            break
        if i:
            tokens = rest[i:]
        elif rest:
            tokens = rest
        else:
            break
    return tokens


def _split_top_level_segments(command: str) -> list[str]:
    """Split a command line on the shell separators ``&&`` / ``||`` / ``;`` / ``|`` / ``&`` /
    newline, QUOTE-AWARELY so a separator INSIDE a quoted argument does not tear the command
    apart. A naive ``re.split`` on ``|`` split ``tg --tag decision "A | B?"`` mid-quote into an
    unbalanced ``tg --tag decision "A `` segment that failed to shlex-tokenize, so the escalation
    went unseen and the bare-body block was bypassed for the common "A vs B" wording. The
    char-scan tolerates an unbalanced quote gracefully (it keeps the quote open to end-of-line)
    rather than raising, so a malformed command still fails toward the same lenient allow.

    SYNC: the same quote-aware scan as no-shell-file-edit `_split_segments`, trimmed to the
    separators this hook needs (a `tg` send has no meaningful redirect/comment semantics)."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if command[i:i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in (";", "|", "&", "\n"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s for s in segments if s.strip()]


def _escalation_request_body(command: str) -> tuple[str, str] | None:
    """If the command is a `tg` invocation carrying an escalation tag (``--tag decision`` /
    ``problem``) or the removed ``question`` tag, return ``(tag, body)`` — the matched tag and
    the message body it would send (positional text + any ``--title``); else None.

    Walks each ``&&``/``||``/``;``/``|``/``&``-separated segment (split QUOTE-AWARELY so a
    separator inside the quoted message body never tears it apart) so a `tg` later in a pipeline
    is seen. Uses shlex tokenization, not raw matching, so the tag and body are read exactly as
    `tg` would receive them."""
    if not _TG_CMD.search(command):
        return None
    for raw_segment in _split_top_level_segments(command):
        tokens = _unwrap_segment(raw_segment)
        if not tokens or not re.fullmatch(r"(?:\S*/)?tg", tokens[0]):
            continue
        result = _body_if_escalation_tag(tokens[1:])
        if result is not None:
            return result
    return None


def _body_if_escalation_tag(args: list[str]) -> tuple[str, str] | None:
    """Given a `tg` invocation's args, return ``(tag, body)`` iff a ``--tag`` from
    ``_ESCALATION_TAGS`` (decision/problem) — or the removed ``question`` tag — is present; else
    None. Collects positional (non-flag) text and the ``--title`` value, which together are what
    the human reads.
    Value-taking flags consume their next token so a flag value is never mistaken for the
    positional message."""
    matched_tag: str | None = None
    title = ""
    positionals: list[str] = []
    # The COMPLETE set of value-taking `tg` flags other than --tag/--title (handled above), so
    # a flag's VALUE is never collected as body text. If a future `tg` adds a value flag not
    # listed here, its value would fall to the `tok.startswith("-")` toggle branch and its
    # argument would leak into `positionals` — a false NEGATIVE only (a leaked value could
    # accidentally satisfy a marker → no advisory), never a false positive, and this hook is
    # advisory regardless. Keep this in sync with `tg --help`'s `--flag <value>` entries.
    value_flags = {"--photo", "--file", "--reply-to", "--pdf-device", "--format"}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--tag":
            # `--tag <word>` consumes the next token; a trailing bare `--tag` has none.
            candidate = args[i + 1] if i + 1 < len(args) else ""
            matched_tag = _inspected_tag(candidate) or matched_tag
            i += 2
            continue
        if tok.startswith("--tag="):
            matched_tag = _inspected_tag(tok.partition("=")[2]) or matched_tag
            i += 1
            continue
        if tok == "--title":
            if i + 1 < len(args):
                title = args[i + 1]
            i += 2
            continue
        if tok.startswith("--title="):
            title = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in value_flags:
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-"):
            i += 1  # a bare/unknown flag (toggle) — no value to skip
            continue
        positionals.append(tok)
        i += 1
    if matched_tag is None:
        return None
    return matched_tag, "\n".join([title, *positionals]).strip()


def _missing_markers(body: str) -> list[str]:
    """The required dimensions absent from the body. A pros/cons TABLE (markdown or HTML) counts
    as the Options dimension — so a table-carrying body is never told to "add a pros/cons table"
    it already has, and a Context + table + Recommendation body is COMPLETE (silent)."""
    has_table = _has_table(body)
    missing: list[str] = []
    for label, pat in _MARKERS:
        if pat.search(body):
            continue
        if label == "Options" and has_table:
            continue  # a pros/cons table IS the Options dimension
        missing.append(label)
    return missing


def _advisory_message(tag: str, missing: list[str]) -> str:
    # `missing` is only ever a subset of the three markers this hook can detect heuristically
    # (Context / Options / Recommendation). The skill asks for two more — a glossary of terms
    # and a "where to look" code ref — that a regex can't reliably check; they're named as a
    # separate reminder, NOT folded into the `missing` list, so the list stays honest about
    # what was actually measured.
    return (
        "Escalation self-check (advisory — your message still sends): this "
        f"`tg --tag {tag}` body appears to be missing {', '.join(missing)} (of the three "
        "markers this hook checks). Read the decision-request-discipline skill and send a "
        "structured escalation: context (a 'where to look' code ref + a glossary of internal "
        "terms), a pros/cons table of the options, and a recommendation — so the human can "
        "decide in 30s without opening the repo. Consider rewriting before sending — or, for a "
        'genuine one-time silence, request approval via '
        'RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<why>" (Telegram approval, deny-by-default).'
    )


def _block_message(tag: str) -> str:
    """The skeleton shown when a genuinely-bare escalation is BLOCKED — it must tell the agent
    exactly how to re-send correctly so the human's channel is never left with a bare 'A or B?'."""
    intro = (
        f"BLOCKED: this `tg --tag {tag}` message is a bare question with none of the required "
        "escalation structure, so it was NOT sent. The human's decision channel needs a "
        "self-contained escalation it can act on in 30s. Re-send with all three parts:"
    )
    # The skeleton lines are joined with explicit "\n" (not implicit-concat leading spaces) so a
    # re-indent can never silently corrupt the emitted layout.
    skeleton = "\n".join([
        "    Context: <where to look — file.ext:line — plus a glossary of any internal terms>",
        "    Options (a pros/cons table):",
        "        | Option | Pros | Cons |",
        "        | --- | --- | --- |",
        "        | A | … | … |",
        "        | B | … | … |",
        "    Recommendation: <which you'd pick and why>",
    ])
    outro = (
        "Read the decision-request-discipline skill for the full format. A message that already "
        "has a pros/cons table (markdown or HTML) is never blocked. If this is genuinely urgent "
        "and cannot be formatted right now, force it through with a written justification: "
        'RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT="<why>" (Telegram approval, deny-by-default; '
        "a bare 1 is rejected)."
    )
    return f"{intro}\n{skeleton}\n{outro}"


def _is_bare(body: str, missing: list[str]) -> bool:
    """A body is BARE — the only case that BLOCKS — when it carries none of the required
    structure: no pros/cons table AND every one of the three markers absent. This is intentionally
    the strictest possible block condition (err toward passing): anything with a table, or any one
    of Context / Options / Recommendation, is NOT bare and is allowed to send."""
    return len(missing) == len(_MARKERS) and not _has_table(body)


def _decide(command: str, cwd: str) -> tuple[str, str | None, int]:
    """Grade an escalation-tagged `tg` send. Returns (decision, message, exit_code):

      - the REMOVED `question` tag                    → BLOCK + the --tag decision redirect
                                                        (exit 10, no hatch — the tag is gone)
      - not an escalation send / a COMPLETE body      → allow, no message         (exit 0)
      - a PARTIAL body (some structure, some missing)  → allow + advisory nudge    (exit 0)
      - a genuinely BARE body (no structure at all)    → BLOCK + how-to skeleton   (exit 10),
        unless an approved RIG_HATCH_REQUEST_DECISION_REQUEST_FORMAT hatch forces it through.

    Only the BARE case blocks — the send-time hard stop the human asked for — and even then a
    justified Telegram hatch still lets it through. Everything with any structure sends."""
    matched = _escalation_request_body(command)
    if matched is None:
        return "allow", None, 0
    tag, body = matched
    if tag == _REMOVED_TAG:
        return "block", _REMOVED_TAG_HINT, BLOCK_EXIT_CODE
    missing = _missing_markers(body)
    if not missing:
        return "allow", None, 0
    if not _is_bare(body, missing):
        return "allow", _advisory_message(tag, missing), 0  # partial → non-blocking nudge
    hatch = hatch_escalation.request_hatch_approval(
        "decision-request-format",
        {"hook": "decision-request-format", "command": command},
        cwd=cwd,
        command=command,
    )
    if hatch.should_stop and hatch.approved:
        # Audit breadcrumb goes to stderr; the allow stays message-free so the allow/message
        # invariant holds (a `message` on an allow always means an advisory nudge).
        warn(f"bare escalation forced through via hatch escalation ({hatch.reason})")
        return "allow", None, 0
    return "block", _block_message(tag), BLOCK_EXIT_CODE


def main() -> int:
    # Fail-OPEN: ANY unexpected error (a malformed event, an input shlex can't tokenize despite
    # the guards) returns `allow`, never a block. This hook gates the human's ONLY comms channel
    # to the agent, so a crash must NEVER wedge a send — the only hard stops are the deliberate,
    # narrow BARE-body block and the removed-`question`-tag block; everything else, including
    # every error path, allows.
    try:
        event = json.load(sys.stdin)
        args = event.get("args") or {}
        command = args.get("command") or args.get("cmd") or event.get("command") or ""
        if not isinstance(command, str):
            command = str(command)
        cwd = str(event.get("cwd") or os.getcwd())
        decision, message, code = _decide(command, cwd)
        emit(decision, message)
        return code
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all: fail-open is the contract
        warn(f"could not inspect command: {exc} — allowing (fail-open)")
        return _allow()


if __name__ == "__main__":
    sys.exit(main())
