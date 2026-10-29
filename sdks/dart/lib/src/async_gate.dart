import 'dart:async';
import 'dart:collection';
import 'types.dart';

/// FIFO ownership with immediate cancellation of queued operations.
class AsyncGate {
  final _waiting = Queue<Completer<void Function()>>();
  bool _busy = false;
  Object? _closed;

  Future<void Function()> acquire([CancellationToken? token]) async {
    token?.throwIfCancelled();
    if (_closed != null) throw _closed!;
    if (!_busy) {
      _busy = true;
      return _lease();
    }
    final waiter = Completer<void Function()>();
    _waiting.add(waiter);
    final remove = token?.listen(() {
      if (_waiting.remove(waiter)) waiter.completeError(CancelledException());
    });
    try {
      final release = await waiter.future;
      if (token?.isCancelled == true) {
        release();
        throw CancelledException();
      }
      return release;
    } finally {
      remove?.call();
    }
  }

  void Function() _lease() {
    var released = false;
    return () {
      if (released) return;
      released = true;
      if (_waiting.isEmpty) {
        _busy = false;
      } else {
        _waiting.removeFirst().complete(_lease());
      }
    };
  }

  void close(Object reason) {
    _closed ??= reason;
    while (_waiting.isNotEmpty) {
      _waiting.removeFirst().completeError(_closed!);
    }
  }
}
