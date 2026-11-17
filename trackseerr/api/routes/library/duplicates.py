"""Endpoints for album merges blocked by duplicate files: list them and resolve each with a choice."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from trackseerr import duplicate_choices
from trackseerr.api.dependencies import (
    get_db,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from trackseerr.api.routes.library._shared import native_only
from trackseerr.api.routes.library.models import (
    DuplicateChoicesResponse,
    DuplicateResolveRequest,
    DuplicateResolveResponse,
)
from trackseerr.redaction import redact_text, safe_exc
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/duplicates",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=DuplicateChoicesResponse,
)
def list_duplicates(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Lists albums whose merge is blocked, with the file pairs the admin has to choose between."""
    items = duplicate_choices.list_choices(db)
    return {"items": items, "total": len(items)}


@router.post(
    "/duplicates/{conflict_id}/resolve",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=DuplicateResolveResponse,
)
def resolve_duplicate(
    conflict_id: int,
    body: DuplicateResolveRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Applies one choice: keep the existing copy, the incoming copy, the better quality, or both."""
    try:
        return duplicate_choices.resolve_choice(db, conflict_id, body.choice)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except duplicate_choices.DisposalError as exc:
        logger.error("Duplicate %s could not be resolved: %s", conflict_id, safe_exc(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=redact_text(str(exc)))
