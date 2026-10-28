"""Rotation state of the single-tab patrol: queue, cursor, current target, next due, last visit.

Accessed via: `chatgpt-guardian live --single-tab` (patrol.py owns the loop and calls this file's
state machine), the legacy `live` launch (cli.py calls `guard_legacy_launch`, the only place that
may retire the `MODE_CONTROL` marker) and the telemetry view (rotation_view.py reads a snapshot).

Assumptions: one file per account, next to that account's recovery database, so the account
scope is the file's own `account` field and is validated on every load. Time is wall-clock
seconds from an injected clock; randomness comes from an injected generator, so tests are
deterministic and the two accounts never share a seed. The state machine never touches a
browser or the network; only `retire_mode`/`guard_legacy_launch` use the recovery Store's
controls and the per-account lock. The 429 evidence ledger holds what the browser's own
responses showed, per exact endpoint path; it is the only basis for releasing a machine hold.

Past bugs it guards against: the confirmation wait was added to the interval instead of being
part of it (the draw is made once, at the START of a visit); a downtime or a cooldown must never
cause a catch-up burst (an overdue or overrun due time is an explicit `deferred` redraw from
"now"); a clock alone released a 429 hold although a fresh list 200 coexisted with new per-chat
429s; a backwards clock must not park the patrol for hours; a corrupt or foreign-account file
must refuse rather than be overwritten; and a half-written file must never replace a good one
(temp file, fsync, atomic replace).
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .lock import AccountBusy, account_lock
from .urls import canonical_chat

SCHEMA = 1
# Durable marker in the account's recovery controls: set by the patrol once it holds the account
# lock, shown by the telemetry, and the reason a legacy `live` launch is refused. The lock itself
# (not this marker) is what stops coverage/startup restore-all while a patrol runs.
MODE_CONTROL = "mode:single-tab"
DEFAULT_INTERVAL = (240.0, 360.0)
INTERVAL_FLOOR = 120.0
INTERVAL_CEILING = 900.0
CONFIRM_WAIT_RANGE = (20.0, 60.0)
DEFAULT_CONFIRM_WAIT = 45.0
GO_STATES = ("sending", "sent", "uncertain", "interrupted")
CONFIRMATIONS = ("unconfirmed", "running", "completed")
COOLDOWN_SOURCES = ("retry-after", "default")
OWNED_URL_LIMIT = 64
ACCOUNTS = ("home", "glide.vc")
# The exact text live.TransportGuard writes for a transport 429; a test pins the two together.
MACHINE_429_HOLD = "transport HTTP 429; manual resume required"
HOLD_PENDING = "429 hold pending: needs verified same-endpoint success / existing authorized coordinator"
TRANSPORT_LIMIT = 128
ENDPOINT_PATTERN = re.compile(r"/[A-Za-z0-9._~%/-]{1,200}")
UNVERIFIED_SHOWN = 8
# A repeat of an already-recorded transport state (a stream of 401s, a polling endpoint that
# keeps succeeding) is persisted at most this often; every transition is persisted at once.
EVIDENCE_SAVE_GAP = 1.0

_KEYS = frozenset(
    {
        "schema",
        "account",
        "interval",
        "queue",
        "cursor",
        "current",
        "next_due",
        "last_visit",
        "go",
        "cooldown",
        "owned_urls",
        "tabs",
        "visit_started_at",
        "deferred",
        "transport",
    }
)
_ENTRY_KEYS = frozenset({"chat", "url", "visits", "last_visit_at", "last_outcome"})
_GO_KEYS = frozenset(
    {
        "chat",
        "turn",
        "state",
        "started_at",
        "sent_at",
        "confirmation",
        "response_turn",
        "confirmed_at",
    }
)
_COOLDOWN_KEYS = frozenset({"until", "reason", "source", "hold"})
_VISIT_KEYS = frozenset({"chat", "at", "started_at", "outcome"})
_TABS_KEYS = frozenset({"owned", "protected", "at"})
_DEFERRED_KEYS = frozenset({"reason", "at"})
_TRANSPORT_KEYS = frozenset({"restricted", "succeeded", "blocked_at"})


class RotationError(ValueError):
    """The rotation state or its configuration is unsafe; nothing may be sent."""


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def validate_interval(low: object, high: object) -> tuple[float, float]:
    """Navigation interval bounds in seconds; refuses anything outside the safe range."""
    if not (_number(low) and _number(high)):
        raise RotationError("interval bounds must be finite numbers")
    if not INTERVAL_FLOOR <= low <= high <= INTERVAL_CEILING:
        raise RotationError(
            f"interval must satisfy {INTERVAL_FLOOR:g} <= min <= max <= {INTERVAL_CEILING:g} seconds"
        )
    return float(low), float(high)


def validate_confirm_wait(value: object) -> float:
    """In-visit confirmation wait in seconds, 20 to 60."""
    low, high = CONFIRM_WAIT_RANGE
    if not _number(value) or not low <= value <= high:
        raise RotationError(f"confirmation wait must be {low:g} to {high:g} seconds")
    return float(value)


def rotation_path(state: Path) -> Path:
    """The account's rotation file, beside its recovery database."""
    state = Path(state)
    return state.with_name(state.stem + ".rotation.json")


def permanent_url(raw: object) -> str:
    """A full permanent conversation URL: query and fragment dropped, project path kept."""
    if not isinstance(raw, str):
        raise RotationError("conversation URL must be text")
    parts = urlsplit(raw)
    url = urlunsplit(parts._replace(query="", fragment="")).rstrip("/")
    try:
        canonical_chat(url)
    except ValueError as exc:
        raise RotationError("not a permanent conversation URL") from exc
    return url


def pause_reason(controls: Mapping[str, str], chat: str) -> str:
    """Why a chat is excluded from visits: a durable per-chat or global stop, else empty."""
    for key in ("stop:" + chat, "stop:*"):
        if key in controls:
            return controls[key] or "stopped"
    return ""


def retire_mode(store) -> None:
    """Clear the single-tab marker; any recovery Store (`connect()`) will do.

    Only `guard_legacy_launch` calls this, and only while it holds the account lock.
    """
    with store.connect() as db:
        db.execute("DELETE FROM controls WHERE key=?", (MODE_CONTROL,))


def guard_legacy_launch(store, *, retire: bool) -> None:
    """Gate of the legacy multi-tab `live` launch while the single-tab mode is recorded.

    A legacy launch never touches the marker implicitly: it is refused while the mode is
    recorded, so an accidental or competing launch that then loses its owner lock can never
    have deleted the marker. A deliberate rollback (`retire`) works only while it holds the
    same per-account lock the running patrol holds for its whole life; with a patrol alive
    the lock is busy and the launch is refused with the marker intact.
    """
    if store.controls().get(MODE_CONTROL) != "1":
        return
    try:
        with account_lock(store.path):
            if store.controls().get(MODE_CONTROL) != "1":
                return
            if not retire:
                raise RotationError(
                    "single-tab mode is recorded for this account: the legacy multi-tab "
                    "launch is refused; start with --single-tab, or roll back deliberately "
                    "with --retire-single-tab while no patrol is running"
                )
            retire_mode(store)
    except AccountBusy:
        raise RotationError(
            "the single-tab patrol (or another applied Guardian run) holds the account "
            "lock: the legacy launch is refused and the single-tab mode is untouched"
        ) from None


@dataclass
class Entry:
    chat: str
    url: str
    visits: int = 0
    last_visit_at: float | None = None
    last_outcome: str = ""

    def to_json(self) -> dict:
        return {
            "chat": self.chat,
            "url": self.url,
            "visits": self.visits,
            "last_visit_at": self.last_visit_at,
            "last_outcome": self.last_outcome,
        }


def _valid_chat(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return canonical_chat(value) == value
    except ValueError:
        return False


def _optional_number(value: object) -> bool:
    return value is None or _number(value)


def _entry(raw: object) -> Entry:
    if not isinstance(raw, dict) or set(raw) != _ENTRY_KEYS:
        raise RotationError("rotation queue entry has unexpected fields")
    url = raw["url"]
    try:
        if not _valid_chat(raw["chat"]) or canonical_chat(url) != raw["chat"]:
            raise ValueError
    except (ValueError, TypeError):
        raise RotationError("rotation queue entry is not one conversation") from None
    if (
        not _integer(raw["visits"])
        or not _optional_number(raw["last_visit_at"])
        or not isinstance(raw["last_outcome"], str)
    ):
        raise RotationError("rotation queue entry has invalid bookkeeping")
    return Entry(
        raw["chat"], url, raw["visits"], raw["last_visit_at"], raw["last_outcome"]
    )


def _queue(raw: object, cursor: object) -> list[Entry]:
    if not isinstance(raw, list):
        raise RotationError("rotation queue must be a list")
    queue = [_entry(item) for item in raw]
    if len({entry.chat for entry in queue}) != len(queue):
        raise RotationError("rotation queue repeats a conversation")
    if not _integer(cursor) or cursor >= max(len(queue), 1):
        raise RotationError("rotation cursor is outside the queue")
    return queue


def _checked_go(raw: object) -> dict | None:
    if raw is None:
        return None
    ok = (
        isinstance(raw, dict)
        and set(raw) == _GO_KEYS
        and _valid_chat(raw["chat"])
        and isinstance(raw["turn"], str)
        and raw["state"] in GO_STATES
        and raw["confirmation"] in CONFIRMATIONS
        and isinstance(raw["response_turn"], str)
        and _number(raw["started_at"])
        and _optional_number(raw["sent_at"])
        and _optional_number(raw["confirmed_at"])
    )
    if not ok:
        raise RotationError("rotation Go record is invalid")
    return dict(raw)


def _checked_cooldown(raw: object) -> dict | None:
    if raw is None:
        return None
    ok = (
        isinstance(raw, dict)
        and set(raw) == _COOLDOWN_KEYS
        and _number(raw["until"])
        and isinstance(raw["reason"], str)
        and raw["source"] in COOLDOWN_SOURCES
        and isinstance(raw["hold"], str)
    )
    if not ok:
        raise RotationError("rotation cooldown record is invalid")
    return dict(raw)


def _checked_visit(raw: object) -> dict | None:
    if raw is None:
        return None
    ok = (
        isinstance(raw, dict)
        and set(raw) == _VISIT_KEYS
        and (raw["chat"] is None or _valid_chat(raw["chat"]))
        and _number(raw["at"])
        and _optional_number(raw["started_at"])
        and isinstance(raw["outcome"], str)
    )
    if not ok:
        raise RotationError("rotation last-visit record is invalid")
    return dict(raw)


def _checked_deferred(raw: object) -> dict | None:
    if raw is None:
        return None
    ok = (
        isinstance(raw, dict)
        and set(raw) == _DEFERRED_KEYS
        and isinstance(raw["reason"], str)
        and _number(raw["at"])
    )
    if not ok:
        raise RotationError("rotation deferral record is invalid")
    return dict(raw)


def _checked_started(raw: object) -> float | None:
    if not _optional_number(raw):
        raise RotationError("rotation visit start is invalid")
    return raw


def _endpoint_ok(path: object) -> bool:
    return isinstance(path, str) and ENDPOINT_PATTERN.fullmatch(path) is not None


def _evidence_map(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and len(raw) <= TRANSPORT_LIMIT
        and all(_endpoint_ok(path) and _number(at) for path, at in raw.items())
    )


def _checked_transport(raw: object) -> dict:
    ok = (
        isinstance(raw, dict)
        and set(raw) == _TRANSPORT_KEYS
        and _optional_number(raw["blocked_at"])
        and _evidence_map(raw["restricted"])
        and _evidence_map(raw["succeeded"])
    )
    if not ok:
        raise RotationError("rotation transport evidence is invalid")
    return {
        "restricted": dict(raw["restricted"]),
        "succeeded": dict(raw["succeeded"]),
        "blocked_at": raw["blocked_at"],
    }


def _no_evidence() -> dict:
    return {"restricted": {}, "succeeded": {}, "blocked_at": None}


def _checked_tabs(raw: object) -> dict:
    ok = (
        isinstance(raw, dict)
        and set(raw) == _TABS_KEYS
        and _integer(raw["owned"])
        and isinstance(raw["protected"], list)
        and all(_valid_chat(chat) for chat in raw["protected"])
        and _optional_number(raw["at"])
    )
    if not ok:
        raise RotationError("rotation tab report is invalid")
    return dict(raw)


def _checked_owned(raw: object) -> list[str]:
    if not isinstance(raw, list) or len(raw) > OWNED_URL_LIMIT:
        raise RotationError("rotation owned-URL list is invalid")
    for url in raw:
        permanent_url(url)
    return list(raw)


def validate(raw: object, account: str) -> dict:
    """Return the state with every field checked, or raise RotationError."""
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise RotationError("rotation state has unexpected fields")
    if type(raw["schema"]) is not int or raw["schema"] != SCHEMA:
        raise RotationError("rotation state schema is not supported")
    if raw["account"] != account:
        raise RotationError("rotation state belongs to a different account")
    interval = raw["interval"]
    if not isinstance(interval, list) or len(interval) != 2:
        raise RotationError("rotation interval is invalid")
    validate_interval(*interval)
    queue = _queue(raw["queue"], raw["cursor"])
    if raw["current"] is not None and not _valid_chat(raw["current"]):
        raise RotationError("rotation current target is not a conversation")
    if not _number(raw["next_due"]):
        raise RotationError("rotation due time is invalid")
    return {
        **raw,
        "queue": queue,
        "last_visit": _checked_visit(raw["last_visit"]),
        "go": _checked_go(raw["go"]),
        "cooldown": _checked_cooldown(raw["cooldown"]),
        "owned_urls": _checked_owned(raw["owned_urls"]),
        "tabs": _checked_tabs(raw["tabs"]),
        "visit_started_at": _checked_started(raw["visit_started_at"]),
        "deferred": _checked_deferred(raw["deferred"]),
        "transport": _checked_transport(raw["transport"]),
    }


def read_state(path: Path, account: str) -> dict | None:
    """The validated state, or None when no file exists. Never creates or repairs a file."""
    if path.is_symlink():
        raise RotationError("rotation state must not be a symlink")
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RotationError(
            "rotation state is unreadable: " + type(exc).__name__
        ) from exc
    return validate(raw, account)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, payload: dict) -> None:
    """Temp file in the same directory, fsync, atomic replace: a crash keeps the old file."""
    data = json.dumps(payload, sort_keys=True, allow_nan=False, indent=1) + "\n"
    temp = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
    with suppress(FileNotFoundError):
        temp.unlink()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            temp.unlink()
        raise
    _sync_directory(path.parent)


def _permanent_map(wanted: Mapping[str, str]) -> dict[str, str]:
    urls = {}
    for chat, url in wanted.items():
        if not _valid_chat(chat):
            raise RotationError("queue identity must be a canonical conversation")
        urls[chat] = permanent_url(url)
    return urls


def _upgrade_urls(entries: list[Entry], urls: Mapping[str, str]) -> None:
    """A richer (project) URL replaces the stored one; a canonical-only sighting never does."""
    for entry in entries:
        seen = urls[entry.chat]
        if seen != entry.chat and seen != entry.url:
            entry.url = seen


class Rotation:
    """Persisted patrol schedule for one account. Every mutator saves atomically."""

    def __init__(
        self,
        path: Path,
        account: str,
        interval: tuple[float, float],
        clock: Callable[[], float],
        rng,
        state: dict | None,
    ):
        self.path = Path(path)
        self.account = account
        self.interval = interval
        self.clock = clock
        self.rng = rng
        state = state or {}
        self.fresh = not state
        self.queue: list[Entry] = state.get("queue", [])
        self.cursor: int = state.get("cursor", 0)
        self.current: str | None = state.get("current")
        self.next_due: float = state.get("next_due", 0.0)
        self.last_visit: dict | None = state.get("last_visit")
        self.go: dict | None = state.get("go")
        self.cooldown: dict | None = state.get("cooldown")
        self.owned_urls: list[str] = state.get("owned_urls", [])
        self.tabs: dict = state.get("tabs", {"owned": 0, "protected": [], "at": None})
        self.visit_started_at: float | None = state.get("visit_started_at")
        self.deferred: dict | None = state.get("deferred")
        self.transport: dict = state.get("transport") or _no_evidence()
        self._evidence_saved = -math.inf

    @classmethod
    def open(
        cls,
        path: Path,
        account: str,
        *,
        interval: tuple[float, float] = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.time,
        rng=None,
    ) -> Rotation:
        """Load (validating schema and account) or start fresh, then settle the schedule."""
        if account not in ACCOUNTS:
            raise RotationError("account must be home or glide.vc")
        low, high = validate_interval(*interval)
        path = Path(path)
        machine = cls(
            path,
            account,
            (low, high),
            clock,
            rng if rng is not None else random.SystemRandom(),
            read_state(path, account),
        )
        machine._settle()
        return machine

    @classmethod
    def read(
        cls, path: Path, account: str, *, clock: Callable[[], float] = time.time
    ) -> Rotation | None:
        """A read-only view of an existing file, or None when there is none.

        Unlike `open` it never settles the schedule and never writes: telemetry must not move
        the patrol's due time or repair a file the owner has to look at.
        """
        state = read_state(Path(path), account)
        if state is None:
            return None
        low, high = state["interval"]
        return cls(Path(path), account, (float(low), float(high)), clock, None, state)

    def _settle(self) -> None:
        """Interrupted sends stay unresolved; an overdue or implausible due time is redrawn once.

        The redraw is an explicit `deferred` state counted from now (or from the end of a running
        cooldown), never a catch-up burst. A first start is not a deferral.
        """
        changed = self.fresh
        if self.go and self.go["state"] == "sending":
            self.go["state"] = "interrupted"
            changed = True
        now = self.clock()
        reason = "" if self.fresh else self._implausible_due(now)
        if self.fresh or reason:
            self.next_due = self._base(now) + self.draw()
            if reason:
                self.deferred = {"reason": reason, "at": now}
            changed = True
        if changed:
            self.save()
        self.fresh = False

    def _base(self, now: float) -> float:
        """The earliest instant a fresh draw may count from: now, or the end of a cooldown."""
        return max(now, self.cooldown["until"]) if self.cooldown else now

    def _implausible_due(self, now: float) -> str:
        if self.next_due <= now:
            return "overdue"
        if self.next_due > self._base(now) + self.interval[1]:
            return "clock"
        return ""

    def draw(self) -> float:
        """One interval in seconds, uniform over [low, high], from the injected generator."""
        low, high = self.interval
        return min(high, max(low, low + (high - low) * self.rng.random()))

    def save(self) -> None:
        write_atomic(
            self.path,
            {
                "schema": SCHEMA,
                "account": self.account,
                "interval": list(self.interval),
                "queue": [entry.to_json() for entry in self.queue],
                "cursor": self.cursor,
                "current": self.current,
                "next_due": self.next_due,
                "last_visit": self.last_visit,
                "go": self.go,
                "cooldown": self.cooldown,
                "owned_urls": self.owned_urls,
                "tabs": self.tabs,
                "visit_started_at": self.visit_started_at,
                "deferred": self.deferred,
                "transport": self.transport,
            },
        )

    # -- schedule -----------------------------------------------------------------------------

    def cooling(self) -> bool:
        return self.cooldown is not None and self.clock() < self.cooldown["until"]

    def due_at(self) -> float:
        """The visit time. A cooldown ends before it: set_cooldown re-bases it when it does not."""
        until = self.cooldown["until"] if self.cooldown else 0.0
        return max(self.next_due, until)

    def is_due(self) -> bool:
        return self.clock() >= self.due_at()

    def set_cooldown(self, until: float, reason: str, source: str, hold: str) -> None:
        if not _number(until) or source not in COOLDOWN_SOURCES:
            raise RotationError("cooldown needs a finite deadline and a known source")
        if self.cooldown and self.cooldown["until"] >= until:
            return
        first = self.cooldown is None
        self.cooldown = {
            "until": float(until),
            "reason": reason,
            "source": source,
            "hold": hold,
        }
        rebased = until >= self.next_due
        if rebased:
            # The cooldown outlasts the draw of the running visit: never due the instant it ends.
            self._defer("cooldown", float(until))
        # A stream of 429s only moves the deadline later by a fraction of a second each time.
        self._persist_evidence(first or rebased)

    def clear_hold(self) -> None:
        """The machine 429 hold is gone (released or never held): forget its cooldown and evidence."""
        self.cooldown = None
        self.transport = _no_evidence()
        self.save()

    def _defer(self, reason: str, base: float) -> None:
        """An explicit persisted deferral: one fresh draw counted from `base`, flagged `reason`."""
        self.deferred = {"reason": reason, "at": self.clock()}
        self.next_due = base + self.draw()

    # -- queue --------------------------------------------------------------------------------

    def sync(self, wanted: Mapping[str, str]) -> bool:
        """Make the queue exactly the wanted chats, keeping order, cursor and stored URLs.

        `wanted` maps canonical chat identity to a permanent URL. New chats join at the end; a
        canonical-only sighting never replaces a stored full project URL.
        """
        urls = _permanent_map(wanted)
        before = [(e.chat, e.url) for e in self.queue]
        kept = [entry for entry in self.queue if entry.chat in urls]
        dropped_before = sum(
            entry.chat not in urls for entry in self.queue[: self.cursor]
        )
        _upgrade_urls(kept, urls)
        known = {entry.chat for entry in kept}
        kept += [Entry(chat, urls[chat]) for chat in sorted(set(urls) - known)]
        self.queue = kept
        self.cursor = self.cursor - dropped_before
        if self.cursor >= len(kept):
            self.cursor = 0
        changed = before != [(e.chat, e.url) for e in self.queue]
        if changed:
            self.save()
        return changed

    def pick(
        self, eligible: Callable[[str], bool], prefer: str | None = None
    ) -> tuple[Entry, bool] | None:
        """The next entry to visit and whether it is the in-order choice (advances the cursor).

        A preferred chat (an owner-approved continuation) may jump the queue without moving the
        cursor, so it can never starve the rest of the lap.
        """
        count = len(self.queue)
        first = next(
            (
                self.queue[(self.cursor + offset) % count]
                for offset in range(count)
                if eligible(self.queue[(self.cursor + offset) % count].chat)
            ),
            None,
        )
        if first is None:
            return None
        jump = next(
            (e for e in self.queue if e.chat == prefer and eligible(e.chat)), None
        )
        if jump is not None and jump is not first:
            return jump, False
        return first, True

    def begin_visit(self, entry: Entry) -> None:
        """The visit starts now: this is the anchor, and the one draw of the visit is made here.

        The next visit is due `draw` seconds after this START, so the in-visit confirmation wait
        is part of the interval, never added to it. Persisted before any navigation, so a restart
        in the middle of the visit keeps the anchor.
        """
        now = self.clock()
        self.visit_started_at = now
        self.deferred = None
        self.current = entry.chat
        self.next_due = now + self.draw()
        self.save()

    def end_visit(self, entry: Entry, outcome: str, advance: bool) -> None:
        """Record the visit. The due time is never redrawn here, except for a genuine overrun."""
        now = self.clock()
        entry.visits += 1
        entry.last_visit_at = now
        entry.last_outcome = outcome
        started = self.visit_started_at if self.visit_started_at is not None else now
        self.last_visit = {
            "chat": entry.chat,
            "at": now,
            "started_at": started,
            "outcome": outcome,
        }
        self.current = entry.chat
        if advance:
            self.cursor = (self.queue.index(entry) + 1) % len(self.queue)
        if self.next_due <= now:
            self._defer("overrun", self._base(now))
        self.save()

    def idle(self, outcome: str) -> None:
        """A due slot with nothing visitable (empty or fully paused queue): wait one interval."""
        now = self.clock()
        self.last_visit = {
            "chat": None,
            "at": now,
            "started_at": None,
            "outcome": outcome,
        }
        self.deferred = None
        self.next_due = self._base(now) + self.draw()
        self.save()

    def defer(self, reason: str, outcome: str) -> None:
        """A due visit that may not start (a protected tab): nothing navigated, nothing rushed.

        The slot is an explicit persisted deferral; the next attempt is one fresh draw from now.
        """
        now = self.clock()
        self.last_visit = {
            "chat": None,
            "at": now,
            "started_at": None,
            "outcome": outcome,
        }
        self._defer(reason, self._base(now))
        self.save()

    # -- 429 transport evidence ---------------------------------------------------------------
    #
    # The ledger of what the browser's own responses showed, per exact endpoint path. It is the
    # only evidence on which the machine 429 hold may ever be released: no clock, no DOM, no
    # probe of our own.

    def note_restricted(self, path: object, at: float) -> None:
        """A 429 for `path` at `at`. An endpoint that cannot be tracked exactly blocks release."""
        t = self.transport
        restricted = t["restricted"]
        if not _endpoint_ok(path):
            self.note_blocked(at)
            return
        full = path not in restricted and len(restricted) >= TRANSPORT_LIMIT
        if full and not self._evict_verified():
            self.note_blocked(at)
            return
        if at > restricted.get(path, -math.inf):
            fresh = path not in restricted
            voids = t["succeeded"].get(path, -math.inf) > restricted.get(
                path, -math.inf
            )
            restricted[path] = at
            self._persist_evidence(fresh or voids)

    def _evict_verified(self) -> bool:
        """Make room by forgetting the oldest endpoint that already has a later success."""
        t = self.transport
        done = [
            p
            for p, at in t["restricted"].items()
            if t["succeeded"].get(p, -math.inf) > at
        ]
        if not done:
            return False
        oldest = min(done, key=lambda p: t["succeeded"][p])
        del t["restricted"][oldest]
        t["succeeded"].pop(oldest, None)
        return True

    def note_success(self, path: object, at: float) -> None:
        """A 2xx for `path`: counted only for an endpoint that was restricted, and only if later."""
        t = self.transport
        if not _endpoint_ok(path) or path not in t["restricted"]:
            return
        done = t["succeeded"].get(path, -math.inf)
        if at > t["restricted"][path] and at > done:
            first = done <= t["restricted"][path]
            t["succeeded"][path] = at
            self._persist_evidence(first)

    def note_blocked(self, at: float) -> None:
        """A newer 401/403/quota (or an untrackable 429) voids every older success."""
        t = self.transport
        blocked = t["blocked_at"]
        if blocked is None or at > blocked:
            voids = blocked is None or any(
                ok > blocked for ok in t["succeeded"].values()
            )
            t["blocked_at"] = at
            self._persist_evidence(voids)

    def _persist_evidence(self, transition: bool) -> None:
        """Save now for a transition; a repeat of a recorded state rides on the next save."""
        now = self.clock()
        if transition or now - self._evidence_saved >= EVIDENCE_SAVE_GAP:
            self._evidence_saved = now
            self.save()

    def hold_unverified(self) -> str:
        """Why the machine 429 hold may not be released now; empty only when it is verified.

        Verified means: the cooldown (the real Retry-After) is over, every endpoint that was
        restricted has a strictly later success of the SAME exact path, and no auth/quota signal
        is newer than any of those successes. An elapsed clock alone is never enough.
        """
        t = self.transport
        if not t["restricted"]:
            return "no-restriction-evidence"
        if self.cooling():
            return "cooldown"
        if self.unverified_paths():
            return "unverified"
        blocked = t["blocked_at"]
        if blocked is not None and any(
            t["succeeded"][path] <= blocked for path in t["restricted"]
        ):
            return "blocked-newer"
        return ""

    def unverified_paths(self) -> list[str]:
        t = self.transport
        return sorted(
            p
            for p, at in t["restricted"].items()
            if t["succeeded"].get(p, -math.inf) <= at
        )

    # -- Go record ----------------------------------------------------------------------------

    def begin_go(self, chat: str, turn: str) -> None:
        """Persisted BEFORE the click, so a crash can never look like 'nothing happened'."""
        self.go = {
            "chat": chat,
            "turn": turn,
            "state": "sending",
            "started_at": self.clock(),
            "sent_at": None,
            "confirmation": "unconfirmed",
            "response_turn": "",
            "confirmed_at": None,
        }
        self.save()

    def finish_go(self, state: str) -> None:
        if self.go is None or state not in GO_STATES:
            return
        self.go["state"] = state
        if state == "sent":
            self.go["sent_at"] = self.clock()
        self.save()

    def confirm_go(self, confirmation: str, response_turn: str = "") -> None:
        """Correlate the Go with the next assistant response (running or completed)."""
        if self.go is None or confirmation not in CONFIRMATIONS:
            return
        self.go["confirmation"] = confirmation
        if confirmation != "unconfirmed":
            self.go["response_turn"] = response_turn
            self.go["confirmed_at"] = self.clock()
        self.save()

    # -- tabs ---------------------------------------------------------------------------------

    def record_owned(self, urls: list[str]) -> None:
        """Remember every owned conversation URL, newest last, before any tab is closed."""
        known = [u for u in self.owned_urls if u not in urls]
        merged = (known + [permanent_url(u) for u in urls])[-OWNED_URL_LIMIT:]
        if merged != self.owned_urls:
            self.owned_urls = merged
            self.save()

    def record_tabs(self, owned: int, protected: list[str]) -> None:
        report = {"owned": owned, "protected": sorted(protected), "at": self.clock()}
        if (report["owned"], report["protected"]) != (
            self.tabs["owned"],
            self.tabs["protected"],
        ):
            self.tabs = report
            self.save()

    # -- telemetry ----------------------------------------------------------------------------

    def snapshot(self, controls: Mapping[str, str] | None = None) -> dict:
        """Operator view: current chat, queue size, last visit, next navigation, last Go."""
        controls = controls or {}
        now = self.clock()
        rows = []
        for entry in self.queue:
            paused = pause_reason(controls, entry.chat)
            rows.append(
                {
                    "chat": entry.chat,
                    "state": "paused"
                    if paused
                    else "current"
                    if entry.chat == self.current
                    else "queued",
                    "reason": paused,
                    "current": entry.chat == self.current,
                    "visits": entry.visits,
                    "last_visit_at": entry.last_visit_at,
                    "last_outcome": entry.last_outcome,
                }
            )
        due = self.due_at()
        return {
            "mode": "single-tab",
            "account": self.account,
            "queue_size": len(self.queue),
            "current": self.current,
            "last_visit": self.last_visit,
            "next_navigation_at": due,
            "next_navigation_in": max(0.0, due - now),
            "deferred": self.deferred,
            "interval": list(self.interval),
            "last_go": self.go,
            "cooldown": self.cooldown,
            "transport": self._transport_view(controls),
            "tabs": self.tabs,
            "queue": rows,
        }

    def _transport_view(self, controls: Mapping[str, str]) -> dict:
        t = self.transport
        pending = self.unverified_paths()
        held = controls.get("stop:*") == MACHINE_429_HOLD
        return {
            "hold": HOLD_PENDING if held else "",
            "restricted": len(t["restricted"]),
            "verified": len(t["restricted"]) - len(pending),
            "unverified": pending[:UNVERIFIED_SHOWN],
            "blocked_at": t["blocked_at"],
        }
