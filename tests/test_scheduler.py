import unittest

from scheduler import Chunk, ChunkScheduler


class SchedulerTests(unittest.TestCase):
    def test_retry_returns_chunk_to_queue(self):
        scheduler = ChunkScheduler([Chunk(0, 0, 4)])
        chunk = scheduler.acquire(timeout=0.01)
        self.assertIsNotNone(chunk)

        scheduler.retry(chunk)

        self.assertEqual(chunk, scheduler.acquire(timeout=0.01))

    def test_complete_is_counted_once(self):
        scheduler = ChunkScheduler([Chunk(0, 0, 4)])
        chunk = scheduler.acquire(timeout=0.01)
        self.assertIsNotNone(chunk)

        scheduler.complete(chunk)

        self.assertTrue(scheduler.all_completed())
        self.assertEqual(1, scheduler.completed_count())
        with self.assertRaises(RuntimeError):
            scheduler.complete(chunk)

    def test_skips_verified_chunks_when_resuming(self):
        first, second = Chunk(0, 0, 4), Chunk(1, 4, 4)
        scheduler = ChunkScheduler([first, second], completed_ids=frozenset({0}))
        self.assertEqual(1, scheduler.completed_count())
        self.assertEqual(second, scheduler.acquire(timeout=0.01))
        scheduler.complete(second)
        self.assertTrue(scheduler.all_completed())

    def test_empty_scheduler_is_complete(self):
        scheduler = ChunkScheduler([])
        self.assertTrue(scheduler.all_completed())
        self.assertFalse(scheduler.has_unfinished())
        self.assertEqual(0, scheduler.in_progress_count())

    def test_state_transitions_reject_wrong_chunk_state(self):
        scheduler = ChunkScheduler([Chunk(0, 0, 4)])
        chunk = Chunk(0, 0, 4)
        with self.assertRaises(RuntimeError):
            scheduler.complete(chunk)
        with self.assertRaises(RuntimeError):
            scheduler.retry(chunk)


if __name__ == "__main__":
    unittest.main()
