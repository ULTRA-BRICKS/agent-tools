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
import { appendFileSync, mkdirSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
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
// holding the call open for a human (a Telegram approval). Say so once, in the file log, so the wait
// is explainable instead of just slow: the old synchronous spawn "explained" it by freezing the
// whole interface. Overridable with OMP_HOOK_BRIDGE_NOTICE_MS (the tests use a small value).
const DEFAULT_SLOW_DISPATCH_NOTICE_MS = 10000;
// Grace between the SIGTERM that asks the dispatcher to stop and the SIGKILL that makes it.
// SIGTERM first matches what the previous synchronous spawn did on timeout (Node's default
// `killSignal`), so a dispatcher that cleans up after itself still gets to.
const KILL_GRACE_MS = 2000;
// omp's own default for `extensionHandlers.toolCallTimeoutMs` (see the README section above).
// Quoted in the slow-dispatch notice so the number is stated once.
const OMP_DEFAULT_TOOL_CALL_TIMEOUT_MS = 30000;

// Grace between the SIGTERM sent on omp's own `session_shutdown` and the SIGKILL that follows —
// short because omp bounds that handler at 2 s (its SESSION_SHUTDOWN_HANDLER_TIMEOUT_MS).
const SHUTDOWN_KILL_GRACE_MS = 1000;
// Output caps. The old `spawnSync` had a bounded output buffer; an awaited stream does not, so a
// dispatcher (or a hook behind it) that never stops writing would grow omp's memory until it
// died. A decision is a few hundred bytes of JSON: past the stdout cap the child is stopped and
// the call resolves through the fail-open path. A stderr line that never ends is flushed in
// bounded pieces instead of being held for its newline.
const MAX_STDOUT_BYTES = 1 << 20;
const MAX_STDERR_LINE_BYTES = 64 << 10;

type BridgeEvent = "tool_call" | "tool_result";
type DispatcherChild = ReturnType<typeof spawn>;

// Every dispatcher child still running, shared across reloads of this module. omp leaving
// mid-dispatch (Ctrl-C, /exit, a crash) must not orphan a `python3 -m omp_hook_bridge` that may be
// holding a Telegram hatch open. The budget timers below are unref'd on purpose — a dispatch nobody
// waits for must not hold omp's exit hostage — so once omp is going down they never fire; the
// reaper is what ends the child instead. The old synchronous spawn never had the problem only
// because omp could not exit at all mid-dispatch. Two paths reach it: `session_shutdown` (omp's own
// teardown, where a SIGTERM→SIGKILL escalation fits inside omp's 2 s cap) and, for a crash or a
// signal that skips it, the process `exit` event, which cannot wait — there a child already asked
// to stop gets SIGKILL, any other SIGTERM. omp reloads an edited extension module under a
// cache-buster query, so the registry and the one `exit` listener live on `globalThis` (keyed by a
// registered symbol) rather than per module instance: a reload neither loses track of the
// children the previous instance spawned nor stacks another listener on `process`.
interface InFlightRegistry {
  children: Set<DispatcherChild>;
  // Children already sent SIGTERM (by the budget, a shutdown or an overflow), so the exit path
  // knows to go straight to SIGKILL. `child.killed` cannot serve: a group signal bypasses it.
  stopping: WeakSet<DispatcherChild>;
}
const IN_FLIGHT_KEY = Symbol.for("agent-tools.omp-hook-bridge.in-flight");
const registry: InFlightRegistry = ((globalThis as any)[IN_FLIGHT_KEY] ??= registerExitReaper());
const inFlight = registry.children;

function registerExitReaper(): InFlightRegistry {
  const created: InFlightRegistry = { children: new Set(), stopping: new WeakSet() };
  process.on("exit", () => {
    if (created.children.size === 0) return;
    logToDebugFile(`omp-hook-bridge: omp is exiting — stopping ${created.children.size} in-flight dispatcher(s)`);
    for (const child of created.children) signalDispatcher(child, created.stopping.has(child) ? "SIGKILL" : "SIGTERM");
    created.children.clear();
  });
  return created;
}

// Deliver a signal to the dispatcher AND everything it started: the child is spawned as its own
// process-group leader (`detached: true` below), so `kill(-pid)` reaches a hook script and the
// helpers it forked. Out of reach by design: a descendant that put itself in a NEW session (the
// hatch's `tg-ctl ask` is started with `start_new_session`), which SIGTERM to the dispatcher must
// end on its own — that is the dispatcher's cleanup, and why SIGTERM always comes first.
function signalDispatcher(child: DispatcherChild, signal: NodeJS.Signals): void {
  if (child.exitCode !== null || child.signalCode !== null) return;
  try {
    if (child.pid) process.kill(-child.pid, signal);
    else child.kill(signal);
  } catch {
    child.kill(signal); // the group is already gone (or not ours): fall back to the child alone
  }
}

// SIGTERM first — the previous synchronous spawn also terminated on SIGTERM (Node's default
// `killSignal`), so a dispatcher that cleans up after itself keeps that chance — then SIGKILL
// after `graceMs` if it is still alive. Returns the disarm for the grace timer (a child that
// exits on SIGTERM must not be followed by a pointless SIGKILL to a reused pid).
function stopDispatcher(child: DispatcherChild, graceMs: number): () => void {
  registry.stopping.add(child);
  signalDispatcher(child, "SIGTERM");
  const killTimer = setTimeout(() => signalDispatcher(child, "SIGKILL"), graceMs);
  killTimer.unref?.();
  return () => clearTimeout(killTimer);
}

function reapInFlight(why: string): void {
  if (inFlight.size === 0) return;
  logToDebugFile(`omp-hook-bridge: ${why} — stopping ${inFlight.size} in-flight dispatcher(s)`);
  for (const child of inFlight) stopDispatcher(child, SHUTDOWN_KILL_GRACE_MS);
}

// Silence contract: omp surfaces extension stderr over the TUI, so NOTHING here may
// reach stderr — every diagnostic lands in the debug file log. The agent receives
// block reasons programmatically via the returned {block, reason}, never via stderr.
function resolveDebugLogPath(): string {
  return (
    process.env.HOOKS_DEBUG_LOG ||
    process.env.OMP_HOOK_BRIDGE_DEBUG_LOG ||
    join(homedir(), ".cache", "agent-tools", "hooks-debug.log")
  );
}

function logToDebugFile(text: string): void {
  try {
    if (!text) return;
    const logPath = resolveDebugLogPath();
    mkdirSync(dirname(logPath), { recursive: true });
    const line = text.endsWith("\n") ? text : `${text}\n`;
    appendFileSync(logPath, `${new Date().toISOString()} ${line}`);
  } catch {
    // Debug logging must never break the hook path.
  }
}

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
  timedOut: boolean;
  overflowed: boolean;
}

// Deliberately the OPPOSITE of opencode's `plugin.js`, which THROWS on a `tool.execute.before`
// bridge failure to fail closed. Per docs/hooks.md, a THROWN error from a `tool_call` handler
// blocks the call in omp too — but omp is Alex's interactive daily driver, and a wedged/broken
// dispatcher process (missing python3, a bug in this bridge, a descriptor timeout) must never
// silently brick every bash/edit/write/task call in a live session. So bridge-infrastructure
// failure here always resolves to "no opinion" (return undefined => allow); only an EXPLICIT
// `{"decision":"block","reason":...}` from the dispatcher becomes a real block. See README.md
// "Fail policy" for the full reasoning.
function firstNonBlank(...vals: unknown[]): string {
  for (const v of vals) {
    if (v == null) continue;
    const s = String(v).trim();
    if (s) return s;
  }
  return "";
}

function ctxString(ctx: any, event: any, prop: string, getter: string): string {
  // omp's extension ctx exposes NO top-level getSessionFile()/getAgentId()/
  // sessionFile/agentId (oh-my-pi#11214 — ExtensionContext carries only
  // `sessionManager`); a blank property must not shadow the getter (`??` only skips
  // null/undefined). Prefer a non-blank property, then the getter, then a same-named
  // field on the event — never tool args. The sessionManager fallback below is what
  // actually resolves today.
  let fromGetter: unknown;
  try {
    fromGetter = typeof ctx?.[getter] === "function" ? ctx[getter]() : undefined;
  } catch {
    fromGetter = undefined;
  }
  return firstNonBlank(ctx?.[prop], fromGetter, event?.[prop]);
}

interface SessionManagerIdentity {
  getSessionFile?: () => string | undefined;
  getSessionId?: () => string | undefined;
}

interface SessionManagerHost {
  sessionManager?: SessionManagerIdentity;
}

function sessionManagerString(ctx: SessionManagerHost | null | undefined, getter: keyof SessionManagerIdentity): string {
  // The REAL identity source on omp's ExtensionContext (agent-tools#743): for a native
  // Task/eval child, ctx.sessionManager is the CHILD's own session manager, so its
  // getSessionFile() returns the nested transcript .../<session-id>/<ChildName>.jsonl —
  // the path shape dispatch.py's _OMP_SESSION_DIR classifies as a subagent, and the one
  // piece of unforgeable evidence oh-my-pi#11214 leaves available (the harness's own
  // session state, never tool args). A main session returns the flat
  // .../<session-id>.jsonl here, which the dispatcher correctly does NOT classify as a
  // child. Returns "" for an in-memory session (getSessionFile() → undefined) or any
  // ctx without a sessionManager — the dispatcher then keeps its conservative default.
  // SAFETY: ctx is typed as SessionManagerHost so no runtime cast is needed here; the
  // actual caller still passes an untyped `any` extension ctx, so the getter is still
  // runtime-guarded (typeof === "function") before it is invoked.
  try {
    const sm = ctx?.sessionManager;
    const fn = sm?.[getter];
    return firstNonBlank(typeof fn === "function" ? fn.call(sm) : undefined);
  } catch {
    return "";
  }
}

async function runBridge(eventName: BridgeEvent, event: any, ctx: any): Promise<BridgeResult | undefined> {
  let outcome: DispatcherOutcome;
  try {
    const agentId = ctxString(ctx, event, "agentId", "getAgentId");
    // Top-level ctx identity is blank under oh-my-pi#11214 (it never existed on
    // ExtensionContext); sessionManagerString is the source that resolves today and is
    // consulted FIRST: ctxString's last leg reads a same-named field off the raw omp
    // extension event, and a future event shape carrying model-influenced sessionId/
    // sessionFile values must not outrank the harness's own session state — session-scoped
    // hook state keys on these (agent-tools#743 RC-2), so a spoofed value would rotate
    // marker keys per call (warn forever, never block).
    const sessionId = firstNonBlank(
      sessionManagerString(ctx, "getSessionId"),
      ctxString(ctx, event, "sessionId", "getSessionId"),
    );
    const sessionFile = firstNonBlank(
      sessionManagerString(ctx, "getSessionFile"),
      ctxString(ctx, event, "sessionFile", "getSessionFile"),
    );
    const payload = JSON.stringify({
      event: eventName,
      toolName: event?.toolName,
      input: event?.input ?? {},
      cwd: ctx?.cwd || process.cwd(),
      toolCallId: event?.toolCallId,
      // Trusted subagent identity from omp's hook ctx (not tool args). Without this,
      // orchestrator-stays-thin treats every native omp helper as the main thread
      // (no RIG_AGENT_ID / PI_SESSION_FILE on the Node hook process).
      agentId,
      sessionId,
      sessionFile,
    });
    outcome = await spawnDispatcher(eventName, payload, event?.toolCallId, {
      sessionFile,
      agentId,
      sessionId,
    });
  } catch (error: any) {
    logToDebugFile(`omp-hook-bridge: dispatcher setup failed, allowing call: ${error?.message}`);
    return undefined;
  }
  return interpretDispatcherResult(eventName, outcome);
}

// AWAITED, never synchronous: omp is single-threaded, so a synchronous spawn here froze the
// whole interface — no repaints, no keypresses — for the entire dispatch (agent-tools#558).
// omp's `emitToolCall` awaits whatever the handler returns, so the decision still arrives before
// the tool runs. The dispatcher's own stderr is captured to the debug file log as it arrives
// rather than buffered to the end, so a hook that explains itself while it waits (a hatch
// posting its Telegram card) is recorded while it is still waiting.
//
// Dispatches can now OVERLAP, which the synchronous spawn made impossible as a side effect of
// blocking the loop. That is fine: nothing here is shared between calls (each gets its own
// child, buffer and timers), and the descriptors already run concurrently under claude-code,
// whose bridge has never serialized them either. Two simultaneous dispatchers interleave
// their lines in the file log, which is why every captured line is tagged with its event and
// tool-call id (`stderrForwarder`) — two hatches both explaining a wait stay attributable.
interface DispatcherIdentity {
  sessionFile?: string;
  agentId?: string;
  sessionId?: string;
}

function spawnDispatcher(
  eventName: BridgeEvent,
  payload: string,
  toolCallId: unknown,
  // Grouped object, not positional strings: a transposed agentId/sessionId pair at a call
  // site would type-check and silently mis-key session-scoped hook state (RIG_AGENT_ID vs
  // RIG_SESSION_ID).
  identity: DispatcherIdentity = {},
): Promise<DispatcherOutcome> {
  const { sessionFile = "", agentId = "", sessionId = "" } = identity;
  const python = process.env.OMP_HOOK_BRIDGE_PYTHON || "python3";
  const priorPythonPath = process.env.PYTHONPATH || "";
  return new Promise((resolveOutcome) => {
    const child = spawn(python, ["-m", "omp_hook_bridge", eventName], {
      stdio: ["pipe", "pipe", "pipe"],
      detached: true, // its own process group, so a stop reaches the hook scripts it forks too
      env: {
        ...process.env,
        PYTHONPATH: priorPythonPath ? `${libDir}${delimiter}${priorPythonPath}` : libDir,
        // Nested omp Task/eval transcript (ctx.getSessionFile()). Overrides a parent
        // PI_SESSION_FILE so _omp_nested_agent_id sees the child, not the main session.
        ...(sessionFile ? { PI_SESSION_FILE: sessionFile } : {}),
        ...(agentId ? { RIG_AGENT_ID: agentId } : {}),
        // Trusted per-session id (ctx/sessionManager, never tool args) so session-keyed
        // hook state — orchestrator-stays-thin's warn/block marker (agent-tools#743,
        // RC-2) — tiers per omp session instead of per tmux pane or shared cwd+point.
        ...(sessionId ? { RIG_SESSION_ID: sessionId } : {}),
      },
    });
    inFlight.add(child);
    let stdout = "";
    let overflowed = false;
    let settled = false;
    const timers = armDispatchTimers(child, eventName);
    const forward = stderrForwarder(`omp-hook-bridge[${eventName} ${String(toolCallId ?? "-")}]`);
    const finish = (outcome: Omit<DispatcherOutcome, "timedOut" | "overflowed">) => {
      if (settled) return;
      settled = true;
      inFlight.delete(child);
      timers.disarm();
      forward.flush();
      resolveOutcome({ ...outcome, timedOut: timers.timedOut(), overflowed });
    };
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      if (overflowed) return;
      stdout += chunk;
      if (Buffer.byteLength(stdout) > MAX_STDOUT_BYTES) {
        overflowed = true;
        stdout = "";
        timers.stopNow(); // no decision can follow; end the child and let `close` settle the call
      }
    });
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", forward.write);
    child.on("error", (error: Error) => finish({ stdout, status: null, signal: null, error }));
    // `close` (not `exit`): stdout must be fully drained before the decision is read.
    child.on("close", (status, signal) => finish({ stdout, status, signal }));
    // A dispatcher that dies before reading its payload would otherwise surface as an unhandled
    // EPIPE, which omp reads as a THROWN handler — i.e. a block. This bridge fails open.
    child.stdin?.on("error", () => undefined);
    child.stdin?.end(payload);
  });
}

interface StderrForwarder {
  write: (chunk: string) => void;
  flush: () => void;
}

// The dispatcher's own stderr is captured to the debug file log as it arrives,
// line by line under `tag`, so a chunk boundary never splits a line and the tag
// lands once per line — never streamed to the terminal. `flush` writes a trailing
// unterminated fragment when the child closes — or, for a line that never ends, once it exceeds
// MAX_STDERR_LINE_BYTES, so an unterminated stream is bounded instead of held.
function stderrForwarder(tag: string): StderrForwarder {
  let pending = "";
  const flush = () => {
    if (pending) logToDebugFile(`${tag} ${pending}`);
    pending = "";
  };
  return {
    write: (chunk) => {
      pending += chunk;
      const lines = pending.split("\n");
      pending = lines.pop() ?? "";
      for (const line of lines) logToDebugFile(`${tag} ${line}`);
      while (pending.length > MAX_STDERR_LINE_BYTES) {
        logToDebugFile(`${tag} ${pending.slice(0, MAX_STDERR_LINE_BYTES)}`);
        pending = pending.slice(MAX_STDERR_LINE_BYTES);
      }
    },
    flush,
  };
}

interface DispatchTimers {
  timedOut: () => boolean;
  stopNow: () => void;
  disarm: () => void;
}

// Two timers per dispatch: the budget that ends it, and the notice that explains a long one.
// Plain global timers, not `ctx.setTimeout` (omp clears its managed timers wholesale on
// extension teardown, silently dropping the budget), but `unref`'d so a dispatch nobody is
// waiting for any more cannot hold the process open on shutdown.
function armDispatchTimers(child: DispatcherChild, eventName: BridgeEvent): DispatchTimers {
  let timedOut = false;
  let disarmKill: (() => void) | undefined;
  const stopNow = () => {
    disarmKill ??= stopDispatcher(child, KILL_GRACE_MS);
  };
  const budgetTimer = setTimeout(() => {
    timedOut = true;
    stopNow();
  }, resolveDispatcherTimeout());
  budgetTimer.unref?.();
  // Only `tool_call` is worth explaining: it is the event omp can refuse, and the one whose
  // wait a human is holding up. A slow `tool_result` costs nothing — the write already landed.
  const noticeTimer =
    eventName === "tool_call" ? setTimeout(() => warnSlowDispatch(eventName), resolveSlowDispatchNotice()) : undefined;
  noticeTimer?.unref?.();
  return {
    timedOut: () => timedOut,
    stopNow,
    disarm: () => {
      clearTimeout(budgetTimer);
      disarmKill?.();
      if (noticeTimer) clearTimeout(noticeTimer);
    },
  };
}

function warnSlowDispatch(eventName: BridgeEvent): void {
  const seconds = Math.round(resolveSlowDispatchNotice() / 1000);
  const ompDefaultSeconds = Math.round(OMP_DEFAULT_TOOL_CALL_TIMEOUT_MS / 1000);
  logToDebugFile(
    `omp-hook-bridge[${eventName}]: a hook has been holding this call for ${seconds}s — usually a ` +
      "Telegram approval hatch waiting for a human, sometimes just a slow multi-file dispatch. omp " +
      "stays responsive, but it refuses the call at its own extensionHandlers.toolCallTimeoutMs " +
      `(${ompDefaultSeconds}s by default) unless that setting is raised — see lib/omp_hook_bridge/README.md.`,
  );
}

function interpretDispatcherResult(eventName: BridgeEvent, outcome: DispatcherOutcome): BridgeResult | undefined {
  if (outcome.timedOut) {
    logToDebugFile(`omp-hook-bridge[${eventName}]: dispatcher timed out, allowing call`);
    return undefined;
  }
  if (outcome.overflowed) {
    logToDebugFile(
      `omp-hook-bridge[${eventName}]: dispatcher stdout exceeded the ${MAX_STDOUT_BYTES}-byte output limit, allowing call`,
    );
    return undefined;
  }
  if (outcome.error) {
    logToDebugFile(`omp-hook-bridge[${eventName}]: dispatcher launch failed, allowing call: ${outcome.error.message}`);
    return undefined;
  }
  if (outcome.signal) {
    logToDebugFile(`omp-hook-bridge[${eventName}]: dispatcher terminated by ${outcome.signal}, allowing call`);
    return undefined;
  }
  if (outcome.status !== 0) {
    logToDebugFile(`omp-hook-bridge[${eventName}]: dispatcher exited ${outcome.status}, allowing call`);
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
    logToDebugFile(`omp-hook-bridge[${eventName}]: invalid dispatcher JSON, allowing call: ${error?.message}`);
    return undefined;
  }
  if (result?.decision === "block") {
    return { block: true, reason: result.reason || "blocked by agents-hooks/v1" };
  }
  return undefined;
}

export default function (pi: any): void {
  // omp's own teardown: end any dispatcher still waiting (see `inFlight`).
  pi.on("session_shutdown", async () => {
    reapInFlight("session shutdown");
  });

  pi.on("tool_call", async (event: any, ctx: any) => {
    try {
      return await runBridge("tool_call", event, ctx);
    } catch (error: any) {
      // Belt-and-braces: runBridge already catches internally, but a bridge-infrastructure
      // bug must NEVER escape as a throw here — that would block the tool call (see the
      // module-level comment on runBridge).
      logToDebugFile(`omp-hook-bridge: unexpected pre-call bridge error, allowing call: ${error?.message}`);
      return undefined;
    }
  });

  pi.on("tool_result", async (event: any, ctx: any) => {
    // Post-execution: the write already landed, so a block decision here is logged only
    // (matching opencode's `tool.execute.after` behavior), never surfaced as a real block.
    try {
      const result = await runBridge("tool_result", event, ctx);
      if (result?.block) {
        logToDebugFile(`omp-hook-bridge: post-write hook would have blocked; write already landed: ${result.reason}`);
      }
    } catch (error: any) {
      logToDebugFile(`omp-hook-bridge: unexpected post-call bridge error (ignored): ${error?.message}`);
    }
    return undefined;
  });
}
