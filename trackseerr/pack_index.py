"""Pure logic and models for torrent pack indexing, parsing, and candidate matching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

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


_TRAILING_KEY_RE = re.compile(
    r"(?i)(?:[\(\[]\s*|\b)((?:[1-9]|1[0-2])[ab]|\d{1,2}[dm])(?:\s*[\)\]]|\b)\s*$"
)
_TRAILING_BPM_RE = re.compile(
    r"(?i)(?:[\(\[]\s*|\b)(\d{2,3})\s*(?:bpm)?(?:\s*[\)\]]|\b)\s*$"
)
_KEY_ANYWHERE_RE = re.compile(
    r"(?i)(?:[\(\[]\s*)(\b(?:[1-9]|1[0-2])[ab]|\d{1,2}[dm])(?:\s*[\)\]])"
)
_VARIANT_RE = re.compile(
    r"(?i)[\(\[]\s*(intro(?:\s+clean|\s+dirty)?|acap+ella|instrumental|inst|dj\s+tool|hype\s+intro)\s*[\)\]]"
)
_EDIT_RE = re.compile(
    r"(?i)[\(\[]\s*(clean(?:\s+edit)?|dirty(?:\s+edit)?|explicit|quick\s*hit|short\s*edit)\s*[\)\]]"
)
_FEAT_RE = re.compile(
    r"(?i)(?:[\(\[]\s*)?\b(?:feat|ft|featuring)\b\.?\s+([^()\[\]\-_]+?)(?:\s*[\)\]]|(?=\s*[\(\[]|$))"
)
_WATERMARK_WWW_RE = re.compile(r"(?i)\[\s*www\.[^\]]*\]")
_WATERMARK_POOL_RE = re.compile(
    r"(?i)\[\s*(?:[a-z0-9_.\s-]*pool|dj\s*city|bpm\s*supreme|direct\s*music\s*service|promo\s*only)[^\]]*\]"
)

_RE_320 = re.compile(r"(?i)\b320\s*(?:kbps|k)?\b")
_RE_256 = re.compile(r"(?i)\b256\s*(?:kbps|k)?\b")
_RE_192 = re.compile(r"(?i)\b192\s*(?:kbps|k)?\b")
_RE_V0 = re.compile(r"(?i)(?:\bv0\b|\bvbr[-_ ]?v0\b|\bvbr[-_ ]?0\b)")


def strip_trailing_bpm(title: str) -> str | None:
    """Return title with trailing 60-200 number removed, or None if no such trailing number."""
    if not title:
        return None
    bm = _TRAILING_BPM_RE.search(title)
    if bm:
        bpm_val = int(bm.group(1))
        if 60 <= bpm_val <= 200:
            return title[: bm.start()].rstrip()
    return None


def _extract_trailing_bpm_key(current: str, extras: dict[str, Any]) -> str:
    changed = True
    while changed:
        changed = False
        km = _TRAILING_KEY_RE.search(current)
        if km:
            extras["key"] = km.group(1).upper()
            current = current[: km.start()].rstrip()
            changed = True
            continue
        bm = _TRAILING_BPM_RE.search(current)
        if bm:
            bpm_val = int(bm.group(1))
            if 60 <= bpm_val <= 200:
                extras["bpm"] = bpm_val
                current = current[: bm.start()].rstrip()
                changed = True
                continue

    if "key" not in extras:
        km = _KEY_ANYWHERE_RE.search(current)
        if km:
            extras["key"] = km.group(1).upper()
            current = f"{current[:km.start()]} {current[km.end():]}"
    return current


def _extract_variant(current: str, extras: dict[str, Any]) -> str:
    vm = _VARIANT_RE.search(current)
    if not vm:
        return current
    v_raw = vm.group(1).lower()
    if "intro clean" in v_raw:
        extras["variant"] = "Intro Clean"
        extras.setdefault("edit_kind", "Clean")
    elif "intro dirty" in v_raw:
        extras["variant"] = "Intro Dirty"
        extras.setdefault("edit_kind", "Dirty")
    elif "hype intro" in v_raw:
        extras["variant"] = "Hype Intro"
    elif "intro" in v_raw:
        extras["variant"] = "Intro"
    elif "acap" in v_raw:
        extras["variant"] = "Acapella"
    elif "inst" in v_raw:
        extras["variant"] = "Instrumental"
    elif "tool" in v_raw:
        extras["variant"] = "DJ Tool"
    return f"{current[:vm.start()]} {current[vm.end():]}"


def _extract_edit_kind(current: str, extras: dict[str, Any]) -> str:
    em = _EDIT_RE.search(current)
    if not em:
        return current
    e_raw = em.group(1).lower()
    if "clean edit" in e_raw:
        extras["edit_kind"] = "Clean Edit"
    elif "dirty edit" in e_raw:
        extras["edit_kind"] = "Dirty Edit"
    elif "clean" in e_raw:
        extras["edit_kind"] = "Clean"
    elif "dirty" in e_raw:
        extras["edit_kind"] = "Dirty"
    elif "explicit" in e_raw:
        extras["edit_kind"] = "Explicit"
    elif "quick hit" in e_raw:
        extras["edit_kind"] = "Quick Hit"
        extras.setdefault("variant", "Quick Hit")
    elif "short edit" in e_raw:
        extras["edit_kind"] = "Short Edit"
        extras.setdefault("variant", "Short Edit")
    return f"{current[:em.start()]} {current[em.end():]}"


def _extract_featured(current: str, extras: dict[str, Any]) -> str:
    fm = _FEAT_RE.search(current)
    if fm:
        extras["featured"] = fm.group(1).strip()
        current = f"{current[:fm.start()]} {current[fm.end():]}"
    return current


_POOL_BRACKET_RE = re.compile(
    r"(?i)\[\s*(?:[a-z0-9_.\s-]*pool|dj\s*city|bpm\s*supreme|direct\s*music\s*service|promo\s*only|www\.[^\]]*)[^\]]*\]"
)


def clean_pool_title(title: str) -> tuple[str, dict[str, Any]]:
    """Clean DJ-pool decorations from title and return (cleaned_title, extras).

    Removes trailing BPM, Camelot/Open keys, edit kinds, variants, featured artist,
    and pool watermarks, while preserving remix/edit/mix names in the title.
    When a trailing 60-200 number is the only pool decoration, the title is returned
    WITHOUT the BPM removed (to avoid stripping real numbers like 'Summer of 69').
    """
    if not title:
        return ("", {})

    extras: dict[str, Any] = {}
    has_pool_bracket = bool(_POOL_BRACKET_RE.search(title))
    current = title.strip()
    current = _WATERMARK_WWW_RE.sub(" ", current)
    current = _WATERMARK_POOL_RE.sub(" ", current)
    current = _extract_trailing_bpm_key(current, extras)
    current = _extract_variant(current, extras)
    current = _extract_edit_kind(current, extras)
    current = _extract_featured(current, extras)
    current = _strip_trailing_bracket_tags(current)
    current = re.sub(r"\s+", " ", current).strip()
    current = re.sub(r"\s*-\s*$", "", current).strip()

    if "bpm" in extras:
        has_other_decorations = (
            "key" in extras
            or "variant" in extras
            or "edit_kind" in extras
            or "featured" in extras
            or has_pool_bracket
        )
        if has_other_decorations:
            extras["bpm_stripped"] = True
        else:
            extras["bpm_stripped"] = False
            current = title.strip()
            current = re.sub(r"\s+", " ", current).strip()
            current = re.sub(r"\s*-\s*$", "", current).strip()

    return (current, extras)


def infer_audio_quality(ext: str, path: str, pack_title: str) -> str:
    """Infer audio quality string understood by parse_release_title."""
    clean_ext = (ext or "").lower()
    if not clean_ext and path:
        clean_ext = Path(path.replace("\\", "/")).suffix.lower()
    if not clean_ext.startswith("."):
        clean_ext = f".{clean_ext}"

    if clean_ext == ".flac":
        return "FLAC"
    elif clean_ext == ".mp3":
        combined = f"{path} {pack_title}"
        if _RE_320.search(combined):
            return "MP3 320"
        if _RE_V0.search(combined):
            return "MP3 V0"
        if _RE_256.search(combined):
            return "MP3 256"
        if _RE_192.search(combined):
            return "MP3 192"
        return "MP3"
    elif clean_ext in (".m4a", ".aac"):
        return "AAC"
    elif clean_ext == ".wav":
        return "WAV"
    elif clean_ext in (".aiff", ".aif"):
        return "AIFF"
    elif clean_ext in (".ogg", ".oga"):
        return "OGG"
    elif clean_ext == ".opus":
        return "Opus"
    elif clean_ext == ".alac":
        return "ALAC"
    return clean_ext.lstrip(".").upper() or "Unknown"


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
    - clean DJ pool decorations (BPM, key, variants, edit kinds, watermarks)
    - keep remix/edit/mix names in the title
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

    extras: dict[str, Any] = {}
    if title:
        title, extras = clean_pool_title(title)
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

    feat = extras.get("featured")
    if feat:
        if artist:
            if not re.search(r"(?i)\b(?:feat|ft|featuring)\b", artist):
                artist = f"{artist} feat. {feat}"
        else:
            artist = f"feat. {feat}"

    final_artist = artist if (artist and artist.strip()) else None
    final_title = title if (title and title.strip()) else None
    return (final_artist, final_title)


def search_text(
    path: str,
    artist: str | None,
    title: str | None,
    extras: dict[str, Any] | None = None,
) -> str:
    """Normalized text for FTS: exclusion_norm applied to artist, title, filename stem, and extras."""
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

    if extras:
        for k in ("bpm", "key", "variant", "edit_kind"):
            val = extras.get(k)
            if val is not None:
                norm_val = exclusion_norm(str(val))
                if norm_val and norm_val not in parts:
                    parts.append(norm_val)

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
    variant: str | None = None
    edit_kind: str | None = None
    bpm: int | None = None
