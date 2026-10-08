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
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from duckai import AsyncDuckAI, Model, Settings, list_models  # noqa: E402  # noqa: E402
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


def test_office_export_replay_edit_and_text_only_model(window, tmp_path):
    widget, requests, _, _ = window
    widget.model.choose(Model.MISTRAL_SMALL_4)
    path = tmp_path / "budget.csv"
    path.write_text("item,amount\nWidget,42\n")
    widget.add_files([str(path)])
    assert widget.chat.draft_files == [str(path)]
    send(widget, "Summarize this spreadsheet")
    assert widget.chat.turns[-1].status == "complete"
    content = requests[0]["messages"][0]["content"]
    assert "Widget,42" in content[1]["text"]
    path.unlink()
    widget.retry()
    wait_for(lambda: widget.active_id is None)
    assert requests[1]["messages"][0]["content"] == content
    widget.edit_last()
    send(widget, "What is the amount?")
    assert requests[2]["messages"][0]["content"][0]["text"] == "What is the amount?"
    assert requests[2]["messages"][0]["content"][1] == content[1]


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


def test_composer_picker_routes_every_free_model_and_restores_selection(window, monkeypatch):
    widget, requests, _, clients = window
    assert widget.model.parent() is widget.composer
    for info in list_models(include_subscriber=False):
        widget.model.open_popup()
        row = widget.model.popup.rows[info.id]
        QTest.mouseClick(row, Qt.MouseButton.LeftButton)
        assert not widget.model.popup.isVisible()
        assert widget.chat.model == info.id
        assert widget.model.popup.checks[info.id].text() == "✓"
        send(widget, "Hello from the selected model")
        assert requests[-1]["model"] == info.id
        assert requests[-1]["reasoningEffort"] in info.reasoning_efforts
        assert requests[-1]["canUseTools"] == info.supports_tools
        assert widget.chat.turns[-1].model == info.id
    assert len(clients) == 1
    for info in list_models():
        assert widget.model.popup.rows[info.id].isEnabled() == (info.access_tier == "free")
    widget.persist()
    restored = Store(widget.store.directory)
    assert restored.preferences["model"] == Model.GEMMA_4_31B
    assert restored.chats[0].model == Model.GEMMA_4_31B
    widget.model.open_popup()
    row = widget.model.popup.rows[Model.GEMMA_4_31B]
    QTest.keyClick(row, Qt.Key.Key_Home)
    first = widget.model.popup.rows[Model.GPT_6_LUNA]
    assert first.hasFocus()
    QTest.keyClick(first, Qt.Key.Key_Escape)
    assert not widget.model.popup.isVisible()
    assert widget.chat.model == Model.GEMMA_4_31B
    widget.model.open_popup()
    QTest.keyClick(row, Qt.Key.Key_Home)
    QTest.keyClick(first, Qt.Key.Key_Down)
    second = widget.model.popup.rows[Model.GPT_5_4_MINI]
    assert second.hasFocus()
    QTest.keyClick(second, Qt.Key.Key_Return)
    assert widget.chat.model == Model.GPT_5_4_MINI
    assert not widget.model.popup.isVisible()
    monkeypatch.setattr(
        "example.model_picker.QInputDialog.getText", lambda *args, **kwargs: ("future-model", True)
    )
    widget.model.popup.custom_model()
    assert widget.chat.model == "future-model"


def test_picker_attachment_rules_and_disabled_during_streaming(window, tmp_path):
    widget, _, _, _ = window
    path = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(path)
    widget.model.choose(Model.MISTRAL_SMALL_4)
    assert widget.attach_button.isEnabled()
    assert widget.attachment_hint.text() == "Documents"
    widget.add_files([str(path)])
    assert not widget.chat.draft_files
    widget.model.choose(Model.GEMMA_4_31B)
    assert widget.attach_button.isEnabled()
    widget.add_files([str(tmp_path / "file.pdf")])
    assert not widget.chat.draft_files
    widget.model.choose(Model.GPT_6_LUNA)
    widget.prompt.setPlainText("slow")
    widget.send()
    wait_for(lambda: bool(widget.chat.turns[-1].answer))
    assert not widget.model.isEnabled() and not widget.model.popup.isVisible()
    widget.stop()
    wait_for(lambda: widget.active_id is None)
    assert widget.model.isEnabled()


def test_visible_theme_switch_and_unsupported_premium_models(window, monkeypatch):
    widget, requests, _, _ = window
    for theme in ("dark", "light"):
        QTest.mouseClick(widget.theme_buttons[theme], Qt.MouseButton.LeftButton)
        assert widget.store.preferences["theme"] == theme
        assert widget.theme_buttons[theme].isChecked()
        widget.persist()
        assert Store(widget.store.directory).preferences["theme"] == theme
    widget.model.open_popup()
    assert widget.model.popup.premium_notice.isVisible()
    assert (
        "aren't supported yet" in widget.model.popup.premium_notice.findChildren(QLabel)[0].text()
    )
    widget.model.popup.hide()
    send(widget, "A free-model reply")
    previous_answer = widget.chat.turns[-1].answer
    widget.model.setCurrentText(
        Model.GPT_5_6_TERRA
    )  # Imported/old workspace or manual configuration.
    widget.model_changed()
    widget.prompt.setPlainText("Keep this draft")
    assert widget.premium_warning.isVisible()
    assert not widget.send_button.isEnabled()
    assert "aren't supported yet" in widget.send_button.toolTip()
    QTest.keyClick(widget.prompt, Qt.Key.Key_Return)
    widget.retry()
    assert len(requests) == 1
    assert widget.prompt.toPlainText() == "Keep this draft"
    assert widget.chat.turns[-1].answer == previous_answer
    widget.model.choose(Model.GPT_6_LUNA)
    assert not widget.premium_warning.isVisible() and widget.send_button.isEnabled()
    messages = []
    monkeypatch.setattr(
        "example.app.QMessageBox.information",
        lambda parent, title, message: messages.append(message),
    )
    dialog = SettingsDialog(widget.store.preferences, widget.store.directory, widget)
    dialog.model.setText(Model.GPT_5_6_SOL)
    dialog.validate()
    assert messages and "aren't supported yet" in messages[0]
    assert dialog.result() != dialog.DialogCode.Accepted
