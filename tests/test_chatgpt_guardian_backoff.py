"""Adaptive backoff of the single-tab rotation: pure ladder, Retry-After, strikes, migration.

Injected clock and generator only: no browser, no network, no live state. The formula under test
is the one documented in docs/superpowers/plans/2026-10-28-guardian-adaptive-backoff.md.
"""

import json
import random
import sys
from dataclasses import asdict
from email.utils import formatdate
from itertools import pairwise
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import backoff, rotation, rotation_view
from agenttools_chatgpt.backoff import BackoffConfig, BackoffError
from agenttools_chatgpt.rotation import Rotation, RotationError

NOW = 1_800_000_000.0
IDS = [f"{n:08d}-0000-4000-8000-000000000000" for n in range(1, 8)]
CHATS = [f"https://chatgpt.com/c/{i}" for i in IDS]
READ_A = "/backend-api/conversation/" + IDS[0]
LIST = "/backend-api/conversations"
CFG = BackoffConfig()


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


def opened(tmp_path, *, clock=None, rng=None, name="r.json", **kwargs):
    return Rotation.open(
        tmp_path / name,
        "home",
        clock=clock or Clock(),
        rng=rng or Dice(0.0),
        **kwargs,
    )


def enrolled(tmp_path, count=3, **kwargs):
    clock = kwargs.pop("clock", None) or Clock()
    machine = opened(tmp_path, clock=clock, **kwargs)
    machine.sync({chat: chat for chat in CHATS[:count]})
    return machine, clock


def read_path(entry):
    return "/backend-api/conversation/" + entry.chat.rsplit("/", 1)[1]


def visit(
    machine,
    clock,
    outcome="complete",
    *,
    work=5.0,
    probe=False,
    entry=None,
    proven=True,
):
    """One visit; `proven` adds the fresh 2xx of the chat's own read endpoint a clean one needs."""
    advance = True
    if entry is None:
        entry, advance = machine.pick(lambda chat: True)
    machine.begin_visit(entry, probe=probe)
    if proven:
        machine.note_target_success(read_path(entry), clock.now)
    clock.now += work
    machine.end_visit(entry, outcome, advance=advance)
    return entry


def climb(machine, clock, strikes):
    """`strikes` visits that each end in a 429, spaced past every cooldown."""
    for _ in range(strikes):
        entry = machine.queue[0]
        machine.begin_visit(entry)
        machine.note_429(None)
        machine.end_visit(entry, "cooldown", advance=True)
        clock.now = machine.cooldown["until"] + 1


# --- configuration -------------------------------------------------------------------------


def test_the_documented_constants_are_the_defaults():
    assert (CFG.base, CFG.cap, CFG.jitter, CFG.clean_visits) == (600.0, 3600.0, 0.25, 3)
    assert (CFG.episode_gap, CFG.transient_base, CFG.transient_cap) == (
        120.0,
        300.0,
        1800.0,
    )
    assert CFG.validated() == CFG
    assert backoff.max_level(CFG) == 4


@pytest.mark.parametrize(
    "change",
    [
        {"base": 10.0},
        {"base": float("nan")},
        {"base": 7200.0},
        {"cap": 300.0},
        {"cap": 10**7},
        {"jitter": -0.1},
        {"jitter": 0.9},
        {"clean_visits": 0},
        {"clean_visits": 2.5},
        {"clean_visits": True},
        {"episode_gap": 5.0},
        {"transient_base": 30.0},
        {"transient_cap": 120.0},
        {"base": "600"},
    ],
)
def test_an_unsafe_configuration_is_refused(change):
    with pytest.raises(BackoffError):
        BackoffConfig(**{**asdict(CFG), **change}).validated()


def test_a_custom_configuration_is_persisted_and_an_explicit_one_replaces_it(tmp_path):
    custom = BackoffConfig(base=300.0, cap=1800.0)
    machine = opened(tmp_path, backoff=custom)
    assert machine.backoff == custom
    assert opened(tmp_path).backoff == custom  # the stored one is kept
    assert opened(tmp_path, backoff=CFG).backoff == CFG  # an explicit one wins
    assert opened(tmp_path).backoff == CFG
    assert backoff.max_level(custom) == 4


# --- the ladder ----------------------------------------------------------------------------


def test_the_ladder_doubles_from_ten_minutes_to_the_one_hour_cap():
    assert [backoff.ladder(level, CFG) for level in range(7)] == [
        0.0,
        600.0,
        1200.0,
        2400.0,
        3600.0,
        3600.0,
        3600.0,
    ]


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_jitter_is_positive_bounded_and_never_exceeds_the_cap(level):
    low, high = backoff.delay_range(level, CFG)
    delays = [backoff.adaptive_delay(level, CFG, u / 100) for u in range(100)]
    assert min(delays) == low and max(delays) <= high <= CFG.cap
    assert max(delays) - min(delays) > 0.9 * (
        high - low
    )  # randomness survives at the cap
    assert high == min(CFG.cap, backoff.ladder(level, CFG) * 1.25)


def test_the_transient_ladder_is_bounded():
    assert [backoff.transient_delay(n, CFG, 0.0) for n in range(1, 7)] == [
        300.0,
        600.0,
        1200.0,
        1800.0,
        1800.0,
        1800.0,
    ]
    assert backoff.transient_delay(10**6, CFG, 1.0) == CFG.transient_cap


# --- Retry-After ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [("15", 15.0), (" 120 ", 120.0), ("0", 0.0), ("200000", 200000.0)],
)
def test_retry_after_delta_seconds_are_taken_literally(value, expected):
    assert backoff.retry_after_seconds(value, NOW) == expected


def test_retry_after_http_date_is_relative_to_the_clock():
    assert backoff.retry_after_seconds(
        formatdate(NOW + 5400, usegmt=True), NOW
    ) == pytest.approx(5400.0, abs=1)
    past = backoff.retry_after_seconds(formatdate(NOW - 50, usegmt=True), NOW)
    assert past is not None and past < 0


@pytest.mark.parametrize(
    "value", [None, "", "soon", "-30", "1e5", 15, "99999999999999"]
)
def test_unusable_retry_after_is_ignored(value):
    assert backoff.retry_after_seconds(value, NOW) is None


# --- 429 strikes and the cooldown deadline -------------------------------------------------


def test_repeated_429_visits_climb_the_ladder_to_the_cap_and_stay(tmp_path):
    machine, clock = enrolled(tmp_path)
    for expected in (600, 1200, 2400, 2880, 2880, 2880):  # zero draw: window floor
        entry = machine.pick(lambda chat: True)[0]
        machine.begin_visit(entry)
        machine.note_429(None)
        assert machine.cooldown["until"] == clock.now + expected
        machine.end_visit(entry, "cooldown", advance=True)
        clock.now = machine.cooldown["until"] + 1
    assert machine.pressure.level == 4 and machine.pressure.strikes == 6


def test_a_burst_of_one_hundred_429s_is_one_strike(tmp_path):
    machine, clock = enrolled(tmp_path)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    for step in range(100):
        clock.now = NOW + step * 0.01
        machine.note_429("15")
    assert machine.pressure.level == 1 and machine.pressure.strikes == 1
    assert machine.pressure.responses_429 == 100
    assert (
        machine.cooldown["until"] == NOW + 600
    )  # the 15 s Retry-After never shortened it


@pytest.mark.parametrize("header", ["15", None])
def test_a_small_or_missing_retry_after_never_shortens_the_adaptive_delay(
    tmp_path, header
):
    machine, clock = enrolled(tmp_path)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429(header)
    assert machine.cooldown["until"] - clock.now >= 600.0


def test_a_larger_server_delay_is_never_capped(tmp_path):
    machine, clock = enrolled(tmp_path)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429(str(3 * 86400))
    assert machine.cooldown["until"] == clock.now + 3 * 86400
    assert machine.cooldown["source"] == "retry-after"


def test_an_http_date_retry_after_longer_than_the_ladder_wins(tmp_path):
    machine = enrolled(tmp_path)[0]
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429(formatdate(NOW + 5400, usegmt=True))
    assert machine.cooldown["until"] == pytest.approx(NOW + 5400, abs=1)


def test_a_burst_extends_the_deadline_only_by_the_server_delay(tmp_path):
    machine, clock = enrolled(tmp_path)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429("30")
    first = machine.cooldown["until"]
    clock.now += 2
    machine.note_429("7200")
    assert machine.cooldown["until"] == clock.now + 7200 > first
    clock.now += 1
    machine.note_429("30")
    assert machine.cooldown["until"] == NOW + 2 + 7200  # never shrinks


def test_a_burst_never_redraws_the_due_time(tmp_path):
    # open draw, begin_visit draw, adaptive jitter 0.5, then 1.0 for any redraw
    machine, clock = enrolled(tmp_path, rng=Dice(0.0, 0.0, 0.5, 1.0))
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429("30")
    assert set(machine.cooldown) == {"until", "reason", "source", "hold"}
    assert machine.due_at() == machine.cooldown["until"] == NOW + 675
    for _ in range(20):
        clock.now += 0.5
        machine.note_429("30")
    assert machine.due_at() == machine.cooldown["until"] == NOW + 675


def test_a_separated_episode_outside_the_cooldown_is_a_new_strike(tmp_path):
    machine, clock = enrolled(tmp_path)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429(None)
    clock.now += 700  # the cooldown is over; no new visit was issued
    machine.note_429(None)
    assert machine.pressure.level == 2 and machine.pressure.strikes == 2


def test_a_429_while_cooling_from_the_page_itself_is_not_a_new_episode(tmp_path):
    machine, clock = enrolled(tmp_path)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429(None)
    clock.now += 300  # still inside the cooldown, past the episode gap
    machine.note_429(None)
    assert machine.pressure.level == 1 and machine.pressure.strikes == 1


def test_a_new_issued_visit_is_a_new_episode_even_right_after_the_previous(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 2)
    assert machine.pressure.level == 2


# --- gradual recovery ----------------------------------------------------------------------


def test_three_clean_visits_lower_the_pressure_by_exactly_one_level(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 3)
    assert machine.pressure.level == 3
    for expected in (3, 3, 2, 2, 2, 1, 1, 1, 0):
        visit(machine, clock, entry=machine.queue[1])
        assert machine.pressure.level == expected


def test_unrelated_success_responses_never_reduce_the_pressure(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 2)
    for path in (LIST, "/backend-api/me", READ_A, "/backend-api/conversation/x"):
        clock.now += 1
        machine.note_success(path, clock.now)
        machine.note_target_success(path, clock.now)
    assert machine.pressure.level == 2 and machine.pressure.clean_streak == 0


def test_a_429_resets_the_clean_streak(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 2)
    visit(machine, clock, entry=machine.queue[1])
    visit(machine, clock, entry=machine.queue[1])
    assert machine.pressure.clean_streak == 2
    climb(machine, clock, 1)
    assert machine.pressure.clean_streak == 0 and machine.pressure.level == 3


def test_a_visit_with_a_429_inside_it_is_never_clean(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 1)
    for _ in range(3):
        entry = machine.pick(lambda chat: True)[0]
        machine.begin_visit(entry)
        machine.note_429(None)
        machine.end_visit(entry, "complete", advance=True)  # the page still read fine
        clock.now = machine.cooldown["until"] + 1
    assert machine.pressure.level == 4


def test_spacing_under_pressure_is_the_ladder_and_level_zero_is_unchanged(tmp_path):
    machine, clock = enrolled(tmp_path, rng=Dice(0.0))
    assert machine.effective_interval() == [240.0, 360.0]
    climb(machine, clock, 2)
    assert machine.effective_interval() == [1200.0, 1500.0]
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    assert machine.next_due == clock.now + 1200.0


def test_the_ladder_is_persisted_across_a_restart(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 3)
    machine.begin_visit(machine.pick(lambda chat: True)[0])
    machine.note_429("90")
    again = opened(tmp_path, clock=clock)
    assert again.pressure == machine.pressure
    assert again.cooldown == machine.cooldown and again.backoff == machine.backoff


def test_every_chats_retry_record_and_the_queue_survive_a_restart_without_a_burst(
    tmp_path,
):
    machine, clock = enrolled(tmp_path)
    for chat in (CHATS[0], CHATS[1]):
        entry = next(e for e in machine.queue if e.chat == chat)
        visit(machine, clock, "error:TimeoutError", entry=entry)
    stored = [(e.chat, dict(e.retry) if e.retry else None) for e in machine.queue]
    due = machine.due_at()
    again = opened(tmp_path, clock=clock)
    assert [(e.chat, e.retry) for e in again.queue] == stored
    assert any(
        e.retry and {"failures", "since", "at"} <= e.retry.keys() for e in again.queue
    )
    assert (
        again.due_at() == due
    )  # a future deadline is kept exactly: no burst, no redraw
    other = Rotation.open(
        tmp_path / "other.json", "glide.vc", clock=clock, rng=Dice(0.0)
    )
    assert other.queue == [] and other.pressure.level == 0 and other.cooldown is None


# --- transient failures --------------------------------------------------------------------


def test_a_timeout_keeps_the_chat_enrolled_with_a_bounded_growing_retry(tmp_path):
    machine, clock = enrolled(tmp_path, count=2)
    target = machine.queue[0]
    for expected in (300, 600, 1200, 1800, 1800):
        clock.now += 5000
        visit(machine, clock, "error:TimeoutError", entry=target, work=1.0)
        assert target.retry["at"] == clock.now + expected
        assert target.retry["reason"] == "error:TimeoutError"
        assert target.last_ok_at is None
    assert [e.chat for e in machine.queue] == CHATS[:2]
    assert machine.cooldown is None


def test_a_failed_chat_is_skipped_until_its_retry_and_never_starves_the_rest(tmp_path):
    machine, clock = enrolled(tmp_path, count=3)
    bad = machine.queue[0]
    visit(machine, clock, "error:TimeoutError", entry=bad)
    retry_at = bad.retry["at"]
    seen = []
    for _ in range(9):
        clock.now += 100
        before = clock.now
        entry = visit(machine, clock)
        seen.append(entry.chat)
        if before + 5 < retry_at:
            assert entry.chat != bad.chat  # kept, not erased, but not retried early
    assert seen.count(CHATS[1]) >= 2 and seen.count(CHATS[2]) >= 2
    assert bad.chat in seen and bad.retry is None and bad.last_ok_at is not None
    assert [e.chat for e in machine.queue] == CHATS[:3]


def test_due_retries_alternate_with_the_cursor_so_one_chat_cannot_take_every_slot(
    tmp_path,
):
    machine, clock = enrolled(tmp_path, count=3)
    bad = machine.queue[0]
    picks = []
    for _ in range(9):
        clock.now += 5000
        entry, advance = machine.pick(lambda chat: True)
        machine.begin_visit(entry)
        clock.now += 1
        machine.end_visit(
            entry, "error:TimeoutError" if entry is bad else "complete", advance
        )
        picks.append(entry.chat)
    assert picks.count(bad.chat) <= 5 and set(picks) == set(CHATS[:3])
    assert all(
        not (a == b == bad.chat) for a, b in pairwise(picks)
    )  # never two failing slots in a row


def test_two_consecutive_transient_visits_slow_the_account_and_a_clean_one_resets(
    tmp_path,
):
    machine, clock = enrolled(tmp_path, count=3)
    visit(machine, clock, "error:TimeoutError")
    assert machine.pressure.transient_streak == 1
    clock.now = machine.due_at()
    visit(machine, clock, "nav-error:Error")
    assert machine.pressure.transient_streak == 2
    assert machine.due_at() >= clock.now + 300  # account-wide push, bounded
    assert machine.due_at() <= clock.now + CFG.transient_cap * 1.25 + 1
    visit(machine, clock)
    assert machine.pressure.transient_streak == 0


def test_a_5xx_during_a_visit_is_transient_never_a_hold(tmp_path):
    machine, clock = enrolled(tmp_path)
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    machine.note_server_error()
    clock.now += 3
    machine.end_visit(entry, "complete", advance=True)
    assert entry.retry["reason"] == "http-5xx" and entry.last_ok_at is None
    assert machine.cooldown is None and machine.pressure.level == 0


def test_a_5xx_outside_any_visit_changes_nothing(tmp_path):
    machine, clock = enrolled(tmp_path)
    visit(machine, clock)
    before = machine.pressure.to_json()
    machine.note_server_error()
    assert machine.pressure.to_json() == before


# --- durable per-chat state ----------------------------------------------------------------


def test_a_429_visit_keeps_a_durable_retry_and_never_counts_as_fresh(tmp_path):
    machine, clock = enrolled(tmp_path)
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    machine.note_429("900")
    clock.now += 2
    machine.end_visit(entry, "cooldown", advance=True)
    assert entry.retry["reason"] == "http-429" and entry.retry["failures"] == 1
    assert entry.retry["at"] == machine.cooldown["until"]
    assert entry.last_ok_at is None and entry.visits == 1
    again = opened(tmp_path, clock=clock)
    assert again.queue[0].retry == entry.retry and again.queue[0].last_ok_at is None


def test_a_429_retry_clears_only_with_a_confirmed_target_success(tmp_path):
    machine, clock = enrolled(tmp_path)
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    machine.note_429(None)
    machine.end_visit(entry, "cooldown", advance=True)
    clock.now = machine.cooldown["until"] + 1
    machine.begin_visit(entry, probe=True)
    clock.now += 2
    machine.end_visit(entry, "complete", advance=False)  # read fine, endpoint unproven
    assert entry.retry is not None and entry.last_ok_at is None
    assert machine.pressure.clean_streak == 0  # not a clean target visit either
    machine.begin_visit(entry, probe=True)
    clock.now += 1
    machine.note_target_success(READ_A, clock.now)
    clock.now += 1
    machine.end_visit(entry, "complete", advance=False)
    assert entry.retry is None and entry.last_ok_at == clock.now
    assert entry.confirmed_at is not None and machine.pressure.clean_streak == 1


def test_a_paused_chat_stays_in_the_queue_as_an_explicit_exclusion(tmp_path):
    machine, clock = enrolled(tmp_path)
    visit(machine, clock, "error:TimeoutError", entry=machine.queue[1])
    snap = machine.snapshot({"stop:" + CHATS[1]: "owner paused"})
    rows = {row["chat"]: row for row in snap["queue"]}
    assert rows[CHATS[1]]["state"] == "paused" and rows[CHATS[1]]["retry"] is not None
    assert snap["coverage"]["excluded"] == [
        {"chat": CHATS[1], "reason": "owner paused"}
    ]
    assert machine.pick(lambda chat: chat != CHATS[1])[0].chat != CHATS[1]
    assert len(machine.queue) == 3


# --- probe targets -------------------------------------------------------------------------


def test_the_probe_target_is_the_chat_of_the_unverified_read_endpoint(tmp_path):
    machine = enrolled(tmp_path, count=4)[0]
    machine.note_restricted("/backend-api/conversation/" + IDS[2], NOW)
    picked = machine.pick(lambda chat: True, probe=True)
    assert picked[0].chat == CHATS[2] and picked[1] is False  # the cursor is untouched
    cursor = machine.cursor
    machine.begin_visit(picked[0], probe=True)
    machine.end_visit(picked[0], "complete", advance=picked[1])
    assert machine.cursor == cursor and machine.pressure.probes == 1


def test_several_unverified_endpoints_are_probed_oldest_visit_first(tmp_path):
    machine = enrolled(tmp_path, count=4)[0]
    machine.note_restricted("/backend-api/conversation/" + IDS[1], NOW)
    machine.note_restricted("/backend-api/conversations/" + IDS[3], NOW)
    machine.queue[1].last_visit_at = NOW - 10
    machine.queue[3].last_visit_at = NOW - 500
    assert machine.pick(lambda chat: True, probe=True)[0].chat == CHATS[3]


def test_an_unmappable_endpoint_probes_the_ordinary_chat_for_the_listing(tmp_path):
    machine = enrolled(tmp_path, count=3)[0]
    machine.note_restricted(LIST, NOW)
    assert machine.pick(lambda chat: True, probe=True)[0].chat == CHATS[0]


def test_a_legacy_hold_gets_an_explicit_unverifiable_state_and_the_cap_cadence(
    tmp_path,
):
    machine, clock = enrolled(tmp_path)
    machine.adopt_legacy_hold()
    assert machine.unverified_paths() == [rotation.FOREIGN_HOLD]
    assert machine.cooldown["until"] == clock.now + CFG.cap
    assert machine.cooldown["reason"] == "legacy-hold"
    clock.now = machine.cooldown["until"] + 1
    assert machine.hold_unverified() == "unverified"  # the clock alone never clears it
    machine.rearm_probe()
    assert machine.cooldown["until"] == clock.now + CFG.cap
    held = machine.snapshot({"stop:*": rotation.MACHINE_429_HOLD})
    assert held["pressure"]["hold_state"] == "legacy-unverifiable"


def test_an_inconclusive_probe_rearms_a_quiet_period_without_a_strike(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 2)
    machine.note_restricted(READ_A, NOW)
    strikes = machine.pressure.strikes
    machine.rearm_probe()
    assert machine.pressure.strikes == strikes
    assert machine.cooldown["until"] == clock.now + 1200
    assert machine.cooldown["reason"] == "hold-recheck"


# --- migration and read-only telemetry -----------------------------------------------------


def as_schema_one(path):
    """The shape 0.3.14 wrote: no pressure, no backoff, 5-key entries, 4-key cooldown."""
    data = json.loads(path.read_text())
    data["schema"] = 1
    for key in ("pressure", "backoff"):
        data.pop(key)
    for entry in data["queue"]:
        for key in ("retry", "last_ok_at", "confirmed_at"):
            entry.pop(key)
    path.write_text(json.dumps(data, sort_keys=True, indent=1) + "\n")
    return data


def legacy_file(tmp_path):
    machine, clock = enrolled(tmp_path, count=3)
    for outcome in ("complete", "failed:sent", "working"):
        visit(machine, clock, outcome)
    machine.begin_go(CHATS[1], "turn-1")
    machine.finish_go("sent")
    machine.record_owned([CHATS[0], CHATS[2]])
    machine.set_cooldown(
        NOW + 5000, "http-429", "retry-after", rotation.MACHINE_429_HOLD
    )
    path = tmp_path / "r.json"
    return path, as_schema_one(path), clock


def test_a_schema_one_file_migrates_losslessly_on_open(tmp_path):
    path, old, clock = legacy_file(tmp_path)
    machine = opened(tmp_path, clock=clock)
    new = json.loads(path.read_text())
    assert new["schema"] == rotation.SCHEMA == 2
    kept = set(old["queue"][0])
    assert [{k: v for k, v in e.items() if k in kept} for e in new["queue"]] == old[
        "queue"
    ]
    for key in ("cursor", "current", "go", "owned_urls", "transport", "tabs"):
        assert new[key] == old[key], key
    assert machine.pressure.level == 0 and machine.pressure.strikes == 0
    assert machine.backoff == CFG
    assert [e.visits for e in machine.queue] == [1, 1, 1]
    assert machine.cooldown["until"] == NOW + 5000 and machine.go["state"] == "sent"
    assert all(e.retry is None for e in machine.queue)


def test_a_schema_one_file_is_never_written_by_the_read_only_views(tmp_path):
    path, _old, clock = legacy_file(tmp_path)
    state = tmp_path / "state.sqlite"
    twin = rotation.rotation_path(state)
    twin.write_bytes(path.read_bytes())
    before = twin.read_bytes()
    view = Rotation.read(twin, "home", clock=clock)
    assert view.pressure.level == 0 and view.queue[0].retry is None
    block = rotation_view.block(
        state, "home", {rotation.MODE_CONTROL: "1"}, clock=clock
    )
    assert block is not None and twin.read_bytes() == before
    assert block["pressure"]["level"] == 0 and block["queue_size"] == 3


def test_a_current_schema_file_is_not_rewritten_by_a_read(tmp_path):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 1)
    path = tmp_path / "r.json"
    before = path.read_bytes()
    Rotation.read(path, "home", clock=clock).snapshot({})
    assert path.read_bytes() == before


def test_every_unsupported_schema_is_refused_and_never_rewritten(tmp_path):
    enrolled(tmp_path)
    path = tmp_path / "r.json"
    good = json.loads(path.read_text())
    for schema in (0, 3, "2", True, None):
        path.write_text(json.dumps({**good, "schema": schema}))
        before = path.read_bytes()
        with pytest.raises(RotationError):
            opened(tmp_path)
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["pressure"].update(level=99),
        lambda d: d["pressure"].update(level=-1),
        lambda d: d["pressure"].update(level=True),
        lambda d: d["pressure"].update(recent=["ok"] * 99),
        lambda d: d["pressure"].update(recent=["fine"]),
        lambda d: d["pressure"].update(last_429_at="soon"),
        lambda d: d["pressure"].update(extra=1),
        lambda d: d["pressure"].pop("issued"),
        lambda d: d.update(pressure=None),
        lambda d: d["backoff"].update(base=1.0),
        lambda d: d.update(backoff=[]),
        lambda d: d["queue"][0].update(retry={"at": 1.0}),
        lambda d: d["queue"][0].update(
            retry={"at": "x", "reason": "r", "failures": 1, "since": 1.0}
        ),
        lambda d: d["queue"][0].update(last_ok_at="x"),
        lambda d: d["queue"][0].pop("retry"),
    ],
)
def test_a_corrupt_pressure_or_retry_record_refuses_and_never_rewrites(
    tmp_path, mutate
):
    enrolled(tmp_path)
    path = tmp_path / "r.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(RotationError):
        opened(tmp_path)
    assert path.read_bytes() == before


# --- telemetry -----------------------------------------------------------------------------


def test_the_snapshot_reports_actual_pressure_retry_and_honest_coverage(tmp_path):
    machine, clock = enrolled(tmp_path, count=3)
    snap = machine.snapshot()
    assert snap["coverage"]["complete"] is False  # registered is not checked
    assert snap["coverage"]["never_clean"] == 3
    assert snap["coverage"]["oldest_unvisited_age"] is None
    assert snap["pressure"]["success_rate"] is None  # no observation, no rate
    assert snap["pressure"]["last_confirmed_target_success_at"] is None
    for _ in range(3):
        visit(machine, clock)
        clock.now += 400
    snap = machine.snapshot()
    assert snap["coverage"]["complete"] is True
    assert snap["coverage"]["never_clean"] == 0
    assert snap["pressure"]["success_rate"] == 1.0
    entry = machine.pick(lambda chat: True)[0]
    machine.begin_visit(entry)
    machine.note_429(None)
    clock.now += 2
    machine.end_visit(entry, "cooldown", advance=True)
    snap = machine.snapshot()
    pressure, coverage = snap["pressure"], snap["coverage"]
    assert coverage["complete"] is False and coverage["retry_pending"] == 1
    assert coverage["next_retry_at"] == machine.cooldown["until"]
    assert pressure["level"] == 1 and pressure["strikes"] == 1
    assert pressure["retry_at"] == machine.cooldown["until"]
    assert pressure["retry_reason"] == "http-429"
    assert pressure["next_due"] == machine.next_due
    assert pressure["effective_interval"] == [600.0, 750.0]
    assert pressure["recent"] == {"ok": 3, "429": 1, "error": 0, "window": 4}
    assert pressure["success_rate"] == 0.75
    assert pressure["responses_429"] == 1 and pressure["controlled_probes"] == 0
    assert pressure["failure_streak"] == 0


def test_the_recent_window_is_bounded(tmp_path):
    machine, clock = enrolled(tmp_path)
    for _ in range(backoff.RECENT_LIMIT + 15):
        visit(machine, clock)
    assert len(machine.pressure.recent) == backoff.RECENT_LIMIT
    assert machine.snapshot()["pressure"]["recent"]["window"] == backoff.RECENT_LIMIT


def test_the_default_generator_still_draws_exactly_the_old_interval_at_level_zero(
    tmp_path,
):
    machine = opened(tmp_path, rng=random.Random(20261028))
    draws = [machine.draw() for _ in range(3000)]
    assert min(draws) >= 240.0 and max(draws) <= 360.0


# --- one retry deadline --------------------------------------------------------------------


def failing_gaps(machine, clock, count):
    """Start-to-due gap of `count` consecutive 429 visits, each started exactly when due."""
    gaps = []
    for _ in range(count):
        entry = machine.queue[0]
        started = clock.now
        machine.begin_visit(entry)
        machine.note_429(None)
        machine.end_visit(entry, "cooldown", advance=True)
        assert (
            machine.due_at() == machine.cooldown["until"]
        )  # no second interval after it
        gaps.append(machine.due_at() - started)
        clock.now = machine.due_at()
    return gaps


def test_zero_jitter_gaps_are_exactly_the_advertised_ten_twenty_forty_minutes(tmp_path):
    machine, clock = enrolled(tmp_path, rng=Dice(0.0))
    assert failing_gaps(machine, clock, 5) == [600.0, 1200.0, 2400.0, 2880.0, 2880.0]


@pytest.mark.parametrize("draws", [(0.4, 0.8), (0.0, 1.0), (0.999, 0.25)])
def test_jittered_gaps_are_the_ladder_times_one_plus_a_quarter_draw_and_nothing_more(
    tmp_path, draws
):
    machine, clock = enrolled(tmp_path, rng=Dice(0.0))
    gaps = []
    for u in draws:
        machine.rng = Dice(u)
        gaps += failing_gaps(machine, clock, 1)
    assert gaps == [
        pytest.approx(600.0 * (1 + 0.25 * draws[0]), abs=1e-4),
        pytest.approx(1200.0 * (1 + 0.25 * draws[1]), abs=1e-4),
    ]
    assert max(gaps) < 1500.0 + 1  # the old extra 240..360 interval is gone


@pytest.mark.parametrize("header", ["5000", "86400", "259200"])
def test_a_long_retry_after_sets_the_gap_exactly(tmp_path, header):
    machine = enrolled(tmp_path, rng=Dice(0.0))[0]
    machine.begin_visit(machine.queue[0])
    machine.note_429(header)
    machine.end_visit(machine.queue[0], "cooldown", advance=True)
    assert machine.due_at() == NOW + float(header) == machine.cooldown["until"]
    assert machine.cooldown["source"] == "retry-after"


def test_an_http_date_retry_after_sets_the_gap_exactly(tmp_path):
    machine = enrolled(tmp_path, rng=Dice(0.0))[0]
    machine.begin_visit(machine.queue[0])
    machine.note_429(formatdate(NOW + 5400, usegmt=True))
    machine.end_visit(machine.queue[0], "cooldown", advance=True)
    assert machine.due_at() == NOW + 5400


def test_a_later_longer_retry_after_still_extends_the_one_deadline(tmp_path):
    machine, clock = enrolled(tmp_path, rng=Dice(0.0))
    machine.begin_visit(machine.queue[0])
    machine.note_429("30")
    assert machine.due_at() == NOW + 600
    clock.now += 2
    machine.note_429("7200")
    assert machine.due_at() == clock.now + 7200 == machine.cooldown["until"]
    clock.now += 1
    machine.note_429("30")
    assert machine.due_at() == NOW + 2 + 7200  # never shortened


def test_the_minimum_spacing_still_bounds_a_short_adaptive_deadline(tmp_path):
    machine, clock = enrolled(tmp_path, rng=Dice(1.0))
    machine.begin_visit(machine.queue[0])
    spacing = machine.next_due - clock.now
    machine.note_429("1")
    assert (
        machine.due_at() >= clock.now + spacing
    )  # never earlier than the visit's spacing


def test_after_a_restart_an_overdue_deadline_is_redrawn_once_from_now(tmp_path):
    machine, clock = enrolled(tmp_path, rng=Dice(0.0))
    machine.begin_visit(machine.queue[0])
    machine.note_429(None)
    machine.end_visit(machine.queue[0], "cooldown", advance=True)
    clock.now = machine.due_at() + 50_000  # down for a long time
    again = opened(tmp_path, clock=clock)
    assert again.due_at() == clock.now + 600  # one redraw, no catch-up burst
    assert again.deferred == {"reason": "overdue", "at": clock.now}
    assert opened(tmp_path, clock=clock).due_at() == again.due_at()  # and only once


def test_after_a_restart_a_future_deadline_is_kept_exactly(tmp_path):
    machine, clock = enrolled(tmp_path, rng=Dice(0.0))
    machine.begin_visit(machine.queue[0])
    machine.note_429("5000")
    machine.end_visit(machine.queue[0], "cooldown", advance=True)
    clock.now += 1000
    again = opened(tmp_path, clock=clock)
    assert again.due_at() == NOW + 5000 == again.cooldown["until"]


# --- jitter at the cap ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "level, window",
    [
        (1, (600.0, 750.0)),
        (2, (1200.0, 1500.0)),
        (3, (2400.0, 3000.0)),
        (4, (2880.0, 3600.0)),
    ],
)
def test_the_delay_window_is_exact_and_jitter_survives_at_the_cap(level, window):
    assert backoff.delay_range(level, CFG) == window
    delays = [backoff.adaptive_delay(level, CFG, u / 100) for u in range(100)]
    assert min(delays) == window[0] and max(delays) < window[1]
    assert len(set(delays)) == 100  # a real spread, never one synchronised point


def test_the_cap_window_is_not_a_single_point():
    delays = [backoff.adaptive_delay(4, CFG, u) for u in (0.0, 0.5, 0.999)]
    assert len(set(delays)) == 3 and all(2880.0 <= d <= 3600.0 for d in delays)
    assert delays[0] == 2880.0 and delays[1] == 3240.0


def test_levels_beyond_the_cap_and_below_zero_are_bounded():
    assert backoff.delay_range(9, CFG) == (2880.0, 3600.0)
    assert backoff.adaptive_delay(0, CFG, 0.7) == 0.0
    assert backoff.adaptive_delay(-3, CFG, 0.7) == 0.0


def test_a_zero_jitter_configuration_stays_deterministic():
    flat = BackoffConfig(jitter=0.0)
    assert [backoff.adaptive_delay(4, flat, u) for u in (0.0, 0.5, 0.999)] == [
        3600.0
    ] * 3
    assert backoff.delay_range(2, flat) == (1200.0, 1200.0)


def test_the_effective_interval_follows_the_delay_window():
    assert backoff.effective_interval(0, (240.0, 360.0), CFG) == [240.0, 360.0]
    assert backoff.effective_interval(3, (240.0, 360.0), CFG) == [2400.0, 3000.0]
    assert backoff.effective_interval(4, (240.0, 360.0), CFG) == [2880.0, 3600.0]


def test_at_the_cap_a_longer_retry_after_always_dominates_every_draw(tmp_path):
    for n, u in enumerate((0.0, 0.5, 0.999)):
        machine, clock = enrolled(tmp_path, name=f"cap{n}.json", rng=Dice(0.0))
        climb(machine, clock, 3)
        machine.rng = Dice(u)
        machine.begin_visit(machine.queue[0])
        machine.note_429("7200")
        assert machine.pressure.level == 4
        assert machine.cooldown["until"] == clock.now + 7200.0


def test_at_the_cap_a_smaller_retry_after_never_shortens_the_jittered_delay(tmp_path):
    untils = []
    for n, u in enumerate((0.0, 0.5, 0.999)):
        machine, clock = enrolled(tmp_path, name=f"small{n}.json", rng=Dice(0.0))
        climb(machine, clock, 3)
        machine.rng = Dice(u)
        machine.begin_visit(machine.queue[0])
        machine.note_429("100")
        untils.append(machine.cooldown["until"] - clock.now)
    assert untils == [
        pytest.approx(backoff.adaptive_delay(4, CFG, u)) for u in (0.0, 0.5, 0.999)
    ]
    assert len(set(untils)) == 3 and all(2880.0 <= d <= 3600.0 for d in untils)


# --- a clean visit needs real transport evidence -------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        "unknown",
        "draft",
        "failed:confirmation-blocked",
        "failed:sent",
        "complete",
        "working",
        "approval:x",
    ],
)
def test_a_dom_outcome_alone_is_unproven_never_success(outcome):
    assert backoff.outcome_kind(outcome) == "unproven"


def test_the_other_outcome_kinds_are_unchanged():
    assert backoff.outcome_kind("cooldown") == "429"
    assert backoff.outcome_kind("error:TimeoutError") == "transient"
    assert backoff.outcome_kind("nav-error:Error") == "transient"
    assert backoff.outcome_kind("challenge") == "neutral"
    assert backoff.outcome_kind("unreadable:blank") == "neutral"


def inconclusive(machine, clock, outcome, noise):
    """Hurt chat 1 with a timeout, then end a visit of it that has no proof of its own read."""
    entry = machine.queue[1]
    climb(machine, clock, 2)
    visit(machine, clock, "error:TimeoutError", entry=entry, proven=False)
    state = (
        machine.pressure.level,
        machine.pressure.clean_streak,
        list(machine.pressure.recent),
        dict(entry.retry),
    )
    machine.begin_visit(entry)
    noise(machine, clock, entry)
    clock.now += 3
    machine.end_visit(entry, outcome, advance=True)
    return entry, state


def no_evidence(machine, clock, entry):
    del machine, clock, entry


def unrelated_successes(machine, clock, entry):
    del entry
    for path in (
        "/backend-api/me",
        LIST,
        "/backend-api/conversation/" + IDS[2],
        "/backend-api/conversation/" + IDS[1] + "/stream_status",
    ):
        machine.note_success(path, clock.now)
        machine.note_target_success(path, clock.now)


def stale_confirmation(machine, clock, entry):
    del clock
    entry.confirmed_at = machine.visit_started_at - 1.0  # a 2xx of an EARLIER visit


@pytest.mark.parametrize("outcome", ["complete", "unknown", "draft", "failed:sent"])
@pytest.mark.parametrize(
    "noise", [no_evidence, unrelated_successes, stale_confirmation]
)
def test_a_visit_without_its_own_fresh_read_2xx_is_neutral_and_changes_nothing(
    tmp_path, outcome, noise
):
    machine, clock = enrolled(tmp_path)
    entry, (level, streak, recent, retry) = inconclusive(machine, clock, outcome, noise)
    assert machine.pressure.level == level == 2
    assert machine.pressure.clean_streak == streak
    assert machine.pressure.recent == recent  # no success-rate sample either
    assert entry.retry == retry and entry.last_ok_at is None
    snap = machine.snapshot()
    assert len(machine.queue) == 3 and snap["coverage"]["never_clean"] == 3
    assert (
        snap["coverage"]["retry_pending"] >= 1 and snap["coverage"]["complete"] is False
    )


@pytest.mark.parametrize(
    "outcome", ["complete", "unknown", "draft", "failed:confirmation-blocked"]
)
def test_a_fresh_matching_read_2xx_in_the_visit_is_transport_ok_whatever_the_task_says(
    tmp_path, outcome
):
    machine, clock = enrolled(tmp_path)
    climb(machine, clock, 1)
    for _ in range(3):
        visit(machine, clock, outcome, entry=machine.queue[1])
    assert machine.pressure.level == 0 and machine.queue[1].last_ok_at is not None
    assert machine.pressure.recent[-3:] == ["ok", "ok", "ok"]
    assert machine.snapshot()["coverage"]["never_clean"] == 2


def test_a_429_inside_a_visit_with_a_matching_2xx_still_wins(tmp_path):
    machine, clock = enrolled(tmp_path)
    entry = machine.queue[1]
    machine.begin_visit(entry)
    machine.note_target_success(read_path(entry), clock.now)
    machine.note_429(None)
    clock.now += 2
    machine.end_visit(entry, "complete", advance=True)
    assert machine.pressure.recent == ["429"] and entry.retry["reason"] == "http-429"
    assert entry.last_ok_at is None and machine.pressure.level == 1


# --- fair half-open probes under a machine hold --------------------------------------------


def probe_slot(machine, clock, failing):
    """One half-open slot at its due time: A keeps answering 429, every other chat reads fine."""
    clock.now = max(clock.now, machine.due_at())
    entry, advance = machine.pick(lambda chat: True, probe=True)
    machine.begin_visit(entry, probe=True)
    clock.now += 1
    if entry.chat == failing:
        machine.note_restricted(read_path(entry), clock.now)
        machine.note_429("15")
        machine.end_visit(entry, "cooldown", advance=advance)
    else:
        machine.note_success(read_path(entry), clock.now)
        machine.note_target_success(read_path(entry), clock.now)
        clock.now += 1
        machine.end_visit(entry, "complete", advance=advance)
    return entry


def held_queue(tmp_path, count):
    """A queue whose first chat's read endpoint is restricted and unverified under a hold."""
    machine, clock = enrolled(tmp_path, count=count, rng=Dice(0.0))
    machine.note_restricted(READ_A, NOW)
    machine.set_cooldown(NOW, "http-429", "adaptive", rotation.MACHINE_429_HOLD)
    assert machine.hold_unverified() == "unverified"
    return machine, clock


def test_a_failing_chat_never_takes_every_half_open_slot(tmp_path):
    machine, clock = held_queue(tmp_path, 3)
    picked, starts = [], []
    for _ in range(8):
        cooldown = dict(machine.cooldown)
        entry = probe_slot(machine, clock, CHATS[0])
        picked.append(entry.chat)
        starts.append(machine.last_visit["started_at"])
        if entry.chat != CHATS[0]:
            assert machine.cooldown == cooldown  # a neighbour's success touches no hold
            assert machine.hold_unverified() in ("unverified", "cooldown")
    assert picked == [CHATS[n] for n in (0, 1, 0, 2, 0, 1, 0, 2)]  # never A,A,A
    a, b, c = machine.queue
    assert [e.chat for e in machine.queue] == CHATS[:3]  # all three stay enrolled
    assert a.retry["reason"] == "http-429" and a.retry["failures"] == 4
    assert a.last_ok_at is None
    assert b.visits == c.visits == 2 and b.retry is None and c.retry is None
    assert b.last_ok_at is not None and c.last_ok_at is not None
    assert all(y - x >= 240.0 for x, y in pairwise(starts))  # paced, no burst
    assert machine.hold_unverified() in ("unverified", "cooldown")


def test_the_hold_releases_only_when_the_failing_endpoint_itself_succeeds(tmp_path):
    machine, clock = held_queue(tmp_path, 3)
    for _ in range(4):
        probe_slot(machine, clock, CHATS[0])  # A, B, A, C
    clock.now = max(clock.now, machine.due_at())
    assert machine.hold_unverified() == "unverified"  # B and C succeeded, A did not
    entry, advance = machine.pick(lambda chat: True, probe=True)
    assert (
        entry.chat == CHATS[0]
    )  # the turn after a neighbour returns to the failed endpoint
    machine.begin_visit(entry, probe=True)
    clock.now += 1
    machine.note_success(READ_A, clock.now)
    machine.note_target_success(READ_A, clock.now)
    clock.now += 1
    machine.end_visit(entry, "complete", advance=advance)
    assert machine.hold_unverified() == "" and entry.retry is None
    machine.clear_hold()
    assert machine.cooldown is None
    laps = []
    for _ in range(3):
        clock.now = max(clock.now, machine.due_at())
        laps.append(visit(machine, clock).chat)
    assert sorted(laps) == sorted(CHATS[:3])  # the ordinary rotation covers A, B and C


def test_a_single_chat_queue_still_probes_that_chat(tmp_path):
    machine, clock = held_queue(tmp_path, 1)
    for _ in range(3):
        assert probe_slot(machine, clock, CHATS[0]).chat == CHATS[0]
    assert machine.queue[0].retry["failures"] == 3
