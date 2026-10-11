@testable import DuckAI
import ImageIO
import XCTest

private func fixture(_ name: String) throws -> Data {
    try Data(contentsOf: Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")!)
}

@MainActor private final class Tokens: TokenProvider {
    var calls = 0; var closed = false
    func headers() async throws -> [String: String] {
        calls += 1; return ["x-vqd-hash-1": "fixture"]
    }

    func close() async {
        closed = true
    }
}

private final class Transport: DuckAITransport {
    var requests: [URLRequest] = []
    var data: Data; var status = 200; var contentType = "text/event-stream"
    init(_ data: Data) {
        self.data = data
    }

    func send(_ request: URLRequest, maxBytes _: Int) async throws -> HTTPResponse {
        requests.append(request)
        let bytes = data
        return HTTPResponse(statusCode: status, headers: ["content-type": contentType, "retry-after": "7"], body: AsyncThrowingStream { c in
            for byte in bytes {
                c.yield(Data([byte]))
            }; c.finish()
        })
    }

    func close() {}
}

private final class WaitingTransport: DuckAITransport {
    func send(_: URLRequest, maxBytes _: Int) async throws -> HTTPResponse {
        try await Task.sleep(nanoseconds: 10_000_000_000)
        throw DuckAIError(detail: "Unexpected timeout test completion")
    }

    func close() {}
}

private final class HangingBodyTransport: DuckAITransport {
    func send(_: URLRequest, maxBytes _: Int) async throws -> HTTPResponse {
        var output: AsyncThrowingStream<Data, Error>.Continuation!
        let body = AsyncThrowingStream<Data, Error> { output = $0; $0.yield(Data("data: {\"message\":\"partial\"}\n\n".utf8)) }
        let continuation = output!
        return HTTPResponse(statusCode: 200, headers: ["content-type": "text/event-stream"], body: body, cancel: { continuation.finish(throwing: CancellationError()) })
    }

    func close() {}
}

final class SDKTests: XCTestCase {
    @MainActor func testContractAndConversation() async throws {
        let f = try JSONDecoder().decode(JSONValue.self, from: fixture("chat"))
        let transport = try Transport(Data(XCTUnwrap(f["sse"]?.string?.utf8))); let tokens = Tokens()
        let ai = try DuckAI(model: XCTUnwrap(f["model"]?.string), tokenProvider: tokens, transport: transport)
        let response = try await ai.chat(XCTUnwrap(f["prompt"]?.string))
        XCTAssertEqual(response.text, f["text"]?.string); XCTAssertTrue(response.done)
        XCTAssertEqual(response.assistantMessage.fields["parts"], f["parts"])
        let request = try JSONDecoder().decode(JSONValue.self, from: XCTUnwrap(transport.requests[0].httpBody))
        XCTAssertEqual(request, f["request"]); XCTAssertEqual(tokens.calls, 1)
        let chat = try ai.conversation(); _ = try await chat.chat("One"); _ = try await chat.chat("Two")
        XCTAssertEqual(chat.messages.count, 4)
        transport.data = Data("data: {\"message\":\"partial\"}\n\n".utf8)
        do { _ = try await chat.chat("failed"); XCTFail("Incomplete response accepted") }
        catch let error as IncompleteResponseError { XCTAssertEqual(error.response.text, "partial") }
        XCTAssertEqual(chat.messages.count, 4)
        await ai.close(); XCTAssertTrue(tokens.closed)
    }

    @MainActor func testErrorsAndLimits() async throws {
        let transport = Transport(Data("{}".utf8)); transport.status = 429
        let ai = try DuckAI(tokenProvider: Tokens(), transport: transport)
        do { _ = try await ai.chat("test"); XCTFail() } catch let e as RateLimitError { XCTAssertEqual(e.retryAfter, "7") }
        transport.status = 200; transport.data = Data("event: proxy_error\ndata: \"failure\"\n\n".utf8)
        do { _ = try await ai.chat("error"); XCTFail("Named SSE error was ignored") } catch let error as DuckAIError { XCTAssertEqual(error.statusCode, 502) }
        await ai.close()
        var decoder = SSEDecoder(maxBytes: 3)
        XCTAssertThrowsError(try decoder.feed(Data("data: never terminated".utf8)))
    }

    @MainActor func testConversationDefaultsAndHeaderTimeout() async throws {
        let transport = Transport(Data("data: [DONE]\n\n".utf8))
        let ai = try DuckAI(tokenProvider: Tokens(), transport: transport)
        var defaults = ChatOptions(); defaults.model = Model.mistralSmall4; defaults.canShowGreeting = true
        defaults.extra = ["durableStream": .bool(true)]
        let chat = try ai.conversation(options: defaults)
        var turn = ChatOptions(); turn.canUseTools = false
        _ = try await chat.chat("hello", options: turn)
        let body = try JSONDecoder().decode(JSONValue.self, from: XCTUnwrap(transport.requests[0].httpBody))
        XCTAssertEqual(body["model"], .string(Model.mistralSmall4))
        XCTAssertEqual(body["canShowGreeting"], .bool(true)); XCTAssertEqual(body["durableStream"], .bool(true))
        await ai.close()
        var settings = Settings(); settings.timeout = 0.02
        let slow = try DuckAI(settings: settings, tokenProvider: Tokens(), transport: WaitingTransport())
        do { _ = try await slow.chat("test"); XCTFail("Header timeout ignored") }
        catch let error as DuckAIError { XCTAssertEqual(error.statusCode, 504) }
        let task = Task { try await slow.chat("cancel") }; task.cancel()
        do { _ = try await task.value; XCTFail("Cancellation ignored") } catch is CancellationError {}
        await slow.close()
    }

    @MainActor func testSingleConsumerAndQueuedClose() async throws {
        let ai = try DuckAI(tokenProvider: Tokens(), transport: HangingBodyTransport())
        let conversation = try ai.conversation()
        let first = conversation.stream("first"); var iterator = first.makeAsyncIterator()
        let delta = try await iterator.next(); XCTAssertEqual(delta, "partial")
        let second = conversation.stream("second")
        let queued = Task { var iterator = second.makeAsyncIterator(); return try await iterator.next() }
        try await Task.sleep(nanoseconds: 20_000_000)
        second.close()
        do { _ = try await queued.value; XCTFail("Queued close was ignored") } catch is CancellationError {}
        XCTAssertThrowsError(try conversation.clear())
        var duplicate = first.makeAsyncIterator()
        do { _ = try await duplicate.next(); XCTFail("A second consumer was allowed") } catch let error as DuckAIError { XCTAssertEqual(error.statusCode, 400) }
        first.close(); try conversation.clear(); XCTAssertEqual(conversation.messages.count, 0)
        let collecting = Task { try await ai.chat("close client") }
        try await Task.sleep(nanoseconds: 20_000_000); await ai.close()
        do { _ = try await collecting.value; XCTFail("Closing client returned a completed response") } catch is CancellationError {}
    }

    func testSSEBoundaries() throws {
        let f = try JSONDecoder().decode(JSONValue.self, from: fixture("chat")); let bytes = try Array(XCTUnwrap(f["sse"]?.string?.utf8))
        for width in 1 ... 17 {
            var decoder = SSEDecoder(maxBytes: 4096); var events: [SSEEvent] = []
            for offset in stride(from: 0, to: bytes.count, by: width) {
                events += try decoder.feed(Data(bytes[offset ..< min(offset + width, bytes.count)]))
            }
            events += try decoder.feed(Data(), eof: true)
            XCTAssertTrue(try XCTUnwrap(events.last?.done)); XCTAssertEqual(events.compactMap { $0.data?["message"]?.string }.joined(), f["text"]?.string)
        }
    }

    @MainActor func testOrientedImagesAndXMLBounds() throws {
        let ai = try DuckAI(tokenProvider: Tokens(), transport: Transport(Data()))
        for f in try XCTUnwrap(try JSONDecoder().decode(JSONValue.self, from: fixture("images")).array) {
            let attachment = try Attachment(data: XCTUnwrap(try Data(base64Encoded: XCTUnwrap(f["data"]?.string))), filename: XCTUnwrap(f["filename"]?.string))
            let part = try ai.prepareFiles([attachment])[0]
            let bytes = try XCTUnwrap(try Data(base64Encoded: String(XCTUnwrap(part["image"]?.string?.split(separator: ",").last))))
            let source = CGImageSourceCreateWithData(bytes as CFData, nil)!
            let info = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil)) as NSDictionary
            XCTAssertEqual(info[kCGImagePropertyPixelWidth] as? Int, 12)
            XCTAssertEqual(info[kCGImagePropertyPixelHeight] as? Int, 28)
        }
        let deep = String(repeating: "<a>", count: 140) + "hello" + String(repeating: "</a>", count: 140)
        XCTAssertThrowsError(try ai.prepareFiles([Attachment(data: Data(deep.utf8), filename: "deep.xml")]))
        XCTAssertTrue(ai.attachmentLimits().supportedFileExtensions.contains("pdf"))
    }

    @MainActor func testNativePDFPreparation() throws {
        let ai = try DuckAI(tokenProvider: Tokens(), transport: Transport(Data()))
        for f in try XCTUnwrap(try JSONDecoder().decode(JSONValue.self, from: fixture("pdfs")).array) {
            let part = try ai.prepareFiles([Attachment(data: XCTUnwrap(try Data(base64Encoded: XCTUnwrap(f["data"]?.string))), filename: XCTUnwrap(f["filename"]?.string))])[0]
            XCTAssertEqual(part["mimeType"], .string("application/pdf"))
        }
    }

    @MainActor func testDocuments() throws {
        let values = try XCTUnwrap(try JSONDecoder().decode(JSONValue.self, from: fixture("documents")).array)
        let ai = try DuckAI(model: Model.mistralSmall4, tokenProvider: Tokens(), transport: Transport(Data()))
        for f in values {
            let a = try Attachment(data: XCTUnwrap(try Data(base64Encoded: XCTUnwrap(f["data"]?.string))), filename: XCTUnwrap(f["filename"]?.string))
            let text = try XCTUnwrap(try ai.prepareFiles([a])[0]["text"]?.string)
            for expected in try XCTUnwrap(f["contains"]?.array) {
                XCTAssertTrue(try text.contains(XCTUnwrap(expected.string)), "\(a.filename) missing \(expected.string!)\n\(text)")
            }
        }
    }
}
