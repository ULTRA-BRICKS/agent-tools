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
The adaptive pressure (backoff.py) and the per-chat retry records live in the same file; the
retry, probe and telemetry helpers are in rotation_stats.py.

Past bugs it guards against: the confirmation wait was added to the interval instead of being
part of it (the draw is made once, at the START of a visit); a downtime or a cooldown must never
cause a catch-up burst (an overdue or overrun due time is an explicit `deferred` redraw from
"now"); a clock alone released a 429 hold although a fresh list 200 coexisted with new per-chat
429s; a backwards clock must not park the patrol for hours; a corrupt or foreign-account file
must refuse rather than be overwritten; and a half-written file must never replace a good one
(temp file, fsync, atomic replace); a burst of late 429s redrew the due time once per response,
an adaptive cooldown was followed by a second adaptive interval (the 429 retry is ONE deadline:
max(Retry-After, jittered adaptive pause, minimum spacing)), and a failed visit left no durable
per-chat retry. Only an overdue restart or an overrun is an explicit redraw from now.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import backoff, rotation_stats
from .backoff import (
    PENDING_PLATFORM_APPROVAL,
    PERMISSION_BLOCKED,
    BackoffConfig,
    BackoffError,
    Pressure,
    max_level,
)
from .lock import AccountBusy, account_lock
from .rotation_evidence import TRANSPORT_LIMIT, TransportEvidence, endpoint_ok
from .rotation_stats import FOREIGN_HOLD, RETRY_REASON_LIMIT, chat_uuid, endpoint_chat
from .urls import canonical_chat

SCHEMA = 2
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
COOLDOWN_SOURCES = ("retry-after", "default", "adaptive")
OWNED_URL_LIMIT = 64
ACCOUNTS = ("home", "glide.vc")
# The exact text live.TransportGuard writes for a transport 429; a test pins the two together.
MACHINE_429_HOLD = "transport HTTP 429; manual resume required"
HOLD_PENDING = "429 hold pending: needs verified same-endpoint success / existing authorized coordinator"
UNVERIFIED_SHOWN = 8

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
        "pressure",
        "backoff",
    }
)
_KEYS_V1 = _KEYS - {"pressure", "backoff"}
_ENTRY_KEYS_V1 = frozenset({"chat", "url", "visits", "last_visit_at", "last_outcome"})
_ENTRY_KEYS = _ENTRY_KEYS_V1 | {"retry", "last_ok_at", "confirmed_at"}
_RETRY_KEYS = frozenset({"at", "reason", "failures", "since"})
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
    # {"at", "reason", "failures", "since"} while a failed visit still awaits its retry.
    retry: dict | None = None
    last_ok_at: float | None = None  # last CLEAN visit; a failed visit never sets it
    confirmed_at: float | None = None  # last 2xx of this chat's own read endpoint

    def to_json(self) -> dict:
        return {
            "chat": self.chat,
            "url": self.url,
            "visits": self.visits,
            "last_visit_at": self.last_visit_at,
            "last_outcome": self.last_outcome,
            "retry": self.retry,
            "last_ok_at": self.last_ok_at,
            "confirmed_at": self.confirmed_at,
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


def _checked_retry(raw: object) -> dict | None:
    if raw is None:
        return None
    ok = (
        isinstance(raw, dict)
        and set(raw) == _RETRY_KEYS
        and _number(raw["at"])
        and isinstance(raw["reason"], str)
        and len(raw["reason"]) <= RETRY_REASON_LIMIT
        and _integer(raw["failures"], 1)
        and _number(raw["since"])
    )
    if not ok:
        raise RotationError("rotation retry record is invalid")
    return dict(raw)


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
        or not _optional_number(raw["last_ok_at"])
        or not _optional_number(raw["confirmed_at"])
    ):
        raise RotationError("rotation queue entry has invalid bookkeeping")
    return Entry(
        raw["chat"],
        url,
        raw["visits"],
        raw["last_visit_at"],
        raw["last_outcome"],
        _checked_retry(raw["retry"]),
        raw["last_ok_at"],
        raw["confirmed_at"],
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


def _evidence_map(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and len(raw) <= TRANSPORT_LIMIT
        and all(endpoint_ok(path) and _number(at) for path, at in raw.items())
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


def _migrate_v1(raw: dict) -> dict:
    """Schema 1 -> 2 in memory: every old field is copied unchanged; pressure starts at zero.

    Queue order, cursor, visit counts, Go record, owned URLs and evidence are untouched; the old
    shape is still checked strictly, so a corrupt v1 file refuses exactly as before.
    """
    if set(raw) != _KEYS_V1:
        raise RotationError("rotation state has unexpected fields")
    queue, cooldown = raw["queue"], raw["cooldown"]
    if not isinstance(queue, list):
        raise RotationError("rotation queue must be a list")
    if not all(isinstance(e, dict) and set(e) == _ENTRY_KEYS_V1 for e in queue):
        raise RotationError("rotation queue entry has unexpected fields")
    if cooldown is not None and not (
        isinstance(cooldown, dict) and set(cooldown) == _COOLDOWN_KEYS
    ):
        raise RotationError("rotation cooldown record is invalid")
    fresh = {"retry": None, "last_ok_at": None, "confirmed_at": None}
    return {
        **raw,
        "schema": SCHEMA,
        "pressure": Pressure().to_json(),
        "backoff": BackoffConfig().to_json(),
        "queue": [{**entry, **fresh} for entry in queue],
        "cooldown": cooldown,
    }


def _checked_pressure(raw: dict) -> tuple[BackoffConfig, Pressure]:
    try:
        cfg = BackoffConfig.from_json(raw["backoff"])
        return cfg, Pressure.from_json(raw["pressure"], cfg)
    except BackoffError as exc:
        raise RotationError(str(exc)) from exc


def validate(raw: object, account: str) -> dict:
    """Return the state with every field checked, or raise RotationError.

    A schema 1 file is migrated in memory (`migrated` is True); only `Rotation.open` persists it.
    """
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int:
        raise RotationError("rotation state has unexpected fields")
    if raw["schema"] not in (1, SCHEMA):
        raise RotationError("rotation state schema is not supported")
    migrated = raw["schema"] == 1
    if migrated:
        raw = _migrate_v1(raw)
    if set(raw) != _KEYS:
        raise RotationError("rotation state has unexpected fields")
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
    cfg, pressure = _checked_pressure(raw)
    return {
        **raw,
        "migrated": migrated,
        "queue": queue,
        "last_visit": _checked_visit(raw["last_visit"]),
        "go": _checked_go(raw["go"]),
        "cooldown": _checked_cooldown(raw["cooldown"]),
        "owned_urls": _checked_owned(raw["owned_urls"]),
        "tabs": _checked_tabs(raw["tabs"]),
        "visit_started_at": _checked_started(raw["visit_started_at"]),
        "deferred": _checked_deferred(raw["deferred"]),
        "transport": _checked_transport(raw["transport"]),
        "backoff": cfg,
        "pressure": pressure,
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


class Rotation(TransportEvidence):
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
        self.backoff: BackoffConfig = state.get("backoff") or BackoffConfig()
        self.pressure: Pressure = state.get("pressure") or Pressure()
        self.stale = state.get(
            "migrated", False
        )  # a migrated file is rewritten on open
        self.retrying = False  # the running visit retries a chat that carries a failure
        self._in_visit = False
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
        backoff: BackoffConfig | None = None,
    ) -> Rotation:
        """Load (validating schema and account) or start fresh, then settle the schedule.

        An explicit `backoff` replaces the stored settings (validated); without one the stored
        settings, or the conservative defaults, apply.
        """
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
        if backoff is not None:
            try:
                backoff = backoff.validated()
            except BackoffError as exc:
                raise RotationError(str(exc)) from exc
            if backoff != machine.backoff:
                machine.backoff = backoff
                machine.pressure.level = min(machine.pressure.level, max_level(backoff))
                machine.stale = True
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
        changed = self.fresh or self.stale
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
        self.fresh = self.stale = False

    def _base(self, now: float) -> float:
        """The earliest instant a fresh draw may count from: now, or the end of a cooldown."""
        return max(now, self.cooldown["until"]) if self.cooldown else now

    def _implausible_due(self, now: float) -> str:
        if self.next_due <= now:
            return "overdue"
        limit = self.effective_interval()[1]
        if self.pressure.transient_streak:
            limit = max(limit, self.backoff.transient_cap)
        if self.next_due > self._base(now) + limit:
            return "clock"
        return ""

    def effective_interval(self) -> list[float]:
        """The range a visit draw falls in now: the base interval or the delay window."""
        return backoff.effective_interval(
            self.pressure.level, self.interval, self.backoff
        )

    def draw(self) -> float:
        """One interval in seconds, uniform over [low, high], from the injected generator.

        Under pressure the visit spacing is at least the adaptive pause of the level, with the
        same draw, so the spacing never undercuts the jittered ladder.
        """
        low, high = self.interval
        u = self.rng.random()
        base = min(high, max(low, low + (high - low) * u))
        return max(base, backoff.adaptive_delay(self.pressure.level, self.backoff, u))

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
                "pressure": self.pressure.to_json(),
                "backoff": self.backoff.to_json(),
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
        """Hold until `until`, which then IS the next retry deadline (no second interval after it).

        The retry deadline is max(server Retry-After, jittered adaptive pause, minimum spacing):
        the caller already folded the first two into `until`, and `next_due` already carries the
        spacing draw of the visit start. A later or longer deadline extends both.
        """
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
        moved = until > self.next_due
        if moved:
            self.deferred = {"reason": "cooldown", "at": self.clock()}
            self.next_due = float(until)
        # A stream of 429s only moves the deadline later by a fraction of a second each time.
        self._persist_evidence(first or moved)

    def clear_hold(self) -> None:
        """The machine 429 hold is gone (released or never held): forget its cooldown and evidence."""
        self.cooldown = None
        self.transport = _no_evidence()
        self.save()

    def _defer(self, reason: str, base: float) -> None:
        """An explicit persisted deferral: one draw counted from `base`, flagged `reason`."""
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
        self,
        eligible: Callable[[str], bool],
        prefer: str | None = None,
        *,
        probe: bool = False,
    ) -> tuple[Entry, bool] | None:
        """The next entry to visit and whether it is the in-order choice (advances the cursor).

        A preferred chat (an owner-approved continuation) may jump the queue without moving the
        cursor, so it can never starve the rest of the lap. A chat whose retry is still in the
        future is skipped (kept, never dropped) and due retries alternate with cursor order. A
        `probe` (a check visit under a 429 hold) targets the known chat of an unverified
        endpoint on every other slot, oldest visit first; the slot after a failing chat goes to
        the ordinary cursor choice, so one failing chat never takes every read slot.
        """
        return rotation_stats.pick_entry(self, eligible, prefer, probe)

    def begin_visit(self, entry: Entry, probe: bool = False) -> None:
        """The visit starts now: this is the anchor, and the one draw of the visit is made here.

        The next visit is due `draw` seconds after this START, so the in-visit confirmation wait
        is part of the interval, never added to it. Persisted before any navigation, so a restart
        in the middle of the visit keeps the anchor. `probe` marks a controlled read-only
        retry (a check under a hold, or a chat carrying a failure) for the statistics.
        """
        now = self.clock()
        self.visit_started_at = now
        self.deferred = None
        self.current = entry.chat
        self.retrying = entry.retry is not None
        self._in_visit = True
        self.pressure.begin(probe)
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
        rotation_stats.settle_visit(self, entry, outcome, started, now)
        self._in_visit = False
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

    # -- adaptive pressure and the 429 hold ---------------------------------------------------

    def note_429(self, header: object = None) -> None:
        """A live 429 response: update the pressure and the cooldown deadline.

        The deadline is `max(adaptive, server)`: the adaptive pause of the ladder (one strike per
        failure episode, so a burst costs one level) and the real Retry-After, which is never
        shortened and never capped. A burst only ever extends the deadline by its server delay.
        """
        now = self.clock()
        cfg, pressure = self.backoff, self.pressure
        asked = backoff.retry_after_seconds(header, now)
        server_until = now + (max(0.0, asked) if asked is not None else 0.0)
        strike = pressure.strike_429(now, self.cooling(), cfg)
        if strike or not self.cooling():
            u = self.rng.random()
            adaptive_until = now + backoff.adaptive_delay(
                max(pressure.level, 1), cfg, u
            )
        else:
            adaptive_until = self.cooldown["until"]
        source = "retry-after" if server_until > adaptive_until else "adaptive"
        self.set_cooldown(
            max(adaptive_until, server_until), "http-429", source, MACHINE_429_HOLD
        )
        if strike:
            self._persist_evidence(True)

    def note_server_error(self) -> None:
        """A 5xx answered a request of the running visit: that visit counts as transient."""
        if self._in_visit:
            self.pressure.transient_issue = self.pressure.issued

    def note_target_success(self, path: object, at: float) -> None:
        """A 2xx of a chat's own read endpoint confirms that target (an unrelated 200 never does)."""
        found = endpoint_chat(path) if isinstance(path, str) else None
        if not found or not found[1]:
            return
        for entry in self.queue:
            if chat_uuid(entry.chat) == found[0]:
                entry.confirmed_at = at
                self.pressure.last_confirmed_at = at
                self._persist_evidence(False)
                return

    def adopt_legacy_hold(self) -> None:
        """A machine 429 hold with no evidence of ours (a legacy owner latched it).

        It is recorded as an explicitly unverifiable endpoint, never cleared by the clock, and
        read-only diagnostic probes continue at the cap cadence.
        """
        now = self.clock()
        self.note_restricted(FOREIGN_HOLD, now)
        self.set_cooldown(
            now + self.backoff.cap, "legacy-hold", "adaptive", MACHINE_429_HOLD
        )

    def rearm_probe(self) -> None:
        """A read-only probe saw neither a 429 nor the matching 2xx: pause again, no strike."""
        paths = self.unverified_paths()
        if paths and all(p == FOREIGN_HOLD for p in paths):
            delay = self.backoff.cap
        else:
            level = max(self.pressure.level, 1)
            delay = backoff.adaptive_delay(level, self.backoff, self.rng.random())
        self.set_cooldown(
            self.clock() + delay, "hold-recheck", "adaptive", MACHINE_429_HOLD
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

    def _row_state(self, entry: Entry, controls: Mapping[str, str]) -> tuple[str, str]:
        """(state, reason) of one queue row: an owner pause wins over a pending approval."""
        paused = pause_reason(controls, entry.chat)
        if paused:
            return "paused", paused
        if entry.last_outcome == PERMISSION_BLOCKED:
            return PERMISSION_BLOCKED, PENDING_PLATFORM_APPROVAL
        return ("current" if entry.chat == self.current else "queued"), ""

    def snapshot(self, controls: Mapping[str, str] | None = None) -> dict:
        """Operator view: current chat, queue size, last visit, next navigation, last Go."""
        controls = controls or {}
        now = self.clock()
        rows = []
        for entry in self.queue:
            state, reason = self._row_state(entry, controls)
            rows.append(
                {
                    "chat": entry.chat,
                    "state": state,
                    "reason": reason,
                    "current": entry.chat == self.current,
                    "visits": entry.visits,
                    "last_visit_at": entry.last_visit_at,
                    "last_outcome": entry.last_outcome,
                    "retry": entry.retry,
                    "last_ok_at": entry.last_ok_at,
                    "confirmed_at": entry.confirmed_at,
                }
            )
        excluded = [
            {"chat": row["chat"], "reason": row["reason"]}
            for row in rows
            if row["state"] == "paused"
        ]
        held = controls.get("stop:*") == MACHINE_429_HOLD
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
            "pressure": rotation_stats.pressure_view(self, held),
            "coverage": rotation_stats.coverage_view(self, excluded),
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
