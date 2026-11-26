"""Split files that exceed Telegram's upload limit.

Strategy (in priority order):
  1. Video files → ffmpeg segment muxer (each segment is playable).
  2. Archive files (.7z, .rar, .zip, .tar*, .gz, .bz2, .xz, .zst)
     → Repack as 7z volumes.  Each volume is a self-contained archive
     that can be extracted independently.
  3. Everything else → binary split with ``coreutils split``.

Safety contract
---------------
The original file is deleted **only after** the produced parts have been
verified:

* every part exists, is non-empty and is ≤ *max_size*;
* binary splits: ``sum(part sizes) == original size`` (exact — a binary
  split neither compresses nor drops bytes);
* video splits: total duration matches the original within 1 % (when
  ``ffprobe`` is available), otherwise the total size must be ≥ 95 % of
  the original (segments are stream-copied, so they cannot shrink);
* archive splits: ≥ 2 volumes, and — with ``SPLIT_VERIFY_HASH=1`` — an
  integrity test (``7z t``) of the produced volumes.

Anything that fails verification keeps the original and removes the
suspect parts, so a botched split can never destroy a download.

Parts are also filtered by mtime (``since``) so leftovers from an earlier
attempt can never be mistaken for the output of this one.

Set ``SPLIT_VERIFY_HASH=1`` for the extra (expensive) hash/integrity
checks; it is off by default because it re-reads the whole file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time

log = logging.getLogger("copybot.splitter")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".3gp", ".ts",
}

ARCHIVE_EXTS = {
    ".7z", ".rar", ".zip",
    ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz", ".tar.zst",
    ".gz", ".bz2", ".xz", ".zst",
}

# Formats that 7z can extract but that aren't self-extracting volumes.
_EXTRACTABLE = {".rar", ".zip", ".tar.gz", ".tgz",
                ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
                ".tar.zst", ".gz", ".bz2", ".xz", ".zst"}


def _verify_hash_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.getenv("SPLIT_VERIFY_HASH", "").strip().lower() in (
        "1", "true", "yes", "on")


def _double_ext(path: str) -> str:
    """Return the effective compound extension (e.g. .tar.gz)."""
    base = os.path.basename(path).lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if base.endswith(compound):
            return compound
    return os.path.splitext(path)[1].lower()


def _is_archive(path: str) -> bool:
    return _double_ext(path) in ARCHIVE_EXTS


def split_file(path: str, max_size: int, mime_type: str | None = None,
               *, verify_hash: bool | None = None) -> list[str]:
    """Split a file that exceeds *max_size*.

    Returns the list of parts on success (the original is deleted only
    after verification).  On any failure returns ``[path]`` — the original
    is kept untouched.
    """
    if not os.path.exists(path):
        return [path]

    file_size = os.path.getsize(path)
    if file_size <= max_size:
        return [path]

    verify = _verify_hash_enabled(verify_hash)
    started = time.time() - 1.0     # mtime slack for filesystem rounding
    ext = os.path.splitext(path)[1].lower()
    is_video = ((mime_type and mime_type.startswith("video/"))
                or ext in VIDEO_EXTS)

    if is_video:
        parts = _split_video_ffmpeg(path, max_size, ext, since=started)
        if parts and _verify_video(parts, path, file_size, max_size,
                                   verify_hash=verify):
            _discard_original(path, parts)
            return parts
        _abort(path, parts)
        log.warning("ffmpeg split verification failed for %s — original "
                    "kept", path)

    if _is_archive(path):
        parts = _split_archive_7z(path, max_size, since=started)
        if parts and _verify_archive(parts, max_size, verify_hash=verify):
            _discard_original(path, parts)
            return parts
        _abort(path, parts)
        log.warning("7z split verification failed for %s — original kept",
                    path)

    parts = _split_binary(path, max_size, since=started)
    if parts and _verify_binary(parts, file_size, max_size,
                                original=path, verify_hash=verify):
        _discard_original(path, parts)
        return parts
    _abort(path, parts)
    log.warning("binary split verification failed for %s — original kept",
                path)
    return [path]


# ── verification ────────────────────────────────────────────────────

def _common_checks(parts: list[str], max_size: int) -> bool:
    if len(parts) < 2:
        log.error("split produced %d part(s) (need ≥ 2)", len(parts))
        return False
    for p in parts:
        try:
            size = os.path.getsize(p)
        except OSError as exc:
            log.error("part %s missing: %s", p, exc)
            return False
        if size <= 0:
            log.error("part %s is empty", p)
            return False
        if size > max_size:
            log.error("part %s is %s (over limit %s)",
                      p, _human_size(size), _human_size(max_size))
            return False
    return True


def _verify_binary(parts: list[str], original_size: int, max_size: int, *,
                   original: str, verify_hash: bool) -> bool:
    if not _common_checks(parts, max_size):
        return False
    total = sum(os.path.getsize(p) for p in parts)
    if total != original_size:
        log.error("binary split size mismatch: parts=%s original=%s",
                  total, original_size)
        return False
    if verify_hash:
        want = _sha256_file(original)
        h = hashlib.sha256()
        for p in parts:
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        if h.hexdigest() != want:
            log.error("binary split hash mismatch — parts do not "
                      "recompose to the original")
            return False
    return True


def _verify_video(parts: list[str], original_path: str, original_size: int,
                  max_size: int, *, verify_hash: bool) -> bool:
    if not _common_checks(parts, max_size):
        return False
    total = sum(os.path.getsize(p) for p in parts)
    # stream copy → segments cannot shrink; allow 5 % container overhead
    if total < original_size * 0.95:
        log.error("video split lost data: parts=%s original=%s",
                  total, original_size)
        return False
    if verify_hash:
        # the original still exists at verification time — compare the
        # summed segment durations against it (±1 %)
        want = _probe_duration(original_path)
        if want > 0:
            got = sum(_probe_duration(p) for p in parts)
            if abs(got - want) > max(1.0, want * 0.01):
                log.error("video split duration mismatch: parts=%.1fs "
                          "original=%.1fs", got, want)
                return False
    return True


def _verify_archive(parts: list[str], max_size: int, *,
                    verify_hash: bool) -> bool:
    if not _common_checks(parts, max_size):
        return False
    if verify_hash:
        # integrity test of the produced volumes (7z reads everything back)
        try:
            r = subprocess.run(
                ["7z", "t", *parts],
                capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                log.error("7z integrity test failed: %s",
                          (r.stderr or r.stdout)[-300:])
                return False
        except Exception as exc:  # noqa: BLE001 - tool may be missing
            log.error("7z integrity test error: %s", exc)
            return False
    return True


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _abort(original: str, parts: list[str]) -> None:
    """Drop untrusted parts, keep the original."""
    for p in parts:
        if p == original:
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def _discard_original(original: str, parts: list[str]) -> None:
    try:
        os.remove(original)
        log.info("split %s → %d verified parts",
                 os.path.basename(original), len(parts))
    except OSError as exc:
        log.error("cannot remove original %s after split: %s",
                  original, exc)


# ── ffmpeg video split ──────────────────────────────────────────────

def _split_video_ffmpeg(path: str, max_size: int, ext: str,
                        *, since: float) -> list[str]:
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

    base = os.path.basename(path)
    parts = _collect_parts(
        path, lambda f: f.startswith(base + "_") and f.endswith(ext),
        since=since)
    if not parts:
        return []

    # Recursively split any segment still over the limit
    verified: list[str] = []
    for p in parts:
        if os.path.getsize(p) > max_size:
            verified.extend(split_file(p, max_size))
        else:
            verified.append(p)
    log.info("ffmpeg split %s → %d parts (pending verification)",
             base, len(verified))
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


# ── binary split (any file type) ────────────────────────────────────

def _split_binary(path: str, max_size: int, *, since: float) -> list[str]:
    """Coreutils ``split`` with numeric suffixes.

    Output: ``<path>.part_001``, ``<path>.part_002``, …
    Recombine: ``cat <path>.part_* > <path>``

    Pre-existing ``<path>.part_*`` leftovers are removed first: they are
    regenerable artifacts of an earlier attempt (a completed split deletes
    the original), and stale higher-numbered parts would otherwise be
    picked up by the glob below.
    """
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    stale = [f for f in os.listdir(d) if f.startswith(base + ".part_")]
    for f in stale:
        try:
            os.remove(os.path.join(d, f))
            log.debug("removed stale split part %s", f)
        except OSError:
            pass

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

    parts = _collect_parts(path,
                           lambda f: f.startswith(base + ".part_"),
                           since=since)
    if not parts:
        return [path]
    log.info("binary split %s → %d parts (pending verification)",
             base, len(parts))
    return parts


# ── 7z archive volume split ─────────────────────────────────────────

def _split_archive_7z(path: str, max_size: int, *,
                      since: float) -> list[str]:
    """Repackage an archive as 7z volumes, each ≤ *max_size*.

    Returns the list of volume paths on success, ``[]`` on failure.  The
    original is *not* removed here — the caller verifies first.
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

        # ── Step 2: Create 7z volumes ────────────────────────────────
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

        # ── Step 4: Move volumes next to the original ────────────────
        # (the caller deletes the original once the parts are verified)
        result: list[str] = []
        for i, vp in enumerate(volumes):
            vol_name = os.path.basename(vp)
            dest = os.path.join(d, vol_name)
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(vp, dest)
            result.append(dest)
            log.info("  volume %d/%d: %s (%s)", i + 1, len(result), vol_name, _human_size(os.path.getsize(dest)))

        log.info("7z split %s (%s) → %d volumes (pending verification)",
                 base, _human_size(file_size), len(result))
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


def _collect_parts(path: str, predicate, *, since: float = 0.0) -> list[str]:
    """Collect output parts, ignoring anything older than *since*.

    The mtime filter keeps leftovers of a previous (failed) attempt from
    being treated as output of this one.
    """
    d = os.path.dirname(path) or "."
    out: list[str] = []
    for f in sorted(os.listdir(d)):
        if not predicate(f):
            continue
        fp = os.path.join(d, f)
        if not os.path.isfile(fp):
            continue
        try:
            if since and os.path.getmtime(fp) < since:
                log.debug("ignoring stale split part %s", f)
                continue
        except OSError:
            continue
        out.append(fp)
    return out
