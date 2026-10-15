"""Explains why a rename preview row proposes a move, by comparing current and proposed paths segment by segment.

Pure functions (no I/O) so each reason is unit-testable. Paths are relative to the library root.
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from trackseerr.naming import VARIOUS_ARTISTS_FOLDER
from trackseerr.storage_common import clean_library_name

ARTIST_FOLDER = "artist_folder"
LOOSE_FILE = "loose_file"
ALBUM_FOLDER_FORMAT = "album_folder_format"
ALBUM_YEAR = "album_year"
ALBUM_TITLE = "album_title"
DISC_LAYOUT = "disc_layout"
TRACK_NUMBER = "track_number"
FILE_NAME_FORMAT = "file_name_format"
FILE_TITLE = "file_title"
COLLISION = "collision"
VERIFIED = "verified"
AUDIO_MISMATCH = "audio_mismatch"
COMPILATION_LINK = "compilation_link"

# A move is pre-selected only when it is purely cosmetic or tidies a stray file: anything that touches numbering,
# titles, years, disc layout or collisions needs a human decision.
SAFE_REASONS = frozenset({ARTIST_FOLDER, LOOSE_FILE, ALBUM_FOLDER_FORMAT, FILE_NAME_FORMAT})

_YEAR_RE = re.compile(r"\((\d{4})\)")
_PREFIXED_NUMBER_RE = re.compile(r"^(?:.*? - )?(\d{1,3}) - (.+)$")
_LEADING_NUMBER_RE = re.compile(r"^(\d{1,3})(?:\s*-\s*|\s+)(.+)$")


def _segments(rel_path: str) -> list[str]:
    return [s for s in rel_path.replace("\\", "/").split("/") if s]


def _album_parts(segment: str) -> tuple[str, str | None]:
    """(clean title without the "(YYYY)" year, year) of an album folder name."""
    years = _YEAR_RE.findall(segment)
    return clean_library_name(_YEAR_RE.sub("", segment)), (years[-1] if years else None)


def _split_stem(file_name: str) -> tuple[str, str]:
    """(stem, extension) of a file name; the extension keeps its dot."""
    dot = file_name.rfind(".")
    if dot <= 0 or len(file_name) - dot > 6:
        return file_name, ""
    return file_name[:dot], file_name[dot:]


def _parse_track_name(stem: str) -> tuple[int | None, str]:
    """(track number, title part) of a file stem, tolerating "Artist - Album - NN - Title" and "NN Title" shapes."""
    for pattern in (_PREFIXED_NUMBER_RE, _LEADING_NUMBER_RE):
        match = pattern.match(stem)
        if match:
            return int(match.group(1)), match.group(2).strip()
    return None, stem.strip()


def _album_reasons(current: str, proposed: str) -> list[str]:
    cur_title, cur_year = _album_parts(current)
    new_title, new_year = _album_parts(proposed)
    reasons: list[str] = []
    if cur_year != new_year:
        reasons.append(ALBUM_YEAR)
    if cur_title != new_title:
        reasons.append(ALBUM_TITLE)
    elif cur_year == new_year and current != proposed:
        reasons.append(ALBUM_FOLDER_FORMAT)
    return reasons


def _disc_reasons(current: Sequence[str], proposed: Sequence[str]) -> list[str]:
    """Disc layout change between the folders below the album folder (a disc subfolder appears, goes or changes).

    A renamed subfolder with the same disc number ("CD 1" vs "Disc 01") is still a layout change: it is never
    auto-selected.
    """
    return [] if list(current) == list(proposed) else [DISC_LAYOUT]


def _file_reasons(current: str, proposed: str) -> list[str]:
    cur_number, cur_title = _parse_track_name(_split_stem(current)[0])
    new_number, new_title = _parse_track_name(_split_stem(proposed)[0])
    reasons: list[str] = []
    if cur_number != new_number:
        reasons.append(TRACK_NUMBER)
    if cur_title != new_title:
        reasons.append(FILE_NAME_FORMAT if clean_library_name(cur_title) == clean_library_name(new_title) else FILE_TITLE)
    return reasons


def classify_rename(current_rel: str, proposed_rel: str) -> list[str]:
    """Reason codes for moving ``current_rel`` to ``proposed_rel`` (root-relative); empty when nothing changes.

    ``collision`` is not decided here: it needs the other rows of the preview (see ``flag_collisions``).
    """
    cur, new = _segments(current_rel), _segments(proposed_rel)
    if not cur or not new:
        return []
    reasons: list[str] = []
    cur_folders = cur[1:-1]
    new_folders = new[1:-1]
    if len(cur) < 2 or cur[0] != new[0]:
        reasons.append(ARTIST_FOLDER)
    if (cur[0] == VARIOUS_ARTISTS_FOLDER) ^ (new[0] == VARIOUS_ARTISTS_FOLDER):
        reasons.append(COMPILATION_LINK)
    if not cur_folders and new_folders:
        reasons.append(LOOSE_FILE)
    elif cur_folders and new_folders:
        reasons += _album_reasons(cur_folders[0], new_folders[0])
        reasons += _disc_reasons(cur_folders[1:], new_folders[1:])
    elif cur_folders:
        reasons.append(ALBUM_TITLE)  # the proposal drops the album folder entirely
    reasons += _file_reasons(cur[-1], new[-1])
    return reasons


def flag_collisions(proposed_paths: Sequence[str]) -> list[bool]:
    """For each proposed path, whether another path in the list proposes the same destination."""
    counts = Counter(proposed_paths)
    return [counts[p] > 1 for p in proposed_paths]


def is_default_selected(reasons: Iterable[str]) -> bool:
    """True when every reason is a cosmetic or tidy-up one (safe to apply in bulk)."""
    return all(r in SAFE_REASONS for r in reasons)
