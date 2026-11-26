# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly:

- **Do NOT** open a public GitHub issue for security vulnerabilities.
- Email the maintainer directly or use GitHub's private vulnerability reporting.

## Sensitive Data

This bot handles Telegram API credentials and session files. **Never commit:**

- `.env` files with real API keys
- `*.session` files (contain authentication data)
- `config.json` with real channel IDs

Always use `.env.example` and `config.example.json` as templates.

## Session & Runtime Data

- **`data/tdlib/`** contains TDLib account databases and key material that
  grant full access to the authenticated Telegram account. Treat it like a
  password.
- **Legacy Telethon `*.session` files** (from pre-migration installs) also
  contain authentication keys. The migration never deletes them; treat them
  as sensitive and do not commit them.
- Never share session data, never commit it, and restrict filesystem
  permissions on `data/` and `.env`.
- Use `.logout` to log the account out of Telegram; remove `data/tdlib/`
  only when you intentionally want a fresh login.
