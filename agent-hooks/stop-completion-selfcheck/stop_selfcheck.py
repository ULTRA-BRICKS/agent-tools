#!/usr/bin/env python3
"""agents-hooks/v1 stop hook — inject the completion self-check.

Fires when the agent is about to END its turn. It BLOCKS the stop exactly once,
returning a self-check prompt as the block message so the agent must run the check
before it can finish. A per-turn marker (keyed by the event's session id) prevents an
infinite stop→block→stop loop: the second time it sees the same session it allows the
stop.

This turns the `task-completion-selfcheck` ritual from advisory text into an enforced
prompt — the model is reliably nudged to (1) confirm everything was done and (2)
surface concrete follow-ups, instead of stopping at the first thing that worked.

Which prompt variant fires is picked from the turn's own transcript (best-effort — see
_classify_turn), not a single static string for every turn:

  - HEDGE   : the agent's own reply offered to do something ("I can check...", or its
              Russian equivalent "могу поискать" / "I can search") instead of doing it,
              and never actually used a tool this turn. A generic "did I finish
              everything" checklist doesn't catch this —
              it's not a missed step, it's a DEFERRED one. Quote the offer back and say
              "no, do it now."
  - LIGHT   : a pure text reply with no tool calls at all, and no hedge detected. The
              full engineering checklist (commits/push/deploy/cleanup) is noise here —
              asking it anyway teaches shallow "yep all done" pattern-completion that
              then carries over to turns where it matters. Ask one relevant question
              instead.
  - FULL    : the original checklist, for turns where real work happened (tool calls
              occurred) — unchanged from before this file learned to read transcripts.

Any transcript read/parse failure (missing path, bad JSON, unexpected shape) falls back
to FULL — this is a heuristic enhancement layered on a hook whose top-level contract is
fail-open; a parsing bug must never produce a worse or blocking-different outcome than
before, only silently lose the extra precision.

Cooldown (agent-tools#529): the marker used to be *deleted* the moment a stop was
allowed ("consumed so a later genuinely-new task re-prompts"). Scanning real session
transcripts (2026-09) showed the actual effect in a long-lived autonomous/watch-loop
session: every wake-up (ScheduleWakeup, a background-agent poll, ...) ends its own turn
with a Stop, so deleting the marker on the very next allowed stop meant the NEXT wake —
sometimes under a minute later — was treated as a brand-new session and re-blocked, over
and over, all day. Measured inter-firing gaps in real logs were as low as 5-70s (median
under 5 minutes in the busiest sessions), nowhere near the intended 30-minute TTL. The
fix is to stop deleting the marker on allow: a block now stays in effect for the full TTL
window, so at most one block fires per TTL rather than one per Stop event. A session that
starts a genuinely new task before the TTL expires simply doesn't get re-prompted until
it does — a real trade, but far better than the observed spam (see README "Cooldown").

Every decision (block/allow, and which prompt variant) is also appended as one line to a
`firings.jsonl` log (see `_log_firing`) — the foundation for measuring, over time, whether
a firing led to real behavior change or was a rubber stamp; nothing here reads that log,
it only writes it. A `SELFCHECK_DISABLE=1` env var or a `DISABLED` sentinel file in
`MARKER_DIR` unconditionally allows the stop with no marker/log writes — a fast, no-code-
change kill switch if this hook is ever misbehaving in a live session.

Contract (agents-hooks/v1):
  stdin  : JSON event; a session/turn id in event.event_id (or args.session_id); an
           optional args.transcript_path (CC-forwarded) pointing at the turn's JSONL log
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash must never trap the agent unable to stop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"


def warn(msg: str) -> None:
    sys.stderr.write(f"stop-selfcheck: {msg}\n")


def _env_int(name: str, default: int) -> int:
    """int(os.environ[name]) that can never crash the module at import time.

    A malformed value (a stray trailing comma from a shell export, an empty string) must
    not raise ValueError at import — that would exit non-zero before main() even runs,
    silently disabling the hook for the rest of the session via on_error=open.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        warn(f"{name}={raw!r} is not an int, using default {default}")
        return default


MARKER_DIR = Path(os.path.expanduser(os.environ.get(
    "SELFCHECK_MARKER_DIR", "~/.cache/agent-tools/selfcheck")))
# After this many seconds a marker is considered stale (a new task in the same session).
MARKER_TTL_S = max(1, _env_int("SELFCHECK_TTL_S", 1800))
# How far back to scan the transcript for the current turn. A turn rarely spans more than
# a few dozen tool round-trips; this is generous headroom, not a tuned limit. <= 0 disables
# the scan entirely (falls back to FULL_PROMPT, the pre-existing behavior).
TRANSCRIPT_TAIL_LINES = _env_int("SELFCHECK_TRANSCRIPT_TAIL_LINES", 500)
# Byte budget for the bounded tail read (see _read_tail_records) — bounds I/O/decode cost
# to this window regardless of total transcript file size, independent of TAIL_LINES.
TAIL_READ_BYTES = _env_int("SELFCHECK_TRANSCRIPT_TAIL_BYTES", 2_000_000)
# Where firing decisions are logged for later usefulness analysis (agent-tools#529).
FIRINGS_LOG = Path(os.path.expanduser(os.environ.get(
    "SELFCHECK_FIRINGS_LOG", str(MARKER_DIR / "firings.jsonl"))))
# Fast kill switch: set truthy, or drop a `DISABLED` file in MARKER_DIR, to unconditionally
# allow every stop with no marker/log writes — no code change or redeploy needed.
DISABLE_ENV = os.environ.get("SELFCHECK_DISABLE", "")

FULL_PROMPT = (
    "Before finishing, run the completion self-check:\n"
    "1. Did I finish EVERYTHING in the request? Walk back through every clause — "
    "code, commits, push, deploy, docs, cleanup, artifacts. What did I miss?\n"
    "2. Concrete follow-ups: any bug I noticed, improvement worth a tracked issue, "
    "or dead code to remove? State each as 'do X because Y', then do it or record it.\n"
    "Back any 'done' claim with evidence you actually produced (test output, a "
    "screenshot you looked at, a command's exit code) — 'it should work' is not "
    "'it works'. If, after this, everything is genuinely done, you may finish."
)

LIGHT_PROMPT = (
    "Before finishing, one check (this turn made no tool calls, so the full "
    "code/commit/deploy checklist doesn't apply): did you fully answer what was asked, "
    "or leave any part of it as a guess, a hedge, or something you could have just "
    "checked instead of speculating about? If there's a cheap, reversible way to turn a "
    "guess into a fact, do it now. If the answer is already complete and grounded, you "
    "may finish."
)

# Phrases where the model proposes doing something instead of doing it. Kept short and
# specific (not a general hedge-word list) to avoid false-positiving on legitimate cases
# where asking first is actually correct (e.g. before something destructive/irreversible —
# those turns say so, and won't also match "let me know if you'd like" filler).
HEDGE_PATTERNS = [
    r"могу\s+(поиска|провер|посмотреть|уточнить|глянуть)",
    r"если\s+хочешь.{0,30}(могу|поищ|провер)",
    r"хочешь[,]?\s+(чтобы\s+я|я\s+(поищу|проверю|посмотрю))",
    r"\bwant me to\b",
    r"\bi can (check|look into|search|dig into|investigate)\b",
    r"\bhappy to (check|look into|dig into|investigate)\b",
    r"\blet me know if you('d| would) like\b",
    r"\bshould i (check|look into|investigate)\b",
]
_HEDGE_RE = re.compile("|".join(HEDGE_PATTERNS), re.IGNORECASE)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def _event_args(event: dict) -> dict:
    """``event["args"]``, coerced to a dict — a malformed (non-dict) value must degrade to
    "no args" everywhere rather than crash whatever reads it next."""
    args = event.get("args")
    return args if isinstance(args, dict) else {}


def session_id(event: dict) -> str:
    args = _event_args(event)
    raw = (
        args.get("session_id")
        or event.get("event_id")
        or event.get("cwd")
        or "default"
    )
    return hashlib.sha256(str(raw).encode()).hexdigest()[:16]


def marker_file(sid: str) -> Path:
    return MARKER_DIR / f"{sid}.done"


def fresh(p: Path) -> bool:
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return False
    # A future-dated mtime (clock skew, a restored cache, a tampered/copied file) must
    # NOT read as "fresh" — since the marker is no longer consumed on allow, a future
    # mtime would otherwise suppress every self-check until wall-clock time caught up to
    # it, which could be indefinite. Treat it the same as absent/stale: re-block now.
    return 0 <= age <= MARKER_TTL_S


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _disabled() -> bool:
    """Kill switch: an env flag or a sentinel file, checked before any marker/log write."""
    if DISABLE_ENV.strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return (MARKER_DIR / "DISABLED").exists()
    except OSError:
        return False


def _sweep_stale_markers() -> None:
    """Opportunistic cleanup: remove ``*.done`` markers older than the TTL.

    The old consume-on-allow behavior was, incidentally, this catalog's only garbage
    collection for MARKER_DIR — removing it (agent-tools#529) meant every session that
    ever stops would otherwise leave a marker file behind forever. Called once per block
    (i.e. at most once per TTL window per session, not on every stop), so the extra
    directory listing is bounded and infrequent. Best-effort: any error here must not
    affect the block/allow decision.
    """
    try:
        now = time.time()
        for p in MARKER_DIR.glob("*.done"):
            try:
                if (now - p.stat().st_mtime) > MARKER_TTL_S:
                    p.unlink()
            except OSError:
                continue
    except Exception as exc:  # noqa: BLE001 - opportunistic cleanup must never crash the
        # hook: this is called after the block is already emitted (main()), but a crash
        # here still means a non-10 exit code, which on_error="open" resolves to allow —
        # silently discarding the block that was already written to stdout. Catching
        # broadly (not just OSError) is deliberate defense in depth, matching the
        # fail-open philosophy this module uses everywhere else (e.g. _select_prompt's
        # classification wrapper).
        warn(f"marker sweep failed (non-fatal): {exc}")


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _disabled() -> bool:
    """Kill switch: an env flag or a sentinel file, checked before any marker/log write."""
    if DISABLE_ENV.strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return (MARKER_DIR / "DISABLED").exists()
    except OSError:
        return False


def _log_firing(sid: str, decision: str, *, prompt_variant: str | None, hook_ms: float) -> None:
    """Append one JSONL row per invocation — data only, nothing here changes the decision.

    Best-effort and silent on failure: a logging problem must never affect whether the
    agent can stop (same fail-open contract as the rest of this hook). Rows are the raw
    material for a future usefulness-over-time report (agent-tools#529); this hook does
    not read them back.
    """
    row = {
        "ts": time.time(),
        "session": sid,
        "decision": decision,
        "prompt_variant": prompt_variant,
        "hook_ms": round(hook_ms, 2),
    }
    try:
        FIRINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FIRINGS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _read_tail_records(path: str, max_lines: int) -> list[dict]:
    """The last ``max_lines`` parseable JSON objects from a JSONL transcript.

    Best-effort: a bad line is skipped, not fatal — a partially-written last line (the
    transcript file can be mid-append when Stop fires) is a normal case, not corruption.

    Reads a BYTE-bounded window from the end of the file (seek + one bounded read), not
    the whole file. A naive ``deque(fh, maxlen=max_lines)`` still iterates and UTF-8-decodes
    every line of the file to fill the deque — only *retention* is bounded, not the I/O —
    which defeats the point on the exact workload this hook targets: a long-lived
    autonomous session's transcript can reach hundreds of MB, and CC JSONL lines can
    themselves be multi-MB (embedded tool output, screenshots). ``TAIL_READ_BYTES`` bounds
    both time and memory to the window, not the file size; ``max_lines`` is then applied on
    top of whatever that byte window happens to contain (fewer than ``max_lines`` records
    when lines are large — a best-effort trade, not a broken guarantee).

    ``max_lines <= 0`` means "scan nothing" — without this guard, ``list[-0:]`` silently
    means "the whole file", the opposite of a caller trying to disable/shrink the scan.
    (Unlike ``MARKER_TTL_S``, which is clamped away from 0 because 0 there means "never
    fresh, block forever" — a different knob with a different failure direction, so a
    different convention is fine and expected here.)
    """
    if max_lines <= 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        read_size = max(0, min(size, TAIL_READ_BYTES))
        fh.seek(size - read_size)
        chunk = fh.read(read_size)
    text = chunk.decode("utf-8", errors="replace")
    # split("\n"), NOT splitlines(): JSONL is newline-delimited, but splitlines() also
    # breaks on NEL/U+2028/U+2029/\v/\f — all legal *inside* a JSON string value (JSON only
    # requires escaping control chars < U+0020, and JS's JSON.stringify doesn't escape
    # NEL/U+2028/U+2029 either). A tool_result embedding one of those bytes would otherwise
    # get split into two JSON fragments, both failing json.loads below and silently
    # dropping a record — possibly the turn's only tool_use, misclassifying a worked turn
    # as LIGHT/HEDGE. `.strip()` on each line (below) already absorbs a trailing `\r`.
    lines = text.split("\n")
    if read_size < size and lines:
        # We seeked to an arbitrary byte offset, so the first line of the window may be a
        # partial line cut mid-record by our own seek (not the transcript's genuine
        # mid-append last line, which is always the LAST line, handled by the per-line
        # json.loads below). Drop it — the complete version of that record, if it matters,
        # sits further back and out of scope for a bounded tail read anyway.
        lines = lines[1:]
    lines = lines[-max_lines:]
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _message_content(record: dict) -> str | list | None:
    message = record.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return None


def _is_real_user_turn_boundary(record: dict, content) -> bool:
    """True for an actual human message; False for a synthetic/injected "user" record.

    CC (like the Anthropic API it's built on) represents a tool result as a role:"user"
    message whose content is entirely ``tool_result`` blocks — that's a continuation of
    the AGENT's turn, not a new human input. The same is true of CC's own ``isMeta``
    records (slash-command wrappers, injected system caveats) and ``isSidechain``
    records (subagent traffic living in the same transcript file) — neither is "what the
    user asked" either, so both must be skipped rather than mistaken for the turn
    boundary (a meta/sidechain record landing between an earlier tool call and this
    turn's text-only reply would otherwise truncate the scan and hide that tool call,
    misclassifying a worked turn as LIGHT/HEDGE).

    A ``content`` list can ALSO mix a ``tool_result`` block with a ``text`` block in the
    same record (the Anthropic message shape permits it, and CC can surface injected text —
    system-reminder style — as user text without setting ``isMeta``). Any ``tool_result``
    sibling proves the record is a turn continuation, not new human input, regardless of an
    accompanying text block — so that check runs BEFORE the text-presence check below.
    """
    if record.get("isMeta") or record.get("isSidechain"):
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(
            isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
            for b in content
        )
    return False


def _classify_turn(transcript_path: str) -> tuple[str, bool] | None:
    """(assistant_text, had_tool_use) for the turn since the last real user message.

    Returns ``None`` when the transcript can't be read/parsed at all — the caller falls
    back to FULL_PROMPT, the pre-existing behavior.
    """
    if not transcript_path:
        return None
    try:
        records = _read_tail_records(transcript_path, TRANSCRIPT_TAIL_LINES)
    except (OSError, ValueError):
        # OSError: unreadable/missing path (open/seek/read on the transcript). ValueError:
        # defensive — _read_tail_records opens in "rb" and decodes with errors="replace",
        # so it does not currently raise ValueError itself, but a future change to that
        # decoding (or to the seek/read arithmetic) raising one should still fail closed
        # here rather than escape to `_select_prompt`'s broader `except Exception` only by
        # accident. Catching both at the source keeps this function's contract
        # self-contained regardless of how _read_tail_records evolves.
        return None

    if not records:
        # Nothing to classify — either the scan is disabled (TRANSCRIPT_TAIL_LINES <= 0)
        # or the file has no parseable lines in the scanned window. Either way we have NO
        # signal, not a confirmed "empty, tool-free turn" — treat it the same as an
        # unreadable transcript (None → caller falls back to FULL) rather than defaulting
        # to LIGHT, which would make disabling the scan MORE aggressive than leaving it on.
        return None

    assistant_texts: list[str] = []
    had_tool_use = False
    found_boundary = False
    for record in reversed(records):
        rtype = record.get("type")
        content = _message_content(record)
        if rtype == "user":
            if _is_real_user_turn_boundary(record, content):
                found_boundary = True
                break
            continue  # synthetic/meta/sidechain user record — still part of this turn
        if rtype != "assistant":
            continue
        if isinstance(content, str):
            assistant_texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    assistant_texts.append(block["text"])
                elif btype == "tool_use":
                    had_tool_use = True

    if not found_boundary:
        # The reverse scan ran off the front of the scanned window without ever finding
        # the real user message that started this turn — either the turn genuinely spans
        # more than TRANSCRIPT_TAIL_LINES records, or the byte-bounded read in
        # _read_tail_records cut it off. Either way we can't be sure we saw the WHOLE
        # turn, so this could be a big worked turn whose earlier tool_use calls fell
        # outside the window — the same "no reliable signal" situation as an empty or
        # unreadable transcript. Fail closed to FULL rather than risk a false LIGHT/HEDGE
        # on what might be a heavy turn.
        return None

    assistant_text = "\n".join(reversed(assistant_texts))
    return assistant_text, had_tool_use


def _select_prompt(event: dict) -> tuple[str, str]:
    """(prompt text, variant name) — the variant name is for `_log_firing` only."""
    args = event.get("args")
    args = args if isinstance(args, dict) else {}
    transcript_path = args.get("transcript_path") or event.get("transcript_path")
    try:
        classified = _classify_turn(transcript_path) if transcript_path else None
    except Exception as exc:  # noqa: BLE001 - heuristic layer must never break the hook
        warn(f"transcript classification failed, falling back to full prompt: {exc}")
        classified = None

    if classified is None:
        return FULL_PROMPT, "full"

    assistant_text, had_tool_use = classified
    if had_tool_use:
        return FULL_PROMPT, "full"

    hedge_match = _HEDGE_RE.search(assistant_text) if assistant_text else None
    if hedge_match:
        quoted = assistant_text[hedge_match.start():hedge_match.end() + 40].strip()
        return (
            "Before finishing: you just offered an action instead of doing it "
            f"(\"{quoted}...\") and made no tool calls this turn. If that action is "
            "cheap, reversible, and read-only (checking a file, running a read-only "
            "command, searching), don't ask — do it now and answer with the result. "
            "Only stop short of it if it's genuinely destructive, ambiguous, or needs "
            "information only the user has."
        ), "hedge"

    return LIGHT_PROMPT, "light"


def main() -> int:
    start = time.monotonic()
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing stop (fail-open)")
        emit("allow")
        return 0

    if _disabled():
        emit("allow")
        return 0

    sid = session_id(event)
    marker = marker_file(sid)

    if fresh(marker):
        # Already blocked within the cooldown window (SELFCHECK_TTL_S) → let it stop.
        # The marker is intentionally NOT deleted here (agent-tools#529): deleting it on
        # every allowed stop meant a session that ends many short turns in a row (a
        # ScheduleWakeup/background-poll loop, for instance) got re-blocked on almost
        # every single one, since the very next stop again found no marker. Leaving the
        # marker in place caps re-blocking at once per TTL, at the cost of a genuinely new
        # task started before the TTL expires not getting an immediate fresh prompt.
        _log_firing(sid, "allow", prompt_variant=None, hook_ms=_elapsed_ms(start))
        emit("allow")
        return 0

    # First stop for this session (or the previous block's cooldown expired) → block once.
    # Select the prompt BEFORE writing the marker: if transcript classification raises or
    # times out (the descriptor's timeout_ms), the marker must not have been written —
    # otherwise a crash here would silently consume the whole cooldown window with no
    # block ever firing and no firings.jsonl row, the exact "worse than before" outcome
    # the module docstring rules out.
    prompt, variant = _select_prompt(event)

    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))
        _sweep_stale_markers()
    except OSError as exc:
        # Can't write a marker → don't risk a loop; allow the stop and just warn.
        warn(f"could not write marker {marker}: {exc} — allowing stop (fail-open)")
        emit("allow")
        return 0

    _log_firing(sid, "block", prompt_variant=variant, hook_ms=_elapsed_ms(start))
    emit("block", prompt)
    # Sweep AFTER emitting the block: this is opportunistic cleanup, not part of the
    # decision — it must not be able to affect whether the block fires (structurally, not
    # just because it currently self-contains its own errors) even if a future change to
    # _sweep_stale_markers raised something unexpected.
    _sweep_stale_markers()
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
