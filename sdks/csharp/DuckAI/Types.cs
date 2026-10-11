using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace DuckAI;

public class DuckAIException(int statusCode, string detail, IReadOnlyDictionary<string, string>? headers = null) : Exception(detail)
{
    public int StatusCode { get; } = statusCode;
    public IReadOnlyDictionary<string, string> Headers { get; } = headers ?? new Dictionary<string, string>();
    public string? RetryAfter => Headers.GetValueOrDefault("retry-after");
    internal static DuckAIException Upstream(int status, string detail, IReadOnlyDictionary<string, string>? headers = null) => status switch
    {
        429 => new RateLimitException(status, detail, headers),
        401 or 403 or 418 => new ChallengeException(status, detail, headers),
        _ => new DuckAIException(status, detail, headers)
    };
}
public sealed class RateLimitException(int status, string detail, IReadOnlyDictionary<string, string>? headers = null) : DuckAIException(status, detail, headers);
public sealed class ChallengeException(int status, string detail, IReadOnlyDictionary<string, string>? headers = null) : DuckAIException(status, detail, headers);
public sealed class AttachmentException(int status, string detail) : DuckAIException(status, detail);
public sealed class IncompleteResponseException(ChatResponse response) : DuckAIException(502, "Duck.ai stream ended before [DONE]") { public ChatResponse Response { get; } = response; }

public sealed record Settings
{
    public IReadOnlyDictionary<string, string> Headers { get; init; } = new Dictionary<string, string>();
    public bool AutoToken { get; init; } = true;
    public bool Headless { get; init; } = true;
    public bool BrowserAutoInstall { get; init; } = true;
    public string? BrowserExecutable { get; init; }
    public TimeSpan Timeout { get; init; } = TimeSpan.FromSeconds(120);
    public TimeSpan TokenGenerationTimeout { get; init; } = TimeSpan.FromSeconds(30);
    public int MaxCollectedBytes { get; init; } = 8 * 1024 * 1024;
    public int MaxDocumentUploadBytes { get; init; } = 20 * 1024 * 1024;
    public int MaxDocumentTextCharacters { get; init; } = 16_000;
    public int MaxImageUploadBytes { get; init; } = 10 * 1024 * 1024;
    public int MaxImagePixels { get; init; } = 20_000_000;
    internal void Validate()
    {
        if (Timeout <= TimeSpan.Zero || TokenGenerationTimeout <= TimeSpan.Zero || Timeout.TotalMilliseconds > uint.MaxValue - 1 || TokenGenerationTimeout.TotalMilliseconds > uint.MaxValue - 1 || MaxCollectedBytes <= 0 || MaxDocumentUploadBytes <= 0 || MaxImageUploadBytes <= 0 || MaxImagePixels <= 0 || MaxDocumentTextCharacters is < 1 or > 16000) throw new ArgumentException("Limits and timeouts must be positive; document text limit cannot exceed 16,000");
        var allowed = new HashSet<string>(["x-vqd-hash-1", "x-vqd-4", "x-fe-signals", "x-fe-version", "x-ddg-journey-id", "user-agent", "accept-language"], StringComparer.OrdinalIgnoreCase);
        if (Headers.Any(h => !allowed.Contains(h.Key) || h.Value.Contains('\r') || h.Value.Contains('\n'))) throw new ArgumentException("Unsupported or invalid Duck.ai header");
    }
}
public sealed record ChatOptions
{
    public string? Model { get; init; }
    public IReadOnlyList<JsonObject> Messages { get; init; } = [];
    public IReadOnlyList<Attachment> Files { get; init; } = [];
    public string? ReasoningEffort { get; init; }
    public bool? CanUseTools { get; init; }
    public bool? CanShowGreeting { get; init; }
    public JsonObject Extra { get; init; } = [];
}
public static class Message { public static JsonObject Text(string role, string content) => new() { ["role"] = role, ["content"] = content }; }
public sealed class ChatResponse(string model)
{
    public string Model { get; } = model;
    public string Text { get; internal set; } = "";
    public bool Done { get; internal set; }
    private readonly List<JsonNode?> events = [];
    private readonly List<JsonObject> parts = [];
    public IReadOnlyList<JsonNode?> Events => events.Select(e => e?.DeepClone()).ToList();
    public JsonObject AssistantMessage
    {
        get { var result = Message.Text("assistant", Text); if (parts.Count > 0) result["parts"] = new JsonArray(parts.Select(p => (JsonNode)p.DeepClone()).ToArray()); return result; }
    }
    internal string Append(JsonNode? value)
    {
        events.Add(value?.DeepClone());
        if (value is not JsonObject obj) return "";
        if (obj["parts"] is JsonArray a) foreach (var part in a.OfType<JsonObject>())
        {
            var id = part["id"];
            var existing = id is null ? null : parts.Find(p => JsonNode.DeepEquals(p["id"], id));
            if (existing is null) parts.Add((JsonObject)part.DeepClone()); else foreach (var field in part) existing[field.Key] = field.Value?.DeepClone();
        }
        var delta = obj["message"] is JsonValue v && v.TryGetValue<string>(out var text) ? text : "";
        Text += delta; return delta;
    }
    public override string ToString() => Text;
}
public sealed record Attachment(byte[] Data, string Filename, string? MimeType = null)
{
    public static async Task<Attachment> FromPathAsync(string path, int maxBytes = 20 * 1024 * 1024, CancellationToken cancellationToken = default)
    {
        if (maxBytes <= 0) throw new ArgumentOutOfRangeException(nameof(maxBytes));
        await using var file = File.OpenRead(path); using var output = new MemoryStream(); var buffer = new byte[65536];
        while (true) { var count = await file.ReadAsync(buffer.AsMemory(0, Math.Min(buffer.Length, maxBytes + 1 - (int)output.Length)), cancellationToken); if (count == 0) break; output.Write(buffer, 0, count); if (output.Length > maxBytes) throw new AttachmentException(413, "File exceeds upload limit"); }
        return new Attachment(output.ToArray(), Path.GetFileName(path));
    }
}
public sealed record ModelInfo(
    string Id, string Name, string Provider, string Description,
    [property: JsonPropertyName("access_tier")] string AccessTier,
    [property: JsonPropertyName("reasoning_efforts")] string[] ReasoningEfforts,
    [property: JsonPropertyName("supports_images")] bool SupportsImages,
    [property: JsonPropertyName("supports_pdf")] bool SupportsPdf,
    [property: JsonPropertyName("supports_tools")] bool SupportsTools,
    bool Beta,
    [property: JsonPropertyName("attachment_profile")] AttachmentProfile? AttachmentProfile)
{ public string DefaultReasoningEffort => ReasoningEfforts.FirstOrDefault() ?? "none"; }
public sealed record AttachmentProfile(
    [property: JsonPropertyName("max_dimension")] int MaxDimension,
    [property: JsonPropertyName("images_per_message")] int ImagesPerMessage,
    [property: JsonPropertyName("images_per_conversation")] int ImagesPerConversation,
    [property: JsonPropertyName("supports_pdf")] bool SupportsPdf);
public static class Catalog
{
    internal static string Resource(string name)
    {
        var asm = typeof(Catalog).Assembly;
        using var stream = asm.GetManifestResourceStream($"DuckAI.Resources.{name}") ?? throw new InvalidOperationException($"Missing SDK resource: {name}");
        using var reader = new StreamReader(stream); return reader.ReadToEnd();
    }
    private static readonly ModelInfo[] models = JsonSerializer.Deserialize<ModelInfo[]>(Resource("models.json"), new JsonSerializerOptions { PropertyNameCaseInsensitive = true })!;
    public static IReadOnlyList<ModelInfo> ListModels(bool includeSubscriber = true) => models.Where(m => includeSubscriber || m.AccessTier == "free").Select(m => m with { ReasoningEfforts = (string[])m.ReasoningEfforts.Clone() }).ToArray();
    public static ModelInfo? GetModel(string id) => ListModels().FirstOrDefault(m => m.Id == id);
}
