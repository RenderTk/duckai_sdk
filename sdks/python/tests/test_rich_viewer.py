import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("markdown_it")
pytest.importorskip("pygments")

from example.code_view import CodeBlock  # noqa: E402
from example.theme import DARK, LIGHT, stylesheet  # noqa: E402
from example.widgets import MarkdownText, MarkdownView, reply_segments  # noqa: E402
from PySide6.QtCore import Qt, QUrl  # noqa: E402
from PySide6.QtGui import QTextTable  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(stylesheet(LIGHT))
    return application


def test_commonmark_fences_indentation_and_partial_streams():
    text = (
        'Intro\n\n````markdown\n```python\nprint("hello")\n```\n````\n\n'
        "~~~js\nconst n = 2;\n~~~\n\n    indented\n"
    )
    segments = reply_segments(text)
    codes = [s for s in segments if s[0] == "code"]
    assert [s[1] for s in codes] == ["markdown", "js", ""]
    assert codes[0][2] == '```python\nprint("hello")\n```\n'
    assert codes[1][2] == "const n = 2;\n"
    assert codes[2][2] == "indented\n"
    assert reply_segments("```python\nprint(")[0] == ("code", "python", "print(")
    assert reply_segments("`inline code`")[0][0] == "markdown"
    assert reply_segments("> ```python\n> x = 1\n> ```")[0][2] == "x = 1\n"


def test_streamed_code_card_reuse_actions_and_exact_export(app, monkeypatch, tmp_path):
    view = MarkdownView()
    view.resize(640, 500)
    view.set_content('Intro\n\n```python\nprint("🦆")\n', DARK)
    view.show()
    app.processEvents()
    code = view.code_blocks()[0]
    QTest.mouseClick(code.wrap, Qt.MouseButton.LeftButton)
    QTest.mouseClick(code.collapse, Qt.MouseButton.LeftButton)
    view.set_content('Intro\n\n```python\nprint("🦆")\nanswer = 42\n```\n\n**Done.**', DARK)
    assert view.code_blocks()[0] is code
    assert code.wrap.isChecked() and code.collapse.isChecked()
    assert not code.editor.isVisible()
    QTest.mouseClick(code.collapse, Qt.MouseButton.LeftButton)
    QTest.mouseClick(code.copy, Qt.MouseButton.LeftButton)
    expected = 'print("🦆")\nanswer = 42\n'
    assert QApplication.clipboard().text() == expected
    assert code.copy.text() == "Copied"
    target = tmp_path / "snippet.py"
    monkeypatch.setattr(
        "example.code_view.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(target), "All files (*)"),
    )
    code.save_code()
    assert target.read_bytes() == expected.encode("utf-8")
    assert code.editor.isReadOnly()
    assert "Done." in view.toPlainText()
    view.close()


def test_highlighting_unicode_unknown_languages_and_large_code(app):
    block = CodeBlock()
    block.set_content('name = "🦆"; count = 42\nvalue = """first\nsecond"""\n', "python", DARK)
    formats = block.editor.highlighter.ranges[0]
    number = next((start, length, fmt) for start, length, fmt in formats if start == 21)
    assert number[1] == 2  # Qt offsets count the emoji as two UTF-16 units.
    colors = {fmt.foreground().color().name() for _, _, fmt in formats}
    assert len(colors) > 1
    assert block.editor.highlighter.ranges[2]  # Multiline strings retain lexer state.
    old_colors = colors
    block.set_content(block.source_code, "python", LIGHT)
    assert {
        f.foreground().color().name() for _, _, f in block.editor.highlighter.ranges[0]
    } != old_colors
    block.set_content('<script>alert("display only")</script>', "unknown-language", LIGHT)
    assert block.editor.toPlainText() == block.source_code
    assert block.title.text() == "unknown-language"
    block.set_content("x" * 200_001, "python", LIGHT)
    assert not block.editor.highlighter.ranges
    assert len(block.source_code) == 200_001


def test_long_lines_scroll_wrap_and_collapse(app):
    block = CodeBlock()
    block.resize(540, 200)
    block.set_content("result = " + repr("a" * 240) + "\n" + "line\n" * 35, "python", LIGHT)
    block.show()
    app.processEvents()
    assert block.editor.horizontalScrollBar().maximum() > 0
    assert block.editor.verticalScrollBar().maximum() > 0
    assert block.editor.height() < 450
    QTest.mouseClick(block.wrap, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert block.editor.horizontalScrollBar().maximum() == 0
    expanded = block.sizeHint().height()
    QTest.mouseClick(block.collapse, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert block.sizeHint().height() < expanded
    block.close()


def test_markdown_tables_formatting_and_resource_isolation(app, monkeypatch):
    view = MarkdownText()
    view.resize(640, 500)
    view.set_content(
        "## Heading\n\nNormal **bold** and `inline code`.\n\n"
        "| A | B |\n| --- | --- |\n| one | two |\n| three | four |\n\n"
        "> A quotation\n\n![image](file:///private/secret.png)\n\n"
        '<script>alert("do not execute")</script>',
        LIGHT,
    )
    tables = [f for f in view.document().rootFrame().childFrames() if isinstance(f, QTextTable)]
    assert len(tables) == 1 and tables[0].rows() == 3
    assert tables[0].cellAt(0, 0).format().background().color().name() == LIGHT["hover"]
    assert tables[0].cellAt(1, 0).format().background().color().name() == LIGHT["surface"]
    assert view.loadResource(2, QUrl("file:///private/secret.png")) is None
    opened = []
    monkeypatch.setattr("example.widgets.QDesktopServices.openUrl", lambda url: opened.append(url))
    view.open_link(QUrl("javascript:alert(1)"))
    view.open_link(QUrl("file:///private/secret.png"))
    assert not opened
    view.open_link(QUrl("https://duck.ai/"))
    assert len(opened) == 1
    assert "Normal bold" in view.toPlainText()
