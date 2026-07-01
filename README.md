<div align="center">

# 📋 Private Channel Copy Bot

**A Telethon-based userbot for copying messages from private Telegram channels**

*Strip forwarded-from tags • Live progress tracking • Bulk delete*

---

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon](https://img.shields.io/badge/Telethon-1.36+-purple.svg)](https://docs.telethon.dev/)
[![GitHub Stars](https://img.shields.io/github/stars/vastomonarch/pvt-channel-copybot-v2?style=social)](https://github.com/vastomonarch/pvt-channel-copybot-v2)
[![Last Commit](https://img.shields.io/github/last-commit/vastomonarch/pvt-channel-copybot-v2)](https://github.com/vastomonarch/pvt-channel-copybot-v2/commits)
[![Issues](https://img.shields.io/github/issues/vastomonarch/pvt-channel-copybot-v2)](https://github.com/vastomonarch/pvt-channel-copybot-v2/issues)
[![Status](https://img.shields.io/badge/status-stable-brightgreen.svg)](https://github.com/vastomonarch/pvt-channel-copybot-v2)

</div>

---

## ✨ Features

- **Message Copying** — Copy messages between channels without "Forwarded from" tags
- **Download Pipeline** — Download → Upload → Verify with resumable checkpoints
- **Bulk Delete** — Delete message ranges with a single command
- **Live Progress** — Real-time progress bars with speed, ETA, and spinners
- **Bot Integration** — Separate bot client for progress updates (keeps user account clean)
- **Error Recovery** — Automatic retry on FloodWait, resumable after crashes
- **Private Channels** — Works with private channels where forwarding is disabled

## 📸 Preview

```
╭──────────────────────────────────────╮
│ ⠹ ⬇ Downloading                     │
│ 📄 video_edit.mov                    │
├──────────────────────────────────────┤
│ ████████████▉▉▉          67.3%       │
│                                      │
│ 📦  1.25 GB / 2.00 GB               │
│ ⚡  12.7 MB/s                        │
│ ⏳  ETA      1m 05s                  │
│ 🕒  Elapsed  2m 30s                  │
╰──────────────────────────────────────╯
```

## 🚀 Quick Start

### Prerequisites

- Python 3.10 or higher
- Telegram API credentials ([get from my.telegram.org](https://my.telegram.org/apps))
- A Telegram bot token (create via [@BotFather](https://t.me/BotFather))

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/pvt-channel-copybot-v2.git
cd pvt-channel-copybot-v2

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials
```

### Configuration

Edit `.env` with your credentials:

```env
API_ID=12345678
API_HASH=your_api_hash_here
BOT_TOKEN=your_bot_token_here
LOG_LEVEL=INFO
```

### Running

```bash
python bot.py
```

On first run, you'll be prompted to log in to your Telegram account. The bot will save a session file for future runs.

## 📖 Commands

All commands use `.` (dot) prefix. The bot's `/start` uses slash (standard Telegram convention).

| Command | Description |
|---------|-------------|
| `.help` | Show all available commands |
| `.pvtcopy <start> <end>` | Server-side copy (strips forwarded-from) |
| `.pvtcopydl <start> <end>` | Download → Upload pipeline with live progress |
| `.pvtcopydlcancel [key]` | Cancel a running copy job |
| `.del <start_link> <end_link>` | Bulk-delete messages in range |
| `.adest <channel_id>` | Add a destination channel |
| `.showdest` | View and select destination channels |
| `.logout` | Disconnect and delete session |

## 💡 Usage Examples

### Copy Messages

```bash
# Copy messages from a private channel
.pvtcopy https://t.me/c/1234567890/100 https://t.me/c/1234567890/200

# Download-upload pipeline (for channels with forwarding disabled)
.pvtcopydl https://t.me/c/1234567890/100 https://t.me/c/1234567890/200
```

### Bulk Delete

```bash
# Delete messages 100 through 200
.del https://t.me/c/1234567890/100 https://t.me/c/1234567890/200

# Works with public channels too
.del https://t.me/examplechannel/100 https://t.me/examplechannel/200
```

### Manage Destinations

```bash
# Add a destination channel
.adest 1234567890

# View and select destinations
.showdest
```

## 📁 Project Structure

```
pvt_channel_copybot_v2/
├── bot.py                  # Main bot logic and handlers
├── trigger_pvtcopydl.py    # Headless trigger for testing
├── requirements.txt        # Python dependencies
├── .env.example           # Environment template
├── config.example.json    # Config template
├── LICENSE                # MIT License
├── README.md              # This file
├── CHANGELOG.md           # Version history
├── CONTRIBUTING.md        # Contribution guidelines
├── SECURITY.md            # Security policy
└── CODE_OF_CONDUCT.md     # Code of conduct
```

## 🔧 How It Works

1. **Two Clients** — The bot runs two Telethon clients:
   - **User Client** — Your Telegram account (handles copying/deleting)
   - **Bot Client** — A separate bot for sending progress updates

2. **Message Pipeline** — For `.pvtcopydl`:
   - Fetch messages from source
   - Download media to temp directory
   - Upload to destination
   - Verify upload integrity
   - Clean up temp files
   - Checkpoint after each message (resumable)

3. **Progress Tracking** — Live updates via the bot client:
   - Unicode progress bar with spinner
   - Download/upload speed
   - ETA calculation
   - Refresh and cancel buttons

## ⚠️ Limitations

- Telegram API rate limits apply (FloodWait handled automatically)
- Maximum file size: 2 GB per file
- Service messages (joins, leaves) are skipped
- Bot progress requires user to `/start` the bot first
- Some channels may restrict message deletion even for admins

## ❓ FAQ

**Q: Why two Telegram clients?**
A: The bot client handles progress updates so your user account doesn't get spammed with message edits.

**Q: Can I resume a crashed download?**
A: Yes! `.pvtcopydl` checkpoints after each message. Re-issue the same command to resume.

**Q: Why dot prefix instead of slash?**
A: This is a userbot (runs on your account). Dot prefix is the convention for userbots to avoid conflicts with normal bot commands.

**Q: Does `.del` work on channels I don't own?**
A: You need admin rights with message deletion permission in the target channel.

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📄 License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

## 🙏 Credits

- [Telethon](https://docs.telethon.dev/) — Telegram client library
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — Bot API wrapper (for progress rendering)

---

<div align="center">

**If this project helps you, consider giving it a ⭐!**

---

📦 [View Repository](https://github.com/vastomonarch/pvt-channel-copybot-v2)

</div>
