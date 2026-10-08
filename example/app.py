"""Gemini-inspired native desktop shell around the Duck.ai SDK."""

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from duckai import get_model
from duckai.documents import EXPORT_GUIDANCE, file_kind, supported_file_extensions
from example.model_picker import PREMIUM_MESSAGE, ModelPicker
from example.store import Chat, Store, Turn
from example.theme import DARK, LIGHT, icon, stylesheet
from example.widgets import (
    SUGGESTIONS,
    ChatDelegate,
    Greeting,
    MessageCard,
    PromptEdit,
    Suggestion,
    label,
    tool,
)
from example.worker import ChatWorker


class SettingsDialog(QDialog):
    def __init__(self, preferences, directory, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings · Duck.ai Desktop")
        self.setMinimumWidth(540)
        self.values = preferences
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(18)
        title = label("Make it yours")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        layout.addWidget(title)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        appearance = QWidget()
        form = QFormLayout(appearance)
        form.setContentsMargins(16, 22, 16, 22)
        form.setVerticalSpacing(16)
        self.theme = QComboBox()
        self.theme.addItems(["light", "dark", "system"])
        self.theme.setCurrentText(preferences["theme"])
        form.addRow("Appearance", self.theme)
        self.model = QLineEdit(preferences["model"])
        form.addRow("Default model ID", self.model)
        info = label(
            "Choose a model from the button in the message composer.\n"
            "Model availability is controlled by Duck.ai.",
            muted=True,
        )
        info.setWordWrap(True)
        form.addRow(info)
        tabs.addTab(appearance, "Appearance")
        response = QWidget()
        form = QFormLayout(response)
        form.setContentsMargins(16, 22, 16, 22)
        form.setVerticalSpacing(16)
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600)
        self.timeout.setSuffix(" seconds")
        self.timeout.setValue(preferences["timeout"])
        form.addRow("Response timeout", self.timeout)
        self.tools = QCheckBox("Allow Duck.ai tools")
        self.tools.setChecked(preferences["tools"])
        form.addRow(self.tools)
        self.reasoning = QLineEdit(preferences["reasoning"])
        self.reasoning.setPlaceholderText("none")
        form.addRow("Reasoning effort", self.reasoning)
        note = label(
            "Tool and reasoning support varies by model.\n"
            "Unsupported options may be rejected by Duck.ai.",
            muted=True,
        )
        form.addRow(note)
        tabs.addTab(response, "Responses")
        connection = QWidget()
        form = QFormLayout(connection)
        form.setContentsMargins(16, 22, 16, 22)
        form.setVerticalSpacing(16)
        self.executable = QLineEdit(preferences["browser_executable"])
        self.executable.setPlaceholderText("Use Playwright’s Chromium (default)")
        form.addRow("Chromium executable", self.executable)
        self.cdp = QLineEdit(preferences["browser_cdp_url"])
        self.cdp.setPlaceholderText("Optional · http://127.0.0.1:9222")
        form.addRow("Browser CDP URL", self.cdp)
        self.headless = QCheckBox("Run Chromium silently in the background")
        self.headless.setChecked(preferences["headless"])
        form.addRow(self.headless)
        note = label(
            "The SDK handles the browser challenge automatically using headless Chromium. "
            "It reuses that background session until this app closes. "
            "Disable this only to show the browser for debugging. No API key is required.",
            muted=True,
        )
        note.setWordWrap(True)
        form.addRow(note)
        tabs.addTab(connection, "Connection")
        storage = QWidget()
        form = QVBoxLayout(storage)
        form.setContentsMargins(16, 22, 16, 22)
        note = label(
            "Chats, drafts, settings, and prepared attachments are stored locally "
            "in a plain JSON file. Prompts and attached files are sent to Duck.ai "
            "when you send a live message. JSON exports include full SDK history "
            "and encoded attachments; Markdown exports contain visible text.",
            muted=True,
        )
        note.setWordWrap(True)
        form.addWidget(note)
        path = label(str(directory))
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addWidget(path)
        form.addStretch()
        tabs.addTab(storage, "Data")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        if not self.model.text().strip():
            QMessageBox.information(self, "Model required", "Enter a default model ID.")
            return
        info = get_model(self.model.text().strip())
        if info and info.access_tier != "free":
            QMessageBox.information(self, "Premium model unavailable", PREMIUM_MESSAGE)
            return
        self.accept()

    def preferences(self):
        return {
            "theme": self.theme.currentText(),
            "model": self.model.text().strip(),
            "timeout": self.timeout.value(),
            "tools": self.tools.isChecked(),
            "reasoning": self.reasoning.text().strip() or "none",
            "browser_executable": self.executable.text().strip(),
            "browser_cdp_url": self.cdp.text().strip(),
            "headless": self.headless.isChecked(),
        }


class MainWindow(QMainWindow):
    def __init__(self, store: Store, *, worker=None):
        super().__init__()
        self.store = store
        self.chat = None
        self.active_id = None
        self.worker_ready = False
        self.closing = False
        self.editing_id = None
        self.cards = []
        self.dirty_response = False
        self.colors = LIGHT
        self.setWindowTitle("Duck.ai Desktop")
        self.setWindowIcon(icon("spark", "#5685d4", 64))
        self.resize(1200, 850)
        self.setMinimumSize(1040, 720)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self.persist)
        self.render_timer = QTimer(self)
        self.render_timer.setInterval(60)
        self.render_timer.timeout.connect(self.render_delta)
        self.render_timer.start()
        self.streaming_save_timer = QTimer(self)
        self.streaming_save_timer.setInterval(2000)
        self.streaming_save_timer.timeout.connect(self.save_streaming)
        self.streaming_save_timer.start()
        self.build_ui()
        self.build_menu()
        self.apply_theme()
        self.worker = worker or ChatWorker()
        self.worker.ready.connect(self.on_ready)
        self.worker.delta.connect(self.on_delta)
        self.worker.completed.connect(self.on_complete)
        self.worker.failed.connect(self.on_error)
        self.worker.stopped.connect(self.on_stopped)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()
        if not store.chats:
            store.chats.append(Chat(model=store.preferences["model"]))
        self.show_chat(sorted(store.chats, key=lambda c: c.updated, reverse=True)[0])
        if store.warning:
            self.notify(store.warning, persistent=True)

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(270)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(18, 25, 18, 18)
        side.setSpacing(15)
        brand_row = QHBoxLayout()
        self.brand_icon = QLabel()
        self.brand_icon.setPixmap(icon("spark", "#5685d4", 26).pixmap(26, 26))
        brand_row.addWidget(self.brand_icon)
        brand_row.addWidget(label("Duck.ai", name="brand"))
        brand_row.addStretch()
        brand_row.addWidget(tool("menu", "Hide sidebar", self.toggle_sidebar))
        side.addLayout(brand_row)
        desktop = label("YOUR DESKTOP COMPANION", name="eyebrow")
        side.addWidget(desktop)
        side.addSpacing(14)
        self.new_button = QPushButton("  New chat")
        self.new_button.setObjectName("newChat")
        self.new_button.setProperty("iconName", "plus")
        self.new_button.setIcon(icon("plus"))
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(self.new_chat)
        side.addWidget(self.new_button)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search conversations")
        self.search.setAccessibleName("Search conversations")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh_sidebar)
        side.addWidget(self.search)
        section_row = QHBoxLayout()
        section_row.addWidget(label("RECENT CONVERSATIONS", name="section"))
        section_row.addStretch()
        side.addLayout(section_row)
        self.chat_list = QListWidget()
        self.chat_list.setMouseTracking(True)
        self.chat_list.setSpacing(2)
        self.chat_list.setAccessibleName("Conversation history")
        self.chat_list.currentItemChanged.connect(self.select_item)
        self.chat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_list.customContextMenuRequested.connect(self.sidebar_menu)
        self.chat_list.itemDoubleClicked.connect(lambda _: self.rename_chat())
        side.addWidget(self.chat_list, 1)
        self.no_results = label("No matching conversations", muted=True)
        self.no_results.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_results.hide()
        side.addWidget(self.no_results)
        bottom = QPushButton("  Settings & preferences")
        bottom.setProperty("iconName", "settings")
        bottom.setIcon(icon("settings"))
        bottom.clicked.connect(self.settings)
        side.addWidget(bottom)
        foot = label("Built with the Duck.ai Python SDK", muted=True)
        foot.setStyleSheet("font-size: 10px; padding-left: 12px;")
        side.addWidget(foot)
        row.addWidget(self.sidebar)
        canvas = QWidget()
        canvas.setObjectName("canvas")
        main = QVBoxLayout(canvas)
        main.setContentsMargins(30, 23, 30, 17)
        main.setSpacing(0)
        header = QHBoxLayout()
        header.setSpacing(12)
        self.show_sidebar = tool("menu", "Show sidebar", self.toggle_sidebar)
        self.show_sidebar.hide()
        header.addWidget(self.show_sidebar)
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        self.header_title = label("Duck.ai", name="headerTitle")
        self.header_title.setMaximumWidth(470)
        title_col.addWidget(self.header_title)
        header.addLayout(title_col)
        header.addStretch()
        self.connection_label = label("ANONYMOUS SESSION", name="eyebrow")
        header.addWidget(self.connection_label)
        header.addSpacing(8)
        self.theme_switch = QFrame()
        self.theme_switch.setObjectName("themeSwitch")
        self.theme_switch.setAccessibleName("Color theme")
        theme_layout = QHBoxLayout(self.theme_switch)
        theme_layout.setContentsMargins(3, 3, 3, 3)
        theme_layout.setSpacing(2)
        self.theme_group = QButtonGroup(self)
        self.theme_buttons = {}
        for value, title, symbol in (("light", "Light", "sun"), ("dark", "Dark", "moon")):
            button = QPushButton(title)
            button.setObjectName("themeChoice")
            button.setProperty("iconName", symbol)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"Switch to {value} mode")
            button.setAccessibleName(f"{title} mode")
            button.clicked.connect(lambda checked=False, theme=value: self.set_theme(theme))
            self.theme_group.addButton(button)
            self.theme_buttons[value] = button
            theme_layout.addWidget(button)
        header.addWidget(self.theme_switch)
        header.addSpacing(4)
        header.addWidget(tool("more", "Conversation options", self.header_menu))
        main.addLayout(header)
        main.addSpacing(14)
        self.notice = label("", name="notice")
        self.notice.setWordWrap(True)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notice.hide()
        main.addWidget(self.notice)
        self.stack = QStackedWidget()
        main.addWidget(self.stack, 1)
        home = QWidget()
        home_layout = QVBoxLayout(home)
        home_layout.setContentsMargins(0, 0, 0, 0)
        home_layout.addStretch(3)
        welcome = QWidget()
        welcome.setMaximumWidth(800)
        welcome_layout = QVBoxLayout(welcome)
        welcome_layout.setContentsMargins(12, 0, 12, 0)
        welcome_layout.setSpacing(12)
        eyebrow = label("A LITTLE CURIOSITY GOES A LONG WAY", name="eyebrow")
        welcome_layout.addWidget(eyebrow)
        welcome_layout.addWidget(Greeting())
        welcome_layout.addWidget(label("What would you like to explore today?", name="subtitle"))
        welcome_layout.addSpacing(26)
        suggestions = QHBoxLayout()
        suggestions.setSpacing(12)
        for entry in SUGGESTIONS:
            suggestions.addWidget(Suggestion(entry, self.use_suggestion))
        welcome_layout.addLayout(suggestions)
        welcome_layout.addSpacing(11)
        hint = label("Think out loud. Start small. Make something yours.", muted=True)
        hint.setStyleSheet("font-size: 12px;")
        welcome_layout.addWidget(hint)
        welcome_row = QHBoxLayout()
        welcome_row.addStretch()
        welcome_row.addWidget(welcome, 20)
        welcome_row.addStretch()
        home_layout.addLayout(welcome_row)
        home_layout.addStretch(4)
        self.stack.addWidget(home)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setObjectName("transcript")
        transcript = QWidget()
        transcript_row = QHBoxLayout(transcript)
        transcript_row.setContentsMargins(0, 16, 0, 14)
        transcript_row.addStretch()
        self.message_column = QWidget()
        self.message_column.setMaximumWidth(800)
        self.message_column.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.messages_layout = QVBoxLayout(self.message_column)
        self.messages_layout.setContentsMargins(6, 0, 6, 0)
        self.messages_layout.setSpacing(0)
        transcript_row.addWidget(self.message_column, 20)
        transcript_row.addStretch()
        self.scroll.setWidget(transcript)
        self.stack.addWidget(self.scroll)
        self.jump = QPushButton("↓  Jump to latest")
        self.jump.clicked.connect(self.scroll_bottom)
        self.jump.setFixedHeight(30)
        self.jump.hide()
        main.addWidget(self.jump, 0, Qt.AlignmentFlag.AlignHCenter)
        self.scroll.verticalScrollBar().valueChanged.connect(self.update_jump)
        self.scroll.verticalScrollBar().rangeChanged.connect(self.update_jump)
        composer_row = QHBoxLayout()
        composer_row.addStretch()
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        self.composer.setMaximumWidth(800)
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(18, 16, 13, 10)
        composer_layout.setSpacing(8)
        self.cancel_edit_button = QPushButton("Editing the last message · Cancel")
        self.cancel_edit_button.setObjectName("fileChip")
        self.cancel_edit_button.clicked.connect(self.cancel_edit)
        self.cancel_edit_button.hide()
        composer_layout.addWidget(self.cancel_edit_button)
        self.files_widget = QWidget()
        self.files_layout = QGridLayout(self.files_widget)
        self.files_layout.setContentsMargins(0, 0, 0, 0)
        self.files_widget.hide()
        composer_layout.addWidget(self.files_widget)
        self.prompt = PromptEdit()
        self.prompt.send.connect(self.send)
        self.prompt.textChanged.connect(self.draft_changed)
        self.prompt.files_dropped.connect(self.add_files)
        composer_layout.addWidget(self.prompt)
        controls = QHBoxLayout()
        self.attach_button = tool(
            "attach",
            "Attach documents, spreadsheets, slides, PDFs or images · You can also drop files",
            self.choose_files,
        )
        controls.addWidget(self.attach_button)
        self.attachment_hint = label("Documents & images", muted=True)
        controls.addWidget(self.attachment_hint)
        controls.addStretch()
        self.character_count = label("", muted=True)
        self.character_count.setStyleSheet("font-size: 11px;")
        controls.addWidget(self.character_count)
        controls.addSpacing(10)
        self.model = ModelPicker(self.store.preferences["model"])
        self.model.changed.connect(self.model_changed)
        controls.addWidget(self.model)
        controls.addSpacing(5)
        self.send_button = tool("send", "Send message · Enter", self.send_or_stop)
        self.send_button.setObjectName("send")
        self.send_button.setFixedSize(40, 40)
        controls.addWidget(self.send_button)
        composer_layout.addLayout(controls)
        self.premium_warning = label(PREMIUM_MESSAGE, name="premiumWarning")
        self.premium_warning.setWordWrap(True)
        self.premium_warning.hide()
        composer_layout.addWidget(self.premium_warning)
        composer_row.addWidget(self.composer, 20)
        composer_row.addStretch()
        main.addLayout(composer_row)
        main.addSpacing(11)
        disclaimer = label(
            "Duck.ai can make mistakes. Check important information.  ·  "
            "Enter to send, Shift + Enter for a new line",
            muted=True,
        )
        disclaimer.setStyleSheet("font-size: 10px;")
        disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main.addWidget(disclaimer)
        row.addWidget(canvas, 1)

    def build_menu(self):
        file_menu = self.menuBar().addMenu("File")
        self.action(file_menu, "New chat", self.new_chat, QKeySequence.StandardKey.New)
        self.action(file_menu, "Import conversation…", self.import_chat, "Ctrl+O")
        self.action(file_menu, "Export conversation…", self.export_chat, "Ctrl+S")
        file_menu.addSeparator()
        self.action(file_menu, "Settings…", self.settings, "Ctrl+,")
        self.action(file_menu, "Quit", self.close, QKeySequence.StandardKey.Quit)
        edit = self.menuBar().addMenu("Edit")
        self.action(edit, "Search conversations", self.focus_search, QKeySequence.StandardKey.Find)
        self.action(edit, "Focus message", self.prompt.setFocus, "Ctrl+L")
        self.action(edit, "Stop response", self.stop, "Esc")
        view = self.menuBar().addMenu("View")
        self.action(view, "Toggle sidebar", self.toggle_sidebar, "Ctrl+B")
        self.action(view, "Toggle light / dark", self.toggle_theme, "Ctrl+Shift+D")
        help_menu = self.menuBar().addMenu("Help")
        self.action(help_menu, "About Duck.ai Desktop", self.about)

    def action(self, menu, title, callback, shortcut=None):
        action = QAction(title, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(callback)
        menu.addAction(action)

    def apply_theme(self):
        theme = self.store.preferences["theme"]
        dark = theme == "dark" or (
            theme == "system" and QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        )
        self.colors = DARK if dark else LIGHT
        c = self.colors
        palette = QApplication.palette()
        for role, color in {
            QPalette.ColorRole.Window: c["background"],
            QPalette.ColorRole.Base: c["background"],
            QPalette.ColorRole.AlternateBase: c["surface"],
            QPalette.ColorRole.WindowText: c["text"],
            QPalette.ColorRole.Text: c["text"],
            QPalette.ColorRole.Button: c["surface"],
            QPalette.ColorRole.ButtonText: c["text"],
            QPalette.ColorRole.Highlight: c["selected"],
            QPalette.ColorRole.HighlightedText: c["text"],
            QPalette.ColorRole.PlaceholderText: c["muted"],
        }.items():
            palette.setColor(role, QColor(color))
        QApplication.instance().setPalette(palette)
        QApplication.instance().setStyleSheet(stylesheet(c))
        self.theme_buttons["dark" if dark else "light"].setChecked(True)
        for button in self.findChildren(QPushButton) + self.findChildren(type(self.attach_button)):
            name = button.property("iconName")
            if name:
                button.setIcon(icon(name, c["muted"]))
        self.new_button.setIcon(icon("plus", c["accent"]))
        self.chat_list.setItemDelegate(ChatDelegate(self.chat_list, c))
        if self.chat:
            self.render_chat()
        self.update_controls()

    def toggle_theme(self):
        self.set_theme("light" if self.colors is DARK else "dark")

    def set_theme(self, theme):
        self.store.preferences["theme"] = theme
        self.apply_theme()
        self.schedule_save()

    def toggle_sidebar(self):
        self.sidebar.setVisible(not self.sidebar.isVisible())
        self.show_sidebar.setVisible(not self.sidebar.isVisible())

    def focus_search(self):
        self.sidebar.show()
        self.show_sidebar.hide()
        self.search.setFocus()
        self.search.selectAll()

    def find_chat(self, chat_id):
        return next((c for c in self.store.chats if c.id == chat_id), None)

    def refresh_sidebar(self, *_):
        query = self.search.text().casefold().strip()
        with QSignalBlocker(self.chat_list):
            self.chat_list.clear()
            for chat in sorted(self.store.chats, key=lambda c: (c.pinned, c.updated), reverse=True):
                search_text = (
                    chat.title + " " + " ".join(t.prompt + " " + t.answer for t in chat.turns)
                )
                if query and query not in search_text.casefold():
                    continue
                snippet = (
                    chat.turns[-1].prompt.replace("\n", " ") if chat.turns else "A fresh start"
                )
                title = ("★  " if chat.pinned else "") + chat.title
                if chat.id == self.active_id:
                    snippet = "Generating a response…"
                item = QListWidgetItem(title + "\n" + snippet)
                item.setData(Qt.ItemDataRole.UserRole, chat.id)
                item.setToolTip(chat.title + "\n" + chat.updated)
                self.chat_list.addItem(item)
                if self.chat and chat.id == self.chat.id:
                    self.chat_list.setCurrentItem(item)
        self.no_results.setVisible(self.chat_list.count() == 0)

    def select_item(self, item, _previous):
        if item:
            chat = self.find_chat(item.data(Qt.ItemDataRole.UserRole))
            if chat and chat is not self.chat:
                self.show_chat(chat)

    def show_chat(self, chat):
        self.chat = chat
        self.editing_id = None
        self.cancel_edit_button.hide()
        with QSignalBlocker(self.prompt):
            self.prompt.setPlainText(chat.draft)
        self.prompt.fit_height()
        with QSignalBlocker(self.model):
            self.model.setCurrentText(chat.model)
        self.update_header()
        self.refresh_files()
        self.render_chat()
        self.refresh_sidebar()
        self.update_controls()
        QTimer.singleShot(0, self.update_header)
        QTimer.singleShot(0, self.scroll_bottom)
        self.prompt.setFocus()

    def update_header(self):
        title = self.chat.title if self.chat and self.chat.turns else "Duck.ai"
        metrics = self.header_title.fontMetrics()
        width = min(440, self.header_title.width()) if self.isVisible() else 440
        self.header_title.setText(metrics.elidedText(title, Qt.TextElideMode.ElideRight, width))
        self.header_title.setToolTip(title)
        self.setWindowTitle(f"{title} · Duck.ai Desktop")

    def new_chat(self):
        if self.chat and not self.chat.turns and not self.chat.draft and not self.chat.draft_files:
            self.prompt.setFocus()
            return
        self.search.clear()
        chat = Chat(model=self.store.preferences["model"])
        self.store.chats.append(chat)
        self.show_chat(chat)
        self.schedule_save()

    def model_changed(self):
        model = self.model.currentText().strip()
        if not self.chat:
            return
        if not model:
            self.model.setCurrentText(self.chat.model)
            return
        self.chat.model = model
        self.store.preferences["model"] = model
        info = get_model(model)
        if info and self.store.preferences["reasoning"] not in info.reasoning_efforts:
            self.store.preferences["reasoning"] = info.default_reasoning_effort
        self.update_controls()
        self.schedule_save()

    def draft_changed(self):
        if self.chat:
            self.chat.draft = self.prompt.toPlainText()
            self.schedule_save()
        self.update_controls()

    def use_suggestion(self, prompt):
        self.prompt.setPlainText(prompt)
        self.prompt.setFocus()
        self.prompt.moveCursor(self.prompt.textCursor().MoveOperation.End)

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Attach files",
            "",
            "Supported files ("
            + " ".join("*" + ext for ext in supported_file_extensions())
            + ");;All files (*)",
        )
        self.add_files(paths)

    def add_files(self, paths):
        if not self.chat or self.active_id:
            return
        rejected = []
        for value in paths:
            path = Path(value).expanduser().resolve()
            info = get_model(self.chat.model)
            kind = file_kind(path.name)
            if path.suffix.lower() in EXPORT_GUIDANCE:
                rejected.append(path.name + " (" + EXPORT_GUIDANCE[path.suffix.lower()] + ")")
            elif info and (
                (kind == "image" and not info.supports_images)
                or (kind == "pdf" and not info.supports_pdf)
            ):
                rejected.append(path.name + f" ({info.name} does not support this attachment)")
            elif kind is None:
                rejected.append(path.name + " (unsupported format)")
            elif not path.is_file():
                rejected.append(path.name + " (not a file)")
            elif str(path) not in self.chat.draft_files:
                same_kind = sum(file_kind(p) == kind for p in self.chat.draft_files)
                if kind != "document" and same_kind >= 3:
                    rejected.append(path.name + " (attach up to three PDFs and three images)")
                else:
                    self.chat.draft_files.append(str(path))
        if rejected:
            self.notify("Could not attach: " + ", ".join(rejected))
        self.refresh_files()
        self.update_controls()
        self.schedule_save()

    def remove_file(self, path):
        if path in self.chat.draft_files:
            self.chat.draft_files.remove(path)
        self.refresh_files()
        self.update_controls()
        self.schedule_save()

    def refresh_files(self):
        while self.files_layout.count():
            item = self.files_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, path in enumerate(self.chat.draft_files):
            chip = QPushButton(Path(path).name[:28] + "  ×")
            chip.setObjectName("fileChip")
            chip.setToolTip(f"Remove attachment\n{path}")
            chip.clicked.connect(lambda _=False, p=path: self.remove_file(p))
            chip.setEnabled(not self.active_id)
            self.files_layout.addWidget(chip, index // 3, index % 3)
        self.files_widget.setVisible(bool(self.chat.draft_files))

    def render_chat(self):
        while self.messages_layout.count():
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = []
        self.stack.setCurrentIndex(1 if self.chat.turns else 0)
        for index, turn in enumerate(self.chat.turns):
            last = index == len(self.chat.turns) - 1
            card = MessageCard(
                turn,
                self.colors,
                retry=self.retry if last else None,
                edit=self.edit_last if last else None,
            )
            self.cards.append(card)
            self.messages_layout.addWidget(card)
        self.messages_layout.addStretch(1)
        self.update_controls()

    def update_controls(self):
        busy = self.active_id is not None
        info = get_model(self.chat.model) if self.chat else None
        premium = info is not None and info.access_tier != "free"
        has_input = bool(self.prompt.toPlainText().strip()) or bool(
            self.chat and self.chat.draft_files
        )
        self.send_button.setEnabled(self.worker_ready and (busy or (has_input and not premium)))
        self.send_button.setIcon(icon("stop" if busy else "send", self.colors["background"]))
        self.send_button.setToolTip("Stop response · Esc" if busy else "Send message · Enter")
        if premium and not busy:
            self.send_button.setToolTip(PREMIUM_MESSAGE)
        self.premium_warning.setVisible(premium)
        self.send_button.setAccessibleName("Stop response" if busy else "Send message")
        self.prompt.setReadOnly(busy)
        self.attach_button.setEnabled(not busy and not premium)
        self.attachment_hint.setText(
            "Documents"
            if info and not info.supports_images
            else "Documents & images"
            if info and not info.supports_pdf
            else "Documents, PDFs & images"
        )
        self.attachment_hint.setToolTip(
            "Office and Google exports are read locally and sent as text. "
            "16,000 characters including your prompt; 4,500 when combined with images. "
            "Legacy Word/PowerPoint, Publisher and Visio files need LibreOffice."
        )
        self.model.setEnabled(not busy)
        for card in self.cards:
            card.retry.setEnabled(not busy and self.worker_ready and not premium)
            card.edit.setEnabled(not busy)
        count = len(self.prompt.toPlainText())
        self.character_count.setText(f"{count:,} characters" if count else "")

    def send_or_stop(self):
        if self.active_id:
            self.stop()
        else:
            self.send()

    def send(self):
        if self.active_id or not self.worker_ready:
            return
        info = get_model(self.model.currentText())
        if info and info.access_tier != "free":
            self.notify(PREMIUM_MESSAGE)
            return
        prompt = self.prompt.toPlainText().strip()
        files = list(self.chat.draft_files)
        if not prompt and not files:
            return
        prompt = prompt or "Please describe and analyze the attached files."
        self.model_changed()
        replay = None
        if self.editing_id == self.chat.id and self.chat.turns:
            previous = self.chat.turns[-1]
            if previous.request_message and previous.files == files:
                replay = deepcopy(previous.request_message)
                if isinstance(replay.get("content"), list):
                    # The first part is the prompt; subsequent text parts are documents.
                    parts = replay["content"][1:]
                    replay["content"] = [{"type": "text", "text": prompt}] + parts
                else:
                    replay["content"] = prompt
            self.chat.history = self.chat.history[: previous.history_start]
            self.chat.turns.pop()
        self.editing_id = None
        turn = Turn(
            prompt=prompt,
            files=files,
            model=self.chat.model,
            history_start=len(self.chat.history),
            request_message=replay,
        )
        self.chat.turns.append(turn)
        if len(self.chat.turns) == 1 and self.chat.title == "New chat":
            title = " ".join(prompt.split())
            self.chat.title = title[:54] + ("…" if len(title) > 54 else "")
        self.chat.draft, self.chat.draft_files = "", []
        with QSignalBlocker(self.prompt):
            self.prompt.clear()
        self.prompt.fit_height()
        self.start_turn(turn)

    def start_turn(self, turn):
        self.cancel_edit_button.hide()
        self.active_id = self.chat.id
        self.chat.touch()
        self.dirty_response = False
        self.refresh_files()
        self.update_header()
        self.render_chat()
        self.refresh_sidebar()
        self.persist()
        self.scroll_bottom()
        history = deepcopy(self.chat.history)
        prompt, files = turn.prompt, turn.files
        if turn.request_message:
            history.append(deepcopy(turn.request_message))
            prompt, files = None, []
        self.worker.submit(self.chat.id, prompt, history, files, turn.model, self.store.preferences)
        self.update_controls()

    def retry(self):
        if self.active_id or not self.worker_ready or not self.chat.turns:
            return
        info = get_model(self.chat.model)
        if info and info.access_tier != "free":
            self.notify(PREMIUM_MESSAGE)
            return
        turn = self.chat.turns[-1]
        # Canonical user content contains encoded attachments and opaque fields.
        if turn.status == "complete":
            turn.request_message = deepcopy(self.chat.history[turn.history_start])
        self.chat.history = self.chat.history[: turn.history_start]
        turn.answer, turn.error, turn.status, turn.model = "", "", "pending", self.chat.model
        self.start_turn(turn)

    def edit_last(self):
        if self.active_id or not self.chat.turns:
            return
        turn = self.chat.turns[-1]
        if turn.status == "complete":
            turn.request_message = deepcopy(self.chat.history[turn.history_start])
        self.editing_id = self.chat.id
        self.cancel_edit_button.show()
        self.prompt.setPlainText(turn.prompt)
        self.chat.draft_files = list(turn.files)
        self.refresh_files()
        self.notify("Editing the last message · Send to replace its response.")
        self.prompt.setFocus()

    def cancel_edit(self):
        if self.editing_id and not self.active_id:
            self.editing_id = None
            self.prompt.clear()
            self.chat.draft_files = []
            self.cancel_edit_button.hide()
            self.refresh_files()
            self.update_controls()
            self.schedule_save()

    def stop(self):
        if self.active_id:
            self.worker.cancel()
            self.send_button.setEnabled(False)
            self.notify("Stopping response…")
        elif self.editing_id:
            self.cancel_edit()

    def on_ready(self):
        self.worker_ready = True
        self.update_controls()
        if self.closing:
            self.worker.shutdown()

    def on_delta(self, chat_id, chunk):
        chat = self.find_chat(chat_id)
        if chat and chat.turns:
            chat.turns[-1].answer += chunk
            if self.chat.id == chat_id:
                self.dirty_response = True

    def render_delta(self):
        if not self.dirty_response or not self.cards:
            return
        at_bottom = self.is_at_bottom()
        self.dirty_response = False
        card = self.cards[-1]
        card.body.set_content(card.turn.answer, self.colors)
        card.update_state()
        if at_bottom:
            QTimer.singleShot(0, self.scroll_bottom)

    def on_complete(self, chat_id, text, history):
        chat = self.find_chat(chat_id)
        if chat:
            turn = chat.turns[-1]
            turn.answer, turn.status, turn.error = text, "complete", ""
            chat.history = history
            # Don't duplicate large attachment content in successful turns.
            turn.request_message = None
        self.finish_turn(chat_id)

    def on_error(self, chat_id, error):
        chat = self.find_chat(chat_id)
        if chat:
            chat.turns[-1].status, chat.turns[-1].error = "error", error
        self.finish_turn(chat_id)
        self.notify(error, persistent=True)

    def on_stopped(self, chat_id):
        chat = self.find_chat(chat_id)
        if chat:
            chat.turns[-1].status = "stopped"
            chat.turns[
                -1
            ].error = "Response stopped. Partial output is excluded from model history."
        self.finish_turn(chat_id)

    def finish_turn(self, chat_id):
        self.active_id = None
        chat = self.find_chat(chat_id)
        if chat:
            chat.touch()
        if self.chat.id == chat_id and self.cards:
            self.dirty_response = True
            self.render_delta()
            self.cards[-1].retry.setToolTip(
                "Regenerate response" if chat.turns[-1].status == "complete" else "Retry message"
            )
        self.update_controls()
        self.refresh_files()
        self.refresh_sidebar()
        self.persist()
        if not self.closing:
            self.prompt.setFocus()

    def is_at_bottom(self):
        bar = self.scroll.verticalScrollBar()
        return bar.maximum() - bar.value() < 70

    def scroll_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.jump.hide()

    def update_jump(self, *_):
        self.jump.setVisible(self.stack.currentIndex() == 1 and not self.is_at_bottom())

    def schedule_save(self):
        self.save_timer.start()

    def persist(self):
        self.save_timer.stop()
        try:
            self.store.save()
        except OSError as error:
            self.notify(f"Could not save local history: {error}", persistent=True)

    def save_streaming(self):
        if self.active_id:
            self.persist()

    def notify(self, message, *, persistent=False):
        self.notice.setText(message)
        self.notice.show()
        if not hasattr(self, "notice_timer"):
            self.notice_timer = QTimer(self)
            self.notice_timer.setSingleShot(True)
            self.notice_timer.timeout.connect(self.notice.hide)
        self.notice_timer.stop()
        if not persistent:
            self.notice_timer.start(6500)

    def header_menu(self):
        menu = self.conversation_menu(self.chat)
        menu.exec(self.sender().mapToGlobal(self.sender().rect().bottomLeft()))

    def sidebar_menu(self, position):
        item = self.chat_list.itemAt(position)
        if item:
            chat = self.find_chat(item.data(Qt.ItemDataRole.UserRole))
            menu = self.conversation_menu(chat)
            menu.exec(self.chat_list.mapToGlobal(position))

    def conversation_menu(self, chat):
        menu = QMenu(self)
        menu.addAction("Rename conversation…", lambda: self.rename_chat(chat))
        menu.addAction(
            "Unpin conversation" if chat.pinned else "Pin conversation", lambda: self.pin_chat(chat)
        )
        menu.addAction("Export conversation…", lambda: self.export_chat(chat))
        menu.addSeparator()
        delete = menu.addAction("Delete conversation…", lambda: self.delete_chat(chat))
        delete.setEnabled(chat.id != self.active_id)
        return menu

    def rename_chat(self, chat=None):
        chat = chat if isinstance(chat, Chat) else self.chat
        name, accepted = QInputDialog.getText(self, "Rename conversation", "Name:", text=chat.title)
        if accepted and name.strip():
            chat.title = name.strip()[:200]
            self.update_header()
            self.refresh_sidebar()
            self.schedule_save()

    def pin_chat(self, chat):
        chat.pinned = not chat.pinned
        self.refresh_sidebar()
        self.schedule_save()

    def delete_chat(self, chat):
        if chat.id == self.active_id:
            return
        answer = QMessageBox.question(
            self,
            "Delete conversation?",
            f"Permanently delete “{chat.title}” from this device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.chats.remove(chat)
        if chat is self.chat:
            if not self.store.chats:
                self.store.chats.append(Chat(model=self.store.preferences["model"]))
            self.show_chat(self.store.chats[-1])
        self.refresh_sidebar()
        self.persist()

    def export_chat(self, chat=None):
        chat = chat if isinstance(chat, Chat) else self.chat
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in chat.title)[:70]
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export conversation",
            safe_name + ".md",
            "Markdown (*.md);;Duck.ai conversation (*.json)",
        )
        if not path:
            return
        target = Path(path)
        as_json = target.suffix.lower() == ".json" or "*.json" in selected
        if not target.suffix or (as_json and target.suffix.lower() == ".md"):
            target = target.with_suffix(".json" if as_json else ".md")
        try:
            target.write_text(
                Store.export_json(chat) if as_json else chat.markdown(), encoding="utf-8"
            )
            self.notify(f"Exported to {target}")
        except OSError as error:
            self.notify(f"Could not export: {error}", persistent=True)

    def import_chat(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import conversation", "", "JSON (*.json)")
        if not path:
            return
        try:
            chat = self.store.import_chat(Path(path))
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.notify(f"Could not import conversation: {error}", persistent=True)
            return
        self.search.clear()
        self.show_chat(chat)
        self.persist()
        self.notify("Conversation imported. You can continue chatting.")

    def settings(self):
        if self.active_id:
            self.notify("Stop the current response before changing settings.")
            return
        dialog = SettingsDialog(self.store.preferences, self.store.directory, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.preferences = dialog.preferences()
            if self.chat and not self.chat.turns:
                self.chat.model = self.store.preferences["model"]
                self.model.setCurrentText(self.chat.model)
            self.apply_theme()
            self.persist()

    def about(self):
        QMessageBox.about(
            self,
            "Duck.ai Desktop",
            "A native Python / PySide6 example built with the Duck.ai SDK.\n\n"
            "Streaming replies, local conversations, attachments, and a little room "
            "for curiosity. Gemini-inspired; not affiliated with Google or DuckDuckGo.\n\n"
            "Browser challenges run silently in the background. "
            "No API key or local server required.",
        )

    def closeEvent(self, event):
        if self.worker.isRunning():
            event.ignore()
            if not self.closing:
                self.closing = True
                self.persist()
                self.setEnabled(False)
                self.notify("Closing the SDK and browser session…", persistent=True)
                if self.worker_ready:
                    self.worker.shutdown()
        else:
            self.persist()
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "header_title"):
            QTimer.singleShot(0, self.update_header)

    def on_worker_finished(self):
        if self.closing:
            self.close()
