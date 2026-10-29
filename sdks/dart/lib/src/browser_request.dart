import 'dart:async';
import 'transport.dart';
import 'types.dart';

/// Bounds bytes queued between the native bridge and the stream consumer.
class BrowserRequest {
  final ready = Completer<HTTPResponse>();
  final _body = StreamController<List<int>>();
  final void Function(Object? error) cancel;
  final int maxBufferedBytes;
  int _buffered = 0;
  bool _ended = false;

  BrowserRequest(this.cancel, {required this.maxBufferedBytes}) {
    // Cancellation can arrive while the adapter is still loading its script.
    // Keep the original future awaitable without an early unhandled error.
    ready.future.ignore();
    _body.onCancel = () => cancel(null);
  }

  Stream<List<int>> _read() async* {
    await for (final chunk in _body.stream) {
      _buffered -= chunk.length;
      yield chunk;
    }
  }

  void headers(int status, Map<String, String> headers) {
    if (!ready.isCompleted) {
      ready.complete(
        HTTPResponse(status, headers, _read(), cancel: () => cancel(null)),
      );
    }
  }

  bool add(List<int> chunk) {
    if (_ended) return false;
    if (_buffered + chunk.length > maxBufferedBytes) {
      final error = DuckAIException(
        502,
        'Duck.ai browser stream buffer exceeded; consume the stream faster',
      );
      fail(error);
      cancel(error);
      return false;
    }
    _buffered += chunk.length;
    _body.add(chunk);
    return true;
  }

  void finish() {
    if (_ended) return;
    _ended = true;
    _body.close();
  }

  void fail(Object error) {
    if (_ended) return;
    _ended = true;
    if (!ready.isCompleted) {
      ready.completeError(error);
    } else {
      _body.addError(error);
    }
    _body.close();
  }
}
