"""SQLite session with WAL mode and busy_timeout to prevent 'database is locked'."""

import sqlite3
from telethon.sessions.sqlite import SQLiteSession


class WALSession(SQLiteSession):
    """SQLiteSession that enables WAL journal mode and busy_timeout."""

    def __init__(self, session_id: str | None = None):
        super().__init__(session_id)
        # Telethon already opened self._conn in super().__init__()
        # Apply pragmas on that live connection
        try:
            c = self._conn.cursor()
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA synchronous=NORMAL;")
            c.execute("PRAGMA busy_timeout=10000;")
            self._conn.commit()
        except Exception:
            pass
