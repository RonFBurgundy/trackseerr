"""Response models for ``/api/recycle-bin`` (``api/routes/recycle_bin.py``)."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class RecycleBinEmptyResponse(ApiModel):
    removed: int
    errors: list[str]
    skipped_reason: str


class RecycleBinLastRun(ApiModel):
    finished_at: str
    removed: int
    errors: int
    emptied: bool


class RecycleBinStatus(ApiModel):
    running: bool
    last_run: Optional[RecycleBinLastRun] = None
