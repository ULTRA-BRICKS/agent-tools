import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pluginPath = realpathSync(fileURLToPath(import.meta.url));
const libDir = resolve(dirname(pluginPath), "..");
const DEFAULT_DISPATCHER_TIMEOUT_MS = 1000000;
const DEFAULT_SESSION_LOOKUP_TIMEOUT_MS = 5000;
const DISPATCHER_TIMEOUT_MS = resolvePositiveIntEnv(
  "OPENCODE_HOOK_BRIDGE_TIMEOUT_MS",
  DEFAULT_DISPATCHER_TIMEOUT_MS,
);
const SESSION_LOOKUP_TIMEOUT_MS = resolvePositiveIntEnv(
  "OPENCODE_HOOK_BRIDGE_SESSION_LOOKUP_TIMEOUT_MS",
  DEFAULT_SESSION_LOOKUP_TIMEOUT_MS,
);

function resolvePositiveIntEnv(name, fallback) {
  const parsed = Number.parseInt(process.env[name] || String(fallback), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function resolveDebugLogPath() {
  return (
    process.env.HOOKS_DEBUG_LOG ||
    process.env.OPENCODE_HOOK_BRIDGE_DEBUG_LOG ||
    join(homedir(), ".cache", "agent-tools", "hook-bridge-debug.log")
  );
}

function logToDebugFile(text) {
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

function failOnDispatcherTimeout(hook, detail) {
  if (hook === "tool.execute.before") {
    throw new Error(`opencode hook bridge dispatcher timed out before tool execution: ${detail}`);
  }
  logToDebugFile(`opencode-hook-bridge: dispatcher timed out after tool execution, allowing call: ${detail}`);
}

function failOnDispatcherSignal(hook, signal) {
  if (hook === "tool.execute.before") {
    throw new Error(`opencode hook bridge dispatcher terminated by ${signal} before tool execution`);
  }
  logToDebugFile(`opencode-hook-bridge: dispatcher terminated by ${signal} after tool execution, allowing call`);
}

// Subagent detection. An opencode `task` subagent runs as a CHILD session whose
// `session.parentID` is non-null; the coordinator's session has none. The hook payload does
// not say which, so the plugin asks opencode itself: `ctx.client` is the PluginInput SDK
// client (`createOpencodeClient` from `@opencode-ai/sdk`, v1 flavor, opencode 1.18.x) and
// `client.session.get({ path: { id } })` -> `{ data: Session, error }`, `Session.parentID?`.
// The result travels to dispatch.py as the TOP-LEVEL `sessionParentID` payload field that
// only this plugin writes - never through `output.args`, which the model controls.
//
// Fail OPEN: any lookup failure (missing client, HTTP error, timeout) resolves to "" =
// unknown = coordinator, logged to the debug file, never thrown - a broken lookup must not
// block a tool call. Only successful lookups are cached (parentage is fixed at session
// creation), so a transient failure is retried on the next call.
async function lookupSessionParentID(ctx, sessionID) {
  const client = ctx?.client;
  if (typeof client?.session?.get !== "function") {
    throw new Error("plugin client has no session.get");
  }
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`session.get timed out after ${SESSION_LOOKUP_TIMEOUT_MS}ms`)),
      SESSION_LOOKUP_TIMEOUT_MS,
    );
  });
  try {
    const result = await Promise.race([client.session.get({ path: { id: sessionID } }), timeout]);
    if (!result || result.error || !result.data) {
      throw new Error(`session.get returned no session: ${JSON.stringify(result?.error ?? null)}`);
    }
    const parentID = result.data.parentID;
    return typeof parentID === "string" ? parentID.trim() : "";
  } finally {
    clearTimeout(timer);
  }
}

async function resolveSessionParentID(ctx, sessionID, cache) {
  if (typeof sessionID !== "string" || !sessionID) return "";
  const cached = cache.get(sessionID);
  if (cached !== undefined) return cached;
  try {
    const parentID = await lookupSessionParentID(ctx, sessionID);
    cache.set(sessionID, parentID);
    return parentID;
  } catch (error) {
    logToDebugFile(
      `opencode-hook-bridge: session parent lookup failed for ${sessionID}, treating as coordinator: ${error.message}`,
    );
    return "";
  }
}

function runBridge(hook, input, output, ctx, sessionParentID = "") {
  const python = process.env.OPENCODE_HOOK_BRIDGE_PYTHON || "python3";
  const priorPythonPath = process.env.PYTHONPATH || "";
  let proc;
  try {
    const payload = JSON.stringify({
      hook,
      input: input || {},
      output: output || {},
      cwd: input?.cwd || ctx.directory || ctx.worktree || process.cwd(),
      directory: ctx.directory,
      worktree: ctx.worktree,
      sessionParentID,
    });
    proc = spawnSync(python, ["-m", "opencode_hook_bridge", hook], {
      input: payload,
      encoding: "utf8",
      timeout: DISPATCHER_TIMEOUT_MS,
      env: {
        ...process.env,
        PYTHONPATH: priorPythonPath ? `${libDir}${delimiter}${priorPythonPath}` : libDir,
      },
    });
  } catch (error) {
    logToDebugFile(`opencode-hook-bridge: dispatcher setup failed, allowing call: ${error.message}`);
    return;
  }

  // opencode surfaces plugin stderr as TUI toasts: NOTHING may reach stderr here.
  // Dispatcher chatter and every diagnostic below go to the debug file log; the
  // agent receives block reasons programmatically via the thrown Error.
  const dispatcherStderr = proc.stderr || "";
  const suppressDispatcherStderr = () => {
    if (dispatcherStderr) logToDebugFile(dispatcherStderr);
  };
  if (proc.error) {
    if (proc.error.code === "ETIMEDOUT") {
      suppressDispatcherStderr();
      failOnDispatcherTimeout(hook, proc.error.message);
      return;
    }
    suppressDispatcherStderr();
    logToDebugFile(`opencode-hook-bridge: dispatcher launch failed, allowing call: ${proc.error.message}`);
    return;
  }
  if (proc.signal) {
    suppressDispatcherStderr();
    failOnDispatcherSignal(hook, proc.signal);
    return;
  }
  if (proc.status !== 0) {
    suppressDispatcherStderr();
    logToDebugFile(`opencode-hook-bridge: dispatcher exited ${proc.status}, allowing call`);
    return;
  }
  const stdout = (proc.stdout || "").trim();
  if (!stdout) {
    suppressDispatcherStderr();
    return;
  }
  let result;
  try {
    result = JSON.parse(stdout);
  } catch (error) {
    suppressDispatcherStderr();
    logToDebugFile(`opencode-hook-bridge: invalid dispatcher JSON, allowing call: ${error.message}`);
    return;
  }
  if (result?.decision === "block") {
    suppressDispatcherStderr();
    throw new Error(result.reason || "blocked by agents-hooks/v1");
  }
  suppressDispatcherStderr();
}

export const AgentToolsHookBridge = async (ctx = {}) => {
  const sessionParents = new Map();
  return {
    "tool.execute.before": async (input, output) => {
      const sessionParentID = await resolveSessionParentID(ctx, input?.sessionID, sessionParents);
      runBridge("tool.execute.before", input, output, ctx, sessionParentID);
    },
    "tool.execute.after": async (input, output) => {
      try {
        const sessionParentID = await resolveSessionParentID(ctx, input?.sessionID, sessionParents);
        runBridge("tool.execute.after", input, output, ctx, sessionParentID);
      } catch (error) {
        logToDebugFile(
          `opencode-hook-bridge: post-write hook reported after tool execution; logging as feedback because the write already landed: ${error.message}`,
        );
      }
    },
  };
};
