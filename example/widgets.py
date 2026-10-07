"""Reusable native controls for the desktop example."""

import re
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextLength,
    QTextTableFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from example.theme import icon


def label(text, *, name="", muted=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setProperty("muted", muted)
    return widget


def tool(name, tooltip, callback=None):
    button = QToolButton()
    button.setObjectName("iconButton")
    button.setProperty("iconName", name)
    button.setIcon(icon(name))
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setFixedSize(36, 36)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        button.clicked.connect(callback)
    return button


class Greeting(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(66)
        self.setAccessibleName("Hello, curious mind.")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPixelSize(43 if self.width() >= 590 else 33)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        gradient = QLinearGradient(0, 0, 520, 0)
        gradient.setColorAt(0, QColor("#4489e7"))
        gradient.setColorAt(0.5, QColor("#9373cb"))
        gradient.setColorAt(1, QColor("#cb7781"))
        painter.setPen(QPen(QBrush(gradient), 1))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter, "Hello, curious mind.")
        painter.end()


SUGGESTIONS = [
    (
        "spark",
        "Make room for ideas",
        "Brainstorm something\nworth building",
        "#6a82c8",
        "Help me brainstorm five creative ideas for a small project I can build this weekend.",
    ),
    (
        "edit",
        "Find the right words",
        "Turn a rough thought\ninto a great first draft",
        "#af7cbd",
        "Help me write something clearly. First, ask me about my audience and what I want to say.",
    ),
    (
        "code",
        "Build something useful",
        "Understand, write,\nand improve your code",
        "#5a9c8b",
        "Teach me a useful Python concept with a practical example and a small exercise.",
    ),
    (
        "file",
        "See the bigger picture",
        "Summarize a document\nor explore an image",
        "#c38a65",
        "I would like to analyze a document or image. "
        "Ask me to attach a file and what to focus on.",
    ),
]


class Suggestion(QPushButton):
    def __init__(self, entry, callback):
        super().__init__()
        name, title, description, color, prompt = entry
        self.setObjectName("suggestion")
        self.setMinimumHeight(156)
        self.setMinimumWidth(146)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{title}. {description}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 17, 14, 13)
        symbol = QLabel()
        symbol.setPixmap(icon(name, color, 24).pixmap(24, 24))
        layout.addWidget(symbol)
        layout.addStretch()
        heading = label(title)
        heading.setWordWrap(True)
        heading.setStyleSheet("font-size: 12px; font-weight: 600;")
        layout.addWidget(heading)
        detail = label(description, muted=True)
        detail.setWordWrap(True)
        detail.setStyleSheet("font-size: 12px;")
        layout.addWidget(detail)
        # Children must pass clicks to the card.
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.clicked.connect(lambda: callback(prompt))


class PromptEdit(QPlainTextEdit):
    send = Signal()
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("prompt")
        self.setPlaceholderText("Ask anything, or give your ideas a little room…")
        self.setAccessibleName("Message to Duck.ai")
        self.setMinimumHeight(62)
        self.setMaximumHeight(170)
        self.setAcceptDrops(True)
        self.setTabChangesFocus(True)
        self.textChanged.connect(self.fit_height)

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            event.accept()
            self.send.emit()
            return
        super().keyPressEvent(event)

    def fit_height(self):
        height = int(self.document().size().height() * self.fontMetrics().lineSpacing()) + 15
        self.setFixedHeight(max(62, min(170, height)))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.files_dropped.emit(
                [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            )
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class MarkdownView(QTextBrowser):
    """Content-driven height inside a single transcript scroll area.

    Model text cannot load local files/images or execute arbitrary URL schemes.
    Native Markdown is parsed with raw HTML disabled.
    """

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(2)
        self.document().documentLayout().documentSizeChanged.connect(self.fit_height)
        self.anchorClicked.connect(self.open_link)
        self.source_text = ""
        self.colors = {}

    def loadResource(self, resource_type, url):
        # Never fetch model-provided images, local URLs, or remote stylesheets.
        return None

    @staticmethod
    def open_link(url: QUrl):
        if url.scheme() in {"https", "http"}:
            QDesktopServices.openUrl(url)

    def set_content(self, text, colors):
        self.source_text, self.colors = text, colors
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(colors["background"]))
        self.setPalette(palette)
        self.document().setDefaultStyleSheet(
            f"pre {{ background-color: {colors['code']}; white-space: pre-wrap; }} "
            f"code {{ font-family: monospace; color: {colors['accent']}; }} "
            f"a {{ color: {colors['accent']}; }} "
            "h1 { font-size: 23px; } h2 { font-size: 20px; } h3 { font-size: 17px; }"
        )
        self.document().setMarkdown(
            text or "Thinking…",
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )
        self.style_blocks()
        self.fit_height()

    def style_blocks(self):
        """Markdown parsing does not apply HTML CSS; format native blocks directly."""
        code_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        # Qt's generic "monospace" alias can resolve to a proportional font on
        # macOS. Prefer an installed concrete family across desktop platforms.
        installed_fonts = set(QFontDatabase.families())
        for family in ("Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono"):
            if family in installed_fonts:
                code_font.setFamily(family)
                break
        code_font.setPixelSize(13)
        block = self.document().begin()
        code_groups = []
        while block.isValid():
            cursor = QTextCursor(block)
            fmt = block.blockFormat()
            is_code = fmt.hasProperty(QTextFormat.Property.BlockCodeFence)
            fmt.setLineHeight(145, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            fmt.setTopMargin(8)
            fmt.setBottomMargin(8)
            if is_code:
                start = block.position()
                lines = []
                while block.isValid() and block.blockFormat().hasProperty(
                    QTextFormat.Property.BlockCodeFence
                ):
                    lines.append(block.text())
                    end = block.position() + block.length() - 1
                    block = block.next()
                code_groups.append((start, end, "\n".join(lines)))
                continue
            elif fmt.headingLevel():
                fmt.setTopMargin(14)
                fmt.setBottomMargin(12)
            elif block.textList():
                fmt.setTopMargin(3)
                fmt.setBottomMargin(3)
            cursor.setBlockFormat(fmt)
            block = block.next()
        # A native table gives each fenced block a continuous, padded background.
        # Work backwards so replacing a group does not shift remaining positions.
        for start, end, code in reversed(code_groups):
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            table_format = QTextTableFormat()
            table_format.setBorder(0)
            table_format.setCellSpacing(0)
            table_format.setCellPadding(14)
            table_format.setBackground(QColor(self.colors["code"]))
            table_format.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
            table = cursor.insertTable(1, 1, table_format)
            cell = table.cellAt(0, 0)
            cell_format = cell.format()
            cell_format.setBackground(QColor(self.colors["code"]))
            cell.setFormat(cell_format)
            code_cursor = cell.firstCursorPosition()
            char = QTextCharFormat()
            char.setFont(code_font)
            char.setForeground(QColor(self.colors["text"]))
            code_cursor.insertText(code, char)
            code_block = table.firstCursorPosition().block()
            last_position = table.lastCursorPosition().position()
            while code_block.isValid() and code_block.position() <= last_position:
                fmt = QTextBlockFormat()
                fmt.setLineHeight(135, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
                QTextCursor(code_block).setBlockFormat(fmt)
                code_block = code_block.next()

    def fit_height(self, *_):
        self.document().setTextWidth(max(100, self.viewport().width()))
        self.setFixedHeight(max(30, int(self.document().size().height()) + 12))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()


class MessageCard(QWidget):
    def __init__(self, turn, colors, *, retry=None, edit=None):
        super().__init__()
        self.turn = turn
        self.colors = colors
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 24)
        layout.setSpacing(18)
        user_row = QHBoxLayout()
        user_row.addStretch(1)
        bubble = QFrame()
        bubble.setObjectName("userBubble")
        bubble.setMaximumWidth(610)
        user_layout = QVBoxLayout(bubble)
        user_layout.setContentsMargins(18, 13, 18, 13)
        user_text = label(turn.prompt)
        user_text.setWordWrap(True)
        user_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        user_layout.addWidget(user_text)
        if turn.files:
            attachments = label("  ·  ".join(Path(p).name for p in turn.files), muted=True)
            attachments.setWordWrap(True)
            attachments.setStyleSheet("font-size: 12px;")
            user_layout.addWidget(attachments)
        user_row.addWidget(bubble, 4)
        layout.addLayout(user_row)
        reply = QHBoxLayout()
        reply.setSpacing(14)
        avatar = QLabel()
        avatar.setPixmap(icon("spark", colors["accent"], 27).pixmap(27, 27))
        avatar.setFixedWidth(32)
        reply.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        heading = label("Duck.ai", muted=False)
        heading.setStyleSheet("font-size: 13px; font-weight: 600;")
        column.addWidget(heading)
        self.body = MarkdownView()
        self.body.set_content(turn.answer, colors)
        column.addWidget(self.body)
        self.status = label("", name="messageStatus")
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        actions = QHBoxLayout()
        actions.setSpacing(1)
        self.copy = tool("copy", "Copy response", self.copy_response)
        actions.addWidget(self.copy)
        self.code = tool("code", "Copy code blocks", self.copy_code)
        actions.addWidget(self.code)
        self.retry = tool(
            "refresh",
            "Regenerate response" if turn.status == "complete" else "Retry message",
            retry,
        )
        self.retry.setVisible(retry is not None)
        actions.addWidget(self.retry)
        self.edit = tool("edit", "Edit and resend last message", edit)
        self.edit.setVisible(edit is not None)
        actions.addWidget(self.edit)
        self.meta = label(turn.model, muted=True)
        self.meta.setStyleSheet("font-size: 11px;")
        actions.addSpacing(8)
        actions.addWidget(self.meta)
        actions.addStretch()
        column.addLayout(actions)
        reply.addLayout(column, 1)
        layout.addLayout(reply)
        self.update_state()

    def update_state(self):
        text = {
            "pending": "Generating…",
            "complete": "",
            "stopped": "Stopped · Partial reply",
            "error": self.turn.error,
        }[self.turn.status]
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.copy.setEnabled(bool(self.turn.answer))
        self.code.setVisible("```" in self.turn.answer)

    def copy_response(self):
        QApplication.clipboard().setText(self.turn.answer)
        self.copy.setToolTip("Copied to clipboard")

    def copy_code(self):
        blocks = re.findall(r"```[^\n]*\n(.*?)```", self.turn.answer, re.DOTALL)
        QApplication.clipboard().setText("\n\n".join(block.rstrip() for block in blocks))


class ChatDelegate(QStyledItemDelegate):
    def __init__(self, parent, colors):
        super().__init__(parent)
        self.colors = colors

    def sizeHint(self, option, index):
        return QSize(230, 64)

    def paint(self, painter, option, index):
        from PySide6.QtWidgets import QStyle

        c = self.colors
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(0, 2, -3, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor(c["selected"]))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QColor(c["hover"]))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 10, 10)
        icon("chat", c["muted"], 17).paint(painter, rect.x() + 12, rect.y() + 16, 17, 17)
        title, _, snippet = index.data().partition("\n")
        font = QFont(option.font)
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QColor(c["text"]))
        x = rect.x() + 40
        available = rect.width() - 50
        painter.drawText(
            x,
            rect.y() + 25,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, available),
        )
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor(c["muted"]))
        painter.drawText(
            x,
            rect.y() + 44,
            painter.fontMetrics().elidedText(snippet, Qt.TextElideMode.ElideRight, available),
        )
        painter.restore()
