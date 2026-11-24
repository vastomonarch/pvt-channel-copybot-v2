"""Media transfers through TDLib: download, upload, progress, cancellation.

Design notes
------------
* **Downloads** use TDLib's native ``downloadFile``; progress and completion
  arrive as ``updateFile`` updates.  A file that TDLib already has locally
  (``local.is_downloading_completed`` and the path still exists with the
  right size) is reused — we never re-download verified media.
* **Uploads** happen inside ``sendMessage``/``sendMessageAlbum`` when the
  input content carries an ``inputFileLocal``; TDLib uploads natively and
  reports progress through ``updateFile`` (``remote.uploaded_size``).
* **Filename preservation**: TDLib stores downloads under its own names in
  its file cache.  Before uploading we hard-link (or symlink) the file to
  ``<staging>/<job>/msg_<id>/<original-name>`` so the *sent* file keeps the
  original filename — no data is copied.
* **Cancellation**: downloads are cancelled with ``cancelDownloadFile``;
  cooperative cancellation stops the pipeline before the next send (an
  in-flight Telegram upload cannot be aborted server-side — see
  docs/BEHAVIOR-CHANGES.md).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from errors import ClassifiedError, ErrorKind, TdError, classify
from tdlib_client import TDClient
from tdlib_messages import caption_of, media_file, media_name, media_size

log = logging.getLogger("copybot.tdlib.media")

__all__ = [
    "DownloadOutcome",
    "MediaGone",
    "sanitize_filename",
    "ensure_named_copy",
    "download_file",
    "download_message_media",
    "build_input_content",
    "UploadWatch",
    "DownloadWatch",
    "content_kind",
    "utf16_len",
    "make_text_entities",
]

ProgressCB = Callable[[int, int], Any]   # (transferred, total)


class MediaGone(Exception):
    """Media is empty / revoked / permanently unavailable → skip message."""


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]")


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Strip path components and control characters; bound the length."""
    name = os.path.basename(name or "").strip()
    name = _BAD_CHARS.sub("_", name)
    name = name.lstrip(".")           # no hidden-file surprises
    while "__" in name:
        name = name.replace("__", "_")
    if not name:
        return fallback
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 12:
            name = stem[: 180 - len(ext) - 1] + "." + ext
        else:
            name = name[:180]
    return name


def ensure_named_copy(src: str, dest_dir: str | Path,
                      original_name: str) -> str:
    """Return a path whose *basename* is the original file name.

    Uses hard-links (falls back to symlinks) so large files are never
    duplicated on disk.  If linking is impossible the source path is
    returned unchanged (the filename may then differ — logged loudly).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(original_name)
    if os.path.basename(src) == safe:
        return src
    dest = dest_dir / safe
    if dest.exists():
        try:
            if dest.stat().st_size == os.path.getsize(src):
                return str(dest)
        except OSError:
            pass
        dest.unlink(missing_ok=True)
    try:
        os.link(src, dest)
        return str(dest)
    except OSError:
        pass
    try:
        os.symlink(os.path.abspath(src), dest)
        return str(dest)
    except OSError as exc:
        log.warning("cannot link %s → %s (%s); uploading under TDLib name",
                    src, dest, exc)
        return src


def _file_ok(path: str, expected: int = 0) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size <= 0:
        return False
    if expected and st.st_size != expected:
        # Photos may be re-encoded? TDLib stores verbatim — mismatch means
        # a partial file; require full size.
        return False
    return True


# ---------------------------------------------------------------------------
# updateFile watching
# ---------------------------------------------------------------------------
class DownloadWatch:
    """Watch one file id through ``updateFile`` until terminal state."""

    def __init__(self, client: TDClient, file_id: int, *,
                 on_progress: ProgressCB | None = None,
                 expected: int = 0, stall_timeout: float = 300.0):
        self.client = client
        self.file_id = int(file_id)
        self.on_progress = on_progress
        self.expected = expected
        self.stall_timeout = stall_timeout

        self.transferred = 0
        self.total = expected
        self.active = False
        self._last_bytes = 0
        self._last_change = time.monotonic()
        self._future: asyncio.Future = client.loop.create_future()
        self._key = client.router.subscribe(self._on_update, ("updateFile",))

    # -- subscription ----------------------------------------------------
    def _on_update(self, payload: dict) -> None:
        file = payload.get("file") or {}
        if int(file.get("id", 0)) != self.file_id:
            return
        local = file.get("local") or {}
        size = int(file.get("size") or file.get("expected_size") or 0)
        if size:
            self.total = size
        downloaded = int(local.get("downloaded_size") or 0)
        if downloaded != self._last_bytes:
            self._last_bytes = downloaded
            self._last_change = time.monotonic()
        self.active = bool(local.get("is_downloading_active"))
        if local.get("is_downloading_completed"):
            self.transferred = int(local.get("downloaded_size") or size or 0)
            if not self._future.done():
                self._future.set_result(str(local.get("path") or ""))
            return
        self.transferred = downloaded
        self._notify()

    def _notify(self) -> None:
        if self.on_progress is None:
            return
        try:
            res = self.on_progress(self.transferred, self.total)
            if asyncio.iscoroutine(res):
                self.client.router.tasks.spawn(res, name="dl-progress")
        except Exception:  # progress must never break a transfer
            log.debug("download progress callback failed", exc_info=True)

    # -- awaiting --------------------------------------------------------
    async def wait(self) -> str:
        """Return the local path when downloaded; raise on stall/failure."""
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(self._future),
                        timeout=max(1.0, self.stall_timeout))
                except asyncio.TimeoutError:
                    if not self.active and time.monotonic() - self._last_change \
                            >= self.stall_timeout:
                        raise TdError(
                            500, f"download of file {self.file_id} stalled "
                                 f"(no progress for {self.stall_timeout:.0f}s)")
                    # still active → keep waiting (TDLib retries internally)
        finally:
            self.close()

    def fail(self, exc: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def cancel(self) -> None:
        """Ask TDLib to stop the download (cooperative cancellation)."""
        self.client.router.tasks.spawn(
            self.client.request({"@type": "cancelDownloadFile",
                                 "file_id": self.file_id,
                                 "only_if_pending": False}),
            name=f"cancelDownloadFile:{self.file_id}")
        self.fail(asyncio.CancelledError())

    def close(self) -> None:
        self.client.router.unsubscribe(self._key)


class UploadWatch:
    """Track upload progress for a local path being sent."""

    def __init__(self, client: TDClient, local_path: str, *,
                 on_progress: ProgressCB | None = None,
                 expected: int = 0):
        self.client = client
        self.local_path = os.path.abspath(local_path)
        self.on_progress = on_progress
        self.expected = expected
        self.transferred = 0
        self.total = expected
        self._key = client.router.subscribe(self._on_update, ("updateFile",))

    def _on_update(self, payload: dict) -> None:
        file = payload.get("file") or {}
        local = file.get("local") or {}
        path = str(local.get("path") or "")
        if not path or os.path.abspath(path) != self.local_path:
            return
        remote = file.get("remote") or {}
        size = int(file.get("size") or file.get("expected_size") or 0)
        if size:
            self.total = size
        self.transferred = int(remote.get("uploaded_size") or 0)
        if self.on_progress is None:
            return
        try:
            res = self.on_progress(self.transferred, self.total)
            if asyncio.iscoroutine(res):
                self.client.router.tasks.spawn(res, name="ul-progress")
        except Exception:
            log.debug("upload progress callback failed", exc_info=True)

    def close(self) -> None:
        self.client.router.unsubscribe(self._key)


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------
async def download_file(client: TDClient, file_id: int, *,
                        expected: int = 0,
                        on_progress: ProgressCB | None = None,
                        stall_timeout: float = 300.0,
                        priority: int = 32) -> tuple[str, int]:
    """Download *file_id* natively; return ``(path, size)``."""
    watch = DownloadWatch(client, file_id, on_progress=on_progress,
                          expected=expected, stall_timeout=stall_timeout)
    try:
        res = await client.request({
            "@type": "downloadFile",
            "file_id": file_id,
            "priority": priority,
            "offset": 0,
            "limit": 0,
            "synchronous": False,
        })
        file = res.get("file") or {}
        local = file.get("local") or {}
        if local.get("is_downloading_completed") and local.get("path"):
            path = str(local["path"])
            if _file_ok(path, expected):
                return path, os.path.getsize(path)
        # else: completion will arrive via updateFile
        path = await watch.wait()
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if expected and size and size != expected:
            raise TdError(500, f"size mismatch for file {file_id}: "
                               f"{size} != {expected}")
        return path, size
    finally:
        watch.close()


async def download_message_media(
    client: TDClient, message: dict, chat_id: int, staging_dir: str | Path, *,
    on_progress: ProgressCB | None = None,
    stall_timeout: float = 300.0,
    max_refetch: int = 2,
) -> tuple[str, int, float]:
    """Download the media of *message* into *staging_dir*.

    Returns ``(path, size, elapsed_seconds)``.

    Raises:
        MediaGone    — media empty / revoked / reference permanently gone
        TdError      — anything else (classified by the caller)

    Already-downloaded files are verified and reused (no second transfer).
    Expired file references trigger a re-fetch of the message (fresh
    reference) and one retry loop, mirroring the legacy behaviour.
    """
    content = message.get("content") or {}
    file_obj = media_file(content)
    if not file_obj:
        raise MediaGone("message has no downloadable file")
    file_id = int(file_obj.get("id", 0))
    if not file_id:
        raise MediaGone("file id is 0 (not available)")
    expected = int(file_obj.get("size") or file_obj.get("expected_size") or 0)

    original_name = media_name(content) or (
        f"msg_{message.get('id', 0)}.jpg"
        if content.get("@type") == "messagePhoto" else f"msg_{message.get('id', 0)}")

    attempts = 0
    while True:
        attempts += 1
        local = file_obj.get("local") or {}
        if local.get("is_downloading_completed") and local.get("path"):
            path = str(local["path"])
            if _file_ok(path, expected):
                named = ensure_named_copy(path, staging_dir, original_name)
                log.info("DL reuse msg=%s file=%s (%s)",
                         message.get("id"), original_name,
                         _fmt_size(os.path.getsize(named)))
                return named, os.path.getsize(named), 0.0
            log.debug("cached file missing/partial, re-downloading: %s", path)

        t0 = time.monotonic()
        try:
            path, size = await download_file(
                client, file_id, expected=expected, on_progress=on_progress,
                stall_timeout=stall_timeout)
        except TdError as exc:
            info = classify(exc)
            if info.kind is ErrorKind.AUTH:
                raise
            if info.needs_refetch and attempts <= max_refetch:
                log.info("media ref expired msg=%s — refetching message",
                         message.get("id"))
                fresh = await _refetch_message(client, chat_id,
                                               int(message["id"]))
                if fresh is None:
                    raise MediaGone("message disappeared during refetch")
                message = fresh
                content = message.get("content") or {}
                file_obj = media_file(content)
                if not file_obj or not int(file_obj.get("id") or 0):
                    raise MediaGone("media gone after refetch")
                file_id = int(file_obj["id"])
                expected = int(file_obj.get("size")
                               or file_obj.get("expected_size") or 0)
                continue
            if info.kind is ErrorKind.MISSING_MEDIA:
                raise MediaGone(info.message) from exc
            raise
        elapsed = time.monotonic() - t0
        named = ensure_named_copy(path, staging_dir, original_name)
        actual = os.path.getsize(named) if os.path.exists(named) else 0
        return named, actual, elapsed


async def _refetch_message(client: TDClient, chat_id: int,
                           message_id: int) -> dict | None:
    try:
        res = await client.request(
            {"@type": "getMessage", "chat_id": chat_id,
             "message_id": message_id})
    except TdError as exc:
        if classify(exc).skip_message or exc.code == 404:
            return None
        raise
    return res if res.get("@type") == "message" else None


# ---------------------------------------------------------------------------
# Input content construction (upload side)
# ---------------------------------------------------------------------------
def utf16_len(s: str) -> int:
    """Length of *s* in UTF-16 code units (Telegram entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def make_text_entities(text: str, specs: list[tuple[int, int, dict]]
                       ) -> list[dict]:
    """Build TDLib text entities from ``(char_offset, char_len, type)``.

    Offsets here are *Python string indices*; TDLib wants UTF-16 units, so
    they are converted.
    """
    entities: list[dict] = []
    for off, length, etype in specs:
        prefix = text[:off]
        u16_off = utf16_len(prefix)
        u16_len = utf16_len(text[off:off + length])
        if u16_len <= 0:
            continue
        entity = {"@type": "textEntity", "offset": u16_off,
                  "length": u16_len, "type": etype}
        entities.append(entity)
    return entities


def content_kind(content: dict) -> str:
    return {
        "messagePhoto": "photo",
        "messageVideo": "video",
        "messageAnimation": "animation",
        "messageAudio": "audio",
        "messageVoiceNote": "voice_note",
        "messageVideoNote": "video_note",
        "messageSticker": "sticker",
        "messageDocument": "document",
    }.get(content.get("@type", ""), "document")


def _input_file(path: str) -> dict:
    return {"@type": "inputFileLocal", "path": str(path)}


def _source_thumb(content: dict) -> dict | None:
    """thumbnail/minithumbnail descriptor of the source media, if any."""
    for key in ("document", "video", "animation", "thumbnail"):
        obj = content.get(key)
        if isinstance(obj, dict):
            th = obj.get("thumbnail")
            if isinstance(th, dict) and (th.get("file") or {}).get("id"):
                return th
    photo = content.get("photo")
    if isinstance(photo, dict):
        th = photo.get("thumbnail")
        if isinstance(th, dict) and (th.get("file") or {}).get("id"):
            return th
    return None


async def download_source_thumbnail(client: TDClient, content: dict,
                                    staging_dir: str | Path,
                                    msg_id: int) -> tuple[str, int, int] | None:
    """Download the source's own thumbnail → (path, width, height)."""
    th = _source_thumb(content)
    if not th:
        return None
    file_obj = th.get("file") or {}
    file_id = int(file_obj.get("id") or 0)
    if not file_id:
        return None
    try:
        path, _size = await download_file(
            client, file_id,
            expected=int(file_obj.get("size") or 0), stall_timeout=60.0)
    except (TdError, asyncio.CancelledError):
        return None
    except Exception:
        log.debug("thumbnail download failed", exc_info=True)
        return None
    named = ensure_named_copy(path, staging_dir, f"thumb_{msg_id}.jpg")
    return named, int(th.get("width") or 0), int(th.get("height") or 0)


def build_input_content(
    message: dict, *,
    path: str | None = None,
    thumb: tuple[str, int, int] | None = None,
    caption_override: str | None = None,
    entities_override: list[dict] | None = None,
) -> dict:
    """Build the ``InputMessageContent`` for re-sending *message*.

    ``path`` is required for media messages (the local file to upload).
    ``thumb`` is ``(path, width, height)`` for the thumbnail, if available.
    Captions keep the original text and formatting entities (capped at the
    Telegram 1024-char limit, like the legacy implementation).
    """
    content = message.get("content") or {}
    ctype = content.get("@type", "")

    if ctype == "messageText":
        text = (content.get("text") or {})
        opts = content.get("link_preview_options")
        return {
            "@type": "inputMessageText",
            "text": {"@type": "formattedText",
                     "text": str(text.get("text") or ""),
                     "entities": list(text.get("entities") or [])},
            "link_preview_options": opts,
            "clear_draft": False,
        }

    if ctype == "messageContact":
        return {"@type": "inputMessageContact",
                "contact": dict(content.get("contact") or {})}

    if ctype == "messageLocation":
        return {"@type": "inputMessageLocation",
                "location": dict(content.get("location") or {})}

    if ctype == "messageVenue":
        return {"@type": "inputMessageVenue",
                "venue": dict(content.get("venue") or {})}

    if ctype == "messagePoll":
        return _poll_as_text(content)

    # ── media ───────────────────────────────────────────────────────
    if path is None:
        raise ValueError("path is required for media content")

    text, entities = caption_of(content)
    if caption_override is not None:
        text, entities = caption_override, entities_override or []
    if len(text) > 1024:
        text = text[:1020] + "…"
        entities = [e for e in entities
                    if int(e.get("offset", 0)) < 1024]
    caption = {"@type": "formattedText", "text": text, "entities": entities}

    thumb_input: dict | None = None
    if thumb:
        tpath, tw, th_ = thumb
        thumb_input = {
            "@type": "inputThumbnail",
            "thumbnail": _input_file(tpath),
            "width": int(tw or 0),
            "height": int(th_ or 0),
        }

    kind = content_kind(content)

    if kind == "photo":
        sizes = (content.get("photo") or {}).get("sizes") or []
        width = height = 0
        for sz in sizes:
            if int(sz.get("width") or 0) > width:
                width, height = int(sz.get("width") or 0), int(sz.get("height") or 0)
        return {
            "@type": "inputMessagePhoto",
            "photo": {
                "@type": "inputPhoto",
                "photo": _input_file(path),
                "thumbnail": thumb_input,
                "video": None,
                "added_sticker_file_ids": [],
                "width": width,
                "height": height,
            },
            "caption": caption,
            "show_caption_above_media": bool(
                content.get("show_caption_above_media")),
            "self_destruct_type": None,
            "has_spoiler": bool(content.get("has_spoiler")),
        }

    if kind == "video":
        video = content.get("video") or {}
        return {
            "@type": "inputMessageVideo",
            "video": {
                "@type": "inputVideo",
                "video": _input_file(path),
                "thumbnail": thumb_input,
                "cover": None,
                "start_timestamp": int(content.get("start_timestamp") or 0),
                "added_sticker_file_ids": [],
                "duration": int(video.get("duration") or 0),
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "supports_streaming": bool(video.get("supports_streaming")),
            },
            "caption": caption,
            "show_caption_above_media": bool(
                content.get("show_caption_above_media")),
            "self_destruct_type": None,
            "has_spoiler": bool(content.get("has_spoiler")),
        }

    if kind == "animation":
        animation = content.get("animation") or {}
        return {
            "@type": "inputMessageAnimation",
            "animation": {
                "@type": "inputAnimation",
                "animation": _input_file(path),
                "thumbnail": thumb_input,
                "added_sticker_file_ids": [],
                "duration": int(animation.get("duration") or 0),
                "width": int(animation.get("width") or 0),
                "height": int(animation.get("height") or 0),
            },
            "caption": caption,
            "show_caption_above_media": False,
            "has_spoiler": False,
        }

    if kind == "audio":
        audio = content.get("audio") or {}
        return {
            "@type": "inputMessageAudio",
            "audio": {
                "@type": "inputAudio",
                "audio": _input_file(path),
                "album_cover_thumbnail": None,
                "duration": int(audio.get("duration") or 0),
                "title": str(audio.get("title") or ""),
                "performer": str(audio.get("performer") or ""),
            },
            "caption": caption,
        }

    if kind == "voice_note":
        vn = content.get("voice_note") or {}
        return {
            "@type": "inputMessageVoiceNote",
            "voice_note": {
                "@type": "inputVoiceNote",
                "voice_note": _input_file(path),
                "duration": int(vn.get("duration") or 0),
                "waveform": vn.get("waveform") or "",
            },
            "caption": caption,
            "self_destruct_type": None,
        }

    if kind == "video_note":
        vn = content.get("video_note") or {}
        return {
            "@type": "inputMessageVideoNote",
            "video_note": {
                "@type": "inputVideoNote",
                "video_note": _input_file(path),
                "thumbnail": thumb_input,
                "duration": int(vn.get("duration") or 0),
                "length": int(vn.get("length") or 0),
            },
            "self_destruct_type": None,
        }

    # documents (and stickers, which we re-send as documents)
    return {
        "@type": "inputMessageDocument",
        "document": {
            "@type": "inputDocument",
            "document": _input_file(path),
            "thumbnail": thumb_input,
            "disable_content_type_detection": False,
        },
        "caption": caption,
    }


def _poll_as_text(content: dict) -> dict:
    """Reproduce the legacy plain-text poll card (with real bold entities)."""
    poll = content.get("poll") or {}
    lines: list[str] = ["📊 Poll"]
    specs: list[tuple[int, int, dict]] = [(0, 5, {"@type": "textEntityTypeBold"})]
    question = poll.get("question") or {}
    q_text = str(question.get("text") or "")
    if q_text:
        lines.append(f"Question: {q_text}")
        q_line = len(lines) - 1
        off = sum(len(l) + 1 for l in lines[:q_line]) + len("Question: ")
        specs.append((off, len(q_text),
                      {"@type": "textEntityTypeBold"}))
    for i, opt in enumerate(poll.get("options") or [], 1):
        o_text = str((opt.get("text") or {}).get("text") or "")
        lines.append(f"  {i}. {o_text or '(empty option)'}")
    if poll.get("is_closed"):
        lines.append("(Poll closed)")
    text = "\n".join(lines)
    return {
        "@type": "inputMessageText",
        "text": {"@type": "formattedText", "text": text,
                 "entities": make_text_entities(text, specs)},
        "link_preview_options": None,
        "clear_draft": False,
    }


def _fmt_size(n: float) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024.0
    return f"{f:.1f} TB"
