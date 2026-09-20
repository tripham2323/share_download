import json
import tempfile
import unittest
from pathlib import Path

from client import (
    ClientConfig,
    DownloadError,
    FileMetadata,
    ServerEndpoint,
    load_config,
    query_metadata,
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


if __name__ == "__main__":
    unittest.main()
