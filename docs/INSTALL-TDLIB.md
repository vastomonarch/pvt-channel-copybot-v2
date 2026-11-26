# Installing TDLib (`libtdjson`)

The bot talks to Telegram exclusively through **TDLib** — the official
Telegram database library — using its JSON interface (`tdjson`) via
`ctypes` bindings in `tdlib_native.py`.

You therefore need **the shared library only** (`libtdjson.so` /
`libtdjson.dylib`); no Python binding package is imported, and no compiled
extension of our own is built. Any Python ≥ 3.10 works (including 3.14,
because `ctypes` ships with the standard library).

The library is resolved in this order (first hit wins):

1. `TDLIB_LIBRARY_PATH` from `.env` — an explicit file path;
2. `ctypes.util.find_library("tdjson")` — the system loader;
3. the shared object bundled inside a `tdjson` pip wheel;
4. the shared object bundled inside a `python-telegram` pip wheel;
5. `./tdlib/lib/libtdjson.so` inside the checkout.

If none resolves, startup fails with a clear `TDLibNotFoundError`.

---

## Option A — distro package (fastest)

```bash
# Debian / Ubuntu
sudo apt install libtdjson-dev          # or: libtdjson0

# Fedora / RHEL
sudo dnf install tdjson

# Arch
sudo pacman -S tdlib

# Alpine
sudo apk add tdlib-dev
```

Verify:

```bash
python -c "from tdlib_native import resolve_library_path; print(resolve_library_path())"
```

> Not every distro ships `tdjson`; if the package is missing, use option B
> or C.

## Option B — a pip wheel that bundles the library

```bash
venv/bin/pip install tdjson          # bundles libtdjson in tdjson.libs/
# or
venv/bin/pip install python-telegram # bundles it in telegram/lib/
```

The bot **never imports these packages** — it only looks for the shared
library they ship. Uninstalling them again is safe as long as some other
provider remains.

Convenience flag on the installer:

```bash
sudo ./deploy/install.sh --with-tdjson-wheel
```

## Option C — build from source (most control)

```bash
sudo apt install build-essential cmake gperf libssl-dev zlib1g-dev

git clone https://github.com/tdlib/td.git
cd td
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$HOME/tdlib-install" ..
cmake --build . --target install -j"$(nproc)"      # takes ~10–20 min
```

Then point the bot at it (`.env`):

```bash
TDLIB_LIBRARY_PATH=$HOME/tdlib-install/lib/libtdjson.so
```

The build directory can be deleted afterwards; only `libtdjson.so` (and its
neighbouring `libtdjson*.so.*` symlinks) must remain reachable.

---

## Verification

```bash
# 1. the library resolves
venv/bin/python -c "from tdlib_native import resolve_library_path; print(resolve_library_path())"

# 2. it actually loads and speaks JSON (offline, no credentials needed)
venv/bin/python tests/test_payload_shapes.py
#    → "33 passed, 0 failed"
```

The second command creates real TDLib clients and validates every request
payload the bot sends, so it proves the library, the ctypes bindings and
the payload shapes all work together — without logging in.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `TDLibNotFoundError: Failed to load libtdjson from …` | Path wrong, wrong architecture (x86_64 vs aarch64), or the file is not a shared object. Re-check `TDLIB_LIBRARY_PATH`. |
| `undefined symbol: …` when loading | The library was built against a newer glibc than the runtime host. Rebuild on the target host (option C) or install a matching package. |
| `find_library('tdjson')` returns `None` | No dev package installed — use option B or C, or set `TDLIB_LIBRARY_PATH`. |
| Login loops on `authorizationStateWaitPhoneNumber` with no TTY | Interactive login needs a terminal: run `./deploy/install.sh --login` (or `python bot.py`) once, then start the service. |
| `another copybot instance is already running` | By design: TDLib databases are single-process. Stop the other instance (`systemctl stop copybot`). |
| Native TDLib messages in the log | Raise/lower `TDLIB_LOG_VERBOSITY` (0–4); the file log is `data/logs/tdlib_native.log`. |

## Security notes

* The library is loaded into the bot process — treat it like any other
  system dependency and keep it updated.
* `API_HASH`, `BOT_TOKEN`, phone codes and 2FA passwords are never logged
  and never written to disk by this project (only `.env`, which is
  git-ignored, holds `API_ID`/`API_HASH`/`BOT_TOKEN`).
* TDLib's own database encryption can be enabled with `TDLIB_DB_KEY` in
  `.env`.
