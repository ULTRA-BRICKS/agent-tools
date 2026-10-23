"""Read-only Guardian evidence projection; telemetry never writes recovery state.

The independent telemetry database has one current snapshot and deduplicated events.
Send acknowledgements, observed assistant starts and observed completions are distinct.
All '*observed_at' timestamps mean when evidence was seen, not inferred execution time.
A session is only `running` while its own last DOM check is fresh; otherwise `running`
is null and the last good observation stays visible as `last_observed_*` history.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

SCHEMA_VERSION = 1
MONITOR_STALE_SECONDS = 180
GUARDIAN_STALE_SECONDS = 660
STALL_SECONDS = 900
# Slack after a session's scheduled re-check before its evidence is called stale.
OBSERVATION_GRACE_SECONDS = 120
DEFAULT_RETRY_SECONDS = 60
DETAIL_LIMIT = 120

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_TOKEN = re.compile(r"[A-Za-z0-9_+/=-]{40,}")


def read_source(path: Path, workspace: str) -> tuple[dict, list[dict]]:
    """Validate account and read existing recovery state without Store initialization."""
    path = Path(path).expanduser().absolute()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT account FROM account_scope").fetchall() != [(workspace,)]:
            raise ValueError("Guardian source account does not match workspace")
        controls = dict(db.execute("SELECT key,value FROM controls"))
        # 'canceled' attempts were reserved (budget consumed) but never clicked; reporting
        # them would present a refused continuation as a send-uncertain attempt.
        rows = db.execute(
            "SELECT chat,turn,generation,wall,state FROM attempts WHERE state!='canceled' ORDER BY wall DESC LIMIT 5000"
        ).fetchall()
        try:
            # Sends made on a human approval are real attempts; label them, never relabel them.
            operator = set(
                db.execute(
                    "SELECT chat,turn FROM continuations "
                    "WHERE state IN ('dispatching','sent','uncertain')"
                )
            )
        except sqlite3.OperationalError:  # state written before continuations existed
            operator = set()
    attempts = [
        dict(zip(("chat", "turn", "generation", "wall", "state"), r)) for r in rows
    ]
    for attempt in attempts:
        if (attempt["chat"], attempt["turn"]) in operator:
            attempt["source"] = "operator"
    return controls, attempts


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def clean_detail(value) -> str:
    """Bounded one-line diagnostic: no URLs, ids, tokens, control characters or page text."""
    if not isinstance(value, str):
        return ""
    lines = value.strip().splitlines()
    text = _URL.sub("<url>", lines[0]) if lines else ""
    text = _TOKEN.sub("<id>", _UUID.sub("<id>", text))
    text = "".join(c if " " <= c <= "~" else " " for c in text)
    return " ".join(text.split())[:DETAIL_LIMIT].rstrip()


def _session_view(row: dict, now: float, snapshot_stale: bool) -> dict:
    """Per-session freshness, independent of the account snapshot's own age."""
    row = dict(row)
    checked = row.get("last_checked_at")
    age = max(0, now - checked) if _number(checked) else None
    backoff = row.get("backoff_seconds")
    limit = (backoff if _number(backoff) else DEFAULT_RETRY_SECONDS) + (
        OBSERVATION_GRACE_SECONDS
    )
    check_stale = age is None or age > limit
    observation = row.get("observation") or ("missing" if age is None else "current")
    if observation == "current" and (check_stale or snapshot_stale):
        observation = "stale"
    if observation != "current":
        row.update(running=None, status="unknown")
    row.update(
        observation=observation,
        check_age_seconds=age,
        check_stale=check_stale,
        session_stale=observation != "current",
    )
    return row


def freshness(snapshot: dict, now: float) -> dict:
    """Recompute age at read time without making old evidence look freshly checked."""
    snap = dict(snapshot)
    generated = snap.get("generated_at")
    age = max(0, now - generated) if _number(generated) else None
    heartbeat = snap.get("heartbeat_at")
    hb_age = max(0, now - heartbeat) if _number(heartbeat) else None
    snap.update(
        read_at=now,
        snapshot_age_seconds=age,
        heartbeat_age_seconds=hb_age,
        stale=bool(
            snap.get("stale")
            or age is None
            or age > MONITOR_STALE_SECONDS
            or (_number(generated) and generated > now + 30)
        ),
        guardian_heartbeat_stale=bool(
            hb_age is None
            or hb_age > GUARDIAN_STALE_SECONDS
            or (_number(heartbeat) and heartbeat > now + 30)
        ),
    )
    if isinstance(snap.get("sessions"), list):
        sessions = [_session_view(s, now, snap["stale"]) for s in snap["sessions"]]
        snap["sessions"] = sessions
        snap["summary"] = dict(
            snap.get("summary", {}),
            running=sum(s["running"] is True for s in sessions),
            running_unknown=sum(
                bool(s.get("watched")) and s["running"] is None for s in sessions
            ),
        )
    return snap


def _identity(attempt: dict | None) -> str:
    if not attempt:
        return ""
    return json.dumps(
        [attempt.get(k) for k in ("chat", "turn", "generation", "wall")],
        separators=(",", ":"),
    )


def _response_turn(attempt: dict | None, sample: dict) -> dict | None:
    """The turn immediately after the failed turn if it is the exact go message."""
    if not attempt or attempt["state"] != "sent":
        return None
    turns = sample.get("turns", [])
    keys = [t.get("id") for t in turns]
    if not keys or len(keys) != len(set(keys)):
        return None
    try:
        index = keys.index(attempt["turn"])
    except ValueError:
        return None
    if index + 1 >= len(turns):
        return None
    response = turns[index + 1]
    return response if response.get("user_go") else None


def _generating(response: dict, sample: dict) -> bool:
    """Ongoing generation for this exact response, never for another turn.

    The page-wide running/Stop signals describe the newest turn only, so they bind to
    the response only while it is that newest turn.
    """
    if (
        response.get("generation_active")
        or response.get("assistant_state") == "in_progress"
    ):
        return True
    turns = sample.get("turns", [])
    newest = bool(turns) and turns[-1].get("id") == response.get("id")
    return newest and bool(sample.get("running") or sample.get("stop_visible"))


def _continuation(attempt: dict | None, sample: dict) -> tuple[str, str]:
    """Failed turn, then the next exact go message, then an assistant side on that turn.

    A running turn or a Stop button alone never links to a send: the sent turn must be
    followed by the go message and the assistant evidence must belong to that same turn.
    A response is only complete when nothing shows it generating; otherwise it runs.
    """
    if not attempt:
        return "none", ""
    if attempt["state"] in ("uncertain", "reserved"):
        return "uncertain", ""
    if attempt["state"] != "sent":
        return "unconfirmed", ""
    response = _response_turn(attempt, sample)
    if response is None:
        return "unconfirmed", ""
    started = response.get("assistant_present") or response.get("assistant_started")
    state = response.get("assistant_state")
    if state == "complete" and _generating(response, sample):
        # A transient complete marker cannot outvote visible ongoing generation.
        return ("running", response["id"]) if started else ("unconfirmed", "")
    if state == "complete" and response.get("assistant_present"):
        return "completed", response["id"]
    if state == "in_progress" and started:
        return "running", response["id"]
    return "unconfirmed", ""


def _recovery(chat: str, controls: dict, attempts: list[dict]) -> dict:
    """What the recovery service would consider for this chat; read from its own records."""
    enrolled = controls.get("watch:" + chat) == "1"
    stop_key = "stop:*" if "stop:*" in controls else "stop:" + chat
    held = stop_key in controls
    hold = (controls.get(stop_key) or "stopped") if held else ""
    quota = "astra_quota" in controls
    unresolved = any(
        a["chat"] == chat and a["state"] in ("uncertain", "reserved") for a in attempts
    )
    blockers = [
        name
        for name, active in (
            ("not-enrolled", not enrolled),
            ("hold", held),
            # Blocks Astra; Sol fallback has its own consent record, not read here.
            ("quota", quota),
            ("unresolved-send", unresolved),
        )
        if active
    ]
    return {
        "enrolled": enrolled,
        "enabled": not blockers,
        "blockers": blockers,
        "hold": hold,
        "hold_scope": ("global" if stop_key == "stop:*" else "chat") if held else "",
        "draft": hold == "human draft",
        "challenge": hold == "human challenge",
        "quota_latched": quota,
        "unresolved_send": unresolved,
    }


def project_snapshot(
    workspace: str,
    controls: dict,
    attempts: list[dict],
    samples: list[dict],
    previous: dict | None,
    health: dict,
    now: float,
) -> dict:
    """Pure projection. Inputs are never mutated; no browser or recovery operations."""
    previous = previous or {}
    previous_rows = {s["chat"]: s for s in previous.get("sessions", [])}
    watched = {
        k[6:] for k, v in controls.items() if k.startswith("watch:") and v == "1"
    }
    known = {k[6:] for k in controls if k.startswith("watch:")}
    groups = defaultdict(list)
    for sample in samples:
        if sample.get("chat"):
            groups[sample["chat"]].append(sample)
    latest = {}
    for attempt in sorted(attempts, key=lambda a: a["wall"], reverse=True):
        latest.setdefault(attempt["chat"], attempt)
    connection = health.get("connection", "unknown")
    coverage_known = connection == "connected"
    interval = health.get("interval_seconds")
    retry = interval if _number(interval) and interval > 0 else DEFAULT_RETRY_SECONDS
    events = []
    rows = []

    def event(kind: str, chat: str = "", identity: str = "", **details):
        key = json.dumps([workspace, kind, chat, identity], sort_keys=True)
        events.append(
            {
                "id": hashlib.sha256(key.encode()).hexdigest()[:24],
                "kind": kind,
                "chat": chat,
                "at": now,
                "baseline": not bool(previous),
                **details,
            }
        )

    for chat in sorted(watched | known | set(groups) | set(latest)):
        old = previous_rows.get(chat, {})
        matches = groups.get(chat, [])
        sample = matches[0] if len(matches) == 1 else {}
        present = bool(matches) if coverage_known else old.get("present", False)
        error = clean_detail(sample.get("error"))
        detail = clean_detail(sample.get("error_detail"))
        conversation = sample.get("conversation_id")
        if (
            sample
            and not error
            and isinstance(conversation, str)
            and conversation != chat.rsplit("/", 1)[-1]
        ):
            # The verified DOM root belongs to another conversation than this page URL.
            error, detail = "conversation-root-unverified", "conversation-id-mismatch"
        checked = sample.get("checked_at")
        valid_check = bool(sample and not error and _number(checked))
        data = sample if valid_check else {}
        fresh_check = valid_check and checked > (old.get("last_checked_at") or 0)
        attempt = latest.get(chat)
        attempt_id = _identity(attempt)
        same_attempt = bool(attempt_id and attempt_id == old.get("attempt_id"))
        old_status = old.get("last_observed_status", old.get("status", "unknown"))
        old_running = old.get("last_observed_running", old.get("running"))
        observed_status = data.get("status", old_status)
        observed_running = bool(data.get("running")) if valid_check else old_running
        token = data.get("progress_token", old.get("progress_token", ""))
        changed = valid_check and (
            token != old.get("progress_token") or observed_status != old_status
        )
        progress_at = checked if changed else old.get("last_progress_at")
        backoff = old.get("backoff_seconds", 60)
        if fresh_check:
            backoff = (
                60 if changed or observed_running else min(300, max(60, backoff * 2))
            )
        # Cached samples are only evidence for as long as the observer would trust them.
        current = bool(
            coverage_known
            and valid_check
            and max(0, now - checked) <= backoff + OBSERVATION_GRACE_SECONDS
        )
        if not coverage_known:
            observation = "unavailable"
            detail = clean_detail(health.get("error_detail")) or (
                "browser-observation-unavailable"
            )
        elif not matches:
            observation, detail = "missing", "no-page-for-url"
        elif len(matches) != 1:
            observation, detail = "duplicate", "multiple-pages-for-url"
        elif error:
            observation, detail = "error", detail or error
        elif not current:
            observation, detail = "stale", ""
        else:
            observation, detail = "current", ""
        view = data if current else {}
        continuation, response_turn = _continuation(attempt, view)
        old_completed = old.get("completion_observed_at") if same_attempt else None
        retracted = False
        if old_completed is not None and view:
            # Only the exact response that was called complete can contradict it.
            response = _response_turn(attempt, view)
            retracted = bool(
                response
                and response.get("id") == old.get("continuation_turn")
                and _generating(response, view)
            )
        fresh_continuation = continuation in ("running", "completed")
        continuation_current = fresh_continuation
        if same_attempt and old.get("continuation") == "completed" and not retracted:
            # Completed evidence is a past fact; it never becomes current again by itself.
            continuation_current = continuation == "completed"
            if not continuation_current:
                continuation, response_turn = (
                    "completed",
                    old.get("continuation_turn", ""),
                )
        reason = ""
        if not coverage_known:
            reason = "browser-observation-unavailable"
        elif not matches:
            reason = "missing-page"
        elif len(matches) != 1:
            reason = "duplicate-pages"
        elif error:
            reason = "observation-error"
        if controls.get("stop:*") or controls.get("stop:" + chat):
            reason = controls.get("stop:*") or controls["stop:" + chat]
        elif attempt and attempt["state"] in ("uncertain", "reserved"):
            reason = "unresolved-send"
        elif controls.get("astra_quota"):
            reason = "Astra quota is latched"
        elif chat not in watched and not reason:
            reason = "not-enrolled"
        stalled = bool(
            current
            and observed_running
            and progress_at is not None
            and checked - progress_at >= STALL_SECONDS
        )
        if fresh_check:
            next_check = checked + backoff
        elif current:
            next_check = old.get("next_check_at")
        else:
            next_check = now + retry
        if next_check is None or next_check <= now:
            next_check = now + retry
        old_confirmed = old.get("continuation_observed_at") if same_attempt else None
        confirmed_at, completed_at = old_confirmed, old_completed
        # Diagnostics of the last retracted completion; the proof itself is cleared.
        invalidated_at = old.get("completion_invalidated_at") if same_attempt else None
        invalidated_from = (
            old.get("completion_invalidated_observed_at") if same_attempt else None
        )
        if retracted:
            invalidated_at, invalidated_from = checked, old_completed
            completed_at = None
        if fresh_continuation and confirmed_at is None:
            confirmed_at = checked
        if (
            continuation == "completed"
            and continuation_current
            and completed_at is None
        ):
            completed_at = checked
        row = {
            "chat": chat,
            "title": sample.get("title") or old.get("title") or chat.rsplit("/", 1)[-1],
            "watched": chat in watched,
            "present": present,
            "page_count": len(matches),
            "observation": observation,
            "error": error if observation == "error" else "",
            "error_detail": detail,
            "status": observed_status if current else "unknown",
            "failure_evidence": clean_detail(data.get("failure_evidence"))
            if current
            else "",
            "reason": reason,
            "running": observed_running if current else None,
            "last_observed_status": observed_status,
            "last_observed_running": observed_running,
            "last_checked_at": checked if valid_check else old.get("last_checked_at"),
            "last_error_at": now
            if sample.get("error") or error
            else old.get("last_error_at"),
            "last_go_at": attempt["wall"] if attempt else None,
            "last_go_state": attempt["state"] if attempt else None,
            "last_go_source": attempt.get("source", "automatic") if attempt else None,
            "attempt_id": attempt_id,
            "continuation": continuation,
            "continuation_current": continuation_current,
            "continuation_turn": response_turn,
            "continuation_observed_at": confirmed_at,
            "completion_observed_at": completed_at,
            "completion_invalidated_at": invalidated_at,
            "completion_invalidated_observed_at": invalidated_from,
            "last_progress_at": progress_at,
            "progress_token": token,
            "no_visible_progress": stalled,
            "next_check_at": next_check,
            "backoff_seconds": backoff,
            "model": data.get("model", ""),
            "effort": data.get("effort", ""),
            "model_verified": False,
            "turn": data.get("turn", old.get("turn", "")),
            "recovery": _recovery(chat, controls, attempts),
        }
        rows.append(row)
        if attempt and (attempt_id, attempt["state"]) != (
            old.get("attempt_id"),
            old.get("last_go_state"),
        ):
            kind = (
                "send-acknowledged" if attempt["state"] == "sent" else "send-uncertain"
            )
            event(
                kind,
                chat,
                attempt_id + attempt["state"],
                send_at=attempt["wall"],
                state=attempt["state"],
                source=attempt.get("source", "automatic"),
            )
        if retracted:
            # Audit trail: the earlier completion event stays; this one supersedes it.
            event(
                "completion-invalidated",
                chat,
                json.dumps([attempt_id, old.get("continuation_turn"), old_completed]),
                send_at=attempt["wall"],
                response_turn=old.get("continuation_turn", ""),
                attempt_id=attempt_id,
                invalidated_completion_at=old_completed,
            )
        if continuation_current and (
            (continuation == "running" and old_confirmed is None)
            or (continuation == "completed" and old_completed is None)
        ):
            # A completion after a retraction is a new fact, so it gets its own id.
            event(
                "continuation-" + continuation,
                chat,
                attempt_id
                + response_turn
                + (str(checked) if continuation == "completed" else ""),
                send_at=attempt["wall"],
                response_turn=response_turn,
                attempt_id=attempt_id,
            )
        if observed_status == "Thinking failed" and (
            observed_status,
            row["turn"],
        ) != (old_status, old.get("turn")):
            event("thinking-failed", chat, row["turn"])
        if observed_status == "quota-or-rate-limit" and old_status != observed_status:
            event("rate-limit-observed", chat, json.dumps([row["turn"], now]))
        if stalled and not old.get("no_visible_progress"):
            event("no-visible-progress", chat, str(progress_at), since=progress_at)
        if reason != old.get("reason", "") and (
            reason != "not-enrolled" or old.get("reason")
        ):
            event("restriction-changed", chat, json.dumps([reason, now]), reason=reason)
    if connection != previous.get("connection"):
        event(
            "connection-changed",
            identity=json.dumps([connection, now]),
            connection=connection,
        )
    if health.get("service", {}).get("active") != previous.get("service", {}).get(
        "active"
    ):
        event(
            "service-state-changed",
            identity=json.dumps([health.get("service"), now]),
            service=health.get("service", {}),
        )
    summary = {
        "enrolled": len(watched),
        "present": sum(
            s["watched"] and s["present"] and s["page_count"] == 1 for s in rows
        ),
        "available": len(groups) if coverage_known else None,
        "unwatched": sum(not s["watched"] and s["present"] for s in rows),
        "running": sum(s["running"] is True for s in rows),
        "blocked": sum(s["watched"] and bool(s["reason"]) for s in rows),
        "sent_24h": sum(
            a["state"] == "sent" and now - 86400 <= a["wall"] <= now for a in attempts
        ),
        "resumed_24h": sum(
            s["continuation_observed_at"] is not None
            and s["last_go_at"] is not None
            and now - 86400 <= s["last_go_at"] <= now
            for s in rows
        ),
        "completed_24h": sum(
            s["completion_observed_at"] is not None
            and s["last_go_at"] is not None
            and now - 86400 <= s["last_go_at"] <= now
            for s in rows
        ),
    }
    snap = {
        "schema_version": SCHEMA_VERSION,
        "workspace": workspace,
        "generated_at": now,
        "heartbeat_at": health.get("heartbeat_at"),
        "connection": connection,
        "error_detail": clean_detail(health.get("error_detail")),
        "stale": not coverage_known,
        "max_pages": health.get("max_pages", 6),
        "page_count": health.get("page_count"),
        "interval_seconds": retry,
        "service": health.get("service", {}),
        "guardian_last_state": health.get("guardian_last_state", {}),
        "service_checked_at": health.get("service_checked_at"),
        "coverage": "dedicated-browser-open-chats",
        "coverage_note": "Open chats in the configured dedicated browser only; not all account sessions.",
        "effective_controls": {
            "global_stop": (controls.get("stop:*") or "stopped")
            if "stop:*" in controls
            else "",
            "quota_latched": "astra_quota" in controls,
        },
        "summary": summary,
        "sessions": rows,
        "events": events,
        "limitations": list(health.get("limitations", [])),
    }
    if len(attempts) >= 5000:
        snap["limitations"].append("Attempt history capped at 5000 entries.")
    return freshness(snap, now)


class MonitorStore:
    """Separate telemetry database. Construction and reads never create files."""

    def __init__(self, source: Path, workspace: str):
        self.source = Path(source).expanduser().absolute()
        self.path = self.source.with_name(self.source.stem + ".monitor.sqlite")
        self.workspace = workspace

    def record(self, snapshot: dict) -> None:
        if (
            snapshot.get("workspace") != self.workspace
            or snapshot.get("schema_version") != SCHEMA_VERSION
        ):
            raise ValueError("Telemetry workspace/schema mismatch")
        if self.path.is_symlink():
            raise ValueError("Telemetry database must not be a symlink")
        read_source(self.source, self.workspace)
        import os

        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with sqlite3.connect(self.path, timeout=2) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS scope(workspace TEXT PRIMARY KEY)")
            scope = db.execute("SELECT workspace FROM scope").fetchall()
            if scope and scope != [(self.workspace,)]:
                raise ValueError("Telemetry account mismatch")
            db.execute("INSERT OR IGNORE INTO scope VALUES (?)", (self.workspace,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS snapshot(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            for event in snapshot.get("events", []):
                db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?)",
                    (event["id"], event["at"], json.dumps(event)),
                )
            payload = dict(snapshot, events=[])
            db.execute(
                "INSERT OR REPLACE INTO snapshot VALUES (1,?)",
                (json.dumps(payload, allow_nan=False),),
            )
            db.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY at DESC LIMIT -1 OFFSET 10000)"
            )

    def read(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not self.path.exists():
            controls, attempts = read_source(self.source, self.workspace)
            snapshot = project_snapshot(
                self.workspace,
                controls,
                attempts,
                [],
                None,
                {
                    "connection": "monitoring-unavailable",
                    "limitations": [
                        "No monitor evidence has been recorded. Source controls are available; session progress is unknown."
                    ],
                },
                now,
            )
            snapshot.update(generated_at=None, events=[])
            return freshness(snapshot, now)
        with sqlite3.connect(
            self.path.as_uri() + "?mode=ro", uri=True, timeout=2
        ) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT workspace FROM scope").fetchall() != [
                (self.workspace,)
            ]:
                raise ValueError("Telemetry account mismatch")
            row = db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
            if not row:
                raise ValueError("Telemetry snapshot is unavailable")
            snapshot = json.loads(row[0])
            snapshot["events"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM events ORDER BY at DESC, id LIMIT 200"
                )
            ]
            confirmations = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM events WHERE at >= ? ORDER BY at, rowid",
                    (now - 86400,),
                )
            ]
        if (
            snapshot.get("schema_version") != SCHEMA_VERSION
            or snapshot.get("workspace") != self.workspace
        ):
            raise ValueError("Telemetry schema/account mismatch")
        starts = {
            e.get("attempt_id")
            for e in confirmations
            if e["kind"] in ("continuation-running", "continuation-completed")
            and e.get("send_at", 0) >= now - 86400
        }
        # Per attempt, the newest of completion / retraction decides; history stays stored.
        folded = {}
        for e in confirmations:
            if e.get("send_at", 0) >= now - 86400:
                if e["kind"] == "continuation-completed":
                    folded[e.get("attempt_id")] = True
                elif e["kind"] == "completion-invalidated":
                    folded[e.get("attempt_id")] = False
        complete = {attempt for attempt, done in folded.items() if done}
        snapshot["summary"]["resumed_24h"] = len(starts - {None})
        snapshot["summary"]["completed_24h"] = len(complete - {None})
        return freshness(snapshot, now)
