"""Response models for ``/api/seed-cleanup`` (``api/routes/seed_cleanup.py``)."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class SeedCleanupStarted(ApiModel):
    started: bool


class SeedCleanupStats(ApiModel):
    evaluated: int = 0
    removed: int = 0
    deleted_files: int = 0
    orphans: int = 0
    failures: int = 0


class SeedCleanupLastRun(ApiModel):
    started_at: str
    finished_at: str
    stats: SeedCleanupStats
    error: Optional[str] = None


class SeedCleanupStatus(ApiModel):
    running: bool
    last_run: Optional[SeedCleanupLastRun] = None


class RemoveOrphanResponse(ApiModel):
    removed: bool
    delete_files: bool


class RetryFailedResponse(ApiModel):
    retried: bool
    removed: bool
    status: Optional[str] = None
    attempts: int
    error: Optional[str] = None
