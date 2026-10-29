import 'dart:isolate';
import 'async_gate.dart';
import 'types.dart';

final _workers = AsyncGate();

/// Callbacks must be static/top-level; jobs contain only isolate-sendable data.
Future<T> runAttachmentWorker<T>(
  T Function(dynamic) callback,
  dynamic job,
  CancellationToken? token,
) async {
  final release = await _workers.acquire(token);
  final replies = ReceivePort();
  Isolate? worker;
  try {
    token?.throwIfCancelled();
    worker = await Isolate.spawn(
      _entry,
      (replies.sendPort, callback, job),
      onError: replies.sendPort,
      onExit: replies.sendPort,
      errorsAreFatal: true,
      debugName: 'duckai-attachment',
    );
    final reply = await (token?.bind(replies.first) ?? replies.first);
    if (reply is _Reply) {
      if (reply.error != null) {
        Error.throwWithStackTrace(
          reply.error!,
          StackTrace.fromString(reply.stack!),
        );
      }
      return reply.value as T;
    }
    throw StateError('Attachment worker exited unexpectedly: $reply');
  } finally {
    worker?.kill(priority: Isolate.immediate);
    replies.close();
    release();
  }
}

class _Reply {
  final dynamic value;
  final Object? error;
  final String? stack;
  _Reply(this.value, [this.error, this.stack]);
}

void _entry((SendPort, dynamic Function(dynamic), dynamic) input) {
  final (port, callback, job) = input;
  try {
    Isolate.exit(port, _Reply(callback(job)));
  } catch (error, stack) {
    Isolate.exit(port, _Reply(null, error, stack.toString()));
  }
}
