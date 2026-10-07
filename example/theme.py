"""Small shared design system, drawn with native Qt widgets."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

LIGHT = {
    "background": "#ffffff",
    "sidebar": "#f0f4f9",
    "surface": "#f6f8fc",
    "composer": "#f0f4f9",
    "text": "#202633",
    "muted": "#727d90",
    "border": "#e3e8f0",
    "hover": "#e7edf6",
    "selected": "#dce7fa",
    "accent": "#3569c8",
    "accent_soft": "#e8efff",
    "danger": "#b74347",
    "code": "#edf1f7",
    "user": "#eff3fa",
}
DARK = {
    "background": "#15181f",
    "sidebar": "#1c2029",
    "surface": "#222732",
    "composer": "#252b36",
    "text": "#e7ecf6",
    "muted": "#9da9bc",
    "border": "#343b49",
    "hover": "#2c3442",
    "selected": "#303f5c",
    "accent": "#a8c7fa",
    "accent_soft": "#293953",
    "danger": "#f49a9d",
    "code": "#242b38",
    "user": "#2a3447",
}


def icon(name: str, color: str = "#727d90", size: int = 20) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    painter.setPen(
        QPen(
            QColor(color),
            1.7,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    paths = {
        "plus": [(5, 12, 19, 12), (12, 5, 12, 19)],
        "send": [(12, 19, 12, 5), (5, 12, 12, 5), (12, 5, 19, 12)],
        "menu": [(4, 7, 20, 7), (4, 12, 16, 12), (4, 17, 20, 17)],
        "close": [(6, 6, 18, 18), (18, 6, 6, 18)],
        "download": [
            (12, 4, 12, 16),
            (7, 11, 12, 16),
            (17, 11, 12, 16),
            (5, 18, 5, 21),
            (5, 21, 19, 21),
            (19, 21, 19, 18),
        ],
        "edit": [
            (5, 19, 9, 18),
            (9, 18, 20, 7),
            (20, 7, 17, 4),
            (17, 4, 6, 15),
            (6, 15, 5, 19),
            (14, 7, 17, 10),
        ],
        "code": [(8, 6, 3, 12), (3, 12, 8, 18), (16, 6, 21, 12), (21, 12, 16, 18), (14, 4, 10, 20)],
        "arrow": [(5, 19, 19, 5), (9, 5, 19, 5), (19, 5, 19, 15)],
        "chevron": [(7, 10, 12, 15), (12, 15, 17, 10)],
    }
    for line in paths.get(name, []):
        painter.drawLine(*line)
    if name == "spark":
        path = QPainterPath(QPointF(12, 1))
        for x, y in [(15, 9), (23, 12), (15, 15), (12, 23), (9, 15), (1, 12), (9, 9)]:
            path.lineTo(x, y)
        path.closeSubpath()
        painter.fillPath(path, QColor(color))
    elif name == "search":
        painter.drawEllipse(4, 4, 12, 12)
        painter.drawLine(14, 14, 20, 20)
    elif name == "chat":
        painter.drawRoundedRect(3, 4, 18, 14, 4, 4)
        painter.drawLine(7, 18, 7, 21)
        painter.drawLine(7, 21, 11, 18)
    elif name == "copy":
        painter.drawRoundedRect(8, 8, 12, 13, 2, 2)
        painter.drawLine(4, 16, 4, 4)
        painter.drawLine(4, 4, 16, 4)
    elif name == "stop":
        painter.fillRect(6, 6, 12, 12, QColor(color))
    elif name == "more":
        painter.setBrush(QColor(color))
        for x in (5, 12, 19):
            painter.drawEllipse(QPointF(x, 12), 1.1, 1.1)
    elif name == "attach":
        path = QPainterPath(QPointF(7, 13))
        path.lineTo(14, 6)
        path.cubicTo(19, 1, 25, 7, 20, 12)
        path.lineTo(10, 22)
        path.cubicTo(5, 27, -1, 21, 4, 16)
        path.lineTo(14, 6)
        painter.translate(1, -2)
        painter.scale(0.9, 0.9)
        painter.drawPath(path)
    elif name == "settings":
        painter.drawEllipse(8, 8, 8, 8)
        for x, y, xx, yy in [
            (12, 2, 12, 5),
            (12, 19, 12, 22),
            (2, 12, 5, 12),
            (19, 12, 22, 12),
            (5, 5, 7, 7),
            (17, 17, 19, 19),
            (5, 19, 7, 17),
            (17, 7, 19, 5),
        ]:
            painter.drawLine(x, y, xx, yy)
    elif name == "refresh":
        painter.drawArc(4, 4, 16, 16, 40 * 16, 290 * 16)
        painter.drawLine(20, 4, 20, 10)
        painter.drawLine(20, 10, 14, 10)
    elif name == "file":
        painter.drawRoundedRect(5, 3, 14, 18, 2, 2)
        painter.drawLine(9, 9, 15, 9)
        painter.drawLine(9, 13, 15, 13)
    painter.end()
    return QIcon(pixmap)


def stylesheet(c: dict) -> str:
    return f"""
    QWidget {{ color: {c["text"]}; font-size: 14px; }}
    QMainWindow, QDialog, #canvas {{ background: {c["background"]}; }}
    #sidebar {{ background: {c["sidebar"]}; border-right: 1px solid {c["border"]}; }}
    QLabel {{ background: transparent; }}
    QLabel[muted="true"] {{ color: {c["muted"]}; }}
    #brand {{ font-size: 22px; font-weight: 600; }}
    #eyebrow {{ color: {c["muted"]}; font-size: 10px; font-weight: 600; letter-spacing: 2px; }}
    #headerTitle {{ font-size: 20px; font-weight: 500; }}
    #subtitle {{ font-size: 17px; color: {c["muted"]}; }}
    #section {{ font-size: 11px; color: {c["muted"]}; font-weight: 600; }}
    QPushButton, QToolButton {{ border: none; border-radius: 10px;
        padding: 9px 12px; background: transparent; text-align: left; }}
    QPushButton:hover, QToolButton:hover {{ background: {c["hover"]}; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {c["muted"]}; }}
    #iconButton {{ padding: 8px; border-radius: 18px; }}
    #newChat {{ background: {c["accent_soft"]}; color: {c["accent"]};
        border-radius: 22px; padding: 13px 18px; font-weight: 600; }}
    #newChat:hover {{ background: {c["selected"]}; }}
    #primary {{ background: {c["accent"]}; color: {c["background"]}; font-weight: 600; }}
    #send {{ background: {c["accent"]}; border-radius: 20px; padding: 8px; }}
    #send:disabled {{ background: {c["hover"]}; }}
    #suggestion {{ background: {c["surface"]}; border: 1px solid {c["border"]};
        border-radius: 16px; text-align: left; padding: 0; }}
    #suggestion:hover {{ background: {c["hover"]}; border-color: {c["accent"]}; }}
    QLineEdit, QSpinBox, QComboBox {{ background: {c["surface"]};
        border: 1px solid {c["border"]}; border-radius: 9px; padding: 9px; }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {c["accent"]}; }}
    #search {{ background: {c["sidebar"]}; border: 1px solid {c["border"]};
        padding: 10px 12px; border-radius: 11px; font-size: 12px; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{ background: {c["surface"]}; color: {c["text"]};
        selection-background-color: {c["selected"]}; border: 1px solid {c["border"]}; }}
    #model {{ background: transparent; border: none; padding: 5px 8px; font-size: 12px; }}
    QListWidget {{ background: transparent; border: none; outline: none; }}
    QListWidget::item {{ border-radius: 10px; }}
    QScrollArea, #transcript, QTextBrowser {{ background: transparent; border: none; }}
    #composer {{ background: {c["composer"]}; border: 1px solid {c["border"]};
        border-radius: 23px; }}
    #prompt {{ background: transparent; border: none; padding: 2px;
        selection-background-color: {c["selected"]}; font-size: 15px; }}
    #fileChip {{ background: {c["background"]}; border: 1px solid {c["border"]};
        border-radius: 9px; padding: 5px 10px; font-size: 12px; }}
    #userBubble {{ background: {c["user"]}; border-radius: 18px;
        border-top-right-radius: 5px; }}
    #notice {{ background: {c["accent_soft"]}; color: {c["accent"]};
        border-radius: 9px; padding: 10px 14px; font-size: 12px; }}
    #messageStatus {{ color: {c["muted"]}; font-size: 12px; }}
    QMenu {{ background: {c["background"]}; border: 1px solid {c["border"]};
        border-radius: 9px; padding: 6px; }}
    QMenu::item {{ padding: 8px 24px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {c["hover"]}; }}
    QMenu::separator {{ height: 1px; background: {c["border"]}; margin: 4px 8px; }}
    QScrollBar:vertical {{ background: transparent; width: 7px; margin: 3px; }}
    QScrollBar::handle:vertical {{ background: {c["border"]}; border-radius: 3px;
        min-height: 30px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QToolTip {{ background: {c["surface"]}; color: {c["text"]};
        border: 1px solid {c["border"]}; padding: 6px; }}
    QCheckBox {{ spacing: 10px; }}
    """
