"""Matches playlist tracks against the NATIVE library (no media server involved).

This is what keeps "missing tracks" / monitoring / wanted working when no media server is configured: the
matcher is the one the iTunes import uses (normalized artist + album + title against indexed library files).
"""

from typing import Any

from plex_playlist_sync.itunes_import import TrackMatcher
from plex_playlist_sync.models import Track
from plex_playlist_sync.storage import Database


def match_playlist_tracks_native(db: Database, tracks: list[Track]) -> tuple[list[Track], list[Track]]:
    """Splits ``tracks`` into ``(matched, missing)`` by looking each one up in the native library."""
    matcher = TrackMatcher(db, [])
    matched: list[Track] = []
    missing: list[Track] = []
    for track in tracks:
        entry: dict[str, Any] = {
            "name": track.title,
            "artist": track.artist,
            "album_artist": "",
            "album": track.album,
        }
        if matcher.match(entry) is not None:
            matched.append(track)
        else:
            missing.append(track)
    return matched, missing
