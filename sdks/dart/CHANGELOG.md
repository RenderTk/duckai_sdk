# Changelog

## 0.2.0

- Pin `flutter_inappwebview` 6.2.0-beta.3 for iOS Swift Package Manager support and bundled privacy resources; the WebView dependency is an upstream prerelease.
- Migrate the iOS example and CI to Swift Package Manager without CocoaPods.
- Lock the example's Swift Collections dependency to 1.2.1, avoiding an unnecessary Swift 6.4 compiler requirement.
- Use the SDK's default Luna model in the Flutter example.

- Serialize complete turns per native WebView with cancellation-aware FIFO queues.
- Await transport/WebView shutdown and cancel pending attachment preparation on close.
- Apply the turn timeout to queueing, preparation, bootstrap and streaming.
- Run image/document processing and attachment validation in bounded cancellable worker isolates.
- Bound native stream buffering and fail explicitly on overflow.
- Add optional raw-event retention and outgoing history limits; buffer accumulated text and avoid JSON round-trip copies.
- Enforce XML complexity budgets before tree allocation and preserve status 413 errors.
- Add regression tests for cancellation isolation, deadlines, disposal, workers and memory controls.

## 0.1.0

- Initial independent Flutter mobile SDK for iOS and Android.
- Native anonymous WebView sessions, streaming, cancellation and transactional conversations.
- Shared model catalogue and protocol/document fixtures.
- Local modern document readers and native image/PDF attachment preparation.
- Mobile setup and lifecycle guide with a runnable example.
