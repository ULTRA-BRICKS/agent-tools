"""Single-tab patrol: one owned conversation tab per account, advanced through a persisted queue.

Accessed via: `chatgpt-guardian live --single-tab` (cli.py dispatches here instead of
live.serve_live). Same account owner as the legacy loop: the same CDP owner lock, recovery
database, idempotency ledger, control endpoint, transport latch, sender and continuation ledger.
It is mutually exclusive with the legacy multi-tab mode: both take the same owner lock. It also
holds the per-account tab lock (lock.account_lock) for its whole life, so `coverage --apply` and
`startup --apply`, the legacy restore-all openers, find the account busy and open nothing.

Each due visit navigates the ONE owned tab to the next queued conversation, reads only that
conversation (read-only), and sends at most one Go for a verified structurally failed latest
turn through the existing Guardian reservation. A bounded in-visit wait then correlates the Go
with the next assistant response. The wait is INSIDE the interval: the next visit is due one
draw after this visit STARTED (see rotation.py).

Assumptions: the browser is the account's dedicated profile; a conversation tab is Guardian-owned
only when its conversation identity is in the rotation queue or in the persisted owned-URL list;
every other page (login, /space, user tabs) is never read, navigated or closed. A nonempty draft,
an authentication screen, an active login flow or a RUNNING RESPONSE protects a tab: the patrol
defers the slot (an explicit persisted state) instead of navigating away or opening a second tab;
navigating away from an active response has no permitted live proof and is disabled. A 429 keeps
the account silent (no read, navigation or Go) until the real Retry-After, or a conservative
default, has passed; an elapsed deadline alone never clears the machine hold (only a verified
later success of every restricted endpoint does) and at most permits one read-only check visit.

Past bugs it avoids: confirm_failure's quota fallback switches models; this path never selects a
model. A refused or interrupted send keeps its (chat, turn) identity consumed (core.Store), so a
restart can never repeat a Go. A native platform approval card on the latest `in_progress` turn
(no Stop) once read as a running response and pinned the single tab, starving every other queued
chat; it is now the neutral `permission-blocked` visit (queue reason `pending-platform-approval`),
the tab may leave it, and nothing is ever clicked, answered or sent for it.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import approval, backoff, live, rotation
from .continuation import Continuations
from .core import Guardian, Observation, Store
from .live import process_continuation
from .live_browser import LiveAdapter
from .lock import AccountBusy, account_lock
from .monitor import _continuation
from .monitor_observer import DOM_SCRIPT
from .service import ControlServer
from .urls import canonical_chat, conversation_identity

# The exact text live.TransportGuard writes for a 429; a test pins the two together.
MACHINE_429_HOLD = rotation.MACHINE_429_HOLD
# `hold_unverified` verdicts that still allow one read-only check visit (nothing to send).
CHECKABLE = ("unverified", "blocked-newer")
MIN_WAIT = 0.25
# Recorded when a live 429 arrives while a machine hold already stands without any evidence of
# ours (a legacy owner or a replayed seed latched it): no response ever targets this path, so
# such a hold can never be released by the patrol's own evidence.
FOREIGN_HOLD = rotation.FOREIGN_HOLD
POLL_SECONDS = 60.0
ACCOUNT_LOCK_WAIT = 150.0
SAMPLE_GAP = 3.0
SETTLE_STEP = 1.0
AUTH_HOSTS = frozenset(
    {
        "auth.openai.com",
        "auth0.openai.com",
        "accounts.google.com",
        "appleid.apple.com",
        "login.microsoftonline.com",
        "login.live.com",
    }
)
AUTH_PATHS = ("/auth", "/login", "/log-in", "/sign-in", "/signin")


def go_in_flight(go: dict | None, chat: str | None) -> bool:
    """A send for this chat is mid-click, or its reply has not been correlated yet.

    An older interrupted or uncertain record is history: the recovery database, not this
    record, decides whether a new turn may be continued.
    """
    if not go or go["chat"] != chat:
        return False
    return go["state"] == "sending" or (
        go["state"] == "sent" and go["confirmation"] == "unconfirmed"
    )


def auth_url(url: str) -> bool:
    """A page that looks like part of a sign-in flow; the patrol never touches one."""
    parts = urlsplit(url)
    if parts.netloc in AUTH_HOSTS:
        return True
    return parts.netloc == "chatgpt.com" and parts.path.startswith(AUTH_PATHS)


class PatrolTransport(live.TransportGuard):
    """The transport latch plus a durable cooldown that carries the real Retry-After."""

    def __init__(self, context, store: Store, machine: rotation.Rotation):
        self.machine = machine
        super().__init__(context, store)

    @staticmethod
    def _chatgpt_api(url: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "chatgpt.com"
            and parsed.path.startswith(("/backend-api/", "/api/auth/", "/auth/"))
        )

    def record(self, url: str, status: int) -> None:
        """Atomically latch transport failures WITHOUT replacing any existing owner stop.

        A later authentication failure may strengthen a machine 429 hold, never downgrade it,
        and no response rewrites an owner, challenge or quota pause. `INSERT OR IGNORE` is
        required: check-then-stop would race an owner's manual pause. The parent `seed` calls
        this override too, so replayed resource entries cannot overwrite a stop either.
        """
        if not self._chatgpt_api(url) or status not in (429, 401, 403):
            return
        reason = MACHINE_429_HOLD if status == 429 else "human challenge"
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO controls VALUES('stop:*', ?)", (reason,))
            if status in (401, 403):
                db.execute(
                    "UPDATE controls SET value=? WHERE key='stop:*' AND value=?",
                    (reason, MACHINE_429_HOLD),
                )
            (stored,) = db.execute(
                "SELECT value FROM controls WHERE key='stop:*'"
            ).fetchone()
        # Own the latch only when the durable stop carries this guard's reason: a different
        # stop (owner pause) or a stronger one this guard wrote earlier stays as it is.
        if stored == reason:
            self.latched = reason

    def on_response(self, response) -> None:
        # The browser's own responses are the only transport evidence. A live 429 carries the
        # real Retry-After; its endpoint and cooldown are durable before the hold exists, so a
        # hold never lacks its evidence. Entries replayed by `seed` are old events, not new
        # 429s: they never start or extend a cooldown and never count as evidence. The latch
        # itself never depends on the evidence write: a full disk must not drop a live 429.
        try:
            if self._chatgpt_api(response.url):
                self._record_evidence(response)
        finally:
            super().on_response(response)

    def _record_evidence(self, response) -> None:
        now = self.machine.clock()
        path = urlsplit(response.url).path
        status = response.status
        if status == 429:
            if self._foreign_hold():
                self.machine.note_restricted(FOREIGN_HOLD, now)
            self.machine.note_restricted(path, now)
            # The deadline is max(adaptive ladder, the real Retry-After): see rotation.note_429.
            self.machine.note_429(response.headers.get("retry-after"))
        elif status in (401, 403):
            self.machine.note_blocked(now)
        elif 200 <= status < 300:
            self.machine.note_success(path, now)
            self.machine.note_target_success(path, now)
        elif 500 <= status < 600:
            self.machine.note_server_error()

    def _foreign_hold(self) -> bool:
        """A machine 429 hold that already stands although the patrol has no evidence for it
        (a legacy owner or a replayed seed latched it): it can never be verified here."""
        held = self.store.controls().get("stop:*") == MACHINE_429_HOLD
        return held and not self.machine.transport["restricted"]


class PageIO:
    """Everything the patrol reads from a page. Reads are passive; `active` may probe and send."""

    def passive(self, page) -> LiveAdapter:
        return LiveAdapter(page, allow_active_probe=False)

    def active(self, page) -> LiveAdapter:
        return LiveAdapter(page)

    async def read(self, page) -> Observation:
        return await asyncio.wait_for(self.passive(page).observe(), timeout=5)

    async def sample(self, page) -> dict:
        """The monitor's metadata-only DOM read; never page text, never a page change."""
        from playwright.async_api import Error as BrowserError

        try:
            result = await asyncio.wait_for(page.evaluate(DOM_SCRIPT), timeout=5)
        except (BrowserError, asyncio.TimeoutError) as exc:
            return {"error": type(exc).__name__}
        return result if isinstance(result, dict) else {"error": "invalid-sample"}


@dataclass(frozen=True)
class PatrolConfig:
    apply: bool = False
    max_pages: int = live.CONVERSATION_PAGE_BUDGET
    min_available_mib: int = live.DEFAULT_MIN_AVAILABLE_MIB
    confirm_wait: float = rotation.DEFAULT_CONFIRM_WAIT
    private_display: bool = False
    go_window: float = 60.0
    settle_seconds: float = 20.0


class GoAdapter:
    """Guardian adapter for one visited failed turn: no model switching, no Stop, one send."""

    def __init__(self, owner: Patrol, page, candidate: Observation, allowed):
        self.owner = owner
        self.candidate = candidate
        self.allowed = allowed
        self.passive = owner.io.passive(page)
        self.active = owner.io.active(page)

    async def _safety(self, observed: Observation) -> None:
        await asyncio.to_thread(self.owner.guardian.record_safety, observed)

    async def observe(self) -> Observation:
        empty = Observation("", "", "")
        if not self.allowed():
            return empty
        structural = await self.passive.observe()
        if (
            not self.allowed()
            or not live.structural_failure(structural)
            or approval.flagged(structural)
            or structural.identity != self.candidate.identity
        ):
            await self._safety(structural)
            return empty
        observed = await self.active.observe()
        await self._safety(observed)
        if (
            not self.allowed()
            or observed.identity != self.candidate.identity
            or not observed.safe_failure()
        ):
            return empty
        return observed

    async def send(self, observed: Observation):
        if not self.allowed():
            return False
        self.owner.rotation.begin_go(observed.chat, observed.turn)
        return await self.active.send(observed)


class Patrol:
    """One account's patrol. `tick()` performs one decision step and never sleeps for a visit."""

    def __init__(
        self,
        *,
        store: Store,
        guardian: Guardian,
        rotation: rotation.Rotation,
        context,
        transport,
        io: PageIO,
        config: PatrolConfig,
        stop: asyncio.Event,
        sleep=asyncio.sleep,
        watch_urls: dict[str, str] | None = None,
    ):
        self.store = store
        self.guardian = guardian
        self.rotation = rotation
        self.context = context
        self.transport = transport
        self.io = io
        self.config = config
        self.stop = stop
        self.sleep = sleep
        self.watch_urls = watch_urls or {}
        self.wait = POLL_SECONDS
        self.checking = False
        self.controls: dict = {}
        self.tab = (
            None  # the page this patrol opened, reused if a navigation left it unowned
        )

    # -- one decision step ---------------------------------------------------------------------

    async def tick(self) -> dict:
        controls = await asyncio.to_thread(self.store.controls)
        self.rotation.sync(self._wanted(controls))
        held, controls = await self._hold(controls)
        self.controls = controls
        if held:
            return self._health(held, hold=controls.get("stop:*", ""))
        if self._login_flow():
            return self._health("login-flow")
        # The check visit under a machine 429 hold may read a chat the global hold would
        # otherwise exclude; per-chat stops still exclude theirs, and `_visit` keeps the hold in
        # the controls it hands to the Go admission, so nothing can be sent.
        readable = {
            k: v for k, v in controls.items() if not (self.checking and k == "stop:*")
        }
        picked = self.rotation.pick(
            lambda chat: not rotation.pause_reason(readable, chat),
            prefer=None if self.checking else await self._approved_chat(),
            probe=self.checking,
        )
        owned = self._owned_pages()
        # Protected extras are re-read only at a due visit, never every minute.
        if len(owned) > 1 and (
            self.rotation.is_due() or not self.rotation.tabs["protected"]
        ):
            await self._consolidate(owned, picked[0].chat if picked else None)
        if not self.rotation.is_due():
            return self._health("connected")
        return await self._visit(controls, picked)

    def _health(self, health: str, **extra) -> dict:
        conversations, auxiliary = live.page_counts(
            live.open_page_urls(self.context.pages)
        )
        due_in = self.rotation.due_at() - self.rotation.clock()
        self.wait = (
            min(POLL_SECONDS, max(MIN_WAIT, due_in))
            if health == "connected"
            else POLL_SECONDS
        )
        pending = self.controls.get("stop:*") == MACHINE_429_HOLD
        return {
            "health": health,
            "mode": "single-tab",
            "apply": self.config.apply,
            "queue": len(self.rotation.queue),
            "current": self.rotation.current,
            "next_navigation_at": self.rotation.due_at(),
            "owned_tabs": len(self._owned_pages()),
            "conversation_page_count": conversations,
            "auxiliary_page_count": auxiliary,
            "transport": self.transport.reason,
            "transport_hold": rotation.HOLD_PENDING if pending else "",
            "pressure_level": self.rotation.pressure.level,
            "retry_pending": sum(e.retry is not None for e in self.rotation.queue),
            "visit": None,
            **extra,
        }

    # -- holds ---------------------------------------------------------------------------------

    async def _hold(self, controls: dict) -> tuple[str, dict]:
        """Why nothing may be read, navigated or sent now.

        An elapsed deadline never clears a hold. Only the exact machine 429 hold, with its own
        cooldown and endpoint evidence, is ever released, and only when the browser's responses
        show a later success of every restricted endpoint (rotation.hold_unverified). Until
        then the elapsed deadline permits one paced read-only check visit and never a Go. A
        hold this process holds no evidence for (a legacy owner latched it) becomes an explicit
        unverifiable state with the same paced diagnostics; the clock never clears it.
        """
        self.checking = False
        if self.rotation.cooling():
            return "cooldown", controls
        if (
            controls.get("stop:*") == MACHINE_429_HOLD
            and self.rotation.cooldown is None
        ):
            self.rotation.adopt_legacy_hold()
            return "cooldown", controls
        machine = self._machine_hold(controls)
        if self.rotation.cooldown is not None and not machine:
            self.rotation.clear_hold()  # the deadline is over and no 429 hold stands
        if machine:
            controls = await self._judge_hold(controls)
            if self.rotation.cooling():
                return "cooldown", controls
        if self.checking:
            return "", controls
        if "stop:*" in controls or self.transport.reason:
            return "stopped", controls
        return "", controls

    def _machine_hold(self, controls: dict) -> bool:
        cooldown = self.rotation.cooldown
        return (
            controls.get("stop:*") == MACHINE_429_HOLD
            and cooldown is not None
            and cooldown["hold"] == MACHINE_429_HOLD
        )

    async def _judge_hold(self, controls: dict) -> dict:
        """A machine 429 hold whose cooldown is over: release it only when verified."""
        why = self.rotation.hold_unverified()
        if why == "no-restriction-evidence":
            self.rotation.adopt_legacy_hold()  # cooldown record without any endpoint evidence
            return controls
        if not why:
            return await self._release()
        self.checking = why in CHECKABLE
        return controls

    def _drop_machine_hold(self) -> None:
        # Compare and delete in one statement: a different `stop:*` written meanwhile survives.
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM controls WHERE key='stop:*' AND value=?",
                (MACHINE_429_HOLD,),
            )

    async def _release(self) -> dict:
        """Drop exactly the machine hold and its evidence; every other control stays."""
        await asyncio.to_thread(self._drop_machine_hold)
        self.rotation.clear_hold()
        return await asyncio.to_thread(self.store.controls)

    def _login_flow(self) -> bool:
        return any(
            not page.is_closed() and auth_url(page.url) for page in self.context.pages
        )

    # -- tabs ----------------------------------------------------------------------------------

    def _wanted(self, controls: dict) -> dict[str, str]:
        """Enrolled chats mapped to the best permanent URL known locally (no history scan)."""
        seen = {
            conversation_identity(page.url): rotation.permanent_url(page.url)
            for page in self.context.pages
            if not page.is_closed() and conversation_identity(page.url)
        }
        wanted = {}
        for key, value in controls.items():
            chat = key.removeprefix("watch:")
            if (
                key.startswith("watch:")
                and value == "1"
                and conversation_identity(chat) == chat
            ):
                wanted[chat] = self.watch_urls.get(chat) or seen.get(chat) or chat
        return wanted

    def _owned_chats(self) -> set[str]:
        stored = {conversation_identity(url) for url in self.rotation.owned_urls}
        return {entry.chat for entry in self.rotation.queue} | stored

    def _owned_pages(self) -> list:
        owned = self._owned_chats()
        return [
            page
            for page in self.context.pages
            if not page.is_closed() and conversation_identity(page.url) in owned
        ]

    async def _leave_blocked(self, page, away: bool = True) -> str:
        """Why the patrol may not navigate this tab away.

        A draft, an authentication screen, or an ACTIVE RESPONSE. Navigating away from a running
        response has no permitted live proof, so it is deliberately disabled: the tab is left
        alone, nothing is clicked (no Stop), and the visit becomes an explicit deferral.
        A clearly paused platform approval card is not a response being generated: with no Stop,
        no draft, no challenge and a precise identity, leaving it reads the next chat without
        answering or cancelling anything; an approval flag without that identity stays protected.
        `away=False` asks only whether the tab may be kept as the keeper of a consolidation.
        """
        observed = await self.io.read(page)
        if observed.draft:
            return "draft"
        if observed.challenge:
            return "challenge"
        if away and observed.running:
            return "running"
        if (
            away
            and approval.flagged(observed)
            and not self._card_on_this_tab(page.url, observed)
        ):
            return "approval-unverified"
        return ""

    @staticmethod
    def _card_on_this_tab(url: str, observed: Observation) -> bool:
        """A card flag is trusted for leaving only with a precise identity on this very tab.

        `approval.pending` already needs chat, turn and generation and no Stop/draft/challenge/
        quota/ambiguity; the chat must also be the tab's own conversation, and the turn must be
        the generation the page script reported for it (a mismatch means a moving head).
        """
        return (
            approval.pending(observed)
            and observed.chat == conversation_identity(url)
            and observed.turn == observed.generation
        )

    async def _close_blocked(self, page) -> str:
        """Why an extra owned tab may not be closed: anything but a provably idle, empty tab."""
        observed = await self.io.read(page)
        unresolved = go_in_flight(self.rotation.go, conversation_identity(page.url))
        checks = (
            ("draft", observed.draft),
            ("challenge", observed.challenge),
            ("running", observed.running),
            ("pending-approval", approval.flagged(observed)),
            ("pending-go", unresolved),
            ("unreadable", observed.chat == ""),
        )
        return next((reason for reason, bad in checks if bad), "")

    async def _consolidate(self, owned: list, target: str | None) -> None:
        """Persist every owned URL first, then close only proven-idle extras beside one keeper."""
        self.rotation.record_owned([page.url for page in owned])
        ordered = sorted(
            owned, key=lambda page: conversation_identity(page.url) != target
        )
        keeper = None
        for page in ordered:
            if not await self._leave_blocked(page, away=False):
                keeper = page
                break
        protected = []
        for page in ordered:
            if page is keeper:
                continue
            if await self._close_blocked(page):
                protected.append(conversation_identity(page.url))
                continue
            await page.close()
        self.rotation.record_tabs(sum(not p.is_closed() for p in owned), protected)

    def _open_pause(self) -> str:
        conversations, auxiliary = live.page_counts(
            live.open_page_urls(self.context.pages)
        )
        return live.resource_pause(
            conversations + 1,
            auxiliary,
            live.available_memory(),
            max_pages=self.config.max_pages,
            min_available_mib=self.config.min_available_mib,
        )

    async def _patrol_tab(
        self, entry: rotation.Entry, recheck: bool = False
    ) -> tuple[object | None, str]:
        """The one tab to drive, or why none may be: the ceiling forbids a second tab.

        A `recheck` reloads the chat the tab already shows, so that tab is checked exactly like
        one that is navigated away (draft, challenge, running response).
        """
        owned = self._owned_pages()
        if not owned:
            if self.tab is not None and not self.tab.is_closed():
                reason = await self._leave_blocked(self.tab)
                return (None, reason) if reason else (self.tab, "")
            paused = self._open_pause()
            if paused:
                return None, "resource-paused:" + paused
            self.tab = await self.context.new_page()
            return self.tab, ""
        ordered = sorted(
            owned, key=lambda page: conversation_identity(page.url) != entry.chat
        )
        first = ""
        for page in ordered:
            away = recheck or conversation_identity(page.url) != entry.chat
            reason = await self._leave_blocked(page, away)
            if not reason:
                return page, ""
            first = first or reason
        return None, first

    # -- a visit -------------------------------------------------------------------------------

    async def _approved_chat(self) -> str | None:
        """The chat of the oldest unexpired owner approval, so its visit comes next."""
        if not self.config.apply:
            return None
        ledger = Continuations(self.store)
        now = time.time()
        await asyncio.to_thread(ledger.expire, now)
        record = await asyncio.to_thread(ledger.next_queued, now)
        return record.chat if record else None

    async def _visit(self, controls: dict, picked) -> dict:
        if picked is None:
            self.rotation.idle("nothing-visitable")
            return self._health(
                "connected", visit={"chat": None, "outcome": "nothing-visitable"}
            )
        entry, advance = picked
        # A check under a hold, or a chat that carries a failure, is a controlled read-only
        # retry: the page is loaded again (once) even when it already shows that chat.
        recheck = self.checking or entry.retry is not None
        page, blocked = await self._patrol_tab(entry, recheck)
        if page is None:
            outcome = (
                "running-protected" if blocked == "running" else "protected:" + blocked
            )
            self.rotation.defer(outcome, outcome)
            health = (
                "resource-paused" if blocked.startswith("resource") else "protected-tab"
            )
            return self._health(health, blocked=blocked)
        self.rotation.begin_visit(entry, probe=recheck)
        outcome = await self._guarded(page, entry, controls, recheck)
        self.rotation.end_visit(entry, outcome, advance)
        if self.checking:
            await self._after_check()
        return self._health("connected", visit={"chat": entry.chat, "outcome": outcome})

    async def _after_check(self) -> None:
        """The single read-only check visit is over: release if its responses verified the
        hold, otherwise stay silent again for a paced quiet period (a new 429 wins)."""
        if not self.rotation.hold_unverified():
            self.controls = await self._release()
        elif not self.rotation.cooling():
            self.rotation.rearm_probe()

    async def _guarded(
        self, page, entry: rotation.Entry, controls: dict, recheck: bool
    ) -> str:
        from playwright.async_api import Error as BrowserError

        try:
            return await self._read_and_act(page, entry, controls, recheck)
        except (BrowserError, asyncio.TimeoutError) as exc:
            self.guardian.stability.pop(entry.chat, None)
            return "error:" + type(exc).__name__

    async def _navigate(self, page, entry: rotation.Entry) -> str:
        from playwright.async_api import Error as BrowserError

        try:
            await page.goto(entry.url, wait_until="domcontentloaded", timeout=30000)
        except (BrowserError, asyncio.TimeoutError) as exc:
            return "nav-error:" + type(exc).__name__
        return "" if conversation_identity(page.url) == entry.chat else "redirected"

    async def _settle(self, page, chat: str) -> Observation:
        deadline = time.monotonic() + self.config.settle_seconds
        while True:
            observed = await self.io.read(page)
            readable = observed.chat == chat and (observed.turn or observed.running)
            if observed.challenge or readable:
                return observed
            if time.monotonic() >= deadline or self.stop.is_set():
                return observed
            await self.sleep(SETTLE_STEP)

    def _silenced(self) -> bool:
        """A cooldown, or a transport latch. The single check visit under a machine 429 hold
        still reads (the legacy owner observes under a hold too); it can never send."""
        return self.rotation.cooling() or (
            bool(self.transport.reason) and not self.checking
        )

    async def _read_and_act(
        self, page, entry: rotation.Entry, controls: dict, recheck: bool = False
    ) -> str:
        if recheck or conversation_identity(page.url) != entry.chat:
            failure = await self._navigate(page, entry)
            if failure:
                return failure
        observed = await self._settle(page, entry.chat)
        await asyncio.wait_for(self.transport.seed(page), timeout=5)
        if self._silenced():
            return "cooldown"
        sample = await self.io.sample(page)
        await asyncio.to_thread(self.guardian.record_safety, observed)
        approved = await self._dispatch_approval(page, entry, observed)
        if approved:
            return approved
        return await self._classify(page, entry, observed, sample, controls)

    async def _dispatch_approval(
        self, page, entry: rotation.Entry, observed: Observation
    ) -> str:
        """An owner approval is dispatched only while the tab is on its own chat."""
        if not self.config.apply or self.checking:
            return ""
        record = await asyncio.to_thread(
            Continuations(self.store).next_queued, time.time()
        )
        if record is None or record.chat != entry.chat:
            return ""
        cfg = self.config
        result = await process_continuation(
            self.guardian,
            {entry.chat: [page]},
            self.context,
            self.transport,
            self.stop,
            pause=live.context_resource_pause(
                self.context,
                max_pages=cfg.max_pages,
                min_available_mib=cfg.min_available_mib,
            ),
            max_pages=cfg.max_pages,
            min_available_mib=cfg.min_available_mib,
            private_display=cfg.private_display,
        )
        if result is None:
            return ""
        if result[1] != "sent" and approval.pending(observed):
            # The refusal receipt stays in the continuation ledger; the visit itself is the
            # neutral blocked state, not a clean "approval:*" visit that would clear retries.
            return backoff.PERMISSION_BLOCKED
        return "approval:" + result[1]

    async def _classify(
        self, page, entry, observed: Observation, sample: dict, controls
    ) -> str:
        if observed.challenge:
            self.rotation.note_blocked(self.rotation.clock())
            return "challenge"
        if observed.quota or sample.get("status") == "quota-or-rate-limit":
            self.rotation.note_blocked(self.rotation.clock())
            return "rate-limited"
        if observed.draft:
            return "draft"
        if live.structural_failure(observed) and not approval.flagged(observed):
            return await self._failed(page, entry, observed, controls)
        await self._reconcile_go(entry.chat, sample)
        if approval.pending(observed) and not sample.get("stop_visible"):
            return backoff.PERMISSION_BLOCKED
        if observed.running or sample.get("running"):
            return "working"
        if sample.get("status") == "complete" or observed.turn_state == "complete":
            return "complete"
        if sample.get("error"):
            return "unreadable:" + str(sample["error"])
        return "unknown"

    # -- Go ------------------------------------------------------------------------------------

    def _go_blocked(self, chat: str, controls: dict) -> str:
        checks = (
            ("observe-only", not self.config.apply),
            ("stopped", "stop:*" in controls or "stop:" + chat in controls),
            ("unwatched", controls.get("watch:" + chat) != "1"),
            ("cooldown", self._silenced()),
            (
                "resource-paused",
                bool(
                    live.context_resource_pause(
                        self.context,
                        max_pages=self.config.max_pages,
                        min_available_mib=self.config.min_available_mib,
                    )
                ),
            ),
        )
        return next((reason for reason, bad in checks if bad), "")

    async def _failed(self, page, entry, observed: Observation, controls: dict) -> str:
        reason = self._go_blocked(entry.chat, controls)
        if reason:
            return "failed:" + reason
        result = await self._go(page, observed)
        if result == "sent":
            await self._confirm(page, observed)
        return "failed:" + result

    async def _go(self, page, candidate: Observation) -> str:
        started = time.monotonic()
        url = page.url
        count = len(self.context.pages)

        def allowed() -> bool:
            return live.mutation_window_open(
                self.guardian,
                page,
                candidate.chat,
                self.context,
                self.transport,
                self.stop,
                url=url,
                page_count=count,
                started=started,
                limit=self.config.go_window,
                max_pages=self.config.max_pages,
                min_available_mib=self.config.min_available_mib,
            )

        adapter = GoAdapter(self, page, candidate, allowed)
        self.guardian.stability.pop(candidate.chat, None)
        try:
            if self.config.private_display and allowed():
                await page.bring_to_front()
            result = await asyncio.wait_for(
                self._stabilize(adapter, allowed), timeout=self.config.go_window
            )
        except asyncio.TimeoutError:
            result = "timeout"
        except Exception:
            self._settle_go("uncertain")
            raise
        finally:
            self.guardian.stability.pop(candidate.chat, None)
        self._settle_go("sent" if result == "sent" else "uncertain")
        return result

    def _settle_go(self, state: str) -> None:
        """Finish a Go record still `sending`; one that was already finished is left alone."""
        go = self.rotation.go
        if go and go["state"] == "sending":
            self.rotation.finish_go(state)

    async def _stabilize(self, adapter: GoAdapter, allowed) -> str:
        """The existing 20 second stability, re-observed every few seconds, then one send."""
        while allowed():
            observed = await adapter.observe()
            if not observed.safe_failure():
                return "confirmation-blocked"
            captured = time.monotonic()
            result = await self.guardian.run_once(
                adapter, initial=observed, observed_at=captured
            )
            if result != "stabilizing":
                return result
            await self.sleep(SAMPLE_GAP)
        return "confirmation-blocked"

    def _attempt(self, chat: str, turn: str) -> dict | None:
        """The newest attempt for this exact turn, whatever its generation id."""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT chat,turn,generation,wall,state FROM attempts "
                "WHERE chat=? AND turn=? ORDER BY wall DESC LIMIT 1",
                (chat, turn),
            ).fetchone()
        return (
            dict(zip(("chat", "turn", "generation", "wall", "state"), row))
            if row
            else None
        )

    async def _confirm(self, page, candidate: Observation) -> str:
        """Bounded in-visit wait for the assistant response that follows the Go."""
        attempt = await asyncio.to_thread(self._attempt, candidate.chat, candidate.turn)
        started = time.monotonic()
        while True:
            state, response = _continuation(attempt, await self.io.sample(page))
            if state in ("running", "completed"):
                self.rotation.confirm_go(state, response)
                return state
            remaining = self.config.confirm_wait - (time.monotonic() - started)
            if remaining <= 0 or self.stop.is_set():
                self.rotation.confirm_go("unconfirmed")
                return "unconfirmed"
            await self.sleep(min(SAMPLE_GAP, remaining))

    async def _reconcile_go(self, chat: str, sample: dict) -> None:
        """A reply that arrived after the in-visit wait is correlated on the next visit."""
        go = self.rotation.go
        if not go or go["chat"] != chat or go["state"] != "sent":
            return
        if go["confirmation"] != "unconfirmed":
            return
        attempt = await asyncio.to_thread(self._attempt, chat, go["turn"])
        state, response = _continuation(attempt, sample)
        if state in ("running", "completed"):
            self.rotation.confirm_go(state, response)


class Reporter:
    """Journal lines like the legacy loop: repeats are suppressed for five minutes."""

    def __init__(self):
        self.last: dict[str, tuple[dict, float]] = {}

    def emit(self, key: str, payload: dict, *, always: bool = False) -> None:
        previous = self.last.get(key)
        now = time.monotonic()
        if (
            always
            or previous is None
            or previous[0] != payload
            or now - previous[1] >= 300
        ):
            print(json.dumps(payload), flush=True)
            self.last[key] = payload, now


def _publish(reporter: Reporter, report: dict, args) -> None:
    visit = report["visit"]
    health = {
        k: v for k, v in report.items() if k not in ("visit", "next_navigation_at")
    }
    reporter.emit("health", {**health, "workspace": args.workspace})
    if visit and visit["chat"]:
        line = {"chat": visit["chat"], "state": visit["outcome"], "apply": args.apply}
        reporter.emit("visit", {**line, "mode": "single-tab"}, always=True)


def _setting(args, name: str, default: float) -> float:
    value = getattr(args, name, None)
    return default if value is None else value


def _backoff_settings(args) -> backoff.BackoffConfig | None:
    """The explicit backoff overrides, validated; None keeps the stored settings."""
    chosen = {key: getattr(args, "backoff_" + key, None) for key in ("base", "cap")}
    chosen = {key: value for key, value in chosen.items() if value is not None}
    if not chosen:
        return None
    try:
        return backoff.BackoffConfig(**chosen).validated()
    except backoff.BackoffError as exc:
        raise rotation.RotationError(str(exc)) from exc


def _validate_session(args):
    """Validate every setting BEFORE any browser work, without touching the rotation state.

    `Rotation.open` settles interrupted sends and overdue schedules, so it MUST NOT run until
    LiveOwner and the account lock are both held: a losing contender may never rewrite the
    owner's in-flight Go record.
    """
    if args.workspace not in rotation.ACCOUNTS:
        raise rotation.RotationError("account must be home or glide.vc")
    endpoint = live.validate_cdp(args.cdp)
    low, high = rotation.DEFAULT_INTERVAL
    interval = rotation.validate_interval(
        _setting(args, "rotation_min", low), _setting(args, "rotation_max", high)
    )
    wait = rotation.validate_confirm_wait(
        _setting(args, "confirm_wait", rotation.DEFAULT_CONFIRM_WAIT)
    )
    poll = getattr(args, "poll_interval", POLL_SECONDS)
    if not 30 <= poll < float("inf"):
        raise ValueError("poll interval must be finite and at least 30 seconds")
    return endpoint, interval, wait, poll, _backoff_settings(args)


async def _run(patrol: Patrol, browser, args, stop: asyncio.Event, poll: float) -> None:
    from playwright.async_api import Error as BrowserError

    reporter = Reporter()
    while not stop.is_set():
        try:
            if not browser.is_connected():
                raise RuntimeError(
                    "CDP browser disconnected; restart preserves uncertain sends"
                )
            _publish(reporter, await patrol.tick(), args)
        except (BrowserError, asyncio.TimeoutError) as exc:
            patrol.guardian.stability.clear()
            payload = {"health": "observation-error", "error": type(exc).__name__}
            reporter.emit("health", {**payload, "retry_seconds": poll})
            patrol.wait = poll
        if args.once:
            return
        # Wake at the due instant (a quarter second floor), so a visit starts promptly and the
        # measured gap between visit starts stays inside the configured interval.
        await live.wait_stop(stop, max(MIN_WAIT, min(poll, patrol.wait)))


async def _hold_account(stack: ExitStack, store: Store, stop: asyncio.Event) -> None:
    """Hold the per-account tab lock for the patrol's whole life.

    `coverage --apply` and `startup --apply` take this same lock before they open tabs, so while
    the patrol runs they find the account busy and reopen nothing: the legacy restore-all
    cannot fight the one-tab patrol. The kernel drops the lock with the process.
    """
    deadline = time.monotonic() + ACCOUNT_LOCK_WAIT
    while True:
        try:
            stack.enter_context(account_lock(store.path))
            return
        except AccountBusy:
            if stop.is_set() or time.monotonic() >= deadline:
                raise RuntimeError(
                    "Another Guardian run holds the account lock; the patrol did not start"
                ) from None
            await asyncio.sleep(1)


async def serve_single_tab(args, store: Store) -> None:
    """The `live --single-tab` service: same owner, ledger, control endpoint and sender."""
    from playwright.async_api import Error as BrowserError
    from playwright.async_api import async_playwright

    from .cli import ensure_token

    endpoint, interval, confirm_wait, poll, tuning = _validate_session(args)
    watch = {canonical_chat(raw): rotation.permanent_url(raw) for raw in args.watch}
    guardian = Guardian(store, apply=args.apply)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    control = ControlServer(
        store,
        ensure_token(store.path.parent / "control.token"),
        args.control_port,
        continuations=args.apply,
    )
    root = Path.home() / ".local/share/agent-tools/chatgpt-guardian"
    with ExitStack() as stack, live.LiveOwner(root, endpoint):
        await _hold_account(stack, store, stop)
        machine = rotation.Rotation.open(
            rotation.rotation_path(store.path),
            args.workspace,
            interval=interval,
            backoff=tuning,
        )
        # Telemetry marker: the monitor and status views report the account as single-tab.
        await asyncio.to_thread(store.set_control, rotation.MODE_CONTROL, "1")
        await asyncio.to_thread(Continuations(store).recover_interrupted)
        for chat in watch:
            store.enroll(chat)
        control.start()
        driver = transport = None
        try:
            driver = await async_playwright().start()
            browser = await driver.chromium.connect_over_cdp(
                endpoint, timeout=10000, no_defaults=True
            )
            if len(browser.contexts) != 1:
                raise RuntimeError(
                    "Expected one dedicated ordinary Chrome account context"
                )
            context = browser.contexts[0]
            transport = PatrolTransport(context, store, machine)
            settings = PatrolConfig(
                apply=args.apply,
                max_pages=getattr(args, "max_pages", live.CONVERSATION_PAGE_BUDGET),
                min_available_mib=getattr(
                    args, "min_available_mib", live.DEFAULT_MIN_AVAILABLE_MIB
                ),
                confirm_wait=confirm_wait,
                private_display=getattr(args, "private_display", False),
            )
            patrol = Patrol(
                store=store,
                guardian=guardian,
                rotation=machine,
                context=context,
                transport=transport,
                io=PageIO(),
                config=settings,
                stop=stop,
                watch_urls=watch,
            )
            await _run(patrol, browser, args, stop, poll)
        except BrowserError as exc:
            raise RuntimeError(
                "CDP unavailable; inspect dedicated Chrome and endpoint; no recovery attempted"
            ) from exc
        finally:
            # Browser.close/context.close would terminate someone else's Chrome.
            try:
                if transport is not None:
                    transport.close()
                if driver is not None:
                    await driver.stop()
            finally:
                control.close()
