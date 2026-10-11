# SDK behavior and verification

The [language guides](../README.md) document installation and every public workflow. All SDKs call Duck.ai directly and share `protocol/models.json`. Native packages carry their own generated resources and work when installed outside this checkout.

## Common behavior

| Capability | Contract |
| --- | --- |
| Sessions | Fresh anonymous browser challenge; no account or session relay required for free models |
| Models | Same IDs, metadata, reasoning defaults and attachment profiles |
| Chat | Collected text, raw events, completion state and reusable assistant message |
| Streaming | Incremental UTF-8 text; SSE parsed correctly across arbitrary byte boundaries |
| History | Unknown message fields and opaque assistant parts retained; turns serialize and commit only after `[DONE]` |
| Cancellation | Cancels bootstrap/request and releases owned resources; failed turns preserve committed history |
| Errors | Entitlement/challenge/rate-limit errors surfaced; partial replies retained; no automatic replay of chat turns |
| Files | Local document text extraction and model-dependent native image/PDF parts; limits include retained history |
| Ownership | Closing a client closes its provider and streams; an injected C# HttpClient remains caller-owned |

Python, JavaScript and C# use isolated Chromium. Swift uses nonpersistent WebKit. Flutter uses incognito WKWebView/Android System WebView. Mobile chat streams in the same browser context that solved its challenge. The challenge expression runs inside the site-created iframe; hashing and metadata normalization happen in its parent page, matching the frontend's WebKit-compatible flow.

Per-turn scalar options override conversation defaults; conversation history always comes from the conversation. The native APIs use optional scalar fields to distinguish omitted values from explicit false. A supplied native options object uses its own file list, avoiding accidental repeat uploads. Additional wire options merge by key.

## Format differences

Native packages include modern Office/OpenDocument/Visio exports, text/web exports, images and PDFs. Python/JavaScript additionally include legacy binary readers and optional LibreOffice conversion. Those desktop readers are not present in the new native packages. Use a modern content export for portable behavior, and inspect the language-specific supported-extension catalogue. The [format guide](file-formats.md) describes export options and extraction limits.

## Automated checks

`protocol/fixtures/chat.json` verifies identical request defaults, Unicode streaming and opaque reasoning parts. `documents.json` verifies exported Word, Excel, PowerPoint, OpenDocument and text/web extraction. `images.json` verifies JPEG EXIF orientation and PNG normalization. `pdfs.json` validates a native one-page PDF; mobile examples exercise their real image/PDF APIs before live chats. Native suites also cover failed-turn rollback, rate limits, collection limits, response-header timeout, conversation default overrides and XML complexity limits.

From the checkout:

```sh
python3 scripts/sync_protocol.py --check
python3 -m pytest
npm run build && npm run check && npm test
dotnet test sdks/csharp/DuckAI.Tests
swift test --package-path sdks/swift
cd sdks/dart
flutter analyze
flutter test
flutter pub publish --dry-run
```

CI builds/tests C# on Windows, Linux and macOS, Swift packages and iOS/macOS apps on macOS, and Flutter iOS/Android native examples. Live checks are separate from deterministic CI because Duck.ai availability, entitlement and quota are external.

## Live checks

- C#: `dotnet run --project sdks/csharp/Examples`.
- Swift/macOS: `swift run --package-path sdks/swift/Examples/CLI`.
- Swift/iOS: run the example with launch argument `--live-check`.
- Flutter/iOS/Android: run the example with `--dart-define=DUCKAI_LIVE_TEST=true`.

Mobile checks first prepare bundled image/PDF fixtures through native APIs. Each sends two turns and verifies completed replies with four retained history messages. Live checks consume quota. App examples display failures explicitly and provide Send/Cancel controls for manual lifecycle checks. Keep the app active; system alerts and background suspension can interrupt mobile work.

Development verification on 2026-10-11 passed live two-turn sessions for C# and Swift on macOS, Swift and Flutter on iOS 18.6 and 26.5 simulators, and Flutter on Android 16. Android bootstrap cancellation was also verified through the example UI. Clean NuGet, Swift 6 and Flutter consumers verified packaged resources outside this checkout. iOS/macOS and Android examples built successfully; C# packed successfully and Flutter's publication dry run had no warnings.

The declared minimum OS targets are iOS 15, macOS 12 and Android API 24. Live simulator checks above are evidence for the tested environments; they do not replace physical-device and deployment-specific testing. Windows/Linux CI coverage is configured separately. Duck.ai's upstream challenge and availability can change, so preserve explicit failure handling in applications.
