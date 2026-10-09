"""Endpoints for music collections management."""

import logging
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status


from plex_playlist_sync.api.dependencies import (
    get_db,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from plex_playlist_sync.api.schemas.library import (
    CollectionAlbumResponse,
    CollectionDeleteResponse,
    LibraryCollectionRecord,
)
from plex_playlist_sync.models import (
    LibraryCollection,
)
from plex_playlist_sync.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from .models import (CreateCollectionRequest, AddAlbumToCollectionRequest)

@router.get("/collections", response_model=list[LibraryCollectionRecord], response_model_exclude_unset=True)
def list_collections(
    query: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Returns list of library collections with album counts."""
    return db.list_library_collections(limit=limit, offset=offset, query=query)

@router.post("/collections", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=LibraryCollectionRecord, response_model_exclude_unset=True)
def create_collection(
    body: CreateCollectionRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Creates a new library collection."""
    col = LibraryCollection(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        summary=body.summary.strip() if body.summary else None,
        poster_url=body.poster_url.strip() if body.poster_url else None,
        monitored=body.monitored,
    )
    return db.upsert_library_collection(col)

@router.get("/collections/{collection_id}", response_model=LibraryCollectionRecord, response_model_exclude_unset=True)
def get_collection(
    collection_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns collection detail along with its ordered albums list."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    result = dict(col)
    result["albums"] = db.get_collection_albums(collection_id)
    return result

@router.delete("/collections/{collection_id}", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=CollectionDeleteResponse, response_model_exclude_unset=True)
def delete_collection(
    collection_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Deletes a library collection."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    deleted = db.delete_library_collection(collection_id)
    return {"success": deleted, "id": collection_id}

@router.post("/collections/{collection_id}/albums", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=CollectionAlbumResponse, response_model_exclude_unset=True)
def add_album_to_collection(
    collection_id: str,
    body: AddAlbumToCollectionRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Adds an album to a collection with optional order_index."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    album = db.get_library_album(body.album_id)
    if album is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Album not found",
        )
    success = db.add_album_to_collection(
        collection_id=collection_id,
        album_id=body.album_id,
        order_index=body.order_index,
    )
    return {"success": success, "collection_id": collection_id, "album_id": body.album_id}

@router.delete("/collections/{collection_id}/albums/{album_id}", dependencies=[Depends(require_core_tier), Depends(track_admin_actor)], response_model=CollectionAlbumResponse, response_model_exclude_unset=True)
def remove_album_from_collection(
    collection_id: str,
    album_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Removes an album from a collection."""
    col = db.get_library_collection(collection_id)
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    removed = db.remove_album_from_collection(collection_id, album_id)
    return {"success": removed, "collection_id": collection_id, "album_id": album_id}

