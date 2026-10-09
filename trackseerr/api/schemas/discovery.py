"""Response models for ``/api/discovery`` (``api/routes/discovery.py``).

Items come from the Deezer / iTunes clients (``models.DiscoveryItem.to_dict`` and the detail builders in
``clients/discovery.py``) and are then annotated by ``annotate_item_statuses`` with ``status`` / ``quality`` /
``request_id`` and, for admins only, ``library_artist_id``. Provider metadata varies by source, so most fields are optional
and the routes use ``response_model_exclude_unset=True`` to keep absent keys absent.

``ArtistProfileResponse`` serves admins (full) and everyone else (``artist_profile.shape_for_requester``). The fields marked
admin-only below are never set for a non-admin, so they stay absent from the non-admin JSON.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class DiscoveryItem(ApiModel):
    """A discovery track or album (also the discography entries of an artist)."""

    id: str
    item_type: Optional[str] = None
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    cover_url: Optional[str] = None
    preview_url: Optional[str] = None
    release_date: Optional[str] = None
    status: Optional[str] = None
    quality: Optional[str] = None
    request_id: Optional[str] = None
    library_artist_id: Optional[str] = None  # admin sessions only (see annotate_item_statuses)
    artist_discovery_id: Optional[str] = None
    album_discovery_id: Optional[str] = None
    record_type: Optional[str] = None  # artist discography entries
    track_count: Optional[int] = None  # artist discography entries


class DiscoveryListResponse(ApiModel):
    items: list[DiscoveryItem]
    count: int


class DiscoverySearchResponse(DiscoveryListResponse):
    query: str
    type: str


class DiscoveryAlbumTrack(DiscoveryItem):
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    duration_seconds: Optional[int] = None


class DiscoveryAlbumDetail(DiscoveryItem):
    artist_id: Optional[str] = None
    label: Optional[str] = None
    genres: Optional[list[str]] = None
    duration_seconds: Optional[int] = None
    track_count: Optional[int] = None
    tracks: list[DiscoveryAlbumTrack]


class TrackContributor(ApiModel):
    name: str
    role: str


class DiscoveryTrackDetail(DiscoveryItem):
    duration: Optional[int] = None
    track_position: Optional[int] = None
    disk_number: Optional[int] = None
    isrc: Optional[str] = None
    explicit: Optional[bool] = None
    contributors: Optional[list[TrackContributor]] = None
    bpm: Optional[int] = None
    gain: Optional[float] = None
    label: Optional[str] = None
    genres: Optional[list[str]] = None


class DiscoveryArtistDetail(ApiModel):
    id: str
    name: str
    image_url: Optional[str] = None
    nb_album: Optional[int] = None
    nb_fan: Optional[int] = None
    albums: list[DiscoveryItem]
    singles_eps: list[DiscoveryItem]
    compilations: list[DiscoveryItem]


# ---------------------------------------------------------------------------------------------- artist profile


class ProfileArtist(ApiModel):
    name: str
    image_url: Optional[str] = None
    discovery_id: Optional[str] = None
    # Admin-only: absent from the non-admin shape.
    library_artist_id: Optional[str] = None
    mbid: Optional[str] = None
    link_confidence: Optional[str] = None


class ProfileLibrary(ApiModel):
    """Admin-only library summary (null for everyone else)."""

    artist_id: str
    monitored: bool
    album_count: int
    track_count: int
    track_file_count: int


class ProfileTopTrack(ApiModel):
    id: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None
    preview_url: Optional[str] = None
    status: str
    request_id: Optional[str] = None


class ProfileAlbum(ApiModel):
    """A discography entry. Non-admins only ever get the subset ``_REQUESTER_ITEM_KEYS`` (or ``{title, year, status}``)."""

    id: Optional[str] = None
    item_type: Optional[str] = None
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    cover_url: Optional[str] = None
    release_date: Optional[str] = None
    record_type: Optional[str] = None
    track_count: Optional[int] = None
    status: Optional[str] = None
    request_id: Optional[str] = None
    quality: Optional[str] = None  # admin-only
    year: Optional[int] = None
    have_tracks: Optional[int] = None
    total_tracks: Optional[int] = None
    artist_discovery_id: Optional[str] = None
    library_album_id: Optional[str] = None  # admin-only


class ProfileDiscography(ApiModel):
    albums: list[ProfileAlbum]
    singles_eps: list[ProfileAlbum]
    compilations: list[ProfileAlbum]
    library_only: list[ProfileAlbum]


class ArtistProfileResponse(ApiModel):
    artist: ProfileArtist
    library: Optional[ProfileLibrary] = None
    top_tracks: list[ProfileTopTrack]
    discography: ProfileDiscography
