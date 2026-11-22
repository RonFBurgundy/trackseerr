"""Selects the single audio file matching a requested track from a multi-file .torrent."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any

from trackseerr.acquisition_import import (
    _SINGLE_VARIANT_RE,
    _strip_any_artist_prefix,
    _strip_artist_prefix,
    _strip_leading_track_number,
)
from trackseerr.bencode import BencodeError, decode, torrent_files
from trackseerr.clients.acquisition import torznab
from trackseerr.clients.acquisition.torznab import IndexerError, IndexerUnavailable
from trackseerr.library import AUDIO_EXTENSIONS
from trackseerr.lidarr_release import titles_match

logger = logging.getLogger(__name__)


def _file_candidate_title(stem: str, artist: str | None) -> str:
    """Extracts a comparable track title from a filename stem."""
    cleaned = _strip_leading_track_number(stem)
    if artist:
        stripped = _strip_artist_prefix(cleaned, artist)
        if stripped != cleaned:
            cleaned = _strip_leading_track_number(stripped) if cleaned == stem else stripped
    return cleaned


def _fallback_other_artist_entry(
    audio_entries: list[dict[str, Any]],
    wanted_title: str,
    artist: str | None,
    wanted_has_single_variant: bool,
) -> tuple[dict[str, Any] | None, str]:
    tier1_plain: list[dict[str, Any]] = []
    tier2_variant: list[dict[str, Any]] = []

    for entry in audio_entries:
        stem = PurePosixPath(entry["path"]).stem
        raw_cand = _file_candidate_title(stem, artist)
        cand = _strip_any_artist_prefix(raw_cand)
        if cand == raw_cand or not titles_match(cand, wanted_title):
            continue

        cand_has_single_variant = bool(
            _SINGLE_VARIANT_RE.search(cand) or _SINGLE_VARIANT_RE.search(stem)
        )
        if wanted_has_single_variant or not cand_has_single_variant:
            tier1_plain.append(entry)
        else:
            tier2_variant.append(entry)

    matches = tier1_plain if tier1_plain else tier2_variant
    if len(matches) == 1:
        return matches[0], "selected"
    if not matches:
        return None, "no_match"
    return None, "ambiguous"


def select_track_file(
    torrent_bytes: bytes,
    wanted_title: str,
    artist: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """Selects the single audio file matching wanted_title from torrent_bytes.

    Returns (entry, status) where status is one of:
    'selected', 'single_file', 'no_match', or 'ambiguous'.
    """
    try:
        meta = decode(torrent_bytes)
        if not isinstance(meta, dict):
            return None, "no_match"
        all_files = torrent_files(meta)
    except BencodeError as exc:
        logger.info("Could not decode torrent metadata for track selection: %s", exc)
        return None, "no_match"

    audio_entries = [
        entry
        for entry in all_files
        if PurePosixPath(entry["path"]).suffix.lower() in AUDIO_EXTENSIONS
    ]
    if len(audio_entries) == 1:
        return None, "single_file"

    wanted_has_single_variant = bool(_SINGLE_VARIANT_RE.search(wanted_title))
    tier1_plain: list[dict[str, Any]] = []
    tier2_variant: list[dict[str, Any]] = []

    for entry in audio_entries:
        stem = PurePosixPath(entry["path"]).stem
        cand_title = _file_candidate_title(stem, artist)
        if not titles_match(cand_title, wanted_title):
            continue

        cand_has_single_variant = bool(
            _SINGLE_VARIANT_RE.search(cand_title) or _SINGLE_VARIANT_RE.search(stem)
        )
        if wanted_has_single_variant or not cand_has_single_variant:
            tier1_plain.append(entry)
        else:
            tier2_variant.append(entry)

    matches = tier1_plain if tier1_plain else tier2_variant
    if len(matches) == 1:
        return matches[0], "selected"
    if not matches:
        return _fallback_other_artist_entry(
            audio_entries, wanted_title, artist, wanted_has_single_variant
        )
    return None, "ambiguous"


def fetch_torrent_bytes(result: Any, timeout: float = 15.0) -> bytes | None:
    """Fetches raw .torrent bytes for a search result, or None for magnets/errors."""
    url = getattr(result, "download_url", None)
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return None

    try:
        data = torznab.fetch_torrent_file(url, timeout)
    except (IndexerUnavailable, IndexerError) as exc:
        logger.info(
            "Could not fetch .torrent file for '%s': %s",
            getattr(result, "title", ""),
            exc,
        )
        return None

    return data if isinstance(data, bytes) else None


__all__ = ["fetch_torrent_bytes", "select_track_file"]
