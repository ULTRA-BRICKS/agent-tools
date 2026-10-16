"""Exercise real monitor loops with deterministic slow browser boundaries.

Each observation takes 2.1 virtual seconds; overlapping reads finish together.
The real Guardian and SQLite store must retain stability and every tab's safety.
"""

import argparse
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agenttools_chatgpt import browser, cli, core
from agenttools_chatgpt.core import Guardian, Store
from test_chatgpt_guardian import failure
from test_chatgpt_guardian_ha import adapter, bridge


@pytest.mark.parametrize("mode", ["cli", "ha"])
@pytest.mark.parametrize("safety", ["", "quota", "challenge", "draft"])
def test_three_slow_tabs_stabilize_and_commit_all_safety(
    tmp_path, monkeypatch, mode, safety
):
    async def scenario():
        clock = SimpleNamespace(now=0.0, cycles=0)
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=True)
        pages = [
            SimpleNamespace(
                url=f"https://chatgpt.com/c/slow{index}",
                is_closed=lambda: False,
            )
            for index in range(3)
        ]
        results = []

        class Sample:
            def __init__(self, page, contract):
                self.page = page

            async def observe(self):
                started = clock.now
                await asyncio.sleep(0)
                clock.now = max(clock.now, started + 2.1)
                observed = failure(chat=self.page.url)
                if safety and self.page is pages[-1]:
                    observed = replace(observed, **{safety: True})
                return observed

            async def send(self, expected):
                assert safety != "quota", "account quota allowed a send"
                assert not (safety and self.page is pages[-1])
                assert expected.chat == self.page.url
                return True

        original = guardian.run_once

        async def ingest(transport, initial=None, observed_at=None):
            if safety:
                controls = Store(store.path).controls()
                key = "astra_quota" if safety == "quota" else "stop:" + pages[-1].url
                assert key in controls, "dispatch began before all tab safety persisted"
            result = await original(transport, initial=initial, observed_at=observed_at)
            results.append((initial.chat, result, observed_at))
            return result

        guardian.run_once = ingest
        virtual_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
        monkeypatch.setattr(core, "time", virtual_time)
        monkeypatch.setattr(browser, "time", virtual_time)
        monkeypatch.setattr(adapter(), "time", virtual_time)
        monkeypatch.setattr(cli, "PlaywrightAdapter", Sample)
        monkeypatch.setattr(browser, "PlaywrightAdapter", Sample)
        contract = browser.Contract(verified_account_ui=True)
        for page in pages:
            store.enroll(page.url)
        if mode == "ha":
            managed = adapter().create_browser(
                bridge(),
                SimpleNamespace(),
                "managed",
                guardian=guardian,
                contract=contract,
            )
            managed.ctx = SimpleNamespace(pages=pages)
            managed.status_page = pages[0]
            for _ in range(12):
                await managed.monitor_once()
                clock.now += 1
        else:

            class Stop:
                def is_set(self):
                    clock.cycles += 1
                    return clock.cycles > 12

                async def wait(self):
                    clock.now += 1
                    raise asyncio.TimeoutError

                def set(self):
                    pass

            monkeypatch.setattr(
                cli,
                "asyncio",
                SimpleNamespace(
                    Event=Stop,
                    get_running_loop=asyncio.get_running_loop,
                    wait_for=asyncio.wait_for,
                    TimeoutError=asyncio.TimeoutError,
                    to_thread=asyncio.to_thread,
                ),
            )
            monkeypatch.setattr(cli, "Guardian", lambda *args, **kwargs: guardian)
            monkeypatch.setattr(cli.Contract, "load", lambda *args: contract)
            monkeypatch.setattr(
                cli,
                "BrowserLifecycle",
                lambda *args: SimpleNamespace(
                    start=AsyncMock(return_value=SimpleNamespace(pages=pages)),
                    close=AsyncMock(),
                ),
            )
            monkeypatch.setattr(
                cli,
                "ControlServer",
                lambda *args: SimpleNamespace(start=Mock(), close=Mock()),
            )
            args = argparse.Namespace(
                contract=None,
                apply=True,
                chat=[page.url for page in pages],
                manual=False,
                profile_root=str(tmp_path),
                workspace="home",
                browser=None,
                token_file=str(tmp_path / "token"),
                control_port=0,
            )
            await cli.serve(args, store)
        if not safety:
            assert store.status()["attempts"], (
                "serial observations never reached reservation"
            )
            assert any(result == "sent" for _, result, _ in results)
        assert {chat for chat, _, _ in results} == {page.url for page in pages}
        if safety == "quota":
            assert not store.status()["attempts"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))


def test_tab_observation_bounds_concurrency_and_keeps_completion_times(monkeypatch):
    async def scenario():
        clock = SimpleNamespace(now=0, active=0, peak=0)
        completed = {}

        class Sample:
            def __init__(self, index):
                self.index = index

            async def observe(self):
                clock.active += 1
                clock.peak = max(clock.peak, clock.active)
                for _ in range(4 - self.index % 4):
                    await asyncio.sleep(0)
                clock.now += 1
                completed[self.index] = clock.now
                clock.active -= 1
                return failure(chat=f"https://chatgpt.com/c/tab{self.index}")

            async def send(self, expected):
                pytest.fail("observations must never send")

        monkeypatch.setattr(
            browser, "time", SimpleNamespace(monotonic=lambda: clock.now)
        )
        transports = [Sample(index) for index in range(9)]
        samples = await browser.observe_tabs(transports)
        assert clock.peak == 4
        assert [transport for transport, _, _ in samples] == transports
        assert len({captured for _, _, captured in samples}) == len(transports)
        for transport, observed, captured in samples:
            assert observed.chat.endswith(f"tab{transport.index}")
            assert captured == completed[transport.index]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["cli", "ha"])
@pytest.mark.parametrize("signal", ["quota", "draft", "challenge"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_failed_tab_settles_siblings_and_persists_safety(
    tmp_path, monkeypatch, mode, signal, error
):
    async def scenario():
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=True)
        pages = [
            SimpleNamespace(
                url=f"https://chatgpt.com/c/error{index}", is_closed=lambda: False
            )
            for index in range(3)
        ]
        finished = []

        class Sample:
            def __init__(self, page, contract):
                self.page = page

            async def observe(self):
                if self.page is pages[0]:
                    finished.append(self.page.url)
                    raise error("tab observation failed")
                for _ in range(5):
                    await asyncio.sleep(0)
                finished.append(self.page.url)
                observed = failure(chat=self.page.url)
                return replace(observed, **{signal: self.page is pages[-1]})

            async def send(self, expected):
                pytest.fail("a failed observation must prevent dispatch")

        guardian.run_once = AsyncMock(side_effect=AssertionError("unexpected dispatch"))
        contract = browser.Contract(verified_account_ui=True)
        monkeypatch.setattr(browser, "PlaywrightAdapter", Sample)
        monkeypatch.setattr(cli, "PlaywrightAdapter", Sample)
        if mode == "ha":
            managed = adapter().create_browser(
                bridge(),
                SimpleNamespace(),
                "managed",
                guardian=guardian,
                contract=contract,
            )
            managed.ctx = SimpleNamespace(pages=pages)
            run_monitor = managed.monitor_once()
        else:
            monkeypatch.setattr(cli, "Guardian", lambda *args, **kwargs: guardian)
            monkeypatch.setattr(cli.Contract, "load", lambda *args: contract)
            monkeypatch.setattr(
                cli,
                "BrowserLifecycle",
                lambda *args: SimpleNamespace(
                    start=AsyncMock(return_value=SimpleNamespace(pages=pages)),
                    close=AsyncMock(),
                ),
            )
            monkeypatch.setattr(
                cli,
                "ControlServer",
                lambda *args: SimpleNamespace(start=Mock(), close=Mock()),
            )
            args = argparse.Namespace(
                contract=None,
                apply=True,
                chat=[page.url for page in pages],
                manual=False,
                profile_root=str(tmp_path),
                workspace="home",
                browser=None,
                token_file=str(tmp_path / "token"),
                control_port=0,
            )
            run_monitor = cli.serve(args, store)
        with pytest.raises(
            error, match="tab observation failed" if error is RuntimeError else None
        ):
            await run_monitor
        assert set(finished) == {page.url for page in pages}, (
            "sibling observation still running"
        )
        controls = Store(store.path).controls()
        key = "astra_quota" if signal == "quota" else "stop:" + pages[-1].url
        assert key in controls, (
            "successful sibling safety discarded after another tab failed"
        )
        guardian.run_once.assert_not_awaited()
        assert not store.status()["attempts"]

    asyncio.run(scenario())
