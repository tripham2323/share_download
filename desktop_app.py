"""Launch ShareDownload as a PySide6 desktop application."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError as exc:
    raise SystemExit("Install desktop dependencies: python -m pip install -r requirements-desktop.txt") from exc

from desktop.window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    for path in (Path("client_config.json"), Path("client_config.example.json")):
        if path.is_file():
            try:
                window.config_form.load_file(path)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(window, "Cấu hình không hợp lệ", str(exc))
            break
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
