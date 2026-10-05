"""Per-track import check (docs/ARR_PROFILES_SPEC.md, Phase B3), designed for ~zero false positives.

It exists to catch fakes (upconverted lossy sold as lossless) and truncated/corrupt files, not to police encoder
choices. Each file is probed with mutagen (container, bit depth, sample rate, channels, duration, audio bitrate) and
produces findings of two classes:

* ERROR (the only class that fails a release in ``reject`` mode):
  - lossless audio-stream bitrate above 1.05 x the PCM ceiling (sample_rate x bits x channels): corrupt or mislabelled;
  - lossless bitrate below a floor: the quality definition's ``min_kbps`` when the user set one (> 0, capped at 95% of
    the PCM ceiling so mono/low-rate material is not judged by a stereo 16/44.1 number), otherwise a heuristic
    ``LOSSLESS_FLOOR_FRACTION`` (25%) of the 16/44.1 stereo PCM rate (~353 kbps, scaled down for mono/low-rate
    sources). Real FLAC of very quiet solo classical averages ~350-600 kbps; below a quarter of PCM is a lossy
    upconvert, mostly silence or a truncated file. Tracks shorter than ``MIN_FLOOR_DURATION`` seconds (skits, silent
    intros) only warn;
  - duration 0 or an unparsable file while at least one other file in the release is readable.
* WARN (never fails a release, even in ``reject`` mode): lossy bitrate outside the quality definition's
  ``[min_kbps, max_kbps]`` widened by 10% (AAC 256: ceiling widened by 25%, since AAC 320 is a normal encoder choice).

Lossless upper bounds come from physics, not from the release-level definition max. The audio bitrate comes from
mutagen's stream info where reliable; otherwise it is derived from the file size minus embedded picture bytes. Every
probe failure (any exception) is logged with its type, the file is skipped and noted, and the worker loop is never
crashed. ``import_bitrate_check`` is off | warn | reject.
"""

from __future__ import annotations

import base64
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

# MP3 average-bitrate thresholds (kbps). >=290 is 320; below that, VBR and CBR/ABR share the buckets so CBR 224/256 land
# in V1/V0 (whose definition ranges contain them) rather than the narrow "MP3 192" tier: V0 ~245, V1 ~225, V2 ~190.
_MP3_320_MIN = 290.0
_MP3_V0_MIN = 235.0
_MP3_V1_MIN = 205.0
# AAC at or above this is "AAC 256", anything lower "AAC (other)" (mirrors the title parser).
_AAC_256_MIN = 240.0

LOSSY_TOLERANCE = 0.10  # lossy bounds are widened by +/-10%
AAC_256_MAX_TOLERANCE = 0.25  # AAC 320 is common: AAC 256's ceiling is only enforced at +25%
PCM_CEILING_TOLERANCE = 0.05  # lossless above ceiling x 1.05 is corrupt/mislabelled
LOSSLESS_FLOOR_FRACTION = 0.25  # heuristic floor as a fraction of 16/44.1 stereo PCM (1411.2 kbps) -> ~353 kbps
LOSSLESS_FLOOR_REFERENCE_KBPS = 44100 * 16 * 2 / 1000.0
USER_FLOOR_CEILING_FRACTION = 0.95  # a user-set lossless min is never allowed to exceed 95% of the file's PCM ceiling
MIN_FLOOR_DURATION = 30.0  # seconds; shorter tracks may legitimately be near-silent, so a low floor only warns

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

_LOSSLESS_QUALITIES = frozenset(
    {
        AudioQuality.FLAC_24BIT.value,
        AudioQuality.FLAC_16BIT.value,
        AudioQuality.ALAC.value,
        AudioQuality.WAV_AIFF.value,
    }
)


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
    sample_rate: Optional[float] = None
    channels: Optional[int] = None
    bits_per_sample: Optional[int] = None
    unparseable: bool = False  # mutagen returned None or raised a parse error: a security failure at any check mode
    corrupt: bool = False  # parse failure / zero duration (error-class when other files are readable)


@dataclass
class TrackFinding:
    path: str
    quality: str
    kbps: float
    min_kbps: Optional[float]
    max_kbps: Optional[float]
    severity: str = SEVERITY_WARNING
    detail: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    @property
    def range_text(self) -> str:
        lo = f"{self.min_kbps:.0f}" if self.min_kbps else "0"
        hi = f"{self.max_kbps:.0f}" if self.max_kbps else "unbounded"
        return f"{lo}-{hi} kbps"

    def describe(self) -> str:
        note = self.detail or f"allowed {self.range_text}"
        return f"{Path(self.path).name}: {self.quality} {self.kbps:.0f} kbps ({note})"


@dataclass
class CheckResult:
    mode: str
    checked: int = 0
    out_of_range: list[TrackFinding] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def errors(self) -> list[TrackFinding]:
        return [f for f in self.out_of_range if f.is_error]

    @property
    def failed(self) -> bool:
        """Only ERROR-class findings fail a release, and only in reject mode; lossy range misses never do."""
        return self.mode == CHECK_REJECT and bool(self.errors)

    def reason(self) -> str:
        """Human-readable summary: the flagged files with kbps and why, plus any skipped files."""
        parts: list[str] = []
        if self.out_of_range:
            parts.append(
                f"{len(self.out_of_range)} of {self.checked} track(s) flagged ({len(self.errors)} error): "
                + "; ".join(f.describe() for f in self.out_of_range)
            )
        skipped = [(p, r) for p, r in self.skipped if not any(f.path == p for f in self.out_of_range)]
        if skipped:
            parts.append("skipped: " + ", ".join(f"{Path(p).name} ({r})" for p, r in skipped))
        return ". ".join(parts)


def _quality_from_mp3(audio: Any, kbps: float) -> str:
    if kbps >= _MP3_320_MIN:
        return AudioQuality.MP3_320.value
    if kbps >= _MP3_V0_MIN:
        return AudioQuality.MP3_V0.value
    if kbps >= _MP3_V1_MIN:
        return AudioQuality.MP3_V1.value
    if getattr(audio.info, "bitrate_mode", None) == BitrateMode.VBR:
        return AudioQuality.MP3_V2.value
    return AudioQuality.MP3_192.value  # CBR/ABR below ~205: the 192 tier


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


def _picture_bytes(audio: Any, spath: str) -> int:
    """Bytes of embedded cover art (FLAC pictures, ID3 APIC, MP4 covr, Ogg METADATA_BLOCK_PICTURE); 0 if unknown."""
    total = 0
    try:
        for pic in getattr(audio, "pictures", None) or []:  # FLAC
            total += len(pic.data)
        tags = getattr(audio, "tags", None)
        if tags is None:
            return total
        if isinstance(audio, MP3):
            total += sum(len(a.data) for a in tags.getall("APIC"))
        elif isinstance(audio, MP4):
            total += sum(len(c) for c in tags.get("covr", []) or [])
        elif isinstance(audio, (OggOpus, OggVorbis)):
            for b64 in tags.get("metadata_block_picture", []) or []:
                total += len(base64.b64decode(b64))
    except Exception as exc:  # noqa: BLE001 - art accounting is best-effort; the bitrate falls back to the raw size
        logger.warning("Import bitrate check: cannot measure embedded art in %s: %s: %s", spath, type(exc).__name__, exc)
    return total


def _int_attr(info: Any, name: str) -> Optional[int]:
    v = getattr(info, name, None)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def probe_audio_file(path: Path) -> ProbedFile:
    """Reads duration, bitrate and detected quality with ``mutagen.File``; never raises for any bad file."""
    spath = str(path)
    try:
        audio = mutagen.File(spath)
    except OSError as exc:
        logger.warning("Import bitrate check: cannot read %s: %s: %s", spath, type(exc).__name__, exc)
        return ProbedFile(spath, None, None, None, f"unreadable: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - mutagen raises MutagenError, struct.error, ValueError, EOFError, ...
        logger.warning("Import bitrate check: cannot parse %s: %s: %s", spath, type(exc).__name__, exc)
        return ProbedFile(spath, None, None, None, f"unreadable: {type(exc).__name__}: {exc}", corrupt=True, unparseable=True)
    try:
        if audio is None or getattr(audio, "info", None) is None:
            return ProbedFile(spath, None, None, None, "unrecognised audio format", unparseable=True)
        info = audio.info
        try:
            duration = float(getattr(info, "length", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            return ProbedFile(spath, None, None, None, "duration unreadable", corrupt=True)
        kbps: Optional[float] = None
        raw_bitrate = getattr(info, "bitrate", None)
        if isinstance(raw_bitrate, (int, float)) and raw_bitrate > 0:
            kbps = float(raw_bitrate) / 1000.0
        if kbps is None:  # e.g. Opus: derive from size minus embedded art, so cover art never inflates the bitrate
            size = path.stat().st_size - _picture_bytes(audio, spath)
            kbps = max(size, 0) * 8 / duration / 1000.0
        return ProbedFile(
            spath,
            detect_audio_quality(audio, kbps),
            kbps,
            duration,
            sample_rate=_int_attr(info, "sample_rate"),
            channels=_int_attr(info, "channels"),
            bits_per_sample=_int_attr(info, "bits_per_sample"),
        )
    except Exception as exc:  # noqa: BLE001 - nothing here may escape into the worker loop
        logger.warning("Import bitrate check: probing %s failed: %s: %s", spath, type(exc).__name__, exc)
        return ProbedFile(spath, None, None, None, f"probe error: {type(exc).__name__}: {exc}")


def _check_lossless(probed: ProbedFile, definition: Optional[dict[str, Any]]) -> Optional[TrackFinding]:
    """PCM-ceiling / floor check; None when fine or when the stream info needed for the ceiling is missing."""
    if not (probed.sample_rate and probed.channels and probed.bits_per_sample and probed.kbps is not None):
        return None
    kbps = float(probed.kbps)
    ceiling = probed.sample_rate * probed.bits_per_sample * probed.channels / 1000.0
    quality = str(probed.quality)
    if kbps > ceiling * (1 + PCM_CEILING_TOLERANCE):
        return TrackFinding(
            probed.path, quality, kbps, None, ceiling, SEVERITY_ERROR,
            f"above the {ceiling:.0f} kbps PCM ceiling ({probed.sample_rate} Hz/{probed.bits_per_sample}-bit/"
            f"{probed.channels} ch): corrupt or mislabelled",
        )
    user_min = float((definition or {}).get("min_kbps") or 0.0)
    if user_min > 0:
        floor = min(user_min, ceiling * USER_FLOOR_CEILING_FRACTION)
    else:
        floor = LOSSLESS_FLOOR_FRACTION * min(ceiling, LOSSLESS_FLOOR_REFERENCE_KBPS)
    if kbps < floor:
        short = (probed.duration or 0.0) < MIN_FLOOR_DURATION
        return TrackFinding(
            probed.path, quality, kbps, floor, ceiling, SEVERITY_WARNING if short else SEVERITY_ERROR,
            f"below the {floor:.0f} kbps lossless floor: likely a lossy upconvert, silence or truncated file",
        )
    return None


def _check_lossy(probed: ProbedFile, definition: dict[str, Any]) -> Optional[TrackFinding]:
    """Range check against the definition widened by the tolerance; always WARN class."""
    kbps = float(probed.kbps or 0.0)
    lo, hi = definition.get("min_kbps"), definition.get("max_kbps")
    hi_tol = AAC_256_MAX_TOLERANCE if probed.quality == AudioQuality.AAC_256.value else LOSSY_TOLERANCE
    below = bool(lo) and kbps < float(lo) * (1 - LOSSY_TOLERANCE)
    above = bool(hi) and kbps > float(hi) * (1 + hi_tol)  # max 0/None = unbounded
    if not (below or above):
        return None
    return TrackFinding(
        path=probed.path,
        quality=str(probed.quality),
        kbps=kbps,
        min_kbps=float(lo) if lo is not None else None,
        max_kbps=float(hi) if hi is not None else None,
        severity=SEVERITY_WARNING,
    )


def check_files(
    files: Iterable[Path],
    mode: str,
    definitions: dict[str, dict[str, Any]],
    probes: Optional[dict[str, ProbedFile]] = None,
) -> CheckResult:
    """Checks every file (see module docstring). ``definitions`` is {quality: {min_kbps, max_kbps, ...}}. Never raises."""
    result = CheckResult(mode=normalize_check_mode(mode))
    if result.mode == CHECK_OFF:
        return result
    corrupt: list[tuple[str, str]] = []
    for f in files:
        try:
            probed = (probes or {}).get(str(f)) or probe_audio_file(f)  # reuse the security stage's probe
            if probed.skipped_reason or probed.kbps is None:
                reason = probed.skipped_reason or "unknown"
                result.skipped.append((probed.path, reason))
                if probed.corrupt:
                    corrupt.append((probed.path, reason))
                continue
            definition = definitions.get(probed.quality or "")
            if probed.quality is None:
                result.skipped.append((probed.path, "no quality definition"))
                continue
            if probed.quality in _LOSSLESS_QUALITIES:
                finding = _check_lossless(probed, definition)
                if finding is None and not (probed.sample_rate and probed.channels and probed.bits_per_sample):
                    result.skipped.append((probed.path, "incomplete stream info"))
                    continue
            elif definition is None:
                result.skipped.append((probed.path, "no quality definition"))
                continue
            else:
                finding = _check_lossy(probed, definition)
            result.checked += 1
            if finding is not None:
                result.out_of_range.append(finding)
        except Exception as exc:  # noqa: BLE001 - a bad file must never crash the worker loop
            logger.warning("Import bitrate check: skipping %s: %s: %s", f, type(exc).__name__, exc)
            result.skipped.append((str(f), f"check error: {type(exc).__name__}: {exc}"))
    if result.checked:  # a zero-length/unparsable file next to readable ones is a broken download
        for path, reason in corrupt:
            result.out_of_range.append(
                TrackFinding(path, "unreadable", 0.0, None, None, SEVERITY_ERROR, f"{reason} while other tracks are readable")
            )
    return result
