"""One reusable SDK client, owned exclusively by a background asyncio loop."""

import asyncio
from copy import deepcopy

from PySide6.QtCore import QThread, Signal

from duckai import AsyncDuckAI, AttachmentError, ChallengeError, RateLimitError


def friendly_error(error: Exception) -> str:
    if isinstance(error, RateLimitError):
        wait = (
            f" Retry after {error.retry_after} seconds."
            if error.retry_after
            else " Try again later."
        )
        return "Duck.ai rate limited this session." + wait
    if isinstance(error, AttachmentError):
        return f"Attachment rejected: {error.detail}"
    if isinstance(error, ChallengeError):
        return "Duck.ai rejected the browser challenge. Check browser settings and try again."
    if "Executable doesn't exist" in str(error):
        return "Chromium is missing. Run: python -m playwright install chromium"
    return f"{type(error).__name__}: {error}"


class ChatWorker(QThread):
    ready = Signal()
    delta = Signal(str, str)
    completed = Signal(str, str, object)
    failed = Signal(str, str)
    stopped = Signal(str)

    def __init__(self, *, client_factory=AsyncDuckAI):
        super().__init__()
        self.client_factory = client_factory
        self.loop = None
        self.client = None
        self.configuration = None
        self.task = None
        self.closing = False

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.emit()
        try:
            self.loop.run_forever()
        finally:
            try:
                # Playwright's reader tasks must remain alive until browser.close()
                # receives its acknowledgement. Cancelling them first deadlocks
                # real-browser shutdown, even though mock transports still pass.
                if self.client is not None:
                    self.loop.run_until_complete(self.client.aclose())
            finally:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()

    def submit(self, chat_id, prompt, history, files, model, preferences):
        if self.loop is None or self.closing:
            raise RuntimeError("Background worker is not ready")
        # Copy everything before crossing the thread boundary.
        arguments = deepcopy((chat_id, prompt, history, files, model, preferences))
        self.loop.call_soon_threadsafe(self._start, arguments)

    def _start(self, arguments):
        if self.closing:
            return
        if self.task is not None and not self.task.done():
            self.failed.emit(arguments[0], "Another request is still running.")
            return
        self.task = self.loop.create_task(self._generate(*arguments))

    async def _generate(self, chat_id, prompt, history, files, model, preferences):
        try:
            config = {
                key: preferences[key]
                for key in ("timeout", "browser_executable", "browser_cdp_url", "headless")
            }
            if self.configuration != config:
                if self.client is not None:
                    await self.client.aclose()
                    self.client = None
                self.client = self.client_factory(
                    **{key: value for key, value in config.items() if value != ""}
                )
                self.configuration = config
            async with self.client.stream(
                prompt,
                messages=history,
                files=files,
                model=model,
                can_use_tools=preferences["tools"],
                reasoning_effort=preferences["reasoning"],
            ) as stream:
                async for chunk in stream:
                    self.delta.emit(chat_id, chunk)
            text = stream.response.text
            messages = stream.request.upstream_payload()["messages"]
            messages.append(stream.response.assistant_message)
            self.completed.emit(chat_id, text, messages)
        except asyncio.CancelledError:
            self.stopped.emit(chat_id)
        except Exception as error:
            self.failed.emit(chat_id, friendly_error(error))

    def cancel(self):
        if self.loop is not None and not self.closing:
            self.loop.call_soon_threadsafe(self._cancel)

    def _cancel(self):
        if self.task is not None:
            self.task.cancel()

    def shutdown(self):
        if self.loop is not None and not self.closing:
            self.closing = True
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._shutdown()))

    async def _shutdown(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # run() owns final cleanup, including the reusable Chromium session.
        self.loop.stop()
