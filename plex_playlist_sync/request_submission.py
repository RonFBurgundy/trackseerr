"""Shared request-submission policy: per-type quotas, duplicate check, auto-approval and native grab kick-off.

Used by the requests route (single and batch) and by tailored mixes so that every path that creates a
``music_requests`` row applies the same rules. The caller supplies a user row (loaded from the DB for
background work).

Request types are ``track``, ``album`` and ``discography`` (a batch of up to 50 albums of one artist that
consumes one discography unit and no album units). Each type has its own quota and its own auto-approve bit
(``UserPermission.AUTO_APPROVE`` = tracks, ``AUTO_APPROVE_ALBUM`` = albums, ``AUTO_APPROVE_DISCOGRAPHY`` =
discographies). Admins are unlimited and always auto-approved.
"""

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator
from plex_playlist_sync.item_history import (
    GrabTrigger,
    emit_named,
    request_trigger,
    trigger_kwargs,
)
from plex_playlist_sync.library_manager import ModeChanged, dispatch_to_lidarr, native_is_configured, run_for_mode
from plex_playlist_sync.models import MusicRequest, NotificationEvent, RequestStatus, UserPermission
from plex_playlist_sync.notifications import notification_dispatcher
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

_locks_guard = threading.Lock()
_user_locks: dict[str, threading.RLock] = {}


def user_request_lock(user_id: str) -> threading.RLock:
    """Per-user lock serialising count-then-insert. Re-entrant so callers may hold it across submit_track_request."""
    key = str(user_id)
    with _locks_guard:
        lock = _user_locks.get(key)
        if lock is None:
            lock = _user_locks[key] = threading.RLock()
        return lock


class RequestRejected(Exception):
    """Policy refused the request. ``code`` is ``quota`` or ``duplicate``; ``status_code`` mirrors the HTTP mapping."""

    def __init__(self, code: str, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.status_code = status_code
        self.detail = detail


@dataclass
class RequestSubmission:
    request: dict[str, Any]
    status: RequestStatus
    grabbed: bool = False


def _has_permission(user: dict[str, Any], permission: UserPermission) -> bool:
    # Deferred import: dependencies pulls in the whole API layer.
    from plex_playlist_sync.api.dependencies import has_permission

    return has_permission(user, permission)


MAX_BATCH_ITEMS = 50
QUOTA_TYPES = ("track", "album", "discography")
_QUOTA_LABELS = {"track": "tracks", "album": "albums", "discography": "discographies"}
_APPROVE_BIT = {
    "track": UserPermission.AUTO_APPROVE,
    "album": UserPermission.AUTO_APPROVE_ALBUM,
    "discography": UserPermission.AUTO_APPROVE_DISCOGRAPHY,
}


def is_unlimited(user: dict[str, Any]) -> bool:
    """Admins are exempt from quotas. A gateway-forwarded principal is never an admin."""
    return bool(user.get("is_admin")) and not user.get("forwarded")


def _quota_type(item_type: Optional[str]) -> str:
    return "album" if item_type == "album" else "track"


def effective_quota_limits(db: Database, user_id: str) -> dict[str, int]:
    """Per-type limits for a user (their override, else the global default), ignoring admin status."""
    defaults = db.get_account_settings()
    overrides = db.get_user_quota_overrides(user_id)

    def pick(override_key: str, default_key: str) -> int:
        value = overrides[override_key]
        return int(value if value is not None else defaults[default_key])

    return {
        "tracks": pick("quota_tracks", "default_quota_tracks"),
        "albums": pick("quota_albums", "default_quota_albums"),
        "discographies": pick("quota_discographies", "default_quota_discographies"),
        "window_days": pick("quota_window_days", "default_quota_window_days"),
    }


def quota_snapshot(db: Database, user: dict[str, Any]) -> dict[str, Any]:
    """Limits (``None`` for an unlimited admin), the window and usage, from the one counting function."""
    limits = effective_quota_limits(db, user["id"])
    window = max(1, limits["window_days"])
    used = db.count_user_requests_by_type(user["id"], window)
    unlimited = is_unlimited(user)
    return {
        "tracks": None if unlimited else limits["tracks"],
        "albums": None if unlimited else limits["albums"],
        "discographies": None if unlimited else limits["discographies"],
        "window_days": window,
        "used": {"tracks": used["track"], "albums": used["album"], "discographies": used["discography"]},
    }


def is_auto_approved(user: dict[str, Any], config: Any, kind: str) -> bool:
    """Whether a request of ``kind`` skips admin approval for this user."""
    if is_unlimited(user) or bool(getattr(config, "auto_approve_requests", False)):
        return True
    return _has_permission(user, _APPROVE_BIT[kind])


def _enforce_quota(db: Database, user: dict[str, Any], needs: dict[str, int]) -> None:
    """Raises ``RequestRejected`` if adding ``needs`` (units per type) would exceed any limit. Admins pass."""
    if is_unlimited(user):
        return
    snap = quota_snapshot(db, user)
    window = snap["window_days"]
    for kind in QUOTA_TYPES:
        need = needs.get(kind, 0)
        if need <= 0:
            continue
        label = _QUOTA_LABELS[kind]
        limit = int(snap[label])
        if snap["used"][label] + need > limit:
            unit = "day" if window == 1 else "days"
            raise RequestRejected("quota", 400, f"Request quota reached for {label} ({limit} per {window} {unit})")


def submit_track_request(
    db: Database,
    config: Any,
    user: dict[str, Any],
    title: str,
    artist: str,
    album: Optional[str] = None,
    quality_profile_id: Optional[str] = None,
    source: str = "api",
    *,
    item_type: str = "track",
    cover_url: Optional[str] = None,
    release_date: Optional[str] = None,
    foreign_id: Optional[str] = None,
    preview_url: Optional[str] = None,
    defer_followups: bool = False,
    lidarr_client: Any = None,
    trigger: Optional[GrabTrigger] = None,
) -> RequestSubmission:
    """Apply request policy and create the request. Raises ``RequestRejected`` on quota or duplicate.

    Non-admins cannot choose a quality profile: it is dropped. The per-user lock covers only the
    count-check plus insert. Notifications and the native grab (network I/O) run after it is released; a caller
    that holds the lock itself passes ``defer_followups=True`` and calls ``run_submission_followups`` once released.

    ``trigger`` names a system source (mix, import list, playlist) that raised the request on the user's behalf; it is
    kept on the request so the eventual grab still carries it. Without one the request is the user's own.
    """
    clean_title = title.strip()
    clean_artist = artist.strip()
    clean_album = album.strip() if album else None
    is_admin = bool(user.get("is_admin"))
    if not is_admin:
        quality_profile_id = None

    kind = _quota_type(item_type)
    with user_request_lock(user["id"]):
        if not is_admin:
            _enforce_quota(db, user, {kind: 1})
            for r in db.list_requests(user_id=user["id"]):
                if r.get("status") in ("pending", "processing", "approved") and (
                    r.get("artist", "").lower() == clean_artist.lower()
                    and r.get("title", "").lower() == clean_title.lower()
                ):
                    raise RequestRejected(
                        "duplicate", 409, "You have already submitted an active request for this item"
                    )

        initial_status = RequestStatus.PROCESSING if is_auto_approved(user, config, kind) else RequestStatus.PENDING

        req_id = f"req-{uuid.uuid4().hex[:12]}"
        created = db.create_request(
            MusicRequest(
                id=req_id,
                user_id=user["id"],
                item_type=item_type,
                title=clean_title,
                artist=clean_artist,
                album=clean_album,
                cover_url=cover_url,
                status=initial_status,
                release_date=release_date,
                foreign_id=foreign_id,
                preview_url=preview_url,
                quality_profile_id=quality_profile_id,
                trigger=trigger.kind if trigger else None,
                trigger_ref=trigger.ref if trigger else None,
                trigger_label=trigger.label if trigger else None,
            )
        )

    _record_requested(db, user, created, initial_status)
    submission = RequestSubmission(request=created, status=initial_status)
    if not defer_followups:
        run_submission_followups(db, user, submission, source=source, config=config, lidarr_client=lidarr_client)
    return submission


def record_request_event(
    db: Database,
    event: str,
    request: dict[str, Any],
    *,
    message: str = "",
    actor_user_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """``requested`` / ``request_approved`` / ``request_declined`` on the item behind ``request``.

    The item is found by name (and adopts the request's ``request_id`` when it is not in the library yet, see
    ``record_item_event``). The trigger is the request's own provenance; ``actor_*`` names an admin acting on it.
    """
    trig = request_trigger(db, request, actor_user_id=actor_user_id)
    is_album = (request.get("item_type") or "track") == "album"
    emit_named(
        db, event,
        artist=request.get("artist"),
        album=request.get("title") if is_album else request.get("album"),
        title=None if is_album else request.get("title"),
        request_id=request.get("id"),
        message=message,
        details={"item_type": request.get("item_type"), **(details or {})},
        **trigger_kwargs(trig),
    )


def _record_requested(db: Database, user: dict[str, Any], created: dict[str, Any], status: RequestStatus) -> None:
    """``requested`` (and ``request_approved`` when auto-approved) item events for a freshly created request."""
    req = {**created, "username": created.get("username") or user.get("username")}
    record_request_event(db, "requested", req, message="Requested")
    if status == RequestStatus.PROCESSING:
        record_request_event(
            db, "request_approved", req, message="Approved automatically", details={"automatic": True}
        )


def _norm(text: Optional[str]) -> str:
    return (text or "").strip().casefold()


def submit_batch_requests(
    db: Database,
    config: Any,
    user: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    kind: Optional[str] = None,
    artist: Optional[str] = None,
) -> list[RequestSubmission]:
    """Creates a batch under one hold of ``user_request_lock`` and returns the created submissions.

    Without ``kind`` the batch is up to 50 independent track/album requests: each consumes its own type's quota
    and follows its own approval bit. With ``kind="discography"`` it is up to 50 albums that must all belong to
    ``artist`` (case-insensitive); it consumes one discography unit and no album units, its albums share a
    ``batch_id`` so they can be counted that way, and its approval follows the discography bit.

    Quota is checked for the whole batch before anything is inserted, so it is all-or-nothing. Items that
    duplicate an active request of the user, or an earlier item in the batch, are skipped. Raises
    ``RequestRejected`` with 422 for a malformed batch and 400 when a quota would be exceeded. Notifications and
    grabs are the caller's job (``run_submission_followups`` or equivalent) after this returns.
    """
    if not items:
        raise RequestRejected("invalid", 422, "A batch needs at least one request")
    if len(items) > MAX_BATCH_ITEMS:
        raise RequestRejected("invalid", 422, f"A batch may contain at most {MAX_BATCH_ITEMS} requests")
    if kind not in (None, "discography"):
        raise RequestRejected("invalid", 422, "Unknown batch kind")
    is_admin = bool(user.get("is_admin"))
    batch_artist = (artist or "").strip()
    if kind == "discography":
        if not batch_artist:
            raise RequestRejected("invalid", 422, "A discography request needs the artist")
        for it in items:
            if it.get("item_type", "album") != "album":
                raise RequestRejected("invalid", 422, "A discography request may only contain albums")
            if _norm(it.get("artist")) != _norm(batch_artist):
                raise RequestRejected("invalid", 422, "All albums of a discography request must be by the same artist")

    with user_request_lock(user["id"]):
        existing_keys: set[tuple[str, str]] = set()
        existing_fids: set[str] = set()
        if not is_admin:
            for r in db.list_requests(user_id=user["id"]):
                if r.get("status") in ("pending", "processing", "approved"):
                    existing_keys.add((_norm(r.get("artist")), _norm(r.get("title"))))
                    if r.get("foreign_id"):
                        existing_fids.add(r["foreign_id"])

        seen_keys: set[tuple[str, str]] = set()
        seen_fids: set[str] = set()
        fresh: list[dict[str, Any]] = []
        for it in items:
            title = (it.get("title") or "").strip()
            art = (it.get("artist") or "").strip()
            fid = (it.get("foreign_id") or "").strip() or None
            key = (art.casefold(), title.casefold())
            if not is_admin and (key in existing_keys or (fid and fid in existing_fids)):
                continue
            if key in seen_keys or (fid and fid in seen_fids):
                continue
            seen_keys.add(key)
            if fid:
                seen_fids.add(fid)
            fresh.append({**it, "title": title, "artist": art, "foreign_id": fid})
        if not fresh:
            return []

        if kind == "discography":
            needs = {"discography": 1}
        else:
            needs = {"track": 0, "album": 0}
            for it in fresh:
                needs[_quota_type(it.get("item_type"))] += 1
        _enforce_quota(db, user, needs)

        batch_id = f"batch-{uuid.uuid4().hex[:12]}" if kind == "discography" else None
        submissions: list[RequestSubmission] = []
        for it in fresh:
            item_kind = "discography" if kind == "discography" else _quota_type(it.get("item_type"))
            initial_status = (
                RequestStatus.PROCESSING if is_auto_approved(user, config, item_kind) else RequestStatus.PENDING
            )
            album = (it.get("album") or "").strip() or None
            created = db.create_request(
                MusicRequest(
                    id=f"req-{uuid.uuid4().hex[:12]}",
                    user_id=user["id"],
                    item_type=it.get("item_type") or "album",
                    title=it["title"],
                    artist=it["artist"],
                    album=album,
                    cover_url=it.get("cover_url"),
                    status=initial_status,
                    release_date=it.get("release_date"),
                    foreign_id=it["foreign_id"],
                    preview_url=it.get("preview_url"),
                    batch_id=batch_id,
                    batch_kind=kind,
                )
            )
            _record_requested(db, user, created, initial_status)
            submissions.append(RequestSubmission(request=created, status=initial_status))
    return submissions


def run_submission_followups(
    db: Database,
    user: dict[str, Any],
    submission: RequestSubmission,
    source: str = "api",
    config: Any = None,
    lidarr_client: Any = None,
) -> RequestSubmission:
    """Dispatch notifications, then hand an approved request to the active library manager.

    Native mode runs the acquisition coordinator and never touches Lidarr; lidarr mode sends the request to Lidarr
    (using ``lidarr_settings``) and never touches the coordinator. Must run without ``user_request_lock`` held.
    """
    created = submission.request
    initial_status = submission.status
    req_id = created["id"]
    clean_artist = created.get("artist") or ""
    clean_title = created.get("title") or ""
    clean_album = created.get("album")
    item_type = created.get("item_type") or "track"

    notification_data = dict(created)
    if not notification_data.get("username"):
        notification_data["username"] = user.get("username")
    notification_dispatcher.dispatch(NotificationEvent.REQUEST_CREATED, data=notification_data, db=db)
    if initial_status == RequestStatus.PROCESSING:
        notification_dispatcher.dispatch(NotificationEvent.REQUEST_APPROVED, data=notification_data, db=db)

    grabbed = False

    def _lidarr_followup() -> None:
        dispatch_to_lidarr(
            db,
            lidarr_client,
            [
                {
                    "id": req_id,
                    "artist": clean_artist,
                    "album": (clean_album or clean_title) if item_type == "album" else (clean_album or ""),
                    "title": clean_title,
                    "item_type": item_type,
                    "is_request": True,
                }
            ],
            config,
        )

    def _native_followup() -> bool:
        if not native_is_configured(db):
            return False
        try:
            grab_res = acquisition_coordinator.search_and_grab(
                artist=clean_artist,
                title=clean_title,
                album=clean_album,
                item_type=item_type,
                request_id=req_id,
                db=db,
                trigger=request_trigger(db, {**created, "username": created.get("username") or user.get("username")}),
            )
            if grab_res.get("success"):
                logger.info(
                    "Native acquisition grabbed request %s (%s - %s) [%s]", req_id, clean_artist, clean_title, source
                )
                return True
            logger.info("Native acquisition found no match for request %s: %s", req_id, grab_res.get("message"))
        except Exception as e:  # coordinator drivers raise heterogeneous errors; the request stays approved
            logger.error("Error in native acquisition for request %s (%s)", req_id, type(e).__name__)
        return False

    if initial_status == RequestStatus.PROCESSING:
        try:
            grabbed = bool(run_for_mode(db, native=_native_followup, lidarr=_lidarr_followup))
        except ModeChanged:
            logger.warning("Library manager kept changing; request %s stays in processing", req_id)

    submission.grabbed = grabbed
    return submission
