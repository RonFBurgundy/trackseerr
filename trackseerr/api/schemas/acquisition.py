"""Response models for ``/api/acquisition`` (``api/routes/acquisition.py``). Admin only (core tier for most).

``/pending`` keeps the router's existing ``PendingReleaseResponse``.
"""

from typing import Any, Optional

from trackseerr.api.response_models import ApiModel


class SearchQueryEcho(ApiModel):
    artist: str
    title: Optional[str] = None
    album: Optional[str] = None
    item_type: str = "track"
    quality_profile_id: Optional[str] = None
    album_id: Optional[str] = None
    track_id: Optional[str] = None


class SearchRelease(ApiModel):
    """A candidate release with its quality evaluation."""

    id: str
    title: str
    indexer_name: str
    protocol: str
    size_bytes: int
    seeders: Optional[int] = None
    publish_date: Optional[str] = None
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    parsed_quality: str
    source: Optional[str] = None
    tags: list[str] = []
    is_acceptable: bool
    score: int
    meets_cutoff: bool
    rejection_reasons: list[str] = []
    format_score: int = 0
    # Free-form: ``DecisionBreakdown.to_dict()``; the engine adds keys as rules evolve.
    breakdown: Optional[dict[str, Any]] = None
    # Free-form: whatever extra metadata each indexer driver attaches to a result (guid, files, publish date...).
    extra: dict[str, Any] = {}


class SearchResponse(ApiModel):
    query: SearchQueryEcho
    profile_name: str
    results: list[SearchRelease]
    count: int


class GrabResponse(ApiModel):
    success: bool
    download_id: str
    client: str
    message: str


class PendingDeletedResponse(ApiModel):
    status: str
    id: int


class PendingGrabResponse(ApiModel):
    success: bool
    id: int
    download_id: Optional[str] = None
    release: Optional[str] = None


class BlocklistEntry(ApiModel):
    id: str
    source_title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    release_guid: Optional[str] = None
    info_hash: Optional[str] = None
    protocol: Optional[str] = None
    indexer: Optional[str] = None
    reason: Optional[str] = None
    created_at: str


class BlocklistRemovedResponse(ApiModel):
    success: bool
    message: str
