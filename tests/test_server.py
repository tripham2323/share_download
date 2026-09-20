import hashlib
import socket
import tempfile
import unittest
from pathlib import Path

from protocol import PROTOCOL_VERSION, recv_frame, send_frame
from server import FileServer


class FileServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.content = b"0123456789abcdef"
        self.path = Path(self.directory.name, "config.dat")
        self.path.write_bytes(self.content)
        self.server = FileServer(self.path, "127.0.0.1", 0, read_timeout=1)
        self.server.start()
        self.addCleanup(self.server.shutdown)

    def connect(self):
        sock = socket.create_connection(self.server.address, timeout=1)
        sock.settimeout(1)
        self.addCleanup(sock.close)
        return sock

    def test_returns_file_metadata(self):
        sock = self.connect()
        send_frame(
            sock,
            {
                "version": PROTOCOL_VERSION,
                "type": "FILE_INFO_REQUEST",
                "filename": "config.dat",
            },
        )

        header, payload = recv_frame(sock)

        self.assertEqual(b"", payload)
        self.assertEqual("FILE_INFO_RESPONSE", header["type"])
        self.assertEqual("OK", header["status"])
        self.assertEqual(len(self.content), header["file_size"])
        self.assertEqual(hashlib.sha256(self.content).hexdigest(), header["file_sha256"])

    def test_returns_requested_chunk_and_hash(self):
        sock = self.connect()
        send_frame(
            sock,
            {
                "version": PROTOCOL_VERSION,
                "type": "GET_CHUNK",
                "filename": "config.dat",
                "chunk_id": 1,
                "offset": 4,
                "length": 4,
            },
        )

        header, payload = recv_frame(sock)

        self.assertEqual("CHUNK_DATA", header["type"])
        self.assertEqual(1, header["chunk_id"])
        self.assertEqual(4, header["offset"])
        self.assertEqual(4, header["length"])
        self.assertEqual(b"4567", payload)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), header["chunk_sha256"])

    def test_rejects_range_beyond_file(self):
        sock = self.connect()
        send_frame(
            sock,
            {
                "version": PROTOCOL_VERSION,
                "type": "GET_CHUNK",
                "filename": "config.dat",
                "chunk_id": 7,
                "offset": 14,
                "length": 4,
            },
        )

        header, payload = recv_frame(sock)

        self.assertEqual(b"", payload)
        self.assertEqual("ERROR", header["type"])
        self.assertEqual("INVALID_RANGE", header["code"])

    def test_rejects_unconfigured_filename(self):
        sock = self.connect()
        send_frame(
            sock,
            {
                "version": PROTOCOL_VERSION,
                "type": "FILE_INFO_REQUEST",
                "filename": "../secret.dat",
            },
        )

        header, _ = recv_frame(sock)

        self.assertEqual("ERROR", header["type"])
        self.assertEqual("FILE_NOT_FOUND", header["code"])


if __name__ == "__main__":
    unittest.main()
