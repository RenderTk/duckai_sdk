"""Native read-only code cards. Snippets are displayed, copied, or saved as text."""

import bisect
from pathlib import Path

from pygments.lexers import get_lexer_by_name
from pygments.lexers.special import TextLexer
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPainter,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from example.theme import LIGHT


def fixed_font():
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    installed = set(QFontDatabase.families())
    for family in ("Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono"):
        if family in installed:
            font.setFamily(family)
            break
    font.setPixelSize(13)
    return font


class SyntaxHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.ranges = {}

    def configure(self, lexer, colors):
        self.ranges = {}
        text = self.document().toPlainText()
        # Large snippets stay selectable and exportable without costly lexing.
        if len(text) > 200_000:
            self.rehighlight()
            return
        starts = [0]
        starts.extend(i + 1 for i, character in enumerate(text) if character == "\n")
        utf16 = [0]
        for character in text:
            utf16.append(utf16[-1] + (2 if ord(character) > 0xFFFF else 1))
        style = get_style_by_name(
            "github-dark" if colors["background"] == "#15181f" else "friendly"
        )
        formats = {}
        for position, token, value in lexer.get_tokens_unprocessed(text):
            if token not in formats:
                data = style.style_for_token(token)
                fmt = QTextCharFormat()
                fmt.setForeground(QColor("#" + data["color"] if data["color"] else colors["text"]))
                if data["bold"]:
                    fmt.setFontWeight(QFont.Weight.Bold)
                fmt.setFontItalic(data["italic"])
                fmt.setFontUnderline(data["underline"])
                formats[token] = fmt
            line = bisect.bisect_right(starts, position) - 1
            for piece in value.splitlines(keepends=True):
                content = piece.removesuffix("\n")
                if content:
                    offset = utf16[position] - utf16[starts[line]]
                    length = utf16[position + len(content)] - utf16[position]
                    self.ranges.setdefault(line, []).append((offset, length, formats[token]))
                position += len(piece)
                if piece.endswith("\n"):
                    line += 1
        self.rehighlight()

    def highlightBlock(self, text):
        for start, length, fmt in self.ranges.get(self.currentBlock().blockNumber(), ()):
            self.setFormat(start, length, fmt)


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def paintEvent(self, event):
        editor = self.editor
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(editor.colors["code"]))
        painter.setFont(editor.font())
        painter.setPen(QColor(editor.colors["muted"]))
        block = editor.firstVisibleBlock()
        number = block.blockNumber()
        top = round(editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            height = round(editor.blockBoundingRect(block).height())
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(
                    0,
                    top,
                    self.width() - 10,
                    editor.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(number + 1),
                )
            top += height
            block = block.next()
            number += 1


class CodeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("codeEditor")
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(fixed_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.colors = LIGHT
        self.gutter = LineNumbers(self)
        self.blockCountChanged.connect(self.fit_height)
        self.updateRequest.connect(self.update_gutter)
        self.highlighter = SyntaxHighlighter(self.document())
        self.fit_height()

    def fit_height(self, *_):
        digits = len(str(max(1, self.blockCount())))
        self.gutter_width = 16 + self.fontMetrics().horizontalAdvance("9") * digits
        self.setViewportMargins(self.gutter_width, 0, 8, 0)
        lines = max(self.blockCount(), self.document().documentLayout().documentSize().height())
        height = round(min(20, max(1, lines)) * self.fontMetrics().lineSpacing()) + 30
        self.setFixedHeight(max(58, height))
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), self.gutter_width, rect.height()))

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()


class CodeBlock(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("codeCard")
        self.source_code = ""
        self.language = ""
        self.colors = {}
        self.lexer = TextLexer()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 10)
        layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("codeHeader")
        controls = QHBoxLayout(header)
        controls.setContentsMargins(12, 6, 9, 6)
        controls.setSpacing(3)
        self.title = QLabel("Code")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setObjectName("codeLanguage")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        controls.addWidget(self.title, 1)
        self.wrap = QPushButton("Wrap")
        self.wrap.setCheckable(True)
        self.wrap.setToolTip("Wrap long lines")
        self.wrap.toggled.connect(self.set_wrap)
        self.copy = QPushButton("Copy")
        self.copy.clicked.connect(self.copy_code)
        self.save = QPushButton("Save")
        self.save.clicked.connect(self.save_code)
        self.collapse = QPushButton("Hide")
        self.collapse.setCheckable(True)
        self.collapse.toggled.connect(self.set_collapsed)
        for button in (self.wrap, self.copy, self.save, self.collapse):
            button.setObjectName("codeAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            controls.addWidget(button)
        layout.addWidget(header)
        self.editor = CodeEditor()
        self.editor.setAccessibleName("Read-only code snippet")
        layout.addWidget(self.editor)
        self.copy_timer = QTimer(self)
        self.copy_timer.setSingleShot(True)
        self.copy_timer.timeout.connect(lambda: self.copy.setText("Copy"))

    def set_content(self, code, language, colors):
        if self.source_code == code and self.language == language and self.colors == colors:
            return
        self.language = language
        self.colors = colors
        try:
            self.lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
        except ClassNotFound:
            self.lexer = TextLexer(stripnl=False, ensurenl=False)
        title = (
            self.lexer.name if not isinstance(self.lexer, TextLexer) else language or "Plain text"
        )
        self.title.setText(title)
        self.title.setToolTip(title)
        self.setAccessibleName(f"{title} code block")
        self.copy.setAccessibleName(f"Copy {title} code")
        self.save.setAccessibleName(f"Save {title} code")
        self.wrap.setAccessibleName(f"Wrap {title} code lines")
        self.collapse.setAccessibleName(f"Collapse or expand {title} code")
        scroll = self.editor.verticalScrollBar().value()
        horizontal = self.editor.horizontalScrollBar().value()
        if code.startswith(self.source_code) and self.source_code:
            cursor = QTextCursor(self.editor.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(code[len(self.source_code) :])
        elif code != self.source_code:
            self.editor.setPlainText(code)
        self.source_code = code
        self.editor.colors = colors
        palette = self.editor.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(colors["code"]))
        self.editor.setPalette(palette)
        self.editor.highlighter.configure(self.lexer, colors)
        self.editor.fit_height()
        self.editor.verticalScrollBar().setValue(scroll)
        self.editor.horizontalScrollBar().setValue(horizontal)
        self.editor.gutter.update()
        self.copy.setEnabled(bool(code))
        self.save.setEnabled(bool(code))

    def set_wrap(self, wrap):
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.editor.fit_height()

    def set_collapsed(self, collapsed):
        self.editor.setVisible(not collapsed)
        self.collapse.setText("Show" if collapsed else "Hide")

    def copy_code(self):
        QApplication.clipboard().setText(self.source_code)
        self.copy.setText("Copied")
        self.copy_timer.start(1800)

    def save_code(self):
        pattern = self.lexer.filenames[0] if self.lexer.filenames else "*.txt"
        suffix = Path(pattern).suffix
        if not suffix or any(c in suffix for c in "*?[]"):
            suffix = ".txt"
        target, _ = QFileDialog.getSaveFileName(
            self, "Save code snippet", "snippet" + suffix, "All files (*)"
        )
        if target:
            try:
                Path(target).write_text(self.source_code, encoding="utf-8", newline="")
            except OSError as error:
                QMessageBox.warning(self, "Could not save snippet", str(error))
