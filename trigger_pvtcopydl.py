#!/usr/bin/env python3
"""
trigger_pvtcopydl.py — drive the copy bot headlessly (no Telegram UI).

Why this exists
---------------
bot.py is a long-running TDLib service.  Normally you interact with it by
sending `.pvtcopydl <start> <end>` from your phone.  In a headless
environment (CI, a server without Telegram installed, smoke tests) you can
issue the very same command from the shell instead::

    python trigger_pvtcopydl.py .pvtcopydl https://t.me/c/123/4 https://t.me/c/123/50
    python trigger_pvtcopydl.py .pvtcopy   https://t.me/c/123/4 https://t/me/c/123/50
    python trigger_pvtcopydl.py .pvtcopydlcontinue ab12cd34ef56
    python trigger_pvtcopydl.py --list

What it does
------------
1. boots the **real** ``CopyBot`` (TDLib clients, login, job manager) —
   the identical code path a Telegram message would take, only the update
   routing is skipped;
2. dispatches the command into a chat (``--chat``, else the paired bot
   progress chat from ``.progress_chat``);
3. watches the job store until the job reaches a terminal state, printing
   a status line every few seconds;
4. shuts TDLib down gracefully and exits ``0`` on completion, ``1``
   otherwise.

There is **no** Telethon, no synthetic events and no session-file
patching: sessions are TDLib databases under ``data/tdlib/``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# Anchor cwd to the project root so load_dotenv() finds .env even when this
# script is launched from elsewhere.
os.chdir(ROOT)
os.environ.setdefault("PYTHONUNBUFFERED", "1")

from checkpoint import JobState, JobStore  # noqa: E402
from config import Config, ConfigError  # noqa: E402

TERMINAL = {JobState.COMPLETED, JobState.FAILED}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="trigger_pvtcopydl",
        description="issue a copy-bot command headlessly and wait for it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("examples:\n"
                "  trigger_pvtcopydl.py .pvtcopydl "
                "https://t.me/c/1/4 https://t.me/c/1/50\n"
                "  trigger_pvtcopydl.py --chat -100123 .pvtcopy "
                "https://t.me/c/1/4 https://t.me/c/1/50\n"
                "  trigger_pvtcopydl.py --list\n"))
    p.add_argument("command", nargs="?",
                   help="the command to run, e.g. .pvtcopydl <start> <end>")
    p.add_argument("rest", nargs="*",
                   help="remaining words of the command (links, job key)")
    p.add_argument("--chat", type=int, default=None, metavar="ID",
                   help="chat that receives the status message "
                        "(default: the paired bot progress chat)")
    p.add_argument("--timeout", type=float, default=3600.0, metavar="SEC",
                   help="give up after this many seconds (default 3600)")
    p.add_argument("--poll", type=float, default=5.0, metavar="SEC",
                   help="status print interval (default 5)")
    p.add_argument("--list", action="store_true",
                   help="list known jobs and exit (no login needed)")
    return p.parse_args(argv)


def list_jobs() -> int:
    """`--list`: open the checkpoint DB only (no TDLib, no login)."""
    cfg = Config.from_env()
    if not cfg.jobs_db_path.exists():
        print(f"no job database at {cfg.jobs_db_path}")
        return 0
    store = JobStore(cfg.jobs_db_path)
    try:
        jobs = store.list_jobs()
        if not jobs:
            print("no jobs recorded")
            return 0
        print(f"{'key':12} {'state':12} {'range':>16} {'cursor':>10} "
              f"{'sent':>6} {'skip':>5} {'err':>4}  src → dst")
        for j in jobs:
            print(f"{j.job_key:12} {j.state:12} "
                  f"{f'{j.start_msg_id}..{j.end_msg_id}':>16} "
                  f"{j.current_msg_id:>10} {j.uploaded:>6} "
                  f"{j.skipped:>5} {j.errors:>4}  {j.src} → {j.dst}")
        return 0
    finally:
        store.close()


async def _wait_for_job(bot, key: str, *, timeout: float,
                        poll: float) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        rec = bot.store.get_job(key)
        if rec is None:
            return "gone"
        line = (f"[state={rec.state}] cursor {rec.current_msg_id}/"
                f"{rec.end_msg_id} sent={rec.uploaded} "
                f"skip={rec.skipped} err={rec.errors} "
                f"retries={rec.retries} flood={rec.flood_waits}")
        if line != last:
            print(line, flush=True)
            last = line
        if rec.state in TERMINAL:
            return rec.state
        await asyncio.sleep(poll)
    return "timeout"


async def run_command(args: argparse.Namespace) -> int:
    from bot import CopyBot, get_progress_chat_id

    cfg = Config.from_env()
    bot = CopyBot(cfg)
    rc = await bot.start()
    if rc != 0:
        print("[trigger] startup failed", file=sys.stderr)
        return rc

    chat_id = args.chat if args.chat is not None else get_progress_chat_id()
    if not chat_id:
        print("[trigger] no chat target: pass --chat <id> or send the bot "
              "/start once so .progress_chat exists", file=sys.stderr)
        await bot._shutdown("trigger")
        return 2

    before = {j.job_key for j in bot.store.list_jobs()}
    text = " ".join([args.command, *args.rest]).strip()
    print(f"[trigger] dispatching {text!r} → chat {chat_id}", flush=True)
    t0 = time.monotonic()
    await bot._dispatch_command(chat_id, text)

    new_jobs = [j for j in bot.store.list_jobs()
                if j.job_key not in before]
    if not new_jobs:
        print("[trigger] no job was created (bad links / duplicate / "
              "no destination?)", file=sys.stderr)
        await bot._shutdown("trigger")
        return 1
    key = new_jobs[-1].job_key
    print(f"[trigger] job {key} created; waiting …", flush=True)

    state = await _wait_for_job(bot, key, timeout=args.timeout,
                                poll=args.poll)
    elapsed = time.monotonic() - t0
    rec = bot.store.get_job(key)
    print(f"[trigger] job {key} → {state} after {elapsed:.1f}s "
          f"(sent={rec.uploaded if rec else '?'} "
          f"skip={rec.skipped if rec else '?'} "
          f"err={rec.errors if rec else '?'})", flush=True)

    await bot._shutdown("trigger")
    return 0 if state == JobState.COMPLETED else 1


def main() -> int:
    args = parse_args()
    if args.list:
        try:
            return list_jobs()
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    if not args.command:
        print("usage: trigger_pvtcopydl.py <command> [args…]  "
              "(or --list)", file=sys.stderr)
        return 2
    if not args.command.startswith("."):
        print("command must start with '.' (e.g. .pvtcopydl)",
              file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_command(args))
    except KeyboardInterrupt:
        print("[trigger] interrupted", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
