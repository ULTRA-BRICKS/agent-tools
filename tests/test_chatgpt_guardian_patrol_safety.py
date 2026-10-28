"""Single-tab patrol safety: one-tab ceiling, tab consolidation, 429 cooldown, crash idempotency.

Same synthetic world as test_chatgpt_guardian_patrol: fakes only, fake time everywhere.
"""

import asyncio
import json
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agenttools_chatgpt import live, patrol, rotation
from agenttools_chatgpt.continuation import Continuations, ContinueRequest
from agenttools_chatgpt.core import Store
from guardian_patrol_fakes import (
    CHATS,
    LOGIN,
    SPACE,
    URLS,
    USER_DRAFT,
    WALL0,
    Crash,
    advance_to_due,
    attempts,
    make_rig,
)

USER_CHAT = "https://chatgpt.com/c/ffffffff-0000-4000-8000-ffffffffffff"
REQUEST = "11111111-1111-4111-8111-111111111111"


def run(case):
    return asyncio.run(case)


async def visit_once(rig):
    await advance_to_due(rig)
    return await rig.patrol.tick()


def failed(*which):
    return {CHATS[i]: "failed" for i in which}


def rotation_file(rig):
    return json.loads(rotation.rotation_path(rig.store.path).read_text())


# --- one-tab ceiling -----------------------------------------------------------------------------


def test_exactly_one_tab_is_opened_and_it_is_reused_for_every_visit(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        for _ in range(len(CHATS) + 1):
            await visit_once(rig)
        assert rig.context.created == 1
        assert len(rig.context.pages) == 1
        assert rig.context.pages[0].calls == ["goto"] * (len(CHATS) + 1)

    run(case())


def test_an_existing_owned_tab_is_adopted_and_no_second_tab_is_ever_opened(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, urls=(URLS[CHATS[3]],))
        for _ in range(4):
            await visit_once(rig)
        assert rig.context.created == 0 and len(rig.context.pages) == 1

    run(case())


def test_a_protected_draft_tab_pauses_the_patrol_without_opening_a_second_tab(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, urls=(URLS[CHATS[2]],))
        rig.world.chats[CHATS[2]].draft = USER_DRAFT
        page = rig.context.pages[0]
        report = await visit_once(rig)
        assert report["health"] == "protected-tab" and report["blocked"] == "draft"
        assert report["visit"] is None
        assert page.calls == [] and rig.context.created == 0
        assert rig.world.chats[CHATS[2]].draft == USER_DRAFT  # byte for byte
        assert rig.world.chats[CHATS[2]].sends == 0
        # The pause is re-evaluated one interval later and still never opens a second tab.
        again = await visit_once(rig)
        assert again["blocked"] == "draft" and rig.context.created == 0
        assert 240.0 <= rig.rotation.due_at() - rig.clock.wall() <= 360.0

    run(case())


def test_a_user_draft_in_the_visited_chat_is_never_sent_over(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.world.chats[CHATS[0]].draft = USER_DRAFT
        report = await visit_once(rig)
        assert report["visit"]["outcome"] == "draft"
        assert rig.world.chats[CHATS[0]].draft == USER_DRAFT
        assert rig.world.chats[CHATS[0]].sends == 0
        assert rig.store.controls()["stop:" + CHATS[0]] == "human draft"
        # The tab now holds the draft and is protected from navigation.
        later = await visit_once(rig)
        assert later["blocked"] == "draft" and later["visit"] is None
        assert rig.context.pages[0].calls == ["goto"]

    run(case())


def test_a_low_memory_or_budget_pause_opens_no_tab(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        monkeypatch.setattr(live, "available_memory", lambda: 512 * 1024**2)
        report = await visit_once(rig)
        assert report["visit"] is None
        assert report["blocked"] == "resource-paused:low-memory"
        assert rig.context.created == 0

    run(case())


def test_auxiliary_tabs_never_count_as_the_patrol_tab(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        rig.context.pages.extend(
            [
                SimpleNamespace(url=SPACE, is_closed=lambda: False),
                SimpleNamespace(url="about:blank", is_closed=lambda: False),
            ]
        )
        report = await visit_once(rig)
        assert report["visit"]["chat"] == CHATS[0]
        assert (
            rig.context.created == 1
        )  # one patrol tab was added next to the user's pages

    run(case())


# --- consolidation -------------------------------------------------------------------------------


def consolidation_rig(tmp_path, monkeypatch):
    urls = (
        URLS[CHATS[0]],  # the patrol tab (the next target)
        URLS[CHATS[1]],  # safe extra
        URLS[CHATS[2]],  # extra with a user draft
        URLS[CHATS[3]],  # extra with a running response
        USER_CHAT,  # an ordinary user conversation tab that is not enrolled
        SPACE,  # an ordinary auxiliary page
    )
    rig = make_rig(tmp_path, monkeypatch, urls=urls, kinds={CHATS[3]: "working"})
    rig.world.chats[CHATS[2]].draft = USER_DRAFT
    return rig


def test_extra_owned_tabs_are_consolidated_only_when_proven_safe(tmp_path, monkeypatch):
    async def case():
        rig = consolidation_rig(tmp_path, monkeypatch)
        pages = {p.url: p for p in rig.context.pages}
        await (
            rig.patrol.tick()
        )  # not due yet: consolidation runs regardless of the schedule
        assert pages[URLS[CHATS[1]]].closed  # the only provably safe extra
        for url in (URLS[CHATS[0]], URLS[CHATS[2]], URLS[CHATS[3]], USER_CHAT, SPACE):
            assert not pages[url].closed, url
            assert pages[url].calls == []
        assert rig.world.chats[CHATS[2]].draft == USER_DRAFT
        assert rig.context.created == 0
        assert rotation_file(rig)["tabs"]["protected"] == sorted([CHATS[2], CHATS[3]])

    run(case())


def test_every_owned_url_is_persisted_before_any_tab_is_closed(tmp_path, monkeypatch):
    async def case():
        rig = consolidation_rig(tmp_path, monkeypatch)
        snapshots = []
        for page in rig.context.pages:
            page.on_close = lambda p: snapshots.append(rotation_file(rig)["owned_urls"])
        await rig.patrol.tick()
        assert len(snapshots) == 1
        owned = {URLS[c] for c in CHATS[:4]}
        assert owned <= set(snapshots[0])
        assert USER_CHAT not in snapshots[0] and SPACE not in snapshots[0]

    run(case())


def test_consolidation_never_closes_a_tab_with_a_pending_go_or_user_tabs(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, urls=(URLS[CHATS[0]], URLS[CHATS[1]], USER_CHAT)
        )
        rig.rotation.begin_go(CHATS[1], "t1")  # a send in flight on the extra tab
        await rig.patrol.tick()
        assert not any(p.closed for p in rig.context.pages)

    run(case())


def test_duplicate_tabs_of_one_chat_collapse_to_one(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, urls=(URLS[CHATS[0]], URLS[CHATS[0]]))
        await rig.patrol.tick()
        assert [p.closed for p in rig.context.pages].count(True) == 1

    run(case())


def test_an_active_login_flow_blocks_consolidation_navigation_and_new_tabs(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, urls=(LOGIN,))
        report = await visit_once(rig)
        assert report["health"] == "login-flow" and report["visit"] is None
        assert rig.context.created == 0 and rig.context.pages[0].calls == []
        assert rig.io.reads == 0

    run(case())


# --- 429 and cooldown ----------------------------------------------------------------------------


def machine_hold(rig, seconds):
    rig.store.stop("*", patrol.MACHINE_429_HOLD)
    rig.rotation.set_cooldown(
        rig.clock.wall() + seconds, "http-429", "retry-after", patrol.MACHINE_429_HOLD
    )


def untouched(rig):
    return (
        rig.io.reads == 0
        and rig.io.samples == 0
        and rig.context.created == 0
        and rig.transport.seeds == 0
        and all(page.calls == [] for page in rig.context.pages)
        and sum(chat.sends for chat in rig.world.chats.values()) == 0
    )


def test_a_429_cooldown_blocks_reads_navigation_and_go_until_it_ends(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0), urls=(URLS[CHATS[0]],))
        machine_hold(rig, 1000)
        due = rig.rotation.due_at()
        # The cooldown outlasts the draw: the visit is deferred to one fresh draw AFTER it ends.
        assert WALL0 + 1000 + 240 <= due <= WALL0 + 1000 + 360
        assert rig.rotation.deferred["reason"] == "cooldown"
        for moment in (240, 500, 999.999):
            rig.clock.t = moment
            report = await rig.patrol.tick()
            assert report["health"] == "cooldown" and report["visit"] is None
        assert untouched(rig)
        assert rig.store.controls()["stop:*"] == patrol.MACHINE_429_HOLD

    run(case())


def test_at_the_exact_cooldown_end_the_clock_alone_releases_nothing_and_rushes_nothing(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.store.quota()  # an unrelated quota latch must survive
        rig.store.stop(CHATS[4], "pet project")  # and so must a per-chat pause
        machine_hold(rig, 1000)
        rig.clock.t = 1000.0
        report = await rig.patrol.tick()
        controls = rig.store.controls()
        assert (
            controls["stop:*"] == patrol.MACHINE_429_HOLD
        )  # an elapsed clock is no evidence
        assert controls["stop:" + CHATS[4]] == "pet project"
        assert "astra_quota" in controls
        assert report["visit"] is None and report["health"].startswith("stopped")
        assert untouched(rig)  # nothing read, navigated or sent

    run(case())


def test_a_user_pause_is_never_released_by_an_elapsed_cooldown(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.store.stop("*", "owner paused everything")
        rig.rotation.set_cooldown(
            rig.clock.wall() + 10, "http-429", "retry-after", patrol.MACHINE_429_HOLD
        )
        rig.clock.t = 5000.0
        report = await rig.patrol.tick()
        assert report["health"].startswith("stopped")
        assert rig.store.controls()["stop:*"] == "owner paused everything"
        assert untouched(rig)

    run(case())


def test_a_legacy_429_hold_without_cooldown_evidence_needs_a_manual_resume(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.store.stop("*", patrol.MACHINE_429_HOLD)
        rig.clock.t = 100000.0
        report = await rig.patrol.tick()
        assert report["health"].startswith("stopped")
        assert rig.store.controls()["stop:*"] == patrol.MACHINE_429_HOLD
        assert untouched(rig)

    run(case())


def test_a_cooldown_still_binds_after_an_owner_resume(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        machine_hold(rig, 1000)
        rig.store.resume("*")
        rig.clock.t = 500.0
        report = await rig.patrol.tick()
        assert report["health"] == "cooldown" and untouched(rig)

    run(case())


def test_the_machine_hold_text_matches_what_the_transport_guard_writes(tmp_path):
    store = Store(tmp_path / "s.sqlite", account="home")
    guard = live.TransportGuard(Mock(), store)
    guard.record("https://chatgpt.com/backend-api/conversation", 429)
    assert store.controls()["stop:*"] == patrol.MACHINE_429_HOLD


def response(status, headers=None, url="https://chatgpt.com/backend-api/conversation"):
    return SimpleNamespace(status=status, url=url, headers=headers or {})


@pytest.mark.parametrize(
    "headers, seconds, source",
    [
        ({"retry-after": "120"}, 120.0, "retry-after"),
        ({"retry-after": "5"}, patrol.COOLDOWN_FLOOR, "retry-after"),
        ({"retry-after": "999999999"}, patrol.COOLDOWN_CEILING, "retry-after"),
        ({}, patrol.DEFAULT_COOLDOWN, "default"),
        ({"retry-after": "soon"}, patrol.DEFAULT_COOLDOWN, "default"),
        ({"retry-after": "-30"}, patrol.DEFAULT_COOLDOWN, "default"),
    ],
)
def test_the_transport_guard_turns_a_real_retry_after_into_a_durable_cooldown(
    tmp_path, headers, seconds, source
):
    store = Store(tmp_path / "s.sqlite", account="home")
    machine = rotation.Rotation.open(
        tmp_path / "r.json", "home", clock=lambda: WALL0, rng=random.Random(1)
    )
    guard = patrol.PatrolTransport(Mock(), store, machine)
    guard.on_response(response(429, headers))
    assert machine.cooldown["until"] == WALL0 + seconds
    assert machine.cooldown["source"] == source
    assert machine.cooldown["hold"] == patrol.MACHINE_429_HOLD
    assert store.controls()["stop:*"] == patrol.MACHINE_429_HOLD
    assert (
        rotation.Rotation.open(
            tmp_path / "r.json", "home", clock=lambda: WALL0 + 1
        ).cooldown
        == machine.cooldown
    )


def test_retry_after_may_be_an_http_date(tmp_path):
    store = Store(tmp_path / "s.sqlite", account="home")
    machine = rotation.Rotation.open(
        tmp_path / "r.json", "home", clock=lambda: WALL0, rng=random.Random(1)
    )
    guard = patrol.PatrolTransport(Mock(), store, machine)
    when = "Fri, 15 Jan 2027 08:05:00 GMT"  # WALL0 is 2027-01-15T08:00:00Z
    guard.on_response(response(429, {"retry-after": when}))
    assert machine.cooldown["until"] == pytest.approx(WALL0 + 300)


@pytest.mark.parametrize(
    "status, url",
    [
        (200, "https://chatgpt.com/backend-api/conversation"),
        (429, "https://example.com/backend-api/conversation"),
        (429, "https://chatgpt.com/static/app.js"),
        (500, "https://chatgpt.com/backend-api/conversation"),
    ],
)
def test_unrelated_responses_never_start_a_cooldown(tmp_path, status, url):
    store = Store(tmp_path / "s.sqlite", account="home")
    machine = rotation.Rotation.open(
        tmp_path / "r.json", "home", clock=lambda: WALL0, rng=random.Random(1)
    )
    patrol.PatrolTransport(Mock(), store, machine).on_response(
        response(status, url=url)
    )
    assert machine.cooldown is None and "stop:*" not in store.controls()


# --- crash idempotency ---------------------------------------------------------------------------


def restarted(rig, monkeypatch, tmp_path, **kwargs):
    """A new service process over the same durable files, browser, world and timeline."""
    rig.clock.advance(30)
    return make_rig(
        tmp_path,
        monkeypatch,
        clock=rig.clock,
        world=rig.world,
        context=rig.context,
        **kwargs,
    )


def test_crash_between_persist_and_click_is_not_repeated_after_restart(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.world.crash_on_send = Crash()
        with pytest.raises(Crash):
            await visit_once(rig)
        assert (
            rotation_file(rig)["go"]["state"] == "sending"
        )  # persisted BEFORE the click
        assert rig.world.chats[CHATS[0]].sends == 0
        rig.world.crash_on_send = None
        again = restarted(rig, monkeypatch, tmp_path, kinds=failed(0))
        assert again.rotation.go["state"] == "interrupted"
        for _ in range(len(CHATS)):
            await visit_once(again)
        assert again.world.chats[CHATS[0]].sends == 0  # the identity stays consumed
        assert [a[3] for a in attempts(again.store)] == ["uncertain"]

    run(case())


def test_a_hard_kill_with_a_reserved_attempt_never_sends_again(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        chat = rig.world.chats[CHATS[0]]
        observed = rig.world.observation(chat, verified=True)
        assert rig.store.reserve(observed, rig.clock.wall()) == "reserved"
        rig.rotation.begin_go(
            CHATS[0], chat.turn
        )  # killed before anything else happened
        again = restarted(rig, monkeypatch, tmp_path, kinds=failed(0))
        for _ in range(len(CHATS)):
            report = await visit_once(again)
        assert chat.sends == 0
        assert again.rotation.go["state"] == "interrupted"
        assert [a[3] for a in attempts(again.store)] == ["reserved"]
        assert report["visit"]["outcome"] != "failed:sent"

    run(case())


def test_crash_between_send_and_confirmation_resumes_confirmation_without_resending(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        calls = {"n": 0}
        sample = rig.io.sample

        async def dying(page):
            calls["n"] += 1
            if rig.world.chats[CHATS[0]].sends:
                raise Crash()
            return await sample(page)

        rig.io.sample = dying
        with pytest.raises(Crash):
            await visit_once(rig)
        chat = rig.world.chats[CHATS[0]]
        assert chat.sends == 1
        assert [a[3] for a in attempts(rig.store)] == ["sent"]
        assert rotation_file(rig)["go"]["confirmation"] == "unconfirmed"
        again = restarted(rig, monkeypatch, tmp_path, kinds=failed(0))
        for _ in range(len(CHATS)):
            await visit_once(again)
        assert chat.sends == 1
        assert again.rotation.go["confirmation"] in ("running", "completed")
        assert again.rotation.go["response_turn"] == chat.go_turn

    run(case())


# --- approvals -----------------------------------------------------------------------------------


def approval(store, chat, rig):
    now = rig.clock.wall()
    request = ContinueRequest(
        REQUEST,
        chat,
        "22222222-2222-4222-8222-222222222222",
        now - 1,
        now + 900,
    )
    accepted, _ = Continuations(store).submit(request, now)
    assert accepted


def test_an_owner_approval_makes_its_chat_the_next_visit_and_is_dispatched_there(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        seen = []

        async def process(guardian, pages, *args, **kwargs):
            seen.append(sorted(pages))
            Continuations(rig.store).refuse(REQUEST, "consumed-in-test")
            return REQUEST, "sent"

        monkeypatch.setattr(patrol, "process_continuation", process)
        approval(rig.store, CHATS[3], rig)
        report = await visit_once(rig)
        assert report["visit"]["chat"] == CHATS[3]
        assert seen == [[CHATS[3]]]
        assert rig.rotation.cursor == 0  # the lap is not disturbed
        assert (await visit_once(rig))["visit"]["chat"] == CHATS[0]

    run(case())


def test_an_approval_is_never_dispatched_while_the_tab_is_on_another_chat(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        rig.store.stop(CHATS[3], "paused after approval")
        approval(rig.store, CHATS[3], rig)
        process = Mock()
        monkeypatch.setattr(patrol, "process_continuation", process)
        for _ in range(2):
            await visit_once(rig)
        process.assert_not_called()
        record = Continuations(rig.store).get(REQUEST)
        assert (
            record.state == "queued"
        )  # left for its own visit, never refused by mistake

    run(case())


# --- bounded re-reads and stale Go records -----------------------------------------------------


def test_protected_extras_are_not_re_read_every_minute_only_at_the_next_due_visit(
    tmp_path, monkeypatch
):
    async def case():
        rig = consolidation_rig(tmp_path, monkeypatch)
        await rig.patrol.tick()
        reads = rig.io.reads
        for _ in range(3):
            rig.clock.advance(60)
            await (
                rig.patrol.tick()
            )  # still not due: the protected extras are left alone
        assert rig.io.reads == reads
        await visit_once(rig)  # the due visit re-checks them
        assert rig.io.reads > reads

    run(case())


def test_an_old_interrupted_go_record_never_blocks_a_new_failure_of_that_chat(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.rotation.begin_go(CHATS[0], "00000000-0000-4000-8000-0000000000aa")
        rig.rotation.go["state"] = "interrupted"  # an earlier turn's unresolved send
        report = await visit_once(rig)
        # The recovery database, not this record, decides whether THIS turn may be continued.
        assert report["visit"]["outcome"] == "failed:sent"
        assert rig.world.chats[CHATS[0]].sends == 1

    run(case())


def test_an_old_go_record_does_not_pin_an_extra_tab_open(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, urls=(URLS[CHATS[0]], URLS[CHATS[1]]))
        rig.rotation.begin_go(CHATS[1], "00000000-0000-4000-8000-0000000000aa")
        rig.rotation.go["state"] = "interrupted"
        await rig.patrol.tick()
        assert [p.closed for p in rig.context.pages] == [False, True]

    run(case())


def test_replayed_resource_entries_never_start_or_extend_a_cooldown(tmp_path):
    """`seed` replays 429s from the page's last five minutes; they are old events, not new ones."""
    store = Store(tmp_path / "s.sqlite", account="home")
    machine = rotation.Rotation.open(
        tmp_path / "r.json", "home", clock=lambda: WALL0, rng=random.Random(1)
    )
    guard = patrol.PatrolTransport(Mock(), store, machine)
    guard.record(
        "https://chatgpt.com/backend-api/conversation", 429
    )  # what seed() calls
    assert machine.cooldown is None
    assert (
        store.controls()["stop:*"] == patrol.MACHINE_429_HOLD
    )  # still latched, manual resume
    machine.set_cooldown(WALL0 + 60, "http-429", "retry-after", patrol.MACHINE_429_HOLD)
    guard.record("https://chatgpt.com/backend-api/conversation", 429)
    assert (
        machine.cooldown["until"] == WALL0 + 60
    )  # a replay never stretches it to 15 minutes


@pytest.mark.parametrize("value, expected", [(0, 240.0), (None, 240.0)])
def test_a_zero_interval_flag_is_refused_not_silently_defaulted(value, expected):
    args = SimpleNamespace(
        rotation_min=value, rotation_max=None, confirm_wait=None, poll_interval=60
    )
    if value is None:
        assert patrol._setting(args, "rotation_min", 240.0) == expected
    else:
        with pytest.raises(rotation.RotationError):
            rotation.validate_interval(
                patrol._setting(args, "rotation_min", 240.0), 360.0
            )


# --- a running response is never navigated away from ---------------------------------------------
#
# Navigating away from a tab with an active response has no permitted live proof, so the patrol
# defers instead. Everything here is an offline fixture: a fake page that raises on any click.

RUNNING = CHATS[3]


def running_rig(tmp_path, monkeypatch, **kwargs):
    kwargs.setdefault("urls", (URLS[RUNNING],))
    kwargs.setdefault("kinds", {RUNNING: "working", **failed(0)})
    return make_rig(tmp_path, monkeypatch, **kwargs)


def test_a_running_tab_is_not_navigated_not_sent_to_and_gets_no_second_tab(
    tmp_path, monkeypatch
):
    async def case():
        rig = running_rig(tmp_path, monkeypatch)
        page = rig.context.pages[0]
        report = await visit_once(rig)
        assert (
            page.url == URLS[RUNNING] and page.calls == []
        )  # no goto, no click, no Stop
        assert rig.context.created == 0 and len(rig.context.pages) == 1
        assert sum(chat.sends for chat in rig.world.chats.values()) == 0
        assert report["health"] == "protected-tab" and report["blocked"] == "running"
        assert report["visit"] is None and attempts(rig.store) == []

    run(case())


def test_the_running_deferral_is_persisted_and_visible_in_the_telemetry(
    tmp_path, monkeypatch
):
    async def case():
        rig = running_rig(tmp_path, monkeypatch)
        await visit_once(rig)
        now = rig.clock.wall()
        stored = rotation_file(rig)
        assert stored["deferred"] == {"reason": "running-protected", "at": now}
        assert stored["last_visit"]["outcome"] == "running-protected"
        assert stored["last_visit"]["chat"] is None
        view = rotation.Rotation.read(
            rotation.rotation_path(rig.store.path), "home", clock=rig.clock.wall
        ).snapshot(rig.store.controls())
        assert view["deferred"]["reason"] == "running-protected"
        assert view["last_visit"]["outcome"] == "running-protected"
        assert view["current"] is None  # nothing was visited

    run(case())


def test_a_deferred_slot_is_followed_by_one_fresh_interval_not_a_catch_up_burst(
    tmp_path, monkeypatch
):
    async def case():
        rig = running_rig(tmp_path, monkeypatch)
        page = rig.context.pages[0]
        reads = []
        for _ in range(4):  # the response keeps running through four slots
            await advance_to_due(rig)
            started = rig.clock.wall()
            assert (await rig.patrol.tick())["blocked"] == "running"
            assert 240.0 <= rig.rotation.due_at() - started <= 360.0
            reads.append(started)
            assert not rig.rotation.is_due()  # never due at once
            assert (await rig.patrol.tick())[
                "visit"
            ] is None  # and not visited meanwhile
        assert page.calls == [] and len(set(reads)) == 4
        assert rig.rotation.last_visit["started_at"] is None  # no visit ever started

    run(case())


def test_once_the_response_has_finished_the_next_due_visit_proceeds(
    tmp_path, monkeypatch
):
    async def case():
        rig = running_rig(tmp_path, monkeypatch)
        page = rig.context.pages[0]
        await visit_once(rig)
        assert page.calls == []
        rig.world.chats[RUNNING].kind = "complete"  # the response finished
        before = rig.clock.wall()
        rig.clock.advance(
            60
        )  # not due yet: nothing happens, even though the tab is idle now
        assert (await rig.patrol.tick())["visit"] is None and page.calls == []
        report = await visit_once(rig)
        assert report["visit"]["chat"] == CHATS[0] and page.calls == ["goto"]
        assert rig.rotation.deferred is None
        assert rig.rotation.last_visit["started_at"] > before

    run(case())


def test_a_running_tab_on_its_own_target_is_read_not_deferred(tmp_path, monkeypatch):
    async def case():
        rig = running_rig(tmp_path, monkeypatch, enroll=(RUNNING,))
        page = rig.context.pages[0]
        report = await visit_once(rig)
        assert report["visit"] == {"chat": RUNNING, "outcome": "working"}
        assert page.calls == []  # no navigation was needed, none happened
        assert rig.rotation.deferred is None

    run(case())


def test_a_draft_and_a_running_response_are_both_protected(tmp_path, monkeypatch):
    async def case():
        both = running_rig(tmp_path / "both", monkeypatch)
        both.world.chats[RUNNING].draft = USER_DRAFT
        report = await visit_once(both)
        assert report["blocked"] == "draft"
        assert both.rotation.last_visit["outcome"] == "protected:draft"
        assert both.context.pages[0].calls == []
        assert both.world.chats[RUNNING].draft == USER_DRAFT
        only_draft = make_rig(tmp_path / "draft", monkeypatch, urls=(URLS[RUNNING],))
        only_draft.world.chats[RUNNING].draft = USER_DRAFT
        assert (await visit_once(only_draft))["blocked"] == "draft"

    run(case())


def test_the_running_deferral_survives_a_restart_and_the_tab_stays_put(
    tmp_path, monkeypatch
):
    async def case():
        rig = running_rig(tmp_path, monkeypatch)
        await visit_once(rig)
        again = restarted(rig, monkeypatch, tmp_path, kinds={RUNNING: "working"})
        assert again.rotation.deferred["reason"] == "running-protected"
        report = await visit_once(again)
        assert report["blocked"] == "running"
        assert again.context.pages[0].calls == [] and again.context.created == 0

    run(case())


def test_consolidation_keeps_a_running_tab_and_closes_only_the_idle_extra(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path,
            monkeypatch,
            urls=(URLS[RUNNING], URLS[CHATS[4]]),
            kinds={RUNNING: "working"},
        )
        running, idle = rig.context.pages
        await rig.patrol.tick()
        assert idle.closed and not running.closed
        assert running.calls == [] and idle.calls == ["close"]

    run(case())
