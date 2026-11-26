#!/usr/bin/env bash
#
# install.sh — install / update the TDLib copy bot on a Linux host.
#
#   sudo ./deploy/install.sh                 # venv + deps + TDLib check
#   sudo ./deploy/install.sh --login         # … then run the interactive
#                                            #    Telegram login once
#   sudo ./deploy/install.sh --systemd       # … then install the service
#   sudo ./deploy/install.sh --systemd --login
#
# Safe to re-run: it never overwrites .env, config.json, data/ or any
# existing TDLib session.  Idempotent by design (used for upgrades too).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/venv"
UNIT_SRC="$ROOT/deploy/copybot.service"
UNIT_DST="/etc/systemd/system/copybot.service"
SERVICE_USER="${COPYBOT_USER:-copybot}"

WITH_SYSTEMD=0
WITH_LOGIN=0
WITH_TDJSON_WHEEL=0
SKIP_TDLIB_CHECK=0

usage() {
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

for arg in "$@"; do
    case "$arg" in
        --systemd) WITH_SYSTEMD=1 ;;
        --login) WITH_LOGIN=1 ;;
        --with-tdjson-wheel) WITH_TDJSON_WHEEL=1 ;;
        --skip-tdlib-check) SKIP_TDLIB_CHECK=1 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $arg" >&2; usage 1 ;;
    esac
done

say()  { printf '\033[36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. Python ───────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || die "python3 not found"
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python >= 3.10 required (found $PYVER)"
say "python $PYVER"

# ── 2. Virtualenv + dependencies ───────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
    say "creating virtualenv at $VENV"
    python3 -m venv "$VENV"
fi
say "installing Python dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$ROOT/requirements.txt"

# ── 3. Optional system tools (file splitting) ───────────────────────────────
for tool in split; do
    command -v "$tool" >/dev/null 2>&1 \
        || warn "'$tool' not found — binary file splitting will fail"
done
command -v ffmpeg >/dev/null 2>&1 \
    || warn "ffmpeg not found — large video splitting will fall back to binary split"
command -v 7z >/dev/null 2>&1 \
    || warn "7z not found — archive volume splitting will fall back to binary split"

# ── 4. TDLib ────────────────────────────────────────────────────────────────
if [ "$SKIP_TDLIB_CHECK" -eq 0 ]; then
    if [ "$WITH_TDJSON_WHEEL" -eq 1 ]; then
        say "installing the 'tdjson' wheel (bundles libtdjson)"
        "$VENV/bin/pip" install --quiet tdjson
    fi
    if ! "$VENV/bin/python" -c \
        "from tdlib_native import resolve_library_path; print(resolve_library_path())" \
        2>/dev/null; then
        cat >&2 <<'EOF'
[install] libtdjson was NOT found.
          See docs/INSTALL-TDLIB.md for the three ways to get it:
            1. distro package (apt/dnf/pacman), or
            2. ./deploy/install.sh --with-tdjson-wheel, or
            3. build from source and set TDLIB_LIBRARY_PATH in .env
EOF
        die "TDLib is required"
    fi
    say "libtdjson: $("$VENV/bin/python" -c \
        "from tdlib_native import resolve_library_path; print(resolve_library_path())")"
fi

# ── 5. Configuration (never overwrites existing files) ──────────────────────
if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    warn ".env created from .env.example — EDIT IT (API_ID / API_HASH / BOT_TOKEN)"
fi
if [ ! -f "$ROOT/config.json" ] && [ -f "$ROOT/config.example.json" ]; then
    cp "$ROOT/config.example.json" "$ROOT/config.json"
fi
mkdir -p "$ROOT/data" "$ROOT/_pvtcopydl_tmp"

# ── 6. Optional service user ────────────────────────────────────────────────
if [ "$WITH_SYSTEMD" -eq 1 ]; then
    if [ "$(id -u)" -ne 0 ]; then
        die "--systemd requires root (sudo ./deploy/install.sh --systemd)"
    fi
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        say "creating service user '$SERVICE_USER'"
        useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin \
            "$SERVICE_USER"
    fi
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$ROOT/data" "$ROOT/_pvtcopydl_tmp"
    [ -f "$ROOT/.env" ] && chown "$SERVICE_USER":"$SERVICE_USER" "$ROOT/.env"

    say "installing $UNIT_DST"
    sed -e "s|/opt/pvt-channel-copybot-v2|$ROOT|g" \
        -e "s|^User=.*|User=$SERVICE_USER|" \
        -e "s|^Group=.*|Group=$SERVICE_USER|" \
        "$UNIT_SRC" > "$UNIT_DST"
    systemctl daemon-reload
    systemctl enable copybot.service >/dev/null 2>&1 || true
    say "service installed and enabled (not started — log in first)"
fi

# ── 7. Interactive login (once, needs a TTY) ────────────────────────────────
if [ "$WITH_LOGIN" -eq 1 ]; then
    say "starting interactive Telegram login (phone / code / 2FA)"
    "$VENV/bin/python" "$ROOT/bot.py" || die "login failed"
    say "login complete — TDLib session stored under data/tdlib/user"
fi

cat <<'EOF'

Done.  Next steps:
  1. Edit .env            → API_ID, API_HASH, BOT_TOKEN (and DESTINATION)
  2. Log in once          → ./deploy/install.sh --login
                            (Telegram asks for phone/code/2FA in this terminal;
                             the session is then reused by the service)
  3. Start the service    → sudo systemctl start copybot
                            journalctl -u copybot -f
  4. In Telegram          → send the bot /start, then issue .pvtcopydl …

Upgrade later with:  git pull && ./deploy/install.sh --systemd
Rollback with:       git checkout <previous-tag-or-commit> && \
                     ./deploy/install.sh --systemd && sudo systemctl restart copybot
EOF
