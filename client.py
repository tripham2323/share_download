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
from checkpoint import CheckpointIdentity, CheckpointStore, load_checkpoint, state_path_for

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

    def __init__(self, message: str, *, code: str = "DOWNLOAD_ERROR"):
        super().__init__(message)
        self.code = code

class DownloadCancelled(DownloadError):
    """The user stopped the session before publication."""


class DownloadController:
    """Own cancellation and interrupt sockets blocked on a remote read."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()

    def cancel(self) -> None:
        with self._lock:
            self.cancelled.set()
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def register(self, sock: socket.socket) -> None:
        with self._lock:
            if self.cancelled.is_set():
                sock.close()
                raise DownloadCancelled("download cancelled")
            self._sockets.add(sock)

    def unregister(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)

    def check(self) -> None:
        if self.cancelled.is_set():
            raise DownloadCancelled("download cancelled")
    def publish(self, part_path: Path, output: Path) -> None:
        with self._lock:
            self.check()
            publish_part_file(part_path, output)



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
    file_size: int = 0
    verified_bytes: int = 0
    chunk_id: int | None = None
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
    return parse_config(raw, config_path.parent)


def parse_config(raw: dict[str, Any], base_dir: Path) -> ClientConfig:
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
        output = base_dir / output

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
    controller: DownloadController | None = None,
) -> tuple[ServerEndpoint, FileMetadata]:
    try:
        with socket.create_connection(
            (endpoint.host, endpoint.port), timeout=config.connect_timeout
        ) as sock:
            sock.settimeout(config.read_timeout)
            if controller is not None:
                controller.register(sock)
            try:
                send_frame(
                    sock,
                    {
                        "version": PROTOCOL_VERSION,
                        "type": "FILE_INFO_REQUEST",
                        "filename": config.filename,
                    },
                )
                header, payload = recv_frame(sock, max_payload=0)
            finally:
                if controller is not None:
                    controller.unregister(sock)
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
        raise DownloadError("no valid metadata responses", code="NO_METADATA")

    groups: dict[FileMetadata, list[ServerEndpoint]] = defaultdict(list)
    for endpoint, metadata in results:
        groups[metadata].append(endpoint)

    ranked = sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)
    metadata, endpoints = ranked[0]
    if len(results) == 1 or len(endpoints) > len(results) / 2:
        return metadata, endpoints
    raise DownloadError("no metadata majority among responding servers",
                        code="METADATA_CONFLICT")


def download(
    config: ClientConfig,
    progress_callback: Callable[[int, int], None] | None = None,
    *,
    event_callback: Callable[[DownloadEvent], None] | None = None,
    controller: DownloadController | None = None,
    resume: bool = False,
    save_checkpoint: bool = False,
) -> DownloadResult:
    event_lock = threading.Lock()

    def emit(event: DownloadEvent) -> None:
        if event_callback is not None:
            with event_lock:
                event_callback(event)
    control = controller or DownloadController()
    control.check()

    metadata_results = _query_all_metadata(config, emit, control)
    control.check()
    metadata, endpoints = select_consistent_servers(metadata_results)
    chunks = build_chunks(metadata.file_size, config.chunk_size)
    part_path = config.output.with_name(config.output.name + ".part")
    identity = CheckpointIdentity(metadata.filename, metadata.file_size,
                                  metadata.file_sha256, config.chunk_size)
    if resume:
        verified = load_checkpoint(part_path, identity, chunks)
        if not part_path.exists():
            prepare_part_file(config.output, metadata.file_size)
    else:
        state_path_for(part_path).unlink(missing_ok=True)
        prepare_part_file(config.output, metadata.file_size)
        verified = {}
    scheduler = ChunkScheduler(chunks, completed_ids=frozenset(verified))
    checkpoint = CheckpointStore(part_path, identity, verified) if resume or save_checkpoint else None
    emit(DownloadEvent("metadata", completed_chunks=len(verified), total_chunks=len(chunks),
                       file_size=metadata.file_size,
                       verified_bytes=sum(chunks[chunk_id].length for chunk_id in verified),
                       message=f"{metadata.file_size} bytes; SHA-256 {metadata.file_sha256}"))
    responded = {endpoint for endpoint, _ in metadata_results}
    for endpoint in config.servers:
        if endpoint not in endpoints:
            unavailable = endpoint not in responded
            emit(DownloadEvent("server_unavailable" if unavailable else "server_excluded",
                               server=endpoint.name,
                               message="No metadata response" if unavailable else "Different file version"))
    control.check()
    stats = {endpoint.name: _WorkerStats() for endpoint in endpoints}

    if scheduler.has_unfinished():
        write_lock = threading.Lock()
        start_barrier = threading.Barrier(len(endpoints))
        worker_errors: list[Exception] = []
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
                        control,
                        checkpoint,
                        worker_errors,
                    ),
                )
                for endpoint in endpoints
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            if checkpoint is not None:
                checkpoint.flush(target)

        if worker_errors:
            error = worker_errors[0]
            code = "DISK_FULL" if isinstance(error, OSError) and error.errno == 28 else "DOWNLOAD_ERROR"
            raise DownloadError(f"unable to write downloaded chunk: {error}", code=code) from error
        control.check()
        if scheduler.has_unfinished():
            raise DownloadError("all usable servers failed before download completed",
                                code="ALL_SERVERS_FAILED")

    control.check()
    emit(DownloadEvent("verifying", completed_chunks=scheduler.completed_count(),
                       total_chunks=scheduler.total_count))
    actual_sha256 = sha256_file(part_path)
    if actual_sha256 != metadata.file_sha256:
        raise DownloadError(
            "downloaded file SHA-256 does not match server metadata",
            code="HASH_MISMATCH",
        )
    control.publish(part_path, config.output)
    if checkpoint is not None:
        checkpoint.state_path.unlink(missing_ok=True)
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
    controller: DownloadController | None = None,
) -> list[tuple[ServerEndpoint, FileMetadata]]:
    results: list[tuple[ServerEndpoint, FileMetadata]] = []
    with ThreadPoolExecutor(max_workers=len(config.servers)) as executor:
        futures = {
            executor.submit(query_metadata, endpoint, config, controller): endpoint
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
    controller: DownloadController,
    checkpoint: CheckpointStore | None,
    worker_errors: list[Exception],
) -> None:
    started = time.monotonic()
    failures = 0
    sock = None
    try:
        try:
            sock = socket.create_connection(
                (endpoint.host, endpoint.port),
                timeout=config.connect_timeout,
            )
            sock.settimeout(config.read_timeout)
            controller.register(sock)
            emit(DownloadEvent("server_connected", server=endpoint.name))
        except DownloadCancelled:
            return
        except OSError as exc:
            failures += 1
            emit(DownloadEvent("server_failed", server=endpoint.name, message=str(exc)))
        finally:
            try:
                start_barrier.wait()
            except threading.BrokenBarrierError:
                pass

        while (scheduler.has_unfinished() and not controller.cancelled.is_set()
               and failures <= config.reconnect_attempts):
            if sock is None:
                try:
                    sock = socket.create_connection(
                        (endpoint.host, endpoint.port),
                        timeout=config.connect_timeout,
                    )
                    sock.settimeout(config.read_timeout)
                    controller.register(sock)
                    emit(DownloadEvent("server_connected", server=endpoint.name))
                except DownloadCancelled:
                    return
                except OSError as exc:
                    failures += 1
                    emit(DownloadEvent("server_failed", server=endpoint.name, message=str(exc)))
                    continue

            try:
                while scheduler.has_unfinished() and not controller.cancelled.is_set():
                    chunk = scheduler.acquire(timeout=0.1)
                    if chunk is None:
                        continue
                    emit(DownloadEvent("chunk_start", server=endpoint.name, chunk_id=chunk.chunk_id))
                    try:
                        payload, digest = _request_chunk(sock, endpoint, config, chunk)
                        if controller.cancelled.is_set():
                            scheduler.retry(chunk)
                            break
                    except (OSError, ConnectionError, socket.timeout) as exc:
                        scheduler.retry(chunk)
                        emit(DownloadEvent("chunk_requeued", server=endpoint.name,
                                           chunk_id=chunk.chunk_id,
                                           message=str(exc)))
                        failures += 1
                        break
                    except (ProtocolError, _ServerDataError) as exc:
                        scheduler.retry(chunk)
                        emit(DownloadEvent("chunk_requeued", server=endpoint.name,
                                           chunk_id=chunk.chunk_id,
                                           message=str(exc)))
                        emit(DownloadEvent("server_failed", server=endpoint.name,
                                           message=str(exc)))
                        return

                    with write_lock:
                        target.seek(chunk.offset)
                        written = target.write(payload)
                        if written != chunk.length:
                            raise OSError("short write to partial file")
                        if checkpoint is not None:
                            checkpoint.record(chunk, digest)
                            if len(checkpoint.pending) >= 16:
                                checkpoint.flush(target)
                        scheduler.complete(chunk)
                    stats.chunks += 1
                    stats.bytes_received += len(payload)
                    failures = 0
                    emit(DownloadEvent("chunk", server=endpoint.name,
                                       chunk_id=chunk.chunk_id,
                                       completed_chunks=scheduler.completed_count(),
                                       total_chunks=scheduler.total_count,
                                       bytes_delta=len(payload)))
                    if progress_callback is not None:
                        progress_callback(
                            scheduler.completed_count(),
                            scheduler.total_count,
                        )
            finally:
                if sock is not None:
                    controller.unregister(sock)
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
        if failures and not controller.cancelled.is_set():
            emit(DownloadEvent("server_failed", server=endpoint.name,
                               message="Unable to continue serving chunks"))
    except threading.BrokenBarrierError:
        return
    except Exception as exc:
        worker_errors.append(exc)
        controller.cancel()
    finally:
        stats.elapsed_seconds = max(time.monotonic() - started, 0.0)


def _request_chunk(
    sock: socket.socket,
    endpoint: ServerEndpoint,
    config: ClientConfig,
    chunk: Chunk,
) -> tuple[bytes, str]:
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
    return payload, chunk_sha256.lower()


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
    parser.add_argument("--config", required=True, type=Path, help="Path to JSON configuration file")
    parser.add_argument("--visual", action="store_true", help="Enable terminal live visual dashboard")
    parser.add_argument("--resume", action="store_true", help="Resume partial download from checkpoint")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    controller = DownloadController()
    dashboard = None

    try:
        config = load_config(args.config)

        event_cb = None
        progress_cb = None

        if args.visual:
            from dashboard import VisualDashboard
            dashboard = VisualDashboard(
                filename=config.filename,
                file_size=0,
                total_chunks=0,
                server_names=[s.name for s in config.servers],
            )

            def _on_event(ev: DownloadEvent) -> None:
                if ev.kind == "metadata":
                    dashboard.setup_metadata(
                        filename=config.filename,
                        file_size=ev.file_size,
                        total_chunks=ev.total_chunks,
                        server_names=[s.name for s in config.servers],
                    )
                elif ev.kind == "chunk_start" and ev.chunk_id is not None and ev.server:
                    dashboard.on_chunk_start(ev.server, ev.chunk_id)
                elif ev.kind == "chunk" and ev.server:
                    dashboard.on_chunk_complete(
                        ev.server,
                        ev.chunk_id if ev.chunk_id is not None else -1,
                        ev.bytes_delta,
                    )
                elif ev.kind == "chunk_requeued" and ev.server:
                    dashboard.on_chunk_retry(
                        ev.server,
                        ev.chunk_id if ev.chunk_id is not None else -1,
                    )
                elif ev.kind in ("server_failed", "server_unavailable") and ev.server:
                    dashboard.on_server_fail(ev.server)
                elif ev.kind == "verifying":
                    dashboard.set_sha256_status("Verifying SHA-256...")

            event_cb = _on_event
        else:
            print("Connecting:")
            for endpoint in config.servers:
                print(f"  {endpoint.name} {endpoint.host}:{endpoint.port}")
            progress_cb = _ProgressPrinter()

        result = download(
            config,
            progress_callback=progress_cb,
            event_callback=event_cb,
            controller=controller,
            resume=args.resume,
            save_checkpoint=args.resume,
        )

        if dashboard:
            dashboard.finish(result.file_sha256)

    except KeyboardInterrupt:
        controller.cancel()
        print("\nDownload interrupted by user. Stopping cleanly...", file=sys.stderr)
        return 130
    except DownloadCancelled:
        print("\nDownload cancelled.", file=sys.stderr)
        return 130
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
