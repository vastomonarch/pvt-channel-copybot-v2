"""UI constants — characters, colors, and layout."""

# ── Spinner Frames ──────────────────────────────────────────────────

SPINNER_FRAMES: list[str] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# ── Progress Bar Blocks ─────────────────────────────────────────────

# Eighth-block characters for fine-grained progress rendering
BLOCK_CHARS: list[str] = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]

# ── Box Drawing ─────────────────────────────────────────────────────


class BoxStyle:
    """Box-drawing character set."""

    __slots__ = ("tl", "tr", "bl", "br", "h", "v", "lt", "rt")

    def __init__(
        self,
        tl: str = "╭",
        tr: str = "╮",
        bl: str = "╰",
        br: str = "╯",
        h: str = "─",
        v: str = "│",
        lt: str = "├",
        rt: str = "┤",
    ) -> None:
        self.tl = tl
        self.tr = tr
        self.bl = bl
        self.br = br
        self.h = h
        self.v = v
        self.lt = lt
        self.rt = rt


DEFAULT_BOX_STYLE = BoxStyle()

# ── Layout ──────────────────────────────────────────────────────────

DEFAULT_BOX_WIDTH = 34
BOX_WIDTH = DEFAULT_BOX_WIDTH

# ── Emoji Icons ─────────────────────────────────────────────────────

ICON_UPLOAD = "⬆"
ICON_DOWNLOAD = "⬇"
ICON_FILE = "📄"
ICON_SIZE = "📦"
ICON_SPEED = "⚡"
ICON_ETA = "⏳"
ICON_ELAPSED = "🕒"
ICON_COMPLETE = "✅"
ICON_CANCEL = "❌"
ICON_DELETE = "🗑"
ICON_CHANNEL = "📢"

# ── Transfer Modes ──────────────────────────────────────────────────

MODE_UPLOAD = "upload"
MODE_DOWNLOAD = "download"
MODE_DELETE = "delete"
