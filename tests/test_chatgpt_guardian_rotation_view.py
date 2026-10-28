"""Single-tab telemetry in the existing status and monitor JSON entrypoints; read-only."""

import json
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import cli, rotation, rotation_view
from agenttools_chatgpt.core import Store
from agenttools_chatgpt.monitor import MonitorStore, project_snapshot, read_source
from agenttools_chatgpt.rotation import Rotation, RotationError

IDS = [f"{n:08d}-0000-4000-8000-00000000000{n}" for n in range(1, 4)]
CHATS = [f"https://chatgpt.com/c/{i}" for i in IDS]


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def single_tab_account(tmp_path, *, marker=True, visit=True):
    """A home account in single-tab mode: three enrolled chats, one of them current."""
    state = tmp_path / "home" / "state.sqlite"
    store = Store(state, account="home")
    for chat in CHATS:
        store.enroll(chat)
    clock = Clock()
    machine = Rotation.open(
        rotation.rotation_path(state), "home", clock=clock, rng=random.Random(4)
    )
    machine.sync({chat: chat for chat in CHATS})
    if visit:
        entry = machine.pick(lambda chat: True)[0]
        machine.begin_visit(entry)
        machine.end_visit(entry, "complete", advance=True)
        machine.begin_go(CHATS[0], "t1")
        machine.finish_go("sent")
        machine.confirm_go("running", "t2")
    if marker:
        store.set_control(rotation.MODE_CONTROL, "1")
    return state, store, machine, clock


def record_observer_snapshot(state, current_chat=CHATS[0]):
    """What the passive observer stores while only the patrol tab is open."""
    now = time.time()
    controls, attempts = read_source(state, "home")
    sample = {
        "chat": current_chat,
        "title": "A task",
        "checked_at": now,
        "status": "complete",
        "running": False,
        "progress_token": "h",
        "turn": "t2",
        "turns": [],
    }
    snapshot = project_snapshot(
        "home",
        controls,
        attempts,
        [sample],
        None,
        {"connection": "connected", "heartbeat_at": now, "page_count": 1},
        now,
    )
    MonitorStore(state, "home").record(snapshot)


def run_cli(capsys, state, *argv):
    assert cli.main(["--workspace", "home", "--state", str(state), *argv]) == 0
    return json.loads(capsys.readouterr().out)


# --- block ---------------------------------------------------------------------------------------


def test_legacy_accounts_have_no_rotation_block(tmp_path):
    state = tmp_path / "home" / "state.sqlite"
    store = Store(state, account="home")
    assert rotation_view.block(state, "home", store.controls()) is None


def test_block_carries_every_operator_field(tmp_path):
    state, store, machine, clock = single_tab_account(tmp_path)
    store.stop(CHATS[2], "pet project")
    view = rotation_view.block(state, "home", store.controls(), clock=clock)
    assert view["active"] is True and view["mode"] == "single-tab"
    assert view["queue_size"] == 3 and view["current"] == CHATS[0]
    assert view["last_visit"]["outcome"] == "complete"
    assert view["next_navigation_at"] == machine.due_at()
    assert view["next_navigation_in"] > 0
    assert view["last_go"]["confirmation"] == "running"
    assert view["last_go"]["response_turn"] == "t2"
    states = {row["chat"]: row["state"] for row in view["queue"]}
    assert states == {CHATS[0]: "current", CHATS[1]: "queued", CHATS[2]: "paused"}


def test_block_shows_the_deferral_the_start_of_the_last_visit_and_the_pending_429_hold(
    tmp_path,
):
    state, store, machine, clock = single_tab_account(tmp_path)
    path = "/backend-api/conversations/" + IDS[1]
    machine.note_restricted(path, clock.now)
    machine.defer("running-protected", "running-protected")
    store.stop("*", rotation.MACHINE_429_HOLD)
    view = rotation_view.block(state, "home", store.controls(), clock=clock)
    assert view["deferred"] == {"reason": "running-protected", "at": clock.now}
    assert view["last_visit"]["outcome"] == "running-protected"
    assert view["transport"]["hold"] == rotation.HOLD_PENDING
    assert view["transport"]["unverified"] == [path]
    quiet = rotation_view.block(state, "home", {}, clock=clock)
    assert quiet["transport"]["hold"] == ""  # no machine hold in the controls


def test_block_is_read_only_even_when_the_due_time_has_long_passed(tmp_path):
    state, store, _machine, clock = single_tab_account(tmp_path)
    clock.now += 99999
    path = rotation.rotation_path(state)
    before = path.read_bytes()
    view = rotation_view.block(state, "home", store.controls(), clock=clock)
    assert path.read_bytes() == before  # a read never redraws the due time
    assert view["next_navigation_in"] == 0


def test_block_never_creates_a_file_when_the_marker_exists_but_no_state_yet(tmp_path):
    state = tmp_path / "home" / "state.sqlite"
    store = Store(state, account="home")
    store.set_control(rotation.MODE_CONTROL, "1")
    view = rotation_view.block(state, "home", store.controls())
    assert view["active"] is True and view["queue_size"] == 0
    assert not rotation.rotation_path(state).exists()


def test_inactive_state_is_reported_as_inactive(tmp_path):
    state, store, _machine, clock = single_tab_account(tmp_path, marker=False)
    view = rotation_view.block(state, "home", store.controls(), clock=clock)
    assert view["active"] is False


def test_a_corrupt_or_foreign_state_is_reported_never_raised_or_rewritten(tmp_path):
    state, store, _machine, clock = single_tab_account(tmp_path)
    path = rotation.rotation_path(state)
    path.write_text("{ not json")
    view = rotation_view.block(state, "home", store.controls(), clock=clock)
    assert view["error"] == "rotation-state-invalid" and view["active"] is True
    assert path.read_text() == "{ not json"
    with pytest.raises(RotationError):
        Rotation.read(path, "home")


def test_read_returns_none_without_a_file_and_never_settles(tmp_path):
    assert Rotation.read(tmp_path / "none.json", "home") is None
    state, _store, machine, clock = single_tab_account(tmp_path)
    clock.now += 99999
    path = rotation.rotation_path(state)
    before = path.read_bytes()
    again = Rotation.read(path, "home", clock=clock)
    assert again.next_due == machine.next_due and path.read_bytes() == before


# --- annotating the monitor snapshot ---------------------------------------------------------------


def test_queued_chats_without_a_tab_are_queued_not_missing(tmp_path):
    state, store, _machine, _clock = single_tab_account(tmp_path)
    record_observer_snapshot(state)
    snapshot = MonitorStore(state, "home").read()
    legacy = {s["chat"]: s for s in snapshot["sessions"]}
    assert legacy[CHATS[1]]["observation"] == "missing"  # the legacy verdict
    view = rotation_view.block(state, "home", store.controls())
    annotated = rotation_view.annotate(snapshot, view)
    rows = {s["chat"]: s for s in annotated["sessions"]}
    assert rows[CHATS[0]]["observation"] == "current"
    assert rows[CHATS[0]]["rotation_state"] == "current"
    for chat in CHATS[1:]:
        assert rows[chat]["observation"] == "queued"
        assert rows[chat]["rotation_state"] == "queued"
        assert rows[chat]["reason"] == "queued"
        assert rows[chat]["error_detail"] == "queued-awaiting-visit"
    counts = annotated["summary"]["observation_counts"]
    assert counts["queued"] == 2 and counts["current"] == 1 and counts["missing"] == 0
    # The two queued chats are not observed merely because they are in the registry.
    assert annotated["summary"]["coverage_complete"] is False
    assert annotated["rotation"]["queue_size"] == 3
    assert (
        snapshot["sessions"][1]["observation"] == "missing"
    )  # the input is never mutated


def test_a_current_chat_whose_tab_vanished_stays_a_real_missing_page(tmp_path):
    state, store, _machine, _clock = single_tab_account(tmp_path)
    record_observer_snapshot(
        state, current_chat=CHATS[1]
    )  # the observer sees another chat
    snapshot = MonitorStore(state, "home").read()
    view = rotation_view.block(state, "home", store.controls())
    rows = {s["chat"]: s for s in rotation_view.annotate(snapshot, view)["sessions"]}
    assert (
        rows[CHATS[0]]["observation"] == "missing"
    )  # current, yet no page: a real defect
    assert rows[CHATS[0]]["rotation_state"] == "current"
    assert rows[CHATS[2]]["observation"] == "queued"


def test_paused_chats_keep_their_pause_reason_and_are_not_reported_missing(tmp_path):
    state, store, _machine, _clock = single_tab_account(tmp_path)
    store.stop(CHATS[2], "pet project")
    record_observer_snapshot(state)
    snapshot = MonitorStore(state, "home").read()
    view = rotation_view.block(state, "home", store.controls())
    rows = {s["chat"]: s for s in rotation_view.annotate(snapshot, view)["sessions"]}
    assert rows[CHATS[2]]["rotation_state"] == "paused"
    assert rows[CHATS[2]]["reason"] == "pet project"
    assert rows[CHATS[2]]["observation"] == "queued"


def test_annotate_does_nothing_for_inactive_or_invalid_rotation(tmp_path):
    state, store, _machine, _clock = single_tab_account(tmp_path, marker=False)
    record_observer_snapshot(state)
    snapshot = MonitorStore(state, "home").read()
    view = rotation_view.block(state, "home", store.controls())
    result = rotation_view.annotate(snapshot, view)
    assert [s["observation"] for s in result["sessions"]] == [
        s["observation"] for s in snapshot["sessions"]
    ]
    assert rotation_view.annotate(snapshot, None) is snapshot


# --- entrypoints ---------------------------------------------------------------------------------


def test_status_json_gains_the_rotation_block_only_in_single_tab_mode(tmp_path, capsys):
    state, _store, _machine, _clock = single_tab_account(tmp_path)
    status = run_cli(capsys, state, "status")
    assert status["rotation"]["queue_size"] == 3
    assert status["rotation"]["last_go"]["state"] == "sent"
    assert "controls" in status and "attempts" in status  # the existing keys stay
    legacy = tmp_path / "legacy" / "state.sqlite"
    Store(legacy, account="home")
    assert "rotation" not in run_cli(capsys, legacy, "status")


def test_monitor_json_reports_queue_current_last_go_and_queued_states(tmp_path, capsys):
    state, _store, _machine, _clock = single_tab_account(tmp_path)
    record_observer_snapshot(state)
    out = run_cli(capsys, state, "monitor", "--json")
    assert out["rotation"]["current"] == CHATS[0]
    assert out["rotation"]["last_go"]["confirmation"] == "running"
    assert {s["rotation_state"] for s in out["sessions"]} == {"current", "queued"}
    assert out["summary"]["observation_counts"]["queued"] == 2


def test_monitor_json_without_rotation_is_exactly_the_legacy_payload(tmp_path, capsys):
    state = tmp_path / "home" / "state.sqlite"
    store = Store(state, account="home")
    store.enroll(CHATS[0])
    out = run_cli(capsys, state, "monitor", "--json")
    assert "rotation" not in out and out["stale"]


def test_queued_but_never_observed_chat_does_not_become_fresh_coverage():
    """Enrolled is not checked: queue membership must never turn false coverage true."""
    snapshot = {
        "connection": "connected", "stale": False,
        "summary": {"coverage_complete": False, "unobserved": 0},
        "sessions": [{
            "chat": CHATS[1], "watched": True, "observation": "missing",
            "last_checked_at": None, "session_stale": True,
        }],
    }
    view = {"active": True, "mode": "single-tab", "queue": [
        {"chat": CHATS[1], "current": False, "state": "queued"}
    ]}
    result = rotation_view.annotate(snapshot, view)
    assert result["sessions"][0]["observation"] == "queued"
    assert result["summary"]["coverage_complete"] is False


def test_a_genuinely_complete_observer_verdict_stays_complete():
    """The correction only removes the queue shortcut: a real full coverage is preserved."""
    snapshot = {
        "connection": "connected", "stale": False,
        "summary": {"coverage_complete": True, "unobserved": 0},
        "sessions": [{
            "chat": CHATS[0], "watched": True, "observation": "current",
            "last_checked_at": 1.0, "session_stale": False,
        }],
    }
    view = {"active": True, "mode": "single-tab", "queue": [
        {"chat": CHATS[0], "current": True, "state": "queued"}
    ]}
    assert rotation_view.annotate(snapshot, view)["summary"]["coverage_complete"] is True
