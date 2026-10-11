"""Applies approved track aliases to a playlist's source tracks before matching, and undoes it for the missing list."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackseerr.models import Track
from trackseerr.track_alias_store import track_alias_key

if TYPE_CHECKING:
    from trackseerr.storage import Database


def apply_track_aliases(db: Database, tracks: list[Track]) -> tuple[list[Track], dict[int, Track], set[int]]:
    """Returns ``(effective_tracks, original_by_effective_id, ignored_ids)``.

    An approved ``alias`` swaps in a new Track carrying the canonical title / artist / album (url and duration kept);
    an approved ``ignore`` keeps the track but flags ``id(effective)`` in ``ignored_ids``. Keys are ``id()`` values of
    the effective Track objects, so they are only meaningful while that list is alive.
    """
    aliases = db.approved_aliases_by_key()
    if not aliases:
        return list(tracks), {}, set()
    effective: list[Track] = []
    originals: dict[int, Track] = {}
    ignored: set[int] = set()
    for track in tracks:
        alias = aliases.get(track_alias_key(track.artist, track.title))
        if alias is None:
            effective.append(track)
        elif alias["kind"] == "ignore":
            effective.append(track)
            ignored.add(id(track))
        elif alias.get("target_title") and alias.get("target_artist"):
            swapped = Track(
                title=alias["target_title"],
                artist=alias["target_artist"],
                album=alias.get("target_album") or track.album,
                url=track.url,
                duration_seconds=track.duration_seconds,
            )
            effective.append(swapped)
            originals[id(swapped)] = track
        else:
            effective.append(track)
    return effective, originals, ignored


def restore_missing(missing: list[Track], original_by_effective_id: dict[int, Track], ignored_ids: set[int]) -> list[Track]:
    """Maps missing effective tracks back to the source tracks and drops the ignored ones."""
    return [original_by_effective_id.get(id(t), t) for t in missing if id(t) not in ignored_ids]
