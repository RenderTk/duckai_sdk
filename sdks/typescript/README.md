# Duck.ai for TypeScript and JavaScript

Native Duck.ai client for Node.js 22.13+, Bun 1.4+ and Deno 2.9+. Includes typed ESM and CommonJS exports, collected replies, incremental streaming, raw events, independent conversations, all catalogue model IDs and native/locally extracted attachments. No Python bridge or desktop app dependency.

## Install from this repository

This package is not yet published to npm. From the repository root:

```sh
npm ci
npm run build
npm pack --workspace @rendertk/duckai-sdk --pack-destination ./output
```

Install the resulting tarball into your application:

```sh
npm install /absolute/path/duckai_sdk/output/rendertk-duckai-sdk-0.2.0.tgz
# Bun: bun add /absolute/path/to/rendertk-duckai-sdk-0.2.0.tgz
# Deno: deno add npm:@rendertk/duckai-sdk is available only after npm publication;
# until then, use the local package workflow below.
```

For local Deno projects, install the tarball with npm into that project's `node_modules` and run with `--node-modules-dir=manual`. This resolves the package's normal bare imports without requiring npm publication.

Chromium is installed automatically on the first chat if missing. For servers or CI, install it during provisioning:

```sh
npx playwright install chromium --no-shell
# Linux hosts may additionally need: npx playwright install-deps chromium
```

Sharp requires a supported native platform. Deno needs subprocess, environment, filesystem, FFI and network permissions; the example commands use `-A`. This SDK targets local/server Node-compatible runtimes, not browser code or edge workers without these capabilities.

## Chat, streams and conversations

```typescript
import { DuckAI, Model } from "@rendertk/duckai-sdk";

const ai = new DuckAI({ model: Model.GPT_6_LUNA });
try {
  const response = await ai.chat("What is recursion?");
  console.log(response.text, response.done);

  const chat = ai.conversation();
  for await (const delta of chat.stream("Remember that my project is called Orbit.")) {
    process.stdout.write(delta);
  }
  console.log((await chat.chat("What is my project's name?")).text);
  console.log(chat.messages); // Defensive copy of successful turns, including opaque parts.
} finally {
  await ai.close();
}
```

CommonJS is also supported:

```javascript
const { DuckAI, Model } = require("@rendertk/duckai-sdk");
```

`ai.stream()` returns an async iterator of text deltas with a collected `stream.response`. `ai.events()` yields raw JSON events and retains upstream fields. A `for await` early break closes the stream; call `await stream.close()` if you stop a manually driven iterator. Both clients and streams support `Symbol.asyncDispose`.

A conversation serializes its turns and commits history only after a complete `[DONE]` response. Interrupted/error turns do not alter history. Different conversations can run concurrently. `conversation.clear()` clears idle history. To supply your own history, use `ai.chat(prompt, { messages })`; unknown message fields and assistant `parts` are preserved.

## Runtime commands

From the repository root after `npm ci` and `npm run build`:

| Runtime | JavaScript chat | Hono server |
| --- | --- | --- |
| Node.js | `node sdks/typescript/examples/chat.mjs` | `node --import tsx sdks/typescript/examples/hono-server.ts` |
| Bun | `bun sdks/typescript/examples/chat.mjs` | `bun sdks/typescript/examples/hono-server.ts` |
| Deno | `deno run -A --node-modules-dir=manual sdks/typescript/examples/chat.mjs` | `deno run -A --node-modules-dir=manual sdks/typescript/examples/hono-server.ts` |

Tests run on all three runtimes in CI. Local live verification used Node.js 25.6.1, Bun 1.4.2 and Deno 2.9.6. Hono is framework integration rather than a core runtime dependency; install `hono` in an application using it, and `@hono/node-server` for Node's HTTP adapter.

## Hono streaming

[`examples/hono.ts`](examples/hono.ts) exports `createChatApp(client)`; [`examples/hono-server.ts`](examples/hono-server.ts) starts the appropriate Node, Bun or Deno HTTP server.

```sh
curl -N http://localhost:3000/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain recursion","model":"gpt-6-luna","messages":[]}'
```

The endpoint sends `message` (text deltas), `done` and `error` SSE events. The `done` payload includes the full assistant message for the next request. Histories are request-scoped; there is no global conversation shared between users. A disconnected SSE consumer aborts upstream work. Once SSE starts, errors are events rather than new HTTP statuses. Add your application's own authentication and request controls when exposing a service.

## Models and request options

```typescript
import { listModels, getModel, Model } from "@rendertk/duckai-sdk";

console.log(listModels()); // Includes accessTier, capabilities and reasoning efforts.
console.log(listModels({ includeSubscriber: false }));
console.log(getModel(Model.GPT_6_LUNA));

const response = await ai.chat("Compare the options", {
  model: Model.MISTRAL_SMALL_4,
  reasoningEffort: "none",
  canUseTools: false,
  canShowGreeting: false,
  signal: AbortSignal.timeout(60_000),
});
```

Known models select their catalogue reasoning/tool defaults and validate reasoning choices. Custom string IDs remain accepted. Premium model IDs are represented, but Duck.ai entitlement is still required: anonymous sessions cannot unlock subscriber models. You can provide fresh authorized captured headers or a custom `tokenProvider` for your deployment.

## Files

```typescript
import { Attachment } from "@rendertk/duckai-sdk";

const response = await ai.chat("Summarize this report", {
  files: ["/absolute/path/report.docx", "/absolute/path/budget.xlsx"],
});
const inMemory = Attachment.fromBytes(bytes, "notes.txt", "text/plain");
const parts = await ai.prepareFiles([inMemory]);
console.log(ai.attachmentLimits());
```

Inputs accept paths, `Attachment`/`{data: Uint8Array, filename, mimeType?}`, or standard `File` objects. Files are read locally, bounded and converted before network submission. Successful conversation histories contain prepared content, so follow-up turns do not reread original files.

PDFs and WebP-normalized images use native attachment parts for models that support them. DOCX, XLSX, PPTX, OpenDocument, CSV, RTF, HTML, email, EPUB and other supported exports become labelled text. Legacy Word/PowerPoint/Publisher formats need a locally installed LibreOffice (set `officeConverterExecutable` if necessary). Spreadsheet formulas use stored values and are not recalculated. Conversion does not execute Office macros. Layout, charts, embedded objects and OCR are not preserved as text. Google `.gdoc`/`.gsheet`/`.gslides` pointers need an actual exported document.

See [the shared file-format guide](https://github.com/RenderTk/duckai_sdk/blob/main/docs/file-formats.md) for detailed format coverage and limits. `supportedFileExtensions()` exposes the advertised extension list.

## Configuration and errors

```typescript
const ai = new DuckAI({
  timeout: 120_000, // Request timeout in milliseconds, after token bootstrap.
  headless: true,
  browserAutoInstall: false,
  browserExecutable: "/path/to/chromium",
  settings: { tokenGenerationTimeout: 30_000, officeConversionTimeout: 60_000 },
});
```

All JavaScript duration options use **milliseconds**. Environment duration variables retain the Python SDK convention of **seconds**: `UPSTREAM_READ_TIMEOUT`, `TOKEN_GENERATION_TIMEOUT`, `BROWSER_INSTALL_TIMEOUT`, `OFFICE_CONVERSION_TIMEOUT`. Both SDKs use `DUCKAI_AUTO_TOKEN`, `DUCKAI_BROWSER_HEADLESS`, `DUCKAI_BROWSER_AUTO_INSTALL`, `DUCKAI_BROWSER_EXECUTABLE`, `DUCKAI_BROWSER_CHANNEL`, `DUCKAI_BROWSER_CDP_URL` and `DUCKAI_HEADERS_JSON`. JavaScript reads the process environment; it does not automatically load `.env` (use your runtime's env-file loader).

Playwright is held to the tested 1.63 release line; update it together with anonymous-session integration checks. Default session generation starts full Chromium in an isolated temporary profile, headless, and refreshes the challenge for each request. Client creation and catalogue/file inspection do not launch it. A configured `browserCDPUrl` borrows a running browser, closing only the SDK's page and connection on cleanup. Always close the client.

`headers` accepts only the supported Duck.ai identity/challenge headers. Providing `x-vqd-hash-1` or `x-vqd-4` bypasses automatic token generation. A custom `tokenProvider` implements asynchronous `headers(signal?)` and `close()`; a custom `fetch` can adapt transport requirements.

```typescript
import { DuckAIError, ChallengeError, RateLimitError, IncompleteResponseError } from "@rendertk/duckai-sdk";

try {
  await ai.chat("Hello");
} catch (error) {
  if (error instanceof RateLimitError) console.log(error.retryAfter);
  if (error instanceof ChallengeError) console.log("Duck.ai rejected the session");
  if (error instanceof IncompleteResponseError) console.log(error.response.text);
  if (error instanceof DuckAIError) console.log(error.statusCode);
}
```

Caller cancellation propagates the signal's reason. Upstream challenge rejection, rate limits, transport errors and incomplete streams are explicit; requests are not automatically retried. Duck.ai service changes may require SDK updates.
