"""Endpoints for album management and bulk hydration."""

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import RedirectResponse


from plex_playlist_sync import art_pipeline
from plex_playlist_sync import lidarr_library
from plex_playlist_sync.api.dependencies import (
    get_db,
    get_lidarr_client,
    get_mbid_enricher,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from plex_playlist_sync.api.schemas.library import (
    AlbumsUpdatedResponse,
    CommandResponse,
    LibraryAlbumRecord,
    SuccessResponse,
)
from plex_playlist_sync.api.routes.activity import (
    lidarr_numeric_id,
    require_lidarr,
)
from plex_playlist_sync.clients.lidarr import (
    LidarrClient,
)
from plex_playlist_sync.album_track_hydration import hydrate_album_tracks
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (_is_lidarr, native_only, _lidarr_fetch, _lidarr_mutation, _lidarr_image, _versioned_art_url, _art_media_type, _native_art, _unlink_library_file, _enrich_albums)
from .models import (AlbumMonitoredRequest, AlbumBulkEditRequest)

# Most albums whose tracklists one bulk "monitor" request will fetch on demand.
BULK_HYDRATE_LIMIT = 25

# Total wall-clock budget for all hydration in one bulk "monitor" request.
BULK_HYDRATE_DEADLINE_SECONDS = 15.0

@router.get("/albums", response_model=list[LibraryAlbumRecord], response_model_exclude_unset=True)
def list_albums(
    artist_id: Optional[str] = None,
    monitored_only: bool = False,
    query: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Lists library albums with optional artist filtering, search query, and pagination, attaching artist name and track count."""
    albums = db.list_library_albums(
        artist_id=artist_id, monitored_only=monitored_only, query=query, limit=limit, offset=offset
    )
    if not albums:
        return []
    return _enrich_albums(db, albums)

@router.get("/albums/{album_id}", dependencies=[Depends(require_core_tier)], response_model=LibraryAlbumRecord, response_model_exclude_unset=True)
def get_album(
    album_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves an album by ID, including its tracks and their linked library files."""
    if _is_lidarr(db):
        numeric = lidarr_numeric_id(album_id, "Album")
        lidarr = require_lidarr(client)
        return _lidarr_fetch(lambda: lidarr_library.album_detail(lidarr, numeric), "Album")
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
    result = dict(album)
    result["cover_url"] = _versioned_art_url("album", album_id, album.get("art_version"), album.get("cover_url"))
    tracks = db.list_library_tracks(album_id=album_id, limit=500)
    for t in tracks:
        file_info = db.get_library_file_for_track(t["id"])
        t["file"] = file_info
    result["tracks"] = tracks
    return result

@router.get("/albums/{album_id}/cover", dependencies=[Depends(require_core_tier)])
def get_album_cover(
    album_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    if_none_match: Optional[str] = Header(None),
    size: Optional[int] = Query(None, description="Thumbnail size: 250 or 500; anything else serves the original"),
    v: Optional[str] = Query(None, description="Art version token; the response is immutable only when it equals the served file's version"),
    _admin: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Serves local album cover artwork or redirects to remote artwork / placeholder."""
    if _is_lidarr(db):
        return _lidarr_image("albums", "album", album_id, "Album", ("cover",), client, if_none_match, size, v)
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    remote_cover = album.get("cover_url")
    served = art_pipeline.serve_art(db, "album", album, db.get_media_management_settings())
    if served is not None:
        return _native_art(served, _art_media_type(served), size, if_none_match, v)

    if remote_cover and (remote_cover.startswith("http://") or remote_cover.startswith("https://")):
        return RedirectResponse(url=remote_cover, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    return RedirectResponse(url="/placeholder.svg", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

@router.put("/albums/{album_id}/monitored", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=LibraryAlbumRecord, response_model_exclude_unset=True)
def set_album_monitored(
    album_id: str,
    body: AlbumMonitoredRequest,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Updates monitoring status for an album and optionally cascades to child tracks."""
    if _is_lidarr(db):
        numeric = lidarr_numeric_id(album_id, "Album")
        lidarr = require_lidarr(client)
        return _lidarr_mutation(
            db, lambda: lidarr_library.set_album_monitored(lidarr, numeric, body.monitored), "Album"
        )
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    if body.monitored and body.cascade_tracks:
        hydrate_album_tracks(db, enricher, album_id)  # no tracks yet: fetch them so the cascade has rows to monitor
    db.set_album_monitored(
        album_id=album_id,
        monitored=body.monitored,
        cascade_tracks=body.cascade_tracks,
    )
    updated = db.get_library_album(album_id)
    if updated is None:  # deleted between the write and the read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
    return updated

@router.post("/albums/bulk-edit", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=AlbumsUpdatedResponse, response_model_exclude_unset=True)
def bulk_edit_albums(
    body: AlbumBulkEditRequest,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, int]:
    """Monitors or unmonitors many albums at once (native: albums and their tracks; Lidarr: one album/monitor call)."""
    if not body.album_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="album_ids must not be empty")
    if _is_lidarr(db):
        lidarr = require_lidarr(client)
        numeric_ids = [lidarr_numeric_id(i, "Album") for i in body.album_ids]
        updated = _lidarr_mutation(
            db, lambda: lidarr_library.set_albums_monitored(lidarr, numeric_ids, body.monitored), "Album"
        )
        return {"albums_updated": int(updated)}
    if body.monitored:
        # Albums without a tracklist are hydrated first, bounded by BULK_HYDRATE_LIMIT albums AND one TOTAL deadline
        # for the whole batch (each fetch is a rate-limited MusicBrainz call). Albums not reached in time are still
        # monitored and simply hydrate lazily on first open.
        deadline = time.monotonic() + BULK_HYDRATE_DEADLINE_SECONDS
        for alb_id in body.album_ids[:BULK_HYDRATE_LIMIT]:
            if time.monotonic() >= deadline:
                break
            hydrate_album_tracks(db, enricher, alb_id, deadline=deadline)
    return {"albums_updated": db.bulk_set_albums_monitored(body.album_ids, body.monitored)}

@router.post("/albums/{album_id}/search", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=CommandResponse, response_model_exclude_unset=True)
def search_album(
    album_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Asks Lidarr to search for the album (Lidarr mode only)."""
    if not _is_lidarr(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only available while Lidarr manages the library")
    numeric = lidarr_numeric_id(album_id, "Album")
    lidarr = require_lidarr(client)
    _lidarr_mutation(db, lambda: lidarr_library.search_album(lidarr, numeric), "Album")
    return {"success": True, "message": "Search queued in Lidarr"}

@router.delete("/albums/{album_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=SuccessResponse, response_model_exclude_unset=True)
def delete_album(
    album_id: str,
    delete_files: bool = Query(False),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes an album and cascades to child tracks and files. Optionally unlinks files on disk."""
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    if delete_files:
        tracks = db.list_library_tracks(album_id=album_id, limit=10000)
        for t in tracks:
            f = db.get_library_file_for_track(t["id"])
            if f and f.get("file_path"):
                _unlink_library_file(db, f)

    success = db.delete_library_album(album_id)
    return {"success": success}

