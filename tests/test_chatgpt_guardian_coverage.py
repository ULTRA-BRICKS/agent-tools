"""Coverage reconciliation: fake processes, DevTools, systemd and notifier only.

No live browser, CDP, systemd or Telegram is reached by any test here.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt import cli, coverage
from agenttools_chatgpt.core import Store
from agenttools_chatgpt.coverage import Config, Delivery, ServiceError, reconcile
from agenttools_chatgpt.lock import lock_path
from agenttools_chatgpt.monitor import MonitorStore, project_snapshot, read_source
from agenttools_chatgpt.startup import Browser, Listener, profile_path
from test_chatgpt_guardian_startup import FakeDevTools, FakeProcesses

NOW = 1_700_000_000.0
PORT = 9226
HOME = Path("/h")
FJ = "https://chatgpt.com/c/fj-finish"
OLD = "https://chatgpt.com/c/old-one"
THIRD = "https://chatgpt.com/c/third"
ALL_ON = Config(coverage_enabled=True, auto_watch_active=True, recovery_autostart=True)
ENABLED = Config(
    coverage_enabled=True, auto_watch_active=False, recovery_autostart=False
)


def sample(chat, *, status="working", running=True, at=NOW, **extra):
    return {
        "chat": chat,
        "checked_at": at,
        "status": status,
        "running": running,
        "progress_token": "tok",
        "turn": "turn-1",
        **extra,
    }


def make_evidence(
    tmp_path,
    *,
    enrolled=(OLD,),
    unwatched=(),
    samples=(),
    snapshot_at=NOW,
    connection="connected",
    workspace="home",
    quota=True,
):
    """Real persisted monitor evidence built by the real projection."""
    state = tmp_path / "state.sqlite"
    store = Store(state, account=workspace)  # the real recovery schema
    for chat in enrolled:
        store.set_control("watch:" + chat, "1")
    for chat in unwatched:
        store.set_control("watch:" + chat, "0")
    if quota:
        store.quota()  # Astra quota stays latched in every scenario
    controls, attempts = read_source(state, workspace)
    snapshot = project_snapshot(
        workspace,
        controls,
        attempts,
        list(samples),
        None,
        {
            "connection": connection,
            "heartbeat_at": snapshot_at,
            "service": {"active": "active"},
        },
        snapshot_at,
    )
    MonitorStore(state, workspace).record(snapshot)
    return state


class FakeSystemd:
    def __init__(self, active="active", fail_start=False):
        self.active = active
        self.fail_start = fail_start
        self.started = []

    def show(self, unit):
        return {"ActiveState": self.active, "SubState": "x"}

    def start(self, unit):
        self.started.append(unit)
        if self.fail_start:
            raise ServiceError("systemctl start failed")


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def __call__(self, text):
        self.messages.append(text)
        return Delivery(self.ok, "" if self.ok else "tg exited 1")


class LoginRedirectDevTools(FakeDevTools):
    """A restore that ends on a login page: the chat never appears."""

    def create(self, url, timeout):
        self.created.append(url)
        self.pages_now.append({"type": "page", "url": "https://auth.openai.com/log-in"})


def ticking():
    ticks = iter(x * 0.5 for x in range(100000))
    return ticks.__next__


def run(
    state,
    *,
    config=ALL_ON,
    apply=True,
    devtools=None,
    processes=None,
    systemd=None,
    notify=None,
    now=NOW,
    workspace="home",
):
    devtools = FakeDevTools([OLD]) if devtools is None else devtools
    systemd = systemd or FakeSystemd()
    notify = notify or FakeNotifier()
    report = reconcile(
        workspace,
        state=state,
        port=PORT,
        apply=apply,
        config=config,
        processes=processes or FakeProcesses(workspace, HOME, PORT),
        devtools=devtools,
        systemd=systemd,
        notify=notify,
        now=now,
        home=HOME,
        clock=ticking(),
        sleep=lambda _s: None,
    )
    return report, devtools, systemd, notify


def controls_of(state):
    return read_source(state, "home")[0]


def row_of(report, chat):
    return next(r for r in report["chats"] if r["chat"] == chat.rsplit("/", 1)[1])


# --- configuration ---------------------------------------------------------


def test_config_defaults_to_everything_off_when_file_missing(tmp_path):
    config = coverage.load_config("home", tmp_path / "absent.json")
    assert config == Config(False, False, False)


def test_config_reads_explicit_flags(tmp_path):
    path = tmp_path / "home.coverage.json"
    path.write_text(
        json.dumps(
            {
                "workspace": "home",
                "coverage_enabled": True,
                "auto_watch_active": True,
                "recovery_autostart": False,
            }
        )
    )
    assert coverage.load_config("home", path) == Config(True, True, False)


@pytest.mark.parametrize(
    "payload",
    [
        {"workspace": "glide.vc", "coverage_enabled": True},
        {"workspace": "home", "coverage_enabled": "yes"},
        {"workspace": "home", "surprise": True},
        ["not", "a", "dict"],
    ],
)
def test_config_rejects_wrong_scope_types_and_unknown_keys(tmp_path, payload):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        coverage.load_config("home", path)


# --- requirement 1: separate facts -----------------------------------------


def test_report_separates_enrolled_present_fresh_active_eligible(tmp_path):
    state = make_evidence(
        tmp_path,
        enrolled=(OLD, FJ),
        samples=[sample(OLD, status="complete", running=False), sample(FJ)],
    )
    report, *_ = run(state, apply=False, devtools=FakeDevTools([OLD, FJ]))
    old, fj = row_of(report, OLD), row_of(report, FJ)
    assert old["enrolled"] and old["present"] and old["fresh_chat"]
    assert old["activity"] == "finished" and not old["active"]
    assert fj["activity"] == "running" and fj["active"]
    assert fj["recovery_eligible"] is False  # the fixture latches Astra quota
    assert "quota" in fj["blockers"]
    assert report["summary"]["fresh_snapshot"] is True
    assert {k for k in report["summary"]} >= {
        "enrolled",
        "present",
        "fresh",
        "active",
        "recovery_eligible",
    }


def test_fresh_snapshot_does_not_make_a_stale_chat_fresh(tmp_path):
    state = make_evidence(
        tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD), sample(FJ, at=NOW - 3000)]
    )
    report, *_ = run(state, apply=False, devtools=FakeDevTools([OLD, FJ]))
    assert report["summary"]["fresh_snapshot"] is True
    assert row_of(report, OLD)["fresh_chat"] is True
    stale = row_of(report, FJ)
    assert stale["fresh_chat"] is False and stale["activity"] == "unknown"
    assert stale["active"] is False


def test_completion_history_is_not_current_finished(tmp_path):
    state = make_evidence(
        tmp_path,
        samples=[
            sample(OLD, status="working", running=False, completion_observed_at=1)
        ],
    )
    report, *_ = run(state, apply=False)
    assert row_of(report, OLD)["activity"] == "idle"


def test_dry_run_is_read_only(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD), sample(FJ)])
    before = state.read_bytes()
    report, dev, systemd, notify = run(
        state,
        apply=False,
        systemd=FakeSystemd("failed"),
        devtools=FakeDevTools([OLD, FJ]),
    )
    assert report["apply"] is False
    assert "watch:" + FJ not in controls_of(state)
    assert report["plan"]["enroll"] == [FJ.rsplit("/", 1)[1]]
    assert dev.created == [] and systemd.started == [] and notify.messages == []
    assert state.read_bytes() == before
    assert not coverage.ledger_path(state).exists()


# --- requirement 2: auto-enrol ---------------------------------------------


def enrol_case(tmp_path, **kwargs):
    state = make_evidence(tmp_path, samples=[sample(OLD), sample(FJ)], **kwargs)
    return state


def test_running_unseen_chat_is_enrolled_and_quota_survives(tmp_path):
    state = enrol_case(tmp_path)
    report, *_ = run(state, devtools=FakeDevTools([OLD, FJ]))
    controls = controls_of(state)
    assert controls["watch:" + FJ] == "1"
    assert controls["astra_quota"] == "blocked"
    assert report["effects"]["enrolled"] == [FJ.rsplit("/", 1)[1]]


def test_thinking_failed_unseen_chat_is_enrolled(tmp_path):
    state = make_evidence(
        tmp_path,
        samples=[sample(OLD), sample(FJ, status="Thinking failed", running=False)],
    )
    run(state, devtools=FakeDevTools([OLD, FJ]))
    assert controls_of(state)["watch:" + FJ] == "1"


def test_manual_unwatch_is_never_undone(tmp_path):
    state = make_evidence(
        tmp_path, enrolled=(OLD,), unwatched=(FJ,), samples=[sample(OLD), sample(FJ)]
    )
    report, *_, notify = run(state, devtools=FakeDevTools([OLD, FJ]))
    assert controls_of(state)["watch:" + FJ] == "0"
    assert row_of(report, FJ)["manually_unwatched"] is True
    assert report["effects"]["enrolled"] == []
    assert (
        notify.messages == []
    )  # the owner's explicit unwatch is reported, not alerted


@pytest.mark.parametrize(
    "config", [ENABLED, Config(False, True, False)], ids=["no-auto-watch", "disabled"]
)
def test_missing_flag_enrols_nothing(tmp_path, config):
    state = enrol_case(tmp_path)
    run(state, config=config, devtools=FakeDevTools([OLD, FJ]))
    assert "watch:" + FJ not in controls_of(state)


def test_stale_snapshot_enrols_nothing(tmp_path):
    state = make_evidence(
        tmp_path, samples=[sample(OLD), sample(FJ)], snapshot_at=NOW - 1000
    )
    run(state, devtools=FakeDevTools([OLD, FJ]))
    assert "watch:" + FJ not in controls_of(state)


def test_duplicate_pages_enrol_nothing(tmp_path):
    state = enrol_case(tmp_path)
    run(state, devtools=FakeDevTools([OLD, FJ, FJ]))
    assert "watch:" + FJ not in controls_of(state)


def test_wrong_scope_browser_enrols_nothing_and_is_blocked(tmp_path):
    state = enrol_case(tmp_path)
    other = FakeProcesses("home", HOME, PORT, browsers=[])
    report, *_ = run(state, devtools=FakeDevTools([OLD, FJ]), processes=other)
    assert "watch:" + FJ not in controls_of(state)
    assert any(b["scope"] == "browser" for b in report["blocked"])


def test_finished_and_unknown_visible_chats_are_uncovered_without_effects(tmp_path):
    state = make_evidence(
        tmp_path,
        samples=[
            sample(OLD),
            sample(FJ, status="complete", running=False),
            sample(THIRD, at=NOW - 3000),
        ],
    )
    dev = FakeDevTools([OLD, FJ, THIRD])
    report, _, _, notify = run(state, devtools=dev)
    controls = controls_of(state)
    assert "watch:" + FJ not in controls and "watch:" + THIRD not in controls
    assert set(report["summary"]["uncovered"]) == {"fj-finish", "third"}
    assert dev.created == []
    assert not any("fj-finish" in m for m in notify.messages)


# --- requirement 3: restore ------------------------------------------------


def test_missing_enrolled_chat_is_restored_once_via_startup(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    report, dev, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert dev.created == [FJ]
    assert report["effects"]["opened"] == ["fj-finish"]


def test_redirected_restore_is_not_repeated_and_blank_tabs_do_not_accumulate(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    dev = LoginRedirectDevTools([OLD])
    run(state, devtools=dev)
    assert len(dev.created) == 1
    for minute in range(1, 40):
        report, *_ = run(state, devtools=dev, now=NOW + 60 * minute)
    assert len(dev.created) == 1
    assert len(dev.pages_now) == 2
    assert report["plan"]["restore_deferred"] == ["fj-finish"]


def test_restore_repeats_only_after_inventory_change_and_cooldown(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    dev = LoginRedirectDevTools([OLD])
    run(state, devtools=dev)
    restarted = FakeProcesses(
        "home",
        HOME,
        PORT,
        browsers=[
            Browser(
                11,
                (
                    "/opt/google/chrome/chrome",
                    "--user-data-dir=" + str(profile_path("home", HOME)),
                    "--remote-debugging-port=" + str(PORT),
                ),
            )
        ],
        listeners=[Listener("127.0.0.1", 11)],
    )
    run(state, devtools=dev, processes=restarted, now=NOW + 60)
    assert len(dev.created) == 1  # inventory changed, but the cooldown still holds
    run(state, devtools=dev, processes=restarted, now=NOW + 3600)
    assert len(dev.created) == 2


def test_restore_ledger_survives_a_new_process(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    dev = LoginRedirectDevTools([OLD])
    run(state, devtools=dev)
    rows = (
        sqlite3.connect(coverage.ledger_path(state))
        .execute("SELECT chat FROM restore")
        .fetchall()
    )
    assert rows == [(FJ,)]


def test_restore_capacity_is_explicitly_blocked(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    dev = FakeDevTools([OLD] + [f"https://example.com/{i}" for i in range(5)])
    report, *_ = run(state, devtools=dev)
    assert dev.created == []
    assert any(
        b["scope"] == "restore" and "ceiling" in b["reason"] for b in report["blocked"]
    )


def test_restore_in_wrong_account_state_is_blocked(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD,), samples=[sample(OLD)])
    report, dev, *_ = run(state, workspace="glide.vc", devtools=FakeDevTools([OLD]))
    assert dev.created == []
    assert any(b["scope"] == "state" for b in report["blocked"])


def test_restore_never_runs_without_the_enable_flag_or_apply(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    for kwargs in ({"config": Config(False, True, True)}, {"apply": False}):
        _, dev, *_ = run(state, devtools=FakeDevTools([OLD]), **kwargs)
        assert dev.created == []


# --- requirement 4: owner alerts -------------------------------------------


def test_failed_service_alerts_once_in_russian_and_persists(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    first = FakeNotifier()
    run(state, systemd=FakeSystemd("failed"), notify=first)
    assert len(first.messages) == 1
    assert re.search("[А-Яа-я]", first.messages[0])
    assert "https://" not in first.messages[0]
    again = FakeNotifier()
    run(state, systemd=FakeSystemd("failed"), notify=again, now=NOW + 120)
    assert again.messages == []


def test_alert_delivery_failure_is_recorded_truthfully_then_retried(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    report, *_ = run(
        state, systemd=FakeSystemd("failed"), notify=FakeNotifier(ok=False)
    )
    assert report["effects"]["alerts"][0]["status"] == "delivery-failed"
    status, detail = (
        sqlite3.connect(coverage.ledger_path(state))
        .execute("SELECT status, detail FROM alerts")
        .fetchone()
    )
    assert status == "delivery-failed" and "tg exited 1" in detail
    soon = FakeNotifier()
    run(state, systemd=FakeSystemd("failed"), notify=soon, now=NOW + 60)
    assert soon.messages == []
    later = FakeNotifier()
    report, *_ = run(state, systemd=FakeSystemd("failed"), notify=later, now=NOW + 3600)
    assert len(later.messages) == 1
    assert report["effects"]["alerts"][0]["status"] == "sent"


def test_missing_page_alert_after_failed_restore_is_deduped(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    dev = LoginRedirectDevTools([OLD])
    notify = FakeNotifier()
    run(state, devtools=dev, notify=notify)
    run(state, devtools=dev, notify=notify, now=NOW + 60)
    assert len(notify.messages) == 1
    assert "fj-finish"[:8] in notify.messages[0]


def test_stale_observation_alert(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)], snapshot_at=NOW - 1000)
    notify = FakeNotifier()
    run(state, notify=notify)
    assert len(notify.messages) == 1


def test_uncovered_active_alert_when_auto_watch_is_off_and_not_repeated(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD), sample(FJ)])
    notify = FakeNotifier()
    dev = FakeDevTools([OLD, FJ])
    run(state, config=ENABLED, devtools=dev, notify=notify)
    run(state, config=ENABLED, devtools=dev, notify=notify, now=NOW + 60)
    assert len(notify.messages) == 1 and "fj-finish"[:8] in notify.messages[0]


def test_chat_enrolled_this_run_is_not_alerted_as_uncovered(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD), sample(FJ)])
    notify = FakeNotifier()
    run(state, devtools=FakeDevTools([OLD, FJ]), notify=notify)
    assert notify.messages == []


def test_stalled_current_generation_alerts_once_per_stall(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    controls, attempts = read_source(state, "home")
    stalled = project_snapshot(
        "home",
        controls,
        attempts,
        [sample(OLD, at=NOW + 1000)],
        json.loads(
            sqlite3.connect(MonitorStore(state, "home").path)
            .execute("SELECT payload FROM snapshot")
            .fetchone()[0]
        ),
        {"connection": "connected", "heartbeat_at": NOW + 1000, "service": {}},
        NOW + 1000,
    )
    MonitorStore(state, "home").record(stalled)
    notify = FakeNotifier()
    report, *_ = run(state, notify=notify, now=NOW + 1000)
    assert row_of(report, OLD)["stalled"] is True
    assert len(notify.messages) == 1
    run(state, notify=notify, now=NOW + 1060)
    assert len(notify.messages) == 1


def test_resolved_alert_rearms_only_after_quiet_period(tmp_path, monkeypatch):
    # Short quiet period so every run stays inside the fixture snapshot's freshness.
    monkeypatch.setattr(coverage, "ALERT_REARM_SECONDS", 100)
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    notify = FakeNotifier()
    run(state, systemd=FakeSystemd("failed"), notify=notify)
    run(state, systemd=FakeSystemd("active"), notify=notify, now=NOW + 10)
    run(state, systemd=FakeSystemd("failed"), notify=notify, now=NOW + 20)
    assert len(notify.messages) == 1  # flapping inside the quiet period
    run(state, systemd=FakeSystemd("active"), notify=notify, now=NOW + 30)
    run(state, systemd=FakeSystemd("failed"), notify=notify, now=NOW + 140)
    assert len(notify.messages) == 2


def test_subprocess_notifier_uses_tg_problem_tag_without_shell(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        coverage.subprocess,
        "run",
        lambda argv, **kw: calls.append((argv, kw)) or Done(),
    )
    assert coverage.tg_notifier("сообщение").ok
    argv, kw = calls[0]
    assert argv == [
        "tg",
        "--title",
        "ChatGPT Guardian",
        "--tag",
        "problem",
        "--format",
        "plain",
        "сообщение",
    ]
    assert kw.get("shell") in (None, False) and kw["timeout"] <= 60


def test_subprocess_notifier_reports_missing_tg(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("tg")

    monkeypatch.setattr(coverage.subprocess, "run", boom)
    result = coverage.tg_notifier("x")
    assert result.ok is False and result.detail


# --- recovery autostart ----------------------------------------------------


def test_autostart_starts_a_failed_service_once_browser_is_verified(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    systemd = FakeSystemd("failed")
    run(state, systemd=systemd)
    assert systemd.started == ["chatgpt-guardian-home.service"]


@pytest.mark.parametrize("active", ["inactive", "active", "activating"])
def test_autostart_never_starts_a_manually_stopped_or_running_service(tmp_path, active):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    systemd = FakeSystemd(active)
    run(state, systemd=systemd)
    assert systemd.started == []


def test_autostart_is_off_by_default_and_needs_apply(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    for kwargs in ({"config": ENABLED}, {"apply": False}):
        systemd = FakeSystemd("failed")
        run(state, systemd=systemd, **kwargs)
        assert systemd.started == []


def test_autostart_respects_global_operator_stop_and_never_clears_it(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    sqlite3.connect(state).execute(
        "INSERT INTO controls VALUES ('stop:*', 'manual stop')"
    ).connection.commit()
    systemd = FakeSystemd("failed")
    run(state, systemd=systemd)
    assert systemd.started == []
    assert controls_of(state)["stop:*"] == "manual stop"


def test_autostart_waits_for_verified_browser(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    systemd = FakeSystemd("failed")
    run(
        state, systemd=systemd, processes=FakeProcesses("home", HOME, PORT, browsers=[])
    )
    assert systemd.started == []


def test_autostart_is_bounded_by_cooldown_and_attempt_cap(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    systemd = FakeSystemd("failed", fail_start=True)
    run(state, systemd=systemd, now=NOW)
    run(state, systemd=systemd, now=NOW + 60)
    assert len(systemd.started) == 1  # cooldown
    for step in range(1, 12):
        run(state, systemd=systemd, now=NOW + 600 * step)
    assert len(systemd.started) == coverage.AUTOSTART_MAX_ATTEMPTS


def test_autostart_failure_is_recorded_and_alerted(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    notify = FakeNotifier()
    report, *_ = run(
        state, systemd=FakeSystemd("failed", fail_start=True), notify=notify
    )
    assert report["effects"]["autostart"]["outcome"] == "failed"
    assert len(notify.messages) == 1


def test_recovery_is_reset_after_the_service_is_seen_active(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    systemd = FakeSystemd("failed", fail_start=True)
    for step in range(10):
        run(state, systemd=systemd, now=NOW + 600 * step)
    capped = len(systemd.started)
    run(state, systemd=FakeSystemd("active"), now=NOW + 7000)
    run(state, systemd=systemd, now=NOW + 8000)
    assert len(systemd.started) == capped + 1


# --- requirement 5: templates, CLI -----------------------------------------


SYSTEMD = Path(coverage.__file__).parent / "systemd" / "chatgpt-guardian-coverage"


def test_coverage_templates_take_the_port_as_the_only_hardcode_free_parameter():
    service = (SYSTEMD / "coverage.service.template").read_text()
    timer = (SYSTEMD / "coverage.timer.template").read_text()
    for text in (service, timer):
        assert not re.search(r"\b92(2[3-9]|3\d)\b", text)
    assert set(re.findall(r"@[A-Z_]+@", service)) == {
        "@VENV_PYTHON@",
        "@WORKSPACE@",
        "@CDP_PORT@",
    }
    assert "coverage --cdp-port @CDP_PORT@ --apply" in service
    assert "Type=oneshot" in service
    assert "OnUnitActiveSec=60" in timer
    assert set(re.findall(r"@[A-Z_]+@", timer)) <= {"@SLUG@"}


def test_cli_registers_coverage_and_requires_the_port(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--workspace", "home", "coverage"])
    assert exit_info.value.code == 2
    assert "--cdp-port" in capsys.readouterr().err


def test_cli_coverage_defaults_to_a_dry_run(tmp_path, monkeypatch, capsys):
    state = make_evidence(tmp_path, samples=[sample(OLD), sample(FJ)])
    monkeypatch.setattr(
        coverage, "ProcFilesystem", lambda: FakeProcesses("home", Path.home(), PORT)
    )
    monkeypatch.setattr(
        coverage, "ChromeDevTools", lambda port: FakeDevTools([OLD, FJ])
    )
    monkeypatch.setattr(coverage, "SystemdUser", FakeSystemd)
    monkeypatch.setattr(coverage, "wall_clock", lambda: NOW)
    config = tmp_path / "cfg.json"
    config.write_text(
        json.dumps(
            {"workspace": "home", "coverage_enabled": True, "auto_watch_active": True}
        )
    )
    monkeypatch.setenv("CHATGPT_GUARDIAN_COVERAGE_CONFIG", str(config))
    code = cli.main(
        ["--workspace", "home", "--state", str(state), "coverage", "--cdp-port", "9226"]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["apply"] is False
    assert "watch:" + FJ not in controls_of(state)
    assert out["plan"]["enroll"] == ["fj-finish"]


def command_args(state, config, **overrides):
    values = {
        "workspace": "home",
        "state": state,
        "cdp_port": PORT,
        "apply": True,
        "config": config,
        "reset_restore": False,
    }
    return type("Args", (), {**values, **overrides})()


def test_reset_restore_is_an_apply_only_owner_action_that_grants_one_new_attempt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(coverage, "wall_clock", lambda: NOW)
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"workspace": "home", "coverage_enabled": True}))
    dev = LoginRedirectDevTools([OLD])
    fakes = {
        "processes": FakeProcesses("home", Path.home(), PORT),
        "devtools": dev,
        "systemd": FakeSystemd(),
        "notify": FakeNotifier(),
    }
    coverage.command(command_args(state, config), **fakes)
    coverage.command(command_args(state, config), **fakes)
    assert len(dev.created) == 1
    with pytest.raises(ValueError):
        coverage.command(
            command_args(state, config, apply=False, reset_restore=True), **fakes
        )
    assert len(dev.created) == 1
    coverage.command(command_args(state, config, reset_restore=True), **fakes)
    assert len(dev.created) == 2


def test_crash_during_restore_never_grants_a_second_attempt(tmp_path):
    class Crashing(FakeDevTools):
        def create(self, url, timeout):
            self.created.append(url)
            raise RuntimeError("process died mid-restore")

    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    crashing = Crashing([OLD])
    with pytest.raises(RuntimeError):
        run(state, devtools=crashing)
    again = FakeDevTools([OLD])
    run(state, devtools=again, now=NOW + 60)
    assert crashing.created == [FJ] and again.created == []


def test_restore_that_creates_nothing_stays_retryable(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    full = FakeDevTools([OLD] + [f"https://example.com/{i}" for i in range(5)])
    run(state, devtools=full)
    roomy = FakeDevTools([OLD])
    run(state, devtools=roomy, now=NOW + 60)
    assert roomy.created == [FJ]


@pytest.mark.parametrize(
    ("active", "eligible"), [("active", 1), ("failed", 0), ("inactive", 0)]
)
def test_recovery_eligibility_requires_an_active_service_and_claims_no_model_proof(
    tmp_path, active, eligible
):
    state = make_evidence(tmp_path, samples=[sample(OLD)], quota=False)
    systemd = FakeSystemd(active)
    report, _, _, notify = run(state, apply=False, systemd=systemd)
    row = row_of(report, OLD)
    assert report["summary"]["recovery_eligible"] == eligible
    assert row["recovery_eligible"] is bool(eligible)
    assert row["eligibility_for_probe"] is bool(eligible)
    assert row["model_proof"] == "unknown"
    assert systemd.started == [] and notify.messages == []


# --- safety finish (0.3.5) -------------------------------------------------

HOLD = "transport HTTP 429; manual resume required"


def hold_locked(state):
    """A second process's view: the account lock is already held by someone else."""
    import fcntl

    handle = open(lock_path(state), "a+")  # noqa: SIM115 - held until the caller closes it
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_overlapping_applied_runs_open_one_tab_and_report_busy(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    inner = []

    class Reentrant(FakeDevTools):
        def create(self, url, timeout):
            inner.append(run(state, devtools=FakeDevTools([OLD]))[0])
            super().create(url, timeout)

    dev = Reentrant([OLD])
    report, *_ = run(state, devtools=dev)
    assert dev.created == [FJ]
    assert report["busy"] is False
    (second,) = inner
    assert second["busy"] is True and second["blocked"] == []
    assert second["effects"] == {
        "enrolled": [],
        "opened": [],
        "alerts": [],
        "autostart": None,
    }


def test_busy_applied_run_changes_nothing_and_keeps_the_schema(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    free, *_ = run(state, apply=False)
    handle = hold_locked(state)
    try:
        dev, notify, systemd = (
            FakeDevTools([OLD]),
            FakeNotifier(),
            FakeSystemd("failed"),
        )
        report, *_ = run(state, devtools=dev, notify=notify, systemd=systemd)
    finally:
        handle.close()
    assert report["busy"] is True
    assert dev.created == [] and notify.messages == [] and systemd.started == []
    assert not coverage.ledger_path(state).exists()
    assert set(report) == set(free) and set(report["summary"]) == set(free["summary"])


def test_dry_run_ignores_the_lock_and_never_creates_it(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    report, *_ = run(state, apply=False)
    assert report["busy"] is False
    assert not lock_path(state).exists()


def test_lock_is_released_after_an_error(tmp_path):
    class Crashing(FakeDevTools):
        def create(self, url, timeout):
            raise RuntimeError("boom")

    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    with pytest.raises(RuntimeError):
        run(state, devtools=Crashing([OLD]))
    report, *_ = run(state, devtools=FakeDevTools([OLD]), now=NOW + 60)
    assert report["busy"] is False


def test_concurrent_reservation_is_never_replaced(tmp_path):
    ledger = coverage.Ledger(coverage.ledger_path(tmp_path / "s.sqlite"), "home")
    assert ledger.reserve_restore([FJ], NOW, "sig") == [FJ]
    assert ledger.reserve_restore([FJ], NOW + 5, "sig") == []
    assert ledger.restore_rows()[FJ]["at"] == NOW


def test_startup_restore_waits_for_a_running_coverage_apply(tmp_path):
    from agenttools_chatgpt import startup
    from test_chatgpt_guardian_startup import make_state

    state = make_state(tmp_path / "s.sqlite")
    handle = hold_locked(state)
    dev = FakeDevTools([])
    try:
        code = startup.main(
            ["--workspace", "home", "--apply", "--cdp-port", str(PORT)],
            processes=FakeProcesses("home", HOME, PORT),
            devtools=dev,
            state=state,
            home=HOME,
            lock_wait=0.0,
        )
    finally:
        handle.close()
    assert code == 1 and dev.created == []


def test_recorded_signature_equals_the_next_runs_when_a_tab_gets_duplicated(tmp_path):
    class DuplicatingRedirect(LoginRedirectDevTools):
        def create(self, url, timeout):
            super().create(url, timeout)
            self.pages_now.append({"type": "page", "url": OLD})  # browser reopens it

    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    dev = DuplicatingRedirect([OLD])
    run(state, devtools=dev)
    recorded = coverage.Ledger(coverage.ledger_path(state), "home").restore_rows()[FJ]
    later, *_ = run(state, apply=False, devtools=dev, now=NOW + 3600)
    assert recorded["signature"] == later["plan"]["inventory_signature"]
    assert len(dev.created) == 1


def test_newly_enrolled_chat_does_not_change_the_next_signature(tmp_path):
    state = make_evidence(
        tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD), sample(THIRD)]
    )
    dev = LoginRedirectDevTools([OLD, THIRD])
    report, *_ = run(state, devtools=dev)
    assert report["effects"]["enrolled"] == ["third"]
    for minute in range(1, 40):
        run(state, devtools=dev, now=NOW + 60 * minute)
    assert len(dev.created) == 1


def test_failed_inventory_reread_never_records_spent_chats_as_present(tmp_path):
    from agenttools_chatgpt.startup import StartupError

    class RereadFails(LoginRedirectDevTools):
        def pages(self, timeout):
            if self.created:
                raise StartupError("DevTools gone")
            return super().pages(timeout)

    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    run(state, devtools=RereadFails([OLD]))
    dev = LoginRedirectDevTools([OLD, "https://auth.openai.com/log-in"])
    for minute in range(20, 60):
        run(state, devtools=dev, now=NOW + 60 * minute)
    assert dev.created == []


def ledger_tables(path):
    db = sqlite3.connect(path)
    return {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_empty_ledger_file_is_initialised_safely(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    coverage.ledger_path(state).touch()
    report, dev, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert report["blocked"] == [] and dev.created == [FJ]
    assert {"scope", "restore", "alerts"} <= ledger_tables(coverage.ledger_path(state))


def test_ledger_with_tables_but_no_scope_row_is_repaired(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    path = coverage.ledger_path(state)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE scope(workspace TEXT PRIMARY KEY)")
    report, dev, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert report["blocked"] == [] and dev.created == [FJ]
    assert sqlite3.connect(path).execute("SELECT * FROM scope").fetchall() == [
        ("home",)
    ]


def test_dry_run_tolerates_an_uninitialised_ledger_without_writing(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    path = coverage.ledger_path(state)
    path.touch()
    report, *_ = run(state, apply=False)
    assert report["blocked"] == [] and path.read_bytes() == b""


def test_failed_first_write_leaves_no_partial_schema(tmp_path):
    ledger = coverage.Ledger(tmp_path / "x.coverage.sqlite", "home")
    with pytest.raises(RuntimeError), ledger._write():
        raise RuntimeError("killed")
    assert ledger_tables(ledger.path) == set()
    ledger.record_autostart(NOW, "started")
    assert ledger.autostart_rows() == [(NOW, "started")]


def test_foreign_account_ledger_is_refused_and_untouched(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    ledger = coverage.Ledger(coverage.ledger_path(state), "glide.vc")
    ledger.record_autostart(NOW, "started")
    before = coverage.ledger_path(state).read_bytes()
    report, dev, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert any(b["scope"] == "state" for b in report["blocked"]) and dev.created == []
    assert coverage.ledger_path(state).read_bytes() == before


def test_nonempty_ledger_without_scope_is_refused_never_adopted(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    path = coverage.ledger_path(state)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE scope(workspace TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE autostart(at REAL NOT NULL, outcome TEXT NOT NULL)")
        db.execute("INSERT INTO autostart VALUES (1, 'started')")
    report, dev, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert any(b["scope"] == "state" for b in report["blocked"]) and dev.created == []
    assert sqlite3.connect(path).execute("SELECT * FROM autostart").fetchall() == [
        (1.0, "started")
    ]
    assert sqlite3.connect(path).execute("SELECT * FROM scope").fetchall() == []


def test_a_stale_per_chat_check_is_never_fresh_or_finished():
    snapshot = {
        "sessions": [
            {
                "chat": OLD,
                "observation": "current",
                "session_stale": False,
                "check_stale": True,
                "page_count": 1,
                "status": "complete",
                "running": False,
            }
        ]
    }
    controls = {"watch:" + OLD: "1"}
    rows, _ = coverage._chat_rows(
        controls, [], snapshot, coverage.Counter({OLD: 1}), True, True
    )
    (row,) = rows
    assert row["fresh_chat"] is False and row["activity"] == "unknown"
    assert row["recovery_eligible"] is False and row["active"] is False


def global_hold(state, value=HOLD):
    sqlite3.connect(state).execute(
        "INSERT OR REPLACE INTO controls VALUES ('stop:*', ?)", (value,)
    ).connection.commit()


def test_global_recovery_hold_alerts_once_and_is_never_lifted(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    global_hold(state)
    notify = FakeNotifier()
    systemd = FakeSystemd("active")
    for step in range(5):
        run(state, notify=notify, systemd=systemd, now=NOW + 5 * step)
    held = [m for m in notify.messages if HOLD in m]
    assert len(held) == 1 and len(notify.messages) == 1
    assert controls_of(state)["stop:*"] == HOLD and systemd.started == []


def test_failed_global_hold_delivery_is_recorded_truthfully(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    global_hold(state, "human challenge")
    report, *_ = run(state, notify=FakeNotifier(ok=False))
    assert [a["status"] for a in report["effects"]["alerts"]] == ["delivery-failed"]


def test_changed_global_hold_alerts_again(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    global_hold(state, "human challenge")
    run(state)
    global_hold(state, "manual stop")
    again, _, _, notify = run(state, now=NOW + 10)
    assert [a["status"] for a in again["effects"]["alerts"]] == ["sent"]
    assert "manual stop" in notify.messages[0]


def test_inactive_service_under_hold_is_never_started_or_alerted_as_failed(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    global_hold(state, "manual stop")
    systemd = FakeSystemd("inactive")
    report, _, _, notify = run(state, systemd=systemd)
    assert systemd.started == [] and report["effects"]["autostart"] is None
    assert not any("failed" in m for m in notify.messages)


def test_cleared_alerts_are_pruned_after_the_rearm_window(tmp_path):
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    run(state, systemd=FakeSystemd("failed"))
    run(state, systemd=FakeSystemd("active"), now=NOW + 60)
    rows = coverage.Ledger(coverage.ledger_path(state), "home").alert_rows()
    assert rows and all(r["cleared_at"] is not None for r in rows.values())
    later = NOW + coverage.ALERT_REARM_SECONDS + 120
    run(state, systemd=FakeSystemd("active"), now=later)
    rows = coverage.Ledger(coverage.ledger_path(state), "home").alert_rows()
    assert not any(key.startswith("service:") for key in rows)


def test_report_schema_is_identical_on_early_failure(tmp_path):
    good = make_evidence(tmp_path, samples=[sample(OLD)])
    full, *_ = run(good, apply=False)
    bad, *_ = run(good, apply=False, workspace="glide.vc")
    assert bad["blocked"] and set(bad) == set(full)
    for key in ("summary", "browser", "service", "plan", "effects"):
        assert set(bad[key]) == set(full[key]), key


def test_matching_scope_with_an_unrecognised_table_is_refused(tmp_path):
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    coverage.Ledger(coverage.ledger_path(state), "home").record_autostart(NOW, "x")
    with sqlite3.connect(coverage.ledger_path(state)) as db:
        db.execute("CREATE TABLE foreign_notes(x TEXT)")
    report, dev, *_ = run(state, devtools=FakeDevTools([OLD]))
    assert any(b["scope"] == "state" for b in report["blocked"]) and dev.created == []


def test_reset_restore_is_never_silently_dropped_by_a_busy_lock(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(coverage, "wall_clock", lambda: NOW)
    state = make_evidence(tmp_path, enrolled=(OLD, FJ), samples=[sample(OLD)])
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"workspace": "home", "coverage_enabled": True}))
    handle = hold_locked(state)
    try:
        code = coverage.command(
            command_args(state, config, reset_restore=True),
            processes=FakeProcesses("home", Path.home(), PORT),
            devtools=FakeDevTools([OLD]),
            systemd=FakeSystemd(),
            notify=FakeNotifier(),
        )
    finally:
        handle.close()
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and report["busy"] is True
    assert report["blocked"][0]["scope"] == "reset-restore"


def test_a_failing_flock_is_a_blocked_lock_not_a_traceback(tmp_path, monkeypatch):
    import errno

    from agenttools_chatgpt import lock

    def broken(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock.fcntl, "flock", broken)
    state = make_evidence(tmp_path, samples=[sample(OLD)])
    report, *_ = run(state)
    assert [b["scope"] for b in report["blocked"]] == ["lock"]


def test_startup_restore_proceeds_once_the_coverage_run_releases_the_lock(tmp_path):
    import threading

    from agenttools_chatgpt import startup
    from test_chatgpt_guardian_startup import make_state

    state = make_state(tmp_path / "s.sqlite", chats=(OLD,))
    handle = hold_locked(state)
    timer = threading.Timer(0.3, handle.close)
    timer.start()
    dev = FakeDevTools([])
    code = startup.main(
        ["--workspace", "home", "--apply", "--cdp-port", str(PORT)],
        processes=FakeProcesses("home", HOME, PORT),
        devtools=dev,
        state=state,
        home=HOME,
        lock_wait=5.0,
    )
    timer.join()
    assert code == 0 and dev.created == [OLD]
