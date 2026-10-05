"""Interactive Manual Search & Release Browser API routes for TrackSeerr Phase 4."""

import logging
import sqlite3
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from plex_playlist_sync.acquisition_coordinator import (
    _to_quality_profile,
    candidate_rank,
    resolve_duration,
    acquisition_coordinator,
)
from plex_playlist_sync import delay_gate
from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier
from plex_playlist_sync.library_manager import MODE_NATIVE, ModeChanged, work_guard
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.clients.acquisition import get_acquisition_driver
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadStatus,
    QualityProfile,
    RequestStatus,
)
from plex_playlist_sync.decision_engine import candidate_context, evaluate_prepared, prepare_profile
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class InteractiveReleaseItem(BaseModel):
    """Candidate release item enriched with quality evaluation, scoring, and source metadata."""

    id: str
    title: str
    indexer_name: str
    protocol: str  # "torrent", "usenet", "p2p"
    size_bytes: int
    seeders: Optional[int] = None
    publish_date: Optional[str] = None
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    parsed_quality: str
    source: Optional[str] = None
    tags: list[str] = []
    is_acceptable: bool
    score: int
    meets_cutoff: bool
    rejection_reasons: list[str] = []
    format_score: int = 0
    breakdown: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = {}


class InteractiveSearchQuery(BaseModel):
    """Payload for manual multi-indexer search."""

    artist: str
    title: Optional[str] = None
    album: Optional[str] = None
    item_type: str = "track"
    quality_profile_id: Optional[str] = None
    album_id: Optional[str] = None
    track_id: Optional[str] = None


class ManualGrabPayload(BaseModel):
    """Payload to force-grab a specific release candidate to a download client."""

    release: InteractiveReleaseItem
    client_id: Optional[str] = None
    artist: str
    title: str
    album: Optional[str] = None
    item_type: str = "track"
    request_id: Optional[str] = None


@router.post("/search", summary="Interactive multi-indexer search with quality evaluation")
def search_releases(
    query: InteractiveSearchQuery,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Queries all configured Torznab, Newznab, and slskd indexers.

    Parses each candidate release and evaluates it against the active or requested
    Quality Profile, returning ranked and scored results with acceptance flags.
    """
    clean_artist = query.artist.strip()
    clean_title = query.title.strip() if query.title else None
    clean_album = query.album.strip() if query.album else None

    # 1. Retrieve Quality Profile
    profile_dict: Optional[dict[str, Any]] = None
    if query.quality_profile_id:
        try:
            profile_dict = db.get_quality_profile(query.quality_profile_id)
        except Exception as e:
            logger.warning(
                "Error retrieving quality profile '%s': %s; falling back to default",
                query.quality_profile_id,
                redact_text(str(e)),
            )

    if not profile_dict:
        try:
            profile_dict = db.get_default_quality_profile()
        except Exception as e:
            logger.error("Failed to retrieve default quality profile: %s", redact_text(str(e)))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No valid quality profile configured: {redact_text(str(e))}",
            )

    profile: QualityProfile = _to_quality_profile(profile_dict)

    # 2. Query all enabled indexers and slskd clients
    try:
        candidates = acquisition_coordinator.search_all_indexers(
            artist=clean_artist,
            title=clean_title,
            album=clean_album,
            db=db,
        )
    except Exception as e:
        logger.error(
            "Failed searching indexers for '%s' ('%s' / '%s'): %s",
            clean_artist,
            clean_title,
            clean_album,
            redact_text(str(e)),
        )
        candidates = []

    # 3. Parse and evaluate each candidate release
    prepared = prepare_profile(profile)
    duration = resolve_duration(db, query.album_id, query.track_id, query.item_type)
    delay_profile = delay_gate.resolve_delay_profile(db, clean_artist)
    preferred_protocol = delay_profile.get("preferred_protocol")
    results: list[InteractiveReleaseItem] = []
    ranks: dict[int, tuple[Any, ...]] = {}
    for r in candidates:
        try:
            parsed = parse_release_title(r.title)
            eval_res = evaluate_prepared(
                parsed,
                prepared,
                r.size_bytes if r.size_bytes > 0 else None,
                duration=duration,
                **candidate_context(r),
            )

            # Determine protocol badge
            proto = str(r.protocol or "torrent").lower()
            if proto in ("slskd", "soulseek"):
                proto = "p2p"

            extra_data = dict(r.extra or {})
            indexer_name = (
                extra_data.get("indexer_name")
                or r.source
                or ("slskd" if proto == "p2p" else "Indexer")
            )
            pub_date = (
                extra_data.get("publish_date")
                or extra_data.get("pub_date")
                or extra_data.get("pubDate")
            )

            release_item = InteractiveReleaseItem(
                id=str(r.download_id or f"rel-{uuid.uuid4().hex[:12]}"),
                title=r.title,
                indexer_name=indexer_name,
                protocol=proto,
                size_bytes=int(r.size_bytes or 0),
                seeders=r.seeders,
                publish_date=str(pub_date) if pub_date is not None else None,
                download_url=r.download_url,
                magnet_url=r.magnet_url,
                parsed_quality=eval_res.parsed_quality,
                source=parsed.source or r.source,
                tags=list(parsed.tags),
                is_acceptable=eval_res.is_acceptable,
                score=eval_res.score,
                meets_cutoff=eval_res.meets_cutoff,
                rejection_reasons=list(eval_res.rejection_reasons),
                format_score=eval_res.format_score,
                breakdown=eval_res.breakdown.to_dict() if eval_res.breakdown else None,
                extra=extra_data,
            )
            results.append(release_item)
            ranks[id(release_item)] = candidate_rank(r, eval_res, preferred_protocol)
        except Exception as e:
            logger.warning("Error evaluating release '%s': %s", r.title, e)

    # 4. Sort: acceptable releases first, then the engine's full rank_key (quality tier > format score > kbps distance
    # > protocol preference of the applicable delay profile > seeders).
    results.sort(key=lambda item: (item.is_acceptable, ranks[id(item)]), reverse=True)

    return {
        "query": query.model_dump(),
        "profile_name": profile.name,
        "results": [item.model_dump() for item in results],
        "count": len(results),
    }


@router.post(
    "/grab",
    summary="Force-enqueue a chosen candidate release to a download client",
    dependencies=[Depends(require_core_tier)],
)
def grab_release(
    payload: ManualGrabPayload,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Native-mode only (409 while Lidarr manages the library); the grab runs under the library-manager guard."""
    try:
        with work_guard(db, MODE_NATIVE):
            return _grab_release(payload, db)
    except ModeChanged as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Library manager is set to Lidarr; native grabs are disabled.",
        ) from exc


def _grab_release(payload: ManualGrabPayload, db: Database) -> dict[str, Any]:
    """Force-enqueues a manually selected candidate release to the target download client.

    Creates an active download tracking record in the activity queue and transitions
    associated requests to 'processing'.
    """
    # 1. Resolve target download client
    client: Optional[dict[str, Any]] = None
    if payload.client_id:
        try:
            client = db.get_download_client(payload.client_id)
        except Exception as e:
            logger.warning("Failed to fetch download client '%s': %s", payload.client_id, e)

    if not client:
        client = acquisition_coordinator.find_client_for_protocol(
            protocol=payload.release.protocol,
            db=db,
        )

    if not client:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No download client configured or available for protocol",
        )

    # 2. Reconstruct canonical AcquisitionSearchResult
    search_result = AcquisitionSearchResult(
        download_id=payload.release.id,
        title=payload.release.title,
        artist=payload.artist,
        album=payload.album,
        item_type=payload.item_type,
        size_bytes=payload.release.size_bytes,
        format=None,
        quality_str=payload.release.parsed_quality,
        seeders=payload.release.seeders,
        download_url=payload.release.download_url,
        magnet_url=payload.release.magnet_url,
        source=payload.release.source or payload.release.indexer_name,
        extra=payload.release.extra or {},
        protocol=payload.release.protocol,
    )

    # 3. Dispatch download to client driver
    try:
        client_driver = get_acquisition_driver(client)
        download_hash = client_driver.download(search_result)
    except ValueError as e:
        logger.error(
            "Validation error dispatching download to '%s': %s",
            client.get("name"),
            redact_text(str(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Download dispatch failed: {redact_text(str(e))}",
        )
    except Exception as e:
        logger.error(
            "Failed dispatching download on client '%s' for '%s': %s",
            client.get("name"),
            payload.release.title,
            redact_text(str(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Download client error: {redact_text(str(e))}",
        )

    # 4. Insert active download record
    download_id = f"dl-{uuid.uuid4().hex[:12]}"
    active_dl = ActiveDownload(
        id=download_id,
        request_id=payload.request_id,
        client_id=str(client["id"]),
        download_hash=download_hash,
        title=payload.release.title,
        artist=payload.artist,
        item_type=payload.item_type,
        status=DownloadStatus.QUEUED.value,
        progress=0.0,
        size_bytes=payload.release.size_bytes,
        source_path=None,
        target_path=None,
    )
    try:
        db.create_active_download(active_dl)
        db.record_download_grab(
            download_id,
            indexer=payload.release.indexer_name,
            quality=payload.release.parsed_quality,
            protocol=payload.release.protocol,
        )
    except Exception as e:
        logger.error("Failed to record active download '%s': %s", download_id, e)

    # 5. Transition linked music request if present
    if payload.request_id:
        try:
            db.update_request_status(payload.request_id, RequestStatus.PROCESSING)
        except Exception as e:
            logger.warning(
                "Failed to update request '%s' to processing: %s",
                payload.request_id,
                redact_text(str(e)),
            )

    # A manual grab bypasses delay profiles and supersedes anything parked for the same request.
    if payload.request_id:
        try:
            parked = db.get_pending_release_by_key(delay_gate.item_key(payload.request_id, None, None, "", "", None))
            if parked:
                db.delete_pending_release(parked["id"])
        except sqlite3.Error as e:
            logger.warning("Failed to clear pending release for request '%s': %s", payload.request_id, type(e).__name__)

    client_name = client.get("name", "download client")
    logger.info(
        "Admin manual grab succeeded: '%s' via %s (download_id=%s)",
        payload.release.title,
        client_name,
        download_id,
    )

    return {
        "success": True,
        "download_id": download_id,
        "client": client_name,
        "message": f"Successfully enqueued '{payload.release.title}'",
    }


class PendingReleaseResponse(BaseModel):
    id: int
    title: str
    album_id: Optional[str] = None
    artist_name: str
    protocol: str
    quality: Optional[str] = None
    format_score: int = 0
    added_at: str
    release_at: str
    reason: str


def _pending_view(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in PendingReleaseResponse.model_fields}


@router.get(
    "/pending",
    response_model=list[PendingReleaseResponse],
    summary="List releases held by a delay profile",
    dependencies=[Depends(require_core_tier)],
)
def list_pending(
    db: Database = Depends(get_db), _admin: dict[str, Any] = Depends(require_admin)
) -> list[dict[str, Any]]:
    return [_pending_view(r) for r in db.list_pending_releases()]


@router.delete(
    "/pending/{pending_id}", summary="Drop a pending release", dependencies=[Depends(require_core_tier)]
)
def drop_pending(
    pending_id: int, db: Database = Depends(get_db), _admin: dict[str, Any] = Depends(require_admin)
) -> dict[str, Any]:
    if not db.delete_pending_release(pending_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pending release not found")
    return {"status": "deleted", "id": pending_id}


@router.post(
    "/pending/{pending_id}/grab",
    summary="Grab a pending release now, skipping its delay",
    dependencies=[Depends(require_core_tier)],
)
def grab_pending_now(
    pending_id: int, db: Database = Depends(get_db), _admin: dict[str, Any] = Depends(require_admin)
) -> dict[str, Any]:
    from plex_playlist_sync.pending_worker import grab_pending  # local: pending_worker imports the coordinator

    row = db.get_pending_release(pending_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pending release not found")
    try:
        result = grab_pending(db, row)
    except ModeChanged as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Library manager is set to Lidarr; native grabs are disabled.",
        ) from exc
    except sqlite3.Error as exc:
        logger.error("Grabbing pending release %s failed: %s", pending_id, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not record the download") from exc
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=redact_text(str(result.get("message") or "Grab failed")),
        )
    return {"success": True, "id": pending_id, "download_id": result.get("download_id"), "release": result.get("release")}


@router.get("/blocklist", summary="List blocklisted releases")
def list_blocklist(
    limit: int = 100,
    offset: int = 0,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Returns list of blocklisted downloads/releases (admin required)."""
    return db.list_blocklist(limit=limit, offset=offset)


@router.delete("/blocklist/{blocklist_id}", summary="Remove entry from blocklist")
def remove_from_blocklist(
    blocklist_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Removes an item from the download blocklist by ID (admin required)."""
    success = db.remove_from_blocklist(blocklist_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blocklist item '{blocklist_id}' not found",
        )
    return {"success": True, "message": f"Blocklist item '{blocklist_id}' removed"}

