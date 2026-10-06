"""Wanted API: monitored items missing from the library, and files below their quality cutoff.

Admin-only and core-only. ``native`` reads the TrackSeerr library; ``lidarr`` proxies Lidarr's wanted lists (at
album level). The search action runs under the active mode's ``work_guard``.
"""

import logging
from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from plex_playlist_sync import activity_service as svc
from plex_playlist_sync.api.dependencies import get_db, get_lidarr_client, require_admin, require_core_tier
from plex_playlist_sync.api.routes.activity import (
    MAX_PAGE,
    SORT_DIR_PATTERN,
    lidarr_call,
    require_lidarr,
    run_mutation,
    validate_sort_key,
)
from plex_playlist_sync.backlog_worker import ReplacementSpec, backlog_worker
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.library_manager import MODE_LIDARR, get_library_mode
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier)])

# Largest "search everything" batch the native side queues in one call (the rest is picked up by the sweep).
MAX_NATIVE_SEARCH_ALL = 1000


class WantedSearchRequest(BaseModel):
    """Either explicit ``ids`` or ``all`` + ``list``."""

    model_config = ConfigDict(populate_by_name=True)

    ids: Optional[list[Union[str, int]]] = Field(default=None, max_length=1000)
    all_items: bool = Field(default=False, alias="all")
    list_name: Optional[Literal["missing", "cutoff"]] = Field(default=None, alias="list")


def _wanted_list(
    kind: str,
    page: int,
    page_size: int,
    sort_key: Optional[str],
    sort_dir: str,
    db: Database,
    client: Optional[LidarrClient],
) -> dict[str, Any]:
    key = validate_sort_key(sort_key, svc.WANTED_SORT_KEYS, "artist")
    if get_library_mode(db) == MODE_LIDARR:
        if key not in svc.LIDARR_WANTED_SORT:
            raise HTTPException(
                status_code=422,
                detail=f"Lidarr cannot sort Wanted by '{key}'; allowed: {', '.join(svc.LIDARR_WANTED_SORT)}",
            )
        return lidarr_call(svc.lidarr_wanted, require_lidarr(client), kind, page, page_size, key, sort_dir)
    return svc.native_wanted(db, kind, page, page_size, key, sort_dir)


@router.get("/missing", summary="Monitored items that are not in the library")
def list_missing(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=SORT_DIR_PATTERN),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _wanted_list("missing", page, page_size, sort_key, sort_dir, db, client)


@router.get("/cutoff", summary="Files below their quality profile cutoff")
def list_cutoff(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=SORT_DIR_PATTERN),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _wanted_list("cutoff", page, page_size, sort_key, sort_dir, db, client)


def _wanted_index(
    kind: str, sort_key: Optional[str], sort_dir: str, db: Database
) -> dict[str, Any]:
    key = validate_sort_key(sort_key, svc.WANTED_SORT_KEYS, "artist")
    if get_library_mode(db) == MODE_LIDARR:
        return svc.empty_index(key, sort_dir)  # Lidarr pages server-side; there is nothing to index locally
    return svc.native_wanted_index(db, kind, key, sort_dir)


@router.get("/missing/index", summary="Scrubber groups for the missing list (native only)")
def missing_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=SORT_DIR_PATTERN),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _wanted_index("missing", sort_key, sort_dir, db)


@router.get("/cutoff/index", summary="Scrubber groups for the cutoff-unmet list (native only)")
def cutoff_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern=SORT_DIR_PATTERN),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _wanted_index("cutoff", sort_key, sort_dir, db)


@router.post("/search", summary="Search for wanted items")
def search_wanted(
    body: WantedSearchRequest,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    ids = [str(i) for i in (body.ids or [])]
    if bool(ids) == bool(body.all_items):
        raise HTTPException(
            status_code=422,
            detail="Provide either a non-empty 'ids' list or 'all': true with a 'list'",
        )
    if body.all_items and body.list_name is None:
        raise HTTPException(status_code=422, detail="'all' requires 'list': missing or cutoff")

    def native() -> dict[str, Any]:
        if ids:
            targets = db.list_wanted_search_targets(track_ids=ids)
        else:
            targets = db.list_wanted_search_targets(kind=body.list_name, limit=MAX_NATIVE_SEARCH_ALL)
        return backlog_worker.queue_wanted_search(db, targets, actor_user_id=_actor_id(_admin))

    def lidarr() -> dict[str, Any]:
        lidarr_client = require_lidarr(client)
        if ids:
            try:
                album_ids = [int(i) for i in ids]
            except ValueError:
                raise HTTPException(status_code=422, detail="Lidarr ids must be numeric")
            lidarr_call(lidarr_client.run_command, "AlbumSearch", albumIds=album_ids)
            return {"queued": len(album_ids)}
        page = lidarr_call(lidarr_client.get_wanted, body.list_name, 1, 1, "albums.title", "asc")
        total = page.get("totalRecords") if isinstance(page, dict) else 0
        command = "MissingAlbumSearch" if body.list_name == "missing" else "CutoffUnmetAlbumSearch"
        lidarr_call(lidarr_client.run_command, command)
        return {"queued": int(total) if isinstance(total, int) else 0}

    return run_mutation(db, native, lidarr)


def _actor_id(user: dict[str, Any]) -> Optional[str]:
    uid = user.get("id")
    return str(uid) if uid and uid != "api_key_user" else None


def search_tracks_for_replacement(
    db: Database, track_ids: list[str], issue_id: str, require_better: bool, actor_user_id: Optional[str] = None
) -> dict[str, Any]:
    """Issue fix actions only: searches the tracks although they have files (see ``ReplacementSpec``)."""
    targets = db.list_wanted_search_targets(track_ids=track_ids)
    return backlog_worker.queue_wanted_search(
        db, targets, replacement=ReplacementSpec(issue_id=issue_id, require_better=require_better),
        actor_user_id=actor_user_id,
    )
