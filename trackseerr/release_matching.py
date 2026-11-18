"""MusicBrainz release matching for library albums missing a release ID."""

from __future__ import annotations

import difflib
import itertools
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any, Optional

import requests

from trackseerr import mb_metadata_store
from trackseerr.album_titles import album_titles_match
from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID
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


def _is_plain_album(group: dict[str, Any]) -> bool:
    return str(group.get("primary_type") or "").casefold() == "album" and not group.get("secondary_types")


def _existing_holder_album(db: Any, artist_id: str, groups: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for group in sorted(groups, key=lambda g: not _is_plain_album(g)):
        rg_id = str(group.get("id") or "").strip()
        if not rg_id:
            continue
        holder_id = db.album_holding_release_group(artist_id, rg_id, "")
        if holder_id is not None:
            album = db.get_library_album(holder_id)
            if album is not None:
                return album
    return None


def _filed_album_for_title(
    db: Any,
    enricher: Any,
    artist_id: str,
    groups: list[dict[str, Any]],
    album_title: str,
    track_title: str,
) -> Optional[dict[str, Any]]:
    """Album for a new file whose own album tag names a compilation or no release at all of the recording."""
    from trackseerr.compilation_filing import (
        ensure_release_group_album,
        home_release_group,
        various_artists_id,
        various_group_for_title,
    )

    va_group = various_group_for_title(groups, album_title)
    if va_group is not None:
        return db.get_library_album(ensure_release_group_album(db, enricher, various_artists_id(db), va_group))
    if any(album_titles_match(g.get("title"), album_title) for g in groups):
        return None
    artist = db.get_library_artist(artist_id) or {}
    home = home_release_group(groups, artist.get("mbid"), track_title)
    if home is None:
        return None
    return db.get_library_album(ensure_release_group_album(db, enricher, artist_id, home))


def album_for_recording(
    db: Any,
    artist_id: Optional[str],
    recording_id: Optional[str],
    album_title: Optional[str] = None,
    track_title: str = "",
) -> Optional[dict[str, Any]]:
    """Returns the library album a file with this recording belongs in, or None.

    That is the artist's existing album holding one of the recording's release groups. With ``album_title`` (the
    file's own album tag) a Various Artists compilation of that name, or, when the tag names no release of the
    recording, the recording's canonical release, is created and returned instead. Callers that get a Various
    Artists album back must write the track's primary credit (``compilation_filing.ensure_primary_credit``).
    """
    clean_artist = str(artist_id or "").strip()
    clean_rec = str(recording_id or "").strip()
    if not clean_artist or not clean_rec:
        return None

    try:
        enricher = mb_metadata_store.get_shared_enricher(db)
        groups = enricher.get_recording_release_groups(clean_rec) or []
        holder = _existing_holder_album(db, clean_artist, groups)
        if holder is not None:
            return holder
        if album_title:
            return _filed_album_for_title(db, enricher, clean_artist, groups, album_title, track_title)
    except (requests.RequestException, sqlite3.Error) as exc:
        logger.warning("Recording release-group lookup failed for %s: %s", clean_rec, exc)
    return None


def _pick_release_group(groups: list[dict[str, Any]], album_title: str) -> Optional[dict[str, Any]]:
    """Title-matching release group: plain Album first, then the earliest first release date."""
    from trackseerr.album_titles import album_titles_match

    candidates = [g for g in groups if album_titles_match(g.get("title"), album_title)]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda g: (
            0 if _is_plain_album(g) else 1,
            str(g.get("first_release_date") or "9999-99-99"),
            str(g.get("id") or ""),
        ),
    )


_SINGLE_ALBUM_MAX_TRACKS = 3


def _merge_into_holder(db: Any, source_album_id: str, holder_album_id: str, counts: dict[str, int]) -> None:
    from trackseerr.artist_repair import merge_single_album_into

    outcome = merge_single_album_into(db, source_album_id, holder_album_id)
    if outcome == "merged":
        db.clear_merge_conflicts_for_album(source_album_id)
        counts["merged"] += 1
    else:
        db.record_merge_conflict(source_album_id, holder_album_id, "duplicate_files")
        counts["merge_conflict"] += 1


def _first_holder_for_groups(
    db: Any, artist_id: str, group_ids: list[str], exclude_album_id: str
) -> Optional[str]:
    for rg_id in group_ids:
        if not rg_id:
            continue
        holder = db.album_holding_release_group(artist_id, rg_id, exclude_album_id)
        if holder is not None:
            return holder
    return None


def _holder_may_absorb(db: Any, album: dict[str, Any], holder_id: str, track_count: int) -> bool:
    """Small albums fold into any holder; larger ones only into a holder whose title says it is the same album."""
    if track_count <= _SINGLE_ALBUM_MAX_TRACKS:
        return True
    holder = db.get_library_album(holder_id)
    return holder is not None and album_titles_match(album["title"], holder.get("title"))


def _recording_groups(
    enricher: Any, rec_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Release groups of the recordings: ``({group id: group}, {recording id: its groups})``."""
    groups: dict[str, dict[str, Any]] = {}
    per_rec: dict[str, list[dict[str, Any]]] = {}
    for rec_id in rec_ids:
        per_rec[rec_id] = []
        for group in enricher.get_recording_release_groups(rec_id):
            gid = str(group.get("id") or "")
            if gid:
                groups.setdefault(gid, group)
                per_rec[rec_id].append(group)
    return groups, per_rec


def _choose_group(
    groups: dict[str, dict[str, Any]],
    per_rec: dict[str, list[dict[str, Any]]],
    album: dict[str, Any],
    owned_rec_ids: list[str],
    allow_various: bool,
) -> tuple[Optional[dict[str, Any]], str]:
    """Title-matching group, else the single group every owned recording shares; Various Artists ones are skipped."""
    candidates = [g for g in groups.values() if allow_various or not g.get("is_various")]
    chosen = _pick_release_group(candidates, album["title"])
    if chosen is not None:
        return chosen, "linked"
    if len(owned_rec_ids) >= 2:
        sets = [{str(g["id"]) for g in per_rec[r]} for r in owned_rec_ids]
        common = {gid for gid in set.intersection(*sets) if gid in {str(g["id"]) for g in candidates}}
        if len(common) == 1:
            return groups[next(iter(common))], "linked_by_agreement"
    return None, "linked"


def _album_is_various_artists(db: Any, album: dict[str, Any]) -> bool:
    artist = db.get_library_artist(str(album["artist_id"])) or {}
    return str(artist.get("mbid") or "").lower() == VARIOUS_ARTISTS_MBID


def _link_unmatched_album(
    db: Any,
    enricher: Any,
    album: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    per_rec: dict[str, list[dict[str, Any]]],
    track_count: int,
    counts: dict[str, int],
) -> None:
    """No title-matching group: fold into an existing holder, else file small albums under the recording's home."""
    from trackseerr.compilation_filing import file_tracks_under_home

    holder = _first_holder_for_groups(db, album["artist_id"], list(groups.keys()), album["id"])
    if holder is not None and _holder_may_absorb(db, album, holder, track_count):
        _merge_into_holder(db, album["id"], holder, counts)
        return
    if (
        holder is None
        and track_count <= _SINGLE_ALBUM_MAX_TRACKS
        and not _album_is_various_artists(db, album)
        and file_tracks_under_home(db, enricher, album, per_rec, counts)
    ):
        return
    counts["no_title_match"] += 1


def _link_one_album_by_recording(db: Any, enricher: Any, album: dict[str, Any], counts: dict[str, int]) -> None:
    from trackseerr.compilation_filing import file_album_under_various, various_group_for_title

    tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    owned_rec_ids = [
        str(t.get("mb_recording_id") or "").strip()
        for t in tracks
        if t.get("has_file") and str(t.get("mb_recording_id") or "").strip()
    ]
    rec_ids = sorted(set(owned_rec_ids))
    if not rec_ids:
        counts["no_recordings"] += 1
        return

    groups, per_rec = _recording_groups(enricher, rec_ids)
    under_various = _album_is_various_artists(db, album)
    va_group = None if under_various else various_group_for_title(list(groups.values()), album["title"])
    if va_group is not None:
        file_album_under_various(db, enricher, album, va_group, counts)
        return

    chosen, linked_key = _choose_group(groups, per_rec, album, owned_rec_ids, under_various)
    if chosen is None:
        _link_unmatched_album(db, enricher, album, groups, per_rec, len(tracks), counts)
        return

    rg_id = str(chosen["id"])
    holder = db.album_holding_release_group(album["artist_id"], rg_id, album["id"])
    if holder is not None:
        if _holder_may_absorb(db, album, holder, len(tracks)):
            _merge_into_holder(db, album["id"], holder, counts)
            return
        counts["rg_taken"] += 1
        logger.info("Album %s not linked: release group %s is already held by the same artist", album["id"], rg_id)
        return

    db.set_album_mb_release_group_id(album["id"], rg_id)
    counts[linked_key] += 1


def link_albums_by_recording(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Links albums with no release group to MusicBrainz through the release groups of their tracks' recordings."""
    counts = {
        "albums": 0,
        "linked": 0,
        "linked_by_agreement": 0,
        "merged": 0,
        "merge_conflict": 0,
        "filed_compilation": 0,
        "filed_home": 0,
        "no_recordings": 0,
        "no_title_match": 0,
        "rg_taken": 0,
        "errors": 0,
    }

    total: Optional[int] = None
    if on_progress is not None:
        total = db.count_albums_without_release_group()
        on_progress(0, total)

    processed = 0
    for album in db.iter_albums_without_release_group(batch=200):
        if should_stop is not None and should_stop():
            logger.info("Recording release-group link stopped early: %s", counts)
            return counts
        try:
            _link_one_album_by_recording(db, enricher, album, counts)
            counts["albums"] += 1
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("Recording release-group link failed for album %s: %s", album.get("id"), exc)
        finally:
            processed += 1
            if on_progress is not None:
                on_progress(processed, total)

    logger.info("Recording release-group link finished: %s", counts)
    return counts


def _fold_title_duplicate(db: Any, album: dict[str, Any], counts: dict[str, int]) -> None:
    holders = [
        h
        for h in db.list_artist_albums_with_release_group(album["artist_id"])
        if h["id"] != album["id"] and album_titles_match(album["title"], h.get("title"))
    ]
    if not holders:
        return
    if len(holders) > 1:
        studio_holders = [
            h
            for h in holders
            if h.get("album_type") == "album" and not h.get("secondary_types")
        ]
        if len(studio_holders) == 1:
            _merge_into_holder(db, album["id"], studio_holders[0]["id"], counts)
            return
        counts["ambiguous"] += 1
        for holder in holders:
            db.record_merge_conflict(album["id"], holder["id"], "ambiguous")
        logger.info("Album %s matches several albums that hold a release group; left alone", album["id"])
        return
    _merge_into_holder(db, album["id"], holders[0]["id"], counts)


def merge_title_duplicates(
    db: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Folds albums with files and no release group into the same-titled album of the artist that holds one."""
    counts = {"albums": 0, "merged": 0, "merge_conflict": 0, "ambiguous": 0, "errors": 0}
    total: Optional[int] = None
    if on_progress is not None:
        total = db.count_albums_without_release_group()
        on_progress(0, total)

    processed = 0
    for album in db.iter_albums_without_release_group(batch=200):
        if should_stop is not None and should_stop():
            logger.info("Title duplicate merge stopped early: %s", counts)
            return counts
        try:
            _fold_title_duplicate(db, album, counts)
            counts["albums"] += 1
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("Title duplicate merge failed for album %s: %s", album.get("id"), exc)
        finally:
            processed += 1
            if on_progress is not None:
                on_progress(processed, total)

    logger.info("Title duplicate merge finished: %s", counts)
    return counts


def _rg_recording_ids(enricher: Any, rg_id: str) -> set[str]:
    if not rg_id:
        return set()
    rec_ids: set[str] = set()
    for rel in enricher.get_release_group_releases(rg_id) or []:
        rec_ids.update(_extract_release_rec_ids(rel))
    return rec_ids


def _usable_matches(matches: list[dict[str, Any]], used_rec_ids: set[str]) -> list[dict[str, Any]]:
    usable: list[dict[str, Any]] = []
    for m in matches:
        rec_id = str(m.get("recording_id") or "").strip()
        if rec_id and rec_id not in used_rec_ids:
            usable.append(m)
    return sorted(usable, key=lambda m: float(m.get("score") or 0.0), reverse=True)


_VERSION_WORDS = frozenset({
    "remix",
    "mix",
    "dub",
    "edit",
    "live",
    "instrumental",
    "acoustic",
    "demo",
    "extended",
    "rework",
    "bootleg",
    "vip",
    "version",
    "karaoke",
    "cover",
})


def _artist_key(name: str | None) -> str:
    from trackseerr.clients.mbid_enricher import artist_name_key

    return artist_name_key(str(name or "")).replace(" ", "")


def _artists_match(match_artist: Any, track_artist: str | None) -> bool:
    from trackseerr.artist_repair import primary_credit

    m_key = _artist_key(str(match_artist or ""))
    t_key = _artist_key(track_artist)
    if not m_key or not t_key:
        return False
    if m_key == t_key:
        return True
    primary = primary_credit(track_artist or "")
    return bool(primary and _artist_key(primary) == m_key)


def _version_words(title: str) -> set[str]:
    from trackseerr.lidarr_release import _fold

    return set(re.findall(r"[a-z0-9]+", _fold(title))) & _VERSION_WORDS


def _titles_close(left: str, right: str) -> bool:
    from trackseerr.lidarr_release import norm_title
    from trackseerr.rename_verification import _YEAR_MIX_SUFFIX

    a_stripped = _YEAR_MIX_SUFFIX.sub("", left)
    b_stripped = _YEAR_MIX_SUFFIX.sub("", right)
    if _version_words(a_stripped) != _version_words(b_stripped):
        return False
    a_norm = norm_title(a_stripped)
    b_norm = norm_title(b_stripped)
    if not a_norm or not b_norm:
        return False
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= 0.85


def _same_song_match(
    m: dict[str, Any], track_title: str, track_artist: str | None = None
) -> bool:
    from trackseerr.rename_verification import _same_song

    score = float(m.get("score") or 0.0)
    match_title = str(m.get("title") or "")
    if score >= 0.90 and _same_song(match_title, track_title):
        return True
    if score < 0.85 or not _artists_match(m.get("artist"), track_artist):
        return False
    return _titles_close(match_title, track_title)


def _pick_recording(
    matches: list[dict[str, Any]],
    track_title: str,
    rg_rec_ids: set[str],
    used_rec_ids: set[str],
    track_artist: str | None = None,
) -> dict[str, Any] | None:
    """Picks the best unused AcoustID match: inside the release group first, else a same-song match."""
    available = _usable_matches(matches, used_rec_ids)
    if rg_rec_ids:
        for m in available:
            if str(m["recording_id"]).strip() in rg_rec_ids:
                return m
    for m in available:
        if _same_song_match(m, track_title, track_artist):
            return m
    return None


def _missing_recording_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tracks:
        if t.get("has_file") and not str(t.get("mb_recording_id") or "").strip():
            out.append(t)
    return out


def _first_lookup_rejected(fp: dict[str, Any], state: dict[str, bool]) -> bool:
    from trackseerr.rename_verification import _key_rejected

    if not state["first_lookup_pending"] or fp.get("cached") or fp.get("error") == "file missing":
        return False
    state["first_lookup_pending"] = False
    return _key_rejected(fp)


def _album_artist_name(
    db: Any, album: dict[str, Any], tracks: list[dict[str, Any]]
) -> str | None:
    artist_id = album.get("artist_id") or (tracks[0].get("artist_id") if tracks else None)
    if not artist_id:
        return None
    artist_row = db.get_library_artist(str(artist_id))
    if not artist_row:
        return None
    return str(artist_row.get("name") or "").strip() or None


def _fill_album(
    db: Any,
    enricher: Any,
    album: dict[str, Any],
    api_key: str,
    counts: dict[str, int],
    state: dict[str, bool],
) -> bool:
    """Fills missing track recording IDs on one album. Returns True when the whole step must stop."""
    from trackseerr.rename_verification import verify_file

    all_tracks = db.list_library_tracks(album_id=album["id"], limit=1000)
    missing = _missing_recording_tracks(all_tracks)
    if not missing:
        return False

    track_artist = _album_artist_name(db, album, all_tracks)
    used_rec_ids = _extract_owned_rec_ids(all_tracks)
    rg_id = str(album.get("mb_release_group_id") or "").strip()
    rg_rec_ids: Optional[set[str]] = None

    for track in missing:
        files = db.list_library_files_for_track(track["id"])
        if not files:
            continue
        fp = verify_file(db, files[0], api_key)
        if _first_lookup_rejected(fp, state):
            logger.warning("Fingerprint recording fill stopped: AcoustID rejected the API key")
            return True

        counts["tracks"] += 1
        matches = fp.get("matches") or []
        if fp.get("status") != "ok" or not matches:
            counts["no_fingerprint"] += 1
            continue

        if rg_rec_ids is None:
            rg_rec_ids = _rg_recording_ids(enricher, rg_id)

        track_title = str(track.get("title") or "")
        chosen = _pick_recording(matches, track_title, rg_rec_ids, used_rec_ids, track_artist)
        if chosen is None:
            counts["no_match"] += 1
            continue

        rec_id = str(chosen["recording_id"]).strip()
        db.set_track_mb_recording_id(str(track["id"]), rec_id)
        used_rec_ids.add(rec_id)
        counts["filled"] += 1
        logger.info(
            "Filled recording for album %s track %r: %s (score=%.2f)",
            album["id"],
            track_title,
            rec_id,
            float(chosen.get("score") or 0.0),
        )
    return False


def fill_recordings_from_fingerprints(
    db: Any,
    enricher: Any,
    api_key: str,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Fills missing track mb_recording_id values from AcoustID fingerprints on albums missing a release."""
    from trackseerr.library import FingerprintUnavailable

    counts = {"tracks": 0, "filled": 0, "no_fingerprint": 0, "no_match": 0, "errors": 0}
    state = {"first_lookup_pending": True}

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
            logger.info("Fingerprint recording fill stopped early: %s", counts)
            return counts
        try:
            if _fill_album(db, enricher, album, api_key, counts, state):
                return counts
        except FingerprintUnavailable:
            logger.warning("Fingerprint recording fill stopped: fingerprinting unavailable")
            return counts
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("Fingerprint recording fill failed for album %s: %s", album.get("id"), exc)
        processed += 1
        if on_progress is not None:
            on_progress(processed, total)

    logger.info("Fingerprint recording fill finished: %s", counts)
    return counts


def match_album_releases(
    db: Any,
    enricher: Any,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
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

    total: Optional[int] = None
    if on_progress is not None:
        total = db.count_albums_missing_release()
        on_progress(0, total)

    processed = 0
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
        finally:
            processed += 1
            if on_progress is not None:
                on_progress(processed, total)

    logger.info("MusicBrainz release match finished: %s", counts)
    return counts
