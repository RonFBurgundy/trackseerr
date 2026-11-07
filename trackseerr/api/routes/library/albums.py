"""Endpoints for album management and bulk hydration."""

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import RedirectResponse


from trackseerr import art_pipeline
from trackseerr import lidarr_library
from trackseerr.api.dependencies import (
    get_db,
    get_discovery_client,
    get_lidarr_client,
    get_mbid_enricher,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from trackseerr.api.schemas.library import (
    AlbumsUpdatedResponse,
    CommandResponse,
    LibraryAlbumRecord,
    MbLinkRequest,
    MbReleaseSeed,
    MbUnlinkedPage,
    SuccessResponse,
)
from trackseerr.api.routes.activity import (
    lidarr_numeric_id,
    require_lidarr,
)
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.lidarr import (
    LidarrApiError,
    LidarrClient,
    LidarrNotFound,
)
from trackseerr.exclusions import annotate_exclusions, find_exclusion
from trackseerr.album_track_hydration import hydrate_album_tracks
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.mb_seed import build_release_seed, link_album_release
from trackseerr.storage import Database


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
    enriched = _enrich_albums(db, albums)
    annotate_exclusions(db, "album", enriched)
    return enriched

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
    art = db.get_library_artist(result["artist_id"]) if result.get("artist_id") else None
    artist_name = art["name"] if art else None
    result["artist_name"] = artist_name
    result["cover_url"] = _versioned_art_url("album", album_id, album.get("art_version"), album.get("cover_url"))
    tracks = db.list_library_tracks(album_id=album_id, limit=500)
    for t in tracks:
        file_info = db.get_library_file_for_track(t["id"])
        t["file"] = file_info
        if not t.get("artist_name"):
            t["artist_name"] = artist_name
        if not t.get("album_title"):
            t["album_title"] = result.get("title")
    annotate_exclusions(
        db,
        "album",
        [result],
        artist_name_by_id={result["artist_id"]: artist_name} if result.get("artist_id") and artist_name else None,
    )
    annotate_exclusions(
        db,
        "track",
        tracks,
        artist_name_by_id={result["artist_id"]: artist_name} if result.get("artist_id") and artist_name else None,
    )
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
        if body.monitored:
            raw_alb = _lidarr_fetch(lambda: lidarr.fetch_album(numeric), "Album")
            art_name = raw_alb.get("artist", {}).get("artistName") if isinstance(raw_alb.get("artist"), dict) else None
            if not art_name:
                art_name = raw_alb.get("artistName")
            alb_title = raw_alb.get("title")
            alb_mbid = raw_alb.get("foreignAlbumId")
            hit = find_exclusion(db, artist=art_name, album=alb_title, album_mbid=alb_mbid)
            if hit is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"{hit.describe()}. Remove the exclusion to monitor it.",
                )
        return _lidarr_mutation(
            db, lambda: lidarr_library.set_album_monitored(lidarr, numeric, body.monitored), "Album"
        )
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    if body.monitored:
        art = db.get_library_artist(album["artist_id"]) if album.get("artist_id") else None
        art_name = art["name"] if art else None
        alb_mbid = album.get("mb_release_group_id") or album.get("foreign_album_id")
        hit = find_exclusion(db, artist=art_name, album=album.get("title"), album_mbid=alb_mbid)
        if hit is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{hit.describe()}. Remove the exclusion to monitor it.",
            )

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
    annotate_exclusions(db, "album", [updated])
    return updated

def _filter_excluded_albums(db: Database, album_ids: list[str]) -> tuple[list[str], int]:
    placeholders = ",".join("?" for _ in album_ids)
    rows = db.conn.execute(
        f"SELECT id, artist_id, title, mb_release_group_id, foreign_album_id FROM library_albums WHERE id IN ({placeholders})",
        album_ids,
    ).fetchall()
    album_map = {r["id"]: dict(r) for r in rows}
    artist_ids = {r["artist_id"] for r in album_map.values() if r.get("artist_id")}
    artist_names: dict[str, str] = {}
    if artist_ids:
        a_placeholders = ",".join("?" for _ in artist_ids)
        a_rows = db.conn.execute(
            f"SELECT id, name FROM library_artists WHERE id IN ({a_placeholders})",
            list(artist_ids),
        ).fetchall()
        artist_names = {r["id"]: r["name"] for r in a_rows}

    allowed_ids: list[str] = []
    skipped = 0
    for aid in album_ids:
        alb = album_map.get(aid)
        if not alb:
            allowed_ids.append(aid)
            continue
        art_name = artist_names.get(alb.get("artist_id"))
        alb_mbid = alb.get("mb_release_group_id") or alb.get("foreign_album_id")
        hit = find_exclusion(db, artist=art_name, album=alb.get("title"), album_mbid=alb_mbid)
        if hit is not None:
            skipped += 1
        else:
            allowed_ids.append(aid)
    return allowed_ids, skipped


def _filter_lidarr_excluded_albums(db: Database, lidarr: LidarrClient, album_ids: list[str]) -> tuple[list[str], int]:
    allowed = []
    skipped = 0
    for alb_id_str in album_ids:
        num = lidarr_numeric_id(alb_id_str, "Album")
        hit = None
        try:
            raw_alb = _lidarr_fetch(lambda n=num: lidarr.fetch_album(n), "Album")
            art_name = raw_alb.get("artist", {}).get("artistName") if isinstance(raw_alb.get("artist"), dict) else None
            if not art_name:
                art_name = raw_alb.get("artistName")
            alb_title = raw_alb.get("title")
            alb_mbid = raw_alb.get("foreignAlbumId")
            hit = find_exclusion(db, artist=art_name, album=alb_title, album_mbid=alb_mbid)
        except (LidarrApiError, LidarrNotFound, HTTPException):
            pass
        if hit is not None:
            skipped += 1
        else:
            allowed.append(alb_id_str)
    return allowed, skipped


@router.post("/albums/bulk-edit", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=AlbumsUpdatedResponse, response_model_exclude_unset=True)
def bulk_edit_albums(
    body: AlbumBulkEditRequest,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Monitors or unmonitors many albums at once (native: albums and their tracks; Lidarr: one album/monitor call)."""
    if not body.album_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="album_ids must not be empty")

    target_ids = list(body.album_ids)
    skipped_excluded = 0

    if _is_lidarr(db):
        lidarr = require_lidarr(client)
        if body.monitored:
            target_ids, skipped_excluded = _filter_lidarr_excluded_albums(db, lidarr, target_ids)

        numeric_ids = [lidarr_numeric_id(i, "Album") for i in target_ids]
        updated = _lidarr_mutation(
            db, lambda: lidarr_library.set_albums_monitored(lidarr, numeric_ids, body.monitored), "Album"
        ) if numeric_ids else 0
        res: dict[str, Any] = {"albums_updated": int(updated)}
        if skipped_excluded > 0:
            res["skipped_excluded"] = skipped_excluded
        return res

    if body.monitored:
        target_ids, skipped_excluded = _filter_excluded_albums(db, target_ids)
        deadline = time.monotonic() + BULK_HYDRATE_DEADLINE_SECONDS
        for alb_id in target_ids[:BULK_HYDRATE_LIMIT]:
            if time.monotonic() >= deadline:
                break
            hydrate_album_tracks(db, enricher, alb_id, deadline=deadline)

    updated_count = db.bulk_set_albums_monitored(target_ids, body.monitored) if target_ids else 0
    res_albums: dict[str, Any] = {"albums_updated": updated_count}
    if skipped_excluded > 0:
        res_albums["skipped_excluded"] = skipped_excluded
    return res_albums

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
    """Deletes an album and cascades to child tracks and files. Optionally moves their files to the recycle bin."""
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    if delete_files:
        tracks = db.list_library_tracks(album_id=album_id, limit=10000)
        for t in tracks:
            for f in db.list_library_files_for_track(t["id"]):
                if f.get("file_path"):
                    _unlink_library_file(db, f)

    success = db.delete_library_album(album_id)
    return {"success": success}


@router.get(
    "/musicbrainz/unlinked",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=MbUnlinkedPage,
    response_model_exclude_unset=True,
)
def list_musicbrainz_unlinked(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    kind: Optional[str] = Query(None),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Lists albums without a MusicBrainz release link."""
    items, total = db.list_albums_without_mb_release(page=page, page_size=page_size, kind=kind)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get(
    "/albums/{album_id}/musicbrainz-seed",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=MbReleaseSeed,
    response_model_exclude_unset=True,
)
def get_album_musicbrainz_seed(
    album_id: str,
    redirect_uri: Optional[str] = Query(None),
    db: Database = Depends(get_db),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Builds prefilled form fields for seeding a release on MusicBrainz."""
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    clean_redirect: Optional[str] = None
    if redirect_uri:
        raw_uri = redirect_uri.strip()
        if len(raw_uri) <= 2048 and raw_uri.startswith(("http://", "https://")):
            clean_redirect = raw_uri

    try:
        return build_release_seed(db, discovery, album_id, redirect_uri=clean_redirect)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/albums/{album_id}/musicbrainz-link",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=LibraryAlbumRecord,
    response_model_exclude_unset=True,
)
def link_album_musicbrainz(
    album_id: str,
    body: MbLinkRequest,
    db: Database = Depends(get_db),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Links an album to a MusicBrainz release MBID."""
    album = db.get_library_album(album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")

    try:
        return link_album_release(db, enricher, album_id, body.release_mbid)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


