"""Operator continuation against sanitized ChatGPT DOM in isolated Chromium, never a live account.

A complete (not failed) latest turn is continued with the exact Cyrillic go, once, only for an
approved queue entry, and the live loop, not the API handler, is the only code that clicks.
"""

import asyncio
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt.continuation import (
    Continuations,
    ContinueRequest,
    dispatch_continuation,
)
from agenttools_chatgpt.core import Guardian, Store
from agenttools_chatgpt.urls import canonical_chat

PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CHAT = canonical_chat(PROJECT)
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
GO = "\u0433\u043e"

HTML = """<meta charset="utf-8"><title>ChatGPT fixture</title>
<button aria-label="Open profile menu">Account</button>
<button aria-label="Select ChatGPT model" data-codex-intelligence-trigger="true"
 data-selected-reasoning-effort="medium" onclick="document.querySelector('#models').hidden=false">Pro</button>
<div id="models" role="menu" hidden><div role="menuitemradio" aria-checked="true"
 data-model-selected="true">GPT-6</div><div role="menuitemradio" aria-checked="false">GPT-5.6 Sol</div></div>
<main><div data-chatgpt-conversation-selection-target="true" data-thread-find-target="conversation">
<div data-turn-key="11111111-1111-4111-8111-111111111111"><div data-talvt-turn-state="complete">
<div role="group" aria-label="Assistant" data-markdown-copy>Finished one step; quota stopped the task.</div></div></div>
</div></main>
<div contenteditable="true" data-composer-markdown role="textbox" aria-label="Ask ChatGPT"><p data-empty-paragraph="true"><br></p></div>
<button aria-label="Start Voice">Voice</button>
<script>
window.clicks = 0; window.switches = 0;
document.addEventListener('keydown', e=>{if(e.key==='Escape')document.querySelector('#models').hidden=true});
for (const radio of document.querySelectorAll('[role=menuitemradio]')) radio.addEventListener('click', () => window.switches++);
document.querySelector('[contenteditable]').oninput=()=>{
 if(!document.querySelector('[aria-label="Send"]')){
 const send=document.createElement('button');send.ariaLabel='Send';send.textContent='Send';
 send.onclick=()=>{window.clicks++;
 document.querySelector('[data-thread-find-target]').insertAdjacentHTML('beforeend',
 '<div data-turn-key="22222222-2222-4222-8222-222222222222"><div data-talvt-turn-state="in_progress">' +
 '<div data-chatgpt-search-unit-key="message:user" data-chatgpt-search-message-ids="22222222-2222-4222-8222-222222222222">' +
 '<div data-user-message-bubble="true">\u0433\u043e</div></div></div></div>')};
 document.body.append(send)}};
</script>"""


def run(case, *, cdp_port=None, html=HTML, operator=True):
    pytest.importorskip("playwright.async_api")
    if not shutil.which("google-chrome"):
        pytest.skip("Local fixture Chrome required")

    async def scenario():
        from agenttools_chatgpt.live_browser import LiveAdapter
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                executable_path=shutil.which("google-chrome"),
                args=[f"--remote-debugging-port={cdp_port}"] if cdp_port else [],
            )
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.route(
                    "**/*",
                    lambda route: route.fulfill(body=html, content_type="text/html"),
                )
                await page.goto(PROJECT)
                await case(page, LiveAdapter(page, operator=operator))
            finally:
                await browser.close()

    asyncio.run(scenario())


def approved(store, turn=T1, **over):
    now = time.time()
    fields = {
        "request_id": str(uuid.uuid4()),
        "chat": CHAT,
        "turn": turn,
        "approved_at": now - 5,
        "expires_at": now + 600,
    }
    fields.update(over)
    cont = Continuations(store)
    accepted, receipt = cont.submit(ContinueRequest(**fields), now)
    assert accepted, receipt
    return cont, cont.get(fields["request_id"])


def make_store(tmp_path):
    store = Store(tmp_path / "state.sqlite", account="glide.vc")
    store.enroll(CHAT)
    return store


async def dispatch(store, record, adapter):
    return await dispatch_continuation(Guardian(store, apply=True), record, adapter)


def test_operator_go_is_typed_and_clicked_exactly_once_on_a_complete_turn(tmp_path):
    async def case(page, adapter):
        store = make_store(tmp_path)
        cont, record = approved(store)
        assert not (
            await adapter.observe()
        ).safe_failure()  # not a failed turn: policy is separate
        assert await dispatch(store, record, adapter) == "sent"
        assert await page.evaluate("window.clicks") == 1
        assert (
            await page.evaluate("window.switches") == 0
        )  # model menu read, never switched
        assert await page.evaluate("document.querySelector('#models').hidden")
        assert await page.evaluate("location.href") == PROJECT  # no navigation
        user = await page.locator(
            '[data-user-message-bubble="true"]'
        ).all_text_contents()
        assert user == [GO]
        receipt = cont.receipt(record.request_id, time.time())
        assert (receipt["status"], receipt["reason"]) == ("sent", "acknowledged")
        assert (
            Store(store.path, account="glide.vc").status()["attempts"][0]["state"]
            == "sent"
        )
        # Replays (same record) and a fresh approval for the same turn never click again.
        assert await dispatch(store, record, adapter) == "not-queued"
        accepted, receipt = cont.submit(
            ContinueRequest(
                str(uuid.uuid4()), CHAT, T1, time.time() - 5, time.time() + 600
            ),
            time.time(),
        )
        assert not accepted and receipt["reason"] == "turn-already-requested"
        assert await page.evaluate("window.clicks") == 1

    run(case)


@pytest.mark.parametrize(
    "state_change",
    [
        "document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','cancelled')",
        "document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','mystery')",
        "document.querySelector('[data-talvt-turn-state]').removeAttribute('data-talvt-turn-state')",
    ],
)
def test_cancelled_and_unknown_non_generating_turns_are_continued_without_forging_failure(
    tmp_path, state_change
):
    async def case(page, adapter):
        await page.evaluate("() => {" + state_change + "}")
        store = make_store(tmp_path)
        _, record = approved(store)
        observed = await adapter.observe()
        assert observed.turn == T1 and not observed.safe_failure()
        assert observed.turn_state in ("cancelled", "unknown")
        assert await dispatch(store, record, adapter) == "sent"
        assert await page.evaluate("window.clicks") == 1

    run(case)


UNSAFE = {
    "running": ("document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','in_progress')", "running"),
    "stop-button": ("document.body.insertAdjacentHTML('beforeend','<button aria-label=Stop>Stop</button>')", "running"),
    "draft": ("document.querySelector('[contenteditable]').textContent='human draft'", "draft-present"),
    "attachment": ("document.body.insertAdjacentHTML('beforeend','<div data-testid=attachment>f</div>')", "draft-present"),
    "login": ("document.body.insertAdjacentHTML('beforeend','<a href=\"/auth/login\">Log in</a>')", "challenge"),
    "second-root": ("document.querySelector('main').append(document.querySelector('[data-thread-find-target]').cloneNode(true))", "view-not-ready"),
    "duplicate-turn": ("document.querySelector('[data-thread-find-target]').append(document.querySelector('[data-turn-key]').cloneNode(true))", "view-not-ready"),
    "two-state-markers": ("document.querySelector('[data-turn-key]').append(document.querySelector('[data-talvt-turn-state]').cloneNode(true))", "view-not-ready"),
    "identity-unknown": ("document.querySelector('button').remove()", "ambiguous-page"),
    "unknown-model": ("document.querySelector('[role=menuitemradio]').textContent='Unknown Model'", "unverified-model-effort"),
    "quota-alert": ("document.body.insertAdjacentHTML('beforeend','<div role=alert>GPT-6 usage limit reached</div>')", "quota"),
    "new-turn": ("document.querySelector('[data-thread-find-target]').insertAdjacentHTML('beforeend','<div data-turn-key=33333333-3333-4333-8333-333333333333><div data-talvt-turn-state=complete>Later</div></div>')", "turn-changed"),
    "other-url": ("history.pushState({},'','/c/wrong-chat')", "chat-changed"),
    "no-composer": ("document.querySelector('[contenteditable]').remove()", "view-not-ready"),
}  # fmt: skip


@pytest.mark.parametrize("name", list(UNSAFE))
def test_unsafe_page_states_refuse_without_clicking_or_touching_the_draft(
    tmp_path, name
):
    change, reason = UNSAFE[name]

    async def case(page, adapter):
        await page.evaluate("() => {" + change + "}")
        store = make_store(tmp_path)
        cont, record = approved(store)
        before = await page.evaluate(
            "document.querySelector('[contenteditable]')?.textContent ?? null"
        )
        assert await dispatch(store, record, adapter) == reason
        assert await page.evaluate("window.clicks") == 0
        after = await page.evaluate(
            "document.querySelector('[contenteditable]')?.textContent ?? null"
        )
        assert after == before  # a human draft is never overwritten; nothing was typed
        assert await page.evaluate("window.switches") == 0
        receipt = cont.receipt(record.request_id, time.time())
        assert (receipt["status"], receipt["reason"]) == ("refused", reason)
        assert Store(store.path, account="glide.vc").status()["attempts"] == []

    run(case)


@pytest.mark.parametrize(
    "race",
    [
        "document.querySelector('[contenteditable]').textContent='human draft'",
        "document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','in_progress')",
        "history.pushState({},'', '/c/wrong-chat')",
        "document.body.insertAdjacentHTML('beforeend','<div role=alert>GPT-6 usage limit reached</div>')",
        "const c=document.querySelector('[contenteditable]'); c.replaceWith(c.cloneNode(true))",
    ],
)
def test_change_during_preparation_never_clicks_and_quarantines_the_attempt(
    tmp_path, race
):
    async def case(page, adapter):
        store = make_store(tmp_path)
        cont, record = approved(store)
        await page.evaluate(
            "() => {document.querySelector('[contenteditable]').addEventListener('input', () => {"
            + race
            + "}, {once:true})}"
        )
        assert await dispatch(store, record, adapter) == "uncertain-send"
        assert await page.evaluate("window.clicks") == 0
        receipt = cont.receipt(record.request_id, time.time())
        assert receipt["status"] == "uncertain"
        assert (
            Store(store.path, account="glide.vc").status()["attempts"][0]["state"]
            == "uncertain"
        )
        if "human draft" in race:
            assert (
                await page.locator("[contenteditable]").text_content() == "human draft"
            )

    run(case)


def test_following_assistant_reply_is_correlated_to_the_operator_send(tmp_path):
    from agenttools_chatgpt import scripts
    from agenttools_chatgpt.monitor import project_snapshot, read_source

    monitor = scripts.load("monitor_dom.js")

    async def case(page, adapter):
        store = make_store(tmp_path)
        _, record = approved(store)
        assert await dispatch(store, record, adapter) == "sent"

        async def snapshot():
            now = time.time()
            sample = dict(await page.evaluate(monitor), chat=CHAT, checked_at=now)
            controls, history = read_source(store.path, "glide.vc")
            snap = project_snapshot(
                "glide.vc", controls, history, [sample], None,
                {"connection": "connected", "heartbeat_at": now}, now,
            )  # fmt: skip
            return next(s for s in snap["sessions"] if s["chat"] == CHAT)

        row = await snapshot()
        assert row["last_go_source"] == "operator" and row["last_go_state"] == "sent"
        assert row["continuation"] == "unconfirmed" or row["continuation"] == "running"
        await page.evaluate(
            """() => { const t = document.querySelector('[data-turn-key="22222222-2222-4222-8222-222222222222"] [data-talvt-turn-state]');
              t.insertAdjacentHTML('beforeend','<div data-markdown-copy>Reply</div>'); t.setAttribute('data-talvt-turn-state','complete'); }"""
        )
        row = await snapshot()
        assert (row["continuation"], row["continuation_turn"]) == ("completed", T2)
        assert row["last_go_source"] == "operator"
        # The reply to the go is evidence the turn finished, never that the TASK is complete:
        # nothing here marks the session itself as done.
        assert row["status"] == "complete" and "task" not in row["continuation"]

    run(case)


def free_port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


@pytest.fixture
def live_owner(tmp_path, monkeypatch):
    from agenttools_chatgpt import live

    owner = live.LiveOwner
    monkeypatch.setattr(
        live, "LiveOwner", lambda root, endpoint: owner(tmp_path / "owner", endpoint)
    )
    return live


def live_args(port, *, apply):
    return SimpleNamespace(
        cdp=f"http://127.0.0.1:{port}",
        watch=[PROJECT],
        apply=apply,
        workspace="glide.vc",
        control_port=0,
        once=True,
    )


def test_live_loop_is_the_sole_sender_and_waits_for_the_queue(tmp_path, live_owner):
    port = free_port()

    async def case(page, adapter):
        store = Store(tmp_path / "glide.vc" / "state.sqlite", account="glide.vc")
        # An apply cycle with nothing queued never touches the composer, even on a complete turn.
        await live_owner.serve_live(live_args(port, apply=True), store)
        assert await page.evaluate("window.clicks") == 0
        assert await page.locator("[contenteditable]").text_content() == ""
        # Observe-only never sends, and leaves the approved request queued for an apply loop.
        cont, record = approved(store)
        await live_owner.serve_live(live_args(port, apply=False), store)
        assert await page.evaluate("window.clicks") == 0
        assert cont.receipt(record.request_id, time.time())["status"] == "queued"
        # The apply loop consumes the approval: exactly one go.
        await live_owner.serve_live(live_args(port, apply=True), store)
        assert await page.evaluate("window.clicks") == 1
        assert cont.receipt(record.request_id, time.time())["status"] == "sent"
        await live_owner.serve_live(live_args(port, apply=True), store)
        assert await page.evaluate("window.clicks") == 1

    run(case, cdp_port=port)


def test_live_loop_refuses_stale_turn_and_missing_page_without_sending(
    tmp_path, live_owner
):
    port = free_port()

    async def case(page, adapter):
        store = Store(tmp_path / "glide.vc" / "state.sqlite", account="glide.vc")
        other = "https://chatgpt.com/c/cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        store.enroll(CHAT)
        store.enroll(other)
        _, stale = approved(
            store, turn=T2
        )  # approval for a turn that is not the latest
        _, missing = approved(store, chat=other)  # watched chat with no open page
        cont = Continuations(store)
        await live_owner.serve_live(live_args(port, apply=True), store)
        await live_owner.serve_live(live_args(port, apply=True), store)
        assert await page.evaluate("window.clicks") == 0
        assert cont.receipt(stale.request_id, time.time())["reason"] == "turn-changed"
        assert (
            cont.receipt(missing.request_id, time.time())["reason"]
            == "page-missing-or-duplicate"
        )

    run(case, cdp_port=port)


def test_live_loop_refuses_a_stopped_chat_with_its_exact_reason(tmp_path, live_owner):
    port = free_port()

    async def case(page, adapter):
        store = Store(tmp_path / "glide.vc" / "state.sqlite", account="glide.vc")
        store.enroll(CHAT)
        cont, record = approved(store)
        store.stop(CHAT, "human stop")
        await live_owner.serve_live(live_args(port, apply=True), store)
        assert await page.evaluate("window.clicks") == 0
        receipt = cont.receipt(record.request_id, time.time())
        assert (receipt["status"], receipt["reason"]) == ("refused", "stopped")
        assert Store(store.path, account="glide.vc").status()["attempts"] == []

    run(case, cdp_port=port)


def test_live_start_marks_an_interrupted_dispatch_uncertain_and_never_resends(
    tmp_path, live_owner
):
    from agenttools_chatgpt.core import Observation

    port = free_port()

    async def case(page, adapter):
        store = Store(tmp_path / "glide.vc" / "state.sqlite", account="glide.vc")
        store.enroll(CHAT)
        cont, record = approved(store)
        seen = Observation(
            CHAT, T1, T1, verified=True, model="Astra", ambiguous=False,
            turn_state="complete",
        )  # fmt: skip
        assert (
            cont.reserve(record, seen, time.time()) == "reserved"
        )  # killed mid-dispatch
        await live_owner.serve_live(live_args(port, apply=True), store)
        await live_owner.serve_live(live_args(port, apply=True), store)
        assert await page.evaluate("window.clicks") == 0
        receipt = cont.receipt(record.request_id, time.time())
        assert (receipt["status"], receipt["reason"]) == (
            "uncertain",
            "restart-interrupted",
        )
        assert (
            Store(store.path, account="glide.vc").status()["attempts"][0]["state"]
            == "reserved"
        )

    run(case, cdp_port=port)


def test_automatic_recovery_of_failed_turns_is_unchanged(tmp_path):
    failed = HTML.replace(
        '<div role="group" aria-label="Assistant" data-markdown-copy>Finished one step; quota stopped the task.</div>',
        '<div class="group/activity-header"><span>Thinking failed</span></div>',
    ).replace('data-talvt-turn-state="complete"', 'data-talvt-turn-state="cancelled"')

    async def case(page, adapter):
        store = make_store(tmp_path)
        observed = await adapter.observe()
        assert observed.safe_failure() and observed.turn_state == "cancelled"
        guardian = Guardian(store, apply=True)
        assert await guardian.run_once(adapter) == "stabilizing"
        now = time.monotonic()
        guardian.stability[observed.chat] = observed, now - 21
        guardian.last_seen[observed.chat] = now
        assert await guardian.run_once(adapter) == "sent"
        assert await page.evaluate("window.clicks") == 1
        assert await guardian.run_once(adapter) == "unsafe-or-not-active-failure"

    run(case, html=failed, operator=False)


# A conversation scrolled away from its end (real pages open like this): the ordinary
# observation is deliberately blind until the approved view preparation brings the end into view.
SCROLLED = HTML.replace(
    "<main>",
    '<main style="height:200px;overflow-y:auto"><div style="height:900px"></div>',
)
OTHER_CHAT = canonical_chat("https://chatgpt.com/c/cccccccc-cccc-4ccc-8ccc-cccccccccccc")
MAIN_TOP = "document.querySelector('main').scrollTop"
COMPOSER_TEXT = "document.querySelector('[contenteditable]').textContent"


def test_scrolled_conversation_is_prepared_then_continued_exactly_once(tmp_path):
    async def case(page, adapter):
        seen = []
        observe = adapter.observe

        async def recording():
            seen.append(await observe())
            return seen[-1]

        adapter.observe = recording
        store = make_store(tmp_path)
        cont, record = approved(store)
        assert await page.evaluate(MAIN_TOP) == 0
        assert await dispatch(store, record, adapter) == "sent"
        assert seen[0].turn == ""  # unreadable before the approved preparation
        assert await page.evaluate(MAIN_TOP) > 0
        assert await page.evaluate("window.clicks") == 1
        assert await page.evaluate("window.switches") == 0
        assert await page.evaluate("location.href") == PROJECT
        assert cont.receipt(record.request_id, time.time())["status"] == "sent"
        assert await dispatch(store, record, adapter) == "not-queued"
        assert await page.evaluate("window.clicks") == 1

    run(case, html=SCROLLED)


PREPARE_REFUSALS = {
    "draft": "document.querySelector('[contenteditable]').textContent='human draft'",
    "running": "document.body.insertAdjacentHTML('beforeend','<button aria-label=Stop>Stop</button>')",
    "challenge": "document.body.insertAdjacentHTML('beforeend','<div role=dialog><h2>Log in</h2><input type=password></div>')",
    "attachment": "document.body.insertAdjacentHTML('beforeend','<div data-testid=attachment>f</div>')",
    "later-turn": "document.querySelector('[data-thread-find-target]').insertAdjacentHTML('beforeend','<div data-turn-key=33333333-3333-4333-8333-333333333333><div data-talvt-turn-state=complete>Later</div></div>')",
    "identity-unknown": "document.querySelector('button[aria-label=\"Open profile menu\"]').remove()",
}  # fmt: skip


@pytest.mark.parametrize("name", list(PREPARE_REFUSALS))
def test_prepare_view_refuses_an_unsafe_page_without_scrolling(name):
    async def case(page, adapter):
        await page.evaluate("() => {" + PREPARE_REFUSALS[name] + "}")
        assert not await adapter.prepare_view(CHAT, T1, time.time() + 600)
        assert await page.evaluate(MAIN_TOP) == 0

    run(case, html=SCROLLED)


def test_prepare_view_binds_chat_turn_and_expiry_and_never_navigates():
    async def case(page, adapter):
        live = time.time() + 600
        assert not await adapter.prepare_view(CHAT, T2, live)
        assert not await adapter.prepare_view(OTHER_CHAT, T1, live)
        assert not await adapter.prepare_view(CHAT, T1, time.time() - 1)
        assert await page.evaluate(MAIN_TOP) == 0
        assert await adapter.prepare_view(CHAT, T1, live) is True
        assert await page.evaluate(MAIN_TOP) > 0
        assert await page.evaluate("location.href") == PROJECT
        assert await page.evaluate("window.clicks") == 0
        assert await page.evaluate(COMPOSER_TEXT) == ""

    run(case, html=SCROLLED)


def test_prepare_view_is_operator_only():
    async def case(page, adapter):
        assert not await adapter.prepare_view(CHAT, T1, time.time() + 600)
        assert await page.evaluate(MAIN_TOP) == 0

    run(case, html=SCROLLED, operator=False)


def test_prepare_view_is_a_no_op_on_a_readable_page():
    async def case(page, adapter):
        assert await adapter.prepare_view(CHAT, T1, time.time() + 600) is True
        assert await page.evaluate("document.scrollingElement.scrollTop") == 0
        assert await page.evaluate("window.clicks") == 0

    run(case)


def test_send_after_approval_expiry_types_and_clicks_nothing():
    async def case(page, adapter):
        observed = await adapter.observe()
        adapter.approval_expires_at = time.time() - 0.5
        assert not await adapter.send(observed)
        assert await page.evaluate("window.clicks") == 0
        assert await page.evaluate(COMPOSER_TEXT) == ""

    run(case)


def test_approval_expiring_during_the_probe_never_clicks_and_stays_quarantined(
    tmp_path,
):
    async def case(page, adapter):
        store = make_store(tmp_path)
        cont, record = approved(store, expires_at=time.time() + 4)
        adapter.approval_expires_at = record.expires_at
        inside, slept = [], []
        send, probe = adapter.send, adapter.probe_model

        async def wrapped_send(expected):
            inside.append(True)
            try:
                return await send(expected)
            finally:
                inside.pop()

        async def wrapped_probe():
            if inside and not slept:
                slept.append(True)
                await asyncio.sleep(4.5)
            await probe()

        adapter.send, adapter.probe_model = wrapped_send, wrapped_probe
        assert await dispatch(store, record, adapter) == "uncertain-send"
        assert slept and await page.evaluate("window.clicks") == 0
        receipt = cont.receipt(record.request_id, time.time())
        assert receipt["status"] == "uncertain"
        attempts = Store(store.path, account="glide.vc").status()["attempts"]
        assert attempts[0]["state"] == "uncertain"
        assert await dispatch(store, record, adapter) == "not-queued"

    run(case)
