"""Admin API endpoints for managing configured media servers and the Plex sign-in server."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from trackseerr.api.dependencies import get_config, get_db, require_admin, require_core_tier
from trackseerr.api.schemas.media_servers import (
    MediaServerDeletedResponse,
    MediaServerItem,
    MediaServerLinksGroup,
    MediaServerLoginPayload,
    MediaServerLoginResponse,
    MediaServerPayload,
    MediaServerTestResult,
    ServerUserLinkDeletedResponse,
    ServerUserLinkPayload,
    ServerUserLinkResponse,
)
from trackseerr.config import MEDIA_SERVER_PLEX, MEDIA_SERVER_SUBSONIC, Config
from trackseerr.media_servers import registry, server_settings
from trackseerr.media_servers.base import MediaServerError
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier)])
links_router = APIRouter(prefix="/links", dependencies=[Depends(require_admin), Depends(require_core_tier)])


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
def list_media_servers(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> list[dict[str, Any]]:
    """Return all configured media servers with secrets masked."""
    by_id = {r.id: r for r in registry.records(config)}
    return [server_settings.present(row, record=by_id.get(row["id"])) for row in db.list_media_server_rows()]


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
    config: Config = Depends(get_config),
) -> MediaServerTestResult:
    """Test connectivity against a media server without saving or caching the adapter."""
    try:
        result = server_settings.test(db, payload.model_dump(), config=config)
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


@links_router.get("", response_model=list[MediaServerLinksGroup], summary="List per-server user links")
@links_router.get("/", response_model=list[MediaServerLinksGroup], include_in_schema=False)
def list_media_server_links(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> list[dict[str, Any]]:
    """Return linked users per enabled playlist server, including unlinked Plex users on the sign-in Plex."""
    all_users: list[dict[str, Any]] | None = None
    groups: list[dict[str, Any]] = []
    for rec in registry.records(config):
        if not rec.enabled or registry.ROLE_PLAYLISTS not in rec.roles:
            continue
        links = db.list_server_user_links(server_id=rec.id)
        users_list: list[dict[str, Any]] = [
            {
                "user_id": str(row["user_id"]),
                "server_username": str(row["server_username"]),
                "synthetic": False,
            }
            for row in links
        ]
        if rec.kind == MEDIA_SERVER_PLEX and rec.is_login:
            if all_users is None:
                all_users = db.list_users()
            linked_ids = {entry["user_id"] for entry in users_list}
            for user in all_users:
                uid = str(user.get("id") or "")
                if user.get("auth_type") == "plex" and uid and uid not in linked_ids:
                    users_list.append(
                        {
                            "user_id": uid,
                            "server_username": str(user.get("username") or ""),
                            "synthetic": True,
                        }
                    )
        groups.append(
            {
                "server_id": rec.id,
                "server_name": rec.name,
                "kind": rec.kind,
                "users": users_list,
            }
        )
    return groups


def _resolve_navidrome_user_id(rec: registry.ServerRecord, username: str) -> Optional[str]:
    """Resolve ``username`` to a Navidrome user id via ``list_users()`` when ``rec`` has ``navidrome_admin()``."""
    if rec.kind != MEDIA_SERVER_SUBSONIC:
        return None
    adapter = registry.build_adapter(rec)
    if adapter is None:
        return None
    nav_fn = getattr(adapter, "navidrome_admin", None)
    nav_admin = nav_fn() if callable(nav_fn) else None
    if nav_admin is None:
        return None
    try:
        users = nav_admin.list_users()
    except MediaServerError as exc:
        logger.warning("Failed to list Navidrome users on '%s': %s", rec.name, exc.safe_detail)
        return None
    target = username.strip().lower()
    for nav_user in users:
        if nav_user.name.strip().lower() == target:
            return str(nav_user.id)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"User '{username}' not found on Navidrome server '{rec.name}'",
    )


@links_router.put(
    "/{server_id}/{user_id}",
    response_model=ServerUserLinkResponse,
    summary="Manually link a user to a media server",
)
def put_media_server_link(
    server_id: str,
    user_id: str,
    payload: ServerUserLinkPayload,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Create or update a manual ``server_user_links`` entry for ``(server_id, user_id)``."""
    rec = next((r for r in registry.records(config) if r.id == server_id), None)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media server not found")
    if db.get_user(user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    cleaned_username = payload.server_username.strip()
    if not cleaned_username:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="server_username must not be blank",
        )
    resolved_id = _resolve_navidrome_user_id(rec, cleaned_username)
    try:
        existing = db.get_server_user_link(user_id, server_id)
        if resolved_id is not None:
            server_user_id = resolved_id
        elif existing is not None and existing.get("server_username") == cleaned_username:
            server_user_id = str(existing["server_user_id"])
        else:
            server_user_id = cleaned_username
        db.link_server_user(user_id, server_id, server_user_id, cleaned_username, source="manual")
    except sqlite3.Error as exc:
        logger.error("Failed to save server user link: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database update failed"
        ) from exc
    return {
        "user_id": user_id,
        "server_id": server_id,
        "server_username": cleaned_username,
        "source": "manual",
    }


@links_router.delete(
    "/{server_id}/{user_id}",
    response_model=ServerUserLinkDeletedResponse,
    summary="Unlink a user from a media server",
)
def delete_media_server_link(
    server_id: str,
    user_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Remove a ``server_user_links`` entry for ``(server_id, user_id)``."""
    try:
        removed = db.unlink_server_user(user_id, server_id)
    except sqlite3.Error as exc:
        logger.error("Failed to delete server user link: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database update failed"
        ) from exc
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server user link not found")
    return {"status": "deleted", "server_id": server_id, "user_id": user_id}
