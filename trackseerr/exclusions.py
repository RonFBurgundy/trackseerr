"""Pure exclusion matching and snapshot models for blocked artists, albums, and tracks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trackseerr.media_match import artist_credits, norm_name

SCOPES = ("artist", "album", "track")


def match_key(scope: str, artist: str, title: str | None = None) -> str:
    """Compute normalized match key for an exclusion.

    Scope artist -> norm_name(artist)
    Scope album/track -> norm_name(artist)|norm_name(title)

    Raises ValueError for unknown scope, empty normalized artist, or missing/empty title for album/track.
    """
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope: {scope!r}. Must be one of {SCOPES}")

    norm_artist = norm_name(artist) if artist else ""
    if not norm_artist:
        raise ValueError("Artist name must not be empty")

    if scope == "artist":
        return norm_artist

    norm_title = norm_name(title) if title else ""
    if not norm_title:
        raise ValueError(f"Title required and must not be empty for {scope} exclusion")

    return f"{norm_artist}|{norm_title}"


@dataclass(frozen=True)
class ExclusionHit:
    """Represents a matched exclusion."""

    id: str
    scope: str
    artist_name: str
    title: str | None = None
    reason: str | None = None

    def describe(self) -> str:
        """Plain English description of the exclusion hit."""
        if self.scope == "artist":
            desc = f'Excluded artist "{self.artist_name}"'
        elif self.scope == "album":
            desc = f'Excluded album "{self.title}" by {self.artist_name}'
        elif self.scope == "track":
            desc = f'Excluded track "{self.title}" by {self.artist_name}'
        else:
            desc = f'Excluded {self.scope} "{self.title or self.artist_name}"'

        if self.reason:
            desc = f"{desc}: {self.reason}"
        return desc


@dataclass(frozen=True)
class ExclusionSnapshot:
    """Immutable snapshot of exclusions indexed for rapid lookups."""

    version: int = 0
    by_mbid: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    by_key: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


def _row_to_hit(row: dict[str, Any]) -> ExclusionHit:
    return ExclusionHit(
        id=str(row["id"]),
        scope=str(row["scope"]),
        artist_name=str(row["artist_name"]),
        title=str(row["title"]) if row.get("title") is not None else None,
        reason=str(row["reason"]) if row.get("reason") is not None else None,
    )


def _check_artist_exclusion(
    snapshot: ExclusionSnapshot,
    artist_mbid: str | None,
    artist: str | None,
) -> ExclusionHit | None:
    """Check artist scope: first by MBID, then by match key / multi-artist credits."""
    if artist_mbid is not None and isinstance(artist_mbid, str) and artist_mbid.strip():
        row = snapshot.by_mbid.get(("artist", artist_mbid.strip().lower()))
        if row is not None:
            return _row_to_hit(row)

    if artist is not None and isinstance(artist, str) and artist.strip():
        norm_a = norm_name(artist)
        if norm_a:
            row = snapshot.by_key.get(("artist", norm_a))
            if row is not None:
                return _row_to_hit(row)
        for credit in artist_credits(artist):
            if credit and credit != norm_a:
                row = snapshot.by_key.get(("artist", credit))
                if row is not None:
                    return _row_to_hit(row)

    return None


def _check_album_exclusion(
    snapshot: ExclusionSnapshot,
    album_mbid: str | None,
    artist: str | None,
    album: str | None,
) -> ExclusionHit | None:
    """Check album scope: first by MBID, then by match key when album given."""
    if album_mbid is not None and isinstance(album_mbid, str) and album_mbid.strip():
        row = snapshot.by_mbid.get(("album", album_mbid.strip().lower()))
        if row is not None:
            return _row_to_hit(row)

    if (
        album is not None
        and isinstance(album, str)
        and album.strip()
        and artist is not None
        and isinstance(artist, str)
        and artist.strip()
    ):
        norm_a = norm_name(artist)
        norm_alb = norm_name(album)
        if norm_a and norm_alb:
            row = snapshot.by_key.get(("album", f"{norm_a}|{norm_alb}"))
            if row is not None:
                return _row_to_hit(row)

    return None


def _check_track_exclusion(
    snapshot: ExclusionSnapshot,
    recording_mbid: str | None,
    artist: str | None,
    title: str | None,
) -> ExclusionHit | None:
    """Check track scope: first by MBID, then by match key when title given."""
    if recording_mbid is not None and isinstance(recording_mbid, str) and recording_mbid.strip():
        row = snapshot.by_mbid.get(("track", recording_mbid.strip().lower()))
        if row is not None:
            return _row_to_hit(row)

    if (
        title is not None
        and isinstance(title, str)
        and title.strip()
        and artist is not None
        and isinstance(artist, str)
        and artist.strip()
    ):
        norm_a = norm_name(artist)
        norm_t = norm_name(title)
        if norm_a and norm_t:
            row = snapshot.by_key.get(("track", f"{norm_a}|{norm_t}"))
            if row is not None:
                return _row_to_hit(row)

    return None


def find_exclusion(
    db: Any,
    *,
    artist: str | None = None,
    title: str | None = None,
    album: str | None = None,
    artist_mbid: str | None = None,
    album_mbid: str | None = None,
    recording_mbid: str | None = None,
) -> ExclusionHit | None:
    """Check if an artist, album, or track matches an active exclusion.

    Lookup order:
    1. artist scope (artist_mbid, then artist match_key / multi-artist credits)
    2. album scope (album_mbid, then album match_key)
    3. track scope (recording_mbid, then track match_key)

    Never raises on missing/blank inputs. MBIDs compare lowercased and stripped.
    """
    if db is None:
        return None

    if isinstance(db, ExclusionSnapshot):
        snapshot = db
    elif hasattr(db, "exclusion_snapshot"):
        snapshot = db.exclusion_snapshot()
    else:
        return None

    if snapshot is None:
        return None

    hit = _check_artist_exclusion(snapshot, artist_mbid, artist)
    if hit is not None:
        return hit

    hit = _check_album_exclusion(snapshot, album_mbid, artist, album)
    if hit is not None:
        return hit

    hit = _check_track_exclusion(snapshot, recording_mbid, artist, title)
    if hit is not None:
        return hit

    return None
