# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — TDLib migration (`omega`)

### Added
- **TDLib engine** via ctypes `td_json_client_*` bindings (`tdlib_native.py`,
  `tdlib_client.py`, `tdlib_updates.py`, `tdlib_auth.py`,
  `tdlib_sessions.py`, `tdlib_messages.py`, `tdlib_media.py`).
- **Transactional job store** (`checkpoint.py`, SQLite WAL, schema version,
  atomic state transitions, per-message markers, legacy `*_meta.json`
  import).
- **Job manager** (`job_manager.py`): dedup, bounded queue/running jobs,
  cooperative cancel/pause/resume, stale-row recovery, tracked tasks.
- **Copy pipeline** (`pipeline.py`): server-side copy fast path
  (`forwardMessages send_copy/remove_caption`), per-message
  download+reupload fallback, album handling, crash-mid-send recovery,
  bounded semaphores, throttled progress, classified retries.
- **Offline tests**: `tests/test_payload_shapes.py` (real libtdjson, 33
  shapes), `tests/test_tdlib_runtime.py`, `tests/test_checkpoint.py`,
  `tests/test_job_manager.py`, `tests/test_errors.py`,
  `tests/test_file_splitter.py`, `tests/test_pipeline.py`.
- Deployment artifacts: `deploy/copybot.service`, `deploy/install.sh`,
  `.env.example` refresh.
- Docs: `docs/INSTALL-TDLIB.md`, `docs/BEHAVIOR-CHANGES.md`,
  `docs/UPGRADE-AND-ROLLBACK.md`, migration note updates.

### Changed
- **Breaking runtime dependency**: Telethon removed; TDLib (`libtdjson`)
  required. Zero Telethon imports at runtime.
- `bot.py` rewritten on top of TDLib clients + JobManager + CopyPipeline.
- `.pvtcopy` is server-only; restricted forwarding returns a clear hint to
  use `.pvtcopydl` instead of silently re-uploading.
- `.logout` performs graceful shutdown (no `os._exit`); Telegram session
  data remains in `data/tdlib/` until intentionally removed.
- `.update` pulls the current tracking branch with `--ff-only`, runs
  git/pip off the event loop, and restarts gracefully after shutdown.
- `.del` uses batched `deleteMessages` with per-id salvage.
- `config.json` and `.progress_chat` writes are atomic.
- `requirements.txt` no longer installs Telethon/cryptg/aiohttp; it
  documents TDLib as a system/shared-library dependency.
- `file_splitter.py` verifies parts before deleting the original; supports
  `SPLIT_VERIFY_HASH`.

### Fixed
- `ACTIVE_DL_JOBS` overwrite / leaked task on duplicate job issue.
- Non-atomic config writes.
- Untracked `asyncio.create_task` callbacks (router now tracks/drains).
- Graceful SIGTERM/SIGINT handling with bounded shutdown.
- Queue pump: queued jobs start when a running slot frees.
- Progress edits use TDLib `parseTextEntities` and are throttled; edit
  failures never kill a job.
- Server-copy batch counter is recorded (`uploaded` bumps), so completion
  screens are not misleading.

### Security
- Secrets (API hash, bot token, phone code, 2FA password) are not logged.
- TDLib account DBs live under git-ignored `data/tdlib/`; `.env` stays
  git-ignored. Legacy `*.session` files remain ignored and untouched.

## [1.0.0] - 2025-01-01 (pre-migration)

### Added
- Initial Telethon-based release.
- `.pvtcopy`, `.pvtcopydl`, `.pvtcopydlcancel`, `.adest`, `.showdest`,
  `.logout`.
- Bot-driven live progress bar with refresh/cancel buttons.
- Resumable downloads with JSON checkpoint files.
