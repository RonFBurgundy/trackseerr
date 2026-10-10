"""Browse and statistics endpoints for artists, albums, and tracks."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status


from trackseerr import lidarr_library
from trackseerr.api.dependencies import (
    get_db,
    get_lidarr_client,
    get_mbid_enricher,
    require_admin,
    require_core_tier,
)
from trackseerr.api.schemas.library import (
    AlbumsPage,
    ArtistsPage,
    LibraryFacetsResponse,
    LibraryIndexResponse,
    LibraryStats,
    TracksPage,
)
from trackseerr.api.routes.activity import (
    MAX_PAGE,
    require_lidarr,
)
from trackseerr.clients.lidarr import (
    LidarrClient,
)
from trackseerr.album_track_hydration import hydrate_album_tracks
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_filters import LibraryFacetFilter, library_facets
from trackseerr.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (
    _SORT_DIR,
    _is_lidarr,
    _lidarr_fetch,
    _paged,
    _index,
    _lidarr_paged,
    _lidarr_index,
    _lidarr_tracks_paged,
    _lidarr_tracks_index,
    _enrich_artists,
    _enrich_albums,
    _enrich_tracks,
    NATIVE_ONLY_DETAIL,
    native_only,
    facet_query,
)

@router.get("/stats", response_model=LibraryStats, response_model_exclude_unset=True)
def get_library_stats(
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Aggregate library statistics (Lidarr's own statistics in Lidarr mode, the native catalog otherwise)."""
    if _is_lidarr(db):
        lidarr = require_lidarr(client)
        return _lidarr_fetch(lambda: lidarr_library.library_stats(lidarr), "Library")
    return db.get_library_stats()

@router.get(
    "/facets",
    dependencies=[Depends(require_core_tier), Depends(native_only)],
    response_model=LibraryFacetsResponse,
    response_model_exclude_unset=True,
)
def get_library_facets(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Summary of library facets: genres, countries, decades, types, and value bounds."""
    return library_facets(db)

@router.get("/artists/paged", dependencies=[Depends(require_core_tier)], response_model=ArtistsPage, response_model_exclude_unset=True)
def paged_artists(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    facets: LibraryFacetFilter = Depends(facet_query),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """A page of library artists (with counts) plus the filtered total."""
    if _is_lidarr(db):
        if not facets.is_empty():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        return _lidarr_paged(db, "artists", page, page_size, sort_key, sort_dir, q, monitored_only, None, client)
    return _paged("artists", page, page_size, sort_key, sort_dir, q, monitored_only, None, None, db, _enrich_artists, facets=facets)

@router.get("/artists/index", dependencies=[Depends(require_core_tier)], response_model=LibraryIndexResponse, response_model_exclude_unset=True)
def artists_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    facets: LibraryFacetFilter = Depends(facet_query),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Scrubber groups ``[{label, offset, count}]`` for the artists list, in its order."""
    if _is_lidarr(db):
        if not facets.is_empty():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        return _lidarr_index("artists", sort_key, sort_dir, q, monitored_only, None, client)
    return _index("artists", sort_key, sort_dir, q, monitored_only, None, None, db, facets=facets)

@router.get("/albums/paged", dependencies=[Depends(require_core_tier)], response_model=AlbumsPage, response_model_exclude_unset=True)
def paged_albums(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    facets: LibraryFacetFilter = Depends(facet_query),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """A page of library albums (with artist name and track count) plus the filtered total."""
    if _is_lidarr(db):
        if not facets.is_empty():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        return _lidarr_paged(db, "albums", page, page_size, sort_key, sort_dir, q, monitored_only, artist_id, client)
    return _paged("albums", page, page_size, sort_key, sort_dir, q, monitored_only, artist_id, None, db, _enrich_albums, facets=facets)

@router.get("/albums/index", dependencies=[Depends(require_core_tier)], response_model=LibraryIndexResponse, response_model_exclude_unset=True)
def albums_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    facets: LibraryFacetFilter = Depends(facet_query),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Scrubber groups for the albums list, in its order."""
    if _is_lidarr(db):
        if not facets.is_empty():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        return _lidarr_index("albums", sort_key, sort_dir, q, monitored_only, artist_id, client)
    return _index("albums", sort_key, sort_dir, q, monitored_only, artist_id, None, db, facets=facets)

@router.get("/tracks/paged", dependencies=[Depends(require_core_tier)], response_model=TracksPage, response_model_exclude_unset=True)
def paged_tracks(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    album_id: Optional[str] = None,
    facets: LibraryFacetFilter = Depends(facet_query),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """A page of library tracks (with artist, album and file details) plus the filtered total.

    Opening an album whose tracklist was never fetched (unmonitored albums are not hydrated on add/refresh) fetches it
    from MusicBrainz once, stored unmonitored, before the first page is read. NOTE: this GET therefore has a write
    side effect (it may create track rows). It is single-flight per album, and an album whose fetch failed or came
    back empty is not retried for 10 minutes (see ``hydrate_album_tracks``).
    """
    if _is_lidarr(db):
        if not facets.is_empty():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        return _lidarr_tracks_paged(page, page_size, sort_key, sort_dir, q, monitored_only, album_id, client)
    if album_id and page == 1 and not q and not monitored_only:
        hydrate_album_tracks(db, enricher, album_id)
    return _paged(
        "tracks", page, page_size, sort_key, sort_dir, q, monitored_only, artist_id, album_id, db, _enrich_tracks, facets=facets
    )

@router.get("/tracks/index", dependencies=[Depends(require_core_tier)], response_model=LibraryIndexResponse, response_model_exclude_unset=True)
def tracks_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    album_id: Optional[str] = None,
    facets: LibraryFacetFilter = Depends(facet_query),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Scrubber groups for the tracks list, in its order."""
    if _is_lidarr(db):
        if not facets.is_empty():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        return _lidarr_tracks_index(sort_key, sort_dir, album_id, client)
    return _index("tracks", sort_key, sort_dir, q, monitored_only, artist_id, album_id, db, facets=facets)

