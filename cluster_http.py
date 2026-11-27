"""Socket limits for the private cluster HTTP service."""
from __future__ import annotations

import http.client
import io
import socket
import ssl
import threading
import time
from http.server import ThreadingHTTPServer

REQUEST_TIMEOUT = 5.0
TRANSFER_TIMEOUT = 30 * 60.0
MAX_HEADERS_BYTES = 16 * 1024
BLOCK_SIZE = 1024**2


def set_deadline(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Hết thời gian truyền dữ liệu của cụm.")
    sock.settimeout(min(REQUEST_TIMEOUT, remaining))


class DeadlineReader(io.RawIOBase):
    """Bound total header bytes and wall time, including slow trickle reads."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._file = sock.makefile("rb", buffering=0)
        self._budget: int | None = MAX_HEADERS_BYTES
        self._deadline = time.monotonic() + REQUEST_TIMEOUT

    def readable(self) -> bool:
        return True

    def start_body(self) -> None:
        self._budget = None
        self._deadline = time.monotonic() + TRANSFER_TIMEOUT

    def readinto(self, buffer: memoryview) -> int:
        if self._budget == 0:
            raise http.client.LineTooLong("cluster HTTP headers")
        set_deadline(self._sock, self._deadline)
        if self._budget is not None and len(buffer) > self._budget:
            buffer = memoryview(buffer)[:self._budget]
        count = self._file.readinto(buffer)
        if self._budget is not None:
            self._budget -= count
        return count

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            super().close()


class _ClusterResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        self._timed_reader = DeadlineReader(sock)
        # Retain the usual SocketIO lifetime: HTTPConnection closes its own
        # socket when a response declines keep-alive, before the body is read.
        super().__init__(sock, *args, **kwargs)
        self.fp.close()
        self.fp = io.BufferedReader(self._timed_reader)

    def begin(self) -> None:
        super().begin()
        self._timed_reader.start_body()


class ClusterConnection(http.client.HTTPSConnection):
    response_class = _ClusterResponse


class ClusterHttpServer(ThreadingHTTPServer):
    """Limit active connections and interrupt their sockets before shutdown."""

    daemon_threads = False
    request_queue_size = 16

    def __init__(self, address, handler, tls: ssl.SSLContext, service):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self._tls = tls
        self.service = service
        self._slots = threading.BoundedSemaphore(8)
        self._active: set[ssl.SSLSocket] = set()
        self._lock = threading.Lock()
        self._stopping = False
        super().__init__(address, handler)

    def process_request(self, request, address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, address) -> None:
        tls = None
        try:
            with self._lock:
                if self._stopping:
                    return
                request.settimeout(REQUEST_TIMEOUT)
                tls = self._tls.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
                self._active.add(tls)
            tls.do_handshake()
            self.finish_request(tls, address)
        except (OSError, http.client.HTTPException):
            # Disconnects, idle peers, oversized headers and non-TLS clients.
            pass
        except Exception:
            self.handle_error(tls or request, address)
        finally:
            try:
                self.shutdown_request(tls or request)
            finally:
                with self._lock:
                    self._active.discard(tls)
                self._slots.release()

    def interrupt(self) -> None:
        with self._lock:
            self._stopping = True
            for session in self._active:
                try:
                    session.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
