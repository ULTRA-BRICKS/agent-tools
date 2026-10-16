"""Exercise HA adapter without account, browser launch, or network."""

import asyncio
import importlib
from contextlib import nullcontext
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize(
    "during", ["send-wait", "reply-readiness", "answer-read", "extraction", "roundtrip"]
)
def test_api_navigation_never_sends_or_returns_other_chat(tmp_path, during):
    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, run

    async def scenario(page, dom):
        source = bridge()
        source.ASSISTANT = '.turn[data-author="assistant"]'
        source.DOM_MARKDOWN = "el => el.textContent"
        source.LIMIT = SimpleNamespace(search=lambda text: False)
        source.visible = lambda locator: locator.is_visible()
        await page.locator(CONTRACT.send).evaluate("""el => el.onclick = () => {
            window.sent = true;
            document.querySelector('#composer').value = '';
            document.querySelector('#conversation').insertAdjacentHTML('beforeend',
              '<article class="turn" data-id="user-new" data-generation="u1" data-author="user"><div class="body">hello</div></article>' +
              '<article class="turn" data-id="answer-new" data-generation="a1" data-author="assistant"><div class="body">right answer</div></article>');
        }""")
        original_locator, original_evaluate = page.locator, page.evaluate

        async def navigate():
            await original_evaluate("""() => {
                history.pushState({}, '', '/c/other');
                document.querySelector('#composer').value = 'hello';
                document.querySelector('#conversation').insertAdjacentHTML('beforeend',
                  '<article class="turn" data-id="wrong" data-generation="wrong-g" data-author="assistant"><div class="body">wrong existing answer</div></article>');
            }""")

        if during in ("send-wait", "roundtrip"):

            def locator(selector):
                result = original_locator(selector)
                if selector == CONTRACT.send:
                    original_wait = result.wait_for

                    async def wait_for(**kwargs):
                        await original_wait(**kwargs)
                        await navigate()
                        if during == "roundtrip":
                            await original_evaluate(
                                "history.pushState({}, '', '/c/fixture')"
                            )

                    result.wait_for = wait_for
                return result

            page.locator = locator
        elif during == "reply-readiness":

            async def readiness(page):
                await navigate()
                return "ready"

            source.readiness = readiness
        elif during == "answer-read":

            async def visible(locator):
                await navigate()
                return False

            source.visible = visible
        else:
            source.DOM_MARKDOWN = """el => {
                history.pushState({}, '', '/c/other');
                return 'wrong existing answer';
            }"""
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite")),
            contract=CONTRACT,
        )
        browser.reply_timeout = 5
        with pytest.raises(BridgeError, match="[Cc]hat|[Nn]avigation|[Cc]onversation"):
            await browser.ask(page, "hello")
        if during in ("send-wait", "roundtrip"):
            assert not await original_evaluate("Boolean(window.sent)")

    run(scenario)


@pytest.mark.parametrize("attribute", ["data-id", "data-generation"])
@pytest.mark.parametrize("while_running", [False, True])
def test_api_reply_rejects_replaced_submitted_turn(tmp_path, attribute, while_running):
    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, run

    async def scenario(page, dom):
        source = bridge()
        source.DOM_MARKDOWN = "el => el.textContent"
        source.LIMIT = SimpleNamespace(search=lambda text: False)
        polls = 0

        async def visible(locator):
            return while_running and polls == 1

        source.visible = visible

        async def readiness(page):
            nonlocal polls
            polls += 1
            if polls == 2:
                await page.locator('.turn[data-id="submitted-answer"]').evaluate(
                    "(el, attribute) => el.setAttribute(attribute, 'different')",
                    attribute,
                )
            return "ready"

        source.readiness = readiness
        await page.locator(CONTRACT.send).evaluate("""el => el.onclick = () => {
            document.querySelector('#composer').value = '';
            document.querySelector('#conversation').insertAdjacentHTML('beforeend',
              '<article class="turn" data-id="submitted-user" data-author="user"><div class="body">hello</div></article>' +
              '<article class="turn" data-id="submitted-answer" data-generation="generation" data-author="assistant"><div class="body">same text</div></article>');
        }""")
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite")),
            contract=CONTRACT,
        )
        browser.reply_timeout = 4
        with pytest.raises(BridgeError, match="assistant turn changed"):
            await browser.ask(page, "hello")

    run(scenario)


def test_dispatch_intervening_draft_stop_survives_clear(tmp_path, monkeypatch):
    from dataclasses import replace

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=True)
        chat = "https://chatgpt.com/c/draft"
        safe = Observation(
            chat,
            "turn",
            "generation",
            model="Astra",
            verified=True,
            ambiguous=False,
            active=True,
            status="Thinking failed",
        )
        current = replace(safe, draft=True)

        async def observe(page):
            return current

        page = api_page(monkeypatch, chat, observe_callback=observe)
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=guardian,
            contract=Contract(verified_account_ui=True),
        )
        with pytest.raises(BridgeError):
            await asyncio.wait_for(browser.dispatch_prompt(page, "hello"), timeout=2)
        assert Store(store.path).controls()["stop:" + chat] == "human draft"
        current = safe
        store.enroll(chat)
        browser.ctx = SimpleNamespace(pages=[page])
        browser.status_page = page
        import time

        now = time.monotonic()
        guardian.stability[chat] = safe, now - 21
        guardian.last_seen[chat] = now
        await browser.monitor_once()
        assert store.status()["attempts"] == []
        guardian.stability[chat] = safe, 0
        guardian.last_seen[chat] = 20
        assert guardian.consider(safe, 21, None) == "stopped"
        store.resume(chat)
        assert guardian.consider(safe, 22, None) != "stopped"

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["recovery", "prepare-api", "send-api"])
@pytest.mark.parametrize("signal", ["quota", "draft", "challenge"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_mutation_safety_commits_before_concurrent_worker(
    tmp_path, monkeypatch, operation, signal, cancelled
):
    import concurrent.futures
    import threading
    import time
    from dataclasses import asdict, replace

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, MutationRefusal, Store
    from test_chatgpt_guardian import failure

    async def scenario():
        store = Store(tmp_path / "state.sqlite")
        other = Store(store.path)
        guardian = Guardian(store, apply=True)
        observed = failure(chat="https://chatgpt.com/c/first")
        evidence = replace(observed, **{signal: True})
        started, contender_started = asyncio.Event(), asyncio.Event()
        release, occupied = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        caller = asyncio.current_task()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))

        def occupy_executor():
            occupied.set()
            assert release.wait(2), "contender did not release executor"

        async def refuse():
            loop.run_in_executor(None, occupy_executor)
            while not occupied.is_set():
                await asyncio.sleep(0)
            started.set()
            await contender_started.wait()
            if cancelled:
                caller.cancel()
            await asyncio.sleep(0)
            return MutationRefusal(evidence)

        async def contender():
            await started.wait()
            contender_started.set()
            try:
                chat = (
                    "https://chatgpt.com/c/second"
                    if signal == "quota"
                    else observed.chat
                )
                async with other.async_dispatch_api(
                    chat, "Astra", verified=True
                ) as allowed:
                    controls = other.controls()
                    assert not allowed, (
                        "second Astra authorized after mutation safety evidence"
                    )
                    key = (
                        "astra_quota" if signal == "quota" else "stop:" + observed.chat
                    )
                    assert key in controls
            finally:
                release.set()

        worker = asyncio.create_task(contender())
        try:
            if operation == "recovery":
                now = time.monotonic()
                guardian.stability[observed.chat] = observed, now - 21
                guardian.last_seen[observed.chat] = now
                transport = SimpleNamespace(
                    observe=AsyncMock(return_value=observed), send=refuse
                )
                transport.send = lambda expected: refuse()
                with (
                    pytest.raises(asyncio.CancelledError)
                    if cancelled
                    else nullcontext()
                ):
                    result = await guardian.run_once(transport)
                    assert result == "uncertain-send"
                attempts = store.status()["attempts"]
                assert len(attempts) == 1 and attempts[0]["state"] == "uncertain"
            else:
                page = api_page(monkeypatch, observed.chat)
                original = page.evaluate

                async def evaluate(script, payload):
                    if payload.get("action") == operation:
                        result = await refuse()
                        return {"blocked": asdict(result.observation)}
                    return await original(script, payload)

                page.evaluate = evaluate
                page.locator = lambda selector: SimpleNamespace(
                    count=AsyncMock(return_value=0), wait_for=AsyncMock()
                )
                source = bridge()
                source.ASSISTANT = "answers"
                browser = adapter().create_browser(
                    source,
                    SimpleNamespace(),
                    "managed",
                    guardian=guardian,
                    contract=Contract(verified_account_ui=True),
                )
                with pytest.raises(
                    asyncio.CancelledError if cancelled else BridgeError
                ):
                    if operation == "prepare-api":
                        await browser.ask(page, "hello")
                    else:
                        await browser.dispatch_prompt(page, "hello")
                assert store.status()["attempts"] == []
            await worker
        finally:
            release.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4))


def test_api_draft_stop_survives_clear_until_human_resume(tmp_path):
    import time

    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, run

    async def scenario(page, dom):
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=True)
        browser = adapter().create_browser(
            bridge(), SimpleNamespace(), "managed", guardian=guardian, contract=CONTRACT
        )
        await page.locator(CONTRACT.composer).fill("human draft")
        with pytest.raises(BridgeError, match="Existing draft"):
            await browser.ask(page, "hello")
        assert Store(store.path).controls()["stop:" + page.url] == "human draft"
        await page.locator(CONTRACT.composer).fill("")
        browser.ctx = SimpleNamespace(pages=[page])
        browser.status_page = page
        await page.locator(CONTRACT.send).evaluate(
            "el => el.onclick = () => window.clicked = true"
        )
        store.enroll(page.url)
        observed = await dom.observe()
        now = time.monotonic()
        guardian.stability[page.url] = observed, now - 21
        guardian.last_seen[page.url] = now
        await browser.monitor_once()
        assert store.status()["attempts"] == []
        assert not await page.evaluate("Boolean(window.clicked)")
        store.resume(page.url)
        await browser.monitor_once()
        assert store.status()["attempts"][0]["state"] == "uncertain"
        assert await page.evaluate("Boolean(window.clicked)")
        assert await page.locator(CONTRACT.composer).input_value() == "го"

    run(scenario)


def test_reply_polling_skips_growing_answer_until_running_ends(tmp_path, monkeypatch):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        source = bridge()
        source.ASSISTANT = "answers"
        source.LIMIT = SimpleNamespace(search=lambda text: False)
        running = [True, True, True, False, False, False, False, False]
        reads = []
        tick = 0

        async def visible(locator):
            nonlocal tick
            tick += 1
            return running[min(tick - 1, len(running) - 1)]

        async def read():
            reads.append(tick)
            return "x" * (tick * 1000) if tick < 4 else "complete answer"

        source.visible = visible
        answer = SimpleNamespace(
            inner_text=AsyncMock(side_effect=read),
            evaluate=AsyncMock(return_value="complete answer"),
        )
        answers = SimpleNamespace(
            count=AsyncMock(side_effect=[0] + [1] * 20), last=answer
        )
        page = api_page(monkeypatch, "https://chatgpt.com/c/reply")
        original_evaluate = page.evaluate

        async def evaluate(script, payload):
            if payload.get("action") in ("reply", "reply-identity"):
                active = payload.get("action") == "reply-identity"
                return {
                    "chat": page.url,
                    "running": active,
                    "challenge": False,
                    "quota": False,
                    "turns": [
                        {
                            "turn": "user",
                            "generation": "",
                            "author": "user",
                            "text": "hello",
                        },
                        {
                            "turn": "answer",
                            "generation": "generation",
                            "author": "assistant",
                            "text": "" if active else await answer.inner_text(),
                        },
                    ],
                    "markdown": ""
                    if active
                    else await answer.evaluate(source.DOM_MARKDOWN),
                }
            return await original_evaluate(script, payload)

        page.evaluate = AsyncMock(side_effect=evaluate)
        page.locator = lambda selector: (
            answers if selector == "answers" else SimpleNamespace(wait_for=AsyncMock())
        )
        page.get_by_role = lambda role: SimpleNamespace(count=AsyncMock(return_value=0))
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite")),
            contract=Contract(verified_account_ui=True),
        )
        browser.reply_timeout = 6
        assert await browser.ask(page, "hello") == "complete answer"
        assert reads and min(reads) >= 4
        assert answer.inner_text.await_count >= 4
        assert answer.evaluate.await_count >= 4

    asyncio.run(scenario())


def adapter():
    try:
        return importlib.import_module("agenttools_chatgpt.ha_bridge")
    except ModuleNotFoundError:
        pytest.fail("HA adapter behavior is missing")


class BridgeError(Exception):
    def __init__(self, message, status=502, kind="upstream_error"):
        super().__init__(message)
        self.status, self.kind = status, kind


class BaseBrowser:
    def __init__(self, *args):
        self.lock = asyncio.Lock()
        self.convs = []
        self.state = "starting"
        self.max_tabs = 3

    async def set_state(self, state):
        self.state = state

    def chat_url(self):
        return "https://chatgpt.com/"


def bridge(state="ready"):
    return SimpleNamespace(
        Browser=BaseBrowser,
        BridgeError=BridgeError,
        NeedsHuman=lambda state: BridgeError(state, 503, state),
        Conversation=lambda page: SimpleNamespace(page=page),
        readiness=AsyncMock(return_value=state),
        HOME="https://chatgpt.com/",
        DOM_MARKDOWN="fixture rendered extraction",
    )


def api_page(monkeypatch, chat, send_callback=None, observe_callback=None):
    """Mock the browser transport while exercising the real shared adapter."""
    import sys
    from dataclasses import asdict

    from agenttools_chatgpt.core import Observation

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )
    page = SimpleNamespace(url=chat)

    async def evaluate(script, payload):
        action = payload.get("action")
        if action == "reply-baseline":
            return {"chat": page.url, "turns": []}
        if action in ("observe-api", "observe"):
            if observe_callback is not None:
                return asdict(await observe_callback(page))
            return asdict(
                Observation(
                    page.url.rstrip("/"),
                    "",
                    "",
                    model="Astra",
                    verified=True,
                    ambiguous=False,
                )
            )
        if action == "prepare-api":
            page.prepared_prompt = payload["ownedDraft"]
            return True
        if action == "send-api":
            if send_callback is not None:
                return await send_callback(payload)
            return True
        raise AssertionError("unexpected browser action: " + str(action))

    page.evaluate = AsyncMock(side_effect=evaluate)
    return page


@pytest.mark.parametrize("busy", [False, True])
def test_initial_api_quota_latches_other_astra_chat(tmp_path, monkeypatch, busy):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite")

        async def observe(page):
            return Observation(
                page.url,
                "",
                "",
                verified=True,
                model="Astra",
                ambiguous=False,
                quota=True,
                running=busy,
            )

        page = api_page(
            monkeypatch, "https://chatgpt.com/c/quota", observe_callback=observe
        )
        page.locator = lambda key: SimpleNamespace(
            count=AsyncMock(return_value=0), wait_for=AsyncMock()
        )
        source = bridge()
        source.ASSISTANT = ".assistant"
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True),
        )
        with pytest.raises(BridgeError):
            await asyncio.wait_for(browser.ask(page, "hello"), timeout=2)
        assert store.controls().get("astra_quota") == "blocked"
        other = api_page(monkeypatch, "https://chatgpt.com/c/other")
        with pytest.raises(BridgeError) as error:
            await browser.dispatch_prompt(other, "hello")
        assert error.value.kind == "guardian_stopped"
        assert not any(
            call.args[1].get("action") in ("prepare-api", "send-api")
            for call in page.evaluate.await_args_list + other.evaluate.await_args_list
        )

    asyncio.run(scenario())


def test_preserves_restored_tabs_without_clipboard_hooks():
    async def scenario():
        page = SimpleNamespace(url="https://chatgpt.com/c/restored", goto=AsyncMock())
        ctx = SimpleNamespace(pages=[page], add_init_script=AsyncMock())
        life = SimpleNamespace(start=AsyncMock(return_value=ctx))
        browser = adapter().create_browser(bridge(), life, "managed")
        await browser.start()
        assert browser.status_page is page
        page.goto.assert_not_awaited()
        ctx.add_init_script.assert_not_awaited()

    asyncio.run(scenario())


def test_preserves_challenged_conversation_for_human():
    async def scenario():
        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
        browser = adapter().create_browser(
            bridge("challenge_required"), SimpleNamespace(), "managed"
        )
        browser.ctx = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with pytest.raises(BridgeError) as error:
            await browser.new_conversation()
        assert error.value.status == 503
        page.close.assert_not_awaited()
        assert browser.state == "challenge_required"

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["challenge_required", "signin_required"])
def test_new_conversation_stop_survives_ready_status_refresh(tmp_path, state):
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite")
        source = bridge(state)
        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
        browser = adapter().create_browser(
            source, SimpleNamespace(), "managed", guardian=Guardian(store)
        )
        browser.ctx = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with pytest.raises(BridgeError):
            await browser.new_conversation()
        source.readiness.return_value = "ready"
        browser.status_page = SimpleNamespace(url="https://chatgpt.com/c/ready")
        assert await browser.refresh_state() == "ready"
        with pytest.raises(BridgeError) as error:
            await browser.enforce_quota(browser.status_page)
        assert error.value.kind == "guardian_stopped"
        assert "stop:*" in Store(store.path).controls()
        store.resume()
        await browser.enforce_quota(browser.status_page)

    asyncio.run(scenario())


def test_capacity_refuses_without_closing_tabs():
    async def scenario():
        page = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())
        browser = adapter().create_browser(bridge(), SimpleNamespace(), "managed")
        browser.convs = [SimpleNamespace(page=page)] * browser.max_tabs
        browser.ctx = SimpleNamespace(new_page=AsyncMock())
        with pytest.raises(BridgeError):
            await browser.new_conversation()
        page.close.assert_not_awaited()
        browser.ctx.new_page.assert_not_awaited()

    asyncio.run(scenario())


def test_manual_mode_never_attaches_and_returns_503():
    async def scenario():
        life = SimpleNamespace(start_manual=AsyncMock(), start=AsyncMock())
        browser = adapter().create_browser(bridge(), life, "manual")
        await browser.start()
        life.start.assert_awaited_once_with(manual=True)
        with pytest.raises(BridgeError) as error:
            await browser.complete([], [])
        assert (error.value.status, error.value.kind) == (503, "manual_login")

    asyncio.run(scenario())


def test_refresh_never_reloads_human_challenge():
    async def scenario():
        page = SimpleNamespace(reload=AsyncMock())
        browser = adapter().create_browser(
            bridge("challenge_required"), SimpleNamespace(), "managed"
        )
        browser.status_page = page
        await browser.refresh_state(reload=True)
        page.reload.assert_not_awaited()
        assert browser.state == "challenge_required"

    asyncio.run(scenario())


def test_monitor_does_not_race_api_completion():
    async def scenario():
        source = bridge()
        browser = adapter().create_browser(source, SimpleNamespace(), "managed")
        async with browser.lock:
            await browser.monitor_once()
        source.readiness.assert_not_awaited()

    asyncio.run(scenario())


def test_navigation_failure_preserves_tab():
    async def scenario():
        page = SimpleNamespace(
            goto=AsyncMock(side_effect=TimeoutError()), close=AsyncMock()
        )
        browser = adapter().create_browser(bridge(), SimpleNamespace(), "managed")
        browser.ctx = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with pytest.raises(BridgeError) as error:
            await browser.new_conversation()
        assert error.value.kind == "navigation_failed"
        page.close.assert_not_awaited()

    asyncio.run(scenario())


def test_quota_refuses_api_even_without_ui_contract():
    async def scenario():
        guardian = SimpleNamespace(
            store=SimpleNamespace(status=lambda: {"controls": {"astra_quota": "1"}})
        )
        browser = adapter().create_browser(
            bridge(), SimpleNamespace(), "managed", guardian=guardian
        )
        with pytest.raises(BridgeError) as error:
            await browser.complete([{"role": "user", "content": "hello"}], [])
        assert (error.value.status, error.value.kind) == (429, "rate_limited")
        with pytest.raises(BridgeError):
            await browser.ask(
                SimpleNamespace(url="https://chatgpt.com/c/chat"), "hello"
            )

    asyncio.run(scenario())


def test_quota_between_draft_and_click_preserves_draft_without_send(
    tmp_path, monkeypatch
):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        guardian = Guardian(store)
        source = bridge()
        source.COMPOSER, source.STOP, source.SEND, source.ASSISTANT = (
            "composer",
            "stop",
            "send",
            "answers",
        )
        source.visible = AsyncMock(return_value=False)
        composer = SimpleNamespace(
            count=AsyncMock(return_value=1),
            evaluate=AsyncMock(return_value=""),
            fill=AsyncMock(),
        )

        async def arrive(**kwargs):
            await asyncio.to_thread(store.quota)

        send = SimpleNamespace(
            wait_for=AsyncMock(side_effect=arrive), click=AsyncMock()
        )
        answers = SimpleNamespace(count=AsyncMock(return_value=0))
        page = api_page(monkeypatch, "https://chatgpt.com/c/chat")
        page.locator = lambda key: {
            "composer": composer,
            "stop": object(),
            "send": send,
            "answers": answers,
        }[key]
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=guardian,
            contract=Contract(verified_account_ui=True, send="send"),
        )
        with pytest.raises(BridgeError) as error:
            await browser.ask(page, "hello")
        assert error.value.status == 429
        composer.fill.assert_not_awaited()
        assert page.prepared_prompt == "hello"
        send.click.assert_not_awaited()

    asyncio.run(scenario())


def test_existing_draft_is_never_overwritten(tmp_path):
    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, run

    async def scenario(page, dom):
        await page.locator(CONTRACT.composer).fill("Human text — keep this")
        observed = await dom.observe_api()
        assert observed.verified and observed.draft and not observed.ambiguous
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite3")),
            contract=CONTRACT,
        )
        with pytest.raises(BridgeError) as error:
            await browser.ask(page, "hello")
        assert error.value.kind == "busy"
        assert (
            await page.locator(CONTRACT.composer).input_value()
            == "Human text — keep this"
        )

    run(scenario)


def test_ha_ask_uses_contract_when_legacy_selectors_diverge(tmp_path):
    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, run

    async def scenario(page, dom):
        source = bridge()
        source.COMPOSER = "#obsolete-composer"
        source.SEND = "#obsolete-send"
        source.ASSISTANT = '.turn[data-author="assistant"]'
        source.DOM_MARKDOWN = "el => el.textContent"
        source.visible = lambda locator: locator.is_visible()
        await page.locator(CONTRACT.send).evaluate("""el => el.onclick = () => {
            document.querySelector('#composer').value = '';
            const answer = document.createElement('article');
            answer.className = 'turn'; answer.dataset.author = 'assistant';
            answer.dataset.id = 'reply'; answer.dataset.generation = 'reply-generation';
            answer.innerHTML = '<div class="body">fixture answer</div>';
            document.querySelector('#conversation').insertAdjacentHTML('beforeend',
              '<article class="turn" data-id="submitted-user" data-author="user"><div class="body">hello</div></article>');
            document.querySelector('#conversation').append(answer);
        }""")
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite3")),
            contract=CONTRACT,
        )
        browser.reply_timeout = 5
        assert await browser.ask(page, "hello") == "fixture answer"
        assert await page.locator(CONTRACT.composer).input_value() == ""

    run(scenario)


def test_challenge_during_api_reply_latches_stop(tmp_path, monkeypatch):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite")
        source = bridge("challenge_required")
        source.ASSISTANT = "answers"
        page = api_page(monkeypatch, "https://chatgpt.com/c/chat")
        page.locator = lambda selector: SimpleNamespace(
            count=AsyncMock(return_value=0), wait_for=AsyncMock()
        )
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        browser.reply_timeout = 5
        with pytest.raises(BridgeError) as error:
            await browser.ask(page, "hello")
        assert error.value.kind == "challenge_required"
        assert "stop:" + page.url in store.controls()

    asyncio.run(scenario())


def test_ha_startup_defaults_keep_home_quota_out_of_glide_account(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from agenttools_chatgpt import core

    paths = []
    stores = []
    original_store = core.Store

    def store(path, *, account):
        paths.append((path, account))
        result = original_store(tmp_path / path.relative_to("/data"), account=account)
        stores.append(result)
        return result

    class StartupInspected(Exception):
        pass

    def inspect(*args, **kwargs):
        raise StartupInspected

    monkeypatch.setattr(core, "Store", store)
    monkeypatch.setattr(adapter(), "create_browser", inspect)
    monkeypatch.setenv("CHATGPT_WEB_API_KEY", "x" * 32)
    monkeypatch.setenv("CHATGPT_WEB_BROWSER_MODE", "managed")
    # This test checks account-scoped startup, not aiohttp transport. The HA
    # dependency is optional in the root project's minimal pytest CI environment.
    import sys

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=SimpleNamespace()))
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    monkeypatch.delenv("CHATGPT_GUARDIAN_CONTRACT", raising=False)
    for account in ("home", "glide.vc"):
        monkeypatch.setenv("CHATGPT_GUARDIAN_WORKSPACE", account)
        with pytest.raises(StartupInspected):
            asyncio.run(adapter().run(SimpleNamespace(Notifier=lambda *args: None)))
        if account == "home":
            stores[-1].quota()
            stores[-1].stop()
    assert paths == [
        (Path("/data/guardian-state") / scope / "state.sqlite", scope)
        for scope in ("home", "glide.vc")
    ]
    assert "astra_quota" in stores[0].controls()
    assert stores[1].controls() == {}


def test_quota_on_unregistered_tab_is_global_before_watched_recovery(
    tmp_path, monkeypatch
):
    import sys
    from dataclasses import asdict

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )

    async def scenario():
        watched_url = "https://chatgpt.com/c/watched"
        quota_url = "https://chatgpt.com/c/restored-unregistered"
        watched = SimpleNamespace(
            url=watched_url + "/",
            evaluate=AsyncMock(
                return_value=asdict(
                    Observation(
                        watched_url, "turn", "generation", model="Astra", verified=True
                    )
                )
            ),
        )
        unregistered = SimpleNamespace(
            url=quota_url,
            evaluate=AsyncMock(
                return_value=asdict(
                    Observation(
                        quota_url,
                        "quota-turn",
                        "generation",
                        model="Astra",
                        quota=True,
                        challenge=True,
                    )
                )
            ),
        )
        store = Store(tmp_path / "state.sqlite3")
        store.set_control("watch:" + watched_url, "1")
        guardian = Guardian(store, apply=False)
        original = guardian.run_once

        async def recover(observed_adapter, initial=None, observed_at=None):
            assert "astra_quota" in store.status()["controls"]
            assert observed_adapter.page is watched
            return await original(
                observed_adapter, initial=initial, observed_at=observed_at
            )

        guardian.run_once = AsyncMock(side_effect=recover)
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=guardian,
            contract=Contract(),
        )
        browser.ctx = SimpleNamespace(pages=[watched, unregistered])
        browser.status_page = watched
        await browser.monitor_once()
        assert "astra_quota" in store.status()["controls"]
        assert "stop:" + quota_url in store.status()["controls"]
        unregistered.evaluate.assert_awaited_once()
        assert guardian.run_once.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("suffix", ["/", "?foo=1", "#fragment"])
def test_trailing_slash_chat_honors_per_chat_stop(tmp_path, suffix):
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        store.stop("https://chatgpt.com/c/stopped")
        source = bridge()
        browser = adapter().create_browser(
            source, SimpleNamespace(), "managed", guardian=Guardian(store)
        )
        with pytest.raises(BridgeError) as error:
            await browser.ask(
                SimpleNamespace(url="https://chatgpt.com/c/stopped" + suffix), "hello"
            )
        assert error.value.kind == "guardian_stopped"
        source.readiness.assert_not_awaited()

    asyncio.run(scenario())


def test_final_dispatch_serializes_stop_mutation_until_click(tmp_path, monkeypatch):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        stop_task = None

        async def click(payload):
            nonlocal stop_task
            stop_task = asyncio.create_task(asyncio.to_thread(store.stop))
            await asyncio.sleep(0.05)
            assert "stop:*" not in store.status()["controls"], (
                "stop committed before atomic dispatch"
            )
            return True

        page = api_page(monkeypatch, "https://chatgpt.com/c/chat", send_callback=click)
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        try:
            await browser.dispatch_prompt(page, "hello")
        finally:
            if stop_task is not None:
                await stop_task
        assert "stop:*" in store.status()["controls"]
        assert (
            sum(
                call.args[1].get("action") == "send-api"
                for call in page.evaluate.await_args_list
            )
            == 1
        )

    asyncio.run(scenario())


def test_uncertain_api_click_persists_chat_stop(tmp_path, monkeypatch):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")

        async def uncertain(payload):
            raise TimeoutError()

        page = api_page(
            monkeypatch, "https://chatgpt.com/c/chat/", send_callback=uncertain
        )
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        with pytest.raises(BridgeError) as error:
            await browser.dispatch_prompt(page, "hello")
        assert error.value.kind == "ambiguous_send"
        assert "stop:https://chatgpt.com/c/chat" in store.status()["controls"]

    asyncio.run(scenario())


def test_navigation_during_final_checks_refuses_stopped_destination(
    tmp_path, monkeypatch
):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        store.stop("https://chatgpt.com/c/stopped")
        observations = 0

        async def navigate(page):
            nonlocal observations
            observations += 1
            if observations == 2:
                page.url = "https://chatgpt.com/c/stopped/"
            return Observation(
                page.url.rstrip("/"),
                "",
                "",
                verified=True,
                model="Astra",
                ambiguous=False,
            )

        page = api_page(
            monkeypatch, "https://chatgpt.com/c/allowed", observe_callback=navigate
        )
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        with pytest.raises(BridgeError) as error:
            await browser.dispatch_prompt(page, "hello")
        assert error.value.kind == "busy"
        assert not any(
            call.args[1].get("action") == "send-api"
            for call in page.evaluate.await_args_list
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "https://chatgpt.com.evil/c/chat",
        "http://chatgpt.com/c/chat",
        "https://chatgpt.com@evil/c/chat",
        "about:blank",
    ],
)
def test_api_dispatch_rejects_foreign_origin_without_click(tmp_path, url):
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        source = bridge()
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite3")),
        )
        page = SimpleNamespace(url=url, evaluate=AsyncMock())
        with pytest.raises(BridgeError) as error:
            await browser.dispatch_prompt(page, "hello")
        assert error.value.kind == "unsafe_origin"
        assert not any(
            call.args[1].get("action") == "send-api"
            for call in page.evaluate.await_args_list
        )
        source.readiness.assert_not_awaited()

    asyncio.run(scenario())


def test_verified_observation_chat_change_refuses_click(tmp_path, monkeypatch):
    import sys
    from dataclasses import asdict

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )

    async def scenario():
        source = bridge()
        source.STOP = "stop"
        source.visible = AsyncMock(return_value=False)
        store = Store(tmp_path / "state.sqlite3")
        store.stop("https://chatgpt.com/c/stopped")
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        page = SimpleNamespace(
            url="https://chatgpt.com/c/allowed",
            locator=lambda key: object(),
            evaluate=AsyncMock(
                side_effect=[
                    asdict(
                        Observation(
                            "https://chatgpt.com/c/allowed",
                            "turn",
                            "generation",
                            verified=True,
                            model="Astra",
                            ambiguous=False,
                        )
                    ),
                    asdict(
                        Observation(
                            "https://chatgpt.com/c/stopped",
                            "turn",
                            "generation",
                            verified=True,
                            model="Astra",
                            ambiguous=False,
                        )
                    ),
                ]
            ),
        )
        with pytest.raises(BridgeError) as error:
            await browser.dispatch_prompt(page, "hello")
        assert error.value.kind == "busy"
        assert not any(
            call.args[1].get("action") == "send-api"
            for call in page.evaluate.await_args_list
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "chat, model, effort, quota",
    [
        ("https://chatgpt.com/", "Astra", "", False),
        ("https://chatgpt.com/c/sol", "Sol", "Extra High", True),
    ],
)
def test_explicit_api_uses_ready_ui_without_failure_turn_and_allows_consented_sol_quota(
    tmp_path, monkeypatch, chat, model, effort, quota
):
    import sys
    from dataclasses import asdict

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )

    async def scenario():
        source = bridge()
        source.COMPOSER, source.STOP, source.SEND, source.ASSISTANT = (
            "composer",
            "stop",
            "send",
            "answers",
        )
        source.visible = AsyncMock(return_value=False)
        store = Store(tmp_path / "state.sqlite3")
        if quota:
            store.quota()
            store.opt_in_sol(chat)
        api_ready = Observation(
            chat,
            "",
            "",
            verified=True,
            model=model,
            effort=effort,
            quota=quota,
            draft=False,
            ambiguous=False,
        )
        recovery = api_ready if quota else Observation("", "", "")

        async def evaluate(script, payload):
            if payload.get("action") == "reply-baseline":
                return {"chat": page.url, "turns": []}
            if payload.get("action") == "prepare-api":
                return True
            if payload.get("action") == "send-api":
                raise BridgeError("fixture sent", 502, "fixture_sent")
            return asdict(
                api_ready if payload.get("action") == "observe-api" else recovery
            )

        composer = SimpleNamespace(
            count=AsyncMock(return_value=1),
            evaluate=AsyncMock(side_effect=["hello"] if quota else ["", "hello"]),
            fill=AsyncMock(),
        )
        answers = SimpleNamespace(count=AsyncMock(return_value=0))
        send = SimpleNamespace(
            wait_for=AsyncMock(),
            click=AsyncMock(
                side_effect=BridgeError("fixture sent", 502, "fixture_sent")
            ),
        )
        page = SimpleNamespace(
            url=chat,
            evaluate=AsyncMock(side_effect=evaluate),
            locator=lambda key: {
                "composer": composer,
                "stop": object(),
                "send": send,
                "answers": answers,
            }[key],
        )
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        if quota:
            with pytest.raises(BridgeError) as error:
                await browser.dispatch_prompt(page, "hello")
        else:
            with pytest.raises(BridgeError) as error:
                await browser.ask(page, "hello")
        assert error.value.kind == "fixture_sent"
        send.click.assert_not_awaited()
        assert (
            sum(
                call.args[1].get("action") == "send-api"
                for call in page.evaluate.await_args_list
            )
            == 1
        )
        assert not api_ready.safe_failure(), (
            "explicit API readiness must never authorize Guardian recovery"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "unsafe", ["draft", "challenge", "running", "ambiguous", "unverified", "home-sol"]
)
def test_explicit_api_snapshot_retains_safety_stops(tmp_path, monkeypatch, unsafe):
    import sys
    from dataclasses import asdict, replace

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )

    async def scenario():
        source = bridge()
        source.STOP = "stop"
        source.visible = AsyncMock(return_value=False)
        chat = (
            "https://chatgpt.com/"
            if unsafe == "home-sol"
            else "https://chatgpt.com/c/chat"
        )
        observation = Observation(
            chat, "", "", model="Astra", verified=True, ambiguous=False
        )
        observation = replace(
            observation,
            **(
                {"verified": False}
                if unsafe == "unverified"
                else {"model": "Sol", "effort": "Extra High"}
                if unsafe == "home-sol"
                else {unsafe: True}
            ),
        )
        store = Store(tmp_path / "state.sqlite3")
        if unsafe == "home-sol":
            with store.connect() as db:
                db.execute("INSERT INTO consent VALUES(?)", (chat,))
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        page = SimpleNamespace(
            url=chat,
            evaluate=AsyncMock(return_value=asdict(observation)),
            locator=lambda key: object(),
        )
        with pytest.raises(BridgeError) as error:
            await browser.dispatch_prompt(page, "hello")
        assert error.value.kind == (
            "sol_consent_required" if unsafe == "home-sol" else "busy"
        )
        assert not any(
            call.args[1].get("action") == "send-api"
            for call in page.evaluate.await_args_list
        )

    asyncio.run(scenario())


def test_blank_startup_navigates_without_changing_restored_tabs():
    async def scenario():
        blank = SimpleNamespace(url="about:blank", goto=AsyncMock())
        restored = SimpleNamespace(
            url="https://chatgpt.com/c/restored", goto=AsyncMock()
        )
        ctx = SimpleNamespace(pages=[blank])
        browser = adapter().create_browser(
            bridge(), SimpleNamespace(start=AsyncMock(return_value=ctx)), "managed"
        )
        await browser.start()
        blank.goto.assert_awaited_once()
        assert blank.goto.await_args.args[0] == "https://chatgpt.com/"
        ctx.pages = [blank, restored]
        await browser.start()
        restored.goto.assert_not_awaited()
        assert browser.status_page is restored

    asyncio.run(scenario())


def test_new_conversation_polls_loading_until_ready():
    async def scenario():
        source = bridge()
        source.readiness = AsyncMock(side_effect=["loading", "ready"])
        page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock())
        browser = adapter().create_browser(source, SimpleNamespace(), "managed")
        browser.ctx = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page))
        result = await browser.new_conversation()
        assert result.page is page
        assert source.readiness.await_count == 2
        page.close.assert_not_awaited()

    asyncio.run(scenario())


def test_attachment_only_draft_is_checked_before_composer_fill(tmp_path, monkeypatch):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    async def scenario():
        source = bridge()
        source.COMPOSER = "composer"
        composer = SimpleNamespace(fill=AsyncMock())

        async def attachment(page):
            return Observation(
                page.url,
                "",
                "",
                model="Astra",
                verified=True,
                ambiguous=False,
                draft=True,
            )

        page = api_page(
            monkeypatch, "https://chatgpt.com/c/chat", observe_callback=attachment
        )
        page.locator = lambda selector: composer
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite3")),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        with pytest.raises(BridgeError) as error:
            await browser.ask(page, "hello")
        assert error.value.kind == "busy"
        composer.fill.assert_not_awaited()

    asyncio.run(scenario())


def test_unconfigured_managed_api_never_mutates_composer():
    async def scenario():
        source = bridge()
        page = SimpleNamespace(url="https://chatgpt.com/", locator=AsyncMock())
        browser = adapter().create_browser(source, SimpleNamespace(), "managed")
        with pytest.raises(BridgeError) as error:
            await browser.ask(page, "hello")
        assert error.value.kind == "verified_contract_required"
        page.locator.assert_not_called()
        source.readiness.assert_not_awaited()

    asyncio.run(scenario())


def test_monitor_deadline_budget_keeps_observation_gaps_below_stability_limit(
    monkeypatch,
):
    async def scenario():
        source = bridge()
        browser = adapter().create_browser(source, SimpleNamespace(), "managed")
        clock = SimpleNamespace(now=0.0)
        clock.time = lambda: clock.now
        observations = []

        async def sleep(duration):
            clock.now += duration

        async def observe():
            observations.append(clock.now)
            clock.now += 1.2
            if len(observations) == 3:
                raise asyncio.CancelledError

        browser.monitor_once = observe
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "get_running_loop", lambda: clock)
            patch.setattr(asyncio, "sleep", sleep)
            with pytest.raises(asyncio.CancelledError):
                await browser.monitor()
        assert all(right - left <= 6 for left, right in pairwise(observations))

    asyncio.run(scenario())


def test_contended_api_guard_keeps_event_loop_responsive(tmp_path, monkeypatch):
    import sqlite3
    import time

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        blocker = sqlite3.connect(tmp_path / "state.sqlite3")
        blocker.execute("BEGIN IMMEDIATE")
        released = False

        def heartbeat():
            nonlocal released
            if not released:
                blocker.rollback()
                blocker.close()
                released = True

        page = api_page(monkeypatch, "https://chatgpt.com/c/chat")
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        started = time.monotonic()
        asyncio.get_running_loop().call_later(0.05, heartbeat)
        try:
            await asyncio.wait_for(browser.dispatch_prompt(page, "hello"), timeout=1)
            assert released
            assert time.monotonic() - started < 1
        finally:
            heartbeat()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["complete", "monitor"])
def test_database_status_io_does_not_stall_api_or_monitor(
    tmp_path, monkeypatch, operation
):
    import sqlite3
    import sys
    import threading
    import time
    from dataclasses import asdict

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )
    monkeypatch.setattr(
        BaseBrowser, "complete", AsyncMock(return_value="completed"), raising=False
    )
    store = Store(tmp_path / "state.sqlite")
    held = threading.Event()

    def holder():
        db = sqlite3.connect(store.path)
        try:
            db.execute("BEGIN EXCLUSIVE")
            held.set()
            time.sleep(0.25)
        finally:
            db.rollback()
            db.close()

    thread = threading.Thread(target=holder)
    thread.start()

    async def scenario():
        await asyncio.to_thread(held.wait)
        page = SimpleNamespace(
            url="https://chatgpt.com/c/test",
            evaluate=AsyncMock(
                return_value=asdict(Observation("https://chatgpt.com/c/test", "", ""))
            ),
        )
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(),
        )
        browser.ctx = SimpleNamespace(pages=[page])
        browser.status_page = page
        ticks = []
        keep = True

        async def heartbeat():
            while keep:
                ticks.append(1)
                await asyncio.sleep(0.01)

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        try:
            if operation == "complete":
                assert await browser.complete([], []) == "completed"
            else:
                await browser.monitor_once()
            assert len(ticks) >= 5
        finally:
            keep = False
            await pulse

    try:
        asyncio.run(scenario())
    finally:
        thread.join(timeout=2)


@pytest.mark.parametrize("blocked", ["sol-consent", "pending-send"])
def test_durable_api_controls_block_before_any_composer_mutation(
    tmp_path, monkeypatch, blocked
):
    import time

    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        chat = "https://chatgpt.com/c/chat"
        if blocked == "pending-send":
            reserved = Observation(
                chat,
                "turn",
                "generation",
                model="Astra",
                verified=True,
                status="Thinking failed",
                active=True,
                ambiguous=False,
            )
            assert store.reserve(reserved, time.time()) == "reserved"

        async def snapshot(page):
            return Observation(
                chat,
                "",
                "",
                model="Sol" if blocked == "sol-consent" else "Astra",
                effort="Extra High" if blocked == "sol-consent" else "",
                verified=True,
                ambiguous=False,
            )

        page = api_page(monkeypatch, chat, observe_callback=snapshot)
        composer = SimpleNamespace(
            count=AsyncMock(return_value=1),
            evaluate=AsyncMock(return_value=""),
            fill=AsyncMock(),
        )
        send = SimpleNamespace(wait_for=AsyncMock())
        source = bridge()
        source.COMPOSER, source.STOP, source.SEND, source.ASSISTANT = (
            "composer",
            "stop",
            "send",
            "answers",
        )
        source.visible = AsyncMock(return_value=False)
        page.locator = lambda selector: {
            "composer": composer,
            "send": send,
            "stop": object(),
            "answers": SimpleNamespace(count=AsyncMock(return_value=0)),
        }[selector]
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        with pytest.raises(BridgeError):
            await browser.ask(page, "hello")
        composer.fill.assert_not_awaited()
        assert not any(
            call.args[1].get("action") in ("prepare-api", "send-api")
            for call in page.evaluate.await_args_list
        )

    asyncio.run(scenario())


def test_human_typing_after_empty_snapshot_is_preserved(tmp_path):
    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, run

    async def scenario(page, dom):
        source = bridge()
        source.ASSISTANT = '.turn[data-author="assistant"]'
        await page.evaluate(
            "() => { document.querySelector('#send').onclick = () => window.clicked = true; }"
        )
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(Store(tmp_path / "state.sqlite3")),
            contract=CONTRACT,
        )
        original = page.evaluate
        actions = []

        async def intervening_human(script, payload=None):
            if isinstance(payload, dict):
                actions.append(payload.get("action"))
                if payload.get("action") == "prepare-api":
                    await original(
                        "document.querySelector('#composer').value = 'human draft'"
                    )
            return await original(script, payload)

        page.evaluate = intervening_human
        with pytest.raises(BridgeError) as error:
            await browser.ask(page, "hello")
        assert error.value.kind == "busy"
        assert actions == ["observe-api", "reply-baseline", "prepare-api"]
        assert await page.locator(CONTRACT.composer).input_value() == "human draft"
        assert not await original("Boolean(window.clicked)")

    run(scenario)


@pytest.mark.parametrize("action", ["prepare-api", "send-api"])
def test_atomic_api_quota_is_latched_before_other_chat(tmp_path, action):
    from agenttools_chatgpt.core import Guardian, Store
    from test_chatgpt_guardian_dom import CONTRACT, HTML, run

    async def scenario(page, dom):
        store = Store(tmp_path / "state.sqlite")
        source = bridge()
        source.ASSISTANT = '.turn[data-author="assistant"]'
        browser = adapter().create_browser(
            source,
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=CONTRACT,
        )
        original = page.evaluate
        await original(
            "() => { document.querySelector('#send').onclick = () => window.clicked = true; }"
        )

        async def quota_at_mutation(script, payload=None):
            if isinstance(payload, dict) and payload.get("action") == action:
                if action == "prepare-api":
                    await original("""() => { document.querySelector('#composer').oninput = () =>
                        document.body.insertAdjacentHTML('beforeend', '<div id=astra-quota>Limit</div>'); }""")
                else:
                    await original(
                        "document.body.insertAdjacentHTML('beforeend', '<div id=astra-quota>Limit</div>')"
                    )
            return await original(script, payload)

        page.evaluate = quota_at_mutation
        with pytest.raises(BridgeError):
            await browser.ask(page, "hello")
        assert "astra_quota" in Store(store.path).controls()
        assert not await original("Boolean(window.clicked)")
        other = await page.context.browser.new_page()
        await other.route(
            "**/*", lambda route: route.fulfill(body=HTML, content_type="text/html")
        )
        await other.goto("https://chatgpt.com/c/other")
        with pytest.raises(BridgeError) as error:
            await browser.ask(other, "second prompt")
        assert error.value.kind == "rate_limited"
        assert await other.locator(CONTRACT.composer).input_value() == ""
        assert await page.locator(CONTRACT.composer).input_value() == "hello"

    run(scenario)


def test_monitor_reuses_first_snapshots_to_preserve_continuous_stability(
    tmp_path, monkeypatch
):
    import sys
    from dataclasses import asdict

    from agenttools_chatgpt import core
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(Error=RuntimeError)
    )

    async def scenario():
        clock = SimpleNamespace(now=0.0)
        store = Store(tmp_path / "state.sqlite3")
        guardian = Guardian(store, apply=False)
        pages = []
        for index in range(3):
            chat = f"https://chatgpt.com/c/chat{index}"
            store.set_control("watch:" + chat, "1")
            observation = Observation(
                chat,
                "turn",
                "generation",
                model="Astra",
                verified=True,
                status="Thinking failed",
                active=True,
                ambiguous=False,
            )

            async def evaluate(script, payload, observation=observation):
                clock.now += 1.2
                return asdict(observation)

            pages.append(
                SimpleNamespace(url=chat, evaluate=AsyncMock(side_effect=evaluate))
            )
        results = []
        original = guardian.run_once

        async def ingest(observed_adapter, initial=None, observed_at=None):
            result = await original(
                observed_adapter, initial=initial, observed_at=observed_at
            )
            results.append(result)
            return result

        guardian.run_once = ingest
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=guardian,
            contract=Contract(verified_account_ui=True, send="send"),
        )
        browser.ctx = SimpleNamespace(pages=pages)
        browser.status_page = pages[0]
        with monkeypatch.context() as patch:
            patch.setattr(core.time, "monotonic", lambda: clock.now)
            for _ in range(8):
                await browser.monitor_once()
        assert "observe" in results, (
            "duplicate snapshots prevented a stable 20-second window"
        )
        assert all(page.evaluate.await_count == 8 for page in pages)

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["initial", "final"])
def test_api_human_challenge_without_chat_latches_global_stop(
    tmp_path, monkeypatch, stage
):
    from agenttools_chatgpt.browser import Contract
    from agenttools_chatgpt.core import Guardian, Observation, Store

    async def scenario():
        store = Store(tmp_path / "state.sqlite3")
        observations = 0

        async def snapshot(page):
            nonlocal observations
            observations += 1
            if stage == "initial" or observations == 2:
                return Observation("", "", "", challenge=True)
            return Observation(
                page.url, "", "", model="Astra", verified=True, ambiguous=False
            )

        page = api_page(
            monkeypatch, "https://chatgpt.com/c/chat", observe_callback=snapshot
        )
        browser = adapter().create_browser(
            bridge(),
            SimpleNamespace(),
            "managed",
            guardian=Guardian(store),
            contract=Contract(verified_account_ui=True, send="send"),
        )
        with pytest.raises(BridgeError):
            if stage == "initial":
                await browser.ask(page, "hello")
            else:
                await browser.dispatch_prompt(page, "hello")
        assert "stop:*" in store.status()["controls"]
        assert not any(
            call.args[1].get("action") in ("prepare-api", "send-api")
            for call in page.evaluate.await_args_list
        )

    asyncio.run(scenario())
