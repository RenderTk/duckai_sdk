using System.Collections.Concurrent;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json.Nodes;

namespace DuckAI;

public sealed class DuckAIClient : IAsyncDisposable
{
    public string Model { get; }
    public Settings Settings { get; }
    private readonly HttpClient http;
    private readonly bool ownsHttp;
    private readonly ITokenProvider tokens;
    private bool closed;
    private readonly ConcurrentDictionary<Guid, ChatStream> streams = new();
    public DuckAIClient(string model = DuckAI.Model.GPT_6_LUNA, Settings? settings = null, ITokenProvider? tokenProvider = null, HttpClient? httpClient = null)
    {
        if (string.IsNullOrWhiteSpace(model)) throw new ArgumentException("Model must not be empty");
        Model = model; Settings = settings ?? new(); Settings.Validate();
        ownsHttp = httpClient is null; http = httpClient ?? new HttpClient(new HttpClientHandler { AllowAutoRedirect = false, UseCookies = false }) { Timeout = System.Threading.Timeout.InfiniteTimeSpan };
        tokens = tokenProvider ?? new AnonymousTokenProvider(Settings);
    }
    internal void Check() => ObjectDisposedException.ThrowIf(closed, this);
    public IReadOnlyList<ModelInfo> ListModels(bool includeSubscriber = true) => Catalog.ListModels(includeSubscriber);
    public AttachmentLimits AttachmentLimits(string? model = null) => Attachments.Limits(model ?? Model, Settings);
    public IReadOnlyList<JsonObject> PrepareFiles(IEnumerable<Attachment> files, string? model = null) { Check(); var id = model ?? Model; var parts = Attachments.Prepare(files, id, Settings); Attachments.Validate([new() { ["role"] = "user", ["content"] = new JsonArray(parts.Select(p => p.DeepClone()).ToArray()) }], id, Settings); return parts; }
    public ChatStream Stream(string? prompt = null, ChatOptions? options = null) { Check(); var stream = new ChatStream(this, prompt, options ?? new()); streams[stream.Id] = stream; return stream; }
    public ChatStream Events(string? prompt = null, ChatOptions? options = null) => Stream(prompt, options);
    public async Task<ChatResponse> ChatAsync(string? prompt = null, ChatOptions? options = null, CancellationToken cancellationToken = default) { await using var stream = Stream(prompt, options); await foreach (var _ in stream.WithCancellation(cancellationToken)) { } return stream.Response; }
    public Conversation Conversation(ChatOptions? options = null) { Check(); return new(this, options ?? new()); }
    internal void Unregister(Guid id) => streams.TryRemove(id, out _);
    internal JsonObject Request(string? prompt, ChatOptions options)
    {
        Check(); var model = options.Model ?? Model; if (string.IsNullOrWhiteSpace(model)) throw new ArgumentException("Model must not be empty");
        var messages = options.Messages.Select(m => (JsonObject)m.DeepClone()).ToList();
        if (prompt is not null) messages.Add(Message.Text("user", prompt));
        if (messages.Count == 0 || messages.Any(m => string.IsNullOrEmpty(m["role"]?.GetValue<string>()))) throw new ArgumentException("Provide a prompt or messages with roles");
        if (options.Files.Count > 0) { var last = messages[^1]; if (last["role"]?.GetValue<string>() != "user") throw new ArgumentException("Files attach to the final user message"); var content = last["content"] switch { JsonArray a => (JsonArray)a.DeepClone(), JsonValue v when v.TryGetValue<string>(out var text) => new JsonArray(new JsonObject { ["type"] = "text", ["text"] = text }), _ => new JsonArray() }; foreach (var p in Attachments.Prepare(options.Files, model, Settings)) content.Add(p); last["content"] = content; }
        var info = Catalog.GetModel(model); var reasoning = options.ReasoningEffort ?? info?.DefaultReasoningEffort ?? "none";
        if (info is not null && !info.ReasoningEfforts.Contains(reasoning)) throw new ArgumentException("Unsupported reasoning effort for " + info.Name);
        Attachments.Validate(messages, model, Settings);
        var body = (JsonObject)options.Extra.DeepClone(); body["model"] = model; body["messages"] = new JsonArray(messages.Select(m => (JsonNode)m).ToArray()); body["reasoningEffort"] = reasoning; body["canUseTools"] = options.CanUseTools ?? info?.SupportsTools ?? true; body["canShowGreeting"] = options.CanShowGreeting ?? false; return body;
    }
    internal async Task<HttpResponseMessage> OpenAsync(JsonObject body, CancellationToken cancellationToken, Action networkReady)
    {
        Check(); var headers = new Dictionary<string, string>(Settings.Headers, StringComparer.OrdinalIgnoreCase);
        if (!headers.ContainsKey("x-vqd-hash-1") && !headers.ContainsKey("x-vqd-4")) { if (!Settings.AutoToken) throw new DuckAIException(503, "Supply fresh captured headers or enable AutoToken"); foreach (var h in await tokens.HeadersAsync(cancellationToken)) headers[h.Key] = h.Value; }
        Check(); cancellationToken.ThrowIfCancellationRequested(); networkReady();
        using var request = new HttpRequestMessage(HttpMethod.Post, "https://duck.ai/duckchat/v1/chat") { Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json") };
        request.Headers.TryAddWithoutValidation("Accept", "text/event-stream"); request.Headers.TryAddWithoutValidation("Origin", "https://duck.ai"); request.Headers.TryAddWithoutValidation("Referer", "https://duck.ai/"); request.Headers.TryAddWithoutValidation("DNT", "1"); request.Headers.TryAddWithoutValidation("Sec-GPC", "1");
        foreach (var header in headers) request.Headers.TryAddWithoutValidation(header.Key, header.Value);
        HttpResponseMessage response;
        try { response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken); }
        catch (HttpRequestException e) { throw new DuckAIException(502, "Could not connect to Duck.ai: " + e.Message); }
        if (!response.IsSuccessStatusCode)
        {
            using (response)
            {
                var data = new byte[65536]; var length = 0; await using var source = await response.Content.ReadAsStreamAsync(cancellationToken); while (length < data.Length) { var n = await source.ReadAsync(data.AsMemory(length), cancellationToken); if (n == 0) break; length += n; }
                var detail = "Duck.ai rejected the request"; try { detail = JsonNode.Parse(data.AsSpan(0, length))?.ToJsonString() ?? detail; } catch { }
                var retained = response.Headers.Where(h => new[] { "retry-after", "x-vqd-hash-1", "x-vqd-4" }.Contains(h.Key.ToLowerInvariant())).ToDictionary(h => h.Key.ToLowerInvariant(), h => string.Join(",", h.Value));
                throw DuckAIException.Upstream((int)response.StatusCode, detail, retained);
            }
        }
        if (!string.Equals(response.Content.Headers.ContentType?.MediaType, "text/event-stream", StringComparison.OrdinalIgnoreCase)) { response.Dispose(); throw new DuckAIException(502, "Duck.ai returned a non-SSE response"); }
        return response;
    }
    public async ValueTask DisposeAsync() { if (closed) return; closed = true; foreach (var s in streams.Values) await s.DisposeAsync(); try { await tokens.DisposeAsync(); } finally { if (ownsHttp) http.Dispose(); } }
}
public sealed class ChatStream : IAsyncEnumerable<string>, IAsyncDisposable
{
    internal Guid Id { get; } = Guid.NewGuid();
    public ChatResponse Response { get; }
    public JsonObject? Request { get; private set; }
    private readonly DuckAIClient client;
    private readonly string? prompt;
    private readonly ChatOptions options;
    private readonly CancellationTokenSource lifetime = new();
    private HttpResponseMessage? network;
    private bool consumed, closed;
    internal ChatStream(DuckAIClient client, string? prompt, ChatOptions options) { this.client = client; this.prompt = prompt; this.options = options; Response = new(options.Model ?? client.Model); }
    public async IAsyncEnumerator<string> GetAsyncEnumerator(CancellationToken cancellationToken = default) { await foreach (var value in EventsAsync(cancellationToken)) { if (value is JsonObject o && o["message"] is JsonValue v && v.TryGetValue<string>(out var text) && text.Length > 0) yield return text; } }
    public async IAsyncEnumerable<JsonNode?> EventsAsync([EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        if (consumed) throw new InvalidOperationException("A stream has one consumer"); consumed = true;
        ObjectDisposedException.ThrowIf(closed, this);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
        try
        {
            linked.Token.ThrowIfCancellationRequested(); Request = client.Request(prompt, options);
            network = await OpenBoundedAsync(Request, linked, cancellationToken);
            await using var body = await network.Content.ReadAsStreamAsync(linked.Token);
            await using var iterator = SSE.Parse(body, client.Settings.MaxCollectedBytes, linked.Token).GetAsyncEnumerator(linked.Token);
            while (true)
            {
                bool more;
                try { more = await iterator.MoveNextAsync(); }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested && !lifetime.IsCancellationRequested) { throw new DuckAIException(504, "Duck.ai request timed out"); }
                if (!more) throw new IncompleteResponseException(Response);
                var e = iterator.Current;
                if (e.Done) { Response.Done = true; yield break; }
                if ((e.Data is JsonObject error && error["action"]?.ToString() == "error") || e.Name is "error" or "proxy_error") {
                    var status = int.TryParse((e.Data as JsonObject)?["status"]?.ToString(), out var code) && code is >= 400 and <= 599 ? code : 502;
                    throw DuckAIException.Upstream(status, e.Data?.ToJsonString() ?? "Duck.ai emitted an error event");
                }
                Response.Append(e.Data); yield return e.Data?.DeepClone();
            }
        }
        finally { await DisposeAsync(); }
    }
    private async Task<HttpResponseMessage> OpenBoundedAsync(JsonObject request, CancellationTokenSource linked, CancellationToken caller)
    {
        try { return await client.OpenAsync(request, linked.Token, () => linked.CancelAfter(client.Settings.Timeout)); }
        catch (OperationCanceledException) when (!caller.IsCancellationRequested && !lifetime.IsCancellationRequested) { throw new DuckAIException(504, "Duck.ai request timed out"); }
    }
    public ValueTask DisposeAsync() { if (!closed) { closed = true; lifetime.Cancel(); network?.Dispose(); client.Unregister(Id); } return ValueTask.CompletedTask; }
}
internal sealed record SSEEvent(JsonNode? Data, string Name, bool Done);
internal static class SSE
{
    public static async IAsyncEnumerable<SSEEvent> Parse(Stream body, int maxBytes, [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        var buffer = new byte[8192]; var line = new List<byte>(); var lines = new List<string>(); var name = "message"; var afterCR = false; long received = 0;
        SSEEvent? Dispatch() { var eventName = name; name = "message"; if (lines.Count == 0) return null; var text = string.Join("\n", lines); lines.Clear(); if (text == "[DONE]") return new(null, eventName, true); JsonNode? value; try { value = JsonNode.Parse(text); } catch { value = new JsonObject { ["data"] = text }; } return new(value, eventName, false); }
        SSEEvent? Line() { var text = Encoding.UTF8.GetString(line.ToArray()); line.Clear(); if (text.Length == 0) return Dispatch(); if (text.StartsWith("data:")) lines.Add(text[5..].StartsWith(' ') ? text[6..] : text[5..]); if (text.StartsWith("event:")) name = text[6..].StartsWith(' ') ? text[7..] : text[6..]; return null; }
        while (true) { var count = await body.ReadAsync(buffer, cancellationToken); if (count == 0) break; received += count; if (received > maxBytes) throw new DuckAIException(502, "Duck.ai response exceeds collection limit"); for (var i = 0; i < count; i++) { var b = buffer[i]; if (afterCR) { afterCR = false; if (b == 10) continue; } if (b is 10 or 13) { var e = Line(); afterCR = b == 13; if (e is not null) { yield return e; if (e.Done) yield break; } } else line.Add(b); } }
        if (line.Count > 0) { var e = Line(); if (e is not null) yield return e; }
        var last = Dispatch(); if (last is not null) yield return last;
    }
}
public sealed class Conversation
{
    private readonly DuckAIClient client;
    private readonly ChatOptions defaults;
    private readonly SemaphoreSlim gate = new(1, 1);
    private List<JsonObject> history;
    private int active;
    internal Conversation(DuckAIClient client, ChatOptions options) { this.client = client; defaults = options; history = options.Messages.Select(m => (JsonObject)m.DeepClone()).ToList(); }
    public IReadOnlyList<JsonObject> Messages => history.Select(m => (JsonObject)m.DeepClone()).ToArray();
    public void Clear() { if (Volatile.Read(ref active) > 0) throw new InvalidOperationException("Cannot clear during a turn"); history = []; }
    private ChatOptions Merge(ChatOptions? options)
    {
        if (options is null) return defaults;
        var extra = (JsonObject)defaults.Extra.DeepClone();
        foreach (var item in options.Extra) extra[item.Key] = item.Value?.DeepClone();
        return options with
        {
            Model = options.Model ?? defaults.Model,
            ReasoningEffort = options.ReasoningEffort ?? defaults.ReasoningEffort,
            CanUseTools = options.CanUseTools ?? defaults.CanUseTools,
            CanShowGreeting = options.CanShowGreeting ?? defaults.CanShowGreeting,
            Extra = extra
        };
    }
    private async IAsyncEnumerable<JsonNode?> Turn(string prompt, ChatOptions? options, Action<ChatResponse>? result, [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        Interlocked.Increment(ref active); var acquired = false;
        try { await gate.WaitAsync(cancellationToken); acquired = true; await using var stream = client.Stream(prompt, Merge(options) with { Messages = Messages }); result?.Invoke(stream.Response); await foreach (var e in stream.EventsAsync(cancellationToken)) yield return e; if (stream.Response.Done) { history = stream.Request!["messages"]!.AsArray().OfType<JsonObject>().Select(m => (JsonObject)m.DeepClone()).ToList(); history.Add(stream.Response.AssistantMessage); } }
        finally { if (acquired) gate.Release(); Interlocked.Decrement(ref active); }
    }
    public async IAsyncEnumerable<string> Stream(string prompt, ChatOptions? options = null, [EnumeratorCancellation] CancellationToken cancellationToken = default) { await foreach (var e in Turn(prompt, options, null, cancellationToken)) if (e is JsonObject o && o["message"] is JsonValue v && v.TryGetValue<string>(out var text) && text.Length > 0) yield return text; }
    public IAsyncEnumerable<JsonNode?> Events(string prompt, ChatOptions? options = null, CancellationToken cancellationToken = default) => Turn(prompt, options, null, cancellationToken);
    public async Task<ChatResponse> ChatAsync(string prompt, ChatOptions? options = null, CancellationToken cancellationToken = default) { ChatResponse? response = null; await foreach (var _ in Turn(prompt, options, r => response = r, cancellationToken)) { } return response!; }
}
