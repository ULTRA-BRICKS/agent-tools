# no-long-inline-process

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 35

A long-running process started inline by the **orchestrator** blocks the main thread for
minutes. This gate **hard-blocks** it (this is the most clear-cut case, so straight block,
not warn-first) and tells the orchestrator to dispatch it to a **background subagent**.
Enforces the "stay responsive" half of `delegate-work-to-subagents`.

Blocked (argv-aware: the command is shell-tokenized and a long process is flagged only when the
**real invoked command** — `argv[0]` after peeling inline env + no-op wrappers — is the runner, so a
substring in a path/word or a keyword inside a normal multi-word quoted argument to a different
command (`tg --title x "…review…"`, `git commit -m "wire --watch"`) does not trip it. One documented
residual over-block remains: a quoted argument equal to *exactly* a lone shell separator immediately
followed by a runner (`tg "(" review`) — see the module docstring's RESIDUAL LIMITATION):

- the **`review`** CLI invoked as a command (multi-model, minutes-long)
- any **`--watch`** flag (`gh pr checks --watch`, `vitest --watch`, `tsc --watch`, …)
- a **build/test suite**: `npm|pnpm|yarn|bun|deno test|build`, `pytest`/`vitest`/`jest`/
  `cypress`/`playwright`, `cargo test|build`, `go test|build`, `make|rake|msbuild test|build|all`,
  `mvn|gradle test|build|verify|package`
- **`sleep N`** with `N >= 10` (short sleeps like `sleep 2` are fine)

Leading **no-op wrappers** are peeled off each segment before matching, so a wrapped long
process is still caught — `timeout 600 npm test`, `env CI=1 pytest`, `timeout 5m review`,
`nice -n10 review`, `time make build`, `stdbuf -oL pytest`, `nohup cargo build`. The wrapper's
own args (`timeout`'s duration, `env`'s `KEY=VALUE` assignments, `-k`/`-n`/`--signal` flags) are
skipped; only the *wrapped* command's long-running-ness decides (`timeout 5 ls` stays allowed).

**Subagent-exempt:** a dispatched subagent (`agent_id` present) is *expected* to run these in
the background, so it is always allowed. This gate governs the orchestrator only. `args.agent_id`
must originate from a **trusted, transport-level** signal — never from model-controlled
`tool_input`. `cc_hook_bridge` forwards it only from CC's top-level event and drops any
`tool_input`-forged copy; a non-CC carrier must replicate that filtering or a forged `agent_id`
self-exempts the orchestrator (full contract in `background-subagent-gate/README.md`).

## Why a hard block (not warn-first)

The other delegation gates warn-then-block, but a long inline process is unambiguous: it WILL
stall the main thread, every time. There is no borderline case to warn about — so this one
blocks on the first occurrence, deny-by-default, with an external Telegram approval for the
rare deliberate exception.

## No self-service bypass — external Telegram approval only

There is **no** env-var or inline escape hatch any more. The old `ALLOW_INLINE_PROCESS=1` +
`ALLOW_INLINE_PROCESS_REASON` env and the `# inline-process-ok:` inline sentinel let the very
agent this gate constrains grant itself an exception — security theater, not a permission gate.
Both were removed.

The block is now **deny-by-default**. For a genuine one-off exception, ASK the human, or request
a one-time Telegram approval with a written justification:

```bash
RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS="one-shot smoke build, output needed now" \
  <the command>
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_NO_LONG_INLINE_PROCESS=…` assignment out of the command string the event
carries (a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd`
prefix, so the value never reaches its `os.environ`). It also works when the gated command is not
first (`cd repo && RIG_HATCH_REQUEST_…="why" <cmd>`). Exporting the var into the harness
environment works too and takes precedence over an inline value.

If the env var is unset, no Telegram call is made and the command simply blocks. If it is
present but blank, whitespace-only, or a bare flag value (`1`/`true`/`yes`/`on`), the hook does
not contact Telegram and denies — a bare `1` is not a justification. A real justification runs
`tg-ctl ask` through a trusted absolute path (never ambient `PATH`) with the question as a JSON
ButtonRequest on stdin; only a reply on stdout whose decision is explicitly `allow` allows — an
empty or unparseable reply, an explicit deny, any nonzero exit, launch error, or timeout denies
(`tg-ctl ask` exits 0 regardless of outcome, so a clean exit alone is never approval). An agent
can *request*, not self-grant — the human taps to approve.

## Fail-open, on purpose

`on_error: "open"`. Responsiveness discipline, not a security boundary — a crash must never
wedge the ability to run a command.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x no_long_inline_process.py
echo '{"args":{"command":"review"}}'   | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"   # → exit=10 (block)
echo '{"args":{"command":"npm test"}}' | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"   # → exit=10 (block)
echo '{"args":{"command":"sleep 2"}}'  | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"   # → exit=0 (allow)
echo '{"args":{"command":"cat docs/review-notes.md"}}' | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"  # → exit=0 (path, not a command)
echo '{"args":{"agent_id":"sub-1","command":"npm test"}}' | ./no_long_inline_process.py; rc=$?; echo "exit=$rc"  # → exit=0 (subagent-exempt)
```
