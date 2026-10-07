"""Response models for the routes of ``api/routes/issues.py`` that had no model.

The issue, comment and action models already live in the router with per-role whitelisting and ``exclude_unset``.
"""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class IssueCount(ApiModel):
    """``unread-count`` (count) and ``open-count`` (count plus ``in_progress``, admin only)."""

    count: int
    in_progress: Optional[int] = None


class IssueSeen(ApiModel):
    id: str
    unread: bool


class IssueDeleted(ApiModel):
    status: str
    id: str
