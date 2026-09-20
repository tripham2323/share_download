"""TCP server that exposes metadata and byte ranges for one configured file."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import socket
import threading
from typing import Any

from file_utils import sha256_file
from protocol import MAX_CHUNK_SIZE, PROTOCOL_VERSION, ProtocolError, recv_frame, send_frame


class RequestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FileServer:
    def __init__(
        self,
        file_path: str | Path,
        host: str,
        port: int,
        read_timeout: float = 60.0,
    ):
        path = Path(file_path).resolve()
        if not path.is_file():
            raise ValueError(f"configured file does not exist: {path}")
        if not host:
            raise ValueError("host must not be empty")
        if not 0 <= port <= 65_535:
            raise ValueError("port must be between 0 and 65535")
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")

        self.file_path = path
        self.host = host
        self.port = port
        self.read_timeout = float(read_timeout)
        self.file_size = path.stat().st_size
        self.file_sha256 = sha256_file(path)

        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._clients: set[socket.socket] = set()
        self._state_lock = threading.Lock()
        self._stopped = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        if self._listener is None:
            raise RuntimeError("server has not started")
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("server has already started")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen()
        listener.settimeout(0.2)
        self._listener = listener
        self._stopped.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"file-server-{self.address[1]}",
            daemon=True,
        )
        self._accept_thread.start()

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stopped.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stopped.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        with self._state_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

        current = threading.current_thread()
        if self._accept_thread is not None and self._accept_thread is not current:
            self._accept_thread.join(timeout=2)
        with self._state_lock:
            threads = list(self._client_threads)
        for thread in threads:
            if thread is not current:
                thread.join(timeout=2)

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.settimeout(self.read_timeout)
            thread = threading.Thread(
                target=self._handle_client,
                args=(client,),
                daemon=True,
            )
            with self._state_lock:
                self._clients.add(client)
                self._client_threads.add(thread)
            thread.start()

    def _handle_client(self, client: socket.socket) -> None:
        current = threading.current_thread()
        try:
            while not self._stopped.is_set():
                try:
                    header, payload = recv_frame(client, max_payload=0)
                except (ConnectionError, socket.timeout):
                    break
                except ProtocolError:
                    break

                if payload:
                    self._send_error(client, "INVALID_REQUEST", "request payload is not allowed")
                    continue
                try:
                    self._dispatch(client, header)
                except RequestError as exc:
                    self._send_error(client, exc.code, exc.message)
                except OSError:
                    self._send_error(client, "INTERNAL_ERROR", "unable to read configured file")
        except OSError:
            pass
        finally:
            with self._state_lock:
                self._clients.discard(client)
                self._client_threads.discard(current)
            try:
                client.close()
            except OSError:
                pass

    def _dispatch(self, client: socket.socket, header: dict[str, Any]) -> None:
        message_type = header["type"]
        if message_type == "FILE_INFO_REQUEST":
            self._validate_filename(header)
            send_frame(
                client,
                {
                    "version": PROTOCOL_VERSION,
                    "type": "FILE_INFO_RESPONSE",
                    "status": "OK",
                    "filename": self.file_path.name,
                    "file_size": self.file_size,
                    "file_sha256": self.file_sha256,
                },
            )
            return
        if message_type == "GET_CHUNK":
            self._send_chunk(client, header)
            return
        raise RequestError("INVALID_REQUEST", f"unsupported message type: {message_type}")

    def _validate_filename(self, header: dict[str, Any]) -> None:
        if header.get("filename") != self.file_path.name:
            raise RequestError("FILE_NOT_FOUND", "requested file is not configured")

    def _send_chunk(self, client: socket.socket, header: dict[str, Any]) -> None:
        self._validate_filename(header)
        chunk_id = self._required_non_negative_int(header, "chunk_id")
        offset = self._required_non_negative_int(header, "offset")
        length = self._required_non_negative_int(header, "length")
        if length == 0 or length > MAX_CHUNK_SIZE or offset + length > self.file_size:
            raise RequestError("INVALID_RANGE", "requested range is outside the file")

        with self.file_path.open("rb") as source:
            source.seek(offset)
            payload = source.read(length)
        if len(payload) != length:
            raise OSError("configured file changed while being served")

        send_frame(
            client,
            {
                "version": PROTOCOL_VERSION,
                "type": "CHUNK_DATA",
                "status": "OK",
                "chunk_id": chunk_id,
                "offset": offset,
                "length": length,
                "chunk_sha256": sha256(payload).hexdigest(),
            },
            payload,
        )

    @staticmethod
    def _required_non_negative_int(header: dict[str, Any], name: str) -> int:
        value = header.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RequestError("INVALID_REQUEST", f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _send_error(client: socket.socket, code: str, message: str) -> None:
        send_frame(
            client,
            {
                "version": PROTOCOL_VERSION,
                "type": "ERROR",
                "status": "ERROR",
                "code": code,
                "message": message,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve one file over the chunk protocol")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--read-timeout", type=float, default=60.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        server = FileServer(args.file, args.host, args.port, args.read_timeout)
        print(f"Serving {server.file_path.name} on {args.host}:{args.port}")
        server.serve_forever()
    except (OSError, ValueError) as exc:
        print(f"Server error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
