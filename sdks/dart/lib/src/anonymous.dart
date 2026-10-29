import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'types.dart';
import 'transport.dart';
import 'async_gate.dart';
import 'browser_request.dart';

abstract interface class TokenProvider {
  Future<Map<String, String>> headers({CancellationToken? cancellationToken});
  Future<void> close();
}

/// Uses the OS browser engine; no subprocess, remote server or visible login.
class AnonymousTokenProvider implements TokenProvider {
  final Duration timeout;
  final int maxBufferedBytes;
  final String _journey = List.generate(
    16,
    (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();
  HeadlessInAppWebView? _web;
  InAppWebViewController? _controller;
  bool _closed = false;
  int _generation = 0;
  final _bootstrap = AsyncGate();
  final _sessions = AsyncGate();
  Future<void>? _closing;
  Future<void> _resetting = Future.value();
  void Function(Object)? _abortActive;
  final Map<String, BrowserRequest> _requests = {};
  AnonymousTokenProvider({
    this.timeout = const Duration(seconds: 30),
    this.maxBufferedBytes = 256 * 1024,
  });

  /// One complete turn owns the context, including bootstrap and streaming.
  Future<void Function()> acquireSession(CancellationToken token) =>
      _sessions.acquire(token);

  Future<void> _reset() {
    _generation++;
    for (final state in List.of(_requests.values)) {
      state.fail(CancelledException());
    }
    _requests.clear();
    final web = _web;
    _web = null;
    _controller = null;
    final previous = _resetting;
    return _resetting = () async {
      try {
        await previous;
      } finally {
        await web?.dispose();
      }
    }();
  }

  @override
  Future<void> close() => _closing ??= _close();
  Future<void> _close() async {
    _closed = true;
    final reason = StateError('Token provider is closed');
    _sessions.close(reason);
    _bootstrap.close(reason);
    _abortActive?.call(StateError('Token provider is closed'));
    await _reset();
  }

  @override
  Future<Map<String, String>> headers({
    CancellationToken? cancellationToken,
  }) async {
    final release = await _bootstrap.acquire(cancellationToken);
    try {
      return await _headers(cancellationToken);
    } finally {
      release();
    }
  }

  Future<Map<String, String>> _headers(
    CancellationToken? cancellationToken,
  ) async {
    cancellationToken?.throwIfCancelled();
    if (_closed) throw StateError('Token provider is closed');
    if (!Platform.isIOS && !Platform.isAndroid) {
      throw UnsupportedError('Automatic sessions support iOS and Android only');
    }
    final failed = Completer<Never>();
    _abortActive = (error) {
      if (!failed.isCompleted) {
        failed.completeError(error);
      }
    };
    final timer = Timer(timeout, () {
      if (!failed.isCompleted) {
        failed.completeError(
          DuckAIException(504, 'Anonymous session generation timed out'),
        );
      }
    });
    final remove = cancellationToken?.listen(() {
      if (!failed.isCompleted) failed.completeError(CancelledException());
    });
    Future<T> bounded<T>(Future<T> operation) =>
        Future.any([operation, failed.future]);
    try {
      cancellationToken?.throwIfCancelled();
      while (WidgetsBinding.instance.lifecycleState != null &&
          WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
        final state = WidgetsBinding.instance.lifecycleState;
        if (state == AppLifecycleState.paused ||
            state == AppLifecycleState.hidden) {
          throw DuckAIException(
            503,
            'Generate sessions while the app is in the foreground',
          );
        }
        // During startup the first frame can precede the resumed notification.
        // Wait within the bootstrap deadline; background work remains unsupported.
        await bounded(Future<void>.delayed(const Duration(milliseconds: 50)));
      }
      if (_web == null) {
        final generation = _generation;
        final created = Completer<void>();
        _web = HeadlessInAppWebView(
          initialUrlRequest: URLRequest(url: WebUri('https://duck.ai/')),
          initialSettings: InAppWebViewSettings(
            incognito: true,
            javaScriptEnabled: true,
            useShouldOverrideUrlLoading: true,
            useOnRenderProcessGone: true,
            transparentBackground: true,
          ),
          onWebViewCreated: (controller) {
            if (_closed || generation != _generation) return;
            _controller = controller;
            controller.addJavaScriptHandler(
              handlerName: 'duckaiStream',
              callback: (args) {
                if (generation != _generation) return false;
                if (args.isNotEmpty && args.first is Map) {
                  return _receive(Map<String, dynamic>.from(args.first as Map));
                }
                return null;
              },
            );
            if (!created.isCompleted) created.complete();
          },
          onWebContentProcessDidTerminate: (controller) {
            if (generation == _generation) _browserTerminated();
          },
          onRenderProcessGone: (controller, detail) {
            if (generation == _generation) _browserTerminated();
          },
          onReceivedError: (controller, request, error) {
            if (generation != _generation) return;
            if (request.isForMainFrame == true && !failed.isCompleted) {
              failed.completeError(
                DuckAIException(
                  503,
                  'Native WebView navigation failed: ${error.description}',
                ),
              );
            }
          },
          onReceivedHttpError: (controller, request, response) {
            if (generation != _generation) return;
            if (request.isForMainFrame == true && !failed.isCompleted) {
              failed.completeError(
                DuckAIException.upstream(
                  response.statusCode ?? 502,
                  'Duck.ai bootstrap page failed',
                ),
              );
            }
          },
          shouldOverrideUrlLoading: (controller, action) async {
            final url = action.request.url;
            if (action.isForMainFrame &&
                (url?.scheme != 'https' || url?.host != 'duck.ai')) {
              return NavigationActionPolicy.CANCEL;
            }
            return NavigationActionPolicy.ALLOW;
          },
        );
        await bounded(_web!.run());
        await bounded(created.future);
      }
      while (true) {
        cancellationToken?.throwIfCancelled();
        if (_closed) throw StateError('Token provider is closed');
        final ready = await bounded(
          _controller!.evaluateJavascript(
            source:
                "location.origin === 'https://duck.ai' && !!document.querySelector('iframe#jsa')?.contentDocument?.body",
          ),
        );
        if (ready == true) break;
        await bounded(Future<void>.delayed(const Duration(milliseconds: 100)));
      }
      final script = await rootBundle.loadString(
        'packages/duckai_sdk/assets/browser-bootstrap.js',
      );
      final result = await bounded(
        _controller!.callAsyncJavaScript(
          functionBody: script,
          arguments: {'journey': _journey},
        ),
      );
      cancellationToken?.throwIfCancelled();
      if (_closed) throw StateError('Token provider is closed');
      if (result?.error != null) {
        throw DuckAIException(
          503,
          'Native WebView challenge failed: ${result!.error}',
        );
      }
      final value = result?.value;
      if (value is! Map) throw DuckAIException(502, 'Invalid bootstrap result');
      if (value['status'] is int) {
        throw DuckAIException.upstream(
          value['status'] as int,
          value['error'] ?? 'Bootstrap rejected',
          {'retry-after': value['retryAfter'] as String? ?? ''},
        );
      }
      final headers = Map<String, String>.from(value['headers'] as Map);
      if ((headers['x-vqd-hash-1'] ?? '').isEmpty) {
        throw DuckAIException(502, 'Bootstrap returned no challenge headers');
      }
      return headers;
    } catch (error) {
      await _reset();
      if (error is DuckAIException ||
          error is CancelledException ||
          error is StateError) {
        rethrow;
      }
      throw DuckAIException(503, 'Native anonymous bootstrap failed: $error');
    } finally {
      timer.cancel();
      remove?.call();
      _abortActive = null;
    }
  }

  void _browserTerminated() {
    final error = DuckAIException(
      503,
      'Native WebView process terminated; retry explicitly',
    );
    for (final state in _requests.values) {
      state.fail(error);
    }
    _requests.clear();
    _abortActive?.call(error);
    unawaited(_reset().catchError((Object _) {}));
  }

  bool _receive(Map<String, dynamic> event) {
    final id = event['id'];
    final state = _requests[id];
    if (state == null) return false;
    switch (event['kind']) {
      case 'headers':
        state.headers(
          event['status'] as int,
          Map<String, String>.from(event['headers'] as Map),
        );
      case 'data':
        final value = event['data'];
        if (value is! String || value.length > 32768) {
          state.fail(DuckAIException(502, 'Invalid browser stream chunk'));
          _cancelRequest(id as String);
          return false;
        }
        try {
          return state.add(base64Decode(value));
        } catch (e) {
          state.fail(DuckAIException(502, 'Invalid browser stream chunk'));
        }
      case 'done':
        state.finish();
        _requests.remove(id);
      case 'error':
        state.fail(
          event['error'] == 'cancelled'
              ? CancelledException()
              : DuckAIException(
                  502,
                  event['error'] ?? 'Browser transport failed',
                ),
        );
        _requests.remove(id);
    }
    return true;
  }

  void _cancelRequest(String id, [Object? error]) {
    _requests.remove(id)?.fail(error ?? CancelledException());
    final controller = _controller;
    if (controller != null) {
      unawaited(
        controller
            .evaluateJavascript(
              source: 'window.__duckaiRequests?.[${jsonEncode(id)}]?.abort()',
            )
            .catchError((Object _) => null),
      );
    }
  }

  Future<HTTPResponse> send(
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken token,
    int maxBytes,
  ) async {
    token.throwIfCancelled();
    final controller = _controller;
    if (_closed || controller == null) {
      throw DuckAIException(503, 'Browser session is unavailable');
    }
    final id = List.generate(
      16,
      (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0'),
    ).join();
    final state = BrowserRequest(
      (error) => _cancelRequest(id, error),
      maxBufferedBytes: maxBufferedBytes,
    );
    _requests[id] = state;
    final remove = token.listen(() => _cancelRequest(id));
    // The HTTP body owns this listener after headers have been delivered.
    try {
      final script = await rootBundle.loadString(
        'packages/duckai_sdk/assets/browser-transport.js',
      );
      token.throwIfCancelled();
      final filtered = {
        for (final e in headers.entries)
          if (!{
            'user-agent',
            'origin',
            'referer',
            'sec-fetch-site',
            'sec-fetch-mode',
            'sec-fetch-dest',
          }.contains(e.key.toLowerCase()))
            e.key: e.value,
      };
      unawaited(
        controller
            .callAsyncJavaScript(
              functionBody:
                  "const emit = async event => { if (await window.flutter_inappwebview.callHandler('duckaiStream',event) === false) throw new Error('Native stream cancelled'); };\n$script",
              arguments: {
                'requestId': id,
                'requestHeaders': filtered,
                'requestBody': body,
                'maxBytes': maxBytes,
              },
            )
            .then(
              (result) {
                if (result?.error != null) {
                  state.fail(
                    DuckAIException(
                      502,
                      'Browser request failed: ${result!.error}',
                    ),
                  );
                  _requests.remove(id);
                }
              },
              onError: (Object error) {
                state.fail(
                  DuckAIException(502, 'Browser request failed: $error'),
                );
                _requests.remove(id);
              },
            ),
      );
      final response = await state.ready.future;
      return HTTPResponse(
        response.statusCode,
        response.headers,
        _releaseListener(response.body, remove),
        cancel: response.cancel,
      );
    } catch (_) {
      remove();
      _cancelRequest(id);
      rethrow;
    }
  }
}

Stream<List<int>> _releaseListener(
  Stream<List<int>> body,
  void Function() remove,
) async* {
  try {
    yield* body;
  } finally {
    remove();
  }
}

/// Keeps challenge generation and chat requests in the same OS browser context.
class WebViewTransport implements DuckAITransport {
  final AnonymousTokenProvider provider;
  final int maxBytes;
  WebViewTransport(this.provider, {this.maxBytes = 8 * 1024 * 1024});
  @override
  Future<void> close() => provider.close();

  Future<void Function()> acquireSession(CancellationToken token) =>
      provider.acquireSession(token);

  @override
  Future<HTTPResponse> send(
    Uri url,
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken cancellationToken,
  ) {
    if (url.toString() != 'https://duck.ai/duckchat/v1/chat') {
      throw ArgumentError('Only the Duck.ai chat endpoint is supported');
    }
    return provider.send(headers, body, cancellationToken, maxBytes);
  }
}
