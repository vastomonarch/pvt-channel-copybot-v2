"""Job orchestration: queueing, dedup, lifecycle, cancellation.

A :class:`JobManager` owns every copy job's *task* and *state machine*:

* **one place** decides whether a job may start (duplicate / queue-full /
  shutdown checks) — the historical ``ACTIVE_DL_JOBS[key] = task``
  overwrite bug is structurally impossible here;
* all tasks are tracked (``asyncio.Task`` bookkeeping) and awaited during
  shutdown, so no orphan coroutine survives SIGTERM;
* state transitions go through :meth:`JobStore.set_state` (validated), so a
  job can only reach ``completed``/``failed`` from a legal state;
* pause/cancel are cooperative: the manager flips the persisted state and
  sets an ``asyncio.Event`` the pipeline checks between messages, plus a
  hard ``task.cancel()`` fallback after a grace period.

The actual copying lives in :mod:`pipeline`; the manager calls it through an
injected ``runner`` coroutine so tests can drive the whole lifecycle with a
fake runner and a real SQLite store.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from checkpoint import JobRecord, JobState, JobStore

log = logging.getLogger("copybot.jobs")

__all__ = [
    "JobManager",
    "JobSpec",
    "DuplicateJobError",
    "QueueFullError",
    "UnknownJobError",
    "job_key_for",
]

# runner(job, control) -> None; raises on failure, returns on completion
Runner = Callable[["JobRecord", "JobControl"], Awaitable[None]]


class DuplicateJobError(RuntimeError):
    """A job for the same source/range is already queued or running."""


class QueueFullError(RuntimeError):
    """The pending-job queue is at its configured limit."""


class UnknownJobError(KeyError):
    """No such job key in the store."""


def job_key_for(src: str, start_id: int, end_id: int) -> str:
    """Stable job key (same algorithm as the legacy implementation, so
    checkpoints imported from Telethon-era runs keep their identity)."""
    raw = f"{src}|{start_id}|{end_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


@dataclass
class JobSpec:
    """What the user asked for; the manager turns it into a JobRecord."""
    src: str
    dst: str
    start_msg_id: int
    end_msg_id: int
    progress_chat_id: int | None = None
    progress_msg_id: int | None = None

    @property
    def job_key(self) -> str:
        return job_key_for(self.src, self.start_msg_id, self.end_msg_id)


@dataclass
class JobControl:
    """Runtime signals handed to the runner for one job."""
    job_key: str
    pause: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check(self) -> None:
        """Raise CancelledError if cancellation was requested."""
        if self.cancel.is_set():
            raise asyncio.CancelledError()


@dataclass
class _Running:
    task: asyncio.Task
    control: JobControl
    record: JobRecord


class JobManager:
    """Create / resume / cancel copy jobs with bounded concurrency."""

    def __init__(self, store: JobStore, runner: Runner, *,
                 max_queued: int = 8, max_running: int = 2,
                 cancel_grace: float = 10.0):
        self.store = store
        self.runner = runner
        self.max_queued = max(1, max_queued)
        self.max_running = max(1, max_running)
        self.cancel_grace = cancel_grace
        self._running: dict[str, _Running] = {}
        self._shutting_down = False
        self._started_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # inspection
    # ------------------------------------------------------------------
    @property
    def running_keys(self) -> list[str]:
        return list(self._running)

    def is_active(self, job_key: str) -> bool:
        return job_key in self._running

    def control_of(self, job_key: str) -> JobControl | None:
        entry = self._running.get(job_key)
        return entry.control if entry else None

    def active_jobs(self) -> list[JobRecord]:
        return [e.record for e in self._running.values()]

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    def _ensure_capacity(self) -> None:
        if self._shutting_down:
            raise RuntimeError("shutting down; refusing new jobs")
        queued = self.store.list_jobs(states=[JobState.QUEUED])
        # queued rows not yet picked up by the scheduler
        waiting = [j for j in queued
                   if j.job_key not in self._running]
        if len(waiting) >= self.max_queued:
            raise QueueFullError(
                f"job queue is full ({len(waiting)}/{self.max_queued}); "
                "wait for running jobs to finish")

    def _ensure_no_duplicate(self, job_key: str) -> None:
        # 1) in-memory: already running (this is the bug the old code had —
        #    it overwrote the dict slot and leaked/abandoned the old task)
        if job_key in self._running:
            raise DuplicateJobError(
                f"job {job_key} is already running")
        # 2) persisted: queued or in-flight rows from this or a prior process
        rec = self.store.get_job(job_key)
        if rec is not None and rec.state in (
                JobState.QUEUED, JobState.RUNNING, JobState.DOWNLOADING,
                JobState.UPLOADING, JobState.CANCELLING):
            raise DuplicateJobError(
                f"job {job_key} already exists in state {rec.state!r}; "
                "use .pvtcopydlcontinue to resume it")

    def create(self, spec: JobSpec, *, fresh: bool = False) -> JobRecord:
        """Validate and persist a new job, then schedule it.

        ``fresh=True`` discards any existing paused/failed record for the
        same range (used by ``--fresh``); it never touches a running job.
        """
        if spec.start_msg_id > spec.end_msg_id:
            spec.start_msg_id, spec.end_msg_id = (
                spec.end_msg_id, spec.start_msg_id)
        key = spec.job_key
        existing = self.store.get_job(key)
        if existing is not None:
            if existing.state in (JobState.PAUSED, JobState.FAILED):
                if not fresh:
                    raise DuplicateJobError(
                        f"job {key} is {existing.state}; use "
                        ".pvtcopydlcontinue (or pass fresh=True) to restart")
                # fresh restart: drop old markers, keep history in `extra`
                self.store.delete_job(key)
            else:
                self._ensure_no_duplicate(key)
        self._ensure_capacity()

        rec = JobRecord(
            job_key=key, src=spec.src, dst=spec.dst,
            start_msg_id=spec.start_msg_id, end_msg_id=spec.end_msg_id,
            current_msg_id=spec.start_msg_id,
            state=JobState.QUEUED,
            progress_chat_id=spec.progress_chat_id,
            progress_msg_id=spec.progress_msg_id,
            extra={"created_by": "user"},
        )
        self.store.create_job(rec)
        log.info("job queued key=%s src=%s dst=%s range=%d..%d",
                 key, spec.src, spec.dst, spec.start_msg_id, spec.end_msg_id)
        return rec

    # ------------------------------------------------------------------
    # scheduling
    # ------------------------------------------------------------------
    def spawn(self, job_key: str) -> asyncio.Task:
        """Start (or restart) the runner task for an existing job row."""
        if self._shutting_down:
            raise RuntimeError("shutting down; refusing to start jobs")
        if job_key in self._running:
            raise DuplicateJobError(f"job {job_key} is already running")
        rec = self.store.get_job(job_key)
        if rec is None:
            raise UnknownJobError(job_key)
        if rec.state in (JobState.COMPLETED, JobState.CANCELLING):
            raise RuntimeError(
                f"job {job_key} is {rec.state} and cannot be restarted")
        if len([k for k in self._running]) >= self.max_running:
            raise QueueFullError(
                f"already {len(self._running)} jobs running "
                f"(max {self.max_running})")

        control = JobControl(job_key=job_key)
        if rec.state != JobState.QUEUED:
            self.store.set_state(job_key, JobState.RUNNING)
        rec.state = JobState.RUNNING if rec.state != JobState.QUEUED \
            else rec.state
        task = asyncio.create_task(
            self._supervise(rec, control), name=f"job:{job_key}")
        self._running[job_key] = _Running(task=task, control=control,
                                          record=rec)
        self._started_at[job_key] = time.monotonic()
        task.add_done_callback(lambda t, k=job_key: self._on_task_done(k, t))
        return task

    async def _supervise(self, rec: JobRecord, control: JobControl) -> None:
        """Run the runner and translate its outcome into state changes."""
        key = rec.job_key
        try:
            self.store.set_state(key, JobState.RUNNING)
        except ValueError as exc:  # illegal transition — surface loudly
            log.error("cannot start job %s: %s", key, exc)
            raise
        try:
            await self.runner(rec, control)
        except asyncio.CancelledError:
            # cooperative or forced cancellation
            self._safe_state(key, JobState.CANCELLING)
            self._safe_state(key, JobState.PAUSED)
            log.info("job %s cancelled (paused for resume)", key)
            raise
        except Exception as exc:
            log.exception("job %s failed: %r", key, exc)
            self._safe_state(key, JobState.FAILED, error=str(exc)[:500])
            raise
        else:
            self._safe_state(key, JobState.COMPLETED)
            log.info("job %s completed", key)

    def _safe_state(self, key: str, state: str, *, error: str = "") -> None:
        try:
            self.store.set_state(key, state, error=error)
        except (ValueError, KeyError) as exc:
            log.warning("state change %s->%s rejected for %s: %s",
                        "?", state, key, exc)

    def _on_task_done(self, key: str, task: asyncio.Task) -> None:
        self._running.pop(key, None)
        self._started_at.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # already logged + persisted by _supervise
            log.debug("job task %s finished with %r", key, exc)

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------
    def request_cancel(self, job_key: str) -> bool:
        """Ask a job to stop at the next checkpoint; returns False if the
        job is not running in this process (e.g. queued after a restart)."""
        entry = self._running.get(job_key)
        if entry is None:
            # Not running here: mark cancelling so a scheduler won't pick it
            # up, then park it as paused (resumable).
            rec = self.store.get_job(job_key)
            if rec is None:
                return False
            if rec.state in (JobState.QUEUED, JobState.PAUSED):
                self._safe_state(job_key, JobState.CANCELLING)
                self._safe_state(job_key, JobState.PAUSED)
                return True
            return False
        entry.control.cancel.set()
        self._safe_state(job_key, JobState.CANCELLING)
        # hard fallback: force cancellation if the runner ignores the flag
        loop = asyncio.get_running_loop()
        loop.call_later(self.cancel_grace, self._force_cancel, job_key)
        return True

    def _force_cancel(self, job_key: str) -> None:
        entry = self._running.get(job_key)
        if entry is None or entry.task.done():
            return
        if entry.control.cancel.is_set() and not entry.task.cancelled():
            log.warning("job %s did not stop within %.0fs — forcing cancel",
                        job_key, self.cancel_grace)
            entry.task.cancel()

    def request_pause(self, job_key: str) -> bool:
        entry = self._running.get(job_key)
        if entry is None:
            return False
        entry.control.pause.set()
        self._safe_state(job_key, JobState.PAUSED)
        return True

    def resume(self, job_key: str) -> asyncio.Task:
        """Resume a paused/failed/queued job (after a restart too)."""
        rec = self.store.get_job(job_key)
        if rec is None:
            raise UnknownJobError(job_key)
        if rec.state == JobState.COMPLETED:
            raise RuntimeError(f"job {job_key} already completed")
        if job_key in self._running:
            raise DuplicateJobError(f"job {job_key} is already running")
        if rec.state in (JobState.RUNNING, JobState.DOWNLOADING,
                         JobState.UPLOADING):
            # stale row from a crashed process → safe to take over
            log.info("taking over stale job %s (was %s)", job_key, rec.state)
        self._ensure_capacity()
        if rec.state == JobState.QUEUED:
            return self.spawn(job_key)
        self.store.set_state(job_key, JobState.QUEUED)
        return self.spawn(job_key)

    # ------------------------------------------------------------------
    # startup / shutdown
    # ------------------------------------------------------------------
    def recover_stale(self) -> list[str]:
        """After a restart, park jobs the previous process died inside.

        Rows left in ``running``/``downloading``/``uploading``/``cancelling``
        belong to a dead process (we hold the instance lock, so no other
        process can own them).  They are moved to ``paused`` — never
        auto-resumed, never marked complete — so the user decides via
        ``.pvtcopydlcontinue``.  Returns the affected keys.
        """
        stale: list[str] = []
        for state in (JobState.RUNNING, JobState.DOWNLOADING,
                      JobState.UPLOADING, JobState.CANCELLING):
            for rec in self.store.list_jobs(states=[state]):
                if rec.job_key in self._running:
                    continue
                try:
                    self.store.set_state(rec.job_key, JobState.PAUSED)
                except ValueError:
                    pass
                stale.append(rec.job_key)
                log.info("recovered stale job %s (was %s) → paused",
                         rec.job_key, state)
        return stale

    async def shutdown(self, timeout: float = 20.0) -> None:
        """Graceful stop: signal, wait, then cancel stragglers."""
        self._shutting_down = True
        if not self._running:
            return
        log.info("shutting down %d job(s) …", len(self._running))
        for entry in self._running.values():
            entry.control.cancel.set()
        tasks = [e.task for e in self._running.values()]
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            log.warning("%d job(s) ignored cancel — forcing", len(pending))
            for t in pending:
                t.cancel()
            await asyncio.wait(pending, timeout=min(5.0, timeout))
        # park whatever is left in a resumable state
        for key in list(self._running):
            self._safe_state(key, JobState.CANCELLING)
            self._safe_state(key, JobState.PAUSED)
        self._running.clear()
        log.info("job manager stopped")

    def stats(self) -> dict:
        return {
            "running": len(self._running),
            "running_keys": list(self._running),
            "max_running": self.max_running,
            "max_queued": self.max_queued,
            "shutting_down": self._shutting_down,
        }
