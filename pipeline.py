"""The copy pipeline: one job's full lifecycle, TDLib-only.

Replaces the Telethon + aria2c pipeline.  Structure::

    run(rec, control)
      ├─ resolve source / destination chats
      ├─ recover ambiguous "sending" markers (crash-mid-send safety)
      ├─ iterate pending ids from the JobStore (never re-sends / re-skips)
      │    ├─ server-side copy (forwardMessages send_copy=True), batched
      │    └─ fallback per message: download → InputMessageContent → send
      │         (semaphore-bounded; progress via updateFile watches)
      └─ every outcome commits with the cursor in one transaction

Key properties
--------------
* **Bounded concurrency**: download/upload semaphores are shared across all
  jobs, so N running jobs can never exceed the configured parallelism.
* **Cooperative cancellation**: ``control.check`` between messages,
  ``control.pause`` honoured between messages, TDLib ``cancelDownloadFile``
  for in-flight downloads.
* **No busy loops**: waits are event waits / semaphore waits / sleeps with
  backoff.
* **Throttled progress**: one ``ThrottledProgress`` enforces the configured
  edit interval; force-emits only on phase changes and completion.
* **Error policy**: classify → flood-wait sleep, exponential backoff +
  jitter for retryables, skip for unrecoverable messages, stop the job for
  destination/permission failures (resumable via .pvtcopydlcontinue).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from checkpoint import JobRecord, JobStore
from config import Config
from errors import ErrorKind, TdError, backoff_delay, classify
from job_manager import JobControl
from progress_ui import Spinner
from progress_ui.renderer import render_copy_progress
from tdlib_client import TDClient
from tdlib_media import (
    MediaGone,
    UploadWatch,
    build_input_content,
    download_message_media,
    download_source_thumbnail,
)
from tdlib_messages import (
    caption_of,
    classify as classify_message,
    content_of,
    copy_messages,
    fetch_window,
    get_messages,
    media_name,
    media_size,
    resolve_chat,
    send_album,
    send_input,
)

log = logging.getLogger("copybot.pipeline")

__all__ = [
    "CopyPipeline",
    "ProgressSnapshot",
    "ProgressSink",
    "JobFailed",
]

# Server-side copy batch size: TDLib forwardMessages is one RPC per batch;
# smaller batches keep failure blast-radius and checkpoint granularity sane.
COPY_BATCH = 50


class JobFailed(RuntimeError):
    """Abort the job with a user-visible reason (state → failed, resumable)."""


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
@dataclass
class ProgressSnapshot:
    job_key: str
    mode: str = "copy"            # copy | downloading | uploading
    current_msg: int = 0
    total: int = 0
    downloaded: int = 0
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0
    speed: float = 0.0            # bytes/s for the active transfer
    eta: float = 0.0
    elapsed: float = 0.0
    filename: str = ""
    file_current: int = 0
    file_total: int = 0
    start_msg: int = 0

    def render(self, spinner: Spinner) -> str:
        return render_copy_progress(
            spinner=spinner,
            mode=self.mode,
            job_key=self.job_key,
            current_msg=self.current_msg,
            total=self.total,
            downloaded=self.downloaded,
            uploaded=self.uploaded,
            skipped=self.skipped,
            errors=self.errors,
            speed=self.speed,
            eta=self.eta,
            elapsed=self.elapsed,
            filename=self.filename,
            file_current=self.file_current,
            file_total=self.file_total,
            start_msg=self.start_msg,
        )


ProgressSink = Callable[[ProgressSnapshot], Awaitable[None]]


class ThrottledProgress:
    """Wraps a sink, enforcing ``progress_edit_interval`` unless forced."""

    def __init__(self, sink: ProgressSink | None, interval: float):
        self._sink = sink
        self._interval = max(0.0, interval)
        self._last = 0.0

    async def emit(self, snap: ProgressSnapshot, *, force: bool = False) -> None:
        if self._sink is None:
            return
        now = time.monotonic()
        if not force and (now - self._last) < self._interval:
            return
        self._last = now
        try:
            await self._sink(snap)
        except Exception:
            # Progress must never kill a job (e.g. FloodWait on edits)
            log.debug("progress sink failed", exc_info=True)


@dataclass
class _SpeedWindow:
    """Rolling (t, bytes) samples → bytes/s over the last ~8 s."""
    samples: list[tuple[float, float]] = field(default_factory=list)
    window: float = 8.0

    def add(self, transferred: int) -> None:
        now = time.monotonic()
        self.samples.append((now, float(transferred)))
        cutoff = now - self.window
        while len(self.samples) > 2 and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def speed(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        t0, b0 = self.samples[0]
        t1, b1 = self.samples[-1]
        if t1 <= t0:
            return 0.0
        return max(0.0, (b1 - b0) / (t1 - t0))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
class CopyPipeline:
    """Executes one :class:`JobRecord` against the TDLib clients."""

    def __init__(self, cfg: Config, store: JobStore,
                 user: TDClient, bot: TDClient | None, *,
                 dl_sem: asyncio.Semaphore, ul_sem: asyncio.Semaphore,
                 progress: ProgressSink | None = None,
                 staging_dir: Path | None = None,
                 rng: random.Random | None = None):
        self.cfg = cfg
        self.store = store
        self.user = user
        self.bot = bot
        self.dl_sem = dl_sem
        self.ul_sem = ul_sem
        self.progress = ThrottledProgress(progress, cfg.progress_edit_interval)
        self.staging = Path(staging_dir or cfg.staging_dir)
        self.rng = rng or random.Random()
        self.spinner = Spinner()
        # ids that exhausted retries during THIS run: excluded from pending
        # so the loop can't spin on them; their 'failed' markers are cleared
        # by reset_failed() at the start of the next run (.pvtcopydlcontinue)
        self._run_failed: set[int] = set()

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    async def run(self, rec: JobRecord, control: JobControl) -> None:
        start = time.monotonic()
        src_id = _parse_identifier(rec.src)
        dst_id = _parse_identifier(rec.dst)
        try:
            src_chat = await resolve_chat(self.user, src_id)
            dst_chat = await resolve_chat(self.user, dst_id)
        except TdError as exc:
            raise JobFailed(
                f"cannot resolve chats: [{exc.code}] {exc.message}") from exc
        if src_chat == dst_chat:
            raise JobFailed("source and destination are the same chat")

        log.info("job %s: copy %d..%d (%d messages) src=%s dst=%s",
                 rec.job_key, rec.start_msg_id, rec.end_msg_id, rec.total,
                 src_chat, dst_chat)

        # a prior run stopped at a failure point — retry those ids now
        cleared = self.store.reset_failed(rec.job_key)
        if cleared:
            log.info("job %s: retrying %d previously failed message(s)",
                     rec.job_key, cleared)

        await self._recover_ambiguous(rec, src_chat, dst_chat)
        await self._loop(rec, control, src_chat, dst_chat, start)

    # ------------------------------------------------------------------
    # crash-recovery of "sending" markers
    # ------------------------------------------------------------------
    async def _recover_ambiguous(self, rec: JobRecord, src_chat: int,
                                 dst_chat: int) -> None:
        """Resolve messages that were in-flight when a previous run died.

        Ordering rule: sends are sequential inside a job, so if a *later*
        message is already marked ``sent``, the ambiguous one completed too.
        Otherwise scan the destination's recent messages for a content match
        (caption/text equality): a match means it was sent; no match means it
        never reached Telegram → drop the marker so it is re-processed.
        """
        ambiguous = self.store.ambiguous_messages(rec.job_key)
        if not ambiguous:
            return
        log.info("job %s: recovering %d ambiguous message(s): %s",
                 rec.job_key, len(ambiguous), ambiguous)
        later_sent = self.store.processed_ids(rec.job_key, "sent")
        dest_texts = await self._recent_dest_texts(dst_chat)
        for mid in ambiguous:
            if any(s > mid for s in later_sent):
                self.store.resolve_ambiguous(
                    rec.job_key, mid, "sent", detail="ordering recovery")
                log.info("msg %d: recovered as sent (ordering)", mid)
                continue
            matched = await self._match_destination(rec, mid, src_chat,
                                                    dest_texts)
            if matched:
                self.store.resolve_ambiguous(
                    rec.job_key, mid, "sent", detail="content match")
                log.info("msg %d: recovered as sent (content match)", mid)
            else:
                self.store.unmark(rec.job_key, mid)
                log.info("msg %d: not found in destination — will re-send",
                         mid)

    async def _recent_dest_texts(self, dst_chat: int) -> list[str]:
        try:
            res = await self.user.request({
                "@type": "getChatHistory", "chat_id": dst_chat,
                "from_message_id": 0, "offset": 0, "limit": 40,
                "only_local": False,
            })
        except Exception:
            log.debug("fetching destination history failed", exc_info=True)
            return []
        out: list[str] = []
        for dm in res.get("messages") or []:
            text, _ = caption_of(content_of(dm))
            if text:
                out.append(text)
        return out

    async def _match_destination(self, rec: JobRecord, mid: int,
                                 src_chat: int,
                                 dest_texts: list[str]) -> bool:
        if not dest_texts:
            return False
        try:
            msgs = await get_messages(self.user, src_chat, [mid])
            src_msg = msgs.get(mid)
            if src_msg is None:
                return False
            text, _ = caption_of(content_of(src_msg))
            return bool(text) and text in dest_texts
        except Exception:
            log.debug("source fetch for match failed (msg %d)", mid,
                      exc_info=True)
            return False

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    async def _loop(self, rec: JobRecord, control: JobControl,
                    src_chat: int, dst_chat: int, start: float) -> None:
        while True:
            if control.cancelled:
                raise asyncio.CancelledError
            if control.pause.is_set():
                self._store_state(rec, "paused")
                while control.pause.is_set() and not control.cancelled:
                    await asyncio.sleep(0.25)
                if control.cancelled:
                    raise asyncio.CancelledError
                self._store_state(rec, "running")

            pending = [m for m in self.store.iter_range(
                rec.job_key, rec.start_msg_id, rec.end_msg_id)
                if m not in self._run_failed]
            if not pending:
                break

            # --- server-side copy fast path (batched) -------------------
            batch = pending[:COPY_BATCH]
            fully = await self._try_server_copy(
                rec, control, src_chat, dst_chat, batch, start)
            if fully:
                await self.progress.emit(self._snapshot(rec, start))
                continue
            if control.cancelled:
                raise asyncio.CancelledError

            # --- per-message fallback (download + re-upload) ------------
            msg_id = pending[0]
            await self._process_one(
                rec, control, src_chat, dst_chat, msg_id, start)

            await self.progress.emit(self._snapshot(rec, start))
            # gentle pacing to stay under Telegram's rate envelope
            await asyncio.sleep(self.rng.uniform(0.05, 0.15))

        self._store_state(rec, "completed")
        await self.progress.emit(self._snapshot(rec, start), force=True)

    def _store_state(self, rec: JobRecord, state: str) -> None:
        try:
            self.store.set_state(rec.job_key, state)
            rec.state = state
        except (ValueError, KeyError) as exc:
            log.debug("state change to %s rejected: %s", state, exc)

    # ------------------------------------------------------------------
    # server-side copy
    # ------------------------------------------------------------------
    async def _try_server_copy(self, rec: JobRecord, control: JobControl,
                               src_chat: int, dst_chat: int,
                               batch: list[int], start: float) -> bool:
        """Try a batched server-side copy (no download/upload at all).

        Returns True if the batch was fully resolved.  Returns False when
        the caller should process ``batch[0]`` via the per-message path
        (batch copy is unavailable for these messages).  Flood waits are
        absorbed here; retryable errors get bounded backoff before falling
        back.
        """
        # Cheap local checks first: a batch containing service messages or
        # missing ids would make forwardMessages fail wholesale.
        msgs = await get_messages(self.user, src_chat, batch)
        resolvable: list[int] = []
        skipped: dict[int, tuple[str, str]] = {}
        for mid in batch:
            m = msgs.get(mid)
            if m is None:
                skipped[mid] = ("skipped", "message not found")
            elif classify_message(m) == "service":
                skipped[mid] = ("skipped", "service message")
            else:
                resolvable.append(mid)

        if skipped:
            self.store.begin_chunk(rec.job_key, list(skipped))
            self.store.finish_chunk(rec.job_key, skipped)
            for mid in sorted(skipped):
                log.info("msg %d: skipped (%s)", mid, skipped[mid][1])
        if not resolvable:
            return True

        self.store.begin_chunk(rec.job_key, resolvable)
        attempt = 0
        while True:
            if control.cancelled:
                raise asyncio.CancelledError
            try:
                sent = await copy_messages(
                    self.user, src_chat, dst_chat, resolvable,
                    timeout=self.cfg.send_timeout)
                if len(sent) != len(resolvable):
                    log.warning("server copy returned %d/%d messages",
                                len(sent), len(resolvable))
                self.store.finish_chunk(rec.job_key, {
                    mid: ("sent", "server copy") for mid in resolvable})
                for mid in resolvable:
                    log.info("COPY msg=%d → server-side", mid)
                await self.progress.emit(
                    self._snapshot(rec, start, mode="copy"), force=True)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                info = classify(exc)
                if info.kind is ErrorKind.FLOOD_WAIT:
                    log.warning("server copy flood-wait %.0fs (msg %s)",
                                info.wait_seconds, resolvable[:3])
                    self._bump(rec, flood_waits=1)
                    await asyncio.sleep(info.wait_seconds)
                    continue
                if info.retryable and attempt < self.cfg.retry_count:
                    attempt += 1
                    delay = backoff_delay(attempt, rng=self.rng)
                    log.info("server copy retry %d/%d in %.1fs (%s)",
                             attempt, self.cfg.retry_count, delay, info)
                    self._bump(rec, retries=1)
                    await asyncio.sleep(delay)
                    continue
                # Not copyable as a batch → per-message fallback.
                # Un-mark the batch first: these ids must stay *pending*
                # (not 'sending') so the fallback loop can process them.
                for mid in resolvable:
                    self.store.unmark(rec.job_key, mid)
                log.info("server copy unavailable (%s); falling back for "
                         "%d message(s)", info, len(resolvable))
                return False

    # ------------------------------------------------------------------
    # per-message processing (server-copy fallback)
    # ------------------------------------------------------------------
    async def _process_one(self, rec: JobRecord, control: JobControl,
                           src_chat: int, dst_chat: int, msg_id: int,
                           start: float) -> None:
        fetched = await fetch_window(
            self.user, src_chat, msg_id,
            rec.start_msg_id, rec.end_msg_id, self.cfg.album_window)
        cur = fetched.get(msg_id)
        if cur is None:
            self._resolve(rec, msg_id, "skipped", "message not found")
            log.info("MSG NOT FOUND msg=%d - skipping", msg_id)
            return

        kind = classify_message(cur)
        if kind == "service":
            self._resolve(rec, msg_id, "skipped", "service message")
            return

        album_id = cur.get("media_album_id") or 0
        if album_id and kind == "media":
            await self._process_album(
                rec, control, src_chat, dst_chat, msg_id, fetched,
                int(album_id), start)
            return

        if kind in ("text", "poll", "contact", "location"):
            await self._process_simple(rec, control, src_chat, dst_chat,
                                       msg_id, cur, start)
            return

        if kind == "media":
            await self._process_media(
                rec, control, src_chat, dst_chat, msg_id, cur, start)
            return

        self._resolve(rec, msg_id, "skipped", f"unhandled content ({kind})")

    # -- text / poll / contact / location --------------------------------
    async def _process_simple(self, rec: JobRecord, control: JobControl,
                              src_chat: int, dst_chat: int, msg_id: int,
                              msg: dict, start: float) -> None:
        """Send a non-media message via input content (one bounded retry
        loop; the server-copy batch already failed for this id)."""
        content = build_input_content(msg)
        await self._send_with_retry(rec, control, dst_chat, msg_id, content,
                                    start)

    # -- media ------------------------------------------------------------
    async def _process_media(self, rec: JobRecord, control: JobControl,
                             src_chat: int, dst_chat: int, msg_id: int,
                             msg: dict, start: float) -> None:
        cfg = self.cfg
        content = content_of(msg)
        name = media_name(content) or f"msg_{msg_id}"
        staging = self.staging / rec.job_key / f"msg_{msg_id}"
        self.store.begin_chunk(rec.job_key, [msg_id])

        # ── download (bounded) ───────────────────────────────────────
        speedwin = _SpeedWindow()
        snap = self._snapshot(rec, start, mode="downloading", filename=name)

        def on_dl(done: int, total: int) -> None:
            speedwin.add(done)
            snap.file_current = done
            snap.file_total = total
            snap.speed = speedwin.speed()

        async with self.dl_sem:
            if control.cancelled:
                raise asyncio.CancelledError
            try:
                path, actual, _elapsed = await download_message_media(
                    self.user, msg, src_chat, staging,
                    on_progress=on_dl,
                    stall_timeout=cfg.download_stall_timeout)
            except MediaGone as exc:
                self._resolve(rec, msg_id, "skipped", f"media gone: {exc}")
                log.info("msg %d: media gone (%s) — skipped", msg_id, exc)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._item_error(rec, msg_id, exc,
                                       context=f"download msg={msg_id}")
                return
        await self.progress.emit(snap, force=True)

        # ── thumbnail (best effort) ──────────────────────────────────
        thumb = await download_source_thumbnail(
            self.user, content, staging, msg_id)

        # ── split if above Telegram's single-file cap ────────────────
        parts = [path]
        try:
            if os.path.getsize(path) > cfg.max_single_file_bytes:
                parts = await asyncio.to_thread(
                    _split_large, path, cfg.max_single_file_bytes)
                if len(parts) > 1:
                    log.info("split %s → %d parts", name, len(parts))
        except OSError as exc:
            self._resolve(rec, msg_id, "failed", f"stat failed: {exc}")
            raise JobFailed(f"msg {msg_id}: cannot stat download: {exc}") from exc

        # ── upload (bounded), one send per part ──────────────────────
        base_caption, base_entities = caption_of(content)
        for idx, part in enumerate(parts):
            if control.cancelled:
                raise asyncio.CancelledError
            caption = base_caption
            entities = base_entities
            if len(parts) > 1:
                caption = (base_caption + "\n\n"
                           f"📦 Part {idx + 1}/{len(parts)}").strip()
                entities = [] if idx > 0 else base_entities
            try:
                await self._upload_part(
                    rec, control, dst_chat, msg, part, caption, entities,
                    thumb if idx == 0 else None, start)
            except asyncio.CancelledError:
                raise
            except JobFailed:
                raise
            except Exception as exc:
                for p in parts:
                    _unlink_quiet(p)
                await self._item_error(rec, msg_id, exc,
                                       context=f"upload msg={msg_id} "
                                               f"part={idx + 1}")
                return

        for p in parts:
            _unlink_quiet(p)
        self._resolve(rec, msg_id, "sent", f"reupload ({len(parts)} part(s))")
        self._bump(rec, downloaded=1, uploaded=1)
        log.info("UL msg=%d ok parts=%d file=%s", msg_id, len(parts), name)

    async def _upload_part(self, rec: JobRecord, control: JobControl,
                           dst_chat: int, msg: dict, path: str,
                           caption: str, entities: list[dict],
                           thumb: tuple | None, start: float) -> None:
        content = build_input_content(
            msg, path=path, thumb=thumb,
            caption_override=caption, entities_override=entities)
        speedwin = _SpeedWindow()
        snap = self._snapshot(rec, start, mode="uploading",
                              filename=os.path.basename(path))

        def on_ul(done: int, total: int) -> None:
            speedwin.add(done)
            snap.file_current = done
            snap.file_total = total
            snap.speed = speedwin.speed()

        async with self.ul_sem:
            if control.cancelled:
                raise asyncio.CancelledError
            watch = UploadWatch(self.user, path, on_progress=on_ul)
            try:
                await send_input(
                    self.user, dst_chat, content,
                    timeout=self.cfg.send_timeout)
            finally:
                watch.close()

    # -- album ------------------------------------------------------------
    async def _process_album(self, rec: JobRecord, control: JobControl,
                             src_chat: int, dst_chat: int, msg_id: int,
                             fetched: dict[int, dict | None],
                             album_id: int, start: float) -> None:
        cfg = self.cfg
        siblings = [m for m in fetched.values()
                    if m is not None and int(m.get("media_album_id") or 0)
                    == album_id]
        siblings.sort(key=lambda m: int(m["id"]))
        ids = sorted({int(m["id"]) for m in siblings} | {msg_id})

        self.store.begin_chunk(rec.job_key, ids)
        staging = self.staging / rec.job_key / f"album_{msg_id}"
        contents: list[dict] = []
        downloaded = 0

        async with self.dl_sem:
            for m in siblings:
                if control.cancelled:
                    raise asyncio.CancelledError
                try:
                    path, _sz, _el = await download_message_media(
                        self.user, m, src_chat, staging,
                        stall_timeout=cfg.download_stall_timeout)
                except MediaGone as exc:
                    log.info("album member %s gone: %s", m.get("id"), exc)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # one broken member aborts the album send (albums are
                    # all-or-nothing); resolve + stop so resume retries it
                    for i in ids:
                        self._resolve_store(
                            rec, i, "failed", f"album download: {exc}")
                    self._bump(rec, errors=len(ids))
                    raise JobFailed(
                        f"album download failed (msg {msg_id}): "
                        f"{classify(exc)}") from exc
                thumb = await download_source_thumbnail(
                    self.user, content_of(m), staging, int(m["id"]))
                contents.append(build_input_content(m, path=path,
                                                    thumb=thumb))
                downloaded += 1

        if not contents:
            for i in ids:
                self._resolve_store(rec, i, "skipped", "album media gone")
            return

        attempt = 0
        while True:
            if control.cancelled:
                raise asyncio.CancelledError
            try:
                async with self.ul_sem:
                    await send_album(self.user, dst_chat, contents,
                                     timeout=self.cfg.send_timeout)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                info = classify(exc)
                if info.kind is ErrorKind.FLOOD_WAIT:
                    self._bump(rec, flood_waits=1)
                    await asyncio.sleep(info.wait_seconds)
                    continue
                if info.retryable and attempt < self.cfg.retry_count:
                    attempt += 1
                    self._bump(rec, retries=1)
                    await asyncio.sleep(backoff_delay(attempt, rng=self.rng))
                    continue
                for i in ids:
                    self._resolve_store(rec, i, "failed", str(info)[:200])
                self._bump(rec, errors=len(ids))
                if info.stop_job:
                    raise JobFailed(
                        f"album upload failed (msg {msg_id}): {info}") from exc
                return

        for i in ids:
            self._resolve_store(rec, i, "sent", "album")
        self._bump(rec, downloaded=downloaded, uploaded=len(contents))
        log.info("album %s: %d/%d sent (msg %d)", album_id, len(contents),
                 len(siblings), msg_id)

    # -- shared send/retry ------------------------------------------------
    async def _send_with_retry(self, rec: JobRecord, control: JobControl,
                               dst_chat: int, msg_id: int,
                               content: dict, start: float) -> None:
        """Send one input content with classify-driven retries."""
        self.store.begin_chunk(rec.job_key, [msg_id])
        attempt = 0
        while True:
            if control.cancelled:
                raise asyncio.CancelledError
            try:
                async with self.ul_sem:
                    await send_input(self.user, dst_chat, content,
                                     timeout=self.cfg.send_timeout)
                self._resolve(rec, msg_id, "sent", "send")
                self._bump(rec, uploaded=1)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                info = classify(exc)
                if info.kind is ErrorKind.FLOOD_WAIT:
                    self._bump(rec, flood_waits=1)
                    log.warning("msg %d flood-wait %.0fs", msg_id,
                                info.wait_seconds)
                    await asyncio.sleep(info.wait_seconds)
                    continue
                if info.skip_message:
                    self._resolve(rec, msg_id, "skipped", str(info)[:200])
                    self._bump(rec, skipped=1)
                    return
                if info.stop_job:
                    self._resolve(rec, msg_id, "failed", str(info)[:200])
                    self._bump(rec, errors=1)
                    raise JobFailed(f"msg {msg_id}: {info}") from exc
                if attempt < self.cfg.retry_count:
                    attempt += 1
                    delay = backoff_delay(attempt, rng=self.rng)
                    log.info("msg %d retry %d/%d in %.1fs (%s)", msg_id,
                             attempt, self.cfg.retry_count, delay, info)
                    self._bump(rec, retries=1)
                    await asyncio.sleep(delay)
                    continue
                self._resolve(rec, msg_id, "failed", str(info)[:200])
                self._bump(rec, errors=1)
                raise JobFailed(
                    f"msg {msg_id} failed after {attempt} retries: {info}"
                ) from exc

    async def _item_error(self, rec: JobRecord, msg_id: int,
                          exc: Exception, *, context: str) -> None:
        """Resolve a per-message failure (marker is 'sending' right now)."""
        info = classify(exc)
        log.warning("%s: %s", context, info)
        if info.kind is ErrorKind.FLOOD_WAIT:
            # Resolve as failed so the id stays pending (failed ids are
            # re-yielded by iter_range) — but the job keeps running after
            # honouring the flood wait, like the legacy loop did.
            self._resolve(rec, msg_id, "failed", f"flood: {info}"[:200])
            self._bump(rec, errors=1, flood_waits=1)
            await asyncio.sleep(info.wait_seconds)
            return
        if info.stop_job:
            self._resolve(rec, msg_id, "failed", str(info)[:200])
            self._bump(rec, errors=1)
            raise JobFailed(f"{context}: {info}") from exc
        if info.skip_message or info.kind is ErrorKind.MISSING_MEDIA:
            self._resolve(rec, msg_id, "skipped", str(info)[:200])
            self._bump(rec, skipped=1)
            return
        # retryable (network/internal): leave pending for a later pass
        self._resolve(rec, msg_id, "failed", str(info)[:200])
        self._bump(rec, errors=1)

    # -- bookkeeping ------------------------------------------------------
    def _resolve(self, rec: JobRecord, msg_id: int, outcome: str,
                 detail: str) -> None:
        self.store.finish_chunk(rec.job_key, {msg_id: (outcome, detail)})

    _resolve_store = _resolve

    def _bump(self, rec: JobRecord, **counters: int) -> None:
        if counters:
            self.store.bump(rec.job_key, **counters)
            for k, v in counters.items():
                setattr(rec, k, getattr(rec, k, 0) + v)

    # ------------------------------------------------------------------
    def _snapshot(self, rec: JobRecord, start: float, *,
                  mode: str = "copy", filename: str = "") -> ProgressSnapshot:
        # re-read counters: the store is the source of truth (rec may be
        # stale after finish_chunk updates)
        fresh = self.store.get_job(rec.job_key) or rec
        elapsed = time.monotonic() - start
        done = max(0, fresh.current_msg_id - fresh.start_msg_id)
        remain = max(0, fresh.end_msg_id - fresh.current_msg_id + 1)
        avg = elapsed / done if done else 0.0
        return ProgressSnapshot(
            job_key=rec.job_key,
            mode=mode,
            current_msg=fresh.current_msg_id,
            total=fresh.total,
            downloaded=fresh.downloaded,
            uploaded=fresh.uploaded,
            skipped=fresh.skipped,
            errors=fresh.errors,
            eta=avg * remain,
            elapsed=elapsed,
            filename=filename,
            start_msg=fresh.start_msg_id,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _parse_identifier(value: str) -> int | str:
    v = str(value).strip()
    if v.lstrip("-").isdigit():
        return int(v)
    return v.lstrip("@")


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _split_large(path: str, limit: int) -> list[str]:
    """Split an oversized file into parts (thread-friendly wrapper)."""
    try:
        from file_splitter import split_file
        parts = split_file(path, limit, None)
        if isinstance(parts, (list, tuple)) and len(parts) > 1:
            return list(parts)
    except Exception:
        log.warning("split failed for %s", path, exc_info=True)
    return [path]
