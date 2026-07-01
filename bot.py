"""
Telegram Private Channel Copy Bot  (with Bot-driven live progress)
==================================================================
A Telethon Userbot that copies messages from private channels to destination
channels WITHOUT the "Forwarded from" tag.  Live progress (bar, ETA, download
speed) is delivered through a SECOND Telegram Bot client driven by the
provided BOT_TOKEN, so the user account does not get spammed with edits.

Quickstart
----------
1. Put API_ID, API_HASH and BOT_TOKEN in `.env`  (see `.env.example`).
2. Open Telegram, find your bot by username, send `/start`.
3. Run `python bot.py`.  Issue `.pvtcopydl ...` somewhere - live progress
   appears in the chat with your bot, complete with bar / ETA / speed and
   inline Refresh + Cancel buttons.
"""

import asyncio
import json
import os
import re
import sys
import time
import random
import hashlib
import pathlib
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, utils as tg_utils
from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    MessageIdInvalidError,
    MessageDeleteForbiddenError,
    SlowModeWaitError,
    FileReferenceExpiredError,
    MediaEmptyError,
)
from telethon.tl import functions as tg_functions
from telethon.tl import types as tg_types
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    InputPeerChannel,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
    DocumentAttributeAudio,
    DocumentAttributeImageSize,
    DocumentAttributeAnimated,
)

# ── Progress UI ─────────────────────────────────────────────────────
from progress_ui import (
    Spinner,
    format_percent,
    format_size,
    format_speed,
    format_time,
    progress_bar,
)
from progress_ui.renderer import (
    render_copy_completed,
    render_copy_progress,
    render_delete_completed,
    render_delete_failed,
    render_delete_progress,
    render_simple_copy_completed,
    render_simple_copy_progress,
)

# ===========================================================================
# Logging  --  clean debug format
# ===========================================================================
_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s:%(lineno)d │ %(message)s"
)
_ANSI = {
    "DEBUG":    "\033[90m",   # bright black
    "INFO":     "\033[36m",   # cyan
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[1;31m", # bold red
}
_RESET = "\033[0m"


class _TtyFormatter(logging.Formatter):
    """Colorised level, microsecond timestamps, module + line number."""

    def __init__(self, *, color: bool):
        super().__init__(_LOG_FORMAT, datefmt="%H:%M:%S")
        self._color = bool(color)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if self._color:
            tag = _ANSI.get(record.levelname, "")
            return f"{tag}{line}{_RESET}"
        return line


_handler = logging.StreamHandler()
_handler.setFormatter(_TtyFormatter(color=bool(sys.stdout and sys.stdout.isatty())))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

# Telethon's MTProto chatter is enormous at DEBUG and useless to humans.
# Keep warnings and above so genuine Telegram-API errors surface.
logging.getLogger("telethon").setLevel(logging.WARNING)

log = logging.getLogger("copybot")


def _set_log_level(level_name: str) -> None:
    """Convenience for `LOG_LEVEL=DEBUG python bot.py` style overrides."""
    level_name = (level_name or "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        return
    logging.getLogger().setLevel(level)
    log.info("log level overridden to %s", level_name)


_set_log_level(os.getenv("LOG_LEVEL", ""))


# ===========================================================================
# Environment
# ===========================================================================
load_dotenv()

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_ID or not API_HASH:
    log.error("API_ID and API_HASH must be set in .env (see .env.example)")
    sys.exit(1)
if not BOT_TOKEN:
    log.warning(
        "BOT_TOKEN not set in .env - live Bot progress DISABLED. "
        "Add BOT_TOKEN=... to enable the progress bar + refresh button."
    )

# ===========================================================================
# Display helpers
# ===========================================================================
BAR_WIDTH = 20


def _bar(pct: float, width: int = BAR_WIDTH) -> str:
    """Render a Unicode block-character progress bar."""
    # Guard NaN / inf / out-of-range values that would otherwise leak into the
    # message body and render as garbage.  Treat anything odd as "unknown".
    try:
        if not (0.0 <= pct <= 1.0) or pct != pct:  # NaN check via self-compare
            return "?" * width
    except TypeError:
        return "?" * width
    filled = max(0, min(width, round(pct * width)))
    return ("█" * filled) + ("░" * (width - filled))


def _human_bytes(n: float) -> str:
    n = float(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    f, i = n, 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)} {units[i]}"
    if f >= 100:
        return f"{int(f)} {units[i]}"
    return f"{f:.1f} {units[i]}"


def _human_speed(bps: float) -> str:
    return f"{_human_bytes(bps)}/s"


def _human_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ===========================================================================
# Clients
# ===========================================================================
SESSION_NAME = "copybot_session"
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# Second Telethon client acts as a Telegram *bot* driven by the BOT_TOKEN.
# Both run in the same asyncio loop so they share in-memory state directly.
BOT_SESSION_NAME = "copybot_bot_session"
bot_client: TelegramClient | None = None
if BOT_TOKEN:
    bot_client = TelegramClient(BOT_SESSION_NAME, API_ID, API_HASH)

# Filled once both clients are ready (in main()).
ME_ID: int | None = None
BOT_USERNAME: str | None = None
BOT_ID: int | None = None

# ===========================================================================
# Bot progress routing
# ===========================================================================
# The Bot can only message a user that has /started it first.  We persist
# that chat_id on /start to .progress_chat so subsequent /pvtcopydl jobs
# know where to send live updates.
PROGRESS_CHAT_FILE = pathlib.Path(__file__).resolve().parent / ".progress_chat"
_progress_chat_id: int | None = None
_progress_chat_loaded = False


def _read_progress_chat_id() -> int | None:
    if not PROGRESS_CHAT_FILE.exists():
        return None
    try:
        return int(PROGRESS_CHAT_FILE.read_text(encoding="utf-8").strip() or "0") or None
    except (ValueError, OSError):
        return None


def _write_progress_chat_id(chat_id: int) -> None:
    try:
        PROGRESS_CHAT_FILE.write_text(str(chat_id), encoding="utf-8")
    except OSError as e:
        log.warning("could not persist progress chat id: %s", e)


def get_progress_chat_id() -> int | None:
    global _progress_chat_loaded, _progress_chat_id
    if not _progress_chat_loaded:
        _progress_chat_id = _read_progress_chat_id()
        _progress_chat_loaded = True
    return _progress_chat_id


# ===========================================================================
# Config (JSON) helpers
# ===========================================================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Default destination from .env (fallback if config.json missing)
_ENV_DESTINATION = os.getenv("DESTINATION", "")


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Merge env destination if config has no destinations
        if not cfg.get("destinations") and _ENV_DESTINATION:
            cfg["destinations"] = [_ENV_DESTINATION]
            cfg["selected"] = _ENV_DESTINATION
            _save_config(cfg)
        return cfg

    # No config.json — build from env var or hardcoded fallback
    dest = _ENV_DESTINATION or "-1003853787609"
    default = {
        "destinations": [dest],
        "selected": dest,
    }
    _save_config(default)
    return default


def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_selected_destination() -> int:
    cfg = _load_config()
    return int(cfg["selected"])


# ===========================================================================
# Link parsers  --  private and public channel links
# ===========================================================================
PRIVATE_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/c/(?P<channel_id>\d+)/(?P<msg_id>\d+)"
)
PUBLIC_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<username>[a-zA-Z0-9_]{5,32})/(?P<msg_id>\d+)"
)


def parse_message_link(link: str):
    m = PRIVATE_LINK_RE.search(link)
    if m:
        return int(f"-100{m.group('channel_id')}"), int(m.group("msg_id"))
    m = PUBLIC_LINK_RE.search(link)
    if m:
        return m.group("username"), int(m.group("msg_id"))
    return None


# ===========================================================================
# Inline-button builder for .showdest
# ===========================================================================
def _build_dest_buttons(cfg: dict) -> list[list[Button]]:
    buttons = []
    row: list[Button] = []
    for ch_id in cfg["destinations"]:
        prefix = "✅ " if ch_id == cfg["selected"] else ""
        cb = f"select:{ch_id}"
        row.append(Button.inline(f"{prefix}{ch_id}", data=cb.encode()))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


# ===========================================================================
# .pvtcopydl  --  Download-Upload Pipeline (with live Bot progress)
# ===========================================================================
ACTIVE_DL_JOBS: dict[str, "asyncio.Task"] = {}
# Live in-memory state for each running job so bot-driven refresh/cancel
# callbacks don't need to re-read the checkpoint JSON from disk.
ACTIVE_DL_STATES: dict[str, dict] = {}

JOB_DIR = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "_pvtcopydl_tmp"
JOB_DIR.mkdir(parents=True, exist_ok=True)

MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024 * 1024

# User-account status edits are now reserved for milestones only (start,
# crash, finish).  Live updates flow through the Bot.
USER_STATUS_EDIT_EVERY = 25

# Bot progress edit cadence (seconds).  Below Telegram's 20/min envelope.
BOT_PROGRESS_EVERY_SEC = 2.5  # Rate limit: edit at most every 2.5 seconds

ALBUM_WINDOW = 15
ETA_NA = "\u2014"  # em-dash

# Rolling bytes/elapsed window used for download-speed calculation.
SPEED_WINDOW = 8


# ---------------------------------------------------------------------------
# State / checkpoint helpers
# ---------------------------------------------------------------------------
def _job_key(src_id, start_id: int, end_id: int) -> str:
    s = f"{src_id}|{start_id}|{end_id}".encode("utf-8")
    return hashlib.sha1(s).hexdigest()[:12]


def _checkpoint_path(job_key: str) -> pathlib.Path:
    return pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / f".pvtcopydl_{job_key}.json"


def _save_checkpoint(job_key: str, state: dict) -> None:
    payload = dict(state)
    payload["updated_at"] = time.time()
    p = _checkpoint_path(job_key)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(p))


def _save_state(state: dict) -> None:
    """Convenience wrapper that uses state[\"job_key\"]."""
    _save_checkpoint(state["job_key"], state)


def _load_checkpoint(job_key: str):
    p = _checkpoint_path(job_key)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _clear_checkpoint(job_key: str) -> None:
    try:
        os.remove(_checkpoint_path(job_key))
    except FileNotFoundError:
        pass


def _safe_unlink(path) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except (FileNotFoundError, TypeError):
        pass


def _progress_pct(state: dict) -> float:
    """Fraction of work done, based on the message cursor (0..1)."""
    total = max(1, state.get("total", 1))
    cur = state.get("current_msg_id", state.get("start_msg_id", 0))
    start = state.get("start_msg_id", cur)
    end = state.get("end_msg_id", cur)
    span = max(1, end - start + 1)
    done = max(0, cur - start)
    return min(1.0, done / span)


def _update_speed(state: dict, size: int, elapsed: float) -> None:
    """Maintain a rolling bytes/elapsed window and compute speed."""
    window: list[list[float]] = state.setdefault("speed_window", [])
    if elapsed > 0 and size > 0:
        window.append([float(size), float(elapsed)])
        while len(window) > SPEED_WINDOW:
            window.pop(0)
    total_b = sum(b for b, _ in window)
    total_e = sum(e for _, e in window)
    state["download_speed_bps"] = total_b / max(total_e, 0.001)
    state["bytes_downloaded_total"] = state.get("bytes_downloaded_total", 0) + max(0, size)


def _update_upload_speed(state: dict, size: int, elapsed: float) -> None:
    """Maintain a rolling bytes/elapsed window for upload speed."""
    window: list[list[float]] = state.setdefault("upload_speed_window", [])
    if elapsed > 0 and size > 0:
        window.append([float(size), float(elapsed)])
        while len(window) > SPEED_WINDOW:
            window.pop(0)
    total_b = sum(b for b, _ in window)
    total_e = sum(e for _, e in window)
    state["upload_speed_bps"] = total_b / max(total_e, 0.001)
    state["upload_bytes_total"] = state.get("upload_bytes_total", 0) + max(0, size)


def _extract_document_attrs(msg):
    """Extract relevant document attributes from a message for re-upload.

    Returns:
        (attributes_list, mime_type, supports_streaming)
    """
    attrs = []
    mime_type = None
    supports_streaming = False

    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None) if media else None
    if doc is None:
        return attrs, mime_type, supports_streaming

    mime_type = getattr(doc, "mime_type", None)

    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeVideo):
            attrs.append(attr)
            supports_streaming = getattr(attr, "supports_streaming", False)
        elif isinstance(attr, DocumentAttributeFilename):
            attrs.append(attr)
        elif isinstance(attr, DocumentAttributeAudio):
            attrs.append(attr)
        elif isinstance(attr, DocumentAttributeImageSize):
            attrs.append(attr)
        elif isinstance(attr, DocumentAttributeAnimated):
            attrs.append(attr)

    return attrs, mime_type, supports_streaming


def _make_upload_progress_callback(state: dict):
    """Create an async progress callback for send_file upload tracking.

    The callback updates the state with upload progress and triggers
    bot progress edits (throttled).
    """
    _last_cb_edit = [0.0]

    async def _upload_cb(current: int, total: int) -> None:
        state["upload_current"] = current
        state["upload_total"] = total
        # Track upload speed via rolling window
        now = time.monotonic()
        last_time = state.get("_upload_cb_last_time", 0.0)
        last_bytes = state.get("_upload_cb_last_bytes", 0)
        dt = now - last_time
        if dt > 0.5 and current > last_bytes:
            _update_upload_speed(state, current - last_bytes, dt)
            state["_upload_cb_last_time"] = now
            state["_upload_cb_last_bytes"] = current
        # Throttled bot progress edit
        if now - _last_cb_edit[0] >= BOT_PROGRESS_EVERY_SEC:
            _last_cb_edit[0] = now
            await _refresh_bot_progress(state)

    return _upload_cb


def _make_album_upload_progress_callback(state: dict, file_index: int, total_files: int):
    """Create a progress callback for individual files within an album."""
    _last_cb_edit = [0.0]

    async def _album_cb(current: int, total: int) -> None:
        # For albums, show as fraction of total files + per-file progress
        state["upload_current"] = file_index + (current / max(total, 1))
        state["upload_total"] = total_files
        now = time.monotonic()
        if now - _last_cb_edit[0] >= BOT_PROGRESS_EVERY_SEC:
            _last_cb_edit[0] = now
            await _refresh_bot_progress(state)

    return _album_cb


# ---------------------------------------------------------------------------
# Status text renderers
# ---------------------------------------------------------------------------
def _format_status(state: dict) -> str:
    """Compact summary written to the user-account chat (milestones only)."""
    total = state.get("total", 0)
    cur = state.get("current_msg_id", "?")
    return (
        f".pvtcopydl progress\n"
        f"  current_msg: `{cur}`  •  total: {total}\n"
        f"  downloaded : {state.get('downloaded', 0)}\n"
        f"  uploaded   : {state.get('uploaded', 0)}\n"
        f"  skipped    : {state.get('skipped', 0)}\n"
        f"  errors     : {state.get('errors', 0)}\n"
        f"  retries    : {state.get('retries', 0)}\n"
        f"  flood_waits: {state.get('flood_waits', 0)}\n"
        f"  eta        : {state.get('eta', ETA_NA)}\n"
        f"  checkpoint : .pvtcopydl_{state.get('job_key', '?')}.json"
    )


def _format_bot_status(state: dict) -> str:
    """Rich Bot-message text with bar / ETA / speed / counters.

    Uses the progress_ui renderer for a professional terminal-style dashboard.
    """
    spinner = state.setdefault("_spinner", Spinner())
    cur = state.get("current_msg_id", 0)
    start = state.get("start_msg_id", cur)
    end = state.get("end_msg_id", cur)
    total = end - start + 1
    speed = state.get("download_speed_bps", 0.0)
    # Parse ETA string back to seconds for the renderer
    eta_str = state.get("eta", ETA_NA)
    eta_seconds = _parse_eta_to_seconds(eta_str)
    elapsed = time.time() - state.get("started_at", time.time())
    filename = state.get("upload_file_name", "")

    return render_copy_progress(
        spinner=spinner,
        mode="download",
        job_key=state.get("job_key", "?"),
        current_msg=cur,
        total=total,
        downloaded=state.get("downloaded", 0),
        uploaded=state.get("uploaded", 0),
        skipped=state.get("skipped", 0),
        errors=state.get("errors", 0),
        speed=speed,
        eta=eta_seconds,
        elapsed=elapsed,
        filename=filename,
    )


def _parse_eta_to_seconds(eta_str: str) -> float:
    """Parse ETA string like '2m 30s' or '1h 5m 3s' back to seconds."""
    if not eta_str or eta_str == ETA_NA:
        return 0.0
    total = 0.0
    for part in eta_str.split():
        part = part.strip()
        if part.endswith("h"):
            try: total += float(part[:-1]) * 3600
            except ValueError: pass
        elif part.endswith("m"):
            try: total += float(part[:-1]) * 60
            except ValueError: pass
        elif part.endswith("s"):
            try: total += float(part[:-1])
            except ValueError: pass
    return total


def _bot_buttons() -> list[list[Button]]:
    return [
        [
            Button.inline("🔄 Refresh", data=b"refresh"),
            Button.inline("⏹ Cancel", data=b"cancel"),
        ]
    ]


def _bot_buttons_for(job_key: str) -> list[list[Button]]:
    """Per-job refresh button (job_key encoded so refresh is unambiguous when
    multiple jobs are live).
    """
    return [
        [
            Button.inline("🔄 Refresh", data=f"refresh:{job_key}".encode()),
            Button.inline("⏹ Cancel", data=f"cancel:{job_key}".encode()),
        ]
    ]


def _job_inline_buttons(job_key: str) -> list[list[Button]]:
    """Inline buttons for job progress messages (user's chat)."""
    return [
        [
            Button.inline("🔄 Refresh", data=f"refresh:{job_key}".encode()),
            Button.inline("⏹ Cancel", data=f"cancel:{job_key}".encode()),
        ]
    ]


# ---------------------------------------------------------------------------
# Live Bot progress editing
# ---------------------------------------------------------------------------
async def _refresh_bot_progress(state: dict, *, force: bool = False) -> None:
    """Edit the Bot's progress message if enough time has elapsed (or force=True)."""
    if bot_client is None:
        return
    chat_id = state.get("progress_chat_id") or get_progress_chat_id() or ME_ID
    msg_id = state.get("progress_msg_id")
    if chat_id is None or msg_id is None:
        return
    now = time.monotonic()
    last = state.get("progress_last_edit", 0.0)
    if not force and now - last < BOT_PROGRESS_EVERY_SEC:
        return
    try:
        await bot_client.edit_message(
            chat_id,
            msg_id,
            _format_bot_status(state),
            buttons=_bot_buttons_for(state.get("job_key", "?")),
        )
        state["progress_last_edit"] = now
    except FloodWaitError as e:
        await asyncio.sleep(getattr(e, "seconds", 0) + 1)
    except Exception as e:  # message-not-modified and the like; ignore
        log.debug("bot progress edit failed: %s", type(e).__name__)


async def _send_initial_bot_progress(state: dict, chat_id: int) -> int | None:
    """Send the first Bot progress message and stash its id on state."""
    if bot_client is None:
        return None
    try:
        msg = await bot_client.send_message(
            chat_id,
            _format_bot_status(state),
            buttons=_bot_buttons_for(state.get("job_key", "?")),
        )
        state["progress_chat_id"] = chat_id
        state["progress_msg_id"] = msg.id
        state["progress_last_edit"] = time.monotonic()
        log.info(
            "live progress → bot chat_id=%s msg_id=%s",
            chat_id, msg.id,
        )
        return msg.id
    except Exception as e:
        log.warning("initial bot progress message failed: %s", e)
        return None


async def _finalise_bot_progress(state: dict, *, success: bool) -> None:
    """Edit the Bot progress message one last time with the final summary."""
    if bot_client is None:
        return
    chat_id = state.get("progress_chat_id") or get_progress_chat_id() or ME_ID
    msg_id = state.get("progress_msg_id")
    if chat_id is None or msg_id is None:
        return
    header = "✅ complete" if success else "⚠️ finished with problems"
    try:
        await bot_client.edit_message(
            chat_id,
            msg_id,
            f"{header}\n\n" + _format_bot_status(state),
            buttons=[[Button.inline("🆔 Job Key", data=b"noop")]],
        )
    except Exception as e:
        log.debug("final bot edit failed: %s", e)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------
def _classify(msg) -> str:
    if getattr(msg, "action", None):
        return "service"
    if not getattr(msg, "media", None) and not getattr(msg, "message", None):
        return "service"
    if getattr(msg, "poll", None):
        return "poll"
    if getattr(msg, "contact", None):
        return "contact"
    if getattr(msg, "geo", None):
        return "location"
    if getattr(msg, "media", None):
        return "media"
    return "text"


def _media_size_bytes(msg) -> int:
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None) if media else None
    if doc and getattr(doc, "size", None):
        return int(doc.size)
    photo = getattr(media, "photo", None) if media else None
    if photo:
        sizes = getattr(photo, "sizes", None) or []
        best = 0
        for s in sizes:
            sz = getattr(s, "size", None) or getattr(s, "sizes", None)
            if isinstance(sz, list):
                if len(sz) >= 3:
                    best = max(best, int(sz[2]))
            elif sz:
                best = max(best, int(sz))
        return best
    return 0


# ---------------------------------------------------------------------------
# Single-shot download / verify
# ---------------------------------------------------------------------------
async def _redownload_media(msg, src_entity=None, retries: int = 2):
    """Download msg.media to JOB_DIR.

    Returns:
        (path, size_bytes, elapsed_seconds) on success
        (None, -1, 0.0) if file exceeds the 2 GB cap
        (None, -2, 0.0) if media is empty / revoked
        (None,  0, 0.0) on hard failure
    """
    size = _media_size_bytes(msg)
    if size and size > MAX_SINGLE_FILE_BYTES:
        log.info(
            "msg=%d skipped (>%s): %s",
            msg.id, _human_bytes(MAX_SINGLE_FILE_BYTES), _human_bytes(size),
        )
        return None, -1, 0.0

    fresh = msg
    last_err = None
    for attempt in range(retries + 1):
        try:
            t0 = time.monotonic()
            path = await client.download_media(fresh, file=str(JOB_DIR) + "/")
            elapsed = time.monotonic() - t0
            if path:
                actual = os.path.getsize(path) if os.path.exists(path) else 0
                log.debug(
                    "msg=%d downloaded %s in %.2fs (%s)",
                    msg.id, _human_bytes(actual), elapsed,
                    _human_speed(actual / max(elapsed, 0.001)),
                )
                # Structured INFO line for the live progress view:
                log.info(
                    "FE msg=%d bytes=%s elapsed=%.2fs speed=%s",
                    msg.id, _human_bytes(actual), elapsed,
                    _human_speed(actual / max(elapsed, 0.001)),
                )
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
        except MediaEmptyError:
            log.info("msg=%d media empty / revoked - gentle skip", msg.id)
            return None, -2, 0.0
        except (ConnectionError, asyncio.TimeoutError) as e:
            last_err = e
            await asyncio.sleep(5)
        except Exception as e:
            log.warning("download_media raised %s for msg=%d", e, msg.id)
            last_err = e
            break
    log.warning(
        "download failed msg=%d attempts=%d err=%s",
        msg.id, retries + 1, last_err,
    )
    return None, 0, 0.0


async def _verify_uploaded(dst_entity, sent_msg, original_msg) -> bool:
    if sent_msg is None:
        return False
    sent_id = getattr(sent_msg, "id", None)
    if sent_id is None:
        return False
    try:
        refetched_list = await client.get_messages(dst_entity, ids=sent_id)
        refetched = refetched_list[0] if isinstance(refetched_list, list) else refetched_list
    except Exception as e:
        log.warning("verify re-fetch failed: %s", e)
        return False
    if refetched is None:
        return False
    if getattr(original_msg, "media", None):
        if not getattr(refetched, "media", None):
            return False
        if getattr(original_msg.media, "document", None) and getattr(refetched.media, "document", None):
            o = getattr(original_msg.media.document, "size", 0) or 0
            u = getattr(refetched.media.document, "size", 0) or 0
            if o and u and u < o * 0.95:
                return False
        return True
    if getattr(original_msg, "message", None):
        return bool(refetched.message and refetched.message == original_msg.message)
    return True


# ---------------------------------------------------------------------------
# Re-upload helpers  (unchanged behaviour)
# ---------------------------------------------------------------------------
async def _reupload_text(msg, dst_entity):
    return await client.send_message(
        entity=dst_entity,
        message=msg.message or "",
        formatting_entities=msg.entities,
    )


async def _reupload_poll(msg, dst_entity):
    poll = msg.poll
    if not poll:
        return None
    parts = ["📊 **Poll**"]
    q = getattr(poll, "question", None)
    if q:
        if hasattr(q, "text"):
            parts.append(f"Question: **{q.text}**")
        else:
            parts.append(f"Question: **{str(q)}**")
    answers = getattr(poll, "answers", None) or []
    for i, ans in enumerate(answers, 1):
        a_text = getattr(ans, "text", None)
        if hasattr(a_text, "text"):
            a_text = a_text.text
        parts.append(f"  {i}. {a_text or '(empty option)'}")
    if getattr(poll, "closed", False):
        parts.append("_(Poll closed)_")
    return await client.send_message(dst_entity, message="\n".join(parts))


async def _reupload_contact(msg, dst_entity):
    c = msg.contact
    if not c:
        return None
    full_name = " ".join(
        x for x in [getattr(c, "first_name", "") or "",
                    getattr(c, "last_name", "") or ""] if x
    ).strip()
    lines = ["📇 **Contact**"]
    if full_name:
        lines.append(f"Name: **{full_name}**")
    if getattr(c, "phone_number", None):
        lines.append(f"Phone: `{c.phone_number}`")
    if getattr(c, "user_id", None):
        lines.append(f"UserID: `{c.user_id}`")
    return await client.send_message(dst_entity, message="\n".join(lines))


async def _reupload_location(msg, dst_entity):
    g = msg.geo
    if not g:
        return None
    if hasattr(g, "lat") and hasattr(g, "long"):
        lat, lon = g.lat, g.long
    elif hasattr(g, "geo_point"):
        gp = g.geo_point
        lat, lon = gp.lat, gp.long
    else:
        return None
    maps_url = f"https://maps.google.com/?q={lat},{lon}"
    live_note = ""
    if getattr(g, "period", None):
        live_note = "\n_(was a live location; TTL stripped - Telegram API limitation)_"
    text = f"📍 **Location**{live_note}\n`{lat:.6f}, {lon:.6f}`\n{maps_url}"
    return await client.send_message(dst_entity, message=text)


async def _reupload_album(siblings_with_paths, dst_entity, state=None):
    """Upload album items preserving original document attributes."""
    if not siblings_with_paths:
        return None

    entity = await client.get_input_entity(dst_entity)
    media_list = []

    for idx, (msg, path) in enumerate(siblings_with_paths):
        orig_attrs, orig_mime, orig_streaming = _extract_document_attrs(msg)

        # Set upload phase for progress tracking
        if state:
            fname = ""
            for a in orig_attrs:
                if isinstance(a, DocumentAttributeFilename):
                    fname = a.file_name
                    break
            if not fname:
                fname = os.path.basename(path) if path else f"file_{idx}"
            state["upload_phase"] = "uploading"
            state["upload_file_name"] = fname
            state["upload_current"] = idx
            state["upload_total"] = len(siblings_with_paths)
            await _refresh_bot_progress(state)

        # Upload the file with per-file progress callback
        cb = _make_album_upload_progress_callback(state, idx, len(siblings_with_paths)) if state else None
        file_handle = await client.upload_file(path, progress_callback=cb)

        # Determine nosound_video for videos
        nosound_video = None
        if orig_mime and orig_mime.startswith("video/"):
            nosound_video = True

        # Construct media with original attributes
        fm = tg_types.InputMediaUploadedDocument(
            file=file_handle,
            mime_type=orig_mime,
            attributes=orig_attrs if orig_attrs else None,
            force_file=False,
            nosound_video=nosound_video,
        )

        # Upload as media to get a proper reference
        r = await client(tg_functions.messages.UploadMediaRequest(entity, media=fm))
        fm = tg_utils.get_input_media(
            r.document,
            supports_streaming=orig_streaming
        )

        caption = msg.message or ""
        media_list.append(tg_types.InputSingleMedia(
            fm,
            message=caption,
            entities=msg.entities
        ))

    if state:
        state["upload_phase"] = "idle"

    request = tg_functions.messages.SendMultiMediaRequest(
        entity,
        multi_media=media_list
    )
    result = await client(request)
    random_ids = [m.random_id for m in media_list]
    return client._get_response_message(random_ids, result, dst_entity)


# ===========================================================================
# Core .pvtcopydl loop
# ===========================================================================
async def _send_wait_sleep(e):
    wait = getattr(e, "seconds", 0) + 2
    log.warning("Telegram asked to sleep %ds - obeying", wait)
    await asyncio.sleep(wait)


async def _loop_pvtcopydl(state: dict, src_entity, dst_entity, status_msg):
    job_key = state["job_key"]
    start_id = state["start_msg_id"]
    end_id = state["end_msg_id"]
    downloaded = state.get("downloaded", 0)
    uploaded = state.get("uploaded", 0)
    last_downloads = state.get("per_item_times", [])
    recent_max = 10
    last_user_edit = time.monotonic()
    flood_waits = state.get("flood_waits", 0)
    skipped = state.get("skipped", 0)
    errors = state.get("errors", 0)
    retries = state.get("retries", 0)
    processed = 0
    bot_component = state.get("component_msg_id")  # human-account placeholder

    while state["current_msg_id"] <= end_id:
        loop_start = time.monotonic()
        msg_id = state["current_msg_id"]
        sent_after_attempt = False
        try:
            window_lo = max(start_id, msg_id - ALBUM_WINDOW)
            window_hi = min(end_id, msg_id + ALBUM_WINDOW)
            ids = list(range(window_lo, window_hi + 1))
            fetched = await client.get_messages(src_entity, ids=ids)
            fetched_list = fetched if isinstance(fetched, list) else [fetched]
            cur = next((m for m in fetched_list if m is not None and m.id == msg_id), None)
            if cur is None:
                state["skipped"] = (state.get("skipped", 0) + 1)
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                continue

            # ----- ALBUM branch -----
            if getattr(cur, "grouped_id", None):
                gid = cur.grouped_id
                siblings = [m for m in fetched_list if m is not None and getattr(m, "grouped_id", None) == gid]
                siblings.sort(key=lambda m: m.id)
                paths = []
                sibs_in_order = []
                too_big = False
                for s in siblings:
                    if _media_size_bytes(s) and _media_size_bytes(s) > MAX_SINGLE_FILE_BYTES:
                        too_big = True
                        continue
                    p, code, _ = await _redownload_media(s, src_entity=src_entity)
                    if code == -1:
                        too_big = True
                        continue
                    if p:
                        paths.append(p)
                        sibs_in_order.append((s, p))
                if not sibs_in_order:
                    state["skipped"] = (state.get("skipped", 0) + 1) + max(0, len(siblings) - 1)
                    state["current_msg_id"] = siblings[-1].id + 1
                    _save_state(state)
                    continue
                downloaded += len(sibs_in_order)
                state["downloaded"] = downloaded
                sent = None
                for attempt in range(4):
                    try:
                        sent = await _reupload_album(sibs_in_order, dst_entity, state=state)
                        break
                    except (FloodWaitError, SlowModeWaitError) as e:
                        flood_waits += 1
                        state["flood_waits"] = flood_waits
                        await _send_wait_sleep(e)
                        continue
                    except (ConnectionError, asyncio.TimeoutError):
                        retries += 1
                        state["retries"] = retries
                        await asyncio.sleep(5)
                        continue
                if sent is not None:
                    head_ok = await _verify_uploaded(dst_entity, sent, sibs_in_order[0][0])
                    if not head_ok:
                        retries += 1
                        state["retries"] = retries
                        sent = None
                    elif len(sibs_in_order) > 1:
                        tail_ids = [sent.id + i for i in range(1, len(sibs_in_order))]
                        try:
                            tail_list = await client.get_messages(dst_entity, ids=tail_ids)
                            tail = tail_list if isinstance(tail_list, list) else [tail_list]
                            missing = sum(1 for t in tail if t is None or not getattr(t, "media", None))
                            if missing:
                                log.warning("album verify: %d/%d tail missing media", missing, len(tail))
                                retries += 1
                                state["retries"] = retries
                        except Exception as e:
                            log.warning("album tail verify failed: %s", e)
                if sent is None:
                    errors += 1
                    state["errors"] = errors
                else:
                    uploaded += len(sibs_in_order)
                    state["uploaded"] = uploaded
                    for _, p in sibs_in_order:
                        _safe_unlink(p)
                    state["current_msg_id"] = siblings[-1].id + 1
                    _save_state(state)
                    sent_after_attempt = True
                state["current_msg_id"] = max(state.get("current_msg_id", msg_id), siblings[-1].id + 1)
                _save_state(state)
                loop_end = time.monotonic()
                last_downloads = (last_downloads + [loop_end - loop_start])[-recent_max:]
                state["per_item_times"] = last_downloads
                if last_downloads:
                    avg = sum(last_downloads) / len(last_downloads)
                    remain = max(0, (end_id - state["current_msg_id"] + 1))
                    state["eta"] = _human_eta(avg * remain)
                await _refresh_bot_progress(state)
                continue

            # ----- SINGLE msg branch -----
            ctype = _classify(cur)
            if ctype == "service":
                state["skipped"] = (state.get("skipped", 0) + 1)
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_attempt = True
                continue

            if ctype == "text":
                sent = None
                for attempt in range(4):
                    try:
                        sent = await _reupload_text(cur, dst_entity)
                        break
                    except (FloodWaitError, SlowModeWaitError) as e:
                        flood_waits += 1
                        state["flood_waits"] = flood_waits
                        await _send_wait_sleep(e)
                        continue
                    except (ConnectionError, asyncio.TimeoutError):
                        retries += 1
                        state["retries"] = retries
                        await asyncio.sleep(5)
                        continue
                if sent and await _verify_uploaded(dst_entity, sent, cur):
                    uploaded += 1
                    state["uploaded"] = uploaded
                else:
                    errors += 1
                    state["errors"] = errors
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_attempt = True

            elif ctype == "poll":
                sent = await _reupload_poll(cur, dst_entity)
                if sent:
                    uploaded += 1
                    state["uploaded"] = uploaded
                else:
                    state["skipped"] = (state.get("skipped", 0) + 1)
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_attempt = True

            elif ctype == "contact":
                if await _reupload_contact(cur, dst_entity):
                    uploaded += 1
                    state["uploaded"] = uploaded
                else:
                    state["skipped"] = (state.get("skipped", 0) + 1)
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_attempt = True

            elif ctype == "location":
                if await _reupload_location(cur, dst_entity):
                    uploaded += 1
                    state["uploaded"] = uploaded
                else:
                    state["skipped"] = (state.get("skipped", 0)) + 1
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_attempt = True

            elif ctype == "media":
                # Extract original document attributes BEFORE upload
                orig_attrs, orig_mime, orig_streaming = _extract_document_attrs(cur)
                file_name = ""
                for a in orig_attrs:
                    if isinstance(a, DocumentAttributeFilename):
                        file_name = a.file_name
                        break

                state["upload_phase"] = "downloading"
                state["upload_file_name"] = file_name or f"msg_{msg_id}"
                state["upload_current"] = 0
                state["upload_total"] = 0

                t_dl_start = time.monotonic()
                path, code, elapsed = await _redownload_media(cur, src_entity=src_entity)
                t_dl_end = time.monotonic()
                log.info(
                    "DL msg=%d file=%s code=%d elapsed=%.2fs speed=%s",
                    msg_id, file_name or "?", code, elapsed,
                    _human_speed(code / max(elapsed, 0.001)) if code > 0 and elapsed > 0 else "—",
                )
                if code == -1:
                    state["skipped"] = (state.get("skipped", 0) + 1)
                    state["current_msg_id"] = msg_id + 1
                    state["upload_phase"] = "idle"
                    _save_state(state)
                    sent_after_attempt = True
                    continue
                if code == -2:
                    state["skipped"] = (state.get("skipped", 0) + 1)
                    state["current_msg_id"] = msg_id + 1
                    state["upload_phase"] = "idle"
                    _save_state(state)
                    sent_after_attempt = True
                    continue
                downloaded += 1
                state["downloaded"] = downloaded
                # Track download speed
                if code > 0 and elapsed > 0:
                    _update_speed(state, code, elapsed)
                if not path:
                    errors += 1
                    state["errors"] = errors
                    state["current_msg_id"] = msg_id + 1
                    state["upload_phase"] = "idle"
                    _save_state(state)
                    sent_after_attempt = True
                    continue

                if not file_name and path:
                    file_name = os.path.basename(path)
                state["upload_file_name"] = file_name or f"msg_{msg_id}"
                state["upload_phase"] = "uploading"
                state["upload_current"] = 0
                state["upload_total"] = os.path.getsize(path) if os.path.exists(path) else 0
                await _refresh_bot_progress(state, force=True)

                upload_cb = _make_upload_progress_callback(state)
                sent = None
                t_ul_start = time.monotonic()
                for attempt in range(4):
                    try:
                        sent = await client.send_file(
                            entity=dst_entity,
                            file=path,
                            caption=cur.message or None,
                            formatting_entities=cur.entities,
                            attributes=orig_attrs if orig_attrs else None,
                            mime_type=orig_mime,
                            supports_streaming=orig_streaming,
                            progress_callback=upload_cb,
                        )
                        break
                    except (FloodWaitError, SlowModeWaitError) as e:
                        flood_waits += 1
                        state["flood_waits"] = flood_waits
                        await _send_wait_sleep(e)
                        continue
                    except (ConnectionError, asyncio.TimeoutError):
                        retries += 1
                        state["retries"] = retries
                        await asyncio.sleep(5)
                        continue
                    except FileReferenceExpiredError:
                        retries += 1
                        state["retries"] = retries
                        _safe_unlink(path)
                        path2, code2, _ = await _redownload_media(cur, src_entity=src_entity)
                        if code2 == -1 or not path2:
                            errors += 1
                            state["errors"] = errors
                            break
                        path = path2
                        continue
                t_ul_end = time.monotonic()
                ul_elapsed = t_ul_end - t_ul_start
                state["upload_phase"] = "idle"
                state["upload_current"] = 0
                state["upload_total"] = 0

                log.info(
                    "UL msg=%d file=%s elapsed=%.2fs speed=%s sent=%s",
                    msg_id, file_name or "?", ul_elapsed,
                    _human_speed(os.path.getsize(path) / max(ul_elapsed, 0.001)) if path and os.path.exists(path) and ul_elapsed > 0 else "—",
                    "ok" if sent else "FAILED",
                )

                if sent is not None and await _verify_uploaded(dst_entity, sent, cur):
                    uploaded += 1
                    state["uploaded"] = uploaded
                    _safe_unlink(path)
                else:
                    retries += 1
                    state["retries"] = retries
                    errors += 1
                    state["errors"] = errors
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_point = sent_after_attempt = True

            else:
                log.warning("unhandled classify=%s msg=%d", ctype, msg_id)
                state["skipped"] = (state.get("skipped", 0) + 1)
                state["current_msg_id"] = msg_id + 1
                _save_state(state)
                sent_after_attempt = True

            loop_end = time.monotonic()
            last_downloads = (last_downloads + [loop_end - loop_start])[-recent_max:]
            state["per_item_times"] = last_downloads
            if last_downloads:
                avg = sum(last_downloads) / len(last_downloads)
                remain = max(0, (end_id - state["current_msg_id"] + 1))
                state["eta"] = _human_eta(avg * remain)

        except (FloodWaitError, SlowModeWaitError) as e:
            flood_waits += 1
            state["flood_waits"] = flood_waits
            await _send_wait_sleep(e)
            continue
        except FileReferenceExpiredError:
            retries += 1
            state["retries"] = retries
            await asyncio.sleep(1)
            continue
        except (MessageIdInvalidError, MessageDeleteForbiddenError, MediaEmptyError):
            state["skipped"] = (state.get("skipped", 0) + 1)
            state["current_msg_id"] = msg_id + 1
            _save_state(state)
        except ChatAdminRequiredError as e:
            log.error("pvtcopydl hard-stop: destination requires admin")
            try:
                await status_msg.edit(
                    f"❌ pvtcopydl {job_key} cannot post to destination: {e}\n"
                    "Make sure your account is admin of the destination channel.\n"
                    f"Checkpoint saved at msg {msg_id} - re-issue after granting access."
                )
            except Exception:
                pass
            _save_state(state)
            await _finalise_bot_progress(state, success=False)
            return
        except (ConnectionError, asyncio.TimeoutError):
            retries += 1
            state["retries"] = retries
            await asyncio.sleep(5)
            continue
        except asyncio.CancelledError:
            _save_state(state)
            raise
        except Exception as e:
            log.exception("unhandled error pvtcopydl msg=%d", msg_id)
            errors += 1
            state["errors"] = errors
            state["current_msg_id"] = msg_id + 1
            _save_state(state)

        # ---- progress routing --------------------------------------------
        processed += 1
        now = time.monotonic()

        # Bot progress: every BOT_PROGRESS_EVERY_SEC (with throttling)
        await _refresh_bot_progress(state)

        # User-account status: only on milestones (not the live progress bar).
        if processed % USER_STATUS_EDIT_EVERY == 0 or now - last_user_edit > 30:
            try:
                await status_msg.edit(_format_status(state))
                last_user_edit = now
            except FloodWaitError as e:
                await _send_wait_sleep(e)
            except Exception as e:
                log.debug("user status edit failed: %s", e)

        # Minimal delay to avoid Telegram rate limits; was 1.5-3s which
        # severely bottlenecked throughput.
        await asyncio.sleep(random.uniform(0.1, 0.3))


# ===========================================================================
# .pvtcopydlcancel
# ===========================================================================
@client.on(events.NewMessage(pattern=r"\.pvtcopydlcancel(?:\s+(\S+))?"))
async def handle_pvtcopydlcancel(event):
    m = event.pattern_match
    key = m.group(1) if m and m.lastindex else None
    if not key:
        if not ACTIVE_DL_JOBS:
            await event.respond("no active .pvtcopydl jobs.")
            return
        listing = "\n".join(f"• {k}" for k in ACTIVE_DL_JOBS.keys())
        await event.respond(
            f"active .pvtcopydl jobs (cancel with `.pvtcopydlcancel <key>`):\n{listing}"
        )
        return
    task = ACTIVE_DL_JOBS.get(key)
    if task is None or task.done():
        await event.respond(f"no running job with key `{key}` (already done or never started).")
        return
    task.cancel()
    await event.respond(f"cancelling job `{key}` - checkpoint preserved.")


# ===========================================================================
# .pvtcopydl
# ===========================================================================
@client.on(events.NewMessage(pattern=r"\.pvtcopydl\s+(\S+)\s+(\S+)"))
async def handle_pvtcopydl(event):
    start_link = event.pattern_match.group(1)
    end_link = event.pattern_match.group(2)

    start = parse_message_link(start_link)
    end = parse_message_link(end_link)
    if start is None:
        await event.respond("invalid start link.")
        return
    if end is None:
        await event.respond("invalid end link.")
        return
    if start[0] != end[0]:
        await event.respond("start and end links must point to the same source channel.")
        return

    src_identifier, start_msg_id = start
    _, end_msg_id = end
    if start_msg_id > end_msg_id:
        start_msg_id, end_msg_id = end_msg_id, start_msg_id

    dest_channel_id = get_selected_destination()
    if str(src_identifier) == str(dest_channel_id):
        await event.respond("source and destination must differ.")
        return

    try:
        src_entity = await client.get_entity(src_identifier)
        dst_entity = await client.get_entity(dest_channel_id)
    except (ChannelPrivateError, ValueError) as e:
        await event.respond(f"cannot resolve channel: {e}")
        return

    job_key = _job_key(src_identifier, start_msg_id, end_msg_id)
    total = end_msg_id - start_msg_id + 1

    prior = _load_checkpoint(job_key)
    if prior is None:
        state = {
            "job_key": job_key,
            "src": str(src_identifier),
            "dst": str(dest_channel_id),
            "start_msg_id": start_msg_id,
            "end_msg_id": end_msg_id,
            "current_msg_id": start_msg_id,
            "total": total,
            "downloaded": 0,
            "uploaded": 0,
            "skipped": 0,
            "errors": 0,
            "retries": 0,
            "flood_waits": 0,
            "eta": ETA_NA,
            "per_item_times": [],
            "speed_window": [],
            "download_speed_bps": 0.0,
            "bytes_downloaded_total": 0,
            "upload_phase": "idle",
            "upload_file_name": "",
            "upload_current": 0,
            "upload_total": 0,
            "upload_speed_bps": 0.0,
            "upload_bytes_total": 0,
            "upload_speed_window": [],
            "started_at": time.time(),
            "_spinner": Spinner(),
        }
    else:
        state = prior
        state.setdefault("speed_window", [])
        state.setdefault("download_speed_bps", 0.0)
        state.setdefault("bytes_downloaded_total", 0)
        state.setdefault("upload_phase", "idle")
        state.setdefault("upload_file_name", "")
        state.setdefault("upload_current", 0)
        state.setdefault("upload_total", 0)
        state.setdefault("upload_speed_bps", 0.0)
        state.setdefault("upload_bytes_total", 0)
        state.setdefault("upload_speed_window", [])
        state.setdefault("_spinner", Spinner())
        state["total"] = total

    # Send initial progress message with inline buttons in user's chat
    spinner = state["_spinner"]
    channel_display = getattr(src_entity, "title", None) or str(src_identifier)
    initial_text = render_copy_progress(
        spinner=spinner,
        mode="download",
        job_key=job_key,
        current_msg=state.get("current_msg_id", start_msg_id),
        total=total,
        downloaded=state.get("downloaded", 0),
        uploaded=state.get("uploaded", 0),
        skipped=state.get("skipped", 0),
        errors=state.get("errors", 0),
        speed=state.get("download_speed_bps", 0.0),
        eta=0.0,
        elapsed=0.0,
        filename="",
    )
    status_msg = await event.respond(
        initial_text,
        buttons=_job_inline_buttons(job_key),
    )
    # Store chat_id and msg_id for inline button callbacks
    state["progress_chat_id"] = event.chat_id
    state["progress_msg_id"] = status_msg.id
    state["progress_last_edit"] = time.monotonic()

    task = asyncio.current_task()
    ACTIVE_DL_JOBS[job_key] = task
    ACTIVE_DL_STATES[job_key] = state
    log.info(
        "pvtcopydl START key=%s src=%s dst=%s range=%d..%d",
        job_key, src_identifier, dest_channel_id, start_msg_id, end_msg_id,
    )

    try:
        await _loop_pvtcopydl(state, src_entity, dst_entity, status_msg)
    except asyncio.CancelledError:
        log.info("pvtcopydl CANCEL key=%s", job_key)
        try:
            await status_msg.edit(
                f"pvtcopydl {job_key} cancelled at msg {state.get('current_msg_id', '?')}.\n"
                "checkpoint saved - re-issue the same command to resume."
            )
        except Exception:
            pass
        await _finalise_bot_progress(state, success=False)
        raise
    except Exception as e:
        log.exception("pvtcopydl CRASH key=%s err=%s", job_key, e)
        try:
            await status_msg.edit(
                f"pvtcopydl {job_key} crashed: {e}\n"
                "checkpoint saved - re-issue the same command to resume."
            )
        except Exception:
            pass
        await _finalise_bot_progress(state, success=False)
        return
    finally:
        ACTIVE_DL_JOBS.pop(job_key, None)
        ACTIVE_DL_STATES.pop(job_key, None)

    try:
        tail_id = state["current_msg_id"]
        if tail_id > end_msg_id:
            if state.get("uploaded", 0) > 0:
                _clear_checkpoint(job_key)
                log.info(
                    "pvtcopydl DONE key=%s uploaded=%d errors=%d",
                    job_key, state.get("uploaded", 0), state.get("errors", 0),
                )
                await _finalise_bot_progress(state, success=True)
                await status_msg.edit(
                    f"pvtcopydl {job_key} complete\n" + _format_status(state)
                )
            else:
                log.warning("pvtcopydl DONE uploaded=0 - checkpoint kept")
                await _finalise_bot_progress(state, success=False)
        else:
            log.warning("pvtcopydl STOP key=%s tail=%d end=%d", job_key, tail_id, end_msg_id)
            await _finalise_bot_progress(state, success=False)
    except Exception as e:
        log.warning("final status update failed: %s", e)


# ===========================================================================
# Other handlers (unchanged)
# ===========================================================================
@client.on(events.NewMessage(pattern=r"\.logout"))
async def handle_logout(event):
    await event.respond("logging out; deleting session file.")
    session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                f"{SESSION_NAME}.session")
    await client.disconnect()
    if os.path.exists(session_file):
        os.remove(session_file)
        log.info("session deleted: %s", session_file)
    if bot_client is not None:
        try:
            await bot_client.disconnect()
        except Exception:
            pass
    os._exit(0)


@client.on(events.NewMessage(pattern=r"\.adest\s+(-?\d+)"))
async def handle_add_destination(event):
    raw_id = event.pattern_match.group(1)
    full_id = f"-100{raw_id}"
    cfg = _load_config()
    if full_id in cfg["destinations"]:
        await event.respond(f"{full_id} already in destination list.")
        return
    cfg["destinations"].append(full_id)
    _save_config(cfg)
    await event.respond(
        f"added destination {full_id}.\nuse .showdest to see all and pick active."
    )


@client.on(events.NewMessage(pattern=r"\.showdest"))
async def handle_show_destination(event):
    cfg = _load_config()
    if not cfg["destinations"]:
        await event.respond("no destinations configured. use .adest <id> first.")
        return
    buttons = _build_dest_buttons(cfg)
    await event.respond(
        "destination channels — tap to set active:",
        buttons=buttons,
    )


@client.on(events.CallbackQuery(pattern=b"select:"))
async def handle_select_callback(event):
    ch_id = event.data.decode().split(":", 1)[1]
    cfg = _load_config()
    if ch_id not in cfg["destinations"]:
        await event.answer("⚠️ channel not found.", alert=True)
        return
    cfg["selected"] = ch_id
    _save_config(cfg)
    buttons = _build_dest_buttons(cfg)
    await event.edit(buttons=buttons)
    await event.answer(f"active destination set to {ch_id}")


@client.on(events.NewMessage(pattern=r"\.pvtcopy\s+(\S+)\s+(\S+)"))
async def handle_pvtcopy(event):
    start_link = event.pattern_match.group(1)
    end_link = event.pattern_match.group(2)
    start = parse_message_link(start_link)
    end = parse_message_link(end_link)
    if start is None or end is None:
        await event.respond("invalid link format.")
        return
    src_identifier, start_msg_id = start
    _dst_identifier_check, end_msg_id = end
    if src_identifier != _dst_identifier_check:
        await event.respond("start and end links must belong to the same source channel.")
        return
    if start_msg_id > end_msg_id:
        start_msg_id, end_msg_id = end_msg_id, start_msg_id
    dest_channel_id = get_selected_destination()
    msg_count = end_msg_id - start_msg_id + 1
    status_msg = await event.respond(
        f"copying {msg_count} messages\nsrc: {src_identifier} → dst: {dest_channel_id}"
    )
    try:
        src_entity = await client.get_entity(src_identifier)
        dst_entity = await client.get_entity(dest_channel_id)
    except (ChannelPrivateError, ValueError) as e:
        await status_msg.edit(f"cannot access channel: {e}")
        return
    copied = skipped = errors = 0
    log.info("pvtcopy START src=%s dst=%s range=%d..%d", src_identifier, dest_channel_id,
             start_msg_id, end_msg_id)

    # Create job key and state for tracking
    job_key = _job_key(src_identifier, start_msg_id, end_msg_id)
    spinner = Spinner()
    t0 = time.time()
    last_edit_time = 0.0
    last_edit_text = ""
    channel_display = getattr(src_entity, "title", None) or str(src_identifier)

    # Send initial progress message with inline buttons
    initial_text = render_simple_copy_progress(
        spinner=spinner,
        channel=channel_display,
        start_id=start_msg_id,
        end_id=end_msg_id,
        copied=copied,
        skipped=skipped,
        errors=errors,
        current_id=start_msg_id,
        elapsed=0.0,
    )
    status_msg = await event.respond(initial_text, buttons=_job_inline_buttons(job_key))
    last_edit_time = time.monotonic()
    last_edit_text = initial_text

    for msg_id in range(start_msg_id, end_msg_id + 1):
        try:
            messages = await client.get_messages(src_entity, ids=msg_id)
            if not messages:
                skipped += 1
                continue
            msg = messages[0] if isinstance(messages, list) else messages
            if msg is None:
                skipped += 1
                continue
            if msg.media:
                await client.send_file(
                    entity=dst_entity,
                    file=msg.media,
                    caption=msg.message or "",
                    formatting_entities=msg.entities,
                )
            elif msg.message:
                await client.send_message(
                    entity=dst_entity,
                    message=msg.message,
                    formatting_entities=msg.entities,
                )
            else:
                skipped += 1
                continue
            copied += 1
        except FloodWaitError as e:
            wait_time = e.seconds + 5
            log.warning("flood-wait sleeping %ds", wait_time)
            await asyncio.sleep(wait_time)
            msg_id -= 1
            continue
        except (MessageIdInvalidError, MessageDeleteForbiddenError):
            skipped += 1
        except Exception as e:
            log.error("pvtcopy error msg=%d err=%s", msg_id, e)
            errors += 1

        # Rate-limited progress update: max every 2.5s and only if text changed
        now = time.monotonic()
        if now - last_edit_time >= 2.5:
            elapsed = time.time() - t0
            new_text = render_simple_copy_progress(
                spinner=spinner,
                channel=channel_display,
                start_id=start_msg_id,
                end_id=end_msg_id,
                copied=copied,
                skipped=skipped,
                errors=errors,
                current_id=msg_id,
                elapsed=elapsed,
            )
            if new_text != last_edit_text:
                try:
                    await status_msg.edit(new_text, buttons=_job_inline_buttons(job_key))
                    last_edit_text = new_text
                    last_edit_time = now
                except FloodWaitError as e:
                    await asyncio.sleep(getattr(e, "seconds", 0) + 1)
                except Exception:
                    pass

        # Brief pause to avoid rate limits
        await asyncio.sleep(random.uniform(0.1, 0.3))

    # Final completion message
    elapsed = time.time() - t0
    final_text = render_simple_copy_completed(
        channel=channel_display,
        start_id=start_msg_id,
        end_id=end_msg_id,
        copied=copied,
        skipped=skipped,
        errors=errors,
        elapsed=elapsed,
    )
    try:
        await status_msg.edit(final_text)
    except Exception:
        pass
    log.info("pvtcopy DONE copied=%d skipped=%d errors=%d", copied, skipped, errors)


# ===========================================================================
# .del  --  Bulk-delete messages by range
# ===========================================================================
_DEL_BATCH_SIZE = 100


@client.on(events.NewMessage(pattern=r"\.del\s+(\S+)\s+(\S+)"))
async def handle_del(event):
    """Bulk-delete messages in an inclusive range.

    Usage:  .del <start_link> <end_link>
    Both links must belong to the same chat/channel.
    """
    start_link = event.pattern_match.group(1)
    end_link = event.pattern_match.group(2)

    start = parse_message_link(start_link)
    end = parse_message_link(end_link)

    if start is None:
        await event.respond("\u274c Invalid start link.")
        return
    if end is None:
        await event.respond("\u274c Invalid end link.")
        return

    src_start, start_msg_id = start
    src_end, end_msg_id = end

    # Verify both links belong to the same chat/channel.
    if str(src_start) != str(src_end):
        await event.respond(
            "\u274c Both links must belong to the same chat/channel."
        )
        return

    # Normalise so start <= end.
    if start_msg_id > end_msg_id:
        start_msg_id, end_msg_id = end_msg_id, start_msg_id

    total = end_msg_id - start_msg_id + 1

    # Resolve the entity and verify access.
    try:
        entity = await client.get_entity(src_start)
    except ChannelPrivateError:
        await event.respond(
            "\u274c Cannot access this chat. "
            "Make sure the channel is not private or you have access."
        )
        return
    except (ValueError, Exception) as e:
        await event.respond(f"\u274c Cannot resolve chat: {e}")
        return

    # Determine a human-readable channel name for status messages.
    channel_display = getattr(entity, "title", None) or str(src_start)

    # --- Permission pre-check -----------------------------------------------
    # Try to fetch a single message in the range to confirm we can interact
    # with the chat.  A real delete-permission check happens on the first
    # batch; if we lack rights Telegram will reply with ChatAdminRequiredError
    # or MessageDeleteForbiddenError.
    try:
        probe = await client.get_messages(entity, ids=start_msg_id)
        if probe is None:
            # Message might not exist; try the end of the range.
            probe = await client.get_messages(entity, ids=end_msg_id)
    except ChatAdminRequiredError:
        await event.respond(
            "\u274c You don\u2019t have permission to interact with this chat."
        )
        return
    except ChannelPrivateError:
        await event.respond(
            "\u274c The channel is private or inaccessible."
        )
        return

    # --- Begin deletion -----------------------------------------------------
    spinner = Spinner()
    deleted = 0
    skipped = 0
    failed = 0
    t0 = time.time()
    last_edit_time = 0.0
    last_edit_text = ""

    # Send initial progress message
    initial_text = render_delete_progress(
        spinner=spinner,
        channel=channel_display,
        start_id=start_msg_id,
        end_id=end_msg_id,
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        current_id=start_msg_id,
        elapsed=0.0,
    )
    status_msg = await event.respond(initial_text)
    last_edit_time = time.monotonic()
    last_edit_text = initial_text

    ids_to_delete = list(range(start_msg_id, end_msg_id + 1))

    for batch_start in range(0, len(ids_to_delete), _DEL_BATCH_SIZE):
        batch = ids_to_delete[batch_start : batch_start + _DEL_BATCH_SIZE]
        current_id = batch[-1]

        try:
            result = await client.delete_messages(entity, batch)
            if isinstance(result, list):
                deleted += len(result)
                skipped += len(batch) - len(result)
            elif result:
                deleted += 1
                skipped += len(batch) - 1
            else:
                skipped += len(batch)
        except FloodWaitError as e:
            wait_s = getattr(e, "seconds", 0) + 1
            log.warning(".del flood-wait %ds", wait_s)
            await asyncio.sleep(wait_s)
            try:
                result = await client.delete_messages(entity, batch)
                if isinstance(result, list):
                    deleted += len(result)
                    skipped += len(batch) - len(result)
                elif result:
                    deleted += 1
                    skipped += len(batch) - 1
                else:
                    skipped += len(batch)
            except Exception:
                failed += len(batch)
        except ChatAdminRequiredError:
            error_text = render_delete_failed(
                channel=channel_display, start_id=start_msg_id, end_id=end_msg_id,
                deleted=deleted, skipped=skipped, failed=failed,
                elapsed=time.time() - t0, error="No permission to delete messages",
            )
            try: await status_msg.edit(error_text)
            except Exception: pass
            return
        except MessageDeleteForbiddenError:
            error_text = render_delete_failed(
                channel=channel_display, start_id=start_msg_id, end_id=end_msg_id,
                deleted=deleted, skipped=skipped, failed=failed,
                elapsed=time.time() - t0, error="Deletion forbidden in this chat",
            )
            try: await status_msg.edit(error_text)
            except Exception: pass
            return
        except ChannelPrivateError:
            error_text = render_delete_failed(
                channel=channel_display, start_id=start_msg_id, end_id=end_msg_id,
                deleted=deleted, skipped=skipped, failed=failed,
                elapsed=time.time() - t0, error="Channel became inaccessible",
            )
            try: await status_msg.edit(error_text)
            except Exception: pass
            return
        except Exception as e:
            log.error(".del batch error: %s", e)
            failed += len(batch)

        # Rate-limited progress update: max every 2.5s and only if text changed
        now = time.monotonic()
        if now - last_edit_time >= 2.5:
            elapsed = time.time() - t0
            new_text = render_delete_progress(
                spinner=spinner,
                channel=channel_display,
                start_id=start_msg_id,
                end_id=end_msg_id,
                deleted=deleted,
                skipped=skipped,
                failed=failed,
                current_id=current_id,
                elapsed=elapsed,
            )
            if new_text != last_edit_text:
                try:
                    await status_msg.edit(new_text)
                    last_edit_text = new_text
                    last_edit_time = now
                except FloodWaitError as e:
                    await asyncio.sleep(getattr(e, "seconds", 0) + 1)
                except Exception:
                    pass

        # Brief pause between batches to stay under rate limits.
        await asyncio.sleep(0.25)

    # --- Final report -------------------------------------------------------
    elapsed = time.time() - t0
    final_text = render_delete_completed(
        channel=channel_display,
        start_id=start_msg_id,
        end_id=end_msg_id,
        deleted=deleted,
        skipped=skipped,
        failed=failed,
        elapsed=elapsed,
    )
    try:
        await status_msg.edit(final_text)
    except Exception:
        pass
    log.info(
        ".del DONE chat=%s range=%d..%d deleted=%d skipped=%d failed=%d elapsed=%.1fs",
        channel_display, start_msg_id, end_msg_id, deleted, skipped, failed, elapsed,
    )


@client.on(events.NewMessage(pattern=r"\.help"))
async def handle_help(event):
    bot_friendly = f"@{BOT_USERNAME}" if BOT_USERNAME else "your Bot"
    await event.respond(
        "private channel copy bot — commands:\n\n"
        ".pvtcopy <start> <end>\n"
        "    server-side copy. works on channels where forwarding is allowed.\n"
        "    strips the forwarded-from tag.\n\n"
        ".pvtcopydl <start> <end>\n"
        "    download → upload → verify → delete pipeline.\n"
        "    for channels where forwarding is disabled.\n"
        f"    live progress (bar / eta / speed / refresh) appears in chat with {bot_friendly}.\n"
        "    resumable after crashes; ETA derived from rolling per-item times.\n\n"
        ".pvtcopydlcancel <job_key>\n"
        "    cancel a .pvtcopydl job.\n\n"
        ".del <start_link> <end_link>\n"
        "    bulk-delete messages in range (inclusive).\n"
        "    accepts public and private t.me links.\n\n"
        ".adest <channel_id>\n"
        "    .showdest  — add or browse destination channels.\n\n"
        ".logout — disconnect.\n\n"
        ".help — this message."
    )


# ===========================================================================
# Bot-driven helpers  (second Telethon client, authenticates with BOT_TOKEN)
# ===========================================================================
def _register_bot_handlers() -> None:
    if bot_client is None:
        return

    @bot_client.on(events.NewMessage(pattern=r"/start(?:\s+(.*))?"))
    async def _bot_start(event):
        chat = await event.get_chat()
        chat_id = event.chat_id
        _write_progress_chat_id(chat_id)
        # Update module-level cache so future .pvtcopydl jobs see this chat.
        global _progress_chat_id, _progress_chat_loaded
        _progress_chat_id = chat_id
        _progress_chat_loaded = True
        first = getattr(chat, "first_name", "") or "there"
        log.info("bot /start chat_id=%d user=%s", chat_id, first)
        await event.respond(
            "hey 👋\n\n"
            "you're now paired as the live-progress channel for the copybot.\n"
            "every .pvtcopydl job the userbot runs will post a progress "
            "message here with: bar / ETA / download speed / refresh and "
            "cancel buttons.\n\n"
            "tap refresh any time to force a re-render."
        )

    @bot_client.on(events.CallbackQuery(pattern=b"noop"))
    async def _bot_noop(event):
        await event.answer("job done.")

    @bot_client.on(events.CallbackQuery(pattern=b"refresh"))
    async def _bot_refresh_latest(event):
        await event.answer("refreshing...")
        await _refresh_for(event, None, force=True)

    @bot_client.on(events.CallbackQuery(pattern=rb"refresh:(.+)"))
    async def _bot_refresh(event):
        job_key = event.pattern_match.group(1).decode()
        await event.answer("refreshing...")
        await _refresh_for(event, job_key, force=True)

    @bot_client.on(events.CallbackQuery(pattern=b"cancel"))
    async def _bot_cancel_latest(event):
        await _cancel_for(event, None)    @bot_client.on(events.CallbackQuery(pattern=rb"cancel:(.+)"))
    async def _bot_cancel(event):
        job_key = event.pattern_match.group(1).decode()
        await _cancel_for(event, job_key)


async def _refresh_for(event, job_key: str | None, *, force: bool) -> None:
    """Force-re-render whichever live job(s) match `job_key` (None = all).

    Uses ACTIVE_DL_STATES so we read the same in-memory state the loop is
    mutating, not a stale checkpoint from disk.

    `event` is the CallbackQuery so we can answer the toast in the user's
    chat even when there is nothing to refresh.
    """
    if job_key:
        state = ACTIVE_DL_STATES.get(job_key)
        if state is not None:
            await _refresh_bot_progress(state, force=force)
        else:
            try:
                await event.respond(f"no live job with key `{job_key}`.")
            except Exception:
                pass
        return
    if not ACTIVE_DL_STATES:
        try:
            await event.respond("no active .pvtcopydl jobs at the moment.")
        except Exception:
            pass
        return
    for state in list(ACTIVE_DL_STATES.values()):
        await _refresh_bot_progress(state, force=force)


async def _cancel_for(event, job_key: str | None) -> None:
    """Cancel a running job (or the most recent one) via bot-side button."""
    if job_key:
        task = ACTIVE_DL_JOBS.get(job_key)
        if task and not task.done():
            task.cancel()
            log.info("bot cancel job=%s", job_key)
            try:
                await event.respond(f"⏹ cancel requested for job `{job_key}`.")
            except Exception:
                pass
        else:
            try:
                await event.respond(f"no live job with key `{job_key}`.")
            except Exception:
                pass
        return
    if not ACTIVE_DL_JOBS:
        try:
            await event.respond("no active jobs to cancel.")
        except Exception:
            pass
        return
    key, task = next(iter(ACTIVE_DL_JOBS.items()))
    task.cancel()
    log.info("bot cancel latest job=%s", key)
    try:
        await event.respond(f"⏹ cancel requested for latest job `{key}`.")
    except Exception:
        pass


# ===========================================================================
# User-account inline button handlers  (progress in user's chat)
# ===========================================================================
@client.on(events.CallbackQuery(pattern=rb"refresh:(.+)"))
async def _user_refresh(event):
    """Handle Refresh button press in user's chat."""
    job_key = event.pattern_match.group(1).decode()
    state = ACTIVE_DL_STATES.get(job_key)
    if state is None:
        await event.answer("no live job found.", alert=False)
        return
    await event.answer("refreshing...")
    # Force re-render the progress message
    try:
        spinner = state.get("_spinner")
        if spinner is None:
            from progress_ui import Spinner as _S
            spinner = _S()
            state["_spinner"] = spinner
        chat_id = state.get("progress_chat_id")
        msg_id = state.get("progress_msg_id")
        if chat_id and msg_id:
            text = _format_bot_status(state)
            await client.edit_message(chat_id, msg_id, text, buttons=_job_inline_buttons(job_key))
    except Exception as e:
        log.debug("user refresh failed: %s", e)


@client.on(events.CallbackQuery(pattern=rb"cancel:(.+)"))
async def _user_cancel(event):
    """Handle Cancel button press in user's chat."""
    job_key = event.pattern_match.group(1).decode()
    task = ACTIVE_DL_JOBS.get(job_key)
    if task and not task.done():
        task.cancel()
        log.info("user cancel job=%s", job_key)
        await event.answer(f"cancelling {job_key}...")
    else:
        await event.answer("job already finished.", alert=False)


# ===========================================================================
# Main entry point
# ===========================================================================
async def main():
    # All globals used in this body are declared up-front so the implicit-
    # global pass doesn't trip the "used prior to global declaration"
    # SyntaxError later in this function.
    global ME_ID, BOT_USERNAME, BOT_ID, bot_client

    log.info("connecting userbot...")
    await client.start()
    me = await client.get_me()
    ME_ID = me.id
    log.info(
        "userbot OK: %s (id=%d)  active-dst=%s",
        me.first_name, me.id, get_selected_destination(),
    )

    if bot_client is not None:
        try:
            log.info("connecting bot_client...")
            await bot_client.start(bot_token=BOT_TOKEN)
            bot_me = await bot_client.get_me()
            BOT_USERNAME = bot_me.username
            BOT_ID = bot_me.id
            log.info(
                "bot_client OK: @%s (id=%d)  url=t.me/%s",
                bot_me.username, bot_me.id, bot_me.username,
            )
            _register_bot_handlers()
        except Exception as e:
            log.error(
                "bot_client start failed: %s - live progress DISABLED", e
            )
            # Reset the global so later `if bot_client is not None` guards
            # correctly short-circuit instead of trying to send messages on
            # an unauthenticated client.
            bot_client = None

    if bot_client is None:
        log.warning(
            "no BOT_TOKEN (or bot_client failed to start) - live bar / ETA / "
            "speed disabled; user-account message edits will be used instead."
        )

    # bot_client is supervised in the background so a bot-side disconnect
    # (network blip, server restart) doesn't kill the userbot.  When the
    # user issues .logout both clients are explicitly disconnected.  We add
    # a done_callback so a silent disconnect is at least logged.
    if bot_client is not None:
        async def _supervise_bot():
            try:
                await bot_client.run_until_disconnected()
            except Exception as e:
                log.error("bot_client run_until_disconnected raised: %r", e)
        bot_task = asyncio.create_task(_supervise_bot())
        bot_task.add_done_callback(
            lambda t: log.warning("bot_client task ended: %r", t.exception())
            if not t.cancelled() and t.exception() is not None else None
        )

    # Block on the userbot.  KeyboardInterrupt / SIGTERM cleanly cancel
    # this task; bot_client is left to its own devices until explicit
    # disconnect from .logout.
    try:
        await client.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        if bot_client is not None:
            try:
                await bot_client.disconnect()
            except Exception:
                pass
        raise


if __name__ == "__main__":
    asyncio.run(main())
