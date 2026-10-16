"""HA launcher adapter: run(bridge) serves the existing OpenAI-compatible app.

The bridge module supplies prompt/reply protocol code; this adapter owns lifecycle.
Restored and challenged pages remain open. Guardian alone authorizes recovery sends,
under the same lock as API prompts; no clipboard, cookie, or token extraction occurs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .core import MutationRefusal, protected_dispatch
from .state import state_path
from .urls import canonical_chat


def page_chat(page):
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
        return ""
    if parsed.path in ("", "/"):
        return "https://chatgpt.com/"
    try:
        return canonical_chat("https://chatgpt.com" + parsed.path)
    except ValueError:
        return ""


def create_browser(bridge, lifecycle, mode: str, *args, guardian=None, contract=None):
    """Adapt the Browser contract at HA PR #10 without modifying its module."""
    if mode not in ("manual", "managed"):
        raise ValueError("browser mode must be manual or managed")

    class Browser(bridge.Browser):
        async def start(self):
            if mode == "manual":
                await lifecycle.start(manual=True)
                self.state = "manual_login"
                return
            self.ctx = await lifecycle.start()
            restored = next((page for page in self.ctx.pages if page_chat(page)), None)
            self.status_page = restored or (
                self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
            )
            if self.status_page.url == "about:blank":
                try:
                    await self.status_page.goto(
                        bridge.HOME, wait_until="domcontentloaded", timeout=10000
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Initial ChatGPT navigation failed; preserved browser"
                    )
            await self.refresh_state()

        async def refresh_state(self, reload: bool = False):
            if mode == "manual":
                return "manual_login"
            state = await asyncio.wait_for(
                bridge.readiness(self.status_page), timeout=5
            )
            await self.set_state(state)
            return state

        async def monitor_once(self):
            if mode == "manual" or self.lock.locked():
                if guardian is not None:
                    guardian.stability.clear()
                return
            async with self.lock:
                samples = []
                if guardian is not None and contract is not None:
                    from .browser import PlaywrightAdapter, observe_tabs

                    # Quota is account-wide even when its tab is not opted into recovery.
                    samples = await observe_tabs(
                        [PlaywrightAdapter(page, contract) for page in self.ctx.pages],
                        record_safety=guardian.record_safety,
                    )
                await self.refresh_state()
                if self.state in ("challenge_required", "signin_required"):
                    if guardian is not None:
                        await asyncio.to_thread(
                            guardian.store.stop,
                            reason="HA browser requires human attention",
                        )
                    return
                if guardian is None or contract is None:
                    return
                controls = await asyncio.to_thread(guardian.store.controls)
                watched = {
                    key.removeprefix("watch:")
                    for key, value in controls.items()
                    if key.startswith("watch:") and value == "1"
                }
                for adapter, observed, captured_at in samples:
                    if (
                        observed.chat in watched
                        and page_chat(adapter.page) == observed.chat
                    ):
                        await asyncio.wait_for(
                            guardian.run_once(
                                adapter,
                                initial=observed,
                                observed_at=captured_at,
                            ),
                            timeout=10,
                        )

        async def monitor(self):
            deadline = asyncio.get_running_loop().time()
            while True:
                deadline += 1
                await asyncio.sleep(
                    max(0, deadline - asyncio.get_running_loop().time())
                )
                try:
                    await self.monitor_once()
                except Exception:
                    logging.getLogger(__name__).exception("HA monitor failed")
                    # Unknown browser state must stop recovery until explicitly resumed.
                    if guardian is not None:
                        await asyncio.to_thread(
                            guardian.store.stop, reason="HA monitor observation failed"
                        )
                    await self.set_state("loading")
                deadline = max(deadline, asyncio.get_running_loop().time() - 1)

        async def new_conversation(self):
            live = [conv for conv in self.convs if not conv.page.is_closed()]
            if max(len(live), len(getattr(self.ctx, "pages", ()))) >= self.max_tabs:
                raise bridge.BridgeError(
                    "Tab capacity reached; close an unused tab manually.",
                    503,
                    "tab_capacity",
                )
            page = await self.ctx.new_page()
            try:
                await page.goto(
                    self.chat_url(), wait_until="domcontentloaded", timeout=60000
                )
            except Exception as exc:
                raise bridge.BridgeError(
                    "Navigation failed; preserved tab for manual inspection.",
                    503,
                    "navigation_failed",
                ) from exc
            deadline = asyncio.get_running_loop().time() + 20
            try:
                state = await asyncio.wait_for(bridge.readiness(page), timeout=5)
                while (
                    state == "loading" and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.25)
                    state = await asyncio.wait_for(
                        bridge.readiness(page),
                        timeout=min(
                            5, max(0.01, deadline - asyncio.get_running_loop().time())
                        ),
                    )
            except TimeoutError as exc:
                raise bridge.BridgeError(
                    "Readiness timed out; preserved tab for inspection.", 503, "loading"
                ) from exc
            if state in ("challenge_required", "signin_required"):
                if guardian is not None:
                    await asyncio.to_thread(
                        guardian.store.stop,
                        reason="HA conversation requires human attention",
                    )
                await self.set_state(state)
                raise bridge.NeedsHuman(state)
            if state != "ready":
                raise bridge.BridgeError(
                    "Composer is not ready; inspect preserved tab.", 503, "loading"
                )
            conv = bridge.Conversation(page)
            self.convs = live + [conv]
            return conv

        async def reply_snapshot(self, page, action):
            from .browser import PlaywrightAdapter

            script = PlaywrightAdapter(page, contract).script
            # URL, turn identity, text and rendered extraction share one JS task.
            render = bridge.DOM_MARKDOWN if action == "reply" else "null"
            return await page.evaluate(
                "payload => { payload.render = (" + render + ");"
                "return (" + script + ")(payload); }",
                {"contract": asdict(contract), "action": action},
            )

        async def enforce_quota(self, page=None, *, owned_draft=False):
            if guardian is None:
                return
            state = await asyncio.to_thread(guardian.store.status)
            controls = state["controls"]
            if "stop:*" in controls or (
                page is not None and "stop:" + page_chat(page) in controls
            ):
                raise bridge.BridgeError(
                    "Guardian was manually stopped.", 503, "guardian_stopped"
                )
            if "astra_quota" not in state["controls"]:
                return
            if page is not None and contract is not None:
                from .browser import PlaywrightAdapter

                observed = await PlaywrightAdapter(page, contract).observe_api()
                if (
                    observed.verified
                    and observed.model == "Sol"
                    and observed.effort == "Extra High"
                    and observed.chat in state["sol_chats"]
                    and not observed.challenge
                    and not observed.ambiguous
                    and not observed.running
                    and (owned_draft or not observed.draft)
                ):
                    return
            raise bridge.BridgeError(
                "Astra quota is blocked; unknown model is unsafe.", 429, "rate_limited"
            )

        async def ask(self, page, prompt):
            expected_chat = page_chat(page)
            navigated = False

            def navigation(frame):
                nonlocal navigated
                if frame == page.main_frame:
                    navigated = True

            def check_chat():
                if navigated or page_chat(page) != expected_chat:
                    raise bridge.BridgeError(
                        "Chat navigation during API request; refusing result.",
                        503,
                        "busy",
                    )

            if hasattr(page, "on"):
                page.on("framenavigated", navigation)
            try:
                return await self.ask_in_chat(page, prompt, expected_chat, check_chat)
            finally:
                if hasattr(page, "remove_listener"):
                    page.remove_listener("framenavigated", navigation)

        async def ask_in_chat(self, page, prompt, expected_chat, check_chat):
            if not page_chat(page):
                raise bridge.BridgeError(
                    "Unsupported chat page; refusing input.", 503, "unsafe_origin"
                )
            await self.enforce_quota(page)
            check_chat()
            if guardian is None or contract is None or not contract.verified_account_ui:
                raise bridge.BridgeError(
                    "Managed API requires an account-verified UI contract.",
                    503,
                    "verified_contract_required",
                )
            from .browser import PlaywrightAdapter

            initial = await asyncio.wait_for(
                PlaywrightAdapter(page, contract).observe_api(), timeout=5
            )
            await asyncio.to_thread(guardian.record_safety, initial)
            check_chat()
            if (
                not initial.verified
                or initial.draft
                or initial.challenge
                or initial.running
                or initial.ambiguous
            ):
                raise bridge.BridgeError(
                    "Existing draft or unverified API UI; refusing input.", 503, "busy"
                )
            if initial.chat.rstrip("/") != expected_chat.rstrip("/"):
                raise bridge.BridgeError(
                    "API chat changed before preparation.", 503, "busy"
                )
            if initial.model == "Sol" and expected_chat == "https://chatgpt.com/":
                raise bridge.BridgeError(
                    "Sol requires consent for a permanent chat.",
                    503,
                    "sol_consent_required",
                )
            before = await asyncio.wait_for(
                self.reply_snapshot(page, "reply-baseline"), timeout=5
            )
            check_chat()
            if not before or before["chat"].rstrip("/") != expected_chat.rstrip("/"):
                raise bridge.BridgeError(
                    "Unverified conversation baseline.", 503, "busy"
                )
            async with guardian.store.async_guard() as db:
                allowed = guardian.store._api_allowed(
                    db, expected_chat, initial.model, initial.effort, initial.verified
                )
                if not allowed:
                    raise bridge.BridgeError(
                        "Guardian controls prohibit API preparation.",
                        503,
                        "guardian_stopped",
                    )

                async def prepare_and_record():
                    result = await PlaywrightAdapter(page, contract).prepare_api(
                        initial, prompt
                    )
                    if isinstance(result, MutationRefusal):
                        guardian.record_safety(result.observation, db)
                    return result

                prepared = await protected_dispatch(prepare_and_record())
            if not prepared:
                raise bridge.BridgeError(
                    "API UI changed before preparation; preserved draft.",
                    503,
                    "busy",
                )
            send = page.locator(contract.send)
            check_chat()
            await send.wait_for(state="visible", timeout=15000)
            check_chat()
            await self.enforce_quota(page, owned_draft=True)
            check_chat()
            await self.dispatch_prompt(
                page, prompt, expected_chat=expected_chat, check_chat=check_chat
            )
            check_chat()
            deadline = time.monotonic() + self.reply_timeout
            last, stable_since, submitted_turn = None, 0.0, None
            while time.monotonic() < deadline:
                check_chat()
                state = await bridge.readiness(page)
                check_chat()
                if state in ("challenge_required", "signin_required"):
                    await asyncio.to_thread(
                        guardian.store.stop,
                        page_chat(page) or "*",
                        "API reply requires human attention",
                    )
                    await self.set_state(state)
                    raise bridge.NeedsHuman(state)
                alerts = page.get_by_role("alert")
                alert_count = await alerts.count()
                check_chat()
                for index in range(min(alert_count, 4)):
                    alert_text = await alerts.nth(index).inner_text()
                    check_chat()
                    if bridge.LIMIT.search(alert_text):
                        if guardian is not None:
                            await asyncio.to_thread(guardian.store.quota)
                        raise bridge.BridgeError(
                            "ChatGPT quota reached.", 429, "rate_limited"
                        )
                running = await bridge.visible(page.locator(contract.running))
                check_chat()
                reply = await self.reply_snapshot(
                    page, "reply-identity" if running else "reply"
                )
                check_chat()
                if not reply or reply["chat"].rstrip("/") != expected_chat.rstrip("/"):
                    raise bridge.BridgeError(
                        "Chat changed while reading answer.", 503, "busy"
                    )
                if reply["challenge"]:
                    await asyncio.to_thread(
                        guardian.store.stop,
                        expected_chat,
                        "API reply requires human attention",
                    )
                    raise bridge.NeedsHuman("challenge_required")
                if reply["quota"]:
                    await asyncio.to_thread(guardian.store.quota)
                    raise bridge.BridgeError(
                        "ChatGPT quota reached.", 429, "rate_limited"
                    )
                if running or reply["running"]:
                    last, stable_since = None, 0.0
                baseline, turns = before["turns"], reply["turns"]
                identity = lambda turn: (
                    turn["turn"],
                    turn["generation"],
                    turn["author"],
                )
                if [identity(turn) for turn in turns[: len(baseline)]] != [
                    identity(turn) for turn in baseline
                ]:
                    raise bridge.BridgeError(
                        "Conversation turns changed during reply.", 503, "busy"
                    )
                added = turns[len(baseline) :]
                if not added:
                    await asyncio.sleep(0.5)
                    continue
                if (
                    len(added) > 2
                    or added[0]["author"] != "user"
                    or added[0]["text"] != prompt
                ):
                    raise bridge.BridgeError(
                        "Conversation does not match submitted prompt.", 503, "busy"
                    )
                if len(added) < 2:
                    await asyncio.sleep(0.5)
                    continue
                answer = added[1]
                turn = identity(answer)
                if (
                    answer["author"] != "assistant"
                    or not answer["generation"]
                    or (submitted_turn is not None and turn != submitted_turn)
                ):
                    raise bridge.BridgeError(
                        "Submitted assistant turn changed.", 503, "busy"
                    )
                submitted_turn = turn
                if running or reply["running"]:
                    await asyncio.sleep(0.5)
                    continue
                text = answer["text"]
                if not text.strip():
                    last, stable_since = None, 0.0
                elif text != last:
                    last, stable_since = text, time.monotonic()
                elif time.monotonic() - stable_since > 1.5:
                    check_chat()
                    return reply["markdown"]
                await asyncio.sleep(0.5)
            raise bridge.BridgeError(
                "No complete answer before timeout; inspect preserved tab.",
                504,
                "timeout",
            )

        async def dispatch_prompt(
            self, page, prompt, *, expected_chat=None, check_chat=None
        ):
            expected_chat = page_chat(page) if expected_chat is None else expected_chat
            if not expected_chat:
                raise bridge.BridgeError(
                    "Unsupported chat page; refusing send.", 503, "unsafe_origin"
                )
            if guardian is None or contract is None or not contract.verified_account_ui:
                raise bridge.BridgeError(
                    "Managed API requires an account-verified UI contract.",
                    503,
                    "verified_contract_required",
                )
            from .browser import PlaywrightAdapter

            observed = await asyncio.wait_for(
                PlaywrightAdapter(page, contract).observe_api(owned_draft=prompt),
                timeout=5,
            )
            await asyncio.to_thread(guardian.record_safety, observed)
            if observed.model == "Sol" and expected_chat == "https://chatgpt.com/":
                raise bridge.BridgeError(
                    "Sol requires consent for a permanent chat.",
                    503,
                    "sol_consent_required",
                )
            if (
                not observed.verified
                or observed.ambiguous
                or observed.challenge
                or observed.running
                or observed.draft
            ):
                raise bridge.BridgeError(
                    "API UI evidence is unsafe or unverified; preserved draft.",
                    503,
                    "busy",
                )
            if observed.chat.rstrip("/") != expected_chat.rstrip("/"):
                raise bridge.BridgeError(
                    "Observed chat differs from authorized page.", 503, "busy"
                )

            async def final_click(db):
                if check_chat is not None:
                    check_chat()
                current = await PlaywrightAdapter(page, contract).observe_api(
                    owned_draft=prompt
                )
                if check_chat is not None:
                    check_chat()
                guardian.record_safety(current, db)
                if (
                    (
                        current.quota
                        and not (
                            current.verified
                            and current.model == "Sol"
                            and current.effort == "Extra High"
                        )
                    )
                    or current.challenge
                    or current.ambiguous
                    or current.running
                    or current.draft
                    or current.chat.rstrip("/") != expected_chat.rstrip("/")
                    or (current.model, current.effort, current.verified)
                    != (observed.model, observed.effort, observed.verified)
                ):
                    raise bridge.BridgeError(
                        "Model or safety state changed before send; preserved draft.",
                        503,
                        "busy",
                    )
                if page_chat(page) != expected_chat:
                    raise bridge.BridgeError(
                        "Chat changed before send; preserved draft.", 503, "busy"
                    )
                result = await PlaywrightAdapter(page, contract).send_api(
                    current, prompt
                )
                if isinstance(result, MutationRefusal):
                    guardian.record_safety(result.observation, db)
                if not result:
                    raise bridge.BridgeError(
                        "API UI changed before atomic send; preserved draft.",
                        503,
                        "busy",
                    )

            try:
                async with guardian.store.async_guard() as db:
                    allowed = guardian.store._api_allowed(
                        db,
                        expected_chat,
                        observed.model,
                        observed.effort,
                        observed.verified,
                    )
                    if not allowed:
                        raise bridge.BridgeError(
                            "Guardian controls prohibit API dispatch; preserved draft.",
                            503,
                            "guardian_stopped",
                        )
                    await protected_dispatch(final_click(db))
            except bridge.BridgeError:
                raise
            except asyncio.CancelledError:
                await asyncio.to_thread(
                    guardian.store.stop, expected_chat, "API dispatch canceled"
                )
                raise
            except Exception as exc:
                await asyncio.to_thread(
                    guardian.store.stop, expected_chat, "API dispatch uncertain"
                )
                raise bridge.BridgeError(
                    "API dispatch uncertain; chat stopped for manual inspection.",
                    503,
                    "ambiguous_send",
                ) from exc

        async def complete(self, messages, tools):
            if mode == "manual":
                raise bridge.BridgeError(
                    "Complete login and human checks manually.", 503, "manual_login"
                )
            page = None
            if self.convs:
                rest = [
                    message
                    for message in messages
                    if message.get("role") not in ("system", "developer")
                ]
                request = [bridge.canon(message) for message in rest]
                index = bridge.match_conversation(
                    [conv.history for conv in self.convs], request
                )
                if index is not None:
                    page = self.convs[index].page
            await self.enforce_quota(page)
            return await super().complete(messages, tools)

    return Browser(*args)


async def run(bridge):
    """Entry point imported by docker/chatgpt-web/launcher.py in HA PR #10."""
    from aiohttp import web

    from .browser import BrowserLifecycle, Contract
    from .core import Guardian, Store

    mode = os.environ.get("CHATGPT_WEB_BROWSER_MODE", "manual")
    api_key = os.environ.get("CHATGPT_WEB_API_KEY", "")
    if len(api_key) < 24:
        raise SystemExit("CHATGPT_WEB_API_KEY must be set (24+ characters).")
    lifecycle = BrowserLifecycle(
        Path(
            os.environ.get("CHATGPT_GUARDIAN_PROFILE_ROOT", "/data/guardian-profiles")
        ),
        os.environ.get("CHATGPT_GUARDIAN_WORKSPACE", "home"),
        executable=os.environ.get("CHROME_BIN"),
    )
    guardian, contract = None, None
    if mode == "managed":
        workspace = os.environ.get("CHATGPT_GUARDIAN_WORKSPACE", "home")
        store = Store(state_path(workspace, ha=True), account=workspace)
        guardian = Guardian(
            store,
            apply=os.environ.get("CHATGPT_GUARDIAN_ENABLED") == "1"
            and os.environ.get("CHATGPT_GUARDIAN_APPLY") == "1",
        )
        contract_path = os.environ.get("CHATGPT_GUARDIAN_CONTRACT")
        if contract_path:
            contract = Contract.load(Path(contract_path))
    notifier = bridge.Notifier(
        os.environ.get("HA_URL", ""), os.environ.get("HA_TOKEN", "")
    )
    browser = create_browser(
        bridge,
        lifecycle,
        mode,
        str(lifecycle.profile),
        int(os.environ.get("CHATGPT_WEB_MAX_TABS", "3")),
        float(os.environ.get("CHATGPT_WEB_REPLY_TIMEOUT", "300")),
        os.environ.get("CHATGPT_WEB_TEMPORARY", "1") == "1",
        os.environ.get("CHATGPT_WEB_MODEL_SLUG", ""),
        notifier,
        guardian=guardian,
        contract=contract,
    )
    runner = web.AppRunner(bridge.make_app(browser, api_key))
    monitor = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        await browser.start()
        await runner.setup()
        await web.TCPSite(
            runner,
            os.environ.get("CHATGPT_WEB_HOST", "127.0.0.1"),
            int(os.environ.get("CHATGPT_WEB_PORT", "8787")),
        ).start()
        monitor = asyncio.create_task(browser.monitor())
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if monitor is not None:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        await runner.cleanup()
        await lifecycle.close()
