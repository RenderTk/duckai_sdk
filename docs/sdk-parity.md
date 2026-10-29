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
### Flutter SPM verification (2026-10-28)

Dart SDK 0.2.0 pins `flutter_inappwebview` 6.2.0-beta.3, including its upstream
iOS Swift package and privacy resources. The Flutter example no longer contains
a Podfile, Pod lockfile, Pods build phases or Pods xcconfig/workspace references.
The example pins Swift Collections 1.2.1 (Swift 5.10+) rather than the
Swift 6.4-only release initially selected by the resolver. Its iOS Simulator SPM
build and Android debug build passed; Flutter analysis and all 17 Dart tests
passed. CI explicitly enables SPM and builds the iOS example without
running CocoaPods. A downstream Flutter consumer (Blazely) also built on both
platforms. Real Luna requests completed through Blazely's JSON-validating router
on iOS 26.5 and Android 16 simulators, including a real food JPEG and generated
nutrition label. Both platforms passed native cancellation followed by a
successful recovery request. Cold Android attempts sometimes exceeded the
application's six-second budget, and an upstream bootstrap returned HTTP 503;
application fallback remains necessary. On iOS, an invalid live workout reply
was rejected and recovered through Blazely's authenticated production API.

### Flutter production hardening (2026-10-28)

Dart SDK 0.2.1 makes its headless browser document inert and excludes its DOM
from assistive navigation. The native plugin keeps its WKWebView behind Flutter
for reliable script execution; its page controls previously appeared in the
app's accessibility tree. A live iOS accessibility snapshot after this change
contained no interactive Duck page controls, and Luna requests still completed.
The SDK's 17 Dart tests passed. A downstream consumer passed a 16-case mixed
Duck/API matrix on iOS and 15/16 primary-only cases in Android release mode; the
remaining cold request exceeded the consumer's deadline. Native cancellation
and recovery passed on both platforms. The consumer also compiled iOS device
Release through SPM and a signed Android release bundle. These are simulator
and build results, not a physical-device or sustained-availability certification.
