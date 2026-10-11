# Duck.ai for C#

A native **.NET 8+ desktop SDK for Windows, macOS and Linux**. It supports anonymous sessions, async chat, streaming, conversations, model metadata and attachments. It does not require Python or an installed Node.js runtime. Chromium is used only for anonymous-session generation.

## Install from the repository

The NuGet package is prepared for packaging and is not published to nuget.org. Add a source reference to your desktop, console, WPF, WinForms or Avalonia project:

```sh
dotnet add YourApp.csproj reference /path/to/duckai_sdk/sdks/csharp/DuckAI/DuckAI.csproj
dotnet build YourApp.csproj
```

Or pack and install from a local feed:

```sh
dotnet pack sdks/csharp/DuckAI/DuckAI.csproj -c Release -o output/nuget
dotnet add YourApp.csproj package RenderTk.DuckAI --version 0.1.0 --source /absolute/path/to/output/nuget
```

The SDK ships its own catalogue/browser script; it does not depend on the repository's `protocol` directory at runtime. Playwright, SkiaSharp and PdfPig are restored by NuGet. SkiaSharp native assets for Linux are included. Normal framework-dependent desktop publishing is supported; trimming and Native AOT have not been validated.

The first anonymous request installs Chromium if missing. Installation has a five-minute deadline and cancels with the caller/client, including its owned process tree; the session-generation deadline starts after installation. For predictable deployments, install it after building your consumer:

```sh
pwsh bin/Debug/net8.0/playwright.ps1 install chromium --no-shell
# Linux hosts may also need system libraries:
pwsh bin/Debug/net8.0/playwright.ps1 install --with-deps chromium --no-shell
```

Use the script in your actual build output directory. PowerShell is only needed for these optional installation commands. Configure `Settings.BrowserExecutable` to use an existing compatible Chromium executable. `BrowserAutoInstall = false` disables downloads and produces an actionable missing-browser error.

## First request

```csharp
using DuckAI;

await using var ai = new DuckAIClient(Model.GPT_6_LUNA);
var response = await ai.ChatAsync("Explain recursion with a short example.");
Console.WriteLine(response.Text);
```

`await using` releases owned browsers, requests and other resources. The first request opens isolated headless Chromium; it does not show a login window. Fresh challenge headers are generated for requests. The SDK never connects to or kills a user-owned browser. A disposed client cannot be reused.

Duck.ai controls availability, rate limits and account entitlement. Anonymous access does not unlock subscriber models. The SDK does not fabricate responses or silently retry chat turns.

## Streaming, events and cancellation

```csharp
using var cancellation = new CancellationTokenSource();
await using var stream = ai.Stream("Write a short explanation.");
await foreach (var delta in stream.WithCancellation(cancellation.Token))
    Console.Write(delta);
Console.WriteLine(stream.Response.Done);
```

Call `cancellation.Cancel()` from your Cancel button. Leaving `await foreach` early disposes its enumerator and aborts the request. `await stream.DisposeAsync()` also cancels it. Keep `await using` around the stream when it may never be consumed or when handing it between components. Each stream has one consumer.

`ChatResponse` exposes `Text`, `Model`, `Done`, `Events` and `AssistantMessage`. Completion is true only after `[DONE]`. Unknown assistant parts are preserved in `AssistantMessage`; retain that object in history for opaque reasoning state.

```csharp
await using var stream = ai.Events("Hello");
await foreach (var value in stream.EventsAsync(cancellation.Token))
    Console.WriteLine(value?.ToJsonString());
```

Raw events exclude the completion marker. A stream ending before completion raises `IncompleteResponseException` with its partial `Response`.

## Conversations and manual history

```csharp
var chat = ai.Conversation();
Console.WriteLine((await chat.ChatAsync("Explain recursion.")).Text);
Console.WriteLine((await chat.ChatAsync("Now make it simpler.")).Text);
Console.WriteLine(chat.Messages.Count); // 4

await foreach (var delta in chat.Stream("Give me an example."))
    Console.Write(delta);
```

Turns on one conversation serialize. History is committed only after a complete reply. Cancelled, incomplete and rejected turns preserve previous history. `Messages` returns a deep copy. `Clear()` rejects calls during a turn. Pass default options to `ai.Conversation(options)`; per-turn options override the supplied fields while retaining the conversation’s model, reasoning, tools, greeting and extra defaults. A supplied options object uses its own file list; history always comes from the conversation.

For manual history, use `JsonObject` messages, which retain unknown upstream fields:

```csharp
using System.Text.Json.Nodes;
var response = await ai.ChatAsync("Hello");
var options = new ChatOptions {
    Messages = [Message.Text("user", "Hello"), response.AssistantMessage]
};
var next = await ai.ChatAsync("Continue", options);
```

You can call `ChatAsync(null, options)` when `Messages` already contains the final user turn. `Message.Text(role, content)` is a convenience for plain text.

## Models and options

```csharp
foreach (var model in ai.ListModels(includeSubscriber: false))
    Console.WriteLine($"{model.Id}: {model.Name}");

var response = await ai.ChatAsync("Compare these ideas.", new ChatOptions {
    Model = Model.GPT_OSS_120B,
    ReasoningEffort = "low",
    CanUseTools = true,
    Extra = new JsonObject { ["canUseApproxLocation"] = false }
});
```

Metadata includes provider, description, access tier, reasoning choices, image/PDF support, tools, beta status and attachment profile. The packaged catalogue is a snapshot, not a live entitlement check. Custom nonempty model IDs are accepted. Known models default to their first reasoning choice and tools capability; unknown models default to reasoning `none` and tools enabled. Invalid reasoning choices for known models fail before network access.

`ChatOptions` includes `Model`, `Messages`, `Files`, `ReasoningEffort`, `CanUseTools`, `CanShowGreeting` and `Extra`. Additional wire options such as `durableStream` or `canDelegateImageGeneration` belong in `Extra`. Typed model/messages/reasoning/tools/greeting fields take precedence.

## Attachments

```csharp
var file = await Attachment.FromPathAsync("report.docx");
var response = await ai.ChatAsync("Summarize this report.", new ChatOptions {
    Files = [file]
});
// For bytes:
var image = new Attachment(bytes, "photo.png", "image/png");
```

The bounded file read defaults to 20 MiB. `PrepareFiles(files, model)` does local processing without contacting Duck.ai. Its JSON parts can be retained in history. Files attach to the last user message. Office exports become labelled text and work with text-only models. Images and PDFs require a model with native attachment capability.

Images accept PNG, JPEG, WebP and GIF, use the first frame, resize without enlargement and are encoded as PNG. PDFs must be readable, unencrypted and contain 1–15 pages. Modern Word, Excel, PowerPoint, OpenDocument, Visio, text, HTML, JSON/XML and exported web/archive formats are included; `AttachmentLimits().SupportedFileExtensions` provides the catalogue.

This package does not invoke LibreOffice or the Python/JavaScript readers. Legacy binary Office files, XLS/XLSB, MSG/OFT and proprietary databases need a supported export. Google/Apple native documents need an actual export; cloud document URLs are not fetched. See the [shared file format guide](../../docs/file-formats.md) for export instructions and reader differences.

Limits apply to the entire retained conversation:

- Documents: 20 MiB per file, 16,000 UTF-16 code units for prompt and all labelled document text.
- Images: 10 MiB, 20 million pixels; model-specific dimensions/counts, and 4,500 text characters in image messages.
- PDFs: at most three per conversation and 5 MiB combined across all retained PDFs, with 1–15 pages each.
- Archives: at most 2,000 members and 64 MiB uncompressed.

Over-limit content is rejected rather than truncated. Macros and formulas are not executed. Text extraction omits layout, embedded pictures and OCR. Spreadsheet formulas are represented as source without recalculation.

## Settings and error handling

```csharp
var settings = new Settings {
    Timeout = TimeSpan.FromSeconds(90),
    TokenGenerationTimeout = TimeSpan.FromSeconds(45),
    BrowserAutoInstall = false,
    BrowserExecutable = "/path/to/chromium",
    MaxDocumentTextCharacters = 8000
};
await using var ai = new DuckAIClient(settings: settings);
```

Defaults: chat 120 seconds, session generation 30 seconds, collection 8 MiB, headless enabled and browser auto-install enabled. Positive upload/pixel/collection limits are configurable; document text limits can be lowered but cannot exceed 16,000. Configuration is explicit; this package does not inherit Python/JavaScript environment variables.

`DuckAIException` exposes `StatusCode`, `Message`, `Headers` and `RetryAfter`. Specific types are `RateLimitException` (429), `ChallengeException` (401/403/418), `AttachmentException` and `IncompleteResponseException`. Caller cancellation raises `OperationCanceledException`. Preserve partial output and offer explicit retry. `RetryAfter` is an upstream string that may contain seconds or an HTTP date. Invalid local options use standard argument exceptions. Non-SSE success responses are protocol errors.

```csharp
try { var answer = await ai.ChatAsync("Hello", cancellationToken: token); }
catch (RateLimitException e) { Console.WriteLine($"Try again after: {e.RetryAfter}"); }
catch (IncompleteResponseException e) { Console.WriteLine(e.Response.Text); }
catch (OperationCanceledException) { Console.WriteLine("Cancelled"); }
```

For custom authentication, implement `ITokenProvider` or set fresh `Settings.Headers` and `AutoToken = false`. Captured headers expire; they are not permanent credentials. Headers are restricted to Duck.ai session headers, user agent and language. `DuckAIClient` disposes its token provider. A supplied `HttpClient` remains caller-owned; configure it to reject redirects and use an appropriate timeout. The default native HTTP client rejects redirects and the SDK controls request lifetime.

## Desktop UI and development

Use async event handlers and `await foreach`; do not call `.Result` or `.Wait()` on the UI thread. Marshal streamed UI updates through your framework's dispatcher when required. Cancel requests when a window closes and asynchronously dispose the client.

```sh
dotnet test sdks/csharp/DuckAI.Tests/DuckAI.Tests.csproj
dotnet run --project sdks/csharp/Examples/Examples.csproj
```

The tests use deterministic protocol/document fixtures. The example performs a real streaming turn and follow-up; it needs network access and consumes quota. Run `python scripts/sync_protocol.py --check` from the repository root to validate packaged catalogue and scripts.
