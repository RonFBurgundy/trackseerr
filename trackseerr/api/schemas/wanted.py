"""Response models for ``/api/wanted`` (``api/routes/wanted.py``), built by ``activity_service``."""

from typing import Optional

from trackseerr.api.response_models import ApiModel
from trackseerr.api.schemas.activity import HistoryIndexResponse as WantedIndexResponse  # same shape
from trackseerr.api.schemas.activity import PagedBase


class WantedRecord(ApiModel):
    id: str
    source: str
    artist: Optional[str] = None
    album: Optional[str] = None
    album_id: Optional[str] = None
    title: Optional[str] = None
    item_type: str
    release_date: Optional[str] = None
    monitored: bool
    last_searched_at: Optional[str] = None
    # Cutoff list only: the key is absent from the missing list.
    current_quality: Optional[str] = None
    cutoff_quality: Optional[str] = None


class WantedPage(PagedBase):
    records: list[WantedRecord]


class WantedSearchResponse(ApiModel):
    queued: int
    message: Optional[str] = None  # only when nothing or fewer than requested ran


__all__ = ["WantedIndexResponse", "WantedPage", "WantedRecord", "WantedSearchResponse"]
