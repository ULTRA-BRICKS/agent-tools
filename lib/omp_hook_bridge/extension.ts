// agent-tools omp extension — bridges omp's native `pi.on("tool_call"/"tool_result", ...)`
// events into the shared agents-hooks/v1 contract by shelling out to the Python dispatcher
// in this same directory (`python3 -m omp_hook_bridge <event>`). See README.md for the full
// contract and, in particular, why this file's fail policy on `tool_call` is the OPPOSITE
// of the sibling opencode bridge's `plugin.js`.
//
// omp's extension loader imports this module with an `?mtime=<n>` cache-buster query string
// appended to the file URL (docs/extension-loading.md) so edited source reloads without a
// restart. `fileURLToPath` strips the query string when converting back to a filesystem
// path, so `realpathSync(fileURLToPath(import.meta.url))` below resolves correctly whether
// or not that cache-buster is present.

import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { delimiter, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const extensionPath = realpathSync(fileURLToPath(import.meta.url));
const libDir = resolve(dirname(extensionPath), "..");
// This bounds the WHOLE dispatch — every matching descriptor run serially for every
// fanned-out file path — not one descriptor. It must exceed the longest shipped descriptor
// budget (960,000 ms: every hatch-capable descriptor, e.g. block-raw-pr-merge /
// worktree-only-writes / orchestrator-stays-thin, waits that long for a Telegram approval)
// with room for more than one such wait in a single call, because this bridge fails OPEN on
// a dispatcher failure (see `runBridge` below and README.md "Fail policy"): a timeout HERE
// is not the safe fallback it is for opencode's fail-CLOSED plugin — it converts a still-
// running fail-closed hook into an allow. 2,000,000 ms covers two serial full approval
// windows plus margin (a hatch wait only happens when the agent set a RIG_HATCH_REQUEST_*
// var, so two in one call is already the rare case); three or more in one call remain a
// documented residual. Override with OMP_HOOK_BRIDGE_TIMEOUT_MS.
//
// IMPORTANT — omp enforces its OWN, much smaller budget on top of this one:
// `extensionHandlers.toolCallTimeoutMs` (default 30,000 ms) bounds a `tool_call` handler, and
// when it fires omp resolves the call to `{block: true, reason: "Extension <path> timed out
// after 30000ms"}` — fail-CLOSED, the opposite of this bridge. The old synchronous spawn
// happened to evade it (a blocked event loop cannot run omp's timer), the awaited spawn below
// does not. So a hatch approval wait only survives if that setting is raised above this budget
// in `~/.omp/agent/config.yml`; see README.md "omp's own handler timeout".
const DEFAULT_DISPATCHER_TIMEOUT_MS = 2000000;
// After this long a dispatch is no longer a normal descriptor run — most often it is a hook
// holding the call open for a human (a Telegram approval). Say so once, on stderr, so the wait
// is explainable instead of just slow: the old synchronous spawn "explained" it by freezing the
// whole interface. Overridable with OMP_HOOK_BRIDGE_NOTICE_MS (the tests use a small value).
const DEFAULT_SLOW_DISPATCH_NOTICE_MS = 10000;
// Grace between the SIGTERM that asks the dispatcher to stop and the SIGKILL that makes it.
// SIGTERM first matches what the previous synchronous spawn did on timeout (Node's default
// `killSignal`), so a dispatcher that cleans up after itself still gets to.
const KILL_GRACE_MS = 2000;

// Read per call, not once at module load: a test (and a one-off debugging run) must be able to
// set a small budget for a single dispatch without defeating omp's extension module cache.
function resolveDispatcherTimeout(): number {
  return positiveEnvMs(process.env.OMP_HOOK_BRIDGE_TIMEOUT_MS, DEFAULT_DISPATCHER_TIMEOUT_MS);
}

function resolveSlowDispatchNotice(): number {
  return positiveEnvMs(process.env.OMP_HOOK_BRIDGE_NOTICE_MS, DEFAULT_SLOW_DISPATCH_NOTICE_MS);
}

function positiveEnvMs(raw: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(raw || String(fallback), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

interface BridgeResult {
  block: boolean;
  reason?: string;
}

interface DispatcherOutcome {
  stdout: string;
  status: number | null;
  signal: NodeJS.Signals | null;
  error?: Error;
  timedOut?: boolean;
}

// Deliberately the OPPOSITE of opencode's `plugin.js`, which THROWS on a `tool.execute.before`
// bridge failure to fail closed. Per docs/hooks.md, a THROWN error from a `tool_call` handler
// blocks the call in omp too — but omp is Alex's interactive daily driver, and a wedged/broken
// dispatcher process (missing python3, a bug in this bridge, a descriptor timeout) must never
// silently brick every bash/edit/write/task call in a live session. So bridge-infrastructure
// failure here always resolves to "no opinion" (return undefined => allow); only an EXPLICIT
// `{"decision":"block","reason":...}` from the dispatcher becomes a real block. See README.md
// "Fail policy" for the full reasoning.
async function runBridge(
  eventName: "tool_call" | "tool_result",
  event: any,
  ctx: any,
): Promise<BridgeResult | undefined> {
  let outcome: DispatcherOutcome;
  try {
    const payload = JSON.stringify({
      event: eventName,
      toolName: event?.toolName,
      input: event?.input ?? {},
      cwd: ctx?.cwd || process.cwd(),
      toolCallId: event?.toolCallId,
    });
    outcome = await spawnDispatcher(eventName, payload);
  } catch (error: any) {
    console.error(`omp-hook-bridge: dispatcher setup failed, allowing call: ${error?.message}`);
    return undefined;
  }
  return interpretDispatcherResult(eventName, outcome);
}

// AWAITED, never synchronous: omp is single-threaded, so a synchronous spawn here froze the
// whole interface — no repaints, no keypresses — for the entire dispatch (agent-tools#558).
// omp's `emitToolCall` awaits whatever the handler returns, so the decision still arrives before
// the tool runs. The dispatcher's own stderr is streamed through as it arrives rather than
// buffered to the end, so a hook that explains itself while it waits (a hatch posting its
// Telegram card) is visible while it is still waiting.
//
// Dispatches can now OVERLAP, which the synchronous spawn made impossible as a side effect of
// blocking the loop. That is fine: nothing here is shared between calls (each gets its own
// child, buffer and timers), and the descriptors already run concurrently under claude-code,
// whose bridge has never serialized them either. Two simultaneous dispatchers do interleave
// their stderr on the one stream, which is why each bridge line names its event.
function spawnDispatcher(eventName: string, payload: string): Promise<DispatcherOutcome> {
  const python = process.env.OMP_HOOK_BRIDGE_PYTHON || "python3";
  const priorPythonPath = process.env.PYTHONPATH || "";
  return new Promise((resolveOutcome) => {
    const child = spawn(python, ["-m", "omp_hook_bridge", eventName], {
      stdio: ["pipe", "pipe", "pipe"],
      env: {
        ...process.env,
        PYTHONPATH: priorPythonPath ? `${libDir}${delimiter}${priorPythonPath}` : libDir,
      },
    });
    let stdout = "";
    let settled = false;
    const timers = armDispatchTimers(child, eventName);
    const finish = (outcome: DispatcherOutcome) => {
      if (settled) return;
      settled = true;
      timers.disarm();
      resolveOutcome({ ...outcome, timedOut: timers.timedOut() });
    };
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => process.stderr.write(chunk));
    child.on("error", (error: Error) => finish({ stdout, status: null, signal: null, error }));
    // `close` (not `exit`): stdout must be fully drained before the decision is read.
    child.on("close", (status, signal) => finish({ stdout, status, signal }));
    // A dispatcher that dies before reading its payload would otherwise surface as an unhandled
    // EPIPE, which omp reads as a THROWN handler — i.e. a block. This bridge fails open.
    child.stdin?.on("error", () => undefined);
    child.stdin?.end(payload);
  });
}

interface DispatchTimers {
  timedOut: () => boolean;
  disarm: () => void;
}

// Two timers per dispatch: the budget that ends it, and the notice that explains a long one.
// Plain global timers, not `ctx.setTimeout` (omp clears its managed timers wholesale on
// extension teardown, silently dropping the budget), but `unref`'d so a dispatch nobody is
// waiting for any more cannot hold the process open on shutdown.
function armDispatchTimers(child: ReturnType<typeof spawn>, eventName: string): DispatchTimers {
  let timedOut = false;
  let killTimer: ReturnType<typeof setTimeout> | undefined;
  const budgetTimer = setTimeout(() => {
    timedOut = true;
    // SIGTERM first, then SIGKILL only if the dispatcher is still alive: the previous
    // synchronous spawn also terminated on SIGTERM (Node's default `killSignal`), so a
    // dispatcher that cleans up after itself keeps that chance.
    child.kill("SIGTERM");
    killTimer = setTimeout(() => child.kill("SIGKILL"), KILL_GRACE_MS);
    killTimer.unref?.();
  }, resolveDispatcherTimeout());
  budgetTimer.unref?.();
  // Only `tool_call` is worth explaining: it is the event omp can refuse, and the one whose
  // wait a human is holding up. A slow `tool_result` costs nothing — the write already landed.
  const noticeTimer =
    eventName === "tool_call" ? setTimeout(() => warnSlowDispatch(eventName), resolveSlowDispatchNotice()) : undefined;
  noticeTimer?.unref?.();
  return {
    timedOut: () => timedOut,
    disarm: () => {
      clearTimeout(budgetTimer);
      if (killTimer) clearTimeout(killTimer);
      if (noticeTimer) clearTimeout(noticeTimer);
    },
  };
}

function warnSlowDispatch(eventName: string): void {
  const seconds = Math.round(resolveSlowDispatchNotice() / 1000);
  console.error(
    `omp-hook-bridge[${eventName}]: a hook has been holding this call for ${seconds}s — usually a ` +
      "Telegram approval hatch waiting for a human, sometimes just a slow multi-file dispatch. omp " +
      "stays responsive, but it refuses the call at its own extensionHandlers.toolCallTimeoutMs " +
      "(30s by default) unless that setting is raised — see lib/omp_hook_bridge/README.md.",
  );
}

function interpretDispatcherResult(eventName: string, outcome: DispatcherOutcome): BridgeResult | undefined {
  if (outcome.timedOut) {
    console.error(`omp-hook-bridge[${eventName}]: dispatcher timed out, allowing call`);
    return undefined;
  }
  if (outcome.error) {
    console.error(`omp-hook-bridge[${eventName}]: dispatcher launch failed, allowing call: ${outcome.error.message}`);
    return undefined;
  }
  if (outcome.signal) {
    console.error(`omp-hook-bridge[${eventName}]: dispatcher terminated by ${outcome.signal}, allowing call`);
    return undefined;
  }
  if (outcome.status !== 0) {
    console.error(`omp-hook-bridge[${eventName}]: dispatcher exited ${outcome.status}, allowing call`);
    return undefined;
  }
  const stdout = (outcome.stdout || "").trim();
  if (!stdout) {
    return undefined;
  }
  let result: any;
  try {
    result = JSON.parse(stdout);
  } catch (error: any) {
    console.error(`omp-hook-bridge[${eventName}]: invalid dispatcher JSON, allowing call: ${error?.message}`);
    return undefined;
  }
  if (result?.decision === "block") {
    return { block: true, reason: result.reason || "blocked by agents-hooks/v1" };
  }
  return undefined;
}

export default function (pi: any): void {
  pi.on("tool_call", async (event: any, ctx: any) => {
    try {
      return await runBridge("tool_call", event, ctx);
    } catch (error: any) {
      // Belt-and-braces: runBridge already catches internally, but a bridge-infrastructure
      // bug must NEVER escape as a throw here — that would block the tool call (see the
      // module-level comment on runBridge).
      console.error(`omp-hook-bridge: unexpected pre-call bridge error, allowing call: ${error?.message}`);
      return undefined;
    }
  });

  pi.on("tool_result", async (event: any, ctx: any) => {
    // Post-execution: the write already landed, so a block decision here is logged only
    // (matching opencode's `tool.execute.after` behavior), never surfaced as a real block.
    try {
      const result = await runBridge("tool_result", event, ctx);
      if (result?.block) {
        console.error(`omp-hook-bridge: post-write hook would have blocked; write already landed: ${result.reason}`);
      }
    } catch (error: any) {
      console.error(`omp-hook-bridge: unexpected post-call bridge error (ignored): ${error?.message}`);
    }
    return undefined;
  });
}
