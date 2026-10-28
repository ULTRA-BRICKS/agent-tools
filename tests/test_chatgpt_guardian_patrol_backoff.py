"""Adaptive backoff through the single-tab patrol: 429 pressure, paced probes, fair retries.

A synthetic backend answers every navigation of the fake page (429, 5xx, timeout or 200) and feeds
the REAL transport recorder, so the cooldown, hold, evidence and rotation code all run; nothing
here touches a browser, a network or a live state. A test that counts `Server.calls` proves how
many programmatic page loads the patrol issued.
"""

import asyncio
import json
from email.utils import formatdate
from itertools import pairwise

import pytest
from agenttools_chatgpt import backoff, patrol, rotation, rotation_view
from agenttools_chatgpt.urls import conversation_identity
from guardian_patrol_fakes import CHATS, URLS, WALL0, attempts, make_rig

HOLD = patrol.MACHINE_429_HOLD
LIST = "/backend-api/conversations"
LOGIN = "https://chatgpt.com/auth/login"
DAY = 86400.0


def read_path(chat):
    return "/backend-api/conversation/" + chat.rsplit("/", 1)[1]


def run(case):
    return asyncio.run(case)


class Server:
    """The fake backend: `answer(chat, t, n)` -> (status, retry_after); status "timeout" raises."""

    def __init__(self, rig, answer):
        self.rig = rig
        self.answer = answer
        self.calls: list[tuple[float, str, object]] = []
        rig.world.goto_hook = self.on_goto

    def on_goto(self, _page, url):
        from playwright.async_api import Error

        chat = conversation_identity(url)
        t = self.rig.clock.t
        status, retry_after = self.answer(chat, t, len(self.calls))
        self.calls.append((t, chat, status))
        if status == "timeout":
            raise Error("Timeout 30000ms exceeded")
        if status == "unrelated":
            self.rig.transport.respond(200, LIST)
            self.rig.transport.respond(200, "/backend-api/me")
            return
        self.rig.transport.respond(status, read_path(chat), retry_after)
        if status == 200:
            self.rig.transport.respond(200, LIST)

    def chats(self):
        return [chat for _t, chat, _s in self.calls]


def rig_with(tmp_path, monkeypatch, **kwargs):
    kwargs.setdefault("enroll", CHATS[:3])
    return make_rig(tmp_path, monkeypatch, real_transport=True, **kwargs)


def held(rig):
    return rig.store.controls().get("stop:*")


def sends(rig):
    return sum(chat.sends for chat in rig.world.chats.values())


async def due_tick(rig):
    """One tick at the exact due instant of the rotation (the cooldown included)."""
    rig.clock.t = max(rig.clock.t, rig.rotation.due_at() - WALL0)
    return await rig.patrol.tick()


async def pump(rig, until, steps=300):
    """Tick at every due instant up to `until` rig seconds; stop where no clock move helps."""
    reports = []
    for _ in range(steps):
        due = rig.rotation.due_at() - WALL0
        if due > until:
            break
        if reports and due <= rig.clock.t and not reports[-1]["visit"]:
            break  # a stop or protected state: the clock cannot change it
        rig.clock.t = max(rig.clock.t, due)
        reports.append(await rig.patrol.tick())
    return reports


def cooldown_seconds(rig):
    return rig.rotation.cooldown["until"] - rig.clock.wall()


def restarted(rig, monkeypatch, tmp_path, *, seconds=30, **kwargs):
    rig.clock.advance(seconds)
    kwargs.setdefault("enroll", CHATS[:3])
    return make_rig(
        tmp_path,
        monkeypatch,
        real_transport=True,
        clock=rig.clock,
        world=rig.world,
        context=rig.context,
        **kwargs,
    )


def first_n_429(n, retry_after=None):
    return lambda chat, t, count: (429, retry_after) if count < n else (200, None)


# --- 429 -> backoff -> 429 -> longer -> 2xx -> the round robin continues -------------------------


def failing_first(chat_index, count, retry_after=None):
    """Only one chat's read endpoint answers 429, for its first `count` loads; all else is 200."""
    seen = {"n": 0}

    def answer(chat, _t, _count):
        if chat != CHATS[chat_index]:
            return 200, None
        seen["n"] += 1
        return (429, retry_after) if seen["n"] <= count else (200, None)

    return answer


def test_429_then_a_longer_429_then_2xx_continues_the_round_robin(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        server = Server(rig, failing_first(0, 2, 15))
        first = await due_tick(rig)
        assert first["visit"] == {"chat": CHATS[0], "outcome": "cooldown"}
        wait_one = cooldown_seconds(rig)
        assert 600 <= wait_one <= 750  # the ladder, not the 15 s Retry-After
        assert held(rig) == HOLD and rig.rotation.pressure.level == 1
        snap = rotation_view.block(
            rig.store.path, "home", rig.store.controls(), clock=rig.clock.wall
        )
        assert snap["coverage"]["complete"] is False
        assert snap["coverage"]["retry_pending"] == 1
        page = rig.context.pages[0]
        start = rig.clock.t
        for offset in (1.0, 60.0, 300.0, wait_one - 0.5):
            rig.clock.t = start + offset
            report = await rig.patrol.tick()
            assert report["health"] == "cooldown" and report["visit"] is None
        assert len(server.calls) == 1 and page.calls == ["goto"]  # fully silent

        second = await due_tick(rig)  # the slot after a failure goes to the next chat
        assert second["visit"] == {"chat": CHATS[1], "outcome": "complete"}
        assert (
            held(rig) == HOLD and rig.rotation.pressure.level == 1
        )  # a neighbour clears nothing

        third = await due_tick(rig)  # the paced controlled probe of the failed target
        assert third["visit"] == {"chat": CHATS[0], "outcome": "cooldown"}
        wait_two = cooldown_seconds(rig)
        assert 1200 <= wait_two <= 1500 and wait_two > wait_one
        assert rig.rotation.pressure.level == 2 and held(rig) == HOLD

        fourth = await due_tick(rig)
        assert fourth["visit"] == {"chat": CHATS[2], "outcome": "complete"}
        assert held(rig) == HOLD

        fifth = await due_tick(rig)
        assert (
            fifth["visit"]["chat"] == CHATS[0]
            and fifth["visit"]["outcome"] == "complete"
        )
        assert held(rig) is None and rig.rotation.cooldown is None
        assert rig.rotation.queue[0].retry is None
        assert rig.rotation.queue[0].confirmed_at is not None
        more = [await due_tick(rig) for _ in range(2)]
        assert {r["visit"]["chat"] for r in more} <= set(CHATS[:3])
        assert server.chats()[:5] == [CHATS[0], CHATS[1], CHATS[0], CHATS[2], CHATS[0]]
        assert (
            rig.rotation.pressure.level >= 1
        )  # recovery is gradual: never straight to zero
        assert sends(rig) == 0
        done = rotation_view.block(
            rig.store.path, "home", rig.store.controls(), clock=rig.clock.wall
        )
        assert done["coverage"]["retry_pending"] == 0

    run(case())


@pytest.mark.parametrize("kind", ["seconds", "http-date", "over-a-day"])
def test_a_server_delay_is_never_shortened_or_capped(tmp_path, monkeypatch, kind):
    async def case():
        rig = rig_with(tmp_path, monkeypatch, urls=(URLS[CHATS[0]],))
        server = Server(rig, first_n_429(0))
        rig.clock.t = 10
        asked = {"seconds": 7200.0, "http-date": 5400.0, "over-a-day": 3 * DAY}[kind]
        header = (
            formatdate(WALL0 + 10 + asked, usegmt=True)
            if kind == "http-date"
            else int(asked)
        )
        rig.transport.respond(429, read_path(CHATS[0]), retry_after=header)
        until = rig.rotation.cooldown["until"]
        assert until >= WALL0 + 10 + asked - 1.0  # never shortened
        if kind != "http-date":
            assert until == WALL0 + 10 + asked  # never capped
        for moment in (600.0, 3700.0, asked - 5, asked - 1):
            rig.clock.t = moment
            report = await rig.patrol.tick()
            assert report["health"] == "cooldown" and report["visit"] is None
        assert (
            server.calls == []
            and rig.io.reads == 0
            and rig.context.pages[0].calls == []
        )

    run(case())


# --- an account-wide cooldown never hammers anything; other accounts are independent ---------


def test_a_failing_account_stays_silent_while_the_other_account_proceeds(
    tmp_path, monkeypatch
):
    async def case():
        home = rig_with(tmp_path, monkeypatch, account="home")
        glide = rig_with(
            tmp_path,
            monkeypatch,
            account="glide.vc",
            clock=home.clock,
            enroll=CHATS[:2],
            interval=(120.0, 120.0),
        )
        server = Server(home, first_n_429(99))
        await due_tick(home)
        assert held(home) == HOLD and len(server.calls) == 1
        glide_visits = []
        for _ in range(4):
            report = await due_tick(glide)
            glide_visits.append(report["visit"]["chat"])
            assert (await home.patrol.tick())["health"] == "cooldown"
        assert glide_visits == [CHATS[0], CHATS[1], CHATS[0], CHATS[1]]
        assert len(server.calls) == 1 and held(glide) is None
        assert glide.rotation.pressure.level == 0 and glide.rotation.cooldown is None

    run(case())


# --- a failing chat never starves the others -------------------------------------------------


def test_a_chat_that_keeps_timing_out_stays_enrolled_and_never_starves_the_rest(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        heal = 6 * 3600.0
        server = Server(
            rig,
            lambda chat, t, n: (
                ("timeout", None) if chat == CHATS[0] and t < heal else (200, None)
            ),
        )
        reports = await pump(rig, heal - 1)
        outcomes = [r["visit"]["outcome"] for r in reports if r["visit"]]
        chats = server.chats()
        assert "nav-error:Error" in outcomes and held(rig) is None
        assert rig.rotation.cooldown is None  # a timeout is never a hold
        assert min(chats.count(CHATS[1]), chats.count(CHATS[2])) >= 3
        assert chats.count(CHATS[0]) <= len(chats) // 2 + 1  # at most every other slot
        assert {entry.chat for entry in rig.rotation.queue} == set(CHATS[:3])
        failing = rig.rotation.queue[0]
        assert failing.retry is not None and failing.last_ok_at is None
        assert failing.retry["failures"] >= 3
        assert (
            failing.retry["at"] - rig.clock.wall()
            <= backoff.BackoffConfig().transient_cap * 1.25
        )
        await pump(rig, heal + 4 * 3600)
        assert failing.retry is None and failing.last_ok_at is not None
        assert sends(rig) == 0

    run(case())


def test_a_503_keeps_the_chat_queued_with_a_bounded_retry_and_never_holds(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        server = Server(
            rig,
            lambda chat, t, n: (
                (503, None) if chat == CHATS[0] and n < 1 else (200, None)
            ),
        )
        report = await due_tick(rig)
        assert report["visit"]["chat"] == CHATS[0]
        entry = rig.rotation.queue[0]
        assert entry.retry["reason"] == "http-5xx" and entry.last_ok_at is None
        assert held(rig) is None and rig.rotation.cooldown is None
        await pump(rig, 4 * 3600)
        assert entry.retry is None and entry.last_ok_at is not None
        assert server.chats().count(CHATS[0]) >= 2

    run(case())


# --- a restart keeps the pressure, the cooldown and the due time ------------------------------


def test_a_restart_in_the_middle_of_a_cooldown_keeps_everything_and_stays_silent(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        server = Server(rig, first_n_429(99))
        await due_tick(rig)
        again = restarted(rig, monkeypatch, tmp_path)
        assert again.rotation.pressure == rig.rotation.pressure
        assert again.rotation.cooldown == rig.rotation.cooldown
        assert again.rotation.due_at() == rig.rotation.due_at()
        assert again.rotation.queue[0].retry == rig.rotation.queue[0].retry
        for _ in range(3):
            again.clock.advance(100)
            assert (await again.patrol.tick())["health"] == "cooldown"
        assert len(server.calls) == 1 and held(again) == HOLD

    run(case())


def test_a_restart_after_a_go_keeps_the_pressure_the_due_time_and_never_repeats_the_go(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch, kinds={CHATS[0]: "failed"})
        rig.rotation.pressure.level = 1
        rig.rotation.save()
        report = await due_tick(rig)
        assert report["visit"]["outcome"] == "failed:sent" and sends(rig) == 1
        again = restarted(rig, monkeypatch, tmp_path, kinds={CHATS[0]: "failed"})
        assert again.rotation.due_at() == rig.rotation.due_at()
        assert again.rotation.pressure.level == 1
        for _ in range(4):
            await due_tick(again)
        assert sends(again) == 1 and len(attempts(again.store)) == 1

    run(case())


def test_an_overdue_clock_after_a_long_outage_causes_one_visit_never_a_catch_up_burst(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        Server(rig, first_n_429(1))
        await due_tick(rig)
        again = restarted(rig, monkeypatch, tmp_path, seconds=10 * 3600)
        server = Server(again, first_n_429(0))
        assert again.rotation.deferred["reason"] == "overdue"
        assert (await again.patrol.tick())[
            "visit"
        ] is None  # not due the instant it wakes
        woke = await due_tick(again)
        assert (
            woke["visit"]["chat"] == CHATS[1]
        )  # the slot after the failure is a neighbour's
        again_report = await again.patrol.tick()  # the same instant: nothing more
        assert again_report["visit"] is None and len(server.calls) == 1
        assert held(again) == HOLD  # a neighbour's success never releases the hold
        assert (await due_tick(again))["visit"]["chat"] == CHATS[0]
        assert held(again) is None  # only the failed endpoint's own later 2xx does

    run(case())


# --- the controlled probe -----------------------------------------------------------------------


def test_a_failed_current_page_gets_exactly_one_real_read_only_recheck(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch, urls=(URLS[CHATS[0]],))
        server = Server(rig, first_n_429(0))
        rig.clock.t = 10
        rig.transport.respond(429, read_path(CHATS[0]), retry_after=60)
        page = rig.context.pages[0]
        rig.clock.t = 5000
        report = await rig.patrol.tick()
        assert (
            report["visit"]["chat"] == CHATS[0]
            and report["visit"]["outcome"] == "complete"
        )
        assert (
            page.calls == ["goto"] and len(server.calls) == 1
        )  # one real read, not DOM only
        assert held(rig) is None and rig.rotation.queue[0].retry is None
        assert rig.rotation.pressure.probes == 1 and sends(rig) == 0
        assert attempts(rig.store) == []

    run(case())


def test_an_unrelated_200_never_releases_the_hold_and_the_probe_is_paced(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        answers = iter(["unrelated", "unrelated", 200])
        server = Server(rig, lambda chat, t, n: (next(answers), None))
        rig.transport.respond(429, read_path(CHATS[0]), retry_after=60)
        level = rig.rotation.pressure.level
        first = await due_tick(rig)
        assert first["visit"]["chat"] == CHATS[0] and held(rig) == HOLD
        assert rig.rotation.pressure.level == level  # inconclusive: no strike
        assert rig.rotation.cooldown["reason"] == "hold-recheck"
        quiet = cooldown_seconds(rig)
        assert 600 <= quiet <= 750
        rig.clock.advance(quiet - 1)
        assert (await rig.patrol.tick())["health"] == "cooldown" and len(
            server.calls
        ) == 1
        second = await due_tick(rig)
        assert held(rig) == HOLD and second["visit"]["chat"] == CHATS[0]
        assert len(server.calls) == 2
        await due_tick(rig)
        assert held(rig) is None and len(server.calls) == 3
        assert sends(rig) == 0

    run(case())


def test_several_failed_endpoints_are_probed_in_turn_until_each_recovers(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        server = Server(rig, first_n_429(0))
        rig.clock.t = 10
        rig.transport.respond(429, read_path(CHATS[1]), retry_after=60)
        rig.transport.respond(429, read_path(CHATS[2]), retry_after=60)
        rig.rotation.sync({chat: chat for chat in CHATS[:3]})
        rig.rotation.queue[1].last_visit_at = WALL0 + 1
        rig.rotation.queue[2].last_visit_at = WALL0 + 2
        await due_tick(rig)
        assert held(rig) == HOLD and server.chats() == [CHATS[1]]
        await due_tick(rig)
        assert held(rig) is None and server.chats() == [CHATS[1], CHATS[2]]

    run(case())


@pytest.mark.parametrize("blocker", ["401", "owner-pause", "draft", "running", "login"])
def test_a_protected_or_blocked_state_stops_every_probe_action(
    tmp_path, monkeypatch, blocker
):
    async def case():
        kinds = {CHATS[0]: "working"} if blocker == "running" else None
        urls = (URLS[CHATS[0]], LOGIN) if blocker == "login" else (URLS[CHATS[0]],)
        rig = rig_with(tmp_path, monkeypatch, urls=urls, kinds=kinds)
        server = Server(rig, first_n_429(0))
        rig.clock.t = 10
        rig.transport.respond(429, read_path(CHATS[0]), retry_after=60)
        if blocker == "401":
            rig.transport.respond(401, read_path(CHATS[0]))
        if blocker == "owner-pause":
            rig.store.stop("*", "owner paused everything")
        if blocker == "draft":
            rig.world.chats[CHATS[0]].draft = "half-written thought"
        rig.clock.t = 5000
        report = await rig.patrol.tick()
        assert server.calls == [] and sends(rig) == 0 and attempts(rig.store) == []
        assert rig.context.pages[0].calls == []
        assert held(rig) == {
            "401": "human challenge",
            "owner-pause": "owner paused everything",
        }.get(blocker, HOLD)
        assert rig.rotation.queue[0].last_ok_at is None
        if blocker in ("401", "owner-pause", "login"):
            assert report["visit"] is None
        else:
            assert report["health"] == "protected-tab"
            assert held(rig) == HOLD

    run(case())


def test_a_new_401_after_a_429_hold_is_never_cleared_by_a_later_2xx(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, read_path(CHATS[0]), retry_after=60)
        rig.clock.t = 20
        rig.transport.respond(401, read_path(CHATS[0]))
        rig.clock.t = 30
        rig.transport.respond(200, read_path(CHATS[0]))
        report = await pump(rig, 100000)
        assert held(rig) == "human challenge" and sends(rig) == 0
        assert all(r["visit"] is None for r in report)

    run(case())


# --- a legacy hold ---------------------------------------------------------------------------------


def test_a_legacy_hold_is_unverifiable_probed_read_only_at_the_cap_and_never_cleared(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        server = Server(rig, first_n_429(0))
        rig.store.stop("*", HOLD)
        rig.clock.t = 100
        first = await rig.patrol.tick()
        assert first["health"] == "cooldown" and server.calls == []
        view = rotation_view.block(
            rig.store.path, "home", rig.store.controls(), clock=rig.clock.wall
        )
        assert view["pressure"]["hold_state"] == "legacy-unverifiable"
        assert view["transport"]["unverified"] == [rotation.FOREIGN_HOLD]
        assert cooldown_seconds(rig) == 3600.0
        for expected in (1, 2, 3):
            await due_tick(rig)
            assert len(server.calls) == expected and held(rig) == HOLD
            assert rig.rotation.cooldown["until"] - rig.clock.wall() == 3600.0
        assert sends(rig) == 0

    run(case())


# --- long failure and recovery ---------------------------------------------------------------------


def assert_paced_while_failing(rig, server, heal):
    """During the outage: probes are paced, A never twice in a row, the neighbours are visited."""
    early = [c for c in server.calls if c[0] < heal]
    assert 6 <= len(early) <= 14  # paced probes, never a stream
    failing = [c for c in early if c[1] == CHATS[0]]
    neighbours = {c[1] for c in early} - {CHATS[0]}
    assert len(failing) >= 4 and neighbours == set(CHATS[1:3])  # nobody is starved
    assert all(a[1] != CHATS[0] or b[1] != CHATS[0] for a, b in pairwise(early))
    gaps = [b[0] - a[0] for a, b in pairwise(early)]
    assert (
        min(gaps) >= 240 and gaps[0] < gaps[2]
    )  # every slot is paced, slower with pressure
    retry_gaps = [b[0] - a[0] for a, b in pairwise(failing)]
    assert retry_gaps == sorted(retry_gaps) and min(retry_gaps) >= 1200
    assert rig.rotation.pressure.level == 4 and held(rig) == HOLD
    assert rig.rotation.cooldown["until"] - rig.clock.wall() <= 3600.0


async def recover_after_heal(rig, heal):
    """Tick at each due time until the pressure is back at level 0; the levels seen."""
    levels = []
    for _ in range(300):
        rig.clock.t = max(rig.clock.t, rig.rotation.due_at() - WALL0)
        await rig.patrol.tick()
        levels.append(rig.rotation.pressure.level)
        if rig.rotation.pressure.level == 0 and rig.clock.t > heal:
            break
    return levels


def test_six_hours_of_429s_stay_bounded_then_recovery_is_gradual(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        heal = 6 * 3600.0
        server = Server(
            rig,
            lambda chat, t, n: (
                (429, None) if chat == CHATS[0] and t < heal else (200, None)
            ),
        )
        before = await pump(rig, heal - 1)
        assert_paced_while_failing(rig, server, heal)
        levels = await recover_after_heal(rig, heal)
        assert held(rig) is None and levels[-1] == 0
        assert max(levels) <= 4 and levels == sorted(levels, reverse=True)
        after = [chat for t, chat, _s in server.calls if t >= heal]
        assert set(after) == set(CHATS[:3])  # the round robin is back
        assert rig.rotation.queue[0].retry is None
        assert sends(rig) == 0 and len(before) >= 5

    run(case())


# --- telemetry through the existing view ------------------------------------------------------------


def test_the_status_view_shows_real_pressure_without_writing(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        Server(rig, first_n_429(99))
        await due_tick(rig)
        path = rotation.rotation_path(rig.store.path)
        before = path.read_bytes()
        view = rotation_view.block(
            rig.store.path, "home", rig.store.controls(), clock=rig.clock.wall
        )
        assert path.read_bytes() == before
        pressure = view["pressure"]
        assert pressure["level"] == 1 and pressure["strikes"] == 1
        assert pressure["retry_reason"] == "http-429"
        assert pressure["hold_state"] in ("cooling", "unverified-pending-probe")
        assert pressure["next_due"] == rig.rotation.next_due
        assert pressure["last_confirmed_target_success_at"] is None
        assert pressure["success_rate"] == 0.0  # one observed visit, and it was a 429
        assert json.loads(json.dumps(view))["coverage"]["never_clean"] == 3

    run(case())
