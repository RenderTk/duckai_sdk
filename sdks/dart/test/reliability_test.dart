import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:typed_data';
import 'package:duckai_sdk/duckai_sdk.dart';
import 'package:duckai_sdk/src/async_gate.dart';
import 'package:duckai_sdk/src/browser_request.dart';
import 'package:duckai_sdk/src/worker.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as image;

class FixtureTokens implements TokenProvider {
  int calls = 0, closes = 0;
  @override
  Future<Map<String, String>> headers({
    CancellationToken? cancellationToken,
  }) async {
    calls++;
    return {'x-vqd-hash-1': 'fixture'};
  }

  @override
  Future<void> close() async {
    closes++;
  }
}

class FixtureTransport implements DuckAITransport {
  int calls = 0;
  String text = 'data: [DONE]\n\n';
  @override
  FutureOr<void> close() {}
  @override
  Future<HTTPResponse> send(
    Uri url,
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken token,
  ) async {
    calls++;
    return HTTPResponse(200, {
      'content-type': 'text/event-stream',
    }, Stream.value(utf8.encode(text)));
  }
}

class DeferredCloseTransport extends FixtureTransport {
  final started = Completer<void>(), disposed = Completer<void>();
  int closes = 0;
  @override
  Future<void> close() {
    closes++;
    started.complete();
    return disposed.future;
  }
}

class PendingTokens extends FixtureTokens {
  final started = Completer<void>();
  final result = Completer<Map<String, String>>();
  @override
  Future<Map<String, String>> headers({CancellationToken? cancellationToken}) {
    started.complete();
    return result.future;
  }
}

// Exercises the actual per-WebView session lease without requiring a platform
// WebView or contacting Duck.ai. Headers would reset the first request if called
// while it is active, matching a failed/cancelled native bootstrap.
class NativeFixture extends AnonymousTokenProvider {
  final firstBody = StreamController<List<int>>();
  final firstCancelled = Completer<void>();
  int headersCalls = 0, sends = 0;
  bool active = false, resetActiveRequest = false;
  @override
  Future<Map<String, String>> headers({
    CancellationToken? cancellationToken,
  }) async {
    headersCalls++;
    if (active) resetActiveRequest = true;
    return {'x-vqd-hash-1': 'fixture'};
  }

  @override
  Future<HTTPResponse> send(
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken token,
    int maxBytes,
  ) async {
    sends++;
    if (sends > 1) {
      return HTTPResponse(200, {
        'content-type': 'text/event-stream',
      }, Stream.value(utf8.encode('data: [DONE]\n\n')));
    }
    active = true;
    void cancel() {
      active = false;
      if (!firstCancelled.isCompleted) firstCancelled.complete();
      firstBody.close();
    }

    firstBody.onCancel = cancel;
    firstBody.add(utf8.encode('data: {"message":"first"}\n\n'));
    return HTTPResponse(
      200,
      {'content-type': 'text/event-stream'},
      firstBody.stream,
      cancel: cancel,
    );
  }
}

class TrackedNativeTransport extends WebViewTransport {
  final secondQueued = Completer<void>();
  int leases = 0;
  TrackedNativeTransport(super.provider);
  @override
  Future<void Function()> acquireSession(CancellationToken token) {
    if (++leases == 2) secondQueued.complete();
    return super.acquireSession(token);
  }
}

String workerIdentity(dynamic _) => Isolate.current.debugName!;
String spinWorker(dynamic port) {
  (port as SendPort).send('started');
  while (true) {
    /* Must be killed on cancellation, rather than abandoned. */
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMessageHandler('flutter/assets', (message) async {
          final key = utf8.decode(message!.buffer.asUint8List());
          return ByteData.sublistView(
            await File(
              key.replaceFirst('packages/duckai_sdk/', ''),
            ).readAsBytes(),
          );
        });
  });

  test('cancelled FIFO waiter cannot release or block another owner', () async {
    final gate = AsyncGate();
    final first = await gate.acquire();
    final cancellation = CancellationToken();
    final second = gate.acquire(cancellation);
    final rejected = expectLater(second, throwsA(isA<CancelledException>()));
    var thirdEntered = false;
    final third = gate.acquire().then((release) {
      thirdEntered = true;
      return release;
    });
    cancellation.cancel();
    await rejected;
    expect(thirdEntered, false);
    first();
    final release = await third;
    first(); // Releasing twice must not unlock the next owner.
    var fourthEntered = false;
    final fourth = gate.acquire().then((lease) {
      fourthEntered = true;
      return lease;
    });
    await Future<void>.delayed(Duration.zero);
    expect(fourthEntered, false);
    release();
    (await fourth)();
  });

  test(
    'cancelling a queued native turn leaves the active chat intact',
    () async {
      final provider = NativeFixture();
      final transport = TrackedNativeTransport(provider);
      final ai = DuckAI(tokenProvider: provider, transport: transport);
      final firstDelta = Completer<void>(), firstDone = Completer<void>();
      final subscription = ai
          .stream('first')
          .listen(
            (delta) {
              expect(delta, 'first');
              firstDelta.complete();
            },
            onError: firstDone.completeError,
            onDone: firstDone.complete,
          );
      try {
        await firstDelta.future;
        final token = CancellationToken();
        final second = ai.chat(
          'cancel me',
          options: ChatOptions(cancellationToken: token),
        );
        final rejected = expectLater(
          second,
          throwsA(isA<CancelledException>()),
        );
        await transport.secondQueued.future;
        token.cancel();
        await rejected;
        expect(provider.headersCalls, 1);
        expect(provider.active, true);
        expect(provider.resetActiveRequest, false);
        provider.firstBody.add(utf8.encode('data: [DONE]\n\n'));
        await firstDone.future;
        expect((await ai.chat('next')).done, true);
        expect(provider.headersCalls, 2);
      } finally {
        await subscription.cancel();
        await ai.close();
        await provider.firstBody.close();
      }
    },
  );

  test(
    'client close awaits transport disposal and remains idempotent',
    () async {
      final tokens = FixtureTokens(), transport = DeferredCloseTransport();
      final ai = DuckAI(tokenProvider: tokens, transport: transport);
      final closing = ai.close();
      expect(identical(closing, ai.close()), true);
      await transport.started.future;
      expect(tokens.closes, 0);
      transport.disposed.complete();
      await closing;
      expect(transport.closes, 1);
      expect(tokens.closes, 1);
      final provider = AnonymousTokenProvider();
      expect(identical(provider.close(), provider.close()), true);
      await provider.close();
    },
  );

  test(
    'total timeout bounds bootstrap even if a custom provider finishes late',
    () async {
      final tokens = PendingTokens(), transport = FixtureTransport();
      final ai = DuckAI(
        tokenProvider: tokens,
        transport: transport,
        settings: Settings(timeout: const Duration(milliseconds: 30)),
      );
      try {
        final answer = ai.chat('test');
        final rejected = expectLater(
          answer,
          throwsA(
            isA<DuckAIException>().having((e) => e.statusCode, 'timeout', 504),
          ),
        );
        await tokens.started.future;
        await rejected;
        tokens.result.complete({'x-vqd-hash-1': 'late'});
        await Future<void>.delayed(Duration.zero);
        expect(transport.calls, 0);
      } finally {
        await ai.close();
      }
    },
  );

  test('native session queue is covered by the turn deadline', () async {
    final provider = NativeFixture()..sends = 1;
    final held = await provider.acquireSession(CancellationToken());
    final ai = DuckAI(
      tokenProvider: provider,
      transport: WebViewTransport(provider),
      settings: Settings(timeout: const Duration(milliseconds: 20)),
    );
    try {
      await expectLater(
        ai.chat('queued'),
        throwsA(
          isA<DuckAIException>().having((e) => e.statusCode, 'timeout', 504),
        ),
      );
      expect(provider.headersCalls, 0);
      held();
      expect((await ai.chat('after timeout')).done, true);
    } finally {
      held();
      await ai.close();
    }
  });

  test(
    'worker runs separately and cancellation terminates a busy worker',
    () async {
      expect(
        await runAttachmentWorker(workerIdentity, null, null),
        'duckai-attachment',
      );
      final started = ReceivePort(), token = CancellationToken();
      try {
        final work = runAttachmentWorker(spinWorker, started.sendPort, token);
        final rejected = expectLater(work, throwsA(isA<CancelledException>()));
        await started.first;
        token.cancel();
        await rejected;
        // The cancelled worker must release its slot for the next job.
        expect(
          await runAttachmentWorker(workerIdentity, null, null),
          'duckai-attachment',
        );
      } finally {
        token.cancel();
        started.close();
      }
    },
  );

  test('a timed-out paused stream releases its native session', () async {
    final provider = NativeFixture();
    final ai = DuckAI(
      tokenProvider: provider,
      transport: WebViewTransport(provider),
      settings: Settings(timeout: const Duration(milliseconds: 50)),
    );
    final firstDelta = Completer<void>(), failed = Completer<void>();
    late StreamSubscription<String> subscription;
    subscription = ai
        .stream('first')
        .listen(
          (_) {
            subscription.pause();
            firstDelta.complete();
          },
          onError: (Object error) {
            expect(
              error,
              isA<DuckAIException>().having(
                (e) => e.statusCode,
                'timeout',
                504,
              ),
            );
            failed.complete();
          },
        );
    try {
      await firstDelta.future;
      await provider.firstCancelled.future;
      expect((await ai.chat('after timeout')).done, true);
      subscription.resume();
      await failed.future;
    } finally {
      await subscription.cancel();
      await ai.close();
      await provider.firstBody.close();
    }
  });

  test('image preparation lets the main isolate process events', () async {
    final ai = DuckAI(tokenProvider: FixtureTokens());
    try {
      await ai.listModels();
      final file = Attachment(
        image.encodePng(image.Image(width: 2000, height: 1500)),
        'photo.png',
      );
      var finished = false, eventBeforeCompletion = false;
      final tick = Completer<void>();
      Timer.run(() {
        eventBeforeCompletion = !finished;
        tick.complete();
      });
      final parts = await ai.prepareFiles([file]);
      finished = true;
      await tick.future;
      expect(eventBeforeCompletion, true);
      expect(parts.single['type'], 'image');
    } finally {
      await ai.close();
    }
  });

  test(
    'bridge buffer overflow fails explicitly without dropping accepted bytes',
    () async {
      Object? cancellation;
      final state = BrowserRequest((error) {
        cancellation ??= error;
      }, maxBufferedBytes: 4);
      state.headers(200, {'content-type': 'text/event-stream'});
      final response = await state.ready.future;
      expect(state.add([1, 2, 3, 4]), true);
      expect(state.add([5]), false);
      expect(cancellation, isA<DuckAIException>());
      final accepted = <int>[];
      await expectLater(
        response.body.forEach(accepted.addAll),
        throwsA(isA<DuckAIException>()),
      );
      expect(accepted, [1, 2, 3, 4]);
    },
  );

  test(
    'disabled event retention preserves text and opaque history parts',
    () async {
      final transport = FixtureTransport()
        ..text =
            'data: {"message":"hello","parts":[{"id":"r","opaque":"keep"}]}\n\ndata: [DONE]\n\n';
      final ai = DuckAI(
        tokenProvider: FixtureTokens(),
        transport: transport,
        settings: Settings(retainEvents: false),
      );
      try {
        final chat = ai.conversation();
        final answer = await chat.chat('test');
        expect(answer.text, 'hello');
        expect(answer.events, isEmpty);
        expect(chat.messages.last['parts'], [
          {'id': 'r', 'opaque': 'keep'},
        ]);
        final copy = answer.assistantMessage;
        (copy['parts'] as List).first['opaque'] = 'changed';
        expect(
          (answer.assistantMessage['parts'] as List).first['opaque'],
          'keep',
        );
      } finally {
        await ai.close();
      }
    },
  );

  test(
    'history budget rejects locally while preserving committed history',
    () async {
      final transport = FixtureTransport();
      final ai = DuckAI(
        tokenProvider: FixtureTokens(),
        transport: transport,
        settings: Settings(maxHistoryMessages: 2),
      );
      try {
        final chat = ai.conversation();
        await chat.chat('one');
        await expectLater(
          chat.chat('two'),
          throwsA(
            isA<DuckAIException>().having(
              (e) => e.statusCode,
              'history limit',
              413,
            ),
          ),
        );
        expect(chat.messages.length, 2);
        expect(transport.calls, 1);
        chat.clear();
        expect((await chat.chat('new')).done, true);
      } finally {
        await ai.close();
      }
    },
  );

  test(
    'XML budgets preserve 413 and reject excessive depth and node counts',
    () async {
      final ai = DuckAI(tokenProvider: FixtureTokens());
      try {
        for (final xml in [
          '${List.filled(140, '<a>').join()}hello${List.filled(140, '</a>').join()}',
          '<root>${List.filled(100001, '<x/>').join()}</root>',
        ]) {
          await expectLater(
            ai.prepareFiles([Attachment(utf8.encode(xml), 'complex.xml')]),
            throwsA(
              isA<AttachmentException>()
                  .having((e) => e.statusCode, 'complexity limit', 413)
                  .having(
                    (e) => e.detail,
                    'reason',
                    'Document XML is too complex',
                  ),
            ),
          );
        }
      } finally {
        await ai.close();
      }
    },
  );
}
