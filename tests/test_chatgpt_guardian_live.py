"""Sanitized observed ChatGPT DOM in isolated Chromium, never a live account."""

import asyncio
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt.urls import canonical_chat

PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_observed_project_url_canonicalizes():
    assert (
        canonical_chat(PROJECT)
        == "https://chatgpt.com/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )


def test_project_id_requires_uuid():
    with pytest.raises(ValueError):
        canonical_chat(PROJECT.rsplit("/", 1)[0] + "/bad_id")


@pytest.mark.parametrize(
    "raw",
    [
        PROJECT + "?x=1",
        PROJECT + "#x",
        PROJECT.replace("chatgpt.com", "chatgpt.com.evil"),
        PROJECT.replace("https:", "http:"),
        PROJECT.replace("//chatgpt", "//user@chatgpt"),
        PROJECT.replace("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bad-project"),
    ],
)
def test_project_url_rejects_lookalikes(raw):
    with pytest.raises(ValueError):
        canonical_chat(raw)


HTML = """<meta charset="utf-8"><title>ChatGPT fixture</title>
<button aria-label="Open profile menu">Account</button>
<button aria-label="Select ChatGPT model" data-codex-intelligence-trigger="true"
 data-selected-reasoning-effort="medium" onclick="document.querySelector('#models').hidden=false">Pro</button>
<div id="models" role="menu" hidden><div role="menuitemradio" aria-checked="true"
 data-model-selected="true">GPT-6</div><div role="menuitemradio" aria-checked="false">GPT-5.6 Sol</div></div>
<main><div data-chatgpt-conversation-selection-target="true" data-thread-find-target="conversation">
<div data-turn-key="11111111-1111-4111-8111-111111111111"><div data-talvt-turn-state="cancelled">
<div class="group/activity-header"><span>Thinking failed</span></div></div></div>
</div></main>
<div contenteditable="true" data-composer-markdown role="textbox" aria-label="Ask ChatGPT"><p data-empty-paragraph="true"><br></p></div>
<button aria-label="Start Voice">Voice</button>
<script>
document.addEventListener('keydown', e=>{if(e.key==='Escape')document.querySelector('#models').hidden=true});
document.querySelector('[contenteditable]').oninput=()=>{
 if(!document.querySelector('[aria-label="Send"]')){
 const send=document.createElement('button');send.ariaLabel='Send';send.textContent='Send';
 send.onclick=()=>{window.clicks=(window.clicks||0)+1;
 document.querySelector('[data-thread-find-target]').insertAdjacentHTML('beforeend',
 '<div data-turn-key="22222222-2222-4222-8222-222222222222"><div data-chatgpt-search-unit-key="message:user" data-chatgpt-search-message-ids="22222222-2222-4222-8222-222222222222"><div data-user-message-bubble="true">го</div></div></div>')};
 document.body.append(send)}};
</script>"""

# Both panels have CSS boxes even when aria-hidden excludes the inactive panel.
MENU_HTML = (
    HTML.replace(
        '<div id="models" role="menu" hidden><div role="menuitemradio" aria-checked="true"\n data-model-selected="true">GPT-6</div><div role="menuitemradio" aria-checked="false">GPT-5.6 Sol</div></div>',
        """<div id="models" role="menu" hidden>
    <div id="simple"><button role="menuitem" aria-label="Select model">Select model</button>
    <button role="menuitem" aria-label="Power" data-reasoning-slider="true" aria-keyshortcuts="ArrowLeft ArrowRight">Power</button>
    <span role="status">Pro, 5 of 5.</span></div>
    <div id="full" aria-hidden="true"><button role="menuitemradio" aria-checked="true" data-model-selected="true">GPT-6</button>
    <button role="menuitemradio" aria-checked="false" data-model-selected="false">GPT-5.6 Sol</button></div></div>""",
    )
    + """<script>
window.model='GPT-6';window.effort='Pro';window.switches=0;
const trigger=document.querySelector('[data-codex-intelligence-trigger]');
const full=document.querySelector('#full'), simple=document.querySelector('#simple');
trigger.onclick=()=>{document.querySelector('#models').hidden=false;trigger.textContent='Thinking effort';};
document.querySelector('[aria-label="Select model"]').onclick=()=>{simple.setAttribute('aria-hidden','true');full.setAttribute('aria-hidden','false');};
for(const radio of full.children)radio.onclick=()=>{
 if(window.model!==radio.textContent)window.switches++;
 window.model=radio.textContent;
 for(const other of full.children){other.setAttribute('aria-checked',String(other===radio));other.setAttribute('data-model-selected',String(other===radio));}
 full.setAttribute('aria-hidden','true');simple.setAttribute('aria-hidden','false');
};
document.querySelector('[aria-label="Power"]').onkeydown=e=>{
 if(document.activeElement===e.currentTarget&&e.key==='ArrowLeft'&&e.keyCode===37&&window.effort==='Pro'){window.effort='Extra High';document.querySelector('[role=status]').textContent='Extra High, 4 of 5.';trigger.setAttribute('data-selected-reasoning-effort','max');}
};
document.addEventListener('keydown', e=>{if(e.key==='Escape'){
 full.setAttribute('aria-hidden','true');simple.setAttribute('aria-hidden','false');
 setTimeout(()=>{trigger.textContent=(window.model==='GPT-5.6 Sol'?'5.6 ':'')+window.effort;},40);
}});
</script>"""
)


def test_aria_hidden_panel_and_stable_closed_model_signature():
    async def case(page, adapter):
        observed = await adapter.observe()
        assert observed.model == "Astra"
        assert observed.verified
        assert observed.safe_failure()
        assert (
            await page.locator("[data-codex-intelligence-trigger]").text_content()
            == "Pro"
        )
        assert await page.evaluate("window.switches") == 0

    run(case, html=MENU_HTML)


@pytest.mark.parametrize("model", ["Astra", "Sol"])
def test_hidden_measurement_does_not_replace_rendered_model_label(model):
    html = MENU_HTML.replace(
        "trigger.textContent=(window.model==='GPT-5.6 Sol'?'5.6 ':'')+window.effort;",
        "trigger.innerHTML='<span aria-hidden=\"true\" style=\"display:none\">Thinking effort</span><span>'+(window.model==='GPT-5.6 Sol'?'5.6 ':'')+window.effort+'</span>';",
    )

    async def case(page, adapter):
        if model == "Sol":
            await page.evaluate("""() => {
              window.model='GPT-5.6 Sol';window.effort='Extra High';
              for(const radio of document.querySelector('#full').children){
                const selected=radio.textContent===window.model;
                radio.setAttribute('aria-checked',String(selected));radio.setAttribute('data-model-selected',String(selected));
              }
              document.querySelector('[role=status]').textContent='Extra High, 4 of 5.';
            }""")
        observed = await adapter.observe()
        trigger = page.locator("[data-codex-intelligence-trigger]")
        label = "Pro" if model == "Astra" else "5.6 Extra High"
        assert await trigger.inner_text() == label
        assert await trigger.text_content() == "Thinking effort" + label
        assert observed.verified and observed.model == model
        assert observed.effort == ("" if model == "Astra" else "Extra High")

    run(case, html=html)


@pytest.mark.parametrize("state", ["running", "draft", "manual-stop", "complete"])
def test_nonrecoverable_latest_state_never_probes_model_menu(tmp_path, state):
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        await page.evaluate(
            """state => {
          window.probeClicks=0;
          document.querySelector('[data-codex-intelligence-trigger]').addEventListener('click',()=>window.probeClicks++);
          const root=document.querySelector('[data-thread-find-target]');
          const latest=root.firstElementChild.cloneNode(true);
          latest.setAttribute('data-turn-key','33333333-3333-4333-8333-333333333333');root.append(latest);
          if(state==='running')latest.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','in_progress');
          if(state==='draft')document.querySelector('[contenteditable]').textContent='human draft';
          if(state==='manual-stop')latest.querySelector('span').textContent='Stopped thinking';
          if(state==='complete')latest.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','complete');
        }""",
            state,
        )
        observed = await adapter.observe()
        assert await page.evaluate("window.probeClicks") == 0
        assert observed.running == (state == "running")
        store = Store(tmp_path / "safety.sqlite")
        guardian = Guardian(store, apply=True)
        await guardian.run_once(adapter, initial=observed, observed_at=time.monotonic())
        if state == "draft":
            assert store.controls()["stop:" + observed.chat] == "human draft"

    run(case, html=MENU_HTML)


def test_durable_watch_controls_refresh_pages_and_restart(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agenttools_chatgpt import live
    from agenttools_chatgpt.core import Store

    async def case():
        store = Store(tmp_path / "watches.sqlite", account="glide.vc")
        page = SimpleNamespace(url=PROJECT, is_closed=lambda: False)
        context = SimpleNamespace(pages=[page], new_page=AsyncMock())
        chat = canonical_chat(PROJECT)
        other = "https://chatgpt.com/c/cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        store.enroll(chat)
        originals = {chat: PROJECT}
        assert await live.watched_pages(context, store, originals) == {chat: [page]}
        store.set_control("watch:" + other, "1")
        pages = await live.watched_pages(context, store, originals)
        assert set(pages) == {chat, other}
        assert pages[other] == []
        assert len(context.pages) == 1
        store.set_control("watch:" + chat, "0")
        restarted = Store(store.path, account="glide.vc")
        assert set(await live.watched_pages(context, restarted, {})) == {other}
        assert set(await live.watched_pages(context, restarted, originals)) == {other}
        assert len(context.pages) == 1
        context.new_page.assert_not_called()

    asyncio.run(case())


@pytest.mark.parametrize(
    "blocked",
    ["", "observe", "consent", "running", "draft", "stopped", "unwatch", "unresolved"],
)
def test_live_cycle_selects_consented_sol_only_when_armed(tmp_path, blocked):
    from agenttools_chatgpt import live
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "fallback.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat, blocked != "consent")
        store.quota()
        if blocked == "running":
            await page.evaluate(
                "document.body.insertAdjacentHTML('beforeend','<button aria-label=Stop>Stop</button>')"
            )
        if blocked == "draft":
            await page.locator("[contenteditable]").fill("human draft")
        if blocked == "stopped":
            store.stop(chat)
        if blocked == "unwatch":
            store.set_control("watch:" + chat, "0")
        if blocked == "unresolved":
            with store.connect() as db:
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,?,?,?)",
                    (chat, "old", "old", time.time(), "uncertain"),
                )
        guardian = Guardian(store, apply=blocked != "observe")
        observed = await adapter.observe()
        result = await live.recover_live(guardian, adapter, observed, time.monotonic())
        assert await page.evaluate("window.switches") == (0 if blocked else 1)
        assert not await page.evaluate("Boolean(window.clicks)")
        if not blocked:
            assert result == "stabilizing"
            final = guardian.stability[chat][0]
            assert (
                final.model == "Sol" and final.effort == "Extra High" and final.verified
            )

    run(case, html=MENU_HTML)


def test_sol_own_quota_stops_durably_without_switch_or_send(tmp_path):
    from agenttools_chatgpt import live
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "sol-quota.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat)
        store.quota()
        assert await adapter.select_sol(store, chat)
        await page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<div role=alert>GPT-5.6 Sol usage limit reached</div>')"
        )
        observed = await adapter.observe()
        guardian = Guardian(store, apply=True)
        assert (
            await live.recover_live(guardian, adapter, observed, time.monotonic())
            == "unsafe-or-not-active-failure"
        )
        assert (
            Store(store.path, account="glide.vc").controls()["stop:" + chat]
            == "Sol quota exhausted"
        )
        assert not await page.evaluate("Boolean(window.clicks)")
        assert await page.evaluate("window.switches") == 1

    run(case, html=MENU_HTML)


def test_unconsented_fallback_preserves_cached_model_probe(tmp_path):
    from agenttools_chatgpt import live
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        store = Store(tmp_path / "cache.sqlite", account="glide.vc")
        chat = canonical_chat(PROJECT)
        store.enroll(chat)
        store.quota()
        observed = await adapter.observe()
        probe = adapter.probe
        await live.recover_live(
            Guardian(store, apply=True), adapter, observed, time.monotonic()
        )
        assert adapter.probe is probe

    run(case, html=MENU_HTML)


@pytest.mark.parametrize("state", ["running", "draft", "manual-stop", "complete"])
def test_send_reprobe_does_not_open_menu_after_safety_changes(state):
    async def case(page, adapter):
        observed = await adapter.observe()
        await page.evaluate(
            """state => {
          window.probeClicks=0;
          document.querySelector('[data-codex-intelligence-trigger]').addEventListener('click',()=>window.probeClicks++);
          if(state==='running')document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','in_progress');
          if(state==='draft')document.querySelector('[contenteditable]').textContent='human draft';
          if(state==='manual-stop')document.querySelector('[class="group/activity-header"] span').textContent='Stopped thinking';
          if(state==='complete')document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','complete');
        }""",
            state,
        )
        assert not await adapter.send(observed)
        assert await page.evaluate("window.probeClicks") == 0
        assert not await page.evaluate("Boolean(window.clicks)")

    run(case, html=MENU_HTML)


@pytest.mark.parametrize("state", ["manual-stop", "complete"])
def test_sol_selection_aborts_if_failure_finishes_between_steps(tmp_path, state):
    from agenttools_chatgpt.core import Store

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "selection-race.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat)
        store.quota()
        await page.evaluate(
            """state => {
          document.querySelector('[aria-label="Select model"]').addEventListener('click',()=>{
            if(state==='manual-stop')document.querySelector('[class="group/activity-header"] span').textContent='Stopped thinking';
            else document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','complete');
          });
        }""",
            state,
        )
        assert not await adapter.select_sol(store, chat)
        assert await page.evaluate("window.switches") == 0

    run(case, html=MENU_HTML)


def test_historical_running_and_nested_wrappers_send_latest(tmp_path):
    async def case(page, adapter):
        await page.evaluate("""() => {
          const root=document.querySelector('[data-thread-find-target]');
          root.insertAdjacentHTML('afterbegin','<div><div data-turn-key="33333333-3333-4333-8333-333333333333"><div data-talvt-turn-state="in_progress">Historical</div></div></div>');
          const latest=root.lastElementChild;const wrapper=document.createElement('div');latest.replaceWith(wrapper);wrapper.append(latest);
        }""")
        observed = await adapter.observe()
        assert not observed.running
        assert observed.safe_failure()
        assert await adapter.send(observed)
        assert await page.evaluate("window.clicks") == 1

    run(case)


def test_closed_trigger_transient_signature_without_hidden_panel():
    async def case(page, adapter):
        await page.evaluate("""() => {
          const trigger=document.querySelector('[data-codex-intelligence-trigger]');
          trigger.onclick=()=>{document.querySelector('#models').hidden=false;trigger.textContent='Thinking effort';};
          document.addEventListener('keydown',e=>{if(e.key==='Escape')setTimeout(()=>trigger.textContent='Pro',40)});
        }""")
        await adapter.observe()
        await asyncio.sleep(0.12)
        assert (await adapter.observe()).safe_failure()

    run(case)


def test_nested_wrappers_acknowledge_latest_without_historical_running():
    async def case(page, adapter):
        await page.evaluate("""() => {
          const root=document.querySelector('[data-thread-find-target]');
          root.insertAdjacentHTML('afterbegin','<div><div data-turn-key="33333333-3333-4333-8333-333333333333"><div data-talvt-turn-state="complete">Historical</div></div></div>');
          const latest=root.lastElementChild;const wrapper=document.createElement('div');latest.replaceWith(wrapper);wrapper.append(latest);
        }""")
        observed = await adapter.observe()
        assert observed.safe_failure()
        assert await adapter.send(observed)

    run(case)


def test_explicit_sol_selection_verifies_and_guardian_sends_with_quota(tmp_path):
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "sol.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat)
        store.quota()
        await page.evaluate(
            'document.body.insertAdjacentHTML("beforeend",\'<div role="alert">GPT-6 usage limit reached</div>\')'
        )
        assert await adapter.select_sol(store, chat)
        observed = await adapter.observe()
        assert observed.model == "Sol" and observed.effort == "Extra High"
        assert observed.verified and observed.quota
        assert await page.evaluate("window.switches") == 1
        guardian = Guardian(store, apply=True)
        now = time.monotonic()
        guardian.stability[chat] = observed, now - 21
        guardian.last_seen[chat] = now
        assert await guardian.run_once(adapter) == "sent"
        assert await page.evaluate("window.clicks") == 1

    run(case, html=MENU_HTML)


def test_sol_helper_navigation_before_selection_cannot_switch_other_chat(tmp_path):
    from agenttools_chatgpt.core import Store

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "sol.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat)
        store.quota()
        menu_state = adapter._menu_state

        async def navigate_between_reads():
            await page.evaluate(
                "history.pushState({},'', '/c/cccccccc-cccc-4ccc-8ccc-cccccccccccc')"
            )
            return await menu_state()

        adapter._menu_state = navigate_between_reads
        assert not await adapter.select_sol(store, chat)
        assert await page.evaluate("window.switches") == 0

    run(case, html=MENU_HTML)


def test_sol_raw_max_and_outside_status_do_not_verify_effort():
    async def case(page, adapter):
        await page.evaluate("""() => {
          window.model='GPT-5.6 Sol';window.effort='Extra High';
          const radios=document.querySelector('#full').children;
          for(const radio of radios){const selected=radio.textContent===window.model;radio.setAttribute('aria-checked',String(selected));radio.setAttribute('data-model-selected',String(selected));}
          document.querySelector('[data-codex-intelligence-trigger]').setAttribute('data-selected-reasoning-effort','max');
          document.querySelector('[role=status]').textContent='Medium, 2 of 5.';
          document.body.insertAdjacentHTML('beforeend','<span role="status">Extra High, 4 of 5.</span>');
        }""")
        assert not (await adapter.observe()).verified

    run(case, html=MENU_HTML)


def test_failed_reprobe_invalidates_previous_identity():
    async def case(page, adapter):
        assert (await adapter.observe()).verified
        await page.evaluate("""() => {
          const trigger=document.querySelector('[data-codex-intelligence-trigger]');
          trigger.replaceWith(trigger.cloneNode(true));
          document.querySelector('[role=menuitemradio]').textContent='Unknown Model';
        }""")
        adapter.probed_at = 0
        assert not (await adapter.observe()).verified

    run(case)


def test_sol_lost_mutation_transport_holds_guard_before_any_dispose(tmp_path):
    import sqlite3

    from agenttools_chatgpt.core import Store
    from playwright.async_api import Error as BrowserError

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "sol.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat)
        store.quota()
        quarantined, release = asyncio.Event(), asyncio.Event()
        evaluate = page.evaluate

        class BrokenHandle:
            async def dispose(self):
                raise AssertionError("Remote dispose must not precede quarantine")

        async def lost_result(expression, arg=None):
            result = await evaluate(expression, arg)
            if (
                isinstance(arg, dict)
                and arg.get("action") == "selection-step"
                and arg.get("target") == "sol"
            ):
                adapter.probe = BrokenHandle()
                raise BrowserError("Lost mutation response")
            return result

        async def quarantine():
            quarantined.set()
            await release.wait()

        page.evaluate = lost_result
        adapter._quarantine = quarantine
        pending = asyncio.create_task(adapter.select_sol(store, chat))
        await asyncio.wait_for(quarantined.wait(), timeout=2)
        assert adapter.probe is None
        try:
            with (
                pytest.raises(sqlite3.OperationalError, match="locked"),
                store.connect(timeout=0) as db,
            ):
                db.execute("INSERT INTO controls VALUES('stop:*','manual stop')")
        finally:
            release.set()
        assert not await pending

    run(case, html=MENU_HTML)


@pytest.mark.parametrize(
    "blocked",
    [
        "consent",
        "quota",
        "watch",
        "stop",
        "unresolved",
        "running",
        "draft",
        "power-focus-draft",
        "unknown-effort",
    ],
)
def test_sol_helper_refuses_unsafe_or_missing_permission(tmp_path, blocked):
    from agenttools_chatgpt.core import Store

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        store = Store(tmp_path / "sol.sqlite", account="glide.vc")
        store.enroll(chat)
        store.opt_in_sol(chat)
        store.quota()
        if blocked == "consent":
            store.opt_in_sol(chat, False)
        elif blocked == "quota":
            store.clear_quota()
        elif blocked == "watch":
            store.set_control("watch:" + chat, "0")
        elif blocked == "stop":
            store.stop()
        elif blocked == "unresolved":
            with store.connect() as db:
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,?,?,?)",
                    (chat, "old", "old", time.time(), "uncertain"),
                )
        elif blocked == "running":
            await page.evaluate(
                'document.body.insertAdjacentHTML("beforeend",\'<button aria-label="Stop">Stop</button>\')'
            )
        elif blocked == "draft":
            await page.locator("[contenteditable]").fill("human draft")
        elif blocked == "power-focus-draft":
            await page.evaluate(
                """document.querySelector('[aria-label="Power"]').onfocus=()=>document.querySelector('[contenteditable]').textContent='human draft' """
            )
        else:
            await page.evaluate(
                """document.querySelector('[role=status]').textContent='Medium, 2 of 5.' """
            )
        assert not await adapter.select_sol(store, chat)
        assert not await page.evaluate("Boolean(window.clicks)")
        assert await page.evaluate("window.effort") == "Pro"
        if blocked not in ("power-focus-draft", "unknown-effort"):
            assert await page.evaluate("window.switches") == 0
        if blocked == "power-focus-draft":
            assert (
                await page.locator("[contenteditable]").text_content() == "human draft"
            )

    run(case, html=MENU_HTML)


@pytest.mark.parametrize(
    "status",
    ["Connection interrupted. Waiting for the complete answer", "Stopped thinking"],
)
def test_interruption_and_manual_stop_are_not_failures(status):
    async def case(page, adapter):
        await page.locator('[class="group/activity-header"] span').evaluate(
            "(el,text)=>el.textContent=text", status
        )
        await page.locator("[data-talvt-turn-state]").evaluate(
            '(el)=>el.setAttribute("data-talvt-turn-state","complete")'
        )
        if status.startswith("Connection"):
            await page.evaluate(
                'document.body.insertAdjacentHTML("beforeend",\'<button aria-label="Stop">Stop</button>\')'
            )
        assert not (await adapter.observe()).safe_failure()

    run(case)


def run(case, *, cdp_port=None, html=HTML):
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
                await case(page, LiveAdapter(page))
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_real_structure_failure_sends_once_with_durable_guardian(tmp_path):
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        observed = await adapter.observe()
        assert observed.safe_failure()
        assert observed.chat == canonical_chat(PROJECT)
        assert observed.turn == observed.generation
        store = Store(tmp_path / "state.sqlite", account="glide.vc")
        store.enroll(observed.chat)
        guardian = Guardian(store, apply=True)
        assert await guardian.run_once(adapter) == "stabilizing"
        now = time.monotonic()
        guardian.stability[observed.chat] = observed, now - 21
        guardian.last_seen[observed.chat] = now
        assert await guardian.run_once(adapter) == "sent"
        assert await page.evaluate("window.clicks") == 1
        assert (
            Store(store.path, account="glide.vc").status()["attempts"][0]["state"]
            == "sent"
        )
        assert await guardian.run_once(adapter) == "unsafe-or-not-active-failure"

    run(case)


@pytest.mark.parametrize(
    "change",
    [
        "document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','in_progress')",
        "document.body.insertAdjacentHTML('beforeend','<button aria-label=Stop>Stop</button>')",
        "document.querySelector('[class]').className='prose'",
        "document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state','complete')",
        "document.querySelector('[contenteditable]').textContent=' '",
        "document.querySelector('main').append(document.querySelector('[data-thread-find-target]').cloneNode(true))",
        "document.querySelector('[data-thread-find-target]').append(document.querySelector('[data-turn-key]').cloneNode(true))",
        "document.querySelector('button').remove()",
        "document.querySelector('[role=menuitemradio]').textContent='Unknown Model'",
        "document.querySelector('[role=menuitemradio]').textContent='GPT-5.6 Sol'",
        "document.querySelector('[data-thread-find-target]').insertAdjacentHTML('beforeend','<div data-turn-key=33333333-3333-4333-8333-333333333333><div data-talvt-turn-state=in_progress>Running</div></div>')",
    ],
)
def test_observed_ui_unsafe_states_never_send(change):
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate("() => {" + change + "}")
        adapter.probed_at = 0
        assert not (await adapter.observe()).safe_failure()
        assert not await adapter.send(expected)
        assert not await page.evaluate("Boolean(window.clicks)")

    run(case)


@pytest.mark.parametrize(
    "change",
    [
        "const c=document.querySelector('[contenteditable]'); c.replaceWith(c.cloneNode(true))",
        "document.querySelector('[contenteditable]').textContent='human draft'",
        "history.pushState({},'', '/c/wrong-chat')",
        "document.querySelector('[data-codex-intelligence-trigger]').setAttribute('data-selected-reasoning-effort','high')",
        "document.body.insertAdjacentHTML('beforeend','<div role=alert>GPT-6 usage limit reached</div>')",
    ],
)
def test_preparation_race_preserves_human_state_and_never_clicks(change):
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate(
            "() => {document.querySelector('[contenteditable]').addEventListener('input', () => {"
            + change
            + "}, {once:true})}"
        )
        assert not await adapter.send(expected)
        assert not await page.evaluate("Boolean(window.clicks)")
        if "human draft" in change:
            assert (
                await page.locator("[contenteditable]").text_content() == "human draft"
            )

    run(case)


def test_unrelated_usage_is_not_quota_and_hidden_model_is_not_evidence():
    async def case(page, adapter):
        await page.evaluate("""() => {
            document.body.insertAdjacentHTML('beforeend','<div role=alert>0% weekly WORK/CODEX. Chat conversations are not included.</div>');
            document.querySelector('#models').insertAdjacentHTML('beforeend','<div aria-hidden=true><div role=menuitemradio aria-checked=true data-model-selected=true>Unknown Model</div></div>');
        }""")
        assert (await adapter.observe()).safe_failure()
        assert not (await adapter.observe()).quota
        await page.evaluate(
            "document.querySelector('[data-codex-intelligence-trigger]').textContent='Changed'"
        )
        assert not (await adapter.observe()).verified
        await page.evaluate(
            "document.querySelector('[data-codex-intelligence-trigger]').textContent='Pro'"
        )
        await page.evaluate("p => {p.at -= 11000}", adapter.probe)
        assert not (await adapter.observe()).verified

    run(case)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:9223",
        "http://localhost:9223",
        "http://127.0.0.1:9223/evil",
        "http://user@127.0.0.1:9223",
        "http://127.0.0.1:9223?x=1",
        "http://192.168.1.1:9223",
        "http://127.0.0.1:0",
    ],
)
def test_cdp_endpoint_requires_exact_loopback(endpoint):
    from agenttools_chatgpt.live import validate_cdp

    with pytest.raises(ValueError):
        validate_cdp(endpoint)


def test_cdp_single_owner_across_workspaces(tmp_path):
    from agenttools_chatgpt.live import LiveOwner, validate_cdp

    assert validate_cdp("http://127.0.0.1:9223") == "http://127.0.0.1:9223"
    with (
        LiveOwner(tmp_path, "http://127.0.0.1:9223"),
        pytest.raises(RuntimeError, match="owner"),
        LiveOwner(tmp_path, "http://127.0.0.1:9223"),
    ):
        pass


def test_uncertain_click_is_not_retried_after_restart(tmp_path):
    from agenttools_chatgpt.core import Guardian, Store

    async def case(page, adapter):
        observed = await adapter.observe()
        # A lost acknowledgement leaves the original turn and an uncertain intent.
        await page.evaluate("""() => {
            document.querySelector('[contenteditable]').addEventListener('input',()=>{
                const send=document.querySelector('[aria-label=Send]');
                send.onclick=()=>{window.clicks=(window.clicks||0)+1; document.querySelector('[contenteditable]').textContent=''};
            });
        }""")
        store = Store(tmp_path / "state.sqlite", account="glide.vc")
        guardian = Guardian(store, apply=True)
        now = time.monotonic()
        guardian.stability[observed.chat] = observed, now - 21
        guardian.last_seen[observed.chat] = now
        assert await guardian.run_once(adapter) == "uncertain-send"
        restarted = Guardian(Store(store.path, account="glide.vc"), apply=True)
        restarted.stability[observed.chat] = observed, time.monotonic() - 21
        restarted.last_seen[observed.chat] = time.monotonic()
        assert await restarted.run_once(adapter) == "unresolved-send"
        assert await page.evaluate("window.clicks") == 1
        assert restarted.store.status()["attempts"][0]["state"] == "uncertain"

    run(case)


def test_enrollment_selects_original_project_page_only():
    from agenttools_chatgpt.live import enrolled_pages

    async def case(page, adapter):
        chat = canonical_chat(PROJECT)
        unrelated = await page.context.new_page()
        await unrelated.goto("about:blank")
        assert enrolled_pages(page.context, {chat}) == {chat: [page]}
        duplicate = await page.context.new_page()
        await duplicate.route(
            "**/*", lambda route: route.fulfill(body=HTML, content_type="text/html")
        )
        await duplicate.goto(chat)
        assert enrolled_pages(page.context, {chat}) == {chat: [page, duplicate]}

    run(case)


def test_live_cli_observe_defaults_and_workspace(tmp_path, monkeypatch):
    from agenttools_chatgpt import cli, live

    captured = []

    async def serve(args, store):
        captured.append((args, store))

    monkeypatch.setattr(live, "serve_live", serve)
    assert (
        cli.main(
            [
                "--state",
                str(tmp_path / "state.sqlite"),
                "live",
                "--workspace",
                "glide.vc",
                "--cdp",
                "http://127.0.0.1:9223",
                "--watch",
                PROJECT,
                "--once",
            ]
        )
        == 0
    )
    args, store = captured[0]
    assert args.workspace == "glide.vc"
    assert not args.apply
    assert args.watch == [PROJECT]
    assert store.path == tmp_path / "state.sqlite"


def test_focus_handler_human_draft_is_never_overwritten():
    async def case(page, adapter):
        expected = await adapter.observe()
        await page.evaluate(
            """() => document.querySelector('[contenteditable]').addEventListener('focus', e=>{e.target.textContent='human draft'}, {once:true})"""
        )
        assert not await adapter.send(expected)
        assert await page.locator("[contenteditable]").text_content() == "human draft"
        assert not await page.evaluate("Boolean(window.clicks)")

    run(case)


def test_live_once_attaches_only_fixture_and_leaves_browser_open(
    tmp_path, monkeypatch, capsys
):
    import socket
    from types import SimpleNamespace

    from agenttools_chatgpt import live
    from agenttools_chatgpt.core import Store
    from playwright.async_api import BrowserType

    connect = BrowserType.connect_over_cdp
    connections = []

    async def attached(self, endpoint_url, **kwargs):
        connections.append(kwargs)
        return await connect(self, endpoint_url, **kwargs)

    monkeypatch.setattr(BrowserType, "connect_over_cdp", attached)

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    owner = live.LiveOwner
    monkeypatch.setattr(
        live, "LiveOwner", lambda root, endpoint: owner(tmp_path / "owner", endpoint)
    )

    async def case(page, adapter):
        args = SimpleNamespace(
            cdp=f"http://127.0.0.1:{port}",
            watch=[PROJECT],
            apply=False,
            workspace="glide.vc",
            control_port=0,
            once=True,
        )
        store = Store(tmp_path / "glide.vc" / "state.sqlite", account="glide.vc")
        await live.serve_live(args, store)
        args.watch = []
        await live.serve_live(args, store)
        store.set_control("watch:" + canonical_chat(PROJECT), "0")
        args.watch = [PROJECT]
        await live.serve_live(args, store)
        assert page.context.browser.is_connected()
        assert await page.title() == "ChatGPT fixture"
        assert len(page.context.pages) == 1
        assert not await page.evaluate("Boolean(window.clicks)")
        assert store.controls()["watch:" + canonical_chat(PROJECT)] == "0"

    run(case, cdp_port=port)
    assert connections == [{"timeout": 10000, "no_defaults": True}] * 3
    assert '"health": "connected"' in capsys.readouterr().out
