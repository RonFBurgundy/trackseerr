"""Response and request models for ``/api/smart-collections``."""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field

from trackseerr.api.response_models import ApiModel
from trackseerr.library_filters import LibraryFacetFilter
from trackseerr.smart_collections import SmartRules

SortOption = Literal["random", "year_desc", "year_asc", "popularity_desc", "added_desc", "artist"]


class SmartRulesBody(BaseModel):
    genres: list[str] = Field(default_factory=list, max_length=20)
    exclude_genres: list[str] = Field(default_factory=list, max_length=20)
    countries: list[str] = Field(default_factory=list, max_length=20)
    year_from: Optional[int] = Field(default=None, ge=1000, le=3000)
    year_to: Optional[int] = Field(default=None, ge=1000, le=3000)
    album_types: list[str] = Field(default_factory=list, max_length=20)
    artist_types: list[str] = Field(default_factory=list, max_length=20)
    members_min: Optional[int] = Field(default=None, ge=1, le=500)
    members_max: Optional[int] = Field(default=None, ge=1, le=500)
    formed_from: Optional[int] = None
    formed_to: Optional[int] = None
    popularity_min: Optional[int] = Field(default=None, ge=0)
    popularity_max: Optional[int] = Field(default=None, ge=0)
    tag_ids: list[int] = Field(default_factory=list, max_length=20)
    sort: SortOption = "random"
    limit: int = Field(default=100, ge=1, le=1000)

    def to_smart_rules(self) -> SmartRules:
        facet_filter = LibraryFacetFilter(
            genres=tuple(self.genres),
            exclude_genres=tuple(self.exclude_genres),
            countries=tuple(self.countries),
            year_from=self.year_from,
            year_to=self.year_to,
            album_types=tuple(self.album_types),
            artist_types=tuple(self.artist_types),
            members_min=self.members_min,
            members_max=self.members_max,
            formed_from=self.formed_from,
            formed_to=self.formed_to,
            popularity_min=self.popularity_min,
            popularity_max=self.popularity_max,
            tag_ids=tuple(self.tag_ids),
        )
        return SmartRules(filter=facet_filter, sort=self.sort, limit=self.limit)

    @classmethod
    def from_smart_rules(cls, rules: SmartRules) -> SmartRulesBody:
        f = rules.filter
        return cls(
            genres=list(f.genres),
            exclude_genres=list(f.exclude_genres),
            countries=list(f.countries),
            year_from=f.year_from,
            year_to=f.year_to,
            album_types=list(f.album_types),
            artist_types=list(f.artist_types),
            members_min=f.members_min,
            members_max=f.members_max,
            formed_from=f.formed_from,
            formed_to=f.formed_to,
            popularity_min=f.popularity_min,
            popularity_max=f.popularity_max,
            tag_ids=list(f.tag_ids),
            sort=rules.sort,  # type: ignore[arg-type]
            limit=rules.limit,
        )


class SmartRulesRecord(ApiModel):
    genres: list[str] = Field(default_factory=list)
    exclude_genres: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    album_types: list[str] = Field(default_factory=list)
    artist_types: list[str] = Field(default_factory=list)
    members_min: Optional[int] = None
    members_max: Optional[int] = None
    formed_from: Optional[int] = None
    formed_to: Optional[int] = None
    popularity_min: Optional[int] = None
    popularity_max: Optional[int] = None
    tag_ids: list[int] = Field(default_factory=list)
    sort: SortOption = "random"
    limit: int = 100

    @classmethod
    def from_smart_rules(cls, rules: SmartRules) -> SmartRulesRecord:
        f = rules.filter
        return cls(
            genres=list(f.genres),
            exclude_genres=list(f.exclude_genres),
            countries=list(f.countries),
            year_from=f.year_from,
            year_to=f.year_to,
            album_types=list(f.album_types),
            artist_types=list(f.artist_types),
            members_min=f.members_min,
            members_max=f.members_max,
            formed_from=f.formed_from,
            formed_to=f.formed_to,
            popularity_min=f.popularity_min,
            popularity_max=f.popularity_max,
            tag_ids=list(f.tag_ids),
            sort=rules.sort,  # type: ignore[arg-type]
            limit=rules.limit,
        )


class SmartPreviewTrack(ApiModel):
    title: str
    artist: str
    album: str
    year: Optional[int] = None


class SmartPreviewResponse(ApiModel):
    track_count: int
    tracks: list[SmartPreviewTrack]


class SmartCollectionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    rules: SmartRulesBody
    targets: list[str] = Field(default_factory=list)
    keep_in_sync: bool = False


class SmartCollectionUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    rules: Optional[SmartRulesBody] = None


class SmartCollectionRecord(ApiModel):
    id: str
    name: str
    description: str = ""
    enabled: bool
    rules: SmartRulesRecord
    targets: list[str]
    last_synced_at: Optional[str] = None
    sync_status: str
    track_count: int

