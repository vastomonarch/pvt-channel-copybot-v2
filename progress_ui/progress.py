"""Unicode progress bar rendering."""

from progress_ui.constants import BLOCK_CHARS


def progress_bar(percent: float, width: int = 20) -> str:
    """Render a progress bar using eighth-block Unicode characters.

    Supports sub-character precision using partial blocks:
    ' '  '▏'  '▎'  '▍'  '▌'  '▋'  '▊'  '▉'  '█'

    Args:
        percent: Progress value from 0.0 to 100.0.
        width: Number of character columns for the bar.

    Returns:
        String of exactly `width` characters representing the bar.
    """
    clamped = max(0.0, min(100.0, percent))

    # Convert to eighth-blocks (8 sub-blocks per character cell)
    total_eighths = int((clamped / 100.0) * width * 8)
    full_blocks = total_eighths // 8
    remainder = total_eighths % 8

    parts: list[str] = []

    for i in range(width):
        if i < full_blocks:
            parts.append(BLOCK_CHARS[8])  # █
        elif i == full_blocks and remainder > 0:
            parts.append(BLOCK_CHARS[remainder])
        else:
            parts.append(BLOCK_CHARS[0])  # ' '

    return "".join(parts)
