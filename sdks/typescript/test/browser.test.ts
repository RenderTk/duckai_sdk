import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";
import { chromium } from "playwright";
import { DuckAI } from "@rendertk/duckai-sdk";
import { spawn } from "node:child_process";

// Opt-in browser integration test: synthetic Duck.ai pages, no upstream requests.
test(
  "headless CDP bootstrap solves iframe challenge and preserves borrowed browser",
  {
    skip: process.env.DUCKAI_TEST_BROWSER !== "1",
    timeout: 30_000,
  },
  async () => {
    const profile = await mkdtemp(join(tmpdir(), "duckai-cdp-test-"));
    const listener = createServer();
    await new Promise<void>((resolve) =>
      listener.listen(0, "127.0.0.1", resolve),
    );
    const address = listener.address();
    assert.ok(address && typeof address !== "string");
    const port = address.port;
    await new Promise<void>((resolve) => listener.close(() => resolve()));
    const args = [
      "--headless=new",
      "--disable-blink-features=AutomationControlled",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "about:blank",
    ];
    // Ubuntu CI runners restrict unprivileged user namespaces for downloaded Chromium.
    // This exception is only for the isolated synthetic-page test browser.
    if (
      process.getuid?.() === 0 ||
      (process.platform === "linux" && process.env.CI === "true")
    )
      args.unshift("--no-sandbox");
    const child = spawn(chromium.executablePath(), args, {
      stdio: ["ignore", "ignore", "pipe"],
      detached: process.platform !== "win32",
      windowsHide: true,
    });
    let startupLog = "";
    let startupError: Error | undefined;
    child.stderr?.on("data", (chunk: Buffer) => {
      startupLog = (startupLog + chunk.toString()).slice(-8192);
    });
    child.once("error", (error) => {
      startupError = error;
    });
    let browser;
    let ai: DuckAI | undefined;
    try {
      const endpoint = `http://127.0.0.1:${port}`;
      let ready = false;
      for (let i = 0; i < 300; i++) {
        if (startupError) throw startupError;
        if (child.exitCode !== null || child.signalCode !== null)
          throw new Error(`Test Chromium exited during startup: ${startupLog}`);
        try {
          const response = await fetch(`${endpoint}/json/version`);
          await response.body?.cancel();
          if (response.ok) {
            ready = true;
            break;
          }
        } catch {
          /* Startup. */
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      assert.ok(ready, `Test Chromium did not become ready: ${startupLog}`);
      browser = await chromium.connectOverCDP(endpoint);
      const context = browser.contexts()[0]!;
      const userPage = await context.newPage();
      await context.route("https://duck.ai/**", (route) =>
        route.fulfill({
          contentType: "text/html",
          body: route.request().url().endsWith("/iframe")
            ? "<html>Challenge frame</html>"
            : '<html data-version-tag="test" data-version-sha="abc"><iframe id="jsa" src="/iframe"></iframe></html>',
        }),
      );
      let chats = 0,
        bootstraps = 0;
      const challenge = Buffer.from(
        '({client_hashes:["synthetic"],meta:{retained:true}})',
      ).toString("base64");
      ai = new DuckAI({
        browserCDPUrl: endpoint,
        timeout: 20,
        fetch: async (input, init) => {
          const url = String(input);
          if (url.endsWith("auth/token")) return new Response("{}");
          if (url.endsWith("/status")) {
            bootstraps++;
            return new Response("{}", {
              headers: { "x-vqd-hash-1": challenge },
            });
          }
          chats++;
          const headers = new Headers(init?.headers);
          const token = JSON.parse(
            Buffer.from(headers.get("x-vqd-hash-1")!, "base64").toString(),
          );
          assert.deepEqual(token.client_hashes, [
            createHash("sha256").update("synthetic").digest("base64"),
          ]);
          assert.equal(token.meta.origin, "https://duck.ai");
          assert.equal(token.meta.retained, true);
          assert.equal(headers.get("x-fe-version"), "test-abc");
          assert.ok(!headers.get("user-agent")!.includes("HeadlessChrome"));
          return new Response('data: {"message":"OK"}\n\ndata: [DONE]\n\n', {
            headers: { "content-type": "text/event-stream" },
          });
        },
      });
      assert.equal(chats, 0);
      assert.equal((await ai.chat("one")).text, "OK");
      assert.equal((await ai.chat("two")).text, "OK");
      assert.equal(bootstraps, 2);
      await ai.close();
      assert.equal(browser.isConnected(), true);
      assert.equal(userPage.isClosed(), false);
      assert.equal(await userPage.evaluate(() => 21 * 2), 42);
      assert.equal(context.pages().length, 2); // Original tab and user's tab only.
    } finally {
      await ai?.close();
      if (browser?.isConnected()) {
        const session = await browser.newBrowserCDPSession();
        await session.send("Browser.close").catch(() => {});
      }
      await browser?.close();
      if (child.pid && child.exitCode === null && child.signalCode === null) {
        const exited = new Promise<void>((resolve) =>
          child.once("exit", () => resolve()),
        );
        if (child.pid && process.platform !== "win32") {
          try {
            process.kill(-child.pid, "SIGKILL");
          } catch {
            /* Already closed. */
          }
        }
        child.kill("SIGKILL");
        await exited;
      }
      await rm(profile, { recursive: true, force: true });
    }
  },
);
