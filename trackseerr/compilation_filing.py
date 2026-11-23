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
SMALL_COMPILATION_MAX_TRACKS = 3


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


def keeps_compilation(db: Any, album: dict[str, Any]) -> bool:
    """True when a compilation should stay under Various Artists (monitored or > SMALL_COMPILATION_MAX_TRACKS owned)."""
    if album.get("monitored"):
        return True
    owned = sum(1 for t in db.list_library_tracks(album_id=str(album["id"]), limit=1000) if t.get("has_file"))
    return owned > SMALL_COMPILATION_MAX_TRACKS


def _primary_track_credit(db: Any, track_id: str) -> Optional[dict[str, Any]]:
    credits = [c for c in db.list_track_artists(track_id) if str(c.get("role") or "").casefold() == "primary"]
    if not credits:
        return None
    return min(credits, key=lambda c: (c.get("position") is None, int(c.get("position") or 0)))


def _artist_for_mbid(db: Any, mbid: str, name: str) -> Optional[dict[str, Any]]:
    """Library artist for an MBID, created unmonitored when missing and named; None without an MBID."""
    clean_mbid = str(mbid or "").strip()
    if not clean_mbid:
        return None
    existing = db.get_library_artist_by_mbid(clean_mbid)
    if existing is not None:
        return dict(existing)
    clean_name = str(name or "").strip()
    if not clean_name:
        return None
    created = db.upsert_library_artist(
        LibraryArtist(
            id=str(uuid.uuid4()),
            name=clean_name,
            mbid=clean_mbid,
            monitored=False,
            monitor_option="none",
        )
    )
    return dict(created)


def performer_artist(db: Any, track_id: str) -> Optional[dict[str, Any]]:
    """Library artist for a track's primary credit (created unmonitored when missing); None without an MBID."""
    credit = _primary_track_credit(db, track_id)
    if credit is None:
        return None
    return _artist_for_mbid(db, str(credit.get("artist_mbid") or ""), str(credit.get("artist_name") or ""))


def _search_home(
    db: Any, enricher: Any, track: dict[str, Any], credit: dict[str, Any]
) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    """Finds a track's performer artist and home release group via MusicBrainz recording search."""
    from trackseerr import recording_search

    name = str(credit.get("artist_name") or "").strip()
    title = str(track.get("title") or "")
    if not name or not title:
        return None
    candidates = enricher.search_recordings(name, title)
    if not candidates:
        return None
    cand = recording_search.pick_search_recording(candidates, title, name, track.get("duration_seconds"), set())
    if cand is None:
        return None
    credit_mbid = str(credit.get("artist_mbid") or "").strip()
    cand_ids = [str(a).strip() for a in cand.get("artist_ids") or [] if str(a).strip()]
    if credit_mbid:
        if credit_mbid.lower() not in {a.lower() for a in cand_ids}:
            return None
        mbid = credit_mbid
    elif cand_ids:
        mbid = cand_ids[0]
    else:
        return None
    cand_id = str(cand.get("id") or "").strip()
    if not cand_id:
        return None
    groups = enricher.get_recording_release_groups(cand_id)
    home = home_release_group(groups or [], mbid, title)
    if home is None:
        return None
    cand_names = [str(n).strip() for n in cand.get("artist_names") or [] if str(n).strip()]
    artist_name = name if credit_mbid else (cand_names[0] if cand_names else name)
    artist = _artist_for_mbid(db, mbid, artist_name)
    if artist is None:
        return None
    return artist, home


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


def _resolve_per_track_target(
    db: Any,
    enricher: Any,
    track: dict[str, Any],
    per_rec_groups: dict[str, list[dict[str, Any]]],
    rec: str,
) -> Optional[tuple[dict[str, Any], dict[str, Any], bool]]:
    """Resolves (artist, home_group, from_search) for a track in per-track mode."""
    title = str(track.get("title") or "")
    target_artist = performer_artist(db, str(track["id"]))
    home = (
        home_release_group(per_rec_groups.get(rec, []), target_artist.get("mbid"), title)
        if target_artist is not None
        else None
    )
    if target_artist is not None and home is not None:
        return target_artist, home, False
    credit = _primary_track_credit(db, str(track["id"]))
    if credit is None:
        return None
    hit = _search_home(db, enricher, track, credit)
    if hit is None:
        return None
    return hit[0], hit[1], True


def file_tracks_under_home(
    db: Any,
    enricher: Any,
    album: dict[str, Any],
    per_rec_groups: dict[str, list[dict[str, Any]]],
    counts: dict[str, int],
    *,
    per_track_artist: bool = False,
) -> bool:
    """Moves each recording-known track of a small album into the album of its canonical release.

    Returns True when at least one track was moved or a conflict was recorded.
    """
    from trackseerr.artist_repair import move_track_into_album

    album_artist_id = str(album["artist_id"])
    album_artist = None if per_track_artist else (db.get_library_artist(album_artist_id) or {})
    acted = False
    for track in db.list_library_tracks(album_id=album["id"], limit=1000):
        rec = str(track.get("mb_recording_id") or "").strip()
        if not rec or not track.get("has_file"):
            continue
        from_search = False
        if per_track_artist:
            resolved = _resolve_per_track_target(db, enricher, track, per_rec_groups, rec)
            if resolved is None:
                continue
            target_artist, home, from_search = resolved
            target_artist_id = str(target_artist["id"])
        else:
            target_artist_id = album_artist_id
            home = home_release_group(
                per_rec_groups.get(rec, []), (album_artist or {}).get("mbid"), str(track.get("title") or "")
            )
            if home is None:
                continue
        holder = ensure_release_group_album(db, enricher, target_artist_id, home)
        if holder == str(album["id"]):
            continue
        acted = True
        if from_search:
            counts["searched_home"] = counts.get("searched_home", 0) + 1
        if move_track_into_album(db, str(track["id"]), holder) == "merged":
            counts["filed_home"] = counts.get("filed_home", 0) + 1
        else:
            db.record_merge_conflict(str(album["id"]), holder, "duplicate_files")
            counts["merge_conflict"] += 1
    return acted


def _per_recording_groups(enricher: Any, tracks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    per_rec: dict[str, list[dict[str, Any]]] = {}
    for track in tracks:
        rec = str(track.get("mb_recording_id") or "").strip()
        if rec and track.get("has_file") and rec not in per_rec:
            per_rec[rec] = enricher.get_recording_release_groups(rec)
    return per_rec


def _various_group_of_album(
    rg_id: str, per_rec: dict[str, list[dict[str, Any]]]
) -> Optional[dict[str, Any]]:
    for groups in per_rec.values():
        for group in groups:
            if group.get("is_various") and str(group.get("id")) == rg_id:
                return group
    return None


def _refile_one_album(db: Any, enricher: Any, album: dict[str, Any], va_id: str, counts: dict[str, int]) -> None:
    rg_id = str(album.get("mb_release_group_id") or "").strip()
    tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    per_rec = _per_recording_groups(enricher, tracks)
    group = _various_group_of_album(rg_id, per_rec)
    if group is None:
        return
    counts["albums"] += 1
    if not keeps_compilation(db, album):
        file_tracks_under_home(db, enricher, album, per_rec, counts)
        counts["kept_small"] = counts.get("kept_small", 0) + 1
        return
    if db.album_holding_release_group(va_id, str(group["id"]), str(album["id"])) is not None:
        file_album_under_various(db, enricher, album, group, counts)
        return
    name, mbid = album_artist_credit(db, album)
    for track in tracks:
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
    counts = {"albums": 0, "moved": 0, "merged": 0, "kept_small": 0, "filed_home": 0, "merge_conflict": 0, "errors": 0}
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


def _group_by_id(rg_id: str, per_rec: dict[str, list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    for groups in per_rec.values():
        for group in groups:
            if str(group.get("id")) == rg_id:
                return group
    return None


def _sole_performer_mbid(db: Any, owned_tracks: list[dict[str, Any]]) -> Optional[str]:
    mbids: set[str] = set()
    for track in owned_tracks:
        credit = _primary_track_credit(db, str(track["id"]))
        mbid = str((credit or {}).get("artist_mbid") or "").strip().lower()
        if not mbid:
            return None
        mbids.add(mbid)
    return next(iter(mbids)) if len(mbids) == 1 else None


def _move_wrongly_linked_album(
    db: Any, album: dict[str, Any], owned_tracks: list[dict[str, Any]], group: dict[str, Any], counts: dict[str, int]
) -> None:
    sole_mbid = _sole_performer_mbid(db, owned_tracks)
    group_artists = {str(a).lower() for a in group.get("artist_ids") or []}
    if sole_mbid is None or sole_mbid not in group_artists:
        counts["skipped"] += 1
        return
    artist = performer_artist(db, str(owned_tracks[0]["id"]))
    if artist is None:
        counts["skipped"] += 1
        return
    db.move_album_to_artist(str(album["id"]), str(artist["id"]))
    counts["moved_to_artist"] += 1


def _refile_out_of_various_one(db: Any, enricher: Any, album: dict[str, Any], counts: dict[str, int]) -> None:
    tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    owned_tracks = [t for t in tracks if t.get("has_file")]
    per_rec = _per_recording_groups(enricher, owned_tracks)
    rg_id = str(album.get("mb_release_group_id") or "").strip()
    if rg_id:
        group = _group_by_id(rg_id, per_rec)
        if group is not None and not group.get("is_various"):
            counts["albums"] += 1
            _move_wrongly_linked_album(db, album, owned_tracks, group, counts)
            return
    if not rg_id or not keeps_compilation(db, album):
        counts["albums"] += 1
        file_tracks_under_home(db, enricher, album, per_rec, counts, per_track_artist=True)


def refile_out_of_various(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Moves real-artist albums and small compilations out of Various Artists to their performer."""
    counts = {
        "albums": 0,
        "moved_to_artist": 0,
        "filed_home": 0,
        "searched_home": 0,
        "merge_conflict": 0,
        "skipped": 0,
        "errors": 0,
    }
    va_id = various_artists_id(db)
    candidates = db.list_albums_with_files_for_artist(va_id)
    total = len(candidates)
    if on_progress is not None:
        on_progress(0, total)
    for done, album in enumerate(candidates, start=1):
        if should_stop is not None and should_stop():
            logger.info("Out of Various Artists refile stopped early: %s", counts)
            return counts
        try:
            _refile_out_of_various_one(db, enricher, album, counts)
        except (sqlite3.Error, requests.RequestException) as exc:
            counts["errors"] += 1
            logger.warning("Out of Various Artists refile failed for album %s: %s", album.get("id"), safe_exc(exc))
        if on_progress is not None:
            on_progress(done, total)
    logger.info("Out of Various Artists refile finished: %s", counts)
    return counts
