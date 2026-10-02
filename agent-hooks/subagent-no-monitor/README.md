# subagent-no-monitor

**Point:** `pre-monitor` · **Fail policy:** `open` · **Priority:** 36

Stops a **dispatched subagent** from calling the **Monitor** tool at all (HYP-1350's
retrospective, hyperide/hyper-saas PR #754). The wedge: a subagent started a Monitor watch on
its own spawned child process, then ended its turn saying *"I'll wait for the completion
notification."* But a **subagent is NOT re-invoked by a Monitor-event notification — only the
main loop is** (Monitor has no harness-tracked child at all, unlike an ordinary backgrounded
Bash command — see "The correct alternative" below). So it idles forever with uncommitted work
and no PR — the exact same failure `subagent-no-bg-longproc` exists to stop for a *labeled*
long process, just reached through a different CC tool that gate doesn't inspect.

## Where it sits in the doctrine (sibling of subagent-no-bg-longproc)

| Gate | Governs | Rule |
| --- | --- | --- |
| `subagent-no-bg-longproc` (pre-bash) | a **subagent** | run your **own** long Bash work in the **foreground** — don't background it |
| **`subagent-no-monitor`** (pre-monitor) | a **subagent** | don't call **Monitor** at all — it has no foreground mode, so there's nothing to fall back to except a bounded synchronous poll |

The orchestrator's own Monitor use (e.g. watching a backgrounded subagent's progress) is
**unaffected** — this gate only fires on a tool call made INSIDE a dispatched subagent
(`agent_id` present).

## Why unconditional (no backgrounded/foreground classification)

`subagent-no-bg-longproc` has to classify: is the command long, AND is it backgrounded? A
subagent running `review` in the foreground is fine. Monitor has no equivalent foreground
mode — the tool's entire contract is "start it, keep working, get notified later." So there is
no shape of Monitor call from a subagent that isn't the wedge: the block is **any subagent
call to Monitor, full stop**, regardless of what it watches or why.

## The correct alternative

Which replacement applies depends on what the subagent is waiting for:

1. **A single ORDINARY command it just started** — one that is NOT itself a labeled long
   process (`review`, `--watch`, a build-test suite, a long `sleep` — those go to option 2
   instead, since `subagent-no-bg-longproc` blocks backgrounding them) — set
   `run_in_background: true` on the Bash tool call. The harness auto-resumes the subagent when
   that command completes; no Monitor is needed for this shape at all.
2. **Anything else** — a labeled long process, a condition to become true, a file to appear,
   several things at once —
   block on it **synchronously**, in the foreground, with a heartbeat loop: echo a line at
   least every ~20s and keep each Bash call comfortably under ~540s (well inside the Bash
   tool's own 600s hard cap), repeating the same bounded call until the wait is over:

```bash
timeout 540 bash -c 'i=0; until <condition-check> || [ "$i" -ge 26 ]; do sleep 20; i=$((i+1)); echo "[wait] tick $i ($((i*20))s)"; done'
```

Both shapes pass `subagent-no-bg-longproc`'s own rules too (verified): a foreground heartbeat
loop is not backgrounded, and that sibling gate only blocks `run_in_background: true` when the
*backgrounded* command is itself a long-process label (`review`/`--watch`/a build-test
suite/a long `sleep`) — an ordinary single command backgrounded this way is unaffected.

## No self-service bypass — external Telegram approval only

Same deny-by-default contract as `subagent-no-bg-longproc`: no env-var self-grant. For a
genuine exception, ASK the human, or request one-time Telegram approval:

```bash
RIG_HATCH_REQUEST_SUBAGENT_NO_MONITOR="self-managed watchdog, will poll for the result inline"
```

Set in the process environment before the tool call — Monitor's `tool_input` is a watch
target (what to observe, e.g. a `description` of the thing being watched), not an invoked
shell command line, so there is no leading `VAR=value` prefix a pre-monitor hook could parse
out of it the way `subagent-no-bg-longproc` does for a real Bash command string. Only the
process-env source applies here, matching how the pre-write hooks read this var. If unset, no
Telegram call is made and the call simply blocks. If present but blank/bare (`1`/`true`/`yes`),
the hook denies without contacting Telegram.

A late approval is not wasted: if the human approves after the ask window expires, the tg-ctl daemon keeps it as a single-use `approved:<id>` grant bound to this exact operation. This hook has no shell command to carry an inline prefix, so the retry form is: simply retry the IDENTICAL operation in the same directory and the shared lib consumes the grant automatically via its pull — which requires this session's route identity to match the original ask; if the retry still denies, ask the human via `tg --tag decision`. A changed edit or a different directory is denied and does not consume the grant. See the shared lib docstring (`lib/agenttools_hatch_escalation/`) for the full Late-Approval Grant Protocol (agent-tools#742).

## Registration (read before assuming this fires on an unpatched machine)

Mapping `Monitor` → `pre-monitor` in `lib/cc_hook_bridge/dispatch.py` is **not**, on its own,
enough for CC to ever invoke the bridge for a `Monitor` tool call — Claude Code only runs
`PreToolUse` hooks it has an explicit **matcher** for in `settings.json`, and matchers are
written by **rig-cli**'s `hook_bridge_entries` (a separate repo), not by this dispatcher. This
is the identical two-repo split `pre-agent` and `pre-skill` went through, and — like both of
those — **has now shipped**: rig-cli#296 adds the `Monitor` matcher, and `rig apply` on a
given machine wires it into `settings.json`. Verified the dispatcher itself: piping a
synthetic subagent `Monitor` PreToolUse event directly into the real deployed
`PYTHONPATH=... python3 -m cc_hook_bridge PreToolUse` command produces a real `deny` with
this hook's documented message — this exercises the dispatcher's own `Monitor -> pre-monitor`
routing and this hook's block logic, but bypasses CC's own `settings.json` matcher lookup, so
it does not on its own confirm the CC-side wiring on any given machine. On a machine where
the Claude Code hook bridge is already
enabled and active (a `claude-code` `harness:` block present, `agent_hooks` on, and
`harness.hook_bridge` not opted out — see `lib/cc_hook_bridge/README.md`'s Installation
section for those prerequisites), two more things gate that specific machine/session
actually seeing the block:
1. `rig apply` needs to have run on that machine AFTER both halves merged (`rig status` /
   the settings.json `hooks.PreToolUse` matcher list will show `Monitor` once it has).
2. Claude Code reads its hook configuration at **session start**, not live — a session (or a
   subagent dispatched from one) already running when `rig apply` adds the matcher keeps
   using the config it started with. Start a fresh session to pick up a newly-applied matcher.
If the bridge itself isn't enabled on a machine at all, `rig apply` and a fresh session are
not enough on their own — check the bridge's own prerequisites first.

## Fail-open, on purpose

`on_error: "open"`. Anti-wedge / responsiveness discipline, not a security boundary — a crash
in the check must never wedge a subagent's ability to call a tool. An unparseable event
likewise allows.

## Test

```bash
chmod +x subagent_no_monitor.py

# subagent calls Monitor → BLOCK
echo '{"args":{"agent_id":"sub-1","description":"watch tests"}}' | ./subagent_no_monitor.py
rc=$?; echo "exit=$rc"   # → "decision":"block" ...  exit=10

# orchestrator calls Monitor (no agent_id) → allow
echo '{"args":{"description":"watch subagent progress"}}' | ./subagent_no_monitor.py
rc=$?; echo "exit=$rc"   # → "decision":"allow"  exit=0
```
