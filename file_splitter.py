"""Split files that exceed Telegram's 2GB upload limit.

For video files: ffmpeg segment muxer (each part is playable).
For archives / other files: binary split (recombine with `cat`).
"""

import os
import subprocess
import logging

log = logging.getLogger("copybot.splitter")

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
              '.m4v', '.mpg', '.mpeg', '.3gp', '.ts'}


def split_file(path: str, max_size: int, mime_type: str | None = None) -> list[str]:
    """Split a file that exceeds *max_size*.

    Returns a list of file paths.  The original file is deleted on success.
    If splitting fails, returns ``[path]`` (original kept).
    """
    if not os.path.exists(path):
        return [path]

    file_size = os.path.getsize(path)
    if file_size <= max_size:
        return [path]

    ext = os.path.splitext(path)[1].lower()
    is_video = (mime_type and mime_type.startswith("video/")) or ext in VIDEO_EXTS

    if is_video:
        parts = _split_video_ffmpeg(path, max_size, ext)
        if parts:
            return parts
        log.warning("ffmpeg split fell through to binary for %s", path)

    return _split_binary(path, max_size)


# ── ffmpeg video split ──────────────────────────────────────────────

def _split_video_ffmpeg(path: str, max_size: int, ext: str) -> list[str]:
    duration = _probe_duration(path)
    if duration <= 0:
        return []

    file_size = os.path.getsize(path)
    # segment duration = (target / actual) * total_duration, 5 % headroom
    seg_dur = max(1.0, (max_size / file_size) * duration * 0.95)
    out_pattern = f"{path}_%03d{ext}"

    try:
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-c", "copy", "-map", "0",
            "-segment_time", str(seg_dur),
            "-f", "segment",
            out_pattern,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            log.error("ffmpeg split rc=%d: %s", r.returncode, r.stderr[-300:])
            return []
    except Exception as e:
        log.error("ffmpeg split error: %s", e)
        return []

    parts = _collect_parts(path, lambda f: f.startswith(os.path.basename(path) + "_") and f.endswith(ext))
    if not parts:
        return []

    # Recursively split any segment still over the limit
    verified: list[str] = []
    for p in parts:
        if os.path.getsize(p) > max_size:
            verified.extend(split_file(p, max_size))
        else:
            verified.append(p)

    os.remove(path)
    log.info("ffmpeg split %s → %d parts", os.path.basename(path), len(verified))
    return verified


def _probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


# ── binary split (any file type) ───────────────────────────────────

def _split_binary(path: str, max_size: int) -> list[str]:
    """Coreutils ``split`` with numeric suffixes.

    Output: ``<path>.part_001``, ``<path>.part_002``, …
    Recombine: ``cat <path>.part_* > <path>``
    """
    prefix = f"{path}.part_"
    try:
        r = subprocess.run(
            ["split", "-b", str(max_size),
             "--numeric-suffixes=1", "-a", "3", path, prefix],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            log.error("split failed: %s", r.stderr)
            return [path]
    except Exception as e:
        log.error("split error: %s", e)
        return [path]

    parts = _collect_parts(path, lambda f: f.startswith(os.path.basename(path) + ".part_"))
    if parts:
        os.remove(path)
        log.info("binary split %s → %d parts", os.path.basename(path), len(parts))
        return parts
    return [path]


# ── helpers ─────────────────────────────────────────────────────────

def _collect_parts(path: str, predicate) -> list[str]:
    d = os.path.dirname(path) or "."
    return sorted(os.path.join(d, f) for f in os.listdir(d) if predicate(f))
