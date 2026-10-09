"""Response models for ``/api/sync`` (``api/routes/sync.py``). ``/stream`` (SSE) is not modelled."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class SyncTriggerResponse(ApiModel):
    status: str
    message: str


class SyncRunStats(ApiModel):
    total_playlists: int
    success_count: int
    total_matched: int
    total_missing: int


class SyncStatusResponse(ApiModel):
    is_syncing: bool
    last_run_at: Optional[str] = None
    last_run_stats: SyncRunStats


class SyncWebhookResponse(SyncTriggerResponse):
    fulfilled_requests: int
