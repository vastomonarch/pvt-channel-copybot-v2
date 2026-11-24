# Migration Note: Telethon → TDLib ("omega" branch)

> **Written before any structural changes**, as required by the project's git and
> safety constraints. This document records the pre-migration state, the plan, and
> the safety/rollback procedure.

## 1. Pre-migration state (audited 2026-11-24)

- Branch: `omega` (clean working tree, at `1f1c323`, matches `origin/omega`).
- Telegram engine: **Telethon ≥ 1.36** with a custom `WALSession` (SQLite WAL).
- Components: `bot.py` (2904 lines, all handlers + copy loop), `pipeline.py`
  (Telethon download/upload + unused aria2c RPC stub), `wal_session.py`
  (Telethon SQLiteSession subclass), `file_splitter.py`, `trigger_pvtcopydl.py`
  (headless trigger with WAL session patching), `progress_ui/` (pure rendering).
- No session files, checkpoints, downloads, `config.json`, or logs are tracked in
  the repository (all gitignored / runtime-only). **Nothing user-owned lives in
  git**, so no tracked data can be destroyed by this migration.

## 2. What is preserved

Every user-facing command and workflow is preserved with identical syntax:

`.pvtcopy`, `.pvtcopydl`, `.pvtcopydlcontinue`, `.pvtcopydlcancel`, `.cancel`,
`.del`, `.adest`, `.showdest`, `.logout`, `.update`, `.help`, plus the bot-side
`/start` pairing and Refresh/Cancel inline buttons (both in the user's chat and
the progress-bot chat).

Behavioral changes are documented in `docs/BEHAVIOR-CHANGES.md` (created with the
implementation) and summarized in the final report.

## 3. Safety rules observed during this migration

1. Telethon `.session` files are **never deleted, overwritten, or converted**.
   They remain on disk as a rollback artifact. TDLib uses new, separate database
   directories (`data/tdlib/user`, `data/tdlib/bot`).
2. Existing checkpoint files (`_pvtcopydl_tmp/*_meta.json`) are **imported** into
   the new job database on first start, then left in place (never deleted).
3. No credentials, session databases, or secrets are committed.
4. No git history is rewritten; no force-push. Work is committed incrementally on
   `omega`.
5. `os._exit()` is removed; shutdown is signal-driven and graceful.

## 4. Integration choice (decision record)

**Direct ctypes integration against the official `tdjson` C interface**
(`td_json_client_*` symbols of `libtdjson.so`), implemented in
`tdlib_native.py` + `tdlib_client.py`.

Rationale (evaluated 2026-11-24):

| Option | Python 3.14 | Notes |
|---|---|---|
| `python-telegram` 2.0.0 (binding) | yes (py3 wheel, bundles libtdjson) | Mature, but its high-level `Client` owns the receive loop and event model; we need a purpose-built dispatcher (per-request timeouts, cancellation, multi-instance, job pipeline integration). Using it only for its bundled `.so` is fragile. |
| `tdjson` PyPI (AYMENJD) 1.8.71 (binding) | yes (`cp314` wheel, bundles libtdjson 1.8.71) | Thin C++ ext over the *global* `td_receive` API — one receive for all clients, must not be called from two threads. Adequate but less isolation than per-client handles. |
| **Own ctypes layer (chosen)** | **guaranteed (ctypes is stdlib)** | Full control of dispatcher/update routing/timeouts/shutdown, no binding lock-in, works with *any* `libtdjson` (system build, pip-bundled, or `TDLIB_LIBRARY_PATH`). |

The native library itself is resolved at runtime in this order
(`tdlib_native.resolve_library_path`):

1. `TDLIB_LIBRARY_PATH` env var (explicit override),
2. system library via `ctypes.util.find_library("tdjson")`,
3. the bundled `libtdjson` shipped in the `tdjson` pip wheel (any Python version),
4. the bundled `libtdjson` shipped in the `python-telegram` wheel,
5. conventional build paths (`./tdlib/lib`, `/usr/local/lib`, `/usr/lib`).

A clear error with install instructions is raised when none is found.

Validated in this environment: TDLib **1.8.71** loads, creates clients, performs
`setTdlibParameters` → `authorizationStateWaitPhoneNumber`, correlates `@extra`
on success *and* error responses, and shuts down cleanly (`close` →
`authorizationStateClosed`).

## 5. Rollback procedure

```bash
git checkout main            # or any pre-migration commit on omega
# Telethon session files were never touched:
ls copybot_session.session   # still present if you had logged in before
pip install -r requirements.txt   # reinstates telethon
```

TDLib's new database directories can be deleted at any time (`data/tdlib/`) —
they contain only the new engine's state. Old checkpoints in
`_pvtcopydl_tmp/` are never removed by the new code, so a rollback can still
resume jobs with the old code.

## 6. Implementation phases (tracked in this document)

- [x] Phase 1 — repository audit, feature map, Telethon usage map, bug list.
- [x] Phase 2 — architecture + integration decision (this document).
- [ ] Phase 3 — implementation (TDLib stack, pipeline, job manager, bot rewrite).
- [ ] Phase 4 — tests, static analysis, startup/shutdown/interrupt verification.
- [ ] Phase 5 — final review (dead code, security, leaks, races, docs).
