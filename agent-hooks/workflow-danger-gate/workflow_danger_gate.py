#!/usr/bin/env python3
"""agents-hooks/v1 pre-workflow hook — block destructive Workflow scripts.

Accessed via: Claude Code PreToolUse on the Workflow tool → cc_hook_bridge
              maps it to `pre-workflow` → this gate inspects the script
              (inline `args.script` or `args.scriptPath` on disk).

Assumptions: a Workflow we cannot inspect must not auto-run (fail-closed).
             JS comments and `prompt:` string literals are documentation, not
             execution — they must not trip the gate. Tagged templates and
             other expressions still evaluate, so they stay visible to the scan.

Why this exists (asks 129/130):
  The harness permissions.allow list includes `Workflow` so ultracode can
  spawn without a prompt. That allow is NOT a blank cheque. This gate is
  the script-level backstop for destructive commands the bash hooks already
  block (`gh pr merge`, `git reset --hard`, force-push, `sudo rm`,
  `--no-verify`, screencapture). Operators must be able to write comments
  explaining that split — a comment that *mentions* a denied command is
  allowed; the same command in executable JS is not.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# ECMA-262 LineTerminator. A `//` comment that only stops on `\n` would let
# U+2028 / U+2029 / CR hide the rest of the script from the scanner.
_LINE_TERMINATORS = frozenset("\n\r\u2028\u2029")

# Short-flag cluster containing `f` (`-f`, `-uf`, `-fu`, `-df`, `-fdx`, …).
# Single dash only — `--force` is handled separately so `--force-with-lease` lives.
_SHORT_F = r"(?<![-\w])-[a-zA-Z]*f[a-zA-Z]*(?![-\w])"
_SHORT_N = r"(?<![-\w])-[a-zA-Z]*n[a-zA-Z]*(?![-\w])"

# Each rule is (id, regex). The id is part of the block message so a test
# (and a human) can see WHICH form fired. `git -C <dir>` wrappers are allowed
# between `git` and the subcommand — the same shape the bash hooks peel.
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("raw-pr-merge", re.compile(r"\bgh\b[^\n;&|]*\spr\b[^\n;&|]*\smerge\b")),
    ("reset-hard", re.compile(r"\bgit\b[^\n;]*\sreset\b[^\n;]*\s--hard\b")),
    (
        "force-push",
        re.compile(
            r"\bgit\b[^\n;]*\spush\b[^\n;]*(?:--force(?!-with-lease)|" + _SHORT_F + r")"
        ),
    ),
    (
        "clean-force",
        re.compile(r"\bgit\b[^\n;]*\sclean\b[^\n;]*(?:--force|" + _SHORT_F + r")"),
    ),
    ("sudo-rm", re.compile(r"\bsudo\b[^\n;]*\brm\b")),
    (
        "no-verify",
        re.compile(
            r"(?:--no-verify\b|\bgit\b[^\n;]*\scommit\b[^\n;]*"
            + _SHORT_N
            + r")"
        ),
    ),
    ("screencapture", re.compile(r"\bscreencapture\b")),
]


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"workflow-danger-gate: {msg}\n")


def strip_js_comments(src: str) -> str:
    """Remove `//` line comments and `/* */` block comments. Strings and
    template literals are kept verbatim (a URL inside quotes is code, not a
    comment; a bun `$`...`` template is the execution surface we scan).
    """
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            i += 2
            while i < n and src[i] not in _LINE_TERMINATORS:
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] in _LINE_TERMINATORS:
                    out.append(src[i])
                i += 1
            i = i + 2 if i + 1 < n else n
            continue
        if c in ("'", '"', "`"):
            chunk, i = _consume_quoted(src, i)
            out.append(chunk)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _consume_quoted(src: str, i: int) -> tuple[str, int]:
    quote = src[i]
    out = [quote]
    i += 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            out.append(src[i : i + 2])
            i += 2
            continue
        out.append(ch)
        i += 1
        if ch == quote:
            break
    return "".join(out), i


def blank_prompt_strings(src: str) -> str:
    """Blank the contents of `prompt:` string/template literals.

    Ultracode Workflows pass instructions to subagents as prompt strings.
    Those prompts routinely *forbid* `gh pr merge` / hard reset. Blanking
    only the prompt literal means a mention inside a prompt cannot trip the
    gate, while a bun `$`...`` / exec(...) — including one sitting in the
    same `agent({...})` object as a sibling of `prompt` — still can.
    """
    chars = list(src)
    i = 0
    n = len(src)
    while i < n:
        if src[i] in ("'", '"', "`"):
            _, nxt = _consume_quoted(src, i)
            i = nxt
            continue
        if _is_ident(src, i, "prompt"):
            j = i + 6
            while j < n and src[j].isspace():
                j += 1
            if j < n and src[j] == ":":
                j += 1
                while j < n and src[j].isspace():
                    j += 1
                if j < n and src[j] in ("'", '"', "`"):
                    nxt = _blank_prompt_literal(chars, src, j)
                    if nxt is not None:
                        i = nxt
                        continue
        i += 1
    return "".join(chars)


def _blank_prompt_literal(chars: list[str], src: str, quote_i: int) -> int | None:
    """Blank a prompt string. For `...` templates, keep `${...}` interpolations
    visible — those expressions still evaluate while the prompt is built."""
    quote = src[quote_i]
    if quote != "`":
        end = _quoted_end(src, quote_i)
        if end is None:
            return None
        for k in range(quote_i + 1, end):
            if chars[k] not in _LINE_TERMINATORS:
                chars[k] = " "
        return end + 1
    i = quote_i + 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return i + 1
        if ch == "$" and i + 1 < n and src[i + 1] == "{":
            end = _matching_brace(src, i + 1)
            i = (end + 1) if end is not None else i + 2
            continue
        if ch not in _LINE_TERMINATORS:
            chars[i] = " "
        i += 1
    return n


def _matching_brace(src: str, open_i: int) -> int | None:
    """`open_i` points at `{`. Return the matching `}` index, skipping strings."""
    depth = 0
    i = open_i
    n = len(src)
    while i < n:
        c = src[i]
        if c in ("'", '"', "`"):
            _, i = _consume_quoted(src, i)
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return None


def _is_ident(src: str, i: int, name: str) -> bool:
    if not src.startswith(name, i):
        return False
    if i > 0 and (src[i - 1].isalnum() or src[i - 1] == "_"):
        return False
    after = i + len(name)
    if after < len(src) and (src[after].isalnum() or src[after] == "_"):
        return False
    return True


def _quoted_end(src: str, i: int) -> int | None:
    """Index of the closing quote, or None if unclosed."""
    quote = src[i]
    i += 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i
        i += 1
    return None


def find_danger(src: str) -> str | None:
    for rule_id, rx in RULES:
        if rx.search(src):
            return rule_id
    return None


def collapse_line_continuations(src: str) -> str:
    """Turn `\\\n` (and other JS line-continuations) into a space so
    `git push \\\n--force` still matches as one command."""
    return re.sub(r"\\[\n\r\u2028\u2029]+", " ", src)


def inspectable_source(src: str) -> str:
    return blank_prompt_strings(collapse_line_continuations(strip_js_comments(src)))


def script_from_event(event: dict) -> str | None:
    args = event.get("args")
    if not isinstance(args, dict):
        args = {}
    script = args.get("script")
    if isinstance(script, str) and script.strip():
        return script
    path = args.get("scriptPath") or args.get("script_path")
    if isinstance(path, str) and path.strip():
        pth = Path(path)
        if not pth.is_absolute():
            cwd = event.get("cwd") or args.get("cwd") or ""
            if cwd:
                pth = Path(cwd) / pth
        try:
            return pth.read_text(encoding="utf-8")
        except OSError as exc:
            warn(f"could not read scriptPath {pth}: {exc}")
            return None
    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"unreadable event: {exc}")
        emit(
            "block",
            "workflow-danger-gate: refusing a Workflow whose payload could not be parsed "
            "(fail-closed). Re-run with a valid script.",
        )
        return BLOCK_EXIT_CODE

    script = script_from_event(event)
    if script is None:
        emit(
            "block",
            "workflow-danger-gate: refusing a Workflow with no inspectable script "
            "(fail-closed). Pass args.script or a readable args.scriptPath.",
        )
        return BLOCK_EXIT_CODE

    rule = find_danger(inspectable_source(script))
    if rule is not None:
        emit(
            "block",
            "workflow-danger-gate: refusing a Workflow whose executable JS runs a "
            f"destructive command ({rule}). Ultracode agent() prompts and JS comments "
            "may mention these forms; the script itself may not execute them. Use gh "
            "ship instead of a raw PR merge; do not force-push, hard-reset, sudo rm, "
            "skip hooks, or screencapture from a Workflow.",
        )
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
