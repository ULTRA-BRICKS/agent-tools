"""The machine 429 hold is released only by verified same-endpoint transport evidence, never by a clock.

Synthetic ids, fake time and fake browser responses only. The causal pattern comes from the real
home counterexample: a fresh 200 for the conversation list while exact chat reads still returned
429, so a timer or a global "something succeeded" reset would have hidden a restriction that was
still in force. Evidence is what the browser's own responses showed; nothing here is fetched.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from agenttools_chatgpt import patrol, rotation
from guardian_patrol_fakes import CHATS, IDS, WALL0, advance_to_due, attempts, make_rig
from test_chatgpt_guardian_patrol_safety import approval

HOLD = patrol.MACHINE_429_HOLD
LIST = "/backend-api/conversations"
READ_A = "/backend-api/conversation/" + IDS[0]
READ_B = "/backend-api/conversation/" + IDS[1]
READ_C = "/backend-api/conversations/" + IDS[2]


def run(case):
    return asyncio.run(case)


def rig_with(tmp_path, monkeypatch, **kwargs):
    return make_rig(
        tmp_path, monkeypatch, kinds={CHATS[0]: "failed"}, real_transport=True, **kwargs
    )


def sends(rig):
    return sum(chat.sends for chat in rig.world.chats.values())


def held(rig):
    return rig.store.controls().get("stop:*")


async def late_tick(rig, at=5000.0):
    """The first tick long after every deadline: the clock alone has done all it can do."""
    rig.clock.t = at
    return await rig.patrol.tick()


# --- the clock alone never releases anything ----------------------------------------------------


def test_an_elapsed_deadline_without_a_matching_success_leaves_the_hold(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        assert held(rig) == HOLD
        report = await late_tick(rig)
        assert held(rig) == HOLD  # the deadline passed an hour ago; the hold did not
        assert sends(rig) == 0 and attempts(rig.store) == []
        assert report["visit"]["outcome"] == "failed:stopped"  # one read-only check
        assert report["transport_hold"] == rotation.HOLD_PENDING

    run(case())


def test_the_check_visit_is_single_then_the_account_is_silent_again(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        await late_tick(rig)
        page = rig.context.pages[0]
        assert page.calls == ["goto"]
        assert rig.rotation.cooldown["reason"] == "hold-recheck"
        assert rig.rotation.cooldown["until"] == WALL0 + 5000 + patrol.DEFAULT_COOLDOWN
        for moment in (5001.0, 5400.0, 5899.0):
            rig.clock.t = moment
            report = await rig.patrol.tick()
            assert report["health"] == "cooldown" and report["visit"] is None
        assert page.calls == ["goto"] and sends(rig) == 0
        assert held(rig) == HOLD

    run(case())


def test_the_success_of_an_unrelated_endpoint_never_releases_the_hold(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 100
        for path in (LIST, READ_B, READ_A + "/stream_status"):
            rig.transport.respond(200, path)
        await late_tick(rig)
        assert held(rig) == HOLD and sends(rig) == 0
        assert (
            rig.rotation.transport["succeeded"] == {}
        )  # nothing unrestricted is tracked

    run(case())


def test_a_fresh_list_success_does_not_hide_a_newer_chat_read_429(
    tmp_path, monkeypatch
):
    """The real home pattern: list 200 a minute ago, exact chat read 429 still newer."""

    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, LIST, retry_after=60)
        rig.clock.t = 2260
        rig.transport.respond(429, READ_C, retry_after=60)
        rig.clock.t = 2261
        rig.transport.respond(200, LIST)  # the list recovered ...
        await late_tick(rig, 9000)
        assert held(rig) == HOLD  # ... the exact chat read did not
        rig.clock.t = 100000  # past the check visit's own re-armed cooldown
        assert rig.rotation.hold_unverified() == "unverified"
        assert rig.rotation.unverified_paths() == [READ_C]

    run(case())


def test_the_home_counterexample_four_endpoints_stay_held_until_each_recovers(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, LIST, retry_after=60)
        rig.clock.t = 2250
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 2251
        rig.transport.respond(200, LIST)
        rig.clock.t = 2290
        rig.transport.respond(200, READ_B)  # never restricted: proves nothing
        rig.clock.t = 2300
        rig.transport.respond(429, READ_C, retry_after=60)
        rig.clock.t = 2310
        rig.transport.respond(200, READ_C)  # recovered
        await late_tick(rig, 9000)
        assert held(rig) == HOLD
        assert rig.rotation.unverified_paths() == [READ_A]  # the chat read 429 stands
        rig.clock.t = 9500
        rig.transport.respond(200, READ_A)
        report = await late_tick(
            rig, 9901
        )  # after the re-armed cooldown of the last check
        assert held(rig) is None and report["transport_hold"] == ""

    run(case())


def test_a_success_older_than_a_newer_429_of_the_same_endpoint_leaves_the_hold(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 20
        rig.transport.respond(200, READ_A)
        rig.clock.t = 30
        rig.transport.respond(429, READ_A, retry_after=60)  # newer than the success
        await late_tick(rig)
        assert held(rig) == HOLD and sends(rig) == 0

    run(case())


def test_a_newer_429_of_another_endpoint_after_the_success_leaves_the_hold(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 20
        rig.transport.respond(200, READ_A)
        rig.clock.t = 30
        rig.transport.respond(429, READ_B, retry_after=60)
        await late_tick(rig)
        assert held(rig) == HOLD and sends(rig) == 0

    run(case())


def test_a_newer_auth_or_quota_signal_voids_an_older_success(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 20
        rig.transport.respond(200, READ_A)
        rig.clock.t = 30
        rig.rotation.note_blocked(rig.clock.wall())  # a quota or challenge sighting
        await late_tick(rig)
        assert held(rig) == HOLD
        rig.clock.t = 100000
        assert rig.rotation.hold_unverified() == "blocked-newer"

    run(case())


def test_a_retry_after_longer_than_the_default_wins_over_an_early_success(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=7200)
        rig.clock.t = 20
        rig.transport.respond(
            200, READ_A
        )  # the page retried on its own: matching success
        for moment in (
            900.0,
            3600.0,
            7209.0,
        ):  # past the 15 minute default, before Retry-After
            report = await late_tick(rig, moment)
            assert report["health"] == "cooldown" and report["visit"] is None
            assert held(rig) == HOLD
        assert rig.context.pages == [] and rig.io.reads == 0  # fully silent
        report = await late_tick(rig, 7211.0)  # the real deadline has passed
        assert held(rig) is None and report["health"] == "connected"

    run(case())


# --- a verified success releases ONLY the machine hold -------------------------------------------


def test_a_matching_success_after_the_latest_429_releases_only_the_machine_hold(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.store.quota()  # a quota latch must survive
        rig.store.stop(CHATS[4], "pet project")  # and so must a per-chat pause
        rig.store.set_control(rotation.MODE_CONTROL, "1")
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 80
        rig.transport.respond(200, READ_A)
        before = rig.store.controls()
        await late_tick(rig, 100.0)  # the cooldown is over, not yet the next visit
        after = rig.store.controls()
        assert before["stop:*"] == HOLD and "stop:*" not in after
        assert {k for k in before if k != "stop:*"} <= set(after)
        assert after["stop:" + CHATS[4]] == "pet project" and "astra_quota" in after
        assert rig.rotation.cooldown is None
        assert rig.rotation.transport == {
            "restricted": {},
            "succeeded": {},
            "blocked_at": None,
        }

    run(case())


def test_after_the_release_a_go_still_needs_the_normal_admission(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.store.stop(CHATS[0], "owner holds this chat")
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 80
        rig.transport.respond(200, READ_A)
        report = await late_tick(rig, 5000)
        assert held(rig) is None
        assert report["visit"]["chat"] != CHATS[0]  # the per-chat stop still binds
        assert rig.store.controls()["stop:" + CHATS[0]] == "owner holds this chat"
        assert sends(rig) == 0 and rig.world.chats[CHATS[0]].sends == 0

    run(case())


def test_after_the_release_the_ordinary_visit_sends_one_go_through_the_normal_path(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 80
        rig.transport.respond(200, READ_A)
        report = await late_tick(rig, 5000)
        assert held(rig) is None and report["visit"]["outcome"] == "failed:sent"
        assert sends(rig) == 1 and len(attempts(rig.store)) == 1

    run(case())


@pytest.mark.parametrize("text", ["owner paused everything", "human challenge"])
def test_a_user_or_auth_hold_is_never_released_even_with_perfect_evidence(
    tmp_path, monkeypatch, text
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 20
        rig.transport.respond(200, READ_A)
        rig.store.stop("*", text)  # replaces the machine text: no longer a 429 hold
        report = await late_tick(rig)
        assert held(rig) == text
        assert report["health"].startswith("stopped")
        assert rig.io.reads == 0 and sends(rig) == 0

    run(case())


def test_a_hold_without_the_patrols_own_evidence_stays_pending_and_silent(
    tmp_path, monkeypatch
):
    """A legacy or foreign 429 hold carries no endpoint, no Retry-After, nothing to verify."""

    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.store.stop("*", HOLD)
        report = await late_tick(rig, 100000)
        assert held(rig) == HOLD and report["health"].startswith("stopped")
        assert report["transport_hold"] == rotation.HOLD_PENDING
        assert rig.io.reads == 0 and rig.context.created == 0 and sends(rig) == 0

    run(case())


def test_an_elapsed_cooldown_record_without_restriction_evidence_stays_silent(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.store.stop("*", HOLD)
        rig.rotation.set_cooldown(WALL0 + 60, "http-429", "retry-after", HOLD)
        report = await late_tick(rig)
        assert held(rig) == HOLD and report["health"].startswith("stopped")
        assert rig.io.reads == 0 and sends(rig) == 0

    run(case())


def test_the_release_deletes_only_the_exact_machine_text_never_another_global_stop(
    tmp_path, monkeypatch
):
    """Compare-and-delete: a `stop:*` written between the verdict and the delete survives."""
    rig = rig_with(tmp_path, monkeypatch)
    rig.store.stop("*", "owner paused everything")
    rig.patrol._drop_machine_hold()
    assert held(rig) == "owner paused everything"
    rig.store.stop("*", HOLD)
    rig.patrol._drop_machine_hold()
    assert held(rig) is None


def test_a_quota_sighting_during_the_check_visit_voids_the_older_successes(
    tmp_path, monkeypatch
):
    async def case():
        rig = make_rig(
            tmp_path,
            monkeypatch,
            kinds={CHATS[0]: "quota"},
            real_transport=True,
        )
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 12
        rig.transport.respond(429, READ_B, retry_after=60)
        rig.clock.t = 20
        rig.transport.respond(200, READ_A)  # one endpoint recovered, the other did not
        report = await late_tick(rig, 5000)
        assert (
            report["visit"]["outcome"] == "rate-limited"
        )  # the visit saw the quota page
        assert rig.rotation.transport["blocked_at"] == WALL0 + 5000
        rig.clock.t = 5001
        rig.transport.respond(200, READ_B)  # newer than the quota sighting ...
        rig.clock.t = 100000
        assert (
            rig.rotation.hold_unverified() == "blocked-newer"
        )  # ... A's success is not
        assert held(rig) == HOLD

    run(case())


# --- a visit under the hold is read-only, and a new 429 wins -------------------------------------


def test_no_go_and_no_approval_dispatch_during_the_check_visit(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        process = AsyncMock(return_value=None)
        monkeypatch.setattr(patrol, "process_continuation", process)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 4990
        approval(rig.store, CHATS[0], rig)  # queued, unexpired at the check visit
        report = await late_tick(rig)
        assert report["visit"]["chat"] == CHATS[0]
        process.assert_not_called()
        assert sends(rig) == 0 and attempts(rig.store) == []

    run(case())


def test_a_new_429_during_the_check_visit_restarts_the_cooldown_from_its_retry_after(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        real = rig.transport.seed

        async def seed(page):
            rig.transport.respond(
                429, READ_A, retry_after=1200
            )  # the visit hit it again
            await real(page)

        rig.transport.seed = seed
        report = await late_tick(rig, 5000)
        assert report["visit"]["outcome"] == "cooldown"
        assert rig.rotation.cooldown["until"] == WALL0 + 5000 + 1200
        assert (
            rig.rotation.cooldown["reason"] == "http-429"
        )  # not replaced by the re-arm
        assert held(rig) == HOLD and sends(rig) == 0

    run(case())


def test_a_success_arriving_during_the_check_visit_releases_at_its_end(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        real = rig.io.sample

        async def sample(page):
            rig.clock.advance(1)
            rig.transport.respond(200, READ_A)  # the page's own read succeeded
            return await real(page)

        rig.io.sample = sample
        report = await late_tick(rig, 5000)
        assert (
            report["visit"]["outcome"] == "failed:stopped"
        )  # this visit still sent nothing
        assert held(rig) is None and rig.rotation.cooldown is None
        assert sends(rig) == 0

    run(case())


# --- restart keeps the evidence ------------------------------------------------------------------


def restarted(rig, monkeypatch, tmp_path):
    rig.clock.advance(30)
    return make_rig(
        tmp_path,
        monkeypatch,
        kinds={CHATS[0]: "failed"},
        real_transport=True,
        clock=rig.clock,
        world=rig.world,
        context=rig.context,
    )


def test_the_evidence_and_the_cooldown_survive_a_restart(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=600)
        again = restarted(rig, monkeypatch, tmp_path)
        assert again.rotation.cooldown == rig.rotation.cooldown
        assert again.rotation.transport["restricted"] == {READ_A: WALL0 + 10}
        report = await again.patrol.tick()
        assert (
            report["health"] == "cooldown"
        )  # Retry-After still binds after the restart
        assert held(again) == HOLD

    run(case())


def test_a_success_after_the_restart_completes_the_verification(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        again = restarted(rig, monkeypatch, tmp_path)
        again.clock.t = 100
        again.transport.respond(200, READ_A)
        await late_tick(again, 5000)
        assert held(again) is None and again.rotation.cooldown is None

    run(case())


def test_a_restart_without_a_success_keeps_the_hold(tmp_path, monkeypatch):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        again = restarted(rig, monkeypatch, tmp_path)
        await late_tick(again, 5000)
        assert held(again) == HOLD and sends(again) == 0

    run(case())


# --- what the recorder accepts ------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, url",
    [
        (200, "https://example.com/backend-api/conversation/x"),
        (200, "https://chatgpt.com/static/app.js"),
        (429, "https://example.com/backend-api/conversation/x"),
    ],
)
def test_responses_outside_the_account_api_are_never_evidence(
    tmp_path, monkeypatch, status, url
):
    rig = rig_with(tmp_path, monkeypatch)
    rig.transport.on_response(
        type("R", (), {"status": status, "url": url, "headers": {}})()
    )
    assert rig.rotation.transport["restricted"] == {}
    assert rig.rotation.transport["succeeded"] == {}


def test_a_query_string_never_makes_a_different_endpoint(tmp_path, monkeypatch):
    rig = rig_with(tmp_path, monkeypatch)
    rig.clock.t = 10
    rig.transport.respond(429, READ_A + "?num_turns=3", retry_after=60)
    rig.clock.t = 20
    rig.transport.respond(200, READ_A + "?include_has_versions=true")
    assert rig.rotation.transport["restricted"] == {READ_A: WALL0 + 10}
    assert rig.rotation.hold_unverified() == "cooldown"  # Retry-After first
    rig.clock.t = 100
    assert rig.rotation.hold_unverified() == ""


def test_a_401_or_403_is_recorded_as_a_blocking_signal(tmp_path, monkeypatch):
    rig = rig_with(tmp_path, monkeypatch)
    rig.clock.t = 10
    rig.transport.respond(403, READ_A)
    assert rig.rotation.transport["blocked_at"] == WALL0 + 10
    assert rig.rotation.transport["restricted"] == {}


# --- review findings: write amplification, check visit, Go record, ------------------------------


def test_a_stream_of_401s_is_not_written_once_per_response(tmp_path, monkeypatch):
    rig = rig_with(tmp_path, monkeypatch)
    writes = []
    real = rotation.write_atomic
    monkeypatch.setattr(
        rotation,
        "write_atomic",
        lambda path, data: (writes.append(1), real(path, data)),
    )
    for step in range(200):  # two seconds of 401s
        rig.clock.t = 10 + step * 0.01
        rig.transport.respond(401, LIST)
    assert len(writes) <= 3
    assert (
        rig.rotation.transport["blocked_at"] == WALL0 + 10 + 1.99
    )  # memory is current


def test_a_transition_is_persisted_at_once_even_inside_the_throttle_window(
    tmp_path, monkeypatch
):
    rig = rig_with(tmp_path, monkeypatch)
    rig.clock.t = 10
    rig.transport.respond(429, READ_A, retry_after=60)
    rig.clock.t = 10.1
    rig.transport.respond(200, READ_A)
    rig.clock.t = 10.2
    rig.transport.respond(429, READ_A, retry_after=60)  # voids the success
    stored = json.loads(rotation.rotation_path(rig.store.path).read_text())["transport"]
    assert stored["restricted"] == {READ_A: WALL0 + 10.2}
    assert stored["succeeded"] == {READ_A: WALL0 + 10.1}
    rig.transport.respond(401, LIST)
    stored = json.loads(rotation.rotation_path(rig.store.path).read_text())["transport"]
    assert stored["blocked_at"] == WALL0 + 10.2  # a first block is a transition


def test_the_check_visit_is_spent_on_the_in_order_chat_not_an_approval_chat(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.clock.t = 10
        rig.transport.respond(429, READ_A, retry_after=60)
        rig.clock.t = 4990
        approval(rig.store, CHATS[3], rig)  # would normally jump the queue
        report = await late_tick(rig)
        assert report["visit"]["chat"] == CHATS[0]

    run(case())


def test_a_browser_error_during_the_click_leaves_the_go_uncertain_not_sending(
    tmp_path, monkeypatch
):
    async def case():
        from playwright.async_api import Error

        rig = rig_with(tmp_path, monkeypatch)
        rig.world.crash_on_send = Error("page crashed")
        await advance_to_due(rig)
        report = await rig.patrol.tick()
        assert report["visit"]["outcome"].startswith(("error:", "failed:"))
        assert rig.rotation.go["state"] == "uncertain"

    run(case())


def test_a_failed_evidence_write_never_drops_the_live_429_latch(tmp_path, monkeypatch):
    rig = rig_with(tmp_path, monkeypatch)

    def full_disk(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(rotation, "write_atomic", full_disk)
    with pytest.raises(OSError):
        rig.transport.respond(429, READ_A, retry_after=60)
    assert held(rig) == HOLD  # the latch never depends on the evidence write


def test_a_hold_that_predates_our_evidence_is_not_released_by_a_later_live_429(
    tmp_path, monkeypatch
):
    async def case():
        rig = rig_with(tmp_path, monkeypatch)
        rig.store.stop(
            "*", HOLD
        )  # a legacy owner or a replayed seed latched it, no endpoint
        rig.clock.t = 10
        rig.transport.respond(429, READ_B, retry_after=60)  # a live 429 elsewhere
        rig.clock.t = 20
        rig.transport.respond(200, READ_B)  # which then recovers
        await late_tick(rig)
        assert held(rig) == HOLD and sends(rig) == 0
        assert patrol.FOREIGN_HOLD in rig.rotation.unverified_paths()

    run(case())


def test_a_stream_of_429s_is_not_written_once_per_response(tmp_path, monkeypatch):
    rig = rig_with(tmp_path, monkeypatch)
    writes = []
    real = rotation.write_atomic
    monkeypatch.setattr(
        rotation,
        "write_atomic",
        lambda path, data: (writes.append(1), real(path, data)),
    )
    for step in range(200):  # two seconds of 429s, each with a later deadline
        rig.clock.t = 10 + step * 0.01
        rig.transport.respond(429, READ_A, retry_after=600)
    assert len(writes) <= 6
    assert (
        rig.rotation.cooldown["until"] == WALL0 + 10 + 1.99 + 600
    )  # memory is current
