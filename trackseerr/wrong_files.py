"""Library-wide AcoustID verification ("Wrong files") service."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any, Callable, Literal, Optional

from trackseerr.compilation_artist import _has_secondary_type
from trackseerr.rename_verification import MISMATCH, start_verification, verdict_for

_NO_KEY_MESSAGE = "Add an AcoustID API key in Settings → Media Management first"
_STOP_WORDS = frozenset({"the", "a", "an", "and", "of", "pt", "part", "pts", "parts"})
_ROMAN_MAP = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
_FEAT_RE = re.compile(r"\s+(?:feat\.|ft\.|featuring)\s+.*")
_PARENS_RE = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")
_DASH_CUT_RE = re.compile(r"\s[-–—]\s.*")
_APOSTROPHE_RE = re.compile(r"['’‘`]")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIX_WORD_RE = re.compile(r"\bmix\b", re.IGNORECASE)


def _tokenize(text: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKD", text).lower()
    text = _APOSTROPHE_RE.sub("", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    tokens = [_ROMAN_MAP.get(tok, tok) for tok in _TOKEN_RE.findall(text)]
    return frozenset(tok for tok in tokens if tok not in _STOP_WORDS)


def _base_tokens(title: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKD", title).lower()
    text = _FEAT_RE.sub("", text)
    text = _PARENS_RE.sub(" ", text)
    text = _DASH_CUT_RE.sub("", text)
    return _tokenize(text)


def _bracket_tokens(title: str) -> list[frozenset[str]]:
    groups = [m.group(1) if m.group(1) is not None else m.group(2) for m in _PARENS_RE.finditer(title)]
    return [toks for g in groups if (toks := _tokenize(g))]


def wrong_file_kind(
    track_title: str,
    actual_title: Optional[str],
) -> Literal["different_song", "different_version"]:
    """Classifies an AcoustID mismatch as a different version of the same song or a different song."""
    if not actual_title:
        return "different_song"
    a = _base_tokens(track_title)
    b = _base_tokens(actual_title)
    if a and b and (a <= b or b <= a):
        return "different_version"
    if (b and any(bt == b for bt in _bracket_tokens(track_title))) or (
        a and any(bt == a for bt in _bracket_tokens(actual_title))
    ):
        return "different_version"
    return "different_song"


def start_wrong_file_check(db: Any) -> bool:
    """Starts a background AcoustID check across every file in the library."""
    key = str(db.get_media_management_settings().get("acoustid_api_key") or "").strip()
    if not key:
        raise ValueError(_NO_KEY_MESSAGE)
    return start_verification(
        db,
        db.list_library_file_ids(),
        key,
        task_id="wrong_files_check",
        task_name="Wrong Files Check",
        thread_name="wrong-files-check",
    )


def _cached_lookup(
    cache: dict[str, Optional[dict[str, Any]]],
    fetcher: Callable[[str], Optional[dict[str, Any]]],
    entity_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if not entity_id:
        return None
    if entity_id not in cache:
        cache[entity_id] = fetcher(entity_id)
    return cache[entity_id]


def _actual_library_fields(
    db: Any,
    actual_track_id: Optional[str],
    albums: dict[str, Optional[dict[str, Any]]],
    artists: dict[str, Optional[dict[str, Any]]],
) -> dict[str, Optional[str]]:
    if not actual_track_id:
        return {"actual_track_title": None, "actual_album_title": None, "actual_artist_name": None}
    actual_track = db.get_library_track(actual_track_id)
    actual_album = _cached_lookup(albums, db.get_library_album, (actual_track or {}).get("album_id"))
    actual_artist_id = (actual_track or {}).get("artist_id") or (actual_album or {}).get("artist_id")
    actual_artist = _cached_lookup(artists, db.get_library_artist, actual_artist_id)
    return {
        "actual_track_title": (actual_track or {}).get("title"),
        "actual_album_title": (actual_album or {}).get("title"),
        "actual_artist_name": (actual_artist or {}).get("name"),
    }


def _is_dj_mix_album(album: Optional[dict[str, Any]]) -> bool:
    if not album:
        return False
    if _has_secondary_type(album, "dj-mix"):
        return True
    album_type = str(album.get("album_type") or "").strip().casefold()
    title = str(album.get("title") or "")
    return album_type == "compilation" and bool(_MIX_WORD_RE.search(title))


def _apply_safety_flags(items: list[dict[str, Any]]) -> int:
    target_counts = Counter(item["actual_track_id"] for item in items if item.get("actual_track_id"))
    safe_count = 0
    for item in items:
        target_id = item.get("actual_track_id")
        claimed = target_counts[target_id] - 1 if target_id else 0
        is_safe = (
            item["kind"] == "different_song"
            and bool(target_id)
            and claimed == 0
            and not item["album_is_mix"]
        )
        item["claimed_by_others"] = claimed
        item["safe"] = is_safe
        if is_safe:
            safe_count += 1
    return safe_count


def _build_item(
    db: Any,
    row: dict[str, Any],
    track: dict[str, Any],
    verdict: dict[str, Any],
    albums: dict[str, Optional[dict[str, Any]]],
    artists: dict[str, Optional[dict[str, Any]]],
) -> dict[str, Any]:
    album = _cached_lookup(albums, db.get_library_album, track.get("album_id"))
    artist_id = track.get("artist_id") or (album or {}).get("artist_id")
    artist = _cached_lookup(artists, db.get_library_artist, artist_id)
    actual_fields = _actual_library_fields(db, verdict.get("actual_track_id"), albums, artists)
    track_title = str(track.get("title") or "")
    actual_title = verdict.get("actual_title") or actual_fields.get("actual_track_title")
    return {
        "file_id": row["file_id"],
        "relative_path": row.get("relative_path"),
        "track_id": row["track_id"],
        "track_title": track_title,
        "album_id": track.get("album_id"),
        "album_title": str((album or {}).get("title") or ""),
        "artist_name": str((artist or {}).get("name") or ""),
        "actual_title": verdict.get("actual_title"),
        "actual_artist": verdict.get("actual_artist"),
        "actual_track_id": verdict.get("actual_track_id"),
        "actual_album_id": verdict.get("actual_album_id"),
        "score": verdict.get("score"),
        "kind": wrong_file_kind(track_title, actual_title),
        "album_is_mix": _is_dj_mix_album(album),
        **actual_fields,
    }


def list_wrong_files(db: Any) -> dict[str, Any]:
    """Lists fingerprinted library files whose audio mismatches their linked track."""
    albums: dict[str, Optional[dict[str, Any]]] = {}
    artists: dict[str, Optional[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    checked = 0

    for row in db.list_fingerprinted_files():
        track = db.get_library_track(row["track_id"])
        if not track:
            continue
        verdict = verdict_for(db, row, track, row["current_path"])
        if verdict is None:
            continue
        checked += 1
        if verdict["status"] != MISMATCH:
            continue
        items.append(_build_item(db, row, track, verdict, albums, artists))

    safe_count = _apply_safety_flags(items)
    items.sort(
        key=lambda item: (
            item["artist_name"].casefold(),
            item["album_title"].casefold(),
            item["track_title"].casefold(),
        )
    )
    different_version = sum(1 for item in items if item["kind"] == "different_version")
    return {
        "items": items,
        "total": len(items),
        "different_song": len(items) - different_version,
        "different_version": different_version,
        "safe_count": safe_count,
        "checked": checked,
        "library_files": len(db.list_library_file_ids()),
    }
