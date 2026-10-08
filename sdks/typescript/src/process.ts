import { spawn, type ChildProcess } from "node:child_process";
import { createRequire } from "node:module";
import { DuckAIError } from "./errors.js";

export function runtime(): "node" | "bun" | "deno" {
  if ("Deno" in globalThis) return "deno";
  if (process.versions.bun) return "bun";
  return "node";
}
export const packageRequire = createRequire(
  typeof __filename === "string" ? __filename : import.meta.url,
);
export function spawnIsolated(
  executable: string,
  args: string[],
): ChildProcess {
  return spawn(executable, args, {
    stdio: "ignore",
    windowsHide: true,
    detached: process.platform !== "win32",
  });
}
export async function stopProcess(processToStop?: ChildProcess): Promise<void> {
  if (
    !processToStop?.pid ||
    processToStop.exitCode !== null ||
    processToStop.signalCode !== null
  )
    return;
  const exited = new Promise<void>((resolve) =>
    processToStop.once("exit", () => resolve()),
  );
  if (process.platform === "win32" && processToStop.pid) {
    const killer = spawn(
      "taskkill",
      ["/PID", String(processToStop.pid), "/T", "/F"],
      { stdio: "ignore", windowsHide: true },
    );
    await new Promise<void>((resolve) => {
      killer.once("exit", () => resolve());
      killer.once("error", () => resolve());
    });
  } else if (processToStop.pid) {
    try {
      process.kill(-processToStop.pid, "SIGKILL");
    } catch {
      /* Already exited. */
    }
  }
  processToStop.kill("SIGKILL");
  await exited;
}
export async function runProcess(
  executable: string,
  args: string[],
  timeout: number,
  signal?: AbortSignal,
): Promise<void> {
  signal?.throwIfAborted();
  const child = spawnIsolated(executable, args);
  const timer = setTimeout(() => {
    void stopProcess(child);
  }, timeout);
  const abort = () => {
    void stopProcess(child);
  };
  signal?.addEventListener("abort", abort, { once: true });
  const started = Date.now();
  try {
    await new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code) => {
        if (signal?.aborted) reject(signal.reason);
        else if (Date.now() - started >= timeout)
          reject(new DuckAIError(504, "Background process timed out"));
        else if (code !== 0)
          reject(new DuckAIError(503, "Background process failed"));
        else resolve();
      });
    });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
    await stopProcess(child);
  }
}
