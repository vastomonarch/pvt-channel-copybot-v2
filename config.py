"""Environment-driven configuration for the copy bot.

All tunables come from environment variables (usually via ``.env``) with
sensible defaults, so nothing is hard-coded to a particular server layout.
Secrets are never logged: :meth:`Config.redacted` returns a safe summary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is optional at import time (tests may not install it)
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore[misc]
        return False

__all__ = ["Config", "ConfigError"]

PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Invalid or missing configuration."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class Config:
    # ── Telegram API credentials (required) ────────────────────────────
    api_id: int
    api_hash: str
    bot_token: str = ""

    # ── Runtime directories ────────────────────────────────────────────
    data_dir: Path = PROJECT_ROOT / "data"
    staging_dir: Path = PROJECT_ROOT / "_pvtcopydl_tmp"  # legacy name kept

    # ── TDLib native library ───────────────────────────────────────────
    tdlib_library_path: str = ""          # empty → auto-resolve
    tdlib_log_verbosity: int = 1          # 0=fatal … 4=debug (native log)
    tdlib_log_file: str = ""              # empty → data_dir/tdlib_native.log
    tdlib_db_key: str = ""                # optional database encryption key

    # ── Concurrency / limits ───────────────────────────────────────────
    max_concurrent_downloads: int = 3
    max_concurrent_uploads: int = 2
    max_queued_jobs: int = 8
    max_running_jobs: int = 2
    retry_count: int = 4
    request_timeout: float = 60.0         # generic TDLib request timeout (s)
    send_timeout: float = 1800.0          # message send / large upload (s)
    download_stall_timeout: float = 300.0 # no-progress watchdog (s)
    progress_edit_interval: float = 2.5   # min seconds between progress edits
    album_window: int = 15                # message fetch window for albums
    prefetch_depth: int = 6               # messages downloaded ahead of upload
    shutdown_timeout: float = 20.0        # graceful shutdown budget (s)
    max_single_file_bytes: int = 1900 * 1024 * 1024  # below Telegram's 2 GB
    split_verify_hash: bool = False       # expensive full-hash split verify
    stop_on_upload_error: bool = True     # fail job instead of skipping (legacy)

    # ── Logging / misc ─────────────────────────────────────────────────
    log_level: str = "INFO"
    destination: str = ""                 # fallback destination chat id
    first_name: str = "Copy"              # registration prompt default
    last_name: str = ""

    @classmethod
    def from_env(cls, *, load: bool = True) -> "Config":
        if load:
            load_dotenv()
        api_id = _env_int("API_ID", 0)
        api_hash = os.getenv("API_HASH", "").strip()
        if not api_id or not api_hash:
            raise ConfigError(
                "API_ID and API_HASH must be set in .env (see .env.example)")
        data_dir = _env_path("DATA_DIR", PROJECT_ROOT / "data")
        staging = _env_path("STAGING_DIR", PROJECT_ROOT / "_pvtcopydl_tmp")
        cfg = cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            data_dir=data_dir,
            staging_dir=staging,
            tdlib_library_path=os.getenv("TDLIB_LIBRARY_PATH", "").strip(),
            tdlib_log_verbosity=_env_int("TDLIB_LOG_VERBOSITY", 1),
            tdlib_log_file=os.getenv("TDLIB_LOG_FILE", "").strip(),
            tdlib_db_key=os.getenv("TDLIB_DB_KEY", ""),
            max_concurrent_downloads=_env_int("MAX_CONCURRENT_DOWNLOADS", 3),
            max_concurrent_uploads=_env_int("MAX_CONCURRENT_UPLOADS", 2),
            max_queued_jobs=_env_int("MAX_QUEUED_JOBS", 8),
            max_running_jobs=_env_int("MAX_RUNNING_JOBS", 2),
            retry_count=_env_int("RETRY_COUNT", 4),
            request_timeout=_env_float("REQUEST_TIMEOUT", 60.0),
            send_timeout=_env_float("SEND_TIMEOUT", 1800.0),
            download_stall_timeout=_env_float("DOWNLOAD_STALL_TIMEOUT", 300.0),
            progress_edit_interval=_env_float("PROGRESS_EDIT_INTERVAL", 2.5),
            album_window=_env_int("ALBUM_WINDOW", 15),
            prefetch_depth=_env_int("PREFETCH_DEPTH", 6),
            shutdown_timeout=_env_float("SHUTDOWN_TIMEOUT", 20.0),
            max_single_file_bytes=_env_int(
                "MAX_SINGLE_FILE_BYTES", 1900 * 1024 * 1024),
            split_verify_hash=_env_bool("SPLIT_VERIFY_HASH", False),
            log_level=(os.getenv("LOG_LEVEL", "INFO") or "INFO").upper(),
            destination=os.getenv("DESTINATION", "").strip(),
            first_name=os.getenv("REGISTER_FIRST_NAME", "Copy"),
            last_name=os.getenv("REGISTER_LAST_NAME", ""),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.max_concurrent_downloads < 1 or self.max_concurrent_uploads < 1:
            raise ConfigError("concurrency limits must be >= 1")
        if self.max_queued_jobs < 1:
            raise ConfigError("MAX_QUEUED_JOBS must be >= 1")
        if self.max_running_jobs < 1:
            raise ConfigError("MAX_RUNNING_JOBS must be >= 1")
        if self.retry_count < 0:
            raise ConfigError("RETRY_COUNT must be >= 0")
        if not 0 <= self.tdlib_log_verbosity <= 4:
            raise ConfigError("TDLIB_LOG_VERBOSITY must be 0..4")

    # ── derived paths ──────────────────────────────────────────────────
    @property
    def user_db_dir(self) -> Path:
        return self.data_dir / "tdlib" / "user"

    @property
    def bot_db_dir(self) -> Path:
        return self.data_dir / "tdlib" / "bot"

    @property
    def user_files_dir(self) -> Path:
        return self.data_dir / "files" / "user"

    @property
    def bot_files_dir(self) -> Path:
        return self.data_dir / "files" / "bot"

    @property
    def jobs_db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def native_log_file(self) -> str:
        if self.tdlib_log_file:
            return self.tdlib_log_file
        return str(self.log_dir / "tdlib_native.log")

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.staging_dir, self.user_db_dir,
                  self.bot_db_dir, self.user_files_dir, self.bot_files_dir,
                  self.log_dir):
            p.mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict:
        """Config summary safe for logging (no secrets)."""
        return {
            "api_id": self.api_id,
            "api_hash": "***",
            "bot_token": "***" if self.bot_token else "(none)",
            "data_dir": str(self.data_dir),
            "staging_dir": str(self.staging_dir),
            "tdlib_library_path": self.tdlib_library_path or "(auto)",
            "max_concurrent_downloads": self.max_concurrent_downloads,
            "max_concurrent_uploads": self.max_concurrent_uploads,
            "max_queued_jobs": self.max_queued_jobs,
            "max_running_jobs": self.max_running_jobs,
            "retry_count": self.retry_count,
            "request_timeout": self.request_timeout,
            "progress_edit_interval": self.progress_edit_interval,
            "max_single_file_bytes": self.max_single_file_bytes,
        }
