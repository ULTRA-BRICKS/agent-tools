"""Single-tab patrol visits on a synthetic ChatGPT world: failed -> one Go -> confirmed response.

Every browser boundary is a fake (guardian_patrol_fakes); time is fake end to end, including
the clock the real Guardian and the real Store read. No Chrome, network or live state.
"""

import asyncio
import itertools
import json

import pytest
from agenttools_chatgpt import rotation
from guardian_patrol_fakes import (
    CHATS,
    URLS,
    WALL0,
    advance_to_due,
    attempts,
    make_rig,
)


def run(case):
    return asyncio.run(case)


async def visit_once(rig):
    await advance_to_due(rig)
    return await rig.patrol.tick()


def failed(*which):
    return {CHATS[i]: "failed" for i in which}


# --- the loop: failed -> exactly one Go -> new response confirmed ----------------------------------


def test_failed_chat_gets_exactly_one_go_and_the_new_response_is_confirmed(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        report = await visit_once(rig)
        chat = rig.world.chats[CHATS[0]]
        assert report["visit"] == {"chat": CHATS[0], "outcome": "failed:sent"}
        assert chat.sends == 1
        assert attempts(rig.store) == [(CHATS[0], chat.turn, chat.turn, "sent")]
        go = rig.rotation.go
        assert (go["chat"], go["turn"], go["state"]) == (CHATS[0], chat.turn, "sent")
        assert go["confirmation"] == "running"
        assert go["response_turn"] == chat.go_turn  # correlated with the NEXT response
        assert go["confirmed_at"] is not None
        # One owned tab, opened once, navigated to the full permanent project URL.
        assert rig.context.created == 1 and len(rig.context.pages) == 1
        assert rig.context.pages[0].url == URLS[CHATS[0]]
        assert rig.context.pages[0].calls == ["goto"]

    run(case())


def test_a_second_visit_to_the_same_chat_never_repeats_the_go(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        await visit_once(rig)
        for _ in range(len(CHATS) + 2):  # a full lap and then some
            await visit_once(rig)
        assert rig.world.chats[CHATS[0]].sends == 1
        assert [a[3] for a in attempts(rig.store)] == ["sent"]
        assert rig.rotation.queue[0].visits >= 2

    run(case())


def test_the_same_failed_head_is_deduplicated_not_resent(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        await visit_once(rig)
        chat = rig.world.chats[CHATS[0]]
        chat.go_turn, chat.go_at = "", None  # the very same failed head shows again
        for _ in range(len(CHATS)):
            await visit_once(rig)
        assert chat.sends == 1
        assert len(attempts(rig.store)) == 1

    run(case())


def test_observe_only_patrol_navigates_and_reads_but_never_sends(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0), apply=False)
        report = await visit_once(rig)
        assert report["visit"]["outcome"] == "failed:observe-only"
        assert rig.world.chats[CHATS[0]].sends == 0
        assert attempts(rig.store) == []
        assert rig.rotation.go is None

    run(case())


@pytest.mark.parametrize(
    "kind, outcome",
    [("working", "working"), ("complete", "complete"), ("quota", "rate-limited")],
)
def test_working_finished_and_rate_limited_chats_never_receive_a_go(
    tmp_path, monkeypatch, kind, outcome
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds={CHATS[0]: kind})
        report = await visit_once(rig)
        assert report["visit"] == {"chat": CHATS[0], "outcome": outcome}
        assert rig.world.chats[CHATS[0]].sends == 0
        assert attempts(rig.store) == []
        assert rig.rotation.go is None

    run(case())


def test_a_final_answer_is_not_blindly_resumed_even_when_the_task_text_is_unfinished(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds={CHATS[0]: "complete"})
        rig.world.chats[CHATS[0]].ambiguous = False
        for _ in range(3):
            await visit_once(rig)
        assert sum(chat.sends for chat in rig.world.chats.values()) == 0

    run(case())


def test_unverified_model_is_never_continued(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.world.chats[CHATS[0]].model = ""
        report = await visit_once(rig)
        assert rig.world.chats[CHATS[0]].sends == 0
        assert report["visit"]["outcome"].startswith("failed:")
        assert report["visit"]["outcome"] != "failed:sent"

    run(case())


# --- refusals ------------------------------------------------------------------------------------


def test_a_changed_head_before_the_click_refuses_and_keeps_the_identity_consumed(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        # 8 stability samples (t=0..21 s) then the 9th active read is the pre-send re-observation.
        rig.world.head_change_on_active_read = {CHATS[0]: 9}
        report = await visit_once(rig)
        assert rig.world.chats[CHATS[0]].sends == 0
        assert report["visit"]["outcome"] == "failed:safety-stop-before-send"
        assert [a[3] for a in attempts(rig.store)] == ["uncertain"]

    run(case())


def test_a_flapping_head_never_stabilises_and_never_sends(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.world.flapping = {CHATS[0]}
        report = await visit_once(rig)
        assert rig.world.chats[CHATS[0]].sends == 0
        assert report["visit"]["outcome"].startswith("failed:")
        assert attempts(rig.store) == []

    run(case())


def test_an_authentication_challenge_is_protected_and_never_sent_to(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.world.chats[CHATS[0]].challenge = True
        report = await visit_once(rig)
        assert report["visit"]["outcome"] == "challenge"
        assert rig.world.chats[CHATS[0]].sends == 0
        assert rig.store.controls()["stop:" + CHATS[0]] == "human challenge"

    run(case())


def test_navigation_that_lands_on_a_login_page_reports_it_and_never_opens_a_second_tab(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.world.redirects[URLS[CHATS[0]]] = "https://chatgpt.com/auth/login"
        first = await visit_once(rig)
        assert first["visit"]["outcome"] == "redirected"
        page = rig.context.pages[0]
        gotos = page.calls.count("goto")
        later = await visit_once(rig)
        assert later["health"] == "login-flow"
        assert later["visit"] is None
        assert (
            page.calls.count("goto") == gotos
        )  # an active login is never navigated away
        assert rig.context.created == 1  # and no second patrol tab appears
        assert rig.world.chats[CHATS[0]].sends == 0

    run(case())


def test_navigation_failure_is_recorded_and_does_not_stall_the_lap(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        rig.world.nav_error = True
        for _ in range(3):  # every failure leaves a blank, unowned tab behind
            report = await visit_once(rig)
            assert report["visit"]["outcome"].startswith("nav-error")
        assert rig.context.created == 1 and len(rig.context.pages) == 1  # it is reused
        rig.world.nav_error = False
        again = await visit_once(rig)
        assert again["visit"]["chat"] == CHATS[3]  # the lap moved on, still one tab
        assert rig.context.created == 1

    run(case())


def test_a_chat_paused_by_the_owner_is_skipped_and_keeps_its_pause(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        rig.store.stop(CHATS[0], "pet project, do not touch")
        visited = []
        for _ in range(len(CHATS)):
            visited.append((await visit_once(rig))["visit"]["chat"])
        assert CHATS[0] not in visited
        assert set(visited) == set(CHATS) - {CHATS[0]}
        assert rig.world.chats[CHATS[0]].sends == 0
        assert rig.store.controls()["stop:" + CHATS[0]] == "pet project, do not touch"

    run(case())


def test_a_missing_tab_is_expected_queued_state_not_an_error(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch)
        report = await rig.patrol.tick()  # nothing is due yet and no tab is open
        assert report["health"] == "connected"
        assert report["visit"] is None
        assert rig.context.created == 0 and rig.context.pages == []
        assert rig.io.reads == 0

    run(case())


# --- pacing -------------------------------------------------------------------------------------


def test_visits_start_at_the_due_instant_and_every_gap_between_starts_is_inside_the_interval(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0), seed=3)
        rig.world.chats[CHATS[0]].finish_after = 1.0  # the reply ends: no protected tab
        starts, ends = [], []
        for _ in range(8):
            await advance_to_due(rig)
            previous_due = rig.rotation.due_at()
            starts.append(rig.clock.wall())
            assert (
                rig.clock.wall() == previous_due
            )  # a visit begins exactly at its due time
            await rig.patrol.tick()
            ends.append(rig.clock.wall())
            assert rig.rotation.visit_started_at == starts[-1]
            assert 240.0 <= rig.rotation.due_at() - starts[-1] <= 360.0
        # The first visit holds a Go and its confirmation wait, which is inside its interval.
        assert ends[0] - starts[0] > 20
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert all(240.0 <= gap <= 360.0 for gap in gaps)
        assert len({round(gap, 3) for gap in gaps}) > 3  # a real spread, not a constant
        assert rig.rotation.deferred is None

    run(case())


def test_nothing_is_visited_before_the_due_instant(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        due = rig.rotation.due_at()
        rig.clock.t = due - WALL0 - 0.001
        assert (await rig.patrol.tick())["visit"] is None
        assert rig.context.created == 0
        rig.clock.t = due - WALL0
        assert (await rig.patrol.tick())["visit"]["chat"] == CHATS[0]

    run(case())


@pytest.mark.parametrize("wait", [20.0, 60.0])
@pytest.mark.parametrize(
    "delta, confirmed", [(-0.001, True), (0.0, True), (0.001, False)]
)
def test_confirmation_window_edges_are_inclusive_and_bounded(
    tmp_path, monkeypatch, wait, delta, confirmed
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0), confirm_wait=wait)
        rig.world.chats[CHATS[0]].reply_delay = wait + delta
        await advance_to_due(rig)
        report = await rig.patrol.tick()
        assert report["visit"]["outcome"] == "failed:sent"
        go = rig.rotation.go
        assert go["state"] == "sent"
        assert go["confirmation"] == ("running" if confirmed else "unconfirmed")
        sent_at = go["sent_at"]
        waited = rig.clock.wall() - sent_at
        # A confirmed reply ends the wait early; an unconfirmed one waits exactly the window.
        assert waited <= wait + 1e-9
        if not confirmed:
            assert waited == pytest.approx(wait)

    run(case())


def test_a_late_reply_is_correlated_on_a_later_visit_of_the_same_chat(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0), confirm_wait=20.0)
        rig.world.chats[CHATS[0]].reply_delay = 100.0
        await visit_once(rig)
        assert rig.rotation.go["confirmation"] == "unconfirmed"
        # The reply is still running, so the tab is protected until it ends; only then does the
        # patrol leave, complete its lap and read this chat again.
        for _ in range(len(CHATS) + 3):
            await visit_once(rig)
        go = rig.rotation.go
        assert go["confirmation"] in ("running", "completed")
        assert go["response_turn"] == rig.world.chats[CHATS[0]].go_turn
        assert rig.world.chats[CHATS[0]].sends == 1

    run(case())


def test_the_in_visit_confirmation_wait_is_part_of_the_interval_not_added_to_it(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path, monkeypatch, kinds=failed(0), confirm_wait=60.0, seed=5
        )
        rig.world.chats[
            CHATS[0]
        ].reply_delay = 1000.0  # never confirmed inside the visit
        await advance_to_due(rig)
        await rig.patrol.tick()
        started = rig.rotation.last_visit["started_at"]
        assert (
            rig.rotation.last_visit["at"] - started >= 60.0
        )  # the wait really happened
        assert 240.0 <= rig.rotation.due_at() - started <= 360.0  # start to start
        assert rig.rotation.deferred is None

    run(case())


# --- state written to disk ----------------------------------------------------------------------


def test_visit_state_is_durable_and_free_of_page_text(tmp_path, monkeypatch):
    async def case():
        rig = make_rig(tmp_path, monkeypatch, kinds=failed(0))
        await visit_once(rig)
        path = rotation.rotation_path(rig.store.path)
        stored = json.loads(path.read_text())
        assert stored["last_visit"]["outcome"] == "failed:sent"
        assert stored["queue"][0]["url"] == URLS[CHATS[0]]
        assert stored["queue"][0]["chat"] == CHATS[0]
        assert stored["go"]["confirmation"] == "running"
        assert "draft" not in path.read_text().lower()

    run(case())
