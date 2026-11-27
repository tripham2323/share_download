"""Final transfer report and throughput samples collected from real TCP traffic."""

from __future__ import annotations

from math import floor, log10
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from client import DownloadResult
from desktop.theme import BORDER, INK, MUTED


class RateChart(QWidget):
    """Draw one point per observed rate sample; never synthesize a curve."""

    def __init__(self):
        super().__init__()
        self.samples: list[tuple[float, float]] = []
        self.setMinimumHeight(120)
        self.setAccessibleName("Biểu đồ tốc độ mạng theo thời gian")

    def set_samples(self, samples: list[tuple[float, float]]) -> None:
        self.samples = samples
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(MUTED))
        if not self.samples:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Chưa có mẫu tốc độ.")
            return
        if len(self.samples) == 1:
            elapsed, rate = self.samples[0]
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             f"Một mẫu: {rate / 1_048_576:.2f} MiB/s · {elapsed:.1f} giây")
            return
        top, right, bottom = 12, self.width() - 20, self.height() - 25
        peak = max(rate for _, rate in self.samples) / 1_048_576
        raw_step = peak / 3 or 1
        magnitude = 10 ** floor(log10(raw_step))
        step = next(unit * magnitude for unit in (1, 2, 5, 10) if unit * magnitude > raw_step)
        maximum = step * 3
        top_label = f"{maximum:.3g} MiB/s"
        left = painter.fontMetrics().horizontalAdvance(top_label) + 12
        if right <= left or bottom <= top:
            return
        first = self.samples[0][0]
        span = self.samples[-1][0] - first
        for tick in range(4):
            y = round(bottom - tick / 3 * (bottom - top))
            painter.setPen(QPen(QColor(BORDER), 1))
            painter.drawLine(left, y, right, y)
            painter.setPen(QColor(MUTED))
            painter.drawText(0, y - 9, left - 8, 18, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             top_label if tick == 3 else f"{step * tick:.3g}")
        painter.drawText(left, bottom + 4, right - left, 18, Qt.AlignmentFlag.AlignLeft,
                         f"{first:.1f} s")
        painter.drawText(left, bottom + 4, right - left, 18, Qt.AlignmentFlag.AlignRight,
                         f"{self.samples[-1][0]:.1f} s")
        painter.setPen(QPen(QColor(INK), 2))
        previous = None
        for timestamp, rate in self.samples:
            x = left + (timestamp - first) / span * (right - left) if span else left
            y = bottom - rate / (maximum * 1_048_576) * (bottom - top)
            point = (int(x), int(y))
            if previous is not None:
                painter.drawLine(*previous, *point)
            previous = point


class SessionSummary(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("report")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        title = QLabel("Phiên hoàn tất gần nhất", objectName="sectionTitle")
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        self.copy_button = QPushButton("Sao chép SHA-256")
        self.copy_button.clicked.connect(self._copy_sha)
        header.addWidget(self.copy_button)
        self.folder_button = QPushButton("Mở thư mục kết quả")
        self.folder_button.clicked.connect(self._open_folder)
        header.addWidget(self.folder_button)
        layout.addLayout(header)
        self.copy_button.hide()
        self.folder_button.hide()
        self.empty = QLabel("Chưa có phiên tải hoàn tất.", objectName="caption")
        layout.addWidget(self.empty)
        self.result_panel = QWidget()
        report = QVBoxLayout(self.result_panel)
        report.setContentsMargins(0, 0, 0, 0)
        report.setSpacing(8)
        layout.addWidget(self.result_panel)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        report.addWidget(self.details)
        self.sha = QLabel()
        self.sha.setObjectName("muted")
        self.sha.setTextFormat(Qt.TextFormat.PlainText)
        self.sha.setWordWrap(True)
        self.sha.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        report.addWidget(self.sha)
        self.path = QLabel()
        self.path.setObjectName("muted")
        self.path.setTextFormat(Qt.TextFormat.PlainText)
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        report.addWidget(self.path)
        report.addWidget(QLabel("Tốc độ đã ghi nhận", objectName="muted"))
        self.chart = RateChart()
        report.addWidget(self.chart)
        self._hash = ""
        self._output: Path | None = None
        self.result_panel.hide()

    def set_result(self, result: DownloadResult, elapsed: float,
                   samples: list[tuple[float, float]], failed_servers: set[str]) -> None:
        self._hash = result.file_sha256
        self._output = result.output
        session_bytes = sum(result.per_server_bytes.values())
        self.details.setText(
            f"{result.bytes_written:,} B đã lưu · {elapsed:.1f} giây\n"
            f"{session_bytes:,} B nhận trong phiên · {sum(result.per_server_chunks.values()):,} chunk mới"
            f" · {result.bytes_written - session_bytes:,} B từ checkpoint"
            + (f"\nNguồn không dùng: {', '.join(sorted(result.unused_servers))}" if result.unused_servers else "")
            + (f"\nNguồn mất kết nối: {', '.join(sorted(failed_servers))}" if failed_servers else "")
        )
        self.sha.setText(f"SHA-256 đã xác minh: {self._hash}")
        self.sha.setToolTip(self._hash)
        self.path.setText(f"Đã lưu: {result.output}")
        self.path.setToolTip(str(result.output))
        self.chart.set_samples(samples)
        self.empty.hide()
        self.result_panel.show()
        self.copy_button.show()
        self.folder_button.show()

    def _copy_sha(self) -> None:
        QGuiApplication.clipboard().setText(self._hash)

    def _open_folder(self) -> None:
        if self._output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output.parent)))
