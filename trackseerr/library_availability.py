"""Sanitized Gateway Library Availability Layer.

Provides fast, non-sensitive availability resolution for web-facing Gateway / frontend discovery.
Strictly avoids leaking internal disk paths or credentials, returning standardized status flags.
"""

import logging
from typing import Any, Optional

from trackseerr.storage import Database, clean_library_name

logger = logging.getLogger(__name__)


def get_item_availability(
    db: Database,
    artist_name: Optional[str] = None,
    album_title: Optional[str] = None,
    track_title: Optional[str] = None,
    foreign_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolves catalog presence and file availability for an artist, album, or track.

    Guarantees sanitized output with no disk paths, absolute file paths, or host credentials.

    Returns:
        dict with keys:
            - in_library: bool
            - monitored: bool
            - status: "available" | "partial" | "cutoff_unmet" | "missing" | "none"
            - quality: Optional[str]
            - file_count: int
            - track_count: int
    """
    artist_clean = artist_name.strip() if artist_name else ""
    album_clean = album_title.strip() if album_title else ""
    track_clean = track_title.strip() if track_title else ""
    fid = str(foreign_id).strip() if foreign_id else None

    # -------------------------------------------------------------------------
    # 1. Resolve Track Availability
    # -------------------------------------------------------------------------
    if track_clean and artist_clean:
        artist_row = db.get_library_artist_by_name(artist_clean)
        if not artist_row and fid:
            with db._lock:
                cur = db.conn.execute(
                    "SELECT * FROM library_artists WHERE foreign_artist_id = ? LIMIT 1",
                    (fid,),
                )
                r = cur.fetchone()
                if r:
                    artist_row = db._map_library_artist(r)

        if artist_row:
            track_row = None
            if album_clean:
                album_row = db.get_library_album_by_title(artist_row["id"], album_clean)
                if album_row:
                    track_row = db.get_library_track_by_title(album_row["id"], track_clean)

            if not track_row:
                clean_t = clean_library_name(track_clean)
                artist_tracks = db.list_library_tracks(artist_id=artist_row["id"], limit=500)
                for t in artist_tracks:
                    if t.get("clean_title") == clean_t or clean_library_name(t.get("title", "")) == clean_t:
                        track_row = t
                        break

            if not track_row and fid:
                with db._lock:
                    cur = db.conn.execute(
                        "SELECT * FROM library_tracks WHERE foreign_track_id = ? AND artist_id = ? LIMIT 1",
                        (fid, str(artist_row["id"])),
                    )
                    r = cur.fetchone()
                    if r:
                        track_row = db._map_library_track(r)

            if track_row:
                file_row = db.get_library_file_for_track(track_row["id"])
                if file_row:
                    cutoff_met = bool(file_row.get("cutoff_met", True))
                    status_str = "available" if cutoff_met else "cutoff_unmet"
                    return {
                        "in_library": True,
                        "monitored": bool(track_row.get("monitored", True)),
                        "status": status_str,
                        "quality": file_row.get("quality_name"),
                        "file_count": 1,
                        "track_count": 1,
                    }
                return {
                    "in_library": True,
                    "monitored": bool(track_row.get("monitored", True)),
                    "status": "missing",
                    "quality": None,
                    "file_count": 0,
                    "track_count": 1,
                }

    # -------------------------------------------------------------------------
    # 2. Resolve Album Availability
    # -------------------------------------------------------------------------
    elif album_clean and artist_clean:
        artist_row = db.get_library_artist_by_name(artist_clean)
        if not artist_row and fid:
            with db._lock:
                cur = db.conn.execute(
                    "SELECT * FROM library_artists WHERE foreign_artist_id = ? LIMIT 1",
                    (fid,),
                )
                r = cur.fetchone()
                if r:
                    artist_row = db._map_library_artist(r)

        if artist_row:
            album_row = db.get_library_album_by_title(artist_row["id"], album_clean)
            if not album_row:
                clean_a = clean_library_name(album_clean)
                albums = db.list_library_albums(artist_id=artist_row["id"], limit=200)
                for a in albums:
                    if a.get("clean_title") == clean_a or clean_library_name(a.get("title", "")) == clean_a:
                        album_row = a
                        break

            if not album_row and fid:
                with db._lock:
                    cur = db.conn.execute(
                        "SELECT * FROM library_albums WHERE foreign_album_id = ? AND artist_id = ? LIMIT 1",
                        (fid, str(artist_row["id"])),
                    )
                    r = cur.fetchone()
                    if r:
                        album_row = db._map_library_album(r)

            if album_row:
                tracks = db.list_library_tracks(album_id=album_row["id"], limit=500)
                track_count = max(len(tracks), int(album_row.get("total_tracks") or 0))
                file_count = 0
                first_quality = None

                for t in tracks:
                    fl = db.get_library_file_for_track(t["id"])
                    if fl:
                        file_count += 1
                        if not first_quality and fl.get("quality_name"):
                            first_quality = fl["quality_name"]

                if file_count >= track_count and file_count > 0:
                    status_str = "available"
                elif 0 < file_count < track_count:
                    status_str = "partial"
                else:
                    status_str = "missing"

                return {
                    "in_library": True,
                    "monitored": bool(album_row.get("monitored", True)),
                    "status": status_str,
                    "quality": first_quality,
                    "file_count": file_count,
                    "track_count": track_count,
                }

    # -------------------------------------------------------------------------
    # 3. Resolve Artist Availability
    # -------------------------------------------------------------------------
    elif artist_clean:
        artist_row = db.get_library_artist_by_name(artist_clean)
        if not artist_row and fid:
            with db._lock:
                cur = db.conn.execute(
                    "SELECT * FROM library_artists WHERE foreign_artist_id = ? LIMIT 1",
                    (fid,),
                )
                r = cur.fetchone()
                if r:
                    artist_row = db._map_library_artist(r)

        if artist_row:
            return {
                "in_library": True,
                "monitored": bool(artist_row.get("monitored", True)),
                "status": "available",
                "quality": None,
                "file_count": 0,
                "track_count": 0,
            }

    # -------------------------------------------------------------------------
    # 4. Fallback: Direct foreign_id matching if names did not match
    # -------------------------------------------------------------------------
    if fid:
        with db._lock:
            # Foreign track lookup
            cur = db.conn.execute(
                "SELECT * FROM library_tracks WHERE foreign_track_id = ? LIMIT 1",
                (fid,),
            )
            r = cur.fetchone()
            if r:
                t = db._map_library_track(r)
                fl = db.get_library_file_for_track(t["id"])
                if fl:
                    cutoff_met = bool(fl.get("cutoff_met", True))
                    return {
                        "in_library": True,
                        "monitored": bool(t.get("monitored", True)),
                        "status": "available" if cutoff_met else "cutoff_unmet",
                        "quality": fl.get("quality_name"),
                        "file_count": 1,
                        "track_count": 1,
                    }
                return {
                    "in_library": True,
                    "monitored": bool(t.get("monitored", True)),
                    "status": "missing",
                    "quality": None,
                    "file_count": 0,
                    "track_count": 1,
                }

            # Foreign album lookup
            cur = db.conn.execute(
                "SELECT * FROM library_albums WHERE foreign_album_id = ? LIMIT 1",
                (fid,),
            )
            r = cur.fetchone()
            if r:
                alb = db._map_library_album(r)
                tracks = db.list_library_tracks(album_id=alb["id"], limit=500)
                track_count = max(len(tracks), int(alb.get("total_tracks") or 0))
                file_count = 0
                first_quality = None
                for trk in tracks:
                    fl = db.get_library_file_for_track(trk["id"])
                    if fl:
                        file_count += 1
                        if not first_quality and fl.get("quality_name"):
                            first_quality = fl["quality_name"]
                if file_count >= track_count and file_count > 0:
                    st = "available"
                elif 0 < file_count < track_count:
                    st = "partial"
                else:
                    st = "missing"
                return {
                    "in_library": True,
                    "monitored": bool(alb.get("monitored", True)),
                    "status": st,
                    "quality": first_quality,
                    "file_count": file_count,
                    "track_count": track_count,
                }

            # Foreign artist lookup
            cur = db.conn.execute(
                "SELECT * FROM library_artists WHERE foreign_artist_id = ? LIMIT 1",
                (fid,),
            )
            r = cur.fetchone()
            if r:
                art = db._map_library_artist(r)
                return {
                    "in_library": True,
                    "monitored": bool(art.get("monitored", True)),
                    "status": "available",
                    "quality": None,
                    "file_count": 0,
                    "track_count": 0,
                }

    # -------------------------------------------------------------------------
    # 5. Default: Not in Library
    # -------------------------------------------------------------------------
    return {
        "in_library": False,
        "monitored": False,
        "status": "none",
        "quality": None,
        "file_count": 0,
        "track_count": 0,
    }
