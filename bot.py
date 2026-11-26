"""
Telegram Private Channel Copy Bot  (TDLib edition)
===================================================
Copies messages from private channels to destination channels WITHOUT the
"Forwarded from" tag.  Every Telegram operation goes through **TDLib**
(``libtdjson``) via the ctypes bindings in :mod:`tdlib_native` — there is no
Telethon (or any other MTProto library) in this process.

Two accounts run side by side, each with its own TDLib database, directory
layout and single-process lock:

* **user account** — performs the copying (``.pvtcopy`` / ``.pvtcopydl``),
  logs in interactively (phone / code / 2FA) the first time;
* **bot account** (optional, ``BOT_TOKEN``) — posts the live progress
  dashboard (bar / ETA / speed + Refresh / Cancel buttons) in the chat where
  the owner sent ``/start``.

Commands (owner-only, sent from the user account)::

    .pvtcopy <start> <end>          server-side copy (no re-upload)
    .pvtcopydl <start> <end>        download → upload → verify pipeline
    .pvtcopydlcontinue <job_key>    resume an interrupted job
    .pvtcopydlcancel [job_key]      cancel a running job (alias: .cancel)
    .del <start> <end>              bulk-delete a message range
    .adest <channel_id>             add / select a destination channel
    .showdest                       list + pick destinations (inline)
    .logout                         log out of Telegram (graceful exit)
    .update                         git pull + graceful restart
    .help                           command reference

Bot side: ``/start`` (pair the progress chat), Refresh / Cancel / select
inline buttons.

Shutdown is signal-driven (SIGTERM / SIGINT): jobs are cancelled
cooperatively, TDLib clients are closed, locks are released — the process
never calls ``os._exit``.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_mod
import json
import logging
import os
import pathlib
import random
import re
import signal
import subprocess
import sys
import time
from typing import Any

from checkpoint import (
    JobState,
    JobStore,
    atomic_write_json,
    import_legacy_checkpoints,
)
from config import Config, ConfigError
from errors import TdError, classify, is_message_not_modified
from job_manager import (
    DuplicateJobError,
    JobManager,
    JobSpec,
    QueueFullError,
    UnknownJobError,
    job_key_for,
)
from pipeline import CopyPipeline, JobFailed, ProgressSnapshot
from progress_ui import Spinner
from progress_ui.renderer import (
    render_copy_completed,
    render_copy_progress,
    render_delete_completed,
    render_delete_progress,
    render_simple_copy_completed,
    render_simple_copy_progress,
)
from tdlib_auth import AuthError, AuthInteractiveRequired, Authenticator
from tdlib_client import TDClient
from tdlib_messages import (
    answer_callback_query,
    chat_title,
    delete_messages,
    edit_message,
    get_me,
    get_messages,
    parse_message_link,
    resolve_chat,
    send_text,
    sender_user_id,
)
from tdlib_native import NativeTdLib, TDLibNotFoundError
from tdlib_sessions import DatabaseLockedError, SessionManager

# ===========================================================================
# Logging  --  clean debug format (same look as the Telethon version)
# ===========================================================================
_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s:%(lineno)d │ %(message)s"
)
_ANSI = {
    "DEBUG": "\033[90m",
    "INFO": "\033[36m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class _TtyFormatter(logging.Formatter):
    """Colourised level, microsecond timestamps, module + line number."""

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

log = logging.getLogger("copybot")


def _set_log_level(level_name: str) -> None:
    """Convenience for ``LOG_LEVEL=DEBUG python bot.py`` overrides."""
    level_name = (level_name or "").upper()
    level = getattr(logging, level_name, None)
    if isinstance(level, int):
        logging.getLogger().setLevel(level)
        log.info("log level overridden to %s", level_name)


# ===========================================================================
# Paths (all relative to this file — nothing hardcoded to a server layout)
# ===========================================================================
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
PROGRESS_CHAT_FILE = PROJECT_ROOT / ".progress_chat"

# .del deletes in batches of this many ids per deleteMessages call
DEL_BATCH_SIZE = 100


# ===========================================================================
# config.json  (destinations + active selection) — always written atomically
# ===========================================================================
def _load_config() -> dict:
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
            else:
                log.warning("config.json is not an object — rebuilding")
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("config.json unreadable (%s) — rebuilding", exc)

    env_dest = os.getenv("DESTINATION", "").strip()
    if not cfg.get("destinations") and env_dest:
        cfg["destinations"] = [env_dest]
        cfg["selected"] = env_dest
        _save_config(cfg)
    cfg.setdefault("destinations", [])
    if not cfg.get("selected") and cfg["destinations"]:
        cfg["selected"] = cfg["destinations"][0]
    return cfg


def _save_config(cfg: dict) -> None:
    """Persist config.json atomically (tmp + fsync + rename)."""
    try:
        atomic_write_json(CONFIG_PATH, cfg)
    except OSError as exc:
        log.error("cannot write config.json: %s", exc)


def get_selected_destination() -> int | None:
    cfg = _load_config()
    sel = str(cfg.get("selected") or "").strip()
    if not sel:
        dests = cfg.get("destinations") or []
        sel = str(dests[0]).strip() if dests else ""
    if not sel:
        return None
    try:
        return int(sel)
    except ValueError:
        log.warning("selected destination %r is not numeric", sel)
        return None


# ===========================================================================
# Progress-chat pairing (bot /start → where live progress is posted)
# ===========================================================================
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
    tmp = PROGRESS_CHAT_FILE.with_name(PROGRESS_CHAT_FILE.name + ".tmp")
    try:
        tmp.write_text(str(chat_id), encoding="utf-8")
        os.replace(tmp, PROGRESS_CHAT_FILE)
    except OSError as exc:
        log.warning("could not persist progress chat id: %s", exc)


def get_progress_chat_id() -> int | None:
    global _progress_chat_loaded, _progress_chat_id
    if not _progress_chat_loaded:
        _progress_chat_id = _read_progress_chat_id()
        _progress_chat_loaded = True
    return _progress_chat_id


def _set_progress_chat_id(chat_id: int) -> None:
    global _progress_chat_loaded, _progress_chat_id
    _write_progress_chat_id(chat_id)
    _progress_chat_id = chat_id
    _progress_chat_loaded = True


# ===========================================================================
# TDLib helpers: HTML → entities, inline keyboards, payload decoding
# ===========================================================================
async def html_to_formatted(client: TDClient, html: str) -> tuple[str, list[dict]]:
    """Convert a Telegram-HTML string to ``(text, entities)``.

    Uses TDLib's own ``parseTextEntities`` (same parser the clients use), so
    the box-drawing dashboards render exactly as intended.  Falls back to a
    plain-text strip if the request fails — progress must never die.
    """
    try:
        res = await client.request({
            "@type": "parseTextEntities",
            "text": html,
            "parse_mode": {"@type": "textParseModeHTML"},
        })
        return str(res.get("text") or ""), list(res.get("entities") or [])
    except Exception as exc:  # noqa: BLE001 - progress must not kill a job
        log.debug("parseTextEntities failed (%s); using plain text", exc)
        plain = re.sub(r"</?[A-Za-z][^>]*>", "", html)
        return html_mod.unescape(plain), []


def cb_button(label: str, data: str, *,
              style: str = "buttonStyleDefault") -> dict:
    """One inline callback button (data is base64-encoded bytes)."""
    return {
        "@type": "inlineKeyboardButton",
        "text": label,
        "style": {"@type": style},
        "icon_custom_emoji_id": 0,
        "type": {
            "@type": "inlineKeyboardButtonTypeCallback",
            "data": base64.b64encode(data.encode("utf-8")).decode("ascii"),
        },
    }


def inline_keyboard(rows: list[list[dict]]) -> dict:
    """``replyMarkupInlineKeyboard`` — rows of rows of buttons."""
    return {"@type": "replyMarkupInlineKeyboard", "rows": rows}


def job_buttons(job_key: str) -> dict:
    return inline_keyboard([[
        cb_button("🔄 Refresh", f"refresh:{job_key}"),
        cb_button("⏹ Cancel", f"cancel:{job_key}"),
    ]])


def final_buttons(job_key: str) -> dict:
    return inline_keyboard([[cb_button(f"🆔 {job_key}", "noop")]])


def dest_buttons(cfg: dict) -> dict:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for ch_id in cfg.get("destinations") or []:
        prefix = "✅ " if str(ch_id) == str(cfg.get("selected")) else ""
        row.append(cb_button(f"{prefix}{ch_id}", f"select:{ch_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return inline_keyboard(rows)


def _text_of(message: dict) -> str:
    content = message.get("content") or {}
    if content.get("@type") != "messageText":
        return ""
    return str((content.get("text") or {}).get("text") or "")


def _callback_data(payload: dict) -> str:
    p = payload.get("payload") or {}
    if p.get("@type") != "callbackQueryPayloadData":
        return ""
    raw = p.get("data")
    if not isinstance(raw, str):
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:  # noqa: BLE001 - malformed button data is not fatal
        return ""


# ===========================================================================
# JobProgress — live dashboard for one job (edits are throttled by pipeline)
# ===========================================================================
class JobProgress:
    """Owns the two messages a job can have:

    * the **user status message** posted by the command handler in the chat
      where the command was issued (start + final render), and
    * the **live message** the pipeline edits: the bot's dashboard in the
      paired chat when a bot is configured, otherwise the user status
      message itself (legacy behaviour when no BOT_TOKEN exists).
    """

    def __init__(self, bot: "CopyBot", rec: Any, *, simple: bool = False,
                 channel: str = ""):
        self.bot = bot
        self.rec = rec
        self.key = rec.job_key
        self.simple = simple
        self.channel = channel
        self.spinner = Spinner()
        self.started = time.monotonic()
        self.live_client: TDClient | None = None
        self.live_chat: int | None = None
        self.live_msg: int | None = None
        self.user_chat: int | None = rec.progress_chat_id
        self.user_msg: int | None = rec.progress_msg_id

    # -- rendering ------------------------------------------------------
    def render(self, snap: ProgressSnapshot) -> str:
        if self.simple:
            return render_simple_copy_progress(
                spinner=self.spinner,
                channel=self.channel or str(self.rec.src),
                start_id=self.rec.start_msg_id,
                end_id=self.rec.end_msg_id,
                copied=snap.uploaded,
                skipped=snap.skipped,
                errors=snap.errors,
                current_id=snap.current_msg or self.rec.start_msg_id,
                elapsed=snap.elapsed,
            )
        return render_copy_progress(
            spinner=self.spinner,
            mode=snap.mode,
            job_key=self.key,
            current_msg=snap.current_msg,
            total=snap.total or self.rec.total,
            downloaded=snap.downloaded,
            uploaded=snap.uploaded,
            skipped=snap.skipped,
            errors=snap.errors,
            speed=snap.speed,
            eta=snap.eta,
            elapsed=snap.elapsed,
            filename=snap.filename,
            file_current=snap.file_current,
            file_total=snap.file_total,
            start_msg=snap.start_msg or self.rec.start_msg_id,
        )

    def snapshot(self) -> ProgressSnapshot:
        """A snapshot built from the *persisted* record (Refresh button)."""
        rec = self.bot.store.get_job(self.key) or self.rec
        return ProgressSnapshot(
            job_key=self.key,
            mode="copy",
            current_msg=rec.current_msg_id,
            total=rec.total,
            downloaded=rec.downloaded,
            uploaded=rec.uploaded,
            skipped=rec.skipped,
            errors=rec.errors,
            elapsed=time.monotonic() - self.started,
            start_msg=rec.start_msg_id,
        )

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        paired = get_progress_chat_id()
        if self.bot.bot is not None and paired:
            try:
                msg = await send_text(
                    self.bot.bot, paired, self.render(self.snapshot()),
                    reply_markup=job_buttons(self.key))
                self.live_client = self.bot.bot
                self.live_chat = paired
                self.live_msg = int(msg.get("id") or 0) or None
                log.info("live progress → bot chat=%s msg=%s",
                         paired, self.live_msg)
            except Exception as exc:  # noqa: BLE001 - fall back to user msg
                log.warning("cannot post bot progress message: %s", exc)
        if self.live_msg is None and self.user_chat and self.user_msg:
            self.live_client = self.bot.user
            self.live_chat = self.user_chat
            self.live_msg = self.user_msg
        if self.live_msg is None:
            log.warning("job %s has no progress message target", self.key)
        self.bot._progress[self.key] = self

    async def on_snapshot(self, snap: ProgressSnapshot) -> None:
        """ProgressSink for CopyPipeline (already throttled upstream)."""
        await self._edit(self.render(snap), job_buttons(self.key))

    async def force_refresh(self) -> None:
        await self._edit(self.render(self.snapshot()), job_buttons(self.key))

    async def _edit(self, html: str, markup: dict | None) -> None:
        if not (self.live_client and self.live_chat and self.live_msg):
            return
        try:
            text, entities = await html_to_formatted(self.bot.user, html)
            await edit_message(self.live_client, self.live_chat, self.live_msg,
                               text, entities=entities, reply_markup=markup)
        except TdError as exc:
            if is_message_not_modified(exc):
                return
            log.debug("progress edit failed: [%s] %s", exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            log.debug("progress edit failed: %s", exc)

    async def _final(self, html: str) -> None:
        """Final render on the live message and the user status message."""
        markup = final_buttons(self.key)
        await self._edit(html, markup)
        if (self.user_chat and self.user_msg
                and (self.user_chat, self.user_msg)
                != (self.live_chat, self.live_msg)):
            try:
                text, entities = await html_to_formatted(self.bot.user, html)
                await edit_message(self.bot.user, self.user_chat, self.user_msg,
                                   text, entities=entities,
                                   reply_markup=markup)
            except Exception as exc:  # noqa: BLE001
                log.debug("final status edit failed: %s", exc)

    async def finish_completed(self) -> None:
        rec = self.bot.store.get_job(self.key) or self.rec
        elapsed = time.monotonic() - self.started
        if self.simple:
            html = render_simple_copy_completed(
                channel=self.channel or str(rec.src),
                start_id=rec.start_msg_id,
                end_id=rec.end_msg_id,
                copied=rec.uploaded,
                skipped=rec.skipped,
                errors=rec.errors,
                elapsed=elapsed,
            )
        else:
            html = render_copy_completed(
                mode="copy",
                job_key=self.key,
                total=rec.total,
                downloaded=rec.downloaded,
                uploaded=rec.uploaded,
                skipped=rec.skipped,
                errors=rec.errors,
                elapsed=elapsed,
            )
        await self._final(html)

    async def finish_problem(self, headline: str) -> None:
        rec = self.bot.store.get_job(self.key) or self.rec
        body = [
            headline,
            "",
            f"job `{self.key}`",
            f"cursor  {rec.current_msg_id} / {rec.end_msg_id}",
            f"sent {rec.uploaded} · skip {rec.skipped} · err {rec.errors}",
            "",
            f"resume with: .pvtcopydlcontinue {self.key}",
        ]
        await self._final(f"<pre>{html_mod.escape(chr(10).join(body))}</pre>")

    async def finish_cancelled(self) -> None:
        rec = self.bot.store.get_job(self.key) or self.rec
        body = [
            "⏹ job cancelled",
            "",
            f"job `{self.key}` stopped at msg {rec.current_msg_id}",
            f"sent {rec.uploaded} · skip {rec.skipped} · err {rec.errors}",
            "",
            f"resume with: .pvtcopydlcontinue {self.key}",
        ]
        await self._final(f"<pre>{html_mod.escape(chr(10).join(body))}</pre>")


# ===========================================================================
# CopyBot — wiring, command handlers, callbacks, lifecycle
# ===========================================================================
class CopyBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store: JobStore | None = None
        self.jobs: JobManager | None = None
        self.user: TDClient | None = None
        self.bot: TDClient | None = None
        self._sessions: list[SessionManager] = []
        self._progress: dict[str, JobProgress] = {}
        self.me_id = 0
        self.shutdown_event = asyncio.Event()
        self._stopping = False
        self._restart = False
        self._shutdown_reason = "signal"
        # shared across all jobs: hard bounds on parallel transfers
        self.dl_sem = asyncio.Semaphore(cfg.max_concurrent_downloads)
        self.ul_sem = asyncio.Semaphore(cfg.max_concurrent_uploads)

    # ------------------------------------------------------------------
    # startup
    # ------------------------------------------------------------------
    async def run(self) -> int:
        rc = await self.start()
        if rc != 0:
            return rc
        # run until a signal (or .logout / .update) requests shutdown
        await self.shutdown_event.wait()
        reason = self._shutdown_reason
        rc = await self._shutdown(reason)
        if self._restart:
            log.info("re-executing %s", " ".join(sys.argv))
            os.execv(sys.executable, [sys.executable, *sys.argv])
        return rc

    async def start(self) -> int:
        """Bring up clients, routing and job scheduling; return 0 when
        ready (non-zero = startup failed).  Does **not** wait for
        shutdown — :meth:`run` and headless drivers do that."""
        cfg = self.cfg
        cfg.ensure_dirs()
        loop = asyncio.get_running_loop()
        self._install_signals(loop)

        # ── single-process guards (TDLib databases are not multi-process safe)
        user_sess = SessionManager("user", cfg.user_db_dir, cfg.user_files_dir)
        user_sess.acquire()
        self._sessions.append(user_sess)
        bot_sess: SessionManager | None = None
        if cfg.bot_token:
            candidate = SessionManager("bot", cfg.bot_db_dir, cfg.bot_files_dir)
            try:
                candidate.acquire()
                self._sessions.append(candidate)
                bot_sess = candidate
            except DatabaseLockedError as exc:
                log.warning("bot session busy (%s) — bot disabled", exc)

        # ── native library + checkpoint store ──────────────────────────
        native = NativeTdLib.shared(
            library_path=cfg.tdlib_library_path,
            log_verbosity=cfg.tdlib_log_verbosity,
            log_file=cfg.native_log_file,
        )
        self.store = JobStore(cfg.jobs_db_path)
        imported = import_legacy_checkpoints(self.store, cfg.staging_dir)
        if imported:
            log.info("imported %d legacy checkpoint(s): %s",
                     len(imported), ", ".join(imported))
        self.jobs = JobManager(
            self.store, self._run_job,
            max_queued=cfg.max_queued_jobs,
            max_running=cfg.max_running_jobs,
            cancel_grace=10.0,
        )

        # ── user account (interactive login the first time) ────────────
        self.user = TDClient("user", native,
                             request_timeout=cfg.request_timeout,
                             send_timeout=cfg.send_timeout)
        self.user.start()
        auth = Authenticator(
            self.user, user_sess,
            api_id=cfg.api_id, api_hash=cfg.api_hash,
            encryption_key=cfg.tdlib_db_key,
            first_name=cfg.first_name, last_name=cfg.last_name,
        )
        try:
            await auth.run()
        except AuthInteractiveRequired:
            log.error(
                "interactive login required — run `python bot.py` once in a "
                "terminal (phone / code / 2FA), then start the service again")
            await self.user.close()
            return 1
        except AuthError as exc:
            log.error("user login failed: %s", exc)
            await self.user.close()
            return 1

        me = await get_me(self.user)
        self.me_id = int(me.get("id") or 0)
        log.info("userbot OK: %s (id=%d) active-dst=%s",
                 me.get("first_name") or me.get("username") or "?",
                 self.me_id, get_selected_destination())

        # ── bot account (optional, token login) ────────────────────────
        if bot_sess is not None and cfg.bot_token:
            try:
                self.bot = TDClient("bot", native,
                                    request_timeout=cfg.request_timeout,
                                    send_timeout=cfg.send_timeout)
                self.bot.start()
                bot_auth = Authenticator(
                    self.bot, bot_sess,
                    api_id=cfg.api_id, api_hash=cfg.api_hash,
                    bot_token=cfg.bot_token,
                    encryption_key=cfg.tdlib_db_key,
                    state_timeout=120.0,
                )
                await bot_auth.run()
                bme = await get_me(self.bot)
                log.info("bot OK: @%s (id=%d)",
                         bme.get("username") or "?", bme.get("id"))
            except Exception as exc:  # noqa: BLE001 - bot is optional
                log.error("bot login failed (%s) — live progress will be "
                          "posted in the command chat instead", exc)
                if self.bot is not None:
                    try:
                        await self.bot.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.bot = None
                try:
                    bot_sess.release()
                except Exception:  # noqa: BLE001
                    pass
                self._sessions = [s for s in self._sessions if s is not bot_sess]
        elif not cfg.bot_token:
            log.warning("no BOT_TOKEN — progress edits happen in the "
                        "command chat (send the bot /start to move them "
                        "to a dedicated chat)")

        # ── update routing (all callbacks are tracked by the router) ───
        assert self.user is not None
        self.user.router.subscribe(
            self._on_user_update,
            ("updateNewMessage", "updateNewCallbackQuery"))
        if self.bot is not None:
            self.bot.router.subscribe(
                self._on_bot_update,
                ("updateNewMessage", "updateNewCallbackQuery"))

        # ── recover interrupted work ───────────────────────────────────
        stale = self.jobs.recover_stale()
        if stale:
            log.warning("recovered %d interrupted job(s) → paused: %s",
                        len(stale), ", ".join(stale))
        self._pump_queue()
        log.info("copybot ready — %d queued job(s), max %d running, "
                 "dl=%d ul=%d",
                 len(self.store.list_jobs(states=[JobState.QUEUED])),
                 self.jobs.max_running,
                 cfg.max_concurrent_downloads, cfg.max_concurrent_uploads)
        return 0

    def _install_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                signal.signal(sig, lambda *_a, _s=sig: self._on_signal(_s))

    def _on_signal(self, sig: Any) -> None:
        name = getattr(sig, "name", None) or str(sig)
        if self._stopping:
            return
        log.info("received %s — graceful shutdown", name)
        self._shutdown_reason = name
        self.shutdown_event.set()

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------
    async def _shutdown(self, reason: str) -> int:
        self._stopping = True
        log.info("shutting down (%s)", reason)
        if self.jobs is not None:
            try:
                await self.jobs.shutdown(timeout=self.cfg.shutdown_timeout)
            except Exception:  # noqa: BLE001
                log.exception("job shutdown failed")
        for cli in (self.user, self.bot):
            if cli is None:
                continue
            try:
                await cli.router.shutdown(timeout=5.0)
            except Exception:  # noqa: BLE001
                log.debug("router shutdown failed", exc_info=True)
            try:
                await cli.close(timeout=10.0)
            except Exception:  # noqa: BLE001
                log.debug("client close failed", exc_info=True)
        for sess in self._sessions:
            try:
                sess.release()
            except Exception:  # noqa: BLE001
                pass
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("shutdown complete")
        return 0

    def request_shutdown(self, reason: str) -> None:
        if self._stopping:
            return
        self._shutdown_reason = reason
        self.shutdown_event.set()

    # ------------------------------------------------------------------
    # job scheduling (bounded: max_running jobs, rest stay queued)
    # ------------------------------------------------------------------
    def _pump_queue(self) -> None:
        """Start queued jobs while capacity allows (sync, loop thread)."""
        if self._stopping or self.jobs is None or self.store is None:
            return
        try:
            for rec in self.store.list_jobs(states=[JobState.QUEUED]):
                if len(self.jobs.running_keys) >= self.jobs.max_running:
                    break
                if rec.job_key in self.jobs.running_keys:
                    continue
                if not self._start_job(rec.job_key):
                    break
        except Exception:  # noqa: BLE001 - never let bookkeeping kill the loop
            log.exception("queue pump failed")

    def _start_job(self, job_key: str) -> bool:
        if self.jobs is None:
            return False
        try:
            task = self.jobs.spawn(job_key)
        except QueueFullError:
            log.info("job %s queued (all %d slots busy)",
                     job_key, self.jobs.max_running)
            return False
        except (DuplicateJobError, UnknownJobError, RuntimeError) as exc:
            log.warning("cannot start job %s: %s", job_key, exc)
            return False
        # the manager's own done-callback runs first (it pops _running),
        # so by the time ours runs the capacity count is accurate
        task.add_done_callback(self._on_job_done)
        return True

    def _on_job_done(self, _task: asyncio.Task) -> None:
        if not self._stopping:
            self._pump_queue()

    # ------------------------------------------------------------------
    # the runner: one job → one CopyPipeline execution
    # ------------------------------------------------------------------
    async def _run_job(self, rec: Any, control: Any) -> None:
        assert self.store is not None and self.user is not None
        mode = str((rec.extra or {}).get("mode") or "")
        simple = mode == "server_only"
        channel = ""
        if simple:
            try:
                src_chat = await resolve_chat(self.user, rec.src)
                channel = await chat_title(self.user, src_chat)
            except Exception as exc:  # noqa: BLE001 - cosmetic only
                log.debug("cannot resolve source title: %s", exc)
                channel = str(rec.src)

        hub = JobProgress(self, rec, simple=simple, channel=channel)
        await hub.start()
        pipeline = CopyPipeline(
            self.cfg, self.store, self.user, self.bot,
            dl_sem=self.dl_sem, ul_sem=self.ul_sem,
            progress=hub.on_snapshot,
            staging_dir=self.cfg.staging_dir,
            server_only=simple,
        )
        try:
            await pipeline.run(rec, control)
        except asyncio.CancelledError:
            log.info("job %s cancelled", rec.job_key)
            await self._safe(hub.finish_cancelled())
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as job failure
            log.exception("job %s failed: %s", rec.job_key, exc)
            await self._safe(hub.finish_problem(f"❌ job failed: {exc}"))
            raise
        else:
            log.info("job %s completed", rec.job_key)
            await self._safe(hub.finish_completed())
        finally:
            self._progress.pop(rec.job_key, None)
            # keep the progress pointers so a later resume edits the same
            # messages (they may have moved from user-msg → bot-msg)
            try:
                self.store.update_job(
                    rec.job_key,
                    progress_chat_id=hub.live_chat or rec.progress_chat_id,
                    progress_msg_id=hub.live_msg or rec.progress_msg_id)
            except Exception:  # noqa: BLE001
                log.debug("could not persist progress pointers", exc_info=True)

    @staticmethod
    async def _safe(awaitable: Any) -> None:
        try:
            await awaitable
        except Exception:  # noqa: BLE001 - progress must not mask outcomes
            log.debug("progress finalisation failed", exc_info=True)

    # ------------------------------------------------------------------
    # update dispatch
    # ------------------------------------------------------------------
    async def _on_user_update(self, payload: dict) -> None:
        utype = payload.get("@type")
        if utype == "updateNewMessage":
            await self._handle_user_message(payload)
        elif utype == "updateNewCallbackQuery":
            assert self.user is not None
            await self._handle_callback(self.user, payload)

    async def _on_bot_update(self, payload: dict) -> None:
        utype = payload.get("@type")
        if utype == "updateNewMessage":
            await self._handle_bot_message(payload)
        elif utype == "updateNewCallbackQuery":
            assert self.bot is not None
            await self._handle_callback(self.bot, payload)

    async def _handle_user_message(self, payload: dict) -> None:
        if self._stopping or not self.me_id:
            return
        message = payload.get("message") or {}
        if sender_user_id(message) != self.me_id:
            return                      # owner-only commands
        text = _text_of(message).strip()
        if not text.startswith("."):
            return
        chat_id = int(message.get("chat_id") or 0)
        await self._dispatch_command(chat_id, text)

    async def _handle_bot_message(self, payload: dict) -> None:
        message = payload.get("message") or {}
        text = _text_of(message).strip()
        if not re.match(r"^/start(?:\s|$)", text):
            return
        chat_id = int(message.get("chat_id") or 0)
        sender = sender_user_id(message)
        if self.me_id and sender and sender != self.me_id:
            try:
                await self._say(chat_id,
                                "this bot is paired with its owner — "
                                "send /start from the owner's account.")
            except Exception:  # noqa: BLE001
                pass
            return
        _set_progress_chat_id(chat_id)
        first = "there"
        if self.bot is not None:
            try:
                chat = await self.bot.request({"@type": "getChat",
                                               "chat_id": chat_id})
                first = str(chat.get("first_name") or chat.get("title")
                            or "there")
            except Exception:  # noqa: BLE001
                pass
        log.info("bot /start chat_id=%d user=%s", chat_id, first)
        await self._say(
            chat_id,
            f"hey 👋\n\nyou're now paired as the live-progress channel for "
            f"the copybot.\n\nevery .pvtcopydl / .pvtcopy job the userbot "
            f"runs will post a progress message here with: bar / ETA / "
            f"download speed / refresh and cancel buttons.\n\ntap refresh "
            f"any time to force a re-render.",
            client=self.bot,
        )

    async def _dispatch_command(self, chat_id: int, text: str) -> None:
        m = re.match(r"^\.([A-Za-z]+)", text)
        if not m:
            return
        word = m.group(1)
        handler = getattr(self, f"_cmd_{word}", None)
        if handler is None:
            return                      # unknown dot-command: ignore silently
        args = text[m.end():].split()
        try:
            await handler(chat_id=chat_id, args=args, text=text)
        except TdError as exc:
            log.warning(".%s failed: [%s] %s", word, exc.code, exc.message)
            await self._safe(self._say(
                chat_id, f"command failed: [{exc.code}] {exc.message}"))
        except Exception as exc:  # noqa: BLE001
            log.exception(".%s crashed", word)
            await self._safe(self._say(chat_id, f"command failed: {exc}"))

    # ------------------------------------------------------------------
    # messaging helpers
    # ------------------------------------------------------------------
    async def _say(self, chat_id: int, text: str, *,
                   reply_markup: dict | None = None,
                   client: TDClient | None = None) -> dict | None:
        cli = client or self.user
        if cli is None:
            return None
        try:
            return await send_text(cli, chat_id, text,
                                   reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot message chat %s: %s", chat_id, exc)
            return None

    async def _say_html(self, chat_id: int, html: str, *,
                        reply_markup: dict | None = None) -> dict | None:
        if self.user is None:
            return None
        try:
            text, entities = await html_to_formatted(self.user, html)
            return await send_text(self.user, chat_id, text,
                                   entities=entities,
                                   reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot post dashboard to chat %s: %s", chat_id, exc)
            return None

    async def _answer(self, client: TDClient, query_id: int, text: str, *,
                      alert: bool = False) -> None:
        if not query_id:
            return
        try:
            await answer_callback_query(client, query_id, text,
                                        show_alert=alert)
        except Exception as exc:  # noqa: BLE001
            log.debug("answerCallbackQuery failed: %s", exc)

    # ------------------------------------------------------------------
    # commands — copy / download-copy
    # ------------------------------------------------------------------
    async def _cmd_pvtcopy(self, *, chat_id: int, args: list[str],
                           text: str) -> None:
        """Server-side copy only (no download/re-upload fallback)."""
        await self._start_range(chat_id, args, simple=True)

    async def _cmd_pvtcopydl(self, *, chat_id: int, args: list[str],
                             text: str) -> None:
        """Full pipeline: server copy first, per-message fallback after."""
        await self._start_range(chat_id, args, simple=False)

    async def _start_range(self, chat_id: int, args: list[str], *,
                           simple: bool) -> None:
        usage = (".pvtcopy <start_link> <end_link>" if simple
                 else ".pvtcopydl <start_link> <end_link>")
        if len(args) < 2:
            await self._say(chat_id, f"usage: {usage}")
            return
        start = parse_message_link(args[0])
        end = parse_message_link(args[1])
        if start is None:
            await self._say(chat_id, "invalid start link.")
            return
        if end is None:
            await self._say(chat_id, "invalid end link.")
            return
        if start[0] != end[0]:
            await self._say(
                chat_id,
                "start and end links must belong to the same source channel.")
            return
        src = start[0]
        a, b = start[1], end[1]
        if a > b:
            a, b = b, a

        dst = get_selected_destination()
        if dst is None:
            await self._say(chat_id,
                            "no destination configured — "
                            "use .adest <channel_id> first.")
            return
        if str(src) == str(dst):
            await self._say(chat_id, "source and destination must differ.")
            return

        assert self.user is not None
        try:
            src_chat = await resolve_chat(self.user, src)
            await resolve_chat(self.user, dst)
        except TdError as exc:
            await self._say(
                chat_id,
                f"cannot access channel: [{exc.code}] {exc.message}")
            return

        title = await chat_title(self.user, src_chat)
        key = job_key_for(str(src), a, b)
        spinner = Spinner()
        total = b - a + 1
        if simple:
            initial = render_simple_copy_progress(
                spinner=spinner, channel=title, start_id=a, end_id=b,
                copied=0, skipped=0, errors=0, current_id=a, elapsed=0.0)
        else:
            initial = render_copy_progress(
                spinner=spinner, mode="downloading", job_key=key,
                current_msg=a, total=total, downloaded=0, uploaded=0,
                skipped=0, errors=0, speed=0.0, eta=0.0, elapsed=0.0,
                filename="", start_msg=a)
        status = await self._say_html(chat_id, initial,
                                      reply_markup=job_buttons(key))
        if status is None:
            await self._say(chat_id, "could not post the status message.")
            return

        spec = JobSpec(
            src=str(src), dst=str(dst),
            start_msg_id=a, end_msg_id=b,
            progress_chat_id=chat_id,
            progress_msg_id=int(status.get("id") or 0),
            extra={"mode": "server_only"} if simple else {},
        )
        try:
            rec = self.jobs.create(spec)
        except DuplicateJobError as exc:
            await self._say(chat_id, str(exc))
            return
        except QueueFullError as exc:
            await self._say(chat_id, str(exc))
            return

        started = self._start_job(rec.job_key)
        log.info("%s START key=%s src=%s dst=%s range=%d..%d",
                 "pvtcopy" if simple else "pvtcopydl",
                 rec.job_key, src, dst, a, b)
        ack = (f"job `{rec.job_key}` — copying {total} message(s)\n"
               f"src: {src} → dst: {dst}\n"
               f"resume: .pvtcopydlcontinue {rec.job_key}")
        if not started:
            ack += "\n\n⏳ queued — starts automatically when a slot frees."
        await self._say(chat_id, ack)

    # ------------------------------------------------------------------
    # commands — resume / cancel
    # ------------------------------------------------------------------
    async def _cmd_pvtcopydlcontinue(self, *, chat_id: int, args: list[str],
                                     text: str) -> None:
        if not args:
            await self._say(chat_id,
                            "usage: .pvtcopydlcontinue <job_key>")
            return
        key = args[0]
        assert self.store is not None and self.jobs is not None
        rec = self.store.get_job(key)
        if rec is None:
            await self._say(chat_id, f"no saved job found for key `{key}`.")
            return
        if rec.state == JobState.COMPLETED:
            await self._say(
                chat_id,
                f"job `{key}` was already complete (sent={rec.uploaded}).")
            return
        if key in self.jobs.running_keys:
            await self._say(chat_id, f"job `{key}` is already running.")
            return

        remaining = max(0, rec.end_msg_id - rec.current_msg_id + 1)
        spinner = Spinner()
        initial = render_copy_progress(
            spinner=spinner, mode="downloading", job_key=key,
            current_msg=rec.current_msg_id, total=rec.total,
            downloaded=rec.downloaded, uploaded=rec.uploaded,
            skipped=rec.skipped, errors=rec.errors,
            speed=0.0, eta=0.0, elapsed=0.0, filename="",
            start_msg=rec.start_msg_id)
        status = await self._say_html(chat_id, initial,
                                      reply_markup=job_buttons(key))
        if status is not None:
            try:
                self.store.update_job(
                    key, progress_chat_id=chat_id,
                    progress_msg_id=int(status.get("id") or 0))
            except Exception:  # noqa: BLE001
                log.debug("could not update progress pointers", exc_info=True)

        await self._say(
            chat_id,
            f"resuming job `{key}` from msg {rec.current_msg_id} "
            f"({remaining} remaining).")
        try:
            task = self.jobs.resume(key)
        except DuplicateJobError as exc:
            await self._say(chat_id, str(exc))
            return
        except UnknownJobError:
            await self._say(chat_id, f"no saved job found for key `{key}`.")
            return
        except RuntimeError as exc:
            await self._say(chat_id, f"cannot resume: {exc}")
            return
        task.add_done_callback(self._on_job_done)
        log.info("pvtcopydl CONTINUE key=%s from msg=%d",
                 key, rec.current_msg_id)

    async def _cmd_pvtcopydlcancel(self, *, chat_id: int, args: list[str],
                                   text: str) -> None:
        await self._cancel_cmd(chat_id, args, prefix=".pvtcopydlcancel")

    async def _cmd_cancel(self, *, chat_id: int, args: list[str],
                          text: str) -> None:
        await self._cancel_cmd(chat_id, args, prefix=".cancel")

    async def _cancel_cmd(self, chat_id: int, args: list[str], *,
                          prefix: str) -> None:
        assert self.jobs is not None and self.store is not None
        key = args[0] if args else None
        if key is None:
            running = list(self.jobs.running_keys)
            queued = [j.job_key
                      for j in self.store.list_jobs(states=[JobState.QUEUED])]
            active = running + [k for k in queued if k not in running]
            if not active:
                await self._say(chat_id, "no active jobs.")
                return
            listing = "\n".join(f"• {k}" for k in active)
            await self._say(
                chat_id,
                f"active jobs (cancel with `{prefix} <key>`):\n{listing}")
            return
        if self.jobs.request_cancel(key):
            await self._say(chat_id, f"cancelling job `{key}`.")
        else:
            await self._say(
                chat_id,
                f"no running job with key `{key}` "
                f"(already done or never started).")

    # ------------------------------------------------------------------
    # command — bulk delete
    # ------------------------------------------------------------------
    async def _cmd_del(self, *, chat_id: int, args: list[str],
                       text: str) -> None:
        """Bulk-delete messages in an inclusive range (same chat for both
        links).  Batches of 100 ids, per-id retry when a batch fails."""
        if len(args) < 2:
            await self._say(chat_id,
                            "usage: .del <start_link> <end_link>")
            return
        start = parse_message_link(args[0])
        end = parse_message_link(args[1])
        if start is None:
            await self._say(chat_id, "invalid start link.")
            return
        if end is None:
            await self._say(chat_id, "invalid end link.")
            return
        if start[0] != end[0]:
            await self._say(chat_id,
                            "both links must belong to the same chat.")
            return
        a, b = start[1], end[1]
        if a > b:
            a, b = b, a

        assert self.user is not None
        try:
            chat = await resolve_chat(self.user, start[0])
        except TdError as exc:
            await self._say(chat_id,
                            f"cannot access chat: [{exc.code}] {exc.message}")
            return
        title = await chat_title(self.user, chat)

        spinner = Spinner()
        t0 = time.monotonic()
        ids = list(range(a, b + 1))
        status = await self._say_html(
            chat_id,
            render_delete_progress(
                spinner=spinner, channel=title, start_id=a, end_id=b,
                deleted=0, skipped=0, failed=0, current_id=a, elapsed=0.0))
        deleted = failed = 0
        last_edit = t0
        log.info("del START chat=%s range=%d..%d (%d ids)",
                 chat, a, b, len(ids))

        for i in range(0, len(ids), DEL_BATCH_SIZE):
            batch = ids[i:i + DEL_BATCH_SIZE]
            try:
                await delete_messages(self.user, chat, batch)
                deleted += len(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - salvage per id
                info = classify(exc)
                log.warning("delete batch %d..%d failed (%s); retrying "
                            "per id", batch[0], batch[-1], info)
                for mid in batch:
                    try:
                        await delete_messages(self.user, chat, [mid])
                        deleted += 1
                    except Exception:  # noqa: BLE001
                        failed += 1
            now = time.monotonic()
            if (status is not None
                    and now - last_edit >= self.cfg.progress_edit_interval):
                last_edit = now
                await self._edit_status(
                    chat_id, int(status.get("id") or 0),
                    render_delete_progress(
                        spinner=spinner, channel=title, start_id=a, end_id=b,
                        deleted=deleted, skipped=0, failed=failed,
                        current_id=batch[-1], elapsed=now - t0))

        elapsed = time.monotonic() - t0
        final = render_delete_completed(
            channel=title, start_id=a, end_id=b, deleted=deleted,
            skipped=0, failed=failed, elapsed=elapsed)
        if status is not None:
            await self._edit_status(chat_id, int(status.get("id") or 0), final)
        else:
            await self._say_html(chat_id, final)
        log.info("del DONE deleted=%d failed=%d elapsed=%.1fs",
                 deleted, failed, elapsed)

    async def _edit_status(self, chat_id: int, msg_id: int,
                           html: str) -> None:
        if not (self.user and msg_id):
            return
        try:
            text, entities = await html_to_formatted(self.user, html)
            await edit_message(self.user, chat_id, msg_id, text,
                               entities=entities)
        except TdError as exc:
            if not is_message_not_modified(exc):
                log.debug("status edit failed: [%s] %s",
                          exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            log.debug("status edit failed: %s", exc)

    # ------------------------------------------------------------------
    # commands — destinations
    # ------------------------------------------------------------------
    async def _cmd_adest(self, *, chat_id: int, args: list[str],
                         text: str) -> None:
        if not args or not re.fullmatch(r"-?\d+", args[0]):
            await self._say(chat_id, "usage: .adest <channel_id>")
            return
        raw = args[0]
        # accept both the bare id (123) and a full channel id (-100123)
        full = raw if raw.startswith("-100") else f"-100{raw.lstrip('-')}"
        cfg = _load_config()
        dests = [str(d) for d in cfg.get("destinations") or []]
        if full in dests:
            cfg["selected"] = full
            _save_config(cfg)
            await self._say(chat_id, f"destination {full} set as active.")
            return
        dests.append(full)
        cfg["destinations"] = dests
        cfg["selected"] = full
        _save_config(cfg)
        await self._say(chat_id, f"added & set destination {full} as active.")

    async def _cmd_showdest(self, *, chat_id: int, args: list[str],
                            text: str) -> None:
        cfg = _load_config()
        if not cfg.get("destinations"):
            await self._say(chat_id,
                            "no destinations configured. "
                            "use .adest <id> first.")
            return
        await self._say(chat_id,
                        "destination channels — tap to set active:",
                        reply_markup=dest_buttons(cfg))

    # ------------------------------------------------------------------
    # commands — session / maintenance
    # ------------------------------------------------------------------
    async def _cmd_logout(self, *, chat_id: int, args: list[str],
                          text: str) -> None:
        await self._say(
            chat_id,
            "logging out — closing sessions. Telegram session data stays in "
            "data/tdlib (delete that directory to force a fresh login).")
        assert self.user is not None
        try:
            # TDLib clears its local database and de-authorises this device
            await self.user.request({"@type": "logOut"}, timeout=15)
        except Exception as exc:  # noqa: BLE001 - close() still follows
            log.debug("logOut request failed: %s", exc)
        self.request_shutdown("logout")

    async def _cmd_update(self, *, chat_id: int, args: list[str],
                          text: str) -> None:
        status = await self._say(chat_id, "checking for updates...")
        sid = int(status.get("id") or 0) if status else 0

        async def _edit(msg: str) -> None:
            if sid:
                await self._edit_status(chat_id, sid,
                                        f"<pre>{html_mod.escape(msg)}</pre>")

        def _git(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", *argv], cwd=str(PROJECT_ROOT),
                capture_output=True, text=True, timeout=timeout)

        try:
            fetch = await asyncio.to_thread(_git, "fetch", "origin")
            if fetch.returncode != 0:
                await _edit(f"git fetch failed:\n{fetch.stderr.strip()[:200]}")
                return
            local = (await asyncio.to_thread(
                _git, "rev-parse", "HEAD", timeout=15)).stdout.strip()
            upstream = (await asyncio.to_thread(
                _git, "rev-parse", "--abbrev-ref", "@{u}",
                timeout=15)).stdout.strip()
            if not upstream:
                upstream = "origin/omega"
            remote = (await asyncio.to_thread(
                _git, "rev-parse", upstream, timeout=15)).stdout.strip()
            if not local or not remote:
                await _edit("cannot determine local/remote revision.")
                return
            if local == remote:
                await _edit("bot is already up to date.\n"
                            f"current revision: `{local[:8]}`")
                return

            log_res = await asyncio.to_thread(
                _git, "log", "--oneline", f"{local}..{remote}", timeout=15)
            new_commits = log_res.stdout.strip()
            commit_count = len(new_commits.splitlines())
            await _edit(f"update available — {commit_count} new commit(s).\n"
                        "pulling changes...")

            pull = await asyncio.to_thread(_git, "pull", "--ff-only",
                                           timeout=120)
            if pull.returncode != 0:
                await _edit(f"git pull failed:\n{pull.stderr.strip()[:200]}")
                return

            req_changed = await asyncio.to_thread(
                _git, "diff", "--name-only", local, "HEAD", "--",
                "requirements.txt", timeout=15)
            if req_changed.stdout.strip():
                await _edit("requirements.txt changed — installing deps...")
                pip = await asyncio.to_thread(
                    lambda: subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r",
                         str(PROJECT_ROOT / "requirements.txt"), "-q"],
                        capture_output=True, text=True, timeout=300))
                if pip.returncode != 0:
                    await _edit("pip install failed — restarting anyway.\n"
                                f"{pip.stderr.strip()[:200]}")

            await _edit("update complete. restarting bot...")
            log.info("update applied: %s -> %s", local[:8], remote[:8])
            self._restart = True
            self.request_shutdown("update")
        except subprocess.TimeoutExpired:
            await _edit("update timed out — try again later.")
        except Exception as exc:  # noqa: BLE001
            log.exception("update failed")
            await _edit(f"update failed: `{exc}`")

    async def _cmd_help(self, *, chat_id: int, args: list[str],
                        text: str) -> None:
        await self._say(
            chat_id,
            "private channel copy bot — commands:\n\n"
            ".pvtcopy <start> <end>\n"
            "    server-side copy. works on channels where forwarding is "
            "allowed.\n"
            "    strips the forwarded-from tag.\n\n"
            ".pvtcopydl <start> <end>\n"
            "    download → upload → verify pipeline.\n"
            "    for channels where forwarding is disabled.\n"
            "    live progress (bar / eta / speed) appears in chat.\n\n"
            ".pvtcopydlcontinue <job_key>\n"
            "    resume an interrupted .pvtcopydl job from where it left "
            "off.\n\n"
            ".cancel [job_key]\n"
            "    cancel a running job. no key = list active jobs.\n\n"
            ".del <start_link> <end_link>\n"
            "    bulk-delete messages in range (inclusive).\n"
            "    accepts public and private t.me links.\n\n"
            ".adest <channel_id>\n"
            "    add destination or switch to existing one.\n\n"
            ".showdest\n"
            "    view and select destination channels.\n\n"
            ".logout — log out of Telegram (graceful shutdown).\n\n"
            ".update — check for updates, pull, and restart.\n\n"
            ".help — this message.")

    # ------------------------------------------------------------------
    # inline callbacks (Refresh / Cancel / select / noop)
    # ------------------------------------------------------------------
    async def _handle_callback(self, client: TDClient,
                               payload: dict) -> None:
        if self._stopping:
            return
        data = _callback_data(payload)
        query_id = int(payload.get("id") or 0)
        chat_id = int(payload.get("chat_id") or 0)
        msg_id = int(payload.get("message_id") or 0)

        if not data:
            await self._answer(client, query_id, "unknown action.")
            return
        if data == "noop":
            await self._answer(client, query_id, "job done.")
            return

        if data == "refresh" or data.startswith("refresh:"):
            key = data.split(":", 1)[1] if ":" in data else None
            hubs = self._hubs_for(key)
            if not hubs:
                await self._answer(client, query_id, "no live job.")
                if key:
                    await self._safe(self._say(
                        chat_id, f"no live job with key `{key}`.",
                        client=client))
                return
            await self._answer(client, query_id, "refreshing...")
            for hub in hubs:
                await self._safe(hub.force_refresh())
            return

        if data == "cancel" or data.startswith("cancel:"):
            key = data.split(":", 1)[1] if ":" in data else None
            await self._cancel_callback(client, query_id, chat_id, key)
            return

        if data.startswith("select:"):
            await self._select_callback(client, query_id, chat_id, msg_id,
                                        data.split(":", 1)[1])
            return

        await self._answer(client, query_id, "unknown action.")

    def _hubs_for(self, key: str | None) -> list[JobProgress]:
        if key:
            hub = self._progress.get(key)
            return [hub] if hub else []
        return list(self._progress.values())

    async def _cancel_callback(self, client: TDClient, query_id: int,
                               chat_id: int, key: str | None) -> None:
        assert self.jobs is not None
        if key is None:
            keys = self.jobs.running_keys
            if not keys:
                await self._answer(client, query_id, "no active jobs.")
                await self._safe(self._say(chat_id, "no active jobs to "
                                                    "cancel.",
                                           client=client))
                return
            key = keys[-1]
        if self.jobs.request_cancel(key):
            log.info("cancel requested for job %s", key)
            await self._answer(client, query_id, f"cancelling {key}")
            await self._safe(self._say(
                chat_id, f"⏹ cancel requested for job `{key}`.",
                client=client))
        else:
            await self._answer(client, query_id, "job not running.")
            await self._safe(self._say(
                chat_id, f"no live job with key `{key}`.", client=client))

    async def _select_callback(self, client: TDClient, query_id: int,
                               chat_id: int, msg_id: int,
                               ch_id: str) -> None:
        cfg = _load_config()
        dests = [str(d) for d in cfg.get("destinations") or []]
        if ch_id not in dests:
            await self._answer(client, query_id, "⚠️ channel not found.",
                               alert=True)
            return
        cfg["selected"] = ch_id
        _save_config(cfg)
        # re-render the buttons under the same text (TDLib needs the text)
        if msg_id:
            try:
                msgs = await get_messages(client, chat_id, [msg_id])
                msg = msgs.get(msg_id)
                content = (msg or {}).get("content") or {}
                if content.get("@type") == "messageText":
                    ft = content.get("text") or {}
                    await edit_message(
                        client, chat_id, msg_id,
                        str(ft.get("text") or ""),
                        entities=list(ft.get("entities") or []),
                        reply_markup=dest_buttons(cfg))
            except Exception as exc:  # noqa: BLE001 - button refresh is nice
                log.debug("cannot refresh destination buttons: %s", exc)
        await self._answer(client, query_id,
                           f"active destination set to {ch_id}")


# ===========================================================================
# entry point
# ===========================================================================
async def _amain() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    _set_log_level(cfg.log_level)
    log.info("config: %s", cfg.redacted())
    bot = CopyBot(cfg)
    try:
        return await bot.run()
    except DatabaseLockedError as exc:
        log.error("another copybot instance is already running: %s", exc)
        return 1
    except TDLibNotFoundError as exc:
        log.error("%s", exc)
        log.error("install TDLib (see docs/INSTALL-TDLIB.md) or set "
                  "TDLIB_LIBRARY_PATH to libtdjson.")
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
