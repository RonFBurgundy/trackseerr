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
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
from typing import Callable, Optional

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


def art_version(src: Path) -> Optional[str]:
    """Short version token for a local art file (``<mtime_ns>-<size>`` in hex), from one ``stat``; None if unreadable.

    It changes whenever the file is replaced, so it can ride in an art URL as ``?v=`` and let the browser cache the
    response forever.
    """
    try:
        st = src.stat()
    except OSError:
        return None
    if not src.is_file() or st.st_size <= 0:
        return None
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def pregenerate(src: Path, cache_dir: Path, stop: Optional[threading.Event] = None) -> int:
    """Generates every missing derivative of ``src`` now (idempotent: existing ones are untouched).

    Returns how many sizes are present afterwards. Resizes go through the shared ``_RESIZE_SLOTS`` semaphore. A set
    ``stop`` event ends the work between sizes (shutdown).
    """
    done = 0
    for size in THUMB_SIZES:
        if stop is not None and stop.is_set():
            break
        if ensure_thumb(src, size, cache_dir) is not None:
            done += 1
    return done


def has_all_thumbs(src: Path, cache_dir: Path) -> bool:
    """True when every derivative of ``src`` already exists (stat only, no decode)."""
    for size in THUMB_SIZES:
        key = thumb_key(src, size)
        if key is None:
            return False
        try:
            dest = cache_dir / f"{key[0]}.jpg"
            if not (dest.is_file() and dest.stat().st_size > 0):
                return False
        except OSError:
            return False
    return True


# Background pre-generation: two workers and a bounded backlog; anything dropped is generated lazily on first request
# or by the backfill task, so nothing depends on this queue being lossless.
_PREGEN_MAX_PENDING = 512
_pregen_lock = threading.Lock()
# source path -> callbacks to run once that source's derivatives are generated (merged when a caller dedupes onto it)
_pregen_pending: dict[str, list[Callable[[], None]]] = {}
_pregen_executor: Optional[ThreadPoolExecutor] = None
# Set by ``shutdown``; each executor gets its own event so a later restart (tests) is not poisoned by an old stop.
_pregen_stop = threading.Event()


def schedule_pregenerate(src: Path, cache_dir: Path, on_done: Optional[Callable[[], None]] = None) -> bool:
    """Queues ``pregenerate(src)`` on a small background pool. Never blocks; False when deduped or the queue is full.

    ``on_done`` runs on the worker after the derivatives are generated (the art version is published from it). When
    the call is deduped onto a queued job its ``on_done`` is merged and still runs; when the backlog is full the item
    is dropped (logged) and ``on_done`` never runs, so nothing is published until a later request or the backfill heals it.
    """
    global _pregen_executor, _pregen_stop
    key = str(src)
    with _pregen_lock:
        if key in _pregen_pending:
            if on_done is not None:
                _pregen_pending[key].append(on_done)
            return False
        if len(_pregen_pending) >= _PREGEN_MAX_PENDING:
            logger.info("Thumbnail pre-generation backlog full (%d); dropping %s", _PREGEN_MAX_PENDING, src)
            return False
        _pregen_pending[key] = [on_done] if on_done is not None else []
        if _pregen_executor is None:
            _pregen_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="art-thumbs")
            _pregen_stop = threading.Event()
        executor, stop = _pregen_executor, _pregen_stop

    def _job() -> None:
        try:
            if stop.is_set():
                return
            pregenerate(src, cache_dir, stop)
            while not stop.is_set():  # drain until empty: a caller may merge in while callbacks run
                with _pregen_lock:
                    pending = _pregen_pending.get(key)
                    callbacks = list(pending) if pending else []
                    if pending:
                        pending.clear()
                    else:
                        _pregen_pending.pop(key, None)
                if not callbacks:
                    break
                for callback in callbacks:
                    try:
                        callback()
                    except Exception as exc:
                        logger.warning("Thumbnail completion callback failed for %s: %s", src, exc)
        except Exception as exc:
            logger.warning("Thumbnail pre-generation failed for %s: %s", src, exc)
        finally:
            with _pregen_lock:
                if _pregen_executor is executor:
                    _pregen_pending.pop(key, None)

    try:
        executor.submit(_job)
    except RuntimeError as exc:
        with _pregen_lock:
            _pregen_pending.pop(key, None)
        logger.warning("Thumbnail pool unavailable: %s", exc)
        return False
    return True


def shutdown() -> None:
    """Stops background pre-generation without waiting: queued work is cancelled and running work ends between sizes."""
    global _pregen_executor
    with _pregen_lock:
        executor, _pregen_executor = _pregen_executor, None
        _pregen_stop.set()
        _pregen_pending.clear()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def wait_idle(timeout: float = 5.0) -> bool:
    """Blocks until no background pre-generation is pending (tests and shutdown)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _pregen_lock:
            if not _pregen_pending:
                return True
        time.sleep(0.01)
    return False
