"""Exclusions management: admin API for blocked artists, albums, and tracks.

Admin only, core tier (deny-by-default on the gateway).
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from trackseerr.api.dependencies import get_db, require_admin, require_core_tier
from trackseerr.api.schemas.exclusions import (
    ExclusionCheckResponse,
    ExclusionCreate,
    ExclusionCreated,
    ExclusionPage,
)
from trackseerr.exclusions import find_exclusion, match_key
from trackseerr.library_manager import MODE_LIDARR, get_library_mode
from trackseerr.storage import Database, clean_library_name

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier)])


def _clean_exclusion_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "scope": row["scope"],
        "mbid": row.get("mbid"),
        "artist_name": row["artist_name"],
        "title": row.get("title"),
        "reason": row.get("reason"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
    }


def _resolve_source_ref(
    body: ExclusionCreate, db: Database
) -> tuple[str, str, str | None, str | None]:
    """Resolves scope, artist_name, title, and mbid from a source ref or body fields."""
    if body.library_artist_id is not None:
        artist = db.get_library_artist(str(body.library_artist_id))
        if not artist:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Library artist {body.library_artist_id} not found",
            )
        return "artist", str(artist["name"]), None, artist.get("foreign_artist_id")

    if body.library_album_id is not None:
        album = db.get_library_album(str(body.library_album_id))
        if not album:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Library album {body.library_album_id} not found",
            )
        artist = db.get_library_artist(str(album["artist_id"]))
        if not artist:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Library artist for album {body.library_album_id} not found",
            )
        mbid = album.get("mb_release_group_id") or album.get("foreign_album_id")
        return "album", str(artist["name"]), str(album["title"]), mbid

    if body.library_track_id is not None:
        track = db.get_library_track(str(body.library_track_id))
        if not track:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Library track {body.library_track_id} not found",
            )
        artist = db.get_library_artist(str(track["artist_id"]))
        if not artist:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Library artist for track {body.library_track_id} not found",
            )
        return "track", str(artist["name"]), str(track["title"]), track.get("mb_recording_id")

    if body.missing_track_id is not None:
        missing = db.get_missing_track(body.missing_track_id)
        if not missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Missing track {body.missing_track_id} not found",
            )
        return "track", str(missing["artist"]), str(missing["title"]), None

    return str(body.scope), str(body.artist_name), body.title, body.mbid


def _unmonitor_artist(db: Database, artist_id: str) -> int:
    with db._lock:
        a = db.conn.execute(
            "SELECT COUNT(*) FROM library_artists WHERE id = ? AND monitored = 1",
            (str(artist_id),),
        ).fetchone()
        al = db.conn.execute(
            "SELECT COUNT(*) FROM library_albums WHERE artist_id = ? AND monitored = 1",
            (str(artist_id),),
        ).fetchone()
        t = db.conn.execute(
            "SELECT COUNT(*) FROM library_tracks WHERE artist_id = ? AND monitored = 1",
            (str(artist_id),),
        ).fetchone()
        count = int(a[0] if a else 0) + int(al[0] if al else 0) + int(t[0] if t else 0)
    db.set_artist_monitored(str(artist_id), False, cascade_children=True)
    return count


def _unmonitor_album(db: Database, album_id: str) -> int:
    with db._lock:
        al = db.conn.execute(
            "SELECT COUNT(*) FROM library_albums WHERE id = ? AND monitored = 1",
            (str(album_id),),
        ).fetchone()
        t = db.conn.execute(
            "SELECT COUNT(*) FROM library_tracks WHERE album_id = ? AND monitored = 1",
            (str(album_id),),
        ).fetchone()
        count = int(al[0] if al else 0) + int(t[0] if t else 0)
    db.set_album_monitored(str(album_id), False, cascade_tracks=True)
    return count


def _unmonitor_track(db: Database, track_id: str) -> int:
    with db._lock:
        t = db.conn.execute(
            "SELECT COUNT(*) FROM library_tracks WHERE id = ? AND monitored = 1",
            (str(track_id),),
        ).fetchone()
        count = int(t[0] if t else 0)
    db.set_track_monitored(str(track_id), False)
    return count


def _find_artist_id(
    db: Database,
    ref_id: str | None,
    mbid: str | None,
    artist_name: str,
) -> str | None:
    if ref_id is not None:
        return str(ref_id)
    if mbid:
        artist_row = db.get_library_artist_by_foreign_id(mbid) or db.get_library_artist_by_mbid(mbid)
        if artist_row:
            return str(artist_row["id"])
    artist_row = db.get_library_artist_by_name(artist_name)
    return str(artist_row["id"]) if artist_row else None


def _find_album_id(
    db: Database,
    ref_id: str | None,
    mbid: str | None,
    artist_name: str,
    title: str | None,
) -> str | None:
    if ref_id is not None:
        return str(ref_id)
    if mbid:
        album_row = db.get_library_album_by_release_group_id(mbid) or db.get_library_album_by_foreign_id(mbid)
        if album_row:
            return str(album_row["id"])
    if title:
        artist_row = db.get_library_artist_by_name(artist_name)
        if artist_row:
            album_row = db.get_library_album_by_title(str(artist_row["id"]), title)
            if album_row:
                return str(album_row["id"])
    return None


def _unmonitor_native_rows(
    db: Database,
    scope: str,
    artist_name: str,
    title: str | None,
    mbid: str | None,
    body: ExclusionCreate,
) -> int:
    if get_library_mode(db) == MODE_LIDARR:
        return 0

    if scope == "artist":
        artist_id = _find_artist_id(db, body.library_artist_id, mbid, artist_name)
        return _unmonitor_artist(db, artist_id) if artist_id else 0

    if scope == "album":
        album_id = _find_album_id(db, body.library_album_id, mbid, artist_name, title)
        return _unmonitor_album(db, album_id) if album_id else 0

    if scope == "track":
        if body.library_track_id is not None:
            return _unmonitor_track(db, str(body.library_track_id))
        if mbid:
            track_row = db.get_library_track_by_mb_recording_id(mbid) or db.get_library_track_by_foreign_id(mbid)
            if track_row:
                return _unmonitor_track(db, str(track_row["id"]))
        if title:
            artist_row = db.get_library_artist_by_name(artist_name)
            if artist_row:
                clean_t = clean_library_name(title)
                with db._lock:
                    rows = db.conn.execute(
                        "SELECT id FROM library_tracks WHERE artist_id = ? AND clean_title = ? AND monitored = 1",
                        (str(artist_row["id"]), clean_t),
                    ).fetchall()
                for r in rows:
                    db.set_track_monitored(str(r[0]), False)
                return len(rows)

    return 0


@router.get("", response_model=ExclusionPage, summary="List exclusions")
def list_exclusions(
    scope: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """List exclusions with optional filtering and pagination."""
    offset = (page - 1) * page_size
    items, total = db.list_exclusions(scope=scope, search=search, limit=page_size, offset=offset)
    return {
        "items": [_clean_exclusion_item(r) for r in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/check", response_model=ExclusionCheckResponse, summary="Check if an item is excluded")
def check_exclusion(
    artist: str | None = Query(None),
    title: str | None = Query(None),
    album: str | None = Query(None),
    artist_mbid: str | None = Query(None),
    album_mbid: str | None = Query(None),
    recording_mbid: str | None = Query(None),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Check if an item matches an active exclusion by match key or MBID."""
    hit = find_exclusion(
        db,
        artist=artist,
        title=title,
        album=album,
        artist_mbid=artist_mbid,
        album_mbid=album_mbid,
        recording_mbid=recording_mbid,
    )
    if hit is not None:
        return {"excluded": True, "reason": hit.describe()}
    return {"excluded": False, "reason": None}


@router.post("", response_model=ExclusionCreated, status_code=status.HTTP_201_CREATED, summary="Create an exclusion")
def create_exclusion(
    body: ExclusionCreate,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Create a new exclusion and unmonitor matching native library items."""
    scope, artist_name, title, mbid = _resolve_source_ref(body, db)

    try:
        match_key(scope, artist_name, title)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    created_by = current_user.get("username")
    item = db.add_exclusion(
        scope=scope,
        artist_name=artist_name,
        title=title,
        mbid=mbid,
        reason=body.reason,
        created_by=created_by,
    )

    if body.missing_track_id is not None:
        db.mark_missing_tracks_list_applied([body.missing_track_id])

    unmonitored = _unmonitor_native_rows(db, scope, artist_name, title, mbid, body)

    return {
        "item": _clean_exclusion_item(item),
        "unmonitored": unmonitored,
    }


@router.delete("/{exclusion_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an exclusion")
def delete_exclusion(
    exclusion_id: str,
    db: Database = Depends(get_db),
) -> Response:
    """Delete an exclusion by ID. Removing an exclusion does not re-monitor items."""
    deleted = db.delete_exclusion(exclusion_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exclusion {exclusion_id} not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
