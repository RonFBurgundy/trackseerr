"""Native library monitoring options and the single rule deciding whether an album is monitored.

``album_monitored_for_option`` is the Python reference implementation. The bulk SQL in
``Database.bulk_edit_library_artists`` mirrors the same rules (see ``ALBUM_MONITORED_SQL``); the tests assert
that the two agree.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

NATIVE_MONITOR_OPTIONS: tuple[str, ...] = ("all", "albums", "singles_eps", "existing", "future", "none")

# Default for new native artists and a fresh install's "add" setting: monitor exactly the tracks with files.
DEFAULT_MONITOR_OPTION = "existing"

# How a playlist or import list reacts to an item: request just the track, monitor its album, monitor its whole
# artist, or only record it.
LIST_MONITOR_MODES: tuple[str, ...] = ("track", "album", "artist", "none")


# Deprecated request-field aliases from before the "release profile" -> "metadata profile" rename (accepted for one
# release; responses only ever use the new names).
_DEPRECATED_PROFILE_KEYS = {
    "release_profile_id": "metadata_profile_id",
    "add_release_profile_id": "add_metadata_profile_id",
}


def accept_deprecated_profile_keys(data: Any) -> Any:
    """Pydantic ``mode="before"`` hook: maps the old ``release_profile_id`` request keys onto the new names.

    The new name wins when both are present. The mapped key lands in ``model_fields_set`` like any given field.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for old, new in _DEPRECATED_PROFILE_KEYS.items():
        if old in out:
            legacy = out.pop(old)
            out.setdefault(new, legacy)
    return out


# Metadata profiles (native mode only, optional, off by default). They shape AUTOMATIC monitoring only: they never
# hide a release from the catalog and never block a manual monitor or request.
RELEASE_PRIMARY_TYPES: tuple[str, ...] = ("album", "ep", "single", "broadcast", "other")
# MusicBrainz secondary types; ``studio`` is the pseudo type meaning "no secondary type at all".
RELEASE_SECONDARY_TYPES: tuple[str, ...] = (
    "studio",
    "compilation",
    "soundtrack",
    "spokenword",
    "interview",
    "audiobook",
    "audio drama",
    "live",
    "remix",
    "dj-mix",
    "mixtape/street",
    "demo",
    "field recording",
)

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


def validate_release_types(
    primary_types: object, secondary_types: object
) -> tuple[list[str], list[str]]:
    """Normalises and validates a metadata profile's type lists (lower-cased, de-duplicated, order kept).

    Raises ValueError for non-lists, unknown types, or an empty primary or secondary list (such a profile would
    exclude every release).
    """
    out: list[list[str]] = []
    for label, value, allowed in (
        ("primary", primary_types, RELEASE_PRIMARY_TYPES),
        ("secondary", secondary_types, RELEASE_SECONDARY_TYPES),
    ):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{label}_types must be a list")
        cleaned = list(dict.fromkeys(str(v).strip().lower() for v in value))
        bad = [v for v in cleaned if v not in allowed]
        if bad:
            raise ValueError(f"Invalid {label} type(s) {bad}; expected a subset of: {', '.join(allowed)}")
        if not cleaned:
            raise ValueError(f"{label}_types must contain at least one type")
        out.append(cleaned)
    return out[0], out[1]


def album_in_metadata_profile(
    profile: Optional[Mapping[str, Any]],
    album_type: Optional[str],
    secondary_types: Optional[Sequence[str]] = None,
) -> bool:
    """True when ``profile`` is None (no profile) or the release matches it.

    The primary type is the stored ``album_type`` when it is a primary type, else ``album`` (live/compilation are
    secondary types of an album). A release with no secondary types (or unknown, NULL, ones) is ``studio`` and
    needs the profile to allow ``studio``; otherwise EVERY secondary type must be allowed. When the secondary list
    is unknown but ``album_type`` is ``live`` or ``compilation`` that type is inferred. Mirrors ``IN_PROFILE_SQL``.
    """
    if profile is None:
        return True
    t = normalize_album_type(album_type)
    primary = t if t in RELEASE_PRIMARY_TYPES else "album"
    if secondary_types is None:
        secondary = [t] if t in ("live", "compilation") else []
    else:
        secondary = normalize_secondary_types(secondary_types) or []
    if primary not in {str(p).lower() for p in profile.get("primary_types") or []}:
        return False
    allowed = {str(s).lower() for s in profile.get("secondary_types") or []}
    if not secondary:
        return "studio" in allowed
    return all(s in allowed for s in secondary)


def normalize_secondary_types(raw: Any) -> Optional[list[str]]:
    """The single normaliser for MusicBrainz secondary types: lower-case, stripped, de-duplicated, order-kept list.

    ``None`` (unknown) and anything that is not a list/tuple/set stay ``None`` so inference applies; every writer of
    ``library_albums.secondary_types`` and ``album_in_metadata_profile`` go through here.
    """
    if raw is None or isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    out: list[str] = []
    for item in raw:
        t = str(item).strip().lower() if item else ""
        if t and t not in out:
            out.append(t)
    return out


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
    profile: Optional[Mapping[str, Any]] = None,
    secondary_types: Optional[Sequence[str]] = None,
) -> bool:
    """Decides an album's monitored flag from its artist's monitor option and optional metadata profile.

    ``existing`` ignores the profile (files win: the user owns those releases) and ``none`` is always False. For
    every other option an album outside ``profile`` is not auto-monitored. ``profile=None`` is the original
    behaviour. Unknown options and unknown/unparseable dates (for ``future``) yield False. ``future`` means the album's
    release date (year-only dates pad to Jan 1) is strictly after the date the artist was added.
    """
    if not artist_monitored:
        return False
    if option == "existing":
        return bool(has_files)
    if not album_in_metadata_profile(profile, album_type, secondary_types):
        return False
    if option == "all":
        return True
    if option == "albums":
        return normalize_album_type(album_type) == "album"
    if option == "singles_eps":
        return normalize_album_type(album_type) in ("single", "ep")
    if option == "future":
        released = _album_date(release_date, year)
        added = _added_date(artist_added_at)
        if released is None or added is None:
            return False
        return released > added
    return False


def hydrated_track_monitored(option: Optional[str], has_file: bool = False) -> bool:
    """Monitored flag for a track created from catalog (MusicBrainz/Deezer) data rather than from a file.

    Monitoring is track-granular. Under ``existing`` exactly the tracks with a file are monitored, so a hydrated
    track with no file is created unmonitored even inside an owned, monitored album. Every other option keeps the
    tracks following their album.
    """
    if option == "existing":
        return bool(has_file)
    return True


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
TRACK_HAS_FILE_SQL = "EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = library_tracks.id)"
_PRIMARY_SQL = (
    "CASE WHEN " + ALBUM_TYPE_SQL + " IN ('album', 'ep', 'single', 'broadcast', 'other') THEN " + ALBUM_TYPE_SQL
    + " ELSE 'album' END"
)
_SECONDARY_JSON_SQL = (
    "CASE WHEN a.secondary_types IS NOT NULL AND json_valid(a.secondary_types) "
    "AND json_type(a.secondary_types) = 'array' THEN a.secondary_types "
    f"WHEN {ALBUM_TYPE_SQL} = 'live' THEN '[\"live\"]' "
    f"WHEN {ALBUM_TYPE_SQL} = 'compilation' THEN '[\"compilation\"]' ELSE '[]' END"
)
# SQL twin of album_in_metadata_profile; ``ar.metadata_profile_id`` NULL (or pointing at a deleted profile) = no profile.
IN_PROFILE_SQL = (
    "(ar.metadata_profile_id IS NULL "
    "OR NOT EXISTS (SELECT 1 FROM native_metadata_profiles rp0 WHERE rp0.id = ar.metadata_profile_id) "
    "OR EXISTS (SELECT 1 FROM native_metadata_profiles rp WHERE rp.id = ar.metadata_profile_id "
    f"AND EXISTS (SELECT 1 FROM json_each(rp.primary_types) pt WHERE pt.value = ({_PRIMARY_SQL})) "
    f"AND NOT EXISTS (SELECT 1 FROM json_each({_SECONDARY_JSON_SQL}) st "
    "WHERE st.value NOT IN (SELECT value FROM json_each(rp.secondary_types))) "
    f"AND (json_array_length({_SECONDARY_JSON_SQL}) > 0 "
    "OR EXISTS (SELECT 1 FROM json_each(rp.secondary_types) sx WHERE sx.value = 'studio'))))"
)
ALBUM_MONITORED_SQL = (
    "CASE WHEN {art_mon} = 0 THEN 0 "
    f"WHEN {{opt}} = 'existing' THEN {HAS_FILES_SQL} "
    f"WHEN NOT {IN_PROFILE_SQL} THEN 0 "
    "WHEN {opt} = 'all' THEN 1 "
    f"WHEN {{opt}} = 'albums' THEN ({ALBUM_TYPE_SQL} = 'album') "
    f"WHEN {{opt}} = 'singles_eps' THEN ({ALBUM_TYPE_SQL} IN ('single', 'ep')) "
    f"WHEN {{opt}} = 'future' THEN COALESCE(({ALBUM_DATE_SQL}) > date(ar.created_at), 0) "
    "ELSE 0 END"
)
