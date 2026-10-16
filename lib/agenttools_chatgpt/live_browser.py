"""Observed ChatGPT UI adapter for the explicit existing-CDP service.

Account UI mappings are explicit; a generic Pro trigger never identifies a model.
Mutations retain the inherited transport quarantine and core dispatch transaction.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from .browser import PlaywrightAdapter
from .core import MutationRefusal, Observation, Store, protected_dispatch


class LiveAdapter(PlaywrightAdapter):
    def __init__(
        self,
        page,
        *,
        probe_lock: asyncio.Lock | None = None,
        allow_active_probe: bool = True,
    ):
        self.page = page
        self.allow_active_probe = allow_active_probe
        self.script = Path(__file__).with_name("live_dom.js").read_text()
        self.probe = None
        self.probed_at = 0.0
        self.probe_lock = probe_lock or asyncio.Lock()
        self.labels = {"GPT-6": "Astra", "GPT-5.6 Sol": "Sol"}
        self.mutation_allowed: Callable[[], bool] = lambda: True

    async def probe_model(self) -> None:
        if not self.allow_active_probe:
            return
        async with self.probe_lock:
            await self._probe_model()

    async def _clear_probe(self) -> None:
        previous = self.probe
        self.probe = None
        if previous is not None:
            await previous.dispose()

    async def _menu_state(self):
        return await self.page.evaluate(self.script, {"action": "menu-state"})

    async def _probe_model(self) -> None:
        await self._clear_probe()
        if not self.mutation_allowed():
            return
        structural = self._parse_observation(await self.page.evaluate(self.script, {}))
        if not replace(structural, verified=True).safe_failure():
            return
        candidate = None
        try:
            trigger = self.page.locator(
                'button[aria-label="Select ChatGPT model"][data-codex-intelligence-trigger="true"]'
            )
            if await trigger.count() != 1:
                return
            if (await self._menu_state())["open"]:
                return
            if not self.mutation_allowed():
                return
            await trigger.click(timeout=1500)
            try:
                if not (await self._menu_state())["selected"]:
                    full = self.page.locator(
                        '[role="menuitem"][aria-label="Select model"]:visible'
                    )
                    if await full.count() == 1 and self.mutation_allowed():
                        await full.click(timeout=1500)
                state = await self._menu_state()
                model = (
                    self.labels.get(state["selected"][0], "")
                    if len(state["selected"]) == 1
                    else ""
                )
                candidate = await self.page.evaluate_handle(
                    self.script, {"action": "probe", "labels": self.labels}
                )
            finally:
                await self.page.keyboard.press("Escape")
            effort = ""
            if model == "Sol":
                if not self.mutation_allowed():
                    return
                await trigger.click(timeout=1500)
                try:
                    if (await self._menu_state())["effort"] == ["Extra High, 4 of 5."]:
                        effort = "Extra High"
                finally:
                    await self.page.keyboard.press("Escape")
            # Closing updates the trigger asynchronously; capture only its stable
            # closed signature, retaining independent full-panel model evidence.
            previous = None
            for _ in range(20):
                state = await self._menu_state()
                wanted = "Pro" if model == "Astra" else "5.6 Extra High"
                if (
                    not state["open"]
                    and state["label"] == wanted
                    and previous == state["signature"]
                ):
                    self.probe = await self.page.evaluate_handle(
                        self.script,
                        {
                            "action": "finalize-probe",
                            "probe": candidate,
                            "model": model,
                            "effort": effort,
                        },
                    )
                    self.probed_at = time.monotonic()
                    return
                previous = state["signature"]
                await asyncio.sleep(0.05)
        finally:
            if candidate is not None:
                await candidate.dispose()

    async def select_sol(
        self, store: Store, chat: str, expected: Observation | None = None
    ) -> bool:
        """Consented quota fallback under the same Store dispatch guard as sends."""
        if not self.allow_active_probe:
            return False
        async with store.async_guard() as db:
            if not (
                db.execute("SELECT 1 FROM controls WHERE key='astra_quota'").fetchone()
                and db.execute(
                    "SELECT 1 FROM controls WHERE key=? AND value='1'",
                    ("watch:" + chat,),
                ).fetchone()
                and store._api_allowed(db, chat, "Sol", "Extra High", True)
            ):
                return False
            async with self.probe_lock:

                async def operation():
                    await self._clear_probe()
                    observed = self._parse_observation(
                        await self.page.evaluate(self.script, {})
                    )
                    if (
                        observed.chat != chat
                        or (
                            expected is not None
                            and observed.identity != expected.identity
                        )
                        or not replace(observed, verified=True).safe_failure()
                    ):
                        return False
                    if (await self._menu_state())["open"]:
                        return False
                    prepared = await self.page.evaluate_handle(
                        self.script,
                        {"action": "selection-start", "expected": asdict(observed)},
                    )
                    deadline = time.time() * 1000 + 5000

                    async def step(target):
                        if not self.mutation_allowed():
                            return False
                        return await self.page.evaluate(
                            self.script,
                            {
                                "action": "selection-step",
                                "prepared": prepared,
                                "target": target,
                                "deadline": deadline,
                            },
                        )

                    try:
                        if not await step("trigger"):
                            return False
                        if not (await self._menu_state())[
                            "selected"
                        ] and not await step("full"):
                            return False
                        if (await self._menu_state())["selected"] not in (
                            ["GPT-6"],
                            ["GPT-5.6 Sol"],
                        ):
                            return False
                        if not await step("sol"):
                            return False
                        state = await self._menu_state()
                        if state["effort"] == ["Pro, 5 of 5."] and not await step(
                            "power"
                        ):
                            return False
                        if (await self._menu_state())["effort"] != [
                            "Extra High, 4 of 5."
                        ]:
                            return False
                    finally:
                        await self.page.keyboard.press("Escape")
                        await prepared.dispose()
                    await self._probe_model()
                    final = self._parse_observation(
                        await self.page.evaluate(self.script, {"probe": self.probe})
                    )
                    verified = (
                        final.identity == observed.identity
                        and final.model == "Sol"
                        and final.effort == "Extra High"
                        and final.verified
                        and not (
                            final.running
                            or final.draft
                            or final.challenge
                            or final.ambiguous
                        )
                    )
                    if not verified:
                        await self._clear_probe()
                    return verified

                from playwright.async_api import Error as BrowserError

                try:
                    return await protected_dispatch(operation())
                except BrowserError:
                    # No transport work may run before quarantine: even disposing
                    # a handle can fail while Chrome still executes a mutation.
                    self.probe = None
                    await self._quarantine()
                    return False

    async def observe(self) -> Observation:
        from playwright.async_api import Error as BrowserError

        try:
            structural = self._parse_observation(
                await self.page.evaluate(self.script, {"probe": self.probe})
            )
            if not self.allow_active_probe:
                return structural
            if not replace(structural, verified=True).safe_failure():
                return structural
            if self.probe is None or time.monotonic() - self.probed_at > 5:
                await self.probe_model()
            return self._parse_observation(
                await self.page.evaluate(self.script, {"probe": self.probe})
            )
        except (BrowserError, ValueError, TypeError):
            await self._clear_probe()
            return Observation("", "", "")

    async def send(self, expected: Observation) -> bool | MutationRefusal:
        if not self.allow_active_probe:
            return False
        # The core wraps this complete two-phase operation in protected_dispatch.
        deadline = time.time() * 1000 + 5000
        await self.probe_model()
        if not self.mutation_allowed():
            return False
        payload = {
            "probe": self.probe,
            "expected": asdict(expected),
            "deadline": deadline,
        }
        # evaluate_handle uses the same quarantine as evaluate: losing transport
        # cannot establish whether preparation has finished executing in Chromium.
        from playwright.async_api import Error as BrowserError

        try:
            prepared = await self.page.evaluate_handle(
                self.script, {**payload, "action": "prepare"}
            )
        except BrowserError:
            await self._quarantine()
            return False
        try:
            for _ in range(10):
                if await self.page.locator('button[aria-label="Send"]:visible').count():
                    break
                await asyncio.sleep(0.05)
            if not self.mutation_allowed():
                return False
            result = await self._mutate(
                {**payload, "action": "send", "prepared": prepared}
            )
            if result is not True:
                return self._mutation_result(result)
            for _ in range(20):
                if await self.page.evaluate(
                    self.script, {**payload, "action": "ack", "prepared": prepared}
                ):
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            await prepared.dispose()

    @staticmethod
    async def _quarantine() -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue
