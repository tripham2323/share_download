"""Desktop dashboard, configuration and TCP session lifecycle."""

from __future__ import annotations

from collections import deque
import errno
from pathlib import Path
import time

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QColor, QFont

from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from client import ClientConfig, DownloadCancelled, DownloadController, DownloadError, DownloadEvent, DownloadResult
from checkpoint import ResumeConflict, state_path_for
from desktop.config_form import ConfigForm
from desktop.summary import SessionSummary
from desktop.theme import STYLESHEET
from desktop.worker import DownloadWorker, MetadataWorker, ResumePreview


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


def error_advice(error: Exception) -> str:
    if isinstance(error, ResumeConflict):
        return "Checkpoint không khớp. Kiểm tra nguồn tệp hoặc chọn Tải lại từ đầu sau khi xem dữ liệu tạm."
    code = error.code if isinstance(error, DownloadError) else (
        "DISK_FULL" if isinstance(error, OSError) and error.errno == errno.ENOSPC else "DOWNLOAD_ERROR"
    )
    return {
        "NO_METADATA": "Kiểm tra IP, port và trạng thái của các server rồi thử lại.",
        "METADATA_CONFLICT": "Các server không cung cấp cùng phiên bản tệp. Đồng bộ tệp nguồn trước khi tải lại.",
        "ALL_SERVERS_FAILED": "Tất cả server đã ngắt kết nối. Kiểm tra mạng rồi tiếp tục từ checkpoint.",
        "HASH_MISMATCH": "SHA-256 toàn tệp không khớp. Kiểm tra tệp nguồn trên server trước khi tải lại.",
        "DISK_FULL": "Ổ đĩa không đủ chỗ. Giải phóng dung lượng rồi tiếp tục.",
    }.get(code, "Xem nhật ký và kiểm tra kết nối, quyền ghi thư mục đích trước khi thử lại.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ShareDownload · Truyền tệp đa nguồn")
        self.resize(1280, 780)
        self.setMinimumSize(760, 480)
        self.setStyleSheet(STYLESHEET)
        self.state = "idle"
        self._thread: QThread | None = None
        self._worker: DownloadWorker | MetadataWorker | None = None
        self._running = False
        self._close_after_work = False
        self._active_part: Path | None = None
        self._controller: DownloadController | None = None
        self._resume_preview: ResumePreview | None = None
        self._server_rows: dict[str, int] = {}
        self._rate_samples: deque[tuple[float, float]] = deque(maxlen=120)
        self._last_bytes: dict[str, int] = {}
        self._rate_time = time.monotonic()
        self._session_started = 0.0
        self._file_size = 0
        self._verified_bytes = 0
        self._server_count = 0
        self._available_servers: set[str] = set()
        self._failed_servers: set[str] = set()
        self._build()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(200)
        self.poll_timer.timeout.connect(self._poll_progress)
        self.poll_timer.start()
        self.config_form.changed.connect(self._on_config_changed)
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
        heading = QVBoxLayout()
        heading.setSpacing(10)
        title = label("Phiên tải hiện tại", "pageTitle")
        heading.addWidget(title)
        subtitle = label("Theo dõi tiến độ và chất lượng kết nối theo thời gian thực.", "muted")
        subtitle.setWordWrap(True)
        heading.addWidget(subtitle)
        actions = QHBoxLayout()
        actions.addStretch()
        self.check_button = QPushButton("Kiểm tra máy chủ")
        self.check_button.clicked.connect(self._check_servers)
        actions.addWidget(self.check_button)
        self.start_button = QPushButton("Bắt đầu tải")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda: self._start_download())
        actions.addWidget(self.start_button)
        self.cancel_button = QPushButton("Hủy tải")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.clicked.connect(self._cancel_download)
        self.cancel_button.setVisible(False)
        actions.addWidget(self.cancel_button)
        heading.addLayout(actions)
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
        self.path_label = label("Cấu hình máy chủ, tệp nguồn và nơi lưu trong mục bên trái.", "pathLabel")
        self.path_label.setToolTip("")
        self.path_label.setWordWrap(True)
        hero_layout.addWidget(self.path_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        hero_layout.addWidget(self.progress)
        self.progress_text = label("—", "muted")
        hero_layout.addWidget(self.progress_text)
        layout.addWidget(hero)
        self.resume_banner, resume_layout = card(name="hero")
        self.resume_label = label("", "warning")
        self.resume_label.setWordWrap(True)
        resume_layout.addWidget(self.resume_label)
        resume_actions = QHBoxLayout()
        self.resume_button = QPushButton("Tiếp tục tải")
        self.resume_button.setObjectName("primary")
        self.resume_button.clicked.connect(lambda: self._start_download(resume=True))
        self.restart_button = QPushButton("Tải lại từ đầu")
        self.restart_button.clicked.connect(self._restart_download)
        resume_actions.addWidget(self.resume_button)
        resume_actions.addWidget(self.restart_button)
        resume_actions.addStretch()
        resume_layout.addLayout(resume_actions)
        self.resume_banner.hide()
        layout.addWidget(self.resume_banner)
        self.summary = SessionSummary()
        layout.addWidget(self.summary)

        metrics = QGridLayout()
        self._metrics_layout = metrics
        self._metric_panels: list[QFrame] = []
        self._metric_columns = 4
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

    def _metric(self, layout: QGridLayout, title: str, initial: str, caption: str) -> QLabel:
        panel, stack = card(name="metric")
        stack.addWidget(label(title, "muted"))
        value = label(initial, "metricValue")
        stack.addWidget(value)
        stack.addWidget(label(caption, "caption"))
        layout.addWidget(panel, 0, len(self._metric_panels))
        self._metric_panels.append(panel)
        return value

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "_metric_panels"):
            return
        columns = 2 if self.width() < 1000 else 4
        if columns == self._metric_columns:
            return
        self._metric_columns = columns
        for index, panel in enumerate(self._metric_panels):
            self._metrics_layout.removeWidget(panel)
            self._metrics_layout.addWidget(panel, index // columns, index % columns)

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for n, button in enumerate(self.nav_buttons):
            button.setChecked(n == index)
        self.breadcrumb.setText(
            "Ứng dụng   /   " + ("Tổng quan phiên tải", "Cấu hình máy chủ", "Nhật ký phiên")[index]
        )

    def _on_config_changed(self):
        self._resume_preview = None
        self.resume_banner.hide()
        self._server_rows = {}
        self._validate()

    def _validate(self) -> None:
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            valid = False
            if not self._running:
                self.server_table.setRowCount(0)
                self._server_rows = {}
        else:
            valid = True
            if not self._running:
                self.file_title.setText(config.filename)
                self.path_label.setText(str(config.output))
                self.path_label.setToolTip(str(config.output))
            if not self._server_rows:
                self._populate_servers(config)
        self.start_button.setEnabled(valid and not self._running
                                     and (self._resume_preview is None
                                          or self._resume_preview.status == "none"))
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
        if column == 1:
            font = item.font()
            if value == "Đã kết nối":
                item.setForeground(QColor("#15803d"))
                font.setBold(True)
            elif any(err in value for err in ("Không", "Khác", "Lỗi", "ngắt")):
                item.setForeground(QColor("#dc2626"))
                font.setBold(True)
            else:
                item.setForeground(QColor("#475569"))
                font.setBold(False)
            item.setFont(font)

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
        self.config_form.setEnabled(False)
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
        self.config_form.setEnabled(True)
        self._controller = None
        self._validate()
        if self._close_after_work:
            self.close()

    def _check_servers(self):
        if self._running:
            return
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            return
        self._server_count = len(config.servers)
        self._available_servers.clear()
        self.available_value.setText(f"0 / {self._server_count}")
        self.resume_banner.hide()
        self._populate_servers(config)
        self._resume_preview = None
        self.status_label.setText("ĐANG KIỂM TRA MÁY CHỦ")
        self._show_page(0)
        worker = MetadataWorker(config)
        def connect(w, thread):
            w.status.connect(self._server_checked)
            w.resume_state.connect(self._resume_checked)
            w.finished.connect(self._metadata_done)
            w.finished.connect(thread.quit)
        self._launch(worker, connect)

    def _resume_checked(self, preview: ResumePreview):
        self._resume_preview = preview
        if preview.status == "valid":
            self.show_resume_banner(preview.verified, preview.total)
        elif preview.status == "conflict":
            self.show_resume_conflict(preview.reason)
        elif preview.status == "unavailable":
            self.resume_label.setText(
                "Chưa thể kiểm tra dữ liệu tạm vì không chọn được server. "
                "Kiểm tra kết nối rồi thử lại; dữ liệu .part được giữ nguyên."
            )
            self.resume_button.setVisible(False)
            self.restart_button.setVisible(False)
            self.resume_banner.show()
        else:
            self.resume_banner.hide()
        self._validate()

    def show_resume_banner(self, verified_chunks: int, total_chunks: int):
        percent = 100 * verified_chunks / total_chunks if total_chunks else 100
        self.resume_label.setText(
            f"Có phiên tải trước: {verified_chunks:,}/{total_chunks:,} chunk đã xác minh "
            f"({percent:.0f}%). Có thể tiếp tục mà không tải lại các chunk này."
        )
        self.restart_button.setVisible(True)
        self.resume_button.setVisible(True)
        self.resume_banner.show()
        self.progress.setValue(round(percent * 10))
        self.percent.setText(f"{percent:.0f}%")
        self.progress_text.setText(f"{verified_chunks:,} / {total_chunks:,} chunk đã kiểm tra trên đĩa")

    def show_resume_conflict(self, reason: str):
        self.resume_label.setText(
            f"Không thể tiếp tục an toàn: {reason}. "
            "Giữ dữ liệu cũ hoặc xác nhận tải lại từ đầu."
        )
        self.resume_button.setVisible(False)
        self.progress.setValue(0)
        self.percent.setText("—")
        self.progress_text.setText("Dữ liệu tạm chưa được xác minh")
        self.restart_button.setVisible(True)
        self.resume_banner.show()

    def _confirm_box(
        self,
        title: str,
        heading: str,
        body: str,
        confirm_text: str = "Đồng ý",
        cancel_text: str = "Hủy bỏ",
        is_danger: bool = False,
    ) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"<div style='font-size: 15px; font-weight: 700; color: #0f172a; margin-bottom: 6px;'>{heading}</div>")
        box.setInformativeText(f"<div style='font-size: 13px; color: #334155; line-height: 1.5;'>{body}</div>")
        btn_confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.YesRole)
        btn_cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.NoRole)
        box.setStyleSheet(
            "QDialog, QMessageBox { background-color: #ffffff; color: #0f172a; }"
            "QLabel { color: #1e293b; background: transparent; font-size: 13px; }"
            "QPushButton { background: #ffffff; color: #1e293b; border: 1px solid #cbd5e1; "
            "border-radius: 6px; padding: 7px 18px; min-width: 90px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #f1f5f9; border-color: #94a3b8; }"
        )
        if is_danger:
            btn_confirm.setStyleSheet(
                "QPushButton { background: #dc2626; color: #ffffff; border: 1px solid #dc2626; "
                "border-radius: 6px; padding: 7px 18px; min-width: 90px; font-weight: 600; font-size: 12px; }"
                "QPushButton:hover { background: #b91c1c; border-color: #b91c1c; }"
            )
        else:
            btn_confirm.setStyleSheet(
                "QPushButton { background: #2563d9; color: #ffffff; border: 1px solid #2563d9; "
                "border-radius: 6px; padding: 7px 18px; min-width: 90px; font-weight: 600; font-size: 12px; }"
                "QPushButton:hover { background: #1d4ed8; border-color: #1d4ed8; }"
            )
        box.setDefaultButton(btn_confirm)
        box.exec()
        return box.clickedButton() == btn_confirm

    def _restart_download(self):
        confirmed = self._confirm_box(
            "Tải lại từ đầu",
            "Xác nhận tải lại từ đầu?",
            "Thay thế dữ liệu <b>.part</b> và <b>checkpoint</b> hiện có?<br>Dữ liệu cũ sẽ không được dùng lại.",
            confirm_text="Tải lại từ đầu",
            cancel_text="Hủy bỏ",
        )
        if confirmed:
            self._start_download(resume=False, confirmed_reset=True)

    def _server_checked(self, name: str, status: str):
        row = self._server_rows.get(name)
        if row is not None:
            self._set_cell(row, 1, status)
        if status == "Đã kết nối":
            self._available_servers.add(name)
        else:
            self._available_servers.discard(name)
        self.available_value.setText(f"{len(self._available_servers)} / {self._server_count}")
        self._log(f"{name}: {status}")

    def _metadata_done(self, message: str, error: Exception | None):
        self.status_label.setText("KHÔNG THỂ CHỌN NGUỒN TẢI" if error else "KIỂM TRA HOÀN TẤT")
        if error:
            self.progress_text.setText(error_advice(error))
            self.progress_text.setToolTip(str(error))
        self._log(message)

    def _start_download(self, *, resume: bool = False, confirmed_reset: bool = False):
        if self._running:
            return
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            return
        part = config.output.with_name(config.output.name + ".part")
        if part.exists() or state_path_for(part).exists():
            if self._resume_preview is None:
                self._check_servers()
                return
            if resume and self._resume_preview.status != "valid":
                return
            if not resume and not confirmed_reset:
                self.resume_banner.show()
                return
        if config.output.exists():
            confirmed = self._confirm_box(
                "Tệp đã tồn tại",
                "Tệp đích đã tồn tại trên đĩa!",
                f"Đường dẫn: <b>{config.output}</b><br><br>Bạn có muốn tải lại từ đầu và ghi đè lên tệp hiện có không?",
                confirm_text="Tải lại (Ghi đè)",
                cancel_text="Hủy bỏ",
            )
            if not confirmed:
                return

        self._show_page(0)
        self._reset_session(config)
        self._controller = DownloadController()
        worker = DownloadWorker(config, self._controller, resume=resume)
        def connect(w, thread):
            w.status.connect(self._download_event)
            w.finished.connect(self._download_finished)
            w.failed.connect(self._download_failed)
            w.finished.connect(thread.quit)
            w.failed.connect(thread.quit)
        self._launch(worker, connect)

    def _reset_session(self, config: ClientConfig):
        self._active_part = config.output.with_name(config.output.name + ".part")
        self.state = "downloading"
        self.status_label.setText("ĐANG KẾT NỐI")
        self.progress.setValue(0)
        self.percent.setText("0%")
        self.speed_value.setText("—")
        self.eta_value.setText("—")
        self.available_value.setText("—")
        self.integrity_value.setText("Chờ xác minh")
        self.progress_text.setText("Đang lấy metadata…")
        self._rate_samples.clear()
        self._rate_time = time.monotonic()
        self._last_bytes = {}
        self._session_started = self._rate_time
        self._file_size = 0
        self._verified_bytes = 0
        self._server_count = len(config.servers)
        self._failed_servers.clear()
        self._available_servers.clear()
        self.available_value.setText(f"0 / {self._server_count}")
        self.summary.hide()
        self.activity_list.clear()
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Hủy tải")
        self.resume_banner.hide()
        self._populate_servers(config)

    def _download_event(self, event: DownloadEvent):
        if event.kind == "metadata":
            self._file_size = event.file_size
            self._verified_bytes = event.verified_bytes
            self.status_label.setText("ĐANG TẢI")
            self.progress.setValue(round(event.completed_chunks * 1000 / event.total_chunks)
                                   if event.total_chunks else 1000)
            self.percent.setText(f"{event.completed_chunks * 100 / event.total_chunks:.0f}%"
                                 if event.total_chunks else "100%")
            self.progress_text.setText(f"{event.completed_chunks:,} / {event.total_chunks:,} chunk")
            self._log("Đã đối chiếu metadata · " + event.message)
        elif event.kind == "verifying":
            self._poll_progress()
            self.state = "verifying"
            self.status_label.setText("ĐANG XÁC MINH SHA-256")
            self.integrity_value.setText("Đang xác minh")
            self._log("Đã tải đủ chunk, đang xác minh SHA-256 toàn tệp")
        elif event.kind in {"server_connected", "server_failed", "server_excluded",
                            "server_unavailable", "chunk_requeued"}:
            name = event.server or "Server"
            row = self._server_rows.get(name)
            texts = {"server_connected": "Đang truyền", "server_failed": "Mất kết nối",
                     "server_excluded": "Không cùng phiên bản",
                     "server_unavailable": "Không phản hồi", "chunk_requeued": "Đang chuyển chunk"}
            if row is not None:
                self._set_cell(row, 1, texts[event.kind])
            if event.kind == "server_connected":
                self._available_servers.add(name)
            elif event.kind in {"server_failed", "server_excluded", "server_unavailable"}:
                self._available_servers.discard(name)
                if event.kind == "server_failed":
                    self._failed_servers.add(name)
            self.available_value.setText(f"{len(self._available_servers)} / {self._server_count}")
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
            self.progress_text.setText(
                f"{completed:,} / {total:,} chunk · "
                f"{self._verified_bytes + sum(bytes_by_server.values()):,} / {self._file_size:,} byte"
            )
            for name, amount in bytes_by_server.items():
                row = self._server_rows.get(name)
                if row is not None:
                    self._set_cell(row, 2, f"{amount:,} B · {chunks_by_server.get(name, 0):,} chunk")
        now = time.monotonic()
        if not bytes_by_server or now - self._rate_time < 1:
            return
        elapsed = max(now - self._rate_time, .001)
        rates = {name: max(0, amount - self._last_bytes.get(name, 0)) / elapsed
                 for name, amount in bytes_by_server.items()}
        self._last_bytes = bytes_by_server
        self._rate_time = now
        total_rate = sum(rates.values())
        self._rate_samples.append((now - self._session_started, total_rate))
        self.speed_value.setText(f"{total_rate / 1_048_576:.1f} MB/s")
        if total_rate > 0 and self.state == "downloading":
            remaining = max(0, self._file_size - self._verified_bytes - sum(bytes_by_server.values()))
            self.eta_value.setText(f"{remaining / total_rate:.0f} giây")
        elif self.state == "downloading":
            self.eta_value.setText("—")
        for name, rate in rates.items():
            row = self._server_rows.get(name)
            if row is not None:
                self._set_cell(row, 3, f"{rate / 1_048_576:.1f} MB/s")

    def _download_finished(self, result: DownloadResult):
        self._poll_progress()
        self.state = "completed"
        self.status_label.setText("TẢI HOÀN TẤT · SHA-256 HỢP LỆ")
        self.integrity_value.setText("Đã xác minh")
        self.progress.setValue(1000)
        self.progress_text.setText(f"{result.bytes_written:,} byte · SHA-256: {result.file_sha256}")
        self.eta_value.setText("0 giây")
        self.summary.set_result(result, time.monotonic() - self._session_started,
                                list(self._rate_samples), self._failed_servers)
        for name, count in result.per_server_chunks.items():
            row = self._server_rows.get(name)
            if row is not None:
                self._set_cell(row, 2, f"{result.per_server_bytes.get(name, 0):,} B · {count:,} chunk")
        self.progress_text.setToolTip(result.file_sha256)
        self.cancel_button.setVisible(False)
        self._log(f"Đã lưu: {result.output} · SHA-256 {result.file_sha256}")

    def _download_failed(self, error: Exception):
        self._poll_progress()
        self.cancel_button.setVisible(False)
        if isinstance(error, DownloadCancelled):
            self.state = "cancelled"
            self.status_label.setText("ĐÃ HỦY · DỮ LIỆU TẠM ĐƯỢC GIỮ LẠI")
            self._log("Đã hủy phiên tải; tệp .part chưa được công bố")
            return
        self.state = "error"
        self.status_label.setText("TẢI THẤT BẠI")
        self.integrity_value.setText("Chưa xác minh")
        self._log(f"Lỗi: {error}")
        advice = error_advice(error)
        part_note = (
            f"\n\nDữ liệu tạm: {self._active_part}"
            if self._active_part is not None and self._active_part.exists() else ""
        )
        self.progress_text.setText(advice)
        self.progress_text.setToolTip(str(error))
        if not self._close_after_work:
            box = QMessageBox(self)
            box.setWindowTitle("Không thể hoàn tất tải")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText("<h3>Không thể hoàn tất phiên tải</h3>")
            box.setInformativeText(f"<p style='color: #334155; font-size: 13px;'>{advice}{part_note}</p><p style='color: #64748b; font-size: 11px;'>Chi tiết: {error}</p>")
            btn_ok = box.addButton("Đã hiểu", QMessageBox.ButtonRole.AcceptRole)
            box.setDefaultButton(btn_ok)
            box.setStyleSheet(
                "QDialog, QMessageBox { background-color: #ffffff; color: #0f172a; }"
                "QLabel { color: #1e293b; background: transparent; font-size: 13px; }"
                "QPushButton { background: #2563d9; color: #ffffff; border: 1px solid #2563d9; border-radius: 6px; padding: 7px 18px; min-width: 80px; font-weight: 600; }"
                "QPushButton:hover { background: #1d4ed8; }"
            )
            box.exec()

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        text = f"{timestamp}  {message}"
        self.activity_list.insertItem(0, text)
        if self.activity_list.count() > 5:
            self.activity_list.takeItem(5)
        self.full_log.addItem(text)
        if self.full_log.count() > 500:
            self.full_log.takeItem(0)

    def _cancel_download(self, *, confirm: bool = True):
        if self._controller is None or not self._running:
            return
        if confirm:
            confirmed = self._confirm_box(
                "Hủy phiên tải",
                "Bạn có chắc muốn dừng tải?",
                "Dừng tải? Tệp đang tải sẽ không được công bố; dữ liệu <b>.part</b> được giữ lại để tiếp tục sau.",
                confirm_text="Dừng tải",
                cancel_text="Tải tiếp",
                is_danger=True,
            )
            if not confirmed:
                return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Đang dừng…")
        self.status_label.setText("ĐANG DỪNG PHIÊN TẢI")
        self._controller.cancel()

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            if not self._close_after_work:
                confirmed = self._confirm_box(
                    "Đóng ứng dụng",
                    "Đang có tiến trình hoạt động!",
                    "Dừng phiên tải và thoát khỏi ứng dụng ngay?",
                    confirm_text="Thoát",
                    cancel_text="Không thoát",
                    is_danger=True,
                )
                if not confirmed:
                    event.ignore()
                    return
            self._close_after_work = True
            if isinstance(self._worker, DownloadWorker):
                self._cancel_download(confirm=False)
            event.ignore()
            self.status_label.setText("ĐANG CHỜ PHIÊN KẾT THÚC")
        else:
            event.accept()
