"""Requests REST API endpoints for user requests, approval workflows, and Lidarr dispatch."""

import logging
import os
from typing import Any, Literal, Optional

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from trackseerr.acquisition_coordinator import acquisition_coordinator
from trackseerr.redaction import redact_text
from trackseerr.api.dependencies import (
    get_config,
    get_current_user,
    get_db,
    get_lidarr_client,
    has_permission,
    require_admin,
    require_admin_or_permission,
    require_user,
)
from trackseerr.api.schemas.requests import (
    BatchCreatedResponse,
    RequestDeleted,
    RequestListResponse,
    RequestRecord,
    RetryResult,
)
from trackseerr.clients.core_client import CoreClient
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.config import Config
from trackseerr.item_history import TRIGGER_REQUEST_APPROVED, TRIGGER_RETRY, request_trigger
from trackseerr.library_manager import ModeChanged, dispatch_to_lidarr, native_is_configured, run_for_mode
from trackseerr.models import (
    NotificationEvent,
    RequestStatus,
    UserPermission,
)
from trackseerr.notifications import notification_dispatcher
from trackseerr.request_submission import (
    MAX_BATCH_ITEMS,
    RequestRejected,
    record_request_event,
    submit_batch_requests,
    submit_track_request,
)
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateMusicRequestBody(BaseModel):
    item_type: str = Field(default="album", pattern="^(album|track)$")
    title: str = Field(..., min_length=1)
    artist: str = Field(..., min_length=1)
    album: Optional[str] = None
    cover_url: Optional[str] = None
    release_date: Optional[str] = None
    foreign_id: Optional[str] = None
    preview_url: Optional[str] = None


class BatchCreateMusicRequestBody(BaseModel):
    """Up to 50 requests. ``kind="discography"`` (with ``artist``) makes it one discography request."""

    requests: list[CreateMusicRequestBody] = Field(..., min_length=1, max_length=MAX_BATCH_ITEMS)
    kind: Optional[Literal["discography"]] = None
    artist: Optional[str] = Field(default=None, max_length=512)


@router.get("", response_model=RequestListResponse, response_model_exclude_unset=True)
def list_requests(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Lists requests. Non-admins see only their own requests; admins and request managers see all."""
    can_manage = bool(current_user.get("is_admin")) or has_permission(current_user, UserPermission.MANAGE_REQUESTS)
    user_id = None if can_manage else current_user["id"]
    requests = db.list_requests(user_id=user_id, status=status_filter)
    return {"requests": requests, "count": len(requests)}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RequestRecord, response_model_exclude_unset=True)
def create_request(
    body: CreateMusicRequestBody,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(require_user),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> dict[str, Any]:
    """Creates a new music request.

    If the user is admin or auto_approve_requests is enabled, transitions immediately
    to processing and dispatches to lidarr_worker (if Lidarr is configured).
    Otherwise, sets status to pending.
    """
    if not has_permission(current_user, UserPermission.REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: requires REQUEST",
        )

    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway" and config.trackseerr_core_url:
        core_client = CoreClient(
            core_url=config.trackseerr_core_url,
            secret=config.internal_core_secret,
        )
        try:
            return core_client.forward_request(body.model_dump(), user_info=current_user)
        except httpx.HTTPStatusError as exc:
            try:
                err_detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                err_detail = exc.response.text
            raise HTTPException(status_code=exc.response.status_code, detail=err_detail) from exc
        except (httpx.RequestError, Exception) as exc:
            logger.error("Failed to forward request to Core at %s: %s", config.trackseerr_core_url, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to communicate with TrackSeerr Core engine",
            ) from exc

    clean_title = body.title.strip()
    clean_artist = body.artist.strip()
    clean_album = body.album.strip() if body.album else None

    try:
        submission = submit_track_request(
            db,
            config,
            current_user,
            clean_title,
            clean_artist,
            clean_album,
            source="api",
            item_type=body.item_type,
            cover_url=body.cover_url,
            release_date=body.release_date,
            foreign_id=body.foreign_id,
            preview_url=body.preview_url,
            lidarr_client=lidarr_client,
        )
    except RequestRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    created = submission.request

    # The shared submission already handed the request to the active library manager (native grab or Lidarr).
    return created


@router.post("/batch", status_code=status.HTTP_201_CREATED, response_model=BatchCreatedResponse, response_model_exclude_unset=True)
def create_batch_requests(
    body: BatchCreateMusicRequestBody,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(require_user),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> dict[str, Any]:
    """Creates multiple music requests (at most 50) within the user's per-type quotas.

    A ``kind="discography"`` batch of one artist's albums consumes one discography unit; otherwise each item
    consumes its own type's quota. Duplicates against active requests or within the batch are skipped.
    Approved requests attempt native grab, falling back to Lidarr trickle worker.
    """
    if not has_permission(current_user, UserPermission.REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: requires REQUEST",
        )

    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway" and config.trackseerr_core_url:
        core_client = CoreClient(
            core_url=config.trackseerr_core_url,
            secret=config.internal_core_secret,
        )
        try:
            return core_client.forward_batch_requests(body.model_dump(), user_info=current_user)
        except httpx.HTTPStatusError as exc:
            try:
                err_detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                err_detail = exc.response.text
            raise HTTPException(status_code=exc.response.status_code, detail=err_detail) from exc
        except (httpx.RequestError, Exception) as exc:
            logger.error("Failed to forward batch requests to Core at %s: %s", config.trackseerr_core_url, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to communicate with TrackSeerr Core engine",
            ) from exc

    # Quota, duplicate handling, approval and the inserts all run under the per-user lock inside
    # submit_batch_requests; network follow-ups (notifications, grabs) below run after it is released.
    try:
        submissions = submit_batch_requests(
            db,
            config,
            current_user,
            [item.model_dump() for item in body.requests],
            kind=body.kind,
            artist=body.artist,
        )
    except RequestRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    created_items: list[dict[str, Any]] = [sub.request for sub in submissions]

    for created in created_items:
        # Dispatch notification events
        notification_data = dict(created)
        if not notification_data.get("username"):
            notification_data["username"] = current_user.get("username")
        if not notification_data.get("requested_by") and notification_data.get("user_id"):
            notification_data["requested_by"] = notification_data["user_id"]
        if current_user.get("id"):
            notification_data["actor_user_id"] = str(current_user["id"])
        notification_dispatcher.dispatch(NotificationEvent.REQUEST_CREATED, data=notification_data, db=db)
        if created.get("status") in (RequestStatus.PROCESSING.value, "processing"):
            notification_dispatcher.dispatch(NotificationEvent.REQUEST_APPROVED, data=notification_data, db=db)

    # Strictly mode-driven: lidarr mode sends everything to Lidarr, native mode only ever uses the coordinator.
    processing_items = [
        c for c in created_items if c.get("status") in (RequestStatus.PROCESSING.value, "processing")
    ]
    if processing_items:
        def _lidarr_batch() -> None:
            dispatch_to_lidarr(
                db,
                lidarr_client,
                [
                    {
                        "id": c["id"],
                        "artist": c["artist"],
                        "album": (c.get("album") or c["title"]) if c.get("item_type") == "album" else (c.get("album") or ""),
                        "title": c["title"],
                        "item_type": c.get("item_type") or "track",
                        "is_request": True,
                    }
                    for c in processing_items
                ],
                config,
            )

        def _native_batch() -> None:
            if not native_is_configured(db):
                return
            for created in processing_items:
                req_id = created["id"]
                try:
                    grab_res = acquisition_coordinator.search_and_grab(
                        artist=created["artist"],
                        title=created["title"],
                        album=created.get("album"),
                        item_type=created.get("item_type", "track"),
                        request_id=req_id,
                        db=db,
                        trigger=request_trigger(db, {**created, "username": created.get("username") or current_user.get("username")}),
                    )
                    if grab_res.get("success"):
                        logger.info(
                            "Native acquisition grabbed batch request %s (%s - %s)",
                            req_id,
                            created["artist"],
                            created["title"],
                        )
                    else:
                        logger.info(
                            "Native acquisition found no match for batch request %s: %s",
                            req_id,
                            grab_res.get("message"),
                        )
                except Exception as e:  # coordinator drivers raise heterogeneous errors; the request stays approved
                    logger.error("Error in native acquisition for batch request %s (%s)", req_id, type(e).__name__)

        try:
            run_for_mode(db, native=_native_batch, lidarr=_lidarr_batch)
        except ModeChanged:
            logger.warning("Library manager kept changing; %d approved request(s) stay in processing", len(processing_items))

    return {"created": created_items, "count": len(created_items)}


def _actor_id(user: dict[str, Any]) -> Optional[str]:
    uid = user.get("id")
    return str(uid) if uid and uid != "api_key_user" else None


@router.post("/{request_id}/approve", response_model=RequestRecord, response_model_exclude_unset=True)
def approve_request(
    request_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(require_admin_or_permission(UserPermission.MANAGE_REQUESTS)),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> dict[str, Any]:
    """Endpoint to approve a request, updating status to processing and dispatching to Lidarr or native acquisition."""
    req = db.get_request(request_id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    db.update_request_status(request_id, RequestStatus.PROCESSING)
    updated = db.get_request(request_id)
    record_request_event(
        db, "request_approved", req, message="Approved",
        actor_user_id=_actor_id(current_user),
    )

    def _lidarr_approve() -> None:
        dispatch_to_lidarr(
            db,
            lidarr_client,
            [
                {
                    "id": request_id,
                    "artist": req["artist"],
                    "album": (req.get("album") or req["title"]) if req.get("item_type") == "album" else (req.get("album") or ""),
                    "title": req["title"],
                    "item_type": req.get("item_type") or "track",
                    "is_request": True,
                }
            ],
            config,
        )

    def _native_approve() -> None:
        if not native_is_configured(db):
            return
        try:
            grab_res = acquisition_coordinator.search_and_grab(
                artist=req["artist"],
                title=req["title"],
                album=req.get("album"),
                item_type=req.get("item_type", "track"),
                request_id=request_id,
                db=db,
                trigger=request_trigger(db, req, kind=TRIGGER_REQUEST_APPROVED, actor_user_id=_actor_id(current_user)),
            )
            if grab_res.get("success"):
                logger.info("Native acquisition grabbed approved request %s (%s - %s)", request_id, req["artist"], req["title"])
            else:
                logger.info("Native acquisition found no match for approved request %s: %s", request_id, grab_res.get("message"))
        except Exception as e:  # coordinator drivers raise heterogeneous errors; the request stays approved
            logger.error("Error in native acquisition for approved request %s (%s)", request_id, type(e).__name__)

    try:
        run_for_mode(db, native=_native_approve, lidarr=_lidarr_approve)
    except ModeChanged:
        logger.warning("Library manager kept changing; approved request %s stays in processing", request_id)

    res_req = updated or req
    dispatch_data = dict(res_req)
    if not dispatch_data.get("requested_by") and dispatch_data.get("user_id"):
        dispatch_data["requested_by"] = dispatch_data["user_id"]
    if current_user.get("id"):
        dispatch_data["actor_user_id"] = str(current_user["id"])
    notification_dispatcher.dispatch(NotificationEvent.REQUEST_APPROVED, data=dispatch_data, db=db)
    return res_req


@router.post("/{request_id}/reject", response_model=RequestRecord, response_model_exclude_unset=True)
def reject_request(
    request_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin_or_permission(UserPermission.MANAGE_REQUESTS)),
) -> dict[str, Any]:
    """Endpoint to reject a request."""
    req = db.get_request(request_id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    db.update_request_status(request_id, RequestStatus.REJECTED)
    updated = db.get_request(request_id)
    record_request_event(
        db, "request_declined", req, message="Declined",
        actor_user_id=_actor_id(current_user),
    )
    res_req = updated or req
    dispatch_data = dict(res_req)
    if not dispatch_data.get("requested_by") and dispatch_data.get("user_id"):
        dispatch_data["requested_by"] = dispatch_data["user_id"]
    if current_user.get("id"):
        dispatch_data["actor_user_id"] = str(current_user["id"])
    notification_dispatcher.dispatch(NotificationEvent.REQUEST_REJECTED, data=dispatch_data, db=db)
    return res_req


@router.delete("/{request_id}", response_model=RequestDeleted, response_model_exclude_unset=True)
def delete_request(
    request_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Deletes a request. Requesters can delete pending requests; admins can delete any."""
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway" and config.trackseerr_core_url:
        core_client = CoreClient(
            core_url=config.trackseerr_core_url,
            secret=config.internal_core_secret,
        )
        try:
            ok = core_client.forward_delete_request(request_id, user_info=current_user)
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to delete request on TrackSeerr Core",
                )
            return {"status": "deleted", "id": request_id}
        except HTTPException:
            raise
        except (httpx.RequestError, Exception) as exc:
            logger.error("Failed to forward delete request to Core at %s: %s", config.trackseerr_core_url, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to communicate with TrackSeerr Core engine",
            ) from exc

    req = db.get_request(request_id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    is_admin = bool(current_user.get("is_admin"))
    is_owner = str(req["user_id"]) == str(current_user["id"])

    if not is_admin and not is_owner:
        # 404, not 403: do not reveal that another user's request exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    if not is_admin and req["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending requests may be canceled by standard users",
        )

    deleted = db.delete_request(request_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete request",
        )

    return {"status": "deleted", "id": request_id}


@router.post("/{request_id}/retry", response_model=RetryResult, response_model_exclude_unset=True)
def retry_request(
    request_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> dict[str, Any]:
    """Admin-only: forces re-search and grab for an existing request."""
    req = db.get_request(request_id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    if req.get("status") == "available":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request is already fulfilled and available",
        )

    db.update_request_status(request_id, RequestStatus.PROCESSING)

    clean_artist = req.get("artist", "").strip()
    clean_title = req.get("title", "").strip()
    clean_album = req.get("album", "").strip() if req.get("album") else None
    item_type = req.get("item_type", "track")

    def _lidarr_retry() -> dict[str, Any]:
        sent = dispatch_to_lidarr(
            db,
            lidarr_client,
            [
                {
                    "id": request_id,
                    "artist": clean_artist,
                    "album": (clean_album or clean_title) if item_type == "album" else (clean_album or ""),
                    "title": clean_title,
                    "item_type": item_type,
                    "is_request": True,
                }
            ],
            config,
        )
        if sent:
            lidarr_msg = "Enqueued to Lidarr for search"
        elif lidarr_client is None:
            lidarr_msg = "Lidarr is not configured"
        else:
            lidarr_msg = "Lidarr did not accept the request; it stays in processing"
        return {"success": sent, "status": "processing", "message": lidarr_msg, "download_id": None}

    def _native_retry() -> dict[str, Any]:
        grabbed = False
        download_id: Optional[str] = None
        msg = "Acquisition initiated"
        if native_is_configured(db):
            try:
                grab_res = acquisition_coordinator.search_and_grab(
                    artist=clean_artist,
                    title=clean_title,
                    album=clean_album,
                    item_type=item_type,
                    request_id=request_id,
                    db=db,
                    trigger=request_trigger(db, req, kind=TRIGGER_RETRY, actor_user_id=_actor_id(_admin)),
                )
                if grab_res.get("success"):
                    grabbed = True
                    download_id = grab_res.get("download_id")
                    msg = f"Grabbed release: {grab_res.get('release')}"
                    logger.info(
                        "Native acquisition grabbed retried request %s (%s - %s)",
                        request_id,
                        clean_artist,
                        clean_title,
                    )
                else:
                    msg = grab_res.get("message") or "No matching release found on indexers"
                    logger.info(
                        "Native acquisition found no match for retried request %s: %s",
                        request_id,
                        msg,
                    )
            except Exception as e:  # coordinator drivers raise heterogeneous errors
                logger.error("Error in native acquisition for retried request %s: %s", request_id, redact_text(str(e)))
                msg = f"Acquisition error: {redact_text(str(e))}"
        return {
            "success": grabbed,
            "status": "processing",
            "message": msg,
            "download_id": download_id,
        }

    try:
        return run_for_mode(db, native=_native_retry, lidarr=_lidarr_retry)
    except ModeChanged as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The library manager is being switched; try again in a moment.",
        ) from exc
