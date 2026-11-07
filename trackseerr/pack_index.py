"""Pure logic and models for torrent pack indexing, parsing, and candidate matching."""

from __future__ import annotations

import re
from dataclasses import dataclass

from trackseerr.exclusions import exclusion_norm

AUDIO_EXTS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".alac",
    ".wma",
}

_LEADING_TRACK_RE = re.compile(
    r"^\s*(?:"
    r"\[\s*\d+\s*\]\s*[-–—.]?\s*"
    r"|\(\s*\d+\s*\)\s*[-–—.]?\s*"
    r"|(?:cd|disc)?\s*\d+[\.\-_]\d+\.?\s*[-–—.]?\s*"
    r"|\d+\s*[-–—]\s*"
    r"|\d+[\._]\s*"
    r"|(?:0\d+|\d{3,4})\s+"
    r")",
    re.IGNORECASE,
)

_SPLIT_SEPARATORS_RE = re.compile(r"\s+[-–—]\s+")
_TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_DISC_FOLDER_RE = re.compile(r"^(?:cd|disc)\s*\d+$", re.IGNORECASE)

_PACK_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"va"
    r"|various\s+artists"
    r"|packs?"
    r"|pools?"
    r"|top\s*(?:100|40|50)"
    r"|charts?"
    r"|compilations?"
    r"|collections?"
    r"|megapacks?"
    r"|discograph(?:y|ies)"
    r"|bundles?"
    r"|promo\s+only"
    r"|dj"
    r"|mixes"
    r"|hits"
    r")\b",
    re.IGNORECASE,
)
_VA_DOT_RE = re.compile(r"(?:^|[\s._\-\(\[])v\.a\.(?:$|[\s._\-\)\]])", re.IGNORECASE)


def _strip_trailing_bracket_tags(text: str) -> str:
    current = text
    while True:
        cleaned = _TRAILING_BRACKET_RE.sub("", current)
        if cleaned == current:
            break
        current = cleaned
    return current.strip()


def parse_pack_file(path: str) -> tuple[str | None, str | None]:
    """Parse artist and title from a torrent pack file path.

    From the last path component without extension:
    - strip leading track/disc numbers and separators ('045 ', '045 - ', '1-05. ', '[045] ')
    - split on the first ' - ' / ' – ' / ' — ' into (artist, title)
    - no separator -> (None, stem)
    - strip trailing [...] tags like [Clean], [320], [www.site.com], keep ( ... Mix) / (Extended) etc. in the title
    - folder-level artist (e.g. 'Artist - Album/01 Song.flac') may be used when the file has no artist.
    """
    if not path or not path.strip():
        return (None, None)

    normalized_path = path.replace("\\", "/")
    parts = [p.strip() for p in normalized_path.split("/") if p.strip()]
    if not parts:
        return (None, None)

    filename = parts[-1]
    stem = re.sub(r"\.[a-zA-Z0-9]+$", "", filename).strip()
    if not stem:
        return (None, None)

    stripped = _LEADING_TRACK_RE.sub("", stem).strip()
    if not stripped:
        stripped = stem

    split_parts = _SPLIT_SEPARATORS_RE.split(stripped, maxsplit=1)
    if len(split_parts) == 2:
        artist: str | None = split_parts[0].strip()
        title: str | None = split_parts[1].strip()
    else:
        artist = None
        title = stripped

    if title:
        title = _strip_trailing_bracket_tags(title)
    if artist:
        artist = _strip_trailing_bracket_tags(artist)

    # If no file-level artist, attempt to infer from parent/grandparent folder
    if not artist and len(parts) > 1:
        parent = parts[-2]
        if _DISC_FOLDER_RE.match(parent) and len(parts) > 2:
            candidate_folder = parts[-3]
        else:
            candidate_folder = parent

        folder_split = _SPLIT_SEPARATORS_RE.split(candidate_folder, maxsplit=1)
        if len(folder_split) == 2:
            cand_artist = folder_split[0].strip()
            cand_artist_clean = _strip_trailing_bracket_tags(cand_artist)
            if cand_artist_clean.casefold() not in ("va", "v.a.", "various", "various artists") and cand_artist_clean:
                artist = cand_artist_clean

    final_artist = artist if (artist and artist.strip()) else None
    final_title = title if (title and title.strip()) else None
    return (final_artist, final_title)


def search_text(path: str, artist: str | None, title: str | None) -> str:
    """Normalized text for FTS: exclusion_norm applied to artist, title and filename stem."""
    normalized_path = path.replace("\\", "/")
    filename = normalized_path.split("/")[-1] if normalized_path else ""
    stem = re.sub(r"\.[a-zA-Z0-9]+$", "", filename).strip()

    parts: list[str] = []
    if artist:
        norm_a = exclusion_norm(artist)
        if norm_a:
            parts.append(norm_a)
    if title:
        norm_t = exclusion_norm(title)
        if norm_t:
            parts.append(norm_t)
    if stem:
        norm_s = exclusion_norm(stem)
        if norm_s and norm_s != (parts[-1] if parts else ""):
            parts.append(norm_s)

    return " ".join(parts)


def looks_like_pack(title: str, file_count: int | None = None) -> bool:
    """True for titles containing case-insensitive, word-bounded pack keywords or file_count >= 25."""
    if file_count is not None and file_count >= 25:
        return True
    if not title:
        return False

    if _VA_DOT_RE.search(title):
        return True

    # Check against original title
    if _PACK_KEYWORDS_RE.search(title):
        return True

    # Check normalized title with separators replaced with spaces
    spaced = re.sub(r"[\._\-\+]+", " ", title)
    return bool(_PACK_KEYWORDS_RE.search(spaced))


@dataclass(frozen=True)
class PackFileMatch:
    pack_id: str
    file_index: int
    path: str
    size_bytes: int | None
    ext: str | None
    parsed_artist: str | None
    parsed_title: str | None
    pack_title: str
    indexer_id: str
    download_url: str | None
    magnet_url: str | None
    info_hash: str | None
    seeders: int | None
    file_count: int
