"""Existing ordinary Chrome CDP service, reached by `chatgpt-guardian live`.

Only explicitly enrolled pages are inspected. The OS lock covers the endpoint
across workspaces; teardown disconnects the driver and never closes Chrome.
Missing tabs stay missing. Service observation is read-only even with --apply:
menu probes cannot safely maintain the core's stability window at idle cadence.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
import signal
import time
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from .browser import observe_tabs
from .core import Guardian, Observation, Store
from .live_browser import LiveAdapter
from .service import ControlServer
from .urls import canonical_chat


def validate_cdp(endpoint: str) -> str:
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", endpoint)
    if not match or not 1 <= int(match[1]) <= 65535 or str(int(match[1])) != match[1]:
        raise ValueError(
            "CDP must be exact http://127.0.0.1:<port> without path or credentials"
        )
    return endpoint


class LiveOwner:
    def __init__(self, root: Path, endpoint: str):
        self.root = Path(root).expanduser().resolve()
        self.endpoint = validate_cdp(endpoint)
        self.lock: TextIO | None = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / ("cdp-" + self.endpoint.rsplit(":", 1)[1] + ".lock")
        if path.is_symlink():
            raise ValueError("Owner lock must not be a symlink")
        self.lock = path.open("a")
        path.chmod(0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            self.lock = None
            raise RuntimeError("CDP endpoint already has a Guardian owner") from exc
        return self

    def __exit__(self, *_):
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def enrolled_pages(context, chats: set[str]):
    pages: dict[str, list] = {chat: [] for chat in chats}
    for page in context.pages:
        if page.is_closed():
            continue
        try:
            chat = canonical_chat(page.url)
        except ValueError:
            continue
        if chat in pages:
            pages[chat].append(page)
    return pages


async def watched_pages(context, store: Store, originals: dict[str, str]):
    controls = await asyncio.to_thread(store.controls)
    chats = {
        canonical_chat(key.removeprefix("watch:"))
        for key, value in controls.items()
        if key.startswith("watch:") and value == "1"
    }
    return enrolled_pages(context, chats)


def resource_pause(
    page_count: int,
    available_bytes: int | None,
    *,
    max_pages: int = 8,
    min_available_mib: int = 2048,
) -> str:
    if page_count > max_pages:
        return "page-ceiling"
    if available_bytes is None:
        return "memory-unavailable"
    if available_bytes < min_available_mib * 1024**2:
        return "low-memory"
    return ""


def available_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class TransportGuard:
    """Latch browser-observed transport failures; never request response contents."""

    def __init__(self, context, store: Store):
        self.context = context
        self.store = store
        self.reason = ""
        self.callback = self.on_response
        context.on("response", self.callback)

    def record(self, url: str, status: int) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "chatgpt.com"
            or not parsed.path.startswith(("/backend-api/", "/api/auth/", "/auth/"))
        ):
            return
        if status not in (429, 401, 403):
            return
        reason = (
            "transport HTTP 429; manual resume required"
            if status == 429
            else "human challenge"
        )
        if self.reason != reason or "stop:*" not in self.store.controls():
            self.reason = reason
            self.store.stop("*", reason)

    def on_response(self, response) -> None:
        self.record(response.url, response.status)

    async def seed(self, page) -> None:
        entries = await page.evaluate("""() => performance.getEntriesByType('resource')
            .filter(e => performance.now() - e.responseEnd <= 300000 &&
                [429, 401, 403].includes(e.responseStatus))
            .map(e => ({url: e.name, status: e.responseStatus}))""")
        for entry in entries:
            self.record(entry["url"], entry["status"])

    def close(self) -> None:
        self.context.remove_listener("response", self.callback)


async def recover_live(
    guardian: Guardian, adapter: LiveAdapter, observed: Observation, captured_at: float
) -> str:
    if (
        guardian.apply
        and observed.safe_failure()
        and observed.model == "Astra"
        and await adapter.select_sol(guardian.store, observed.chat)
    ):
        guardian.stability.pop(observed.chat, None)
        observed = await adapter.observe()
        captured_at = time.monotonic()
    return await guardian.run_once(adapter, initial=observed, observed_at=captured_at)


async def serve_live(args, store: Store) -> None:
    from playwright.async_api import Error as BrowserError
    from playwright.async_api import async_playwright

    from .cli import ensure_token

    endpoint = validate_cdp(args.cdp)
    originals = {canonical_chat(raw): raw for raw in args.watch}
    guardian = Guardian(store, apply=args.apply)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    root = Path.home() / ".local/share/agent-tools/chatgpt-guardian"
    control = ControlServer(
        store, ensure_token(store.path.parent / "control.token"), args.control_port
    )
    poll_interval = getattr(args, "poll_interval", 60)
    if not 30 <= poll_interval < float("inf"):
        raise ValueError("poll interval must be finite and at least 30 seconds")
    reports: dict[str, tuple[dict, float]] = {}

    def report(key: str, payload: dict) -> None:
        previous = reports.get(key)
        now = time.monotonic()
        if previous is None or previous[0] != payload or now - previous[1] >= 300:
            print(json.dumps(payload), flush=True)
            reports[key] = payload, now

    with LiveOwner(root, endpoint):
        control.start()
        driver = None
        transport = None
        try:
            driver = await async_playwright().start()
            browser = await driver.chromium.connect_over_cdp(
                endpoint, timeout=10000, no_defaults=True
            )
            if len(browser.contexts) != 1:
                raise RuntimeError(
                    "Expected one dedicated ordinary Chrome account context"
                )
            context = browser.contexts[0]
            transport = TransportGuard(context, store)
            for chat in originals:
                store.enroll(chat)
            adapters: dict[object, LiveAdapter] = {}
            seeded: set[object] = set()
            while not stop.is_set():
                delay = poll_interval
                if not browser.is_connected():
                    raise RuntimeError(
                        "CDP browser disconnected; restart preserves uncertain sends"
                    )
                pages = await watched_pages(context, store, originals)
                for chat in set(guardian.stability) - set(pages):
                    guardian.stability.pop(chat, None)
                unique = [matches[0] for matches in pages.values() if len(matches) == 1]
                adapters = {
                    page: adapters.get(page)
                    or LiveAdapter(page, allow_active_probe=False)
                    for matches in pages.values()
                    for page in matches
                }
                try:
                    pause = resource_pause(
                        len(context.pages),
                        available_memory(),
                        max_pages=getattr(args, "max_pages", 8),
                        min_available_mib=getattr(args, "min_available_mib", 2048),
                    )
                    seeded.intersection_update(adapters)
                    for page in adapters if not pause else ():
                        if page not in seeded:
                            await asyncio.wait_for(transport.seed(page), timeout=5)
                            seeded.add(page)
                    samples = await observe_tabs(
                        list(adapters.values()) if not pause else [],
                        record_safety=guardian.record_safety,
                    )
                    controls = await asyncio.to_thread(store.controls)
                    for _, observed, _ in samples:
                        state = "observe-only"
                        if not observed.chat:
                            pause = "observation-unverified"
                            delay = max(60, poll_interval)
                        if "stop:*" in controls or "stop:" + observed.chat in controls:
                            state = "manual-stop"
                        elif args.apply:
                            # No verified confirmation burst is available: never
                            # dispatch from sparse or menu-dependent observations.
                            state = "recovery-confirmation-disabled"
                        report(
                            observed.chat,
                            {
                                "chat": observed.chat,
                                "state": state,
                                "apply": args.apply,
                                "model": observed.model,
                                "verified": observed.verified,
                            },
                        )
                    for chat, matches in pages.items():
                        if len(matches) != 1:
                            guardian.stability.pop(chat, None)
                            report(
                                chat,
                                {"chat": chat, "state": "missing-or-duplicate-page"},
                            )
                    report(
                        "health",
                        {
                            "health": pause or "connected",
                            "workspace": args.workspace,
                            "watched": len(unique),
                            "mode": "observe",
                            "recovery": "confirmation-disabled",
                            "transport": transport.reason,
                        },
                    )
                except (BrowserError, asyncio.TimeoutError, ValueError) as exc:
                    guardian.stability.clear()
                    delay = max(60, poll_interval)
                    report(
                        "health",
                        {
                            "health": "observation-error",
                            "error": type(exc).__name__,
                            "retry_seconds": max(60, poll_interval),
                        },
                    )
                if args.once:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except BrowserError as exc:
            raise RuntimeError(
                "CDP unavailable; inspect dedicated Chrome and endpoint; no recovery attempted"
            ) from exc
        finally:
            # Browser.close/context.close would terminate someone else's Chrome.
            try:
                if transport is not None:
                    transport.close()
                if driver is not None:
                    await driver.stop()
            finally:
                control.close()


async def select_live_sol(args, store: Store) -> bool:
    """Attach without navigation and change one explicitly requested enrolled chat."""
    from playwright.async_api import async_playwright

    endpoint = validate_cdp(args.cdp)
    chat = canonical_chat(args.chat)
    root = Path.home() / ".local/share/agent-tools/chatgpt-guardian"
    with LiveOwner(root, endpoint):
        driver = await async_playwright().start()
        try:
            browser = await driver.chromium.connect_over_cdp(
                endpoint, timeout=10000, no_defaults=True
            )
            if len(browser.contexts) != 1:
                raise RuntimeError(
                    "Expected one dedicated ordinary Chrome account context"
                )
            pages = enrolled_pages(browser.contexts[0], {chat})[chat]
            if len(pages) != 1:
                raise ValueError(
                    "Sol helper requires one already open matching chat; no navigation attempted"
                )
            return await LiveAdapter(pages[0]).select_sol(store, chat)
        finally:
            await driver.stop()


if __name__ == "__main__":
    import sys

    from .cli import main

    raise SystemExit(main(["live", *sys.argv[1:]]))
