"""Transactional conversation history: failed or interrupted replies never commit."""

import asyncio
from copy import deepcopy

from duckai.models import Message


class AsyncConversation:
    def __init__(self, client, *, messages=None, **options):
        self.client = client
        self._messages = [
            m.model_dump(exclude_unset=True) if isinstance(m, Message) else deepcopy(dict(m))
            for m in (messages or [])
        ]
        self._options = options
        self._lock = asyncio.Lock()

    @property
    def messages(self):
        return deepcopy(self._messages)

    def clear(self):
        if self._lock.locked():
            raise RuntimeError("Cannot clear a conversation during a chat turn")
        self._messages.clear()

    def stream(self, prompt: str, *, files=None, **options):
        return AsyncConversationStream(self, prompt, files, self._options | options)

    def events(self, prompt: str, *, files=None, **options):
        stream = self.stream(prompt, files=files, **options)
        stream._raw_events = True
        return stream

    async def chat(self, prompt: str, *, files=None, **options):
        async with self.stream(prompt, files=files, **options) as stream:
            async for _ in stream:
                pass
        return stream.response


class AsyncConversationStream:
    def __init__(self, conversation, prompt, files, options):
        self._conversation = conversation
        self._prompt, self._files, self._options = prompt, files, options
        self._inner = None
        self._locked = False
        self._closed = False
        self._raw_events = False

    @property
    def response(self):
        if self._inner is None:
            raise RuntimeError("Conversation stream has not started")
        return self._inner.response

    async def _start(self):
        if self._closed:
            raise RuntimeError("Conversation stream is closed")
        if self._inner is not None:
            return
        await self._conversation._lock.acquire()
        self._locked = True
        try:
            self._inner = self._conversation.client.stream(
                self._prompt,
                messages=self._conversation.messages,
                files=self._files,
                **self._options,
            )
            self._inner._raw_events = self._raw_events
            await self._inner.__aenter__()
            self._conversation.client._streams.add(self)
        except BaseException:
            await self.aclose()
            raise

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
        await self._start()
        try:
            return await anext(self._inner)
        except StopAsyncIteration:
            if self.response.done:
                self._conversation._messages = self._inner.request.upstream_payload()[
                    "messages"
                ] + [self.response.assistant_message]
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._inner is not None:
                await self._inner.aclose()
        finally:
            self._conversation.client._streams.discard(self)
            if self._locked:
                self._locked = False
                self._conversation._lock.release()
