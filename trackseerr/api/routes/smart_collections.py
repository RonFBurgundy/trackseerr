"""Smart collection endpoints for rule-based library playlists."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_media_client,
    require_admin,
    require_core_tier,
)
from trackseerr.api.routes.library._shared import native_only
from trackseerr.api.routes.playlists import _sync_stored_playlist
from trackseerr.api.schemas.playlists import PlaylistImportResponse
from trackseerr.api.schemas.smart_collections import (
    SmartCollectionCreateRequest,
    SmartCollectionRecord,
    SmartCollectionUpdateRequest,
    SmartPreviewResponse,
    SmartPreviewTrack,
    SmartRulesBody,
)
from trackseerr.config import Config
from trackseerr.playlist_policy import SMART_KIND, SMART_SERVICE, is_smart_collection
from trackseerr.security import sanitize_text
from trackseerr.smart_collections import (
    SmartCollectionError,
    SmartRules,
    count,
    evaluate,
    refresh_tracks,
)
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[
        Depends(require_core_tier),
        Depends(require_admin),
        Depends(native_only),
    ]
)


@router.post("/preview", response_model=SmartPreviewResponse, response_model_exclude_unset=True)
def preview_smart_collection(
    body: SmartRulesBody,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Previews matching tracks and total count for given smart collection rules."""
    try:
        rules = body.to_smart_rules()
    except (SmartCollectionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    total_count = count(db, rules)

    # tracks = first min(limit, 25) in the chosen order
    sample_limit = min(rules.limit, 25)
    sample_rules = SmartRules(filter=rules.filter, sort=rules.sort, limit=sample_limit)
    sample_tracks = evaluate(db, sample_rules)

    return {
        "track_count": total_count,
        "tracks": [
            {
                "title": t["title"],
                "artist": t["artist"],
                "album": t["album"],
                "year": t["year"],
            }
            for t in sample_tracks
        ],
    }


@router.post(
    "",
    response_model=PlaylistImportResponse,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
)
def create_smart_collection(
    req: SmartCollectionCreateRequest,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Creates a rule-based smart collection playlist."""
    user_id = str(current_user["id"])
    try:
        rules = req.rules.to_smart_rules()
    except (SmartCollectionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Validate target users
    targets = req.targets if req.targets else [user_id]
    for tid in targets:
        if not db.get_user(tid):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Target user '{tid}' does not exist",
            )

    # Evaluate tracks
    try:
        from trackseerr.smart_collections import evaluate_tracks
        tracks = evaluate_tracks(db, rules)
    except (SmartCollectionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not tracks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The collection matched no tracks",
        )

    playlist_id = f"smart_{uuid.uuid4().hex}"
    name = sanitize_text(req.name)
    description = sanitize_text(req.description)
    tracks_json = json.dumps([{"title": t.title, "artist": t.artist, "album": t.album} for t in tracks])

    db.upsert_playlist(
        playlist_id=playlist_id,
        name=name,
        service=SMART_SERVICE,
        description=description,
        enabled=req.keep_in_sync,
        creator_id=user_id,
        tracks_json=tracks_json,
    )
    db.set_playlist_source(playlist_id, SMART_KIND, rules.to_json())
    db.set_playlist_monitor_mode(playlist_id, "none")
    db.set_playlist_auto_request(playlist_id, False)
    db.set_playlist_targets(playlist_id, targets)

    matched_count, missing_count = _sync_stored_playlist(
        db, config, plex_client, playlist_id, name, description, "", tracks, targets
    )
    return {
        "id": playlist_id,
        "name": name,
        "service": SMART_SERVICE,
        "track_count": len(tracks),
        "matched_count": matched_count,
        "missing_count": missing_count,
        "targets": targets,
        "status": "synced",
    }


def _get_smart_collection_or_404(db: Database, playlist_id: str) -> dict[str, Any]:
    playlist = db.get_playlist(playlist_id)
    if not playlist or not is_smart_collection(playlist):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Smart collection not found",
        )
    return playlist


@router.get(
    "/{playlist_id}",
    response_model=SmartCollectionRecord,
    response_model_exclude_unset=True,
)
def get_smart_collection(
    playlist_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Gets details of an existing smart collection."""
    pl = _get_smart_collection_or_404(db, playlist_id)
    raw_rules = pl.get("source_ref") or ""
    try:
        rules = SmartRules.from_json(raw_rules)
    except SmartCollectionError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    targets = db.get_playlist_targets(playlist_id)
    tracks_json = pl.get("tracks_json")
    track_count = len(json.loads(tracks_json)) if tracks_json else 0

    return {
        "id": pl["id"],
        "name": pl["name"],
        "description": pl.get("description", ""),
        "enabled": bool(pl.get("enabled")),
        "rules": SmartRulesBody.from_smart_rules(rules),
        "targets": targets,
        "last_synced_at": pl.get("last_synced_at"),
        "sync_status": pl.get("sync_status", "never_synced"),
        "track_count": track_count,
    }


@router.put(
    "/{playlist_id}",
    response_model=SmartCollectionRecord,
    response_model_exclude_unset=True,
)
def update_smart_collection(
    playlist_id: str,
    req: SmartCollectionUpdateRequest,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Updates a smart collection's name, description, or rules, re-evaluates, and pushes sync."""
    if req.name is None and req.description is None and req.rules is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided for update",
        )

    pl = _get_smart_collection_or_404(db, playlist_id)

    new_name = sanitize_text(req.name) if req.name is not None else pl["name"]
    new_desc = sanitize_text(req.description) if req.description is not None else pl.get("description", "")

    if req.rules is not None:
        try:
            rules = req.rules.to_smart_rules()
        except (SmartCollectionError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    else:
        try:
            rules = SmartRules.from_json(pl.get("source_ref") or "")
        except SmartCollectionError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    from trackseerr.smart_collections import evaluate_tracks
    try:
        tracks = evaluate_tracks(db, rules)
    except (SmartCollectionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not tracks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The collection matched no tracks",
        )

    tracks_json = json.dumps([{"title": t.title, "artist": t.artist, "album": t.album} for t in tracks])

    # Pass current enabled and creator_id through
    db.upsert_playlist(
        playlist_id=playlist_id,
        name=new_name,
        service=SMART_SERVICE,
        description=new_desc,
        enabled=bool(pl.get("enabled")),
        creator_id=pl.get("creator_id"),
        tracks_json=tracks_json,
    )
    if req.rules is not None:
        db.set_playlist_source(playlist_id, SMART_KIND, rules.to_json())

    targets = db.get_playlist_targets(playlist_id)
    _sync_stored_playlist(
        db, config, plex_client, playlist_id, new_name, new_desc, "", tracks, targets
    )

    updated_pl = db.get_playlist(playlist_id) or pl
    return {
        "id": playlist_id,
        "name": new_name,
        "description": new_desc,
        "enabled": bool(updated_pl.get("enabled")),
        "rules": SmartRulesBody.from_smart_rules(rules),
        "targets": targets,
        "last_synced_at": updated_pl.get("last_synced_at"),
        "sync_status": updated_pl.get("sync_status", "never_synced"),
        "track_count": len(tracks),
    }


@router.post(
    "/{playlist_id}/sync",
    response_model=PlaylistImportResponse,
    response_model_exclude_unset=True,
)
def sync_smart_collection(
    playlist_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Forces a one-time evaluation and push for a smart collection."""
    pl = _get_smart_collection_or_404(db, playlist_id)

    try:
        tracks = refresh_tracks(db, pl)
    except SmartCollectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    targets = db.get_playlist_targets(playlist_id)
    matched_count, missing_count = _sync_stored_playlist(
        db,
        config,
        plex_client,
        playlist_id,
        pl["name"],
        pl.get("description", ""),
        pl.get("poster_url", ""),
        tracks,
        targets,
    )

    return {
        "id": playlist_id,
        "name": pl["name"],
        "service": SMART_SERVICE,
        "track_count": len(tracks),
        "matched_count": matched_count,
        "missing_count": missing_count,
        "targets": targets,
        "status": "synced",
    }
