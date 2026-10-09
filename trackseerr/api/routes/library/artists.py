"""Endpoints for artist management, metadata profiles, and ingestion."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional
import uuid

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
    ArtistRefreshResponse,
    BulkArtistsResult,
    CommandResponse,
    IngestArtistResponse,
    LibraryArtistRecord,
    MetadataProfile,
    MetadataProfileDeleteResponse,
    MetadataProfilePreview,
    MetadataProfilesResponse,
    SuccessResponse,
)
from trackseerr.api.routes.activity import (
    lidarr_numeric_id,
    require_lidarr,
)
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.lidarr import (
    LidarrClient,
    LidarrNotFound,
)
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_monitoring import (
    RELEASE_PRIMARY_TYPES,
    RELEASE_SECONDARY_TYPES,
    album_in_metadata_profile,
    album_monitored_for_option,
    DEFAULT_MONITOR_OPTION,
    hydrated_track_monitored,
    section_to_album_type,
)
from trackseerr.mediacover import mediacover_service
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
)
from trackseerr.library import (
    find_folder_art,
)
from trackseerr.storage import Database, clean_library_name
from trackseerr.tag_store import UnknownTag
from trackseerr.track_counts import positive_int as _positive_int


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (validate_media_path, NATIVE_ONLY_DETAIL, _is_lidarr, native_only, _lidarr_fetch, _lidarr_mutation, _lidarr_image, _versioned_art_url, _art_media_type, _native_art, _with_discovery_ids, _enrich_artists, _unlink_library_file)
from .models import (IngestArtistRequest, ArtistMonitoredRequest, MetadataProfileBody, ArtistBulkEditRequest, ArtistTagsRequest, ArtistTagsResponse)
from trackseerr.artist_refresh import _store_total_tracks, _album_track_counts, refresh_single_artist

@router.get("/artists", response_model=list[LibraryArtistRecord], response_model_exclude_unset=True)
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
    from trackseerr.artist_refresh_worker import artist_refresh_worker

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

@router.post("/artists/ingest", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=IngestArtistResponse, response_model_exclude_unset=True)
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

@router.get("/artists/{artist_id}", dependencies=[Depends(require_core_tier)], response_model=LibraryArtistRecord, response_model_exclude_unset=True)
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

@router.put("/artists/{artist_id}/monitored", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=LibraryArtistRecord, response_model_exclude_unset=True)
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
    if updated is None:  # deleted between the write and the read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artist not found")
    return updated

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

@router.post("/artists/bulk-edit", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=BulkArtistsResult, response_model_exclude_unset=True)
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
@router.get("/release-profiles", dependencies=_NATIVE_ADMIN, deprecated=True, response_model=MetadataProfilesResponse, response_model_exclude_unset=True)
@router.get("/metadata-profiles", dependencies=_NATIVE_ADMIN, response_model=MetadataProfilesResponse, response_model_exclude_unset=True)
def list_metadata_profiles(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Native metadata profiles with ``artist_count`` (artists using each) and the default for newly added artists."""
    return {
        "profiles": db.list_metadata_profiles(),
        "default_profile_id": db.get_media_management_settings().get("add_metadata_profile_id"),
        "primary_types": list(RELEASE_PRIMARY_TYPES),
        "secondary_types": list(RELEASE_SECONDARY_TYPES),
    }

# Deprecated alias (pre-v48 name), kept for one release.
@router.post("/release-profiles", dependencies=_NATIVE_ADMIN, status_code=status.HTTP_201_CREATED, deprecated=True, response_model=MetadataProfile, response_model_exclude_unset=True)
@router.post("/metadata-profiles", dependencies=_NATIVE_ADMIN, status_code=status.HTTP_201_CREATED, response_model=MetadataProfile, response_model_exclude_unset=True)
def create_metadata_profile(body: MetadataProfileBody, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return {**db.create_metadata_profile(body.name, body.primary_types, body.secondary_types), "artist_count": 0}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

# Deprecated alias (pre-v48 name), kept for one release.
@router.put("/release-profiles/{profile_id}", dependencies=_NATIVE_ADMIN, deprecated=True, response_model=MetadataProfile, response_model_exclude_unset=True)
@router.put("/metadata-profiles/{profile_id}", dependencies=_NATIVE_ADMIN, response_model=MetadataProfile, response_model_exclude_unset=True)
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
@router.delete("/release-profiles/{profile_id}", dependencies=_NATIVE_ADMIN, deprecated=True, response_model=MetadataProfileDeleteResponse, response_model_exclude_unset=True)
@router.delete("/metadata-profiles/{profile_id}", dependencies=_NATIVE_ADMIN, response_model=MetadataProfileDeleteResponse, response_model_exclude_unset=True)
def delete_metadata_profile(profile_id: int, db: Database = Depends(get_db)) -> dict[str, int]:
    """Deletes a profile; artists using it fall back to no profile (their albums keep their monitored flags)."""
    cleared = db.delete_metadata_profile(profile_id)
    if cleared is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata profile not found")
    return {"deleted": 1, "artists_cleared": cleared}

# Deprecated alias (pre-v48 name), kept for one release.
@router.get("/artists/{artist_id}/release-profile-preview", dependencies=_NATIVE_ADMIN, deprecated=True, response_model=MetadataProfilePreview, response_model_exclude_unset=True)
@router.get("/artists/{artist_id}/metadata-profile-preview", dependencies=_NATIVE_ADMIN, response_model=MetadataProfilePreview, response_model_exclude_unset=True)
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

@router.post("/artists/{artist_id}/refresh", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=ArtistRefreshResponse, response_model_exclude_unset=True)
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
        force=True,
    )

@router.post("/artists/{artist_id}/search", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=CommandResponse, response_model_exclude_unset=True)
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

@router.delete("/artists/{artist_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=SuccessResponse, response_model_exclude_unset=True)
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

