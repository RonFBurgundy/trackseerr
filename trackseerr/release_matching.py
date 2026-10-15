"""MusicBrainz release matching for library albums missing a release ID."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any, Optional

from trackseerr.storage_common import clean_library_name

logger = logging.getLogger(__name__)


def _extract_owned_rec_ids(owned: list[dict[str, Any]]) -> set[str]:
    rec_ids: set[str] = set()
    for t in owned:
        rec = str(t.get("recording_id") or t.get("mb_recording_id") or "").strip()
        if rec:
            rec_ids.add(rec)
    return rec_ids


def _extract_release_rec_ids(release: dict[str, Any]) -> set[str]:
    rec_ids: set[str] = set()
    for medium in release.get("media") or []:
        for trk in medium.get("tracks") or []:
            rec = str(trk.get("recording_id") or "").strip()
            if rec:
                rec_ids.add(rec)
    return rec_ids


def choose_release(
    releases: list[dict[str, Any]],
    owned: list[dict[str, Any]],
    catalog_track_count: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Chooses the best MusicBrainz release in a release group for an album.

    Score = number of owned recording_ids present in the release (primary).
    Tie-breaks in order:
    1. Total track count closest to catalog track count (or owned track count).
    2. Status == "Official".
    3. Earliest release date.
    4. Lexicographical release id.
    When no release contains any owned recording, falls back to: single release -> that one, else None.
    """
    if not releases:
        return None

    owned_rec_ids = _extract_owned_rec_ids(owned)
    target_count = int(catalog_track_count) if catalog_track_count is not None else len(owned)

    scored: list[tuple[int, dict[str, Any]]] = []
    for rel in releases:
        rel_rec_ids = _extract_release_rec_ids(rel)
        score = len(owned_rec_ids & rel_rec_ids) if owned_rec_ids else 0
        scored.append((score, rel))

    max_score = max(s[0] for s in scored)
    if max_score == 0:
        if len(releases) == 1:
            return releases[0]
        return None

    candidates = [rel for score, rel in scored if score == max_score]

    def _sort_key(rel: dict[str, Any]) -> tuple[Any, ...]:
        tc = int(rel.get("track_count") or 0)
        tc_dist = abs(tc - target_count)
        is_official = 0 if str(rel.get("status") or "").strip().lower() == "official" else 1
        raw_date = str(rel.get("date") or "").strip()
        has_date = 0 if raw_date else 1
        rel_id = str(rel.get("id") or "")
        return (tc_dist, is_official, has_date, raw_date, rel_id)

    return min(candidates, key=_sort_key)


def _fill_missing_recordings(
    db: Any, chosen_release: dict[str, Any], owned_tracks: list[dict[str, Any]]
) -> int:
    """Fills mb_recording_id for owned tracks missing one when position and cleaned title match."""
    release_tracks_by_pos: dict[tuple[int, int], dict[str, Any]] = {}
    for medium in chosen_release.get("media") or []:
        try:
            med_disc = int(medium.get("position") or 1)
        except (ValueError, TypeError):
            med_disc = 1
        for trk in medium.get("tracks") or []:
            try:
                trk_pos = int(trk.get("position") or 1)
            except (ValueError, TypeError):
                trk_pos = 1
            release_tracks_by_pos[(med_disc, trk_pos)] = trk

    filled = 0
    for t in owned_tracks:
        if t.get("mb_recording_id") or t.get("recording_id"):
            continue
        try:
            disc = int(t.get("disc_number") or 1)
        except (ValueError, TypeError):
            disc = 1
        try:
            pos = int(t.get("track_number") or 1)
        except (ValueError, TypeError):
            pos = 1

        match_trk = release_tracks_by_pos.get((disc, pos))
        if not match_trk or not match_trk.get("recording_id"):
            continue

        t_clean = clean_library_name(str(t.get("title") or ""))
        r_clean = clean_library_name(str(match_trk.get("title") or ""))
        if t_clean and t_clean == r_clean:
            db.set_track_mb_recording_id(str(t["id"]), str(match_trk["recording_id"]))
            t["mb_recording_id"] = match_trk["recording_id"]
            filled += 1

    return filled


def _match_one_album(
    db: Any,
    enricher: Any,
    album: dict[str, Any],
    counts: dict[str, int],
) -> None:
    raw_tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    owned = [t for t in raw_tracks if t.get("has_file")]
    if not owned and not any("has_file" in t for t in raw_tracks):
        owned = raw_tracks

    rg_id = str(album.get("mb_release_group_id") or "").strip()
    if not rg_id:
        counts["no_releases"] += 1
        return

    releases = enricher.get_release_group_releases(rg_id)
    if not releases:
        counts["no_releases"] += 1
        return

    owned_payload = [
        {
            "id": t.get("id"),
            "recording_id": t.get("mb_recording_id") or t.get("recording_id"),
            "disc_number": t.get("disc_number", 1),
            "track_number": t.get("track_number", 1),
            "title": t.get("title", ""),
        }
        for t in owned
    ]

    chosen = choose_release(releases, owned_payload, catalog_track_count=album.get("total_tracks"))
    if chosen is None:
        counts["no_match"] += 1
        return

    db.set_album_mb_release_id(str(album["id"]), str(chosen["id"]))
    counts["matched"] += 1
    counts["recordings_filled"] += _fill_missing_recordings(db, chosen, owned)


def match_album_releases(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, int]:
    """Iterates library albums missing a release ID and assigns the best release from MusicBrainz."""
    counts = {
        "albums": 0,
        "matched": 0,
        "no_match": 0,
        "no_releases": 0,
        "recordings_filled": 0,
        "errors": 0,
    }

    for album in db.iter_albums_missing_release(batch=200):
        if should_stop is not None and should_stop():
            logger.info("MusicBrainz release match stopped early: %s", counts)
            return counts
        try:
            _match_one_album(db, enricher, album, counts)
            counts["albums"] += 1
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("MusicBrainz release match failed for album %s: %s", album.get("id"), exc)

    logger.info("MusicBrainz release match finished: %s", counts)
    return counts
