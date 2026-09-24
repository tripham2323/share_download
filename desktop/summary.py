"""Final transfer report and throughput samples collected from real TCP traffic."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from client import DownloadResult


class RateChart(QWidget):
    """Draw one point per observed rate sample; never synthesize a curve."""

    def __init__(self):
        super().__init__()
        self.samples: list[tuple[float, float]] = []
        self.setMinimumHeight(105)
        self.setAccessibleName("Biểu đồ tốc độ mạng theo thời gian")

    def set_samples(self, samples: list[tuple[float, float]]) -> None:
        self.samples = samples
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#566980"))
        if len(self.samples) < 2:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Không đủ mẫu để vẽ")
            return
        left, top, right, bottom = 38, 10, self.width() - 12, self.height() - 24
        if right <= left or bottom <= top:
            return
        peak = max(rate for _, rate in self.samples)
        span = self.samples[-1][0] - self.samples[0][0]
        painter.drawText(0, 0, self.width(), 20, Qt.AlignmentFlag.AlignLeft,
                         f"{peak / 1_048_576:.1f} MB/s")
        painter.drawText(0, bottom + 2, self.width(), 20, Qt.AlignmentFlag.AlignRight,
                         f"{span:.0f} giây")
        painter.setPen(QPen(QColor("#e3e9f0"), 1))
        painter.drawLine(left, bottom, right, bottom)
        painter.setPen(QPen(QColor("#2563d9"), 2))
        previous = None
        for timestamp, rate in self.samples:
            x = left + (timestamp - self.samples[0][0]) / span * (right - left) if span else left
            y = bottom - rate / peak * (bottom - top) if peak else bottom
            point = (int(x), int(y))
            if previous is not None:
                painter.drawLine(*previous, *point)
            previous = point


class SessionSummary(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(19, 17, 19, 17)
        layout.setSpacing(8)
        title = QLabel("Báo cáo phiên tải")
        title.setObjectName("fileTitle")
        layout.addWidget(title)
        self.details = QLabel()
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self.sha = QLabel()
        self.sha.setObjectName("muted")
        self.sha.setWordWrap(True)
        self.sha.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.sha)
        self.path = QLabel()
        self.path.setObjectName("muted")
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path)
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Sao chép SHA-256")
        self.copy_button.clicked.connect(self._copy_sha)
        actions.addWidget(self.copy_button)
        self.folder_button = QPushButton("Mở thư mục kết quả")
        self.folder_button.clicked.connect(self._open_folder)
        actions.addWidget(self.folder_button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(QLabel("Tốc độ tổng theo thời gian (mẫu đo thực)", objectName="muted"))
        self.chart = RateChart()
        layout.addWidget(self.chart)
        self._hash = ""
        self._output: Path | None = None
        self.hide()

    def set_result(self, result: DownloadResult, elapsed: float,
                   samples: list[tuple[float, float]], failed_servers: set[str]) -> None:
        self._hash = result.file_sha256
        self._output = result.output
        used = sorted(name for name, count in result.per_server_chunks.items() if count)
        excluded = ", ".join(sorted(result.unused_servers)) or "không"
        self.details.setText(
            f"{result.bytes_written:,} byte · {sum(result.per_server_chunks.values()):,} chunk "
            f"· {elapsed:.1f} giây · Server đã truyền: {', '.join(used) or 'không'} "
            f"· Loại: {excluded}"
            + (f" · Mất kết nối: {', '.join(sorted(failed_servers))}" if failed_servers else "")
        )
        self.sha.setText(f"SHA-256 đã xác minh: {self._hash}")
        self.sha.setToolTip(self._hash)
        self.path.setText(f"Đã lưu: {result.output}")
        self.path.setToolTip(str(result.output))
        self.chart.set_samples(samples)
        self.show()

    def _copy_sha(self) -> None:
        QGuiApplication.clipboard().setText(self._hash)

    def _open_folder(self) -> None:
        if self._output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output.parent)))
