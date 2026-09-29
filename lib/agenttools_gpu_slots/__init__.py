"""agenttools_gpu_slots — a machine-wide pool of GPU-model slots, shared by every agent and
harness on the machine (agent-tools#817).

WHY: on 2026-09-29 thirteen concurrent ``whisper-cli`` runs, each loading the 1.6 GB
large-v3-turbo model into Metal buffers, wired 24.65 of 25.77 GB and kernel-panicked the Mac
(WindowServer watchdog). GPU buffers are WIRED memory: the kernel can neither page nor compress
them, so the jetsam pressure level stayed "normal" the whole time. Nothing on the machine capped
how many model loads ran at once.

THE POOL: ``N`` lock files (``slot-0.lock`` .. ``slot-{N-1}.lock``) in one per-user directory. A
model run holds one exclusive ``flock`` for its whole lifetime. flock locks belong to the open
file description, so the KERNEL releases a slot the instant its holder dies — SIGKILL, crash,
OOM kill, a harness torn down mid-run. No pid files, no counters, no reaper: a stale slot cannot
exist, which is what made the earlier semaphore idea (rejected in heavy-op-memory-gate's
docstring) unsafe.

SHARED PROTOCOL (stt-cli implements the same one, so both draw from the SAME slots — change it
only in lockstep with stt-cli):

- directory: ``$RIG_GPU_SLOT_DIR``, else ``~/.local/state/agent-tools/gpu-slots`` (mode 0700)
- slot count: ``$RIG_GPU_SLOTS`` (integer >= 1), default 2
- acquire: ``open(O_RDWR|O_CREAT, 0o600)`` + ``flock(LOCK_EX|LOCK_NB)`` on each slot in turn,
  polling every 0.25 s while all are busy; then truncate + write ``pid=<pid> argv=<argv>``
  (diagnostics only — the lock, not the text, is the truth)
- hold: keep the fd open for the model's whole lifetime; a child that does the real GPU work
  inherits it, so the slot stays taken until the model process itself exits
- re-entrancy: a holder exports ``RIG_GPU_SLOT_FD=<fd>``; a nested acquirer whose inherited fd
  is one of the slot files (same dev/inode) reuses that slot instead of taking a second one

The wrapper (``python3 <agent-tools>/lib/agenttools_gpu_slots run -- <command>``) waits for a
slot, then EXECs the command in place, so the lock is held by the model process itself rather
than by a parent that could die and free the slot while the model keeps running.

stdlib-only (POSIX ``fcntl``), so the heavy-op-memory-gate hook can load it by path.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence

DEFAULT_SLOTS = 2
DEFAULT_WAIT_SECONDS = 1800.0  # same bound stt-cli uses before it gives up with a clear error
POLL_SECONDS = 0.25

ENV_SLOTS = "RIG_GPU_SLOTS"
ENV_DIR = "RIG_GPU_SLOT_DIR"
ENV_FD = "RIG_GPU_SLOT_FD"

EXIT_USAGE = 2
EXIT_SLOT_TIMEOUT = 75  # sysexits EX_TEMPFAIL: every slot stayed busy — retry later
EXIT_NOT_EXECUTABLE = 126
EXIT_MISSING_COMMAND = 127

_HOLDER_ARGV_MAX = 300
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def slot_dir(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get(ENV_DIR)
    if override:
        return Path(override).expanduser()
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "state" / "agent-tools" / "gpu-slots"


def slot_count(env: Mapping[str, str] = os.environ) -> int:
    """``$RIG_GPU_SLOTS`` when it is a positive integer, else the default. A nonsense value falls
    back to the default rather than to "unlimited" or "zero slots"."""
    raw = env.get(ENV_SLOTS)
    if not raw:
        return DEFAULT_SLOTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SLOTS
    return value if value >= 1 else DEFAULT_SLOTS


def slot_paths(directory: Path, count: int) -> List[Path]:
    return [directory / f"slot-{i}.lock" for i in range(count)]


@dataclass
class Slot:
    index: int
    path: Path
    fd: int

    def release(self) -> None:
        os.close(self.fd)


@dataclass(frozen=True)
class SlotState:
    index: int
    path: Path
    held: bool
    holder: str  # the holder's "pid=… argv=…" line; empty for a free slot


def _lock_nonblocking(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise
    return True


def _record_holder(fd: int, argv: Optional[Sequence[str]]) -> None:
    line = f"pid={os.getpid()} argv={shlex.join(argv or [])}"[:_HOLDER_ARGV_MAX] + "\n"
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, line.encode("utf-8", "replace"), 0)
    except OSError:
        pass  # diagnostics only; the lock itself is already held


def try_acquire(directory: Path, count: int, argv: Optional[Sequence[str]] = None) -> Optional[Slot]:
    """Take the first free slot without waiting, or return None when all are busy."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for index, path in enumerate(slot_paths(directory, count)):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_CLOEXEC, 0o600)
        if _lock_nonblocking(fd):
            _record_holder(fd, argv)
            return Slot(index, path, fd)
        os.close(fd)
    return None


def acquire(
    directory: Path,
    count: int,
    *,
    timeout: Optional[float],
    argv: Optional[Sequence[str]] = None,
    on_wait: Optional[Callable[[], None]] = None,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[Slot]:
    """Wait up to ``timeout`` seconds (None = forever) for a free slot. ``on_wait`` runs once,
    the first time every slot is found busy."""
    deadline = None if timeout is None else clock() + timeout
    announced = False
    while True:
        slot = try_acquire(directory, count, argv)
        if slot is not None:
            return slot
        if deadline is not None and clock() >= deadline:
            return None
        if not announced and on_wait is not None:
            on_wait()
            announced = True
        sleep(poll if deadline is None else max(0.0, min(poll, deadline - clock())))


def slot_states(directory: Path, count: int) -> List[SlotState]:
    """Probe every slot without creating anything and without keeping any lock: a slot we can
    lock is free (our probe lock is dropped by the close right after)."""
    states = []
    for index, path in enumerate(slot_paths(directory, count)):
        try:
            fd = os.open(path, os.O_RDONLY | _O_CLOEXEC)
        except FileNotFoundError:
            states.append(SlotState(index, path, False, ""))
            continue
        try:
            held = not _lock_nonblocking(fd)
            holder = os.read(fd, _HOLDER_ARGV_MAX + 1).decode("utf-8", "replace").strip() if held else ""
        finally:
            os.close(fd)
        states.append(SlotState(index, path, held, holder))
    return states


def free_slot_count(directory: Path, count: int) -> int:
    return sum(1 for state in slot_states(directory, count) if not state.held)


def inherited_slot(directory: Path, env: Mapping[str, str] = os.environ) -> Optional[Slot]:
    """The slot an ancestor already holds for this process tree, if ``$RIG_GPU_SLOT_FD`` names an
    inherited fd that really is one of the pool's lock files (a stale or foreign fd number never
    bypasses the pool)."""
    raw = env.get(ENV_FD)
    if not raw:
        return None
    try:
        fd = int(raw)
        fd_stat = os.fstat(fd)
    except (ValueError, OSError):
        return None
    for path in sorted(directory.glob("slot-*.lock")):
        try:
            path_stat = os.stat(path)
        except OSError:
            continue
        if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
            continue
        # Same open file description as the ancestor's lock: re-locking it is a no-op. If
        # another description holds this slot, the fd is not ours after all.
        if not _lock_nonblocking(fd):
            return None
        suffix = path.stem.rsplit("-", 1)[-1]
        return Slot(int(suffix) if suffix.isdigit() else -1, path, fd)
    return None


def _say(message: str) -> None:
    sys.stderr.write(f"gpu-slot: {message}\n")
    sys.stderr.flush()


def _holders_summary(directory: Path, count: int) -> str:
    holders = [s.holder or f"{s.path.name}: holder unknown" for s in slot_states(directory, count) if s.held]
    return "; ".join(holders) if holders else "none right now"


def _run(timeout: float, command: List[str]) -> int:
    directory, count = slot_dir(), slot_count()
    slot = inherited_slot(directory)
    if slot is None:
        slot = acquire(
            directory,
            count,
            timeout=timeout,
            argv=command,
            on_wait=lambda: _say(
                f"all {count} GPU model slots in {directory} are busy — waiting up to {timeout:g}s "
                f"(holders: {_holders_summary(directory, count)})"
            ),
        )
        if slot is None:
            _say(
                f"gave up after {timeout:g}s: all {count} GPU model slots in {directory} are still "
                f"taken (holders: {_holders_summary(directory, count)}). Retry later or raise --timeout; "
                f"{ENV_SLOTS} sets the machine-wide slot count."
            )
            return EXIT_SLOT_TIMEOUT
    os.set_inheritable(slot.fd, True)
    env = dict(os.environ)
    env[ENV_FD] = str(slot.fd)
    try:
        os.execvpe(command[0], command, env)
    except FileNotFoundError:
        _say(f"{command[0]}: command not found")
        return EXIT_MISSING_COMMAND
    except PermissionError:
        _say(f"{command[0]}: permission denied")
        return EXIT_NOT_EXECUTABLE
    return EXIT_USAGE  # unreachable: execvpe either replaces this process or raises


def _status() -> int:
    directory, count = slot_dir(), slot_count()
    states = slot_states(directory, count)
    busy = sum(1 for s in states if s.held)
    print(f"{busy} of {count} GPU model slots busy ({directory})")
    for s in states:
        print(f"  {s.path.name}: {'busy  ' + s.holder if s.held else 'free'}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agenttools-gpu-slot",
        description=(
            "Machine-wide GPU-model slot pool (flock lock files, freed by the kernel when a holder "
            f"dies). Slot count: ${ENV_SLOTS} (default {DEFAULT_SLOTS}); directory: ${ENV_DIR} "
            "(default ~/.local/state/agent-tools/gpu-slots)."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser(
        "run",
        help="wait for a free slot, then exec COMMAND holding it",
        usage="%(prog)s [--timeout SECONDS] -- COMMAND [ARG...]",
    )
    run.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help=f"seconds to wait for a slot before giving up with exit {EXIT_SLOT_TIMEOUT} "
        f"(default {DEFAULT_WAIT_SECONDS:g}; 0 = do not wait)",
    )
    run.add_argument("command", nargs="*", help=argparse.SUPPRESS)
    sub.add_parser("status", help="show which slots are busy and who holds them")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first `--` is the model command, verbatim — never parsed as our options.
    command: List[str] = []
    if "--" in args:
        split = args.index("--")
        args, command = args[:split], args[split + 1 :]
    parser = _parser()
    ns = parser.parse_args(args)
    if ns.action == "status":
        return _status()
    command = list(ns.command) + command
    if not command:
        parser.error("run needs a command: agenttools-gpu-slot run -- COMMAND [ARG...]")
    if ns.timeout < 0:
        parser.error("--timeout must be >= 0")
    return _run(ns.timeout, command)
