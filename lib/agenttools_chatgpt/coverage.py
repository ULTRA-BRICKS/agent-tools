"""Coverage reconciliation: is every ACTIVE chat of an account genuinely supervised?

Run as ``chatgpt-guardian --workspace home|glide.vc coverage --cdp-port PORT [--apply]``
(a systemd timer runs it every 60 seconds). Evidence is only the persisted monitor
snapshot, the recovery database (read-only) and the owned browser's loopback DevTools
HTTP inventory after ``startup.verify_browser``; no new CDP observer exists. Without
``--apply`` nothing is written, started or sent. With ``--apply`` and per-account
``coverage_enabled`` the only effects are:

* enrol a fresh, unique, running or ``Thinking failed`` chat that has NO ``watch:`` key
  (``auto_watch_active``; ``INSERT OR IGNORE``, so a manual unwatch stays);
* reopen missing enrolled chats through ``startup.restore`` (one attempt per chat per
  inventory, durable);
* start a FAILED recovery service (``recovery_autostart``, bounded; never an inactive one);
* send one deduplicated Russian owner alert through ``tg``.

Stops, quota, uncertain sends, models and the browser itself are never touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .core import Store
from .lock import AccountBusy, account_lock
from .monitor import MonitorStore, _recovery, clean_detail, read_source
from .monitor_observer import UNITS
from .startup import (
    HTTP_TIMEOUT_SECONDS,
    ChromeDevTools,
    DevTools,
    Processes,
    ProcFilesystem,
    StartupError,
    UncertainCreation,
    _cdp_port,
    page_chat,
    restore,
    verify_browser,
)
from .urls import canonical_chat

CONFIG_KEYS = ("coverage_enabled", "auto_watch_active", "recovery_autostart")
RESTORE_COOLDOWN_SECONDS = 900
AUTOSTART_COOLDOWN_SECONDS = 300
AUTOSTART_MAX_ATTEMPTS = 3
ALERT_RETRY_SECONDS = 900
ALERT_MAX_DELIVERY_ATTEMPTS = 5
ALERT_REARM_SECONDS = 3600
RESTORED_GRACE_SECONDS = 300
MESSAGE_LINES = 8
NOTIFY_TIMEOUT_SECONDS = 30
START_TIMEOUT_SECONDS = 30
STALLED_MINUTES = 15
LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS scope(workspace TEXT PRIMARY KEY)",
    (
        "CREATE TABLE IF NOT EXISTS restore("
        "chat TEXT PRIMARY KEY, attempted_at REAL NOT NULL, "
        "outcome TEXT NOT NULL, signature TEXT NOT NULL)"
    ),
    "CREATE TABLE IF NOT EXISTS autostart(at REAL NOT NULL, outcome TEXT NOT NULL)",
    (
        "CREATE TABLE IF NOT EXISTS alerts("
        "key TEXT PRIMARY KEY, kind TEXT NOT NULL, first_at REAL NOT NULL, "
        "last_at REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, "
        "attempts INTEGER NOT NULL, cleared_at REAL)"
    ),
)
LEDGER_TABLES = ("scope", "restore", "autostart", "alerts")

wall_clock = time.time


class ServiceError(Exception):
    """The service manager refused or could not be reached."""


@dataclass(frozen=True)
class Config:
    coverage_enabled: bool = False
    auto_watch_active: bool = False
    recovery_autostart: bool = False


@dataclass(frozen=True)
class Delivery:
    ok: bool
    detail: str = ""


class Systemd(Protocol):
    def show(self, unit: str) -> dict[str, str]: ...
    def start(self, unit: str) -> None: ...


Notifier = Callable[[str], Delivery]


def config_path(workspace: str) -> Path:
    override = os.environ.get("CHATGPT_GUARDIAN_COVERAGE_CONFIG")
    if override:
        return Path(override)
    return (
        Path.home()
        / ".config/agent-tools/chatgpt-guardian"
        / (workspace + ".coverage.json")
    )


def load_config(workspace: str, path: Path | None = None) -> Config:
    """Per-account flags; an absent file means every flag is off (no effects)."""
    path = Path(path) if path else config_path(workspace)
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Unreadable coverage configuration: " + str(exc)) from exc
    if (
        not isinstance(raw, dict)
        or raw.get("workspace") != workspace
        or not set(raw) <= {"workspace", *CONFIG_KEYS}
        or any(not isinstance(raw[k], bool) for k in CONFIG_KEYS if k in raw)
    ):
        raise ValueError(
            "Invalid coverage configuration: expected the account's workspace and "
            "boolean " + ", ".join(CONFIG_KEYS)
        )
    return Config(**{k: raw[k] for k in CONFIG_KEYS if k in raw})


def ledger_path(state: Path) -> Path:
    return state.with_name(state.stem + ".coverage.sqlite")


class Ledger:
    """Durable restore/autostart/alert records, separate from recovery state.

    Reads never create the file; only an applied effect does.
    """

    def __init__(self, path: Path, workspace: str):
        self.path = path
        self.workspace = workspace

    def _owned(self, db: sqlite3.Connection) -> bool:
        """True for this account's ledger, False for a truly uninitialised one.

        Foreign scope, unrecognised tables or records without a scope are refused:
        nothing here ever adopts or deletes evidence it cannot attribute.
        """
        tables = {
            name
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables - set(LEDGER_TABLES):
            raise ValueError("Coverage ledger holds unrecognised tables")
        scope = (
            db.execute("SELECT workspace FROM scope").fetchall()
            if "scope" in tables
            else []
        )
        if scope:
            if scope != [(self.workspace,)]:
                raise ValueError("Coverage ledger belongs to a different account")
            return True
        for table in tables:  # names come from LEDGER_TABLES only
            if db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone():
                raise ValueError("Coverage ledger has records but no account scope")
        return False

    def _select(self, sql: str, args: tuple = ()) -> list[tuple]:
        if not self.path.exists():
            return []
        with closing(
            sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            if not self._owned(db):
                return []
            try:
                return db.execute(sql, args).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(
                    exc
                ):  # an incomplete own ledger holds nothing
                    return []
                raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction for schema, scope row and the caller's writes: a kill at any
        point leaves either nothing or everything."""
        if self.path.is_symlink():
            raise ValueError("Coverage ledger must not be a symlink")
        os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600))
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                owned = self._owned(db)
                for statement in LEDGER_SCHEMA:
                    db.execute(statement)
                if not owned:
                    db.execute("INSERT INTO scope VALUES (?)", (self.workspace,))
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        self.path.chmod(0o600)

    def restore_rows(self) -> dict[str, dict]:
        return {
            chat: {"at": at, "outcome": outcome, "signature": signature}
            for chat, at, outcome, signature in self._select(
                "SELECT chat,attempted_at,outcome,signature FROM restore"
            )
        }

    def record_restore(self, chat: str, now: float, outcome: str, signature: str):
        with self._write() as db:
            db.execute(
                "INSERT OR REPLACE INTO restore VALUES (?,?,?,?)",
                (chat, now, outcome, signature),
            )

    def reset_restore(self) -> None:
        if self.restore_rows():
            with self._write() as db:
                db.execute("DELETE FROM restore")

    def reserve_restore(
        self, chats: list[str], now: float, signature: str
    ) -> list[str]:
        """Check-and-set in one write transaction; returns the chats actually reserved.

        A chat whose attempt is still deferred (same inventory or inside the cooldown) is
        never replaced, whoever asks.
        """
        reserved = []
        with self._write() as db:
            for chat in chats:
                row = db.execute(
                    "SELECT attempted_at, signature FROM restore WHERE chat=?", (chat,)
                ).fetchone()
                if row and (
                    row[1] == signature or now - row[0] < RESTORE_COOLDOWN_SECONDS
                ):
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO restore VALUES (?,?,'pending',?)",
                    (chat, now, signature),
                )
                reserved.append(chat)
        return reserved

    def forget_restore(self, chats: list[str]) -> None:
        if chats:
            with self._write() as db:
                db.executemany(
                    "DELETE FROM restore WHERE chat=?", [(c,) for c in chats]
                )

    def autostart_rows(self) -> list[tuple[float, str]]:
        return self._select("SELECT at,outcome FROM autostart ORDER BY at")

    def record_autostart(self, now: float, outcome: str) -> None:
        with self._write() as db:
            db.execute("INSERT INTO autostart VALUES (?,?)", (now, outcome))

    def reset_autostart(self) -> None:
        if self.autostart_rows():
            with self._write() as db:
                db.execute("DELETE FROM autostart")

    def alert_rows(self) -> dict[str, dict]:
        return {
            key: {
                "status": status,
                "last_at": last_at,
                "attempts": attempts,
                "cleared_at": cleared_at,
            }
            for key, status, last_at, attempts, cleared_at in self._select(
                "SELECT key,status,last_at,attempts,cleared_at FROM alerts"
            )
        }

    def settle_alerts(
        self,
        now: float,
        active: set[str],
        rearm: set[str],
        outcomes: dict[str, tuple[str, str, str, int]],
    ) -> None:
        """One transaction: record deliveries, mark cleared conditions, quietly re-arm."""
        with self._write() as db:
            for key, (kind, status, detail, attempts) in outcomes.items():
                db.execute(
                    "INSERT INTO alerts VALUES (?,?,?,?,?,?,?,NULL) ON CONFLICT(key) DO "
                    "UPDATE SET kind=?, last_at=?, status=?, detail=?, attempts=?, "
                    "cleared_at=NULL",
                    (key, kind, now, now, status, detail, attempts)
                    + (kind, now, status, detail, attempts),
                )
            for (key,) in db.execute("SELECT key FROM alerts").fetchall():
                if key in outcomes:
                    continue
                if key in rearm:
                    db.execute("UPDATE alerts SET cleared_at=NULL WHERE key=?", (key,))
                elif key not in active:
                    db.execute(
                        "DELETE FROM alerts WHERE key=? AND status='delivery-failed'",
                        (key,),
                    )
                    db.execute(
                        "UPDATE alerts SET cleared_at=? WHERE key=? AND cleared_at IS NULL",
                        (now, key),
                    )
            db.execute(
                "DELETE FROM alerts WHERE cleared_at IS NOT NULL AND cleared_at < ?",
                (now - ALERT_REARM_SECONDS,),
            )


class SystemdUser:
    """``systemctl --user``; never touches the browser unit."""

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["systemctl", "--user", *argv],
                capture_output=True,
                text=True,
                timeout=START_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(type(exc).__name__) from exc

    def show(self, unit: str) -> dict[str, str]:
        done = self._run(
            [
                "show",
                unit,
                "--property=ActiveState",
                "--property=SubState",
                "--no-pager",
            ]
        )
        if done.returncode != 0:
            raise ServiceError("systemctl show exited " + str(done.returncode))
        return dict(
            line.split("=", 1) for line in done.stdout.splitlines() if "=" in line
        )

    def start(self, unit: str) -> None:
        done = self._run(["start", "--no-block", unit])
        if done.returncode != 0:
            raise ServiceError(
                clean_detail(
                    "systemctl start exited "
                    + str(done.returncode)
                    + ": "
                    + done.stderr
                )
            )


def tg_notifier(text: str) -> Delivery:
    """Owner alert through the supported ``tg`` CLI (no direct Bot API)."""
    try:
        done = subprocess.run(
            [
                "tg",
                "--title",
                "ChatGPT Guardian",
                "--tag",
                "problem",
                "--format",
                "plain",
                text,
            ],
            capture_output=True,
            text=True,
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Delivery(False, clean_detail(type(exc).__name__ + ": " + str(exc)))
    if done.returncode != 0:
        return Delivery(
            False,
            clean_detail("tg exited " + str(done.returncode) + ": " + done.stderr),
        )
    return Delivery(True)


def inventory_signature(pid: int, live: Mapping[str, int], enrolled: set[str]) -> str:
    """Identity of the browser inventory that matters: the process and which enrolled
    chats are open exactly once. The same predicate is used before and after a restore;
    blank/login tabs left by a failed restore and duplicated chats never enter it."""
    unique = sorted(url for url in enrolled if live.get(url) == 1)
    return hashlib.sha256(json.dumps([pid, unique]).encode()).hexdigest()[:24]


def _live_chats(pages: list) -> Counter:
    """Canonical chat -> number of open tabs; any non-object entry fails closed."""
    if any(not isinstance(p, dict) for p in pages):
        raise StartupError("Unexpected DevTools page inventory")
    return Counter(c for p in pages if p.get("type") == "page" and (c := page_chat(p)))


def _canonical(chat: str) -> bool:
    try:
        return canonical_chat(chat) == chat
    except ValueError:
        return False


def _label(chat: str) -> str:
    return chat.rsplit("/", 1)[1]


@dataclass(frozen=True)
class Alert:
    key: str
    kind: str
    line: str


def _compose(workspace: str, alerts: list[Alert]) -> str:
    lines = ["• " + a.line for a in alerts[:MESSAGE_LINES]]
    if len(alerts) > MESSAGE_LINES:
        lines.append("…и ещё " + str(len(alerts) - MESSAGE_LINES))
    return "Guardian " + workspace + ":\n" + "\n".join(lines)


def _chat_rows(
    controls: dict,
    attempts: list[dict],
    snapshot: dict,
    live: Counter | None,
    snapshot_fresh: bool,
    service_active: bool,
) -> tuple[list[dict], dict[str, dict]]:
    sessions = {s["chat"]: s for s in snapshot.get("sessions", [])}
    watch = {k[6:]: v for k, v in controls.items() if k.startswith("watch:")}
    chats = sorted(
        c for c in set(watch) | set(sessions) | set(live or ()) if _canonical(c)
    )
    rows = []
    for chat in chats:
        session = sessions.get(chat, {})
        flag = watch.get(chat)
        enrolled = flag == "1"
        pages = None if live is None else live.get(chat, 0)
        present = None if pages is None else pages == 1
        fresh = bool(
            snapshot_fresh
            and session
            and session.get("observation") == "current"
            and not session.get("session_stale")
            and not session.get("check_stale")
            and session.get("page_count") == 1
        )
        status = session.get("status") if fresh else ""
        running = fresh and session.get("running") is True
        if not fresh:
            activity = "unknown"
        elif running:
            activity = "running"
        elif status == "Thinking failed":
            activity = "failed"
        elif status == "quota-or-rate-limit":
            activity = "rate-limited"
        elif status == "complete":
            activity = "finished"
        else:
            activity = "idle"
        recovery = _recovery(chat, controls, attempts)
        eligible = bool(
            service_active
            and live is not None
            and enrolled
            and present
            and fresh
            and recovery["enabled"]
        )
        rows.append(
            {
                "chat": _label(chat),
                "enrolled": enrolled,
                "manually_unwatched": flag == "0",
                "known_watch_key": flag is not None,
                "present": present,
                "live_pages": pages,
                "fresh_chat": fresh,
                "activity": activity,
                "active": activity in ("running", "failed"),
                "recovery_eligible": eligible,
                # Probe-level only: service/browser/page/control checks, not a
                # validated model selection or a send.
                "eligibility_for_probe": eligible,
                "model_proof": "unknown",
                "blockers": recovery["blockers"],
                "stalled": bool(
                    fresh and running and session.get("no_visible_progress")
                ),
            }
        )
    return rows, sessions


def _empty_report(
    workspace: str, port: int, apply: bool, config: Config, now: float
) -> dict:
    """Every section exists with its final shape, whatever stops the run early."""
    return {
        "workspace": workspace,
        "apply": apply,
        "port": port,
        "config": asdict(config),
        "generated_at": now,
        "busy": False,
        "note": "",
        "blocked": [],
        "chats": [],
        "browser": {
            "verified": False,
            "pages": 0,
            "conversation_pages": 0,
            "auxiliary_pages": 0,
        },
        "service": {"unit": UNITS[workspace], "active_state": "unknown"},
        "summary": {
            "fresh_snapshot": False,
            "snapshot_age_seconds": None,
            "enrolled": 0,
            "present": 0,
            "fresh": 0,
            "active": 0,
            "recovery_eligible": 0,
            "uncovered": [],
            "observation_complete": False,
            "resource_paused": None,
        },
        "plan": {
            "enroll": [],
            "restore": [],
            "restore_deferred": [],
            "inventory_signature": None,
            "autostart": False,
            "autostart_skipped": None,
            "alerts": [],
        },
        "effects": {"enrolled": [], "opened": [], "alerts": [], "autostart": None},
    }


def reconcile(
    workspace: str,
    *,
    state: Path,
    port: int,
    apply: bool,
    config: Config,
    processes: Processes,
    devtools: DevTools,
    systemd: Systemd,
    notify: Notifier,
    now: float,
    home: Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    reset_restore: bool = False,
) -> dict:
    """Applied runs hold the account lock for their whole duration (released on any
    error); a dry run reads only and never creates the lock."""
    report = _empty_report(workspace, port, apply, config, now)
    with ExitStack() as stack:
        if apply and config.coverage_enabled:
            try:
                stack.enter_context(account_lock(state))
            except AccountBusy:
                report["busy"] = True
                return report
            except ValueError as exc:
                report["blocked"].append(
                    {"scope": "lock", "reason": clean_detail(str(exc))}
                )
                return report
        return _reconcile(
            report,
            workspace,
            state=state,
            port=port,
            apply=apply,
            config=config,
            processes=processes,
            devtools=devtools,
            systemd=systemd,
            notify=notify,
            now=now,
            home=home,
            clock=clock,
            sleep=sleep,
            reset_restore=reset_restore,
        )


def _reconcile(
    report: dict,
    workspace: str,
    *,
    state: Path,
    port: int,
    apply: bool,
    config: Config,
    processes: Processes,
    devtools: DevTools,
    systemd: Systemd,
    notify: Notifier,
    now: float,
    home: Path | None,
    clock: Callable[[], float] | None,
    sleep: Callable[[float], None] | None,
    reset_restore: bool,
) -> dict:
    unit = UNITS[workspace]
    permitted = apply and config.coverage_enabled
    ledger = Ledger(ledger_path(state), workspace)
    blocked = report["blocked"]
    plan, effects = report["plan"], report["effects"]
    if apply and not config.coverage_enabled:
        report["note"] = "coverage_enabled is false: report only, no effects"

    def block(scope: str, reason: str) -> None:
        blocked.append({"scope": scope, "reason": clean_detail(reason)})

    try:
        if reset_restore and permitted:
            ledger.reset_restore()
        controls, attempts = read_source(state, workspace)
        snapshot = MonitorStore(state, workspace).read(now)
        restore_rows = ledger.restore_rows()
        alert_rows = ledger.alert_rows()
        autostart_rows = ledger.autostart_rows()
    except (ValueError, sqlite3.Error, OSError) as exc:
        block("state", str(exc))
        return report

    live: Counter | None = None
    pid = 0
    page_total = 0
    try:
        pid = verify_browser(workspace, processes, home, port)
        pages = devtools.pages(HTTP_TIMEOUT_SECONDS)
        live = _live_chats(pages)
        page_total = sum(p.get("type") == "page" for p in pages)
    except (StartupError, ValueError) as exc:
        block("browser", str(exc))
    conversation_pages = sum(live.values()) if live is not None else 0
    report["browser"] = {
        "verified": live is not None,
        "pages": page_total,
        "conversation_pages": conversation_pages,
        "auxiliary_pages": page_total - conversation_pages,
    }

    try:
        service = systemd.show(unit).get("ActiveState", "unknown")
    except ServiceError as exc:
        service = "unknown"
        block("service", str(exc))
    report["service"] = {"unit": unit, "active_state": service}

    snapshot_fresh = bool(
        snapshot.get("connection") == "connected"
        and not snapshot.get("stale")
        and snapshot.get("generated_at") is not None
    )
    rows, sessions = _chat_rows(
        controls, attempts, snapshot, live, snapshot_fresh, service == "active"
    )
    report["chats"] = rows
    url = {r["chat"]: "https://chatgpt.com/c/" + r["chat"] for r in rows}
    report["summary"] = {
        "fresh_snapshot": snapshot_fresh,
        "snapshot_age_seconds": snapshot.get("snapshot_age_seconds"),
        "enrolled": sum(r["enrolled"] for r in rows),
        "present": sum(r["enrolled"] and bool(r["present"]) for r in rows),
        "fresh": sum(r["enrolled"] and r["fresh_chat"] for r in rows),
        "active": sum(r["active"] for r in rows),
        "recovery_eligible": sum(r["recovery_eligible"] for r in rows),
        "uncovered": [
            r["chat"] for r in rows if not r["enrolled"] and (r["live_pages"] or 0) > 0
        ],
        # The observer's own verdict: every enrolled chat has current evidence and no open
        # conversation tab went unread. Independent of the sender's mutation pause below.
        # Both are claims about the browser, so they need a fresh snapshot AND a browser
        # this run just verified.
        "observation_complete": bool(
            snapshot_fresh
            and live is not None
            and snapshot.get("summary", {}).get("coverage_complete")
        ),
        # "" = admitted, a reason = paused, None = unknown (stale snapshot, browser not
        # verified now).
        "resource_paused": snapshot.get("resource_paused")
        if snapshot_fresh and live is not None
        else None,
    }

    # --- plan ---------------------------------------------------------------
    flagged = config.coverage_enabled
    if live is not None and snapshot_fresh and flagged and config.auto_watch_active:
        plan["enroll"] = [
            r["chat"]
            for r in rows
            if r["active"]
            and r["fresh_chat"]
            and not r["known_watch_key"]
            and r["live_pages"] == 1
        ]
    missing = [r["chat"] for r in rows if r["enrolled"] and r["live_pages"] == 0]
    unit_failed = service == "failed"
    skip_reason = ""
    if unit_failed and flagged and config.recovery_autostart:
        if live is None:
            skip_reason = "browser-not-verified"
        elif "stop:*" in controls:
            skip_reason = "operator-stop"
        elif len(autostart_rows) >= AUTOSTART_MAX_ATTEMPTS:
            skip_reason = "attempt-cap-reached"
        elif (
            autostart_rows and now - autostart_rows[-1][0] < AUTOSTART_COOLDOWN_SECONDS
        ):
            skip_reason = "cooldown"
        plan["autostart"] = not skip_reason
        plan["autostart_skipped"] = skip_reason or None

    # --- effects ------------------------------------------------------------
    enrolled_now: set[str] = set()
    if permitted and plan["enroll"]:
        try:
            store = Store(state, account=workspace)
            for chat in plan["enroll"]:
                store.enroll(url[chat])  # INSERT OR IGNORE: a manual unwatch survives
            after = read_source(state, workspace)[0]
            enrolled_now = {
                c for c in plan["enroll"] if after.get("watch:" + url[c]) == "1"
            }
            effects["enrolled"] = sorted(enrolled_now)
        except (ValueError, sqlite3.Error, OSError) as exc:
            block("enroll", str(exc))

    enrolled_urls = {url[r["chat"]] for r in rows if r["enrolled"]} | {
        url[c] for c in enrolled_now
    }
    signature = inventory_signature(pid, live or Counter(), enrolled_urls)
    plan["inventory_signature"] = signature
    deferred = [
        c
        for c in missing
        if url[c] in restore_rows
        and (
            restore_rows[url[c]]["signature"] == signature
            or now - restore_rows[url[c]]["at"] < RESTORE_COOLDOWN_SECONDS
        )
    ]
    if live is not None and flagged:
        plan["restore"] = [c for c in missing if c not in deferred]
        plan["restore_deferred"] = deferred
    opened_now: set[str] = set()
    if permitted and plan["restore"]:
        candidates = [url[c] for c in plan["restore"]]
        # Reserved (check-and-set) before any tab exists: a crash mid-restore can never
        # grant a second try, and a reservation is never replaced by another caller.
        reserved = ledger.reserve_restore(candidates, now, signature)
    else:
        candidates, reserved = [], []
    if reserved:
        outcome: dict = {}
        try:
            restore(
                workspace,
                apply=True,
                state=state,
                processes=processes,
                devtools=devtools,
                home=home,
                port=port,
                clock=clock,
                sleep=sleep,
                result=outcome,
                skip=frozenset(url[c] for c in deferred)
                | (frozenset(candidates) - frozenset(reserved)),
            )
        except UncertainCreation as exc:
            block("restore", "creation outcome unknown; inventory first: " + str(exc))
        except (StartupError, ValueError) as exc:
            block("restore", str(exc))
        attempted = list(outcome.get("opened", []))
        if outcome.get("uncertain"):
            attempted.append(outcome["uncertain"])
        opened_now = set(outcome.get("opened", []))
        effects["opened"] = sorted(opened_now)
        spent = {"https://chatgpt.com/c/" + label for label in attempted}
        # No tab was created for these, so nothing was spent; they stay retryable.
        ledger.forget_restore([c for c in reserved if c not in spent])
        if attempted:
            try:
                after_live = _live_chats(devtools.pages(HTTP_TIMEOUT_SECONDS))
                after_signature = inventory_signature(pid, after_live, enrolled_urls)
            except (StartupError, ValueError):
                # Unknown inventory: keep the planning signature. Spent chats are not
                # open tabs, so they must never be added to it.
                after_signature = signature
            for label in attempted:
                ledger.record_restore(
                    "https://chatgpt.com/c/" + label,
                    now,
                    "uncertain" if label == outcome.get("uncertain") else "opened",
                    after_signature,
                )

    if permitted and plan["autostart"]:
        try:
            systemd.start(unit)
            ledger.record_autostart(now, "started")
            effects["autostart"] = {"outcome": "started"}
        except ServiceError as exc:
            ledger.record_autostart(now, "failed")
            effects["autostart"] = {
                "outcome": "failed",
                "detail": clean_detail(str(exc)),
            }
            block("autostart", str(exc))
    elif permitted and service == "active":
        ledger.reset_autostart()

    # --- alerts (conditions evaluated after the effects of this run) --------
    covered = {r["chat"] for r in rows if r["enrolled"]} | enrolled_now
    conditions: list[Alert] = []
    if unit_failed:
        conditions.append(
            Alert(
                "service:" + unit,
                "service-failed",
                "сервис восстановления " + unit + " в состоянии failed",
            )
        )
    if effects["autostart"] and effects["autostart"]["outcome"] == "failed":
        conditions.append(
            Alert(
                "autostart-failed:" + unit,
                "service-failed",
                "автозапуск " + unit + " не удался",
            )
        )
    elif plan.get("autostart_skipped") == "attempt-cap-reached":
        conditions.append(
            Alert(
                "autostart-exhausted:" + unit,
                "service-failed",
                "автозапуск " + unit + " исчерпан, нужно вмешательство владельца",
            )
        )
    if "stop:*" in controls:
        hold = clean_detail(controls["stop:*"]) or "stopped"
        conditions.append(
            Alert(
                "hold:global:" + hold,
                "recovery-hold",
                "восстановление остановлено глобальной паузой «"
                + hold
                + "»: Guardian ничего не отправляет и не возобновляет, снять паузу может только владелец",
            )
        )
    if live is None:
        conditions.append(
            Alert(
                "browser:account",
                "missing-pages",
                "браузер аккаунта недоступен или не подтверждён, наличие вкладок неизвестно",
            )
        )
    for r in rows:
        label = r["chat"]
        short = label[:8]
        if r["enrolled"] and r["live_pages"] == 0 and label not in opened_now:
            conditions.append(
                Alert(
                    "missing:" + label,
                    "missing-pages",
                    "нет открытой вкладки чата " + short + " (под наблюдением)",
                )
            )
        elif r["enrolled"] and (r["live_pages"] or 0) > 1:
            conditions.append(
                Alert(
                    "duplicate:" + label,
                    "missing-pages",
                    "несколько вкладок одного чата "
                    + short
                    + ", наблюдение неоднозначно",
                )
            )
    if not snapshot_fresh:
        conditions.append(
            Alert(
                "stale:account",
                "stale-observation",
                "наблюдение устарело: нет свежего снимка монитора",
            )
        )
    else:
        for r in rows:
            label = r["chat"]
            recent = restore_rows.get(url[label], {}).get("at", 0)
            if (
                r["enrolled"]
                and r["present"]
                and not r["fresh_chat"]
                and label not in opened_now
                and now - recent >= RESTORED_GRACE_SECONDS
            ):
                conditions.append(
                    Alert(
                        "stale:" + label,
                        "stale-observation",
                        "наблюдение за чатом " + label[:8] + " устарело",
                    )
                )
    for r in rows:
        label = r["chat"]
        session = sessions.get(url[label], {})
        if r["active"] and label not in covered and not r["manually_unwatched"]:
            what = "идёт генерация" if r["activity"] == "running" else "Thinking failed"
            conditions.append(
                Alert(
                    "uncovered:"
                    + ":".join((label, str(session.get("turn", "")), r["activity"])),
                    "uncovered-active",
                    "активный чат "
                    + label[:8]
                    + " ("
                    + what
                    + ") не под наблюдением Guardian",
                )
            )
        if r["stalled"]:
            conditions.append(
                Alert(
                    "stalled:" + label + ":" + str(session.get("last_progress_at")),
                    "stalled-generation",
                    "чат "
                    + label[:8]
                    + ": генерация без видимого прогресса более "
                    + str(STALLED_MINUTES)
                    + " мин",
                )
            )

    active_keys = {a.key for a in conditions}
    to_send: list[Alert] = []
    rearm: set[str] = set()
    for alert in conditions:
        row = alert_rows.get(alert.key)
        if row is None:
            to_send.append(alert)
        elif row["status"] == "sent":
            if row["cleared_at"] is not None:
                if now - row["cleared_at"] >= ALERT_REARM_SECONDS:
                    to_send.append(alert)
                else:
                    rearm.add(alert.key)
        elif (
            row["attempts"] < ALERT_MAX_DELIVERY_ATTEMPTS
            and now - row["last_at"] >= ALERT_RETRY_SECONDS
        ):
            to_send.append(alert)
    plan["alerts"] = [a.key for a in to_send]
    outcomes: dict[str, tuple[str, str, str, int]] = {}
    if permitted and to_send:
        try:
            delivery = notify(_compose(workspace, to_send))
        except (OSError, subprocess.SubprocessError) as exc:
            delivery = Delivery(
                False, clean_detail(type(exc).__name__ + ": " + str(exc))
            )
        status = "sent" if delivery.ok else "delivery-failed"
        for alert in to_send:
            previous = alert_rows.get(alert.key)
            attempts_made = (
                previous["attempts"] + 1
                if previous and previous["status"] == "delivery-failed"
                else 1
            )
            outcomes[alert.key] = (alert.kind, status, delivery.detail, attempts_made)
            effects["alerts"].append(
                {"key": alert.key, "kind": alert.kind, "status": status}
            )
    if permitted and (outcomes or alert_rows):
        ledger.settle_alerts(now, active_keys, rearm, outcomes)
    return report


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cdp-port",
        type=_cdp_port,
        required=True,
        metavar="PORT",
        help="Loopback DevTools port of the owned browser; must equal its "
        "--remote-debugging-port (authoritative, no default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the effects the account's coverage flags allow (default: dry run)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Per-account coverage flags JSON (default: "
        "~/.config/agent-tools/chatgpt-guardian/<workspace>.coverage.json)",
    )
    parser.add_argument(
        "--reset-restore",
        action="store_true",
        help="Owner action with --apply: forget spent restore attempts so each missing "
        "chat gets one new attempt",
    )


def command(
    args,
    *,
    processes: Processes | None = None,
    devtools: DevTools | None = None,
    systemd: Systemd | None = None,
    notify: Notifier | None = None,
) -> int:
    """Entry from the Guardian CLI, before the mutable recovery Store is constructed."""
    if args.reset_restore and not args.apply:
        raise ValueError("--reset-restore requires --apply")
    config = load_config(args.workspace, args.config)
    report = reconcile(
        args.workspace,
        state=args.state,
        port=args.cdp_port,
        apply=args.apply,
        config=config,
        processes=processes or ProcFilesystem(),
        devtools=devtools or ChromeDevTools(args.cdp_port),
        systemd=systemd or SystemdUser(),
        notify=notify or tg_notifier,
        now=wall_clock(),
        reset_restore=args.reset_restore,
    )
    if report["busy"] and args.reset_restore:
        report["blocked"].append(
            {
                "scope": "reset-restore",
                "reason": "account lock is busy: nothing was reset, run it again",
            }
        )
    report["ok"] = not report["blocked"]
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if report["ok"] else 1
