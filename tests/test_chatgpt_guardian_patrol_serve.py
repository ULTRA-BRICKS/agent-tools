"""`live --single-tab` service wiring with every Playwright boundary in memory.

Proves what the service does around the patrol: validation before any browser work, one owner
lock, the account lock held while connected, no startup burst, clean teardown that never closes
Chrome, and the durable marker and rotation file.
"""

import asyncio
import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import cli, live, patrol, rotation
from agenttools_chatgpt.core import Store
from agenttools_chatgpt.lock import AccountBusy, account_lock

ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/" + ID
CHAT = "https://chatgpt.com/c/" + ID


def args_for(**over):
    values = {
        "cdp": "http://127.0.0.1:9226",
        "watch": [PROJECT],
        "apply": True,
        "control_port": 0,
        "workspace": "home",
        "once": True,
        "poll_interval": 60,
        "private_display": False,
        "rotation_min": None,
        "rotation_max": None,
        "confirm_wait": None,
        "max_pages": 6,
        "min_available_mib": 2048,
    }
    values.update(over)
    return SimpleNamespace(**values)


class Boundary:
    """Fake Playwright, owner lock and control server for one serve_single_tab run."""

    def __init__(self, monkeypatch, state, *, contexts=1, connected=True):
        from playwright import async_api

        self.state = state
        self.context = Mock(pages=[])
        self.browser = Mock(contexts=[self.context] * contexts)
        self.browser.is_connected.return_value = connected
        self.lock_held_while_connected = None
        self.attached = []

        async def connect(endpoint, **kwargs):
            self.attached.append((endpoint, kwargs))
            try:
                with account_lock(state):
                    self.lock_held_while_connected = False
            except AccountBusy:
                self.lock_held_while_connected = True
            return self.browser

        self.driver = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=connect), stop=AsyncMock()
        )
        self.started = AsyncMock(return_value=self.driver)
        self.playwright = Mock(side_effect=lambda: SimpleNamespace(start=self.started))
        monkeypatch.setattr(async_api, "async_playwright", self.playwright)
        monkeypatch.setattr(live, "LiveOwner", lambda *args: nullcontext())
        self.control = Mock()
        monkeypatch.setattr(patrol, "ControlServer", lambda *a, **k: self.control)
        monkeypatch.setattr(cli, "ensure_token", lambda *a: "fake-local-token")


def serve(monkeypatch, tmp_path, **over):
    store = Store(tmp_path / "home" / "state.sqlite", account="home")
    boundary = Boundary(
        monkeypatch,
        store.path,
        contexts=over.pop("contexts", 1),
        connected=over.pop("connected", True),
    )
    asyncio.run(patrol.serve_single_tab(args_for(**over), store))
    return store, boundary


def test_first_service_start_connects_once_and_visits_nothing(
    tmp_path, monkeypatch, capsys
):
    before = time.time()
    store, b = serve(monkeypatch, tmp_path)
    assert b.attached == [
        ("http://127.0.0.1:9226", {"timeout": 10000, "no_defaults": True})
    ]
    assert (
        b.lock_held_while_connected is True
    )  # coverage/startup apply find the account busy
    b.context.new_page.assert_not_called()  # no startup burst, no navigation
    b.context.close.assert_not_called()
    b.browser.close.assert_not_called()
    b.driver.stop.assert_awaited_once()
    b.context.remove_listener.assert_called_once_with(
        "response", b.context.on.call_args.args[1]
    )
    b.control.start.assert_called_once()
    b.control.close.assert_called_once()
    assert store.controls()[rotation.MODE_CONTROL] == "1"
    with account_lock(store.path):  # released when the service ends
        pass
    machine = rotation.Rotation.read(rotation.rotation_path(store.path), "home")
    assert [e.chat for e in machine.queue] == [CHAT]
    assert machine.queue[0].url == PROJECT  # the full permanent project URL is stored
    assert before + 240 <= machine.next_due <= time.time() + 360
    line = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    health = next(x for x in line if "health" in x)
    assert health["health"] == "connected" and health["mode"] == "single-tab"
    assert health["queue"] == 1 and health["workspace"] == "home"


def test_restart_after_downtime_redraws_the_overdue_due_time_and_does_not_burst(
    tmp_path, monkeypatch
):
    state = tmp_path / "home" / "state.sqlite"
    Store(state, account="home")
    machine = rotation.Rotation.open(rotation.rotation_path(state), "home")
    machine.next_due = time.time() - 36000
    machine.save()
    _store, b = serve(monkeypatch, tmp_path)
    b.context.new_page.assert_not_called()
    again = rotation.Rotation.read(rotation.rotation_path(state), "home")
    assert again.next_due >= time.time() + 239


def test_wrong_account_rotation_state_refuses_before_any_browser_work(
    tmp_path, monkeypatch
):
    state = tmp_path / "home" / "state.sqlite"
    Store(state, account="home")
    rotation.Rotation.open(rotation.rotation_path(state), "glide.vc")  # a foreign file
    with pytest.raises(rotation.RotationError, match="different account"):
        serve(monkeypatch, tmp_path)
    assert rotation.MODE_CONTROL not in Store(state, account="home").controls()


@pytest.mark.parametrize("payload", ["{ broken", '{"schema": 1}', "[]"])
def test_corrupt_rotation_state_refuses_and_is_left_for_the_owner(
    tmp_path, monkeypatch, payload
):
    state = tmp_path / "home" / "state.sqlite"
    Store(state, account="home")
    path = rotation.rotation_path(state)
    path.write_text(payload)
    with pytest.raises(rotation.RotationError):
        serve(monkeypatch, tmp_path)
    assert path.read_text() == payload


def test_an_unexpected_context_count_fails_closed_and_still_cleans_up(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "home" / "state.sqlite", account="home")
    boundary = Boundary(monkeypatch, store.path, contexts=2)
    with pytest.raises(RuntimeError, match="one dedicated"):
        asyncio.run(patrol.serve_single_tab(args_for(), store))
    boundary.driver.stop.assert_awaited_once()
    boundary.control.close.assert_called_once()
    with account_lock(store.path):
        pass


def test_a_disconnected_browser_is_a_failure_not_a_silent_loop(tmp_path, monkeypatch):
    store = Store(tmp_path / "home" / "state.sqlite", account="home")
    boundary = Boundary(monkeypatch, store.path, connected=False)
    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(patrol.serve_single_tab(args_for(), store))
    boundary.driver.stop.assert_awaited_once()


def test_observe_only_service_never_enables_continuations(tmp_path, monkeypatch):
    store = Store(tmp_path / "home" / "state.sqlite", account="home")
    Boundary(monkeypatch, store.path)
    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return Mock()

    monkeypatch.setattr(patrol, "ControlServer", capture)
    asyncio.run(patrol.serve_single_tab(args_for(apply=False), store))
    assert seen["continuations"] is False


def test_a_429_response_through_the_real_transport_guard_starts_the_durable_cooldown(
    tmp_path, monkeypatch
):
    store, b = serve(monkeypatch, tmp_path)
    callback = b.context.on.call_args.args[1]
    response = SimpleNamespace(
        url="https://chatgpt.com/backend-api/conversation",
        status=429,
        headers={"retry-after": "600"},
    )
    callback(response)
    machine = rotation.Rotation.read(rotation.rotation_path(store.path), "home")
    assert machine.cooldown["source"] == "retry-after"
    assert machine.cooldown["until"] == pytest.approx(time.time() + 600, abs=5)
    assert store.controls()["stop:*"] == patrol.MACHINE_429_HOLD
