import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { DuckAI, DuckAIError, type Message } from "@rendertk/duckai-sdk";

/** One shared client; each request supplies its own conversation history. */
export function createChatApp(client: DuckAI): Hono {
  const app = new Hono();
  app.post("/chat", async (c) => {
    const data = await c.req
      .json<{ prompt?: unknown; messages?: Message[]; model?: string }>()
      .catch(() => null);
    if (!data || typeof data.prompt !== "string" || !data.prompt.trim())
      return c.json({ error: "Provide a prompt" }, 400);
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, c.req.raw.signal]);
    return streamSSE(c, async (output) => {
      output.onAbort(() => controller.abort());
      const stream = client.stream(data.prompt as string, {
        messages: data.messages,
        model: data.model,
        signal,
      });
      try {
        for await (const delta of stream)
          await output.writeSSE({
            event: "message",
            data: JSON.stringify({ delta }),
          });
        await output.writeSSE({
          event: "done",
          data: JSON.stringify({
            text: stream.response.text,
            assistantMessage: stream.response.assistantMessage,
          }),
        });
      } catch (error) {
        if (!signal.aborted)
          await output.writeSSE({
            event: "error",
            data: JSON.stringify({
              message: error instanceof Error ? error.message : "Chat failed",
              status: error instanceof DuckAIError ? error.statusCode : 500,
              retryAfter:
                error instanceof DuckAIError ? error.retryAfter : undefined,
            }),
          });
      } finally {
        await stream.close();
      }
    });
  });
  return app;
}
