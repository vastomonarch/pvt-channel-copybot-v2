# Upgrade and rollback

## Before upgrading

1. **Back up the things the bot must never lose:**
   ```bash
   cp -a .env .env.bak
   cp -a config.json config.json.bak          # if present
   cp -a .progress_chat .progress_chat.bak    # if present
   cp -a data data.bak                        # TDLib DBs + jobs.sqlite3 + logs
   cp -a _pvtcopydl_tmp _pvtcopydl_tmp.bak    # in-flight staging / legacy metas
   # legacy Telethon sessions (never deleted by the migration, but back them up)
   cp -a *.session *.session-journal *.session-wal *.session-shm sessions.bak/ 2>/dev/null || true
   ```
2. **Stop the bot cleanly** so jobs park in a resumable state:
   ```bash
   sudo systemctl stop copybot
   # or, if running in a terminal: Ctrl-C / SIGTERM
   ```
3. Note the current revision:
   ```bash
   git rev-parse --short HEAD
   ```

## Upgrade (systemd host)

```bash
cd /opt/pvt-channel-copybot-v2          # your checkout
git fetch origin
git checkout omega                      # or the tag/branch you deploy
git pull --ff-only

# re-create venv deps / refresh the unit file; never touches .env/data/config
sudo ./deploy/install.sh --systemd

sudo systemctl start copybot
journalctl -u copybot -f
```

`install.sh` is idempotent: it will not overwrite `.env`, `config.json`,
`data/`, or any TDLib session.

## Upgrade (manual / dev)

```bash
git pull --ff-only
venv/bin/pip install -r requirements.txt
python -m compileall -q .
venv/bin/python bot.py
```

## Post-upgrade smoke check

1. The process starts and logs `copybot ready — …`.
2. `python trigger_pvtcopydl.py --list` shows known jobs.
3. Send `/start` to the bot and confirm the pairing reply.
4. In a safe private channel you control, run `.help`, `.showdest`, then a
   tiny `.pvtcopy` range (2–3 messages) and confirm completion.
5. Run a tiny `.pvtcopydl` range and confirm download/upload progress.
6. Cancel a running job with the inline Cancel button and resume it with
   `.pvtcopydlcontinue <key>`.

> Real Telegram end-to-end testing requires valid credentials and a test
> channel. Do **not** smoke-test against production channels you cannot
> afford to duplicate/delete in.

## Rollback

Rollback means: restore the previous code **and** keep the data the
previous version understands.

### If you only need to undo code

```bash
sudo systemctl stop copybot
git checkout <previous-revision>
sudo ./deploy/install.sh --systemd
sudo systemctl start copybot
```

### Rolling back across the TDLib migration (omega → pre-migration)

The TDLib branch keeps legacy Telethon `*.session` files untouched, but it
**does not** write Telethon checkpoints. Rolling back to a Telethon build
means:

1. Stop the service.
2. `git checkout <pre-migration-revision>` and reinstall its
   `requirements.txt` (which include `telethon`).
3. Keep `*.session` files — the old build will reuse them.
4. `data/jobs.sqlite3` is a TDLib-era checkpoint DB; the old Telethon code
   will not read it. Legacy `*_meta.json` files that were present before
   migration are still in `_pvtcopydl_tmp/` (the importer never deletes
   them) and can be resumed by the old code.
5. Any job created **after** the migration exists only in `jobs.sqlite3`.
   Before rolling back, either let it finish, or accept that you must
   re-issue it on the old build (dedup in the old build is by its own meta
   files).

```bash
sudo systemctl stop copybot
git checkout 13a217c          # example pre-migration revision — pick yours
python -m venv venv && venv/bin/pip install -r requirements.txt
# restore .env/config.json if needed
sudo systemctl start copybot   # if you use the old unit; otherwise run manually
```

### If a job is stuck after crash

Do **not** delete `data/jobs.sqlite3`. Start the bot; stale
`running/downloading/uploading/cancelling` rows are recovered to `paused`.
Then resume with `.pvtcopydlcontinue <job_key>`.

### If the TDLib DB is corrupted

Stop the bot, move `data/tdlib/<account>/` aside (keep a backup), restart
and log in again. `jobs.sqlite3` and `config.json` are separate; keep them
unless they are the corrupted component.
