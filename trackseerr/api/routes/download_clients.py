"""REST API endpoints for managing download clients and connection testing."""

import json
import logging
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from trackseerr.api.response_models import ApiModel

from trackseerr.api.dependencies import get_db, require_admin
from trackseerr.api.schemas.download_clients import DeletedResponse
from trackseerr.redaction import redact_text
from trackseerr.clients.acquisition import get_acquisition_driver
from trackseerr.download_roots import describe_client_roots
from trackseerr.models import DownloadClientConfig, DownloadDriverType
from trackseerr.security import MASK_MARKER, is_safe_service_url, mask_secret, resolve_masked_value
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class DownloadClientItem(ApiModel):
    id: str
    name: str
    driver_type: str
    host_url: str
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True
    priority: int = 1
    category: Optional[str] = None
    remote_path_mappings: Optional[list[dict[str, str]]] = None
    extra_settings_json: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DownloadClientPayload(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    driver_type: str
    host_url: str
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True
    priority: int = Field(1, ge=1, le=50)
    category: Optional[str] = None
    remote_path_mappings: Optional[list[dict[str, str]]] = None
    extra_settings_json: Optional[str] = None


class TestConnectionPayload(BaseModel):
    id: Optional[str] = None
    driver_type: str
    host_url: str
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    category: Optional[str] = None
    remote_path_mappings: Optional[list[dict[str, str]]] = None
    extra_settings_json: Optional[str] = None


class TestConnectionResponse(ApiModel):
    success: bool
    message: str


def _missing_credentials_message(
    driver_type: str, api_key: Optional[str], username: Optional[str], password: Optional[str]
) -> Optional[str]:
    """Returns an error message when the driver type's required credentials are absent."""
    if driver_type == "deluge" and not password:
        return "Password is required for Deluge"
    if driver_type == "nzbget" and (not username or not password):
        return "Username and password are required for NZBGet"
    if driver_type == "slskd" and not api_key:
        return "slskd needs an API key"
    return None


def _verify_connection(cfg: dict[str, Any]) -> tuple[bool, str]:
    """Builds the driver for ``cfg`` and runs its connection test; the single seam tests patch."""
    try:
        driver = get_acquisition_driver(cfg)
        return driver.test_connection()
    except (httpx.HTTPError, ValueError, RuntimeError, OSError) as e:
        logger.warning("Download client connection test failed: %s", redact_text(str(e)))
        return False, f"Connection failed: {redact_text(str(e))}"


def _resolve_test_secrets(
    payload: "TestConnectionPayload", db: Database
) -> tuple[Optional[str], Optional[str]]:
    """Resolves masked/omitted secrets in a test payload against the stored client named by ``payload.id``.

    Raises ValueError when a masked placeholder cannot be resolved.
    """
    api_key, password = payload.api_key, payload.password
    stored = db.get_download_client(payload.id.strip()) if payload.id and payload.id.strip() else None
    if stored:
        api_key = resolve_masked_value(api_key, stored.get("api_key"), mask_secret(stored.get("api_key")), "api_key")
        password = resolve_masked_value(password, stored.get("password"), mask_secret(stored.get("password")), "password")
        return api_key or stored.get("api_key"), password or stored.get("password")
    if (api_key and MASK_MARKER in api_key) or (password and MASK_MARKER in password):
        raise ValueError("Masked placeholder submitted; enter the full value")
    return api_key, password


def _normalize_extra_settings(
    extra_json: Optional[str],
    category: Optional[str] = None,
    remote_path_mappings: Optional[list[dict[str, str]]] = None,
) -> str:
    """Merges category and remote_path_mappings into extra_settings_json with defaults."""
    extra: dict[str, Any] = {}
    if extra_json:
        try:
            parsed = json.loads(extra_json)
            if isinstance(parsed, dict):
                extra = parsed
        except (json.JSONDecodeError, TypeError):
            extra = {}

    if category is not None:
        extra["category"] = category
    elif "category" not in extra:
        extra["category"] = "music"

    if remote_path_mappings is not None:
        extra["remote_path_mappings"] = remote_path_mappings
    elif "remote_path_mappings" not in extra:
        extra["remote_path_mappings"] = []

    return json.dumps(extra)


def _mask_client_dict(client: dict[str, Any]) -> dict[str, Any]:
    c = dict(client)
    if c.get("api_key"):
        c["api_key"] = mask_secret(c["api_key"])
    if c.get("password"):
        c["password"] = mask_secret(c["password"])

    extra_json = c.get("extra_settings_json")
    if extra_json:
        try:
            extra = json.loads(extra_json)
            c["category"] = extra.get("category", "music")
            c["remote_path_mappings"] = extra.get("remote_path_mappings", [])
        except (json.JSONDecodeError, TypeError):
            c["category"] = "music"
            c["remote_path_mappings"] = []
    else:
        c["category"] = "music"
        c["remote_path_mappings"] = []

    return c


@router.get("", response_model=list[DownloadClientItem], summary="List configured download clients")
@router.get("/", response_model=list[DownloadClientItem], include_in_schema=False)
def list_download_clients(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Lists all configured download clients with masked credentials."""
    clients = db.list_download_clients()
    return [_mask_client_dict(c) for c in clients]


class DownloadRootsItem(ApiModel):
    client_id: str
    name: str
    roots: list[str]
    error: Optional[str] = None


@router.get("/roots", response_model=list[DownloadRootsItem], summary="Download folders reported by each client")
def list_download_roots(
    refresh: bool = False,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Completed-download folders each enabled client reports (as TrackSeerr sees them after path mappings)."""
    return describe_client_roots(db, force=refresh)


@router.post("", response_model=DownloadClientItem, summary="Create or update download client")
@router.post("/", response_model=DownloadClientItem, include_in_schema=False)
def create_or_update_download_client(
    payload: DownloadClientPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only endpoint to save or update a download client."""
    # SSRF Validation
    clean_host = payload.host_url.strip()
    if not is_safe_service_url(clean_host):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Prohibited or invalid host URL (SSRF protection)",
        )

    # Validate driver type
    valid_types = {t.value for t in DownloadDriverType}
    clean_type = payload.driver_type.strip().lower()
    if clean_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid driver type '{payload.driver_type}'. Supported: {', '.join(sorted(valid_types))}",
        )

    client_id = payload.id.strip() if payload.id and payload.id.strip() else str(uuid.uuid4())
    existing = db.get_download_client(client_id)

    # Preserve secret if masked or omitted during edit
    api_key = payload.api_key
    password = payload.password
    if existing:
        try:
            api_key = resolve_masked_value(api_key, existing.get("api_key"), mask_secret(existing.get("api_key")), "api_key")
            password = resolve_masked_value(password, existing.get("password"), mask_secret(existing.get("password")), "password")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        if not api_key:
            api_key = existing.get("api_key")
        if not password:
            password = existing.get("password")
    elif (api_key and "•••" in api_key) or (password and "•••" in password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Masked placeholder submitted for a new client; enter the full value")

    # Validate per-type required credentials
    effective_username = payload.username.strip() if payload.username else (existing.get("username") if existing else None)
    missing = _missing_credentials_message(clean_type, api_key, effective_username, password)
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=missing)

    extra_settings = _normalize_extra_settings(
        payload.extra_settings_json,
        category=payload.category,
        remote_path_mappings=payload.remote_path_mappings,
    )

    if payload.enabled:
        ok, msg = _verify_connection(
            {
                "driver_type": clean_type,
                "host_url": clean_host,
                "api_key": api_key,
                "username": effective_username,
                "password": password,
                "extra_settings_json": extra_settings,
            }
        )
        if not ok:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Connection test failed: {msg}")

    config = DownloadClientConfig(
        id=client_id,
        name=payload.name.strip(),
        driver_type=clean_type,
        host_url=clean_host,
        api_key=api_key,
        username=payload.username.strip() if payload.username else None,
        password=password,
        enabled=payload.enabled,
        priority=payload.priority,
        extra_settings_json=extra_settings,
    )

    saved = db.create_download_client(config)
    return _mask_client_dict(saved)


@router.post("/test", response_model=TestConnectionResponse, summary="Test download client connection")
def test_download_client_connection(
    payload: TestConnectionPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> TestConnectionResponse:
    """Tests connectivity to a download client before saving."""
    clean_host = payload.host_url.strip()
    if not is_safe_service_url(clean_host):
        return TestConnectionResponse(
            success=False,
            message="Prohibited or invalid host URL (SSRF defense)",
        )

    clean_type = payload.driver_type.strip().lower()
    valid_types = {t.value for t in DownloadDriverType}
    if clean_type not in valid_types:
        return TestConnectionResponse(
            success=False,
            message=f"Invalid driver type '{payload.driver_type}'",
        )

    try:
        api_key, password = _resolve_test_secrets(payload, db)
    except ValueError as exc:
        return TestConnectionResponse(success=False, message=str(exc))

    missing = _missing_credentials_message(clean_type, api_key, payload.username, password)
    if missing:
        return TestConnectionResponse(success=False, message=missing)

    extra_settings = _normalize_extra_settings(
        payload.extra_settings_json,
        category=payload.category,
        remote_path_mappings=payload.remote_path_mappings,
    )

    success, msg = _verify_connection(
        {
            "driver_type": clean_type,
            "host_url": clean_host,
            "api_key": api_key,
            "username": payload.username,
            "password": password,
            "extra_settings_json": extra_settings,
        }
    )
    return TestConnectionResponse(success=success, message=msg)


@router.delete("/{client_id}", response_model=DeletedResponse, response_model_exclude_unset=True, summary="Delete download client")
def delete_download_client(
    client_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only deletion of a download client."""
    deleted = db.delete_download_client(client_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download client '{client_id}' not found",
        )
    return {"status": "deleted", "id": client_id}
