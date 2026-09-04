# background-subagent-gate

**Point:** `pre-agent` · **Fail policy:** `open` · **Priority:** 30

Fires when the **main thread** dispatches a subagent (the CC `Agent`/`Task` tool). A
non-trivial subagent run in the **foreground** blocks the orchestrator until it finishes —
which defeats fanning work out. This gate **blocks** such a dispatch and tells the
orchestrator to use `subagent_type: "fork"` or `isolation: "remote"` (or model the work as a
dynamic Workflow).

Lets through:

- `subagent_type: "fork"` — CC's own `Agent` tool description states a fork runs in the
  background and reports back via a completion notification
- `isolation: "remote"` — CC's own tool description states this always runs in background
- a dispatch already marked `run_in_background: true` — kept for forward-compat with any
  harness/carrier that exposes such a field. CC's own `Agent` tool schema, as of 2.1.177,
  carries no such property, so this path is dead for CC specifically — but **opencode's**
  bridge (`lib/opencode_hook_bridge/dispatch.py`) normalizes its own `background: true/false`
  field into `run_in_background` before this hook ever runs, so for an opencode-driven
  orchestrator (which has no `fork`/`isolation` concept) this IS the live, only background
  signal
- a **trivial** one-liner dispatch (prompt/description `< 200` chars and single-line)
- a dispatch made **by a subagent itself** (subagent-exempt — `agent_id` present): a worker
  may fan out further, and this gate governs the orchestrator, not the workers

`isolation: "worktree"` is deliberately **not** treated as inherently background — worktree
isolation is about workspace separation, not execution timing, so a `worktree`-isolated
dispatch still needs one of the paths above to pass.

## Why an agent-hook (and the `pre-agent` point)

"Dispatch heavy work in the background" was advice only. An autonomous orchestrator with
auto-accepted prompts will happily fire a foreground subagent and block itself for minutes.
The only place to catch it is **before the Agent/Task call runs** — a new `pre-agent` point
the bridge now maps from CC's `PreToolUse` on `Agent`/`Task`. It enforces the orchestration
half of `delegate-work-to-subagents`.

> The `agent_id`/`agent_type` subagent signal is forwarded by `lib/cc_hook_bridge` into the
> v1 event. For CC to actually fire this point, rig-cli must wire an `Agent|Task` PreToolUse
> matcher in `settings.json` (`hook_bridge_entries`) — a **rig-cli follow-up**, separate repo.
>
> **Trust-boundary contract (any carrier, not just CC).** This hook (and `orchestrator-stays-thin`,
> `no-long-inline-process`) exempts a tool use when `args.agent_id` is present — so that value must
> originate from a **trusted, transport-level** subagent signal, never from model-controlled
> `tool_input`. `cc_hook_bridge` enforces this: it takes `agent_id`/`agent_type` only from CC's
> top-level event and **drops** any copy a prompt forged inside `tool_input`. A non-CC carrier that
> wires these hooks MUST replicate that filtering, or a forged `tool_input.agent_id` would let the
> orchestrator exempt itself from the gate.

## Triviality heuristic

A prompt is trivial (cheap to run inline) only if it is **short** (`< 200` chars) **and**
single-line. Deliberately conservative: we block only a **clearly** non-trivial foreground
dispatch, so a borderline one slipping through is preferred to nagging on quick tasks.

Triviality is judged on the **prompt/description length and shape only** — not the
`subagent_type` or model. A short prompt to a heavyweight agent still reads as trivial here;
the gate's job is "don't block the main thread on a long foreground run", and prompt size is
the cheap, payload-only proxy for that.

## No self-service bypass — external Telegram approval only

There is **no** env-var escape hatch any more. The old `ALLOW_FOREGROUND_SUBAGENT=1` +
`ALLOW_FOREGROUND_SUBAGENT_REASON` env let the very orchestrator this gate constrains grant
itself an exception — security theater, not a permission gate. It was removed. (There was never
an inline sentinel — the Agent tool carries no shell string to hide a `# ...` in.)

The block is now **deny-by-default**. For a genuine exception, ASK the human, or request a
one-time Telegram approval with a written justification. Because this is a **pre-agent** hook
(it gates the Agent/Task tool, which carries no shell command string), the inline
`VAR=… <command>` prefix form does NOT apply here — the variable must be **exported** into the
harness process environment so the hook reads it from `os.environ`:

```bash
export RIG_HATCH_REQUEST_BACKGROUND_SUBAGENT_GATE="quick probe, latency matters"
# …then make the dispatch that would otherwise be blocked.
```

If the env var is unset, no Telegram call is made and the dispatch simply blocks. If it is
present but blank, whitespace-only, or a bare flag value (`1`/`true`/`yes`/`on`), the hook does
not contact Telegram and denies — a bare `1` is not a justification. A real justification runs
`tg-ctl ask` through a trusted absolute path (never ambient `PATH`) with the question as a JSON
ButtonRequest on stdin; only a reply on stdout whose decision is explicitly `allow` allows — an
empty or unparseable reply, an explicit deny, any nonzero exit, launch error, or timeout denies
(`tg-ctl ask` exits 0 regardless of outcome, so a clean exit alone is never approval). An agent
can *request*, not self-grant.

## Fail-open, on purpose

`on_error: "open"`. This is orchestration discipline, not a security boundary — a crash in
the check must never wedge the ability to dispatch work.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x background_subagent_gate.py
echo '{"args":{"prompt":"'"$(python3 -c 'print("x"*300)')"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"block ...  exit=10  (long foreground dispatch)

LONG=$(python3 -c 'print("x"*300)')

echo '{"args":{"subagent_type":"fork","prompt":"'"$LONG"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (fork — inherently background)

echo '{"args":{"isolation":"remote","prompt":"'"$LONG"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (isolation:remote — inherently background)

# NOTE: a short/trivial prompt would pass here too, but for the WRONG reason (triviality,
# not backgrounding) — use a long prompt so this actually exercises the worktree-is-not-
# background path, not the unrelated trivial-dispatch allow.
echo '{"args":{"isolation":"worktree","prompt":"'"$LONG"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"block ...  exit=10  (worktree isolation is NOT background)

echo '{"args":{"isolation":"worktree","subagent_type":"fork","prompt":"'"$LONG"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (worktree is IGNORED, not a block signal — fork still wins)

echo '{"args":{"run_in_background":true,"prompt":"'"$LONG"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (forward-compat path; the live signal for opencode)

echo '{"args":{"agent_id":"sub-1","prompt":"'"$LONG"'"}}' | ./background_subagent_gate.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0  (subagent-exempt — checked before triviality, so
                          #   this demonstrates the agent_id path specifically, not just a short prompt)
```
