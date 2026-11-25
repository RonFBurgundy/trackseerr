"""Admin API endpoints for managing configured media servers and the Plex sign-in server."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status

from trackseerr.api.dependencies import get_config, get_db, require_admin, require_core_tier
from trackseerr.api.schemas.media_servers import (
    MediaServerDeletedResponse,
    MediaServerItem,
    MediaServerLoginPayload,
    MediaServerLoginResponse,
    MediaServerPayload,
    MediaServerTestResult,
)
from trackseerr.config import Config
from trackseerr.media_servers import server_settings
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier)])


def _raise_for_settings_error(exc: server_settings.ServerSettingsError) -> NoReturn:
    if isinstance(exc, server_settings.ServerNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, server_settings.ServerLockedError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, server_settings.ServerSecretReuseError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("", response_model=list[MediaServerItem], summary="List configured media servers")
@router.get("/", response_model=list[MediaServerItem], include_in_schema=False)
def list_media_servers(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    """Return all configured media servers with secrets masked."""
    return [server_settings.present(row) for row in db.list_media_server_rows()]


@router.post("", response_model=MediaServerItem, summary="Create or update a media server")
@router.post("/", response_model=MediaServerItem, include_in_schema=False)
def save_media_server(
    payload: MediaServerPayload,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Create or update a media server, reload the registry, and notify listeners."""
    try:
        return server_settings.save(db, config, payload.model_dump())
    except server_settings.ServerSettingsError as exc:
        _raise_for_settings_error(exc)
    except sqlite3.Error as exc:
        logger.error("Failed to save media server: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database update failed"
        ) from exc


@router.post("/test", response_model=MediaServerTestResult, summary="Test a media-server connection")
def test_media_server(
    payload: MediaServerPayload,
    db: Database = Depends(get_db),
) -> MediaServerTestResult:
    """Test connectivity against a media server without saving or caching the adapter."""
    try:
        result = server_settings.test(db, payload.model_dump())
    except server_settings.ServerSettingsError as exc:
        _raise_for_settings_error(exc)
    except sqlite3.Error as exc:
        logger.error("Failed to load media server for test: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database update failed"
        ) from exc
    return MediaServerTestResult(ok=result.ok, message=result.message)


@router.delete("/{server_id}", response_model=MediaServerDeletedResponse, summary="Delete a media server")
def delete_media_server(
    server_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Delete a settings-configured media server."""
    try:
        server_settings.delete(db, config, server_id)
    except server_settings.ServerSettingsError as exc:
        _raise_for_settings_error(exc)
    except sqlite3.Error as exc:
        logger.error("Failed to delete media server: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database update failed"
        ) from exc
    return {"status": "deleted", "id": server_id}


@router.put("/login", response_model=MediaServerLoginResponse, summary="Select the Plex sign-in server")
def set_login_media_server(
    payload: MediaServerLoginPayload,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Select the unique Plex login server, or clear it when ``server_id`` is null."""
    try:
        server_settings.set_login(db, config, payload.server_id)
    except server_settings.ServerSettingsError as exc:
        _raise_for_settings_error(exc)
    except sqlite3.Error as exc:
        logger.error("Failed to update login media server: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database update failed"
        ) from exc
    return {"status": "ok", "server_id": payload.server_id}
