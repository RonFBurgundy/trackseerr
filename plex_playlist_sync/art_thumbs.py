"""Cached JPEG thumbnails (250/500px) for native library artwork.

A derivative is generated once from the local original, written atomically into the mediacover cache, and keyed by
the original's path, mtime and size, so a replaced original produces a new key (and a new strong ETag) while an
unchanged one is never resized again. The key is computable from a ``stat`` alone, so a matching ``If-None-Match``
is answered without touching the image at all.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Optional

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

THUMB_SIZES = (250, 500)
_JPEG_QUALITY = 85
# Resizing is CPU bound; a fast scroll must not fan out into dozens of simultaneous decodes.
_RESIZE_SLOTS = threading.BoundedSemaphore(4)


def normalize_size(size: Optional[int]) -> Optional[int]:
    """Returns ``size`` when supported, else None (callers then serve the original)."""
    return size if size in THUMB_SIZES else None


def thumb_key(src: Path, size: int) -> Optional[tuple[str, str]]:
    """Returns ``(file_stem, etag)`` for the derivative of ``src``, or None if ``src`` cannot be stat'ed."""
    try:
        st = src.stat()
    except OSError:
        return None
    path_hash = hashlib.sha1(str(src).encode("utf-8", "surrogateescape")).hexdigest()[:20]
    stem = f"{path_hash}-{st.st_mtime_ns:x}-{st.st_size:x}-{size}"
    return stem, f'"{stem}"'


def original_etag(src: Path) -> Optional[str]:
    """Strong ETag for the unsized original, from path + mtime + size (no read of the file); None if unreadable."""
    try:
        st = src.stat()
    except OSError:
        return None
    path_hash = hashlib.sha1(str(src).encode("utf-8", "surrogateescape")).hexdigest()[:20]
    return f'"{path_hash}-{st.st_mtime_ns:x}-{st.st_size:x}-orig"'


def _resize(src: Path, dest: Path, size: int) -> None:
    """Decodes ``src``, fits it inside ``size`` x ``size`` and writes a JPEG to ``dest`` atomically."""
    with Image.open(src) as img:
        img.draft("RGB", (size * 2, size * 2))  # JPEG: decode at reduced scale
        img = ImageOps.exif_transpose(img)
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        if img.mode != "RGB":
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (0, 0, 0))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".thumb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                img.save(fh, "JPEG", quality=_JPEG_QUALITY, optimize=True, progressive=True)
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def ensure_thumb(src: Path, size: int, cache_dir: Path) -> Optional[tuple[Path, str]]:
    """Returns ``(derivative_path, etag)``, generating the derivative on first use; None means serve the original."""
    key = thumb_key(src, size)
    if key is None:
        return None
    stem, etag = key
    dest = cache_dir / f"{stem}.jpg"
    try:
        if dest.is_file() and dest.stat().st_size > 0:
            return dest, etag
    except OSError:
        pass
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with _RESIZE_SLOTS:
            _resize(src, dest, size)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        logger.warning("Thumbnail generation failed for %s (%dpx): %s", src, size, exc)
        return None
    # Drop derivatives of earlier versions of this original (same path hash and size, different mtime/size).
    prefix, suffix = stem.split("-", 1)[0] + "-", f"-{size}.jpg"
    try:
        for old in cache_dir.glob(f"{prefix}*{suffix}"):
            if old != dest:
                old.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not prune stale thumbnails for %s: %s", src, exc)
    return dest, etag
