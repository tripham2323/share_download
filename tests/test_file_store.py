import hashlib
import io
import socket
from pathlib import Path

import pytest

from file_store import FileStore
from protocol import PROTOCOL_VERSION, recv_frame, send_frame
from server import FileServer


def test_rejected_transfer_never_replaces_served_content(tmp_path):
    store = FileStore(tmp_path)
    first = store.put('demo.dat', io.BytesIO(b'first'))
    with pytest.raises(ValueError):
        store.put('demo.dat', io.BytesIO(b'other'))
    with pytest.raises(ValueError):
        store.put('partial.dat', io.BytesIO(b'cut'), size=99)
    with pytest.raises(ValueError):
        store.put('unverified.dat', io.BytesIO(b'bad'), digest='0' * 64)
    with pytest.raises(ValueError):
        store.put('../outside.dat', io.BytesIO(b'no'))
    reopened = FileStore(tmp_path)
    assert [(entry.name, entry.size) for entry in reopened.list()] == [('demo.dat', 5)]
    assert reopened.get('demo.dat').path.read_bytes() == b'first'
    assert reopened.put('demo.dat', io.BytesIO(b'first')) == first


def test_running_server_exposes_only_files_in_its_own_store(tmp_path):
    root = Path(tmp_path)
    store = FileStore(root / 'S1')
    other = FileStore(root / 'S2')
    other.put('local-only', io.BytesIO(b'second node'))
    server = FileServer(store, '127.0.0.1', 0)
    server.start()
    try:
        store.put('b.dat', io.BytesIO(b'new!'))
        store.put('a.dat', io.BytesIO(b'earlier'))
        with socket.create_connection(server.address, timeout=2) as sock:
            send_frame(sock, {'version': PROTOCOL_VERSION, 'type': 'LIST_FILES', 'after': 'a.dat'})
            info, payload = recv_frame(sock)
            assert not payload
            assert info['files'] == [{
                'filename': 'b.dat', 'file_size': 4,
                'file_sha256': hashlib.sha256(b'new!').hexdigest(),
            }]
            assert info['next_after'] is None
            send_frame(sock, {'version': PROTOCOL_VERSION, 'type': 'GET_CHUNK',
                              'filename': 'b.dat', 'chunk_id': 0, 'offset': 1, 'length': 2})
            assert recv_frame(sock)[1] == b'ew'
            send_frame(sock, {'version': PROTOCOL_VERSION, 'type': 'FILE_INFO_REQUEST',
                              'filename': 'local-only'})
            assert recv_frame(sock)[0]['code'] == 'FILE_NOT_FOUND'
    finally:
        server.shutdown()
