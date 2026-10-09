"""Response models for changelog API routes."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class ChangelogSection(ApiModel):
    title: str
    items: list[str]


class ChangelogRelease(ApiModel):
    version: str
    date_note: Optional[str] = None
    unreleased: bool
    sections: list[ChangelogSection]


class ChangelogResponse(ApiModel):
    version: str
    commit: Optional[str] = None
    releases: list[ChangelogRelease]
    latest: Optional[ChangelogRelease] = None


class ChangelogUnseenResponse(ApiModel):
    show: bool
    release: Optional[ChangelogRelease] = None


class ChangelogSeenResponse(ApiModel):
    success: bool = True
