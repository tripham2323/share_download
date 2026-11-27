import socket

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from protocol import PROTOCOL_VERSION, recv_frame, send_frame
from server_app import NodeWindow


def until(predicate):
    for _ in range(150):
        QTest.qWait(15)
        if predicate():
            return
    assert predicate()


def test_gui_start_and_stop_controls_real_listener(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NodeWindow('S2', tmp_path, port=0)
    try:
        window.show()
        window.service_button.click()
        until(lambda: window.node.server is not None and window.service_button.isEnabled())
        address = window.node.server.address
        with socket.create_connection(address, timeout=1) as sock:
            send_frame(sock, {'version': PROTOCOL_VERSION, 'type': 'LIST_FILES'})
            assert recv_frame(sock)[0]['files'] == []
        window.service_button.click()
        until(lambda: window.node.server is None and window.service_button.isEnabled())
        try:
            sock = socket.create_connection(address, timeout=0.2)
        except OSError:
            pass
        else:
            sock.close()
            raise AssertionError('Server accepting clients after operator stopped it')
    finally:
        window.close()
        app.processEvents()
