"""Synchronous facade using one persistent event loop for the browser's lifetime."""

import asyncio
import threading
from typing import Any

from duckai.client import AsyncDuckAI


class DuckAI:
    """Direct synchronous SDK. Use in one thread; async applications use AsyncDuckAI."""

    def __init__(self, model: str = "gpt-6-luna", **options: Any):
        self._client = AsyncDuckAI(model, **options)
        self._runner = asyncio.Runner()
        self._thread = threading.get_ident()
        self._closed = False

    def _check(self):
        if self._closed:
            raise RuntimeError("Duck.ai client is closed")
        if threading.get_ident() != self._thread:
            raise RuntimeError("Use DuckAI in its creating thread, or use AsyncDuckAI")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError("Use AsyncDuckAI inside an async application")

    def _run(self, method, *args, **kwargs):
        self._check()
        return self._runner.run(method(*args, **kwargs))

    @property
    def model(self):
        return self._client.model

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._closed:
            return
        self._check()
        try:
            self._run(self._client.aclose)
        finally:
            self._closed = True
            self._runner.close()

    def chat(self, prompt: str | None = None, **options: Any):
        return self._run(self._client.chat, prompt, **options)

    def stream(self, prompt: str | None = None, **options: Any):
        self._check()
        return ChatStream(self, self._client.stream(prompt, **options))

    def events(self, prompt: str | None = None, **options: Any):
        self._check()
        return ChatStream(self, self._client.events(prompt, **options))

    def prepare_files(self, files, **options: Any):
        return self._run(self._client.prepare_files, files, **options)

    def attachment_limits(self, model: str | None = None):
        self._check()
        return self._client.attachment_limits(model)

    def list_models(self, *, include_subscriber: bool = True):
        self._check()
        return self._client.list_models(include_subscriber=include_subscriber)

    def conversation(self, **options: Any):
        self._check()
        return Conversation(self, self._client.conversation(**options))


class ChatStream:
    def __init__(self, client, stream):
        self._client, self._stream = client, stream

    @property
    def response(self):
        return self._stream.response

    def __enter__(self):
        self._client._run(self._stream.__aenter__)
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return self._client._run(self._stream.__anext__)
        except StopAsyncIteration:
            raise StopIteration from None

    def close(self):
        if not self._client._closed:
            self._client._run(self._stream.aclose)


class Conversation:
    def __init__(self, client, conversation):
        self._client, self._conversation = client, conversation

    @property
    def messages(self):
        self._client._check()
        return self._conversation.messages

    def clear(self):
        self._client._check()
        self._conversation.clear()

    def chat(self, prompt: str, **options: Any):
        return self._client._run(self._conversation.chat, prompt, **options)

    def stream(self, prompt: str, **options: Any):
        self._client._check()
        return ChatStream(self._client, self._conversation.stream(prompt, **options))

    def events(self, prompt: str, **options: Any):
        self._client._check()
        return ChatStream(self._client, self._conversation.events(prompt, **options))
