"""Tags (Lidarr parity): the tag registry at ``/api/tags``.

Admin-only and Core-tier only. Tags are a native-database feature and work the same in Lidarr mode (Lidarr's own tags
are never proxied). Artists carry tags by id (``PUT /api/library/artists/{id}/tags``); delay profiles, release
profiles and import lists reference them by label (see ``tag_store``).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from plex_playlist_sync.api.dependencies import get_db, require_admin, require_core_tier, track_admin_actor
from plex_playlist_sync.storage import Database
from plex_playlist_sync.tag_store import DuplicateTag, normalize_label

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier), Depends(require_admin), Depends(track_admin_actor)])


class TagPayload(BaseModel):
    label: str = Field(..., description="1-40 characters of a-z, 0-9, space, hyphen or underscore (lower-cased)")

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return normalize_label(value)


class TagOut(BaseModel):
    id: int
    label: str
    created_at: str
    artist_count: int = 0
    delay_profile_count: int = 0
    release_profile_count: int = 0
    import_list_count: int = 0


class TagRef(BaseModel):
    id: int | str
    name: str


class TagUsage(BaseModel):
    tag: TagOut
    artist_count: int
    artists: list[TagRef] = Field(description="First 200 artists carrying the tag, by name")
    delay_profiles: list[TagRef]
    release_profiles: list[TagRef]
    import_lists: list[TagRef]


class TagDeleted(BaseModel):
    status: str = "deleted"
    id: int


def _out(db: Database, tag_id: int) -> TagOut:
    for row in db.list_tags():
        if row["id"] == tag_id:
            return TagOut(**row)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")


@router.get("", response_model=list[TagOut], summary="List tags with usage counts")
def list_tags(db: Database = Depends(get_db)) -> list[TagOut]:
    return [TagOut(**row) for row in db.list_tags()]


@router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED, summary="Create a tag")
def create_tag(body: TagPayload, db: Database = Depends(get_db)) -> TagOut:
    try:
        created = db.create_tag(body.label)
    except DuplicateTag as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _out(db, created["id"])


@router.put("/{tag_id}", response_model=TagOut, summary="Rename a tag")
def rename_tag(tag_id: int, body: TagPayload, db: Database = Depends(get_db)) -> TagOut:
    """Renames the tag; delay/release profiles and import lists using the old label follow."""
    try:
        updated = db.rename_tag(tag_id, body.label)
    except DuplicateTag as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return _out(db, tag_id)


@router.delete("/{tag_id}", response_model=TagDeleted, summary="Delete a tag")
def delete_tag(tag_id: int, db: Database = Depends(get_db)) -> TagDeleted:
    """Removes the tag from every artist, delay profile, release profile and import list."""
    if not db.delete_tag(tag_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return TagDeleted(id=tag_id)


@router.get("/{tag_id}/usage", response_model=TagUsage, summary="Where a tag is used")
def tag_usage(tag_id: int, db: Database = Depends(get_db)) -> TagUsage:
    usage = db.get_tag_usage(tag_id)
    if usage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return TagUsage(**{**usage, "tag": _out(db, tag_id)})
