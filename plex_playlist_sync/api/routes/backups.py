"""REST API for database backups and restore (``/api/system/backups``).

Core / all-in-one tier only, administrative authentication required.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from plex_playlist_sync import __version__
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from plex_playlist_sync.api.response_models import ApiModel
from plex_playlist_sync.backup import (
    BackupValidationError,
    InvalidBackupNameError,
    create_backup,
    delete_backup,
    get_backup_dir,
    get_backup_retention,
    list_backups,
    resolve_backup_path,
    set_backup_retention,
    stage_restore,
    trigger_restart,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.storage import SCHEMA_VERSION, Database
from plex_playlist_sync.task_manager import (
    TASKS,
    effective_interval_seconds,
    set_interval_override,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier), Depends(require_admin)])


class BackupItem(ApiModel):
    name: str
    size: int
    created_at: Optional[str] = None
    kind: Optional[str] = None
    app_version: Optional[str] = None
    schema_version: Optional[int] = None
    error: Optional[str] = None


class BackupSettingsResponse(ApiModel):
    retention: int
    interval_seconds: Optional[int] = None


class BackupSettingsUpdate(ApiModel):
    retention: Optional[int] = None
    interval_seconds: Optional[int] = None


class SuccessFlag(ApiModel):
    success: bool


class RestoreResponse(ApiModel):
    success: bool
    message: str


def _get_target_backup_dir(db: Database) -> Path:
    base = db.db_path.parent if isinstance(db.db_path, Path) else None
    return get_backup_dir(base_dir=base)


@router.get("", response_model=list[BackupItem], response_model_exclude_unset=True, summary="List database backups")
def get_backups(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    """Lists all available database backups in order of newest first."""
    b_dir = _get_target_backup_dir(db)
    return list_backups(backup_dir=b_dir)


@router.post("", dependencies=[Depends(track_admin_actor)], response_model=BackupItem, response_model_exclude_unset=True, summary="Create manual database backup")
def create_manual_backup(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Snapshots the live database on-demand and creates a manual backup zip archive."""
    b_dir = _get_target_backup_dir(db)
    backup_path = create_backup(db, kind="manual", backup_dir=b_dir)
    backups = list_backups(backup_dir=b_dir)
    for b in backups:
        if b["name"] == backup_path.name:
            return b
    return {
        "name": backup_path.name,
        "size": backup_path.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "manual",
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "error": None,
    }


@router.get("/settings", response_model=BackupSettingsResponse, response_model_exclude_unset=True, summary="Get backup retention and schedule settings")
def get_settings(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Retrieves current scheduled backup retention count and effective interval."""
    return {
        "retention": get_backup_retention(db),
        "interval_seconds": effective_interval_seconds(db, config, "backup"),
    }


@router.put("/settings", dependencies=[Depends(track_admin_actor)], response_model=BackupSettingsResponse, response_model_exclude_unset=True, summary="Update backup retention and schedule settings")
def update_settings(
    body: BackupSettingsUpdate,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Updates scheduled backup retention count and/or interval schedule."""
    if body.retention is not None:
        if body.retention <= 0 or body.retention > 365:
            raise HTTPException(status_code=400, detail="Retention must be between 1 and 365")
        set_backup_retention(db, body.retention)
    if body.interval_seconds is not None:
        spec = TASKS.get("backup")
        if spec and spec.presets and body.interval_seconds not in spec.presets:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid interval_seconds: must be one of {list(spec.presets)}",
            )
        set_interval_override(db, "backup", body.interval_seconds)
    return {
        "retention": get_backup_retention(db),
        "interval_seconds": effective_interval_seconds(db, config, "backup"),
    }


@router.get("/{name}/download", summary="Download backup archive")
def download_backup(name: str, db: Database = Depends(get_db)) -> FileResponse:
    """Downloads a backup archive by name (admin only)."""
    b_dir = _get_target_backup_dir(db)
    try:
        path = resolve_backup_path(name, backup_dir=b_dir)
    except InvalidBackupNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Backup file not found")
    return FileResponse(path=str(path), filename=name, media_type="application/zip")


@router.delete("/{name}", dependencies=[Depends(track_admin_actor)], response_model=SuccessFlag, response_model_exclude_unset=True, summary="Delete backup archive")
def delete_backup_route(name: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Deletes a backup archive by name (admin only)."""
    b_dir = _get_target_backup_dir(db)
    try:
        delete_backup(name, backup_dir=b_dir)
    except InvalidBackupNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup file not found")
    return {"success": True}


@router.post("/{name}/restore", dependencies=[Depends(track_admin_actor)], response_model=RestoreResponse, response_model_exclude_unset=True, summary="Stage backup for restore and trigger restart")
def restore_backup(name: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Stages an existing backup archive for restore and triggers a graceful application restart."""
    b_dir = _get_target_backup_dir(db)
    try:
        path = resolve_backup_path(name, backup_dir=b_dir)
    except InvalidBackupNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Backup file not found")
    try:
        stage_restore(path, backup_dir=b_dir)
    except BackupValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Backup validation failed: {exc}")

    trigger_restart(delay_seconds=0.5)
    return {"success": True, "message": "Restore staged; server restarting"}


@router.post("/restore-upload", dependencies=[Depends(track_admin_actor)], response_model=RestoreResponse, response_model_exclude_unset=True, summary="Upload backup archive, stage for restore, and trigger restart")
async def restore_upload(
    request: Request,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Accepts an uploaded backup zip archive, streams it safely to disk, validates, stages it, and restarts."""
    from email.parser import BytesFeedParser
    from email.policy import default

    b_dir = _get_target_backup_dir(db)
    temp_fd, temp_path_str = tempfile.mkstemp(suffix=".upload.zip", dir=str(b_dir))
    os.close(temp_fd)
    temp_path = Path(temp_path_str)
    uploaded_size = 0
    max_upload_size = 2 * 1024 * 1024 * 1024  # 2 GiB

    try:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            parser = BytesFeedParser(policy=default)
            parser.feed(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            async for chunk in request.stream():
                uploaded_size += len(chunk)
                if uploaded_size > max_upload_size:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Uploaded file exceeds 2 GiB size limit",
                    )
                parser.feed(chunk)
            msg = parser.close()
            parts = list(msg.iter_parts())
            if not parts:
                raise HTTPException(status_code=400, detail="No file found in multipart upload")
            file_part = parts[0]
            for p in parts:
                if p.get_filename():
                    file_part = p
                    break
            payload = file_part.get_payload(decode=True)
            if not payload:
                raise HTTPException(status_code=400, detail="Empty file payload in upload")
            temp_path.write_bytes(payload)
        else:
            with open(temp_path, "wb") as dst:
                async for chunk in request.stream():
                    uploaded_size += len(chunk)
                    if uploaded_size > max_upload_size:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="Uploaded file exceeds 2 GiB size limit",
                        )
                    dst.write(chunk)
            if uploaded_size == 0:
                raise HTTPException(status_code=400, detail="Empty upload")

        try:
            stage_restore(temp_path, backup_dir=b_dir)
        except BackupValidationError as exc:
            raise HTTPException(status_code=400, detail=f"Backup validation failed: {exc}")

        trigger_restart(delay_seconds=0.5)
        return {"success": True, "message": "Restore staged; server restarting"}
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError as exc:
                logger.debug("Failed to remove uploaded temp file: %s", safe_exc(exc))
