"""One-off, re-runnable repair of the native library's artist rows.

Three passes, database only (files on disk are never moved; the Rename preview offers folder moves afterwards):

1. Rows that share one MusicBrainz ID and carry the same name once case, accents, dashes and "&"/"and" are folded
   ("Jay Z" / "JAY-Z" / "JAŸ-Z") are merged into one artist.
2. IDs that do not belong to the artist (a blind top-hit lookup stored "Basement Jaxx" on "Basement") are replaced by a
   validated ID or cleared, and the albums that came from the wrong discography are dropped.
3. Artists without an ID whose name is a joined credit ("2Pac; Dr. Dre; Roger Troutman") are filed under the primary
   artist.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from trackseerr.clients.mbid_enricher import artist_name_key
from trackseerr.library import primary_artist
from trackseerr.models import LibraryArtist
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)

REPAIR_KV_KEY = "artist_repair_v1"

# Tables whose plain-text columns hold a library album id (no foreign key, or a foreign key without cascade).
_ALBUM_REF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("active_downloads", "album_id"),
    ("download_history", "album_id"),
    ("item_events", "album_id"),
    ("pending_releases", "album_id"),
    ("media_issues", "album_id"),
    ("search_log", "album_id"),
    ("music_requests", "catalog_album_id"),
)
_TRACK_REF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("active_downloads", "track_id"),
    ("download_history", "track_id"),
    ("item_events", "track_id"),
    ("pending_releases", "track_id"),
    ("media_issues", "track_id"),
    ("search_log", "track_id"),
    ("music_requests", "catalog_track_id"),
)
# Same for library artist ids (artist_links has a unique index and is handled separately).
_ARTIST_REF_COLUMNS: tuple[tuple[str, str], ...] = (("item_events", "artist_id"),)

_ARTIST_FILL_COLUMNS = (
    "mbid", "foreign_artist_id", "image_url", "banner_url", "bio", "genres", "country", "art_version", "metadata_json",
)
_ALBUM_FILL_COLUMNS = ("mb_release_group_id", "mb_release_id", "cover_url", "art_version")

_JOIN_SEPARATOR_RE = re.compile(r", | & | [xX] ")
_X_SEPARATOR_RE = re.compile(r" [xX] ")

_HAS_FILE_SQL = (
    "EXISTS (SELECT 1 FROM library_tracks t WHERE t.album_id = a.id "
    "AND (t.has_file = 1 OR EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id)))"
)
_REMOVABLE_ALBUMS_SQL = f"""
    SELECT a.id FROM library_albums a
    WHERE a.artist_id = ?
      AND NOT {_HAS_FILE_SQL}
      AND NOT EXISTS (SELECT 1 FROM active_downloads d WHERE d.album_id = a.id
                      OR d.track_id IN (SELECT id FROM library_tracks WHERE album_id = a.id))
      AND NOT EXISTS (SELECT 1 FROM music_requests r WHERE r.catalog_album_id = a.id
                      OR r.catalog_track_id IN (SELECT id FROM library_tracks WHERE album_id = a.id))
"""

_COUNT_KEYS = (
    "artists_merged", "albums_merged", "ids_kept_alias", "ids_replaced", "ids_cleared",
    "wrong_albums_removed", "credits_refiled", "lookups_unavailable", "errors",
)

StopFn = Optional[Callable[[], bool]]
ProgressFn = Optional[Callable[[int, Optional[int]], None]]


def _empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _fetchall(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db._lock:
        return list(db.conn.execute(sql, params).fetchall())


def _mb_name(enricher: Any, mbid: str) -> Optional[str]:
    """Canonical MusicBrainz name for ``mbid``; None when unknown or the lookup fails."""
    details = enricher.get_artist_details(mbid)
    if not isinstance(details, dict):
        return None
    name = details.get("name")
    return str(name) if name else None


# --- merging one artist row into another -----------------------------------------------------------------------------


def _fill_empty(cur: sqlite3.Cursor, table: str, columns: tuple[str, ...], keep_id: str, dupe: sqlite3.Row) -> None:
    """Copies the dupe's value into every column where the keeper's is empty."""
    keeper = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (keep_id,)).fetchone()
    if keeper is None:
        return
    sets: list[str] = []
    params: list[Any] = []
    for col in columns:
        if _empty(keeper[col]) and not _empty(dupe[col]):
            sets.append(f"{col} = ?")
            params.append(dupe[col])
    if sets:
        params.append(keep_id)
        cur.execute(f"UPDATE {table} SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", params)


def _merge_album_rows(cur: sqlite3.Cursor, keep_album: str, dupe_album: sqlite3.Row, keep_artist: str) -> None:
    """Folds one album into the keeper's album of the same title; every reference follows the keeper."""
    dupe_id = str(dupe_album["id"])
    cur.execute(
        "UPDATE library_tracks SET album_id = ?, artist_id = ? WHERE album_id = ?", (keep_album, keep_artist, dupe_id)
    )
    cur.execute(
        "INSERT OR IGNORE INTO library_collection_albums (collection_id, album_id, order_index) "
        "SELECT collection_id, ?, order_index FROM library_collection_albums WHERE album_id = ?",
        (keep_album, dupe_id),
    )
    cur.execute("DELETE FROM library_collection_albums WHERE album_id = ?", (dupe_id,))
    for table, column in _ALBUM_REF_COLUMNS:
        cur.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (keep_album, dupe_id))
    cur.execute("DELETE FROM library_albums WHERE id = ?", (dupe_id,))
    _fill_empty(cur, "library_albums", _ALBUM_FILL_COLUMNS, keep_album, dupe_album)
    if dupe_album["monitored"]:
        cur.execute("UPDATE library_albums SET monitored = 1 WHERE id = ?", (keep_album,))


def _find_holder_track(
    source_track: sqlite3.Row, holder_tracks: list[sqlite3.Row], used_ids: set[str]
) -> Optional[sqlite3.Row]:
    from trackseerr.lidarr_release import titles_match

    available = [ht for ht in holder_tracks if str(ht["id"]) not in used_ids]
    rec_id = str(source_track["mb_recording_id"] or "").strip()
    if rec_id:
        for ht in available:
            if str(ht["mb_recording_id"] or "").strip() == rec_id:
                return ht
    title_matches = [ht for ht in available if titles_match(ht["title"], source_track["title"])]
    return title_matches[0] if len(title_matches) == 1 else None


def _track_has_file(cur: sqlite3.Cursor, track: sqlite3.Row) -> bool:
    if int(track["has_file"] or 0):
        return True
    row = cur.execute("SELECT 1 FROM library_files WHERE track_id = ? LIMIT 1", (str(track["id"]),)).fetchone()
    return row is not None


def _merge_matched_track(cur: sqlite3.Cursor, source_track: sqlite3.Row, holder_track: sqlite3.Row) -> None:
    source_tid = str(source_track["id"])
    holder_tid = str(holder_track["id"])
    cur.execute("UPDATE library_files SET track_id = ? WHERE track_id = ?", (holder_tid, source_tid))
    rec_id = (
        source_track["mb_recording_id"]
        if _empty(holder_track["mb_recording_id"]) and not _empty(source_track["mb_recording_id"])
        else holder_track["mb_recording_id"]
    )
    monitored = 1 if (holder_track["monitored"] or source_track["monitored"]) else 0
    cur.execute(
        "UPDATE library_tracks SET has_file = 1, mb_recording_id = ?, monitored = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (rec_id, monitored, holder_tid),
    )
    for table, column in _TRACK_REF_COLUMNS:
        cur.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (holder_tid, source_tid))
    cur.execute("DELETE FROM library_tracks WHERE id = ?", (source_tid,))


def _plan_track_merges(
    cur: sqlite3.Cursor, source_tracks: list[sqlite3.Row], holder_tracks: list[sqlite3.Row]
) -> Optional[list[tuple[sqlite3.Row, Optional[sqlite3.Row]]]]:
    used_holder_ids: set[str] = set()
    plan: list[tuple[sqlite3.Row, Optional[sqlite3.Row]]] = []
    for st in source_tracks:
        ht = _find_holder_track(st, holder_tracks, used_holder_ids)
        if ht is not None:
            if _track_has_file(cur, ht):
                return None
            used_holder_ids.add(str(ht["id"]))
        plan.append((st, ht))
    return plan


def _apply_track_merges(
    cur: sqlite3.Cursor,
    plan: list[tuple[sqlite3.Row, Optional[sqlite3.Row]]],
    holder_id: str,
    holder_artist_id: str,
) -> tuple[int, int]:
    merged_count = 0
    moved_count = 0
    for st, ht in plan:
        if ht is not None:
            _merge_matched_track(cur, st, ht)
            merged_count += 1
        else:
            cur.execute(
                "UPDATE library_tracks SET album_id = ?, artist_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (holder_id, holder_artist_id, str(st["id"])),
            )
            moved_count += 1
    return merged_count, moved_count


def _repoint_and_delete_source_album(cur: sqlite3.Cursor, source_id: str, holder_id: str) -> None:
    cur.execute(
        "INSERT OR IGNORE INTO library_collection_albums (collection_id, album_id, order_index) "
        "SELECT collection_id, ?, order_index FROM library_collection_albums WHERE album_id = ?",
        (holder_id, source_id),
    )
    cur.execute("DELETE FROM library_collection_albums WHERE album_id = ?", (source_id,))
    for table, column in _ALBUM_REF_COLUMNS:
        cur.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (holder_id, source_id))
    cur.execute("DELETE FROM library_albums WHERE id = ?", (source_id,))


def merge_single_album_into(db: Any, source_album_id: str, holder_album_id: str) -> str:
    """Folds a playlist single album into the album that holds its release group in one transaction.

    Returns ``"merged"`` or ``"conflict"`` (when a matched holder track already owns a file).
    """
    source_id, holder_id = str(source_album_id), str(holder_album_id)
    if source_id == holder_id:
        return "conflict"
    with db._lock:
        try:
            cur = db.conn.cursor()
            source_album = cur.execute("SELECT * FROM library_albums WHERE id = ?", (source_id,)).fetchone()
            holder_album = cur.execute("SELECT * FROM library_albums WHERE id = ?", (holder_id,)).fetchone()
            if source_album is None or holder_album is None:
                db.conn.rollback()
                return "conflict"
            source_tracks = cur.execute(
                "SELECT * FROM library_tracks WHERE album_id = ? ORDER BY disc_number, track_number, id", (source_id,)
            ).fetchall()
            holder_tracks = cur.execute(
                "SELECT * FROM library_tracks WHERE album_id = ? ORDER BY disc_number, track_number, id", (holder_id,)
            ).fetchall()
            plan = _plan_track_merges(cur, source_tracks, holder_tracks)
            if plan is None:
                db.conn.rollback()
                return "conflict"
            merged_count, moved_count = _apply_track_merges(cur, plan, holder_id, str(holder_album["artist_id"]))
            _repoint_and_delete_source_album(cur, source_id, holder_id)
            db.conn.commit()
        except sqlite3.Error as exc:
            db.conn.rollback()
            logger.warning("Merging album %s into %s failed: %s", source_id, holder_id, safe_exc(exc))
            raise
    logger.info(
        "Merged single album '%s' (%s) into '%s' (%s): %d tracks merged, %d moved",
        source_album["title"],
        source_id,
        holder_album["title"],
        holder_id,
        merged_count,
        moved_count,
    )
    return "merged"


def _move_albums(cur: sqlite3.Cursor, keep_id: str, dupe_id: str) -> tuple[int, list[str]]:
    """Moves the dupe's albums under the keeper, folding same-title ones. Returns (folded count, albums to dedupe)."""
    keeper_by_title: dict[str, str] = {}
    for row in cur.execute(
        "SELECT id, clean_title FROM library_albums WHERE artist_id = ? ORDER BY created_at, rowid", (keep_id,)
    ).fetchall():
        keeper_by_title.setdefault(str(row["clean_title"]), str(row["id"]))
    folded = 0
    touched: list[str] = []
    for album in cur.execute("SELECT * FROM library_albums WHERE artist_id = ?", (dupe_id,)).fetchall():
        title = str(album["clean_title"])
        target = keeper_by_title.get(title) if title else None
        if target:
            _merge_album_rows(cur, target, album, keep_id)
            touched.append(target)
            folded += 1
        else:
            cur.execute("UPDATE library_albums SET artist_id = ? WHERE id = ?", (keep_id, str(album["id"])))
            if title:
                keeper_by_title[title] = str(album["id"])
    return folded, touched


def _repoint_artist_refs(cur: sqlite3.Cursor, keep_id: str, dupe_id: str) -> None:
    cur.execute("UPDATE library_tracks SET artist_id = ? WHERE artist_id = ?", (keep_id, dupe_id))
    cur.execute(
        "INSERT OR IGNORE INTO artist_tags (artist_id, tag_id) SELECT ?, tag_id FROM artist_tags WHERE artist_id = ?",
        (keep_id, dupe_id),
    )
    cur.execute("DELETE FROM artist_tags WHERE artist_id = ?", (dupe_id,))
    # artist_links is unique per library artist: the keeper's own link wins, the dupe's is dropped.
    cur.execute("UPDATE OR IGNORE artist_links SET library_artist_id = ? WHERE library_artist_id = ?", (keep_id, dupe_id))
    cur.execute("DELETE FROM artist_links WHERE library_artist_id = ?", (dupe_id,))
    for table, column in _ARTIST_REF_COLUMNS:
        cur.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (keep_id, dupe_id))


def _merge_artist_into(db: Any, keep_id: str, dupe_id: str) -> int:
    """Merges artist ``dupe_id`` into ``keep_id`` in one transaction and deletes the dupe; returns albums folded.

    Raises ``sqlite3.Error`` (after rolling back) when anything fails, leaving both artists untouched.
    """
    keep_id, dupe_id = str(keep_id), str(dupe_id)
    if keep_id == dupe_id:
        return 0
    with db._lock:
        try:
            cur = db.conn.cursor()
            dupe = cur.execute("SELECT * FROM library_artists WHERE id = ?", (dupe_id,)).fetchone()
            if dupe is None or cur.execute("SELECT 1 FROM library_artists WHERE id = ?", (keep_id,)).fetchone() is None:
                db.conn.rollback()
                return 0
            folded, touched = _move_albums(cur, keep_id, dupe_id)
            _repoint_artist_refs(cur, keep_id, dupe_id)
            _fill_empty(cur, "library_artists", _ARTIST_FILL_COLUMNS, keep_id, dupe)
            if dupe["monitored"]:
                cur.execute("UPDATE library_artists SET monitored = 1 WHERE id = ?", (keep_id,))
            cur.execute("DELETE FROM library_artists WHERE id = ?", (dupe_id,))
            db.conn.commit()
            if hasattr(db, "_clear_artist_key_cache"):
                db._clear_artist_key_cache()
        except sqlite3.Error as exc:
            db.conn.rollback()
            logger.warning("Merging artist %s into %s failed: %s", dupe_id, keep_id, safe_exc(exc))
            raise
    for album_id in dict.fromkeys(touched):
        db.merge_album_track_twins(album_id)
    _record_merge(db, keep_id, str(dupe["name"]))
    return folded


def _record_merge(db: Any, keep_id: str, dupe_name: str) -> None:
    try:
        db.record_item_event("relinked", artist_id=keep_id, message=f"Merged artist '{dupe_name}' into this artist")
    except (ValueError, sqlite3.Error) as exc:
        logger.warning("Could not record the artist merge of '%s': %s", dupe_name, safe_exc(exc))


# --- pass 1: duplicates sharing an MBID ------------------------------------------------------------------------------


def _pick_keeper(rows: list[sqlite3.Row], mb_name: Optional[str], album_counts: dict[str, int]) -> sqlite3.Row:
    """Exact MusicBrainz name first, then the most albums, then the oldest row."""
    exact = [r for r in rows if mb_name and str(r["name"]) == mb_name]
    pool = exact or rows
    return min(pool, key=lambda r: (-album_counts.get(str(r["id"]), 0), str(r["created_at"] or ""), str(r["id"])))


def merge_duplicate_mbid_artists(db: Any, enricher: Any, counts: dict[str, int], should_stop: StopFn = None) -> None:
    """Pass 1: merge rows that share an MBID and are the same artist after name folding."""
    rows = _fetchall(
        db,
        "SELECT id, name, mbid, created_at FROM library_artists WHERE mbid IS NOT NULL AND TRIM(mbid) != '' "
        "ORDER BY created_at, rowid",
    )
    by_mbid: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_mbid.setdefault(str(row["mbid"]).strip().lower(), []).append(row)
    album_counts = {
        str(r["artist_id"]): int(r["n"])
        for r in _fetchall(db, "SELECT artist_id, COUNT(*) AS n FROM library_albums GROUP BY artist_id")
    }
    for mbid, group in by_mbid.items():
        if len(group) < 2:
            continue
        if should_stop is not None and should_stop():
            return
        clusters: dict[str, list[sqlite3.Row]] = {}
        for row in group:
            clusters.setdefault(artist_name_key(str(row["name"])), []).append(row)
        mb_name = None
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            if mb_name is None:
                mb_name = _mb_name(enricher, mbid) or ""
            keeper = _pick_keeper(cluster, mb_name or None, album_counts)
            for dupe in cluster:
                if dupe["id"] == keeper["id"]:
                    continue
                try:
                    counts["albums_merged"] += _merge_artist_into(db, str(keeper["id"]), str(dupe["id"]))
                    counts["artists_merged"] += 1
                    album_counts[str(keeper["id"])] = album_counts.get(str(keeper["id"]), 0) + album_counts.get(
                        str(dupe["id"]), 0
                    )
                except sqlite3.Error:
                    counts["errors"] += 1


# --- pass 2: IDs that belong to someone else -------------------------------------------------------------------------


def _drop_wrong_discography(db: Any, artist_id: str) -> int:
    """Clears the artist's album links and deletes albums with no file, download or request. Returns albums deleted."""
    with db._lock:
        try:
            cur = db.conn.cursor()
            doomed = [str(r["id"]) for r in cur.execute(_REMOVABLE_ALBUMS_SQL, (artist_id,)).fetchall()]
            for album_id in doomed:
                cur.execute("DELETE FROM library_tracks WHERE album_id = ?", (album_id,))
                cur.execute("DELETE FROM library_albums WHERE id = ?", (album_id,))
            cur.execute(
                "UPDATE library_albums SET mb_release_group_id = NULL, mb_release_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE artist_id = ? "
                "AND (mb_release_group_id IS NOT NULL OR mb_release_id IS NOT NULL)",
                (artist_id,),
            )
            db.conn.commit()
        except sqlite3.Error:
            db.conn.rollback()
            raise
    return len(doomed)


def _set_artist_mbid(db: Any, artist_id: str, mbid: Optional[str]) -> None:
    with db._lock:
        try:
            db.conn.execute(
                "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (mbid, artist_id)
            )
            db.conn.commit()
        except sqlite3.Error:
            db.conn.rollback()
            raise


def _repair_one_mismatch(db: Any, enricher: Any, row: sqlite3.Row, counts: dict[str, int]) -> None:
    mbid = str(row["mbid"]).strip()
    mb_name = _mb_name(enricher, mbid)
    if mb_name is None or artist_name_key(str(row["name"])) == artist_name_key(mb_name):
        return  # unknown to MusicBrainz right now, or the name agrees: nothing to judge
    answered, found = enricher.resolve_artist_mbid(str(row["name"]))
    if not answered:
        counts["lookups_unavailable"] += 1
        logger.info("MusicBrainz could not be asked about %r; leaving its ID alone", row["name"])
        return
    if found and str(found).lower() == mbid.lower():
        counts["ids_kept_alias"] += 1
        return
    logger.info("Artist %r holds the MusicBrainz ID of %r; %s", row["name"], mb_name, f"using {found}" if found else "clearing it")
    _set_artist_mbid(db, str(row["id"]), str(found) if found else None)
    counts["ids_replaced" if found else "ids_cleared"] += 1
    counts["wrong_albums_removed"] += _drop_wrong_discography(db, str(row["id"]))


def clear_mismatched_mbids(
    db: Any, enricher: Any, counts: dict[str, int], should_stop: StopFn = None, on_progress: ProgressFn = None
) -> None:
    """Pass 2: replace or clear artist IDs whose MusicBrainz name does not match the artist."""
    rows = _fetchall(
        db, "SELECT id, name, mbid FROM library_artists WHERE mbid IS NOT NULL AND TRIM(mbid) != '' ORDER BY name"
    )
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        if should_stop is not None and should_stop():
            return
        try:
            _repair_one_mismatch(db, enricher, row, counts)
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("Checking the MusicBrainz ID of %r failed: %s", row["name"], safe_exc(exc))
        if on_progress is not None:
            on_progress(index, total)


# --- pass 3: joined-credit artists -----------------------------------------------------------------------------------


def _artist_index(db: Any) -> dict[str, list[tuple[str, bool]]]:
    """Folded name -> [(artist id, has mbid)] for every library artist."""
    index: dict[str, list[tuple[str, bool]]] = {}
    for row in _fetchall(db, "SELECT id, name, mbid FROM library_artists"):
        index.setdefault(artist_name_key(str(row["name"])), []).append((str(row["id"]), not _empty(row["mbid"])))
    return index


def _others(index: dict[str, list[tuple[str, bool]]], key: str, source_id: str) -> list[tuple[str, bool]]:
    return [entry for entry in index.get(key, []) if entry[0] != source_id]


def primary_credit(
    name: str, known_artist: Callable[[str], bool] | None = None
) -> str | None:
    """Primary artist of a joined credit (';', ' / ', 'feat.', ', ', ' & ', ' x '); None when not split."""
    text = (name or "").strip()
    if not text:
        return None
    primary = primary_artist(text, known_artist)
    if primary and artist_name_key(primary) != artist_name_key(text):
        return primary
    for pattern in (_X_SEPARATOR_RE, _JOIN_SEPARATOR_RE):
        match = pattern.search(text)
        if match:
            head = text[: match.start()].strip()
            if head and artist_name_key(head) != artist_name_key(text):
                return head
    return None


def _joined_credit_primary(
    name: str, source_id: str, index: dict[str, list[tuple[str, bool]]]
) -> tuple[str, bool]:
    """(primary, needs_check) for a possibly joined credit; ``name`` itself when it is not one.

    ``needs_check`` marks a ", " / " & " / " x " split: it is only safe once MusicBrainz confirms the whole name is
    not an artist ("Earth, Wind & Fire", "Tyler, The Creator").
    """

    def known(candidate: str) -> bool:
        return bool(_others(index, artist_name_key(candidate), source_id))

    primary = primary_artist(name, known)
    if artist_name_key(primary) != artist_name_key(name):
        return primary, False
    head = primary_credit(name, known)
    if head and any(has_id for _, has_id in _others(index, artist_name_key(head), source_id)):
        return head, True
    return name, False


def _full_name_is_not_an_artist(enricher: Any, name: str, counts: dict[str, int]) -> bool:
    """True only when MusicBrainz answered and has no artist of exactly this name."""
    answered, found = enricher.resolve_artist_mbid(name)
    if not answered:
        counts["lookups_unavailable"] += 1
        return False
    return found is None


def _refile_one(
    db: Any, enricher: Any, row: sqlite3.Row, index: dict[str, list[tuple[str, bool]]], counts: dict[str, int]
) -> bool:
    source_id = str(row["id"])
    primary, needs_check = _joined_credit_primary(str(row["name"]), source_id, index)
    key = artist_name_key(primary)
    if not key or key == artist_name_key(str(row["name"])):
        return False
    if needs_check and not _full_name_is_not_an_artist(enricher, str(row["name"]), counts):
        return False
    targets = _others(index, key, source_id)
    if targets:
        target_id = min(targets, key=lambda e: not e[1])[0]
    else:
        target_id = str(uuid.uuid4())
        db.upsert_library_artist(
            LibraryArtist(id=target_id, name=primary, monitored=bool(row["monitored"])), preserve_monitoring=False
        )
        index.setdefault(key, []).append((target_id, False))
    _merge_artist_into(db, target_id, source_id)
    index[artist_name_key(str(row["name"]))] = [
        e for e in index.get(artist_name_key(str(row["name"])), []) if e[0] != source_id
    ]
    return True


def refile_joined_credit_artists(db: Any, enricher: Any, counts: dict[str, int], should_stop: StopFn = None) -> None:
    """Pass 3: file ID-less joined-credit artists under their primary artist."""
    rows = _fetchall(
        db, "SELECT id, name, monitored FROM library_artists WHERE mbid IS NULL OR TRIM(mbid) = '' ORDER BY name"
    )
    index = _artist_index(db)
    for row in rows:
        if should_stop is not None and should_stop():
            return
        try:
            if _refile_one(db, enricher, row, index, counts):
                counts["credits_refiled"] += 1
        except sqlite3.Error as exc:
            counts["errors"] += 1
            logger.warning("Refiling joined-credit artist %r failed: %s", row["name"], safe_exc(exc))


# --- entry points ----------------------------------------------------------------------------------------------------


def run_artist_repair(
    db: Any, enricher: Any, should_stop: StopFn = None, on_progress: ProgressFn = None
) -> dict[str, int]:
    """Runs the three repair passes in order and returns what changed."""
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    merge_duplicate_mbid_artists(db, enricher, counts, should_stop)
    if should_stop is not None and should_stop():
        return counts
    clear_mismatched_mbids(db, enricher, counts, should_stop, on_progress)
    if should_stop is not None and should_stop():
        return counts
    refile_joined_credit_artists(db, enricher, counts, should_stop)
    logger.info("Artist repair finished: %s", counts)
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in counts.items())


def run_repair_and_match(db: Any, run: Any, extra_stop: StopFn = None) -> None:
    """Body of the ``mb_release_match`` task: back up, repair artist rows, then match albums to releases.

    ``run`` is the task's ``RunHandle``. ``extra_stop`` lets the start-up caller also stop on process shutdown.
    """
    from trackseerr.backup import create_backup
    from trackseerr.mb_metadata_store import get_shared_enricher
    from trackseerr.recording_search import fill_recordings_from_search
    from trackseerr.release_matching import (
        fill_recordings_from_fingerprints,
        link_albums_by_recording,
        match_album_releases,
        merge_title_duplicates,
    )

    def stop() -> bool:
        return bool(run.should_stop() or (extra_stop is not None and extra_stop()))

    try:
        create_backup(db, kind="pre_repair")
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not take the pre-repair backup, continuing: %s", safe_exc(exc))
    enricher = get_shared_enricher(db)
    repair = run_artist_repair(db, enricher, should_stop=stop, on_progress=run.set_progress)
    run.message = f"repair: {_format_counts(repair)}"
    if stop():
        return
    api_key = str(db.get_media_management_settings().get("acoustid_api_key") or "").strip()
    if api_key:
        filled = fill_recordings_from_fingerprints(
            db, enricher, api_key, should_stop=stop, on_progress=run.set_progress
        )
        run.message = f"{run.message}; fill: {_format_counts(filled)}"
        if stop():
            return
    else:
        run.message = f"{run.message}; fill: skipped (no AcoustID key)"
    searched = fill_recordings_from_search(db, enricher, should_stop=stop, on_progress=run.set_progress)
    run.message = f"{run.message}; search: {_format_counts(searched)}"
    if stop():
        return
    linked = link_albums_by_recording(db, enricher, should_stop=stop, on_progress=run.set_progress)
    run.message = f"{run.message}; link: {_format_counts(linked)}"
    if stop():
        return
    dupes = merge_title_duplicates(db, should_stop=stop, on_progress=run.set_progress)
    run.message = f"{run.message}; dupes: {_format_counts(dupes)}"
    if stop():
        return
    counts = match_album_releases(db, enricher, should_stop=stop, on_progress=run.set_progress)
    run.message = f"{run.message}; match: {_format_counts(counts)}"


def run_startup_artist_repair(db: Any, stopping: StopFn = None) -> bool:
    """Runs the repair once per database (marker ``artist_repair_v1``), recorded as a start-up task run.

    Returns True when it ran to completion. A failure is logged and leaves the marker unset, so the next start retries.
    """
    from trackseerr.task_manager import TRIGGER_STARTUP, record_task_run

    if db.get_kv(REPAIR_KV_KEY) is not None:
        return False
    try:
        with record_task_run(db, "mb_release_match", TRIGGER_STARTUP) as run:
            run_repair_and_match(db, run, stopping)
            if (stopping is not None and stopping()) or run.should_stop():
                run.cancelled = True
                return False
    except Exception as exc:  # last resort for a daemon thread; record_task_run already logged and recorded the failure
        logger.error("Start-up artist repair failed, will retry next start: %s", safe_exc(exc))
        logger.debug("Artist repair traceback", exc_info=True)
        return False
    db.set_kv(REPAIR_KV_KEY, datetime.now(timezone.utc).isoformat())
    return True


def start_startup_artist_repair(db: Any, stopping: StopFn = None) -> Optional[threading.Thread]:
    """Starts the one-time repair on a daemon thread in native library mode; None when not needed."""
    from trackseerr.library_manager import get_library_mode

    if get_library_mode(db) == "lidarr" or db.get_kv(REPAIR_KV_KEY) is not None:
        return None
    thread = threading.Thread(
        target=run_startup_artist_repair, args=(db, stopping), daemon=True, name="StartupArtistRepairTask"
    )
    thread.start()
    return thread
