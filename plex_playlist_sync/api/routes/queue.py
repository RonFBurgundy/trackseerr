"""REST API endpoints for Arr-style Activity Queue view and cancellation."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from plex_playlist_sync.api.response_models import ApiModel

from plex_playlist_sync.api.dependencies import get_db, require_admin
from plex_playlist_sync.api.schemas.queue import QueueCancelResponse
from plex_playlist_sync.clients.acquisition import get_acquisition_driver
from plex_playlist_sync.models import DownloadStatus
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class QueueItemResponse(ApiModel):
    id: str
    request_id: Optional[str] = None
    client_id: str
    download_hash: Optional[str] = None
    title: str
    artist: str
    item_type: str = "track"
    status: str = "queued"
    progress: float = 0.0
    size_bytes: int = 0
    source_path: Optional[str] = None
    target_path: Optional[str] = None
    error_message: Optional[str] = None
    client_name: Optional[str] = None
    client_driver_type: Optional[str] = None
    speed_bps: Optional[int] = None
    eta_seconds: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    track_id: Optional[str] = None
    album_id: Optional[str] = None
    progress_updated_at: Optional[str] = None
    indexer: Optional[str] = None
    indexer_id: Optional[str] = None
    quality: Optional[str] = None
    protocol: Optional[str] = None
    unmatched_files: list[str] = []
    placed_files: list[str] = []
    placed_mode: Optional[str] = None
    seed_ratio_target: Optional[float] = None
    seed_time_target_minutes: Optional[int] = None
    seed_rule_source: Optional[str] = None
    seed_ratio_current: Optional[float] = None
    seeding_seconds: Optional[int] = None
    cleanup_attempts: int = 0
    cleanup_error: Optional[str] = None


@router.get("", response_model=list[QueueItemResponse], summary="List active download queue")
@router.get("/", response_model=list[QueueItemResponse], include_in_schema=False)
def get_queue(
    include_history: bool = Query(False, description="Include completed and failed downloads"),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Admin-only: returns active downloads in the activity queue with progress and client badges."""
    if include_history:
        items = db.list_active_downloads()
    else:
        # Show active downloads plus recent importing/completed
        items = db.list_active_downloads(
            statuses=[
                DownloadStatus.QUEUED.value,
                DownloadStatus.DOWNLOADING.value,
                DownloadStatus.IMPORTING.value,
            ]
        )
    return items


@router.delete("/{download_id}", response_model=QueueCancelResponse, response_model_exclude_unset=True, summary="Cancel and remove download from queue")
def cancel_download(
    download_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only: cancels a download with the underlying download client and removes it from the queue."""
    item = db.get_active_download(download_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download '{download_id}' not found",
        )

    # Cancel in download client if possible
    client_id = item.get("client_id")
    if client_id:
        client_cfg = db.get_download_client(client_id)
        if client_cfg:
            try:
                driver = get_acquisition_driver(client_cfg)
                target_hash = item.get("download_hash") or download_id
                driver.cancel(target_hash)
            except Exception as e:
                logger.warning("Could not signal cancel to download client %s: %s", client_id, e)

    db.delete_active_download(download_id)
    return {"status": "cancelled", "id": download_id}
