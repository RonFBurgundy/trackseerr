"""REST API for delay profiles (``/api/settings/delay-profiles``)."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier), Depends(require_admin)])

MAX_DELAY_MIN = 60 * 24 * 365


class DelaysModel(BaseModel):
    usenet: int = Field(0, ge=0, le=MAX_DELAY_MIN)
    torrent: int = Field(0, ge=0, le=MAX_DELAY_MIN)
    soulseek: int = Field(0, ge=0, le=MAX_DELAY_MIN)


class DelayProfilePayload(BaseModel):
    name: str = Field("", max_length=120)
    preferred_protocol: Literal["usenet", "torrent", "soulseek"] = "usenet"
    delays: DelaysModel = Field(default_factory=DelaysModel)
    bypass_if_highest_quality: bool = True
    bypass_if_above_score: int | None = Field(None, ge=-100000, le=100000)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, tags: list[str]) -> list[str]:
        out: list[str] = []
        for t in tags:
            t = t.strip()
            if len(t) > 60:
                raise ValueError("tag too long")
            if t and t.lower() not in {x.lower() for x in out}:
                out.append(t)
        return out


class DelayProfileResponse(BaseModel):
    id: int
    order: int
    name: str
    preferred_protocol: str
    delays: DelaysModel
    bypass_if_highest_quality: bool
    bypass_if_above_score: int | None = None
    tags: list[str]
    is_default: bool


class ReorderPayload(BaseModel):
    ids: list[int]


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delay profile not found")


def _data(payload: DelayProfilePayload) -> dict[str, Any]:
    data = payload.model_dump()
    data["name"] = payload.name.strip() or "Delay profile"
    return data


@router.get("", response_model=list[DelayProfileResponse], summary="List delay profiles")
@router.get("/", response_model=list[DelayProfileResponse], include_in_schema=False)
def list_delay_profiles(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_delay_profiles()


@router.post("", response_model=DelayProfileResponse, summary="Create a delay profile")
@router.post("/", response_model=DelayProfileResponse, include_in_schema=False)
def create_delay_profile(payload: DelayProfilePayload, db: Database = Depends(get_db)) -> dict[str, Any]:
    return db.create_delay_profile(_data(payload))


@router.post("/reorder", response_model=list[DelayProfileResponse], summary="Reorder delay profiles")
def reorder_delay_profiles(payload: ReorderPayload, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    """``ids`` lists the non-default profiles in their new order (the default is always last and may be omitted)."""
    if not db.reorder_delay_profiles(payload.ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ids must list each non-default delay profile exactly once",
        )
    return db.list_delay_profiles()


@router.put("/{profile_id}", response_model=DelayProfileResponse, summary="Update a delay profile")
def update_delay_profile(profile_id: int, payload: DelayProfilePayload, db: Database = Depends(get_db)) -> dict[str, Any]:
    updated = db.update_delay_profile(profile_id, _data(payload))
    if updated is None:
        raise _not_found()
    return updated


@router.delete("/{profile_id}", summary="Delete a delay profile")
def delete_delay_profile(profile_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    outcome = db.delete_delay_profile(profile_id)
    if outcome == "not_found":
        raise _not_found()
    if outcome == "default":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The default delay profile cannot be deleted")
    return {"status": "deleted", "id": profile_id}
