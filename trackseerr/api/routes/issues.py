"""Media Issues REST API: reporting, discussion, lifecycle and admin fix actions.

Placement (see docs/design/two-tier-security.md): create / list / get / comments / unread-count / seen / status are
user-scoped and forwarded gateway -> core as the signed-in user (never admin). ``open-count``, ``PUT``, ``DELETE`` and
``actions`` are core-only admin routes and are on no gateway allowlist.
"""

import logging
import re
import uuid
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from trackseerr.api.response_models import ApiModel

from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_lidarr_client,
    has_permission,
    require_admin,
    require_core_tier,
    require_permission,
    require_user,
)
from trackseerr.api.schemas.issues import IssueCount, IssueDeleted, IssueSeen
from trackseerr.api.routes.library.manual_import import manual_import_album_tracks
from trackseerr.api.routes import requests as requests_routes
from trackseerr.api.routes import wanted as wanted_routes
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.config import Config
from trackseerr.item_history import TRIGGER_USER, emit, emit_named
from trackseerr.library_manager import MODE_NATIVE, get_library_mode
from trackseerr.models import IssueStatus, IssueType, NotificationEvent, UserPermission
from trackseerr.notifications import notification_dispatcher
from trackseerr.request_submission import user_request_lock
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

ISSUE_DAILY_LIMIT = 10
ISSUE_COMMENT_LIMIT = 200
ISSUE_NOTIFICATION_DETAILS_MAX = 500

FINAL_STATUSES = frozenset({IssueStatus.RESOLVED.value, IssueStatus.WONT_FIX.value})
# Request states from which "this request is stuck" makes no sense.
FINAL_REQUEST_STATUSES = frozenset({"available", "rejected"})

ACTION_RETRY_REQUEST = "retry_request"
ACTION_RESEARCH = "research"
ACTION_BLOCKLIST_AND_RESEARCH = "blocklist_and_research"
ACTION_REMATCH = "rematch"
ACTION_LABELS: dict[str, str] = {
    ACTION_RETRY_REQUEST: "Retry request",
    ACTION_RESEARCH: "Search again",
    ACTION_BLOCKLIST_AND_RESEARCH: "Blocklist release and search again",
    ACTION_REMATCH: "Manual import (rematch)",
}
_RESEARCH_TYPES = frozenset(
    {IssueType.CORRUPTED_FILE.value, IssueType.MISSING_TRACKS.value, IssueType.AUDIO_QUALITY.value}
)

_Stripped = Annotated[str, StringConstraints(strip_whitespace=True)]
# Control characters (C0 range, DEL, C1 range) other than \n and \t.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _is_admin(user: dict[str, Any]) -> bool:
    """True only for a real admin session or API key; gateway-forwarded principals never qualify."""
    if user.get("forwarded"):
        return False
    return bool(user.get("is_admin") or has_permission(user, UserPermission.ADMIN))


def _reject_control_chars(value: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    return value


class CreateIssueBody(BaseModel):
    media_title: _Stripped = Field(min_length=1, max_length=300)
    artist: _Stripped = Field(min_length=1, max_length=300)
    issue_type: IssueType
    problem_details: _Stripped = Field(min_length=1, max_length=2000)
    request_id: Optional[_Stripped] = Field(default=None, max_length=200)
    # Library references: admins only (requesters never learn library ids).
    album_id: Optional[_Stripped] = Field(default=None, max_length=200)
    track_id: Optional[_Stripped] = Field(default=None, max_length=200)
    # Discovery reference: what a requester can point at from a Discover album / track popup.
    discovery_id: Optional[_Stripped] = Field(default=None, max_length=200)
    item_type: Optional[Literal["album", "track"]] = None

    @field_validator("media_title", "artist", "problem_details")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        return _reject_control_chars(value)

    @field_validator("request_id", "album_id", "track_id", "discovery_id")
    @classmethod
    def _blank_ref_is_none(cls, value: Optional[str]) -> Optional[str]:
        return value or None

    @model_validator(mode="after")
    def _discovery_ref_is_complete(self) -> "CreateIssueBody":
        if bool(self.discovery_id) != bool(self.item_type):
            raise ValueError("discovery_id and item_type must be given together")
        return self


class UpdateIssueBody(BaseModel):
    """Admin PUT. Unknown fields are refused so nothing but these two can be smuggled in."""

    model_config = ConfigDict(extra="forbid")

    status: Optional[IssueStatus] = None
    problem_details: Optional[_Stripped] = Field(default=None, min_length=1, max_length=2000)

    @field_validator("problem_details")
    @classmethod
    def _no_control_chars(cls, value: Optional[str]) -> Optional[str]:
        return _reject_control_chars(value) if value is not None else value

    @model_validator(mode="after")
    def _something_to_change(self) -> "UpdateIssueBody":
        if self.status is None and self.problem_details is None:
            raise ValueError("provide status and/or problem_details")
        return self


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IssueStatus


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: _Stripped = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        return _reject_control_chars(value)


class IssueResponse(ApiModel):
    """Issue as returned to the viewer. Responses are built from a per-role whitelist and serialised with
    ``exclude_unset``, so admin-only fields are absent (not null) for requesters."""

    id: str
    media_title: str
    artist: str
    issue_type: str
    problem_details: str
    status: str
    user_id: str
    request_id: Optional[str] = None
    username: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    last_activity_at: Optional[str] = None
    comment_count: int = 0
    item_type: Optional[str] = None
    discovery_id: Optional[str] = None
    unread: Optional[bool] = None  # reporter's own view only
    # Admin only:
    album_id: Optional[str] = None
    track_id: Optional[str] = None
    available_actions: Optional[list[str]] = None


class CommentResponse(ApiModel):
    id: str
    issue_id: str
    body: str
    created_at: str
    is_admin: bool
    username: Optional[str] = None
    mine: bool = False
    # Admin only:
    user_id: Optional[str] = None
    is_system: Optional[bool] = None


class ActionResponse(ApiModel):
    action: str
    result: dict[str, Any]
    issue: IssueResponse


_PUBLIC_ISSUE_FIELDS = (
    "id", "media_title", "artist", "issue_type", "problem_details", "status", "user_id", "request_id",
    "username", "created_at", "updated_at", "resolved_at", "resolved_by", "last_activity_at", "item_type",
    "discovery_id",
)


# ------------------------------------------------------------------------------------------------ helpers


def _resolve_album_id(db: Database, issue: dict[str, Any]) -> Optional[str]:
    """The library album an issue points at (directly, or through its track); None when absent or gone."""
    album_id = issue.get("album_id")
    if not album_id and issue.get("track_id"):
        track = db.get_library_track(str(issue["track_id"]))
        album_id = track.get("album_id") if track else None
    if album_id and db.get_library_album(str(album_id)) is not None:
        return str(album_id)
    return None


def _available_actions(db: Database, issue: dict[str, Any], native: bool) -> list[str]:
    """Fix actions that make sense for this issue's type and linked references."""
    actions: list[str] = []
    request_id = issue.get("request_id")
    if request_id:
        req = db.get_request(str(request_id))
        if req and str(req.get("status")) not in FINAL_REQUEST_STATUSES:
            actions.append(ACTION_RETRY_REQUEST)
    if native and _resolve_album_id(db, issue) is not None:
        if issue["issue_type"] in _RESEARCH_TYPES:
            actions.append(ACTION_RESEARCH)
        if issue["issue_type"] == IssueType.WRONG_RELEASE.value:
            actions.append(ACTION_BLOCKLIST_AND_RESEARCH)
        actions.append(ACTION_REMATCH)
    return actions


def _present_issue(
    db: Database, issue: dict[str, Any], viewer: dict[str, Any], is_admin: bool, native: Optional[bool] = None
) -> dict[str, Any]:
    """Whitelisted issue dict for the viewer: admins also get library ids and ``available_actions``."""
    out: dict[str, Any] = {key: issue.get(key) for key in _PUBLIC_ISSUE_FIELDS}
    out["comment_count"] = int(issue["comment_count_all" if is_admin else "comment_count_public"])
    if str(issue["user_id"]) == str(viewer["id"]):
        out["unread"] = bool(issue.get("unread"))
    if is_admin:
        out["album_id"] = issue.get("album_id")
        out["track_id"] = issue.get("track_id")
        if native is None:
            native = get_library_mode(db) == MODE_NATIVE
        out["available_actions"] = _available_actions(db, issue, native)
    return out


def _present_comment(comment: dict[str, Any], viewer: dict[str, Any], is_admin: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": comment["id"],
        "issue_id": comment["issue_id"],
        "body": comment["body"],
        "created_at": comment["created_at"],
        "is_admin": bool(comment["is_admin"]),
        "username": comment.get("username"),
        "mine": comment.get("user_id") is not None and str(comment["user_id"]) == str(viewer["id"]),
    }
    if is_admin:
        out["user_id"] = comment.get("user_id")
        out["is_system"] = bool(comment["is_system"])
    return out


def _visible_issue(db: Database, issue_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """The issue for its reporter or an admin; everyone else gets the same 404 as for a missing issue."""
    issue = db.get_issue(issue_id)
    if not issue or (not _is_admin(user) and str(issue["user_id"]) != str(user["id"])):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Media issue {issue_id} not found")
    return issue


def _notify(
    db: Database,
    event: NotificationEvent,
    issue: dict[str, Any],
    actor_name: Optional[str],
    update: str,
    actor_user_id: Optional[str] = None,
) -> None:
    """Sends an issue event to the notification agents (admin-facing; carries no library ids or paths)."""
    data: dict[str, Any] = {
        key: issue.get(key)
        for key in (
            "id",
            "media_title",
            "artist",
            "issue_type",
            "status",
            "username",
            "request_id",
            "user_id",
        )
    }
    data["issue_id"] = issue.get("id")
    data["title"] = issue.get("media_title")
    data["problem_details"] = str(issue.get("problem_details") or "")[:ISSUE_NOTIFICATION_DETAILS_MAX]
    data["update"] = f"{update} by {actor_name}" if actor_name else update
    if actor_user_id is not None:
        data["actor_user_id"] = str(actor_user_id)
    notification_dispatcher.dispatch(event, data, db)


def _notify_status(
    db: Database,
    issue: dict[str, Any],
    actor_name: Optional[str],
    actor_user_id: Optional[str] = None,
) -> None:
    final = issue["status"] in FINAL_STATUSES
    event = NotificationEvent.ISSUE_RESOLVED if final else NotificationEvent.ISSUE_UPDATED
    _notify(db, event, issue, actor_name, f"Status changed to {issue['status']}", actor_user_id=actor_user_id)


def _conflict_response(detail: str, existing_issue_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": detail, "existing_issue_id": existing_issue_id},
    )


def _record_issue_event(
    db: Database, event: str, issue: dict[str, Any], user: dict[str, Any], message: str, **details: Any
) -> None:
    """``issue_opened`` / ``issue_resolved`` on the item an issue is about (library ids, else a name match)."""
    uid = user.get("id")
    common: dict[str, Any] = {
        "message": message,
        "trigger": TRIGGER_USER,
        "trigger_ref": str(issue["id"]),
        "trigger_label": user.get("username"),
        "actor_user_id": str(uid) if uid and uid != "api_key_user" else None,
        "request_id": issue.get("request_id"),
        "details": {"issue_id": issue["id"], "issue_type": issue.get("issue_type"), **details},
    }
    track_id = str(issue["track_id"]) if issue.get("track_id") else None
    album_id = _resolve_album_id(db, issue)
    if track_id or album_id:
        emit(db, event, track_id=track_id, album_id=album_id, **common)
    else:
        emit_named(
            db, event, artist=issue.get("artist"),
            album=issue.get("media_title") if issue.get("item_type") == "album" else None,
            title=issue.get("media_title") if issue.get("item_type") != "album" else None, **common,
        )


def _apply_status(
    db: Database, issue: dict[str, Any], new_status: str, user: dict[str, Any], is_admin: bool
) -> dict[str, Any]:
    """Validates and performs a status transition for ``user``; returns the refreshed issue.

    Admins may move an issue anywhere except to its current status. A reporter may only reopen (resolved / wont_fix
    -> open) or close (open / in_progress -> resolved) their own issue.
    """
    current = issue["status"]
    is_owner = str(issue["user_id"]) == str(user["id"])
    if new_status == current:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Issue is already {current}")
    if not is_admin:
        reopen = new_status == IssueStatus.OPEN.value and current in FINAL_STATUSES
        close = new_status == IssueStatus.RESOLVED.value and current in (
            IssueStatus.OPEN.value,
            IssueStatus.IN_PROGRESS.value,
        )
        if not (is_owner and (reopen or close)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only reopen or close your own issue",
            )
    staff = is_admin and not is_owner
    if not db.set_issue_status(issue["id"], new_status, str(user["id"]), staff, expected_status=current):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="The issue changed while you were editing it; reload it"
        )
    updated = db.get_issue(issue["id"])
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media issue not found")
    _notify_status(db, updated, user.get("username"), actor_user_id=str(user["id"]) if user.get("id") else None)
    if new_status in FINAL_STATUSES:
        _record_issue_event(db, "issue_resolved", updated, user, f"Issue {new_status}", status=new_status)
    return updated


# ------------------------------------------------------------------------------------------ user-scoped routes


@router.get("", response_model=list[IssueResponse], response_model_exclude_unset=True, summary="List media issues")
@router.get("/", response_model=list[IssueResponse], response_model_exclude_unset=True, include_in_schema=False)
def list_issues(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    media_title: Optional[str] = Query(default=None, max_length=300),
    artist: Optional[str] = Query(default=None, max_length=300),
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Lists issues. Filterable by status, media_title and artist (exact, case-insensitive).

    Admins see all; regular users see only their own, regardless of filters.
    """
    is_admin = _is_admin(current_user)
    rows = db.list_issues(
        status=status_filter,
        user_id=None if is_admin else current_user["id"],
        media_title=(media_title or "").strip() or None,
        artist=(artist or "").strip() or None,
    )
    native = get_library_mode(db) == MODE_NATIVE if is_admin else None
    return [_present_issue(db, row, current_user, is_admin, native) for row in rows]


@router.get("/unread-count", response_model=IssueCount, response_model_exclude_unset=True, summary="Issues with admin activity the reporter has not seen")
def unread_count(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, int]:
    return {"count": db.count_unread_issues(str(current_user["id"]))}


@router.get("/open-count", response_model=IssueCount, response_model_exclude_unset=True, summary="Open issues awaiting an admin (nav badge)")
def open_count(
    db: Database = Depends(get_db),
    _core: None = Depends(require_core_tier),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, int]:
    return {
        "count": db.count_issues_by_status(IssueStatus.OPEN.value),
        "in_progress": db.count_issues_by_status(IssueStatus.IN_PROGRESS.value),
    }


@router.post(
    "",
    response_model=IssueResponse,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
    summary="Report a media issue",
)
@router.post(
    "/",
    response_model=IssueResponse,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_issue(
    body: CreateIssueBody,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_permission(UserPermission.REPORT_ISSUE)),
) -> Any:
    """Creates an issue, emits ISSUE_REPORTED, and returns the created issue.

    Rejections: 403 (library ids from a non-admin), 404 (unknown / not-owned request or unknown library ref),
    409 (request already final, or a duplicate open issue: body carries ``existing_issue_id``), 422, 429 (24h cap).
    """
    is_admin = _is_admin(current_user)
    user_id = str(current_user["id"])
    issue_type = body.issue_type.value

    if issue_type == IssueType.REQUEST_STUCK.value and not body.request_id:
        raise HTTPException(status_code=422, detail="request_stuck issues require a request_id")
    if (body.album_id or body.track_id) and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Library references are only accepted from admins"
        )
    if body.album_id and db.get_library_album(body.album_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Album {body.album_id} not found")
    if body.track_id and db.get_library_track(body.track_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Track {body.track_id} not found")

    if body.request_id:
        request = db.get_request(body.request_id)
        if not request or (not is_admin and str(request["user_id"]) != user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Request {body.request_id} not found",
            )
        if issue_type == IssueType.REQUEST_STUCK.value and str(request.get("status")) in FINAL_REQUEST_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Request {body.request_id} is already {request.get('status')}; it cannot be stuck",
            )

    with user_request_lock(user_id):
        duplicate = db.find_active_duplicate_issue(
            user_id,
            body.media_title,
            body.artist,
            issue_type,
            request_id=body.request_id,
            album_id=body.album_id,
            track_id=body.track_id,
            discovery_id=body.discovery_id,
        )
        if duplicate:
            return _conflict_response(
                f"You already have an open issue for this item (issue {duplicate['id']})", str(duplicate["id"])
            )
        if not is_admin and db.count_recent_issues(user_id, hours=24) >= ISSUE_DAILY_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Issue limit reached: at most {ISSUE_DAILY_LIMIT} issues per 24 hours",
            )
        created = db.create_issue(
            {
                "id": f"issue-{uuid.uuid4().hex[:12]}",
                "user_id": user_id,
                "media_title": body.media_title,
                "artist": body.artist,
                "issue_type": issue_type,
                "problem_details": body.problem_details,
                "request_id": body.request_id,
                "album_id": body.album_id,
                "track_id": body.track_id,
                "discovery_id": body.discovery_id,
                "item_type": body.item_type,
                "status": IssueStatus.OPEN.value,
            }
        )

    notification_data = dict(created)
    notification_data["problem_details"] = str(created.get("problem_details") or "")[
        :ISSUE_NOTIFICATION_DETAILS_MAX
    ]
    if not notification_data.get("username"):
        notification_data["username"] = current_user.get("username")
    notification_data.setdefault("title", created.get("media_title"))
    if current_user.get("id"):
        notification_data["actor_user_id"] = str(current_user["id"])
    notification_dispatcher.dispatch(NotificationEvent.ISSUE_REPORTED, notification_data, db)
    _record_issue_event(db, "issue_opened", created, current_user, f"Issue opened: {issue_type}")

    return _present_issue(db, created, current_user, is_admin)


@router.get(
    "/{issue_id}", response_model=IssueResponse, response_model_exclude_unset=True, summary="Get media issue by ID"
)
def get_issue(
    issue_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Retrieves an issue by ID. Non-admins get 404 for issues they do not own."""
    issue = _visible_issue(db, issue_id, current_user)
    return _present_issue(db, issue, current_user, _is_admin(current_user))


@router.post(
    "/{issue_id}/status",
    response_model=IssueResponse,
    response_model_exclude_unset=True,
    summary="Reopen or close your own issue (admins: any transition)",
)
def set_issue_status(
    issue_id: str,
    body: StatusBody,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """The reporter's lifecycle control: reopen (resolved / wont_fix -> open) or close (-> resolved)."""
    is_admin = _is_admin(current_user)
    issue = _visible_issue(db, issue_id, current_user)
    updated = _apply_status(db, issue, body.status.value, current_user, is_admin)
    return _present_issue(db, updated, current_user, is_admin)


@router.post("/{issue_id}/seen", response_model=IssueSeen, response_model_exclude_unset=True, summary="Mark an issue as seen by its reporter")
def mark_seen(
    issue_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    issue = _visible_issue(db, issue_id, current_user)
    if str(issue["user_id"]) == str(current_user["id"]):
        db.mark_issue_seen(issue_id)
    return {"id": issue_id, "unread": False}


@router.get("/{issue_id}/comments", response_model=list[CommentResponse], response_model_exclude_unset=True)
def list_comments(
    issue_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """The discussion on an issue, oldest first. Reporter and admins only; admin action notes are admin-only."""
    is_admin = _is_admin(current_user)
    _visible_issue(db, issue_id, current_user)
    comments = db.list_issue_comments(issue_id, include_system=is_admin)
    return [_present_comment(c, current_user, is_admin) for c in comments]


@router.post(
    "/{issue_id}/comments",
    response_model=CommentResponse,
    response_model_exclude_unset=True,
    status_code=status.HTTP_201_CREATED,
)
def add_comment(
    issue_id: str,
    body: CommentBody,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Adds a comment. An admin's comment on an ``open`` issue moves it to ``in_progress``."""
    is_admin = _is_admin(current_user)
    issue = _visible_issue(db, issue_id, current_user)
    is_owner = str(issue["user_id"]) == str(current_user["id"])
    comment = db.add_issue_comment(
        issue_id,
        str(current_user["id"]),
        body.body,
        is_admin=is_admin,
        staff=is_admin and not is_owner,
        max_comments=ISSUE_COMMENT_LIMIT,
    )
    if comment is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"This issue already has {ISSUE_COMMENT_LIMIT} comments",
        )
    if is_admin and issue["status"] == IssueStatus.OPEN.value:
        db.set_issue_status(
            issue_id, IssueStatus.IN_PROGRESS.value, str(current_user["id"]), is_admin and not is_owner,
            expected_status=IssueStatus.OPEN.value,
        )
    refreshed = db.get_issue(issue_id) or issue
    _notify(
        db,
        NotificationEvent.ISSUE_UPDATED,
        refreshed,
        current_user.get("username"),
        "New comment",
        actor_user_id=str(current_user["id"]) if current_user.get("id") else None,
    )
    return _present_comment(comment, current_user, is_admin)


# --------------------------------------------------------------------------------------- core-only admin routes


@router.put(
    "/{issue_id}",
    response_model=IssueResponse,
    response_model_exclude_unset=True,
    summary="Update media issue",
)
def update_issue(
    issue_id: str,
    body: UpdateIssueBody,
    db: Database = Depends(get_db),
    _core: None = Depends(require_core_tier),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only: changes status (validated, no-ops refused) and/or problem_details; nothing else."""
    issue = db.get_issue(issue_id)
    if not issue:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media issue {issue_id} not found",
        )

    new_status = body.status.value if body.status is not None else None
    if new_status == issue["status"]:
        if body.problem_details is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Issue is already {new_status}")
        new_status = None
    if body.problem_details is not None:
        db.update_issue(issue_id, {"problem_details": body.problem_details})
    if new_status is not None:
        _apply_status(db, db.get_issue(issue_id) or issue, new_status, admin, is_admin=True)
    return _present_issue(db, db.get_issue(issue_id) or issue, admin, True)


@router.delete("/{issue_id}", response_model=IssueDeleted, response_model_exclude_unset=True, summary="Delete media issue")
def delete_issue(
    issue_id: str,
    db: Database = Depends(get_db),
    _core: None = Depends(require_core_tier),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only: deletes an issue."""
    issue = db.get_issue(issue_id)
    if not issue:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media issue {issue_id} not found",
        )

    deleted = db.delete_issue(issue_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete issue",
        )

    return {"status": "deleted", "id": issue_id}


def _queue_album_search(
    db: Database, issue: dict[str, Any], album_id: str, actor_user_id: Optional[str] = None
) -> dict[str, Any]:
    """Searches every track of the album as a replacement search (``wanted.search_tracks_for_replacement``): the
    files exist and meet the cutoff, so the Wanted page's search would skip or reject them. Same quality is fine
    unless the issue is about audio quality."""
    track_ids = [str(t["id"]) for t in db.list_library_tracks(album_id=album_id, limit=1000)]
    if not track_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The album has no tracks to search for")
    return wanted_routes.search_tracks_for_replacement(
        db, track_ids, str(issue["id"]), require_better=issue["issue_type"] == IssueType.AUDIO_QUALITY.value,
        actor_user_id=actor_user_id,
    )


def _blocklist_current_release(db: Database, issue: dict[str, Any], album_id: str) -> dict[str, Any]:
    """Blocklists the release behind the album's current files; 409 when the import history does not name one."""
    track_ids = [str(t["id"]) for t in db.list_library_tracks(album_id=album_id, limit=1000)]
    source = db.get_imported_release_source(album_id, track_ids, issue.get("request_id"))
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The release that produced this album's files is not on record, so it cannot be blocklisted. "
            "Use manual import to replace the files instead.",
        )
    item = db.add_to_blocklist(
        source_title=str(source["release_title"]),
        artist=source.get("artist"),
        album=source.get("album"),
        release_guid=source.get("release_guid"),
        info_hash=source.get("info_hash"),
        protocol=source.get("protocol"),
        indexer=source.get("indexer"),
        reason=f"Wrong release reported in issue {issue['id']}",
    )
    return {"blocklist_id": item["id"], "release": item["source_title"]}


@router.post(
    "/{issue_id}/actions/{action}",
    response_model=ActionResponse,
    response_model_exclude_unset=True,
    summary="Run a fix action for an issue",
)
def run_issue_action(
    issue_id: str,
    action: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _core: None = Depends(require_core_tier),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only fix actions, each delegating to the endpoint's existing service code.

    Every action except ``rematch`` (a read) appends a system comment and moves the issue to ``in_progress``.
    """
    if action not in ACTION_LABELS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown action '{action}'")
    issue = db.get_issue(issue_id)
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Media issue {issue_id} not found")
    native = get_library_mode(db) == MODE_NATIVE
    if action not in _available_actions(db, issue, native):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{ACTION_LABELS[action]}' is not available for this issue",
        )
    album_id = _resolve_album_id(db, issue)

    result: dict[str, Any]
    if action == ACTION_RETRY_REQUEST:
        result = requests_routes.retry_request(
            str(issue["request_id"]), db=db, config=config, _admin=admin, lidarr_client=lidarr_client
        )
    elif action == ACTION_RESEARCH:
        assert album_id is not None  # guaranteed by _available_actions
        result = _queue_album_search(db, issue, album_id, str(admin["id"]))
        if not result.get("queued"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=result.get("message") or "No search was queued"
            )
    elif action == ACTION_BLOCKLIST_AND_RESEARCH:
        assert album_id is not None
        blocklisted = _blocklist_current_release(db, issue, album_id)
        result = {**blocklisted, "search": _queue_album_search(db, issue, album_id, str(admin["id"]))}
    else:  # ACTION_REMATCH: a read; hands the UI what the manual import modal needs
        assert album_id is not None
        album = db.get_library_album(album_id) or {}
        artist = db.get_library_artist(album["artist_id"]) if album.get("artist_id") else None
        tracks = manual_import_album_tracks(album_id, db=db, _admin=admin)
        result = {
            "scope": {"album_id": album_id},
            "album": {"id": album_id, "title": album.get("title"), "artist_name": (artist or {}).get("name")},
            "tracks": tracks,
        }
        return {"action": action, "result": result, "issue": _present_issue(db, issue, admin, True, native)}

    db.add_issue_comment(
        issue_id, str(admin["id"]), f"Admin ran: {ACTION_LABELS[action]}", is_admin=True, is_system=True, staff=True
    )
    if issue["status"] != IssueStatus.IN_PROGRESS.value:
        db.set_issue_status(
            issue_id, IssueStatus.IN_PROGRESS.value, str(admin["id"]), True, expected_status=issue["status"]
        )
    updated = db.get_issue(issue_id) or issue
    _notify(
        db,
        NotificationEvent.ISSUE_UPDATED,
        updated,
        admin.get("username"),
        f"Ran {ACTION_LABELS[action]}",
        actor_user_id=str(admin["id"]) if admin.get("id") else None,
    )
    return {"action": action, "result": result, "issue": _present_issue(db, updated, admin, True, native)}
