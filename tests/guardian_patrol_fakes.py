"""Synthetic ChatGPT world for the single-tab patrol tests: no browser, no network, no live state.

`World` owns what each conversation looks like at a fake time. `FakePage` is deliberately tiny
(url, goto, close, bring_to_front): any other page access, such as a click, a key press or an
evaluate, raises AttributeError, so a patrol that touches more than navigation fails loudly.
`FakeIO` stands in for the adapter wiring (`patrol.PageIO`) and reads the World.
"""

from __future__ import annotations

import asyncio
import random
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import core, live, patrol, rotation
from agenttools_chatgpt.approval import ApprovalObservation
from agenttools_chatgpt.core import Guardian, MutationRefusal, Observation, Store
from agenttools_chatgpt.urls import conversation_identity

WALL0 = 1_800_000_000.0
IDS = [f"{n:08d}-0000-4000-8000-00000000000{n}" for n in range(1, 8)]
CHATS = [f"https://chatgpt.com/c/{i}" for i in IDS]
PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/"
URLS = {chat: PROJECT + chat.rsplit("/", 1)[1] for chat in CHATS}
LOGIN = "https://chatgpt.com/auth/login"
SPACE = "https://chatgpt.com/space?tab=all"
USER_DRAFT = "half-written thought, мысль, 🙂\n  second line"


class Crash(BaseException):
    """Stands in for a process kill: not an Exception, so no handler may swallow it."""


class Clock:
    """One timeline: `mono` seconds since start, `wall` is the same instant in epoch seconds."""

    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    def wall(self):
        return WALL0 + self.t

    def advance(self, seconds):
        self.t += seconds

    async def sleep(self, seconds):
        self.t += seconds

    @property
    def clock_module(self):
        return SimpleNamespace(monotonic=self.mono, time=self.wall)


@dataclass
class Chat:
    chat: str
    kind: str = (
        "complete"  # failed | working | complete | quota | unreadable | approval
    )
    turn: str = ""
    draft: str = ""
    challenge: bool = False
    model: str = "Astra"
    sends: int = 0
    go_turn: str = ""
    go_at: float | None = None
    reply_delay: float = 8.0
    finish_after: float = 600.0
    ambiguous: bool = False
    turns_seen: list = field(default_factory=list)
    # Fields forced onto every observation of this chat (a stop, quota or draft beside a card).
    overlay: dict = field(default_factory=dict)

    def __post_init__(self):
        self.turn = self.turn or f"aaaaaaaa-0000-4000-8000-{self.chat[-12:]}"


class World:
    def __init__(self, clock, kinds=None):
        self.clock = clock
        kinds = kinds or {}
        self.chats = {chat: Chat(chat, kinds.get(chat, "complete")) for chat in CHATS}
        self.redirects: dict[str, str] = {}
        self.crash_on_send: BaseException | None = None
        self.head_change_on_active_read: dict[str, int] = {}
        self.flapping: set[str] = set()
        self.nav_error = False
        self.goto_hook = None  # called with (page, url) on every navigation; may raise
        self.reads: dict[str, int] = {}
        self.active_reads: dict[str, int] = {}

    def state(self, url) -> Chat | None:
        return self.chats.get(conversation_identity(url))

    # -- what a page shows ---------------------------------------------------------------------

    def phase(self, chat: Chat) -> str:
        """Effective kind: a sent Go turns the chat into working, then complete."""
        if chat.go_at is None:
            return chat.kind
        elapsed = self.clock.mono() - chat.go_at
        return "working" if elapsed < chat.finish_after else "complete"

    def latest_turn(self, chat: Chat) -> str:
        return chat.go_turn or chat.turn

    def read_head(self, chat: Chat, verified: bool) -> str:
        """The latest turn id as this read sees it; a test may make the head move."""
        turn = self.latest_turn(chat)
        if verified:
            self.active_reads[chat.chat] = self.active_reads.get(chat.chat, 0) + 1
        moved = self.head_change_on_active_read.get(chat.chat) == self.active_reads.get(
            chat.chat
        )
        if chat.chat in self.flapping or (verified and moved):
            return turn[:-1] + format(self.reads[chat.chat] % 16, "x")
        return turn

    def observation(self, chat: Chat, verified: bool) -> Observation:
        phase = self.phase(chat)
        self.reads[chat.chat] = self.reads.get(chat.chat, 0) + 1
        if chat.challenge:
            return Observation(chat.chat, "", "", challenge=True)
        if phase == "unreadable":
            return Observation("", "", "", draft=bool(chat.draft))
        failed = phase == "failed"
        turn = self.read_head(chat, verified)
        proven = verified and failed and bool(chat.model)
        return replace(self._page(chat, phase, turn, proven, failed), **chat.overlay)

    def _page(self, chat: Chat, phase: str, turn: str, proven: bool, failed: bool):
        # Only the page's pending native approval card yields the approval observation.
        kind = ApprovalObservation if phase == "approval" else Observation
        extra = {"approval_wait": True} if phase == "approval" else {}
        return kind(
            chat.chat,
            turn,
            turn,
            status="Thinking failed" if failed else "",
            active=failed,
            verified=proven,
            model=chat.model if proven else "",
            running=phase == "working",
            draft=bool(chat.draft),
            ambiguous=chat.ambiguous,
            quota=phase == "quota",
            turn_state={
                "failed": "cancelled",
                "working": "in_progress",
                "approval": "in_progress",
            }.get(phase, "complete"),
            **extra,
        )

    def sample(self, chat: Chat) -> dict:
        phase = self.phase(chat)
        if phase == "unreadable":
            return {"error": "conversation-root-unverified", "title": "x"}
        turns = [
            {
                "id": chat.turn,
                "user_go": False,
                "assistant_state": "cancelled" if chat.kind == "failed" else "complete",
                "generation_active": False,
                "assistant_started": True,
                "assistant_present": chat.kind != "failed",
            }
        ]
        # A pending platform approval still reads as in_progress with no Stop: the monitor sample
        # carries no card knowledge, so patrol must not let it re-pin the account.
        running = phase in ("working", "approval")
        if chat.go_turn:
            started = self.clock.mono() >= chat.go_at + chat.reply_delay
            turns.append(
                {
                    "id": chat.go_turn,
                    "user_go": True,
                    "assistant_state": ("in_progress" if running else "complete")
                    if started
                    else "unknown",
                    "generation_active": running and started,
                    "assistant_started": started,
                    "assistant_present": started and not running,
                }
            )
        status = {
            "failed": "Thinking failed",
            "working": "working",
            "approval": "working",
            "quota": "quota-or-rate-limit",
        }.get(phase, "complete")
        return {
            "title": "t",
            "turn": turns[-1]["id"],
            "turns": turns,
            "running": running,
            "stop_visible": phase == "working",
            "status": status,
            "failure_evidence": "",
            "progress_token": f"{turns[-1]['id']}:{status}",
        }

    def apply_go(self, chat: Chat) -> None:
        chat.sends += 1
        chat.go_turn = chat.turn[:-1] + "9"
        chat.go_at = self.clock.mono()


class FakePage:
    def __init__(self, url, world, name="page"):
        self.url = url
        self.world = world
        self.name = name
        self.closed = False
        self.calls: list[str] = []
        self.on_close = None

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.calls.append("goto")
        if self.world.nav_error:
            from playwright.async_api import Error

            raise Error("net::ERR_FAILED")
        if self.world.goto_hook:
            self.world.goto_hook(self, url)
        self.url = self.world.redirects.get(url, url)

    async def close(self, **kwargs):
        self.calls.append("close")
        if self.on_close:
            self.on_close(self)
        self.closed = True

    async def bring_to_front(self):
        self.calls.append("bring_to_front")


class FakeContext:
    def __init__(self, world, urls=()):
        self.world = world
        self.pages = [FakePage(u, world, f"tab{i}") for i, u in enumerate(urls)]
        self.created = 0

    async def new_page(self):
        self.created += 1
        page = FakePage("about:blank", self.world, "created")
        self.pages.append(page)
        return page


class FakeAdapter:
    def __init__(self, io, page, active):
        self.io, self.page, self.active = io, page, active

    async def observe(self):
        chat = self.io.world.state(self.page.url)
        if chat is None:
            return Observation("", "", "")
        return self.io.world.observation(chat, verified=self.active)

    async def send(self, expected):
        world = self.io.world
        chat = world.state(self.page.url)
        if world.crash_on_send is not None:
            raise world.crash_on_send
        if chat is None or world.phase(chat) != "failed" or chat.turn != expected.turn:
            return MutationRefusal(world.observation(chat, True)) if chat else False
        world.apply_go(chat)
        return True


class FakeIO:
    """Replaces patrol.PageIO; counts reads so a test can prove none happened."""

    def __init__(self, world):
        self.world = world
        self.reads = 0
        self.samples = 0

    async def read(self, page):
        self.reads += 1
        return await FakeAdapter(self, page, False).observe()

    def passive(self, page):
        return FakeAdapter(self, page, False)

    def active(self, page):
        return FakeAdapter(self, page, True)

    async def sample(self, page):
        self.samples += 1
        chat = self.world.state(page.url)
        return self.world.sample(chat) if chat else {"error": "not-a-conversation"}


class FakeTransport:
    def __init__(self, store=None):
        self.reason = ""
        self.seeds = 0

    async def seed(self, page):
        self.seeds += 1


class RecordingTransport(patrol.PatrolTransport):
    """The REAL latch and evidence recorder fed by fake responses; `seed` only counts calls."""

    def __init__(self, store, machine):
        self.seeds = 0
        super().__init__(Mock(), store, machine)

    async def seed(self, page):
        self.seeds += 1

    def respond(self, status, path, retry_after=None):
        headers = {} if retry_after is None else {"retry-after": str(retry_after)}
        url = "https://chatgpt.com" + path
        self.on_response(SimpleNamespace(status=status, url=url, headers=headers))


@dataclass
class Rig:
    patrol: patrol.Patrol
    world: World
    clock: Clock
    store: Store
    rotation: rotation.Rotation
    context: FakeContext
    io: FakeIO
    transport: FakeTransport
    stop: asyncio.Event
    tmp: Path


def make_rig(
    tmp_path,
    monkeypatch,
    *,
    kinds=None,
    urls=(),
    apply=True,
    enroll=CHATS,
    account="home",
    confirm_wait=45.0,
    seed=7,
    interval=(240.0, 360.0),
    clock=None,
    real_transport=False,
    world=None,
    context=None,
    **config,
):
    """A patrol over fakes. Time is fake everywhere the real code reads a clock."""
    clock = clock or Clock()
    for module in (core, live, patrol):
        monkeypatch.setattr(module, "time", clock.clock_module)
    monkeypatch.setattr(live, "available_memory", lambda: 8 * 1024**3)
    world = world or World(clock, kinds)
    store = Store(tmp_path / account / "state.sqlite", account=account)
    for chat in enroll:
        store.enroll(chat)
    machine = rotation.Rotation.open(
        rotation.rotation_path(store.path),
        account,
        interval=interval,
        clock=clock.wall,
        rng=random.Random(seed),
    )
    context = context or FakeContext(world, urls)
    transport = (
        RecordingTransport(store, machine) if real_transport else FakeTransport()
    )
    io = FakeIO(world)
    stop = asyncio.Event()
    settings = patrol.PatrolConfig(apply=apply, confirm_wait=confirm_wait, **config)
    machine_patrol = patrol.Patrol(
        store=store,
        guardian=Guardian(store, apply=apply),
        rotation=machine,
        context=context,
        transport=transport,
        io=io,
        config=settings,
        stop=stop,
        sleep=clock.sleep,
        watch_urls=dict(URLS),
    )
    return Rig(
        machine_patrol,
        world,
        clock,
        store,
        machine,
        context,
        io,
        transport,
        stop,
        tmp_path,
    )


async def advance_to_due(rig: Rig) -> None:
    """Move fake time to the exact due instant of the rotation."""
    rig.clock.t = max(rig.clock.t, rig.rotation.due_at() - WALL0)


def attempts(store: Store) -> list[tuple]:
    with store.connect() as db:
        return db.execute(
            "SELECT chat,turn,generation,state FROM attempts ORDER BY wall"
        ).fetchall()
