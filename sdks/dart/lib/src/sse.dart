import 'dart:convert';
import 'types.dart';

class SSEEvent {
  final dynamic data;
  final String name;
  final bool done;
  SSEEvent(this.data, this.name, this.done);
}

Stream<SSEEvent> parseSSE(Stream<List<int>> body, int maxBytes) async* {
  final line = <int>[];
  final lines = <String>[];
  var name = 'message', afterCR = false;
  var received = 0;
  SSEEvent? dispatch() {
    final event = name;
    name = 'message';
    if (lines.isEmpty) return null;
    final text = lines.join('\n');
    lines.clear();
    if (text == '[DONE]') return SSEEvent(null, event, true);
    dynamic data;
    try {
      data = jsonDecode(text);
    } catch (_) {
      data = {'data': text};
    }
    return SSEEvent(data, event, false);
  }

  SSEEvent? consume() {
    final text = utf8.decode(line, allowMalformed: true);
    line.clear();
    if (text.isEmpty) return dispatch();
    if (text.startsWith('data:')) {
      final value = text.substring(5);
      lines.add(value.startsWith(' ') ? value.substring(1) : value);
    } else if (text.startsWith('event:')) {
      final value = text.substring(6);
      name = value.startsWith(' ') ? value.substring(1) : value;
    }
    return null;
  }

  await for (final chunk in body) {
    received += chunk.length;
    if (received > maxBytes) {
      throw DuckAIException(502, 'Duck.ai response exceeds collection limit');
    }
    for (final byte in chunk) {
      if (afterCR) {
        afterCR = false;
        if (byte == 10) continue;
      }
      if (byte == 10 || byte == 13) {
        final event = consume();
        afterCR = byte == 13;
        if (event != null) {
          yield event;
          if (event.done) return;
        }
      } else {
        line.add(byte);
      }
    }
  }
  if (line.isNotEmpty) {
    final event = consume();
    if (event != null) yield event;
  }
  final last = dispatch();
  if (last != null) yield last;
}
