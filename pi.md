# pvt-channel-copybot-v2 — reference (TDLib / `omega`)

## What it is

A Telegram **userbot + optional bot** that copies messages from private
channels to a destination channel without the “Forwarded from” tag. The
`omega` branch runs entirely on **TDLib (libtdjson)** via stdlib `ctypes`
bindings — no Telethon, no MTProto Python library.

## Commands

| Command | Owner | Notes |
|---|---:|---|
| `.pvtcopy <start> <end>` | user | server-side copy only; no fallback re-upload |
| `.pvtcopydl <start> <end>` | user | server copy fast path, then download+reupload fallback |
| `.pvtcopydlcontinue <job_key>` | user | resume paused/failed/interrupted job |
| `.pvtcopydlcancel [job_key]` | user | cancel running/queued job |
| `.cancel [job_key]` | user | alias for cancel |
| `.del <start> <end>` | user | batched deleteMessages |
| `.adest <channel_id>` | user | add/select destination (config.json) |
| `.showdest` | user | inline destination picker |
| `.logout` | user | TDLib logOut + graceful shutdown |
| `.update` | user | git pull --ff-only of tracking branch + graceful restart |
| `.help` | user | command reference |
| `/start` | bot | pair progress chat (`.progress_chat`) |
| Refresh/Cancel/select buttons | bot/user | inline callback queries |

## Architecture

```
Telegram updates
  └─ TDClient.receive thread (td_json_client_receive, 1s timeout)
       └─ loop.call_soon_threadsafe → UpdateRouter.dispatch
            ├─ bot.py handlers (owner-filtered commands / callbacks)
            └─ SendTracker / DownloadWatch / UploadWatch subscribers

Command
  └─ JobManager.create/spec → JobStore (SQLite WAL)
       └─ bounded scheduler (MAX_RUNNING_JOBS)
            └─ CopyPipeline.run
                 ├─ resolve chats
                 ├─ recover ambiguous "sending" markers
                 ├─ server copy batch: forwardMessages(send_copy, remove_caption)
                 └─ fallback: download (dl_sem) → build input content → send (ul_sem)
                      └─ per message/chunk: BEGIN IMMEDIATE → markers+cursor+commit
```

## Key modules

- `bot.py` — wiring, handlers, HTML→entities, inline keyboards, signals,
  graceful shutdown/restart.
- `pipeline.py` — one job execution; no busy loops; progress sink throttled.
- `checkpoint.py` — `jobs.sqlite3` source of truth; atomic JSON helper;
  legacy meta import.
- `job_manager.py` — validated state machine, dedup, queue, task tracking.
- `tdlib_native.py` — libtdjson resolution/loading; `td_json_client_*`.
- `tdlib_client.py` — request/response correlation via `@extra`, update
  waiters, bounded close.
- `tdlib_auth.py` — waitTdlibParameters → phone/code/password/registration →
  ready; bot token path.
- `tdlib_media.py` — `updateFile`-watched downloads; upload completion via
  `sendMessage` + message send-state tracking; `build_input_content`.
- `file_splitter.py` — ffmpeg/7z/split; verify before deleting original.

## Job states

`queued → running/downloading/uploading → completed|failed|paused|cancelling`

`cancelling` is transient; cooperative cancel parks as `paused` (resumable).
Stale in-flight rows after crash are recovered to `paused` on startup.

## Important invariants

- Never delete/overwrite legacy `*.session` files.
- Never log API hash / bot token / phone code / 2FA password.
- TDLib DBs are single-process (`copybot.lock`).
- Progress edits are best-effort; never fail a job because of an edit.
- A message is marked `sending` before side effects that can create a
  remote message; ambiguous markers are resolved by ordering/content match.
- `forwardMessages(send_copy=true, remove_caption=false)` is the server-copy
  path. `.pvtcopy` does not fall back to reupload.

## Config / data

- `.env` — secrets and tunables (git-ignored).
- `config.json` — destinations/selected (atomic writes).
- `.progress_chat` — paired bot chat id.
- `data/tdlib/{user,bot}` — TDLib databases (sensitive).
- `data/jobs.sqlite3` — job/checkpoint DB.
- `_pvtcopydl_tmp/` — staging downloads/split parts; legacy meta files left
  in place.

## Tests (actually run in this environment)

```bash
python -m compileall -q .
python tests/test_payload_shapes.py                  # real libtdjson, offline
python -m unittest discover -s tests -p 'test_*.py' -v
```

No real Telegram credentials were available here, so auth/media transfer
against Telegram was not executed; see final report / BEHAVIOR-CHANGES.
