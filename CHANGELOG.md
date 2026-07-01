# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `.del` command for bulk-deleting messages by range
- Professional terminal-style progress bar with Unicode block characters
- Rate-limited progress updates (2–3 second intervals)
- Automatic FloodWait handling with retry
- MIT License
- Comprehensive README with badges
- Security policy
- Contributing guidelines

### Changed
- Command prefix changed from `/` to `.` (dot) for all userbot commands
- Bot `/start` command unchanged (standard Telegram convention)
- Improved progress reporting with spinner animations
- Optimized Telegram API usage to avoid rate limits

### Fixed
- Session file WAL mode for overlayfs compatibility

## [1.0.0] - 2025-01-01

### Added
- Initial release
- `.pvtcopy` — server-side message copying
- `.pvtcopydl` — download-upload pipeline with live progress
- `.pvtcopydlcancel` — cancel running jobs
- `.adest` / `.showdest` — destination channel management
- `.logout` — session cleanup
- Bot-driven live progress bar with refresh/cancel buttons
- Resumable downloads with checkpoint system
