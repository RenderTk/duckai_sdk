import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

/// JSON objects preserve unknown upstream fields in requests and responses.
typedef Message = Map<String, dynamic>;
typedef ContentPart = Map<String, dynamic>;
T cloneJson<T>(T value) => jsonDecode(jsonEncode(value)) as T;

class DuckAIException implements Exception {
  final int statusCode;
  final Object detail;
  final Map<String, String> headers;
  DuckAIException(
    this.statusCode,
    this.detail, [
    Map<String, String> headers = const {},
  ]) : headers = Map.unmodifiable(headers);
  String? get retryAfter => headers['retry-after'];
  @override
  String toString() => 'DuckAIException($statusCode): $detail';
  static DuckAIException upstream(
    int status,
    Object detail, [
    Map<String, String> headers = const {},
  ]) => switch (status) {
    429 => RateLimitException(status, detail, headers),
    401 || 403 || 418 => ChallengeException(status, detail, headers),
    _ => DuckAIException(status, detail, headers),
  };
}

class RateLimitException extends DuckAIException {
  RateLimitException(super.statusCode, super.detail, [super.headers]);
}

class ChallengeException extends DuckAIException {
  ChallengeException(super.statusCode, super.detail, [super.headers]);
}

class AttachmentException extends DuckAIException {
  AttachmentException(super.statusCode, super.detail);
}

class IncompleteResponseException extends DuckAIException {
  final ChatResponse response;
  IncompleteResponseException(this.response)
    : super(502, 'Duck.ai stream ended before [DONE]');
}

class CancelledException implements Exception {
  @override
  String toString() => 'Duck.ai operation cancelled';
}

/// Reusable explicit cancellation, including session bootstrap and queued turns.
class CancellationToken {
  bool _cancelled = false;
  final Set<void Function()> _listeners = {};
  bool get isCancelled => _cancelled;
  void throwIfCancelled() {
    if (_cancelled) throw CancelledException();
  }

  void cancel() {
    if (_cancelled) return;
    _cancelled = true;
    for (final listener in List.of(_listeners)) {
      listener();
    }
    _listeners.clear();
  }

  void Function() listen(void Function() callback) {
    if (_cancelled) {
      callback();
      return () {};
    }
    _listeners.add(callback);
    return () {
      _listeners.remove(callback);
    };
  }
}

class Settings {
  final Map<String, String> headers;
  final bool autoToken;
  final Duration timeout, tokenGenerationTimeout;
  final int maxCollectedBytes,
      maxDocumentUploadBytes,
      maxDocumentTextCharacters,
      maxImageUploadBytes,
      maxImagePixels;
  Settings({
    Map<String, String> headers = const {},
    this.autoToken = true,
    this.timeout = const Duration(seconds: 120),
    this.tokenGenerationTimeout = const Duration(seconds: 30),
    this.maxCollectedBytes = 8 * 1024 * 1024,
    this.maxDocumentUploadBytes = 20 * 1024 * 1024,
    this.maxDocumentTextCharacters = 16000,
    this.maxImageUploadBytes = 10 * 1024 * 1024,
    this.maxImagePixels = 20000000,
  }) : headers = Map.unmodifiable({
         for (final e in headers.entries) e.key.toLowerCase(): e.value,
       }) {
    if (timeout <= Duration.zero ||
        tokenGenerationTimeout <= Duration.zero ||
        maxCollectedBytes <= 0 ||
        maxDocumentUploadBytes <= 0 ||
        maxImageUploadBytes <= 0 ||
        maxImagePixels <= 0 ||
        maxDocumentTextCharacters <= 0 ||
        maxDocumentTextCharacters > 16000) {
      throw ArgumentError('Invalid positive limits or timeouts');
    }
    const allowed = {
      'x-vqd-hash-1',
      'x-vqd-4',
      'x-fe-signals',
      'x-fe-version',
      'x-ddg-journey-id',
      'user-agent',
      'accept-language',
    };
    if (headers.entries.any(
      (e) =>
          !allowed.contains(e.key.toLowerCase()) ||
          e.value.contains(RegExp(r'[\r\n]')),
    )) {
      throw ArgumentError('Unsupported or invalid Duck.ai header');
    }
  }
}

class ChatOptions {
  final String? model, reasoningEffort;
  final List<Message> messages;
  final List<Attachment> files;
  final bool? canUseTools;
  final bool? canShowGreeting;
  final Map<String, dynamic> extra;
  final CancellationToken? cancellationToken;
  const ChatOptions({
    this.model,
    this.reasoningEffort,
    this.messages = const [],
    this.files = const [],
    this.canUseTools,
    this.canShowGreeting,
    this.extra = const {},
    this.cancellationToken,
  });
  ChatOptions merge(ChatOptions? other) => other == null
      ? this
      : ChatOptions(
          model: other.model ?? model,
          reasoningEffort: other.reasoningEffort ?? reasoningEffort,
          canUseTools: other.canUseTools ?? canUseTools,
          canShowGreeting: other.canShowGreeting ?? canShowGreeting,
          messages: messages,
          files: other.files,
          extra: {...extra, ...other.extra},
          cancellationToken: other.cancellationToken ?? cancellationToken,
        );
  ChatOptions withMessages(
    List<Message> value, {
    CancellationToken? cancellationToken,
  }) => ChatOptions(
    model: model,
    reasoningEffort: reasoningEffort,
    messages: value,
    files: files,
    canUseTools: canUseTools,
    canShowGreeting: canShowGreeting,
    extra: extra,
    cancellationToken: cancellationToken ?? this.cancellationToken,
  );
}

class ChatResponse {
  final String model;
  String text = '';
  bool done = false;
  final List<dynamic> _events = [];
  final List<Map<String, dynamic>> _parts = [];
  ChatResponse(this.model);
  List<dynamic> get events => cloneJson<List<dynamic>>(_events);
  Message get assistantMessage => {
    'role': 'assistant',
    'content': text,
    if (_parts.isNotEmpty) 'parts': cloneJson<List<dynamic>>(_parts),
  };
  String append(dynamic event) {
    _events.add(cloneJson<dynamic>(event));
    if (event is! Map) return '';
    if (event['parts'] is List) {
      for (final part in event['parts'] as List) {
        if (part is! Map<String, dynamic>) continue;
        final index = part['id'] == null
            ? -1
            : _parts.indexWhere((p) => p['id'] == part['id']);
        if (index < 0) {
          _parts.add(cloneJson(part));
        } else {
          _parts[index].addAll(cloneJson(part));
        }
      }
    }
    final delta = event['message'] is String ? event['message'] as String : '';
    text += delta;
    return delta;
  }

  @override
  String toString() => text;
}

class Attachment {
  final Uint8List data;
  final String filename;
  final String? mimeType;
  Attachment(Uint8List data, this.filename, {this.mimeType})
    : data = Uint8List.fromList(data);
  static Future<Attachment> fromPath(
    String path, {
    int maxBytes = 20 * 1024 * 1024,
    CancellationToken? cancellationToken,
  }) async {
    if (maxBytes <= 0) throw ArgumentError.value(maxBytes, 'maxBytes');
    final file = await File(path).open();
    try {
      final bytes = BytesBuilder(copy: false);
      while (bytes.length <= maxBytes) {
        cancellationToken?.throwIfCancelled();
        final chunk = await file.read(
          (maxBytes + 1 - bytes.length).clamp(0, 65536),
        );
        if (chunk.isEmpty) break;
        bytes.add(chunk);
      }
      if (bytes.length > maxBytes) {
        throw AttachmentException(413, 'File exceeds upload limit');
      }
      return Attachment(
        bytes.takeBytes(),
        path.replaceAll('\\', '/').split('/').last,
      );
    } finally {
      await file.close();
    }
  }
}
