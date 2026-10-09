"""Response models for ``/api/library`` (``api/routes/library.py``).

Every route serves either the native catalog (SQLite rows) or Lidarr's live library mapped by ``lidarr_library``; the
two shapes overlap but are not equal, so the record models are the union and almost everything is optional. The routes
declare ``response_model_exclude_unset=True``, so a key a given mode never produces stays absent from the JSON exactly
as before (it is not turned into ``null``).
"""

from typing import Any, Literal, Optional

from pydantic import Field

from plex_playlist_sync.api.response_models import ApiModel

Number = int | float


# ----------------------------------------------------------------------------------------------- catalog records


class LibraryFileRecord(ApiModel):
    """A track's file. Native: a ``library_files`` row. Lidarr: ``{id, file_path, size_bytes, quality}``."""

    id: str
    track_id: Optional[str] = None
    file_path: Optional[str] = None
    relative_path: Optional[str] = None
    codec: Optional[str] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    bits_per_sample: Optional[int] = None
    quality_name: Optional[str] = None
    size_bytes: Optional[int] = None
    cutoff_met: Optional[bool] = None
    date_added: Optional[str] = None
    updated_at: Optional[str] = None
    quality: Optional[str] = None  # Lidarr mode only: the quality name of the track file


class LibraryTrackRecord(ApiModel):
    """A library track (native row plus the list/detail enrichments, or Lidarr's mapped track)."""

    id: str
    album_id: Optional[str] = None
    artist_id: Optional[str] = None
    title: Optional[str] = None
    clean_title: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    duration_seconds: Optional[float] = None
    monitored: Optional[bool] = None  # Lidarr tracks carry no flag: null
    foreign_track_id: Optional[str] = None
    mb_recording_id: Optional[str] = None
    isrc: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_searched_at: Optional[str] = None
    sort_title: Optional[str] = None
    artist_name: Optional[str] = None
    album_title: Optional[str] = None
    file: Optional[LibraryFileRecord] = None
    has_file: Optional[bool] = None  # Lidarr mode only
    source: Optional[str] = None  # "lidarr" in Lidarr mode


class LibraryAlbumRecord(ApiModel):
    """A library album (native row plus enrichments, or Lidarr's mapped album)."""

    id: str
    artist_id: Optional[str] = None
    title: Optional[str] = None
    clean_title: Optional[str] = None
    foreign_album_id: Optional[str] = None
    release_date: Optional[str] = None
    year: Optional[int] = None
    album_type: Optional[str] = None
    monitored: Optional[bool] = None
    path: Optional[str] = None
    cover_url: Optional[str] = None
    total_tracks: Optional[int] = None
    mb_release_group_id: Optional[str] = None
    mb_release_id: Optional[str] = None
    genres: Optional[str] = None
    sort_title: Optional[str] = None
    secondary_types: Optional[list[str]] = None
    art_version: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    artist_name: Optional[str] = None
    track_count: Optional[int] = None
    track_file_count: Optional[int] = None
    in_profile: Optional[bool] = None  # artist detail: inside the artist's metadata profile (informational)
    order_index: Optional[int] = None  # collection detail only
    tracks: Optional[list[LibraryTrackRecord]] = None  # album detail only
    mbid: Optional[str] = None  # Lidarr mode
    added_at: Optional[str] = None  # Lidarr mode
    size_bytes: Optional[int] = None  # Lidarr mode
    source: Optional[str] = None  # "lidarr" in Lidarr mode


class LibraryArtistFields(ApiModel):
    """Every artist key either mode produces (``name`` is required on ``LibraryArtistRecord``)."""

    id: str
    name: Optional[str] = None
    clean_name: Optional[str] = None
    foreign_artist_id: Optional[str] = None
    path: Optional[str] = None
    monitored: Optional[bool] = None
    monitor_option: Optional[str] = None  # None: Lidarr reported no native-equivalent preset
    quality_profile_id: Optional[str] = None
    metadata_profile_id: Optional[int] = None
    metadata_json: Optional[str] = None
    mbid: Optional[str] = None
    image_url: Optional[str] = None
    banner_url: Optional[str] = None
    bio: Optional[str] = None
    genres: Optional[str | list[str]] = None  # native: stored text; Lidarr: a list
    country: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    sort_name: Optional[str] = None
    pending_profile_recompute: Optional[int] = None
    art_version: Optional[str] = None
    tags: Optional[list[int]] = None
    album_count: Optional[int] = None
    track_count: Optional[int] = None
    discovery_id: Optional[str] = None
    albums: Optional[list[LibraryAlbumRecord]] = None  # artist detail only
    added_at: Optional[str] = None  # Lidarr mode
    total_track_count: Optional[int] = None  # Lidarr mode
    track_file_count: Optional[int] = None  # Lidarr mode
    size_bytes: Optional[int] = None  # Lidarr mode
    status: Optional[str] = None  # Lidarr mode: continuing / ended
    source: Optional[str] = None  # "lidarr" in Lidarr mode


class LibraryArtistRecord(LibraryArtistFields):
    name: str


class IngestArtistResponse(LibraryArtistFields):
    """The artist (native row, or the Lidarr stub ``{id, artist_name, mode}``) plus what the ingest did."""

    artist_name: Optional[str] = None  # Lidarr mode
    mode: Optional[str] = None  # Lidarr mode
    albums_ingested: int
    tracks_ingested: int
    already_existed: Optional[bool] = None


# ---------------------------------------------------------------------------------------- paged lists and indexes


class IndexGroup(ApiModel):
    label: str
    offset: int
    count: int


class LibraryIndexResponse(ApiModel):
    """Scrubber groups for a list. ``mode`` is only present in Lidarr mode."""

    mode: Optional[str] = None
    sort_key: str
    sort_dir: str
    total: int
    groups: list[IndexGroup]


class _PageBase(ApiModel):
    mode: str
    page: int
    page_size: int
    total: int
    sort_key: str
    sort_dir: str


class ArtistsPage(_PageBase):
    records: list[LibraryArtistRecord]


class AlbumsPage(_PageBase):
    records: list[LibraryAlbumRecord]


class TracksPage(_PageBase):
    records: list[LibraryTrackRecord]


# ------------------------------------------------------------------------------------------------------- stats


class LibraryStats(ApiModel):
    source: str
    artist_count: int
    monitored_artist_count: Optional[int] = None
    unmonitored_artist_count: Optional[int] = None
    continuing_artist_count: Optional[int] = None  # native artists carry no status: null
    ended_artist_count: Optional[int] = None
    album_count: int
    track_count: int
    total_track_count: Optional[int] = None
    track_file_count: Optional[int] = None
    file_count: Optional[int] = None
    missing_track_count: Optional[int] = None
    total_size_bytes: Optional[int] = None
    monitored_track_count: Optional[int] = None  # native only
    cutoff_unmet_track_count: Optional[int] = None  # native only


# ------------------------------------------------------------------------------------------------ small results


class SuccessResponse(ApiModel):
    success: bool


class CommandResponse(ApiModel):
    success: bool
    message: Optional[str] = None


class ArtistRefreshResponse(ApiModel):
    success: bool
    message: Optional[str] = None
    artist_id: Optional[str] = None
    refreshed_at: Optional[str] = None


class BulkArtistsResult(ApiModel):
    artists_updated: int
    albums_monitored: int
    albums_unmonitored: int
    tags_added: Optional[int] = None
    tags_removed: Optional[int] = None


class AlbumsUpdatedResponse(ApiModel):
    albums_updated: int


class TracksUpdatedResponse(ApiModel):
    tracks_updated: int


# -------------------------------------------------------------------------------------------- metadata profiles


class MetadataProfile(ApiModel):
    id: int
    name: str
    primary_types: list[str]
    secondary_types: list[str]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    artist_count: int = 0


class MetadataProfilesResponse(ApiModel):
    profiles: list[MetadataProfile]
    default_profile_id: Optional[int] = None
    primary_types: list[str]
    secondary_types: list[str]


class MetadataProfileDeleteResponse(ApiModel):
    deleted: int
    artists_cleared: int


class MetadataProfileChange(ApiModel):
    albums_to_monitor: int
    albums_to_unmonitor: int
    tracks_to_monitor: int
    tracks_to_unmonitor: int


class MetadataProfilePreview(ApiModel):
    matching: int
    total: int
    would_change: MetadataProfileChange


# ----------------------------------------------------------------------------------------------- item history


class ItemHistoryEvent(ApiModel):
    id: int
    event: str
    created_at: Optional[str] = None
    track_id: Optional[str] = None
    album_id: Optional[str] = None
    artist_id: Optional[str] = None
    track_title: str = ""
    album_title: str = ""
    artist_name: str = ""
    trigger: Optional[str] = None
    trigger_label: Optional[str] = None
    actor_display: str
    message: str = ""
    # Free-form per event type; already redacted by HistoryPresenter for non-admin viewers.
    details: dict[str, Any] = {}


class ItemHistoryOrigin(ApiModel):
    trigger: Optional[str] = None
    trigger_label: Optional[str] = None
    actor_display: str
    created_at: Optional[str] = None


class ItemHistoryResponse(ApiModel):
    entity: Literal["artist", "album", "track"]
    entity_id: str
    origin: Optional[ItemHistoryOrigin] = None
    events: list[ItemHistoryEvent]
    next_before: Optional[int] = None


# ------------------------------------------------------------------------------------------------ availability


class AvailabilityResponse(ApiModel):
    in_library: bool
    monitored: bool
    status: Literal["available", "partial", "cutoff_unmet", "missing", "none"]
    quality: Optional[str] = None
    file_count: int
    track_count: int


# ------------------------------------------------------------------------------------- scanner / migration jobs


class ScanStatus(ApiModel):
    is_scanning: bool
    total_files_found: int = 0
    processed_files: int = 0
    artists_created: int = 0
    albums_created: int = 0
    tracks_created: int = 0
    files_indexed: int = 0
    files_pruned: int = 0
    current_file: Optional[str] = None
    status: str  # idle | scanning | completed | cancelled | failed | skipped
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ScanTriggerResponse(ApiModel):
    success: bool
    status: ScanStatus


class MigrationStatus(ApiModel):
    is_migrating: bool
    artists_migrated: int = 0
    albums_migrated: int = 0
    tracks_migrated: int = 0
    files_migrated: int = 0
    status: str  # idle | running | completed | cancelled | failed
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class MigrationTriggerResponse(ApiModel):
    success: bool
    status: MigrationStatus


# --------------------------------------------------------------------------------------------- manual import


class ManualImportTags(ApiModel):
    """``inspect_audio_file`` output (or the minimal filename fallback when the file cannot be read)."""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    date: Optional[str] = None
    year: Optional[int] = None
    release_year: Optional[int] = None
    track_number: Optional[int] = None
    total_tracks: Optional[int] = None
    disc_number: Optional[int] = None
    total_discs: Optional[int] = None
    codec: Optional[str] = None
    bitrate: Optional[Number] = None
    sample_rate: Optional[Number] = None
    bits_per_sample: Optional[Number] = None
    duration: Optional[Number] = None
    extension: Optional[str] = None
    file_path: Optional[str] = None
    musicbrainz_artistid: Optional[str] = None
    musicbrainz_albumartistid: Optional[str] = None
    artists: Optional[list[str]] = None
    musicbrainz_albumid: Optional[str] = None
    musicbrainz_releasegroupid: Optional[str] = None
    musicbrainz_trackid: Optional[str] = None
    isrc: Optional[str] = None
    quality_full: Optional[str] = None


class ManualImportCandidateTrack(ApiModel):
    id: str
    title: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    album_id: Optional[str] = None
    album_title: Optional[str] = None
    artist_id: Optional[str] = None
    artist_name: Optional[str] = None
    has_file: bool


class ManualImportScanItem(ApiModel):
    file_path: str
    filename: str
    size_bytes: int
    tags: ManualImportTags
    matched_artist_id: Optional[str] = None
    matched_artist_name: Optional[str] = None
    matched_album_id: Optional[str] = None
    matched_album_title: Optional[str] = None
    matched_track_id: Optional[str] = None
    matched_track_title: Optional[str] = None
    confidence: float
    match_strength: str  # strong | weak | none
    suggested_track_id: Optional[str] = None
    candidate_tracks: list[ManualImportCandidateTrack]


class ManualImportResult(ApiModel):
    source_path: Optional[str] = None  # absent when the item had no source path
    status: str  # imported | failed
    error: Optional[str] = None
    destination_path: Optional[str] = None
    artist_id: Optional[str] = None
    album_id: Optional[str] = None
    track_id: Optional[str] = None
    file_id: Optional[str] = None
    mode: Optional[str] = None
    rematch: Optional[bool] = None


class ManualImportCommitResponse(ApiModel):
    imported_count: int
    failed_count: int
    results: list[ManualImportResult]
    download_cleared: bool


class FingerprintMatch(ApiModel):
    score: float
    recording_id: str
    title: Optional[str] = None
    artist: Optional[str] = None


class FingerprintLibraryTrack(ApiModel):
    id: str
    title: Optional[str] = None
    album_id: Optional[str] = None
    artist: Optional[str] = None


class FingerprintResponse(ApiModel):
    success: bool
    message: Optional[str] = None
    fingerprint: Optional[FingerprintMatch] = None
    library_track: Optional[FingerprintLibraryTrack] = None


# ------------------------------------------------------------------------------------------------------ rename


class RenamePreviewItem(ApiModel):
    file_id: str
    track_id: str
    current_path: str
    proposed_path: str
    needs_rename: bool


class RenameApplyResponse(ApiModel):
    renamed_count: int
    errors: list[str]


# -------------------------------------------------------------------------------------------------------- retag


class RetagFieldDiff(ApiModel):
    field: str
    current: Optional[str] = None
    proposed: Optional[str] = None


class RetagPreviewItem(ApiModel):
    file_id: str
    track_id: Optional[str] = None
    path: str
    changes: list[RetagFieldDiff] = Field(default_factory=list)
    diffs: list[RetagFieldDiff] = Field(default_factory=list)
    skipped_reason: Optional[str] = None


class RetagFileResult(ApiModel):
    file_id: str
    status: Literal["ok", "skipped", "error"]
    message: Optional[str] = None
    reason: Optional[str] = None


class RetagApplyResponse(ApiModel):
    results: list[RetagFileResult] = Field(default_factory=list)
    retagged_count: int = 0
    applied_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------------------------ collections


class LibraryCollectionRecord(ApiModel):
    id: str
    name: str
    clean_name: Optional[str] = None
    summary: Optional[str] = None
    poster_url: Optional[str] = None
    monitored: Optional[bool] = None
    foreign_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    album_count: int = 0
    preview_covers: list[str] = []
    albums: Optional[list[LibraryAlbumRecord]] = None  # collection detail only


class CollectionDeleteResponse(ApiModel):
    success: bool
    id: str


class CollectionAlbumResponse(ApiModel):
    success: bool
    collection_id: str
    album_id: str
