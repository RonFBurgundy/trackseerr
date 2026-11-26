"""Matches playlist tracks against the NATIVE library (no media server involved).

This is what keeps "missing tracks" / monitoring / wanted working when no media server is configured: the
matcher is the one the iTunes import uses (normalized artist + album + title against indexed library files).
"""

from typing import Sequence

from trackseerr.itunes_import import TrackMatcher
from trackseerr.media_match import split_artist_credits
from trackseerr.media_servers.song_scoring import clean_title
from trackseerr.models import Track
from trackseerr.playlist_identity import LibraryHit, hit_from_track
from trackseerr.storage import Database


def _match_with_fallbacks(matcher: TrackMatcher, track: Track) -> tuple[str, str] | None:
    """Try ``TrackMatcher`` with raw metadata, then cleaned title and/or first credited artist."""
    cleaned_title = clean_title(track.title) or track.title
    credits = split_artist_credits(track.artist)
    first_artist = credits[0] if credits else track.artist
    attempts = [
        (track.title, track.artist),
        (cleaned_title, track.artist),
        (track.title, first_artist),
        (cleaned_title, first_artist),
    ]
    for title, artist in dict.fromkeys(attempts):
        if not title:
            continue
        matched = matcher.match(
            {
                "name": title,
                "artist": artist,
                "album_artist": "",
                "album": track.album,
            }
        )
        if matched is not None:
            return matched
    return None


def resolve_playlist_library_hits(
    db: Database, tracks: Sequence[Track]
) -> tuple[dict[int, LibraryHit], set[int]]:
    """Resolve ``tracks`` against the native library once, returning ``(library_hits, library_missing)``."""
    matcher = TrackMatcher(db, [])
    hits: dict[int, LibraryHit] = {}
    missing: set[int] = set()
    for idx, track in enumerate(tracks):
        pin_hit = db.get_library_pin(track.title, track.artist) if hasattr(db, "get_library_pin") else None
        if pin_hit is not None:
            hits[idx] = pin_hit
            continue
        matched = _match_with_fallbacks(matcher, track)
        if matched is None:
            missing.add(idx)
            continue
        track_id, via = matched
        row = db.get_library_track(track_id)
        hit = hit_from_track(db, row, via=via) if row else None
        if hit is not None:
            hits[idx] = hit
        else:
            missing.add(idx)
    return hits, missing


def match_playlist_tracks_native(db: Database, tracks: list[Track]) -> tuple[list[Track], list[Track]]:
    """Splits ``tracks`` into ``(matched, missing)`` by looking each one up in the native library."""
    matcher = TrackMatcher(db, [])
    matched: list[Track] = []
    missing: list[Track] = []
    for track in tracks:
        if hasattr(db, "get_library_pin") and db.get_library_pin(track.title, track.artist) is not None:
            matched.append(track)
            continue
        if _match_with_fallbacks(matcher, track) is not None:
            matched.append(track)
        else:
            missing.append(track)
    return matched, missing
