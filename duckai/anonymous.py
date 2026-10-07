"""Anonymous-session bootstrap reconstructed from the supplied HAR and live frontend."""

import asyncio
import base64
import json
import os
import socket
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path

import httpx
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from duckai.config import Settings
from duckai.errors import ChallengeError, DuckAIError, RateLimitError

ORIGIN = "https://duck.ai"

# Duck.ai evaluates its status challenge in a same-origin iframe, SHA-256 hashes
# each returned client value, adds metadata, then base64-encodes the JSON object.
# This runs only in Chromium, never in the API server's Python/OS environment.
SOLVE_CHALLENGE = """async encoded => {
    const started = Date.now();
    const result = await eval(atob(encoded));
    if (!result || !Array.isArray(result.client_hashes)) {
        throw new Error('Unexpected Duck.ai challenge result');
    }
    const hashes = await Promise.all(result.client_hashes.map(async value => {
        const bytes = new TextEncoder().encode(value);
        const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
        return btoa(Array.from(digest, byte => String.fromCharCode(byte)).join(''));
    }));
    const stack = (new Error()).stack.split('\\n').slice(1, 6)
        .map(line => line.trim()).join('\\n');
    return btoa(JSON.stringify({
        ...result,
        client_hashes: hashes,
        meta: {...result.meta, origin: window.top.location.origin,
               stack, duration: String(Date.now() - started)}
    }));
}"""


class AnonymousTokenProvider:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.journey_id = uuid.uuid4().hex
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._page = None
        self._process = None
        self._profile = None

    async def close(self):
        try:
            if self._page is not None and self.settings.duckai_browser_cdp_url:
                await self._page.close()
            if self._browser is not None and not self.settings.duckai_browser_cdp_url:
                await self._browser.close()
        finally:
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._page = self._browser = self._playwright = None
                await self._stop_regular_browser()

    async def _stop_regular_browser(self):
        try:
            if self._process is not None:
                if self._process.returncode is None:
                    with suppress(ProcessLookupError):
                        self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
        finally:
            self._process = None
            if self._profile is not None:
                self._profile.cleanup()
                self._profile = None

    async def _launch_regular_browser(self):
        await self._stop_regular_browser()
        executable = (
            self.settings.duckai_browser_executable or self._playwright.chromium.executable_path
        )
        if not Path(executable).is_file():
            raise BrowserError("Chromium executable is missing")
        # A real browser debugging session, isolated from the user's profiles.
        # A nonzero debugging port preserves the browser's normal runtime behavior.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self._profile = tempfile.TemporaryDirectory(prefix="duckai-anonymous-")
        arguments = [
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._profile.name}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-search-engine-choice-screen",
            "--disable-features=GlobalMediaControls,MediaRouter,Translate,OptimizationHints,PaintHolding",
            "about:blank",
        ]
        # Chromium cannot start as root in a container with its OS sandbox enabled.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            arguments.insert(0, "--no-sandbox")
        self._process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        endpoint = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(trust_env=False, timeout=1) as probe:
            while True:
                if self._process.returncode is not None:
                    raise BrowserError("Chromium exited during startup; a display is required")
                try:
                    response = await probe.get(f"{endpoint}/json/version")
                    if response.is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        return await self._playwright.chromium.connect_over_cdp(endpoint)

    async def _browser_metadata(self):
        if self._page is None or self._page.is_closed():
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None or not self._browser.is_connected():
                options = {"headless": self.settings.duckai_browser_headless}
                if self.settings.duckai_browser_executable:
                    options["executable_path"] = self.settings.duckai_browser_executable
                elif self.settings.duckai_browser_channel:
                    options["channel"] = self.settings.duckai_browser_channel
                if self.settings.duckai_browser_cdp_url:
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        self.settings.duckai_browser_cdp_url
                    )
                elif (
                    not self.settings.duckai_browser_headless
                    and not self.settings.duckai_browser_channel
                ):
                    self._browser = await self._launch_regular_browser()
                else:
                    self._browser = await self._playwright.chromium.launch(**options)
            if self.settings.duckai_browser_cdp_url or self._process is not None:
                self._page = await self._browser.contexts[0].new_page()
            else:
                self._page = await self._browser.new_page()
            await self._page.goto(ORIGIN, wait_until="domcontentloaded")
            await self._page.wait_for_selector("iframe#jsa", state="attached")
        return await self._page.evaluate("""() => ({
            userAgent: navigator.userAgent,
            version: `${document.documentElement.getAttribute('data-version-tag')}-${
                document.documentElement.getAttribute('data-version-sha')}`
        })""")

    async def _solve(self, challenge: str):
        element = await self._page.query_selector("iframe#jsa")
        frame = await element.content_frame() if element else None
        if frame is None:
            raise DuckAIError(502, detail="Duck.ai challenge iframe unavailable")
        return await frame.evaluate(SOLVE_CHALLENGE, challenge)

    async def headers(self) -> dict[str, str]:
        # Serialize iframe access, but chats themselves can run concurrently.
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._generate(), timeout=self.settings.token_generation_timeout
                )
            except (TimeoutError, httpx.TimeoutException) as exc:
                # Discard a page with an unfinished challenge before the next request.
                if self._page is not None:
                    page, self._page = self._page, None
                    with suppress(TimeoutError, BrowserError):
                        await asyncio.wait_for(page.close(), timeout=5)
                raise DuckAIError(504, detail="Anonymous token generation timed out") from exc
            except BrowserError as exc:
                if self._page is not None:
                    page, self._page = self._page, None
                    with suppress(TimeoutError, BrowserError):
                        await asyncio.wait_for(page.close(), timeout=5)
                raise DuckAIError(
                    503,
                    detail=(
                        "Chromium token generation failed. Run `playwright install chromium`, "
                        "check that a display is available (Xvfb on Linux), or set "
                        "DUCKAI_BROWSER_EXECUTABLE to an installed Chromium browser."
                    ),
                ) from exc
            except httpx.HTTPStatusError as exc:
                response = exc.response
                retry_after = response.headers.get("retry-after")
                error_type = (
                    RateLimitError
                    if response.status_code == 429
                    else ChallengeError
                    if response.status_code in {401, 403, 418}
                    else DuckAIError
                )
                raise error_type(
                    response.status_code,
                    detail="Duck.ai rejected the anonymous session bootstrap",
                    headers={"retry-after": retry_after} if retry_after else None,
                ) from exc
            except httpx.HTTPError as exc:
                raise DuckAIError(502, detail="Duck.ai anonymous session bootstrap failed") from exc

    async def _generate(self) -> dict[str, str]:
        started = int(time.time() * 1000)
        metadata = await self._browser_metadata()
        headers = {
            "user-agent": metadata["userAgent"],
            "accept": "application/json",
            "referer": f"{ORIGIN}/",
            "origin": ORIGIN,
            "x-ddg-journey-id": self.journey_id,
        }
        # The anonymous auth/token response in the HAR is {}. It is a session
        # bootstrap, not an access token to invent or hard-code.
        auth = await self.client.get(f"{ORIGIN}/duckchat/v1/auth/token", headers=headers)
        auth.raise_for_status()
        status = await self.client.get(
            f"{ORIGIN}/duckchat/v1/status",
            headers=headers | {"x-vqd-accept": "1", "cache-control": "no-store"},
        )
        status.raise_for_status()
        challenge = status.headers.get("x-vqd-hash-1")
        if not challenge:
            raise DuckAIError(502, detail="Duck.ai status did not return a browser challenge")
        token = await self._solve(challenge)
        # Signals describe this API-driven session; no fabricated trusted UI events.
        signals = base64.b64encode(
            json.dumps(
                {"start": started, "events": [], "end": int(time.time() * 1000) - started},
                separators=(",", ":"),
            ).encode()
        ).decode()
        return {
            "x-vqd-hash-1": token,
            "x-fe-version": metadata["version"],
            "x-fe-signals": signals,
            "x-ddg-journey-id": self.journey_id,
            "user-agent": metadata["userAgent"],
        }
