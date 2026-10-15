"""Observed and resolved artist folder names for library tracks and imports."""

import logging
import os
import sqlite3
from collections import Counter
from typing import Any

from trackseerr.naming import (
    _fixed_artist_folder,
    existing_artist_folder,
    is_various_artists_folder,
)

logger = logging.getLogger(__name__)


def _extract_artist_folder(path: str, norm_root: str) -> str | None:
    """Extracts a valid artist folder name from a file path relative to norm_root."""
    if not path:
        return None
    norm_p = os.path.normpath(str(path))
    try:
        rel = os.path.relpath(norm_p, norm_root)
    except ValueError:
        return None
    if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
        return None
    parts = [seg for seg in rel.replace("\\", "/").split("/") if seg and seg != "."]
    if len(parts) < 2:
        return None
    candidate = _fixed_artist_folder(parts[0])
    if not candidate or is_various_artists_folder(candidate):
        return None
    return candidate


def _break_folder_tie(
    candidates: list[str],
    artist: dict[str, Any] | None,
    artist_id: str,
    db: Any,
    settings: dict[str, Any],
) -> str:
    """Selects the matching existing_artist_folder if present, otherwise alphabetical winner."""
    existing = None
    if artist is None and hasattr(db, "get_library_artist"):
        try:
            artist = db.get_library_artist(artist_id)
        except sqlite3.Error as e:
            logger.warning("Failed to lookup artist %s for tie-breaking: %s", artist_id, e)
            artist = None
    if artist:
        existing = existing_artist_folder(artist, settings)
    if existing and existing in candidates:
        return existing
    return min(candidates)


def observed_artist_folder(
    db: Any,
    artist_id: str,
    settings: dict[str, Any],
    artist: dict[str, Any] | None = None,
) -> str | None:
    """The most common first path segment, relative to settings["root_folder_path"], among the artist's files.

    Ignores files outside the root and the Various Artists folder name.
    Ties resolve to the one matching existing_artist_folder if any, else alphabetical.
    The result is passed through naming._fixed_artist_folder for safety.
    """
    root = str((settings or {}).get("root_folder_path") or "").strip()
    if not root or not artist_id or db is None:
        return None

    if not hasattr(db, "list_artist_file_paths"):
        return None

    paths = db.list_artist_file_paths(artist_id, limit=5000)
    if not paths:
        return None

    norm_root = os.path.normpath(root)
    counts: Counter[str] = Counter()
    for p in paths:
        folder = _extract_artist_folder(p, norm_root)
        if folder:
            counts[folder] += 1

    if not counts:
        return None

    max_count = max(counts.values())
    top_candidates = [folder for folder, count in counts.items() if count == max_count]

    winner = (
        top_candidates[0]
        if len(top_candidates) == 1
        else _break_folder_tie(top_candidates, artist, artist_id, db, settings)
    )
    safe_winner = _fixed_artist_folder(winner)
    return safe_winner or None


def resolve_artist_folder(
    db: Any,
    artist: dict[str, Any] | None,
    settings: dict[str, Any],
) -> str | None:
    """Folder name for an artist's files.

    Returns None when ``rename_artist_folders`` is on (the format renders it);
    otherwise the observed folder among existing library files, falling back to
    ``existing_artist_folder`` when the artist has no files yet or on sqlite3 error.
    """
    if not artist or (settings or {}).get("rename_artist_folders"):
        return None

    try:
        artist_id = str(artist.get("id") or "").strip()
        if artist_id and db is not None:
            observed = observed_artist_folder(db, artist_id, settings, artist=artist)
            if observed:
                return observed
    except sqlite3.Error as e:
        logger.warning(
            "Database error resolving artist folder for artist %s: %s",
            artist.get("id"),
            e,
        )

    return existing_artist_folder(artist, settings)
