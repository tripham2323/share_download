# Multi-Source File Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xây dựng hệ thống Python để C1 tải `config.dat` đồng thời từ S1, S2 và S3 qua TCP, phân phối chunk động và xác minh SHA-256.

**Architecture:** Ba server chạy cùng một chương trình và phục vụ metadata hoặc vùng byte của tệp qua giao thức frame TCP. Client kiểm tra metadata, tạo hàng đợi chunk dùng chung và chạy một worker cho mỗi server; server nhanh tự nhận nhiều chunk hơn. Dữ liệu được ghi vào tệp `.part`, kiểm tra SHA-256 rồi mới đổi tên thành tệp đích.

**Tech Stack:** Python 3.11+, standard library (`socket`, `threading`, `queue`, `hashlib`, `json`, `struct`, `argparse`, `unittest`), Git/GitHub.

## Global Constraints

- Chạy trên Windows và Linux bằng Python 3.11 trở lên.
- Mã chạy thực tế chỉ dùng Python standard library; không thêm dependency runtime.
- Giao thức dùng TCP, JSON header UTF-8 và binary payload.
- Header có tiền tố độ dài unsigned 4 byte big-endian; header tối đa 65,536 byte.
- Chunk mặc định 262,144 byte và không vượt quá 1,048,576 byte.
- TCP xử lý mất gói; tầng ứng dụng xử lý framing, timeout, server disconnect, chunk scheduling và hash.
- File đầu ra chỉ được công bố sau khi SHA-256 toàn tệp hợp lệ.
- Server chỉ phục vụ đúng file được truyền qua `--file`.
- Mọi commit được push lên `origin/main` sau khi task tương ứng vượt qua kiểm thử.

---

## File Map

- `protocol.py`: đóng/mở frame, đọc chính xác byte, lỗi giao thức và mã thông điệp.
- `file_utils.py`: SHA-256 streaming, tạo range chunk, chuẩn bị/hoàn tất file tạm.
- `scheduler.py`: mô hình chunk và máy trạng thái thread-safe.
- `server.py`: TCP listener, metadata, range validation và gửi chunk.
- `client.py`: đọc cấu hình, chọn nhóm metadata, worker tải song song, tiến độ và CLI.
- `client_config.example.json`: cấu hình localhost có thể đổi sang IP VMware/máy thật.
- `tests/test_protocol.py`: framing và dữ liệu TCP bị phân mảnh.
- `tests/test_file_utils.py`: biên chia chunk và hash.
- `tests/test_scheduler.py`: cấp phát, hoàn thành và trả lại chunk.
- `tests/test_server.py`: metadata, chunk và lỗi range.
- `tests/test_client.py`: chọn metadata, tải đa nguồn và phục hồi mất server.

---

### Task 1: TCP Framing Protocol

**Files:**
- Create: `protocol.py`
- Create: `tests/__init__.py`
- Create: `tests/test_protocol.py`

**Interfaces:**
- Produces: `ProtocolError`, `recv_exact(sock, size)`, `send_frame(sock, header, payload=b"")`, `recv_frame(sock, max_payload=MAX_CHUNK_SIZE)`.
- Frame headers always contain `version`, `type`, and generated `payload_length`.

- [ ] **Step 1: Write failing framing tests**

```python
# tests/test_protocol.py
import socket
import unittest

from protocol import ProtocolError, recv_frame, send_frame


class ProtocolTests(unittest.TestCase):
    def test_round_trip_header_and_binary_payload(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        payload = b"abc" * 4096
        send_frame(left, {"version": 1, "type": "CHUNK_DATA", "chunk_id": 3}, payload)
        header, received = recv_frame(right)
        self.assertEqual(3, header["chunk_id"])
        self.assertEqual(len(payload), header["payload_length"])
        self.assertEqual(payload, received)

    def test_rejects_payload_larger_than_limit(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        send_frame(left, {"version": 1, "type": "CHUNK_DATA"}, b"12345")
        with self.assertRaises(ProtocolError):
            recv_frame(right, max_payload=4)
```

- [ ] **Step 2: Run tests and confirm the missing-module failure**

Run: `python -m unittest tests.test_protocol -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'protocol'`.

- [ ] **Step 3: Implement bounded TCP framing**

```python
# protocol.py
import json
import struct

PROTOCOL_VERSION = 1
MAX_HEADER_SIZE = 65_536
MAX_CHUNK_SIZE = 1_048_576
_LENGTH = struct.Struct("!I")


class ProtocolError(Exception):
    pass


def recv_exact(sock, size: int) -> bytes:
    if size < 0:
        raise ProtocolError("negative receive size")
    data = bytearray()
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise ConnectionError(f"connection closed after {len(data)}/{size} bytes")
        data.extend(block)
    return bytes(data)


def send_frame(sock, header: dict, payload: bytes = b"") -> None:
    wire_header = dict(header)
    wire_header["payload_length"] = len(payload)
    encoded = json.dumps(wire_header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_HEADER_SIZE:
        raise ProtocolError("header exceeds 65536 bytes")
    sock.sendall(_LENGTH.pack(len(encoded)))
    sock.sendall(encoded)
    if payload:
        sock.sendall(payload)


def recv_frame(sock, max_payload: int = MAX_CHUNK_SIZE) -> tuple[dict, bytes]:
    header_size = _LENGTH.unpack(recv_exact(sock, _LENGTH.size))[0]
    if header_size == 0 or header_size > MAX_HEADER_SIZE:
        raise ProtocolError("invalid header length")
    try:
        header = json.loads(recv_exact(sock, header_size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON header") from exc
    if not isinstance(header, dict) or header.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if not isinstance(header.get("type"), str):
        raise ProtocolError("missing message type")
    payload_length = header.get("payload_length")
    if not isinstance(payload_length, int) or not 0 <= payload_length <= max_payload:
        raise ProtocolError("invalid payload length")
    return header, recv_exact(sock, payload_length)
```

- [ ] **Step 4: Run protocol tests**

Run: `python -m unittest tests.test_protocol -v`

Expected: 2 tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add protocol.py tests/__init__.py tests/test_protocol.py
git commit -m "feat: add bounded TCP framing protocol"
git push
```

---

### Task 2: File Ranges and Thread-Safe Chunk Scheduler

**Files:**
- Create: `file_utils.py`
- Create: `scheduler.py`
- Create: `tests/test_file_utils.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `sha256_file(path) -> str`, `build_chunks(file_size, chunk_size) -> list[Chunk]`, `prepare_part_file(path, size) -> Path`, `publish_part_file(part_path, output_path)`.
- Produces: immutable `Chunk(chunk_id, offset, length)` and `ChunkScheduler.acquire()`, `.complete()`, `.retry()`, `.all_completed()`.

- [ ] **Step 1: Write failing boundary and scheduler tests**

```python
# tests/test_file_utils.py
import unittest
from file_utils import build_chunks


class ChunkRangeTests(unittest.TestCase):
    def test_ranges_cover_file_without_overlap(self):
        chunks = build_chunks(10, 4)
        self.assertEqual([(0, 0, 4), (1, 4, 4), (2, 8, 2)],
                         [(c.chunk_id, c.offset, c.length) for c in chunks])

    def test_empty_file_has_no_chunks(self):
        self.assertEqual([], build_chunks(0, 256))

    def test_rejects_invalid_chunk_size(self):
        with self.assertRaises(ValueError):
            build_chunks(10, 0)
```

```python
# tests/test_scheduler.py
import unittest
from scheduler import Chunk, ChunkScheduler


class SchedulerTests(unittest.TestCase):
    def test_retry_returns_chunk_to_queue(self):
        scheduler = ChunkScheduler([Chunk(0, 0, 4)])
        chunk = scheduler.acquire(timeout=0.01)
        scheduler.retry(chunk)
        self.assertEqual(chunk, scheduler.acquire(timeout=0.01))

    def test_complete_is_counted_once(self):
        scheduler = ChunkScheduler([Chunk(0, 0, 4)])
        chunk = scheduler.acquire(timeout=0.01)
        scheduler.complete(chunk)
        self.assertTrue(scheduler.all_completed())
        with self.assertRaises(RuntimeError):
            scheduler.complete(chunk)
```

- [ ] **Step 2: Run tests and confirm missing modules**

Run: `python -m unittest tests.test_file_utils tests.test_scheduler -v`

Expected: FAIL because `file_utils` and `scheduler` do not exist.

- [ ] **Step 3: Implement chunk model, ranges and scheduler invariants**

```python
# scheduler.py
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Lock


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: int
    offset: int
    length: int


class ChunkScheduler:
    def __init__(self, chunks: list[Chunk]):
        self._queue = Queue()
        self._state = {chunk.chunk_id: "PENDING" for chunk in chunks}
        self._lock = Lock()
        for chunk in chunks:
            self._queue.put(chunk)

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
        with self._lock:
            if self._state[chunk.chunk_id] != "IN_PROGRESS":
                raise RuntimeError("chunk is not in progress")
            self._state[chunk.chunk_id] = "COMPLETED"

    def retry(self, chunk: Chunk) -> None:
        with self._lock:
            if self._state[chunk.chunk_id] != "IN_PROGRESS":
                raise RuntimeError("chunk is not in progress")
            self._state[chunk.chunk_id] = "PENDING"
        self._queue.put(chunk)

    def all_completed(self) -> bool:
        with self._lock:
            return all(state == "COMPLETED" for state in self._state.values())

    def completed_count(self) -> int:
        with self._lock:
            return sum(state == "COMPLETED" for state in self._state.values())
```

Implement `file_utils.py` with these concrete operations:

```python
from hashlib import sha256
import os
from pathlib import Path

from protocol import MAX_CHUNK_SIZE
from scheduler import Chunk


def build_chunks(file_size: int, chunk_size: int) -> list[Chunk]:
    if file_size < 0:
        raise ValueError("file_size must be non-negative")
    if not 1 <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError("chunk_size must be between 1 and 1048576")
    return [
        Chunk(chunk_id, offset, min(chunk_size, file_size - offset))
        for chunk_id, offset in enumerate(range(0, file_size, chunk_size))
    ]


def sha256_file(path: str | Path, block_size: int = 1_048_576) -> str:
    digest = sha256()
    with Path(path).open("rb") as source:
        while block := source.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def prepare_part_file(output: str | Path, size: int) -> Path:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(output_path.name + ".part")
    with part_path.open("wb") as target:
        target.truncate(size)
    return part_path


def publish_part_file(part_path: str | Path, output: str | Path) -> None:
    os.replace(Path(part_path), Path(output))
```

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_file_utils tests.test_scheduler -v`

Expected: all tests pass, including empty file and retry transitions.

- [ ] **Step 5: Commit and push**

```bash
git add file_utils.py scheduler.py tests/test_file_utils.py tests/test_scheduler.py
git commit -m "feat: add chunk ranges and scheduler"
git push
```

---

### Task 3: Configured File Server

**Files:**
- Create: `server.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `protocol.recv_frame`, `protocol.send_frame`, `file_utils.sha256_file`.
- Produces: `FileServer(file_path, host, port, read_timeout=60)`, `.start()`, `.serve_forever()`, `.shutdown()`, and `.address`.
- Supports messages `FILE_INFO_REQUEST` and `GET_CHUNK`; returns `FILE_INFO_RESPONSE`, `CHUNK_DATA`, or `ERROR`.

- [ ] **Step 1: Write failing server behavior tests**

Create a temporary `config.dat`, start `FileServer` on `127.0.0.1:0`, and assert:

```python
request = {"version": 1, "type": "GET_CHUNK", "filename": "config.dat",
           "chunk_id": 1, "offset": 4, "length": 4}
send_frame(sock, request)
header, payload = recv_frame(sock)
self.assertEqual("CHUNK_DATA", header["type"])
self.assertEqual(b"4567", payload)
self.assertEqual(hashlib.sha256(payload).hexdigest(), header["chunk_sha256"])
```

Add a second test requesting `offset=7, length=4` from an 8-byte file and assert `type == "ERROR"`, `code == "INVALID_RANGE"`, and empty payload.

- [ ] **Step 2: Run tests and confirm missing server class**

Run: `python -m unittest tests.test_server -v`

Expected: FAIL importing `FileServer`.

- [ ] **Step 3: Implement server lifecycle and request validation**

`FileServer.__init__` resolves the configured file once, caches `file_size` and `file_sha256`, and rejects a missing/non-file path. `start()` binds and begins an accept thread; port `0` exposes the assigned test port through `.address`. Each accepted connection runs in a daemon handler thread.

For every request:

- require empty request payload;
- require `filename == configured_path.name`;
- validate integer `chunk_id >= 0`, `offset >= 0`, `1 <= length <= MAX_CHUNK_SIZE`;
- require `offset + length <= file_size`;
- read under a short-lived file handle using `seek(offset)` and verify exactly `length` bytes;
- send `CHUNK_DATA` with `chunk_sha256` and payload;
- catch malformed requests as `INVALID_REQUEST`, invalid ranges as `INVALID_RANGE`, and close only the offending connection on `ProtocolError`.

The CLI must be:

```python
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--file", required=True)
parser.add_argument("--read-timeout", type=float, default=60.0)
```

- [ ] **Step 4: Run server tests and direct CLI help smoke test**

Run: `python -m unittest tests.test_server -v`

Expected: metadata, valid chunk and invalid range tests pass.

Run: `python server.py --help`

Expected: exits 0 and lists `--host`, `--port`, `--file`, `--read-timeout`.

- [ ] **Step 5: Commit and push**

```bash
git add server.py tests/test_server.py
git commit -m "feat: serve file metadata and chunks"
git push
```

---

### Task 4: Client Configuration and Metadata Consensus

**Files:**
- Create: `client.py`
- Create: `client_config.example.json`
- Create: `tests/test_client.py`

**Interfaces:**
- Produces: `ServerEndpoint(name, host, port)`, `ClientConfig`, `FileMetadata`, `load_config(path)`, `query_metadata(endpoint, config)`, `select_consistent_servers(results)`.
- `select_consistent_servers` returns one metadata value and the matching endpoints or raises `DownloadError`.

- [ ] **Step 1: Write failing config and consensus tests**

```python
# tests/test_client.py
import unittest
from client import DownloadError, FileMetadata, ServerEndpoint, select_consistent_servers


class MetadataConsensusTests(unittest.TestCase):
    def setUp(self):
        self.s1 = ServerEndpoint("S1", "127.0.0.1", 5001)
        self.s2 = ServerEndpoint("S2", "127.0.0.1", 5002)
        self.s3 = ServerEndpoint("S3", "127.0.0.1", 5003)
        self.a = FileMetadata("config.dat", 10, "a" * 64)
        self.b = FileMetadata("config.dat", 10, "b" * 64)

    def test_two_matching_servers_win_over_one_mismatch(self):
        metadata, endpoints = select_consistent_servers(
            [(self.s1, self.a), (self.s2, self.a), (self.s3, self.b)])
        self.assertEqual(self.a, metadata)
        self.assertEqual([self.s1, self.s2], endpoints)

    def test_two_different_servers_are_ambiguous(self):
        with self.assertRaises(DownloadError):
            select_consistent_servers([(self.s1, self.a), (self.s2, self.b)])
```

Add config tests rejecting duplicate server names, invalid ports, non-positive timeouts, invalid SHA-256 strings, empty server lists, and chunk sizes over 1 MiB.

- [ ] **Step 2: Run tests and confirm missing client module**

Run: `python -m unittest tests.test_client -v`

Expected: FAIL importing `client`.

- [ ] **Step 3: Implement typed configuration and exact consensus rules**

Use frozen dataclasses:

```python
@dataclass(frozen=True, slots=True)
class ServerEndpoint:
    name: str
    host: str
    port: int

@dataclass(frozen=True, slots=True)
class FileMetadata:
    filename: str
    file_size: int
    file_sha256: str

@dataclass(frozen=True, slots=True)
class ClientConfig:
    servers: tuple[ServerEndpoint, ...]
    filename: str
    output: Path
    chunk_size: int = 262_144
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    reconnect_attempts: int = 3
```

`query_metadata` opens one TCP connection, applies connect/read timeouts, sends `FILE_INFO_REQUEST`, validates every response field, and returns `(endpoint, metadata)`. Metadata requests run concurrently so an unreachable server does not delay queries to the others.

Consensus rules must exactly match the approved specification: use all matching replies; with three replies use a 2-of-3 majority; with two differing replies fail; with one valid reply continue with a warning.

- [ ] **Step 4: Run client metadata tests**

Run: `python -m unittest tests.test_client.MetadataConsensusTests -v`

Expected: all consensus and validation tests pass.

- [ ] **Step 5: Add localhost example configuration and commit**

```json
{
  "servers": [
    {"name": "S1", "host": "127.0.0.1", "port": 5001},
    {"name": "S2", "host": "127.0.0.1", "port": 5002},
    {"name": "S3", "host": "127.0.0.1", "port": 5003}
  ],
  "filename": "config.dat",
  "output": "downloads/config.dat",
  "chunk_size": 262144,
  "connect_timeout": 5,
  "read_timeout": 60,
  "reconnect_attempts": 3
}
```

```bash
git add client.py client_config.example.json tests/test_client.py
git commit -m "feat: add client config and metadata consensus"
git push
```

---

### Task 5: Parallel Download Coordinator and Recovery

**Files:**
- Modify: `client.py`
- Modify: `scheduler.py`
- Modify: `tests/test_client.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `ChunkScheduler`, `build_chunks`, protocol and metadata selection.
- Produces: `DownloadResult(output, file_sha256, bytes_written, per_server_chunks, per_server_bytes, per_server_kbps)`, `download(config) -> DownloadResult`.

- [ ] **Step 1: Write failing multi-source integration tests**

In `tests/test_client.py`, start three `FileServer` instances over the same deterministic 2 MiB source. Configure 64 KiB chunks and assert:

```python
result = download(config)
self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), result.file_sha256)
self.assertEqual(source_bytes, output.read_bytes())
self.assertGreaterEqual(sum(count > 0 for count in result.per_server_chunks.values()), 2)
self.assertEqual(len(source_bytes), sum(result.per_server_bytes.values()))
self.assertTrue(all(speed >= 0 for speed in result.per_server_kbps.values()))
```

Add a recovery test with a test-only `DisconnectAfterMetadataServer`: it returns valid `FILE_INFO_RESPONSE`, then accepts the worker connection and closes it on the first `GET_CHUNK`. Assert the two normal servers finish the file and the failed endpoint has zero completed chunks. Add a mismatch test where S3 serves different bytes and assert only S1/S2 appear in `per_server_chunks`. Keep this failure fixture inside `tests/test_client.py`; do not add test hooks to production `FileServer`.

- [ ] **Step 2: Run focused tests and observe missing `download` behavior**

Run: `python -m unittest tests.test_client -v`

Expected: metadata tests pass; download tests fail because `download` and `DownloadResult` are absent.

- [ ] **Step 3: Implement worker and coordinator invariants**

Each worker must:

1. Connect to its fixed endpoint with configured timeouts.
2. Acquire one chunk from the shared scheduler.
3. Send `GET_CHUNK` and verify response type, IDs, offset, length and SHA-256.
4. Hold payload in memory until verified.
5. Write through one shared file lock using `seek + write`.
6. Mark the chunk complete and increment only that endpoint's counter.
7. On connection failure, return an in-progress chunk exactly once, reconnect at most `reconnect_attempts`, then mark the worker unavailable.
8. On protocol/hash violation, return the chunk and permanently remove that endpoint.

The coordinator must:

- create/truncate `<output>.part` before workers start;
- handle the empty-file case without starting workers;
- stop successfully only when every chunk is complete;
- fail if all workers are unavailable while chunks remain;
- flush and close the part file before hashing;
- compare whole-file SHA-256 with metadata;
- call `os.replace(part_path, output)` only on a match;
- join every worker and close every socket on success or failure.

Add scheduler accessors `has_unfinished()` and `in_progress_count()` under the same lock; never inspect private state from `client.py`.

- [ ] **Step 4: Run parallel and recovery tests repeatedly**

Run: `python -m unittest tests.test_scheduler tests.test_client -v`

Expected: all tests pass.

Run three times to catch race conditions:

```text
python -m unittest tests.test_client -v
python -m unittest tests.test_client -v
python -m unittest tests.test_client -v
```

Expected: all three runs pass without hangs or duplicate-completion errors.

- [ ] **Step 5: Commit and push**

```bash
git add client.py scheduler.py tests/test_client.py tests/test_scheduler.py
git commit -m "feat: download chunks concurrently with recovery"
git push
```

---

### Task 6: CLI, Progress Output and End-to-End Verification

**Files:**
- Modify: `client.py`
- Create: `tests/test_integration.py`

**Interfaces:**
- Produces CLI `python client.py --config <path>` with exit code 0 on verified download and nonzero on configuration, network, protocol or hash failure.
- Output includes file size/hash, progress, per-server chunk totals, bytes, average Kbps and final saved path.

- [ ] **Step 1: Write end-to-end CLI test**

The test starts three `FileServer` objects on ephemeral ports, writes a temporary JSON config, launches:

```python
completed = subprocess.run(
    [sys.executable, "client.py", "--config", str(config_path)],
    cwd=PROJECT_ROOT,
    text=True,
    capture_output=True,
    timeout=30,
)
self.assertEqual(0, completed.returncode, completed.stderr)
self.assertIn("SHA-256 verified", completed.stdout)
self.assertEqual(source.read_bytes(), output.read_bytes())
```

Also invoke a config pointing to unavailable ports and assert a nonzero exit code, an actionable error on stderr, and no final output file.

- [ ] **Step 2: Run test and confirm CLI contract failure**

Run: `python -m unittest tests.test_integration -v`

Expected: FAIL until `client.py` exposes the final CLI contract.

- [ ] **Step 3: Implement CLI and stable progress reporting**

Use:

```python
parser = argparse.ArgumentParser(description="Download one file from multiple TCP servers")
parser.add_argument("--config", required=True, type=Path)
```

Print connection results, filename, human-readable size, SHA-256, chunk count, periodic `completed/total` progress, and per-server completed chunk/byte counts. Measure each worker with `time.monotonic()` and report average Kbps as `(bytes_received * 8 / 1000) / elapsed_seconds`; use `0.0` when no bytes were received. Finish with `SHA-256 verified` and `Saved: <path>`. Catch `DownloadError`, `OSError`, `ValueError`, and malformed JSON at the CLI boundary; print one concise error to stderr and return exit code 1. Do not swallow traceback-producing programmer errors.

- [ ] **Step 4: Run the full automated suite**

Run: `python -m unittest discover -s tests -v`

Expected: all protocol, range, scheduler, server, client and CLI integration tests pass.

- [ ] **Step 5: Run an actual localhost smoke scenario**

Create a temporary 10 MiB `config.dat`, start three real server processes on ports 5001, 5002 and 5003, then run `python client.py --config client_config.example.json`. Verify:

- client reports connections to all three servers;
- at least two servers complete chunks;
- output path is `downloads/config.dat`;
- `python -c "import hashlib,pathlib; p=pathlib.Path('downloads/config.dat'); print(hashlib.sha256(p.read_bytes()).hexdigest())"` equals the source SHA-256;
- terminating one server during a second run still produces a verified output through the remaining servers.

Remove generated smoke-test data and `downloads/config.dat` after verification; keep only source, tests and example config.

- [ ] **Step 6: Commit and push complete implementation**

```bash
git add client.py tests/test_integration.py
git commit -m "feat: add download CLI and end-to-end verification"
git push
```

---

## Final Verification Gate

Run:

```text
python -m unittest discover -s tests -v
python server.py --help
python client.py --help
git status --short --branch
```

Acceptance:

- Entire test suite passes without hangs.
- Both CLIs exit 0 for `--help`.
- Localhost smoke download produces the same SHA-256 as the source.
- A server can disappear mid-download while remaining servers finish.
- Working tree is clean and `main` tracks `origin/main`.
