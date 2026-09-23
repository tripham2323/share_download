import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from checkpoint import CheckpointIdentity, CheckpointStore, ResumeConflict, load_checkpoint
from client import ClientConfig, DownloadCancelled, DownloadController, ServerEndpoint, download
from file_utils import build_chunks
from server import FileServer


class CountingServer(FileServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requested = []
        self.lock = threading.Lock()

    def _send_chunk(self, client, header):
        with self.lock:
            self.requested.append(header["chunk_id"])
        super()._send_chunk(client, header)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = bytes(range(256)) * 2048
        self.source = self.root / "config.dat"
        self.source.write_bytes(self.data)
        self.output = self.root / "out.dat"
        self.server = CountingServer(self.source, "127.0.0.1", 0, read_timeout=2)
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.endpoint = ServerEndpoint("S1", *self.server.address)

    def config(self):
        return ClientConfig((self.endpoint,), "config.dat", self.output,
                            chunk_size=16_384, connect_timeout=1, read_timeout=2)

    def cancelled_session(self):
        controller = DownloadController()
        seen = []
        def on_event(event):
            if event.kind == "chunk":
                seen.append(event.completed_chunks)
                if len(seen) == 4:
                    controller.cancel()
        with self.assertRaises(DownloadCancelled):
            download(self.config(), controller=controller, resume=True,
                     event_callback=on_event)
        self.assertFalse(self.output.exists())
        self.assertEqual(4, len(seen))
        state = self.root / "out.dat.part.state.json"
        self.assertTrue(state.exists())
        return state

    def test_resume_requests_only_missing_chunks(self):
        self.cancelled_session()
        self.server.requested.clear()

        result = download(self.config(), resume=True)

        self.assertEqual(self.data, result.output.read_bytes())
        self.assertEqual(28, len(self.server.requested))
        self.assertFalse((self.root / "out.dat.part.state.json").exists())

    def test_corrupted_checkpoint_chunk_is_downloaded_again(self):
        self.cancelled_session()
        part = self.root / "out.dat.part"
        with part.open("r+b") as target:
            target.seek(0)
            target.write(b"x")
        self.server.requested.clear()

        result = download(self.config(), resume=True)

        self.assertEqual(self.data, result.output.read_bytes())
        self.assertIn(0, self.server.requested)
        self.assertEqual(29, len(self.server.requested))

    def test_conflicting_metadata_preserves_previous_part(self):
        self.cancelled_session()
        part = self.root / "out.dat.part"
        previous = part.read_bytes()
        self.source.write_bytes(b"new source" * 100)
        new_server = CountingServer(self.source, "127.0.0.1", 0)
        new_server.start()
        self.addCleanup(new_server.shutdown)
        self.endpoint = ServerEndpoint("S1", *new_server.address)

        with self.assertRaises(ResumeConflict):
            download(self.config(), resume=True)

        self.assertEqual(previous, part.read_bytes())
        self.assertFalse(self.output.exists())

    def test_broken_sidecar_never_publishes_old_bytes(self):
        state = self.cancelled_session()
        state.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ResumeConflict):
            download(self.config(), resume=True)
        self.assertFalse(self.output.exists())

    def test_revalidates_chunk_bytes_before_skipping(self):
        part = self.root / "out.dat.part"
        part.write_bytes(self.data)
        chunks = build_chunks(len(self.data), 16_384)
        identity = CheckpointIdentity("config.dat", len(self.data),
                                      hashlib.sha256(self.data).hexdigest(), 16_384)
        store = CheckpointStore(part, identity)
        store.record(chunks[0], hashlib.sha256(self.data[:16_384]).hexdigest())
        with part.open("r+b") as target:
            store.flush(target)
        self.assertEqual({0}, set(load_checkpoint(part, identity, chunks)))
        with part.open("r+b") as target:
            target.write(b"x")
        self.assertEqual({}, load_checkpoint(part, identity, chunks))


if __name__ == "__main__":
    unittest.main()
