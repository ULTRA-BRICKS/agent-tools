"""Passive evidence is separated from mutation admission (Refs #1020).

Fake CDP pages only; nothing here touches Chrome, an account or the network. The observer
must keep reporting the real state of the conversation tabs it can read while the sender's
resource policy (conversation budget, auxiliary budget, memory floor) decides separately
whether any mutation is admitted.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import live
from agenttools_chatgpt.core import Store
from agenttools_chatgpt.monitor import project_snapshot

NOW = 1792254000.0
SPACE = "https://chatgpt.com/space?tab=all"
EXTERNAL = "https://example.com/"
UUID = "12345678-1234-1234-1234-123456789abc"
PLAIN = "https://chatgpt.com/c/" + UUID
PROJECT = "https://chatgpt.com/g/g-p-" + "a" * 32 + "-demo/c/" + UUID
GIB = 1024**3


def conv(i):
    return f"https://chatgpt.com/c/conv-{i}"


class Tab:
    def __init__(self, url, running=False):
        self.url = url
        self.running = running
        self.reads = 0

    def is_closed(self):
        return False

    async def evaluate(self, script):
        self.reads += 1
        return {
            "title": "Task",
            "status": "working" if self.running else "complete",
            "running": self.running,
            "progress_token": "t",
            "turn": "response",
            "turns": [],
        }


def write_source(path, chats):
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE account_scope(account TEXT);"
            "CREATE TABLE controls(key TEXT, value TEXT);"
            "CREATE TABLE attempts(chat TEXT, turn TEXT, generation TEXT, wall REAL, state TEXT);"
        )
        db.execute("INSERT INTO account_scope VALUES ('glide.vc')")
        db.executemany(
            "INSERT INTO controls VALUES (?, '1')", [("watch:" + c,) for c in chats]
        )


def run_observer(
    tmp_path, monkeypatch, urls, watched, memory=3 * GIB, running=(), **limits
):
    """One observer cycle against fake tabs; returns (snapshot, tabs)."""
    pytest.importorskip("playwright.async_api")
    from agenttools_chatgpt import monitor_observer

    tabs = [Tab(u, running=u in running) for u in urls]
    context = SimpleNamespace(pages=tabs)
    browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
    driver = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
    )
    monkeypatch.setattr(
        monitor_observer,
        "service_health",
        lambda workspace, now: {
            "service": {"active": "active"},
            "service_checked_at": now,
        },
    )
    monkeypatch.setattr(live, "available_memory", lambda: memory)
    source = tmp_path / "state.sqlite"
    write_source(source, watched)
    snap = asyncio.run(
        monitor_observer.observe(
            source, "glide.vc", "http://127.0.0.1:9", once=True, **limits
        )
    )
    return snap, tabs


def rows(snap):
    return {s["chat"]: s for s in snap["sessions"]}


# --- (a) six conversations + one /space tab -----------------------------------------


def test_six_conversations_plus_space_tab_report_real_states(tmp_path, monkeypatch):
    chats = [conv(i) for i in range(6)]
    snap, tabs = run_observer(
        tmp_path, monkeypatch, chats + [SPACE], chats, running={conv(0), conv(3)}
    )
    by_chat = rows(snap)
    assert {c: by_chat[c]["observation"] for c in chats} == dict.fromkeys(
        chats, "current"
    )
    assert [by_chat[c]["running"] for c in chats] == [
        True,
        False,
        False,
        True,
        False,
        False,
    ]
    assert by_chat[conv(0)]["status"] == "working"
    assert snap["conversation_page_count"] == 6 and snap["auxiliary_page_count"] == 1
    assert snap["resource_paused"] == ""
    assert snap["summary"]["coverage_complete"] is True
    assert tabs[-1].reads == 0  # the Space tab is never a DOM-read target


def test_mutation_is_admitted_with_six_conversations_and_one_space_tab(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(live, "available_memory", lambda: 3 * GIB)
    chat = conv(0)
    store = Store(tmp_path / "db", account="glide.vc")
    store.enroll(chat)
    tabs = [Tab(conv(i)) for i in range(6)] + [Tab(SPACE)]
    context = SimpleNamespace(pages=tabs)
    assert admitted(store, context, tabs[0], chat)


def admitted(store, context, page, chat, *, max_pages=6):
    import time

    from agenttools_chatgpt.core import Guardian

    return live.mutation_window_open(
        Guardian(store, apply=True),
        page,
        chat,
        context,
        SimpleNamespace(reason=""),
        asyncio.Event(),
        url=page.url,
        page_count=len(context.pages),
        started=time.monotonic(),
        limit=35,
        max_pages=max_pages,
        min_available_mib=2048,
    )


# --- (b)/(g) seven conversations ----------------------------------------------------


def test_seven_conversations_pause_mutation_keep_evidence_and_are_not_complete(
    tmp_path, monkeypatch
):
    chats = [conv(i) for i in range(7)]
    snap, tabs = run_observer(tmp_path, monkeypatch, chats, chats, running={conv(1)})
    assert snap["resource_paused"] == "conversation-budget"
    assert snap["conversation_page_count"] == 7 and snap["auxiliary_page_count"] == 0
    observed = [r for r in snap["sessions"] if r["observation"] == "current"]
    assert len(observed) == 6
    assert rows(snap)[conv(1)]["running"] is True
    beyond = [r for r in snap["sessions"] if r["observation"] == "unobserved"]
    assert len(beyond) == 1 and beyond[0]["error_detail"] == "beyond-passive-cap"
    assert beyond[0]["running"] is None and beyond[0]["status"] == "unknown"
    assert snap["summary"]["coverage_complete"] is False
    assert snap["summary"]["unobserved"] == 1
    assert sum(t.reads for t in tabs) == 6
    assert any("conversation-budget" in text for text in snap["limitations"])


def test_seven_conversations_refuse_every_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "available_memory", lambda: 3 * GIB)
    store = Store(tmp_path / "db", account="glide.vc")
    store.enroll(conv(0))
    tabs = [Tab(conv(i)) for i in range(7)]
    context = SimpleNamespace(pages=tabs)
    assert not admitted(store, context, tabs[0], conv(0))


def test_unwatched_pages_are_dropped_before_watched_ones_beyond_the_cap(
    tmp_path, monkeypatch
):
    chats = [conv(i) for i in range(7)]
    # The last tab is the only enrolled one: it must be observed ahead of earlier unwatched tabs.
    snap, _ = run_observer(tmp_path, monkeypatch, chats, [conv(6)])
    by_chat = rows(snap)
    assert by_chat[conv(6)]["observation"] == "current"
    assert snap["summary"]["unobserved"] == 1
    assert snap["summary"]["coverage_complete"] is False


# --- (c) auxiliary budget -----------------------------------------------------------


def test_external_pages_count_as_auxiliary_and_pause_mutation_not_evidence(
    tmp_path, monkeypatch
):
    chats = [conv(0), conv(1)]
    urls = chats + [SPACE, EXTERNAL, "about:blank"]
    snap, tabs = run_observer(tmp_path, monkeypatch, urls, chats)
    assert snap["auxiliary_page_count"] == 3 and snap["conversation_page_count"] == 2
    assert snap["resource_paused"] == "auxiliary-budget"
    assert {rows(snap)[c]["observation"] for c in chats} == {"current"}
    assert snap["summary"]["coverage_complete"] is True
    assert tabs[3].reads == 0 and tabs[2].reads == 0 and tabs[4].reads == 0


@pytest.mark.parametrize(
    "aux, admitted_expected",
    [
        (["about:blank"], True),
        ([SPACE, EXTERNAL], True),
        ([SPACE, EXTERNAL, "about:blank"], False),
    ],
)
def test_auxiliary_budget_bounds_mutation(
    tmp_path, monkeypatch, aux, admitted_expected
):
    monkeypatch.setattr(live, "available_memory", lambda: 3 * GIB)
    store = Store(tmp_path / "db", account="glide.vc")
    store.enroll(conv(0))
    tabs = [Tab(conv(0))] + [Tab(u) for u in aux]
    assert (
        admitted(store, SimpleNamespace(pages=tabs), tabs[0], conv(0))
        is admitted_expected
    )


# --- (d) duplicate conversation URL -------------------------------------------------


@pytest.mark.parametrize(
    "variant", [PLAIN, PLAIN + "/", PLAIN + "?model=x", PLAIN + "#top", PROJECT]
)
def test_duplicate_conversation_tab_vetoes_mutation_and_counts_physically(
    tmp_path, monkeypatch, variant
):
    monkeypatch.setattr(live, "available_memory", lambda: 3 * GIB)
    store = Store(tmp_path / "db", account="glide.vc")
    store.enroll(PLAIN)
    first, second = Tab(PLAIN), Tab(variant)
    context = SimpleNamespace(pages=[first, second])
    assert not admitted(store, context, first, PLAIN)
    assert not admitted(store, context, second, PLAIN)
    assert live.page_counts([first.url, second.url]) == (2, 0)


def test_duplicate_conversation_is_reported_as_duplicate_by_the_observer(
    tmp_path, monkeypatch
):
    snap, _ = run_observer(tmp_path, monkeypatch, [PLAIN, PROJECT], [PLAIN])
    assert rows(snap)[PLAIN]["observation"] == "duplicate"
    assert snap["conversation_page_count"] == 2
    assert snap["summary"]["coverage_complete"] is False


def test_duplicate_tab_takes_no_passive_read_slot_and_is_not_read_twice(
    tmp_path, monkeypatch
):
    chats = [conv(i) for i in range(6)]
    snap, tabs = run_observer(tmp_path, monkeypatch, [chats[0]] + chats, chats)
    assert snap["summary"]["unobserved"] == 0
    by_chat = rows(snap)
    assert by_chat[chats[0]]["observation"] == "duplicate"
    assert {by_chat[c]["observation"] for c in chats[1:]} == {"current"}
    assert [t.reads for t in tabs] == [1, 0, 1, 1, 1, 1, 1]


def test_unread_conversation_with_a_duplicate_tab_is_unobserved_not_complete(
    tmp_path, monkeypatch
):
    chats = [conv(i) for i in range(7)]
    # Only conv(0) is enrolled; conv(6) is beyond the cap and open twice.
    snap, tabs = run_observer(tmp_path, monkeypatch, chats + [conv(6)], [conv(0)])
    assert rows(snap)[conv(6)]["observation"] == "unobserved"
    assert snap["summary"]["unobserved"] == 1
    assert snap["summary"]["coverage_complete"] is False
    assert tabs[6].reads == 0 and tabs[7].reads == 0
    assert any("1 conversation(s) beyond" in text for text in snap["limitations"])


@pytest.mark.parametrize(
    "limits, reason",
    [
        ({"max_pages": 3}, "conversation-budget"),
        ({"min_available_mib": 4096}, "low-memory"),
    ],
)
def test_observer_label_uses_its_configured_limits(
    tmp_path, monkeypatch, limits, reason
):
    chats = [conv(i) for i in range(4)]
    snap, _ = run_observer(tmp_path, monkeypatch, chats, chats, **limits)
    assert snap["resource_paused"] == reason
    assert {snap[key] for key in limits} == set(limits.values())
    assert {r["observation"] for r in snap["sessions"]} == {"current"}


# --- (e) identity -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        PLAIN,
        PLAIN + "/",
        PLAIN + "?x=1",
        PLAIN + "#a",
        PLAIN + "/?x=1#a",
        PROJECT,
        PROJECT + "/",
    ],
)
def test_conversation_url_variants_share_one_identity(url):
    tab = Tab(url)
    context = SimpleNamespace(pages=[tab])
    assert live.enrolled_pages(context, {PLAIN}) == {PLAIN: [tab]}


@pytest.mark.parametrize(
    "url",
    [
        SPACE,
        "https://chatgpt.com/",
        "about:blank",
        EXTERNAL,
        "https://chatgpt.com/c/",
        "https://chatgpt.com/g/g-p-x/c/",
    ],
)
def test_non_conversation_urls_are_auxiliary(url):
    assert live.page_counts([url]) == (0, 1)
    assert live.enrolled_pages(SimpleNamespace(pages=[Tab(url)]), {PLAIN}) == {
        PLAIN: []
    }


def test_observer_sample_identity_is_the_plain_conversation_url():
    from agenttools_chatgpt.monitor_observer import collect_samples

    tabs = [Tab(PROJECT + "?x=1"), Tab(SPACE)]
    result = asyncio.run(collect_samples(tabs, None, {}, NOW))
    assert [r["chat"] for r in result] == [PLAIN]
    assert tabs[1].reads == 0


# --- (f) memory ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "memory, reason", [(1 * GIB, "low-memory"), (None, "memory-unavailable")]
)
def test_memory_pause_blocks_mutation_but_labeled_evidence_continues(
    tmp_path, monkeypatch, memory, reason
):
    chats = [conv(0), conv(1)]
    snap, tabs = run_observer(tmp_path, monkeypatch, chats, chats, memory=memory)
    assert snap["resource_paused"] == reason
    assert {r["observation"] for r in snap["sessions"]} == {"current"}
    assert all(t.reads == 1 for t in tabs)
    assert any(reason in text for text in snap["limitations"])
    store = Store(tmp_path / "db", account="glide.vc")
    store.enroll(conv(0))
    assert not admitted(store, SimpleNamespace(pages=tabs), tabs[0], conv(0))


@pytest.mark.parametrize(
    "conversations, auxiliary, memory, expected",
    [
        (6, 2, 3 * GIB, ""),
        (7, 0, 3 * GIB, "conversation-budget"),
        (0, 3, 3 * GIB, "auxiliary-budget"),
        (1, 0, 2 * GIB - 1, "low-memory"),
        (1, 0, None, "memory-unavailable"),
        (7, 3, None, "conversation-budget"),
    ],
)
def test_resource_pause_reasons_are_specific(
    conversations, auxiliary, memory, expected
):
    assert live.resource_pause(conversations, auxiliary, memory) == expected


@pytest.mark.parametrize("limit", [0, 7, 40])
def test_conversation_budget_ceiling_is_never_raised_or_disabled(limit):
    assert live.resource_pause(1, 0, 3 * GIB, max_pages=limit) == "conversation-budget"


@pytest.mark.parametrize("limit", [-1, 0, 1])
def test_auxiliary_budget_can_only_be_lowered(limit):
    assert live.resource_pause(0, 2, 3 * GIB, max_auxiliary_pages=limit) == (
        "auxiliary-budget"
    )
    assert live.resource_pause(0, 3, 3 * GIB, max_auxiliary_pages=9) == (
        "auxiliary-budget"
    )


# --- (h) stale vs missing -----------------------------------------------------------


def snapshot(samples, previous=None, now=NOW):
    return project_snapshot(
        "glide.vc",
        {"watch:" + conv(i): "1" for i in range(2)},
        [],
        samples,
        previous,
        {"connection": "connected", "heartbeat_at": now, "page_count": len(samples)},
        now,
    )


def checked(chat, at=NOW):
    return {
        "chat": chat,
        "checked_at": at,
        "status": "complete",
        "running": False,
        "progress_token": "t",
        "turn": "r",
        "turns": [],
    }


def test_stale_and_missing_are_distinct_and_never_complete():
    first = snapshot([checked(conv(0)), checked(conv(1))])
    assert first["summary"]["coverage_complete"] is True
    assert first["summary"]["observation_counts"]["current"] == 2
    later = snapshot([], previous=first, now=NOW + 1000)
    # Both pages vanished: missing, not stale.
    counts = later["summary"]["observation_counts"]
    assert counts["missing"] == 2 and counts["stale"] == 0
    # One page present but its evidence is too old; the other is gone.
    mixed = snapshot([checked(conv(0))], previous=first, now=NOW + 1000)
    counts = mixed["summary"]["observation_counts"]
    assert counts["stale"] == 1 and counts["missing"] == 1
    assert counts["current"] == 0
    assert mixed["summary"]["coverage_complete"] is False
    assert later["summary"]["coverage_complete"] is False


def test_unavailable_browser_is_never_complete():
    snap = project_snapshot(
        "glide.vc",
        {"watch:" + conv(0): "1"},
        [],
        [],
        None,
        {"connection": "unavailable"},
        NOW,
    )
    assert snap["summary"]["coverage_complete"] is False
