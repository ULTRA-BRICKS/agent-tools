#!/usr/bin/env python3
"""runpod_run_policy.py — the spend policy of `runpod-run`, as pure functions plus a thin CLI.

Ships next to `runpod-run` in the `runpod-offload` skill dir (rig copies the whole dir), and
the bash script calls it as `python3 <dir of the resolved script>/runpod_run_policy.py
<subcommand> …`. Policy lives here rather than in bash so pytest can exercise every rule
directly. Stdlib only: the helper must run on any machine that runs the script.

Subcommands:
  check --cwd DIR --shape cpu3g-4|gpu:<id> [--reason T] [--remote-reason T] -- <cmd…>
      exit 0 when the run may proceed; exit 20 with the refusal on stderr when it may not:
      a no-op command, a single test-file invocation (unless --remote-reason), a flavor
      above 4 vCPU / 16 GB or any GPU without --reason, a GPU for a command that shows
      no sign of needing CUDA.
  ledger-append --state DIR --json '<object>'
      append one line to DIR/ledger.jsonl: the given fields, plus `ts`, `agent`, `session`
      and `est_usd` when the object leaves them out (exit 2 on an unknown `event`).
  report --state DIR [--runs DIR] [--day YYYY-MM-DD] [--billed USD] [--api URL]
      the day's spend by category from the ledger (or, for a day without ledger lines, from
      the legacy run logs in --runs, default DIR/runs), set against the billed $: --billed,
      or read from URL's billing API with RUNPOD_API_KEY (a stderr note and "unknown" when
      that fails).
  budget --api URL --state DIR --price USD_PER_H --timeout-s S --live-json FILE [--config-dir D]
      the daily $ cap before a pod start or create: prints the budget line; exit 21 when
      billed today + (live pods' + the new pod's $/h) x S/3600 reaches the cap, or when
      today's billing (RUNPOD_API_KEY), or the price, cannot be read (fail closed). The cap
      is the owner's: D/budget (`daily_usd = N`, default 6) and D/budget-override
      (`YYYY-MM-DD N`, that day only); D defaults to ~/.config/runpod-run of the account's
      passwd home (HOME and XDG_CONFIG_HOME are ignored). Today's billed $ is
      cached for 5 minutes in DIR/billing-today.json.
  slots acquire --state DIR --name POD --pid PID --timeout-min M
      take one of the host's 3 pod slots for POD on behalf of the runpod-run PID, or wait
      for one in FIFO order, printing `runpod-run: queued for a pod slot (position N of M;
      K live: …)` whenever the position changes; exit 22 after M minutes. POD's own live
      slot (a pod kept by --ttl, which this run reuses) is taken over: no new slot.
  slots handover|release --state DIR --name POD --pid PID
      handover: PID (the --ttl timer of a kept pod) holds POD's slot from now on; release:
      PID gives the slot up (back to the holder it took it over from, while that one lives).
  slots list --state DIR [--name POD]
      `slots: N/3 live (names), Q queued`, with `→ reuse|take a free slot|queue (…)` for a
      run of POD; then one line per live slot with its holder.
  account-home
      the account's home from the passwd database (exit 2 without an entry). The script
      keeps the account state (ledger, pod slots, billing cache) in its .cache/runpod-run
      and passes that dir as --state; HOME never moves it.
  dedup-key --shape S --cmd C --dir D [--dir D…]
      the dedup key of a run: sha256 over each dir's synced files with their content (git
      ls-files tracked + untracked-not-ignored with git hash-object; a dir outside git: its
      relative paths and sha256s), the quoted command and the compute shape.
  dedup-lookup|dedup-store --state DIR --repo-tag T --key K [--exit N --log FILE]
      DIR/dedup/T.json = {key, ts, exit, log}: lookup prints the record as JSON when it has
      key K and is less than 24 h old (else exit 1); store writes it unless the exit code
      says the command never ran (2, 3, 20-23, 255).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep, time_ns
from typing import NamedTuple

EXIT_REFUSED = 20
_GPU_PREFIX = "gpu:"

_NOOP_COMMANDS = (["true"], [":"], ["/bin/true"], ["/usr/bin/true"], ["exit", "0"])
_SHELL_OPERATORS = {"&&", "||", ";", "|", "&"}

# uv run / poetry run options that consume the next token (so that token is no operand).
_UV_RUN_VALUE_OPTS = {
    "--with", "--with-editable", "--with-requirements", "--python", "-p", "--extra", "--group",
    "--only-group", "--no-group", "--project", "--directory", "--package", "--index",
    "--default-index", "--index-url", "--extra-index-url", "--find-links", "-f", "--env-file",
    "--config-file", "--cache-dir",
}
# npx / bunx / uvx options that consume the next token (`npx --package vitest vitest run …`).
_PACKAGE_RUNNER_VALUE_OPTS = {"--package", "-p", "--from", "--with", "--python", "--cache"}
# pnpm exec / dlx options that consume the next token (`pnpm exec --filter web vitest …`).
_PNPM_VALUE_OPTS = {"--package", "--filter", "-F", "--dir", "-C"}
# env options that consume the next token (`env -u HOME pytest …`); `-S` is split, not skipped.
_ENV_VALUE_OPTS = {"-u", "--unset", "-C", "--chdir"}
# Python interpreter options that consume the next token (`python -W error -m pytest …`).
_PYTHON_VALUE_OPTS = {"-W", "-X"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# A `sh -c` script with any of these does more than run one simple command: not unwrapped.
_SHELL_SYNTAX_RE = re.compile(r"[;&|`$()<>\n]")
# Test-runner options that take a value. Unknown options are assumed to be flags: a value
# miscounted as an operand only ever makes a command look LESS trivial (never refused wrongly).
_RUNNER_VALUE_OPTS = {
    "pytest": {"-m", "-p", "-c", "-o", "-n", "--tb", "--maxfail", "--rootdir", "--basetemp",
               "--deselect", "--ignore", "--ignore-glob", "--timeout", "--junitxml", "--durations",
               "--cov", "--cov-report", "-W", "--confcutdir", "--log-level", "--dist", "--capture",
               "--color", "--import-mode", "--log-cli-level", "--log-file", "--cov-fail-under",
               "--reruns", "--count", "-r"},
    "bun": {"--timeout", "--rerun-each", "--preload", "--bail", "--coverage-reporter",
            "--coverage-dir", "--reporter", "--reporter-outfile", "--seed"},
    "vitest": {"--config", "-c", "--project", "--root", "-r", "--dir", "--reporter",
               "--outputFile", "--pool", "--environment", "--shard", "--testTimeout"},
    "jest": {"--config", "-c", "--projects", "--rootDir", "--reporters", "--shard",
             "--testTimeout", "--maxWorkers", "-w", "--testPathIgnorePatterns"},
    "go": {"-count", "-timeout", "-p", "-parallel", "-tags", "-coverprofile", "-covermode",
           "-cpu", "-bench", "-benchtime", "-o", "-exec", "-skip", "-shuffle"},
}
# Test-name filters: one of these narrows the run to a few tests, as one file does.
_FILTER_OPTS = {
    "pytest": {"-k"},
    "bun": {"-t", "--test-name-pattern"},
    "vitest": {"-t", "--testNamePattern"},
    "jest": {"-t", "--testNamePattern"},
    "go": {"-run"},
}

_CUDA_RE = re.compile(
    r"cuda|torch|nvidia|nvcc|gpu|tensorflow|jax|triton|vllm|llama|whisper|diffusers|transformers"
    r"|comfy|ollama|onnxruntime-gpu|\.cu\b",
    re.IGNORECASE,
)
_GPU_REASON_RE = re.compile(r"cuda|gpu", re.IGNORECASE)

# Memory per vCPU of the Runpod CPU flavor families (cpu3c-4 = 4 vCPU x 2 GB).
_GB_PER_VCPU = {"c": 2, "g": 4, "m": 8}
_MAX_VCPUS = 4
_MAX_MEMORY_GB = 16


def _tokens(argv: list[str]) -> list[str]:
    """The command as words: `runpod-run -- "bun test x.ts"` (one string) splits like a shell."""
    if len(argv) == 1:
        try:
            return shlex.split(argv[0])
        except ValueError:
            return argv
    return argv


def is_noop(argv: list[str]) -> bool:
    """A command that proves nothing (`true`, `:`, `exit 0`) still boots a billed pod — also
    behind `env VAR=…` or `sh -c 'true'`."""
    words = _strip_launchers(_tokens(argv))
    return words is not None and words in _NOOP_COMMANDS


def _strip_launchers(words: list[str]) -> list[str] | None:
    """Peel `env [opts] VAR=…`, `exec`/`command` and one-command `sh -c '…'` layers off a
    command. None when a `-c` script does more than run one simple command (its words cannot
    be classified)."""
    while words:
        head = os.path.basename(words[0])
        if head == "env":
            words = _env_command(words[1:])
            if words is None:
                return None
        elif head == "exec":
            words = _skip_options(words[1:], {"-a"})
        elif head == "command" and words[1:2] not in (["-v"], ["-V"]):
            words = _skip_options(words[1:], set())
        elif head in _SHELLS:
            script = _shell_c_script(words[1:])
            if script is None:
                return words  # `sh script.sh …`: not a launcher of one command
            if _SHELL_SYNTAX_RE.search(script):
                return None
            try:
                words = shlex.split(script, comments=True)
            except ValueError:
                return None
        else:
            return words
    return words


def _env_command(args: list[str]) -> list[str] | None:
    """What `env [opts] [VAR=…] cmd…` runs. `-S STRING` splits STRING into words the way env
    does (`env -S 'pytest a.py'` runs pytest), re-read as a fresh `env` invocation."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        split, rest = _env_split_string(args, i)
        if split is not None:
            try:
                return ["env", *shlex.split(split), *rest]
            except ValueError:
                return None
        i += 2 if arg in _ENV_VALUE_OPTS else 1
    args = args[i:]
    while args and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", args[0], re.DOTALL):
        args = args[1:]
    return args


def _env_split_string(args: list[str], i: int) -> tuple[str | None, list[str]]:
    """(STRING, the words after it) when args[i] is env's `-S STRING` / `-SSTRING` /
    `--split-string[=]STRING`; (None, []) for any other option."""
    arg = args[i]
    if arg in ("-S", "--split-string"):
        return (args[i + 1], args[i + 2 :]) if i + 1 < len(args) else ("", [])
    if arg.startswith("--split-string="):
        return arg.partition("=")[2], args[i + 1 :]
    if arg.startswith("-S"):
        return arg[2:], args[i + 1 :]
    return None, []


def _skip_options(words: list[str], value_opts: set[str]) -> list[str]:
    """Drop leading options (and their values) of a wrapper like `uv run`."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in value_opts else 1
    return words[i:]


def _runner(words: list[str]) -> tuple[str, list[str]] | None:
    """(runner, its arguments) for a test-runner invocation, through the usual wrappers."""
    while words:
        head, rest = os.path.basename(words[0]), words[1:]
        if head == "uv" and rest[:1] == ["run"]:
            words = _skip_options(rest[1:], _UV_RUN_VALUE_OPTS)
        elif head == "poetry" and rest[:1] == ["run"]:
            words = _skip_options(rest[1:], set())  # also drops a `--` separator
        elif head == "pnpm" and rest[:1] in (["exec"], ["dlx"]):
            words = _skip_options(rest[1:], _PNPM_VALUE_OPTS)
        elif head in ("npx", "bunx", "uvx"):
            words = _skip_options(rest, _PACKAGE_RUNNER_VALUE_OPTS)
        elif head in ("env", "exec", "command") or head in _SHELLS:
            stripped = _strip_launchers(words)
            if stripped is None or stripped == words:
                return None
            words = stripped
        elif re.fullmatch(r"python[0-9.]*", head):
            return _python_module_runner(rest)
        elif head in ("pytest", "py.test"):
            return "pytest", rest
        elif head == "bun" and rest[:1] == ["test"]:
            return "bun", rest[1:]
        elif head == "vitest":
            return "vitest", rest[1:] if rest[:1] == ["run"] else rest
        elif head == "jest":
            return "jest", rest
        elif head == "go" and rest[:1] == ["test"]:
            return "go", rest[1:]
        else:
            return None
    return None


def _shell_c_script(args: list[str]) -> str | None:
    """The script of `sh [-e…] -c 'script'` (also `-lc`, `-ec`); None for any other shell use."""
    for i, arg in enumerate(args):
        if not arg.startswith("-"):
            return None
        if re.fullmatch(r"-[a-z]*c[a-z]*", arg):
            script_at = i + 2 if args[i + 1 : i + 2] == ["--"] else i + 1  # `sh -c -- 'script'`
            return args[script_at] if script_at < len(args) else None
    return None


def _python_module_runner(args: list[str]) -> tuple[str, list[str]] | None:
    """`python [interpreter options] -m pytest …` (`python -u -m pytest`, `python -mpytest`)."""
    i = 0
    while i < len(args) and args[i].startswith("-") and not args[i].startswith("-m"):
        i += 2 if args[i] in _PYTHON_VALUE_OPTS else 1
    if args[i : i + 2] == ["-m", "pytest"]:
        return "pytest", args[i + 2 :]
    if args[i : i + 1] == ["-mpytest"]:
        return "pytest", args[i + 1 :]
    return None


def _is_directory_operand(operand: str, cwd: str) -> bool:
    path = operand.split("::", 1)[0]
    if path.endswith(("/", "/...")) or path in (".", "..", "./..."):
        return True
    if os.path.isdir(os.path.join(cwd, path)):
        return True
    # An operand with no file extension that is not a file names a directory or package
    # (`pytest tests`, `go test ./pkg`): a suite, not one file.
    return "::" not in operand and "." not in os.path.basename(path) and not os.path.isfile(os.path.join(cwd, path))


def classify_trivial(argv: list[str], cwd: str) -> bool:
    """True for a single test-file invocation: one file operand, or a test-name filter, and
    no directory operand. Such a run fits the local machine (heavy-op-memory-gate allows 1-2
    files) and a pod's boot + bootstrap costs more than the run itself."""
    words = _tokens(argv)
    if any(w in _SHELL_OPERATORS for w in words):
        return False
    found = _runner(words)
    if found is None:
        return False
    runner, args = found
    value_opts, filter_opts = _RUNNER_VALUE_OPTS[runner], _FILTER_OPTS[runner]
    operands: list[str] = []
    filtered = False
    i = 0
    while i < len(args):
        arg = args[i]
        name = arg.split("=", 1)[0]
        if name in filter_opts:
            filtered = True
            i += 1 if "=" in arg else 2
        elif any(len(opt) == 2 and arg.startswith(opt) for opt in filter_opts):
            filtered = True  # attached short value: `pytest -kfoo`, `bun test -tfoo`
            i += 1
        elif arg.startswith("-"):
            i += 2 if arg in value_opts else 1
        else:
            operands.append(arg)
            i += 1
    if any(_is_directory_operand(op, cwd) for op in operands):
        return False
    return len(operands) == 1 or filtered


def needs_cuda(argv: list[str]) -> bool:
    """The command text shows it uses a GPU stack (torch, CUDA, vLLM, …)."""
    return bool(_CUDA_RE.search(" ".join(argv)))


def flavor_needs_reason(spec: str) -> bool:
    """Any GPU, and any CPU flavor above 4 vCPU or 16 GB, must say why (`--reason`)."""
    if spec.startswith(_GPU_PREFIX):
        return True
    match = re.fullmatch(r"cpu\d+([a-z]+)-(\d+)", spec)
    if not match:
        return True
    gb_per_vcpu = _GB_PER_VCPU.get(match.group(1))
    vcpus = int(match.group(2))
    if gb_per_vcpu is None:
        return True  # an unknown family's memory cannot be shown to be small
    return vcpus > _MAX_VCPUS or vcpus * gb_per_vcpu > _MAX_MEMORY_GB


def check(argv: list[str], cwd: str, shape: str, reason: str, remote_reason: str) -> str | None:
    """The refusal message for this run, or None when it may proceed."""
    if is_noop(argv):
        return (f"refusing a no-op command ({shlex.join(argv)}): it proves nothing and still boots "
                "a billed pod; run the real command")
    if classify_trivial(argv, cwd) and not remote_reason.strip():
        return ("run it locally: one test file does not need a pod (heavy-op-memory-gate allows 1-2 files)"
                ' — or pass --remote-reason "<why it must run on a pod>"')
    if flavor_needs_reason(shape) and not reason.strip():
        if shape.startswith(_GPU_PREFIX):
            return f'--gpu {shape[len(_GPU_PREFIX):]!r} needs --reason "<why this run needs a GPU>"'
        return (f'--cpu {shape} is above 4 vCPU / 16 GB and needs --reason "<why the default cpu3g-4 '
                'is not enough>"')
    if shape.startswith(_GPU_PREFIX) and not needs_cuda(argv) and not _GPU_REASON_RE.search(reason):
        return ("--gpu refused: the command shows no sign of needing CUDA (torch, nvcc, vllm, …); "
                "run it on a CPU pod, or say why it needs the GPU in --reason (mention cuda or gpu)")
    return None


# --- ledger (R8) ------------------------------------------------------------------------------
# One JSON line per runpod-run invocation, in this key order. The script writes it to the file
# itself, so a caller that throws stderr away (`runpod-run … 2>/dev/null | tail`) still leaves it.
LEDGER_FIELDS = (
    "ts", "event", "repo", "repo_tag", "cmd", "pod_id", "pod_name", "flavor", "usd_per_h",
    "boot_s", "sync_s", "bootstrap_s", "command_s", "idle_s", "exit", "est_usd", "agent",
    "session", "parent", "reason", "remote_reason",
)
LEDGER_EVENTS = ("run", "refused", "dedup", "queue-timeout", "budget", "terminate")
_PHASES = ("boot_s", "sync_s", "bootstrap_s", "command_s", "idle_s")
_SESSION_VARS = ("OMP_SESSION_ID", "OMP_SESSION_DIR", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")
_PARENT_MAX = 200


def _cost(usd_per_h: float | None, seconds: float | None) -> float:
    return (usd_per_h or 0.0) * (seconds or 0.0) / 3600


def ledger_record(fields: Mapping[str, object], env: Mapping[str, str], now: datetime) -> dict[str, object]:
    """`fields` completed into a full ledger line: every key present (None when not given, an
    empty string counts as not given), `ts` now in local time with its offset, `agent` and
    `session` from the environment, `est_usd` from the price and the phase seconds."""
    record: dict[str, object] = {key: (None if fields.get(key) == "" else fields.get(key)) for key in LEDGER_FIELDS}
    if record["event"] not in LEDGER_EVENTS:
        raise ValueError(f"unknown ledger event {record['event']!r} (one of {', '.join(LEDGER_EVENTS)})")
    if record["ts"] is None:
        record["ts"] = now.astimezone().isoformat(timespec="seconds")
    if record["agent"] is None:
        record["agent"] = env.get("RIG_AGENT_ID") or None
    if record["session"] is None:
        record["session"] = next((env[name] for name in _SESSION_VARS if env.get(name)), None)
    if isinstance(record["parent"], str):
        record["parent"] = record["parent"][:_PARENT_MAX]
    usd_per_h = record["usd_per_h"]
    if record["est_usd"] is None and isinstance(usd_per_h, (int, float)):
        seconds = sum(value for key in _PHASES if isinstance(value := record[key], (int, float)))
        record["est_usd"] = round(_cost(usd_per_h, seconds), 6)
    return record


def ledger_append(path: Path, record: Mapping[str, object]) -> None:
    """Append `record` as one line with ONE write on an O_APPEND descriptor, so lines from
    concurrent runs never interleave."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


# --- report (R8) ------------------------------------------------------------------------------
# Before the ledger existed, a run left only runs/YYYYMMDD-HHMMSS-<repo_tag>.log (opened when the
# command started, last written when it ended) and every pod was the default cpu3g-4.
_LEGACY_LOG_RE = re.compile(r"(\d{8}-\d{6})-(.+)\.log")
LEGACY_USD_PER_H = 0.16
TRIVIAL_COMMAND_S = 30
# The report's dollar categories; G (unattributed) = billed - their sum.
_CATEGORIES = {
    "A": "command, exit 0/1",
    "B": "boot+sync+bootstrap (runs that reached it)",
    "C": "idle (pods kept by --ttl)",
    "D": "failed before the command",
    "E": "failed run, other exits",
}
_COUNTED_EVENTS = ("refused", "dedup", "queue-timeout", "budget", "terminate")


def _local_day(ts: object) -> date | None:
    try:
        moment = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return (moment.astimezone() if moment.tzinfo else moment).date()


def _ledger_lines(state: Path, day: date) -> list[dict[str, object]]:
    try:
        text = (state / "ledger.jsonl").read_text(errors="replace")
    except FileNotFoundError:
        return []
    lines = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a torn or hand-edited line must not hide the rest of the day
        if isinstance(line, dict) and _local_day(line.get("ts")) == day:
            lines.append(line)
    return lines


def _num(value: object) -> float | None:
    """A usable figure from a ledger field: a finite number, else None (a hand-edited `NaN`,
    which Python's json accepts, must not turn every total into `$nan`)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _usd(value: float) -> str:
    return f"${value:.3f}"


def _tally(counter: Counter[str]) -> str:
    return ", ".join(f"{name} {n}" for name, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


_LABEL_WIDTH = 44  # the report's label column, so the $ figures line up


def _row(label: str, value: str) -> str:
    return f"  {label:<{_LABEL_WIDTH}}{value}"


def _billed_line(billed_usd: float | None) -> str:
    if billed_usd is None:
        return "billed:   unknown (no API key, or the billing API was unreadable)"
    return f"billed:   {_usd(billed_usd)} (billing API)"


def _unattributed(billed_usd: float | None, attributed: float) -> str:
    return _usd(billed_usd - attributed) if billed_usd is not None else "unknown (no billed $)"


def _legacy_report(runs_dir: Path, day: date, billed_usd: float | None) -> list[str] | None:
    runs: list[tuple[str, float]] = []
    stamp = day.strftime("%Y%m%d")
    for path in sorted(runs_dir.glob(f"{stamp}-*.log")) if runs_dir.is_dir() else []:
        match = _LEGACY_LOG_RE.fullmatch(path.name)
        if not match:
            continue
        start = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
        runs.append((match.group(2), max(0.0, path.stat().st_mtime - start)))
    if not runs:
        return None
    command_usd = _cost(LEGACY_USD_PER_H, sum(seconds for _, seconds in runs))
    return [
        f"legacy: {len(runs)} runs from run logs (no ledger) — categories B-E unavailable",
        _billed_line(billed_usd),
        _row(f"A command time at ${LEGACY_USD_PER_H}/h", _usd(command_usd)),
        # A log's mtime is its last output, so a command that went quiet ran longer than this.
        "  (a lower bound: a run log ends at the command's last output line)",
        _row("G unattributed (billed - A)", _unattributed(billed_usd, command_usd)),
        f"runs: {len(runs)} — per repo: {_tally(Counter(tag for tag, _ in runs))}",
        f"trivial (<{TRIVIAL_COMMAND_S} s command): {sum(1 for _, s in runs if s < TRIVIAL_COMMAND_S)}",
    ]


def report(state: Path, day: date, billed_usd: float | None, runs_dir: Path | None = None) -> str:
    """The day's spend: billed $ against the ledger's estimate split into A command (exit 0/1),
    B boot+sync+bootstrap of runs that reached their command, C idle (kept by --ttl), D runs
    that failed before their command (all their time), E command time of runs that exited
    otherwise, G unattributed = billed - (A..E); then runs per repo and per agent, trivial
    runs, and the refusal counts. A day without ledger lines falls back to the run logs in
    runs_dir (default state/runs)."""
    head = f"runpod-run report for {day.isoformat()} (local day)"
    lines = _ledger_lines(state, day)
    if not lines:
        legacy = _legacy_report(runs_dir or state / "runs", day, billed_usd)
        if legacy is None:
            legacy = [f"no runs recorded for {day.isoformat()} (no ledger lines, no run logs)",
                      _billed_line(billed_usd), _row("G unattributed (billed)", _unattributed(billed_usd, 0.0))]
        return "\n".join([head, *legacy]) + "\n"

    split = dict.fromkeys(_CATEGORIES, 0.0)
    runs_per_repo: Counter[str] = Counter()
    runs_per_agent: Counter[str] = Counter()
    events: Counter[str] = Counter()
    trivial = unpriced = 0
    for line in lines:
        event = str(line.get("event"))
        events[event] += 1
        usd_per_h = _num(line.get("usd_per_h"))
        phases = {key: _num(line.get(key)) for key in _PHASES}
        command_s = phases["command_s"]
        if usd_per_h is None and any(phases.values()):
            unpriced += 1
        split["C"] += _cost(usd_per_h, phases["idle_s"])
        if event == "terminate":
            continue  # an ended pod's line carries its idle time only
        overhead = sum(phases[key] or 0.0 for key in ("boot_s", "sync_s", "bootstrap_s"))
        if command_s is None:
            split["D"] += _cost(usd_per_h, overhead)
        else:
            split["B"] += _cost(usd_per_h, overhead)
            split["A" if line.get("exit") in (0, 1) else "E"] += _cost(usd_per_h, command_s)
        if event == "run":
            runs_per_repo[str(line.get("repo") or line.get("repo_tag") or "?")] += 1
            runs_per_agent[str(line.get("agent") or "(no RIG_AGENT_ID)")] += 1
            if command_s is not None and command_s < TRIVIAL_COMMAND_S:
                trivial += 1
    ledger_usd = sum(split.values())
    out = [
        head,
        _billed_line(billed_usd),
        f"ledger:   {_usd(ledger_usd)} estimated over {len(lines)} ledger line(s)",
        *(_row(f"{key} {label}", _usd(split[key])) for key, label in _CATEGORIES.items()),
        _row("G unattributed (billed - ledger)", _unattributed(billed_usd, ledger_usd)),
    ]
    if unpriced:
        out.append(f"  ({unpriced} line(s) carry no $/h; their time is not in the figures above)")
    runs = sum(runs_per_repo.values())
    out.append(f"runs: {runs}" + (f" — per repo: {_tally(runs_per_repo)}; per agent: {_tally(runs_per_agent)}"
                                    if runs else ""))
    out.append(f"trivial (<{TRIVIAL_COMMAND_S} s command): {trivial}")
    out.append(", ".join(f"{event} {events[event]}" for event in _COUNTED_EVENTS))
    return "\n".join(out) + "\n"


# --- daily budget (R3) ------------------------------------------------------------------------
EXIT_BUDGET = 21
DEFAULT_DAILY_USD = 6.0
BILLING_CACHE_S = 300
_BILLING_HTTP_TIMEOUT_S = 20
_DOWN_STATUSES = {"EXITED", "TERMINATED"}  # pods that bill no compute $/h
_CAP_RE = re.compile(r"\s*daily_usd\s*=\s*(\d+(?:\.\d+)?)\s*(?:#.*)?")
_OVERRIDE_RE = re.compile(r"\s*(\d{4}-\d{2}-\d{2})\s+(\d+(?:\.\d+)?)\s*(?:#.*)?")


def _escalation(title: str, summary: str) -> str:
    """The refusal's last words: what the agent sends the owner. Names the command and the
    format rather than printing one to paste: tg's decision gate wants a structured body."""
    return (f'escalate to the owner: send a decision request with tg --tag decision --title "runpod-run daily'
            f' budget {title}" (decision-request format: tg help format) — {summary}; the owner raises the'
            f" cap by writing ~/.config/runpod-run/budget-override")


class BillingUnreadable(Exception):
    """The billing API gave no usable figure; the budget then refuses (fail closed)."""


def _utc_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017 — datetime.UTC needs 3.11


def _local_midnight(day: date) -> datetime:
    # A naive datetime's astimezone() reads it as system local time, DST rules included.
    return datetime.combine(day, time()).astimezone()


def day_window(day: date, now: datetime) -> tuple[str, str]:
    """The billing query window of one LOCAL day as UTC ISO Z strings: its midnight to the
    next one, or to `now` when the day is not over yet."""
    end = min(_local_midnight(day + timedelta(days=1)), now)
    return _utc_z(_local_midnight(day)), _utc_z(end)


def local_day_window(now: datetime) -> tuple[str, str]:
    """Today's billing window: local midnight to `now`, as UTC ISO Z strings."""
    return day_window(now.astimezone().date(), now)


def account_home() -> Path:
    """The ACCOUNT's home, from the passwd database: where the owner's budget files and the
    account state (ledger, pod slots, billing cache) live. HOME and XDG_CONFIG_HOME are ignored
    on purpose: an agent can set either, and the caps must not move with them. KeyError for a
    uid without a passwd entry."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def account_config_dir() -> Path:
    """Where the owner's budget files live: ~/.config/runpod-run of the account's home (the rig
    guard protects this one path)."""
    return account_home() / ".config" / "runpod-run"


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def daily_cap(config: Path, today: date) -> float:
    """The owner's cap for `today`: `budget-override`'s line for today, else `budget`'s
    `daily_usd`, else $6. A value that does not parse counts as absent."""
    cap = DEFAULT_DAILY_USD
    for line in _read_lines(config / "budget"):
        match = _CAP_RE.fullmatch(line)
        if match:
            cap = float(match.group(1))
    for line in _read_lines(config / "budget-override"):
        match = _OVERRIDE_RE.fullmatch(line)
        if match and match.group(1) == today.isoformat():
            cap = float(match.group(2))
    return cap


def _fetch_billed(api: str, key: str, start: str, end: str) -> float:
    query = urllib.parse.urlencode({"bucketSize": "hour", "startTime": start, "endTime": end}, safe=":")
    request = urllib.request.Request(f"{api}/billing?{query}", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=_BILLING_HTTP_TIMEOUT_S) as response:
            body = json.loads(response.read())
    except (OSError, ValueError) as exc:  # URLError/HTTPError/timeouts are OSErrors
        raise BillingUnreadable(f"GET {api}/billing: {exc}") from exc
    records = body.get("records") if isinstance(body, dict) else None
    if not isinstance(records, list):
        raise BillingUnreadable(f"GET {api}/billing: no records list in the answer")
    total = 0.0
    for record in records:
        # Every hourly record carries a numeric totalAmount (checked live, 2026-10-16); one
        # without it is an answer we cannot read, never $0 spent.
        amount = record.get("totalAmount") if isinstance(record, dict) else None
        value = _num(amount)
        if value is None:
            raise BillingUnreadable(f"GET {api}/billing: a record's totalAmount is {amount!r}, not a number")
        total += value
    return total


def _account_tag(api: str, key: str) -> str:
    # Whose billing a cached figure is: a key (account) switch must not reuse another's total.
    return hashlib.sha256(f"{api}\0{key}".encode()).hexdigest()[:16]


def billed_usd(api: str, key: str, state: Path, day: date, now: datetime) -> float:
    """Billed USD of one LOCAL day, summed over GET /billing's hourly records. Today's figure
    is cached in STATE/billing-today.json for 5 minutes (per API + key); another day is always
    read fresh."""
    today = now.astimezone().date()
    cache = state / "billing-today.json"
    account = _account_tag(api, key)
    if day == today:
        try:
            cached = json.loads(cache.read_text())
            usd, age = _num(cached["usd"]), now.timestamp() - float(cached["fetched"])
            if (cached["day"] == today.isoformat() and cached["account"] == account and usd is not None
                    and 0 <= age < BILLING_CACHE_S):
                return usd
        except (OSError, ValueError, KeyError, TypeError):
            pass  # no usable cache: read the API
    start, end = local_day_window(now) if day == today else day_window(day, now)
    if start >= end:
        return 0.0  # a day that has not begun yet
    usd = _fetch_billed(api, key, start, end)
    if day == today:
        state.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"day": today.isoformat(), "account": account, "fetched": now.timestamp(),
                                   "usd": usd}))
        tmp.replace(cache)
    return usd


def billed_today(api: str, key: str, state: Path, now: datetime) -> float:
    return billed_usd(api, key, state, now.astimezone().date(), now)


def live_usd_per_h(pods: list[dict[str, object]]) -> float:
    """Σ $/h of the account's pods that bill compute now (any status but EXITED/TERMINATED).
    A live pod without a usable price raises ValueError: it cannot be counted as free."""
    total = 0.0
    for pod in pods:
        if pod.get("status") in _DOWN_STATUSES:
            continue
        cost = _num(pod.get("cost"))
        if cost is None or cost < 0:
            raise ValueError(f"live pod {pod.get('id')!r} has no usable $/h (cost {pod.get('cost')!r})")
        total += cost
    return total


class Budget(NamedTuple):
    ok: bool
    line: str
    hours: float  # this run's --timeout
    live: float   # $ the live pods commit over those hours
    new: float    # $ the new pod commits over those hours
    total: float


def budget(billed: float, live_per_h: float, new_per_h: float, timeout_s: float, cap: float) -> Budget:
    """Whether a start/create fits today's cap: billed + (live pods' + the new pod's $/h) for
    this run's --timeout must stay below the cap."""
    hours = timeout_s / 3600
    live, new = live_per_h * hours, new_per_h * hours
    total = billed + live + new
    ok = total < cap
    line = (f"budget: today {_usd(billed)} billed + {_usd(live)} live/committed + {_usd(new)} this run"
            f" = {_usd(total)} of {_usd(cap)} cap → {'ok' if ok else 'REFUSE'}")
    return Budget(ok, line, hours, live, new, total)


def _read_pods(path: str) -> list[dict[str, object]]:
    """GET /pods objects from a file of JSON lines (what the script's all_pods prints)."""
    with open(path, encoding="utf-8") as fh:
        pods = [json.loads(raw) for raw in fh if raw.strip()]
    return [pod for pod in pods if isinstance(pod, dict)]


def _refuse_unverifiable(why: str, cap: float) -> int:
    print(f"budget: unverifiable ({why}) → REFUSE")
    print(f"runpod-run: daily budget: {why}; refusing to start or create a pod without a verified budget"
          f" (fail closed); {_escalation('unverifiable', f'{why}; cap {_usd(cap)}')}", file=sys.stderr)
    return EXIT_BUDGET


def _positive(unit: str) -> Callable[[str], float]:
    """An argparse type: a finite number above 0 (0 seconds of --timeout would pass any cap)."""
    def parse(text: str) -> float:
        value = _price(text)
        if not value:  # None, or 0
            raise argparse.ArgumentTypeError(f"wants a positive number of {unit}, not {text!r}")
        return value
    return parse


def _price(text: str) -> float | None:
    try:
        value = _num(float(text))
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def _cmd_budget(args: argparse.Namespace, _command: list[str]) -> int:
    now = datetime.now().astimezone()
    today = now.date()
    try:
        config = Path(args.config_dir) if args.config_dir else account_config_dir()
    except KeyError:  # a uid without a passwd entry: the owner's files cannot be located
        return _refuse_unverifiable(f"uid {os.getuid()} has no passwd entry, so the budget files cannot be found",
                                    DEFAULT_DAILY_USD)
    cap = daily_cap(config, today)
    try:
        pods = _read_pods(args.live_json)
    except (OSError, ValueError) as exc:
        print(f"runpod-run: budget: cannot read the pod list {args.live_json}: {exc}", file=sys.stderr)
        return 2
    try:
        live = live_usd_per_h(pods)
    except ValueError as exc:
        return _refuse_unverifiable(str(exc), cap)
    key = os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        return _refuse_unverifiable("no API key to read today's billing with", cap)
    try:
        billed = billed_today(args.api, key, Path(args.state), now)
    except BillingUnreadable as exc:
        return _refuse_unverifiable(f"the billing API could not be read: {exc}", cap)
    new = _price(args.price)
    if new is None:
        return _refuse_unverifiable(f"the new pod's price is unknown (catalog unreadable: {args.price!r})", cap)
    verdict = budget(billed, live, new, args.timeout_s, cap)
    print(verdict.line)
    if verdict.ok:
        return 0
    detail = (f"{_usd(verdict.total)} committed for {today.isoformat()}: billed {_usd(billed)}, live pods"
              f" {_usd(verdict.live)}, this run {_usd(verdict.new)} over its {verdict.hours:g} h --timeout")
    summary = f"today {_usd(billed)} billed, {_usd(verdict.total)} with this run, of the {_usd(cap)} cap"
    print(f"runpod-run: daily budget reached ({detail}); no pod started or created. The cap is the"
          f" owner's: never edit its files; {_escalation('reached', summary)}", file=sys.stderr)
    return EXIT_BUDGET


def _cmd_check(args: argparse.Namespace, command: list[str]) -> int:
    if not command:
        print("runpod-run: policy check needs a command after --", file=sys.stderr)
        return 2
    refusal = check(command, args.cwd, args.shape, args.reason, args.remote_reason)
    if refusal is None:
        return 0
    print(f"runpod-run: {refusal}", file=sys.stderr)
    return EXIT_REFUSED


def _cmd_ledger_append(args: argparse.Namespace, _command: list[str]) -> int:
    try:
        fields = json.loads(args.json)
        record = ledger_record(fields, os.environ, datetime.now()) if isinstance(fields, dict) else None
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        print(f"runpod-run: ledger-append: {exc}", file=sys.stderr)
        return 2
    if record is None:
        print("runpod-run: ledger-append: the record must be a JSON object", file=sys.stderr)
        return 2
    ledger_append(Path(args.state) / "ledger.jsonl", record)
    return 0


def _cmd_report(args: argparse.Namespace, _command: list[str]) -> int:
    try:
        day = date.fromisoformat(args.day) if args.day else datetime.now().astimezone().date()
    except ValueError:
        print(f"runpod-run: --day wants YYYY-MM-DD, not {args.day!r}", file=sys.stderr)
        return 2
    billed = args.billed
    key = os.environ.get("RUNPOD_API_KEY", "")
    if billed is None and args.api and key:
        try:
            billed = billed_usd(args.api, key, Path(args.state), day, datetime.now().astimezone())
        except BillingUnreadable as exc:
            print(f"runpod-run: the billing API could not be read ({exc}); billed $ unknown", file=sys.stderr)
    sys.stdout.write(report(Path(args.state), day, billed, Path(args.runs) if args.runs else None))
    return 0


# --- pod slots (R2) --------------------------------------------------------------------------

EXIT_QUEUE_TIMEOUT = 22
MAX_LIVE_PODS = 3  # per host; a constant, never an option: agents could raise an option
QUEUE_POLL_S = 5.0
_PROC = Path("/proc")
_POD_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a marker file name: never a path or a dotfile
_TICKET_RE = re.compile(r"(\d+)-(\d+)")


class Slot(NamedTuple):
    pod_name: str
    pid: int  # the live runpod-run holding it: a run, or the --ttl timer of a pod a run kept


class _Ticket(NamedTuple):
    ns: int  # when its run joined the queue (time_ns): the FIFO order
    pid: int
    path: Path


def runpod_run_alive(pid: int, proc_root: Path = _PROC) -> bool:
    """Whether `pid` is a live runpod-run — by its command line, not just its existence, so a
    dead run's pid that the kernel recycled for another program holds nothing. (One recycled
    for another runpod-run reads as live until that one exits: a slot over-counted for a
    while, never one too few.)"""
    try:
        return b"runpod-run" in (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False


def _marker_pids(marker: Path) -> list[int]:
    try:
        words = marker.read_text(encoding="utf-8").split()
    except OSError:
        return []
    return [int(word) for word in words if word.isascii() and word.isdigit()]


def _holder(marker: Path, proc_root: Path) -> int | None:
    return next((pid for pid in _marker_pids(marker) if runpod_run_alive(pid, proc_root)), None)


def _markers(state: Path) -> list[Path]:
    root = state / "slots"
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:  # no run has taken a slot yet
        return []
    return [root / name for name in names if _POD_NAME_RE.fullmatch(name)]


def live_slots(state: Path, proc_root: Path = _PROC) -> list[Slot]:
    """The host's live pod slots, by pod name. A marker STATE/slots/<pod_name> lists pids, one
    per line: its holder, then — after a run took over the slot of a pod kept by --ttl — the
    timer it took it from, which still keeps that pod should the run end before ending the
    timer. The slot is live while one of them is a live runpod-run, so a SIGKILLed run's slot
    frees itself."""
    slots = []
    for marker in _markers(state):
        holder = _holder(marker, proc_root)
        if holder is not None:
            slots.append(Slot(marker.name, holder))
    return slots


def _queue(state: Path) -> list[_Ticket]:
    """Every ticket in STATE/queue (named <time_ns>-<pid>), oldest first."""
    root = state / "queue"
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    return sorted(_Ticket(int(match[1]), int(match[2]), root / name)
                  for name in names if (match := _TICKET_RE.fullmatch(name)))


def _waiting(state: Path, proc_root: Path) -> list[_Ticket]:
    return [ticket for ticket in _queue(state) if runpod_run_alive(ticket.pid, proc_root)]


@contextmanager
def _slots_lock(state: Path) -> Iterator[None]:
    """STATE/slots.lock: every change to the slots and the queue happens under it, one check at
    a time. A queued caller never holds it while it waits."""
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "slots.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_pids(marker: Path, pids: list[int]) -> None:
    # Replaced whole, so a reader that takes no lock (list, a dry run) never sees half a file.
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f".{marker.name}.tmp")
    tmp.write_text("".join(f"{pid}\n" for pid in pids), encoding="utf-8")
    os.replace(tmp, marker)


def _sweep(state: Path, proc_root: Path) -> None:
    """Under the lock: drop the markers and tickets that no live runpod-run holds any more."""
    for marker in _markers(state):
        if _holder(marker, proc_root) is None:
            marker.unlink(missing_ok=True)
    for ticket in _queue(state):
        if not runpod_run_alive(ticket.pid, proc_root):
            ticket.path.unlink(missing_ok=True)


def _check_pod_name(pod_name: str) -> None:
    if not _POD_NAME_RE.fullmatch(pod_name):
        raise ValueError(f"not a pod name: {pod_name!r}")


def acquire(state: Path, pod_name: str, pid: int, timeout_s: float, max_pods: int = MAX_LIVE_PODS,
            on_position: Callable[[int, int, list[Slot]], object] | None = None, *, proc_root: Path = _PROC,
            poll_s: float = QUEUE_POLL_S, clock: Callable[[], float] = monotonic,
            sleep: Callable[[float], object] = sleep) -> bool:
    """Take one of the host's `max_pods` pod slots for pod_name on behalf of the runpod-run `pid`.

    True at once when pod_name's own slot is live — a pod kept by --ttl, which this run reuses:
    `pid` takes that slot over (the previous holder stays on the marker's next line, see
    release) and no new slot is used. Otherwise the caller joins a FIFO queue
    (STATE/queue/<time_ns>-<pid>) and is admitted once fewer live callers are ahead of it than
    there are free slots; on_position(position, waiting, live) hears every change of its
    position. Each check takes STATE/slots.lock; the wait between checks (`poll_s`) does not.
    False after timeout_s, or as soon as `pid` is no longer a live runpod-run (a run killed
    while it queued)."""
    _check_pod_name(pod_name)
    marker = state / "slots" / pod_name
    deadline = clock() + timeout_s
    ticket: _Ticket | None = None
    position = 0
    try:
        while True:
            with _slots_lock(state):
                if not runpod_run_alive(pid, proc_root):
                    return False
                _sweep(state, proc_root)
                held = _marker_pids(marker)  # after the sweep: this pod's live slot, or nothing
                if held:
                    if held[0] != pid:
                        _write_pids(marker, [pid, *(p for p in held if p != pid and runpod_run_alive(p, proc_root))])
                    return True
                if ticket is None:
                    ns = time_ns()
                    ticket = _Ticket(ns, pid, state / "queue" / f"{ns}-{pid}")
                    ticket.path.parent.mkdir(exist_ok=True)
                    ticket.path.touch(exist_ok=False)
                live = live_slots(state, proc_root)
                waiting = _waiting(state, proc_root)
                ahead = sum(1 for other in waiting if (other.ns, other.pid) < (ticket.ns, ticket.pid))
                if ahead < max_pods - len(live):
                    _write_pids(marker, [pid])
                    ticket.path.unlink(missing_ok=True)
                    ticket = None
                    return True
            if ahead + 1 != position:
                position = ahead + 1
                if on_position is not None:
                    on_position(position, len(waiting), live)
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            sleep(min(poll_s, remaining))
    finally:
        if ticket is not None:
            ticket.path.unlink(missing_ok=True)


def handover(state: Path, pod_name: str, new_pid: int) -> None:
    """new_pid alone holds pod_name's slot from now on: the --ttl timer of a pod its run kept,
    so the kept pod counts as live until the timer's --stop terminates it."""
    _check_pod_name(pod_name)
    with _slots_lock(state):
        _write_pids(state / "slots" / pod_name, [new_pid])


def release(state: Path, pod_name: str, pid: int, *, proc_root: Path = _PROC) -> None:
    """`pid` gives pod_name's slot up: its pod was terminated, or never came. The marker goes,
    or goes back to the holder `pid` took it over from while that one lives — a run that took
    over a kept pod's slot but ended before ending its --ttl timer leaves the pod, and the
    slot, to that timer. A slot another live runpod-run holds is left alone."""
    _check_pod_name(pod_name)
    marker = state / "slots" / pod_name
    with _slots_lock(state):
        pids = _marker_pids(marker)
        if not pids or (pids[0] != pid and runpod_run_alive(pids[0], proc_root)):
            return
        rest = [other for other in pids[1:] if other != pid and runpod_run_alive(other, proc_root)]
        if rest:
            _write_pids(marker, rest)
        else:
            marker.unlink(missing_ok=True)


def slot_plan(state: Path, pod_name: str, max_pods: int = MAX_LIVE_PODS, proc_root: Path = _PROC) -> str:
    """What acquire would do for pod_name now, without doing it (a dry run)."""
    live = live_slots(state, proc_root)
    if any(slot.pod_name == pod_name for slot in live):
        return "reuse (this pod holds a live slot)"
    waiting = len(_waiting(state, proc_root))
    if waiting < max_pods - len(live):
        return "take a free slot"
    return f"queue (position {waiting + 1} of {waiting + 1})"


def _live_names(live: list[Slot]) -> str:
    return ", ".join(slot.pod_name for slot in live) or "none"


def _holder_kind(pid: int) -> str:
    try:
        cmdline = (_PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return "gone"
    # The timer is `bash -c 'sleep N && exec …/runpod-run --stop <id>'`, then that --stop.
    return "--ttl timer" if b"--stop" in cmdline else "run"


def _cmd_slots_acquire(args: argparse.Namespace, _command: list[str]) -> int:
    state = Path(args.state)
    queued_at: float | None = None

    def report(position: int, waiting: int, live: list[Slot]) -> None:
        nonlocal queued_at
        if queued_at is None:
            queued_at = monotonic()
        print(f"runpod-run: queued for a pod slot (position {position} of {waiting};"
              f" {len(live)} live: {_live_names(live)})", file=sys.stderr)

    if acquire(state, args.name, args.pid, args.timeout_min * 60, on_position=report):
        if queued_at is not None:
            print(f"runpod-run: got a pod slot after {monotonic() - queued_at:.0f} s in the queue", file=sys.stderr)
        return 0
    if not runpod_run_alive(args.pid):
        print(f"runpod-run: slots: pid {args.pid} is not a live runpod-run; nothing queues for it", file=sys.stderr)
        return 2
    live = live_slots(state)
    print(f"runpod-run: queue timeout: no pod slot came free within {args.timeout_min:g} min ({len(live)} live:"
          f" {_live_names(live)}); no pod was started. Run it later, or end a --ttl-kept pod you no longer"
          " need (runpod-run --status; runpod-run --stop <pod-id>)", file=sys.stderr)
    return EXIT_QUEUE_TIMEOUT


def _cmd_slots_handover(args: argparse.Namespace, _command: list[str]) -> int:
    handover(Path(args.state), args.name, args.pid)
    return 0


def _cmd_slots_release(args: argparse.Namespace, _command: list[str]) -> int:
    release(Path(args.state), args.name, args.pid)
    return 0


def _cmd_slots_list(args: argparse.Namespace, _command: list[str]) -> int:
    state = Path(args.state)
    live = live_slots(state)
    line = f"slots: {len(live)}/{MAX_LIVE_PODS} live ({_live_names(live)}), {len(_waiting(state, _PROC))} queued"
    if args.name:
        line += f" → {slot_plan(state, args.name)}"
    print(line)
    for slot in live:
        print(f"  {slot.pod_name:<32} pid {slot.pid} ({_holder_kind(slot.pid)})")
    return 0


def _pod_name(text: str) -> str:
    if not _POD_NAME_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"wants a pod name (letters, digits, '.', '_', '-'), not {text!r}")
    return text


def _pid(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) == 0:
        raise argparse.ArgumentTypeError(f"wants a process id, not {text!r}")
    return int(text)


def _cmd_account_home(_args: argparse.Namespace, _command: list[str]) -> int:
    try:
        print(account_home())
    except KeyError:
        print(f"runpod-run: uid {os.getuid()} has no passwd entry", file=sys.stderr)
        return 2
    return 0


# --- dedup (R7) ---------------------------------------------------------------------------------
DEDUP_WINDOW = timedelta(hours=24)
# Exit codes of a run whose command never ran (usage, pod failure, policy, budget, queue, no
# ssh, ssh lost): nothing to replay.
_NOT_RUN_EXITS = frozenset({2, 3, 20, 21, 22, 23, 255})
_REPO_TAG_RE = _POD_NAME_RE  # a file name under STATE/dedup: never a path or a dotfile
_NEVER_SYNCED = frozenset({".git", "node_modules", ".venv"})  # the fixed rsync excludes


class Hit(NamedTuple):
    ts: str  # the stored run's local ISO time
    exit: int
    log: str


def _git_entries(root: Path) -> list[tuple[str, str]] | None:
    """(path, content id) of every file a sync sends from a git checkout: tracked + untracked
    not ignored, content via `git hash-object`. None outside git."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=False)
    if listed.returncode != 0:
        return None
    # Inside an uninitialized (empty) submodule dir, git answers for the parent repo and lists
    # the gitlink itself as "./": that is this very dir, not an entry of it (and recursing into
    # it never ends). rsync sends an empty dir's contents as nothing.
    names = sorted({name.decode("utf-8", "surrogateescape") for name in listed.stdout.split(b"\0")
                    if name and name.rstrip(b"/") not in (b"", b".")})
    entries: list[tuple[str, str]] = []
    batch: list[str] = []
    for name in names:
        path = root / name
        kind = _kind(path)
        if kind not in ("file", "file+x") or "\n" in name:  # --stdin-paths is line-based
            entries.append((name, _entry(path, kind)))
        else:
            batch.append(name)
    if batch:
        hashed = subprocess.run(["git", "-C", str(root), "hash-object", "--no-filters", "--stdin-paths"],
                                input="".join(f"{name}\n" for name in batch).encode("utf-8", "surrogateescape"),
                                capture_output=True, check=True)
        entries.extend((name, f"{_kind(root / name)}:{blob}")
                       for name, blob in zip(batch, hashed.stdout.decode().split(), strict=True))
    return sorted(entries)


def _kind(path: Path) -> str:
    """What rsync -a sends for `path`: its type, and for a file whether it is executable."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"  # a tracked file deleted on disk
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "file+x" if mode & stat.S_IXUSR else "file"
    return "dir" if stat.S_ISDIR(mode) else "special"


def _entry(path: Path, kind: str) -> str:
    """A content id that never opens a FIFO or a device: only regular files are read."""
    if kind == "link":
        return "link:" + os.readlink(path)
    if kind in ("file", "file+x"):
        return f"{kind}:{_file_sha256(path)}"
    if kind == "dir":  # a submodule checkout: rsync sends its contents
        return "dir:" + _dir_digest(path)
    return kind


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_entries(root: Path) -> list[tuple[str, str]]:
    """(relative path, content id) of every entry under a dir outside git, minus what never
    syncs. A symlinked dir is recorded as the link (rsync -a copies the link, not its target)."""
    entries: list[tuple[str, str]] = []
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NEVER_SYNCED and not (top == str(root) and d == ".worktrees")]
        links = [d for d in dirs if (Path(top) / d).is_symlink()]
        dirs[:] = [d for d in dirs if d not in links]
        for name in [*files, *links]:
            path = Path(top) / name
            entries.append((os.fsdecode(path.relative_to(root)), _entry(path, _kind(path))))
    return sorted(entries)


def _dir_digest(root: Path) -> str:
    digest = hashlib.sha256()
    entries = _git_entries(root)
    digest.update(b"git\0" if entries is not None else b"dir\0")
    for name, content in entries if entries is not None else _plain_entries(root):
        digest.update(f"{name}\0{content}\0".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def tree_key(dirs: list[Path], cmd: str, shape: str) -> str:
    """What makes two runs the same: every synced dir's files, contents and modes, in argument
    order, plus the quoted command and the run's shape."""
    digest = hashlib.sha256()
    for root in dirs:
        digest.update(f"{_dir_digest(root)}\0".encode())
    digest.update(f"cmd\0{cmd}\0shape\0{shape}".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _dedup_file(state: Path, repo_tag: str) -> Path:
    if not _REPO_TAG_RE.fullmatch(repo_tag):
        raise ValueError(f"not a repo tag: {repo_tag!r}")
    return state / "dedup" / f"{repo_tag}.json"


def lookup(state: Path, repo_tag: str, key: str, now: datetime) -> Hit | None:
    """The stored run of repo_tag when it had this key and ended less than 24 h before `now`."""
    try:
        record = json.loads(_dedup_file(state, repo_tag).read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(record["ts"])
        hit = Hit(record["ts"], int(record["exit"]), str(record["log"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if record.get("key") != key or ts.tzinfo is None or not timedelta(0) <= now - ts < DEDUP_WINDOW:
        return None
    return hit


def store(state: Path, repo_tag: str, key: str, exit_code: int, log_path: Path, now: datetime) -> None:
    """Record a run whose command ran; one record per repo_tag, the latest wins."""
    path = _dedup_file(state, repo_tag)
    if exit_code in _NOT_RUN_EXITS:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    local = now if now.tzinfo else now.astimezone()  # a naive `now` is local time
    tmp.write_text(json.dumps({"key": key, "ts": local.isoformat(timespec="seconds"),
                               "exit": exit_code, "log": str(log_path)}) + "\n", encoding="utf-8")
    tmp.replace(path)


def _cmd_dedup_key(args: argparse.Namespace, _command: list[str]) -> int:
    try:
        print(tree_key([Path(d) for d in args.dir], args.cmd, args.shape))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"runpod-run: dedup key: {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_dedup_lookup(args: argparse.Namespace, _command: list[str]) -> int:
    hit = lookup(Path(args.state), args.repo_tag, args.key, datetime.now().astimezone())
    if hit is None:
        return 1
    print(json.dumps(hit._asdict()))
    return 0


def _cmd_dedup_store(args: argparse.Namespace, _command: list[str]) -> int:
    store(Path(args.state), args.repo_tag, args.key, args.exit, Path(args.log), datetime.now())
    return 0


def _repo_tag(text: str) -> str:
    if not _REPO_TAG_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"wants a repo tag (letters, digits, '.', '_', '-'), not {text!r}")
    return text


def main(argv: list[str]) -> int:
    # Everything after the first `--` is the command, verbatim: argparse must never read its
    # options (`pytest -k foo`) as the helper's own.
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    else:
        command = []
    parser = argparse.ArgumentParser(prog="runpod_run_policy.py", description="runpod-run spend policy")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    p_check = sub.add_parser("check", help="refuse a run that should not rent a pod (exit 20)")
    p_check.add_argument("--cwd", required=True, help="the synced checkout the command runs in")
    p_check.add_argument("--shape", required=True, help="cpu<gen><family>-<vcpus> or gpu:<type id>")
    p_check.add_argument("--reason", default="")
    p_check.add_argument("--remote-reason", default="")
    p_check.set_defaults(handler=_cmd_check)
    p_ledger = sub.add_parser("ledger-append", help="append one run's line to STATE/ledger.jsonl")
    p_ledger.add_argument("--state", required=True, help="runpod-run's state dir")
    p_ledger.add_argument("--json", required=True, help="the line's fields, a JSON object")
    p_ledger.set_defaults(handler=_cmd_ledger_append)
    p_report = sub.add_parser("report", help="one local day's spend by category")
    p_report.add_argument("--state", required=True, help="runpod-run's account state dir (the ledger)")
    p_report.add_argument("--runs", default="", help="the run-log dir for a day without ledger lines (default STATE/runs)")
    p_report.add_argument("--day", default="", help="YYYY-MM-DD, default today (local)")
    p_report.add_argument("--billed", type=float, default=None, help="billed USD for the day, when known")
    p_report.add_argument("--api", default="", help="Runpod REST base to read the billed $ from (key: RUNPOD_API_KEY)")
    p_report.set_defaults(handler=_cmd_report)
    p_budget = sub.add_parser("budget", help="refuse a start/create that would break the daily $ cap (exit 21)")
    p_budget.add_argument("--api", required=True, help="Runpod REST base (key: RUNPOD_API_KEY)")
    p_budget.add_argument("--state", required=True, help="runpod-run's state dir (billing cache)")
    p_budget.add_argument("--price", required=True, help="the new pod's $/h; empty when unknown")
    p_budget.add_argument("--timeout-s", type=_positive("seconds"), required=True, help="this run's --timeout in seconds")
    p_budget.add_argument("--live-json", required=True, help="file of GET /pods objects, one JSON per line")
    p_budget.add_argument("--config-dir", default="",
                          help="the budget files' dir; default ~/.config/runpod-run of the account's passwd home")
    p_budget.set_defaults(handler=_cmd_budget)
    p_slots = sub.add_parser("slots", help=f"at most {MAX_LIVE_PODS} live pods per host; a FIFO queue (exit 22)")
    slot_actions = p_slots.add_subparsers(dest="action", required=True)
    p_acquire = slot_actions.add_parser("acquire", help="take a pod slot for --name, or queue for one")
    p_handover = slot_actions.add_parser("handover", help="--pid (a --ttl timer) holds --name's slot from now on")
    p_release = slot_actions.add_parser("release", help="--pid gives --name's slot up")
    p_list = slot_actions.add_parser("list", help="the live slots; with --name, what a run of that pod would do")
    for action, handler in ((p_acquire, _cmd_slots_acquire), (p_handover, _cmd_slots_handover),
                            (p_release, _cmd_slots_release)):
        action.add_argument("--state", required=True, help="runpod-run's state dir")
        action.add_argument("--name", required=True, type=_pod_name, help="the pod's name (rr-<host>-…)")
        action.add_argument("--pid", required=True, type=_pid, help="the runpod-run process (a run or a --ttl timer)")
        action.set_defaults(handler=handler)
    p_acquire.add_argument("--timeout-min", required=True, type=_positive("minutes"),
                           help="give up after this long in the queue (exit 22)")
    p_list.add_argument("--state", required=True, help="runpod-run's state dir")
    p_list.add_argument("--name", default=None, type=_pod_name, help="the pod a run would start")
    p_list.set_defaults(handler=_cmd_slots_list)
    p_home = sub.add_parser("account-home", help="the account's home from the passwd database (never $HOME)")
    p_home.set_defaults(handler=_cmd_account_home)
    p_key = sub.add_parser("dedup-key", help="the dedup key of a run: synced files + command + shape")
    p_key.add_argument("--dir", action="append", required=True, help="a synced dir, in argument order (repeatable)")
    p_key.add_argument("--cmd", required=True, help="the quoted command")
    p_key.add_argument("--shape", required=True, help="the compute shape (wanted_shape)")
    p_key.set_defaults(handler=_cmd_dedup_key)
    p_lookup = sub.add_parser("dedup-lookup", help="the identical run of the last 24 h as JSON; exit 1 when none")
    p_store = sub.add_parser("dedup-store", help="record a run whose command ran")
    for action in (p_lookup, p_store):
        action.add_argument("--state", required=True, help="runpod-run's account state dir")
        action.add_argument("--repo-tag", required=True, type=_repo_tag)
        action.add_argument("--key", required=True)
    p_store.add_argument("--exit", required=True, type=int, help="the run's exit code")
    p_store.add_argument("--log", required=True, help="the run's log file")
    p_lookup.set_defaults(handler=_cmd_dedup_lookup)
    p_store.set_defaults(handler=_cmd_dedup_store)
    args = parser.parse_args(argv)
    return args.handler(args, command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
