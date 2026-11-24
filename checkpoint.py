"""Durable job state: transactional SQLite store + atomic JSON helpers.

Why SQLite (and not giant JSON arrays)
--------------------------------------
The historical checkpoint was one ``*_meta.json`` written with a plain
``write_text`` (non-atomic) plus in-memory dicts that died with the process.
That allowed lost/corrupt checkpoints, duplicate sends after a crash, and no
way to answer "was message 1234 already sent?" without loading everything.

This module provides:

* :class:`JobStore` — WAL-mode SQLite database holding the job rows *and* a
  per-message ``processed`` table.  The cursor advance and the per-message
  outcome marker are committed in the **same transaction**, so a crash can
  never leave the cursor ahead of the markers (which would skip messages) or
  behind them (which would duplicate sends).
* :func:`atomic_write_json` — tmp + flush + fsync + ``os.replace`` for the
  small JSON files that remain (``config.json``, ``.progress_chat``).
* :func:`import_legacy_checkpoints` — one-time import of the old
  ``_pvtcopydl_tmp/*_meta.json`` files, leaving them untouched on disk.

Schema versioning: the ``meta`` table stores ``schema_version``; a mismatch
raises :class:`SchemaError` instead of silently mis-reading tables.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("copybot.checkpoint")

__all__ = [
    "SCHEMA_VERSION",
    "SchemaError",
    "JobState",
    "JobRecord",
    "JobStore",
    "atomic_write_json",
    "import_legacy_checkpoints",
]

SCHEMA_VERSION = 1

# Job lifecycle.  Valid transitions are enforced by JobStore.set_state.
_JOB_STATES = frozenset({
    "queued", "running", "downloading", "uploading",
    "paused", "cancelling", "completed", "failed",
})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued":      frozenset({"running", "cancelling", "failed", "paused"}),
    "running":     frozenset({"downloading", "uploading", "paused",
                              "cancelling", "completed", "failed", "running"}),
    "downloading": frozenset({"running", "uploading", "paused", "cancelling",
                              "completed", "failed"}),
    "uploading":   frozenset({"running", "downloading", "paused",
                              "cancelling", "completed", "failed"}),
    "paused":      frozenset({"queued", "running", "cancelling", "failed"}),
    "cancelling":  frozenset({"completed", "failed", "paused"}),
    "completed":   frozenset(),
    "failed":      frozenset({"queued", "running"}),   # manual retry
}

# processed.outcome values
OUT_SENDING = "sending"    # ambiguous: crash happened mid-send
OUT_SENT = "sent"
OUT_SKIPPED = "skipped"
OUT_FAILED = "failed"


class SchemaError(RuntimeError):
    """The checkpoint database has an incompatible schema."""


class JobState(str):
    """Small namespace for state name constants (typos become visible)."""
    QUEUED = "queued"
    RUNNING = "running"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class JobRecord:
    job_key: str
    src: str
    dst: str
    start_msg_id: int
    end_msg_id: int
    current_msg_id: int
    state: str = JobState.QUEUED
    total: int = 0
    downloaded: int = 0
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0
    retries: int = 0
    flood_waits: int = 0
    progress_chat_id: int | None = None
    progress_msg_id: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.end_msg_id - self.current_msg_id + 1)

    @property
    def done(self) -> bool:
        return self.current_msg_id > self.end_msg_id


# ---------------------------------------------------------------------------
# Atomic JSON (config / progress chat pointer)
# ---------------------------------------------------------------------------
def atomic_write_json(path: str | Path, obj: Any, *, indent: int = 2) -> None:
    """Write JSON atomically: tmp file → flush → fsync → rename.

    Readers therefore only ever see the previous complete file or the new
    complete file — never a truncated one (the old ``_save_config`` bug).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    data = json.dumps(obj, indent=indent, ensure_ascii=False)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # atomic on POSIX
        try:                           # best-effort directory fsync
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class JobStore:
    """Transactional job + processed-message database.

    One instance per process; all calls run on the event loop thread (SQLite
    ops are microseconds; WAL keeps readers non-blocking).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # -- schema ---------------------------------------------------------
    def _init_schema(self) -> None:
        cur = self._conn
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_key          TEXT PRIMARY KEY,
                src              TEXT NOT NULL,
                dst              TEXT NOT NULL,
                start_msg_id     INTEGER NOT NULL,
                end_msg_id       INTEGER NOT NULL,
                current_msg_id   INTEGER NOT NULL,
                state            TEXT NOT NULL,
                total            INTEGER NOT NULL DEFAULT 0,
                downloaded       INTEGER NOT NULL DEFAULT 0,
                uploaded         INTEGER NOT NULL DEFAULT 0,
                skipped          INTEGER NOT NULL DEFAULT 0,
                errors           INTEGER NOT NULL DEFAULT 0,
                retries          INTEGER NOT NULL DEFAULT 0,
                flood_waits      INTEGER NOT NULL DEFAULT 0,
                progress_chat_id INTEGER,
                progress_msg_id  INTEGER,
                created_at       REAL NOT NULL,
                updated_at       REAL NOT NULL,
                error            TEXT NOT NULL DEFAULT '',
                extra            TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

            CREATE TABLE IF NOT EXISTS processed (
                job_key    TEXT NOT NULL,
                msg_id     INTEGER NOT NULL,
                outcome    TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY (job_key, msg_id)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_processed_outcome
                ON processed(job_key, outcome, msg_id);
            """
        )
        row = cur.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            cur.commit()
        else:
            try:
                found = int(row["value"])
            except ValueError as exc:
                raise SchemaError(
                    f"corrupt schema_version {row['value']!r} in {self.path}"
                ) from exc
            if found != SCHEMA_VERSION:
                raise SchemaError(
                    f"checkpoint schema v{found} != supported v"
                    f"{SCHEMA_VERSION} ({self.path}); migrate or rollback")
        cur.commit()

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

    # -- jobs -----------------------------------------------------------
    def create_job(self, rec: JobRecord) -> JobRecord:
        now = time.time()
        rec.created_at = rec.created_at or now
        rec.updated_at = now
        if not rec.total:
            rec.total = max(0, rec.end_msg_id - rec.start_msg_id + 1)
        self._conn.execute(
            """INSERT INTO jobs(job_key, src, dst, start_msg_id, end_msg_id,
                                current_msg_id, state, total, downloaded,
                                uploaded, skipped, errors, retries, flood_waits,
                                progress_chat_id, progress_msg_id,
                                created_at, updated_at, error, extra)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.job_key, rec.src, rec.dst, rec.start_msg_id, rec.end_msg_id,
             rec.current_msg_id, rec.state, rec.total, rec.downloaded,
             rec.uploaded, rec.skipped, rec.errors, rec.retries,
             rec.flood_waits, rec.progress_chat_id, rec.progress_msg_id,
             rec.created_at, rec.updated_at, rec.error,
             json.dumps(rec.extra)))
        self._conn.commit()
        return rec

    def get_job(self, job_key: str) -> JobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        return self._to_record(row) if row else None

    def list_jobs(self, states: Iterable[str] | None = None) -> list[JobRecord]:
        if states:
            states = list(states)
            marks = ",".join("?" * len(states))
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE state IN ({marks}) "
                "ORDER BY created_at", states).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at").fetchall()
        return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> JobRecord:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        return JobRecord(
            job_key=row["job_key"], src=row["src"], dst=row["dst"],
            start_msg_id=row["start_msg_id"], end_msg_id=row["end_msg_id"],
            current_msg_id=row["current_msg_id"], state=row["state"],
            total=row["total"], downloaded=row["downloaded"],
            uploaded=row["uploaded"], skipped=row["skipped"],
            errors=row["errors"], retries=row["retries"],
            flood_waits=row["flood_waits"],
            progress_chat_id=row["progress_chat_id"],
            progress_msg_id=row["progress_msg_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            error=row["error"] or "", extra=extra)

    def update_job(self, job_key: str, *, commit: bool = True,
                   **fields: Any) -> None:
        """Update arbitrary job columns (validated against the schema)."""
        allowed = {
            "src", "dst", "start_msg_id", "end_msg_id", "current_msg_id",
            "state", "total", "downloaded", "uploaded", "skipped", "errors",
            "retries", "flood_waits", "progress_chat_id", "progress_msg_id",
            "error", "extra",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown job columns: {sorted(unknown)}")
        if "state" in fields and fields["state"] not in _JOB_STATES:
            raise ValueError(f"invalid job state {fields['state']!r}")
        if "extra" in fields and isinstance(fields["extra"], dict):
            fields["extra"] = json.dumps(fields["extra"])
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(
            f"UPDATE jobs SET {cols} WHERE job_key=?",
            (*fields.values(), job_key))
        if commit:
            self._conn.commit()

    def set_state(self, job_key: str, new_state: str,
                  *, error: str = "") -> None:
        if new_state not in _JOB_STATES:
            raise ValueError(f"invalid job state {new_state!r}")
        row = self._conn.execute(
            "SELECT state FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        if row is None:
            raise KeyError(f"unknown job {job_key}")
        current = row["state"]
        if new_state != current and new_state not in _TRANSITIONS.get(
                current, frozenset()):
            raise ValueError(
                f"illegal job transition {current!r} -> {new_state!r}")
        self._conn.execute(
            "UPDATE jobs SET state=?, error=?, updated_at=? WHERE job_key=?",
            (new_state, error, time.time(), job_key))
        self._conn.commit()

    def delete_job(self, job_key: str) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "DELETE FROM processed WHERE job_key=?", (job_key,))
            self._conn.execute(
                "DELETE FROM jobs WHERE job_key=?", (job_key,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- processed messages --------------------------------------------
    def is_processed(self, job_key: str, msg_id: int) -> bool:
        row = self._conn.execute(
            "SELECT outcome FROM processed WHERE job_key=? AND msg_id=?",
            (job_key, msg_id)).fetchone()
        return row is not None and row["outcome"] in (
            OUT_SENT, OUT_SKIPPED, OUT_FAILED)

    def outcome_of(self, job_key: str, msg_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT outcome FROM processed WHERE job_key=? AND msg_id=?",
            (job_key, msg_id)).fetchone()
        return row["outcome"] if row else None

    def processed_ids(self, job_key: str,
                      outcome: str | None = None) -> set[int]:
        if outcome:
            rows = self._conn.execute(
                "SELECT msg_id FROM processed WHERE job_key=? AND outcome=?",
                (job_key, outcome)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT msg_id FROM processed WHERE job_key=?",
                (job_key,)).fetchall()
        return {int(r["msg_id"]) for r in rows}

    def begin_message(self, job_key: str, msg_id: int) -> None:
        """Mark *msg_id* as in-flight **and** advance the cursor together.

        Both statements commit atomically, so the durable state is always
        one of:  (cursor <= msg  and no marker)  → not started,
                (cursor == msg+? and 'sending') → maybe sent (recoverable),
                (cursor > msg and 'sent'/'skipped'/'failed') → finished.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """INSERT INTO processed(job_key, msg_id, outcome, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(job_key, msg_id) DO NOTHING""",
                (job_key, msg_id, OUT_SENDING, time.time()))
            self._conn.execute(
                """UPDATE jobs SET current_msg_id=?, updated_at=?
                   WHERE job_key=? AND current_msg_id < ?""",
                (msg_id, time.time(), job_key, msg_id))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def finish_message(self, job_key: str, msg_id: int, outcome: str,
                       *, detail: str = "", **counters: Any) -> None:
        """Resolve an in-flight message and advance past it, atomically.

        ``counters`` may include ``downloaded``/``uploaded``/``skipped``/
        ``errors``/``retries``/``flood_waits`` as *deltas*.
        """
        if outcome not in (OUT_SENT, OUT_SKIPPED, OUT_FAILED):
            raise ValueError(f"invalid outcome {outcome!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """UPDATE processed SET outcome=?, detail=?, updated_at=?
                   WHERE job_key=? AND msg_id=?""",
                (outcome, detail[:500], time.time(), job_key, msg_id))
            now = time.time()
            self._conn.execute(
                """UPDATE jobs SET current_msg_id=?, updated_at=?
                   WHERE job_key=? AND current_msg_id <= ?""",
                (msg_id + 1, now, job_key, msg_id))
            if counters:
                sets, vals = [], []
                for k, v in counters.items():
                    if k not in ("downloaded", "uploaded", "skipped",
                                 "errors", "retries", "flood_waits"):
                        raise ValueError(f"unknown counter {k!r}")
                    sets.append(f"{k}={k}+?")
                    vals.append(int(v))
                self._conn.execute(
                    f"UPDATE jobs SET {', '.join(sets)} WHERE job_key=?",
                    (*vals, job_key))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def ambiguous_messages(self, job_key: str) -> list[int]:
        """Message ids stuck in ``sending`` (crash mid-send), ascending."""
        rows = self._conn.execute(
            """SELECT msg_id FROM processed
               WHERE job_key=? AND outcome=? ORDER BY msg_id""",
            (job_key, OUT_SENDING)).fetchall()
        return [int(r["msg_id"]) for r in rows]

    def resolve_ambiguous(self, job_key: str, msg_id: int,
                          outcome: str, *, detail: str = "") -> None:
        self.finish_message(job_key, msg_id, outcome, detail=detail)

    def unmark(self, job_key: str, msg_id: int) -> None:
        """Drop a marker entirely so the id becomes eligible again.

        Used when recovery proves a ``sending`` message never reached
        Telegram (no matching message in the destination) — the id must then
        be re-processed from scratch.
        """
        self._conn.execute(
            "DELETE FROM processed WHERE job_key=? AND msg_id=?",
            (job_key, msg_id))
        self._conn.commit()

    def counters(self, job_key: str) -> dict[str, int]:
        row = self._conn.execute(
            """SELECT downloaded, uploaded, skipped, errors, retries,
                      flood_waits FROM jobs WHERE job_key=?""",
            (job_key,)).fetchone()
        if row is None:
            return {}
        return {k: int(row[k]) for k in row.keys()}

    def pending_count(self, job_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM processed WHERE job_key=?",
            (job_key,)).fetchone()
        return int(row["n"])

    # -- resume helpers -------------------------------------------------
    def iter_range(self, job_key: str, start: int, end: int) -> Iterator[int]:
        """Yield message ids in ``[start, end]`` with **no** marker at all.

        Ids already resolved (``sent``/``skipped``/``failed``) are excluded,
        and so are ids stuck in ``sending`` — those are ambiguous and must be
        resolved by the resume recovery path first (see
        :meth:`ambiguous_messages`), never blindly re-sent or skipped.
        """
        rows = self._conn.execute(
            """SELECT msg_id FROM processed
               WHERE job_key=? AND msg_id BETWEEN ? AND ?""",
            (job_key, start, end)).fetchall()
        done = {int(r["msg_id"]) for r in rows}
        for mid in range(start, end + 1):
            if mid not in done:
                yield mid


# ---------------------------------------------------------------------------
# Legacy import
# ---------------------------------------------------------------------------
def import_legacy_checkpoints(store: JobStore,
                              staging_dir: str | Path) -> list[str]:
    """Import ``<key>_meta.json`` files written by the Telethon version.

    The legacy files are **left in place** (rollback requirement).  Each is
    turned into a job row plus ``processed`` markers for the message ids the
    old code had already recorded as uploaded/skipped, so a resume with the
    new engine neither re-sends them nor skips anything else.

    Returns the list of imported job keys (idempotent — existing jobs in the
    database are never overwritten).
    """
    staging = Path(staging_dir)
    if not staging.is_dir():
        return []
    imported: list[str] = []
    for path in sorted(staging.glob("*_meta.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            log.warning("skipping unreadable legacy checkpoint %s", path)
            continue
        try:
            key = str(data["job_key"])
            start = int(data["start_msg_id"])
            end = int(data["end_msg_id"])
            current = int(data.get("current_msg_id", start))
        except (KeyError, TypeError, ValueError):
            log.warning("skipping malformed legacy checkpoint %s", path)
            continue
        if store.get_job(key) is not None:
            continue  # already imported / created fresh

        # Legacy semantics: everything below current_msg_id was handled.
        # uploaded_msgs/skipped_msgs give the precise per-id outcomes.
        skipped = {int(x) for x in (data.get("skipped_msgs") or [])
                   if isinstance(x, (int, float, str))}
        # ids strictly below the cursor that were not explicitly listed were
        # processed by the old loop but not recorded → assume uploaded (the
        # legacy loop only advanced the cursor after a successful send).
        assumed = {m for m in range(start, current) if m not in skipped}

        rec = JobRecord(
            job_key=key,
            src=str(data.get("src", "")),
            dst=str(data.get("dst", "")),
            start_msg_id=start,
            end_msg_id=end,
            current_msg_id=current,
            state=("completed" if current > end else "paused"),
            total=end - start + 1,
            downloaded=int(data.get("downloaded") or 0),
            uploaded=int(data.get("uploaded") or 0),
            skipped=int(data.get("skipped") or 0),
            errors=int(data.get("errors") or 0),
            extra={"imported_from": path.name, "legacy": True},
        )
        try:
            store.create_job(rec)
            now = time.time()
            store._conn.execute("BEGIN IMMEDIATE")  # one transaction for markers
            for mid in assumed:
                store._conn.execute(
                    """INSERT OR IGNORE INTO processed
                       (job_key, msg_id, outcome, updated_at)
                       VALUES(?,?,?,?)""",
                    (key, mid, OUT_SENT, now))
            for mid in skipped - assumed:
                store._conn.execute(
                    """INSERT OR IGNORE INTO processed
                       (job_key, msg_id, outcome, updated_at)
                       VALUES(?,?,?,?)""",
                    (key, mid, OUT_SKIPPED, now))
            store._conn.commit()
        except sqlite3.IntegrityError:
            store._conn.rollback()
            continue
        imported.append(key)
        log.info("imported legacy checkpoint %s (cursor=%d/%d, state=%s)",
                 key, current, end, rec.state)
    return imported
