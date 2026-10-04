"""REST API endpoints for Quality Profiles and Release Evaluation."""

import logging
import sqlite3
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from plex_playlist_sync.api.dependencies import get_db, require_admin
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.models import (
    QualityProfile,
    QualityProfileItem,
)
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class QualityProfileItemModel(BaseModel):
    quality: str
    allowed: bool = True
    weight: int = 100


class QualityProfilePayload(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    cutoff: str = Field(..., min_length=1)
    items: list[QualityProfileItemModel] = Field(default_factory=list)
    preferred_tags: list[str] = Field(default_factory=list)
    ignored_tags: list[str] = Field(default_factory=list)
    min_size_mb: Optional[float] = None
    max_size_mb: Optional[float] = None
    is_default: bool = False
    custom_formats: list[dict[str, Any]] = Field(default_factory=list)
    min_score: Optional[int] = None
    upgrade_allowed: bool = True


class QualityProfileResponse(BaseModel):
    id: str
    name: str
    cutoff: str
    items: list[QualityProfileItemModel]
    preferred_tags: list[str] = Field(default_factory=list)
    ignored_tags: list[str] = Field(default_factory=list)
    min_size_mb: Optional[float] = None
    max_size_mb: Optional[float] = None
    is_default: bool = False
    custom_formats: list[dict[str, Any]] = Field(default_factory=list)
    min_score: Optional[int] = None
    upgrade_allowed: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class EvaluateTitlePayload(BaseModel):
    title: str = Field(..., min_length=1)
    profile_id: Optional[str] = None
    size_bytes: Optional[int] = None


class EvaluateTitleResponse(BaseModel):
    parsed: dict[str, Any]
    evaluation: dict[str, Any]


@router.get("", response_model=list[QualityProfileResponse], summary="List quality profiles")
@router.get("/", response_model=list[QualityProfileResponse], include_in_schema=False)
def list_quality_profiles(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Lists all configured quality profiles."""
    return db.list_quality_profiles()


@router.get("/{profile_id}", response_model=QualityProfileResponse, summary="Get quality profile by ID")
def get_quality_profile(
    profile_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Retrieves a single quality profile."""
    profile = db.get_quality_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Quality profile '{profile_id}' not found",
        )
    return profile


@router.post("", response_model=QualityProfileResponse, summary="Create or update quality profile")
@router.post("/", response_model=QualityProfileResponse, include_in_schema=False)
def create_or_update_quality_profile(
    payload: QualityProfilePayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Creates or updates a quality profile."""
    p_id = payload.id.strip() if payload.id and payload.id.strip() else str(uuid.uuid4())
    profile_obj = QualityProfile(
        id=p_id,
        name=payload.name.strip(),
        cutoff=payload.cutoff.strip(),
        items=[
            QualityProfileItem(
                quality=item.quality.strip(),
                allowed=item.allowed,
                weight=item.weight,
            )
            for item in payload.items
        ],
        preferred_tags=[t.strip() for t in payload.preferred_tags if t.strip()],
        ignored_tags=[t.strip() for t in payload.ignored_tags if t.strip()],
        min_size_mb=payload.min_size_mb,
        max_size_mb=payload.max_size_mb,
        is_default=payload.is_default,
        custom_formats=payload.custom_formats,
        min_score=payload.min_score,
        upgrade_allowed=payload.upgrade_allowed,
    )
    try:
        saved = db.upsert_quality_profile(profile_obj)
        return saved
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Profile name already exists or violates constraint: {redact_text(str(e))}",
        )


@router.delete("/{profile_id}", summary="Delete quality profile")
def delete_quality_profile(
    profile_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes a quality profile. Prevents deleting default profile."""
    existing = db.get_quality_profile(profile_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Quality profile '{profile_id}' not found",
        )
    if existing.get("is_default"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the default quality profile",
        )
    try:
        deleted = db.delete_quality_profile(profile_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Quality profile '{profile_id}' not found",
        )
    return {"status": "deleted", "id": profile_id}


@router.post("/evaluate", response_model=EvaluateTitleResponse, summary="Evaluate release title against profile")
def evaluate_release_title(
    payload: EvaluateTitlePayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Evaluates a raw release title string against a quality profile."""
    if payload.profile_id:
        profile_data = db.get_quality_profile(payload.profile_id)
        if not profile_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Quality profile '{payload.profile_id}' not found",
            )
    else:
        try:
            profile_data = db.get_default_quality_profile()
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )

    profile_obj = QualityProfile(
        id=profile_data["id"],
        name=profile_data["name"],
        cutoff=profile_data["cutoff"],
        items=[
            QualityProfileItem(
                quality=i["quality"],
                allowed=i.get("allowed", True),
                weight=i.get("weight", 100),
            )
            for i in profile_data.get("items", [])
        ],
        preferred_tags=profile_data.get("preferred_tags", []),
        ignored_tags=profile_data.get("ignored_tags", []),
        min_size_mb=profile_data.get("min_size_mb"),
        max_size_mb=profile_data.get("max_size_mb"),
        is_default=profile_data.get("is_default", False),
        custom_formats=profile_data.get("custom_formats", []),
        min_score=profile_data.get("min_score"),
    )

    parsed = parse_release_title(payload.title)
    evaluation = evaluate_release(parsed, profile_obj, size_bytes=payload.size_bytes)

    return {
        "parsed": parsed.to_dict(),
        "evaluation": evaluation.to_dict(),
    }
