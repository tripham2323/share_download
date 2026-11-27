"""Transfer workspace, configuration and TCP session lifecycle."""

from __future__ import annotations

from collections import deque
import errno
from pathlib import Path
import time

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QBoxLayout, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from client import (
    ClientConfig, DownloadCancelled, DownloadController, DownloadError, DownloadEvent,
    DownloadResult, ServerEndpoint,
)
from checkpoint import ResumeConflict, state_path_for
from desktop.client_catalog import CatalogDialog, CatalogWorker
from desktop.config_form import ConfigForm
from desktop.summary import SessionSummary
from desktop.theme import MUTED, NEGATIVE, POSITIVE, STYLESHEET, WARNING
from desktop.worker import DownloadWorker, MetadataWorker, ResumePreview


def label(text: str, object_name: str = "") -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    if object_name:
        result.setObjectName(object_name)
    return result


def section(name: str, inset: int = 16) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(inset, inset, inset, inset)
    layout.setSpacing(8)
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
        self.resize(1120, 760)
        self.setMinimumSize(700, 480)
        self.setStyleSheet(STYLESHEET)
        self.state = "idle"
        self._thread: QThread | None = None
        self._worker: DownloadWorker | MetadataWorker | CatalogWorker | None = None
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
        self._verified_chunks = 0
        self._total_chunks = 0
        self._server_count = 0
        self._available_servers: set[str] = set()
        self._failed_servers: set[str] = set()
        self._build()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(200)
        self.poll_timer.timeout.connect(self._poll_progress)
        self.poll_timer.start()
        self.config_form.changed.connect(self._on_config_changed)
        self._on_config_changed()

    def _build(self) -> None:
        body = QWidget()
        self.setCentralWidget(body)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        masthead = QFrame()
        masthead.setObjectName("masthead")
        nav_layout = QHBoxLayout(masthead)
        nav_layout.setContentsMargins(24, 8, 24, 8)
        nav_layout.setSpacing(8)
        nav_layout.addWidget(label("⇣", "brandMark"))
        nav_layout.addWidget(label("ShareDownload", "brand"))
        nav_layout.addStretch()
        self.pages = QStackedWidget()
        self.nav_buttons = []
        for index, title in enumerate(("Tải tệp", "Cấu hình", "Nhật ký")):
            button = QPushButton(title)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, n=index: self._show_page(n))
            button.setToolTip(f"{title} (Alt+{index + 1})")
            shortcut = QShortcut(QKeySequence(f"Alt+{index + 1}"), self)
            shortcut.activated.connect(button.click)
            nav_layout.addWidget(button)
            self.nav_buttons.append(button)
        outer.addWidget(masthead)
        outer.addWidget(self.pages, 1)

        self.pages.addWidget(self._overview())
        self.config_form = ConfigForm()
        config_page = QWidget()
        config_layout = QVBoxLayout(config_page)
        config_layout.setContentsMargins(24, 20, 24, 24)
        config_layout.setSpacing(12)
        config_actions = QHBoxLayout()
        config_actions.addWidget(label("Cấu hình tải", "pageTitle"))
        config_actions.addStretch()
        self.open_button = QPushButton("&Mở JSON…")
        self.open_button.clicked.connect(self._open_config)
        self.save_button = QPushButton("&Lưu JSON…")
        self.save_button.clicked.connect(self._save_config)
        config_actions.addWidget(self.open_button)
        config_actions.addWidget(self.save_button)
        config_layout.addLayout(config_actions)
        config_layout.addWidget(self.config_form)
        config_scroll = QScrollArea()
        config_scroll.setWidgetResizable(True)
        config_scroll.setFrameShape(QFrame.Shape.NoFrame)
        config_scroll.setWidget(config_page)
        self.pages.addWidget(config_scroll)
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(24, 20, 24, 24)
        log_layout.addWidget(label("Nhật ký phiên", "pageTitle"))
        self.full_log = QListWidget()
        self.full_log.setAccessibleName("Nhật ký kết nối và tải tệp")
        log_layout.addWidget(self.full_log)
        self.pages.addWidget(log_page)
        self._catalog_dialog = CatalogDialog(self)
        self._catalog_dialog.file_chosen.connect(self._catalog_selected)
        self._catalog_dialog.refresh_requested.connect(self._open_catalog)
        self._catalog_dialog.rejected.connect(self._cancel_catalog)
        self._show_page(0)

    def _overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        heading.addWidget(label("Tải tệp", "pageTitle"))
        heading.addStretch()
        self.catalog_button = QPushButton("Chọn từ &máy chủ…")
        self.catalog_button.clicked.connect(self._open_catalog)
        heading.addWidget(self.catalog_button)
        self.check_button = QPushButton("&Kiểm tra nguồn")
        self.check_button.clicked.connect(self._check_servers)
        heading.addWidget(self.check_button)
        self.start_button = QPushButton("Bắt đầu tải")
        self.start_button.setObjectName("primary")
        self.start_button.setShortcut(QKeySequence("Ctrl+Return"))
        self.start_button.setToolTip("Ctrl+Enter")
        self.start_button.clicked.connect(self._start_primary)
        heading.addWidget(self.start_button)
        self.cancel_button = QPushButton("Dừng tải")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.clicked.connect(self._cancel_download)
        self.cancel_button.hide()
        heading.addWidget(self.cancel_button)
        layout.addLayout(heading)

        transfer, transfer_layout = section("transfer")
        file_line = QHBoxLayout()
        self.file_title = label("Chưa chọn tệp", "pageTitle")
        self.file_title.setWordWrap(True)
        self.file_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        file_line.addWidget(self.file_title, 1)
        self.percent = label("—", "percent")
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        file_line.addWidget(self.percent)
        transfer_layout.addLayout(file_line)
        self.path_label = label("", "muted")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        transfer_layout.addWidget(self.path_label)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Tiến độ tải tệp")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        transfer_layout.addWidget(self.progress)
        self.progress_text = label("—", "muted")
        self.progress_text.setWordWrap(True)
        transfer_layout.addWidget(self.progress_text)
        self.resume_banner, resume_layout = section("resumeNotice", 12)
        self.resume_label = label("", "warning")
        self.resume_label.setWordWrap(True)
        resume_layout.addWidget(self.resume_label)
        self.restart_button = QPushButton("Tải lại từ đầu…")
        self.restart_button.clicked.connect(self._restart_download)
        resume_layout.addWidget(self.restart_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.resume_banner.hide()
        transfer_layout.addWidget(self.resume_banner)
        status = QHBoxLayout()
        self.status_label = label("", "state")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status.addWidget(self.status_label, 1)
        self.integrity_value = label("—", "muted")
        status.addWidget(label("SHA-256 toàn tệp:", "muted"))
        status.addWidget(self.integrity_value)
        transfer_layout.addLayout(status)
        transfer_layout.addSpacing(4)
        rates = QHBoxLayout()
        rates.addWidget(label("Tốc độ", "muted"))
        self.speed_value = label("—")
        rates.addWidget(self.speed_value)
        rates.addSpacing(16)
        rates.addWidget(label("Ước tính còn lại", "muted"))
        self.eta_value = label("—")
        rates.addWidget(self.eta_value)
        rates.addStretch()
        transfer_layout.addLayout(rates)
        layout.addWidget(transfer)

        self._evidence = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._evidence.setSpacing(16)
        source_panel, source_layout = section("evidence", 0)
        source_header = QHBoxLayout()
        source_header.addWidget(label("Đóng góp từ máy chủ", "sectionTitle"))
        source_header.addStretch()
        source_header.addWidget(label("Nguồn phù hợp:", "caption"))
        self.available_value = label("—", "caption")
        source_header.addWidget(self.available_value)
        source_layout.addLayout(source_header)
        self.server_table = QTableWidget(0, 6)
        self.server_table.setAccessibleName("Đóng góp theo máy chủ trong phiên tải")
        self.server_table.setHorizontalHeaderLabels(
            ("Máy chủ", "Kết nối", "Đã nhận", "Chunk", "% byte phiên", "Tốc độ")
        )
        headers = self.server_table.horizontalHeader()
        headers.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        headers.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 6):
            headers.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            self.server_table.horizontalHeaderItem(column).setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
        self.server_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.server_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.server_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.server_table.setShowGrid(False)
        self.server_table.verticalHeader().hide()
        self.server_table.verticalHeader().setDefaultSectionSize(36)
        self.server_table.setMinimumHeight(150)
        source_layout.addWidget(self.server_table)
        self.sources_note = label("Thêm máy chủ trong Cấu hình để tìm tệp.", "caption")
        self.sources_note.setWordWrap(True)
        source_layout.addWidget(self.sources_note)
        self._evidence.addWidget(source_panel, 7)
        activity_panel, activity_layout = section("evidence", 0)
        activity_header = QHBoxLayout()
        activity_header.addWidget(label("Hoạt động gần đây", "sectionTitle"))
        activity_header.addStretch()
        view_log = QPushButton("Xem nhật ký")
        view_log.clicked.connect(lambda: self._show_page(2))
        activity_header.addWidget(view_log)
        activity_layout.addLayout(activity_header)
        self.activity_list = QListWidget()
        self.activity_list.setAccessibleName("Hoạt động tải tệp gần đây")
        self.activity_list.setMinimumHeight(150)
        activity_layout.addWidget(self.activity_list)
        self.activity_note = label("Chưa có sự kiện kết nối hoặc tải.", "caption")
        self.activity_note.setWordWrap(True)
        activity_layout.addWidget(self.activity_note)
        self._evidence.addWidget(activity_panel, 4)
        layout.addLayout(self._evidence, 1)
        self.summary = SessionSummary()
        layout.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _start_primary(self) -> None:
        self._start_download(resume=self._resume_preview is not None
                             and self._resume_preview.status == "valid")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_evidence"):
            direction = (QBoxLayout.Direction.TopToBottom if self.width() < 980
                         else QBoxLayout.Direction.LeftToRight)
            self._evidence.setDirection(direction)

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for n, button in enumerate(self.nav_buttons):
            button.setChecked(n == index)
        self.nav_buttons[index].setFocus()

    def _on_config_changed(self):
        if not self._running:
            self._resume_preview = None
            self.resume_banner.hide()
            self._server_rows.clear()
            self.state = "idle"
            self.status_label.setText("Chưa bắt đầu tải")
            self.progress.setValue(0)
            self.percent.setText("—")
            self.progress_text.clear()
            self.progress_text.setToolTip("")
            self.speed_value.setText("—")
            self.eta_value.setText("—")
            self.available_value.setText("—")
            self._set_integrity("—")
        self._validate()

    def _validate(self) -> None:
        filename = self.config_form.filename_input.text().strip()
        sources = self.config_form.catalog_sources()
        self.catalog_button.setEnabled(bool(sources) and not self._running)
        self.catalog_button.setToolTip(
            "Chọn tệp do máy chủ niêm yết" if sources else "Nhập tên và địa chỉ các máy chủ trong Cấu hình"
        )
        try:
            config = self.config_form.to_client_config()
        except ValueError:
            config = None
        if not self._running:
            self.file_title.setText(filename or "Chưa chọn tệp")
            self.file_title.setToolTip(filename)
            output = self.config_form.output_input.text().strip()
            target = str(config.output) if config else output
            self.path_label.setText(f"Lưu vào: {target}" if target else "Chưa chọn nơi lưu")
            self.path_label.setToolTip(target)
            if not self._server_rows:
                self._populate_servers(sources)
            if self.state == "idle":
                if not sources:
                    text = "Thêm máy chủ trong Cấu hình để bắt đầu."
                elif config is None:
                    text = "Chọn tệp từ danh mục hoặc nhập tên tệp và đường dẫn lưu trong Cấu hình."
                else:
                    text = "Chọn Bắt đầu tải hoặc kiểm tra nguồn trước."
                self.progress_text.setText(text)
        preview = self._resume_preview.status if self._resume_preview else "none"
        self.start_button.setText("Tiếp tục tải" if preview == "valid" else "Bắt đầu tải")
        self.start_button.setEnabled(config is not None and not self._running
                                     and preview in {"none", "valid"})
        self.check_button.setEnabled(config is not None and not self._running)

    def _populate_servers(self, sources: tuple[ServerEndpoint, ...]) -> None:
        self.server_table.setRowCount(len(sources))
        self._server_rows.clear()
        self.sources_note.setText(
            "Tỷ lệ tính trên số byte nhận trong phiên này, không tính dữ liệu từ checkpoint."
            if sources else "Thêm máy chủ trong Cấu hình để tìm tệp."
        )
        for index, endpoint in enumerate(sources):
            self._server_rows[endpoint.name] = index
            self._set_cell(index, 0, f"{endpoint.name} · {endpoint.host}:{endpoint.port}")
            self._set_cell(index, 1, "Chưa kiểm tra")
            for column in range(2, 6):
                self._set_cell(index, column, "—")

    def _set_cell(self, row: int, column: int, value: str) -> None:
        item = self.server_table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.server_table.setItem(row, column, item)
            if column >= 2:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setText(value)
        item.setToolTip(value)
        if column == 1:
            if value in {"Đã kết nối", "Đang truyền"}:
                color = POSITIVE
            elif "Khác phiên bản" in value or "Không cùng phiên bản" in value:
                color = WARNING
            elif value.startswith(("Không", "Mất", "Lỗi")):
                color = NEGATIVE
            else:
                color = MUTED
            item.setForeground(QColor(color))

    def _set_integrity(self, value: str, tone: str = "muted") -> None:
        self.integrity_value.setText(value)
        if self.integrity_value.objectName() != tone:
            self.integrity_value.setObjectName(tone)
            style = self.integrity_value.style()
            style.unpolish(self.integrity_value)
            style.polish(self.integrity_value)

    def _open_catalog(self) -> None:
        sources = self.config_form.catalog_sources()
        if self._running or not sources:
            return
        dialog = self._catalog_dialog
        dialog.begin()
        self._controller = DownloadController()
        worker = CatalogWorker(sources, self.config_form.connect_timeout.value(),
                               self.config_form.read_timeout.value(), self._controller)

        def connect(w, thread):
            w.finished.connect(dialog.show_results)
            w.finished.connect(thread.quit)

        self._launch(worker, connect)
        if not dialog.isVisible():
            dialog.open()

    def _cancel_catalog(self) -> None:
        if isinstance(self._worker, CatalogWorker) and self._controller is not None:
            self._controller.cancel()

    def _catalog_selected(self, filename: str) -> None:
        self.config_form.choose_catalog_file(filename)
        self.start_button.setFocus()

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
        self.open_button.setEnabled(False)
        self.save_button.setEnabled(False)
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
        if isinstance(self._worker, CatalogWorker):
            self._catalog_dialog.end()
        self._thread = None
        self._worker = None
        self._running = False
        self.config_form.setEnabled(True)
        self.open_button.setEnabled(True)
        self.save_button.setEnabled(True)
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
        self.state = "checking"
        self._server_count = len(config.servers)
        self._available_servers.clear()
        self.available_value.setText(f"0 / {self._server_count}")
        self.resume_banner.hide()
        self.progress.setValue(0)
        self.percent.setText("—")
        self.progress_text.setText("Đang đối chiếu metadata của tệp…")
        self.progress_text.setToolTip("")
        self.speed_value.setText("—")
        self.eta_value.setText("—")
        self._set_integrity("Chưa xác minh")
        self._populate_servers(config.servers)
        self._resume_preview = None
        self.status_label.setText("Đang kiểm tra nguồn")
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
            self.restart_button.setVisible(False)
            self.resume_banner.show()
        else:
            self.resume_banner.hide()
        self._validate()

    def show_resume_banner(self, verified_chunks: int, total_chunks: int):
        percent = 100 * verified_chunks / total_chunks if total_chunks else 100
        self.resume_label.setText(
            f"Đã kiểm tra {verified_chunks:,}/{total_chunks:,} chunk trên đĩa. "
            "Tiếp tục tải sẽ dùng lại các chunk đã xác minh."
        )
        self.restart_button.setVisible(True)
        self.resume_banner.show()
        self.progress.setValue(round(percent * 10))
        self.percent.setText(f"{percent:.0f}%")
        self.progress_text.setText(f"{verified_chunks:,} / {total_chunks:,} chunk có sẵn từ phiên trước")

    def show_resume_conflict(self, reason: str):
        self.resume_label.setText(
            f"Không thể tiếp tục an toàn: {reason}. "
            "Giữ dữ liệu cũ hoặc xác nhận tải lại từ đầu."
        )
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
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(heading)
        box.setInformativeText(body)
        btn_confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.YesRole)
        btn_cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.NoRole)
        if is_danger:
            btn_confirm.setObjectName("danger")
        box.setDefaultButton(btn_cancel if is_danger else btn_confirm)
        box.setEscapeButton(btn_cancel)
        box.exec()
        return box.clickedButton() == btn_confirm

    def _restart_download(self):
        confirmed = self._confirm_box(
            "Tải lại từ đầu",
            "Xác nhận tải lại từ đầu?",
            "Dữ liệu .part và checkpoint cũ sẽ bị thay thế và không thể tiếp tục dùng.",
            confirm_text="Tải lại từ đầu",
            cancel_text="Hủy bỏ",
            is_danger=True,
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
        self.state = "error" if error else "ready"
        self.status_label.setText("Không thể chọn nguồn tải" if error else "Đã kiểm tra nguồn")
        if error:
            if isinstance(error, DownloadError) and error.code == "METADATA_CONFLICT":
                self.available_value.setText("—")
            self.progress_text.setText(error_advice(error))
            self.progress_text.setToolTip(str(error))
        elif self._resume_preview is None or self._resume_preview.status == "none":
            self.progress_text.setText("Có thể bắt đầu tải từ các nguồn phù hợp.")
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
                "Ghi đè tệp đã tồn tại?",
                f"{config.output}\n\nTệp đích sẽ được thay thế sau khi xác minh xong.",
                confirm_text="Ghi đè",
                cancel_text="Hủy bỏ",
                is_danger=True,
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
        self.status_label.setText("Đang kết nối")
        self.progress.setValue(0)
        self.percent.setText("—")
        self.speed_value.setText("—")
        self.eta_value.setText("—")
        self._set_integrity("Chờ xác minh")
        self.progress_text.setText("Đang đối chiếu metadata của tệp…")
        self.progress_text.setToolTip("")
        self._rate_samples.clear()
        self._rate_time = time.monotonic()
        self._last_bytes = {}
        self._session_started = self._rate_time
        self._file_size = 0
        self._verified_bytes = 0
        self._verified_chunks = 0
        self._total_chunks = 0
        self._server_count = len(config.servers)
        self._failed_servers.clear()
        self._available_servers.clear()
        self.available_value.setText(f"0 / {self._server_count}")
        self.activity_list.clear()
        self.activity_note.show()
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Dừng tải")
        self.resume_banner.hide()
        self._resume_preview = None
        self._populate_servers(config.servers)

    def _download_event(self, event: DownloadEvent):
        if event.kind == "metadata":
            self._file_size = event.file_size
            self._verified_bytes = event.verified_bytes
            self._verified_chunks = event.completed_chunks
            self._total_chunks = event.total_chunks
            self.status_label.setText("Đang tải tệp")
            self._display_progress(self._verified_bytes, self._verified_chunks)
            self._display_contributions({}, {}, 0)
            self._log("Đã đối chiếu metadata · " + event.message)
        elif event.kind == "verifying":
            self._poll_progress(force_rate=True)
            self.state = "verifying"
            self.status_label.setText("Đã nhận đủ dữ liệu")
            self._display_progress(self._file_size, event.total_chunks)
            self._set_integrity("Đang xác minh")
            self._clear_live_rates()
            self._log("Đã tải đủ chunk, đang xác minh SHA-256 toàn tệp")
        elif event.kind in {"server_connected", "server_failed", "server_excluded",
                            "server_unavailable", "chunk_requeued"}:
            name = event.server or "Server"
            row = self._server_rows.get(name)
            texts = {"server_connected": "Đang truyền", "server_failed": "Mất kết nối",
                     "server_excluded": "Khác phiên bản tệp",
                     "server_unavailable": "Không phản hồi", "chunk_requeued": "Chuyển chunk sang nguồn khác"}
            if row is not None and event.kind != "chunk_requeued":
                self._set_cell(row, 1, texts[event.kind])
            if event.kind == "server_connected":
                self._available_servers.add(name)
            elif event.kind in {"server_failed", "server_excluded", "server_unavailable"}:
                self._available_servers.discard(name)
                if event.kind == "server_failed":
                    self._failed_servers.add(name)
            self.available_value.setText(f"{len(self._available_servers)} / {self._server_count}")
            self._log(f"{name}: {texts[event.kind]}" + (f" · {event.message}" if event.message else ""))

    def _display_progress(self, ready_bytes: int, ready_chunks: int) -> None:
        ratio = min(ready_bytes / self._file_size, 1) if self._file_size else 1
        self.progress.setValue(min(999, round(ratio * 1000)) if ratio < 1 else 1000)
        self.percent.setText(f"{min(99, round(ratio * 100))}%" if ratio < 1 else "100%")
        self.progress_text.setText(
            f"{ready_bytes:,} / {self._file_size:,} B · {ready_chunks:,} / {self._total_chunks:,} chunk"
        )

    def _display_contributions(self, bytes_by_server: dict[str, int],
                               chunks_by_server: dict[str, int], total: int) -> None:
        for name, row in self._server_rows.items():
            amount = bytes_by_server.get(name, 0)
            self._set_cell(row, 2, f"{amount:,} B")
            self._set_cell(row, 3, f"{chunks_by_server.get(name, 0):,}")
            self._set_cell(row, 4, f"{amount / total:.1%}" if total else "—")

    def _clear_live_rates(self) -> None:
        self.speed_value.setText("—")
        self.eta_value.setText("—")
        for row in self._server_rows.values():
            self._set_cell(row, 5, "—")

    def _poll_progress(self, *, force_rate: bool = False):
        worker = self._worker
        if not isinstance(worker, DownloadWorker) or self.state != "downloading":
            return
        latest, bytes_by_server, chunks_by_server = worker.snapshot()
        total_bytes = sum(bytes_by_server.values())
        if latest is not None:
            self._display_progress(self._verified_bytes + total_bytes,
                                   self._verified_chunks + sum(chunks_by_server.values()))
            self._display_contributions(bytes_by_server, chunks_by_server, total_bytes)
        now = time.monotonic()
        elapsed = now - self._rate_time
        if not bytes_by_server or (elapsed < 1 and
                                    (not force_rate or bytes_by_server == self._last_bytes)):
            return
        rates = {name: (amount - self._last_bytes.get(name, 0)) / max(elapsed, .001)
                 for name, amount in bytes_by_server.items()}
        self._last_bytes = bytes_by_server
        self._rate_time = now
        total_rate = sum(rates.values())
        self._rate_samples.append((now - self._session_started, total_rate))
        self.speed_value.setText(f"{total_rate / 1_048_576:.1f} MiB/s")
        remaining = max(0, self._file_size - self._verified_bytes - total_bytes)
        self.eta_value.setText(f"{remaining / total_rate:.0f} giây" if total_rate > 0 else "—")
        for name, row in self._server_rows.items():
            if name in self._available_servers:
                self._set_cell(row, 5, f"{rates.get(name, 0) / 1_048_576:.1f} MiB/s")
            else:
                self._set_cell(row, 5, "—")

    def _download_finished(self, result: DownloadResult):
        self.state = "completed"
        self.status_label.setText("Đã lưu tệp")
        self._set_integrity("Đã xác minh", "positive")
        self._display_progress(result.bytes_written, self._total_chunks)
        self._clear_live_rates()
        self.summary.set_result(result, time.monotonic() - self._session_started,
                                list(self._rate_samples), self._failed_servers)
        self._display_contributions(result.per_server_bytes, result.per_server_chunks,
                                    sum(result.per_server_bytes.values()))
        matched = self._server_count - len(result.unused_servers)
        self.available_value.setText(f"{matched} / {self._server_count}")
        for name in self._available_servers:
            self._set_cell(self._server_rows[name], 1, "Đã kết thúc")
        self.progress_text.setToolTip(result.file_sha256)
        self.cancel_button.hide()
        self._log(f"Đã lưu: {result.output} · SHA-256 {result.file_sha256}")

    def _download_failed(self, error: Exception):
        self._poll_progress(force_rate=True)
        self._clear_live_rates()
        self.cancel_button.hide()
        part_exists = self._active_part is not None and self._active_part.exists()
        if isinstance(error, DownloadCancelled):
            self.state = "cancelled"
            self.status_label.setText("Đã dừng tải")
            self._set_integrity("Chưa xác minh")
            message = ("Dữ liệu .part được giữ lại để tiếp tục." if part_exists
                       else "Chưa có dữ liệu tạm để tiếp tục.")
            self.progress_text.setText(message)
            self._log(message)
            return
        self.state = "error"
        self.status_label.setText("Tải thất bại")
        if isinstance(error, DownloadError) and error.code == "HASH_MISMATCH":
            self._set_integrity("Không khớp", "negative")
        else:
            self._set_integrity("Chưa xác minh")
        self._log(f"Lỗi: {error}")
        part_note = f"\nDữ liệu tạm: {self._active_part}" if part_exists else ""
        self.progress_text.setText(error_advice(error) + part_note)
        self.progress_text.setToolTip(str(error))

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        text = f"{timestamp}  {message}"
        self.activity_list.insertItem(0, text)
        self.activity_note.hide()
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
                "Tệp đang tải sẽ không được công bố. Dữ liệu .part được giữ lại nếu đã có.",
                confirm_text="Dừng tải",
                cancel_text="Tải tiếp",
                is_danger=True,
            )
            if not confirmed:
                return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Đang dừng…")
        self.status_label.setText("Đang dừng phiên tải")
        self._controller.cancel()

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            if not self._close_after_work:
                confirmed = self._confirm_box(
                    "Đóng ứng dụng",
                    "Ứng dụng đang làm việc.",
                    "Dừng công việc đang chạy và đóng ứng dụng?",
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
            else:
                self._cancel_catalog()
            event.ignore()
            self.status_label.setText("Đang chờ công việc kết thúc")
        else:
            event.accept()
