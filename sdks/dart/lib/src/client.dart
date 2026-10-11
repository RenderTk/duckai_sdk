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

class DuckAI {
  final String model;
  final Settings settings;
  late final TokenProvider _tokens;
  late final DuckAITransport _transport;
  final Set<ChatStream> _streams = {};
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
        ? AnonymousTokenProvider(timeout: this.settings.tokenGenerationTimeout)
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
    try {
      for (final stream in List.of(_streams)) {
        await stream.close();
      }
    } finally {
      _transport.close();
      await _tokens.close();
    }
  }

  Future<List<ModelInfo>> listModels({bool includeSubscriber = true}) =>
      Catalog.listModels(includeSubscriber: includeSubscriber);
  Future<AttachmentLimits> attachmentLimits({String? model}) =>
      Attachments.limits(model ?? this.model, settings);
  Future<List<ContentPart>> prepareFiles(
    List<Attachment> files, {
    String? model,
  }) async {
    _check();
    final id = model ?? this.model;
    final parts = await Attachments.prepare(files, id, settings);
    await Attachments.validate(
      [
        {'role': 'user', 'content': parts},
      ],
      id,
      settings,
    );
    return parts;
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
    await Attachments.validate(messages, id, settings);
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
    void Function() networkReady,
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
      headers.addAll(await _tokens.headers(cancellationToken: token));
    }
    token.throwIfCancelled();
    _check();
    networkReady();
    HTTPResponse response;
    try {
      response = await _transport.send(
        Uri.parse('https://duck.ai/duckchat/v1/chat'),
        headers,
        body,
        token,
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
  ChatStream._(
    this._client,
    this._prompt,
    this._options, {
    Future<ChatOptions> Function(CancellationToken)? acquire,
    void Function(ChatStream)? finish,
  }) : response = ChatResponse(_options.model ?? _client.model),
       _acquire = acquire,
       _finish = finish;
  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    _token.cancel();
    _network?.cancel();
    _timer?.cancel();
    _client._streams.remove(this);
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
    late StreamSubscription<dynamic> subscription;
    final controller = StreamController<dynamic>();
    controller.onListen = () {
      subscription = _run(raw).listen(
        controller.add,
        onError: controller.addError,
        onDone: controller.close,
      );
    };
    controller.onPause = () {
      subscription.pause();
    };
    controller.onResume = () {
      subscription.resume();
    };
    controller.onCancel = () async {
      await close();
      await subscription.cancel();
    };
    return controller.stream;
  }

  Stream<dynamic> _run(bool raw) async* {
    if (_consumed) throw StateError('A stream has one consumer');
    _consumed = true;
    if (_closed) throw StateError('Stream is closed');
    final remove = _options.cancellationToken?.listen(_token.cancel);
    try {
      _token.throwIfCancelled();
      var options = _options;
      if (_acquire != null) {
        options = await _acquire(_token);
        _acquired = true;
      }
      _request = await _client._request(_prompt, options, _token);
      _network = await _client._open(_request!, _token, () {
        _timer = Timer(_client.settings.timeout, () {
          _timedOut = true;
          _token.cancel();
          _network?.cancel();
        });
      });
      await for (final event in parseSSE(
        _network!.body,
        _client.settings.maxCollectedBytes,
      )) {
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
      remove?.call();
      if (_acquired) _finish?.call(this);
      await close();
    }
  }
}

class Conversation {
  final DuckAI _client;
  final ChatOptions _defaults;
  List<Message> _history;
  bool _busy = false;
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
    final stream = ChatStream._(
      _client,
      prompt,
      selected,
      acquire: (token) async {
        _active++;
        try {
          while (_busy) {
            token.throwIfCancelled();
            await Future<void>.delayed(const Duration(milliseconds: 10));
          }
          token.throwIfCancelled();
          _busy = true;
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
        _busy = false;
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
