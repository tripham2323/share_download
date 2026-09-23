"""Multi-source file download client."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import socket
import sys
import threading
import time
from typing import Any, Callable

from file_utils import (
    build_chunks,
    prepare_part_file,
    publish_part_file,
    sha256_file,
)
from protocol import MAX_CHUNK_SIZE, PROTOCOL_VERSION, ProtocolError, recv_frame, send_frame
from scheduler import Chunk, ChunkScheduler


class DownloadError(Exception):
    """Raised when a download cannot safely continue."""


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


@dataclass(frozen=True, slots=True)
class DownloadResult:
    output: Path
    file_sha256: str
    bytes_written: int
    per_server_chunks: dict[str, int]
    per_server_bytes: dict[str, int]
    per_server_kbps: dict[str, float]
    unused_servers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DownloadEvent:
    kind: str
    server: str | None = None
    completed_chunks: int = 0
    total_chunks: int = 0
    bytes_delta: int = 0
    message: str = ""


@dataclass(slots=True)
class _WorkerStats:
    chunks: int = 0
    bytes_received: int = 0
    elapsed_seconds: float = 0.0


class _ServerDataError(Exception):
    pass


def load_config(path: str | Path) -> ClientConfig:
    config_path = Path(path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("config root must be a JSON object")

    raw_servers = raw.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        raise ValueError("servers must be a non-empty list")

    servers: list[ServerEndpoint] = []
    for index, item in enumerate(raw_servers):
        if not isinstance(item, dict):
            raise ValueError(f"servers[{index}] must be an object")
        name = item.get("name")
        host = item.get("host")
        port = item.get("port")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"servers[{index}].name must be non-empty")
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"servers[{index}].host must be non-empty")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise ValueError(f"servers[{index}].port must be between 1 and 65535")
        servers.append(ServerEndpoint(name.strip(), host.strip(), port))

    names = [server.name for server in servers]
    if len(names) != len(set(names)):
        raise ValueError("server names must be unique")

    filename = raw.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise ValueError("filename must be a plain file name")

    output_value = raw.get("output")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValueError("output must be a non-empty path")
    output = Path(output_value)
    if not output.is_absolute():
        output = config_path.parent / output

    chunk_size = _integer_setting(raw, "chunk_size", 262_144, minimum=1)
    if chunk_size > MAX_CHUNK_SIZE:
        raise ValueError(f"chunk_size must not exceed {MAX_CHUNK_SIZE}")
    reconnect_attempts = _integer_setting(
        raw, "reconnect_attempts", 3, minimum=0
    )
    connect_timeout = _positive_number(raw, "connect_timeout", 5.0)
    read_timeout = _positive_number(raw, "read_timeout", 60.0)

    return ClientConfig(
        servers=tuple(servers),
        filename=filename,
        output=output,
        chunk_size=chunk_size,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        reconnect_attempts=reconnect_attempts,
    )


def query_metadata(
    endpoint: ServerEndpoint,
    config: ClientConfig,
) -> tuple[ServerEndpoint, FileMetadata]:
    try:
        with socket.create_connection(
            (endpoint.host, endpoint.port), timeout=config.connect_timeout
        ) as sock:
            sock.settimeout(config.read_timeout)
            send_frame(
                sock,
                {
                    "version": PROTOCOL_VERSION,
                    "type": "FILE_INFO_REQUEST",
                    "filename": config.filename,
                },
            )
            header, payload = recv_frame(sock, max_payload=0)
    except (OSError, ConnectionError, ProtocolError) as exc:
        raise DownloadError(f"{endpoint.name}: metadata request failed: {exc}") from exc

    if payload:
        raise DownloadError(f"{endpoint.name}: metadata response has a payload")
    if header["type"] == "ERROR":
        raise DownloadError(
            f"{endpoint.name}: {header.get('code', 'ERROR')}: "
            f"{header.get('message', 'metadata request failed')}"
        )
    if header["type"] != "FILE_INFO_RESPONSE" or header.get("status") != "OK":
        raise DownloadError(f"{endpoint.name}: invalid metadata response")

    filename = header.get("filename")
    file_size = header.get("file_size")
    file_sha256 = header.get("file_sha256")
    if filename != config.filename:
        raise DownloadError(f"{endpoint.name}: metadata filename mismatch")
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        raise DownloadError(f"{endpoint.name}: invalid file size")
    if not _is_sha256(file_sha256):
        raise DownloadError(f"{endpoint.name}: invalid SHA-256")

    return endpoint, FileMetadata(filename, file_size, file_sha256.lower())


def select_consistent_servers(
    results: list[tuple[ServerEndpoint, FileMetadata]],
) -> tuple[FileMetadata, list[ServerEndpoint]]:
    if not results:
        raise DownloadError("no valid metadata responses")

    groups: dict[FileMetadata, list[ServerEndpoint]] = defaultdict(list)
    for endpoint, metadata in results:
        groups[metadata].append(endpoint)

    ranked = sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)
    metadata, endpoints = ranked[0]
    if len(results) == 1 or len(endpoints) > len(results) / 2:
        return metadata, endpoints
    raise DownloadError("no metadata majority among responding servers")


def download(
    config: ClientConfig,
    progress_callback: Callable[[int, int], None] | None = None,
    *,
    event_callback: Callable[[DownloadEvent], None] | None = None,
) -> DownloadResult:
    event_lock = threading.Lock()

    def emit(event: DownloadEvent) -> None:
        if event_callback is not None:
            with event_lock:
                event_callback(event)

    metadata_results = _query_all_metadata(config, emit)
    metadata, endpoints = select_consistent_servers(metadata_results)
    chunks = build_chunks(metadata.file_size, config.chunk_size)
    scheduler = ChunkScheduler(chunks)
    emit(DownloadEvent("metadata", completed_chunks=0, total_chunks=len(chunks),
                       message=f"{metadata.file_size} bytes; SHA-256 {metadata.file_sha256}"))
    for endpoint in config.servers:
        if endpoint not in endpoints:
            emit(DownloadEvent("server_excluded", server=endpoint.name,
                               message="Unavailable or different file version"))
    part_path = prepare_part_file(config.output, metadata.file_size)
    stats = {endpoint.name: _WorkerStats() for endpoint in endpoints}

    if chunks:
        write_lock = threading.Lock()
        start_barrier = threading.Barrier(len(endpoints))
        with part_path.open("r+b") as target:
            workers = [
                threading.Thread(
                    target=_download_worker,
                    name=f"download-{endpoint.name}",
                    args=(
                        endpoint,
                        config,
                        scheduler,
                        target,
                        write_lock,
                        start_barrier,
                        stats[endpoint.name],
                        progress_callback,
                        emit,
                    ),
                )
                for endpoint in endpoints
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

        if scheduler.has_unfinished():
            raise DownloadError("all usable servers failed before download completed")

    emit(DownloadEvent("verifying", completed_chunks=scheduler.completed_count(),
                       total_chunks=scheduler.total_count))
    actual_sha256 = sha256_file(part_path)
    if actual_sha256 != metadata.file_sha256:
        raise DownloadError(
            "downloaded file SHA-256 does not match server metadata"
        )
    publish_part_file(part_path, config.output)
    emit(DownloadEvent("completed", completed_chunks=scheduler.completed_count(),
                       total_chunks=scheduler.total_count))

    return DownloadResult(
        output=config.output,
        file_sha256=actual_sha256,
        bytes_written=metadata.file_size,
        per_server_chunks={name: item.chunks for name, item in stats.items()},
        per_server_bytes={
            name: item.bytes_received for name, item in stats.items()
        },
        per_server_kbps={
            name: (
                (item.bytes_received * 8 / 1000) / item.elapsed_seconds
                if item.bytes_received and item.elapsed_seconds > 0
                else 0.0
            )
            for name, item in stats.items()
        },
        unused_servers=tuple(
            endpoint.name
            for endpoint in config.servers
            if endpoint not in endpoints
        ),
    )


def _query_all_metadata(
    config: ClientConfig,
    emit: Callable[[DownloadEvent], None] | None = None,
) -> list[tuple[ServerEndpoint, FileMetadata]]:
    results: list[tuple[ServerEndpoint, FileMetadata]] = []
    with ThreadPoolExecutor(max_workers=len(config.servers)) as executor:
        futures = {
            executor.submit(query_metadata, endpoint, config): endpoint
            for endpoint in config.servers
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except DownloadError as exc:
                if emit is not None:
                    emit(DownloadEvent("server_failed", server=futures[future].name,
                                       message=str(exc)))
    return results


def _download_worker(
    endpoint: ServerEndpoint,
    config: ClientConfig,
    scheduler: ChunkScheduler,
    target,
    write_lock: threading.Lock,
    start_barrier: threading.Barrier,
    stats: _WorkerStats,
    progress_callback: Callable[[int, int], None] | None,
    emit: Callable[[DownloadEvent], None],
) -> None:
    started = time.monotonic()
    failures = 0
    try:
        start_barrier.wait()
        while scheduler.has_unfinished() and failures <= config.reconnect_attempts:
            try:
                sock = socket.create_connection(
                    (endpoint.host, endpoint.port),
                    timeout=config.connect_timeout,
                )
                sock.settimeout(config.read_timeout)
                emit(DownloadEvent("server_connected", server=endpoint.name))
            except OSError as exc:
                failures += 1
                emit(DownloadEvent("server_failed", server=endpoint.name, message=str(exc)))
                continue

            try:
                while scheduler.has_unfinished():
                    chunk = scheduler.acquire(timeout=0.1)
                    if chunk is None:
                        continue
                    try:
                        payload = _request_chunk(sock, endpoint, config, chunk)
                    except (OSError, ConnectionError, socket.timeout) as exc:
                        scheduler.retry(chunk)
                        emit(DownloadEvent("chunk_requeued", server=endpoint.name,
                                           message=str(exc)))
                        failures += 1
                        break
                    except (ProtocolError, _ServerDataError) as exc:
                        scheduler.retry(chunk)
                        emit(DownloadEvent("chunk_requeued", server=endpoint.name,
                                           message=str(exc)))
                        emit(DownloadEvent("server_failed", server=endpoint.name,
                                           message=str(exc)))
                        return

                    with write_lock:
                        target.seek(chunk.offset)
                        written = target.write(payload)
                        if written != chunk.length:
                            scheduler.retry(chunk)
                            return
                    scheduler.complete(chunk)
                    stats.chunks += 1
                    stats.bytes_received += len(payload)
                    failures = 0
                    emit(DownloadEvent("chunk", server=endpoint.name,
                                       completed_chunks=scheduler.completed_count(),
                                       total_chunks=scheduler.total_count,
                                       bytes_delta=len(payload)))
                    if progress_callback is not None:
                        progress_callback(
                            scheduler.completed_count(),
                            scheduler.total_count,
                        )
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
    except threading.BrokenBarrierError:
        return
    finally:
        stats.elapsed_seconds = max(time.monotonic() - started, 0.0)


def _request_chunk(
    sock: socket.socket,
    endpoint: ServerEndpoint,
    config: ClientConfig,
    chunk: Chunk,
) -> bytes:
    send_frame(
        sock,
        {
            "version": PROTOCOL_VERSION,
            "type": "GET_CHUNK",
            "filename": config.filename,
            "chunk_id": chunk.chunk_id,
            "offset": chunk.offset,
            "length": chunk.length,
        },
    )
    header, payload = recv_frame(sock, max_payload=chunk.length)
    if header["type"] == "ERROR":
        raise _ServerDataError(
            f"{endpoint.name}: {header.get('code', 'ERROR')}: "
            f"{header.get('message', 'chunk request failed')}"
        )
    if (
        header["type"] != "CHUNK_DATA"
        or header.get("status") != "OK"
        or header.get("chunk_id") != chunk.chunk_id
        or header.get("offset") != chunk.offset
        or header.get("length") != chunk.length
        or len(payload) != chunk.length
    ):
        raise _ServerDataError(f"{endpoint.name}: invalid chunk response")
    chunk_sha256 = header.get("chunk_sha256")
    if not _is_sha256(chunk_sha256):
        raise _ServerDataError(f"{endpoint.name}: invalid chunk SHA-256")
    if sha256(payload).hexdigest() != chunk_sha256.lower():
        raise _ServerDataError(f"{endpoint.name}: chunk SHA-256 mismatch")
    return payload


def _integer_setting(
    raw: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _positive_number(raw: dict[str, Any], name: str, default: float) -> float:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


class _ProgressPrinter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_completed = -1

    def __call__(self, completed: int, total: int) -> None:
        with self._lock:
            if completed != self._last_completed:
                self._last_completed = completed
                percent = completed * 100 / total if total else 100.0
                print(f"Progress: {completed}/{total} ({percent:.1f}%)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download one file from multiple TCP servers"
    )
    parser.add_argument("--config", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        print("Connecting:")
        for endpoint in config.servers:
            print(f"  {endpoint.name} {endpoint.host}:{endpoint.port}")
        result = download(config, progress_callback=_ProgressPrinter())
    except (DownloadError, OSError, ValueError) as exc:
        print(f"Download error: {exc}", file=sys.stderr)
        return 1

    print(f"File size: {_format_size(result.bytes_written)}")
    for name in result.per_server_chunks:
        print(
            f"  {name}: {result.per_server_chunks[name]} chunks, "
            f"{result.per_server_bytes[name]} bytes, "
            f"{result.per_server_kbps[name]:.1f} Kbps"
        )
    if result.unused_servers:
        print(
            "Warning: unused servers: "
            + ", ".join(result.unused_servers),
            file=sys.stderr,
        )
    if result.bytes_written == 0:
        print("Progress: 0/0 (100.0%)")
    print(f"SHA-256 verified: {result.file_sha256}")
    print(f"Saved: {result.output}")
    return 0


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KiB"
    return f"{size / (1024 * 1024):.2f} MiB"


if __name__ == "__main__":
    raise SystemExit(main())
