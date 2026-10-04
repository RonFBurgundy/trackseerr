"""Activity API: download queue, history and blocklist for whichever library manager is active.

Admin-only and core-only (deny-by-default on the gateway). Lists dispatch on ``library_mode``: ``native`` reads
TrackSeerr's tables, ``lidarr`` proxies Lidarr API v1 live. Every mutating action runs under the active mode's
``work_guard``; a mode flip mid-request is a 409.
"""

import logging
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from plex_playlist_sync import activity_service as svc
from plex_playlist_sync.api.dependencies import get_db, get_lidarr_client, require_admin, require_core_tier
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient, _exc_text
from plex_playlist_sync.library_manager import MODE_LIDARR, ModeChanged, get_library_mode, run_for_mode
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_core_tier)])

SORT_DIR_PATTERN = "^(asc|desc)$"
MAX_PAGE = 1_000_000  # bounds page * page_size so offset arithmetic stays small


def validate_sort_key(sort_key: Optional[str], allowed: tuple[str, ...], default: str) -> str:
    """The requested sort key, or ``default`` when omitted; 422 for a key outside the whitelist."""
    key = sort_key if sort_key is not None else default
    if key not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown sort_key '{redact_text(str(key))[:64]}'; allowed: {', '.join(allowed)}",
        )
    return key


def lidarr_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Runs a Lidarr call; any Lidarr failure becomes a 502 whose message is redacted."""
    try:
        return fn(*args, **kwargs)
    except LidarrApiError as exc:
        logger.warning("Lidarr request failed: %s", redact_text(_exc_text(exc)))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=redact_text(_exc_text(exc)))


def require_lidarr(client: Optional[LidarrClient]) -> LidarrClient:
    if client is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Lidarr is not configured")
    return client


_LIDARR_ID_RE = re.compile(r"[0-9]{1,10}")


def lidarr_numeric_id(raw: str, what: str) -> int:
    """A Lidarr id: 1-10 ASCII digits only (no sign, whitespace, underscores or unicode digits); else 404."""
    if not isinstance(raw, str) or _LIDARR_ID_RE.fullmatch(raw) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} '{redact_text(str(raw))[:64]}' not found")
    return int(raw)


def run_mutation(db: Database, native: Callable[[], Any], lidarr: Callable[[], Any]) -> Any:
    """Runs an action under the active mode's guard; ModeChanged (the manager flipped) is a 409."""
    try:
        return run_for_mode(db, native=native, lidarr=lidarr)
    except ModeChanged:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The library manager changed while this was running; reload and try again.",
        )


def _is_lidarr(db: Database) -> bool:
    return get_library_mode(db) == MODE_LIDARR


# ------------------------------------------------------------------------------------------------------- queue


@router.get("/queue", summary="Download queue (native or Lidarr)")
def list_queue(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern=SORT_DIR_PATTERN),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    key = validate_sort_key(sort_key, svc.QUEUE_SORT_KEYS, "added_at")
    if _is_lidarr(db):
        return lidarr_call(svc.lidarr_queue, require_lidarr(client), page, page_size, key, sort_dir)
    return svc.native_queue(db, page, page_size, key, sort_dir)


@router.delete("/queue/{queue_id}", summary="Remove a queue item, optionally blocklisting it")
def delete_queue_item(
    queue_id: str,
    remove_from_client: bool = Query(True),
    blocklist: bool = Query(False),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    def native() -> dict[str, Any]:
        message = svc.native_delete_queue_item(db, queue_id, remove_from_client, blocklist)
        if message is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Queue item not found")
        return {"success": True, "message": message}

    def lidarr() -> dict[str, Any]:
        numeric = lidarr_numeric_id(queue_id, "Queue item")
        lidarr_call(require_lidarr(client).delete_queue_item, numeric, remove_from_client, blocklist)
        return {"success": True, "message": "Removed from the Lidarr queue" + (" and blocklisted" if blocklist else "")}

    return run_mutation(db, native, lidarr)


@router.post("/queue/{queue_id}/retry", summary="Search again for a queue item")
def retry_queue_item(
    queue_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    def native() -> dict[str, Any]:
        result = svc.native_retry_queue_item(db, queue_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Queue item not found")
        return result

    def lidarr() -> dict[str, Any]:
        numeric = lidarr_numeric_id(queue_id, "Queue item")
        lidarr_client = require_lidarr(client)
        album_id = lidarr_call(svc.lidarr_album_id_for_queue_item, lidarr_client, numeric)
        if album_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Queue item not found")
        lidarr_call(lidarr_client.run_command, "AlbumSearch", albumIds=[album_id])
        return {"success": True, "message": "Search queued in Lidarr"}

    return run_mutation(db, native, lidarr)


# ------------------------------------------------------------------------------------------------------ history


@router.get("/history", summary="Download history (native or Lidarr)")
def list_history(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern=SORT_DIR_PATTERN),
    event: Optional[str] = Query(None),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    validate_sort_key(sort_key, svc.HISTORY_SORT_KEYS, "date")
    if event is not None and event not in svc.HISTORY_EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event; allowed: {', '.join(svc.HISTORY_EVENTS)}",
        )
    if _is_lidarr(db):
        return lidarr_call(svc.lidarr_history, require_lidarr(client), page, page_size, sort_dir, event)
    return svc.native_history(db, page, page_size, sort_dir, event)


@router.get("/history/index", summary="Scrubber groups for the download history (native only)")
def history_index(
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern=SORT_DIR_PATTERN),
    event: Optional[str] = Query(None),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    validate_sort_key(sort_key, svc.HISTORY_SORT_KEYS, "date")
    if event is not None and event not in svc.HISTORY_EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event; allowed: {', '.join(svc.HISTORY_EVENTS)}",
        )
    if _is_lidarr(db):
        return svc.empty_index("date", sort_dir)
    return svc.native_history_index(db, sort_dir, event)


@router.post("/history/{history_id}/failed", summary="Mark a grab as failed: blocklist it and search again")
def mark_history_failed(
    history_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    def native() -> dict[str, Any]:
        try:
            result = svc.native_mark_history_failed(db, history_id)
        except svc.HistoryConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="History item not found")
        return result

    def lidarr() -> dict[str, Any]:
        numeric = lidarr_numeric_id(history_id, "History item")
        lidarr_call(require_lidarr(client).mark_history_failed, numeric)
        return {"success": True, "message": "Marked as failed in Lidarr, which blocklists the release and searches again"}

    return run_mutation(db, native, lidarr)


# ---------------------------------------------------------------------------------------------------- blocklist


@router.get("/blocklist", summary="Blocklisted releases (native or Lidarr)")
def list_blocklist(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=200),
    sort_key: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern=SORT_DIR_PATTERN),
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    key = validate_sort_key(sort_key, svc.BLOCKLIST_SORT_KEYS, "date")
    if _is_lidarr(db):
        return lidarr_call(svc.lidarr_blocklist, require_lidarr(client), page, page_size, key, sort_dir)
    return svc.native_blocklist(db, page, page_size, key, sort_dir)


@router.delete("/blocklist/{blocklist_id}", summary="Remove an entry from the blocklist")
def delete_blocklist_item(
    blocklist_id: str,
    db: Database = Depends(get_db),
    client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    def native() -> dict[str, Any]:
        if not db.remove_from_blocklist(blocklist_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blocklist item not found")
        return {"success": True, "message": "Removed from the blocklist"}

    def lidarr() -> dict[str, Any]:
        numeric = lidarr_numeric_id(blocklist_id, "Blocklist item")
        lidarr_call(require_lidarr(client).delete_blocklist_item, numeric)
        return {"success": True, "message": "Removed from the Lidarr blocklist"}

    return run_mutation(db, native, lidarr)
