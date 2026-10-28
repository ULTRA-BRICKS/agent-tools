"""Read-only monitoring must never confuse dispatch with assistant progress."""

import asyncio
import json
import sqlite3

import pytest

CHAT = "https://chatgpt.com/c/one"
OTHER = "https://chatgpt.com/c/two"
NOW = 1792254000.0


def source(path, workspace="glide.vc"):
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE account_scope(account TEXT); CREATE TABLE controls(key TEXT, value TEXT); CREATE TABLE attempts(chat TEXT, turn TEXT, generation TEXT, wall REAL, state TEXT);"
        )
        db.execute("INSERT INTO account_scope VALUES (?)", (workspace,))
        db.execute("INSERT INTO controls VALUES (?,?)", ("watch:" + CHAT, "1"))
        db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?)",
            (CHAT, "failed", "failed", NOW - 100, "sent"),
        )


def sample(chat=CHAT, state="in_progress", go=True, **extra):
    return {
        "chat": chat,
        "title": "A task",
        "present": True,
        "checked_at": NOW,
        "status": "working" if state == "in_progress" else "complete",
        "running": state == "in_progress",
        "progress_token": "hash1",
        "turn": "response",
        "turns": [
            {
                "id": "failed",
                "user_go": False,
                "assistant_state": "cancelled",
                "assistant_present": False,
            },
            {
                "id": "response",
                "user_go": go,
                "assistant_state": state,
                "assistant_present": True,
            },
        ],
        **extra,
    }


def project(
    samples=None, attempts=None, previous=None, controls=None, now=NOW, **health
):
    from agenttools_chatgpt.monitor import project_snapshot

    return project_snapshot(
        "glide.vc",
        controls if controls is not None else {"watch:" + CHAT: "1"},
        attempts
        if attempts is not None
        else [
            {
                "chat": CHAT,
                "turn": "failed",
                "generation": "failed",
                "wall": NOW - 100,
                "state": "sent",
            }
        ],
        samples if samples is not None else [sample()],
        previous,
        {
            "connection": "connected",
            "heartbeat_at": NOW - 5,
            "page_count": 2,
            "max_pages": 6,
            **health,
        },
        now,
    )


def row(snap, chat=CHAT):
    return next(s for s in snap["sessions"] if s["chat"] == chat)


def test_read_only_account_and_missing_source(tmp_path):
    from agenttools_chatgpt.monitor import read_source

    p = tmp_path / "state.sqlite"
    source(p)
    before = p.read_bytes()
    modified = p.stat().st_mtime_ns
    controls, attempts = read_source(p, "glide.vc")
    assert controls["watch:" + CHAT] == "1" and attempts[0]["state"] == "sent"
    assert p.read_bytes() == before and p.stat().st_mtime_ns == modified
    with pytest.raises(ValueError):
        read_source(p, "home")
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.Error):
        read_source(missing, "glide.vc")
    assert not missing.exists()


def test_coverage_includes_unwatched_without_enrolling():
    controls = {"watch:" + CHAT: "1"}
    snap = project(samples=[sample(), sample(OTHER)], controls=controls)
    assert snap["summary"]["enrolled"] == 1 and snap["summary"]["available"] == 2
    assert snap["summary"]["unwatched"] == 1 and controls == {"watch:" + CHAT: "1"}


def test_missing_and_duplicate_are_explicit():
    snap = project(samples=[sample(OTHER), sample(OTHER)])
    rows = {s["chat"]: s for s in snap["sessions"]}
    assert rows[CHAT]["reason"] == "missing-page"
    assert rows[OTHER]["reason"] == "duplicate-pages"
    assert snap["summary"]["available"] == 1 and snap["summary"]["present"] == 0


def test_sent_is_not_proof():
    snap = project(samples=[sample(go=False)])
    assert snap["sessions"][0]["last_go_state"] == "sent"
    assert snap["sessions"][0]["continuation"] == "unconfirmed"
    assert snap["summary"]["resumed_24h"] == 0


def test_linked_running_completed_and_event_dedup():
    first = project()
    assert first["sessions"][0]["continuation"] == "running"
    repeated = project(previous=first, now=NOW + 60)
    assert not [e for e in repeated["events"] if e["kind"] == "continuation-running"]
    done = project(
        previous=repeated,
        samples=[sample(state="complete", checked_at=NOW + 120)],
        now=NOW + 120,
    )
    assert done["sessions"][0]["continuation"] == "completed"
    assert done["sessions"][0]["completion_observed_at"] == NOW + 120
    assert any(e["kind"] == "continuation-completed" for e in done["events"])


def test_unrelated_and_empty_assistant_cannot_confirm():
    for changes in (
        {"user_go": False},
        {"assistant_present": False},
        {"assistant_state": "unknown"},
    ):
        s = sample(state="complete")
        s["turns"][-1].update(changes)
        assert project(samples=[s])["sessions"][0]["continuation"] == "unconfirmed"


def test_uncertain_and_stop_are_never_cleared():
    attempts = [
        {
            "chat": CHAT,
            "turn": "failed",
            "generation": "failed",
            "wall": NOW - 100,
            "state": "uncertain",
        }
    ]
    snap = project(attempts=attempts, samples=[sample(state="complete")])
    assert snap["sessions"][0]["continuation"] == "uncertain"
    assert snap["summary"]["completed_24h"] == 0
    assert attempts[0]["state"] == "uncertain"
    stopped = project(controls={"watch:" + CHAT: "1", "stop:*": "manual stop"})
    assert stopped["sessions"][0]["reason"] == "manual stop"


def test_caches_backoff_stall_and_first_reappearance():
    first = project()
    cached = project(previous=first, now=NOW + 900)
    assert cached["sessions"][0]["last_checked_at"] == NOW
    assert not cached["sessions"][0]["no_visible_progress"]
    stalled = project(
        previous=first, now=NOW + 900, samples=[sample(checked_at=NOW + 900)]
    )
    assert stalled["sessions"][0]["no_visible_progress"]
    idle = project(samples=[sample(state="complete")])
    idle2 = project(
        previous=idle,
        now=NOW + 60,
        samples=[sample(state="complete", checked_at=NOW + 60)],
    )
    assert idle2["sessions"][0]["backoff_seconds"] == 120
    missing = project(samples=[])
    visible = project(
        previous=missing, now=NOW + 60, samples=[sample(checked_at=NOW + 60)]
    )
    assert visible["sessions"][0]["last_checked_at"] == NOW + 60


def test_restriction_clear_and_repeat_failure_events():
    first = project(controls={"watch:" + CHAT: "1", "stop:" + CHAT: "operator stop"})
    second = project(previous=first, now=NOW + 60)
    assert any(
        e["kind"] == "restriction-changed" and not e["reason"] for e in second["events"]
    )
    failure = sample(status="Thinking failed", running=False)
    first = project(samples=[failure])
    second = project(previous=first, samples=[failure], now=NOW + 60)
    assert not [e for e in second["events"] if e["kind"] == "thinking-failed"]


def test_durable_event_dedup_and_staleness(tmp_path):
    from agenttools_chatgpt.monitor import MonitorStore

    p = tmp_path / "state.sqlite"
    source(p)
    before = p.read_bytes()
    store = MonitorStore(p, "glide.vc")
    assert store.read(NOW)["stale"] and not store.path.exists()
    first = project()
    store.record(first)
    store.record(first)
    data = store.read(NOW)
    assert len(data["events"]) == len({e["id"] for e in first["events"]})
    assert p.read_bytes() == before
    stale = store.read(NOW + 400)
    assert stale["stale"] and stale["generated_at"] == NOW
    assert not stale["guardian_heartbeat_stale"]
    assert store.read(NOW + 700)["guardian_heartbeat_stale"]


def test_browser_unavailable_is_not_service_stopped():
    snap = project(samples=[], connection="unavailable", service={"active": "active"})
    assert snap["stale"] and snap["service"]["active"] == "active"
    assert snap["summary"]["available"] is None


def test_passive_collection_observes_all_and_respects_deferred_checks():
    from agenttools_chatgpt.monitor_observer import collect_samples

    class Page:
        def __init__(self, url):
            self.url = url
            self.calls = 0

        def is_closed(self):
            return False

        async def evaluate(self, script):
            self.calls += 1
            return sample(chat=self.url)

    pages = [Page(CHAT), Page(OTHER)]
    cache = {}
    rows = asyncio.run(collect_samples(pages, None, cache, NOW))
    assert {r["chat"] for r in rows} == {CHAT, OTHER}
    prior = project(samples=rows)
    rows2 = asyncio.run(collect_samples(pages, prior, cache, NOW + 10))
    assert all(p.calls == 1 for p in pages)
    assert all(r["checked_at"] == NOW for r in rows2)


def test_health_uses_actual_journal_timestamps():
    from agenttools_chatgpt.monitor_observer import parse_health

    journal = json.dumps(
        {
            "__REALTIME_TIMESTAMP": str(int((NOW - 15) * 1e6)),
            "MESSAGE": json.dumps({"health": "connected"}),
        }
    )
    health = parse_health("ActiveState=active\nMainPID=123\n", journal, NOW)
    assert health["heartbeat_at"] == NOW - 15 and health["service"]["pid"] == "123"
    assert parse_health("", "", NOW)["service"]["active"] == "unknown"


def test_dom_is_passive():
    from agenttools_chatgpt.monitor_observer import DOM_SCRIPT

    for forbidden in (
        "click(",
        "focus(",
        "fetch(",
        "scrollTo(",
        "execCommand(",
        "new_page(",
        ".goto(",
    ):
        assert forbidden not in DOM_SCRIPT


def test_monitor_cli_never_initializes_recovery_store(tmp_path, monkeypatch, capsys):
    from agenttools_chatgpt import cli

    p = tmp_path / "state.sqlite"
    source(p)
    before = p.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only monitor constructed Store")

    monkeypatch.setattr(cli, "Store", forbidden)
    assert (
        cli.main(["--workspace", "glide.vc", "--state", str(p), "monitor", "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["stale"]
    assert p.read_bytes() == before


def test_assistant_start_marker_confirms_start_before_final_text():
    s = sample()
    s["turns"][-1].update(
        assistant_present=False, assistant_started=True, generation_active=True
    )
    assert project(samples=[s])["sessions"][0]["continuation"] == "running"


def test_in_progress_go_turn_without_assistant_marker_is_only_running_metadata():
    s = sample()
    s["turns"][-1].update(
        assistant_present=False, assistant_started=False, generation_active=True
    )
    r = row(project(samples=[s]))
    assert r["running"] is True
    assert r["continuation"] == "unconfirmed"
    assert r["continuation_observed_at"] is None


def test_visible_rate_limit_is_an_event_once():
    first = project()
    s = sample(status="quota-or-rate-limit")
    limited = project(previous=first, samples=[s], now=NOW + 60)
    assert any(e["kind"] == "rate-limit-observed" for e in limited["events"])
    again = project(previous=limited, samples=[s], now=NOW + 120)
    assert not any(e["kind"] == "rate-limit-observed" for e in again["events"])


def test_error_detail_is_kept_next_to_the_coarse_reason():
    bad = {
        "chat": CHAT,
        "error": "conversation-root-unverified",
        "error_detail": "root-attribute-mismatch",
    }
    r = row(project(samples=[bad]))
    assert r["reason"] == "observation-error"
    assert r["error"] == "conversation-root-unverified"
    assert r["error_detail"] == "root-attribute-mismatch"


def test_error_detail_is_bounded_and_free_of_urls_and_ids():
    nasty = (
        "net::ERR_ABORTED at https://chatgpt.com/c/"
        + "a" * 36
        + "?token=secret\nsecond line with page text "
        + "x" * 500
    )
    detail = row(project(samples=[{"chat": CHAT, "error": "Error", "error_detail": nasty}]))[
        "error_detail"
    ]
    assert detail == "net::ERR_ABORTED at <url>"
    long = "word " * 100
    detail = row(project(samples=[{"chat": CHAT, "error": "Error", "error_detail": long}]))[
        "error_detail"
    ]
    assert 0 < len(detail) <= 120


def test_failed_observation_never_reports_the_previous_running_state():
    first = project()
    assert row(first)["running"] is True and row(first)["status"] == "working"
    failed = project(
        previous=first,
        now=NOW + 60,
        samples=[{"chat": CHAT, "error": "TimeoutError", "error_detail": "timeout"}],
    )
    r = row(failed)
    assert r["running"] is None and r["status"] == "unknown"
    assert r["observation"] == "error" and r["error_detail"] == "timeout"
    # The last good observation stays available, labelled as history.
    assert r["last_observed_running"] is True
    assert r["last_observed_status"] == "working"
    assert r["last_checked_at"] == NOW
    assert failed["summary"]["running"] == 0
    assert failed["summary"]["running_unknown"] == 1
    # Started evidence is a past fact, not a current continuation.
    assert r["continuation"] == "unconfirmed"
    assert r["continuation_observed_at"] == NOW


@pytest.mark.parametrize(
    "kwargs,observation,detail",
    [
        ({"samples": []}, "missing", "no-page-for-url"),
        (
            {"samples": [], "connection": "unavailable", "error_detail": "cdp-unreachable"},
            "unavailable",
            "cdp-unreachable",
        ),
        ({"samples": [sample(), sample()]}, "duplicate", "multiple-pages-for-url"),
    ],
)
def test_unobserved_session_is_unknown_not_running(kwargs, observation, detail):
    first = project()
    r = row(project(previous=first, now=NOW + 60, **kwargs))
    assert r["running"] is None and r["status"] == "unknown"
    assert r["observation"] == observation and r["error_detail"] == detail


def test_cached_observation_beyond_freshness_is_stale_per_session():
    first = project()
    within = row(project(previous=first, now=NOW + 60))
    assert within["observation"] == "current" and within["running"] is True
    assert within["check_age_seconds"] == 60 and within["check_stale"] is False
    late = project(previous=first, now=NOW + 400)
    r = row(late)
    # Account snapshot is fresh; only this session's own evidence is old.
    assert late["snapshot_age_seconds"] == 0 and late["stale"] is False
    assert r["observation"] == "stale" and r["check_stale"] is True
    assert r["running"] is None and r["last_observed_running"] is True
    assert r["last_checked_at"] == NOW and r["check_age_seconds"] == 400


def test_session_stale_flags_every_non_current_observation_with_fresh_snapshot():
    first = project()
    current = project(previous=first, now=NOW + 60)
    assert current["stale"] is False and row(current)["session_stale"] is False
    cases = [
        {"samples": [], "connection": "unavailable", "error_detail": "cdp-unreachable"},
        {"samples": [sample(), sample()]},
    ]
    for kwargs in cases:
        snap = project(previous=first, now=NOW + 60, **kwargs)
        assert row(snap)["session_stale"] is True
    late = project(previous=first, now=NOW + 400)
    assert late["stale"] is False and row(late)["session_stale"] is True


def test_session_missing_is_session_stale():
    from agenttools_chatgpt.monitor import _session_view

    r = _session_view({}, NOW, False)
    assert r["observation"] == "missing" and r["session_stale"] is True


def test_package_data_lists_every_bundled_js():
    import tomllib
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "lib" / "agenttools_chatgpt"
    data = tomllib.loads((pkg / "pyproject.toml").read_text())
    listed = data["tool"]["setuptools"]["package-data"]["agenttools_chatgpt"]
    assert {p.name for p in pkg.glob("*.js")} <= set(listed)


def test_writer_lock_acquires_and_excludes_second_owner(tmp_path):
    from agenttools_chatgpt.monitor_observer import WriterLock

    with (
        WriterLock(tmp_path / "w.lock"),
        pytest.raises(RuntimeError),
        WriterLock(tmp_path / "w.lock"),
    ):
        pass


def test_session_freshness_is_recomputed_when_telemetry_is_read_later(tmp_path):
    from agenttools_chatgpt.monitor import MonitorStore

    p = tmp_path / "state.sqlite"
    source(p)
    store = MonitorStore(p, "glide.vc")
    store.record(project())
    soon = store.read(NOW + 30)
    assert row(soon)["running"] is True and soon["summary"]["running"] == 1
    later = store.read(NOW + 400)
    r = row(later)
    assert later["stale"] is True
    assert r["running"] is None and r["observation"] == "stale"
    assert later["summary"]["running"] == 0
    # Reading never rewrites the stored evidence.
    assert row(store.read(NOW + 30))["running"] is True


def test_next_check_for_unobserved_sessions_is_a_future_retry():
    first = project(samples=[sample(state="complete")])
    assert row(first)["next_check_at"] == NOW + 60
    bad = {"chat": CHAT, "error": "TimeoutError", "error_detail": "timeout"}
    errored = project(previous=first, now=NOW + 500, samples=[bad], interval_seconds=45)
    assert row(errored)["next_check_at"] == NOW + 545
    # A past deadline is never preserved across failed cycles.
    again = project(previous=errored, now=NOW + 700, samples=[bad])
    assert row(again)["next_check_at"] == NOW + 760
    missing = project(previous=first, now=NOW + 500, samples=[])
    assert row(missing)["next_check_at"] == NOW + 560


def test_successful_observation_clears_error_fields_and_reschedules():
    bad = {"chat": CHAT, "error": "TimeoutError", "error_detail": "timeout"}
    failed = project(samples=[bad])
    assert row(failed)["error"] == "TimeoutError"
    ok = project(
        previous=failed, now=NOW + 60, samples=[sample(checked_at=NOW + 60)]
    )
    r = row(ok)
    assert r["error"] == "" and r["error_detail"] == "" and r["reason"] == ""
    assert r["observation"] == "current" and r["running"] is True
    assert r["next_check_at"] == NOW + 120
    assert r["last_error_at"] == NOW  # history of the earlier failure


def test_effective_recovery_controls_are_exposed_read_only():
    controls = {
        "watch:" + CHAT: "1",
        "stop:" + CHAT: "human draft",
        "astra_quota": "blocked",
    }
    snap = project(controls=controls, attempts=[])
    assert row(snap)["recovery"] == {
        "enrolled": True,
        "enabled": False,
        "blockers": ["hold", "quota"],
        "hold": "human draft",
        "hold_scope": "chat",
        "draft": True,
        "challenge": False,
        "quota_latched": True,
        "unresolved_send": False,
    }
    assert snap["effective_controls"] == {"global_stop": "", "quota_latched": True}
    glob = project(
        controls={"watch:" + CHAT: "1", "stop:*": "human challenge"}, attempts=[]
    )
    rec = row(glob)["recovery"]
    assert rec["hold_scope"] == "global" and rec["challenge"] is True
    assert glob["effective_controls"]["global_stop"] == "human challenge"
    uncertain = [
        {"chat": CHAT, "turn": "t", "generation": "t", "wall": NOW - 9, "state": "uncertain"}
    ]
    rec = row(project(attempts=uncertain))["recovery"]
    assert rec["unresolved_send"] is True and rec["blockers"] == ["unresolved-send"]
    ready = row(project(attempts=[]))["recovery"]
    assert ready["enabled"] is True and ready["blockers"] == []
    unwatched = row(project(controls={}, samples=[sample()], attempts=[]))["recovery"]
    assert unwatched["enrolled"] is False and unwatched["enabled"] is False


def test_completed_evidence_stays_historical_after_turn_leaves_window():
    done = project(samples=[sample(state="complete")])
    assert row(done)["continuation"] == "completed"
    assert row(done)["continuation_current"] is True
    other = [
        {
            "id": "elsewhere",
            "user_go": False,
            "assistant_state": "complete",
            "assistant_present": True,
        }
    ]
    gone = project(
        previous=done,
        now=NOW + 60,
        samples=[sample(state="complete", checked_at=NOW + 60, turns=other)],
    )
    r = row(gone)
    assert r["continuation"] == "completed" and r["continuation_current"] is False
    assert r["completion_observed_at"] == NOW and r["continuation_turn"] == "response"
    assert not [e for e in gone["events"] if e["kind"].startswith("continuation")]


def test_started_evidence_is_not_continued_after_the_turn_disappears():
    first = project()
    other = [
        {
            "id": "elsewhere",
            "user_go": False,
            "assistant_state": "complete",
            "assistant_present": True,
        }
    ]
    gone = project(
        previous=first,
        now=NOW + 60,
        samples=[sample(state="complete", checked_at=NOW + 60, turns=other)],
    )
    r = row(gone)
    assert r["continuation"] == "unconfirmed" and r["continuation_current"] is False
    assert r["continuation_observed_at"] == NOW
    assert gone["summary"]["resumed_24h"] == 1  # evidence existed, recorded as history


def test_turns_from_another_conversation_are_rejected():
    r = row(project(samples=[sample(conversation_id="two")]))
    assert r["observation"] == "error" and r["error_detail"] == "conversation-id-mismatch"
    assert r["continuation"] == "unconfirmed" and r["running"] is None
    assert row(project(samples=[sample(conversation_id="one")]))["continuation"] == "running"


def test_collection_errors_keep_a_sanitized_detail():
    from agenttools_chatgpt.monitor_observer import collect_samples
    from playwright.async_api import Error as BrowserError

    class Broken:
        def __init__(self, url, exc):
            self.url, self.exc = url, exc

        def is_closed(self):
            return False

        async def evaluate(self, script):
            raise self.exc

    pages = [
        Broken(CHAT, TimeoutError()),
        Broken(
            OTHER,
            BrowserError(
                "Execution context was destroyed at https://chatgpt.com/c/two?t=secret\n  at stack"
            ),
        ),
    ]
    rows = {r["chat"]: r for r in asyncio.run(collect_samples(pages, None, {}, NOW))}
    assert rows[CHAT]["error_detail"] == "page-evaluate-timeout"
    assert rows[OTHER]["error"] == "Error"
    assert rows[OTHER]["error_detail"] == "Execution context was destroyed at <url>"


def test_unreachable_cdp_is_reported_with_detail_and_future_retry(tmp_path, monkeypatch):
    from agenttools_chatgpt import monitor_observer

    pytest.importorskip("playwright.async_api")
    p = tmp_path / "state.sqlite"
    source(p)
    monkeypatch.setattr(
        monitor_observer,
        "service_health",
        lambda workspace, now: {"service": {"active": "active"}, "service_checked_at": now},
    )
    snap = asyncio.run(
        monitor_observer.observe(p, "glide.vc", "http://127.0.0.1:1", once=True)
    )
    assert snap["connection"] == "unavailable"
    assert snap["error_detail"].startswith("cdp-unreachable")
    r = row(snap)
    assert r["running"] is None and r["observation"] == "unavailable"
    assert r["error_detail"].startswith("cdp-unreachable")
    assert r["next_check_at"] > snap["generated_at"]
    assert "http" not in snap["error_detail"]


def _generating(**turn):
    """A complete-looking response whose page still shows ongoing generation."""
    s = sample(state="complete", running=True, stop_visible=True, status="working")
    s["turns"][-1].update(turn)
    return s


def _kinds(snap):
    return [e["kind"] for e in snap["events"]]


def test_transient_complete_with_stop_visible_is_running_not_completed():
    snap = project(samples=[_generating()])
    r = row(snap)
    assert r["continuation"] == "running" and r["continuation_current"] is True
    assert r["completion_observed_at"] is None
    assert snap["summary"]["completed_24h"] == 0
    assert "continuation-completed" not in _kinds(snap)
    assert "continuation-running" in _kinds(snap)


@pytest.mark.parametrize(
    "extra",
    [
        {"running": True, "stop_visible": False},
        {"running": False, "stop_visible": True},
    ],
)
def test_page_generation_signals_each_block_completion_of_the_latest_turn(extra):
    s = sample(state="complete", **extra)
    r = row(project(samples=[s]))
    assert r["continuation"] == "running" and r["completion_observed_at"] is None


def test_response_generation_active_blocks_completion_without_page_signals():
    s = sample(state="complete")
    s["turns"][-1]["generation_active"] = True
    r = row(project(samples=[s]))
    assert r["continuation"] == "running" and r["completion_observed_at"] is None


def test_complete_with_stop_but_no_assistant_evidence_stays_unconfirmed():
    s = _generating(assistant_present=False, assistant_started=False)
    r = row(project(samples=[s]))
    assert r["continuation"] == "unconfirmed"
    assert r["continuation_observed_at"] is None and r["completion_observed_at"] is None


def test_later_in_progress_contradicts_and_retracts_a_false_completion_once():
    done = project(samples=[sample(state="complete")])
    assert row(done)["completion_observed_at"] == NOW
    live = sample(checked_at=NOW + 60)
    live["turns"][-1]["generation_active"] = True
    again = project(previous=done, samples=[live], now=NOW + 60)
    r = row(again)
    assert r["continuation"] == "running" and r["continuation_current"] is True
    assert r["completion_observed_at"] is None
    assert r["completion_invalidated_at"] == NOW + 60
    assert r["completion_invalidated_observed_at"] == NOW
    assert again["summary"]["completed_24h"] == 0
    assert again["summary"]["resumed_24h"] == 1
    assert _kinds(again).count("completion-invalidated") == 1
    live2 = dict(live, checked_at=NOW + 120)
    repeat = project(previous=again, samples=[live2], now=NOW + 120)
    assert "completion-invalidated" not in _kinds(repeat)
    assert row(repeat)["completion_invalidated_at"] == NOW + 60


def test_incomplete_go_echo_after_completion_is_unconfirmed_and_retracts():
    done = project(samples=[sample(state="complete")])
    echo = sample(checked_at=NOW + 60)
    echo["turns"][-1].update(
        assistant_present=False, assistant_started=False, generation_active=True
    )
    r = row(project(previous=done, samples=[echo], now=NOW + 60))
    assert r["continuation"] == "unconfirmed" and r["continuation_current"] is False
    assert r["completion_observed_at"] is None
    assert r["completion_invalidated_at"] == NOW + 60


def test_new_unrelated_turn_keeps_the_past_completion():
    done = project(samples=[sample(state="complete")])
    s = sample(state="complete", running=True, stop_visible=True, checked_at=NOW + 60)
    s["turns"].append(
        {
            "id": "next",
            "user_go": False,
            "assistant_state": "in_progress",
            "assistant_present": True,
            "generation_active": True,
        }
    )
    snap = project(previous=done, samples=[s], now=NOW + 60)
    r = row(snap)
    assert r["continuation"] == "completed" and r["continuation_turn"] == "response"
    assert r["completion_observed_at"] == NOW
    assert "completion-invalidated" not in _kinds(snap)
    assert snap["summary"]["completed_24h"] == 1
    fresh = project(samples=[s])
    assert row(fresh)["continuation"] == "completed"


def test_stored_false_completion_is_corrected_without_deleting_history(tmp_path):
    from agenttools_chatgpt.monitor import MonitorStore

    p = tmp_path / "state.sqlite"
    source(p)
    store = MonitorStore(p, "glide.vc")
    done = project(samples=[sample(state="complete")])
    # Telemetry written by 0.2.1 lacks the retraction fields.
    for key in ("completion_invalidated_at", "completion_invalidated_observed_at"):
        row(done).pop(key, None)
    store.record(done)
    assert store.read(NOW)["summary"]["completed_24h"] == 1
    live = sample(checked_at=NOW + 60)
    live["turns"][-1]["generation_active"] = True
    fixed = project(previous=done, samples=[live], now=NOW + 60)
    store.record(fixed)
    store.record(fixed)
    data = store.read(NOW + 60)
    assert data["summary"]["completed_24h"] == 0
    assert data["summary"]["resumed_24h"] == 1
    kinds = [e["kind"] for e in data["events"]]
    assert kinds.count("continuation-completed") == 1
    assert kinds.count("completion-invalidated") == 1


def test_real_completion_after_retraction_is_recorded_once(tmp_path):
    from agenttools_chatgpt.monitor import MonitorStore

    p = tmp_path / "state.sqlite"
    source(p)
    store = MonitorStore(p, "glide.vc")
    first = project(samples=[sample(state="complete")])
    store.record(first)
    live = sample(checked_at=NOW + 60)
    live["turns"][-1]["generation_active"] = True
    retracted = project(previous=first, samples=[live], now=NOW + 60)
    store.record(retracted)
    assert store.read(NOW + 60)["summary"]["completed_24h"] == 0
    real = project(
        previous=retracted,
        samples=[sample(state="complete", checked_at=NOW + 120)],
        now=NOW + 120,
    )
    assert row(real)["completion_observed_at"] == NOW + 120
    assert _kinds(real).count("continuation-completed") == 1
    store.record(real)
    store.record(real)
    assert store.read(NOW + 120)["summary"]["completed_24h"] == 1
    settled = project(
        previous=real,
        samples=[sample(state="complete", checked_at=NOW + 180)],
        now=NOW + 180,
    )
    assert not [k for k in _kinds(settled) if k.startswith("continuation")]
    store.record(settled)
    data = store.read(NOW + 180)
    assert data["summary"]["completed_24h"] == 1
    assert [e["kind"] for e in data["events"]].count("continuation-completed") == 2
    # Retracting the second completion again is a distinct, countable event.
    live3 = sample(checked_at=NOW + 240)
    live3["turns"][-1]["generation_active"] = True
    again = project(previous=settled, samples=[live3], now=NOW + 240)
    store.record(again)
    assert store.read(NOW + 240)["summary"]["completed_24h"] == 0


def test_stale_or_missing_samples_do_not_erase_or_invent_completion():
    done = project(samples=[sample(state="complete")])
    stale = project(previous=done, samples=[_generating()], now=NOW + 3600)
    r = row(stale)
    assert r["observation"] == "stale"
    assert r["continuation"] == "completed" and r["continuation_current"] is False
    assert r["completion_observed_at"] == NOW
    assert "completion-invalidated" not in _kinds(stale)
    missing = project(previous=done, samples=[], now=NOW + 60)
    assert row(missing)["completion_observed_at"] == NOW


def _waiting():
    return sample(status="awaiting-approval", running=False, approval_wait=True)


def test_a_pending_platform_approval_is_not_counted_as_running_or_completed():
    snap = project(samples=[_waiting()])
    r = row(snap)
    assert r["running"] is False and r["status"] == "awaiting-approval"
    assert r["completion_observed_at"] is None and r["continuation"] != "completed"
    assert snap["summary"]["running"] == 0 and snap["summary"]["completed_24h"] == 0


def test_a_user_only_turn_beside_a_pending_approval_is_not_resumed():
    s = _waiting()
    s["turns"][-1].update(assistant_present=False, assistant_started=False)
    r = row(project(samples=[s]))
    assert r["continuation_observed_at"] is None and r["running"] is False


def test_the_waiting_row_survives_storage_and_later_freshness(tmp_path):
    from agenttools_chatgpt.monitor import MonitorStore

    p = tmp_path / "state.sqlite"
    source(p)
    store = MonitorStore(p, "glide.vc")
    store.record(project(samples=[_waiting()]))
    soon = store.read(NOW + 30)
    assert row(soon)["status"] == "awaiting-approval" and soon["summary"]["running"] == 0
    later = row(store.read(NOW + 400))
    assert later["observation"] == "stale" and later["running"] is None
