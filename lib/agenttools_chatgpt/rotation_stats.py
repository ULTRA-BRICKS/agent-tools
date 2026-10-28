"""Retry selection, visit accounting and pressure telemetry of the single-tab rotation.

Accessed via: rotation.py, whose `Rotation` calls these helpers (they read and mutate its queue,
`pressure`, `backoff`, `cooldown` and `next_due` and never touch a file, a clock of their own, a
browser or the network; the machine passes its injected clock and generator).

Assumptions: a chat id is the UUID at the end of its canonical URL, and a read endpoint of one
chat is `/backend-api/conversation[s]/<uuid>`; a failed visit keeps its chat enrolled with a
durable `retry` record and can never set `last_ok_at` (a failed visit is not fresh, successful or
done); due retries alternate with cursor order so one failing chat uses at most every second slot.

Past bugs it guards against: the check visit under a 429 hold took the next chat in cursor order
instead of the chat whose endpoint was unverified; a failed visit advanced the cursor with no
durable per-chat retry; a failed chat could starve the others or be silently dropped; the hold
probe re-picked the same failing chat every slot (A,A,A); and a DOM-only "complete" counted as a
clean visit with no transport evidence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from . import backoff

# A legacy machine hold that carries no endpoint evidence is recorded as this pseudo endpoint: no
# response ever targets it, so it stays explicitly unverified and the clock never clears it.
FOREIGN_HOLD = "/hold-without-evidence"
RETRY_REASON_LIMIT = 80
# One chat's read endpoint. The bare form (no suffix) is the chat's own read and confirms the
# target; a suffixed form still maps an unverified path back to its chat.
CHAT_ENDPOINT = re.compile(
    r"/backend-api/conversations?/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(/[A-Za-z0-9._~%/-]{0,150})?"
)


def chat_uuid(chat: str) -> str:
    return chat.rsplit("/", 1)[1]


def endpoint_chat(path: str) -> tuple[str, bool] | None:
    """(chat uuid, is the bare read endpoint) for a chat read path, else None."""
    match = CHAT_ENDPOINT.fullmatch(path)
    return None if match is None else (match.group(1), match.group(2) is None)


def probe_target(machine, eligible: Callable[[str], bool]):
    """The known chat of an unverified read endpoint, oldest visit first, else None."""
    wanted = set()
    for path in machine.unverified_paths():
        found = endpoint_chat(path)
        if found:
            wanted.add(found[0])
    candidates = [
        entry
        for entry in machine.queue
        if chat_uuid(entry.chat) in wanted and eligible(entry.chat)
    ]
    return min(
        candidates,
        key=lambda e: -math.inf if e.last_visit_at is None else e.last_visit_at,
        default=None,
    )


def _usable(machine, eligible: Callable[[str], bool]) -> list:
    """Eligible chats in cursor order whose own retry is not still in the future."""
    count = len(machine.queue)
    now = machine.clock()
    ordered = [machine.queue[(machine.cursor + step) % count] for step in range(count)]
    return [
        e for e in ordered if eligible(e.chat) and not (e.retry and e.retry["at"] > now)
    ]


def _first_usable(machine, usable: list):
    """(entry, advance): the ordinary cursor chat, or the oldest due retry on a retry's turn."""
    others = [e for e in usable if e.retry is None]
    due = [e for e in usable if e.retry is not None]
    in_order = others[0] if others else usable[0]
    if machine.pressure.retry_turn:
        first = in_order
    elif due:
        first = min(due, key=lambda e: e.retry["at"])
    else:
        first = usable[0]
    return first, first is in_order or first is usable[0]


def pick_entry(machine, eligible, prefer, probe: bool):
    """(entry, in_order) for the next visit, or None. See Rotation.pick."""
    if not machine.queue:
        return None
    if probe:
        target = probe_target(machine, eligible)
        # Alternate with the ordinary cursor check: a retry turn means the previous slot already
        # went to a failing chat, so the others get this one and no single chat takes every slot.
        if target is not None and not machine.pressure.retry_turn:
            return target, False
    usable = _usable(machine, eligible)
    if not usable:
        return None
    first, advance = _first_usable(machine, usable)
    jump = next(
        (e for e in machine.queue if e.chat == prefer and eligible(e.chat)), None
    )
    if jump is not None and jump is not first:
        return jump, False
    return first, advance


def _fail(entry, reason: str, at: float, now: float) -> None:
    prior = entry.retry
    entry.retry = {
        "at": float(at),
        "reason": reason[:RETRY_REASON_LIMIT],
        "failures": (prior["failures"] + 1) if prior else 1,
        "since": prior["since"] if prior else now,
    }


def visit_kind(machine, entry, outcome: str, started: float) -> str:
    """429, transient, neutral (no signal) or ok for one finished visit.

    Transport health and task completion are separate: "ok" needs a fresh 2xx of THIS chat's own
    bare read endpoint seen in the current visit (`confirmed_at >= started`, set by the response
    hook). A complete/draft/unknown/failed:* DOM outcome with such a 2xx is transport-ok; without
    it the visit is neutral (inconclusive): no pressure change, no success-rate sample, the retry
    stays and `last_ok_at` is not set. An unrelated 200 (avatar, list, another chat) never counts.
    """
    p = machine.pressure
    kind = backoff.outcome_kind(outcome)
    if p.last_429_at is not None and p.last_429_at >= started:
        kind = "429"
    # A blocked visit is neutral, but a real 5xx of its own requests still counts (429 wins).
    blocked = kind == "neutral" and outcome == backoff.PERMISSION_BLOCKED
    if (kind == "unproven" or blocked) and p.transient_issue == p.issued:
        kind = "transient"  # a 5xx answered one of this visit's own requests
    if kind == "unproven":
        proven = entry.confirmed_at is not None and entry.confirmed_at >= started
        kind = "ok" if proven else "neutral"
    return kind


def settle_visit(machine, entry, outcome: str, started: float, now: float) -> None:
    """Account one finished visit: pressure recovery, per-chat retry and account-wide slowdown."""
    p, cfg = machine.pressure, machine.backoff
    kind = visit_kind(machine, entry, outcome, started)
    p.record(kind, cfg)
    if kind == "429":
        until = max(machine.cooldown["until"], now) if machine.cooldown else now
        _fail(entry, "http-429", until, now)
    elif kind == "transient":
        u = machine.rng.random()
        named = backoff.outcome_kind(outcome) == "transient"
        failures = (entry.retry["failures"] if entry.retry else 0) + 1
        delay = backoff.transient_delay(failures, cfg, u)
        _fail(entry, outcome if named else "http-5xx", now + delay, now)
        if p.transient_streak >= 2:
            push = now + backoff.transient_delay(p.transient_streak - 1, cfg, u)
            if push > machine.next_due:
                machine.next_due = push
                machine.deferred = {"reason": "transient", "at": now}
    elif kind == "ok":
        entry.retry = None
        entry.last_ok_at = now
    p.retry_turn = kind in ("429", "transient") or machine.retrying


def hold_state(machine, held: bool) -> str:
    """Where a machine 429 hold stands: none, cooling, waiting for its probe, or unverifiable."""
    if not held:
        return "none"
    paths = machine.unverified_paths()
    if FOREIGN_HOLD in paths or not machine.transport["restricted"]:
        return "legacy-unverifiable"
    if machine.cooling():
        return "cooling"
    return "unverified-pending-probe" if paths else "verified-pending-release"


def pressure_view(machine, held: bool) -> dict:
    p, cfg = machine.pressure, machine.backoff
    pending = [e for e in machine.queue if e.retry]
    soonest = min(pending, key=lambda e: e.retry["at"], default=None)
    return {
        "level": p.level,
        "max_level": backoff.max_level(cfg),
        "strikes": p.strikes,
        "effective_interval": machine.effective_interval(),
        "failure_streak": p.transient_streak,
        "clean_streak": p.clean_streak,
        "last_429_at": p.last_429_at,
        "retry_at": soonest.retry["at"] if soonest else None,
        "retry_reason": soonest.retry["reason"] if soonest else "",
        "next_due": machine.next_due,
        "due_at": machine.due_at(),
        "last_confirmed_target_success_at": p.last_confirmed_at,
        "controlled_probes": p.probes,
        "responses_429": p.responses_429,
        "recent": p.recent_counts(),
        "success_rate": p.success_rate(),
        "hold_state": hold_state(machine, held),
        "config": cfg.to_json(),
    }


def coverage_view(machine, excluded: list[dict]) -> dict:
    """Honest coverage: a registered chat is not a checked chat. Exclusions stay explicit."""
    now = machine.clock()
    queue = machine.queue
    stamps = [e.last_ok_at for e in queue if e.last_ok_at is not None]
    pending = [e for e in queue if e.retry]
    never = [e for e in queue if e.last_ok_at is None]
    return {
        "enrolled": len(queue),
        "never_clean": len(never),
        "oldest_unvisited_age": max(0.0, now - min(stamps)) if stamps else None,
        "retry_pending": len(pending),
        "next_retry_at": min((e.retry["at"] for e in pending), default=None),
        "remaining_in_pass": len(queue) - machine.cursor if queue else 0,
        "excluded": excluded,
        "complete": bool(queue) and not pending and not never and not excluded,
    }
