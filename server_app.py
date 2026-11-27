"""Operator desktop for one server, or three isolated servers on loopback."""
from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import sys

from PySide6.QtCore import QLockFile, Qt, QTimer
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from client import parse_config
from cluster_sync import ClusterService, ClusterSettings
from desktop.theme import STYLESHEET
from file_store import FileStore
from runtime_paths import APP_DIR
from server import FileServer

NODES = ('S1', 'S2', 'S3')


def _private_file(path: Path, text: str) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


class Node:
    def __init__(self, name: str, directory: Path):
        self.name = name
        self.directory = directory
        self.store = FileStore(directory / 'files')
        self.server: FileServer | None = None
        self.cluster: ClusterService | None = None
        self.credentials = directory / 'cluster.private.json'

    def install(self, settings: ClusterSettings) -> None:
        if settings.node_id != self.name:
            raise ValueError(f'Hồ sơ dành cho {settings.node_id}, không phải {self.name}.')
        if self.credentials.exists():
            raise ValueError('Máy này đã tham gia cụm. Hãy giữ nguyên danh tính và hồ sơ đã cấp.')
        _private_file(self.credentials, settings.to_json())

    def start(self, host: str, port: int) -> tuple[str, int]:
        listener = FileServer(self.store, host, port)
        listener.start()
        try:
            if self.credentials.is_file():
                settings = ClusterSettings.from_json(self.credentials.read_text(encoding='utf-8'))
                if settings.node_id != self.name:
                    raise ValueError('Danh tính trong hồ sơ đồng bộ không khớp máy này.')
                cluster = ClusterService(settings, self.store, self.directory / 'sync')
                cluster.start()
                self.cluster = cluster
        except Exception:
            listener.shutdown()
            raise
        self.server = listener
        return listener.address

    def stop(self) -> None:
        try:
            if self.cluster is not None:
                cluster, self.cluster = self.cluster, None
                cluster.close()
        finally:
            if self.server is not None:
                server, self.server = self.server, None
                server.shutdown()

    def add(self, path: Path, name: str, replicate: bool) -> None:
        if replicate:
            if self.cluster is None:
                raise ValueError('Cần bật phục vụ và thiết lập cụm trước khi phát hành.')
            self.cluster.publish(path, name=name)
        else:
            with path.open('rb') as stream:
                self.store.put(name, stream)

    def export_enrollment(self, node: str, path: Path) -> None:
        settings = ClusterSettings.from_json(self.credentials.read_text(encoding='utf-8'))
        with path.open('x', encoding='utf-8') as file:
            file.write(settings.export_enrollment(node))


def _label(text: str, role: str = '') -> QLabel:
    label = QLabel(text)
    label.setObjectName(role)
    label.setWordWrap(True)
    return label


def _lan_addresses() -> list[str]:
    return sorted({address.toString() for address in QNetworkInterface.allAddresses()
                   if '.' in address.toString() and ':' not in address.toString() and not address.isLoopback()})


class NodeWindow(QWidget):
    def __init__(self, name: str, directory: Path, *, port: int = 5000, demo: bool = False):
        super().__init__()
        self.node = Node(name, directory)
        self.demo = demo
        self.setWindowTitle(f'ShareDownload · {name}')
        self.resize(1050, 680)
        self.setMinimumSize(680, 460)
        self._snapshot = None
        self._rendered = None
        self._job: Future | None = None
        self._queued = None
        self._on_result = None
        self._action = ''
        self._closing = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'node-{name}')
        self._network = directory / 'listener.json'
        settings = json.loads(self._network.read_text(encoding='utf-8')) if self._network.exists() else {}

        content = QVBoxLayout(self)
        content.setContentsMargins(24, 20, 24, 20)
        content.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(_label(f'ShareDownload · {name} · Kho tệp', 'pageTitle'), 1)
        self.add_button = QPushButton('Thêm tệp…')
        self.add_button.setObjectName('primary')
        self.add_button.clicked.connect(self._add)
        header.addWidget(self.add_button)
        content.addLayout(header)
        self.notice = _label('Chưa bật phục vụ.', 'muted')
        content.addWidget(self.notice)

        endpoint = QHBoxLayout()
        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.addItems(['127.0.0.1', *_lan_addresses(), '0.0.0.0'])
        self.host.setCurrentText(settings.get('host', '127.0.0.1'))
        self.host.setAccessibleName('Địa chỉ lắng nghe')
        self.port = QSpinBox()
        self.port.setAccessibleName('Cổng tải')
        self.port.setRange(0 if port == 0 else 1, 65535)
        self.port.setValue(settings.get('port', port))
        self.service_button = QPushButton('Bật phục vụ')
        self.service_button.clicked.connect(self._toggle)
        self.copy_button = QPushButton('Sao chép địa chỉ')
        self.copy_button.clicked.connect(self._copy_endpoint)
        endpoint.addWidget(QLabel('Lắng nghe'))
        endpoint.addWidget(self.host, 1)
        endpoint.addWidget(QLabel('Cổng TCP'))
        endpoint.addWidget(self.port)
        endpoint.addWidget(self.service_button)
        endpoint.addWidget(self.copy_button)
        content.addLayout(endpoint)

        self.sync_status = _label('Chưa thiết lập đồng bộ.' if not self.node.credentials.exists()
                                  else 'Đã thiết lập. Bật phục vụ để kết nối cụm.', 'muted')
        content.addWidget(self.sync_status)
        self.files = QTableWidget(0, 6)
        self.files.setHorizontalHeaderLabels(('Tệp đã công bố', 'Dung lượng', 'SHA-256', *NODES))
        self.files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.files.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.files.setShowGrid(False)
        self.files.verticalHeader().setVisible(False)
        self.files.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.verticalHeader().setDefaultSectionSize(34)
        self.files.itemDoubleClicked.connect(self._copy_hash)
        content.addWidget(self.files, 3)
        self.empty = _label('Kho trống. Chọn Thêm tệp để bắt đầu.', 'muted')
        content.addWidget(self.empty)

        buttons = QHBoxLayout()
        self.setup_button = QPushButton('Tạo cụm…' if name == 'S1' else 'Nhập lời mời…')
        self.setup_button.clicked.connect(self._setup)
        self.invite_button = QPushButton('Xuất lời mời riêng…')
        self.invite_button.clicked.connect(self._invite)
        self.invite_button.setVisible(name == 'S1' and not demo)
        self.profile_button = QPushButton('Hồ sơ địa chỉ LAN…')
        self.profile_button.clicked.connect(self._profile)
        self.setup_button.setVisible(not demo)
        self.profile_button.setVisible(not demo)
        buttons.addWidget(self.setup_button)
        buttons.addWidget(self.invite_button)
        buttons.addWidget(self.profile_button)
        buttons.addStretch()
        content.addLayout(buttons)
        content.addWidget(_label('Nhật ký vận hành', 'fileTitle'))
        self.log = QListWidget()
        self.log.setAccessibleName('Nhật ký vận hành')
        content.addWidget(self.log, 1)
        self.timer = QTimer(self, interval=65)
        self.timer.timeout.connect(self._finish)
        self.timer.start()
        self.sync_timer = QTimer(self, interval=2000)
        self.sync_timer.timeout.connect(self._refresh)
        self.sync_timer.start()
        self._render()
        self._enable()

    def _record(self, text: str) -> None:
        self.log.insertItem(0, f'{datetime.now():%H:%M:%S}  {text}')
        if self.log.count() > 100:
            self.log.takeItem(100)

    def _submit(self, action, work, on_result=None):
        if self._job is not None:
            if action != 'Đồng bộ' and self._action == 'Đồng bộ' and self._queued is None:
                self._queued = action, work, on_result
                self.notice.setText(f'{action} · đang đợi tác vụ đồng bộ hiện tại…')
                self._enable()
            return
        self._action, self._on_result = action, on_result
        self._job = self._executor.submit(work)
        if action != 'Đồng bộ':
            self.notice.setText(f'{action}…')
            self._enable()

    def _finish(self):
        if self._job is None or not self._job.done():
            return
        future, self._job = self._job, None
        try:
            result = future.result()
            if self._on_result is not None:
                self._on_result(result)
        except Exception as exc:
            if self._action == 'Đồng bộ':
                text = f'Không cập nhật được từ S1. Bản sao đã xác minh vẫn có thể tải. {exc}'
                if self.sync_status.text() != text:
                    self.sync_status.setText(text)
                    self._record(text)
            else:
                text = f'{self._action} thất bại: {exc}'
                self.notice.setText(text)
                self._record(text)
        if self._closing:
            self._queued = None
            if self.node.server is not None:
                self._submit('Dừng phục vụ', self.node.stop)
            else:
                self.close()
        elif self._queued is not None:
            queued, self._queued = self._queued, None
            self._submit(*queued)
        else:
            self._enable()

    def _enable(self):
        running = self.node.server is not None
        free = (self._job is None or self._action == 'Đồng bộ') and self._queued is None and not self._closing
        self.add_button.setEnabled(free)
        self.service_button.setText('Dừng phục vụ' if running else 'Bật phục vụ')
        self.service_button.setEnabled(free)
        for widget in (self.host, self.port, self.profile_button):
            widget.setEnabled(free and not running and not self.demo)
        self.setup_button.setEnabled(free and not running and not self.node.credentials.exists())
        self.invite_button.setEnabled(free and self.node.credentials.exists())
        self.copy_button.setEnabled(running)

    def _toggle(self):
        if self.node.server is not None:
            self._submit('Dừng phục vụ', self.node.stop, lambda _: self._stopped())
        else:
            host, port = self.host.currentText().strip(), self.port.value()
            def start():
                address = self.node.start(host, port)
                try:
                    _private_file(self._network, json.dumps({'host': host, 'port': port}))
                except OSError:
                    self.node.stop()
                    raise
                return address
            self._submit('Bật phục vụ', start, self._started)

    def _started(self, address):
        host, port = address
        status = f'Đang phục vụ · {host}:{port}'
        self.notice.setText(status)
        self._record(status)
        if self.node.cluster is not None:
            self.sync_status.setText('Đang chờ cập nhật trạng thái cụm…')
        if self.demo and self.node.name == 'S1' and self.node.store.get('config.dat') is None:
            def seed():
                sample = self.node.directory / 'config.dat'
                with sample.open('wb') as output:
                    for index in range(40):
                        output.write(bytes([index]) * (256 * 1024))
                try:
                    self.node.add(sample, 'config.dat', True)
                    return self.node.store.list()
                finally:
                    sample.unlink(missing_ok=True)
            def seeded(entries):
                self._render(entries)
                self.notice.setText(status)
                self._record('Đã phát hành tệp mẫu config.dat.')
            self._submit('Tạo tệp demo', seed, seeded)

    def _stopped(self):
        self.notice.setText('Đã dừng phục vụ; tệp vẫn được lưu trên máy này.')
        self._record(self.notice.text())
        if self.node.credentials.exists():
            self.sync_status.setText('Đồng bộ tạm dừng. Trạng thái hàng bên dưới là lần xác nhận gần nhất.')

    def _copy_endpoint(self):
        if self.node.server is None:
            return
        host, port = self.node.server.address
        if host == '0.0.0.0':
            addresses = _lan_addresses()
            if not addresses:
                self.notice.setText('Chưa tìm thấy địa chỉ IPv4 LAN.')
                return
            host, ok = QInputDialog.getItem(self, 'Địa chỉ LAN', 'Chọn IPv4 mà C1 có thể kết nối:', addresses, 0, False)
            if not ok:
                return
        QApplication.clipboard().setText(f'{host}:{port}')
        self._record(f'Đã sao chép {host}:{port}')

    def _copy_hash(self, item):
        if item.column() == 2:
            QApplication.clipboard().setText(item.toolTip())
            self._record('Đã sao chép SHA-256 đầy đủ.')

    def _add(self):
        selected, _ = QFileDialog.getOpenFileName(self, 'Chọn tệp để công bố')
        if not selected:
            return
        path = Path(selected)
        dialog = QDialog(self)
        dialog.setWindowTitle('Thêm tệp')
        form = QFormLayout(dialog)
        name = QLineEdit(path.name)
        scope = QComboBox()
        scope.addItem('Chỉ máy này', False)
        if self.node.cluster is not None:
            scope.addItem('Phát hành cho cả cụm', True)
        form.addRow('Tên tệp cung cấp', name)
        form.addRow('Phạm vi', scope)
        form.addRow(_label('Tối đa 4 GiB. Không thay thế tệp khác nội dung đã công bố.'))
        if self.node.cluster is None:
            form.addRow(_label('Để phát hành cả cụm, thiết lập đồng bộ rồi bật phục vụ.'))
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        form.addRow(actions)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            filename, cluster = name.text(), scope.currentData()
            def add():
                self.node.add(path, filename, cluster)
                return self.node.store.list()
            def done(entries):
                self.notice.setText(f'{filename} · ' + ('S1 đã nhận; đang chờ các bản sao.' if cluster else 'Đã lưu trên máy này.'))
                self._record(self.notice.text())
                self._render(entries)
                self._refresh()
            self._submit('Phát hành' if cluster else 'Lưu tệp', add, done)

    def _refresh(self):
        if self.node.cluster is None or self._closing:
            return
        def query():
            return self.node.cluster.refresh(), self.node.store.list()
        def updated(result):
            self._snapshot, entries = result
            count = sum(len(release.acknowledged) == len(NODES) for release in self._snapshot.releases)
            self.sync_status.setText(f'{count}/{len(self._snapshot.releases)} tệp đã đồng bộ 3/3 · cập nhật {datetime.now():%H:%M:%S}')
            self._render(entries)
        self._submit('Đồng bộ', query, updated)

    def _render(self, entries=None):
        if entries is None:
            entries = self.node.store.list()
        visible = (entries, self._snapshot)
        if self._rendered == visible:
            return
        self._rendered = visible
        releases = {r.filename: r for r in self._snapshot.releases} if self._snapshot else {}
        local = {e.name: e for e in entries}
        conflicts = self._snapshot.conflicts if self._snapshot else {}
        self.files.setRowCount(len(local.keys() | releases.keys()))
        for row, name in enumerate(sorted(local.keys() | releases.keys(), key=str.casefold)):
            entry, release = local.get(name), releases.get(name)
            digest = release.file_sha256 if release else entry.digest
            size = release.file_size if release else entry.size
            self.files.setItem(row, 0, QTableWidgetItem(name))
            size_item = QTableWidgetItem(f'{size:,} B')
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.files.setItem(row, 1, size_item)
            hash_item = QTableWidgetItem(digest[:12] + '…')
            hash_item.setToolTip(digest)
            self.files.setItem(row, 2, hash_item)
            for column, node in enumerate(NODES, 3):
                if release is None:
                    text = 'Chỉ máy này' if self.node.name == node else '—'
                elif node == self.node.name and name in conflicts:
                    text = 'Xung đột'
                else:
                    text = 'Đã xác minh' if node in release.acknowledged else 'Chưa xác nhận'
                cell = QTableWidgetItem(text)
                if text == 'Xung đột':
                    cell.setToolTip(conflicts[name])
                self.files.setItem(row, column, cell)
        self.empty.setVisible(self.files.rowCount() == 0)

    def _setup(self):
        if self.node.name == 'S1':
            address = self.host.currentText()
            if address == '0.0.0.0':
                address = next(iter(_lan_addresses()), '')
            host, ok = QInputDialog.getText(self, 'Tạo cụm', 'Địa chỉ IPv4 hoặc DNS cố định mà S2/S3 dùng đến S1:', text=address)
            if not ok or not host.strip():
                return
            port, ok = QInputDialog.getInt(self, 'Cổng đồng bộ', 'Cổng HTTPS dành riêng cho đồng bộ:', 5100, 1, 65535)
            if not ok:
                return
            if port == self.port.value():
                self.notice.setText('Cổng tải và cổng đồng bộ phải khác nhau.')
                return
            bind = '127.0.0.1' if host.strip() == '127.0.0.1' else '0.0.0.0'
            work = lambda: self.node.install(ClusterSettings.create_coordinator(host.strip(), port, bind_host=bind))
        else:
            filename, _ = QFileDialog.getOpenFileName(self, 'Mở lời mời riêng dành cho máy này', filter='JSON (*.json)')
            if not filename:
                return
            work = lambda: self.node.install(ClusterSettings.from_json(Path(filename).read_text(encoding='utf-8-sig')))
        def configured(_):
            self.notice.setText('Đã lưu thông tin đồng bộ. Bật phục vụ để kết nối cụm.')
            self.sync_status.setText('Đã thiết lập danh tính cụm.')
            self._record(self.notice.text())
        self._submit('Thiết lập cụm', work, configured)

    def _invite(self):
        node, ok = QInputDialog.getItem(self, 'Xuất lời mời riêng', 'Máy nhận lời mời:', list(NODES[1:]), 0, False)
        if not ok:
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Lưu tệp bí mật, chỉ gửi cho máy nhận', f'{node}.enroll.json', 'JSON (*.json)')
        if filename:
            self._submit('Xuất lời mời', lambda: self.node.export_enrollment(node, Path(filename)),
                         lambda _: self.notice.setText(f'Đã xuất lời mời riêng cho {node}.'))

    def _profile(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Hồ sơ địa chỉ LAN')
        form = QFormLayout(dialog)
        inputs = {}
        for index, node in enumerate(NODES, 11):
            row = QHBoxLayout()
            host = QLineEdit(self.host.currentText() if node == self.node.name else '')
            host.setPlaceholderText(f'192.168.1.{index}')
            port = QSpinBox()
            port.setRange(1, 65535)
            port.setValue(self.port.value() if node == self.node.name else 5000)
            host.setAccessibleName(f'Địa chỉ {node}')
            port.setAccessibleName(f'Cổng {node}')
            row.addWidget(host, 1)
            row.addWidget(port)
            form.addRow(node, row)
            inputs[node] = host, port
        def load():
            name, _ = QFileDialog.getOpenFileName(dialog, 'Nhập hồ sơ địa chỉ LAN', filter='JSON (*.json)')
            if not name:
                return
            try:
                config = parse_config(json.loads(Path(name).read_text(encoding='utf-8-sig')), Path(name).parent)
                if {server.name for server in config.servers} != set(NODES):
                    raise ValueError('Hồ sơ phải có đúng S1, S2 và S3.')
                for server in config.servers:
                    host, port = inputs[server.name]
                    host.setText(server.host)
                    port.setValue(server.port)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dialog, 'Không mở được hồ sơ', str(exc))
        def save():
            data = {'servers': [{'name': node, 'host': host.text().strip(), 'port': port.value()}
                                for node, (host, port) in inputs.items()],
                    'filename': 'config.dat', 'output': 'downloads/config.dat'}
            try:
                parse_config(data, APP_DIR)
                if any(server['host'] == '0.0.0.0' for server in data['servers']):
                    raise ValueError('Dùng IP LAN thực, không dùng 0.0.0.0 làm địa chỉ kết nối.')
                name, _ = QFileDialog.getSaveFileName(dialog, 'Lưu hồ sơ công khai', str(APP_DIR / 'share-lan.json'), 'JSON (*.json)')
                if name:
                    _private_file(Path(name), json.dumps(data, ensure_ascii=False, indent=2))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dialog, 'Không xuất được hồ sơ', str(exc))
        buttons = QHBoxLayout()
        for title, fn in (('Nhập…', load), ('Xuất cho C1…', save), ('Áp dụng máy này', dialog.accept)):
            button = QPushButton(title)
            button.clicked.connect(fn)
            buttons.addWidget(button)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            host, port = inputs[self.node.name]
            if host.text().strip():
                self.host.setCurrentText(host.text().strip())
                self.port.setValue(port.value())

    def closeEvent(self, event):
        if self._job is None and self.node.server is None:
            self.timer.stop()
            self.sync_timer.stop()
            self._executor.shutdown(wait=False)
            event.accept()
            return
        self._closing = True
        self._enable()
        if self._job is None:
            self._submit('Dừng phục vụ', self.node.stop)
        event.ignore()


class DemoWindow(QTabWidget):
    def __init__(self, root: Path):
        super().__init__()
        self.setWindowTitle('ShareDownload · Demo ba kho độc lập')
        self.resize(1080, 720)
        dirs = {name: root / name for name in NODES}
        self.nodes = []
        for index, (name, directory) in enumerate(dirs.items(), 5001):
            node = NodeWindow(name, directory, port=index, demo=True)
            self.nodes.append(node)
            self.addTab(node, name)
        coordinator = self.nodes[0].node
        if coordinator.credentials.is_file():
            credentials = ClusterSettings.from_json(coordinator.credentials.read_text(encoding='utf-8'))
        else:
            credentials = ClusterSettings.create_coordinator('127.0.0.1', 5100, bind_host='127.0.0.1')
            coordinator.install(credentials)
        for node in self.nodes[1:]:
            if not node.node.credentials.is_file():
                node.node.install(ClusterSettings.from_json(credentials.export_enrollment(node.node.name)))
        for node in self.nodes:
            node.sync_status.setText('Đã thiết lập. Đang khởi động demo…')
            node._enable()
            QTimer.singleShot(0, node.service_button.click)
        self._closing_demo = False

    def closeEvent(self, event):
        if all(node._job is None and node.node.server is None for node in self.nodes):
            for node in self.nodes:
                node.close()
            event.accept()
            return
        if not self._closing_demo:
            self._closing_demo = True
            for node in self.nodes:
                node.close()
        QTimer.singleShot(150, self.close)
        event.ignore()


def main(argv=None):
    parser = argparse.ArgumentParser(description='ShareDownload server desktop')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--node', choices=NODES)
    options = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setStyleSheet(STYLESHEET)
    try:
        root = APP_DIR / 'server-data'
        root.mkdir(exist_ok=True)
        if options.demo:
            mode = 'demo'
        else:
            selection = root / 'role.txt'
            mode = options.node or (selection.read_text(encoding='utf-8').strip() if selection.is_file() else '')
            if mode not in NODES:
                mode, ok = QInputDialog.getItem(None, 'ShareDownload Server', 'Vai trò máy này:', list(NODES), 0, False)
                if not ok:
                    return 0
                _private_file(selection, mode)
        lock = QLockFile(str(root / f'{mode}.lock'))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError(f'{mode} đang chạy trong thư mục này.')
        window = DemoWindow(root / 'demo') if options.demo else NodeWindow(mode, root / mode)
        window.show()
        return app.exec()
    except Exception as exc:
        QMessageBox.critical(None, 'Không thể mở ShareDownload Server', str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
