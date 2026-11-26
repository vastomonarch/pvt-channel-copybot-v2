"""Dashboard renderer — builds terminal-style box layouts for copy/delete ops."""

from __future__ import annotations

import html

from progress_ui.constants import (
    DEFAULT_BOX_STYLE,
    DEFAULT_BOX_WIDTH,
    ICON_CANCEL,
    ICON_CHANNEL,
    ICON_COMPLETE,
    ICON_COPY,
    ICON_DELETE,
    ICON_DOWNLOAD,
    ICON_ELAPSED,
    ICON_ETA,
    ICON_FILE,
    ICON_SIZE,
    ICON_SPEED,
    ICON_UPLOAD,
    BoxStyle,
)
from progress_ui.formatter import format_percent, format_size, format_speed, format_time
from progress_ui.progress import progress_bar
from progress_ui.spinner import Spinner


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


def box_line(content: str, width: int, style: BoxStyle | None = None) -> str:
    """Wrap content in box left wall, padded to width (no right border)."""
    s = style or DEFAULT_BOX_STYLE
    display_content = content[:width] if len(content) > width else content
    pad = max(0, width - len(display_content))
    return f"{s.v} {display_content}{' ' * pad}"


def box_top(width: int, style: BoxStyle | None = None) -> str:
    """Top border: ╭──────────────────────────╮"""
    s = style or DEFAULT_BOX_STYLE
    return f"{s.tl}{s.h * width}{s.tr}"


def box_bottom(width: int, style: BoxStyle | None = None) -> str:
    """Bottom border: ╰──────────────────────────╯"""
    s = style or DEFAULT_BOX_STYLE
    return f"{s.bl}{s.h * width}{s.br}"


def box_separator(width: int, style: BoxStyle | None = None) -> str:
    """Middle separator: ├──────────────────────────┤"""
    s = style or DEFAULT_BOX_STYLE
    return f"{s.lt}{s.h * width}{s.rt}"


# ── Copy Progress Dashboard ─────────────────────────────────────────


def render_copy_progress(
    *,
    spinner: Spinner,
    mode: str,
    job_key: str,
    current_msg: int,
    total: int,
    downloaded: int,
    uploaded: int,
    skipped: int,
    errors: int,
    speed: float = 0.0,
    eta: float = 0.0,
    elapsed: float = 0.0,
    filename: str = "",
    file_current: int = 0,
    file_total: int = 0,
    start_msg: int = 0,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render the copy/download progress dashboard.

    When file_current/file_total are provided (>0), shows file-level
    progress (bytes transferred) instead of message-count progress.

    Returns HTML-formatted string with <pre> wrapping.
    """
    spin = spinner.next()

    # Determine mode display
    if mode == "uploading":
        arrow = ICON_UPLOAD
        action = "Uploading"
    elif mode == "copy":
        arrow = ICON_COPY
        action = "Copying"
    elif mode == "downloading":
        arrow = ICON_DOWNLOAD
        action = "Downloading"
    else:
        arrow = ICON_DOWNLOAD
        action = "Downloading"

    # File-level progress (active transfer)
    use_file_progress = file_total > 0
    if use_file_progress:
        pct = min(100.0, (file_current / max(1, file_total)) * 100.0)
        size_line = f"{ICON_SIZE}  {format_size(file_current)} / {format_size(file_total)}"
    else:
        # Message-count progress (between files)
        done_count = max(0, current_msg - start_msg) if start_msg else 0
        pct = (done_count / max(1, total)) * 100.0
        size_line = f"{ICON_SIZE}  msg {current_msg} / {total}"

    bar = progress_bar(pct, width=20)
    pct_str = format_percent(pct)

    # Header
    header = f"{spin} {arrow} {action}"

    # File info line
    file_line = f"{ICON_FILE} {filename}" if filename else f"{ICON_FILE} msg {current_msg}"

    # Stats
    speed_line = f"{ICON_SPEED}  {format_speed(speed)}" if speed > 0 else f"{ICON_SPEED}  —"
    eta_line = f"{ICON_ETA}  ETA      {format_time(eta)}" if eta > 0 else f"{ICON_ETA}  ETA      —"
    elapsed_line = f"{ICON_ELAPSED}  Elapsed  {format_time(elapsed)}"

    # Counters
    counter_line = f"DL: {downloaded} · UL: {uploaded} · skip: {skipped} · err: {errors}"

    # Assemble box
    lines = [
        box_top(width),
        box_line(header, width),
        box_line(file_line, width),
        box_separator(width),
        box_line(f"{bar}  {pct_str}", width),
        box_line("", width),
        box_line(size_line, width),
        box_line(speed_line, width),
        box_line(eta_line, width),
        box_line(elapsed_line, width),
        box_separator(width),
        box_line(counter_line, width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


def render_copy_completed(
    *,
    mode: str,
    job_key: str,
    total: int,
    downloaded: int,
    uploaded: int,
    skipped: int,
    errors: int,
    elapsed: float,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render the copy completion screen."""
    bar = progress_bar(100.0, width=20)
    if mode in ("upload", "uploading"):
        action = "Uploaded"
    elif mode == "copy":
        action = "Copied"
    else:
        action = "Downloaded"

    lines = [
        box_top(width),
        box_line(f"{ICON_COMPLETE} {action}", width),
        box_line(f"job [{job_key[:8]}]", width),
        box_separator(width),
        box_line(bar, width),
        box_line("", width),
        box_line(f"{ICON_SIZE}  {total} messages", width),
        box_line(f"DL: {downloaded} · UL: {uploaded}", width),
        box_line(f"skip: {skipped} · err: {errors}", width),
        box_line(f"{ICON_ELAPSED}  Completed in {format_time(elapsed)}", width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


# ── Delete Progress Dashboard ───────────────────────────────────────


def render_delete_progress(
    *,
    spinner: Spinner,
    channel: str,
    start_id: int,
    end_id: int,
    deleted: int,
    skipped: int,
    failed: int,
    current_id: int,
    elapsed: float = 0.0,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render the bulk delete progress dashboard.

    Returns HTML-formatted string with <pre> wrapping.
    """
    spin = spinner.next()
    total = end_id - start_id + 1
    done = max(0, current_id - start_id)
    pct = (done / max(1, total)) * 100.0
    bar = progress_bar(pct, width=20)
    pct_str = format_percent(pct)

    # Header
    header = f"{spin} {ICON_DELETE} Deleting"

    # Channel (truncate if needed)
    ch_display = channel[:28] if len(channel) > 28 else channel
    ch_line = f"{ICON_CHANNEL} {ch_display}"

    # Range
    range_line = f"Range  {start_id} → {end_id}  ({total} ids)"

    # Stats
    deleted_line = f"Deleted  {deleted}"
    skipped_line = f"Skipped  {skipped}"
    failed_line = f"Failed   {failed}"
    elapsed_line = f"{ICON_ELAPSED}  Elapsed  {format_time(elapsed)}"

    # Progress
    progress_line = f"Current  {current_id} / {end_id}"

    lines = [
        box_top(width),
        box_line(header, width),
        box_line(ch_line, width),
        box_separator(width),
        box_line(f"{bar}  {pct_str}", width),
        box_line("", width),
        box_line(range_line, width),
        box_line(progress_line, width),
        box_line("", width),
        box_line(deleted_line, width),
        box_line(skipped_line, width),
        box_line(failed_line, width),
        box_separator(width),
        box_line(elapsed_line, width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


def render_delete_completed(
    *,
    channel: str,
    start_id: int,
    end_id: int,
    deleted: int,
    skipped: int,
    failed: int,
    elapsed: float,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render the delete completion screen."""
    total = end_id - start_id + 1
    bar = progress_bar(100.0, width=20)
    ch_display = channel[:28] if len(channel) > 28 else channel

    lines = [
        box_top(width),
        box_line(f"{ICON_COMPLETE} Deletion Complete", width),
        box_line(f"{ICON_CHANNEL} {ch_display}", width),
        box_separator(width),
        box_line(bar, width),
        box_line("", width),
        box_line(f"Range  {start_id} → {end_id}  ({total} ids)", width),
        box_line("", width),
        box_line(f"Deleted  {deleted}", width),
        box_line(f"Skipped  {skipped}", width),
        box_line(f"Failed   {failed}", width),
        box_separator(width),
        box_line(f"{ICON_ELAPSED}  Completed in {format_time(elapsed)}", width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


def render_delete_failed(
    *,
    channel: str,
    start_id: int,
    end_id: int,
    deleted: int,
    skipped: int,
    failed: int,
    elapsed: float,
    error: str,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render the delete failure screen."""
    total = end_id - start_id + 1
    ch_display = channel[:28] if len(channel) > 28 else channel

    lines = [
        box_top(width),
        box_line(f"{ICON_CANCEL} Deletion Failed", width),
        box_line(f"{ICON_CHANNEL} {ch_display}", width),
        box_separator(width),
        box_line(f"Range  {start_id} → {end_id}  ({total} ids)", width),
        box_line("", width),
        box_line(f"Deleted  {deleted}", width),
        box_line(f"Skipped  {skipped}", width),
        box_line(f"Failed   {failed}", width),
        box_separator(width),
        box_line(f"Error: {error[:30]}", width),
        box_line(f"{ICON_ELAPSED}  Elapsed {format_time(elapsed)}", width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


# ── Simple Copy Progress (server-side, no download) ─────────────────


def render_simple_copy_progress(
    *,
    spinner: Spinner,
    channel: str,
    start_id: int,
    end_id: int,
    copied: int,
    skipped: int,
    errors: int,
    current_id: int,
    elapsed: float = 0.0,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render progress for server-side copy (no download/upload phases)."""
    spin = spinner.next()
    total = end_id - start_id + 1
    done = max(0, current_id - start_id)
    pct = (done / max(1, total)) * 100.0
    bar = progress_bar(pct, width=20)
    pct_str = format_percent(pct)

    header = f"{spin} ⬇ Copying"
    ch_display = channel[:28] if len(channel) > 28 else channel
    ch_line = f"{ICON_CHANNEL} {ch_display}"
    range_line = f"Range  {start_id} → {end_id}  ({total} msgs)"
    progress_line = f"Current  {current_id} / {end_id}"
    copied_line = f"Copied  {copied}"
    skipped_line = f"Skipped  {skipped}"
    errors_line = f"Errors  {errors}"
    elapsed_line = f"{ICON_ELAPSED}  Elapsed  {format_time(elapsed)}"

    lines = [
        box_top(width),
        box_line(header, width),
        box_line(ch_line, width),
        box_separator(width),
        box_line(f"{bar}  {pct_str}", width),
        box_line("", width),
        box_line(range_line, width),
        box_line(progress_line, width),
        box_line("", width),
        box_line(copied_line, width),
        box_line(skipped_line, width),
        box_line(errors_line, width),
        box_separator(width),
        box_line(elapsed_line, width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


def render_simple_copy_completed(
    *,
    channel: str,
    start_id: int,
    end_id: int,
    copied: int,
    skipped: int,
    errors: int,
    elapsed: float,
    width: int = DEFAULT_BOX_WIDTH,
) -> str:
    """Render completion screen for server-side copy."""
    total = end_id - start_id + 1
    bar = progress_bar(100.0, width=20)
    ch_display = channel[:28] if len(channel) > 28 else channel

    lines = [
        box_top(width),
        box_line(f"{ICON_COMPLETE} Copy Complete", width),
        box_line(f"{ICON_CHANNEL} {ch_display}", width),
        box_separator(width),
        box_line(bar, width),
        box_line("", width),
        box_line(f"Range  {start_id} → {end_id}  ({total} msgs)", width),
        box_line("", width),
        box_line(f"Copied  {copied}", width),
        box_line(f"Skipped  {skipped}", width),
        box_line(f"Errors  {errors}", width),
        box_separator(width),
        box_line(f"{ICON_ELAPSED}  Completed in {format_time(elapsed)}", width),
        box_bottom(width),
    ]

    body = escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"
