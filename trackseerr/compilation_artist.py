"""Compilation album-artist derivation (soundtrack, MusicBrainz series, title series, label)."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, _is_ranked_list_series

_MIN_SERIES_LEN = 3

_PAREN_SERIES_SUFFIX_RE = re.compile(
    r"\s*[,:\-–—]?\s*[\(\[]\s*(?:vol(?:ume)?\.?|pt\.?|part|no\.?|#)\s*\d{1,4}\s*[\)\]]\s*$",
    re.IGNORECASE,
)
_SERIES_SUFFIX_RE = re.compile(
    r"(?:\s*[,:\-–—]\s*|\s+)(?:(?:vol(?:ume)?\.?|v\.|no\.?|pt\.?|part)\s*\d{1,4}|#\s*\d{1,4}|\d{1,4})\s*$",
    re.IGNORECASE,
)

_SOUNDTRACK_PAREN_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(?:original|sound\s*track|music\s+from|\bost\b)[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)
_SOUNDTRACK_SEP_RE = re.compile(
    r"\s*[:\-–—,]\s*(?:the\s+)?(?:original|sound\s*track|music\s+from|\bost\b).*$",
    re.IGNORECASE,
)
_SOUNDTRACK_WORD_RE = re.compile(
    r"(?:\s+|^)(?:the\s+)?(?:original\s+(?:[\w]+\s+){0,3}?(?:sound\s*track|score|music)|sound\s*track|ost)\s*$",
    re.IGNORECASE,
)
_LEADING_MUSIC_RE = re.compile(
    r"^(?:the\s+music\s+(?:of|from)|music\s+(?:from|of))\s+",
    re.IGNORECASE,
)


def series_from_title(title: str) -> Optional[str]:
    """Strips a trailing volume/number/year and separator when at least 3 characters remain."""
    text = str(title or "").strip()
    if not text:
        return None
    for pattern in (_PAREN_SERIES_SUFFIX_RE, _SERIES_SUFFIX_RE):
        stripped = pattern.sub("", text).strip()
        if stripped != text:
            return stripped if len(stripped) >= _MIN_SERIES_LEN else None
    return None


def soundtrack_name(title: str) -> str:
    """Derives the film/game title from a soundtrack album title."""
    text = str(title or "").strip()
    if not text:
        return ""

    while True:
        prev = text
        for pattern in (_SOUNDTRACK_PAREN_RE, _SOUNDTRACK_SEP_RE, _SOUNDTRACK_WORD_RE):
            text = pattern.sub("", text).strip()
        if text == prev:
            break

    series = series_from_title(text)
    if series:
        text = series

    text = _LEADING_MUSIC_RE.sub("", text).strip()
    return text


def _has_secondary_type(album: dict[str, Any], target: str) -> bool:
    """Returns True when target (casefolded) is present in album['secondary_types']."""
    raw = album.get("secondary_types")
    if raw is None:
        return False
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return False
        try:
            raw = json.loads(stripped)
        except (ValueError, TypeError):
            return stripped.casefold() == target
    if not isinstance(raw, (list, tuple, set)):
        return False
    return any(str(item).strip().casefold() == target for item in raw if item is not None)


def work_out_compilation_artist(enricher: Any, album: dict[str, Any]) -> tuple[str, bool]:
    """Resolves a display album artist for a compilation via soundtrack -> MB series -> title series -> label."""
    title = str(album.get("title") or "")
    local_soundtrack = _has_secondary_type(album, "soundtrack")
    if local_soundtrack:
        st = soundtrack_name(title)
        if st:
            return st, True

    rg_id = str(album.get("mb_release_group_id") or "").strip()
    identity: dict[str, Any] = (
        enricher.get_release_group_identity(rg_id) if (rg_id and enricher is not None) else {}
    ) or {}
    complete = not rg_id or bool(identity.get("complete", True))

    is_soundtrack = local_soundtrack or any(
        str(t).strip().casefold() == "soundtrack" for t in identity.get("secondary_types") or []
    )
    if is_soundtrack:
        st = soundtrack_name(title)
        if st:
            return st, complete

    for raw_series in identity.get("series") or []:
        cleaned = re.sub(r"\s+series$", "", str(raw_series or "").strip(), flags=re.IGNORECASE).strip()
        if cleaned and not _is_ranked_list_series(cleaned):
            return cleaned, complete

    title_series = series_from_title(title)
    if title_series:
        return title_series, complete

    for raw_label in identity.get("labels") or []:
        label = str(raw_label or "").strip()
        if label:
            return label, complete

    return "", complete


def compilation_display_artist(
    db: Any,
    enricher: Any,
    album: dict[str, Any],
    artist: dict[str, Any],
    settings: dict[str, Any],
) -> Optional[str]:
    """Returns the compilation display artist when enabled for a Various Artists album, else None."""
    if not settings or settings.get("compilation_album_artist") != "series_label":
        return None
    if not artist or str(artist.get("mbid") or "").strip().lower() != VARIOUS_ARTISTS_MBID:
        return None

    cached = album.get("compilation_artist")
    if cached is None:
        worked, complete = work_out_compilation_artist(enricher, album)
        if complete:
            album_id = album.get("id")
            if album_id:
                db.set_album_compilation_artist(str(album_id), worked)
            album["compilation_artist"] = worked
        cached = worked
    return str(cached) if cached else None


def populate_various_compilation_artists(db: Any, enricher: Any) -> int:
    """Populates compilation_artist for Various Artists albums where it is currently NULL."""
    va = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    if va is None:
        return 0
    albums = db.list_library_albums(artist_id=str(va["id"]), limit=5000)
    updated = 0
    for album in albums:
        if album.get("compilation_artist") is not None:
            continue
        worked, complete = work_out_compilation_artist(enricher, album)
        if complete or worked:
            db.set_album_compilation_artist(str(album["id"]), worked)
            album["compilation_artist"] = worked
            updated += 1
    return updated


def reset_compilation_artist_cache_once(db: Any, enricher: Any = None) -> bool:
    """Clears cached compilation_artist values once and populates NULL Various Artists albums."""
    was_reset = False
    if db.get_kv("compilation_artist_v2") is None:
        db.clear_album_compilation_artists()
        db.set_kv("compilation_artist_v2", "1")
        was_reset = True

    active_enricher = enricher
    if active_enricher is None:
        from trackseerr.mb_metadata_store import get_shared_enricher

        active_enricher = get_shared_enricher(db)
    populate_various_compilation_artists(db, active_enricher)
    return was_reset
