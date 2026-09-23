import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from desktop.config_form import ConfigForm


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



if __name__ == "__main__":
    unittest.main()
