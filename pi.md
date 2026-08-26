# pvt-channel-copybot-v2 — Reference

> Last updated: 2026-08-05 (commit `5d0d7a8`, branch `omega` + `main`)
> Repo: https://github.com/vastomonarch/pvt-channel-copybot-v2

---

## 1. What the bot does

A **Telethon userbot** that copies messages from private Telegram channels to destination channels. Two modes of operation:

- **`.pvtcopy`** — Server-side copy (uses Telegram's native forward, strips "Forwarded from" tag). Only works when the source channel allows forwarding.
- **`.pvtcopydl`** — Download → Upload pipeline. Works on channels where forwarding is disabled. Downloads each media file to disk, then re-uploads to the destination channel. This is the primary mode for restrictive channels.

Additional commands: bulk delete (`.del`), destination management (`.adest`, `.showdest`), job resume (`.pvtcopydlcontinue`), job cancel (`.pvtcopydlcancel`), logout (`.logout`).

---

## 2. Architecture

```
bot.py                  Main bot: handlers, progress routing, core loop
pipeline.py             Download/upload with progress callbacks (Telethon + aria2c stubs)
file_splitter.py        File splitting: 7z volumes (archives), ffmpeg (videos), binary (fallback)
wal_session.py          SQLite WAL session to avoid "database is locked" on overlayfs
trigger_pvtcopydl.py    Headless trigger for testing without Telegram client
progress_ui/            Modular progress UI (spinner, bar, renderer, formatter)
```

**Two clients run simultaneously:**
- **User client** (`client`) — Your Telegram account. Does all the actual copying, downloading, uploading.
- **Bot client** (`bot_client`) — A separate Telegram bot (via BOT_TOKEN). Sends live progress updates to the user so the user account chat isn't spammed with edits.

**Session management:** `WALSession` subclasses Telethon's `SQLiteSession` and enables WAL journal mode + `busy_timeout=10000` to prevent SQLite locking issues on overlayfs filesystems.

---

## 3. File structure

```
bot.py                  ~2700 lines — handlers, core loop, progress UI
pipeline.py             ~400 lines — TransferProgress, upload/download with callbacks
file_splitter.py        ~300 lines — split_file(), archive/video/binary strategies
wal_session.py          ~20 lines  — WAL SQLite session
trigger_pvtcopydl.py    ~180 lines — headless test runner
progress_ui/__init__.py exports
progress_ui/constants.py  box chars, spinner frames
progress_ui/formatter.py  human-readable size/speed/time
progress_ui/progress.py   unicode progress bar
progress_ui/renderer.py   box-drawing dashboard renderers
progress_ui/spinner.py    spinner animation
config.json            runtime config (destinations)
.progress_chat          persisted bot chat_id for progress routing
_pvtcopydl_tmp/        job dir: temp files, meta JSON
config.example.json    template
.env.example           template (API_ID, API_HASH, BOT_TOKEN)
requirements.txt       telethon, python-dotenv, cryptg, aiohttp
```

---

## 4. Environment & Config

**`.env` variables:**
- `API_ID` / `API_HASH` — from my.telegram.org (required)
- `BOT_TOKEN` — from @BotFather (optional, enables live progress)
- `DESTINATION` — fallback channel ID if config.json missing
- `LOG_LEVEL` — override logging level (e.g. `DEBUG`)

**`config.json`** (auto-created):
```json
{"destinations": ["-100XXXXXXXXXX"], "selected": "-100XXXXXXXXXX"}
```

---

## 5. Commands

| Command | Description |
|---------|-------------|
| `.pvtcopy <start_link> <end_link>` | Server-side copy (strips forwarded-from) |
| `.pvtcopydl <start_link> <end_link>` | Download → Upload pipeline with live progress |
| `.pvtcopydlcontinue <job_key>` | Resume interrupted .pvtcopydl job |
| `.pvtcopydlcancel [job_key]` | Cancel running job (no key = cancel latest) |
| `.del <start_link> <end_link>` | Bulk-delete messages in range |
| `.adest <channel_id>` | Add destination channel |
| `.showdest` | View/select active destination |
| `.logout` | Disconnect and delete session |
| `.help` | Show commands |

All commands are **dot-prefixed** (userbot convention). Owner-only via `ME_ID` check.

---

## 6. `.pvtcopydl` — the main pipeline

### Flow per message:
1. **Fetch** — Batch fetch messages in windows of 15 (album support)
2. **Classify** — `service`, `text`, `poll`, `contact`, `location`, `media`
3. **Download** — Media files downloaded via `download_with_progress()` with live progress callbacks
4. **Split** — If file > `MAX_SINGLE_FILE_BYTES` (1.9 GB), split via `split_file()`
5. **Upload** — Upload via `upload_with_progress()` with live progress callbacks. Split parts uploaded sequentially with `📦 Part N/M` captions.
6. **Verify** — Re-fetch uploaded message and compare sizes/content
7. **Cleanup** — Delete temp files after successful upload
8. **Checkpoint** — Save job meta to disk after each message

### Album handling:
- Messages with the same `grouped_id` are collected as siblings
- All siblings downloaded, then uploaded as a single `SendMultiMediaRequest`
- Thumbnail preservation, original document attributes preserved
- Individual per-file progress within album

### Message types handled:
- **text** → `send_message` with original entities
- **media** (documents, videos, audio, photos) → download + re-upload with attributes
- **poll** → formatted as text with question + options
- **contact** → formatted as text card
- **location** → Google Maps link + coordinates
- **service** (joins, leaves) → skipped

---

## 7. File splitting (`file_splitter.py`)

### Strategy priority:
1. **Archive files** (.7z, .rar, .zip, .tar*, .gz, .bz2, .xz, .zst) → Extract to temp dir, repack as **7z volumes** (`-v` switch). Each volume ≤ limit, self-contained.
2. **Video files** (.mp4, .mkv, .avi, etc.) → ffmpeg segment muxer (`-c copy`, each segment playable)
3. **Everything else** → `coreutils split` with numeric suffixes (recombine with `cat`)

### Constants:
- `MAX_SINGLE_FILE_BYTES = 1900 * 1024 * 1024` (~1.9 GB) — safely below Telegram's 2 GB MTProto limit
- Each split part verified to be ≤ `max_size` before returning

### Archive volume naming:
- `original.7z.001`, `original.7z.002`, `original.7z.003`, ...
- Extract from `.001` — 7z auto-finds siblings in same directory
- Fallback to binary split if 7z fails

### Key functions:
- `split_file(path, max_size, mime_type) -> list[str]` — main entry point
- `_split_archive_7z(path, max_size)` — extract → repackage as 7z volumes
- `_split_video_ffmpeg(path, max_size, ext)` — ffmpeg segment split
- `_split_binary(path, max_size)` — coreutils split fallback
- `_is_archive(path)` — checks compound extensions (`.tar.gz` etc.)
- `_double_ext(path)` — returns effective compound extension

---

## 8. Progress UI

### Dual-channel progress:
- **Bot chat** (primary): Live dashboard with spinner, progress bar, speed, ETA, counters. Throttled to edits every 2.5s.
- **User chat** (fallback): Same dashboard via user client edits when bot_client unavailable.

### Dashboard components:
- `Spinner` — rotating Unicode character
- `progress_bar()` — `█` / `░` block characters
- Box-drawing renderer with sections: header, filename, bar, size, speed, ETA, counters
- Inline buttons: 🔄 Refresh (force re-render), ⏹ Cancel

### Speed calculation:
- Rolling window of last 8 measurements (`SPEED_WINDOW = 8`)
- Smoothed: `speed = (current_bytes - last_bytes) / dt`
- Download and upload speeds tracked independently

### Throttling:
- `BOT_PROGRESS_EVERY_SEC = 2.5` — max edit frequency
- `MessageNotModifiedError` silently caught (identical text)
- `MessageIdInvalidError` clears progress_msg_id (message deleted)

---

## 9. Error handling & recovery

### Retries:
- Downloads: 2 retries with `FileReferenceExpiredError` re-fetch
- Uploads: 4 attempts + 2 additional retry rounds with 10s sleep
- FloodWait: sleep for `e.seconds + 2`
- Connection errors: 5s backoff

### Job persistence:
- `_save_job_meta()` writes JSON checkpoint after each message
- `.pvtcopydlcontinue` reads meta and resumes from `current_msg_id`
- Uploaded message IDs tracked for skip-on-resume

### Cancellation:
- `asyncio.CancelledError` propagates from `task.cancel()`
- Progress message updated on cancel
- Temp files cleaned up in `finally` blocks

---

## 10. Bug fixes history

### Fix: `MAX_SINGLE_FILE_BYTES` too conservative
- **Before:** `1000 * 1024 * 1024` (1 GB) — files between 1-2 GB were unnecessarily split
- **After:** `1900 * 1024 * 1024` (~1.9 GB) — safely below Telegram's 2 GB cap with ~100 MB headroom
- **Files:** `bot.py`, `pipeline.py`

### Fix: Archive files binary-split into non-extractable chunks
- **Before:** Archives (.7z, .rar, .zip) were split with `coreutils split`, producing raw `.part_001` files that couldn't be individually extracted
- **After:** Archives extracted safely to temp dir, then repackaged as **7z volumes** (`.7z.001`, `.7z.002`, ...). Each volume is a valid archive extractable from `.001`.
- **Files:** `file_splitter.py` (new `_split_archive_7z()` function)

### Fix: SQLite "database is locked" on overlayfs
- **Problem:** Overlayfs mishandles POSIX `fcntl` advisory locks that SQLite uses when escalating to EXCLUSIVE lock. After SIGKILL, the lock state persists and next boot gets `OperationalError: database is locked`.
- **Fix:** `WALSession` subclass enables WAL journal mode + `busy_timeout=10000`. WAL never needs exclusive DB-level locks for reads.
- **File:** `wal_session.py`

### Fix: `total` NameError in `_loop_pvtcopydl`
- **Before:** `total` variable was used but never defined in the loop scope
- **After:** Added `total = end_id - start_id + 1` at loop start
- **File:** `bot.py`

### Fix: FloodWait sleep not including buffer
- **Before:** Slept exactly `e.seconds`, causing immediate re-trigger
- **After:** `await asyncio.sleep(getattr(e, "seconds", 0) + 2)` — 2 second buffer

### Fix: Upload retry logic not breaking on persistent failure
- **Before:** Could loop indefinitely on some error paths
- **After:** Clear `break` after 2 retry rounds; job stops with error message on persistent failure

### Fix: `tc` NameError in `handle_pvtcopy` and `handle_del`
- **Problem:** `src_entity = await tc.get_entity(...)` raised `NameError: name 'tc' is not defined` when executing `.pvtcopy` or `.del`.
- **Fix:** Added `tc = client` at start of `handle_pvtcopy()` and `handle_del()` handlers.
- **File:** `bot.py`

### Fix: `MessageMediaWebPage` error in `_classify` and `handle_pvtcopy`
- **Problem:** Text messages with link previews (`MessageMediaWebPage`) caused `TypeError: Cannot use <MessageMediaWebPage> as file` when passed to `tc.send_file()`.
- **Fix:** Updated `_classify()` to treat `MessageMediaWebPage`, `MessageMediaEmpty`, and `MessageMediaUnsupported` as text messages (when text is present) or service messages, and updated `handle_pvtcopy()` to dispatch message types via `_classify()`.
- **File:** `bot.py`

---

## 11. Known limitations

- **2 GB per file hard cap** — files > 2 GB are split; this is a Telegram MTProto limit
- **Bot API limit is 50 MB** — only relevant if using Bot API uploads (this project uses MTProto via Telethon)
- **Premium users** get 4 GB limit on some endpoints — this bot uses the standard 2 GB limit
- **Albums** — large individual files within albums may exceed the limit and are skipped with `too_big = True`
- **Service messages** (joins, leaves, etc.) are always skipped
- **Resumability** — checkpoint is per-message, not per-file; a partially uploaded file is re-uploaded from scratch on resume
- **7z archive re-extraction** — multi-volume 7z archives require ALL volumes in the same directory to extract

---

## 12. Dependencies

```
telethon>=1.36.0        Telegram MTProto client
python-dotenv>=1.0.0    .env loading
cryptg>=0.4.0           Cryptographic acceleration for Telethon
aiohttp>=3.9.0          Async HTTP (aria2c RPC, future use)
```

**System tools (not pip):**
- `ffmpeg` / `ffprobe` — video segment splitting
- `7z` — archive extraction and volume creation
- `split` (coreutils) — binary file splitting

---

## 13. Key constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_SINGLE_FILE_BYTES` | `1900 * 1024 * 1024` (~1.9 GB) | Split threshold; safely below 2 GB Telegram limit |
| `BOT_PROGRESS_EVERY_SEC` | `2.5` | Max frequency of bot progress edits (below Telegram's 20/min) |
| `SPEED_WINDOW` | `8` | Rolling measurements for speed calculation |
| `ALBUM_WINDOW` | `15` | Message fetch window for album detection |
| `_DEL_BATCH_SIZE` | `100` | Messages per delete batch |
| `USER_STATUS_EDIT_EVERY` | `25` | Message-level milestone edit interval |

---

## 14. Deployment

```bash
# On server (Termux / Linux):
cd pvt_channel_copybot_v2
cp .env.example .env    # fill API_ID, API_HASH, BOT_TOKEN
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python bot.py

# Headless testing:
python trigger_pvtcopydl.py
```

Session files: `copybot_session.session` (user), `copybot_bot_session.session` (bot).
Config: `config.json` (auto-created from .env or default destination).
