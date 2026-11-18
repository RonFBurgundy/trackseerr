"""Suggests MusicBrainz matches for albums Self-Heal could not link, and applies the one an admin picks.

Suggestions only read (library and MusicBrainz). Applying changes catalog rows only; files on disk are never touched
(rename and retag are separate, user-confirmed steps).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Optional

import requests

from trackseerr.album_titles import album_titles_match, strip_edition
from trackseerr.compilation_filing import (
    ensure_release_group_album,
    file_album_under_various,
    various_artists_id,
)
from trackseerr.recording_search import (
    MAX_DURATION_GAP_SECONDS,
    _artist_ok,
    _duration_gap,
    _track_artist_name,
    search_title,
)
from trackseerr.redaction import safe_exc
from trackseerr.release_matching import _album_artist_name, _match_one_album, _merge_into_holder, _titles_close

logger = logging.getLogger(__name__)

LOOSE_SCORE = 70
COMPILATION_PENALTY = 20
VARIOUS_NAME = "Various Artists"


class _MusicBrainzUnavailable(Exception):
    """A MusicBrainz text search failed transiently."""


def _searched(result: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if result is None:
        raise _MusicBrainzUnavailable()
    return result


def _group_fields(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(group.get("id") or ""),
        "title": str(group.get("title") or ""),
        "primary_type": group.get("primary_type"),
        "secondary_types": list(group.get("secondary_types") or []),
        "first_release_date": group.get("first_release_date"),
        "artist_credit": group.get("artist_credit"),
        "is_various": bool(group.get("is_various")),
    }


def _loose_recording_groups(enricher: Any, track: dict[str, Any], artist: Optional[str]) -> list[dict[str, Any]]:
    """Release groups of the first search hit that clearly is this untagged track (looser bar than Self-Heal)."""
    wanted = search_title(str(track.get("title") or ""), artist)
    if not artist or not wanted:
        return []
    duration = track.get("duration_seconds")
    for cand in _searched(enricher.search_recordings(artist, wanted)):
        if int(cand.get("score") or 0) < LOOSE_SCORE or not _artist_ok(cand, artist):
            continue
        if not _titles_close(search_title(str(cand.get("title") or "")), wanted):
            continue
        gap = _duration_gap(cand, duration)
        if gap is not None and gap > MAX_DURATION_GAP_SECONDS:
            continue
        return list(cand.get("release_groups") or [])
    return []


def _collect_groups(
    enricher: Any,
    tracks: list[dict[str, Any]],
    artist: Optional[str],
    clean_title: str,
    track_artists: dict[str, Optional[str]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Candidate release groups by id, plus the ids that come from the tracks' own recordings."""
    groups: dict[str, dict[str, Any]] = {}
    known: set[str] = set()
    for track in tracks:
        rec = str(track.get("mb_recording_id") or "").strip()
        if not rec:
            continue
        for group in enricher.get_recording_release_groups(rec) or []:
            if group.get("id"):
                groups.setdefault(str(group["id"]), group)
                known.add(str(group["id"]))
    searches = [(artist, clean_title), (VARIOUS_NAME, clean_title)] if artist else [(VARIOUS_NAME, clean_title)]
    for name, title in searches:
        for group in _searched(enricher.search_release_groups(name, title)):
            if group.get("id"):
                groups.setdefault(str(group["id"]), group)
    for track in tracks:
        if str(track.get("mb_recording_id") or "").strip():
            continue
        for group in _loose_recording_groups(enricher, track, track_artists.get(str(track["id"]))):
            if group.get("id"):
                groups.setdefault(str(group["id"]), group)
    return groups, known


def _durations_agree(track: dict[str, Any], release_track: dict[str, Any]) -> Optional[bool]:
    mine, theirs = track.get("duration_seconds"), release_track.get("duration_seconds")
    if not mine or not theirs:
        return None
    return abs(float(mine) - float(theirs)) <= MAX_DURATION_GAP_SECONDS


def _map_track(
    track: dict[str, Any], release_tracks: list[dict[str, Any]], used: set[int]
) -> Optional[tuple[int, dict[str, Any]]]:
    rec = str(track.get("mb_recording_id") or "").strip()
    if rec:
        for idx, rt in enumerate(release_tracks):
            if idx not in used and rt.get("mb_recording_id") == rec:
                return idx, rt
    wanted = search_title(str(track.get("title") or ""))
    for idx, rt in enumerate(release_tracks):
        if idx in used or not _titles_close(search_title(str(rt.get("title") or "")), wanted):
            continue
        if _durations_agree(track, rt) is False:
            continue
        return idx, rt
    return None


def _map_tracks(tracks: list[dict[str, Any]], release_tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used: set[int] = set()
    rows: list[dict[str, Any]] = []
    for track in tracks:
        found = _map_track(track, release_tracks, used)
        row: dict[str, Any] = {
            "track_id": str(track["id"]),
            "title": str(track.get("title") or ""),
            "has_file": bool(track.get("has_file")),
            "release_track_title": None,
            "release_track_number": None,
            "recording_id": None,
        }
        if found is not None:
            used.add(found[0])
            rt = found[1]
            row.update(
                release_track_title=rt.get("title"),
                release_track_number=rt.get("track_number"),
                recording_id=rt.get("mb_recording_id") or None,
                _duration_ok=_durations_agree(track, rt),
            )
        rows.append(row)
    return rows


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _score(rows: list[dict[str, Any]], files: int, title_match: bool, from_recordings: bool) -> tuple[int, list[str]]:
    mapped = [r for r in rows if r["release_track_title"] is not None and r["has_file"]]
    comparable = [r for r in mapped if r.get("_duration_ok") is not None]
    agree = sum(1 for r in comparable if r["_duration_ok"])
    share = (len(mapped) / files) if files else 0.0
    duration_share = (agree / len(comparable)) if comparable else 0.0
    confidence = round(
        40 * share + 30 * (1 if title_match else 0) + 20 * duration_share + 10 * (1 if from_recordings else 0)
    )
    if files and len(mapped) == files:
        reasons = [f"All {_plural(files, 'file')} found on this release"]
    else:
        reasons = [f"{len(mapped)} of {_plural(files, 'file')} found on this release"]
    if title_match:
        reasons.append("Album title matches")
    if comparable and agree == len(comparable):
        reasons.append("Track lengths agree")
    if from_recordings:
        reasons.append("Contains recordings already identified in your files")
    return min(100, confidence), reasons


def _build_candidate(
    enricher: Any,
    group: dict[str, Any],
    tracks: list[dict[str, Any]],
    files: int,
    album_artist: str,
    clean_title: str,
    from_recordings: bool,
) -> dict[str, Any]:
    release_tracks = enricher.get_release_group_tracks(str(group["id"])) or []
    rows = _map_tracks(tracks, release_tracks)
    title_match = album_titles_match(group.get("title"), clean_title)
    confidence, reasons = _score(rows, files, title_match, from_recordings)
    various = bool(group.get("is_various"))
    if various:
        reasons.append("Released as a compilation by Various Artists")
        if not title_match:
            confidence = max(0, confidence - COMPILATION_PENALTY)
            reasons.append("Found only on a Various Artists compilation")
    mapped = sum(1 for r in rows if r["release_track_title"] is not None)
    for row in rows:
        row.pop("_duration_ok", None)
    candidate = _group_fields(group)
    candidate.update(
        target_artist=VARIOUS_NAME if various else album_artist,
        mapped=mapped,
        track_count=len(release_tracks),
        track_map=rows,
        confidence=confidence,
        reasons=reasons,
    )
    return candidate


def _sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, str, str]:
    secondary = candidate.get("secondary_types") or []
    own_rank = 0 if (not candidate.get("is_various") and "Compilation" not in secondary) else 1
    primary = candidate.get("primary_type")
    if not secondary and primary == "Album":
        type_rank = 0
    elif not secondary and primary in ("Single", "EP"):
        type_rank = 1
    else:
        type_rank = 2
    return (
        -int(candidate["confidence"]),
        own_rank,
        type_rank,
        str(candidate.get("first_release_date") or "9999"),
        str(candidate["id"]),
    )


def suggest_matches(db: Any, enricher: Any, album_id: str, limit: int = 5) -> dict[str, Any]:
    """Ranked MusicBrainz release groups an unlinked album could be, with how each file would map. Never writes."""
    album = db.get_library_album(album_id)
    if album is None:
        raise ValueError("Album not found")
    tracks = db.list_library_tracks(album_id=album_id, limit=1000)
    files = [t for t in tracks if t.get("has_file")] or tracks
    artist = _album_artist_name(db, album, tracks) or ""
    summary = {
        "id": str(album["id"]),
        "title": str(album.get("title") or ""),
        "artist_name": artist,
        "track_count": len(tracks),
    }
    raw_title = str(album.get("title") or "")
    clean_title = strip_edition(raw_title) or raw_title
    track_artists = {str(t["id"]): _track_artist_name(db, t, artist or None) for t in tracks}
    try:
        groups, known = _collect_groups(enricher, files, artist or None, clean_title, track_artists)
    except _MusicBrainzUnavailable:
        logger.info("MusicBrainz unavailable while suggesting matches for album %s", album_id)
        return {"album": summary, "candidates": [], "unavailable": True}
    candidates = [
        _build_candidate(enricher, group, tracks, len(files), artist, clean_title, gid in known)
        for gid, group in groups.items()
    ]
    candidates.sort(key=_sort_key)
    return {"album": summary, "candidates": candidates[: max(1, limit)], "unavailable": False}


def _set_recordings(db: Any, album_id: str, track_recordings: dict[str, str]) -> None:
    own = {str(t["id"]) for t in db.list_library_tracks(album_id=album_id, limit=1000)}
    for track_id, rec_id in track_recordings.items():
        if str(track_id) in own and str(rec_id).strip():
            db.set_track_mb_recording_id(str(track_id), str(rec_id).strip())


def apply_match(
    db: Any, enricher: Any, album_id: str, group: dict[str, Any], track_recordings: dict[str, str]
) -> dict[str, Any]:
    """Links the album to the chosen release group (folding it into an existing album when one holds it).

    Raises ``ValueError`` for an unknown album, one that already has a release group, or a group without an id.
    """
    album = db.get_library_album(album_id)
    if album is None:
        raise ValueError("Album not found")
    if str(album.get("mb_release_group_id") or "").strip():
        raise ValueError("Album already has a MusicBrainz release group")
    rg_id = str(group.get("id") or "").strip()
    if not rg_id:
        raise ValueError("Release group id is required")

    _set_recordings(db, album_id, track_recordings)
    counts: dict[str, int] = defaultdict(int)
    final_id = album_id
    try:
        if group.get("is_various"):
            file_album_under_various(db, enricher, album, group, counts)
            holder = db.album_holding_release_group(various_artists_id(db), rg_id, "")
            final_id = str(holder) if counts["merged"] and holder else album_id
        elif db.album_holding_release_group(str(album["artist_id"]), rg_id, album_id) is not None:
            holder_id = ensure_release_group_album(db, enricher, str(album["artist_id"]), group)
            _merge_into_holder(db, album_id, holder_id, counts)
            final_id = holder_id if counts["merged"] else album_id
        else:
            db.set_album_mb_release_group_id(album_id, rg_id)
            linked = db.get_library_album(album_id) or album
            _match_one_album(db, enricher, linked, counts)
    except requests.RequestException as exc:
        logger.warning("Applying match for album %s failed: %s", album_id, safe_exc(exc))
        raise
    return {"album_id": final_id, "merged": counts["merged"] > 0, "conflict": counts["merge_conflict"] > 0}


SURE_CONFIDENCE = 90
SURE_MARGIN = 10
MIN_SUGGEST_CONFIDENCE = 30
MAX_CONSECUTIVE_UNAVAILABLE = 3
_AUTO_KEYS = ("albums", "applied", "suggested", "no_candidates", "unavailable", "conflicts", "errors")


def _album_file_count(db: Any, album_id: str) -> int:
    tracks = db.list_library_tracks(album_id=album_id, limit=1000)
    return len([t for t in tracks if t.get("has_file")] or tracks)


def _is_sure(candidates: list[dict[str, Any]], file_count: int) -> bool:
    """The best candidate is certain: high confidence, every file mapped, and no close runner-up."""
    best = candidates[0]
    if file_count <= 0 or int(best["confidence"]) < SURE_CONFIDENCE or int(best["mapped"]) != file_count:
        return False
    runner_up = next((c for c in candidates[1:] if c["id"] != best["id"]), None)
    return runner_up is None or int(best["confidence"]) - int(runner_up["confidence"]) >= SURE_MARGIN


def _recordings_from(candidate: dict[str, Any]) -> dict[str, str]:
    return {
        str(row["track_id"]): str(row["recording_id"])
        for row in candidate.get("track_map") or []
        if row.get("recording_id")
    }


def _auto_one(db: Any, enricher: Any, album_id: str, counts: dict[str, int]) -> bool:
    """Handles one album; returns True when MusicBrainz was unavailable for it."""
    result = suggest_matches(db, enricher, album_id, limit=3)
    if result.get("unavailable"):
        counts["unavailable"] += 1
        return True
    candidates = result["candidates"]
    if (
        not candidates
        or int(candidates[0]["confidence"]) < MIN_SUGGEST_CONFIDENCE
        or int(candidates[0]["mapped"]) == 0
    ):
        db.delete_match_suggestion(album_id)
        counts["no_candidates"] += 1
        return False
    file_count = _album_file_count(db, album_id)
    best = candidates[0]
    if not _is_sure(candidates, file_count):
        db.save_match_suggestion(album_id, best, int(best["mapped"]), file_count)
        counts["suggested"] += 1
        return False
    outcome = apply_match(db, enricher, album_id, best, _recordings_from(best))
    db.delete_match_suggestion(album_id)
    counts["conflicts" if outcome["conflict"] else "applied"] += 1
    return False


def auto_match_unlinked(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Links unlinked albums whose best MusicBrainz match is certain and stores the rest for bulk review."""
    counts: dict[str, int] = dict.fromkeys(_AUTO_KEYS, 0)
    album_ids = [str(a["id"]) for a in db.iter_albums_without_release_group(batch=200)]
    consecutive_down = 0
    for index, album_id in enumerate(album_ids):
        if should_stop is not None and should_stop():
            break
        counts["albums"] += 1
        try:
            down = _auto_one(db, enricher, album_id, counts)
        except (sqlite3.Error, requests.RequestException, ValueError) as exc:
            logger.warning("Automatic match for album %s failed: %s", album_id, safe_exc(exc))
            counts["errors"] += 1
            down = False
        consecutive_down = consecutive_down + 1 if down else 0
        if on_progress is not None:
            on_progress(index + 1, len(album_ids))
        if consecutive_down >= MAX_CONSECUTIVE_UNAVAILABLE:
            logger.warning("MusicBrainz unavailable %d times in a row; stopping automatic matching", consecutive_down)
            break
    return counts


def _apply_stored_one(db: Any, enricher: Any, album_id: str) -> dict[str, Any]:
    stored = db.get_match_suggestion(album_id)
    if stored is None:
        return {"album_id": album_id, "final_album_id": album_id, "status": "missing", "message": "No stored suggestion"}
    candidate = stored["candidate"]
    try:
        outcome = apply_match(db, enricher, album_id, candidate, _recordings_from(candidate))
    except (ValueError, requests.RequestException, sqlite3.Error) as exc:
        logger.warning("Applying stored match for album %s failed: %s", album_id, safe_exc(exc))
        return {"album_id": album_id, "final_album_id": album_id, "status": "error", "message": safe_exc(exc)}
    db.delete_match_suggestion(album_id)
    status = "conflict" if outcome["conflict"] else "applied"
    return {"album_id": album_id, "final_album_id": outcome["album_id"], "status": status}


def apply_stored_suggestions(db: Any, enricher: Any, album_ids: list[str]) -> list[dict[str, Any]]:
    """Applies the stored candidate of each album; a successful apply (conflict included) clears the suggestion."""
    return [_apply_stored_one(db, enricher, str(album_id)) for album_id in album_ids]
