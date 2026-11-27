"""Private S1 coordination and pull-based replication for S2 and S3."""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
import re
import sqlite3
import ssl
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from stat import S_ISREG
from typing import BinaryIO

from cluster_http import (BLOCK_SIZE, REQUEST_TIMEOUT, TRANSFER_TIMEOUT, ClusterConnection,
                          ClusterHttpServer, DeadlineReader, set_deadline)
from cluster_settings import NODE_IDS, ClusterError, ClusterSettings
from file_store import MAX_FILE_SIZE, FileStore

__all__ = ["ClusterError", "ClusterRelease", "ClusterService", "ClusterSettings", "ClusterSnapshot"]

_PAGE_SIZE = 64
_MAX_JSON_BYTES = 128 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_LENGTH = re.compile(r"[0-9]{1,20}")
_CATALOG = re.compile(r"/v1/catalog(?:/([0-9a-f]{64}))?")
_RELEASE = re.compile(r"/v1/releases/([0-9a-f]{64})(/ack)?")


@dataclass(frozen=True, slots=True)
class ClusterRelease:
    release_id: str
    filename: str
    file_size: int
    file_sha256: str
    acknowledged: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClusterSnapshot:
    releases: tuple[ClusterRelease, ...]
    conflicts: dict[str, str]


class _HttpProblem(ClusterError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _ReplicaConflict(ClusterError):
    pass


def _filename_bytes(name: str) -> bytes:
    # FileStore is responsible for validating platform-safe names. The wire
    # also bounds their encoded size before constructing an HTTP request.
    if not isinstance(name, str) or not name or len(name) > 180:
        raise ValueError("Tên tệp không hợp lệ hoặc dài quá 180 ký tự.")
    return name.encode("utf-8")


def _identity(name: str, size: int, digest: str) -> str:
    return sha256(json.dumps([name, size, digest], separators=(",", ":")).encode("ascii")).hexdigest()


def _release_from_json(data: object) -> ClusterRelease:
    try:
        name, size, digest = data["filename"], data["file_size"], data["file_sha256"]
        _filename_bytes(name)
        if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE or not _SHA.fullmatch(digest):
            raise ValueError
        release_id, acks = data["release_id"], data["acknowledged"]
        if release_id != _identity(name, size, digest) or not isinstance(acks, list):
            raise ValueError
        if "S1" not in acks or acks != [node for node in NODE_IDS if node in acks]:
            raise ValueError
        return ClusterRelease(release_id, name, size, digest, tuple(acks))
    except (KeyError, TypeError, ValueError) as error:
        raise ClusterError("S1 trả về thông tin bản phát hành không hợp lệ.") from error


def _from_row(row) -> ClusterRelease:
    release_id, name, size, digest, mask = row
    return ClusterRelease(release_id, name, size, digest,
                          tuple(node for index, node in enumerate(NODE_IDS) if mask & (1 << index)))


class _Ledger:
    """A bit per node persists ACKs atomically with an immutable release ID."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS releases (
                id TEXT PRIMARY KEY NOT NULL, name TEXT UNIQUE NOT NULL,
                size INTEGER NOT NULL, digest TEXT NOT NULL, acks INTEGER NOT NULL
            )""")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=REQUEST_TIMEOUT)) as db, db:
            yield db

    def get(self, release_id: str) -> ClusterRelease | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
            return _from_row(row) if row else None

    def commit(self, release_id: str, name: str, size: int, digest: str) -> ClusterRelease:
        with self.connect() as db:
            db.execute("INSERT INTO releases VALUES (?, ?, ?, ?, 1) ON CONFLICT(id) DO NOTHING",
                       (release_id, name, size, digest))
            return _from_row(db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone())

    def acknowledge(self, release_id: str, node: str) -> ClusterRelease:
        bit = 1 << NODE_IDS.index(node)
        with self.connect() as db:
            updated = db.execute("UPDATE releases SET acks=acks | ? WHERE id=?", (bit, release_id))
            if not updated.rowcount:
                raise _HttpProblem(404, "Không tìm thấy bản phát hành đã chốt.")
            return _from_row(db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone())

    def page(self, after: str) -> tuple[list[ClusterRelease], str | None]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM releases WHERE id>? ORDER BY id LIMIT ?",
                              (after, _PAGE_SIZE + 1)).fetchall()
        more = len(rows) > _PAGE_SIZE
        entries = [_from_row(row) for row in rows[:_PAGE_SIZE]]
        return entries, entries[-1].release_id if more else None


class ClusterService:
    """No background application work: call refresh periodically on the owning worker."""

    def __init__(self, settings: ClusterSettings, store: FileStore, state_dir: str | Path):
        self.settings = settings
        self.store = store
        self.state_dir = Path(state_dir)
        self._lock = threading.Lock()
        self._started = False
        self._http: ClusterHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._ledger: _Ledger | None = None
        self._client_tls: ssl.SSLContext | None = None
        self._pin: bytes | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            try:
                self._pin = ssl.PEM_cert_to_DER_cert(self.settings.certificate_pem)
                if self.settings.is_coordinator:
                    self.state_dir.mkdir(parents=True, exist_ok=True)
                    # Reprovisioning a new cluster must not expose an old cluster's catalog.
                    self._ledger = _Ledger(self.state_dir / f"releases-{sha256(self._pin).hexdigest()}.sqlite3")
                    tls = self.settings._server_context(self.state_dir)
                    server = ClusterHttpServer((self.settings.bind_host, self.settings.port),
                                               _ClusterHandler, tls, self)
                    thread = threading.Thread(target=server.serve_forever,
                                              kwargs={"poll_interval": 0.1}, name="cluster-S1", daemon=True)
                    try:
                        thread.start()
                    except RuntimeError:
                        server.server_close()
                        raise
                    self._http, self._thread = server, thread
                else:
                    self._client_tls = self.settings._client_context()
                self._started = True
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                raise ClusterError(f"Không thể khởi động cụm {self.settings.node_id}: {error}") from error

    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            if self._http is not None:
                self._http.interrupt()
                self._http.shutdown()
                self._http.server_close()
                self._thread.join()
                self._http, self._thread = None, None

    def _require_started(self) -> None:
        if not self._started:
            raise ClusterError("Dịch vụ cụm chưa khởi động.")

    def publish(self, path: str | Path, *, name: str | None = None) -> ClusterRelease:
        """Publish a normal local file without changing its source name or contents."""
        self._require_started()
        path = Path(path)
        filename = path.name if name is None else name
        try:
            if not path.is_file():
                raise ValueError("Hãy chọn một tệp bình thường để phát hành.")
            with path.open("rb") as source:
                info = os.fstat(source.fileno())
                if not S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_FILE_SIZE:
                    raise ValueError("Tệp phải là tệp bình thường và không vượt quá 4 GiB.")
                if self.settings.is_coordinator:
                    stored = self.store.put(filename, source)
                    release_id = _identity(stored.name, stored.size, stored.digest)
                    return self._ledger.commit(release_id, stored.name, stored.size, stored.digest)
                wire_name = base64.urlsafe_b64encode(_filename_bytes(filename)).decode("ascii")
                size = info.st_size
                digest = self._inspect(source, size)
                release_id = _identity(filename, size, digest)
                already_stored = self._replica_present(filename, size, digest)
                result = self._json("PUT", f"/v1/releases/{release_id}", source, headers={
                    "Content-Length": str(size), "X-File-Name": wire_name, "X-File-Sha256": digest,
                })
                release = _release_from_json(result)
                if release.release_id != release_id:
                    raise ClusterError("S1 xác nhận mã bản phát hành khác với tệp được gửi.")
                try:
                    if not already_stored:
                        source.seek(0)
                        self.store.put(filename, source, digest=digest)
                    return self._acknowledge(release)
                except (OSError, ValueError, sqlite3.Error, ClusterError) as error:
                    raise ClusterError(f"S1 đã chốt {filename}, nhưng bản sao cục bộ chưa được xác nhận: {error}") from error
        except (OSError, ValueError, sqlite3.Error) as error:
            raise ClusterError(f"Không thể phát hành {filename}: {error}") from error

    @staticmethod
    def _inspect(source: BinaryIO, size: int) -> str:
        digest = sha256()
        remaining = size
        while remaining:
            chunk = source.read(min(BLOCK_SIZE, remaining))
            if not chunk:
                raise ValueError("Tệp nguồn đã bị thay đổi trong khi đọc.")
            digest.update(chunk)
            remaining -= len(chunk)
        if source.read(1):
            raise ValueError("Tệp nguồn đã bị thay đổi trong khi đọc.")
        source.seek(0)
        return digest.hexdigest()

    def _commit_upload(self, release_id: str, name: str, size: int, digest: str, source: BinaryIO) -> ClusterRelease:
        # The store commits verified bytes first. A retry safely adopts the same
        # immutable file if interrupted before the coordinator's ledger commit.
        self.store.put(name, source, size=size, digest=digest)
        return self._ledger.commit(release_id, name, size, digest)

    def refresh(self) -> ClusterSnapshot:
        """Return committed releases and, on followers, install and ACK missing copies."""
        self._require_started()
        releases, conflicts = [], {}
        try:
            for release in self._catalog():
                if not self.settings.is_coordinator:
                    try:
                        if not self._replica_present(release.filename, release.file_size, release.file_sha256):
                            self._pull(release)
                        if self.settings.node_id not in release.acknowledged:
                            release = self._acknowledge(release)
                    except _ReplicaConflict as error:
                        conflicts[release.filename] = str(error)
                releases.append(release)
        except (OSError, sqlite3.Error) as error:
            raise ClusterError(f"Không thể cập nhật danh mục cụm: {error}") from error
        return ClusterSnapshot(tuple(releases), conflicts)

    def _catalog(self):
        after = ""
        while True:
            if self.settings.is_coordinator:
                page, following = self._ledger.page(after)
            else:
                data = self._json("GET", f"/v1/catalog/{after}" if after else "/v1/catalog")
                if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
                    raise ClusterError("S1 trả về trang danh mục không hợp lệ.")
                page = [_release_from_json(item) for item in data["releases"]]
                following = data.get("next_after")
                if len(page) > _PAGE_SIZE:
                    raise ClusterError("S1 trả về quá nhiều bản phát hành trong một trang.")
                last = after
                for item in page:
                    if item.release_id <= last:
                        raise ClusterError("S1 trả về thứ tự danh mục không hợp lệ.")
                    last = item.release_id
                if following is not None and (not page or following != page[-1].release_id):
                    raise ClusterError("S1 trả về vị trí danh mục không hợp lệ.")
            yield from page
            if following is None:
                break
            after = following

    def _replica_present(self, name: str, size: int, digest: str) -> bool:
        current = self.store.get(name)
        if current is None:
            return False
        if (current.size, current.digest) != (size, digest):
            raise _ReplicaConflict(f"Tệp {name} đã có ở nút này nhưng khác dung lượng hoặc SHA-256.")
        return current.path.is_file()

    def _pull(self, release: ClusterRelease) -> None:
        with self._request("GET", f"/v1/releases/{release.release_id}") as response:
            if _response_length(response, MAX_FILE_SIZE) != release.file_size:
                raise ClusterError(f"Dung lượng S1 trả về không khớp: {release.filename}.")
            try:
                self.store.put(release.filename, response, size=release.file_size, digest=release.file_sha256)
            except ValueError as error:
                raise _ReplicaConflict(str(error)) from error

    def _acknowledge(self, release: ClusterRelease) -> ClusterRelease:
        if self.settings.node_id in release.acknowledged:
            return release
        updated = _release_from_json(self._json("POST", f"/v1/releases/{release.release_id}/ack"))
        if updated.release_id != release.release_id or self.settings.node_id not in updated.acknowledged:
            raise ClusterError("S1 không xác nhận ACK cho bản phát hành của nút này.")
        return updated

    def _json(self, method: str, path: str, source: BinaryIO | None = None,
              headers: dict[str, str] | None = None):
        with self._request(method, path, source, headers) as response:
            length = _response_length(response, _MAX_JSON_BYTES)
            payload = response.read(length)
            if len(payload) != length:
                raise ClusterError("S1 gửi thiếu dữ liệu JSON.")
            try:
                return json.loads(payload)
            except (UnicodeError, ValueError, RecursionError) as error:
                raise ClusterError("S1 trả về dữ liệu JSON không hợp lệ.") from error

    @contextmanager
    def _request(self, method: str, path: str, source: BinaryIO | None = None,
                 headers: dict[str, str] | None = None):
        connection = ClusterConnection(self.settings.host, self.settings.port,
                                       context=self._client_tls, timeout=REQUEST_TIMEOUT)
        try:
            connection.connect()
            if connection.sock.getpeercert(binary_form=True) != self._pin:
                raise ssl.SSLCertVerificationError("Chứng chỉ TLS của S1 không khớp hồ sơ riêng.")
            connection.putrequest(method, path)
            connection.putheader("Authorization", f"Bearer {self.settings.token}")
            connection.putheader("Connection", "close")
            for key, value in (headers or {}).items():
                connection.putheader(key, value)
            connection.endheaders()
            if source is not None:
                _send_file(connection.sock, source, int(headers["Content-Length"]))
            with connection.getresponse() as response:
                if response.status != 200:
                    detail = response.reason
                    length = _response_length(response, _MAX_JSON_BYTES, required=False)
                    if length:
                        try:
                            detail = json.loads(response.read(length)).get("error") or detail
                        except (AttributeError, UnicodeError, ValueError, RecursionError):
                            pass
                    raise ClusterError(f"S1 từ chối yêu cầu (HTTP {response.status}): {detail}")
                yield response
        except (OSError, http.client.HTTPException) as error:
            raise ClusterError(f"Không thể trao đổi với S1 ({self.settings.host}:{self.settings.port}): {error}") from error
        finally:
            connection.close()


def _response_length(response, limit: int, *, required=True) -> int:
    lengths = response.headers.get_all("Content-Length", [])
    if (response.headers.get("Transfer-Encoding") is not None or len(lengths) > 1
            or (lengths and (not _LENGTH.fullmatch(lengths[0]) or int(lengths[0]) > limit))
            or (required and not lengths)):
        raise ClusterError("S1 trả về độ dài nội dung không hợp lệ.")
    return int(lengths[0]) if lengths else 0


def _send_file(sock, source: BinaryIO, length: int) -> None:
    deadline = time.monotonic() + TRANSFER_TIMEOUT
    while length:
        chunk = source.read(min(BLOCK_SIZE, length))
        if not chunk:
            raise OSError("Tệp nguồn kết thúc trước dung lượng đã khai báo.")
        set_deadline(sock, deadline)
        sock.sendall(chunk)
        length -= len(chunk)


class _ClusterHandler(BaseHTTPRequestHandler):
    """Exactly one authenticated request per TLS connection."""

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self._reader = DeadlineReader(self.connection)
        self.rfile = io.BufferedReader(self._reader)

    def log_message(self, format, *args) -> None:
        # Never log private requests, credentials, untrusted paths or headers.
        pass

    def _header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise _HttpProblem(400, f"Tiêu đề {name} bị lặp.")
        return values[0] if values else None

    def _authenticate(self) -> str:
        scheme, separator, token = (self._header("Authorization") or "").partition(" ")
        if scheme == "Bearer" and separator and token.isascii():
            for node, expected in zip(NODE_IDS[1:], self.server.service.settings.follower_tokens):
                if compare_digest(token, expected):
                    return node
        raise _HttpProblem(401, "Thông tin xác thực nút không hợp lệ.")

    def _body_length(self, *, upload: bool) -> int:
        if self._header("Transfer-Encoding") is not None or self._header("Expect") is not None:
            raise _HttpProblem(400, "Yêu cầu phải khai báo dung lượng cố định.")
        header = self._header("Content-Length")
        if header is None:
            if upload:
                raise _HttpProblem(411, "Thiếu dung lượng tệp.")
            return 0
        if not _LENGTH.fullmatch(header):
            raise _HttpProblem(400, "Dung lượng yêu cầu không hợp lệ.")
        length = int(header)
        if length > (MAX_FILE_SIZE if upload else 0):
            raise _HttpProblem(413, "Nội dung yêu cầu vượt giới hạn.")
        return length

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        deadline = time.monotonic() + REQUEST_TIMEOUT
        set_deadline(self.connection, deadline)
        self.end_headers()
        set_deadline(self.connection, deadline)
        self.wfile.write(body)

    def _route(self) -> None:
        self.close_connection = True
        try:
            node = self._authenticate()
            length = self._body_length(upload=self.command == "PUT")
            catalog = _CATALOG.fullmatch(self.path)
            release = _RELEASE.fullmatch(self.path)
            ledger = self.server.service._ledger
            if self.command == "GET" and catalog:
                entries, after = ledger.page(catalog[1] or "")
                self._send_json(200, {"releases": [asdict(item) for item in entries], "next_after": after})
            elif self.command == "GET" and release and not release[2]:
                self._send_replica(release[1])
            elif self.command == "PUT" and release and not release[2]:
                self._upload(release[1], length)
            elif self.command == "POST" and release and release[2]:
                self._send_json(200, asdict(ledger.acknowledge(release[1], node)))
            else:
                raise _HttpProblem(404, "Đường dẫn cụm không tồn tại.")
        except _HttpProblem as error:
            self._send_json(error.status, {"error": str(error)})
        except sqlite3.Error:
            self._send_json(503, {"error": "Không thể cập nhật dữ liệu cụm trên S1."})

    do_GET = do_POST = do_PUT = _route

    def _upload(self, release_id: str, size: int) -> None:
        try:
            encoded = self._header("X-File-Name") or ""
            name = base64.b64decode(encoded, altchars=b"-_", validate=True).decode("utf-8")
            _filename_bytes(name)
        except ValueError as error:
            raise _HttpProblem(400, "Tên tệp tải lên không hợp lệ.") from error
        digest = self._header("X-File-Sha256") or ""
        if not _SHA.fullmatch(digest) or release_id != _identity(name, size, digest):
            raise _HttpProblem(400, "Mã bản phát hành hoặc SHA-256 không hợp lệ.")
        self._reader.start_body()
        try:
            release = self.server.service._commit_upload(release_id, name, size, digest, self.rfile)
        except ValueError as error:
            raise _HttpProblem(422, str(error)) from error
        except TimeoutError as error:
            raise _HttpProblem(408, "Hết thời gian tải tệp lên S1.") from error
        except OSError as error:
            raise _HttpProblem(503, "Không thể nhận và lưu tệp trên S1.") from error
        self._send_json(200, asdict(release))

    def _send_replica(self, release_id: str) -> None:
        service = self.server.service
        release = service._ledger.get(release_id)
        if release is None:
            raise _HttpProblem(404, "Không tìm thấy bản phát hành đã chốt.")
        entry = service.store.get(release.filename)
        if entry is None or (entry.size, entry.digest) != (release.file_size, release.file_sha256):
            raise _HttpProblem(503, "Bản sao của S1 không khớp với bản phát hành.")
        try:
            source = entry.path.open("rb")
        except OSError as error:
            raise _HttpProblem(503, "Không thể mở bản sao trên S1.") from error
        with source:
            if os.fstat(source.fileno()).st_size != entry.size:
                raise _HttpProblem(503, "Dung lượng bản sao của S1 không khớp với bản phát hành.")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(entry.size))
            self.send_header("Connection", "close")
            self.connection.settimeout(REQUEST_TIMEOUT)
            self.end_headers()
            _send_file(self.connection, source, entry.size)
