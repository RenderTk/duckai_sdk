using Microsoft.Playwright;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace DuckAI;

public interface ITokenProvider : IAsyncDisposable { Task<IReadOnlyDictionary<string, string>> HeadersAsync(CancellationToken cancellationToken = default); }
/// <summary>Owns isolated Chromium resources. Never connects to or closes a user's browser.</summary>
public sealed class AnonymousTokenProvider(Settings settings) : ITokenProvider
{
    private readonly SemaphoreSlim gate = new(1, 1);
    private readonly CancellationTokenSource lifetime = new();
    private readonly string journey = Guid.NewGuid().ToString("N");
    private IPlaywright? playwright;
    private IBrowser? browser;
    private IPage? page;
    private bool closed;
    private static readonly SemaphoreSlim installGate = new(1, 1);
    private async Task ResetAsync() { var old = browser; browser = null; page = null; if (old is not null) try { await old.CloseAsync(); } catch { } playwright?.Dispose(); playwright = null; }
    private static async Task InstallBrowserAsync(CancellationToken cancellationToken)
    {
        // Run the CLI shipped with Playwright so cancellation owns the installer
        // process tree instead of abandoning an in-process Program.Main call.
        var root = AppContext.BaseDirectory;
        var platform = OperatingSystem.IsWindows() ? "win32_x64" :
            (OperatingSystem.IsMacOS() ? "darwin" : "linux") +
            (RuntimeInformation.ProcessArchitecture == Architecture.Arm64 ? "-arm64" : "-x64");
        var node = Path.Combine(root, ".playwright", "node", platform, OperatingSystem.IsWindows() ? "node.exe" : "node");
        var cli = Path.Combine(root, ".playwright", "package", "cli.js");
        if (!File.Exists(node) || !File.Exists(cli)) throw new DuckAIException(503, "Playwright installer resources are missing; run playwright.ps1 install chromium --no-shell after publishing");
        var start = new ProcessStartInfo(node) { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
        start.ArgumentList.Add(cli); start.ArgumentList.Add("install"); start.ArgumentList.Add("chromium"); start.ArgumentList.Add("--no-shell");
        using var installer = Process.Start(start) ?? throw new DuckAIException(503, "Could not start Chromium installer");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromMinutes(5));
        var output = installer.StandardOutput.BaseStream.CopyToAsync(Stream.Null);
        var errors = installer.StandardError.BaseStream.CopyToAsync(Stream.Null);
        try
        {
            await installer.WaitForExitAsync(deadline.Token);
            await Task.WhenAll(output, errors);
            if (installer.ExitCode != 0) throw new DuckAIException(503, "Chromium installation failed; run playwright.ps1 install chromium --no-shell for diagnostics");
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new DuckAIException(504, "Chromium installation timed out after five minutes");
        }
        finally
        {
            if (!installer.HasExited) { installer.Kill(entireProcessTree: true); await installer.WaitForExitAsync(); }
            await Task.WhenAll(output, errors);
        }
    }
    public async Task<IReadOnlyDictionary<string, string>> HeadersAsync(CancellationToken cancellationToken = default)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
        await gate.WaitAsync(linked.Token);
        try
        {
            ObjectDisposedException.ThrowIf(closed, this);
            playwright ??= await Playwright.CreateAsync();
            linked.Token.ThrowIfCancellationRequested();
            if (browser is null)
            {
                if (settings.BrowserExecutable is null && !File.Exists(playwright.Chromium.ExecutablePath))
                {
                    if (!settings.BrowserAutoInstall) throw new DuckAIException(503, "Chromium is missing. Run the generated playwright.ps1 install chromium --no-shell script, or configure BrowserExecutable.");
                    await installGate.WaitAsync(linked.Token);
                    try { if (!File.Exists(playwright.Chromium.ExecutablePath)) { await InstallBrowserAsync(linked.Token); } }
                    finally { installGate.Release(); }
                }
                browser = await playwright.Chromium.LaunchAsync(new() { Headless = settings.Headless, ExecutablePath = settings.BrowserExecutable, Args = ["--disable-blink-features=AutomationControlled"], IgnoreDefaultArgs = ["--enable-automation"], Timeout = (float)settings.TokenGenerationTimeout.TotalMilliseconds });
                linked.Token.ThrowIfCancellationRequested();
                var context = await browser.NewContextAsync();
                page = await context.NewPageAsync();
                var ua = await page.EvaluateAsync<string>("navigator.userAgent");
                if (ua.Contains("HeadlessChrome/")) { var cdp = await context.NewCDPSessionAsync(page); await cdp.SendAsync("Network.setUserAgentOverride", new Dictionary<string, object> { ["userAgent"] = ua.Replace("HeadlessChrome/", "Chrome/") }); }
            }
            linked.CancelAfter(settings.TokenGenerationTimeout);
            using var registration = linked.Token.Register(() => { if (browser is { } b) _ = b.CloseAsync(); });
            if (page!.Url != "https://duck.ai/") await page.GotoAsync("https://duck.ai/", new() { WaitUntil = WaitUntilState.DOMContentLoaded, Timeout = (float)settings.TokenGenerationTimeout.TotalMilliseconds }).WaitAsync(linked.Token);
            await page.WaitForSelectorAsync("iframe#jsa", new() { State = WaitForSelectorState.Attached }).WaitAsync(linked.Token);
            var value = await page.EvaluateAsync<JsonElement>("async journey => {" + Catalog.Resource("browser-bootstrap.js") + "}", journey).WaitAsync(linked.Token);
            if (value.TryGetProperty("status", out var status)) throw DuckAIException.Upstream(status.GetInt32(), "Duck.ai rejected anonymous bootstrap", new Dictionary<string, string> { ["retry-after"] = value.GetProperty("retryAfter").GetString() ?? "" });
            var headers = JsonSerializer.Deserialize<Dictionary<string, string>>(value.GetProperty("headers"))!;
            if (!headers.TryGetValue("x-vqd-hash-1", out var token) || token.Length == 0) throw new DuckAIException(502, "Bootstrap returned no challenge headers");
            return headers;
        }
        catch (Exception error)
        {
            await ResetAsync();
            if (cancellationToken.IsCancellationRequested || lifetime.IsCancellationRequested) throw new OperationCanceledException(cancellationToken.IsCancellationRequested ? cancellationToken : lifetime.Token);
            if (linked.IsCancellationRequested) throw new DuckAIException(504, "Anonymous session generation timed out");
            if (error is DuckAIException) throw;
            throw new DuckAIException(503, "Chromium anonymous bootstrap failed: " + error.Message);
        }
        finally { gate.Release(); }
    }
    public async ValueTask DisposeAsync()
    {
        if (closed) return; closed = true; lifetime.Cancel();
        await gate.WaitAsync(); try { await ResetAsync(); } finally { gate.Release(); }
    }
}
