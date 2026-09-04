# subagent-no-bg-longproc

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 36

Stops a **dispatched subagent** from **backgrounding a long process** and then wedging
forever (agent-tools#52). The wedge, seen ~6× in one session: a subagent runs a multi-model
`review`, a build/test suite, a `--watch` loop, or a long `sleep` with `run_in_background:
true` (or a shell `&` / `setsid`), then ends its turn saying *"I'll wait for the completion
notification."* But a **subagent is NOT re-invoked by a background-completion notification —
only the main loop is.** So it idles forever with uncommitted work and no PR, and the
orchestrator has to catch the rest-notification, kill the stray process, and salvage the
half-done work by hand.

This gate makes a subagent run its **own** long work in the **foreground** and block on it.

## Where it sits in the doctrine (the inverse of two sibling gates)

| Gate | Governs | Rule |
| --- | --- | --- |
| `no-long-inline-process` (pre-bash) | the **orchestrator** | don't run a long process inline — dispatch it to a **background** subagent (subagent-**exempt**) |
| `background-subagent-gate` (pre-agent) | the **orchestrator** | dispatch a non-trivial subagent in the **background** |
| **`subagent-no-bg-longproc`** (pre-bash) | a **subagent** | run your **own** long work in the **foreground** — don't background it, you'll never be told it finished |

The orchestrator *delegates* long work to a backgrounded subagent (the main loop *does* get
the completion notification). A subagent's own long work must be foreground, because the
worker doesn't.

## Fires only on the wedge — never the correct shape

It blocks **only** when **all three** hold:

1. the tool use is a **subagent's** (`agent_id` present) — a non-subagent orchestrator is
   always allowed (its discipline is `no-long-inline-process`);
2. the command is **backgrounded**;
3. the command is a **long process**.

So a subagent running `review` in the **foreground** (the correct shape) is allowed; a
subagent backgrounding a *short* command (`cp big big2 &`) is allowed.

### What counts as "backgrounded"

Backgrounding binds to the long-process command with correct bash **job** semantics (the wedge
is "the long process *itself* is detached"), not the whole line — so `echo started & review …`
(the `echo` job is backgrounded, `review` runs **foreground**) is **allowed**, while `review …
&` is blocked. A command counts as backgrounded when:

- the CC Bash tool's **`run_in_background: true`** flag is set (forwarded into `args` by
  `lib/cc_hook_bridge`) — it backgrounds the **whole command line**, so every job is then
  detached; the primary, unambiguous wedge signal;
- a **`&`** backgrounds the **job** it terminates. `&` (like `;`) launches the entire preceding
  **pipeline / AND-OR list**, so `review | tee log &` and `review && git commit &` both
  background `review` — the `&` is not bound to only the last simple command. `&&` (logical AND)
  is its own token, and a `&` fused into a redirection (`2>&1` → `>&`, `&>file`) is a redirect,
  not a background — both correctly ignored;
- the command is led by **`setsid`** (it detaches into a new session and the parent returns
  immediately, a background even without a `&`; a `setsid` sitting as a non-leading *argument*
  does not count).

`nohup` alone does **not** background: `nohup cmd` runs in the foreground and blocks, so it
is no wedge. The real `nohup cmd &` form is caught by the `&`.

> **Documented under-block** (same precision class as the sibling's nested-shell limit): a long
> process wrapped in a **nested shell construct** right before a trailing `&` — a subshell
> `(review)&`, a process substitution `review <(git diff) &`, a command substitution `foo
> $(review) &` — is **not** flagged (the construct's paren/opener is a job boundary that ends
> the inner job before the `&`). The realistic direct wedge forms (`review … &`, `review | tee
> log &`, `review && commit &`, `run_in_background: true`, `setsid review`) are all caught;
> under-block is the safe direction for this `on_error: open` discipline gate.
>
> One known minor **over-block** (acceptable): `review … & wait` backgrounds review then blocks
> on `wait`, so it would not actually wedge — but it is flagged. That is fine: the gate's remedy
> ("run it in the foreground") is exactly the simpler equivalent, and the escape hatch covers
> the rare case.

### What counts as a "long process"

The same **argv-aware** detection as `no-long-inline-process` (the command is shell-tokenized,
split into segments, and the **real** invoked command — `argv[0]` after peeling inline env +
no-op wrappers — is inspected): the `review` CLI, a `--watch` flag, a build/test suite
(npm/pnpm/yarn/bun/deno test|build, pytest/vitest/jest/cypress/playwright, cargo/go test|build,
make/rake/msbuild test|build|all, mvn/gradle test|build|verify|package), or `sleep N` with
N≥10s. A keyword inside a quoted argument to a different command (`tg "…review…" &`) never
trips it.

## Why an agent-hook (and the `pre-bash` point)

"A subagent must run its long work foreground" was advice only, and it regressed every time —
a worker happily backgrounds `review` and ends its turn. The only place to catch it is
**before the Bash command runs**, with the `run_in_background` flag and the `agent_id`
subagent signal both visible: `lib/cc_hook_bridge` passes the whole Bash `tool_input` (incl.
`run_in_background`) through under `args`, and forwards CC's top-level `agent_id`/`agent_type`
so the gate can tell a worker's tool use from the orchestrator's. CC fires `pre-bash` for a
subagent's Bash call today (no rig-cli follow-up needed — unlike the `pre-agent` point).

> **Trust-boundary contract.** The `agent_id` exemption-inverse must come from a trusted,
> transport-level signal, never model-controlled `tool_input`. `cc_hook_bridge` enforces this:
> it takes `agent_id`/`agent_type` only from CC's top-level event and **drops** any copy a
> prompt forged inside `tool_input`. That matters in BOTH directions here — a forged
> `args.agent_id` must not let the orchestrator be mis-classified as a subagent, and (the
> direction this gate cares about) a worker can't strip its own `agent_id` to dodge the gate.

## No self-service bypass — external Telegram approval only

There is **no** env-var or inline escape hatch any more. The old `ALLOW_SUBAGENT_BACKGROUND=1` +
`ALLOW_SUBAGENT_BACKGROUND_REASON` env and the `# subagent-bg-ok:` inline sentinel let the very
worker this gate constrains grant itself an exception — security theater, not a permission gate.
Both were removed.

The block is now **deny-by-default**. For a genuine exception, ASK the human, or request a
one-time Telegram approval with a written justification:

```bash
RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC="self-managed watchdog, polls inline" \
  review diff -C /repo &
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_SUBAGENT_NO_BG_LONGPROC=…` assignment out of the command string the event
carries (a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd`
prefix, so the value never reaches its `os.environ`). Exporting the var into the harness
environment works too and takes precedence over an inline value.

If the env var is unset, no Telegram call is made and the command simply blocks. If it is
present but blank, whitespace-only, or a bare flag value (`1`/`true`/`yes`/`on`), the hook does
not contact Telegram and denies — a bare `1` is not a justification. A real justification runs
`tg-ctl ask` through a trusted absolute path (never ambient `PATH`) with the question as a JSON
ButtonRequest on stdin; only a reply on stdout whose decision is explicitly `allow` allows — an
empty or unparseable reply, an explicit deny, any nonzero exit, launch error, or timeout denies
(`tg-ctl ask` exits 0 regardless of outcome, so a clean exit alone is never approval). An agent
can *request*, not self-grant.

## Fail-open, on purpose

`on_error: "open"`. This is anti-wedge / responsiveness discipline, not a security boundary —
a crash in the check must never wedge a subagent's ability to run a command. An unparseable
command (unbalanced quotes) likewise allows.

## Test

Capture the hook's exit on its OWN line right after the pipe:

```bash
chmod +x subagent_no_bg_longproc.py

# subagent backgrounds review → BLOCK
echo '{"args":{"agent_id":"sub-1","command":"review diff -C /repo","run_in_background":true}}' \
  | ./subagent_no_bg_longproc.py
rc=$?; echo "exit=$rc"   # → decision":"block ...  exit=10

# subagent runs review FOREGROUND → allow
echo '{"args":{"agent_id":"sub-1","command":"review diff -C /repo"}}' | ./subagent_no_bg_longproc.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0

# orchestrator backgrounds review (no agent_id) → allow (no-long-inline-process governs it)
echo '{"args":{"command":"review diff -C /repo","run_in_background":true}}' | ./subagent_no_bg_longproc.py
rc=$?; echo "exit=$rc"   # → decision":"allow"  exit=0
```
