import hashlib
import tempfile
import unittest
from pathlib import Path

from file_utils import build_chunks, prepare_part_file, publish_part_file, sha256_file


class FileUtilsTests(unittest.TestCase):
    def test_ranges_cover_file_without_overlap(self):
        chunks = build_chunks(10, 4)
        self.assertEqual(
            [(0, 0, 4), (1, 4, 4), (2, 8, 2)],
            [(chunk.chunk_id, chunk.offset, chunk.length) for chunk in chunks],
        )

    def test_empty_file_has_no_chunks(self):
        self.assertEqual([], build_chunks(0, 256))

    def test_rejects_invalid_sizes(self):
        for file_size, chunk_size in [(-1, 1), (10, 0), (10, 1_048_577)]:
            with self.subTest(file_size=file_size, chunk_size=chunk_size):
                with self.assertRaises(ValueError):
                    build_chunks(file_size, chunk_size)

    def test_sha256_file_streams_expected_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "source.dat")
            content = b"network-data" * 1000
            path.write_bytes(content)
            self.assertEqual(hashlib.sha256(content).hexdigest(), sha256_file(path))

    def test_prepare_and_publish_part_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "nested", "config.dat")
            part = prepare_part_file(output, 12)
            self.assertEqual(12, part.stat().st_size)
            self.assertFalse(output.exists())
            publish_part_file(part, output)
            self.assertTrue(output.exists())
            self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()
