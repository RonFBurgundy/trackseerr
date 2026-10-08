"""Lidarr-compatible indexer API endpoints for Prowlarr synchronization.

Allows Prowlarr to manage TrackSeerr as a Lidarr application:
Settings -> Apps -> Lidarr.
Server URL = TrackSeerr core URL (not the public gateway).
API key = TrackSeerr API key.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from plex_playlist_sync import __version__
from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier
from plex_playlist_sync.clients.acquisition import get_indexer_driver
from plex_playlist_sync.models import IndexerConfig
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.security import is_safe_service_url
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

# Lidarr version reported to satisfy Prowlarr's MinimumApplicationVersion (1.0.2.0)
LIDARR_COMPAT_VERSION = "2.9.6.4552"

router = APIRouter(
    dependencies=[Depends(require_admin), Depends(require_core_tier)],
)


class LidarrFieldModel(BaseModel):
    name: str
    value: Any = None
    order: Optional[int] = None
    label: Optional[str] = None
    type: Optional[str] = None
    advanced: Optional[bool] = None
    section: Optional[str] = None
    hidden: Optional[str] = None


class LidarrIndexerItem(BaseModel):
    id: int
    name: str
    enableRss: bool = True
    enableAutomaticSearch: bool = True
    enableInteractiveSearch: bool = True
    priority: int = 25
    implementationName: str = "Torznab"
    implementation: str = "Torznab"
    configContract: str = "TorznabSettings"
    protocol: str = "torrent"
    infoLink: Optional[str] = None
    downloadClientId: Optional[int] = 0
    tags: list[int] = Field(default_factory=list)
    fields: list[LidarrFieldModel] = Field(default_factory=list)


class SystemStatusResponse(BaseModel):
    appName: str = "TrackSeerr"
    instanceName: str = "TrackSeerr"
    version: str = LIDARR_COMPAT_VERSION
    trackseerrVersion: str
    status: str = "ok"
    isProduction: bool = True
    isAdmin: bool = True


class LidarrIndexerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    name: Optional[str] = None
    enableRss: Optional[bool] = None
    enableAutomaticSearch: Optional[bool] = None
    enableInteractiveSearch: Optional[bool] = None
    priority: Optional[int] = 25
    implementation: Optional[str] = "Torznab"
    implementationName: Optional[str] = None
    configContract: Optional[str] = None
    protocol: Optional[str] = None
    downloadClientId: Optional[int] = None
    tags: Optional[list[int]] = None
    fields: list[dict[str, Any]] = Field(default_factory=list)


TORZNAB_SCHEMA: dict[str, Any] = {
    "id": 0,
    "name": "",
    "enableRss": True,
    "enableAutomaticSearch": True,
    "enableInteractiveSearch": True,
    "priority": 25,
    "implementationName": "Torznab",
    "implementation": "Torznab",
    "configContract": "TorznabSettings",
    "protocol": "torrent",
    "downloadClientId": 0,
    "tags": [],
    "fields": [
        {"name": "baseUrl", "value": "", "order": 0, "label": "URL", "type": "textbox"},
        {"name": "apiPath", "value": "/api", "order": 1, "label": "API Path", "type": "textbox"},
        {"name": "apiKey", "value": "", "order": 2, "label": "API Key", "type": "textbox"},
        {"name": "categories", "value": [3000, 3010, 3030, 3040], "order": 3, "label": "Categories", "type": "select"},
        {"name": "earlyReleaseLimit", "value": None, "order": 4, "label": "Early Release Limit", "type": "textbox"},
        {"name": "additionalParameters", "value": None, "order": 5, "label": "Additional Parameters", "type": "textbox"},
        {"name": "minimumSeeders", "value": None, "order": 6, "label": "Minimum Seeders", "type": "textbox"},
        {"name": "seedCriteria.seedRatio", "value": None, "order": 7, "label": "Seed Ratio", "type": "textbox"},
        {"name": "seedCriteria.seedTime", "value": None, "order": 8, "label": "Seed Time", "type": "textbox"},
        {"name": "seedCriteria.discographySeedTime", "value": None, "order": 9, "label": "Discography Seed Time", "type": "textbox"},
        {"name": "rejectBlocklistedTorrentHashesWhileGrabbing", "value": False, "order": 10, "label": "Reject Blocklisted Torrent Hashes While Grabbing", "type": "checkbox"},
    ],
}

NEWZNAB_SCHEMA: dict[str, Any] = {
    "id": 0,
    "name": "",
    "enableRss": True,
    "enableAutomaticSearch": True,
    "enableInteractiveSearch": True,
    "priority": 25,
    "implementationName": "Newznab",
    "implementation": "Newznab",
    "configContract": "NewznabSettings",
    "protocol": "usenet",
    "downloadClientId": 0,
    "tags": [],
    "fields": [
        {"name": "baseUrl", "value": "", "order": 0, "label": "URL", "type": "textbox"},
        {"name": "apiPath", "value": "/api", "order": 1, "label": "API Path", "type": "textbox"},
        {"name": "apiKey", "value": "", "order": 2, "label": "API Key", "type": "textbox"},
        {"name": "categories", "value": [3000, 3010, 3030, 3040], "order": 3, "label": "Categories", "type": "select"},
        {"name": "earlyReleaseLimit", "value": None, "order": 4, "label": "Early Release Limit", "type": "textbox"},
        {"name": "additionalParameters", "value": None, "order": 5, "label": "Additional Parameters", "type": "textbox"},
    ],
}


def _validation_error_response(property_name: str, message: str) -> JSONResponse:
    """Returns a Lidarr-formatted validation error array (status 400)."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=[{"propertyName": property_name, "errorMessage": message, "severity": "error"}],
        headers={"X-Application-Version": LIDARR_COMPAT_VERSION},
    )


def _row_to_lidarr_indexer(row: dict[str, Any]) -> dict[str, Any]:
    indexer_type = (row.get("indexer_type") or "torznab").strip().lower()
    is_torrent = indexer_type == "torznab"
    impl_name = "Torznab" if is_torrent else "Newznab"
    config_contract = "TorznabSettings" if is_torrent else "NewznabSettings"
    protocol = "torrent" if is_torrent else "usenet"
    enabled = bool(row.get("enabled", 1))

    # Parse categories CSV into list of integers
    categories_raw = str(row.get("categories") or "3000,3010,3030,3040")
    cats: list[int] = []
    for c in categories_raw.split(","):
        c_clean = c.strip()
        if c_clean:
            try:
                cats.append(int(c_clean))
            except ValueError:
                pass
    if not cats:
        cats = [3000, 3010, 3030, 3040]

    fields: list[dict[str, Any]] = [
        {"name": "baseUrl", "value": row.get("host_url", "")},
        {"name": "apiPath", "value": "/api"},
        {"name": "apiKey", "value": row.get("api_key") or ""},
        {"name": "categories", "value": cats},
        {"name": "earlyReleaseLimit", "value": None},
        {"name": "additionalParameters", "value": None},
    ]

    if is_torrent:
        fields.extend(
            [
                {"name": "minimumSeeders", "value": row.get("minimum_seeders")},
                {"name": "seedCriteria.seedRatio", "value": row.get("seed_ratio")},
                {"name": "seedCriteria.seedTime", "value": row.get("seed_time_minutes")},
                {"name": "seedCriteria.discographySeedTime", "value": row.get("discography_seed_time_minutes")},
                {"name": "rejectBlocklistedTorrentHashesWhileGrabbing", "value": False},
            ]
        )

    # Using SQLite rowid as integer ID (stable: the app never VACUUMs and backups use the sqlite backup API)
    return {
        "id": int(row["rowid"]),
        "name": row.get("name", ""),
        "enableRss": enabled,
        "enableAutomaticSearch": enabled,
        "enableInteractiveSearch": enabled,
        "priority": int(row.get("priority", 25)),
        "implementationName": impl_name,
        "implementation": impl_name,
        "configContract": config_contract,
        "protocol": protocol,
        "downloadClientId": 0,
        "tags": [],
        "fields": fields,
    }


def _parse_and_validate_indexer_payload(
    payload: LidarrIndexerPayload,
) -> tuple[Optional[dict[str, Any]], Optional[JSONResponse]]:
    field_map: dict[str, Any] = {}
    for f in payload.fields:
        if isinstance(f, dict) and "name" in f:
            field_map[str(f["name"])] = f.get("value")

    # 1. Name validation (1-120 characters)
    name = payload.name or ""
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= 120):
        return None, _validation_error_response("name", "Name must be between 1 and 120 characters")
    name = name.strip()

    # 2. baseUrl validation (SSRF check with LAN allowed; preserve trailing slash)
    raw_host = field_map.get("baseUrl")
    if not raw_host or not isinstance(raw_host, str) or not raw_host.strip():
        return None, _validation_error_response("baseUrl", "baseUrl is required")
    host_url = raw_host.strip()
    if not is_safe_service_url(host_url):
        return None, _validation_error_response("baseUrl", "Prohibited or invalid host URL (SSRF protection)")

    # 3. apiPath validation (must be "/api")
    api_path = field_map.get("apiPath")
    if api_path != "/api":
        return None, _validation_error_response("apiPath", "apiPath must be /api")

    # 4. apiKey (unmasked)
    raw_key = field_map.get("apiKey")
    api_key = str(raw_key).strip() if raw_key is not None and str(raw_key).strip() else None

    # 5. categories validation (CSV of ints)
    raw_cats = field_map.get("categories")
    if raw_cats is None:
        categories_str = "3000,3010,3030,3040"
    elif isinstance(raw_cats, (list, tuple)):
        try:
            cat_ints = [int(c) for c in raw_cats]
            categories_str = ",".join(str(c) for c in cat_ints)
        except (ValueError, TypeError):
            return None, _validation_error_response("categories", "Categories must be integers")
    elif isinstance(raw_cats, int):
        categories_str = str(raw_cats)
    elif isinstance(raw_cats, str):
        parts = [c.strip() for c in raw_cats.split(",") if c.strip()]
        try:
            cat_ints = [int(c) for c in parts]
            categories_str = ",".join(str(c) for c in cat_ints)
        except ValueError:
            return None, _validation_error_response("categories", "Categories must be integers")
    else:
        return None, _validation_error_response("categories", "Categories must be integers")

    # 6. enabled flags
    # enable* flags -> enabled = any of enableRss/enableAutomaticSearch/enableInteractiveSearch (return all three = enabled)
    if payload.enableRss is None and payload.enableAutomaticSearch is None and payload.enableInteractiveSearch is None:
        enabled = True
    else:
        enabled = bool(payload.enableRss or payload.enableAutomaticSearch or payload.enableInteractiveSearch)

    # 7. priority (Lidarr 1-50, default 25)
    priority = int(payload.priority) if payload.priority is not None else 25
    priority = max(1, min(50, priority))

    # 8. implementation / indexer_type
    impl = (payload.implementation or "Torznab").strip().lower()
    indexer_type = "newznab" if impl == "newznab" else "torznab"

    # 9. seed fields
    minimum_seeders: Optional[int] = None
    raw_seeders = field_map.get("minimumSeeders")
    if raw_seeders is not None and str(raw_seeders).strip() != "":
        try:
            val = int(raw_seeders)
            if val < 0:
                return None, _validation_error_response("minimumSeeders", "minimumSeeders must be non-negative")
            minimum_seeders = val
        except (ValueError, TypeError):
            return None, _validation_error_response("minimumSeeders", "minimumSeeders must be an integer")

    seed_ratio: Optional[float] = None
    raw_ratio = field_map.get("seedCriteria.seedRatio")
    if raw_ratio is not None and str(raw_ratio).strip() != "":
        try:
            r_val = float(raw_ratio)
            if r_val < 0:
                return None, _validation_error_response("seedCriteria.seedRatio", "seedRatio must be non-negative")
            seed_ratio = r_val
        except (ValueError, TypeError):
            return None, _validation_error_response("seedCriteria.seedRatio", "seedRatio must be a number")

    seed_time_minutes: Optional[int] = None
    raw_time = field_map.get("seedCriteria.seedTime")
    if raw_time is not None and str(raw_time).strip() != "":
        try:
            t_val = int(raw_time)
            if t_val < 0:
                return None, _validation_error_response("seedCriteria.seedTime", "seedTime must be non-negative")
            seed_time_minutes = t_val
        except (ValueError, TypeError):
            return None, _validation_error_response("seedCriteria.seedTime", "seedTime must be an integer")

    discography_seed_time_minutes: Optional[int] = None
    raw_disco = field_map.get("seedCriteria.discographySeedTime")
    if raw_disco is not None and str(raw_disco).strip() != "":
        try:
            d_val = int(raw_disco)
            if d_val < 0:
                return None, _validation_error_response("seedCriteria.discographySeedTime", "discographySeedTime must be non-negative")
            discography_seed_time_minutes = d_val
        except (ValueError, TypeError):
            return None, _validation_error_response("seedCriteria.discographySeedTime", "discographySeedTime must be an integer")

    parsed = {
        "name": name,
        "indexer_type": indexer_type,
        "host_url": host_url,
        "api_key": api_key,
        "categories": categories_str,
        "enabled": enabled,
        "priority": priority,
        "minimum_seeders": minimum_seeders,
        "seed_ratio": seed_ratio,
        "seed_time_minutes": seed_time_minutes,
        "discography_seed_time_minutes": discography_seed_time_minutes,
    }
    return parsed, None


@router.get("/system/status", response_model=SystemStatusResponse, summary="Lidarr-compatible system status")
def get_system_status(response: Response) -> dict[str, Any]:
    """Returns application status satisfying Prowlarr's Lidarr version check."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    return {
        "appName": "TrackSeerr",
        "instanceName": "TrackSeerr",
        "version": LIDARR_COMPAT_VERSION,
        "trackseerrVersion": __version__,
        "status": "ok",
        "isProduction": True,
        "isAdmin": True,
    }


@router.get("/indexer/schema", response_model=list[LidarrIndexerItem], summary="List indexer schemas")
def get_indexer_schema(response: Response) -> list[dict[str, Any]]:
    """Returns Torznab and Newznab indexer schema templates."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    return [TORZNAB_SCHEMA, NEWZNAB_SCHEMA]


@router.get("/indexer", response_model=list[LidarrIndexerItem], summary="List all indexers")
@router.get("/indexer/", response_model=list[LidarrIndexerItem], include_in_schema=False)
def list_indexers(
    response: Response,
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Returns all indexers mapped to Lidarr format with integer IDs and unmasked API keys."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    rows = db.list_indexers_with_rowid()
    return [_row_to_lidarr_indexer(r) for r in rows]


@router.get("/indexer/{indexer_rowid}", response_model=LidarrIndexerItem, summary="Get indexer by ID")
def get_indexer(
    indexer_rowid: int,
    response: Response,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Retrieves an indexer by SQLite rowid."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    row = db.get_indexer_by_rowid(indexer_rowid)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Indexer {indexer_rowid} not found",
        )
    return _row_to_lidarr_indexer(row)


@router.post("/indexer", response_model=LidarrIndexerItem, summary="Create indexer")
@router.post("/indexer/", response_model=LidarrIndexerItem, include_in_schema=False)
def create_indexer(
    payload: LidarrIndexerPayload,
    response: Response,
    force_save: bool = Query(default=False, alias="forceSave"),
    db: Database = Depends(get_db),
) -> Any:
    """Creates a new indexer from a Lidarr-formatted payload."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    parsed, err_resp = _parse_and_validate_indexer_payload(payload)
    if err_resp is not None:
        return err_resp
    assert parsed is not None

    indexer_id = str(uuid.uuid4())
    config = IndexerConfig(
        id=indexer_id,
        name=parsed["name"],
        indexer_type=parsed["indexer_type"],
        host_url=parsed["host_url"],
        api_key=parsed["api_key"],
        categories=parsed["categories"],
        enabled=parsed["enabled"],
        priority=parsed["priority"],
        minimum_seeders=parsed["minimum_seeders"],
        seed_ratio=parsed["seed_ratio"],
        seed_time_minutes=parsed["seed_time_minutes"],
        discography_seed_time_minutes=parsed["discography_seed_time_minutes"],
    )
    db.create_indexer(config)
    rowid = db.get_indexer_rowid(indexer_id)
    row = db.get_indexer_by_rowid(rowid) if rowid else None
    if not row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save indexer",
        )
    return _row_to_lidarr_indexer(row)


@router.put("/indexer/{indexer_rowid}", response_model=LidarrIndexerItem, summary="Update indexer")
def update_indexer(
    indexer_rowid: int,
    payload: LidarrIndexerPayload,
    response: Response,
    force_save: bool = Query(default=False, alias="forceSave"),
    db: Database = Depends(get_db),
) -> Any:
    """Updates an existing indexer identified by its SQLite rowid."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    existing = db.get_indexer_by_rowid(indexer_rowid)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Indexer {indexer_rowid} not found",
        )

    parsed, err_resp = _parse_and_validate_indexer_payload(payload)
    if err_resp is not None:
        return err_resp
    assert parsed is not None

    updates = {
        "name": parsed["name"],
        "indexer_type": parsed["indexer_type"],
        "host_url": parsed["host_url"],
        "api_key": parsed["api_key"],
        "categories": parsed["categories"],
        "enabled": parsed["enabled"],
        "priority": parsed["priority"],
        "minimum_seeders": parsed["minimum_seeders"],
        "seed_ratio": parsed["seed_ratio"],
        "seed_time_minutes": parsed["seed_time_minutes"],
        "discography_seed_time_minutes": parsed["discography_seed_time_minutes"],
    }
    db.update_indexer(existing["id"], updates)
    updated_row = db.get_indexer_by_rowid(indexer_rowid)
    if not updated_row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve updated indexer",
        )
    return _row_to_lidarr_indexer(updated_row)


@router.delete("/indexer/{indexer_rowid}", summary="Delete indexer")
def delete_indexer(
    indexer_rowid: int,
    response: Response,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Deletes an indexer by SQLite rowid."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION
    existing = db.get_indexer_by_rowid(indexer_rowid)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Indexer {indexer_rowid} not found",
        )
    db.delete_indexer_by_rowid(indexer_rowid)
    return {}


@router.post("/indexer/test", summary="Test indexer connection")
def test_indexer(
    payload: LidarrIndexerPayload,
    response: Response,
    force_test: bool = Query(default=False, alias="forceTest"),
) -> Any:
    """Tests connectivity against an indexer, matching Lidarr's test contract."""
    response.headers["X-Application-Version"] = LIDARR_COMPAT_VERSION

    field_map: dict[str, Any] = {}
    for f in payload.fields:
        if isinstance(f, dict) and "name" in f:
            field_map[str(f["name"])] = f.get("value")

    # Validate baseUrl
    raw_host = field_map.get("baseUrl")
    if not raw_host or not isinstance(raw_host, str) or not raw_host.strip():
        return _validation_error_response("baseUrl", "baseUrl is required")
    host_url = raw_host.strip()
    if not is_safe_service_url(host_url):
        return _validation_error_response("baseUrl", "Prohibited or invalid host URL (SSRF defense)")

    # Validate apiPath
    api_path = field_map.get("apiPath")
    if api_path != "/api":
        return _validation_error_response("apiPath", "apiPath must be /api")

    raw_key = field_map.get("apiKey")
    api_key = str(raw_key).strip() if raw_key is not None and str(raw_key).strip() else None

    # Categories
    raw_cats = field_map.get("categories")
    if raw_cats is None:
        categories_str = "3000,3010,3030,3040"
    elif isinstance(raw_cats, (list, tuple)):
        try:
            cat_ints = [int(c) for c in raw_cats]
            categories_str = ",".join(str(c) for c in cat_ints)
        except (ValueError, TypeError):
            return _validation_error_response("categories", "Categories must be integers")
    elif isinstance(raw_cats, int):
        categories_str = str(raw_cats)
    elif isinstance(raw_cats, str):
        parts = [c.strip() for c in raw_cats.split(",") if c.strip()]
        try:
            cat_ints = [int(c) for c in parts]
            categories_str = ",".join(str(c) for c in cat_ints)
        except ValueError:
            return _validation_error_response("categories", "Categories must be integers")
    else:
        return _validation_error_response("categories", "Categories must be integers")

    impl = (payload.implementation or "Torznab").strip().lower()
    indexer_type = "newznab" if impl == "newznab" else "torznab"

    try:
        driver = get_indexer_driver(
            {
                "indexer_type": indexer_type,
                "host_url": host_url,
                "api_key": api_key,
                "categories": categories_str,
            }
        )
        success, msg = driver.test_connection()
        if success:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={},
                headers={"X-Application-Version": LIDARR_COMPAT_VERSION},
            )
        else:
            return _validation_error_response("", redact_text(msg or "Connection test failed"))
    except Exception as exc:
        logger.warning("Lidarr compat test connection failed: %s", redact_text(str(exc)))
        return _validation_error_response("", f"Connection failed: {redact_text(str(exc))}")
