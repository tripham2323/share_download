"""File hashing, chunk ranges, and safe temporary output handling."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

from protocol import MAX_CHUNK_SIZE
from scheduler import Chunk


def build_chunks(file_size: int, chunk_size: int) -> list[Chunk]:
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        raise ValueError("file_size must be a non-negative integer")
    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 1 <= chunk_size <= MAX_CHUNK_SIZE
    ):
        raise ValueError("chunk_size must be between 1 and 1048576")

    return [
        Chunk(chunk_id, offset, min(chunk_size, file_size - offset))
        for chunk_id, offset in enumerate(range(0, file_size, chunk_size))
    ]


def sha256_file(path: str | Path, block_size: int = 1_048_576) -> str:
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    digest = sha256()
    with Path(path).open("rb") as source:
        while block := source.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def prepare_part_file(output: str | Path, size: int) -> Path:
    if size < 0:
        raise ValueError("size must be non-negative")
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(output_path.name + ".part")
    with part_path.open("wb") as target:
        target.truncate(size)
    return part_path


def publish_part_file(part_path: str | Path, output: str | Path) -> None:
    os.replace(Path(part_path), Path(output))
