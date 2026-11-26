import asyncio
import tempfile
import unittest
from pathlib import Path

from checkpoint import JobRecord, JobState, JobStore
from job_manager import (
    DuplicateJobError, JobControl, JobManager, JobSpec, QueueFullError,
)


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.td.name) / "jobs.sqlite3")
        self.events: list[str] = []
        self.release = asyncio.Event()
        self.fail_with: Exception | None = None

        async def runner(rec: JobRecord, control: JobControl):
            self.events.append(f"start:{rec.job_key}")
            # wait, but stay responsive to cancellation
            while not self.release.is_set():
                if control.cancelled:
                    raise asyncio.CancelledError()
                try:
                    await asyncio.wait_for(self.release.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
            if control.cancelled:
                raise asyncio.CancelledError()
            if self.fail_with:
                raise self.fail_with
            self.events.append(f"done:{rec.job_key}")

        self.runner = runner
        self.mgr = JobManager(self.store, self.runner, max_queued=2, max_running=1)

    async def asyncTearDown(self):
        self.release.set()
        try:
            await self.mgr.shutdown(timeout=2)
        finally:
            self.store.close()
            self.td.cleanup()

    def spec(self, n=1):
        return JobSpec(src="-1001", dst="-1002", start_msg_id=n, end_msg_id=n + 9)

    async def test_lifecycle_complete(self):
        rec = self.mgr.create(self.spec(1))
        self.mgr.spawn(rec.job_key)
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.get_job(rec.job_key).state, JobState.RUNNING)
        self.release.set()
        await asyncio.sleep(0.1)
        self.assertEqual(self.store.get_job(rec.job_key).state, JobState.COMPLETED)

    async def test_duplicate_running_rejected(self):
        rec = self.mgr.create(self.spec(1))
        self.mgr.spawn(rec.job_key)
        with self.assertRaises(DuplicateJobError):
            self.mgr.create(self.spec(1))

    async def test_queue_full(self):
        # max_running=1, max_queued=2
        r1 = self.mgr.create(self.spec(1))
        self.mgr.spawn(r1.job_key)
        self.mgr.create(self.spec(20))  # queued row, not running
        self.mgr.create(self.spec(40))
        with self.assertRaises(QueueFullError):
            self.mgr.create(self.spec(60))

    async def test_cancel_running(self):
        rec = self.mgr.create(self.spec(1))
        self.mgr.spawn(rec.job_key)
        await asyncio.sleep(0.05)
        self.assertTrue(self.mgr.request_cancel(rec.job_key))
        self.release.set()  # let runner observe cancel
        await asyncio.sleep(0.3)
        self.assertEqual(self.store.get_job(rec.job_key).state, JobState.PAUSED)

    async def test_shutdown_parks_jobs(self):
        rec = self.mgr.create(self.spec(1))
        self.mgr.spawn(rec.job_key)
        await asyncio.sleep(0.05)
        # never release -> shutdown must force-cancel
        await self.mgr.shutdown(timeout=0.5)
        state = self.store.get_job(rec.job_key).state
        self.assertIn(state, (JobState.PAUSED, JobState.CANCELLING, JobState.FAILED, JobState.COMPLETED))

    async def test_recover_stale(self):
        rec = self.mgr.create(self.spec(1))
        self.store.set_state(rec.job_key, JobState.RUNNING)
        # simulate crash: row is RUNNING but not in mgr._running
        stale = self.mgr.recover_stale()
        self.assertIn(rec.job_key, stale)
        self.assertEqual(self.store.get_job(rec.job_key).state, JobState.PAUSED)

    async def test_resume_paused(self):
        rec = self.mgr.create(self.spec(1))
        self.store.set_state(rec.job_key, JobState.RUNNING)
        self.store.set_state(rec.job_key, JobState.PAUSED)
        self.release.set()
        self.mgr.resume(rec.job_key)
        await asyncio.sleep(0.15)
        self.assertEqual(self.store.get_job(rec.job_key).state, JobState.COMPLETED)

    async def test_failed_job_marked_failed(self):
        self.fail_with = RuntimeError("boom")
        rec = self.mgr.create(self.spec(1))
        self.mgr.spawn(rec.job_key)
        self.release.set()
        await asyncio.sleep(0.15)
        self.assertEqual(self.store.get_job(rec.job_key).state, JobState.FAILED)


if __name__ == "__main__":
    unittest.main()
