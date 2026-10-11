import Foundation

/// Lossless JSON storage, including unknown upstream message and part fields.
public enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue]), array([JSONValue]), string(String), number(Double), bool(Bool), null
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = try .object(c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case let .object(v): try c.encode(v)
        case let .array(v): try c.encode(v)
        case let .string(v): try c.encode(v)
        case let .number(v): try c.encode(v)
        case let .bool(v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    public subscript(_ key: String) -> JSONValue? {
        if case let .object(o) = self { return o[key] }; return nil
    }

    public var string: String? {
        if case let .string(s) = self { return s }; return nil
    }

    public var array: [JSONValue]? {
        if case let .array(a) = self { return a }; return nil
    }

    public var object: [String: JSONValue]? {
        if case let .object(o) = self { return o }; return nil
    }
}

public struct Message: Codable, Equatable, Sendable {
    public var fields: [String: JSONValue]
    public init(role: String, content: String) {
        fields = ["role": .string(role), "content": .string(content)]
    }

    public init(fields: [String: JSONValue]) {
        self.fields = fields
    }

    public init(from decoder: Decoder) throws {
        fields = try [String: JSONValue](from: decoder)
    }

    public func encode(to encoder: Encoder) throws {
        try fields.encode(to: encoder)
    }
}

public struct Settings: Sendable {
    public var headers: [String: String] = [:]
    public var autoToken = true
    public var timeout: TimeInterval = 120
    public var tokenGenerationTimeout: TimeInterval = 30
    public var maxCollectedBytes = 8 * 1024 * 1024
    public var maxDocumentUploadBytes = 20 * 1024 * 1024
    public var maxDocumentTextCharacters = 16000
    public var maxImageUploadBytes = 10 * 1024 * 1024
    public var maxImagePixels = 20_000_000
    public init() {}
    func validate() throws {
        guard timeout.isFinite, timeout > 0, timeout < Double(UInt64.max) / 1_000_000_000,
              tokenGenerationTimeout.isFinite, tokenGenerationTimeout > 0, tokenGenerationTimeout < Double(UInt64.max) / 1_000_000_000,
              maxCollectedBytes > 0, maxDocumentUploadBytes > 0, maxImageUploadBytes > 0,
              maxImagePixels > 0, (1 ... 16000).contains(maxDocumentTextCharacters)
        else {
            throw DuckAIError(statusCode: 400, detail: "Invalid positive limits or timeouts")
        }
        let allowed: Set<String> = ["x-vqd-hash-1", "x-vqd-4", "x-fe-signals", "x-fe-version", "x-ddg-journey-id", "user-agent", "accept-language"]
        guard headers.allSatisfy({ allowed.contains($0.key.lowercased()) && !$0.value.contains("\r") && !$0.value.contains("\n") }) else {
            throw DuckAIError(statusCode: 400, detail: "Unsupported or invalid Duck.ai header")
        }
    }
}

public struct ChatOptions: Sendable {
    public var model: String?
    public var messages: [Message] = []
    public var files: [Attachment] = []
    public var reasoningEffort: String?
    public var canUseTools: Bool?
    public var canShowGreeting: Bool?
    /// Extra wire options, such as canUseApproxLocation and durableStream.
    public var extra: [String: JSONValue] = [:]
    public init() {}
}

public class DuckAIError: Error, CustomStringConvertible {
    public let statusCode: Int
    public let detail: String
    public let headers: [String: String]
    public var retryAfter: String? {
        headers["retry-after"]
    }

    public var description: String {
        "DuckAIError(\(statusCode)): \(detail)"
    }

    public init(statusCode: Int = 502, detail: String, headers: [String: String] = [:]) {
        self.statusCode = statusCode; self.detail = detail; self.headers = headers
    }
}

public final class RateLimitError: DuckAIError {}
public final class ChallengeError: DuckAIError {}
public final class AttachmentError: DuckAIError {}
public final class IncompleteResponseError: DuckAIError {
    public let response: ChatResponse
    public init(response: ChatResponse) {
        self.response = response; super.init(detail: "Duck.ai stream ended before [DONE]")
    }
}

func upstreamError(_ status: Int, _ detail: String, _ headers: [String: String] = [:]) -> DuckAIError {
    if status == 429 { return RateLimitError(statusCode: status, detail: detail, headers: headers) }
    if [401, 403, 418].contains(status) { return ChallengeError(statusCode: status, detail: detail, headers: headers) }
    return DuckAIError(statusCode: status, detail: detail, headers: headers)
}

public final class ChatResponse {
    public let model: String
    public internal(set) var text = ""
    public internal(set) var events: [JSONValue] = []
    public internal(set) var done = false
    private var parts: [[String: JSONValue]] = []
    public init(model: String) {
        self.model = model
    }

    public var assistantMessage: Message {
        var fields: [String: JSONValue] = ["role": .string("assistant"), "content": .string(text)]
        if !parts.isEmpty { fields["parts"] = .array(parts.map(JSONValue.object)) }
        return Message(fields: fields)
    }

    func append(_ event: JSONValue) -> String {
        events.append(event)
        for value in event["parts"]?.array ?? [] {
            guard let part = value.object else { continue }
            if let id = part["id"], id != .null, let i = parts.firstIndex(where: { $0["id"] == id }) { parts[i].merge(part, uniquingKeysWith: { _, new in new }) }
            else { parts.append(part) }
        }
        let delta = event["message"]?.string ?? ""; text += delta; return delta
    }
}

public struct ModelInfo: Decodable, Sendable {
    public let id, name, provider, description, accessTier: String
    public let reasoningEfforts: [String]
    public let supportsImages, supportsPDF, supportsTools, beta: Bool
    public let attachmentProfile: AttachmentProfile?
    public var defaultReasoningEffort: String {
        reasoningEfforts.first ?? "none"
    }

    enum CodingKeys: String, CodingKey {
        case id, name, provider, description, beta
        case accessTier = "access_tier", reasoningEfforts = "reasoning_efforts"
        case supportsImages = "supports_images", supportsPDF = "supports_pdf", supportsTools = "supports_tools", attachmentProfile = "attachment_profile"
    }
}

public struct AttachmentProfile: Decodable, Sendable {
    public let maxDimension, imagesPerMessage, imagesPerConversation: Int
    public let supportsPDF: Bool
    enum CodingKeys: String, CodingKey {
        case maxDimension = "max_dimension", imagesPerMessage = "images_per_message", imagesPerConversation = "images_per_conversation", supportsPDF = "supports_pdf"
    }
}

public enum Catalog {
    public static let models: [ModelInfo] = {
        guard let url = Bundle.module.url(forResource: "models", withExtension: "json"),
              let data = try? Data(contentsOf: url), let models = try? JSONDecoder().decode([ModelInfo].self, from: data) else { preconditionFailure("Missing SDK model catalogue") }
        return models
    }()

    public static func listModels(includeSubscriber: Bool = true) -> [ModelInfo] {
        models.filter { includeSubscriber || $0.accessTier == "free" }
    }

    public static func getModel(_ id: String) -> ModelInfo? {
        models.first { $0.id == id }
    }
}
