"""Native library monitoring options and the single rule deciding whether an album is monitored.

``album_monitored_for_option`` is the Python reference implementation. The bulk SQL in
``Database.bulk_edit_library_artists`` mirrors the same rules (see ``ALBUM_MONITORED_SQL``); the tests assert
that the two agree.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

NATIVE_MONITOR_OPTIONS: tuple[str, ...] = ("all", "albums", "singles_eps", "existing", "future", "none")

# How a playlist or import list reacts to an item: request just the track, monitor its album, monitor its whole
# artist, or only record it.
LIST_MONITOR_MODES: tuple[str, ...] = ("track", "album", "artist", "none")

_ALBUM_TYPE_ALIASES = {"studio": "album", "singles": "single", "eps": "ep"}
# Release dates are only trusted in exactly these shapes (the SQL twin GLOBs the same set); anything else
# falls back to the album's year.
_DATE_RE = re.compile(r"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?$")


def validate_monitor_option(option: object) -> str:
    """Returns ``option`` if it is a native monitor option, else raises ValueError."""
    if not isinstance(option, str) or option not in NATIVE_MONITOR_OPTIONS:
        raise ValueError(
            f"Invalid monitor option {option!r}; expected one of: {', '.join(NATIVE_MONITOR_OPTIONS)}"
        )
    return option


def validate_list_monitor_mode(mode: object) -> str:
    """Returns ``mode`` if it is a list monitor mode, else raises ValueError."""
    if not isinstance(mode, str) or mode not in LIST_MONITOR_MODES:
        raise ValueError(f"Invalid monitor mode {mode!r}; expected one of: {', '.join(LIST_MONITOR_MODES)}")
    return mode


def normalize_album_type(album_type: Optional[str]) -> str:
    """Lower-cases an album type and folds legacy spellings (studio, singles, eps)."""
    t = (album_type or "album").strip().lower() or "album"
    return _ALBUM_TYPE_ALIASES.get(t, t)


def section_to_album_type(section_name: str) -> str:
    """Maps a discovery section name (albums / singles_eps / compilations) to an album type."""
    if section_name == "singles_eps":
        return "single"
    if section_name == "compilations":
        return "compilation"
    return "album"


def _parse_partial_date(value: object) -> Optional[date]:
    """Parses exactly YYYY, YYYY-MM or YYYY-MM-DD (ASCII digits, valid calendar date); missing parts pad to day 1."""
    if value is None:
        return None
    m = _DATE_RE.match(str(value))
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _album_date(release_date: object, year: Optional[int]) -> Optional[date]:
    parsed = _parse_partial_date(release_date)
    if parsed is not None:
        return parsed
    if year is not None:
        try:
            return date(int(year), 1, 1)
        except (TypeError, ValueError):
            return None
    return None


def _added_date(artist_added_at: object) -> Optional[date]:
    if isinstance(artist_added_at, datetime):
        return artist_added_at.date()
    if isinstance(artist_added_at, date):
        return artist_added_at
    if artist_added_at is None:
        return None
    # created_at looks like "YYYY-MM-DD HH:MM:SS"; only the date part matters.
    return _parse_partial_date(str(artist_added_at).strip()[:10])


def album_monitored_for_option(
    option: str,
    *,
    artist_monitored: bool,
    album_type: Optional[str],
    has_files: bool,
    release_date: Optional[str] = None,
    year: Optional[int] = None,
    artist_added_at: object = None,
) -> bool:
    """Decides an album's monitored flag from its artist's monitor option.

    Unknown options and unknown/unparseable dates (for ``future``) yield False. ``future`` means the album's
    release date (year-only dates pad to Jan 1) is strictly after the date the artist was added.
    """
    if not artist_monitored:
        return False
    if option == "all":
        return True
    if option == "albums":
        return normalize_album_type(album_type) == "album"
    if option == "singles_eps":
        return normalize_album_type(album_type) in ("single", "ep")
    if option == "existing":
        return bool(has_files)
    if option == "future":
        released = _album_date(release_date, year)
        added = _added_date(artist_added_at)
        if released is None or added is None:
            return False
        return released > added
    return False


def hydrated_track_monitored(option: Optional[str]) -> bool:
    """Monitored flag for a track created from catalog (MusicBrainz/Deezer) data rather than from a file.

    Under ``existing`` only owned tracks are monitored, so a hydrated track with no file is created unmonitored even
    inside an owned, monitored album. Every other option keeps the tracks following their album.
    """
    return option != "existing"


# SQL twin of album_monitored_for_option for set-based updates. Expects tables aliased as ``a`` (library_albums)
# and ``ar`` (library_artists) and the artist's resulting option/monitored values in ``:opt`` / ``:art_mon``
# style expressions supplied by the caller via format placeholders {opt} and {art_mon}.
ALBUM_DATE_SQL = (
    "CASE "
    "WHEN a.release_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]' AND a.release_date NOT GLOB '0000*' "
    "AND date(a.release_date) = a.release_date THEN a.release_date "
    "WHEN a.release_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' AND a.release_date NOT GLOB '0000*' "
    "AND date(a.release_date || '-01') = a.release_date || '-01' THEN a.release_date || '-01' "
    "WHEN a.release_date GLOB '[0-9][0-9][0-9][0-9]' AND a.release_date <> '0000' THEN a.release_date || '-01-01' "
    "WHEN a.year IS NOT NULL THEN printf('%04d-01-01', a.year) END"
)
ALBUM_TYPE_SQL = (
    "CASE LOWER(TRIM(COALESCE(NULLIF(a.album_type, ''), 'album'))) "
    "WHEN 'studio' THEN 'album' WHEN 'singles' THEN 'single' WHEN 'eps' THEN 'ep' "
    "ELSE LOWER(TRIM(COALESCE(NULLIF(a.album_type, ''), 'album'))) END"
)
HAS_FILES_SQL = (
    "EXISTS (SELECT 1 FROM library_tracks t JOIN library_files f ON f.track_id = t.id WHERE t.album_id = a.id)"
)
ALBUM_MONITORED_SQL = (
    "CASE WHEN {art_mon} = 0 THEN 0 "
    "WHEN {opt} = 'all' THEN 1 "
    f"WHEN {{opt}} = 'albums' THEN ({ALBUM_TYPE_SQL} = 'album') "
    f"WHEN {{opt}} = 'singles_eps' THEN ({ALBUM_TYPE_SQL} IN ('single', 'ep')) "
    f"WHEN {{opt}} = 'existing' THEN {HAS_FILES_SQL} "
    f"WHEN {{opt}} = 'future' THEN COALESCE(({ALBUM_DATE_SQL}) > date(ar.created_at), 0) "
    "ELSE 0 END"
)
