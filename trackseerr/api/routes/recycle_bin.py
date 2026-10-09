"""Recycle bin: manual empty and status. Admin only, core tier (deny-by-default on the gateway)."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from trackseerr import recycle_bin
from trackseerr.api.dependencies import get_db, require_admin, require_core_tier
from trackseerr.api.schemas.recycle_bin import RecycleBinEmptyResponse, RecycleBinStatus
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier)])


class EmptyRecycleBinRequest(BaseModel):
    confirm: bool = False


@router.post("/empty", response_model=RecycleBinEmptyResponse, response_model_exclude_unset=True, summary="Permanently delete everything in the recycle bin (explicit confirmation required)")
def empty_recycle_bin(body: EmptyRecycleBinRequest, db: Database = Depends(get_db)) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Emptying the recycle bin requires confirm=true")
    result = recycle_bin.run_cleanup(db, empty_all=True)
    logger.warning("Recycle bin emptied by an admin: %d item(s) removed", len(result.removed))
    return {"removed": len(result.removed), "errors": result.errors, "skipped_reason": result.skipped_reason}


@router.get("/status", response_model=RecycleBinStatus, response_model_exclude_unset=True, summary="Whether a cleanup is running, and the last run")
def get_recycle_bin_status() -> dict[str, Any]:
    return recycle_bin.get_status()
