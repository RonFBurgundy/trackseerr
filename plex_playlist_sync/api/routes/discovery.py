"""Discovery REST API endpoints for trending charts, new releases, and unified multi-source search."""

import concurrent.futures
import logging
import os
import sqlite3
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    get_discovery_client,
    get_lidarr_client,
    get_media_client,
    require_user,
)
from plex_playlist_sync import artist_profile, lidarr_library
from plex_playlist_sync.artist_links import normalize_artist_name
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient, LidarrNotFound
from plex_playlist_sync.library_manager import MODE_LIDARR, build_lidarr_client
from plex_playlist_sync.clients.core_client import CoreClient
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers import as_media_server
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_availability import get_item_availability
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


_CORE_AVAILABILITY_WORKERS = 8


def _gateway_core_client(config: Optional[Config]) -> Optional[CoreClient]:
    """Returns a CoreClient when running as a gateway with a configured core, else None."""
    if config is None:
        return None
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway" and config.trackseerr_core_url:
        return CoreClient(core_url=config.trackseerr_core_url, secret=config.internal_core_secret)
    return None


def _core_availability(
    client: CoreClient,
    items: list[dict[str, Any]],
    user: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolves library availability for each item on core (signed as ``user``); failures read as not in library."""

    def _one(it: dict[str, Any]) -> dict[str, Any]:
        is_album = (it.get("type") == "album") or (it.get("item_type") == "album")
        try:
            return client.get_availability(
                artist_name=it.get("artist"),
                album_title=it.get("title") if is_album else None,
                track_title=it.get("title") if not is_album else None,
                foreign_id=it.get("id"),
                user_info=user,
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Core availability lookup failed for '%s': %s", it.get("title"), exc)
            return {"in_library": False}

    if not items:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_CORE_AVAILABILITY_WORKERS, len(items))) as pool:
        return list(pool.map(_one, items))


def _is_admin(user: Optional[dict[str, Any]]) -> bool:
    """True only for a direct admin session; forwarded (gateway-signed) calls and missing users are never admin."""
    return bool(user and user.get("is_admin") and not user.get("forwarded"))


def _library_artist_ids_by_name(
    db: Database, config: Optional[Config], library_mode: str, clean_names: set[str]
) -> dict[str, str]:
    """``{clean_name: library_artist_id}`` for the given normalised names, in one batched lookup (no per-item queries)."""
    names = sorted(n for n in clean_names if n)
    if not names:
        return {}
    found: dict[str, str] = {}
    if library_mode == MODE_LIDARR:
        client = build_lidarr_client(db, config)
        if client is None:
            return {}
        try:
            for row in lidarr_library.snapshot("artists", client):
                clean = row.record.get("clean_name") or normalize_artist_name(row.record.get("name"))
                if clean in clean_names:
                    found.setdefault(clean, str(row.id))
        except (LidarrApiError, LidarrNotFound, httpx.HTTPError) as exc:
            logger.warning("Lidarr artist list unavailable for library hints: %s", exc)
        return found
    try:
        with db._lock:
            for start in range(0, len(names), 500):
                chunk = names[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = db.conn.execute(
                    f"SELECT clean_name, id FROM library_artists WHERE clean_name IN ({marks}) ORDER BY name COLLATE NOCASE, id",
                    chunk,
                ).fetchall()
                for clean, art_id in rows:
                    found.setdefault(clean, str(art_id))
    except sqlite3.Error as exc:
        logger.warning("Could not look up library artists for discovery hints: %s", exc)
    return found


def annotate_item_statuses(
    items: list[dict[str, Any]],
    db: Database,
    plex_client: Optional[PlexClient] = None,
    config: Optional[Config] = None,
    user: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Cross-references discovery items with music_requests and native library or Plex.

    On a gateway the local database holds no library, so availability is resolved on core
    through the signed ``CoreClient.get_availability`` call (as ``user``).
    """
    core_client = _gateway_core_client(config)
    core_avail: Optional[list[dict[str, Any]]] = (
        _core_availability(core_client, items, user) if core_client is not None else None
    )
    try:
        media_settings = db.get_media_management_settings()
        library_mode = media_settings.get("library_mode", "native")
    except Exception as e:
        logger.warning("Error loading media management settings for status annotation: %s", e)
        library_mode = "native"

    try:
        all_requests = db.list_requests()
        req_by_foreign_id = {r["foreign_id"]: r for r in all_requests if r.get("foreign_id")}
        req_by_artist_title = {
            ((r.get("artist") or "").lower().strip(), (r.get("title") or "").lower().strip()): r
            for r in all_requests
        }
    except Exception as e:
        logger.warning("Error loading requests for status annotation: %s", e)
        req_by_foreign_id = {}
        req_by_artist_title = {}

    annotated: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        it = dict(item)
        foreign_id = it.get("id")
        artist = (it.get("artist") or "").lower().strip()
        title = (it.get("title") or "").lower().strip()

        matched_req = req_by_foreign_id.get(foreign_id) or req_by_artist_title.get((artist, title))

        if core_avail is not None or library_mode == "native":
            if core_avail is not None:
                avail = core_avail[index]
            else:
                is_album = (it.get("type") == "album") or (it.get("item_type") == "album")
                avail = get_item_availability(
                    db,
                    artist_name=it.get("artist"),
                    album_title=it.get("title") if is_album else None,
                    track_title=it.get("title") if not is_album else None,
                    foreign_id=foreign_id,
                )
            if avail.get("in_library"):
                it["status"] = avail["status"]
                it["quality"] = avail.get("quality")
                if matched_req:
                    it["request_id"] = matched_req.get("id")
                annotated.append(it)
                continue

        if matched_req:
            req_status = matched_req.get("status")
            if req_status in ("available", "completed"):
                it["status"] = "available"
            elif req_status in ("processing", "approved"):
                it["status"] = "processing"
            elif req_status == "rejected":
                it["status"] = "rejected"
            else:
                it["status"] = "requested"
            it["request_id"] = matched_req.get("id")
        else:
            it["status"] = "none"

        annotated.append(it)

    # Routing hints: link each item to its library artist (one batched lookup). Admin-only: a library id is a
    # library-management handle. Gateways hold no library and a forwarded call is never admin.
    if core_client is None and _is_admin(user):
        hints = _library_artist_ids_by_name(
            db,
            config,
            library_mode,
            {normalize_artist_name(it.get("artist")) for it in annotated if it.get("artist")},
        )
        if hints:
            for it in annotated:
                lib_id = hints.get(normalize_artist_name(it.get("artist")))
                if lib_id:
                    it["library_artist_id"] = lib_id

    # For items without matches, skip individual per-track network round trips on batch discovery lists (> 5 items).
    # For small sets (<= 5 items) when plex_client is provided, resolve concurrently via ThreadPoolExecutor.
    server = as_media_server(plex_client)
    if server is not None and len(items) <= 5:
        plex_lookups: list[tuple[int, str, str]] = []
        for idx, it in enumerate(annotated):
            if it.get("status") in (None, "none") and not it.get("request_id"):
                art = (it.get("artist") or "").lower().strip()
                tit = (it.get("title") or "").lower().strip()
                if tit:
                    plex_lookups.append((idx, art, tit))

        if plex_lookups:
            def _check_plex(entry: tuple[int, str, str]) -> tuple[int, bool]:
                i, a, t = entry
                try:
                    plex_matches = server.search_tracks(t, limit=5)
                    for pm in plex_matches:
                        pm_artist = (pm.get("artist") or "").lower().strip()
                        pm_title = (pm.get("title") or "").lower().strip()
                        if (pm_artist and (a in pm_artist or pm_artist in a)) and (pm_title == t):
                            return i, True
                except Exception as exc:
                    logger.debug("Plex library check error for '%s': %s", t, exc)
                return i, False

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(plex_lookups))) as executor:
                futures = [executor.submit(_check_plex, entry) for entry in plex_lookups]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        idx_found, is_match = fut.result(timeout=2.0)
                        if is_match:
                            annotated[idx_found]["status"] = "in_library"
                    except Exception as exc:
                        logger.debug("Plex check future error: %s", exc)

    return annotated


@router.get("/trending")
def get_trending(
    limit: int = Query(default=25, ge=1, le=50),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Retrieves trending music tracks and albums annotated with library & request status."""
    raw_items = discovery.get_trending(limit=limit)
    annotated = annotate_item_statuses(raw_items, db=db, plex_client=plex_client, config=config, user=_user)
    return {"items": annotated, "count": len(annotated)}


@router.get("/new-releases")
def get_new_releases(
    limit: int = Query(default=25, ge=1, le=50),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Retrieves latest album releases annotated with library & request status."""
    raw_items = discovery.get_new_releases(limit=limit)
    annotated = annotate_item_statuses(raw_items, db=db, plex_client=plex_client, config=config, user=_user)
    return {"items": annotated, "count": len(annotated)}


@router.get("/search")
def search_discovery(
    q: str = Query(..., min_length=1),
    type: str = Query(default="all"),
    limit: int = Query(default=25, ge=1, le=50),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Performs unified multi-source search across iTunes and Deezer public APIs."""
    raw_items = discovery.search(query=q, item_type=type, limit=limit)
    annotated = annotate_item_statuses(raw_items, db=db, plex_client=plex_client, config=config, user=_user)
    return {"items": annotated, "query": q, "type": type, "count": len(annotated)}


@router.get("/album/{album_id}")
def get_album(
    album_id: str,
    discovery: DiscoveryClient = Depends(get_discovery_client),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Retrieves deep album details including tracklist and previews annotated with status."""
    album_data = discovery.get_album_details(album_id)
    if not album_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Album '{album_id}' not found",
        )

    album_dict = dict(album_data)
    album_dict.setdefault("item_type", "album")

    raw_tracks = album_dict.get("tracks", [])
    for t in raw_tracks:
        t.setdefault("item_type", "track")

    album_artist_ref = album_dict.get("artist_id")
    for t in raw_tracks:
        if album_artist_ref and t.get("artist") == album_dict.get("artist"):
            t.setdefault("artist_discovery_id", album_artist_ref)
    if album_artist_ref:
        album_dict.setdefault("artist_discovery_id", album_artist_ref)

    annotated_tracks = annotate_item_statuses(raw_tracks, db=db, plex_client=plex_client, config=config, user=_user)
    annotated_album = annotate_item_statuses([album_dict], db=db, plex_client=plex_client, config=config, user=_user)[0]
    annotated_album["tracks"] = annotated_tracks
    return annotated_album


@router.get("/artist/{artist_id}")
def get_artist(
    artist_id: str,
    discovery: DiscoveryClient = Depends(get_discovery_client),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Retrieves artist details and discography grouped into albums, singles_eps, and compilations."""
    artist_data = discovery.get_artist_details(artist_id)
    if not artist_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artist '{artist_id}' not found",
        )

    artist_dict = dict(artist_data)
    for group_key in ("albums", "singles_eps", "compilations"):
        items = artist_dict.get(group_key, [])
        for it in items:
            it.setdefault("item_type", "album")
        artist_dict[group_key] = annotate_item_statuses(items, db=db, plex_client=plex_client, config=config, user=_user)

    return artist_dict


@router.get("/artist-profile")
def get_artist_profile(
    discovery_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    library_artist_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
    lidarr: Optional[LidarrClient] = Depends(get_lidarr_client),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Unified artist profile: top tracks plus the full discography merged with what the library owns.

    Pass exactly one of ``discovery_id`` (``deezer:artist:<n>`` / ``itunes:artist:<n>``) or ``library_artist_id``.
    Admins get the full profile; everyone else gets library-free fields only (see ``shape_for_requester``).
    Library data survives a Deezer or MusicBrainz failure (empty discography, ``link_confidence: "none"``).
    """
    if bool(discovery_id) == bool(library_artist_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of discovery_id or library_artist_id",
        )

    admin = _is_admin(_user)
    if library_artist_id and not admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Library artist ids are admin-only")

    is_gateway = _gateway_core_client(config) is not None
    source = None if is_gateway else artist_profile.make_library_source(db, lidarr)

    def _annotate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return annotate_item_statuses(items, db=db, plex_client=plex_client, config=config, user=_user)

    profile = artist_profile.build_profile(
        db,
        discovery,
        source,
        _annotate,
        discovery_id=discovery_id,
        library_artist_id=library_artist_id,
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")
    return profile if admin else artist_profile.shape_for_requester(profile)
