"""REST API for quality definitions, custom formats (Lidarr-schema import/export) and release profiles."""

import logging
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier
from plex_playlist_sync.decision_engine import (
    export_format,
    check_raw_format,
    normalize_format,
    validate_format,
    validate_term,
)
from plex_playlist_sync.quality_defaults import QUALITY_ORDER
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.safe_regex import UnsafeRegexError
from plex_playlist_sync.storage import Database
from plex_playlist_sync.tag_store import normalize_labels

logger = logging.getLogger(__name__)

_deps = [Depends(require_core_tier), Depends(require_admin)]
definitions_router = APIRouter(dependencies=_deps)
formats_router = APIRouter(dependencies=_deps)
release_profiles_router = APIRouter(dependencies=_deps)


def _bad(detail: Any) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


# --------------------------------------------------------------------------------------------------------------------
# Quality definitions
# --------------------------------------------------------------------------------------------------------------------


class QualityDefinitionPayload(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=60)
    min_kbps: Optional[float] = Field(None, ge=0, le=100000)
    preferred_kbps: Optional[float] = Field(None, ge=0, le=100000)
    max_kbps: Optional[float] = Field(None, ge=0, le=100000)


class QualityDefinitionResponse(BaseModel):
    quality: str
    title: str
    min_kbps: Optional[float] = None
    preferred_kbps: Optional[float] = None
    max_kbps: Optional[float] = None
    default_min_kbps: Optional[float] = None
    default_preferred_kbps: Optional[float] = None
    default_max_kbps: Optional[float] = None
    is_default: bool = True
    updated_at: Optional[str] = None


@definitions_router.get("", response_model=list[QualityDefinitionResponse], summary="List quality definitions")
@definitions_router.get("/", response_model=list[QualityDefinitionResponse], include_in_schema=False)
def list_definitions(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_quality_definitions()


@definitions_router.post("/reset", response_model=list[QualityDefinitionResponse], summary="Reset all definitions")
def reset_all_definitions(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return db.reset_quality_definitions()


@definitions_router.put("/{quality}", response_model=QualityDefinitionResponse, summary="Update a definition")
def update_definition(
    quality: str, payload: QualityDefinitionPayload, db: Database = Depends(get_db)
) -> dict[str, Any]:
    if quality not in QUALITY_ORDER:
        raise _not_found(f"Quality '{quality}'")
    bounds = [b for b in (payload.min_kbps, payload.preferred_kbps, payload.max_kbps) if b is not None]
    # max 0 / null means unbounded, so it only constrains the order when it is a positive number.
    if payload.min_kbps is not None and payload.preferred_kbps is not None and payload.min_kbps > payload.preferred_kbps:
        raise _bad("min_kbps must not exceed preferred_kbps")
    if payload.max_kbps:
        if payload.min_kbps is not None and payload.min_kbps > payload.max_kbps:
            raise _bad("min_kbps must not exceed max_kbps")
        if payload.preferred_kbps is not None and payload.preferred_kbps > payload.max_kbps:
            raise _bad("preferred_kbps must not exceed max_kbps")
    del bounds
    result = db.update_quality_definition(
        quality, payload.min_kbps, payload.preferred_kbps, payload.max_kbps, payload.title
    )
    if result is None:
        raise _not_found(f"Quality '{quality}'")
    return result


@definitions_router.post("/{quality}/reset", response_model=QualityDefinitionResponse, summary="Reset one definition")
def reset_definition(quality: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    if quality not in QUALITY_ORDER:
        raise _not_found(f"Quality '{quality}'")
    db.reset_quality_definitions(quality)
    result = db.get_quality_definition(quality)
    if result is None:
        raise _not_found(f"Quality '{quality}'")
    return result


# --------------------------------------------------------------------------------------------------------------------
# Custom formats
# --------------------------------------------------------------------------------------------------------------------


class SpecificationModel(BaseModel):
    name: str = Field("", max_length=200)
    implementation: str = Field(..., min_length=1, max_length=80)
    negate: bool = False
    required: bool = False
    fields: Any = Field(default_factory=dict)


class CustomFormatPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    include_in_rename: bool = False
    specifications: list[SpecificationModel] = Field(default_factory=list, max_length=50)


class CustomFormatResponse(BaseModel):
    id: int
    name: str
    include_in_rename: bool = False
    specifications: list[dict[str, Any]] = Field(default_factory=list)
    unsupported: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _checked_format(raw: dict[str, Any]) -> dict[str, Any]:
    shape = check_raw_format(raw)
    if shape:
        raise _bad(shape)
    fmt = normalize_format(raw)
    problems = validate_format(fmt)
    if problems:
        raise _bad(problems)
    return fmt


@formats_router.get("", response_model=list[CustomFormatResponse], summary="List custom formats")
@formats_router.get("/", response_model=list[CustomFormatResponse], include_in_schema=False)
def list_formats(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_custom_formats()


@formats_router.post("", response_model=CustomFormatResponse, summary="Create a custom format")
@formats_router.post("/", response_model=CustomFormatResponse, include_in_schema=False)
def create_format(payload: CustomFormatPayload, db: Database = Depends(get_db)) -> dict[str, Any]:
    fmt = _checked_format(payload.model_dump())
    try:
        return db.create_custom_format(fmt)
    except sqlite3.IntegrityError:
        raise _bad(f"A custom format named '{fmt['name']}' already exists")


@formats_router.post("/import", summary="Import Lidarr-schema custom format JSON (one object or a list)")
def import_formats(payload: Any = Body(...), db: Database = Depends(get_db)) -> dict[str, Any]:
    """Accepts one Lidarr/Servarr custom-format object or a list of them.

    Unknown implementations are kept, flagged ``unsupported`` and ignored by the engine. A format whose name already
    exists is replaced. Invalid entries are reported in ``errors`` and do not abort the rest; if nothing could be
    imported the response is a 400.
    """
    items = payload if isinstance(payload, list) else [payload]
    if not items or not all(isinstance(i, dict) for i in items):
        raise _bad("Body must be a custom format object or a non-empty list of them")
    if len(items) > 200:
        raise _bad("At most 200 custom formats per import")
    imported: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, raw in enumerate(items):
        shape = check_raw_format(raw)
        if shape:
            raw_name = raw.get("name")
            errors.append({"index": idx, "name": raw_name[:200] if isinstance(raw_name, str) else None, "errors": shape})
            continue
        fmt = normalize_format(raw)
        problems = validate_format(fmt)
        if problems:
            errors.append({"index": idx, "name": fmt.get("name") or None, "errors": problems})
            continue
        existing = db.get_custom_format_by_name(fmt["name"])
        saved = db.update_custom_format(existing["id"], fmt) if existing else db.create_custom_format(fmt)
        if saved is None:  # the format vanished between lookup and update (concurrent delete)
            errors.append({"index": idx, "name": fmt["name"], "errors": ["could not be saved; try again"]})
            continue
        saved["action"] = "updated" if existing else "created"
        imported.append(saved)
    if not imported:
        raise _bad({"message": "No custom formats could be imported", "errors": errors})
    return {"imported": imported, "errors": errors}


@formats_router.get("/{format_id}/export", summary="Export a custom format as Lidarr-schema JSON")
def export_custom_format(format_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    fmt = db.get_custom_format(format_id)
    if not fmt:
        raise _not_found("Custom format")
    return export_format(fmt)


@formats_router.get("/{format_id}", response_model=CustomFormatResponse, summary="Get a custom format")
def get_format(format_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    fmt = db.get_custom_format(format_id)
    if not fmt:
        raise _not_found("Custom format")
    return fmt


@formats_router.put("/{format_id}", response_model=CustomFormatResponse, summary="Update a custom format")
def update_format(format_id: int, payload: CustomFormatPayload, db: Database = Depends(get_db)) -> dict[str, Any]:
    fmt = _checked_format(payload.model_dump())
    try:
        result = db.update_custom_format(format_id, fmt)
    except sqlite3.IntegrityError:
        raise _bad(f"A custom format named '{fmt['name']}' already exists")
    if not result:
        raise _not_found("Custom format")
    return result


@formats_router.delete("/{format_id}", summary="Delete a custom format")
def delete_format(format_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    if not db.delete_custom_format(format_id):
        raise _not_found("Custom format")
    return {"status": "deleted", "id": format_id}


# --------------------------------------------------------------------------------------------------------------------
# Release profiles
# --------------------------------------------------------------------------------------------------------------------


class ReleaseProfilePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    enabled: bool = True
    required: list[str] = Field(default_factory=list, max_length=200)
    ignored: list[str] = Field(default_factory=list, max_length=200)
    indexer_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=50)
    quality_profile_ids: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, tags: list[str]) -> list[str]:
        return normalize_labels(tags)


class ReleaseProfileResponse(BaseModel):
    id: int
    name: str
    enabled: bool
    required: list[str]
    ignored: list[str]
    indexer_ids: list[Any]
    tags: list[Any]
    quality_profile_ids: list[str]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _release_profile_data(payload: ReleaseProfilePayload) -> dict[str, Any]:
    data = payload.model_dump()
    data["name"] = payload.name.strip()
    for key in ("required", "ignored"):
        terms = [t.strip() for t in data[key] if t.strip()]
        for term in terms:
            try:
                validate_term(term)
            except UnsafeRegexError as exc:
                raise _bad(f"{key} term '{term[:60]}': {exc}")
        data[key] = terms
    return data


@release_profiles_router.get("", response_model=list[ReleaseProfileResponse], summary="List release profiles")
@release_profiles_router.get("/", response_model=list[ReleaseProfileResponse], include_in_schema=False)
def list_release_profiles(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_release_profiles()


@release_profiles_router.post("", response_model=ReleaseProfileResponse, summary="Create a release profile")
@release_profiles_router.post("/", response_model=ReleaseProfileResponse, include_in_schema=False)
def create_release_profile(payload: ReleaseProfilePayload, db: Database = Depends(get_db)) -> dict[str, Any]:
    data = _release_profile_data(payload)
    try:
        return db.create_release_profile(data)
    except sqlite3.IntegrityError as e:
        raise _bad(f"Release profile name already exists: {redact_text(str(e))}")


@release_profiles_router.get("/{profile_id}", response_model=ReleaseProfileResponse, summary="Get a release profile")
def get_release_profile(profile_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    rp = db.get_release_profile(profile_id)
    if not rp:
        raise _not_found("Release profile")
    return rp


@release_profiles_router.put("/{profile_id}", response_model=ReleaseProfileResponse, summary="Update a release profile")
def update_release_profile(
    profile_id: int, payload: ReleaseProfilePayload, db: Database = Depends(get_db)
) -> dict[str, Any]:
    data = _release_profile_data(payload)
    try:
        rp = db.update_release_profile(profile_id, data)
    except sqlite3.IntegrityError as e:
        raise _bad(f"Release profile name already exists: {redact_text(str(e))}")
    if not rp:
        raise _not_found("Release profile")
    return rp


@release_profiles_router.delete("/{profile_id}", summary="Delete a release profile")
def delete_release_profile(profile_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    if not db.delete_release_profile(profile_id):
        raise _not_found("Release profile")
    return {"status": "deleted", "id": profile_id}
