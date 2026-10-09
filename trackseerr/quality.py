"""Quality Profiles release parser and scoring engine.

Supports standalone Arr-grade audio format selection, quality cutoffs,
release tag filtering, and score evaluation without Lidarr.
"""

import logging
import re
from typing import Any, Optional

from trackseerr.models import (
    AudioQuality,
    EvaluationResult,
    ParsedRelease,
    QualityProfile,
)

logger = logging.getLogger(__name__)

# Format detection patterns in strict order of precedence
_FORMAT_PATTERNS: list[tuple[AudioQuality, re.Pattern[str]]] = [
    # ALAC and PCM are explicit-word only and yield to an explicit "flac" (e.g. "FLAC + WAV sampler").
    (AudioQuality.ALAC, re.compile(r"(?i)^(?!.*\bflac\b).*\balac\b")),
    (AudioQuality.WAV_AIFF, re.compile(r"(?i)^(?!.*\bflac\b).*\b(?:wav|aiff?|pcm)\b")),
    (
        AudioQuality.FLAC_24BIT,
        re.compile(
            r"(?i)(?:\b24[-_ ]?bit\b|\b24[/_ -](?:96|192|88|48|44(?:\.1)?)\b|\b(?:96|192|88)[/-]24\b|\bhi[-_ ]?res\b|\bhires\b|\bhigh[-_ ]res\b)",
        ),
    ),
    (
        AudioQuality.FLAC_16BIT,
        re.compile(
            r"(?i)(?:\b16[-_ ]?bit\b|\b16[/_ -]44(?:\.1)?\b|\bflac\b|\blossless\b)",
        ),
    ),
    (AudioQuality.OPUS, re.compile(r"(?i)\bopus\b")),
    (AudioQuality.OGG_VORBIS, re.compile(r"(?i)\b(?:ogg|oga|vorbis)\b")),
    (
        AudioQuality.MP3_320,
        re.compile(r"(?i)(?:\b320\s*(?:kbps|k)?\b|\bcbr\s*320\b)"),
    ),
    (
        AudioQuality.MP3_V0,
        re.compile(r"(?i)(?:\bv0\b|\bvbr[-_ ]?v0\b|\bvbr[-_ ]?0\b)"),
    ),
    (AudioQuality.MP3_V1, re.compile(r"(?i)(?:\bv1\b|\bvbr[-_ ]?v1\b|\bvbr[-_ ]?1\b)")),
    (
        AudioQuality.AAC_256,
        re.compile(r"(?i)\b256\s*(?:kbps|k)?\b"),
    ),
    (
        AudioQuality.MP3_192,
        re.compile(r"(?i)(?:\b192\s*(?:kbps|k)?\b|\bcbr\s*192\b)"),
    ),
    (
        AudioQuality.MP3_V2,
        re.compile(r"(?i)(?:\bv2\b|\bvbr[-_ ]?v2\b|\bvbr[-_ ]?2\b)"),
    ),
]

_AAC_WORD = re.compile(r"(?i)\b(?:aac|m4a)\b")
_AAC_256_MIN_KBPS = 256
_AAC_ADJACENT_KBPS = re.compile(r"(?i)(?:\b(?:aac|m4a)[-_ ]+(\d{2,3})\b(?!\s*(?:bit|khz|hz))|\b(\d{2,3})[-_ ]+(?:aac|m4a)\b)")
_LOSSLESS_QUALITIES = frozenset(
    {
        AudioQuality.FLAC_24BIT.value,
        AudioQuality.FLAC_16BIT.value,
        AudioQuality.ALAC.value,
        AudioQuality.WAV_AIFF.value,
    }
)
# Codecs a stray "aac"/"m4a" word must not override (the title names the codec explicitly).
_NON_AAC_QUALITIES = _LOSSLESS_QUALITIES | {AudioQuality.OPUS.value, AudioQuality.OGG_VORBIS.value}

_SOURCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "WEB",
        re.compile(
            r"(?i)\b(?:web(?:-?dl|-?rip)?|qobuz|tidal|deezer|itunes|bandcamp|amazon(?:-hd)?)\b"
        ),
    ),
    ("CD", re.compile(r"(?i)\b(?:cd|cdda|cd-?rip|retail)\b")),
    ("Vinyl", re.compile(r"(?i)\b(?:vinyl|lp|12-inch|record)\b")),
    ("SACD", re.compile(r"(?i)\b(?:sacd|dsd(?:64|128|256)?)\b")),
    ("Cassette", re.compile(r"(?i)\b(?:cassette|tape)\b")),
]

_TAG_SPECS: list[tuple[str, re.Pattern[str]]] = [
    ("remaster", re.compile(r"(?i)\bremaster(?:ed)?\b")),
    ("remastered", re.compile(r"(?i)\bremastered\b")),
    ("deluxe", re.compile(r"(?i)\bdeluxe\b")),
    ("live", re.compile(r"(?i)\blive\b")),
    ("bootleg", re.compile(r"(?i)\bbootleg\b")),
    ("tribute", re.compile(r"(?i)\btribute\b")),
    ("instrumental", re.compile(r"(?i)\binstrumental\b")),
    ("karaoke", re.compile(r"(?i)\bkaraoke\b")),
    ("clean edit", re.compile(r"(?i)\bclean(?:\s+edit)?\b")),
    ("explicit", re.compile(r"(?i)\bexplicit\b")),
    ("bonus", re.compile(r"(?i)\bbonus\b")),
]


_NOISE_GROUPS = frozenset(
    {
        "flac", "mp3", "aac", "alac", "ape", "wav", "ogg", "opus", "m4a", "wv", "wavpack", "lossless", "cd", "cdda",
        "web", "vinyl", "lp", "sacd", "dsd", "cbr", "vbr", "v0", "v2", "320", "256", "192", "128", "16bit", "24bit",
        "16", "24", "44", "96", "192khz", "hires", "hi-res", "320kbps", "kbps", "mqa", "remaster", "remastered",
        "deluxe", "edition", "single", "ep", "album", "live", "mono", "stereo", "ost", "rip", "retail", "proper",
        "repack", "internal", "tape", "cassette",
    }
)
_SCENE_SUFFIX = re.compile(r"(?<![\s-])-([A-Za-z0-9][A-Za-z0-9_.]{1,30})$")
_BRACKET_GROUP = re.compile(r"[\[(]([^\[\]()\s]{1,29})[\])]")


def _is_noise_group(token: str) -> bool:
    t = token.strip().lower()
    return (
        t in _NOISE_GROUPS
        or bool(re.fullmatch(r"(19|20)\d{2}", t))
        or bool(re.fullmatch(r"\d+(?:[-/.]\d+)*(?:bit|khz|kbps|k)?", t))
        or bool(re.fullmatch(r"(?:flac|mp3|web|cd)[-_ ]?\w*", t) and t.split("-")[0] in _NOISE_GROUPS)
    )


def extract_release_group(title: str) -> Optional[str]:
    """Best-effort release group: a trailing ``-GROUP`` (scene), else the last ``[GROUP]`` / ``(GROUP)`` that is not a
    format, source, year or other noise token."""
    text = re.sub(r"\.(?:flac|mp3|m4a|zip|rar)$", "", (title or "").strip(), flags=re.IGNORECASE)
    m = _SCENE_SUFFIX.search(text)
    if m and not _is_noise_group(m.group(1)):
        return m.group(1)
    for token in reversed(_BRACKET_GROUP.findall(text)):
        if not _is_noise_group(token):
            return token.strip()
    return None


def parse_release_title(title: str) -> ParsedRelease:  # noqa: C901, PLR0915
    """Parses a release title into structured audio format, source, tags, and metadata.

    Handles scene and P2P conventions, including bitrates, audio channels,
    and release naming structures.
    """
    raw_title = title.strip()

    # 1. Format Detection
    detected_quality = AudioQuality.UNKNOWN.value
    bitrate_kbps: Optional[int] = None

    for quality_enum, pattern in _FORMAT_PATTERNS:
        if pattern.search(raw_title):
            detected_quality = quality_enum.value
            break

    # Extract explicit bitrate if present
    br_match = re.search(r"(?i)\b(\d{2,4})\s*k(?:bps)?\b", raw_title)

    # AAC is classified by bitrate: >= 256 kbps (or an unstated bitrate) is "AAC 256"; any lower bitrate is
    # "AAC (other)", a separate quality so it cannot satisfy a cutoff set at AAC 256.
    if _AAC_WORD.search(raw_title) and detected_quality not in _NON_AAC_QUALITIES:
        aac_kbps = int(br_match.group(1)) if br_match else None
        if aac_kbps is None:  # "AAC 192" without a k/kbps suffix: a 2-3 digit number right next to the codec word
            adj = _AAC_ADJACENT_KBPS.search(raw_title)
            if adj:
                aac_kbps = int(adj.group(1) or adj.group(2))
        if aac_kbps is None or aac_kbps >= _AAC_256_MIN_KBPS:
            detected_quality = AudioQuality.AAC_256.value
        else:
            detected_quality = AudioQuality.AAC_OTHER.value
    if br_match:
        try:
            bitrate_kbps = int(br_match.group(1))
        except ValueError:
            bitrate_kbps = None
    elif detected_quality == AudioQuality.MP3_320.value:
        bitrate_kbps = 320
    elif detected_quality == AudioQuality.AAC_256.value:
        bitrate_kbps = 256
    elif detected_quality == AudioQuality.MP3_192.value:
        bitrate_kbps = 192

    # 2. Source Detection
    detected_source: Optional[str] = None
    for src_name, pattern in _SOURCE_PATTERNS:
        if pattern.search(raw_title):
            detected_source = src_name
            break

    # 3. Tag Detection
    detected_tags: list[str] = []
    for tag_name, pattern in _TAG_SPECS:
        if pattern.search(raw_title):
            if tag_name not in detected_tags:
                detected_tags.append(tag_name)

    # 4. Year Extraction: prioritize parenthesized or bracketed years e.g. (2014) or [2014]
    detected_year: Optional[int] = None
    bracketed_year_match = re.search(r"[\(\[]\s*(19\d{2}|20\d{2})\s*[\)\]]", raw_title)
    if bracketed_year_match:
        try:
            detected_year = int(bracketed_year_match.group(1))
        except ValueError:
            detected_year = None
    else:
        # Fallback to general 4-digit year, picking the last one found before format tags
        all_years = list(re.finditer(r"\b(19\d{2}|20\d{2})\b", raw_title))
        if all_years:
            try:
                detected_year = int(all_years[-1].group(1))
            except ValueError:
                detected_year = None

    # 5. Artist, Album, and Title Extraction
    artist: Optional[str] = None
    album: Optional[str] = None
    extracted_title: Optional[str] = None

    # Standard "Artist - Album" or "Artist - Title" format
    if " - " in raw_title:
        parts = raw_title.split(" - ", 1)
        artist_candidate = parts[0].strip()
        rest = parts[1].strip()

        # Remove bracketed metadata [FLAC], (2020), etc.
        album_clean = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", rest).strip()
        album_clean = re.sub(r"[-_\s]+$", "", album_clean).strip()

        if artist_candidate:
            artist = artist_candidate
        if album_clean:
            album = album_clean
            extracted_title = album_clean
        else:
            album = rest
            extracted_title = rest
    else:
        # Check underscore scene format: Artist_Name-Album_Title-Year-FLAC
        scene_dash_parts = raw_title.split("-")
        if len(scene_dash_parts) >= 2 and ("_" in scene_dash_parts[0] or "_" in scene_dash_parts[1]):
            artist_candidate = scene_dash_parts[0].replace("_", " ").strip()
            album_candidate = scene_dash_parts[1].replace("_", " ").strip()
            # Clean if year was captured in album candidate
            album_candidate = re.sub(r"\b(19\d{2}|20\d{2})\b", "", album_candidate).strip()
            if artist_candidate:
                artist = artist_candidate
            if album_candidate:
                album = album_candidate
                extracted_title = album_candidate
        else:
            clean_title = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", raw_title).strip()
            extracted_title = clean_title if clean_title else raw_title

    return ParsedRelease(
        raw_title=raw_title,
        artist=artist,
        album=album,
        title=extracted_title,
        year=detected_year,
        quality=detected_quality,
        source=detected_source,
        tags=detected_tags,
        bitrate_kbps=bitrate_kbps,
        release_group=extract_release_group(raw_title),
    )


def evaluate_release(
    release: ParsedRelease,
    profile: QualityProfile,
    size_bytes: Optional[int] = None,
    *,
    protocol: Optional[str] = None,
    indexer_id: Optional[Any] = None,
    indexer_name: Optional[str] = None,
    indexer_flags: int = 0,
    duration: Optional[Any] = None,
    artist_tags: Optional[Any] = None,
) -> EvaluationResult:
    """Evaluates a parsed release against a quality profile via the decision engine.

    Applies release profiles, quality allowance, quality-definition kbps limits and custom-format scoring, and
    records every decision in ``EvaluationResult.breakdown``. ``duration`` is a ``decision_engine.DurationInfo``.
    """
    from trackseerr.decision_engine import evaluate_release_with_context

    return evaluate_release_with_context(
        release,
        profile,
        size_bytes,
        protocol=protocol,
        indexer_id=indexer_id,
        indexer_name=indexer_name,
        indexer_flags=indexer_flags,
        duration=duration,
        artist_tags=artist_tags,
    )
