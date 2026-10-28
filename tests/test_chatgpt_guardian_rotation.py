"""Single-tab patrol rotation state: persisted queue, cursor, due time; injected clock and RNG only."""

import json
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import rotation
from agenttools_chatgpt.rotation import Rotation, RotationError

NOW = 1_800_000_000.0
IDS = [f"{n:08d}-0000-4000-8000-000000000000" for n in range(1, 8)]
CHATS = [f"https://chatgpt.com/c/{i}" for i in IDS]
PROJECT_URL = (
    "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/" + IDS[0]
)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Dice:
    """`random()` replays a script, then repeats its last value."""

    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def opened(
    tmp_path, account="home", *, clock=None, rng=None, interval=None, name="r.json"
):
    kwargs = {} if interval is None else {"interval": interval}
    return Rotation.open(
        tmp_path / name,
        account,
        clock=clock or Clock(),
        rng=rng or random.Random(1),
        **kwargs,
    )


def queue_of(machine, urls=None):
    machine.sync({chat: (urls or {}).get(chat, chat) for chat in CHATS})


# --- interval -------------------------------------------------------------------------------


def test_defaults_are_240_to_360_seconds():
    assert rotation.DEFAULT_INTERVAL == (240.0, 360.0)
    assert rotation.validate_interval(240, 360) == (240.0, 360.0)


@pytest.mark.parametrize("value, expected", [(0.0, 240.0), (1.0, 360.0), (0.5, 300.0)])
def test_draw_hits_both_interval_edges_exactly(tmp_path, value, expected):
    assert opened(tmp_path, rng=Dice(value)).draw() == expected


def test_every_draw_is_inside_the_interval(tmp_path):
    machine = opened(tmp_path, rng=random.Random(20261027))
    draws = [machine.draw() for _ in range(5000)]
    assert min(draws) >= 240.0 and max(draws) <= 360.0
    assert max(draws) - min(draws) > 100  # a real spread, not a constant


@pytest.mark.parametrize(
    "low, high",
    [
        (119, 300),  # below the floor
        (240, 901),  # above the ceiling
        (360, 240),  # inverted
        (float("nan"), 300),
        (240, float("inf")),
        (True, 300),
        ("240", 300),
        (None, None),
    ],
)
def test_unsafe_interval_configuration_is_refused(low, high):
    with pytest.raises(RotationError):
        rotation.validate_interval(low, high)


def test_safe_custom_interval_and_confirmation_bounds():
    assert rotation.validate_interval(120, 900) == (120.0, 900.0)
    assert rotation.validate_interval(300, 300) == (300.0, 300.0)
    for ok in (20, 45, 60):
        assert rotation.validate_confirm_wait(ok) == float(ok)
    for bad in (19.99, 60.01, float("nan"), True, "30", None):
        with pytest.raises(RotationError):
            rotation.validate_confirm_wait(bad)


def test_first_start_schedules_a_delayed_first_visit_never_an_immediate_one(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.0))
    assert machine.due_at() == NOW + 240
    clock.now = NOW + 239.999
    assert not machine.is_due()
    clock.now = NOW + 240
    assert machine.is_due()  # exactly at the edge is due
    clock.now = NOW + 240.001
    assert machine.is_due()


def visit(machine, clock, entry=None, *, work=0.0, outcome="complete", advance=True):
    """One visit: it starts now, `work` seconds of in-visit wait pass, then it ends."""
    entry = entry or machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    started = clock.now
    clock.now += work
    machine.end_visit(entry, outcome, advance=advance)
    return started


@pytest.mark.parametrize(
    "dice, draw", [(0.0, 240.0), (0.5, 300.0), (1.0, 360.0)], ids=["low", "mid", "high"]
)
def test_the_interval_is_between_visit_starts_the_confirmation_is_inside_it(
    tmp_path, dice, draw
):
    """55 s of confirmation plus the 360 s draw is a 360 s gap between starts, never 415 s."""
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, dice))
    queue_of(machine)
    clock.now = machine.due_at()
    started = visit(machine, clock, work=55.0, outcome="failed:sent")
    assert machine.due_at() == started + draw
    assert machine.deferred is None
    clock.now = started + draw - 0.001
    assert not machine.is_due()
    clock.now = started + draw
    assert machine.is_due()
    second = visit(machine, clock, work=55.0)
    assert second - started == draw  # start to start, the wait is part of it


def test_one_draw_is_made_per_visit_and_the_end_of_a_visit_never_redraws(tmp_path):
    clock = Clock()
    dice = Dice(0.5, 0.0, 1.0)
    machine = opened(tmp_path, clock=clock, rng=dice)
    queue_of(machine)
    clock.now = machine.due_at()
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)  # the one draw of this visit: 0.0 -> 240 s
    due = machine.due_at()
    assert due == clock.now + 240
    clock.now += 100
    machine.end_visit(entry, "complete", advance=True)
    assert machine.due_at() == due and dice.values == [1.0]  # nothing else was drawn


def test_the_start_of_the_visit_is_persisted_and_shown_in_the_last_visit(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5))
    queue_of(machine)
    clock.now = machine.due_at()
    started = visit(machine, clock, work=30.0)
    stored = json.loads((tmp_path / "r.json").read_text())
    assert stored["visit_started_at"] == started
    assert stored["last_visit"]["started_at"] == started
    assert stored["last_visit"]["at"] == started + 30.0
    assert machine.snapshot()["last_visit"]["started_at"] == started


def test_a_restart_in_the_middle_of_a_visit_keeps_the_anchor_and_does_not_redraw(
    tmp_path,
):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, 1.0))
    queue_of(machine)
    clock.now = machine.due_at()
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)  # the draw of this visit: 360 s
    started = clock.now
    clock.now += 30  # killed while the visit is in progress, restarted 30 s later
    again = opened(tmp_path, clock=clock, rng=Dice(0.0))  # a redraw would give 240 s
    assert again.due_at() == started + 360
    assert again.visit_started_at == started and again.deferred is None


@pytest.mark.parametrize("work", [360.0, 361.0, 900.0])
def test_a_visit_that_outlasts_its_draw_is_an_explicit_deferral_without_a_burst(
    tmp_path, work
):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, 1.0, 0.25))
    queue_of(machine)
    clock.now = machine.due_at()
    started = visit(machine, clock, work=work, outcome="cooldown")
    now = clock.now
    assert machine.deferred == {"reason": "overrun", "at": now}
    assert machine.due_at() == now + 270  # one fresh draw from now, never due at now
    assert not machine.is_due()
    assert machine.snapshot()["deferred"] == {"reason": "overrun", "at": now}
    assert opened(tmp_path, clock=clock).deferred == machine.deferred  # persisted
    clock.now = machine.due_at()
    visit(machine, clock)  # exactly one visit, then the deferral is over
    assert machine.deferred is None
    assert machine.due_at() - clock.now > 0 and started < clock.now


def test_a_deferred_slot_is_recorded_and_the_next_visit_clears_it(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, 0.0, 1.0))
    queue_of(machine)
    clock.now = machine.due_at()
    before = machine.visit_started_at
    machine.defer("running-protected", "running-protected")
    assert machine.last_visit == {
        "chat": None,
        "at": clock.now,
        "started_at": None,
        "outcome": "running-protected",
    }
    assert machine.deferred == {"reason": "running-protected", "at": clock.now}
    assert machine.due_at() == clock.now + 240  # a fresh draw from now, no burst
    assert machine.visit_started_at == before  # nothing started, nothing navigated
    assert opened(tmp_path, clock=clock).deferred == machine.deferred
    clock.now = machine.due_at()
    visit(machine, clock)
    assert machine.deferred is None


def test_an_empty_slot_schedules_one_interval_and_is_not_a_deferral(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, 0.0))
    clock.now = machine.due_at()
    machine.idle("nothing-visitable")
    assert machine.due_at() == clock.now + 240 and machine.deferred is None


# --- independence, restart, downtime ------------------------------------------------------------


def test_two_accounts_keep_independent_state_and_schedules(tmp_path):
    clock = Clock()
    home = opened(tmp_path, "home", clock=clock, rng=random.Random(1), name="home.json")
    glide = opened(
        tmp_path, "glide.vc", clock=clock, rng=random.Random(2), name="glide.json"
    )
    queue_of(home)
    glide.sync({CHATS[0]: CHATS[0]})
    assert [home.draw() for _ in range(5)] != [glide.draw() for _ in range(5)]
    assert home.due_at() != glide.due_at()
    assert len(home.queue) == 7 and len(glide.queue) == 1
    with pytest.raises(RotationError, match="account"):
        opened(tmp_path, "glide.vc", name="home.json")
    assert json.loads((tmp_path / "home.json").read_text())["account"] == "home"


def test_default_rng_is_per_instance_system_entropy_not_a_shared_seed(tmp_path):
    first = Rotation.open(tmp_path / "a.json", "home", clock=Clock())
    second = Rotation.open(tmp_path / "b.json", "glide.vc", clock=Clock())
    assert first.rng is not second.rng
    assert first.due_at() != second.due_at()


def test_restart_preserves_due_time_cursor_and_queue(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5))
    queue_of(machine, {CHATS[0]: PROJECT_URL})
    for _ in range(3):
        entry = machine.pick(lambda chat: True)[0]
        machine.begin_visit(entry)
        machine.end_visit(entry, "complete", advance=True)
    due, cursor = machine.due_at(), machine.cursor
    clock.now += 100  # restart well before the due time
    again = opened(tmp_path, clock=clock, rng=Dice(0.99))
    assert (again.due_at(), again.cursor) == (due, cursor)
    assert [e.chat for e in again.queue] == [e.chat for e in machine.queue]
    assert again.queue[0].url == PROJECT_URL
    assert again.current == machine.current
    assert again.last_visit == machine.last_visit


def test_long_downtime_never_causes_a_catch_up_burst(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.0))
    queue_of(machine)
    stale_due = machine.due_at()
    clock.now = stale_due + 10 * 3600  # ten hours of downtime
    again = opened(tmp_path, clock=clock, rng=Dice(0.25))
    assert again.due_at() == clock.now + 270  # one fresh draw from "now"
    assert again.deferred == {
        "reason": "overdue",
        "at": clock.now,
    }  # visible, not silent
    assert not again.is_due()
    visits = 0
    while again.is_due():
        visits += 1
        entry = again.pick(lambda chat: True)[0]
        again.begin_visit(entry)
        again.end_visit(entry, "complete", advance=True)
    assert visits == 0
    clock.now = again.due_at()
    assert again.is_due()
    entry = again.pick(lambda chat: True)[0]
    again.begin_visit(entry)
    again.end_visit(entry, "complete", advance=True)
    assert not again.is_due()  # exactly one visit, then a full fresh interval


def test_restarted_accounts_do_not_synchronise(tmp_path):
    clock = Clock()
    for name, seed in (("home.json", 1), ("glide.json", 2)):
        machine = opened(tmp_path, clock=clock, rng=random.Random(seed), name=name)
        machine.next_due = NOW - 5
        machine.save()
    clock.now = NOW + 7200
    home = opened(
        tmp_path, "home", clock=clock, rng=random.Random(11), name="home.json"
    )
    glide = opened(
        tmp_path, "home", clock=clock, rng=random.Random(12), name="glide.json"
    )
    assert home.due_at() != glide.due_at()
    for machine in (home, glide):
        assert clock.now + 240 <= machine.due_at() <= clock.now + 360


def test_clock_moved_backwards_cannot_park_the_patrol_for_hours(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5))
    machine.next_due = NOW + 6 * 3600  # written by a clock that was hours ahead
    machine.save()
    again = opened(tmp_path, clock=clock, rng=Dice(0.5))
    assert again.due_at() == NOW + 300
    assert again.deferred == {"reason": "clock", "at": NOW}


# --- queue, cursor, starvation ------------------------------------------------------------------


def test_queue_persists_full_permanent_url_and_canonical_identity(tmp_path):
    machine = opened(tmp_path)
    machine.sync({CHATS[0]: PROJECT_URL, CHATS[1]: CHATS[1]})
    first = machine.queue[0]
    assert first.chat == CHATS[0] and first.url == PROJECT_URL
    stored = json.loads((tmp_path / "r.json").read_text())
    assert stored["queue"][0]["url"] == PROJECT_URL
    assert stored["queue"][0]["chat"] == CHATS[0]
    # A later canonical-only sighting never downgrades the stored project URL.
    machine.sync({CHATS[0]: CHATS[0], CHATS[1]: CHATS[1]})
    assert machine.queue[0].url == PROJECT_URL


@pytest.mark.parametrize(
    "raw",
    [
        PROJECT_URL + "?model=gpt",
        PROJECT_URL + "#frag",
        PROJECT_URL + "/",
    ],
)
def test_permanent_url_drops_query_and_fragment_only(raw):
    assert rotation.permanent_url(raw).rstrip("/") == PROJECT_URL


@pytest.mark.parametrize(
    "raw",
    ["https://evil.test/c/x", "http://chatgpt.com/c/abc", "about:blank", "", None, 7],
)
def test_permanent_url_refuses_anything_but_a_conversation(raw):
    with pytest.raises(RotationError):
        rotation.permanent_url(raw)


def test_sync_adds_removes_and_keeps_the_cursor_on_the_same_next_chat(tmp_path):
    machine = opened(tmp_path)
    queue_of(machine)
    for _ in range(3):
        entry = machine.pick(lambda chat: True)[0]
        machine.begin_visit(entry)
        machine.end_visit(entry, "complete", advance=True)
    upcoming = machine.queue[machine.cursor].chat
    # Drop one chat before the cursor and one after, and add a brand new one.
    keep = {c: c for c in CHATS if c not in (CHATS[0], CHATS[5])}
    new = "https://chatgpt.com/c/99999999-0000-4000-8000-000000000000"
    machine.sync({**keep, new: new})
    assert machine.queue[machine.cursor].chat == upcoming
    assert machine.queue[-1].chat == new  # new chats join at the end


def test_no_queued_chat_is_starved_even_with_paused_and_priority_chats(tmp_path):
    machine = opened(tmp_path)
    queue_of(machine)
    paused = {CHATS[1], CHATS[4]}
    eligible = set(CHATS) - paused
    seen, in_order, priority_visits = [], [], 0
    for step in range(40):
        prefer = CHATS[6] if step % 3 == 0 else None
        entry, advance = machine.pick(lambda chat: chat not in paused, prefer=prefer)
        priority_visits += not advance
        machine.begin_visit(entry)
        machine.end_visit(entry, "complete", advance=advance)
        seen.append(entry.chat)
        if advance:
            in_order.append(entry.chat)
    assert priority_visits > 5
    assert set(seen) == eligible
    # Round-robin: priority visits never push an eligible chat out of its lap.
    for start in range(len(in_order) - len(eligible) + 1):
        assert set(in_order[start : start + len(eligible)]) == eligible


def test_pick_returns_none_when_everything_is_paused_or_the_queue_is_empty(tmp_path):
    machine = opened(tmp_path)
    assert machine.pick(lambda chat: True) is None
    queue_of(machine)
    assert machine.pick(lambda chat: False) is None


def test_visit_bookkeeping_is_recorded(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock)
    queue_of(machine)
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    assert machine.current == entry.chat
    clock.now += 12
    machine.end_visit(entry, "working", advance=True)
    assert machine.last_visit == {
        "chat": entry.chat,
        "at": NOW + 12,
        "started_at": NOW,
        "outcome": "working",
    }
    assert (entry.visits, entry.last_outcome, entry.last_visit_at) == (
        1,
        "working",
        NOW + 12,
    )
    assert machine.cursor == 1


# --- cooldown ------------------------------------------------------------------------------------


def test_a_cooldown_that_outlasts_the_due_time_is_itself_the_next_retry_deadline(
    tmp_path,
):
    """The cooldown end IS the deadline: no second interval is drawn after it (0.3.15)."""
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, 0.0))
    machine.set_cooldown(NOW + 1000, "retry-after", "retry-after", "hold text")
    assert machine.deferred == {"reason": "cooldown", "at": NOW}
    assert machine.due_at() == NOW + 1000
    assert set(machine.cooldown) == {"until", "reason", "source", "hold"}
    clock.now = NOW + 999.9
    assert machine.cooling() and not machine.is_due()
    clock.now = NOW + 1000
    assert not machine.cooling() and machine.is_due()
    clock.now = NOW + 900  # a restart before the due time keeps the deadline exactly
    assert opened(tmp_path, clock=clock).due_at() == NOW + 1000


def test_a_longer_cooldown_moves_the_deadline_and_never_adds_an_interval(
    tmp_path,
):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5, 0.0, 1.0))
    machine.set_cooldown(NOW + 1000, "a", "retry-after", "hold")
    machine.set_cooldown(NOW + 3000, "b", "retry-after", "hold")
    assert machine.due_at() == NOW + 3000 == machine.cooldown["until"]


def test_an_earlier_cooldown_never_delays_or_defers_the_rotation(tmp_path):
    machine = opened(tmp_path, rng=Dice(1.0))
    machine.set_cooldown(NOW + 10, "retry-after", "retry-after", "hold text")
    assert machine.due_at() == NOW + 360 and machine.deferred is None


def test_cooldown_survives_restart_and_keeps_the_later_deadline(tmp_path):
    machine = opened(tmp_path)
    machine.set_cooldown(NOW + 2000, "a", "retry-after", "hold")
    machine.set_cooldown(
        NOW + 500, "b", "default", "hold"
    )  # a shorter one never shrinks it
    assert machine.cooldown["until"] == NOW + 2000
    again = opened(tmp_path)
    assert again.cooldown["until"] == NOW + 2000
    again.clear_hold()
    assert opened(tmp_path).cooldown is None


# --- 429 transport evidence ----------------------------------------------------------------------

LIST = "/backend-api/conversations"
READ_A = "/backend-api/conversations/" + IDS[0]
READ_B = "/backend-api/conversation/" + IDS[1]


def evidence(tmp_path, clock=None, **kwargs):
    clock = clock or Clock(NOW + 10_000)  # every cooldown below has long ended
    return opened(tmp_path, clock=clock, **kwargs)


def test_no_recorded_restriction_is_never_a_verification(tmp_path):
    machine = evidence(tmp_path)
    assert machine.hold_unverified() == "no-restriction-evidence"
    machine.note_success(LIST, NOW + 5)  # a success nobody restricted proves nothing
    assert machine.hold_unverified() == "no-restriction-evidence"
    assert machine.transport["succeeded"] == {}


def test_a_restriction_without_a_success_stays_unverified(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 1)
    assert machine.hold_unverified() == "unverified"


def test_the_success_of_an_unrelated_endpoint_verifies_nothing(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 1)
    machine.note_success(LIST, NOW + 9)
    machine.note_success(READ_B, NOW + 9)
    machine.note_success(READ_A + "/other", NOW + 9)
    assert machine.hold_unverified() == "unverified"


@pytest.mark.parametrize("offset", [-5.0, 0.0], ids=["older", "same-instant"])
def test_a_success_not_strictly_after_the_restriction_does_not_count(tmp_path, offset):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 10)
    machine.note_success(READ_A, NOW + 10 + offset)
    assert machine.hold_unverified() == "unverified"


def test_a_matching_success_after_the_latest_restriction_verifies(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 10)
    machine.note_success(READ_A, NOW + 11)
    assert machine.hold_unverified() == ""


def test_a_newer_restriction_after_the_success_makes_it_unverified_again(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 10)
    machine.note_success(READ_A, NOW + 11)
    machine.note_restricted(READ_A, NOW + 12)
    assert machine.hold_unverified() == "unverified"
    machine.note_success(READ_A, NOW + 13)
    assert machine.hold_unverified() == ""


def test_every_restricted_endpoint_needs_its_own_later_success(tmp_path):
    """The home counterexample: a fresh list 200 while per-chat reads still returned 429."""
    machine = evidence(tmp_path)
    machine.note_restricted(LIST, NOW + 0)
    machine.note_restricted(READ_A, NOW + 2250)
    machine.note_restricted(READ_B, NOW + 2300)
    machine.note_success(LIST, NOW + 2251)  # the list recovered ...
    assert machine.hold_unverified() == "unverified"  # ... the exact chat reads did not
    machine.note_success(READ_B, NOW + 2290)  # older than READ_B's own 429
    assert machine.hold_unverified() == "unverified"
    machine.note_success(READ_A, NOW + 2400)
    machine.note_success(READ_B, NOW + 2401)
    assert machine.hold_unverified() == ""


def test_a_newer_auth_or_quota_signal_voids_every_older_success(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 10)
    machine.note_success(READ_A, NOW + 11)
    machine.note_blocked(NOW + 12)
    assert machine.hold_unverified() == "blocked-newer"
    machine.note_success(READ_A, NOW + 13)
    assert machine.hold_unverified() == ""


def test_nothing_is_verified_while_a_cooldown_is_running(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5))
    machine.note_restricted(READ_A, NOW + 1)
    machine.note_success(READ_A, NOW + 2)
    machine.set_cooldown(NOW + 3600, "retry-after", "retry-after", "hold")
    clock.now = NOW + 3599
    assert machine.hold_unverified() == "cooldown"  # Retry-After wins over any success
    clock.now = NOW + 3600
    assert machine.hold_unverified() == ""


def test_the_evidence_survives_a_restart(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 1)
    machine.note_restricted(LIST, NOW + 2)
    machine.note_success(LIST, NOW + 3)
    machine.note_blocked(NOW + 4)
    again = evidence(tmp_path)
    assert again.transport == machine.transport
    assert again.transport["restricted"] == {READ_A: NOW + 1, LIST: NOW + 2}
    assert again.transport["succeeded"] == {LIST: NOW + 3}
    assert again.transport["blocked_at"] == NOW + 4
    assert again.hold_unverified() == "unverified"


def test_clearing_the_hold_forgets_the_cooldown_and_every_restriction(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 1)
    machine.note_blocked(NOW + 2)
    machine.set_cooldown(NOW + 50_000, "http-429", "retry-after", "hold")
    machine.clear_hold()
    assert machine.cooldown is None
    assert machine.transport == {"restricted": {}, "succeeded": {}, "blocked_at": None}
    assert evidence(tmp_path).transport == machine.transport


@pytest.mark.parametrize(
    "path",
    ["", "backend-api/x", "/backend-api/ x", "/backend-api/" + "a" * 200, 7, None],
)
def test_an_endpoint_that_cannot_be_recorded_exactly_blocks_verification(
    tmp_path, path
):
    machine = evidence(tmp_path)
    machine.note_restricted(READ_A, NOW + 1)
    machine.note_success(READ_A, NOW + 2)
    machine.note_restricted(path, NOW + 3)
    assert machine.transport["blocked_at"] == NOW + 3
    assert machine.hold_unverified() == "blocked-newer"


def test_the_evidence_is_bounded_and_never_forgets_an_unverified_restriction(
    tmp_path,
):
    machine = evidence(tmp_path)
    for number in range(rotation.TRANSPORT_LIMIT):
        machine.note_restricted(f"/backend-api/r/{number}", NOW + 1)
    machine.note_restricted("/backend-api/r/overflow", NOW + 9)
    assert len(machine.transport["restricted"]) == rotation.TRANSPORT_LIMIT
    assert "/backend-api/r/overflow" not in machine.transport["restricted"]
    assert machine.transport["blocked_at"] == NOW + 9
    assert machine.hold_unverified() == "unverified"
    for number in range(rotation.TRANSPORT_LIMIT):  # verified entries make room again
        machine.note_success(f"/backend-api/r/{number}", NOW + 10)
    machine.note_restricted("/backend-api/r/overflow", NOW + 11)
    assert "/backend-api/r/overflow" in machine.transport["restricted"]
    assert len(machine.transport["restricted"]) <= rotation.TRANSPORT_LIMIT


def test_the_snapshot_reports_a_pending_hold_and_the_unverified_endpoints(tmp_path):
    machine = evidence(tmp_path)
    machine.note_restricted(LIST, NOW + 1)
    machine.note_restricted(READ_A, NOW + 2)
    machine.note_success(LIST, NOW + 3)
    held = machine.snapshot({"stop:*": rotation.MACHINE_429_HOLD})["transport"]
    assert held["hold"] == rotation.HOLD_PENDING
    assert held["restricted"] == 2 and held["verified"] == 1
    assert held["unverified"] == [READ_A]
    quiet = machine.snapshot({})["transport"]
    assert quiet["hold"] == ""  # no machine hold in the controls: nothing is pending
    user = machine.snapshot({"stop:*": "owner paused everything"})["transport"]
    assert user["hold"] == ""  # a user pause is never described as a 429 hold


# --- go record / crash recovery -----------------------------------------------------------------


def test_go_record_lifecycle_and_confirmation_are_durable(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock)
    machine.begin_go(CHATS[0], "t1")
    assert machine.go["state"] == "sending"
    clock.now += 5
    machine.finish_go("sent")
    assert machine.go["state"] == "sent" and machine.go["sent_at"] == NOW + 5
    clock.now += 20
    machine.confirm_go("running", "t2")
    reread = opened(tmp_path, clock=clock).go
    assert reread["confirmation"] == "running" and reread["response_turn"] == "t2"
    assert reread["confirmed_at"] == NOW + 25


def test_go_interrupted_by_a_crash_is_never_resumed_on_restart(tmp_path):
    machine = opened(tmp_path)
    machine.begin_go(CHATS[0], "t1")  # killed between persist and click
    again = opened(tmp_path)
    assert again.go["state"] == "interrupted"
    assert again.go["confirmation"] == "unconfirmed"
    assert opened(tmp_path).go["state"] == "interrupted"  # and stays so


# --- persistence and validation -----------------------------------------------------------------


def test_state_file_is_private_and_json_with_a_schema(tmp_path):
    machine = opened(tmp_path)
    queue_of(machine)
    path = tmp_path / "r.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    stored = json.loads(path.read_text())
    assert stored["schema"] == rotation.SCHEMA and stored["account"] == "home"
    assert stored["interval"] == [240.0, 360.0]
    assert set(stored) >= {
        "queue",
        "cursor",
        "current",
        "next_due",
        "last_visit",
        "go",
        "cooldown",
        "visit_started_at",
        "deferred",
        "transport",
    }


def test_write_is_atomic_a_failed_replace_keeps_the_previous_file_intact(
    tmp_path, monkeypatch
):
    machine = opened(tmp_path)
    queue_of(machine)
    path = tmp_path / "r.json"
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    machine.queue[0].last_outcome = "changed"
    with pytest.raises(OSError):
        machine.save()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]  # no stray temp file


def test_state_path_follows_the_account_database(tmp_path):
    assert rotation.rotation_path(tmp_path / "home" / "state.sqlite") == (
        tmp_path / "home" / "state.rotation.json"
    )


def corrupt(tmp_path, mutate):
    machine = opened(tmp_path)
    queue_of(machine)
    path = tmp_path / "r.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(account="glide.vc"),
        lambda d: d.update(schema=3),
        lambda d: d.update(schema="1"),
        lambda d: d.update(extra=1),
        lambda d: d.pop("queue"),
        lambda d: d.update(cursor=99),
        lambda d: d.update(cursor=-1),
        lambda d: d.update(cursor=True),
        lambda d: d.update(next_due="soon"),
        lambda d: d.update(next_due=float("nan")),
        lambda d: d.update(queue="x"),
        lambda d: d["queue"][0].update(chat="https://evil.test/c/x"),
        lambda d: d["queue"][0].update(url="https://chatgpt.com/c/" + IDS[3]),
        lambda d: d["queue"][0].update(visits=-1),
        lambda d: d["queue"].append(dict(d["queue"][0])),  # duplicate chat
        lambda d: d.update(current="not a chat"),
        lambda d: d.update(go={"state": "unknown"}),
        lambda d: d.update(cooldown={"until": "later"}),
        lambda d: d.update(interval=[1, 2]),
        lambda d: d.update(visit_started_at="x"),
        lambda d: d.update(deferred={"reason": 1, "at": 1}),
        lambda d: d.update(deferred={"reason": "x"}),
        lambda d: d.update(transport=None),
        lambda d: d.update(transport={"restricted": {}, "succeeded": {}}),
        lambda d: d["transport"]["restricted"].update({"no slash": 1.0}),
        lambda d: d["transport"]["restricted"].update({"/backend-api/x": "late"}),
        lambda d: d["transport"].update(blocked_at="soon"),
        lambda d: d["transport"].update(succeeded={"/backend-api/x": float("nan")}),
    ],
)
def test_wrong_account_schema_or_corruption_refuses_and_never_rewrites(
    tmp_path, mutate
):
    path = corrupt(tmp_path, mutate)
    before = path.read_bytes()
    with pytest.raises(RotationError):
        opened(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("payload", ["", "{", "[]", "null", "\x00\x01"])
def test_unreadable_state_refuses_and_never_rewrites(tmp_path, payload):
    path = tmp_path / "r.json"
    path.write_text(payload)
    with pytest.raises(RotationError):
        opened(tmp_path)
    assert path.read_text() == payload


def test_symlinked_state_path_is_refused(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (tmp_path / "r.json").symlink_to(target)
    with pytest.raises(RotationError, match="symlink"):
        opened(tmp_path)


def test_snapshot_exposes_the_operator_fields(tmp_path):
    clock = Clock()
    machine = opened(tmp_path, clock=clock, rng=Dice(0.5))
    queue_of(machine)
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    clock.now += 3
    machine.end_visit(entry, "failed:sent", advance=True)
    machine.begin_go(CHATS[0], "t1")
    machine.finish_go("sent")
    snap = machine.snapshot({"stop:" + CHATS[2]: "pet project"})
    assert snap["mode"] == "single-tab" and snap["account"] == "home"
    assert snap["queue_size"] == 7 and snap["current"] == CHATS[0]
    assert snap["last_visit"]["outcome"] == "failed:sent"
    assert snap["next_navigation_at"] == machine.due_at()
    assert snap["interval"] == [240.0, 360.0]
    assert snap["last_go"]["state"] == "sent"
    states = {row["chat"]: row["state"] for row in snap["queue"]}
    assert states[CHATS[0]] == "current"
    assert states[CHATS[2]] == "paused"
    assert states[CHATS[3]] == "queued"
