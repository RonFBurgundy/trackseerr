"""Files compilation tracks under Various Artists and stray singles under their real release.

Everything here is keyed on a known MusicBrainz recording id; nothing is guessed from titles alone, and no file on
disk is touched (renames happen later through the rename path).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from typing import Any, Optional

import requests

from trackseerr.album_titles import album_titles_match
from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID
from trackseerr.models import LibraryAlbum, LibraryArtist
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)

_PRIMARY_RANK = {"album": 0, "single": 1, "ep": 2}


def various_artists_id(db: Any) -> str:
    """Library id of the Various Artists artist, created unmonitored when the library has none."""
    existing = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    if existing is not None:
        return str(existing["id"])
    created = db.upsert_library_artist(
        LibraryArtist(
            id=str(uuid.uuid4()),
            name="Various Artists",
            mbid=VARIOUS_ARTISTS_MBID,
            monitored=False,
            monitor_option="none",
        )
    )
    return str(created["id"])


def _album_type_for(group: dict[str, Any]) -> str:
    secondary = [str(s).casefold() for s in group.get("secondary_types") or []]
    if "compilation" in secondary:
        return "compilation"
    return str(group.get("primary_type") or "album").casefold()


def ensure_release_group_album(db: Any, enricher: Any, artist_id: str, group: dict[str, Any]) -> str:
    """Id of the artist's album for a release group, created unmonitored (with its tracklist) when missing."""
    from trackseerr.album_track_hydration import hydrate_album_tracks

    rg_id = str(group["id"])
    holder = db.album_holding_release_group(artist_id, rg_id, "")
    if holder is not None:
        return str(holder)
    date = str(group.get("first_release_date") or "")
    year = int(date[:4]) if date[:4].isdigit() else None
    album = db.upsert_library_album(
        LibraryAlbum(
            id=str(uuid.uuid4()),
            artist_id=artist_id,
            title=str(group.get("title") or "Unknown Album"),
            year=year,
            album_type=_album_type_for(group),
            secondary_types=list(group.get("secondary_types") or []),
            mb_release_group_id=rg_id,
            monitored=False,
        )
    )
    album_id = str(album["id"])
    hydrate_album_tracks(db, enricher, album_id)
    return album_id


def _group_sort_key(group: dict[str, Any]) -> tuple[int, str, str]:
    primary = str(group.get("primary_type") or "").casefold()
    return (
        _PRIMARY_RANK.get(primary, 3),
        str(group.get("first_release_date") or "9999-99-99"),
        str(group.get("id") or ""),
    )


def _secondary_allowed(group: dict[str, Any], version_words: set[str]) -> bool:
    secondary = {str(s).casefold() for s in group.get("secondary_types") or []}
    if not secondary:
        return True
    allowed: set[str] = set()
    if "live" in version_words:
        allowed.add("live")
    if version_words & {"remix", "mix"}:
        allowed.add("remix")
    return secondary <= allowed


def home_release_group(
    groups: list[dict[str, Any]], artist_mbid: Optional[str], track_title: str
) -> Optional[dict[str, Any]]:
    """Canonical non-various release group of a recording (album, then single, then EP; earliest first)."""
    from trackseerr.release_matching import _version_words

    words = _version_words(track_title or "")
    mbid = str(artist_mbid or "").strip().lower()
    candidates = []
    for group in groups:
        if group.get("is_various") or not group.get("id"):
            continue
        if mbid and mbid not in {str(a).lower() for a in group.get("artist_ids") or []}:
            continue
        if not _secondary_allowed(group, words):
            continue
        candidates.append(group)
    return min(candidates, key=_group_sort_key) if candidates else None


def various_group_for_title(groups: list[dict[str, Any]], album_title: Optional[str]) -> Optional[dict[str, Any]]:
    """The Various Artists release group whose title matches the album title (earliest on ties)."""
    if not album_title:
        return None
    matches = [
        g for g in groups if g.get("is_various") and g.get("id") and album_titles_match(g.get("title"), album_title)
    ]
    if not matches:
        return None
    return min(matches, key=lambda g: (str(g.get("first_release_date") or "9999-99-99"), str(g.get("id"))))


def ensure_primary_credit(db: Any, track_id: str, artist_name: str, artist_mbid: Optional[str]) -> None:
    """Writes a primary credit for a track that has none (compilation file names use it)."""
    if not artist_name or db.list_track_artists(track_id):
        return
    db.replace_track_artists(
        track_id,
        [
            {
                "artist_name": artist_name,
                "artist_mbid": artist_mbid or None,
                "role": "primary",
                "position": 0,
                "source": "tags",
            }
        ],
    )


def credit_filed_tracks(
    db: Any, holder_album_id: str, track_ids: set[str], rec_ids: set[str], artist_name: str, artist_mbid: Optional[str]
) -> None:
    """Gives the tracks that just landed in a Various Artists album their original artist as primary credit."""
    for track in db.list_library_tracks(album_id=holder_album_id, limit=1000):
        tid = str(track["id"])
        rec = str(track.get("mb_recording_id") or "").strip()
        if tid in track_ids or (rec and rec in rec_ids and track.get("has_file")):
            ensure_primary_credit(db, tid, artist_name, artist_mbid)


def album_artist_credit(db: Any, album: dict[str, Any]) -> tuple[str, Optional[str]]:
    """Name and MBID of the artist an album currently sits under."""
    artist = db.get_library_artist(str(album["artist_id"])) or {}
    return str(artist.get("name") or ""), artist.get("mbid")


def file_album_under_various(
    db: Any, enricher: Any, album: dict[str, Any], group: dict[str, Any], counts: dict[str, int]
) -> bool:
    """Folds an album into the Various Artists album of ``group``; True when merged."""
    from trackseerr.release_matching import _merge_into_holder

    holder = ensure_release_group_album(db, enricher, various_artists_id(db), group)
    name, mbid = album_artist_credit(db, album)
    tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    track_ids = {str(t["id"]) for t in tracks}
    rec_ids = {str(t["mb_recording_id"]).strip() for t in tracks if t.get("mb_recording_id")}
    before = counts["merged"]
    _merge_into_holder(db, str(album["id"]), holder, counts)
    merged = counts["merged"] > before
    if merged:
        credit_filed_tracks(db, holder, track_ids, rec_ids, name, mbid)
        counts["filed_compilation"] = counts.get("filed_compilation", 0) + 1
    return merged


def file_tracks_under_home(
    db: Any,
    enricher: Any,
    album: dict[str, Any],
    per_rec_groups: dict[str, list[dict[str, Any]]],
    counts: dict[str, int],
) -> bool:
    """Moves each recording-known track of a small album into the album of its canonical release.

    Returns True when at least one track was moved or a conflict was recorded.
    """
    from trackseerr.artist_repair import move_track_into_album

    artist_id = str(album["artist_id"])
    artist = db.get_library_artist(artist_id) or {}
    acted = False
    for track in db.list_library_tracks(album_id=album["id"], limit=1000):
        rec = str(track.get("mb_recording_id") or "").strip()
        if not rec or not track.get("has_file"):
            continue
        home = home_release_group(per_rec_groups.get(rec, []), artist.get("mbid"), str(track.get("title") or ""))
        if home is None:
            continue
        holder = ensure_release_group_album(db, enricher, artist_id, home)
        if holder == str(album["id"]):
            continue
        acted = True
        if move_track_into_album(db, str(track["id"]), holder) == "merged":
            counts["filed_home"] = counts.get("filed_home", 0) + 1
        else:
            db.record_merge_conflict(str(album["id"]), holder, "duplicate_files")
            counts["merge_conflict"] += 1
    return acted


def _various_group_of_album(db: Any, enricher: Any, album: dict[str, Any]) -> Optional[dict[str, Any]]:
    rg_id = str(album.get("mb_release_group_id") or "").strip()
    for track in db.list_library_tracks(album_id=album["id"], limit=1000):
        rec = str(track.get("mb_recording_id") or "").strip()
        if not rec or not track.get("has_file"):
            continue
        for group in enricher.get_recording_release_groups(rec):
            if group.get("is_various") and str(group.get("id")) == rg_id:
                return group
    return None


def _refile_one_album(db: Any, enricher: Any, album: dict[str, Any], va_id: str, counts: dict[str, int]) -> None:
    group = _various_group_of_album(db, enricher, album)
    if group is None:
        return
    counts["albums"] += 1
    if db.album_holding_release_group(va_id, str(group["id"]), str(album["id"])) is not None:
        file_album_under_various(db, enricher, album, group, counts)
        return
    name, mbid = album_artist_credit(db, album)
    for track in db.list_library_tracks(album_id=album["id"], limit=1000):
        ensure_primary_credit(db, str(track["id"]), name, mbid)
    db.move_album_to_artist(str(album["id"]), va_id)
    counts["moved"] += 1


def refile_various_albums(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Moves linked compilation albums that sit under a track artist to Various Artists."""
    counts = {"albums": 0, "moved": 0, "merged": 0, "merge_conflict": 0, "errors": 0}
    va_id = various_artists_id(db)
    candidates = db.list_albums_with_files_outside_artist(va_id)
    total = len(candidates)
    if on_progress is not None:
        on_progress(0, total)
    for done, album in enumerate(candidates, start=1):
        if should_stop is not None and should_stop():
            logger.info("Various Artists refile stopped early: %s", counts)
            return counts
        try:
            _refile_one_album(db, enricher, album, va_id, counts)
        except (sqlite3.Error, requests.RequestException) as exc:
            counts["errors"] += 1
            logger.warning("Various Artists refile failed for album %s: %s", album.get("id"), safe_exc(exc))
        if on_progress is not None:
            on_progress(done, total)
    logger.info("Various Artists refile finished: %s", counts)
    return counts
