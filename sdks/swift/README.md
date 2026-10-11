# Duck.ai for Swift and SwiftUI

A native Swift package for **iOS 15+ and macOS 12+**, with async chat, streaming, conversations, model metadata and file attachments. It does not call Python, Node.js or an external session server. Swift 5.9+ is required. SwiftUI and UIKit/AppKit apps can use the same library.

## Install

From this checkout, choose **File → Add Package Dependencies → Add Local** in Xcode and select `sdks/swift`. Add the **DuckAI** library to your app target. This is currently a source package; there is no published standalone Swift package release.

A Swift package consumer can use:

```swift
// Package.swift
// Add to dependencies:
.package(path: "/absolute/path/to/duckai_sdk/sdks/swift")
// Add to your target's dependencies:
.product(name: "DuckAI", package: "swift")
```

The package includes its model catalogue and browser scripts. ZIPFoundation is resolved by Swift Package Manager. ImageIO, PDFKit and WebKit are supplied by the operating system. A macOS sandboxed app must enable **App Sandbox → Outgoing Connections (Client)**, equivalent to `com.apple.security.network.client`. All traffic uses HTTPS; no App Transport Security exception is needed.

## First request

Create and use the client on the main actor. Async operations suspend while WebKit and the network work; they do not synchronously wait on the UI thread.

```swift
import DuckAI

@MainActor func ask() async throws -> String {
    let ai = try DuckAI(model: Model.mistralSmall4)
    do {
        let answer = try await ai.chat("Explain recursion with a short example.")
        await ai.close()
        return answer.text
    } catch {
        await ai.close()
        throw error
    }
}
```

The default model is `Model.gpt6Luna`. The first request creates an isolated, nonpersistent native WebKit view and obtains a fresh anonymous challenge. Default chat requests stay in that browser context so session cookies and request identity are consistent. No login window or Chromium download is needed. The client owns that view and its streams; always call `await ai.close()` when finished. A closed client cannot be reused; create a new client.

Duck.ai controls access, availability and rate limits. Anonymous sessions do not unlock subscriber models. The SDK surfaces failures and does not silently retry chat turns.

## Streaming and cancellation

```swift
let stream = try ai.stream("Write a short explanation.")
defer { stream.close() }
for try await delta in stream {
    // Append to @State on the main actor.
    print(delta, terminator: "")
}
let response = stream.response
print(response.done) // true only after the upstream [DONE] marker
```

A stream is for one consumer. Use `defer { stream.close() }` if you may leave the loop early. Closing the stream aborts its request. Cancelling the task also cancels waiting and reading operations. Keep the `Task` handle for a Cancel button and call `task.cancel()`.

A complete response exposes `text`, `model`, `events`, `done` and `assistantMessage`. The last property preserves opaque upstream parts, including encrypted reasoning, for reuse in history. These fields are retained, not decrypted or interpreted as tools.

For raw events:

```swift
let events = try ai.events("Hello")
defer { events.close() }
for try await event in events {
    if let text = event["message"]?.string { print(text) }
}
```

`[DONE]` marks completion and is not returned as a JSON event. An unexpected end raises `IncompleteResponseError`; its `response` retains partial text and events.

## Conversations

```swift
let chat = try ai.conversation()
let first = try await chat.chat("Explain recursion.")
let second = try await chat.chat("Make that simpler.")
print(chat.messages.count) // 4 after two complete turns
```

`chat.stream(...)` returns a conversation stream with a `response` once reading starts. Close it in `defer` just like a standalone stream. Turns on one conversation serialize. History is committed only after a complete reply. Cancelled, incomplete and failed turns leave the previous history intact. `messages` is a value snapshot; mutating a copy cannot change the conversation. `try chat.clear()` clears history and rejects calls during an active turn.

Pass defaults through `ai.conversation(options:)`. A per-turn `ChatOptions` overrides supplied model, reasoning, tools, greeting and extra fields while retaining other defaults. A supplied options object uses its own file list; history always comes from the conversation.

## Models and request options

```swift
for model in ai.listModels(includeSubscriber: false) {
    print(model.id, model.name, model.reasoningEfforts)
}
var options = ChatOptions()
options.model = Model.gptOss120b
options.reasoningEffort = "low"
options.canUseTools = true
options.extra["canUseApproxLocation"] = .bool(false)
let answer = try await ai.chat("Compare these ideas.", options: options)
```

Model metadata includes provider, description, access tier, reasoning choices, image/PDF support, tools, beta status and native attachment profile. The catalogue is a packaged snapshot, not a live entitlement check. Custom nonempty model IDs are accepted. Unknown models default to reasoning `none` and tools enabled; known models use the first catalogue reasoning choice and their tools capability. Invalid reasoning choices for known models fail locally.

`ChatOptions` also accepts `messages`, `files`, `canShowGreeting` and `extra`. Use `extra` for other wire options such as `canDelegateImageGeneration` and `durableStream`. Reserved model/messages/reasoning/tools/greeting fields are set by the typed options.

```swift
var options = ChatOptions()
options.messages = [Message(role: "user", content: "Hello"), savedResponse.assistantMessage]
let answer = try await ai.chat("Continue", options: options)
```

`Message(fields:)` and `JSONValue` preserve unknown JSON fields. `messages` can be used without a separate prompt by calling `ai.chat(nil, options: options)`.

## Files

```swift
let file = try Attachment.fromURL(pickerURL)
var options = ChatOptions()
options.files = [file]
let answer = try await ai.chat("Summarize this file.", options: options)
```

`Attachment.fromURL` reads within a security-scoped access grant and bounds the read before allocating the whole file. Pass an actual exported file URL from your document picker. For bytes:

```swift
let file = Attachment(data: bytes, filename: "report.docx")
let prepared = try ai.prepareFiles([file]) // local processing; no session/network
let limits = ai.attachmentLimits()
```

Files attach to the final user message. Preparation produces portable JSON content parts you can retain in history without keeping the source file. Office files become labelled text and work with text-only models. PDFs and images use the native Duck.ai attachment protocol. Images are oriented, resized to the model's maximum dimension without enlarging and encoded as PNG; supported input types are PNG, JPEG, WebP and GIF, using the first frame.

Included exported formats: DOCX/DOCM/DOTX/DOTM, XLSX/XLSM/XLTX/XLTM/XLAM, PPTX/PPTM/PPSX/PPSM/POTX/POTM, ODT/OTT/ODS/OTS/ODP/OTP/ODG/OTG and their flat XML variants, modern Visio XML/ZIP formats, SVG, HTML/XHTML, text/Markdown, CSV/TSV/DIF/SLK, JSON/XML, calendar/contact exports and HTML ZIP/EPUB exports. Consult `attachmentLimits().supportedFileExtensions` for the package's format catalogue.

Mobile does not run LibreOffice. Export legacy binary Office documents, Apple iWork files, password-protected files and proprietary databases to a supported modern format or PDF before attaching them. Google Drive shortcut files and cloud URLs require a real content export. See the repository's [file format guide](../../docs/file-formats.md) for export options and desktop-only reader differences.

Limits are validated across retained conversation history:

- Document source: 20 MiB; prompt plus labelled document text: at most 16,000 UTF-16 code units.
- Image source: 10 MiB and 20 million pixels; model-specific image counts and dimensions.
- Messages containing images: at most 4,500 UTF-16 code units of text.
- PDFs: 1–15 pages, up to three in a conversation, 5 MiB combined across the conversation, unencrypted.
- ZIP exports: at most 2,000 members and 64 MiB uncompressed.

Content exceeding limits fails locally rather than being truncated. Macros and formulas are not executed; spreadsheet formula source is retained. Text extraction does not provide OCR, layout, embedded images or spreadsheet recalculation.

## SwiftUI lifecycle

The [runnable example](Examples/App/DuckAIExampleApp.swift) demonstrates Send, Cancel, `.task`, screen disappearance and background cancellation. Open [DuckAIExample.xcodeproj](Examples/DuckAIExample.xcodeproj) and select `DuckAIExample` for iOS or `DuckAIMacExample` for macOS. Its project can be regenerated with `xcodegen generate --spec sdks/swift/Examples/project.yml`.

Start requests while the app is active. Do not rely on an invisible WebView to continue work while iOS suspends the app. Cancel an in-flight task when the screen disappears or the scene enters the background; create/resume requests explicitly after returning. Failed or cancelled conversation turns do not pollute history. SwiftUI previews should use injected fixture transport rather than contacting Duck.ai.

## Settings and errors

```swift
var settings = Settings()
settings.timeout = 90
settings.tokenGenerationTimeout = 45
settings.maxDocumentTextCharacters = 8000
let ai = try DuckAI(settings: settings)
```

Timeouts are seconds. Defaults are 120 seconds for chat and 30 seconds for session generation; collection defaults to 8 MiB. Positive byte/pixel limits can be configured. The document text limit can be lowered but cannot exceed 16,000. The package does not read desktop environment variables on mobile; configuration is explicit.

Errors expose `statusCode`, `detail`, `headers` and `retryAfter`. Handle `RateLimitError` (429), `ChallengeError` (401/403/418), `AttachmentError`, `IncompleteResponseError` and `CancellationError` separately. Preserve partial output when useful and offer an explicit retry. `retryAfter` is the upstream header string and may be seconds or an HTTP date. Other upstream and protocol failures use `DuckAIError`. A non-SSE response and missing completion marker are failures, even if HTTP status was successful.

Advanced integrations can supply `TokenProvider`, `DuckAITransport`, or captured `settings.headers` with `autoToken = false`. Captured headers expire and may be tied to their browser; they are not a persistent mobile credential. The captured-header path uses native HTTP unless you explicitly provide another transport. Injected transports must honor cancellation and response bounds. The client closes its token provider and transport.

## Development

```sh
swift test --package-path sdks/swift
swift run --package-path sdks/swift/Examples/CLI
```

The first command uses deterministic fixtures; the second performs a real two-turn macOS session. Run the example with the launch argument `--live-check` for native image/PDF preparation and a two-turn simulator check. Live tests require network access and consume Duck.ai quota. Model/resource synchronization is checked with `python scripts/sync_protocol.py --check` from the repository root.
