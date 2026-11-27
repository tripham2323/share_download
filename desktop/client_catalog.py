"""Read the LAN file catalogue without blocking the desktop event loop."""

from __future__ import annotations

import socket
import string
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHeaderView, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from client import (
    DownloadCancelled, DownloadController, DownloadError, FileMetadata, ServerEndpoint,
    select_consistent_servers,
)
from desktop.theme import NEGATIVE, WARNING
from protocol import PROTOCOL_VERSION, ProtocolError, recv_frame, send_frame


@dataclass(frozen=True, slots=True)
class _SourceCatalogue:
    endpoint: ServerEndpoint
    files: tuple[FileMetadata, ...] = ()
    issue: str = ""
    detail: str = ""


def _plain_name(name: object) -> bool:
    return (isinstance(name, str) and bool(name) and name == name.strip()
            and name not in {".", ".."} and not any(c in name for c in ":/\\\0")
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in name))


def _list_source(endpoint: ServerEndpoint, *, connect_timeout: float, read_timeout: float,
                 control: DownloadController) -> _SourceCatalogue:
    entries: list[FileMetadata] = []
    after = None
    try:
        control.check()
        with socket.create_connection((endpoint.host, endpoint.port), timeout=connect_timeout) as sock:
            sock.settimeout(read_timeout)
            control.register(sock)
            try:
                while True:
                    control.check()
                    request = {"version": PROTOCOL_VERSION, "type": "LIST_FILES"}
                    if after is not None:
                        request["after"] = after
                    send_frame(sock, request)
                    header, _ = recv_frame(sock, max_payload=0)
                    if header["type"] == "ERROR":
                        code = header.get("code", "ERROR")
                        if after is None and code in ("INVALID_REQUEST", "UNSUPPORTED_REQUEST"):
                            return _SourceCatalogue(endpoint, issue="Không hỗ trợ danh mục")
                        raise ProtocolError(str(code))
                    files = header.get("files")
                    if (header["type"] != "FILE_LIST" or header.get("status") != "OK"
                            or not isinstance(files, list) or "next_after" not in header):
                        raise ProtocolError("Phản hồi FILE_LIST không hợp lệ")
                    previous = after
                    for entry in files:
                        if not isinstance(entry, dict):
                            raise ProtocolError("Mục tệp không hợp lệ")
                        name, size, digest = (entry.get(key) for key in ("filename", "file_size", "file_sha256"))
                        if (not _plain_name(name) or (previous is not None and name <= previous)
                                or type(size) is not int or size < 0 or not isinstance(digest, str)
                                or len(digest) != 64 or any(c not in string.hexdigits for c in digest)):
                            raise ProtocolError("Metadata tệp không hợp lệ")
                        entries.append(FileMetadata(name, size, digest.lower()))
                        previous = name
                    after = header["next_after"]
                    if after is None:
                        return _SourceCatalogue(endpoint, tuple(entries))
                    if not files or after != previous:
                        raise ProtocolError("Con trỏ danh mục không hợp lệ")
            finally:
                control.unregister(sock)
    except DownloadCancelled:
        return _SourceCatalogue(endpoint)
    except OSError as exc:
        return _SourceCatalogue(endpoint, issue="Không kết nối được", detail=str(exc))
    except (ProtocolError, ValueError, RecursionError) as exc:
        return _SourceCatalogue(endpoint, issue="Danh mục không hợp lệ", detail=str(exc))


class CatalogWorker(QObject):
    finished = Signal(object)

    def __init__(self, sources: tuple[ServerEndpoint, ...], connect_timeout: float,
                 read_timeout: float, control: DownloadController):
        super().__init__()
        self.sources = sources
        self.fetch = partial(_list_source, connect_timeout=connect_timeout,
                             read_timeout=read_timeout, control=control)
        self.control = control

    @Slot()
    def run(self) -> None:
        with ThreadPoolExecutor(max_workers=min(8, len(self.sources))) as pool:
            result = tuple(pool.map(self.fetch, self.sources))
        self.finished.emit(() if self.control.cancelled.is_set() else result)


class CatalogDialog(QDialog):
    file_chosen = Signal(str)
    refresh_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Danh mục tệp · ShareDownload")
        self.setModal(True)
        self.resize(700, 430)
        self._choices: dict[int, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel("Tệp trên máy chủ")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName("Danh sách tệp trên máy chủ")
        self.table.setHorizontalHeaderLabels(("Tên tệp", "Kích thước", "Nguồn", "Phiên bản"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.currentCellChanged.connect(self._update_selection)
        self.table.itemDoubleClicked.connect(self._choose)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._choose)
        layout.addWidget(self.table, 1)

        self.sources = QLabel()
        self.sources.setObjectName("muted")
        self.sources.setTextFormat(Qt.TextFormat.PlainText)
        self.sources.setWordWrap(True)
        layout.addWidget(self.sources)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("&Làm mới")
        self.refresh_button.clicked.connect(self.refresh_requested)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        close_button = QPushButton("Đóng (Esc)")
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        self.choose_button = QPushButton("&Chọn tệp")
        self.choose_button.setObjectName("primary")
        self.choose_button.clicked.connect(self._choose)
        actions.addWidget(self.choose_button)
        for button in (self.refresh_button, close_button, self.choose_button):
            button.setAutoDefault(False)
        layout.addLayout(actions)

    def begin(self) -> None:
        self.status.setText("Đang đọc danh mục từ các máy chủ nguồn…")
        self.sources.clear()
        self._choices.clear()
        self.table.setRowCount(0)
        self.refresh_button.setEnabled(False)
        self.choose_button.setEnabled(False)

    def end(self) -> None:
        self.refresh_button.setEnabled(True)
        self._update_selection()

    @Slot(object)
    def show_results(self, reports: tuple[_SourceCatalogue, ...]) -> None:
        if not reports:
            return  # The user closed the catalogue and cancelled its request.
        groups = defaultdict(list)
        issues = []
        details = []
        for report in reports:
            if report.issue:
                issue = f"{report.endpoint.name}: {report.issue}"
                issues.append(issue)
                details.append(f"{issue} · {report.detail}" if report.detail else issue)
            for entry in report.files:
                groups[entry.filename].append((report.endpoint, entry))

        self.table.setRowCount(len(groups))
        divergent_files = blocked_files = 0
        for row, (name, versions) in enumerate(sorted(groups.items())):
            source_names = ", ".join(source.name for source, _ in versions)
            try:
                metadata, matching_sources = select_consistent_servers(versions)
            except DownloadError:
                blocked_files += 1
                size, status = "—", f"Không khớp · {len(versions)} nguồn"
                tone = NEGATIVE
            else:
                self._choices[row] = name
                size = f"{metadata.file_size:,} B"
                divergent = len(versions) - len(matching_sources)
                if divergent:
                    divergent_files += 1
                    status = f"{len(matching_sources)}/{len(versions)} khớp · {divergent} khác"
                    tone = WARNING
                else:
                    status, tone = "Đã niêm yết", None
            detail = "\n".join(f"{source.name} · {entry.file_size:,} B · SHA-256 {entry.file_sha256}"
                               for source, entry in versions)
            for column, text in enumerate((name, size, source_names, status)):
                cell = QTableWidgetItem(text)
                cell.setToolTip(detail if column == 3 else text)
                if column == 1:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if tone and column == 3:
                    cell.setForeground(QColor(tone))
                self.table.setItem(row, column, cell)

        if groups:
            summary = f"{len(groups):,} tệp được niêm yết."
            if divergent_files:
                summary += f" {divergent_files:,} tệp có nguồn khác phiên bản nhưng vẫn có đa số khớp."
            if blocked_files:
                summary += f" {blocked_files:,} tệp không có đa số khớp nên không thể chọn."
        elif len(issues) < len(reports):
            summary = "Chưa có tệp trên các máy chủ đã trả danh mục."
        elif all(report.issue == "Không kết nối được" for report in reports):
            summary = "Không kết nối được với máy chủ nào. Bạn vẫn có thể nhập tên tệp thủ công."
        else:
            summary = "Không lấy được danh mục. Bạn vẫn có thể nhập tên tệp thủ công."
        self.status.setText(summary)
        self.sources.setText(" · ".join(issues))
        self.sources.setToolTip("\n".join(details))
        if groups and self.isVisible():
            self.table.setCurrentCell(0, 0)
            self.table.setFocus()
        self._update_selection()

    def _update_selection(self) -> None:
        self.choose_button.setEnabled(self.refresh_button.isEnabled() and self.table.currentRow() in self._choices)

    def _choose(self) -> None:
        name = self._choices.get(self.table.currentRow())
        if name is not None and self.choose_button.isEnabled():
            self.accept()
            self.file_chosen.emit(name)
