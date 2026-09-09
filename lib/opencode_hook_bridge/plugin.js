import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pluginPath = realpathSync(fileURLToPath(import.meta.url));
const libDir = resolve(dirname(pluginPath), "..");
const DEFAULT_DISPATCHER_TIMEOUT_MS = 1000000;
const DISPATCHER_TIMEOUT_MS = resolveDispatcherTimeout();

function resolveDispatcherTimeout() {
  const parsed = Number.parseInt(
    process.env.OPENCODE_HOOK_BRIDGE_TIMEOUT_MS || String(DEFAULT_DISPATCHER_TIMEOUT_MS),
    10,
  );
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_DISPATCHER_TIMEOUT_MS;
}

function resolveDebugLogPath() {
  return (
    process.env.OPENCODE_HOOK_BRIDGE_DEBUG_LOG ||
    join(homedir(), ".cache", "agent-tools", "hook-bridge-debug.log")
  );
}

function logSuppressedDispatcherStderr(text) {
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
  console.error(`opencode-hook-bridge: dispatcher timed out after tool execution, allowing call: ${detail}`);
}

function failOnDispatcherSignal(hook, signal) {
  if (hook === "tool.execute.before") {
    throw new Error(`opencode hook bridge dispatcher terminated by ${signal} before tool execution`);
  }
  console.error(`opencode-hook-bridge: dispatcher terminated by ${signal} after tool execution, allowing call`);
}

function runBridge(hook, input, output, ctx) {
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
    console.error(`opencode-hook-bridge: dispatcher setup failed, allowing call: ${error.message}`);
    return;
  }

  const dispatcherStderr = proc.stderr || "";
  const forwardDispatcherStderr = () => {
    if (dispatcherStderr) process.stderr.write(dispatcherStderr);
  };
  const suppressDispatcherStderr = () => {
    if (dispatcherStderr) logSuppressedDispatcherStderr(dispatcherStderr);
  };
  if (proc.error) {
    if (proc.error.code === "ETIMEDOUT") {
      forwardDispatcherStderr();
      failOnDispatcherTimeout(hook, proc.error.message);
      return;
    }
    forwardDispatcherStderr();
    console.error(`opencode-hook-bridge: dispatcher launch failed, allowing call: ${proc.error.message}`);
    return;
  }
  if (proc.signal) {
    forwardDispatcherStderr();
    failOnDispatcherSignal(hook, proc.signal);
    return;
  }
  if (proc.status !== 0) {
    forwardDispatcherStderr();
    console.error(`opencode-hook-bridge: dispatcher exited ${proc.status}, allowing call`);
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
    forwardDispatcherStderr();
    console.error(`opencode-hook-bridge: invalid dispatcher JSON, allowing call: ${error.message}`);
    return;
  }
  if (result?.decision === "block") {
    forwardDispatcherStderr();
    throw new Error(result.reason || "blocked by agents-hooks/v1");
  }
  suppressDispatcherStderr();
}

export const AgentToolsHookBridge = async (ctx = {}) => ({
  "tool.execute.before": async (input, output) => runBridge("tool.execute.before", input, output, ctx),
  "tool.execute.after": async (input, output) => {
    try {
      runBridge("tool.execute.after", input, output, ctx);
    } catch (error) {
      console.error(`opencode-hook-bridge: post-write hook reported after tool execution; logging as feedback because the write already landed: ${error.message}`);
    }
  },
});
