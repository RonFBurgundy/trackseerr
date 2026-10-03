"""REST API endpoints for managing Torznab / Newznab indexers."""

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from plex_playlist_sync.api.dependencies import get_db, require_admin
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.clients.acquisition import get_indexer_driver
from plex_playlist_sync.models import IndexerConfig
from plex_playlist_sync.security import is_safe_service_url, mask_secret
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class IndexerItem(BaseModel):
    id: str
    name: str
    indexer_type: str
    host_url: str
    api_key: Optional[str] = None
    categories: str = "3000,3010,3020,3030,3040"
    enabled: bool = True
    priority: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class IndexerPayload(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    indexer_type: str = "torznab"
    host_url: str
    api_key: Optional[str] = None
    categories: str = "3000,3010,3020,3030,3040"
    enabled: bool = True
    priority: int = 1


class TestIndexerPayload(BaseModel):
    indexer_type: str = "torznab"
    host_url: str
    api_key: Optional[str] = None
    categories: str = "3000,3010,3020,3030,3040"


class TestIndexerResponse(BaseModel):
    success: bool
    message: str


def _mask_indexer_dict(indexer: dict[str, Any]) -> dict[str, Any]:
    idx = dict(indexer)
    if idx.get("api_key"):
        idx["api_key"] = mask_secret(idx["api_key"])
    return idx


@router.get("", response_model=list[IndexerItem], summary="List configured indexers")
@router.get("/", response_model=list[IndexerItem], include_in_schema=False)
def list_indexers(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Lists all configured indexers with masked API keys."""
    indexers = db.list_indexers()
    return [_mask_indexer_dict(i) for i in indexers]


@router.post("", response_model=IndexerItem, summary="Create or update indexer")
@router.post("/", response_model=IndexerItem, include_in_schema=False)
def create_or_update_indexer(
    payload: IndexerPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only endpoint to create or update an indexer."""
    clean_host = payload.host_url.strip()
    if not is_safe_service_url(clean_host):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Prohibited or invalid host URL (SSRF protection)",
        )

    indexer_id = payload.id.strip() if payload.id and payload.id.strip() else str(uuid.uuid4())
    existing = db.get_indexer(indexer_id)

    # Preserve secret if masked or omitted
    api_key = payload.api_key
    if api_key and "•••" in api_key and existing:
        api_key = existing.get("api_key")
    elif not api_key and existing:
        api_key = existing.get("api_key")

    config = IndexerConfig(
        id=indexer_id,
        name=payload.name.strip(),
        indexer_type=payload.indexer_type.strip().lower(),
        host_url=clean_host,
        api_key=api_key,
        categories=payload.categories.strip(),
        enabled=payload.enabled,
        priority=payload.priority,
    )

    saved = db.create_indexer(config)
    return _mask_indexer_dict(saved)


@router.post("/test", response_model=TestIndexerResponse, summary="Test indexer capabilities")
def test_indexer_connection(
    payload: TestIndexerPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> TestIndexerResponse:
    """Tests capabilities and connection against a Torznab/Newznab indexer."""
    clean_host = payload.host_url.strip()
    if not is_safe_service_url(clean_host):
        return TestIndexerResponse(
            success=False,
            message="Prohibited or invalid host URL (SSRF defense)",
        )

    try:
        driver = get_indexer_driver(
            {
                "indexer_type": payload.indexer_type,
                "host_url": clean_host,
                "api_key": payload.api_key,
                "categories": payload.categories,
            }
        )
        success, msg = driver.test_connection()
        return TestIndexerResponse(success=success, message=msg)
    except Exception as e:
        logger.warning("Indexer test failed: %s", redact_text(str(e)))
        return TestIndexerResponse(success=False, message=f"Connection failed: {redact_text(str(e))}")


@router.delete("/{indexer_id}", summary="Delete indexer")
def delete_indexer(
    indexer_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only deletion of an indexer."""
    deleted = db.delete_indexer(indexer_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Indexer '{indexer_id}' not found",
        )
    return {"status": "deleted", "id": indexer_id}
