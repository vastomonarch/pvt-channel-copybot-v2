import json
import os
import tempfile
import unittest
from pathlib import Path

from checkpoint import (
    JobRecord, JobState, JobStore, OUT_FAILED, OUT_SENT, OUT_SKIPPED,
    atomic_write_json, import_legacy_checkpoints,
)


class TestAtomicWriteJson(unittest.TestCase):
    def test_atomic_write_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cfg" / "config.json"
            path.parent.mkdir(parents=True)
            atomic_write_json(path, {"a": 1, "b": [2, 3]})
            self.assertEqual(json.loads(path.read_text()), {"a": 1, "b": [2, 3]})
            # no temp files left behind
            leftovers = list(path.parent.iterdir())
            self.assertEqual(leftovers, [path])

    def test_atomic_write_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.json"
            atomic_write_json(path, {"v": 1})
            atomic_write_json(path, {"v": 2})
            self.assertEqual(json.loads(path.read_text()), {"v": 2})


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / "jobs.sqlite3"
        self.store = JobStore(self.db)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _rec(self, key="job1", start=1, end=5, **kw):
        return JobRecord(
            job_key=key, src="src", dst="dst",
            start_msg_id=start, end_msg_id=end,
            current_msg_id=start, state=JobState.QUEUED,
            total=end - start + 1, **kw,
        )

    def test_create_get_update(self):
        rec = self._rec()
        self.store.create_job(rec)
        got = self.store.get_job("job1")
        self.assertIsNotNone(got)
        self.assertEqual(got.state, JobState.QUEUED)
        self.store.set_state("job1", JobState.RUNNING)
        self.store.bump("job1", uploaded=2, skipped=1)
        got = self.store.get_job("job1")
        self.assertEqual(got.state, JobState.RUNNING)
        self.assertEqual(got.uploaded, 2)
        self.assertEqual(got.skipped, 1)

    def test_state_transition_validation(self):
        self.store.create_job(self._rec())
        self.store.set_state("job1", JobState.RUNNING)
        self.store.set_state("job1", JobState.COMPLETED)
        with self.assertRaises(ValueError):
            self.store.set_state("job1", JobState.RUNNING)  # illegal from completed

    def test_resume_markers(self):
        self.store.create_job(self._rec(start=10, end=12))
        self.store.begin_chunk("job1", [10, 11, 12])
        self.store.finish_chunk("job1", {10: (OUT_SENT, "ok"), 11: (OUT_SKIPPED, "svc")})
        # 12 left sending -> ambiguous; iter_range excludes it (must be resolved first)
        pending = list(self.store.iter_range("job1", 10, 12))
        self.assertNotIn(12, pending)
        self.assertEqual(self.store.ambiguous_messages("job1"), [12])
        # resolve as not-sent so it becomes pending again
        self.store.unmark("job1", 12)
        self.assertEqual(list(self.store.iter_range("job1", 10, 12)), [12])

    def test_failed_markers_retried_on_reset(self):
        self.store.create_job(self._rec(start=1, end=3))
        self.store.begin_chunk("job1", [1])
        self.store.finish_chunk("job1", {1: (OUT_FAILED, "boom")})
        # iter_range yields failed ids (so resume can retry)
        self.assertEqual(list(self.store.iter_range("job1", 1, 3)), [1, 2, 3])
        cleared = self.store.reset_failed("job1")
        self.assertEqual(cleared, 1)
        # marker gone, id is pending again
        self.assertEqual(self.store.outcome_of("job1", 1), None)

    def test_begin_finish_chunk_atomic_cursor(self):
        self.store.create_job(self._rec(start=1, end=5))
        self.store.begin_chunk("job1", [1, 2, 3])
        self.store.finish_chunk("job1", {
            1: (OUT_SENT, "ok"), 2: (OUT_SENT, "ok"), 3: (OUT_SKIPPED, "s"),
        })
        got = self.store.get_job("job1")
        self.assertEqual(got.current_msg_id, 4)
        self.assertEqual(list(self.store.iter_range("job1", 1, 5)), [4, 5])

    def test_delete_job(self):
        self.store.create_job(self._rec())
        self.store.begin_chunk("job1", [1])
        self.store.finish_chunk("job1", {1: (OUT_SENT, "ok")})
        self.store.delete_job("job1")
        self.assertIsNone(self.store.get_job("job1"))
        self.assertEqual(list(self.store.iter_range("job1", 1, 5)), [1, 2, 3, 4, 5])

    def test_import_legacy_checkpoints(self):
        staging = Path(self.td.name) / "staging"
        staging.mkdir()
        legacy = {
            "job_key": "abc123", "src": "-1001", "dst": "-1002",
            "start_msg_id": 10, "end_msg_id": 15, "current_msg_id": 13,
            "downloaded": 2, "uploaded": 2, "skipped": 1, "errors": 0,
            "uploaded_msgs": [10, 12], "skipped_msgs": [11],
        }
        (staging / "abc123_meta.json").write_text(json.dumps(legacy))
        imported = import_legacy_checkpoints(self.store, staging)
        self.assertEqual(imported, ["abc123"])
        # idempotent
        self.assertEqual(import_legacy_checkpoints(self.store, staging), [])
        got = self.store.get_job("abc123")
        self.assertIsNotNone(got)
        self.assertEqual(got.state, JobState.PAUSED)
        self.assertEqual(got.current_msg_id, 13)
        self.assertEqual(self.store.outcome_of("abc123", 10), OUT_SENT)
        self.assertEqual(self.store.outcome_of("abc123", 11), OUT_SKIPPED)
        self.assertEqual(self.store.outcome_of("abc123", 12), OUT_SENT)
        # legacy file preserved
        self.assertTrue((staging / "abc123_meta.json").exists())


if __name__ == "__main__":
    unittest.main()
