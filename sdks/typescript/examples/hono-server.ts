import { DuckAI } from "@rendertk/duckai-sdk";
import { createChatApp } from "./hono.ts";

const client = new DuckAI();
const app = createChatApp(client);
const port = 3000;
const globals = globalThis as typeof globalThis & {
  Bun?: {
    serve(options: { fetch: typeof app.fetch; port: number }): { stop(): void };
  };
  Deno?: {
    serve(
      options: { port: number },
      handler: typeof app.fetch,
    ): { shutdown(): Promise<void> };
  };
};
let stop: () => void | Promise<void>;
if (globals.Bun) {
  const server = globals.Bun.serve({ fetch: app.fetch, port });
  stop = () => server.stop();
} else if (globals.Deno) {
  const server = globals.Deno.serve({ port }, app.fetch);
  stop = () => server.shutdown();
} else {
  const { serve } = await import("@hono/node-server");
  const server = serve({ fetch: app.fetch, port });
  stop = () => new Promise<void>((resolve) => server.close(() => resolve()));
}
async function shutdown() {
  await client.close();
  await stop();
}
process.once("SIGINT", () => {
  void shutdown();
});
process.once("SIGTERM", () => {
  void shutdown();
});
console.log(`Duck.ai chat: http://localhost:${port}/chat`);
