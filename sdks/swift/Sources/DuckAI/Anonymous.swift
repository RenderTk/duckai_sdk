import Foundation
import WebKit
#if os(iOS)
    import UIKit
#endif

@MainActor public protocol TokenProvider: AnyObject {
    func headers() async throws -> [String: String]
    func close() async
}

/// An isolated, nonpersistent native WebKit view; no Chromium or login required.
@MainActor public final class AnonymousTokenProvider: NSObject, TokenProvider, WKNavigationDelegate {
    private var webView: WKWebView?
    private var closed = false
    private var busy = false
    private let journey = UUID().uuidString.replacingOccurrences(of: "-", with: "")
    private let timeout: TimeInterval
    private var navigationError: Error?
    private var pending: [String: BrowserRequest] = [:]
    private var bootstrapAbort: ((Error) -> Void)?
    public init(timeout: TimeInterval = 30) {
        self.timeout = timeout; super.init()
    }

    public func close() async {
        closed = true; bootstrapAbort?(CancellationError()); reset()
    }

    private func reset() {
        for state in pending.values {
            state.fail(CancellationError())
        }; pending.removeAll()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "duckaiStream")
        webView?.stopLoading(); webView?.navigationDelegate = nil
        #if os(iOS)
            webView?.removeFromSuperview()
        #endif
        webView = nil
    }

    public func webView(_: WKWebView, didFail _: WKNavigation!, withError error: Error) {
        navigationError = error
    }

    public func webView(_: WKWebView, didFailProvisionalNavigation _: WKNavigation!, withError error: Error) {
        navigationError = error
    }

    public func webViewWebContentProcessDidTerminate(_: WKWebView) {
        let error = DuckAIError(statusCode: 503, detail: "WebKit process terminated; retry explicitly")
        navigationError = error
        for state in pending.values {
            state.fail(error)
        }; pending.removeAll()
        bootstrapAbort?(error); reset()
    }

    public func webView(_: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        decisionHandler(navigationAction.targetFrame?.isMainFrame != true || (url?.scheme == "https" && url?.host == "duck.ai") ? .allow : .cancel)
    }

    public func headers() async throws -> [String: String] {
        while busy {
            try Task.checkCancellation(); if closed { throw DuckAIError(statusCode: 503, detail: "Token provider is closed") }; try await Task.sleep(nanoseconds: 10_000_000)
        }
        busy = true; defer { busy = false }
        guard !closed else { throw DuckAIError(statusCode: 503, detail: "Token provider is closed") }
        let deadline = Date().addingTimeInterval(timeout)
        if webView == nil {
            navigationError = nil
            let config = WKWebViewConfiguration(); config.websiteDataStore = .nonPersistent()
            config.userContentController.add(StreamBridge(self), name: "duckaiStream")
            let web = WKWebView(frame: CGRect(x: 0, y: 0, width: 800, height: 600), configuration: config)
            #if os(iOS)
                if let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first(where: { $0.activationState == .foregroundActive }),
                   let window = scene.windows.first(where: \.isKeyWindow)
                {
                    // A window host is necessary for reliable iOS WebKit rendering.
                    // Put this noninteractive SDK-owned view behind the app content.
                    web.frame = window.bounds
                    web.isUserInteractionEnabled = false; web.accessibilityElementsHidden = true
                    web.alpha = 0.01
                    window.insertSubview(web, at: 0)
                }
            #endif
            web.navigationDelegate = self; webView = web
            web.load(URLRequest(url: URL(string: "https://duck.ai/")!))
        }
        do {
            guard let web = webView else { throw DuckAIError(statusCode: 503, detail: "WebKit unavailable") }
            while true {
                try Task.checkCancellation()
                if closed { throw DuckAIError(statusCode: 503, detail: "Token provider is closed") }
                if let error = navigationError { throw error }
                if Date() >= deadline { throw DuckAIError(statusCode: 504, detail: "Anonymous session generation timed out") }
                if await (try? web.evaluateJavaScript("location.origin === 'https://duck.ai' && !!document.querySelector('iframe#jsa')?.contentDocument?.body")) as? Bool == true { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            let script = try String(contentsOf: Bundle.module.url(forResource: "browser-bootstrap", withExtension: "js")!, encoding: .utf8)
            // A continuation lets cancellation/timeouts stop the view without waiting
            // for an upstream JavaScript promise that might never resolve.
            let value: [String: Any] = try await withTaskCancellationHandler(operation: {
                try await withCheckedThrowingContinuation { continuation in
                    var completed = false
                    let timer = Task { @MainActor in
                        try? await Task.sleep(nanoseconds: UInt64(max(0, deadline.timeIntervalSinceNow) * 1_000_000_000))
                        guard !completed else { return }; completed = true
                        self.reset(); continuation.resume(throwing: DuckAIError(statusCode: 504, detail: "Anonymous session generation timed out"))
                    }
                    self.bootstrapAbort = { error in
                        guard !completed else { return }; completed = true; timer.cancel()
                        continuation.resume(throwing: error)
                    }
                    web.callAsyncJavaScript(script, arguments: ["journey": journey], in: nil, in: .page) { result in
                        guard !completed else { return }; completed = true; timer.cancel()
                        switch result {
                        case let .success(v):
                            if let object = v as? [String: Any] { continuation.resume(returning: object) }
                            else { continuation.resume(throwing: DuckAIError(detail: "Invalid anonymous bootstrap result")) }
                        case let .failure(error): continuation.resume(throwing: error)
                        }
                    }
                }
            }, onCancel: { Task { @MainActor in self.bootstrapAbort?(CancellationError()); self.reset() } })
            bootstrapAbort = nil
            try Task.checkCancellation()
            guard !closed else { throw DuckAIError(statusCode: 503, detail: "Token provider is closed") }
            if let status = value["status"] as? Int {
                throw upstreamError(status, value["error"] as? String ?? "Bootstrap rejected", ["retry-after": value["retryAfter"] as? String ?? ""])
            }
            guard let headers = value["headers"] as? [String: String], !(headers["x-vqd-hash-1"] ?? "").isEmpty else { throw DuckAIError(detail: "Bootstrap returned no challenge headers") }
            return headers
        } catch {
            reset()
            if error is CancellationError || error is DuckAIError { throw error }
            throw DuckAIError(statusCode: 503, detail: "Native WebKit anonymous bootstrap failed: \(error.localizedDescription)")
        }
    }

    fileprivate func receive(_ message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, message.frameInfo.request.url?.host == "duck.ai",
              let event = message.body as? [String: Any], let id = event["id"] as? String,
              let state = pending[id], let kind = event["kind"] as? String else { return }
        switch kind {
        case "headers":
            guard let status = event["status"] as? Int, let headers = event["headers"] as? [String: String] else { state.fail(DuckAIError(detail: "Invalid browser response")); return }
            state.headers(status, headers)
        case "data":
            guard let value = event["data"] as? String, value.count <= 32768, let data = Data(base64Encoded: value) else { state.fail(DuckAIError(detail: "Invalid browser stream chunk")); return }
            state.body.yield(data)
        case "done": state.body.finish(); pending.removeValue(forKey: id)
        case "error": state.fail(event["error"] as? String == "cancelled" ? CancellationError() : DuckAIError(detail: event["error"] as? String ?? "Browser transport failed")); pending.removeValue(forKey: id)
        default: break
        }
    }

    fileprivate func cancel(_ id: String) {
        pending.removeValue(forKey: id)?.fail(CancellationError())
        let key = String(data: try! JSONEncoder().encode(id), encoding: .utf8)!
        webView?.evaluateJavaScript("window.__duckaiRequests?.[\(key)]?.abort()", completionHandler: nil)
    }

    func send(_ request: URLRequest, maxBytes: Int) async throws -> HTTPResponse {
        try Task.checkCancellation()
        guard !closed, let web = webView else { throw DuckAIError(statusCode: 503, detail: "Browser session is unavailable") }
        let id = UUID().uuidString
        let body = try JSONSerialization.jsonObject(with: request.httpBody ?? Data())
        var headers = request.allHTTPHeaderFields ?? [:]
        // User-Agent/Origin are browser-controlled on fetch; retaining a copied UA
        // here can cause Chrome to reject it. The live context supplies them.
        headers = headers.filter { !["user-agent", "origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"].contains($0.key.lowercased()) }
        let script = try "const emit = event => window.webkit.messageHandlers.duckaiStream.postMessage(event);\n" + String(contentsOf: Bundle.module.url(forResource: "browser-transport", withExtension: "js")!, encoding: .utf8)
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                let state = BrowserRequest(continuation, cancel: { Task { @MainActor in self.cancel(id) } })
                pending[id] = state
                web.callAsyncJavaScript(script, arguments: ["requestId": id, "requestHeaders": headers, "requestBody": body, "maxBytes": maxBytes], in: nil, in: .page) { result in
                    if case let .failure(error) = result { state.fail(DuckAIError(detail: "Browser request failed: \(error.localizedDescription)")); self.pending.removeValue(forKey: id) }
                }
            }
        }, onCancel: { Task { @MainActor in self.cancel(id) } })
    }
}

@MainActor private final class StreamBridge: NSObject, WKScriptMessageHandler {
    weak var owner: AnonymousTokenProvider?
    init(_ owner: AnonymousTokenProvider) {
        self.owner = owner
    }

    func userContentController(_: WKUserContentController, didReceive message: WKScriptMessage) {
        owner?.receive(message)
    }
}

@MainActor private final class BrowserRequest {
    var headerContinuation: CheckedContinuation<HTTPResponse, Error>?
    let stream: AsyncThrowingStream<Data, Error>
    let body: AsyncThrowingStream<Data, Error>.Continuation
    let cancel: () -> Void
    init(_ continuation: CheckedContinuation<HTTPResponse, Error>, cancel: @escaping () -> Void) {
        headerContinuation = continuation; self.cancel = cancel
        var output: AsyncThrowingStream<Data, Error>.Continuation!
        stream = AsyncThrowingStream { output = $0 }; body = output
        body.onTermination = { _ in cancel() }
    }

    func headers(_ status: Int, _ headers: [String: String]) {
        headerContinuation?.resume(returning: HTTPResponse(statusCode: status, headers: headers, body: stream, cancel: cancel)); headerContinuation = nil
    }

    func fail(_ error: Error) {
        headerContinuation?.resume(throwing: error); headerContinuation = nil; body.finish(throwing: error)
    }
}

/// Chat requests stay in the anonymous WebKit context on Apple platforms.
public final class WebViewTransport: DuckAITransport {
    private let provider: AnonymousTokenProvider
    public init(provider: AnonymousTokenProvider) {
        self.provider = provider
    }

    public func send(_ request: URLRequest, maxBytes: Int) async throws -> HTTPResponse {
        try await provider.send(request, maxBytes: maxBytes)
    }

    public func close() {
        Task { @MainActor in await provider.close() }
    }
}
