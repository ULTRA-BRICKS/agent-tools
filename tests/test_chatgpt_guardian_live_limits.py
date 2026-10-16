"""Incident #891 safety boundaries; fake CDP only, never Chrome or an account.

Exercises the live entry point and adapter with SQLite and fake response events.
Missing tabs must stay missing and observation must never become a UI action.
"""

import asyncio
import sys
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import cli, live
from agenttools_chatgpt.core import Observation, Store
from agenttools_chatgpt.live_browser import LiveAdapter

CHAT = "https://chatgpt.com/c/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROJECT = CHAT.replace("/c/", "/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/")
FAILURE = Observation(
    CHAT, "turn", "turn", status="Thinking failed", active=True, ambiguous=False
)


def page_at(url=PROJECT):
    return Mock(
        url=url,
        is_closed=Mock(return_value=False),
        goto=AsyncMock(),
        reload=AsyncMock(),
        bring_to_front=AsyncMock(),
        evaluate=AsyncMock(return_value=asdict(FAILURE)),
        locator=Mock(),
        keyboard=SimpleNamespace(press=AsyncMock()),
        evaluate_handle=AsyncMock(),
    )


def test_missing_closed_redirected_tabs_stay_missing_across_cycles_and_restart(
    tmp_path,
):
    async def scenario():
        store = Store(tmp_path / "state.sqlite", account="glide.vc")
        store.enroll(CHAT)
        page = page_at()
        context = SimpleNamespace(pages=[page], new_page=AsyncMock(return_value=page))
        assert await live.watched_pages(context, store, {CHAT: PROJECT}) == {
            CHAT: [page]
        }
        for state in ("closed", "redirected", "absent"):
            page.is_closed.return_value = state == "closed"
            page.url = "https://chatgpt.com/" if state == "redirected" else PROJECT
            context.pages = [] if state == "absent" else [page]
            for _ in range(3):
                restarted = Store(store.path, account="glide.vc")
                assert await live.watched_pages(context, restarted, {}) == {CHAT: []}
        context.new_page.assert_not_called()
        for action in (page.goto, page.reload, page.bring_to_front):
            action.assert_not_called()

    asyncio.run(scenario())


def test_passive_adapter_failure_never_touches_controls():
    async def scenario():
        page = page_at()
        adapter = LiveAdapter(page, allow_active_probe=False)
        for _ in range(8):
            assert await adapter.observe() == FAILURE
        page.locator.assert_not_called()
        page.keyboard.press.assert_not_called()
        page.evaluate_handle.assert_not_called()
        assert all(
            call.args[1].get("action", "observe") == "observe"
            for call in page.evaluate.call_args_list
        )
        assert not await adapter.send(FAILURE)
        assert not await adapter.select_sol(Mock(), CHAT)
        await adapter.probe_model()
        page.locator.assert_not_called()
        page.keyboard.press.assert_not_called()
        page.evaluate_handle.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("interval", [0, 1, 29, float("nan"), float("inf")])
def test_poll_interval_rejects_fast_or_nonfinite_values(tmp_path, interval):
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--state",
                str(tmp_path / "state.sqlite"),
                "live",
                "--cdp",
                "http://127.0.0.1:9223",
                "--poll-interval",
                str(interval),
            ]
        )


def test_poll_defaults_and_resource_limits(tmp_path, monkeypatch):
    captured = []

    async def serve(args, store):
        captured.append(args)

    monkeypatch.setattr(live, "serve_live", serve)
    cli.main(
        [
            "--state",
            str(tmp_path / "state.sqlite"),
            "live",
            "--cdp",
            "http://127.0.0.1:9223",
            "--once",
        ]
    )
    assert captured[0].poll_interval == 60
    assert captured[0].max_pages == 8
    assert captured[0].min_available_mib == 2048
    assert live.resource_pause(9, 3 * 1024**3) == "page-ceiling"
    assert live.resource_pause(8, 2 * 1024**3) == ""
    assert live.resource_pause(4, 2 * 1024**3 - 1) == "low-memory"
    assert live.resource_pause(4, None) == "memory-unavailable"


@pytest.mark.parametrize(
    "url,status,blocked",
    [
        ("https://chatgpt.com/backend-api/conversations?offset=0", 429, True),
        ("https://chatgpt.com/backend-api/f/conversation", 429, True),
        ("https://chatgpt.com/api/auth/session", 401, True),
        ("https://chatgpt.com/backend-api/models", 403, True),
        ("https://chatgpt.com.evil/backend-api/conversations", 429, False),
        ("https://evil/chatgpt.com/backend-api/conversations", 429, False),
        ("https://chatgpt.com:444/backend-api/conversations", 429, False),
        ("https://chatgpt.com/assets/image.png", 429, False),
        ("https://chatgpt.com/backend-api/conversation", 200, False),
    ],
)
def test_transport_events_durable_global_stop_and_detachment(
    tmp_path, url, status, blocked
):
    store = Store(tmp_path / "state.sqlite", account="glide.vc")
    context = Mock()
    guard = live.TransportGuard(context, store)
    callback = context.on.call_args.args[1]
    response = SimpleNamespace(url=url, status=status)
    callback(response)
    assert ("stop:*" in Store(store.path, account="glide.vc").controls()) == blocked
    if blocked:
        observed = Observation(
            **{**asdict(FAILURE), "verified": True, "model": "Astra"}
        )
        assert store.reserve(observed, None) == "stopped"
        callback(SimpleNamespace(url=url, status=200))
        assert "stop:*" in store.controls()
        store.resume("*")
        callback(response)
        assert "stop:*" in store.controls()
    guard.close()
    context.remove_listener.assert_called_once_with("response", callback)


def test_recent_performance_429_seeds_stop_without_navigation(tmp_path):
    async def scenario():
        store = Store(tmp_path / "state.sqlite", account="glide.vc")
        guard = live.TransportGuard(Mock(), store)
        page = page_at()
        page.evaluate.return_value = [
            {"url": "https://chatgpt.com/backend-api/conversations", "status": 429}
        ]
        await guard.seed(page)
        assert "stop:*" in store.controls()
        page.goto.assert_not_called()
        guard.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "condition",
    [
        "observe",
        "apply",
        "stop",
        "quota",
        "429",
        "challenge",
        "pages",
        "memory",
        "error",
    ],
)
def test_service_cycles_are_passive_bounded_and_detach(
    tmp_path, monkeypatch, capsys, condition
):
    """Drive serve_live itself; every Playwright boundary is in-memory."""
    from playwright import async_api

    async def scenario():
        store = Store(tmp_path / "state.sqlite", account="glide.vc")
        if condition == "stop":
            store.stop("*", "incident")
        if condition == "quota":
            store.quota()
        page = page_at()
        context = Mock(pages=[page])
        if condition == "pages":
            context.pages.extend(page_at("about:blank") for _ in range(8))

        async def evaluate(script, *args):
            if "getEntriesByType" in script:
                return []
            if condition == "429":
                callback = context.on.call_args.args[1]
                callback(
                    SimpleNamespace(
                        url="https://chatgpt.com/backend-api/conversation", status=429
                    )
                )
            if condition == "error":
                raise async_api.Error("fake transport failure")
            if condition == "challenge":
                return {**asdict(FAILURE), "challenge": True}
            return asdict(FAILURE)

        page.evaluate.side_effect = evaluate
        browser = Mock(contexts=[context])
        browser.is_connected.return_value = True
        driver = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)),
            stop=AsyncMock(),
        )
        monkeypatch.setattr(
            async_api,
            "async_playwright",
            lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
        )
        monkeypatch.setattr(live, "LiveOwner", lambda *args: nullcontext())
        control = Mock()
        monkeypatch.setattr(live, "ControlServer", lambda *args: control)
        monkeypatch.setattr(cli, "ensure_token", lambda *args: "fake-local-token")
        monkeypatch.setattr(
            live,
            "available_memory",
            lambda: 1024**3 if condition == "memory" else 3 * 1024**3,
        )

        class Stop:
            cycles = 0

            def is_set(self):
                return self.cycles >= 2

            def set(self):
                self.cycles = 2

            async def wait(self):
                self.cycles += 1
                raise asyncio.TimeoutError

        stop = Stop()
        monkeypatch.setattr(live.asyncio, "Event", lambda: stop)
        real_wait = asyncio.wait_for
        delays = []

        async def wait_for(coro, timeout):
            if coro.cr_code is Stop.wait.__code__:
                delays.append(timeout)
            return await real_wait(coro, timeout)

        monkeypatch.setattr(live.asyncio, "wait_for", wait_for)
        args = SimpleNamespace(
            cdp="http://127.0.0.1:9223",
            watch=[PROJECT],
            apply=condition != "observe",
            control_port=0,
            workspace="glide.vc",
            once=False,
            poll_interval=30,
        )
        await live.serve_live(args, store)
        assert delays == ([60, 60] if condition == "error" else [30, 30])
        page.locator.assert_not_called()
        page.keyboard.press.assert_not_called()
        page.evaluate_handle.assert_not_called()
        page.goto.assert_not_called()
        page.reload.assert_not_called()
        page.bring_to_front.assert_not_called()
        context.new_page.assert_not_called()
        context.close.assert_not_called()
        browser.close.assert_not_called()
        driver.stop.assert_awaited_once()
        context.remove_listener.assert_called_once_with(
            "response", context.on.call_args.args[1]
        )
        control.close.assert_called_once()
        assert not store.status()["attempts"]
        if condition in ("pages", "memory"):
            page.evaluate.assert_not_called()
        if condition in ("stop", "429"):
            assert "stop:*" in Store(store.path, account="glide.vc").controls()
        if condition == "challenge":
            assert store.controls()["stop:" + CHAT] == "human challenge"

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert output.count('"health":') == 1
    if condition == "apply":
        assert '"state": "recovery-confirmation-disabled"' in output


def test_memavailable_reader_is_cheap_and_fails_closed(monkeypatch):
    monkeypatch.setattr(
        Path, "read_text", lambda self: "MemFree: 1 kB\nMemAvailable: 2097152 kB\n"
    )
    assert live.available_memory() == 2 * 1024**3
    monkeypatch.setattr(Path, "read_text", lambda self: "MemAvailable: unknown kB\n")
    assert live.available_memory() is None
