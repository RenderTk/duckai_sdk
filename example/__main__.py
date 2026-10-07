"""Run from the repository root: python -m example."""

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from example.app import MainWindow
from example.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Override the local workspace directory")
    parser.add_argument("--screenshot", type=Path, help="Save a window PNG and exit")
    parser.add_argument("--dark", action="store_true", help="Start with the dark theme")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Duck.ai Desktop")
    app.setOrganizationName("DuckAI SDK Examples")
    app.setStyle("Fusion")
    app.setFont(QFont("SF Pro Text" if sys.platform == "darwin" else "Segoe UI", 11))
    directory = args.data_dir or Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )
    directory = directory.expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        QMessageBox.critical(None, "Cannot open local workspace", str(error))
        return 1
    # Keep the lock alive throughout app.exec(). Two windows writing the same
    # JSON workspace would otherwise silently replace each other's conversations.
    workspace_lock = QLockFile(str(directory / "workspace.lock"))
    workspace_lock.setStaleLockTime(0)
    if not workspace_lock.tryLock(100):
        QMessageBox.information(
            None,
            "Workspace already open",
            "This workspace is already open or cannot be locked. "
            "Use --data-dir to open a separate workspace.",
        )
        return 1
    store = Store(directory)
    if args.dark:
        store.preferences["theme"] = "dark"
    window = MainWindow(store)
    app.styleHints().colorSchemeChanged.connect(
        lambda _: window.apply_theme() if store.preferences["theme"] == "system" else None
    )
    window.show()
    if args.screenshot:

        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot)):
                print("Could not write screenshot", file=sys.stderr)
            window.close()

        QTimer.singleShot(1000, capture)
    result = app.exec()
    workspace_lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
