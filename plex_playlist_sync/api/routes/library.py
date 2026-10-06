"""FastAPI router for native music library management.

Provides statistics and browsing CRUD for artists, albums, tracks, and files;
controls for filesystem scanning and Lidarr catalog migration;
interactive Manual Import scan and commit pipelines;
and Arr-grade token-template preview and batch-renaming engine.
"""

from datetime import datetime, timezone
import sqlite3
import json
import logging
import os
import re
from pathlib import Path
import threading
import time
from typing import Any, Callable, Literal, Optional
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator

import httpx

from plex_playlist_sync import delay_gate
from plex_playlist_sync import library_paging as paging
from plex_playlist_sync.acquisition_coordinator import _to_quality_profile
from plex_playlist_sync import art_pipeline, art_thumbs
from plex_playlist_sync import lidarr_library
from plex_playlist_sync.download_roots import allowed_roots_for_all_clients
from plex_playlist_sync.item_history import TRIGGER_MANUAL_IMPORT, GrabTrigger, HistoryPresenter, emit, set_provenance
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.clients.acquisition import get_acquisition_driver, is_torrent_driver_type
from plex_playlist_sync.acquisition_worker import (
    MATCH_NONE,
    MATCH_STRONG,
    effective_import_mode,
    record_import_events,
    prepare_file_for_tagging,
    place_audio_file,
    preserves_source,
    seed_action,
    settle_transfer_after_import,
    reconcile_audio_file_to_track,
    reconcile_audio_file_to_track_scored,
    resolve_download_expected_tracks,
    safe_atomic_move,
)
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    get_discovery_client,
    get_lidarr_client,
    get_mbid_enricher,
    get_media_client,
    require_admin,
    require_core_tier,
    require_user,
    track_admin_actor,
)
from plex_playlist_sync.api.routes.activity import (
    MAX_PAGE,
    SORT_DIR_PATTERN,
    lidarr_call,
    lidarr_numeric_id,
    require_lidarr,
    validate_sort_key,
)
from plex_playlist_sync.clients.core_client import CoreClient
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.lidarr import (
    LidarrApiError,
    LidarrBadArtwork,
    LidarrClient,
    LidarrNotFound,
    MediaCover,
    _exc_text,
)
from plex_playlist_sync.album_track_hydration import album_hydration_lock, hydrate_album_tracks
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers import as_media_server
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_monitoring import (
    NATIVE_MONITOR_OPTIONS,
    RELEASE_PRIMARY_TYPES,
    RELEASE_SECONDARY_TYPES,
    accept_deprecated_profile_keys,
    album_in_metadata_profile,
    album_monitored_for_option,
    normalize_secondary_types,
    DEFAULT_MONITOR_OPTION,
    hydrated_track_monitored,
    section_to_album_type,
)
from plex_playlist_sync.library_manager import MODE_LIDARR, ModeChanged, get_library_mode, work_guard
from plex_playlist_sync.library_availability import get_item_availability
from plex_playlist_sync.mediacover import mediacover_service
from plex_playlist_sync.models import (
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryCollection,
    LibraryFile,
    LibraryTrack,
    UserPermission,
)
from plex_playlist_sync.library import (
    AUDIO_EXTENSIONS,
    fingerprint_audio_file,
    find_folder_art,
    inspect_audio_file,
    parse_filename_track,
    resolve_album_artist,
    resolve_collision,
    write_audio_tags,
)
from plex_playlist_sync.library_scanner import library_scanner
from plex_playlist_sync.lidarr_migration import lidarr_migration_job
from plex_playlist_sync.naming import build_track_path
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.system_paths import is_system_folder_name
from plex_playlist_sync.recycle_bin import (
    is_excluded_entry,
    is_system_dirname,
    is_system_filename,
    log_recycled,
    recycle_in_place_target,
    recycle_replaced_files,
    restore_recycled,
)
from plex_playlist_sync.storage import Database, clean_library_name
from plex_playlist_sync.tag_store import UnknownTag
from plex_playlist_sync.track_counts import positive_int as _positive_int

logger = logging.getLogger(__name__)

router = APIRouter()

# Most albums whose tracklists one bulk "monitor" request will fetch on demand.
BULK_HYDRATE_LIMIT = 25
# Total wall-clock budget for all hydration in one bulk "monitor" request.
BULK_HYDRATE_DEADLINE_SECONDS = 15.0


# -------------------------------------------------------------------------
# Request Models
# -------------------------------------------------------------------------

MONITOR_OPTION_PATTERN = "^(" + "|".join(NATIVE_MONITOR_OPTIONS) + ")$"


class IngestArtistRequest(BaseModel):
    foreign_artist_id: str  # e.g. "deezer:artist:13" or "itunes:artist:..."
    artist_name: str
    quality_profile_id: Optional[str] = None
    monitor_option: Optional[str] = Field(default=None, pattern=MONITOR_OPTION_PATTERN)
    monitored: bool = True
    root_folder: Optional[str] = None
    # Native only. Omitted: the saved ``add_metadata_profile_id`` default (NULL = no profile); explicit null = none.
    metadata_profile_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def _deprecated_profile_keys(cls, data: Any) -> Any:
        return accept_deprecated_profile_keys(data)


class ArtistMonitoredRequest(BaseModel):
    monitored: bool
    cascade_children: bool = True
    monitor_option: Optional[str] = Field(default=None, pattern=MONITOR_OPTION_PATTERN)
    # Native only. Present (even null = clear) sets the artist's metadata profile; ``apply_monitor_to_albums`` then
    # recomputes the albums like an option change (omitted: no recompute for a profile-only change).
    metadata_profile_id: Optional[int] = None
    apply_monitor_to_albums: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def _deprecated_profile_keys(cls, data: Any) -> Any:
        return accept_deprecated_profile_keys(data)


class MetadataProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    primary_types: list[str]
    secondary_types: list[str]


class AlbumMonitoredRequest(BaseModel):
    monitored: bool
    cascade_tracks: bool = True


class ArtistBulkEditRequest(BaseModel):
    artist_ids: Optional[list[str]] = None
    all: bool = False
    monitored: Optional[bool] = None
    monitor_option: Optional[str] = Field(default=None, pattern=MONITOR_OPTION_PATTERN)
    quality_profile_id: Optional[str] = None  # an explicit null clears the profile; omitted leaves it alone
    metadata_profile_id: Optional[int] = None  # native only; an explicit null clears it, omitted leaves it alone
    # Omitted: an unmonitor (monitored=false) cascades to albums (and native tracks) by default, so artists never end
    # up unmonitored with monitored children; an explicit value always wins.
    apply_monitor_to_albums: Optional[bool] = None
    # Native only: tag ids to add to / remove from every selected artist (no overlap allowed).
    add_tags: list[int] = Field(default_factory=list)
    remove_tags: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _deprecated_profile_keys(cls, data: Any) -> Any:
        return accept_deprecated_profile_keys(data)


class ArtistTagsRequest(BaseModel):
    tags: list[int] = Field(default_factory=list, description="Tag ids; replaces the artist's tags")


class ArtistTagsResponse(BaseModel):
    artist_id: str
    tags: list[int]


class AlbumBulkEditRequest(BaseModel):
    album_ids: list[str]
    monitored: bool


class TrackMonitoredRequest(BaseModel):
    monitored: bool


class TrackBulkEditRequest(BaseModel):
    track_ids: list[str]
    monitored: bool


class ScanRequest(BaseModel):
    root_folder: Optional[str] = None
    prune_missing: bool = False


class MigrateLidarrRequest(BaseModel):
    auto_switch_mode: bool = True


class ManualImportScanRequest(BaseModel):
    folder_path: Optional[str] = None
    download_id: Optional[str] = None
    album_id: Optional[str] = None
    file_paths: Optional[list[str]] = None


class ManualImportItem(BaseModel):
    source_path: Optional[str] = None
    file_path: Optional[str] = None
    artist_name: Optional[str] = None
    artist_id: Optional[str] = None
    album_title: Optional[str] = None
    album_id: Optional[str] = None
    track_title: Optional[str] = None
    track_id: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = 1
    year: Optional[int] = None
    mode: Optional[Literal["move", "hardlink", "copy"]] = None
    write_tags: Optional[bool] = None


class ManualImportCommitRequest(BaseModel):
    items: list[ManualImportItem] = Field(default_factory=list)
    download_id: Optional[str] = None
    issue_id: Optional[str] = None  # an issue's "Rematch files" flow: replaced files are reported on that issue


class RenamePreviewRequest(BaseModel):
    artist_id: Optional[str] = None
    album_id: Optional[str] = None
    limit: int = 200


class RenameApplyRequest(BaseModel):
    file_ids: list[str] = Field(default_factory=list)


class FingerprintRequest(BaseModel):
    file_path: str


class CreateCollectionRequest(BaseModel):
    name: str
    summary: Optional[str] = None
    poster_url: Optional[str] = None
    monitored: bool = True


class AddAlbumToCollectionRequest(BaseModel):
    album_id: str
    order_index: int = 0


# -------------------------------------------------------------------------
# Security & Path Traversal Defense
# -------------------------------------------------------------------------

def _approved_media_bases(db: Optional[Database], purpose: str) -> list[Path]:
    """Configured roots (resolved) a path may live under for ``purpose``; empty when nothing is configured.

    ``library``: the library root. ``import``: library + download-client roots + legacy staging folder.
    ``internal``: ``import`` plus the app's own config dirs, only for callers that touch app-internal files.
    """
    if purpose not in _MEDIA_PATH_PURPOSES:
        raise ValueError(f"Unknown media path purpose: {purpose!r}")
    bases: list[Path] = []
    if db is None:
        return bases
    try:
        mm = db.get_media_management_settings()
        if mm.get("root_folder_path"):
            bases.append(Path(mm["root_folder_path"]).resolve())
        if purpose in ("import", "internal"):
            staging = str(mm.get("staging_folder_path") or "").strip()
            if staging:
                bases.append(Path(staging).resolve())
            roots = allowed_roots_for_all_clients(db, mm)
            bases.extend(roots.roots)
            if purpose == "internal":
                bases.extend(roots.config_dirs)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Could not query media management settings for path validation: %s", exc)
    return bases


_MEDIA_PATH_PURPOSES = ("library", "import", "internal")


def validate_media_path(path_str: str, db: Optional[Database] = None, purpose: str = "library") -> Path:
    """Validates a path against traversal and symlink escapes, and that it sits under a configured root.

    There are no hardcoded bases (``/data``, ``/tmp``, cwd and the config dir are not implicitly approved): only the
    configured library root (and, for ``purpose="import"``, download-client roots and the legacy staging folder) count.
    The path is ``resolve()``d (following symlinks) before the containment check.
    """
    if not path_str or not isinstance(path_str, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path must be a non-empty string",
        )

    parts = path_str.replace("\\", "/").split("/")
    if ".." in parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal attempt detected",
        )

    resolved = Path(path_str).resolve()
    approved_bases = _approved_media_bases(db, purpose)

    is_approved = any(resolved == base or resolved.is_relative_to(base) for base in approved_bases)
    if not is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: path '{path_str}' is outside approved media mounts",
        )

    return resolved


# -------------------------------------------------------------------------
# 1. Library Statistics & Browsing Endpoints
# -------------------------------------------------------------------------

@router.get("/stats")
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


# -------------------------------------------------------------------------
# Virtualized lists: paged records and the scrubber group index
# -------------------------------------------------------------------------

_SORT_DIR = SORT_DIR_PATTERN

# -------------------------------------------------------------------------
# Lidarr mode: the same routes serve Lidarr's library live (ids are Lidarr numeric ids)
# -------------------------------------------------------------------------

NATIVE_ONLY_DETAIL = "Not available while Lidarr manages the library"


def _is_lidarr(db: Database) -> bool:
    return get_library_mode(db) == MODE_LIDARR


def native_only(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> None:
    """Route dependency: 409 while Lidarr manages the library (after the admin check, so non-admins still get 403)."""
    if _is_lidarr(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)


def _lidarr_fetch(fn: Callable[[], Any], what: str) -> Any:
    try:
        return fn()
    except LidarrNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")
    except LidarrApiError as exc:
        logger.warning("Lidarr request failed: %s", redact_text(_exc_text(exc)))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=redact_text(_exc_text(exc)))


def _lidarr_mutation(db: Database, fn: Callable[[], Any], what: str) -> Any:
    """A Lidarr mutation under ``work_guard(lidarr)``; the manager flipping mid-flight is a 409."""
    try:
        with work_guard(db, MODE_LIDARR):
            return _lidarr_fetch(fn, what)
    except ModeChanged:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The library manager changed while this was running; reload and try again.",
        )


_PLACEHOLDER = "/placeholder.svg"


def _image_headers(cache_control: str, etag: Optional[str] = None) -> dict[str, str]:
    """Headers for every artwork response: never sniffed, never scriptable even if navigated to directly."""
    headers = {
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    if etag:
        headers["ETag"] = etag
    return headers


def _placeholder() -> RedirectResponse:
    return RedirectResponse(url=_PLACEHOLDER, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


def _lidarr_image(
    kind_list: str,
    kind_cover: str,
    raw_id: str,
    what: str,
    types: tuple[str, ...],
    client: Optional[LidarrClient],
    if_none_match: Optional[str] = None,
    size: Optional[int] = None,
    version: Optional[str] = None,
) -> Response:
    """Serves one Lidarr media cover through the core; the API key never leaves the server.

    ``size`` (250 or 500) selects Lidarr's pre-rendered thumbnail variant, falling back to the original when Lidarr has
    none. Fetched covers are kept on disk, so a repeat view (or another browser) never reaches Lidarr; a matching
    ``If-None-Match`` is a body-less 304. Only raster types pass (JPEG/PNG/WebP/GIF); anything else becomes the
    placeholder. Upstream concurrency is bounded and a saturated proxy answers 503 + Retry-After.
    """
    numeric = lidarr_numeric_id(raw_id, what)
    lidarr = require_lidarr(client)
    base = _lidarr_fetch(lambda: lidarr_library.cover_file(kind_list, lidarr, numeric, types), what)
    if base is None:
        return _placeholder()
    name = lidarr_library.sized_cover_name(base, size)
    identity = lidarr_library._identity(lidarr)
    # Immutable only when the request's ``v`` is the token the stored body was fetched for.
    def cache_for(stored_version: Optional[str]) -> str:
        return _art_cache_control(version, stored_version)

    known = lidarr_library.known_cover_etag(identity, kind_cover, numeric, name)
    if known and lidarr_library.etag_matches(if_none_match, known):
        known_version = lidarr_library.known_cover_version(identity, kind_cover, numeric, name)
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=_image_headers(cache_for(known_version), known))
    cached = lidarr_library.read_cached_cover(identity, kind_cover, numeric, name)
    if cached is not None and cached.fresh:
        lidarr_library.remember_cover_etag(identity, kind_cover, numeric, name, cached.etag, cached.version)
        headers = _image_headers(cache_for(cached.version), cached.etag)
        if lidarr_library.etag_matches(if_none_match, cached.etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return Response(content=cached.body, media_type=cached.content_type, headers=headers)

    def fetch_one(filename: str) -> Optional[MediaCover]:
        try:
            return lidarr.fetch_mediacover(
                kind_cover, numeric, filename, lidarr_library.MAX_COVER_BYTES, lidarr_library.COVER_DEADLINE_SECONDS
            )
        except LidarrNotFound:
            return None
        except LidarrBadArtwork as exc:
            logger.warning(
                "Lidarr %s %s artwork refused: %s", kind_cover, numeric, redact_text(_exc_text(exc))
            )
            return None

    def fetch() -> Optional[MediaCover]:
        got = fetch_one(name)
        if got is None and name != base:
            got = fetch_one(base)  # Lidarr has not rendered that size: serve the original under the sized key
        return got

    upstream_failed = False
    try:
        with lidarr_library.cover_slot():
            try:
                fetched = _lidarr_fetch(fetch, what)
            except HTTPException:
                if cached is None:
                    raise
                logger.warning("Lidarr unreachable for %s %s artwork; serving the expired cached copy", kind_cover, numeric)
                fetched, upstream_failed = None, True
    except lidarr_library.CoverBusy:
        logger.warning("Lidarr artwork proxy saturated; answering 503 for %s %s", kind_cover, numeric)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Artwork proxy is busy; retry shortly",
            headers={"Retry-After": "2"},
        )
    if fetched is None:
        if cached is not None and upstream_failed:
            return Response(content=cached.body, media_type=cached.content_type, headers=_image_headers(_COVER_CACHE, cached.etag))
        return _placeholder()
    etag = lidarr_library.cover_etag(identity, kind_cover, numeric, name, fetched.validator)
    lidarr_library.remember_cover_etag(identity, kind_cover, numeric, name, etag, version)
    lidarr_library.write_cached_cover(
        identity, kind_cover, numeric, name, fetched.body, fetched.content_type, etag, version
    )
    headers = _image_headers(cache_for(version), etag)
    if lidarr_library.etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=fetched.body, media_type=fetched.content_type, headers=headers)


# Art sits behind require_admin, so it must be ``private``; a day of freshness plus a cheap ETag revalidation (304)
# means replaced art shows up within a day. Only URLs carrying a ``?v=`` version token (below) are ``immutable``.
_COVER_CACHE = "private, max-age=86400"
_NATIVE_ART_CACHE = _COVER_CACHE
# A request whose ``?v=`` token equals the version of the exact file being served names one revision of the art, so it
# can be cached for a year with no revalidation, as Lidarr does for ``?lastWrite=``. A token that does not match (stale,
# forged, or for another file) never earns it.
_IMMUTABLE_ART_CACHE = "private, max-age=31536000, immutable"


def _art_cache_control(requested: Optional[str], current: Optional[str]) -> str:
    """Immutable only when the request's ``v`` equals the version ``current`` of the file actually served."""
    return _IMMUTABLE_ART_CACHE if requested and current and requested == current else _NATIVE_ART_CACHE


def _versioned_art_url(kind: str, item_id: Any, version: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """The art URL for a native record: the local proxy URL with ``?v=<version>`` once the art is local, else the
    record's existing value (a remote URL or an unversioned proxy URL)."""
    if not version:
        return fallback
    leaf = "image" if kind == "artist" else "cover"
    return f"/api/library/{kind}s/{item_id}/{leaf}?v={version}"


def _art_media_type(path: Path) -> str:
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


def _native_art(
    src: Path, media_type: str, size: Optional[int], if_none_match: Optional[str], version: Optional[str] = None
) -> Response:
    """Serves native artwork. ``size`` 250/500 returns a cached JPEG derivative (generated once, off the event loop
    because the routes are sync), with a strong ETag and a body-less 304 on a match; any other size serves the original."""
    cache = _art_cache_control(version, art_thumbs.art_version(src))
    wanted = art_thumbs.normalize_size(size)
    if wanted:
        key = art_thumbs.thumb_key(src, wanted)
        if key and lidarr_library.etag_matches(if_none_match, key[1]):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=_image_headers(cache, key[1]))
        made = art_thumbs.ensure_thumb(src, wanted, mediacover_service.base_dir / "mediacover" / "thumbs")
        if made:
            return FileResponse(
                str(made[0]), media_type="image/jpeg", headers=_image_headers(cache, made[1])
            )
    original = art_thumbs.original_etag(src)
    if original and lidarr_library.etag_matches(if_none_match, original):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=_image_headers(cache, original))
    return FileResponse(str(src), media_type=media_type, headers=_image_headers(cache, original))


def _library_sort_key(kind: str, sort_key: Optional[str]) -> str:
    return validate_sort_key(sort_key, paging.sort_keys(kind), paging.default_sort_key(kind))


def _library_filters(kind: str, artist_id: Optional[str], album_id: Optional[str]) -> dict[str, Optional[str]]:
    filters: dict[str, Optional[str]] = {}
    if kind in ("albums", "tracks"):
        filters["artist_id"] = artist_id
    if kind == "tracks":
        filters["album_id"] = album_id
    return filters


def _paged(
    kind: str,
    page: int,
    page_size: int,
    sort_key: Optional[str],
    sort_dir: str,
    q: Optional[str],
    monitored_only: bool,
    artist_id: Optional[str],
    album_id: Optional[str],
    db: Database,
    enrich: Callable[[Database, list[dict[str, Any]]], list[dict[str, Any]]],
) -> dict[str, Any]:
    key = _library_sort_key(kind, sort_key)
    rows, total = paging.page_library(
        db, kind, page, page_size, key, sort_dir, q, monitored_only, _library_filters(kind, artist_id, album_id)
    )
    return {
        "mode": "native",
        "page": page,
        "page_size": page_size,
        "total": total,
        "sort_key": key,
        "sort_dir": sort_dir,
        "records": enrich(db, rows) if rows else [],
    }


def _index(
    kind: str,
    sort_key: Optional[str],
    sort_dir: str,
    q: Optional[str],
    monitored_only: bool,
    artist_id: Optional[str],
    album_id: Optional[str],
    db: Database,
) -> dict[str, Any]:
    key = _library_sort_key(kind, sort_key)
    total, groups = paging.index_library(
        db, kind, key, sort_dir, q, monitored_only, _library_filters(kind, artist_id, album_id)
    )
    return {"sort_key": key, "sort_dir": sort_dir, "total": total, "groups": groups}


def _lidarr_paged(
    db: Database,
    kind: str,
    page: int,
    page_size: int,
    sort_key: Optional[str],
    sort_dir: str,
    q: Optional[str],
    monitored_only: bool,
    artist_id: Optional[str],
    client: Optional[LidarrClient],
) -> dict[str, Any]:
    key = _library_sort_key(kind, sort_key)
    lidarr = require_lidarr(client)
    records, total = _lidarr_fetch(
        lambda: lidarr_library.list_page(kind, lidarr, page, page_size, key, sort_dir, q, monitored_only, artist_id),
        "Library",
    )
    if kind == "artists":
        records = _with_discovery_ids(db, records)
    return {
        "mode": "lidarr",
        "page": page,
        "page_size": page_size,
        "total": total,
        "sort_key": key,
        "sort_dir": sort_dir,
        "records": records,
    }


def _lidarr_index(
    kind: str,
    sort_key: Optional[str],
    sort_dir: str,
    q: Optional[str],
    monitored_only: bool,
    artist_id: Optional[str],
    client: Optional[LidarrClient],
) -> dict[str, Any]:
    key = _library_sort_key(kind, sort_key)
    lidarr = require_lidarr(client)
    total, groups = _lidarr_fetch(
        lambda: lidarr_library.list_index(kind, lidarr, key, sort_dir, q, monitored_only, artist_id), "Library"
    )
    return {"mode": "lidarr", "sort_key": key, "sort_dir": sort_dir, "total": total, "groups": groups}


def _lidarr_album_tracks(album_id: Optional[str], client: Optional[LidarrClient]) -> list[lidarr_library.Row]:
    """Tracks of one album from Lidarr; Lidarr has no global track list, so ``album_id`` is required (else 409)."""
    if not album_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
    numeric = lidarr_numeric_id(album_id, "Album")
    lidarr = require_lidarr(client)
    return _lidarr_fetch(lambda: lidarr_library.album_track_rows(lidarr, numeric), "Album")


def _lidarr_tracks_paged(
    page: int,
    page_size: int,
    sort_key: Optional[str],
    sort_dir: str,
    q: Optional[str],
    monitored_only: bool,
    album_id: Optional[str],
    client: Optional[LidarrClient],
) -> dict[str, Any]:
    key = _library_sort_key("tracks", sort_key)
    rows = lidarr_library.filter_rows(_lidarr_album_tracks(album_id, client), q, monitored_only)
    return {
        "mode": "lidarr",
        "page": page,
        "page_size": page_size,
        "total": len(rows),
        "sort_key": key,
        "sort_dir": sort_dir,
        "records": lidarr_library.page_rows("tracks", rows, page, page_size, key, sort_dir),
    }


def _lidarr_tracks_index(
    sort_key: Optional[str], sort_dir: str, album_id: Optional[str], client: Optional[LidarrClient]
) -> dict[str, Any]:
    """No scrubber groups for tracks in Lidarr mode (an album's tracks are a short list)."""
    key = _library_sort_key("tracks", sort_key)
    total = len(_lidarr_album_tracks(album_id, client)) if album_id else 0
    return {"mode": "lidarr", "sort_key": key, "sort_dir": sort_dir, "total": total, "groups": []}


@router.get("/artists/paged", dependencies=[Depends(require_core_tier)])
def paged_artists(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """A page of library artists (with counts) plus the filtered total."""
    if _is_lidarr(db):
        return _lidarr_paged(db, "artists", page, page_size, sort_key, sort_dir, q, monitored_only, None, client)
    return _paged("artists", page, page_size, sort_key, sort_dir, q, monitored_only, None, None, db, _enrich_artists)


@router.get("/artists/index", dependencies=[Depends(require_core_tier)])
def artists_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Scrubber groups ``[{label, offset, count}]`` for the artists list, in its order."""
    if _is_lidarr(db):
        return _lidarr_index("artists", sort_key, sort_dir, q, monitored_only, None, client)
    return _index("artists", sort_key, sort_dir, q, monitored_only, None, None, db)


@router.get("/albums/paged", dependencies=[Depends(require_core_tier)])
def paged_albums(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """A page of library albums (with artist name and track count) plus the filtered total."""
    if _is_lidarr(db):
        return _lidarr_paged(db, "albums", page, page_size, sort_key, sort_dir, q, monitored_only, artist_id, client)
    return _paged("albums", page, page_size, sort_key, sort_dir, q, monitored_only, artist_id, None, db, _enrich_albums)


@router.get("/albums/index", dependencies=[Depends(require_core_tier)])
def albums_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Scrubber groups for the albums list, in its order."""
    if _is_lidarr(db):
        return _lidarr_index("albums", sort_key, sort_dir, q, monitored_only, artist_id, client)
    return _index("albums", sort_key, sort_dir, q, monitored_only, artist_id, None, db)


@router.get("/tracks/paged", dependencies=[Depends(require_core_tier)])
def paged_tracks(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    album_id: Optional[str] = None,
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
        return _lidarr_tracks_paged(page, page_size, sort_key, sort_dir, q, monitored_only, album_id, client)
    if album_id and page == 1 and not q and not monitored_only:
        hydrate_album_tracks(db, enricher, album_id)
    return _paged(
        "tracks", page, page_size, sort_key, sort_dir, q, monitored_only, artist_id, album_id, db, _enrich_tracks
    )


@router.get("/tracks/index", dependencies=[Depends(require_core_tier)])
def tracks_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=_SORT_DIR),
    q: Optional[str] = Query(None, max_length=200),
    monitored_only: bool = False,
    artist_id: Optional[str] = None,
    album_id: Optional[str] = None,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Scrubber groups for the tracks list, in its order."""
    if _is_lidarr(db):
        return _lidarr_tracks_index(sort_key, sort_dir, album_id, client)
    return _index("tracks", sort_key, sort_dir, q, monitored_only, artist_id, album_id, db)


def _with_discovery_ids(db: Database, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds ``discovery_id`` (None when unlinked) from the cached ``artist_links`` table; never touches the network."""
    ids = [str(r["id"]) for r in records if r.get("id") is not None]
    try:
        linked = db.get_discovery_ids_for_library_artists(ids)
    except sqlite3.Error as exc:
        logger.warning("Could not read cached artist links: %s", exc)
        linked = {}
    return [{**r, "discovery_id": linked.get(str(r.get("id")))} for r in records]


def _enrich_artists(db: Database, artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attaches album/track counts and a resolved image URL to library artists."""
    artist_ids = [a["id"] for a in artists]
    placeholders = ",".join("?" for _ in artist_ids)
    with db._lock:
        album_cur = db.conn.execute(
            f"SELECT artist_id, COUNT(*) FROM library_albums WHERE artist_id IN ({placeholders}) GROUP BY artist_id",
            artist_ids,
        )
        album_counts = dict(album_cur.fetchall())
        track_cur = db.conn.execute(
            f"SELECT artist_id, COUNT(*) FROM library_tracks WHERE artist_id IN ({placeholders}) GROUP BY artist_id",
            artist_ids,
        )
        track_counts = dict(track_cur.fetchall())
        cover_cur = db.conn.execute(
            f"SELECT artist_id, cover_url FROM library_albums WHERE artist_id IN ({placeholders}) AND cover_url IS NOT NULL AND cover_url != '' GROUP BY artist_id",
            artist_ids,
        )
        cover_urls = dict(cover_cur.fetchall())
    tag_ids = db.get_artist_tags_map(artist_ids)

    results: list[dict[str, Any]] = []
    for artist in artists:
        a_dict = dict(artist)
        a_dict["tags"] = tag_ids.get(str(artist["id"]), [])
        a_dict["album_count"] = album_counts.get(artist["id"], 0)
        a_dict["track_count"] = track_counts.get(artist["id"], 0)

        # Image resolution: parsed metadata_json image_url or first album cover_url
        img: Optional[str] = None
        if a_dict.get("metadata_json"):
            try:
                m = (
                    json.loads(a_dict["metadata_json"])
                    if isinstance(a_dict["metadata_json"], str)
                    else a_dict["metadata_json"]
                )
                if isinstance(m, dict):
                    img = (
                        m.get("image_url")
                        or m.get("picture_xl")
                        or m.get("picture_large")
                        or m.get("picture")
                    )
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        if not img:
            img = cover_urls.get(artist["id"])
        a_dict["image_url"] = _versioned_art_url("artist", artist["id"], a_dict.get("art_version"), img)
        results.append(a_dict)
    return _with_discovery_ids(db, results)


@router.get("/artists")
def list_artists(
    monitored_only: bool = False,
    query: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Lists library artists with optional filtering, search query, and pagination, attaching album and track counts."""
    artists = db.list_library_artists(
        monitored_only=monitored_only, query=query, limit=limit, offset=offset
    )
    if not artists:
        return []
    return _enrich_artists(db, artists)


def _ingest_artist_lidarr(db: Database, lidarr: LidarrClient, body: IngestArtistRequest) -> dict[str, Any]:
    results = lidarr.lookup_artist(body.artist_name)
    if not results:
        raise LidarrNotFound("Artist not found in Lidarr lookup")
    candidate = results[0]
    existing_id = int(candidate.get("id") or 0)
    added = False
    if not existing_id:
        search = bool(db.get_lidarr_settings().get("auto_search", True))
        created = lidarr.add_artist_with_defaults(candidate, whole_artist=True, search=search)
        existing_id = int(created.get("id") or 0)
        added = True
        lidarr_library.invalidate()
    return {
        "id": str(existing_id),
        "artist_name": str(candidate.get("artistName") or body.artist_name),
        "mode": "lidarr",
        "albums_ingested": 0,
        "tracks_ingested": 0,
        "already_existed": not added,
    }


def _rg_release_date(rg: dict[str, Any]) -> Optional[str]:
    """Release date of an enricher release group (``first_release_date``; legacy ``release_date`` accepted)."""
    return rg.get("first_release_date") or rg.get("release_date") or None


def _store_total_tracks(db: Database, album_id: str, count: Any, authoritative: bool = False) -> None:
    """Persists a provider-reported release track count on ``library_albums.total_tracks`` (ignored when unknown).

    Rule: the stored value only ever grows (``max(existing, new)``), because a provider may describe a shorter edition
    (Deezer's standard cut, a MusicBrainz first release without bonus discs) than the one already recorded. Pass
    ``authoritative=True`` only for a full MusicBrainz release with all media, which may replace the value outright.
    """
    n = _positive_int(count)
    if n is None:
        return
    db.set_library_album_total_tracks(album_id, n, authoritative=authoritative)


def _album_track_counts(db: Database, artist_id: str) -> tuple[dict[str, int], dict[str, int]]:
    """Per album of an artist: stored track rows, and rows that own at least one file."""
    with db._lock:
        rows = db.conn.execute(
            "SELECT t.album_id, COUNT(*), "
            "SUM(CASE WHEN EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) THEN 1 ELSE 0 END) "
            "FROM library_tracks t WHERE t.artist_id = ? GROUP BY t.album_id",
            (artist_id,),
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}, {r[0]: int(r[2] or 0) for r in rows}


def _queue_release_date_update(
    existing_alb: dict[str, Any], rg: dict[str, Any], upd_album: list[str], upd_params: list[Any]
) -> None:
    """Queues a backfill of an existing album's missing release date / year from a MusicBrainz release group."""
    rdate = _rg_release_date(rg)
    if rdate and not existing_alb.get("release_date"):
        upd_album.append("release_date = ?")
        upd_params.append(str(rdate))
    if rg.get("year") is not None and existing_alb.get("year") is None:
        upd_album.append("year = ?")
        upd_params.append(rg["year"])
    if _positive_int(rg.get("track_count")) is not None and not existing_alb.get("total_tracks"):
        upd_album.append("total_tracks = ?")
        upd_params.append(_positive_int(rg.get("track_count")))


def _run_in_background(target: Callable[[], None], name: str) -> None:
    """Runs ``target`` on a daemon thread (a seam tests replace to drive the job deterministically)."""
    threading.Thread(target=target, daemon=True, name=name).start()


def _deferred_profile_recompute_job(
    db: Database,
    enricher: MbidEnricherClient,
    discovery_client: Optional[DiscoveryClient],
    artist_id: str,
    artist_name: str,
) -> None:
    """Background half of a profile-bearing ingest: resolve the MBID, then run the normal artist refresh.

    The refresh persists MusicBrainz secondary types and (via ``Database.finish_pending_profile_recompute``) runs the
    deferred recompute once. Failures are logged and leave ``pending_profile_recompute`` set for the next refresh.
    """
    from plex_playlist_sync.artist_refresh_worker import artist_refresh_worker

    try:
        artist = db.get_library_artist(artist_id)
        if artist is None:
            return
        if not artist.get("mbid"):
            resolved = enricher.lookup_artist_mbid(artist_name)
            if not resolved:
                logger.warning("No MusicBrainz match for %s; profile recompute stays deferred", artist_name)
                return
            db.set_library_artist_mbid(artist_id, str(resolved))
        artist_refresh_worker.refresh_once(
            db=db, discovery_client=discovery_client, enricher=enricher, artist_ids=[artist_id]
        )
    except Exception as exc:  # background thread: nothing above us can handle it, so log the cause and keep the flag
        logger.warning(
            "Deferred metadata-profile recompute failed for %s (%s): %s", artist_name, artist_id, exc, exc_info=True
        )


def _defer_profile_recompute_after_ingest(
    db: Database,
    enricher: MbidEnricherClient,
    discovery_client: Optional[DiscoveryClient],
    artist_id: str,
    artist_name: str,
) -> None:
    """Makes a metadata profile effective for a freshly ingested artist without blocking the request.

    Discovery albums carry no secondary types, so everything would pass a studio-only profile. Ingest always sets
    ``pending_profile_recompute`` and queues a background MusicBrainz refresh that persists the types and recomputes
    once; a manual album/track edit before it finishes clears the flag and wins.
    """
    db.set_pending_profile_recompute(artist_id, True)
    _run_in_background(
        lambda: _deferred_profile_recompute_job(db, enricher, discovery_client, artist_id, artist_name),
        "ProfileRecomputeAfterIngest",
    )


@router.post("/artists/ingest", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def ingest_artist(
    body: IngestArtistRequest,
    db: Database = Depends(get_db),
    discovery_client: DiscoveryClient = Depends(get_discovery_client),
    _admin: dict[str, Any] = Depends(require_admin),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
) -> dict[str, Any]:
    """Ingests an artist discography from discovery metadata into the native catalog.

    In Lidarr mode the artist is added to Lidarr instead, with every default of the Lidarr root folder (profiles,
    monitoring, new-item monitoring, tags) and a search when auto-search is on; ``monitor_option``,
    ``quality_profile_id`` and ``monitored`` are ignored there. An artist Lidarr already has is never modified.
    """
    if _is_lidarr(db):
        lidarr = require_lidarr(lidarr_client)
        return _lidarr_mutation(db, lambda: _ingest_artist_lidarr(db, lidarr, body), "Artist")
    existing_artist = None
    if body.foreign_artist_id:
        existing_artist = db.get_library_artist_by_foreign_id(body.foreign_artist_id)
    if not existing_artist and body.artist_name:
        existing_artist = db.get_library_artist_by_name(body.artist_name)

    if existing_artist:
        res = dict(existing_artist)
        res["albums_ingested"] = 0
        res["tracks_ingested"] = 0
        res["already_existed"] = True
        return res

    mm = db.get_media_management_settings()
    root_folder_str = body.root_folder or mm.get("root_folder_path") or "/music"
    if body.root_folder:
        validate_media_path(body.root_folder, db=db)
    root_path = Path(root_folder_str).resolve()
    artist_folder = str(root_path / body.artist_name)

    monitor_option = body.monitor_option or str(mm.get("add_monitor_option") or DEFAULT_MONITOR_OPTION)
    metadata_profile_id: Optional[int] = (
        body.metadata_profile_id
        if "metadata_profile_id" in body.model_fields_set
        else mm.get("add_metadata_profile_id")
    )
    metadata_profile: Optional[dict[str, Any]] = None
    if metadata_profile_id is not None:
        metadata_profile = db.get_metadata_profile(metadata_profile_id)
        if metadata_profile is None:
            if "metadata_profile_id" in body.model_fields_set:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=f"Metadata profile {metadata_profile_id} not found"
                )
            metadata_profile_id = None
    artist_added_at = datetime.now(timezone.utc).date().isoformat()

    artist_id = str(uuid.uuid4())
    artist_dict = db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name=body.artist_name,
            clean_name=clean_library_name(body.artist_name),
            foreign_artist_id=body.foreign_artist_id,
            path=artist_folder,
            monitored=body.monitored,
            monitor_option=monitor_option,
            quality_profile_id=body.quality_profile_id,
            metadata_profile_id=metadata_profile_id,
        )
    )

    albums_ingested = 0
    tracks_ingested = 0

    artist_details: Optional[dict[str, Any]] = None
    try:
        artist_details = discovery_client.get_artist_details(body.foreign_artist_id)
    except Exception as exc:
        logger.warning(
            "Discovery client get_artist_details failed for %s: %s",
            body.foreign_artist_id,
            exc,
        )

    if artist_details:
        sections = [
            ("albums", artist_details.get("albums") or []),
            ("singles_eps", artist_details.get("singles_eps") or []),
            ("compilations", artist_details.get("compilations") or []),
        ]

        seen_album_ids: set[str] = set()
        for section_name, album_list in sections:
            for album in album_list:
                foreign_album_id = album.get("id")
                if foreign_album_id and foreign_album_id in seen_album_ids:
                    continue
                if foreign_album_id:
                    seen_album_ids.add(foreign_album_id)

                alb_monitored = album_monitored_for_option(
                    monitor_option,
                    artist_monitored=body.monitored,
                    album_type=section_to_album_type(section_name),
                    has_files=False,
                    release_date=album.get("release_date"),
                    year=album.get("year"),
                    artist_added_at=artist_added_at,
                    profile=metadata_profile,
                )
                album_title = album.get("title") or "Unknown Album"
                year_val: Optional[int] = None
                if album.get("year") is not None:
                    try:
                        year_val = int(album["year"])
                    except (ValueError, TypeError):
                        pass
                if year_val is None and album.get("release_date"):
                    rdate = str(album["release_date"]).strip()
                    if len(rdate) >= 4 and rdate[:4].isdigit():
                        year_val = int(rdate[:4])

                album_type = album.get("record_type") or (
                    "single" if section_name == "singles_eps" else (
                        "compilation" if section_name == "compilations" else "album"
                    )
                )

                existing_alb = None
                if foreign_album_id:
                    existing_alb = db.get_library_album_by_foreign_id(foreign_album_id)
                if not existing_alb:
                    existing_alb = db.get_library_album_by_title(artist_id, album_title)

                album_id = existing_alb["id"] if existing_alb else str(uuid.uuid4())
                album_path = str(Path(artist_folder) / album_title)

                db.upsert_library_album(
                    LibraryAlbum(
                        id=album_id,
                        artist_id=artist_id,
                        title=album_title,
                        clean_title=clean_library_name(album_title),
                        foreign_album_id=foreign_album_id,
                        release_date=album.get("release_date"),
                        year=year_val,
                        album_type=album_type,
                        monitored=alb_monitored,
                        path=album_path,
                        cover_url=album.get("cover_url"),
                        total_tracks=_positive_int(album.get("track_count")),
                    )
                )
                albums_ingested += 1

                if alb_monitored and foreign_album_id:
                    album_details = None
                    try:
                        time.sleep(0.01)
                        album_details = discovery_client.get_album_details(foreign_album_id)
                    except Exception as exc:
                        logger.warning(
                            "Discovery client get_album_details failed for %s: %s",
                            foreign_album_id,
                            exc,
                        )

                    if album_details:
                        _store_total_tracks(db, album_id, album_details.get("track_count"))
                    if album_details and isinstance(album_details.get("tracks"), list):
                        for trk in album_details["tracks"]:
                            foreign_track_id = trk.get("id")
                            existing_trk = None
                            if foreign_track_id:
                                existing_trk = db.get_library_track_by_foreign_id(
                                    foreign_track_id, album_id=album_id
                                )
                            if not existing_trk:
                                existing_trk = db.get_library_track_by_title(
                                    album_id,
                                    trk.get("title", ""),
                                    track_number=trk.get("track_number"),
                                )
                            track_id = existing_trk["id"] if existing_trk else str(uuid.uuid4())
                            trk_title = trk.get("title") or "Unknown Track"
                            trk_num = int(trk.get("track_number") or 1)
                            disc_num = int(trk.get("disc_number") or 1)
                            dur = (
                                float(trk["duration_seconds"])
                                if trk.get("duration_seconds") is not None
                                else None
                            )

                            db.upsert_library_track(
                                LibraryTrack(
                                    id=track_id,
                                    album_id=album_id,
                                    artist_id=artist_id,
                                    title=trk_title,
                                    clean_title=clean_library_name(trk_title),
                                    track_number=trk_num,
                                    disc_number=disc_num,
                                    duration_seconds=dur,
                                    monitored=(
                                        bool(existing_trk["monitored"])
                                        if existing_trk and monitor_option == "existing"
                                        else hydrated_track_monitored(monitor_option)
                                    ),
                                    foreign_track_id=foreign_track_id,
                                )
                            )
                            tracks_ingested += 1

    if metadata_profile is not None and body.monitored and monitor_option not in ("existing", "none") and albums_ingested:
        _defer_profile_recompute_after_ingest(db, enricher, discovery_client, artist_id, body.artist_name)

    if monitor_option == "existing":
        # Track-level "existing" on add: monitor exactly the tracks that already have files (none for a new artist,
        # but a re-added folder may already own some).
        db.bulk_edit_library_artists([artist_id], apply_monitor_to_albums=True)

    # Pre-cache the artist image and every album cover (throttled, background) so the first view is local.
    art_pipeline.schedule_precache(db, artist_id)

    result = db.get_library_artist(artist_id) or artist_dict
    return {
        **result,
        "albums_ingested": albums_ingested,
        "tracks_ingested": tracks_ingested,
    }


@router.get("/artists/{artist_id}", dependencies=[Depends(require_core_tier)])
def get_artist(
    artist_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves a single artist by ID, including its child albums and image_url."""
    if _is_lidarr(db):
        numeric = lidarr_numeric_id(artist_id, "Artist")
        lidarr = require_lidarr(client)
        detail = _lidarr_fetch(lambda: lidarr_library.artist_detail(lidarr, numeric), "Artist")
        return _with_discovery_ids(db, [detail])[0]
    artist = db.get_library_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")
    result = dict(artist)
    result["mbid"] = artist.get("mbid")
    result["banner_url"] = artist.get("banner_url")
    result["bio"] = artist.get("bio")
    result["genres"] = artist.get("genres")
    result["country"] = artist.get("country")
    albums = db.list_library_albums(artist_id=artist_id, limit=500)
    profile = db.get_metadata_profile(artist["metadata_profile_id"]) if artist.get("metadata_profile_id") else None
    stored, with_files = _album_track_counts(db, artist_id)
    for alb in albums:
        # Informational only: albums outside the profile stay in the catalog and can be monitored manually.
        alb["in_profile"] = album_in_metadata_profile(profile, alb.get("album_type"), alb.get("secondary_types"))
        alb["cover_url"] = _versioned_art_url("album", alb["id"], alb.get("art_version"), alb.get("cover_url"))
        # Release track count (provider-reported, else the stored rows) and how many tracks own a file.
        alb["track_count"] = alb.get("total_tracks") or stored.get(alb["id"], 0)
        alb["track_file_count"] = with_files.get(alb["id"], 0)
    result["metadata_profile_id"] = profile["id"] if profile else None
    result["tags"] = db.get_artist_tag_ids(artist_id)
    result["albums"] = albums

    img: Optional[str] = artist.get("image_url")
    if not img and result.get("metadata_json"):
        try:
            m = (
                json.loads(result["metadata_json"])
                if isinstance(result["metadata_json"], str)
                else result["metadata_json"]
            )
            if isinstance(m, dict):
                img = (
                    m.get("image_url")
                    or m.get("picture_xl")
                    or m.get("picture_large")
                    or m.get("picture")
                )
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    if not img:
        for alb in albums:
            if alb.get("cover_url"):
                img = alb["cover_url"]
                break
    result["image_url"] = _versioned_art_url("artist", artist_id, artist.get("art_version"), img)
    return _with_discovery_ids(db, [result])[0]


@router.get("/artists/{artist_id}/image", dependencies=[Depends(require_core_tier)])
def get_artist_image(
    artist_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    if_none_match: Optional[str] = Header(None),
    size: Optional[int] = Query(None, description="Thumbnail size: 250 or 500; anything else serves the original"),
    v: Optional[str] = Query(None, description="Art version token; the response is immutable only when it equals the served file's version"),
    _admin: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Serves local artist artwork or redirects to remote image / first album cover / placeholder."""
    if _is_lidarr(db):
        return _lidarr_image("artists", "artist", artist_id, "Artist", ("poster", "cover"), client, if_none_match, size, v)
    artist = db.get_library_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    served = art_pipeline.serve_art(db, "artist", artist, db.get_media_management_settings())
    if served is not None:
        return _native_art(served, _art_media_type(served), size, if_none_match, v)

    # Else if artist has image_url (http/https), return RedirectResponse
    image_url = artist.get("image_url")
    if image_url and (image_url.startswith("http://") or image_url.startswith("https://")):
        return RedirectResponse(url=image_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Else check if artist's first album has a cover and redirect to /api/library/albums/{first_album_id}/cover
    albums = db.list_library_albums(artist_id=artist_id, limit=10)
    for alb in albums:
        alb_id = alb.get("id")
        if alb_id:
            has_cover = False
            cov = alb.get("cover_url")
            if cov and (cov.startswith("http://") or cov.startswith("https://") or "/cover" in cov):
                has_cover = True
            elif alb.get("path"):
                try:
                    p = validate_media_path(alb["path"], db=db)
                    if find_folder_art(p) is not None:
                        has_cover = True
                except Exception:
                    pass
            if has_cover:
                return RedirectResponse(
                    url=f"/api/library/albums/{alb_id}/cover",
                    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                )

    return RedirectResponse(url="/placeholder.svg", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/artists/{artist_id}/banner", dependencies=[Depends(require_core_tier)])
def get_artist_banner(
    artist_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    if_none_match: Optional[str] = Header(None),
    _admin: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Serves cached or local artist banner artwork."""
    if _is_lidarr(db):
        return _lidarr_image("artists", "artist", artist_id, "Artist", ("banner",), client, if_none_match)
    artist = db.get_library_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    artist_path_str = artist.get("path")
    if artist_path_str:
        try:
            validated = validate_media_path(artist_path_str, db=db)
            if validated.is_dir():
                for cand_name in ("banner.jpg", "banner.png", "artist-banner.jpg"):
                    cand = validated / cand_name
                    if cand.is_file():
                        media_type = "image/png" if cand_name.endswith(".png") else "image/jpeg"
                        return _native_art(cand, media_type, None, if_none_match)
        except Exception as exc:
            logger.debug("Failed validating artist banner path '%s': %s", artist_path_str, exc)

    cached_banner = mediacover_service.ensure_artwork("artist_banner", artist_id, artist.get("banner_url"))
    if cached_banner and cached_banner.is_file() and cached_banner.stat().st_size > 0:
        return _native_art(cached_banner, "image/jpeg", None, if_none_match)

    banner_url = artist.get("banner_url")
    if banner_url and (banner_url.startswith("http://") or banner_url.startswith("https://")):
        return RedirectResponse(url=banner_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    return RedirectResponse(url="/placeholder.svg", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.put("/artists/{artist_id}/monitored", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def set_artist_monitored(
    artist_id: str,
    body: ArtistMonitoredRequest,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Updates monitoring status for an artist, optionally cascading to albums and tracks or applying a preset."""
    if _is_lidarr(db):
        if "metadata_profile_id" in body.model_fields_set:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
        numeric = lidarr_numeric_id(artist_id, "Artist")
        lidarr = require_lidarr(client)
        preset = body.monitor_option
        if preset is not None:
            try:
                return _lidarr_mutation(
                    db, lambda: lidarr_library.apply_monitor_preset(lidarr, numeric, preset), "Artist"
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        return _lidarr_mutation(
            db, lambda: lidarr_library.set_artist_monitored(lidarr, numeric, body.monitored, body.cascade_children),
            "Artist"
        )
    artist = db.get_library_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    profile_given = "metadata_profile_id" in body.model_fields_set
    if body.monitor_option is not None or profile_given:
        # Same set-based rules as bulk edit: the artist's option / profile and monitored flag are written, then
        # every album is recomputed from them and its tracks follow the album.
        apply_recompute = (
            True
            if body.monitor_option is not None and body.apply_monitor_to_albums is None
            else bool(body.apply_monitor_to_albums)
        )
        # A profile-only edit still honours ``monitored``: a recompute folds it in (an unmonitored artist unmonitors
        # its children); without one, albums stay as they are unless the flag actually changes and cascades.
        if body.monitor_option is not None:
            monitored_value: Optional[bool] = body.monitor_option != "none"
        elif apply_recompute:
            monitored_value = body.monitored
        else:
            monitored_value = None
        try:
            db.bulk_edit_library_artists(
                [str(artist_id)],
                monitored=monitored_value,
                monitor_option=body.monitor_option,
                metadata_profile_id=body.metadata_profile_id if profile_given else Database._UNSET,
                apply_monitor_to_albums=apply_recompute,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if (
            body.monitor_option is None
            and not apply_recompute
            and body.monitored != bool(artist.get("monitored", True))
        ):
            db.set_artist_monitored(artist_id=artist_id, monitored=body.monitored, cascade_children=body.cascade_children)
    else:
        db.set_artist_monitored(
            artist_id=artist_id,
            monitored=body.monitored,
            cascade_children=body.cascade_children,
        )

    updated = db.get_library_artist(artist_id)
    return updated or {}


@router.put(
    "/artists/{artist_id}/tags",
    response_model=ArtistTagsResponse,
    dependencies=[Depends(require_core_tier), Depends(track_admin_actor)],
)
def set_artist_tags(
    artist_id: str,
    body: ArtistTagsRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> ArtistTagsResponse:
    """Replaces an artist's tags with ``tags`` (ids from ``/api/tags``). Native library only."""
    try:
        result = db.set_artist_tags(artist_id, body.tags)
    except UnknownTag as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")
    return ArtistTagsResponse(artist_id=str(artist_id), tags=result)


@router.post("/artists/bulk-edit", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def bulk_edit_artists(
    body: ArtistBulkEditRequest,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, int]:
    """Bulk-edits many artists (selected by ``artist_ids`` or ``all``) in one set-based transaction.

    Native mode: ``monitored``, ``monitor_option`` and ``quality_profile_id`` are written when given;
    ``apply_monitor_to_albums`` then recomputes every affected album (and its tracks) from the artist's resulting
    option. Lidarr mode: ``monitored``/``quality_profile_id`` via ``artist/editor`` and the presets
    all/albums/singles_eps/none per artist.
    """
    has_ids = bool(body.artist_ids)
    if has_ids == bool(body.all):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of a non-empty artist_ids or all=true",
        )
    profile_given = "quality_profile_id" in body.model_fields_set
    release_given = "metadata_profile_id" in body.model_fields_set
    tags_given = bool(body.add_tags or body.remove_tags)
    if (
        body.monitored is None and body.monitor_option is None and not profile_given and not release_given
        and not tags_given
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one of monitored, monitor_option, quality_profile_id, metadata_profile_id, "
            "add_tags or remove_tags",
        )
    if (release_given or tags_given) and _is_lidarr(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NATIVE_ONLY_DETAIL)
    # Omitted: unmonitoring cascades, and switching to "existing" recomputes (track-level: only tracks with files) --
    # but only the artists whose option actually changes; artists already on "existing" are left alone unless the
    # flag is explicitly true.
    apply_to_albums = (
        body.apply_monitor_to_albums if body.apply_monitor_to_albums is not None else body.monitored is False
    )
    recompute_on_change = body.apply_monitor_to_albums is None and body.monitor_option == "existing"
    if _is_lidarr(db):
        lidarr = require_lidarr(client)
        numeric_ids = [lidarr_numeric_id(i, "Artist") for i in body.artist_ids] if has_ids else None
        profile_id: Optional[int] = None
        if profile_given:
            if body.quality_profile_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="quality_profile_id cannot be cleared in Lidarr mode",
                )
            profile_id = lidarr_numeric_id(body.quality_profile_id, "Quality profile")
        try:
            return _lidarr_mutation(
                db,
                lambda: lidarr_library.bulk_edit_artists(
                    lidarr, numeric_ids, body.monitored, body.monitor_option, profile_id,
                    apply_to_albums or recompute_on_change,
                ),
                "Artist",
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        return db.bulk_edit_library_artists(
            body.artist_ids if has_ids else None,
            monitored=body.monitored,
            monitor_option=body.monitor_option,
            quality_profile_id=body.quality_profile_id if profile_given else Database._UNSET,
            apply_monitor_to_albums=apply_to_albums,
            metadata_profile_id=body.metadata_profile_id if release_given else Database._UNSET,
            recompute_when_option_changes=recompute_on_change,
            add_tag_ids=body.add_tags,
            remove_tag_ids=body.remove_tags,
        )
    except ValueError as exc:  # includes tag_store.UnknownTag
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


_NATIVE_ADMIN = [Depends(require_core_tier), Depends(native_only)]


# Deprecated alias (pre-v48 name), kept for one release.
@router.get("/release-profiles", dependencies=_NATIVE_ADMIN, deprecated=True)
@router.get("/metadata-profiles", dependencies=_NATIVE_ADMIN)
def list_metadata_profiles(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Native metadata profiles with ``artist_count`` (artists using each) and the default for newly added artists."""
    return {
        "profiles": db.list_metadata_profiles(),
        "default_profile_id": db.get_media_management_settings().get("add_metadata_profile_id"),
        "primary_types": list(RELEASE_PRIMARY_TYPES),
        "secondary_types": list(RELEASE_SECONDARY_TYPES),
    }


# Deprecated alias (pre-v48 name), kept for one release.
@router.post("/release-profiles", dependencies=_NATIVE_ADMIN, status_code=status.HTTP_201_CREATED, deprecated=True)
@router.post("/metadata-profiles", dependencies=_NATIVE_ADMIN, status_code=status.HTTP_201_CREATED)
def create_metadata_profile(body: MetadataProfileBody, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return {**db.create_metadata_profile(body.name, body.primary_types, body.secondary_types), "artist_count": 0}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# Deprecated alias (pre-v48 name), kept for one release.
@router.put("/release-profiles/{profile_id}", dependencies=_NATIVE_ADMIN, deprecated=True)
@router.put("/metadata-profiles/{profile_id}", dependencies=_NATIVE_ADMIN)
def update_metadata_profile(profile_id: int, body: MetadataProfileBody, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Edits a profile. Existing monitoring is untouched until an artist's albums are recomputed."""
    try:
        updated = db.update_metadata_profile(profile_id, body.name, body.primary_types, body.secondary_types)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata profile not found")
    count = next((p["artist_count"] for p in db.list_metadata_profiles() if p["id"] == profile_id), 0)
    return {**updated, "artist_count": count}


# Deprecated alias (pre-v48 name), kept for one release.
@router.delete("/release-profiles/{profile_id}", dependencies=_NATIVE_ADMIN, deprecated=True)
@router.delete("/metadata-profiles/{profile_id}", dependencies=_NATIVE_ADMIN)
def delete_metadata_profile(profile_id: int, db: Database = Depends(get_db)) -> dict[str, int]:
    """Deletes a profile; artists using it fall back to no profile (their albums keep their monitored flags)."""
    cleared = db.delete_metadata_profile(profile_id)
    if cleared is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata profile not found")
    return {"deleted": 1, "artists_cleared": cleared}


# Deprecated alias (pre-v48 name), kept for one release.
@router.get("/artists/{artist_id}/release-profile-preview", dependencies=_NATIVE_ADMIN, deprecated=True)
@router.get("/artists/{artist_id}/metadata-profile-preview", dependencies=_NATIVE_ADMIN)
def preview_metadata_profile(
    artist_id: str, profile_id: Optional[int] = None, db: Database = Depends(get_db)
) -> dict[str, Any]:
    """What a metadata profile would do for an artist; ``profile_id`` omitted/null previews clearing the profile.

    Returns ``{matching, total, would_change}`` where ``would_change`` is ``{albums_to_monitor, albums_to_unmonitor,
    tracks_to_monitor, tracks_to_unmonitor}``: the effect of a recompute (``apply_monitor_to_albums``) against the
    current flags, computed with the same predicate as the bulk edit, without writing anything.
    """
    result = db.metadata_profile_preview(artist_id, profile_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist or metadata profile not found")
    return result


def reconcile_artist_files(db: Database, artist_id: str) -> int:
    """Matches unlinked or unassigned library_files in artist folder to canonical library_tracks."""
    artist = db.get_library_artist(artist_id)
    if not artist:
        return 0

    reconciled_count = 0
    candidate_tracks = db.list_library_tracks(artist_id=artist_id, limit=5000)
    if not candidate_tracks:
        return 0

    artist_path_str = artist.get("path")
    if not artist_path_str:
        return 0

    artist_path = Path(artist_path_str)
    if not artist_path.is_dir():
        return 0

    # 1. Look for orphaned library_files (files where track_id doesn't exist)
    with db._lock:
        cur = db.conn.execute(
            """
            SELECT f.* FROM library_files f
            LEFT JOIN library_tracks t ON f.track_id = t.id
            WHERE t.id IS NULL AND f.file_path LIKE ?
            """,
            (f"{artist_path_str}%",),
        )
        orphaned_files = [dict(r) for r in cur.fetchall()]

    for f in orphaned_files:
        fpath = Path(f["file_path"])
        if not fpath.is_file():
            continue
        try:
            meta = inspect_audio_file(fpath)
            matched = reconcile_audio_file_to_track(meta, candidate_tracks)
            if matched:
                with db._lock:
                    db.conn.execute(
                        "UPDATE library_files SET track_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (matched["id"], f["id"]),
                    )
                    db.conn.commit()
                reconciled_count += 1
        except Exception as exc:
            logger.debug("reconcile_artist_files: Failed matching orphaned file %s: %s", fpath, exc)

    # 2. Check tracks that have no library_file, and look for matching audio files on disk
    tracks_without_files = [
        t for t in candidate_tracks if db.get_library_file_for_track(t["id"]) is None
    ]
    if not tracks_without_files:
        return reconciled_count

    try:
        for audio_ext in AUDIO_EXTENSIONS:
            for disk_file in artist_path.rglob(f"*{audio_ext}"):
                if is_excluded_entry(disk_file, artist_path, []) or not disk_file.is_file():
                    continue
                existing_f = db.get_library_file_by_path(str(disk_file))
                if existing_f and db.get_library_track(existing_f["track_id"]):
                    continue

                try:
                    meta = inspect_audio_file(disk_file)
                    matched = reconcile_audio_file_to_track(meta, tracks_without_files)
                    if matched:
                        rel_path = (
                            str(disk_file.relative_to(artist_path.parent))
                            if artist_path.parent != artist_path
                            else disk_file.name
                        )
                        file_id = str(existing_f["id"]) if existing_f else str(uuid.uuid4())
                        file_size = 0
                        try:
                            file_size = disk_file.stat().st_size
                        except OSError:
                            pass

                        db.upsert_library_file(
                            LibraryFile(
                                id=file_id,
                                track_id=matched["id"],
                                file_path=str(disk_file),
                                relative_path=rel_path,
                                codec=str(meta.get("codec") or disk_file.suffix.lstrip(".").upper() or "UNKNOWN"),
                                bitrate=meta.get("bitrate"),
                                sample_rate=meta.get("sample_rate"),
                                bits_per_sample=meta.get("bits_per_sample"),
                                quality_name=str(meta.get("quality_full") or "Unknown"),
                                size_bytes=file_size,
                                cutoff_met=True,
                            )
                        )
                        tracks_without_files = [t for t in tracks_without_files if t["id"] != matched["id"]]
                        reconciled_count += 1
                except Exception as exc:
                    logger.debug("reconcile_artist_files: Failed inspecting disk file %s: %s", disk_file, exc)
    except Exception as exc:
        logger.debug("reconcile_artist_files: Error traversing artist directory %s: %s", artist_path, exc)

    return reconciled_count


def refresh_single_artist(
    artist_id: str,
    db: Database,
    discovery_client: Optional[DiscoveryClient] = None,
    enricher: Optional[MbidEnricherClient] = None,
) -> dict[str, Any]:
    """Refreshes artist metadata, canonical discography, full tracklist hydration, artwork, and file reconciliation."""
    if enricher is None:
        enricher = MbidEnricherClient()
    if discovery_client is None:
        discovery_client = DiscoveryClient()

    artist = db.get_library_artist(artist_id)
    if artist is None:
        return {"success": False, "message": "Artist not found", "artist_id": artist_id}

    artist_name = str(artist.get("name") or "").strip()
    if is_system_folder_name(artist_name):
        logger.info("Skipping metadata refresh for artist %r (%s): name is an OS/NAS system or trash folder", artist_name, artist_id)
        return {"success": False, "message": "Artist is a system folder; skipped", "artist_id": artist_id}
    foreign_artist_id = artist.get("foreign_artist_id")
    # Optional metadata profile: shapes only the monitored flag of albums created by this refresh.
    metadata_profile = db.get_metadata_profile(artist["metadata_profile_id"]) if artist.get("metadata_profile_id") else None

    # 1. Enrich with MusicBrainz metadata and discography
    mbid = artist.get("mbid")
    if not mbid and not foreign_artist_id and artist_name:
        try:
            mbid = enricher.lookup_artist_mbid(artist_name)
            if mbid:
                with db._lock:
                    db.conn.execute(
                        "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (mbid, artist_id),
                    )
                    db.conn.commit()
                artist["mbid"] = mbid
        except Exception as exc:
            logger.warning("Error looking up artist MBID for %s: %s", artist_name, exc)

    mb_discography_found = False
    if mbid:
        try:
            mb_details = enricher.get_artist_details(mbid)
            if mb_details:
                country = mb_details.get("country")
                genres_raw = mb_details.get("genres")
                genres_str = (
                    ", ".join(genres_raw)
                    if isinstance(genres_raw, (list, tuple))
                    else (str(genres_raw) if genres_raw else None)
                )
                bio = mb_details.get("bio") or mb_details.get("disambiguation")

                art_updates: list[str] = []
                art_params: list[Any] = []
                if country and not artist.get("country"):
                    art_updates.append("country = ?")
                    art_params.append(country)
                    artist["country"] = country

                if genres_str and not artist.get("genres"):
                    art_updates.append("genres = ?")
                    art_params.append(genres_str)
                    artist["genres"] = genres_str

                if bio and not artist.get("bio"):
                    art_updates.append("bio = ?")
                    art_params.append(bio)
                    artist["bio"] = bio

                if art_updates:
                    art_updates.append("updated_at = CURRENT_TIMESTAMP")
                    sql = f"UPDATE library_artists SET {', '.join(art_updates)} WHERE id = ?"
                    art_params.append(artist_id)
                    with db._lock:
                        db.conn.execute(sql, art_params)
                        db.conn.commit()

            discography = enricher.get_artist_discography(mbid)
            if discography and len(discography) > 0:
                mb_discography_found = True
                artist_path = artist.get("path")
                monitor_opt = artist.get("monitor_option", "all")
                for rg in discography:
                    rg_id = rg.get("id")
                    title = rg.get("title") or "Unknown Album"
                    album_type = rg.get("album_type", "album")

                    alb_monitored = album_monitored_for_option(
                        monitor_opt,
                        artist_monitored=bool(artist.get("monitored", True)),
                        album_type=album_type,
                        has_files=False,
                        release_date=_rg_release_date(rg),
                        year=rg.get("year"),
                        artist_added_at=artist.get("created_at"),
                        profile=metadata_profile,
                        secondary_types=rg.get("secondary_types"),
                    )

                    existing_alb = None
                    if rg_id:
                        existing_alb = db.get_library_album_by_release_group_id(rg_id)
                    if not existing_alb:
                        existing_alb = db.get_library_album_by_title(artist_id, title)

                    if existing_alb:
                        album_id = existing_alb["id"]
                        alb_monitored = bool(existing_alb["monitored"])
                        upd_album: list[str] = []
                        upd_params: list[Any] = []
                        if rg_id and existing_alb.get("mb_release_group_id") != rg_id:
                            upd_album.append("mb_release_group_id = ?")
                            upd_params.append(rg_id)
                        if not existing_alb.get("cover_url") and rg.get("cover_url"):
                            upd_album.append("cover_url = ?")
                            upd_params.append(rg["cover_url"])
                        _queue_release_date_update(existing_alb, rg, upd_album, upd_params)
                        # Backfill/refresh the MusicBrainz secondary types; never touches the monitored flag.
                        if normalize_secondary_types(rg.get("secondary_types")) is not None and normalize_secondary_types(
                            rg["secondary_types"]
                        ) != existing_alb.get("secondary_types"):
                            upd_album.append("secondary_types = ?")
                            upd_params.append(json.dumps(normalize_secondary_types(rg["secondary_types"])))
                        if upd_album:
                            upd_album.append("updated_at = CURRENT_TIMESTAMP")
                            alb_sql = f"UPDATE library_albums SET {', '.join(upd_album)} WHERE id = ?"
                            upd_params.append(album_id)
                            with db._lock:
                                db.conn.execute(alb_sql, upd_params)
                                db.conn.commit()
                    else:
                        album_id = str(uuid.uuid4())
                        alb_path = str(Path(artist_path) / title) if artist_path else None
                        db.upsert_library_album(
                            LibraryAlbum(
                                id=album_id,
                                artist_id=artist_id,
                                title=title,
                                clean_title=clean_library_name(title),
                                mb_release_group_id=rg_id,
                                album_type=album_type,
                                release_date=_rg_release_date(rg),
                                year=rg.get("year"),
                                cover_url=rg.get("cover_url"),
                                secondary_types=rg.get("secondary_types"),
                                monitored=alb_monitored,
                                path=alb_path,
                                total_tracks=_positive_int(rg.get("track_count")),
                            )
                        )

                    # Cache cover artwork via mediacover
                    cov_url = rg.get("cover_url") or (existing_alb.get("cover_url") if existing_alb else None)
                    if cov_url:
                        try:
                            mediacover_service.ensure_artwork("album_cover", album_id, cov_url)
                        except Exception as c_err:
                            logger.debug("Error caching cover for album %s: %s", album_id, c_err)

                    # Track hydration: If album is monitored, hydrate canonical tracks
                    if alb_monitored and rg_id:
                        tracks = enricher.get_release_group_tracks(rg_id)
                        if not tracks and discovery_client:
                            # Fallback to Deezer album search/details for that album title
                            try:
                                dz_results = discovery_client.search(f"{artist_name} {title}", item_type="album", limit=3)
                                if isinstance(dz_results, list):
                                    for dz_item in dz_results:
                                        if clean_library_name(dz_item.get("title") or "") == clean_library_name(title):
                                            dz_alb_details = discovery_client.get_album_details(dz_item["id"])
                                            if dz_alb_details and dz_alb_details.get("tracks"):
                                                tracks = [
                                                    {
                                                        "track_number": int(t.get("track_number") or 1),
                                                        "disc_number": int(t.get("disc_number") or 1),
                                                        "title": t.get("title") or "Unknown Track",
                                                        "duration_seconds": float(t["duration_seconds"]) if t.get("duration_seconds") is not None else None,
                                                        "mb_recording_id": None,
                                                    }
                                                    for t in dz_alb_details["tracks"]
                                                ]
                                                if dz_alb_details.get("cover_url") and not rg.get("cover_url"):
                                                    mediacover_service.ensure_artwork("album_cover", album_id, dz_alb_details["cover_url"])
                                                break
                            except Exception as dz_err:
                                logger.debug("Deezer track fallback failed for %s - %s: %s", artist_name, title, dz_err)

                        if tracks:
                            _store_total_tracks(db, album_id, len(tracks))
                            with album_hydration_lock(album_id):
                                for trk in tracks:
                                    trk_title = trk.get("title") or "Unknown Track"
                                    trk_num = int(trk.get("track_number") or 1)
                                    disc_num = int(trk.get("disc_number") or 1)
                                    dur = trk.get("duration_seconds")
                                    mb_rec_id = trk.get("mb_recording_id")

                                    existing_trk = db.get_library_track_by_title(
                                        album_id,
                                        trk_title,
                                        track_number=trk_num,
                                    )
                                    if existing_trk:
                                        trk_id = existing_trk["id"]
                                        t_monitored = bool(existing_trk["monitored"])
                                    else:
                                        trk_id = str(uuid.uuid4())
                                        t_monitored = hydrated_track_monitored(monitor_opt)

                                    db.upsert_library_track(
                                        LibraryTrack(
                                            id=trk_id,
                                            album_id=album_id,
                                            artist_id=artist_id,
                                            title=trk_title,
                                            clean_title=clean_library_name(trk_title),
                                            track_number=trk_num,
                                            disc_number=disc_num,
                                            duration_seconds=dur,
                                            monitored=t_monitored,
                                            mb_recording_id=mb_rec_id,
                                        )
                                    )
        except Exception as exc:
            logger.warning("Error enriching artist %s via MusicBrainz: %s", artist_id, exc)
        else:
            db.finish_pending_profile_recompute(artist_id)

    # 2. Retrieve discography and artwork from Deezer only if MusicBrainz discography was not found
    if not mb_discography_found:
        if not foreign_artist_id and artist_name:
            try:
                d_art = discovery_client.search_artist(artist_name)
                if not isinstance(d_art, dict):
                    d_art = None
                if not d_art:
                    search_results = discovery_client.search(artist_name, item_type="all", limit=5)
                    if isinstance(search_results, list):
                        for item in search_results:
                            if isinstance(item, dict):
                                item_art = (item.get("artist") or "").lower().strip()
                                if item.get("item_type") == "artist" or item_art == artist_name.lower():
                                    item_id = item.get("id")
                                    if item_id and isinstance(item_id, (str, int)):
                                        d_art = {
                                            "id": str(item_id),
                                            "name": str(item.get("artist") or item.get("title") or ""),
                                            "image_url": str(item.get("cover_url")) if item.get("cover_url") else None,
                                        }
                                        break
                if isinstance(d_art, dict) and d_art.get("id") and isinstance(d_art["id"], (str, int)):
                    foreign_artist_id = str(d_art["id"])
                    artist["foreign_artist_id"] = foreign_artist_id
                    art_upd = ["foreign_artist_id = ?"]
                    art_params = [foreign_artist_id]
                    if d_art.get("image_url") and not artist.get("image_url") and isinstance(d_art["image_url"], str):
                        art_upd.append("image_url = ?")
                        art_params.append(d_art["image_url"])
                        artist["image_url"] = d_art["image_url"]
                    if d_art.get("banner_url") and not artist.get("banner_url") and isinstance(d_art["banner_url"], str):
                        art_upd.append("banner_url = ?")
                        art_params.append(d_art["banner_url"])
                        artist["banner_url"] = d_art["banner_url"]
                    art_upd.append("updated_at = CURRENT_TIMESTAMP")
                    art_params.append(artist_id)
                    with db._lock:
                        db.conn.execute(
                            f"UPDATE library_artists SET {', '.join(art_upd)} WHERE id = ?",
                            art_params,
                        )
                        db.conn.commit()
            except Exception as exc:
                logger.warning("Error resolving Deezer artist ID for '%s': %s", artist_name, exc)

        if foreign_artist_id:
            try:
                artist_details = discovery_client.get_artist_details(foreign_artist_id)
                if artist_details:
                    d_img = (
                        artist_details.get("image_url")
                        or artist_details.get("picture_xl")
                        or artist_details.get("picture_big")
                    )
                    d_banner = (
                        artist_details.get("banner_url")
                        or artist_details.get("picture_xl")
                    )
                    art_upd = []
                    art_params = []
                    if d_img and not artist.get("image_url"):
                        art_upd.append("image_url = ?")
                        art_params.append(d_img)
                        artist["image_url"] = d_img
                    if d_banner and not artist.get("banner_url"):
                        art_upd.append("banner_url = ?")
                        art_params.append(d_banner)
                        artist["banner_url"] = d_banner
                    if art_upd:
                        art_upd.append("updated_at = CURRENT_TIMESTAMP")
                        art_params.append(artist_id)
                        with db._lock:
                            db.conn.execute(
                                f"UPDATE library_artists SET {', '.join(art_upd)} WHERE id = ?",
                                art_params,
                            )
                            db.conn.commit()

                    sections = [
                        ("albums", artist_details.get("albums") or []),
                        ("singles_eps", artist_details.get("singles_eps") or []),
                        ("compilations", artist_details.get("compilations") or []),
                    ]
                    artist_path = artist.get("path")
                    seen_album_ids: set[str] = set()

                    for section_name, album_list in sections:
                        for album in album_list:
                            foreign_album_id = album.get("id")
                            if foreign_album_id and foreign_album_id in seen_album_ids:
                                continue
                            if foreign_album_id:
                                seen_album_ids.add(foreign_album_id)

                            album_title = album.get("title") or "Unknown Album"
                            existing_alb = None
                            if foreign_album_id:
                                existing_alb = db.get_library_album_by_foreign_id(foreign_album_id)
                            if not existing_alb:
                                existing_alb = db.get_library_album_by_title(artist_id, album_title)

                            year_val: Optional[int] = None
                            if album.get("year") is not None:
                                try:
                                    year_val = int(album["year"])
                                except (ValueError, TypeError):
                                    pass
                            if year_val is None and album.get("release_date"):
                                rdate = str(album["release_date"]).strip()
                                if len(rdate) >= 4 and rdate[:4].isdigit():
                                    year_val = int(rdate[:4])

                            album_type = album.get("record_type") or (
                                "single" if section_name == "singles_eps" else (
                                    "compilation" if section_name == "compilations" else "album"
                                )
                            )

                            if existing_alb:
                                album_id = existing_alb["id"]
                                alb_monitored = bool(existing_alb["monitored"])
                                alb_path = existing_alb.get("path") or (
                                    str(Path(artist_path) / album_title) if artist_path else None
                                )
                                upd_cov = existing_alb.get("cover_url") or album.get("cover_url")
                                db.upsert_library_album(
                                    LibraryAlbum(
                                        id=album_id,
                                        artist_id=artist_id,
                                        title=album_title,
                                        clean_title=clean_library_name(album_title),
                                        foreign_album_id=foreign_album_id,
                                        release_date=album.get("release_date") or existing_alb.get("release_date"),
                                        year=year_val or existing_alb.get("year"),
                                        album_type=album_type,
                                        monitored=alb_monitored,
                                        path=alb_path,
                                        cover_url=upd_cov,
                                        mb_release_group_id=existing_alb.get("mb_release_group_id"),
                                        mb_release_id=existing_alb.get("mb_release_id"),
                                        total_tracks=_positive_int(album.get("track_count")),
                                    )
                                )
                            else:
                                album_id = str(uuid.uuid4())
                                monitor_opt = artist.get("monitor_option", "all")
                                alb_monitored = album_monitored_for_option(
                                    monitor_opt,
                                    artist_monitored=bool(artist.get("monitored", True)),
                                    album_type=section_to_album_type(section_name),
                                    has_files=False,
                                    release_date=album.get("release_date"),
                                    year=year_val,
                                    artist_added_at=artist.get("created_at"),
                                    profile=metadata_profile,
                                )

                                alb_path = str(Path(artist_path) / album_title) if artist_path else None
                                db.upsert_library_album(
                                    LibraryAlbum(
                                        id=album_id,
                                        artist_id=artist_id,
                                        title=album_title,
                                        clean_title=clean_library_name(album_title),
                                        foreign_album_id=foreign_album_id,
                                        release_date=album.get("release_date"),
                                        year=year_val,
                                        album_type=album_type,
                                        monitored=alb_monitored,
                                        path=alb_path,
                                        cover_url=album.get("cover_url"),
                                        total_tracks=_positive_int(album.get("track_count")),
                                    )
                                )

                            # Cache album cover
                            cov = album.get("cover_url") or (existing_alb.get("cover_url") if existing_alb else None)
                            if cov:
                                try:
                                    mediacover_service.ensure_artwork("album_cover", album_id, cov)
                                except Exception:
                                    pass

                            if alb_monitored and foreign_album_id:
                                album_details = None
                                try:
                                    album_details = discovery_client.get_album_details(foreign_album_id)
                                except Exception as exc:
                                    logger.warning(
                                        "Discovery client get_album_details failed during refresh for %s: %s",
                                        foreign_album_id,
                                        exc,
                                    )

                                if album_details:
                                    _store_total_tracks(db, album_id, album_details.get("track_count"))
                                if album_details and isinstance(album_details.get("tracks"), list):
                                    with album_hydration_lock(album_id):
                                        for trk in album_details["tracks"]:
                                            foreign_track_id = trk.get("id")
                                            existing_trk = None
                                            if foreign_track_id:
                                                existing_trk = db.get_library_track_by_foreign_id(
                                                    foreign_track_id, album_id=album_id
                                                )
                                            if not existing_trk:
                                                existing_trk = db.get_library_track_by_title(
                                                    album_id,
                                                    trk.get("title", ""),
                                                    track_number=trk.get("track_number"),
                                                )

                                            if existing_trk:
                                                track_id = existing_trk["id"]
                                                trk_monitored = bool(existing_trk["monitored"])
                                            else:
                                                track_id = str(uuid.uuid4())
                                                trk_monitored = hydrated_track_monitored(artist.get("monitor_option", "all"))

                                            trk_title = trk.get("title") or "Unknown Track"
                                            trk_num = int(trk.get("track_number") or 1)
                                            disc_num = int(trk.get("disc_number") or 1)
                                            dur = (
                                                float(trk["duration_seconds"])
                                                if trk.get("duration_seconds") is not None
                                                else None
                                            )

                                            db.upsert_library_track(
                                                LibraryTrack(
                                                    id=track_id,
                                                    album_id=album_id,
                                                    artist_id=artist_id,
                                                    title=trk_title,
                                                    clean_title=clean_library_name(trk_title),
                                                    track_number=trk_num,
                                                    disc_number=disc_num,
                                                    duration_seconds=dur,
                                                    monitored=trk_monitored,
                                                    foreign_track_id=foreign_track_id,
                                                )
                                            )
            except Exception as exc:
                logger.warning("Discovery client get_artist_details failed for refresh of %s: %s", foreign_artist_id, exc)

        # 3. Post-Deezer MusicBrainz metadata enrichment (bio, country, genres, and album release groups)
        if not mbid and artist_name:
            try:
                mbid = enricher.lookup_artist_mbid(artist_name)
                if mbid:
                    with db._lock:
                        db.conn.execute(
                            "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (mbid, artist_id),
                        )
                        db.conn.commit()
                    artist["mbid"] = mbid
            except Exception as exc:
                logger.warning("Error looking up artist MBID for %s: %s", artist_name, exc)

        if mbid:
            try:
                mb_details = enricher.get_artist_details(mbid)
                if mb_details:
                    country = mb_details.get("country")
                    genres_raw = mb_details.get("genres")
                    genres_str = (
                        ", ".join(genres_raw)
                        if isinstance(genres_raw, (list, tuple))
                        else (str(genres_raw) if genres_raw else None)
                    )
                    bio = mb_details.get("bio") or mb_details.get("disambiguation")

                    art_updates = []
                    art_params = []
                    if country and not artist.get("country"):
                        art_updates.append("country = ?")
                        art_params.append(country)
                        artist["country"] = country
                    if genres_str and not artist.get("genres"):
                        art_updates.append("genres = ?")
                        art_params.append(genres_str)
                        artist["genres"] = genres_str
                    if bio and not artist.get("bio"):
                        art_updates.append("bio = ?")
                        art_params.append(bio)
                        artist["bio"] = bio
                    if art_updates:
                        art_updates.append("updated_at = CURRENT_TIMESTAMP")
                        sql = f"UPDATE library_artists SET {', '.join(art_updates)} WHERE id = ?"
                        art_params.append(artist_id)
                        with db._lock:
                            db.conn.execute(sql, art_params)
                            db.conn.commit()

                discography = enricher.get_artist_discography(mbid)
                if discography:
                    for rg in discography:
                        rg_id = rg.get("id")
                        title = rg.get("title") or "Unknown Album"
                        existing_alb = None
                        if rg_id:
                            existing_alb = db.get_library_album_by_release_group_id(rg_id)
                        if not existing_alb:
                            existing_alb = db.get_library_album_by_title(artist_id, title)
                        if existing_alb:
                            upd_album = []
                            upd_params = []
                            if rg_id and existing_alb.get("mb_release_group_id") != rg_id:
                                upd_album.append("mb_release_group_id = ?")
                                upd_params.append(rg_id)
                            if not existing_alb.get("cover_url") and rg.get("cover_url"):
                                upd_album.append("cover_url = ?")
                                upd_params.append(rg["cover_url"])
                            _queue_release_date_update(existing_alb, rg, upd_album, upd_params)
                            if normalize_secondary_types(rg.get("secondary_types")) is not None and normalize_secondary_types(
                                rg["secondary_types"]
                            ) != existing_alb.get("secondary_types"):
                                upd_album.append("secondary_types = ?")
                                upd_params.append(json.dumps(normalize_secondary_types(rg["secondary_types"])))
                            if upd_album:
                                upd_album.append("updated_at = CURRENT_TIMESTAMP")
                                alb_sql = f"UPDATE library_albums SET {', '.join(upd_album)} WHERE id = ?"
                                upd_params.append(existing_alb["id"])
                                with db._lock:
                                    db.conn.execute(alb_sql, upd_params)
                                    db.conn.commit()
            except Exception as exc:
                logger.warning("Error enriching artist %s via MusicBrainz: %s", artist_id, exc)
            else:
                db.finish_pending_profile_recompute(artist_id)

    # Cache artist poster & banner
    if artist.get("image_url"):
        try:
            mediacover_service.ensure_artwork("artist_poster", artist_id, artist["image_url"])
        except Exception:
            pass
    if artist.get("banner_url"):
        try:
            mediacover_service.ensure_artwork("artist_banner", artist_id, artist["banner_url"])
        except Exception:
            pass

    art_pipeline.schedule_precache(db, artist_id)

    # Reconcile files
    try:
        reconcile_artist_files(db, artist_id)
    except Exception as r_err:
        logger.warning("Error running file reconciliation for artist %s: %s", artist_id, r_err)

    if not foreign_artist_id and not mbid:
        return {"success": False, "message": "Artist has no linked discovery foreign ID or MusicBrainz ID"}

    return {
        "success": True,
        "artist_id": artist_id,
        "refreshed_at": datetime.now().isoformat(),
    }


@router.post("/artists/{artist_id}/refresh", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def refresh_artist(
    artist_id: str,
    db: Database = Depends(get_db),
    discovery_client: DiscoveryClient = Depends(get_discovery_client),
    enricher: MbidEnricherClient = Depends(get_mbid_enricher),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Refreshes artist discography from Deezer/discovery metadata and enriches via MusicBrainz."""
    if _is_lidarr(db):
        numeric = lidarr_numeric_id(artist_id, "Artist")
        lidarr = require_lidarr(client)
        _lidarr_mutation(db, lambda: lidarr_library.refresh_artist(lidarr, numeric), "Artist")
        return {"success": True, "artist_id": artist_id, "message": "Refresh queued in Lidarr"}
    artist = db.get_library_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    return refresh_single_artist(
        artist_id=artist_id,
        db=db,
        discovery_client=discovery_client,
        enricher=enricher,
    )


@router.post("/artists/{artist_id}/search", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def search_artist(
    artist_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Asks Lidarr to search for every monitored missing album of the artist (Lidarr mode only)."""
    if not _is_lidarr(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only available while Lidarr manages the library")
    numeric = lidarr_numeric_id(artist_id, "Artist")
    lidarr = require_lidarr(client)
    _lidarr_mutation(db, lambda: lidarr_library.search_artist(lidarr, numeric), "Artist")
    return {"success": True, "message": "Search queued in Lidarr"}


def _unlink_library_file(db: Database, file_row: dict[str, Any]) -> None:
    """Unlinks a library file's bytes on an admin's request; a failure is logged, and a success lands in item history."""
    path = Path(file_row["file_path"])
    try:
        if not path.exists():
            return
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to delete file %s from disk: %s", path, exc)
        return
    if file_row.get("track_id"):
        emit(
            db, "file_deleted", track_id=str(file_row["track_id"]), message=f"Deleted {path.name} from disk",
            details={"path": str(path), "quality": file_row.get("quality_name"), "codec": file_row.get("codec")},
        )


@router.delete("/artists/{artist_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def delete_artist(
    artist_id: str,
    delete_files: bool = Query(False),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes an artist and cascades to child albums, tracks, and files. Optionally unlinks files on disk."""
    artist = db.get_library_artist(artist_id)
    if artist is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")

    if delete_files:
        tracks = db.list_library_tracks(artist_id=artist_id, limit=10000)
        for t in tracks:
            f = db.get_library_file_for_track(t["id"])
            if f and f.get("file_path"):
                _unlink_library_file(db, f)

    success = db.delete_library_artist(artist_id)
    return {"success": success}


def _enrich_albums(db: Database, albums: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attaches the artist name and track count to library albums."""
    album_ids = [a["id"] for a in albums]
    placeholders = ",".join("?" for _ in album_ids)
    with db._lock:
        cur = db.conn.execute(
            f"SELECT album_id, COUNT(*) FROM library_tracks WHERE album_id IN ({placeholders}) GROUP BY album_id",
            album_ids,
        )
        track_counts = dict(cur.fetchall())

    results: list[dict[str, Any]] = []
    artist_cache: dict[str, str] = {}
    for album in albums:
        a_dict = dict(album)
        art_id = album.get("artist_id")
        if art_id not in artist_cache:
            art = db.get_library_artist(art_id) if art_id else None
            artist_cache[art_id] = art["name"] if art else "Unknown Artist"
        a_dict["artist_name"] = artist_cache[art_id]
        a_dict["track_count"] = track_counts.get(album["id"], 0)
        a_dict["cover_url"] = _versioned_art_url("album", album["id"], a_dict.get("art_version"), a_dict.get("cover_url"))
        results.append(a_dict)
    return results


@router.get("/albums")
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


@router.get("/albums/{album_id}", dependencies=[Depends(require_core_tier)])
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


@router.put("/albums/{album_id}/monitored", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
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
    return updated or {}


@router.post("/albums/bulk-edit", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
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


@router.post("/albums/{album_id}/search", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
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


@router.delete("/albums/{album_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
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


def _enrich_tracks(db: Database, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attaches artist name, album title and the linked library file to library tracks."""
    artist_cache: dict[str, str] = {}
    album_cache: dict[str, str] = {}
    for t in tracks:
        art_id = t.get("artist_id")
        if art_id:
            if art_id not in artist_cache:
                art = db.get_library_artist(art_id)
                artist_cache[art_id] = art["name"] if art else "Unknown Artist"
            t["artist_name"] = artist_cache[art_id]
        else:
            t["artist_name"] = "Unknown Artist"

        alb_id = t.get("album_id")
        if alb_id:
            if alb_id not in album_cache:
                alb = db.get_library_album(alb_id)
                album_cache[alb_id] = alb["title"] if alb else "Unknown Album"
            t["album_title"] = album_cache[alb_id]
        else:
            t["album_title"] = "Unknown Album"

        t["file"] = db.get_library_file_for_track(t["id"])
    return tracks


@router.get("/tracks")
def list_tracks(
    album_id: Optional[str] = None,
    artist_id: Optional[str] = None,
    monitored_only: bool = False,
    query: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Lists library tracks with optional filtering and joins linked library file details."""
    tracks = db.list_library_tracks(
        album_id=album_id,
        artist_id=artist_id,
        monitored_only=monitored_only,
        query=query,
        limit=limit,
        offset=offset,
    )
    return _enrich_tracks(db, tracks)


@router.put("/tracks/{track_id}/monitored", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def set_track_monitored(
    track_id: str,
    body: TrackMonitoredRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Updates monitoring status for a single track."""
    track = db.get_library_track(track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    db.set_track_monitored(track_id=track_id, monitored=body.monitored)
    updated = db.get_library_track(track_id)
    return updated or {}


@router.post("/tracks/bulk-edit", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def bulk_edit_tracks(
    body: TrackBulkEditRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, int]:
    """Sets ``monitored`` on many tracks at once (native mode only; 409 while Lidarr manages the library)."""
    if not body.track_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="track_ids must not be empty")
    return {"tracks_updated": db.bulk_set_tracks_monitored(body.track_ids, body.monitored)}


@router.delete("/tracks/{track_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def delete_track(
    track_id: str,
    delete_files: bool = Query(False),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes a library track and cascades to child files. Optionally unlinks files on disk."""
    track = db.get_library_track(track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    if delete_files:
        f = db.get_library_file_for_track(track_id)
        if f and f.get("file_path"):
            _unlink_library_file(db, f)

    success = db.delete_library_track(track_id)
    return {"success": success}


@router.delete("/files/{file_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def delete_file(
    file_id: str,
    delete_file_from_disk: bool = Query(True),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes a library file record and optionally unlinks the physical file from disk."""
    row = db.get_library_file(file_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if delete_file_from_disk and row.get("file_path"):
        _unlink_library_file(db, row)

    success = db.delete_library_file(file_id)
    return {"success": success}


_HISTORY_LOOKUPS: dict[str, str] = {
    "artist": "get_library_artist", "album": "get_library_album", "track": "get_library_track",
}


@router.get(
    "/{entity}/{entity_id}/history",
    dependencies=[Depends(require_core_tier)],
    summary="Audit trail of an artist, album or track",
)
def get_item_history(
    entity: Literal["artist", "album", "track"],
    entity_id: str,
    limit: int = Query(100, ge=1, le=500),
    before: Optional[int] = Query(None, ge=1, description="Return events older than this event id"),
    db: Database = Depends(get_db),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Where an item came from and everything that happened to it since, newest first (keyset paging on ``before``).

    Album history includes its tracks' events and artist history everything beneath the artist. Administrators see
    every detail; everyone else gets the same events with indexer, client, protocol, hashes, file paths and other
    users' identities removed. 404 when the id has neither a library row nor any history.
    """
    if getattr(db, _HISTORY_LOOKUPS[entity])(entity_id) is None and not db.has_item_events(entity, entity_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity.capitalize()} not found")
    presenter = HistoryPresenter(db, user, _is_admin_principal(user))
    rows = db.list_item_events(entity, entity_id, limit=limit + 1, before_id=before)
    page = rows[:limit]
    return {
        "entity": entity,
        "entity_id": entity_id,
        "origin": presenter.present_origin(db.earliest_item_event(entity, entity_id)),
        "events": [presenter.present(row) for row in page],
        "next_before": int(page[-1]["id"]) if len(rows) > limit and page else None,
    }


def _is_admin_principal(user: dict[str, Any]) -> bool:
    """True only for a real admin session or API key; gateway-forwarded principals never qualify."""
    if user.get("forwarded"):
        return False
    perms = int(user.get("permissions") if user.get("permissions") is not None else 0)
    return bool(user.get("is_admin") or perms & int(UserPermission.ADMIN))


@router.get("/availability", summary="Get library availability")
def get_availability(
    artist_name: Optional[str] = Query(None),
    album_title: Optional[str] = Query(None),
    track_title: Optional[str] = Query(None),
    foreign_id: Optional[str] = Query(None),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Resolves library presence and file availability. In gateway mode, forwards to Core."""
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway" and config.trackseerr_core_url:
        core_client = CoreClient(
            core_url=config.trackseerr_core_url,
            secret=config.internal_core_secret,
        )
        try:
            return core_client.get_availability(
                artist_name=artist_name,
                album_title=album_title,
                track_title=track_title,
                foreign_id=foreign_id,
                user_info=_user,
            )
        except httpx.HTTPStatusError as exc:
            try:
                err_detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                err_detail = exc.response.text
            raise HTTPException(status_code=exc.response.status_code, detail=err_detail) from exc
        except (httpx.RequestError, Exception) as exc:
            logger.error("Failed to forward availability request to Core: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to communicate with TrackSeerr Core engine",
            ) from exc

    return get_item_availability(
        db,
        artist_name=artist_name,
        album_title=album_title,
        track_title=track_title,
        foreign_id=foreign_id,
    )


# -------------------------------------------------------------------------
# 2. Filesystem Scanner Controls
# -------------------------------------------------------------------------

@router.post("/scan", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def trigger_scan(
    body: Optional[ScanRequest] = None,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Triggers an asynchronous background filesystem scan."""
    req = body or ScanRequest()
    if req.root_folder:
        validate_media_path(req.root_folder, db=db)

    library_scanner.start_scan(
        db=db,
        root_folder=req.root_folder,
        prune_missing=req.prune_missing,
        plex_client=plex_client,
    )
    return {"success": True, "status": library_scanner.get_status()}


@router.get("/scan/status")
def get_scan_status(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves current filesystem scanner status."""
    return library_scanner.get_status()


@router.post("/scan/cancel", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def cancel_scan(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Signals active scan to stop and returns current scanner status."""
    return library_scanner.cancel_scan()


# -------------------------------------------------------------------------
# 3. Lidarr Migration Controls
# -------------------------------------------------------------------------

@router.post("/migrate-lidarr", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def trigger_lidarr_migration(
    body: Optional[MigrateLidarrRequest] = None,
    db: Database = Depends(get_db),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Triggers an asynchronous background Lidarr migration job."""
    if lidarr_client is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lidarr is not configured or settings are missing",
        )

    conn = lidarr_client.test_connection()
    if not conn.get("online"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lidarr is offline: {conn.get('error', 'connection failed')}",
        )

    req = body or MigrateLidarrRequest()
    success = lidarr_migration_job.start_migration(
        db=db,
        lidarr_client=lidarr_client,
        auto_switch_mode=req.auto_switch_mode,
    )
    return {"success": success, "status": lidarr_migration_job.get_status()}


@router.get("/migrate-lidarr/status")
def get_lidarr_migration_status(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves current Lidarr migration job status."""
    return lidarr_migration_job.get_status()


@router.post("/migrate-lidarr/cancel", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def cancel_lidarr_migration(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Signals active Lidarr migration job to stop and returns status."""
    return lidarr_migration_job.cancel()


# -------------------------------------------------------------------------
# 4. Manual Import Pipeline
# -------------------------------------------------------------------------

def _scan_fallback_tags(p: Path) -> dict[str, Any]:
    return {
        "title": p.stem,
        "artist": None,
        "album": None,
        "year": None,
        "track_number": None,
        "disc_number": 1,
        "codec": p.suffix.lstrip(".").upper(),
        "file_path": str(p),
    }


def _candidate_tracks(db: Database, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tracks in the manual-import picker shape, with artist/album names and whether a library file exists."""
    artist_names: dict[str, str] = {}
    album_titles: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for t in tracks:
        art_id = t.get("artist_id")
        if art_id and art_id not in artist_names:
            art = db.get_library_artist(art_id)
            artist_names[art_id] = art["name"] if art else "Unknown Artist"
        alb_id = t.get("album_id")
        if alb_id and alb_id not in album_titles:
            alb = db.get_library_album(alb_id)
            album_titles[alb_id] = alb["title"] if alb else "Unknown Album"
        out.append({
            "id": t["id"],
            "title": t.get("title"),
            "track_number": t.get("track_number"),
            "disc_number": t.get("disc_number"),
            "album_id": alb_id,
            "album_title": album_titles.get(alb_id) if alb_id else None,
            "artist_id": art_id,
            "artist_name": artist_names.get(art_id) if art_id else None,
            "has_file": db.get_library_file_for_track(t["id"]) is not None,
        })
    return out


def _tracks_without_file(db: Database, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in tracks if db.get_library_file_for_track(t["id"]) is None]


def _scan_one_file(db: Database, p: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inspects one file; returns (tags, base scan item with the unscoped tag-based library match)."""
    try:
        inspected = inspect_audio_file(p)
    except Exception as exc:
        logger.warning("Failed to inspect %s: %s", p, exc)
        inspected = _scan_fallback_tags(p)

    title = inspected.get("title")
    artist = resolve_album_artist(inspected, known_artist=lambda n: db.get_library_artist_by_name(n) is not None) or None
    album = inspected.get("album")
    trkn = inspected.get("track_number")

    # Fuzzy/clean search against existing library catalog
    matched_artist = db.get_library_artist_by_name(artist) if artist else None
    matched_album = None
    matched_track = None
    confidence = 0.0

    if matched_artist:
        confidence = 0.4
        if album:
            matched_album = db.get_library_album_by_title(matched_artist["id"], album)
            if matched_album:
                confidence = 0.7
                if title:
                    matched_track = db.get_library_track_by_title(matched_album["id"], title, track_number=trkn)
                    if matched_track:
                        confidence = 1.0

    try:
        size = p.stat().st_size
    except OSError:
        size = 0

    item = {
        "file_path": str(p),
        "filename": p.name,
        "size_bytes": size,
        "tags": inspected,
        "matched_artist_id": matched_artist["id"] if matched_artist else None,
        "matched_artist_name": matched_artist["name"] if matched_artist else (artist or None),
        "matched_album_id": matched_album["id"] if matched_album else None,
        "matched_album_title": matched_album["title"] if matched_album else (album or None),
        "matched_track_id": matched_track["id"] if matched_track else None,
        "matched_track_title": matched_track["title"] if matched_track else (title or None),
        "confidence": round(confidence, 2),
    }
    return inspected, item


def _unscoped_match_fields(db: Database, item: dict[str, Any]) -> dict[str, Any]:
    """match_strength / suggested_track_id / candidate_tracks for a folder-scan item (no candidate scope)."""
    if item["confidence"] == 1.0:
        strength = "strong"
    elif item["matched_album_id"]:
        strength = "weak"
    else:
        strength = "none"
    candidates: list[dict[str, Any]] = []
    if item["matched_album_id"]:
        candidates = _candidate_tracks(db, db.list_library_tracks(album_id=item["matched_album_id"], limit=1000))
    return {
        "match_strength": strength,
        "suggested_track_id": item["matched_track_id"],
        "candidate_tracks": candidates,
    }


def _scoped_match_fields(
    db: Database,
    item: dict[str, Any],
    inspected: dict[str, Any],
    remaining: list[dict[str, Any]],
    all_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Suggests a track from the scoped candidates (consuming it from ``remaining``) and rewrites matched_* to it."""
    meta = dict(inspected)
    meta.setdefault("file_path", item["file_path"])
    track, strength = reconcile_audio_file_to_track_scored(meta, remaining)
    if track is not None:
        remaining.remove(track)
        artist = db.get_library_artist(track["artist_id"]) if track.get("artist_id") else None
        album = db.get_library_album(track["album_id"]) if track.get("album_id") else None
        item.update({
            "matched_artist_id": artist["id"] if artist else None,
            "matched_artist_name": artist["name"] if artist else item["matched_artist_name"],
            "matched_album_id": album["id"] if album else None,
            "matched_album_title": album["title"] if album else item["matched_album_title"],
            "matched_track_id": track["id"],
            "matched_track_title": track["title"],
            "confidence": 1.0 if strength == MATCH_STRONG else 0.7,
        })
    else:
        item.update({"matched_album_id": None, "matched_track_id": None, "confidence": 0.0})
        strength = MATCH_NONE
    return {
        "match_strength": strength,
        "suggested_track_id": track["id"] if track is not None else None,
        "candidate_tracks": all_candidates,
    }


def _walk_audio_files(folder: Path) -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(str(folder)):
        dirs[:] = [d for d in dirs if not is_system_dirname(d)]
        for f in sorted(files):
            if is_system_filename(f):
                continue
            p = Path(root) / f
            if p.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(p)
    return found


@router.post("/manual-import/scan", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def manual_import_scan(
    body: Optional[ManualImportScanRequest] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Scans files for manual import, inspects metadata and suggests library matches.

    Scope (first that applies): ``download_id`` (that native download's held files, matched against its tracks that
    have no file), ``file_paths`` (exactly those files), ``album_id`` (the folder scan, matched against that album's
    tracks that have no file), otherwise a plain folder scan with the tag-based match.
    """
    req = body or ManualImportScanRequest()
    files: list[Path] = []
    scoped_tracks: Optional[list[dict[str, Any]]] = None

    if req.download_id:
        download = db.get_active_download(req.download_id)
        if download is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Download '{req.download_id}' not found")
        for raw in download.get("unmatched_files") or []:
            try:
                held = validate_media_path(raw, db=db, purpose="import")
            except HTTPException as exc:
                logger.warning("Skipping held file %s for download %s: %s", raw, req.download_id, exc.detail)
                continue
            if held.is_file():
                files.append(held)
        req_row = db.get_request(download["request_id"]) if download.get("request_id") else None
        _, expected = resolve_download_expected_tracks(db, download, req_row)
        scoped_tracks = _tracks_without_file(db, expected)
        if req.album_id:
            scoped_tracks = _tracks_without_file(db, db.list_library_tracks(album_id=req.album_id, limit=1000))
    elif req.file_paths is not None:
        for raw in req.file_paths:
            p = validate_media_path(raw, db=db, purpose="import")
            if not p.is_file():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"File does not exist: {raw}")
            files.append(p)
        if req.album_id:
            scoped_tracks = _tracks_without_file(db, db.list_library_tracks(album_id=req.album_id, limit=1000))
    else:
        folder_path = req.folder_path
        if not folder_path:
            mm = db.get_media_management_settings()
            roots = allowed_roots_for_all_clients(db, mm)
            folder_path = next((str(r) for r in roots.usable_roots()), None)
            if not folder_path:
                detail = "; ".join(roots.errors) or "No download folder is known"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{detail}. Pass a folder_path, or check your download client connection.",
                )
        validated_dir = validate_media_path(folder_path, db=db, purpose="import")
        if not validated_dir.exists() or not validated_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Directory does not exist or is not a directory: {folder_path}",
            )
        files = _walk_audio_files(validated_dir)
        if req.album_id:
            scoped_tracks = _tracks_without_file(db, db.list_library_tracks(album_id=req.album_id, limit=1000))

    all_candidates = _candidate_tracks(db, scoped_tracks) if scoped_tracks is not None else []
    remaining = list(scoped_tracks) if scoped_tracks is not None else []

    results: list[dict[str, Any]] = []
    for p in files:
        inspected, item = _scan_one_file(db, p)
        if scoped_tracks is not None:
            item.update(_scoped_match_fields(db, item, inspected, remaining, all_candidates))
        else:
            item.update(_unscoped_match_fields(db, item))
        results.append(item)
    return results


@router.get("/manual-import/album-tracks", dependencies=[Depends(require_core_tier), Depends(native_only)])
def manual_import_album_tracks(
    album_id: str = Query(..., min_length=1),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """An album's tracks in the manual-import picker shape (``has_file`` marks tracks that already have a file)."""
    if db.get_library_album(album_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
    return _candidate_tracks(db, db.list_library_tracks(album_id=album_id, limit=1000))


@router.post("/manual-import/commit", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def manual_import_commit(
    body: ManualImportCommitRequest,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Commits selected manual import items: resolves/creates catalog entities, moves/copies files to destination, tags them, and registers them in the library."""
    set_provenance(
        GrabTrigger(
            TRIGGER_MANUAL_IMPORT, label=_admin.get("username"),
            actor_user_id=str(_admin["id"]) if _admin.get("id") and _admin["id"] != "api_key_user" else None,
        )
    )
    imported_count = 0
    failed_count = 0
    results: list[dict[str, Any]] = []

    download_row: Optional[dict[str, Any]] = None
    if body.download_id:
        download_row = db.get_active_download(body.download_id)
        if download_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Download '{body.download_id}' not found"
            )

    media_settings = db.get_media_management_settings()
    root_folder_str = media_settings.get("root_folder_path") or "/music"
    root_dir = Path(root_folder_str).resolve()

    # Import mode only applies to torrent downloads; a download-scoped commit follows its client's type.
    download_client_type: Optional[str] = None
    if download_row is not None and download_row.get("client_id"):
        client_cfg = db.get_download_client(download_row["client_id"])
        download_client_type = str(client_cfg.get("driver_type") or "") if client_cfg else None

    try:
        client_roots = list(allowed_roots_for_all_clients(db, media_settings).roots)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Could not read every download client's folders for the recycle check: %s", redact_text(str(exc)))
        client_roots = []
    batch_placed: set[str] = set()  # files placed by this commit: never recycled as another item's "old" file
    replaced_retired: list[str] = []
    replaced_kept: list[str] = []

    manual_import_tag_cache: dict[str, list[str]] = {}  # artist id -> tag labels, one lookup per artist
    for item in body.items:
        source_str = item.source_path or item.file_path
        if not source_str:
            failed_count += 1
            results.append({"status": "failed", "error": "No source path provided"})
            continue

        try:
            source_path = validate_media_path(source_str, db=db, purpose="import")
            if not source_path.exists() or not source_path.is_file():
                failed_count += 1
                results.append({
                    "source_path": source_str,
                    "status": "failed",
                    "error": "Source file not found on disk",
                })
                continue

            try:
                inspected = inspect_audio_file(source_path)
            except Exception as exc:
                logger.warning("inspect_audio_file failed for %s, using fallback tags: %s", source_path, exc)
                inspected = {
                    "title": source_path.stem,
                    "artist": None,
                    "album": None,
                    "year": None,
                    "track_number": None,
                    "disc_number": 1,
                    "codec": source_path.suffix.lstrip(".").upper(),
                    "file_path": str(source_path),
                }

            # 1. Resolve or Create Artist
            artist_id = item.artist_id
            artist = db.get_library_artist(artist_id) if artist_id else None
            if not artist:
                art_name = (
                    item.artist_name
                    or resolve_album_artist(inspected, known_artist=lambda n: db.get_library_artist_by_name(n) is not None)
                    or "Unknown Artist"
                ).strip()
                artist = db.get_library_artist_by_name(art_name)
                if not artist:
                    artist = db.upsert_library_artist({
                        "id": str(uuid.uuid4()),
                        "name": art_name,
                        "clean_name": clean_library_name(art_name),
                        "monitored": True,
                        "monitor_option": str(media_settings.get("add_monitor_option") or DEFAULT_MONITOR_OPTION),
                    })
            artist_id = artist["id"]

            # 2. Resolve or Create Album
            album_id = item.album_id
            album = db.get_library_album(album_id) if album_id else None
            if not album:
                alb_title = (item.album_title or inspected.get("album") or "Unknown Album").strip()
                album = db.get_library_album_by_title(artist_id, alb_title)
                if not album:
                    alb_year = item.year or inspected.get("year")
                    album = db.upsert_library_album({
                        "id": str(uuid.uuid4()),
                        "artist_id": artist_id,
                        "title": alb_title,
                        "clean_title": clean_library_name(alb_title),
                        "year": alb_year,
                        "monitored": True,
                    })
            album_id = album["id"]

            # 3. Resolve or Create Track
            track_id = item.track_id
            track = db.get_library_track(track_id) if track_id else None
            file_title, file_trkn = parse_filename_track(source_path.stem)
            trkn = item.track_number or inspected.get("track_number") or file_trkn
            disc = item.disc_number or inspected.get("disc_number") or 1
            if not track:
                trk_title = (item.track_title or inspected.get("title") or file_title).strip()
                track = db.get_library_track_by_title(album_id, trk_title, track_number=trkn)
                if not track:
                    track = db.upsert_library_track({
                        "id": str(uuid.uuid4()),
                        "album_id": album_id,
                        "artist_id": artist_id,
                        "title": trk_title,
                        "clean_title": clean_library_name(trk_title),
                        "track_number": trkn or 1,
                        "disc_number": disc,
                        "duration_seconds": inspected.get("duration"),
                        "monitored": True,
                    })
            track_id = track["id"]

            # 3b. Quality profile & Cutoff evaluation
            cutoff_met = True
            quality_name = str(inspected.get("quality_full") or inspected.get("codec") or "Unknown")
            try:
                qp_id = artist.get("quality_profile_id")
                profile_dict = db.get_quality_profile(qp_id) if qp_id else None
                if not profile_dict:
                    profile_dict = db.get_default_quality_profile()
                if profile_dict:
                    qp = _to_quality_profile(profile_dict)
                    quality_input = (
                        inspected.get("quality_full")
                        or inspected.get("codec")
                        or source_path.suffix.lstrip(".").upper()
                    )
                    parsed = parse_release_title(str(quality_input))
                    if parsed.quality == "Unknown" and quality_input:
                        parsed.quality = str(quality_input)
                    fsize = source_path.stat().st_size
                    if artist_id not in manual_import_tag_cache:
                        manual_import_tag_cache[artist_id] = delay_gate.artist_tags(db, artist.get("name"), artist_id)
                    import_artist_tags = manual_import_tag_cache[artist_id]
                    eval_result = evaluate_release(parsed, qp, size_bytes=fsize, artist_tags=import_artist_tags)
                    # A bare quality string scores 0 format points and could never reach ``cutoff_format_score``, so
                    # judge the quality tier only; when the imported release title is known (and its quality matches)
                    # score from it instead, exactly as ``backlog_worker._current_floor`` does.
                    bd = eval_result.breakdown
                    cutoff_met = bool(bd.quality_cutoff_met if bd is not None else eval_result.meets_cutoff)
                    title = db.get_imported_release_title(track_id=track_id, album_id=album_id)
                    if title:
                        titled = parse_release_title(title)
                        if titled.quality in {parsed.quality, str(quality_input)}:
                            cutoff_met = bool(
                                evaluate_release(
                                    titled, qp, size_bytes=fsize, artist_tags=import_artist_tags
                                ).meets_cutoff
                            )
                    quality_name = eval_result.parsed_quality or str(quality_input)
            except Exception as exc:
                logger.warning("Cutoff evaluation error during manual import for %s: %s", source_path, exc)
                cutoff_met = True

            # 4. Resolve destination path
            meta = dict(inspected)
            meta["quality_full"] = quality_name  # same catalog source as rename preview/apply
            meta["artist"] = artist["name"]
            meta["album_artist"] = artist["name"]
            meta["album"] = album["title"]
            meta["title"] = track["title"]
            meta["track_number"] = track["track_number"]
            meta["disc_number"] = track["disc_number"]
            meta["total_discs"] = _album_total_discs(db, album_id, disc, inspected.get("total_discs"))
            if album.get("year"):
                meta["year"] = album["year"]
                meta["release_year"] = album["year"]
            meta["extension"] = source_path.suffix

            target_proposed = build_track_path(meta, media_settings)
            validate_media_path(target_proposed, db=db)

            # A file already registered inside the library is a re-assign, not a new import: it is always moved
            # within the library (never copied or linked from itself), and the old track loses its file record.
            # A hardlinked library file keeps the torrent's inode: renaming a link never touches the other name.
            source_row = db.get_library_file_by_path(str(source_path))
            is_rematch = source_row is not None and source_path.is_relative_to(root_dir)
            pre_recycled: Optional[tuple[Any, dict[str, Any]]] = None
            desired_path = Path(target_proposed).resolve()
            if is_rematch and desired_path == source_path:
                target_dest = source_path  # already at its naming path for the new track
            else:
                if desired_path != source_path and str(desired_path) not in batch_placed:
                    # The track's current file sits on the clean target name: rename it into the bin first so the
                    # new file takes that name instead of ``Name (1).ext`` (restored below if placement fails).
                    pre_recycled = recycle_in_place_target(
                        db, media_settings, root_dir, desired_path, track_id, client_roots
                    )
                target_dest = desired_path if pre_recycled is not None else resolve_collision(target_proposed)

            # 5. Place file
            if is_rematch:
                effective_mode = "move"
            elif download_row is not None:
                # Non-torrent downloads never seed, so they are always moved whatever the item asks for.
                effective_mode = (
                    item.mode or effective_import_mode(download_client_type, media_settings)
                    if is_torrent_driver_type(download_client_type)
                    else "move"
                )
            else:
                effective_mode = item.mode or str(media_settings.get("import_mode") or "move")
            if is_rematch and target_dest == source_path:
                placed_file = source_path
            else:
                try:
                    placed_file = place_audio_file(source_path, target_dest, mode=effective_mode)
                except Exception:
                    if pre_recycled is not None:
                        restore_recycled(pre_recycled[0])  # the replacement never landed: put the old bytes back
                    raise
            newly_placed = placed_file != source_path or not is_rematch
            if pre_recycled is not None:
                # The old file's bytes now live in the bin; its row (same path) must go before the new row is keyed.
                log_recycled(
                    db, pre_recycled[0], placed_file, pre_recycled[1], quality_name,
                    title=str(track.get("title") or ""), download_id=body.download_id,
                    issue_id=body.issue_id, retired=replaced_retired,
                    source="ManualImport", log_prefix="Manual import",
                )
                try:
                    db.delete_library_file(str(pre_recycled[1]["id"]))
                except sqlite3.Error as del_err:
                    logger.warning("Could not remove stale library file row %s: %s", pre_recycled[1].get("id"), redact_text(str(del_err)))
            if is_rematch and source_row is not None and str(placed_file) != str(source_row["file_path"]):
                db.delete_library_file(str(source_row["id"]))

            # 6. Write audio tags if requested
            write_tags = item.write_tags
            if write_tags is None:
                write_tags = bool(media_settings.get("write_audio_tags", True))
            if write_tags and not prepare_file_for_tagging(placed_file, media_settings):
                # A shared inode (torrent seeding link) must never be rewritten; the copy failed, so skip tags.
                write_tags = False
            if write_tags:
                try:
                    write_audio_tags(placed_file, meta)
                except Exception as exc:
                    logger.warning("Error writing tags to %s: %s", placed_file, exc)

            try:
                rel_path = str(placed_file.relative_to(root_dir))
            except ValueError:
                rel_path = placed_file.name

            existing_f = db.get_library_file_by_path(str(placed_file))
            file_id = str(existing_f["id"]) if existing_f else str(uuid.uuid4())
            db.upsert_library_file({
                "id": file_id,
                "track_id": track_id,
                "file_path": str(placed_file),
                "relative_path": rel_path,
                "codec": meta.get("codec") or placed_file.suffix.lstrip(".").upper(),
                "bitrate": meta.get("bitrate"),
                "sample_rate": meta.get("sample_rate"),
                "bits_per_sample": meta.get("bits_per_sample"),
                "quality_name": quality_name,
                "size_bytes": placed_file.stat().st_size if placed_file.exists() else 0,
                "cutoff_met": cutoff_met,
            })

            superseded: list[dict[str, Any]] = []
            if newly_placed:
                # A file really landed for this track: every other file it had is superseded (rename to the bin,
                # same rules and ``file_recycled`` event as a worker import). Files from this batch, and the
                # rematched library file itself (moved, not recycled), are never candidates.
                batch_placed.add(str(placed_file))
                skip_ids = {file_id} | ({str(source_row["id"])} if source_row is not None else set())
                superseded = [
                    r for r in db.list_library_files_for_track(track_id)
                    if str(r["id"]) not in skip_ids
                    and str(r.get("file_path") or "") not in batch_placed
                    and str(r.get("file_path") or "") != str(source_path)
                ]
                if superseded:
                    recycle_replaced_files(
                        db, superseded, placed_file, root_dir, media_settings, client_roots, quality_name,
                        replaced_retired, replaced_kept,
                        title=str(track.get("title") or ""), download_id=body.download_id,
                        issue_id=body.issue_id, source="ManualImport", log_prefix="Manual import",
                    )

            if is_rematch:
                if str(placed_file) != str(source_path):
                    emit(
                        db, "moved", track_id=str(track_id), message=f"Moved to {placed_file.name}",
                        details={"from": str(source_path), "to": str(placed_file), "reason": "manual rematch"},
                    )
            elif newly_placed:
                record_import_events(
                    db,
                    {"id": body.download_id, "title": str(track.get("title") or ""),
                     "request_id": download_row.get("request_id") if download_row else None},
                    str(track_id), placed_file, quality_name, meta,
                    superseded + ([pre_recycled[1]] if pre_recycled is not None else []),
                )

            if is_rematch:
                for finding_path in {str(source_path), str(placed_file)}:
                    db.delete_library_health_finding_by_path(finding_path, kind="weak_match")

            # Update album folder path if missing
            if not album.get("path"):
                db.upsert_library_album({
                    "id": album_id,
                    "artist_id": artist_id,
                    "title": album["title"],
                    "path": str(placed_file.parent),
                })

            imported_count += 1
            results.append({
                "source_path": source_str,
                "destination_path": str(placed_file),
                "artist_id": artist_id,
                "album_id": album_id,
                "track_id": track_id,
                "file_id": file_id,
                "status": "imported",
                "mode": effective_mode,
                "rematch": is_rematch,
            })

        except Exception as exc:
            logger.exception("Failed to import %s: %s", source_str, redact_text(str(exc)))
            failed_count += 1
            results.append({
                "source_path": source_str,
                "status": "failed",
                "error": redact_text(str(exc)),
            })

    if body.issue_id and (replaced_retired or replaced_kept):
        try:
            if db.get_issue(body.issue_id):
                comment = "Replacement imported"
                for line in replaced_retired:
                    comment += f"\nRetired old file: {line}"
                for line in replaced_kept:
                    comment += f"\nOld file kept at {line}"
                db.add_issue_comment(body.issue_id, None, comment, is_admin=True, is_system=True, staff=True)
        except sqlite3.Error as issue_err:
            logger.warning("Could not comment on issue %s after manual import: %s", body.issue_id, redact_text(str(issue_err)))

    download_cleared = False
    if download_row is not None and str(download_row.get("status")) == DownloadStatus.WARNING.value:
        download_cleared = _settle_download_after_manual_import(db, download_row, results, media_settings)

    if plex_client:
        try:
            as_media_server(plex_client).refresh_library()
        except Exception as exc:
            logger.warning("Error refreshing media-server library: %s", exc)

    return {
        "imported_count": imported_count,
        "failed_count": failed_count,
        "results": results,
        "download_cleared": download_cleared,
    }


def _settle_download_after_manual_import(
    db: Database,
    download: dict[str, Any],
    results: list[dict[str, Any]],
    media_settings: dict[str, Any],
) -> bool:
    """Drops imported files from a download's held list; settles the transfer once nothing is left to import."""
    done = {str(Path(r["source_path"]).resolve()) for r in results if r.get("status") == "imported" and r.get("source_path")}
    placed_modes = {str(r.get("mode") or "move") for r in results if r.get("status") == "imported" and r.get("destination_path")}
    placed_paths = [str(r["destination_path"]) for r in results if r.get("status") == "imported" and r.get("destination_path")]
    if placed_paths:
        # Record what this commit placed so the seed-cleanup safety gate can verify the library copies later.
        record_mode = "move" if "move" in placed_modes else ("hardlink" if "hardlink" in placed_modes else "copy")
        db.add_download_placed_files(download["id"], placed_paths, record_mode)
        download = db.get_active_download(download["id"]) or download
    held = [p for p in download.get("unmatched_files") or []]
    still_held = [p for p in held if str(Path(p).resolve()) not in done and os.path.isfile(p)]
    db.set_download_unmatched_files(download["id"], still_held)
    if still_held:
        db.update_download_status(
            download["id"],
            status=DownloadStatus.WARNING.value,
            error_message=f"{len(still_held)} file(s) couldn't be matched — manual import required",
        )
        return False
    modes = [str(r.get("mode") or "move") for r in results if r.get("status") == "imported"]
    # Any move-mode file has left the torrent's folder, so seeding retention no longer applies.
    effective_mode = "move" if not modes or not all(preserves_source(m) for m in modes) else modes[0]
    download = db.get_active_download(download["id"]) or download  # fresh placed/unmatched records for the safety gate
    new_status = _govern_download_at_client(db, download, media_settings, effective_mode)
    fields: dict[str, Any] = {"status": new_status, "error_message": ""}
    if new_status == DownloadStatus.COMPLETED.value:
        # target_path marks it already imported, so the worker's governance branch removes it once limits are met.
        fields["target_path"] = ", ".join(sorted({str(Path(r["destination_path"]).parent) for r in results if r.get("destination_path")}))
    db.update_download_status(download["id"], **fields)
    return True


def _govern_download_at_client(
    db: Database, download: dict[str, Any], media_settings: dict[str, Any], import_mode: str
) -> str:
    """Runs the worker's shared post-import governance for a manual import; returns the status to record."""
    if seed_action(media_settings) == "keep":
        return DownloadStatus.IMPORTED.value
    client_id = download.get("client_id")
    if not client_id:
        return DownloadStatus.IMPORTED.value
    target_lookup = download.get("download_hash") or download["id"]
    try:
        client_cfg = db.get_download_client(client_id)
        if not client_cfg:
            logger.warning("Cannot clean up %s: download client %s no longer exists", download["id"], client_id)
            return DownloadStatus.IMPORTED.value
        driver = get_acquisition_driver(client_cfg)
        try:
            status_dict: Optional[dict[str, Any]] = driver.get_status(target_lookup)
        except Exception as exc:  # driver errors span HTTP, auth and parsing
            logger.warning(
                "Keeping transfer %s: could not fetch seeding status (%s)", target_lookup, redact_text(str(exc))
            )
            return DownloadStatus.COMPLETED.value
        if not status_dict:
            logger.warning("Keeping transfer %s: driver returned no status", target_lookup)
            return DownloadStatus.COMPLETED.value
        return settle_transfer_after_import(driver, target_lookup, media_settings, import_mode, status_dict, download, db)
    except Exception as exc:  # the commit has already succeeded; never fail it over client governance
        logger.warning("Error settling transfer %s: %s", target_lookup, redact_text(str(exc)))
        return DownloadStatus.COMPLETED.value


# -------------------------------------------------------------------------
# 5. Preview & Batch Renamer
# -------------------------------------------------------------------------

def _album_total_discs(db: Database, album_id: str, *extra: Any) -> int:
    """Disc count of an album: the highest disc number in the catalog (or in ``extra`` hints), minimum 1.

    Needed so disc 1 of a multi-disc release is routed to the multi-disc naming format too.
    """
    discs: list[int] = []
    for value in extra:
        try:
            discs.append(int(value))
        except (TypeError, ValueError):
            pass
    for trk in db.list_library_tracks(album_id=album_id, limit=1000):
        try:
            discs.append(int(trk.get("disc_number") or 1))
        except (TypeError, ValueError):
            pass
    return max([1, *discs])


@router.post("/rename/preview", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def rename_preview(
    body: Optional[RenamePreviewRequest] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Previews proposed file path changes based on token naming templates and identifies files needing renaming."""
    req = body or RenamePreviewRequest()
    limit = req.limit or 200

    if req.album_id:
        tracks = db.list_library_tracks(album_id=req.album_id, limit=limit)
    elif req.artist_id:
        tracks = db.list_library_tracks(artist_id=req.artist_id, limit=limit)
    else:
        tracks = db.list_library_tracks(limit=limit)

    media_settings = db.get_media_management_settings()
    preview_diffs: list[dict[str, Any]] = []

    artist_cache: dict[str, dict[str, Any]] = {}
    album_cache: dict[str, dict[str, Any]] = {}

    for t in tracks:
        f = db.get_library_file_for_track(t["id"])
        if not f:
            continue
        current_path = f.get("file_path")
        if not current_path:
            continue

        art_id = t["artist_id"]
        if art_id not in artist_cache:
            art = db.get_library_artist(art_id)
            if art:
                artist_cache[art_id] = art
        artist = artist_cache.get(art_id)

        alb_id = t["album_id"]
        if alb_id not in album_cache:
            alb = db.get_library_album(alb_id)
            if alb:
                album_cache[alb_id] = alb
        album = album_cache.get(alb_id)

        if not artist or not album:
            continue

        meta = {
            "artist": artist["name"],
            "album_artist": artist["name"],
            "album": album["title"],
            "title": t["title"],
            "track_number": t["track_number"],
            "disc_number": t["disc_number"],
            "total_discs": _album_total_discs(db, alb_id),
            "year": album.get("year"),
            "release_year": album.get("year"),
            "codec": f.get("codec"),
            "bitrate": f.get("bitrate"),
            "sample_rate": f.get("sample_rate"),
            "bits_per_sample": f.get("bits_per_sample"),
            "quality_full": f.get("quality_name"),
            "file_path": current_path,
            "extension": Path(current_path).suffix,
        }
        proposed_path = build_track_path(meta, media_settings)
        needs_rename = Path(current_path).resolve() != Path(proposed_path).resolve()
        preview_diffs.append({
            "file_id": f["id"],
            "track_id": t["id"],
            "current_path": current_path,
            "proposed_path": proposed_path,
            "needs_rename": needs_rename,
        })

    return preview_diffs


@router.post("/rename/apply", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def rename_apply(
    body: RenameApplyRequest,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Applies batch renaming to specified library files, moving them to their template-rendered destinations and updating the catalog."""
    renamed_count = 0
    errors: list[str] = []

    media_settings = db.get_media_management_settings()
    root_folder_str = media_settings.get("root_folder_path") or "/music"
    root_dir = Path(root_folder_str).resolve()

    for fid in body.file_ids:
        f = db.get_library_file(fid)
        if not f:
            errors.append(f"Library file ID '{fid}' not found")
            continue

        current_path_str = f.get("file_path")
        if not current_path_str:
            errors.append(f"Library file '{fid}' has no recorded file path")
            continue

        try:
            current_path = validate_media_path(current_path_str, db=db)
            if not current_path.exists() or not current_path.is_file():
                errors.append(f"File '{current_path_str}' does not exist on disk")
                continue

            track = db.get_library_track(f["track_id"])
            if not track:
                errors.append(f"Track '{f['track_id']}' not found for file '{fid}'")
                continue

            album = db.get_library_album(track["album_id"])
            artist = db.get_library_artist(track["artist_id"])
            if not album or not artist:
                errors.append(f"Album or artist not found for track '{track['id']}'")
                continue

            meta = {
                "artist": artist["name"],
                "album_artist": artist["name"],
                "album": album["title"],
                "title": track["title"],
                "track_number": track["track_number"],
                "disc_number": track["disc_number"],
                "total_discs": _album_total_discs(db, track["album_id"]),
                "year": album.get("year"),
                "release_year": album.get("year"),
                "codec": f.get("codec"),
                "bitrate": f.get("bitrate"),
                "sample_rate": f.get("sample_rate"),
                "bits_per_sample": f.get("bits_per_sample"),
                "quality_full": f.get("quality_name"),
                "file_path": str(current_path),
                "extension": current_path.suffix,
            }
            proposed = build_track_path(meta, media_settings)
            validate_media_path(proposed, db=db)

            if current_path.resolve() == Path(proposed).resolve():
                continue  # Already matches target format

            target_path = resolve_collision(proposed)
            old_parent = current_path.parent
            new_path = safe_atomic_move(current_path, target_path)

            try:
                rel_path = str(new_path.relative_to(root_dir))
            except ValueError:
                rel_path = new_path.name

            db.upsert_library_file({
                "id": fid,
                "track_id": f["track_id"],
                "file_path": str(new_path),
                "relative_path": rel_path,
                "codec": f.get("codec"),
                "bitrate": f.get("bitrate"),
                "sample_rate": f.get("sample_rate"),
                "bits_per_sample": f.get("bits_per_sample"),
                "quality_name": f.get("quality_name"),
                "size_bytes": new_path.stat().st_size if new_path.exists() else f.get("size_bytes", 0),
                "cutoff_met": f.get("cutoff_met", True),
            })
            renamed_count += 1
            emit(
                db, "renamed" if new_path.parent == old_parent else "moved", track_id=str(f["track_id"]),
                message=f"{current_path.name} -> {new_path.name}",
                details={"from": str(current_path), "to": str(new_path)},
            )

            # Clean up empty parent folder if no other files remain
            try:
                if old_parent.exists() and not any(old_parent.iterdir()):
                    old_parent.rmdir()
            except OSError:
                pass

        except Exception as exc:
            logger.exception("Failed to rename file ID %s: %s", fid, redact_text(str(exc)))
            errors.append(f"Error renaming file '{fid}': {redact_text(str(exc))}")

    if plex_client:
        try:
            as_media_server(plex_client).refresh_library()
        except Exception as exc:
            logger.warning("Error refreshing media-server library: %s", exc)

    return {"renamed_count": renamed_count, "errors": errors}


# -------------------------------------------------------------------------
# AcoustID On-Demand Fingerprinting
# -------------------------------------------------------------------------

@router.post("/manual-import/fingerprint", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)])
def fingerprint_file(
    body: FingerprintRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Fingerprints an audio file on-demand via AcoustID without routine scanner overhead."""
    validated_file = validate_media_path(body.file_path, db=db, purpose="import")
    if not validated_file.exists() or not validated_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File does not exist: {body.file_path}",
        )
    settings = db.get_media_management_settings()
    fp = fingerprint_audio_file(
        str(validated_file),
        api_key=settings.get("acoustid_api_key"),
    )
    if fp:
        library_track: Optional[dict[str, Any]] = None
        if fp.get("recording_id"):
            row = db.get_library_track_by_mb_recording_id(fp["recording_id"])
            if row:
                artist_row = db.get_library_artist(row["artist_id"]) if row.get("artist_id") else None
                library_track = {
                    "id": row["id"],
                    "title": row.get("title"),
                    "album_id": row.get("album_id"),
                    "artist": artist_row["name"] if artist_row else None,
                }
        return {"success": True, "fingerprint": fp, "library_track": library_track}
    return {
        "success": False,
        "message": "Fingerprinting unavailable or no match found",
    }


# -------------------------------------------------------------------------
# Library Collections CRUD
# -------------------------------------------------------------------------

@router.get("/collections")
def list_collections(
    query: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Returns list of library collections with album counts."""
    return db.list_library_collections(limit=limit, offset=offset, query=query)


@router.post("/collections", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def create_collection(
    body: CreateCollectionRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Creates a new library collection."""
    col = LibraryCollection(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        summary=body.summary.strip() if body.summary else None,
        poster_url=body.poster_url.strip() if body.poster_url else None,
        monitored=body.monitored,
    )
    return db.upsert_library_collection(col)


@router.get("/collections/{collection_id}")
def get_collection(
    collection_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns collection detail along with its ordered albums list."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    result = dict(col)
    result["albums"] = db.get_collection_albums(collection_id)
    return result


@router.delete("/collections/{collection_id}", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def delete_collection(
    collection_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes a library collection."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    deleted = db.delete_library_collection(collection_id)
    return {"success": deleted, "id": collection_id}


@router.post("/collections/{collection_id}/albums", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def add_album_to_collection(
    collection_id: str,
    body: AddAlbumToCollectionRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Adds an album to a collection with optional order_index."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    album = db.get_library_album(body.album_id)
    if album is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Album not found",
        )
    success = db.add_album_to_collection(
        collection_id=collection_id,
        album_id=body.album_id,
        order_index=body.order_index,
    )
    return {"success": success, "collection_id": collection_id, "album_id": body.album_id}


@router.delete("/collections/{collection_id}/albums/{album_id}", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)])
def remove_album_from_collection(
    collection_id: str,
    album_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Removes an album from a collection."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    removed = db.remove_album_from_collection(collection_id, album_id)
    return {"success": removed, "collection_id": collection_id, "album_id": album_id}

