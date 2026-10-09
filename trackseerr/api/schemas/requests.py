"""Response models for ``/api/requests`` (``api/routes/requests.py``).

``RequestRecord`` is a ``music_requests`` row with the requester's username joined (``Database.get_request`` /
``list_requests``). ``list_requests`` omits the trigger columns, so those are optional and the routes use
``response_model_exclude_unset=True`` to keep them absent there.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class RequestRecord(ApiModel):
    id: str
    user_id: str
    item_type: str
    title: str
    artist: str
    album: Optional[str] = None
    cover_url: Optional[str] = None
    preview_url: Optional[str] = None
    status: str
    release_date: Optional[str] = None
    foreign_id: Optional[str] = None
    quality_profile_id: Optional[str] = None
    current_quality: Optional[str] = None
    cutoff_met: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    batch_id: Optional[str] = None
    batch_kind: Optional[str] = None
    status_reason: Optional[str] = None
    status_message: Optional[str] = None
    next_attempt_at: Optional[str] = None  # UTC 'YYYY-MM-DD HH:MM:SS' of the next automatic Lidarr retry
    trigger: Optional[str] = None
    trigger_ref: Optional[str] = None
    trigger_label: Optional[str] = None
    username: Optional[str] = None  # null when the requesting user row is gone


class RequestListResponse(ApiModel):
    requests: list[RequestRecord]
    count: int


class BatchCreatedResponse(ApiModel):
    created: list[RequestRecord]
    count: int


class RequestDeleted(ApiModel):
    status: str
    id: str


class RetryResult(ApiModel):
    success: bool
    status: str
    message: str
    download_id: Optional[str] = None
