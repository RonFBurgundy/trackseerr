"""Response models for ``/api/itunes-import`` (``api/routes/itunes_import.py``). Admin only."""

from typing import Optional

from pydantic import ConfigDict, Field

from trackseerr.api.response_models import ApiModel


class ItunesPreviewPlaylist(ApiModel):
    key: str
    name: str
    is_smart: bool
    item_count: int


class ItunesSuggestedMapping(ApiModel):
    # ``from`` is a Python keyword; FastAPI serializes by alias, so the JSON key stays "from".
    from_: str = Field(alias="from")
    to: str
    sample_matches: int

    model_config = ConfigDict(extra=ApiModel.model_config["extra"], from_attributes=True, populate_by_name=True)


class ItunesSkipped(ApiModel):
    builtin: int = 0
    folders: int = 0
    empty: int = 0


class ItunesPreviewResponse(ApiModel):
    import_id: str
    track_count: int
    playlists: list[ItunesPreviewPlaylist]
    suggested_mappings: list[ItunesSuggestedMapping]
    skipped: ItunesSkipped


class ItunesCommitResponse(ApiModel):
    job_id: str


class ItunesJobPlaylist(ApiModel):
    key: str
    name: str
    state: str
    matched: int = 0
    missing: int = 0
    created_playlist_id: Optional[str] = None
    updated: Optional[bool] = None
    error: Optional[str] = None


class ItunesPlayStats(ApiModel):
    requested: bool
    applied: bool
    note: Optional[str] = None


class ItunesImportStatus(ApiModel):
    state: str
    job_id: Optional[str] = None
    total: int = 0
    done: int = 0
    playlists: list[ItunesJobPlaylist]
    error: Optional[str] = None
    play_stats: Optional[ItunesPlayStats] = None
