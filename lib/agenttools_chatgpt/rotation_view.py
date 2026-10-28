"""Single-tab telemetry for the existing `status` and `monitor --json` entrypoints.

Accessed via: cli.py (`status` adds a `rotation` block, `monitor` prints the monitor snapshot
annotated with it). Read-only: it validates the account's rotation file but never creates,
settles, repairs or rewrites it, so a read cannot move the patrol's due time.

Assumptions: the monitor's own projection (monitor.py) stays the source of per-chat evidence. In
single-tab mode only the patrol tab is open, so the projection reports every other enrolled chat
as a missing page. That is the expected queued state, so here, and only here, a chat that is
queued or paused (not the current one) is relabelled `queued`; the current chat without a page is
still a real defect and stays `missing`. Legacy accounts get no extra keys at all.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .monitor import OBSERVATIONS, MonitorStore, clean_detail, read_source
from .rotation import MODE_CONTROL, Rotation, RotationError, rotation_path


def block(
    state: Path,
    account: str,
    controls: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.time,
) -> dict | None:
    """The operator view, or None when this account never ran in single-tab mode."""
    active = controls.get(MODE_CONTROL) == "1"
    path = rotation_path(state)
    if not active and not path.exists() and not path.is_symlink():
        return None
    try:
        machine = Rotation.read(path, account, clock=clock)
    except RotationError as exc:
        return {
            "mode": "single-tab",
            "active": active,
            "error": "rotation-state-invalid",
            "detail": clean_detail(str(exc)),
        }
    if machine is None:
        return {
            "mode": "single-tab",
            "active": active,
            "account": account,
            "queue_size": 0,
            "current": None,
            "queue": [],
        }
    return {**machine.snapshot(controls), "active": active}


def _session(session: dict, row: dict | None) -> dict:
    session = dict(session)
    if row is None:
        session["rotation_state"] = "untracked"
        return session
    session["rotation_state"] = row["state"]
    if session.get("observation") == "missing" and not row["current"]:
        session["observation"] = "queued"
        session["error_detail"] = "queued-awaiting-visit"
        if session.get("reason") == "missing-page":
            session["reason"] = "queued"
    return session


def _summary(summary: dict, sessions: list[dict], top: dict) -> dict:
    watched = [s for s in sessions if s.get("watched")]
    counts = {
        kind: sum(s["observation"] == kind for s in watched)
        for kind in (*OBSERVATIONS, "queued")
    }
    # Queue membership is not observation: preserve the raw observer's coverage verdict.
    # Otherwise merely enrolling a never-visited chat changes false to a false-positive true.
    complete = (
        summary.get("coverage_complete") is True
        and top.get("connection") == "connected"
        and not top.get("stale")
        and all(s["observation"] == "current" for s in watched)
    )
    return {**summary, "observation_counts": counts, "coverage_complete": complete}


def annotate(snapshot: dict, view: dict | None) -> dict:
    """The monitor snapshot plus the rotation block; the input is never mutated."""
    if view is None:
        return snapshot
    result = dict(snapshot, rotation=view)
    if not view.get("active") or "queue" not in view:
        return result
    rows = {row["chat"]: row for row in view["queue"]}
    sessions = [_session(s, rows.get(s["chat"])) for s in snapshot.get("sessions", [])]
    result["sessions"] = sessions
    result["summary"] = _summary(snapshot.get("summary", {}), sessions, snapshot)
    return result


def monitor_command(args) -> int:
    """`chatgpt-guardian monitor`: the persisted telemetry, annotated, as JSON."""
    controls, _ = read_source(args.state, args.workspace)
    snapshot = MonitorStore(args.state, args.workspace).read()
    view = block(args.state, args.workspace, controls)
    print(json.dumps(annotate(snapshot, view), ensure_ascii=False, allow_nan=False))
    return 0
