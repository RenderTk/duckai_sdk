import Foundation

/// Use from SwiftUI's main actor. Networking suspends without blocking UI work.
@MainActor public final class DuckAI {
    public let model: String
    public let settings: Settings
    private let tokens: TokenProvider
    private let transport: DuckAITransport
    private var closed = false
    private var streams: [UUID: WeakStream] = [:]
    public init(model: String = Model.gpt6Luna, settings: Settings = Settings(), tokenProvider: TokenProvider? = nil, transport: DuckAITransport? = nil) throws {
        try settings.validate()
        guard !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw DuckAIError(statusCode: 400, detail: "Model must not be empty") }
        self.model = model; self.settings = settings
        let native = tokenProvider == nil && transport == nil && !settings.headers.keys.contains(where: { ["x-vqd-hash-1", "x-vqd-4"].contains($0.lowercased()) }) ? AnonymousTokenProvider(timeout: settings.tokenGenerationTimeout) : nil
        tokens = tokenProvider ?? native ?? AnonymousTokenProvider(timeout: settings.tokenGenerationTimeout)
        self.transport = transport ?? native.map(WebViewTransport.init(provider:)) ?? URLSessionTransport()
    }

    func check() throws {
        if closed { throw DuckAIError(statusCode: 503, detail: "Duck.ai client is closed") }
    }

    public func close() async {
        guard !closed else { return }; closed = true
        for stream in Array(streams.values) {
            stream.value?.close()
        }; streams.removeAll()
        transport.close(); await tokens.close()
    }

    public func listModels(includeSubscriber: Bool = true) -> [ModelInfo] {
        Catalog.listModels(includeSubscriber: includeSubscriber)
    }

    public func attachmentLimits(model: String? = nil) -> AttachmentLimits {
        AttachmentLimits(model: model ?? self.model, settings: settings)
    }

    public func prepareFiles(_ files: [Attachment], model: String? = nil) throws -> [JSONValue] {
        try check(); let id = model ?? self.model
        let parts = try Attachments.prepare(files, model: id, settings: settings)
        try Attachments.validate(messages: [Message(fields: ["role": .string("user"), "content": .array(parts)])], model: id, settings: settings)
        return parts
    }

    public func stream(_ prompt: String? = nil, options: ChatOptions = ChatOptions()) throws -> ChatStream {
        try check(); let stream = ChatStream(client: self, prompt: prompt, options: options)
        streams[stream.id] = WeakStream(stream); return stream
    }

    public func events(_ prompt: String? = nil, options: ChatOptions = ChatOptions()) throws -> EventStream {
        try EventStream(stream(prompt, options: options))
    }

    public func chat(_ prompt: String? = nil, options: ChatOptions = ChatOptions()) async throws -> ChatResponse {
        let stream = try stream(prompt, options: options); defer { stream.close() }
        for try await _ in stream {}
        guard stream.response.done else { throw CancellationError() }
        return stream.response
    }

    public func conversation(options: ChatOptions = ChatOptions()) throws -> Conversation {
        try check(); return Conversation(client: self, options: options)
    }

    func unregister(_ id: UUID) {
        streams.removeValue(forKey: id)
    }

    func request(_ prompt: String?, _ options: ChatOptions) throws -> JSONValue {
        try check(); try Task.checkCancellation()
        let id = options.model ?? model
        guard !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw DuckAIError(statusCode: 400, detail: "Model must not be empty") }
        var messages = options.messages
        if let prompt { messages.append(Message(role: "user", content: prompt)) }
        guard !messages.isEmpty, messages.allSatisfy({ !($0.fields["role"]?.string ?? "").isEmpty }) else { throw DuckAIError(statusCode: 400, detail: "Provide a prompt or messages with roles") }
        if !options.files.isEmpty {
            let i = messages.count - 1
            guard messages[i].fields["role"]?.string == "user" else { throw AttachmentError(statusCode: 422, detail: "Files attach to the final user message") }
            var content = messages[i].fields["content"]?.array ?? []
            if let text = messages[i].fields["content"]?.string { content = [.object(["type": .string("text"), "text": .string(text)])] }
            content += try Attachments.prepare(options.files, model: id, settings: settings)
            messages[i].fields["content"] = .array(content)
        }
        let info = Catalog.getModel(id); let reasoning = options.reasoningEffort ?? info?.defaultReasoningEffort ?? "none"
        if let info, !info.reasoningEfforts.contains(reasoning) { throw DuckAIError(statusCode: 400, detail: "Unsupported reasoning effort for \(info.name)") }
        try Attachments.validate(messages: messages, model: id, settings: settings)
        var wire = options.extra
        wire["model"] = .string(id); wire["messages"] = .array(messages.map { .object($0.fields) })
        wire["reasoningEffort"] = .string(reasoning); wire["canUseTools"] = .bool(options.canUseTools ?? info?.supportsTools ?? true)
        wire["canShowGreeting"] = .bool(options.canShowGreeting ?? false)
        return .object(wire)
    }

    func open(_ body: JSONValue, networkReady: () -> Void) async throws -> HTTPResponse {
        try check(); try Task.checkCancellation()
        var headers = ["accept": "text/event-stream", "content-type": "application/json", "origin": "https://duck.ai", "referer": "https://duck.ai/", "accept-language": "en-US,en;q=0.6", "dnt": "1", "sec-gpc": "1"]
        for (k, v) in settings.headers {
            headers[k.lowercased()] = v
        }
        if headers["x-vqd-hash-1"] == nil, headers["x-vqd-4"] == nil {
            guard settings.autoToken else { throw DuckAIError(statusCode: 503, detail: "Supply fresh captured headers or enable autoToken") }
            try await headers.merge(tokens.headers(), uniquingKeysWith: { _, new in new })
        }
        try check(); try Task.checkCancellation()
        var request = URLRequest(url: URL(string: "https://duck.ai/duckchat/v1/chat")!)
        request.httpMethod = "POST"; request.allHTTPHeaderFields = headers
        request.httpBody = try JSONEncoder().encode(body); request.timeoutInterval = settings.timeout
        networkReady()
        let response: HTTPResponse
        do { response = try await transport.send(request, maxBytes: settings.maxCollectedBytes) }
        catch { if error is CancellationError || error is DuckAIError { throw error }; throw DuckAIError(detail: "Could not connect to Duck.ai: \(error.localizedDescription)") }
        guard (200 ..< 300).contains(response.statusCode) else {
            defer { response.cancel() }; var data = Data()
            for try await chunk in response.body {
                data.append(chunk.prefix(max(0, 65536 - data.count))); if data.count >= 65536 { break }
            }
            let detail = (try? JSONDecoder().decode(JSONValue.self, from: data)).map { String(describing: $0) } ?? "Duck.ai rejected the request"
            throw upstreamError(response.statusCode, detail, response.headers)
        }
        guard response.headers["content-type"]?.lowercased().contains("text/event-stream") == true else { response.cancel(); throw DuckAIError(detail: "Duck.ai returned a non-SSE response") }
        return response
    }
}

private final class WeakStream { weak var value: ChatStream?; init(_ value: ChatStream) {
    self.value = value
} }

@MainActor public final class ChatStream: AsyncSequence {
    public typealias Element = String
    public struct AsyncIterator: AsyncIteratorProtocol {
        let stream: ChatStream
        let consumer = UUID()
        public mutating func next() async throws -> String? {
            while let event = try await stream.nextEvent(consumer: consumer) {
                let value = event["message"]?.string ?? ""; if !value.isEmpty { return value }
            }
            return nil
        }
    }

    let id = UUID()
    public let response: ChatResponse
    public private(set) var request: JSONValue?
    private let client: DuckAI
    private let prompt: String?
    private let options: ChatOptions
    private var iterator: AsyncThrowingStream<Data, Error>.Iterator?
    private var network: HTTPResponse?
    private var decoder: SSEDecoder
    private var pending: [SSEEvent] = []
    private var closed = false
    private var reading = false
    private var consumer: UUID?
    private var deadline: Date?
    private var opening: Task<HTTPResponse, Error>?
    private var watchdog: Task<Void, Never>?
    private var timedOut = false
    init(client: DuckAI, prompt: String?, options: ChatOptions) {
        self.client = client; self.prompt = prompt; self.options = options
        response = ChatResponse(model: options.model ?? client.model); decoder = SSEDecoder(maxBytes: client.settings.maxCollectedBytes)
    }

    public nonisolated func makeAsyncIterator() -> AsyncIterator {
        AsyncIterator(stream: self)
    }

    public func close() {
        if closed { return }; closed = true; opening?.cancel(); watchdog?.cancel(); network?.cancel(); iterator = nil; client.unregister(id)
    }

    func nextEvent(consumer id: UUID) async throws -> JSONValue? {
        guard consumer == nil || consumer == id else { throw DuckAIError(statusCode: 400, detail: "A stream has one consumer") }
        consumer = id
        if closed { return nil }
        guard !reading else { throw DuckAIError(statusCode: 400, detail: "Only one consumer can read a stream") }
        reading = true; defer { reading = false }
        do {
            try Task.checkCancellation()
            if iterator == nil {
                request = try client.request(prompt, options)
                let body = request!
                let operation = Task { @MainActor in
                    try await client.open(body, networkReady: {
                        self.deadline = Date().addingTimeInterval(self.client.settings.timeout)
                        self.watchdog = Task { @MainActor in
                            do { try await Task.sleep(nanoseconds: UInt64(self.client.settings.timeout * 1_000_000_000)) } catch { return }
                            self.timedOut = true; self.opening?.cancel(); self.network?.cancel()
                        }
                    })
                }
                opening = operation
                network = try await withTaskCancellationHandler(operation: { try await operation.value }, onCancel: { operation.cancel() })
                opening = nil
                if closed { network?.cancel(); return nil }
                iterator = network!.body.makeAsyncIterator()
            }
            while true {
                try Task.checkCancellation()
                if let deadline, Date() > deadline { throw DuckAIError(statusCode: 504, detail: "Duck.ai request timed out") }
                if pending.isEmpty {
                    // Copy the iterator while suspended so close() can run safely.
                    var local = iterator!
                    let chunk = try await local.next()
                    if closed { return nil }; iterator = local
                    pending += try decoder.feed(chunk ?? Data(), eof: chunk == nil)
                    if chunk == nil, pending.isEmpty { throw IncompleteResponseError(response: response) }
                }
                while !pending.isEmpty {
                    let event = pending.removeFirst()
                    if event.done { response.done = true; close(); return nil }
                    guard let data = event.data else { continue }
                    if data["action"]?.string == "error" || ["error", "proxy_error"].contains(event.name) {
                        let status: Int; if case let .number(n) = data["status"], n >= 400, n <= 599 { status = Int(n) } else { status = 502 }
                        throw upstreamError(status, String(describing: data))
                    }
                    _ = response.append(data); return data
                }
            }
        } catch { close(); if timedOut { throw DuckAIError(statusCode: 504, detail: "Duck.ai request timed out") }; throw error }
    }
}

@MainActor public struct EventStream: AsyncSequence {
    public typealias Element = JSONValue
    let stream: ChatStream
    init(_ stream: ChatStream) {
        self.stream = stream
    }

    public var response: ChatResponse {
        stream.response
    }

    public func close() {
        stream.close()
    }

    public struct AsyncIterator: AsyncIteratorProtocol {
        let stream: ChatStream
        let consumer = UUID()
        public mutating func next() async throws -> JSONValue? {
            try await stream.nextEvent(consumer: consumer)
        }
    }

    public nonisolated func makeAsyncIterator() -> AsyncIterator {
        AsyncIterator(stream: stream)
    }
}

@MainActor public final class Conversation {
    private let client: DuckAI
    private let defaults: ChatOptions
    public private(set) var messages: [Message]
    private var busy = false
    private var active = 0
    init(client: DuckAI, options: ChatOptions) {
        self.client = client; defaults = options; messages = options.messages
    }

    public func clear() throws {
        guard active == 0 else { throw DuckAIError(statusCode: 400, detail: "Cannot clear a conversation during a turn") }; messages = []
    }

    public func chat(_ prompt: String, options: ChatOptions? = nil) async throws -> ChatResponse {
        let stream = stream(prompt, options: options)
        for try await _ in stream {}
        guard let response = stream.response, response.done else { throw CancellationError() }
        return response
    }

    public func stream(_ prompt: String, options: ChatOptions? = nil) -> ConversationStream {
        var merged = defaults
        if let options {
            merged.model = options.model ?? defaults.model
            merged.reasoningEffort = options.reasoningEffort ?? defaults.reasoningEffort
            merged.canUseTools = options.canUseTools ?? defaults.canUseTools
            merged.canShowGreeting = options.canShowGreeting ?? defaults.canShowGreeting
            merged.files = options.files
            merged.extra.merge(options.extra, uniquingKeysWith: { _, new in new })
        }
        return ConversationStream(conversation: self, prompt: prompt, options: merged)
    }

    fileprivate func begin(_ prompt: String, _ options: ChatOptions, isClosed: () -> Bool) async throws -> ChatStream {
        while busy {
            if isClosed() { throw CancellationError() }
            try Task.checkCancellation(); try await Task.sleep(nanoseconds: 10_000_000)
        }
        if isClosed() { throw CancellationError() }
        try Task.checkCancellation(); busy = true
        do { var options = options; options.messages = messages; return try client.stream(prompt, options: options) }
        catch { busy = false; throw error }
    }

    fileprivate func started() {
        active += 1
    }

    fileprivate func stopped() {
        active -= 1
    }

    fileprivate func finish(_ stream: ChatStream) {
        if stream.response.done, let request = stream.request { messages = (request["messages"]?.array ?? []).compactMap { $0.object.map(Message.init(fields:)) } + [stream.response.assistantMessage] }
        stream.close(); busy = false
    }
}

@MainActor public final class ConversationStream: AsyncSequence {
    public typealias Element = String
    private let conversation: Conversation
    private let prompt: String
    private let options: ChatOptions
    private var stream: ChatStream?
    private var iterator: ChatStream.AsyncIterator?
    private var closed = false
    private var started = false
    private var reading = false
    private var consumer: UUID?
    public var response: ChatResponse? {
        stream?.response
    }

    fileprivate init(conversation: Conversation, prompt: String, options: ChatOptions) {
        self.conversation = conversation; self.prompt = prompt; self.options = options
    }

    public func close() {
        guard !closed else { return }; closed = true; if let stream { conversation.finish(stream) }; if started { conversation.stopped() }
    }

    public struct AsyncIterator: AsyncIteratorProtocol {
        let owner: ConversationStream
        let consumer = UUID()
        public mutating func next() async throws -> String? {
            try await owner.next(consumer: consumer)
        }
    }

    public nonisolated func makeAsyncIterator() -> AsyncIterator {
        AsyncIterator(owner: self)
    }

    private func next(consumer id: UUID) async throws -> String? {
        guard consumer == nil || consumer == id else { throw DuckAIError(statusCode: 400, detail: "A stream has one consumer") }; consumer = id
        guard !reading else { throw DuckAIError(statusCode: 400, detail: "Only one consumer can read a stream") }
        reading = true; defer { reading = false }
        if closed { return nil }
        do {
            if !started { started = true; conversation.started() }
            if stream == nil { stream = try await conversation.begin(prompt, options, isClosed: { self.closed }); if closed { conversation.finish(stream!); return nil }; iterator = stream!.makeAsyncIterator() }
            var local = iterator!
            let value = try await local.next(); iterator = local
            if value == nil { close() }; return value
        } catch { close(); throw error }
    }
}
