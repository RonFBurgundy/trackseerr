"""Native library albums, tracks, files, stats, and library collections.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from typing import Any, Optional, Union

from trackseerr.library_monitoring import (
    normalize_secondary_types,
)
from trackseerr.list_index import fold_search_text, library_sort_key
from trackseerr.models import (
    LibraryAlbum,
    LibraryCollection,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.storage_common import (
    _TRACK_DURATION_TOLERANCE,
    _has_extra_bracket_suffix,
    _titles_near_equal,
    clean_library_name,
)

logger = logging.getLogger(__name__)


class LibraryCatalogStoreMixin:
    def upsert_library_album(
        self, album_data: Union[LibraryAlbum, dict[str, Any]], preserve_monitoring: bool = False
    ) -> dict[str, Any]:
        """Creates or updates a native library album (``preserve_monitoring`` keeps an existing row's flag)."""
        d = album_data.to_dict() if hasattr(album_data, "to_dict") else dict(album_data)
        album_id = str(d.get("id") or uuid.uuid4())
        artist_id = str(d.get("artist_id") or "")
        title = str(d.get("title") or "")
        clean_title = clean_library_name(d.get("clean_title") or title)
        sort_title = library_sort_key(title)
        foreign_album_id = str(d["foreign_album_id"]) if d.get("foreign_album_id") is not None else None
        release_date = str(d["release_date"]) if d.get("release_date") is not None else None
        year = int(d["year"]) if d.get("year") is not None else None
        album_type = str(d.get("album_type") or "album")
        monitored = 1 if d.get("monitored", True) else 0
        path = str(d["path"]) if d.get("path") is not None else None
        cover_url = str(d["cover_url"]) if d.get("cover_url") is not None else None
        total_tracks = int(d["total_tracks"]) if d.get("total_tracks") is not None else None
        mb_release_group_id = str(d["mb_release_group_id"]) if d.get("mb_release_group_id") is not None else None
        mb_release_id = str(d["mb_release_id"]) if d.get("mb_release_id") is not None else None
        genres = str(d["genres"]) if d.get("genres") is not None else None
        raw_secondary = d.get("secondary_types")
        normalized_secondary = normalize_secondary_types(raw_secondary)
        secondary_types = json.dumps(normalized_secondary) if normalized_secondary is not None else None
        created_at = d.get("created_at")

        with self._lock:
            is_new = self.conn.execute("SELECT 1 FROM library_albums WHERE id = ?", (album_id,)).fetchone() is None
            self.conn.execute(
                """
                INSERT INTO library_albums (
                    id, artist_id, title, clean_title, sort_title, search_text, search_clean, foreign_album_id,
                    release_date, year, album_type, monitored, path, cover_url, total_tracks,
                    mb_release_group_id, mb_release_id, genres, secondary_types,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    artist_id = excluded.artist_id,
                    title = excluded.title,
                    clean_title = excluded.clean_title,
                    sort_title = excluded.sort_title,
                    search_text = excluded.search_text,
                    search_clean = excluded.search_clean,
                    foreign_album_id = COALESCE(excluded.foreign_album_id, library_albums.foreign_album_id),
                    release_date = COALESCE(excluded.release_date, library_albums.release_date),
                    year = COALESCE(excluded.year, library_albums.year),
                    album_type = excluded.album_type,
                    monitored = CASE WHEN ? THEN library_albums.monitored ELSE excluded.monitored END,
                    path = COALESCE(excluded.path, library_albums.path),
                    cover_url = COALESCE(excluded.cover_url, library_albums.cover_url),
                    total_tracks = COALESCE(excluded.total_tracks, library_albums.total_tracks),
                    mb_release_group_id = COALESCE(excluded.mb_release_group_id, library_albums.mb_release_group_id),
                    mb_release_id = COALESCE(excluded.mb_release_id, library_albums.mb_release_id),
                    genres = COALESCE(excluded.genres, library_albums.genres),
                    secondary_types = COALESCE(excluded.secondary_types, library_albums.secondary_types),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    album_id,
                    artist_id,
                    title,
                    clean_title,
                    sort_title,
                    fold_search_text(title),
                    fold_search_text(clean_title),
                    foreign_album_id,
                    release_date,
                    year,
                    album_type,
                    monitored,
                    path,
                    cover_url,
                    total_tracks,
                    mb_release_group_id,
                    mb_release_id,
                    genres,
                    secondary_types,
                    created_at,
                    1 if preserve_monitoring else 0,
                ),
            )
            self.conn.commit()
            if is_new:
                self._safe_item_event(
                    "added_to_library", album_id=album_id, artist_id=artist_id or None, message=f"Added album '{title}'"
                )

        album = self.get_library_album(album_id)
        if album is None:
            raise RuntimeError(f"Failed to upsert library album {album_id}")
        return album

    def set_library_album_total_tracks(self, album_id: str, count: int, authoritative: bool = False) -> None:
        """Stores the release's track count (ignored when not positive).

        The stored value only grows (``max(existing, count)``): a provider can describe a shorter edition than the one
        already recorded. ``authoritative=True`` (a full MusicBrainz release with all media) replaces it outright.
        """
        if int(count) <= 0:
            return
        sql = (
            "UPDATE library_albums SET total_tracks = ? WHERE id = ?"
            if authoritative
            else "UPDATE library_albums SET total_tracks = MAX(COALESCE(total_tracks, 0), ?) WHERE id = ?"
        )
        with self._lock:
            self.conn.execute(sql, (int(count), str(album_id)))
            self.conn.commit()

    def get_library_album(self, album_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single library album by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_albums WHERE id = ?",
                (str(album_id),),
            )
            row = cur.fetchone()
            return self._map_library_album(row) if row else None

    def get_library_album_by_title(
        self, artist_id: str, title: str, release_group_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library album by artist ID and title using clean_library_name.

        When ``release_group_id`` is given, a row already linked to a different release group is never returned
        (an album and a single can share a normalised title); an exact RG match is preferred over an unlinked row.
        """
        clean = clean_library_name(title)
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_albums WHERE artist_id = ? AND clean_title = ?",
                (str(artist_id), clean),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            if release_group_id is None:
                return self._map_library_album(rows[0])
            rg = str(release_group_id)
            unlinked = None
            for row in rows:
                linked = row["mb_release_group_id"]
                if linked and str(linked) == rg:
                    return self._map_library_album(row)
                if not linked and unlinked is None:
                    unlinked = row
            return self._map_library_album(unlinked) if unlinked is not None else None

    def get_library_album_by_foreign_id(
        self, foreign_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library album by foreign_album_id."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_albums WHERE foreign_album_id = ? LIMIT 1",
                (str(foreign_id),),
            )
            row = cur.fetchone()
            return self._map_library_album(row) if row else None

    def get_library_album_by_release_group_id(
        self, mb_release_group_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library album by MusicBrainz release group ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_albums WHERE mb_release_group_id = ? LIMIT 1",
                (str(mb_release_group_id),),
            )
            row = cur.fetchone()
            return self._map_library_album(row) if row else None


    def list_library_albums(
        self,
        artist_id: Optional[str] = None,
        monitored_only: bool = False,
        query: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Lists library albums with optional artist filtering and pagination."""
        sql = "SELECT * FROM library_albums WHERE 1=1"
        params: list[Any] = []
        if artist_id:
            sql += " AND artist_id = ?"
            params.append(str(artist_id))
        if monitored_only:
            sql += " AND monitored = 1"
        if query:
            clean_q = clean_library_name(query)
            sql += " AND (clean_title LIKE ? OR title LIKE ?)"
            params.extend([f"%{clean_q}%", f"%{query}%"])
        sql += " ORDER BY year DESC, title COLLATE NOCASE ASC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

        with self._lock:
            cur = self.conn.execute(sql, params)
            return [self._map_library_album(row) for row in cur.fetchall()]

    def delete_library_album(self, album_id: str) -> bool:
        """Deletes a library album and cascades to child tracks and files (history is kept)."""
        with self._lock:
            self._emit_removed("album", str(album_id))
            cur = self.conn.execute(
                "DELETE FROM library_albums WHERE id = ?",
                (str(album_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def _cancel_pending_profile_recompute_for_albums(self, album_ids: list[str]) -> None:
        """A manual album monitor edit cancels a deferred profile recompute so it can never undo the user's choice.

        Caller holds ``self._lock``; the surrounding transaction commits.
        """
        marks = ", ".join("?" for _ in album_ids)
        self.conn.execute(
            "UPDATE library_artists SET pending_profile_recompute = 0 WHERE pending_profile_recompute = 1 "
            f"AND id IN (SELECT artist_id FROM library_albums WHERE id IN ({marks}))",
            list(album_ids),
        )

    def _cancel_pending_profile_recompute_for_tracks(self, track_ids: list[str]) -> None:
        """A manual track monitor edit cancels a deferred profile recompute (same rule as for albums).

        Caller holds ``self._lock``; the surrounding transaction commits.
        """
        marks = ", ".join("?" for _ in track_ids)
        self.conn.execute(
            "UPDATE library_artists SET pending_profile_recompute = 0 WHERE pending_profile_recompute = 1 "
            f"AND id IN (SELECT artist_id FROM library_tracks WHERE id IN ({marks}))",
            list(track_ids),
        )

    def set_library_artist_mbid(self, artist_id: str, mbid: str) -> None:
        """Stores a resolved MusicBrainz artist id."""
        with self._lock:
            self.conn.execute(
                "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(mbid), str(artist_id)),
            )
            self.conn.commit()

    def set_pending_profile_recompute(self, artist_id: str, pending: bool) -> None:
        """Sets or clears the deferred metadata-profile recompute flag of an artist."""
        with self._lock:
            self.conn.execute(
                "UPDATE library_artists SET pending_profile_recompute = ? WHERE id = ?",
                (1 if pending else 0, str(artist_id)),
            )
            self.conn.commit()

    def set_album_monitored(
        self, album_id: str, monitored: bool, cascade_tracks: bool = True
    ) -> bool:
        """Sets monitoring status for an album and optionally cascades to child tracks."""
        val = 1 if monitored else 0
        with self._lock:
            before = self.conn.execute("SELECT monitored FROM library_albums WHERE id = ?", (str(album_id),)).fetchone()
            cur = self.conn.execute(
                "UPDATE library_albums SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (val, str(album_id)),
            )
            if cur.rowcount == 0:
                return False
            self._cancel_pending_profile_recompute_for_albums([str(album_id)])
            if cascade_tracks:
                self.conn.execute(
                    "UPDATE library_tracks SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE album_id = ?",
                    (val, str(album_id)),
                )
            self.conn.commit()
            if before is not None and int(before["monitored"]) != val:
                self._safe_item_event(
                    "monitored" if val else "unmonitored", album_id=str(album_id),
                    details={"cascade_tracks": bool(cascade_tracks)},
                )
            return True

    def upsert_library_track(
        self, track_data: Union[LibraryTrack, dict[str, Any]], preserve_monitoring: bool = False
    ) -> dict[str, Any]:
        """Creates or updates a native library track (``preserve_monitoring`` keeps an existing row's flag)."""
        d = track_data.to_dict() if hasattr(track_data, "to_dict") else dict(track_data)
        track_id = str(d.get("id") or uuid.uuid4())
        album_id = str(d.get("album_id") or "")
        artist_id = str(d.get("artist_id") or "")
        title = str(d.get("title") or "")
        clean_title = clean_library_name(d.get("clean_title") or title)
        sort_title = library_sort_key(title)
        track_number = int(d.get("track_number", 1))
        disc_number = int(d.get("disc_number", 1))
        duration_seconds = float(d["duration_seconds"]) if d.get("duration_seconds") is not None else None
        monitored = 1 if d.get("monitored", True) else 0
        foreign_track_id = str(d["foreign_track_id"]) if d.get("foreign_track_id") is not None else None
        mb_recording_id = str(d["mb_recording_id"]) if d.get("mb_recording_id") is not None else None
        isrc = str(d["isrc"]) if d.get("isrc") is not None else None
        created_at = d.get("created_at")

        with self._lock:
            if self.conn.execute("SELECT 1 FROM library_tracks WHERE id = ?", (track_id,)).fetchone() is None:
                # A new id for a track the album already holds is a duplicate, not a new track: reuse the row.
                dup = None
                if foreign_track_id:
                    dup = self.conn.execute(
                        "SELECT id FROM library_tracks WHERE album_id = ? AND foreign_track_id = ? LIMIT 1",
                        (album_id, foreign_track_id),
                    ).fetchone()
                if dup is None and clean_title and duration_seconds is not None:
                    # disc/number default to 1, so only a non-empty title AND a matching known duration prove identity.
                    dup = self.conn.execute(
                        "SELECT id FROM library_tracks WHERE album_id = ? AND disc_number = ? "
                        "AND track_number = ? AND clean_title = ? AND duration_seconds IS NOT NULL "
                        "AND ABS(duration_seconds - ?) <= ? LIMIT 1",
                        (album_id, disc_number, track_number, clean_title, duration_seconds,
                         _TRACK_DURATION_TOLERANCE),
                    ).fetchone()
                if dup is not None:
                    track_id = str(dup[0])
            self.conn.execute(
                """
                INSERT INTO library_tracks (
                    id, album_id, artist_id, title, clean_title, sort_title, search_text, search_clean,
                    track_number, disc_number, duration_seconds, monitored, foreign_track_id,
                    mb_recording_id, isrc,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    album_id = excluded.album_id,
                    artist_id = excluded.artist_id,
                    title = excluded.title,
                    clean_title = excluded.clean_title,
                    sort_title = excluded.sort_title,
                    search_text = excluded.search_text,
                    search_clean = excluded.search_clean,
                    track_number = excluded.track_number,
                    disc_number = excluded.disc_number,
                    duration_seconds = COALESCE(excluded.duration_seconds, library_tracks.duration_seconds),
                    monitored = CASE WHEN ? THEN library_tracks.monitored ELSE excluded.monitored END,
                    foreign_track_id = COALESCE(excluded.foreign_track_id, library_tracks.foreign_track_id),
                    mb_recording_id = COALESCE(excluded.mb_recording_id, library_tracks.mb_recording_id),
                    isrc = COALESCE(excluded.isrc, library_tracks.isrc),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    track_id,
                    album_id,
                    artist_id,
                    title,
                    clean_title,
                    sort_title,
                    fold_search_text(title),
                    fold_search_text(clean_title),
                    track_number,
                    disc_number,
                    duration_seconds,
                    monitored,
                    foreign_track_id,
                    mb_recording_id,
                    isrc,
                    created_at,
                    1 if preserve_monitoring else 0,
                ),
            )
            self.conn.commit()

        track = self.get_library_track(track_id)
        if track is None:
            raise RuntimeError(f"Failed to upsert library track {track_id}")
        return track

    _CREDIT_SOURCE_RANK = {"title": 1, "tags": 2, "musicbrainz": 3}

    def replace_track_artists(self, track_id: str, credits: list[dict[str, Any]]) -> None:
        """Replaces a track's artist credits (positions 0..n in list order) in one transaction.

        Source precedence is musicbrainz > tags > title: the incoming list is ranked by its best source, and if
        the track already has rows from a higher-precedence source the call is a no-op, so MusicBrainz credits are
        never overwritten by tag- or title-derived ones. Equal or higher incoming rank replaces.
        """
        rank = self._CREDIT_SOURCE_RANK
        incoming = max((rank.get(str(c.get("source")), 0) for c in credits), default=0)
        with self._lock:
            existing_sources = [
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT source FROM library_track_artists WHERE track_id = ?", (str(track_id),)
                ).fetchall()
            ]
            existing = max((rank.get(str(s), 0) for s in existing_sources), default=0)
            if existing > incoming:
                return
            try:
                self.conn.execute("DELETE FROM library_track_artists WHERE track_id = ?", (str(track_id),))
                for position, credit in enumerate(credits):
                    self.conn.execute(
                        "INSERT INTO library_track_artists "
                        "(track_id, position, artist_name, artist_mbid, role, join_phrase, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(track_id),
                            position,
                            str(credit["artist_name"]),
                            credit.get("artist_mbid") or None,
                            str(credit.get("role") or "primary"),
                            str(credit.get("join_phrase") or ""),
                            str(credit.get("source") or "musicbrainz"),
                        ),
                    )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

    def list_albums_needing_credits(self, after_id: Optional[str], limit: int) -> list[dict[str, Any]]:
        """Albums with a track that has a file but no artist credits, keyset-paged by album id (ascending)."""
        sql = (
            "SELECT a.id, a.title, a.mb_release_group_id, a.artist_id FROM library_albums a "
            "WHERE EXISTS (SELECT 1 FROM library_tracks t WHERE t.album_id = a.id "
            "AND EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) "
            "AND NOT EXISTS (SELECT 1 FROM library_track_artists c WHERE c.track_id = t.id))"
        )
        params: list[Any] = []
        if after_id:
            sql += " AND a.id > ?"
            params.append(str(after_id))
        sql += " ORDER BY a.id LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            {"id": r[0], "title": r[1], "mb_release_group_id": r[2], "artist_id": r[3]} for r in rows
        ]

    def list_album_tracks_with_files(self, album_id: str) -> list[dict[str, Any]]:
        """Tracks of an album that have a file, with one file path each and whether credits already exist."""
        sql = (
            "SELECT t.id, t.title, t.disc_number, t.track_number, t.mb_recording_id, "
            "(SELECT MIN(f.file_path) FROM library_files f WHERE f.track_id = t.id), "
            "EXISTS (SELECT 1 FROM library_track_artists c WHERE c.track_id = t.id) "
            "FROM library_tracks t WHERE t.album_id = ? "
            "AND EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) "
            "ORDER BY t.disc_number, t.track_number, t.id"
        )
        with self._lock:
            rows = self.conn.execute(sql, (str(album_id),)).fetchall()
        return [
            {
                "id": r[0], "title": r[1], "disc_number": r[2], "track_number": r[3],
                "mb_recording_id": r[4], "file_path": r[5], "has_credits": bool(r[6]),
            }
            for r in rows
        ]

    _CREDIT_COLUMNS = "track_id, position, artist_name, artist_mbid, role, join_phrase, source"

    @staticmethod
    def _credit_row(row: Any) -> dict[str, Any]:
        return {
            "position": row[1],
            "artist_name": row[2],
            "artist_mbid": row[3],
            "role": row[4],
            "join_phrase": row[5],
            "source": row[6],
        }

    def list_track_artists(self, track_id: str) -> list[dict[str, Any]]:
        """Returns a track's artist credits ordered by position."""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {self._CREDIT_COLUMNS} FROM library_track_artists WHERE track_id = ? ORDER BY position",
                (str(track_id),),
            ).fetchall()
        return [self._credit_row(r) for r in rows]

    def list_track_artists_for_album(self, album_id: str) -> dict[str, list[dict[str, Any]]]:
        """Returns ``{track_id: credits}`` for every credited track of an album (one query)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT a.track_id, a.position, a.artist_name, a.artist_mbid, a.role, a.join_phrase, a.source "
                "FROM library_track_artists a JOIN library_tracks t ON t.id = a.track_id "
                "WHERE t.album_id = ? ORDER BY a.track_id, a.position",
                (str(album_id),),
            ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r[0]), []).append(self._credit_row(r))
        return out

    def get_library_track(self, track_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single library track by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_tracks WHERE id = ?",
                (str(track_id),),
            )
            row = cur.fetchone()
            return self._map_library_track(row) if row else None

    def get_library_track_by_mb_recording_id(
        self, mb_recording_id: str, album_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library track by MusicBrainz recording id (first match), or None.

        When ``album_id`` is given the match is limited to that album.
        """
        if not mb_recording_id:
            return None
        query = "SELECT * FROM library_tracks WHERE mb_recording_id = ?"
        params: list[Any] = [str(mb_recording_id)]
        if album_id is not None:
            query += " AND album_id = ?"
            params.append(str(album_id))
        with self._lock:
            cur = self.conn.execute(query + " LIMIT 1", params)
            row = cur.fetchone()
            return self._map_library_track(row) if row else None

    def get_library_track_by_foreign_id(
        self, foreign_id: str, album_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library track by foreign_track_id and optional album_id."""
        with self._lock:
            if album_id:
                cur = self.conn.execute(
                    "SELECT * FROM library_tracks WHERE foreign_track_id = ? AND album_id = ? LIMIT 1",
                    (str(foreign_id), str(album_id)),
                )
            else:
                cur = self.conn.execute(
                    "SELECT * FROM library_tracks WHERE foreign_track_id = ? LIMIT 1",
                    (str(foreign_id),),
                )
            row = cur.fetchone()
            return self._map_library_track(row) if row else None

    def get_library_track_by_title(
        self,
        album_id: str,
        title: str,
        track_number: Optional[int] = None,
        disc_number: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library track by album ID and title, with optional track-number and disc tiebreakers.

        Order: exact clean title; clean title ignoring spaces ('Nightvision' finds 'Night Vision'); then, only when
        a track number is supplied, a track holding that number whose title is near-equal. A track number alone never
        matches: untagged or differently named tracks sharing a number are different tracks.

        With ``disc_number`` the title matches prefer a row on the same disc (and number); a row from another disc is
        returned only when no track number is given, and the near-equal fallback only considers same-disc,
        same-number candidates. Without it, discs are ignored.
        """
        clean = clean_library_name(title)
        with self._lock:
            row = self._pick_title_match(
                self.conn.execute(
                    "SELECT * FROM library_tracks WHERE album_id = ? AND clean_title = ?", (str(album_id), clean)
                ).fetchall(),
                track_number,
                disc_number,
            )
            if row:
                return self._map_library_track(row)
            squashed = clean.replace(" ", "")
            if squashed:
                row = self._pick_title_match(
                    self.conn.execute(
                        "SELECT * FROM library_tracks WHERE album_id = ? AND REPLACE(clean_title, ' ', '') = ?",
                        (str(album_id), squashed),
                    ).fetchall(),
                    track_number,
                    disc_number,
                )
                if row:
                    return self._map_library_track(row)
            if track_number is not None and clean:
                for cand in self.conn.execute(
                    "SELECT * FROM library_tracks WHERE album_id = ? AND track_number = ?",
                    (str(album_id), int(track_number)),
                ).fetchall():
                    if disc_number is not None and int(cand["disc_number"] or 1) != int(disc_number):
                        continue
                    if _titles_near_equal(str(cand["clean_title"] or ""), clean) and not _has_extra_bracket_suffix(
                        str(cand["title"] or ""), str(title or "")
                    ):
                        return self._map_library_track(cand)
            return None

    @staticmethod
    def _pick_title_match(
        rows: list[Any], track_number: Optional[int], disc_number: Optional[int]
    ) -> Optional[Any]:
        """Chooses among same-title rows; with a disc, prefers the same disc and crosses discs only without a number."""
        if not rows:
            return None
        if disc_number is None:
            return rows[0]
        pool = [r for r in rows if int(r["disc_number"] or 1) == int(disc_number)]
        if not pool:
            pool = rows if track_number is None else []
        if not pool:
            return None
        if track_number is not None:
            for r in pool:
                if int(r["track_number"] or 1) == int(track_number):
                    return r
        return pool[0]

    def list_library_tracks(
        self,
        album_id: Optional[str] = None,
        artist_id: Optional[str] = None,
        monitored_only: bool = False,
        query: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Lists library tracks filtered by album and/or artist with pagination."""
        sql = "SELECT * FROM library_tracks WHERE 1=1"
        params: list[Any] = []
        if album_id:
            sql += " AND album_id = ?"
            params.append(str(album_id))
        if artist_id:
            sql += " AND artist_id = ?"
            params.append(str(artist_id))
        if monitored_only:
            sql += " AND monitored = 1"
        if query:
            clean_q = clean_library_name(query)
            sql += " AND (clean_title LIKE ? OR title LIKE ?)"
            params.extend([f"%{clean_q}%", f"%{query}%"])
        sql += " ORDER BY disc_number ASC, track_number ASC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

        with self._lock:
            cur = self.conn.execute(sql, params)
            return [self._map_library_track(row) for row in cur.fetchall()]

    def iter_library_files_for_rename(
        self, batch_size: int = 1000
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Yields ``(track, file)`` for every file that has a track, in artist/album/disc/track/file order.

        Keyset-paginated (no OFFSET) and driven from ``library_files`` so tracks without a file are never touched.
        ``track`` carries id, artist_id, album_id, title, track_number and disc_number.
        """
        cols = ("id", "artist_id", "album_id", "title", "track_number", "disc_number")
        select = "SELECT f.*, " + ", ".join(f"t.{c} AS t_{c}" for c in cols)
        order = "t.artist_id, t.album_id, t.disc_number, t.track_number, f.id"
        after: Optional[tuple[Any, ...]] = None
        while True:
            where = ""
            params: list[Any] = []
            if after is not None:
                where = " WHERE (t.artist_id, t.album_id, t.disc_number, t.track_number, f.id) > (?, ?, ?, ?, ?)"
                params.extend(after)
            params.append(int(batch_size))
            with self._lock:
                rows = self.conn.execute(
                    f"{select} FROM library_files f JOIN library_tracks t ON f.track_id = t.id"
                    f"{where} ORDER BY {order} LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row in rows:
                d = dict(row)
                track = {c: d.pop(f"t_{c}") for c in cols}
                yield track, self._map_library_file(d)  # type: ignore[arg-type]
            last = rows[-1]
            after = (last["t_artist_id"], last["t_album_id"], last["t_disc_number"], last["t_track_number"], last["id"])
            if len(rows) < batch_size:
                return

    def delete_library_track(self, track_id: str) -> bool:
        """Deletes a library track and cascades to child files (history is kept)."""
        with self._lock:
            self._emit_removed("track", str(track_id))
            cur = self.conn.execute(
                "DELETE FROM library_tracks WHERE id = ?",
                (str(track_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_track_monitored(self, track_id: str, monitored: bool) -> bool:
        """Sets monitoring status for a single library track."""
        val = 1 if monitored else 0
        with self._lock:
            before = self.conn.execute("SELECT monitored FROM library_tracks WHERE id = ?", (str(track_id),)).fetchone()
            cur = self.conn.execute(
                "UPDATE library_tracks SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (val, str(track_id)),
            )
            if cur.rowcount > 0:
                self._cancel_pending_profile_recompute_for_tracks([str(track_id)])
            self.conn.commit()
            if cur.rowcount > 0 and before is not None and int(before["monitored"]) != val:
                self._safe_item_event("monitored" if val else "unmonitored", track_id=str(track_id))
            return cur.rowcount > 0

    def bulk_set_tracks_monitored(self, track_ids: list[str], monitored: bool) -> int:
        """Sets ``monitored`` on many tracks in one transaction; returns tracks updated."""
        val = 1 if monitored else 0
        unique = list(dict.fromkeys(str(i) for i in track_ids))
        updated = 0
        changed: list[str] = []
        with self._lock:
            try:
                for i in range(0, len(unique), self._BULK_CHUNK):
                    chunk = unique[i : i + self._BULK_CHUNK]
                    marks = ", ".join("?" for _ in chunk)
                    changed += [
                        r[0] for r in self.conn.execute(
                            f"SELECT id FROM library_tracks WHERE monitored <> ? AND id IN ({marks})", [val, *chunk]
                        ).fetchall()
                    ]
                    cur = self.conn.execute(
                        f"UPDATE library_tracks SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE id IN ({marks})",
                        [val, *chunk],
                    )
                    updated += max(int(cur.rowcount), 0)
                    self._cancel_pending_profile_recompute_for_tracks(chunk)
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("bulk_set_tracks_monitored failed; transaction rolled back")
                raise
            self._safe_item_events_bulk(
                [{"event": "monitored" if val else "unmonitored", "track_id": track_id} for track_id in changed]
            )
        return updated

    def _file_link_is_new(self, file_id: str, track_id: str, file_path: str = "") -> bool:
        """True when this file path is being linked to a track for the first time (caller holds the lock).

        Only a path that has never been linked to ANY track counts. A rescan (same track) and a re-link of a known
        path or id to a different track (dedupe merge, re-resolve) are not new, so they never re-monitor anything
        or override a track the user unmonitored.
        """
        if not track_id:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM library_files WHERE (id = ? OR (? <> '' AND file_path = ?)) "
            "AND COALESCE(track_id, '') <> '' LIMIT 1",
            (file_id, file_path, file_path),
        ).fetchone()
        return row is None

    def _monitor_track_for_new_file(self, track_id: str) -> None:
        """A newly linked file monitors its track and album when the artist's option is ``existing``.

        Caller holds the lock and commits. Other options and unmonitored artists are left alone.
        """
        owner = (
            "EXISTS (SELECT 1 FROM library_artists ar WHERE ar.id = {col} "
            "AND ar.monitor_option = 'existing' AND ar.monitored = 1)"
        )
        self.conn.execute(
            "UPDATE library_albums SET monitored = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE monitored = 0 AND id = (SELECT album_id FROM library_tracks WHERE id = ?) AND "
            + owner.format(col="library_albums.artist_id"),
            (track_id,),
        )
        self.conn.execute(
            "UPDATE library_tracks SET monitored = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE monitored = 0 AND id = ? AND " + owner.format(col="library_tracks.artist_id"),
            (track_id,),
        )

    def upsert_library_file(
        self, file_data: Union[LibraryFile, dict[str, Any]]
    ) -> dict[str, Any]:
        """Creates or updates a native library file."""
        d = file_data.to_dict() if hasattr(file_data, "to_dict") else dict(file_data)
        file_path = str(d.get("file_path") or "")

        with self._lock:
            if not d.get("id"):
                cur = self.conn.execute(
                    "SELECT id FROM library_files WHERE file_path = ?", (file_path,)
                )
                row = cur.fetchone()
                file_id = str(row[0]) if row else str(uuid.uuid4())
            else:
                file_id = str(d["id"])

            track_id = str(d.get("track_id") or "")
            newly_linked = self._file_link_is_new(file_id, track_id, file_path)
            relative_path = str(d.get("relative_path") or "")
            codec = str(d.get("codec") or "")
            bitrate = int(d["bitrate"]) if d.get("bitrate") is not None else None
            sample_rate = int(d["sample_rate"]) if d.get("sample_rate") is not None else None
            bits_per_sample = int(d["bits_per_sample"]) if d.get("bits_per_sample") is not None else None
            quality_name = str(d.get("quality_name") or "Unknown")
            size_bytes = int(d.get("size_bytes", 0))
            cutoff_met = 1 if d.get("cutoff_met", True) else 0
            date_added = d.get("date_added")

            self.conn.execute(
                """
                INSERT INTO library_files (
                    id, track_id, file_path, relative_path, codec, bitrate,
                    sample_rate, bits_per_sample, quality_name, size_bytes,
                    cutoff_met, date_added, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    track_id = excluded.track_id,
                    file_path = excluded.file_path,
                    relative_path = excluded.relative_path,
                    codec = excluded.codec,
                    bitrate = excluded.bitrate,
                    sample_rate = excluded.sample_rate,
                    bits_per_sample = excluded.bits_per_sample,
                    quality_name = excluded.quality_name,
                    size_bytes = excluded.size_bytes,
                    cutoff_met = excluded.cutoff_met,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    file_id,
                    track_id,
                    file_path,
                    relative_path,
                    codec,
                    bitrate,
                    sample_rate,
                    bits_per_sample,
                    quality_name,
                    size_bytes,
                    cutoff_met,
                    date_added,
                ),
            )
            if newly_linked:
                self._monitor_track_for_new_file(track_id)
            self.conn.commit()

        fl = self.get_library_file(file_id)
        if fl is None:
            raise RuntimeError(f"Failed to upsert library file {file_id}")
        return fl

    def upsert_library_files_batch(
        self, files: list[Union[LibraryFile, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Batched upsert for library files within a single transaction commit."""
        if not files:
            return []
        with self._lock:
            for file_data in files:
                d = file_data.to_dict() if hasattr(file_data, "to_dict") else dict(file_data)
                file_path = str(d.get("file_path") or "")

                if not d.get("id"):
                    cur = self.conn.execute(
                        "SELECT id FROM library_files WHERE file_path = ?", (file_path,)
                    )
                    row = cur.fetchone()
                    file_id = str(row[0]) if row else str(uuid.uuid4())
                else:
                    file_id = str(d["id"])

                track_id = str(d.get("track_id") or "")
                newly_linked = self._file_link_is_new(file_id, track_id, file_path)
                relative_path = str(d.get("relative_path") or "")
                codec = str(d.get("codec") or "")
                bitrate = int(d["bitrate"]) if d.get("bitrate") is not None else None
                sample_rate = int(d["sample_rate"]) if d.get("sample_rate") is not None else None
                bits_per_sample = int(d["bits_per_sample"]) if d.get("bits_per_sample") is not None else None
                quality_name = str(d.get("quality_name") or "Unknown")
                size_bytes = int(d.get("size_bytes", 0))
                cutoff_met = 1 if d.get("cutoff_met", True) else 0
                date_added = d.get("date_added")

                self.conn.execute(
                    """
                    INSERT INTO library_files (
                        id, track_id, file_path, relative_path, codec, bitrate,
                        sample_rate, bits_per_sample, quality_name, size_bytes,
                        cutoff_met, date_added, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        track_id = excluded.track_id,
                        file_path = excluded.file_path,
                        relative_path = excluded.relative_path,
                        codec = excluded.codec,
                        bitrate = excluded.bitrate,
                        sample_rate = excluded.sample_rate,
                        bits_per_sample = excluded.bits_per_sample,
                        quality_name = excluded.quality_name,
                        size_bytes = excluded.size_bytes,
                        cutoff_met = excluded.cutoff_met,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        file_id,
                        track_id,
                        file_path,
                        relative_path,
                        codec,
                        bitrate,
                        sample_rate,
                        bits_per_sample,
                        quality_name,
                        size_bytes,
                        cutoff_met,
                        date_added,
                    ),
                )
                if newly_linked:
                    self._monitor_track_for_new_file(track_id)
            self.conn.commit()
            return [f.to_dict() if hasattr(f, "to_dict") else dict(f) for f in files]

    def get_library_file(self, file_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a library file by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_files WHERE id = ?",
                (str(file_id),),
            )
            row = cur.fetchone()
            return self._map_library_file(row) if row else None

    def get_library_file_by_path(self, file_path: str) -> Optional[dict[str, Any]]:
        """Retrieves a library file by full file path."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_files WHERE file_path = ? LIMIT 1",
                (str(file_path),),
            )
            row = cur.fetchone()
            return self._map_library_file(row) if row else None

    def get_library_file_for_track(self, track_id: str) -> Optional[dict[str, Any]]:
        """Retrieves the file associated with a specific track ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_files WHERE track_id = ? LIMIT 1",
                (str(track_id),),
            )
            row = cur.fetchone()
            return self._map_library_file(row) if row else None

    def list_library_files_for_track(self, track_id: str) -> list[dict[str, Any]]:
        """Every file row attached to a track (a track normally has one; replacements briefly have two)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM library_files WHERE track_id = ?", (str(track_id),)
            ).fetchall()
            return [self._map_library_file(r) for r in rows]

    def list_library_artist_track_index(self, artist_id: str) -> list[dict[str, Any]]:
        """One artist's tracks for import matching: id, clean_title, album clean title, duration, file presence."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT t.id, t.clean_title, t.track_number, t.duration_seconds,
                       COALESCE(a.clean_title, '') AS album_clean_title,
                       EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) AS has_file
                FROM library_tracks t
                LEFT JOIN library_albums a ON a.id = t.album_id
                WHERE t.artist_id = ?
                """,
                (str(artist_id),),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_library_file_paths(self, limit: int = 500000) -> list[tuple[str, str]]:
        """(absolute path, track id) of library files (bounded), for import path matching and mapping suggestions."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT file_path, track_id FROM library_files LIMIT ?", (int(limit),)
            ).fetchall()
            return [(str(r["file_path"]), str(r["track_id"])) for r in rows]

    def delete_library_file(self, file_id: str) -> bool:
        """Deletes a library file record."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM library_files WHERE id = ?",
                (str(file_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def relink_library_file(self, file_id: str, track_id: str) -> bool:
        """Points a library file row at another track. True when the row existed and was updated."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE library_files SET track_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(track_id), str(file_id)),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def list_library_files(
        self, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Lists library files ordered by date added with pagination."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_files ORDER BY date_added DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            )
            return [self._map_library_file(row) for row in cur.fetchall()]

    def get_library_stats(self) -> dict[str, Any]:
        """Calculates aggregate statistics for the native library catalog."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM library_artists")
            artist_count = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(*) FROM library_albums")
            album_count = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(*) FROM library_tracks")
            total_track_count = int(cur.fetchone()[0] or 0)

            # Lidarr semantics: ``track_count`` covers tracks on monitored albums or that already have a file
            # (unmonitored, file-less discography stays stored, as ``total_track_count``).
            cur.execute(
                "SELECT COUNT(*) FROM library_tracks t JOIN library_albums al ON al.id = t.album_id "
                "WHERE al.monitored = 1 OR EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id)"
            )
            track_count = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM library_files")
            row = cur.fetchone()
            file_count = int(row[0] or 0) if row else 0
            total_size_bytes = int(row[1] or 0) if row else 0

            cur.execute("SELECT COUNT(*) FROM library_artists WHERE monitored = 1")
            monitored_artist_count = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(*) FROM library_tracks WHERE monitored = 1")
            monitored_track_count = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(DISTINCT track_id) FROM library_files WHERE cutoff_met = 0")
            cutoff_unmet_track_count = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT COUNT(DISTINCT track_id) FROM library_files")
            track_file_count = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT COUNT(*) FROM library_tracks t JOIN library_albums al ON al.id = t.album_id "
                "JOIN library_artists ar ON ar.id = t.artist_id "
                "WHERE t.monitored = 1 AND al.monitored = 1 AND ar.monitored = 1 "
                "AND NOT EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id)"
            )
            missing_track_count = int(cur.fetchone()[0] or 0)

            return {
                "source": "native",
                "unmonitored_artist_count": artist_count - monitored_artist_count,
                # Native artists carry no continuing/ended status.
                "continuing_artist_count": None,
                "ended_artist_count": None,
                "total_track_count": total_track_count,
                "track_file_count": track_file_count,
                "missing_track_count": missing_track_count,
                "artist_count": artist_count,
                "album_count": album_count,
                "track_count": track_count,
                "file_count": file_count,
                "total_size_bytes": total_size_bytes,
                "monitored_artist_count": monitored_artist_count,
                "monitored_track_count": monitored_track_count,
                "cutoff_unmet_track_count": cutoff_unmet_track_count,
            }

    # -------------------------------------------------------------------------
    # Library Collections CRUD
    # -------------------------------------------------------------------------

    def _map_library_collection(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["monitored"] = bool(res.get("monitored", 1))
        if "album_count" in res and res["album_count"] is not None:
            res["album_count"] = int(res["album_count"])
        else:
            res["album_count"] = 0
        if "preview_covers" not in res:
            res["preview_covers"] = []
        return res

    def upsert_library_collection(
        self, collection_data: Union[LibraryCollection, dict[str, Any]]
    ) -> dict[str, Any]:
        """Creates or updates a native library collection."""
        d = collection_data.to_dict() if hasattr(collection_data, "to_dict") else dict(collection_data)
        col_id = str(d.get("id") or uuid.uuid4())
        name = str(d.get("name") or "")
        clean_name = clean_library_name(d.get("clean_name") or name)
        summary = str(d["summary"]) if d.get("summary") is not None else None
        poster_url = str(d["poster_url"]) if d.get("poster_url") is not None else None
        monitored = 1 if d.get("monitored", True) else 0
        foreign_id = str(d["foreign_id"]) if d.get("foreign_id") is not None else None
        created_at = d.get("created_at")

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO library_collections (
                    id, name, clean_name, summary, poster_url, monitored, foreign_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    clean_name = excluded.clean_name,
                    summary = COALESCE(excluded.summary, library_collections.summary),
                    poster_url = COALESCE(excluded.poster_url, library_collections.poster_url),
                    monitored = excluded.monitored,
                    foreign_id = COALESCE(excluded.foreign_id, library_collections.foreign_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (col_id, name, clean_name, summary, poster_url, monitored, foreign_id, created_at),
            )
            self.conn.commit()

        col = self.get_library_collection(col_id)
        if col is None:
            raise RuntimeError(f"Failed to upsert library collection {col_id}")
        return col

    def get_library_collection(self, collection_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single library collection by ID with album count and preview covers."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT c.*, (
                    SELECT COUNT(*) FROM library_collection_albums ca WHERE ca.collection_id = c.id
                ) AS album_count
                FROM library_collections c
                WHERE c.id = ?
                """,
                (str(collection_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            col = self._map_library_collection(row)
            p_cur = self.conn.execute(
                """
                SELECT a.cover_url
                FROM library_albums a
                JOIN library_collection_albums ca ON a.id = ca.album_id
                WHERE ca.collection_id = ? AND a.cover_url IS NOT NULL AND a.cover_url != ''
                ORDER BY ca.order_index ASC
                LIMIT 4
                """,
                (str(collection_id),),
            )
            col["preview_covers"] = [r[0] for r in p_cur.fetchall() if r[0]]
            return col

    def list_library_collections(
        self, limit: int = 100, offset: int = 0, query: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Lists library collections with optional search query, album counts, preview covers, and pagination."""
        sql = """
            SELECT c.*, (
                SELECT COUNT(*) FROM library_collection_albums ca WHERE ca.collection_id = c.id
            ) AS album_count
            FROM library_collections c
            WHERE 1=1
        """
        params: list[Any] = []
        if query:
            clean_q = clean_library_name(query)
            sql += " AND (c.clean_name LIKE ? OR c.name LIKE ?)"
            params.extend([f"%{clean_q}%", f"%{query}%"])
        sql += " ORDER BY c.name COLLATE NOCASE ASC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

        with self._lock:
            cur = self.conn.execute(sql, params)
            collections = [self._map_library_collection(row) for row in cur.fetchall()]
            for col in collections:
                col_id = col["id"]
                p_cur = self.conn.execute(
                    """
                    SELECT a.cover_url
                    FROM library_albums a
                    JOIN library_collection_albums ca ON a.id = ca.album_id
                    WHERE ca.collection_id = ? AND a.cover_url IS NOT NULL AND a.cover_url != ''
                    ORDER BY ca.order_index ASC
                    LIMIT 4
                    """,
                    (col_id,),
                )
                col["preview_covers"] = [r[0] for r in p_cur.fetchall() if r[0]]
            return collections

    def delete_library_collection(self, collection_id: str) -> bool:
        """Deletes a library collection and cascades to collection albums."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM library_collections WHERE id = ?",
                (str(collection_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def add_album_to_collection(
        self, collection_id: str, album_id: str, order_index: int = 0
    ) -> bool:
        """Associates an album with a collection."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO library_collection_albums (collection_id, album_id, order_index)
                VALUES (?, ?, ?)
                ON CONFLICT(collection_id, album_id) DO UPDATE SET
                    order_index = excluded.order_index
                """,
                (str(collection_id), str(album_id), int(order_index)),
            )
            self.conn.commit()
            return True

    def remove_album_from_collection(self, collection_id: str, album_id: str) -> bool:
        """Removes an album association from a collection."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM library_collection_albums WHERE collection_id = ? AND album_id = ?",
                (str(collection_id), str(album_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_collection_albums(self, collection_id: str) -> list[dict[str, Any]]:
        """Retrieves all albums associated with a collection, ordered by order_index, year, title."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT a.*, ca.order_index
                FROM library_collection_albums ca
                JOIN library_albums a ON a.id = ca.album_id
                WHERE ca.collection_id = ?
                ORDER BY ca.order_index ASC, a.year DESC, a.title COLLATE NOCASE ASC
                """,
                (str(collection_id),),
            )
            rows = cur.fetchall()
            results = []
            for r in rows:
                d = self._map_library_album(r)
                d["order_index"] = int(r["order_index"])
                results.append(d)
            return results

