"""Tailored mixes API: per-user mix configs, preview, background generation and last result."""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel

from plex_playlist_sync.api.dependencies import (
    get_config,
    get_current_user,
    get_db,
    get_discovery_client,
    get_media_client,
)
from plex_playlist_sync.api.schemas.mixes import (
    MixConfig,
    MixGenerateResponse,
    MixPreviewResponse,
    MixResult,
)
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.request_submission import effective_quota_limits
from plex_playlist_sync.storage import Database
from plex_playlist_sync.tailored_mixes import (
    InsufficientHistoryError,
    compile_user_mix,
    default_mix_name,
    generate_and_sync,
)

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_MIXES_PER_USER = 10
GENERATE_COOLDOWN_SECONDS = 300

_state_lock = threading.Lock()
_create_locks: dict[str, threading.Lock] = {}
_generating: set[str] = set()
_last_generate_request: dict[str, float] = {}

PUBLIC_FIELDS = (
    "id",
    "user_id",
    "mix_type",
    "name",
    "seed_artist",
    "track_count",
    "discovery_ratio",
    "seed_window_days",
    "excluded_genres",
    "auto_acquire_missing",
    "max_weekly_acquisitions",
    "quality_profile_id",
    "enabled",
    "last_generated_at",
)


class MixCreateBody(BaseModel):
    mix_type: str
    name: Optional[str] = None
    seed_artist: Optional[str] = None
    track_count: int = 30
    discovery_ratio: float = 0.7
    seed_window_days: int = 14
    excluded_genres: list[str] = []
    auto_acquire_missing: bool = False
    max_weekly_acquisitions: int = 10
    quality_profile_id: Optional[str] = None
    enabled: bool = True
    user_id: Optional[str] = None


class MixUpdateBody(BaseModel):
    mix_type: Optional[str] = None
    name: Optional[str] = None
    seed_artist: Optional[str] = None
    track_count: Optional[int] = None
    discovery_ratio: Optional[float] = None
    seed_window_days: Optional[int] = None
    excluded_genres: Optional[list[str]] = None
    auto_acquire_missing: Optional[bool] = None
    max_weekly_acquisitions: Optional[int] = None
    quality_profile_id: Optional[str] = None
    enabled: Optional[bool] = None


def _is_admin(user: dict[str, Any]) -> bool:
    """Forwarded (gateway-proxied) principals never carry admin powers."""
    if user.get("forwarded"):
        return False
    return bool(user.get("is_admin"))


def _prune_last_generate_locked() -> None:
    """Drop cooldown entries past the window. Caller must hold ``_state_lock``."""
    cutoff = time.monotonic() - GENERATE_COOLDOWN_SECONDS
    for key in [k for k, ts in _last_generate_request.items() if ts < cutoff]:
        del _last_generate_request[key]


def _create_lock(user_id: str) -> threading.Lock:
    with _state_lock:
        return _create_locks.setdefault(user_id, threading.Lock())


def _clamp_weekly(db: Database, config: Config, owner: str, value: int) -> int:
    """Non-admin owners may not set a weekly acquisition cap above their track quota."""
    owner_row = db.get_user(owner)
    if owner_row is None or owner_row.get("is_admin"):
        return value
    return min(value, effective_quota_limits(db, owner)["tracks"])


def _seconds_since_iso(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in PUBLIC_FIELDS}


def _target_user(current_user: dict[str, Any], requested: Optional[str]) -> str:
    if requested and requested != current_user["id"] and _is_admin(current_user):
        return str(requested)
    return str(current_user["id"])


def _owned_mix(db: Database, mix_id: str, current_user: dict[str, Any]) -> dict[str, Any]:
    row = db.get_mix_config(mix_id)
    if row is None or (str(row["user_id"]) != str(current_user["id"]) and not _is_admin(current_user)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mix not found")
    return row


def _run_generation(
    db: Database,
    plex_client: Optional[PlexClient],
    discovery: DiscoveryClient,
    mix_id: str,
    config: Config,
) -> None:
    row = db.get_mix_config(mix_id)
    if row is None:
        with _state_lock:
            _generating.discard(mix_id)
        return
    try:
        generate_and_sync(db, plex_client, discovery, row, config)
    except InsufficientHistoryError as exc:
        logger.info("Mix %s generation skipped: %s", mix_id, exc)
    except Exception as exc:  # background task: nothing above to surface to; type only, messages can embed Plex tokens
        logger.error("Mix %s generation failed (%s)", mix_id, type(exc).__name__)  # no traceback/message: may embed Plex token URLs
    finally:
        with _state_lock:
            _generating.discard(mix_id)


@router.get("", response_model=list[MixConfig], response_model_exclude_unset=True)
def list_mixes(
    user_id: Optional[str] = None,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return [_public(r) for r in db.list_mix_configs(_target_user(current_user, user_id))]


@router.post("", response_model=MixConfig, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def create_mix(
    body: MixCreateBody,
    user_id: Optional[str] = None,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    owner = _target_user(current_user, body.user_id or user_id)
    is_admin = _is_admin(current_user)
    quality_profile_id = body.quality_profile_id if is_admin else None
    max_weekly = body.max_weekly_acquisitions if is_admin else _clamp_weekly(db, config, owner, body.max_weekly_acquisitions)
    if is_admin:
        return _create(db, owner, body, quality_profile_id, max_weekly, enforce_cap=False)
    with _create_lock(owner):
        return _create(db, owner, body, quality_profile_id, max_weekly, enforce_cap=True)


def _create(
    db: Database,
    owner: str,
    body: MixCreateBody,
    quality_profile_id: Optional[str],
    max_weekly: int,
    enforce_cap: bool,
) -> dict[str, Any]:
    if enforce_cap and len(db.list_mix_configs(owner)) >= MAX_MIXES_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Mix limit reached (maximum {MAX_MIXES_PER_USER} mixes)"
        )
    name = (body.name or "").strip() or default_mix_name(body.mix_type, body.seed_artist)
    try:
        row = db.create_mix_config(
            owner,
            body.mix_type,
            name,
            seed_artist=body.seed_artist,
            track_count=body.track_count,
            discovery_ratio=body.discovery_ratio,
            seed_window_days=body.seed_window_days,
            excluded_genres=body.excluded_genres,
            auto_acquire_missing=body.auto_acquire_missing,
            max_weekly_acquisitions=max_weekly,
            quality_profile_id=quality_profile_id,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        logger.warning("Mix create rejected by constraint: %s", exc)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown quality profile or user") from exc
    return _public(row)


@router.put("/{mix_id}", response_model=MixConfig, response_model_exclude_unset=True)
def update_mix(
    mix_id: str,
    body: MixUpdateBody,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    existing = _owned_mix(db, mix_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not _is_admin(current_user):
        fields.pop("quality_profile_id", None)
        if fields.get("max_weekly_acquisitions") is not None:
            fields["max_weekly_acquisitions"] = _clamp_weekly(
                db, config, str(existing["user_id"]), int(fields["max_weekly_acquisitions"])
            )
    try:
        row = db.update_mix_config(mix_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        logger.warning("Mix update rejected by constraint: %s", exc)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown quality profile") from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mix not found")
    return _public(row)


@router.delete("/{mix_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mix(
    mix_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> Response:
    _owned_mix(db, mix_id, current_user)
    db.delete_mix_config(mix_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{mix_id}/preview", response_model=MixPreviewResponse, response_model_exclude_unset=True)
def preview_mix(
    mix_id: str,
    db: Database = Depends(get_db),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    row = _owned_mix(db, mix_id, current_user)
    try:
        tracks = compile_user_mix(db, discovery, row)
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"tracks": [{"artist": t.artist, "title": t.title, "album": t.album, "origin": t.origin} for t in tracks]}


@router.post("/{mix_id}/generate", response_model=MixGenerateResponse, response_model_exclude_unset=True, status_code=status.HTTP_202_ACCEPTED)
def generate_mix(
    mix_id: str,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
    discovery: DiscoveryClient = Depends(get_discovery_client),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    row = _owned_mix(db, mix_id, current_user)
    with _state_lock:
        _prune_last_generate_locked()
        if mix_id in _generating:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A generation is already running for this mix")
        last_request_ts = _last_generate_request.get(mix_id)
    if not _is_admin(current_user):
        ages = [
            age
            for age in (
                _seconds_since_iso(row.get("last_generated_at")),
                (time.monotonic() - last_request_ts) if last_request_ts is not None else None,
            )
            if age is not None
        ]
        if ages and min(ages) < GENERATE_COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="This mix was generated recently; try again in a few minutes",
            )
    if row["mix_type"] != "artist_radio":
        window = int(row["seed_window_days"])
        if row["mix_type"] == "daily_blend":
            window = min(window, 3)
        since = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat(timespec="seconds")
        if not db.top_artists(str(row["user_id"]), since, limit=1):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Not enough listening history to build this mix yet",
            )
    with _state_lock:
        if mix_id in _generating:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A generation is already running for this mix")
        _generating.add(mix_id)
        _prune_last_generate_locked()
        _last_generate_request[mix_id] = time.monotonic()
    background_tasks.add_task(_run_generation, db, plex_client, discovery, mix_id, config)
    return {"status": "queued"}


@router.get("/{mix_id}/result", response_model=MixResult, response_model_exclude_unset=True)
def get_mix_result(
    mix_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    row = _owned_mix(db, mix_id, current_user)
    raw = row.get("last_result_json")
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No result yet")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Mix %s has a corrupt stored result: %s", mix_id, exc)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No result yet") from exc
