import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from playwright.async_api import Error as BrowserError

from duckai import AsyncDuckAI
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
    monkeypatch.setattr(AnonymousTokenProvider, "_prepare_runtime", AsyncMock())
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
    monkeypatch.setattr(AnonymousTokenProvider, "_prepare_runtime", AsyncMock())
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


@pytest.mark.anyio
@pytest.mark.parametrize("headless", [True, False])
async def test_full_chromium_launch_is_silent_by_default_and_can_be_visible(
    monkeypatch, tmp_path, headless
):
    executable = tmp_path / "chromium"
    executable.touch()
    process = SimpleNamespace(
        returncode=None, terminate=Mock(), wait=AsyncMock(return_value=0), kill=Mock()
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=httpx.Response(200)))
    browser = SimpleNamespace(close=AsyncMock())
    chromium = SimpleNamespace(
        executable_path=str(executable), connect_over_cdp=AsyncMock(return_value=browser)
    )
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(
            client, Settings(_env_file=None, duckai_browser_headless=headless)
        )
        provider._playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        try:
            provider._browser = await provider._launch_chromium()
            arguments = spawn.call_args.args
            assert arguments[0] == str(executable)
            assert ("--headless=new" in arguments) is headless
            assert ("--disable-blink-features=AutomationControlled" in arguments) is headless
            assert "--enable-automation" not in arguments
            port = next(a for a in arguments if a.startswith("--remote-debugging-port="))
            assert int(port.split("=")[1]) > 0
            assert "--remote-debugging-address=127.0.0.1" in arguments
            profile = provider._profile.name
        finally:
            await provider.close()
        assert not Path(profile).exists()
        process.terminate.assert_called_once()
        browser.close.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["X11; Linux x86_64", "Windows NT 10.0; Win64; x64"])
async def test_headless_identity_is_derived_from_browser_and_set_before_navigation(platform):
    user_agent = f"Mozilla/5.0 ({platform}) HeadlessChrome/153.0.0.0 Safari/537.36"
    normalized = user_agent.replace("HeadlessChrome/", "Chrome/")
    order = []

    async def override(*args):
        order.append("identity")

    async def navigate(*args, **kwargs):
        order.append("navigation")

    session = SimpleNamespace(send=AsyncMock(side_effect=override))
    context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    page = SimpleNamespace(
        context=context,
        is_closed=Mock(return_value=False),
        evaluate=AsyncMock(
            side_effect=[
                user_agent,
                {"userAgent": normalized, "version": "live-version"},
                {"userAgent": normalized, "version": "live-version"},
            ]
        ),
        goto=AsyncMock(side_effect=navigate),
        wait_for_selector=AsyncMock(),
    )
    context.new_page = AsyncMock(return_value=page)
    browser = SimpleNamespace(
        contexts=[context], is_connected=Mock(return_value=True), close=AsyncMock()
    )
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        assert provider.settings.duckai_browser_headless is True
        provider._browser = browser
        provider._process = SimpleNamespace(
            returncode=0, wait=AsyncMock(return_value=0), kill=Mock()
        )
        assert (await provider._browser_metadata())["userAgent"] == normalized
        assert (await provider._browser_metadata())["version"] == "live-version"
        session.send.assert_awaited_once_with(
            "Network.setUserAgentOverride", {"userAgent": normalized}
        )
        context.new_page.assert_awaited_once()
        assert order == ["identity", "navigation"]
        await provider.close()


@pytest.mark.anyio
async def test_visible_or_custom_browser_identity_is_left_intact():
    page = SimpleNamespace(evaluate=AsyncMock(return_value="A custom Chromium UA"), context=Mock())
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        provider._page = page
        await provider._configure_headless_page()
        page.context.new_cdp_session.assert_not_called()


@pytest.mark.anyio
async def test_first_use_installs_full_browser_once_without_a_console(monkeypatch, tmp_path):
    executable = tmp_path / "chromium"
    process = SimpleNamespace(returncode=None)

    async def installed():
        executable.touch()
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=installed)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        provider._playwright = SimpleNamespace(
            chromium=SimpleNamespace(executable_path=str(executable)), stop=AsyncMock()
        )
        await provider._prepare_runtime()
        await provider._prepare_runtime()
        assert spawn.await_count == 1
        assert spawn.call_args.args[1:] == ("-m", "playwright", "install", "chromium", "--no-shell")
        assert spawn.call_args.kwargs["stdout"] == asyncio.subprocess.DEVNULL
        assert provider._install_process is None
        await provider.close()


@pytest.mark.anyio
async def test_missing_custom_or_external_browser_never_triggers_a_download(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with httpx.AsyncClient() as client:
        for options in (
            {"duckai_browser_executable": "/custom/chromium"},
            {"duckai_browser_cdp_url": "http://127.0.0.1:9222"},
            {"duckai_browser_channel": "chrome"},
        ):
            provider = AnonymousTokenProvider(client, Settings(_env_file=None, **options))
            await provider._prepare_runtime()
            assert provider._playwright is None
    spawn.assert_not_awaited()


@pytest.mark.anyio
async def test_automatic_browser_installation_can_be_disabled(monkeypatch, tmp_path):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(
            client, Settings(_env_file=None, duckai_browser_auto_install=False)
        )
        provider._playwright = SimpleNamespace(
            chromium=SimpleNamespace(executable_path=str(tmp_path / "missing")), stop=AsyncMock()
        )
        with pytest.raises(BrowserError, match="automatic installation is disabled"):
            await provider._prepare_runtime()
        spawn.assert_not_awaited()
        await provider.close()


@pytest.mark.anyio
async def test_cancelled_browser_download_cleans_up_its_process_group(monkeypatch, tmp_path):
    import os
    import signal

    waiting = asyncio.Event()
    process = SimpleNamespace(returncode=None, pid=12345)

    async def wait():
        if process.returncode is None:
            waiting.set()
            await asyncio.Future()
        return process.returncode

    def terminate(*args):
        process.returncode = -15

    process.wait = AsyncMock(side_effect=wait)
    process.terminate = Mock(side_effect=terminate)
    terminate_group = Mock(side_effect=terminate)
    if os.name != "nt":
        monkeypatch.setattr(os, "killpg", terminate_group)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        provider._playwright = SimpleNamespace(
            chromium=SimpleNamespace(executable_path=str(tmp_path / "missing")), stop=AsyncMock()
        )
        task = asyncio.create_task(provider._prepare_runtime())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider._install_process is None
        if os.name != "nt":
            terminate_group.assert_called_once_with(12345, signal.SIGTERM)
        else:
            process.terminate.assert_called_once()
        await provider.close()


@pytest.mark.anyio
async def test_windows_download_cleanup_terminates_node_child_without_a_console(monkeypatch):
    from duckai import anonymous

    process = SimpleNamespace(pid=12345, returncode=None, terminate=Mock(), wait=AsyncMock())
    killer = SimpleNamespace(wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=killer)
    monkeypatch.setattr(anonymous, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with httpx.AsyncClient() as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        provider._install_process = process
        await provider._stop_installer()
        assert spawn.call_args.args == ("taskkill", "/PID", "12345", "/T", "/F")
        assert spawn.call_args.kwargs["creationflags"] == 0x08000000
        assert provider._install_process is None


@pytest.mark.anyio
async def test_cancelled_challenge_discards_partial_page_and_next_request_can_start(monkeypatch):
    waiting = asyncio.Event()
    page = SimpleNamespace(close=AsyncMock())
    calls = 0

    async def metadata(provider):
        nonlocal calls
        calls += 1
        if calls == 1:
            provider._page = page
            waiting.set()
            await asyncio.Future()
        return {"userAgent": "browser UA", "version": "live-version"}

    monkeypatch.setattr(AnonymousTokenProvider, "_prepare_runtime", AsyncMock())
    monkeypatch.setattr(AnonymousTokenProvider, "_browser_metadata", metadata)
    monkeypatch.setattr(AnonymousTokenProvider, "_solve", AsyncMock(return_value="fresh-token"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={}, headers={"x-vqd-hash-1": "challenge"})
        )
    ) as client:
        provider = AnonymousTokenProvider(client, Settings(_env_file=None))
        task = asyncio.create_task(provider.headers())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider._page is None
        page.close.assert_awaited_once()
        assert (await provider.headers())["x-vqd-hash-1"] == "fresh-token"
        await provider.close()


@pytest.mark.anyio
async def test_public_client_handles_cold_start_and_reuses_installed_browser(monkeypatch, tmp_path):
    executable = tmp_path / "chromium"
    order = []
    process = SimpleNamespace(returncode=None)

    async def installed():
        order.append("install")
        executable.touch()
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=installed)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    driver = SimpleNamespace(
        chromium=SimpleNamespace(executable_path=str(executable)), stop=AsyncMock()
    )
    start = AsyncMock(return_value=driver)
    monkeypatch.setattr("duckai.anonymous.async_playwright", lambda: SimpleNamespace(start=start))

    async def metadata(provider):
        order.append("metadata")
        return {"userAgent": "browser UA", "version": "live-version"}

    async def solve(provider, challenge):
        order.append("solve")
        return "solved-token"

    monkeypatch.setattr(AnonymousTokenProvider, "_browser_metadata", metadata)
    monkeypatch.setattr(AnonymousTokenProvider, "_solve", solve)

    def handler(request):
        action = request.url.path.rsplit("/", 1)[-1]
        order.append(action)
        if action == "chat":
            assert request.headers["x-vqd-hash-1"] == "solved-token"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"message":"ready"}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(200, json={}, headers={"x-vqd-hash-1": "challenge"})

    async with AsyncDuckAI(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    ) as ai:
        assert ai.settings.duckai_browser_headless and ai.settings.duckai_browser_auto_install
        assert (await ai.chat("Hello")).text == "ready"
        assert order == ["install", "metadata", "token", "status", "solve", "chat"]
        assert (await ai.chat("Again")).done
    assert start.await_count == 1 and spawn.await_count == 1
    driver.stop.assert_awaited_once()
