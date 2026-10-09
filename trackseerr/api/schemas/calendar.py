"""Schemas for release calendar endpoints."""

from __future__ import annotations

from typing import Literal, Optional
from trackseerr.api.response_models import ApiModel

CalendarStatus = Literal["downloaded", "partial", "missing", "upcoming"]


class CalendarItem(ApiModel):
    """A release calendar item representing an album."""

    id: str
    artist_id: str
    artist_name: str
    title: str
    album_type: Optional[str] = "album"
    release_date: str
    monitored: bool
    status: CalendarStatus
    cover_url: Optional[str] = None
