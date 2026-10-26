"""Restore enrolled Guardian chats after a dedicated browser (re)start.

Run as ``python -m agenttools_chatgpt.startup --workspace home|glide.vc [--apply]``.
Only missing enrolled canonical chats are opened, as new tabs, through the
browser's loopback DevTools HTTP endpoint. Existing pages are never navigated,
reloaded, activated or closed, and no ChatGPT message or control is touched.
Default is a dry run; ``--apply`` authorizes tab creation and nothing else.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

from .lock import AccountBusy, account_lock
from .state import state_path
from .urls import canonical_chat

PORTS = {"home": 9224, "glide.vc": 9223}
MAX_PAGES = 6
DEADLINE_SECONDS = 30.0
LOCK_WAIT_SECONDS = 60.0
HTTP_TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 1 << 20
SETTLE_SECONDS = 5.0
SETTLE_INTERVAL_SECONDS = 1.0
PROFILE_SUBPATH = ".local/share/agent-tools/chatgpt-guardian/profiles"


class StartupError(Exception):
    """Fail-closed refusal; nothing further is attempted."""


class NotReady(StartupError):
    """The owned browser has not started listening yet; safe to wait and re-check."""


class UncertainCreation(StartupError):
    """A tab creation may or may not have happened; never retried in this run."""


@dataclass(frozen=True)
class Browser:
    pid: int
    args: tuple[str, ...]


@dataclass(frozen=True)
class Listener:
    address: str
    pid: int | None
    # Owning uid from /proc/net/tcp; the only ownership evidence when the pid is unreadable.
    uid: int | None = None


class Processes(Protocol):
    def browsers(self) -> list[Browser]: ...
    def listeners(self, port: int) -> list[Listener]: ...


class DevTools(Protocol):
    def pages(self, timeout: float) -> list[dict]: ...
    def create(self, url: str, timeout: float) -> None: ...


def profile_path(workspace: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / PROFILE_SUBPATH / workspace


def enrolled_chats(path: Path, workspace: str) -> list[str]:
    """Watched canonical chats (controls ``watch:<chat>`` = '1') of an existing,
    account-validated database. The consent table is Sol opt-in, not enrollment."""
    if not path.is_file():
        raise StartupError("Guardian state database does not exist")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT account FROM account_scope").fetchall() != [
                (workspace,)
            ]:
                raise StartupError("State belongs to a different account")
            rows = db.execute(
                "SELECT substr(key, 7) FROM controls"
                " WHERE substr(key, 1, 6) = 'watch:' AND value = '1'"
            ).fetchall()
    except sqlite3.Error as exc:
        raise StartupError("Guardian state is unreadable: " + type(exc).__name__)
    chats = set()
    for (chat,) in rows:
        try:
            canonical = canonical_chat(chat)
        except ValueError:
            raise StartupError("Enrolled chat is not an exact canonical URL")
        if canonical != chat:
            raise StartupError("Enrolled chat is not an exact canonical URL")
        chats.add(canonical)
    return sorted(chats)


def verify_browser(
    workspace: str,
    processes: Processes,
    home: Path | None = None,
    port: int | None = None,
) -> int:
    """Return the owned browser pid, or refuse unless exactly it owns the port."""
    port = PORTS[workspace] if port is None else port
    profile = "--user-data-dir=" + str(profile_path(workspace, home))
    port_arg = "--remote-debugging-port=" + str(port)
    related = [
        b
        for b in processes.browsers()
        if not any(a.startswith("--type=") for a in b.args)
        and (profile in b.args or port_arg in b.args)
    ]
    if len(related) != 1:
        if not related:
            raise NotReady("Owned browser is not ready: no matching process")
        raise StartupError(
            "Expected exactly one owned browser process, found " + str(len(related))
        )
    browser = related[0]
    if profile not in browser.args or port_arg not in browser.args:
        raise StartupError("Browser process does not match the owned profile and port")
    listeners = processes.listeners(port)
    if not listeners:
        raise NotReady("Owned browser is not ready: debug port not listening")
    for listener in listeners:
        if listener.address not in ("127.0.0.1", "::1"):
            raise StartupError("Debug port listener is not the owned loopback browser")
        if listener.pid is None:
            # A live Chrome denies /proc/PID/fd to its own user, so the socket's owner pid
            # is unreadable. The sole remaining listener must then at least belong to this
            # user; the exact profile+port process match above is the other half.
            if listener.uid != os.getuid():
                raise StartupError(
                    "Debug port listener is not the owned loopback browser"
                )
        elif listener.pid != browser.pid:
            raise StartupError("Debug port listener is not the owned loopback browser")
    return browser.pid


def page_chat(page: dict) -> str | None:
    """Canonical chat for a page URL (project URLs normalized) or None."""
    url = page.get("url")
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    bare = parts._replace(query="", fragment="").geturl()
    try:
        return canonical_chat(bare)
    except ValueError:
        return None


def snapshot(pages: list[dict]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (str(p.get("id")), str(p.get("type")), str(p.get("url")))
            for p in pages
            if isinstance(p, dict)
        )
    )


def inspect_inventory(pages: list[dict], enrolled: list[str]) -> tuple[int, set[str]]:
    """Return (page count, present enrolled chats); duplicates fail closed."""
    wanted = set(enrolled)
    seen: dict[str, int] = {}
    count = 0
    for page in pages:
        if not isinstance(page, dict):
            raise StartupError("Unexpected DevTools page inventory")
        if page.get("type") != "page":
            continue
        count += 1
        chat = page_chat(page)
        if chat in wanted:
            seen[chat] = seen.get(chat, 0) + 1
    if any(n > 1 for n in seen.values()):
        raise StartupError("Ambiguous duplicate tabs for an enrolled chat")
    return count, set(seen)


class ChromeDevTools:
    """Loopback-only DevTools HTTP: no redirects, no proxies, bounded bodies."""

    def __init__(self, port: int):
        self.port = port

    def _request(self, method: str, target: str, timeout: float) -> bytes:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, target, headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            if response.status != 200:
                raise StartupError("DevTools answered HTTP " + str(response.status))
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise StartupError("DevTools response exceeds the size limit")
            return body
        finally:
            connection.close()

    def pages(self, timeout: float) -> list[dict]:
        try:
            data = json.loads(self._request("GET", "/json/list", timeout))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise StartupError("Cannot read DevTools inventory: " + type(exc).__name__)
        if not isinstance(data, list):
            raise StartupError("Unexpected DevTools page inventory")
        return data

    def create(self, url: str, timeout: float) -> None:
        try:
            self._request("PUT", "/json/new?" + quote(url, safe=""), timeout)
        except StartupError as exc:
            raise UncertainCreation(str(exc))
        except (OSError, http.client.HTTPException) as exc:
            raise UncertainCreation(
                "Tab creation outcome unknown: " + type(exc).__name__
            )


CHROME_EXECUTABLES = frozenset(
    {"chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"}
)


def _split_cmdline(raw: bytes) -> tuple[str, ...]:
    """Chrome rewrites its argv into ONE item holding the whole command; split only that."""
    items = [a for a in raw.decode(errors="replace").split("\0") if a]
    if len(items) == 1 and " " in items[0]:
        try:
            return tuple(shlex.split(items[0]))
        except ValueError:
            return ()
    return tuple(items)


class ProcFilesystem:
    """Process and listener facts read from /proc for the current user."""

    def __init__(self, root: Path = Path("/proc")):
        self.root = root

    def browsers(self) -> list[Browser]:
        found = []
        uid = os.getuid()
        for entry in self.root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            args = _split_cmdline(raw)
            if not args or not self._is_chrome(entry, args[0]):
                continue
            found.append(Browser(int(entry.name), args))
        return found

    @staticmethod
    def _is_chrome(entry: Path, argv0: str) -> bool:
        """The executable must be a chrome/chromium binary, not merely look like one.

        /proc/PID/exe is authoritative. A live Chrome often denies reading it to its own
        user (EACCES); only then fall back to /proc/PID/comm plus argv[0], and any other
        failure is a refusal.
        """
        try:
            return os.path.basename(os.readlink(entry / "exe")) in CHROME_EXECUTABLES
        except PermissionError:
            pass
        except OSError:
            return False
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            return False
        return (
            comm in CHROME_EXECUTABLES and os.path.basename(argv0) in CHROME_EXECUTABLES
        )

    def listeners(self, port: int) -> list[Listener]:
        inodes: dict[str, tuple[str, int]] = {}
        for table, width in (("tcp", 8), ("tcp6", 32)):
            try:
                lines = (self.root / "net" / table).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] != "0A":
                    continue
                host, _, hex_port = fields[1].partition(":")
                if int(hex_port, 16) != port:
                    continue
                inodes[fields[9]] = (_address(host, width), int(fields[7]))
        owners: dict[str, int] = {}
        if inodes:
            for entry in self.root.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    for fd in (entry / "fd").iterdir():
                        match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(fd))
                        if match and match.group(1) in inodes:
                            owners[match.group(1)] = int(entry.name)
                except OSError:
                    continue
        return [
            Listener(addr, owners.get(inode), uid)
            for inode, (addr, uid) in inodes.items()
        ]


def _address(host: str, width: int) -> str:
    if width == 8:
        return ".".join(str(int(host[i : i + 2], 16)) for i in (6, 4, 2, 0))
    return "::1" if host == "00000000000000000000000001000000" else host


def restore(
    workspace: str,
    *,
    apply: bool,
    state: Path,
    processes: Processes,
    devtools: DevTools,
    home: Path | None = None,
    port: int | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    deadline: float = DEADLINE_SECONDS,
    settle: float = SETTLE_SECONDS,
    interval: float = SETTLE_INTERVAL_SECONDS,
    result: dict | None = None,
    skip: frozenset[str] = frozenset(),
) -> dict:
    """Fill ``result`` as it goes so a failure still reports what was opened."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    started = clock()
    result = {} if result is None else result
    result.update(workspace=workspace, apply=apply, opened=[], missing=[], deferred=[])

    def budget() -> float:
        remaining = deadline - (clock() - started)
        if remaining <= 0:
            raise StartupError("Startup deadline exceeded")
        return min(HTTP_TIMEOUT_SECONDS, remaining)

    enrolled = enrolled_chats(state, workspace)
    result["enrolled"] = len(enrolled)
    while True:  # ExecStartPost runs right after fork, before the port is open
        try:
            verify_browser(workspace, processes, home, port)
            pages = devtools.pages(budget())
            break
        except NotReady as exc:
            budget()
            if clock() - started + interval >= deadline:
                raise StartupError(str(exc)) from exc
            sleep(interval)
    count, present = inspect_inventory(pages, enrolled)
    if set(enrolled) - present:
        # restore-last-session may still be reopening tabs; wait for a stable inventory.
        settle_end = clock() + settle
        previous = snapshot(pages)
        while True:
            if clock() >= settle_end:
                raise StartupError("Browser page inventory did not settle")
            sleep(min(interval, max(0.0, settle_end - clock())))
            pages = devtools.pages(budget())
            current = snapshot(pages)
            if current == previous:
                break
            previous = current
    for chat in enrolled:
        pages = devtools.pages(budget())
        count, present = inspect_inventory(pages, enrolled)
        if chat in present:
            continue
        if chat in skip:
            # The caller's durable ledger already spent this chat's one attempt.
            result["deferred"].append(chat.rsplit("/", 1)[1])
            continue
        if count >= MAX_PAGES:
            raise StartupError("Page ceiling reached; refusing to open more tabs")
        label = chat.rsplit("/", 1)[1]
        result["missing"].append(label)
        if apply:
            try:
                devtools.create(chat, budget())
            except UncertainCreation:
                result["uncertain"] = label
                raise
            result["opened"].append(label)
    present_after = len(enrolled) - len(result["missing"]) - len(result["deferred"])
    result["already_open"] = present_after
    return result


def _cdp_port(text: str) -> int:
    """argparse type: a non-privileged TCP port (``choices=range(...)`` would print all 64k values on error)."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range 1024-65535: {port}")
    return port


def main(
    argv: list[str] | None = None,
    *,
    processes: Processes | None = None,
    devtools: DevTools | None = None,
    state: Path | None = None,
    home: Path | None = None,
    lock_wait: float = LOCK_WAIT_SECONDS,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agenttools_chatgpt.startup",
        description="Open missing enrolled Guardian chats in the dedicated browser. "
        "Dry run by default; --apply only authorizes opening tabs.",
    )
    parser.add_argument("--workspace", required=True, choices=sorted(PORTS))
    parser.add_argument(
        "--apply", action="store_true", help="Create missing tabs (default: dry run)"
    )
    parser.add_argument(
        "--cdp-port",
        type=_cdp_port,
        metavar="PORT",
        help="Loopback DevTools port of the owned browser (default: the workspace's "
        "usual port; must equal the browser's --remote-debugging-port)",
    )
    args = parser.parse_args(argv)
    port = args.cdp_port or PORTS[args.workspace]
    result: dict = {}
    try:
        target = state or state_path(args.workspace)
        # An applied run opens tabs: never concurrently with a coverage --apply run.
        with account_lock(target, wait=lock_wait) if args.apply else nullcontext():
            restore(
                args.workspace,
                apply=args.apply,
                state=target,
                processes=processes or ProcFilesystem(),
                devtools=devtools or ChromeDevTools(port),
                home=home,
                port=port,
                result=result,
            )
        code = 0
    except UncertainCreation as exc:
        result.update(error=str(exc), rerun_checks_inventory_first=True)
        code = 1
    except (StartupError, ValueError, AccountBusy) as exc:
        result["error"] = str(exc)
        code = 1
    result.setdefault("workspace", args.workspace)
    result["ok"] = code == 0
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
