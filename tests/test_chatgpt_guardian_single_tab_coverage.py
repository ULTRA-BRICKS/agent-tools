"""While the single-tab patrol runs, the legacy tab openers cannot reopen the queue; fakes only.

`coverage --apply` and `startup --apply` take the per-account lock before they open any tab. The
patrol holds that lock for its whole life, so both find the account busy and open nothing.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import cli, live, patrol, rotation, startup
from agenttools_chatgpt.core import Store
from agenttools_chatgpt.coverage import Ledger, ledger_path
from agenttools_chatgpt.lock import AccountBusy, account_lock
from test_chatgpt_guardian_coverage import (
    ALL_ON,
    FJ,
    HOME,
    OLD,
    FakeDevTools,
    FakeProcesses,
    make_evidence,
    run,
)


@pytest.fixture(autouse=True)
def ample_memory(monkeypatch):
    monkeypatch.setattr(live, "available_memory", lambda: 8 * 1024**3)


def test_without_the_patrol_legacy_coverage_still_restores_missing_chats(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ))
    report, devtools, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert report["plan"]["restore"] == ["fj-finish"]
    assert devtools.created == [FJ]


def test_while_the_patrol_holds_the_account_lock_coverage_apply_opens_nothing(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ))
    with account_lock(state):  # what the running patrol holds
        report, devtools, *_ = run(state, config=ALL_ON, devtools=FakeDevTools([OLD]))
    assert report["busy"] is True
    assert devtools.created == [] and report["effects"]["opened"] == []
    assert report["plan"]["restore"] == []
    assert (
        Ledger(ledger_path(state), "home").restore_rows() == {}
    )  # no attempt was spent


def test_the_dry_run_report_is_still_available_next_to_the_patrol(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ))
    with account_lock(state):
        report, devtools, *_ = run(state, apply=False, devtools=FakeDevTools([OLD]))
    assert report["busy"] is False and devtools.created == []


def test_startup_restore_after_a_browser_restart_is_refused_while_the_patrol_runs(
    tmp_path, capsys
):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ))
    devtools = FakeDevTools([OLD])
    with account_lock(state):
        code = startup.main(
            ["--workspace", "home", "--apply"],
            processes=FakeProcesses("home", HOME),
            devtools=devtools,
            state=state,
            home=HOME,
            lock_wait=0.0,
        )
    assert code == 1 and devtools.created == []
    assert "account lock" in json.loads(capsys.readouterr().out)["error"]


# --- the patrol takes and releases that lock --------------------------------------------------


def test_the_patrol_waits_for_a_running_coverage_then_holds_the_lock(tmp_path):
    import asyncio
    from contextlib import ExitStack

    state = tmp_path / "state.sqlite"
    Store(state, account="home")

    async def case():
        stack = ExitStack()
        busy = ExitStack()
        busy.enter_context(account_lock(state))
        stop = asyncio.Event()
        waiting = asyncio.ensure_future(
            patrol._hold_account(stack, Store(state, account="home"), stop)
        )
        await asyncio.sleep(0.05)
        assert not waiting.done()  # coverage still holds it: wait, never steal
        busy.close()
        await asyncio.wait_for(waiting, timeout=5)
        with pytest.raises(AccountBusy), account_lock(state):
            pass
        stack.close()
        with account_lock(state):  # released with the patrol
            pass

    asyncio.run(case())


def test_the_patrol_refuses_to_start_when_the_lock_never_frees_or_stop_is_set(
    tmp_path, monkeypatch
):
    import asyncio
    from contextlib import ExitStack

    state = tmp_path / "state.sqlite"
    store = Store(state, account="home")
    monkeypatch.setattr(patrol, "ACCOUNT_LOCK_WAIT", 0.0)

    async def case():
        with account_lock(state), pytest.raises(RuntimeError, match="account lock"):
            await patrol._hold_account(ExitStack(), store, asyncio.Event())

    asyncio.run(case())


# --- the CLI owns the marker's life cycle ------------------------------------------------------


def live_argv(state, *extra):
    return [
        "--workspace",
        "home",
        "--state",
        str(state),
        "live",
        "--cdp",
        "http://127.0.0.1:9224",
        *extra,
    ]


def marked(state):
    Store(state, account="home").set_control(rotation.MODE_CONTROL, "1")


def recorded(state):
    return Store(state, account="home").controls().get(rotation.MODE_CONTROL)


def refused(argv):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "flags", [[], ["--retire-single-tab"]], ids=["plain", "rollback"]
)
def test_a_competing_legacy_launch_while_the_patrol_holds_the_lock_leaves_the_marker(
    tmp_path, monkeypatch, flags
):
    """The marker is the single-tab veto: a launch that loses the owner lock must not erase it."""
    state = make_evidence(tmp_path, enrolled=(OLD, FJ))
    marked(state)
    legacy = AsyncMock()
    monkeypatch.setattr(live, "serve_live", legacy)
    with account_lock(state):  # what the running patrol holds for its whole life
        refused(live_argv(state, *flags))
        assert recorded(state) == "1"
        report, devtools, *_ = run(state, config=ALL_ON, devtools=FakeDevTools([OLD]))
        assert report["busy"] is True and devtools.created == []  # coverage still busy
    legacy.assert_not_called()
    assert recorded(state) == "1"


def test_a_legacy_launch_never_retires_the_marker_implicitly(tmp_path, monkeypatch):
    """No patrol holds the lock, but the mode is still recorded: refuse, do not erase it."""
    state = make_evidence(tmp_path, enrolled=(OLD,))
    marked(state)
    legacy = AsyncMock()
    monkeypatch.setattr(live, "serve_live", legacy)
    refused(live_argv(state))
    legacy.assert_not_called()
    assert recorded(state) == "1"


def test_a_launch_that_would_lose_the_owner_lock_never_reaches_the_marker(
    tmp_path, monkeypatch
):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    marked(state)
    monkeypatch.setattr(
        live, "serve_live", AsyncMock(side_effect=RuntimeError("owner"))
    )
    with pytest.raises(SystemExit):
        cli.main(live_argv(state))
    assert recorded(state) == "1"


def test_a_deliberate_rollback_retires_the_marker_only_while_holding_the_lock(
    tmp_path, monkeypatch
):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    marked(state)
    seen = {}
    real = rotation.retire_mode

    def retire(store):
        try:
            with account_lock(store.path):
                seen["exclusive"] = (
                    False  # we could take it: the caller did not hold it
                )
        except AccountBusy:
            seen["exclusive"] = True
        real(store)

    monkeypatch.setattr(rotation, "retire_mode", retire)
    serve = AsyncMock()
    monkeypatch.setattr(live, "serve_live", serve)
    assert cli.main(live_argv(state, "--retire-single-tab")) == 0
    assert seen == {"exclusive": True}
    serve.assert_awaited_once()
    assert recorded(state) is None
    with account_lock(state):  # and the lock was released before the legacy loop ran
        pass


def test_a_legacy_launch_without_a_recorded_mode_is_unchanged_and_ignores_the_lock(
    tmp_path, monkeypatch
):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    serve = AsyncMock()
    monkeypatch.setattr(live, "serve_live", serve)
    with account_lock(state):  # a coverage or startup run may hold it at any moment
        assert cli.main(live_argv(state)) == 0
        assert cli.main(live_argv(state, "--retire-single-tab")) == 0
    assert serve.await_count == 2 and recorded(state) is None


def test_the_rollback_flag_is_refused_together_with_single_tab(tmp_path, monkeypatch):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    marked(state)
    single = AsyncMock()
    monkeypatch.setattr(patrol, "serve_single_tab", single)
    refused(live_argv(state, "--single-tab", "--retire-single-tab"))
    single.assert_not_called()
    assert recorded(state) == "1"


def test_single_tab_start_routes_to_the_patrol_and_never_the_legacy_loop(
    tmp_path, monkeypatch
):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    legacy, single = AsyncMock(), AsyncMock()
    monkeypatch.setattr(live, "serve_live", legacy)
    monkeypatch.setattr(patrol, "serve_single_tab", single)
    flags = ["--single-tab", "--rotation-min", "250", "--rotation-max", "350"]
    assert cli.main(live_argv(state, *flags, "--confirm-wait", "30")) == 0
    legacy.assert_not_called()
    single.assert_awaited_once()
    args = single.await_args.args[0]
    assert (
        args.single_tab,
        args.rotation_min,
        args.rotation_max,
        args.confirm_wait,
    ) == (
        True,
        250.0,
        350.0,
        30.0,
    )


@pytest.mark.parametrize(
    "flag",
    [["--rotation-min", "250"], ["--rotation-max", "350"], ["--confirm-wait", "30"]],
)
def test_rotation_flags_without_single_tab_are_refused(tmp_path, monkeypatch, flag):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    legacy = AsyncMock()
    monkeypatch.setattr(live, "serve_live", legacy)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(live_argv(state, *flag))
    assert exit_info.value.code == 2
    legacy.assert_not_called()


@pytest.mark.parametrize(
    "flags",
    [
        ["--rotation-min", "50"],
        ["--rotation-min", "400", "--rotation-max", "300"],
        ["--rotation-max", "5000"],
        ["--confirm-wait", "10"],
        ["--confirm-wait", "90"],
    ],
)
def test_unsafe_single_tab_settings_refuse_before_any_browser_work(
    tmp_path, monkeypatch, flags
):
    state = make_evidence(tmp_path, enrolled=(OLD,))
    playwright = Mock()
    monkeypatch.setattr("playwright.async_api.async_playwright", playwright)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(live_argv(state, "--single-tab", *flags))
    assert exit_info.value.code == 2
    playwright.assert_not_called()
    assert rotation.MODE_CONTROL not in Store(state, account="home").controls()
