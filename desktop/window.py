"""Desktop dashboard, configuration and TCP session lifecycle."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import time

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from client import ClientConfig, DownloadEvent, DownloadResult
from desktop.config_form import ConfigForm
from desktop.theme import STYLESHEET
from desktop.worker import DownloadWorker, MetadataWorker


def label(text: str, object_name: str = "") -> QLabel:
    result = QLabel(text)
    if object_name:
        result.setObjectName(object_name)
    return result


def card(layout_class=QVBoxLayout, name="card") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(name)
    layout = layout_class(frame)
    layout.setContentsMargins(19, 17, 19, 17)
    layout.setSpacing(10)
    return frame, layout


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ShareDownload · Truyền tệp đa nguồn")
        self.resize(1280, 780)
        self.setMinimumSize(870, 610)
        self.setStyleSheet(STYLESHEET)
        self.state = "idle"
        self._thread: QThread | None = None
        self._worker: DownloadWorker | MetadataWorker | None = None
        self._running = False
        self._close_after_work = False
        self._server_rows: dict[str, int] = {}
        self._history: deque[tuple[float, dict[str, int]]] = deque()
        self._last_bytes: dict[str, int] = {}
        self._rate_time = time.monotonic()
        self._build()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(200)
        self.poll_timer.timeout.connect(self._poll_progress)
        self.poll_timer.start()
        self.config_form.changed.connect(self._validate)
        self._validate()

    def _build(self) -> None:
        body = QWidget()
        self.setCentralWidget(body)
        outer = QHBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(207)
        nav_layout = QVBoxLayout(sidebar)
        nav_layout.setContentsMargins(15, 25, 15, 20)
        nav_layout.setSpacing(8)
        nav_layout.addWidget(label("↗  ShareDownload", "brand"))
        nav_layout.addSpacing(32)
        nav_layout.addWidget(label("KHÔNG GIAN LÀM VIỆC", "caption"))
        self.pages = QStackedWidget()
        self.nav_buttons = []
        for index, title in enumerate(("Tổng quan phiên tải", "Cấu hình máy chủ", "Nhật ký phiên")):
            button = QPushButton(title)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, n=index: self._show_page(n))
            nav_layout.addWidget(button)
            self.nav_buttons.append(button)
        nav_layout.addStretch()
        nav_layout.addWidget(label("TRUYỀN TỆP ĐA NGUỒN", "caption"))
        nav_layout.addWidget(label("TCP · SHA-256 · Client C1", "caption"))
        outer.addWidget(sidebar)

        workspace = QWidget()
        space = QVBoxLayout(workspace)
        space.setContentsMargins(0, 0, 0, 0)
        space.setSpacing(0)
        top = QFrame()
        top.setObjectName("topbar")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(30, 15, 30, 15)
        self.breadcrumb = label("Ứng dụng   /   Tổng quan phiên tải", "muted")
        top_layout.addWidget(self.breadcrumb)
        top_layout.addStretch()
        top_layout.addWidget(label("Client C1", "muted"))
        space.addWidget(top)
        space.addWidget(self.pages, 1)
        outer.addWidget(workspace, 1)

        self.pages.addWidget(self._overview())
        self.config_form = ConfigForm()
        config_page = QWidget()
        config_layout = QVBoxLayout(config_page)
        config_layout.setContentsMargins(30, 26, 30, 24)
        config_actions = QHBoxLayout()
        open_button = QPushButton("Mở cấu hình…")
        open_button.clicked.connect(self._open_config)
        save_button = QPushButton("Lưu cấu hình…")
        save_button.clicked.connect(self._save_config)
        config_actions.addWidget(open_button)
        config_actions.addWidget(save_button)
        config_actions.addStretch()
        config_layout.addLayout(config_actions)
        config_layout.addWidget(self.config_form)
        config_scroll = QScrollArea()
        config_scroll.setWidgetResizable(True)
        config_scroll.setFrameShape(QFrame.Shape.NoFrame)
        config_scroll.setWidget(config_page)
        self.pages.addWidget(config_scroll)
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(30, 26, 30, 24)
        log_layout.addWidget(label("Nhật ký phiên", "pageTitle"))
        self.full_log = QListWidget()
        log_layout.addWidget(self.full_log)
        self.pages.addWidget(log_page)
        self._show_page(0)

    def _overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 26, 30, 26)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        heading_text = QVBoxLayout()
        heading_text.addWidget(label("Phiên tải hiện tại", "pageTitle"))
        heading_text.addWidget(label("Theo dõi tiến độ và chất lượng kết nối theo thời gian thực.", "muted"))
        heading.addLayout(heading_text)
        heading.addStretch()
        self.check_button = QPushButton("Kiểm tra máy chủ")
        self.check_button.clicked.connect(self._check_servers)
        heading.addWidget(self.check_button)
        self.start_button = QPushButton("Bắt đầu tải")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start_download)
        heading.addWidget(self.start_button)
        self.cancel_button = QPushButton("Hủy tải")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setVisible(False)
        heading.addWidget(self.cancel_button)
        layout.addLayout(heading)

        hero, hero_layout = card(name="hero")
        self.status_label = label("CHƯA CÓ PHIÊN TẢI", "state")
        hero_layout.addWidget(self.status_label)
        file_line = QHBoxLayout()
        self.file_title = label("Chọn cấu hình để bắt đầu", "fileTitle")
        file_line.addWidget(self.file_title, 1)
        self.percent = label("—", "percent")
        file_line.addWidget(self.percent)
        hero_layout.addLayout(file_line)
        self.path_label = label("Cấu hình máy chủ, tệp nguồn và nơi lưu trong mục bên trái.", "muted")
        self.path_label.setToolTip("")
        hero_layout.addWidget(self.path_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        hero_layout.addWidget(self.progress)
        self.progress_text = label("—", "muted")
        hero_layout.addWidget(self.progress_text)
        layout.addWidget(hero)

        metrics = QHBoxLayout()
        self.speed_value = self._metric(metrics, "Tốc độ tổng", "—", "Theo dữ liệu thực")
        self.eta_value = self._metric(metrics, "Ước tính còn lại", "—", "Theo tốc độ gần đây")
        self.available_value = self._metric(metrics, "Máy chủ khả dụng", "—", "Máy chủ cùng phiên bản")
        self.integrity_value = self._metric(metrics, "Toàn vẹn dữ liệu", "—", "SHA-256 toàn tệp sau tải")
        layout.addLayout(metrics)

        lower = QHBoxLayout()
        servers_card, server_layout = card()
        server_layout.addWidget(label("Máy chủ nguồn", "fileTitle"))
        self.server_table = QTableWidget(0, 4)
        self.server_table.setHorizontalHeaderLabels(("MÁY CHỦ", "TRẠNG THÁI", "ĐÃ TẢI", "TỐC ĐỘ"))
        self.server_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.server_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.server_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.server_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.server_table.verticalHeader().hide()
        self.server_table.setAlternatingRowColors(True)
        server_layout.addWidget(self.server_table)
        lower.addWidget(servers_card, 3)
        activity_card, activity_layout = card()
        activity_layout.addWidget(label("Hoạt động gần đây", "fileTitle"))
        self.activity_list = QListWidget()
        activity_layout.addWidget(self.activity_list)
        lower.addWidget(activity_card, 2)
        layout.addLayout(lower, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _metric(layout: QHBoxLayout, title: str, initial: str, caption: str) -> QLabel:
        panel, stack = card(name="metric")
        stack.addWidget(label(title, "muted"))
        value = label(initial, "metricValue")
        stack.addWidget(value)
        stack.addWidget(label(caption, "caption"))
        layout.addWidget(panel, 1)
        return value

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for n, button in enumerate(self.nav_buttons):
            button.setChecked(n == index)
        self.breadcrumb.setText(
            "Ứng dụng   /   " + ("Tổng quan phiên tải", "Cấu hình máy chủ", "Nhật ký phiên")[index]
        )

    def _validate(self) -> None:
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            valid = False
        else:
            valid = True
            if not self._running:
                self.file_title.setText(config.filename)
                self.path_label.setText(str(config.output))
                self.path_label.setToolTip(str(config.output))
            self._populate_servers(config)
        self.start_button.setEnabled(valid and not self._running)
        self.check_button.setEnabled(valid and not self._running)

    def _populate_servers(self, config: ClientConfig) -> None:
        if self._running:
            return
        self.server_table.setRowCount(len(config.servers))
        self._server_rows = {}
        for index, endpoint in enumerate(config.servers):
            self._server_rows[endpoint.name] = index
            self._set_cell(index, 0, f"{endpoint.name}  ·  {endpoint.host}:{endpoint.port}")
            self._set_cell(index, 1, "Chưa kiểm tra")
            self._set_cell(index, 2, "—")
            self._set_cell(index, 3, "—")

    def _set_cell(self, row: int, column: int, value: str) -> None:
        item = self.server_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.server_table.setItem(row, column, item)
        item.setText(value)
        item.setToolTip(value)

    def _open_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Mở cấu hình", "", "JSON (*.json)")
        if path:
            try:
                self.config_form.load_file(Path(path))
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "Cấu hình không hợp lệ", str(exc))

    def _save_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "Lưu cấu hình", str(self.config_form.config_path), "JSON (*.json)")
        if path:
            try:
                self.config_form.save_file(Path(path))
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "Không thể lưu", str(exc))

    def _launch(self, worker, done):
        self._running = True
        self._validate()
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker
        thread.started.connect(worker.run)
        done(worker, thread)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_done)
        thread.start()

    def _thread_done(self):
        self._thread = None
        self._worker = None
        self._running = False
        self._validate()
        if self._close_after_work:
            self.close()

    def _check_servers(self):
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            return
        self.status_label.setText("ĐANG KIỂM TRA MÁY CHỦ")
        self._show_page(0)
        worker = MetadataWorker(config)
        def connect(w, thread):
            w.status.connect(self._server_checked)
            w.finished.connect(self._metadata_done)
            w.finished.connect(thread.quit)
        self._launch(worker, connect)

    def _server_checked(self, name: str, status: str):
        row = self._server_rows.get(name)
        if row is not None:
            self._set_cell(row, 1, status)
        self._log(f"{name}: {status}")

    def _metadata_done(self, message: str):
        self.status_label.setText("KIỂM TRA HOÀN TẤT")
        self._log(message)

    def _start_download(self):
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            return
        if config.output.exists():
            answer = QMessageBox.question(self, "Tệp đã tồn tại", f"Thay thế tệp hiện có?\n{config.output}",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._show_page(0)
        self._reset_session(config)
        worker = DownloadWorker(config)
        def connect(w, thread):
            w.status.connect(self._download_event)
            w.finished.connect(self._download_finished)
            w.failed.connect(self._download_failed)
            w.finished.connect(thread.quit)
            w.failed.connect(thread.quit)
        self._launch(worker, connect)

    def _reset_session(self, config: ClientConfig):
        self.state = "downloading"
        self.status_label.setText("ĐANG KẾT NỐI")
        self.progress.setValue(0)
        self.percent.setText("0%")
        self.speed_value.setText("—")
        self.eta_value.setText("—")
        self.available_value.setText("—")
        self.integrity_value.setText("Chờ xác minh")
        self.progress_text.setText("Đang lấy metadata…")
        self._history.clear()
        self._rate_time = time.monotonic()
        self._last_bytes = {}
        self.activity_list.clear()
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(False)
        self._populate_servers(config)

    def _download_event(self, event: DownloadEvent):
        if event.kind == "metadata":
            self.status_label.setText("ĐANG TẢI")
            self.progress_text.setText(f"0 / {event.total_chunks} chunk")
            self._log("Đã đối chiếu metadata · " + event.message)
        elif event.kind == "verifying":
            self._poll_progress()
            self.state = "verifying"
            self.status_label.setText("ĐANG XÁC MINH SHA-256")
            self.integrity_value.setText("Đang xác minh")
            self._log("Đã tải đủ chunk, đang xác minh SHA-256 toàn tệp")
        elif event.kind in {"server_connected", "server_failed", "server_excluded", "chunk_requeued"}:
            name = event.server or "Server"
            row = self._server_rows.get(name)
            texts = {"server_connected": "Đang truyền", "server_failed": "Mất kết nối",
                     "server_excluded": "Không cùng phiên bản", "chunk_requeued": "Đang chuyển chunk"}
            if row is not None:
                self._set_cell(row, 1, texts[event.kind])
            self._log(f"{name}: {texts[event.kind]}" + (f" · {event.message}" if event.message else ""))
        elif event.kind == "completed":
            self._poll_progress()

    def _poll_progress(self):
        worker = self._worker
        if not isinstance(worker, DownloadWorker):
            return
        latest, bytes_by_server, chunks_by_server = worker.snapshot()
        if latest is not None:
            total = latest.total_chunks
            completed = latest.completed_chunks
            self.progress.setValue(round(completed * 1000 / total) if total else 1000)
            self.percent.setText(f"{completed * 100 / total:.0f}%" if total else "100%")
            self.progress_text.setText(f"{completed:,} / {total:,} chunk · {sum(bytes_by_server.values()):,} byte đã tải")
            for name, amount in bytes_by_server.items():
                row = self._server_rows.get(name)
                if row is not None:
                    self._set_cell(row, 2, f"{chunks_by_server.get(name, 0):,} chunk")
        now = time.monotonic()
        if not bytes_by_server or now - self._rate_time < 1:
            return
        self._history.append((now, bytes_by_server))
        while self._history and now - self._history[0][0] > 5:
            self._history.popleft()
        elapsed = max(now - self._rate_time, .001)
        rates = {name: max(0, amount - self._last_bytes.get(name, 0)) / elapsed
                 for name, amount in bytes_by_server.items()}
        self._last_bytes = bytes_by_server
        self._rate_time = now
        total_rate = sum(rates.values())
        self.speed_value.setText(f"{total_rate / 1_048_576:.1f} MB/s")
        for name, rate in rates.items():
            row = self._server_rows.get(name)
            if row is not None:
                self._set_cell(row, 3, f"{rate / 1_048_576:.1f} MB/s")

    def _download_finished(self, result: DownloadResult):
        self._poll_progress()
        self.state = "completed"
        self.status_label.setText("TẢI HOÀN TẤT · SHA-256 HỢP LỆ")
        self.integrity_value.setText("Đã xác minh")
        self.percent.setText("100%")
        self.progress.setValue(1000)
        self.progress_text.setText(f"{result.bytes_written:,} byte · SHA-256: {result.file_sha256}")
        self.progress_text.setToolTip(result.file_sha256)
        self.cancel_button.setVisible(False)
        self._log(f"Đã lưu: {result.output} · SHA-256 {result.file_sha256}")

    def _download_failed(self, error: Exception):
        self._poll_progress()
        self.state = "error"
        self.status_label.setText("TẢI THẤT BẠI")
        self.integrity_value.setText("Chưa xác minh")
        self.cancel_button.setVisible(False)
        self._log(f"Lỗi: {error}")
        QMessageBox.warning(self, "Không thể hoàn tất tải", str(error))

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        text = f"{timestamp}  {message}"
        self.activity_list.insertItem(0, text)
        if self.activity_list.count() > 5:
            self.activity_list.takeItem(5)
        self.full_log.addItem(text)
        if self.full_log.count() > 500:
            self.full_log.takeItem(0)

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            self._close_after_work = True
            event.ignore()
            self.status_label.setText("ĐANG CHỜ PHIÊN KẾT THÚC")
        else:
            event.accept()
