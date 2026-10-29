import 'dart:async';
import 'dart:io';
import 'dart:convert';
import 'anonymous.dart';
import 'attachments.dart';
import 'catalog.dart';
import 'model.dart';
import 'sse.dart';
import 'transport.dart';
import 'types.dart';
import 'async_gate.dart';

class DuckAI {
  final String model;
  final Settings settings;
  late final TokenProvider _tokens;
  late final DuckAITransport _transport;
  final Set<ChatStream> _streams = {};
  final _lifetime = CancellationToken();
  bool _closed = false;
  Future<void>? _closing;
  DuckAI({
    this.model = Model.gpt6Luna,
    Settings? settings,
    TokenProvider? tokenProvider,
    DuckAITransport? transport,
  }) : settings = settings ?? Settings() {
    if (model.trim().isEmpty) throw ArgumentError('Model must not be empty');
    final native =
        tokenProvider == null &&
            transport == null &&
            !this.settings.headers.containsKey('x-vqd-hash-1') &&
            !this.settings.headers.containsKey('x-vqd-4')
        ? AnonymousTokenProvider(
            timeout: this.settings.tokenGenerationTimeout,
            maxBufferedBytes: this.settings.maxBufferedBytes,
          )
        : null;
    _tokens =
        tokenProvider ??
        native ??
        AnonymousTokenProvider(timeout: this.settings.tokenGenerationTimeout);
    _transport =
        transport ??
        (native == null
            ? IOTransport()
            : WebViewTransport(
                native,
                maxBytes: this.settings.maxCollectedBytes,
              ));
  }
  void _check() {
    if (_closed) throw StateError('Duck.ai client is closed');
  }

  Future<void> close() => _closing ??= _close();
  Future<void> _close() async {
    _closed = true;
    _lifetime.cancel();
    try {
      await Future.wait(List.of(_streams).map((stream) => stream.close()));
    } finally {
      try {
        await _transport.close();
      } finally {
        await _tokens.close();
      }
    }
  }

  Future<List<ModelInfo>> listModels({bool includeSubscriber = true}) =>
      Catalog.listModels(includeSubscriber: includeSubscriber);
  Future<AttachmentLimits> attachmentLimits({String? model}) =>
      Attachments.limits(model ?? this.model, settings);
  Future<List<ContentPart>> prepareFiles(
    List<Attachment> files, {
    String? model,
    CancellationToken? cancellationToken,
  }) async {
    _check();
    final token = CancellationToken();
    final removeCaller = cancellationToken?.listen(token.cancel);
    final removeLifetime = _lifetime.listen(token.cancel);
    try {
      final id = model ?? this.model;
      final parts = await Attachments.prepare(
        files,
        id,
        settings,
        cancellationToken: token,
      );
      await Attachments.validate(
        [
          {'role': 'user', 'content': parts},
        ],
        id,
        settings,
        cancellationToken: token,
      );
      token.throwIfCancelled();
      _check();
      return parts;
    } finally {
      removeCaller?.call();
      removeLifetime();
    }
  }

  ChatStream stream(
    String? prompt, {
    ChatOptions options = const ChatOptions(),
  }) {
    _check();
    final stream = ChatStream._(this, prompt, options);
    _streams.add(stream);
    return stream;
  }

  Stream<dynamic> events(
    String? prompt, {
    ChatOptions options = const ChatOptions(),
  }) => stream(prompt, options: options).events;
  Future<ChatResponse> chat(
    String? prompt, {
    ChatOptions options = const ChatOptions(),
  }) async {
    final stream = this.stream(prompt, options: options);
    try {
      await for (final _ in stream) {}
      return stream.response;
    } finally {
      await stream.close();
    }
  }

  Conversation conversation({ChatOptions options = const ChatOptions()}) {
    _check();
    return Conversation._(this, options);
  }

  Future<Map<String, dynamic>> _request(
    String? prompt,
    ChatOptions options,
    CancellationToken token,
  ) async {
    _check();
    token.throwIfCancelled();
    final id = options.model ?? model;
    if (id.trim().isEmpty) throw ArgumentError('Model must not be empty');
    final count = options.messages.length + (prompt == null ? 0 : 1);
    final limit = settings.maxHistoryMessages;
    if (limit != null && count > limit) {
      throw DuckAIException(
        413,
        'Conversation exceeds message limit; clear or shorten its history',
      );
    }
    final messages = cloneJson<List<dynamic>>(options.messages).cast<Message>();
    if (prompt != null) messages.add({'role': 'user', 'content': prompt});
    if (messages.isEmpty ||
        messages.any(
          (m) => m['role'] is! String || (m['role'] as String).isEmpty,
        )) {
      throw ArgumentError('Provide a prompt or messages with roles');
    }
    if (options.files.isNotEmpty) {
      final last = messages.last;
      if (last['role'] != 'user') {
        throw ArgumentError('Files attach to the final user message');
      }
      final content = last['content'] is String
          ? <ContentPart>[
              {'type': 'text', 'text': last['content']},
            ]
          : (last['content'] as List? ?? []).cast<ContentPart>().toList();
      content.addAll(
        await Attachments.prepare(
          options.files,
          id,
          settings,
          cancellationToken: token,
        ),
      );
      last['content'] = content;
    }
    final info = await Catalog.getModel(id);
    final reasoning =
        options.reasoningEffort ?? info?.defaultReasoningEffort ?? 'none';
    if (info != null && !info.reasoningEfforts.contains(reasoning)) {
      throw ArgumentError('Unsupported reasoning effort for ${info.name}');
    }
    await Attachments.validate(
      messages,
      id,
      settings,
      cancellationToken: token,
    );
    token.throwIfCancelled();
    _check();
    return {
      ...cloneJson<Map<String, dynamic>>(options.extra),
      'model': id,
      'messages': messages,
      'reasoningEffort': reasoning,
      'canUseTools': options.canUseTools ?? info?.supportsTools ?? true,
      'canShowGreeting': options.canShowGreeting ?? false,
    };
  }

  Future<HTTPResponse> _open(
    Map<String, dynamic> body,
    CancellationToken token,
  ) async {
    _check();
    final headers = <String, String>{
      'accept': 'text/event-stream',
      'content-type': 'application/json',
      'origin': 'https://duck.ai',
      'referer': 'https://duck.ai/',
      'accept-language': 'en-US,en;q=0.6',
      'dnt': '1',
      'sec-gpc': '1',
      ...settings.headers,
    };
    if (!headers.containsKey('x-vqd-hash-1') &&
        !headers.containsKey('x-vqd-4')) {
      if (!settings.autoToken) {
        throw DuckAIException(
          503,
          'Supply fresh captured headers or enable autoToken',
        );
      }
      headers.addAll(
        await token.bind(_tokens.headers(cancellationToken: token)),
      );
    }
    token.throwIfCancelled();
    _check();
    HTTPResponse response;
    try {
      response = await token.bind(
        _transport.send(
          Uri.parse('https://duck.ai/duckchat/v1/chat'),
          headers,
          body,
          token,
        ),
        onCancelledValue: (response) => response.cancel(),
      );
    } on SocketException catch (e) {
      throw DuckAIException(502, 'Could not connect to Duck.ai: $e');
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final bytes = <int>[];
      try {
        await for (final chunk in response.body) {
          bytes.addAll(chunk.take(65536 - bytes.length));
          if (bytes.length >= 65536) break;
        }
      } finally {
        response.cancel();
      }
      Object detail = 'Duck.ai rejected the request';
      try {
        detail = jsonDecode(utf8.decode(bytes)) as Object;
      } catch (_) {}
      throw DuckAIException.upstream(
        response.statusCode,
        detail,
        response.headers,
      );
    }
    if (!(response.headers['content-type'] ?? '').toLowerCase().contains(
      'text/event-stream',
    )) {
      response.cancel();
      throw DuckAIException(502, 'Duck.ai returned a non-SSE response');
    }
    return response;
  }
}

/// Single-consumer delta stream. Cancelling its subscription releases the request.
class ChatStream extends Stream<String> {
  final DuckAI _client;
  final String? _prompt;
  final ChatOptions _options;
  final ChatResponse response;
  Map<String, dynamic>? _request;
  Map<String, dynamic>? get request =>
      _request == null ? null : cloneJson(_request!);
  final CancellationToken _token = CancellationToken();
  final Future<ChatOptions> Function(CancellationToken)? _acquire;
  final void Function(ChatStream)? _finish;
  bool _consumed = false, _closed = false, _acquired = false;
  HTTPResponse? _network;
  Timer? _timer;
  bool _timedOut = false;
  StreamSubscription<dynamic>? _source;
  Future<void>? _closing;
  ChatStream._(
    this._client,
    this._prompt,
    this._options, {
    Future<ChatOptions> Function(CancellationToken)? acquire,
    void Function(ChatStream)? finish,
  }) : response = ChatResponse(
         _options.model ?? _client.model,
         retainEvents: _client.settings.retainEvents,
       ),
       _acquire = acquire,
       _finish = finish;
  void _cleanup() {
    _closed = true;
    _cancel();
    _timer?.cancel();
    _client._streams.remove(this);
  }

  void _cancel() {
    _token.cancel();
    _network?.cancel();
    // Finish cleanup even while the caller has paused its subscription. The
    // outer controller retains the terminal error until the caller resumes.
    if (_source?.isPaused == true) _source?.resume();
  }

  Future<void> close() => _closing ??= _close();
  Future<void> _close() async {
    _cleanup();
    await _source?.cancel();
  }

  Stream<dynamic> get events => _make(true);
  @override
  StreamSubscription<String> listen(
    void Function(String)? onData, {
    Function? onError,
    void Function()? onDone,
    bool? cancelOnError,
  }) => _make(false).cast<String>().listen(
    onData,
    onError: onError,
    onDone: onDone,
    cancelOnError: cancelOnError,
  );
  Stream<dynamic> _make(bool raw) {
    final controller = StreamController<dynamic>();
    var ownsSource = false;
    controller.onListen = () {
      if (_consumed) {
        controller.addError(StateError('A stream has one consumer'));
        controller.close();
        return;
      }
      _consumed = true;
      ownsSource = true;
      _source = _run(raw).listen(
        controller.add,
        onError: controller.addError,
        onDone: controller.close,
      );
    };
    controller.onPause = () {
      if (ownsSource) _source?.pause();
    };
    controller.onResume = () {
      if (ownsSource) _source?.resume();
    };
    controller.onCancel = () async {
      if (ownsSource) await close();
    };
    return controller.stream;
  }

  Stream<dynamic> _run(bool raw) async* {
    if (_closed) throw StateError('Stream is closed');
    final remove = _options.cancellationToken?.listen(_cancel);
    void Function()? releaseSession;
    StreamIterator<SSEEvent>? iterator;
    // Covers queues, attachment workers, bootstrap, headers, and the response.
    _timer = Timer(_client.settings.timeout, () {
      _timedOut = true;
      _cancel();
    });
    try {
      _token.throwIfCancelled();
      var options = _options;
      if (_acquire != null) {
        options = await _acquire(_token);
        _acquired = true;
      }
      _request = await _token.bind(_client._request(_prompt, options, _token));
      final transport = _client._transport;
      if (transport is WebViewTransport) {
        releaseSession = await transport.acquireSession(_token);
      }
      _token.throwIfCancelled();
      _network = await _client._open(_request!, _token);
      iterator = StreamIterator(
        parseSSE(_network!.body, _client.settings.maxCollectedBytes),
      );
      while (await _token.bind(iterator.moveNext())) {
        final event = iterator.current;
        _token.throwIfCancelled();
        if (event.done) {
          response.done = true;
          return;
        }
        final value = event.data;
        if ((value is Map && value['action'] == 'error') ||
            event.name == 'error' ||
            event.name == 'proxy_error') {
          final s = value is Map ? value['status'] : null;
          throw DuckAIException.upstream(
            s is int && s >= 400 && s <= 599 ? s : 502,
            value,
          );
        }
        final delta = response.append(value);
        if (raw) {
          yield cloneJson<dynamic>(value);
        } else if (delta.isNotEmpty) {
          yield delta;
        }
      }
      _token.throwIfCancelled();
      throw IncompleteResponseException(response);
    } catch (_) {
      if (_timedOut) throw DuckAIException(504, 'Duck.ai request timed out');
      rethrow;
    } finally {
      try {
        await iterator?.cancel();
      } finally {
        remove?.call();
        releaseSession?.call();
        try {
          if (_acquired) _finish?.call(this);
        } finally {
          _cleanup();
        }
      }
    }
  }
}

class Conversation {
  final DuckAI _client;
  final ChatOptions _defaults;
  List<Message> _history;
  final _gate = AsyncGate();
  int _active = 0;
  Conversation._(this._client, this._defaults)
    : _history = cloneJson<List<dynamic>>(_defaults.messages).cast<Message>();
  List<Message> get messages =>
      cloneJson<List<dynamic>>(_history).cast<Message>();
  void clear() {
    if (_active > 0) throw StateError('Cannot clear during a turn');
    _history = [];
  }

  ChatStream stream(String prompt, {ChatOptions? options}) {
    _client._check();
    final selected = _defaults.merge(options);
    void Function()? release;
    final stream = ChatStream._(
      _client,
      prompt,
      selected,
      acquire: (token) async {
        _active++;
        try {
          // The lease belongs to this turn, not to the conversation closure.
          release = await _gate.acquire(token);
          return selected.withMessages(messages, cancellationToken: token);
        } catch (_) {
          _active--;
          rethrow;
        }
      },
      finish: (stream) {
        if (stream.response.done && stream._request != null) {
          _history = (cloneJson<List<dynamic>>(
            stream._request!['messages'] as List,
          ).cast<Message>())..add(stream.response.assistantMessage);
        }
        release?.call();
        _active--;
      },
    );
    _client._streams.add(stream);
    return stream;
  }

  Stream<dynamic> events(String prompt, {ChatOptions? options}) =>
      stream(prompt, options: options).events;
  Future<ChatResponse> chat(String prompt, {ChatOptions? options}) async {
    final stream = this.stream(prompt, options: options);
    try {
      await for (final _ in stream) {}
      return stream.response;
    } finally {
      await stream.close();
    }
  }
}
