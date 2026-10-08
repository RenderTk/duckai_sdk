"""A native composer model button with a keyboard-accessible popup."""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from duckai import get_model, list_models
from example.widgets import label

PREMIUM_MESSAGE = "Premium models aren't supported yet. Choose a free model to chat."


class ModelPicker(QPushButton):
    changed = Signal()

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.setObjectName("modelButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Choose chat model")
        self.setMinimumHeight(38)
        self.setMaximumWidth(260)
        self.popup = ModelPopup(self)
        self.clicked.connect(self.open_popup)
        self.setCurrentText(model)

    def currentText(self):
        return self.model_id

    def setCurrentText(self, model):
        self.model_id = model
        info = get_model(model)
        name = info.name if info else model
        self.setText(f"{name}  ⌄")
        self.setToolTip(f"Choose model · {name}\n{model}")
        self.popup.update_selection(model)

    def choose(self, model):
        self.setCurrentText(model)
        self.popup.hide()
        self.changed.emit()
        self.setFocus()

    def open_popup(self):
        if not self.isEnabled():
            return
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.popup.adjustSize()
        origin = self.mapToGlobal(self.rect().topRight())
        screen = self.screen().availableGeometry()
        width = min(370, screen.width() - 24)
        height = min(570, screen.height() - 32)
        self.popup.resize(width, height)
        x = max(screen.left() + 12, min(origin.x() - width, screen.right() - width - 12))
        y = origin.y() - height - 8
        if y < screen.top() + 12:
            y = min(
                self.mapToGlobal(self.rect().bottomLeft()).y() + 8, screen.bottom() - height - 12
            )
        self.popup.move(x, max(screen.top() + 12, y))
        self.popup.show()
        self.popup.focus_selected()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.popup.hide()
        super().changeEvent(event)


class ModelPopup(QFrame):
    def __init__(self, picker):
        super().__init__(picker, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.picker = picker
        self.setObjectName("modelPopup")
        self.setAccessibleName("Select model")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("modelSurface")
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.addWidget(label("Select model", name="modelMenuTitle", muted=True))
        self.premium_notice = QFrame()
        self.premium_notice.setObjectName("premiumNotice")
        notice_layout = QVBoxLayout(self.premium_notice)
        notice_layout.setContentsMargins(12, 10, 12, 10)
        notice_layout.setSpacing(4)
        title = label("Premium models aren't supported yet", name="premiumTitle")
        title.setWordWrap(True)
        notice_layout.addWidget(title)
        explanation = label("All six free models are ready to use.", muted=True)
        explanation.setWordWrap(True)
        explanation.setStyleSheet("font-size: 11px;")
        notice_layout.addWidget(explanation)
        layout.addWidget(self.premium_notice)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("modelList")
        scroll.viewport().setObjectName("modelViewport")
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 4, 0, 4)
        rows.setSpacing(3)
        self.rows = {}
        self.checks = {}
        advanced = False
        for info in list_models():
            if info.access_tier != "free" and not advanced:
                advanced = True
                note = label("Premium · Not supported yet", name="modelGroup", muted=True)
                rows.addWidget(note)
                hint = label("Subscription login is not available in this app yet.", muted=True)
                hint.setWordWrap(True)
                hint.setStyleSheet("font-size: 11px; padding: 2px 10px 6px;")
                rows.addWidget(hint)
            button = QPushButton()
            button.setObjectName("modelOption")
            button.setMinimumHeight(68)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(info.access_tier == "free")
            button.setAccessibleName(f"{info.name}, {info.description}, {info.access_tier}")
            button.setToolTip(
                PREMIUM_MESSAGE if info.access_tier != "free" else f"{info.provider} · {info.id}"
            )
            button.installEventFilter(self)
            row = QHBoxLayout(button)
            row.setContentsMargins(12, 9, 12, 9)
            row.setSpacing(12)
            badge = label(
                {"OpenAI": "AI", "Anthropic": "✳", "Mistral AI": "M", "Google": "G"}[info.provider],
                name="modelBadge",
                muted=True,
            )
            badge.setFixedWidth(30)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(badge)
            text = QVBoxLayout()
            text.setSpacing(3)
            name = label(info.name, name="modelName")
            name.setProperty("available", info.access_tier == "free")
            text.addWidget(name)
            subtitle = info.description
            if info.beta:
                subtitle += " · Beta"
            if info.access_tier != "free":
                subtitle = f"{info.access_tier.title()} plan · Not supported yet"
            description = label(subtitle, name="modelDescription", muted=True)
            description.setWordWrap(True)
            text.addWidget(description)
            row.addLayout(text, 1)
            if info.access_tier != "free":
                unavailable = label("Not yet", name="premiumBadge")
                unavailable.setAccessibleName("Not supported yet")
                unavailable.setFixedHeight(24)
                unavailable.setAlignment(Qt.AlignmentFlag.AlignCenter)
                row.addWidget(unavailable, 0, Qt.AlignmentFlag.AlignVCenter)
            check = label("", name="modelCheck")
            check.setFixedWidth(20)
            row.addWidget(check)
            # Labels are decorative; mouse and keyboard activation belong to the row.
            for child in button.findChildren(QLabel):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            button.clicked.connect(lambda checked=False, model=info.id: picker.choose(model))
            self.rows[info.id], self.checks[info.id] = button, check
            rows.addWidget(button)
        self.custom = QPushButton("Custom model ID…")
        self.custom.setObjectName("modelCustom")
        self.custom.clicked.connect(self.custom_model)
        self.custom.installEventFilter(self)
        rows.addWidget(self.custom)
        rows.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.scroll = scroll

    def update_selection(self, model):
        for model_id, row in self.rows.items():
            selected = model_id == model
            row.setProperty("selected", selected)
            self.checks[model_id].setText("✓" if selected else "")
            row.style().unpolish(row)
            row.style().polish(row)

    def focus_selected(self):
        row = self.rows.get(self.picker.currentText())
        if row is None or not row.isEnabled():
            row = next(iter(self.rows.values()))
        row.setFocus()
        self.scroll.ensureWidgetVisible(row)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and watched.isEnabled():
                watched.click()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.hide()
                self.picker.setFocus()
                return True
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Home, Qt.Key.Key_End):
                enabled = [r for r in self.rows.values() if r.isEnabled()] + [self.custom]
                index = enabled.index(watched) if watched in enabled else 0
                if event.key() == Qt.Key.Key_Home:
                    index = 0
                elif event.key() == Qt.Key.Key_End:
                    index = len(enabled) - 1
                else:
                    index = (index + (1 if event.key() == Qt.Key.Key_Down else -1)) % len(enabled)
                enabled[index].setFocus()
                self.scroll.ensureWidgetVisible(enabled[index])
                return True
        return super().eventFilter(watched, event)

    def custom_model(self):
        self.hide()
        model, accepted = QInputDialog.getText(
            self.picker.window(),
            "Custom Duck.ai model",
            "Model ID supported by your session:",
            text=self.picker.currentText(),
        )
        if accepted and model.strip():
            info = get_model(model.strip())
            if info and info.access_tier != "free":
                QMessageBox.information(
                    self.picker.window(),
                    "Premium model unavailable",
                    PREMIUM_MESSAGE,
                )
                return
            self.picker.choose(model.strip())
