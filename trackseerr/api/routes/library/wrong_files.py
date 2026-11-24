"""Endpoints for library-wide AcoustID verification ("Wrong files")."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from trackseerr import wrong_files
from trackseerr.api.dependencies import (
    get_db,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from trackseerr.api.routes.library._shared import native_only
from trackseerr.api.routes.library.models import RenameVerifyStatus, WrongFilesResponse
from trackseerr.rename_verification import get_verification_status
from trackseerr.storage import Database

router = APIRouter()


@router.get(
    "/wrong-files",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=WrongFilesResponse,
)
def list_wrong_files(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Lists fingerprinted library files whose audio mismatches their linked track."""
    return wrong_files.list_wrong_files(db)


@router.post(
    "/wrong-files/check",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=RenameVerifyStatus,
)
def start_wrong_files_check(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Starts a background AcoustID check across every file in the library."""
    try:
        started = wrong_files.start_wrong_file_check(db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not started:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A check is already running")
    return {**get_verification_status(), "api_key_set": True}


@router.get(
    "/wrong-files/check",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=RenameVerifyStatus,
)
def wrong_files_check_status(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Reports the progress of the current or last library-wide AcoustID check."""
    key = str(db.get_media_management_settings().get("acoustid_api_key") or "").strip()
    return {**get_verification_status(), "api_key_set": bool(key)}
