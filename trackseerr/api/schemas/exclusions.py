"""Response and request models for ``/api/exclusions`` (``api/routes/exclusions.py``)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from trackseerr.api.response_models import ApiModel


class ExclusionItem(ApiModel):
    """An exclusion record."""

    id: str
    scope: str
    mbid: str | None = None
    artist_name: str
    title: str | None = None
    reason: str | None = None
    created_by: str | None = None
    created_at: str | None = None


class ExclusionPage(ApiModel):
    """Paginated list of exclusion records."""

    items: list[ExclusionItem]
    total: int
    page: int
    page_size: int


class ExclusionCreate(BaseModel):
    """Request model for creating an exclusion.

    Either names (artist_name and title for album/track) or exactly one source ref must be given.
    """

    scope: Literal["artist", "album", "track"] | None = None
    artist_name: str | None = None
    title: str | None = None
    mbid: str | None = None
    reason: str | None = Field(default=None, max_length=500)
    library_artist_id: int | None = None
    library_album_id: int | None = None
    library_track_id: int | None = None
    missing_track_id: int | None = None

    @model_validator(mode="after")
    def validate_names_or_ref(self) -> "ExclusionCreate":
        refs = [
            self.library_artist_id is not None,
            self.library_album_id is not None,
            self.library_track_id is not None,
            self.missing_track_id is not None,
        ]
        num_refs = sum(refs)
        has_artist = bool(self.artist_name and self.artist_name.strip())
        has_title = bool(self.title and self.title.strip())

        if num_refs > 1:
            raise ValueError("At most one source ref may be provided")
        if num_refs == 1 and (has_artist or has_title):
            raise ValueError("Either names or exactly one source ref must be given, not both")
        if num_refs == 0:
            if not has_artist:
                raise ValueError("Either artist_name or exactly one source ref must be given")
            if not self.scope:
                raise ValueError("scope is required when creating an exclusion by name")
        return self


class ExclusionCreated(ApiModel):
    """Response returned when an exclusion is created."""

    item: ExclusionItem
    unmonitored: int


class ExclusionCheckResponse(ApiModel):
    """Response returned by the exclusion pre-flight check endpoint."""

    excluded: bool
    reason: str | None = None
