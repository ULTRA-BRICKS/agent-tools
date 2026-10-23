"""Independent passive observer. No recovery controls or browser mutations.

Only existing local browser pages are read. Driver teardown disconnects this client;
no browser/context close, tab activation, navigation, model probing or dispatch exists.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import subprocess
import time
from pathlib import Path

from . import scripts
from .monitor import MonitorStore, clean_detail, project_snapshot, read_source

DOM_SCRIPT = scripts.load("monitor_dom.js")
UNITS = {
    "home": "chatgpt-guardian-home.service",
    "glide.vc": "chatgpt-guardian-glide.service",
}


def canonical_url(url: str) -> str | None:
    match = re.fullmatch(
        r"https://chatgpt\.com/(?:g/g-p-[a-f0-9]{32}(?:-[A-Za-z0-9-]+)?/)?c/([A-Za-z0-9_-]+)/?",
        url,
    )
    return "https://chatgpt.com/c/" + match[1] if match else None


def error_detail(exc: BaseException, stage: str) -> str:
    """Sanitized reason for an observation failure; stage is 'connect' or 'page'."""
    if isinstance(exc, TimeoutError):
        return "cdp-connect-timeout" if stage == "connect" else "page-evaluate-timeout"
    text = clean_detail(str(exc)) or type(exc).__name__
    return "cdp-unreachable: " + text if stage == "connect" else text


async def collect_samples(pages, previous, cache, now, *, force_chats=(), max_pages=6):
    """Sample due pages only. Cached records retain their real check timestamps."""
    from playwright.async_api import Error as BrowserError

    prior = {s["chat"]: s for s in (previous or {}).get("sessions", [])}
    rows = []
    seen = set()
    for page in pages:
        chat = canonical_url(page.url)
        if not chat or page.is_closed():
            continue
        seen.add(chat)
        old = prior.get(chat, {})
        if len(pages) > max_pages:
            rows.append(
                {
                    "chat": chat,
                    "error": "page-ceiling",
                    "error_detail": f"more than {max_pages} pages open; DOM observation paused",
                    "title": old.get("title", ""),
                }
            )
        elif (
            chat in cache
            and now < (old.get("next_check_at") or 0)
            and chat not in force_chats
        ):
            rows.append(dict(cache[chat]))
        else:
            try:
                result = await asyncio.wait_for(page.evaluate(DOM_SCRIPT), timeout=5)
                if not isinstance(result, dict):
                    raise TypeError("DOM metadata was not an object")
                row = dict(result, chat=chat)
                if row.get("error"):
                    cache.pop(chat, None)
                else:
                    row["checked_at"] = now
                    cache[chat] = row
                rows.append(row)
            except (
                BrowserError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                TimeoutError,
            ) as exc:
                cache.pop(chat, None)
                rows.append(
                    {
                        "chat": chat,
                        "error": type(exc).__name__,
                        "error_detail": error_detail(exc, "page"),
                        "title": old.get("title", ""),
                    }
                )
    for chat in set(cache) - seen:
        del cache[chat]
    return rows


def parse_health(properties: str, journal: str, now: float) -> dict:
    values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
    service = {
        "active": values.get("ActiveState", "unknown"),
        "sub": values.get("SubState", "unknown"),
        "pid": values.get("MainPID", ""),
    }
    heartbeat = None
    states = {}
    for line in journal.splitlines():
        try:
            entry = json.loads(line)
            message = json.loads(entry["MESSAGE"])
            at = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
            if not isinstance(message, dict):
                continue
            if "health" in message and (heartbeat is None or at > heartbeat):
                heartbeat = at
            if message.get("chat") and at >= states.get(message["chat"], {}).get(
                "at", 0
            ):
                states[message["chat"]] = {"state": message.get("state", ""), "at": at}
        except (ValueError, TypeError, KeyError):
            continue
    return {
        "heartbeat_at": heartbeat,
        "guardian_last_state": states,
        "service": service,
        "service_checked_at": now,
    }


def service_health(workspace: str, now: float) -> dict:
    unit = UNITS[workspace]

    def read(argv):
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=5, check=False
            )
            return result.stdout if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    props = read(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--no-pager",
        ]
    )
    logs = read(
        [
            "journalctl",
            "--user",
            "--unit",
            unit,
            "--lines",
            "500",
            "--output=json",
            "--no-pager",
        ]
    )
    return parse_health(props, logs, now)


class WriterLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        import fcntl
        import os

        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "a")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise RuntimeError(
                "A read-only observer already owns this telemetry path"
            ) from None
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()


async def observe(
    source: Path, workspace: str, endpoint: str, *, once=False, interval=60
):
    if not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", endpoint):
        raise ValueError("Observer endpoint must be exact http://127.0.0.1:<port>")
    if not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("Invalid local endpoint port")
    if not 30 <= interval <= 300:
        raise ValueError("Observer interval must be 30 to 300 seconds")
    read_source(source, workspace)
    from playwright.async_api import Error as BrowserError

    store = MonitorStore(source, workspace)
    previous = store.read() if store.path.exists() else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    cache = {}
    driver = browser = None
    with WriterLock(store.path.with_suffix(".lock")):
        try:
            while not stop.is_set():
                now = time.time()
                controls, attempts = read_source(source, workspace)
                health = await asyncio.to_thread(service_health, workspace, now)
                health["interval_seconds"] = interval
                samples = []
                stage = "page"
                try:
                    if browser is None or not browser.is_connected():
                        if driver is not None:
                            await driver.stop()
                        from playwright.async_api import async_playwright

                        stage = "connect"
                        driver = await async_playwright().start()
                        browser = await driver.chromium.connect_over_cdp(
                            endpoint, timeout=10000, no_defaults=True
                        )
                        stage = "page"
                        cache.clear()
                    if len(browser.contexts) != 1:
                        raise RuntimeError(
                            "Expected exactly one dedicated account context"
                        )
                    pages = browser.contexts[0].pages
                    old = {s["chat"]: s for s in (previous or {}).get("sessions", [])}
                    force = {
                        a["chat"]
                        for a in attempts
                        if a["wall"] > (old.get(a["chat"], {}).get("last_go_at") or 0)
                    }
                    samples = await collect_samples(
                        pages, previous, cache, now, force_chats=force
                    )
                    health.update(connection="connected", page_count=len(pages))
                    if len(pages) > 6:
                        health["limitations"] = [
                            "Page ceiling exceeded; DOM observation paused, inventory only."
                        ]
                except (
                    BrowserError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    TimeoutError,
                ) as exc:
                    health.update(
                        connection="unavailable",
                        error_detail=error_detail(exc, stage),
                        limitations=[
                            "Browser observation failed: " + type(exc).__name__
                        ],
                    )
                    browser = None
                snapshot = project_snapshot(
                    workspace,
                    controls,
                    attempts,
                    samples,
                    previous,
                    health,
                    time.time(),
                )
                store.record(snapshot)
                previous = snapshot
                print(
                    json.dumps(
                        {
                            "monitor": snapshot["connection"],
                            "generated_at": snapshot["generated_at"],
                            "summary": snapshot["summary"],
                            "new_events": len(snapshot["events"]),
                        }
                    ),
                    flush=True,
                )
                if once:
                    return snapshot
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
        finally:
            if driver is not None:
                await driver.stop()


def command(args) -> int:
    """Entry from Guardian CLI, before constructing the mutable recovery Store."""
    if args.command == "observer":
        asyncio.run(
            observe(
                args.state,
                args.workspace,
                args.cdp,
                once=args.once,
                interval=args.interval,
            )
        )
    else:
        print(
            json.dumps(
                MonitorStore(args.state, args.workspace).read(),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    return 0
