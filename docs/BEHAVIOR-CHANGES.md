# Behavior changes — Telethon → TDLib

This release replaces the Telegram engine (Telethon / MTProto) with **TDLib**
(`libtdjson`, official Telegram database library) while preserving the
user-facing command surface.  The changes below are the ones operators and
users should know about.

## Preserved

- Commands: `.pvtcopy`, `.pvtcopydl`, `.pvtcopydlcontinue`,
  `.pvtcopydlcancel`, `.cancel`, `.del`, `.adest`, `.showdest`, `.logout`,
  `.update`, `.help`.
- Bot `/start` pairs a chat for live progress; inline Refresh / Cancel /
  destination-select buttons work.
- Destination config in `config.json` (`destinations` + `selected`),
  written atomically.
- `.progress_chat` pairing file.
- Legacy `*_meta.json` checkpoints are imported idempotently on first
  start and **never deleted**.
- `MAX_SINGLE_FILE_BYTES` default remains 1.9 GB; split parts are verified
  before the original is deleted.

## Changed / fixed

| Area | Old (Telethon) | New (TDLib) |
|---|---|---|
| Telegram engine | Telethon + custom `WALSession` | TDLib `tdjson` via `ctypes`; one client per account, one receive thread per client |
| Auth | Telethon interactive login | TDLib auth state machine; phone/code/2FA prompted on TTY; bot token via `checkAuthenticationBotToken` |
| Session storage | `*.session` SQLite files in project root | TDLib databases under `data/tdlib/{user,bot}/` with a single-process lock (`copybot.lock`) |
| Progress transport | Telethon Bot client + user edits | TDLib bot account posts/edits the dashboard in the paired chat; falls back to the user message if no bot token |
| `.pvtcopy` | server-side copy via Telethon, with ad-hoc fallback | **server-only**: batches `forwardMessages(send_copy=true, remove_caption=false)`; if forwarding is restricted it fails with a clear hint to use `.pvtcopydl` (never silently re-uploads) |
| `.pvtcopydl` | download+upload pipeline with Telethon + aria2c stubs | TDLib async download (`downloadFile` + `updateFile` watch) + TDLib upload (`sendMessage`/`sendMessageAlbum` with `inputFileLocal`); server-copy fast path tried first, then per-message fallback |
| Checkpoints | JSON `_meta.json` per job | SQLite `data/jobs.sqlite3` (WAL, busy_timeout, transactional state machine) as primary; legacy meta imported, not deleted |
| Job state | in-memory `ACTIVE_DL_JOBS` dict | `JobManager` with validated transitions, dedup, bounded queue, cooperative cancel/pause, stale-row recovery after restart |
| Concurrency | effectively unbounded/loop-driven | bounded semaphores for downloads and uploads, bounded running jobs, bounded queue, throttled progress edits |
| `.logout` | `client.disconnect()` + delete session + `os._exit(0)` | TDLib `logOut` + graceful shutdown (no `os._exit`); session data stays in `data/tdlib/` until you remove it deliberately |
| `.update` | `git pull origin main` + pip install + `os.execv` inline | `git fetch` + `git pull --ff-only` of the tracking branch, optional `pip install`, graceful restart via re-exec **after** shutdown; git/pip run off the event loop |
| `.del` | per-message delete loop | batched `deleteMessages` (100 ids) with per-id salvage on batch failure |
| Config writes | `open(...,"w")` + `json.dump` | atomic `tmp + fsync + rename` |
| Shutdown | ad-hoc | SIGTERM/SIGINT install a handler that cancels jobs cooperatively, closes both TDLib clients, drains tracked tasks, releases locks |
| Error handling | broad `except Exception` in places | classified errors (`ErrorKind`) with flood-wait respect, exponential backoff + jitter, skip vs stop-job vs retry decisions |

## Operational notes

- **No hybrid runtime.** There is zero Telethon import at runtime; do not
  re-add `telethon`/`cryptg`.
- **TDLib databases are single-process.** A `copybot.lock` file in each
  account DB directory prevents concurrent access. A second start fails
  fast with a clear error.
- **Legacy sessions are preserved.** `*.session` files are never deleted or
  overwritten by the migration. TDLib creates its own database; you will be
  asked to log in once.
- **Progress chat pairing.** Send `/start` to the bot account from the
  owner's account to receive live dashboards in a dedicated chat. Without a
  bot token, the dashboard is edited in the command chat.
- **`MAX_CONCURRENT_*` semantics.** These are now real semaphores shared
  across all running jobs. Old values that relied on "one job at a time"
  may behave differently if you raise `MAX_RUNNING_JOBS`.
- **`.update` target branch.** The updater pulls the current tracking
  branch (`@{u}`) with `--ff-only`, not a hardcoded `origin/main`. This is
  deliberate on the `omega` migration branch; review before merging to
  main.

## Known limitations

- **No live Telegram auth/media transfer was executed in this environment**
  (no `API_ID`/`API_HASH`/`BOT_TOKEN`, no Telegram credentials). The
  payload shapes, dispatcher, state machine, checkpoint/resume and
  pipeline logic are covered by offline and fake-adapter tests; real
  end-to-end Telegram behaviour must be smoke-tested on a staging account.
- TDLib is a native dependency. See `docs/INSTALL-TDLIB.md`.
- `inputMessagePoll` re-upload is intentionally conservative (polls are
  re-sent as text cards in the fallback path when a direct input poll is
  not representable) — verify poll-heavy channels manually.
