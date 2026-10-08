# Duck.ai SDKs

Native SDKs for calling Duck.ai directly, with silent anonymous sessions, streaming, conversations, model selection and file attachments.

| SDK | Supported environments | Documentation |
| --- | --- | --- |
| Python | Python 3.11+ | [Python SDK](sdks/python/README.md) |
| TypeScript / JavaScript | Node.js 22.13+, Bun 1.4+, Deno 2.9+; ESM and CommonJS | [TypeScript / JavaScript SDK](sdks/typescript/README.md) |

The JavaScript SDK runs natively in each runtime. Hono integration includes Server-Sent Events and cancellation when a client disconnects. Browser pages, Cloudflare Workers and other runtimes without subprocesses and native Node-compatible dependencies are not supported: automatic anonymous sessions need Chromium, and image processing uses Sharp.

## Python

Install the SDK from this repository:

```sh
python -m pip install 'git+https://github.com/RenderTk/duckai_sdk.git#subdirectory=sdks/python'
```

```python
from duckai import DuckAI, Model

with DuckAI(model=Model.GPT_6_LUNA) as ai:
    chat = ai.conversation()
    print(chat.chat("Explain recursion with a short example.").text)
    print(chat.chat("Now make the example simpler.").text)
```

Async APIs, attachments and configuration are documented in [sdks/python](sdks/python/README.md).

## TypeScript / JavaScript

The npm package is prepared for distribution but has not been published to npm. Build it from this checkout:

```sh
git clone https://github.com/RenderTk/duckai_sdk.git
cd duckai_sdk
npm ci
npm run build
node sdks/typescript/examples/chat.mjs
```

```typescript
import { DuckAI, Model } from "@rendertk/duckai-sdk";

const ai = new DuckAI({ model: Model.GPT_6_LUNA });
try {
  const chat = ai.conversation();
  for await (const delta of chat.stream("Explain recursion.")) {
    process.stdout.write(delta);
  }
} finally {
  await ai.close();
}
```

See the [JavaScript SDK guide](sdks/typescript/README.md) for installing a packed SDK into another project, Bun and Deno commands, and the [Hono server example](sdks/typescript/examples/hono-server.ts).

## Sessions, models and files

The first request starts isolated headless Chromium to obtain fresh Duck.ai challenge headers. It does not open a browser window. Chromium is downloaded automatically if missing; use the Chromium installation commands in the language-specific SDK guides for predictable deployment. Always close the client to release its browser and streams.

Both SDKs expose the same versioned [model catalogue](protocol/models.json), including free and subscriber model metadata. Subscriber models require Duck.ai account entitlement; automatic anonymous sessions do not grant premium access. Custom model IDs remain usable when the upstream catalogue changes.

PDFs and images use native model attachments where supported. Office and Google suite **exported files** are extracted locally into labelled text. Legacy conversion can use an optional installed LibreOffice. See [file formats and limitations](docs/file-formats.md). Cloud document URLs and Drive authentication are outside the SDK's scope.

Duck.ai remains the upstream service. Its challenge format, model access, rate limits and endpoints can change. Errors are surfaced with their upstream status; the SDK does not silently retry or provide offline responses.

## Repository layout

```text
sdks/
  python/          # Independent Python package, tests and examples
  typescript/      # Independent npm package, tests and examples
protocol/          # Shared model metadata and wire/attachment fixtures
scripts/           # Shared catalogue generation and consistency checks
docs/              # Cross-language behavior and contribution guides
example/           # Optional native Python desktop chat project
```

The desktop chat app is a built-in example project. SDK installation does not include or launch it, and JavaScript consumers do not need Python or Qt. To run it from a checkout:

```sh
python -m pip install -e './sdks/python[desktop]'
python -m example
```

See [example/README.md](example/README.md) for desktop features and setup.

## Development

```sh
python -m pip install -e './sdks/python[dev,desktop]'
npm ci
python scripts/sync_protocol.py --check
python -m pytest
ruff check .
npm run build
npm run check
npm test
npm run test:bun
npm run test:deno
```

Tests use deterministic transport fixtures, not an offline chat mode in the SDK. Live checks are separate and require Duck.ai network access. [Adding another language SDK](docs/adding-sdk.md) explains package boundaries and contract requirements.
