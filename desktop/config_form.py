"""Editable server configuration shared with the CLI schema."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QDoubleSpinBox, QFileDialog, QVBoxLayout, QWidget,
)

from client import ClientConfig, parse_config


class ServerRow(QWidget):
    changed = Signal()

    def __init__(self, name: str, host: str, port: int, remove):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("Tên, ví dụ S1")
        self.host = QLineEdit(host)
        self.host.setPlaceholderText("IP hoặc hostname")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(port)
        button = QPushButton("Xóa")
        button.setObjectName("removeServer")
        button.clicked.connect(lambda: remove(self))
        layout.addWidget(self.name, 1)
        layout.addWidget(self.host, 2)
        layout.addWidget(self.port, 1)
        layout.addWidget(button)
        self.name.textChanged.connect(self.changed)
        self.host.textChanged.connect(self.changed)
        self.port.valueChanged.connect(self.changed)

    def as_dict(self) -> dict:
        return {"name": self.name.text(), "host": self.host.text(), "port": self.port.value()}


class ConfigForm(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_path = Path.cwd() / "client_config.json"
        self.rows: list[ServerRow] = []
        root = QVBoxLayout(self)
        root.setSpacing(17)
        title = QLabel("Cấu hình máy chủ")
        title.setObjectName("formTitle")
        root.addWidget(title)
        subtitle = QLabel("Kết nối nhiều nguồn TCP chứa cùng một phiên bản tệp.")
        subtitle.setObjectName("muted")
        root.addWidget(subtitle)
        server_group = QGroupBox("Máy chủ nguồn")
        server_layout = QVBoxLayout(server_group)
        self.server_rows = QVBoxLayout()
        server_layout.addLayout(self.server_rows)
        add_button = QPushButton("+ Thêm máy chủ")
        add_button.clicked.connect(lambda: self.add_server())
        server_layout.addWidget(add_button)
        root.addWidget(server_group)

        file_group = QGroupBox("Tệp cần tải")
        form = QFormLayout(file_group)
        self.filename_input = QLineEdit("config.dat")
        self.filename_input.setPlaceholderText("config.dat")
        self.output_input = QLineEdit("downloads/config.dat")
        self.output_input.setPlaceholderText("Chọn đường dẫn lưu")
        file_picker = QPushButton("Chọn…")
        file_picker.clicked.connect(self._choose_output)
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_input, 1)
        output_layout.addWidget(file_picker)
        form.addRow("Tên tệp trên server", self.filename_input)
        form.addRow("Lưu vào", output_row)
        root.addWidget(file_group)

        self.advanced_toggle = QPushButton("Tùy chọn nâng cao")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAccessibleName("Mở hoặc thu gọn tùy chọn nâng cao")
        root.addWidget(self.advanced_toggle)
        self.advanced_content = QGroupBox()
        advanced_form = QFormLayout(self.advanced_content)
        self.chunk_size = QSpinBox()
        self.chunk_size.setRange(1, 1_048_576)
        self.chunk_size.setValue(262_144)
        self.chunk_size.setSuffix(" byte")
        self.connect_timeout = self._seconds(5)
        self.read_timeout = self._seconds(60)
        self.reconnect_attempts = QSpinBox()
        self.reconnect_attempts.setRange(0, 100)
        self.reconnect_attempts.setValue(3)
        advanced_form.addRow("Kích thước chunk", self.chunk_size)
        advanced_form.addRow("Timeout kết nối", self.connect_timeout)
        advanced_form.addRow("Timeout đọc", self.read_timeout)
        advanced_form.addRow("Kết nối lại", self.reconnect_attempts)
        self.advanced_content.hide()
        self.advanced_toggle.toggled.connect(self.advanced_content.setVisible)
        root.addWidget(self.advanced_content)
        self.error_label = QLabel("")
        self.error_label.setObjectName("formError")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)
        root.addStretch()
        for widget in (self.filename_input, self.output_input):
            widget.textChanged.connect(self.changed)
        for widget in (self.chunk_size, self.connect_timeout, self.read_timeout,
                       self.reconnect_attempts):
            widget.valueChanged.connect(self.changed)

    @staticmethod
    def _seconds(value: float) -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setRange(0.1, 3600.0)
        control.setDecimals(1)
        control.setSuffix(" giây")
        control.setValue(value)
        return control

    def _choose_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Chọn nơi lưu", self.output_input.text())
        if path:
            self.output_input.setText(path)

    def add_server(self, name: str = "", host: str = "", port: int = 5000) -> None:
        row = ServerRow(name, host, port, self.remove_server)
        row.changed.connect(self.changed)
        self.rows.append(row)
        self.server_rows.addWidget(row)
        self.changed.emit()

    def remove_server(self, row: ServerRow) -> None:
        self.rows.remove(row)
        self.server_rows.removeWidget(row)
        row.deleteLater()
        self.changed.emit()

    def as_json_dict(self) -> dict:
        return {
            "servers": [row.as_dict() for row in self.rows],
            "filename": self.filename_input.text().strip(),
            "output": self.output_input.text().strip(),
            "chunk_size": self.chunk_size.value(),
            "connect_timeout": self.connect_timeout.value(),
            "read_timeout": self.read_timeout.value(),
            "reconnect_attempts": self.reconnect_attempts.value(),
        }

    def to_client_config(self) -> ClientConfig:
        try:
            config = parse_config(self.as_json_dict(), self.config_path.parent)
        except ValueError as exc:
            self.error_label.setText(str(exc))
            raise
        self.error_label.setText("")
        return config

    def load_file(self, path: Path) -> None:
        path = Path(path).resolve()
        raw = json.loads(path.read_text(encoding="utf-8"))
        parse_config(raw, path.parent)
        self.config_path = path
        for row in self.rows[:]:
            self.remove_server(row)
        for item in raw["servers"]:
            self.add_server(item["name"], item["host"], item["port"])
        self.filename_input.setText(raw["filename"])
        self.output_input.setText(raw["output"])
        self.chunk_size.setValue(raw.get("chunk_size", 262_144))
        self.connect_timeout.setValue(raw.get("connect_timeout", 5))
        self.read_timeout.setValue(raw.get("read_timeout", 60))
        self.reconnect_attempts.setValue(raw.get("reconnect_attempts", 3))
        self.changed.emit()

    def save_file(self, path: Path) -> None:
        path = Path(path).resolve()
        raw = self.as_json_dict()
        parse_config(raw, path.parent)
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.config_path = path
        self.error_label.setText("")
