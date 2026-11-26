<div align="center">

# 📋 Private Channel Copy Bot v2 — TDLib edition

**Copy messages from private Telegram channels without the “Forwarded from” tag**

*TDLib engine • resumable jobs • live progress • bulk delete*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![TDLib](https://img.shields.io/badge/Telegram-TDLib-2CA5E0.svg)](https://core.telegram.org/tdlib)

</div>

---

> **Migration status (branch `omega`)**: the bot has been migrated from
> Telethon to **TDLib (tdjson)**. There are **zero Telethon imports at
> runtime**. Legacy Telethon `*.session` files are preserved (never
> deleted/overwritten); TDLib uses its own database under `data/tdlib/`
> and you will log in once on first run. See
> [docs/MIGRATION-TELETHON-TO-TDLIB.md](docs/MIGRATION-TELETHON-TO-TDLIB.md),
> [docs/BEHAVIOR-CHANGES.md](docs/BEHAVIOR-CHANGES.md) and
> [docs/UPGRADE-AND-ROLLBACK.md](docs/UPGRADE-AND-ROLLBACK.md).

## ✨ Features

- **Server-side copy** (`.pvtcopy`) — `forwardMessages(send_copy=true)`,
  strips the forwarded-from tag. If forwarding is restricted, it tells you
  to use `.pvtcopydl` instead of silently re-uploading.
- **Download → upload pipeline** (`.pvtcopydl`) — for channels where
  forwarding is disabled. Server-copy fast path first, per-message
  fallback with download/upload verification semantics.
- **Resumable jobs** — transactional SQLite checkpoint (`data/jobs.sqlite3`,
  WAL). Crash/restart recovery parks interrupted jobs as `paused`; resume
  with `.pvtcopydlcontinue <job_key>`.
- **Live progress** — box-drawing dashboard with bar/ETA/speed/counters,
  Refresh/Cancel inline buttons. Posted by the bot account when paired;
  falls back to the command chat.
- **Bulk delete** (`.del`) — batched `deleteMessages`, per-id salvage on
  batch errors.
- **Bounded concurrency** — configurable max concurrent downloads/uploads,
  max running jobs, queued jobs, retries with exponential backoff + jitter,
  FloodWait respect, throttled progress edits.
- **Graceful shutdown** — SIGTERM/SIGINT cancels jobs cooperatively,
  closes TDLib clients, releases single-process DB locks. No `os._exit`.
- **Safe splitting** — files above Telegram’s limit are split and the
  original is deleted only after verification; optional `SPLIT_VERIFY_HASH`.

## 📸 Progress preview

```
╭──────────────────────────────────╮
│ ⠹ ⬇ Downloading                 │
│ 📄 video.mov                     │
├──────────────────────────────────┤
│ ████████████░░░░░░░░  67.3%      │
│                                  │
│ 📦  1.25 GB / 2.00 GB            │
│ ⚡  12.7 MB/s                    │
│ ⏳  ETA      1m 05s              │
│ 🕒  Elapsed  2m 30s              │
├──────────────────────────────────┤
│ DL: 45 · UL: 40 · skip: 3 · err: 2│
╰──────────────────────────────────╯
```

## 🚀 Quick start

### Prerequisites

- Python **3.10+** (3.14 OK; bindings use stdlib `ctypes`)
- Telegram API credentials: <https://my.telegram.org/apps>
- A Bot token from @BotFather (optional but recommended for live progress)
- **TDLib `libtdjson` shared library** — see
  [docs/INSTALL-TDLIB.md](docs/INSTALL-TDLIB.md)

### Install

```bash
git clone https://github.com/vastomonarch/pvt-channel-copybot-v2.git
cd pvt-channel-copybot-v2

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: API_ID, API_HASH, BOT_TOKEN (optional), DESTINATION
```

Install TDLib (pick one):

```bash
# easiest: wheel that bundles libtdjson (the bot only loads the .so)
pip install tdjson

# or distro package / source build — see docs/INSTALL-TDLIB.md
```

Verify TDLib is loadable:

```bash
python -c "from tdlib_native import resolve_library_path; print(resolve_library_path())"
```

### First run (interactive login)

```bash
python bot.py
```

On first run TDLib will prompt for phone / code / 2FA in the terminal.
The session is stored in `data/tdlib/user/` (never committed). If you use
a bot token, the bot account logs in non-interactively.

Then send `/start` to your bot in Telegram to pair the live-progress chat.

## 📖 Commands

All owner commands use the `.` prefix (userbot convention). `/start` is a
normal bot command.

| Command | Description |
|---|---|
| `.pvtcopy <start> <end>` | Server-side copy only. Fails with a hint if forwarding is restricted. |
| `.pvtcopydl <start> <end>` | Download/upload pipeline with live progress; resumes after crash. |
| `.pvtcopydlcontinue <job_key>` | Resume an interrupted/paused/failed job. |
| `.pvtcopydlcancel [job_key]` | Cancel a running job (alias: `.cancel`). |
| `.cancel [job_key]` | Cancel a running job; no key lists active jobs. |
| `.del <start_link> <end_link>` | Bulk-delete messages in an inclusive range. |
| `.adest <channel_id>` | Add destination or switch to an existing one. |
| `.showdest` | Show destinations with inline select buttons. |
| `.logout` | Log out of Telegram and shut down gracefully. |
| `.update` | `git pull --ff-only` of the tracking branch, install deps if needed, graceful restart. |
| `.help` | Show command reference. |

Links can be private (`https://t.me/c/<id>/<msg>`) or public
(`https://t.me/<user>/<msg>`). Start/end must belong to the same source.

## 💡 Examples

```text
.pvtcopy https://t.me/c/1234567890/100 https://t.me/c/1234567890/200
.pvtcopydl https://t.me/c/1234567890/100 https://t.me/c/1234567890/200
.pvtcopydlcontinue ab12cd34ef56
.cancel ab12cd34ef56
.del https://t.me/c/1234567890/100 https://t.me/c/1234567890/200
.adest -1001234567890
.showdest
```

## ⚙️ Configuration

Main config is `.env` (see [.env.example](.env.example)). Runtime
destination selection is `config.json` (see
[config.example.json](config.example.json)).

Important knobs:

| Variable | Default | Meaning |
|---|---:|---|
| `MAX_CONCURRENT_DOWNLOADS` | 3 | parallel download slots (shared across jobs) |
| `MAX_CONCURRENT_UPLOADS` | 2 | parallel upload slots |
| `MAX_RUNNING_JOBS` | 2 | jobs running at once |
| `MAX_QUEUED_JOBS` | 8 | queued jobs waiting for a slot |
| `RETRY_COUNT` | 4 | transient retries per message/batch |
| `PROGRESS_EDIT_INTERVAL` | 2.5 | min seconds between progress edits |
| `SEND_TIMEOUT` | 1800 | seconds for send/upload requests |
| `DOWNLOAD_STALL_TIMEOUT` | 300 | no-progress watchdog |
| `SHUTDOWN_TIMEOUT` | 20 | graceful shutdown budget |
| `MAX_SINGLE_FILE_BYTES` | 1.9 GB | split threshold |
| `SPLIT_VERIFY_HASH` | 0 | expensive split verification |

## 🗂️ Project structure

```text
.
├── bot.py                  # command/callback handlers, lifecycle, shutdown
├── pipeline.py             # per-job copy pipeline (server copy + fallback)
├── checkpoint.py           # SQLite job store, atomic JSON, legacy import
├── job_manager.py          # queue/dedup/state machine/cancellation
├── tdlib_native.py         # ctypes bindings + libtdjson resolution
├── tdlib_client.py         # async request/update client (per account)
├── tdlib_updates.py        # update router + tracked tasks
├── tdlib_auth.py           # TDLib authorization state machine
├── tdlib_sessions.py       # DB dirs + single-process lock
├── tdlib_messages.py       # message helpers (copy/send/edit/delete)
├── tdlib_media.py          # download/upload watches, input content builders
├── config.py               # env-driven config
├── errors.py               # TdError + classification/backoff
├── file_splitter.py        # verified splitting (ffmpeg/split/7z)
├── trigger_pvtcopydl.py    # headless CLI driver (no Telegram UI)
├── progress_ui/            # box/spinner/format renderers
├── deploy/
│   ├── copybot.service     # systemd unit template
│   └── install.sh          # idempotent install/upgrade helper
├── docs/
│   ├── INSTALL-TDLIB.md
│   ├── MIGRATION-TELETHON-TO-TDLIB.md
│   ├── BEHAVIOR-CHANGES.md
│   └── UPGRADE-AND-ROLLBACK.md
└── tests/                  # offline/fake-adapter tests (run for real)
```

Runtime data (git-ignored):

```text
data/
  tdlib/user/   tdlib/bot/      # TDLib databases (sensitive)
  jobs.sqlite3                  # checkpoint/state DB
  logs/
_pvtcopydl_tmp/                 # staging downloads / split parts
.progress_chat                  # paired bot chat id
config.json                     # destinations
```

## 🛠️ Deployment (systemd)

```bash
sudo ./deploy/install.sh                 # venv + deps + TDLib check
sudo ./deploy/install.sh --login         # one-time interactive Telegram login
sudo ./deploy/install.sh --systemd       # install + enable service
sudo systemctl start copybot
journalctl -u copybot -f
```

The service uses `KillSignal=SIGTERM` and `TimeoutStopSec=60` so the bot
can park jobs resumably. See [deploy/copybot.service](deploy/copybot.service).

## ⬆️ Upgrade / rollback

See [docs/UPGRADE-AND-ROLLBACK.md](docs/UPGRADE-AND-ROLLBACK.md). Short
version: stop cleanly, back up `.env`/`config.json`/`data/`/
`_pvtcopydl_tmp/`/legacy `*.session`, `git pull --ff-only`, rerun
`install.sh`, restart. Rollback across the TDLib migration keeps legacy
`*.session` files and legacy `*_meta.json` files; jobs created only in
`jobs.sqlite3` are not readable by the old Telethon build.

## 🧪 Testing

Run the tests that do **not** require Telegram credentials:

```bash
python -m compileall -q .
python tests/test_payload_shapes.py
python -m unittest discover -s tests -p 'test_*.py' -v
```

The payload-shape test uses the real `libtdjson` and validates request
shapes offline (TDLib field validation happens before the auth gate).

> **Honest limitation:** real Telegram authentication, message copy,
> download/upload, FloodWait/restricted-channel behaviour, and bot button
> delivery were **not** executed in this development environment because no
> Telegram credentials are available. Those paths must be smoke-tested on a
> staging account before production rollout.

## ⚠️ Limitations

- Telegram rate limits/FloodWaits apply (classified and respected, but not
  magically removable).
- Very large files are subject to Telegram’s limits; the bot splits above
  `MAX_SINGLE_FILE_BYTES` and verifies parts before deleting the original.
- Service messages are skipped; polls/venues/contacts have conservative
  fallback rendering in the reupload path.
- TDLib databases are single-process; two bot instances cannot share the
  same `data/tdlib/<account>` directory.
- `.pvtcopy` refuses to re-upload when forwarding is restricted — use
  `.pvtcopydl`.

## 🔒 Security

See [SECURITY.md](SECURITY.md). Never commit `.env`, `data/`, `*.session`,
or `config.json` with real IDs. Session data grants account access.

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please include Python/OS/TDLib
version and redact tokens, phone numbers, and channel IDs in reports.

## 📄 License

[MIT](LICENSE)
