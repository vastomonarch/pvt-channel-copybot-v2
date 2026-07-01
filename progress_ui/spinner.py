"""Terminal spinner animation."""

from progress_ui.constants import SPINNER_FRAMES


class Spinner:
    """Rotating terminal spinner.

    Cycles through braille dot patterns to indicate activity.
    Call next() on each tick to advance the animation.
    """

    __slots__ = ("_index", "_frames")

    def __init__(self, frames: list[str] | None = None) -> None:
        self._index = 0
        self._frames = frames or SPINNER_FRAMES

    @property
    def current(self) -> str:
        """Return the current frame without advancing."""
        return self._frames[self._index % len(self._frames)]

    def next(self) -> str:
        """Return current frame and advance to next."""
        frame = self._frames[self._index % len(self._frames)]
        self._index += 1
        return frame

    def reset(self) -> None:
        """Reset spinner to the first frame."""
        self._index = 0
