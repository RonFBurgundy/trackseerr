"""Native library artist CRUD, artist links, metadata profiles, and bulk artist edits.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Union

from trackseerr.library_monitoring import (
    ALBUM_MONITORED_SQL,
    DEFAULT_MONITOR_OPTION,
    TRACK_HAS_FILE_SQL,
    album_in_metadata_profile,
    validate_monitor_option,
    validate_release_types,
)
from trackseerr.list_index import fold_search_text, library_sort_key
from trackseerr.models import (
    LibraryArtist,
)
from trackseerr.storage_common import (
    clean_library_name,
)

logger = logging.getLogger(__name__)


class LibraryArtistStoreMixin:
    # -------------------------------------------------------------------------
    # Native Library CRUD
    # -------------------------------------------------------------------------

    def _map_library_artist(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res.pop("search_text", None)
        res.pop("search_clean", None)
        res["monitored"] = bool(res.get("monitored", 1))
        res["monitor_option"] = str(res.get("monitor_option") or "all")
        return res

    @staticmethod
    def _decode_type_list(raw: Any) -> Optional[list[str]]:
        """Decodes a JSON list column; NULL or unparseable data is None (unknown)."""
        if raw is None:
            return None
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            logger.warning("Ignoring malformed type list in database: %r", raw)
            return None
        return [str(v) for v in val] if isinstance(val, list) else None

    def _map_library_album(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res.pop("search_text", None)
        res.pop("search_clean", None)
        res["monitored"] = bool(res.get("monitored", 1))
        if res.get("year") is not None:
            res["year"] = int(res["year"])
        if res.get("total_tracks") is not None:
            res["total_tracks"] = int(res["total_tracks"])
        res["secondary_types"] = self._decode_type_list(res.get("secondary_types"))
        return res

    def _map_library_track(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res.pop("search_text", None)
        res.pop("search_clean", None)
        res["monitored"] = bool(res.get("monitored", 1))
        res["track_number"] = int(res.get("track_number", 1))
        res["disc_number"] = int(res.get("disc_number", 1))
        if res.get("duration_seconds") is not None:
            res["duration_seconds"] = float(res["duration_seconds"])
        return res

    def _map_library_file(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["cutoff_met"] = bool(res.get("cutoff_met", 1))
        res["size_bytes"] = int(res.get("size_bytes", 0))
        if res.get("bitrate") is not None:
            res["bitrate"] = int(res["bitrate"])
        if res.get("sample_rate") is not None:
            res["sample_rate"] = int(res["sample_rate"])
        if res.get("bits_per_sample") is not None:
            res["bits_per_sample"] = int(res["bits_per_sample"])
        return res

    def upsert_library_artist(
        self, artist_data: Union[LibraryArtist, dict[str, Any]], preserve_monitoring: bool = False
    ) -> dict[str, Any]:
        """Creates or updates a native library artist.

        With ``preserve_monitoring`` an existing row keeps its ``monitored`` / ``monitor_option`` (user choices).
        """
        d = artist_data.to_dict() if hasattr(artist_data, "to_dict") else dict(artist_data)
        artist_id = str(d.get("id") or uuid.uuid4())
        name = str(d.get("name") or "")
        clean_name = clean_library_name(d.get("clean_name") or name)
        sort_name = library_sort_key(name)
        foreign_artist_id = str(d["foreign_artist_id"]) if d.get("foreign_artist_id") is not None else None
        path = str(d["path"]) if d.get("path") is not None else None
        monitored = 1 if d.get("monitored", True) else 0
        monitor_option = str(d.get("monitor_option") or DEFAULT_MONITOR_OPTION)
        quality_profile_id = str(d["quality_profile_id"]) if d.get("quality_profile_id") is not None else None
        metadata_profile_id = int(d["metadata_profile_id"]) if d.get("metadata_profile_id") is not None else None
        metadata_json = d.get("metadata_json")
        if isinstance(metadata_json, dict):
            metadata_json = json.dumps(metadata_json)
        elif metadata_json is not None:
            metadata_json = str(metadata_json)
        mbid = str(d["mbid"]) if d.get("mbid") is not None else None
        image_url = str(d["image_url"]) if d.get("image_url") is not None else None
        banner_url = str(d["banner_url"]) if d.get("banner_url") is not None else None
        bio = str(d["bio"]) if d.get("bio") is not None else None
        genres = str(d["genres"]) if d.get("genres") is not None else None
        country = str(d["country"]) if d.get("country") is not None else None
        created_at = d.get("created_at")

        with self._lock:
            is_new = self.conn.execute("SELECT 1 FROM library_artists WHERE id = ?", (artist_id,)).fetchone() is None
            self.conn.execute(
                """
                INSERT INTO library_artists (
                    id, name, clean_name, sort_name, search_text, search_clean, foreign_artist_id, path, monitored,
                    monitor_option, quality_profile_id, metadata_json, mbid,
                    image_url, banner_url, bio, genres, country, metadata_profile_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    clean_name = excluded.clean_name,
                    sort_name = excluded.sort_name,
                    search_text = excluded.search_text,
                    search_clean = excluded.search_clean,
                    foreign_artist_id = COALESCE(excluded.foreign_artist_id, library_artists.foreign_artist_id),
                    path = COALESCE(excluded.path, library_artists.path),
                    monitored = CASE WHEN ? THEN library_artists.monitored ELSE excluded.monitored END,
                    monitor_option = CASE WHEN ? THEN library_artists.monitor_option ELSE excluded.monitor_option END,
                    quality_profile_id = COALESCE(excluded.quality_profile_id, library_artists.quality_profile_id),
                    metadata_json = COALESCE(excluded.metadata_json, library_artists.metadata_json),
                    mbid = COALESCE(excluded.mbid, library_artists.mbid),
                    image_url = COALESCE(excluded.image_url, library_artists.image_url),
                    banner_url = COALESCE(excluded.banner_url, library_artists.banner_url),
                    bio = COALESCE(excluded.bio, library_artists.bio),
                    genres = COALESCE(excluded.genres, library_artists.genres),
                    country = COALESCE(excluded.country, library_artists.country),
                    metadata_profile_id = COALESCE(excluded.metadata_profile_id, library_artists.metadata_profile_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    artist_id,
                    name,
                    clean_name,
                    sort_name,
                    fold_search_text(name),
                    fold_search_text(clean_name),
                    foreign_artist_id,
                    path,
                    monitored,
                    monitor_option,
                    quality_profile_id,
                    metadata_json,
                    mbid,
                    image_url,
                    banner_url,
                    bio,
                    genres,
                    country,
                    metadata_profile_id,
                    created_at,
                    1 if preserve_monitoring else 0,
                    1 if preserve_monitoring else 0,
                ),
            )
            self.conn.commit()
            if is_new:
                self._safe_item_event(
                    "added_to_library", artist_id=artist_id, artist_name=name, message=f"Added artist '{name}'"
                )

        artist = self.get_library_artist(artist_id)
        if artist is None:
            raise RuntimeError(f"Failed to upsert library artist {artist_id}")
        return artist

    # -------------------------------------------------------------------------
    # Artist links (discovery <-> library identity cache)
    # -------------------------------------------------------------------------

    def get_artist_link(
        self, library_artist_id: Optional[str] = None, discovery_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """The cached link row keyed by library artist id or discovery id (library id wins); None when absent."""
        with self._lock:
            row = None
            if library_artist_id:
                row = self.conn.execute(
                    "SELECT * FROM artist_links WHERE library_artist_id = ?", (str(library_artist_id),)
                ).fetchone()
            if row is None and discovery_id:
                row = self.conn.execute(
                    "SELECT * FROM artist_links WHERE discovery_id = ?", (str(discovery_id),)
                ).fetchone()
            return dict(row) if row else None

    def save_artist_link(
        self,
        library_artist_id: Optional[str],
        discovery_id: Optional[str],
        mbid: Optional[str],
        confidence: str,
        name: Optional[str] = None,
    ) -> None:
        """Replaces any rows holding either key, then stores the link stamped with the current time (UTC)."""
        if not library_artist_id and not discovery_id:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if library_artist_id:
                self.conn.execute("DELETE FROM artist_links WHERE library_artist_id = ?", (str(library_artist_id),))
            if discovery_id:
                self.conn.execute("DELETE FROM artist_links WHERE discovery_id = ?", (str(discovery_id),))
            self.conn.execute(
                "INSERT INTO artist_links (library_artist_id, discovery_id, mbid, name, confidence, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(library_artist_id) if library_artist_id else None,
                    str(discovery_id) if discovery_id else None,
                    mbid,
                    name,
                    confidence,
                    stamp,
                ),
            )
            self.conn.commit()

    def get_discovery_ids_for_library_artists(self, library_artist_ids: list[str]) -> dict[str, str]:
        """``{library_artist_id: discovery_id}`` for cached positive links only (no freshness check, no network)."""
        ids = [str(i) for i in library_artist_ids if i]
        out: dict[str, str] = {}
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"SELECT library_artist_id, discovery_id FROM artist_links "
                    f"WHERE library_artist_id IN ({marks}) AND discovery_id IS NOT NULL AND confidence != 'none'",
                    chunk,
                ).fetchall()
                out.update({r[0]: r[1] for r in rows})
        return out

    def get_library_artist(self, artist_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single library artist by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_artists WHERE id = ?",
                (str(artist_id),),
            )
            row = cur.fetchone()
            return self._map_library_artist(row) if row else None

    _ART_TABLES = {"artist": "library_artists", "album": "library_albums"}

    def set_library_art_version(self, kind: str, item_id: str, version: Optional[str]) -> bool:
        """Records the art version token of an artist or album (``kind`` is "artist" or "album"). True if a row changed.

        Deliberately does not touch ``updated_at``: art bookkeeping is not a user-visible edit.
        """
        table = self._ART_TABLES[kind]
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE {table} SET art_version = ? WHERE id = ? AND COALESCE(art_version, '') != COALESCE(?, '')",
                (version, str(item_id), version),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_library_art_rows(self, kind: str, after_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """Keyset page (by id) of ``id``, ``path``, ``art_version`` and ``url`` (the row's ``image_url`` for artists,
        ``cover_url`` for albums, aliased to ``url``) for the art backfill and art resolver."""
        table = self._ART_TABLES[kind]
        url_col = "image_url" if kind == "artist" else "cover_url"
        with self._lock:
            cur = self.conn.execute(
                f"SELECT id, path, {url_col} AS url, art_version FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
                (str(after_id), int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_library_artist_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Retrieves an artist by exact name or cleaned normalized name."""
        clean = clean_library_name(name)
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_artists WHERE clean_name = ? OR LOWER(name) = LOWER(?) LIMIT 1",
                (clean, str(name).strip()),
            )
            row = cur.fetchone()
            return self._map_library_artist(row) if row else None

    def get_library_artist_by_foreign_id(
        self, foreign_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieves a library artist by foreign_artist_id."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM library_artists WHERE foreign_artist_id = ? LIMIT 1",
                (str(foreign_id),),
            )
            row = cur.fetchone()
            return self._map_library_artist(row) if row else None

    def get_library_artist_by_mbid(self, mbid: str) -> Optional[dict[str, Any]]:
        """Retrieves a library artist by MusicBrainz artist id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM library_artists WHERE mbid = ? LIMIT 1", (str(mbid),)
            ).fetchone()
            return self._map_library_artist(row) if row else None

    def monitor_library_album_and_tracks(self, album_id: str) -> tuple[bool, int]:
        """Sets an album and all its tracks monitored (never unmonitors). Returns (album changed, tracks changed)."""
        with self._lock:
            album_cur = self.conn.execute(
                "UPDATE library_albums SET monitored = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND monitored = 0",
                (str(album_id),),
            )
            track_cur = self.conn.execute(
                "UPDATE library_tracks SET monitored = 1, updated_at = CURRENT_TIMESTAMP WHERE album_id = ? AND monitored = 0",
                (str(album_id),),
            )
            self.conn.commit()
            if album_cur.rowcount > 0:
                self._safe_item_event("monitored", album_id=str(album_id), message="Album and its tracks monitored")
            return album_cur.rowcount > 0, track_cur.rowcount

    def list_library_artists(
        self,
        monitored_only: bool = False,
        query: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Lists library artists with optional filtering, search query, and pagination."""
        sql = "SELECT * FROM library_artists WHERE 1=1"
        params: list[Any] = []
        if monitored_only:
            sql += " AND monitored = 1"
        if query:
            clean_q = clean_library_name(query)
            sql += " AND (clean_name LIKE ? OR name LIKE ?)"
            params.extend([f"%{clean_q}%", f"%{query}%"])
        sql += " ORDER BY name COLLATE NOCASE ASC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

        with self._lock:
            cur = self.conn.execute(sql, params)
            return [self._map_library_artist(row) for row in cur.fetchall()]

    def delete_library_artist(self, artist_id: str) -> bool:
        """Deletes a library artist and cascades to child albums, tracks, and files (history is kept)."""
        with self._lock:
            self._emit_removed("artist", str(artist_id))
            cur = self.conn.execute(
                "DELETE FROM library_artists WHERE id = ?",
                (str(artist_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_artist_monitored(
        self, artist_id: str, monitored: bool, cascade_children: bool = True
    ) -> bool:
        """Sets monitoring status for an artist and optionally cascades to child albums and tracks."""
        val = 1 if monitored else 0
        with self._lock:
            before = self.conn.execute("SELECT monitored FROM library_artists WHERE id = ?", (str(artist_id),)).fetchone()
            cur = self.conn.execute(
                "UPDATE library_artists SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (val, str(artist_id)),
            )
            if cur.rowcount == 0:
                return False
            if cascade_children:
                self.conn.execute(
                    "UPDATE library_albums SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE artist_id = ?",
                    (val, str(artist_id)),
                )
                self.conn.execute(
                    "UPDATE library_tracks SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE artist_id = ?",
                    (val, str(artist_id)),
                )
            self.conn.commit()
            if before is not None and int(before["monitored"]) != val:
                self._safe_item_event(
                    "monitored" if val else "unmonitored", artist_id=str(artist_id),
                    details={"cascade_children": bool(cascade_children)},
                )
            return True

    # ---- native metadata profiles (optional; shape automatic monitoring only) ----

    def _map_metadata_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["primary_types"] = self._decode_type_list(res.get("primary_types")) or []
        res["secondary_types"] = self._decode_type_list(res.get("secondary_types")) or []
        return res

    def list_metadata_profiles(self) -> list[dict[str, Any]]:
        """All metadata profiles (oldest first) with ``artist_count``, how many artists use each."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM library_artists ar WHERE ar.metadata_profile_id = p.id) "
                "AS artist_count FROM native_metadata_profiles p ORDER BY p.id"
            ).fetchall()
        return [{**self._map_metadata_profile(r), "artist_count": int(r["artist_count"])} for r in rows]

    def get_metadata_profile(self, profile_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM native_metadata_profiles WHERE id = ?", (int(profile_id),)).fetchone()
        return self._map_metadata_profile(row) if row else None

    def create_metadata_profile(self, name: str, primary_types: Any, secondary_types: Any) -> dict[str, Any]:
        """Creates a profile; ValueError for a bad name/type list or a duplicate name."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name must not be empty")
        primary, secondary = validate_release_types(primary_types, secondary_types)
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO native_metadata_profiles (name, primary_types, secondary_types) VALUES (?, ?, ?)",
                    (clean_name, json.dumps(primary), json.dumps(secondary)),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise ValueError(f"A metadata profile named {clean_name!r} already exists") from exc
            new_id = int(cur.lastrowid or 0)
        created = self.get_metadata_profile(new_id)
        if created is None:
            raise RuntimeError("Failed to create metadata profile")
        return created

    def update_metadata_profile(
        self, profile_id: int, name: str, primary_types: Any, secondary_types: Any
    ) -> Optional[dict[str, Any]]:
        """Replaces a profile's fields; None when it does not exist, ValueError on bad input or duplicate name."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name must not be empty")
        primary, secondary = validate_release_types(primary_types, secondary_types)
        with self._lock:
            try:
                cur = self.conn.execute(
                    "UPDATE native_metadata_profiles SET name = ?, primary_types = ?, secondary_types = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (clean_name, json.dumps(primary), json.dumps(secondary), int(profile_id)),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise ValueError(f"A metadata profile named {clean_name!r} already exists") from exc
            if cur.rowcount <= 0:
                return None
        return self.get_metadata_profile(profile_id)

    def delete_metadata_profile(self, profile_id: int) -> Optional[int]:
        """Deletes a profile, clearing it from artists and the add default; returns artists cleared (None = missing)."""
        with self._lock:
            try:
                if self.conn.execute(
                    "SELECT 1 FROM native_metadata_profiles WHERE id = ?", (int(profile_id),)
                ).fetchone() is None:
                    return None
                cleared = self.conn.execute(
                    "UPDATE library_artists SET metadata_profile_id = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE metadata_profile_id = ?",
                    (int(profile_id),),
                ).rowcount
                self.conn.execute(
                    "UPDATE media_management_settings SET add_metadata_profile_id = NULL WHERE add_metadata_profile_id = ?",
                    (int(profile_id),),
                )
                self.conn.execute("DELETE FROM native_metadata_profiles WHERE id = ?", (int(profile_id),))
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("delete_metadata_profile failed; transaction rolled back")
                raise
        return max(int(cleared), 0)

    def metadata_profile_preview(
        self, artist_id: str, profile_id: Optional[int]
    ) -> Optional[dict[str, Any]]:
        """What assigning ``profile_id`` (None = clear) to an artist would do; None if the artist/profile is missing.

        Returns ``matching`` / ``total`` release-group counts under the profile plus ``would_change``: how many albums
        and tracks a recompute (``apply_monitor_to_albums``) would newly monitor / unmonitor against their CURRENT
        flags. It evaluates the same SQL predicate as ``bulk_edit_library_artists`` with the artist's metadata profile
        swapped for the candidate, and writes nothing.
        """
        profile = self.get_metadata_profile(int(profile_id)) if profile_id is not None else None
        if profile_id is not None and profile is None:
            return None
        if self.get_library_artist(artist_id) is None:
            return None
        album_expr = ALBUM_MONITORED_SQL.format(opt="ar.monitor_option", art_mon="ar.monitored")
        # ``ar`` is the artist row with its metadata profile replaced, so the real predicate is reused unchanged.
        ar_sub = (
            "(SELECT id, monitor_option, monitored, created_at, ? AS metadata_profile_id "
            "FROM library_artists WHERE id = ?) ar"
        )
        ar_params = [int(profile_id) if profile_id is not None else None, str(artist_id)]
        with self._lock:
            rows = self.conn.execute(
                "SELECT album_type, secondary_types FROM library_albums WHERE artist_id = ?", (str(artist_id),)
            ).fetchall()
            alb = self.conn.execute(
                "SELECT COALESCE(SUM(a.monitored = 0 AND n = 1), 0), COALESCE(SUM(a.monitored = 1 AND n = 0), 0) FROM ("
                f"SELECT a.monitored AS monitored, ({album_expr}) AS n FROM library_albums a "
                f"JOIN {ar_sub} ON ar.id = a.artist_id WHERE a.artist_id = ?) a",
                [*ar_params, str(artist_id)],
            ).fetchone()
            trk = self.conn.execute(
                "SELECT COALESCE(SUM(m = 0 AND n = 1), 0), COALESCE(SUM(m = 1 AND n = 0), 0) FROM ("
                "SELECT library_tracks.monitored AS m, CASE WHEN ar.monitor_option = 'existing' "
                f"THEN ({album_expr}) AND {TRACK_HAS_FILE_SQL} ELSE ({album_expr}) END AS n "
                "FROM library_tracks JOIN library_albums a ON a.id = library_tracks.album_id "
                f"JOIN {ar_sub} ON ar.id = a.artist_id WHERE a.artist_id = ?)",
                [*ar_params, str(artist_id)],
            ).fetchone()
        matching = sum(
            1
            for r in rows
            if album_in_metadata_profile(profile, r["album_type"], self._decode_type_list(r["secondary_types"]))
        )
        return {
            "matching": matching,
            "total": len(rows),
            "would_change": {
                "albums_to_monitor": int(alb[0]),
                "albums_to_unmonitor": int(alb[1]),
                "tracks_to_monitor": int(trk[0]),
                "tracks_to_unmonitor": int(trk[1]),
            },
        }

    def finish_pending_profile_recompute(self, artist_id: str) -> bool:
        """Runs a deferred metadata-profile recompute once secondary types are known; True when it ran.

        The recompute and the ``pending_profile_recompute`` clear commit together in one transaction, and the check
        happens under the same lock hold, so a manual album/track edit (which clears the flag) can never be undone by
        a recompute that read a stale flag.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT pending_profile_recompute FROM library_artists WHERE id = ?", (str(artist_id),)
            ).fetchone()
            if row is None or not row[0]:
                return False
            known = self.conn.execute(
                "SELECT 1 FROM library_albums WHERE artist_id = ? AND secondary_types IS NOT NULL LIMIT 1",
                (str(artist_id),),
            ).fetchone()
            if known is None:
                return False
            # Clears the flag in the same transaction as the album/track recompute.
            self.bulk_edit_library_artists([str(artist_id)], apply_monitor_to_albums=True)
            return True

    _BULK_CHUNK = 500
    _UNSET: Any = object()

    def bulk_edit_library_artists(  # noqa: C901, PLR0915
        self,
        artist_ids: Optional[list[str]] = None,
        *,
        monitored: Optional[bool] = None,
        monitor_option: Optional[str] = None,
        quality_profile_id: Any = _UNSET,
        apply_monitor_to_albums: bool = False,
        metadata_profile_id: Any = _UNSET,
        recompute_when_option_changes: bool = False,
        add_tag_ids: Optional[list[int]] = None,
        remove_tag_ids: Optional[list[int]] = None,
    ) -> dict[str, int]:
        """Set-based bulk edit of native artists (``artist_ids=None`` means every artist), in one transaction.

        ``monitored`` / ``monitor_option`` / ``quality_profile_id`` (pass None to clear) are written only when
        given. With ``apply_monitor_to_albums`` every album of the affected artists is recomputed from the artist's
        resulting option and monitored flag (see ``library_monitoring.ALBUM_MONITORED_SQL``; ``existing`` keeps
        albums having at least one track with a library file), and each album's tracks follow their album.
        ``metadata_profile_id`` (None clears it) is the artist's optional metadata profile; an album outside it is not
        auto-monitored by the recompute (files still win under ``existing``).
        ``recompute_when_option_changes`` (ignored when ``apply_monitor_to_albums``) recomputes only the artists whose
        ``monitor_option`` differs from the new ``monitor_option`` before this edit; the rest are just written.
        ``add_tag_ids`` / ``remove_tag_ids`` add/remove tags in the same transaction (the result then also carries
        ``tags_added`` / ``tags_removed``; ``UnknownTag`` for a bad id, ``ValueError`` when a tag is in both).
        Returns ``artists_updated`` plus the post-update count of ``albums_monitored`` / ``albums_unmonitored``
        among the affected artists' albums (0/0 when albums were not recomputed).
        """
        tag_add = list(dict.fromkeys(int(i) for i in add_tag_ids or []))
        tag_remove = list(dict.fromkeys(int(i) for i in remove_tag_ids or []))
        if set(tag_add) & set(tag_remove):
            raise ValueError("A tag cannot be both added and removed")
        tag_events: dict[str, tuple[list[str], list[str]]] = {}
        if monitor_option is not None:
            validate_monitor_option(monitor_option)
        sets: list[str] = []
        set_params: list[Any] = []
        if monitored is not None:
            sets.append("monitored = ?")
            set_params.append(1 if monitored else 0)
        if monitor_option is not None:
            sets.append("monitor_option = ?")
            set_params.append(monitor_option)
        if quality_profile_id is not self._UNSET:
            sets.append("quality_profile_id = ?")
            set_params.append(str(quality_profile_id) if quality_profile_id is not None else None)
        if metadata_profile_id is not self._UNSET:
            if metadata_profile_id is not None and self.get_metadata_profile(int(metadata_profile_id)) is None:
                raise ValueError(f"Metadata profile {metadata_profile_id} does not exist")
            sets.append("metadata_profile_id = ?")
            set_params.append(int(metadata_profile_id) if metadata_profile_id is not None else None)
        if not sets and not apply_monitor_to_albums and not tag_add and not tag_remove:
            raise ValueError("No changes requested")
        result_tags = {"tags_added": 0, "tags_removed": 0}

        if artist_ids is None:
            chunks: list[Optional[list[str]]] = [None]
        else:
            unique = list(dict.fromkeys(str(i) for i in artist_ids))
            chunks = [unique[i : i + self._BULK_CHUNK] for i in range(0, len(unique), self._BULK_CHUNK)]

        album_expr = ALBUM_MONITORED_SQL.format(opt="ar.monitor_option", art_mon="ar.monitored")
        result = {"artists_updated": 0, "albums_monitored": 0, "albums_unmonitored": 0}

        def recompute(target: Optional[list[str]]) -> None:
            """Recomputes albums and tracks of ``target`` artists (None = all); caller holds the lock and commits."""
            alb_where, alb_where_a, params = "", "", []
            marks = ""
            if target is not None:
                marks = ", ".join("?" for _ in target)
                alb_where = f" WHERE artist_id IN ({marks})"
                alb_where_a = f" WHERE a.artist_id IN ({marks})"
                params = target
            # The recompute supersedes any deferred one, so it can never run again over this result.
            self.conn.execute(
                "UPDATE library_artists SET pending_profile_recompute = 0 WHERE pending_profile_recompute = 1"
                + (f" AND id IN ({marks})" if target is not None else ""),
                params,
            )
            self.conn.execute(
                "UPDATE library_albums AS a SET monitored = ("
                f"SELECT {album_expr} FROM library_artists ar WHERE ar.id = a.artist_id"
                f"), updated_at = CURRENT_TIMESTAMP{alb_where_a}",
                params,
            )
            # Tracks follow their album, except under ``existing`` where monitoring is track-granular: only
            # tracks that have a file stay monitored (and only inside a monitored album).
            self.conn.execute(
                "UPDATE library_tracks SET monitored = ("
                "SELECT CASE WHEN ar.monitor_option = 'existing' "
                f"THEN al.monitored AND {TRACK_HAS_FILE_SQL} ELSE al.monitored END "
                "FROM library_albums al JOIN library_artists ar ON ar.id = al.artist_id "
                "WHERE al.id = library_tracks.album_id"
                f"), updated_at = CURRENT_TIMESTAMP{alb_where}",
                params,
            )
            counts = self.conn.execute(
                "SELECT COALESCE(SUM(monitored), 0), COUNT(*) FROM library_albums" + alb_where, params
            ).fetchone()
            result["albums_monitored"] += int(counts[0])
            result["albums_unmonitored"] += int(counts[1]) - int(counts[0])

        with self._lock:
            try:
                tag_labels = self._require_tag_ids([*tag_add, *tag_remove]) if (tag_add or tag_remove) else {}
                for chunk in chunks:
                    art_where, params = "", []
                    if chunk is not None:
                        marks = ", ".join("?" for _ in chunk)
                        art_where = f" WHERE id IN ({marks})"
                        params = chunk
                    if tag_add or tag_remove:
                        t_added, t_removed, per_artist = self._apply_artist_tag_changes(
                            chunk, tag_add, tag_remove, tag_labels
                        )
                        result_tags["tags_added"] += t_added
                        result_tags["tags_removed"] += t_removed
                        tag_events.update(per_artist)
                    changing: list[str] = []
                    if recompute_when_option_changes and monitor_option is not None and not apply_monitor_to_albums:
                        # Only artists whose option actually changes are recomputed; ones already on it are left alone.
                        changing = [
                            str(r[0])
                            for r in self.conn.execute(
                                f"SELECT id FROM library_artists{art_where}"
                                f"{' AND' if art_where else ' WHERE'} monitor_option <> ?",
                                [*params, monitor_option],
                            ).fetchall()
                        ]
                    if sets:
                        cur = self.conn.execute(
                            f"UPDATE library_artists SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP{art_where}",
                            [*set_params, *params],
                        )
                    else:
                        cur = self.conn.execute(f"SELECT COUNT(*) FROM library_artists{art_where}", params)
                    updated = cur.rowcount if sets else int(cur.fetchone()[0])
                    result["artists_updated"] += max(int(updated), 0)
                    if apply_monitor_to_albums:
                        recompute(chunk)
                    else:
                        for i in range(0, len(changing), self._BULK_CHUNK):
                            recompute(changing[i : i + self._BULK_CHUNK])
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("bulk_edit_library_artists failed; transaction rolled back")
                raise
        if tag_add or tag_remove:
            result.update(result_tags)
            self._emit_tag_events(tag_events)
        return result

    def bulk_set_albums_monitored(
        self, album_ids: list[str], monitored: bool, cascade_tracks: bool = True
    ) -> int:
        """Sets ``monitored`` on many albums (and their tracks) in one transaction; returns albums updated."""
        val = 1 if monitored else 0
        unique = list(dict.fromkeys(str(i) for i in album_ids))
        updated = 0
        changed: list[str] = []
        with self._lock:
            try:
                for i in range(0, len(unique), self._BULK_CHUNK):
                    chunk = unique[i : i + self._BULK_CHUNK]
                    marks = ", ".join("?" for _ in chunk)
                    changed += [
                        r[0] for r in self.conn.execute(
                            f"SELECT id FROM library_albums WHERE monitored <> ? AND id IN ({marks})", [val, *chunk]
                        ).fetchall()
                    ]
                    cur = self.conn.execute(
                        f"UPDATE library_albums SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE id IN ({marks})",
                        [val, *chunk],
                    )
                    updated += max(int(cur.rowcount), 0)
                    self._cancel_pending_profile_recompute_for_albums(chunk)
                    if cascade_tracks:
                        self.conn.execute(
                            f"UPDATE library_tracks SET monitored = ?, updated_at = CURRENT_TIMESTAMP WHERE album_id IN ({marks})",
                            [val, *chunk],
                        )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                logger.exception("bulk_set_albums_monitored failed; transaction rolled back")
                raise
            self._safe_item_events_bulk(
                [{"event": "monitored" if val else "unmonitored", "album_id": album_id} for album_id in changed]
            )
        return updated

