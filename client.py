"""Multi-source file download client."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import socket
from typing import Any

from protocol import MAX_CHUNK_SIZE, PROTOCOL_VERSION, ProtocolError, recv_frame, send_frame


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
