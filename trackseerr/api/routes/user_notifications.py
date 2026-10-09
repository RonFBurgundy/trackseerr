"""REST API endpoints for per-user notifications, in-app inbox, Web Push, and personal channels."""

import logging
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from trackseerr.api.dependencies import get_current_user_or_api_key, get_db
from trackseerr.api.response_models import ApiModel
from trackseerr.api.routes.notifications import (
    _URL_KEYS,
    _mask_channel_dict,
    _masked,
    _merge_masked_config,
    _reject_placeholders,
)
from trackseerr.api.schemas.notifications import DeletedResponse
from trackseerr.models import NotificationChannel, NotificationChannelType
from trackseerr.notifications import USER_FACING_EVENTS, notification_dispatcher
from trackseerr.security import is_safe_service_url
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

_NON_ACCOUNT_IDS = frozenset({"api_key_user", "feed_token_user", "gateway_service"})
ALLOWED_USER_CHANNEL_TYPES = frozenset({"discord", "telegram", "pushover", "webhook"})


def require_session_user(
    current_user: dict[str, Any] = Depends(get_current_user_or_api_key),
) -> dict[str, Any]:
    """Requires an interactive user session. API-key pseudo users get 403."""
    uid = str(current_user.get("id"))
    if uid in _NON_ACCOUNT_IDS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account notification endpoints require an interactive user session",
        )
    return current_user


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

class UserNotificationItem(ApiModel):
    id: str
    user_id: str
    event: str
    title: str
    message: str
    link: Optional[str] = None
    created_at: str
    read_at: Optional[str] = None


class NotificationInboxResponse(ApiModel):
    page: int
    page_size: int
    total: int
    unread_count: int
    items: list[UserNotificationItem]


class UnreadCountResponse(ApiModel):
    unread_count: int


class MarkAllReadResponse(ApiModel):
    status: str
    marked_read: int


class NotificationActionResponse(ApiModel):
    status: str
    id: Optional[str] = None


class NotificationPrefItem(ApiModel):
    event: str
    in_app: bool
    push: bool


class NotificationPrefUpdatePayload(BaseModel):
    in_app: bool
    push: bool


class NotificationPrefBulkUpdatePayload(BaseModel):
    prefs: list[NotificationPrefItem]


class PushConfigResponse(ApiModel):
    public_key: str
    enabled: bool = True


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(..., min_length=1, max_length=500)
    auth: str = Field(..., min_length=1, max_length=500)


class PushSubscribePayload(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=1000)
    keys: PushSubscriptionKeys
    user_agent: Optional[str] = Field(None, max_length=500)


class PushSubscriptionResponse(ApiModel):
    id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    user_agent: Optional[str] = None
    created_at: str
    last_success_at: Optional[str] = None
    failure_count: int = 0


class PushUnsubscribePayload(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=1000)


class UserNotificationChannelItem(ApiModel):
    id: str
    name: str
    channel_type: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    owner_user_id: Optional[str] = None


class UserNotificationChannelPayload(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    channel_type: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    events: Optional[list[str]] = None


class UserTestNotificationPayload(BaseModel):
    channel_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    channel_id: Optional[str] = None


class TestNotificationResponse(ApiModel):
    success: bool
    message: str


# -----------------------------------------------------------------------------
# Inbox Endpoints
# -----------------------------------------------------------------------------

@router.get(
    "/inbox",
    response_model=NotificationInboxResponse,
    summary="List user notifications",
)
def list_inbox_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> NotificationInboxResponse:
    """Lists paged notifications for current user, newest first."""
    items, total, unread = db.list_user_notifications(
        current_user["id"], page=page, page_size=page_size
    )
    return NotificationInboxResponse(
        page=page,
        page_size=page_size,
        total=total,
        unread_count=unread,
        items=[UserNotificationItem(**it) for it in items],
    )


@router.get(
    "/inbox/unread-count",
    response_model=UnreadCountResponse,
    summary="Get unread notification count",
)
def get_unread_count(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> UnreadCountResponse:
    """Returns the unread notifications count for the current user."""
    count = db.get_user_notification_unread_count(current_user["id"])
    return UnreadCountResponse(unread_count=count)


@router.post(
    "/inbox/{notification_id}/read",
    response_model=UserNotificationItem,
    summary="Mark notification as read",
)
def mark_notification_read(
    notification_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> UserNotificationItem:
    """Marks a single notification as read if owned by the current user."""
    row = db.mark_user_notification_read(notification_id, current_user["id"])
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    return UserNotificationItem(**row)


@router.post(
    "/inbox/read-all",
    response_model=MarkAllReadResponse,
    summary="Mark all notifications as read",
)
def mark_all_read(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> MarkAllReadResponse:
    """Marks all notifications for the current user as read."""
    count = db.mark_all_user_notifications_read(current_user["id"])
    return MarkAllReadResponse(status="ok", marked_read=count)


@router.delete(
    "/inbox/{notification_id}",
    response_model=NotificationActionResponse,
    summary="Delete a notification",
)
def delete_notification(
    notification_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> NotificationActionResponse:
    """Deletes a single notification owned by current user."""
    deleted = db.delete_user_notification(notification_id, current_user["id"])
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    return NotificationActionResponse(status="deleted", id=notification_id)


# -----------------------------------------------------------------------------
# Preferences Endpoints
# -----------------------------------------------------------------------------

@router.get(
    "/prefs",
    response_model=list[NotificationPrefItem],
    summary="Get user notification preferences",
)
def get_user_prefs(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> list[NotificationPrefItem]:
    """Returns notification delivery preferences for all user-facing events."""
    stored = db.get_all_user_notification_prefs(current_user["id"])
    res: list[NotificationPrefItem] = []
    for ev in sorted(USER_FACING_EVENTS):
        pref = stored.get(ev, {"in_app": True, "push": True})
        res.append(NotificationPrefItem(event=ev, in_app=pref["in_app"], push=pref["push"]))
    return res


@router.get(
    "/prefs/{event}",
    response_model=NotificationPrefItem,
    summary="Get user notification preference for event",
)
def get_user_pref_for_event(
    event: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> NotificationPrefItem:
    """Returns notification preference for a specific user-facing event."""
    if event not in USER_FACING_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user event '{event}'. Supported: {', '.join(sorted(USER_FACING_EVENTS))}",
        )
    in_app, push = db.get_user_notification_pref(current_user["id"], event)
    return NotificationPrefItem(event=event, in_app=in_app, push=push)


@router.put(
    "/prefs/{event}",
    response_model=NotificationPrefItem,
    summary="Update user notification preference for event",
)
def update_user_pref_for_event(
    event: str,
    payload: NotificationPrefUpdatePayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> NotificationPrefItem:
    """Updates notification preference for a specific user-facing event."""
    if event not in USER_FACING_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user event '{event}'. Supported: {', '.join(sorted(USER_FACING_EVENTS))}",
        )
    db.set_user_notification_pref(current_user["id"], event, payload.in_app, payload.push)
    return NotificationPrefItem(event=event, in_app=payload.in_app, push=payload.push)


@router.put(
    "/prefs",
    response_model=list[NotificationPrefItem],
    summary="Bulk update user notification preferences",
)
def update_user_prefs_bulk(
    payload: NotificationPrefBulkUpdatePayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> list[NotificationPrefItem]:
    """Updates multiple notification preferences at once."""
    for item in payload.prefs:
        if item.event in USER_FACING_EVENTS:
            db.set_user_notification_pref(current_user["id"], item.event, item.in_app, item.push)
    return get_user_prefs(db, current_user)


# -----------------------------------------------------------------------------
# Web Push Endpoints
# -----------------------------------------------------------------------------

@router.get(
    "/push",
    response_model=PushConfigResponse,
    summary="Get Web Push configuration",
)
def get_push_config(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> PushConfigResponse:
    """Returns server VAPID public key and enabled status (private key never exposed)."""
    pub_key, _, _ = db.get_or_create_vapid_keys()
    return PushConfigResponse(public_key=pub_key, enabled=True)


@router.post(
    "/push/subscribe",
    response_model=PushSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe to Web Push",
)
def subscribe_push(
    payload: PushSubscribePayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> PushSubscriptionResponse:
    """Registers a browser Web Push subscription (max 10 per user)."""
    endpoint = payload.endpoint.strip()
    if not endpoint.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Push subscription endpoint must use HTTPS",
        )
    if len(endpoint) > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Push subscription endpoint exceeds length limit",
        )

    # Check max 10 subscriptions per user
    existing_subs = db.get_user_web_push_subscriptions(current_user["id"])
    already_subscribed = any(s["endpoint"] == endpoint for s in existing_subs)
    if not already_subscribed and len(existing_subs) >= 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum number of push subscriptions (10) reached for this account",
        )

    sub = db.create_or_update_web_push_subscription(
        user_id=current_user["id"],
        endpoint=endpoint,
        p256dh=payload.keys.p256dh.strip(),
        auth=payload.keys.auth.strip(),
        user_agent=payload.user_agent.strip() if payload.user_agent else None,
    )
    return PushSubscriptionResponse(**sub)


@router.delete(
    "/push/subscription",
    response_model=NotificationActionResponse,
    summary="Unsubscribe from Web Push",
)
def unsubscribe_push(
    endpoint: Optional[str] = Query(None),
    payload: Optional[PushUnsubscribePayload] = None,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> NotificationActionResponse:
    """Unsubscribes an endpoint from Web Push."""
    target_endpoint = endpoint or (payload.endpoint if payload else None)
    if not target_endpoint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Endpoint is required to unsubscribe",
        )
    db.delete_web_push_subscription(target_endpoint.strip(), user_id=current_user["id"])
    return NotificationActionResponse(status="deleted")


@router.post(
    "/push/test",
    response_model=TestNotificationResponse,
    summary="Send test Web Push to caller",
)
def test_push_notification(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> TestNotificationResponse:
    """Sends a synthetic Web Push test notification strictly to the caller's devices."""
    ok, msg = notification_dispatcher.send_push_test(db, current_user["id"])
    return TestNotificationResponse(success=ok, message=msg)


# -----------------------------------------------------------------------------
# Own Notification Channels (CRUD + Test)
# -----------------------------------------------------------------------------

@router.get(
    "/channels",
    response_model=list[UserNotificationChannelItem],
    summary="List user notification channels",
)
def list_user_channels(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> list[dict[str, Any]]:
    """Lists notification channels owned by current user with masked credentials."""
    channels = db.list_notification_channels(owner_user_id=current_user["id"])
    return [_mask_channel_dict(c) for c in channels]


@router.post(
    "/channels",
    response_model=UserNotificationChannelItem,
    status_code=status.HTTP_201_CREATED,
    summary="Create user notification channel",
)
def create_user_channel(
    payload: UserNotificationChannelPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> dict[str, Any]:
    """Creates a user notification channel (max 5 per user)."""
    # Enforce channel limit
    count = db.count_user_notification_channels(current_user["id"])
    if count >= 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum number of notification channels (5) reached for this account",
        )

    clean_type = payload.channel_type.strip().lower()
    if clean_type not in ALLOWED_USER_CHANNEL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid channel type '{payload.channel_type}'. Supported: {', '.join(sorted(ALLOWED_USER_CHANNEL_TYPES))}",
        )

    cfg = dict(payload.config)
    is_admin = bool(current_user.get("is_admin"))
    if "webhook_url" in cfg and cfg["webhook_url"]:
        clean_url = str(cfg["webhook_url"]).strip()
        if not is_safe_service_url(clean_url, allow_lan=is_admin):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prohibited or invalid webhook URL (SSRF protection)",
            )

    # Validate events against USER_FACING_EVENTS
    if payload.events is not None:
        for ev in payload.events:
            if ev not in USER_FACING_EVENTS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid notification event '{ev}'. Supported: {', '.join(sorted(USER_FACING_EVENTS))}",
                )
        events = payload.events
    else:
        events = sorted(USER_FACING_EVENTS)

    channel_id = (
        payload.id.strip()
        if payload.id and payload.id.strip()
        else f"uchan-{uuid.uuid4().hex[:12]}"
    )

    channel = NotificationChannel(
        id=channel_id,
        name=payload.name.strip(),
        channel_type=clean_type,
        enabled=payload.enabled,
        config=cfg,
        events=events,
        owner_user_id=current_user["id"],
    )

    saved = db.create_notification_channel(channel)
    return _mask_channel_dict(saved)


@router.get(
    "/channels/{channel_id}",
    response_model=UserNotificationChannelItem,
    summary="Get user notification channel",
)
def get_user_channel(
    channel_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> dict[str, Any]:
    """Retrieves an owned notification channel by ID."""
    existing = db.get_notification_channel(channel_id)
    if not existing or existing.get("owner_user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification channel '{channel_id}' not found",
        )
    return _mask_channel_dict(existing)


@router.put(
    "/channels/{channel_id}",
    response_model=UserNotificationChannelItem,
    summary="Update user notification channel",
)
def update_user_channel(
    channel_id: str,
    payload: UserNotificationChannelPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> dict[str, Any]:
    """Updates an owned notification channel, preserving masked credentials."""
    existing = db.get_notification_channel(channel_id)
    if not existing or existing.get("owner_user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification channel '{channel_id}' not found",
        )

    clean_type = payload.channel_type.strip().lower()
    if clean_type not in ALLOWED_USER_CHANNEL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid channel type '{payload.channel_type}'. Supported: {', '.join(sorted(ALLOWED_USER_CHANNEL_TYPES))}",
        )

    existing_cfg = existing.get("config") or {}
    new_cfg = _merge_masked_config(payload.config, existing_cfg, existing.get("channel_type", ""))

    is_admin = bool(current_user.get("is_admin"))
    if "webhook_url" in new_cfg and new_cfg["webhook_url"]:
        clean_url = str(new_cfg["webhook_url"]).strip()
        if not is_safe_service_url(clean_url, allow_lan=is_admin):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prohibited or invalid webhook URL (SSRF protection)",
            )

    if payload.events is not None:
        for ev in payload.events:
            if ev not in USER_FACING_EVENTS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid notification event '{ev}'. Supported: {', '.join(sorted(USER_FACING_EVENTS))}",
                )
        events = payload.events
    else:
        events = existing.get("events") or sorted(USER_FACING_EVENTS)

    updated = db.update_notification_channel(
        channel_id,
        {
            "name": payload.name.strip(),
            "channel_type": clean_type,
            "enabled": payload.enabled,
            "config": new_cfg,
            "events": events,
            "owner_user_id": current_user["id"],
        },
    )
    return _mask_channel_dict(updated)


@router.delete(
    "/channels/{channel_id}",
    response_model=DeletedResponse,
    response_model_exclude_unset=True,
    summary="Delete user notification channel",
)
def delete_user_channel(
    channel_id: str,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> dict[str, Any]:
    """Deletes an owned notification channel."""
    existing = db.get_notification_channel(channel_id)
    if not existing or existing.get("owner_user_id") != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification channel '{channel_id}' not found",
        )
    db.delete_notification_channel(channel_id)
    return {"status": "deleted", "id": channel_id}


@router.post(
    "/channels/test",
    response_model=TestNotificationResponse,
    summary="Test user notification channel",
)
def test_user_channel(
    payload: UserTestNotificationPayload,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_session_user),
) -> TestNotificationResponse:
    """Tests a user channel configuration with SSRF enforcement (allow_lan=False for non-admins)."""
    clean_type = payload.channel_type.strip().lower()
    if clean_type not in ALLOWED_USER_CHANNEL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid channel type '{payload.channel_type}'. Supported: {', '.join(sorted(ALLOWED_USER_CHANNEL_TYPES))}",
        )

    test_cfg = dict(payload.config)
    is_admin = bool(current_user.get("is_admin"))

    if payload.channel_id:
        existing = db.get_notification_channel(payload.channel_id)
        if not existing or existing.get("owner_user_id") != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Notification channel '{payload.channel_id}' not found",
            )
        existing_cfg = existing.get("config") or {}
        same_destination = all(
            test_cfg.get(k) in (None, existing_cfg.get(k)) or test_cfg.get(k) == _masked(existing, k)
            for k in _URL_KEYS
        )
        if same_destination:
            test_cfg = _merge_masked_config(test_cfg, existing_cfg, existing.get("channel_type", ""))
        else:
            _reject_placeholders(test_cfg)

    if "webhook_url" in test_cfg and test_cfg["webhook_url"]:
        clean_url = str(test_cfg["webhook_url"]).strip()
        if not is_safe_service_url(clean_url, allow_lan=is_admin):
            return TestNotificationResponse(
                success=False,
                message="Prohibited or invalid webhook URL (SSRF protection)",
            )

    ok, msg = notification_dispatcher.test_channel(
        clean_type,
        test_cfg,
        allow_lan=is_admin,
        owner_user_id=current_user["id"],
    )
    return TestNotificationResponse(success=ok, message=msg)
