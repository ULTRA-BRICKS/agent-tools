# agent-tools `lib/`

Reusable library code + language-agnostic **contracts** the ecosystem CLIs depend on.

| Path | What it is |
| --- | --- |
| `agenttools_log/` | Shared **structured JSONL logging** (stdlib-only) — documented below. |
| `agent_hooks_v1/` | Shared **`agents-hooks/v1` descriptor runner** used by harness bridges. It loads local descriptor JSON, preserves priority ordering, runs absolute hook commands with the v1 JSON payload, and applies exit-10 / `on_error` semantics. stdlib-only. |
| `cc_hook_bridge/` | The **`agents-hooks/v1` → Claude Code bridge** — a dispatcher CC's `settings.json` PreToolUse/Stop hooks call that runs the installed `~/.claude/hooks/*.json` descriptors and translates exit-10 BLOCK into CC's `permissionDecision: "deny"` / `decision: "block"`. Without it, every agent-hook is INERT in CC (agent-tools#18). stdlib-only. See [`cc_hook_bridge/README.md`](cc_hook_bridge/README.md). |
| `codex_hook_bridge/` | The **`agents-hooks/v1` → Codex bridge** — a dispatcher Codex TOML hooks call that runs installed `~/.codex/hooks/*.json` descriptors and translates exit-10 BLOCK into Codex's plain `{"decision":"block","reason":"…"}` shape. Maps `PreToolUse` `Bash`→`pre-bash`, `apply_patch`→`pre-write`, `PostToolUse` `apply_patch`→`post-write`, and `Stop`→`stop`; `pre-agent` waits for a trustworthy `SubagentStart` fixture. stdlib-only. See [`codex_hook_bridge/README.md`](codex_hook_bridge/README.md). |
| `opencode_hook_bridge/` | The **`agents-hooks/v1` -> opencode bridge** - a JavaScript plugin plus Python dispatcher. Rig symlinks the plugin into the repo-local `.opencode/plugins/zz-agent-tools-hook-bridge.js` ordered path, calls the dispatcher for `tool.execute.before` / `tool.execute.after`, runs installed `~/.config/opencode/hooks/*.json` descriptors, and throws only to block pre-tool opencode calls. Maps `bash`->`pre-bash`, `edit`/`write`/`apply_patch`->`pre-write`, `task`->`pre-agent`, `skill`->`pre-skill`, and completed writes->`post-write` feedback; no `stop` mapping because opencode's session events are notification hooks, not a pre-stop block contract. stdlib-only Python + dependency-free JS. See [`opencode_hook_bridge/README.md`](opencode_hook_bridge/README.md). |
| `omp_hook_bridge/` | The **`agents-hooks/v1` -> omp (oh-my-pi) bridge** - a TS extension module (`pi.on(...)`) plus Python dispatcher. Rig symlinks the extension into omp's extension directory (default `~/.omp/agent/extensions`), calls the dispatcher for `tool_call` / `tool_result`, runs installed `~/.omp/agent/hooks/*.json` descriptors, and translates an explicit block decision into `{block: true, reason}` — but, unlike opencode's plugin, NEVER throws on a bridge-infrastructure failure (fails OPEN on `tool_call` too, since omp is an interactive daily-driver CLI where a throw would brick every tool call). Maps `bash`->`pre-bash`, `edit`/`write`/`apply_patch`/`notebook`->`pre-write`, `task`->`pre-agent`, and completed writes->`post-write` feedback; `yield`->`stop`` mapping because omp's session-lifecycle events document no pre-stop block contract. stdlib-only Python + dependency-free TS. See [`omp_hook_bridge/README.md`](omp_hook_bridge/README.md). |
| `agenttools_config/` | Generalized **two-layer config-cascade loader** — global `~/.config/<tool>/config.yaml` (XDG-aware) + per-repo `<tool>.yaml`, deep-merged with the repo file winning, plus a pluggable `schema_validate` hook. Tool-agnostic (owns the cascade/path logic, not any tool's schema), stdlib-only at import (lazy PyYAML). Extracted from `rig-cli`'s `riglib.config`. See [`agenttools_config/README.md`](agenttools_config/README.md). |
| `advertise/` | The shared **`install-skill` core** every tool CLI duplicates — write `SKILL.md` into `~/.agents/skills/<tool>/` and symlink it into each harness discovery dir (`~/.claude/skills`), idempotently, with rig's safe-symlink discipline (re-point a wrong link, never clobber a real dir). stdlib-only. See [`advertise/README.md`](advertise/README.md). |
| `agenttools_registry/` | The shared **trust kernel + trust-gated registry** — one importable home for the algorithm `review-cli`'s visual-module registry and `tg-cli`'s `agents-hooks/v1` trust gate re-implement in two languages: trust-by-default, opt-in TOFU sha-pin guard, an `auto` escape hatch, inert-not-blocking quarantine, append-only audit. The consumer owns HOW to run an entry; the kernel owns WHETHER. stdlib-only. See [`agenttools_registry/README.md`](agenttools_registry/README.md). |
| `agenttools_daemon/` | A small **dependency-free process supervisor** — keep a child alive across crashes/kills with exponential backoff, a pidfile as source of truth, and `start`/`stop`/`status`/`restart`/`run_forever`. The one shared copy `task-cli` and `tg-ctl` babysit long-running children with, instead of each hand-rolling a `while True: spawn; sleep`. Every time/process seam is injectable (testable with a fake clock + process). stdlib-only. See [`agenttools_daemon/README.md`](agenttools_daemon/README.md). |
| `agenttools_providers/` | The tool-agnostic **CORE of the multi-model provider abstraction** (stdlib-only at import): a capability-tagged model registry, role→model resolution honoring tags (role `vision` resolves only to vision-capable), a priority-ordered **failover board** (top-N reachable + reserve backfill), and a **key cascade** (env-name precedence, then `.env` files). Decides *which* model/seat/key; transports (`oc:` routing, live calls) stay in the consuming tool. Loads `contracts/models.yaml`. See [`agenttools_providers/README.md`](agenttools_providers/README.md). |
| `agenttools_tmux_inject/` | **Inject text/keys into a named tmux pane/session** — a finishing agent posts a line into *another* agent's interactive pane via tmux `send-keys` (e.g. "done, X unblocked"). Never-raises `InjectResult` so a completion hook can fire-and-forget; stdlib-only (subprocess + shutil). See [`agenttools_tmux_inject/README.md`](agenttools_tmux_inject/README.md). |
| `agenttools_stall_watchdog/` | **Tiered warn/abort stall detection** for a long-running process by one watched file's mtime (a subagent transcript or any e2e/test run's log): WARN (default 5 min) nudges the watching agent's tmux pane in English via `agenttools_tmux_inject`; ABORT (default 30 min) kills the pid's process group when one is known and sends the human a Russian explicit-field `tg` alert with the exact diagnostic pointer. Edge-triggered, fake-clock-testable `Watchdog` core; runs as a `Monitor` command or a standalone daemon. stdlib + `agenttools-tmux-inject`. See [`agenttools_stall_watchdog/README.md`](agenttools_stall_watchdog/README.md). |
| `agenttools_gpu_slots/` | The **machine-wide GPU-model slot pool** (agent-tools#817) — `RIG_GPU_SLOTS` (default 2) `flock` lock files shared by every agent and harness; a running model holds one for its lifetime and the kernel frees it the instant the holder dies (SIGKILL included), so no slot can go stale. `python3 lib/agenttools_gpu_slots run -- <cmd>` waits for a slot, then execs the command holding it; `status` lists holders. stt-cli implements the same protocol; `heavy-op-memory-gate` probes it. stdlib-only, POSIX. See [`agenttools_gpu_slots/README.md`](agenttools_gpu_slots/README.md). |
| `agenttools_errors/` | The shared **error/exit-code layer** (error-system v2) — one structured **what/why/how-to-fix** error type with **stable per-class exit codes** (`0/1/2/3/4/5/6/7/8/127`, a PUBLIC CONTRACT) every CLI raises and `guard()` renders, plus the did-you-mean + removed-slot + missing-dependency builders. Generalized from `rig-cli/riglib/errors.py` so the whole ecosystem shares one error shape. stdlib-only. See [`agenttools_errors/README.md`](agenttools_errors/README.md). |
| `agenttools_help/` | The shared **help formatter** — one colorized help layer (palette, sections, usage line(s), subcommands list, **ACTUAL defaults**, **scoped** subcommand options, **topic-help** `<tool> help <topic>`, **install-state** `✓`/`○`/`⚠`) built as data, so the help-clarity rules live in one place and a fix lands across `review`/`rig`/`tg`/`draw`/`3d`/`task` at once. Includes `options_from_argparse` introspection. stdlib-only. See [`agenttools_help/README.md`](agenttools_help/README.md). |
| `contracts/models.yaml` | The **model board**: the current-best concrete model per provider, each tagged with capabilities (esp. `vision`), plus a symbolic `roles:`/`aliases:` map. Single source of truth for `review-cli`/`task-cli`/future tools. Validated by `contracts/models.schema.json` (JSON-Schema) + the checker's cross-reference `--validate`. Loaded by `agenttools_providers.load_registry`. |
| `checker/` | The **model-freshness checker** (`model_freshness.py`) — a daily job that polls provider model-list endpoints and PROPOSES version bumps for `contracts/models.yaml` (a PR via `gh`, else a dated report). Semi-automatic: it proposes, a human confirms. rig provisions it as a daily noon cron. See [`checker/README.md`](checker/README.md). |

---

# agenttools-log

Shared **structured JSONL logging** for the agent-tools ecosystem — one JSON object per
line, **stdlib only** (no `pino`, no `structlog`, no `python-json-logger`). It is a thin,
generalized wrapper over the stdlib `logging` module so `review-cli`, `rig-cli`, and any
future Python CLI emit logs in the same shape.

## Record shape

Every line is a JSON object with at least these keys, plus any structured fields you pass:

```json
{"ts":"2026-06-15T09:00:00.123456+00:00","level":"INFO","logger":"myapp.server","msg":"server started","port":8080,"pid":1234}
```

| Key      | Meaning                                              |
| -------- | ---------------------------------------------------- |
| `ts`     | ISO-8601 timestamp, **UTC**                          |
| `level`  | `DEBUG` / `INFO` / `WARNING` / `ERROR`               |
| `logger` | module name (`agenttools.<name>`)                    |
| `msg`    | the log message                                      |
| `exc`    | formatted traceback (only when `exc_info` is passed) |
| `stack`  | formatted stack (only when `stack_info` is passed)   |
| *…*      | any structured fields you pass as kwargs             |

## Usage

```python
from agenttools_log import get_logger

log = get_logger(__name__)

log.debug("cache lookup", key="user:42", hit=False)
log.info("server started", port=8080, pid=1234)
log.warn("retry", attempt=2, url="https://api.example.com")

# stdlib-style lazy %-formatting also works (positional args after the message):
log.info("user %s connected from %s", user_id, ip)

try:
    risky()
except Exception:
    log.error("request failed", request_id="abc", exc_info=True)
    # or, equivalently, inside an except block:
    log.exception("request failed", request_id="abc")
```

Positional arguments after the message feed stdlib's lazy `%`-formatting (so migrating
existing `log.info("x=%s", x)` call sites Just Works). Arbitrary keyword arguments become
top-level JSON fields. `exc_info`, `stack_info`, and
`stacklevel` are passed through to stdlib instead of being logged as fields (`exc_info`
adds an `exc` field, `stack_info` adds a `stack` field). Reserved keys
(`ts`/`level`/`logger`/`msg`) can never be clobbered by a stray field — the message
parameter is positional-only, so even a field literally named `msg` is safe.

## Configuration

`get_logger` auto-configures the shared `agenttools` logger tree **once** from the
environment on first use. Override from code with `configure(...)` (e.g. a `--log-file`
flag) — explicitly-passed arguments win over the environment. An explicit `stream=`
suppresses `AGENTTOOLS_LOG_FILE`; pass `log_file=None` explicitly to force a stream sink
even when that env var is set.

| Env var                 | Values                          | Default | Effect                                  |
| ----------------------- | ------------------------------- | ------- | --------------------------------------- |
| `AGENTTOOLS_LOG_LEVEL`  | `debug`/`info`/`warn`/`error`   | `info`  | minimum level emitted                   |
| `AGENTTOOLS_LOG_FILE`   | a path                          | *(unset)* | append JSONL to this file (created `0600`); otherwise stderr |
| `AGENTTOOLS_LOG_FORMAT` | `json` / `pretty`               | `json`  | `pretty` = human-readable dev mode      |

```python
from agenttools_log import configure, get_logger

# Explicit override (e.g. driven by CLI flags):
configure(level="debug", fmt="pretty", log_file="/tmp/run.jsonl")
log = get_logger("mytool")
```

`pretty` mode renders a compact human line instead of JSON, for local development:

```
09:00:00 INFO  agenttools.myapp.server: server started  port=8080 pid=1234
```

## Safety posture

- **Never crashes the caller.** The formatter, every log call, and `configure` swallow
  their own errors — a logging failure degrades output, it does not raise.
- **`0600` file perms.** A file sink is `chmod`-ed to `0600` on every open, even for a
  pre-existing file, mirroring `review-cli`'s `stats.py` privacy posture. If the file
  can't be opened (or `chmod`-ed), it falls back to stderr rather than leave a
  world-readable log behind.
- **No automatic redaction.** Nothing is scrubbed for you — but structured fields make it
  trivial to log exactly what you intend and nothing more.

## Public API

| Symbol                                   | Purpose                                          |
| ---------------------------------------- | ------------------------------------------------ |
| `get_logger(name=None) -> StructuredLogger` | per-module logger; auto-configures from env   |
| `configure(*, level, fmt, stream, log_file, force=True)` | explicit (re)configuration       |
| `StructuredLogger`                       | the per-module facade (`.debug/.info/.warn/.error/.exception`) |
| `reset()`                                | tear down configuration (mainly for tests)       |
| `DEBUG` / `INFO` / `WARNING` / `ERROR`   | re-exported stdlib level ints                    |

`StructuredLogger.stdlib` exposes the underlying `logging.Logger` for callers that need
native access; `StructuredLogger.isEnabledFor(level)` mirrors the stdlib guard.

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the `agenttools-log`
distribution. A consumer (e.g. `review-cli`, `rig-cli`) depends on it like any package:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-log"]
```

For local/dev installs from the umbrella checkout:

```sh
pip install -e /path/to/agent-tools/lib      # editable install of agenttools-log
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib python -c "from agenttools_log import get_logger"
```

Then in code:

```python
from agenttools_log import get_logger
log = get_logger(__name__)
```

### Migration seam for existing consumers

`rig-cli`'s `riglib.logging` already emits this exact shape by hand; it can become a thin
re-export once this lands (`from agenttools_log import get_logger, configure`), keeping its
`RIG_LOG*` env names as aliases if desired. `review-cli` currently prints to stderr with no
log dependency; it can adopt `agenttools_log` incrementally for its structured channels.
**Wiring those CLIs is a deliberate follow-up — this package ships standalone.**
