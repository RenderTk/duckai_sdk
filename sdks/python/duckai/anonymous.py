"""Anonymous-session bootstrap reconstructed from the supplied HAR and live frontend."""

import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import sys
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
        self._install_process = None

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
                try:
                    await self._stop_installer()
                finally:
                    await self._stop_regular_browser()

    async def _prepare_runtime(self):
        """Provision the bundled browser without consuming the challenge timeout."""
        if (
            self.settings.duckai_browser_cdp_url
            or self.settings.duckai_browser_executable
            or self.settings.duckai_browser_channel
        ):
            return
        if self._playwright is None:
            self._playwright = await asyncio.wait_for(
                async_playwright().start(), timeout=self.settings.token_generation_timeout
            )
        executable = Path(self._playwright.chromium.executable_path)
        if executable.is_file():
            return
        if not self.settings.duckai_browser_auto_install:
            raise BrowserError("Chromium is missing and automatic installation is disabled")
        process_options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            self._install_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                "--no-shell",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **process_options,
            )
            try:
                code = await asyncio.wait_for(
                    self._install_process.wait(), timeout=self.settings.browser_install_timeout
                )
            except TimeoutError as error:
                raise BrowserError("Chromium automatic installation timed out") from error
            if code != 0 or not executable.is_file():
                raise BrowserError("Chromium automatic installation failed")
        finally:
            await self._stop_installer()

    async def _stop_installer(self):
        process, self._install_process = self._install_process, None
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            # Python's Playwright CLI owns a Node child. Terminating only Python
            # would leave that child downloading after cancellation on Windows.
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            await killer.wait()
        with suppress(ProcessLookupError):
            if os.name == "nt":
                process.terminate()
            else:
                # The CLI launches a Node child. Its own process group lets us
                # clean up the whole download if the caller cancels startup.
                os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

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

    async def _launch_chromium(self):
        await self._stop_regular_browser()
        executable = (
            self.settings.duckai_browser_executable or self._playwright.chromium.executable_path
        )
        if not Path(executable).is_file():
            raise BrowserError("Chromium executable is missing")
        # Launch full Chromium directly, isolated from the user's profiles. The
        # headless shell and Playwright's automation launch defaults were rejected
        # in live checks. Unified headless Chromium works with the same challenge
        # evaluator as a visible browser and never needs a desktop window.
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
        if self.settings.duckai_browser_headless:
            arguments[0:0] = [
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
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
                    raise BrowserError("Chromium exited during startup")
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
                elif not self.settings.duckai_browser_channel:
                    self._browser = await self._launch_chromium()
                else:
                    if self.settings.duckai_browser_headless:
                        options["args"] = ["--disable-blink-features=AutomationControlled"]
                        options["ignore_default_args"] = ["--enable-automation"]
                    self._browser = await self._playwright.chromium.launch(**options)
            if self.settings.duckai_browser_cdp_url or self._process is not None:
                self._page = await self._browser.contexts[0].new_page()
            else:
                self._page = await self._browser.new_page()
            if self.settings.duckai_browser_headless:
                await self._configure_headless_page()
            await self._page.goto(ORIGIN, wait_until="domcontentloaded")
            await self._page.wait_for_selector("iframe#jsa", state="attached")
        return await self._page.evaluate("""() => ({
            userAgent: navigator.userAgent,
            version: `${document.documentElement.getAttribute('data-version-tag')}-${
                document.documentElement.getAttribute('data-version-sha')}`
        })""")

    async def _configure_headless_page(self):
        # Use the running browser's platform and version, rather than a static
        # fingerprint. The headless-specific UA marker also caused challenge
        # rejection in live checks. CDP updates the page's JS and HTTP identity
        # together, and the setting applies to the challenge iframe as well.
        user_agent = await self._page.evaluate("navigator.userAgent")
        if "HeadlessChrome/" in user_agent:
            session = await self._page.context.new_cdp_session(self._page)
            await session.send(
                "Network.setUserAgentOverride",
                {"userAgent": user_agent.replace("HeadlessChrome/", "Chrome/")},
            )

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
                await self._prepare_runtime()
                return await asyncio.wait_for(
                    self._generate(), timeout=self.settings.token_generation_timeout
                )
            except asyncio.CancelledError:
                # Cancellation can interrupt navigation or evaluation inside the
                # challenge iframe. A later request needs a fresh page, rather
                # than reusing one whose initialisation never finished.
                if self._page is not None:
                    page, self._page = self._page, None
                    with suppress(TimeoutError, BrowserError):
                        await asyncio.wait_for(page.close(), timeout=5)
                raise
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
                        "or set DUCKAI_BROWSER_EXECUTABLE to an installed Chromium browser."
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
