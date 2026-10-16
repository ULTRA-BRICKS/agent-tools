"""Incident #891 safety boundaries; fake CDP only, never Chrome or an account.

Exercises the live entry point and adapter with SQLite and fake response events.
Missing tabs stay missing; only an apply failure burst can authorize UI actions.
"""

import asyncio
import json
import sys
import time
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import cli, core, live
from agenttools_chatgpt.core import Guardian, Observation, Store
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


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "default",
        "idle",
        "draft",
        "running",
        "pages",
        "transport",
        "stop",
        "during-stop",
        "during-draft",
        "during-url",
        "during-pages",
        "during-transport",
    ],
)
def test_private_display_activation_is_opt_in_and_rechecks_safety(
    tmp_path, monkeypatch, change
):
    async def scenario():
        store = Store(tmp_path / "db", account="glide.vc")
        store.enroll(CHAT)
        page = page_at()
        context = SimpleNamespace(pages=[page])
        transport = SimpleNamespace(reason="")
        stop = asyncio.Event()
        observed = FAILURE
        if change in ("draft", "running"):
            observed = replace(observed, **{change: True})
        if change == "idle":
            observed = replace(observed, status="Complete")
        if change == "pages":
            context.pages.extend(page_at("about:blank") for _ in range(6))
        if change == "transport":
            transport.reason = "lost"
        if change == "stop":
            stop.set()
        passive = SimpleNamespace(observe=AsyncMock(side_effect=lambda: observed))
        active = SimpleNamespace(
            observe=AsyncMock(return_value=FAILURE), send=AsyncMock()
        )
        monkeypatch.setattr(
            live,
            "LiveAdapter",
            lambda *a, **kw: (
                passive if kw.get("allow_active_probe") is False else active
            ),
        )
        monkeypatch.setattr(live, "available_memory", lambda: 3 * 1024**3)

        async def activate():
            nonlocal observed
            await asyncio.sleep(0)
            if change == "during-stop":
                stop.set()
            if change == "during-draft":
                observed = replace(observed, draft=True)
            if change == "during-url":
                page.url = CHAT
            if change == "during-pages":
                context.pages.extend(page_at("about:blank") for _ in range(6))
            if change == "during-transport":
                transport.reason = "lost"

        page.bring_to_front.side_effect = activate
        kwargs = {} if change == "default" else {"private_display": True}
        await live.confirm_failure(
            Guardian(store, apply=True),
            page,
            FAILURE,
            context,
            transport,
            stop,
            max_pages=6,
            min_available_mib=2048,
            **kwargs,
        )
        assert page.bring_to_front.await_count == (
            1 if change == "none" or change.startswith("during-") else 0
        )
        if change.startswith("during-") or change in (
            "idle",
            "draft",
            "running",
            "pages",
            "transport",
            "stop",
        ):
            active.observe.assert_not_awaited()
        active.send.assert_not_awaited()
        assert not store.status()["attempts"]

    asyncio.run(scenario())


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


@pytest.mark.parametrize("private_display", [False, True])
def test_poll_defaults_and_resource_limits(tmp_path, monkeypatch, private_display):
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
            *(["--private-display"] if private_display else []),
        ]
    )
    assert captured[0].poll_interval == 60
    assert captured[0].max_pages == 6
    assert captured[0].min_available_mib == 2048
    assert captured[0].private_display is private_display
    assert live.resource_pause(7, 3 * 1024**3) == "page-ceiling"
    assert live.resource_pause(6, 2 * 1024**3) == ""
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
        "burst",
        "empty-sibling",
        "empty-sibling-429",
        "empty-sibling-pages",
    ],
)
@pytest.mark.parametrize("private_display", [False, True])
def test_service_cycles_are_passive_bounded_and_detach(
    tmp_path, monkeypatch, capsys, condition, private_display
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
        empty_first = condition.startswith("empty-sibling")
        if condition in ("pages", "empty-sibling-pages"):
            context.pages.extend(page_at("about:blank") for _ in range(8))

        async def evaluate(script, *args):
            if "getEntriesByType" in script:
                return []
            if condition in ("429", "empty-sibling-429"):
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
            if empty_first:
                return {}
            return asdict(FAILURE)

        page.evaluate.side_effect = evaluate
        clock = [100.0]
        bursts = []
        if condition == "burst" or empty_first:
            second_chat = CHAT.replace(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            )
            second = page_at(second_chat)

            async def second_evaluate(script, *args):
                return (
                    []
                    if "getEntriesByType" in script
                    else asdict(
                        replace(
                            FAILURE,
                            chat=second_chat,
                            verified=empty_first,
                            model="Sol" if empty_first else "",
                        )
                    )
                )

            second.evaluate.side_effect = second_evaluate
            context.pages.append(second)
            store.enroll(second_chat)
            if empty_first:
                watched_pages = live.watched_pages

                async def ordered_pages(*args):
                    pages = await watched_pages(*args)
                    return {CHAT: pages[CHAT], second_chat: pages[second_chat]}

                monkeypatch.setattr(live, "watched_pages", ordered_pages)
            monkeypatch.setattr(
                live, "time", SimpleNamespace(monotonic=lambda: clock[0])
            )
            confirm = live.confirm_failure

            async def burst(*args, **kwargs):
                bursts.append((args[2].chat, clock[0]))
                result = (
                    "confirmation-blocked"
                    if empty_first
                    else await confirm(*args, **kwargs)
                )
                assert result == "confirmation-blocked"
                clock[0] += 25
                return result

            monkeypatch.setattr(live, "confirm_failure", burst)
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
        # Missing model proof stops the burst after its first active sample.
        monkeypatch.setattr(LiveAdapter, "probe_model", AsyncMock())
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
                if condition == "burst" or empty_first:
                    clock[0] += timeout
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
            private_display=private_display,
        )
        await live.serve_live(args, store)
        assert delays == [60, 60]
        page.locator.assert_not_called()
        page.keyboard.press.assert_not_called()
        page.evaluate_handle.assert_not_called()
        page.goto.assert_not_called()
        page.reload.assert_not_called()
        activations = (
            (2 if condition == "burst" else 1)
            if private_display and condition in ("apply", "quota", "burst")
            else 0
        )
        if condition == "burst" and private_display:
            assert (
                page.bring_to_front.await_count + second.bring_to_front.await_count
                == activations
            )
        else:
            assert page.bring_to_front.await_count == activations
        if not activations:
            page.bring_to_front.assert_not_called()
            if condition == "burst":
                second.bring_to_front.assert_not_called()
        context.new_page.assert_not_called()
        context.close.assert_not_called()
        browser.close.assert_not_called()
        driver.stop.assert_awaited_once()
        context.remove_listener.assert_called_once_with(
            "response", context.on.call_args.args[1]
        )
        control.close.assert_called_once()
        assert not store.status()["attempts"]
        if condition == "burst":
            # Two eligible pages each cycle still produce one serialized burst;
            # the next starts 60 seconds after the previous 25-second burst ends.
            assert [chat for chat, _ in bursts] in (
                [CHAT, second_chat],
                [second_chat, CHAT],
            )
            assert [started for _, started in bursts] == [100, 185]
        if empty_first:
            assert bursts == (
                [(second_chat, 100), (second_chat, 185)]
                if condition == "empty-sibling"
                else []
            )
            LiveAdapter.probe_model.assert_not_awaited()
        if condition in ("pages", "memory", "empty-sibling-pages"):
            page.evaluate.assert_not_called()
        if condition in ("stop", "429", "empty-sibling-429"):
            assert "stop:*" in Store(store.path, account="glide.vc").controls()
        if condition == "challenge":
            assert store.controls()["stop:" + CHAT] == "human challenge"

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert output.count('"health":') == 1
    if condition == "apply":
        assert '"state": "confirmation-blocked"' in output
    if condition == "empty-sibling":
        assert '"state": "observation-unverified"' in output


@pytest.mark.parametrize("replacement", ["turn", "generation", "chat"])
def test_confirmation_sol_selection_preserves_candidate_at_first_snapshot(
    tmp_path, monkeypatch, replacement
):
    async def scenario():
        store = Store(tmp_path / "db", account="glide.vc")
        store.enroll(CHAT)
        store.opt_in_sol(CHAT)
        store.quota()
        candidate = replace(FAILURE, model="Astra", verified=True)
        changed = replace(candidate, **{replacement: "replacement"})
        page = page_at()
        steps = []

        async def evaluate(script, payload):
            if payload.get("action") == "selection-step":
                steps.append(payload["target"])
                return True
            return asdict(changed)

        page.evaluate.side_effect = evaluate
        page.evaluate_handle.return_value = SimpleNamespace(dispose=AsyncMock())
        monkeypatch.setattr(
            LiveAdapter,
            "observe",
            AsyncMock(side_effect=[candidate, candidate, changed]),
        )
        monkeypatch.setattr(
            LiveAdapter,
            "_menu_state",
            AsyncMock(
                return_value={
                    "open": False,
                    "selected": ["GPT-6"],
                    "effort": ["Extra High, 4 of 5."],
                }
            ),
        )
        monkeypatch.setattr(LiveAdapter, "_probe_model", AsyncMock())
        monkeypatch.setattr(live, "available_memory", lambda: 3 * 1024**3)
        monkeypatch.setattr(live, "wait_stop", AsyncMock(return_value=False))
        assert (
            await live.confirm_failure(
                Guardian(store, apply=True),
                page,
                candidate,
                SimpleNamespace(pages=[page]),
                SimpleNamespace(reason=""),
                asyncio.Event(),
                max_pages=6,
                min_available_mib=2048,
            )
            == "confirmation-blocked"
        )
        assert steps == []
        page.evaluate_handle.assert_not_called()
        page.keyboard.press.assert_not_called()
        assert not store.status()["attempts"]

    asyncio.run(scenario())


def test_memavailable_reader_is_cheap_and_fails_closed(monkeypatch):
    monkeypatch.setattr(
        Path, "read_text", lambda self: "MemFree: 1 kB\nMemAvailable: 2097152 kB\n"
    )
    assert live.available_memory() == 2 * 1024**3
    monkeypatch.setattr(Path, "read_text", lambda self: "MemAvailable: unknown kB\n")
    assert live.available_memory() is None


def configured_state(tmp_path, monkeypatch, payload=None, *, create_legacy=True):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    path = tmp_path / "live.sqlite"
    store = Store(path, account="glide.vc")
    store.stop("*", "retained")
    with store.connect() as db:
        db.execute(
            "INSERT INTO attempts VALUES(?,?,?,?,?)",
            (CHAT, "old", "old", 1, "uncertain"),
        )
    legacy = tmp_path / ".local/state/agent-tools/chatgpt-guardian/state.sqlite"
    if create_legacy:
        legacy.parent.mkdir(parents=True)
        legacy.touch()
    config = tmp_path / ".config/agent-tools/chatgpt-guardian/glide.vc.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(payload or {"workspace": "glide.vc", "state": str(path)})
    )
    return store, legacy


def test_default_config_uses_existing_controls_attempts_and_live_state(
    tmp_path, monkeypatch, capsys
):
    store, legacy = configured_state(tmp_path, monkeypatch)
    cli.main(["--workspace", "glide.vc", "status"])
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == str(store.path)
    assert status["controls"]["stop:*"] == "retained"
    assert status["attempts"][0]["state"] == "uncertain"
    captured = []

    async def serve(args, selected):
        captured.append(selected.path)

    monkeypatch.setattr(live, "serve_live", serve)
    cli.main(
        ["--workspace", "glide.vc", "live", "--cdp", "http://127.0.0.1:9223", "--once"]
    )
    assert captured == [store.path]
    assert legacy.read_bytes() == b""


@pytest.mark.parametrize(
    "bad", ["workspace", "missing", "relative", "empty", "scope", "json", "schema"]
)
def test_config_refuses_without_creating_replacement(tmp_path, monkeypatch, bad):
    store, legacy = configured_state(tmp_path, monkeypatch, create_legacy=False)
    config = tmp_path / ".config/agent-tools/chatgpt-guardian/glide.vc.json"
    payload = {"workspace": "glide.vc", "state": str(store.path)}
    if bad == "workspace":
        payload["workspace"] = "home"
    if bad == "missing":
        payload["state"] = str(tmp_path / "missing.sqlite")
    if bad == "relative":
        payload["state"] = "live.sqlite"
    if bad == "empty":
        empty = tmp_path / "empty.sqlite"
        empty.touch()
        payload["state"] = str(empty)
    if bad == "scope":
        with store.connect() as db:
            db.execute("UPDATE account_scope SET account='home'")
    if bad == "schema":
        with store.connect() as db:
            db.execute("DROP TABLE attempts")
    config.write_text("{" if bad == "json" else json.dumps(payload))
    with pytest.raises(SystemExit) as failure:
        cli.main(["--workspace", "glide.vc", "status"])
    assert failure.value.code == 2
    assert not (tmp_path / "missing.sqlite").exists()
    assert not legacy.exists()
    assert not (legacy.parent / "glide.vc/state.sqlite").exists()


def test_explicit_state_then_environment_override_config(tmp_path, monkeypatch):
    from agenttools_chatgpt.state import state_path

    configured, _ = configured_state(tmp_path, monkeypatch)
    environment = tmp_path / "environment.sqlite"
    explicit = tmp_path / "explicit.sqlite"
    assert state_path("glide.vc") == configured.path
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(environment))
    assert state_path("glide.vc") == environment
    assert state_path("glide.vc", explicit) == explicit


@pytest.mark.parametrize("limit", [0, 7, -1, 100])
def test_page_cap_cli_refuses_outside_one_to_six(tmp_path, monkeypatch, limit):
    serve = AsyncMock()
    monkeypatch.setattr(live, "serve_live", serve)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--state",
                str(tmp_path / "db"),
                "live",
                "--cdp",
                "http://127.0.0.1:9223",
                "--max-pages",
                str(limit),
            ]
        )
    serve.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "draft",
        "turn",
        "running",
        "mode",
        "url",
        "duplicate",
        "pages",
        "memory",
        "429",
        "stop",
        "chat-stop",
        "cancel",
        "unverified",
        "historical",
        "nonfailure",
        "disabled",
        "gap",
        "uncertain",
    ],
)
def test_confirmation_real_guardian_bounded_and_fails_closed(
    tmp_path, monkeypatch, change
):
    async def scenario():
        store = Store(tmp_path / "db", account="glide.vc")
        store.enroll(CHAT)
        guardian = Guardian(store, apply=change != "disabled")
        page = page_at()
        context = SimpleNamespace(pages=[page])
        transport = SimpleNamespace(reason="")
        stop = asyncio.Event()
        clock = [100.0]
        fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        monkeypatch.setattr(live, "time", fake_time)
        monkeypatch.setattr(core, "time", fake_time)
        monkeypatch.setattr(live, "available_memory", lambda: 3 * 1024**3)
        observed = replace(FAILURE, verified=True, model="Astra")
        if change == "historical":
            observed = replace(observed, active=False)
        if change == "nonfailure":
            observed = replace(observed, status="Complete")
        adapter = SimpleNamespace(
            observe=AsyncMock(side_effect=lambda: observed),
            send=AsyncMock(return_value=change != "uncertain"),
            select_sol=AsyncMock(return_value=False),
        )
        monkeypatch.setattr(live, "LiveAdapter", lambda *args, **kwargs: adapter)
        waits = []

        async def wait_stop(event, delay):
            nonlocal observed
            waits.append(delay)
            clock[0] += 7 if change == "gap" else delay
            if change in ("draft", "running"):
                observed = replace(observed, **{change: True})
            elif change == "turn":
                observed = replace(observed, turn="changed")
            elif change == "mode":
                observed = replace(observed, model="Sol", effort="Extra High")
            elif change == "url":
                page.url = CHAT
            elif change in ("pages", "duplicate"):
                context.pages.extend(
                    page_at(PROJECT if change == "duplicate" else "about:blank")
                    for _ in range(6 if change == "pages" else 1)
                )
            elif change == "memory":
                monkeypatch.setattr(live, "available_memory", lambda: 1)
            elif change in ("429", "stop", "chat-stop"):
                store.stop(CHAT if change == "chat-stop" else "*", change)
                transport.reason = "429" if change == "429" else ""
            elif change == "cancel":
                event.set()
            return event.is_set()

        monkeypatch.setattr(live, "wait_stop", wait_stop, raising=False)
        if change == "unverified":
            observed = replace(observed, verified=False)
        result = await live.confirm_failure(
            guardian,
            page,
            replace(observed, verified=False),
            context,
            transport,
            stop,
            max_pages=6,
            min_available_mib=2048,
        )
        assert sum(waits) <= 35
        assert all(delay <= 3 for delay in waits)
        if change in ("none", "uncertain"):
            assert result == ("sent" if change == "none" else "uncertain-send")
            adapter.send.assert_awaited_once()
            assert store.status()["attempts"][0]["state"] == (
                "sent" if change == "none" else "uncertain"
            )
            assert await live.confirm_failure(
                guardian,
                page,
                replace(observed, verified=False),
                context,
                transport,
                stop,
                max_pages=6,
                min_available_mib=2048,
            ) == ("deduplicated" if change == "none" else "unresolved-send")
            assert adapter.send.await_count == 1
        else:
            adapter.send.assert_not_called()
            assert not store.status()["attempts"]
        if change in ("historical", "nonfailure", "disabled"):
            adapter.observe.assert_not_called()
        if change == "unverified":
            assert len(waits) == 0

    asyncio.run(scenario())
