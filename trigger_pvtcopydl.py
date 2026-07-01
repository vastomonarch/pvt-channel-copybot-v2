#!/usr/bin/env python3
"""
trigger_pvtcopydl.py — invoke bot.py's .pvtcopydl handler directly.

Why this exists
---------------
bot.py is a long-running Telethon Userbot.  Normally you would interact
with it by sending `.pvtcopydl <start> <end>` to your own Telegram chat
from your phone.  In a headless environment we can't do that, so we
import the module, bring up both Telethon clients (user account + bot),
and call the registered handler with a synthetic NewMessage event.

The synthetic event satisfies the surface that the handler actually
touches on:

    event.pattern_match.group(i)            – regex match groups
    await event.respond(text)               – returns a status_msg
    status_msg.edit(text)                   – live progress updates

Everything else (Telethon API calls, Bot-driven progress callbacks,
FileReferences, FloodWaits) is the real bot.py code path.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
# Anchor cwd to the project root so `load_dotenv()` finds `.env` even when
# this script is launched from a tmux pane whose cwd may differ.
os.chdir(ROOT)
os.environ.setdefault("PYTHONUNBUFFERED", "1")


# ---------------------------------------------------------------------------
# Workaround: enable WAL journal mode on the Telethon SQLite session files.
#
# The project lives on an overlayfs mount, which is known to mishandle the
# POSIX fcntl advisory locks SQLite uses when it first escalates to an
# EXCLUSIVE lock (Telethon triggers this during `connect()` with
# `delete from sessions`).  After any SIGKILL of a previous bot process
# the overlayfs lock state drags, so the next process boots into
# `sqlite3.OperationalError: database is locked`.
#
# Switching the database to WAL (Write-Ahead Logging) is persistent — the
# change is written into the SQLite header — so it survives process
# restarts.  WAL uses a `-wal` file plus a `-shm` shared-memory index and
# never needs exclusive DB-level locks for normal reads, so the overlayfs
# fcntl bug is bypassed entirely.  Auth keys are preserved.
# ---------------------------------------------------------------------------
def _enable_wal_journal(path: str) -> None:
    import sqlite3
    if not os.path.exists(path):
        print(f"[trigger] session file absent, skipping WAL setup: {path}")
        return
    try:
        # isolation_level=None keeps autocommit, so the PRAGMA persists
        # without us having to remember to commit.
        c = sqlite3.connect(path, timeout=5, isolation_level=None)
        mode = c.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
        # NORMAL is the recommended pairing for WAL — full FSYNC on each
        # commit is unnecessary and slower.
        c.execute("PRAGMA synchronous=NORMAL;")
        c.close()
        print(f"[trigger] set {mode!r} journal_mode on {path}")
    except Exception as e:
        print(f"[trigger] WAL setup failed for {path}: {type(e).__name__}: {e}")


for _sess_name in ("copybot_session.session", "copybot_bot_session.session"):
    _enable_wal_journal(os.path.join(ROOT, _sess_name))

# Importing bot.py triggers its module-level wiring (clients, globals, env
# loading, config).  We just import, we don't yet connect.
from bot import (  # noqa: E402
    client,
    bot_client,
    BOT_TOKEN,
    handle_pvtcopydl,
    _register_bot_handlers,
)

# The exact command the user asked for.
CMD = (
    ".pvtcopydl "
    "https://t.me/c/3854998856/3 "
    "https://t.me/c/3854998856/234"
)


class FakeStatusMsg:
    """Mimics the Telethon Message object returned by `event.respond(...)`.

    bot.py calls `.edit(text, buttons=...)` on this object several times
    while the job runs; printing each edit lets us see the live progress
    in stdout without needing the user to /pvtcopy a personal account.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        print(f"[STATUS] {text}")

    async def edit(self, text, **kwargs) -> "FakeStatusMsg":
        self.text = text
        print(f"[STATUS] {text}")
        return self

    async def delete(self) -> None:
        pass


class FakeEvent:
    """Mimics a Telethon NewMessage event for the .pvtcopydl pattern."""

    def __init__(self, text: str) -> None:
        m = re.match(r"\.pvtcopydl\s+(\S+)\s+(\S+)", text)
        if not m:
            raise RuntimeError(f"command does not match .pvtcopydl pattern: {text!r}")
        self.pattern_match = m

    async def respond(self, msg, **kwargs) -> FakeStatusMsg:
        return FakeStatusMsg(msg)


async def main() -> None:
    # Set these globals that main() in bot.py normally sets; bot.py's
    # handle_pvtcopydl references ME_ID when the .progress_chat cache is
    # empty.  Better to set them ourselves so we don't depend on bot.py
    # having been run first.
    import bot as botmod
    botmod._progress_chat_id = None  # force re-read from .progress_chat
    botmod._progress_chat_loaded = False

    print(f"[trigger] python={sys.version.split()[0]} cwd={os.getcwd()}")
    print("[trigger] starting userbot client (session=copybot_session)…")
    await client.start()
    me = await client.get_me()
    botmod.ME_ID = me.id
    print(f"[trigger] userbot OK: {me.first_name} (id={me.id})")

    if bot_client is not None and BOT_TOKEN:
        print("[trigger] starting bot_client (bot_token)…")
        await bot_client.start(bot_token=BOT_TOKEN)
        bot_me = await bot_client.get_me()
        botmod.BOT_USERNAME = bot_me.username
        botmod.BOT_ID = bot_me.id
        # Wire the bot-side Refresh / Cancel / /start callbacks so the
        # inline buttons on the live progress message actually work.  This
        # is the same call bot.py's main() makes on first run.
        _register_bot_handlers()
        print(f"[trigger] bot_client OK: @{bot_me.username} (id={bot_me.id})")
    else:
        print("[trigger] bot_client disabled (no BOT_TOKEN in .env)")

    evt = FakeEvent(CMD)
    print(f"[trigger] invoking handle_pvtcopydl for: {CMD}")
    await handle_pvtcopydl(evt)
    print("[trigger] handle_pvtcopydl returned cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[trigger] KeyboardInterrupt — exiting")
    except Exception as e:
        print(f"[trigger] FATAL: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
