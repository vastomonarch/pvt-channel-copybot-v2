"""Progress UI components for Telegram userbot operations.

Adapted from progressbar_demo/ui/ for Telethon integration.
Provides terminal-style dashboards for copy/delete operations.
"""

from progress_ui.constants import (
    BLOCK_CHARS,
    BOX_WIDTH,
    SPINNER_FRAMES,
    BoxStyle,
    DEFAULT_BOX_STYLE,
)
from progress_ui.formatter import (
    format_percent,
    format_size,
    format_speed,
    format_time,
)
from progress_ui.progress import progress_bar
from progress_ui.renderer import (
    box_bottom,
    box_line,
    box_separator,
    box_top,
    escape_html,
    render_copy_completed,
    render_copy_progress,
    render_delete_completed,
    render_delete_failed,
    render_delete_progress,
    render_simple_copy_completed,
    render_simple_copy_progress,
)
from progress_ui.spinner import Spinner

__all__ = [
    "BoxStyle",
    "DEFAULT_BOX_STYLE",
    "BOX_WIDTH",
    "BLOCK_CHARS",
    "SPINNER_FRAMES",
    "format_size",
    "format_speed",
    "format_time",
    "format_percent",
    "Spinner",
    "progress_bar",
    "escape_html",
    "box_line",
    "box_top",
    "box_bottom",
    "box_separator",
]
