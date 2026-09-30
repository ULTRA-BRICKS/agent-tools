"""The inbox key derivation + the consuming reader (see the package docstring)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_NAME_MAX = 64
PENDING_FILE = "pending.jsonl"
# One COMPLETE file per consumption (temp + rename), never a shared append target: the
# daemon claims/unlinks what it reads, and an append into a file that was just unlinked
# would land on a dead inode. `delivered-<pid>-<ns>-<rnd>.jsonl`; the in-flight temp file
# carries a DIFFERENT prefix so no `delivered-*` glob on the daemon side can ever see a
# half-written batch.
DELIVERED_PREFIX = "delivered-"
TMP_PREFIX = "tmp-"
DIR_MODE = 0o700
FILE_MODE = 0o600


def _debug_log_path() -> Path:
    """File log for inbox diagnostics, overridable for tests."""
    override = os.environ.get("HOOKS_DEBUG_LOG")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.cache/agent-tools/hooks-debug.log"))


def _warn(msg: str) -> None:
    """File-only diagnostics: bridges must never write to stderr (TUI noise in
    every harness). Decisions travel via stdout JSON; this lands in the file log."""
    try:
        path = _debug_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"tg-inbox: {msg}\n")
    except OSError:
        pass


# --- key derivation (the shared contract) ---


def sanitize_agent_name(name: str) -> str | None:
    """Filesystem-safe key segment for a ``--name`` value; ``None`` when nothing is left.

    ``.`` and ``-`` are allowed characters, so a value made only of dots (``.``, ``..``)
    would otherwise survive and resolve ``inbox/<key>`` to the inbox root or its PARENT —
    rejected here, the single gate between a user-typed value and a path (review finding).
    """
    safe = _SAFE_NAME.sub("_", name)[:_NAME_MAX]
    if not safe or set(safe) == {"."}:
        return None
    return safe


def _normalize_cwd(cwd: str) -> str:
    stripped = cwd.rstrip("/")
    return stripped or "/"


def agent_key(name: str | None, cwd: str) -> str:
    """THE shared key: the sanitized ``--name``, else ``cwd-<sha256(cwd)[:16]>``."""
    safe = sanitize_agent_name(name) if name else None
    if safe:
        return safe
    digest = hashlib.sha256(_normalize_cwd(cwd).encode("utf-8")).hexdigest()
    return f"cwd-{digest[:16]}"


# --- the inbox on disk ---


def inbox_root() -> Path:
    override = os.environ.get("TG_CTL_CONFIG_DIR")
    base = Path(override) if override else Path(os.path.expanduser("~/.config/tg-cli"))
    return base / "inbox"


def inbox_dir(key: str) -> Path:
    return inbox_root() / key


def _parse_lines(text: str, *, stamp: str, session_id: str) -> tuple[list[dict], list[str], int]:
    """One pass over the JSONL: ``(entries to deliver, archive lines, malformed count)``.

    A valid entry is an object with a non-blank string ``wrapped``; it is archived stamped
    with ``delivered_ts``/``session_id``. Anything else (a blank ``wrapped`` included — it
    would be consumed yet deliver nothing) is archived flagged ``malformed`` (never
    silently dropped) and not delivered.
    """
    entries: list[dict] = []
    archived: list[str] = []
    malformed = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("wrapped"), str) and obj["wrapped"].strip():
            entries.append(obj)
            record = dict(obj, delivered_ts=stamp, session_id=session_id)
        else:
            malformed += 1
            record = {"malformed": True, "raw": line, "delivered_ts": stamp, "session_id": session_id}
        archived.append(json.dumps(record, ensure_ascii=False))
    return entries, archived, malformed


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _requeue_late_appends(claim: Path, pending: Path, consumed: int) -> None:
    """Close the writer/claim race: the daemon appends with open-then-write, so a claim
    rename between those two syscalls lands its record on the CLAIMED inode — after
    this reader took its snapshot. Once the batch is published, re-read the claim: any
    bytes past the snapshot go back to a fresh ``pending.jsonl`` (picked up at the next
    Stop) instead of dying with the claim. Raises ``OSError`` when the requeue fails;
    the caller then keeps the claim on disk (nothing is lost).
    """
    tail = claim.read_bytes()[consumed:]
    if not tail:
        return
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "wb") as fh:
        fh.write(tail)
    _warn(f"{len(tail)} byte(s) reached {claim.name} after the snapshot — requeued to {pending.name}")


def _unique_name(prefix: str) -> str:
    return f"{prefix}{os.getpid()}-{time.time_ns()}-{os.urandom(4).hex()}.jsonl"


def consume_pending(key: str, *, session_id: str = "") -> list[dict]:
    """Claim + archive the agent's pending inbox; return the entries to hand over.

    CLAIM by rename (atomic): the daemon's appends after that land in a fresh
    ``pending.jsonl`` and are picked up at the next Stop. The claim name is unique
    (pid + nanoseconds + random), so a stale claim left by an earlier failure can never
    be overwritten by a later rename (review finding). The claimed records are published
    as ONE complete ``delivered-<pid>-<ns>-<rnd>.jsonl`` (written to a ``tmp-…`` file, then
    renamed — the daemon only ever sees whole batches) and the claim is removed — all
    BEFORE the caller emits the block, so a message is never delivered twice.
    Any problem → ``[]`` with a file-log line (fail-open: the stop proceeds normally).
    """
    directory = inbox_dir(key)
    pending = directory / PENDING_FILE
    if not pending.exists():
        return []
    claim = directory / _unique_name("claim-")
    try:
        os.rename(pending, claim)
    except OSError as exc:
        _warn(f"could not claim {pending}: {exc}")
        return []
    try:
        snapshot = claim.read_bytes()
        text = snapshot.decode("utf-8")
    except (OSError, ValueError) as exc:  # ValueError: invalid UTF-8 — the claim stays on disk
        _warn(f"could not read {claim}: {exc}")
        return []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries, archived, malformed = _parse_lines(text, stamp=stamp, session_id=session_id)
    if malformed:
        _warn(f"{malformed} malformed line(s) in {pending} — archived, not delivered")
    if not archived:
        try:
            claim.unlink()
        except OSError:
            pass
        return []
    batch = directory / _unique_name(DELIVERED_PREFIX)
    tmp = directory / _unique_name(TMP_PREFIX)
    try:
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            pass
        _write_private(tmp, "".join(f"{line}\n" for line in archived))
        os.rename(tmp, batch)
    except OSError as exc:
        # Archive failed: do NOT deliver (the claim file keeps the records on disk for a
        # human to inspect) — delivering without a record could re-deliver on retry.
        _warn(f"could not publish {batch}: {exc} — left {claim}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return []
    _finish_claim(claim, pending, len(snapshot))
    return entries


def _finish_claim(claim: Path, pending: Path, consumed: int) -> None:
    """The batch is published, so the entries ARE delivered; the claim is now only a
    carrier for a late append. Requeue that, then drop the claim — or leave it on disk
    (nothing lost, said in the file log) if either step fails."""
    try:
        _requeue_late_appends(claim, pending, consumed)
        claim.unlink()
    except OSError as exc:
        _warn(f"could not requeue/remove {claim.name}: {exc} — left on disk")


def format_block_reason(entries: list[dict]) -> str:
    """The Stop block reason: the pre-wrapped texts, oldest first, blank-line separated."""
    return "\n\n".join(e["wrapped"] for e in entries)


def combine_stop_parts(inbox_text: str, hook_reason: str | None) -> str | None:
    """The ONE combining rule the omp bridge applies on yield→stop: the inbox messages
    first, a blocking hook's reason after, blank-line separated; ``None`` when neither
    blocks. A blocking hook with an EMPTY reason still blocks (``is not None``, never
    truthiness) — a fail-closed gate that died without a message must not become an
    allow."""
    if not inbox_text and hook_reason is None:
        return None
    parts = [inbox_text] if inbox_text else []
    if hook_reason is not None:
        parts.append(hook_reason)
    return "\n\n".join(parts)


def stop_inbox_text(key_fn: Callable[[], str], *, session_id: str, warn: Callable[[str], None]) -> str:
    """The fail-open Stop-side wrapper (``omp_hook_bridge`` on yield): the pending messages
    for this agent, consumed (at-most-once) and rendered as block-reason text, or ``""``.

    ``key_fn`` is called LAZILY, only when an inbox root exists at all, so a machine
    without tg-cli pays nothing on every yield. Any failure is one ``warn`` line and ``""``
    (never blocks).
    """
    try:
        if not inbox_root().exists():
            return ""
        key = key_fn()
        entries = consume_pending(key, session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - fail-open: a broken inbox never wedges the agent
        warn(f"tg inbox check failed, skipping (fail-open): {exc}")
        return ""
    if not entries:
        return ""
    warn(f"delivering {len(entries)} queued Telegram message(s) from the tg-ctl inbox ({key})")
    return format_block_reason(entries)
