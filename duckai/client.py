"""Direct async client and streaming interface. No localhost server is involved."""

import asyncio
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from duckai.anonymous import AnonymousTokenProvider
from duckai.attachments import PDF_BYTES, Attachments
from duckai.config import Settings
from duckai.errors import ChallengeError, DuckAIError, IncompleteResponseError, RateLimitError
from duckai.models import ChatRequest, Message
from duckai.types import Attachment, ChatResponse
from duckai.upstream import DuckAIClient, close_response, iter_events

FileInput = str | Path | Attachment
MessageInput = Mapping[str, Any] | Message


class AsyncDuckAI:
    """Owns a reusable HTTP session and lazy anonymous browser session.

    Use ``async with AsyncDuckAI() as ai`` to release both automatically.
    Configuration defaults read DUCKAI_* environment variables and .env.
    """

    def __init__(
        self,
        model: str = "gpt-6-luna",
        *,
        settings: Settings | None = None,
        headless: bool | None = None,
        browser_executable: str | None = None,
        browser_cdp_url: str | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        values = (settings or Settings()).model_dump()
        overrides = {
            "duckai_browser_headless": headless,
            "duckai_browser_executable": browser_executable,
            "duckai_browser_cdp_url": browser_cdp_url,
            "upstream_read_timeout": timeout,
            "duckai_headers_json": headers,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        self.settings = Settings(_env_file=None, **values)
        if not model:
            raise ValueError("model must not be empty")
        self.model = model
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.settings.upstream_read_timeout, connect=self.settings.upstream_connect_timeout
            ),
            follow_redirects=False,
            transport=transport,
        )
        self._tokens = AnonymousTokenProvider(self._http, self.settings)
        self._upstream = DuckAIClient(self._http, self.settings, self._tokens)
        self._attachments = Attachments(self.settings)
        self._streams: set[AsyncChatStream] = set()
        self._closed = False

    def _check_open(self):
        if self._closed:
            raise RuntimeError("Duck.ai client is closed")

    async def __aenter__(self):
        self._check_open()
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            for stream in tuple(self._streams):
                await stream.aclose()
        finally:
            try:
                await self._tokens.close()
            finally:
                await self._http.aclose()

    def attachment_limits(self, model: str | None = None) -> dict[str, Any]:
        return self._attachments.limits(model or self.model)

    def _prepare_files(self, files: Sequence[FileInput], model: str) -> list[dict[str, Any]]:
        if isinstance(files, (str, Path, Attachment)):
            raise TypeError("Pass files as a sequence, for example files=[Path('report.pdf')]")
        parts = []
        limit = max(PDF_BYTES, self.settings.max_image_upload_bytes)
        for file in files:
            attachment = (
                file
                if isinstance(file, Attachment)
                else Attachment.from_path(file, max_bytes=limit)
            )
            parts.append(
                self._attachments.prepare(
                    attachment.data,
                    attachment.filename,
                    attachment.mime_type,
                    model,
                )
            )
        parts.sort(key=lambda p: p["type"] == "file")
        return parts

    async def prepare_files(
        self,
        files: Sequence[FileInput],
        *,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return validated native content parts without contacting Duck.ai."""
        self._check_open()
        selected = model or self.model
        parts = await asyncio.to_thread(self._prepare_files, files, selected)
        body = ChatRequest(model=selected, messages=[{"role": "user", "content": parts}])
        await asyncio.to_thread(self._attachments.validate_payload, body)
        return parts

    async def _request(
        self,
        prompt: str | None,
        messages: Sequence[MessageInput] | None,
        files: Sequence[FileInput] | None,
        model: str | None,
        options: dict[str, Any],
    ) -> ChatRequest:
        self._check_open()
        if prompt is not None and not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        history = [
            m.model_dump(exclude_unset=True) if isinstance(m, Message) else deepcopy(dict(m))
            for m in (messages or [])
        ]
        if prompt is not None:
            history.append({"role": "user", "content": prompt})
        if not history:
            raise ValueError("Provide a prompt or at least one message")
        selected = model or self.model
        aliases = {
            "reasoning_effort": "reasoningEffort",
            "can_use_tools": "canUseTools",
            "can_use_approx_location": "canUseApproxLocation",
            "can_delegate_image_generation": "canDelegateImageGeneration",
            "can_show_greeting": "canShowGreeting",
            "durable_stream": "durableStream",
        }
        options = dict(options)
        for python_name, wire_name in aliases.items():
            if python_name in options:
                if wire_name in options:
                    raise ValueError(f"Use either {python_name} or {wire_name}, not both")
                options[wire_name] = options.pop(python_name)
        if files:
            if history[-1].get("role") != "user":
                raise ValueError("Files must attach to the final user message")
            parts = await asyncio.to_thread(self._prepare_files, files, selected)
            content = history[-1].get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            history[-1]["content"] = content + parts
        body = ChatRequest(model=selected, messages=history, **options)
        await asyncio.to_thread(self._attachments.validate_payload, body)
        return body

    def stream(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[MessageInput] | None = None,
        files: Sequence[FileInput] | None = None,
        model: str | None = None,
        **options: Any,
    ) -> "AsyncChatStream":
        """An async context manager yielding text deltas. Access .response afterward."""
        self._check_open()
        return AsyncChatStream(self, prompt, messages, files, model, options)

    def events(self, prompt: str | None = None, **kwargs: Any) -> "AsyncChatStream":
        """Like stream(), but yields original parsed events, including tools and reasoning."""
        stream = self.stream(prompt, **kwargs)
        stream._raw_events = True
        return stream

    async def chat(self, prompt: str | None = None, **kwargs: Any) -> ChatResponse:
        async with self.stream(prompt, **kwargs) as stream:
            async for _ in stream:
                pass
        return stream.response

    def conversation(self, *, messages: Sequence[MessageInput] | None = None, **options: Any):
        """Create independent history sharing this client's browser and HTTP session."""
        from duckai.conversation import AsyncConversation

        self._check_open()
        return AsyncConversation(self, messages=messages, **options)


class AsyncChatStream:
    def __init__(self, client, prompt, messages, files, model, options):
        self.client = client
        self.response = ChatResponse(model=model or client.model)
        self._args = (prompt, messages, files, model, options)
        self._http_response = None
        self._iterator = None
        self._closed = False
        self._raw_events = False
        self.request: ChatRequest | None = None

    async def _start(self):
        if self._closed:
            raise RuntimeError("Duck.ai stream is closed")
        if self._iterator is not None:
            return
        self.request = await self.client._request(*self._args)
        self._http_response = await self.client._upstream.open_chat(
            self.request.upstream_payload(),
            httpx.Headers(),
        )
        self.client._streams.add(self)
        self._iterator = iter_events(
            self._http_response,
            self.client.settings.max_collected_bytes,
        )

    async def __aenter__(self):
        await self._start()
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        try:
            await self._start()
            while True:
                try:
                    event = await anext(self._iterator)
                except StopAsyncIteration:
                    raise IncompleteResponseError(self.response) from None
                if event.done:
                    self.response.done = True
                    await self.aclose()
                    raise StopAsyncIteration
                data = event.data
                if isinstance(data, dict) and (
                    data.get("action") == "error" or event.event in {"error", "proxy_error"}
                ):
                    status = data.get("status", 502)
                    status = status if isinstance(status, int) and 400 <= status <= 599 else 502
                    error = (
                        RateLimitError
                        if status == 429
                        else ChallengeError
                        if status in {401, 403, 418}
                        else DuckAIError
                    )
                    raise error(status, data)
                delta = self.response._append(data)
                if self._raw_events:
                    return data
                if delta:
                    return delta
        except StopAsyncIteration:
            raise
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._iterator is not None:
                await self._iterator.aclose()
        finally:
            if self._http_response is not None:
                await close_response(self._http_response)
            self.client._streams.discard(self)
