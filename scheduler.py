"""Thread-safe chunk scheduling for multi-source downloads."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Lock


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: int
    offset: int
    length: int

    def __post_init__(self) -> None:
        if self.chunk_id < 0 or self.offset < 0 or self.length <= 0:
            raise ValueError("chunk fields must be non-negative and length positive")


class ChunkScheduler:
    """Own chunk state transitions and distribute pending work safely."""

    def __init__(self, chunks: list[Chunk], completed_ids: frozenset[int] = frozenset()):
        identifiers = [chunk.chunk_id for chunk in chunks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("chunk identifiers must be unique")
        if not completed_ids.issubset(identifiers):
            raise ValueError("completed chunk identifiers must be known")

        self._queue: Queue[Chunk] = Queue()
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self._state = {chunk.chunk_id: ("COMPLETED" if chunk.chunk_id in completed_ids else "PENDING")
                       for chunk in chunks}
        self._lock = Lock()
        for chunk in chunks:
            if chunk.chunk_id not in completed_ids:
                self._queue.put(chunk)

    @property
    def total_count(self) -> int:
        return len(self._state)

    def acquire(self, timeout: float = 0.2) -> Chunk | None:
        try:
            chunk = self._queue.get(timeout=timeout)
        except Empty:
            return None
        with self._lock:
            if self._state[chunk.chunk_id] != "PENDING":
                raise RuntimeError("chunk is not pending")
            self._state[chunk.chunk_id] = "IN_PROGRESS"
        return chunk

    def complete(self, chunk: Chunk) -> None:
        self._require_known(chunk)
        with self._lock:
            if self._state[chunk.chunk_id] != "IN_PROGRESS":
                raise RuntimeError("chunk is not in progress")
            self._state[chunk.chunk_id] = "COMPLETED"

    def retry(self, chunk: Chunk) -> None:
        self._require_known(chunk)
        with self._lock:
            if self._state[chunk.chunk_id] != "IN_PROGRESS":
                raise RuntimeError("chunk is not in progress")
            self._state[chunk.chunk_id] = "PENDING"
        self._queue.put(chunk)

    def all_completed(self) -> bool:
        with self._lock:
            return all(state == "COMPLETED" for state in self._state.values())

    def has_unfinished(self) -> bool:
        return not self.all_completed()

    def completed_count(self) -> int:
        with self._lock:
            return sum(state == "COMPLETED" for state in self._state.values())

    def in_progress_count(self) -> int:
        with self._lock:
            return sum(state == "IN_PROGRESS" for state in self._state.values())

    def _require_known(self, chunk: Chunk) -> None:
        if self._chunks.get(chunk.chunk_id) != chunk:
            raise RuntimeError("unknown chunk")
