"""Per-track bitrate check on import (docs/ARR_PROFILES_SPEC.md, Phase B3).

Each file's duration and bitrate are read with mutagen, its quality is detected from the container/codec and compared
with that quality's definition ``[min_kbps, max_kbps]`` (``None``/``0`` max = unbounded). A file outside its range is
flagged; ``import_bitrate_check`` (off|warn|reject) decides whether that only warns or fails the import. Files whose
duration cannot be read are skipped and noted. Catches fake FLAC upconverts with a suspiciously low bitrate and
truncated files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from plex_playlist_sync.models import AudioQuality

logger = logging.getLogger(__name__)

CHECK_OFF = "off"
CHECK_WARN = "warn"
CHECK_REJECT = "reject"
CHECK_MODES = (CHECK_OFF, CHECK_WARN, CHECK_REJECT)
DEFAULT_CHECK_MODE = CHECK_WARN

# MP3 average-bitrate thresholds (kbps) for telling LAME VBR presets apart: V0 ~245, V1 ~225, V2 ~190.
_MP3_320_MIN = 290.0
_MP3_V0_MIN = 235.0
_MP3_V1_MIN = 205.0
# AAC at or above this is "AAC 256", anything lower "AAC (other)" (mirrors the title parser).
_AAC_256_MIN = 240.0


def normalize_check_mode(value: Any) -> str:
    """Returns a valid mode; anything unrecognized falls back to the default (warn)."""
    mode = str(value or "").strip().lower()
    return mode if mode in CHECK_MODES else DEFAULT_CHECK_MODE


@dataclass
class ProbedFile:
    path: str
    quality: Optional[str]
    kbps: Optional[float]
    duration: Optional[float]
    skipped_reason: Optional[str] = None


@dataclass
class TrackFinding:
    path: str
    quality: str
    kbps: float
    min_kbps: Optional[float]
    max_kbps: Optional[float]

    @property
    def range_text(self) -> str:
        lo = f"{self.min_kbps:.0f}" if self.min_kbps else "0"
        hi = f"{self.max_kbps:.0f}" if self.max_kbps else "unbounded"
        return f"{lo}-{hi} kbps"

    def describe(self) -> str:
        return f"{Path(self.path).name}: {self.quality} {self.kbps:.0f} kbps (allowed {self.range_text})"


@dataclass
class CheckResult:
    mode: str
    checked: int = 0
    out_of_range: list[TrackFinding] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.mode == CHECK_REJECT and bool(self.out_of_range)

    def reason(self) -> str:
        """Human-readable summary: the offending files with kbps vs range, plus any skipped files."""
        parts: list[str] = []
        if self.out_of_range:
            parts.append(
                f"{len(self.out_of_range)} of {self.checked} track(s) outside their quality's bitrate range: "
                + "; ".join(f.describe() for f in self.out_of_range)
            )
        if self.skipped:
            parts.append("skipped (unreadable duration): " + ", ".join(Path(p).name for p, _ in self.skipped))
        return ". ".join(parts)


def _quality_from_mp3(audio: Any, kbps: float) -> str:
    if kbps >= _MP3_320_MIN:
        return AudioQuality.MP3_320.value
    if getattr(audio.info, "bitrate_mode", None) == BitrateMode.VBR:
        if kbps >= _MP3_V0_MIN:
            return AudioQuality.MP3_V0.value
        if kbps >= _MP3_V1_MIN:
            return AudioQuality.MP3_V1.value
        return AudioQuality.MP3_V2.value
    return AudioQuality.MP3_192.value  # CBR/ABR below 290: the 192 tier (its minimum flags anything lower)


def detect_audio_quality(audio: Any, kbps: float) -> Optional[str]:
    """Quality id for a mutagen file object and its measured kbps, or None when the container is not recognised."""
    info = getattr(audio, "info", None)
    if isinstance(audio, FLAC):
        return (
            AudioQuality.FLAC_24BIT.value
            if (getattr(info, "bits_per_sample", 16) or 16) > 16
            else AudioQuality.FLAC_16BIT.value
        )
    if isinstance(audio, MP3):
        return _quality_from_mp3(audio, kbps)
    if isinstance(audio, MP4):
        if str(getattr(info, "codec", "")).lower() == "alac":
            return AudioQuality.ALAC.value
        return AudioQuality.AAC_256.value if kbps >= _AAC_256_MIN else AudioQuality.AAC_OTHER.value
    if isinstance(audio, OggOpus):
        return AudioQuality.OPUS.value
    if isinstance(audio, OggVorbis):
        return AudioQuality.OGG_VORBIS.value
    if isinstance(audio, (WAVE, AIFF)):
        return AudioQuality.WAV_AIFF.value
    return None


def probe_audio_file(path: Path) -> ProbedFile:
    """Reads duration, bitrate and detected quality with ``mutagen.File``; never raises for a bad file."""
    spath = str(path)
    try:
        audio = mutagen.File(spath)
    except (mutagen.MutagenError, OSError) as exc:
        logger.warning("Import bitrate check: cannot read %s: %s", spath, exc)
        return ProbedFile(spath, None, None, None, f"unreadable: {exc}")
    if audio is None or getattr(audio, "info", None) is None:
        return ProbedFile(spath, None, None, None, "unrecognised audio format")
    info = audio.info
    try:
        duration = float(getattr(info, "length", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return ProbedFile(spath, None, None, None, "duration unreadable")
    kbps: Optional[float] = None
    raw_bitrate = getattr(info, "bitrate", None)
    if isinstance(raw_bitrate, (int, float)) and raw_bitrate > 0:
        kbps = float(raw_bitrate) / 1000.0
    if kbps is None:  # e.g. Opus: derive the average from size and length
        try:
            kbps = path.stat().st_size * 8 / duration / 1000.0
        except OSError as exc:
            logger.warning("Import bitrate check: cannot stat %s: %s", spath, exc)
            return ProbedFile(spath, None, None, duration, "bitrate unreadable")
    return ProbedFile(spath, detect_audio_quality(audio, kbps), kbps, duration)


def check_files(files: Iterable[Path], mode: str, definitions: dict[str, dict[str, Any]]) -> CheckResult:
    """Checks every file against ``definitions`` ({quality: {min_kbps, max_kbps, ...}}). ``off`` does nothing."""
    result = CheckResult(mode=normalize_check_mode(mode))
    if result.mode == CHECK_OFF:
        return result
    for f in files:
        probed = probe_audio_file(f)
        if probed.skipped_reason or probed.kbps is None:
            result.skipped.append((probed.path, probed.skipped_reason or "unknown"))
            continue
        definition = definitions.get(probed.quality or "")
        if probed.quality is None or definition is None:
            result.skipped.append((probed.path, "no quality definition"))
            continue
        result.checked += 1
        lo = definition.get("min_kbps")
        hi = definition.get("max_kbps")
        below = bool(lo) and probed.kbps < float(lo)
        above = bool(hi) and probed.kbps > float(hi)  # max 0/None = unbounded
        if below or above:
            result.out_of_range.append(
                TrackFinding(
                    path=probed.path,
                    quality=str(probed.quality),
                    kbps=probed.kbps,
                    min_kbps=float(lo) if lo is not None else None,
                    max_kbps=float(hi) if hi is not None else None,
                )
            )
    return result
