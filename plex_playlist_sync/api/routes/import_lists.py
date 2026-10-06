"""Import lists: remote lists (Last.fm, ListenBrainz, MusicBrainz collections) synced into the library.

Admin-only and Core-tier only. Secret provider settings (API keys, tokens) are returned as ``********``; a write
that sends the placeholder keeps the stored value.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from plex_playlist_sync.api.dependencies import get_config, get_db, require_admin, require_core_tier
from plex_playlist_sync.clients.import_lists import (
    PROVIDERS,
    SECRET_KEYS,
    SECRET_PLACEHOLDER,
    ImportListError,
    fetch_items,
    provider_metadata,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.import_list_worker import claim_sync, is_syncing, release_sync, sync_import_list
from plex_playlist_sync.library_monitoring import validate_list_monitor_mode, validate_monitor_option
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.storage import Database
from plex_playlist_sync.tag_store import normalize_labels

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier), Depends(require_admin)])

MIN_SYNC_INTERVAL_MINUTES = 60
MAX_SYNC_INTERVAL_MINUTES = 60 * 24 * 30
ITEM_STATUSES = ("pending", "applied", "unresolved", "skipped", "failed")
TEST_SAMPLE_SIZE = 10


class ImportListInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    provider: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    monitor_mode: str = "track"
    artist_monitor_option: Optional[str] = None
    quality_profile_id: Optional[str] = None
    sync_interval_minutes: int = Field(1440, ge=MIN_SYNC_INTERVAL_MINUTES, le=MAX_SYNC_INTERVAL_MINUTES)
    # Tag labels applied to every artist this list adds to the library (Lidarr list tags).
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        return normalize_labels(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        if value not in PROVIDERS:
            raise ValueError(f"provider must be one of: {', '.join(PROVIDERS)}")
        return value

    @field_validator("monitor_mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        return validate_list_monitor_mode(value)

    @field_validator("artist_monitor_option")
    @classmethod
    def _option(cls, value: Optional[str]) -> Optional[str]:
        return None if value in (None, "") else validate_monitor_option(value)

    @model_validator(mode="after")
    def _required_fields(self) -> "ImportListInput":
        for field in PROVIDERS[self.provider]["fields"]:
            if field["required"] and not str(self.config.get(field["key"]) or "").strip():
                raise ValueError(f"config.{field['key']} is required")
        return self


class ImportListItemCounts(BaseModel):
    applied: int = 0
    pending: int = 0
    unresolved: int = 0
    failed: int = 0
    skipped: int = 0


class ImportList(ImportListInput):
    id: str
    last_synced_at: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    item_counts: ImportListItemCounts = Field(default_factory=ImportListItemCounts)
    created_at: str
    updated_at: str


class ImportListItemOut(BaseModel):
    id: int = 0  # 0 only for test-sample rows, which are not stored
    kind: str
    mbid: Optional[str] = None
    artist_name: str = ""
    album_title: str = ""
    track_title: str = ""
    status: str = "pending"
    applied_level: Optional[str] = None
    error: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None


class ImportListItemsPage(BaseModel):
    items: list[ImportListItemOut]
    total: int


class ImportListTestResult(BaseModel):
    ok: bool
    item_count: int = 0
    sample: list[ImportListItemOut] = Field(default_factory=list)
    error: Optional[str] = None


def _mask(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    secrets = SECRET_KEYS.get(provider, ())
    return {k: (SECRET_PLACEHOLDER if k in secrets and str(v or "").strip() else v) for k, v in config.items()}


def _merge_secrets(provider: str, incoming: dict[str, Any], stored: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Incoming config with each ``********`` secret replaced by the stored value (or dropped if there is none)."""
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if key in SECRET_KEYS.get(provider, ()) and value == SECRET_PLACEHOLDER:
            if stored and stored.get(key):
                merged[key] = stored[key]
            continue
        merged[key] = value
    return merged


def _present(db: Database, row: dict[str, Any]) -> ImportList:
    counts = db.import_list_item_counts(row["id"])
    return ImportList(
        id=row["id"],
        name=row["name"],
        provider=row["provider"],
        config=_mask(row["provider"], row["config"]),
        enabled=row["enabled"],
        monitor_mode=row["monitor_mode"],
        artist_monitor_option=row.get("artist_monitor_option"),
        quality_profile_id=row.get("quality_profile_id"),
        sync_interval_minutes=row["sync_interval_minutes"],
        tags=row.get("tags", []),
        last_synced_at=row.get("last_synced_at"),
        last_status=row.get("last_status"),
        last_error=row.get("last_error"),
        item_counts=ImportListItemCounts(**counts),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _get_or_404(db: Database, list_id: str) -> dict[str, Any]:
    row = db.get_import_list(list_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import list not found")
    return row


def _payload(body: ImportListInput, stored: Optional[dict[str, Any]]) -> dict[str, Any]:
    data = body.model_dump()
    data["config"] = _merge_secrets(body.provider, body.config, stored["config"] if stored else None)
    return data


@router.get("/providers")
def list_providers() -> list[dict[str, Any]]:
    """Provider metadata the UI renders its forms from."""
    return provider_metadata()


@router.get("")
def list_import_lists(db: Database = Depends(get_db)) -> list[ImportList]:
    return [_present(db, row) for row in db.list_import_lists()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_import_list(body: ImportListInput, db: Database = Depends(get_db)) -> ImportList:
    return _present(db, db.create_import_list(_payload(body, None)))


@router.post("/test")
def test_import_list(
    body: ImportListInput,
    list_id: Optional[str] = Query(None, description="Existing list whose stored secrets replace ******** values"),
    db: Database = Depends(get_db),
) -> ImportListTestResult:
    """Fetches the list and reports what it would import. Reads only: nothing is written."""
    stored = db.get_import_list(list_id) if list_id else None
    # Stored secrets belong to the stored provider: they must never be sent to a different one.
    if stored is not None and stored["provider"] != body.provider:
        stored = None
    config = _merge_secrets(body.provider, body.config, stored["config"] if stored else None)
    try:
        items = fetch_items(
            body.provider, config, mb_base_url=db.get_media_management_settings().get("mb_mirror_url") or None
        )
    except ImportListError as exc:
        return ImportListTestResult(ok=False, error=str(exc))
    sample = [
        ImportListItemOut(
            kind=i.kind,
            mbid=i.mbid,
            artist_name=i.artist_name,
            album_title=i.album_title,
            track_title=i.track_title,
        )
        for i in items[:TEST_SAMPLE_SIZE]
    ]
    return ImportListTestResult(ok=True, item_count=len(items), sample=sample)


@router.get("/{list_id}")
def get_import_list(list_id: str, db: Database = Depends(get_db)) -> ImportList:
    return _present(db, _get_or_404(db, list_id))


@router.put("/{list_id}")
def update_import_list(list_id: str, body: ImportListInput, db: Database = Depends(get_db)) -> ImportList:
    stored = _get_or_404(db, list_id)
    # A provider change invalidates the old secrets: only same-provider placeholders may resolve to stored values.
    stored_for_merge = stored if stored["provider"] == body.provider else None
    data = body.model_dump()
    data["config"] = _merge_secrets(body.provider, body.config, stored_for_merge["config"] if stored_for_merge else None)
    updated = db.update_import_list(list_id, data)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import list not found")
    return _present(db, updated)


@router.delete("/{list_id}")
def delete_import_list(list_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    _get_or_404(db, list_id)
    db.delete_import_list(list_id)
    return {"status": "deleted", "id": list_id}


def _background_sync(db: Database, config: Config, list_id: str, token: str) -> None:
    try:
        sync_import_list(db, list_id, config, claim_token=token)
    except Exception as exc:  # background task: nowhere to raise to, so record the cause on the list
        logger.error("Manual import list sync of %s failed: %s", list_id, safe_exc(exc))
        logger.debug("Manual import list sync traceback", exc_info=True)
        try:
            db.set_import_list_sync_result(list_id, "error", safe_exc(exc))
        except Exception as db_exc:  # the original failure is already logged above
            logger.error("Could not record import list failure for %s: %s", list_id, safe_exc(db_exc))
    finally:
        release_sync(list_id, token)  # token-checked: a no-op once sync_import_list released this claim


@router.post("/{list_id}/sync")
def sync_now(
    list_id: str,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, bool]:
    _get_or_404(db, list_id)
    token = claim_sync(list_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This list is already syncing")
    try:
        background_tasks.add_task(_background_sync, db, config, list_id, token)
    except Exception:
        release_sync(list_id, token)
        raise
    return {"queued": True}


@router.get("/{list_id}/items")
def list_items(
    list_id: str,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> ImportListItemsPage:
    _get_or_404(db, list_id)
    if status_filter and status_filter not in ITEM_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {', '.join(ITEM_STATUSES)}",
        )
    rows, total = db.list_import_list_items(list_id, status_filter, limit, offset)
    return ImportListItemsPage(items=[ImportListItemOut(**r) for r in rows], total=total)


__all__ = ["router", "is_syncing"]
