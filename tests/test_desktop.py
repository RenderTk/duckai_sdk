"""Native UI integration through the real SDK with a controlled HTTP transport.

Install the desktop extra to run. Nothing in the application provides synthetic
responses; the test transport lets cancellation and failure be verified reliably.
"""

import asyncio
import io
import json
import os
import time

import httpx
import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from duckai import AsyncDuckAI, Settings  # noqa: E402
from example.app import MainWindow, SettingsDialog  # noqa: E402
from example.store import Store  # noqa: E402
from example.worker import ChatWorker  # noqa: E402


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        # Let the SDK's attachment executor and asyncio thread acquire the GIL.
        time.sleep(0.005)
    assert condition(), "Timed out waiting for UI / background worker"


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    application.setFont(QFont("SF Pro Text", 11))
    return application


class Stream(httpx.AsyncByteStream):
    def __init__(self, text, delay=0.01):
        self.text, self.delay, self.closed = text, delay, False

    async def __aiter__(self):
        for offset in range(0, len(self.text), 12):
            await asyncio.sleep(self.delay)
            data = json.dumps({"message": self.text[offset : offset + 12]})
            yield f"data: {data}\n\n".encode()
        yield b'data: {"parts":[{"type":"reasoning","encryptedText":"opaque"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


class ClientWithBrowserReader(AsyncDuckAI):
    """Model the SDK's persistent browser reader while using the real HTTP SDK."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.close_requested = asyncio.Event()
        self.close_acknowledged = asyncio.Event()
        self.reader = asyncio.create_task(self.read_browser())

    async def read_browser(self):
        await self.close_requested.wait()
        await asyncio.sleep(0)
        self.close_acknowledged.set()

    async def aclose(self):
        if not self._closed:
            self.close_requested.set()
            await asyncio.wait_for(self.close_acknowledged.wait(), timeout=1)
        await super().aclose()


@pytest.fixture
def window(app, tmp_path):
    requests, streams, clients = [], [], []
    reply = "## A useful answer\n\nA **streamed reply**.\n\n```python\nprint('hello')\n```"

    def handler(request):
        assert str(request.url) == "https://duck.ai/duckchat/v1/chat"
        body = json.loads(request.content)
        requests.append(body)
        content = body["messages"][-1]["content"]
        if content == "rate limit":
            return httpx.Response(429, json={"message": "limit"}, headers={"retry-after": "30"})
        stream = Stream(reply, delay=0.25 if content == "slow" else 0.01)
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def factory(**config):
        client = ClientWithBrowserReader(
            settings=Settings(_env_file=None, duckai_headers_json={"x-vqd-hash-1": "test"}),
            transport=httpx.MockTransport(handler),
            **config,
        )
        clients.append(client)
        return client

    worker = ChatWorker(client_factory=factory)
    widget = MainWindow(Store(tmp_path), worker=worker)
    widget.show()
    wait_for(lambda: widget.worker_ready)
    yield widget, requests, streams, clients
    widget.close()
    wait_for(lambda: not worker.isRunning())
    app.processEvents()
    assert all(client._closed for client in clients)
    assert all(client.close_acknowledged.is_set() for client in clients)
    assert all(stream.closed for stream in streams)


def send(window, text):
    window.prompt.setPlainText(text)
    QTest.keyClick(window.prompt, Qt.Key.Key_Return)
    wait_for(lambda: window.active_id is None)


def test_attachment_regeneration_edits_and_reuse(window, tmp_path):
    widget, requests, _, clients = window
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, "PNG")
    path = tmp_path / "chart.png"
    path.write_bytes(image.getvalue())
    widget.add_files([str(path)])
    send(widget, "Explain this image")
    assert widget.chat.turns[-1].status == "complete"
    assert len(widget.chat.history) == 2
    assert widget.chat.history[-1]["parts"][0]["encryptedText"] == "opaque"
    assert requests[0]["messages"][0]["content"][1]["type"] == "image"
    widget.cards[-1].copy_code()
    assert QApplication.clipboard().text() == "print('hello')"
    path.unlink()
    widget.retry()
    wait_for(lambda: widget.active_id is None)
    assert requests[1]["messages"][0] == requests[0]["messages"][0]
    assert len(widget.chat.history) == 2
    widget.edit_last()
    send(widget, "Look at this image again")
    assert len(widget.chat.turns) == 1
    assert requests[2]["messages"][0]["content"][0]["text"] == "Look at this image again"
    assert requests[2]["messages"][0]["content"][1] == requests[0]["messages"][0]["content"][1]
    send(widget, "A follow up")
    assert len(requests[3]["messages"]) == 3
    assert len(widget.chat.history) == 4
    assert len(clients) == 1  # One HTTP/browser owner for the whole desktop session.
    restored = Store(widget.store.directory)
    assert restored.chats[0].history == widget.chat.history


def test_stop_partial_navigation_and_rate_limit_retry(window):
    widget, requests, streams, _ = window
    widget.prompt.setPlainText("slow")
    widget.send()
    original = widget.chat
    wait_for(lambda: bool(original.turns[-1].answer))
    widget.new_chat()
    assert widget.chat is not original and widget.prompt.isReadOnly()
    widget.stop()
    wait_for(lambda: widget.active_id is None)
    assert original.turns[-1].status == "stopped"
    assert original.turns[-1].answer and not original.history
    assert streams[0].closed
    send(widget, "rate limit")
    assert widget.chat.turns[-1].status == "error"
    assert "30 seconds" in widget.chat.turns[-1].error
    assert widget.chat.history == []
    widget.retry()
    wait_for(lambda: widget.active_id is None)
    assert len(requests) == 3  # No automatic request retries.


def test_drafts_search_theme_and_native_screens(window, tmp_path):
    widget, _, _, _ = window
    first = widget.chat
    widget.prompt.setPlainText("A draft to keep")
    widget.new_chat()
    second = widget.chat
    widget.show_chat(first)
    assert widget.prompt.toPlainText() == "A draft to keep"
    widget.show_chat(second)
    send(widget, "A useful prompt")
    widget.search.setText("streamed reply")
    assert widget.chat_list.count() == 1
    widget.search.setText("no such text")
    assert widget.chat_list.count() == 0 and widget.no_results.isVisible()
    widget.search.clear()
    widget.toggle_theme()
    assert widget.store.preferences["theme"] == "dark"
    dialog = SettingsDialog(widget.store.preferences, tmp_path, widget)
    dialog.show()
    QApplication.processEvents()
    assert dialog.preferences()["theme"] == "dark"
    dialog.close()
    # A small desktop window must not force the main content outside the frame.
    widget.resize(1040, 720)
    QApplication.processEvents()
    assert widget.composer.width() < widget.width() - widget.sidebar.width()
    assert "print('hello')" in widget.cards[-1].body.toPlainText()
    assert widget.cards[-1].body.width() <= widget.message_column.width()
    widget.persist()
    assert Store(widget.store.directory).preferences["theme"] == "dark"
