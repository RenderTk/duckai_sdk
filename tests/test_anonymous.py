from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from playwright.async_api import Error as BrowserError

from duckai.anonymous import AnonymousTokenProvider
from duckai.config import Settings
from duckai.errors import DuckAIError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_browser(monkeypatch):
    metadata = AsyncMock(return_value={"userAgent": "Real browser UA", "version": "live-version"})
    solve = AsyncMock(side_effect=lambda challenge: f"solved-{challenge}")
    monkeypatch.setattr(AnonymousTokenProvider, "_browser_metadata", metadata)
    monkeypatch.setattr(AnonymousTokenProvider, "_solve", solve)
    return metadata, solve


@pytest.mark.anyio
async def test_each_request_gets_a_fresh_challenge(monkeypatch):
    _, solve = fake_browser(monkeypatch)
    counter = 0

    def handler(request):
        nonlocal counter
        if request.url.path.endswith("/status"):
            counter += 1
            return httpx.Response(200, headers={"x-vqd-hash-1": str(counter)})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        first = await provider.headers()
        second = await provider.headers()
        assert first["x-vqd-hash-1"] != second["x-vqd-hash-1"]
        assert first["x-ddg-journey-id"] == second["x-ddg-journey-id"]
        assert solve.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("status", [418, 429])
async def test_bootstrap_rejections_preserve_status_and_retry_after(monkeypatch, status):
    fake_browser(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"retry-after": "30"})
        )
    ) as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        with pytest.raises(DuckAIError) as error:
            await provider.headers()
        assert error.value.status_code == status
        assert error.value.headers == {"retry-after": "30"}


@pytest.mark.anyio
async def test_missing_challenge_is_reported(monkeypatch):
    fake_browser(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        with pytest.raises(DuckAIError) as error:
            await provider.headers()
        assert error.value.status_code == 502


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,status", [(TimeoutError(), 504), (BrowserError("missing browser"), 503)]
)
async def test_browser_failure_is_actionable(monkeypatch, failure, status):
    monkeypatch.setattr(AnonymousTokenProvider, "_browser_metadata", AsyncMock(side_effect=failure))
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        with pytest.raises(DuckAIError) as error:
            await provider.headers()
        assert error.value.status_code == status


@pytest.mark.anyio
async def test_external_browser_is_not_closed():
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(
            client, Settings(_env_file=None, duckai_browser_cdp_url="http://127.0.0.1:9222")
        )
        page, browser, driver = AsyncMock(), AsyncMock(), AsyncMock()
        provider._page, provider._browser, provider._playwright = page, browser, driver
        await provider.close()
        page.close.assert_awaited_once()
        browser.close.assert_not_awaited()
        driver.stop.assert_awaited_once()


@pytest.mark.anyio
async def test_owned_process_and_profile_cleanup_handles_already_exited_process():
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        process = SimpleNamespace(
            returncode=None,
            terminate=Mock(side_effect=ProcessLookupError),
            wait=AsyncMock(return_value=0),
            kill=Mock(),
        )
        profile = Mock()
        provider._process, provider._profile = process, profile
        await provider.close()
        process.wait.assert_awaited_once()
        profile.cleanup.assert_called_once()
        assert provider._process is None
        assert provider._profile is None
