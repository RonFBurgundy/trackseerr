"""Quality Profiles release parser and scoring engine.

Supports standalone Arr-grade audio format selection, quality cutoffs,
release tag filtering, and score evaluation without Lidarr.
"""

import logging
import re
from typing import Optional

from plex_playlist_sync.models import (
    AudioQuality,
    EvaluationResult,
    ParsedRelease,
    QualityProfile,
)

logger = logging.getLogger(__name__)

# Format detection patterns in strict order of precedence
_FORMAT_PATTERNS: list[tuple[AudioQuality, re.Pattern[str]]] = [
    (
        AudioQuality.FLAC_24BIT,
        re.compile(
            r"(?i)(?:\b24[-_ ]?bit\b|\b24[/_ -](?:96|192|88|48|44(?:\.1)?)\b|\b(?:96|192|88)[/-]24\b|\bhi[-_ ]?res\b|\bhires\b|\bhigh[-_ ]res\b)",
        ),
    ),
    (
        AudioQuality.FLAC_16BIT,
        re.compile(
            r"(?i)(?:\b16[-_ ]?bit\b|\b16[/_ -]44(?:\.1)?\b|\bflac\b|\blossless\b|\balac\b)",
        ),
    ),
    (
        AudioQuality.MP3_320,
        re.compile(r"(?i)(?:\b320\s*(?:kbps|k)?\b|\bcbr\s*320\b)"),
    ),
    (
        AudioQuality.MP3_V0,
        re.compile(r"(?i)(?:\bv0\b|\bvbr[-_ ]?v0\b|\bvbr[-_ ]?0\b)"),
    ),
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
_LOSSLESS_QUALITIES = frozenset({AudioQuality.FLAC_24BIT.value, AudioQuality.FLAC_16BIT.value})

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


def parse_release_title(title: str) -> ParsedRelease:
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

    # AAC is classified by bitrate: only >= 256 kbps (or an unstated bitrate) is "AAC 256". The quality model has no
    # lower AAC tier, so a lower-bitrate AAC is "Unknown" (it must not satisfy a cutoff it does not meet).
    if _AAC_WORD.search(raw_title) and detected_quality not in _LOSSLESS_QUALITIES:
        aac_kbps = int(br_match.group(1)) if br_match else None
        if aac_kbps is None or aac_kbps >= _AAC_256_MIN_KBPS:
            detected_quality = AudioQuality.AAC_256.value
        else:
            detected_quality = AudioQuality.UNKNOWN.value
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
    )


def evaluate_release(
    release: ParsedRelease,
    profile: QualityProfile,
    size_bytes: Optional[int] = None,
) -> EvaluationResult:
    """Evaluates a parsed release against a quality profile.

    Validates audio format allowances, ignored tags, file size bounds,
    and calculates ranking score and cutoff fulfillment.
    """
    is_acceptable = True
    rejection_reasons: list[str] = []

    # 1. Size Constraints
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        if profile.min_size_mb is not None and size_mb < profile.min_size_mb:
            rejection_reasons.append(
                f"Release size ({size_mb:.1f} MB) is below minimum ({profile.min_size_mb:.1f} MB)"
            )
            is_acceptable = False
        if profile.max_size_mb is not None and size_mb > profile.max_size_mb:
            rejection_reasons.append(
                f"Release size ({size_mb:.1f} MB) exceeds maximum ({profile.max_size_mb:.1f} MB)"
            )
            is_acceptable = False

    # 2. Ignored Tags Check
    raw_lower = release.raw_title.lower()
    release_tags_lower = {t.lower() for t in release.tags}
    for tag in profile.ignored_tags:
        clean_tag = tag.strip().lower()
        if not clean_tag:
            continue
        if clean_tag in release_tags_lower or re.search(r"\b" + re.escape(clean_tag) + r"\b", raw_lower):
            rejection_reasons.append(f"Contains rejected keyword '{tag}'")
            is_acceptable = False

    # 3. Quality Allowed Check
    matching_item = None
    item_index = None
    for idx, item in enumerate(profile.items):
        if item.quality == release.quality:
            matching_item = item
            item_index = idx
            break

    if matching_item is None or not matching_item.allowed:
        rejection_reasons.append(f"Quality '{release.quality}' is not allowed in profile")
        is_acceptable = False

    # 4. Score Calculation
    base_score = 0
    if matching_item is not None:
        base_score = matching_item.weight if matching_item.weight else max(0, 1000 - (item_index * 100 if item_index is not None else 0))

    bonus_score = 0
    for pref in profile.preferred_tags:
        clean_pref = pref.strip().lower()
        if not clean_pref:
            continue
        source_matches = release.source is not None and clean_pref == release.source.lower()
        tag_matches = clean_pref in release_tags_lower
        raw_matches = bool(re.search(r"\b" + re.escape(clean_pref) + r"\b", raw_lower))
        if source_matches or tag_matches or raw_matches:
            bonus_score += 50

    total_score = base_score + bonus_score

    # Custom Formats (CF) regex scoring
    for cf in (profile.custom_formats or []):
        name = cf.get("name", "Custom Format")
        try:
            score = int(cf.get("score", 0))
        except (ValueError, TypeError):
            score = 0
        pattern_str = cf.get("pattern", "")
        negate = bool(cf.get("negate", False))
        if not pattern_str:
            continue
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            matched = bool(pattern.search(release.raw_title))
        except re.error as e:
            logger.warning("Invalid regex pattern in custom format '%s': %s", name, e)
            continue

        if (matched and not negate) or (not matched and negate):
            total_score += score

    # Minimum score threshold check
    if profile.min_score is not None and total_score < profile.min_score:
        is_acceptable = False
        rejection_reasons.append(
            f"Score {total_score} is below profile minimum {profile.min_score}"
        )

    # 5. Cutoff Check
    # Lower item index indicates higher quality/preference
    cutoff_index = next(
        (i for i, it in enumerate(profile.items) if it.quality == profile.cutoff), None
    )
    if item_index is not None and cutoff_index is not None:
        meets_cutoff = item_index <= cutoff_index
    elif matching_item is not None:
        cutoff_item = next(
            (it for it in profile.items if it.quality == profile.cutoff), None
        )
        if cutoff_item:
            meets_cutoff = matching_item.weight >= cutoff_item.weight
        else:
            meets_cutoff = False
    else:
        meets_cutoff = False

    return EvaluationResult(
        is_acceptable=is_acceptable,
        score=total_score,
        rejection_reasons=rejection_reasons,
        parsed_quality=release.quality,
        meets_cutoff=meets_cutoff,
    )
