"""Finds MusicBrainz recordings by artist and title for library tracks AcoustID cannot identify.

Matching is deliberately strict: a wrong recording ID mislinks an album and later renames files, so every
ambiguous case returns no match.
"""

from __future__ import annotations

import itertools
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any, Optional

from trackseerr.clients.mbid_enricher import artist_name_key
from trackseerr.redaction import safe_exc
from trackseerr.release_matching import (
    _album_artist_name,
    _artists_match,
    _extract_owned_rec_ids,
    _missing_recording_tracks,
    _titles_close,
)

logger = logging.getLogger(__name__)

MIN_SCORE = 90
MAX_DURATION_GAP_SECONDS = 5.0
MAX_CONSECUTIVE_UNAVAILABLE = 3

_SINGLES_PREFIX_RE = re.compile(r"^\s*\w+\s*-\s*(?=\d{1,3}\s*(?:-|\.)\s)")
_TRACK_NUMBER_RE = re.compile(
    r"^\s*(?:\d{1,2}-\d{1,3}(?:\s*-\s*|\s+)|[A-Da-d]?\d{1,3}\s+-\s*|[A-Da-d]?\d{2,3}-\s*|[A-Da-d]?\d{1,3}\.\s+)"
)
_ARTIST_PREFIX_RE = re.compile(r"^(?P<artist>[^-]+?)\s+-\s+(?P<rest>.+)$")
_SOUNDTRACK_DASH_RE = re.compile(r"\s+-\s+from\s+the\b.*$", re.IGNORECASE)
_SOUNDTRACK_BRACKET_RE = re.compile(r"\s*[\(\[]\s*from\s[^\)\]]*[\)\]]", re.IGNORECASE)
_FEAT_BRACKET_RE = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s[^\)\]]*[\)\]]", re.IGNORECASE)
_FEAT_TAIL_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def search_title(title: str, artist: str | None = None) -> str:
    """Title cleaned for searching and comparing: track numbers, artist prefix, soundtrack and featured notes removed.

    Version words (remix, live, edit, ...) are kept because the title comparison needs them.
    """
    text = (title or "").strip()
    text = _SINGLES_PREFIX_RE.sub("", text)
    text = _TRACK_NUMBER_RE.sub("", text)
    if artist:
        match = _ARTIST_PREFIX_RE.match(text)
        if match and artist_name_key(match.group("artist")) == artist_name_key(artist):
            text = match.group("rest")
    text = _SOUNDTRACK_DASH_RE.sub("", text)
    text = _SOUNDTRACK_BRACKET_RE.sub("", text)
    text = _FEAT_BRACKET_RE.sub("", text)
    text = _FEAT_TAIL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _artist_ok(candidate: dict[str, Any], track_artist: str | None) -> bool:
    if _artists_match(candidate.get("artist_credit"), track_artist):
        return True
    return any(_artists_match(name, track_artist) for name in candidate.get("artist_names") or [])


def _duration_gap(candidate: dict[str, Any], duration_seconds: float | None) -> float | None:
    length_ms = candidate.get("length_ms")
    if length_ms is None or not duration_seconds:
        return None
    return abs(float(length_ms) / 1000.0 - float(duration_seconds))


def _has_studio_release(candidate: dict[str, Any]) -> bool:
    return any(
        not rg.get("is_various") and rg.get("primary_type") in ("Album", "Single")
        for rg in candidate.get("release_groups") or []
    )


def _passes(
    candidate: dict[str, Any],
    wanted_title: str,
    track_artist: str | None,
    duration_seconds: float | None,
    used_rec_ids: Iterable[str],
) -> bool:
    if int(candidate.get("score") or 0) < MIN_SCORE or candidate.get("id") in used_rec_ids:
        return False
    if not _artist_ok(candidate, track_artist):
        return False
    if not _titles_close(search_title(str(candidate.get("title") or "")), wanted_title):
        return False
    gap = _duration_gap(candidate, duration_seconds)
    return gap is None or gap <= MAX_DURATION_GAP_SECONDS


def pick_search_recording(
    candidates: list[dict[str, Any]],
    track_title: str,
    track_artist: str | None,
    duration_seconds: float | None,
    used_rec_ids: set[str],
) -> dict[str, Any] | None:
    """The one search candidate safe to link to the track, or None when nothing passes every check."""
    wanted_title = search_title(track_title, track_artist)
    passing = [
        c for c in candidates if _passes(c, wanted_title, track_artist, duration_seconds, used_rec_ids)
    ]
    if not passing:
        return None
    if duration_seconds:
        known = [c for c in passing if _duration_gap(c, duration_seconds) is not None]
        if known:
            return min(known, key=lambda c: _duration_gap(c, duration_seconds) or 0.0)
    return max(
        passing,
        key=lambda c: (_has_studio_release(c), int(c.get("score") or 0), len(c.get("release_groups") or [])),
    )


def _track_artist_name(db: Any, track: dict[str, Any], album_artist: str | None) -> str | None:
    for credit in db.list_track_artists(str(track["id"])):
        if credit.get("role") == "primary" and str(credit.get("artist_name") or "").strip():
            return str(credit["artist_name"]).strip()
    return album_artist


def _search_album(
    db: Any, enricher: Any, album: dict[str, Any], counts: dict[str, int], state: dict[str, int]
) -> bool:
    """Fills missing recording IDs on one album by text search. Returns True when MusicBrainz looks down."""
    all_tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    missing = _missing_recording_tracks(all_tracks)
    if not missing:
        return False
    album_artist = _album_artist_name(db, album, all_tracks)
    used_rec_ids = _extract_owned_rec_ids(all_tracks)
    for track in missing:
        counts["tracks"] += 1
        track_artist = _track_artist_name(db, track, album_artist)
        track_title = str(track.get("title") or "")
        query_title = search_title(track_title, track_artist)
        if not track_artist or not query_title:
            counts["no_match"] += 1
            continue
        candidates = enricher.search_recordings(track_artist, query_title)
        if candidates is None:
            counts["unavailable"] += 1
            state["unavailable_run"] += 1
            if state["unavailable_run"] >= MAX_CONSECUTIVE_UNAVAILABLE:
                return True
            continue
        state["unavailable_run"] = 0
        chosen = pick_search_recording(
            candidates, track_title, track_artist, track.get("duration_seconds"), used_rec_ids
        )
        if chosen is None:
            counts["no_match"] += 1
            continue
        rec_id = str(chosen["id"]).strip()
        db.set_track_mb_recording_id(str(track["id"]), rec_id)
        used_rec_ids.add(rec_id)
        counts["filled"] += 1
        logger.info(
            "Filled recording by search for album %s track %r: %s (score=%s)",
            album["id"],
            track_title,
            rec_id,
            chosen.get("score"),
        )
    return False


def fill_recordings_from_search(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Fills missing track mb_recording_id values by artist and title search on albums missing a release."""
    counts = {"tracks": 0, "filled": 0, "no_match": 0, "unavailable": 0, "errors": 0}
    state = {"unavailable_run": 0}

    total: Optional[int] = None
    if on_progress is not None:
        total = db.count_albums_without_release_group() + db.count_albums_missing_release()
        on_progress(0, total)

    albums = itertools.chain(
        db.iter_albums_without_release_group(batch=200),
        db.iter_albums_missing_release(batch=200),
    )
    processed = 0
    for album in albums:
        if should_stop is not None and should_stop():
            logger.info("Recording search fill stopped early: %s", counts)
            return counts
        try:
            if _search_album(db, enricher, album, counts, state):
                logger.warning("Recording search fill stopped: MusicBrainz is not answering")
                return counts
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("Recording search fill failed for album %s: %s", album.get("id"), safe_exc(exc))
        processed += 1
        if on_progress is not None:
            on_progress(processed, total)

    logger.info("Recording search fill finished: %s", counts)
    return counts
