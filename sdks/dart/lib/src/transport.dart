import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'types.dart';

class HTTPResponse {
  final int statusCode;
  final Map<String, String> headers;
  final Stream<List<int>> body;
  final void Function() cancel;
  HTTPResponse(
    this.statusCode,
    this.headers,
    this.body, {
    void Function()? cancel,
  }) : cancel = cancel ?? (() {});
}

abstract interface class DuckAITransport {
  Future<HTTPResponse> send(
    Uri url,
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken cancellationToken,
  );
  void close();
}

class IOTransport implements DuckAITransport {
  final HttpClient _http = HttpClient();
  @override
  void close() {
    _http.close(force: true);
  }

  @override
  Future<HTTPResponse> send(
    Uri url,
    Map<String, String> headers,
    Map<String, dynamic> body,
    CancellationToken cancellationToken,
  ) async {
    cancellationToken.throwIfCancelled();
    final request = await _http.postUrl(url);
    request.followRedirects = false;
    final remove = cancellationToken.listen(() {
      request.abort(CancelledException());
    });
    try {
      cancellationToken.throwIfCancelled();
      headers.forEach(request.headers.set);
      request.add(utf8.encode(jsonEncode(body)));
      final response = await request.close();
      final values = <String, String>{};
      response.headers.forEach((k, v) {
        values[k.toLowerCase()] = v.join(',');
      });
      late StreamSubscription<List<int>> subscription;
      final controller = StreamController<List<int>>();
      controller.onListen = () {
        subscription = response.listen(
          controller.add,
          onError: controller.addError,
          onDone: () {
            remove();
            controller.close();
          },
        );
      };
      controller.onPause = () {
        subscription.pause();
      };
      controller.onResume = () {
        subscription.resume();
      };
      controller.onCancel = () async {
        remove();
        request.abort();
        await subscription.cancel();
      };
      return HTTPResponse(
        response.statusCode,
        values,
        controller.stream,
        cancel: () {
          request.abort();
        },
      );
    } catch (_) {
      remove();
      rethrow;
    }
  }
}
