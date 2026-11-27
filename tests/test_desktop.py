import json
import hashlib
import os
import tempfile
import socket
import time
import unittest
from contextlib import contextmanager
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import Event, Thread
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    raise unittest.SkipTest("PySide6 is not installed")

from desktop.config_form import ConfigForm
from checkpoint import CheckpointIdentity, CheckpointStore
from file_utils import build_chunks
from protocol import recv_frame, send_frame
from server import FileServer


def catalog_file(name, data):
    return {"filename": name, "file_size": len(data), "file_sha256": hashlib.sha256(data).hexdigest()}


@contextmanager
def catalog_server(pages, *, ready=None):
    class Handler(BaseRequestHandler):
        def handle(self):
            try:
                while True:
                    header, _ = recv_frame(self.request, max_payload=0)
                    page = pages.get(header.get("after")) if header["type"] == "LIST_FILES" else None
                    if page is None:
                        send_frame(self.request, {"version": 1, "type": "ERROR", "code": "INVALID_REQUEST"})
                        return
                    if ready is not None and not ready.wait(.5):
                        return
                    files, next_after = page
                    send_frame(self.request, {"version": 1, "type": "FILE_LIST", "status": "OK",
                                              "files": files, "next_after": next_after})
            except OSError:
                pass

    with ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            thread.join()


class DesktopConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_idle(self, window):
        deadline = time.monotonic() + 3
        while window._running and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(window._running, "The background task did not finish")

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

    def test_catalog_paginates_without_blocking_qt_and_selects_by_keyboard(self):
        from desktop.window import MainWindow

        ready = Event()
        pages = {
            None: ([catalog_file("bao-cao.txt", b"abc")], "bao-cao.txt"),
            "bao-cao.txt": ([catalog_file("tong-hop.txt", b"12345")], None),
        }
        with catalog_server(pages, ready=ready) as address:
            window = MainWindow()
            self.addCleanup(window.close)
            window.config_form.add_server("S1", *address)
            window.config_form.filename_input.clear()
            window.config_form.output_input.setText("downloads/selected.txt")
            window.show()

            QTimer.singleShot(0, ready.set)
            window.catalog_button.click()
            self.wait_idle(window)
            dialog = window._catalog_dialog
            self.assertEqual("tong-hop.txt", dialog.table.item(1, 0).text())
            dialog.table.setCurrentCell(0, 0)
            dialog.table.setFocus()
            QTest.keyClick(dialog.table, Qt.Key.Key_Down)
            QTest.keyClick(dialog.table, Qt.Key.Key_Return)

            self.assertFalse(dialog.isVisible())
            self.assertEqual("tong-hop.txt", window.config_form.filename_input.text())
            self.assertEqual("downloads/selected.txt", window.config_form.output_input.text())
            self.assertTrue(window.start_button.isEnabled())

    def test_catalog_keeps_empty_distinct_from_offline_and_manual_is_available(self):
        from desktop.window import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)
        window.show()
        with catalog_server({None: ([], None)}) as address:
            window.config_form.add_server("S1", *address)
            window.catalog_button.click()
            self.wait_idle(window)
            dialog = window._catalog_dialog
            self.assertIn("chưa có tệp", dialog.status.text().lower())
            dialog.reject()

        window.catalog_button.click()
        self.wait_idle(window)
        self.assertIn("không kết nối", dialog.status.text().lower())
        self.assertFalse(dialog.choose_button.isEnabled())
        dialog.reject()
        window.config_form.filename_input.setText("manual.dat")
        self.assertTrue(window.start_button.isEnabled())

    def test_catalog_blocks_selection_when_advertised_versions_tie(self):
        from desktop.window import MainWindow

        with (catalog_server({None: ([catalog_file("du-lieu.dat", b"a")], None)}) as first,
              catalog_server({None: ([catalog_file("du-lieu.dat", b"b")], None)}) as second):
            window = MainWindow()
            self.addCleanup(window.close)
            window.show()
            window.config_form.add_server("S1", *first)
            window.config_form.add_server("S2", *second)
            window.catalog_button.click()
            self.wait_idle(window)
            dialog = window._catalog_dialog
            dialog.table.setCurrentCell(0, 0)
            self.assertEqual("du-lieu.dat", dialog.table.item(0, 0).text())
            self.assertIn("không khớp", dialog.table.item(0, 3).text().lower())
            self.assertIn("2 nguồn", dialog.table.item(0, 3).text())
            self.assertFalse(dialog.choose_button.isEnabled())
            QTest.keyClick(dialog.table, Qt.Key.Key_Return)
            self.assertEqual("config.dat", window.config_form.filename_input.text())
            dialog.reject()

    def test_catalog_offers_strict_majority_and_shows_divergent_count(self):
        from desktop.window import MainWindow

        majority = {None: ([catalog_file("du-lieu.dat", b"same")], None)}
        minority = {None: ([catalog_file("du-lieu.dat", b"x")], None)}
        with (catalog_server(minority) as first, catalog_server(majority) as second,
              catalog_server(majority) as third):
            window = MainWindow()
            self.addCleanup(window.close)
            window.show()
            for number, address in enumerate((first, second, third), 1):
                window.config_form.add_server(f"S{number}", *address)
            window.catalog_button.click()
            self.wait_idle(window)
            dialog = window._catalog_dialog
            dialog.table.setCurrentCell(0, 0)
            self.assertEqual("4 B", dialog.table.item(0, 1).text())
            self.assertIn("2/3", dialog.table.item(0, 3).text())
            self.assertIn("1 khác", dialog.table.item(0, 3).text())
            self.assertTrue(dialog.choose_button.isEnabled())
            dialog.choose_button.click()
            self.assertEqual("du-lieu.dat", window.config_form.to_client_config().filename)


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
            self.wait_idle(window)
            self.assertTrue(window.start_button.isEnabled())
            self.assertEqual("Tiếp tục tải", window.start_button.text())
            self.assertEqual("25%", window.percent.text())
            window.start_button.click()
            self.wait_idle(window)
            self.assertEqual(source.read_bytes(), output.read_bytes())
            self.assertEqual("49,152 B", window.server_table.item(0, 2).text())
            self.assertEqual("3", window.server_table.item(0, 3).text())
            self.assertEqual("100.0%", window.server_table.item(0, 4).text())
            self.assertIn("16,384 B từ checkpoint", window.summary.details.text())
            window.config_form.filename_input.setText("khac.dat")
            self.assertTrue(window.summary.isVisible())
            self.assertIn(str(output), window.summary.path.text())
            self.assertEqual("—", window.percent.text())


    def test_start_after_check_downloads_from_two_available_servers(self):
        from desktop.window import MainWindow

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.dat"
            data = bytes(range(256)) * 256
            source.write_bytes(data)
            servers = [FileServer(source, "127.0.0.1", 0) for _ in range(2)]
            for server in servers:
                server.start()
                self.addCleanup(server.shutdown)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                unavailable_port = probe.getsockname()[1]
            window = MainWindow()
            self.addCleanup(window.close)
            window.config_form.add_server("S1", "127.0.0.1", unavailable_port)
            for number, server in enumerate(servers, 2):
                window.config_form.add_server(f"S{number}", *server.address)
            output = root / "received.dat"
            window.config_form.output_input.setText(str(output))
            window.show()
            window._check_servers()
            self.wait_idle(window)

            window.start_button.click()
            self.wait_idle(window)
            self.assertTrue(output.exists(), "Bắt đầu tải không khởi chạy phiên sau khi kiểm tra")
            self.assertEqual(data, output.read_bytes())
            self.assertEqual("Đã xác minh", window.integrity_value.text())
            self.assertEqual("0 B", window.server_table.item(0, 2).text())
            self.assertEqual("0", window.server_table.item(0, 3).text())
            self.assertEqual("0.0%", window.server_table.item(0, 4).text())

if __name__ == "__main__":
    unittest.main()
