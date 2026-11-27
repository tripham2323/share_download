"""Per-node, immutable file catalogue; incomplete transfers are never readable."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import BinaryIO

MAX_FILE_SIZE = 4 * 1024**3
_BLOCK_SIZE = 1024**2
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'{prefix}{n}' for prefix in ('COM', 'LPT') for n in '123456789¹²³')}


@dataclass(frozen=True, slots=True)
class StoredFile:
    name: str
    size: int
    digest: str
    path: Path


def _name_key(name: str) -> str:
    if (not isinstance(name, str) or not name.strip() or name != name.strip()
            or len(name) > 180 or name.endswith('.') or _FORBIDDEN.search(name)
            or name.split('.', 1)[0].rstrip().upper() in _RESERVED):
        raise ValueError('Tên tệp không hợp lệ hoặc không dùng được trên Windows.')
    # Surrogate code points are not valid names on the wire.
    name.encode('utf-8')
    return name.casefold()


class FileStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.data = self.root / 'objects'
        self.data.mkdir(parents=True, exist_ok=True)
        self._db = self.root / 'files.sqlite3'
        with closing(sqlite3.connect(self._db)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS files ('
                       'name_key TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, '
                       'size INTEGER NOT NULL, digest TEXT NOT NULL)')

    def _entry(self, row: tuple[str, int, str]) -> StoredFile:
        name, size, digest = row
        return StoredFile(name, size, digest, self.data / digest)

    def get(self, name: str) -> StoredFile | None:
        with closing(sqlite3.connect(self._db)) as db:
            row = db.execute('SELECT name,size,digest FROM files WHERE name=?', (name,)).fetchone()
        return self._entry(row) if row else None

    def list(self) -> list[StoredFile]:
        with closing(sqlite3.connect(self._db)) as db:
            return [self._entry(row) for row in db.execute('SELECT name,size,digest FROM files ORDER BY name')]

    def page(self, after: str | None, count: int) -> list[StoredFile]:
        with closing(sqlite3.connect(self._db)) as db:
            rows = db.execute('SELECT name,size,digest FROM files WHERE name>? ORDER BY name LIMIT ?',
                              (after or '', count))
            return [self._entry(row) for row in rows]

    def put(self, name: str, stream: BinaryIO, *, size: int | None = None,
            digest: str | None = None) -> StoredFile:
        key = _name_key(name)
        if size is not None and (type(size) is not int or not 0 <= size <= MAX_FILE_SIZE):
            raise ValueError('Dung lượng tệp vượt giới hạn 4 GiB hoặc không hợp lệ.')
        checksum = sha256()
        total = 0
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.data, suffix='.part', delete=False) as output:
                temporary = Path(output.name)
                while size is None or total < size:
                    block = stream.read(_BLOCK_SIZE if size is None else min(_BLOCK_SIZE, size - total))
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_FILE_SIZE:
                        raise ValueError('Dung lượng tệp vượt giới hạn 4 GiB.')
                    checksum.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            actual = checksum.hexdigest()
            if (size is not None and size != total) or (digest is not None and digest != actual):
                raise ValueError('Dung lượng hoặc SHA-256 của bản sao không khớp.')
            with closing(sqlite3.connect(self._db)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT name,size,digest FROM files WHERE name_key=?', (key,)).fetchone()
                if row and row != (name, total, actual):
                    raise ValueError(f'Tên tệp đã tồn tại: {row[0]}. Hãy chọn tên khác.')
                destination = self.data / actual
                if not destination.is_file():
                    os.replace(temporary, destination)
                if not row:
                    db.execute('INSERT INTO files VALUES (?,?,?,?)', (key, name, total, actual))
            return StoredFile(name, total, actual, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
