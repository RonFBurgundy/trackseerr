"""Request and response models for library routes."""

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


from trackseerr.api.response_models import ApiModel
from trackseerr.library_monitoring import (
    NATIVE_MONITOR_OPTIONS,
    accept_deprecated_profile_keys,
)


logger = logging.getLogger(__name__)

MONITOR_OPTION_PATTERN = "^(" + "|".join(NATIVE_MONITOR_OPTIONS) + ")$"

class IngestArtistRequest(BaseModel):
    foreign_artist_id: str  # e.g. "deezer:artist:13" or "itunes:artist:..."
    artist_name: str
    quality_profile_id: Optional[str] = None
    monitor_option: Optional[str] = Field(default=None, pattern=MONITOR_OPTION_PATTERN)
    monitored: bool = True
    root_folder: Optional[str] = None
    # Native only. Omitted: the saved ``add_metadata_profile_id`` default (NULL = no profile); explicit null = none.
    metadata_profile_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def _deprecated_profile_keys(cls, data: Any) -> Any:
        return accept_deprecated_profile_keys(data)

class ArtistMonitoredRequest(BaseModel):
    monitored: bool
    cascade_children: bool = True
    monitor_option: Optional[str] = Field(default=None, pattern=MONITOR_OPTION_PATTERN)
    # Native only. Present (even null = clear) sets the artist's metadata profile; ``apply_monitor_to_albums`` then
    # recomputes the albums like an option change (omitted: no recompute for a profile-only change).
    metadata_profile_id: Optional[int] = None
    apply_monitor_to_albums: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def _deprecated_profile_keys(cls, data: Any) -> Any:
        return accept_deprecated_profile_keys(data)

class MetadataProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    primary_types: list[str]
    secondary_types: list[str]

class AlbumMonitoredRequest(BaseModel):
    monitored: bool
    cascade_tracks: bool = True

class ArtistBulkEditRequest(BaseModel):
    artist_ids: Optional[list[str]] = None
    all: bool = False
    monitored: Optional[bool] = None
    monitor_option: Optional[str] = Field(default=None, pattern=MONITOR_OPTION_PATTERN)
    quality_profile_id: Optional[str] = None  # an explicit null clears the profile; omitted leaves it alone
    metadata_profile_id: Optional[int] = None  # native only; an explicit null clears it, omitted leaves it alone
    # Omitted: an unmonitor (monitored=false) cascades to albums (and native tracks) by default, so artists never end
    # up unmonitored with monitored children; an explicit value always wins.
    apply_monitor_to_albums: Optional[bool] = None
    # Native only: tag ids to add to / remove from every selected artist (no overlap allowed).
    add_tags: list[int] = Field(default_factory=list)
    remove_tags: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _deprecated_profile_keys(cls, data: Any) -> Any:
        return accept_deprecated_profile_keys(data)

class ArtistTagsRequest(BaseModel):
    tags: list[int] = Field(default_factory=list, description="Tag ids; replaces the artist's tags")

class ArtistTagsResponse(ApiModel):
    artist_id: str
    tags: list[int]

class AlbumBulkEditRequest(BaseModel):
    album_ids: list[str]
    monitored: bool

class TrackMonitoredRequest(BaseModel):
    monitored: bool

class TrackBulkEditRequest(BaseModel):
    track_ids: list[str]
    monitored: bool

class ScanRequest(BaseModel):
    root_folder: Optional[str] = None
    prune_missing: bool = False

class MigrateLidarrRequest(BaseModel):
    auto_switch_mode: bool = True

class ManualImportScanRequest(BaseModel):
    folder_path: Optional[str] = None
    download_id: Optional[str] = None
    album_id: Optional[str] = None
    file_paths: Optional[list[str]] = None

class ManualImportItem(BaseModel):
    source_path: Optional[str] = None
    file_path: Optional[str] = None
    artist_name: Optional[str] = None
    artist_id: Optional[str] = None
    album_title: Optional[str] = None
    album_id: Optional[str] = None
    track_title: Optional[str] = None
    track_id: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = 1
    year: Optional[int] = None
    mode: Optional[Literal["move", "hardlink", "copy"]] = None
    write_tags: Optional[bool] = None

class ManualImportCommitRequest(BaseModel):
    items: list[ManualImportItem] = Field(default_factory=list)
    download_id: Optional[str] = None
    issue_id: Optional[str] = None  # an issue's "Rematch files" flow: replaced files are reported on that issue

class RenamePreviewRequest(BaseModel):
    artist_id: Optional[str] = None
    album_id: Optional[str] = None
    limit: int = 200

class RenameApplyRequest(BaseModel):
    file_ids: list[str] = Field(default_factory=list)

class RetagPreviewRequest(BaseModel):
    artist_id: Optional[str] = None
    album_id: Optional[str] = None
    limit: int = Field(default=200, le=200, ge=1)
    offset: int = Field(default=0, ge=0)

class RetagApplyRequest(BaseModel):
    file_ids: list[str] = Field(default_factory=list, max_length=500)
    embed_art: bool = False

class FingerprintRequest(BaseModel):
    file_path: str

class CreateCollectionRequest(BaseModel):
    name: str
    summary: Optional[str] = None
    poster_url: Optional[str] = None
    monitored: bool = True

class AddAlbumToCollectionRequest(BaseModel):
    album_id: str
    order_index: int = 0

