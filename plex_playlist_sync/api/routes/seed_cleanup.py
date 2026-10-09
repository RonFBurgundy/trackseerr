"""Seed cleanup: run the sweep, read its status, and act on its Needs-review findings. Admin only, core tier, native mode."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from plex_playlist_sync import seed_cleanup
from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier
from plex_playlist_sync.api.schemas.seed_cleanup import (
    RemoveOrphanResponse,
    RetryFailedResponse,
    SeedCleanupStarted,
    SeedCleanupStatus,
)
from plex_playlist_sync.api.routes.library._shared import native_only
from plex_playlist_sync.library_health import KIND_CLEANUP_FAILED, KIND_ORPHAN_TORRENT
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier), Depends(native_only)])


class RemoveOrphanRequest(BaseModel):
    delete_files: bool = False


@router.post("/run", response_model=SeedCleanupStarted, response_model_exclude_unset=True, status_code=status.HTTP_202_ACCEPTED, summary="Run the seed cleanup sweep in the background")
def run_seed_cleanup(db: Database = Depends(get_db)) -> Any:
    if not seed_cleanup.start_sweep_async(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A seed cleanup is already running")
    return JSONResponse({"started": True}, status_code=status.HTTP_202_ACCEPTED)


@router.get("/status", response_model=SeedCleanupStatus, response_model_exclude_unset=True, summary="Whether a sweep is running, and the last run")
def get_seed_cleanup_status(db: Database = Depends(get_db)) -> dict[str, Any]:
    return seed_cleanup.get_status(db)


def _finding(db: Database, finding_id: str, kind: str) -> dict[str, Any]:
    finding = db.get_library_health_finding(finding_id)
    if finding is None or finding["kind"] != kind:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")
    return finding


@router.post("/orphans/{finding_id}/remove", response_model=RemoveOrphanResponse, response_model_exclude_unset=True, summary="Remove an orphaned torrent (explicit user action)")
def remove_orphan_torrent(
    finding_id: str, body: RemoveOrphanRequest, db: Database = Depends(get_db)
) -> dict[str, Any]:
    finding = _finding(db, finding_id, KIND_ORPHAN_TORRENT)
    try:
        return seed_cleanup.remove_orphan(db, finding, body.delete_files)
    except seed_cleanup.OrphanActionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/failed/{finding_id}/retry", response_model=RetryFailedResponse, response_model_exclude_unset=True, summary="Reset a failed cleanup and try again now")
def retry_failed_cleanup(finding_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    finding = _finding(db, finding_id, KIND_CLEANUP_FAILED)
    try:
        return seed_cleanup.retry_failed(db, finding)
    except seed_cleanup.OrphanActionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
