/** Duck.ai challenges are evaluated only in the site's Chromium iframe. */
import { randomUUID } from "node:crypto";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createServer } from "node:net";
import type { Browser, Page } from "playwright";
import type { ChildProcess } from "node:child_process";
import type { FetchFunction, Settings, TokenProvider } from "./types.js";
import { DuckAIError, upstreamError } from "./errors.js";
import {
  packageRequire,
  runProcess,
  runtime,
  spawnIsolated,
  stopProcess,
} from "./process.js";

const ORIGIN = "https://duck.ai";
const SOLVE_CHALLENGE = async (encoded: string): Promise<string> => {
  const started = Date.now();
  const result = await eval(atob(encoded));
  if (!result || !Array.isArray(result.client_hashes))
    throw new Error("Unexpected Duck.ai challenge result");
  const hashes = await Promise.all(
    result.client_hashes.map(async (value: string) => {
      const digest = new Uint8Array(
        await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
      );
      return btoa(
        Array.from(digest, (byte) => String.fromCharCode(byte)).join(""),
      );
    }),
  );
  const stack = new Error()
    .stack!.split("\n")
    .slice(1, 6)
    .map((line) => line.trim())
    .join("\n");
  return btoa(
    JSON.stringify({
      ...result,
      client_hashes: hashes,
      meta: {
        ...result.meta,
        origin: window.top!.location.origin,
        stack,
        duration: String(Date.now() - started),
      },
    }),
  );
};

export class Mutex {
  private tail = Promise.resolve();
  async acquire(signal?: AbortSignal): Promise<() => void> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    let abort: (() => void) | undefined;
    try {
      if (signal) {
        const aborted = new Promise<never>((_, reject) => {
          abort = () => reject(signal.reason);
          signal.addEventListener("abort", abort, { once: true });
          if (signal.aborted) abort();
        });
        await Promise.race([previous, aborted]);
      } else await previous;
    } catch (error) {
      void previous.then(release);
      throw error;
    } finally {
      if (abort) signal?.removeEventListener("abort", abort);
    }
    if (signal?.aborted) {
      release();
      signal.throwIfAborted();
    }
    return release;
  }
}

async function freePort(): Promise<number> {
  const server = createServer();
  return await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("No local port"));
        return;
      }
      server.close((error) => (error ? reject(error) : resolve(address.port)));
    });
  });
}
async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}
async function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  await new Promise<void>((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", abort, { once: true });
  });
}

export class AnonymousTokenProvider implements TokenProvider {
  private readonly journey = randomUUID().replaceAll("-", "");
  private readonly lock = new Mutex();
  private readonly lifetime = new AbortController();
  private browser?: Browser;
  private page?: Page;
  private process?: ChildProcess;
  private profile?: string;
  private closed = false;
  private resetting = Promise.resolve();
  constructor(
    private readonly settings: Settings,
    private readonly fetch: FetchFunction,
  ) {}

  private async reset(): Promise<void> {
    const page = this.page,
      browser = this.browser,
      process = this.process,
      profile = this.profile;
    this.page = undefined;
    this.browser = undefined;
    this.process = undefined;
    this.profile = undefined;
    this.resetting = this.resetting.then(async () => {
      try {
        await page?.close().catch(() => {});
      } finally {
        try {
          await browser?.close().catch(() => {});
        } finally {
          await stopProcess(process);
          if (profile) await rm(profile, { recursive: true, force: true });
        }
      }
    });
    await this.resetting;
  }
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.lifetime.abort();
    await this.reset();
    const release = await this.lock.acquire();
    try {
      await this.reset();
    } finally {
      release();
    }
  }
  private async prepareRuntime(signal: AbortSignal): Promise<void> {
    const { chromium } = await import("playwright");
    if (
      this.settings.browserExecutable ||
      this.settings.browserCDPUrl ||
      this.settings.browserChannel
    )
      return;
    if (await exists(chromium.executablePath())) return;
    if (!this.settings.browserAutoInstall)
      throw new DuckAIError(
        503,
        "Chromium is missing; run npx playwright install chromium --no-shell",
      );
    const cli = join(
      dirname(packageRequire.resolve("playwright/package.json")),
      "cli.js",
    );
    const args = runtime() === "deno" ? ["run", "-A", cli] : [cli];
    await runProcess(
      process.execPath,
      [...args, "install", "chromium", "--no-shell"],
      this.settings.browserInstallTimeout,
      signal,
    );
    if (!(await exists(chromium.executablePath())))
      throw new DuckAIError(503, "Chromium installation failed");
  }
  private async metadata(
    signal: AbortSignal,
  ): Promise<{ userAgent: string; version: string }> {
    signal.throwIfAborted();
    if (!this.page || this.page.isClosed()) {
      const { chromium } = await import("playwright");
      if (this.settings.browserCDPUrl) {
        this.browser = await chromium.connectOverCDP(
          this.settings.browserCDPUrl,
        );
      } else if (this.settings.browserChannel) {
        this.browser = await chromium.launch({
          channel: this.settings.browserChannel,
          headless: this.settings.headless,
          args: ["--disable-blink-features=AutomationControlled"],
          ignoreDefaultArgs: ["--enable-automation"],
        });
      } else {
        const executable =
          this.settings.browserExecutable ?? chromium.executablePath();
        if (!(await exists(executable)))
          throw new DuckAIError(503, "Chromium executable is missing");
        const port = await freePort();
        this.profile = await mkdtemp(join(tmpdir(), "duckai-anonymous-"));
        const args = [
          `--remote-debugging-port=${port}`,
          "--remote-debugging-address=127.0.0.1",
          `--user-data-dir=${this.profile}`,
          "--no-first-run",
          "--no-default-browser-check",
          "--disable-extensions",
          "--disable-background-timer-throttling",
          "--disable-backgrounding-occluded-windows",
          "--disable-renderer-backgrounding",
          "--disable-sync",
          "--password-store=basic",
          "--use-mock-keychain",
          "--disable-search-engine-choice-screen",
          "--disable-features=GlobalMediaControls,MediaRouter,Translate,OptimizationHints,PaintHolding",
          "about:blank",
        ];
        if (this.settings.headless)
          args.unshift(
            "--headless=new",
            "--disable-blink-features=AutomationControlled",
          );
        if (process.getuid?.() === 0) args.unshift("--no-sandbox");
        this.process = spawnIsolated(executable, args);
        let launchError: Error | undefined;
        this.process.once("error", (error) => {
          launchError = error;
        });
        const endpoint = `http://127.0.0.1:${port}`;
        while (true) {
          signal.throwIfAborted();
          if (launchError) throw launchError;
          if (
            this.process.exitCode !== null ||
            this.process.signalCode !== null
          )
            throw new DuckAIError(503, "Chromium exited during startup");
          try {
            const response = await globalThis.fetch(
              `${endpoint}/json/version`,
              { signal: AbortSignal.any([signal, AbortSignal.timeout(1000)]) },
            );
            await response.body?.cancel();
            if (response.ok) break;
          } catch {
            signal.throwIfAborted();
          }
          await pause(100, signal);
        }
        this.browser = await chromium.connectOverCDP(endpoint);
      }
      signal.throwIfAborted();
      const context =
        this.browser.contexts()[0] ?? (await this.browser.newContext());
      this.page = await context.newPage();
      this.page.setDefaultTimeout(this.settings.tokenGenerationTimeout);
      if (this.settings.headless) {
        const userAgent = await this.page.evaluate(() => navigator.userAgent);
        if (userAgent.includes("HeadlessChrome/")) {
          const session = await context.newCDPSession(this.page);
          await session.send("Network.setUserAgentOverride", {
            userAgent: userAgent.replace("HeadlessChrome/", "Chrome/"),
          });
        }
      }
      await this.page.goto(ORIGIN, { waitUntil: "domcontentloaded" });
      await this.page.waitForSelector("iframe#jsa", { state: "attached" });
    }
    return await this.page.evaluate(() => ({
      userAgent: navigator.userAgent,
      version: `${document.documentElement.getAttribute("data-version-tag")}-${document.documentElement.getAttribute("data-version-sha")}`,
    }));
  }
  async headers(callerSignal?: AbortSignal): Promise<Record<string, string>> {
    const lifetime = callerSignal
      ? AbortSignal.any([callerSignal, this.lifetime.signal])
      : this.lifetime.signal;
    const release = await this.lock.acquire(lifetime);
    let abort: (() => void) | undefined;
    let signal = lifetime;
    try {
      if (this.closed) throw new Error("Token provider is closed");
      await this.prepareRuntime(lifetime);
      signal = AbortSignal.any([
        lifetime,
        AbortSignal.timeout(this.settings.tokenGenerationTimeout),
      ]);
      abort = () => {
        void this.reset();
      };
      signal.addEventListener("abort", abort, { once: true });
      const started = Date.now();
      const metadata = await this.metadata(signal);
      const headers = {
        "user-agent": metadata.userAgent,
        accept: "application/json",
        referer: `${ORIGIN}/`,
        origin: ORIGIN,
        "x-ddg-journey-id": this.journey,
      };
      const auth = await this.fetch(`${ORIGIN}/duckchat/v1/auth/token`, {
        headers,
        signal,
        redirect: "error",
      });
      await auth.body?.cancel();
      if (!auth.ok)
        throw upstreamError(
          auth.status,
          "Duck.ai rejected anonymous bootstrap",
          Object.fromEntries(auth.headers),
        );
      const status = await this.fetch(`${ORIGIN}/duckchat/v1/status`, {
        headers: {
          ...headers,
          "x-vqd-accept": "1",
          "cache-control": "no-store",
        },
        signal,
        redirect: "error",
      });
      await status.body?.cancel();
      if (!status.ok)
        throw upstreamError(
          status.status,
          "Duck.ai rejected anonymous bootstrap",
          Object.fromEntries(status.headers),
        );
      const challenge = status.headers.get("x-vqd-hash-1");
      if (!challenge)
        throw new DuckAIError(
          502,
          "Duck.ai status returned no browser challenge",
        );
      const element = await this.page!.$("iframe#jsa");
      const frame = await element?.contentFrame();
      if (!frame)
        throw new DuckAIError(502, "Duck.ai challenge iframe is unavailable");
      const token = (await frame.evaluate(
        SOLVE_CHALLENGE,
        challenge,
      )) as string;
      signal.throwIfAborted();
      return {
        "x-vqd-hash-1": token,
        "x-fe-version": metadata.version,
        "x-fe-signals": Buffer.from(
          JSON.stringify({
            start: started,
            events: [],
            end: Date.now() - started,
          }),
        ).toString("base64"),
        "x-ddg-journey-id": this.journey,
        "user-agent": metadata.userAgent,
      };
    } catch (error) {
      await this.reset();
      if (lifetime.aborted) throw lifetime.reason;
      if (signal.aborted)
        throw new DuckAIError(
          504,
          "Anonymous token generation timed out",
          {},
          { cause: error },
        );
      if (error instanceof DuckAIError) throw error;
      throw new DuckAIError(
        503,
        "Chromium token generation failed. Install Chromium with npx playwright install chromium --no-shell, or set browserExecutable.",
        {},
        { cause: error },
      );
    } finally {
      if (abort) signal.removeEventListener("abort", abort);
      release();
    }
  }
}
