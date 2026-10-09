"""Endpoints for library scanning and Lidarr catalog migration."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status


from plex_playlist_sync.api.dependencies import (
    get_db,
    get_lidarr_client,
    get_media_client,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from plex_playlist_sync.api.schemas.library import (
    MigrationStatus,
    MigrationTriggerResponse,
    ScanStatus,
    ScanTriggerResponse,
)
from plex_playlist_sync.clients.lidarr import (
    LidarrClient,
)
from plex_playlist_sync.library_scanner import library_scanner
from plex_playlist_sync.lidarr_migration import lidarr_migration_job
from plex_playlist_sync.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (validate_media_path, native_only)
from .models import (ScanRequest, MigrateLidarrRequest)

@router.post("/scan", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=ScanTriggerResponse, response_model_exclude_unset=True)
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

@router.get("/scan/status", response_model=ScanStatus, response_model_exclude_unset=True)
def get_scan_status(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves current filesystem scanner status."""
    return library_scanner.get_status()

@router.post("/scan/cancel", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=ScanStatus, response_model_exclude_unset=True)
def cancel_scan(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Signals active scan to stop and returns current scanner status."""
    return library_scanner.cancel_scan()

@router.post("/migrate-lidarr", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=MigrationTriggerResponse, response_model_exclude_unset=True)
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

@router.get("/migrate-lidarr/status", response_model=MigrationStatus, response_model_exclude_unset=True)
def get_lidarr_migration_status(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves current Lidarr migration job status."""
    return lidarr_migration_job.get_status()

@router.post("/migrate-lidarr/cancel", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=MigrationStatus, response_model_exclude_unset=True)
def cancel_lidarr_migration(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Signals active Lidarr migration job to stop and returns status."""
    return lidarr_migration_job.cancel()

