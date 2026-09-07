#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — refuse to START a heavy operation while the
machine is genuinely under memory pressure.

WHY THIS EXISTS (2026-08-27/28 incident): a single long-running orchestrator session
had roughly 10-15 background agents running CONCURRENTLY, several doing heavy local
work at once — extension rebuilds (`build-and-install.sh`), multi-model `review`
passes (each spawning several `opencode run`/`codex exec` subprocess trees), full
test suites. Free memory on a 24GB machine dropped to ~65MB
(`vm_stat` showed ~4000 free pages at 16KB/page) and several real VS Code windows
hung on native "reload required" / "app not responding" modals. Nothing on the
machine was checking "is there room for one more heavy thing" before starting one.

THIS IS DELIBERATELY NOT A CONCURRENCY COUNTER OR A LOCK/SEMAPHORE. That shape was
considered and rejected: every existing pre-bash hook in this ecosystem decides
ALLOW/BLOCK FAST (this file included) — a hook that WAITS either blocks past the
dispatcher's timeout and then silently ALLOWS anyway (useless), or turns "machine
busy" into a hard failure with no visibility. A counting semaphore also needs a slot
released by whoever acquired it; an agent killed mid-operation (routine in this
ecosystem — session churn, `TaskStop`, a crash) leaks its slot forever, and reaping
a stale slot correctly is itself a hard, stateful problem — get it wrong and every
agent on the machine deadlocks on a phantom "queue" (see the "Double-block deadlock
configs" / "Never break depended-on tool" lessons this ecosystem has already paid
for). So this hook carries **zero persistent state** — no lock file, no counter, no
queue position. Every invocation re-reads the machine's REAL memory pressure at
that instant and decides fresh. Nothing to leak, nothing to go stale, nothing that
can deadlock a second hook.

The signal is macOS's own kernel jetsam pressure level
(`sysctl kern.memorystatus_vm_pressure_level`) — the exact value Activity Monitor's
"Memory Pressure" graph and the OS's own out-of-memory killer read, NOT a raw
"free pages" heuristic. Raw free-page counts are noisy on macOS (the OS deliberately
keeps free low by using spare RAM as disk cache — a healthy machine often shows a
few hundred MB "free" with plenty of headroom), so gating on a raw byte threshold
would false-positive constantly. The jetsam level collapses that noise into three
values: 1 = normal, 2 = warn, 4 = critical. This hook blocks at WARN-or-worse by
default (level >= 2) — deliberately proactive, since the goal is to stop the SECOND
heavy operation from tipping an already-strained machine into the incident above,
not to wait until the machine is already critical. Tune via
`RIG_HEAVY_OP_BLOCK_AT_LEVEL` (see below) if that default proves too aggressive in
practice. On any platform where this sysctl is unavailable (Linux, sandboxed
environments), the hook has no reliable signal and ALLOWS (fail-open) — see
`_read_linux_pressure_level` for the best-effort `/proc/pressure/memory` fallback
tried first.

"Heavy operation" is intentionally scoped to the operations actually implicated in
the incident: the extension rebuild script (`build-and-install.sh`),
`@vscode/vsce package`, the multi-model `review` CLI's heavy subcommands
(`diff`/`quorum`/`brainstorm`/`just-ask` — each spawns multiple provider
subprocess trees), and build/test-suite invocations (npm/pnpm/yarn/bun/deno/
cargo/go/make/rake/mvn/gradle/msbuild test|build|..., and the direct runners
playwright/cypress/jest/vitest/pytest).

Detection is TOKEN-based (`shlex.split`, exact token/adjacency matching), not a
raw substring search over the whole command string — deliberately NOT the
`enforce-timeout-on-bash` posture. That sibling hook's regex-over-raw-string
approach is safe there because it's advisory (warns and still allows); THIS hook
hard-blocks with no bypass, so the same approach would turn `git commit -m "make
all tests green"` or `task new --title "fix go build flake"` into a false-positive
hard block on a WARN-pressure machine (a real bug caught in review before this
hook shipped) — the quoted commit message becomes one shlex token ("make all
tests green"), not the bare word "make", so it never matches. The real siblings
that detect the SAME command classes (`no-long-inline-process`,
`subagent-no-bg-longproc`) are argv-aware for exactly this reason; this hook
doesn't port their full tokenizer (wrapper/env/job-boundary peeling) since it
doesn't need backgrounding semantics, just "don't match words trapped inside a
quoted argument" — plain `shlex.split` with exact-token comparison covers that.
An unparseable command (unbalanced quotes) falls back to whitespace-splitting,
which is still token-shaped (never a raw substring search).

No self-service bypass and deliberately NO Telegram hatch-escalation machinery
either (unlike most hard-block hooks in this ecosystem) — that machinery exists to
get a HUMAN JUDGMENT call on a decision only a human can make; whether the machine
currently has enough free memory is not a judgment call, it is an objective,
self-resolving fact that a plain retry re-measures. Wiring `tg-ctl` in here would
also reintroduce exactly the complexity/latency risk this hook was written to
avoid for a check that must stay fast and stateless. If pressure genuinely never
clears (every other agent is idle, nothing is going to free memory) that is itself
a signal worth telling the machine's operator about directly, not something to
hatch-approve past.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a crash or an unreadable pressure signal here must never wedge
an agent's ability to run a command — this is an availability circuit-breaker, not
a security boundary.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# jetsam pressure levels (macOS kern.memorystatus_vm_pressure_level): 1=normal,
# 2=warn, 4=critical. Default blocks at WARN-or-worse; override for a looser or
# stricter posture without touching code.
DEFAULT_BLOCK_AT_LEVEL = 2
# The ONLY levels the sysctl actually reports (see module docstring). Restricting
# the env override to this set closes an otherwise-real bypass: an operator (or a
# confused agent) setting RIG_HEAVY_OP_BLOCK_AT_LEVEL=5 or =999 would make even
# jetsam level 4 (critical) pass, silently defeating the hook's own "no bypass"
# claim in `decide`'s block message.
_VALID_PRESSURE_LEVELS = frozenset({1, 2, 4})

# ── heavy-operation detection (token-based — see module docstring for why this
# hard-block hook does NOT use the raw-substring style of enforce-timeout-on-bash) ──
_SUITE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun", "deno", "cargo", "go", "make", "rake", "mvn", "gradle", "msbuild"})
_SUITE_SUBCOMMANDS = frozenset({"test", "build", "verify", "package", "all"})
_DIRECT_TEST_RUNNERS = frozenset({"playwright", "cypress", "jest", "vitest", "pytest"})
_REVIEW_HEAVY_SUBCOMMANDS = frozenset({"diff", "quorum", "brainstorm", "just-ask"})

# Shell chain/pipe operators that shlex.split leaves as their own tokens (when
# space-separated, the common style). Used to scope a `--help`/`-h` exemption
# to the SAME segment as the matched invocation — see `_segment_requests_help`.
_SHELL_BOUNDARY_TOKENS = frozenset({"|", "||", "&&", ";"})


def _tokenize(command: str) -> list[str]:
    """Best-effort shell tokenization. Falls back to a bare whitespace split on
    unbalanced quotes — still token-shaped (never a raw substring search), so an
    unparseable command degrades to a slightly looser but still argv-like match
    rather than reopening the quoted-prose false-positive this replaced."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _segment_requests_help(tokens: list[str], start: int) -> bool:
    """True if `-h`/`--help` appears between `tokens[start]` and the next shell
    chain/pipe operator (or end of command) — i.e. in the SAME segment as the
    invocation matched at `start`. A real CLI's `-h`/`--help` short-circuits to
    printing usage and exiting, never actually starting the heavy work, so
    `review diff --help` (confirmed hard-blocking a genuinely non-heavy `--help`
    call in a 2026-09 incident, agent-tools follow-up) should never match.

    Deliberately SEGMENT-scoped, not "anywhere in the whole command": an
    unrelated tool's own `-h` further down a pipe (`review diff -C . | grep -h
    pattern`) must not spuriously exempt a segment that genuinely starts a
    heavy operation — see the module docstring on why a false NEGATIVE (a
    genuinely heavy op running unthrottled) is the more dangerous failure
    direction for this hard-block hook than an occasional unnecessary block.
    """
    for tok in tokens[start:]:
        if tok in _SHELL_BOUNDARY_TOKENS:
            break
        if tok in ("-h", "--help"):
            return True
    return False


def classify_heavy_operation(command: str) -> Optional[str]:
    """Return a short human label for the heavy operation `command` runs, or None.

    Kept as a pure function (no I/O) so it's directly unit-testable without
    mocking subprocess/sysctl. Token-based: a heavy-operation word trapped
    inside a MULTI-WORD quoted argument (a commit message, a `--title`) is one
    shlex token and never matches a bare-word check — see module docstring. A
    SINGLE-WORD quoted argument (`git commit -m "pytest"`) is NOT distinguishable
    from a real bare invocation after shlex strips its quotes — that ambiguity,
    AND the analogous ambiguity for an unrelated command's PATH ARGUMENT that
    happens to share a runner's basename (`git add ./vitest`), are documented,
    accepted trade-offs (same posture as `echo review diff`, see
    `test_bare_unquoted_tokens_are_still_flagged_documented_edge_case`), not bugs
    this function tries to close. A stricter, command-position-aware version
    was tried and reverted after review found it traded this false-positive
    class for a worse false-NEGATIVE class (`cd /repo && pnpm test`, `sudo
    pytest`, `env CI=1 npm test`, `npm --prefix x test` all stopped matching) —
    for a hard-block memory-safety gate, silently ALLOWING a genuinely heavy
    operation is the more dangerous failure direction than an occasional
    unnecessary block, so the loose-membership version is the deliberate choice
    here, not an oversight.
    """
    tokens = _tokenize(command)
    basenames = [_basename(t) for t in tokens]

    for i, b in enumerate(basenames):
        if b == "build-and-install.sh" and not _segment_requests_help(tokens, i):
            return "VS Code extension rebuild/package"
    for i, tok in enumerate(tokens[:-1]):
        if _basename(tok) == "vsce" and tokens[i + 1] == "package" and not _segment_requests_help(tokens, i):
            return "VS Code extension rebuild/package"
    for i, tok in enumerate(tokens[:-1]):
        if (
            _basename(tok) == "review"
            and tokens[i + 1] in _REVIEW_HEAVY_SUBCOMMANDS
            and not _segment_requests_help(tokens, i)
        ):
            return "multi-model review-cli pass"

    # Match on BASENAMES, not raw tokens (review-caught P2): the earlier
    # `set(tokens) & _SUITE_RUNNERS` / `& _DIRECT_TEST_RUNNERS` held only bare
    # names ("npm", "vitest", ...), so a path-qualified real invocation like
    # `/usr/bin/npm test` or `./node_modules/.bin/vitest run` never matched and
    # silently bypassed the gate entirely — a false NEGATIVE, the dangerous
    # direction (a genuinely heavy op runs unthrottled), unlike the accepted
    # false-positive ambiguity documented above. Kept as membership over ALL
    # basenames (not anchored to a resolved executable position) so wrapper
    # invocations `npx <tool>` / `pnpm dlx <tool>` / `npm run <script>` /
    # `sudo <tool>` / `cd x && <tool>` — where the real runner/subcommand isn't
    # in position 0 or 1 — still match, same as before this fix.
    basename_set = set(basenames)
    if basename_set & _SUITE_RUNNERS and basename_set & _SUITE_SUBCOMMANDS:
        return "build/test suite"
    if basename_set & _DIRECT_TEST_RUNNERS:
        return "test-suite runner"
    return None


# ── memory-pressure reading ───────────────────────────────────────────────────
_RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


def _read_macos_pressure_level(run: _RunFn = subprocess.run) -> Optional[int]:
    """The XNU jetsam pressure level (1/2/4), or None when unavailable/unparseable."""
    try:
        proc = run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _default_read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _read_linux_pressure_level(read_text: Callable[[str], str] = _default_read_text) -> Optional[int]:
    """Best-effort Linux fallback via PSI (`/proc/pressure/memory`'s `some avg10`).

    Maps the percentage onto the same 1/2/4 scale as macOS so callers share one
    threshold: <10% -> 1 (normal), <60% -> 2 (warn), >=60% -> 4 (critical). Not
    calibrated against a real incident (no Linux repro available) — deliberately
    conservative thresholds; returns None (fail-open) on any read/parse failure,
    including "file doesn't exist" (non-Linux, or PSI disabled in the kernel).
    """
    try:
        text = read_text("/proc/pressure/memory")
    except OSError:
        return None
    match = re.search(r"^some\s+.*\bavg10=([\d.]+)", text, re.MULTILINE)
    if not match:
        return None
    try:
        avg10 = float(match.group(1))
    except ValueError:
        return None
    if avg10 >= 60:
        return 4
    if avg10 >= 10:
        return 2
    return 1


def read_pressure_level(
    macos_reader: Optional[Callable[[], Optional[int]]] = None,
    linux_reader: Optional[Callable[[], Optional[int]]] = None,
    platform_name: Optional[str] = None,
) -> Optional[int]:
    """Platform-DISPATCHING (not fallback-chaining) pressure read. None means
    "no reliable signal" — callers MUST treat that as fail-open, never as
    "normal".

    Dispatches on the real platform rather than "try macOS, then fall back to
    Linux" — that chain was dead weight in practice (each reader already fails
    fast when its OS-specific binary/file is absent) but still cost one extra
    subprocess spawn attempt on every single heavy-op check on Linux, which is
    pure waste on a platform that never has `sysctl kern.memorystatus_*`.

    Defaults are resolved INSIDE the body (not bound as parameter defaults) so
    that monkeypatching the module-level `_read_macos_pressure_level` /
    `_read_linux_pressure_level` names (as `main()`'s tests do, exercising the
    real end-to-end call path) takes effect — a parameter default would bind
    the original function object at import time and silently ignore a patch.
    """
    macos_reader = macos_reader or _read_macos_pressure_level
    linux_reader = linux_reader or _read_linux_pressure_level
    platform_name = platform_name if platform_name is not None else platform.system()
    if platform_name == "Darwin":
        return macos_reader()
    return linux_reader()


def emit(decision: str, message: Optional[str] = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"heavy-op-memory-gate: {msg}\n")


def _block_at_level() -> int:
    raw = os.environ.get("RIG_HEAVY_OP_BLOCK_AT_LEVEL")
    if not raw:
        return DEFAULT_BLOCK_AT_LEVEL
    try:
        value = int(raw)
    except ValueError:
        warn(f"RIG_HEAVY_OP_BLOCK_AT_LEVEL={raw!r} is not an int — using default {DEFAULT_BLOCK_AT_LEVEL}")
        return DEFAULT_BLOCK_AT_LEVEL
    if value not in _VALID_PRESSURE_LEVELS:
        # A value outside {1,2,4} (e.g. 5 or 999) would make `level < block_at`
        # true for every real jetsam reading — a silent, total bypass of the
        # "no bypass" contract documented in `decide`'s block message. Reject it
        # rather than honor a value that can never actually block.
        warn(
            f"RIG_HEAVY_OP_BLOCK_AT_LEVEL={value} is not one of the real jetsam "
            f"levels {sorted(_VALID_PRESSURE_LEVELS)} — using default {DEFAULT_BLOCK_AT_LEVEL} "
            "(an out-of-range value would silently bypass every block)"
        )
        return DEFAULT_BLOCK_AT_LEVEL
    return value


def decide(label: Optional[str], level: Optional[int], block_at: int) -> tuple[str, Optional[str]]:
    """Pure decision core — the seam every test in this suite exercises directly.

    Takes the already-computed `classify_heavy_operation` label (not the raw
    command) so `main()` can skip reading memory pressure entirely for a
    non-heavy command — see `main()` for why that ordering matters.
    """
    if label is None:
        return "allow", None
    if level is None:
        return "allow", None  # no reliable pressure signal on this platform — fail open
    if level < block_at:
        return "allow", None

    level_name = {1: "normal", 2: "warn", 4: "critical"}.get(level, str(level))
    message = (
        f"Blocked: this looks like a {label}, and the machine's memory pressure is "
        f"currently '{level_name}' (jetsam level {level}, block threshold {block_at}). "
        "Starting another heavy operation now risks repeating the 2026-08-27/28 "
        "near-total-memory-exhaustion incident (free memory dropped to ~65MB and "
        "several VS Code windows hung). There is no bypass for this check — wait a "
        "few minutes for other concurrent work to finish or be reaped, then retry; "
        "the pressure level is re-read fresh on every attempt, so a retry after "
        "genuine memory frees up will pass. If pressure never clears even with "
        "nothing else obviously running, that itself is worth reporting to the "
        "machine's operator directly rather than working around this gate."
    )
    return "block", message


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    if not isinstance(event, dict):
        warn(f"event is a {type(event).__name__}, not an object — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args")
    if not isinstance(args, dict):
        args = {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)

    # Classify BEFORE touching memory pressure: the vast majority of Bash calls
    # are not heavy operations, and reading pressure means spawning `sysctl`
    # (or reading /proc/pressure/memory) on every single one. Skipping that for
    # every non-heavy command also matters most exactly when it's most
    # expensive: process spawn is slowest precisely when the machine is under
    # the memory pressure this hook is watching for.
    label = classify_heavy_operation(command)
    if label is None:
        emit("allow")
        return 0

    level = read_pressure_level()
    decision, message = decide(label, level, _block_at_level())

    if decision == "block":
        emit("block", message)
        return BLOCK_EXIT_CODE

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
