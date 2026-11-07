"""Pure exclusion matching and snapshot models for blocked artists, albums, and tracks."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
import unicodedata

from trackseerr.media_match import norm_name, split_artist_credits

SCOPES = ("artist", "album", "track")
_UNICODE_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def exclusion_norm(text: str | None) -> str:
    """Normalize text for exclusion matching.

    Uses norm_name(text); if non-empty returns it; else Unicode fallback:
    NFKC normalize, casefold, replace [\\W_]+ with space, collapse and strip.
    """
    if not text:
        return ""
    n = norm_name(text)
    if n:
        return n
    fallback = unicodedata.normalize("NFKC", text).casefold()
    cleaned = _UNICODE_NON_WORD_RE.sub(" ", fallback)
    return " ".join(cleaned.split())


def match_key(scope: str, artist: str, title: str | None = None) -> str:
    """Compute normalized match key for an exclusion.

    Scope artist -> exclusion_norm(artist)
    Scope album/track -> exclusion_norm(artist)|exclusion_norm(title)

    Raises ValueError for unknown scope, empty normalized artist, or missing/empty title for album/track.
    """
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope: {scope!r}. Must be one of {SCOPES}")

    norm_artist = exclusion_norm(artist) if artist else ""
    if not norm_artist:
        raise ValueError("Artist name must not be empty")

    if scope == "artist":
        return norm_artist

    norm_title = exclusion_norm(title) if title else ""
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
        norm_a = exclusion_norm(artist)
        if norm_a:
            row = snapshot.by_key.get(("artist", norm_a))
            if row is not None:
                return _row_to_hit(row)
        for p in split_artist_credits(artist):
            credit = exclusion_norm(p)
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
        norm_a = exclusion_norm(artist)
        norm_alb = exclusion_norm(album)
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
        norm_a = exclusion_norm(artist)
        norm_t = exclusion_norm(title)
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


def annotate_exclusions(
    db: Any,
    scope: str,
    rows: list[dict[str, Any]],
    *,
    artist_name_by_id: dict[str, str] | None = None,
) -> None:
    """Annotate library records in-place with exclusion status flags."""
    for row in rows:
        row["excluded"] = False
        row["exclusion_id"] = None
        row["exclusion_reason"] = None

        if not isinstance(row, dict):
            continue

        try:
            hit: ExclusionHit | None = None
            if scope == "artist":
                name = row.get("name")
                if name:
                    mbid = row.get("foreign_artist_id") or row.get("mbid")
                    hit = find_exclusion(db, artist=name, artist_mbid=mbid)
            elif scope == "album":
                title = row.get("title")
                artist_name = row.get("artist_name")
                if not artist_name and artist_name_by_id and row.get("artist_id"):
                    artist_name = artist_name_by_id.get(row["artist_id"])
                if title and artist_name:
                    mbid = (
                        row.get("mb_release_group_id")
                        or row.get("foreign_album_id")
                        or row.get("mbid")
                    )
                    hit = find_exclusion(db, artist=artist_name, album=title, album_mbid=mbid)
            elif scope == "track":
                title = row.get("title")
                artist_name = row.get("artist_name")
                if not artist_name and artist_name_by_id and row.get("artist_id"):
                    artist_name = artist_name_by_id.get(row["artist_id"])
                if title and artist_name:
                    album_title = row.get("album_title")
                    rec_mbid = row.get("mb_recording_id") or row.get("foreign_track_id")
                    hit = find_exclusion(
                        db,
                        artist=artist_name,
                        title=title,
                        album=album_title,
                        recording_mbid=rec_mbid,
                    )

            if hit is not None:
                row["excluded"] = True
                row["exclusion_id"] = hit.id
                row["exclusion_reason"] = hit.describe()
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
