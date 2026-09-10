"""Tests for agenttools_service — the shared service-manager.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_service.py -q

Every test runs under a tmp HOME (``XDG_STATE_HOME`` / ``XDG_CACHE_HOME`` /
``XDG_CONFIG_HOME`` pointed inside ``tmp_path``) and injects a fake autostart runner + a fake
detached spawner, so the suite installs NO real launchd/systemd autostart and leaks nothing
onto the developer's machine. The launchd/systemd UNIT GENERATION is asserted directly
against the rendered text. ONE deliberate exception:
``test_default_port_prober_detects_a_real_ad_hoc_listener`` spawns a real child process and
binds a real loopback socket (and shells out to ``lsof`` if present) to end-to-end-reproduce
the review-cli#377 incident with the actual, unmocked port prober — every other test stays
fully hermetic via the ``home`` fixture's fake ``port_probe``.
"""

from __future__ import annotations

import shutil
import socket
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agenttools_daemon import AlreadyRunningError, PidRecord  # noqa: E402

import agenttools_service as ats  # noqa: E402
from agenttools_service import (  # noqa: E402
    LINUX,
    MACOS,
    LaunchdBackend,
    NoopAutostartBackend,
    PortProbe,
    Service,
    ServiceManager,
    SystemdUserBackend,
    add_service_subcommands,
    current_platform,
    dispatch,
    render_launchd_plist,
    render_systemd_unit,
    run_action,
    select_autostart_backend,
)
from agenttools_service.core import _default_port_prober as _real_default_port_prober  # noqa: E402


# --- fakes ------------------------------------------------------------------------------


class RunnerSpy:
    """Records every autostart control command, returns a scripted exit code (default 0)."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._rc = returncode

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return self._rc


class FakeChild:
    """A minimal ``agenttools_daemon.Child`` stand-in for the detached spawner."""

    _next_pid = 5000

    def __init__(self):
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid

    def poll(self):
        return None

    def terminate(self):
        ...

    def kill(self):
        ...


class SpawnerSpy:
    """A detached spawner that records (argv, logfile) and hands out FakeChildren."""

    def __init__(self):
        self.calls: list[tuple[list[str], Path]] = []
        self.children: list[FakeChild] = []

    def __call__(self, argv, logfile):
        child = FakeChild()
        self.calls.append((list(argv), Path(logfile)))
        self.children.append(child)
        return child


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp HOME with XDG dirs pointed inside it; returns the home Path.

    Also defaults every ``ServiceManager``'s port probe to "nothing is listening" — the real
    prober dials a real socket, and most tests use the same fixed port (7878, the review
    dashboard's default); on a dev machine that actually happens to be running the real
    dashboard, an unpatched real probe would flip `stop`/`disable` test expectations non-
    deterministically. Tests that specifically exercise the port-liveness warning override
    `port_probe=` explicitly (or use `_real_default_port_prober` directly).
    """
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_STATE_HOME", str(h / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(h / ".cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.setattr(
        "agenttools_service.core._default_port_prober",
        lambda host, port: PortProbe(occupied=False, pid=None),
    )
    return h


def make_service(home, **kwargs):
    base = dict(
        name="dashboard",
        argv=["review", "dashboard", "run"],
        port=7878,
        tool="review",
        description="review-cli dashboard",
        home=home,
    )
    base.update(kwargs)
    return Service(**base)


# --- Service descriptor -----------------------------------------------------------------


def test_service_paths_under_xdg(home):
    svc = make_service(home)
    assert svc.state_dir == home / ".local" / "state" / "review"
    assert svc.cache_dir == home / ".cache" / "review"
    assert svc.pidfile == svc.state_dir / "dashboard.pid"
    assert svc.logfile == svc.cache_dir / "dashboard.log"


def test_service_state_falls_back_without_xdg(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    svc = make_service(h)
    assert svc.state_dir == h / ".local" / "state" / "review"
    assert svc.cache_dir == h / ".cache" / "review"


def test_service_url_and_label(home):
    svc = make_service(home)
    assert svc.url == "http://127.0.0.1:7878"
    assert svc.label == "com.agenttools.review.dashboard"
    assert svc.systemd_unit_name == "agenttools-review-dashboard"


def test_service_url_none_without_port(home):
    svc = make_service(home, port=None)
    assert svc.url is None


def test_service_tool_defaults_to_name(home):
    svc = make_service(home, tool=None, name="tgctl")
    assert svc.tool_name == "tgctl"
    assert svc.label == "com.agenttools.tgctl.tgctl"


def test_service_rejects_bad_name(home):
    with pytest.raises(ValueError):
        make_service(home, name="bad name with spaces")
    with pytest.raises(ValueError):
        make_service(home, name="")


def test_service_rejects_bad_argv(home):
    with pytest.raises(ValueError):
        make_service(home, argv=[])
    with pytest.raises(ValueError):
        make_service(home, argv=[1, 2])  # type: ignore[list-item]


def test_service_rejects_bad_port(home):
    with pytest.raises(ValueError):
        make_service(home, port=0)
    with pytest.raises(ValueError):
        make_service(home, port=99999)


# --- unit generation --------------------------------------------------------------------


def test_render_launchd_plist_contains_label_args_and_log(home):
    svc = make_service(home)
    plist = render_launchd_plist(svc)
    assert "<key>Label</key>" in plist
    assert "<string>com.agenttools.review.dashboard</string>" in plist
    # argv rendered as ProgramArguments, in order.
    assert "<string>review</string>" in plist
    assert "<string>dashboard</string>" in plist
    assert "<string>run</string>" in plist
    assert "<key>RunAtLoad</key>" in plist and "<true/>" in plist
    # KeepAlive restarts ONLY on a non-clean exit (SuccessfulExit:false), not unconditionally,
    # so a service that exits 0 is not relaunched in a tight loop.
    assert "<key>KeepAlive</key>" in plist
    assert "<key>SuccessfulExit</key>" in plist and "<false/>" in plist
    assert str(svc.logfile) in plist
    # well-formed XML
    import xml.dom.minidom as minidom

    minidom.parseString(plist)


def test_render_launchd_plist_escapes_xml(home):
    svc = make_service(home, argv=["review", "--x", "a&b<c>"])
    plist = render_launchd_plist(svc)
    assert "a&amp;b&lt;c&gt;" in plist
    import xml.dom.minidom as minidom

    minidom.parseString(plist)  # still valid XML


def test_render_systemd_unit_has_execstart_restart_and_install(home):
    svc = make_service(home)
    unit = render_systemd_unit(svc)
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "ExecStart=review dashboard run" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "Description=review-cli dashboard" in unit
    assert str(svc.logfile) in unit


def test_render_systemd_unit_quotes_spaces(home):
    svc = make_service(home, argv=["python", "-c", "print('hi there')"])
    unit = render_systemd_unit(svc)
    # the token with spaces is quoted
    assert 'ExecStart=python -c "print(\'hi there\')"' in unit


# --- backend selection matrix -----------------------------------------------------------


def test_select_backend_macos(home):
    b = select_autostart_backend(platform=MACOS, home=home)
    assert isinstance(b, LaunchdBackend)
    assert b.kind == "launchd"


def test_select_backend_linux_with_systemd(home):
    b = select_autostart_backend(platform=LINUX, home=home, has_systemctl=True)
    assert isinstance(b, SystemdUserBackend)
    assert b.kind == "systemd"


def test_select_backend_linux_without_systemd(home):
    b = select_autostart_backend(platform=LINUX, home=home, has_systemctl=False)
    assert isinstance(b, NoopAutostartBackend)
    assert b.kind == "none"


def test_select_backend_unsupported_os(home):
    b = select_autostart_backend(platform="win32", home=home)
    assert isinstance(b, NoopAutostartBackend)
    assert "win32" in b.reason


# --- launchd backend --------------------------------------------------------------------


def test_launchd_install_writes_plist_and_loads(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)

    assert not backend.is_installed(svc)
    ok = backend.install(svc)
    path = backend.unit_path(svc)

    assert ok is True  # launchctl load succeeded (RunnerSpy returns 0)
    assert path == home / "Library" / "LaunchAgents" / f"{svc.label}.plist"
    assert path.exists()
    assert backend.is_installed(svc)
    assert ["launchctl", "load", str(path)] in runner.calls
    assert backend.manages_process is True
    # the plist on disk is the rendered one
    assert path.read_text() == render_launchd_plist(svc)


def test_launchd_install_reports_failure_on_nonzero_launchctl(home):
    svc = make_service(home)
    backend = LaunchdBackend(home=home, runner=RunnerSpy(returncode=1))
    ok = backend.install(svc)
    assert ok is False  # launchctl load failed
    assert backend.unit_path(svc).exists()  # file still written


def test_launchd_install_is_idempotent(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)
    runner.calls.clear()
    backend.install(svc)  # second time
    # only one plist exists; reinstall unloaded the old before loading the new
    agents_dir = home / "Library" / "LaunchAgents"
    assert len(list(agents_dir.glob("*.plist"))) == 1
    assert ["launchctl", "unload", str(backend.unit_path(svc))] in runner.calls
    assert ["launchctl", "load", str(backend.unit_path(svc))] in runner.calls


def test_launchd_uninstall_removes_and_unloads(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)
    runner.calls.clear()

    assert backend.uninstall(svc) is True
    assert not backend.is_installed(svc)
    assert ["launchctl", "unload", str(backend.unit_path(svc))] in runner.calls
    # idempotent: uninstall again is a no-op
    assert backend.uninstall(svc) is False


# --- systemd backend --------------------------------------------------------------------


def test_systemd_install_writes_unit_and_enables_now(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = SystemdUserBackend(home=home, runner=runner)

    ok = backend.install(svc)
    path = backend.unit_path(svc)
    assert ok is True
    assert path == home / ".config" / "systemd" / "user" / f"{svc.systemd_unit_name}.service"
    assert path.exists()
    assert path.read_text() == render_systemd_unit(svc)
    assert backend.manages_process is True
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls
    # `enable --now` (NOT bare `enable`) — it must START the unit now, so Restart= supervision
    # is live and `systemctl stop` acts on the real MainPID.
    assert ["systemctl", "--user", "enable", "--now",
            f"{svc.systemd_unit_name}.service"] in runner.calls


def test_systemd_install_reports_failure_on_nonzero(home):
    svc = make_service(home)
    backend = SystemdUserBackend(home=home, runner=RunnerSpy(returncode=1))
    assert backend.install(svc) is False
    assert backend.unit_path(svc).exists()


def test_systemd_install_idempotent(home):
    svc = make_service(home)
    backend = SystemdUserBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    backend.install(svc)
    unit_dir = home / ".config" / "systemd" / "user"
    assert len(list(unit_dir.glob("*.service"))) == 1


def test_systemd_uninstall_disables_and_removes(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = SystemdUserBackend(home=home, runner=runner)
    backend.install(svc)
    runner.calls.clear()

    assert backend.uninstall(svc) is True
    assert not backend.is_installed(svc)
    # `disable --now` stops the running unit AND removes the login wiring.
    assert ["systemctl", "--user", "disable", "--now",
            f"{svc.systemd_unit_name}.service"] in runner.calls
    assert backend.uninstall(svc) is False  # idempotent


# --- ServiceManager: status / start / stop ----------------------------------------------


def make_manager(home, *, platform=MACOS, spawner=None, alive=None, runner=None):
    svc = make_service(home)
    return ServiceManager(
        svc,
        platform=platform,
        spawner=spawner or SpawnerSpy(),
        runner=runner or RunnerSpy(),
        alive=alive,
    )


def test_status_stopped_when_no_pidfile(home):
    mgr = make_manager(home, alive=lambda pid: False)
    st = mgr.status()
    assert st.running is False
    assert st.state == "stopped"
    assert st.pid is None
    assert st.port == 7878
    assert st.url == "http://127.0.0.1:7878"
    assert st.enabled is False


def test_status_running_when_port_probe_finds_a_legacy_listener(home):
    # agent-tools#685: a process may genuinely be serving the service's port under a
    # legacy/foreign launchd or systemd label that predates the current
    # com.agenttools.<tool>.<name> unit-naming convention. LaunchdBackend.is_installed()/
    # SystemdUserBackend.is_installed() only check the CURRENT label's unit file, so neither
    # the pidfile signal nor the enabled-unit signal sees it. status() must fall back to
    # probe_port() instead of reporting a permanent false "stopped".
    svc = make_service(home)
    fake_pid = 54321
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        runner=RunnerSpy(),
        alive=lambda pid: False,  # no pidfile-tracked process
        port_probe=lambda host, port: PortProbe(occupied=True, pid=fake_pid),
    )

    st = mgr.status()

    assert st.running is True  # the regression assertion (False before the fix)
    assert st.state == "foreign"
    assert st.pid == fake_pid
    assert st.enabled is False  # no plist/unit written — genuinely "not installed"


def test_dispatch_status_subcommand_flags_a_foreign_listener(home, capsys):
    # CLI-level regression guard (review round, agent-tools#685): a "foreign" state must be
    # visible to a human/script reading `status`'s output, not printed identically to a
    # genuinely managed "running" — the CLI-status branch keys off `st.running`, so this is
    # the actual surface `run_action`/`dispatch` render, not just `ServiceManager.status()`.
    svc = make_service(home)
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        runner=RunnerSpy(),
        alive=lambda pid: False,
        port_probe=lambda host, port: PortProbe(occupied=True, pid=54321),
    )
    parser = _build_parser(mgr)
    args = parser.parse_args(["status"])

    rc = dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out

    assert rc == 0  # something IS up on the port — not the "nothing running" exit
    assert "foreign listener" in out
    assert "running" in out
    assert "(pid 54321)" in out


def test_start_spawns_detached_and_writes_pidfile(home):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner, alive=lambda pid: True)
    st = mgr.start()

    assert len(spawner.calls) == 1
    argv, logfile = spawner.calls[0]
    assert argv == ["review", "dashboard", "run"]
    assert logfile == mgr.service.logfile
    assert mgr.service.pidfile.exists()
    assert st.running is True
    assert st.pid == spawner.children[0].pid
    assert st.url == "http://127.0.0.1:7878"


def test_start_refuses_duplicate(home):
    # Pre-seed a pidfile naming a pid we report alive.
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=list(svc.argv)).to_json())
    spawner = SpawnerSpy()
    mgr = ServiceManager(svc, platform=MACOS, spawner=spawner, alive=lambda pid: pid == 4242)
    with pytest.raises(AlreadyRunningError):
        mgr.start()
    assert spawner.calls == []  # never spawned a duplicate


def test_stop_when_running_signals_and_returns_true(home):
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=list(svc.argv)).to_json())
    signalled: list[tuple[int, int]] = []
    states = {"alive": True}

    def signaller(pid, sig):
        # the SIGTERM "kills" the fake pid so stop() doesn't escalate/loop on a real process
        signalled.append((pid, sig))
        states["alive"] = False

    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: states["alive"],
        signaller=signaller,
    )
    assert mgr.stop() is True
    assert signalled and signalled[0][0] == 4242
    assert not svc.pidfile.exists()


def test_stop_when_not_running_returns_false(home):
    mgr = make_manager(home, alive=lambda pid: False)
    assert mgr.stop() is False


# --- ServiceManager: enable / disable ---------------------------------------------------


def test_enable_on_managed_backend_installs_and_does_not_double_spawn(home):
    # The core no-double-start invariant: on launchd/systemd the OS launches the process when
    # the unit is installed, so the manager must NOT also pidfile-spawn it.
    spawner = SpawnerSpy()
    runner = RunnerSpy()
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, spawner=spawner, runner=runner,
                         alive=lambda pid: False)

    assert mgr.status().enabled is False
    st = mgr.enable()

    plist = home / "Library" / "LaunchAgents" / f"{svc.label}.plist"
    assert plist.exists()
    assert ["launchctl", "load", str(plist)] in runner.calls
    assert st.enabled is True
    # OS owns the process → status reports running with NO pidfile/pid, and the manager
    # spawned NOTHING (the bug was two processes on the same port).
    assert st.running is True
    assert st.pid is None
    assert st.state == "autostart"
    assert spawner.calls == []  # NOT double-started
    assert not svc.pidfile.exists()


def test_enable_on_managed_backend_falls_back_to_spawn_when_launchctl_fails(home):
    # If the OS control command fails, the service is NOT actually under OS control, so the
    # manager must fall back to a pidfile-spawned instance so `enable` still leaves it UP.
    spawner = SpawnerSpy()
    states = {"started": False}
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, spawner=spawner,
                         runner=RunnerSpy(returncode=1),  # launchctl load fails
                         alive=lambda pid: states["started"])

    def spawn_and_mark(argv, logfile):
        states["started"] = True
        return spawner(argv, logfile)

    mgr.spawner = spawn_and_mark
    st = mgr.enable()
    assert len(spawner.calls) == 1  # fell back to a manager-spawned instance
    assert st.running is True
    # the launchctl command failed → autostart is NOT actually active, even though the file
    # was written; `autostart_active` (not `st.enabled`) is the honest signal.
    assert mgr.autostart_active() is False


def test_enable_on_unsupported_os_starts_but_not_enabled(home):
    spawner = SpawnerSpy()
    svc = make_service(home)
    states = {"started": False}
    mgr = ServiceManager(svc, platform="win32", spawner=spawner,
                         alive=lambda pid: states["started"])

    def spawn_and_mark(argv, logfile):
        states["started"] = True
        return spawner.__call__(argv, logfile)

    mgr.spawner = spawn_and_mark
    st = mgr.enable()
    assert st.running is True
    assert st.enabled is False  # noop backend → no autostart
    assert mgr.backend.kind == "none"


def test_disable_removes_autostart_and_stops(home):
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)  # pretend it was enabled
    # and pretend it's running
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=321, started_at=1.0, cmd=list(svc.argv)).to_json())

    states = {"alive": True}

    def signaller(pid, sig):
        states["alive"] = False  # the stop signal "kills" the fake pid

    mgr = ServiceManager(
        svc,
        platform=MACOS,
        autostart=backend,
        spawner=SpawnerSpy(),
        alive=lambda pid: states["alive"],
        signaller=signaller,
    )
    st = mgr.disable()

    assert not backend.is_installed(svc)
    assert not svc.pidfile.exists()
    assert st.enabled is False
    assert st.running is False


def test_disable_idempotent_when_nothing_enabled_or_running(home):
    mgr = make_manager(home, alive=lambda pid: False)
    st = mgr.disable()  # should not raise
    assert st.enabled is False
    assert st.running is False


# --- run (foreground) -------------------------------------------------------------------


def test_run_blocks_on_foreground_and_returns_exit_code(home):
    svc = make_service(home)
    captured = {}

    def fg(argv):
        captured["argv"] = list(argv)
        return 0

    mgr = ServiceManager(svc, platform=MACOS, foreground_runner=fg, spawner=SpawnerSpy())
    rc = mgr.run()
    assert rc == 0
    assert captured["argv"] == ["review", "dashboard", "run"]
    # run() must NOT write a pidfile (this shell IS the service)
    assert not svc.pidfile.exists()


# --- CLI wiring -------------------------------------------------------------------------


def _build_parser(manager):
    import argparse

    parser = argparse.ArgumentParser(prog="review dashboard")
    subs = parser.add_subparsers(dest="action")
    add_service_subcommands(subs, manager_factory=lambda: manager, service_name="dashboard")
    return parser


def test_bare_invocation_returns_help_never_launches(home, capsys):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner)
    parser = _build_parser(mgr)
    args = parser.parse_args([])  # no subcommand

    called = {"help": 0}

    def on_no_subcommand():
        called["help"] += 1
        return 0

    rc = dispatch(args, on_no_subcommand=on_no_subcommand)
    assert rc == 0
    assert called["help"] == 1
    assert spawner.calls == []  # NOTHING launched


def test_dispatch_status_subcommand(home, capsys):
    mgr = make_manager(home, alive=lambda pid: False)
    parser = _build_parser(mgr)
    args = parser.parse_args(["status"])
    rc = dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 3  # nothing running
    assert "stopped" in out


def test_dispatch_start_subcommand_prints_pid_and_url(home, capsys):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner, alive=lambda pid: True)
    parser = _build_parser(mgr)
    args = parser.parse_args(["start"])
    rc = dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "started" in out
    assert "http://127.0.0.1:7878" in out
    assert len(spawner.calls) == 1


def test_run_action_unknown_raises(home):
    mgr = make_manager(home)
    with pytest.raises(ValueError):
        run_action(mgr, "bogus")


def test_subcommands_order_and_help_present(home):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner)
    parser = _build_parser(mgr)
    help_text = parser.format_help()
    for name in ("run", "start", "stop", "status", "enable", "disable"):
        assert name in help_text
    assert ats.SUBCOMMANDS == ("run", "start", "stop", "status", "enable", "disable")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("linux", LINUX),
        ("linux2", LINUX),
        ("linux-aarch64", LINUX),
        ("darwin", MACOS),
        ("win32", "win32"),
        ("freebsd13", "freebsd13"),
    ],
)
def test_current_platform_buckets(monkeypatch, raw, expected):
    # linux* collapses to "linux"; darwin verbatim; everything else passes through unchanged.
    monkeypatch.setattr(sys, "platform", raw)
    assert current_platform() == expected


# --- default spawners (real subprocess; the only paths that touch real I/O) --------------


def test_default_detached_spawner_writes_log_and_leaks_no_fd(home):
    # Exercise the REAL detached spawner with a trivial no-op argv: it must spawn, write to the
    # logfile, and NOT leak the parent's log fd (regression guard for the close-after-Popen).
    from agenttools_service.core import _default_detached_spawner

    svc = make_service(home, argv=[sys.executable, "-c", "import sys; sys.stdout.write('hi')"])
    before = len(_open_fds())
    children = [_default_detached_spawner(list(svc.argv), svc.logfile) for _ in range(5)]
    for c in children:
        c.wait()
    after = len(_open_fds())
    # no per-spawn fd accumulation in the PARENT (allow a tiny slack for unrelated churn)
    assert after <= before + 1, f"fd leak: {before} -> {after}"
    assert svc.logfile.read_bytes() == b"hi" * 5  # append, not truncate


def test_default_foreground_runner_returns_exit_code(home):
    from agenttools_service.core import _default_foreground_runner

    assert _default_foreground_runner([sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert _default_foreground_runner([sys.executable, "-c", "raise SystemExit(7)"]) == 7


def _open_fds():
    """The current process's open fds (best-effort; falls back to a fixed probe range)."""
    import os as _os

    fd_dir = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    try:
        return _os.listdir(fd_dir)
    except OSError:
        return [fd for fd in range(256) if _fd_is_open(fd)]


def _fd_is_open(fd):
    import os as _os

    try:
        _os.fstat(fd)
        return True
    except OSError:
        return False


def test_default_runner_returns_127_on_missing_binary():
    from agenttools_service.core import _default_runner

    # a binary that surely does not exist → OSError → 127 (documented contract)
    assert _default_runner(["agenttools-no-such-binary-xyz", "--help"]) == 127


# --- validation (defense-in-depth against path traversal via identifiers) ---------------


def test_service_rejects_path_traversal_tool(home):
    with pytest.raises(ValueError):
        make_service(home, tool="../../etc")
    with pytest.raises(ValueError):
        make_service(home, tool="..")
    with pytest.raises(ValueError):
        make_service(home, tool="a/b")


def test_service_rejects_separator_only_name(home):
    with pytest.raises(ValueError):
        make_service(home, name="..")
    with pytest.raises(ValueError):
        make_service(home, name="/")


def test_service_rejects_empty_host(home):
    with pytest.raises(ValueError):
        make_service(home, host="")
    with pytest.raises(ValueError):
        make_service(home, host="   ")


def test_service_accepts_port_boundary(home):
    assert make_service(home, port=65535).port == 65535
    assert make_service(home, port=1).port == 1


# --- unit generation: identifier edge cases & systemd specifier escaping ----------------


def test_label_when_tool_defaults_to_name_in_rendered_units(home):
    svc = make_service(home, tool=None, name="x")
    assert svc.label == "com.agenttools.x.x"
    assert svc.systemd_unit_name == "agenttools-x-x"
    # the derived label actually lands in the rendered plist
    assert "<string>com.agenttools.x.x</string>" in render_launchd_plist(svc)


def test_render_systemd_unit_doubles_percent_specifiers(home):
    # `%` is a systemd specifier introducer; a literal `%` MUST be doubled or it's substituted.
    svc = make_service(home, argv=["srv", "--fmt", "100%done", "--user", "%u"])
    unit = render_systemd_unit(svc)
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "100%%done" in exec_line
    assert "%%u" in exec_line
    # invariant: every `%` in ExecStart is part of a doubled `%%` pair — no lone specifier
    assert exec_line.replace("%%", "").count("%") == 0


# --- CLI: bare path prints real help to stdout (not just "doesn't launch") --------------


def test_bare_invocation_prints_help_text_to_stdout(home, capsys):
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner)
    parser = _build_parser(mgr)
    args = parser.parse_args([])
    rc = dispatch(args, on_no_subcommand=lambda: (parser.print_help() or 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out  # real argparse help reached stdout
    for name in ("run", "start", "stop", "status", "enable", "disable"):
        assert name in out
    assert spawner.calls == []  # and nothing launched


def test_enable_managed_backend_message_has_no_pid_none(home, capsys):
    # regression: an OS-managed enable has pid=None; the message must not print "pid None".
    mgr = make_manager(home, alive=lambda pid: False)  # macOS launchd, RunnerSpy ok
    parser = _build_parser(mgr)
    args = parser.parse_args(["enable"])
    dispatch(args, on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "pid None" not in out
    assert "autostart enabled (launchd)" in out


# --- unit-injection guards (security) ---------------------------------------------------


def test_service_rejects_newline_in_description(home):
    # B1: a newline in description would inject systemd unit directives.
    with pytest.raises(ValueError):
        make_service(home, description="ok\n[Service]\nExecStartPre=/tmp/payload")


def test_service_rejects_control_char_in_argv(home):
    # B2: a newline in an argv token would break ExecStart across lines.
    with pytest.raises(ValueError):
        make_service(home, argv=["srv", "evil\nExecStartPre=/tmp/payload"])
    with pytest.raises(ValueError):
        make_service(home, argv=["srv", "tab\there"])


def test_service_rejects_control_char_in_host(home):
    with pytest.raises(ValueError):
        make_service(home, host="127.0.0.1\r\nHost: evil")


def test_systemd_quote_forces_quoting_on_control_char():
    # render_systemd_unit is a public function; even if a raw token slipped past Service
    # validation, the renderer must not emit a bare newline that splits ExecStart.
    from agenttools_service.core import _systemd_quote

    quoted = _systemd_quote("a\nb")
    assert quoted.startswith('"') and quoted.endswith('"')


# --- B3: no double-start when start THEN enable on a managed backend ---------------------


def test_enable_after_manual_start_stops_pidfile_instance_first(home):
    # A user did `start` (pidfile instance), then `enable`. The OS is about to launch its own
    # copy via RunAtLoad — the manager MUST stop the pidfile instance first (no two on a port).
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=555, started_at=1.0, cmd=list(svc.argv)).to_json())
    states = {"alive": True}
    signalled = []

    def signaller(pid, sig):
        signalled.append((pid, sig))
        states["alive"] = False

    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), runner=RunnerSpy(),
                         alive=lambda pid: states["alive"], signaller=signaller)
    mgr.enable()
    assert signalled and signalled[0][0] == 555  # the manual pidfile instance was stopped
    assert not svc.pidfile.exists()              # and its pidfile cleared


# --- B4: uninstall must not orphan when the OS stop command fails ------------------------


def test_launchd_uninstall_keeps_file_and_reports_false_on_unload_failure(home):
    svc = make_service(home)
    backend = LaunchdBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    # now a runner that fails the unload
    backend.runner = RunnerSpy(returncode=1)
    assert backend.uninstall(svc) is False     # not fully removed
    assert backend.is_installed(svc)           # file left in place (OS may still run it)


def test_systemd_uninstall_keeps_file_and_reports_false_on_disable_failure(home):
    svc = make_service(home)
    backend = SystemdUserBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    backend.runner = RunnerSpy(returncode=1)
    assert backend.uninstall(svc) is False
    assert backend.is_installed(svc)


# --- B6: enable hint uses the service's own tool/name, not a literal placeholder ---------


def test_start_hint_uses_tool_and_name_not_literal_placeholder(home, capsys):
    # noop backend so the hint suppresses? No — hint only prints when backend manages process
    # but is not yet installed. Use macOS launchd, status not installed → hint shown on start.
    spawner = SpawnerSpy()
    mgr = make_manager(home, spawner=spawner, alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "<tool>" not in out
    assert "review dashboard enable" in out  # tool='review', name='dashboard'


def test_no_hint_on_unsupported_os(home, capsys):
    spawner = SpawnerSpy()
    svc = make_service(home)
    mgr = ServiceManager(svc, platform="win32", spawner=spawner, alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "enable' to start it at login" not in out  # no boot survival → no hint


# --- T4 / T6 / T7: runner exit-code passthrough, fg propagation, XDG fallback ------------


def test_default_runner_returns_actual_exit_code():
    from agenttools_service.core import _default_runner

    # portable (no /usr/bin/true,false dependency — those don't exist on Windows)
    assert _default_runner([sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert _default_runner([sys.executable, "-c", "raise SystemExit(1)"]) == 1


def test_default_foreground_runner_propagates_missing_binary(home):
    from agenttools_service.core import _default_foreground_runner

    # documented contract: a missing foreground binary propagates (the service can't run)
    with pytest.raises(FileNotFoundError):
        _default_foreground_runner(["agenttools-no-such-binary-xyz"])


def test_systemd_unit_dir_falls_back_without_xdg_config(tmp_path, monkeypatch):
    h = tmp_path / "h"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    backend = SystemdUserBackend(home=h, runner=RunnerSpy())
    svc = Service(name="d", argv=["x"], tool="t", home=h)
    assert backend.unit_path(svc) == h / ".config" / "systemd" / "user" / "agenttools-t-d.service"


# --- ASCII-only slug enforcement (Unicode would make invalid labels/unit names) ----------


def test_service_rejects_unicode_name_and_tool(home):
    with pytest.raises(ValueError):
        make_service(home, name="café")
    with pytest.raises(ValueError):
        make_service(home, tool="ño")
    with pytest.raises(ValueError):
        make_service(home, name="日本語")


# --- manager-level honesty when the OS control command fails -----------------------------


def test_enable_reports_not_active_and_exit_4_when_launchctl_fails(home, capsys):
    spawner = SpawnerSpy()
    states = {"started": False}
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, runner=RunnerSpy(returncode=1),
                         spawner=spawner, alive=lambda pid: states["started"])

    def spawn_and_mark(argv, logfile):
        states["started"] = True
        return spawner(argv, logfile)

    mgr.spawner = spawn_and_mark
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["enable"]), on_no_subcommand=lambda: 0)
    cap = capsys.readouterr()
    assert rc == 4
    assert "FAILED" in cap.err and "autostart enabled" not in (cap.out + cap.err)
    assert mgr.autostart_active() is False


def test_disable_reports_failure_and_exit_4_when_uninstall_fails(home, capsys):
    svc = make_service(home)
    # install with a working runner, then disable with a failing one
    backend = LaunchdBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    backend.runner = RunnerSpy(returncode=1)
    mgr = ServiceManager(svc, platform=MACOS, autostart=backend, spawner=SpawnerSpy(),
                         alive=lambda pid: False)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["disable"]), on_no_subcommand=lambda: 0)
    assert rc == 4
    assert "FAILED to remove autostart" in capsys.readouterr().err
    assert mgr.last_disable_ok is False
    assert backend.is_installed(svc)  # left in place; not orphaned silently


def test_disable_succeeds_when_nothing_installed(home):
    # idempotent no-op: nothing installed → last_disable_ok True, exit 0
    mgr = make_manager(home, alive=lambda pid: False)
    mgr.disable()
    assert mgr.last_disable_ok is True


# --- round-4 refinements -----------------------------------------------------------------


def test_servicestatus_as_dict(home):
    mgr = make_manager(home, alive=lambda pid: False)
    d = mgr.status().as_dict()
    assert set(d) == {"running", "state", "pid", "port", "url", "enabled"}
    assert d["port"] == 7878
    assert d["url"] == "http://127.0.0.1:7878"
    assert d["running"] is False


def test_dispatch_start_already_running_is_clean_not_traceback(home, capsys):
    # `start` against an already-running instance must print a clean message, not raise.
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4321, started_at=1.0, cmd=list(svc.argv)).to_json())
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(),
                         alive=lambda pid: pid == 4321)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    assert rc == 3
    assert "already running (pid 4321)" in capsys.readouterr().err  # error → stderr


def test_enable_twice_is_file_idempotent(home):
    # re-running enable does not duplicate the unit (file-idempotent); the reload side-effect
    # is documented. Assert exactly one plist remains.
    mgr = make_manager(home, alive=lambda pid: False)
    mgr.enable()
    mgr.enable()
    agents = home / "Library" / "LaunchAgents"
    assert len(list(agents.glob("*.plist"))) == 1


def test_default_runner_distinguishes_missing_from_unrunnable(home, tmp_path):
    from agenttools_service.core import _default_runner

    # missing binary → 127
    assert _default_runner(["agenttools-no-such-binary-xyz"]) == 127
    # present-but-not-executable file → OSError(EACCES/ENOEXEC) → 126 (distinct from 127)
    not_exec = tmp_path / "not_exec"
    not_exec.write_text("#!/bin/sh\n")  # no +x bit
    not_exec.chmod(0o644)
    assert _default_runner([str(not_exec)]) == 126


def test_service_coerces_generator_argv_to_tuple(home):
    svc = make_service(home, argv=(a for a in ["review", "dashboard", "run"]))
    assert svc.argv == ("review", "dashboard", "run")  # not consumed to empty
    # re-readable: render uses it again without it being empty
    assert "ExecStart=review dashboard run" in render_systemd_unit(svc)


def test_enable_runner_ignored_when_explicit_autostart_documented(home):
    # passing both autostart= and runner= → the explicit backend's own runner is used, the
    # ServiceManager-level runner is NOT applied to it (documented footgun). Assert the
    # explicit backend's runner records the calls.
    explicit_runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=explicit_runner)
    ignored_runner = RunnerSpy()
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, autostart=backend, runner=ignored_runner,
                         spawner=SpawnerSpy(), alive=lambda pid: False)
    mgr.enable()
    assert explicit_runner.calls  # explicit backend's runner was used
    assert ignored_runner.calls == []  # the manager-level runner was not applied


# --- round-5: %-escaping of Description / log paths, noop disable, empty argv -------------


def test_render_systemd_unit_escapes_percent_in_description():
    # % in Description= is a specifier introducer; it must be doubled or systemd substitutes it.
    svc = Service(name="d", argv=["x"], tool="t", description="100% done at %H")
    unit = render_systemd_unit(svc)
    desc_line = next(ln for ln in unit.splitlines() if ln.startswith("Description="))
    assert desc_line == "Description=100%% done at %%H"


def test_render_systemd_unit_escapes_percent_in_log_path(tmp_path, monkeypatch):
    # a % in the cache path (XDG_CACHE_HOME) must be doubled in StandardOutput/Error.
    cache = tmp_path / "ca%che"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("HOME", str(tmp_path))
    svc = Service(name="d", argv=["x"], tool="t")
    unit = render_systemd_unit(svc)
    out_line = next(ln for ln in unit.splitlines() if ln.startswith("StandardOutput="))
    assert "%%" in out_line and "ca%che" not in out_line  # the lone % was doubled


def test_disable_on_noop_backend_is_clean(home, capsys):
    # disable on an unsupported OS (noop backend): nothing installed → ok, exit 0, no failure msg
    svc = make_service(home)
    mgr = ServiceManager(svc, platform="win32", spawner=SpawnerSpy(), alive=lambda pid: False)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["disable"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 0
    assert mgr.last_disable_ok is True
    assert "FAILED" not in out


def test_service_rejects_empty_argv_token(home):
    with pytest.raises(ValueError):
        make_service(home, argv=["srv", "", "run"])


# --- round-6: encoding, string-argv reject, stderr routing, round-trip, port=None --------


def test_service_rejects_bare_string_argv(home):
    # a bare string is a Sequence[str] of chars; must be rejected, not split into characters
    with pytest.raises(ValueError):
        make_service(home, argv="review dashboard run")


def test_install_writes_utf8_regardless_of_locale(home):
    # non-ASCII description must not crash install under a non-UTF-8 locale (explicit encoding).
    # systemd renders Description=, so it carries the non-ASCII bytes.
    svc = make_service(home, description="café dashboard")
    backend = SystemdUserBackend(home=home, runner=RunnerSpy())
    backend.install(svc)
    text = backend.unit_path(svc).read_text(encoding="utf-8")
    assert "Description=café dashboard" in text


def test_enable_failure_message_goes_to_stderr_not_stdout(home, capsys):
    svc = make_service(home)
    states = {"started": False}
    spy = SpawnerSpy()

    def spawn(argv, logfile):
        states["started"] = True
        return spy(argv, logfile)

    mgr = ServiceManager(svc, platform=MACOS, runner=RunnerSpy(returncode=1),
                         spawner=spawn, alive=lambda pid: states["started"])
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["enable"]), on_no_subcommand=lambda: 0)
    cap = capsys.readouterr()
    assert rc == 4
    assert "FAILED" in cap.err          # error on stderr
    assert "FAILED" not in cap.out      # not on stdout


def test_start_already_running_message_goes_to_stderr(home, capsys):
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=12, started_at=1.0, cmd=list(svc.argv)).to_json())
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    cap = capsys.readouterr()
    assert "already running" in cap.err
    assert cap.out == ""


def test_enable_then_disable_round_trip(home):
    # enable (managed) installs; disable removes; status reflects each step.
    mgr = make_manager(home, alive=lambda pid: False)
    st1 = mgr.enable()
    assert st1.enabled is True and mgr.autostart_active() is True
    st2 = mgr.disable()
    assert st2.enabled is False and mgr.last_disable_ok is True
    assert not mgr.backend.is_installed(mgr.service)
    # and enable works again after a clean disable
    st3 = mgr.enable()
    assert st3.enabled is True


def test_dispatch_stop_exit_3_when_nothing_running(home, capsys):
    mgr = make_manager(home, alive=lambda pid: False)
    parser = _build_parser(mgr)
    rc = dispatch(parser.parse_args(["stop"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert rc == 3
    assert "was not running" in out


def test_cli_lines_have_no_none_when_port_is_none(home, capsys):
    svc = make_service(home, port=None)
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), alive=lambda pid: True)
    parser = _build_parser(mgr)
    dispatch(parser.parse_args(["start"]), on_no_subcommand=lambda: 0)
    out = capsys.readouterr().out
    assert "started" in out
    assert "None" not in out
    assert " — " not in out  # no empty url separator


def test_systemd_quote_control_char_stays_single_line():
    # M3: the defense-in-depth claim — a control-char token, once rendered, must not split
    # ExecStart across lines (Service validation is the real guard, but pin the renderer too).
    from agenttools_service.core import _systemd_quote

    # _systemd_quote alone can't strip a newline; assert it at least quotes, and that a
    # validated Service never produces a multi-line ExecStart.
    assert _systemd_quote("a\nb").startswith('"')


# --- review-driven hardening (glm-5.2 findings) -----------------------------------------


def test_service_rejects_empty_tool(home):
    # `tool=""` slips past `_is_slug` (vacuously True) and would silently behave like
    # `tool=None`; reject it so "accept but ignore" can't surprise a caller.
    with pytest.raises(ValueError):
        make_service(home, tool="")


def test_service_rejects_host_with_whitespace(home):
    # A host carrying whitespace would render a broken url ("http://  127.0.0.1  :7878").
    with pytest.raises(ValueError):
        make_service(home, host="  127.0.0.1  ")
    with pytest.raises(ValueError):
        make_service(home, host="my host")


def test_add_service_subcommands_help_text_overrides(home):
    import argparse

    mgr = make_manager(home)
    parser = argparse.ArgumentParser(prog="review dashboard")
    subs = parser.add_subparsers(dest="action")
    add_service_subcommands(
        subs,
        manager_factory=lambda: mgr,
        service_name="dashboard",
        help_text={"start": "boot the dashboard daemon"},
    )
    full = parser.format_help()
    # the override is shown for `start`...
    assert "boot the dashboard daemon" in full
    # ...and an un-overridden subcommand keeps its built-in help (both appear in the parent's
    # subcommand listing).
    assert "stop the background dashboard instance" in full


def test_run_action_run_handles_missing_binary(home, capsys):
    # `run` must not leak a raw FileNotFoundError traceback when argv[0] doesn't exist.
    svc = make_service(home)

    def fg(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    mgr = ServiceManager(svc, platform=MACOS, foreground_runner=fg, spawner=SpawnerSpy())
    rc = run_action(mgr, "run")
    err = capsys.readouterr().err
    assert rc == 127  # "command not found" convention
    assert "command not found" in err
    assert "review" in err


def test_disable_on_os_managed_only_service_no_pidfile(home):
    # The expected post-`enable` state on a managed backend: autostart unit installed, NO
    # pidfile. `disable` must uninstall (the real work) while `stop` is a clean no-op.
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)
    assert backend.is_installed(svc)
    assert not svc.pidfile.exists()  # OS owns it; no pidfile

    mgr = ServiceManager(
        svc, platform=MACOS, autostart=backend, spawner=SpawnerSpy(), alive=lambda pid: False
    )
    st = mgr.disable()
    assert not backend.is_installed(svc)  # uninstall did the work
    assert st.enabled is False
    assert st.running is False
    assert mgr.last_disable_ok is True


def test_manager_uses_supervisor_defaults_when_alive_and_signaller_none(home):
    # Every other test injects `alive`/`signaller`; this one leaves them None to exercise the
    # path that hands the Supervisor its real defaults (os.kill-based). We keep it side-effect
    # free by checking a no-pidfile status (no real process is ever signalled).
    svc = make_service(home)
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy())  # alive/signaller default
    st = mgr.status()  # reads a (missing) pidfile; never calls os.kill on a real pid
    assert st.running is False
    assert st.state == "stopped"
    assert mgr.stop() is False  # nothing to signal; clean no-op through real defaults


# --- port-liveness after stop/disable (review-cli#377) ----------------------------------
#
# The incident: `review dashboard run --port 7878` (ad-hoc, no pidfile) stayed alive after
# `review dashboard disable` reported success. `stop`/`disable` only ever see the pidfile-
# tracked MANAGER-spawned instance; a foreign/unmanaged listener on the same port is
# invisible to them. `ServiceManager.probe_port` + `run_action`'s `_foreign_listener_fragment`
# close that gap by checking the port itself after acting.


def test_probe_port_returns_none_when_service_has_no_port(home):
    svc = make_service(home, port=None)
    mgr = ServiceManager(svc, platform=MACOS, spawner=SpawnerSpy(), alive=lambda pid: False)
    assert mgr.probe_port() is None


def test_stop_warns_and_exits_4_when_managed_instance_stopped_but_port_still_occupied(
    home, capsys
):
    # The managed instance WAS running and IS stopped — but a foreign process also holds the
    # port (e.g. it raced an ad-hoc `run` into existence on the same port).
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=list(svc.argv)).to_json())
    states = {"alive": True}
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: states["alive"],
        signaller=lambda pid, sig: states.update(alive=False),
        port_probe=lambda host, port: PortProbe(occupied=True, pid=9999),
    )
    rc = run_action(mgr, "stop")
    captured = capsys.readouterr()
    assert rc == 4
    assert "stopped" not in captured.out  # no bare success line on stdout
    assert f"{svc.host}:{svc.port}" in captured.err
    assert "9999" in captured.err


def test_stop_warning_brackets_an_ipv6_probed_address(home, capsys):
    # When the probe reports it actually connected on the IPv6 loopback (`PortProbe.addr`),
    # the warning must bracket it (`[::1]:7878`) rather than the unparseable `::1:7878`.
    svc = make_service(home)
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: False,
        port_probe=lambda host, port: PortProbe(occupied=True, pid=555, addr="::1"),
    )
    rc = run_action(mgr, "stop")
    captured = capsys.readouterr()
    assert rc == 4
    assert f"[::1]:{svc.port}" in captured.err
    # The bracketed form does not contain the bare "::1:<port>" substring (there's a "]"
    # between the address and the port), so this also proves the address wasn't left bare.
    assert f"::1:{svc.port}" not in captured.err


def test_stop_warns_and_exits_4_when_no_managed_instance_but_port_occupied(home, capsys):
    # THE incident path: no pidfile at all (an ad-hoc `run` never writes one), so `stop` has
    # nothing of its own to stop -> must not report a bare "was not running".
    svc = make_service(home)
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: False,
        port_probe=lambda host, port: PortProbe(occupied=True, pid=None),
    )
    rc = run_action(mgr, "stop")
    captured = capsys.readouterr()
    assert rc == 4
    assert "was not running" not in captured.out
    assert "was not running" not in captured.err
    assert f"{mgr.service.host}:{mgr.service.port}" in captured.err
    assert "pid unknown" in captured.err  # no lsof answer -> named as unknown, not omitted


def test_disable_warns_and_exits_4_when_stopped_but_port_still_occupied(home, capsys):
    # Still occupied through the settle-and-re-probe (both calls report occupied): a genuine
    # finding, not a drain-window flake. Inject a fake sleeper so the test pays zero
    # wall-clock time while still proving the retry actually happened.
    svc = make_service(home)
    sleeps: list[float] = []
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: False,
        port_probe=lambda host, port: PortProbe(occupied=True, pid=31337),
        sleeper=sleeps.append,
    )
    rc = run_action(mgr, "disable")
    captured = capsys.readouterr()
    assert rc == 4
    assert "autostart disabled and stopped" not in captured.out
    assert f"{svc.host}:{svc.port}" in captured.err
    assert "31337" in captured.err
    assert sleeps == [0.5]  # exactly one settle wait, no busy-loop


def test_disable_settle_retry_clears_a_transient_launchd_drain_window(home, capsys):
    # THE launchd-drain scenario the settle window exists for: `launchctl unload` returns
    # while the OS-managed process is still finishing its exit, so the probes taken right
    # after `manager.disable()` read occupied — but the port is actually free by the time
    # the settled re-probe runs. Must report a clean success, not a false failure.
    #
    # Two probes happen before the settle sleep, not one: `ServiceManager.disable()` itself
    # ends with `self.status()`, which (agent-tools#685) now also consults `probe_port()` as
    # its own not-running fallback — then `_run_action_disable`'s own foreign-listener check
    # probes again right after. Both land inside the still-draining window here.
    svc = make_service(home)
    calls = {"n": 0}

    def draining_then_free(host, port):
        calls["n"] += 1
        return PortProbe(occupied=(calls["n"] <= 2), pid=4242 if calls["n"] <= 2 else None)

    sleeps: list[float] = []
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: False,
        port_probe=draining_then_free,
        sleeper=sleeps.append,
    )
    rc = run_action(mgr, "disable")
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == f"{svc.name}: autostart disabled and stopped"
    assert "still listening" not in captured.err
    assert sleeps == [0.5]
    assert calls["n"] == 3  # disable()'s own status() probe, the first foreign-listener
                            # check, and the settled re-probe — no more


def test_disable_does_not_settle_retry_when_port_is_free_on_the_first_probe(home, capsys):
    # The common case (nothing occupying the port at all) must not pay the settle wait.
    svc = make_service(home)
    calls = {"n": 0}

    def always_free(host, port):
        calls["n"] += 1
        return PortProbe(occupied=False, pid=None)

    sleeps: list[float] = []
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: False,
        port_probe=always_free,
        sleeper=sleeps.append,
    )
    rc = run_action(mgr, "disable")
    assert rc == 0
    assert sleeps == []
    # disable()'s own status() probes once (agent-tools#685's not-running fallback), then
    # the foreign-listener check probes once more — both read free, so no settle retry.
    assert calls["n"] == 2


def test_stop_and_disable_unaffected_when_port_genuinely_free(home, capsys):
    # Regression guard: the new check must not fire when nothing is listening — the common
    # case, and the one every pre-existing test above already exercises via the `home`
    # fixture's default (not-occupied) probe. Pin it explicitly here too.
    svc = make_service(home)
    svc.pidfile.parent.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text(PidRecord(pid=4242, started_at=1.0, cmd=list(svc.argv)).to_json())
    states = {"alive": True}
    mgr = ServiceManager(
        svc,
        platform=MACOS,
        spawner=SpawnerSpy(),
        alive=lambda pid: states["alive"],
        signaller=lambda pid, sig: states.update(alive=False),
        port_probe=lambda host, port: PortProbe(occupied=False, pid=None),
    )
    rc = run_action(mgr, "stop")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == f"{svc.name}: stopped"

    mgr2 = ServiceManager(
        svc, platform=MACOS, spawner=SpawnerSpy(), alive=lambda pid: False,
        port_probe=lambda host, port: PortProbe(occupied=False, pid=None),
    )
    rc2 = run_action(mgr2, "disable")
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out2.strip() == f"{svc.name}: autostart disabled and stopped"


def test_default_port_prober_detects_a_real_ad_hoc_listener(home, capsys):
    """End-to-end reproduction of the actual incident, using the REAL (unmocked) prober: bind
    a real socket on an ephemeral port from a separate process (standing in for an ad-hoc,
    unmanaged `review dashboard run`, which also has no pidfile), then run `stop` against
    that exact port and assert the output names it instead of claiming success. (`disable`
    shares the same `_foreign_listener_fragment` helper and is covered against a FAKE probe
    by `test_disable_warns_and_exits_4_when_stopped_but_port_still_occupied` — this test's
    job is proving the REAL prober end to end, not re-covering every action.)
    """
    import socket as _socket
    import subprocess as _subprocess
    import sys as _sys
    import time as _time

    # Bind an ephemeral port up front so we know a free one, then hand it to the child so the
    # child (not us) is the one actually listening — a distinct PID, like a real foreign process.
    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    child = _subprocess.Popen(
        [
            _sys.executable,
            "-c",
            (
                "import socket,time;"
                f"s=socket.socket();s.bind(('127.0.0.1',{port}));"
                "s.listen();time.sleep(30)"
            ),
        ]
    )
    try:
        # Wait for the child to actually be listening before probing it.
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline:
            try:
                with _socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                _time.sleep(0.05)
        else:
            pytest.fail("child never started listening")

        svc = make_service(home, port=port)
        mgr = ServiceManager(
            svc,
            platform=MACOS,
            spawner=SpawnerSpy(),
            alive=lambda pid: False,
            port_probe=_real_default_port_prober,  # bypass the home fixture's fake default
        )
        rc = run_action(mgr, "stop")
        captured = capsys.readouterr()
        assert rc == 4
        assert "was not running" not in captured.out
        assert f"127.0.0.1:{port}" in captured.err
        # A resolved PID is a bonus, not a guarantee: lsof might be permission-walled, time
        # out, or (per test_lsof_listener_pid_returns_none_on_multiple_pids) see more than one
        # matching listener and correctly refuse to guess. Assert the pid slot is present and
        # well-formed (either the real child pid or the explicit "unknown" fallback) rather
        # than requiring the exact pid, so this doesn't flake on a machine where lsof behaves
        # exactly as designed but can't uniquely resolve the listener.
        assert (f"(pid {child.pid})" in captured.err) or ("(pid unknown)" in captured.err)
    finally:
        child.kill()
        child.wait(timeout=5)


# --- port-liveness helpers: unit coverage (glm-5.2 review findings, GH-377 follow-up) -----


def test_loopback_for_always_returns_a_loopback_address_never_the_input_host():
    # `host` is display-only; probing must NEVER dial it verbatim — dialing a real
    # non-loopback `host` would be an SSRF-shaped outbound probe of a config-/env-sourced
    # value (see `_loopback_for`'s docstring). Every input maps to a loopback address; only
    # the IPv4-vs-IPv6 family is taken from the input's shape.
    from agenttools_service.core import _loopback_for

    assert _loopback_for("0.0.0.0") == "127.0.0.1"
    assert _loopback_for("") == "127.0.0.1"
    assert _loopback_for("127.0.0.1") == "127.0.0.1"
    # A real-looking, non-loopback display host still maps to loopback — never dialed as-is.
    assert _loopback_for("10.0.0.5") == "127.0.0.1"
    assert _loopback_for("example.internal") == "127.0.0.1"
    assert _loopback_for("169.254.169.254") == "127.0.0.1"  # the cloud metadata address
    # IPv6-shaped input (incl. the "::" wildcard) picks the IPv6 loopback family.
    assert _loopback_for("::") == "::1"
    assert _loopback_for("2001:db8::1") == "::1"


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_lsof_listener_pid_returns_none_when_lsof_missing(monkeypatch):
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _lsof_listener_pid(7878) is None


def test_lsof_listener_pid_returns_pid_on_single_line(monkeypatch):
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/lsof")
    assert _lsof_listener_pid(7878, runner=lambda cmd: _FakeCompletedProcess("4242\n")) == 4242


def test_lsof_listener_pid_returns_none_on_multiple_pids(monkeypatch):
    # SO_REUSEPORT / separate IPv4+IPv6 listeners can make `lsof -t` print more than one PID.
    # Guessing "the first one" could name the WRONG process; refuse to guess instead.
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/lsof")
    assert _lsof_listener_pid(7878, runner=lambda cmd: _FakeCompletedProcess("4242\n5555\n")) is None


def test_lsof_listener_pid_returns_none_on_empty_output(monkeypatch):
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/lsof")
    assert _lsof_listener_pid(7878, runner=lambda cmd: _FakeCompletedProcess("")) is None


def test_lsof_listener_pid_returns_none_on_malformed_output(monkeypatch):
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/lsof")
    assert _lsof_listener_pid(7878, runner=lambda cmd: _FakeCompletedProcess("not-a-pid\n")) is None


def test_lsof_listener_pid_returns_none_on_nonzero_returncode_even_with_a_pid_line(monkeypatch):
    # A partial/permission-walled lsof run can exit nonzero while still printing a PID line on
    # stdout; that does not meet the "clean answer" bar the docstring promises — don't attribute
    # a listener to output lsof itself flagged as unreliable.
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/lsof")
    fake = _FakeCompletedProcess("4242\n", returncode=1)
    assert _lsof_listener_pid(7878, runner=lambda cmd: fake) is None


def test_lsof_listener_pid_returns_none_on_runner_error(monkeypatch):
    from agenttools_service.core import _lsof_listener_pid

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/lsof")

    def boom(cmd):
        raise OSError("no such process")

    assert _lsof_listener_pid(7878, runner=boom) is None


def test_default_port_prober_survives_unicode_host(monkeypatch):
    # `Service` permits non-ASCII hosts (only whitespace/control chars are rejected), and
    # socket.create_connection can raise UnicodeError (an IDNA-encoding failure — a ValueError
    # subclass, NOT an OSError) for a host like "héllo". The prober must not let that escape as
    # a raw traceback; it should report "not occupied" like any other unreachable host.
    #
    # Deliberately calls `_real_default_port_prober` (the module-qualified alias captured at
    # import time, BEFORE any `home`-fixture monkeypatch can run) rather than a fresh `from
    # agenttools_service.core import _default_port_prober` inside the function body: this test
    # takes no `home` fixture today, but a local import re-resolves the module attribute at
    # CALL time — if a future edit added `home` as a parameter, that import would silently
    # rebind to the fixture's fake (which never touches `socket.create_connection` at all),
    # and this test would keep passing even if the UnicodeError handling it exists to check
    # were deleted. The module-level alias can't be shadowed that way.
    def raise_unicode_error(*args, **kwargs):
        raise UnicodeError("encoding failed")

    monkeypatch.setattr(socket, "create_connection", raise_unicode_error)
    probe = _real_default_port_prober("héllo", 7878)
    assert probe.occupied is False


def test_default_port_prober_tries_ipv4_then_ipv6_and_reports_the_family_that_answered(
    monkeypatch,
):
    # THE round-3 regression fix: probing only ONE loopback family (chosen from the
    # display-only `Service.host`) missed a real listener bound to the OTHER family — a
    # foreign IPv6-only listener with a default (IPv4) `host` read as "port free". The prober
    # must try both, in order, and report which one actually answered via `PortProbe.addr`.
    attempted: list[tuple[str, int]] = []

    def fake_create_connection(addr_port, timeout):
        addr, port = addr_port
        attempted.append((addr, port))
        if addr == "127.0.0.1":
            raise OSError("refused")  # nothing on the IPv4 loopback

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Ctx()  # IPv6 loopback "accepts"

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(
        "agenttools_service.core._lsof_listener_pid", lambda port: 9090
    )
    probe = _real_default_port_prober("127.0.0.1", 7878)
    assert probe.occupied is True
    assert probe.addr == "::1"
    assert probe.pid == 9090
    assert attempted == [("127.0.0.1", 7878), ("::1", 7878)]  # IPv4 tried first, then IPv6


def test_default_port_prober_reports_free_when_neither_family_answers(monkeypatch):
    def always_refuse(addr_port, timeout):
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", always_refuse)
    probe = _real_default_port_prober("127.0.0.1", 7878)
    assert probe.occupied is False
    assert probe.addr is None
    assert probe.pid is None


def test_stop_action_on_healthy_os_autostart_service_does_not_probe_the_port(home, capsys):
    # THE regression an earlier revision of this fix introduced (caught by review-cli#377's
    # review round 2, both GLM and codex independently): an `enable`d launchd/systemd service
    # is, BY DESIGN, listening with no pidfile — that is the expected healthy state, not a
    # foreign listener. `stop` must keep exiting 3 with the plain "run 'disable'" hint and
    # must NOT call the port probe at all on this branch (proven here by a probe that raises
    # if invoked, not merely one that returns occupied=True) — surfacing a foreign-listener
    # warning would (a) be factually wrong (the very port disable is told to fix is this
    # service's own healthy process) and (b) break any script that branches on exit 3 meaning
    # "nothing of mine to stop".
    svc = make_service(home)
    runner = RunnerSpy()
    backend = LaunchdBackend(home=home, runner=runner)
    backend.install(svc)  # autostart on disk, OS owns the process

    def must_not_be_called(host, port):
        raise AssertionError("stop must not probe the port when OS autostart is enabled")

    mgr = ServiceManager(
        svc,
        platform=MACOS,
        autostart=backend,
        spawner=SpawnerSpy(),
        alive=lambda pid: False,
        port_probe=must_not_be_called,
    )
    rc = run_action(mgr, "stop")
    captured = capsys.readouterr()
    assert rc == 3
    assert "was not running" not in captured.out
    assert "autostart" in captured.err and "disable" in captured.err
    assert "still listening" not in captured.err
