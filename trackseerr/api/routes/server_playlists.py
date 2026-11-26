"""Admin API routes to inspect and adopt playlists from Subsonic and Jellyfin media servers."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trackseerr.api.dependencies import actor_id, get_config, get_db, require_admin, require_core_tier
from trackseerr.api.routes.playlists import _guard_existing_playlist
from trackseerr.api.schemas.playlists import PlaylistRecord
from trackseerr.api.schemas.server_playlists import ServerPlaylistSummary, ServerPlaylistTrack
from trackseerr.config import MEDIA_SERVER_PLEX, Config
from trackseerr.media_servers import (
    MediaServer,
    MediaServerError,
    MediaServerNotFound,
    MediaServerUnsupported,
    registry,
)
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.playlist_policy import initial_monitor_mode_forced
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier)])


def _resolve_server(server_id: str, config: Config) -> tuple[ServerRecord, MediaServer]:
    """Return ``(record, adapter)`` for an adoptable server, or raise 404 / 409 / 502."""
    pair = registry.server_by_id(server_id, config=config)
    if pair is not None:
        record, adapter = pair
        if record.kind == MEDIA_SERVER_PLEX and record.is_login:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Use the Plex page for the sign-in Plex server",
            )
        return record, adapter
    rec = next((r for r in registry.records(config) if r.id == server_id), None)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media server not found")
    if rec.kind == MEDIA_SERVER_PLEX and rec.is_login:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use the Plex page for the sign-in Plex server",
        )
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Media server '{rec.name}' is unavailable")


@contextmanager
def _server_errors(server_id: str) -> Iterator[None]:
    """Translate ``MediaServerError`` subclasses into HTTP responses."""
    try:
        yield
    except MediaServerUnsupported as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_detail) from exc
    except MediaServerNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.safe_detail) from exc
    except MediaServerError as exc:
        logger.warning("Media server playlist request failed for %s: %s", server_id, exc.safe_detail)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_detail) from exc


@router.get("/{server_id}/playlists", response_model=list[ServerPlaylistSummary])
def list_server_playlists(
    server_id: str,
    user: Optional[str] = Query(default=None),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> list[dict[str, Any]]:
    """List playlists available on a Subsonic or Jellyfin server alongside any adopted Trackseerr id."""
    _, adapter = _resolve_server(server_id, config)
    with _server_errors(server_id):
        playlists = adapter.list_source_playlists(user=user)
    adopted_by_pid = {
        row["server_playlist_id"]: row["playlist_id"] for row in db.list_server_playlist_sources(server_id)
    }
    return [{**pl, "adopted_playlist_id": adopted_by_pid.get(pl["id"])} for pl in playlists]


@router.get("/{server_id}/playlists/{pid}/items", response_model=list[ServerPlaylistTrack])
def get_server_playlist_items(
    server_id: str,
    pid: str,
    user: Optional[str] = Query(default=None),
    config: Config = Depends(get_config),
) -> list[dict[str, str]]:
    """Return read-only tracks of a playlist on a Subsonic or Jellyfin server."""
    _, adapter = _resolve_server(server_id, config)
    with _server_errors(server_id):
        return adapter.source_playlist_tracks(pid, user=user)


@router.post(
    "/{server_id}/playlists/{pid}/adopt",
    response_model=PlaylistRecord,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
)
def adopt_server_playlist(
    server_id: str,
    pid: str,
    user: Optional[str] = Query(default=None),
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Adopt a Subsonic or Jellyfin playlist into Trackseerr as a read-only mirrored snapshot."""
    record, adapter = _resolve_server(server_id, config)
    with _server_errors(server_id):
        source_playlists = adapter.list_source_playlists(user=user)
        source_pl = next((p for p in source_playlists if p["id"] == pid), None)
        if source_pl is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Playlist '{pid}' not found")
        tracks = adapter.source_playlist_tracks(pid, user=user)

    playlist_id = f"{record.kind}_{server_id[:8]}_{pid}"
    previous = _guard_existing_playlist(db, playlist_id, current_user)
    db.upsert_playlist(
        playlist_id,
        name=str(source_pl.get("title") or pid),
        service=record.kind,
        description=str(source_pl.get("description") or ""),
        creator_id=actor_id(current_user),
        tracks_json=json.dumps(tracks),
    )
    if previous is None:
        forced_mode = initial_monitor_mode_forced(current_user)
        if forced_mode is not None:
            db.set_playlist_monitor_mode(playlist_id, forced_mode)
    server_user = (user or str(source_pl.get("owner") or "")).strip()
    db.set_server_playlist_source(playlist_id, server_id, pid, server_user)

    created = db.get_playlist(playlist_id)
    if created is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to adopt playlist")
    created["targets"] = db.get_playlist_targets(playlist_id)
    created["excluded_servers"] = sorted(db.get_playlist_server_excludes(playlist_id))
    return created
