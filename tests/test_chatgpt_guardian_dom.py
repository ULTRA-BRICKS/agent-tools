"""Actual Chromium fixtures for UI structure, not live ChatGPT/account testing."""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt.browser import BrowserLifecycle, Contract, PlaywrightAdapter

CONTRACT = Contract(
    True,
    "#conversation",
    ".turn",
    "data-id",
    "data-generation",
    "data-author",
    ".thinking-status",
    "data-active",
    "true",
    ".body",
    "#composer",
    "#send",
    "#model",
    "#effort",
    "#logged-in",
    "#running",
    "#challenge",
    "#attachment",
    "#astra-quota",
)
HTML = """<title>ChatGPT fixture</title><div id="logged-in">Account</div>
<div id="model">Astra</div><div id="effort">Extra High</div>
<div id="conversation"><article class="turn" data-id="old" data-generation="g0" data-author="assistant">
<div class="thinking-status" role="status" data-active="true">Thinking failed</div><div class="body">Old answer</div></article>
<article class="turn" data-id="latest" data-generation="g1" data-author="assistant">
<div class="thinking-status" role="status" data-active="true">Thinking failed</div><div class="body">Answer</div></article></div>
<textarea id="composer"></textarea><button id="send">Send</button>"""


async def browser_case(callback):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=shutil.which("google-chrome")
        )
        try:
            page = await browser.new_page()
            await page.route(
                "**/*", lambda route: route.fulfill(body=HTML, content_type="text/html")
            )
            await page.goto("https://chatgpt.com/c/fixture")
            await callback(page, PlaywrightAdapter(page, CONTRACT))
        finally:
            await browser.close()


def run(callback):
    pytest.importorskip("playwright.async_api")
    if not shutil.which("google-chrome"):
        pytest.skip("Fixture browser requires installed Chrome; no downloads in tests")
    asyncio.run(browser_case(callback))


def test_mid_input_quota_blocks_second_recovery_in_same_cycle(tmp_path):
    import time

    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=True)
        observed = await adapter.observe()
        now = time.monotonic()
        guardian.stability[observed.chat] = observed, now - 21
        guardian.last_seen[observed.chat] = now
        await page.evaluate("""() => {
            document.querySelector('#composer').oninput = () =>
                document.body.insertAdjacentHTML('beforeend', '<div id=astra-quota>Limit</div>');
            document.querySelector('#send').onclick = () => window.clicked = true;
        }""")
        assert await guardian.run_once(adapter) == "uncertain-send"
        assert "astra_quota" in Store(store.path).controls()
        assert store.status()["attempts"][0]["state"] == "uncertain"
        assert not await page.evaluate("Boolean(window.clicked)")
        other = await page.context.browser.new_page()
        await other.route(
            "**/*", lambda route: route.fulfill(body=HTML, content_type="text/html")
        )
        await other.goto("https://chatgpt.com/c/second")
        second = PlaywrightAdapter(other, CONTRACT)
        observation = await second.observe()
        guardian.stability[observation.chat] = observation, now - 21
        guardian.last_seen[observation.chat] = time.monotonic()
        assert await guardian.run_once(second) == "astra-quota"
        assert len(store.status()["attempts"]) == 1
        assert await other.locator("#composer").input_value() == ""

    run(case)


def test_latest_status_is_unique_active_ui_only():
    async def case(page, adapter):
        assert (await adapter.observe()).safe_failure()
        await (
            page.locator(".turn")
            .last.locator(".thinking-status")
            .evaluate("el=>el.removeAttribute('data-active')")
        )
        assert not (await adapter.observe()).safe_failure()
        await (
            page.locator(".turn")
            .last.locator(".thinking-status")
            .evaluate("(el)=>el.remove()")
        )
        await (
            page.locator(".turn")
            .last.locator(".body")
            .evaluate("el=>el.textContent='Thinking failed'")
        )
        assert not (await adapter.observe()).safe_failure()

    run(case)


@pytest.mark.parametrize("structure", ["missing", "drifted", "duplicate"])
def test_unknown_body_structure_never_authorizes_recovery(structure):
    async def case(page, adapter):
        await page.evaluate(
            """kind => {
          const turn = document.querySelector('.turn:last-child');
          const body = turn.querySelector('.body');
          if (kind === 'missing') body.remove();
          if (kind === 'drifted') {
            body.className = 'new-body';
            body.append(turn.querySelector('.thinking-status'));
          }
          if (kind === 'duplicate') turn.append(body.cloneNode(true));
          document.querySelector('#send').onclick = () => window.clicked = true;
        }""",
            structure,
        )
        observed = await adapter.observe()
        assert not observed.safe_failure()
        assert not await adapter.send(observed)
        assert not await page.evaluate("Boolean(window.clicked)")

    run(case)


@pytest.mark.parametrize("path", ["recovery", "api"])
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
def test_browser_execution_retains_dispatch_lock_until_finished(
    tmp_path, path, interruption
):
    import time
    from types import SimpleNamespace

    from agenttools_chatgpt.core import Guardian, Store
    from agenttools_chatgpt.ha_bridge import create_browser
    from test_chatgpt_guardian_ha import BridgeError, bridge

    async def case(page, adapter):
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=True)
        entered = asyncio.Event()
        await page.expose_function("enteredHandler", entered.set)
        await page.evaluate("""() => {
          window.clicked = false;
          document.querySelector('#send').onclick = () => window.clicked = true;
          const slow = () => {
            window.enteredHandler();
            const until = performance.now() + 6500;
            while (performance.now() < until) {}
          };
          document.querySelector('#composer').addEventListener('input', slow);
        }""")
        if path == "recovery":
            obs = await adapter.observe()
            now = time.monotonic()
            guardian.stability[obs.chat] = (obs, now - 21)
            guardian.last_seen[obs.chat] = now
            operation = guardian.run_once(adapter)
        else:
            # send-api is synchronous too: a DOM getter can stall its final check.
            await page.evaluate("""() => {
              document.querySelector('#composer').value = 'hello';
              Object.defineProperty(document.querySelector('#send'), 'disabled', {get() {
                window.enteredHandler();
                const until = performance.now() + 6500;
                while (performance.now() < until) {}
                return false;
              }});
            }""")
            browser = create_browser(
                bridge(),
                SimpleNamespace(),
                "managed",
                guardian=guardian,
                contract=CONTRACT,
            )
            operation = browser.dispatch_prompt(page, "hello")
        task = asyncio.create_task(operation)
        await asyncio.wait_for(entered.wait(), timeout=3)
        if interruption == "cancel":
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
        else:
            await asyncio.sleep(5.2)
        stop = asyncio.create_task(asyncio.to_thread(store.stop))
        await asyncio.sleep(0.1)
        assert not stop.done(), "stop committed while Chromium could still click"
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError, BridgeError)):
            await task
        await stop
        clicked_at_stop = await page.evaluate("window.clicked")
        await asyncio.sleep(0.1)
        assert await page.evaluate("window.clicked") == clicked_at_stop
        assert not clicked_at_stop, "overdue renderer must not click"
        if path == "recovery":
            assert store.status()["attempts"][0]["state"] == "uncertain"
        else:
            assert "stop:" + page.url in store.controls()

    run(case)


@pytest.mark.parametrize("action", ["send", "send-api", "prepare-api"])
def test_uncertain_transport_quarantines_dispatch_with_real_chromium(tmp_path, action):
    import multiprocessing
    import os

    def child(connection):
        from agenttools_chatgpt.core import Store, protected_dispatch
        from playwright.async_api import Error as BrowserError

        results = {}

        async def case(page, adapter):
            store = Store(tmp_path / "state.sqlite")
            entered = asyncio.Event()
            await page.expose_function("enteredHandler", entered.set)
            await page.evaluate("""() => {
              window.clicked = false;
              const slow = () => {
                window.enteredHandler();
                const until = performance.now() + 1000;
                while (performance.now() < until) {}
              };
              document.querySelector('#composer').addEventListener('input', slow);
              document.querySelector('#send').onclick = () => window.clicked = true;
            }""")
            if action == "send":
                expected = await adapter.observe()
            else:
                expected = await adapter.observe_api()
                if action == "send-api":
                    await page.evaluate("""() => {
                      document.querySelector('#composer').value = 'hello';
                      Object.defineProperty(document.querySelector('#send'), 'disabled', {get() {
                        window.enteredHandler();
                        const until = performance.now() + 1000;
                        while (performance.now() < until) {}
                        return false;
                      }});
                    }""")
            original = page.evaluate
            executions = []

            async def lose_transport(script, payload):
                executions.append(asyncio.create_task(original(script, payload)))
                await entered.wait()
                raise BrowserError("transport lost after renderer started")

            page.evaluate = lose_transport

            async def dispatch():
                async with store.async_dispatch_api(page.url):
                    operation = (
                        adapter.send(expected)
                        if action == "send"
                        else (
                            adapter.send_api(expected, "hello")
                            if action == "send-api"
                            else adapter.prepare_api(expected, "hello")
                        )
                    )
                    await protected_dispatch(operation)

            pending = asyncio.create_task(dispatch())
            await asyncio.wait_for(entered.wait(), timeout=3)
            await asyncio.sleep(0.05)
            pending.cancel()
            await asyncio.sleep(0.05)
            pending.cancel()
            stop = asyncio.create_task(asyncio.to_thread(store.stop))
            await asyncio.sleep(0.1)
            results["stop_committed_during_execution"] = stop.done()
            await asyncio.gather(*executions)
            results["clicked"] = await original("window.clicked")
            results["quarantined"] = not pending.done() and not stop.done()
            if pending.done():
                await asyncio.gather(pending, return_exceptions=True)

        async def scenario():
            # Close this child's owned browser before releasing its SQLite lock
            # through process exit; a transport error alone cannot prove shutdown.
            await browser_case(case)
            connection.send(results)
            connection.close()
            os._exit(0)

        asyncio.run(scenario())

    pytest.importorskip("playwright.async_api")
    if not shutil.which("google-chrome"):
        pytest.skip("Fixture browser requires installed Chrome")
    context = multiprocessing.get_context("fork")
    parent, child_connection = context.Pipe(duplex=False)
    process = context.Process(target=child, args=(child_connection,))
    process.start()
    child_connection.close()
    try:
        assert parent.poll(15), "isolated Chromium regression did not return proof"
        result = parent.recv()
        process.join(timeout=3)
        assert process.exitcode == 0
        assert not result["stop_committed_during_execution"]
        assert result["quarantined"]
        assert result["clicked"] == (action != "prepare-api")
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)


@pytest.mark.parametrize("action", ["send", "send-api", "prepare-api"])
def test_queued_browser_mutation_cannot_renew_python_deadline(action):
    async def case(page, adapter):
        expected = (
            await adapter.observe() if action == "send" else await adapter.observe_api()
        )
        if action == "send-api":
            await page.evaluate("document.querySelector('#composer').value = 'hello'")
        entered = asyncio.Event()
        await page.expose_function("enteredHandler", entered.set)
        busy = asyncio.create_task(
            page.evaluate("""() => {
          window.clicked = false;
          document.querySelector('#send').onclick = () => window.clicked = true;
          window.enteredHandler();
          const until = performance.now() + 6500;
          while (performance.now() < until) {}
        }""")
        )
        await asyncio.wait_for(entered.wait(), timeout=3)
        result = await (
            adapter.send(expected)
            if action == "send"
            else (
                adapter.send_api(expected, "hello")
                if action == "send-api"
                else adapter.prepare_api(expected, "hello")
            )
        )
        await busy
        assert not result
        assert not await page.evaluate("window.clicked")
        assert await page.locator("#composer").input_value() == (
            "hello" if action == "send-api" else ""
        )

    run(case)


def test_old_failure_prose_duplicate_and_unknown_model_never_send():
    async def case(page, adapter):
        await (
            page.locator(".turn")
            .last.locator(".thinking-status")
            .evaluate("el=>el.parentElement.querySelector('.body').append(el)")
        )
        assert not (await adapter.observe()).safe_failure()
        assert not await adapter.send(await adapter.observe())
        await page.set_content(HTML)
        await page.locator("#model").evaluate("el=>el.textContent='GPT-5 Pro'")
        assert not (await adapter.observe()).safe_failure()
        await page.set_content(HTML)
        await page.locator(".turn").last.evaluate(
            "el=>el.setAttribute('data-id','old')"
        )
        assert not (await adapter.observe()).safe_failure()

    run(case)


def test_draft_challenge_running_and_stale_snapshot_prevent_click():
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.locator("#composer").fill(" ")
        assert (await adapter.observe()).draft
        assert not await adapter.send(expected)
        await page.locator("#composer").fill("")
        await page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<div id=running>Running</div>')"
        )
        assert not await adapter.send(expected)
        await page.locator("#running").evaluate("(el)=>el.remove()")
        await page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<div role=dialog>Human check</div>')"
        )
        assert (await adapter.observe()).challenge
        assert not await adapter.send(expected)

    run(case)


def test_synchronous_input_handler_transition_prevents_send():
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate("""document.querySelector('#composer').addEventListener('input',()=>{
          document.body.insertAdjacentHTML('beforeend','<div id=running>Running</div>')});
          document.querySelector('#send').addEventListener('click',()=>window.clicked=true)""")
        assert not await adapter.send(expected)
        assert not await page.evaluate("Boolean(window.clicked)")

    run(case)


def test_success_requires_ui_user_ack_and_never_retries_ambiguous_click():
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate("""document.querySelector('#send').addEventListener('click',()=>{
          document.querySelector('#conversation').insertAdjacentHTML('beforeend',
          '<article class=turn data-id=user1 data-generation=g2 data-author=user><div class=body>го</div></article>')})""")
        assert await adapter.send(expected)
        assert not await adapter.send(expected)

    run(case)


def test_profile_isolation_and_refusal_of_existing_home(tmp_path):
    home = BrowserLifecycle(tmp_path / "profiles", "home")
    glide = BrowserLifecycle(tmp_path / "profiles", "glide.vc")
    assert home.profile != glide.profile
    home.acquire()
    try:
        with pytest.raises(BlockingIOError):
            BrowserLifecycle(tmp_path / "profiles", "home").acquire()
    finally:
        asyncio.run(home.close())
    foreign = tmp_path / "foreign" / "home"
    foreign.mkdir(parents=True)
    with pytest.raises(ValueError, match="unowned"):
        BrowserLifecycle(tmp_path / "foreign", "home").acquire()


def test_quota_and_challenge_on_homepage_are_account_signals():
    async def case(page, adapter):
        await page.goto("https://chatgpt.com/")
        await page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<div id=astra-quota>Astra limit</div><div role=dialog>Human</div>')"
        )
        observed = await adapter.observe()
        assert observed.chat == ""
        assert observed.quota and observed.challenge
        assert not observed.safe_failure()

    run(case)


def test_api_evidence_permits_verified_empty_conversation_without_recovery():
    async def case(page, adapter):
        await page.goto("https://chatgpt.com/")
        await page.locator("#conversation").evaluate('el=>el.innerHTML=""')
        observed = await adapter.observe_api()
        assert observed.chat == "https://chatgpt.com/"
        assert observed.verified and not observed.ambiguous
        assert not observed.safe_failure()

    run(case)


def test_ack_allows_immediate_assistant_placeholder():
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate("""document.querySelector('#send').addEventListener('click',()=>{
          document.querySelector('#conversation').insertAdjacentHTML('beforeend',
          '<article class=turn data-id=user1 data-generation=g2 data-author=user><div class=body>го</div></article>'+
          '<article class=turn data-id=assistant2 data-generation=g3 data-author=assistant><div class=body>Working</div></article>')})""")
        assert await adapter.send(expected)

    run(case)


def test_verified_sol_can_dispatch_with_astra_quota_banner():
    async def case(page, adapter):
        await page.locator("#model").evaluate("el=>el.textContent='Sol'")
        await page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<div id=astra-quota>Astra limit</div>')"
        )
        expected = await adapter.observe()
        assert expected.verified and expected.quota and expected.model == "Sol"
        await page.evaluate("""document.querySelector('#send').addEventListener('click',()=>{
          document.querySelector('#conversation').insertAdjacentHTML('beforeend',
          '<article class=turn data-id=user1 data-generation=g2 data-author=user><div class=body>го</div></article>')})""")
        assert await adapter.send(expected)

    run(case)


@pytest.mark.parametrize(
    "mutation",
    [
        "history.pushState({}, '', '/c/different')",
        "document.querySelector('#composer').value=''",
    ],
)
def test_input_mutations_cannot_change_chat_or_clear_owned_text(mutation):
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate(
            """mutation=>{
            document.querySelector('#composer').addEventListener('input',()=>eval(mutation));
            document.querySelector('#send').addEventListener('click',()=>window.clicked=true)
        }""",
            mutation,
        )
        assert not await adapter.send(expected)
        assert not await page.evaluate("Boolean(window.clicked)")

    run(case)


def test_draft_is_evidence_even_when_model_disappears():
    async def case(page, adapter):
        await page.locator("#composer").fill("human draft")
        await page.locator("#model").evaluate("el=>el.remove()")
        observed = await adapter.observe()
        assert observed.draft and not observed.safe_failure()

    run(case)


def test_teardown_attempts_driver_stop_and_retains_owner_on_failure(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    async def case():
        lifecycle = BrowserLifecycle(tmp_path / "profiles", "home")
        lifecycle.context = SimpleNamespace(
            close=AsyncMock(side_effect=RuntimeError("close failed"))
        )
        driver = SimpleNamespace(stop=AsyncMock())
        lifecycle.playwright = driver
        lock = Mock()
        lifecycle.lock = lock
        with pytest.raises(RuntimeError, match="close failed"):
            await lifecycle.close()
        driver.stop.assert_awaited_once()
        lock.close.assert_not_called()

    asyncio.run(case())


def test_api_native_click_cannot_wait_for_a_different_model():
    async def case(page, adapter):
        await page.locator("#composer").fill("explicit prompt")
        expected = await adapter.observe_api(owned_draft="explicit prompt")
        await page.locator("#send").evaluate("el=>el.disabled=true")
        await page.evaluate("""document.querySelector('#send').addEventListener('click',()=>window.clicked=true);
            setTimeout(()=>{document.querySelector('#model').textContent='Sol';document.querySelector('#send').disabled=false},50)""")
        assert not await adapter.send_api(expected, "explicit prompt")
        await asyncio.sleep(0.08)
        assert not await page.evaluate("Boolean(window.clicked)")
        assert not await adapter.send_api(expected, "explicit prompt")

    run(case)


@pytest.mark.parametrize(
    "mutation",
    [
        "document.querySelector('#composer').outerHTML='<textarea id=composer></textarea>'",
        "document.querySelectorAll('[role=status]').forEach(el=>el.style.opacity='0')",
        "document.querySelector('#conversation').style.opacity='0'",
    ],
)
def test_recovery_rejects_replaced_composer_and_transparent_failure(mutation):
    async def case(page, adapter):
        expected = await adapter.observe()
        if "outerHTML" in mutation:
            await page.evaluate(
                "mutation=>document.querySelector('#composer').addEventListener('input',()=>eval(mutation))",
                mutation,
            )
        else:
            await page.evaluate(mutation)
            expected = await adapter.observe()
            assert not expected.safe_failure()
        await page.evaluate(
            "document.querySelector('#send').addEventListener('click',()=>window.clicked=true)"
        )
        assert not await adapter.send(expected)
        assert not await page.evaluate("Boolean(window.clicked)")

    run(case)


def test_api_prepare_atomically_preserves_new_human_draft():
    async def case(page, adapter):
        expected = await adapter.observe_api()
        await page.locator("#composer").fill("human typed after observation")
        assert not await adapter.prepare_api(expected, "API prompt")
        assert (
            await page.locator("#composer").input_value()
            == "human typed after observation"
        )

    run(case)


def test_api_prepare_uses_calibrated_composer_and_rejects_input_replacement():
    async def case(page, adapter):
        await page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<textarea id=legacy></textarea>')"
        )
        expected = await adapter.observe_api()
        assert await adapter.prepare_api(expected, "API prompt")
        assert await page.locator("#legacy").input_value() == ""
        assert await page.locator("#composer").input_value() == "API prompt"
        await page.locator("#composer").fill("")
        await page.evaluate(
            "document.querySelector('#composer').addEventListener('input',()=>document.querySelector('#composer').outerHTML='<textarea id=composer></textarea>')"
        )
        assert not await adapter.prepare_api(expected, "another prompt")

    run(case)
