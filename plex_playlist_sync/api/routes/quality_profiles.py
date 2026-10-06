"""REST API endpoints for Quality Profiles and Release Evaluation."""

import logging
import sqlite3
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from plex_playlist_sync.acquisition_coordinator import _to_quality_profile, resolve_duration
from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier
from plex_playlist_sync.decision_engine import evaluate_prepared, evaluate_upgrade, prepare_profile
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.safe_regex import UnsafeRegexError, validate_pattern
from plex_playlist_sync.quality import parse_release_title
from plex_playlist_sync.quality_defaults import QUALITY_ORDER, entry_label, entry_qualities, normalize_entries
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier), Depends(require_admin)])


class FormatItemModel(BaseModel):
    format_id: int
    score: int = Field(0, ge=-100000, le=100000)


class QualityProfileItemModel(BaseModel):
    """One ordered profile entry (top = best): a quality, or a group of equivalent qualities.

    Legacy payloads (``{quality, allowed, weight}`` with no ``type``) are still accepted and ordered by weight.
    """

    type: Optional[str] = None  # "quality" | "group"; omitted = legacy weight item
    quality: Optional[str] = None
    name: Optional[str] = None
    allowed: bool = True
    items: list[str] = Field(default_factory=list)
    weight: Optional[int] = None


class QualityProfilePayload(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    cutoff: str = Field(..., min_length=1)
    items: list[QualityProfileItemModel] = Field(default_factory=list)
    upgrade_allowed: bool = True
    min_format_score: int = Field(-100, ge=-100000, le=100000)
    cutoff_format_score: int = Field(0, ge=-100000, le=100000)
    min_upgrade_format_score: int = Field(1, ge=1, le=100000)
    format_items: Optional[list[FormatItemModel]] = None
    is_default: bool = False
    # Deprecated, kept for old clients: tags are folded into custom formats / a release profile on save.
    preferred_tags: list[str] = Field(default_factory=list)
    ignored_tags: list[str] = Field(default_factory=list)
    min_size_mb: Optional[float] = None
    max_size_mb: Optional[float] = None
    custom_formats: list[dict[str, Any]] = Field(default_factory=list)
    min_score: Optional[int] = None


class QualityProfileResponse(BaseModel):
    id: str
    name: str
    cutoff: str
    items: list[QualityProfileItemModel]
    upgrade_allowed: bool = True
    min_format_score: int = -100
    cutoff_format_score: int = 0
    min_upgrade_format_score: int = 1
    format_items: list[FormatItemModel] = Field(default_factory=list)
    is_default: bool = False
    preferred_tags: list[str] = Field(default_factory=list)
    ignored_tags: list[str] = Field(default_factory=list)
    min_size_mb: Optional[float] = None
    max_size_mb: Optional[float] = None
    custom_formats: list[dict[str, Any]] = Field(default_factory=list)
    min_score: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CopyPayload(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)


class EvaluateTitlePayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=1000)
    profile_id: Optional[str] = None
    size_bytes: Optional[int] = Field(None, ge=0)
    protocol: Optional[str] = None
    indexer_id: Optional[str] = None
    indexer_name: Optional[str] = None
    indexer_flags: int = 0
    album_id: Optional[str] = None
    track_id: Optional[str] = None
    artist_id: Optional[str] = Field(None, description="Library artist whose tags scope tag-restricted release profiles")
    current_title: Optional[str] = Field(None, max_length=1000)


class EvaluateTitleResponse(BaseModel):
    parsed: dict[str, Any]
    evaluation: dict[str, Any]
    breakdown: Optional[dict[str, Any]] = None
    upgrade: Optional[dict[str, Any]] = None


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _validate_profile(payload: QualityProfilePayload, db: Database) -> list[dict[str, Any]]:
    """Normalizes ``items`` to v2 entries and raises clear 400s for an inconsistent profile."""
    for idx, cf in enumerate(payload.custom_formats):
        pattern = cf.get("pattern") if isinstance(cf, dict) else None
        if pattern:
            try:
                validate_pattern(str(pattern))
            except UnsafeRegexError as exc:
                raise _bad_request(f"custom_formats[{idx}] pattern: {exc}")
    raw = [i.model_dump(exclude_none=True) for i in payload.items]
    legacy = bool(raw) and all("type" not in i and i.get("weight") is not None for i in raw)
    if not legacy:  # legacy weight payloads stay type-less so normalize_entries orders them by weight
        for item in raw:
            item.setdefault("type", "quality")
            if item["type"] not in ("quality", "group"):
                raise _bad_request(f"Unknown item type '{item['type']}'")
    entries = normalize_entries(raw)
    if not entries:
        return entries  # an empty profile accepts nothing; it is allowed so a profile can be created before its items
    known = set(QUALITY_ORDER)
    seen: set[str] = set()
    names: set[str] = set()
    for entry in entries:
        label = entry_label(entry).strip()
        if not label:
            raise _bad_request("Every group needs a name")
        if label in names:
            raise _bad_request(f"Duplicate entry name '{label}'")
        names.add(label)
        if entry["type"] == "group" and not entry["items"]:
            raise _bad_request(f"Group '{label}' has no qualities")
        for quality in entry_qualities(entry):
            if quality not in known:
                raise _bad_request(f"Unknown quality '{quality}'")
            if quality in seen:
                raise _bad_request(f"Quality '{quality}' appears more than once")
            seen.add(quality)
    if payload.cutoff.strip() not in names and payload.cutoff.strip() not in seen:
        raise _bad_request(f"Cutoff '{payload.cutoff}' is not a quality or group in this profile")
    if payload.format_items:
        existing_ids = {f["id"] for f in db.list_custom_formats()}
        seen_ids: set[int] = set()
        for fi in payload.format_items:
            if fi.format_id not in existing_ids:
                raise _bad_request(f"Custom format {fi.format_id} does not exist")
            if fi.format_id in seen_ids:
                raise _bad_request(f"Custom format {fi.format_id} is listed twice")
            seen_ids.add(fi.format_id)
    return entries


def _profile_dict(payload: QualityProfilePayload, entries: list[dict[str, Any]], p_id: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": p_id,
        "name": payload.name.strip(),
        "cutoff": payload.cutoff.strip(),
        "items": entries,
        "upgrade_allowed": payload.upgrade_allowed,
        "min_format_score": payload.min_format_score,
        "cutoff_format_score": payload.cutoff_format_score,
        "min_upgrade_format_score": payload.min_upgrade_format_score,
        "is_default": payload.is_default,
    }
    # Legacy fields: the v2 UI payload omits them. Only fields the client actually sent are written; omitted ones keep
    # their stored values (db.upsert_quality_profile preserves any key missing from the dict).
    sent = payload.model_fields_set
    if "preferred_tags" in sent:
        data["preferred_tags"] = [t.strip() for t in payload.preferred_tags if t.strip()]
    if "ignored_tags" in sent:
        data["ignored_tags"] = [t.strip() for t in payload.ignored_tags if t.strip()]
    for key in ("min_size_mb", "max_size_mb", "custom_formats", "min_score"):
        if key in sent:
            data[key] = getattr(payload, key)
    if payload.format_items is not None:
        data["format_items"] = [fi.model_dump() for fi in payload.format_items]
    return data


@router.get("", response_model=list[QualityProfileResponse], summary="List quality profiles")
@router.get("/", response_model=list[QualityProfileResponse], include_in_schema=False)
def list_quality_profiles(
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Lists all configured quality profiles."""
    return db.list_quality_profiles()


@router.get("/{profile_id}", response_model=QualityProfileResponse, summary="Get quality profile by ID")
def get_quality_profile(
    profile_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Retrieves a single quality profile."""
    profile = db.get_quality_profile(profile_id, include_catalog=False)
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
) -> dict[str, Any]:
    """Creates or updates a quality profile (full v2 shape)."""
    p_id = payload.id.strip() if payload.id and payload.id.strip() else str(uuid.uuid4())
    entries = _validate_profile(payload, db)
    previous = db.get_quality_profile(p_id, include_catalog=False)
    try:
        saved = db.upsert_quality_profile(_profile_dict(payload, entries, p_id))
    except sqlite3.IntegrityError as e:
        raise _bad_request(f"Profile name already exists or violates constraint: {redact_text(str(e))}")
    # Fold only tags that are new relative to what was stored, so a re-save never resets tuned scores or resurrects
    # release profiles the user deleted or edited.
    old_pref = set((previous or {}).get("preferred_tags") or [])
    old_ign = set((previous or {}).get("ignored_tags") or [])
    added_pref = [t for t in saved.get("preferred_tags") or [] if t not in old_pref]
    added_ign = [t for t in saved.get("ignored_tags") or [] if t not in old_ign]
    if added_pref or added_ign:
        saved = db.apply_legacy_tags(p_id, added_pref, added_ign) or saved
    return saved


@router.post("/{profile_id}/copy", response_model=QualityProfileResponse, summary="Copy a quality profile")
def copy_quality_profile(
    profile_id: str,
    payload: CopyPayload = CopyPayload(),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    source = db.get_quality_profile(profile_id, include_catalog=False)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Quality profile '{profile_id}' not found")
    taken = {p["name"] for p in db.list_quality_profiles()}
    name = payload.name.strip() if payload.name else f"{source['name']} (Copy)"
    if payload.name and name in taken:
        raise _bad_request(f"Profile name '{name}' already exists")
    base, n = name, 2
    while name in taken:
        name = f"{base} {n}"
        n += 1
    clone = dict(source)
    clone.update(id=str(uuid.uuid4()), name=name, is_default=False)
    return db.upsert_quality_profile(clone)


@router.post("/{profile_id}/default", response_model=QualityProfileResponse, summary="Set the default profile")
def set_default_quality_profile(
    profile_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    result = db.set_default_quality_profile(profile_id)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Quality profile '{profile_id}' not found")
    return result


@router.delete("/{profile_id}", summary="Delete quality profile")
def delete_quality_profile(
    profile_id: str,
    db: Database = Depends(get_db),
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
) -> dict[str, Any]:
    """Previews a release title against a profile and returns the full decision breakdown.

    ``protocol``, ``indexer_*`` and ``size_bytes`` feed the custom formats; ``album_id`` (or ``track_id``) supplies the
    duration for the kbps limits; ``current_title`` additionally returns the upgrade decision against that file.
    """
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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    profile_obj = _to_quality_profile(profile_data)
    prepared = prepare_profile(profile_obj)
    duration = resolve_duration(db, payload.album_id, payload.track_id, "track" if payload.track_id else "album")
    parsed = parse_release_title(payload.title)
    ctx = {
        "protocol": payload.protocol,
        "indexer_id": payload.indexer_id,
        "indexer_name": payload.indexer_name,
        "indexer_flags": payload.indexer_flags,
        "duration": duration,
        "artist_tags": db.get_artist_tag_labels(artist_id=payload.artist_id) if payload.artist_id else [],
    }
    evaluation = evaluate_prepared(parsed, prepared, payload.size_bytes or None, **ctx)
    upgrade = None
    if payload.current_title:
        current = evaluate_prepared(parse_release_title(payload.current_title), prepared, artist_tags=ctx["artist_tags"])
        upgrade = evaluate_upgrade(current, evaluation, profile_obj).to_dict()
    return {
        "parsed": parsed.to_dict(),
        "evaluation": evaluation.to_dict(),
        "breakdown": evaluation.breakdown.to_dict() if evaluation.breakdown else None,
        "upgrade": upgrade,
    }
