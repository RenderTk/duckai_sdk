# Duck.ai SDKs

Native SDKs for calling Duck.ai directly, with silent anonymous sessions, streaming, conversations, model selection and file attachments.

| SDK | Supported environments | Documentation |
| --- | --- | --- |
| Python | Python 3.11+ | [Python SDK](sdks/python/README.md) |
| TypeScript / JavaScript | Node.js 22.13+, Bun 1.4+, Deno 2.9+; ESM and CommonJS | [TypeScript / JavaScript SDK](sdks/typescript/README.md) |
| C# | .NET 8+; Windows, macOS, Linux desktop | [C# SDK](sdks/csharp/README.md) |
| Swift / SwiftUI | Swift 5.9+; iOS 15+, macOS 12+ | [Swift SDK](sdks/swift/README.md) |
| Dart / Flutter | Dart 3.8+, Flutter 3.32+; iOS 15+, Android API 24+ | [Dart SDK](sdks/dart/README.md) |

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

## C#, Swift and Dart

Each SDK is an independent native package. The new packages are source-distributed from this repository; updating GitHub does not publish them to NuGet or pub.dev. See the guides for installation, complete API examples, platform setup, cancellation and lifecycle handling:

- [C# desktop guide](sdks/csharp/README.md) and [console example](sdks/csharp/Examples/Program.cs).
- [Swift/iOS/macOS guide](sdks/swift/README.md) and [SwiftUI example](sdks/swift/Examples/App/DuckAIExampleApp.swift).
- [Dart/iOS/Android guide](sdks/dart/README.md) and [Flutter example](sdks/dart/example/lib/main.dart).

Swift and Flutter use an isolated native WebView for automatic sessions, with chat streaming in that same browser context. No mobile Chromium install or session server is needed. Run mobile requests in the foreground and cancel them when the owning screen/app is suspended. The SwiftUI and Flutter examples demonstrate this lifecycle.

## Sessions, models and files

Python, JavaScript and C# first requests start isolated headless Chromium to obtain fresh Duck.ai challenge headers. It does not open a browser window. Chromium is downloaded automatically if missing; use the Chromium installation commands in the language-specific SDK guides for predictable deployment. Always close the client to release its browser and streams.

All SDKs expose the same versioned [model catalogue](protocol/models.json), including free and subscriber model metadata. Subscriber models require Duck.ai account entitlement; automatic anonymous sessions do not grant premium access. Custom model IDs remain usable when the upstream catalogue changes.

PDFs and images use native model attachments where supported. Office and Google suite **exported files** are extracted locally into labelled text. Legacy conversion can use an optional installed LibreOffice. The new native packages support modern exported documents; legacy binary readers and optional LibreOffice conversion remain specific to Python/JavaScript. See [file formats and limitations](docs/file-formats.md) and each package's supported-format catalogue. Cloud document URLs and Drive authentication are outside the SDK's scope.

Duck.ai remains the upstream service. Its challenge format, model access, rate limits and endpoints can change. Errors are surfaced with their upstream status; the SDK does not silently retry or provide offline responses.

See [cross-SDK behavior and verification](docs/sdk-parity.md) for the shared contract, regression commands, live checks and platform evidence.

## Repository layout

```text
sdks/
  python/          # Independent Python package, tests and examples
  typescript/      # Independent npm package, tests and examples
  csharp/          # .NET desktop package, tests and console example
  swift/           # Swift package, tests and iOS/macOS examples
  dart/            # Flutter mobile package, tests and iOS/Android example
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
dotnet test sdks/csharp/DuckAI.Tests/DuckAI.Tests.csproj
swift test --package-path sdks/swift
(cd sdks/dart && flutter analyze && flutter test)
```

Tests use deterministic transport fixtures, not an offline chat mode in the SDK. Live checks are separate and require Duck.ai network access. [Adding another language SDK](docs/adding-sdk.md) explains package boundaries and contract requirements.
