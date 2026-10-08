import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import sharp from "sharp";
import { PDFDocument } from "pdf-lib";
import {
  DuckAI,
  Attachment,
  AttachmentError,
  ChallengeError,
  RateLimitError,
  IncompleteResponseError,
  DuckAIError,
  Model,
  listModels,
  getModel,
  supportedFileExtensions,
  type FetchFunction,
  type TokenProvider,
  type ChatRequest,
} from "@rendertk/duckai-sdk";
import { createChatApp } from "../examples/hono.ts";

const root = new URL("../../../", import.meta.url);
const contract = JSON.parse(
  await readFile(new URL("protocol/fixtures/chat.json", root), "utf8"),
) as {
  model: string;
  prompt: string;
  sse: string;
  text: string;
  request: ChatRequest;
  parts: Record<string, unknown>[];
};
const documents = JSON.parse(
  await readFile(new URL("protocol/fixtures/documents.json", root), "utf8"),
) as { filename: string; data: string; contains: string[] }[];
const headers = { "x-vqd-hash-1": "test" };
const encoder = new TextEncoder();
const sse = (text = contract.sse): Response =>
  new Response(text, { headers: { "content-type": "text/event-stream" } });
const fetcher =
  (
    handler: (url: string, init: RequestInit) => Response | Promise<Response>,
  ): FetchFunction =>
  async (input, init) =>
    handler(String(input), init ?? {});
const fixture = (): Attachment =>
  Attachment.fromBytes(
    Buffer.from(documents[0]!.data, "base64"),
    documents[0]!.filename,
  );

test("shared protocol payload, collected Unicode and opaque parts", async () => {
  let sent: unknown;
  const ai = new DuckAI({
    model: contract.model,
    headers,
    fetch: fetcher((url, init) => {
      assert.equal(url, "https://duck.ai/duckchat/v1/chat");
      assert.equal(init.method, "POST");
      sent = JSON.parse(String(init.body));
      assert.equal(new Headers(init.headers).get("x-vqd-hash-1"), "test");
      return sse();
    }),
  });
  try {
    const response = await ai.chat(contract.prompt);
    assert.deepEqual(sent, contract.request);
    assert.equal(response.text, contract.text);
    assert.equal(response.done, true);
    assert.deepEqual(response.assistantMessage.parts, contract.parts);
    const copy = response.assistantMessage;
    copy.parts![0]!.encryptedText = "changed";
    assert.equal(response.assistantMessage.parts![0]!.encryptedText, "opaque");
  } finally {
    await ai.close();
  }
});

test("SSE handles byte-sized UTF-8/CRLF chunks, multiline data and final frame", async () => {
  let cancelled = false;
  const bytes = encoder.encode(
    'event: message\r\ndata: {"message":\r\ndata: "café ☃"}\r\n\r\ndata: [DONE]',
  );
  let offset = 0;
  const ai = new DuckAI({
    headers,
    fetch: fetcher(
      () =>
        new Response(
          new ReadableStream({
            pull(controller) {
              if (offset < bytes.length)
                controller.enqueue(bytes.slice(offset, ++offset));
              else controller.close();
            },
            cancel() {
              cancelled = true;
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
    ),
  });
  try {
    assert.equal((await ai.chat("hello")).text, "café ☃");
    assert.ok(cancelled || offset === bytes.length);
  } finally {
    await ai.close();
  }
});

test("raw events preserve non-text data and parts updates", async () => {
  const ai = new DuckAI({
    headers,
    fetch: fetcher(() =>
      sse(
        'data: {"message":"A"}\n\ndata: {"parts":[{"id":"r","encryptedText":"one"}]}\n\ndata: {"parts":[{"id":"r","encryptedText":"two","extra":true}]}\n\ndata: [DONE]\n\n',
      ),
    ),
  });
  try {
    const stream = ai.events("test");
    const events = [];
    for await (const event of stream) events.push(event);
    assert.equal(events.length, 3);
    assert.equal(stream.response.text, "A");
    assert.deepEqual(stream.response.assistantMessage.parts, [
      { id: "r", encryptedText: "two", extra: true },
    ]);
  } finally {
    await ai.close();
  }
});

test("early break cancels upstream and does not commit a conversation", async () => {
  let cancelled = false;
  const ai = new DuckAI({
    headers,
    fetch: fetcher(
      () =>
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode('data: {"message":"partial"}\n\n'),
              );
            },
            cancel() {
              cancelled = true;
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
    ),
  });
  try {
    const conversation = ai.conversation();
    for await (const delta of conversation.stream("hello")) {
      assert.equal(delta, "partial");
      break;
    }
    assert.deepEqual(conversation.messages, []);
    assert.equal(cancelled, true);
  } finally {
    await ai.close();
  }
});

test("incomplete replies retain partial output and never commit", async () => {
  const ai = new DuckAI({
    headers,
    fetch: fetcher(() => sse('data: {"message":"partial"}\n\n')),
  });
  try {
    const conversation = ai.conversation();
    await assert.rejects(conversation.chat("hello"), (error) => {
      assert.ok(error instanceof IncompleteResponseError);
      assert.equal(error.response.text, "partial");
      return true;
    });
    assert.deepEqual(conversation.messages, []);
  } finally {
    await ai.close();
  }
});

for (const status of [418, 429, 500])
  test(`HTTP ${status} is typed, bounded and not retried`, async () => {
    let calls = 0;
    const ai = new DuckAI({
      headers,
      fetch: fetcher(() => {
        calls++;
        return new Response('{"message":"Rejected"}', {
          status,
          headers: { "retry-after": "30" },
        });
      }),
    });
    try {
      await assert.rejects(ai.chat("hello"), (error) => {
        assert.ok(
          error instanceof
            (status === 418
              ? ChallengeError
              : status === 429
                ? RateLimitError
                : DuckAIError),
        );
        assert.equal(error.statusCode, status);
        assert.equal(error.retryAfter, "30");
        return true;
      });
      assert.equal(calls, 1);
    } finally {
      await ai.close();
    }
  });

test("SSE errors preserve typed status", async () => {
  const ai = new DuckAI({
    headers,
    fetch: fetcher(() =>
      sse('event: error\ndata: {"status":429,"message":"limit"}\n\n'),
    ),
  });
  try {
    await assert.rejects(ai.chat("hello"), RateLimitError);
  } finally {
    await ai.close();
  }
});

test("response collection bound applies to unterminated frames", async () => {
  const ai = new DuckAI({
    headers,
    settings: { maxCollectedBytes: 10 },
    fetch: fetcher(() => sse("data: " + "x".repeat(100))),
  });
  try {
    await assert.rejects(ai.chat("hello"), /collection limit/);
  } finally {
    await ai.close();
  }
});

test("non-SSE responses and network failures are explicit", async () => {
  for (const transport of [
    fetcher(() => new Response("HTML")),
    fetcher(() => {
      throw new TypeError("connect failed");
    }),
  ]) {
    const ai = new DuckAI({ headers, fetch: transport });
    try {
      await assert.rejects(ai.chat("hello"), DuckAIError);
    } finally {
      await ai.close();
    }
  }
});

test("AbortSignal and timeout terminate a pending read", async () => {
  for (const abortByCaller of [true, false]) {
    const controller = new AbortController();
    let started!: () => void;
    const ready = new Promise<void>((resolve) => {
      started = resolve;
    });
    const ai = new DuckAI({
      headers,
      timeout: 50,
      fetch: fetcher(
        (_url, init) =>
          new Response(
            new ReadableStream({
              start(output) {
                init.signal!.addEventListener(
                  "abort",
                  () => output.error(init.signal!.reason),
                  { once: true },
                );
                started();
              },
            }),
            { headers: { "content-type": "text/event-stream" } },
          ),
      ),
    });
    try {
      const promise = ai.chat("hello", { signal: controller.signal });
      await ready;
      if (abortByCaller) controller.abort(new Error("cancelled by caller"));
      await assert.rejects(promise, (error) => {
        if (abortByCaller)
          assert.equal((error as Error).message, "cancelled by caller");
        else {
          assert.ok(error instanceof DuckAIError);
          assert.equal(error.statusCode, 504);
        }
        return true;
      });
    } finally {
      await ai.close();
    }
  }
});

test("client close cancels live streams, is idempotent and forbids reuse", async () => {
  let tokenClosed = 0;
  const provider: TokenProvider = {
    async headers() {
      return headers;
    },
    async close() {
      tokenClosed++;
    },
  };
  const ai = new DuckAI({
    tokenProvider: provider,
    fetch: fetcher(
      (_url, init) =>
        new Response(
          new ReadableStream({
            start(output) {
              output.enqueue(encoder.encode('data: {"message":"first"}\n\n'));
              init.signal!.addEventListener(
                "abort",
                () => output.error(init.signal!.reason),
                { once: true },
              );
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
    ),
  });
  const stream = ai.stream("hello");
  assert.equal((await stream.next()).value, "first");
  const pending = stream.next();
  const rejected = assert.rejects(pending);
  await ai.close();
  await rejected;
  await ai.close();
  assert.equal(tokenClosed, 1);
  assert.throws(() => ai.stream("again"), /closed/);
});

test("conversation turns serialize, histories are isolated and copies are defensive", async () => {
  const requests: ChatRequest[] = [];
  const ai = new DuckAI({
    headers,
    fetch: fetcher((_url, init) => {
      requests.push(JSON.parse(String(init.body)));
      return sse();
    }),
  });
  try {
    const a = ai.conversation(),
      b = ai.conversation();
    await Promise.all([a.chat("first"), a.chat("second")]);
    assert.equal(requests[0]!.messages.length, 1);
    assert.equal(requests[1]!.messages.length, 3);
    assert.equal(a.messages.length, 4);
    const copy = a.messages;
    copy[0]!.content = "changed";
    assert.equal(a.messages[0]!.content, "first");
    await b.chat("independent");
    assert.equal(requests[2]!.messages.length, 1);
    a.clear();
    assert.equal(a.messages.length, 0);
  } finally {
    await ai.close();
  }
});

test("queued conversation cancellation releases its lock without waiting for a reply", async () => {
  const ai = new DuckAI({
    headers,
    fetch: fetcher(
      (_url, init) =>
        new Response(
          new ReadableStream({
            start(out) {
              out.enqueue(encoder.encode('data: {"message":"first"}\n\n'));
              init.signal!.addEventListener(
                "abort",
                () => out.error(init.signal!.reason),
                { once: true },
              );
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
    ),
  });
  try {
    const c = ai.conversation();
    const first = c.stream("one");
    await first.next();
    assert.throws(() => c.clear(), /during a turn/);
    const controller = new AbortController();
    const next = c.chat("two", { signal: controller.signal });
    const rejected = assert.rejects(next, /queued cancelled/);
    controller.abort(new Error("queued cancelled"));
    await rejected;
    await first.close();
    assert.deepEqual(c.messages, []);
  } finally {
    await ai.close();
  }
});

test("token providers are lazy, called freshly per request and closed", async () => {
  let calls = 0,
    closed = false;
  const ai = new DuckAI({
    tokenProvider: {
      async headers() {
        return { "x-vqd-hash-1": String(++calls) };
      },
      async close() {
        closed = true;
      },
    },
    fetch: fetcher((_url, init) => {
      assert.equal(
        new Headers(init.headers).get("x-vqd-hash-1"),
        String(calls),
      );
      return sse();
    }),
  });
  assert.equal(calls, 0);
  ai.listModels();
  ai.attachmentLimits();
  assert.equal(calls, 0);
  try {
    await ai.chat("one");
    await ai.chat("two");
    assert.equal(calls, 2);
  } finally {
    await ai.close();
  }
  assert.equal(closed, true);
});

test("every model uses shared defaults, and custom models remain usable", async () => {
  const models = JSON.parse(
    await readFile(new URL("protocol/models.json", root), "utf8"),
  ) as { id: string; reasoning_efforts: string[]; supports_tools: boolean }[];
  let request: ChatRequest;
  const ai = new DuckAI({
    headers,
    fetch: fetcher((_url, init) => {
      request = JSON.parse(String(init.body));
      return sse();
    }),
  });
  try {
    assert.equal(listModels().length, models.length);
    assert.equal(listModels({ includeSubscriber: false }).length, 6);
    for (const m of models) {
      await ai.chat("hello", { model: m.id });
      assert.equal(request!.model, m.id);
      assert.equal(request!.reasoningEffort, m.reasoning_efforts[0]);
      assert.equal(request!.canUseTools, m.supports_tools);
    }
    await ai.chat("hello", { model: "future-model" });
    assert.equal(request!.model, "future-model");
    assert.equal(getModel("unknown"), undefined);
    await assert.rejects(
      ai.chat("bad", {
        model: Model.MISTRAL_SMALL_4,
        reasoningEffort: "medium",
      }),
      TypeError,
    );
  } finally {
    await ai.close();
  }
});

for (const item of documents)
  test(`shared document contract: ${item.filename}`, async () => {
    const ai = new DuckAI({ model: Model.MISTRAL_SMALL_4 });
    try {
      const parts = await ai.prepareFiles([
        Attachment.fromBytes(Buffer.from(item.data, "base64"), item.filename),
      ]);
      const part = parts[0]!;
      assert.equal(part.type, "text");
      const text = "text" in part ? String(part.text) : "";
      for (const expected of item.contains)
        assert.ok(
          text.includes(expected),
          `${item.filename} missing ${expected}: ${text}`,
        );
    } finally {
      await ai.close();
    }
  });

test("native PDFs and images stay binary; combined document limits apply", async () => {
  const pdf = await PDFDocument.create();
  pdf.addPage();
  const bytes = await pdf.save();
  const image = await sharp({
    create: { width: 900, height: 600, channels: 3, background: "red" },
  })
    .png()
    .toBuffer();
  const ai = new DuckAI();
  try {
    const parts = await ai.prepareFiles([
      Attachment.fromBytes(bytes, "paper.pdf"),
      Attachment.fromBytes(image, "image.png"),
    ]);
    assert.equal(parts[0]!.type, "image");
    assert.equal(parts[1]!.type, "file");
    assert.equal(
      Buffer.from(
        String("content" in parts[1]! ? parts[1]!.content : ""),
        "base64",
      ).equals(Buffer.from(bytes)),
      true,
    );
    const metadata = await sharp(
      Buffer.from(
        String("image" in parts[0]! ? parts[0]!.image : "").split(",")[1]!,
        "base64",
      ),
    ).metadata();
    assert.equal(metadata.width, 512);
    await assert.rejects(
      ai.prepareFiles([Attachment.fromBytes(image, "image.png")], {
        model: Model.MISTRAL_SMALL_4,
      }),
      AttachmentError,
    );
    await assert.rejects(
      ai.prepareFiles([Attachment.fromBytes(bytes, "paper.pdf")], {
        model: Model.GEMMA_4_31B,
      }),
      AttachmentError,
    );
    await assert.rejects(
      ai.prepareFiles([
        Attachment.fromBytes(image, "image.png"),
        Attachment.fromBytes(encoder.encode("x".repeat(4500)), "long.txt"),
      ]),
      /image_text_limit/,
    );
  } finally {
    await ai.close();
  }
});

test("invalid documents, DTDs, Google shortcuts, sizes and native payloads are rejected locally", async () => {
  let calls = 0;
  const ai = new DuckAI({
    headers,
    fetch: fetcher(() => {
      calls++;
      return sse();
    }),
  });
  try {
    for (const [name, data] of [
      ["bad.docx", "not a ZIP"],
      ["bad.fodt", '<!DOCTYPE root [<!ENTITY e "expanded">]><root>&e;</root>'],
      ["link.gdoc", '{"url":"https://example.invalid"}'],
      ["database.accdb", "data"],
      ["empty.txt", "   "],
      ["binary.txt", "abc\0def"],
    ])
      await assert.rejects(
        ai.prepareFiles([Attachment.fromBytes(encoder.encode(data), name!)]),
        AttachmentError,
      );
    await assert.rejects(
      ai.chat("p".repeat(16_000), { files: [fixture()] }),
      /document_text_limit/,
    );
    assert.equal(calls, 0);
    await assert.rejects(
      ai.chat(undefined, {
        messages: [
          {
            role: "user",
            content: [
              {
                type: "image",
                mimeType: "image/png",
                image: "data:image/png;base64,invalid",
              },
            ],
          },
        ],
      }),
      AttachmentError,
    );
    assert.ok(supportedFileExtensions().includes(".xlsb"));
    assert.equal(supportedFileExtensions().includes(".gdoc"), false);
  } finally {
    await ai.close();
  }
});

test("paths, File objects and retained canonical attachments work after source deletion", async () => {
  const directory = await mkdtemp(join(tmpdir(), "duckai-js-test-"));
  const ai = new DuckAI({ headers, fetch: fetcher(() => sse()) });
  try {
    const { writeFile } = await import("node:fs/promises");
    const path = join(directory, "data.csv");
    await writeFile(path, "Product,Count\nOranges,19");
    const parts = await ai.prepareFiles([
      path,
      new File(["Hello"], "text.txt", { type: "text/plain" }),
    ]);
    await rm(path);
    const response = await ai.chat(undefined, {
      messages: [
        {
          role: "user",
          content: [{ type: "text", text: "Analyze" }, ...parts],
        },
      ],
    });
    assert.equal(response.done, true);
  } finally {
    await ai.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("Hono streams real SDK deltas, accepts history and emits a completion event", async () => {
  let body: ChatRequest;
  const ai = new DuckAI({
    headers,
    fetch: fetcher((_url, init) => {
      body = JSON.parse(String(init.body));
      return sse();
    }),
  });
  try {
    const app = createChatApp(ai);
    const response = await app.request("/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        prompt: "hello",
        messages: [
          { role: "user", content: "before" },
          { role: "assistant", content: "previous" },
        ],
      }),
    });
    assert.match(response.headers.get("content-type")!, /text\/event-stream/);
    const text = await response.text();
    assert.ok(text.includes("event: message"));
    assert.ok(text.includes("event: done"));
    assert.equal(body!.messages.length, 3);
    assert.equal(
      (await app.request("/chat", { method: "POST", body: "{}" })).status,
      400,
    );
  } finally {
    await ai.close();
  }
});

test("Hono disconnect aborts the upstream SDK request", async () => {
  let aborted = false;
  const ai = new DuckAI({
    headers,
    fetch: fetcher(
      (_url, init) =>
        new Response(
          new ReadableStream({
            start(out) {
              out.enqueue(encoder.encode('data: {"message":"first"}\n\n'));
              init.signal!.addEventListener(
                "abort",
                () => {
                  aborted = true;
                  out.error(init.signal!.reason);
                },
                { once: true },
              );
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
    ),
  });
  try {
    const app = createChatApp(ai);
    const response = await app.request("/chat", {
      method: "POST",
      body: JSON.stringify({ prompt: "hello" }),
    });
    const reader = response.body!.getReader();
    await reader.read();
    await reader.cancel();
    for (let i = 0; i < 50 && !aborted; i++)
      await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(aborted, true);
  } finally {
    await ai.close();
  }
});
