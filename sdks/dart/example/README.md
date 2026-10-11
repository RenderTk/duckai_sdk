# Duck.ai Flutter example

Run this iOS/Android app from a checkout:

```sh
cd sdks/dart/example
flutter pub get
flutter run -d YOUR_DEVICE
```

Initialize and keep the app in the foreground. The Send button streams a reply;
Cancel aborts it. Disposing the screen or backgrounding the app cancels the
request and closes the owned client. The app uses a text-only model to make
live checks inexpensive.

For a two-turn anonymous-session check:

```sh
flutter run -d YOUR_DEVICE --dart-define=DUCKAI_LIVE_TEST=true
```

The check first prepares a bundled JPEG and PDF through native plugins, then sends two chat turns. Success prints `DUCKAI LIVE CHECK PASSED` and shows four retained history
messages. A live failure is displayed and is not silently retried.

See [the complete SDK guide](../README.md) for native prerequisites, files,
models, customization and errors. Android uses AGP 8 and JDK 17; point Flutter
at JDK 17 if your Android Studio installation uses a newer incompatible JDK.
The iOS Podfile raises pod deployment targets to iOS 15.
