import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/services.dart';
import 'package:image/image.dart' as image;
import 'package:flutter_test/flutter_test.dart';
import 'package:duckai_sdk/duckai_sdk.dart';

class Tokens implements TokenProvider {
  int calls = 0;
  bool closed = false;
  @override
  Future<Map<String, String>> headers({
    CancellationToken? cancellationToken,
  }) async {
    calls++;
    return {'x-vqd-hash-1': 'fixture'};
  }

  @override
  Future<void> close() async {
    closed = true;
  }
}

class Transport implements DuckAITransport {
  String text;
  int status = 200;
  List<Map<String, dynamic>> requests = [];
  Transport(this.text);
  @override
  void close() {}
  @override
  Future<HTTPResponse> send(
    Uri url,
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken token,
  ) async {
    requests.add(jsonDecode(jsonEncode(body)) as Map<String, dynamic>);
    return HTTPResponse(status, {
      'content-type': 'text/event-stream',
      'retry-after': '7',
    }, Stream.fromIterable(utf8.encode(text).map((b) => [b])));
  }
}

class WaitingTransport implements DuckAITransport {
  @override
  void close() {}
  @override
  Future<HTTPResponse> send(
    Uri url,
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken token,
  ) {
    final waiting = Completer<HTTPResponse>();
    token.listen(() {
      if (!waiting.isCompleted) waiting.completeError(CancelledException());
    });
    return waiting.future;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMessageHandler('flutter/assets', (message) async {
          final key = utf8.decode(message!.buffer.asUint8List());
          final file = File(key.replaceFirst('packages/duckai_sdk/', ''));
          return ByteData.sublistView(await file.readAsBytes());
        });
  });
  final fixture =
      jsonDecode(File('../../protocol/fixtures/chat.json').readAsStringSync())
          as Map<String, dynamic>;
  test(
    'shared request, bytewise Unicode SSE, opaque parts, transactional history',
    () async {
      final transport = Transport(fixture['sse'] as String), tokens = Tokens();
      final ai = DuckAI(
        model: fixture['model'] as String,
        transport: transport,
        tokenProvider: tokens,
      );
      final response = await ai.chat(fixture['prompt'] as String);
      expect(response.text, fixture['text']);
      expect(response.done, true);
      expect(response.assistantMessage['parts'], fixture['parts']);
      expect(transport.requests.first, fixture['request']);
      expect(tokens.calls, 1);
      final conversation = ai.conversation();
      await conversation.chat('One');
      await conversation.chat('Two');
      expect(conversation.messages.length, 4);
      transport.text = 'data: {"message":"partial"}\n\n';
      try {
        await conversation.chat('failed');
        fail('Incomplete reply accepted');
      } on IncompleteResponseException catch (e) {
        expect(e.response.text, 'partial');
      }
      expect(conversation.messages.length, 4);
      await ai.close();
      expect(tokens.closed, true);
    },
  );
  test('rate limits and cancellation are surfaced', () async {
    final transport = Transport('{}')..status = 429;
    final ai = DuckAI(transport: transport, tokenProvider: Tokens());
    await expectLater(
      ai.chat('test'),
      throwsA(
        isA<RateLimitException>().having(
          (e) => e.retryAfter,
          'retryAfter',
          '7',
        ),
      ),
    );
    transport.status = 200;
    transport.text = 'event: proxy_error\ndata: "failure"\n\n';
    await expectLater(
      ai.chat('error'),
      throwsA(
        isA<DuckAIException>().having((e) => e.statusCode, 'SSE error', 502),
      ),
    );
    final token = CancellationToken()..cancel();
    await expectLater(
      ai.chat('test', options: ChatOptions(cancellationToken: token)),
      throwsA(isA<CancelledException>()),
    );
    await ai.close();
  });
  test(
    'conversation defaults survive turn options; response header waits time out',
    () async {
      final transport = Transport('data: [DONE]\n\n');
      final ai = DuckAI(transport: transport, tokenProvider: Tokens());
      final chat = ai.conversation(
        options: const ChatOptions(
          model: Model.mistralSmall4,
          canShowGreeting: true,
          extra: {'durableStream': true},
        ),
      );
      await chat.chat('hello', options: const ChatOptions(canUseTools: false));
      expect(transport.requests.first['model'], Model.mistralSmall4);
      expect(transport.requests.first['canShowGreeting'], true);
      expect(transport.requests.first['durableStream'], true);
      await ai.close();
      final slow = DuckAI(
        settings: Settings(timeout: const Duration(milliseconds: 20)),
        tokenProvider: Tokens(),
        transport: WaitingTransport(),
      );
      await expectLater(
        slow.chat('test'),
        throwsA(
          isA<DuckAIException>().having(
            (e) => e.statusCode,
            'timeout status',
            504,
          ),
        ),
      );
      await slow.close();
    },
  );
  test('image orientation and document complexity limits', () async {
    final ai = DuckAI(tokenProvider: Tokens());
    for (final f
        in jsonDecode(
              File('../../protocol/fixtures/images.json').readAsStringSync(),
            )
            as List) {
      final part = (await ai.prepareFiles([
        Attachment(base64Decode(f['data'] as String), f['filename'] as String),
      ])).first;
      final decoded = image.decodePng(
        base64Decode((part['image'] as String).split(',').last),
      )!;
      expect(decoded.width, 12);
      expect(decoded.height, 28);
    }
    final deep =
        '${List.filled(140, '<a>').join()}hello${List.filled(140, '</a>').join()}';
    await expectLater(
      ai.prepareFiles([Attachment(utf8.encode(deep), 'deep.xml')]),
      throwsA(isA<AttachmentException>()),
    );
    await ai.close();
  });
  test('local shared document fixtures and bounded unterminated SSE', () async {
    final ai = DuckAI(model: Model.mistralSmall4, tokenProvider: Tokens());
    for (final value
        in jsonDecode(
              File('../../protocol/fixtures/documents.json').readAsStringSync(),
            )
            as List) {
      final parts = await ai.prepareFiles([
        Attachment(
          base64Decode(value['data'] as String),
          value['filename'] as String,
        ),
      ]);
      for (final expected in value['contains'] as List) {
        expect(
          parts.first['text'],
          contains(expected),
          reason: value['filename'] as String,
        );
      }
    }
    await ai.close();
    final limited = DuckAI(
      settings: Settings(maxCollectedBytes: 3),
      tokenProvider: Tokens(),
      transport: Transport('data: never terminated'),
    );
    await expectLater(limited.chat('test'), throwsA(isA<DuckAIException>()));
    await limited.close();
  });
}
