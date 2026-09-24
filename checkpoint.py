"""Durable per-chunk checkpoints; local bytes are rehashed before reuse."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import BinaryIO

from scheduler import Chunk


class ResumeConflict(ValueError):
    """Saved data cannot be matched safely to the current source."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CheckpointIdentity:
    filename: str
    file_size: int
    file_sha256: str
    chunk_size: int


def state_path_for(part_path: Path) -> Path:
    return Path(str(part_path) + ".state.json")


def load_checkpoint(part_path: Path, identity: CheckpointIdentity,
                    chunks: list[Chunk]) -> dict[int, str]:
    state_path = state_path_for(part_path)
    if not part_path.exists() and not state_path.exists():
        return {}
    if not part_path.is_file() or not state_path.is_file():
        raise ResumeConflict("Missing partial file or checkpoint; keep the old data or start over",
                             code="MISSING_FILES")
    if part_path.stat().st_size != identity.file_size:
        raise ResumeConflict("Partial file size differs from server metadata", code="SIZE_MISMATCH")
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResumeConflict("Checkpoint cannot be read or is not valid JSON",
                             code="INVALID_JSON") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ResumeConflict("Unsupported checkpoint version", code="UNSUPPORTED_VERSION")
    expected = {"filename": identity.filename, "file_size": identity.file_size,
                "file_sha256": identity.file_sha256, "chunk_size": identity.chunk_size}
    if any(raw.get(field) != value for field, value in expected.items()):
        raise ResumeConflict("Server file version or chunk size changed", code="IDENTITY_MISMATCH")
    saved = raw.get("completed")
    if not isinstance(saved, dict):
        raise ResumeConflict("Checkpoint chunk list is invalid", code="INVALID_CHUNKS")
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    verified = {}
    with part_path.open("rb") as target:
        for chunk_id, digest in saved.items():
            if (not isinstance(chunk_id, str) or not chunk_id.isdecimal()
                    or int(chunk_id) not in by_id or not isinstance(digest, str)
                    or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise ResumeConflict("Checkpoint contains an invalid chunk entry", code="INVALID_ENTRY")
            chunk = by_id[int(chunk_id)]
            target.seek(chunk.offset)
            data = target.read(chunk.length)
            if len(data) == chunk.length and sha256(data).hexdigest() == digest:
                verified[chunk.chunk_id] = digest
    return verified


class CheckpointStore:
    def __init__(self, part_path: Path, identity: CheckpointIdentity,
                 verified: dict[int, str] | None = None):
        self.identity = identity
        self.state_path = state_path_for(part_path)
        self.temp_path = Path(str(self.state_path) + ".tmp")
        self.completed = dict(verified or {})
        self.pending: dict[int, str] = {}

    def record(self, chunk: Chunk, digest: str) -> None:
        self.pending[chunk.chunk_id] = digest

    def flush(self, target: BinaryIO) -> None:
        if not self.pending:
            return
        target.flush()
        os.fsync(target.fileno())
        updated = {**self.completed, **self.pending}
        payload = {"version": 1, "filename": self.identity.filename,
                   "file_size": self.identity.file_size,
                   "file_sha256": self.identity.file_sha256,
                   "chunk_size": self.identity.chunk_size,
                   "completed": {str(key): value for key, value in updated.items()}}
        with self.temp_path.open("w", encoding="utf-8") as sidecar:
            json.dump(payload, sidecar, sort_keys=True)
            sidecar.flush()
            os.fsync(sidecar.fileno())
        os.replace(self.temp_path, self.state_path)
        self.completed = updated
        self.pending.clear()
