import socket
import struct
import unittest

from protocol import MAX_HEADER_SIZE, ProtocolError, recv_exact, recv_frame, send_frame


class FragmentingSocket:
    def __init__(self, sock: socket.socket, max_read: int):
        self._sock = sock
        self._max_read = max_read

    def recv(self, size: int) -> bytes:
        return self._sock.recv(min(size, self._max_read))


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

    def test_recv_exact_handles_fragmented_tcp_reads(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.sendall(b"abcdefgh")

        received = recv_exact(FragmentingSocket(right, max_read=2), 8)

        self.assertEqual(b"abcdefgh", received)

    def test_rejects_payload_larger_than_limit(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        send_frame(left, {"version": 1, "type": "CHUNK_DATA"}, b"12345")

        with self.assertRaisesRegex(ProtocolError, "payload length"):
            recv_frame(right, max_payload=4)

    def test_rejects_oversized_header_before_reading_it(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.sendall(struct.pack("!I", MAX_HEADER_SIZE + 1))

        with self.assertRaisesRegex(ProtocolError, "header length"):
            recv_frame(right)

    def test_rejects_connection_closed_mid_frame(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        left.sendall(b"ab")
        left.close()

        with self.assertRaises(ConnectionError):
            recv_exact(right, 4)


if __name__ == "__main__":
    unittest.main()
