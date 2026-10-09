"""File placement, atomic operations, and import mode handling for Trackseerr."""

import errno
import logging
import os
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional


from trackseerr.clients.acquisition import is_torrent_driver_type
from trackseerr.import_security import (
    clear_exec_bits,
)
from trackseerr.security import is_safe_service_url


logger = logging.getLogger(__name__)


def _is_safe_cover_url(url: Optional[str]) -> bool:
    """Validates that a cover artwork URL is safe against SSRF attacks."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower()
        whitelisted_domains = (
            "mzstatic.com",
            "deezer.com",
            "dzcdn.net",
            "spotify.com",
            "scdn.co",
            "last.fm",
            "musicbrainz.org",
            "discogs.com",
            "coverartarchive.org",
            "archive.org",
        )
        if any(hostname == d or hostname.endswith("." + d) for d in whitelisted_domains):
            return True
        return is_safe_service_url(url, allow_lan=False)
    except Exception:
        return False



IMPORT_MODES = ("move", "hardlink", "copy")



def preserves_source(mode: str | None) -> bool:
    """True when an import mode leaves the source file in place, so a torrent can keep seeding."""
    return mode in ("hardlink", "copy")



def _copy_atomic(src: Path, dst: Path) -> Path:
    """Copies src to a hidden temp file beside dst, then os.replace: readers never see a partial file."""
    tmp_dst = dst.parent / f".tmp_{dst.name}_{os.getpid()}_{time.time_ns()}"
    try:
        shutil.copy2(str(src), str(tmp_dst))
        os.replace(str(tmp_dst), str(dst))
    except BaseException:
        try:
            tmp_dst.unlink(missing_ok=True)
        except OSError as cleanup_err:
            logger.warning("Could not remove temp file '%s': %s", tmp_dst, cleanup_err)
        raise
    return dst



def safe_atomic_move(source_file: Path | str, target_file: Path | str) -> Path:
    """Atomically places source_file at target_file, safely handling cross-device mounts.

    If source and destination reside on the same filesystem, os.replace is used directly.
    On EXDEV (different filesystems), writes to a temporary hidden file in the destination
    folder first, then atomically replaces to ensure Plex never indexes incomplete files.
    Any other OSError is logged and re-raised.
    """
    src = Path(source_file).resolve()
    dst = Path(target_file).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.replace(str(src), str(dst))
        return dst
    except OSError as e:
        if e.errno != errno.EXDEV:
            logger.error("Move '%s' -> '%s' failed: %s", src, dst, e)
            raise
    _copy_atomic(src, dst)
    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Copied '%s' -> '%s' across devices but could not remove the source: %s", src, dst, e)
    return dst



def place_audio_file(
    source_file: Path | str, target_file: Path | str, mode: str = "move"
) -> Path:
    """Places source_file at target_file according to mode.

    - "hardlink": os.link(src, dst); on OSError (e.g. EXDEV) falls back to an atomic copy.
      The source is always left untouched.
    - "copy": atomic copy (hidden temp + os.replace); the source is left untouched.
    - "move": safe_atomic_move (atomic replace, source removed).
    Any other mode raises ValueError.
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"Unknown import mode {mode!r}; expected one of {', '.join(IMPORT_MODES)}")
    src = Path(source_file).resolve()
    dst = Path(target_file).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    if mode == "hardlink":
        try:
            os.link(str(src), str(dst))
            logger.info("Successfully hardlinked '%s' -> '%s'", src, dst)
            clear_exec_bits(dst)
            return dst
        except OSError as e:
            logger.warning("os.link failed (%s); falling back to atomic copy for '%s' -> '%s'", e, src, dst)
            _copy_atomic(src, dst)
            clear_exec_bits(dst)
            return dst
    if mode == "copy":
        _copy_atomic(src, dst)
        clear_exec_bits(dst)
        return dst
    placed = safe_atomic_move(source_file, target_file)
    clear_exec_bits(placed)
    return placed



def ensure_private_copy(path: Path | str) -> bool:
    """Makes ``path`` safe to rewrite in place: True when no other link shares its inode afterwards.

    A hardlink-imported library file shares its inode with the torrent's seeding file, so tagging it would corrupt the
    torrent's data. When ``st_nlink > 1`` the file is copied to a hidden temp beside it and ``os.replace``d over it
    (atomic; the other link keeps the original inode and bytes). Returns False when the copy failed: the caller must
    then skip every tag/artwork write for that file.
    """
    p = Path(path)
    try:
        if p.stat().st_nlink <= 1:
            return True
    except OSError as e:
        logger.warning("Cannot stat '%s' before tagging; skipping tag writes: %s", p, e)
        return False
    try:
        _copy_atomic(p, p)
        clear_exec_bits(p)
    except OSError as e:
        logger.warning("Could not break hardlink for tagging '%s'; skipping tag writes: %s", p, e)
        return False
    logger.info("Broke hardlink for tagging: %s", p)
    return True



TORRENT_HARDLINK_TAG_MODES = ("copy_and_tag", "keep_hardlink")



def effective_import_mode(client_type: str | None, media_settings: dict[str, Any]) -> str:
    """Import mode for a download: the configured mode for torrent clients, always "move" for everything else.

    Usenet and Soulseek files do not seed from their source, so there is nothing to preserve.
    """
    if not is_torrent_driver_type(client_type):
        return "move"
    mode = str(media_settings.get("import_mode") or "move")
    return mode if mode in IMPORT_MODES else "move"



def prepare_file_for_tagging(path: Path | str, media_settings: dict[str, Any]) -> bool:
    """True when ``path`` may be rewritten with tags/artwork; False when tag and art writes must be skipped.

    A hardlinked file (shared inode with a seeding torrent) is either kept untouched (``keep_hardlink``) or split into
    a private copy first (``copy_and_tag``, the default, via ``ensure_private_copy``).
    """
    p = Path(path)
    if str(media_settings.get("torrent_hardlink_tags") or "copy_and_tag") == "keep_hardlink":
        try:
            shared = p.stat().st_nlink > 1
        except OSError as e:
            logger.warning("Cannot stat '%s' before tagging; skipping tag writes: %s", p, e)
            return False
        if shared:
            logger.info("Kept hardlink; skipped tag writing for %s", p)
            return False
        return True
    return ensure_private_copy(p)



def translate_remote_path(
    remote_path: Optional[str], mappings: list[dict[str, str]]
) -> Optional[str]:
    """Translates remote download client file paths to local mount paths.

    If remote_path starts with a mapping's remote_path, replaces that prefix with local_path.
    Guards against directory traversal attacks.
    """
    if remote_path is None:
        return None

    # Defense against directory traversal attempts in remote path input
    parts = remote_path.replace("\\", "/").split("/")
    if ".." in parts:
        logger.warning("Path traversal attempt rejected in remote_path: %s", remote_path)
        return None

    if not mappings:
        return remote_path

    resolved = remote_path
    for m in mappings:
        if not isinstance(m, dict):
            continue
        r = m.get("remote_path")
        l = m.get("local_path")
        if not r or not l:
            continue
        r_clean = r.rstrip("/")
        l_clean = l.rstrip("/")
        if resolved == r_clean:
            resolved = l_clean
            break
        elif resolved.startswith(r_clean + "/"):
            resolved = l_clean + resolved[len(r_clean):]
            break
        elif resolved.startswith(r_clean + "\\"):
            resolved = l_clean + "/" + resolved[len(r_clean) + 1:].replace("\\", "/")
            break

    norm = os.path.normpath(resolved)
    if ".." in norm.replace("\\", "/").split("/"):
        logger.warning("Directory traversal detected in remote path mapping: %s", resolved)
        return None

    return norm



def _path_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents

