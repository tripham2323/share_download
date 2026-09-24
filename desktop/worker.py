"""Run TCP operations outside the Qt GUI thread."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import threading
from checkpoint import CheckpointIdentity, ResumeConflict, load_checkpoint, state_path_for
from PySide6.QtCore import QObject, Signal, Slot

from client import (
    ClientConfig, DownloadController, DownloadEvent, DownloadResult, download,
    query_metadata, select_consistent_servers,
)
from file_utils import build_chunks


@dataclass(frozen=True, slots=True)
class ResumePreview:
    status: str
    verified: int = 0
    total: int = 0
    reason: str = ""



class MetadataWorker(QObject):
    status = Signal(str, str)
    finished = Signal(str, object)
    resume_state = Signal(object)

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
            part = self.config.output.with_name(self.config.output.name + ".part")
            state = state_path_for(part)
            if part.exists() or state.exists():
                chunks = build_chunks(metadata.file_size, self.config.chunk_size)
                identity = CheckpointIdentity(metadata.filename, metadata.file_size,
                                              metadata.file_sha256, self.config.chunk_size)
                try:
                    verified = load_checkpoint(part, identity, chunks)
                except ResumeConflict as exc:
                    reason = {
                        "MISSING_FILES": "Thiếu tệp .part hoặc checkpoint",
                        "SIZE_MISMATCH": "Kích thước tệp .part không khớp metadata",
                        "INVALID_JSON": "Checkpoint bị hỏng hoặc không đọc được",
                        "UNSUPPORTED_VERSION": "Phiên bản checkpoint không được hỗ trợ",
                        "IDENTITY_MISMATCH": "Tệp nguồn hoặc kích thước chunk đã thay đổi",
                        "INVALID_CHUNKS": "Danh sách chunk trong checkpoint không hợp lệ",
                        "INVALID_ENTRY": "Một chunk trong checkpoint không hợp lệ",
                    }.get(exc.code, "Checkpoint không hợp lệ")
                    self.resume_state.emit(ResumePreview("conflict", reason=reason))
                else:
                    self.resume_state.emit(ResumePreview("valid", len(verified), len(chunks)))
            else:
                self.resume_state.emit(ResumePreview("none"))
            self.finished.emit(
                f"{len(selected)}/{len(self.config.servers)} máy chủ phù hợp · "
                f"{metadata.file_size:,} byte · SHA-256 {metadata.file_sha256}", None
            )
        except Exception as exc:
            part = self.config.output.with_name(self.config.output.name + ".part")
            if part.exists() or state_path_for(part).exists():
                self.resume_state.emit(ResumePreview("unavailable", reason=str(exc)))
            self.finished.emit(f"Không thể chọn nguồn tải: {exc}", exc)


class DownloadWorker(QObject):
    status = Signal(object)
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, config: ClientConfig, controller: DownloadController,
                 resume: bool = False):
        super().__init__()
        self.config = config
        self.controller = controller
        self.resume = resume
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
                                        controller=self.controller, resume=self.resume,
                                        save_checkpoint=True))
        except Exception as exc:
            self.failed.emit(exc)
