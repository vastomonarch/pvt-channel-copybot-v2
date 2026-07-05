"""Optimized download/upload pipeline with aria2c and Telethon.

Architecture:
- aria2c for fast parallel downloads with resume support
- Telethon MTProto for uploads with progress callbacks
- Single progress message that transitions between phases
- Rate-limited updates (max every 2s or on significant change)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient, Button
from telethon.errors import (
    FloodWaitError,
    FileReferenceExpiredError,
    MessageDeleteForbiddenError,
)
from telethon.tl import types as tg_types

from progress_ui import Spinner, format_size, format_speed, format_time
from progress_ui.renderer import (
    box_bottom,
    box_line,
    box_separator,
    box_top,
    escape_html,
    progress_bar,
)

log = logging.getLogger("copybot.pipeline")

# ── Constants ────────────────────────────────────────────────────────

JOB_DIR = Path(__file__).resolve().parent / "_downloads"
JOB_DIR.mkdir(parents=True, exist_ok=True)

MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB Telegram limit
ARIA2_RPC_SECRET = None  # Set if aria2c is configured with auth
ARIA2_JSONRPC = "http://localhost:6800/jsonrpc"

# Rate limiting for progress edits
MIN_EDIT_INTERVAL = 2.0  # seconds
MIN_EDIT_DELTA_PCT = 2.0  # only edit if percent changed by this much


# ── Data Classes ─────────────────────────────────────────────────────

@dataclass
class TransferProgress:
    """Tracks progress for a single download or upload operation."""

    phase: str = "idle"  # idle, downloading, verifying, uploading, done, error
    filename: str = ""
    total_bytes: int = 0
    transferred_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    start_time: float = field(default_factory=time.monotonic)
    last_update: float = 0.0
    last_pct: float = -1.0
    error: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, (self.transferred_bytes / self.total_bytes) * 100)

    @property
    def should_update(self) -> bool:
        """Check if enough time/progress has passed to warrant an edit."""
        now = time.monotonic()
        if now - self.last_update < MIN_EDIT_INTERVAL:
            return False
        if abs(self.percent - self.last_pct) < MIN_EDIT_DELTA_PCT:
            return False
        return True

    def mark_updated(self) -> None:
        self.last_update = time.monotonic()
        self.last_pct = self.percent


@dataclass
class JobState:
    """Complete state for a copy job."""

    job_key: str
    src_identifier: str | int
    dst_identifier: str | int
    start_msg_id: int
    end_msg_id: int
    current_msg_id: int
    downloaded: int = 0
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    download: TransferProgress = field(default_factory=TransferProgress)
    upload: TransferProgress = field(default_factory=TransferProgress)
    spinner: Spinner = field(default_factory=Spinner)

    @property
    def total(self) -> int:
        return self.end_msg_id - self.start_msg_id + 1

    @property
    def progress_pct(self) -> float:
        done = max(0, self.current_msg_id - self.start_msg_id)
        return (done / max(1, self.total)) * 100


# ── Progress Renderer ────────────────────────────────────────────────

def render_job_progress(state: JobState) -> str:
    """Render the combined download/upload progress dashboard."""
    spin = state.spinner.next()
    dl = state.download
    ul = state.upload

    # Determine active phase
    if dl.phase == "downloading":
        header = f"{spin} 📥 Downloading"
        bar = progress_bar(dl.percent, width=20)
        pct_str = f"{dl.percent:.1f}%"
        size_line = f"{format_size(dl.transferred_bytes)} / {format_size(dl.total_bytes)}"
        speed_line = format_speed(dl.speed_bps) if dl.speed_bps > 0 else "—"
        eta_line = format_time(dl.eta_seconds) if dl.eta_seconds > 0 else "—"
    elif ul.phase == "uploading":
        header = f"{spin} 📤 Uploading"
        bar = progress_bar(ul.percent, width=20)
        pct_str = f"{ul.percent:.1f}%"
        size_line = f"{format_size(ul.transferred_bytes)} / {format_size(ul.total_bytes)}"
        speed_line = format_speed(ul.speed_bps) if ul.speed_bps > 0 else "—"
        eta_line = format_time(ul.eta_seconds) if ul.eta_seconds > 0 else "—"
    elif dl.phase == "done" and ul.phase == "done":
        header = "✅ Upload completed successfully"
        bar = progress_bar(100.0, width=20)
        pct_str = "100%"
        size_line = format_size(ul.total_bytes)
        speed_line = format_speed(ul.total_bytes / max(ul.elapsed_seconds, 0.01))
        eta_line = format_time(ul.elapsed_seconds)
    elif dl.phase == "error" or ul.phase == "error":
        header = f"❌ {dl.error or ul.error or 'Transfer failed'}"
        bar = progress_bar(dl.percent or ul.percent, width=20)
        pct_str = f"{dl.percent or ul.percent:.1f}%"
        size_line = "—"
        speed_line = "—"
        eta_line = "—"
    else:
        # Processing/idle
        header = f"{spin} ⏳ Processing"
        bar = progress_bar(state.progress_pct, width=20)
        pct_str = f"{state.progress_pct:.1f}%"
        size_line = f"msg {state.current_msg_id} / {state.end_msg_id}"
        speed_line = "—"
        eta_line = "—"

    # Filename (truncate if long)
    fname = dl.filename or ul.filename or ""
    if len(fname) > 28:
        fname = fname[:25] + "..."

    lines = [
        box_top(34),
        box_line(header, 34),
        box_line(f"📄 {fname}" if fname else f"📄 msg {state.current_msg_id}", 34),
        box_separator(34),
        box_line(f"{bar}  {pct_str}", 34),
        box_line("", 34),
        box_line(f"📦  {size_line}", 34),
        box_line(f"⚡  {speed_line}", 34),
        box_line(f"⏳  ETA      {eta_line}", 34),
        box_separator(34),
        box_line(f"DL: {state.downloaded} · UL: {state.uploaded} · skip: {state.skipped}", 34),
        box_bottom(34),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


def render_job_buttons(job_key: str) -> list[list[Button]]:
    """Inline buttons for job progress."""
    return [
        [
            Button.inline("🔄 Refresh", data=f"refresh:{job_key}".encode()),
            Button.inline("⏹ Cancel", data=f"cancel:{job_key}".encode()),
        ]
    ]


# ── Aria2c Integration ──────────────────────────────────────────────

class Aria2cDownloader:
    """Async wrapper around aria2c for fast parallel downloads."""

    def __init__(self, rpc_url: str = ARIA2_JSONRPC, secret: str | None = None):
        self.rpc_url = rpc_url
        self.secret = secret

    async def _rpc_call(self, method: str, params: list | None = None) -> Any:
        """Make an RPC call to aria2c."""
        import aiohttp

        payload = {
            "jsonrpc": "2.0",
            "id": "copybot",
            "method": method,
            "params": params or [],
        }
        if self.secret:
            payload["params"].insert(0, f"token:{self.secret}")

        async with aiohttp.ClientSession() as session:
            async with session.post(self.rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"aria2c RPC error: {data['error']}")
                return data.get("result")

    async def download(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, int], Any] | None = None,
    ) -> str | None:
        """Download a file using aria2c with resume support.

        Returns the output path on success, None on failure.
        """
        try:
            options = {
                "dir": str(Path(output_path).parent),
                "out": Path(output_path).name,
                "continue": "true",
                "auto-file-renaming": "false",
                "allow-overwrite": "true",
            }

            # Add the download
            result = await self._rpc_call("aria2.addUri", [[url], options])
            gid = result

            # Poll for completion
            while True:
                status = await self._rpc_call("aria2.tellStatus", [gid])
                state = status.get("status", "")

                if state == "complete":
                    return output_path
                elif state == "error":
                    error_code = status.get("errorCode", "?")
                    error_msg = status.get("errorMessage", "unknown")
                    log.error("aria2c download failed: %s - %s", error_code, error_msg)
                    return None
                elif state == "removed":
                    log.warning("aria2c download removed: %s", gid)
                    return None

                # Report progress
                if progress_callback:
                    completed = int(status.get("completedLength", 0))
                    total = int(status.get("totalLength", 0))
                    speed = int(status.get("downloadSpeed", 0))
                    try:
                        result = progress_callback(completed, total)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

        except Exception as e:
            log.error("aria2c download error: %s", e)
            return None

    async def get_status(self, gid: str) -> dict | None:
        """Get download status."""
        try:
            return await self._rpc_call("aria2.tellStatus", [gid])
        except Exception:
            return None

    async def pause(self, gid: str) -> bool:
        """Pause a download."""
        try:
            await self._rpc_call("aria2.pause", [gid])
            return True
        except Exception:
            return False

    async def unpause(self, gid: str) -> bool:
        """Resume a paused download."""
        try:
            await self._rpc_call("aria2.unpause", [gid])
            return True
        except Exception:
            return False

    async def remove(self, gid: str) -> bool:
        """Remove a download."""
        try:
            await self._rpc_call("aria2.remove", [gid])
            return True
        except Exception:
            return False


# ── Telethon Upload with Progress ────────────────────────────────────

async def upload_with_progress(
    client: TelegramClient,
    entity: Any,
    file_path: str,
    progress: TransferProgress,
    caption: str | None = None,
    formatting_entities: list | None = None,
    attributes: list | None = None,
    mime_type: str | None = None,
    supports_streaming: bool = False,
    force_document: bool = False,
    thumb: str | None = None,
    on_progress: Callable[[TransferProgress], Any] | None = None,
) -> Any:
    """Upload a file using Telethon with progress tracking.

    Args:
        on_progress: Optional async callback invoked on each significant
            progress tick.  Used by the caller to edit a Telegram message.

    Returns the sent message on success, None on failure.
    """
    if not os.path.exists(file_path):
        log.error("upload file not found: %s", file_path)
        return None

    file_size = os.path.getsize(file_path)
    progress.phase = "uploading"
    progress.total_bytes = file_size
    progress.transferred_bytes = 0
    progress.start_time = time.monotonic()
    progress.last_update = 0
    progress.last_pct = -1

    last_bytes = 0
    last_time = time.monotonic()
    _notify_handle = [None]  # mutable container for closure

    async def _progress_cb(current: int, total: int) -> None:
        nonlocal last_bytes, last_time
        now = time.monotonic()
        dt = now - last_time

        # Always update the dataclass so callers see fresh numbers
        progress.transferred_bytes = current
        progress.total_bytes = total

        if dt > 0.3 and current > last_bytes:
            speed = (current - last_bytes) / dt
            progress.speed_bps = speed * 0.3 + progress.speed_bps * 0.7
            remaining = total - current
            progress.eta_seconds = remaining / progress.speed_bps if progress.speed_bps > 0 else 0
            progress.elapsed_seconds = now - progress.start_time
            last_bytes = current
            last_time = now
            # Throttled notification to caller (every ~2 s or 2% change)
            if on_progress and progress.should_update:
                progress.mark_updated()
                try:
                    result = on_progress(progress)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    log.debug("on_progress callback error: %s", exc)

    try:
        result = await client.send_file(
            entity=entity,
            file=file_path,
            caption=caption,
            formatting_entities=formatting_entities,
            attributes=attributes,
            mime_type=mime_type,
            supports_streaming=supports_streaming,
            force_document=force_document,
            thumb=thumb,
            progress_callback=_progress_cb,
        )

        # Mark complete
        progress.transferred_bytes = file_size
        progress.elapsed_seconds = time.monotonic() - progress.start_time
        if progress.elapsed_seconds > 0:
            progress.speed_bps = file_size / progress.elapsed_seconds
        progress.phase = "done"
        # Final notification
        if on_progress:
            try:
                result2 = on_progress(progress)
                if asyncio.iscoroutine(result2):
                    await result2
            except Exception:
                pass

        return result

    except FloodWaitError:
        raise
    except Exception as e:
        log.error("upload failed: %s", e)
        progress.phase = "error"
        progress.error = str(e)
        return None


# ── Download with Progress (Telethon fallback) ──────────────────────

async def download_with_progress(
    client: TelegramClient,
    msg: Any,
    output_dir: str,
    progress: TransferProgress,
    src_entity: Any = None,
    retries: int = 2,
    on_progress: Callable[[TransferProgress], Any] | None = None,
) -> tuple[str | None, int, float]:
    """Download media from a message with progress tracking.

    Args:
        on_progress: Optional async callback invoked on each significant
            progress tick.  Used by the caller to edit a Telegram message.

    Returns (path, size_bytes, elapsed_seconds).
    """
    progress.phase = "downloading"
    progress.filename = ""
    progress.transferred_bytes = 0
    progress.start_time = time.monotonic()
    progress.last_update = 0
    progress.last_pct = -1

    # Get filename from media
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None) if media else None
    if doc:
        for attr in getattr(doc, "attributes", []):
            if hasattr(attr, "file_name"):
                progress.filename = attr.file_name
                break
    if not progress.filename:
        progress.filename = f"msg_{msg.id}"

    # Get total size
    if doc:
        progress.total_bytes = getattr(doc, "size", 0) or 0
    else:
        photo = getattr(media, "photo", None) if media else None
        if photo:
            sizes = getattr(photo, "sizes", None) or []
            for s in sizes:
                sz = getattr(s, "size", None)
                if sz:
                    progress.total_bytes = max(progress.total_bytes, int(sz))

    last_bytes = 0
    last_time = time.monotonic()

    async def _progress_cb(current: int, total: int) -> None:
        nonlocal last_bytes, last_time
        now = time.monotonic()
        dt = now - last_time

        # Always update the dataclass
        progress.transferred_bytes = current
        if total > 0:
            progress.total_bytes = total

        if dt > 0.3 and current > last_bytes:
            speed = (current - last_bytes) / dt
            progress.speed_bps = speed * 0.3 + progress.speed_bps * 0.7
            remaining = total - current
            if progress.speed_bps > 0:
                progress.eta_seconds = remaining / progress.speed_bps
            progress.elapsed_seconds = now - progress.start_time
            last_bytes = current
            last_time = now
            # Throttled notification to caller
            if on_progress and progress.should_update:
                progress.mark_updated()
                try:
                    result = on_progress(progress)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    log.debug("on_progress callback error: %s", exc)

    fresh = msg
    last_err = None

    for attempt in range(retries + 1):
        try:
            t0 = time.monotonic()
            path = await client.download_media(
                fresh,
                file=str(output_dir) + "/",
                progress_callback=_progress_cb,
            )
            elapsed = time.monotonic() - t0

            if path:
                actual = os.path.getsize(path) if os.path.exists(path) else 0
                progress.transferred_bytes = actual
                progress.elapsed_seconds = elapsed
                if elapsed > 0:
                    progress.speed_bps = actual / elapsed
                progress.phase = "done"
                return path, actual, elapsed

            return None, 0, 0.0

        except FileReferenceExpiredError:
            try:
                lookup = src_entity if src_entity is not None else fresh.peer_id
                fresh_list = await client.get_messages(lookup, ids=fresh.id)
                fresh = fresh_list[0] if isinstance(fresh_list, list) else fresh_list
                if fresh is None:
                    return None, 0, 0.0
            except Exception as e:
                last_err = e
                await asyncio.sleep(3)
        except FloodWaitError:
            raise
        except (ConnectionError, asyncio.TimeoutError) as e:
            last_err = e
            await asyncio.sleep(5)
        except Exception as e:
            log.warning("download_media raised %s for msg=%d", e, msg.id)
            last_err = e
            break

    log.warning("download failed msg=%d attempts=%d err=%s", msg.id, retries + 1, last_err)
    progress.phase = "error"
    progress.error = str(last_err) if last_err else "download failed"
    return None, 0, 0.0


# ── Cleanup ──────────────────────────────────────────────────────────

def cleanup_file(path: str | None) -> None:
    """Safely remove a temporary file."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except (OSError, FileNotFoundError):
        pass


def cleanup_job_dir(job_key: str) -> None:
    """Remove all files for a specific job."""
    import glob
    pattern = str(JOB_DIR / f"*{job_key}*")
    for f in glob.glob(pattern):
        cleanup_file(f)
