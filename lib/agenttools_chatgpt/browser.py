"""Owned browser lifecycle and fail-closed DOM adapter reached by CLI and HA.

Only dedicated marked profiles are opened; no CDP attachment, home-browser reuse,
stealth patches, login automation, clipboard hooks, cookie or token extraction.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright

from .core import MutationRefusal, Observation


@dataclass(frozen=True)
class Contract:
    verified_account_ui: bool = False
    conversation: str = ""
    turn: str = ""
    turn_id_attribute: str = ""
    generation_attribute: str = ""
    author_attribute: str = ""
    status: str = ""
    active_attribute: str = ""
    active_value: str = ""
    body: str = ""
    composer: str = ""
    send: str = ""
    model: str = ""
    effort: str = ""
    logged_in: str = ""
    running: str = ""
    challenge: str = ""
    attachments: str = ""
    astra_quota: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Contract:
        if path is None:
            return cls()
        raw = json.loads(Path(path).read_text())
        if not isinstance(raw, dict) or set(raw) != {f.name for f in fields(cls)}:
            raise ValueError("UI contract must contain exactly the documented fields")
        if type(raw["verified_account_ui"]) is not bool:
            raise ValueError("verified_account_ui must be a boolean")
        if any(
            not isinstance(value, str) or not value
            for key, value in raw.items()
            if key != "verified_account_ui"
        ):
            raise ValueError(
                "Every selector/attribute needs explicit nonempty account UI evidence"
            )
        return cls(**raw)


class PlaywrightAdapter:
    def __init__(self, page, contract: Contract):
        self.page, self.contract = page, contract
        self.script = Path(__file__).with_name("dom.js").read_text()

    async def observe(self) -> Observation:
        return await self._observe("observe")

    @staticmethod
    def _parse_observation(raw) -> Observation:
        names = {f.name for f in fields(Observation)}
        if not isinstance(raw, dict) or set(raw) != names:
            raise ValueError("Unrecognized UI observation")
        for field in fields(Observation):
            expected_type = (
                str
                if field.name
                in {"chat", "turn", "generation", "status", "model", "effort"}
                else bool
            )
            if type(raw[field.name]) is not expected_type:
                raise ValueError("Invalid UI observation field")
        return Observation(**raw)

    @classmethod
    def _mutation_result(cls, raw) -> bool | MutationRefusal:
        if raw is True:
            return True
        if isinstance(raw, dict) and set(raw) == {"blocked"}:
            try:
                return MutationRefusal(cls._parse_observation(raw["blocked"]))
            except (ValueError, TypeError):
                pass
        return False

    async def _observe(self, action: str, owned_draft: str = "") -> Observation:
        from playwright.async_api import Error as BrowserError

        try:
            raw = await self.page.evaluate(
                self.script,
                {
                    "contract": asdict(self.contract),
                    "action": action,
                    "ownedDraft": owned_draft,
                },
            )
            return self._parse_observation(raw)
        except (BrowserError, ValueError, TypeError):
            return Observation("", "", "")

    async def observe_api(self, owned_draft: str = "") -> Observation:
        return await self._observe("observe-api", owned_draft)

    async def _mutate(self, payload: dict[str, object]) -> object:
        from playwright.async_api import Error as BrowserError

        try:
            return await self.page.evaluate(self.script, payload)
        except BrowserError:
            # Losing transport does not prove Chromium stopped executing. Keep
            # the dispatch transaction quarantined until the operator closes
            # Chrome and then terminates this owner; cancellation cannot unlock it.
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue

    async def prepare_api(
        self, expected: Observation, prompt: str
    ) -> bool | MutationRefusal:
        return self._mutation_result(
            await self._mutate(
                {
                    "contract": asdict(self.contract),
                    "expected": asdict(expected),
                    "ownedDraft": prompt,
                    "action": "prepare-api",
                    "deadline": time.time() * 1000 + 5000,
                },
            )
        )

    async def send_api(
        self, expected: Observation, prompt: str
    ) -> bool | MutationRefusal:
        return self._mutation_result(
            await self._mutate(
                {
                    "contract": asdict(self.contract),
                    "expected": asdict(expected),
                    "ownedDraft": prompt,
                    "action": "send-api",
                    "deadline": time.time() * 1000 + 5000,
                },
            )
        )

    async def send(self, expected: Observation) -> bool | MutationRefusal:
        clicked = await self._mutate(
            {
                "contract": asdict(self.contract),
                "expected": asdict(expected),
                "action": "send",
                "deadline": time.time() * 1000 + 5000,
            },
        )
        if clicked is not True:
            return self._mutation_result(clicked)
        for _ in range(20):
            if await self.page.evaluate(
                self.script,
                {
                    "contract": asdict(self.contract),
                    "expected": asdict(expected),
                    "action": "ack",
                },
            ):
                return True
            await asyncio.sleep(0.1)
        return False


class BrowserLifecycle:
    def __init__(self, root: Path, workspace: str, executable: str | None = None):
        if workspace not in ("home", "glide.vc"):
            raise ValueError("workspace must be home or glide.vc")
        self.root = Path(root).expanduser().resolve()
        self.profile = self.root / workspace
        if self.profile.is_symlink() or self.profile.resolve().parent != self.root:
            raise ValueError(
                "Profile symlinks are forbidden; home and glide.vc must be separate"
            )
        self.workspace = workspace
        self.executable = (
            executable or shutil.which("google-chrome") or shutil.which("chromium")
        )
        self.context: BrowserContext | None = None
        self.playwright: Playwright | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.lock: TextIO | None = None

    def acquire(self) -> None:
        if self.lock is not None:
            raise RuntimeError("Browser lifecycle is already started")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.profile.is_symlink():
            raise ValueError("Profile symlinks are forbidden")
        singleton = self.profile / "SingletonLock"
        if singleton.exists() or singleton.is_symlink():
            raise RuntimeError(
                "Browser profile has an existing Chromium lock; inspect it manually"
            )
        marker = self.profile / "guardian-owner.json"
        owner = {
            "application": "agenttools-chatgpt",
            "workspace": self.workspace,
            "profile": str(self.profile),
            "uid": os.getuid(),
        }
        # No directory with existing login/profile state is silently adopted.
        if self.profile.exists() and (
            not marker.is_file() or json.loads(marker.read_text()) != owner
        ):
            raise ValueError(
                "Refusing unowned browser profile; choose a fresh dedicated root"
            )
        self.profile.mkdir(mode=0o700, exist_ok=True)
        self.lock = open(self.profile / "guardian.lock", "a")  # noqa: SIM115 — lock must span browser lifecycle
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker.write_text(json.dumps(owner))
            marker.chmod(0o600)
        except BaseException:
            self.lock.close()
            self.lock = None
            raise

    async def start(self, manual: bool = False):
        self.acquire()
        try:
            if manual:
                if not self.executable:
                    raise RuntimeError(
                        "Set CHROME_BIN to a browser executable for manual mode"
                    )
                self.process = await asyncio.create_subprocess_exec(
                    self.executable,
                    "--user-data-dir=" + str(self.profile),
                    "--no-first-run",
                    "https://chatgpt.com/",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                return None
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile), headless=False, executable_path=self.executable
            )
            return self.context
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        errors: list[BaseException] = []
        if self.context:
            try:
                await asyncio.wait_for(self.context.close(), timeout=5)
                self.context = None
            except BaseException as error:
                logging.getLogger(__name__).exception("Owned browser teardown failed")
                errors.append(error)
        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), timeout=5)
                self.playwright = None
            except BaseException as error:
                logging.getLogger(__name__).exception("Owned browser teardown failed")
                errors.append(error)
        if self.process:
            try:
                if self.process.returncode is None:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await asyncio.wait_for(self.process.wait(), timeout=10)
                singleton = self.profile / "SingletonLock"
                if singleton.exists() or singleton.is_symlink():
                    raise RuntimeError(
                        "Manual browser launcher detached or left a profile lock; close its window manually"
                    )
                self.process = None
            except BaseException as error:
                logging.getLogger(__name__).exception("Owned browser teardown failed")
                errors.append(error)
        # Failed teardown retains ownership until a human confirms profile shutdown.
        if errors:
            raise errors[0]
        if self.lock:
            self.lock.close()
            self.lock = None
