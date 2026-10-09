"""Shared helpers and internal utilities for library routes."""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse, Response


from plex_playlist_sync import library_paging as paging
from plex_playlist_sync import art_thumbs
from plex_playlist_sync import lidarr_library
from plex_playlist_sync.download_roots import allowed_roots_for_all_clients
from plex_playlist_sync.item_history import emit
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.api.dependencies import (
    get_db,
    require_admin,
)
from plex_playlist_sync.api.routes.activity import (
    SORT_DIR_PATTERN,
    lidarr_numeric_id,
    require_lidarr,
    validate_sort_key,
)
from plex_playlist_sync.clients.lidarr import (
    LidarrApiError,
    LidarrBadArtwork,
    LidarrClient,
    LidarrNotFound,
    MediaCover,
    _exc_text,
)
from plex_playlist_sync.library_manager import MODE_LIDARR, ModeChanged, get_library_mode, work_guard
from plex_playlist_sync.mediacover import mediacover_service
from plex_playlist_sync.storage import Database


logger = logging.getLogger(__name__)


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

_SORT_DIR = SORT_DIR_PATTERN

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

