using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using DuckAI;
using Xunit;
using SkiaSharp;

public sealed class SDKTests
{
    private sealed class Tokens : ITokenProvider
    {
        public int Calls; public bool Closed;
        public Task<IReadOnlyDictionary<string, string>> HeadersAsync(CancellationToken cancellationToken = default) { Calls++; return Task.FromResult<IReadOnlyDictionary<string, string>>(new Dictionary<string, string> { ["x-vqd-hash-1"] = "fixture" }); }
        public ValueTask DisposeAsync() { Closed = true; return ValueTask.CompletedTask; }
    }
    private sealed class Transport(string text) : HttpMessageHandler
    {
        public string Text = text; public HttpStatusCode Status = HttpStatusCode.OK; public List<JsonNode?> Bodies = [];
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            Bodies.Add(JsonNode.Parse(await request.Content!.ReadAsStringAsync(token)));
            var response = new HttpResponseMessage(Status) { Content = new StreamContent(new Chunks(Encoding.UTF8.GetBytes(Text))) };
            response.Content.Headers.ContentType = new("text/event-stream"); response.Headers.TryAddWithoutValidation("Retry-After", "7"); return response;
        }
    }
    private sealed class Chunks(byte[] data) : MemoryStream(data)
    {
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) => base.ReadAsync(buffer[..Math.Min(1, buffer.Length)], cancellationToken);
    }
    private static JsonNode Fixture(string name) => JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", name + ".json")))!;
    [Fact]
    public async Task ContractConversationAndPartialFailure()
    {
        var f = Fixture("chat"); var transport = new Transport(f["sse"]!.ToString()); using var http = new HttpClient(transport); var tokens = new Tokens();
        await using var ai = new DuckAIClient(f["model"]!.ToString(), tokenProvider: tokens, httpClient: http);
        var response = await ai.ChatAsync(f["prompt"]!.ToString());
        Assert.Equal(f["text"]!.ToString(), response.Text); Assert.True(response.Done); Assert.True(JsonNode.DeepEquals(response.AssistantMessage["parts"], f["parts"])); Assert.True(JsonNode.DeepEquals(f["request"], transport.Bodies[0]));
        var chat = ai.Conversation(); await chat.ChatAsync("One"); await chat.ChatAsync("Two"); Assert.Equal(4, chat.Messages.Count);
        transport.Text = "data: {\"message\":\"partial\"}\n\n"; var error = await Assert.ThrowsAsync<IncompleteResponseException>(() => chat.ChatAsync("failed")); Assert.Equal("partial", error.Response.Text); Assert.Equal(4, chat.Messages.Count);
        await ai.DisposeAsync(); Assert.True(tokens.Closed);
    }
    [Fact]
    public async Task RateLimitAndCancellation()
    {
        var transport = new Transport("{}"); transport.Status = HttpStatusCode.TooManyRequests; using var http = new HttpClient(transport); await using var ai = new DuckAIClient(tokenProvider: new Tokens(), httpClient: http);
        var error = await Assert.ThrowsAsync<RateLimitException>(() => ai.ChatAsync("hello")); Assert.Equal("7", error.RetryAfter);
        transport.Status = HttpStatusCode.OK; transport.Text = "event: proxy_error\ndata: \"failure\"\n\n";
        var named = await Assert.ThrowsAsync<DuckAIException>(()=>ai.ChatAsync("error")); Assert.Equal(502,named.StatusCode);
        using var token = new CancellationTokenSource(); token.Cancel(); await Assert.ThrowsAnyAsync<OperationCanceledException>(() => ai.ChatAsync("hello", cancellationToken: token.Token));
    }
    private sealed class WaitingTransport : HttpMessageHandler
    {
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            await Task.Delay(10000, token); return new(HttpStatusCode.OK);
        }
    }
    [Fact]
    public async Task DefaultsAndHeaderTimeout()
    {
        var transport = new Transport("data: [DONE]\n\n"); using var http = new HttpClient(transport);
        await using var ai = new DuckAIClient(tokenProvider: new Tokens(), httpClient: http);
        var chat = ai.Conversation(new() { Model = Model.MISTRAL_SMALL_4, CanShowGreeting = true, Extra = new() { ["durableStream"] = true } });
        await chat.ChatAsync("hello", new() { CanUseTools = false });
        Assert.Equal(Model.MISTRAL_SMALL_4, transport.Bodies[0]!["model"]!.ToString());
        Assert.True(transport.Bodies[0]!["canShowGreeting"]!.GetValue<bool>());
        Assert.True(transport.Bodies[0]!["durableStream"]!.GetValue<bool>());
        using var slowHttp = new HttpClient(new WaitingTransport());
        await using var slow = new DuckAIClient(settings: new() { Timeout = TimeSpan.FromMilliseconds(20) }, tokenProvider: new Tokens(), httpClient: slowHttp);
        var error = await Assert.ThrowsAsync<DuckAIException>(() => slow.ChatAsync("test")); Assert.Equal(504, error.StatusCode);
    }
    [Fact]
    public async Task ImageOrientationAndXmlBounds()
    {
        await using var ai = new DuckAIClient(tokenProvider: new Tokens());
        foreach (var f in Fixture("images").AsArray())
        {
            var part = ai.PrepareFiles([new(Convert.FromBase64String(f!["data"]!.ToString()), f["filename"]!.ToString())])[0];
            using var image = SKBitmap.Decode(Convert.FromBase64String(part["image"]!.ToString().Split(',')[1]));
            Assert.Equal(12, image.Width); Assert.Equal(28, image.Height);
        }
        var deep = string.Concat(Enumerable.Repeat("<a>", 140)) + "hello" + string.Concat(Enumerable.Repeat("</a>", 140));
        Assert.Throws<AttachmentException>(() => ai.PrepareFiles([new(Encoding.UTF8.GetBytes(deep), "deep.xml")]));
    }
    [Fact] public async Task NativePdfPreparation() {
        await using var ai = new DuckAIClient(tokenProvider:new Tokens());
        foreach (var f in Fixture("pdfs").AsArray()) {
            var part = ai.PrepareFiles([new(Convert.FromBase64String(f!["data"]!.ToString()),f["filename"]!.ToString())])[0];
            Assert.Equal("application/pdf",part["mimeType"]!.ToString());
        }
    }
    [Fact]
    public async Task LocalDocumentsAndBounds()
    {
        await using var ai = new DuckAIClient(Model.MISTRAL_SMALL_4, tokenProvider: new Tokens());
        foreach (var f in Fixture("documents").AsArray()) { var name = f!["filename"]!.ToString(); var parts = ai.PrepareFiles([new(Convert.FromBase64String(f["data"]!.ToString()), name)]); foreach (var expected in f["contains"]!.AsArray()) Assert.Contains(expected!.ToString(), parts[0]["text"]!.ToString()); }
        await using var bounded = new DuckAIClient(settings: new() { MaxCollectedBytes = 3 }, tokenProvider: new Tokens(), httpClient: new HttpClient(new Transport("data: never terminated")));
        await Assert.ThrowsAsync<DuckAIException>(() => bounded.ChatAsync("test"));
    }
}
