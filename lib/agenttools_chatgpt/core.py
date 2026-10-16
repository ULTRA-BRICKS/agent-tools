"""Guardian policy reached from CLI/HA; SQLite intents precede irreversible UI sends.

A database is shared only across sessions of one account. Stability is process-local and
resets on restart; intents and human/quota stops persist without a retry lease.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from .urls import canonical_chat

DispatchResult = TypeVar("DispatchResult")


async def protected_dispatch(operation: Awaitable[DispatchResult]) -> DispatchResult:
    """Keep the caller's dispatch lock until browser execution conclusively ends.

    Canceling a Playwright await does not abort Chromium JavaScript. Timeout and
    cancellation therefore drain the shielded operation, even on repeated cancel.
    A hung renderer intentionally retains the lock and requires operator recovery.
    """
    pending = asyncio.ensure_future(operation)
    try:
        return await asyncio.wait_for(asyncio.shield(pending), timeout=5)
    except BaseException:
        drained = asyncio.gather(pending, return_exceptions=True)
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                continue
        # gather retrieves browser exceptions without replacing the original
        # timeout/cancellation or acknowledging a late successful send.
        drained.result()
        raise


@dataclass(frozen=True)
class Observation:
    chat: str
    turn: str
    generation: str
    status: str = ""
    active: bool = False
    verified: bool = False
    model: str = ""
    effort: str = ""
    running: bool = False
    draft: bool = False
    challenge: bool = False
    ambiguous: bool = True
    quota: bool = False

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.chat, self.turn, self.generation

    def safe_failure(self) -> bool:
        return (
            bool(self.chat and self.turn and self.generation)
            and self.status == "Thinking failed"
            and self.active
            and self.verified
            and not any((self.running, self.draft, self.challenge, self.ambiguous))
        )


@dataclass(frozen=True)
class MutationRefusal:
    observation: Observation

    def __bool__(self) -> bool:
        return False


class Adapter(Protocol):
    async def observe(self) -> Observation: ...
    async def send(self, expected: Observation) -> bool | MutationRefusal: ...


class Store:
    def __init__(self, path: Path, *, account: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = self.path.exists()
        if account is not None:
            if account not in ("home", "glide.vc"):
                raise ValueError("Account must be home or glide.vc")
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "account_scope" not in tables:
                    if existed or tables:
                        raise ValueError(
                            "Unscoped state requires offline account reconciliation; refusing silent migration"
                        )
                    db.execute("CREATE TABLE account_scope(account TEXT PRIMARY KEY)")
                    db.execute("INSERT INTO account_scope VALUES(?)", (account,))
                if db.execute("SELECT account FROM account_scope").fetchall() != [
                    (account,)
                ]:
                    raise ValueError(
                        "State belongs to a different account; choose that workspace or a separate state file"
                    )
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS controls(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS consent(chat TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS attempts(
                    chat TEXT, turn TEXT, generation TEXT, wall REAL NOT NULL,
                    state TEXT NOT NULL, PRIMARY KEY(chat,turn,generation));
                CREATE INDEX IF NOT EXISTS attempts_wall ON attempts(wall DESC);
                CREATE INDEX IF NOT EXISTS attempts_chat_wall ON attempts(chat,wall);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self, timeout: float = 10) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=timeout)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def set_control(
        self, key: str, value: str, db: sqlite3.Connection | None = None
    ) -> None:
        if db is not None:
            db.execute("INSERT OR REPLACE INTO controls VALUES(?,?)", (key, value))
            return
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO controls VALUES(?,?)", (key, value)
            )

    def enroll(self, chat: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO controls VALUES(?, '1')", ("watch:" + chat,)
            )

    def _api_allowed(
        self, db: sqlite3.Connection, chat: str, model: str, effort: str, verified: bool
    ) -> bool:
        stopped = db.execute(
            "SELECT 1 FROM controls WHERE key IN ('stop:*',?)", ("stop:" + chat,)
        ).fetchone()
        unresolved = db.execute(
            "SELECT 1 FROM attempts WHERE chat=? AND state IN ('reserved','uncertain')",
            (chat,),
        ).fetchone()
        quota = db.execute("SELECT 1 FROM controls WHERE key='astra_quota'").fetchone()
        sol_allowed = (
            chat.startswith("https://chatgpt.com/c/")
            and verified
            and model == "Sol"
            and effort == "Extra High"
            and bool(
                db.execute("SELECT 1 FROM consent WHERE chat=?", (chat,)).fetchone()
            )
        )
        return (
            not stopped
            and not unresolved
            and (not quota or sol_allowed)
            and (model != "Sol" or sol_allowed)
        )

    @contextmanager
    def dispatch_api(
        self, chat: str, model: str = "", effort: str = "", verified: bool = False
    ):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield self._api_allowed(db, chat, model, effort, verified)

    async def _acquire(self, db: sqlite3.Connection) -> None:
        started = time.monotonic()
        while True:
            try:
                db.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                if (
                    not any(word in str(error).lower() for word in ("locked", "busy"))
                    or time.monotonic() - started >= 5
                ):
                    raise
                await asyncio.sleep(0.02)

    @asynccontextmanager
    async def async_guard(self) -> AsyncIterator[sqlite3.Connection]:
        # Browser refusals and cancellation must commit known safety evidence before
        # another worker can authorize a mutation. Only safety writes belong here.
        with self.connect(timeout=0) as db:
            await self._acquire(db)
            try:
                yield db
            finally:
                db.commit()

    @asynccontextmanager
    async def async_dispatch_api(
        self, chat: str, model: str = "", effort: str = "", verified: bool = False
    ) -> AsyncIterator[bool]:
        # timeout=0 never sleeps inside SQLite on the event-loop thread.
        async with self.async_guard() as db:
            yield self._api_allowed(db, chat, model, effort, verified)

    def stop(
        self,
        chat: str = "*",
        reason: str = "manual stop",
        db: sqlite3.Connection | None = None,
    ) -> None:
        self.set_control("stop:" + chat, reason, db)

    def resume(self, chat: str = "*") -> None:
        # Resuming never clears unresolved intents or quota; those need separate human action.
        with self.connect() as db:
            db.execute("DELETE FROM controls WHERE key=?", ("stop:" + chat,))

    def opt_in_sol(self, chat: str, enabled: bool = True) -> None:
        chat = canonical_chat(chat)
        with self.connect() as db:
            if enabled:
                db.execute("INSERT OR IGNORE INTO consent VALUES(?)", (chat,))
            else:
                db.execute("DELETE FROM consent WHERE chat=?", (chat,))

    def quota(self, db: sqlite3.Connection | None = None) -> None:
        self.set_control("astra_quota", "blocked", db)

    def clear_quota(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM controls WHERE key='astra_quota'")

    def resolve(self, chat: str, turn: str, generation: str) -> None:
        # Human acknowledgement consumes the identity permanently; it never makes it retryable.
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET state='resolved' WHERE chat=? AND turn=? AND generation=?",
                (chat, turn, generation),
            )

    def blocked(self, observation: Observation, db: sqlite3.Connection) -> str:
        if db.execute(
            "SELECT 1 FROM controls WHERE key=? AND value='0'",
            ("watch:" + observation.chat,),
        ).fetchone():
            return "unwatched"
        keys = ("stop:*", "stop:" + observation.chat)
        if db.execute("SELECT 1 FROM controls WHERE key IN (?,?)", keys).fetchone():
            return "stopped"
        if observation.model == "Astra":
            if db.execute("SELECT 1 FROM controls WHERE key='astra_quota'").fetchone():
                return "astra-quota"
        elif observation.model == "Sol" and observation.effort == "Extra High":
            if not db.execute(
                "SELECT 1 FROM consent WHERE chat=?", (observation.chat,)
            ).fetchone():
                return "sol-consent-required"
        else:
            return "unverified-model-effort"
        if db.execute(
            "SELECT 1 FROM attempts WHERE chat=? AND state IN ('reserved','uncertain')",
            (observation.chat,),
        ).fetchone():
            return "unresolved-send"
        return ""

    def reserve(self, observation: Observation, wall: float | None = None) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if wall is None:
                wall = time.time()
            clock = db.execute(
                "SELECT value FROM controls WHERE key='clock'"
            ).fetchone()
            if clock and wall < float(clock[0]):
                db.execute(
                    "INSERT OR REPLACE INTO controls VALUES('stop:*','clock moved backwards')"
                )
                return "clock-backwards"
            db.execute(
                "INSERT OR REPLACE INTO controls VALUES('clock',?)", (str(wall),)
            )
            reason = self.blocked(observation, db)
            if reason:
                return reason
            if db.execute(
                "SELECT 1 FROM attempts WHERE chat=? AND turn=? AND generation=?",
                observation.identity,
            ).fetchone():
                return "deduplicated"
            latest = db.execute(
                "SELECT MAX(wall) FROM attempts WHERE chat=?", (observation.chat,)
            ).fetchone()[0]
            if latest is not None and wall - latest < 60:
                return "cooldown"
            count = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE chat=? AND wall>?",
                (observation.chat, wall - 3600),
            ).fetchone()[0]
            if count >= 3:
                return "hourly-limit"
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?, 'reserved')",
                (*observation.identity, wall),
            )
            return "reserved"

    def finish(self, observation: Observation, acknowledged: bool) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET state=? WHERE chat=? AND turn=? AND generation=?",
                ("sent" if acknowledged else "uncertain", *observation.identity),
            )

    def _reserved_allowed(
        self, db: sqlite3.Connection, observation: Observation
    ) -> bool:
        own = db.execute(
            "SELECT state FROM attempts WHERE chat=? AND turn=? AND generation=?",
            observation.identity,
        ).fetchone()
        if not own or own[0] != "reserved":
            return False
        if db.execute(
            "SELECT 1 FROM controls WHERE key IN ('stop:*',?)",
            ("stop:" + observation.chat,),
        ).fetchone():
            return False
        if db.execute(
            "SELECT 1 FROM controls WHERE key=? AND value='0'",
            ("watch:" + observation.chat,),
        ).fetchone():
            return False
        if observation.model == "Astra":
            return not db.execute(
                "SELECT 1 FROM controls WHERE key='astra_quota'"
            ).fetchone()
        return (
            observation.model == "Sol"
            and observation.effort == "Extra High"
            and bool(
                db.execute(
                    "SELECT 1 FROM consent WHERE chat=?", (observation.chat,)
                ).fetchone()
            )
        )

    def can_send_reserved(self, observation: Observation) -> bool:
        with self.connect() as db:
            return self._reserved_allowed(db, observation)

    @contextmanager
    def dispatch(self, observation: Observation):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield self._reserved_allowed(db, observation)

    @asynccontextmanager
    async def async_dispatch(self, observation: Observation) -> AsyncIterator[bool]:
        async with self.async_guard() as db:
            yield self._reserved_allowed(db, observation)

    def controls(self) -> dict[str, str]:
        with self.connect() as db:
            return dict(db.execute("SELECT key,value FROM controls"))

    def status(self) -> dict:
        with self.connect() as db:
            return {
                "controls": dict(db.execute("SELECT key,value FROM controls")),
                "sol_chats": [row[0] for row in db.execute("SELECT chat FROM consent")],
                "attempts": [
                    dict(zip(("chat", "turn", "generation", "wall", "state"), row))
                    for row in db.execute(
                        "SELECT * FROM attempts ORDER BY wall DESC LIMIT 100"
                    )
                ],
            }


class Guardian:
    def __init__(self, store: Store, apply: bool = False):
        self.store = store
        self.apply = apply
        self.stability: dict[str, tuple[Observation, float]] = {}
        self.last_seen: dict[str, float] = {}

    def record_safety(
        self, observation: Observation, db: sqlite3.Connection | None = None
    ) -> None:
        if observation.quota:
            self.store.quota(db)
        if not observation.safe_failure():
            if not observation.chat:
                self.stability.clear()
            else:
                self.stability.pop(observation.chat, None)
            if observation.challenge or (self.apply and observation.draft):
                self.store.stop(
                    observation.chat or "*",
                    "human challenge" if observation.challenge else "human draft",
                    db,
                )

    def consider(
        self, observation: Observation, monotonic: float, wall: float | None
    ) -> str:
        self.record_safety(observation)
        if not observation.safe_failure():
            return "unsafe-or-not-active-failure"
        previous = self.stability.get(observation.chat)
        last = self.last_seen.get(observation.chat)
        self.last_seen[observation.chat] = monotonic
        if (
            previous is None
            or last is None
            or monotonic < last
            or monotonic - last > 6
            or previous[0] != observation
            or monotonic < previous[1]
        ):
            self.stability[observation.chat] = observation, monotonic
            return "stabilizing"
        if monotonic - previous[1] < 20:
            return "stabilizing"
        if not self.apply:
            return "observe"
        return self.store.reserve(observation, wall)

    def recent(self, observation: Observation) -> bool:
        last = self.last_seen.get(observation.chat)
        return last is not None and 0 <= time.monotonic() - last <= 6

    async def run_once(
        self,
        adapter: Adapter,
        initial: Observation | None = None,
        observed_at: float | None = None,
    ) -> str:
        observed = initial if initial is not None else await adapter.observe()
        captured = time.monotonic() if observed_at is None else observed_at
        if (
            initial is not None and observed_at is None
        ) or not 0 <= time.monotonic() - captured <= 6:
            self.stability.pop(observed.chat, None)
            return "stale-observation"
        reason = await asyncio.to_thread(self.consider, observed, captured, None)
        if reason != "reserved":
            return reason
        try:
            current = await adapter.observe()
            if current.quota:
                await asyncio.to_thread(self.store.quota)
            if current.challenge or (self.apply and current.draft):
                await asyncio.to_thread(
                    self.store.stop,
                    current.chat or "*",
                    "human challenge" if current.challenge else "human draft",
                )
            if (
                current != observed
                or not current.safe_failure()
                or not self.recent(observed)
            ):
                await asyncio.to_thread(self.store.finish, observed, False)
                self.stability.pop(observed.chat, None)
                return "safety-stop-before-send"
            async with self.store.async_guard() as db:
                dispatch_allowed = self.store._reserved_allowed(
                    db, observed
                ) and self.recent(observed)
                acknowledged: bool | MutationRefusal
                if not dispatch_allowed:
                    acknowledged = False
                else:

                    async def send_and_record():
                        result = await adapter.send(observed)
                        if isinstance(result, MutationRefusal):
                            self.record_safety(result.observation, db)
                        return result

                    acknowledged = await protected_dispatch(send_and_record())
            await asyncio.to_thread(self.store.finish, observed, bool(acknowledged))
            if not dispatch_allowed:
                self.stability.pop(observed.chat, None)
                return "safety-stop-before-send"
            return "sent" if acknowledged else "uncertain-send"
        except BaseException:
            await asyncio.to_thread(self.store.finish, observed, False)
            raise
