"""Account self-service endpoints for saving per-user Subsonic logins."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from trackseerr.api.dependencies import get_config, get_current_user, get_db
from trackseerr.api.schemas.account_media_servers import (
    AccountMediaServerDeletedResponse,
    AccountMediaServerItem,
    AccountMediaServerPayload,
)
from trackseerr.config import MEDIA_SERVER_SUBSONIC, Config
from trackseerr.media_servers import MediaServerError, registry
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.secret_box import seal
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

_NON_ACCOUNT_IDS = frozenset({"api_key_user", "feed_token_user", "gateway_service"})


def _session_user(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if str(current_user.get("id")) in _NON_ACCOUNT_IDS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account endpoints require a user session",
        )
    return current_user


def _listed_subsonic_servers(config: Config) -> list[ServerRecord]:
    return [
        rec
        for rec in registry.records(config)
        if rec.enabled and rec.kind == MEDIA_SERVER_SUBSONIC and registry.ROLE_PLAYLISTS in rec.roles
    ]


@router.get("", response_model=list[AccountMediaServerItem], summary="List Subsonic servers for personal login")
@router.get("/", response_model=list[AccountMediaServerItem], include_in_schema=False)
def list_account_media_servers(
    current_user: dict[str, Any] = Depends(_session_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> list[dict[str, Any]]:
    """Return every enabled Subsonic playlist server and whether the signed-in user has a saved login."""
    saved = {row["server_id"]: row for row in db.list_server_accounts_for_user(current_user["id"])}
    items: list[dict[str, Any]] = []
    for rec in _listed_subsonic_servers(config):
        entry = saved.get(rec.id)
        items.append(
            {
                "server_id": rec.id,
                "name": rec.name,
                "username": entry["username"] if entry is not None else "",
                "saved": entry is not None,
            }
        )
    return items


@router.put("/{server_id}", response_model=AccountMediaServerItem, summary="Test and save a personal Subsonic login")
def save_account_media_server(
    server_id: str,
    payload: AccountMediaServerPayload,
    current_user: dict[str, Any] = Depends(_session_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Verify ``username``/``password`` against ``server_id`` and store it encrypted for the signed-in user."""
    rec = next((s for s in _listed_subsonic_servers(config) if s.id == server_id), None)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media server not found")

    username = payload.username.strip()
    password = payload.password
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Username and password are required",
        )

    adapter = registry.build_adapter(rec)
    if adapter is None or not hasattr(adapter, "with_login"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Subsonic server is unavailable",
        )

    user_adapter = None
    try:
        user_adapter = adapter.with_login(username, password)
        result = user_adapter.test_connection()
    except MediaServerError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.safe_detail,
        ) from exc
    finally:
        if user_adapter is not None and hasattr(user_adapter, "close"):
            user_adapter.close()

    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=result.message or "Subsonic rejected the credentials",
        )

    sealed = seal(password, data_dir=config.state_dir)
    try:
        db.set_server_account(current_user["id"], rec.id, username, sealed)
    except sqlite3.Error as exc:
        logger.error("Failed to save user server account: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database update failed",
        ) from exc

    return {
        "server_id": rec.id,
        "name": rec.name,
        "username": username,
        "saved": True,
    }


@router.delete(
    "/{server_id}",
    response_model=AccountMediaServerDeletedResponse,
    summary="Remove a personal Subsonic login",
)
def delete_account_media_server(
    server_id: str,
    current_user: dict[str, Any] = Depends(_session_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Delete the signed-in user's saved login for ``server_id``."""
    try:
        db.delete_server_account(current_user["id"], server_id)
    except sqlite3.Error as exc:
        logger.error("Failed to delete user server account: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database update failed",
        ) from exc
    return {"status": "deleted", "server_id": server_id}
