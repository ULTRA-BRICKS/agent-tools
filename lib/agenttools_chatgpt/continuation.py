"""Human-approved continuation: request ledger, operator policy and the one dispatch path.

A trusted local caller (pm) holds an owner approval for exactly one chat+turn and posts it
to the account-local control API. The API only validates and queues; the live loop is the
sole sender and re-validates the page immediately before the click. The ledger lives in the
same durable store as the recovery attempts, so at-most-once ownership of a (chat, turn)
survives restarts and simultaneous callers. Nothing here stores or exposes page text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .core import (
    OPERATOR_TURN_STATES,
    Guardian,
    MutationRefusal,
    Observation,
    Store,
    protected_dispatch,
)
from .urls import canonical_chat

MAX_APPROVAL_SECONDS = 900
FUTURE_SKEW_SECONDS = 30
MAX_ACTIVE_REQUESTS = 8
REFUSAL_RETENTION_SECONDS = 30 * 86400
MAX_REFUSED_RECEIPTS = 1000
_FLOAT_NOISE = 0.001
_TURN = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.IGNORECASE)
_FIELDS = ("request_id", "chat", "expected_turn", "approved_at", "expires_at")
# A turn is owned while a request for it can still lead to, or already led to, a click.
_OWNING = "('queued','dispatching','sent','uncertain')"
_COLUMNS = (
    "request_id,chat,turn,digest,approved_at,expires_at,state,reason,created_at,"
    "updated_at,attempt_wall,obs_model,obs_effort,obs_state"
)


class RequestError(ValueError):
    """The body is not exactly the documented approval; nothing is stored."""


class RequestConflict(Exception):
    """The request id exists with a different payload."""


class QueueFull(Exception):
    """Too many approvals are waiting for the live loop."""


@dataclass(frozen=True)
class ContinueRequest:
    request_id: str
    chat: str
    turn: str
    approved_at: float
    expires_at: float

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            [
                self.chat,
                self.turn,
                repr(float(self.approved_at)),
                repr(float(self.expires_at)),
            ]
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Record:
    request_id: str
    chat: str
    turn: str
    digest: str
    approved_at: float
    expires_at: float
    state: str
    reason: str
    created_at: float
    updated_at: float
    attempt_wall: float | None
    obs_model: str | None
    obs_effort: str | None
    obs_state: str | None


def _number(raw: object, name: str) -> float:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        raise RequestError(name + " must be a positive finite number")
    return float(raw)


def parse_request(raw: object) -> ContinueRequest:
    """Exactly the five documented fields; nothing a caller adds can grant authority."""
    if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
        raise RequestError("body must be exactly " + ", ".join(_FIELDS))
    request_id = raw["request_id"]
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except ValueError:
        raise RequestError("request_id must be a lowercase canonical UUID") from None
    try:
        chat = canonical_chat(raw["chat"])
    except ValueError:
        raise RequestError("chat must be an exact permanent conversation URL") from None
    turn = raw["expected_turn"]
    if not isinstance(turn, str) or not _TURN.fullmatch(turn):
        raise RequestError("expected_turn must be a turn UUID")
    approved_at = _number(raw["approved_at"], "approved_at")
    expires_at = _number(raw["expires_at"], "expires_at")
    if not 0 < expires_at - approved_at <= MAX_APPROVAL_SECONDS + _FLOAT_NOISE:
        raise RequestError("expires_at must be within 900 seconds after approved_at")
    return ContinueRequest(request_id, chat, turn, approved_at, expires_at)


def window_reason(approved_at: float, expires_at: float, now: float) -> str:
    if now > expires_at:
        return "approval-expired"
    if approved_at > now + FUTURE_SKEW_SECONDS:
        return "approval-in-future"
    return ""


def observation_refusal(chat: str, turn: str, observed: Observation) -> str:
    """Why a fresh observation may not be continued; empty when every gate passes."""
    if not (observed.chat and observed.turn and observed.generation):
        return "observation-unverified"
    if observed.chat != chat:
        return "chat-changed"
    if observed.turn != turn:
        return "turn-changed"
    if observed.running or observed.turn_state == "in_progress":
        return "running"
    if observed.draft:
        return "draft-present"
    if observed.challenge:
        return "challenge"
    if observed.ambiguous:
        return "ambiguous-page"
    if observed.quota or observed.status == "Sol quota exhausted":
        return "quota"
    if not observed.verified or not (
        observed.model == "Astra"
        or (observed.model == "Sol" and observed.effort == "Extra High")
    ):
        return "unverified-model-effort"
    if observed.turn_state not in OPERATOR_TURN_STATES:
        return "turn-state-unsupported"
    return ""


class Continuations:
    """The request ledger over the Guardian store. Every method is one short transaction."""

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _fetch(db: sqlite3.Connection, request_id: str) -> Record | None:
        row = db.execute(
            f"SELECT {_COLUMNS} FROM continuations WHERE request_id=?", (request_id,)
        ).fetchone()
        return Record(*row) if row else None

    def get(self, request_id: str) -> Record | None:
        with self.store.connect() as db:
            return self._fetch(db, request_id)

    @staticmethod
    def receipt_of(record: Record, now: float) -> dict:
        """Public receipt: metadata only, never page text, message bodies or credentials."""
        state, reason = record.state, record.reason
        if state == "queued" and now > record.expires_at:
            state, reason = "refused", "approval-expired"
        if state == "dispatching":
            reason = "dispatching"
        observed = (
            {
                "model": record.obs_model or "",
                "effort": record.obs_effort or "",
                "turn_state": record.obs_state or "",
            }
            if record.obs_state is not None
            else None
        )
        return {
            "request_id": record.request_id,
            "status": "queued" if state in ("queued", "dispatching") else state,
            "reason": reason,
            "chat": record.chat,
            "expected_turn": record.turn,
            "approved_at": record.approved_at,
            "expires_at": record.expires_at,
            "queued_at": record.created_at,
            "updated_at": record.updated_at,
            "sent_at": record.attempt_wall if state == "sent" else None,
            "observed": observed,
            "source": "operator",
        }

    def receipt(self, request_id: str, now: float) -> dict | None:
        record = self.get(request_id)
        return None if record is None else self.receipt_of(record, now)

    def submit(self, request: ContinueRequest, now: float) -> tuple[bool, dict]:
        """Validate admission and queue once. Returns (accepted, receipt)."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._fetch(db, request.request_id)
            if existing is not None:
                if existing.digest != request.digest:
                    raise RequestConflict(request.request_id)
                return existing.state != "refused", self.receipt_of(existing, now)
            # Refused receipts are bounded by age and count; owning rows are never pruned.
            db.execute(
                "DELETE FROM continuations WHERE state='refused' AND (updated_at<? OR "
                "request_id IN (SELECT request_id FROM continuations WHERE state='refused' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET ?))",
                (now - REFUSAL_RETENTION_SECONDS, MAX_REFUSED_RECEIPTS),
            )
            reason = window_reason(request.approved_at, request.expires_at, now)
            if (
                not reason
                and not db.execute(
                    "SELECT 1 FROM controls WHERE key=? AND value='1'",
                    ("watch:" + request.chat,),
                ).fetchone()
            ):
                reason = "not-enrolled"
            if (
                not reason
                and db.execute(
                    f"SELECT 1 FROM continuations WHERE chat=? AND turn=? AND state IN {_OWNING}",
                    (request.chat, request.turn),
                ).fetchone()
            ):
                reason = "turn-already-requested"
            if not reason:
                active = db.execute(
                    "SELECT COUNT(*) FROM continuations WHERE state IN ('queued','dispatching')"
                ).fetchone()[0]
                if active >= MAX_ACTIVE_REQUESTS:
                    raise QueueFull
            state = "refused" if reason else "queued"
            db.execute(
                "INSERT INTO continuations(request_id,chat,turn,digest,approved_at,expires_at,"
                "state,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    request.request_id,
                    request.chat,
                    request.turn,
                    request.digest,
                    request.approved_at,
                    request.expires_at,
                    state,
                    reason,
                    now,
                    now,
                ),
            )
            return not reason, self.receipt_of(self._fetch(db, request.request_id), now)

    def next_queued(self, now: float) -> Record | None:
        with self.store.connect() as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM continuations WHERE state='queued' AND expires_at>=? "
                "ORDER BY created_at, rowid LIMIT 1",
                (now,),
            ).fetchone()
        return Record(*row) if row else None

    def expire(self, now: float) -> int:
        """Queued approvals past their expiry are refused without any browser work."""
        with self.store.connect() as db:
            return db.execute(
                "UPDATE continuations SET state='refused',reason='approval-expired',updated_at=? "
                "WHERE state='queued' AND expires_at<?",
                (now, now),
            ).rowcount

    def recover_interrupted(self) -> int:
        """A restart cannot know whether an in-flight click happened; it never retries one.

        The matching attempt stays `reserved`, which keeps blocking the chat until a human
        resolves it through the existing resolve-send acknowledgement.
        """
        with self.store.connect() as db:
            return db.execute(
                "UPDATE continuations SET state='uncertain',reason='restart-interrupted',"
                "updated_at=? WHERE state='dispatching'",
                (time.time(),),
            ).rowcount

    def refuse(self, request_id: str, reason: str) -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE continuations SET state='refused',reason=?,updated_at=? "
                "WHERE request_id=? AND state='queued'",
                (reason, time.time(), request_id),
            )

    def reserve(self, record: Record, observed: Observation, wall: float) -> str:
        """Atomically own the exact turn: ledger row to `dispatching`, attempt to `reserved`."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._fetch(db, record.request_id)
            if row is None or row.state != "queued":
                return "not-queued"
            reason = window_reason(row.approved_at, row.expires_at, wall)
            if not reason and (observed.chat, observed.turn) != (row.chat, row.turn):
                reason = "turn-changed"
            if not reason:
                reason = self.store.reserve_in(
                    db, observed, wall, self.store.operator_blocked
                )
            if reason == "reserved":
                db.execute(
                    "UPDATE continuations SET state='dispatching',reason='',updated_at=?,"
                    "attempt_wall=?,obs_model=?,obs_effort=?,obs_state=? WHERE request_id=?",
                    (
                        time.time(),
                        wall,
                        observed.model,
                        observed.effort,
                        observed.turn_state,
                        record.request_id,
                    ),
                )
                return "reserved"
            db.execute(
                "UPDATE continuations SET state='refused',reason=?,updated_at=? "
                "WHERE request_id=?",
                (reason, time.time(), record.request_id),
            )
            return reason

    def guard(
        self,
        db: sqlite3.Connection,
        request_id: str,
        observed: Observation,
        wall: float,
    ) -> str:
        """Last gate inside the dispatch transaction, immediately before the click."""
        row = self._fetch(db, request_id)
        if row is None or row.state != "dispatching":
            return "not-dispatching"
        own = db.execute(
            "SELECT state FROM attempts WHERE chat=? AND turn=? AND generation=?",
            observed.identity,
        ).fetchone()
        if not own or own[0] != "reserved":
            return "not-reserved"
        return window_reason(row.approved_at, row.expires_at, wall) or (
            self.store.operator_blocked(observed, db, own_reservation=True)
        )

    def release_in(
        self,
        db: sqlite3.Connection,
        request_id: str,
        observed: Observation,
        reason: str,
    ) -> None:
        """Nothing was dispatched: refuse the request but keep its consumed attempt.

        The reservation already consumed the turn identity and the cooldown/hourly/dedup
        budget. A 'canceled' attempt does not block the chat as an unresolved send (only
        'reserved' and 'uncertain' do) but can never make the turn retryable.
        """
        db.execute(
            "UPDATE attempts SET state='canceled' WHERE chat=? AND turn=? AND generation=? AND state='reserved'",
            observed.identity,
        )
        db.execute(
            "UPDATE continuations SET state='refused',reason=?,updated_at=? "
            "WHERE request_id=? AND state='dispatching'",
            (reason, time.time(), request_id),
        )

    def release(self, request_id: str, observed: Observation, reason: str) -> None:
        with self.store.connect() as db:
            self.release_in(db, request_id, observed, reason)

    def finish(
        self, request_id: str, observed: Observation, acknowledged: bool, reason: str
    ) -> None:
        """The click was invoked: record the real outcome in both ledgers, never retryable."""
        state = "sent" if acknowledged else "uncertain"
        with self.store.connect() as db:
            db.execute(
                "UPDATE attempts SET state=? WHERE chat=? AND turn=? AND generation=?",
                (state, *observed.identity),
            )
            db.execute(
                "UPDATE continuations SET state=?,reason=?,updated_at=? WHERE request_id=?",
                (state, reason, time.time(), request_id),
            )


async def dispatch_continuation(
    guardian: Guardian,
    record: Record,
    adapter,
    *,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """The only code path that may click for an approval; returns a reason code.

    An approval authorizes exactly one `go` on the approved turn: a refusal never switches
    models, clears quota, navigates or retries, and an invoked click is never repeated.
    """
    store = guardian.store
    ledger = Continuations(store)
    current = await asyncio.to_thread(ledger.get, record.request_id)
    if current is None or current.state != "queued":
        return "not-queued"
    record = current

    async def refuse(reason: str) -> str:
        await asyncio.to_thread(ledger.refuse, record.request_id, reason)
        return reason

    reason = window_reason(record.approved_at, record.expires_at, clock())
    if reason:
        return await refuse(reason)
    observed = await adapter.observe()
    captured = monotonic()
    await asyncio.to_thread(guardian.record_safety, observed)
    prepare = getattr(adapter, "prepare_view", None)
    if not observed.turn and callable(prepare):
        # Unreadable view: one bounded, approval-scoped preparation, then a fresh observation.
        reason = window_reason(record.approved_at, record.expires_at, clock())
        if reason:
            return await refuse(reason)
        if not await prepare(record.chat, record.turn, record.expires_at):
            return await refuse("view-not-ready")
        reason = window_reason(record.approved_at, record.expires_at, clock())
        if reason:
            return await refuse(reason)
        observed = await adapter.observe()
        captured = monotonic()
        await asyncio.to_thread(guardian.record_safety, observed)
    reason = observation_refusal(record.chat, record.turn, observed)
    if reason:
        return await refuse(reason)
    result = await asyncio.to_thread(ledger.reserve, record, observed, clock())
    if result != "reserved":
        return result
    invoked = False
    try:
        latest = await adapter.observe()
        await asyncio.to_thread(guardian.record_safety, latest)
        reason = ""
        if latest != observed or not latest.operator_ready():
            reason = "changed-before-send"
        elif not 0 <= monotonic() - captured <= 6:
            reason = "stale-observation"
        if reason:
            await asyncio.to_thread(ledger.release, record.request_id, observed, reason)
            return reason
        acknowledged: bool | MutationRefusal = False
        async with store.async_guard() as db:
            reason = ledger.guard(db, record.request_id, observed, clock())
            if reason:
                ledger.release_in(db, record.request_id, observed, reason)
            else:

                async def send_and_record():
                    outcome = await adapter.send(observed)
                    if isinstance(outcome, MutationRefusal):
                        guardian.record_safety(outcome.observation, db)
                    return outcome

                invoked = True
                acknowledged = await protected_dispatch(send_and_record())
        if reason:
            return reason
        await asyncio.to_thread(
            ledger.finish,
            record.request_id,
            observed,
            bool(acknowledged),
            "acknowledged" if acknowledged else "unacknowledged",
        )
        return "sent" if acknowledged else "uncertain-send"
    except BaseException:
        if invoked:
            await asyncio.to_thread(
                ledger.finish, record.request_id, observed, False, "interrupted"
            )
        else:
            await asyncio.to_thread(
                ledger.release, record.request_id, observed, "interrupted-before-send"
            )
        raise
