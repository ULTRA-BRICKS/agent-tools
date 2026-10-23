"""Authenticated loopback control, owned lifecycle and CLI boundary tests."""

import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt.cli import canonical_chat, ensure_token, main
from agenttools_chatgpt.core import Guardian, Observation, Store
from agenttools_chatgpt.service import ControlServer


@pytest.mark.parametrize("first_safety", ["", "draft", "running"])
def test_cli_reuses_slow_snapshots_and_keeps_initial_safety(
    tmp_path, monkeypatch, first_safety
):
    import argparse
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from agenttools_chatgpt import cli, core
    from agenttools_chatgpt.browser import Contract

    async def scenario():
        clock = SimpleNamespace(now=0.0, cycles=0)
        store = Store(tmp_path / "state.sqlite")
        guardian = Guardian(store, apply=bool(first_safety))
        results = []
        pages = []

        class Sample:
            def __init__(self, page, contract):
                self.page = page

            async def observe(self):
                self.page.count += 1
                clock.now += 1.2
                observed = self.page.observed
                if self.page is pages[0] and self.page.count == 1 and first_safety:
                    observed = replace(observed, **{first_safety: True})
                return observed

            async def send(self, expected):
                pytest.fail("initial safety evidence must prevent a send")

        for index in range(3):
            observed = Observation(
                f"https://chatgpt.com/c/slow{index}",
                "turn",
                "generation",
                status="Thinking failed",
                active=True,
                verified=True,
                model="Astra",
                ambiguous=False,
            )
            page = SimpleNamespace(
                url=observed.chat, is_closed=lambda: False, count=0, observed=observed
            )
            pages.append(page)
        if first_safety:
            guardian.stability[pages[0].url] = pages[0].observed, -21
            guardian.last_seen[pages[0].url] = 0
        original = guardian.run_once

        async def ingest(adapter, initial=None, observed_at=None):
            result = await original(adapter, initial=initial, observed_at=observed_at)
            results.append(result)
            if first_safety and len(results) == 1:
                assert pages[0].url not in guardian.stability
            return result

        guardian.run_once = ingest

        class Stop:
            def is_set(self):
                clock.cycles += 1
                return clock.cycles > (1 if first_safety else 8)

            async def wait(self):
                clock.now += 1
                raise asyncio.TimeoutError

            def set(self):
                pass

        monkeypatch.setattr(cli.asyncio, "Event", Stop)
        monkeypatch.setattr(core.time, "monotonic", lambda: clock.now)
        monkeypatch.setattr(cli, "Guardian", lambda *args, **kwargs: guardian)
        monkeypatch.setattr(cli, "PlaywrightAdapter", Sample)
        monkeypatch.setattr(
            cli.Contract, "load", lambda *args: Contract(verified_account_ui=True)
        )
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
        for page in pages[1:]:
            if first_safety:
                store.set_control("watch:" + page.url, "0")
        args = argparse.Namespace(
            contract=None,
            apply=bool(first_safety),
            chat=[page.url for page in pages],
            manual=False,
            profile_root=str(tmp_path),
            workspace="home",
            browser=None,
            token_file=str(tmp_path / "token"),
            control_port=0,
        )
        await cli.serve(args, store)
        if first_safety == "draft":
            assert "stop:" + pages[0].url in Store(store.path).controls()
        if not first_safety:
            assert "observe" in results
            assert all(page.count == 8 for page in pages)
        assert not store.status()["attempts"]

    asyncio.run(scenario())


def test_cli_control_closes_when_browser_cleanup_raises(tmp_path, monkeypatch):
    import argparse
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from agenttools_chatgpt import cli

    life = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError("startup failed")),
        close=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    control = SimpleNamespace(start=Mock(), close=Mock())
    monkeypatch.setattr(cli, "BrowserLifecycle", lambda *args: life)
    monkeypatch.setattr(cli, "ControlServer", lambda *args: control)
    args = argparse.Namespace(
        contract=None,
        apply=False,
        chat=["https://chatgpt.com/c/test"],
        manual=False,
        profile_root=str(tmp_path),
        workspace="home",
        browser=None,
        token_file=str(tmp_path / "token"),
        control_port=0,
    )
    with pytest.raises(RuntimeError, match="cleanup failed"):
        asyncio.run(cli.serve(args, Store(tmp_path / "state.sqlite")))
    control.close.assert_called_once()


def test_unauthenticated_controls_cannot_mutate_and_no_remote_enable(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    server = ControlServer(store, "x" * 32, 0)
    server.start()
    assert server.server.server_address[0] == "127.0.0.1"
    base = "http://127.0.0.1:" + str(server.server.server_port)
    try:
        request = urllib.request.Request(base + "/stop", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=3)
        assert error.value.code == 401
        assert not store.status()["controls"]
        request.add_header("Authorization", "Bearer " + "x" * 32)
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response) == {"ok": True}
        assert "stop:*" in store.status()["controls"]
        request = urllib.request.Request(
            base + "/enable",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer " + "x" * 32},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=3)
        assert error.value.code == 404
    finally:
        server.close()


@pytest.mark.parametrize(
    "raw",
    [
        "https://chatgpt.com/c/",
        "https://evil.test/c/id",
        "https://chatgpt.com/c/id?model=sol",
        "https://chatgpt.com/c/id#fragment",
    ],
)
def test_conversation_identity_is_canonical(raw):
    with pytest.raises(ValueError):
        canonical_chat(raw)


def test_cli_controls_and_secret_not_printed(tmp_path, capsys):
    path = tmp_path / "state.sqlite"
    assert main(["--state", str(path), "init"]) == 0
    token = ensure_token(tmp_path / "control.token")
    assert token not in capsys.readouterr().out
    assert (tmp_path / "control.token").stat().st_mode & 0o077 == 0
    assert main(["--state", str(path), "sol-opt-in", "https://chatgpt.com/c/test"]) == 0
    assert Store(path).status()["sol_chats"] == ["https://chatgpt.com/c/test"]
    with pytest.raises(SystemExit):
        main(
            [
                "--state",
                str(path),
                "run",
                "--workspace",
                "home",
                "--apply",
                "--chat",
                "https://chatgpt.com/c/test",
            ]
        )


def test_cli_workspace_defaults_isolate_controls_and_status(
    tmp_path, monkeypatch, capsys
):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    chat = "https://chatgpt.com/c/test"
    assert main(["--workspace", "home", "stop"]) == 0
    Store(
        tmp_path / ".local/state/agent-tools/chatgpt-guardian/home/state.sqlite"
    ).quota()
    assert main(["--workspace", "home", "sol-opt-in", chat]) == 0
    assert main(["--workspace", "glide.vc", "watch", chat]) == 0
    assert main(["--workspace", "glide.vc", "status"]) == 0
    glide = json.loads(capsys.readouterr().out)
    assert "stop:*" not in glide["controls"]
    assert "astra_quota" not in glide["controls"]
    assert glide["sol_chats"] == []
    assert glide["controls"]["watch:" + chat] == "1"
    assert main(["--workspace", "home", "status"]) == 0
    assert "stop:*" in json.loads(capsys.readouterr().out)["controls"]


def test_cli_explicit_state_cannot_be_shared_across_accounts(tmp_path):
    path = str(tmp_path / "shared.sqlite")
    assert main(["--workspace", "home", "--state", path, "init"]) == 0
    assert main(["--workspace", "home", "--state", path, "stop"]) == 0
    with pytest.raises(SystemExit) as error:
        main(["--workspace", "glide.vc", "--state", path, "status"])
    assert error.value.code == 2


def test_cli_run_workspace_alias_uses_same_state_as_controls(tmp_path, monkeypatch):
    from agenttools_chatgpt import cli

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    assert main(["--workspace", "glide.vc", "stop"]) == 0

    async def serve(args, store):
        assert args.workspace == "glide.vc"
        assert "stop:*" in store.controls()

    monkeypatch.setattr(cli, "serve", serve)
    assert main(["run", "--workspace", "glide.vc"]) == 0
    assert main(["--workspace", "glide.vc", "run"]) == 0
    with pytest.raises(SystemExit) as error:
        main(["--workspace", "home", "run", "--workspace", "glide.vc"])
    assert error.value.code == 2


def test_state_environment_override_is_account_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(tmp_path / "shared.sqlite"))
    assert main(["quota-clear"]) == 0
    Store(tmp_path / "shared.sqlite").quota()
    with pytest.raises(SystemExit) as error:
        main(["--workspace", "glide.vc", "quota-clear"])
    assert error.value.code == 2
    assert "astra_quota" in Store(tmp_path / "shared.sqlite").controls()


def test_scoped_sessions_share_quota_stops_and_consent(tmp_path):
    path = tmp_path / "state.sqlite"
    first = Store(path, account="home")
    first.quota()
    first.stop()
    first.opt_in_sol("https://chatgpt.com/c/test")
    second = Store(path, account="home")
    assert first.status() == second.status()


@pytest.mark.parametrize("explicit", [True, False])
def test_cli_refuses_existing_unscoped_state_without_mutation(
    tmp_path, monkeypatch, explicit
):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    path = tmp_path / ".local/state/agent-tools/chatgpt-guardian/state.sqlite"
    Store(path).stop()
    before = path.read_bytes()
    args = ["--state", str(path)] if explicit else []
    with pytest.raises(SystemExit) as error:
        main([*args, "status"])
    assert error.value.code == 2
    assert path.read_bytes() == before


def test_ha_legacy_default_requires_reconciliation(monkeypatch):
    from agenttools_chatgpt.state import state_path

    original_exists = Path.exists
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: path == Path("/data/guardian.sqlite3") or original_exists(path),
    )
    for workspace in ("home", "glide.vc"):
        with pytest.raises(ValueError, match="Legacy unscoped state"):
            state_path(workspace, ha=True)


def test_dispatch_serializes_manual_stop_until_click_finishes(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    observed = Observation(
        "chat",
        "turn",
        "g",
        status="Thinking failed",
        active=True,
        verified=True,
        model="Astra",
        ambiguous=False,
    )
    store.reserve(observed, 1000)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        with store.dispatch(observed) as allowed:
            assert allowed
            stopped = pool.submit(store.stop)
            time.sleep(0.03)
            assert not stopped.done()
        stopped.result(timeout=2)
    assert not store.can_send_reserved(observed)


def test_unknown_observation_and_poll_gap_reset_stability(tmp_path):
    guardian = Guardian(Store(tmp_path / "state.sqlite"), apply=True)
    observed = Observation(
        "chat",
        "turn",
        "g",
        status="Thinking failed",
        active=True,
        verified=True,
        model="Astra",
        ambiguous=False,
    )
    guardian.consider(observed, 0, 1000)
    assert guardian.consider(observed, 20, 1020) == "stabilizing"
    guardian.consider(Observation("", "", ""), 21, 1021)
    assert not guardian.stability


def test_human_draft_and_challenge_stop_persistently(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    guardian = Guardian(store, apply=True)
    guardian.consider(Observation("chat", "turn", "g", draft=True), 0, 1000)
    assert "stop:chat" in Store(store.path).status()["controls"]
    guardian.consider(Observation("", "", "", challenge=True), 1, 1001)
    assert "stop:*" in Store(store.path).status()["controls"]


def test_restart_enrollment_preserves_explicit_unwatch(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    chat = "https://chatgpt.com/c/chat"
    store.enroll(chat)
    store.set_control("watch:" + chat, "0")
    Store(store.path).enroll(chat)
    assert store.status()["controls"]["watch:" + chat] == "0"


def test_api_dispatch_checks_durable_stop_quota_and_unresolved_send(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    chat = "https://chatgpt.com/c/chat"
    store.stop(chat)
    with store.dispatch_api(chat) as allowed:
        assert not allowed
    store.resume(chat)
    store.quota()
    with store.dispatch_api(chat) as allowed:
        assert not allowed
    store.opt_in_sol(chat)
    with store.dispatch_api(chat, "Sol", "Extra High", True) as allowed:
        assert allowed
    with store.dispatch_api(chat, "Sol", "High", True) as allowed:
        assert not allowed


@pytest.mark.parametrize(
    "raw",
    [
        " https://chatgpt.com/c/id",
        "https://chatgpt.com/c/i\nd",
        "https://chatgpt.com/c/id?",
        "https://chatgpt.com/c/id#",
    ],
)
def test_noncanonical_parser_inputs_are_rejected(raw):
    with pytest.raises(ValueError):
        canonical_chat(raw)


def test_store_closes_connections_on_success_and_exception(tmp_path, monkeypatch):
    import sqlite3

    from agenttools_chatgpt import core

    connections = []
    original = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(core.sqlite3, "connect", connect)
    store = Store(tmp_path / "state.sqlite")
    store.status()
    assert all(connection.closed for connection in connections)
    with pytest.raises(ValueError), store.connect() as db:
        db.execute("INSERT INTO controls VALUES('rollback','1')")
        raise ValueError("test rollback")
    assert all(connection.closed for connection in connections)
    assert "rollback" not in store.status()["controls"]


def test_completed_unwatch_cancels_final_recovery(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    chat = "https://chatgpt.com/c/chat"
    obs = Observation(
        chat,
        "turn",
        "g",
        status="Thinking failed",
        active=True,
        verified=True,
        model="Astra",
        ambiguous=False,
    )
    store.enroll(chat)
    store.reserve(obs, 1000)
    store.set_control("watch:" + chat, "0")
    assert not store.can_send_reserved(obs)


def test_runtime_samples_wall_clock_after_write_lock(tmp_path, monkeypatch):
    import asyncio
    import time
    from contextlib import contextmanager
    from types import SimpleNamespace

    from agenttools_chatgpt import core

    acquired = False

    class ProbeStore(Store):
        @contextmanager
        def connect(self, timeout=10):
            with super().connect(timeout=timeout) as db:

                def trace(sql):
                    nonlocal acquired
                    if sql == "BEGIN IMMEDIATE":
                        acquired = True

                db.set_trace_callback(trace)
                yield db

    store = ProbeStore(tmp_path / "state.sqlite")
    obs = Observation(
        "chat",
        "turn",
        "g",
        status="Thinking failed",
        active=True,
        verified=True,
        model="Astra",
        ambiguous=False,
    )
    guardian = Guardian(store, apply=True)
    guardian.stability[obs.chat] = (obs, 0)
    guardian.last_seen[obs.chat] = time.monotonic()

    def clock():
        assert acquired, "Wall clock must be sampled after BEGIN IMMEDIATE"
        return 1000

    monkeypatch.setattr(
        core, "time", SimpleNamespace(monotonic=time.monotonic, time=clock)
    )

    class Browser:
        async def observe(self):
            return obs

        async def send(self, expected):
            return True

    assert asyncio.run(guardian.run_once(Browser())) == "sent"


def test_async_database_guard_keeps_event_loop_responsive(tmp_path):
    import asyncio
    import sqlite3
    import threading
    import time

    store = Store(tmp_path / "state.sqlite")
    held = threading.Event()

    def holder():
        with sqlite3.connect(store.path) as db:
            db.execute("BEGIN IMMEDIATE")
            held.set()
            time.sleep(0.25)

    thread = threading.Thread(target=holder)
    thread.start()

    async def case():
        await asyncio.to_thread(held.wait)
        ticks = []
        keep = True

        async def heartbeat():
            while keep:
                ticks.append(1)
                await asyncio.sleep(0.01)

        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        async with store.async_dispatch_api("https://chatgpt.com/c/test") as allowed:
            assert allowed
            assert len(ticks) >= 5
        keep = False
        await task

    try:
        asyncio.run(case())
    finally:
        thread.join(timeout=2)


def test_slow_final_observation_never_dispatches(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agenttools_chatgpt import core

    now = 0
    obs = Observation(
        "chat",
        "turn",
        "g",
        status="Thinking failed",
        active=True,
        verified=True,
        model="Astra",
        ambiguous=False,
    )
    store = Store(tmp_path / "state.sqlite")
    guardian = Guardian(store, apply=True)
    guardian.stability[obs.chat] = (obs, -20)
    guardian.last_seen[obs.chat] = 0
    monkeypatch.setattr(
        core, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: 1000)
    )

    class Browser:
        calls = 0
        sent = 0

        async def observe(self):
            nonlocal now
            self.calls += 1
            if self.calls == 2:
                now = 7
            return obs

        async def send(self, expected):
            self.sent += 1
            return True

    browser = Browser()
    assert asyncio.run(guardian.run_once(browser)) == "safety-stop-before-send"
    assert browser.sent == 0


def test_async_guard_cancellation_does_not_leave_a_lock(tmp_path):
    import asyncio
    import sqlite3

    store = Store(tmp_path / "state.sqlite")
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN IMMEDIATE")

    async def case():
        async def acquire():
            async with store.async_dispatch_api("https://chatgpt.com/c/test"):
                raise AssertionError("must still be waiting")

        task = asyncio.create_task(acquire())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(case())
    finally:
        blocker.rollback()
        blocker.close()
    store.stop()
    assert "stop:*" in store.controls()


def test_status_history_uses_index_and_controls_skip_history(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    with store.connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM attempts ORDER BY wall DESC LIMIT 100"
        ).fetchall()
    assert any("attempts_wall" in row[3] for row in plan)
    assert store.controls() == {}


def test_incomplete_requests_have_bounded_connection_and_workers(tmp_path):
    import socket

    server = ControlServer(Store(tmp_path / "state.sqlite"), "x" * 32, 0)
    server.start()
    client = socket.create_connection(server.server.server_address, timeout=2)
    try:
        assert server.server.connection_timeout <= 5
        assert server.server.max_handlers <= 16
        client.sendall(b"GET /status HTTP/1.1\r\nHost: local\r\n")
        client.settimeout(6)
        assert client.recv(1) == b""
    finally:
        client.close()
        server.close()


def test_incomplete_authenticated_body_times_out_and_concurrency_is_capped(tmp_path):
    import socket

    server = ControlServer(Store(tmp_path / "state.sqlite"), "x" * 32, 0)
    server.server.connection_timeout = 0.15
    server.server.max_handlers = 2
    server.server.slots = __import__("threading").BoundedSemaphore(2)
    server.start()
    clients = []
    try:
        for _ in range(2):
            client = socket.create_connection(server.server.server_address, timeout=2)
            clients.append(client)
            client.sendall(
                b"POST /stop HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer "
                + b"x" * 32
                + b"\r\nContent-Length: 100\r\n\r\n{"
            )
        time.sleep(0.04)
        extra = socket.create_connection(server.server.server_address, timeout=2)
        clients.append(extra)
        extra.settimeout(1)
        assert extra.recv(1) == b""
        time.sleep(0.2)
        assert not server.store.controls()
        for client in clients[:2]:
            client.settimeout(1)
            assert b"503" in client.recv(4096)
            assert client.recv(1) == b""
    finally:
        for client in clients:
            client.close()
        server.close()
