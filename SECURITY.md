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

## Session Files

Telethon session files (`*.session`) contain authentication keys that grant
full access to your Telegram account. Treat them like passwords:

- Never share session files
- Never commit them to version control
- Delete them if compromised
- Use `.logout` command to invalidate sessions
