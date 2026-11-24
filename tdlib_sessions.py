"""Persistent TDLib database & session lifecycle.

Separation of concerns (explicitly required by the migration):

* **TDLib owns its database files.**  We never open, migrate, or modify the
  files under ``data/tdlib/*`` ourselves — we only pass the directory paths
  to ``setTdlibParameters``.
* **One process per database.**  An advisory ``flock`` on a sidecar lock
  file prevents two bot processes from opening the same TDLib database
  (the root cause of the historical *"database is locked"* crashes).
* **Per-account isolation.**  The user account and the bot account use
  different database *and* file-cache directories.
* **Telethon sessions are untouched.**  Legacy ``*.session`` files are
  never opened, converted, or deleted by this module; they remain on disk
  for rollback.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import pathlib
from typing import Any

log = logging.getLogger("copybot.tdlib.sessions")

__all__ = ["DatabaseLockedError", "InstanceLock", "SessionManager"]


class DatabaseLockedError(RuntimeError):
    """Another process already owns this TDLib database directory."""


class InstanceLock:
    """Advisory exclusive lock guarding one TDLib database directory.

    ``flock`` is released automatically by the kernel when the process
    dies (including SIGKILL), so a crashed process can never leave the
    database locked behind — unlike SQLite page locks on overlayfs.
    """

    def __init__(self, directory: pathlib.Path, label: str):
        self.path = directory / "copybot.lock"
        self.label = label
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = ""
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 512).decode("utf-8", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DatabaseLockedError(
                    f"TDLib database {self.label!r} is locked by another "
                    f"process ({holder or 'unknown'}). Only one copybot "
                    "process may use a given database directory."
                ) from exc
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} label={self.label}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class SessionManager:
    """Owns directory layout, locks and ``setTdlibParameters`` payloads."""

    def __init__(self, label: str, db_dir: pathlib.Path,
                 files_dir: pathlib.Path):
        self.label = label
        self.db_dir = pathlib.Path(db_dir)
        self.files_dir = pathlib.Path(files_dir)
        self.lock: InstanceLock | None = None

    def acquire(self) -> None:
        """Take the single-process lock (call before creating the client)."""
        if self.lock is None:
            self.lock = InstanceLock(self.db_dir, self.label)
            self.lock.acquire()
            log.info("session lock acquired: %s (%s)",
                     self.lock.path, self.label)

    def release(self) -> None:
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def tdlib_parameters(self, *, api_id: int, api_hash: str,
                         encryption_key: str = "",
                         application_version: str = "1.0") -> dict[str, Any]:
        """Build the ``setTdlibParameters`` payload for this account."""
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        return {
            "@type": "setTdlibParameters",
            "use_test_dc": False,
            "database_directory": str(self.db_dir),
            "files_directory": str(self.files_dir),
            "database_encryption_key": encryption_key or "",
            "use_file_database": True,
            "use_chat_info_database": True,
            "use_message_database": True,
            "use_secret_chats": False,
            "api_id": api_id,
            "api_hash": api_hash,
            "system_language_code": "en",
            "device_model": "linux-server",
            "system_version": "linux",
            "application_version": application_version,
        }

    @property
    def lock_path(self) -> pathlib.Path:
        return self.db_dir / "copybot.lock"
