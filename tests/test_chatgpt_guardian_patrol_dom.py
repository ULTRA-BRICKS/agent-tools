"""The single-tab patrol against real DOM fixtures in an isolated headless Chromium.

Routes serve sanitized ChatGPT-shaped pages for chatgpt.com URLs; nothing leaves the machine and
no account, profile or live state is touched. A capture-phase click log, installed in every
document, proves what the patrol actually clicked across navigations.
"""

import asyncio
import random
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import live, patrol, rotation
from agenttools_chatgpt.core import Guardian, Store
from agenttools_chatgpt.urls import conversation_identity
from guardian_patrol_fakes import FakeTransport
from test_chatgpt_guardian_live import HTML, MENU_HTML

WALL0 = 1_800_000_000.0
IDS = {
    "failed": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "working": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "complete": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    "idle": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
}
PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/"
URLS = {kind: PROJECT + ident for kind, ident in IDS.items()}
CHATS = {kind: "https://chatgpt.com/c/" + ident for kind, ident in IDS.items()}

CLICK_LOG = """document.addEventListener('click', e => {
  const el = e.target.closest('[aria-label],button');
  window.guardianLog(el ? (el.getAttribute('aria-label') || el.textContent.trim()) : '?');
}, true);"""
FAILED_TURN = 'data-talvt-turn-state="cancelled"'
RESPONSE = '<div data-talvt-turn-state="in_progress"></div><div data-markdown-copy>Working on it</div>'
GO_TURN = (
    '<div data-turn-key="22222222-2222-4222-8222-222222222222"><div data-chatgpt-search-unit-key="message:user" '
    'data-chatgpt-search-message-ids="22222222-2222-4222-8222-222222222222">'
    '<div data-user-message-bubble="true">го</div></div></div>'
)

assert GO_TURN in MENU_HTML and FAILED_TURN in HTML, "fixture markup drifted"

PAGES = {
    "failed": MENU_HTML.replace(
        GO_TURN, GO_TURN[: -len("</div>")] + RESPONSE + "</div>"
    ),
    "working": HTML.replace(FAILED_TURN, 'data-talvt-turn-state="in_progress"')
    .replace("<span>Thinking failed</span>", "<span>Thinking</span>")
    .replace("<script>", '<button aria-label="Stop">Stop</button><script>', 1),
    "complete": HTML.replace(FAILED_TURN, 'data-talvt-turn-state="complete"').replace(
        "<span>Thinking failed</span>", "<span>Done</span>"
    ),
}
PAGES["idle"] = PAGES["complete"]


class Clock:
    def __init__(self):
        self.now = WALL0

    def __call__(self):
        return self.now


def serve_page(route):
    ident = conversation_identity(route.request.url)
    kind = next((k for k, chat in CHATS.items() if chat == ident), None)
    if kind is None:
        return route.fulfill(status=404, body="")
    return route.fulfill(body=PAGES[kind], content_type="text/html")


def drive(case, tmp_path, monkeypatch, kinds):
    pytest.importorskip("playwright.async_api")
    if not shutil.which("google-chrome"):
        pytest.skip("Local fixture Chrome required")
    monkeypatch.setattr(live, "available_memory", lambda: 8 * 1024**3)

    async def scenario():
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, executable_path=shutil.which("google-chrome")
            )
            try:
                clicks = []
                context = await browser.new_context()
                await context.expose_binding(
                    "guardianLog", lambda source, label: clicks.append(label)
                )
                await context.add_init_script(CLICK_LOG)
                await context.route("https://chatgpt.com/**", serve_page)
                store = Store(tmp_path / "home" / "state.sqlite", account="home")
                for kind in kinds:
                    store.enroll(CHATS[kind])
                clock = Clock()
                machine = rotation.Rotation.open(
                    rotation.rotation_path(store.path),
                    "home",
                    clock=clock,
                    rng=random.Random(3),
                )
                settings = patrol.PatrolConfig(apply=True, confirm_wait=20.0)
                watcher = patrol.Patrol(
                    store=store,
                    guardian=Guardian(store, apply=True),
                    rotation=machine,
                    context=context,
                    transport=FakeTransport(),
                    io=patrol.PageIO(),
                    config=settings,
                    stop=asyncio.Event(),
                    watch_urls={CHATS[k]: URLS[k] for k in kinds},
                )
                await case(watcher, machine, clock, context, clicks, store)
            finally:
                await browser.close()

    asyncio.run(scenario())


async def visit(watcher, machine, clock):
    clock.now = max(clock.now, machine.due_at())
    return await watcher.tick()


def test_navigation_between_idle_chats_uses_one_tab_and_clicks_nothing(
    tmp_path, monkeypatch
):
    async def case(watcher, machine, clock, context, clicks, store):
        seen = []
        for _ in range(6):
            report = await visit(watcher, machine, clock)
            seen.append(report["visit"]["chat"])
            assert len(context.pages) == 1  # one owned tab for the whole run
            assert (
                conversation_identity(context.pages[0].url) == report["visit"]["chat"]
            )
            assert context.pages[0].url.startswith(PROJECT)  # the permanent project URL
        assert set(seen) == {CHATS["idle"], CHATS["complete"]}
        outcomes = {e.chat: e.last_outcome for e in machine.queue}
        assert outcomes[CHATS["idle"]] == outcomes[CHATS["complete"]] == "complete"
        # Not one click of any kind: no Stop, no Send, no model menu, no navigation control.
        assert clicks == []
        assert store.status()["attempts"] == []

    drive(case, tmp_path, monkeypatch, ["idle", "complete"])


FINISH = """() => {
  document.querySelector('[aria-label="Stop"]').remove();
  document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state', 'complete');
}"""


def test_a_real_running_response_is_never_navigated_away_from_until_it_finishes(
    tmp_path, monkeypatch
):
    """Offline fixture only: navigating away from an active response has no permitted live proof."""

    async def case(watcher, machine, clock, context, clicks, store):
        first = await visit(watcher, machine, clock)
        assert first["visit"] == {"chat": CHATS["working"], "outcome": "working"}
        page = context.pages[0]
        for _ in range(3):  # the response keeps running through three more slots
            report = await visit(watcher, machine, clock)
            assert (
                report["health"] == "protected-tab" and report["blocked"] == "running"
            )
            assert conversation_identity(page.url) == CHATS["working"]  # not navigated
            assert len(context.pages) == 1
        assert machine.deferred["reason"] == "running-protected"
        assert clicks == [] and store.status()["attempts"] == []  # no Stop, no Go
        await page.evaluate(FINISH)  # the response ends
        report = await visit(watcher, machine, clock)
        assert report["visit"] == {"chat": CHATS["complete"], "outcome": "complete"}
        assert conversation_identity(page.url) == CHATS["complete"]
        assert machine.deferred is None and clicks == []

    drive(case, tmp_path, monkeypatch, ["working", "complete"])


def test_a_user_draft_in_the_owned_tab_is_preserved_and_blocks_navigation(
    tmp_path, monkeypatch
):
    draft = "unfinished thought\nsecond line, мысль"

    async def case(watcher, machine, clock, context, clicks, store):
        first = await visit(watcher, machine, clock)
        page = context.pages[0]
        first_chat = first["visit"]["chat"]
        await page.evaluate(
            "text => { const c = document.querySelector('[contenteditable]'); c.textContent = text; }",
            draft,
        )
        later = await visit(watcher, machine, clock)
        assert later["health"] == "protected-tab" and later["blocked"] == "draft"
        assert conversation_identity(page.url) == first_chat  # never navigated away
        assert await page.locator("[contenteditable]").text_content() == draft
        assert len(context.pages) == 1 and clicks == []

    drive(case, tmp_path, monkeypatch, ["complete", "working"])


def test_a_failed_chat_gets_one_go_through_the_real_adapter_and_the_reply_is_confirmed(
    tmp_path, monkeypatch
):
    async def case(watcher, machine, clock, context, clicks, store):
        report = await visit(watcher, machine, clock)
        assert report["visit"] == {"chat": CHATS["failed"], "outcome": "failed:sent"}
        page = context.pages[0]
        assert await page.evaluate("window.clicks") == 1
        go = machine.go
        assert go["state"] == "sent" and go["confirmation"] == "running"
        assert go["response_turn"] == "22222222-2222-4222-8222-222222222222"
        assert [a["state"] for a in store.status()["attempts"]] == ["sent"]
        # Probing opens the model menu and the send clicks Send; nothing else, never Stop.
        assert "Stop" not in clicks and "Send" in clicks
        assert set(clicks) <= {"Select ChatGPT model", "Select model", "Send"}
        assert len(context.pages) == 1

    drive(case, tmp_path, monkeypatch, ["failed"])
