import hashlib
import json
import socket
import threading
import tempfile
import unittest
from pathlib import Path

from client import (
    ClientConfig,
    DownloadCancelled,
    DownloadController,
    DownloadError,
    FileMetadata,
    ServerEndpoint,
    download,
    load_config,
    parse_config,
    select_consistent_servers,
)
from server import FileServer


class MetadataConsensusTests(unittest.TestCase):
    def setUp(self):
        self.s1 = ServerEndpoint("S1", "127.0.0.1", 5001)
        self.s2 = ServerEndpoint("S2", "127.0.0.1", 5002)
        self.s3 = ServerEndpoint("S3", "127.0.0.1", 5003)
        self.a = FileMetadata("config.dat", 10, "a" * 64)
        self.b = FileMetadata("config.dat", 10, "b" * 64)
        self.c = FileMetadata("config.dat", 10, "c" * 64)

    def test_two_matching_servers_win_over_one_mismatch(self):
        metadata, endpoints = select_consistent_servers(
            [(self.s1, self.a), (self.s2, self.a), (self.s3, self.b)]
        )
        self.assertEqual(self.a, metadata)
        self.assertEqual([self.s1, self.s2], endpoints)

    def test_two_different_servers_are_ambiguous(self):
        with self.assertRaisesRegex(DownloadError, "no metadata majority"):
            select_consistent_servers([(self.s1, self.a), (self.s2, self.b)])

    def test_three_different_servers_are_ambiguous(self):
        with self.assertRaisesRegex(DownloadError, "no metadata majority"):
            select_consistent_servers(
                [(self.s1, self.a), (self.s2, self.b), (self.s3, self.c)]
            )

    def test_one_server_is_usable(self):
        metadata, endpoints = select_consistent_servers([(self.s1, self.a)])
        self.assertEqual(self.a, metadata)
        self.assertEqual([self.s1], endpoints)

    def test_no_server_fails(self):
        with self.assertRaisesRegex(DownloadError, "no valid metadata"):
            select_consistent_servers([])


class ClientConfigTests(unittest.TestCase):
    def write_config(self, directory: str, overrides=None) -> Path:
        data = {
            "servers": [
                {"name": "S1", "host": "127.0.0.1", "port": 5001},
                {"name": "S2", "host": "127.0.0.1", "port": 5002},
            ],
            "filename": "config.dat",
            "output": "downloads/config.dat",
            "chunk_size": 262144,
            "connect_timeout": 5,
            "read_timeout": 60,
            "reconnect_attempts": 3,
        }
        if overrides:
            data.update(overrides)
        path = Path(directory, "client.json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_loads_config_and_resolves_relative_output(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(self.write_config(directory))
            self.assertEqual(2, len(config.servers))
            self.assertEqual(Path(directory, "downloads", "config.dat"), config.output)
            self.assertEqual(262144, config.chunk_size)

    def test_parses_in_memory_form_like_saved_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(load_config(path), parse_config(raw, path.parent))

    def test_rejects_duplicate_server_names(self):
        with tempfile.TemporaryDirectory() as directory:
            servers = [
                {"name": "S1", "host": "127.0.0.1", "port": 1},
                {"name": "S1", "host": "127.0.0.2", "port": 2},
            ]
            with self.assertRaisesRegex(ValueError, "unique"):
                load_config(self.write_config(directory, {"servers": servers}))

    def test_rejects_invalid_numeric_settings(self):
        invalid = [
            {"chunk_size": 0},
            {"chunk_size": 1_048_577},
            {"connect_timeout": 0},
            {"read_timeout": -1},
            {"reconnect_attempts": -1},
        ]
        with tempfile.TemporaryDirectory() as directory:
            for values in invalid:
                with self.subTest(values=values):
                    with self.assertRaises(ValueError):
                        load_config(self.write_config(directory, values))


class MetadataQueryTests(unittest.TestCase):
    def test_queries_real_server(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "config.dat")
            source.write_bytes(b"network-file")
            server = FileServer(source, "127.0.0.1", 0, read_timeout=1)
            server.start()
            self.addCleanup(server.shutdown)
            endpoint = ServerEndpoint("S1", *server.address)
            config = ClientConfig(
                servers=(endpoint,),
                filename="config.dat",
                output=Path(directory, "out.dat"),
                connect_timeout=1,
                read_timeout=1,
            )

            returned_endpoint, metadata = query_metadata(endpoint, config)

            self.assertEqual(endpoint, returned_endpoint)
            self.assertEqual("config.dat", metadata.filename)
            self.assertEqual(len(b"network-file"), metadata.file_size)
            self.assertEqual(64, len(metadata.file_sha256))


class DisconnectOnChunkServer(FileServer):
    def _send_chunk(self, client, header):
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()


class StallSecondChunkServer(FileServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocked = threading.Event()
        self.release = threading.Event()

    def _send_chunk(self, client, header):
        if header["chunk_id"] == 1:
            self.blocked.set()
            self.release.wait(timeout=10)
            return
        super()._send_chunk(client, header)


class ParallelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_bytes = bytes(range(256)) * 2048
        self.source = self.root / "config.dat"
        self.source.write_bytes(self.source_bytes)
        self.servers = []

    def start_server(self, name, source=None, server_type=FileServer):
        server = server_type(source or self.source, "127.0.0.1", 0, read_timeout=1)
        server.start()
        self.servers.append(server)
        self.addCleanup(server.shutdown)
        return ServerEndpoint(name, *server.address)

    def config_for(self, endpoints):
        return ClientConfig(
            servers=tuple(endpoints),
            filename="config.dat",
            output=self.root / "downloaded.dat",
            chunk_size=16_384,
            connect_timeout=1,
            read_timeout=2,
            reconnect_attempts=1,
        )

    def test_downloads_verified_file_from_multiple_servers(self):
        endpoints = [
            self.start_server("S1"),
            self.start_server("S2"),
            self.start_server("S3"),
        ]

        result = download(self.config_for(endpoints))

        self.assertEqual(hashlib.sha256(self.source_bytes).hexdigest(), result.file_sha256)
        self.assertEqual(self.source_bytes, result.output.read_bytes())
        self.assertGreaterEqual(
            sum(count > 0 for count in result.per_server_chunks.values()),
            2,
        )
        self.assertEqual(len(self.source_bytes), sum(result.per_server_bytes.values()))
        self.assertTrue(all(speed >= 0 for speed in result.per_server_kbps.values()))

    def test_emits_structured_events(self):
        endpoints = [self.start_server("S1"), self.start_server("S2")]
        events = []

        result = download(self.config_for(endpoints), event_callback=events.append)

        kinds = [event.kind for event in events]
        self.assertLess(kinds.index("metadata"), kinds.index("chunk"))
        self.assertLess(kinds.index("chunk"), kinds.index("verifying"))
        self.assertLess(kinds.index("verifying"), kinds.index("completed"))
        self.assertEqual(
            len(self.source_bytes),
            sum(event.bytes_delta for event in events if event.kind == "chunk"),
        )
        self.assertEqual(self.source_bytes, result.output.read_bytes())

    def test_excludes_server_with_different_file(self):
        different = self.root / "different" / "config.dat"
        different.parent.mkdir()
        different.write_bytes(b"different-content")
        endpoints = [
            self.start_server("S1"),
            self.start_server("S2"),
            self.start_server("S3", source=different),
        ]

        result = download(self.config_for(endpoints))

        self.assertEqual({"S1", "S2"}, set(result.per_server_chunks))
        self.assertEqual(("S3",), result.unused_servers)
        self.assertEqual(self.source_bytes, result.output.read_bytes())

    def test_reassigns_chunk_when_server_disconnects(self):
        endpoints = [
            self.start_server("S1"),
            self.start_server("S2"),
            self.start_server("S3", server_type=DisconnectOnChunkServer),
        ]

        result = download(self.config_for(endpoints))

        self.assertEqual(self.source_bytes, result.output.read_bytes())
        self.assertEqual(0, result.per_server_chunks["S3"])
    def test_reports_failed_server_after_reassignment(self):
        endpoints = [
            self.start_server("S1"),
            self.start_server("S2"),
            self.start_server("S3", server_type=DisconnectOnChunkServer),
        ]
        events = []

        result = download(self.config_for(endpoints), event_callback=events.append)

        self.assertEqual(self.source_bytes, result.output.read_bytes())
        self.assertTrue(any(event.kind == "chunk_requeued" and event.server == "S3"
                            for event in events))
        self.assertTrue(any(event.kind == "server_failed" and event.server == "S3"
                            for event in events))



    def test_cancel_during_blocked_read(self):
        server = StallSecondChunkServer(self.source, "127.0.0.1", 0, read_timeout=2)
        server.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.release.set)
        endpoint = ServerEndpoint("S1", *server.address)
        controller = DownloadController()
        outcome = {}

        def run():
            try:
                download(self.config_for([endpoint]), controller=controller)
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(server.blocked.wait(timeout=3))
        controller.cancel()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get("error"), DownloadCancelled)
        self.assertFalse((self.root / "downloaded.dat").exists())
        self.assertTrue((self.root / "downloaded.dat.part").exists())


    def test_cancel_during_verification_does_not_publish(self):
        endpoint = self.start_server("S1")
        controller = DownloadController()

        def cancel_on_verifying(event):
            if event.kind == "verifying":
                controller.cancel()

        with self.assertRaises(DownloadCancelled):
            download(self.config_for([endpoint]), controller=controller,
                     event_callback=cancel_on_verifying)
        self.assertFalse((self.root / "downloaded.dat").exists())


if __name__ == "__main__":
    unittest.main()
