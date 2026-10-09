"""Response models for ``/api/mixes`` (``api/routes/mixes.py``). The 204 delete route has no body and stays untyped."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class MixConfig(ApiModel):
    """A tailored mix config (``PUBLIC_FIELDS`` of the row; the stored result blob is served by ``/result`` only)."""

    id: str
    user_id: str
    mix_type: str
    name: str
    seed_artist: Optional[str] = None
    track_count: int
    discovery_ratio: float
    seed_window_days: int
    excluded_genres: list[str]
    auto_acquire_missing: bool
    max_weekly_acquisitions: int
    quality_profile_id: Optional[str] = None
    enabled: bool
    last_generated_at: Optional[str] = None


class MixPreviewTrack(ApiModel):
    artist: str
    title: str
    album: Optional[str] = None
    origin: str


class MixPreviewResponse(ApiModel):
    tracks: list[MixPreviewTrack]


class MixGenerateResponse(ApiModel):
    status: str


class MixResultTrack(ApiModel):
    artist: str
    title: str
    album: Optional[str] = None
    origin: str
    status: str


class MixResult(ApiModel):
    """``TailoredMixResult.to_dict()`` as persisted after the last generation."""

    mix_id: str
    generated_at: str
    total: int = 0
    available: int = 0
    missing: int = 0
    acquisitions_queued: int = 0
    quota_remaining: int = 0
    synced: bool = False
    sync_error: Optional[str] = None
    tracks: list[MixResultTrack] = []
