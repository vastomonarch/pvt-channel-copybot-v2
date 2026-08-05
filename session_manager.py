"""Session manager for multi-account Telethon support.

Manages multiple Telegram user account sessions with shared API credentials.
Each session has its own .session file in the sessions/ directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from telethon import TelegramClient

log = logging.getLogger("copybot.sessions")

SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"


def _enable_wal_journal(path: str) -> None:
    """Enable WAL journal mode on SQLite session files to avoid lock issues."""
    if not os.path.exists(path):
        return
    # Remove stale WAL/SHM files from crashed sessions
    for suffix in ("-wal", "-shm"):
        stale = path + suffix
        if os.path.exists(stale) and os.path.getsize(stale) == 0:
            try:
                os.remove(stale)
            except OSError:
                pass
    try:
        c = sqlite3.connect(path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA busy_timeout=5000;")
        c.close()
    except Exception as e:
        log.debug("WAL setup failed for %s: %s", path, e)


def mask_phone(phone: str | None) -> str:
    """Mask phone number for display: +1***1234."""
    if not phone:
        return "unknown"
    clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if len(clean) <= 4:
        return f"+{clean}"
    if len(clean) > 7:
        country = clean[: len(clean) - 4]
        last4 = clean[-4:]
        masked = "*" * min(len(country) + 3, 6)
        return f"+{country}{masked}{last4}"
    return f"+{clean[:2]}***{clean[-4:]}"


class SessionManager:
    """Manages multiple Telethon user account sessions.

    Responsibilities:
    - Load sessions from sessions/ directory
    - Store client instances
    - Select the active/default session
    - Retrieve clients by name
    - Validate session existence
    """

    def __init__(self, api_id: int, api_hash: str, sessions_dir: Path | str | None = None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.sessions_dir = Path(sessions_dir) if sessions_dir else SESSIONS_DIR
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        self._clients: dict[str, TelegramClient] = {}
        self._metadata: dict[str, dict] = {}
        self._active: str | None = None

        self._load_metadata()

    # ── Persistence ──────────────────────────────────────────────────

    def _metadata_path(self) -> Path:
        return self.sessions_dir / "metadata.json"

    def _active_path(self) -> Path:
        return self.sessions_dir / "active.txt"

    def _session_path(self, name: str) -> Path:
        return self.sessions_dir / f"{name}.session"

    def _load_metadata(self) -> None:
        path = self._metadata_path()
        if path.exists():
            try:
                self._metadata = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._metadata = {}
        else:
            self._metadata = {}

        active_path = self._active_path()
        if active_path.exists():
            try:
                self._active = active_path.read_text(encoding="utf-8").strip() or None
            except OSError:
                self._active = None

    def _save_metadata(self) -> None:
        try:
            self._metadata_path().write_text(
                json.dumps(self._metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("could not save metadata: %s", e)

    def _save_active(self) -> None:
        try:
            if self._active:
                self._active_path().write_text(self._active, encoding="utf-8")
            elif self._active_path().exists():
                self._active_path().unlink()
        except OSError as e:
            log.warning("could not save active session: %s", e)

    # ── Client lifecycle ─────────────────────────────────────────────

    def _create_client(self, name: str) -> TelegramClient:
        session_path = self._session_path(name)
        _enable_wal_journal(str(session_path))
        return TelegramClient(str(session_path), self.api_id, self.api_hash)

    def create_or_get_client(self, name: str) -> TelegramClient:
        """Get an existing client or create a new one.

        Unlike add_session(), this does not require the session to be new.
        It's used to create the module-level TelethonClient instance that
        is shared between bot.py and the session manager.
        """
        if name not in self._clients:
            client = self._create_client(name)
            self._clients[name] = client
            if name not in self._metadata:
                self._metadata[name] = {"phone": "unknown", "authorized": False}
            self._save_metadata()
        return self._clients[name]

    async def load_sessions(self) -> list[str]:
        """Load all .session files from sessions/ directory.

        On first run, migrates existing copybot_session.session from project root.
        Returns list of loaded session names.
        """
        loaded = []

        # Migrate existing session if sessions/ is empty
        existing_sessions = list(self.sessions_dir.glob("*.session"))
        if not existing_sessions:
            root_session = Path(__file__).resolve().parent / "copybot_session.session"
            if root_session.exists():
                self._migrate_root_session(root_session, "default")
                existing_sessions = list(self.sessions_dir.glob("*.session"))

        for session_file in existing_sessions:
            name = session_file.stem
            try:
                client = self._create_client(name)
                self._clients[name] = client
                loaded.append(name)

                if name not in self._metadata:
                    self._metadata[name] = {
                        "phone": "unknown",
                        "authorized": False,
                        "created": os.path.getmtime(session_file),
                    }
                log.info("loaded session: %s", name)
            except Exception as e:
                log.warning("failed to load session %s: %s", name, e)

        # Set default active if not set
        if not self._active and loaded:
            self._active = loaded[0]
            self._save_active()

        self._save_metadata()
        return loaded

    def _migrate_root_session(self, old_path: Path, name: str) -> None:
        """Migrate an existing session file from project root to sessions/."""
        new_path = self._session_path(name)
        if new_path.exists():
            return

        shutil.copy2(old_path, new_path)
        for suffix in ("-shm", "-wal"):
            src = Path(str(old_path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(new_path) + suffix))

        log.info("migrated session from %s to %s", old_path, new_path)
        self._metadata[name] = {
            "phone": "migrated",
            "authorized": True,
            "created": os.path.getmtime(old_path),
        }
        self._save_metadata()

    async def add_session(self, name: str) -> TelegramClient:
        """Create a new session client for login flow.

        Raises ValueError if session already exists or name is invalid.
        """
        if name in self._clients:
            raise ValueError(f"session '{name}' already exists")

        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid session name: {name}")

        client = self._create_client(name)
        self._clients[name] = client
        self._metadata[name] = {
            "phone": "pending",
            "authorized": False,
        }
        self._save_metadata()

        log.info("created session: %s", name)
        return client

    async def remove_session(self, name: str) -> bool:
        """Remove a session. Returns True if successful."""
        if name not in self._metadata and name not in self._clients:
            raise ValueError(f"session '{name}' not found")

        # Disconnect client if connected
        client = self._clients.pop(name, None)
        if client:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        # Remove session files
        session_path = self._session_path(name)
        for suffix in ("", "-shm", "-wal"):
            p = Path(str(session_path) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        # Remove metadata
        self._metadata.pop(name, None)
        self._save_metadata()

        # If this was the active session, switch to another
        if self._active == name:
            remaining = [n for n in self._metadata if n != name]
            self._active = remaining[0] if remaining else None
            self._save_active()

        log.info("removed session: %s", name)
        return True

    def get_client(self, name: str | None = None) -> TelegramClient:
        """Get a client by name. If name is None, return the active client.

        Raises ValueError if session not found.
        """
        if name is None:
            name = self._active

        if name is None:
            raise ValueError("no active session and no session name specified")

        client = self._clients.get(name)
        if client is None:
            raise ValueError(f"session '{name}' not loaded")

        return client

    def set_active(self, name: str) -> None:
        """Set the active/default session.

        Raises ValueError if session doesn't exist.
        """
        if name not in self._metadata and name not in self._clients:
            raise ValueError(f"session '{name}' not found")

        self._active = name
        self._save_active()
        log.info("active session set to: %s", name)

    def get_active_name(self) -> str | None:
        """Get the name of the active session."""
        return self._active

    def list_sessions(self) -> list[dict]:
        """List all sessions with metadata."""
        sessions = []
        for name in sorted(self._metadata.keys()):
            meta = self._metadata[name]
            sessions.append({
                "name": name,
                "phone": meta.get("phone", "unknown"),
                "authorized": meta.get("authorized", False),
                "active": name == self._active,
            })
        return sessions

    def update_session_info(self, name: str, phone: str | None = None, authorized: bool | None = None) -> None:
        """Update session metadata after login."""
        if name not in self._metadata:
            self._metadata[name] = {}

        if phone is not None:
            self._metadata[name]["phone"] = phone
        if authorized is not None:
            self._metadata[name]["authorized"] = authorized

        self._save_metadata()

    def validate_session(self, name: str) -> tuple[bool, str]:
        """Validate that a session exists and is usable.

        Returns (is_valid, error_message).
        """
        if name not in self._metadata:
            return False, f"session '{name}' not found"

        meta = self._metadata[name]
        if not meta.get("authorized", False):
            return False, f"session '{name}' is not authorized"

        return True, ""

    async def connect_all(self) -> dict[str, bool]:
        """Connect all loaded sessions and verify authorization.

        Returns dict of session name -> success.
        """
        results = {}
        for name, client in self._clients.items():
            try:
                if not client.is_connected():
                    await client.connect()

                if await client.is_user_authorized():
                    me = await client.get_me()
                    phone = getattr(me, "phone", None)
                    self.update_session_info(name, phone=phone, authorized=True)
                    results[name] = True
                    log.info("connected session %s: %s (id=%d)", name, me.first_name, me.id)
                else:
                    self.update_session_info(name, authorized=False)
                    results[name] = False
                    log.warning("session %s not authorized", name)
            except Exception as e:
                log.warning("failed to connect session %s: %s", name, e)
                self.update_session_info(name, authorized=False)
                results[name] = False

        return results

    def get_all_clients(self) -> dict[str, TelegramClient]:
        """Get all loaded clients."""
        return dict(self._clients)

    def has_session(self, name: str) -> bool:
        """Check if a session exists."""
        return name in self._metadata or name in self._clients

    def is_job_running_on(self, name: str) -> bool:
        """Check if any job is currently running on this session.

        This is a placeholder - actual job tracking is done in bot.py.
        """
        # This will be called from bot.py which tracks active jobs
        return False
