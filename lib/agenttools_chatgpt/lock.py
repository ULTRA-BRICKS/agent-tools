"""Per-account advisory lock serialising every Guardian effect that opens browser tabs.

``coverage --apply`` and ``startup --apply`` both create tabs for missing enrolled
chats; two of them at once would each see the chat missing and open it twice. Both
take this ``flock`` for their whole applied run. The kernel drops it with the
process, so a crash or timeout never leaves a stale lock behind.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

POLL_SECONDS = 0.25


class AccountBusy(Exception):
    """Another applied run for the same account holds the lock."""


def lock_path(state: Path) -> Path:
    return state.with_name(state.stem + ".coverage.lock")


@contextmanager
def account_lock(
    state: Path,
    *,
    wait: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold the account lock; raise ``AccountBusy`` if it stays taken for ``wait`` seconds."""
    path = lock_path(state)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:  # includes a symlink at the lock path (ELOOP)
        raise ValueError(
            "Cannot open the account lock: " + (exc.strerror or str(exc))
        ) from exc
    try:
        waited = 0.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited >= wait:
                    raise AccountBusy(
                        "Another Guardian run holds the account lock"
                    ) from None
                step = min(POLL_SECONDS, wait - waited)
                sleep(step)
                waited += step
            except OSError as exc:  # e.g. ENOLCK on a network filesystem
                raise ValueError(
                    "Cannot lock the account: " + (exc.strerror or str(exc))
                ) from exc
        yield
    finally:
        os.close(fd)
