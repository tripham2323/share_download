import json
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from desktop.config_form import ConfigForm
from checkpoint import CheckpointIdentity, CheckpointStore
from file_utils import build_chunks
from server import FileServer


class DesktopConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_save_then_load_preserves_servers_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            form = ConfigForm()
            form.add_server("S1", "127.0.0.1", 5001)
            form.filename_input.setText("config.dat")
            output = Path(directory) / "downloaded.dat"
            form.output_input.setText(str(output))
            path = Path(directory) / "settings.json"

            form.save_file(path)
            loaded = ConfigForm()
            loaded.load_file(path)

            config = loaded.to_client_config()
            self.assertEqual("config.dat", config.filename)
            self.assertEqual(output, config.output)
            self.assertEqual(("S1", "127.0.0.1", 5001),
                             (config.servers[0].name, config.servers[0].host, config.servers[0].port))
            self.assertEqual(1, len(json.loads(path.read_text(encoding="utf-8"))["servers"]))
    def test_advanced_controls_are_collapsed_until_requested(self):
        form = ConfigForm()
        form.show()
        self.addCleanup(form.close)
        self.app.processEvents()
        self.assertTrue(form.advanced_content.isHidden())
        form.advanced_toggle.click()
        self.app.processEvents()
        self.assertTrue(form.advanced_content.isVisible())


    def test_empty_configuration_cannot_start_download(self):
        from desktop.window import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)
        self.assertFalse(window.start_button.isEnabled())
        self.assertEqual("—", window.speed_value.text())
        window.config_form.add_server("S1", "127.0.0.1", 5001)
        self.assertTrue(window.start_button.isEnabled())


    def test_checked_server_offers_verified_resume(self):
        from desktop.window import MainWindow

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.dat"
            source.write_bytes(bytes(range(256)) * 256)
            output = root / "result.dat"
            part = root / "result.dat.part"
            part.write_bytes(source.read_bytes()[:16_384] + bytes(49_152))
            chunks = build_chunks(65_536, 16_384)
            identity = CheckpointIdentity("config.dat", 65_536,
                                          hashlib.sha256(source.read_bytes()).hexdigest(), 16_384)
            store = CheckpointStore(part, identity)
            store.record(chunks[0], hashlib.sha256(source.read_bytes()[:16_384]).hexdigest())
            with part.open("r+b") as target:
                store.flush(target)
            server = FileServer(source, "127.0.0.1", 0)
            server.start()
            self.addCleanup(server.shutdown)
            window = MainWindow()
            window.config_form.add_server("S1", *server.address)
            window.config_form.chunk_size.setValue(16_384)
            window.config_form.output_input.setText(str(output))
            window.show()
            self.addCleanup(window.close)
            window._check_servers()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and window._running:
                self.app.processEvents()
                time.sleep(.01)
            self.app.processEvents()
            self.assertTrue(window.resume_button.isVisible())
            self.assertEqual("25%", window.percent.text())


if __name__ == "__main__":
    unittest.main()
