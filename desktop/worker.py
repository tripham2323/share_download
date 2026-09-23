"""Run TCP operations outside the Qt GUI thread."""

from __future__ import annotations

from collections import defaultdict
import threading
from PySide6.QtCore import QObject, Signal, Slot

from client import (
    ClientConfig, DownloadController, DownloadEvent, DownloadResult, download,
    query_metadata, select_consistent_servers,
)


class MetadataWorker(QObject):
    status = Signal(str, str)
    finished = Signal(str)

    def __init__(self, config: ClientConfig):
        super().__init__()
        self.config = config

    @Slot()
    def run(self):
        responses = []
        for endpoint in self.config.servers:
            try:
                responses.append(query_metadata(endpoint, self.config))
                self.status.emit(endpoint.name, "Đã kết nối")
            except Exception as exc:
                self.status.emit(endpoint.name, f"Không phản hồi: {exc}")
        try:
            metadata, selected = select_consistent_servers(responses)
            for endpoint, _ in responses:
                if endpoint not in selected:
                    self.status.emit(endpoint.name, "Khác phiên bản tệp")
            self.finished.emit(
                f"{len(selected)}/{len(self.config.servers)} máy chủ phù hợp · "
                f"{metadata.file_size:,} byte · SHA-256 {metadata.file_sha256}"
            )
        except Exception as exc:
            self.finished.emit(f"Không thể chọn nguồn tải: {exc}")


class DownloadWorker(QObject):
    status = Signal(object)
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, config: ClientConfig, controller: DownloadController):
        super().__init__()
        self.config = config
        self.controller = controller
        self._lock = threading.Lock()
        self._latest: DownloadEvent | None = None
        self._bytes: dict[str, int] = defaultdict(int)
        self._chunks: dict[str, int] = defaultdict(int)

    def _event(self, event: DownloadEvent) -> None:
        if event.kind == "chunk":
            with self._lock:
                self._latest = event
                self._bytes[event.server or ""] += event.bytes_delta
                self._chunks[event.server or ""] += 1
        else:
            self.status.emit(event)

    def snapshot(self) -> tuple[DownloadEvent | None, dict[str, int], dict[str, int]]:
        with self._lock:
            latest = self._latest
            self._latest = None
            return latest, dict(self._bytes), dict(self._chunks)

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(download(self.config, event_callback=self._event,
                                        controller=self.controller))
        except Exception as exc:
            self.failed.emit(exc)
