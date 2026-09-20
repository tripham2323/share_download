import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server import FileServer

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ClientCliIntegrationTests(unittest.TestCase):
    def test_cli_downloads_and_verifies_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.dat"
            source.write_bytes(bytes(range(256)) * 2048)
            servers = [
                FileServer(source, "127.0.0.1", 0, read_timeout=2)
                for _ in range(3)
            ]
            for server in servers:
                server.start()
                self.addCleanup(server.shutdown)

            output = root / "downloads" / "config.dat"
            config_path = root / "client.json"
            config_path.write_text(
                json.dumps(
                    {
                        "servers": [
                            {
                                "name": f"S{index}",
                                "host": server.address[0],
                                "port": server.address[1],
                            }
                            for index, server in enumerate(servers, start=1)
                        ],
                        "filename": "config.dat",
                        "output": str(output),
                        "chunk_size": 16384,
                        "connect_timeout": 1,
                        "read_timeout": 2,
                        "reconnect_attempts": 1,
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, "client.py", "--config", str(config_path)],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Progress:", completed.stdout)
            self.assertIn("SHA-256 verified", completed.stdout)
            self.assertIn("Saved:", completed.stdout)
            self.assertIn("S1:", completed.stdout)
            self.assertEqual(source.read_bytes(), output.read_bytes())

    def test_cli_reports_unavailable_servers_without_publishing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unavailable_ports = [self.reserve_unused_port() for _ in range(3)]
            output = root / "config.dat"
            config_path = root / "client.json"
            config_path.write_text(
                json.dumps(
                    {
                        "servers": [
                            {"name": f"S{index}", "host": "127.0.0.1", "port": port}
                            for index, port in enumerate(unavailable_ports, start=1)
                        ],
                        "filename": "config.dat",
                        "output": str(output),
                        "connect_timeout": 0.2,
                        "read_timeout": 0.2,
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, "client.py", "--config", str(config_path)],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("Download error:", completed.stderr)
            self.assertFalse(output.exists())

    @staticmethod
    def reserve_unused_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
