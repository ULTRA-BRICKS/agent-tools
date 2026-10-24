"""agenttools_service — one reusable service-manager for every long-running server.

Every long-running server in the ecosystem (the ``review`` dashboard, ``config-web``,
``tg-ctl``, future daemons) wants the SAME lifecycle subcommands. Instead of each tool
hand-rolling them (slightly differently, and one always forgetting the systemd fallback or
accidentally launching on the bare command), this package gives you one shared manager:

    run      — foreground/blocking (this shell), for ``disable``d / ad-hoc use.
    start    — background: the autostart unit when enabled, else a detached pidfile daemon;
               with ``Service.health_check`` it returns once the server answers.
    restart  — stop then start (one owner either way).
    status   — {running, pid, port, url, enabled}; the unit's real state when enabled.
    stop     — stop the running instance (autostart stays installed).
    logs     — last lines of the service log (-n N, -f).
    enable   — install OS autostart (launchd / systemd --user / a no-systemd fallback)
               AND start it now.
    disable  — remove OS autostart AND stop.

A bare invocation (no subcommand) returns HELP — it NEVER launches anything.

This is the SAME machinery the roadmap wants shared by tg-ctl autostart + the
daemon-supervisor (§3): one service-management helper, not per-tool copies.

Quick start
-----------
    from agenttools_service import Service, ServiceManager

    svc = Service(
        name="dashboard",
        argv=["review", "dashboard", "run"],   # the foreground server command
        port=7878,
        tool="review",
        description="review-cli dashboard",
    )
    mgr = ServiceManager(svc)

    mgr.run()       # foreground, blocking -> exit code
    mgr.start()     # background detached -> ServiceStatus(running=True, pid=..., url=...)
    mgr.status()    # -> ServiceStatus(running=..., pid=..., port=..., url=..., enabled=...)
    mgr.stop()      # -> True if a live process was signalled
    mgr.enable()    # install launchd/systemd autostart + start now
    mgr.disable()   # remove autostart + stop

Wiring it into a CLI (argparse)
-------------------------------
    import argparse
    from agenttools_service import add_service_subcommands, dispatch

    parser = argparse.ArgumentParser(prog="review dashboard")
    subs = parser.add_subparsers(dest="action")
    add_service_subcommands(subs, manager_factory=lambda: ServiceManager(svc),
                            service_name="dashboard")
    args = parser.parse_args(argv)
    raise SystemExit(dispatch(args, on_no_subcommand=lambda: (parser.print_help() or 0)))

Supported-OS matrix and the full reference live in
``lib/agenttools_service/README.md``.
"""

from __future__ import annotations

from .core import (
    LINUX,
    MACOS,
    SUBCOMMANDS,
    AutostartBackend,
    LaunchdBackend,
    NoopAutostartBackend,
    PortProbe,
    Service,
    ServiceManager,
    ServiceStatus,
    ServiceCheck,
    ServiceControlError,
    UnitState,
    SystemdUserBackend,
    add_service_subcommands,
    service_checks,
    current_platform,
    dispatch,
    render_launchd_plist,
    render_systemd_unit,
    run_logs,
    run_action,
    select_autostart_backend,
)

__all__ = [
    "Service",
    "ServiceManager",
    "ServiceStatus",
    "PortProbe",
    "AutostartBackend",
    "LaunchdBackend",
    "SystemdUserBackend",
    "NoopAutostartBackend",
    "select_autostart_backend",
    "render_launchd_plist",
    "render_systemd_unit",
    "current_platform",
    "add_service_subcommands",
    "dispatch",
    "run_action",
    "run_logs",
    "service_checks",
    "ServiceCheck",
    "ServiceControlError",
    "UnitState",
    "SUBCOMMANDS",
    "MACOS",
    "LINUX",
]

__version__ = "0.3.0"
