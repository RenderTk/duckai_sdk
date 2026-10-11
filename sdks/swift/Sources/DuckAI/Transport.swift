import Foundation

public struct HTTPResponse {
    public let statusCode: Int
    public let headers: [String: String]
    public let body: AsyncThrowingStream<Data, Error>
    public let cancel: () -> Void
    public init(statusCode: Int, headers: [String: String], body: AsyncThrowingStream<Data, Error>, cancel: @escaping () -> Void = {}) {
        self.statusCode = statusCode; self.headers = headers; self.body = body; self.cancel = cancel
    }
}

public protocol DuckAITransport {
    func send(_ request: URLRequest, maxBytes: Int) async throws -> HTTPResponse
    func close()
}

private final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_: URLSession, task _: URLSessionTask, willPerformHTTPRedirection _: HTTPURLResponse, newRequest _: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

/// The stream may terminate on a different executor while its worker is assigned.
private final class TaskCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var task: Task<Void, Never>?
    private var cancelled = false
    func set(_ value: Task<Void, Never>) {
        lock.lock(); task = value; let shouldCancel = cancelled; lock.unlock()
        if shouldCancel { value.cancel() }
    }

    func cancel() {
        lock.lock(); cancelled = true; let value = task; lock.unlock()
        value?.cancel()
    }
}

public final class URLSessionTransport: DuckAITransport {
    private let session: URLSession
    public init() {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil; config.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }

    public func close() {
        session.invalidateAndCancel()
    }

    public func send(_ request: URLRequest, maxBytes: Int) async throws -> HTTPResponse {
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse else { throw DuckAIError(detail: "Invalid HTTP response") }
        var headers: [String: String] = [:]
        for (key, value) in http.allHeaderFields {
            headers[String(describing: key).lowercased()] = String(describing: value)
        }
        let worker = TaskCancellation()
        let body = AsyncThrowingStream<Data, Error> { continuation in
            worker.set(Task {
                do {
                    var chunk = Data(); var count = 0
                    for try await byte in bytes {
                        try Task.checkCancellation(); count += 1
                        guard count <= maxBytes else { throw DuckAIError(detail: "Duck.ai response exceeds collection limit") }
                        chunk.append(byte)
                        if byte == 10 || byte == 13 || chunk.count >= 1024 { continuation.yield(chunk); chunk.removeAll(keepingCapacity: true) }
                    }
                    if !chunk.isEmpty { continuation.yield(chunk) }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            })
            continuation.onTermination = { _ in worker.cancel() }
        }
        return HTTPResponse(statusCode: http.statusCode, headers: headers, body: body, cancel: { worker.cancel() })
    }
}

struct SSEEvent { var data: JSONValue?; var name: String; var done: Bool }
/// Parses bytes before UTF-8 decoding so arbitrary network chunk boundaries are safe.
struct SSEDecoder {
    private var line = Data()
    private var lines: [String] = []
    private var name = "message"
    private var afterCR = false
    private var received = 0
    let maxBytes: Int
    mutating func feed(_ bytes: Data, eof: Bool = false) throws -> [SSEEvent] {
        received += bytes.count
        guard received <= maxBytes else { throw DuckAIError(detail: "Duck.ai response exceeds collection limit") }
        var events: [SSEEvent] = []
        for byte in bytes {
            if afterCR { afterCR = false; if byte == 10 { continue } }
            if byte == 10 || byte == 13 {
                if let event = consumeLine() { events.append(event) }
                afterCR = byte == 13
            } else { line.append(byte) }
        }
        if eof {
            if !line.isEmpty, let event = consumeLine() { events.append(event) }
            if let event = dispatch() { events.append(event) }
        }
        return events
    }

    private mutating func consumeLine() -> SSEEvent? {
        let value = String(decoding: line, as: UTF8.self); line.removeAll(keepingCapacity: true)
        if value.isEmpty { return dispatch() }
        if value.hasPrefix("data:") { var v = String(value.dropFirst(5)); if v.hasPrefix(" ") { v.removeFirst() }; lines.append(v) }
        if value.hasPrefix("event:") { var v = String(value.dropFirst(6)); if v.hasPrefix(" ") { v.removeFirst() }; name = v }
        return nil
    }

    private mutating func dispatch() -> SSEEvent? {
        let eventName = name; name = "message"
        guard !lines.isEmpty else { return nil }
        let value = lines.joined(separator: "\n"); lines.removeAll(keepingCapacity: true)
        if value == "[DONE]" { return SSEEvent(data: nil, name: eventName, done: true) }
        let data = (try? JSONDecoder().decode(JSONValue.self, from: Data(value.utf8))) ?? .object(["data": .string(value)])
        return SSEEvent(data: data, name: eventName, done: false)
    }
}
