"""Cheap, standard import hardening: magic-byte verification, header-parse requirement and quarantine.

Every check costs microseconds to a few milliseconds per file: one 16-byte read (plus one small read when a FLAC
carries a leading ID3v2 tag) and the mutagen header parse that the bitrate check already needs (the probe is shared,
never repeated). There is no decoding, antivirus or sandboxing.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

QUARANTINE_DIRNAME = "_quarantine"  # legacy location: no longer written, still skipped by scans
_HEADER_LEN = 16
_ID3_HEADER_LEN = 10
_MAX_ID3_SKIP = 64 * 1024 * 1024  # a larger "tag" is not a tag
LIBRARY_FILE_MODE = 0o644


def _is_mpeg_sync(h: bytes) -> bool:
    return len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0


def _is_adts_sync(h: bytes) -> bool:
    return len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF0) == 0xF0


def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _flac_after_id3(path: Path, header: bytes) -> bool:
    """True when ``header`` starts an ID3v2 tag that is directly followed by ``fLaC``."""
    if len(header) < _ID3_HEADER_LEN or any(b & 0x80 for b in header[6:10]):
        return False
    size = _syncsafe(header[6:10])
    if header[5] & 0x10:  # footer present
        size += 10
    if size > _MAX_ID3_SKIP:
        return False
    with open(path, "rb") as fh:
        fh.seek(_ID3_HEADER_LEN + size)
        return fh.read(4) == b"fLaC"


def _matches(ext: str, header: bytes, path: Path) -> bool:
    if ext == ".flac":
        return header[:4] == b"fLaC" or (header[:3] == b"ID3" and _flac_after_id3(path, header))
    if ext == ".mp3":
        return header[:3] == b"ID3" or _is_mpeg_sync(header)
    if ext in (".m4a", ".alac"):
        return header[4:8] == b"ftyp" or _is_adts_sync(header)
    if ext == ".aac":
        return header[4:8] == b"ftyp" or _is_adts_sync(header) or header[:3] == b"ID3"
    if ext in (".ogg", ".opus"):
        return header[:4] == b"OggS"
    if ext == ".wav":
        return header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    if ext in (".aiff", ".aif"):
        return header[:4] == b"FORM" and header[8:12] in (b"AIFF", b"AIFC")
    return False


def check_magic(path: Path | str) -> Optional[str]:
    """Returns None when the file's leading bytes match its extension, else a short failure reason."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        with open(p, "rb") as fh:
            header = fh.read(_HEADER_LEN)
        if not header:
            return "empty file"
        if _matches(ext, header, p):
            return None
    except OSError as exc:
        return f"unreadable: {type(exc).__name__}: {exc}"
    return f"content does not match {ext} signature"


@dataclass
class SecurityResult:
    failures: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    def reason(self) -> str:
        return "; ".join(f"{Path(p).name}: {r}" for p, r in self.failures)


def verify_files(files: Iterable[Path], probes: dict[str, "object"]) -> SecurityResult:
    """Magic-byte check plus the shared mutagen probe. ``probes`` maps str(path) -> ProbedFile and is filled in here
    (each file is parsed at most once; a file already failing the magic check is not parsed at all)."""
    from trackseerr.import_quality_check import probe_audio_file

    result = SecurityResult()
    for f in files:
        key = str(f)
        bad = check_magic(f)
        if bad is None:
            probed = probes.get(key)
            if probed is None:
                probed = probe_audio_file(f)
                probes[key] = probed
            if getattr(probed, "unparseable", False):
                bad = f"not parseable as audio ({getattr(probed, 'skipped_reason', 'unknown')})"
        if bad is not None:
            result.failures.append((key, bad))
    return result


def quarantine_files(
    paths: Iterable[str | Path],
    quarantine_dir: Path | str,
    download_id: str,
    *,
    copy: bool = False,
) -> list[Path]:
    """Puts rejected files in ``<quarantine_dir>/<download_id>/`` (never the library); returns the new locations.

    ``copy=False`` moves each file (usenet / Soulseek / staging sources, where nothing seeds). ``copy=True`` copies and
    leaves the source in place: a file a torrent client is seeding must never leave its download folder.
    """
    safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(download_id)) or "unknown"
    qdir = Path(quarantine_dir).resolve() / safe_id
    qdir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for i, src in enumerate(paths):
        src_p = Path(src)
        dest = qdir / f"{i:03d}_{src_p.name}"
        try:
            if copy:
                shutil.copyfile(str(src_p), str(dest))
            else:
                shutil.move(str(src_p), str(dest))
            os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)  # 0600: not executable, not readable by others
            moved.append(dest)
        except OSError as exc:
            logger.error("Quarantine of %s failed: %s: %s", src_p, type(exc).__name__, exc)
    return moved


def clear_exec_bits(path: Path | str) -> None:
    """chmod 0644 a placed library file. Failure is logged, never raised."""
    try:
        os.chmod(path, LIBRARY_FILE_MODE)
    except OSError as exc:
        logger.warning("Could not chmod %s to %o: %s: %s", path, LIBRARY_FILE_MODE, type(exc).__name__, exc)
