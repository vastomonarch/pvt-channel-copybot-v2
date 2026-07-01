"""Format bytes, speeds, and times into human-readable strings."""

# ── Size Formatting ─────────────────────────────────────────────────

_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def format_size(size_bytes: float) -> str:
    """Convert bytes to human-readable size.

    Examples: 1.25 GB, 590.0 KB, 42 B
    """
    if size_bytes < 0:
        size_bytes = 0.0

    idx = 0
    size = float(size_bytes)

    while size >= 1024.0 and idx < len(_SIZE_UNITS) - 1:
        size /= 1024.0
        idx += 1

    unit = _SIZE_UNITS[idx]

    if size >= 100:
        return f"{size:.0f} {unit}"
    if size >= 10:
        return f"{size:.1f} {unit}"
    return f"{size:.2f} {unit}"


# ── Speed Formatting ────────────────────────────────────────────────

_SPEED_UNITS = ["B/s", "KB/s", "MB/s", "GB/s"]


def format_speed(speed_bps: float) -> str:
    """Convert bytes/sec to human-readable speed.

    Examples: 12.7 MB/s, 3.14 KB/s
    """
    if speed_bps < 0:
        speed_bps = 0.0

    idx = 0
    speed = float(speed_bps)

    while speed >= 1024.0 and idx < len(_SPEED_UNITS) - 1:
        speed /= 1024.0
        idx += 1

    unit = _SPEED_UNITS[idx]

    if speed >= 100:
        return f"{speed:.0f} {unit}"
    if speed >= 10:
        return f"{speed:.1f} {unit}"
    return f"{speed:.2f} {unit}"


# ── Time Formatting ─────────────────────────────────────────────────


def format_time(seconds: float) -> str:
    """Convert seconds to human-readable duration.

    Examples: 20 s, 1m 05s, 12m 30s
    """
    seconds = max(0, int(seconds))

    if seconds < 60:
        return f"{seconds} s"

    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}m {secs:02d}s"


def format_percent(percent: float, decimals: int = 1) -> str:
    """Format percentage with fixed width.

    Examples: 43.7%, 100.0%, 0.5%
    """
    clamped = max(0.0, min(100.0, percent))
    return f"{clamped:.{decimals}f}%"
