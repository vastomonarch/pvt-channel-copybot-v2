import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from checkpoint import JobRecord, JobState, JobStore, OUT_SENT
from config import Config
from job_manager import JobControl
from pipeline import CopyPipeline, JobFailed, ProgressSnapshot, ThrottledProgress


def make_cfg(tmp: str) -> Config:
    return Config(
        api_id=1, api_hash="h", bot_token="",
        data_dir=Path(tmp) / "data",
        staging_dir=Path(tmp) / "stage",
        max_concurrent_downloads=2,
        max_concurrent_uploads=2,
        max_queued_jobs=4,
        max_running_jobs=2,
        retry_count=2,
        request_timeout=5,
        send_timeout=5,
        download_stall_timeout=5,
        progress_edit_interval=0.0,
        album_window=15,
    )


def make_rec(key="k1", start=1, end=3):
    return JobRecord(
        job_key=key, src="-1001", dst="-1002",
        start_msg_id=start, end_msg_id=end,
        current_msg_id=start, state=JobState.QUEUED,
        total=end - start + 1,
    )


class ThrottlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_throttled_progress(self):
        seen = []

        async def sink(snap):
            seen.append(snap.mode)

        tp = ThrottledProgress(sink, interval=10.0)
        snap = ProgressSnapshot(job_key="k", mode="copy")
        await tp.emit(snap)                # first emits
        await tp.emit(snap)                # throttled
        await tp.emit(snap, force=True)    # forced
        self.assertEqual(seen, ["copy", "copy"])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.td.name)
        self.cfg.ensure_dirs()
        self.store = JobStore(self.cfg.jobs_db_path)
        self.dl_sem = asyncio.Semaphore(2)
        self.ul_sem = asyncio.Semaphore(2)

    async def asyncTearDown(self):
        self.store.close()
        self.td.cleanup()

    def _pipe(self, **kw):
        return CopyPipeline(
            self.cfg, self.store, user=object(), bot=None,
            dl_sem=self.dl_sem, ul_sem=self.ul_sem, **kw,
        )

    async def test_server_copy_fast_path(self):
        rec = make_rec(start=1, end=3)
        self.store.create_job(rec)
        self.store.set_state(rec.job_key, JobState.RUNNING)
        control = JobControl(job_key=rec.job_key)

        async def fake_resolve(client, ident):
            return -1001 if str(ident) == "-1001" else -1002

        async def fake_get_messages(client, chat_id, ids):
            return {i: {"@type": "message", "id": i, "chat_id": chat_id,
                        "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": "hi", "entities": []}}}
                    for i in ids}

        async def fake_copy(client, from_chat, to_chat, ids, *, timeout=None):
            return [{"@type": "message", "id": 100 + i} for i in ids]

        with mock.patch("pipeline.resolve_chat", side_effect=fake_resolve), \
             mock.patch("pipeline.get_messages", side_effect=fake_get_messages), \
             mock.patch("pipeline.copy_messages", side_effect=fake_copy) as mcopy:
            await self._pipe().run(rec, control)

        self.assertEqual(self.store.get_job("k1").state, JobState.COMPLETED)
        self.assertEqual(self.store.get_job("k1").uploaded, 3)
        self.assertTrue(mcopy.called)

    async def test_server_only_restricted_fails_clearly(self):
        rec = make_rec(start=1, end=1)
        rec.extra = {"mode": "server_only"}
        self.store.create_job(rec)
        self.store.set_state(rec.job_key, JobState.RUNNING)
        control = JobControl(job_key=rec.job_key)

        async def fake_resolve(client, ident):
            return -1001 if str(ident) == "-1001" else -1002

        async def fake_get_messages(client, chat_id, ids):
            return {i: {"@type": "message", "id": i, "chat_id": chat_id,
                        "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": "hi", "entities": []}}}
                    for i in ids}

        async def fake_copy(*args, **kwargs):
            raise RuntimeError("CHAT_SEND_PROTECTED")

        with mock.patch("pipeline.resolve_chat", side_effect=fake_resolve), \
             mock.patch("pipeline.get_messages", side_effect=fake_get_messages), \
             mock.patch("pipeline.copy_messages", side_effect=fake_copy):
            with self.assertRaises(JobFailed) as ctx:
                await self._pipe(server_only=True).run(rec, control)
        self.assertIn("use .pvtcopydl", str(ctx.exception))

    async def test_retry_then_success(self):
        rec = make_rec(start=5, end=5)
        self.store.create_job(rec)
        self.store.set_state(rec.job_key, JobState.RUNNING)
        control = JobControl(job_key=rec.job_key)
        calls = {"n": 0}

        async def fake_resolve(client, ident):
            return -1001 if str(ident) == "-1001" else -1002

        async def fake_get_messages(client, chat_id, ids):
            return {i: {"@type": "message", "id": i, "chat_id": chat_id,
                        "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": "hi", "entities": []}}}
                    for i in ids}

        calls2 = {"n": 0}

        async def flaky_copy2(*args, **kwargs):
            calls2["n"] += 1
            if calls2["n"] == 1:
                from errors import TdError
                raise TdError(500, "Failed to get response")
            return [{"@type": "message", "id": 999}]

        with mock.patch("pipeline.resolve_chat", side_effect=fake_resolve), \
             mock.patch("pipeline.get_messages", side_effect=fake_get_messages), \
             mock.patch("pipeline.copy_messages", side_effect=flaky_copy2):
            await self._pipe().run(rec, control)
        self.assertEqual(self.store.get_job("k1").state, JobState.COMPLETED)
        self.assertGreaterEqual(calls2["n"], 2)

    async def test_resume_skips_sent(self):
        rec = make_rec(start=1, end=3)
        self.store.create_job(rec)
        # mark 1,2 sent already
        self.store.begin_chunk("k1", [1, 2])
        self.store.finish_chunk("k1", {1: (OUT_SENT, "ok"), 2: (OUT_SENT, "ok")})
        self.store.set_state("k1", JobState.RUNNING)
        control = JobControl(job_key=rec.job_key)

        async def fake_resolve(client, ident):
            return -1001 if str(ident) == "-1001" else -1002

        seen_ids = []

        async def fake_get_messages(client, chat_id, ids):
            return {i: {"@type": "message", "id": i, "chat_id": chat_id,
                        "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": "hi", "entities": []}}}
                    for i in ids}

        async def fake_copy(client, from_chat, to_chat, ids, *, timeout=None):
            seen_ids.extend(ids)
            return [{"@type": "message", "id": 100 + i} for i in ids]

        with mock.patch("pipeline.resolve_chat", side_effect=fake_resolve), \
             mock.patch("pipeline.get_messages", side_effect=fake_get_messages), \
             mock.patch("pipeline.copy_messages", side_effect=fake_copy):
            await self._pipe().run(rec, control)

        self.assertEqual(seen_ids, [3])
        self.assertEqual(self.store.get_job("k1").state, JobState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
