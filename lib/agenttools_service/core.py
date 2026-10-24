"""agenttools_service.core — one reusable service-manager for every long-running server.

WHAT THIS FILE IS
    The engine behind :class:`ServiceManager`. A :class:`Service` descriptor names a
    long-running server (its ``argv``, a default ``port``, and where its pidfile / logfile /
    autostart unit live). :class:`ServiceManager` gives that server the identical lifecycle
    subcommands every daemon in the ecosystem wants:

        run      — foreground/blocking (this shell), for ``disable``d / ad-hoc use.
        start    — background detached daemon; writes a pidfile, returns immediately.
        status   — {running, pid, port, url} from the pidfile + a liveness probe.
        stop     — stop the background instance, remove the pidfile.
        enable   — install OS autostart (launchd / systemd --user / a no-systemd fallback)
                   AND start it in the background now.
        disable  — remove autostart AND stop.

    A bare invocation (no subcommand) returns HELP — it NEVER launches anything.

HOW IT'S REACHED AT RUNTIME
    A consumer (``review dashboard``, ``config-web``, ``tg-ctl``, a future daemon) builds a
    :class:`Service`, wraps it in a :class:`ServiceManager`, and wires
    ``add_service_subcommands(...)`` into its argparse so ``<tool> <service>
    run|start|stop|status|enable|disable`` dispatches to the manager. The same machinery the
    roadmap wants shared by tg-ctl autostart + the daemon-supervisor (§3).

INVARIANTS / DESIGN
    - **Stdlib only at import time.** ``os`` / ``sys`` / ``shutil`` / ``subprocess`` /
      ``socket`` / ``pathlib`` / ``dataclasses`` / ``typing``. Zero third-party deps, so a
      consumer's ``--help`` stays fast and offline. The ONLY in-ecosystem dependency is
      ``agenttools_daemon`` (also stdlib-only), reused for the pidfile + start/stop/status
      machinery rather than reimplementing it (shared-util single source).
    - **Pidfile is the cross-process source of truth** — delegated to
      ``agenttools_daemon.Supervisor``. ``start`` writes it, ``stop`` reads+removes it,
      ``status`` reads+probes it. A ``stop`` in a different process finds the daemon via the
      file alone.
    - **Background = detached.** ``start`` spawns ``argv`` with ``start_new_session=True`` so
      the daemon outlives the launching shell; stdout/stderr go to the service's logfile.
    - **Autostart is pluggable + idempotent + removable.** ``enable``/``disable`` go through
      an :class:`AutostartBackend` chosen by OS: launchd LaunchAgent on macOS, a systemd
      ``--user`` unit on Linux with ``systemctl`` present, else a no-op fallback that still
      lets ``start``/``stop`` work (just no boot-survival). Re-running ``enable`` overwrites
      the unit in place (no duplicates); ``disable`` removes it and is a no-op if absent.
    - **Everything OS/process/path-related is injectable.** ``home`` / ``platform`` /
      ``has_systemctl`` / ``spawner`` / ``runner`` / ``alive`` are all parameters, so the
      whole manager — INCLUDING enable/disable and the launchd/systemd unit generation — is
      tested under a tmp HOME with fakes, touching no real autostart and no real process.

PAST BUGS THIS GUARDS AGAINST
    - A "service" CLI where the bare command launches the server is a footgun: a user typing
      ``review dashboard`` to read help accidentally starts a daemon. Here the bare path is
      HELP-only; launching requires an explicit ``start``/``run``/``enable``.
    - Hand-rolled autostart that appends a fresh LaunchAgent/unit on every ``enable`` leaves
      duplicates that fight at boot. ``enable`` is idempotent: it writes the unit to a
      deterministic path, overwriting.
    - Per-tool copies of this logic drift (slightly different pidfile formats, one forgets
      the systemd fallback, one launches on the bare command). One shared manager keeps every
      daemon's lifecycle identical.
    - ``stop``/``disable`` used to report an unqualified success ("stopped" / "disabled and
      stopped") purely from the pidfile's point of view — but the pidfile only tracks a
      MANAGER-spawned instance. An ad-hoc, unmanaged process bound to the same port (e.g.
      ``review dashboard run``) has no pidfile at all, so ``stop``/``disable`` could not see
      it and still claimed success while it kept running and holding the port (agent-tools
      review-cli#377). ``ServiceManager.probe_port`` now checks the port itself, after
      acting, and ``run_action`` warns + exits nonzero instead of lying about success.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, Sequence

# agenttools_daemon is the only in-ecosystem dep — also stdlib-only. Reused for the pidfile +
# start/stop/status machinery so this layer does not reimplement (and subtly diverge from) it.
from agenttools_daemon import AlreadyRunningError
from agenttools_daemon import Status as DaemonStatus
from agenttools_daemon import Supervisor

Argv = Sequence[str]

# ---------------------------------------------------------------------------------------
# OS detection — injectable, so tests pin a platform without a real OS.
# ---------------------------------------------------------------------------------------

MACOS = "darwin"
LINUX = "linux"


def current_platform() -> str:
    """The running platform key (``sys.platform``), normalized to our coarse buckets.

    Anything starting ``linux`` collapses to ``"linux"``; macOS is ``"darwin"``; everything
    else is returned verbatim (so the matrix can report it as unsupported-for-autostart).
    """
    plat = sys.platform
    if plat.startswith("linux"):
        return LINUX
    return plat


def _default_home() -> Path:
    """The user's home, honoring ``$HOME`` so a tmp-HOME test is fully isolated."""
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


# ---------------------------------------------------------------------------------------
# Service descriptor
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    """A long-running server's identity + where its runtime files live.

    Parameters
    ----------
    name
        The service id — used to name the pidfile, logfile, and autostart unit. Keep it a
        filesystem- and label-safe slug (``[A-Za-z0-9_.-]``); it is validated on construction.
    argv
        The command that runs the server **in the foreground** (the thing ``run`` blocks on
        and ``start`` detaches). A list/tuple of strings, e.g.
        ``["review", "dashboard", "run"]`` or ``[sys.executable, "-m", "mytool.server"]``.
    port
        The default TCP port the server listens on (used to build ``url`` for ``status``).
        ``None`` if the service is not a network server.
    host
        The host the ``url`` should point at (display only). Defaults to ``127.0.0.1``.
    tool
        The owning tool name, used to namespace the autostart label
        (``com.agenttools.<tool>.<name>`` / ``agenttools-<tool>-<name>``). Defaults to
        ``name`` when omitted.
    description
        A one-line human description, surfaced in the generated unit + ``--help``.
    home
        The home directory under which state/cache live. Injected by tests (a tmp HOME);
        defaults to ``$HOME``. The state dir (pid + autostart unit) is
        ``$XDG_STATE_HOME/<tool>`` (fallback ``~/.local/state/<tool>``); the cache dir
        (logs) is ``$XDG_CACHE_HOME/<tool>`` (fallback ``~/.cache/<tool>``).
    health_check
        Optional zero-arg probe returning ``True`` once the server answers (e.g. an HTTP
        ``/health`` GET). When set, ``run_action``/``dispatch`` wait after
        ``start``/``restart``/``enable`` up to ``ready_timeout`` seconds for it and fail with
        exit 4 plus the log path, because a live pid does not mean the port is serving (a
        server that exits at boot, or a port held by another process, would otherwise read as
        a successful start). The ``ServiceManager`` methods themselves return immediately;
        library callers call ``manager.wait_ready()`` (``None`` = healthy, else why not).
    ready_timeout
        Seconds ``health_check`` may take to turn true. Default 10.
    cli
        How a user invokes this service's lifecycle verbs (``"hsw-editor --port 9000"``), used in
        hints and doctor fixes (``<cli> enable``). Default: ``"<tool> <name>"`` when ``tool`` is
        set, else ``name``.
    """

    name: str
    argv: Argv
    port: Optional[int] = None
    host: str = "127.0.0.1"
    tool: Optional[str] = None
    description: str = ""
    home: Optional[Path] = None
    health_check: Optional[Callable[[], bool]] = None
    ready_timeout: float = 10.0
    cli: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name or not _is_slug(self.name):
            raise ValueError(
                f"service name must be a non-empty slug [A-Za-z0-9_.-], got {self.name!r}"
            )
        # `tool` ends up in filesystem paths (state/cache dirs, plist/unit paths) and in the
        # autostart label, so it MUST be a slug too — an unvalidated `tool` like "../../etc"
        # would write pid/log/plist/unit files outside the intended dirs (arbitrary file
        # write for any consumer that sources `tool` from config/env). `_is_slug` rejects "/"
        # and any other separator; "." and ".." are rejected explicitly (they slip past a
        # naive slug check, and "." would also collapse the path).
        # `tool=""` (empty) is rejected too: `_is_slug("")` is vacuously True so it would slip
        # through, then `tool_name = self.tool or self.name` silently treats it as `None` — a
        # confusing accept-but-ignore. Pass `tool=None` to mean "default to name"; an empty
        # string is a bug, so fail closed.
        if self.tool is not None and (
            self.tool == "" or not _is_slug(self.tool) or self.tool in (".", "..")
        ):
            raise ValueError(
                f"tool must be a non-empty slug [A-Za-z0-9_.-] (not '.'/'..'), got {self.tool!r}"
            )
        if not self.name.strip("._-"):
            raise ValueError(f"service name must not be only separators, got {self.name!r}")
        # Coerce argv to a tuple FIRST: a generator passed as argv is truthy but is consumed by
        # the first iteration below, leaving a later `list(self.argv)` empty (a silent broken
        # daemon / empty ExecStart). A tuple is safe to re-iterate and keeps the frozen dataclass
        # hashable. `object.__setattr__` because the dataclass is frozen.
        # Reject a bare string BEFORE coercion: `tuple("review")` would silently become
        # ("r","e","v",…) — every per-char token is a valid 1-char str, so it sails through
        # and renders as `ExecStart=r e v i e w`. A str is a Sequence[str], so the type hint
        # won't catch it; reject explicitly.
        if isinstance(self.argv, (str, bytes)):
            raise ValueError("argv must be a list/tuple of strings, not a bare string")
        object.__setattr__(self, "argv", tuple(self.argv))
        if not self.argv or not all(isinstance(a, str) for a in self.argv):
            raise ValueError("argv must be a non-empty sequence of strings")
        if any(a == "" for a in self.argv):
            # an empty token would render as a bare `""` in ExecStart — almost always a bug
            # (a dropped variable), so fail closed rather than launch a malformed command.
            raise ValueError("argv tokens must be non-empty strings")
        # Reject control characters (notably newlines) in everything that is rendered into a
        # systemd unit / launchd plist or printed. A newline in `description` or an argv token
        # would otherwise inject arbitrary unit directives (`\n[Service]\nExecStartPre=…`) that
        # systemd runs at every login — arbitrary code execution for any consumer that sources
        # these from config. `name`/`tool` are already slug-constrained (no control chars get
        # past `_is_slug`), so this covers the free-text fields.
        for token in self.argv:
            if _has_control_char(token):
                raise ValueError(f"argv token must not contain control characters: {token!r}")
        if _has_control_char(self.description):
            raise ValueError("description must not contain control characters (e.g. newlines)")
        if self.cli is not None and (not self.cli.strip() or _has_control_char(self.cli)):
            raise ValueError("cli must be a non-blank string without control characters")
        if self.port is not None and not (0 < int(self.port) < 65536):
            raise ValueError(f"port must be in 1..65535 or None, got {self.port!r}")
        if not 0 < self.ready_timeout < float("inf"):  # also rejects NaN
            raise ValueError(f"ready_timeout must be a finite number of seconds > 0, got {self.ready_timeout!r}")
        # Reject a blank/control-char host AND any host carrying whitespace: a value like
        # "  127.0.0.1  " or "my host" would otherwise render into `url` as
        # "http://  127.0.0.1  :7878" — a silently broken URL. The host lands verbatim in the
        # URL, so it must be a single whitespace-free token; fail closed rather than emit a URL
        # no client can use.
        if (
            not self.host
            or _has_control_char(self.host)
            or any(c.isspace() for c in self.host)
        ):
            raise ValueError(
                "host must be a non-empty string with no whitespace or control characters"
            )

    # --- identity --------------------------------------------------------------------

    @property
    def tool_name(self) -> str:
        """The owning tool (``tool`` if set, else ``name``)."""
        return self.tool or self.name

    @property
    def label(self) -> str:
        """Reverse-DNS-ish autostart label, unique per (tool, service).

        macOS launchd wants a reverse-DNS label; systemd wants a unit name. We derive both
        from the same stem so a service is identifiable across either backend.
        """
        return f"com.agenttools.{self.tool_name}.{self.name}"

    @property
    def systemd_unit_name(self) -> str:
        """The systemd ``--user`` unit filename stem (``.service`` appended by the backend)."""
        return f"agenttools-{self.tool_name}-{self.name}"

    # --- paths -----------------------------------------------------------------------

    @property
    def _home(self) -> Path:
        return self.home or _default_home()

    @property
    def state_dir(self) -> Path:
        """Where the pidfile + autostart unit live: ``$XDG_STATE_HOME/<tool>``.

        Falls back to ``~/.local/state/<tool>``. Read from the environment on every access so
        a test's ``XDG_STATE_HOME`` (or a per-process override) is honored.
        """
        base = os.environ.get("XDG_STATE_HOME")
        root = Path(base) if base else self._home / ".local" / "state"
        return root / self.tool_name

    @property
    def cache_dir(self) -> Path:
        """Where the logfile lives: ``$XDG_CACHE_HOME/<tool>`` (fallback ``~/.cache/<tool>``)."""
        base = os.environ.get("XDG_CACHE_HOME")
        root = Path(base) if base else self._home / ".cache"
        return root / self.tool_name

    @property
    def pidfile(self) -> Path:
        return self.state_dir / f"{self.name}.pid"

    @property
    def logfile(self) -> Path:
        return self.cache_dir / f"{self.name}.log"

    @property
    def url(self) -> Optional[str]:
        """``http://<host>:<port>`` when a port is set, else ``None``."""
        if self.port is None:
            return None
        return f"http://{self.host}:{self.port}"


def _is_slug(text: str) -> bool:
    # ASCII-only: `str.isalnum()` accepts Unicode letters (é, ñ, CJK, …), which pass here but
    # produce invalid launchd labels / systemd unit names (both want ASCII). Enforce the
    # documented [A-Za-z0-9_.-] by requiring each char be ASCII as well as alnum.
    return all((c.isascii() and c.isalnum()) or c in "_.-" for c in text)


def _has_control_char(text: str) -> bool:
    """True if ``text`` contains any C0/C1 control char (newlines, tabs, NUL, etc.).

    Used to reject values that would break out of a single line in a rendered systemd unit /
    launchd plist (the unit-injection guard). ``\\t`` is included: a tab in ``ExecStart`` is a
    token separator and a stray one would split an argument.
    """
    return any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in text)


# ---------------------------------------------------------------------------------------
# Status — a thin view a CLI prints. Carries the daemon state plus port/url.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceStatus:
    """A snapshot a CLI prints for ``status``.

    ``running`` mirrors ``agenttools_daemon``'s liveness probe (pidfile present + pid alive),
    OR'd with the OS-managed-autostart and legacy-listener fallbacks below. ``state`` is one
    of: ``running`` / ``stale`` / ``stopped`` (the underlying daemon state), ``autostart``
    (an OS-managed, manager-unspawned instance), or ``foreign`` (agent-tools#685 — a live
    process on the port that neither the pidfile nor the installed-unit signal accounts for,
    e.g. one running under a legacy/foreign OS-autostart label). ``enabled`` reflects whether
    OS autostart is installed.
    """

    running: bool
    state: str
    pid: Optional[int]
    port: Optional[int]
    url: Optional[str]
    enabled: bool = False

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "url": self.url,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class PortProbe:
    """Whether something is currently listening on a service's port, independent of the
    pidfile.

    ``occupied`` is the authoritative signal (a live TCP accept on the port). ``pid`` is a
    best-effort identification of the listener (via ``lsof``) — ``None`` when unresolvable
    (no ``lsof`` on this host, a permission wall, or the listener exited between the connect
    probe and the ``lsof`` call). ``addr`` is the loopback address the connect actually
    succeeded against (``"127.0.0.1"`` or ``"::1"``) when known — ``None`` for a
    caller-supplied fake probe that doesn't track it (message formatting falls back to
    :func:`_loopback_for` in that case). Callers must treat ``occupied`` as ground truth and
    ``pid``/``addr`` as a bonus, never require either.
    """

    occupied: bool
    pid: Optional[int] = None
    addr: Optional[str] = None


# The two loopback addresses :func:`_default_port_prober` tries, in order.
_LOOPBACK_ADDRS = ("127.0.0.1", "::1")


def _loopback_for(host: str) -> str:
    """A best-guess loopback address for DISPLAY when a probe result carries no ``addr`` of
    its own (e.g. a test-supplied fake :class:`PortProbe`) — deliberately ignores ``host``
    beyond an IPv6-vs-IPv4 hint; never dials ``host`` itself.

    ``Service.host`` is documented as **display-only** (it feeds the ``url`` shown to a
    human; nothing in ``Service``/``ServiceManager`` ever binds a socket to it — the actual
    server process picks its own bind address independently), so this is a fallback for
    WORDING a warning, not a connection target: :func:`_default_port_prober` dials both
    loopback families directly (see ``_LOOPBACK_ADDRS``) rather than picking one via this
    function — probing only the family this heuristic would have guessed, chosen from a
    field that doesn't reliably say which family the daemon actually bound, was a proven
    false-negative in an earlier revision (review-cli#377 review round 3: a foreign listener
    on the OTHER family read as "port free"). This function's remaining job is cosmetic:
    anything containing ``:`` (an IPv6 literal, incl. the ``::`` wildcard) guesses the IPv6
    loopback (``::1``); everything else guesses the IPv4 loopback (``127.0.0.1``).
    """
    if ":" in host:
        return "::1"
    return "127.0.0.1"


def _lsof_listener_pid(
    port: int, *, runner: Optional[Callable[[Sequence[str]], "subprocess.CompletedProcess"]] = None
) -> Optional[int]:
    """Best-effort PID of the process LISTENing on ``port`` (macOS/Linux, via ``lsof``).

    Returns ``None`` on anything short of a clean, UNAMBIGUOUS single-PID answer: no ``lsof``
    binary, a nonzero/timed-out/malformed run, no output, OR **more than one** PID line
    (``lsof -t`` prints one PID per matching socket — SO_REUSEPORT, or separate IPv4/IPv6
    listeners on the same port from different processes, can legitimately produce several).
    Guessing "the first one" in that case can name the WRONG process in the warning message,
    which is worse than naming none — so this returns ``None`` and the caller falls back to
    "(pid unknown)" instead of a plausible-looking lie. Never raises. The port-occupied signal
    from :func:`_default_port_prober`'s connect probe is authoritative regardless; this is
    purely a "name the culprit if we can" nicety for the warning message.

    ``runner`` is an injectable ``Callable[[argv], CompletedProcess]`` (defaults to a real
    ``subprocess.run``) so tests can exercise every branch (missing binary, timeout, 0/1/N
    PID lines) without a real ``lsof`` process.
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    run = runner or _default_lsof_runner
    try:
        completed = run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        # A nonzero exit (e.g. a partial/permission-walled run) means the docstring's "clean
        # answer" bar isn't met even if stdout happens to carry a PID line — don't attribute
        # a listener to output lsof itself flagged as unreliable.
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        return int(lines[0])
    except ValueError:
        return None


def _default_lsof_runner(cmd: Sequence[str]) -> "subprocess.CompletedProcess":
    """Real ``lsof`` invocation for :func:`_lsof_listener_pid` (list-argv, no shell)."""
    return subprocess.run(  # noqa: S603
        list(cmd), check=False, capture_output=True, text=True, timeout=3
    )


def _default_port_prober(host: str, port: int) -> PortProbe:
    """Real liveness probe: attempt a TCP connect to a service's port.

    ``host`` is accepted (to match the ``Callable[[str, int], PortProbe]`` seam contract) but
    deliberately UNUSED beyond the fact that a caller passed one — see :func:`_loopback_for`'s
    docstring for why dialing it would be wrong. Instead this tries BOTH loopback address
    families in turn, IPv4 (``127.0.0.1``) then IPv6 (``::1``): a service's descriptor doesn't
    reliably say which family the daemon actually bound, and probing only one produced a
    proven false-negative (review-cli#377 review round 3). Occupied if EITHER family accepts
    a connection; a short timeout (0.5s) per attempt keeps ``stop``/``disable`` responsive
    even when a port is filtered rather than refused.

    Any connect failure on an address (``ConnectionRefusedError``, DNS/other ``OSError``, or a
    ``UnicodeError`` — a ``ValueError`` subclass, NOT an ``OSError`` — from an address literal
    that somehow fails to parse) means "nothing here", so the next family is tried; if BOTH
    fail, "nothing is listening" — the common, fast case. (The ``UnicodeError`` catch is
    defensive-only: both dialed literals are fixed ASCII constants, so IDNA encoding can't
    actually fail for them today — kept because a caller-controlled probe target is not
    inconceivable for a future revision of this seam, and the cost of catching a class that
    can't currently fire is zero.)
    """
    for addr in _LOOPBACK_ADDRS:
        try:
            with socket.create_connection((addr, port), timeout=0.5):
                pass
        except (OSError, UnicodeError):
            continue
        return PortProbe(occupied=True, pid=_lsof_listener_pid(port), addr=addr)
    return PortProbe(occupied=False, pid=None)


@dataclass(frozen=True)
class UnitState:
    """``systemctl --user show`` answer for a service unit.

    ``active_state`` is ``None`` when systemd could not answer (no user bus, ``systemctl``
    missing or sandboxed): callers then fall back to the pidfile/installed-unit inference.
    """

    active_state: Optional[str]
    main_pid: Optional[int] = None
    error: str = ""

    @property
    def answered(self) -> bool:
        return self.active_state is not None

    @property
    def up(self) -> bool:
        """Something of the unit exists (also while it starts, auto-restarts or stops): ``stop`` acts."""
        return self.active_state in ("active", "activating", "reloading", "deactivating")

    @property
    def serving(self) -> bool:
        """A process is running. A crash loop waiting in ``auto-restart`` is ``activating`` with no MainPID."""
        return self.active_state in ("active", "reloading") or (
            self.active_state == "activating" and self.main_pid is not None
        )


class ServiceControlError(RuntimeError):
    """The OS refused a lifecycle command (``systemctl --user start|stop|restart`` failed)."""


def _default_capture(cmd: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run a control/query command capturing stdout+stderr. Never raises.

    Same exit-code convention as :func:`_default_runner`: missing binary ``127``, any other
    ``OSError`` or a timeout ``126``.
    """
    try:
        return subprocess.run(  # noqa: S603
            list(cmd), check=False, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(cmd), 127, "", "command not found")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(cmd), 126, "", str(exc))


def _no_answer(cmd: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Query seam used when a test injected its own ``runner``: never ask the real systemd."""
    return subprocess.CompletedProcess(list(cmd), 127, "", "no state query (injected runner)")


# ---------------------------------------------------------------------------------------
# Autostart backends
# ---------------------------------------------------------------------------------------


class AutostartBackend(Protocol):
    """A per-OS mechanism that makes a :class:`Service` start at login/boot.

    Idempotent and removable: ``install`` overwrites any prior unit (no duplicates),
    ``uninstall`` is a no-op when nothing is installed, ``is_installed`` reports presence.
    ``kind`` is a short tag for status/help (``"launchd"`` / ``"systemd"`` / ``"none"``).

    ``manages_process`` is the ownership flag that prevents a double-start: when ``True``
    (launchd / systemd), installing the unit ALSO launches the process now (launchd
    ``RunAtLoad`` / ``systemctl --user enable --now``), so the manager must NOT also spawn its
    own pidfile-tracked daemon — the OS is the single owner. When ``False`` (the no-autostart
    fallback) there is no OS to run anything, so the manager falls back to a pidfile ``start``.
    """

    kind: str
    manages_process: bool

    def install(self, service: Service) -> bool:  # pragma: no cover - structural
        """Write the autostart unit and register/launch it. ``True`` if fully successful.

        ``False`` means the unit file was written but the OS control command failed (e.g.
        ``launchctl``/``systemctl`` returned nonzero) — so ``is_installed`` may still be
        ``True`` on disk while the service is not actually under OS control.
        """
        ...

    def uninstall(self, service: Service) -> bool:  # pragma: no cover - structural
        """Remove the unit (and stop any OS-managed process). ``True`` if one was present."""
        ...

    def is_installed(self, service: Service) -> bool:  # pragma: no cover - structural
        ...

    def unit_path(self, service: Service) -> Path:  # pragma: no cover - structural
        ...


# --- unit generation (pure, directly tested) -------------------------------------------


def render_launchd_plist(service: Service) -> str:
    """Render a launchd LaunchAgent ``.plist`` for ``service`` (pure; no I/O).

    ``RunAtLoad`` starts it at load/login; ``KeepAlive`` restarts it **only on a non-clean
    exit** (``SuccessfulExit: false``) rather than unconditionally — a plain ``KeepAlive=true``
    would relaunch a service that exited 0 in a tight loop. This mirrors systemd's
    ``Restart=on-failure``. stdout/stderr are redirected to the logfile.

    PATH caveat: launchd runs ``ProgramArguments[0]`` with a minimal environment, NOT the
    user's shell ``PATH`` — a bare ``argv[0]`` like ``"review"`` may not resolve, and the
    agent silently won't start. This renderer is intentionally pure (no filesystem lookup), so
    it does NOT resolve ``argv[0]``; pass an ABSOLUTE ``argv[0]`` (e.g. ``shutil.which("review")``
    or ``sys.executable``) in the :class:`Service` for a reliable LaunchAgent.
    """
    args = "".join(f"\t\t<string>{_xml_escape(a)}</string>\n" for a in service.argv)
    log = _xml_escape(str(service.logfile))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "\t<key>Label</key>\n"
        f"\t<string>{_xml_escape(service.label)}</string>\n"
        "\t<key>ProgramArguments</key>\n"
        f"\t<array>\n{args}\t</array>\n"
        "\t<key>RunAtLoad</key>\n"
        "\t<true/>\n"
        "\t<key>KeepAlive</key>\n"
        "\t<dict>\n"
        "\t\t<key>SuccessfulExit</key>\n"
        "\t\t<false/>\n"
        "\t</dict>\n"
        "\t<key>StandardOutPath</key>\n"
        f"\t<string>{log}</string>\n"
        "\t<key>StandardErrorPath</key>\n"
        f"\t<string>{log}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_systemd_unit(service: Service) -> str:
    """Render a systemd ``--user`` ``.service`` unit for ``service`` (pure; no I/O).

    ``Restart=on-failure`` survives crashes; ``WantedBy=default.target`` is the user-session
    equivalent of "enable at login". argv is rendered as a single ``ExecStart`` line with each
    token quoted so paths/args with spaces survive.

    Call ONLY with a validated :class:`Service`: this renderer escapes spacing and ``%``
    specifiers but does NOT and cannot neutralize newlines (systemd units are line-oriented —
    a newline terminates a directive even inside quotes). ``Service`` rejects control chars at
    construction; that validation is the actual injection guard.

    NOTE: ``%`` is a specifier introducer in EVERY directive value, not just ``ExecStart`` —
    so ``Description=`` and the ``append:`` log paths are ``%%``-escaped too. ``append:`` has no
    quoting syntax, so a SPACE in the log path (e.g. a username with a space) would truncate the
    path; that is a systemd limitation, documented rather than worked around.
    """
    exec_start = " ".join(_systemd_quote(a) for a in service.argv)
    desc = _systemd_escape_value(service.description
                                 or f"{service.tool_name} {service.name} service")
    log = _systemd_escape_value(str(service.logfile))
    return (
        "[Unit]\n"
        f"Description={desc}\n"
        # No `Wants=/After=network-online.target`: that is a SYSTEM target and the user manager
        # does not know it (`systemctl --user status network-online.target` -> not found), so
        # the lines ordered nothing. A service that needs the network at boot must retry.
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _xml_escape(text: str) -> str:
    """Escape XML metacharacters, including quotes.

    All current interpolations land in element CONTENT, where ``"``/``'`` need no escaping —
    but escaping them anyway is free and means a future maintainer who moves a value into an
    attribute (``<string foo="...">``) can't introduce an injection. Order matters: ``&``
    first so we don't double-escape the entities we just produced.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _systemd_escape_value(value: str) -> str:
    """Escape a non-``ExecStart`` directive value (``Description=``, ``append:`` paths).

    ``%`` is a specifier introducer in every systemd directive value, so a literal ``%`` must
    be doubled or it is substituted with host/unit data at parse/display time. These directives
    take the rest of the line verbatim (no token splitting), so no quoting is needed — only the
    ``%`` doubling. (A space in an ``append:`` path still truncates it; systemd has no quoting
    there — that is a documented limitation, not something escaping can fix.)
    """
    return value.replace("%", "%%")


def _systemd_quote(token: str) -> str:
    """Quote a single ``ExecStart`` token for systemd, escaping its specifiers.

    systemd treats ``%`` as a specifier introducer inside ``ExecStart=`` (``%H`` → hostname,
    ``%u`` → user, …), so a literal ``%`` MUST be doubled to ``%%`` or it is silently
    substituted with host/user data — even inside quotes. We double ``%`` first, then quote
    the whole token if it contains whitespace/quotes/backslashes so paths and args with spaces
    survive intact.
    """
    token = token.replace("%", "%%")
    # Quote on whitespace/quote/backslash so tokens with spaces survive as one argument.
    # NOTE: quoting does NOT neutralize a newline — systemd unit files are line-oriented, so a
    # newline (even inside double quotes) terminates the ExecStart= assignment. The real guard
    # against newline injection is Service validation (`_has_control_char` rejects them at
    # construction); this function only handles spacing/escaping of well-formed tokens.
    if token and all(c not in token for c in ' \t"\\') and not _has_control_char(token):
        return token
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --- concrete backends ------------------------------------------------------------------


@dataclass
class LaunchdBackend:
    """macOS autostart via a per-user LaunchAgent ``.plist`` + ``launchctl``.

    The plist lives at ``~/Library/LaunchAgents/<label>.plist``. ``launchctl`` calls go
    through an injected ``runner`` (defaults to a real ``subprocess.run``) so a test can
    assert the load/unload commands without touching the real ``launchctl``.

    NOTE: this uses the legacy ``launchctl load``/``unload`` verbs. They still work on current
    macOS but are deprecated in favor of ``launchctl bootstrap gui/$UID <path>`` /
    ``bootout gui/$UID/<label>``. Migration is deferred — the modern verbs need the user's UID
    and a domain target, which widens the seam; the load/unload shape is pinned by the test
    suite so the eventual switch is deliberate, not accidental.
    """

    home: Path
    runner: Callable[[Sequence[str]], int] = field(default=None)  # type: ignore[assignment]
    kind: str = "launchd"
    # launchd runs the job on load (RunAtLoad) and at every login — it OWNS the process when
    # enabled, so the manager must not also pidfile-spawn it (see ServiceManager.enable).
    manages_process: bool = True

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = _default_runner

    def unit_path(self, service: Service) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{service.label}.plist"

    def install(self, service: Service) -> bool:
        """Write the plist and ``launchctl load`` it (which launches it now via RunAtLoad).

        Returns ``True`` only if ``launchctl load`` succeeded — a nonzero rc (sandbox-blocked
        launchctl, malformed plist) yields ``False`` so the manager reports ``enabled=False``
        and warns, rather than pretending autostart is live just because the file is on disk.
        """
        path = self.unit_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        service.logfile.parent.mkdir(parents=True, exist_ok=True)
        # Overwrite-in-place = idempotent (no duplicate agents). Unload an old copy first so
        # launchctl doesn't refuse the reload, then load the fresh one.
        if path.exists():
            self.runner(["launchctl", "unload", str(path)])
        # Explicit UTF-8: write_text defaults to the locale encoding, so a non-UTF-8 locale
        # (C/POSIX, some CI) would raise UnicodeEncodeError on a legal non-ASCII description.
        path.write_text(render_launchd_plist(service), encoding="utf-8")
        rc = self.runner(["launchctl", "load", str(path)])
        return rc == 0

    def uninstall(self, service: Service) -> bool:
        path = self.unit_path(service)
        if not path.exists():
            return False
        # `launchctl unload` stops the running job AND removes it from launchd, so a separate
        # stop is unnecessary on macOS. Gate the file removal on its success: if unload failed
        # (sandbox-blocked, job loaded under a different path), the OS may still be running the
        # process — removing the file then would orphan it and make `is_installed` lie. Leave
        # the file in place and report failure so the caller knows the service is NOT stopped.
        rc = self.runner(["launchctl", "unload", str(path)])
        if rc != 0:
            return False
        path.unlink()
        return True

    def is_installed(self, service: Service) -> bool:
        return self.unit_path(service).exists()


@dataclass
class SystemdUserBackend:
    """Linux autostart via a systemd ``--user`` unit + ``systemctl --user``.

    The unit lives at ``~/.config/systemd/user/<unit>.service`` (honoring
    ``$XDG_CONFIG_HOME``). ``systemctl --user`` calls go through an injected ``runner``.
    """

    home: Path
    runner: Callable[[Sequence[str]], int] = field(default=None)  # type: ignore[assignment]
    kind: str = "systemd"
    # `enable --now` starts the unit now and at every login — systemd OWNS the process when
    # enabled, so the manager must not also pidfile-spawn it (see ServiceManager.enable).
    manages_process: bool = True
    # Runs a query/control command and captures its output (state, linger, verify, start/stop
    # with a stderr message). When a test injects its own `runner`, this defaults to a stub
    # that cannot answer, so no test ever asks the real systemd about a real unit.
    capture: Optional[Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]] = None

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = _default_runner
        if self.capture is None:
            self.capture = _default_capture if self.runner is _default_runner else _no_answer

    def _unit_dir(self) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else self.home / ".config"
        return root / "systemd" / "user"

    def unit_path(self, service: Service) -> Path:
        return self._unit_dir() / f"{service.systemd_unit_name}.service"

    def install(self, service: Service) -> bool:
        """Write the unit and ``systemctl --user enable --now`` it (registers + starts now).

        ``--now`` is what makes ``enable`` actually run the process under systemd's tracking
        (so its declared ``Restart=on-failure`` supervision is live and ``systemctl stop``
        acts on the real MainPID). Returns ``True`` only if the ``enable --now`` succeeded.
        """
        path = self.unit_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        service.logfile.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd_unit(service), encoding="utf-8")  # overwrite=idempotent
        unit = f"{service.systemd_unit_name}.service"
        self.runner(["systemctl", "--user", "daemon-reload"])
        rc = self.runner(["systemctl", "--user", "enable", "--now", unit])
        return rc == 0

    def uninstall(self, service: Service) -> bool:
        path = self.unit_path(service)
        unit = f"{service.systemd_unit_name}.service"
        if not path.exists():
            return False
        # `disable --now` stops the running unit AND removes the login wiring, so a unit systemd
        # actually started (e.g. after a reboot) is brought down, not orphaned. Gate the file
        # removal on its success: if it failed, systemd may still be running the unit — leave
        # the file and report failure rather than orphaning the process and lying via
        # `is_installed`.
        rc = self.runner(["systemctl", "--user", "disable", "--now", unit])
        if rc != 0:
            return False
        path.unlink()
        self.runner(["systemctl", "--user", "daemon-reload"])
        return True

    def is_installed(self, service: Service) -> bool:
        return self.unit_path(service).exists()

    def unit_name(self, service: Service) -> str:
        return f"{service.systemd_unit_name}.service"

    def unit_state(self, service: Service) -> UnitState:
        """The unit's real ``ActiveState``/``MainPID`` (``answered`` is False if systemd is unreachable)."""
        proc = self.capture(
            ["systemctl", "--user", "show", "--property=ActiveState,MainPID", self.unit_name(service)]
        )
        if proc.returncode != 0:
            return UnitState(None, None, proc.stderr.strip() or f"systemctl exited {proc.returncode}")
        props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        pid = props.get("MainPID", "")
        return UnitState(
            props.get("ActiveState", "unknown"),
            int(pid) if pid.isdigit() and int(pid) > 0 else None,
        )

    def control(self, service: Service, verb: str) -> "subprocess.CompletedProcess[str]":
        """``systemctl --user <verb> <unit>`` (start / stop / restart) with captured stderr."""
        return self.capture(["systemctl", "--user", verb, self.unit_name(service)])

    def linger(self) -> Optional[bool]:
        """Whether ``loginctl`` linger is on for this user (user manager runs without a login,
        i.e. the unit starts at boot). ``None`` when loginctl cannot answer."""
        proc = self.capture(["loginctl", "show-user", str(os.getuid()), "--property=Linger"])
        if proc.returncode != 0:
            return None
        value = proc.stdout.strip().partition("=")[2]
        return {"yes": True, "no": False}.get(value)

    def ensure_linger(self) -> Optional[bool]:
        """Turn linger on when it is off (``loginctl enable-linger``); return the resulting state."""
        state = self.linger()
        if state is False:
            self.runner(["loginctl", "enable-linger"])
            state = self.linger()
        return state

    def verify(self, service: Service) -> Optional[list[str]]:
        """``systemd-analyze --user verify`` findings about THIS unit (other units' noise is
        dropped); ``None`` when systemd-analyze is unavailable."""
        path = self.unit_path(service)
        proc = self.capture(["systemd-analyze", "--user", "verify", str(path)])
        if proc.returncode in (126, 127):
            return None
        return [line for line in (proc.stdout + "\n" + proc.stderr).splitlines() if path.name in line]


@dataclass
class NoopAutostartBackend:
    """Fallback when the OS has no supported autostart (no systemd, or an unsupported OS).

    ``enable``/``disable`` still work for the foreground/background lifecycle — there is just
    no boot/login survival. ``install`` records nothing and returns a sentinel path; the
    manager reports ``enabled=False`` so a CLI can warn the user.
    """

    home: Path
    reason: str = "no supported OS autostart backend"
    kind: str = "none"
    # No OS to run anything → the manager owns the process via the pidfile path on enable.
    manages_process: bool = False

    def unit_path(self, service: Service) -> Path:
        return service.state_dir / f"{service.name}.autostart-unsupported"

    def install(self, service: Service) -> bool:
        return False  # nothing installed; the manager will pidfile-start instead

    def uninstall(self, service: Service) -> bool:
        return False

    def is_installed(self, service: Service) -> bool:
        return False


def select_autostart_backend(
    *,
    platform: str,
    home: Path,
    has_systemctl: Optional[bool] = None,
    runner: Optional[Callable[[Sequence[str]], int]] = None,
) -> AutostartBackend:
    """Pick the right autostart backend for ``platform``.

    - macOS (``darwin``)         → :class:`LaunchdBackend`.
    - Linux **with** systemctl   → :class:`SystemdUserBackend`.
    - Linux **without** systemctl, or any other OS → :class:`NoopAutostartBackend`.

    ``has_systemctl`` is probed with ``shutil.which("systemctl")`` when not given (injectable
    so a test can force either branch without a real systemd).
    """
    if platform == MACOS:
        return LaunchdBackend(home=home, runner=runner or _default_runner)
    if platform == LINUX:
        present = has_systemctl
        if present is None:
            present = shutil.which("systemctl") is not None
        if present:
            return SystemdUserBackend(home=home, runner=runner or _default_runner)
        return NoopAutostartBackend(home=home, reason="systemd (systemctl) not found")
    return NoopAutostartBackend(home=home, reason=f"unsupported platform {platform!r}")


def _default_runner(cmd: Sequence[str]) -> int:
    """Run an autostart control command, returning its exit code. Never raises on nonzero.

    A missing binary or a nonzero exit is reported as a nonzero return rather than an
    exception, so ``enable`` does not hard-fail on a quirky host — the unit file is still
    written and the backend reports the discrepancy via its ``install`` return value.
    stdout/stderr are DISCARDED (sent to ``DEVNULL``, not inherited) so a launchctl/systemctl
    warning cannot interleave with the manager's own one-line status output and break a script
    parsing ``dispatch`` output. Exit-code mapping follows the shell convention and keeps failure
    classes distinguishable: a **missing** binary → ``127``; any other ``OSError`` (e.g.
    permission denied, sandbox-blocked exec) → ``126`` — not collapsed together, so a caller
    can tell "not installed" from "installed but couldn't run it".
    """
    try:
        completed = subprocess.run(  # noqa: S603
            list(cmd),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode
    except FileNotFoundError:
        return 127
    except OSError:
        return 126


# ---------------------------------------------------------------------------------------
# ServiceManager
# ---------------------------------------------------------------------------------------


@dataclass
class ServiceManager:
    """Lifecycle operations for one :class:`Service`: run/start/status/stop/enable/disable.

    The pidfile + start/stop/status machinery is delegated to
    ``agenttools_daemon.Supervisor`` (one shared copy, not a reimplementation). Autostart is
    delegated to an :class:`AutostartBackend` chosen by OS. All OS/process seams are
    injectable so the whole manager is testable under a tmp HOME with no real side effects.

    Parameters
    ----------
    service
        The :class:`Service` to manage.
    platform
        The platform key (defaults to :func:`current_platform`); pinned by tests.
    has_systemctl
        Whether ``systemctl`` is available (Linux autostart selection). ``None`` ⇒ probe.
    autostart
        An explicit :class:`AutostartBackend` (overrides OS selection); mainly for tests.
        NOTE: when you pass ``autostart=`` explicitly, ``runner=`` is NOT applied to it (the
        explicit backend carries its own ``runner``) — set the runner on the backend you pass.
        ``runner=`` only configures the auto-selected backend.
    spawner
        ``Callable[[argv, logfile], Child]`` that detaches the background daemon. Defaults to
        a ``subprocess.Popen`` with ``start_new_session=True`` and stdout/stderr → logfile.
        ``start_new_session`` is POSIX-only; on Windows the spawned process is not fully
        detached (pass a Windows-specific spawner if you need boot/login survival there).
    runner
        The autostart control-command runner passed to the AUTO-SELECTED backend. Ignored when
        ``autostart=`` is given explicitly (that backend owns its own runner).
    alive
        Liveness probe passed through to the Supervisor (``os.kill(pid, 0)`` by default).
    signaller
        ``Callable[[pid, sig], None]`` passed through to the Supervisor for the cross-process
        ``stop`` signal (``os.kill`` by default). Injected in tests.
    foreground_runner
        ``Callable[[argv], int]`` used by ``run`` to block on the foreground process.
        Defaults to ``subprocess.call`` (returns the exit code). Injected in tests.
    port_probe
        ``Callable[[host, port], PortProbe]`` used by :meth:`probe_port` to check whether
        something is listening on the service's port, independent of the pidfile. Defaults to
        :func:`_default_port_prober` (a real TCP connect + best-effort ``lsof`` lookup).
        Injected in tests so the suite never dials a real socket.
    sleeper
        ``Callable[[seconds], None]`` used ONLY by :func:`_run_action_disable`'s post-teardown
        settle-and-re-probe (an OS-managed ``launchctl unload``/``systemctl disable --now``
        can return before the process has actually released the port — see that function's
        docstring). Defaults to ``time.sleep``. Injected in tests so the suite waits zero
        wall-clock time while still asserting the retry happened.
    """

    service: Service
    platform: str = field(default_factory=current_platform)
    has_systemctl: Optional[bool] = None
    autostart: Optional[AutostartBackend] = None
    spawner: Optional[Callable[[Argv, Path], object]] = None
    runner: Optional[Callable[[Sequence[str]], int]] = None
    alive: Optional[Callable[[int], bool]] = None
    signaller: Optional[Callable[[int, int], None]] = None
    foreground_runner: Optional[Callable[[Argv], int]] = None
    port_probe: Optional[Callable[[str, int], PortProbe]] = None
    sleeper: Optional[Callable[[float], None]] = None

    _backend: AutostartBackend = field(init=False, repr=False)
    # Same-process record of whether the most recent enable's OS control command succeeded.
    # True by default so a plain `status` (no enable yet) treats an installed unit as active.
    _last_enable_ok: bool = field(default=True, init=False, repr=False)
    # Same-process record of whether the most recent disable's uninstall fully succeeded.
    _last_disable_ok: bool = field(default=True, init=False, repr=False)
    # Same-process record of the linger state `enable` left behind (None: unknown / no systemd).
    _linger: Optional[bool] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._backend = self.autostart or select_autostart_backend(
            platform=self.platform,
            home=self.service._home,
            has_systemctl=self.has_systemctl,
            runner=self.runner,
        )
        if self.spawner is None:
            self.spawner = _default_detached_spawner
        if self.foreground_runner is None:
            self.foreground_runner = _default_foreground_runner
        if self.port_probe is None:
            self.port_probe = _default_port_prober
        if self.sleeper is None:
            self.sleeper = time.sleep

    # --- helpers ---------------------------------------------------------------------

    @property
    def backend(self) -> AutostartBackend:
        """The selected autostart backend (for status/help)."""
        return self._backend

    def _supervisor(self) -> Supervisor:
        """A Supervisor bound to this service's argv + pidfile, with our detached spawner.

        A FRESH Supervisor is built on every call (``start``/``status``/``stop``/``enable``/
        ``disable``). This is correct precisely because the pidfile is the sole source of truth:
        the Supervisor's only durable state is the pidfile, so a new instance reads the same
        truth a previous one wrote. The manager intentionally keeps no in-memory ``Child``
        handle — a background daemon outlives the launching process, so an in-memory handle
        would be useless to a later ``stop`` in a different process anyway.
        """
        sup = Supervisor(
            list(self.service.argv),
            pidfile=str(self.service.pidfile),
            spawner=self._spawn_for_supervisor,
        )
        # Override the Supervisor's process seams only when the manager was given explicit ones
        # (tests inject fakes; production leaves them None → the Supervisor's real os.kill-based
        # defaults stand). Set post-construction rather than via a `**kwargs` dict so each
        # assignment keeps its own precise callable type (alive: (int)->bool vs signaller:
        # (int,int)->None) instead of being unified into one loose dict value type.
        if self.alive is not None:
            sup.alive = self.alive
        if self.signaller is not None:
            sup.signaller = self.signaller
        return sup

    def _spawn_for_supervisor(self, cmd: Argv):
        """Adapt our (argv, logfile) detached spawner to the Supervisor's (cmd)->Child seam."""
        self.service.logfile.parent.mkdir(parents=True, exist_ok=True)
        return self.spawner(list(cmd), self.service.logfile)  # type: ignore[misc]

    # --- public API ------------------------------------------------------------------

    def run(self) -> int:
        """Run the service in the FOREGROUND (this shell), blocking. Returns its exit code.

        For ``disable``d / ad-hoc use. Does not write a pidfile (this shell IS the service).
        """
        return int(self.foreground_runner(list(self.service.argv)))  # type: ignore[misc]

    def owned_unit_state(self) -> Optional[UnitState]:
        """The unit's real state when an installed systemd unit owns this service, else ``None``.

        ``None`` also when systemd cannot answer (no user bus): the pidfile path then applies.
        """
        backend = self._backend
        query = getattr(backend, "unit_state", None)
        if query is None or not backend.manages_process or not backend.is_installed(self.service):
            return None
        state = query(self.service)
        return state if state.answered else None

    def _control(self, verb: str) -> None:
        proc = self._backend.control(self.service, verb)  # type: ignore[attr-defined]
        if proc.returncode != 0:
            unit = self._backend.unit_name(self.service)  # type: ignore[attr-defined]
            raise ServiceControlError(
                f"systemctl --user {verb} {unit} failed: {proc.stderr.strip() or proc.returncode}"
            )

    def start(self) -> ServiceStatus:
        """Start the service in the background; return immediately.

        With an installed systemd unit the UNIT is started (one owner: a leftover pidfile
        daemon holds the port, so it is stopped first). Otherwise a detached pidfile daemon is
        spawned. Raises ``agenttools_daemon.AlreadyRunningError`` if an instance is already up,
        :class:`ServiceControlError` if systemctl refuses.
        """
        unit = self.owned_unit_state()
        if unit is not None:
            self._supervisor().stop()  # a leftover pidfile daemon holds the port the unit needs
            if unit.serving:
                raise AlreadyRunningError(unit.main_pid or 0)
            self._control("start")
            return self.status()
        sup = self._supervisor()
        sup.start()
        return self.status()

    def restart(self) -> ServiceStatus:
        """Stop then start, whichever owner runs the service (the unit: ``systemctl restart``)."""
        unit = self.owned_unit_state()
        if unit is not None:
            self._supervisor().stop()
            self._control("restart")
            return self.status()
        self.stop()
        return self.start()

    def wait_ready(self) -> Optional[str]:
        """Wait for ``service.health_check``; ``None`` when it passes (or none is set), else why not."""
        check = self.service.health_check
        if check is None:
            return None
        deadline = time.monotonic() + self.service.ready_timeout
        while True:
            try:
                if check():
                    return None
            except (OSError, ValueError):
                pass  # a refused connection / garbled reply is just "not ready yet"
            if time.monotonic() >= deadline:
                break
            self.sleeper(0.25)  # type: ignore[misc]
        probe = self.probe_port()
        if probe is not None and probe.occupied:
            pid = f" (pid {probe.pid})" if probe.pid is not None else ""
            return (
                f"port {self.service.port} is in use{pid} but the health check did not pass "
                f"— another process may hold it"
            )
        return "the server is not listening — it exited or is still starting"

    def status(self) -> ServiceStatus:
        """Report {running, pid, port, url, enabled} from the pidfile + a liveness probe.

        ``running``/``pid``/``state`` come from the pidfile + a liveness probe — i.e. they
        track the MANAGER-spawned (``start``) instance. When OS autostart owns the process
        (``enable`` on launchd/systemd), that instance has no pidfile, so ``running`` reflects
        the autostart-INSTALLED state instead. ``enabled`` is ``True`` only when the OS unit is
        installed.

        LIMITATION: for an OS-managed (enabled) service, ``running`` is inferred from "the unit
        is installed", NOT from probing the actual launchd/systemd process. If that process
        crashed in a way the OS won't auto-restart (a clean ``exit 0``, or a SIGKILL — neither
        triggers ``KeepAlive{SuccessfulExit:false}`` / ``Restart=on-failure``), ``status`` can
        report ``running=True`` while nothing is up. Probing the live OS process would need a
        ``launchctl list`` / ``systemctl is-active`` parse (a separate seam) — deliberately out
        of scope here; use the OS's own tools for authoritative OS-managed liveness.

        FALLBACK (agent-tools#685): when neither of the two signals above shows a live
        process, fall back to :meth:`probe_port`. A process genuinely serving this service's
        port under a legacy/foreign launchd or systemd label (one that predates the current
        ``com.agenttools.<tool>.<name>`` unit-naming convention) is invisible to both —
        ``LaunchdBackend.is_installed``/``SystemdUserBackend.is_installed`` only check whether
        the CURRENT label's unit file exists, never the actual set of running OS processes.
        When the probe reports the port occupied, report ``running=True``/``state="foreign"``
        with the probe's best-effort ``pid`` (``None`` when unresolvable) instead of a
        permanent false "stopped".
        """
        sup = self._supervisor()
        st: DaemonStatus = sup.status()
        enabled = self._backend.is_installed(self.service)
        unit = self.owned_unit_state()
        pid = st.pid
        if unit is not None:
            # systemd answered: its ActiveState/MainPID are the truth, not "the unit file exists".
            if st.running:  # a leftover pidfile daemon
                running, state = True, st.state
            elif unit.serving:
                running = True
                state = "autostart" if unit.active_state != "activating" else "starting"
                pid = unit.main_pid
            else:
                running = False
                state = "restarting" if unit.up else (unit.active_state or "stopped")
                if state == "inactive":
                    state = "stopped"
        else:
            # An OS-managed (enabled) service is "running" even though no pidfile names it — the
            # OS is the supervisor. Surface that so `status` after `enable` doesn't read "stopped".
            running = st.running or (enabled and self._backend.manages_process)
            state = (
                "autostart" if (enabled and self._backend.manages_process and not st.running)
                else st.state
            )
        if not running:
            probe = self.probe_port()
            if probe is not None and probe.occupied:
                running = True
                state = "foreign"
                pid = probe.pid
        return ServiceStatus(
            running=running,
            state=state,
            pid=pid,
            port=self.service.port,
            url=self.service.url,
            enabled=enabled,
        )

    def stop(self) -> bool:
        """Stop the running instance and remove the pidfile. Idempotent.

        Stops the pidfile daemon and, when an installed systemd unit owns the service, the
        unit too (autostart stays installed: it comes back at the next login/boot or
        ``start``). Returns ``True`` if something live was signalled. On a backend whose
        state cannot be queried an OS-owned process is left to ``disable``.
        """
        unit = self.owned_unit_state()
        released = self._supervisor().stop()
        if unit is not None and unit.up:
            self._control("stop")
            return True
        return released

    def probe_port(self) -> Optional[PortProbe]:
        """Check whether something is listening on this service's port right now.

        ``None`` when the service has no port (not a network server) — nothing to probe.
        This is independent of the pidfile: it answers "is the PORT free", not "is the
        MANAGER-spawned instance gone". The two differ exactly when an ad-hoc, unmanaged
        process (e.g. ``review dashboard run``) is bound to the same port — ``stop``/
        ``disable`` only ever touch the pidfile-tracked instance, so a foreign listener
        survives them; :func:`run_action` calls this afterward to catch that case instead of
        reporting an unqualified success.
        """
        if self.service.port is None:
            return None
        return self.port_probe(self.service.host, self.service.port)  # type: ignore[misc]

    def autostart_active(self) -> bool:
        """Whether OS autostart is BOTH installed AND was last set up successfully.

        ``status().enabled`` is the durable, file-on-disk truth (the unit IS installed) — it
        stays ``True`` after a ``launchctl load`` / ``systemctl enable`` that returned nonzero,
        because the file is there and may still load at next login. This method is the
        same-process, "did the control command actually succeed" signal: it ANDs the
        file-presence with the result of the most recent ``enable`` in this process
        (``True`` when no ``enable`` has run, so a plain ``status`` after a clean enable reads
        active). ``run_action`` uses it to avoid printing "enabled" when the OS command failed.
        """
        return self._backend.is_installed(self.service) and self._last_enable_ok

    @property
    def last_disable_ok(self) -> bool:
        """Whether the most recent ``disable`` fully removed autostart (same-process signal).

        ``False`` means the backend's ``uninstall`` reported failure (the OS unload/disable
        command returned nonzero) — the unit may still be loaded and the process still running.
        ``True`` when nothing was installed (no-op) or the removal succeeded.
        """
        return self._last_disable_ok

    @property
    def linger(self) -> Optional[bool]:
        """Linger state `enable` left behind: ``False`` means the unit will NOT start at boot
        (only at login). ``None``: unknown or not a systemd backend."""
        return self._linger

    def enable(self) -> ServiceStatus:
        """Install OS autostart AND start the service now. Idempotent.

        Single-owner, no double-start: on a backend that manages the process
        (launchd/systemd), installing the unit ALSO launches it now (``RunAtLoad`` /
        ``enable --now``), so the manager does NOT also pidfile-spawn it — that would put two
        processes on the same port. On the no-autostart fallback OS there is no OS to run
        anything, so the manager falls back to a background ``start`` (writing the pidfile);
        ``status().enabled`` stays ``False`` and the caller should warn it won't survive reboot.

        Order is install-then-fallback-start so a manager-owned start failure can't leave a
        half-enabled state with no running process for the backends that manage it.
        """
        if self._backend.manages_process:
            # The OS is about to launch its own copy (RunAtLoad / enable --now). Stop any
            # manager-spawned (pidfile) instance first so we don't end up with two processes
            # on the same port — the exact no-double-start invariant. No-op if none is running.
            self._supervisor().stop()
        installed_ok = self._backend.install(self.service)
        # Remember the OS control command's outcome so `autostart_active` / `run_action` can
        # distinguish "unit on disk" from "OS actually accepted it" (a failed launchctl/
        # systemctl leaves the file but does not make autostart live).
        self._last_enable_ok = installed_ok or not self._backend.manages_process
        ensure = getattr(self._backend, "ensure_linger", None)
        self._linger = ensure() if (ensure is not None and installed_ok) else None
        if not self._backend.manages_process:
            # No OS supervisor — the manager owns the process. Start if not already up.
            if not self._supervisor().status().running:
                self._supervisor().start()
        elif not installed_ok:
            # The OS backend exists but the control command failed (sandbox/missing binary);
            # the unit file may be on disk but nothing is actually running under the OS. Fall
            # back to a manager-spawned instance so `enable` still leaves the service UP.
            if not self._supervisor().status().running:
                self._supervisor().start()
        return self.status()

    def disable(self) -> ServiceStatus:
        """Remove OS autostart AND stop the running instance. Idempotent.

        Uninstalling the OS unit stops the OS-managed process (``launchctl unload`` /
        ``systemctl --user disable --now``); ``stop()`` then also brings down any
        manager-spawned (pidfile) instance — covering a service that was ``start``ed manually
        and then ``enable``d, or one started via the fallback path. Both halves are no-ops when
        absent, so ``disable`` is safe on an already-disabled / already-stopped service.

        If the backend's ``uninstall`` reports failure (the OS unload/disable command returned
        nonzero — the unit may still be loaded and running), that is recorded so ``run_action``
        reports the failure instead of a false "disabled and stopped".

        CAVEAT: the ``ServiceStatus`` this returns can transiently read
        ``running=True``/``state="foreign"`` during the same launchd/systemd drain window
        :func:`_run_action_disable`'s own settle-and-re-probe exists to mask (agent-tools#685's
        ``status()`` fallback now probes here too) — it is naming this call's OWN
        not-yet-fully-drained process, not a genuine foreign listener. ``run_action`` is
        unaffected (it discards this return value and does its own settled probe via
        :func:`_foreign_listener_fragment`); a direct API caller racing ``disable()`` should
        expect this and re-check rather than trust the immediate return.
        """
        was_installed = self._backend.is_installed(self.service)
        uninstalled = self._backend.uninstall(self.service)
        # disable "succeeded" if nothing was installed (nothing to do) or uninstall fully
        # removed it; a present-but-failed uninstall is a failure.
        self._last_disable_ok = (not was_installed) or uninstalled
        self.stop()
        return self.status()


# --- default detached / foreground spawners --------------------------------------------


def _default_detached_spawner(argv: Argv, logfile: Path):
    """Spawn ``argv`` as a detached background daemon, stdout/stderr → ``logfile``.

    ``start_new_session=True`` puts the child in its own session so it outlives the launching
    shell (the shell's SIGHUP on exit doesn't reach it). The log is opened append so restarts
    accumulate rather than truncate. Returns the ``Popen`` (a valid ``agenttools_daemon.Child``).
    """
    logfile.parent.mkdir(parents=True, exist_ok=True)
    # Open the log, hand it to the child, then close the PARENT's copy: Popen dup()s the fd
    # into the child, so the parent's handle is redundant and would otherwise leak one fd per
    # start. The child keeps its own dup'd fd open for as long as it runs.
    with open(logfile, "ab") as log:
        return subprocess.Popen(  # noqa: S603
            list(argv),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def _default_foreground_runner(argv: Argv) -> int:
    """Run ``argv`` in the foreground and block until it exits; return its exit code."""
    return subprocess.call(list(argv))  # noqa: S603


# ---------------------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------------------

# The lifecycle subcommands, in the order a CLI should present them.
SUBCOMMANDS = ("run", "start", "stop", "restart", "status", "enable", "disable", "logs")


def add_service_subcommands(
    subparsers,
    *,
    manager_factory: Callable[[], ServiceManager],
    service_name: str,
    help_text: Optional[Mapping[str, str]] = None,
) -> None:
    """Wire ``run|start|stop|restart|status|enable|disable|logs`` for one service into an argparse tree.

    ``subparsers`` is the result of ``parser.add_subparsers(...)`` for the SERVICE level
    (e.g. ``review dashboard <here>``). Each lifecycle subcommand gets a parser whose
    ``func`` is set to a thin handler that builds the manager (via ``manager_factory``) and
    calls the matching method, printing a one-line result. ``manager_factory`` is a no-arg
    callable so the manager is constructed lazily (only when a subcommand actually runs),
    keeping ``--help`` cheap.

    Wire the SERVICE-level parser so a bare invocation (no subcommand) prints HELP, never
    launches — see :func:`dispatch`.

    ``help_text`` optionally OVERRIDES the per-subcommand help with a mapping of
    ``{subcommand: help string}`` (e.g. ``{"start": "boot the dashboard"}``). Keys not present
    fall back to the built-in :data:`_SUBCOMMAND_HELP`, so you can override just one line.
    """
    for name in SUBCOMMANDS:
        override = help_text.get(name) if help_text else None
        sub = subparsers.add_parser(
            name,
            help=override or _SUBCOMMAND_HELP[name].format(service=service_name),
        )
        sub.set_defaults(_service_action=name, _service_manager_factory=manager_factory)
        if name == "logs":
            sub.add_argument("-n", "--lines", type=int, default=50, help="last N lines (default 50)")
            sub.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")


def dispatch(args, *, on_no_subcommand: Callable[[], int]) -> int:
    """Run the lifecycle action chosen on ``args`` (set by :func:`add_service_subcommands`).

    If no subcommand was chosen (bare ``<tool> <service>``), call ``on_no_subcommand`` (which
    should print HELP and return an exit code) — NEVER launch. Otherwise build the manager and
    invoke the matching method, printing a human-readable result, and return an exit code.
    """
    action = getattr(args, "_service_action", None)
    if action is None:
        return int(on_no_subcommand())
    factory = getattr(args, "_service_manager_factory")
    manager = factory()
    if action == "logs":
        return run_logs(manager, lines=getattr(args, "lines", 50), follow=getattr(args, "follow", False))
    return run_action(manager, action)


def run_action(manager: ServiceManager, action: str) -> int:
    """Invoke ``action`` on ``manager``, print a one-line human result, return an exit code.

    Exit codes: ``0`` on success; ``3`` when ``status``/``stop`` find nothing running (so a
    script can branch on "was it up?"); ``4`` when ``enable``/``disable`` could not set up /
    tear down OS autostart (the launchctl/systemctl command failed), when systemctl refused
    ``start``/``stop``/``restart``, when a ``start``/``restart``/``enable`` of a service with
    a ``health_check`` never became healthy, OR when ``stop``/``disable`` acted on (or found
    nothing of) the MANAGER-spawned instance but something else is still listening on the
    service's port afterward — an unmanaged/foreign process (e.g. an ad-hoc ``run``) this
    manager does not own and did not stop; see :meth:`ServiceManager.probe_port`. NOTE:
    ``run`` returns the RAW exit code of the foreground child, which may itself be ``3``/``4``
    — the 3/4 contract above applies only to the manager-driven actions, not to ``run``'s
    passthrough. Success lines go to stdout; failure lines go to stderr (Unix convention).
    Kept tiny + side-effecting so a consumer can call it directly or let :func:`dispatch`
    call it.
    """
    try:
        return _run_action(manager, action)
    except ServiceControlError as exc:
        _eprint(f"{manager.service.name}: {exc}")
        return 4


def _run_action(manager: ServiceManager, action: str) -> int:
    """The action table behind :func:`run_action` (which maps a refused systemctl to exit 4)."""
    svc = manager.service
    if action == "run":
        try:
            return manager.run()
        except FileNotFoundError:
            # The foreground binary (argv[0]) doesn't exist. `start` already gives a clean
            # message for its common failure (already-running); do the same here instead of
            # letting a raw traceback escape through dispatch. 127 = "command not found"
            # (the shell convention), so a script can branch on it.
            _eprint(f"{svc.name}: command not found: {svc.argv[0]!r}")
            return 127
    if action in ("start", "restart"):
        if action == "restart":
            st = manager.restart()
        else:
            try:
                st = manager.start()
            except AlreadyRunningError as exc:
                # Clean CLI message instead of a raw traceback for the common "already up" case.
                _eprint(f"{svc.name}: already running (pid {exc.pid})")
                return 3
        not_ready = manager.wait_ready()
        if not_ready is not None:
            return _report_not_ready(manager, not_ready)
        verb = "restarted" if action == "restart" else "started"
        _print(f"{svc.name}: {verb}{_pid_suffix(st)}{_url_suffix(st)}")
        if action == "start":
            _hint_enable(manager)
        return 0
    if action == "logs":
        return run_logs(manager)
    if action == "status":
        st = manager.status()
        if st.running:
            # agent-tools#685: a "foreign" state means something IS occupying the port, but
            # it is not this manager's own pidfile/OS-managed instance (a legacy/foreign
            # autostart label, or an unrelated process) — say so, rather than printing an
            # indistinguishable "running" that reads as this service being healthy.
            qualifier = (
                " [foreign listener]" if st.state == "foreign"
                else " [autostart on]" if st.enabled
                else ""
            )
            if st.state == "starting":
                qualifier += " [starting]"
            _print(f"{svc.name}: running{_pid_suffix(st)}{_url_suffix(st)}{qualifier}")
            return 0
        _print(f"{svc.name}: {st.state}")
        return 3
    if action == "stop":
        return _run_action_stop(manager)
    if action == "enable":
        st = manager.enable()
        where = manager.backend.kind
        # `autostart_active` is true only when the unit is installed AND the OS control command
        # accepted it — distinct from `st.enabled` (file on disk), which stays true even after a
        # failed launchctl/systemctl. Don't claim "enabled" when the OS rejected it.
        if manager.autostart_active():
            not_ready = manager.wait_ready()
            if not_ready is not None:
                return _report_not_ready(manager, not_ready)
            _print(f"{svc.name}: autostart enabled ({where}), "
                   f"running{_pid_suffix(st)}{_url_suffix(st)}")
            if manager.linger is False:
                _eprint(
                    f"{svc.name}: user linger is off and could not be enabled — the service "
                    f"starts at login, NOT at boot; run 'loginctl enable-linger $USER'"
                )
            return 0
        if st.enabled:
            # file written but the OS command failed; we fell back to a manager-spawned start.
            _eprint(
                f"{svc.name}: started{_pid_suffix(st)}, but enabling autostart ({where}) "
                f"FAILED — it may not survive reboot; check the {where} unit"
            )
            return 4
        # not a failure — the OS just has no autostart; warn on stderr so stdout stays clean.
        _eprint(
            f"{svc.name}: started{_pid_suffix(st)}, but autostart is unavailable "
            f"on this OS ({where}) — it will NOT survive reboot"
        )
        return 0
    if action == "disable":
        return _run_action_disable(manager)
    raise ValueError(f"unknown service action: {action!r}")


def _run_action_stop(manager: ServiceManager) -> int:
    """``stop`` handler for :func:`run_action`.

    Stops the MANAGER-spawned instance, then checks the port is actually free. Reporting
    success (or a bare "was not running") while a foreign, unmanaged process is still bound
    to the port is the exact false-confidence bug this guards against (review-cli#377).

    NOT probed on the OS-autostart-enabled/no-pidfile branch below: there, an occupied port
    is the EXPECTED, healthy state (the OS-owned process itself), and `stop` has no way to
    tell that apart from a genuine foreign listener without identifying the OS process's own
    pid (out of scope here) — probing there produced a real regression in an earlier
    revision (a healthy `enable`d service made `stop` cry wolf; review-cli#377 review
    round 2). `disable` is the action that actually removes the OS-managed process, so ITS
    post-action probe (see :func:`_run_action_disable`) is the one that means something.
    """
    svc = manager.service
    stopped = manager.stop()
    if stopped:
        foreign = _foreign_listener_fragment(manager)
        if foreign:
            _eprint(f"{svc.name}: stopped the managed instance, but {foreign}")
            return 4
        _print(f"{svc.name}: stopped{_autostart_stays(manager)}")
        return 0
    # No pidfile-tracked instance. But if OS autostart owns the process (enabled on a
    # launchd/systemd backend), a bare "was not running" is misleading — the service is
    # very likely alive under the OS, and `stop` does not touch the OS-managed process
    # (that's `disable`'s job). Warn on stderr so a script reading stdout/exit-3 isn't
    # fooled into thinking nothing is up. Deliberately does NOT probe the port here (see the
    # docstring above) — an occupied port in this exact state is expected, not a finding.
    if manager.backend.manages_process and manager.status().enabled and manager.owned_unit_state() is None:
        _eprint(
            f"{svc.name}: no background instance to stop, but OS autostart "
            f"({manager.backend.kind}) is enabled — run 'disable' to stop the "
            f"OS-managed process"
        )
        return 3
    foreign = _foreign_listener_fragment(manager)
    if foreign:
        # THE incident this guards against: an ad-hoc `run` process (no pidfile) is bound to
        # the port. `stop` has nothing of its own to stop, but claiming "was not running"
        # would be a false all-clear.
        _eprint(f"{svc.name}: no managed instance was running, but {foreign}")
        return 4
    _print(f"{svc.name}: was not running")
    return 3


def _run_action_disable(manager: ServiceManager) -> int:
    """``disable`` handler for :func:`run_action`.

    Removes autostart + stops the managed instance, then checks the port is actually free
    (see :func:`_run_action_stop` for why that check matters — unlike `stop`, an occupied
    port HERE is always meaningful: `disable` is the action that actually tears down the
    OS-managed process, so nothing of this service's own should be listening once it
    returns).

    SETTLE WINDOW: on the launchd backend, `uninstall` calls `launchctl unload`, which is not
    guaranteed to have fully reaped the process (and released the port) by the time it
    returns (`systemctl --user disable --now` is closer to synchronous). A daemon that takes
    a moment to drain its listener would otherwise make this probe transiently read
    "occupied" for an otherwise-successful disable — flapping a real success into a false
    failure, AND misattributing the manager's own dying process as a foreign listener. So an
    occupied first probe gets ONE settle-and-re-probe (:data:`_DISABLE_SETTLE_SECONDS` via
    `manager.sleeper`, injectable in tests) before being reported; still occupied after that
    is treated as a genuine finding.
    """
    svc = manager.service
    manager.disable()
    if not manager.last_disable_ok:
        _eprint(
            f"{svc.name}: FAILED to remove autostart ({manager.backend.kind}) — "
            f"the unit may still be loaded; check it manually"
        )
        return 4
    foreign = _foreign_listener_fragment(manager)
    if foreign:
        # Give a slow-draining OS teardown one chance to actually finish before believing it.
        manager.sleeper(_DISABLE_SETTLE_SECONDS)  # type: ignore[misc]
        foreign = _foreign_listener_fragment(manager)
    if foreign:
        # Don't claim "the managed instance stopped" here — `disable` may have had nothing
        # of its own to stop (was_installed=False, or the pidfile path was already empty);
        # the only claim this branch is entitled to make is "autostart is removed".
        _eprint(f"{svc.name}: autostart disabled, but {foreign}")
        return 4
    _print(f"{svc.name}: autostart disabled and stopped")
    return 0


# Grace window `_run_action_disable` waits, once, before believing an occupied port really
# means a foreign listener rather than its own OS-managed process still draining after
# `launchctl unload` returns. Short enough to keep `disable` responsive; long enough to cover
# a normal process-exit drain (an unusually slow shutdown still gets the correct, if delayed,
# verdict on the next manual `disable`/`status`).
_DISABLE_SETTLE_SECONDS = 0.5


def _foreign_listener_fragment(manager: ServiceManager) -> Optional[str]:
    """A warning fragment naming the port (+ pid if resolvable) when something is still
    listening on the service's port after ``stop``/``disable`` acted.

    Returns ``None`` when the port is free or the service has no port (nothing to warn
    about). Deliberately does NOT claim the listener "was not started by this manager": the
    common case IS an ad-hoc/unmanaged process this manager never tracked, but two edge cases
    make a flat "not ours" claim occasionally false — a forked child that inherited the
    manager's own listening socket and outlived the parent's stop signal, or a stubborn
    process that survived ``agenttools_daemon.Supervisor``'s SIGTERM/SIGKILL escalation
    (``Supervisor.stop`` reports success once the *original* pid is confirmed dead, which does
    not guarantee an inherited fd was closed). Either way the fact worth reporting is the same
    — the port is occupied right now, regardless of `stop`/`disable`'s own success — so the
    wording stays neutral about ownership rather than asserting a cause it cannot verify.
    """
    probe = manager.probe_port()
    if probe is None or not probe.occupied:
        return None
    svc = manager.service
    # Prefer the address the real probe actually connected on; fall back to a display guess
    # only for a caller-supplied fake PortProbe that doesn't track it (tests).
    addr = probe.addr or _loopback_for(svc.host)
    # Bracket an IPv6 literal (`[::1]:7878`) so the address:port pair parses unambiguously —
    # `::1:7878` is unparseable/misleading (looks like part of the address).
    probed_addr = f"[{addr}]:{svc.port}" if ":" in addr else f"{addr}:{svc.port}"
    pid_part = f" (pid {probe.pid})" if probe.pid is not None else " (pid unknown)"
    # Match the docstring's own "stays neutral about ownership" promise: don't assert WHO the
    # listener is (a prior wording said "likely a process this manager never tracked, e.g. an
    # ad-hoc run" — which is exactly the claim the docstring above disavows, since a forked
    # child or a stop-signal survivor can still be this manager's own). State only the fact
    # the probe actually established: it wasn't stopped by this command.
    return (
        f"something is still listening on {probed_addr}{pid_part} — it was not stopped "
        f"by this command"
    )


def _autostart_stays(manager: ServiceManager) -> str:
    """Suffix for ``stop``: an enabled unit stays installed and returns at login/boot."""
    if manager.backend.manages_process and manager.backend.is_installed(manager.service):
        return f" (autostart stays enabled; '{_cli_name(manager.service)} disable' removes it)"
    return ""


def _cli_name(svc: Service) -> str:
    return svc.cli or (f"{svc.tool_name} {svc.name}" if svc.tool else svc.name)


def _report_not_ready(manager: ServiceManager, why: str) -> int:
    svc = manager.service
    _eprint(
        f"{svc.name}: did not come up within {svc.ready_timeout:g}s: {why}\n"
        f"  log: {svc.logfile}"
    )
    return 4


def run_logs(manager: ServiceManager, *, lines: int = 50, follow: bool = False) -> int:
    """Print the last ``lines`` of the service logfile; ``follow`` keeps printing new lines.

    Exit ``3`` when there is no logfile yet. Reads the file the unit/daemon appends to, so
    it works for both owners.
    """
    svc = manager.service
    log = svc.logfile
    if not log.exists():
        _eprint(f"{svc.name}: no log yet at {log}")
        return 3
    with log.open("r", encoding="utf-8", errors="replace") as handle:
        for line in deque(handle, maxlen=max(lines, 0)):
            sys.stdout.write(line)
        sys.stdout.flush()
        while follow:
            line = handle.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.25)
    return 0


@dataclass(frozen=True)
class ServiceCheck:
    """One doctor finding about the service's lifecycle wiring (``level``: ok / warn / fail)."""

    name: str
    level: str
    detail: str
    fix: str = ""


def service_checks(manager: ServiceManager) -> list[ServiceCheck]:
    """Generic lifecycle checks a consumer's ``doctor`` can append to its own.

    The command to run exists; autostart is installed; the unit file passes
    ``systemd-analyze --user verify``; the unit's real state (a ``failed`` unit is a fail);
    linger is on so the unit starts at boot, not only after a login.
    """
    svc = manager.service
    backend = manager.backend
    cli = _cli_name(svc)
    checks: list[ServiceCheck] = []
    exe = svc.argv[0]
    if shutil.which(exe) or (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        checks.append(ServiceCheck("service command", "ok", exe))
    else:
        checks.append(ServiceCheck("service command", "fail", f"{exe} is not an executable", f"fix the {svc.name} install"))
    if backend.kind == "none":
        checks.append(ServiceCheck("autostart", "warn", "unavailable on this OS: the service will not survive a reboot", f"{cli} start"))
        return checks
    if not backend.is_installed(svc):
        checks.append(ServiceCheck("autostart", "warn", "not enabled", f"{cli} enable"))
        return checks
    checks.append(ServiceCheck("autostart", "ok", f"{backend.kind} unit {backend.unit_path(svc)}"))
    verify = getattr(backend, "verify", None)
    if verify is not None:
        problems = verify(svc)
        if problems:
            checks.append(ServiceCheck("unit file", "fail", "; ".join(problems), f"{cli} enable  (rewrites the unit)"))
        elif problems is not None:
            checks.append(ServiceCheck("unit file", "ok", "systemd-analyze verify: clean"))
    state = manager.owned_unit_state()
    if state is not None:
        level = "fail" if state.active_state == "failed" else "ok"
        checks.append(ServiceCheck("unit state", level, state.active_state or "unknown",
                                   f"{cli} logs; {cli} restart" if level == "fail" else ""))
    linger = getattr(backend, "linger", None)
    if linger is not None:
        enabled = linger()
        if enabled is False:
            checks.append(ServiceCheck("linger", "warn", "off: the unit starts at login, not at boot", "loginctl enable-linger $USER"))
        elif enabled:
            checks.append(ServiceCheck("linger", "ok", "on: the unit starts at boot"))
    return checks


def _url_suffix(st: ServiceStatus) -> str:
    return f" — {st.url}" if st.url else ""


def _pid_suffix(st: ServiceStatus) -> str:
    """`` (pid N)`` when a pidfile-tracked pid exists; empty when the OS owns the process."""
    return f" (pid {st.pid})" if st.pid is not None else ""


def _hint_enable(manager: ServiceManager) -> None:
    """Print the 'run … enable to start at login' hint after a foreground/background start.

    The command is built from the service's own ``tool``/``name`` (e.g. ``review dashboard
    enable``) — a best-effort guess at the consumer's CLI shape, which is right whenever the
    tool's argv mirrors ``<tool> <service>`` (the convention this lib targets).
    """
    svc = manager.service
    if not manager.backend.is_installed(svc) and manager.backend.kind != "none":
        cmd = _cli_name(svc)
        _print(f"  hint: run '{cmd} enable' to start it at login")


def _print(msg: str) -> None:
    print(msg)


def _eprint(msg: str) -> None:
    """Print an error/warning line to stderr (Unix convention), keeping stdout for results."""
    print(msg, file=sys.stderr)


_SUBCOMMAND_HELP = {
    "run": "run {service} in the foreground (this shell), blocking",
    "start": "start {service} in the background (the autostart unit when enabled, else a detached daemon)",
    "stop": "stop {service} (autostart, if enabled, stays installed)",
    "restart": "stop then start {service}",
    "status": "show whether {service} is running (pid/port/url)",
    "enable": "install OS autostart for {service} AND start it now",
    "disable": "remove OS autostart for {service} AND stop it",
    "logs": "print the last lines of the {service} log (-n N, -f to follow)",
}


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
