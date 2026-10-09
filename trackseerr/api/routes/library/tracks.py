"""Endpoints for track and file management and availability."""

import logging
import os
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

import httpx

from trackseerr.item_history import HistoryPresenter
from trackseerr.api.dependencies import (
    get_config,
    get_db,
    require_admin,
    require_core_tier,
    require_user,
    track_admin_actor,
)
from trackseerr.api.schemas.library import (
    AvailabilityResponse,
    ItemHistoryResponse,
    LibraryTrackRecord,
    SuccessResponse,
    TracksUpdatedResponse,
)
from trackseerr.clients.core_client import CoreClient
from trackseerr.config import Config
from trackseerr.library_availability import get_item_availability
from trackseerr.models import (
    UserPermission,
)
from trackseerr.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (native_only, _unlink_library_file, _enrich_tracks)
from .models import (TrackMonitoredRequest, TrackBulkEditRequest)

@router.get("/tracks", response_model=list[LibraryTrackRecord], response_model_exclude_unset=True)
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

@router.put("/tracks/{track_id}/monitored", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=LibraryTrackRecord, response_model_exclude_unset=True)
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
    if updated is None:  # deleted between the write and the read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
    return updated

@router.post("/tracks/bulk-edit", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=TracksUpdatedResponse, response_model_exclude_unset=True)
def bulk_edit_tracks(
    body: TrackBulkEditRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, int]:
    """Sets ``monitored`` on many tracks at once (native mode only; 409 while Lidarr manages the library)."""
    if not body.track_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="track_ids must not be empty")
    return {"tracks_updated": db.bulk_set_tracks_monitored(body.track_ids, body.monitored)}

@router.delete("/tracks/{track_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=SuccessResponse, response_model_exclude_unset=True)
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

@router.delete("/files/{file_id}", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=SuccessResponse, response_model_exclude_unset=True)
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
    response_model=ItemHistoryResponse,
    response_model_exclude_unset=True,
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

@router.get("/availability", summary="Get library availability", response_model=AvailabilityResponse, response_model_exclude_unset=True)
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

