# Contributing

Contributions are welcome! Here's how to get started.

## Development Setup

1. Fork and clone the repository
2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your credentials
5. Run the bot:
   ```bash
   python bot.py
   ```

## Code Style

- Follow PEP 8
- Use type hints where practical
- Keep functions focused and under ~50 lines
- Add docstrings for public functions
- Use `log` (the module logger) instead of `print()`

## Pull Requests

1. Create a feature branch from `main`
2. Make your changes
3. Test thoroughly (the bot should start and respond to `.help`)
4. Commit with a clear message
5. Open a pull request

## Reporting Issues

- Use GitHub Issues
- Include Python version and Telethon version
- Describe steps to reproduce
- Include relevant log output (redact any tokens/keys)
