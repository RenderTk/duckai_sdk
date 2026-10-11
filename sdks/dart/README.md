# Duck.ai for Dart and Flutter

A native SDK for **Flutter on iOS 15+ and Android API 24+**. It supports anonymous sessions, async chat, streaming, conversations, model metadata and attachments. Dart 3.8+ and Flutter 3.32+ are required. This package targets Flutter mobile apps; command-line Dart, web and desktop are outside its supported runtime set.

Session generation uses the device's native WKWebView (iOS) or Android System WebView. No Chromium bundle, Python process, login screen or external session server is required. Default chat streaming stays in that native browser context to preserve the session's cookies and request identity.

## Install

The package is currently distributed from the repository and is not published on pub.dev. For a local checkout:

```yaml
# Your Flutter app's pubspec.yaml
dependencies:
  flutter:
    sdk: flutter
  duckai_sdk:
    path: /absolute/path/to/duckai_sdk/sdks/dart
```

A Git dependency can select the package subdirectory:

```yaml
dependencies:
  duckai_sdk:
    git:
      url: https://github.com/RenderTk/duckai_sdk.git
      path: sdks/dart
      ref: YOUR_COMMIT_OR_RELEASE_TAG
```

Use a commit containing this package; pin a reviewed commit or release for reproducible builds. Then run `flutter pub get`. The package includes its own model JSON and browser scripts as Flutter assets; do not copy them into your app manually.

### iOS setup

Set your Runner target and Podfile to iOS 15 or newer. If native pods specify an older deployment version, set their target to 15 in your existing Podfile post-install block:

```ruby
platform :ios, '15.0'
post_install do |installer|
  installer.pods_project.targets.each do |target|
    flutter_additional_ios_build_settings(target)
    target.build_configurations.each do |config|
      config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '15.0'
    end
  end
end
```

Keep the Flutter-generated settings and other post-install customizations. The WebView dependency currently uses CocoaPods on iOS. No App Transport Security exception is needed because the SDK uses HTTPS. Add any document-picker permissions required by your own picker package; the SDK does not request photo or file access itself.

### Android setup

Add the internet permission to the main manifest so release builds can connect:

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

Set `minSdk` to 24 or higher. Keep Android System WebView updated; Duck.ai's challenge executes browser JavaScript and an obsolete WebView may fail regardless of Android version. The runnable example pins a compatible Android Gradle Plugin 8.13.2, Gradle 8.14.3 and Kotlin 2.2.20. The stable WebView dependency used here is incompatible with AGP 9's removal of the old default ProGuard configuration; use the example's AGP 8 setup with this dependency set. JDK 17 is the supported Android build baseline. No cleartext networking or browser activity intent is required.

## First request

Initialize Flutter before constructing clients. Run requests in the app's foreground, on the main Flutter isolate:

```dart
import 'package:flutter/widgets.dart';
import 'package:duckai_sdk/duckai_sdk.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const YourApp());
}

Future<String> ask() async {
  final ai = DuckAI(model: Model.gpt6Luna);
  try {
    final answer = await ai.chat('Explain recursion with a short example.');
    return answer.text;
  } finally {
    await ai.close();
  }
}
```

The default model is `Model.gpt6Luna`. The first request starts an isolated, nonpersistent headless native WebView and obtains a fresh anonymous challenge. The client owns the WebView and its active streams; always `await ai.close()` when finished. Closing is idempotent. Create a new client after closing one.

Duck.ai controls availability, rate limits and model entitlement. Anonymous sessions do not unlock subscriber models. Upstream failures are surfaced; the package does not fabricate responses or silently retry chat turns.

## Streaming and cancellation

```dart
final cancellation = CancellationToken();
final stream = ai.stream('Write a short explanation.', options: ChatOptions(
  cancellationToken: cancellation,
));
try {
  await for (final delta in stream) {
    // Append to state; avoid calling setState after dispose.
    print(delta);
  }
  print(stream.response.done);
} finally {
  await stream.close();
}
```

Call `cancellation.cancel()` from your Cancel button. Cancelling the stream subscription or leaving `await for` early aborts the request. `stream.close()` also cancels it. A stream accepts one consumer. Keep the stream handle when you need its collected response or want to close it before listening.

`ChatResponse` exposes `text`, `model`, `done`, `events` and `assistantMessage`. The latter retains unknown assistant parts such as opaque reasoning state; do not replace it with only `text` when reconstructing history. Completion is true only after upstream `[DONE]`.

```dart
await for (final event in ai.events('Hello')) {
  if (event is Map && event['message'] is String) print(event['message']);
}
// To retain the response while reading raw events:
final stream = ai.stream('Hello');
await for (final event in stream.events) { print(event); }
print(stream.response.text);
```

Raw events omit the completion marker. Missing completion raises `IncompleteResponseException`, whose `response` preserves partial text and events.

## Conversations

```dart
final chat = ai.conversation();
print((await chat.chat('Explain recursion.')).text);
print((await chat.chat('Now make it simpler.')).text);
print(chat.messages.length); // 4

await for (final delta in chat.stream('Give me an example.')) {
  print(delta);
}
```

Turns on one conversation serialize. History is committed only after a complete reply; failed, cancelled and incomplete turns preserve the previous history. `messages` returns a deep copy. `clear()` rejects calls during an active turn. Defaults can be passed to `ai.conversation(options: ...)`; per-turn `ChatOptions` override supplied model, reasoning, tools, greeting, extra and cancellation fields while retaining other defaults. A supplied options object uses its own file list; history always comes from the conversation itself.

For manual history:

```dart
final first = await ai.chat('Hello');
final next = await ai.chat('Continue', options: ChatOptions(messages: [
  {'role': 'user', 'content': 'Hello'},
  first.assistantMessage,
]));
```

Messages and content parts are JSON maps so unknown fields survive. Pass `null` as the prompt when the `messages` list already contains the complete request.

## Models and options

```dart
for (final model in await ai.listModels(includeSubscriber: false)) {
  print('${model.id}: ${model.name}');
}
final answer = await ai.chat('Compare these ideas.', options: const ChatOptions(
  model: Model.gptOss120b,
  reasoningEffort: 'low',
  canUseTools: true,
  extra: {'canUseApproxLocation': false},
));
```

Model metadata includes provider, description, access tier, reasoning choices, image/PDF support, tools, beta status and native attachment profile. The packaged catalogue is a snapshot, not a live entitlement check. Custom nonempty model IDs are accepted. Known models default to their first reasoning choice and tools capability; unknown IDs default to `none` reasoning with tools enabled. Unsupported reasoning choices for known models fail locally.

`ChatOptions` also accepts `messages`, `files`, `canShowGreeting`, `extra` and `cancellationToken`. Additional wire fields, such as `canDelegateImageGeneration` and `durableStream`, go in `extra`. The typed model/messages/reasoning/tools/greeting fields take precedence.

## Attachments and pickers

Use your app's picker to obtain file bytes or a readable local path. The SDK does not obtain Android content URI grants or iOS picker permissions. If your picker returns an Android `content://` URI or a security-scoped object, read it through that picker's API and use bytes:

```dart
final attachment = Attachment(bytes, 'report.docx');
final answer = await ai.chat('Summarize this report.', options: ChatOptions(
  files: [attachment],
));
```

For a real accessible file path:

```dart
final file = await Attachment.fromPath('/app/documents/report.docx');
final parts = await ai.prepareFiles([file]); // local; no session/network
final limits = await ai.attachmentLimits();
```

Reads are bounded before loading the entire file. Preparation creates JSON content parts that can be retained in history without retaining the source file. Files attach to the last user message. Office exports become labelled text and work with text-only models. PDFs and images use Duck.ai's native attachment protocol and require a compatible model.

Images accept PNG, JPEG, WebP and GIF, use the first frame, apply orientation, resize without enlargement and encode as PNG. PDF validation uses the operating system's PDF reader through the bundled dependency; run PDF preparation after Flutter initialization on the main isolate.

Included exported formats cover modern Word, Excel, PowerPoint, OpenDocument, flat OpenDocument XML, modern Visio, SVG, HTML/XHTML, plain text/Markdown, CSV/TSV/DIF/SLK, JSON/XML, calendar/contact and web/archive exports. `supportedFileExtensions()` returns the complete package catalogue. Mobile does not run LibreOffice. Legacy binary Office documents, XLS/XLSB, MSG/OFT, native Apple/Google files and proprietary databases require a supported export or PDF. Cloud document links are not fetched. See the [shared format guide](../../docs/file-formats.md) for export choices and desktop-only reader differences.

Limits apply to retained conversation history:

- Document sources: 20 MiB each; prompt plus all labelled document text: at most 16,000 UTF-16 code units.
- Image sources: 10 MiB and 20 million pixels; model-specific dimensions/counts. Image messages allow at most 4,500 text characters.
- PDFs: 1–15 pages, at most three in a conversation, 5 MiB combined, unencrypted.
- Archives: at most 2,000 members and 64 MiB uncompressed.

Oversized content is rejected, not truncated. Macros and formulas are not executed. Text extraction omits layout, embedded pictures, OCR and formula recalculation; spreadsheet formula source is retained.

## Mobile lifecycle

Use `WidgetsBindingObserver` or `AppLifecycleListener` to cancel an in-flight token/stream when your screen is disposed or the app is paused. Start a new request explicitly when foregrounded. Android/iOS can suspend or terminate a background WebView; this SDK does not turn chat into a background service.

```dart
@override
void dispose() {
  cancellation.cancel();
  // dispose is synchronous; arrange async client cleanup from your owner.
  unawaited(ai.close());
  super.dispose();
}
```

Import `dart:async` for `unawaited`. Check `mounted` before UI updates. Keep a client at the screen/service scope appropriate to your app; do not create clients during every widget `build()`. Do not call platform WebView/PDF APIs from a background isolate. Text/image preparation can be moved to a worker by your app if needed, but browser operations remain on the main isolate.

The [runnable example](example/lib/main.dart) includes Send, Cancel, disposal, foreground/background handling and a two-turn live check:

```sh
cd sdks/dart/example
flutter pub get
flutter run -d YOUR_DEVICE
# Automatically run an anonymous session and follow-up:
flutter run -d YOUR_DEVICE --dart-define=DUCKAI_LIVE_TEST=true
```

## Settings and errors

```dart
final ai = DuckAI(settings: Settings(
  timeout: const Duration(seconds: 90),
  tokenGenerationTimeout: const Duration(seconds: 45),
  maxDocumentTextCharacters: 8000,
));
```

Defaults: chat 120 seconds, session generation 30 seconds, collection 8 MiB. Positive upload/collection/pixel limits are configurable; document text limits can be lowered but cannot exceed 16,000. The package uses explicit settings, not desktop environment variables.

`DuckAIException` exposes `statusCode`, `detail`, `headers` and `retryAfter`. Specific errors are `RateLimitException` (429), `ChallengeException` (401/403/418), `AttachmentException` and `IncompleteResponseException`. Explicit cancellation raises `CancelledException`; invalid arguments use `ArgumentError` and closed objects use `StateError`. Preserve partial output when appropriate and offer explicit retry. `retryAfter` is the original string and may contain seconds or an HTTP date.

Advanced integrations can implement `TokenProvider` and `DuckAITransport`, or supply fresh captured `Settings.headers` with `autoToken: false`. Captured challenges expire and may be tied to a browser; they are not permanent mobile credentials. Custom/captured session configurations use native HTTP unless a transport is explicitly provided. Injected implementations must honor cancellation and bounds. Closing the client closes its provider and transport.

## Development and packaging

```sh
cd sdks/dart
flutter pub get
flutter analyze
flutter test
dart pub publish --dry-run
```

Unit tests use deterministic request, bytewise Unicode SSE and document fixtures. Live checks are separate, require Duck.ai access and consume quota. Run `python scripts/sync_protocol.py --check` from the repository root to check packaged model/scripts. Dependency updates must be validated with both an iOS simulator/device build and an Android build; a Dart analyzer success alone does not validate native plugins.
