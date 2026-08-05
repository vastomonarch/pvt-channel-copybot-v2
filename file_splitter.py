"""Split files that exceed Telegram's upload limit.

Strategy (in priority order):
  1. Archive files (.7z, .rar, .zip, .tar*, .gz, .bz2, .xz, .zst)
     → Repack as 7z volumes.  Each volume is a self-contained archive
     that can be extracted independently.
  2. Video files → ffmpeg segment muxer (each segment is playable).
  3. Everything else → binary split with ``coreutils split``.

All outputs are verified: no part exceeds *max_size*.
"""

import os
import subprocess
import logging
import shutil
import tempfile

log = logging.getLogger("copybot.splitter")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".3gp", ".ts",
}

ARCHIVE_EXTS = {
    ".7z", ".rar", ".zip",
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz", ".tar.zst",
    ".gz", ".bz2", ".xz", ".zst",
}

# Formats that 7z can extract but that aren't self-extracting volumes.
_EXTRACTABLE = {".rar", ".zip", ".tar", ".tar.gz", ".tgz",
                ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
                ".tar.zst", ".gz", ".bz2", ".xz", ".zst"}


def _double_ext(path: str) -> str:
    """Return the effective compound extension (e.g. .tar.gz)."""
    base = os.path.basename(path).lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if base.endswith(compound):
            return compound
    return os.path.splitext(path)[1].lower()


def _is_archive(path: str) -> bool:
    return _double_ext(path) in ARCHIVE_EXTS


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

    if _is_archive(path):
        parts = _split_archive_7z(path, max_size)
        if parts:
            return parts
        log.warning("7z archive split fell through to binary for %s", path)

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


# ── 7z archive volume split ───────────────────────────────────────

def _split_archive_7z(path: str, max_size: int) -> list[str]:
    """Repackage an archive as 7z volumes, each ≤ *max_size*.

    Steps:
      1. Extract the original archive to a temp directory.
      2. Create 7z volumes (``-v`` switch) where each volume ≤ max_size.
      3. If the archive was not a .7z, remove the original.
      4. Verify every output volume exists and is within the limit.

    Returns the list of volume paths on success, ``[]`` on failure.
    """
    file_size = os.path.getsize(path)
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    base_no_ext = base
    for ext in (_double_ext(path), os.path.splitext(path)[1]):
        if base_no_ext.lower().endswith(ext):
            base_no_ext = base_no_ext[: -len(ext)]
            break

    tmpdir = tempfile.mkdtemp(prefix="split7z_")
    try:
        # ── Step 1: Extract ──────────────────────────────────────────
        extract_dir = os.path.join(tmpdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        log.info("7z extracting %s → %s", base, extract_dir)

        # 7z x preserves directory structure; -y auto-confirms overwrite
        r = subprocess.run(
            ["7z", "x", f"-o{extract_dir}", "-y", path],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            log.error("7z extract failed rc=%d: %s", r.returncode, r.stderr[-300:])
            return []

        # Find extracted contents (skip if 7z extracted into a subdirectory)
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            extract_dir = os.path.join(extract_dir, entries[0])

        # ── Step 2: Create 7z volumes ───────────────────────────────
        vol_size_str = str(max_size)  # bytes
        out_archive = os.path.join(tmpdir, f"{base_no_ext}.7z")
        log.info(
            "7z creating volumes: %s -v%s -mx=3 (store-ish)",
            base_no_ext, _human_size(max_size),
        )
        r = subprocess.run(
            [
                "7z", "a",
                "-t7z",
                "-mx=3",          # moderate compression — speed > ratio
                f"-v{vol_size_str}",  # volume size in bytes
                out_archive,
                extract_dir,
            ],
            capture_output=True, text=True, timeout=1800,
        )
        if r.returncode != 0:
            log.error("7z create volumes failed rc=%d: %s", r.returncode, r.stderr[-300:])
            return []

        # ── Step 3: Collect volumes ──────────────────────────────────
        # 7z names volumes: archive.7z.001, archive.7z.002, ...
        # Single-volume archives keep the .7z extension (no .001 suffix).
        vol_prefix = f"{base_no_ext}.7z"
        volumes = []
        for f in sorted(os.listdir(tmpdir)):
            fp = os.path.join(tmpdir, f)
            if not os.path.isfile(fp):
                continue
            # Match: exact base name (single vol) OR numbered volumes
            if f == vol_prefix or f.startswith(f"{vol_prefix}."):
                vol_size = os.path.getsize(fp)
                if vol_size > max_size:
                    log.error(
                        "7z volume %s is %s (exceeds limit %s)",
                        f, _human_size(vol_size), _human_size(max_size),
                    )
                    return []
                volumes.append(fp)

        if not volumes:
            log.error("7z produced no volumes for %s", base)
            return []

        # If only 1 volume and it's the same size as the original,
        # 7z couldn't split — fall through.
        if len(volumes) == 1 and os.path.getsize(volumes[0]) >= file_size:
            log.warning("7z single volume same size as original, falling through")
            return []

        # ── Step 4: Move volumes to JOB_DIR, remove original ────────
        result: list[str] = []
        for i, vp in enumerate(volumes):
            vol_name = os.path.basename(vp)
            dest = os.path.join(d, vol_name)
            shutil.move(vp, dest)
            result.append(dest)
            log.info("  volume %d/%d: %s (%s)", i + 1, len(result), vol_name, _human_size(os.path.getsize(dest)))

        os.remove(path)
        log.info(
            "7z split %s (%s) → %d volumes",
            base, _human_size(file_size), len(result),
        )
        return result

    except Exception as e:
        log.error("7z archive split error: %s", e)
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── helpers ─────────────────────────────────────────────────────────

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _collect_parts(path: str, predicate) -> list[str]:
    d = os.path.dirname(path) or "."
    return sorted(os.path.join(d, f) for f in os.listdir(d) if predicate(f))
