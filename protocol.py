"""Length-prefixed JSON headers with optional binary payloads over TCP."""

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL_VERSION = 1
MAX_HEADER_SIZE = 65_536
MAX_CHUNK_SIZE = 1_048_576
_LENGTH = struct.Struct("!I")


class ProtocolError(Exception):
    """Raised when a peer sends a malformed or unsupported frame."""


def recv_exact(sock: Any, size: int) -> bytes:
    """Receive exactly *size* bytes or raise when the peer closes early."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ProtocolError("invalid receive size")

    data = bytearray()
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise ConnectionError(
                f"connection closed after {len(data)}/{size} bytes"
            )
        data.extend(block)
    return bytes(data)


def send_frame(sock: Any, header: dict[str, Any], payload: bytes = b"") -> None:
    """Send one frame without mutating the caller's header dictionary."""
    if header.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if not isinstance(header.get("type"), str) or not header["type"]:
        raise ProtocolError("missing message type")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")

    wire_header = dict(header)
    wire_header["payload_length"] = len(payload)
    try:
        encoded = json.dumps(
            wire_header,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("header is not JSON serializable") from exc

    if not encoded or len(encoded) > MAX_HEADER_SIZE:
        raise ProtocolError("invalid header length")

    sock.sendall(_LENGTH.pack(len(encoded)))
    sock.sendall(encoded)
    if payload:
        sock.sendall(payload)


def recv_frame(
    sock: Any,
    max_payload: int = MAX_CHUNK_SIZE,
) -> tuple[dict[str, Any], bytes]:
    """Receive and validate one complete frame."""
    if (
        isinstance(max_payload, bool)
        or not isinstance(max_payload, int)
        or max_payload < 0
    ):
        raise ValueError("max_payload must be a non-negative integer")

    header_size = _LENGTH.unpack(recv_exact(sock, _LENGTH.size))[0]
    if header_size == 0 or header_size > MAX_HEADER_SIZE:
        raise ProtocolError("invalid header length")

    encoded = recv_exact(sock, header_size)
    try:
        header = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON header") from exc

    if not isinstance(header, dict):
        raise ProtocolError("header must be a JSON object")
    if header.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if not isinstance(header.get("type"), str) or not header["type"]:
        raise ProtocolError("missing message type")

    payload_length = header.get("payload_length")
    if (
        isinstance(payload_length, bool)
        or not isinstance(payload_length, int)
        or payload_length < 0
        or payload_length > max_payload
    ):
        raise ProtocolError("invalid payload length")

    return header, recv_exact(sock, payload_length)
